"""
tests/test_real_sports_identity_audit.py
========================================
Correctness tests for ``tools/real_sports_identity_audit.py``.

Scope discipline
----------------
Every fixture in this module is **synthetic** and exists only to verify code
behaviour: ordering, fail-closed handling, metric denominators and read-only
safety.  Synthetic fixtures never stand in for a business measurement; the
real-data acceptance result lives in ``docs/analysis/`` and is produced from a
separate, explicitly-labelled local run (see the last test, which skips when no
local dataset is present, as in CI).
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from tools.real_sports_identity_audit import (
    AUDIT_KIND,
    DECISION,
    EXIT_OK,
    EXIT_PROTECTED_CHANGE,
    HUMAN_LABEL_AVAILABILITY,
    LABEL_PROVENANCE,
    PRODUCTION_ACTIVATION_READY,
    PROTECTED_STORES,
    SAME_EVENT,
    AMBIGUOUS,
    DIFFERENT_EVENT,
    INSUFFICIENT_EVIDENCE,
    AuditInputError,
    analyze_canonical_ownership,
    analyze_downstream,
    analyze_pairs,
    build_inventory,
    classify_pair_evidence,
    compute_audit,
    default_data_dir,
    deterministic_view,
    load_dataset,
    main,
    observe_canonical,
    replay_planner,
    team_relation,
    verify_protected_stores,
)
from utils.match_identity import same_event
from utils.source_identity import detect_all_conflicts, observe_source_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_METRIC_KEYS = {
    "id",
    "name",
    "definition",
    "numerator",
    "denominator",
    "value",
    "unit",
    "status",
    "data_scope",
    "evidence_quality",
    "limitations",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_match(match_id: str, **overrides):
    """One synthetic Canonical row with the real store's field shape."""
    match = {
        "id": match_id,
        "sport": "football",
        "league": "测试联赛",
        "date": "2030-05-01",
        "time": "20:00",
        "home": "主队A",
        "away": "客队B",
        "status": "upcoming",
        "jczq_no": "",
        "score": None,
        "odds": None,
    }
    match.update(overrides)
    return match


def write_dataset(
    root,
    matches,
    *,
    predictions=None,
    odds=None,
    settlements=None,
    rows=None,
    omit=(),
    meta=None,
):
    """Write a complete or deliberately incomplete seven-store fixture."""
    root = str(root)
    os.makedirs(root, exist_ok=True)
    payloads = {
        "daily_matches.json": {"matches": list(matches), "meta": dict(meta or {})},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": {"snapshots": dict(predictions or {}), "version": 1},
        "odds_snapshots.json": {"snapshots": dict(odds or {}), "version": 1},
        "settlements.json": {"settlements": dict(settlements or {}), "version": 1},
        "evaluation_rows.json": {"rows": dict(rows or {}), "version": 1},
    }
    for name in PROTECTED_STORES:
        if name in omit:
            continue
        path = os.path.join(root, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payloads[name], handle, ensure_ascii=False, sort_keys=True)
    return root


def store_hashes(root):
    out = {}
    for name in PROTECTED_STORES:
        path = os.path.join(str(root), name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def snapshot(snapshot_id, match_id, **overrides):
    record = {
        "snapshot_id": snapshot_id,
        "match_id": match_id,
        "sport": "football",
        "league": "测试联赛",
        "home_team": "主队A",
        "away_team": "客队B",
        "match_date": "2030-05-01",
        "match_time": "20:00",
        "kickoff_at": "2030-05-01T20:00:00+08:00",
        "generated_at": "2030-04-30T12:00:00+08:00",
        "model_version": "test-1",
    }
    record.update(overrides)
    return record


def settlement(settlement_id, snapshot_id, match_id, **overrides):
    record = {
        "settlement_id": settlement_id,
        "snapshot_id": snapshot_id,
        "match_id": match_id,
        "sport": "football",
        "home_team": "主队A",
        "away_team": "客队B",
        "final_score": {"home": 2, "away": 1},
        "actual_outcome": "home_win",
        "result_fingerprint": "placeholder",
        "model_version": "test-1",
        "settled_at": "2030-05-02T00:00:00+08:00",
    }
    record.update(overrides)
    return record


def evaluation_row(evaluation_id, snapshot_id, settlement_id, match_id, **overrides):
    record = {
        "evaluation_id": evaluation_id,
        "snapshot_id": snapshot_id,
        "settlement_id": settlement_id,
        "match_id": match_id,
        "sport": "football",
        "home_team": "主队A",
        "away_team": "客队B",
        "final_score": {"home": 2, "away": 1},
        "actual_outcome": "home_win",
        "model_version": "test-1",
    }
    record.update(overrides)
    return record


def audit(root, **kwargs):
    return compute_audit(load_dataset(str(root)), **kwargs)


def metric(report, metric_id):
    for item in report["metrics"]:
        if item["id"] == metric_id:
            return item
    raise AssertionError(f"metric {metric_id!r} not present")


def without_paths(report):
    """Deterministic view with the input directory path removed."""
    view = deterministic_view(report)
    dataset = dict(view["dataset"])
    dataset.pop("data_dir", None)
    view["dataset"] = dataset
    return view


# ---------------------------------------------------------------------------
# 1. Read-only safety and seven-store protection
# ---------------------------------------------------------------------------

def test_audit_report_declares_read_only_and_no_activation(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")])
    report = audit(root)
    assert report["audit_kind"] == AUDIT_KIND
    assert report["read_only"] is True
    assert report["network_access"] == "none"
    assert report["production_writes"] == "none"
    assert report["production_activation_ready"] is False
    assert PRODUCTION_ACTIVATION_READY is False
    assert report["decision"] == DECISION == "NO-GO"


def test_all_seven_protected_stores_remain_byte_identical(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001")],
        predictions={"ps-1": snapshot("ps-1", "500l-2030-05-01-2001")},
    )
    before = store_hashes(root)
    dataset = load_dataset(str(root))
    report = compute_audit(dataset)
    after = verify_protected_stores(dataset)
    assert store_hashes(root) == before
    assert report["dataset"]["store_sha256"] == before
    assert after == before


def test_output_inside_the_repository_is_refused(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")])
    target = os.path.join(ROOT, "audit-report-must-not-exist.json")
    code = main(["--data-dir", root, "--output", target])
    assert code == EXIT_PROTECTED_CHANGE
    assert not os.path.exists(target)


def test_output_outside_the_repository_is_written(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")])
    target = os.path.join(str(tmp_path), "report.json")
    code = main(["--data-dir", root, "--output", target, "--json"])
    assert code == EXIT_OK
    with open(target, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["audit_kind"] == AUDIT_KIND
    assert payload["dataset"]["stores_unchanged"] is True


def test_tool_source_contains_no_network_or_writer_imports():
    with open(
        os.path.join(ROOT, "tools", "real_sports_identity_audit.py"), encoding="utf-8"
    ) as handle:
        source = handle.read()
    for forbidden in ("import socket", "import requests", "urllib", "http.client", "aiohttp"):
        assert forbidden not in source
    assert "scraper" not in source
    assert "refresh(" not in source


# ---------------------------------------------------------------------------
# 2. Malformed input fails safely
# ---------------------------------------------------------------------------

def test_malformed_json_fails_closed(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001")])
    with open(os.path.join(root, "odds_snapshots.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with pytest.raises(AuditInputError):
        load_dataset(root)


def test_missing_matches_list_fails_closed(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001")])
    with open(os.path.join(root, "daily_matches.json"), "w", encoding="utf-8") as handle:
        json.dump({"meta": {}}, handle)
    with pytest.raises(AuditInputError):
        load_dataset(root)


def test_duplicate_canonical_ids_fail_closed(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001"), make_match("500l-2030-05-01-2001")],
    )
    with pytest.raises(AuditInputError):
        load_dataset(root)


def test_non_object_match_record_fails_closed(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001")])
    payload = {"matches": ["not-an-object"], "meta": {}}
    with open(os.path.join(root, "daily_matches.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    with pytest.raises(AuditInputError):
        load_dataset(root)


def test_cli_reports_input_error_code(tmp_path, capsys):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001")])
    with open(os.path.join(root, "daily_matches.json"), "w", encoding="utf-8") as handle:
        handle.write("[]")
    code = main(["--data-dir", root])
    assert code == 2
    assert "AUDIT INPUT ERROR" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3. Duplicate provider identity / row-position reuse / cross-sport collision
# ---------------------------------------------------------------------------

def test_duplicate_source_identity_key_is_detected(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500j-2030-05-01-301", jczq_no="301", league="联赛甲"),
            make_match(
                "500l-2030-05-01-301",
                jczq_no="301",
                league="联赛乙",
                home="主队C",
                away="客队D",
            ),
        ],
    )
    report = audit(root)
    assert report["identity"]["duplicate_identity_key_count"] == 1
    duplicate = report["identity"]["duplicate_identity_keys"][0]
    assert duplicate["identity_kind"] == "COMPETITION_NUMBER"
    assert duplicate["canonical_ids"] == [
        "500j-2030-05-01-301",
        "500l-2030-05-01-301",
    ]
    assert metric(report, "duplicate_source_key_rate")["numerator"] == 1


def test_reused_row_position_identifier_is_flagged_and_insufficient():
    first = make_match("500w-2030-05-01-1", home="主队A", away="客队B")
    second = make_match("500w-2030-05-01-1", home="主队C", away="客队D")
    observations = [
        observe_source_row(first, adapter="finished_feed"),
        observe_source_row(second, adapter="finished_feed"),
    ]
    assert observations[0].identifier_stability == "ROW_POSITION_UNSTABLE"
    conflicts = detect_all_conflicts(observations)
    assert any(conflict.category == "ROW_POSITION_REUSE" for conflict in conflicts)


def test_row_position_identifier_is_never_sufficient(tmp_path):
    root = write_dataset(tmp_path, [make_match("500w-2030-05-01-1")])
    report = audit(root)
    item = report["identity"]["insufficient_examples"][0]
    assert item["canonical_id"] == "500w-2030-05-01-1"
    assert item["identifier_stability"] == "ROW_POSITION_UNSTABLE"
    assert metric(report, "insufficient_evidence_rate")["numerator"] == 1


def test_cross_sport_shared_namespace_is_flagged():
    football = make_match("500j-2030-05-01-301", sport="football", jczq_no="301")
    basketball = make_match(
        "500j-2030-05-01-301",
        sport="basketball",
        jczq_no="301",
        odds={"matchnum": "301"},
    )
    observations = [
        observe_source_row(football, adapter="market_xml"),
        observe_source_row(basketball, adapter="market_xml"),
    ]
    conflicts = detect_all_conflicts(observations)
    categories = {conflict.category for conflict in conflicts}
    assert "CROSS_SPORT_SHARED_NAMESPACE" in categories


def test_shared_numeric_competition_number_across_sports_does_not_collide(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500j-2030-05-01-301", sport="football", jczq_no="301"),
            make_match(
                "500j-2030-05-01-302",
                sport="basketball",
                jczq_no="301",
                home="主队C",
                away="客队D",
            ),
        ],
    )
    report = audit(root)
    assert report["identity"]["duplicate_identity_key_count"] == 0
    keys = report["identity"]["identity_kind_distribution"]
    assert keys == {"COMPETITION_NUMBER": 2}


# ---------------------------------------------------------------------------
# 4. Equivalence evidence: same team, different event, ambiguity
# ---------------------------------------------------------------------------

def test_team_relation_distinguishes_truncation_from_contradiction():
    assert team_relation("主队A", "主队A") == "EXACT"
    assert team_relation("主队 A", "主队A") == "NORMALIZED_EQUAL"
    assert team_relation("哈萨克斯坦", "哈萨克") == "ABBREVIATION"
    assert team_relation("主队A", "主队Z") == "DIFFERENT"
    assert team_relation(None, "主队A") == "MISSING"


def test_different_teams_are_a_different_event():
    left = make_match("500l-2030-05-01-2001", jczq_no="2001")
    right = make_match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D")
    label, evidence = classify_pair_evidence(left, right)
    assert label == DIFFERENT_EVENT
    assert evidence["reason"] == "team names contradict"


def test_same_teams_with_different_kickoff_are_ambiguous():
    left = make_match("500l-2030-05-01-2001", jczq_no="2001", time="20:00")
    right = make_match("500l-2030-05-01-2002", jczq_no="2002", time="22:00")
    label, evidence = classify_pair_evidence(left, right)
    assert label == AMBIGUOUS
    assert evidence["reason"] == "kickoff time disagreement"


def test_same_teams_with_conflicting_competition_numbers_are_ambiguous():
    left = make_match("500j-2030-05-01-301", jczq_no="301")
    right = make_match("500j-2030-05-01-302", jczq_no="302")
    label, evidence = classify_pair_evidence(left, right)
    assert label == AMBIGUOUS
    assert evidence["reason"] == "competition number disagreement"


def test_same_teams_with_conflicting_results_are_ambiguous():
    left = make_match(
        "500l-2030-05-01-2001", status="finished", score={"ft": [2, 1]}
    )
    right = make_match(
        "500l-2030-05-01-2002", status="finished", score={"ft": [0, 0]}
    )
    label, evidence = classify_pair_evidence(left, right)
    assert label == AMBIGUOUS
    assert evidence["reason"] == "final score disagreement"


def test_truncated_names_are_never_auto_confirmed(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2003", jczq_no="2003", home="哈萨克"),
            make_match(
                "500l-2030-05-01-1398660",
                home="哈萨克斯坦",
                time="20:00",
                status="finished",
                score={"ft": [2, 2]},
            ),
        ],
    )
    report = audit(root)
    assert report["pairs"]["label_distribution"].get(AMBIGUOUS) == 1
    assert report["canonical_ownership"]["duplicate_cluster_count"] == 0
    assert report["canonical_ownership"]["ambiguous_cluster_count"] == 1
    kinds = {item["kind"] for item in report["review_queue"]["items"]}
    assert "AMBIGUOUS_EQUIVALENCE" in kinds
    for item in report["review_queue"]["items"]:
        assert item["reviewer_label"] is None


def test_missing_kickoff_time_does_not_fabricate_a_time():
    left = make_match("500j-2030-05-01-301", time="00:00", jczq_no="301")
    right = make_match("500j-2030-05-01-301-copy", time="00:00", jczq_no="301")
    label, evidence = classify_pair_evidence(left, right)
    assert label == SAME_EVENT
    assert evidence["left_kickoff"] is None and evidence["right_kickoff"] is None


def test_insufficient_evidence_when_teams_are_missing():
    left = make_match("500l-2030-05-01-2001", home="", jczq_no="2001")
    right = make_match("500l-2030-05-01-2002", jczq_no="2002")
    label, evidence = classify_pair_evidence(left, right)
    assert label == INSUFFICIENT_EVIDENCE
    assert evidence["reason"] == "missing team names"


# ---------------------------------------------------------------------------
# 5. False-positive and false-negative candidates
# ---------------------------------------------------------------------------

def test_false_positive_candidate_is_counted(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001", time="20:00"),
            make_match("500l-2030-05-01-999999", time="22:00"),
        ],
    )
    assert same_event(
        make_match("500l-2030-05-01-2001", jczq_no="2001"),
        make_match("500l-2030-05-01-999999"),
    ) is True
    report = audit(root)
    assert report["pairs"]["proxy_false_positive_count"] == 1
    assert metric(report, "proxy_false_positive_rate")["numerator"] == 1


def test_false_negative_candidate_is_counted(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001", home="主队 A"),
            make_match("500l-2030-05-01-888888", home="主队A"),
        ],
    )
    assert same_event(
        make_match("500l-2030-05-01-2001", jczq_no="2001", home="主队 A"),
        make_match("500l-2030-05-01-888888", home="主队A"),
    ) is False
    report = audit(root)
    assert report["pairs"]["proxy_false_negative_count"] == 1
    assert metric(report, "proxy_false_negative_rate")["numerator"] == 1


def test_identical_records_are_never_duplicated_within_one_store(tmp_path):
    root = write_dataset(
        tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")]
    )
    report = audit(root)
    assert report["pairs"]["scoped_pairs_compared"] == 0
    assert metric(report, "proxy_false_positive_rate")["status"] == "N/A_ZERO_DENOMINATOR"


# ---------------------------------------------------------------------------
# 6. Canonical ownership and planner replay
# ---------------------------------------------------------------------------

def test_duplicate_canonical_ownership_blocks_the_planner(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001"),
            make_match("500l-2030-05-01-1398620"),
        ],
    )
    report = audit(root)
    ownership = report["canonical_ownership"]
    assert ownership["duplicate_cluster_count"] == 1
    assert ownership["canonical_ids_in_duplicate_clusters"] == 2
    planner = report["planner_replay"]
    assert planner["raw_replay"]["status"] == "BLOCKED"
    assert planner["final_replay"]["status"] == "OK"
    assert planner["review_only_recommended_drops"] == ["500l-2030-05-01-1398620"]
    assert metric(report, "automatic_mapping_coverage")["numerator"] == 1
    # The audit only measured; the store still holds both records.
    assert len(load_dataset(root).matches) == 2


def test_replay_is_idempotent_when_no_conflict_exists():
    matches = [
        make_match("500l-2030-05-01-2001", jczq_no="2001"),
        make_match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
    ]
    result = replay_planner(matches, [])
    assert result["raw_replay"]["status"] == "OK"
    assert result["raw_replay"]["idempotent_mismatch_count"] == 0
    assert result["review_only_recommended_drops"] == []


def test_ambiguous_pairs_never_become_duplicate_clusters():
    left = make_match("500l-2030-05-01-2003", jczq_no="2003", home="哈萨克")
    right = make_match("500l-2030-05-01-1398660", home="哈萨克斯坦")
    ownership = analyze_canonical_ownership([left, right])
    assert ownership["duplicate_cluster_count"] == 0
    assert ownership["ambiguous_cluster_count"] == 1


# ---------------------------------------------------------------------------
# 7. Downstream linkage
# ---------------------------------------------------------------------------

def test_incomplete_lifecycle_is_absence_not_an_error(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001", status="finished", score={"ft": [1, 0]})],
        omit=("prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json"),
    )
    dataset = load_dataset(root)
    downstream = analyze_downstream(dataset)
    assert downstream["finished_events"] == 1
    assert downstream["finished_events_with_final_score"] == 1
    assert downstream["orphaned_settlements"] == 0
    assert downstream["settlement_snapshot_missing"] == 0
    report = compute_audit(dataset)
    assert metric(report, "settlement_linkage_consistency_rate")["status"] == "N/A_ZERO_DENOMINATOR"
    assert metric(report, "final_score_linkage_rate")["value"] == 1.0


def test_finished_event_without_score_is_reported(tmp_path):
    root = write_dataset(
        tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001", status="finished")]
    )
    report = audit(root)
    assert report["downstream"]["finished_events_without_final_score"] == [
        "500l-2030-05-01-2001"
    ]
    assert metric(report, "final_score_linkage_rate")["numerator"] == 0


def test_stale_canonical_reference_is_reported_as_orphan(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001")],
        predictions={"ps-stale": snapshot("ps-stale", "500l-2030-05-01-9999")},
    )
    report = audit(root)
    downstream = report["downstream"]
    assert downstream["orphaned_prediction_snapshots"] == 1
    assert downstream["distinct_events_with_prediction"] == 0
    checks = downstream["reference_checks"]["prediction_snapshots.json"]
    assert checks["ORPHANED_MATCH_REFERENCE"] == 1


def test_invalid_referenced_ids_are_reported_not_crashed(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001")],
        rows={
            "er-1": evaluation_row(
                "er-1", "ps-does-not-exist", "st-does-not-exist", "500l-2030-05-01-2001"
            )
        },
    )
    report = audit(root)
    downstream = report["downstream"]
    assert downstream["evaluation_snapshot_missing"] == 1
    assert downstream["evaluation_settlement_missing"] == 1
    consistency = metric(report, "evaluation_row_linkage_consistency_rate")
    assert consistency["value"] == 0.0
    assert consistency["numerator"] >= 0


def test_settlement_snapshot_match_mismatch_is_detected(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001"),
            make_match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
        ],
        predictions={
            "ps-1": snapshot("ps-1", "500l-2030-05-01-2001"),
        },
        settlements={
            "st-1": settlement("st-1", "ps-1", "500l-2030-05-01-2002"),
        },
    )
    report = audit(root)
    assert report["downstream"]["settlement_snapshot_match_mismatch"] == 1


def test_result_fingerprint_mismatch_is_detected(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001", status="finished", score={"ft": [2, 1]})],
        predictions={"ps-1": snapshot("ps-1", "500l-2030-05-01-2001")},
        settlements={"st-1": settlement("st-1", "ps-1", "500l-2030-05-01-2001")},
    )
    report = audit(root)
    assert report["downstream"]["settlement_result_fingerprint_mismatch"] == 1
    checks = report["downstream"]["reference_checks"]["settlements.json"]
    assert checks["RESULT_FINGERPRINT_MISMATCH"] == 1


def test_downstream_abbreviated_team_name_is_counted(tmp_path):
    root = write_dataset(
        tmp_path,
        [make_match("500l-2030-05-01-2001", jczq_no="2001", home="哈萨克斯坦")],
        predictions={
            "ps-1": snapshot("ps-1", "500l-2030-05-01-2001", home_team="哈萨克")
        },
    )
    report = audit(root)
    checks = report["downstream"]["reference_checks"]["prediction_snapshots.json"]
    assert checks.get("TEAM_NAME_ABBREVIATION") == 1


# ---------------------------------------------------------------------------
# 8. Ground truth separation and N/A discipline
# ---------------------------------------------------------------------------

def test_absent_ground_truth_is_never_claimed_as_precision(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")])
    report = audit(root)
    assert report["ground_truth"]["human_reviewed_labels_available"] is False
    assert report["ground_truth"]["proxy_label_provenance"] == LABEL_PROVENANCE
    assert HUMAN_LABEL_AVAILABILITY in report["review_queue"]["human_label_availability"]
    for metric_id in ("automatic_mapping_precision", "mapping_recall"):
        item = metric(report, metric_id)
        assert item["status"] == "N/A_NO_INDEPENDENT_LABELS"
        assert item["numerator"] is None and item["denominator"] is None
        assert item["value"] is None
        assert item["evidence_quality"] == "UNKNOWN"


def test_zero_denominators_are_reported_as_unavailable(tmp_path):
    root = write_dataset(tmp_path, [])
    report = audit(root)
    assert report["inventory"]["events"] == 0
    for item in report["metrics"]:
        if item["denominator"] == 0:
            assert item["status"] == "N/A_ZERO_DENOMINATOR"
            assert item["value"] is None
    assert metric(report, "canonical_conflict_rate")["status"] == "N/A_ZERO_DENOMINATOR"


def test_missing_optional_stores_are_reported_as_evidence_gaps(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match(
                "500l-2030-05-01-2001",
                jczq_no="2001",
                odds={"home_win": 2.1, "draw": 3.2, "away_win": 3.4, "matchnum": "2001"},
            )
        ],
        omit=tuple(name for name in PROTECTED_STORES if name != "daily_matches.json"),
    )
    report = audit(root)
    assert len(report["inventory"]["evidence_gaps"]) == 6
    assert report["dataset"]["stores_present"] == ["daily_matches.json"]
    assert len(report["dataset"]["stores_absent"]) == 6
    assert metric(report, "odds_snapshot_linkage_rate")["denominator"] == 1
    assert metric(report, "odds_snapshot_linkage_rate")["numerator"] == 0


def test_every_metric_has_a_uniform_shape_with_explicit_denominator(tmp_path):
    root = write_dataset(tmp_path, [make_match("500l-2030-05-01-2001", jczq_no="2001")])
    report = audit(root)
    assert report["metrics"], "metrics must not be empty"
    allowed = {
        "MEASURED",
        "MEASURED_LOCAL_ONLY",
        "ESTIMATE",
        "N/A",
        "N/A_ZERO_DENOMINATOR",
        "N/A_NO_INDEPENDENT_LABELS",
    }
    for item in report["metrics"]:
        assert set(item) >= REQUIRED_METRIC_KEYS
        assert item["status"] in allowed
        if item["status"].startswith("MEASURED"):
            assert item["denominator"] and item["denominator"] > 0
            assert item["value"] is not None


# ---------------------------------------------------------------------------
# 9. Determinism
# ---------------------------------------------------------------------------

def test_repeated_audit_is_deterministic(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001"),
            make_match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
        ],
        predictions={"ps-1": snapshot("ps-1", "500l-2030-05-01-2001")},
    )
    first = json.dumps(deterministic_view(audit(root)), sort_keys=True, ensure_ascii=False)
    second = json.dumps(deterministic_view(audit(root)), sort_keys=True, ensure_ascii=False)
    assert first == second


def test_record_order_does_not_change_the_result(tmp_path):
    matches = [
        make_match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
        make_match("500l-2030-05-01-2001", jczq_no="2001"),
    ]
    forward = audit(write_dataset(tmp_path / "a", matches))
    backward = audit(write_dataset(tmp_path / "b", list(reversed(matches))))
    # File bytes legitimately differ (row order), so hashes are excluded here;
    # everything derived from the record set must be identical.
    forward.pop("dataset")
    backward.pop("dataset")
    # JSON-pointer paths inside the source file are positional by definition and
    # are reported for auditability, so they are excluded from this comparison.
    forward["downstream"].pop("store_reference_enumeration")
    backward["downstream"].pop("store_reference_enumeration")
    assert json.dumps(deterministic_view(forward), sort_keys=True, ensure_ascii=False) == (
        json.dumps(deterministic_view(backward), sort_keys=True, ensure_ascii=False)
    )


def test_inventory_is_ordered_and_attributable(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            make_match("500l-2030-05-01-2001", jczq_no="2001"),
            make_match("500j-2030-05-01-301", jczq_no="301", sport="basketball"),
        ],
    )
    dataset = load_dataset(root)
    inventory = build_inventory(dataset, observe_canonical(dataset.matches))
    assert inventory["events"] == 2
    assert inventory["namespace_distribution"] == {"500j": 1, "500l": 1}
    assert inventory["sport_distribution"] == {"basketball": 1, "football": 1}
    assert inventory["missing_field_counts"].get("jczq_no", 0) == 0
    assert inventory["raw_provider_ids_preserved"] == 0


# ---------------------------------------------------------------------------
# 10. Real local dataset (business acceptance stays separate from these tests)
# ---------------------------------------------------------------------------

REAL_CANONICAL = os.path.join(default_data_dir(), "daily_matches.json")


@pytest.mark.skipif(
    not os.path.isfile(REAL_CANONICAL),
    reason="no local real dataset (git-ignored); synthetic tests only in CI",
)
def test_real_dataset_audit_is_measured_and_reproducible():
    """
    Local-only smoke validation on the real dataset.

    This asserts *behaviour on real bytes*, never business acceptance: the
    acceptance figures are reported separately in ``docs/analysis/``.
    """
    data_dir = default_data_dir()
    before = store_hashes(data_dir)
    dataset = load_dataset(data_dir)
    report = compute_audit(dataset)
    after = verify_protected_stores(dataset)

    assert report["inventory"]["events"] > 0
    assert before == after == store_hashes(data_dir)
    assert report["dataset"]["store_sha256"] == before
    for item in report["metrics"]:
        if item["status"].startswith("MEASURED"):
            assert item["denominator"] and item["denominator"] > 0
            assert item["value"] is not None
    assert report["pairs"]["label_provenance"] == LABEL_PROVENANCE
    assert report["production_activation_ready"] is False
    assert json.dumps(deterministic_view(report), sort_keys=True, ensure_ascii=False) == (
        json.dumps(deterministic_view(compute_audit(load_dataset(data_dir))),
                   sort_keys=True, ensure_ascii=False)
    )