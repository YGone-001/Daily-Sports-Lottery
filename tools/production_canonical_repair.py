"""
tools/production_canonical_repair.py
====================================
Auditable, owner-authorized production repair entry point for historical
Canonical identity collisions in ``data/daily_matches.json``.

WHY THIS EXISTS
---------------
``utils.scraper`` enforces a fail-closed invariant: the Canonical tracking set
may never contain two records sharing one ``id``.  That guard was introduced
*after* a legacy duplicate (``500l-2026-10-09-1362422``) had already been
persisted, so every subsequent refresh aborted before writing and both
``POST /api/refresh`` and the background auto-sync failed permanently.

The two pre-existing tools deliberately refuse production paths::

    tools/staging_canonical_repair.py    -> PRODUCTION_REPAIR_AUTHORIZATION = NOT_GRANTED
    tools/canonical_snapshot_adapter.py  -> rejects <repo>/data

This module closes that operational gap with a guarded, auditable production
entry point.  It is NOT a general-purpose editor: it performs exactly one
operation -- the ``N -> N - 1`` de-duplication of a single Canonical identity --
and refuses anything else.

SAFETY MODEL
------------
1.  No write happens without an explicit *authorization artifact* whose
    ``data_dir``, ``expected_source_sha256``, ``target_canonical_id`` and
    ``expected_candidate_sha256`` all bind to the exact live dataset.  Any drift
    fails closed before a single byte is written.
2.  ``apply`` additionally requires ``--confirm-apply`` and an ``--operator``
    matching the artifact.
3.  Evidence-epoch archives are refused; the workspace must live outside the
    data directory.
4.  The very same ``.refresh.lock`` byte-range lock used by ``utils.scraper``
    is held for the whole transaction, so a concurrent refresh cannot interleave.
5.  Seven-phase journal, forensic archive, atomic ``os.replace`` and crash
    recovery with fail-closed UNKNOWN classification.
6.  Postconditions are re-derived from the archived raw bytes rather than
    trusted from the active file.

The cryptographic and serialization primitives (``hash_record``,
``serialize_record``, ``sha256_file``, ``valid_full_time_score``) are imported
from the staging tool so both entry points speak one identical evidence
language -- the same record must hash the same way everywhere.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

# --- shared evidence primitives (single source of truth for hashing) ---------
from tools.staging_canonical_repair import (  # noqa: E402
    ImmutableStoreMutationError,
    PreconditionViolationError,
    ProtectedPathViolationError,
    RecoveryStateError,
    StagingLockError,
    StagingMigrationError,
    canonicalize_path,
    group_attempts,
    hash_record,
    is_subpath_or_equal,
    read_json_strict,
    serialize_record,
    sha256_bytes,
    sha256_file,
    valid_full_time_score,
)


# =============================================================================
# Constants
# =============================================================================

CANONICAL_FILE = "daily_matches.json"

NON_CANONICAL_STORES: List[str] = [
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
]

ALL_STORES: List[str] = NON_CANONICAL_STORES + [CANONICAL_FILE]

AUTHORIZATION_SCHEMA_VERSION = "1.0.0"
REPAIR_SCOPE = "PRODUCTION_CANONICAL_DEDUP"

AUTHORIZATION_REQUIRED_FIELDS: List[str] = [
    "data_dir",
    "target_canonical_id",
    "expected_source_sha256",
    "expected_candidate_sha256",
    "operator",
    "approved_at",
    "reason",
]

EVIDENCE_EPOCH_PREFIX = "_quiescent_evidence_backup"

DEFAULT_WORKSPACE_DIRNAME = "repair_runs"

MISSING = "MISSING"

#: Seven-phase production transaction sequence.  Phase 3 is named for what it
#: actually authorizes here -- a *production* repair -- rather than reusing the
#: staging tool's simulation-oriented state name.
JOURNAL_STATES: List[str] = [
    "PREPARED",
    "EVIDENCE_VERIFIED",
    "AUTHORIZED_FOR_PRODUCTION_REPAIR",
    "COMMIT_INTENT_DURABLE",
    "PROJECTION_COMMITTED",
    "POSTCONDITIONS_VERIFIED",
    "COMPLETION_RECORDED",
]


# =============================================================================
# Exceptions
# =============================================================================

class AuthorizationError(StagingMigrationError):
    """Raised when the owner authorization artifact is missing, malformed, or
    does not bind to the live dataset."""
    pass


# =============================================================================
# Path safety
# =============================================================================

def assert_production_data_dir(data_dir: str) -> str:
    """
    Validate that ``data_dir`` is a plausible live Canonical data directory.

    Rejects: empty input, filesystem/drive roots, and preserved evidence epoch
    archives.  Requires ``daily_matches.json`` to be present so the tool can
    never be pointed at an unrelated directory by accident.
    """
    if not data_dir or not str(data_dir).strip():
        raise ProtectedPathViolationError("Empty data directory provided.")

    norm = canonicalize_path(data_dir)
    parts = norm.replace("\\", "/").split("/")
    for part in parts:
        if part.startswith(EVIDENCE_EPOCH_PREFIX):
            raise ProtectedPathViolationError(
                f"Refusing to operate on preserved evidence epoch: '{data_dir}'"
            )

    # Refuse drive roots / filesystem roots (e.g. 'C:\\', '/').
    parent = canonicalize_path(os.path.dirname(norm))
    if parent == norm:
        raise ProtectedPathViolationError(
            f"Refusing to operate on a filesystem root: '{data_dir}'"
        )

    if not os.path.isdir(norm):
        raise PreconditionViolationError(f"Data directory not found: {data_dir}")

    if not os.path.exists(os.path.join(norm, CANONICAL_FILE)):
        raise PreconditionViolationError(
            f"'{CANONICAL_FILE}' not found in data directory: {data_dir}"
        )

    return norm


def assert_workspace_outside_data_dir(workspace: str, data_dir: str) -> str:
    """
    Validate that ``workspace`` (journal / archive / output root) is neither
    inside the live data directory nor a parent of it.
    """
    if not workspace or not str(workspace).strip():
        raise ProtectedPathViolationError("Empty workspace provided.")

    norm_ws = canonicalize_path(workspace)
    parts = norm_ws.replace("\\", "/").split("/")
    for part in parts:
        if part.startswith(EVIDENCE_EPOCH_PREFIX):
            raise ProtectedPathViolationError(
                f"Refusing to place a workspace inside an evidence epoch: '{workspace}'"
            )

    if is_subpath_or_equal(norm_ws, data_dir):
        raise ProtectedPathViolationError(
            f"Workspace '{workspace}' must not live inside the data directory '{data_dir}'"
        )
    if is_subpath_or_equal(data_dir, norm_ws):
        raise ProtectedPathViolationError(
            f"Workspace '{workspace}' must not be a parent of the data directory '{data_dir}'"
        )

    return norm_ws


# =============================================================================
# Serialization / hashing
# =============================================================================

def derive_file_bytes(data: Any) -> bytes:
    """
    Reproduce the exact on-disk byte representation produced by
    ``utils.atomic_json.atomic_write_json`` (``ensure_ascii=False``, ``indent=2``,
    ``sort_keys=True``, text-mode newline translation).

    Verified against the live ``data/daily_matches.json``: the derived SHA-256
    equals the on-disk SHA-256 byte for byte.
    """
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    if os.name == "nt":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def derive_file_hash(data: Any) -> str:
    return sha256_bytes(derive_file_bytes(data))


def write_json_atomic(path: str, data: Any) -> None:
    """Single-file atomic replacement with flush, fsync and ``os.replace``."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_repair_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def restore_from_archive_atomic(archive_path: str, target_path: str, expected_hash: str) -> None:
    """Atomically restore ``target_path`` from ``archive_path`` and re-verify."""
    if not os.path.exists(archive_path):
        raise RecoveryStateError(f"Forensic archive missing: {archive_path}")
    if sha256_file(archive_path) != expected_hash:
        raise RecoveryStateError(f"Forensic archive hash mismatch: {archive_path}")

    directory = os.path.dirname(os.path.abspath(target_path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_recover_", suffix=".json")
    try:
        with open(archive_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            while chunk := src.read(65536):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, target_path)
    except BaseException:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if sha256_file(target_path) != expected_hash:
        raise RecoveryStateError("Post-recovery replacement hash verification failed!")


# =============================================================================
# Locking (shared with utils.scraper)
# =============================================================================

@contextmanager
def acquire_repair_lock(data_dir: str):
    """
    Acquire the cross-process exclusive OS byte lock on
    ``<data_dir>/.refresh.lock`` -- the identical lock used by
    ``utils.concurrency.acquire_refresh_lock`` -- so a production repair and a
    provider refresh can never interleave.
    """
    os.makedirs(data_dir, exist_ok=True)
    lock_file = os.path.join(data_dir, ".refresh.lock")
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        st = os.fstat(fd)
        if st.st_size == 0:
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)

        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise StagingLockError(f"Repair lock busy or denied: {exc}") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StagingLockError(f"Repair lock busy or denied: {exc}") from None

        yield fd
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)


# =============================================================================
# Journal
# =============================================================================

class RepairJournal:
    """
    Append-only disposition journal for a production repair.

    Lazy filesystem initialization keeps ``inspect`` / ``dry-run`` / ``verify``
    strictly read-only.
    """

    def __init__(self, journal_file: str, writable: bool = False):
        self.journal_file = os.path.abspath(journal_file)
        if writable:
            os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)

    def append_entry(self, state: str, payload: dict) -> None:
        if state not in JOURNAL_STATES:
            raise StagingMigrationError(f"Unknown state: {state}")
        os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)
        entry = {
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        with open(self.journal_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_entries(self) -> List[dict]:
        if not os.path.exists(self.journal_file):
            return []
        entries: List[dict] = []
        with open(self.journal_file, "r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception as exc:  # noqa: BLE001
                    entries.append(
                        {"state": "CORRUPTED_LINE", "line_no": line_no, "raw": line, "error": str(exc)}
                    )
        return entries

    def validate_chain(self, entries: List[dict], expected_migration_id: Optional[str] = None) -> None:
        """Enforce strict transition ordering, identity binding and attempt scoping."""
        if not entries:
            return

        for entry in entries:
            if entry.get("state") == "CORRUPTED_LINE":
                raise RecoveryStateError(f"Corrupted journal line detected: line {entry.get('line_no')}")

        first_payload = entries[0].get("payload") if isinstance(entries[0].get("payload"), dict) else {}
        if entries[0].get("state") != "PREPARED":
            raise RecoveryStateError(f"Journal must begin with PREPARED, got '{entries[0].get('state')}'")
        if first_payload.get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE":
            raise RecoveryStateError("Journal cannot begin with a rollback PREPARED entry")

        intent_fields = (
            "source_file_sha256",
            "projected_file_sha256",
            "candidate_record_sha256",
            "record_a_sha256",
            "record_b_sha256",
        )
        migration_id: Optional[str] = None
        intent_hashes: Optional[dict] = None
        completion_count = 0

        for idx, entry in enumerate(entries):
            state = entry.get("state")
            if state not in JOURNAL_STATES:
                raise RecoveryStateError(f"Unknown state '{state}' in journal")
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                raise RecoveryStateError(f"Entry {idx} payload must be a JSON object")

            mig = payload.get("migration_id")
            if not mig or not isinstance(mig, str) or not mig.strip():
                raise RecoveryStateError(f"Entry {idx} '{state}' is missing a non-empty migration_id")
            if migration_id is None:
                migration_id = mig
            elif mig != migration_id:
                raise RecoveryStateError(f"Conflicting migration_id in journal: '{mig}' != '{migration_id}'")
            if expected_migration_id and mig != expected_migration_id:
                raise RecoveryStateError(f"Mismatched migration ID: expected '{expected_migration_id}', got '{mig}'")

            if state == "COMPLETION_RECORDED":
                completion_count += 1
                if completion_count > 1:
                    raise RecoveryStateError("Duplicate COMPLETION_RECORDED entry in journal")

            if state == "COMMIT_INTENT_DURABLE":
                for field in intent_fields:
                    value = payload.get(field)
                    if not value or not isinstance(value, str) or not value.strip():
                        raise RecoveryStateError(
                            f"Missing required intent hash field '{field}' in COMMIT_INTENT_DURABLE"
                        )
                current = {k: payload.get(k) for k in intent_fields}
                if intent_hashes is None:
                    intent_hashes = current
                elif current != intent_hashes:
                    raise RecoveryStateError("Conflicting intent hashes across COMMIT_INTENT_DURABLE entries")

        order = JOURNAL_STATES
        attempts = group_attempts(entries)
        for att_idx, attempt in enumerate(attempts):
            if not attempt or attempt[0].get("state") != "PREPARED":
                raise RecoveryStateError(f"Attempt {att_idx} must begin with PREPARED")

            last_index = 0
            for idx, entry in enumerate(attempt[1:], 1):
                state = entry.get("state")
                payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                prev_state = attempt[idx - 1].get("state")

                if state == "PREPARED" and payload.get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE":
                    if prev_state != "COMMIT_INTENT_DURABLE":
                        raise RecoveryStateError(
                            f"Invalid rollback reset: previous state was '{prev_state}', "
                            f"expected 'COMMIT_INTENT_DURABLE'"
                        )
                    if idx != len(attempt) - 1:
                        raise RecoveryStateError(f"Rollback to PREPARED must be terminal in attempt {att_idx}")
                    continue

                if state == "PREPARED":
                    raise RecoveryStateError("Duplicate PREPARED inside attempt")

                state_idx = order.index(state)
                if state_idx <= last_index:
                    raise RecoveryStateError(f"Journal state regression/duplication: '{state}' after '{prev_state}'")
                if state_idx > last_index + 1:
                    raise RecoveryStateError(
                        f"Journal skipped intermediate state: expected '{order[last_index + 1]}', got '{state}'"
                    )
                last_index = state_idx

            if att_idx < len(attempts) - 1 and last_index >= order.index("PROJECTION_COMMITTED"):
                raise RecoveryStateError(
                    f"Attempt {att_idx} committed the projection; a retry attempt is illegal"
                )

        if completion_count == 1 and entries[-1].get("state") != "COMPLETION_RECORDED":
            raise RecoveryStateError("COMPLETION_RECORDED must be the final journal entry")

    def get_latest_valid_state(self) -> Optional[str]:
        for entry in reversed(self.read_entries()):
            if entry.get("state") in JOURNAL_STATES:
                return entry.get("state")
        return None


# =============================================================================
# Collision inspection & candidate synthesis
# =============================================================================

def inspect_dataset(data_dir: str) -> dict:
    """Strictly read-only inspection. Zero writes, zero directory creation."""
    norm = assert_production_data_dir(data_dir)
    daily_file = os.path.join(norm, CANONICAL_FILE)
    daily_data = read_json_strict(daily_file)
    if not isinstance(daily_data, dict):
        raise PreconditionViolationError("Canonical root is not a JSON object")
    matches = daily_data.get("matches", [])
    if not isinstance(matches, list):
        raise PreconditionViolationError("Canonical 'matches' is not an array")

    id_counts: Dict[str, int] = {}
    indices: Dict[str, List[int]] = {}
    for idx, match in enumerate(matches):
        if not isinstance(match, dict):
            raise PreconditionViolationError(f"Match entry at index {idx} is not an object")
        mid = match.get("id")
        if not mid or not isinstance(mid, str):
            id_counts["__INVALID__"] = id_counts.get("__INVALID__", 0) + 1
            continue
        id_counts[mid] = id_counts.get(mid, 0) + 1
        indices.setdefault(mid, []).append(idx)

    duplicate_groups: Dict[str, dict] = {}
    for mid, count in id_counts.items():
        if count > 1:
            recs = [matches[i] for i in indices.get(mid, [])]
            duplicate_groups[mid] = {
                "count": count,
                "indices": indices.get(mid, []),
                "record_sha256": [hash_record(r) for r in recs],
            }

    store_hashes: Dict[str, str] = {}
    for store in ALL_STORES:
        path = os.path.join(norm, store)
        store_hashes[store] = sha256_file(path) if os.path.exists(path) else MISSING

    return {
        "data_dir": norm,
        "total_matches": len(matches),
        "unique_ids_count": len(id_counts),
        "duplicate_ids": {k: v for k, v in id_counts.items() if v > 1},
        "duplicate_groups": duplicate_groups,
        "store_hashes": store_hashes,
        "meta_total": (daily_data.get("meta") or {}).get("total") if isinstance(daily_data.get("meta"), dict) else None,
    }


def _rank_record(match: dict) -> tuple:
    """
    Ordering used to choose the synthesis base record:

    1. A terminal ``finished`` record carrying a strictly valid full-time score
       wins (it is the authoritative identity/result source).
    2. Ties break toward the record with the most complete (longest) club names,
       which prefers the standard full name over an abbreviated alias.
    """
    terminal = 1 if (match.get("status") == "finished" and valid_full_time_score(match.get("score"))) else 0
    name_len = len(str(match.get("home") or "")) + len(str(match.get("away") or ""))
    return (terminal, name_len)


def synthesize_candidate(records: List[dict]) -> Tuple[dict, dict]:
    """
    Deterministically synthesize one canonical record from the colliding set.

    Policy ("Owner Decision A", generalized): start from the highest-ranked
    record, then fill only *empty* fields from the siblings.  Populated fields
    are never overwritten, so the terminal result, the standard club names, the
    competition number and the closing odds all survive in one record.

    Returns ``(candidate, field_diff)``.
    """
    if not records or len(records) < 2:
        raise PreconditionViolationError("synthesize_candidate requires at least 2 records")

    base = max(records, key=_rank_record)
    candidate = copy.deepcopy(base)
    provenance = {k: "base" for k in base}

    for record in records:
        if record is base:
            continue
        for key, value in record.items():
            if key == "id":
                continue
            current = candidate.get(key)
            is_empty = current is None or current == "" or current == [] or current == {}
            if is_empty and value not in (None, "", [], {}):
                candidate[key] = copy.deepcopy(value)
                provenance[key] = "sibling_fill"

    field_diff = {
        "id": {"policy": "shared", "value": candidate.get("id")},
        "base_record_sha256": hash_record(base),
        "provenance": provenance,
        "record_sha256": [hash_record(r) for r in records],
    }
    return candidate, field_diff


def default_migration_id(target_canonical_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    safe = "".join(ch for ch in target_canonical_id if ch.isalnum() or ch in "-_")
    return f"MIG-{stamp}-CANONICAL-{safe}"


# =============================================================================
# Authorization artifact
# =============================================================================

def authorization_fingerprint(authorization: dict) -> str:
    return sha256_bytes(serialize_record(authorization).encode("utf-8"))


def load_authorization(path: str) -> dict:
    if not path or not os.path.exists(path):
        raise AuthorizationError(f"Authorization artifact not found: {path}")
    data = read_json_strict(path)
    if not isinstance(data, dict):
        raise AuthorizationError("Authorization artifact must be a JSON object")

    if data.get("authorization_schema_version") != AUTHORIZATION_SCHEMA_VERSION:
        raise AuthorizationError(
            f"Unsupported authorization schema: '{data.get('authorization_schema_version')}'"
        )
    if data.get("scope") != REPAIR_SCOPE:
        raise AuthorizationError(f"Unexpected authorization scope: '{data.get('scope')}'")
    if data.get("authorized") is not True:
        raise AuthorizationError("Authorization artifact must carry 'authorized': true")

    for field in AUTHORIZATION_REQUIRED_FIELDS:
        value = data.get(field)
        if value is None or not str(value).strip():
            raise AuthorizationError(f"Authorization artifact missing required field '{field}'")

    return data


def build_authorization(
    data_dir: str,
    target_canonical_id: str,
    operator: str,
    reason: str,
    migration_id: Optional[str] = None,
) -> dict:
    """
    Read-only: derive the exact expected source hash and candidate hash for one
    collision and package them into a reviewable authorization artifact.
    """
    if not operator or not str(operator).strip():
        raise AuthorizationError("--operator is required to build an authorization artifact")
    if not reason or not str(reason).strip():
        raise AuthorizationError("--reason is required to build an authorization artifact")

    report = inspect_dataset(data_dir)
    groups = report["duplicate_groups"]
    if not groups:
        raise PreconditionViolationError(
            "Dataset is already free of duplicate Canonical IDs; nothing to authorize."
        )
    if set(groups.keys()) != {target_canonical_id}:
        raise PreconditionViolationError(
            f"Refusing to authorize: unexpected duplicate groups {sorted(groups.keys())}"
        )
    if groups[target_canonical_id]["count"] != 2:
        raise PreconditionViolationError(
            f"Refusing to authorize: target '{target_canonical_id}' has "
            f"{groups[target_canonical_id]['count']} records, expected exactly 2"
        )

    norm = assert_production_data_dir(data_dir)
    daily_data = read_json_strict(os.path.join(norm, CANONICAL_FILE))
    matches = daily_data.get("matches", [])
    records = [m for m in matches if m.get("id") == target_canonical_id]
    candidate, field_diff = synthesize_candidate(records)

    return {
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "scope": REPAIR_SCOPE,
        "authorized": True,
        "migration_id": migration_id or default_migration_id(target_canonical_id),
        "data_dir": norm,
        "target_canonical_id": target_canonical_id,
        "expected_source_sha256": sha256_file(os.path.join(norm, CANONICAL_FILE)),
        "expected_source_total": report["total_matches"],
        "expected_candidate_sha256": hash_record(candidate),
        "operator": operator,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "field_diff": field_diff,
    }


# =============================================================================
# Repairer
# =============================================================================

class ProductionCanonicalRepairer:
    """Guarded production de-duplication of a single Canonical identity."""

    def __init__(
        self,
        data_dir: str,
        workspace: Optional[str] = None,
        migration_id: Optional[str] = None,
        writable: bool = True,
    ):
        self.data_dir = assert_production_data_dir(data_dir)
        self._migration_id_explicit = migration_id is not None
        self._workspace_explicit = bool(workspace)
        self._writable = writable
        self.migration_id = migration_id or default_migration_id("pending")

        if workspace:
            self.workspace = assert_workspace_outside_data_dir(workspace, self.data_dir)
        else:
            self.workspace = assert_workspace_outside_data_dir(
                os.path.join(os.path.dirname(self.data_dir), DEFAULT_WORKSPACE_DIRNAME, self.migration_id),
                self.data_dir,
            )
        self._bind_paths()

    def _bind_paths(self) -> None:
        self.archive_dir = os.path.join(self.workspace, "archive")
        self.journal_dir = os.path.join(self.workspace, "journal")
        self.output_dir = os.path.join(self.workspace, "output")
        self.journal = RepairJournal(
            os.path.join(self.journal_dir, "disposition_journal.jsonl"),
            writable=self._writable,
        )

    # -- helpers ------------------------------------------------------------

    def get_store_hashes(self) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        for store in ALL_STORES:
            path = os.path.join(self.data_dir, store)
            hashes[store] = sha256_file(path) if os.path.exists(path) else MISSING
        return hashes

    def _daily_file(self) -> str:
        return os.path.join(self.data_dir, CANONICAL_FILE)

    def _raw_archive(self) -> str:
        return os.path.join(self.archive_dir, "daily_matches_raw.json")

    def _derive_projected_from_raw(self, raw_data: dict, target_id: str) -> Tuple[list, dict]:
        """Independently derive the projected dataset from archived raw bytes."""
        matches = raw_data.get("matches", [])
        targets = [(i, m) for i, m in enumerate(matches) if m.get("id") == target_id]
        if len(targets) != 2:
            raise RecoveryStateError(
                f"Archived raw source does not contain exactly 2 target records ({len(targets)} found)"
            )
        idx_a, rec_a = targets[0]
        idx_b, rec_b = targets[1]
        candidate, _diff = synthesize_candidate([rec_a, rec_b])
        projected_matches = matches[:idx_a] + [candidate] + matches[idx_a + 1:idx_b] + matches[idx_b + 1:]

        projected_data: Dict[str, Any] = {"matches": projected_matches}
        projected_data["meta"] = copy.deepcopy(raw_data.get("meta", {}))
        for key, value in raw_data.items():
            if key not in ("matches", "meta"):
                projected_data[key] = copy.deepcopy(value)
        if isinstance(projected_data.get("meta"), dict):
            projected_data["meta"]["total"] = len(projected_matches)
        return projected_matches, projected_data

    def _assert_authorization_binding(self, authorization: dict, operator: Optional[str]) -> None:
        if canonicalize_path(authorization["data_dir"]) != self.data_dir:
            raise AuthorizationError(
                f"Authorization is bound to '{authorization['data_dir']}' but the target is '{self.data_dir}'"
            )
        if operator is not None and authorization["operator"] != operator:
            raise AuthorizationError(
                f"Operator mismatch: authorization was approved by '{authorization['operator']}', "
                f"invoked by '{operator}'"
            )
        bound_migration = authorization.get("migration_id")
        if bound_migration:
            if self._migration_id_explicit and self.migration_id != bound_migration:
                raise AuthorizationError(
                    f"Migration ID mismatch: authorization '{bound_migration}' vs '{self.migration_id}'"
                )
            if self.migration_id != bound_migration:
                self.migration_id = bound_migration
                if not self._workspace_explicit:
                    self.workspace = assert_workspace_outside_data_dir(
                        os.path.join(
                            os.path.dirname(self.data_dir), DEFAULT_WORKSPACE_DIRNAME, self.migration_id
                        ),
                        self.data_dir,
                    )
                    self._bind_paths()

    # -- read-only preview --------------------------------------------------

    def dry_run(self, authorization: Optional[dict] = None) -> dict:
        """Read-only preview. Never touches the data directory."""
        report = inspect_dataset(self.data_dir)
        groups = report["duplicate_groups"]
        if not groups:
            return {
                "status": "DRY_RUN_NO_COLLISION",
                "read_only_data_dir": True,
                "total_matches": report["total_matches"],
                "duplicate_groups": {},
            }

        if authorization is not None:
            self._assert_authorization_binding(authorization, None)
            target_id = authorization["target_canonical_id"]
            if set(groups.keys()) != {target_id}:
                raise PreconditionViolationError(
                    f"Authorization targets '{target_id}' but dataset has groups {sorted(groups.keys())}"
                )
        else:
            if len(groups) != 1:
                raise PreconditionViolationError(
                    f"Ambiguous collision set {sorted(groups.keys())}; pass --authorization to disambiguate"
                )
            target_id = next(iter(groups))

        daily_data = read_json_strict(self._daily_file())
        matches = daily_data.get("matches", [])
        records = [m for m in matches if m.get("id") == target_id]
        candidate, field_diff = synthesize_candidate(records)

        projected_matches = []
        seen_target = False
        for match in matches:
            if match.get("id") == target_id:
                if not seen_target:
                    projected_matches.append(candidate)
                    seen_target = True
                continue
            projected_matches.append(match)

        preview = {
            "status": "DRY_RUN_COMPLETED",
            "read_only_data_dir": True,
            "data_dir": self.data_dir,
            "target_canonical_id": target_id,
            "before_total": len(matches),
            "after_total": len(projected_matches),
            "candidate_record": candidate,
            "candidate_sha256": hash_record(candidate),
            "field_diff": field_diff,
            "authorized_candidate_sha256": (
                authorization.get("expected_candidate_sha256") if authorization else None
            ),
        }
        if authorization is not None:
            preview["authorization_matches_candidate"] = (
                hash_record(candidate) == authorization["expected_candidate_sha256"]
            )

        os.makedirs(self.output_dir, exist_ok=True)
        preview_path = os.path.join(self.output_dir, "production-projection-preview.json")
        write_json_atomic(preview_path, preview)
        preview["preview_file"] = preview_path
        return preview

    # -- forensic archive ---------------------------------------------------

    def verify_forensic_archive(self, baseline_source_hash: str, expected_migration_id: str) -> None:
        if not os.path.isdir(self.archive_dir):
            raise RecoveryStateError(f"Forensic archive directory missing: {self.archive_dir}")

        raw = self._raw_archive()
        if not os.path.exists(raw):
            raise RecoveryStateError("Forensic archive raw file missing: daily_matches_raw.json")
        if sha256_file(raw) != baseline_source_hash:
            raise RecoveryStateError("Forensic archive raw hash mismatch")

        manifest_path = os.path.join(self.archive_dir, "forensic_manifest.json")
        if not os.path.exists(manifest_path):
            raise RecoveryStateError("Forensic manifest missing")
        manifest = read_json_strict(manifest_path)
        if not isinstance(manifest, dict):
            raise RecoveryStateError("Forensic manifest must be a JSON object")
        if manifest.get("migration_id") != expected_migration_id:
            raise RecoveryStateError("Forensic manifest migration_id mismatch")
        if manifest.get("source_file_sha256") != baseline_source_hash:
            raise RecoveryStateError("Forensic manifest source_file_sha256 mismatch")

        for name in ("record_a", "record_b"):
            rec_path = os.path.join(self.archive_dir, f"{name}.json")
            if not os.path.exists(rec_path):
                raise RecoveryStateError(f"Forensic archive {name}.json missing")
            if hash_record(read_json_strict(rec_path)) != manifest.get(f"{name}_sha256"):
                raise RecoveryStateError(f"Forensic archive {name} hash mismatch")

    # -- apply --------------------------------------------------------------

    def apply(
        self,
        authorization: dict,
        confirm_apply: bool = False,
        operator: Optional[str] = None,
        fault_inject: Optional[str] = None,
    ) -> dict:
        """Execute the guarded production repair under the seven-phase machine."""
        if not confirm_apply:
            raise AuthorizationError("apply requires the explicit --confirm-apply flag")

        self._assert_authorization_binding(authorization, operator)
        target_id = authorization["target_canonical_id"]
        authorization_sha = authorization_fingerprint(authorization)
        operator_name = authorization["operator"]

        daily_file = self._daily_file()

        with acquire_repair_lock(self.data_dir):
            current_hash = sha256_file(daily_file)
            daily_data = read_json_strict(daily_file)
            matches = daily_data.get("matches", [])
            total = len(matches)

            counts = Counter(m.get("id") for m in matches)
            dup_groups = {k: v for k, v in counts.items() if v > 1}

            # --- Idempotency: dataset already repaired --------------------
            if not dup_groups:
                targets = [m for m in matches if m.get("id") == target_id]
                if len(targets) != 1:
                    raise PreconditionViolationError(
                        f"No duplicates, but target '{target_id}' appears {len(targets)} times"
                    )
                if hash_record(targets[0]) != authorization["expected_candidate_sha256"]:
                    raise PreconditionViolationError(
                        "No duplicates remain but the target record does not match the authorized candidate"
                    )
                self.verify_completed_dataset(authorization)
                return {
                    "status": "NO_OP_ALREADY_REPAIRED",
                    "target_canonical_id": target_id,
                    "total_matches": total,
                    "candidate_sha256": authorization["expected_candidate_sha256"],
                    "message": "Dataset already satisfies the authorized projection; nothing written.",
                }

            if set(dup_groups.keys()) != {target_id} or dup_groups.get(target_id) != 2:
                raise PreconditionViolationError(
                    f"Refusing to repair: unexpected duplicate groups {dup_groups}; "
                    f"expected exactly {{'{target_id}': 2}}"
                )

            if current_hash != authorization["expected_source_sha256"]:
                raise PreconditionViolationError(
                    "Source file hash does not match the authorization binding "
                    f"({current_hash} != {authorization['expected_source_sha256']}); "
                    "the dataset drifted after authorization was approved."
                )

            if fault_inject == "before_prepare":
                raise StagingMigrationError("Fault injection: before_prepare")

            # --- Phase 1: PREPARED ---------------------------------------
            pre_hashes = self.get_store_hashes()
            for store in ALL_STORES:
                if pre_hashes[store] == MISSING:
                    raise PreconditionViolationError(f"Required store missing: {store}")

            existing_entries = self.journal.read_entries()
            self.journal.validate_chain(existing_entries, expected_migration_id=self.migration_id)

            attempt = 1
            action = "INITIAL_PREPARED"
            if existing_entries:
                last_state = group_attempts(existing_entries)[-1][-1].get("state")
                if last_state in ("PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED", "COMPLETION_RECORDED"):
                    raise RecoveryStateError(
                        "Projection already committed for this migration; a new attempt is illegal"
                    )
                if last_state == "COMMIT_INTENT_DURABLE":
                    raise RecoveryStateError(
                        "Previous attempt reached COMMIT_INTENT_DURABLE; run 'recover' first"
                    )
                attempt = max(
                    (e.get("payload", {}).get("attempt", 1) for e in existing_entries if isinstance(e.get("payload"), dict)),
                    default=1,
                ) + 1
                action = "RETRY_PREPARED"

            self.journal.append_entry("PREPARED", {
                "migration_id": self.migration_id,
                "data_dir": self.data_dir,
                "authorization_sha256": authorization_sha,
                "operator": operator_name,
                "target_canonical_id": target_id,
                "pre_store_hashes": pre_hashes,
                "before_total": total,
                "attempt": attempt,
                "action": action,
            })

            if fault_inject == "before_archive":
                raise StagingMigrationError("Fault injection: before_archive")

            # --- Phase 2: EVIDENCE_VERIFIED ------------------------------
            targets = [(i, m) for i, m in enumerate(matches) if m.get("id") == target_id]
            idx_a, rec_a = targets[0]
            idx_b, rec_b = targets[1]
            rec_a_hash = hash_record(rec_a)
            rec_b_hash = hash_record(rec_b)

            if not any(
                m.get("status") == "finished" and valid_full_time_score(m.get("score")) for _, m in targets
            ):
                raise PreconditionViolationError(
                    "No terminal 'finished' record with a strictly valid full-time score among the duplicates"
                )

            self.journal.append_entry("EVIDENCE_VERIFIED", {
                "migration_id": self.migration_id,
                "attempt": attempt,
                "record_a_sha256": rec_a_hash,
                "record_b_sha256": rec_b_hash,
                "target_indices": [idx_a, idx_b],
            })

            if fault_inject == "during_archive":
                raise StagingMigrationError("Fault injection: during_archive")

            raw_archive = self._raw_archive()
            if os.path.exists(raw_archive):
                self.verify_forensic_archive(pre_hashes[CANONICAL_FILE], self.migration_id)
            else:
                os.makedirs(self.archive_dir, exist_ok=True)
                with open(daily_file, "rb") as src, open(raw_archive, "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
                write_json_atomic(os.path.join(self.archive_dir, "record_a.json"), rec_a)
                write_json_atomic(os.path.join(self.archive_dir, "record_b.json"), rec_b)
                write_json_atomic(os.path.join(self.archive_dir, "forensic_manifest.json"), {
                    "migration_id": self.migration_id,
                    "data_dir": self.data_dir,
                    "authorization_sha256": authorization_sha,
                    "source_file_sha256": sha256_file(raw_archive),
                    "record_a_sha256": rec_a_hash,
                    "record_b_sha256": rec_b_hash,
                    "record_a_index": idx_a,
                    "record_b_index": idx_b,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                })

            if fault_inject == "after_archive_before_verify":
                raise StagingMigrationError("Fault injection: after_archive_before_verify")

            if sha256_file(raw_archive) != pre_hashes[CANONICAL_FILE]:
                raise PreconditionViolationError("Forensic archive raw bytes hash mismatch!")

            # --- Phase 3: AUTHORIZED_FOR_PRODUCTION_REPAIR ---------------
            self.journal.append_entry("AUTHORIZED_FOR_PRODUCTION_REPAIR", {
                "migration_id": self.migration_id,
                "attempt": attempt,
                "operator": operator_name,
                "authorization_sha256": authorization_sha,
                "approved_at": authorization["approved_at"],
                "reason": authorization["reason"],
                "scope": REPAIR_SCOPE,
            })

            if fault_inject == "before_commit_intent":
                raise StagingMigrationError("Fault injection: before_commit_intent")

            # --- Phase 4: COMMIT_INTENT_DURABLE --------------------------
            candidate, _diff = synthesize_candidate([rec_a, rec_b])
            cand_hash = hash_record(candidate)
            if cand_hash != authorization["expected_candidate_sha256"]:
                raise PreconditionViolationError(
                    f"Synthesized candidate hash {cand_hash} does not match the authorized "
                    f"candidate {authorization['expected_candidate_sha256']}"
                )

            projected_matches, projected_data = self._derive_projected_from_raw(daily_data, target_id)
            projected_total = len(projected_matches)
            if projected_total != total - 1:
                raise PreconditionViolationError(
                    f"Projected cardinality error: {projected_total} != {total - 1}"
                )
            projected_file_hash = derive_file_hash(projected_data)

            for entry in existing_entries:
                if entry.get("state") == "COMMIT_INTENT_DURABLE":
                    prev = entry.get("payload", {})
                    if (
                        prev.get("source_file_sha256") != pre_hashes[CANONICAL_FILE]
                        or prev.get("projected_file_sha256") != projected_file_hash
                        or prev.get("candidate_record_sha256") != cand_hash
                    ):
                        raise PreconditionViolationError(
                            f"Reused migration ID '{self.migration_id}' with conflicting intent hashes"
                        )

            self.journal.append_entry("COMMIT_INTENT_DURABLE", {
                "migration_id": self.migration_id,
                "attempt": attempt,
                "data_dir": self.data_dir,
                "target_canonical_id": target_id,
                "source_file_sha256": pre_hashes[CANONICAL_FILE],
                "projected_file_sha256": projected_file_hash,
                "candidate_record_sha256": cand_hash,
                "record_a_sha256": rec_a_hash,
                "record_b_sha256": rec_b_hash,
                "immutable_store_hashes": {k: v for k, v in pre_hashes.items() if k != CANONICAL_FILE},
                "before_total": total,
                "projected_total": projected_total,
            })

            if fault_inject == "after_commit_intent":
                raise StagingMigrationError("Fault injection: after_commit_intent")
            if fault_inject == "before_file_replace":
                raise StagingMigrationError("Fault injection: before_file_replace")

            if sha256_file(daily_file) != pre_hashes[CANONICAL_FILE]:
                raise PreconditionViolationError(
                    "Stale source: daily_matches.json changed concurrently before commit"
                )

            # --- Phase 5: PROJECTION_COMMITTED ---------------------------
            write_json_atomic(daily_file, projected_data)

            if fault_inject == "after_replace_before_committed":
                raise StagingMigrationError("Fault injection: after_replace_before_committed")

            self.journal.append_entry("PROJECTION_COMMITTED", {
                "migration_id": self.migration_id,
                "attempt": attempt,
                "daily_matches_sha256": sha256_file(daily_file),
            })

            if fault_inject == "after_file_replace":
                raise StagingMigrationError("Fault injection: after_file_replace")

            if fault_inject == "corrupt_store_pre_postconditions":
                # Test-only hook: prove the immutability gate actually fires.
                with open(os.path.join(self.data_dir, "team_strength.json"), "a", encoding="utf-8") as fh:
                    fh.write("\n")

            if fault_inject == "before_postconditions":
                raise StagingMigrationError("Fault injection: before_postconditions")

            # --- Phase 6: POSTCONDITIONS_VERIFIED ------------------------
            self._verify_postconditions(
                pre_hashes=pre_hashes,
                projected_data=projected_data,
                projected_total=projected_total,
                projected_file_hash=projected_file_hash,
                target_id=target_id,
                cand_hash=cand_hash,
                daily_data=daily_data,
            )

            post_hashes = self.get_store_hashes()
            self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                "migration_id": self.migration_id,
                "attempt": attempt,
                "after_total": projected_total,
                "projected_file_sha256": post_hashes[CANONICAL_FILE],
            })

            if fault_inject == "before_completion":
                raise StagingMigrationError("Fault injection: before_completion")

            # --- Phase 7: COMPLETION_RECORDED ----------------------------
            result = {
                "status": "SUCCESS",
                "migration_id": self.migration_id,
                "attempt": attempt,
                "operator": operator_name,
                "authorization_sha256": authorization_sha,
                "target_canonical_id": target_id,
                "before_total": total,
                "after_total": projected_total,
                "unique_ids": projected_total,
                "duplicate_ids_count": 0,
                "candidate_sha256": cand_hash,
                "projected_file_sha256": post_hashes[CANONICAL_FILE],
                "immutable_stores_changed": 0,
            }
            self.journal.append_entry("COMPLETION_RECORDED", result)

            os.makedirs(self.output_dir, exist_ok=True)
            result_path = os.path.join(self.output_dir, "production-apply-result.json")
            write_json_atomic(result_path, result)
            result["result_file"] = result_path
            return result

    # -- verification -------------------------------------------------------

    def _verify_postconditions(
        self,
        pre_hashes: Dict[str, str],
        projected_data: dict,
        projected_total: int,
        projected_file_hash: str,
        target_id: str,
        cand_hash: str,
        daily_data: dict,
    ) -> None:
        report = inspect_dataset(self.data_dir)
        if report["total_matches"] != projected_total:
            raise PreconditionViolationError(
                f"Postcondition: total {report['total_matches']} != {projected_total}"
            )
        if report["unique_ids_count"] != projected_total:
            raise PreconditionViolationError(
                f"Postcondition: unique IDs {report['unique_ids_count']} != {projected_total}"
            )
        if report["duplicate_ids"]:
            raise PreconditionViolationError(f"Postcondition: duplicates remain {report['duplicate_ids']}")

        current = read_json_strict(self._daily_file())
        targets = [m for m in current.get("matches", []) if m.get("id") == target_id]
        if len(targets) != 1:
            raise PreconditionViolationError(
                f"Postcondition: target '{target_id}' occurs {len(targets)} times, expected 1"
            )
        if hash_record(targets[0]) != cand_hash:
            raise PreconditionViolationError("Postcondition: target record hash mismatch")

        # Independently re-derive the expected file bytes from the raw archive.
        raw_data = read_json_strict(self._raw_archive())
        _pm, derived = self._derive_projected_from_raw(raw_data, target_id)
        if derive_file_hash(derived) != projected_file_hash:
            raise PreconditionViolationError(
                "Postcondition: file hash does not match the independently derived projection"
            )
        if sha256_file(self._daily_file()) != projected_file_hash:
            raise PreconditionViolationError("Postcondition: active file hash mismatch")

        # Unrelated records preserved, in original relative order.
        raw_unrelated = [m for m in raw_data.get("matches", []) if m.get("id") != target_id]
        cur_unrelated = [m for m in current.get("matches", []) if m.get("id") != target_id]
        if serialize_record(raw_unrelated) != serialize_record(cur_unrelated):
            raise PreconditionViolationError("Postcondition: unrelated Canonical records altered or reordered")

        # Metadata preservation.
        old_meta = daily_data.get("meta") if isinstance(daily_data.get("meta"), dict) else {}
        new_meta = current.get("meta") if isinstance(current.get("meta"), dict) else {}
        if new_meta.get("total") != projected_total:
            raise PreconditionViolationError("Postcondition: meta.total not updated")
        for key in old_meta:
            if key == "total":
                continue
            if serialize_record(old_meta.get(key)) != serialize_record(new_meta.get(key)):
                raise PreconditionViolationError(f"Postcondition: meta.{key} was altered")
        for key in new_meta:
            if key not in old_meta:
                raise PreconditionViolationError(f"Postcondition: unexpected new meta key '{key}'")

        # Unknown top-level keys preserved exactly.
        for key, value in daily_data.items():
            if key in ("matches", "meta"):
                continue
            if serialize_record(value) != serialize_record(current.get(key)):
                raise PreconditionViolationError(f"Postcondition: top-level key '{key}' was altered")

        # Six non-Canonical stores byte-identical.
        post_hashes = report["store_hashes"]
        for store in NON_CANONICAL_STORES:
            if post_hashes[store] != pre_hashes[store]:
                raise ImmutableStoreMutationError(
                    f"CRITICAL: immutable store '{store}' mutated: "
                    f"{pre_hashes[store]} -> {post_hashes[store]}"
                )

    def verify_completed_dataset(self, authorization: dict) -> dict:
        target_id = authorization["target_canonical_id"]
        report = inspect_dataset(self.data_dir)
        if report["duplicate_ids"]:
            raise RecoveryStateError(f"Duplicate IDs present: {report['duplicate_ids']}")
        if report["unique_ids_count"] != report["total_matches"]:
            raise RecoveryStateError("Unique ID count does not equal total match count")

        current = read_json_strict(self._daily_file())
        targets = [m for m in current.get("matches", []) if m.get("id") == target_id]
        if len(targets) != 1:
            raise RecoveryStateError(f"Expected exactly 1 target record, found {len(targets)}")
        if hash_record(targets[0]) != authorization["expected_candidate_sha256"]:
            raise RecoveryStateError("Target record does not match the authorized candidate")
        if not valid_full_time_score(targets[0].get("score")):
            raise RecoveryStateError("Target record has an invalid full-time score")

        for store in NON_CANONICAL_STORES:
            path = os.path.join(self.data_dir, store)
            if os.path.exists(path) and report["store_hashes"][store] == MISSING:
                raise RecoveryStateError(f"Immutable store disappeared: {store}")

        entries = self.journal.read_entries()
        if entries:
            self.journal.validate_chain(entries)
            if self.journal.get_latest_valid_state() != "COMPLETION_RECORDED":
                raise RecoveryStateError("Journal latest state is not COMPLETION_RECORDED")
            completions = [e for e in entries if e.get("state") == "COMPLETION_RECORDED"]
            if len(completions) != 1:
                raise RecoveryStateError(f"Expected exactly 1 COMPLETION_RECORDED entry, got {len(completions)}")

        raw_archive = self._raw_archive()
        if os.path.exists(raw_archive):
            self.verify_forensic_archive(
                baseline_source_hash=sha256_file(raw_archive),
                expected_migration_id=self.migration_id,
            )
        return report

    def verify(self, authorization: Optional[dict] = None) -> dict:
        """Read-only postcondition audit."""
        try:
            report = inspect_dataset(self.data_dir)
            if report["duplicate_ids"]:
                return {
                    "verified": False,
                    "duplicate_ids": report["duplicate_ids"],
                    "error": "Duplicate Canonical IDs still present",
                    "error_type": "PreconditionViolationError",
                }
            if authorization is not None:
                self.verify_completed_dataset(authorization)
            return {
                "verified": True,
                "total_matches": report["total_matches"],
                "unique_ids_count": report["unique_ids_count"],
                "duplicate_ids": {},
                "dataset_report": report,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "verified": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    # -- crash recovery -----------------------------------------------------

    def recover(self) -> dict:
        """Classify durable state under one lock and resolve deterministically."""
        with acquire_repair_lock(self.data_dir):
            entries = self.journal.read_entries()
            if not entries:
                return {"status": "NO_JOURNAL_ENTRIES", "action": "NOTHING_TO_RECOVER"}

            self.journal.validate_chain(entries)
            latest = self.journal.get_latest_valid_state()

            daily_file = self._daily_file()
            current_hash = sha256_file(daily_file) if os.path.exists(daily_file) else MISSING

            prep = next(
                (e for e in entries
                 if e.get("state") == "PREPARED"
                 and (e.get("payload") or {}).get("action") != "ROLLED_BACK_TO_RAW_ARCHIVE"),
                None,
            )
            baseline_hash = None
            if prep:
                baseline_hash = (prep.get("payload") or {}).get("pre_store_hashes", {}).get(CANONICAL_FILE)
                pre_hashes = (prep.get("payload") or {}).get("pre_store_hashes", {})
                current_stores = self.get_store_hashes()
                for store in NON_CANONICAL_STORES:
                    if store in pre_hashes and current_stores.get(store) != pre_hashes[store]:
                        raise ImmutableStoreMutationError(
                            f"CRITICAL: immutable store '{store}' mutated prior to recovery!"
                        )

            intent = next((e for e in reversed(entries) if e.get("state") == "COMMIT_INTENT_DURABLE"), None)
            intended_hash = (intent.get("payload") or {}).get("projected_file_sha256") if intent else None
            migration_id = (
                (intent.get("payload") or {}).get("migration_id") if intent
                else ((prep.get("payload") or {}).get("migration_id") if prep else self.migration_id)
            )

            if baseline_hash and current_hash == baseline_hash:
                file_state = "EXPECTED_SOURCE"
            elif intended_hash and current_hash == intended_hash:
                file_state = "EXPECTED_PROJECTED"
            else:
                file_state = "UNKNOWN"

            if file_state == "UNKNOWN":
                raise RecoveryStateError(
                    f"Recovery failed closed: active Canonical hash '{current_hash}' is UNKNOWN. No write permitted."
                )

            if file_state == "EXPECTED_PROJECTED":
                self.verify_forensic_archive(baseline_hash, migration_id)

                if latest == "COMPLETION_RECORDED":
                    return {
                        "status": "ALREADY_COMPLETED_NO_OP",
                        "action": "ALREADY_COMPLETED",
                        "latest_state": latest,
                        "migration_id": migration_id,
                    }
                if latest == "POSTCONDITIONS_VERIFIED":
                    self.journal.append_entry("COMPLETION_RECORDED", {
                        "migration_id": migration_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "RECORDED_COMPLETION",
                        "after_total": len(read_json_strict(daily_file).get("matches", [])),
                    })
                    return {"status": "RECOVERED_FORWARD_COMPLETED", "action": "RECORDED_COMPLETION"}
                if latest == "PROJECTION_COMMITTED":
                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                        "migration_id": migration_id,
                        "note": "recovery verification",
                    })
                    self.journal.append_entry("COMPLETION_RECORDED", {
                        "migration_id": migration_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "VERIFIED_POSTCONDITIONS_AND_RECORDED_COMPLETION",
                    })
                    return {"status": "RECOVERED_FORWARD_COMPLETED", "action": "VERIFIED_AND_COMPLETED"}
                if latest == "COMMIT_INTENT_DURABLE":
                    self.journal.append_entry("PROJECTION_COMMITTED", {
                        "migration_id": migration_id,
                        "daily_matches_sha256": current_hash,
                    })
                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                        "migration_id": migration_id,
                        "note": "recovery verification",
                    })
                    self.journal.append_entry("COMPLETION_RECORDED", {
                        "migration_id": migration_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "COMMITTED_PROJECTION_AND_RECORDED_COMPLETION",
                    })
                    return {"status": "RECOVERED_FORWARD_COMPLETED", "action": "COMMITTED_AND_COMPLETED"}

                raise RecoveryStateError(f"Unexpected journal state '{latest}' with a projected file")

            # file_state == EXPECTED_SOURCE
            if latest in ("COMPLETION_RECORDED", "PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED"):
                raise RecoveryStateError(
                    "Contradictory state: journal claims a committed projection but the file is the baseline"
                )
            if latest == "COMMIT_INTENT_DURABLE":
                raw_archive = self._raw_archive()
                if not os.path.exists(raw_archive):
                    raise RecoveryStateError("Missing raw archive to roll back from COMMIT_INTENT_DURABLE")
                restore_from_archive_atomic(raw_archive, daily_file, expected_hash=baseline_hash)
                self.journal.append_entry("PREPARED", {
                    "migration_id": migration_id,
                    "action": "ROLLED_BACK_TO_RAW_ARCHIVE",
                    "attempt": (intent.get("payload") or {}).get("attempt", 1) if intent else 1,
                })
                return {"status": "RECOVERED_ROLLED_BACK", "action": "RESTORED_ARCHIVE_PRE_COMMIT"}

            return {
                "status": "RECOVERED_PRE_COMMIT_CLEAN",
                "action": "SAFE_RESET",
                "latest_state": latest,
            }


# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="production_canonical_repair",
        description="Auditable, owner-authorized production Canonical de-duplication.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_inspect = sub.add_parser("inspect", help="Read-only dataset inspection")
    p_inspect.add_argument("--data-dir", required=True)

    p_auth = sub.add_parser("authorize", help="Read-only: build an authorization artifact")
    p_auth.add_argument("--data-dir", required=True)
    p_auth.add_argument("--target-id", required=True, help="Canonical ID to de-duplicate")
    p_auth.add_argument("--operator", required=True, help="Approving operator identity")
    p_auth.add_argument("--reason", required=True, help="Justification recorded in the journal")
    p_auth.add_argument("--out", required=True, help="Where to write the authorization artifact")
    p_auth.add_argument("--migration-id", default=None)

    p_dry = sub.add_parser("dry-run", help="Read-only projection preview")
    p_dry.add_argument("--data-dir", required=True)
    p_dry.add_argument("--authorization", default=None)
    p_dry.add_argument("--workspace", default=None)

    p_apply = sub.add_parser("apply", help="Execute the guarded repair")
    p_apply.add_argument("--data-dir", required=True)
    p_apply.add_argument("--authorization", required=True)
    p_apply.add_argument("--workspace", default=None)
    p_apply.add_argument("--operator", default=None)
    p_apply.add_argument("--confirm-apply", action="store_true")
    p_apply.add_argument("--fault-inject", default=None, help=argparse.SUPPRESS)

    p_verify = sub.add_parser("verify", help="Read-only postcondition audit")
    p_verify.add_argument("--data-dir", required=True)
    p_verify.add_argument("--authorization", default=None)

    p_recover = sub.add_parser("recover", help="Resolve an interrupted repair")
    p_recover.add_argument("--data-dir", required=True)
    p_recover.add_argument("--workspace", required=True)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.subcommand == "inspect":
            print(json.dumps(inspect_dataset(args.data_dir), indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "authorize":
            artifact = build_authorization(
                data_dir=args.data_dir,
                target_canonical_id=args.target_id,
                operator=args.operator,
                reason=args.reason,
                migration_id=args.migration_id,
            )
            write_json_atomic(args.out, artifact)
            print(json.dumps({
                "status": "AUTHORIZATION_WRITTEN",
                "out": args.out,
                "migration_id": artifact["migration_id"],
                "target_canonical_id": artifact["target_canonical_id"],
                "expected_source_sha256": artifact["expected_source_sha256"],
                "expected_candidate_sha256": artifact["expected_candidate_sha256"],
                "authorization_sha256": authorization_fingerprint(artifact),
            }, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "dry-run":
            authorization = load_authorization(args.authorization) if args.authorization else None
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                writable=False,
            )
            print(json.dumps(repairer.dry_run(authorization), indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "apply":
            authorization = load_authorization(args.authorization)
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                migration_id=authorization.get("migration_id"),
            )
            result = repairer.apply(
                authorization=authorization,
                confirm_apply=args.confirm_apply,
                operator=args.operator,
                fault_inject=args.fault_inject,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "verify":
            authorization = load_authorization(args.authorization) if args.authorization else None
            repairer = ProductionCanonicalRepairer(data_dir=args.data_dir, writable=False)
            report = repairer.verify(authorization)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("verified") else 1

        if args.subcommand == "recover":
            repairer = ProductionCanonicalRepairer(data_dir=args.data_dir, workspace=args.workspace)
            print(json.dumps(repairer.recover(), indent=2, ensure_ascii=False))
            return 0

        return 0

    except AuthorizationError as exc:
        print(f"AUTHORIZATION ERROR: {exc}", file=sys.stderr)
        return 110
    except ProtectedPathViolationError as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 101
    except PreconditionViolationError as exc:
        print(f"PRECONDITION ERROR: {exc}", file=sys.stderr)
        return 102
    except StagingLockError as exc:
        print(f"LOCK ERROR: {exc}", file=sys.stderr)
        return 103
    except ImmutableStoreMutationError as exc:
        print(f"MUTATION ERROR: {exc}", file=sys.stderr)
        return 104
    except RecoveryStateError as exc:
        print(f"RECOVERY ERROR: {exc}", file=sys.stderr)
        return 106
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
