"""
tools/staging_canonical_repair.py
=================================
Guarded Historical Canonical Projection Migration Tool for Isolated Staging.

This tool performs guarded, deterministic reconciliation of historical Canonical
duplicate records in isolated staging replicas.

STRICT CONSTRAINTS:
- NEVER runs against production directories or evidence epoch archives.
- STRICTLY rejects protected paths:
    * Production repo: Daily-Sports-Lottery
    * Production DATA_DIR: Daily-Sports-Lottery/data
    * EPOCH_0: _quiescent_evidence_backup_20261010_224710
    * EPOCH_1: _quiescent_evidence_backup_epoch1_20261011_014059
- Operates under a 7-phase crash-recovery state machine.
- Preserves all 6 non-Canonical stores with byte-level immutability.
- Candidate projection status is explicitly PROPOSED_NOT_APPROVED.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# Protected path substrings or exact basenames for fail-closed safety gating
PROTECTED_PATH_PATTERNS = [
    r"Daily-Sports-Lottery\data",
    r"Daily-Sports-Lottery/data",
    r"_quiescent_evidence_backup_20261010_224710",
    r"_quiescent_evidence_backup_epoch1_20261011_014059",
]

NON_CANONICAL_STORES = [
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
]

TARGET_CANONICAL_ID = "500l-2026-10-09-1362422"

EXPECTED_RECORD_A_SHA256 = "c692bbff117559e7f320093f5f49e9478522501de012a66fe22442dfff0624ce"
EXPECTED_RECORD_B_SHA256 = "bbfaed31fba1df112da92b7f4ec14ec647bdedcaa7734265db69d5ef114748c6"
EXPECTED_CANDIDATE_SHA256 = "93a1a6105b0b5db6fe0ce2b7a5d38d5af4a73d3aa980c62c718c7c6464c1785f"
EXPECTED_SOURCE_DAILY_SHA256 = "a53b351628d7736d5573752e5d7ae2a38b25394fbddaa2f1950e93efd68f760e"
DEFAULT_MIGRATION_ID = "MIG-20261011-CANONICAL-500L-1362422"


# =============================================================================
# Custom Exception Hierarchy
# =============================================================================

class StagingMigrationError(Exception):
    """Base exception for staging migration operations."""
    pass


class ProtectedPathViolationError(StagingMigrationError):
    """Raised when an operation targets or touches a protected path."""
    pass


class PreconditionViolationError(StagingMigrationError):
    """Raised when migration preconditions or schema integrity checks fail."""
    pass


class StagingLockError(StagingMigrationError):
    """Raised when failing to acquire or hold the staging lock."""
    pass


class ImmutableStoreMutationError(StagingMigrationError):
    """Raised when a non-Canonical store has been altered."""
    pass


class FaultInjectionError(StagingMigrationError):
    """Raised by deliberate fault-injection hooks for testing."""
    pass


class RecoveryStateError(StagingMigrationError):
    """Raised when recovery cannot be safely resolved."""
    pass


# =============================================================================
# Safety Boundary Validation
# =============================================================================

def normalize_abs_path(path: str) -> str:
    """Resolve and normalize path handling symlinks, junctions, and case."""
    if not path:
        return ""
    try:
        real = os.path.realpath(os.path.abspath(path))
        return os.path.normcase(os.path.normpath(real))
    except Exception as exc:
        raise ProtectedPathViolationError(f"Cannot resolve path '{path}': {exc}")


def assert_safe_path(target_path: str, context: str = "path") -> str:
    """
    Validate that target_path does NOT resolve into any protected production,
    repository root, or evidence epoch path.
    """
    if not target_path:
        raise ProtectedPathViolationError(f"Empty {context} provided.")

    norm = normalize_abs_path(target_path)
    norm_lower = norm.lower()

    # Check for direct protected directory keywords
    for pattern in PROTECTED_PATH_PATTERNS:
        pat_clean = os.path.normcase(os.path.normpath(pattern)).lower()
        if pat_clean in norm_lower:
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: {context} '{target_path}' resolves to protected pattern '{pattern}'"
            )

    # Check for production checkout root
    prod_markers = [
        os.path.normcase(os.path.normpath(r"program\Daily-Sports-Lottery")).lower(),
        os.path.normcase(os.path.normpath(r"program/Daily-Sports-Lottery")).lower(),
    ]
    for marker in prod_markers:
        if marker in norm_lower and "dsl-inc-i" not in norm_lower:
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: {context} '{target_path}' is inside production repository!"
            )

    # Check for evidence backup directories
    if "_quiescent_evidence_backup" in norm_lower:
        raise ProtectedPathViolationError(
            f"SAFETY VIOLATION: {context} '{target_path}' resolves into preserved evidence epoch!"
        )

    return norm


# =============================================================================
# Cryptographic & File Utilities
# =============================================================================

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def serialize_record(record: dict) -> str:
    """Canonical deterministically sorted JSON serialization for record hashing."""
    return json.dumps(record, ensure_ascii=False, sort_keys=True)


def hash_record(record: dict) -> str:
    return sha256_bytes(serialize_record(record).encode("utf-8"))


def read_json_strict(path: str) -> Any:
    """Strict JSON file reader with NO .corrupt side effects."""
    if not os.path.exists(path):
        raise PreconditionViolationError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception as exc:
            raise PreconditionViolationError(f"Malformed JSON in {path}: {exc}")


def write_json_atomic(path: str, data: Any, indent: int = 2) -> None:
    """Single-file atomic replacement with flush, sync and os.replace."""
    assert_safe_path(path, context="atomic write target")
    dirname = os.path.dirname(os.path.abspath(path))
    os.makedirs(dirname, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix=".tmp_staging_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise


def valid_full_time_score(score: Any) -> bool:
    """Strictly validate full time score structure."""
    if not isinstance(score, dict):
        return False
    ft = score.get("ft")
    if not isinstance(ft, (list, tuple)) or len(ft) < 2:
        return False
    h, a = ft[0], ft[1]
    if type(h) is not int or type(a) is not int:
        return False
    if h < 0 or a < 0:
        return False
    return True


# =============================================================================
# Staging Locking Protocol
# =============================================================================

@contextmanager
def acquire_staging_lock(data_dir: str):
    """
    Acquire cross-process exclusive OS lock on .refresh.lock inside isolated data_dir.
    """
    assert_safe_path(data_dir, context="staging data_dir lock")
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
                raise StagingLockError(f"Staging lock busy or denied: {exc}")
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StagingLockError(f"Staging lock busy or denied: {exc}")

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
# Journal & State Machine
# =============================================================================

JOURNAL_STATES = [
    "PREPARED",
    "EVIDENCE_VERIFIED",
    "AUTHORIZED_FOR_STAGING_SIMULATION",
    "COMMIT_INTENT_DURABLE",
    "PROJECTION_COMMITTED",
    "POSTCONDITIONS_VERIFIED",
    "COMPLETION_RECORDED",
]


class StagingJournal:
    def __init__(self, journal_file: str):
        self.journal_file = assert_safe_path(journal_file, context="journal_file")
        os.makedirs(os.path.dirname(self.journal_file), exist_ok=True)

    def append_entry(self, state: str, payload: dict) -> None:
        if state not in JOURNAL_STATES:
            raise StagingMigrationError(f"Unknown state: {state}")

        entry = {
            "state": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"

        with open(self.journal_file, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def read_entries(self) -> List[dict]:
        if not os.path.exists(self.journal_file):
            return []
        entries = []
        with open(self.journal_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception as exc:
                    entries.append({"state": "CORRUPTED_LINE", "line_no": line_no, "raw": line, "error": str(exc)})
        return entries

    def get_latest_valid_state(self) -> Optional[str]:
        entries = self.read_entries()
        for e in reversed(entries):
            st = e.get("state")
            if st in JOURNAL_STATES:
                return st
        return None


# =============================================================================
# Core Migration & Projection Logic
# =============================================================================

def build_candidate_projection(record_a: dict, record_b: dict) -> Tuple[dict, dict]:
    """
    Synthesize candidate record from Record A and Record B according to design:
    - Retain Canonical ID: 500l-2026-10-09-1362422
    - Club identities: Record B full names ('巴拉纳竞技' vs '米内罗竞技')
    - Terminal status: Record B finished status
    - Final score: Record B valid 2-2 score
    - Competition number: Record A value 4004
    - Market odds: Record A closing odds (4.75 / 3.40 / 1.60)
    - Odds timestamp: Record A original timestamp
    - Preserves sport, date, time, league, round, kickoff_time_known, market_tracked.

    Returns: (candidate_record, field_diff)
    """
    candidate = copy.deepcopy(record_b)
    candidate["jczq_no"] = record_a.get("jczq_no", "")
    candidate["odds"] = copy.deepcopy(record_a.get("odds"))
    candidate["odds_updated_at"] = record_a.get("odds_updated_at")

    field_diff = {
        "id": {"source": "shared", "val": candidate["id"]},
        "home": {"source": "record_b", "val": candidate["home"], "orig_a": record_a.get("home")},
        "away": {"source": "record_b", "val": candidate["away"], "orig_a": record_a.get("away")},
        "status": {"source": "record_b", "val": candidate["status"], "orig_a": record_a.get("status")},
        "score": {"source": "record_b", "val": candidate["score"], "orig_a": record_a.get("score")},
        "jczq_no": {"source": "record_a", "val": candidate["jczq_no"], "orig_b": record_b.get("jczq_no")},
        "odds": {"source": "record_a", "val": candidate["odds"], "orig_b": record_b.get("odds")},
        "odds_updated_at": {"source": "record_a", "val": candidate["odds_updated_at"], "orig_b": record_b.get("odds_updated_at")},
        "projection_status": "PROPOSED_NOT_APPROVED",
        "approval_requirement": "HUMAN_PRODUCTION_APPROVAL_PENDING",
    }

    return candidate, field_diff


class StagingCanonicalMigrator:
    """
    Manages isolated staging migration operations.
    """

    def __init__(self, staging_root: str):
        self.staging_root = assert_safe_path(staging_root, context="staging_root")
        self.input_dir = os.path.join(self.staging_root, "input", "data")
        self.working_dir = os.path.join(self.staging_root, "working", "data")
        self.archive_dir = os.path.join(self.staging_root, "archive")
        self.journal_dir = os.path.join(self.staging_root, "journal")
        self.output_dir = os.path.join(self.staging_root, "output")

        self.journal = StagingJournal(os.path.join(self.journal_dir, "disposition_journal.jsonl"))

    def get_store_hashes(self, data_dir: str) -> Dict[str, str]:
        assert_safe_path(data_dir, context="data_dir for store hashes")
        hashes = {}
        for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
            path = os.path.join(data_dir, store)
            if os.path.exists(path):
                hashes[store] = sha256_file(path)
            else:
                hashes[store] = "MISSING"
        return hashes

    def inspect_dataset(self, data_dir: Optional[str] = None) -> dict:
        target_dir = data_dir or self.working_dir
        assert_safe_path(target_dir, context="inspection target_dir")

        daily_file = os.path.join(target_dir, "daily_matches.json")
        if not os.path.exists(daily_file):
            raise PreconditionViolationError(f"daily_matches.json not found in {target_dir}")

        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])
        total_matches = len(matches)

        id_counts: Dict[str, int] = {}
        target_indices: List[int] = []
        target_records: List[dict] = []

        for idx, m in enumerate(matches):
            mid = m.get("id")
            if not mid or not isinstance(mid, str):
                id_counts["__INVALID__"] = id_counts.get("__INVALID__", 0) + 1
            else:
                id_counts[mid] = id_counts.get(mid, 0) + 1
                if mid == TARGET_CANONICAL_ID:
                    target_indices.append(idx)
                    target_records.append(m)

        duplicate_ids = {k: v for k, v in id_counts.items() if v > 1}
        unique_ids_count = len(id_counts)

        store_hashes = self.get_store_hashes(target_dir)

        # Downstream references count
        downstream = {}
        for fn in ["prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json"]:
            p = os.path.join(target_dir, fn)
            if os.path.exists(p):
                d = read_json_strict(p)
                items = d.get("snapshots") or d.get("settlements") or d.get("rows") or []
                if isinstance(items, dict):
                    matches_ref = [v for v in items.values() if v.get("match_id") == TARGET_CANONICAL_ID]
                    downstream[fn] = {"total": len(items), "target_refs": len(matches_ref)}
                elif isinstance(items, list):
                    matches_ref = [v for v in items if v.get("match_id") == TARGET_CANONICAL_ID]
                    downstream[fn] = {"total": len(items), "target_refs": len(matches_ref)}

        calib_file = os.path.join(target_dir, "calibrated.json")
        if os.path.exists(calib_file):
            calib = read_json_strict(calib_file)
            ids_list = calib.get("ids", []) if isinstance(calib, dict) else (calib if isinstance(calib, list) else [])
            downstream["calibrated.json"] = {
                "total": len(ids_list),
                "target_in_calibrated": TARGET_CANONICAL_ID in ids_list,
            }

        team_file = os.path.join(target_dir, "team_strength.json")
        if os.path.exists(team_file):
            ts = read_json_strict(team_file)
            teams_dict = ts.get("teams", {}) if isinstance(ts, dict) and "teams" in ts else ts
            downstream["team_strength.json"] = {"total_teams": len(teams_dict)}

        return {
            "data_dir": target_dir,
            "total_matches": total_matches,
            "unique_ids_count": unique_ids_count,
            "duplicate_ids": duplicate_ids,
            "target_occurrences": len(target_indices),
            "target_indices": target_indices,
            "store_hashes": store_hashes,
            "downstream_references": downstream,
        }

    def dry_run(self) -> dict:
        """
        Produce candidate projection and field diff without modifying source files.
        """
        assert_safe_path(self.working_dir, context="working_dir dry-run")
        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])

        target_matches = [(i, m) for i, m in enumerate(matches) if m.get("id") == TARGET_CANONICAL_ID]
        if len(target_matches) != 2:
            raise PreconditionViolationError(
                f"dry-run expects exactly 2 target occurrences, found {len(target_matches)}"
            )

        idx_a, rec_a = target_matches[0]
        idx_b, rec_b = target_matches[1]

        candidate, diff = build_candidate_projection(rec_a, rec_b)

        os.makedirs(self.output_dir, exist_ok=True)
        preview_path = os.path.join(self.output_dir, "canonical-projection-preview.json")
        diff_path = os.path.join(self.output_dir, "canonical-field-diff.json")

        preview_data = {
            "STAGING_ONLY": True,
            "HUMAN_PRODUCTION_APPROVAL": "NOT_GRANTED",
            "PROJECTION_STATUS": "PROPOSED_NOT_APPROVED",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_id": TARGET_CANONICAL_ID,
            "candidate_record": candidate,
            "candidate_sha256": hash_record(candidate),
        }

        write_json_atomic(preview_path, preview_data)
        write_json_atomic(diff_path, diff)

        return {
            "status": "DRY_RUN_COMPLETED",
            "candidate_sha256": hash_record(candidate),
            "preview_file": preview_path,
            "diff_file": diff_path,
            "projection_status": "PROPOSED_NOT_APPROVED",
        }

    def staging_apply(
        self,
        simulation: bool = False,
        fault_inject: Optional[str] = None,
        expected_source_sha: Optional[str] = None,
        migration_id: str = DEFAULT_MIGRATION_ID,
    ) -> dict:
        """
        Execute migration against isolated staging replica under 7-phase state machine.
        """
        if not simulation:
            raise PreconditionViolationError(
                "staging_apply requires simulation=True marker to confirm staging mode."
            )

        assert_safe_path(self.working_dir, context="working_dir staging_apply")
        os.makedirs(self.archive_dir, exist_ok=True)
        os.makedirs(self.output_dir, exist_ok=True)

        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        current_daily = read_json_strict(daily_file)
        current_matches = current_daily.get("matches", [])
        current_targets = [m for m in current_matches if m.get("id") == TARGET_CANONICAL_ID]

        # 0. Check Idempotency (Already Applied?)
        if len(current_matches) == 192 and len(current_targets) == 1:
            cand_hash = hash_record(current_targets[0])
            if cand_hash == EXPECTED_CANDIDATE_SHA256:
                return {
                    "status": "ALREADY_APPLIED_NO_OP",
                    "total_matches": 192,
                    "target_occurrences": 1,
                    "candidate_sha256": cand_hash,
                    "message": "Store is already in post-migration projected state. No operation performed.",
                }

        # 1. Acquire staging OS byte lock
        with acquire_staging_lock(self.working_dir):
            if fault_inject == "before_prepare":
                raise FaultInjectionError("Fault injection: before_prepare")

            # Phase 1: PREPARED
            pre_store_hashes = self.get_store_hashes(self.working_dir)
            for store, h in pre_store_hashes.items():
                if h == "MISSING":
                    raise PreconditionViolationError(f"Required store missing: {store}")

            if expected_source_sha and pre_store_hashes["daily_matches.json"] != expected_source_sha:
                raise PreconditionViolationError(
                    f"Canonical source hash mismatch: {pre_store_hashes['daily_matches.json']} != {expected_source_sha}"
                )

            # Check journal for reused migration ID with conflicting target
            for entry in self.journal.read_entries():
                if entry.get("state") == "COMMIT_INTENT_DURABLE":
                    payload = entry.get("payload", {})
                    prev_mig_id = payload.get("migration_id")
                    prev_target_hash = payload.get("projected_file_sha256")
                    if prev_mig_id == migration_id and prev_target_hash != EXPECTED_CANDIDATE_SHA256:
                        raise PreconditionViolationError("Reused migration ID with different target hash")

            self.journal.append_entry("PREPARED", {
                "migration_id": migration_id,
                "working_dir": self.working_dir,
                "pre_store_hashes": pre_store_hashes,
            })

            if fault_inject == "before_archive":
                raise FaultInjectionError("Fault injection: before_archive")

            # Phase 2: EVIDENCE_VERIFIED
            daily_data = read_json_strict(daily_file)
            matches = daily_data.get("matches", [])
            if len(matches) != 193:
                raise PreconditionViolationError(f"Expected 193 matches, found {len(matches)}")

            all_ids = [m.get("id") for m in matches]
            counts = Counter(all_ids)
            unrelated_dups = {k: v for k, v in counts.items() if v > 1 and k != TARGET_CANONICAL_ID}
            if unrelated_dups:
                raise PreconditionViolationError(f"Unrelated duplicate IDs detected: {unrelated_dups}")

            targets = [(i, m) for i, m in enumerate(matches) if m.get("id") == TARGET_CANONICAL_ID]
            if len(targets) != 2:
                if len(targets) > 2:
                    raise PreconditionViolationError(f"Unexpected third target duplicate found ({len(targets)})")
                else:
                    raise PreconditionViolationError(f"Missing target records ({len(targets)} found, expected 2)")

            idx_a, rec_a = targets[0]
            idx_b, rec_b = targets[1]
            rec_a_hash = hash_record(rec_a)
            rec_b_hash = hash_record(rec_b)

            if rec_a_hash != EXPECTED_RECORD_A_SHA256:
                raise PreconditionViolationError(f"Record A hash mismatch: {rec_a_hash} != {EXPECTED_RECORD_A_SHA256}")
            if rec_b_hash != EXPECTED_RECORD_B_SHA256:
                raise PreconditionViolationError(f"Record B hash mismatch: {rec_b_hash} != {EXPECTED_RECORD_B_SHA256}")

            # Validate final score format
            if not valid_full_time_score(rec_b.get("score")):
                raise PreconditionViolationError("Record B final score is malformed or invalid")

            self.journal.append_entry("EVIDENCE_VERIFIED", {
                "migration_id": migration_id,
                "rec_a_hash": rec_a_hash,
                "rec_b_hash": rec_b_hash,
                "target_indices": [idx_a, idx_b],
            })

            if fault_inject == "during_archive":
                raise FaultInjectionError("Fault injection: during_archive")

            # Create forensic archive
            raw_archive_path = os.path.join(self.archive_dir, "daily_matches_raw.json")
            with open(daily_file, "rb") as src, open(raw_archive_path, "wb") as dst:
                raw_bytes = src.read()
                dst.write(raw_bytes)
                dst.flush()
                os.fsync(dst.fileno())

            write_json_atomic(os.path.join(self.archive_dir, "record_a.json"), rec_a)
            write_json_atomic(os.path.join(self.archive_dir, "record_b.json"), rec_b)

            archive_manifest = {
                "migration_id": migration_id,
                "source_file_sha256": sha256_file(raw_archive_path),
                "record_a_sha256": rec_a_hash,
                "record_b_sha256": rec_b_hash,
                "record_a_index": idx_a,
                "record_b_index": idx_b,
                "archived_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json_atomic(os.path.join(self.archive_dir, "forensic_manifest.json"), archive_manifest)

            if fault_inject == "after_archive_before_verify":
                raise FaultInjectionError("Fault injection: after_archive_before_verify")

            # Verify archive
            if sha256_file(raw_archive_path) != pre_store_hashes["daily_matches.json"]:
                raise PreconditionViolationError("Forensic archive raw bytes hash mismatch!")

            if fault_inject == "after_archive_verify":
                raise FaultInjectionError("Fault injection: after_archive_verify")

            # Phase 3: AUTHORIZED_FOR_STAGING_SIMULATION
            self.journal.append_entry("AUTHORIZED_FOR_STAGING_SIMULATION", {
                "migration_id": migration_id,
                "simulation": True,
                "human_approval": "NOT_GRANTED",
                "projection_status": "PROPOSED_NOT_APPROVED",
            })

            # Build candidate projection
            candidate, diff = build_candidate_projection(rec_a, rec_b)
            cand_hash = hash_record(candidate)

            projected_matches = matches[:idx_a] + [candidate] + matches[idx_a + 1:idx_b] + matches[idx_b + 1:]
            if len(projected_matches) != 192:
                raise PreconditionViolationError(f"Projected count error: {len(projected_matches)} != 192")

            projected_data = {
                "matches": projected_matches,
                "meta": copy.deepcopy(daily_data.get("meta", {})),
            }
            projected_data["meta"]["total"] = 192

            # Compute serialized projected bytes
            projected_json_bytes = json.dumps(projected_data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            projected_file_hash = sha256_bytes(projected_json_bytes)

            if fault_inject == "before_commit_intent":
                raise FaultInjectionError("Fault injection: before_commit_intent")

            # Phase 4: COMMIT_INTENT_DURABLE
            self.journal.append_entry("COMMIT_INTENT_DURABLE", {
                "migration_id": migration_id,
                "target_id": TARGET_CANONICAL_ID,
                "candidate_sha256": cand_hash,
                "projected_file_sha256": projected_file_hash,
                "projected_total": 192,
            })

            if fault_inject == "after_commit_intent":
                raise FaultInjectionError("Fault injection: after_commit_intent")

            if fault_inject == "before_file_replace":
                raise FaultInjectionError("Fault injection: before_file_replace")

            # Verify daily_file has not become stale immediately before write
            if sha256_file(daily_file) != pre_store_hashes["daily_matches.json"]:
                raise PreconditionViolationError("Stale source file: daily_matches.json modified concurrently before commit")

            # Phase 5: PROJECTION_COMMITTED
            write_json_atomic(daily_file, projected_data)

            self.journal.append_entry("PROJECTION_COMMITTED", {
                "migration_id": migration_id,
                "daily_matches_sha256": sha256_file(daily_file),
            })

            if fault_inject == "after_file_replace":
                raise FaultInjectionError("Fault injection: after_file_replace")

            if fault_inject == "before_postconditions":
                raise FaultInjectionError("Fault injection: before_postconditions")

            # Phase 6: POSTCONDITIONS_VERIFIED
            post_inspect = self.inspect_dataset(self.working_dir)
            if post_inspect["total_matches"] != 192:
                raise PreconditionViolationError(f"Postcondition failed: total {post_inspect['total_matches']} != 192")
            if post_inspect["unique_ids_count"] != 192:
                raise PreconditionViolationError(f"Postcondition failed: unique {post_inspect['unique_ids_count']} != 192")
            if post_inspect["target_occurrences"] != 1:
                raise PreconditionViolationError(f"Postcondition failed: target occurrences {post_inspect['target_occurrences']} != 1")

            # Check 6 non-Canonical stores unchanged!
            post_store_hashes = post_inspect["store_hashes"]
            for store in NON_CANONICAL_STORES:
                if post_store_hashes[store] != pre_store_hashes[store]:
                    raise ImmutableStoreMutationError(
                        f"CRITICAL: Immutable store {store} mutated! {pre_store_hashes[store]} -> {post_store_hashes[store]}"
                    )

            self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                "migration_id": migration_id,
                "post_inspect": post_inspect,
            })

            if fault_inject == "before_completion":
                raise FaultInjectionError("Fault injection: before_completion")

            # Phase 7: COMPLETION_RECORDED
            result_payload = {
                "migration_id": migration_id,
                "status": "SUCCESS",
                "before_total": 193,
                "after_total": 192,
                "unique_ids": 192,
                "duplicate_ids_count": 0,
                "candidate_sha256": cand_hash,
                "projected_file_sha256": post_store_hashes["daily_matches.json"],
                "immutable_stores_changed": 0,
                "projection_status": "PROPOSED_NOT_APPROVED",
                "human_approval": "NOT_GRANTED",
            }

            self.journal.append_entry("COMPLETION_RECORDED", result_payload)

            result_file = os.path.join(self.output_dir, "staging-apply-result.json")
            write_json_atomic(result_file, result_payload)

            return result_payload

    def staging_recover(self) -> dict:
        """
        Evaluate journal and filesystem state to resolve interrupted or crashed migrations.
        """
        assert_safe_path(self.working_dir, context="working_dir staging_recover")
        entries = self.journal.read_entries()
        if not entries:
            return {"status": "NO_JOURNAL_ENTRIES", "action": "NOTHING_TO_RECOVER"}

        for entry in entries:
            if entry.get("state") == "CORRUPTED_LINE":
                raise RecoveryStateError(f"Corrupted journal line detected: line {entry.get('line_no')}")

        latest_state = self.journal.get_latest_valid_state()
        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        current_daily_hash = sha256_file(daily_file) if os.path.exists(daily_file) else "MISSING"

        raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
        raw_archive_exists = os.path.exists(raw_archive)

        with acquire_staging_lock(self.working_dir):
            # Check immutable stores against PREPARED entry if available
            prep_entry = next((e for e in entries if e.get("state") == "PREPARED"), None)
            if prep_entry:
                pre_hashes = prep_entry.get("payload", {}).get("pre_store_hashes", {})
                current_store_hashes = self.get_store_hashes(self.working_dir)
                for s in NON_CANONICAL_STORES:
                    if s in pre_hashes and current_store_hashes.get(s) != pre_hashes[s]:
                        raise ImmutableStoreMutationError(
                            f"CRITICAL: Immutable store {s} mutated prior to recovery!"
                        )

            if latest_state in ("COMPLETION_RECORDED", "POSTCONDITIONS_VERIFIED"):
                return {
                    "status": "RECOVERED_COMPLETE",
                    "action": "ALREADY_COMPLETED",
                    "latest_state": latest_state,
                }

            if latest_state == "PROJECTION_COMMITTED":
                post_inspect = self.inspect_dataset(self.working_dir)
                if post_inspect["total_matches"] == 192 and post_inspect["unique_ids_count"] == 192:
                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {"post_inspect": post_inspect})
                    result_payload = {
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "after_total": 192,
                        "unique_ids": 192,
                        "projection_status": "PROPOSED_NOT_APPROVED",
                    }
                    self.journal.append_entry("COMPLETION_RECORDED", result_payload)
                    return result_payload
                else:
                    raise RecoveryStateError("Postconditions invalid after PROJECTION_COMMITTED crash")

            if latest_state == "COMMIT_INTENT_DURABLE":
                intent_entry = next((e for e in reversed(entries) if e.get("state") == "COMMIT_INTENT_DURABLE"), None)
                expected_hash = intent_entry["payload"].get("projected_file_sha256") if intent_entry else None

                if current_daily_hash == expected_hash:
                    self.journal.append_entry("PROJECTION_COMMITTED", {"daily_matches_sha256": current_daily_hash})
                    return self.staging_recover()
                else:
                    if raw_archive_exists:
                        with open(raw_archive, "rb") as src, open(daily_file, "wb") as dst:
                            dst.write(src.read())
                            dst.flush()
                            os.fsync(dst.fileno())
                        self.journal.append_entry("PREPARED", {"action": "ROLLED_BACK_TO_RAW_ARCHIVE"})
                        return {
                            "status": "RECOVERED_ROLLED_BACK",
                            "action": "RESTORED_ARCHIVE_PRE_COMMIT",
                        }
                    else:
                        raise RecoveryStateError("Missing raw archive to roll back from COMMIT_INTENT_DURABLE")

            if raw_archive_exists and current_daily_hash != sha256_file(raw_archive):
                with open(raw_archive, "rb") as src, open(daily_file, "wb") as dst:
                    dst.write(src.read())
                    dst.flush()
                    os.fsync(dst.fileno())
            return {
                "status": "RECOVERED_PRE_COMMIT_CLEAN",
                "action": "SAFE_RESET",
                "latest_state": latest_state,
            }


# =============================================================================
# CLI Interface
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded Historical Canonical Staging Migration Tool (Isolated Staging Only)"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # Subcommand: inspect
    p_inspect = subparsers.add_parser("inspect", help="Read-only dataset inspection")
    p_inspect.add_argument("--data-dir", required=True, help="Path to isolated data directory")

    # Subcommand: dry-run
    p_dry = subparsers.add_parser("dry-run", help="Simulate candidate projection and output diff")
    p_dry.add_argument("--staging-dir", required=True, help="Path to isolated staging workspace root")

    # Subcommand: staging-apply
    p_apply = subparsers.add_parser("staging-apply", help="Execute guarded staging migration")
    p_apply.add_argument("--staging-dir", required=True, help="Path to isolated staging workspace root")
    p_apply.add_argument("--simulation", action="store_true", required=True, help="Explicit staging simulation confirmation")
    p_apply.add_argument("--fault-inject", default=None, help="Optional test fault-injection point")
    p_apply.add_argument("--expected-source-sha", default=None, help="Optional source hash enforcement")
    p_apply.add_argument("--migration-id", default=DEFAULT_MIGRATION_ID, help="Migration execution identifier")

    # Subcommand: staging-verify
    p_verify = subparsers.add_parser("staging-verify", help="Verify post-migration staging dataset")
    p_verify.add_argument("--staging-dir", required=True, help="Path to isolated staging workspace root")

    # Subcommand: staging-recover
    p_recover = subparsers.add_parser("staging-recover", help="Recover from staging failure or crash")
    p_recover.add_argument("--staging-dir", required=True, help="Path to isolated staging workspace root")

    return parser


def main(args: Optional[List[str]] = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(args)

    try:
        if parsed.subcommand == "inspect":
            assert_safe_path(parsed.data_dir, context="inspect data-dir")
            migrator = StagingCanonicalMigrator(staging_root=os.path.dirname(os.path.abspath(parsed.data_dir)))
            report = migrator.inspect_dataset(parsed.data_dir)
            print(json.dumps(report, indent=2, ensure_ascii=False))
            return 0

        elif parsed.subcommand == "dry-run":
            assert_safe_path(parsed.staging_dir, context="dry-run staging-dir")
            migrator = StagingCanonicalMigrator(staging_root=parsed.staging_dir)
            res = migrator.dry_run()
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0

        elif parsed.subcommand == "staging-apply":
            assert_safe_path(parsed.staging_dir, context="staging-apply staging-dir")
            migrator = StagingCanonicalMigrator(staging_root=parsed.staging_dir)
            res = migrator.staging_apply(
                simulation=parsed.simulation,
                fault_inject=parsed.fault_inject,
                expected_source_sha=parsed.expected_source_sha,
                migration_id=parsed.migration_id,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0

        elif parsed.subcommand == "staging-verify":
            assert_safe_path(parsed.staging_dir, context="staging-verify staging-dir")
            migrator = StagingCanonicalMigrator(staging_root=parsed.staging_dir)
            res = migrator.inspect_dataset(migrator.working_dir)
            is_valid = (
                res["total_matches"] == 192
                and res["unique_ids_count"] == 192
                and res["target_occurrences"] == 1
                and len(res["duplicate_ids"]) == 0
            )
            print(json.dumps({"verified": is_valid, "report": res}, indent=2, ensure_ascii=False))
            return 0 if is_valid else 1

        elif parsed.subcommand == "staging-recover":
            assert_safe_path(parsed.staging_dir, context="staging-recover staging-dir")
            migrator = StagingCanonicalMigrator(staging_root=parsed.staging_dir)
            res = migrator.staging_recover()
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0

        return 0

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
    except FaultInjectionError as exc:
        print(f"FAULT INJECTION: {exc}", file=sys.stderr)
        return 105
    except RecoveryStateError as exc:
        print(f"RECOVERY ERROR: {exc}", file=sys.stderr)
        return 106
    except Exception as exc:
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
