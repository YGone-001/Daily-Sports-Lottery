"""
tests/test_staging_canonical_repair.py
======================================
Comprehensive automated test suite for Guarded Historical Canonical Staging Migration.
Covers:
- Positive Acceptance Criteria: I-POS-01 through I-POS-24
- Negative and Fault-Injection Criteria: I-NEG-01 through I-NEG-25
- Phase I-C Regression Suite: IC-REG-01 through IC-REG-15
- Path safety boundaries, junction escapes, and Windows lock contention
- Portable synthetic fixtures runnable offline anywhere
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import pytest

from tools.staging_canonical_repair import (
    StagingCanonicalMigrator,
    StagingJournal,
    ProtectedPathViolationError,
    PreconditionViolationError,
    StagingLockError,
    ImmutableStoreMutationError,
    FaultInjectionError,
    RecoveryStateError,
    StagingMigrationError,
    assert_safe_path,
    canonicalize_path,
    is_subpath_or_equal,
    build_candidate_projection,
    hash_record,
    serialize_record,
    init_staging_workspace,
    verify_staging_workspace,
    restore_from_archive_atomic,
    TARGET_CANONICAL_ID,
    EXPECTED_RECORD_A_SHA256,
    EXPECTED_RECORD_B_SHA256,
    EXPECTED_CANDIDATE_SHA256,
    NON_CANONICAL_STORES,
    PROTECTED_ROOTS,
    LEGACY_PROTECTED_ROOTS,
    LEGACY_PROD_REPO_ROOT,
    SOURCE_REPO_ROOT,
    SOURCE_DATA_DIR,
    PROD_REPO_ROOT,
    EPOCH_0_ROOT,
    EPOCH_1_ROOT,
    STAGING_MARKER_FILENAME,
    acquire_staging_lock,
    containment_relation,
)

# Canonical Record A from historical evidence
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

# Canonical Record B from historical evidence
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


def create_synthetic_staging_workspace(
    root_dir: str,
    create_marker: bool = True,
    dataset_identity: str = "EXPLICIT_SYNTHETIC_TEST_FIXTURE",
) -> dict:
    """
    Construct a complete, portable synthetic staging workspace with 193 records,
    Record A at index 55, Record B at index 56, and all 6 non-Canonical stores.
    Also initializes staging_marker.json with bound baseline manifest.
    """
    input_data = os.path.join(root_dir, "input", "data")
    working_data = os.path.join(root_dir, "working", "data")
    os.makedirs(input_data, exist_ok=True)
    os.makedirs(working_data, exist_ok=True)

    # 1. Matches: 55 dummy matches + Record A + Record B + 136 dummy matches = 193 matches
    matches = []
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

    matches.append(copy.deepcopy(RECORD_A_FIXTURE))
    matches.append(copy.deepcopy(RECORD_B_FIXTURE))

    for i in range(136):
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
            "sources": ["500.com", "jczq_xml"],
        }
    }

    # 2. Six non-Canonical stores
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
                "odds_2": {"match_id": TARGET_CANONICAL_ID, "h": 4.70},
                "odds_3": {"match_id": TARGET_CANONICAL_ID, "h": 4.80},
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

    # 3. Write to input and working
    for path_dir in (input_data, working_data):
        with open(os.path.join(path_dir, "daily_matches.json"), "w", encoding="utf-8") as f:
            json.dump(daily_payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        for sname, sdata in stores.items():
            with open(os.path.join(path_dir, sname), "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, indent=2, sort_keys=True)

    # 4. Calculate baseline manifest and projected file hash
    daily_matches_path = os.path.join(working_data, "daily_matches.json")
    source_daily_hash = hashlib.sha256(open(daily_matches_path, "rb").read()).hexdigest()

    cand, _ = build_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)
    projected_matches = matches[:55] + [cand] + matches[57:]
    projected_payload = {
        "matches": projected_matches,
        "meta": copy.deepcopy(daily_payload["meta"]),
    }
    projected_payload["meta"]["total"] = 192
    projected_str = json.dumps(projected_payload, ensure_ascii=False, indent=2, sort_keys=True)
    if os.name == "nt":
        projected_str = projected_str.replace("\n", "\r\n")
    projected_daily_hash = hashlib.sha256(projected_str.encode("utf-8")).hexdigest()

    manifest = {
        "daily_matches.json": source_daily_hash,
        "projected_file_sha256": projected_daily_hash,
    }
    for sname in stores:
        store_path = os.path.join(working_data, sname)
        manifest[sname] = hashlib.sha256(open(store_path, "rb").read()).hexdigest()

    if create_marker:
        init_staging_workspace(
            staging_root=root_dir,
            dataset_identity=dataset_identity,
            baseline_manifest=manifest,
        )

    return {
        "root": root_dir,
        "input_data": input_data,
        "working_data": working_data,
        "daily_matches_path": daily_matches_path,
        "manifest": manifest,
        "source_daily_hash": source_daily_hash,
        "projected_daily_hash": projected_daily_hash,
    }


# =============================================================================
# Positive Tests: I-POS-01 .. I-POS-24
# =============================================================================

def test_record_fingerprints_match_authoritative_baseline():
    """I-POS-06 & I-POS-07: Verify Record A and B hash calculations match expectations."""
    assert hash_record(RECORD_A_FIXTURE) == EXPECTED_RECORD_A_SHA256
    assert hash_record(RECORD_B_FIXTURE) == EXPECTED_RECORD_B_SHA256


def test_field_crosswalk_projection():
    """I-POS-08, I-POS-11, I-POS-12, I-POS-13: Validate candidate synthesis."""
    cand, diff = build_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)
    assert cand["id"] == TARGET_CANONICAL_ID
    assert cand["home"] == "巴拉纳竞技"  # Full club name from Record B
    assert cand["away"] == "米内罗竞技"  # Full club name from Record B
    assert cand["status"] == "finished"
    assert cand["score"] == {"ft": [2, 2], "ht": [1, 1]}
    assert cand["jczq_no"] == "4004"  # From Record A
    assert cand["odds"]["home_win"] == 4.75  # Closing odds from Record A
    assert cand["odds_updated_at"] == "2026-10-08T21:19:44.432817+08:00"
    assert hash_record(cand) == EXPECTED_CANDIDATE_SHA256
    assert diff["projection_status"] == "PROPOSED_NOT_APPROVED"


def test_dry_run_generates_preview_without_mutation(tmp_path):
    """Dry-run produces preview artifacts and does NOT mutate working Canonical data."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    pre_hash = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    res = migrator.dry_run()

    assert res["status"] == "DRY_RUN_COMPLETED"
    assert res["candidate_sha256"] == EXPECTED_CANDIDATE_SHA256
    assert os.path.exists(res["preview_file"])
    assert os.path.exists(res["diff_file"])

    post_hash = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    assert pre_hash == post_hash, "dry-run must not mutate daily_matches.json!"


def test_staging_apply_full_lifecycle(tmp_path):
    """
    I-POS-03..05, I-POS-09..10, I-POS-14..16, I-POS-21:
    Full staging migration lifecycle.
    """
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Pre-inspect
    pre_report = migrator.inspect_dataset(ws["working_data"])
    assert pre_report["total_matches"] == 193
    assert pre_report["unique_ids_count"] == 192
    assert pre_report["target_occurrences"] == 2

    pre_store_hashes = {k: v for k, v in pre_report["store_hashes"].items() if k != "daily_matches.json"}

    # Apply migration
    apply_res = migrator.staging_apply(simulation=True)
    assert apply_res["status"] == "SUCCESS"
    assert apply_res["before_total"] == 193
    assert apply_res["after_total"] == 192
    assert apply_res["unique_ids"] == 192
    assert apply_res["candidate_sha256"] == EXPECTED_CANDIDATE_SHA256

    # Verify forensic archive
    archive_raw = os.path.join(ws["root"], "archive", "daily_matches_raw.json")
    assert os.path.exists(archive_raw)
    assert hashlib.sha256(open(archive_raw, "rb").read()).hexdigest() == pre_report["store_hashes"]["daily_matches.json"]

    # Post-inspect
    post_report = migrator.inspect_dataset(ws["working_data"])
    assert post_report["total_matches"] == 192
    assert post_report["unique_ids_count"] == 192
    assert post_report["target_occurrences"] == 1
    assert len(post_report["duplicate_ids"]) == 0

    # Verify 6 non-Canonical stores unchanged!
    post_store_hashes = {k: v for k, v in post_report["store_hashes"].items() if k != "daily_matches.json"}
    assert pre_store_hashes == post_store_hashes

    # Verify journal contains all 7 states
    entries = migrator.journal.read_entries()
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


def test_idempotency_second_run_is_noop(tmp_path):
    """I-POS-20: Second execution is a no-op."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    res1 = migrator.staging_apply(simulation=True)
    assert res1["status"] == "SUCCESS"

    journal_entries_count = len(migrator.journal.read_entries())
    file_hash_after_run1 = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()

    # Second run
    res2 = migrator.staging_apply(simulation=True)
    assert res2["status"] == "ALREADY_APPLIED_NO_OP"

    file_hash_after_run2 = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    assert file_hash_after_run1 == file_hash_after_run2
    assert len(migrator.journal.read_entries()) == journal_entries_count, "No duplicate journal entries!"


# =============================================================================
# Negative Tests: I-NEG-01 .. I-NEG-25
# =============================================================================

def test_neg_01_wrong_canonical_source_hash(tmp_path):
    """I-NEG-01: Reject if expected source hash does not match baseline manifest."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    # Tamper with the daily_matches.json file so its hash differs from manifest binding
    with open(ws["daily_matches_path"], "a", encoding="utf-8") as f:
        f.write(" ")

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Canonical source hash mismatch"):
        migrator.staging_apply(simulation=True)


def test_neg_02_wrong_record_a_hash(tmp_path):
    """I-NEG-02: Tampered Record A causes rejection."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"][55]["home"] = "篡改球队"
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    # Update manifest daily_matches.json hash to match so it reaches Record A check
    new_h = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    ws["manifest"]["daily_matches.json"] = new_h
    init_staging_workspace(ws["root"], "EXPLICIT_SYNTHETIC_TEST_FIXTURE", ws["manifest"])

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Record A hash mismatch"):
        migrator.staging_apply(simulation=True)


def test_neg_03_wrong_record_b_hash(tmp_path):
    """I-NEG-03: Tampered Record B causes rejection."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"][56]["away"] = "篡改球队"
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    # Update manifest daily_matches.json hash to match so it reaches Record B check
    new_h = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    ws["manifest"]["daily_matches.json"] = new_h
    init_staging_workspace(ws["root"], "EXPLICIT_SYNTHETIC_TEST_FIXTURE", ws["manifest"])

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Record B hash mismatch"):
        migrator.staging_apply(simulation=True)


def test_neg_04_missing_target_record(tmp_path):
    """I-NEG-04: Target record missing."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"] = [m for m in d["matches"] if m.get("id") != TARGET_CANONICAL_ID]
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError):
        migrator.staging_apply(simulation=True)


def test_neg_05_unexpected_third_target_duplicate(tmp_path):
    """I-NEG-05: Unexpected third duplicate record."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"].append(copy.deepcopy(RECORD_A_FIXTURE))
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError):
        migrator.staging_apply(simulation=True)


def test_neg_06_unrelated_duplicate_id(tmp_path):
    """I-NEG-06: Unrelated duplicate ID detected."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"][0]["id"] = d["matches"][1]["id"]  # Duplicate unrelated ID
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    new_h = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    ws["manifest"]["daily_matches.json"] = new_h
    init_staging_workspace(ws["root"], "EXPLICIT_SYNTHETIC_TEST_FIXTURE", ws["manifest"])

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Unrelated duplicate IDs detected"):
        migrator.staging_apply(simulation=True)


def test_neg_07_malformed_json(tmp_path):
    """I-NEG-07: Malformed JSON syntax fails cleanly."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        f.write("{ invalid json [")

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Malformed JSON"):
        migrator.staging_apply(simulation=True)


def test_neg_08_malformed_final_score(tmp_path):
    """I-NEG-08: Malformed score structure in Record B fails."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"][56]["score"] = {"ft": [-1, 2]}  # Invalid negative score
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)

    new_h = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    ws["manifest"]["daily_matches.json"] = new_h
    init_staging_workspace(ws["root"], "EXPLICIT_SYNTHETIC_TEST_FIXTURE", ws["manifest"])

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Record B hash mismatch"):
        migrator.staging_apply(simulation=True)


def test_neg_09_missing_immutable_store(tmp_path):
    """I-NEG-09: Missing immutable store causes failure."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    os.remove(os.path.join(ws["working_data"], "team_strength.json"))

    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    with pytest.raises(PreconditionViolationError, match="Required store missing"):
        migrator.staging_apply(simulation=True)


def test_neg_10_through_15_modified_stores(tmp_path):
    """I-NEG-10..15: Mutation of any immutable store triggers ImmutableStoreMutationError."""
    for store in NON_CANONICAL_STORES:
        ws = create_synthetic_staging_workspace(str(tmp_path / f"ws_{store}"))
        migrator = StagingCanonicalMigrator(staging_root=ws["root"])

        # Fault-inject after PREPARED / before postconditions
        with pytest.raises(FaultInjectionError):
            migrator.staging_apply(simulation=True, fault_inject="before_postconditions")

        # Mutate the store file
        target_store_file = os.path.join(ws["working_data"], store)
        with open(target_store_file, "a", encoding="utf-8") as f:
            f.write(" ")

        # Run recovery, which detects the mutation and raises ImmutableStoreMutationError
        with pytest.raises(ImmutableStoreMutationError, match="mutated prior to recovery"):
            migrator.staging_recover()


def test_neg_16_stale_source_file_detection(tmp_path):
    """I-NEG-16: Concurrent external modification before replacement fails closed."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Simulate crash right before file replacement
    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="before_file_replace")

    # Modify daily_matches.json externally
    with open(ws["daily_matches_path"], "a", encoding="utf-8") as f:
        f.write(" ")

    # Re-running apply must fail due to hash mismatch or recovery
    with pytest.raises(PreconditionViolationError):
        migrator.staging_apply(simulation=True)


def test_neg_17_concurrent_writer_locking(tmp_path):
    """I-NEG-17: Concurrent staging lock acquisition fails closed."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with acquire_staging_lock(ws["working_data"]):
        with pytest.raises(StagingLockError, match="Staging lock busy or denied"):
            migrator.staging_apply(simulation=True)


def test_neg_18_corrupted_journal_fails_recovery(tmp_path):
    """I-NEG-18: Corrupted journal line triggers RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Write broken journal line
    journal_path = os.path.join(ws["root"], "journal", "disposition_journal.jsonl")
    os.makedirs(os.path.dirname(journal_path), exist_ok=True)
    with open(journal_path, "w", encoding="utf-8") as f:
        f.write('{"state": "PREPARED"}\n')
        f.write('BROKEN LINE NOT JSON\n')

    with pytest.raises(RecoveryStateError, match="Corrupted journal line detected"):
        migrator.staging_recover()


def test_neg_19_crash_before_commit_intent_recovers_cleanly(tmp_path):
    """I-NEG-19: Interruption before commit intent resets safely."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="before_commit_intent")

    # Recover
    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_PRE_COMMIT_CLEAN"


def test_neg_20_crash_after_commit_intent_recovers(tmp_path):
    """I-NEG-20: Interruption after commit intent rolls back from archive."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_commit_intent")

    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_ROLLED_BACK"


def test_neg_21_crash_after_projection_replacement_recovers_forward(tmp_path):
    """I-NEG-21: Interruption after file replacement completes forward."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_COMPLETED_FORWARD"
    assert rec["after_total"] == 192


def test_neg_22_reused_migration_id(tmp_path):
    """I-NEG-22: Reusing migration ID with conflicting intent fails."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Simulate interruption after commit intent
    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, migration_id="MIG-TEST-001", fault_inject="after_commit_intent")

    # Recover to clean state
    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_ROLLED_BACK"

    # Modify the previously recorded intent in the journal to have a conflicting target hash
    journal_path = os.path.join(ws["root"], "journal", "disposition_journal.jsonl")
    entries = migrator.journal.read_entries()
    for e in entries:
        if e.get("state") == "COMMIT_INTENT_DURABLE":
            e["payload"]["projected_file_sha256"] = "CONFLICTING_TARGET_HASH"
    with open(journal_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")

    # Re-running staging_apply with same migration_id detects conflicting intent
    with pytest.raises(PreconditionViolationError, match="Reused migration ID"):
        migrator.staging_apply(simulation=True, migration_id="MIG-TEST-001")


def test_neg_23_protected_production_path_rejected():
    """I-NEG-23: Reject production repo paths."""
    prod_paths = [
        PROD_REPO_ROOT,
        os.path.join(PROD_REPO_ROOT, "data"),
        PROD_REPO_ROOT.lower(),
        PROD_REPO_ROOT.replace("\\", "/"),
    ]
    for p in prod_paths:
        with pytest.raises(ProtectedPathViolationError):
            assert_safe_path(p)


def test_neg_24_protected_evidence_epoch_rejected():
    """I-NEG-24: Reject evidence epoch paths."""
    epoch_paths = [
        EPOCH_0_ROOT,
        EPOCH_1_ROOT,
        os.path.join(EPOCH_0_ROOT, "data"),
        os.path.join(EPOCH_1_ROOT, "data"),
    ]
    for p in epoch_paths:
        with pytest.raises(ProtectedPathViolationError):
            assert_safe_path(p)


def test_neg_25_path_traversal_and_junction_escape(tmp_path):
    """I-NEG-25: Reject path traversal attempts resolving into protected roots and escaping staging root."""
    # 1. Traversal resolving into production repo
    traversal_path = os.path.join(PROD_REPO_ROOT, "data", "..")
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(traversal_path)

    # 2. Traversal resolving into epoch backup
    epoch_traversal = os.path.join(EPOCH_1_ROOT, "data", "..")
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(epoch_traversal)

    # 3. Traversal escaping staging root
    staging_ws = str(tmp_path / "escape_ws")
    os.makedirs(staging_ws, exist_ok=True)
    escaping_path = os.path.join(staging_ws, "..", "escaped_outside")
    with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
        assert_safe_path(escaping_path, staging_root=staging_ws)


# =============================================================================
# CLI Interface Integration Tests
# =============================================================================

def test_cli_lifecycle(tmp_path):
    """Test inspect, dry-run, staging-apply, staging-verify, and recover via CLI main()."""
    from tools.staging_canonical_repair import main

    ws = create_synthetic_staging_workspace(str(tmp_path / "cli_ws"))

    # 1. inspect (read-only)
    rc = main(["inspect", "--data-dir", ws["working_data"]])
    assert rc == 0

    # 2. dry-run
    rc = main(["dry-run", "--staging-dir", ws["root"]])
    assert rc == 0

    # 3. staging-apply
    rc = main(["staging-apply", "--staging-dir", ws["root"], "--simulation"])
    assert rc == 0

    # 4. staging-verify
    rc = main(["staging-verify", "--staging-dir", ws["root"]])
    assert rc == 0

    # 5. staging-recover (idempotent / already complete)
    rc = main(["staging-recover", "--staging-dir", ws["root"]])
    assert rc == 0


# =============================================================================
# Phase I-C Regression Suite: IC-REG-01 through IC-REG-15
# =============================================================================

def test_ic_reg_01_production_path_bypass_attempt(tmp_path):
    """IC-REG-01: Rigorous rejection of production repo, epoch backups, and source repo."""
    # Direct production roots
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(PROD_REPO_ROOT)
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(os.path.join(PROD_REPO_ROOT, "data"))

    # Source repo checkout (dsl-inc-i)
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(SOURCE_REPO_ROOT)

    # Epoch backup paths
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(EPOCH_0_ROOT)
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(EPOCH_1_ROOT)

    # Any subpath containing _quiescent_evidence_backup
    fake_epoch = str(tmp_path / "_quiescent_evidence_backup_custom" / "data")
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(fake_epoch)


def test_ic_reg_02_read_only_inspect_creates_no_files_or_dirs(tmp_path):
    """IC-REG-02: inspect_dataset is strictly read-only and creates zero files or directories."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "inspect_ws"))

    def get_fs_state(root):
        state = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                state[os.path.relpath(fp, root)] = os.path.getmtime(fp)
        return state

    pre_state = get_fs_state(ws["root"])

    # Run inspect
    res = StagingCanonicalMigrator.inspect_dataset(ws["working_data"])
    assert res["total_matches"] == 193
    assert res["unique_ids_count"] == 192
    assert res["target_occurrences"] == 2

    post_state = get_fs_state(ws["root"])
    assert pre_state == post_state, "inspect_dataset must not create or modify ANY file or directory!"


def test_ic_reg_03_file_and_record_hash_separation():
    """IC-REG-03: Whole-file hashes and record hashes are strictly distinguished."""
    cand, _ = build_candidate_projection(RECORD_A_FIXTURE, RECORD_B_FIXTURE)
    rec_hash = hash_record(cand)
    assert rec_hash == EXPECTED_CANDIDATE_SHA256

    # Entire file containing candidate has a completely different hash from individual record hash
    wrapper_bytes = json.dumps({"matches": [cand]}, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    file_hash = hashlib.sha256(wrapper_bytes).hexdigest()
    assert rec_hash != file_hash, "Record hash and file hash must never be conflated!"


def test_ic_reg_04_single_lock_recovery_no_recursion(tmp_path):
    """IC-REG-04: Recovery executes under a single lock and never deadlocks or calls itself recursively."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "lock_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Fault-inject to leave dirty state
    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_commit_intent")

    # Single-lock recovery succeeds without deadlock or recursion
    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_ROLLED_BACK"


def test_ic_reg_05_explicit_file_state_classification(tmp_path):
    """IC-REG-05: Recovery classifies Canonical file into baseline, projected, or unknown state."""
    # 1. State 1: EXPECTED_BASELINE
    ws1 = create_synthetic_staging_workspace(str(tmp_path / "state_ws1"))
    migrator1 = StagingCanonicalMigrator(staging_root=ws1["root"])
    migrator1.journal.append_entry("PREPARED", {
        "migration_id": "MIG-20261011-CANONICAL-500L-1362422",
        "pre_store_hashes": migrator1.get_store_hashes(ws1["working_data"]),
    })
    rec1 = migrator1.staging_recover()
    assert rec1["status"] == "RECOVERED_PRE_COMMIT_CLEAN"

    # 2. State 2: EXPECTED_PROJECTED
    ws2 = create_synthetic_staging_workspace(str(tmp_path / "state_ws2"))
    migrator2 = StagingCanonicalMigrator(staging_root=ws2["root"])
    with pytest.raises(FaultInjectionError):
        migrator2.staging_apply(simulation=True, fault_inject="after_file_replace")
    rec2 = migrator2.staging_recover()
    assert rec2["status"] == "RECOVERED_COMPLETED_FORWARD"

    # 3. State 3: UNKNOWN -> fails closed
    ws3 = create_synthetic_staging_workspace(str(tmp_path / "state_ws3"))
    migrator3 = StagingCanonicalMigrator(staging_root=ws3["root"])
    with pytest.raises(FaultInjectionError):
        migrator3.staging_apply(simulation=True, fault_inject="after_file_replace")
    with open(ws3["daily_matches_path"], "a", encoding="utf-8") as f:
        f.write("\n/* UNKNOWN_CORRUPTION */")
    with pytest.raises(RecoveryStateError, match="UNKNOWN state"):
        migrator3.staging_recover()


def test_ic_reg_06_atomic_recovery_write_and_verification(tmp_path):
    """IC-REG-06: Atomic recovery restoration with flush, sync, and post-replacement hash validation."""
    staging_root = str(tmp_path / "atomic_ws")
    os.makedirs(staging_root, exist_ok=True)
    archive_file = os.path.join(staging_root, "source.json")
    target_file = os.path.join(staging_root, "target.json")

    content = b'{"atomic": true}'
    expected_h = hashlib.sha256(content).hexdigest()
    with open(archive_file, "wb") as f:
        f.write(content)

    # Successful atomic restore
    restore_from_archive_atomic(archive_file, target_file, expected_hash=expected_h, staging_root=staging_root)
    assert os.path.exists(target_file)
    assert open(target_file, "rb").read() == content

    # Tampered expected hash raises RecoveryStateError
    with pytest.raises(RecoveryStateError, match="hash mismatch"):
        restore_from_archive_atomic(archive_file, target_file, expected_hash="0000000000", staging_root=staging_root)


def test_ic_reg_07_journal_consistency_and_state_sequence(tmp_path):
    """IC-REG-07: Journal state machine enforces strict linear sequencing and no state regressions."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "seq_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Linear progression followed by regression to EVIDENCE_VERIFIED
    migrator.journal.append_entry("PREPARED", {"migration_id": "MIG-01"})
    migrator.journal.append_entry("EVIDENCE_VERIFIED", {"migration_id": "MIG-01"})
    migrator.journal.append_entry("AUTHORIZED_FOR_STAGING_SIMULATION", {"migration_id": "MIG-01"})
    migrator.journal.append_entry(
        "COMMIT_INTENT_DURABLE",
        {
            "migration_id": "MIG-01",
            "source_file_sha256": "hash_s",
            "projected_file_sha256": "hash_p",
            "candidate_record_sha256": "hash_c",
            "record_a_sha256": "hash_a",
            "record_b_sha256": "hash_b",
        },
    )
    migrator.journal.append_entry("EVIDENCE_VERIFIED", {"migration_id": "MIG-01"})

    with pytest.raises(RecoveryStateError, match="Journal state regression detected"):
        migrator.staging_recover()


def test_ic_reg_08_lock_gated_idempotency_verification(tmp_path):
    """IC-REG-08: Idempotency verification executes under exclusive lock and verifies all invariants."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "idem_lock_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    res1 = migrator.staging_apply(simulation=True)
    assert res1["status"] == "SUCCESS"

    # Second run inside lock returns ALREADY_APPLIED_NO_OP
    res2 = migrator.staging_apply(simulation=True)
    assert res2["status"] == "ALREADY_APPLIED_NO_OP"


def test_ic_reg_09_strengthened_staging_verify_checks(tmp_path):
    """IC-REG-09: Strengthened staging-verify checks whole-file hash, candidate record, stores, archive, and journal."""
    from tools.staging_canonical_repair import main

    ws = create_synthetic_staging_workspace(str(tmp_path / "verify_ws"))

    # Before migration: verify fails (returncode != 0)
    assert main(["staging-verify", "--staging-dir", ws["root"]]) != 0

    # Apply migration
    assert main(["staging-apply", "--staging-dir", ws["root"], "--simulation"]) == 0

    # After migration: verify passes
    assert main(["staging-verify", "--staging-dir", ws["root"]]) == 0


def test_ic_reg_10_mandatory_baseline_binding_rejection(tmp_path):
    """IC-REG-10: Staging apply rejects unbound dataset identities or baseline manifest mismatches."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "unbound_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    # Try applying with an unknown dataset identity
    with pytest.raises(PreconditionViolationError, match="Unknown or unbound dataset identity"):
        migrator.staging_apply(simulation=True, dataset_identity="RANDOM_UNBOUND_DATASET")


def test_ic_reg_11_unmarked_staging_workspace_rejected(tmp_path):
    """IC-REG-11: Unmarked workspace without staging_marker.json is strictly rejected."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "unmarked_ws"), create_marker=False)
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(PreconditionViolationError, match="Missing staging workspace marker"):
        migrator.dry_run()

    with pytest.raises(PreconditionViolationError, match="Missing staging workspace marker"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(PreconditionViolationError, match="Missing staging workspace marker"):
        migrator.staging_recover()


# NTFS junctions are a Windows-only capability.  This regression must run on a
# real Windows host (and CI enforces that it does not skip there); simulating it
# on POSIX would prove nothing about the reparse-point resolution being tested.
requires_windows_junctions = pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "NTFS junction creation requires Windows (_winapi.CreateJunction); "
        "the junction regression is executed on the windows-latest CI runner."
    ),
)


def _remove_junction(path: str) -> None:
    """
    Remove a junction reparse point without ever touching its target.

    ``os.rmdir`` maps to ``RemoveDirectoryW``, which deletes the reparse point
    itself.  ``shutil.rmtree`` must never be used here: on this Python version a
    junction is not reported as a link, so rmtree would recurse through it and
    could delete the protected directory it points at.
    """
    try:
        os.rmdir(path)
        return
    except OSError:
        pass
    try:
        import _winapi
        _winapi.RemoveDirectory(path)
    except Exception:  # noqa: BLE001
        pass


@requires_windows_junctions
def test_ic_reg_12_windows_junction_escape_rejected(tmp_path):
    """
    IC-REG-12: Reject Windows NTFS junction escapes into protected roots and
    outside the staging root.

    Junctions are created only under ``tmp_path`` and always point at a target
    that has been verified to exist, so the test carries no machine-specific
    assumption about a historical production directory being present.
    """
    import _winapi

    junction_dir = str(tmp_path / "junctions")
    os.makedirs(junction_dir, exist_ok=True)

    created: list = []
    try:
        # --- Case 1: junction into the live protected source checkout --------
        assert os.path.isdir(SOURCE_REPO_ROOT), "protected junction target must exist"
        link_to_source = os.path.join(junction_dir, "link_to_source_checkout")
        _winapi.CreateJunction(SOURCE_REPO_ROOT, link_to_source)
        created.append(link_to_source)

        with pytest.raises(
            ProtectedPathViolationError, match="is inside source repository checkout"
        ):
            assert_safe_path(link_to_source)

        # Reaching the Canonical data directory *through* the junction is
        # refused too -- the junction is resolved before the check.
        with pytest.raises(ProtectedPathViolationError):
            assert_safe_path(os.path.join(link_to_source, "data"))

        # --- Case 2: junction into a declared legacy protected root ----------
        # Only exercised when the historical location actually exists here; the
        # declaration itself is asserted unconditionally in IC-REG-12c.
        for index, legacy in enumerate(LEGACY_PROTECTED_ROOTS):
            if not os.path.isdir(legacy):
                continue
            link_legacy = os.path.join(junction_dir, f"link_legacy_{index}")
            _winapi.CreateJunction(legacy, link_legacy)
            created.append(link_legacy)
            with pytest.raises(
                ProtectedPathViolationError, match="resolves inside protected root"
            ):
                assert_safe_path(link_legacy)

        # --- Case 3: junction escaping the staging root ----------------------
        target_outside = str(tmp_path / "outside_dir")
        os.makedirs(target_outside, exist_ok=True)
        staging_dir = str(tmp_path / "staging_box")
        os.makedirs(staging_dir, exist_ok=True)
        link_outside = os.path.join(staging_dir, "junction_escape")
        _winapi.CreateJunction(target_outside, link_outside)
        created.append(link_outside)

        with pytest.raises(ProtectedPathViolationError, match="escapes staging root"):
            assert_safe_path(link_outside, staging_root=staging_dir)
    finally:
        for link in created:
            _remove_junction(link)

    # Temporary resources are gone and the staging box holds no reparse points.
    for link in created:
        assert not os.path.exists(link)


def test_ic_reg_12b_live_checkout_is_protected_dynamically():
    """
    IC-REG-12b: The checkout that is actually executing this code is protected,
    resolved from __file__ rather than from any hardcoded machine layout.
    """
    assert SOURCE_REPO_ROOT, "SOURCE_REPO_ROOT must resolve"
    assert os.path.isdir(SOURCE_REPO_ROOT)
    assert os.path.normcase(os.path.realpath(SOURCE_REPO_ROOT)) == os.path.normcase(
        os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

    for target in (
        SOURCE_REPO_ROOT,
        SOURCE_DATA_DIR,
        os.path.join(SOURCE_REPO_ROOT, "data", "daily_matches.json"),
        os.path.join(SOURCE_REPO_ROOT, "tools", "staging_canonical_repair.py"),
    ):
        with pytest.raises(
            ProtectedPathViolationError, match="is inside source repository checkout"
        ):
            assert_safe_path(target)


def test_ic_reg_12c_legacy_roots_remain_declared_protections():
    """
    IC-REG-12c: Legacy production and evidence-epoch roots stay protected as
    declarations -- matched by path prefix whether or not they exist here -- and
    are never silently redefined as the current development checkout.
    """
    assert PROTECTED_ROOTS == LEGACY_PROTECTED_ROOTS

    for legacy in LEGACY_PROTECTED_ROOTS:
        assert legacy in PROTECTED_ROOTS
        # Never re-pointed at the live checkout.
        assert canonicalize_path(legacy) != canonicalize_path(SOURCE_REPO_ROOT)

        for target in (legacy, os.path.join(legacy, "data")):
            with pytest.raises(
                ProtectedPathViolationError, match="resolves inside protected root"
            ):
                assert_safe_path(target)

        # Case variants and separator variants normalize to the same root.
        for variant in (legacy.lower(), legacy.replace("\\", "/")):
            with pytest.raises(ProtectedPathViolationError):
                assert_safe_path(variant)


def test_ic_reg_12d_containment_is_tri_state_and_fails_closed(tmp_path, monkeypatch):
    """
    IC-REG-12d: An undeterminable containment relation must be refused, never
    reported as "outside" and thereby silently authorized.
    """
    import tools.staging_canonical_repair as mod

    # Undeterminable: an empty canonical form.
    assert containment_relation("", LEGACY_PROD_REPO_ROOT) is None
    # Definite.
    assert containment_relation(SOURCE_REPO_ROOT, SOURCE_REPO_ROOT) is True
    assert containment_relation(str(tmp_path / "elsewhere"), SOURCE_REPO_ROOT) is False

    # The gate refuses rather than authorizing when resolution is impossible.
    monkeypatch.setattr(mod, "canonicalize_path", lambda _p: "")
    with pytest.raises(ProtectedPathViolationError, match="fail closed"):
        mod.assert_safe_path("/anywhere/at/all")


def test_ic_reg_13_immutable_store_mutation_rejected_in_recovery(tmp_path):
    """IC-REG-13: Mutation of any immutable store is detected and rejected during recovery."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "store_mut_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_commit_intent")

    # Mutate team_strength.json
    store_file = os.path.join(ws["working_data"], "team_strength.json")
    with open(store_file, "a", encoding="utf-8") as f:
        f.write(" ")

    with pytest.raises(ImmutableStoreMutationError, match="mutated prior to recovery"):
        migrator.staging_recover()


def test_ic_reg_14_unrelated_record_tampering_rejected_in_idempotency(tmp_path):
    """IC-REG-14: Tampering with unrelated records invalidates idempotency and fails closed."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "tamper_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    res1 = migrator.staging_apply(simulation=True)
    assert res1["status"] == "SUCCESS"

    # Modify an unrelated match record in the working data
    daily_file = ws["daily_matches_path"]
    with open(daily_file, "r", encoding="utf-8") as f:
        daily_data = json.load(f)
    daily_data["matches"][0]["home"] = "TAMPERED_TEAM_NAME"
    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(daily_data, f, ensure_ascii=False, indent=2, sort_keys=True)

    # Re-running apply must NOT return ALREADY_APPLIED_NO_OP because file hash does not match
    with pytest.raises(PreconditionViolationError):
        migrator.staging_apply(simulation=True)


def test_ic_reg_15_legitimate_second_run_remains_zero_write_noop(tmp_path):
    """IC-REG-15: Legitimate second execution is a zero-write no-op with zero journal changes."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "noop_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    res1 = migrator.staging_apply(simulation=True)
    assert res1["status"] == "SUCCESS"

    daily_mtime_before = os.path.getmtime(ws["daily_matches_path"])
    daily_hash_before = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    journal_len_before = len(migrator.journal.read_entries())

    res2 = migrator.staging_apply(simulation=True)
    assert res2["status"] == "ALREADY_APPLIED_NO_OP"

    daily_mtime_after = os.path.getmtime(ws["daily_matches_path"])
    daily_hash_after = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    journal_len_after = len(migrator.journal.read_entries())

    assert daily_hash_before == daily_hash_after
    assert daily_mtime_before == daily_mtime_after
    assert journal_len_before == journal_len_after


# =============================================================================
# Phase J Recovery Criteria: J-REC-01 through J-REC-13
# =============================================================================

def test_j_rec_01_archive_dir_deleted(tmp_path):
    """J-REC-01: Deleted archive dir before recovery raises RecoveryStateError, files and journal untouched."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec01_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    archive_dir = migrator.archive_dir
    assert os.path.exists(archive_dir)
    shutil.rmtree(archive_dir)

    daily_mtime_before = os.path.getmtime(ws["daily_matches_path"])
    daily_hash_before = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    journal_len_before = len(migrator.journal.read_entries())

    with pytest.raises(RecoveryStateError, match="Forensic archive directory missing"):
        migrator.staging_recover()

    assert os.path.getmtime(ws["daily_matches_path"]) == daily_mtime_before
    assert hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest() == daily_hash_before
    assert len(migrator.journal.read_entries()) == journal_len_before


def test_j_rec_02_raw_archive_missing(tmp_path):
    """J-REC-02: Missing daily_matches_raw.json raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec02_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    raw_file = os.path.join(migrator.archive_dir, "daily_matches_raw.json")
    os.remove(raw_file)

    with pytest.raises(RecoveryStateError, match="Forensic archive raw file missing"):
        migrator.staging_recover()


def test_j_rec_03_raw_archive_corrupted(tmp_path):
    """J-REC-03: Byte-corrupted daily_matches_raw.json raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec03_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    raw_file = os.path.join(migrator.archive_dir, "daily_matches_raw.json")
    with open(raw_file, "ab") as f:
        f.write(b" ")

    with pytest.raises(RecoveryStateError, match="Forensic archive raw hash mismatch"):
        migrator.staging_recover()


def test_j_rec_04_manifest_missing(tmp_path):
    """J-REC-04: Missing forensic_manifest.json raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec04_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    manifest_file = os.path.join(migrator.archive_dir, "forensic_manifest.json")
    os.remove(manifest_file)

    with pytest.raises(RecoveryStateError, match="Forensic manifest missing"):
        migrator.staging_recover()


def test_j_rec_05_record_a_missing_or_mismatched(tmp_path):
    """J-REC-05: Missing or hash-mismatched record_a.json raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec05_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    rec_a_path = os.path.join(migrator.archive_dir, "record_a.json")
    with open(rec_a_path, "w", encoding="utf-8") as f:
        json.dump({"tampered": True, "id": TARGET_CANONICAL_ID}, f)

    with pytest.raises(RecoveryStateError, match="record_a hash mismatch"):
        migrator.staging_recover()


def test_j_rec_06_record_b_missing_or_mismatched(tmp_path):
    """J-REC-06: Missing or hash-mismatched record_b.json raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec06_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    rec_b_path = os.path.join(migrator.archive_dir, "record_b.json")
    with open(rec_b_path, "w", encoding="utf-8") as f:
        json.dump({"tampered": True, "id": TARGET_CANONICAL_ID}, f)

    with pytest.raises(RecoveryStateError, match="record_b hash mismatch"):
        migrator.staging_recover()


def test_j_rec_07_immutable_store_modified_in_recovery(tmp_path):
    """J-REC-07: Modified non-Canonical store during recovery raises RecoveryStateError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec07_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    store_file = os.path.join(ws["working_data"], "calibrated.json")
    with open(store_file, "a", encoding="utf-8") as f:
        f.write(" ")

    with pytest.raises(RecoveryStateError):
        migrator.staging_recover()


def test_j_rec_08_corrupted_active_canonical_classified_unknown(tmp_path):
    """J-REC-08: Corrupted active Canonical file classified UNKNOWN, raises RecoveryStateError with zero writes."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec08_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    # Corrupt daily_matches.json to unknown hash
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        f.write('{"matches": [], "meta": {"corrupted": true}}')

    daily_mtime_before = os.path.getmtime(ws["daily_matches_path"])
    daily_hash_before = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    journal_len_before = len(migrator.journal.read_entries())

    with pytest.raises(RecoveryStateError, match="UNKNOWN state"):
        migrator.staging_recover()

    assert os.path.getmtime(ws["daily_matches_path"]) == daily_mtime_before
    assert hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest() == daily_hash_before
    assert len(migrator.journal.read_entries()) == journal_len_before


def test_j_rec_09_crash_after_postconditions_verified(tmp_path):
    """J-REC-09: Crash after POSTCONDITIONS_VERIFIED recovers forward, appends COMPLETION_RECORDED."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec09_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="before_completion")

    entries_before = migrator.journal.read_entries()
    assert entries_before[-1]["state"] == "POSTCONDITIONS_VERIFIED"

    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_COMPLETED_FORWARD"
    assert rec["action"] == "RECORDED_COMPLETION"

    entries_after = migrator.journal.read_entries()
    assert len(entries_after) == len(entries_before) + 1
    assert entries_after[-1]["state"] == "COMPLETION_RECORDED"

    # Re-running recovery is a zero-write no-op
    rec2 = migrator.staging_recover()
    assert rec2["status"] == "ALREADY_COMPLETED_NO_OP"
    assert len(migrator.journal.read_entries()) == len(entries_after)


def test_j_rec_10_re_recovery_after_completion_noop(tmp_path):
    """J-REC-10: Recovery on already completed migration returns ALREADY_COMPLETED_NO_OP with 0 writes/appends."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec10_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"

    daily_mtime_before = os.path.getmtime(ws["daily_matches_path"])
    daily_hash_before = hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest()
    journal_len_before = len(migrator.journal.read_entries())

    rec = migrator.staging_recover()
    assert rec["status"] == "ALREADY_COMPLETED_NO_OP"
    assert rec["latest_state"] == "COMPLETION_RECORDED"

    assert os.path.getmtime(ws["daily_matches_path"]) == daily_mtime_before
    assert hashlib.sha256(open(ws["daily_matches_path"], "rb").read()).hexdigest() == daily_hash_before
    assert len(migrator.journal.read_entries()) == journal_len_before


def test_j_rec_11_concurrent_recovery_lock_contention(tmp_path):
    """J-REC-11: Concurrent recovery lock contention raises StagingLockError, directory unharmed."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec11_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_file_replace")

    with acquire_staging_lock(ws["working_data"], staging_root=ws["root"]):
        with pytest.raises(StagingLockError, match="Staging lock busy or denied"):
            migrator.staging_recover()


def test_j_rec_12_tampered_staging_marker_rejected(tmp_path):
    """J-REC-12: Tampered staging marker file raises PreconditionViolationError."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec12_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    marker_path = os.path.join(ws["root"], STAGING_MARKER_FILENAME)
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write('{"tampered": true}')

    with pytest.raises(PreconditionViolationError):
        migrator.staging_recover()


def test_j_rec_13_crash_after_intent_before_committed(tmp_path):
    """J-REC-13: Crash after file replacement before PROJECTION_COMMITTED recovers forward under lock."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "rec13_ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_replace_before_committed")

    entries_before = migrator.journal.read_entries()
    assert entries_before[-1]["state"] == "COMMIT_INTENT_DURABLE"

    rec = migrator.staging_recover()
    assert rec["status"] == "RECOVERED_COMPLETED_FORWARD"
    assert rec["after_total"] == 192
    assert rec["unique_ids"] == 192

    entries_after = migrator.journal.read_entries()
    states_after = [e["state"] for e in entries_after]
    assert states_after[-3:] == ["PROJECTION_COMMITTED", "POSTCONDITIONS_VERIFIED", "COMPLETION_RECORDED"]

    # Verify active Canonical store
    data = json.load(open(ws["daily_matches_path"], "r", encoding="utf-8"))
    assert len(data["matches"]) == 192
    targets = [m for m in data["matches"] if m.get("id") == TARGET_CANONICAL_ID]
    assert len(targets) == 1
    assert hash_record(targets[0]) == EXPECTED_CANDIDATE_SHA256


# =============================================================================
# Phase J Journal Criteria: J-JRN-01 through J-JRN-12
# =============================================================================

def test_j_jrn_01_missing_prepared(tmp_path):
    """J-JRN-01: Journal missing PREPARED initial entry raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    bad_entries = [
        {"state": "COMMIT_INTENT_DURABLE", "payload": {"migration_id": "MIG-01"}}
    ]
    with pytest.raises(RecoveryStateError, match="Journal must begin with PREPARED"):
        journal.validate_chain(bad_entries)


def test_j_jrn_02_skipped_intermediate_state(tmp_path):
    """J-JRN-02: Journal with skipped state raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    bad_entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "hash"}},
    ]
    with pytest.raises(RecoveryStateError, match="skipped intermediate state"):
        journal.validate_chain(bad_entries)


def test_j_jrn_03_missing_migration_id(tmp_path):
    """J-JRN-03: Journal with missing migration_id raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    bad_entries = [
        {"state": "PREPARED", "payload": {}},
    ]
    with pytest.raises(RecoveryStateError, match="missing required non-empty migration_id"):
        journal.validate_chain(bad_entries)


def test_j_jrn_04_conflicting_migration_id(tmp_path):
    """J-JRN-04: Journal with conflicting migration_id raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    bad_entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-02"}},
    ]
    with pytest.raises(RecoveryStateError, match="Conflicting migration_id"):
        journal.validate_chain(bad_entries)


def test_j_jrn_05_duplicate_completion_recorded(tmp_path):
    """J-JRN-05: Journal with duplicate COMPLETION_RECORDED raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "p"}},
        {"state": "POSTCONDITIONS_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "COMPLETION_RECORDED", "payload": {"migration_id": "MIG-01"}},
        {"state": "COMPLETION_RECORDED", "payload": {"migration_id": "MIG-01"}},
    ]
    with pytest.raises(RecoveryStateError, match="Duplicate COMPLETION_RECORDED|duplicate state transition"):
        journal.validate_chain(entries)


def test_j_jrn_06_corrupt_json_line(tmp_path):
    """J-JRN-06: Journal with corrupt JSON line raises RecoveryStateError."""
    journal_file = str(tmp_path / "corrupt_j.jsonl")
    with open(journal_file, "w", encoding="utf-8") as f:
        f.write('{"state": "PREPARED", "payload": {"migration_id": "MIG-01"}}\n')
        f.write('CORRUPT NOT JSON LINE\n')

    journal = StagingJournal(journal_file)
    entries = journal.read_entries()
    with pytest.raises(RecoveryStateError, match="Corrupted journal line detected"):
        journal.validate_chain(entries)


def test_j_jrn_07_unknown_state_name(tmp_path):
    """J-JRN-07: Journal with unknown state name raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "MAGIC_UNRECOGNIZED_STATE", "payload": {"migration_id": "MIG-01"}},
    ]
    with pytest.raises(RecoveryStateError, match="Unknown state"):
        journal.validate_chain(entries)


def test_j_jrn_08_mismatched_dataset_identity(tmp_path):
    """J-JRN-08: Journal with mismatched dataset identity raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "dataset_identity": "ID_A"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01", "dataset_identity": "ID_B"}},
    ]
    with pytest.raises(RecoveryStateError, match="Mismatched dataset identity"):
        journal.validate_chain(entries)


def test_j_jrn_09_missing_intent_hashes(tmp_path):
    """J-JRN-09: Journal with missing intent hashes in COMMIT_INTENT_DURABLE raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                # missing projected_file_sha256
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
    ]
    with pytest.raises(RecoveryStateError, match="Missing required intent hash field"):
        journal.validate_chain(entries)


def test_j_jrn_10_mismatched_intent_hash(tmp_path):
    """J-JRN-10: Journal with mismatched intent hash raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "pre_store_hashes": {"daily_matches.json": "correct_source"}}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "wrong_source_hash",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
    ]
    with pytest.raises(RecoveryStateError, match="Intent source_file_sha256 mismatch"):
        journal.validate_chain(entries)

    # Also test mismatched projected hash in PROJECTION_COMMITTED
    entries2 = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "expected_p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "wrong_p"}},
    ]
    with pytest.raises(RecoveryStateError, match="Mismatched projected file hash in PROJECTION_COMMITTED"):
        journal.validate_chain(entries2)


def test_j_jrn_11_valid_rollback_record(tmp_path):
    """J-JRN-11: Journal with valid rollback record validates successfully."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "ROLLED_BACK_TO_RAW_ARCHIVE"}},
    ]
    # Must validate without raising
    journal.validate_chain(entries)


def test_j_jrn_12_invalid_rollback_reset(tmp_path):
    """J-JRN-12: Journal with invalid rollback record raises RecoveryStateError."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    # Rollback after PROJECTION_COMMITTED is invalid
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "p"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "ROLLED_BACK_TO_RAW_ARCHIVE"}},
    ]
    with pytest.raises(RecoveryStateError, match="Invalid rollback reset to PREPARED"):
        journal.validate_chain(entries)


# =============================================================================
# Phase K Release Candidate Regression Suite: K-RET-01 through K-RET-12
# =============================================================================

def test_k_ret_01_retry_after_pre_commit_prepared(tmp_path):
    """K-RET-01: Retry after pre-commit interruption at PREPARED succeeds cleanly."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="before_archive")

    entries_after_fault = migrator.journal.read_entries()
    assert len(entries_after_fault) == 1
    assert entries_after_fault[0]["state"] == "PREPARED"

    # Retry staging-apply
    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"
    assert res["after_total"] == 192

    all_entries = migrator.journal.read_entries()
    assert len(all_entries) == 8  # 1 from attempt 1 + 7 from attempt 2
    migrator.journal.validate_chain(all_entries)
    assert migrator.journal.get_latest_valid_state() == "COMPLETION_RECORDED"


def test_k_ret_02_retry_after_pre_commit_evidence_verified(tmp_path):
    """K-RET-02: Retry after pre-commit interruption at EVIDENCE_VERIFIED succeeds cleanly."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="during_archive")

    entries_after_fault = migrator.journal.read_entries()
    assert len(entries_after_fault) == 2
    assert entries_after_fault[-1]["state"] == "EVIDENCE_VERIFIED"

    # Retry staging-apply
    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"

    all_entries = migrator.journal.read_entries()
    migrator.journal.validate_chain(all_entries)
    assert migrator.journal.get_latest_valid_state() == "COMPLETION_RECORDED"


def test_k_ret_03_retry_after_pre_commit_authorized(tmp_path):
    """K-RET-03: Retry after pre-commit interruption at AUTHORIZED_FOR_STAGING_SIMULATION succeeds."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="before_commit_intent")

    entries_after_fault = migrator.journal.read_entries()
    assert len(entries_after_fault) == 3
    assert entries_after_fault[-1]["state"] == "AUTHORIZED_FOR_STAGING_SIMULATION"

    # Retry staging-apply
    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"

    all_entries = migrator.journal.read_entries()
    migrator.journal.validate_chain(all_entries)
    assert migrator.journal.get_latest_valid_state() == "COMPLETION_RECORDED"


def test_k_ret_04_retry_after_rollback_from_commit_intent(tmp_path):
    """K-RET-04: Full lifecycle: attempt 1 commits intent, fails, rolls back, attempt 2 succeeds."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_commit_intent")

    # File is baseline, recovery rolls back cleanly
    rec_res = migrator.staging_recover()
    assert rec_res["status"] == "RECOVERED_ROLLED_BACK"

    entries_after_rec = migrator.journal.read_entries()
    assert len(entries_after_rec) == 5
    assert entries_after_rec[-1]["state"] == "PREPARED"
    assert entries_after_rec[-1]["payload"].get("action") == "ROLLED_BACK_TO_RAW_ARCHIVE"

    # Retry staging-apply succeeds and reuses verified archive
    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"
    assert res["after_total"] == 192

    all_entries = migrator.journal.read_entries()
    assert len(all_entries) == 12  # 5 from attempt 1 + 7 from attempt 2
    migrator.journal.validate_chain(all_entries)
    assert migrator.journal.get_latest_valid_state() == "COMPLETION_RECORDED"


def test_k_ret_05_retry_rejected_if_commit_intent_without_rollback(tmp_path):
    """K-RET-05: Direct retry after unrecovered COMMIT_INTENT_DURABLE crash is rejected."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(FaultInjectionError):
        migrator.staging_apply(simulation=True, fault_inject="after_commit_intent")

    # Attempting to call staging-apply directly without recovery first fails closed
    with pytest.raises(RecoveryStateError, match="Previous attempt declared COMMIT_INTENT_DURABLE without rollback"):
        migrator.staging_apply(simulation=True)


def test_k_ret_06_retry_rejected_if_projection_committed(tmp_path):
    """K-RET-06: Starting new attempt after PROJECTION_COMMITTED is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "p"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "RETRY_PREPARED"}},
    ]
    with pytest.raises(RecoveryStateError, match="committed projection; subsequent retry attempt is illegal"):
        journal.validate_chain(entries)


def test_k_ret_07_retry_rejected_if_completion_recorded(tmp_path):
    """K-RET-07: Starting new attempt after COMPLETION_RECORDED is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s",
                "projected_file_sha256": "p",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PROJECTION_COMMITTED", "payload": {"migration_id": "MIG-01", "daily_matches_sha256": "p"}},
        {"state": "POSTCONDITIONS_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "COMPLETION_RECORDED", "payload": {"migration_id": "MIG-01"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
    ]
    with pytest.raises(RecoveryStateError, match="committed projection; subsequent retry attempt is illegal"):
        journal.validate_chain(entries)


def test_k_ret_08_multi_attempt_mismatched_migration_id(tmp_path):
    """K-RET-08: Multi-attempt journal with conflicting migration_id is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-02", "action": "RETRY_PREPARED"}},
    ]
    with pytest.raises(RecoveryStateError, match="Conflicting migration_id in journal"):
        journal.validate_chain(entries)


def test_k_ret_09_multi_attempt_conflicting_intent_hashes(tmp_path):
    """K-RET-09: Multi-attempt journal with conflicting intent hashes is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s1",
                "projected_file_sha256": "p1",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "ROLLED_BACK_TO_RAW_ARCHIVE"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "RETRY_PREPARED"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {
            "state": "COMMIT_INTENT_DURABLE",
            "payload": {
                "migration_id": "MIG-01",
                "source_file_sha256": "s2",  # Conflicting source hash!
                "projected_file_sha256": "p1",
                "candidate_record_sha256": "c",
                "record_a_sha256": "a",
                "record_b_sha256": "b",
            },
        },
    ]
    with pytest.raises(RecoveryStateError, match="Conflicting source_file_sha256 in subsequent COMMIT_INTENT_DURABLE"):
        journal.validate_chain(entries)


def test_k_ret_10_multi_attempt_corrupted_line(tmp_path):
    """K-RET-10: Multi-attempt journal containing a corrupted line fails closed."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "CORRUPTED_LINE", "line_no": 2},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "RETRY_PREPARED"}},
    ]
    with pytest.raises(RecoveryStateError, match="Corrupted journal line detected"):
        journal.validate_chain(entries)


def test_k_ret_11_multi_attempt_internal_regression(tmp_path):
    """K-RET-11: Multi-attempt journal with internal regression in attempt 2 is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "RETRY_PREPARED"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
        {"state": "EVIDENCE_VERIFIED", "payload": {"migration_id": "MIG-01"}},  # Regression in attempt 2
    ]
    with pytest.raises(RecoveryStateError, match="Journal state regression detected"):
        journal.validate_chain(entries)


def test_k_ret_12_multi_attempt_internal_skip(tmp_path):
    """K-RET-12: Multi-attempt journal skipping intermediate state in attempt 2 is rejected."""
    journal = StagingJournal(str(tmp_path / "j.jsonl"))
    entries = [
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01"}},
        {"state": "PREPARED", "payload": {"migration_id": "MIG-01", "action": "RETRY_PREPARED"}},
        # Skips EVIDENCE_VERIFIED
        {"state": "AUTHORIZED_FOR_STAGING_SIMULATION", "payload": {"migration_id": "MIG-01"}},
    ]
    with pytest.raises(RecoveryStateError, match="Journal state skipped intermediate state"):
        journal.validate_chain(entries)


# =============================================================================
# Phase K Forensic Archive Matrix Suite: K-ARC-01 through K-ARC-08
# =============================================================================

def setup_completed_staging_workspace(root: str) -> StagingCanonicalMigrator:
    ws = create_synthetic_staging_workspace(root)
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])
    res = migrator.staging_apply(simulation=True)
    assert res["status"] == "SUCCESS"
    assert migrator.staging_verify()["verified"] is True
    return migrator


def test_k_arc_01_archive_dir_missing(tmp_path):
    """K-ARC-01: Archive directory missing causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    shutil.rmtree(migrator.archive_dir)

    # 1. staging_verify
    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "Forensic archive directory missing" in v_res["error"]

    # 2. staging_apply idempotency check fails closed
    with pytest.raises(RecoveryStateError, match="Forensic archive directory missing"):
        migrator.staging_apply(simulation=True)

    # 3. staging_recover fails closed
    with pytest.raises(RecoveryStateError, match="Forensic archive directory missing"):
        migrator.staging_recover()


def test_k_arc_02_daily_matches_raw_missing(tmp_path):
    """K-ARC-02: daily_matches_raw.json missing causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    os.unlink(os.path.join(migrator.archive_dir, "daily_matches_raw.json"))

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "daily_matches_raw.json" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="daily_matches_raw.json"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="daily_matches_raw.json"):
        migrator.staging_recover()


def test_k_arc_03_daily_matches_raw_corrupted(tmp_path):
    """K-ARC-03: daily_matches_raw.json corrupted bytes causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    raw_path = os.path.join(migrator.archive_dir, "daily_matches_raw.json")
    with open(raw_path, "ab") as f:
        f.write(b"CORRUPTION_BYTES")

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "raw hash mismatch" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="raw hash mismatch"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="raw hash mismatch"):
        migrator.staging_recover()


def test_k_arc_04_forensic_manifest_missing_or_corrupt(tmp_path):
    """K-ARC-04: forensic_manifest.json missing or corrupt causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    man_path = os.path.join(migrator.archive_dir, "forensic_manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        f.write("{ invalid json")

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "Forensic manifest corrupted" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="Forensic manifest corrupted"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="Forensic manifest corrupted"):
        migrator.staging_recover()


def test_k_arc_05_forensic_manifest_metadata_mismatch(tmp_path):
    """K-ARC-05: forensic_manifest.json metadata mismatch causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    man_path = os.path.join(migrator.archive_dir, "forensic_manifest.json")
    with open(man_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["migration_id"] = "FORGED_MIGRATION_ID"
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "migration_id mismatch" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="migration_id mismatch"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="migration_id mismatch"):
        migrator.staging_recover()


def test_k_arc_06_record_a_missing_or_corrupt(tmp_path):
    """K-ARC-06: record_a.json missing or content corrupted causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    rec_a_path = os.path.join(migrator.archive_dir, "record_a.json")
    with open(rec_a_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["home"] = "TAMPERED_TEAM"
    with open(rec_a_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "record_a hash mismatch" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="record_a hash mismatch"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="record_a hash mismatch"):
        migrator.staging_recover()


def test_k_arc_07_record_b_missing_or_corrupt(tmp_path):
    """K-ARC-07: record_b.json missing or content corrupted causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    rec_b_path = os.path.join(migrator.archive_dir, "record_b.json")
    with open(rec_b_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["score"]["ft"] = [9, 9]
    with open(rec_b_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "record_b hash mismatch" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="record_b hash mismatch"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="record_b hash mismatch"):
        migrator.staging_recover()


def test_k_arc_08_manifest_record_hash_mismatch(tmp_path):
    """K-ARC-08: forensic_manifest.json record hash fields mismatch causes failure across verify, apply, recover."""
    migrator = setup_completed_staging_workspace(str(tmp_path / "ws"))
    man_path = os.path.join(migrator.archive_dir, "forensic_manifest.json")
    with open(man_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["record_a_sha256"] = "wrong_sha256_hash_here"
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    v_res = migrator.staging_verify(raise_on_error=False)
    assert v_res["verified"] is False
    assert "record_a_sha256 mismatch" in v_res["error"]

    with pytest.raises(RecoveryStateError, match="record_a_sha256 mismatch"):
        migrator.staging_apply(simulation=True)

    with pytest.raises(RecoveryStateError, match="record_a_sha256 mismatch"):
        migrator.staging_recover()
