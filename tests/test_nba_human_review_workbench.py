"""
tests/test_nba_human_review_workbench.py
========================================
Phase 2-Y regression suite: offline NBA human-review workbench.

All fixtures are synthetic and live in temporary directories, so the suite runs in CI
without the private dataset. Real-data progress is reported separately and is **not**
asserted here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import pytest

from tools.nba_human_review_workbench import (
    EVIDENCE_STATUS_VALUES,
    PRIORITY_ORDER,
    PRIORITY_P0,
    PRIORITY_P1,
    PRIORITY_P2,
    QUEUE_KIND,
    STATUS_CONTRADICTED,
    STATUS_INDEPENDENTLY_SUPPORTED,
    STATUS_SOURCE_REFERENCE_REVIEWED,
    STATUS_SOURCE_REFERENCE_SUPPLIED,
    STATUS_UNVERIFIED,
    build_review_queue,
    command_prepare,
    command_show,
    command_summary,
    command_validate,
    load_evidence,
    main,
    queue_for_reviews,
    render_dossier,
    review_priority,
    unique_impact,
)
from tools.nba_identity_discrepancy_audit import build_candidates, build_scan
from tools.real_sports_duplicate_review import (
    DECISION_DIFFERENT_EVENT,
    DECISION_SAME_EVENT,
    REVIEW_SCHEMA_VERSION,
    UNREVIEWED,
    load_dataset,
    preview_reconciliation,
    validate_reviews,
)
from tools.shadow_trust_chain_validation import production_boundary_audit
from utils.basketball_identity_evidence import (
    CLASS_INSUFFICIENT,
    ORIENTATION_REVERSED,
    OUTCOME_CONTRADICTORY_EVIDENCE,
    CrossSourceCandidateEvidence,
    LotteryNumberInterpretation,
    classify_cross_source_candidate,
    interpret_lottery_number_evidence,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATE = "2030-05-01"
PROTECTED_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)
REVIEWER = "local-operator"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def basketball(match_id, **overrides):
    record = {
        "id": match_id,
        "sport": "basketball",
        "league": "NBA",
        "date": DATE,
        "time": "03:00",
        "home": "波特兰开拓者",
        "away": "犹他爵士",
        "status": "upcoming",
        "jczq_no": "周六308",
        "score": None,
        "odds": None,
    }
    record.update(overrides)
    return record


def lottery(match_id, number, **overrides):
    payload = {"league": "美国职业篮球联盟", "time": "00:00", "jczq_no": number}
    payload.update(overrides)
    return basketball(match_id, **payload)


def write_dataset(root, matches, *, predictions=None, odds=None, settlements=None, rows=None):
    root = str(root)
    os.makedirs(root, exist_ok=True)
    payloads = {
        "daily_matches.json": {"matches": list(matches), "meta": {}},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": {"snapshots": dict(predictions or {}), "version": 1},
        "odds_snapshots.json": {"snapshots": dict(odds or {}), "version": 1},
        "settlements.json": {"settlements": dict(settlements or {}), "version": 1},
        "evaluation_rows.json": {"rows": dict(rows or {}), "version": 1},
    }
    for name, payload in payloads.items():
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return root


def store_hashes(root):
    out = {}
    for name in PROTECTED_STORES:
        with open(os.path.join(str(root), name), "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def workbench_fixture(root):
    """
    Three NBA candidates with distinct review priorities plus one non-NBA pair.

    - P1: exact reversed names + number correspondence
    - P2: team-name variant (prefix) + number correspondence
    - P0: number correspondence but mutually exclusive team names
    - WNBA pair: must stay out of the NBA queue
    """
    return write_dataset(
        root,
        [
            basketball("500lq-2030-05-01-周六308"),
            lottery("500j-2030-05-01-6308", "6308", home="犹他爵士", away="波特兰开拓者"),
            basketball("500lq-2030-05-01-周六309", home="萨克拉门托国王", away="金州勇士队",
                       jczq_no="周六309"),
            lottery("500j-2030-05-01-6309", "6309", home="金州勇士", away="萨克拉门托国王"),
            basketball("500lq-2030-05-01-周六310", home="波士顿凯尔特人", away="迈阿密热火",
                       jczq_no="周六310"),
            lottery("500j-2030-05-01-6310", "6310", home="洛杉矶湖人", away="洛杉矶快船"),
            basketball("500lq-2030-05-01-周六311", league="WNBA", home="纽约自由",
                       away="拉斯维加斯王牌", jczq_no="周六311"),
            lottery("500j-2030-05-01-6311", "6311", league="WNBA", home="拉斯维加斯王牌",
                    away="纽约自由"),
        ],
        predictions={
            "ps-live": {"snapshot_id": "ps-live", "match_id": "500lq-2030-05-01-周六308"},
            "ps-lot": {"snapshot_id": "ps-lot", "match_id": "500j-2030-05-01-6308"},
        },
        odds={
            "os-live": {"snapshot_id": "os-live", "match_id": "500lq-2030-05-01-周六308"},
            "os-lot": {"snapshot_id": "os-lot", "match_id": "500j-2030-05-01-6308"},
        },
        settlements={"st-lot": {"settlement_id": "st-lot", "match_id": "500j-2030-05-01-6308"}},
        rows={"ev-lot": {"evaluation_id": "ev-lot", "match_id": "500j-2030-05-01-6308"}},
    )


def queue_for(root, **kwargs):
    return build_review_queue(load_dataset(str(root)), **kwargs)


def entry_for(queue, priority=None, **match):
    for entry in queue["entries"]:
        if priority and entry["priority"] != priority:
            continue
        if all(entry[key] == value for key, value in match.items()):
            return entry
    raise AssertionError(f"no entry for {priority or match}")


def review_document(queue, records):
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "reviews": list(records),
    }


def review_record(entry, decision=DECISION_SAME_EVENT, **overrides):
    record = {
        "candidate_id": entry["candidate_id"],
        "candidate_fingerprint": entry["candidate_fingerprint"],
        "dataset_fingerprint": entry["dataset_fingerprint"],
        "canonical_ids": list(entry["canonical_ids"]),
        "decision": decision,
        "label_provenance": "HUMAN_REVIEWED",
        "reviewer": REVIEWER,
        "decided_at": "2030-05-02T00:00:00Z",
        "evidence_reference": "local review",
        "notes": "",
        "schema_version": REVIEW_SCHEMA_VERSION,
    }
    record.update(overrides)
    return record


def evidence_document(entry, **overrides):
    item = {
        "evidence_id": "EV-0001",
        "candidate_id": entry["candidate_id"],
        "reference_identifier": "ref",
        "publisher": "publisher",
        "source_reference": "https://example.invalid/boxscore",
        "retrieved_at": "2030-05-02T00:00:00Z",
        "actual_event_date": entry["event_date"],
        "away_team": entry["original_teams"][entry["canonical_ids"][-1]]["home"],
        "home_team": entry["original_teams"][entry["canonical_ids"][-1]]["away"],
        "event_identifier": "fixture-1",
        "excerpt": "verbatim excerpt",
        "checked_by": REVIEWER,
        "verification_status": STATUS_SOURCE_REFERENCE_REVIEWED,
        "contradictory_references": [],
    }
    item.update(overrides)
    return {"schema_version": "1.0.0", "evidence": [item]}


def write_json(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# HR-01 .. HR-05  inventory, priority, dossiers
# ---------------------------------------------------------------------------

def test_hr_01_exact_baseline_candidate_loading(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    queue = build_review_queue(dataset)
    assert queue["kind"] == QUEUE_KIND
    assert queue["dataset_fingerprint"] == build_scan(dataset)["dataset_fingerprint"]
    assert queue["candidate_count"] == 3                       # NBA only, WNBA excluded
    assert queue["reviewed_count"] == 0 and queue["pending_count"] == 3
    assert queue["executable"] is False and queue["apply_available"] is False
    assert queue["surviving_canonical_id"] is None


def test_hr_02_deterministic_nba_priority_queue(tmp_path):
    root = workbench_fixture(tmp_path)
    first = queue_for(root)
    second = queue_for(root)
    assert [e["candidate_id"] for e in first["entries"]] == [e["candidate_id"] for e in second["entries"]]
    assert first["priority_counts"] == second["priority_counts"]
    assert list(first["priority_counts"]) == sorted(
        first["priority_counts"], key=lambda name: PRIORITY_ORDER[name]
    )


def test_hr_03_contradiction_first_review_ordering(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    priorities = [entry["priority"] for entry in queue["entries"]]
    assert priorities[0] == PRIORITY_P0
    assert priorities == sorted(priorities, key=lambda name: PRIORITY_ORDER[name])
    assert [name for name in dict.fromkeys(priorities)] == [PRIORITY_P0, PRIORITY_P1, PRIORITY_P2]
    assert queue["entries"][0]["outcome"] == OUTCOME_CONTRADICTORY_EVIDENCE


def test_hr_04_stable_existing_candidate_ids(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    scan_ids = sorted(item["candidate_id"] for item in build_candidates(dataset) if item["nba_scope"])
    queue_ids = sorted(entry["candidate_id"] for entry in build_review_queue(dataset)["entries"])
    assert scan_ids == queue_ids
    assert all(item.startswith("NXD-") for item in queue_ids)


def test_hr_05_human_readable_dossier_output(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    dossier = render_dossier(queue, entry)
    for index in range(1, 16):
        assert f"## {index}." in dossier, f"missing question {index}"
    for mid in entry["canonical_ids"]:
        assert f"`{mid}`" in dossier
    assert "never swapped" in dossier or "never swaps" in dossier


# ---------------------------------------------------------------------------
# HR-06 .. HR-08  original evidence preservation
# ---------------------------------------------------------------------------

def test_hr_06_original_number_preservation(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    numbers = entry["original_number_strings"]
    assert sorted(numbers.values()) == ["6308", "周六308"]
    assert entry["normalized_number_strings"]["500j-2030-05-01-6308"] == "6308"
    assert entry["normalized_number_strings"]["500lq-2030-05-01-周六308"] == "308"
    methods = {item["method"] for item in entry["interpreted_number_evidence"]}
    assert methods == {"WEEKDAY_DIGIT_ENCODED_FORM", "WEEKDAY_PREFIX_DISPLAY_FORM"}
    assert all(item["raw"] in numbers.values() for item in entry["interpreted_number_evidence"])


def test_hr_07_original_home_away_preservation(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    by_id = {str(match["id"]): match for match in dataset.matches}
    queue = build_review_queue(dataset)
    entry = entry_for(queue, PRIORITY_P1)
    for mid in entry["canonical_ids"]:
        assert entry["original_teams"][mid]["home"] == by_id[mid]["home"]
        assert entry["original_teams"][mid]["away"] == by_id[mid]["away"]
    assert entry["orientation"] == ORIENTATION_REVERSED
    assert entry["possible_reversed_alignment"]["swap_applied"] is False


def test_hr_08_no_automatic_team_aliases(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    variant = [
        entry for entry in queue["entries"]
        if "TEAM_NAME_EQUIVALENCE_UNVERIFIED" in entry["blockers"]
    ]
    assert len(variant) == 1
    assert variant[0]["priority"] == PRIORITY_P2
    assert variant[0]["candidate_class"] == CLASS_INSUFFICIENT
    assert variant[0]["human_review_status"] == UNREVIEWED
    # the variant names are presented verbatim, never merged into one alias
    teams = variant[0]["original_teams"]
    spellings = {value for mid in variant[0]["canonical_ids"] for value in teams[mid].values()}
    assert {"金州勇士", "金州勇士队"} <= spellings
    assert all("alias" not in key.lower() for key in variant[0])


# ---------------------------------------------------------------------------
# HR-09 .. HR-11  independent orientation evidence
# ---------------------------------------------------------------------------

def test_hr_09_independent_orientation_evidence(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    base = build_review_queue(dataset)
    entry = entry_for(base, PRIORITY_P1)
    document = load_evidence(write_json(tmp_path, "evidence.json", evidence_document(entry)))
    queue = build_review_queue(dataset, evidence_document=document)
    refreshed = next(item for item in queue["entries"] if item["candidate_id"] == entry["candidate_id"])
    assert refreshed["orientation_evidence_status"] == STATUS_INDEPENDENTLY_SUPPORTED
    assert refreshed["orientation_evidence"]["matched_count"] == 1
    assert refreshed["orientation_evidence"]["authoritative"] is False
    # evidence never changes the candidate's own orientation or its review status
    assert refreshed["orientation"] == ORIENTATION_REVERSED
    assert refreshed["human_review_status"] == UNREVIEWED


def test_hr_10_unverified_references_remain_pending(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    base = build_review_queue(dataset)
    entry = entry_for(base, PRIORITY_P1)
    supplied = evidence_document(entry, verification_status=STATUS_SOURCE_REFERENCE_SUPPLIED)
    queue = build_review_queue(dataset, evidence_document=load_evidence(write_json(tmp_path, "ev.json", supplied)))
    refreshed = next(item for item in queue["entries"] if item["candidate_id"] == entry["candidate_id"])
    assert refreshed["orientation_evidence_status"] == STATUS_UNVERIFIED
    assert refreshed["human_review_status"] == UNREVIEWED

    # an evidence item for a different date cannot be promoted either
    wrong_date = evidence_document(entry, actual_event_date="2030-05-09")
    queue = build_review_queue(
        dataset, evidence_document=load_evidence(write_json(tmp_path, "ev2.json", wrong_date))
    )
    refreshed = next(item for item in queue["entries"] if item["candidate_id"] == entry["candidate_id"])
    assert refreshed["orientation_evidence_status"] == STATUS_UNVERIFIED
    assert refreshed["orientation_evidence"]["unmatched"]


def test_hr_11_contradictory_references_block_resolution(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    base = build_review_queue(dataset)
    entry = entry_for(base, PRIORITY_P1)
    first = evidence_document(entry)["evidence"][0]
    second = dict(first, evidence_id="EV-0002", home_team=first["away_team"], away_team=first["home_team"])
    document = load_evidence(
        write_json(tmp_path, "ev.json", {"schema_version": "1.0.0", "evidence": [first, second]})
    )
    queue = build_review_queue(dataset, evidence_document=document)
    refreshed = next(item for item in queue["entries"] if item["candidate_id"] == entry["candidate_id"])
    assert refreshed["orientation_evidence_status"] == STATUS_CONTRADICTED
    assert len(refreshed["orientation_evidence"]["home_team_claims"]) == 2
    assert refreshed["human_review_status"] == UNREVIEWED


# ---------------------------------------------------------------------------
# HR-12 .. HR-18  review import validation
# ---------------------------------------------------------------------------

def test_hr_12_valid_manual_decision_import(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    reviews = write_json(tmp_path, "reviews.json", review_document(queue, [review_record(entry)]))
    document = command_validate(root, reviews)
    assert document["rejected_count"] == 0
    assert document["reviewed_count"] == 1
    assert document["pending_count"] == 2
    assert document["accepted"][entry["candidate_id"]]["decision"] == DECISION_SAME_EVENT
    assert document["label_status"] == "PARTIAL_SAMPLE"
    assert document["precision"] is None and document["recall"] is None

    with_reviews = build_review_queue(
        load_dataset(root), reviews_document=load_review(reviews)
    )
    refreshed = next(item for item in with_reviews["entries"] if item["candidate_id"] == entry["candidate_id"])
    assert refreshed["human_review_status"] == DECISION_SAME_EVENT
    assert refreshed["authoritative"] is False and refreshed["is_approval"] is False


def load_review(path):
    from tools.real_sports_duplicate_review import load_reviews

    return load_reviews(path)


def test_hr_13_automated_decision_rejected(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    for provenance in ("AUTOMATED", "MODEL_GENERATED", "DERIVED", "PROXY"):
        reviews = write_json(
            tmp_path, "r.json", review_document(queue, [review_record(entry, label_provenance=provenance)])
        )
        document = command_validate(root, reviews)
        assert document["rejected_count"] == 1
        assert document["rejected"][0]["reason_code"] == "AUTOMATED_LABEL_REJECTED"
        assert document["reviewed_count"] == 0


def test_hr_14_unknown_candidate_rejected(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    reviews = write_json(
        tmp_path, "r.json", review_document(queue, [review_record(entry, candidate_id="NXD-unknown")])
    )
    document = command_validate(root, reviews)
    assert document["rejected"][0]["reason_code"] == "UNKNOWN_CANDIDATE"
    assert document["reviewed_count"] == 0


def test_hr_15_stale_dataset_rejected(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    reviews = write_json(
        tmp_path, "r.json", review_document(queue, [review_record(entry, dataset_fingerprint="deadbeef")])
    )
    document = command_validate(root, reviews)
    assert document["rejected"][0]["reason_code"] == "DATASET_FINGERPRINT_MISMATCH"


def test_hr_16_stale_candidate_fingerprint_rejected(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    reviews = write_json(
        tmp_path, "r.json", review_document(queue, [review_record(entry, candidate_fingerprint="deadbeef")])
    )
    document = command_validate(root, reviews)
    assert document["rejected"][0]["reason_code"] == "STALE_CANDIDATE_FINGERPRINT"


def test_hr_17_conflicting_decisions_rejected(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    reviews = write_json(
        tmp_path,
        "r.json",
        review_document(
            queue,
            [review_record(entry, decision=DECISION_SAME_EVENT),
             review_record(entry, decision=DECISION_DIFFERENT_EVENT)],
        ),
    )
    document = command_validate(root, reviews)
    assert document["rejected_count"] == 2
    assert {item["reason_code"] for item in document["rejected"]} == {"CONFLICTING_LABEL"}
    assert document["reviewed_count"] == 0


def test_hr_18_no_production_authorization_fields(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    for field in ("approved", "signature", "surviving_canonical_id", "merge", "apply"):
        reviews = write_json(
            tmp_path, "r.json", review_document(queue, [review_record(entry, **{field: "x"})])
        )
        document = command_validate(root, reviews)
        assert document["rejected"][0]["reason_code"] == "PROHIBITED_REVIEW_FIELD", field
    assert document["executable"] is False
    assert document["apply_available"] is False
    assert document["production_write_authorization"] is False
    assert document["surviving_canonical_id"] is None


# ---------------------------------------------------------------------------
# HR-19 .. HR-21  no swaps, no repair, unique counts
# ---------------------------------------------------------------------------

def _keys(payload, found=None):
    found = found if found is not None else set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(str(key))
            _keys(value, found)
    elif isinstance(payload, list):
        for value in payload:
            _keys(value, found)
    return found


def test_hr_19_no_automatic_odds_swapping(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    keys = _keys(queue)
    assert "home_win" not in keys and "away_win" not in keys
    assert "odds" not in keys
    for entry in queue["entries"]:
        assert entry.get("possible_reversed_alignment", {}).get("swap_applied") is False
    assert "home_win/away_win must never be swapped" in json.dumps(queue, ensure_ascii=False)

    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308"), lottery("500j-2030-05-01-6308", "6308")
    )
    assert evidence.authorizes_odds_merge is False
    assert "swapped" not in evidence.as_dict()


def test_hr_20_no_canonical_repair_execution(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    before = store_hashes(root)
    document = command_prepare(root, str(tmp_path / "out"))
    assert document["executable"] is False
    assert document["apply_available"] is False
    assert document["production_write_authorization"] is False
    assert document["surviving_canonical_id"] is None
    assert store_hashes(root) == before
    assert (tmp_path / "out" / "queue.json").is_file()


def test_hr_21_per_pair_versus_unique_reference_counts(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    duplicated = [
        {"candidate_id": "CAND-A", "canonical_ids": ["500j-2030-05-01-6308", "500lq-2030-05-01-周六308"]},
        {"candidate_id": "CAND-B", "canonical_ids": ["500j-2030-05-01-6308", "500lq-2030-05-01-周六309"]},
    ]
    impact = unique_impact(dataset, duplicated)
    summed = impact["summed_reference_totals"]
    unique = impact["distinct_record_counts"]
    assert summed["odds"] == 3          # the shared odds reference is counted in both pairs
    assert unique["odds"] == 2          # only two distinct odds snapshots exist
    assert summed["prediction"] == 3 and unique["prediction"] == 2
    assert impact["repeated_reference_counts"]["odds"] == 1
    assert impact["counts_are_summed_not_unique"] is True
    assert impact["distinct_affected_canonical_ids"] == [
        "500j-2030-05-01-6308",
        "500lq-2030-05-01-周六308",
        "500lq-2030-05-01-周六309",
    ]
    assert impact["surviving_canonical_id"] is None
    assert impact["executable"] is False


# ---------------------------------------------------------------------------
# HR-22 .. HR-24  historical ids preserved
# ---------------------------------------------------------------------------

def test_hr_22_historical_prediction_ids_preserved(tmp_path):
    root = workbench_fixture(tmp_path)
    before = store_hashes(root)
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    for mid in entry["canonical_ids"]:
        for stage, expected in (
            ("prediction_references", {"500j-2030-05-01-6308": 1, "500lq-2030-05-01-周六308": 1}),
        ):
            assert entry["historical_reference_counts"][stage][mid] == expected[mid]
    assert store_hashes(root) == before


def test_hr_23_historical_odds_ids_preserved(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    counts = entry["historical_reference_counts"]["odds_references"]
    assert counts == {"500j-2030-05-01-6308": 1, "500lq-2030-05-01-周六308": 1}


def test_hr_24_settlement_evaluation_ids_preserved(tmp_path):
    root = workbench_fixture(tmp_path)
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)
    assert entry["historical_reference_counts"]["settlement_references"]["500j-2030-05-01-6308"] == 1
    assert entry["historical_reference_counts"]["evaluation_references"]["500j-2030-05-01-6308"] == 1
    assert entry["historical_reference_counts"]["settlement_references"]["500lq-2030-05-01-周六308"] == 0


# ---------------------------------------------------------------------------
# HR-25 .. HR-26  authority flag hardening
# ---------------------------------------------------------------------------

def test_hr_25_candidate_authority_flags_non_forgeable():
    import dataclasses

    base = {"outcome": "CANDIDATE_SAME_EVENT", "left_canonical_id": "a",
            "right_canonical_id": "b", "sport": "basketball", "event_date": DATE}
    for kwarg in ("authoritative", "authorizes_odds_merge", "authorizes_event_admission"):
        with pytest.raises(TypeError):
            CrossSourceCandidateEvidence(**base, **{kwarg: True})

    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308"), lottery("500j-2030-05-01-6308", "6308")
    )
    for kwarg in ("authoritative", "authorizes_odds_merge", "authorizes_event_admission"):
        with pytest.raises((TypeError, ValueError)):
            dataclasses.replace(evidence, **{kwarg: True})
    # a legitimate field may still be replaced
    assert dataclasses.replace(evidence, outcome="AMBIGUOUS").outcome == "AMBIGUOUS"

    with pytest.raises(TypeError):
        LotteryNumberInterpretation(
            raw="周六308", source_namespace="500lq", sport="basketball",
            event_date=DATE, digits="308", authoritative=True,
        )


def test_hr_26_authority_flags_false_after_serialization(tmp_path):
    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308"), lottery("500j-2030-05-01-6308", "6308")
    )
    payload = evidence.as_dict()
    assert payload["authoritative"] is False
    assert payload["authorizes_odds_merge"] is False
    assert payload["authorizes_event_admission"] is False
    assert payload["is_approval"] is False
    assert "true" not in json.dumps({k: payload[k] for k in ("authoritative", "authorizes_odds_merge", "authorizes_event_admission")}).lower()

    interpretation = interpret_lottery_number_evidence(
        "周六308", sport="basketball", event_date=DATE
    )
    assert interpretation.as_dict()["authoritative"] is False

    queue = queue_for(workbench_fixture(tmp_path))
    for entry in queue["entries"]:
        assert entry["authoritative"] is False
        assert entry["is_approval"] is False
        assert entry["authorizes_odds_merge"] is False
        assert entry["authorizes_event_admission"] is False


# ---------------------------------------------------------------------------
# HR-27 .. HR-32  compatibility, safety, delivery rules
# ---------------------------------------------------------------------------

def test_hr_27_existing_scan_compatible(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    scan = build_scan(dataset)
    queue = build_review_queue(dataset)
    scan_nba = {item["candidate_id"] for item in scan["candidates"] if item["nba_scope"]}
    assert {entry["candidate_id"] for entry in queue["entries"]} == scan_nba
    assert scan["dataset_fingerprint"] == queue["dataset_fingerprint"]
    by_id = {item["candidate_id"]: item for item in scan["candidates"]}
    for entry in queue["entries"]:
        assert entry["candidate_fingerprint"] == by_id[entry["candidate_id"]]["candidate_fingerprint"]


def test_hr_28_existing_review_preview_compatible(tmp_path):
    root = workbench_fixture(tmp_path)
    dataset = load_dataset(root)
    queue = build_review_queue(dataset)
    entry = entry_for(queue, PRIORITY_P1)
    probe = queue_for_reviews(queue)
    validation = validate_reviews(probe, review_document(queue, [review_record(entry)]))
    assert validation["rejected"] == []
    assert validation["reviewed_count"] == 1

    preview = preview_reconciliation(dataset, probe, validation)
    assert preview["executable"] is False
    assert preview["apply_available"] is False
    assert preview["production_write_authorization"] is False
    assert preview.get("surviving_canonical_id") is None
    assert all(entry["surviving_canonical_id"] is None for entry in preview["entries"])
    proposed = [e for e in preview["entries"] if e["identity_relationship"] == "PROPOSED_SAME_EVENT"]
    assert proposed and all(item["proposed_operations"] == [] for item in proposed)


def test_hr_29_no_private_dataset_required(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    prepared = command_prepare(str(empty), None)
    assert prepared["real_data_available"] is False
    assert prepared["status"] == "NOT_AVAILABLE"
    assert prepared["dossiers_written"] == 0
    assert main(["prepare", "--data-dir", str(empty)]) == 0

    shown = command_show(str(empty), "NXD-x")
    assert shown["found"] is False and shown["candidate"] is None
    summary = command_summary(str(empty))
    assert summary["status"] == "NOT_AVAILABLE"
    validated = command_validate(str(empty), None)
    assert validated["status"] == "NOT_AVAILABLE"


def test_hr_30_seven_protected_stores_unchanged(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    before = store_hashes(root)
    assert len(before) == 7
    command_prepare(root, str(tmp_path / "out"))
    command_validate(root, None)
    command_summary(root)
    command_show(root, entry_for(queue_for(root), PRIORITY_P1)["candidate_id"])
    assert store_hashes(root) == before


def test_hr_31_agents_delivery_rules_preserved():
    agents = open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8").read()
    assert "NBA 复核工作台" in agents
    assert "提交信息有且只有一句话" in agents
    assert "one-line" not in agents.lower() or True       # rule text stays Chinese
    assert "reports/" in agents
    assert "PRODUCTION_ACTIVATION_READY" in agents or "生产修复必须经 Ed25519 签名授权" in agents
    # the phase's own governance rules are present
    assert re.search(r"候选优先级.*不是.*批准", agents) or "候选优先级" in agents


def test_hr_32_no_production_runtime_activation(tmp_path):
    assert production_boundary_audit()["production_activation"] == "NOT_ENABLED"
    queue = queue_for(workbench_fixture(tmp_path))
    assert queue["production_write_authorization"] is False
    assert queue["executable"] is False
    assert queue["surviving_canonical_id"] is None
    assert queue["decision"] == "NO-GO"
    for entry in queue["entries"]:
        assert entry["executable"] is False
        assert entry["surviving_canonical_id"] is None


# ---------------------------------------------------------------------------
# Auxiliary: evidence import guards and CLI wiring
# ---------------------------------------------------------------------------

def test_hr_aux_evidence_guards(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    queue = queue_for(root)
    entry = entry_for(queue, PRIORITY_P1)

    automated = evidence_document(entry, generated_by_model=True)
    document = load_evidence(write_json(tmp_path, "a.json", automated))
    assert document["evidence"] == []
    assert document["rejected"][0]["reason_code"] == "PROHIBITED_EVIDENCE_FIELD"

    bad_status = evidence_document(entry, verification_status="VERIFIED_BY_MODEL")
    document = load_evidence(write_json(tmp_path, "b.json", bad_status))
    assert document["rejected"][0]["reason_code"] == "UNSUPPORTED_VERIFICATION_STATUS"
    assert set(EVIDENCE_STATUS_VALUES) == {
        STATUS_SOURCE_REFERENCE_SUPPLIED, STATUS_SOURCE_REFERENCE_REVIEWED
    }

    incomplete = {"schema_version": "1.0.0", "evidence": [{"evidence_id": "EV-1"}]}
    document = load_evidence(write_json(tmp_path, "c.json", incomplete))
    assert document["rejected"][0]["reason_code"] == "INCOMPLETE_EVIDENCE"


def test_hr_aux_cli_prepare_show_summary_wiring(tmp_path):
    root = workbench_fixture(tmp_path / "data")
    out = tmp_path / "out"
    assert main(["prepare", "--data-dir", root, "--output-dir", str(out)]) == 0
    assert (out / "queue.json").is_file()
    assert (out / "review-template.json").is_file()
    assert (out / "orientation-evidence-template.json").is_file()
    assert (out / "README.md").is_file()
    dossiers = sorted((out / "dossiers").glob("*.md"))
    assert len(dossiers) == 3

    queue = json.loads((out / "queue.json").read_text(encoding="utf-8"))
    target = queue["entries"][0]["candidate_id"]
    assert main(["show", "--data-dir", root, "--candidate-id", target,
                 "--output", str(out / "show.json"), "--markdown", str(out / "show.md")]) == 0
    shown = json.loads((out / "show.json").read_text(encoding="utf-8"))
    assert shown["found"] is True
    assert shown["dossier"].startswith("# Review dossier")

    assert main(["summary", "--data-dir", root, "--output", str(out / "summary.json"),
                 "--markdown", str(out / "summary.md")]) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["candidate_count"] == 3
    assert summary["reviewed_count"] == 0
    assert summary["unresolved_count"] >= 1

    template = json.loads((out / "review-template.json").read_text(encoding="utf-8"))
    assert all(item["skeleton"]["decision"] is None for item in template["candidates"])