"""
tools/canonical_snapshot_adapter.py
===================================
Manifest-Bound Canonical Snapshot Adapter for Isolated Staging Remediation.

Phase P1-B-INC-M Implementation:
Provides a dynamic, snapshot-aware Canonical remediation engine capable of operating
on independently identified, frozen, isolated datasets without hardcoding fixed whole-file
hashes or fixed 193-to-192 match totals.

KEY DESIGN PRINCIPLES:
1. Dynamic baseline binding: Operates on N source matches -> (N - 1) projected matches.
2. Strict manifest schema: Validates all 7 stores, record fingerprints, and metadata.
3. Permitted Phase M classifications:
   - PRESERVED_EPOCH1_FIXTURE
   - EXPLICIT_SYNTHETIC_DRIFT_FIXTURE
   (Rejects FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT and unapproved classifications).
4. Owner Decision A field mapping: Retains standard club names and finished score from
   Record B; retains JCZQ number and closing odds from Record A.
5. Byte-level immutability for all 6 non-Canonical stores.
6. Zero unrelated match loss: Preserves all N - 2 unrelated records in exact order.
7. Conservative metadata semantics: Dynamically updates meta.total; preserves meta.last_refresh
   and meta.sources exactly.
8. Full crash-recovery and transaction journaling with attempt scoping.
9. Strict protected-path enforcement: Rejects production root, production data, preserved
   evidence epochs, and source repository checkouts.
10. HARD PROHIBITION: Production execution is strictly NOT AUTHORIZED in Phase M.
    Rejects any --allow-production option immediately.
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

# Re-export and reuse baseline safety primitives and exceptions from staging repair
from tools.staging_canonical_repair import (
    PROD_REPO_ROOT,
    EPOCH_0_ROOT,
    EPOCH_1_ROOT,
    SOURCE_REPO_ROOT,
    PROTECTED_ROOTS,
    NON_CANONICAL_STORES,
    TARGET_CANONICAL_ID,
    EXPECTED_RECORD_A_SHA256,
    EXPECTED_RECORD_B_SHA256,
    EXPECTED_CANDIDATE_SHA256,
    DEFAULT_MIGRATION_ID,
    JOURNAL_STATES,
    StagingMigrationError,
    ProtectedPathViolationError,
    PreconditionViolationError,
    StagingLockError,
    RecoveryStateError,
    ImmutableStoreMutationError,
    FaultInjectionError,
    canonicalize_path,
    is_subpath_or_equal,
    assert_safe_path,
    sha256_bytes,
    sha256_file,
    serialize_record,
    hash_record,
    read_json_strict,
    write_json_atomic,
    restore_from_archive_atomic,
    valid_full_time_score,
    acquire_staging_lock,
    group_attempts,
    StagingJournal,
    init_staging_workspace,
    verify_staging_workspace,
    STAGING_MARKER_FILENAME,
)


# =============================================================================
# Custom Snapshot Adapter Exceptions
# =============================================================================

class SnapshotAdapterError(StagingMigrationError):
    """Base exception for snapshot adapter operations."""
    pass


class CompletedStateVerificationError(SnapshotAdapterError):
    """Raised when completed-state invariants fail verification."""
    pass


class SnapshotManifestError(SnapshotAdapterError):
    """Raised when a snapshot manifest is malformed, missing fields, or fails verification."""
    pass


class SnapshotClassificationError(SnapshotAdapterError):
    """Raised when an unapproved or forged snapshot classification is encountered."""
    pass


class SnapshotIdentityError(SnapshotAdapterError):
    """Raised when snapshot identity or dataset binding fails."""
    pass


class PolicyMismatchError(SnapshotAdapterError):
    """Raised when field mapping policy differs from owner-approved policy."""
    pass


class UnrelatedMatchLossError(SnapshotAdapterError):
    """Raised when any unrelated match record is mutated, dropped, or reordered."""
    pass


# =============================================================================
# Constants and Policy Definitions
# =============================================================================

MANIFEST_SCHEMA_VERSION = "1.0.0"

SNAPSHOT_CLASSIFICATIONS = [
    "PRESERVED_EPOCH1_FIXTURE",
    "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
    "FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT",
]

ALLOWED_CLASSIFICATIONS_PHASE_M = [
    "PRESERVED_EPOCH1_FIXTURE",
    "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
]

FIELD_MAPPING_POLICY_ID = "POLICY-OWNER-DECISION-A-20261011"

FIELD_MAPPING_POLICY_DEFINITION = {
    "policy_id": FIELD_MAPPING_POLICY_ID,
    "target_canonical_id": TARGET_CANONICAL_ID,
    "owner_decision": "APPROVED",
    "owner_decision_scope": "DECISION_A_CANONICAL_FIELD_ATTRIBUTION",
    "rule_attribution": {
        "id": "shared",
        "home": "record_b",
        "away": "record_b",
        "status": "record_b",
        "score": "record_b",
        "jczq_no": "record_a",
        "odds": "record_a",
        "odds_updated_at": "record_a",
    },
    "preserved_context_fields": [
        "sport",
        "date",
        "time",
        "league",
        "round",
        "kickoff_time_known",
        "market_tracked",
    ],
    "approved_record_a_sha256": EXPECTED_RECORD_A_SHA256,
    "approved_record_b_sha256": EXPECTED_RECORD_B_SHA256,
    "approved_candidate_sha256": EXPECTED_CANDIDATE_SHA256,
}

FIELD_MAPPING_POLICY_SHA256 = sha256_bytes(
    json.dumps(FIELD_MAPPING_POLICY_DEFINITION, ensure_ascii=False, sort_keys=True).encode("utf-8")
)


# =============================================================================
# Manifest Schema Validation & Verification
# =============================================================================

MANDATORY_MANIFEST_FIELDS = [
    "manifest_schema_version",
    "snapshot_id",
    "snapshot_classification",
    "snapshot_source_description",
    "dataset_identity",
    "captured_at",
    "capture_consistency_status",
    "source_git_sha",
    "daily_matches_sha256",
    "team_strength_sha256",
    "calibrated_sha256",
    "prediction_snapshots_sha256",
    "odds_snapshots_sha256",
    "settlements_sha256",
    "evaluation_rows_sha256",
    "total_matches",
    "unique_canonical_ids",
    "target_canonical_id",
    "target_record_a_sha256",
    "target_record_b_sha256",
    "field_mapping_policy_id",
    "field_mapping_policy_sha256",
    "manifest_generated_at",
    "manifest_verification_status",
]


def generate_snapshot_manifest(
    data_dir: str,
    snapshot_id: str,
    snapshot_classification: str,
    snapshot_source_description: str,
    dataset_identity: str,
    source_git_sha: Optional[str] = None,
    captured_at: Optional[str] = None,
    capture_consistency_status: Optional[str] = None,
    manifest_verification_status: str = "MANIFEST_GENERATED",
    staging_root: Optional[str] = None,
) -> dict:
    """
    Generate an authoritative snapshot manifest by inspecting a frozen isolated data directory.
    Fails closed if files are missing or safety boundaries are violated.
    Separates MANIFEST_GENERATED from independently verified trust states.
    """
    assert_safe_path(data_dir, context="data_dir for manifest generation", staging_root=staging_root)

    if snapshot_classification not in SNAPSHOT_CLASSIFICATIONS:
        raise SnapshotClassificationError(f"Unknown snapshot classification: '{snapshot_classification}'")

    if snapshot_classification not in ALLOWED_CLASSIFICATIONS_PHASE_M:
        raise SnapshotClassificationError(
            f"Snapshot classification '{snapshot_classification}' is NOT AUTHORIZED in Phase M. "
            f"Allowed classifications: {ALLOWED_CLASSIFICATIONS_PHASE_M}"
        )

    daily_path = os.path.join(data_dir, "daily_matches.json")
    if not os.path.exists(daily_path):
        raise SnapshotManifestError(f"Missing daily_matches.json in {data_dir}")

    daily_data = read_json_strict(daily_path)
    matches = daily_data.get("matches", [])
    total_matches = len(matches)

    all_ids = [m.get("id") for m in matches if isinstance(m, dict)]
    id_counts = Counter(all_ids)
    unique_ids_count = len(id_counts)

    target_matches = [m for m in matches if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
    if len(target_matches) != 2:
        raise SnapshotManifestError(
            f"Expected exactly 2 occurrences of target ID {TARGET_CANONICAL_ID}, found {len(target_matches)}"
        )

    rec_a_hash = hash_record(target_matches[0])
    rec_b_hash = hash_record(target_matches[1])

    store_hashes = {}
    for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
        p = os.path.join(data_dir, store)
        if not os.path.exists(p):
            raise SnapshotManifestError(f"Required store '{store}' missing from {data_dir}")
        store_hashes[store] = sha256_file(p)

    now_iso = datetime.now(timezone.utc).isoformat()

    # Determine captured_at without fabricating or defaulting to current manifest generation time
    if captured_at is not None and str(captured_at).strip():
        actual_captured_at = captured_at
    else:
        evidence_manifest_time = None
        for candidate_path in [
            os.path.join(data_dir, "manifest.json"),
            os.path.join(os.path.dirname(data_dir), "manifest.json"),
        ]:
            if os.path.exists(candidate_path):
                try:
                    ev_data = read_json_strict(candidate_path)
                    if isinstance(ev_data, dict) and "creation_time" in ev_data:
                        evidence_manifest_time = ev_data["creation_time"]
                        break
                except Exception:
                    pass

        if evidence_manifest_time:
            actual_captured_at = evidence_manifest_time
        elif snapshot_classification == "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE":
            actual_captured_at = "SYNTHETIC_FIXTURE_NOT_HISTORICAL"
        else:
            actual_captured_at = "UNKNOWN_HISTORICAL_CAPTURE_TIME"

    # Determine source_git_sha without inventing Git provenance
    if source_git_sha is not None and str(source_git_sha).strip():
        actual_git_sha = source_git_sha
    else:
        actual_git_sha = "UNKNOWN_SOURCE_GIT_SHA"

    # Determine capture_consistency_status without automatically inferring quiescence
    if capture_consistency_status is not None and str(capture_consistency_status).strip():
        actual_consistency = capture_consistency_status
    elif snapshot_classification == "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE":
        actual_consistency = "SYNTHETIC_GENERATED"
    elif snapshot_classification == "PRESERVED_EPOCH1_FIXTURE":
        actual_consistency = "HISTORICAL_EPOCH1_PRESERVED"
    else:
        actual_consistency = "CAPTURE_CONSISTENCY_UNVERIFIED"

    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "snapshot_classification": snapshot_classification,
        "snapshot_source_description": snapshot_source_description,
        "dataset_identity": dataset_identity,
        "captured_at": actual_captured_at,
        "capture_consistency_status": actual_consistency,
        "source_git_sha": actual_git_sha,
        "daily_matches_sha256": store_hashes["daily_matches.json"],
        "team_strength_sha256": store_hashes["team_strength.json"],
        "calibrated_sha256": store_hashes["calibrated.json"],
        "prediction_snapshots_sha256": store_hashes["prediction_snapshots.json"],
        "odds_snapshots_sha256": store_hashes["odds_snapshots.json"],
        "settlements_sha256": store_hashes["settlements.json"],
        "evaluation_rows_sha256": store_hashes["evaluation_rows.json"],
        "total_matches": total_matches,
        "unique_canonical_ids": unique_ids_count,
        "target_canonical_id": TARGET_CANONICAL_ID,
        "target_record_a_sha256": rec_a_hash,
        "target_record_b_sha256": rec_b_hash,
        "field_mapping_policy_id": FIELD_MAPPING_POLICY_ID,
        "field_mapping_policy_sha256": FIELD_MAPPING_POLICY_SHA256,
        "manifest_generated_at": now_iso,
        "manifest_verification_status": manifest_verification_status,
    }
    return manifest


def verify_snapshot_manifest(
    manifest: dict,
    data_dir: str,
    allow_unapproved_classification: bool = False,
    staging_root: Optional[str] = None,
) -> dict:
    """
    Validate snapshot manifest schema, policy bindings, and byte hashes against disk.
    Strictly enforces Phase M classification boundary and byte-level target fingerprints.
    Rejects FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT, LIVE_PRODUCTION_RUNTIME_SNAPSHOT,
    and unknown classifications unconditionally in Phase M.
    """
    assert_safe_path(data_dir, context="data_dir for manifest verification", staging_root=staging_root)

    if not isinstance(manifest, dict):
        raise SnapshotManifestError("Snapshot manifest must be a JSON dictionary")

    # 1. Verify schema completeness
    missing_fields = [f for f in MANDATORY_MANIFEST_FIELDS if f not in manifest]
    if missing_fields:
        raise SnapshotManifestError(f"Snapshot manifest missing required fields: {missing_fields}")

    # 2. Verify schema version
    if manifest["manifest_schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise SnapshotManifestError(
            f"Unsupported manifest_schema_version: {manifest['manifest_schema_version']} != {MANIFEST_SCHEMA_VERSION}"
        )

    # 3. Verify snapshot classification strictly (no bypass in Phase M)
    classification = manifest["snapshot_classification"]
    if classification not in SNAPSHOT_CLASSIFICATIONS:
        raise SnapshotClassificationError(f"Unknown snapshot classification: '{classification}'")

    if classification not in ALLOWED_CLASSIFICATIONS_PHASE_M:
        raise SnapshotClassificationError(
            f"Snapshot classification '{classification}' is NOT AUTHORIZED in Phase M. "
            f"Allowed classifications: {ALLOWED_CLASSIFICATIONS_PHASE_M}"
        )

    # 4. Verify snapshot identity completeness
    if not manifest["snapshot_id"] or not str(manifest["snapshot_id"]).strip():
        raise SnapshotIdentityError("Snapshot manifest has empty snapshot_id")
    if not manifest["dataset_identity"] or not str(manifest["dataset_identity"]).strip():
        raise SnapshotIdentityError("Snapshot manifest has empty dataset_identity")

    # 5. Verify field mapping policy binding
    if manifest["field_mapping_policy_id"] != FIELD_MAPPING_POLICY_ID:
        raise PolicyMismatchError(
            f"Mismatched policy ID: expected '{FIELD_MAPPING_POLICY_ID}', got '{manifest['field_mapping_policy_id']}'"
        )
    if manifest["field_mapping_policy_sha256"] != FIELD_MAPPING_POLICY_SHA256:
        raise PolicyMismatchError(
            f"Mismatched policy SHA-256: expected '{FIELD_MAPPING_POLICY_SHA256}', got '{manifest['field_mapping_policy_sha256']}'"
        )

    # 6. Verify target canonical ID and record fingerprints against actual daily_matches.json bytes
    if manifest["target_canonical_id"] != TARGET_CANONICAL_ID:
        raise PreconditionViolationError(
            f"Mismatched target_canonical_id: {manifest['target_canonical_id']} != {TARGET_CANONICAL_ID}"
        )
    if manifest["target_record_a_sha256"] != EXPECTED_RECORD_A_SHA256:
        raise PreconditionViolationError(
            f"Record A hash mismatch in manifest: {manifest['target_record_a_sha256']} != {EXPECTED_RECORD_A_SHA256}"
        )
    if manifest["target_record_b_sha256"] != EXPECTED_RECORD_B_SHA256:
        raise PreconditionViolationError(
            f"Record B hash mismatch in manifest: {manifest['target_record_b_sha256']} != {EXPECTED_RECORD_B_SHA256}"
        )

    daily_path = os.path.join(data_dir, "daily_matches.json")
    if not os.path.exists(daily_path):
        raise SnapshotManifestError(f"Missing daily_matches.json in {data_dir}")

    daily_data = read_json_strict(daily_path)
    matches = daily_data.get("matches", [])
    if len(matches) != manifest["total_matches"]:
        raise SnapshotManifestError(
            f"daily_matches.json record count mismatch: {len(matches)} != manifest {manifest['total_matches']}"
        )

    all_ids = []
    for m in matches:
        if not isinstance(m, dict):
            raise PreconditionViolationError("Malformed non-dict match record in daily_matches.json")
        mid = m.get("id")
        if not mid or not isinstance(mid, str) or not mid.strip():
            raise PreconditionViolationError("Malformed or empty match ID detected in daily_matches.json")
        all_ids.append(mid)

    counts = Counter(all_ids)
    unrelated_dups = {k: v for k, v in counts.items() if v > 1 and k != TARGET_CANONICAL_ID}
    if unrelated_dups:
        raise PreconditionViolationError(f"Unrelated duplicate IDs detected: {unrelated_dups}")

    unique_ids_count = len(counts)
    if unique_ids_count != manifest["unique_canonical_ids"]:
        raise SnapshotManifestError(
            f"Unique IDs count mismatch: {unique_ids_count} != manifest {manifest['unique_canonical_ids']}"
        )

    target_matches = [(i, m) for i, m in enumerate(matches) if m.get("id") == TARGET_CANONICAL_ID]
    if len(target_matches) != 2:
        if len(target_matches) > 2:
            raise PreconditionViolationError(f"Unexpected third target duplicate found ({len(target_matches)})")
        else:
            raise PreconditionViolationError(
                f"Expected exactly 2 target occurrences, found {len(target_matches)}"
            )

    idx_a, rec_a = target_matches[0]
    idx_b, rec_b = target_matches[1]
    rec_a_hash = hash_record(rec_a)
    rec_b_hash = hash_record(rec_b)

    if rec_a_hash != EXPECTED_RECORD_A_SHA256:
        raise PreconditionViolationError(f"Record A hash mismatch in source daily_matches.json: {rec_a_hash} != {EXPECTED_RECORD_A_SHA256}")
    if rec_b_hash != EXPECTED_RECORD_B_SHA256:
        raise PreconditionViolationError(f"Record B hash mismatch in source daily_matches.json: {rec_b_hash} != {EXPECTED_RECORD_B_SHA256}")

    # 7. Verify all seven stores on disk against manifest SHA-256
    for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
        file_path = os.path.join(data_dir, store)
        if not os.path.exists(file_path):
            raise SnapshotManifestError(f"Required store '{store}' missing from data directory '{data_dir}'")

        actual_hash = sha256_file(file_path)
        manifest_key = f"{store.split('.')[0]}_sha256"
        expected_hash = manifest.get(manifest_key)
        if not expected_hash:
            raise SnapshotManifestError(f"Manifest missing hash key '{manifest_key}'")

        if actual_hash != expected_hash:
            raise SnapshotManifestError(
                f"Store '{store}' hash mismatch against manifest: actual {actual_hash} != manifest {expected_hash}"
            )

    verified_manifest = copy.deepcopy(manifest)
    verified_manifest["manifest_verification_status"] = "VERIFIED"
    return verified_manifest


def hash_manifest(manifest: dict) -> str:
    """
    Compute deterministic SHA-256 identity hash of snapshot manifest.
    Excludes ephemeral verification lifecycle status so identity remains invariant across verification transitions.
    """
    m_copy = {k: v for k, v in manifest.items() if k != "manifest_verification_status"}
    m_str = json.dumps(m_copy, ensure_ascii=False, sort_keys=True)
    return sha256_bytes(m_str.encode("utf-8"))




# =============================================================================
# Candidate Projection and Dataset Transformation
# =============================================================================

def build_approved_candidate_projection(record_a: dict, record_b: dict) -> Tuple[dict, dict]:
    """
    Synthesize candidate record from Record A and Record B according to Owner Decision A:
    - Retain Canonical ID: 500l-2026-10-09-1362422
    - Club identities: Record B standard names ('巴拉纳竞技' vs '米内罗竞技')
    - Terminal status: Record B finished status ('finished')
    - Final score: Record B valid 2-2 score (ft: [2, 2], ht: [1, 1])
    - Competition number: Record A value '4004'
    - Market odds: Record A closing odds (4.75 / 3.40 / 1.60)
    - Odds timestamp: Record A original timestamp
    - Preserves sport, date, time, league, round, kickoff_time_known, market_tracked from Record B.

    Returns: (candidate_record, field_diff)
    """
    rec_a_hash = hash_record(record_a)
    rec_b_hash = hash_record(record_b)

    if rec_a_hash != EXPECTED_RECORD_A_SHA256:
        raise PreconditionViolationError(f"Record A hash mismatch: {rec_a_hash} != {EXPECTED_RECORD_A_SHA256}")
    if rec_b_hash != EXPECTED_RECORD_B_SHA256:
        raise PreconditionViolationError(f"Record B hash mismatch: {rec_b_hash} != {EXPECTED_RECORD_B_SHA256}")

    candidate = copy.deepcopy(record_b)
    candidate["jczq_no"] = record_a.get("jczq_no", "")
    candidate["odds"] = copy.deepcopy(record_a.get("odds"))
    candidate["odds_updated_at"] = record_a.get("odds_updated_at")

    cand_hash = hash_record(candidate)
    if cand_hash != EXPECTED_CANDIDATE_SHA256:
        raise PreconditionViolationError(
            f"Candidate record fingerprint mismatch: {cand_hash} != {EXPECTED_CANDIDATE_SHA256}"
        )

    field_diff = {
        "id": {"source": "shared", "val": candidate["id"]},
        "home": {"source": "record_b", "val": candidate["home"], "orig_a": record_a.get("home")},
        "away": {"source": "record_b", "val": candidate["away"], "orig_a": record_a.get("away")},
        "status": {"source": "record_b", "val": candidate["status"], "orig_a": record_a.get("status")},
        "score": {"source": "record_b", "val": candidate["score"], "orig_a": record_a.get("score")},
        "jczq_no": {"source": "record_a", "val": candidate["jczq_no"], "orig_b": record_b.get("jczq_no")},
        "odds": {"source": "record_a", "val": candidate["odds"], "orig_b": record_b.get("odds")},
        "odds_updated_at": {"source": "record_a", "val": candidate["odds_updated_at"], "orig_b": record_b.get("odds_updated_at")},
        "policy_id": FIELD_MAPPING_POLICY_ID,
        "policy_sha256": FIELD_MAPPING_POLICY_SHA256,
        "owner_decision": "APPROVED",
        "projection_status": "APPROVED_BY_OWNER_DECISION_A",
    }

    return candidate, field_diff


def project_snapshot_dataset(
    daily_data: dict,
    candidate: dict,
    expected_target_occurrences: int = 2,
) -> Tuple[dict, dict]:
    """
    Dynamically projects daily_matches.json from N matches to (N - 1) matches.
    Strictly preserves:
    - All N - 2 unrelated match records in their exact original order and identities.
    - All appended matches.
    - Any valid top-level metadata and extra keys.
    - Updates meta.total to N - 1.
    - Preserves meta.last_refresh and meta.sources exactly.
    """
    matches = daily_data.get("matches", [])
    source_total = len(matches)

    target_indices = [i for i, m in enumerate(matches) if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
    if len(target_indices) != expected_target_occurrences:
        raise PreconditionViolationError(
            f"Expected exactly {expected_target_occurrences} target occurrences, found {len(target_indices)}"
        )

    idx_a, idx_b = target_indices[0], target_indices[1]

    # Partition matches preserving exact order and all unrelated records
    projected_matches = (
        matches[:idx_a]
        + [candidate]
        + matches[idx_a + 1:idx_b]
        + matches[idx_b + 1:]
    )

    projected_total = len(projected_matches)
    if projected_total != source_total - 1:
        raise UnrelatedMatchLossError(
            f"Projected total matches error: {projected_total} != {source_total - 1}"
        )

    # Rigorous invariant verification: ensure zero unrelated record loss
    unrelated_source = [m for i, m in enumerate(matches) if i not in (idx_a, idx_b)]
    unrelated_projected = [m for m in projected_matches if m.get("id") != TARGET_CANONICAL_ID]

    if len(unrelated_source) != len(unrelated_projected):
        raise UnrelatedMatchLossError(
            f"Unrelated match count mismatch: {len(unrelated_source)} != {len(unrelated_projected)}"
        )

    for i, (orig_m, proj_m) in enumerate(zip(unrelated_source, unrelated_projected)):
        if orig_m != proj_m:
            raise UnrelatedMatchLossError(
                f"Unrelated match mutated at relative index {i}: ID '{orig_m.get('id')}' != '{proj_m.get('id')}'"
            )

    # Preserve all top-level keys and conservative metadata semantics
    projected_daily = copy.deepcopy(daily_data)
    projected_daily["matches"] = projected_matches

    if "meta" in projected_daily and isinstance(projected_daily["meta"], dict):
        projected_daily["meta"]["total"] = projected_total
        # Explicitly verify meta.last_refresh and meta.sources preserved
        if "meta" in daily_data and isinstance(daily_data["meta"], dict):
            orig_meta = daily_data["meta"]
            if "last_refresh" in orig_meta:
                projected_daily["meta"]["last_refresh"] = orig_meta["last_refresh"]
            if "sources" in orig_meta:
                projected_daily["meta"]["sources"] = copy.deepcopy(orig_meta["sources"])

    stats = {
        "source_total_matches": source_total,
        "projected_total_matches": projected_total,
        "target_indices": [idx_a, idx_b],
        "unrelated_matches_preserved": len(unrelated_source),
        "unrelated_matches_lost": 0,
    }

    return projected_daily, stats


# =============================================================================
# Core Snapshot Remediation Adapter Engine
# =============================================================================

class SnapshotCanonicalAdapter:
    """
    Manages manifest-bound snapshot adaptation, preview, apply, and recovery.
    """

    def __init__(
        self,
        staging_root: str,
        manifest: dict,
        writable: bool = True,
    ):
        self.staging_root = assert_safe_path(staging_root, context="staging_root")
        self.manifest = manifest
        self.writable = writable
        self.input_dir = os.path.join(self.staging_root, "input", "data")
        self.working_dir = os.path.join(self.staging_root, "working", "data")
        self.archive_dir = os.path.join(self.staging_root, "archive")
        self.journal_dir = os.path.join(self.staging_root, "journal")
        self.output_dir = os.path.join(self.staging_root, "output")

        if self.writable:
            # Writing operations strictly require verified staging workspace marker
            marker_file = os.path.join(self.staging_root, STAGING_MARKER_FILENAME)
            if not os.path.exists(marker_file):
                raise PreconditionViolationError(
                    f"Unmarked workspace rejected before writes: missing '{STAGING_MARKER_FILENAME}' in '{self.staging_root}'"
                )
            verify_staging_workspace(self.staging_root)

        self.journal = StagingJournal(
            os.path.join(self.journal_dir, "snapshot_adaptation_journal.jsonl"),
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

    def inspect_dataset(self, data_dir: str) -> dict:
        """Strictly read-only inspection of dataset under safety boundaries."""
        assert_safe_path(data_dir, context="inspection data_dir", staging_root=self.staging_root)

        daily_file = os.path.join(data_dir, "daily_matches.json")
        if not os.path.exists(daily_file):
            raise PreconditionViolationError(f"daily_matches.json not found in {data_dir}")

        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])
        total_matches = len(matches)

        id_counts: Dict[str, int] = {}
        target_indices: List[int] = []

        for idx, m in enumerate(matches):
            mid = m.get("id") if isinstance(m, dict) else None
            if not mid or not isinstance(mid, str):
                id_counts["__INVALID__"] = id_counts.get("__INVALID__", 0) + 1
            else:
                id_counts[mid] = id_counts.get(mid, 0) + 1
                if mid == TARGET_CANONICAL_ID:
                    target_indices.append(idx)

        duplicate_ids = {k: v for k, v in id_counts.items() if v > 1}
        unique_ids_count = len(id_counts)

        store_hashes = self.get_store_hashes(data_dir)

        # Inspect non-canonical schemas
        downstream = {}
        for fn in ["prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json"]:
            p = os.path.join(data_dir, fn)
            if os.path.exists(p):
                d = read_json_strict(p)
                items = d.get("snapshots") or d.get("settlements") or d.get("rows") or []
                if isinstance(items, dict):
                    matches_ref = [v for v in items.values() if isinstance(v, dict) and v.get("match_id") == TARGET_CANONICAL_ID]
                    downstream[fn] = {"total": len(items), "target_refs": len(matches_ref)}
                elif isinstance(items, list):
                    matches_ref = [v for v in items if isinstance(v, dict) and v.get("match_id") == TARGET_CANONICAL_ID]
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
            downstream["team_strength.json"] = {"total_teams": len(teams_dict) if isinstance(teams_dict, dict) else 0}

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

    def validate_journal_chain(
        self,
        entries: List[dict],
        expected_migration_id: Optional[str] = None,
        expected_snapshot_id: Optional[str] = None,
        expected_manifest_sha256: Optional[str] = None,
        expected_dataset_identity: Optional[str] = None,
    ) -> None:
        """Enforce journal transition consistency, cross-attempt snapshot identity binding, and ordering."""
        self.journal.validate_chain(entries, expected_migration_id=expected_migration_id)
        if not entries:
            return

        first_snap_id = None
        first_manifest_sha256 = None
        first_dataset_id = None
        first_proj_hash = None
        first_immut_hashes = None
        last_attempt = None

        for idx, entry in enumerate(entries):
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}

            # Validate attempt sequence
            att = payload.get("attempt")
            if isinstance(att, int):
                if last_attempt is not None and att < last_attempt:
                    raise RecoveryStateError(
                        f"Non-monotonic attempt sequence: attempt {att} < {last_attempt}"
                    )
                last_attempt = att

            # Validate snapshot_id binding
            snap_id = payload.get("snapshot_id")
            if snap_id:
                if first_snap_id is None:
                    first_snap_id = snap_id
                elif snap_id != first_snap_id:
                    raise RecoveryStateError(
                        f"Cross-snapshot retry rejected: changed snapshot_id '{snap_id}' != '{first_snap_id}'"
                    )
                if expected_snapshot_id and snap_id != expected_snapshot_id:
                    raise RecoveryStateError(
                        f"Journal snapshot_id mismatch: '{snap_id}' != '{expected_snapshot_id}'"
                    )

            # Validate dataset_identity binding
            ds_id = payload.get("dataset_identity")
            if ds_id:
                if first_dataset_id is None:
                    first_dataset_id = ds_id
                elif ds_id != first_dataset_id:
                    raise RecoveryStateError(
                        f"Cross-snapshot retry rejected: changed dataset_identity '{ds_id}' != '{first_dataset_id}'"
                    )
                if expected_dataset_identity and ds_id != expected_dataset_identity:
                    raise RecoveryStateError(
                        f"Journal dataset_identity mismatch: '{ds_id}' != '{expected_dataset_identity}'"
                    )

            # Validate manifest_sha256 binding
            m_sha = payload.get("manifest_sha256")
            if m_sha:
                if first_manifest_sha256 is None:
                    first_manifest_sha256 = m_sha
                elif m_sha != first_manifest_sha256:
                    raise RecoveryStateError(
                        f"Cross-snapshot retry rejected: source manifest changed across attempts: '{m_sha}' != '{first_manifest_sha256}'"
                    )
                if expected_manifest_sha256 and m_sha != expected_manifest_sha256:
                    raise RecoveryStateError(
                        f"Journal manifest_sha256 mismatch: '{m_sha}' != '{expected_manifest_sha256}'"
                    )

            # Validate projected_file_sha256 binding
            proj_h = payload.get("projected_file_sha256")
            if proj_h:
                if first_proj_hash is None:
                    first_proj_hash = proj_h
                elif proj_h != first_proj_hash:
                    raise RecoveryStateError(
                        f"Conflicting projected_file_sha256 in journal: '{proj_h}' != '{first_proj_hash}'"
                    )

            # Validate immutable_store_hashes binding
            immut = payload.get("immutable_store_hashes")
            if isinstance(immut, dict):
                if first_immut_hashes is None:
                    first_immut_hashes = immut
                elif immut != first_immut_hashes:
                    raise RecoveryStateError(
                        "Conflicting immutable_store_hashes in journal across attempts"
                    )

    def verify_completed_dataset(
        self,
        migration_id: Optional[str] = None,
    ) -> dict:
        """
        Shared, strict completed-state verifier.
        Validates all 20 completed-state invariants:
        - Workspace marker
        - Approved snapshot classification
        - Snapshot identity and manifest binding
        - Approved field mapping policy
        - Source archive integrity (raw file, records A & B, forensic manifest)
        - Record A and Record B fingerprints
        - Candidate record fingerprint
        - Expected projected file hash derived from bound source archive and approved policy
        - Actual active projected whole-file hash matches expected
        - Exactly one target occurrence
        - Canonical uniqueness
        - All N - 2 unrelated matches preserved in exact order
        - Metadata preserved (total updated to N - 1; last_refresh and sources unchanged)
        - All six non-Canonical stores unchanged against manifest
        - Journal chain complete and valid, ending in exactly one terminal COMPLETION_RECORDED
        """
        # 1. Verified staging workspace marker
        verify_staging_workspace(self.staging_root)

        # 2. Approved snapshot classification
        classification = self.manifest.get("snapshot_classification")
        if classification not in ALLOWED_CLASSIFICATIONS_PHASE_M:
            raise SnapshotClassificationError(
                f"Snapshot classification '{classification}' is NOT AUTHORIZED in Phase M"
            )

        # 3. Snapshot identity and manifest binding
        snapshot_id = self.manifest.get("snapshot_id")
        dataset_identity = self.manifest.get("dataset_identity")
        if not snapshot_id or not str(snapshot_id).strip():
            raise SnapshotIdentityError("Snapshot manifest has empty snapshot_id")
        if not dataset_identity or not str(dataset_identity).strip():
            raise SnapshotIdentityError("Snapshot manifest has empty dataset_identity")

        manifest_sha256 = hash_manifest(self.manifest)

        # 4. Exact approved field mapping policy
        if self.manifest.get("field_mapping_policy_id") != FIELD_MAPPING_POLICY_ID:
            raise PolicyMismatchError(
                f"Field mapping policy ID mismatch: {self.manifest.get('field_mapping_policy_id')} != {FIELD_MAPPING_POLICY_ID}"
            )
        if self.manifest.get("field_mapping_policy_sha256") != FIELD_MAPPING_POLICY_SHA256:
            raise PolicyMismatchError(
                f"Field mapping policy SHA-256 mismatch: {self.manifest.get('field_mapping_policy_sha256')} != {FIELD_MAPPING_POLICY_SHA256}"
            )

        # 5. Original source archive integrity
        archive_manifest = self.verify_forensic_archive(
            baseline_source_hash=self.manifest.get("daily_matches_sha256"),
            expected_migration_id=migration_id or DEFAULT_MIGRATION_ID,
            expected_snapshot_id=snapshot_id,
        )

        raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
        raw_daily = read_json_strict(raw_archive)
        raw_matches = raw_daily.get("matches", [])
        source_total = self.manifest.get("total_matches", len(raw_matches))

        # 6. Candidate fingerprint & expected projected whole-file hash derived from source archive & policy
        rec_a_data = read_json_strict(os.path.join(self.archive_dir, "record_a.json"))
        rec_b_data = read_json_strict(os.path.join(self.archive_dir, "record_b.json"))
        candidate, diff = build_approved_candidate_projection(rec_a_data, rec_b_data)

        expected_projected_daily, stats = project_snapshot_dataset(raw_daily, candidate)
        proj_str = json.dumps(expected_projected_daily, ensure_ascii=False, indent=2, sort_keys=True)
        if os.name == "nt":
            proj_str = proj_str.replace("\n", "\r\n")
        expected_projected_hash = sha256_bytes(proj_str.encode("utf-8"))

        # 7. Actual active projected whole-file check
        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        if not os.path.exists(daily_file):
            raise CompletedStateVerificationError(f"daily_matches.json missing from {self.working_dir}")

        actual_projected_hash = sha256_file(daily_file)
        if actual_projected_hash != expected_projected_hash:
            raise CompletedStateVerificationError(
                f"Active file hash does not match expected projected hash: actual {actual_projected_hash} != expected {expected_projected_hash}"
            )

        # 8. Active file dataset invariants
        active_daily = read_json_strict(daily_file)
        active_matches = active_daily.get("matches", [])
        expected_projected_total = source_total - 1

        if len(active_matches) != expected_projected_total:
            raise CompletedStateVerificationError(
                f"Projected total matches mismatch: active {len(active_matches)} != expected {expected_projected_total}"
            )

        active_ids = [m.get("id") for m in active_matches if isinstance(m, dict)]
        if len(active_ids) != len(set(active_ids)):
            raise CompletedStateVerificationError("Duplicate canonical IDs detected in active projected file")

        target_matches = [m for m in active_matches if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
        if len(target_matches) != 1:
            raise CompletedStateVerificationError(
                f"Expected exactly 1 target canonical occurrence, found {len(target_matches)}"
            )

        if hash_record(target_matches[0]) != EXPECTED_CANDIDATE_SHA256:
            raise CompletedStateVerificationError("Candidate record fingerprint altered in completed dataset")

        # 9. All unrelated records preserved in exact order
        unrelated_raw = [m for m in raw_matches if m.get("id") != TARGET_CANONICAL_ID]
        unrelated_active = [m for m in active_matches if m.get("id") != TARGET_CANONICAL_ID]
        if len(unrelated_raw) != len(unrelated_active):
            raise UnrelatedMatchLossError(
                f"Unrelated matches count mismatch: {len(unrelated_active)} != {len(unrelated_raw)}"
            )
        for i, (orig_m, act_m) in enumerate(zip(unrelated_raw, unrelated_active)):
            if orig_m != act_m:
                raise UnrelatedMatchLossError(
                    f"Unrelated match altered at index {i}: ID '{orig_m.get('id')}'"
                )

        # 10. Top-level metadata preservation
        if "meta" in raw_daily and isinstance(raw_daily["meta"], dict):
            if "meta" not in active_daily or not isinstance(active_daily["meta"], dict):
                raise CompletedStateVerificationError("meta block missing from active daily_matches.json")
            if active_daily["meta"].get("total") != expected_projected_total:
                raise CompletedStateVerificationError(
                    f"meta.total mismatch: {active_daily['meta'].get('total')} != {expected_projected_total}"
                )
            if "last_refresh" in raw_daily["meta"]:
                if active_daily["meta"].get("last_refresh") != raw_daily["meta"]["last_refresh"]:
                    raise CompletedStateVerificationError("meta.last_refresh was altered")
            if "sources" in raw_daily["meta"]:
                if active_daily["meta"].get("sources") != raw_daily["meta"]["sources"]:
                    raise CompletedStateVerificationError("meta.sources was altered")

        # 11. Six non-Canonical stores unchanged
        current_store_hashes = self.get_store_hashes(self.working_dir)
        for store in NON_CANONICAL_STORES:
            exp_h = self.manifest.get(f"{store.split('.')[0]}_sha256")
            act_h = current_store_hashes.get(store)
            if act_h != exp_h:
                raise ImmutableStoreMutationError(
                    f"Immutable store '{store}' hash mismatch in completed dataset: {act_h} != {exp_h}"
                )

        # 12. Journal chain complete and valid
        entries = self.journal.read_entries()
        if not entries:
            raise CompletedStateVerificationError("Journal is missing or empty")

        self.validate_journal_chain(
            entries,
            expected_migration_id=migration_id,
            expected_snapshot_id=snapshot_id,
            expected_manifest_sha256=manifest_sha256,
            expected_dataset_identity=dataset_identity,
        )

        last_entry = entries[-1]
        if last_entry.get("state") != "COMPLETION_RECORDED":
            raise CompletedStateVerificationError(
                f"Journal final state is '{last_entry.get('state')}', expected COMPLETION_RECORDED"
            )

        completion_count = sum(1 for e in entries if e.get("state") == "COMPLETION_RECORDED")
        if completion_count != 1:
            raise CompletedStateVerificationError(
                f"Expected exactly 1 COMPLETION_RECORDED entry, found {completion_count}"
            )

        last_payload = last_entry.get("payload", {})
        if last_payload.get("migration_id") != (migration_id or DEFAULT_MIGRATION_ID):
            raise CompletedStateVerificationError("Journal migration_id mismatch")
        if last_payload.get("snapshot_id") != snapshot_id:
            raise CompletedStateVerificationError("Journal snapshot_id mismatch")
        if last_payload.get("dataset_identity") != dataset_identity:
            raise CompletedStateVerificationError("Journal dataset_identity mismatch")

        return {
            "status": "COMPLETED_STATE_VERIFIED",
            "verified_invariants": 20,
            "snapshot_id": snapshot_id,
            "dataset_identity": dataset_identity,
            "manifest_sha256": manifest_sha256,
            "projected_file_sha256": actual_projected_hash,
            "expected_projected_file_sha256": expected_projected_hash,
            "candidate_record_sha256": EXPECTED_CANDIDATE_SHA256,
            "source_total_matches": source_total,
            "projected_total_matches": expected_projected_total,
            "unique_ids_count": expected_projected_total,
            "target_occurrences": 1,
            "unrelated_records_preserved": len(unrelated_raw),
            "immutable_stores_preserved": len(NON_CANONICAL_STORES),
            "journal_entries_count": len(entries),
            "verification_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def staging_verify(self) -> dict:
        """Execute real completed-state verification on dataset."""
        return self.verify_completed_dataset()

    def preview_projection(self, write_output: bool = True) -> dict:
        """
        Produce candidate projection preview, field diff, and projected hash without touching source files.
        Writes preview files only to self.output_dir when write_output is True and adapter is writable.
        """
        assert_safe_path(self.working_dir, context="working_dir preview", staging_root=self.staging_root)
        verify_snapshot_manifest(self.manifest, self.working_dir, staging_root=self.staging_root)

        daily_file = os.path.join(self.working_dir, "daily_matches.json")
        daily_data = read_json_strict(daily_file)
        matches = daily_data.get("matches", [])

        target_matches = [(i, m) for i, m in enumerate(matches) if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
        if len(target_matches) != 2:
            raise PreconditionViolationError(
                f"Preview expects exactly 2 target occurrences, found {len(target_matches)}"
            )

        idx_a, rec_a = target_matches[0]
        idx_b, rec_b = target_matches[1]

        candidate, diff = build_approved_candidate_projection(rec_a, rec_b)
        projected_daily, stats = project_snapshot_dataset(daily_data, candidate)

        # Deterministic projected bytes hash calculation
        projected_json_str = json.dumps(projected_daily, ensure_ascii=False, indent=2, sort_keys=True)
        if os.name == "nt":
            projected_json_str = projected_json_str.replace("\n", "\r\n")
        projected_file_sha256 = sha256_bytes(projected_json_str.encode("utf-8"))

        preview_data = {
            "staging_only": True,
            "production_execution_authorized": False,
            "snapshot_id": self.manifest["snapshot_id"],
            "snapshot_classification": self.manifest["snapshot_classification"],
            "source_file_sha256": sha256_file(daily_file),
            "source_total_matches": stats["source_total_matches"],
            "source_unique_ids": stats["source_total_matches"] - 1,
            "target_occurrences": 2,
            "candidate_record": candidate,
            "candidate_record_sha256": hash_record(candidate),
            "projected_file_sha256": projected_file_sha256,
            "projected_total_matches": stats["projected_total_matches"],
            "projected_unique_ids": stats["projected_total_matches"],
            "unrelated_records_preserved": stats["unrelated_matches_preserved"],
            "six_store_hashes_before": {k: v for k, v in self.manifest.items() if k.endswith("_sha256") and k != "daily_matches_sha256"},
            "field_mapping_policy_id": FIELD_MAPPING_POLICY_ID,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if write_output and self.writable:
            os.makedirs(self.output_dir, exist_ok=True)
            preview_path = os.path.join(self.output_dir, "canonical-projection-preview.json")
            diff_path = os.path.join(self.output_dir, "canonical-field-diff.json")
            write_json_atomic(preview_path, preview_data, staging_root=self.staging_root)
            write_json_atomic(diff_path, diff, staging_root=self.staging_root)

        return preview_data

    def verify_forensic_archive(
        self,
        baseline_source_hash: str,
        expected_migration_id: str,
        expected_snapshot_id: str,
        expected_dataset_identity: Optional[str] = None,
    ) -> dict:
        """Verify the cryptographic integrity of archived evidence before reuse or recovery."""
        raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
        rec_a_file = os.path.join(self.archive_dir, "record_a.json")
        rec_b_file = os.path.join(self.archive_dir, "record_b.json")
        manifest_file = os.path.join(self.archive_dir, "forensic_manifest.json")

        for f_path in [raw_archive, rec_a_file, rec_b_file, manifest_file]:
            if not os.path.exists(f_path):
                raise RecoveryStateError(f"Missing required forensic archive file: {f_path}")

        raw_hash = sha256_file(raw_archive)
        if raw_hash != baseline_source_hash:
            raise RecoveryStateError(
                f"Forensic archive raw file hash mismatch: {raw_hash} != expected {baseline_source_hash}"
            )

        rec_a_data = read_json_strict(rec_a_file)
        rec_b_data = read_json_strict(rec_b_file)

        a_hash = hash_record(rec_a_data)
        b_hash = hash_record(rec_b_data)

        if a_hash != EXPECTED_RECORD_A_SHA256:
            raise RecoveryStateError(f"Archived record_a hash corrupted: {a_hash}")
        if b_hash != EXPECTED_RECORD_B_SHA256:
            raise RecoveryStateError(f"Archived record_b hash corrupted: {b_hash}")

        # Find target occurrences in raw archive and verify positions
        raw_daily = read_json_strict(raw_archive)
        raw_matches = raw_daily.get("matches", []) if isinstance(raw_daily, dict) else []
        target_indices = [
            i for i, m in enumerate(raw_matches)
            if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID
        ]
        if len(target_indices) != 2:
            raise RecoveryStateError(
                f"Forensic raw archive contains {len(target_indices)} target occurrences, expected 2"
            )
        actual_idx_a, actual_idx_b = target_indices[0], target_indices[1]

        # Verify that records at those positions match archived records A and B
        if hash_record(raw_matches[actual_idx_a]) != a_hash:
            raise RecoveryStateError("Forensic archive raw match at record_a position does not match record_a")
        if hash_record(raw_matches[actual_idx_b]) != b_hash:
            raise RecoveryStateError("Forensic archive raw match at record_b position does not match record_b")

        # Derive expected dataset identity from bound snapshot transaction if not explicitly passed
        target_dataset_id = expected_dataset_identity or self.manifest.get("dataset_identity")
        if not target_dataset_id or not str(target_dataset_id).strip():
            raise RecoveryStateError("Missing expected_dataset_identity for forensic archive verification")

        archive_manifest = read_json_strict(manifest_file)
        if not isinstance(archive_manifest, dict):
            raise RecoveryStateError("Forensic manifest is not a JSON object")

        # Required fields in forensic manifest - do not silently accept missing fields
        required_fields = [
            "migration_id",
            "snapshot_id",
            "dataset_identity",
            "source_file_sha256",
            "record_a_sha256",
            "record_b_sha256",
            "record_a_index",
            "record_b_index",
        ]
        for field in required_fields:
            if field not in archive_manifest or archive_manifest[field] is None:
                raise RecoveryStateError(f"Forensic manifest missing required field: '{field}'")

        if archive_manifest["migration_id"] != expected_migration_id:
            raise RecoveryStateError(
                f"Forensic manifest migration_id mismatch: '{archive_manifest['migration_id']}' != '{expected_migration_id}'"
            )
        if archive_manifest["snapshot_id"] != expected_snapshot_id:
            raise RecoveryStateError(
                f"Forensic manifest snapshot_id mismatch: '{archive_manifest['snapshot_id']}' != '{expected_snapshot_id}'"
            )
        if archive_manifest["dataset_identity"] != target_dataset_id:
            raise RecoveryStateError(
                f"Forensic manifest dataset_identity mismatch: '{archive_manifest['dataset_identity']}' != '{target_dataset_id}'"
            )
        if archive_manifest["source_file_sha256"] != baseline_source_hash:
            raise RecoveryStateError(
                f"Forensic manifest source_file_sha256 mismatch: '{archive_manifest['source_file_sha256']}' != '{baseline_source_hash}'"
            )
        if archive_manifest["record_a_sha256"] != a_hash:
            raise RecoveryStateError(
                f"Forensic manifest record_a_sha256 mismatch: '{archive_manifest['record_a_sha256']}' != '{a_hash}'"
            )
        if archive_manifest["record_b_sha256"] != b_hash:
            raise RecoveryStateError(
                f"Forensic manifest record_b_sha256 mismatch: '{archive_manifest['record_b_sha256']}' != '{b_hash}'"
            )
        if archive_manifest["record_a_index"] != actual_idx_a:
            raise RecoveryStateError(
                f"Forensic manifest record_a_index mismatch: {archive_manifest['record_a_index']} != {actual_idx_a}"
            )
        if archive_manifest["record_b_index"] != actual_idx_b:
            raise RecoveryStateError(
                f"Forensic manifest record_b_index mismatch: {archive_manifest['record_b_index']} != {actual_idx_b}"
            )

        return archive_manifest

    def staging_apply(
        self,
        simulation: bool = False,
        fault_inject: Optional[str] = None,
        migration_id: str = DEFAULT_MIGRATION_ID,
    ) -> dict:
        """
        Execute snapshot-bound migration under full 7-phase state machine with exclusive OS locking.
        """
        if not simulation:
            raise PreconditionViolationError(
                "staging_apply requires simulation=True marker to confirm staging mode."
            )

        assert_safe_path(self.working_dir, context="working_dir staging_apply", staging_root=self.staging_root)
        source_total = self.manifest.get("total_matches", 193)
        projected_expected_total = source_total - 1
        daily_file = os.path.join(self.working_dir, "daily_matches.json")

        with acquire_staging_lock(self.working_dir, staging_root=self.staging_root):
            # Check for already applied state (MC-02 strict completed-state idempotency)
            if os.path.exists(daily_file):
                current_daily = read_json_strict(daily_file)
                current_matches = current_daily.get("matches", [])
                target_matches = [m for m in current_matches if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
                if len(target_matches) == 1 and len(current_matches) == projected_expected_total:
                    # Require full completed-state verification before returning no-op
                    verified_state = self.verify_completed_dataset(migration_id=migration_id)
                    return {
                        "status": "ALREADY_APPLIED_NO_OP",
                        "total_matches": projected_expected_total,
                        "unique_ids_count": projected_expected_total,
                        "target_occurrences": 1,
                        "candidate_sha256": EXPECTED_CANDIDATE_SHA256,
                        "projected_file_sha256": verified_state["projected_file_sha256"],
                        "message": "Store is already in post-migration projected state. Verified invariants.",
                    }

            # 1. Not already applied: verify complete snapshot manifest against source files
            manifest = verify_snapshot_manifest(self.manifest, self.working_dir, staging_root=self.staging_root)

            snapshot_id = manifest["snapshot_id"]
            dataset_identity = manifest["dataset_identity"]
            expected_source_hash = manifest["daily_matches_sha256"]
            source_total = manifest["total_matches"]
            projected_expected_total = source_total - 1

            self.manifest = manifest
            manifest_sha256 = hash_manifest(manifest)

            if fault_inject == "before_prepare":
                raise FaultInjectionError("Fault injection: before_prepare")

            # Phase 1: PREPARED
            pre_store_hashes = self.get_store_hashes(self.working_dir)
            for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
                if pre_store_hashes[store] == "MISSING":
                    raise PreconditionViolationError(f"Required store missing: {store}")

            if pre_store_hashes["daily_matches.json"] != expected_source_hash:
                raise PreconditionViolationError(
                    f"Canonical source hash mismatch: {pre_store_hashes['daily_matches.json']} != {expected_source_hash}"
                )

            for store in NON_CANONICAL_STORES:
                exp_h = manifest.get(f"{store.split('.')[0]}_sha256")
                if exp_h and pre_store_hashes[store] != exp_h:
                    raise PreconditionViolationError(
                        f"Source store '{store}' hash mismatch against manifest binding"
                    )

            # Check journal state consistency and handle multi-attempt scoping
            existing_entries = self.journal.read_entries()
            self.validate_journal_chain(
                existing_entries,
                expected_migration_id=migration_id,
                expected_snapshot_id=snapshot_id,
                expected_manifest_sha256=manifest_sha256,
            )

            attempt_num = 1
            action = "INITIAL_PREPARED"
            if existing_entries:
                attempts = group_attempts(existing_entries)
                last_attempt = attempts[-1]
                last_st = last_attempt[-1].get("state")
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
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
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
            if len(matches) != source_total:
                raise PreconditionViolationError(f"Expected {source_total} matches, found {len(matches)}")

            all_ids = [m.get("id") for m in matches if isinstance(m, dict)]
            counts = Counter(all_ids)
            unrelated_dups = {k: v for k, v in counts.items() if v > 1 and k != TARGET_CANONICAL_ID}
            if unrelated_dups:
                raise PreconditionViolationError(f"Unrelated duplicate IDs detected: {unrelated_dups}")

            targets = [(i, m) for i, m in enumerate(matches) if isinstance(m, dict) and m.get("id") == TARGET_CANONICAL_ID]
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
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "rec_a_hash": rec_a_hash,
                "rec_b_hash": rec_b_hash,
                "target_indices": [idx_a, idx_b],
                "source_total_matches": source_total,
            })

            if fault_inject == "during_archive":
                raise FaultInjectionError("Fault injection: during_archive")

            # Create or reuse forensic archive
            raw_archive_path = os.path.join(self.archive_dir, "daily_matches_raw.json")
            if os.path.exists(raw_archive_path):
                self.verify_forensic_archive(
                    baseline_source_hash=pre_store_hashes["daily_matches.json"],
                    expected_migration_id=migration_id,
                    expected_snapshot_id=snapshot_id,
                    expected_dataset_identity=dataset_identity,
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
                    "snapshot_id": snapshot_id,
                    "dataset_identity": dataset_identity,
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
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "simulation": True,
                "owner_approval": "APPROVED",
                "field_mapping_policy_id": FIELD_MAPPING_POLICY_ID,
                "field_mapping_policy_sha256": FIELD_MAPPING_POLICY_SHA256,
                "projection_status": "APPROVED_BY_OWNER_DECISION_A",
            })

            # Build candidate projection and full projected dataset
            candidate, diff = build_approved_candidate_projection(rec_a, rec_b)
            cand_hash = hash_record(candidate)

            projected_daily, stats = project_snapshot_dataset(daily_data, candidate)
            if stats["projected_total_matches"] != projected_expected_total:
                raise PreconditionViolationError(
                    f"Projected count error: {stats['projected_total_matches']} != {projected_expected_total}"
                )

            projected_json_str = json.dumps(projected_daily, ensure_ascii=False, indent=2, sort_keys=True)
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
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "target_id": TARGET_CANONICAL_ID,
                "source_file_sha256": pre_store_hashes["daily_matches.json"],
                "projected_file_sha256": projected_file_hash,
                "candidate_record_sha256": cand_hash,
                "record_a_sha256": rec_a_hash,
                "record_b_sha256": rec_b_hash,
                "field_mapping_policy_sha256": FIELD_MAPPING_POLICY_SHA256,
                "immutable_store_hashes": {k: v for k, v in pre_store_hashes.items() if k != "daily_matches.json"},
                "source_total": source_total,
                "projected_total": projected_expected_total,
            })

            if fault_inject == "after_commit_intent":
                raise FaultInjectionError("Fault injection: after_commit_intent")

            if fault_inject == "before_file_replace":
                raise FaultInjectionError("Fault injection: before_file_replace")

            if fault_inject == "mutate_source_before_write":
                with open(daily_file, "a") as f:
                    f.write(" ")

            # Verify daily_file has not become stale before write
            if sha256_file(daily_file) != pre_store_hashes["daily_matches.json"]:
                raise PreconditionViolationError("Stale source file: daily_matches.json modified concurrently before commit")

            # Phase 5: PROJECTION_COMMITTED
            write_json_atomic(daily_file, projected_daily, staging_root=self.staging_root)

            self.journal.append_entry("PROJECTION_COMMITTED", {
                "migration_id": migration_id,
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "daily_matches_sha256": sha256_file(daily_file),
            })

            if fault_inject == "after_file_replace":
                raise FaultInjectionError("Fault injection: after_file_replace")

            if fault_inject == "before_postconditions":
                raise FaultInjectionError("Fault injection: before_postconditions")

            # Phase 6: POSTCONDITIONS_VERIFIED
            post_inspect = self.inspect_dataset(self.working_dir)
            if post_inspect["total_matches"] != projected_expected_total:
                raise PreconditionViolationError(
                    f"Postcondition failed: total {post_inspect['total_matches']} != {projected_expected_total}"
                )
            if post_inspect["unique_ids_count"] != projected_expected_total:
                raise PreconditionViolationError(
                    f"Postcondition failed: unique {post_inspect['unique_ids_count']} != {projected_expected_total}"
                )
            if post_inspect["target_occurrences"] != 1:
                raise PreconditionViolationError(
                    f"Postcondition failed: target occurrences {post_inspect['target_occurrences']} != 1"
                )

            # Check all 6 non-Canonical stores unchanged
            post_store_hashes = post_inspect["store_hashes"]
            for store in NON_CANONICAL_STORES:
                if post_store_hashes[store] != pre_store_hashes[store]:
                    raise ImmutableStoreMutationError(
                        f"CRITICAL: Immutable store '{store}' mutated! {pre_store_hashes[store]} -> {post_store_hashes[store]}"
                    )

            self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                "migration_id": migration_id,
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "post_inspect": post_inspect,
            })

            if fault_inject == "before_completion":
                raise FaultInjectionError("Fault injection: before_completion")

            # Phase 7: COMPLETION_RECORDED
            result_payload = {
                "migration_id": migration_id,
                "snapshot_id": snapshot_id,
                "dataset_identity": dataset_identity,
                "manifest_sha256": manifest_sha256,
                "source_total_matches": source_total,
                "projected_total_matches": projected_expected_total,
                "unique_canonical_ids": projected_expected_total,
                "target_id": TARGET_CANONICAL_ID,
                "target_occurrences": 1,
                "candidate_sha256": cand_hash,
                "projected_file_sha256": projected_file_hash,
                "unrelated_records_preserved": stats["unrelated_matches_preserved"],
                "unrelated_records_lost": 0,
                "immutable_stores_preserved": len(NON_CANONICAL_STORES),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "status": "COMPLETED",
            }

            self.journal.append_entry("COMPLETION_RECORDED", result_payload)

            return {
                "status": "MIGRATION_COMPLETED",
                "migration_id": migration_id,
                "snapshot_id": snapshot_id,
                "source_total_matches": source_total,
                "projected_total_matches": projected_expected_total,
                "candidate_sha256": cand_hash,
                "projected_file_sha256": projected_file_hash,
                "unrelated_records_preserved": stats["unrelated_matches_preserved"],
                "unrelated_records_lost": 0,
            }

    def staging_recover(self) -> dict:
        """
        Evaluate journal and filesystem state under a single lock without recursive locking.
        Classifies active file into EXPECTED_SOURCE, EXPECTED_PROJECTED, or UNKNOWN.
        Recovers interrupted transactions safely and fails closed on inconsistent state.
        Binds recovery strictly to the supplied snapshot manifest and validates journal bindings.
        """
        assert_safe_path(self.working_dir, context="working_dir staging_recover", staging_root=self.staging_root)
        verify_staging_workspace(self.staging_root)

        with acquire_staging_lock(self.working_dir, staging_root=self.staging_root):
            # 1. Validate the supplied snapshot manifest's classification and required identity fields
            if not isinstance(self.manifest, dict):
                raise SnapshotManifestError("Snapshot manifest must be a JSON dictionary")

            classification = self.manifest.get("snapshot_classification")
            if classification not in ALLOWED_CLASSIFICATIONS_PHASE_M:
                raise SnapshotClassificationError(
                    f"Snapshot classification '{classification}' is NOT AUTHORIZED in Phase M"
                )

            expected_snapshot_id = self.manifest.get("snapshot_id")
            expected_dataset_identity = self.manifest.get("dataset_identity")
            if not expected_snapshot_id or not str(expected_snapshot_id).strip():
                raise SnapshotIdentityError("Snapshot manifest has empty or missing snapshot_id")
            if not expected_dataset_identity or not str(expected_dataset_identity).strip():
                raise SnapshotIdentityError("Snapshot manifest has empty or missing dataset_identity")

            if self.manifest.get("field_mapping_policy_id") != FIELD_MAPPING_POLICY_ID:
                raise PolicyMismatchError(
                    f"Field mapping policy ID mismatch: {self.manifest.get('field_mapping_policy_id')} != {FIELD_MAPPING_POLICY_ID}"
                )
            if self.manifest.get("field_mapping_policy_sha256") != FIELD_MAPPING_POLICY_SHA256:
                raise PolicyMismatchError(
                    f"Field mapping policy SHA-256 mismatch: {self.manifest.get('field_mapping_policy_sha256')} != {FIELD_MAPPING_POLICY_SHA256}"
                )
            if self.manifest.get("target_canonical_id") != TARGET_CANONICAL_ID:
                raise PreconditionViolationError(
                    f"Target canonical ID mismatch: {self.manifest.get('target_canonical_id')} != {TARGET_CANONICAL_ID}"
                )
            if self.manifest.get("target_record_a_sha256") != EXPECTED_RECORD_A_SHA256:
                raise PreconditionViolationError(
                    f"Target record A hash mismatch: {self.manifest.get('target_record_a_sha256')} != {EXPECTED_RECORD_A_SHA256}"
                )
            if self.manifest.get("target_record_b_sha256") != EXPECTED_RECORD_B_SHA256:
                raise PreconditionViolationError(
                    f"Target record B hash mismatch: {self.manifest.get('target_record_b_sha256')} != {EXPECTED_RECORD_B_SHA256}"
                )

            # 2. Compute exact manifest identity hash
            expected_manifest_sha256 = hash_manifest(self.manifest)
            expected_source_hash = self.manifest.get("daily_matches_sha256")
            if not expected_source_hash:
                raise SnapshotManifestError("Snapshot manifest missing daily_matches_sha256")

            entries = self.journal.read_entries()
            if not entries:
                return {"status": "NO_JOURNAL_ENTRIES", "action": "NOTHING_TO_RECOVER"}

            prep_entry = next((e for e in entries if e.get("state") == "PREPARED"), None)
            if not prep_entry:
                raise RecoveryStateError("Journal missing required PREPARED entry")

            prep_payload = prep_entry.get("payload")
            if not isinstance(prep_payload, dict):
                raise RecoveryStateError("PREPARED entry payload is malformed")

            # 6. Reject any missing required transaction-binding field rather than silently skipping
            required_prep_fields = [
                "migration_id",
                "snapshot_id",
                "dataset_identity",
                "manifest_sha256",
                "pre_store_hashes",
            ]
            for field in required_prep_fields:
                val = prep_payload.get(field)
                if val is None or (isinstance(val, str) and not val.strip()):
                    raise RecoveryStateError(f"Journal PREPARED entry missing required transaction-binding field: '{field}'")

            migration_id = prep_payload["migration_id"]
            journal_snapshot_id = prep_payload["snapshot_id"]
            journal_dataset_identity = prep_payload["dataset_identity"]
            journal_manifest_sha256 = prep_payload["manifest_sha256"]
            pre_hashes = prep_payload["pre_store_hashes"]

            # 3. Validate journal against expected migration ID, snapshot ID, manifest SHA-256, and dataset identity
            if journal_snapshot_id != expected_snapshot_id:
                raise RecoveryStateError(
                    f"Journal snapshot_id mismatch: '{journal_snapshot_id}' != expected '{expected_snapshot_id}'"
                )
            if journal_dataset_identity != expected_dataset_identity:
                raise RecoveryStateError(
                    f"Journal dataset_identity mismatch: '{journal_dataset_identity}' != expected '{expected_dataset_identity}'"
                )
            if journal_manifest_sha256 != expected_manifest_sha256:
                raise RecoveryStateError(
                    f"Journal manifest_sha256 mismatch: '{journal_manifest_sha256}' != expected '{expected_manifest_sha256}'"
                )

            self.validate_journal_chain(
                entries,
                expected_migration_id=migration_id,
                expected_snapshot_id=expected_snapshot_id,
                expected_manifest_sha256=expected_manifest_sha256,
                expected_dataset_identity=expected_dataset_identity,
            )

            # 4. Bind source file hashes and all six immutable-store hashes to the same snapshot
            if not isinstance(pre_hashes, dict):
                raise RecoveryStateError("pre_store_hashes must be a dictionary")

            if "daily_matches.json" not in pre_hashes or pre_hashes["daily_matches.json"] != expected_source_hash:
                raise RecoveryStateError(
                    f"Journal daily_matches.json hash mismatch against manifest: {pre_hashes.get('daily_matches.json')} != {expected_source_hash}"
                )

            current_hashes = self.get_store_hashes(self.working_dir)
            for store in NON_CANONICAL_STORES:
                manifest_store_key = f"{store.split('.')[0]}_sha256"
                exp_store_hash = self.manifest.get(manifest_store_key)
                if not exp_store_hash:
                    raise SnapshotManifestError(f"Manifest missing hash for immutable store '{store}'")
                if store not in pre_hashes or pre_hashes[store] is None:
                    raise RecoveryStateError(
                        f"Journal pre_store_hashes missing immutable store: '{store}'"
                    )
                if pre_hashes[store] != exp_store_hash:
                    raise RecoveryStateError(
                        f"Journal pre_store_hashes for '{store}' does not match manifest binding: {pre_hashes.get(store)} != {exp_store_hash}"
                    )
                if current_hashes.get(store) != exp_store_hash:
                    raise ImmutableStoreMutationError(
                        f"CRITICAL: Immutable store {store} altered during crash! {exp_store_hash} != {current_hashes.get(store)}"
                    )

            latest_state = self.journal.get_latest_valid_state()

            # 1. COMPLETION_RECORDED: require full completed-state verification
            if latest_state == "COMPLETION_RECORDED":
                try:
                    self.verify_completed_dataset(migration_id=migration_id)
                except Exception as exc:
                    raise RecoveryStateError(
                        f"COMPLETION_RECORDED recovery failed completed-state verification: {exc}"
                    ) from exc
                return {
                    "status": "ALREADY_COMPLETED_NO_OP",
                    "action": "NONE",
                    "latest_state": "COMPLETION_RECORDED",
                }

            daily_file = os.path.join(self.working_dir, "daily_matches.json")
            current_daily_hash = sha256_file(daily_file) if os.path.exists(daily_file) else "MISSING"

            raw_archive = os.path.join(self.archive_dir, "daily_matches_raw.json")
            raw_archive_exists = os.path.exists(raw_archive)

            # Locate intent entry
            intent_entry = next((e for e in reversed(entries) if e.get("state") == "COMMIT_INTENT_DURABLE"), None)
            intent_projected_hash = None
            intent_mig_id = migration_id
            projected_total = self.manifest.get("total_matches", 193) - 1

            if intent_entry:
                i_p = intent_entry.get("payload")
                if not isinstance(i_p, dict):
                    raise RecoveryStateError("COMMIT_INTENT_DURABLE payload is malformed")

                required_intent_fields = [
                    "migration_id",
                    "snapshot_id",
                    "dataset_identity",
                    "manifest_sha256",
                    "projected_file_sha256",
                ]
                for field in required_intent_fields:
                    val = i_p.get(field)
                    if val is None or (isinstance(val, str) and not val.strip()):
                        raise RecoveryStateError(f"COMMIT_INTENT_DURABLE missing required transaction field: '{field}'")

                if i_p["snapshot_id"] != expected_snapshot_id:
                    raise RecoveryStateError(
                        f"Intent snapshot_id mismatch: '{i_p['snapshot_id']}' != expected '{expected_snapshot_id}'"
                    )
                if i_p["dataset_identity"] != expected_dataset_identity:
                    raise RecoveryStateError(
                        f"Intent dataset_identity mismatch: '{i_p['dataset_identity']}' != expected '{expected_dataset_identity}'"
                    )
                if i_p["manifest_sha256"] != expected_manifest_sha256:
                    raise RecoveryStateError(
                        f"Intent manifest_sha256 mismatch: '{i_p['manifest_sha256']}' != expected '{expected_manifest_sha256}'"
                    )

                intent_projected_hash = i_p["projected_file_sha256"]
                intent_mig_id = i_p["migration_id"]
                projected_total = i_p.get("projected_total", projected_total)

            expected_projected_hash = None
            if raw_archive_exists:
                self.verify_forensic_archive(
                    baseline_source_hash=expected_source_hash,
                    expected_migration_id=intent_mig_id,
                    expected_snapshot_id=expected_snapshot_id,
                    expected_dataset_identity=expected_dataset_identity,
                )
                rec_a_data = read_json_strict(os.path.join(self.archive_dir, "record_a.json"))
                rec_b_data = read_json_strict(os.path.join(self.archive_dir, "record_b.json"))
                candidate, _ = build_approved_candidate_projection(rec_a_data, rec_b_data)
                proj_daily, _ = project_snapshot_dataset(read_json_strict(raw_archive), candidate)
                proj_str = json.dumps(proj_daily, ensure_ascii=False, indent=2, sort_keys=True)
                if os.name == "nt":
                    proj_str = proj_str.replace("\n", "\r\n")
                derived_projected_hash = sha256_bytes(proj_str.encode("utf-8"))

                # 5. Require projected hash from durable intent to equal independently derived projected hash
                if intent_projected_hash and intent_projected_hash != derived_projected_hash:
                    raise RecoveryStateError(
                        f"Durable intent projected hash mismatch with independently derived projection: "
                        f"intent '{intent_projected_hash}' != derived '{derived_projected_hash}'"
                    )
                expected_projected_hash = derived_projected_hash
            elif intent_projected_hash:
                if current_daily_hash != expected_source_hash:
                    raise RecoveryStateError("Missing raw archive to derive projected hash for COMMIT_INTENT_DURABLE")

            # Classify file state cryptographically (MC-03)
            if current_daily_hash == expected_source_hash:
                file_state = "EXPECTED_SOURCE"
            elif expected_projected_hash and current_daily_hash == expected_projected_hash:
                file_state = "EXPECTED_PROJECTED"
            else:
                file_state = "UNKNOWN"

            # 2. POSTCONDITIONS_VERIFIED: verify active file is exact expected projected file
            if latest_state == "POSTCONDITIONS_VERIFIED":
                if file_state != "EXPECTED_PROJECTED":
                    raise RecoveryStateError(
                        f"POSTCONDITIONS_VERIFIED recovery requires active file to match expected projected hash, got {file_state} ({current_daily_hash})"
                    )
                self.verify_forensic_archive(
                    baseline_source_hash=expected_source_hash,
                    expected_migration_id=intent_mig_id,
                    expected_snapshot_id=expected_snapshot_id,
                    expected_dataset_identity=expected_dataset_identity,
                )
                post_inspect = self.inspect_dataset(self.working_dir)
                if post_inspect["total_matches"] != projected_total:
                    raise PreconditionViolationError(f"Postcondition failed: total {post_inspect['total_matches']} != {projected_total}")
                if post_inspect["target_occurrences"] != 1:
                    raise PreconditionViolationError(f"Postcondition failed: target occurrences {post_inspect['target_occurrences']} != 1")

                self.journal.append_entry("COMPLETION_RECORDED", {
                    "migration_id": intent_mig_id,
                    "snapshot_id": expected_snapshot_id,
                    "dataset_identity": expected_dataset_identity,
                    "manifest_sha256": expected_manifest_sha256,
                    "projected_total_matches": projected_total,
                    "unique_canonical_ids": projected_total,
                    "target_id": TARGET_CANONICAL_ID,
                    "target_occurrences": 1,
                    "candidate_sha256": EXPECTED_CANDIDATE_SHA256,
                    "projected_file_sha256": expected_projected_hash,
                    "status": "COMPLETED_VIA_RECOVERY",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                return {
                    "status": "RECOVERED_FORWARD_COMPLETED",
                    "action": "APPENDED_COMPLETION_RECORDED",
                }

            # 3. PROJECTION_COMMITTED
            if latest_state == "PROJECTION_COMMITTED":
                if file_state != "EXPECTED_PROJECTED":
                    raise RecoveryStateError(
                        f"PROJECTION_COMMITTED recovery requires active file to match expected projected hash, got {file_state} ({current_daily_hash})"
                    )
                self.verify_forensic_archive(
                    baseline_source_hash=expected_source_hash,
                    expected_migration_id=intent_mig_id,
                    expected_snapshot_id=expected_snapshot_id,
                    expected_dataset_identity=expected_dataset_identity,
                )
                post_inspect = self.inspect_dataset(self.working_dir)
                if post_inspect["total_matches"] != projected_total:
                    raise PreconditionViolationError(f"Recovery postcondition failed: total {post_inspect['total_matches']} != {projected_total}")
                if post_inspect["target_occurrences"] != 1:
                    raise PreconditionViolationError(f"Recovery postcondition failed: target occurrences {post_inspect['target_occurrences']} != 1")

                self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                    "migration_id": intent_mig_id,
                    "snapshot_id": expected_snapshot_id,
                    "dataset_identity": expected_dataset_identity,
                    "manifest_sha256": expected_manifest_sha256,
                    "post_inspect": post_inspect,
                })
                self.journal.append_entry("COMPLETION_RECORDED", {
                    "migration_id": intent_mig_id,
                    "snapshot_id": expected_snapshot_id,
                    "dataset_identity": expected_dataset_identity,
                    "manifest_sha256": expected_manifest_sha256,
                    "projected_total_matches": projected_total,
                    "unique_canonical_ids": projected_total,
                    "target_id": TARGET_CANONICAL_ID,
                    "target_occurrences": 1,
                    "candidate_sha256": EXPECTED_CANDIDATE_SHA256,
                    "projected_file_sha256": expected_projected_hash,
                    "status": "COMPLETED_VIA_RECOVERY",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                })
                return {
                    "status": "RECOVERED_FORWARD_COMPLETED",
                    "action": "COMMITTED_PROJECTION_AND_VERIFIED",
                }

            # 4. COMMIT_INTENT_DURABLE
            if latest_state == "COMMIT_INTENT_DURABLE":
                if file_state == "EXPECTED_PROJECTED":
                    self.verify_forensic_archive(
                        baseline_source_hash=expected_source_hash,
                        expected_migration_id=intent_mig_id,
                        expected_snapshot_id=expected_snapshot_id,
                        expected_dataset_identity=expected_dataset_identity,
                    )
                    self.journal.append_entry("PROJECTION_COMMITTED", {
                        "migration_id": intent_mig_id,
                        "snapshot_id": expected_snapshot_id,
                        "dataset_identity": expected_dataset_identity,
                        "manifest_sha256": expected_manifest_sha256,
                        "daily_matches_sha256": current_daily_hash,
                    })
                    post_inspect = self.inspect_dataset(self.working_dir)
                    if post_inspect["total_matches"] != projected_total:
                        raise PreconditionViolationError(f"Recovery postcondition failed: total {post_inspect['total_matches']} != {projected_total}")
                    if post_inspect["target_occurrences"] != 1:
                        raise PreconditionViolationError(f"Recovery postcondition failed: target occurrences {post_inspect['target_occurrences']} != 1")
                    self.journal.append_entry("POSTCONDITIONS_VERIFIED", {
                        "migration_id": intent_mig_id,
                        "snapshot_id": expected_snapshot_id,
                        "dataset_identity": expected_dataset_identity,
                        "manifest_sha256": expected_manifest_sha256,
                        "post_inspect": post_inspect,
                    })
                    self.journal.append_entry("COMPLETION_RECORDED", {
                        "migration_id": intent_mig_id,
                        "snapshot_id": expected_snapshot_id,
                        "dataset_identity": expected_dataset_identity,
                        "manifest_sha256": expected_manifest_sha256,
                        "projected_total_matches": projected_total,
                        "unique_canonical_ids": projected_total,
                        "target_id": TARGET_CANONICAL_ID,
                        "target_occurrences": 1,
                        "candidate_sha256": EXPECTED_CANDIDATE_SHA256,
                        "projected_file_sha256": expected_projected_hash,
                        "status": "COMPLETED_VIA_RECOVERY",
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    return {
                        "status": "RECOVERED_FORWARD_COMPLETED",
                        "action": "COMMITTED_PROJECTION_AND_VERIFIED",
                    }
                elif file_state == "EXPECTED_SOURCE":
                    if not raw_archive_exists:
                        raise RecoveryStateError("Missing raw archive to roll back from COMMIT_INTENT_DURABLE")
                    self.verify_forensic_archive(
                        baseline_source_hash=expected_source_hash,
                        expected_migration_id=intent_mig_id,
                        expected_snapshot_id=expected_snapshot_id,
                        expected_dataset_identity=expected_dataset_identity,
                    )
                    restore_from_archive_atomic(
                        raw_archive,
                        daily_file,
                        expected_hash=expected_source_hash,
                        staging_root=self.staging_root,
                    )
                    self.journal.append_entry("PREPARED", {
                        "migration_id": intent_mig_id,
                        "snapshot_id": expected_snapshot_id,
                        "dataset_identity": expected_dataset_identity,
                        "manifest_sha256": expected_manifest_sha256,
                        "pre_store_hashes": pre_hashes,
                        "action": "ROLLED_BACK_TO_RAW_ARCHIVE",
                    })
                    return {
                        "status": "RECOVERED_ROLLED_BACK",
                        "action": "RESTORED_ARCHIVE_PRE_COMMIT",
                    }
                else:
                    raise RecoveryStateError(
                        f"COMMIT_INTENT_DURABLE recovery failed closed: active file has unknown hash '{current_daily_hash}'"
                    )

            # 5. Pre-commit states
            if file_state != "EXPECTED_SOURCE":
                raise RecoveryStateError(
                    f"Pre-commit recovery requires active file to match expected source hash, got {file_state} ({current_daily_hash})"
                )

            return {
                "status": "RECOVERED_PRE_COMMIT_CLEAN",
                "action": "SAFE_RESET",
                "latest_state": latest_state,
            }


# =============================================================================
# Synthetic Fixture Helper
# =============================================================================

def create_synthetic_drift_fixture(
    staging_root: str,
    source_data_dir: str,
    num_new_matches: int = 17,
) -> Tuple[str, dict]:
    """
    Construct Fixture B (Synthetic Production Drift) from an isolated source directory.
    Adds exactly `num_new_matches` distinct, fictional matches (e.g. 193 -> 210 matches).
    Target Record A and Record B remain unchanged at indices 55 and 56.
    Generates and returns the isolated working directory and verified manifest.
    """
    assert_safe_path(staging_root, context="synthetic fixture staging_root")
    assert_safe_path(source_data_dir, context="synthetic fixture source_data_dir")

    init_staging_workspace(staging_root, dataset_identity="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    working_data = os.path.join(staging_root, "working", "data")
    os.makedirs(working_data, exist_ok=True)

    # Copy all 7 stores from source_data_dir
    for store in NON_CANONICAL_STORES + ["daily_matches.json"]:
        src = os.path.join(source_data_dir, store)
        dst = os.path.join(working_data, store)
        with open(src, "rb") as s, open(dst, "wb") as d:
            d.write(s.read())

    daily_path = os.path.join(working_data, "daily_matches.json")
    daily_data = read_json_strict(daily_path)
    matches = daily_data.get("matches", [])
    base_count = len(matches)

    # Generate synthetic matches
    for i in range(num_new_matches):
        synth_match = {
            "away": f"Synthetic Away Club {i + 1}",
            "date": "2026-10-12",
            "home": f"Synthetic Home Club {i + 1}",
            "id": f"test-synthetic-2026-10-11-{1000 + i}",
            "jczq_no": f"900{i + 1}",
            "kickoff_time_known": True,
            "league": "Synthetic League",
            "market_tracked": True,
            "odds": {"draw": 3.20, "home_win": 2.10, "away_win": 3.10},
            "odds_updated_at": "2026-10-11T12:00:00+08:00",
            "round": "1",
            "score": {"ft": [1, 0], "ht": [0, 0]},
            "sport": "football",
            "status": "finished",
            "synthetic": True,
        }
        matches.append(synth_match)

    daily_data["matches"] = matches
    if "meta" in daily_data and isinstance(daily_data["meta"], dict):
        daily_data["meta"]["total"] = len(matches)

    write_json_atomic(daily_path, daily_data, staging_root=staging_root)

    manifest = generate_snapshot_manifest(
        data_dir=working_data,
        snapshot_id="SNAPSHOT-SYNTHETIC-210-FIXTURE",
        snapshot_classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
        snapshot_source_description=f"Isolated synthetic production drift fixture with +{num_new_matches} test matches",
        dataset_identity="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
        staging_root=staging_root,
    )

    manifest_path = os.path.join(staging_root, "snapshot_manifest.json")
    write_json_atomic(manifest_path, manifest, staging_root=staging_root)

    return working_data, manifest


# =============================================================================
# CLI Interface
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canonical_snapshot_adapter",
        description="Guarded Snapshot Canonical Adapter for Isolated Staging Remediation",
    )
    # Check for hard prohibition against --allow-production
    parser.add_argument("--staging-root", help="Path to verified isolated staging root directory")
    parser.add_argument("--manifest", help="Path to snapshot manifest JSON file")
    parser.add_argument("--data-dir", help="Path to data directory for inspection or manifest generation")

    subparsers = parser.add_subparsers(dest="command", help="Subcommand to execute")

    subparsers.add_parser("inspect-snapshot", help="Read and inspect snapshot manifest and store consistency")
    subparsers.add_parser("preview-projection", help="Generate preview and diff of candidate projection")
    subparsers.add_parser("validate-projection", help="Validate projection invariants without mutation")

    p_apply = subparsers.add_parser("staging-apply", help="Execute 7-phase migration on isolated staging workspace")
    p_apply.add_argument("--simulation", action="store_true", help="Mandatory staging simulation confirmation flag")
    p_apply.add_argument("--fault-inject", help="Fault-injection hook for testing")
    p_apply.add_argument("--migration-id", default=DEFAULT_MIGRATION_ID, help="Unique migration run ID")

    p_recover = subparsers.add_parser("staging-recover", help="Evaluate journal and recover interrupted staging migration")
    p_recover.add_argument("--migration-id", default=DEFAULT_MIGRATION_ID, help="Expected migration ID")

    p_verify = subparsers.add_parser("staging-verify", help="Verify post-migration invariants on completed dataset")

    p_gen = subparsers.add_parser("generate-manifest", help="Generate snapshot manifest from isolated data directory")
    p_gen.add_argument("--data-dir", help="Path to data directory for manifest generation")
    p_gen.add_argument("--snapshot-id", required=True, help="Snapshot identifier")
    p_gen.add_argument("--classification", required=True, help="Snapshot classification")
    p_gen.add_argument("--description", default="Snapshot manifest", help="Source description")
    p_gen.add_argument("--dataset-identity", required=True, help="Dataset identity")
    p_gen.add_argument("--output", help="Path to save generated manifest")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw_args = argv if argv is not None else sys.argv[1:]

    # Enforce strict prohibition on --allow-production
    for arg in raw_args:
        if arg in ("--allow-production", "--enable-production", "--production"):
            raise ProtectedPathViolationError(
                f"SAFETY VIOLATION: Production option '{arg}' is strictly prohibited. "
                f"Production execution is NOT AUTHORIZED."
            )

    parser = build_parser()
    args = parser.parse_args(raw_args)

    if not args.command:
        parser.print_help()
        return 1

    try:
        if args.command == "generate-manifest":
            if not args.data_dir:
                print("Error: --data-dir is required for generate-manifest", file=sys.stderr)
                return 1
            manifest = generate_snapshot_manifest(
                data_dir=args.data_dir,
                snapshot_id=args.snapshot_id,
                snapshot_classification=args.classification,
                snapshot_source_description=args.description,
                dataset_identity=args.dataset_identity,
                staging_root=args.staging_root,
            )
            out_str = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            if args.output:
                if not args.staging_root:
                    print("Error: --staging-root is required when specifying --output", file=sys.stderr)
                    return 1
                verify_staging_workspace(args.staging_root)
                safe_output = assert_safe_path(
                    args.output,
                    context="generate-manifest output",
                    staging_root=args.staging_root,
                )
                write_json_atomic(safe_output, manifest, staging_root=args.staging_root)
                print(f"Manifest written to {safe_output}")
            else:
                print(out_str)
            return 0

        if not args.staging_root:
            print("Error: --staging-root is required", file=sys.stderr)
            return 1

        manifest_data = {}
        if args.manifest:
            manifest_data = read_json_strict(args.manifest)
        else:
            default_manifest_path = os.path.join(args.staging_root, "snapshot_manifest.json")
            if os.path.exists(default_manifest_path):
                manifest_data = read_json_strict(default_manifest_path)

        if args.command == "inspect-snapshot":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=False,
            )
            target_data = args.data_dir or adapter.working_dir
            res = adapter.inspect_dataset(target_data)
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        elif args.command == "preview-projection":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=True,
            )
            res = adapter.preview_projection(write_output=True)
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        elif args.command == "validate-projection":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=False,
            )
            preview = adapter.preview_projection(write_output=False)
            print(f"Validation successful. Candidate SHA-256: {preview['candidate_record_sha256']}")
            return 0

        elif args.command == "staging-apply":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=True,
            )
            res = adapter.staging_apply(
                simulation=args.simulation,
                fault_inject=args.fault_inject,
                migration_id=args.migration_id,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        elif args.command == "staging-recover":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=True,
            )
            res = adapter.staging_recover()
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        elif args.command == "staging-verify":
            adapter = SnapshotCanonicalAdapter(
                staging_root=args.staging_root,
                manifest=manifest_data,
                writable=False,
            )
            res = adapter.staging_verify()
            print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

    except Exception as exc:
        if argv is not None:
            raise
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
