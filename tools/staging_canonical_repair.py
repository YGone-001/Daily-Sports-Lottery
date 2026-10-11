"""
tools/staging_canonical_repair.py
=================================
Guarded Historical Canonical Projection Migration Tool for Isolated Staging.

This tool performs guarded, deterministic reconciliation of historical Canonical
duplicate records in isolated staging replicas.

STRICT CONSTRAINTS:
- NEVER runs against production directories or evidence epoch archives.
- STRICTLY rejects protected paths:
    * Production repo: Daily-Sports-Lottery and all descendants
    * Production DATA_DIR: Daily-Sports-Lottery/data
    * EPOCH_0: _quiescent_evidence_backup_20261010_224710
    * EPOCH_1: _quiescent_evidence_backup_epoch1_20261011_014059
    * Source Git checkout: dsl-inc-i and all descendants
- Requires verified staging workspace marker and dataset binding.
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

# Pinned Protected Paths
PROD_REPO_ROOT = r"C:\Users\YGone\Desktop\program\Daily-Sports-Lottery"
EPOCH_0_ROOT = r"C:\Users\YGone\Desktop\program\_quiescent_evidence_backup_20261010_224710"
EPOCH_1_ROOT = r"C:\Users\YGone\Desktop\_quiescent_evidence_backup_epoch1_20261011_014059"

# Source repository root (the git checkout hosting this code)
SOURCE_REPO_ROOT = os.path.realpath(os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

PROTECTED_ROOTS = [
    PROD_REPO_ROOT,
    EPOCH_0_ROOT,
    EPOCH_1_ROOT,
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
EXPECTED_SOURCE_DAILY_SHA256 = "a53b351628d7736d5444919d10d57fb153a4cb102eda4a159ca8f837a4500ce9"
EXPECTED_PROJECTED_DAILY_SHA256 = "3991ea06d05a18c8feb85a70c3fed466ddaaa268affa06b15b437e948569f453"

DEFAULT_MIGRATION_ID = "MIG-20261011-CANONICAL-500L-1362422"

EPOCH_1_BASELINE_MANIFEST = {
    "dataset_identity": "EXPECTED_EPOCH_1_FIXTURE",
    "daily_matches.json": EXPECTED_SOURCE_DAILY_SHA256,
    "team_strength.json": "2f255e71a745cdf31c833976f25029851b2e8ed475783a15675428913e344399",
    "calibrated.json": "0c36bcec7aed0a3c70f1aabff1fde75d8e1dec0ce6fe3b64d022390ededf769e",
    "prediction_snapshots.json": "4882735a06886339559f5fef6dfa2547f4656c430698f9b4b9670f75ddc75bd2",
    "odds_snapshots.json": "05356e6f9654d06519415c42f81e2231685163190de0d05c01a8f62bf020ab37",
    "settlements.json": "357ce7565b0ef682095624acfbe7b2046fd0e77eea102d53537cf95fe2bdd453",
    "evaluation_rows.json": "fc73826f209857d1179ceb44d30c67fa401d7c3ad55db3e59df183bc20e91dc0",
    "projected_file_sha256": EXPECTED_PROJECTED_DAILY_SHA256,
    "candidate_record_sha256": EXPECTED_CANDIDATE_SHA256,
    "record_a_sha256": EXPECTED_RECORD_A_SHA256,
    "record_b_sha256": EXPECTED_RECORD_B_SHA256,
}


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


class RecoveryStateError(StagingMigrationError):
    """Raised when recovery cannot be safely resolved."""
    pass


class ImmutableStoreMutationError(RecoveryStateError):
    """Raised when a non-Canonical store has been altered."""
    pass


class FaultInjectionError(StagingMigrationError):
    """Raised by deliberate fault-injection hooks for testing."""
    pass



# =============================================================================
# Safety Boundary Validation
# =============================================================================

def canonicalize_path(path: str) -> str:
    """Resolve and normalize path handling symlinks, junctions, and Windows case."""
    if not path:
        return ""
    try:
        real = os.path.realpath(os.path.abspath(path))
        return os.path.normcase(os.path.normpath(real))
    except Exception as exc:
        raise ProtectedPathViolationError(f"Cannot resolve path '{path}': {exc}")


def is_subpath_or_equal(target_path: str, parent_path: str) -> bool:
    """Check directory-boundary containment using os.path.commonpath."""
    try:
        target_norm = canonicalize_path(target_path)
        parent_norm = canonicalize_path(parent_path)
        if target_norm == parent_norm:
            return True
        common = os.path.commonpath([target_norm, parent_norm])
        return common == parent_norm
    except (ValueError, OSError):
        return False


def assert_safe_path(
    target_path: str,
    context: str = "path",
    staging_root: Optional[str] = None,
    allow_source_repo: bool = False,
) -> str:
    """
    Validate that target_path does NOT resolve into any protected production,
    evidence epoch, or source repository path.
    Also validates that target_path does not escape staging_root if provided.
    """
    if not target_path:
        raise ProtectedPathViolationError(f"Empty {context} provided.")

    norm = canonicalize_path(target_path)
    norm_lower = norm.lower()

    # 1. Check direct protected roots (production and preserved evidence epochs)
    for prot in PROTECTED_ROOTS:
        if is_subpath_or_equal(norm, prot):
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: {context} '{target_path}' resolves inside protected root '{prot}'"
            )

    # 2. Check for quiescent evidence backup keyword in components
    parts = norm.replace("\\", "/").split("/")
    for p in parts:
        if p.startswith("_quiescent_evidence_backup"):
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: {context} '{target_path}' resolves into preserved evidence epoch!"
            )

    # 3. Reject staging paths placed inside the source Git checkout (dsl-inc-i)
    if not allow_source_repo and is_subpath_or_equal(norm, SOURCE_REPO_ROOT):
        raise ProtectedPathViolationError(
            f"SAFETY VIOLATION: {context} '{target_path}' is inside source repository checkout '{SOURCE_REPO_ROOT}'"
        )

    # 4. If staging_root is specified, verify target_path does not escape staging_root
    if staging_root:
        staging_norm = canonicalize_path(staging_root)
        if not is_subpath_or_equal(norm, staging_norm):
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: {context} '{target_path}' escapes staging root '{staging_root}'"
            )

    return norm


# =============================================================================
# Staging Workspace Marker Protocol
# =============================================================================

STAGING_MARKER_FILENAME = "staging_marker.json"


def init_staging_workspace(
    staging_root: str,
    dataset_identity: str,
    baseline_manifest: Optional[dict] = None,
) -> dict:
    """Write verified staging workspace marker inside isolated staging root."""
    assert_safe_path(staging_root, context="init staging_root")
    os.makedirs(staging_root, exist_ok=True)
    marker_path = os.path.join(staging_root, STAGING_MARKER_FILENAME)

    marker_data = {
        "staging_mode": True,
        "staging_root": canonicalize_path(staging_root),
        "dataset_identity": dataset_identity,
        "baseline_manifest": baseline_manifest or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker_data, f, ensure_ascii=False, indent=2, sort_keys=True)

    return marker_data


def verify_staging_workspace(
    staging_root: str,
    expected_dataset_identity: Optional[str] = None,
) -> dict:
    """Verify that staging_root contains a valid staging workspace marker."""
    assert_safe_path(staging_root, context="verify staging_root")
    marker_path = os.path.join(staging_root, STAGING_MARKER_FILENAME)

    if not os.path.exists(marker_path):
        raise PreconditionViolationError(
            f"Missing staging workspace marker '{STAGING_MARKER_FILENAME}' in '{staging_root}'."
        )

    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise PreconditionViolationError(f"Forged or malformed staging marker: {exc}")

    if not isinstance(data, dict) or not data.get("staging_mode"):
        raise PreconditionViolationError("Invalid staging workspace marker: staging_mode not True")

    recorded_root = data.get("staging_root")
    current_root = canonicalize_path(staging_root)
    if not recorded_root or canonicalize_path(recorded_root) != current_root:
        raise PreconditionViolationError("Forged or mismatched staging root in marker")

    if expected_dataset_identity:
        if data.get("dataset_identity") != expected_dataset_identity:
            raise PreconditionViolationError(
                f"Mismatched dataset identity: expected '{expected_dataset_identity}', got '{data.get('dataset_identity')}'"
            )

    return data


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


def serialize_record(record: Any) -> str:
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


def write_json_atomic(
    path: str,
    data: Any,
    indent: int = 2,
    staging_root: Optional[str] = None,
) -> None:
    """Single-file atomic replacement with flush, sync and os.replace."""
    assert_safe_path(path, context="atomic write target", staging_root=staging_root)
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


def restore_from_archive_atomic(
    archive_path: str,
    target_path: str,
    expected_hash: str,
    staging_root: Optional[str] = None,
) -> None:
    """Atomically restore target_path from archive_path and verify post-replacement hash."""
    assert_safe_path(target_path, context="atomic recovery target", staging_root=staging_root)
    if not os.path.exists(archive_path):
        raise RecoveryStateError(f"Forensic archive missing: {archive_path}")
    if sha256_file(archive_path) != expected_hash:
        raise RecoveryStateError(f"Forensic archive hash mismatch: {archive_path}")

    dirname = os.path.dirname(os.path.abspath(target_path))
    fd, tmp_path = tempfile.mkstemp(dir=dirname, prefix=".tmp_recover_", suffix=".json")
    try:
        with open(archive_path, "rb") as src, os.fdopen(fd, "wb") as dst:
            while chunk := src.read(65536):
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if sha256_file(target_path) != expected_hash:
        raise RecoveryStateError("Post-recovery replacement hash verification failed!")


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
def acquire_staging_lock(data_dir: str, staging_root: Optional[str] = None):
    """
    Acquire cross-process exclusive OS lock on .refresh.lock inside isolated data_dir.
    """
    assert_safe_path(data_dir, context="staging data_dir lock", staging_root=staging_root)
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



def group_attempts(entries: List[dict]) -> List[List[dict]]:
    """
    Partition journal entries into discrete attempts.
    An attempt starts with PREPARED where action != 'ROLLED_BACK_TO_RAW_ARCHIVE'.
    A rollback entry (PREPARED with action == 'ROLLED_BACK_TO_RAW_ARCHIVE')
    belongs to the attempt that declared COMMIT_INTENT_DURABLE.
    """
    attempts: List[List[dict]] = []
    current_attempt: List[dict] = []

    for entry in entries:
        st = entry.get("state")
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        action = payload.get("action")

        if st == "PREPARED" and action != "ROLLED_BACK_TO_RAW_ARCHIVE":
            if current_attempt:
                attempts.append(current_attempt)
            current_attempt = [entry]
        else:
            if not current_attempt:
                current_attempt = [entry]
            else:
                current_attempt.append(entry)

    if current_attempt:
        attempts.append(current_attempt)

    return attempts


class StagingJournal:
    """
    Append-only disposition journal. Lazy filesystem initialization ensures
    inspect operations remain strictly read-only.
    """

    def __init__(self, journal_file: str, writable: bool = False, staging_root: Optional[str] = None):
        self.journal_file = assert_safe_path(journal_file, context="journal_file", staging_root=staging_root)
        self.staging_root = staging_root
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
                    data = json.loads(line)
                    entries.append(data)
                except Exception as exc:
                    entries.append({"state": "CORRUPTED_LINE", "line_no": line_no, "raw": line, "error": str(exc)})
        return entries

    def validate_chain(
        self,
        entries: List[dict],
        expected_migration_id: Optional[str] = None,
        expected_hashes: Optional[dict] = None,
    ) -> None:
        """Enforce strict journal transition consistency, identity binding, and ordering across attempts."""
        if not entries:
            return

        # 1. Immediate scan for corrupted JSON lines
        for entry in entries:
            if entry.get("state") == "CORRUPTED_LINE":
                raise RecoveryStateError(f"Corrupted journal line detected: line {entry.get('line_no')}")

        # 2. First entry of journal must be non-rollback PREPARED
        first_payload = entries[0].get("payload") if isinstance(entries[0].get("payload"), dict) else {}
        if entries[0].get("state") != "PREPARED" or first_payload.get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE":
            raise RecoveryStateError(f"Journal must begin with PREPARED, got '{entries[0].get('state')}'")

        # 3. Validate global cross-entry invariants
        first_mig_id = None
        seen_dataset_identity = None
        first_intent_hashes = None
        completion_count = 0
        required_intent_fields = [
            "source_file_sha256",
            "projected_file_sha256",
            "candidate_record_sha256",
            "record_a_sha256",
            "record_b_sha256",
        ]

        for idx, entry in enumerate(entries):
            st = entry.get("state")
            if st not in JOURNAL_STATES:
                raise RecoveryStateError(f"Unknown state '{st}' in journal")

            payload = entry.get("payload")
            if not isinstance(payload, dict):
                raise RecoveryStateError(f"Entry {idx} payload must be a JSON object")

            # Validate migration_id
            mig_id = payload.get("migration_id")
            if not mig_id or not isinstance(mig_id, str) or not mig_id.strip():
                raise RecoveryStateError(f"Entry {idx} '{st}' is missing required non-empty migration_id")

            if first_mig_id is None:
                first_mig_id = mig_id
            elif mig_id != first_mig_id:
                raise RecoveryStateError(f"Conflicting migration_id in journal: '{mig_id}' != '{first_mig_id}'")

            if expected_migration_id and mig_id != expected_migration_id:
                raise RecoveryStateError(f"Mismatched migration ID: expected '{expected_migration_id}', got '{mig_id}'")

            # Validate dataset_identity consistency
            ds_id = payload.get("dataset_identity")
            if ds_id:
                if seen_dataset_identity is None:
                    seen_dataset_identity = ds_id
                elif ds_id != seen_dataset_identity:
                    raise RecoveryStateError(f"Mismatched dataset identity across entries: '{ds_id}' != '{seen_dataset_identity}'")

            # Track COMPLETION_RECORDED
            if st == "COMPLETION_RECORDED":
                completion_count += 1
                if completion_count > 1:
                    raise RecoveryStateError("Duplicate COMPLETION_RECORDED entry in journal")

            # Check intent hash consistency across all COMMIT_INTENT_DURABLE entries
            if st == "COMMIT_INTENT_DURABLE":
                for f_name in required_intent_fields:
                    val = payload.get(f_name)
                    if not val or not isinstance(val, str) or not val.strip():
                        raise RecoveryStateError(f"Missing required intent hash field '{f_name}' in COMMIT_INTENT_DURABLE")

                if first_intent_hashes is None:
                    first_intent_hashes = {k: payload.get(k) for k in required_intent_fields}
                else:
                    for k in required_intent_fields:
                        if payload.get(k) != first_intent_hashes[k]:
                            raise RecoveryStateError(
                                f"Conflicting {k} in subsequent COMMIT_INTENT_DURABLE: '{payload.get(k)}' != '{first_intent_hashes[k]}'"
                            )

                if expected_hashes:
                    for k in required_intent_fields:
                        if k in expected_hashes and payload.get(k) != expected_hashes[k]:
                            raise RecoveryStateError(
                                f"Intent {k} mismatch against expected hashes: '{payload.get(k)}' != '{expected_hashes[k]}'"
                            )

        # 4. Partition into discrete attempts and validate attempt progression
        attempts = group_attempts(entries)
        expected_order = JOURNAL_STATES

        for att_idx, attempt_entries in enumerate(attempts):
            if not attempt_entries or attempt_entries[0].get("state") != "PREPARED":
                raise RecoveryStateError(f"Attempt {att_idx} must begin with PREPARED")

            att_first_payload = attempt_entries[0].get("payload") if isinstance(attempt_entries[0].get("payload"), dict) else {}
            if att_first_payload.get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE":
                raise RecoveryStateError(f"Attempt {att_idx} cannot begin with rollback action")

            prep_source_hash = None
            pre_store_hashes = att_first_payload.get("pre_store_hashes")
            if isinstance(pre_store_hashes, dict):
                prep_source_hash = pre_store_hashes.get("daily_matches.json")

            rec_a_hash = None
            rec_b_hash = None
            intent_projected_hash = None
            last_index = 0
            has_commit_intent = False
            has_rollback = False

            for idx, entry in enumerate(attempt_entries[1:], 1):
                st = entry.get("state")
                payload = entry.get("payload")
                prev_st = attempt_entries[idx - 1].get("state")

                # Handle rollback within attempt
                if st == "PREPARED" and payload.get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE":
                    if prev_st != "COMMIT_INTENT_DURABLE":
                        raise RecoveryStateError(
                            f"Invalid rollback reset to PREPARED: previous state was '{prev_st}', expected 'COMMIT_INTENT_DURABLE'"
                        )
                    if idx != len(attempt_entries) - 1:
                        raise RecoveryStateError(
                            f"Rollback to PREPARED must be terminal in attempt {att_idx}"
                        )
                    has_rollback = True
                    continue

                if st == "PREPARED":
                    raise RecoveryStateError("Duplicate PREPARED inside attempt")

                state_idx = expected_order.index(st)

                if state_idx < last_index:
                    raise RecoveryStateError(
                        f"Journal state regression detected: '{st}' after '{prev_st}'"
                    )
                if state_idx == last_index:
                    raise RecoveryStateError(
                        f"Journal duplicate state transition detected: '{st}' after '{prev_st}'"
                    )
                if state_idx > last_index + 1:
                    raise RecoveryStateError(
                        f"Journal state skipped intermediate state: expected '{expected_order[last_index + 1]}', got '{st}'"
                    )

                last_index = state_idx

                if st == "EVIDENCE_VERIFIED":
                    rec_a_hash = payload.get("rec_a_hash")
                    rec_b_hash = payload.get("rec_b_hash")

                elif st == "COMMIT_INTENT_DURABLE":
                    has_commit_intent = True
                    intent_projected_hash = payload.get("projected_file_sha256")

                    if prep_source_hash and payload.get("source_file_sha256") != prep_source_hash:
                        raise RecoveryStateError(
                            f"Intent source_file_sha256 mismatch with PREPARED baseline: "
                            f"'{payload.get('source_file_sha256')}' != '{prep_source_hash}'"
                        )
                    if rec_a_hash and payload.get("record_a_sha256") != rec_a_hash:
                        raise RecoveryStateError(
                            f"Intent record_a_sha256 mismatch with EVIDENCE_VERIFIED: "
                            f"'{payload.get('record_a_sha256')}' != '{rec_a_hash}'"
                        )
                    if rec_b_hash and payload.get("record_b_sha256") != rec_b_hash:
                        raise RecoveryStateError(
                            f"Intent record_b_sha256 mismatch with EVIDENCE_VERIFIED: "
                            f"'{payload.get('record_b_sha256')}' != '{rec_b_hash}'"
                        )

                elif st == "PROJECTION_COMMITTED":
                    daily_h = payload.get("daily_matches_sha256")
                    if not daily_h or not isinstance(daily_h, str) or not daily_h.strip():
                        raise RecoveryStateError("Missing daily_matches_sha256 in PROJECTION_COMMITTED")
                    if intent_projected_hash and daily_h != intent_projected_hash:
                        raise RecoveryStateError(
                            f"Mismatched projected file hash in PROJECTION_COMMITTED: '{daily_h}' != '{intent_projected_hash}'"
                        )

            # Enforce non-final attempt boundary constraints
            if att_idx < len(attempts) - 1:
                if last_index >= expected_order.index("PROJECTION_COMMITTED"):
                    raise RecoveryStateError(
                        f"Attempt {att_idx} committed projection; subsequent retry attempt is illegal"
                    )
                if has_commit_intent and not has_rollback:
                    raise RecoveryStateError(
                        f"Attempt {att_idx} declared COMMIT_INTENT_DURABLE without rollback; unrecovered crash cannot start new attempt"
                    )

        # If COMPLETION_RECORDED is present, ensure it is the last entry of the final attempt
        if completion_count == 1:
            if entries[-1].get("state") != "COMPLETION_RECORDED":
                raise RecoveryStateError("COMPLETION_RECORDED must be the final entry of the journal")


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

    def __init__(self, staging_root: str, writable: bool = True):
        self.staging_root = assert_safe_path(staging_root, context="staging_root")
        self.input_dir = os.path.join(self.staging_root, "input", "data")
        self.working_dir = os.path.join(self.staging_root, "working", "data")
        self.archive_dir = os.path.join(self.staging_root, "archive")
        self.journal_dir = os.path.join(self.staging_root, "journal")
        self.output_dir = os.path.join(self.staging_root, "output")

        self.journal = StagingJournal(
            os.path.join(self.journal_dir, "disposition_journal.jsonl"),
            writable=writable,
            staging_root=self.staging_root,
        )

    def get_store_hashes(self, data_dir: str) -> Dict[str, str]:
        assert_safe_path(data_dir, context="data_dir for store hashes", staging_root=self.staging_root)
        hashes = {}
        for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
            path = os.path.join(data_dir, store)
            if os.path.exists(path):
                hashes[store] = sha256_file(path)
            else:
                hashes[store] = "MISSING"
        return hashes

    @staticmethod
    def inspect_dataset(data_dir: str) -> dict:
        """Strictly read-only inspection of dataset. Zero directories or files created."""
        assert_safe_path(data_dir, context="inspection data_dir")

        daily_file = os.path.join(data_dir, "daily_matches.json")
        if not os.path.exists(daily_file):
            raise PreconditionViolationError(f"daily_matches.json not found in {data_dir}")

        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])
        total_matches = len(matches)

        id_counts: Dict[str, int] = {}
        target_indices: List[int] = []

        for idx, m in enumerate(matches):
            mid = m.get("id")
            if not mid or not isinstance(mid, str):
                id_counts["__INVALID__"] = id_counts.get("__INVALID__", 0) + 1
            else:
                id_counts[mid] = id_counts.get(mid, 0) + 1
                if mid == TARGET_CANONICAL_ID:
                    target_indices.append(idx)

        duplicate_ids = {k: v for k, v in id_counts.items() if v > 1}
        unique_ids_count = len(id_counts)

        # Store hashes (read-only)
        store_hashes = {}
        for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
            p = os.path.join(data_dir, store)
            if os.path.exists(p):
                store_hashes[store] = sha256_file(p)
            else:
                store_hashes[store] = "MISSING"

        # Downstream references count
        downstream = {}
        for fn in ["prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json"]:
            p = os.path.join(data_dir, fn)
            if os.path.exists(p):
                d = read_json_strict(p)
                items = d.get("snapshots") or d.get("settlements") or d.get("rows") or []
                if isinstance(items, dict):
                    matches_ref = [v for v in items.values() if v.get("match_id") == TARGET_CANONICAL_ID]
                    downstream[fn] = {"total": len(items), "target_refs": len(matches_ref)}
                elif isinstance(items, list):
                    matches_ref = [v for v in items if v.get("match_id") == TARGET_CANONICAL_ID]
                    downstream[fn] = {"total": len(items), "target_refs": len(matches_ref)}

        calib_file = os.path.join(data_dir, "calibrated.json")
        if os.path.exists(calib_file):
            calib = read_json_strict(calib_file)
            ids_list = calib.get("ids", []) if isinstance(calib, dict) else (calib if isinstance(calib, list) else [])
            downstream["calibrated.json"] = {
                "total": len(ids_list),
                "target_in_calibrated": TARGET_CANONICAL_ID in ids_list,
            }

        team_file = os.path.join(data_dir, "team_strength.json")
        if os.path.exists(team_file):
            ts = read_json_strict(team_file)
            teams_dict = ts.get("teams", {}) if isinstance(ts, dict) and "teams" in ts else ts
            downstream["team_strength.json"] = {"total_teams": len(teams_dict)}

        return {
            "data_dir": data_dir,
            "total_matches": total_matches,
            "unique_ids_count": unique_ids_count,
            "duplicate_ids": duplicate_ids,
            "target_occurrences": len(target_indices),
            "target_indices": target_indices,
            "store_hashes": store_hashes,
            "downstream_references": downstream,
        }

    def dry_run(self) -> dict:
        """Produce candidate projection preview and diff without modifying source files."""
        verify_staging_workspace(self.staging_root)
        assert_safe_path(self.working_dir, context="working_dir dry-run", staging_root=self.staging_root)

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

        write_json_atomic(preview_path, preview_data, staging_root=self.staging_root)
        write_json_atomic(diff_path, diff, staging_root=self.staging_root)

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
        migration_id: str = DEFAULT_MIGRATION_ID,
        dataset_identity: Optional[str] = None,
    ) -> dict:
        """
        Execute migration against isolated staging replica under 7-phase state machine.
        """
        if not simulation:
            raise PreconditionViolationError(
                "staging_apply requires simulation=True marker to confirm staging mode."
            )

        # Verify staging workspace marker and dataset binding
        marker = verify_staging_workspace(self.staging_root)
        bound_identity = dataset_identity or marker.get("dataset_identity") or "EXPECTED_EPOCH_1_FIXTURE"

        if bound_identity == "EXPECTED_EPOCH_1_FIXTURE":
            baseline_manifest = EPOCH_1_BASELINE_MANIFEST
        elif bound_identity == "EXPLICIT_SYNTHETIC_TEST_FIXTURE":
            baseline_manifest = marker.get("baseline_manifest", {})
            if not baseline_manifest:
                raise PreconditionViolationError("Missing baseline_manifest in synthetic test fixture marker")
        else:
            raise PreconditionViolationError(f"Unknown or unbound dataset identity: {bound_identity}")

        assert_safe_path(self.working_dir, context="working_dir staging_apply", staging_root=self.staging_root)

        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        expected_projected_file_hash = baseline_manifest["projected_file_sha256"]
        expected_source_file_hash = baseline_manifest["daily_matches.json"]

        # Acquire staging OS byte lock BEFORE any idempotency or mutation checks
        with acquire_staging_lock(self.working_dir, staging_root=self.staging_root):
            # 0. Rigorous Idempotency Preflight (Inside Exclusive Lock)
            current_daily = read_json_strict(daily_file)
            current_matches = current_daily.get("matches", [])
            current_daily_hash = sha256_file(daily_file)

            if (
                len(current_matches) == 192
                and current_daily_hash == expected_projected_file_hash
            ):
                # Verify complete already-applied invariants via unified verification
                self.verify_completed_dataset(migration_id=migration_id, dataset_identity=bound_identity)

                return {
                    "status": "ALREADY_APPLIED_NO_OP",
                    "total_matches": 192,
                    "target_occurrences": 1,
                    "candidate_sha256": EXPECTED_CANDIDATE_SHA256,
                    "projected_file_sha256": current_daily_hash,
                    "message": "Store is already in post-migration projected state. Verified completed state invariants.",
                }

            if fault_inject == "before_prepare":
                raise FaultInjectionError("Fault injection: before_prepare")

            # Phase 1: PREPARED
            pre_store_hashes = self.get_store_hashes(self.working_dir)
            for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
                if pre_store_hashes[store] == "MISSING":
                    raise PreconditionViolationError(f"Required store missing: {store}")

            # Verify baseline source binding
            if pre_store_hashes["daily_matches.json"] != expected_source_file_hash:
                raise PreconditionViolationError(
                    f"Canonical source hash mismatch: {pre_store_hashes['daily_matches.json']} != {expected_source_file_hash}"
                )

            for store in NON_CANONICAL_STORES:
                exp_h = baseline_manifest.get(store)
                if exp_h and pre_store_hashes[store] != exp_h:
                    raise PreconditionViolationError(
                        f"Source store '{store}' hash mismatch against baseline manifest binding"
                    )

            # Check journal state consistency and handle multi-attempt scoping
            existing_entries = self.journal.read_entries()
            self.journal.validate_chain(existing_entries, expected_migration_id=migration_id)

            attempt_num = 1
            action = "INITIAL_PREPARED"
            if existing_entries:
                attempts = group_attempts(existing_entries)
                last_attempt = attempts[-1]
                last_st = last_attempt[-1].get("state")
                last_act = (last_attempt[-1].get("payload") or {}).get("action")

                if last_st in ("PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED", "COMPLETION_RECORDED"):
                    raise RecoveryStateError("Migration has already committed projection; new attempt cannot be started")
                if last_st == "COMMIT_INTENT_DURABLE":
                    raise RecoveryStateError("Previous attempt declared COMMIT_INTENT_DURABLE without rollback; run staging_recover first")

                prior_attempts = [
                    e.get("payload", {}).get("attempt", 1)
                    for e in existing_entries
                    if isinstance(e.get("payload"), dict)
                ]
                attempt_num = max(prior_attempts, default=1) + 1
                action = "RETRY_PREPARED"

            self.journal.append_entry("PREPARED", {
                "migration_id": migration_id,
                "dataset_identity": bound_identity,
                "working_dir": self.working_dir,
                "pre_store_hashes": pre_store_hashes,
                "attempt": attempt_num,
                "action": action,
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

            if not valid_full_time_score(rec_b.get("score")):
                raise PreconditionViolationError("Record B final score is malformed or invalid")

            self.journal.append_entry("EVIDENCE_VERIFIED", {
                "migration_id": migration_id,
                "attempt": attempt_num,
                "rec_a_hash": rec_a_hash,
                "rec_b_hash": rec_b_hash,
                "target_indices": [idx_a, idx_b],
            })

            if fault_inject == "during_archive":
                raise FaultInjectionError("Fault injection: during_archive")

            # Create or reuse forensic archive
            raw_archive_path = os.path.join(self.archive_dir, "daily_matches_raw.json")
            if os.path.exists(raw_archive_path):
                # Verify existing archive before reusing
                self.verify_forensic_archive(
                    baseline_source_hash=pre_store_hashes["daily_matches.json"],
                    expected_migration_id=migration_id,
                    expected_dataset_identity=bound_identity,
                )
            else:
                os.makedirs(self.archive_dir, exist_ok=True)
                with open(daily_file, "rb") as src, open(raw_archive_path, "wb") as dst:
                    raw_bytes = src.read()
                    dst.write(raw_bytes)
                    dst.flush()
                    os.fsync(dst.fileno())

                write_json_atomic(os.path.join(self.archive_dir, "record_a.json"), rec_a, staging_root=self.staging_root)
                write_json_atomic(os.path.join(self.archive_dir, "record_b.json"), rec_b, staging_root=self.staging_root)

                archive_manifest = {
                    "migration_id": migration_id,
                    "dataset_identity": bound_identity,
                    "source_file_sha256": sha256_file(raw_archive_path),
                    "record_a_sha256": rec_a_hash,
                    "record_b_sha256": rec_b_hash,
                    "record_a_index": idx_a,
                    "record_b_index": idx_b,
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                }
                write_json_atomic(
                    os.path.join(self.archive_dir, "forensic_manifest.json"),
                    archive_manifest,
                    staging_root=self.staging_root,
                )

            if fault_inject == "after_archive_before_verify":
                raise FaultInjectionError("Fault injection: after_archive_before_verify")

            if sha256_file(raw_archive_path) != pre_store_hashes["daily_matches.json"]:
                raise PreconditionViolationError("Forensic archive raw bytes hash mismatch!")

            if fault_inject == "after_archive_verify":
                raise FaultInjectionError("Fault injection: after_archive_verify")

            # Phase 3: AUTHORIZED_FOR_STAGING_SIMULATION
            self.journal.append_entry("AUTHORIZED_FOR_STAGING_SIMULATION", {
                "migration_id": migration_id,
                "attempt": attempt_num,
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

            projected_json_str = json.dumps(projected_data, ensure_ascii=False, indent=2, sort_keys=True)
            if os.name == "nt":
                projected_json_str = projected_json_str.replace("\n", "\r\n")
            projected_json_bytes = projected_json_str.encode("utf-8")
            projected_file_hash = sha256_bytes(projected_json_bytes)

            # Check intent consistency against any prior intent with same migration_id
            for entry in existing_entries:
                if entry.get("state") == "COMMIT_INTENT_DURABLE":
                    prev_p = entry.get("payload", {})
                    if prev_p.get("migration_id") == migration_id:
                        if (
                            prev_p.get("source_file_sha256") != pre_store_hashes["daily_matches.json"]
                            or prev_p.get("projected_file_sha256") != projected_file_hash
                            or prev_p.get("candidate_record_sha256") != cand_hash
                            or prev_p.get("record_a_sha256") != rec_a_hash
                            or prev_p.get("record_b_sha256") != rec_b_hash
                        ):
                            raise PreconditionViolationError(
                                f"Reused migration ID '{migration_id}' with conflicting intent/hashes"
                            )

            if fault_inject == "before_commit_intent":
                raise FaultInjectionError("Fault injection: before_commit_intent")

            # Phase 4: COMMIT_INTENT_DURABLE
            self.journal.append_entry("COMMIT_INTENT_DURABLE", {
                "migration_id": migration_id,
                "attempt": attempt_num,
                "dataset_identity": bound_identity,
                "target_id": TARGET_CANONICAL_ID,
                "source_file_sha256": pre_store_hashes["daily_matches.json"],
                "projected_file_sha256": projected_file_hash,
                "candidate_record_sha256": cand_hash,
                "record_a_sha256": rec_a_hash,
                "record_b_sha256": rec_b_hash,
                "immutable_store_hashes": {k: v for k, v in pre_store_hashes.items() if k != "daily_matches.json"},
                "projected_total": 192,
            })

            if fault_inject == "after_commit_intent":
                raise FaultInjectionError("Fault injection: after_commit_intent")

            if fault_inject == "before_file_replace":
                raise FaultInjectionError("Fault injection: before_file_replace")

            # Verify daily_file has not become stale before write
            if sha256_file(daily_file) != pre_store_hashes["daily_matches.json"]:
                raise PreconditionViolationError("Stale source file: daily_matches.json modified concurrently before commit")

            # Phase 5: PROJECTION_COMMITTED
            write_json_atomic(daily_file, projected_data, staging_root=self.staging_root)

            if fault_inject == "after_replace_before_committed":
                raise FaultInjectionError("Fault injection: after_replace_before_committed")

            self.journal.append_entry("PROJECTION_COMMITTED", {
                "migration_id": migration_id,
                "attempt": attempt_num,
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
                "attempt": attempt_num,
                "post_inspect": post_inspect,
            })

            if fault_inject == "before_completion":
                raise FaultInjectionError("Fault injection: before_completion")

            # Phase 7: COMPLETION_RECORDED
            result_payload = {
                "migration_id": migration_id,
                "attempt": attempt_num,
                "dataset_identity": bound_identity,
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

            os.makedirs(self.output_dir, exist_ok=True)
            result_file = os.path.join(self.output_dir, "staging-apply-result.json")
            write_json_atomic(result_file, result_payload, staging_root=self.staging_root)

            return result_payload

    def verify_forensic_archive(
        self,
        baseline_source_hash: str,
        expected_migration_id: str,
        expected_dataset_identity: Optional[str] = None,
    ) -> None:
        """
        Verify complete integrity of the forensic archive before forward recovery:
        1. Archive directory exists.
        2. daily_matches_raw.json exists and full-file SHA-256 == baseline source hash.
        3. forensic_manifest.json exists and is valid JSON dict.
        4. record_a.json exists and hash_record(rec_a) == EXPECTED_RECORD_A_SHA256 and matches manifest.
        5. record_b.json exists and hash_record(rec_b) == EXPECTED_RECORD_B_SHA256 and matches manifest.
        6. Manifest migration_id == expected_migration_id.
        7. Manifest dataset_identity == expected_dataset_identity (if provided).
        8. Manifest source_file_sha256 == baseline_source_hash.
        """
        if not os.path.isdir(self.archive_dir):
            raise RecoveryStateError(f"Forensic archive directory missing: {self.archive_dir}")

        raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
        if not os.path.exists(raw_archive):
            raise RecoveryStateError("Forensic archive raw file missing: daily_matches_raw.json")

        raw_hash = sha256_file(raw_archive)
        if raw_hash != baseline_source_hash:
            raise RecoveryStateError(
                f"Forensic archive raw hash mismatch: '{raw_hash}' != '{baseline_source_hash}'"
            )

        manifest_path = os.path.join(self.archive_dir, "forensic_manifest.json")
        if not os.path.exists(manifest_path):
            raise RecoveryStateError("Forensic manifest missing: forensic_manifest.json")

        try:
            manifest = read_json_strict(manifest_path)
        except Exception as exc:
            raise RecoveryStateError(f"Forensic manifest corrupted: {exc}")

        if not isinstance(manifest, dict):
            raise RecoveryStateError("Forensic manifest must be a JSON object")

        if manifest.get("migration_id") != expected_migration_id:
            raise RecoveryStateError(
                f"Forensic manifest migration_id mismatch: '{manifest.get('migration_id')}' != '{expected_migration_id}'"
            )

        if expected_dataset_identity and manifest.get("dataset_identity") != expected_dataset_identity:
            raise RecoveryStateError(
                f"Forensic manifest dataset_identity mismatch: '{manifest.get('dataset_identity')}' != '{expected_dataset_identity}'"
            )

        if manifest.get("source_file_sha256") != baseline_source_hash:
            raise RecoveryStateError(
                f"Forensic manifest source_file_sha256 mismatch: '{manifest.get('source_file_sha256')}' != '{baseline_source_hash}'"
            )

        rec_a_path = os.path.join(self.archive_dir, "record_a.json")
        if not os.path.exists(rec_a_path):
            raise RecoveryStateError("Forensic archive record_a.json missing")
        try:
            rec_a = read_json_strict(rec_a_path)
        except Exception as exc:
            raise RecoveryStateError(f"Forensic archive record_a.json corrupted: {exc}")
        rec_a_hash = hash_record(rec_a)
        if rec_a_hash != EXPECTED_RECORD_A_SHA256:
            raise RecoveryStateError(
                f"Forensic archive record_a hash mismatch: '{rec_a_hash}' != '{EXPECTED_RECORD_A_SHA256}'"
            )
        if manifest.get("record_a_sha256") != EXPECTED_RECORD_A_SHA256:
            raise RecoveryStateError("Forensic manifest record_a_sha256 mismatch")

        rec_b_path = os.path.join(self.archive_dir, "record_b.json")
        if not os.path.exists(rec_b_path):
            raise RecoveryStateError("Forensic archive record_b.json missing")
        try:
            rec_b = read_json_strict(rec_b_path)
        except Exception as exc:
            raise RecoveryStateError(f"Forensic archive record_b.json corrupted: {exc}")
        rec_b_hash = hash_record(rec_b)
        if rec_b_hash != EXPECTED_RECORD_B_SHA256:
            raise RecoveryStateError(
                f"Forensic archive record_b hash mismatch: '{rec_b_hash}' != '{EXPECTED_RECORD_B_SHA256}'"
            )
        if manifest.get("record_b_sha256") != EXPECTED_RECORD_B_SHA256:
            raise RecoveryStateError("Forensic manifest record_b_sha256 mismatch")

    def verify_completed_dataset(
        self,
        migration_id: Optional[str] = None,
        dataset_identity: Optional[str] = None,
    ) -> dict:
        """
        Enforce all completed-state invariants across staging-apply (idempotency check),
        staging-verify, and staging-recover (completed state check).
        """
        marker = verify_staging_workspace(self.staging_root)
        bound_identity = dataset_identity or marker.get("dataset_identity") or "EXPECTED_EPOCH_1_FIXTURE"
        if bound_identity == "EXPECTED_EPOCH_1_FIXTURE":
            baseline_manifest = EPOCH_1_BASELINE_MANIFEST
        elif bound_identity == "EXPLICIT_SYNTHETIC_TEST_FIXTURE":
            baseline_manifest = marker.get("baseline_manifest", {})
            if not baseline_manifest:
                raise PreconditionViolationError("Missing baseline_manifest in synthetic test fixture marker")
        else:
            raise PreconditionViolationError(f"Unknown or unbound dataset identity: {bound_identity}")

        expected_source_file_hash = baseline_manifest["daily_matches.json"]
        expected_projected_file_hash = baseline_manifest["projected_file_sha256"]

        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        if not os.path.exists(daily_file):
            raise RecoveryStateError("Canonical active file missing: daily_matches.json")

        actual_projected_hash = sha256_file(daily_file)
        if actual_projected_hash != expected_projected_file_hash:
            raise RecoveryStateError(
                f"Active file hash does not match expected projected hash: '{actual_projected_hash}' != '{expected_projected_file_hash}'"
            )

        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])
        if len(matches) != 192:
            raise RecoveryStateError(f"Expected 192 matches in completed dataset, got {len(matches)}")

        all_ids = [m.get("id") for m in matches]
        counts = Counter(all_ids)
        dup_ids = [k for k, v in counts.items() if v > 1]
        if dup_ids:
            raise RecoveryStateError(f"Duplicate IDs detected in completed dataset: {dup_ids}")
        if len(set(all_ids)) != 192:
            raise RecoveryStateError(f"Unique IDs count is not 192: {len(set(all_ids))}")

        target_records = [m for m in matches if m.get("id") == TARGET_CANONICAL_ID]
        if len(target_records) != 1:
            raise RecoveryStateError(f"Expected exactly 1 target match in completed dataset, got {len(target_records)}")

        target_rec = target_records[0]
        cand_hash = hash_record(target_rec)
        if cand_hash != EXPECTED_CANDIDATE_SHA256:
            raise RecoveryStateError(f"Candidate record hash mismatch in completed dataset: '{cand_hash}' != '{EXPECTED_CANDIDATE_SHA256}'")

        if not valid_full_time_score(target_rec.get("score")):
            raise RecoveryStateError("Invalid full time score in candidate record in completed dataset")

        # Check 6 non-Canonical stores unchanged against baseline manifest
        store_hashes = self.get_store_hashes(self.working_dir)
        for store in NON_CANONICAL_STORES:
            exp_h = baseline_manifest.get(store)
            act_h = store_hashes.get(store)
            if exp_h and act_h != exp_h:
                raise ImmutableStoreMutationError(
                    f"Immutable store '{store}' mutated: '{act_h}' != '{exp_h}'"
                )

        # Check journal
        j_entries = self.journal.read_entries()
        self.journal.validate_chain(j_entries, expected_migration_id=migration_id)
        if self.journal.get_latest_valid_state() != "COMPLETION_RECORDED":
            raise RecoveryStateError("Journal latest valid state is not COMPLETION_RECORDED")
        completions = [e for e in j_entries if e.get("state") == "COMPLETION_RECORDED"]
        if len(completions) != 1:
            raise RecoveryStateError(f"Expected exactly 1 COMPLETION_RECORDED entry, got {len(completions)}")

        # Check complete forensic archive integrity (K-ARC-01..08)
        resolved_migration_id = migration_id or completions[0].get("payload", {}).get("migration_id") or DEFAULT_MIGRATION_ID
        self.verify_forensic_archive(
            baseline_source_hash=expected_source_file_hash,
            expected_migration_id=resolved_migration_id,
            expected_dataset_identity=bound_identity,
        )

        # Check unrelated Canonical records match raw archive byte-for-byte in serialized form
        raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
        raw_data = read_json_strict(raw_archive)
        raw_unrelated = [m for m in raw_data.get("matches", []) if m.get("id") != TARGET_CANONICAL_ID]
        curr_unrelated = [m for m in matches if m.get("id") != TARGET_CANONICAL_ID]
        if serialize_record(raw_unrelated) != serialize_record(curr_unrelated):
            raise RecoveryStateError("Unrelated Canonical records altered in projected state")

        inspect_report = self.inspect_dataset(self.working_dir)
        return inspect_report

    def staging_verify(self, raise_on_error: bool = False) -> dict:
        """
        Verify post-migration staging dataset against the full completed-state specification.
        """
        try:
            marker = verify_staging_workspace(self.staging_root)
            bound_identity = marker.get("dataset_identity")
            dataset_report = self.verify_completed_dataset(dataset_identity=bound_identity)
            return {
                "verified": True,
                "projected_hash_match": True,
                "target_match_ok": True,
                "stores_ok": True,
                "journal_ok": True,
                "archive_ok": True,
                "dataset_report": dataset_report,
            }
        except Exception as exc:
            if raise_on_error:
                raise
            return {
                "verified": False,
                "projected_hash_match": False,
                "target_match_ok": False,
                "stores_ok": False,
                "journal_ok": False,
                "archive_ok": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }


    def staging_recover(self) -> dict:
        """
        Evaluate journal and filesystem state under a single lock without recursive locking.
        Fails closed on unknown state or corrupted journal.
        """
        verify_staging_workspace(self.staging_root)
        assert_safe_path(self.working_dir, context="working_dir staging_recover", staging_root=self.staging_root)

        with acquire_staging_lock(self.working_dir, staging_root=self.staging_root):
            entries = self.journal.read_entries()
            if not entries:
                return {"status": "NO_JOURNAL_ENTRIES", "action": "NOTHING_TO_RECOVER"}

            self.journal.validate_chain(entries)

            latest_state = self.journal.get_latest_valid_state()
            daily_file = os.path.join(self.working_dir, "daily_matches.json")
            current_daily_hash = sha256_file(daily_file) if os.path.exists(daily_file) else "MISSING"

            raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
            raw_archive_exists = os.path.exists(raw_archive)

            # Check immutable stores against PREPARED entry
            prep_entry = next((e for e in entries if e.get("state") == "PREPARED"), None)
            baseline_source_hash = None
            bound_identity = None
            if prep_entry:
                pre_hashes = prep_entry.get("payload", {}).get("pre_store_hashes", {})
                baseline_source_hash = pre_hashes.get("daily_matches.json")
                bound_identity = prep_entry.get("payload", {}).get("dataset_identity")
                current_store_hashes = self.get_store_hashes(self.working_dir)
                for s in NON_CANONICAL_STORES:
                    if s in pre_hashes and current_store_hashes.get(s) != pre_hashes[s]:
                        raise ImmutableStoreMutationError(
                            f"CRITICAL: Immutable store {s} mutated prior to recovery!"
                        )

            # Extract intended projected file hash if COMMIT_INTENT_DURABLE reached
            intent_entry = next((e for e in reversed(entries) if e.get("state") == "COMMIT_INTENT_DURABLE"), None)
            intended_projected_hash = intent_entry["payload"].get("projected_file_sha256") if intent_entry else None
            intent_mig_id = intent_entry["payload"].get("migration_id") if intent_entry else (prep_entry.get("payload", {}).get("migration_id") if prep_entry else DEFAULT_MIGRATION_ID)

            # Classify Canonical File State into: EXPECTED_BASELINE, EXPECTED_PROJECTED, UNKNOWN
            if baseline_source_hash and current_daily_hash == baseline_source_hash:
                file_state = "EXPECTED_BASELINE"
            elif intended_projected_hash and current_daily_hash == intended_projected_hash:
                file_state = "EXPECTED_PROJECTED"
            else:
                file_state = "UNKNOWN"

            # Fail closed on UNKNOWN state!
            if file_state == "UNKNOWN":
                raise RecoveryStateError(
                    f"Recovery failed: active Canonical file hash '{current_daily_hash}' is in an UNKNOWN state. No write permitted."
                )

            # If file is EXPECTED_PROJECTED: complete forward under current lock
            if file_state == "EXPECTED_PROJECTED":
                # J-02: Mandatory forensic archive verification before forward recovery
                self.verify_forensic_archive(
                    baseline_source_hash=baseline_source_hash,
                    expected_migration_id=intent_mig_id,
                    expected_dataset_identity=bound_identity,
                )

                if latest_state == "COMPLETION_RECORDED":
                    self.verify_completed_dataset(migration_id=intent_mig_id, dataset_identity=bound_identity)
                    return {
                        "status": "ALREADY_COMPLETED_NO_OP",
                        "action": "ALREADY_COMPLETED",
                        "latest_state": "COMPLETION_RECORDED",
                        "migration_id": intent_mig_id,
                    }

                if latest_state == "POSTCONDITIONS_VERIFIED":
                    result_payload = {
                        "migration_id": intent_mig_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "RECORDED_COMPLETION",
                        "after_total": 192,
                        "unique_ids": 192,
                        "projection_status": "PROPOSED_NOT_APPROVED",
                    }
                    self.journal.append_entry("COMPLETION_RECORDED", result_payload)
                    return result_payload

                if latest_state == "PROJECTION_COMMITTED":
                    post_inspect = self.inspect_dataset(self.working_dir)
                    if post_inspect["total_matches"] != 192 or post_inspect["unique_ids_count"] != 192 or post_inspect["target_occurrences"] != 1:
                        raise RecoveryStateError("Postconditions invalid after projection replacement")

                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                        "migration_id": intent_mig_id,
                        "post_inspect": post_inspect,
                    })

                    result_payload = {
                        "migration_id": intent_mig_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "VERIFIED_POSTCONDITIONS_AND_RECORDED_COMPLETION",
                        "after_total": 192,
                        "unique_ids": 192,
                        "projection_status": "PROPOSED_NOT_APPROVED",
                    }
                    self.journal.append_entry("COMPLETION_RECORDED", result_payload)
                    return result_payload

                if latest_state == "COMMIT_INTENT_DURABLE":
                    self.journal.append_entry("PROJECTION_COMMITTED", {
                        "migration_id": intent_mig_id,
                        "daily_matches_sha256": current_daily_hash,
                    })

                    post_inspect = self.inspect_dataset(self.working_dir)
                    if post_inspect["total_matches"] != 192 or post_inspect["unique_ids_count"] != 192 or post_inspect["target_occurrences"] != 1:
                        raise RecoveryStateError("Postconditions invalid after projection replacement")

                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                        "migration_id": intent_mig_id,
                        "post_inspect": post_inspect,
                    })

                    result_payload = {
                        "migration_id": intent_mig_id,
                        "status": "RECOVERED_COMPLETED_FORWARD",
                        "action": "COMMITTED_PROJECTION_AND_RECORDED_COMPLETION",
                        "after_total": 192,
                        "unique_ids": 192,
                        "projection_status": "PROPOSED_NOT_APPROVED",
                    }
                    self.journal.append_entry("COMPLETION_RECORDED", result_payload)
                    return result_payload

                raise RecoveryStateError(
                    f"Unexpected journal state '{latest_state}' when active file is already projected."
                )

            # If file is EXPECTED_BASELINE:
            if file_state == "EXPECTED_BASELINE":
                if latest_state in ("COMPLETION_RECORDED", "PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED"):
                    raise RecoveryStateError(
                        "Contradictory state: journal indicates committed projection but file has baseline hash."
                    )

                if latest_state == "COMMIT_INTENT_DURABLE":
                    # Roll back intent to clean archive state atomically
                    if not raw_archive_exists:
                        raise RecoveryStateError("Missing raw archive to roll back from COMMIT_INTENT_DURABLE")

                    if sha256_file(raw_archive) != baseline_source_hash:
                        raise RecoveryStateError("Raw archive hash corrupted; cannot roll back")

                    restore_from_archive_atomic(
                        raw_archive,
                        daily_file,
                        expected_hash=baseline_source_hash,
                        staging_root=self.staging_root,
                    )
                    self.journal.append_entry("PREPARED", {
                        "migration_id": intent_mig_id,
                        "action": "ROLLED_BACK_TO_RAW_ARCHIVE",
                        "attempt": intent_entry.get("payload", {}).get("attempt", 1) if intent_entry else 1,
                    })
                    return {
                        "status": "RECOVERED_ROLLED_BACK",
                        "action": "RESTORED_ARCHIVE_PRE_COMMIT",
                    }

                # Pre-commit state (PREPARED, EVIDENCE_VERIFIED, AUTHORIZED_FOR_STAGING_SIMULATION)
                return {
                    "status": "RECOVERED_PRE_COMMIT_CLEAN",
                    "action": "SAFE_RESET",
                    "latest_state": latest_state,
                }

            raise RecoveryStateError("Unhandled recovery decision branch")


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
    p_apply.add_argument("--migration-id", default=DEFAULT_MIGRATION_ID, help="Migration execution identifier")
    p_apply.add_argument("--dataset-identity", default=None, help="Bound dataset identity")

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
            report = StagingCanonicalMigrator.inspect_dataset(parsed.data_dir)
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
                migration_id=parsed.migration_id,
                dataset_identity=parsed.dataset_identity,
            )
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0

        elif parsed.subcommand == "staging-verify":
            assert_safe_path(parsed.staging_dir, context="staging-verify staging-dir")
            migrator = StagingCanonicalMigrator(staging_root=parsed.staging_dir, writable=False)
            audit_report = migrator.staging_verify(raise_on_error=False)
            print(json.dumps(audit_report, indent=2, ensure_ascii=False))
            return 0 if audit_report.get("verified") else 1

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
