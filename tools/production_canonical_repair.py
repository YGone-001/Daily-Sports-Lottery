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
1.  No write happens without a *signed owner approval* whose ``data_dir``,
    ``expected_source_sha256``, ``expected_source_total``,
    ``target_canonical_id``, ``expected_candidate_sha256``, ``migration_id``,
    ``operator`` and ``purpose`` all bind to the exact live dataset.  Any drift
    fails closed before a single byte is written.
2.  Preparation and approval are separate steps.  ``prepare`` emits an
    *unsigned* artifact (``authorized: false``) that grants nothing; only an
    Ed25519 signature -- produced with an owner key held outside the repository
    and verified against a separately provisioned trusted public key -- can
    raise it to ``authorized: true``.  A SHA-256 fingerprint is integrity, not
    authentication, so it never authorizes anything.
3.  ``apply`` additionally requires ``--confirm-apply``, an explicit
    ``--operator`` matching the approver, and the trusted public key.
4.  Evidence-epoch archives are refused; the workspace must live outside the
    data directory.
5.  The very same ``.refresh.lock`` byte-range lock used by ``utils.scraper``
    is held for the whole transaction, so a concurrent refresh cannot interleave.
6.  Seven-phase journal, forensic archive, atomic ``os.replace`` and crash
    recovery with fail-closed UNKNOWN classification.  Recovery is itself a
    mutation, so it requires the same signed authorization and operator.
7.  Postconditions are re-derived from the archived raw bytes rather than
    trusted from the active file.
8.  Deterministic fault hooks exist for tests only and are unreachable from the
    production command surface.

READ-ONLY SEMANTICS
-------------------
``inspect`` reads the data directory and writes nothing at all.
``dry-run`` reads the data directory and writes one preview artifact to
``<workspace>/output/`` -- it is *not* globally zero-write and reports so.
``verify`` / ``verify-authorization`` are strictly read-only.
``prepare`` / ``approve`` write only to the operator-chosen output path.
``apply`` / ``recover`` write the journal, the forensic archive and, finally,
the Canonical file itself.

The cryptographic and serialization primitives (``hash_record``,
``serialize_record``, ``sha256_file``, ``valid_full_time_score``) are imported
from the staging tool so both entry points speak one identical evidence
language -- the same record must hash the same way everywhere.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import stat
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

#: Schema ``2.0.0`` separates *preparation* (an unsigned, reviewable artifact)
#: from *approval* (an Ed25519 detached signature produced by an owner-held
#: private key).  Schema ``1.0.0`` artifacts carried ``authorized: true`` with no
#: signature at all and are therefore permanently barred from authorizing a
#: production mutation.
AUTHORIZATION_SCHEMA_VERSION = "2.0.0"
LEGACY_AUTHORIZATION_SCHEMA_VERSIONS: List[str] = ["1.0.0"]
REPAIR_SCOPE = "PRODUCTION_CANONICAL_DEDUP"

#: The authorization purpose is signed, so an approval granted for one class of
#: action can never be replayed against another.
AUTHORIZATION_PURPOSE = "PRODUCTION_CANONICAL_DEDUP_APPLY"

#: Established signature mechanism (RFC 8032 Ed25519 via ``cryptography``).
#: No bespoke cryptography is implemented anywhere in this module.
APPROVAL_ALGORITHM = "ed25519"
APPROVAL_FIELD = "approval"
APPROVAL_PUBLIC_KEY_BYTES = 32
APPROVAL_SIGNATURE_BYTES = 64

AUTHORIZATION_REQUIRED_FIELDS: List[str] = [
    "migration_id",
    "data_dir",
    "target_canonical_id",
    "expected_source_sha256",
    "expected_source_total",
    "expected_candidate_sha256",
    "operator",
    "approved_at",
    "reason",
    "purpose",
]

#: Every field an owner approval must bind.  The signed payload covers the whole
#: artifact minus the ``approval`` block, so this list is documentation of the
#: required bindings rather than an allow-list of what is covered.
AUTHORIZATION_BOUND_FIELDS: List[str] = [
    "scope",
    "target_canonical_id",
    "expected_source_sha256",
    "expected_candidate_sha256",
    "expected_source_total",
    "data_dir",
    "migration_id",
    "operator",
    "purpose",
]

EVIDENCE_EPOCH_PREFIX = "_quiescent_evidence_backup"

DEFAULT_WORKSPACE_DIRNAME = "repair_runs"

MISSING = "MISSING"

#: Two deliberately distinct verification contracts.  A successful dataset
#: integrity inspection must never be reported as a successful repair
#: verification.
DATASET_INTEGRITY_INSPECTION = "DATASET_INTEGRITY_INSPECTION"
FULL_REPAIR_VERIFICATION = "FULL_REPAIR_POSTCONDITION_VERIFICATION"

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

#: Deterministic failure hooks exist so crash recovery can be exercised without
#: killing a process.  They are *test-only*: the production command surface
#: exposes no flag, no environment variable and no argument that can arm them,
#: and ``apply`` refuses any ``fault_inject`` value while this gate is closed.
#: Only ``fault_injection_enabled()`` -- a context manager tests import directly
#: -- can open it.
_FAULT_INJECTION_GATE: Dict[str, bool] = {"enabled": False}


@contextmanager
def fault_injection_enabled():
    """Test-only: arm the deterministic fault hooks for the enclosed block."""
    previous = _FAULT_INJECTION_GATE["enabled"]
    _FAULT_INJECTION_GATE["enabled"] = True
    try:
        yield
    finally:
        _FAULT_INJECTION_GATE["enabled"] = previous


def fault_injection_armed() -> bool:
    """Whether the test-only fault hooks are currently armed."""
    return bool(_FAULT_INJECTION_GATE["enabled"])


# =============================================================================
# Exceptions
# =============================================================================

class AuthorizationError(StagingMigrationError):
    """Raised when the owner authorization artifact is missing, malformed, or
    does not bind to the live dataset."""
    pass


class SignatureVerificationError(AuthorizationError):
    """Raised when an approval signature is absent, malformed, untrusted or does
    not verify against the canonical signed payload."""
    pass


class IncompleteVerificationError(StagingMigrationError):
    """Raised when the evidence required for *full repair postcondition*
    verification is missing or unverifiable.

    This is deliberately distinct from a plain failure: it signals that the
    question could not be answered, so callers must never report a successful
    basic inspection as a successful full repair verification.
    """
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

    @staticmethod
    def build_entry(state: str, payload: dict) -> dict:
        """Construct a journal entry without writing it.

        Lets recovery validate the complete chain -- including the terminal
        entry it is about to append -- before that entry becomes durable.
        """
        if state not in JOURNAL_STATES:
            raise StagingMigrationError(f"Unknown state: {state}")
        return {
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }

    def append_entry(self, state: str, payload: dict) -> None:
        entry = self.build_entry(state, payload)
        os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)
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
        return self.latest_valid_state(self.read_entries())

    @staticmethod
    def latest_valid_state(entries: List[dict]) -> Optional[str]:
        """Latest journalled state in a *given* entry list.

        Recovery needs to reason about the chain it is about to write, not the
        chain that is currently on disk.
        """
        for entry in reversed(entries):
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
        "verification": DATASET_INTEGRITY_INSPECTION,
        "read_only_data_dir": True,
        "writes_external_artifact": False,
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
# Authorization: preparation vs. approval (Ed25519 trust boundary)
# =============================================================================
#
# TRUST BOUNDARY
# --------------
# ``prepare`` is a read-only convenience.  It derives the exact expected source
# hash, source record count and candidate hash for one collision and packages
# them into an artifact whose ``authorized`` flag is ``false`` and whose
# ``approval`` block is ``null``.  That artifact grants nothing: possession of a
# preparation artifact is not permission to mutate production.
#
# ``approve`` is the owner decision.  It signs the artifact's canonical payload
# with an Ed25519 key the owner holds outside the repository.  Only a signature
# that verifies against a *separately provisioned* trusted public key can raise
# an artifact to ``authorized: true``.
#
# A SHA-256 fingerprint is an integrity check, not an authentication primitive.
# ``authorization_fingerprint`` therefore exists only to tie a journal entry to
# the exact artifact it recorded -- never as evidence of owner intent.

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _crypto_backend() -> Dict[str, Any]:
    """
    Import the established Ed25519 implementation, or fail closed.

    No bespoke cryptography is implemented in this module: Ed25519 comes from
    ``cryptography`` (PyCA).  If the backend is unavailable, every signature
    check raises, so an artifact can never be *silently* treated as trusted.
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(
            "Ed25519 verification backend ('cryptography') is unavailable; refusing to "
            "treat any authorization artifact as trusted."
        ) from exc
    return {
        "InvalidSignature": InvalidSignature,
        "serialization": serialization,
        "Ed25519PrivateKey": Ed25519PrivateKey,
        "Ed25519PublicKey": Ed25519PublicKey,
    }


def public_key_id(raw_public_key: bytes) -> str:
    """Stable identifier for a trusted public key: ``sha256:<hex>``."""
    return "sha256:" + sha256_bytes(raw_public_key)


def authorization_signing_payload(authorization: dict) -> bytes:
    """
    Deterministic canonical payload for the owner approval signature.

    The payload is the artifact *minus* its ``approval`` block, serialized with
    sorted keys, no insignificant whitespace and UTF-8 encoding.  A signature is
    therefore bound to every declared field -- repair scope, canonical target
    ID, source SHA-256, candidate SHA-256, expected source record count,
    canonical data directory, migration ID, approver identity, authorization
    purpose, approval timestamp and justification.
    """
    body = {k: v for k, v in authorization.items() if k != APPROVAL_FIELD}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def authorization_fingerprint(authorization: dict) -> str:
    """Integrity fingerprint of the whole artifact (journal binding only)."""
    return sha256_bytes(serialize_record(authorization).encode("utf-8"))


# --- owner key provisioning -------------------------------------------------

def _assert_private_key_path_is_external(path: str) -> str:
    """
    A production private signing key may never be generated into, or loaded
    from, the repository or the application workspace.
    """
    if not path or not str(path).strip():
        raise AuthorizationError("A private signing key path is required")
    norm = canonicalize_path(path)
    if is_subpath_or_equal(norm, REPO_ROOT):
        raise AuthorizationError(
            "Refusing to use a private signing key stored inside the repository "
            f"({REPO_ROOT}). Provision the owner key outside the repository and the "
            "application workspace, and share only the public half with this tool."
        )
    return norm


def generate_signing_keypair(
    private_key_path: str,
    public_key_path: str,
) -> Dict[str, str]:
    """
    Provision a fresh Ed25519 owner keypair **outside** the repository.

    Writes the PKCS#8 private key (owner-held, never committed) and the
    SubjectPublicKeyInfo public key (safe to distribute / pin as trusted).
    """
    backend = _crypto_backend()
    serialization = backend["serialization"]

    private_norm = _assert_private_key_path_is_external(private_key_path)
    if not public_key_path or not str(public_key_path).strip():
        raise AuthorizationError("A public key output path is required")

    private_key = backend["Ed25519PrivateKey"].generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    for target, blob in ((private_norm, private_pem), (canonicalize_path(public_key_path), public_pem)):
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp_key_", suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(tmp_path, target)
        except BaseException:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    raw_public = _raw_public_key_from_pem(public_pem)
    return {
        "private_key_path": private_norm,
        "public_key_path": canonicalize_path(public_key_path),
        "key_id": public_key_id(raw_public),
        "algorithm": APPROVAL_ALGORITHM,
    }


def _raw_public_key_from_pem(public_pem: bytes) -> bytes:
    backend = _crypto_backend()
    serialization = backend["serialization"]
    try:
        key = serialization.load_pem_public_key(public_pem)
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(f"Cannot parse trusted public key: {exc}") from exc
    if not isinstance(key, backend["Ed25519PublicKey"]):
        raise SignatureVerificationError(
            "Trusted public key is not an Ed25519 public key; refusing to trust it."
        )
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def load_trusted_public_key(path: str) -> Dict[str, Any]:
    """
    Load the separately provisioned trusted public key.

    Rejects an unavailable, unreadable, malformed or non-Ed25519 key: an
    unverifiable approval must never be treated as verified.
    """
    if not path or not str(path).strip():
        raise SignatureVerificationError(
            "A trusted public key is required to verify an owner approval; none was provided."
        )
    if not os.path.isfile(path):
        raise SignatureVerificationError(f"Trusted public key not found: {path}")
    try:
        with open(path, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        raise SignatureVerificationError(f"Cannot read trusted public key '{path}': {exc}") from exc

    raw = _raw_public_key_from_pem(pem)
    if len(raw) != APPROVAL_PUBLIC_KEY_BYTES:
        raise SignatureVerificationError(
            f"Trusted public key has unexpected length {len(raw)}; expected {APPROVAL_PUBLIC_KEY_BYTES}"
        )
    return {"path": canonicalize_path(path), "raw": raw, "key_id": public_key_id(raw)}


def load_owner_private_key(private_key_path: str):
    """Load the owner-held Ed25519 private key from outside the repository."""
    backend = _crypto_backend()
    norm = _assert_private_key_path_is_external(private_key_path)
    if not os.path.isfile(norm):
        raise AuthorizationError(f"Owner private key not found: {private_key_path}")
    try:
        with open(norm, "rb") as fh:
            pem = fh.read()
    except OSError as exc:
        raise AuthorizationError(f"Cannot read owner private key: {exc}") from exc
    try:
        key = backend["serialization"].load_pem_private_key(pem, password=None)
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(f"Cannot parse owner private key: {exc}") from exc
    if not isinstance(key, backend["Ed25519PrivateKey"]):
        raise SignatureVerificationError("Owner private key is not an Ed25519 private key")
    return key


# --- approval ---------------------------------------------------------------

def sign_authorization(
    prepared: dict,
    private_key_path: str,
    *,
    operator: Optional[str] = None,
    signed_at: Optional[str] = None,
) -> dict:
    """
    Owner approval: turn an unsigned preparation artifact into a signed one.

    The private key is supplied by the owner from outside the repository; this
    function never generates or persists a production signing key.
    """
    if not isinstance(prepared, dict):
        raise AuthorizationError("Preparation artifact must be a JSON object")
    if prepared.get("authorization_schema_version") != AUTHORIZATION_SCHEMA_VERSION:
        raise AuthorizationError(
            f"Preparation artifact schema must be '{AUTHORIZATION_SCHEMA_VERSION}', got "
            f"'{prepared.get('authorization_schema_version')}'"
        )
    if prepared.get(APPROVAL_FIELD) is not None:
        raise AuthorizationError("Preparation artifact is already approved; re-signing is refused")

    artifact = {k: v for k, v in prepared.items() if k != APPROVAL_FIELD}
    artifact["authorized"] = True

    if operator is not None and artifact.get("operator") != operator:
        raise AuthorizationError(
            f"Approving operator '{operator}' does not match the prepared operator "
            f"'{artifact.get('operator')}'"
        )
    for field in AUTHORIZATION_REQUIRED_FIELDS:
        value = artifact.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AuthorizationError(f"Preparation artifact missing required field '{field}'")

    backend = _crypto_backend()
    serialization = backend["serialization"]
    private_key = load_owner_private_key(private_key_path)

    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = authorization_signing_payload(artifact)
    signature = private_key.sign(payload)

    artifact[APPROVAL_FIELD] = {
        "algorithm": APPROVAL_ALGORITHM,
        "key_id": public_key_id(public_raw),
        "public_key": base64.b64encode(public_raw).decode("ascii"),
        "payload_sha256": sha256_bytes(payload),
        "signature": base64.b64encode(signature).decode("ascii"),
        "signed_at": signed_at or datetime.now(timezone.utc).isoformat(),
    }
    return artifact


def verify_authorization_signature(
    authorization: dict,
    trusted_public_key: Optional[str],
) -> Dict[str, Any]:
    """
    Verify the owner approval against a separately provisioned trusted key.

    Rejects missing, malformed, tampered and unverifiable signatures, and
    rejects unavailable, unparseable or mismatched verification keys.
    """
    if not isinstance(authorization, dict):
        raise SignatureVerificationError("Authorization artifact must be a JSON object")

    approval = authorization.get(APPROVAL_FIELD)
    if approval is None:
        raise SignatureVerificationError(
            "Authorization artifact carries no owner approval; an unsigned preparation "
            "artifact can never authorize a production mutation."
        )
    if not isinstance(approval, dict):
        raise SignatureVerificationError("Authorization 'approval' block must be a JSON object")
    if approval.get("algorithm") != APPROVAL_ALGORITHM:
        raise SignatureVerificationError(
            f"Unsupported approval algorithm '{approval.get('algorithm')}'; expected "
            f"'{APPROVAL_ALGORITHM}'"
        )

    # Resolve the trusted key *first*: an untrusted key must reject the artifact
    # before any signature math runs.
    trusted = load_trusted_public_key(trusted_public_key) if trusted_public_key else None
    if trusted is None:
        raise SignatureVerificationError(
            "A trusted public key is required to verify an owner approval; none was provided."
        )

    try:
        embedded_raw = base64.b64decode(str(approval.get("public_key")), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(f"Approval 'public_key' is not valid base64: {exc}") from exc
    if len(embedded_raw) != APPROVAL_PUBLIC_KEY_BYTES:
        raise SignatureVerificationError(
            f"Approval public key has unexpected length {len(embedded_raw)}"
        )

    embedded_key_id = public_key_id(embedded_raw)
    if approval.get("key_id") != embedded_key_id:
        raise SignatureVerificationError(
            "Approval 'key_id' does not match the embedded public key (tampered artifact)"
        )
    if trusted["key_id"] != embedded_key_id:
        raise SignatureVerificationError(
            f"Approval was signed by untrusted key '{embedded_key_id}'; the configured trusted "
            f"key is '{trusted['key_id']}'"
        )

    payload = authorization_signing_payload(authorization)
    payload_sha = sha256_bytes(payload)
    if approval.get("payload_sha256") != payload_sha:
        raise SignatureVerificationError(
            "Signed payload digest mismatch: the authorization was modified after approval"
        )

    try:
        signature = base64.b64decode(str(approval.get("signature")), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(f"Approval 'signature' is not valid base64: {exc}") from exc
    if len(signature) != APPROVAL_SIGNATURE_BYTES:
        raise SignatureVerificationError(
            f"Approval signature has unexpected length {len(signature)}"
        )

    backend = _crypto_backend()
    serialization = backend["serialization"]
    try:
        public_key = backend["Ed25519PublicKey"].from_public_bytes(trusted["raw"])
        public_key.verify(signature, payload)
    except backend["InvalidSignature"] as exc:
        raise SignatureVerificationError(
            "Owner approval signature does not verify against the canonical payload"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(f"Owner approval signature is unverifiable: {exc}") from exc

    return {
        "verified": True,
        "algorithm": APPROVAL_ALGORITHM,
        "key_id": trusted["key_id"],
        "trusted_public_key": trusted["path"],
        "payload_sha256": payload_sha,
        "signed_at": approval.get("signed_at"),
    }


# --- loading ----------------------------------------------------------------

def load_authorization(
    path: str,
    trusted_public_key: Optional[str] = None,
    require_signature: bool = True,
) -> dict:
    """
    Load and validate an authorization artifact.

    ``require_signature=True`` (the default) is the mutation path: the artifact
    must be schema ``2.0.0`` and its approval must verify against the trusted
    public key.  ``require_signature=False`` is the read-only preview path: an
    unsigned preparation artifact is accepted for inspection, but an artifact
    *claiming* ``authorized: true`` is still rejected unless its signature
    verifies.
    """
    if not path or not os.path.exists(path):
        raise AuthorizationError(f"Authorization artifact not found: {path}")
    data = read_json_strict(path)
    if not isinstance(data, dict):
        raise AuthorizationError("Authorization artifact must be a JSON object")

    schema = data.get("authorization_schema_version")
    if schema in LEGACY_AUTHORIZATION_SCHEMA_VERSIONS:
        raise AuthorizationError(
            f"Authorization schema '{schema}' predates signed owner approvals and can never "
            f"authorize a production mutation; re-prepare and re-approve under schema "
            f"'{AUTHORIZATION_SCHEMA_VERSION}'."
        )
    if schema != AUTHORIZATION_SCHEMA_VERSION:
        raise AuthorizationError(f"Unsupported authorization schema: '{schema}'")
    if data.get("scope") != REPAIR_SCOPE:
        raise AuthorizationError(f"Unexpected authorization scope: '{data.get('scope')}'")
    if data.get("purpose") != AUTHORIZATION_PURPOSE:
        raise AuthorizationError(f"Unexpected authorization purpose: '{data.get('purpose')}'")

    for field in AUTHORIZATION_REQUIRED_FIELDS:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise AuthorizationError(f"Authorization artifact missing required field '{field}'")

    total = data.get("expected_source_total")
    if not isinstance(total, int) or isinstance(total, bool) or total < 1:
        raise AuthorizationError(
            "Authorization artifact 'expected_source_total' must be a positive integer"
        )

    if require_signature:
        verify_authorization_signature(data, trusted_public_key)
        if data.get("authorized") is not True:
            raise SignatureVerificationError(
                "Authorization artifact is not marked 'authorized': true; a verified approval "
                "must set that flag explicitly."
            )
    elif data.get("authorized") is True:
        # An artifact asserting authorization without a verified signature is
        # precisely the forgery this schema exists to reject.
        verify_authorization_signature(data, trusted_public_key)

    return data


def build_preparation(
    data_dir: str,
    target_canonical_id: str,
    operator: str,
    reason: str,
    migration_id: Optional[str] = None,
) -> dict:
    """
    Read-only: derive the exact expected source hash, source record count and
    candidate hash for one collision and package them into an **unsigned**
    preparation artifact for owner review.

    The result carries ``authorized: false`` and ``approval: null``: it cannot
    authorize a production mutation on its own.
    """
    if not operator or not str(operator).strip():
        raise AuthorizationError("--operator is required to prepare an authorization artifact")
    if not reason or not str(reason).strip():
        raise AuthorizationError("--reason is required to prepare an authorization artifact")

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
        "purpose": AUTHORIZATION_PURPOSE,
        "authorized": False,
        APPROVAL_FIELD: None,
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
        trusted_public_key: Optional[str] = None,
    ):
        self.data_dir = assert_production_data_dir(data_dir)
        self._migration_id_explicit = migration_id is not None
        self._workspace_explicit = bool(workspace)
        self._writable = writable
        #: Path to the separately provisioned trusted public key.  Only
        #: mutation paths need it; inspection and preview do not.
        self.trusted_public_key = trusted_public_key
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

    def _assert_authorization_binding(
        self,
        authorization: dict,
        operator: Optional[str],
        trusted_public_key: Optional[str] = None,
        require_signature: bool = True,
        require_operator: bool = True,
    ) -> Dict[str, Any]:
        """
        Enforce the full authorization binding *before* any mutation.

        Order matters: the signature is checked first, because nothing else in
        the artifact is meaningful until the owner's approval has been proven.
        """
        if not isinstance(authorization, dict):
            raise AuthorizationError("Authorization artifact must be a JSON object")

        signature = {"verified": False}
        if require_signature:
            signature = verify_authorization_signature(
                authorization, trusted_public_key if trusted_public_key else self.trusted_public_key
            )
            if authorization.get("authorized") is not True:
                raise SignatureVerificationError(
                    "Authorization artifact is not marked 'authorized': true"
                )

        for field in AUTHORIZATION_REQUIRED_FIELDS:
            value = authorization.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise AuthorizationError(f"Authorization artifact missing required field '{field}'")

        if authorization.get("purpose") != AUTHORIZATION_PURPOSE:
            raise AuthorizationError(
                f"Unexpected authorization purpose: '{authorization.get('purpose')}'"
            )

        # --- operator identity -------------------------------------------
        if require_operator:
            if operator is None or not str(operator).strip():
                raise AuthorizationError(
                    "An explicit --operator is required for any authorized production mutation"
                )
        if operator is not None and str(operator).strip():
            if authorization["operator"] != operator:
                raise AuthorizationError(
                    f"Operator mismatch: authorization was approved by "
                    f"'{authorization['operator']}', invoked by '{operator}'"
                )

        # --- canonical data directory ------------------------------------
        if canonicalize_path(authorization["data_dir"]) != self.data_dir:
            raise AuthorizationError(
                f"Authorization is bound to '{authorization['data_dir']}' but the target is '{self.data_dir}'"
            )

        # --- expected source record count --------------------------------
        total = authorization.get("expected_source_total")
        if not isinstance(total, int) or isinstance(total, bool) or total < 1:
            raise AuthorizationError(
                "Authorization 'expected_source_total' must be a positive integer"
            )

        # --- migration ID -------------------------------------------------
        bound_migration = authorization.get("migration_id")
        if not bound_migration or not str(bound_migration).strip():
            raise AuthorizationError("Authorization artifact is missing a non-empty migration_id")
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

        return signature

    # -- read-only preview --------------------------------------------------

    def dry_run(self, authorization: Optional[dict] = None) -> dict:
        """
        Read-only preview of the projection.

        The live data directory is never touched.  A preview artifact *is*
        written to ``<workspace>/output/``, so this command is not globally
        zero-write and says so in its result.
        """
        report = inspect_dataset(self.data_dir)
        groups = report["duplicate_groups"]
        if not groups:
            return {
                "status": "DRY_RUN_NO_COLLISION",
                "read_only_data_dir": True,
                "writes_external_artifact": False,
                "total_matches": report["total_matches"],
                "duplicate_groups": {},
                "canonical_source_sha256": report["store_hashes"][CANONICAL_FILE],
                "immutable_store_hashes": {
                    k: v for k, v in report["store_hashes"].items() if k != CANONICAL_FILE
                },
            }

        if authorization is not None:
            self._assert_authorization_binding(
                authorization, None, require_signature=False, require_operator=False
            )
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

        # Independently derived projected file fingerprint and the immutable-store
        # baseline, so a reviewer can audit the projection without running apply.
        try:
            _pm, projected_data = self._derive_projected_from_raw(daily_data, target_id)
            projected_file_sha256 = derive_file_hash(projected_data)
        except RecoveryStateError:
            # Malformed collision shapes are refused by the apply guards; the
            # preview simply cannot offer a projected fingerprint for them.
            projected_file_sha256 = None

        preview = {
            "status": "DRY_RUN_COMPLETED",
            "read_only_data_dir": True,
            "writes_external_artifact": True,
            "data_dir": self.data_dir,
            "target_canonical_id": target_id,
            "before_total": len(matches),
            "after_total": len(projected_matches),
            "candidate_record": candidate,
            "candidate_sha256": hash_record(candidate),
            "field_diff": field_diff,
            "canonical_source_sha256": report["store_hashes"][CANONICAL_FILE],
            "projected_file_sha256": projected_file_sha256,
            "immutable_store_hashes": {
                k: v for k, v in report["store_hashes"].items() if k != CANONICAL_FILE
            },
            "authorization_signature_verified": False,
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
        trusted_public_key: Optional[str] = None,
    ) -> dict:
        """
        Execute the guarded production repair under the seven-phase machine.

        ``fault_inject`` exists solely so the test suite can exercise crash
        recovery.  It is inert unless :func:`fault_injection_enabled` is active,
        and the production CLI exposes no way to reach it.
        """
        if not confirm_apply:
            raise AuthorizationError("apply requires the explicit --confirm-apply flag")

        if fault_inject is not None and not fault_injection_armed():
            raise StagingMigrationError(
                "Fault injection is a test-only facility and is not available in this process"
            )

        # Everything that can refuse happens here, before the lock is taken and
        # long before a single byte of Canonical data is replaced.
        signature = self._assert_authorization_binding(
            authorization, operator, trusted_public_key=trusted_public_key
        )
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
                self.verify_completed_dataset(
                    authorization, trusted_public_key=trusted_public_key
                )
                return {
                    "status": "NO_OP_ALREADY_REPAIRED",
                    "target_canonical_id": target_id,
                    "total_matches": total,
                    "candidate_sha256": authorization["expected_candidate_sha256"],
                    "authorization_key_id": signature.get("key_id"),
                    "message": "Dataset already satisfies the authorized projection; nothing written.",
                }

            # --- Mutation path: the source must match the authorized count --
            if total != authorization["expected_source_total"]:
                raise PreconditionViolationError(
                    f"Source record count {total} does not match the authorized "
                    f"{authorization['expected_source_total']}"
                )

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
                "authorization_key_id": signature.get("key_id"),
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

    def verify_completed_dataset(
        self,
        authorization: dict,
        trusted_public_key: Optional[str] = None,
        *,
        journal_entries: Optional[List[dict]] = None,
        require_journal: bool = True,
        require_archive: bool = True,
    ) -> dict:
        """
        FULL REPAIR POSTCONDITION VERIFICATION.

        Strictly stronger than ``inspect_dataset``, which only answers "is this
        dataset structurally sound?".  Every element below must be *evidenced*.
        When evidence is missing or unverifiable this raises
        :class:`IncompleteVerificationError` -- it never downgrades to a pass.
        """
        evidence: Dict[str, Any] = {}
        missing: List[str] = []

        # 1. Verified authorization binding (signature + directory + ids).
        signature = self._assert_authorization_binding(
            authorization,
            None,
            trusted_public_key=trusted_public_key,
            require_operator=False,
        )
        evidence["authorization_binding"] = "VERIFIED"
        evidence["authorization_key_id"] = signature.get("key_id")
        target_id = authorization["target_canonical_id"]

        # 2. Dataset integrity -- necessary, but far from sufficient.
        report = inspect_dataset(self.data_dir)
        if report["duplicate_ids"]:
            raise RecoveryStateError(f"Duplicate IDs present: {report['duplicate_ids']}")
        if report["unique_ids_count"] != report["total_matches"]:
            raise RecoveryStateError("Unique ID count does not equal total match count")
        evidence["dataset_integrity"] = "OK"

        current = read_json_strict(self._daily_file())
        targets = [m for m in current.get("matches", []) if m.get("id") == target_id]
        if len(targets) != 1:
            raise RecoveryStateError(f"Expected exactly 1 target record, found {len(targets)}")
        if hash_record(targets[0]) != authorization["expected_candidate_sha256"]:
            raise RecoveryStateError("Target record does not match the authorized candidate")
        if not valid_full_time_score(targets[0].get("score")):
            raise RecoveryStateError("Target record has an invalid full-time score")
        evidence["candidate_record"] = "MATCHES_AUTHORIZED_HASH"

        # 3. N -> N-1 cardinality against the authorized source count.
        if report["total_matches"] != authorization["expected_source_total"] - 1:
            raise RecoveryStateError(
                f"Cardinality {report['total_matches']} != authorized N-1 "
                f"({authorization['expected_source_total'] - 1})"
            )
        evidence["cardinality"] = f"N-1={report['total_matches']}"

        # 4. Archived source fingerprint (independent of the active file).
        raw_archive = self._raw_archive()
        if not os.path.exists(raw_archive):
            missing.append("forensic_archive_raw")
        else:
            baseline_hash = sha256_file(raw_archive)
            if baseline_hash != authorization["expected_source_sha256"]:
                raise RecoveryStateError(
                    "Archived source fingerprint does not match the authorized source hash"
                )
            evidence["archived_source_sha256"] = baseline_hash

        if not missing:
            raw_data = read_json_strict(raw_archive)
            raw_targets = [
                m for m in raw_data.get("matches", []) if m.get("id") == target_id
            ]
            if len(raw_targets) != 2:
                raise RecoveryStateError(
                    f"Archived source holds {len(raw_targets)} target records, expected exactly 2"
                )

            # 5. Candidate record fingerprint, re-derived from archived bytes.
            candidate, _diff = synthesize_candidate(raw_targets)
            if hash_record(candidate) != authorization["expected_candidate_sha256"]:
                raise RecoveryStateError(
                    "Candidate re-derived from the archive does not match the authorized candidate"
                )
            evidence["candidate_recomputed"] = "MATCHES_AUTHORIZED_HASH"

            # 6. Independently derived projected-file fingerprint.
            _pm, derived = self._derive_projected_from_raw(raw_data, target_id)
            derived_hash = derive_file_hash(derived)
            if sha256_file(self._daily_file()) != derived_hash:
                raise RecoveryStateError(
                    "Active file hash does not match the projection independently derived "
                    "from the archived raw source"
                )
            evidence["projected_file_sha256"] = derived_hash
            evidence["projected_hash_independently_derived"] = True

            # 7. Unrelated record preservation.
            raw_unrelated = [m for m in raw_data.get("matches", []) if m.get("id") != target_id]
            cur_unrelated = [m for m in current.get("matches", []) if m.get("id") != target_id]
            if serialize_record(raw_unrelated) != serialize_record(cur_unrelated):
                raise RecoveryStateError("Unrelated Canonical records altered or reordered")
            evidence["unrelated_records_preserved"] = len(cur_unrelated)

            # 8. Six immutable-store hashes versus the archived pre-state.
            entries = journal_entries if journal_entries is not None else self.journal.read_entries()
            prep = next(
                (e for e in entries
                 if e.get("state") == "PREPARED"
                 and (e.get("payload") or {}).get("action") != "ROLLED_BACK_TO_RAW_ARCHIVE"),
                None,
            )
            pre_hashes = (prep.get("payload") or {}).get("pre_store_hashes") if prep else None
            if not pre_hashes:
                missing.append("pre_store_hashes")
            else:
                current_stores = self.get_store_hashes()
                for store in NON_CANONICAL_STORES:
                    if current_stores.get(store) != pre_hashes.get(store):
                        raise ImmutableStoreMutationError(
                            f"CRITICAL: immutable store '{store}' mutated: "
                            f"{pre_hashes.get(store)} -> {current_stores.get(store)}"
                        )
                evidence["immutable_store_hashes"] = "ALL_SIX_UNCHANGED"

            # 9. Forensic manifest, evidence records and migration identity.
            if require_archive:
                self.verify_forensic_archive(
                    baseline_source_hash=evidence["archived_source_sha256"],
                    expected_migration_id=self.migration_id,
                )
                evidence["forensic_manifest"] = "VERIFIED"

        # 10. Journal progression and terminal completion state.
        if require_journal:
            entries = journal_entries if journal_entries is not None else self.journal.read_entries()
            if not entries:
                missing.append("journal_entries")
            else:
                self.journal.validate_chain(entries, expected_migration_id=self.migration_id)
                latest = RepairJournal.latest_valid_state(entries)
                if latest != "COMPLETION_RECORDED":
                    raise RecoveryStateError(
                        f"Journal latest state is '{latest}', not COMPLETION_RECORDED"
                    )
                completions = [e for e in entries if e.get("state") == "COMPLETION_RECORDED"]
                if len(completions) != 1:
                    raise RecoveryStateError(
                        f"Expected exactly 1 COMPLETION_RECORDED entry, got {len(completions)}"
                    )
                expected_fingerprint = authorization_fingerprint(authorization)
                for entry in entries:
                    recorded = (entry.get("payload") or {}).get("authorization_sha256")
                    if recorded and recorded != expected_fingerprint:
                        raise RecoveryStateError(
                            "Journal records a different authorization fingerprint for this migration"
                        )
                evidence["journal_chain"] = "VALID_TERMINAL_COMPLETION"

        if missing:
            raise IncompleteVerificationError(
                "Full repair verification is INCOMPLETE; missing evidence: "
                + ", ".join(sorted(set(missing)))
            )

        return {
            "verification": FULL_REPAIR_VERIFICATION,
            "verified": True,
            "complete": True,
            "evidence": evidence,
            "dataset_report": report,
        }

    def inspect_only(self) -> dict:
        """
        DATASET INTEGRITY INSPECTION -- explicitly *not* repair verification.

        The result is labelled so it can never be mistaken for a completed
        repair audit, and it carries no claim about a repair at all.
        """
        report = inspect_dataset(self.data_dir)
        return {
            "verification": DATASET_INTEGRITY_INSPECTION,
            "verified": False,
            "complete": False,
            "repair_verified": False,
            "note": (
                "Dataset integrity inspection only. This is NOT full repair "
                "postcondition verification; run 'verify' with a signed "
                "authorization to audit a repair."
            ),
            "dataset_report": report,
        }

    def verify(
        self,
        authorization: Optional[dict] = None,
        trusted_public_key: Optional[str] = None,
        operator: Optional[str] = None,
    ) -> dict:
        """
        Read-only audit.

        Without an authorization artifact this can only report dataset
        integrity, and it says so -- a clean dataset is never reported as a
        successful repair verification.
        """
        try:
            if authorization is None:
                return self.inspect_only()

            if operator is not None and authorization.get("operator") != operator:
                raise AuthorizationError(
                    f"Operator mismatch: authorization was approved by "
                    f"'{authorization.get('operator')}', invoked by '{operator}'"
                )

            return self.verify_completed_dataset(
                authorization, trusted_public_key=trusted_public_key
            )
        except IncompleteVerificationError as exc:
            return {
                "verification": FULL_REPAIR_VERIFICATION,
                "verified": False,
                "complete": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "verification": FULL_REPAIR_VERIFICATION,
                "verified": False,
                "complete": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }

    # -- crash recovery -----------------------------------------------------

    def _assert_recovery_authorized(
        self,
        entries: List[dict],
        authorization: Optional[dict],
        operator: Optional[str],
        trusted_public_key: Optional[str],
    ) -> None:
        """
        Tie recovery to the authenticated transaction that produced the journal.

        Recovery can rewrite the Canonical file, so it is a mutation and needs
        the same owner approval, operator identity and migration identity as the
        transaction it is finishing.
        """
        if authorization is None:
            raise AuthorizationError(
                "recover requires the signed authorization artifact that authorized this "
                "transaction; recovery is a mutation and cannot run unauthenticated"
            )
        self._assert_authorization_binding(
            authorization, operator, trusted_public_key=trusted_public_key
        )

        recorded_migrations = {
            (e.get("payload") or {}).get("migration_id") for e in entries
        }
        if authorization["migration_id"] not in recorded_migrations:
            raise RecoveryStateError(
                f"Journal does not belong to the authorized migration "
                f"'{authorization['migration_id']}'"
            )

        expected_fingerprint = authorization_fingerprint(authorization)
        for entry in entries:
            recorded = (entry.get("payload") or {}).get("authorization_sha256")
            if recorded and recorded != expected_fingerprint:
                raise RecoveryStateError(
                    "Journal was produced under a different authorization artifact; "
                    "refusing to recover unrelated or forged journal state"
                )

    def _derive_recovery_fingerprints(
        self,
        baseline_hash: Optional[str],
        target_id: Optional[str],
        journal_claimed_projected: Optional[str],
    ) -> Dict[str, Optional[str]]:
        """
        Independently derive the expected pre- and post-repair file fingerprints
        from the forensic archive instead of trusting the journal's own claims.
        """
        raw_archive = self._raw_archive()
        if not os.path.exists(raw_archive):
            return {"source": None, "projected": None}

        if not baseline_hash or not target_id:
            return {"source": None, "projected": None}

        if sha256_file(raw_archive) != baseline_hash:
            raise RecoveryStateError(
                "Forensic archive raw bytes do not match the journal's recorded pre-state "
                "fingerprint; refusing to recover from unverifiable evidence"
            )

        raw_data = read_json_strict(raw_archive)
        raw_targets = [m for m in raw_data.get("matches", []) if m.get("id") == target_id]
        if len(raw_targets) != 2:
            raise RecoveryStateError(
                f"Forensic archive holds {len(raw_targets)} target records, expected exactly 2"
            )

        _pm, derived = self._derive_projected_from_raw(raw_data, target_id)
        derived_projected = derive_file_hash(derived)

        if journal_claimed_projected and journal_claimed_projected != derived_projected:
            raise RecoveryStateError(
                "Journal's projected fingerprint disagrees with the projection independently "
                "derived from the forensic archive (forged or stale journal state)"
            )

        return {"source": baseline_hash, "projected": derived_projected}

    def recover(
        self,
        authorization: Optional[dict] = None,
        operator: Optional[str] = None,
        trusted_public_key: Optional[str] = None,
    ) -> dict:
        """
        Classify durable state under one lock and resolve deterministically.

        Ordering is deliberate: immutable-store drift is detected first, the
        transaction is authenticated second, and the expected fingerprints are
        independently re-derived from the forensic archive before any mutation.
        A completion entry is only ever appended after the full postcondition
        contract has been proven against the journal state it would create.
        """
        with acquire_repair_lock(self.data_dir):
            entries = self.journal.read_entries()
            if not entries:
                return {"status": "NO_JOURNAL_ENTRIES", "action": "NOTHING_TO_RECOVER"}

            prep = next(
                (e for e in entries
                 if e.get("state") == "PREPARED"
                 and (e.get("payload") or {}).get("action") != "ROLLED_BACK_TO_RAW_ARCHIVE"),
                None,
            )

            # (1) Immutable-store drift voids every downstream conclusion.
            if prep:
                pre_hashes = (prep.get("payload") or {}).get("pre_store_hashes", {})
                current_stores = self.get_store_hashes()
                for store in NON_CANONICAL_STORES:
                    if store in pre_hashes and current_stores.get(store) != pre_hashes[store]:
                        raise ImmutableStoreMutationError(
                            f"CRITICAL: immutable store '{store}' mutated prior to recovery!"
                        )

            # (2) Authentication: only the authorized transaction may recover.
            self._assert_recovery_authorized(entries, authorization, operator, trusted_public_key)

            self.journal.validate_chain(entries, expected_migration_id=self.migration_id)
            latest = self.journal.get_latest_valid_state()

            daily_file = self._daily_file()
            current_hash = sha256_file(daily_file) if os.path.exists(daily_file) else MISSING

            baseline_hash = None
            target_id = None
            if prep:
                payload = prep.get("payload") or {}
                baseline_hash = (payload.get("pre_store_hashes") or {}).get(CANONICAL_FILE)
                target_id = payload.get("target_canonical_id")

            intent = next(
                (e for e in reversed(entries) if e.get("state") == "COMMIT_INTENT_DURABLE"), None
            )
            journal_claimed_projected = (
                (intent.get("payload") or {}).get("projected_file_sha256") if intent else None
            )
            migration_id = self.migration_id

            # (3) Independently derived fingerprints (never trusted from the journal).
            derived = self._derive_recovery_fingerprints(
                baseline_hash, target_id, journal_claimed_projected
            )

            if baseline_hash and current_hash == baseline_hash:
                file_state = "EXPECTED_SOURCE"
            elif derived["projected"] and current_hash == derived["projected"]:
                file_state = "EXPECTED_PROJECTED"
            else:
                file_state = "UNKNOWN"

            if file_state == "UNKNOWN":
                raise RecoveryStateError(
                    f"Recovery failed closed: active Canonical hash '{current_hash}' is UNKNOWN. "
                    "No write permitted."
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

                if latest in ("COMMIT_INTENT_DURABLE", "PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED"):
                    planned = list(entries)
                    if latest == "COMMIT_INTENT_DURABLE":
                        planned.append(RepairJournal.build_entry("PROJECTION_COMMITTED", {
                            "migration_id": migration_id,
                            "daily_matches_sha256": current_hash,
                        }))
                    if latest in ("COMMIT_INTENT_DURABLE", "PROJECTION_COMMITTED"):
                        planned.append(RepairJournal.build_entry("POSTCONDITIONS_VERIFIED", {
                            "migration_id": migration_id,
                            "note": "recovery verification",
                        }))
                    completion = RepairJournal.build_entry("COMPLETION_RECORDED", {
                        "migration_id": migration_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "VERIFIED_POSTCONDITIONS_AND_RECORDED_COMPLETION",
                        "after_total": len(read_json_strict(daily_file).get("matches", [])),
                        "authorization_sha256": authorization_fingerprint(authorization),
                    })
                    planned.append(completion)

                    # Prove the full postcondition contract -- including the
                    # terminal journal state we are about to write -- before it
                    # is allowed to become durable.
                    self.verify_completed_dataset(
                        authorization,
                        trusted_public_key=trusted_public_key,
                        journal_entries=planned,
                    )

                    for entry in planned[len(entries):]:
                        self.journal.append_entry(entry["state"], entry["payload"])
                    return {
                        "status": "RECOVERED_FORWARD_COMPLETED",
                        "action": "VERIFIED_AND_COMPLETED",
                        "latest_state": "COMPLETION_RECORDED",
                    }

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

    p_inspect = sub.add_parser(
        "inspect", help="Read-only dataset integrity inspection (not repair verification)"
    )
    p_inspect.add_argument("--data-dir", required=True)

    p_keygen = sub.add_parser(
        "keygen",
        help="Provision an owner Ed25519 signing keypair OUTSIDE the repository",
    )
    p_keygen.add_argument("--private-key-out", required=True, help="Owner-held PKCS#8 PEM path")
    p_keygen.add_argument("--public-key-out", required=True, help="Trusted public key PEM path")

    p_prepare = sub.add_parser(
        "prepare",
        help="Read-only: build an UNSIGNED preparation artifact for owner review",
    )
    p_prepare.add_argument("--data-dir", required=True)
    p_prepare.add_argument("--target-id", required=True, help="Canonical ID to de-duplicate")
    p_prepare.add_argument("--operator", required=True, help="Operator identity")
    p_prepare.add_argument("--reason", required=True, help="Justification recorded in the journal")
    p_prepare.add_argument("--out", required=True, help="Where to write the preparation artifact")
    p_prepare.add_argument("--migration-id", default=None)

    p_sign = sub.add_parser(
        "approve",
        help="Owner approval: sign a preparation artifact with an external Ed25519 key",
    )
    p_sign.add_argument("--prepared", required=True, help="Unsigned preparation artifact")
    p_sign.add_argument("--private-key", required=True, help="Owner Ed25519 private key (outside repo)")
    p_sign.add_argument("--out", required=True, help="Where to write the signed authorization artifact")
    p_sign.add_argument("--operator", default=None, help="Must match the prepared operator, if given")

    p_verify_auth = sub.add_parser(
        "verify-authorization",
        help="Read-only: verify an owner approval against the trusted public key",
    )
    p_verify_auth.add_argument("--authorization", required=True)
    p_verify_auth.add_argument("--trusted-public-key", required=True)

    p_dry = sub.add_parser(
        "dry-run",
        help="Read-only projection preview (writes only an external preview artifact)",
    )
    p_dry.add_argument("--data-dir", required=True)
    p_dry.add_argument("--authorization", default=None)
    p_dry.add_argument("--workspace", default=None)
    p_dry.add_argument(
        "--trusted-public-key", default=None,
        help="Required only when the supplied artifact claims 'authorized': true",
    )

    p_apply = sub.add_parser("apply", help="Execute the guarded repair")
    p_apply.add_argument("--data-dir", required=True)
    p_apply.add_argument("--authorization", required=True)
    p_apply.add_argument("--workspace", default=None)
    p_apply.add_argument("--operator", required=True, help="Operator identity; must match the approval")
    p_apply.add_argument("--trusted-public-key", required=True, help="Trusted Ed25519 public key PEM")
    p_apply.add_argument("--confirm-apply", action="store_true")

    p_verify = sub.add_parser(
        "verify", help="Read-only FULL repair postcondition verification"
    )
    p_verify.add_argument("--data-dir", required=True)
    p_verify.add_argument("--authorization", required=True)
    p_verify.add_argument("--trusted-public-key", required=True)
    p_verify.add_argument("--operator", default=None, help="If given, must match the approval")
    p_verify.add_argument(
        "--workspace", default=None,
        help="Journal/archive workspace; defaults to <parent_of_data_dir>/repair_runs/<migration_id>",
    )

    p_recover = sub.add_parser("recover", help="Resolve an interrupted repair")
    p_recover.add_argument("--data-dir", required=True)
    p_recover.add_argument("--workspace", required=True)
    p_recover.add_argument("--authorization", required=True)
    p_recover.add_argument("--trusted-public-key", required=True)
    p_recover.add_argument("--operator", required=True)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.subcommand == "inspect":
            print(json.dumps(inspect_dataset(args.data_dir), indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "keygen":
            info = generate_signing_keypair(args.private_key_out, args.public_key_out)
            print(json.dumps({
                "status": "OWNER_KEYPAIR_PROVISIONED",
                "algorithm": info["algorithm"],
                "key_id": info["key_id"],
                "private_key_path": info["private_key_path"],
                "public_key_path": info["public_key_path"],
                "note": "Keep the private key offline; pin the public key as --trusted-public-key.",
            }, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "prepare":
            artifact = build_preparation(
                data_dir=args.data_dir,
                target_canonical_id=args.target_id,
                operator=args.operator,
                reason=args.reason,
                migration_id=args.migration_id,
            )
            write_json_atomic(args.out, artifact)
            print(json.dumps({
                "status": "PREPARATION_WRITTEN_UNSIGNED",
                "out": args.out,
                "authorized": False,
                "migration_id": artifact["migration_id"],
                "target_canonical_id": artifact["target_canonical_id"],
                "expected_source_sha256": artifact["expected_source_sha256"],
                "expected_source_total": artifact["expected_source_total"],
                "expected_candidate_sha256": artifact["expected_candidate_sha256"],
                "preparation_sha256": authorization_fingerprint(artifact),
                "note": (
                    "This artifact grants nothing. Have the owner run 'approve' with the "
                    "external Ed25519 private key before 'apply'."
                ),
            }, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "approve":
            prepared = read_json_strict(args.prepared)
            artifact = sign_authorization(
                prepared, args.private_key, operator=args.operator
            )
            write_json_atomic(args.out, artifact)
            print(json.dumps({
                "status": "AUTHORIZATION_SIGNED",
                "out": args.out,
                "authorized": True,
                "algorithm": artifact[APPROVAL_FIELD]["algorithm"],
                "key_id": artifact[APPROVAL_FIELD]["key_id"],
                "migration_id": artifact["migration_id"],
                "authorization_sha256": authorization_fingerprint(artifact),
            }, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "verify-authorization":
            authorization = load_authorization(
                args.authorization, args.trusted_public_key, require_signature=True
            )
            print(json.dumps({
                "status": "AUTHORIZATION_SIGNATURE_VERIFIED",
                "verification": verify_authorization_signature(
                    authorization, args.trusted_public_key
                ),
                "migration_id": authorization["migration_id"],
                "target_canonical_id": authorization["target_canonical_id"],
                "operator": authorization["operator"],
            }, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "dry-run":
            authorization = (
                load_authorization(
                    args.authorization, args.trusted_public_key, require_signature=False
                )
                if args.authorization else None
            )
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                writable=False,
            )
            print(json.dumps(repairer.dry_run(authorization), indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "apply":
            authorization = load_authorization(
                args.authorization, args.trusted_public_key, require_signature=True
            )
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                migration_id=authorization.get("migration_id"),
                trusted_public_key=args.trusted_public_key,
            )
            result = repairer.apply(
                authorization=authorization,
                confirm_apply=args.confirm_apply,
                operator=args.operator,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        if args.subcommand == "verify":
            authorization = load_authorization(
                args.authorization, args.trusted_public_key, require_signature=True
            )
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                writable=False,
                migration_id=authorization.get("migration_id"),
                trusted_public_key=args.trusted_public_key,
            )
            report = repairer.verify(
                authorization,
                trusted_public_key=args.trusted_public_key,
                operator=args.operator,
            )
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0 if report.get("verified") and report.get("complete") else 1

        if args.subcommand == "recover":
            authorization = load_authorization(
                args.authorization, args.trusted_public_key, require_signature=True
            )
            repairer = ProductionCanonicalRepairer(
                data_dir=args.data_dir,
                workspace=args.workspace,
                migration_id=authorization.get("migration_id"),
                trusted_public_key=args.trusted_public_key,
            )
            print(json.dumps(
                repairer.recover(
                    authorization,
                    operator=args.operator,
                    trusted_public_key=args.trusted_public_key,
                ),
                indent=2, ensure_ascii=False,
            ))
            return 0

        return 0

    except SignatureVerificationError as exc:
        print(f"SIGNATURE ERROR: {exc}", file=sys.stderr)
        return 112
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
    except IncompleteVerificationError as exc:
        print(f"INCOMPLETE VERIFICATION: {exc}", file=sys.stderr)
        return 107
    except RecoveryStateError as exc:
        print(f"RECOVERY ERROR: {exc}", file=sys.stderr)
        return 106
    except Exception as exc:  # noqa: BLE001
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
