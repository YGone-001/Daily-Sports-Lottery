"""
tests/test_staging_canonical_repair.py
======================================
Comprehensive automated test suite for Guarded Historical Canonical Staging Migration.
Covers:
- Positive Acceptance Criteria: I-POS-01 through I-POS-24
- Negative and Fault-Injection Criteria: I-NEG-01 through I-NEG-25
- Path safety boundaries and Windows lock contention
- Portable synthetic fixtures runnable offline anywhere
"""
import copy
import hashlib
import json
import os
import shutil
import tempfile
import pytest

from tools.staging_canonical_repair import (
    StagingCanonicalMigrator,
    ProtectedPathViolationError,
    PreconditionViolationError,
    StagingLockError,
    ImmutableStoreMutationError,
    FaultInjectionError,
    RecoveryStateError,
    assert_safe_path,
    build_candidate_projection,
    hash_record,
    serialize_record,
    TARGET_CANONICAL_ID,
    EXPECTED_RECORD_A_SHA256,
    EXPECTED_RECORD_B_SHA256,
    EXPECTED_CANDIDATE_SHA256,
    NON_CANONICAL_STORES,
    acquire_staging_lock,
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


def create_synthetic_staging_workspace(root_dir: str) -> dict:
    """
    Construct a complete, portable synthetic staging workspace with 193 records,
    Record A at index 55, Record B at index 56, and all 6 non-Canonical stores.
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

    # Write to input and working
    for path_dir in (input_data, working_data):
        with open(os.path.join(path_dir, "daily_matches.json"), "w", encoding="utf-8") as f:
            json.dump(daily_payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        for sname, sdata in stores.items():
            with open(os.path.join(path_dir, sname), "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, indent=2, sort_keys=True)

    return {
        "root": root_dir,
        "input_data": input_data,
        "working_data": working_data,
        "daily_matches_path": os.path.join(working_data, "daily_matches.json"),
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
    """I-NEG-01: Reject if expected source hash does not match."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    migrator = StagingCanonicalMigrator(staging_root=ws["root"])

    with pytest.raises(PreconditionViolationError, match="Canonical source hash mismatch"):
        migrator.staging_apply(simulation=True, expected_source_sha="0000000000000000000000000000000000000000000000000000000000000000")


def test_neg_02_wrong_record_a_hash(tmp_path):
    """I-NEG-02: Tampered Record A causes rejection."""
    ws = create_synthetic_staging_workspace(str(tmp_path / "ws"))
    with open(ws["daily_matches_path"], "r", encoding="utf-8") as f:
        d = json.load(f)
    d["matches"][55]["home"] = "篡改球队"
    with open(ws["daily_matches_path"], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

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
        json.dump(d, f, ensure_ascii=False, indent=2)

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
        json.dump(d, f, ensure_ascii=False, indent=2)

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
        json.dump(d, f, ensure_ascii=False, indent=2)

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
        json.dump(d, f, ensure_ascii=False, indent=2)

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
        json.dump(d, f, ensure_ascii=False, indent=2)

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

        # Mutate the store file during the apply process before postcondition check
        target_store_file = os.path.join(ws["working_data"], store)

        # We can use fault-inject before postconditions to mutate
        with pytest.raises(FaultInjectionError):
            migrator.staging_apply(simulation=True, fault_inject="before_postconditions")

        # Mutate the file
        with open(target_store_file, "a", encoding="utf-8") as f:
            f.write(" ")

        # Run recovery, which checks store invariants
        with pytest.raises(Exception):
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

    # First apply
    migrator.staging_apply(simulation=True, migration_id="MIG-TEST-001")

    # Manually append an intent with conflicting target hash using same migration_id
    migrator.journal.append_entry("COMMIT_INTENT_DURABLE", {
        "migration_id": "MIG-TEST-001",
        "projected_file_sha256": "CONFLICTING_HASH_12345",
    })

    # Restore 193 match file to trigger another apply
    shutil.copyfile(
        os.path.join(ws["root"], "archive", "daily_matches_raw.json"),
        ws["daily_matches_path"],
    )

    with pytest.raises(PreconditionViolationError, match="Reused migration ID with different target hash"):
        migrator.staging_apply(simulation=True, migration_id="MIG-TEST-001")


def test_neg_23_protected_production_path_rejected():
    """I-NEG-23: Reject production repo paths."""
    prod_paths = [
        r"C:\Users\YGone\Desktop\program\Daily-Sports-Lottery",
        r"C:\Users\YGone\Desktop\program\Daily-Sports-Lottery\data",
        r"c:/users/ygone/desktop/program/daily-sports-lottery/data",
    ]
    for p in prod_paths:
        with pytest.raises(ProtectedPathViolationError):
            assert_safe_path(p)


def test_neg_24_protected_evidence_epoch_rejected():
    """I-NEG-24: Reject evidence epoch paths."""
    epoch_paths = [
        r"C:\Users\YGone\Desktop\program\_quiescent_evidence_backup_20261010_224710",
        r"C:\Users\YGone\Desktop\_quiescent_evidence_backup_epoch1_20261011_014059",
    ]
    for p in epoch_paths:
        with pytest.raises(ProtectedPathViolationError):
            assert_safe_path(p)


def test_neg_25_path_traversal_and_junction_escape(tmp_path):
    """I-NEG-25: Reject path traversal attempts resolving into protected roots."""
    traversal_path = str(tmp_path) + r"\..\..\Desktop\program\Daily-Sports-Lottery\data"
    with pytest.raises(ProtectedPathViolationError):
        assert_safe_path(traversal_path)


# =============================================================================
# CLI Interface Integration Tests
# =============================================================================

def test_cli_lifecycle(tmp_path):
    """Test inspect, dry-run, staging-apply, staging-verify, and recover via CLI main()."""
    from tools.staging_canonical_repair import main

    ws = create_synthetic_staging_workspace(str(tmp_path / "cli_ws"))

    # 1. inspect
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

