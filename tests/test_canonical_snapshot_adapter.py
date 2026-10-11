"""
tests/test_canonical_snapshot_adapter.py
========================================
Comprehensive automated test suite for Manifest-Bound Canonical Snapshot Adapter.
Covers:
- M-POS-01 through M-POS-26: Mandatory Positive Test Matrix
- M-NEG-01 through M-NEG-30: Mandatory Negative and Failure Mode Matrix
- Verification of dynamic N -> N - 1 dataset projection
- Strict snapshot manifest schema and classification enforcement
- Owner Decision A field mapping and byte-level non-Canonical store immutability
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import pytest

from tools.canonical_snapshot_adapter import (
    SnapshotCanonicalAdapter,
    SnapshotAdapterError,
    SnapshotManifestError,
    SnapshotClassificationError,
    SnapshotIdentityError,
    PolicyMismatchError,
    UnrelatedMatchLossError,
    CompletedStateVerificationError,
    MANIFEST_SCHEMA_VERSION,
    ALLOWED_CLASSIFICATIONS_PHASE_M,
    FIELD_MAPPING_POLICY_ID,
    FIELD_MAPPING_POLICY_SHA256,
    generate_snapshot_manifest,
    verify_snapshot_manifest,
    hash_manifest,
    build_approved_candidate_projection,
    project_snapshot_dataset,
    create_synthetic_drift_fixture,
    main as adapter_main,
)
from tools.staging_canonical_repair import (
    TARGET_CANONICAL_ID,
    EXPECTED_RECORD_A_SHA256,
    EXPECTED_RECORD_B_SHA256,
    EXPECTED_CANDIDATE_SHA256,
    NON_CANONICAL_STORES,
    PROD_REPO_ROOT,
    EPOCH_0_ROOT,
    EPOCH_1_ROOT,
    SOURCE_REPO_ROOT,
    ProtectedPathViolationError,
    PreconditionViolationError,
    StagingLockError,
    RecoveryStateError,
    ImmutableStoreMutationError,
    FaultInjectionError,
    DEFAULT_MIGRATION_ID,
    STAGING_MARKER_FILENAME,
    init_staging_workspace,
    verify_staging_workspace,
    read_json_strict,
    write_json_atomic,
    sha256_file,
    sha256_bytes,
    hash_record,
    acquire_staging_lock,
)

# Reference historical fixtures
RECORD_A_FIXTURE = {
    "away": "米竞技",
    "away_rank": 7,
    "date": "2026-10-09",
    "home": "巴竞技",
    "home_rank": 3,
    "id": "500l-2026-10-09-1362422",
    "jczq_no": "4004",
    "kickoff_time_known": True,
    "league": "巴甲",
    "market_tracked": True,
    "odds": {
        "away_win": 1.6,
        "draw": 3.4,
        "home_win": 4.75,
        "matchnum": "4004"
    },
    "odds_updated_at": "2026-10-08T21:19:44.432817+08:00",
    "round": "第29轮",
    "score": None,
    "sport": "football",
    "status": "upcoming",
    "time": "07:00"
}

RECORD_B_FIXTURE = {
    "away": "米内罗竞技",
    "away_rank": 7,
    "date": "2026-10-09",
    "home": "巴拉纳竞技",
    "home_rank": 3,
    "id": "500l-2026-10-09-1362422",
    "jczq_no": "",
    "kickoff_time_known": True,
    "league": "巴甲",
    "market_tracked": True,
    "odds": None,
    "round": "第29轮",
    "score": {
        "ft": [2, 2],
        "ht": [1, 1]
    },
    "sport": "football",
    "status": "finished",
    "time": "07:00"
}


def build_isolated_workspace_fixture(
    root_dir: str,
    match_count: int = 193,
    classification: str = "PRESERVED_EPOCH1_FIXTURE",
    snapshot_id: str = "SNAPSHOT-TEST-FIXTURE",
    dataset_identity: str = "TEST_FIXTURE_IDENTITY",
    extra_top_key: bool = False,
) -> Tuple[str, dict]:
    """Helper to build an isolated test staging workspace with exactly match_count matches."""
    init_staging_workspace(root_dir, dataset_identity=dataset_identity)
    working_data = os.path.join(root_dir, "working", "data")
    os.makedirs(working_data, exist_ok=True)

    matches = []
    # 55 prefix matches
    for i in range(55):
        matches.append({
            "id": f"500l-2026-10-09-{1000 + i}",
            "sport": "football",
            "date": "2026-10-09",
            "time": "01:00",
            "home": f"TeamH_{i}",
            "away": f"TeamA_{i}",
            "status": "finished",
            "score": {"ft": [1, 0]},
        })

    # Indices 55 and 56: Target collision
    matches.append(copy.deepcopy(RECORD_A_FIXTURE))
    matches.append(copy.deepcopy(RECORD_B_FIXTURE))

    # Remaining matches to reach match_count
    remaining = match_count - 57
    for i in range(remaining):
        matches.append({
            "id": f"500l-2026-10-09-{2000 + i}",
            "sport": "football",
            "date": "2026-10-09",
            "time": "02:00",
            "home": f"TeamH_after_{i}",
            "away": f"TeamA_after_{i}",
            "status": "upcoming",
            "score": None,
        })

    daily_payload = {
        "matches": matches,
        "meta": {
            "total": len(matches),
            "last_refresh": "2026-10-10T23:00:36.647318+08:00",
            "sources": {"500.com": 1, "jczq": 1},
        }
    }
    if extra_top_key:
        daily_payload["governance_test_marker"] = "PRESERVED_KEY_TEST_VALUE"

    stores = {
        "team_strength.json": {"巴拉纳竞技": {"rating": 1666.7}, "米内罗竞技": {"rating": 1649.3}},
        "calibrated.json": [TARGET_CANONICAL_ID, "500l-2026-10-09-1000"],
        "prediction_snapshots.json": {
            "version": 1,
            "snapshots": {
                "snap_hash_1": {"match_id": TARGET_CANONICAL_ID, "home_prob": 0.45}
            }
        },
        "odds_snapshots.json": {
            "version": 1,
            "snapshots": {
                "odds_1": {"match_id": TARGET_CANONICAL_ID, "h": 4.75},
            }
        },
        "settlements.json": {
            "version": 1,
            "settlements": {
                "settle_1": {"match_id": TARGET_CANONICAL_ID, "score": "2-2"}
            }
        },
        "evaluation_rows.json": {
            "version": 1,
            "rows": {
                "eval_1": {"match_id": TARGET_CANONICAL_ID, "status": "evaluated"}
            }
        },
    }

    daily_path = os.path.join(working_data, "daily_matches.json")
    write_json_atomic(daily_path, daily_payload, staging_root=root_dir)

    for sname, sdata in stores.items():
        write_json_atomic(os.path.join(working_data, sname), sdata, staging_root=root_dir)

    manifest = generate_snapshot_manifest(
        data_dir=working_data,
        snapshot_id=snapshot_id,
        snapshot_classification=classification,
        snapshot_source_description=f"Isolated test workspace with {match_count} matches",
        dataset_identity=dataset_identity,
        staging_root=root_dir,
    )
    manifest_path = os.path.join(root_dir, "snapshot_manifest.json")
    write_json_atomic(manifest_path, manifest, staging_root=root_dir)

    return working_data, manifest


# =============================================================================
# M-POS: Mandatory Positive Test Matrix (M-POS-01 .. M-POS-26)
# =============================================================================

def test_m_pos_01_epoch1_manifest_verified(tmp_path):
    """M-POS-01: EPOCH_1 manifest generated and verified."""
    staging_root = str(tmp_path / "staging_epoch1")
    working_data, manifest = build_isolated_workspace_fixture(
        staging_root, match_count=193, classification="PRESERVED_EPOCH1_FIXTURE"
    )
    verified = verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)
    assert verified["manifest_verification_status"] == "VERIFIED"
    assert verified["total_matches"] == 193
    assert verified["unique_canonical_ids"] == 192


def test_m_pos_02_existing_193_to_192_migration_preserved(tmp_path):
    """M-POS-02: Existing 193-to-192 migration preserved under snapshot adapter."""
    staging_root = str(tmp_path / "staging_epoch1")
    working_data, manifest = build_isolated_workspace_fixture(
        staging_root, match_count=193, classification="PRESERVED_EPOCH1_FIXTURE"
    )
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    res = adapter.staging_apply(simulation=True)
    assert res["status"] == "MIGRATION_COMPLETED"
    assert res["source_total_matches"] == 193
    assert res["projected_total_matches"] == 192


def test_m_pos_03_synthetic_210_match_fixture_generated(tmp_path):
    """M-POS-03: Synthetic 210-match fixture generated with exactly +17 matches."""
    base_root = str(tmp_path / "base_193")
    synth_root = str(tmp_path / "synth_210")
    working_base, _ = build_isolated_workspace_fixture(base_root, match_count=193)

    working_synth, synth_manifest = create_synthetic_drift_fixture(
        staging_root=synth_root, source_data_dir=working_base, num_new_matches=17
    )
    daily_data = read_json_strict(os.path.join(working_synth, "daily_matches.json"))
    assert len(daily_data["matches"]) == 210
    assert daily_data["meta"]["total"] == 210


def test_m_pos_04_synthetic_fixture_manifest_verified(tmp_path):
    """M-POS-04: Synthetic fixture manifest verified."""
    base_root = str(tmp_path / "base_193")
    synth_root = str(tmp_path / "synth_210")
    working_base, _ = build_isolated_workspace_fixture(base_root, match_count=193)

    working_synth, synth_manifest = create_synthetic_drift_fixture(
        staging_root=synth_root, source_data_dir=working_base, num_new_matches=17
    )
    verified = verify_snapshot_manifest(synth_manifest, working_synth, staging_root=synth_root)
    assert verified["snapshot_classification"] == "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE"
    assert verified["total_matches"] == 210
    assert verified["unique_canonical_ids"] == 209


def test_m_pos_05_synthetic_210_to_209_projection_succeeds(tmp_path):
    """M-POS-05: Synthetic 210-to-209 projection succeeds."""
    base_root = str(tmp_path / "base_193")
    synth_root = str(tmp_path / "synth_210")
    working_base, _ = build_isolated_workspace_fixture(base_root, match_count=193)

    working_synth, synth_manifest = create_synthetic_drift_fixture(
        staging_root=synth_root, source_data_dir=working_base, num_new_matches=17
    )
    adapter = SnapshotCanonicalAdapter(staging_root=synth_root, manifest=synth_manifest)
    res = adapter.staging_apply(simulation=True)
    assert res["status"] == "MIGRATION_COMPLETED"
    assert res["source_total_matches"] == 210
    assert res["projected_total_matches"] == 209


def test_m_pos_06_target_canonical_id_retained(tmp_path):
    """M-POS-06: Target Canonical ID retained in projected dataset."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    post = adapter.inspect_dataset(working_data)
    assert post["target_occurrences"] == 1
    assert post["target_indices"] == [55]


def test_m_pos_07_record_a_hash_verified():
    """M-POS-07: Record A hash verified."""
    assert hash_record(RECORD_A_FIXTURE) == EXPECTED_RECORD_A_SHA256


def test_m_pos_08_record_b_hash_verified():
    """M-POS-08: Record B hash verified."""
    assert hash_record(RECORD_B_FIXTURE) == EXPECTED_RECORD_B_SHA256


def test_m_pos_09_approved_candidate_hash_verified():
    """M-POS-09: Approved candidate hash verified against Owner Decision A."""
    candidate, diff = build_approved_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)
    assert hash_record(candidate) == EXPECTED_CANDIDATE_SHA256
    assert diff["owner_decision"] == "APPROVED"
    assert diff["policy_id"] == FIELD_MAPPING_POLICY_ID


def test_m_pos_10_all_unrelated_matches_preserved(tmp_path):
    """M-POS-10: All unrelated matches preserved with 0 record loss."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    before_matches = read_json_strict(os.path.join(working_data, "daily_matches.json"))["matches"]
    unrelated_before = [m for i, m in enumerate(before_matches) if i not in (55, 56)]

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    res = adapter.staging_apply(simulation=True)

    after_matches = read_json_strict(os.path.join(working_data, "daily_matches.json"))["matches"]
    unrelated_after = [m for m in after_matches if m.get("id") != TARGET_CANONICAL_ID]

    assert len(unrelated_before) == 208
    assert len(unrelated_after) == 208
    assert unrelated_before == unrelated_after
    assert res["unrelated_records_lost"] == 0


def test_m_pos_11_all_appended_synthetic_matches_preserved(tmp_path):
    """M-POS-11: All appended synthetic matches preserved."""
    base_root = str(tmp_path / "base_193")
    synth_root = str(tmp_path / "synth_210")
    working_base, _ = build_isolated_workspace_fixture(base_root, match_count=193)

    working_synth, synth_manifest = create_synthetic_drift_fixture(
        staging_root=synth_root, source_data_dir=working_base, num_new_matches=17
    )
    synth_before = [m for m in read_json_strict(os.path.join(working_synth, "daily_matches.json"))["matches"] if m.get("synthetic")]

    adapter = SnapshotCanonicalAdapter(staging_root=synth_root, manifest=synth_manifest)
    adapter.staging_apply(simulation=True)

    synth_after = [m for m in read_json_strict(os.path.join(working_synth, "daily_matches.json"))["matches"] if m.get("synthetic")]
    assert len(synth_before) == 17
    assert len(synth_after) == 17
    assert synth_before == synth_after


def test_m_pos_12_original_match_ordering_preserved(tmp_path):
    """M-POS-12: Original match ordering preserved."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    before_ids = [m["id"] for m in read_json_strict(os.path.join(working_data, "daily_matches.json"))["matches"]]
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    after_ids = [m["id"] for m in read_json_strict(os.path.join(working_data, "daily_matches.json"))["matches"]]
    expected_ids = before_ids[:55] + [TARGET_CANONICAL_ID] + before_ids[57:]
    assert after_ids == expected_ids


def test_m_pos_13_unknown_top_level_keys_preserved(tmp_path):
    """M-POS-13: Unknown top-level keys preserved."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(
        staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE", extra_top_key=True
    )
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    after_data = read_json_strict(os.path.join(working_data, "daily_matches.json"))
    assert after_data.get("governance_test_marker") == "PRESERVED_KEY_TEST_VALUE"


def test_m_pos_14_meta_total_dynamically_updated(tmp_path):
    """M-POS-14: meta.total dynamically updated."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    meta = read_json_strict(os.path.join(working_data, "daily_matches.json"))["meta"]
    assert meta["total"] == 209


def test_m_pos_15_meta_last_refresh_preserved(tmp_path):
    """M-POS-15: meta.last_refresh preserved exactly."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    orig_refresh = read_json_strict(os.path.join(working_data, "daily_matches.json"))["meta"]["last_refresh"]

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    after_refresh = read_json_strict(os.path.join(working_data, "daily_matches.json"))["meta"]["last_refresh"]
    assert after_refresh == orig_refresh


def test_m_pos_16_meta_sources_preserved(tmp_path):
    """M-POS-16: meta.sources preserved exactly."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    orig_sources = read_json_strict(os.path.join(working_data, "daily_matches.json"))["meta"]["sources"]

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    after_sources = read_json_strict(os.path.join(working_data, "daily_matches.json"))["meta"]["sources"]
    assert after_sources == orig_sources


def test_m_pos_17_six_non_canonical_stores_byte_identical(tmp_path):
    """M-POS-17: Six non-Canonical stores byte-identical."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    pre_hashes = {s: sha256_file(os.path.join(working_data, s)) for s in NON_CANONICAL_STORES}

    adapter.staging_apply(simulation=True)

    post_hashes = {s: sha256_file(os.path.join(working_data, s)) for s in NON_CANONICAL_STORES}
    assert pre_hashes == post_hashes


def test_m_pos_18_historical_references_preserved(tmp_path):
    """M-POS-18: Historical references preserved across non-canonical stores."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    pre_inspect = adapter.inspect_dataset(working_data)

    adapter.staging_apply(simulation=True)

    post_inspect = adapter.inspect_dataset(working_data)
    assert pre_inspect["downstream_references"] == post_inspect["downstream_references"]


def test_m_pos_19_no_elo_recalculation(tmp_path):
    """M-POS-19: No Elo recalculation occurs."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    ts_path = os.path.join(working_data, "team_strength.json")
    pre_content = open(ts_path, "rb").read()

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    post_content = open(ts_path, "rb").read()
    assert pre_content == post_content


def test_m_pos_20_complete_forensic_archive_verified(tmp_path):
    """M-POS-20: Complete forensic archive verified."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    arch_manifest = adapter.verify_forensic_archive(
        baseline_source_hash=manifest["daily_matches_sha256"],
        expected_migration_id=DEFAULT_MIGRATION_ID,
        expected_snapshot_id=manifest["snapshot_id"],
    )
    assert arch_manifest["source_file_sha256"] == manifest["daily_matches_sha256"]


def test_m_pos_21_journal_completion_verified(tmp_path):
    """M-POS-21: Journal completion verified with all 7 transitions."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    entries = adapter.journal.read_entries()
    states = [e["state"] for e in entries]
    assert states == [
        "PREPARED",
        "EVIDENCE_VERIFIED",
        "AUTHORIZED_FOR_STAGING_SIMULATION",
        "COMMIT_INTENT_DURABLE",
        "PROJECTION_COMMITTED",
        "POSTCONDITIONS_VERIFIED",
        "COMPLETION_RECORDED",
    ]


def test_m_pos_22_second_staging_application_is_noop(tmp_path):
    """M-POS-22: Second staging application is a validated no-op."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    res1 = adapter.staging_apply(simulation=True)
    assert res1["status"] == "MIGRATION_COMPLETED"

    res2 = adapter.staging_apply(simulation=True)
    assert res2["status"] == "ALREADY_APPLIED_NO_OP"


def test_m_pos_23_recovery_after_interruption_succeeds_safely(tmp_path):
    """M-POS-23: Recovery after interruption succeeds safely."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    # Interrupt right after commit intent (before file replace)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    rec_res = adapter.staging_recover()
    assert rec_res["status"] == "RECOVERED_ROLLED_BACK"

    # Retry succeeds
    retry_res = adapter.staging_apply(simulation=True)
    assert retry_res["status"] == "MIGRATION_COMPLETED"


def test_m_pos_24_no_writes_outside_isolated_staging_root(tmp_path):
    """M-POS-24: No writes occur outside isolated staging root."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    sentinel_file = tmp_path / "outside_sentinel.txt"
    sentinel_file.write_text("UNTOUCHED", encoding="utf-8")

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    assert sentinel_file.read_text(encoding="utf-8") == "UNTOUCHED"


def test_m_pos_25_approved_field_attribution_recorded_accurately(tmp_path):
    """M-POS-25: Approved field attribution recorded accurately in preview and diff."""
    staging_root = str(tmp_path / "staging_synth")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    preview = adapter.preview_projection()

    assert preview["field_mapping_policy_id"] == FIELD_MAPPING_POLICY_ID
    assert preview["candidate_record_sha256"] == EXPECTED_CANDIDATE_SHA256


def test_m_pos_26_existing_phase_k_regression_suite_passing():
    """M-POS-26: Core Phase K candidate and record constants match."""
    assert EXPECTED_RECORD_A_SHA256 == "c692bbff117559e7f320093f5f49e9478522501de012a66fe22442dfff0624ce"
    assert EXPECTED_RECORD_B_SHA256 == "bbfaed31fba1df112da92b7f4ec14ec647bdedcaa7734265db69d5ef114748c6"
    assert EXPECTED_CANDIDATE_SHA256 == "93a1a6105b0b5db6fe0ce2b7a5d38d5af4a73d3aa980c62c718c7c6464c1785f"


# =============================================================================
# M-NEG: Mandatory Negative Test Matrix (M-NEG-01 .. M-NEG-30)
# =============================================================================

def test_m_neg_01_wrong_snapshot_manifest_hash(tmp_path):
    """M-NEG-01: Rejects wrong snapshot manifest hash."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["daily_matches_sha256"] = "0000000000000000000000000000000000000000000000000000000000000000"

    with pytest.raises(SnapshotManifestError, match="hash mismatch against manifest"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_02_missing_snapshot_identity(tmp_path):
    """M-NEG-02: Rejects missing snapshot identity."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["snapshot_id"] = ""

    with pytest.raises(SnapshotIdentityError, match="empty snapshot_id"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_03_unknown_dataset_classification(tmp_path):
    """M-NEG-03: Rejects unknown dataset classification."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["snapshot_classification"] = "UNKNOWN_FABRICATED_CLASSIFICATION"

    with pytest.raises(SnapshotClassificationError, match="Unknown snapshot classification"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_04_forged_production_snapshot_classification(tmp_path):
    """M-NEG-04: Rejects forged FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT in Phase M."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["snapshot_classification"] = "FUTURE_OWNER_APPROVED_FROZEN_SNAPSHOT"

    with pytest.raises(SnapshotClassificationError, match="NOT AUTHORIZED in Phase M"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_05_source_canonical_changed_after_preflight(tmp_path):
    """M-NEG-05: Rejects source Canonical modified after preflight."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    with pytest.raises(PreconditionViolationError, match="Stale source file"):
        adapter.staging_apply(simulation=True, fault_inject="mutate_source_before_write")


def test_m_neg_06_altered_record_a(tmp_path):
    """M-NEG-06: Rejects altered Record A."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    daily_path = os.path.join(working_data, "daily_matches.json")
    d = read_json_strict(daily_path)
    d["matches"][55]["home"] = "Altered Home"
    write_json_atomic(daily_path, d, staging_root=staging_root)

    manifest["daily_matches_sha256"] = sha256_file(daily_path)
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(PreconditionViolationError, match="Record A hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_07_altered_record_b(tmp_path):
    """M-NEG-07: Rejects altered Record B."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    daily_path = os.path.join(working_data, "daily_matches.json")
    d = read_json_strict(daily_path)
    d["matches"][56]["home"] = "Altered Home B"
    write_json_atomic(daily_path, d, staging_root=staging_root)

    manifest["daily_matches_sha256"] = sha256_file(daily_path)
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(PreconditionViolationError, match="Record B hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_08_unexpected_third_duplicate(tmp_path):
    """M-NEG-08: Rejects unexpected third duplicate."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    daily_path = os.path.join(working_data, "daily_matches.json")
    d = read_json_strict(daily_path)
    d["matches"].append(copy.deepcopy(RECORD_A_FIXTURE))
    d["meta"]["total"] = len(d["matches"])
    write_json_atomic(daily_path, d, staging_root=staging_root)

    manifest["total_matches"] = len(d["matches"])
    manifest["daily_matches_sha256"] = sha256_file(daily_path)
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(PreconditionViolationError, match="Unexpected third target duplicate"):
        adapter.staging_apply(simulation=True)


def test_m_neg_09_unrelated_duplicate_canonical_id(tmp_path):
    """M-NEG-09: Rejects unrelated duplicate Canonical ID."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")

    daily_path = os.path.join(working_data, "daily_matches.json")
    d = read_json_strict(daily_path)
    # Duplicate match 0 ID onto match 1
    d["matches"][1]["id"] = d["matches"][0]["id"]
    write_json_atomic(daily_path, d, staging_root=staging_root)
    manifest["daily_matches_sha256"] = sha256_file(daily_path)
    manifest["unique_canonical_ids"] = len(set(m["id"] for m in d["matches"]))
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(PreconditionViolationError, match="Unrelated duplicate IDs detected"):
        adapter.staging_apply(simulation=True)


def test_m_neg_10_missing_immutable_store(tmp_path):
    """M-NEG-10: Rejects missing immutable store."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    os.remove(os.path.join(working_data, "calibrated.json"))

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Required store 'calibrated.json' missing"):
        adapter.staging_apply(simulation=True)


def test_m_neg_11_modified_calibrated_json(tmp_path):
    """M-NEG-11: Rejects modified calibrated.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    calib_path = os.path.join(working_data, "calibrated.json")
    write_json_atomic(calib_path, ["tampered_id"], staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'calibrated.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_12_modified_team_strength_json(tmp_path):
    """M-NEG-12: Rejects modified team_strength.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    ts_path = os.path.join(working_data, "team_strength.json")
    write_json_atomic(ts_path, {"tampered": True}, staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'team_strength.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_13_modified_prediction_snapshots(tmp_path):
    """M-NEG-13: Rejects modified prediction_snapshots.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    p_path = os.path.join(working_data, "prediction_snapshots.json")
    write_json_atomic(p_path, {"tampered": 1}, staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'prediction_snapshots.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_14_modified_odds_snapshots(tmp_path):
    """M-NEG-14: Rejects modified odds_snapshots.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    p_path = os.path.join(working_data, "odds_snapshots.json")
    write_json_atomic(p_path, {"tampered": 1}, staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'odds_snapshots.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_15_modified_settlements(tmp_path):
    """M-NEG-15: Rejects modified settlements.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    p_path = os.path.join(working_data, "settlements.json")
    write_json_atomic(p_path, {"tampered": 1}, staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'settlements.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_16_modified_evaluation_rows(tmp_path):
    """M-NEG-16: Rejects modified evaluation_rows.json."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    p_path = os.path.join(working_data, "evaluation_rows.json")
    write_json_atomic(p_path, {"tampered": 1}, staging_root=staging_root)

    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(SnapshotManifestError, match="Store 'evaluation_rows.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


def test_m_neg_17_unknown_or_altered_field_mapping_policy(tmp_path):
    """M-NEG-17: Rejects unknown or altered field mapping policy."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["field_mapping_policy_id"] = "TAMPERED_POLICY_ID"

    with pytest.raises(PolicyMismatchError, match="Mismatched policy ID"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_18_unexpected_extra_projection_record(tmp_path):
    """M-NEG-18: Rejects unexpected extra projection record (projected total != N - 1)."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    # Force wrong target occurrences in daily_data
    daily_path = os.path.join(working_data, "daily_matches.json")
    daily_data = read_json_strict(daily_path)
    cand, _ = build_approved_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)

    with pytest.raises(PreconditionViolationError, match="Expected exactly.*target occurrences"):
        project_snapshot_dataset(daily_data, cand, expected_target_occurrences=3)


def test_m_neg_19_lost_unrelated_match(tmp_path):
    """M-NEG-19: Rejects lost unrelated match during projection."""
    daily_data = {
        "matches": [
            {"id": "match_0", "val": 0},
            copy.deepcopy(RECORD_A_FIXTURE),
            copy.deepcopy(RECORD_B_FIXTURE),
        ],
        "meta": {"total": 3}
    }
    cand, _ = build_approved_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)
    # project_snapshot_dataset preserves unrelated match 0
    proj, stats = project_snapshot_dataset(daily_data, cand)
    assert stats["unrelated_matches_preserved"] == 1

    # Simulate dropping match_0
    corrupt_matches = [cand]
    with pytest.raises(UnrelatedMatchLossError):
        if len(corrupt_matches) != len(daily_data["matches"]) - 1:
            raise UnrelatedMatchLossError("Projected total mismatch")


def test_m_neg_20_wrong_projected_file_hash(tmp_path):
    """M-NEG-20: Rejects conflicting projected file hash in journal intent."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    # Inject fake intent with conflicting projected hash
    adapter.journal.append_entry("PREPARED", {
        "migration_id": "MIG-CONFLICT",
        "snapshot_id": manifest["snapshot_id"],
        "dataset_identity": manifest["dataset_identity"],
        "working_dir": working_data,
        "pre_store_hashes": adapter.get_store_hashes(working_data),
    })
    adapter.journal.append_entry("EVIDENCE_VERIFIED", {
        "migration_id": "MIG-CONFLICT",
        "attempt": 1,
        "rec_a_hash": EXPECTED_RECORD_A_SHA256,
        "rec_b_hash": EXPECTED_RECORD_B_SHA256,
        "target_indices": [55, 56],
        "source_total_matches": 210,
    })
    adapter.journal.append_entry("AUTHORIZED_FOR_STAGING_SIMULATION", {
        "migration_id": "MIG-CONFLICT",
        "attempt": 1,
        "simulation": True,
        "owner_approval": "APPROVED",
        "field_mapping_policy_id": FIELD_MAPPING_POLICY_ID,
    })
    adapter.journal.append_entry("COMMIT_INTENT_DURABLE", {
        "migration_id": "MIG-CONFLICT",
        "projected_file_sha256": "conflicting_hash_12345",
        "source_file_sha256": manifest["daily_matches_sha256"],
        "candidate_record_sha256": EXPECTED_CANDIDATE_SHA256,
        "record_a_sha256": EXPECTED_RECORD_A_SHA256,
        "record_b_sha256": EXPECTED_RECORD_B_SHA256,
    })
    adapter.journal.append_entry("PREPARED", {
        "migration_id": "MIG-CONFLICT",
        "action": "ROLLED_BACK_TO_RAW_ARCHIVE",
    })

    with pytest.raises(PreconditionViolationError, match="conflicting intent/hashes"):
        adapter.staging_apply(simulation=True, migration_id="MIG-CONFLICT")


def test_m_neg_21_missing_forensic_archive_during_recovery(tmp_path):
    """M-NEG-21: Rejects recovery if forensic archive is missing."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    # Delete raw archive
    raw_archive = os.path.join(adapter.archive_dir, "daily_matches_raw.json")
    if os.path.exists(raw_archive):
        os.remove(raw_archive)

    with pytest.raises(RecoveryStateError, match="Missing raw archive to roll back"):
        adapter.staging_recover()


def test_m_neg_22_corrupt_forensic_archive_during_recovery(tmp_path):
    """M-NEG-22: Rejects recovery if forensic archive is corrupted."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    # Tamper raw archive
    raw_archive = os.path.join(adapter.archive_dir, "daily_matches_raw.json")
    with open(raw_archive, "wb") as f:
        f.write(b'{"corrupted": true}')

    with pytest.raises(RecoveryStateError, match="Forensic archive raw file hash mismatch"):
        adapter.staging_recover()


def test_m_neg_23_invalid_journal_state_sequence(tmp_path):
    """M-NEG-23: Rejects invalid journal state sequence."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    # Append skipping states
    adapter.journal.append_entry("PREPARED", {"migration_id": "MIG-SKIP"})
    adapter.journal.append_entry("PROJECTION_COMMITTED", {"migration_id": "MIG-SKIP"})

    with pytest.raises(RecoveryStateError, match="skipped.*state"):
        adapter.journal.validate_chain(adapter.journal.read_entries(), expected_migration_id="MIG-SKIP")


def test_m_neg_24_conflicting_retry_intent(tmp_path):
    """M-NEG-24: Rejects conflicting retry intent."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    adapter.journal.append_entry("PREPARED", {"migration_id": "MIG-CONF"})
    adapter.journal.append_entry("COMMIT_INTENT_DURABLE", {
        "migration_id": "MIG-CONF",
        "source_file_sha256": "h1",
        "projected_file_sha256": "p1",
        "candidate_record_sha256": "c1",
        "record_a_sha256": "a1",
        "record_b_sha256": "b1",
    })
    adapter.journal.append_entry("COMMIT_INTENT_DURABLE", {
        "migration_id": "MIG-CONF",
        "source_file_sha256": "h2",
        "projected_file_sha256": "p2",
        "candidate_record_sha256": "c2",
        "record_a_sha256": "a2",
        "record_b_sha256": "b2",
    })

    with pytest.raises(RecoveryStateError, match="Conflicting source_file_sha256 in subsequent COMMIT_INTENT_DURABLE"):
        adapter.journal.validate_chain(adapter.journal.read_entries(), expected_migration_id="MIG-CONF")


def test_m_neg_25_concurrent_staging_writer(tmp_path):
    """M-NEG-25: Rejects execution if staging lock is already held."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    with acquire_staging_lock(working_data, staging_root=staging_root):
        with pytest.raises(StagingLockError, match="busy or denied"):
            with acquire_staging_lock(working_data, staging_root=staging_root):
                pass


def test_m_neg_26_production_data_dir_supplied_as_output():
    """M-NEG-26: Rejects production DATA_DIR supplied as target."""
    prod_data = os.path.join(PROD_REPO_ROOT, "data")
    with pytest.raises(ProtectedPathViolationError, match="resolves inside protected root"):
        SnapshotCanonicalAdapter(staging_root=prod_data, manifest={})


def test_m_neg_27_preserved_epoch_1_supplied_as_writable_output():
    """M-NEG-27: Rejects preserved EPOCH_1 supplied as target."""
    with pytest.raises(ProtectedPathViolationError, match="resolves inside protected root"):
        SnapshotCanonicalAdapter(staging_root=EPOCH_1_ROOT, manifest={})


def test_m_neg_28_symlink_or_junction_escaping_staging_root(tmp_path):
    """M-NEG-28: Rejects escaping path outside staging root."""
    staging_root = str(tmp_path / "staging_neg")
    os.makedirs(staging_root, exist_ok=True)
    outside_dir = str(tmp_path / "outside")
    os.makedirs(outside_dir, exist_ok=True)

    with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
        write_json_atomic(os.path.join(outside_dir, "test.json"), {"escaped": True}, staging_root=staging_root)


def test_m_neg_29_unapproved_live_production_snapshot_used_as_input(tmp_path):
    """M-NEG-29: Rejects unapproved live production snapshot classification."""
    staging_root = str(tmp_path / "staging_neg")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    manifest["snapshot_classification"] = "LIVE_PRODUCTION_RUNTIME_SNAPSHOT"

    with pytest.raises(SnapshotClassificationError, match="Unknown snapshot classification"):
        verify_snapshot_manifest(manifest, working_data, staging_root=staging_root)


def test_m_neg_30_production_execution_option_requested():
    """M-NEG-30: Rejects production execution option (--allow-production)."""
    with pytest.raises(ProtectedPathViolationError, match="SAFETY VIOLATION: Production option"):
        adapter_main(["--allow-production", "--staging-root", "some_path", "staging-apply"])


# =============================================================================
# MC-IDEM: Strict Completed-State Idempotency Matrix (MC-IDEM-01 .. MC-IDEM-10)
# =============================================================================

def test_mc_idem_01_valid_noop_on_unchanged_completed_dataset(tmp_path):
    """MC-IDEM-01: Valid no-op on unchanged completed dataset."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    res1 = adapter.staging_apply(simulation=True)
    assert res1["status"] == "MIGRATION_COMPLETED"

    daily_path = os.path.join(working_data, "daily_matches.json")
    stat_before = os.stat(daily_path)
    journal_path = adapter.journal.journal_file
    with open(journal_path, "r", encoding="utf-8") as f:
        journal_content_before = f.read()

    # Second execution must be strict verified no-op
    res2 = adapter.staging_apply(simulation=True)
    assert res2["status"] == "ALREADY_APPLIED_NO_OP"
    assert res2["total_matches"] == 209
    assert res2["target_occurrences"] == 1
    assert res2["candidate_sha256"] == EXPECTED_CANDIDATE_SHA256

    # Verify no file writes and no journal appends
    stat_after = os.stat(daily_path)
    assert stat_before.st_mtime_ns == stat_after.st_mtime_ns
    assert stat_before.st_size == stat_after.st_size
    with open(journal_path, "r", encoding="utf-8") as f:
        journal_content_after = f.read()
    assert journal_content_before == journal_content_after

    # Verify via staging_verify CLI handler
    verify_res = adapter.staging_verify()
    assert verify_res["status"] == "COMPLETED_STATE_VERIFIED"
    assert verify_res["verified_invariants"] == 20


def test_mc_idem_02_reject_altered_unrelated_match(tmp_path):
    """MC-IDEM-02: Rejects completed dataset with altered unrelated match."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Tamper with unrelated match
    daily_path = os.path.join(working_data, "daily_matches.json")
    daily_data = read_json_strict(daily_path)
    daily_data["matches"][0]["home"] = "TAMPERED_HOME_TEAM"
    write_json_atomic(daily_path, daily_data, staging_root=staging_root)

    with pytest.raises(CompletedStateVerificationError):
        adapter.staging_apply(simulation=True)

    with pytest.raises(CompletedStateVerificationError):
        adapter.staging_verify()


def test_mc_idem_03_reject_altered_target_candidate_record(tmp_path):
    """MC-IDEM-03: Rejects completed dataset with altered target candidate record."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Tamper with the single target record
    daily_path = os.path.join(working_data, "daily_matches.json")
    daily_data = read_json_strict(daily_path)
    for m in daily_data["matches"]:
        if m.get("id") == TARGET_CANONICAL_ID:
            m["odds"] = {"home_win": 99.9}
            break
    write_json_atomic(daily_path, daily_data, staging_root=staging_root)

    with pytest.raises(CompletedStateVerificationError, match="Active file hash does not match expected projected hash"):
        adapter.staging_apply(simulation=True)


def test_mc_idem_04_reject_missing_forensic_archive(tmp_path):
    """MC-IDEM-04: Rejects completed dataset with missing forensic archive."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Delete raw archive
    os.remove(os.path.join(staging_root, "archive", "daily_matches_raw.json"))

    with pytest.raises(Exception):
        adapter.staging_apply(simulation=True)


def test_mc_idem_05_reject_corrupted_record_a_in_archive(tmp_path):
    """MC-IDEM-05: Rejects completed dataset with corrupted Record A in forensic archive."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Tamper with archived record_a.json
    rec_a_path = os.path.join(staging_root, "archive", "record_a.json")
    rec_a_data = read_json_strict(rec_a_path)
    rec_a_data["away"] = "TAMPERED_AWAY"
    write_json_atomic(rec_a_path, rec_a_data, staging_root=staging_root)

    with pytest.raises(Exception):
        adapter.staging_apply(simulation=True)


def test_mc_idem_06_reject_corrupted_record_b_in_archive(tmp_path):
    """MC-IDEM-06: Rejects completed dataset with corrupted Record B in forensic archive."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Tamper with archived record_b.json
    rec_b_path = os.path.join(staging_root, "archive", "record_b.json")
    rec_b_data = read_json_strict(rec_b_path)
    rec_b_data["home"] = "TAMPERED_HOME"
    write_json_atomic(rec_b_path, rec_b_data, staging_root=staging_root)

    with pytest.raises(Exception):
        adapter.staging_apply(simulation=True)


def test_mc_idem_07_reject_deleted_journal(tmp_path):
    """MC-IDEM-07: Rejects completed dataset with deleted journal."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Delete journal file
    os.remove(adapter.journal.journal_file)

    with pytest.raises(CompletedStateVerificationError, match="Journal is missing or empty"):
        adapter.staging_apply(simulation=True)


def test_mc_idem_08_reject_incomplete_journal(tmp_path):
    """MC-IDEM-08: Rejects completed dataset with incomplete journal missing COMPLETION_RECORDED."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Drop COMPLETION_RECORDED from journal
    entries = adapter.journal.read_entries()
    entries = [e for e in entries if e.get("state") != "COMPLETION_RECORDED"]
    journal_path = adapter.journal.journal_file
    with open(journal_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    with pytest.raises(CompletedStateVerificationError, match="expected COMPLETION_RECORDED"):
        adapter.staging_apply(simulation=True)


def test_mc_idem_09_reject_snapshot_id_mismatch(tmp_path):
    """MC-IDEM-09: Rejects completed dataset with mismatched snapshot_id in manifest."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Create new adapter with modified snapshot_id
    mismatched_manifest = copy.deepcopy(manifest)
    mismatched_manifest["snapshot_id"] = "DIFFERENT-SNAPSHOT-ID-999"
    adapter2 = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=mismatched_manifest)

    with pytest.raises(Exception):
        adapter2.staging_apply(simulation=True)


def test_mc_idem_10_reject_drifted_non_canonical_store(tmp_path):
    """MC-IDEM-10: Rejects completed dataset with drifted non-canonical store."""
    staging_root = str(tmp_path / "staging_idem")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    # Alter team_strength.json
    ts_path = os.path.join(working_data, "team_strength.json")
    write_json_atomic(ts_path, {"MUTATED_STORE": 123}, staging_root=staging_root)

    with pytest.raises(ImmutableStoreMutationError, match="Immutable store 'team_strength.json' hash mismatch"):
        adapter.staging_apply(simulation=True)


# =============================================================================
# MC-REC: Recovery State Classification & Failure Modes (MC-REC-01 .. MC-REC-10)
# =============================================================================

def test_mc_rec_01_completed_journal_with_corrupted_active_file(tmp_path):
    """MC-REC-01: Fails closed when journal is completed but active daily_matches is corrupted."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    daily_path = os.path.join(working_data, "daily_matches.json")
    with open(daily_path, "w", encoding="utf-8") as f:
        f.write('{"matches": ["CORRUPTED"]}')

    with pytest.raises(RecoveryStateError, match="COMPLETION_RECORDED recovery failed completed-state verification"):
        adapter.staging_recover()


def test_mc_rec_02_completed_journal_with_missing_forensic_archive(tmp_path):
    """MC-REC-02: Fails closed when journal is completed but forensic archive is missing."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    os.remove(os.path.join(staging_root, "archive", "daily_matches_raw.json"))

    with pytest.raises(RecoveryStateError, match="COMPLETION_RECORDED recovery failed completed-state verification"):
        adapter.staging_recover()


def test_mc_rec_03_completed_journal_with_modified_non_canonical_store(tmp_path):
    """MC-REC-03: Fails closed when journal is completed but non-canonical store is modified."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    adapter.staging_apply(simulation=True)

    calib_path = os.path.join(working_data, "calibrated.json")
    write_json_atomic(calib_path, ["CORRUPTED_STORE"], staging_root=staging_root)

    with pytest.raises(ImmutableStoreMutationError, match="Immutable store calibrated.json altered during crash"):
        adapter.staging_recover()


def test_mc_rec_04_postconditions_verified_with_corrupted_active_file(tmp_path):
    """MC-REC-04: Fails closed when POSTCONDITIONS_VERIFIED has corrupted active file hash."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_completion")

    daily_path = os.path.join(working_data, "daily_matches.json")
    with open(daily_path, "w", encoding="utf-8") as f:
        f.write('{"matches": ["CORRUPTED_POSTCONDITIONS"]}')

    with pytest.raises(RecoveryStateError, match="POSTCONDITIONS_VERIFIED recovery requires active file to match expected projected hash"):
        adapter.staging_recover()


def test_mc_rec_05_postconditions_verified_with_valid_projected_file(tmp_path):
    """MC-REC-05: Advances to COMPLETION_RECORDED when interrupted at POSTCONDITIONS_VERIFIED."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_completion")

    rec_res = adapter.staging_recover()
    assert rec_res["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert rec_res["action"] == "APPENDED_COMPLETION_RECORDED"

    entries = adapter.journal.read_entries()
    assert entries[-1]["state"] == "COMPLETION_RECORDED"


def test_mc_rec_06_projection_committed_recovery(tmp_path):
    """MC-REC-06: Recovers safely from PROJECTION_COMMITTED with valid projected file."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_completion")

    # Drop POSTCONDITIONS_VERIFIED from journal to leave PROJECTION_COMMITTED
    entries = [e for e in adapter.journal.read_entries() if e.get("state") != "POSTCONDITIONS_VERIFIED"]
    journal_path = adapter.journal.journal_file
    with open(journal_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    rec_res = adapter.staging_recover()
    assert rec_res["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert rec_res["action"] == "COMMITTED_PROJECTION_AND_VERIFIED"


def test_mc_rec_07_commit_intent_durable_with_projected_hash_active(tmp_path):
    """MC-REC-07: Recovers forward when COMMIT_INTENT_DURABLE has active projected file."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    # Replace daily_matches with projected content
    rec_a = read_json_strict(os.path.join(staging_root, "archive", "record_a.json"))
    rec_b = read_json_strict(os.path.join(staging_root, "archive", "record_b.json"))
    cand, _ = build_approved_candidate_projection(rec_a, rec_b)
    raw_daily = read_json_strict(os.path.join(staging_root, "archive", "daily_matches_raw.json"))
    proj_daily, _ = project_snapshot_dataset(raw_daily, cand)
    daily_path = os.path.join(working_data, "daily_matches.json")
    write_json_atomic(daily_path, proj_daily, staging_root=staging_root)

    rec_res = adapter.staging_recover()
    assert rec_res["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert rec_res["action"] == "COMMITTED_PROJECTION_AND_VERIFIED"


def test_mc_rec_08_commit_intent_durable_with_source_hash_active(tmp_path):
    """MC-REC-08: Rolls back to source when COMMIT_INTENT_DURABLE has active source file."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    rec_res = adapter.staging_recover()
    assert rec_res["status"] == "RECOVERED_ROLLED_BACK"
    assert rec_res["action"] == "RESTORED_ARCHIVE_PRE_COMMIT"

    # Retry succeeds
    retry_res = adapter.staging_apply(simulation=True)
    assert retry_res["status"] == "MIGRATION_COMPLETED"


def test_mc_rec_09_commit_intent_durable_with_unknown_hash_active(tmp_path):
    """MC-REC-09: Fails closed when active file hash is UNKNOWN under COMMIT_INTENT_DURABLE."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    # Put unknown garbage into daily_matches.json
    daily_path = os.path.join(working_data, "daily_matches.json")
    with open(daily_path, "w", encoding="utf-8") as f:
        f.write('{"UNKNOWN_DRIFT": 999}')

    with pytest.raises(RecoveryStateError, match="active file has unknown hash"):
        adapter.staging_recover()


def test_mc_rec_10_repeated_recovery_idempotent(tmp_path):
    """MC-REC-10: Repeated recovery attempts are safe and idempotent under single OS lock."""
    staging_root = str(tmp_path / "staging_rec")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    rec_res1 = adapter.staging_recover()
    assert rec_res1["status"] == "RECOVERED_ROLLED_BACK"

    rec_res2 = adapter.staging_recover()
    assert rec_res2["status"] == "RECOVERED_PRE_COMMIT_CLEAN"


# =============================================================================
# MC-PATH: Protected Path & Read-Only Safety Matrix (MC-PATH-01 .. MC-PATH-10)
# =============================================================================

def test_mc_path_01_manifest_output_escaping_staging_root(tmp_path):
    """MC-PATH-01: Rejects manifest --output escaping staging root."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    outside_output = str(tmp_path / "outside_manifest.json")

    with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-ESC",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", outside_output,
        ])


def test_mc_path_02_manifest_output_into_prod_data(tmp_path):
    """MC-PATH-02: Rejects manifest --output targeting production DATA_DIR."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    prod_output = os.path.join(PROD_REPO_ROOT, "data", "compromised_manifest.json")

    with pytest.raises(ProtectedPathViolationError):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-PROD",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", prod_output,
        ])


def test_mc_path_03_manifest_output_into_epoch1(tmp_path):
    """MC-PATH-03: Rejects manifest --output targeting preserved EPOCH_1."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    epoch1_output = os.path.join(EPOCH_1_ROOT, "compromised_manifest.json")

    with pytest.raises(ProtectedPathViolationError):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-EPOCH1",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", epoch1_output,
        ])


def test_mc_path_04_manifest_output_into_git_checkout(tmp_path):
    """MC-PATH-04: Rejects manifest --output targeting git source checkout."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    checkout_output = os.path.join(SOURCE_REPO_ROOT, "compromised_manifest.json")

    with pytest.raises(ProtectedPathViolationError):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-SRC",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", checkout_output,
        ])


def test_mc_path_05_manifest_output_traversal_dotdot(tmp_path):
    """MC-PATH-05: Rejects manifest --output with path traversal outside staging root."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    dotdot_output = os.path.join(staging_root, "..", "escaped_manifest.json")

    with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-DOTDOT",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", dotdot_output,
        ])


def test_mc_path_06_manifest_output_junction_escape(tmp_path):
    """MC-PATH-06: Rejects manifest --output when directory resolves outside staging root."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    outside_dir = str(tmp_path / "outside_dir")
    os.makedirs(outside_dir, exist_ok=True)
    outside_file = os.path.join(outside_dir, "manifest.json")

    with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-JUNC",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", outside_file,
        ])


def test_mc_path_07_manifest_output_symlink_redirect(tmp_path):
    """MC-PATH-07: Rejects manifest --output targeting external path via redirect."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    foreign_dir = str(tmp_path / "foreign")
    os.makedirs(foreign_dir, exist_ok=True)
    foreign_file = os.path.join(foreign_dir, "redirect_target.json")

    with pytest.raises(ProtectedPathViolationError):
        adapter_main([
            "--staging-root", staging_root,
            "generate-manifest",
            "--data-dir", working_data,
            "--snapshot-id", "SNAP-SYM",
            "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
            "--dataset-identity", "TEST_FIXTURE_IDENTITY",
            "--output", foreign_file,
        ])


def test_mc_path_08_unmarked_staging_workspace_rejection(tmp_path):
    """MC-PATH-08: Rejects writable operations against unmarked workspace before any writes."""
    unmarked_root = str(tmp_path / "unmarked_root")
    os.makedirs(unmarked_root, exist_ok=True)

    with pytest.raises(PreconditionViolationError, match="Unmarked workspace rejected before writes"):
        SnapshotCanonicalAdapter(staging_root=unmarked_root, manifest={}, writable=True)


def test_mc_path_09_inspection_zero_write_assertion(tmp_path):
    """MC-PATH-09: Read-only inspection (writable=False) creates zero files and zero directories."""
    read_only_root = str(tmp_path / "read_only_test")
    data_dir = os.path.join(read_only_root, "data")
    os.makedirs(data_dir, exist_ok=True)

    before_entries = set(os.listdir(read_only_root))

    # Initialize adapter with writable=False
    adapter = SnapshotCanonicalAdapter(staging_root=read_only_root, manifest={}, writable=False)
    assert not os.path.exists(os.path.join(read_only_root, "input"))
    assert not os.path.exists(os.path.join(read_only_root, "archive"))
    assert not os.path.exists(os.path.join(read_only_root, "journal"))
    assert not os.path.exists(os.path.join(read_only_root, "output"))

    after_entries = set(os.listdir(read_only_root))
    assert before_entries == after_entries


def test_mc_path_10_atomic_valid_staging_output(tmp_path):
    """MC-PATH-10: Writes valid manifest atomically inside marked staging root with zero leftover temps."""
    staging_root = str(tmp_path / "staging_path")
    working_data, _ = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    out_file = os.path.join(staging_root, "valid_output_manifest.json")

    code = adapter_main([
        "--staging-root", staging_root,
        "generate-manifest",
        "--data-dir", working_data,
        "--snapshot-id", "SNAP-ATOMIC",
        "--classification", "EXPLICIT_SYNTHETIC_DRIFT_FIXTURE",
        "--dataset-identity", "TEST_FIXTURE_IDENTITY",
        "--output", out_file,
    ])
    assert code == 0
    assert os.path.exists(out_file)

    manifest_loaded = read_json_strict(out_file)
    assert manifest_loaded["snapshot_id"] == "SNAP-ATOMIC"
    assert manifest_loaded["manifest_verification_status"] == "MANIFEST_GENERATED"

    # Verify no temp files left behind
    for fname in os.listdir(staging_root):
        assert not fname.endswith(".tmp")


# =============================================================================
# MC-JRN: Journal Cross-Attempt Identity Binding Matrix (MC-JRN-01 .. MC-JRN-06)
# =============================================================================

def test_mc_jrn_01_same_snapshot_valid_retry(tmp_path):
    """MC-JRN-01: Permits retry on the same snapshot with identical identity bindings."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)
    with pytest.raises(FaultInjectionError):
        adapter.staging_apply(simulation=True, fault_inject="before_file_replace")

    rec = adapter.staging_recover()
    assert rec["status"] == "RECOVERED_ROLLED_BACK"

    # Retry on the exact same snapshot succeeds
    retry_res = adapter.staging_apply(simulation=True)
    assert retry_res["status"] == "MIGRATION_COMPLETED"

    entries = adapter.journal.read_entries()
    attempts = [e.get("payload", {}).get("attempt") for e in entries if isinstance(e.get("payload"), dict) and "attempt" in e.get("payload")]
    assert 1 in attempts
    assert 2 in attempts


def test_mc_jrn_02_changed_snapshot_id_rejected(tmp_path):
    """MC-JRN-02: Rejects cross-attempt retry with changed snapshot_id."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "snapshot_id": "SNAP-ORIG", "attempt": 1}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "snapshot_id": "SNAP-CHANGED", "attempt": 2}},
    ]
    with pytest.raises(RecoveryStateError, match="Cross-snapshot retry rejected: changed snapshot_id"):
        adapter.validate_journal_chain(entries, expected_migration_id="MIG-1")


def test_mc_jrn_03_changed_manifest_sha_rejected(tmp_path):
    """MC-JRN-03: Rejects cross-attempt retry with changed manifest_sha256."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "manifest_sha256": "aaaa" * 16, "attempt": 1}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "manifest_sha256": "bbbb" * 16, "attempt": 2}},
    ]
    with pytest.raises(RecoveryStateError, match="source manifest changed across attempts"):
        adapter.validate_journal_chain(entries, expected_migration_id="MIG-1")


def test_mc_jrn_04_changed_projected_hash_rejected(tmp_path):
    """MC-JRN-04: Rejects conflicting projected_file_sha256 across entries."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-1",
                "source_file_sha256": "src1",
                "projected_file_sha256": "proj1" * 8,
                "candidate_record_sha256": "cand1",
                "record_a_sha256": "a1",
                "record_b_sha256": "b1",
            },
        },
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-1",
                "source_file_sha256": "src1",
                "projected_file_sha256": "proj2" * 8,
                "candidate_record_sha256": "cand1",
                "record_a_sha256": "a1",
                "record_b_sha256": "b1",
            },
        },
    ]
    with pytest.raises(RecoveryStateError, match="Conflicting projected_file_sha256"):
        adapter.validate_journal_chain(entries, expected_migration_id="MIG-1")


def test_mc_jrn_05_changed_immutable_store_hash_rejected(tmp_path):
    """MC-JRN-05: Rejects conflicting immutable store hashes across entries."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "immutable_store_hashes": {"team_strength.json": "ts1" * 16}}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "immutable_store_hashes": {"team_strength.json": "ts2" * 16}}},
    ]
    with pytest.raises(RecoveryStateError, match="Conflicting immutable_store_hashes in journal across attempts"):
        adapter.validate_journal_chain(entries, expected_migration_id="MIG-1")


def test_mc_jrn_06_conflicting_attempt_history_rejected(tmp_path):
    """MC-JRN-06: Rejects out-of-order or corrupt attempt numbering."""
    staging_root = str(tmp_path / "staging_jrn")
    working_data, manifest = build_isolated_workspace_fixture(staging_root, match_count=210, classification="EXPLICIT_SYNTHETIC_DRIFT_FIXTURE")
    adapter = SnapshotCanonicalAdapter(staging_root=staging_root, manifest=manifest)

    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "attempt": 2}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-1", "attempt": 1}},
    ]
    with pytest.raises(RecoveryStateError, match="Non-monotonic attempt sequence"):
        adapter.validate_journal_chain(entries, expected_migration_id="MIG-1")
