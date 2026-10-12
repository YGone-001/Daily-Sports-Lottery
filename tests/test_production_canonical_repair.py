"""
tests/test_production_canonical_repair.py
=========================================
Offline test suite for the auditable production Canonical de-duplication entry
point (``tools/production_canonical_repair.py``).

Coverage
--------
* Safety boundaries: evidence epochs, drive roots, workspace placement.
* Read-only guarantees for ``inspect`` / ``dry-run`` / ``verify``.
* Authorization binding: schema, scope, ``authorized`` flag, data-dir, source
  hash, operator and migration-ID binding.
* Happy path: dynamic ``N -> N - 1`` projection with every postcondition
  enforced (uniqueness, unrelated-record preservation and ordering, metadata
  preservation, unknown top-level key preservation, six immutable stores).
* Idempotency, lock contention, and refusal on unexpected collision shapes.
* Fault injection and deterministic crash recovery (rollback / forward / fail
  closed on UNKNOWN).
"""
from __future__ import annotations

import copy
import hashlib
import json
import os

import pytest

from tools.production_canonical_repair import (
    CANONICAL_FILE,
    JOURNAL_STATES,
    NON_CANONICAL_STORES,
    AuthorizationError,
    ImmutableStoreMutationError,
    PreconditionViolationError,
    ProductionCanonicalRepairer,
    ProtectedPathViolationError,
    RecoveryStateError,
    StagingLockError,
    acquire_repair_lock,
    build_authorization,
    canonicalize_path,
    default_migration_id,
    derive_file_hash,
    inspect_dataset,
    load_authorization,
    synthesize_candidate,
    write_json_atomic,
)
from tools.staging_canonical_repair import hash_record

TARGET_ID = "500l-2026-10-09-1362422"

# Record A: live-score alias (abbreviated club names, closing odds, lottery no.)
RECORD_A = {
    "id": TARGET_ID,
    "sport": "football",
    "league": "巴甲",
    "round": "第29轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "upcoming",
    "home": "巴竞技",
    "away": "米竞技",
    "home_rank": 3,
    "away_rank": 7,
    "score": None,
    "odds": {"home_win": 4.75, "away_win": 1.6, "draw": 3.4, "matchnum": "4004"},
    "jczq_no": "4004",
    "market_tracked": True,
    "odds_updated_at": "2026-10-08T21:19:44.432817+08:00",
}

# Record B: finished feed (standard club names, terminal score, no odds)
RECORD_B = {
    "id": TARGET_ID,
    "sport": "football",
    "league": "巴甲",
    "round": "第29轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "finished",
    "home": "巴拉纳竞技",
    "away": "米内罗竞技",
    "home_rank": 3,
    "away_rank": 7,
    "score": {"ft": [2, 2], "ht": [1, 1]},
    "odds": None,
    "jczq_no": "",
    "market_tracked": True,
}


def _match(mid, home, away, *, status="upcoming", score=None, date="2026-10-09", time="01:00"):
    return {
        "id": mid,
        "sport": "football",
        "league": "测试联赛",
        "round": "第1轮",
        "date": date,
        "time": time,
        "status": status,
        "home": home,
        "away": away,
        "score": score,
        "odds": None,
        "jczq_no": "",
    }


def _stores_payload():
    return {
        "team_strength.json": {"巴拉纳竞技": {"rating": 1666.7}},
        "calibrated.json": {"ids": [TARGET_ID, "500l-2026-10-09-1000"]},
        "prediction_snapshots.json": {"version": 1, "snapshots": {"s1": {"match_id": TARGET_ID}}},
        "odds_snapshots.json": {"version": 1, "snapshots": {"o1": {"match_id": TARGET_ID}}},
        "settlements.json": {"version": 1, "settlements": {"t1": {"match_id": TARGET_ID}}},
        "evaluation_rows.json": {"version": 1, "rows": {"e1": {"match_id": TARGET_ID}}},
    }


def build_dataset(
    root: str,
    *,
    n_before: int = 6,
    n_after: int = 5,
    target_occurrences: int = 2,
    extra_dup_group: bool = False,
) -> str:
    """
    Construct a synthetic data directory with all seven stores.

    ``target_occurrences == 2`` is the well-formed collision; other values model
    malformed datasets the tool must refuse.
    """
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    matches = [
        _match(f"500l-2026-10-09-{1000 + i}", f"主队{i}", f"客队{i}", status="finished", score={"ft": [1, 0]})
        for i in range(n_before)
    ]

    if target_occurrences >= 1:
        matches.append(copy.deepcopy(RECORD_A))
    if target_occurrences >= 2:
        matches.append(copy.deepcopy(RECORD_B))
    for extra in range(max(0, target_occurrences - 2)):
        third = copy.deepcopy(RECORD_B)
        third["score"] = {"ft": [3, 3], "ht": [1, 1]}
        third["home"] = f"第三记录{extra}"
        matches.append(third)

    if extra_dup_group:
        matches.append(_match("dup-other", "X", "Y"))
        matches.append(_match("dup-other", "X2", "Y2"))

    matches.extend(
        _match(f"500l-2026-10-09-{2000 + i}", f"后续主队{i}", f"后续客队{i}")
        for i in range(n_after)
    )

    payload = {
        "schema_version": 3,  # unknown top-level key that must survive
        "matches": matches,
        "meta": {
            "total": len(matches),
            "last_refresh": "2026-10-10T18:45:23.927174+08:00",
            "sources": {"live_football": 462, "wanchang": 914},
        },
    }
    write_json_atomic(os.path.join(data_dir, CANONICAL_FILE), payload)
    for name, data in _stores_payload().items():
        write_json_atomic(os.path.join(data_dir, name), data)

    return data_dir


def snapshot_dir_hashes(directory: str) -> dict:
    """Hash every visible file in ``directory``.

    Dotfiles are skipped: ``.refresh.lock`` is created on demand by the shared
    cross-process lock and is not part of the evidence set.
    """
    hashes = {}
    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            hashes[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return hashes


def make_authorization(data_dir: str, **overrides):
    artifact = build_authorization(
        data_dir=data_dir,
        target_canonical_id=TARGET_ID,
        operator="ops-oncall",
        reason="repair legacy canonical duplicate",
    )
    artifact.update(overrides)
    return artifact


def manual_authorization(data_dir: str) -> dict:
    """Hand-built artifact used to exercise apply-side guards that
    ``build_authorization`` itself would refuse to produce."""
    report = inspect_dataset(data_dir)
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    records = [m for m in payload["matches"] if m.get("id") == TARGET_ID]
    candidate, _diff = synthesize_candidate(records)
    return {
        "authorization_schema_version": "1.0.0",
        "scope": "PRODUCTION_CANONICAL_DEDUP",
        "authorized": True,
        "migration_id": default_migration_id(TARGET_ID),
        "data_dir": data_dir,
        "target_canonical_id": TARGET_ID,
        "expected_source_sha256": report["store_hashes"][CANONICAL_FILE],
        "expected_candidate_sha256": hash_record(candidate),
        "operator": "ops-oncall",
        "approved_at": "2026-10-12T00:00:00+00:00",
        "reason": "manual",
    }


def apply_artifact(tmp_path, data_dir, artifact, **kwargs):
    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact.get("migration_id"),
    )
    return repairer, repairer.apply(artifact, confirm_apply=True, **kwargs)


# ===========================================================================
# 1. Inspection & read-only guarantees
# ===========================================================================

def test_inspect_reports_collision(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    report = inspect_dataset(data_dir)

    assert report["duplicate_ids"] == {TARGET_ID: 2}
    assert report["duplicate_groups"][TARGET_ID]["count"] == 2
    assert report["duplicate_groups"][TARGET_ID]["indices"][1] - report["duplicate_groups"][TARGET_ID]["indices"][0] == 1
    assert report["total_matches"] == report["unique_ids_count"] + 1
    assert report["store_hashes"][CANONICAL_FILE] != "MISSING"


def test_inspect_clean_dataset_has_no_collision(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=1)
    report = inspect_dataset(data_dir)
    assert report["duplicate_ids"] == {}
    assert report["total_matches"] == report["unique_ids_count"]


def test_inspect_rejects_missing_canonical_file(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PreconditionViolationError):
        inspect_dataset(str(empty))


def test_inspect_rejects_evidence_epoch_path(tmp_path):
    data_dir = build_dataset(str(tmp_path / "_quiescent_evidence_backup_20261010"))
    with pytest.raises(ProtectedPathViolationError):
        inspect_dataset(data_dir)


def test_inspect_is_read_only(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    before = snapshot_dir_hashes(data_dir)
    inspect_dataset(data_dir)
    inspect_dataset(data_dir)
    assert snapshot_dir_hashes(data_dir) == before


def test_workspace_inside_data_dir_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(ProtectedPathViolationError):
        ProductionCanonicalRepairer(data_dir=data_dir, workspace=os.path.join(data_dir, "ws"))


def test_workspace_parent_of_data_dir_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(ProtectedPathViolationError):
        ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path))


# ===========================================================================
# 2. Authorization artifact
# ===========================================================================

def test_build_authorization_binds_source_and_candidate(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    assert artifact["authorization_schema_version"] == "1.0.0"
    assert artifact["scope"] == "PRODUCTION_CANONICAL_DEDUP"
    assert artifact["authorized"] is True
    assert artifact["data_dir"] == canonicalize_path(data_dir)
    assert artifact["expected_source_sha256"] == inspect_dataset(data_dir)["store_hashes"][CANONICAL_FILE]

    candidate, _ = synthesize_candidate([RECORD_A, RECORD_B])
    assert artifact["expected_candidate_sha256"] == hash_record(candidate)
    # The synthesized record keeps the terminal result and the closing odds.
    assert candidate["home"] == "巴拉纳竞技"
    assert candidate["status"] == "finished"
    assert candidate["score"] == {"ft": [2, 2], "ht": [1, 1]}
    assert candidate["jczq_no"] == "4004"
    assert candidate["odds"]["home_win"] == 4.75


def test_build_authorization_refuses_clean_dataset(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=1)
    with pytest.raises(PreconditionViolationError):
        make_authorization(data_dir)


def test_build_authorization_refuses_unexpected_duplicate_group(tmp_path):
    data_dir = build_dataset(str(tmp_path), extra_dup_group=True)
    with pytest.raises(PreconditionViolationError):
        make_authorization(data_dir)


def test_build_authorization_refuses_three_occurrences(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=3)
    with pytest.raises(PreconditionViolationError):
        make_authorization(data_dir)


def test_build_authorization_requires_operator_and_reason(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(AuthorizationError):
        build_authorization(data_dir, TARGET_ID, "", "reason")
    with pytest.raises(AuthorizationError):
        build_authorization(data_dir, TARGET_ID, "ops", "   ")


def test_load_authorization_rejects_unauthorized_flag(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir, authorized=False)
    path = str(tmp_path / "auth.json")
    write_json_atomic(path, artifact)
    with pytest.raises(AuthorizationError):
        load_authorization(path)


def test_load_authorization_rejects_bad_scope_and_missing_fields(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    bad_scope = make_authorization(data_dir, scope="SOMETHING_ELSE")
    path = str(tmp_path / "bad_scope.json")
    write_json_atomic(path, bad_scope)
    with pytest.raises(AuthorizationError):
        load_authorization(path)

    missing = make_authorization(data_dir)
    missing.pop("expected_candidate_sha256")
    path2 = str(tmp_path / "missing.json")
    write_json_atomic(path2, missing)
    with pytest.raises(AuthorizationError):
        load_authorization(path2)


# ===========================================================================
# 3. Dry run
# ===========================================================================

def test_dry_run_is_read_only_and_predicts_n_minus_1(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir, workspace=str(tmp_path / "ws"), writable=False
    )
    preview = repairer.dry_run(artifact)

    assert preview["read_only_data_dir"] is True
    assert preview["after_total"] == preview["before_total"] - 1
    assert preview["candidate_sha256"] == artifact["expected_candidate_sha256"]
    assert preview["authorization_matches_candidate"] is True
    assert snapshot_dir_hashes(data_dir) == before


# ===========================================================================
# 4. Apply guards
# ===========================================================================

def test_apply_requires_confirm_flag(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    with pytest.raises(AuthorizationError):
        repairer.apply(artifact, confirm_apply=False)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_artifact_bound_to_other_data_dir(tmp_path):
    data_dir_a = build_dataset(str(tmp_path / "a"))
    data_dir_b = build_dataset(str(tmp_path / "b"))
    artifact = make_authorization(data_dir_a)

    repairer = ProductionCanonicalRepairer(data_dir=data_dir_b, workspace=str(tmp_path / "ws"))
    with pytest.raises(AuthorizationError):
        repairer.apply(artifact, confirm_apply=True)
    assert inspect_dataset(data_dir_b)["duplicate_ids"] == {TARGET_ID: 2}


def test_apply_rejects_operator_mismatch(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    with pytest.raises(AuthorizationError):
        repairer.apply(artifact, confirm_apply=True, operator="someone-else")


def test_apply_rejects_stale_source_hash(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    # Dataset drifts after authorization was approved.
    path = os.path.join(data_dir, CANONICAL_FILE)
    payload = json.load(open(path, encoding="utf-8"))
    payload["matches"][0]["home"] = "改名后的主队"
    write_json_atomic(path, payload)
    drifted = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True)
    assert snapshot_dir_hashes(data_dir) == drifted


def test_apply_rejects_unexpected_duplicate_groups(tmp_path):
    data_dir = build_dataset(str(tmp_path), extra_dup_group=True)
    artifact = manual_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_three_occurrences(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=3)
    artifact = manual_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_lock_contention_fails_closed(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))

    with acquire_repair_lock(data_dir):
        with pytest.raises(StagingLockError):
            repairer.apply(artifact, confirm_apply=True)


# ===========================================================================
# 5. Happy path
# ===========================================================================

def test_apply_happy_path_enforces_every_postcondition(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before_stores = snapshot_dir_hashes(data_dir)
    original = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))

    repairer, result = apply_artifact(tmp_path, data_dir, artifact)

    assert result["status"] == "SUCCESS"
    assert result["before_total"] == original["meta"]["total"]
    assert result["after_total"] == original["meta"]["total"] - 1
    assert result["duplicate_ids_count"] == 0
    assert result["immutable_stores_changed"] == 0
    assert result["candidate_sha256"] == artifact["expected_candidate_sha256"]

    report = inspect_dataset(data_dir)
    assert report["duplicate_ids"] == {}
    assert report["total_matches"] == report["unique_ids_count"] == result["after_total"]

    current = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    assert current["meta"]["total"] == result["after_total"]
    assert current["meta"]["last_refresh"] == original["meta"]["last_refresh"]
    assert current["meta"]["sources"] == original["meta"]["sources"]
    assert current["schema_version"] == original["schema_version"]

    # Unrelated records preserved byte-for-byte, in the original relative order.
    before_unrelated = [m for m in original["matches"] if m.get("id") != TARGET_ID]
    after_unrelated = [m for m in current["matches"] if m.get("id") != TARGET_ID]
    assert before_unrelated == after_unrelated

    # Target appears once, at the position of the first duplicate.
    assert [m["id"] for m in current["matches"]].count(TARGET_ID) == 1
    assert hash_record([m for m in current["matches"] if m["id"] == TARGET_ID][0]) == result["candidate_sha256"]

    # Six non-Canonical stores untouched; only the Canonical file changed.
    after_stores = snapshot_dir_hashes(data_dir)
    for store in NON_CANONICAL_STORES:
        assert after_stores[store] == before_stores[store]
    assert after_stores[CANONICAL_FILE] != before_stores[CANONICAL_FILE]

    # Journal completed the full seven-phase sequence.
    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states == JOURNAL_STATES

    assert os.path.exists(result["result_file"])
    assert repairer.verify(artifact)["verified"] is True


def test_apply_is_idempotent(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer, _result = apply_artifact(tmp_path, data_dir, artifact)

    journal_before = repairer.journal.read_entries()
    hashes_before = snapshot_dir_hashes(data_dir)

    second = repairer.apply(artifact, confirm_apply=True)

    assert second["status"] == "NO_OP_ALREADY_REPAIRED"
    assert snapshot_dir_hashes(data_dir) == hashes_before
    assert repairer.journal.read_entries() == journal_before


def test_derived_hash_matches_on_disk_bytes(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    on_disk = hashlib.sha256(open(os.path.join(data_dir, CANONICAL_FILE), "rb").read()).hexdigest()
    assert derive_file_hash(payload) == on_disk


# ===========================================================================
# 6. Immutable store protection
# ===========================================================================

def test_immutable_store_mutation_is_detected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact["migration_id"],
    )

    with pytest.raises(ImmutableStoreMutationError):
        repairer.apply(
            artifact,
            confirm_apply=True,
            fault_inject="corrupt_store_pre_postconditions",
        )

    # The projection was committed but completion was correctly withheld.
    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states[-1] == "PROJECTION_COMMITTED"
    assert "COMPLETION_RECORDED" not in states

    # Recovery must also fail closed rather than paper over the mutation.
    with pytest.raises(ImmutableStoreMutationError):
        repairer.recover()


# ===========================================================================
# 7. Crash recovery
# ===========================================================================

def test_recover_rolls_back_from_commit_intent_then_apply_succeeds(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    baseline_bytes = open(os.path.join(data_dir, CANONICAL_FILE), "rb").read()

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact["migration_id"],
    )
    with pytest.raises(Exception):
        repairer.apply(artifact, confirm_apply=True, fault_inject="after_commit_intent")

    assert open(os.path.join(data_dir, CANONICAL_FILE), "rb").read() == baseline_bytes

    recovery = repairer.recover()
    assert recovery["status"] == "RECOVERED_ROLLED_BACK"
    assert open(os.path.join(data_dir, CANONICAL_FILE), "rb").read() == baseline_bytes

    result = repairer.apply(artifact, confirm_apply=True)
    assert result["status"] == "SUCCESS"
    assert inspect_dataset(data_dir)["duplicate_ids"] == {}
    assert repairer.verify(artifact)["verified"] is True


def test_recover_completes_forward_when_projection_already_written(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact["migration_id"],
    )
    with pytest.raises(Exception):
        repairer.apply(artifact, confirm_apply=True, fault_inject="after_replace_before_committed")

    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states[-1] == "COMMIT_INTENT_DURABLE"
    assert inspect_dataset(data_dir)["duplicate_ids"] == {}

    recovery = repairer.recover()
    assert recovery["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert [e["state"] for e in repairer.journal.read_entries()][-1] == "COMPLETION_RECORDED"
    assert repairer.verify(artifact)["verified"] is True


def test_recover_fails_closed_on_unknown_state(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact["migration_id"],
    )
    with pytest.raises(Exception):
        repairer.apply(artifact, confirm_apply=True, fault_inject="after_commit_intent")

    # Corrupt the active file into a state matching neither baseline nor projection.
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    payload["matches"] = payload["matches"][:2]
    write_json_atomic(os.path.join(data_dir, CANONICAL_FILE), payload)

    with pytest.raises(RecoveryStateError):
        repairer.recover()


def test_recover_without_journal_is_a_no_op(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    assert repairer.recover()["status"] == "NO_JOURNAL_ENTRIES"


# ===========================================================================
# 8. CLI surface
# ===========================================================================

def test_cli_authorize_apply_verify_roundtrip(tmp_path, capsys):
    from tools.production_canonical_repair import main

    data_dir = build_dataset(str(tmp_path))
    auth_path = str(tmp_path / "authorization.json")
    workspace = str(tmp_path / "ws")

    assert main(["inspect", "--data-dir", data_dir]) == 0
    capsys.readouterr()

    assert main([
        "authorize", "--data-dir", data_dir, "--target-id", TARGET_ID,
        "--operator", "ops-oncall", "--reason", "cli roundtrip", "--out", auth_path,
    ]) == 0
    capsys.readouterr()
    assert os.path.exists(auth_path)

    assert main(["dry-run", "--data-dir", data_dir, "--authorization", auth_path, "--workspace", workspace]) == 0
    capsys.readouterr()

    assert main([
        "apply", "--data-dir", data_dir, "--authorization", auth_path,
        "--workspace", workspace, "--operator", "ops-oncall", "--confirm-apply",
    ]) == 0
    capsys.readouterr()

    assert main(["verify", "--data-dir", data_dir, "--authorization", auth_path]) == 0
    capsys.readouterr()

    # Applying without --confirm-apply is refused with the authorization exit code.
    assert main([
        "apply", "--data-dir", data_dir, "--authorization", auth_path, "--workspace", workspace,
    ]) == 110
