"""
tests/test_real_sports_duplicate_review.py
==========================================
Focused tests for ``tools/real_sports_duplicate_review.py``.

All fixtures are synthetic and live in temporary directories. They verify code
behaviour only: determinism, schema enforcement, fail-closed validation and the
read-only nature of the preview. No human labels are fabricated anywhere, and no
business metric is derived from these fixtures.

Reviewer identities used below are explicit test values inside a temporary file;
they never touch the real dataset and never claim organizational approval.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import shutil

import pytest

from tools.real_sports_duplicate_review import (
    CLASS_AMBIGUOUS,
    CLASS_CONTROL,
    CLASS_HIGH,
    CLASS_INSUFFICIENT,
    DECISION_DIFFERENT_EVENT,
    DECISION_SAME_EVENT,
    EXIT_OK,
    EXIT_PROTECTED_PATH,
    EXIT_REVIEW_REJECTED,
    GENERATE_KIND,
    PREVIEW_KIND,
    REVIEW_SCHEMA_VERSION,
    UNREVIEWED,
    ProtectedOutputError,
    candidate_fingerprint,
    command_generate,
    command_preview,
    command_validate,
    dataset_fingerprint,
    generate_candidates,
    load_dataset,
    main,
    preview_reconciliation,
    validate_output_path,
    validate_reviews,
)
from tools.real_sports_identity_audit import verify_protected_stores
from utils.ingestion_identity import CanonicalIdentityCollisionError, plan_ingestion_identity
from utils.source_identity import detect_strong_number_conflict, observe_source_row

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTECTED_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)
REVIEWER = "test-reviewer"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def match(match_id, **overrides):
    record = {
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
    record.update(overrides)
    return record


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


def high_evidence_fixture(root):
    """Two Canonical IDs for one real event, plus one genuinely different event."""
    return write_dataset(
        root,
        [
            match("500l-2030-05-01-2001", jczq_no="2001", status="finished",
                  score={"ft": [2, 1]}),
            match("500l-2030-05-01-1398620", home="主队A", away="客队B"),
            match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
        ],
        predictions={
            "ps-a": {"snapshot_id": "ps-a", "match_id": "500l-2030-05-01-2001"},
            "ps-b": {"snapshot_id": "ps-b", "match_id": "500l-2030-05-01-1398620"},
        },
    )


def review_record(candidate, decision=DECISION_SAME_EVENT, **overrides):
    record = {
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "dataset_fingerprint": candidate["dataset_fingerprint"],
        "canonical_ids": list(candidate["canonical_ids"]),
        "decision": decision,
        "label_provenance": "HUMAN_REVIEWED",
        "reviewer": REVIEWER,
        "decided_at": "2030-06-01T12:00:00+08:00",
        "evidence_reference": "manual schedule cross-check",
        "notes": "fixture decision",
        "schema_version": REVIEW_SCHEMA_VERSION,
    }
    record.update(overrides)
    return record


def review_document(queue, records):
    return {
        "kind": "READ_ONLY_DUPLICATE_REVIEW_DECISIONS",
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "reviews": list(records),
    }


def queue_for(root, **kwargs):
    return generate_candidates(load_dataset(str(root)), **kwargs)


def candidate_of_class(queue, candidate_class):
    for candidate in queue["candidates"]:
        if candidate["candidate_class"] == candidate_class:
            return candidate
    raise AssertionError(f"no candidate of class {candidate_class}")


# ---------------------------------------------------------------------------
# DR-01 / DR-02 / DR-03 / DR-04 / DR-05: candidate generation
# ---------------------------------------------------------------------------

def test_dr_01_candidate_generation_is_deterministic(tmp_path):
    root = high_evidence_fixture(tmp_path)
    first = queue_for(root)
    second = queue_for(root)
    assert first["kind"] == GENERATE_KIND
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["dataset_fingerprint"] == second["dataset_fingerprint"]
    assert first["counts"]["total"] >= 1


def test_dr_02_high_evidence_duplicate_candidate(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    assert candidate["canonical_ids"] == ["500l-2030-05-01-1398620", "500l-2030-05-01-2001"]
    assert candidate["field_evidence_label"] == "SAME_EVENT"
    assert candidate["evidence_quality"] == "HIGH_FIELD_EVIDENCE"
    assert candidate["reviewer_status"] == UNREVIEWED
    assert candidate["review_provenance"] == "NONE"
    assert candidate["prediction_references"]["500l-2030-05-01-2001"]["count"] == 1


def test_dr_03_truncated_name_ambiguity(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-2003", jczq_no="2003", home="哈萨克"),
            match("500l-2030-05-01-1398660", home="哈萨克斯坦"),
        ],
    )
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_AMBIGUOUS)
    assert candidate["field_evidence_label"] == "AMBIGUOUS"
    assert candidate["evidence_quality"] == "AMBIGUOUS_FIELD_EVIDENCE"
    assert any("ABBREVIATION" in item for item in candidate["contradictory_evidence"])
    assert candidate["candidate_class"] != CLASS_HIGH


def test_dr_04_missing_number_candidate(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-1398620"),
            match("500l-2030-05-02-2001", jczq_no="2001", date="2030-05-02"),
        ],
    )
    queue = queue_for(root)
    insufficient = [c for c in queue["candidates"] if c["candidate_class"] == CLASS_INSUFFICIENT]
    assert insufficient, "the record without a competition number must be reviewable"
    single = [c for c in insufficient if c["pair_shape"] == "SINGLE_RECORD"]
    assert single, "an unassociated insufficient record must be surfaced on its own"
    candidate = single[0]
    assert candidate["canonical_ids"] == ["500l-2030-05-01-1398620"]
    assert candidate["classification_basis"]["identity_kind"] == "INSUFFICIENT"
    assert candidate["source_identifiers"]["500l-2030-05-01-1398620"][
        "competition_number_normalized"
    ] is None
    assert candidate["evidence_quality"] != "HIGH_FIELD_EVIDENCE"


def test_dr_04b_insufficient_identity_inside_a_pair_is_not_lost(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-2001", jczq_no="2001"),
            match("500l-2030-05-01-1398620"),
        ],
    )
    queue = queue_for(root)
    pair = candidate_of_class(queue, CLASS_HIGH)
    assert CLASS_INSUFFICIENT in pair["secondary_classes"]


def test_dr_05_different_event_negative_control(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-2001", jczq_no="2001"),
            match("500l-2030-05-01-2002", jczq_no="2002", home="主队C", away="客队D"),
            match("500l-2030-05-02-2003", jczq_no="2003", home="主队A", away="客队B"),
        ],
    )
    queue = queue_for(root)
    controls = [c for c in queue["candidates"] if c["candidate_class"] == CLASS_CONTROL]
    assert controls, "negative controls must be generated"
    for control in controls:
        assert control["requires_human_review"] is True
        assert control["reviewer_status"] == UNREVIEWED
        assert control["contradictory_evidence"]


# ---------------------------------------------------------------------------
# DR-06 / DR-07: deduplication and stable fingerprints
# ---------------------------------------------------------------------------

def test_dr_06_candidate_pairs_are_deduplicated(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    keys = [tuple(sorted(candidate["canonical_ids"])) for candidate in queue["candidates"]]
    assert len(keys) == len(set(keys))
    ids = [candidate["candidate_id"] for candidate in queue["candidates"]]
    assert len(ids) == len(set(ids))


def test_dr_07_candidate_fingerprint_is_stable_and_identity_bound(tmp_path):
    root = high_evidence_fixture(tmp_path)
    first = candidate_of_class(queue_for(root), CLASS_HIGH)
    second = candidate_of_class(queue_for(root), CLASS_HIGH)
    assert first["candidate_id"] == second["candidate_id"]
    assert first["candidate_fingerprint"] == second["candidate_fingerprint"]
    assert first["candidate_fingerprint"] == candidate_fingerprint(first)

    tampered = dict(first)
    tampered["sport"] = "basketball"
    assert candidate_fingerprint(tampered) != first["candidate_fingerprint"]


# ---------------------------------------------------------------------------
# DR-08 / DR-09: unreviewed vs accepted human decisions
# ---------------------------------------------------------------------------

def test_dr_08_unreviewed_candidate_stays_pending(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    validation = validate_reviews(queue, review_document(queue, []))
    assert validation["reviewed_count"] == 0
    assert validation["pending_count"] == queue["counts"]["total"]
    assert validation["label_status"] == "NOT_COMPUTABLE_NO_HUMAN_DECISIONS"
    assert validation["rejected_count"] == 0


def test_dr_09_valid_human_decision_can_be_imported(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    validation = validate_reviews(queue, review_document(queue, [review_record(candidate)]))
    assert validation["rejected_count"] == 0
    assert validation["reviewed_count"] == 1
    assert validation["accepted"][candidate["candidate_id"]]["decision"] == DECISION_SAME_EVENT
    assert validation["accepted"][candidate["candidate_id"]]["reviewer"] == REVIEWER
    assert validation["precision"] is None and validation["recall"] is None


# ---------------------------------------------------------------------------
# DR-10 / DR-11 / DR-12 / DR-13 / DR-14 / DR-15: fail-closed validation
# ---------------------------------------------------------------------------

def test_dr_10_unknown_candidate_rejects(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    record = review_record(candidate, candidate_id="CAND-does-not-exist")
    validation = validate_reviews(queue, review_document(queue, [record]))
    assert [item["reason_code"] for item in validation["rejected"]] == ["UNKNOWN_CANDIDATE"]


def test_dr_11_wrong_dataset_fingerprint_rejects(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    document = review_document(queue, [review_record(candidate)])
    document["dataset_fingerprint"] = "0" * 64
    validation = validate_reviews(queue, document)
    codes = {item["reason_code"] for item in validation["rejected"]}
    assert "DATASET_FINGERPRINT_MISMATCH" in codes


def test_dr_12_stale_candidate_fingerprint_rejects(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    record = review_record(candidate, candidate_fingerprint="f" * 64)
    validation = validate_reviews(queue, review_document(queue, [record]))
    assert [item["reason_code"] for item in validation["rejected"]] == ["STALE_CANDIDATE_FINGERPRINT"]


def test_dr_13_conflicting_labels_reject(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    records = [
        review_record(candidate, DECISION_SAME_EVENT),
        review_record(candidate, DECISION_DIFFERENT_EVENT, decided_at="2030-06-02T12:00:00+08:00"),
    ]
    validation = validate_reviews(queue, review_document(queue, records))
    assert validation["reviewed_count"] == 0
    assert {item["reason_code"] for item in validation["rejected"]} == {"CONFLICTING_LABEL"}


def test_dr_14_unsupported_label_rejects(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    validation = validate_reviews(
        queue, review_document(queue, [review_record(candidate, decision="MERGE_NOW")])
    )
    assert [item["reason_code"] for item in validation["rejected"]] == ["UNSUPPORTED_DECISION"]


def test_dr_15_missing_reviewer_evidence_rejects(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    for overrides, code in (
        ({"reviewer": ""}, "MISSING_REVIEWER"),
        ({"evidence_reference": ""}, "MISSING_EVIDENCE_REFERENCE"),
        ({"decided_at": "not-a-timestamp"}, "INVALID_TIMESTAMP"),
        ({"notes": 7}, "INVALID_NOTES"),
    ):
        validation = validate_reviews(queue, review_document(queue, [review_record(candidate, **overrides)]))
        assert [item["reason_code"] for item in validation["rejected"]] == [code], overrides

    incomplete = review_record(candidate)
    incomplete.pop("evidence_reference")
    validation = validate_reviews(queue, review_document(queue, [incomplete]))
    assert [item["reason_code"] for item in validation["rejected"]] == ["INCOMPLETE_REVIEW"]

    automated = review_record(candidate, label_provenance="MODEL_GENERATED")
    validation = validate_reviews(queue, review_document(queue, [automated]))
    assert [item["reason_code"] for item in validation["rejected"]] == ["AUTOMATED_LABEL_REJECTED"]


def test_dr_15b_prohibited_production_semantics_reject(tmp_path):
    root = high_evidence_fixture(tmp_path)
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    for field in ("approved", "signature", "surviving_canonical_id", "apply"):
        record = review_record(candidate, **{field: "yes"})
        validation = validate_reviews(queue, review_document(queue, [record]))
        assert [item["reason_code"] for item in validation["rejected"]] == ["PROHIBITED_REVIEW_FIELD"], field


# ---------------------------------------------------------------------------
# DR-16 / DR-17: no self-approval, no elevated identity
# ---------------------------------------------------------------------------

def test_dr_16_ambiguous_candidate_cannot_auto_approve_itself(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-2003", jczq_no="2003", home="哈萨克"),
            match("500l-2030-05-01-1398660", home="哈萨克斯坦"),
        ],
    )
    queue = queue_for(root)
    candidate = candidate_of_class(queue, CLASS_AMBIGUOUS)
    assert candidate["reviewer_status"] == UNREVIEWED
    assert candidate["review_provenance"] == "NONE"
    validation = validate_reviews(queue, review_document(queue, []))
    assert candidate["candidate_id"] in validation["pending"]
    # Without a human record the candidate can never appear as decided.
    assert candidate["candidate_id"] not in validation["accepted"]


def test_dr_17_insufficient_identity_cannot_become_authorised_binding(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-1398620"),
            match("500l-2030-05-02-2001", jczq_no="2001", date="2030-05-02"),
        ],
    )
    queue = queue_for(root)
    insufficient = [c for c in queue["candidates"] if c["candidate_class"] == CLASS_INSUFFICIENT]
    assert insufficient
    for candidate in insufficient:
        if candidate["pair_shape"] == "SINGLE_RECORD":
            assert candidate["classification_basis"]["identity_kind"] == "INSUFFICIENT"
            assert candidate["reviewer_status"] == UNREVIEWED
            for source in candidate["source_identifiers"].values():
                assert source["competition_number_normalized"] is None
    # The record is never elevated: its identity key stays insufficient, and the
    # ingestion planner still refuses an alias that claims two different events.
    rows = [
        match("500l-2030-05-01-1398620"),
        match("500l-2030-05-01-2001", jczq_no="2001"),
    ]
    with pytest.raises(CanonicalIdentityCollisionError):
        plan_ingestion_identity(rows, rows)


# ---------------------------------------------------------------------------
# DR-18 / DR-19 / DR-20 / DR-21 / DR-22 / DR-23: preview is read-only
# ---------------------------------------------------------------------------

def preview_fixture(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            match("500l-2030-05-01-2001", jczq_no="2001", status="finished", score={"ft": [2, 1]}),
            match("500l-2030-05-01-1398620"),
        ],
        predictions={
            "ps-a": {"snapshot_id": "ps-a", "match_id": "500l-2030-05-01-2001"},
            "ps-b": {"snapshot_id": "ps-b", "match_id": "500l-2030-05-01-1398620"},
        },
        odds={
            "os-a": {"snapshot_id": "os-a", "match_id": "500l-2030-05-01-2001"},
            "os-b": {"snapshot_id": "os-b", "match_id": "500l-2030-05-01-1398620"},
        },
        settlements={
            "st-a": {"settlement_id": "st-a", "snapshot_id": "ps-a", "match_id": "500l-2030-05-01-2001"},
        },
        rows={
            "er-a": {"evaluation_id": "er-a", "snapshot_id": "ps-a", "settlement_id": "st-a",
                     "match_id": "500l-2030-05-01-2001"},
        },
    )
    return root


def preview_for(root):
    dataset = load_dataset(str(root))
    queue = generate_candidates(dataset)
    candidate = candidate_of_class(queue, CLASS_HIGH)
    validation = validate_reviews(queue, review_document(queue, [review_record(candidate)]))
    assert validation["rejected_count"] == 0
    return dataset, queue, validation, preview_reconciliation(dataset, queue, validation), candidate


def test_dr_18_canonical_preview_produces_no_write(tmp_path):
    root = preview_fixture(tmp_path)
    before = store_hashes(root)
    dataset, _queue, _validation, preview, _candidate = preview_for(root)
    assert store_hashes(root) == before
    assert verify_protected_stores(dataset) == before
    assert preview["kind"] == PREVIEW_KIND
    assert preview["executable"] is False
    assert preview["apply_available"] is False
    assert preview["production_write_authorization"] is False
    assert preview["no_mutation_performed"] is True
    assert preview["decision"] == "NO-GO"
    for entry in preview["entries"]:
        assert entry["surviving_canonical_id"] is None
        assert entry["proposed_operations"] == []


def test_dr_19_prediction_references_are_preserved(tmp_path):
    root = preview_fixture(tmp_path)
    _dataset, _queue, _validation, preview, candidate = preview_for(root)
    entry = next(e for e in preview["entries"] if e["candidate_id"] == candidate["candidate_id"])
    assert entry["identity_relationship"] == "PROPOSED_SAME_EVENT"
    refs = entry["downstream_references"]
    assert refs["500l-2030-05-01-2001"]["prediction"]["ids"] == ["ps-a"]
    assert refs["500l-2030-05-01-1398620"]["prediction"]["ids"] == ["ps-b"]
    assert entry["historical_id_retention"]["required"] is True
    assert set(entry["historical_id_retention"]["canonical_ids"]) == set(candidate["canonical_ids"])


def test_dr_20_odds_references_are_preserved(tmp_path):
    root = preview_fixture(tmp_path)
    _dataset, _queue, _validation, preview, candidate = preview_for(root)
    entry = next(e for e in preview["entries"] if e["candidate_id"] == candidate["candidate_id"])
    refs = entry["downstream_references"]
    assert refs["500l-2030-05-01-2001"]["odds"]["ids"] == ["os-a"]
    assert refs["500l-2030-05-01-1398620"]["odds"]["ids"] == ["os-b"]


def test_dr_21_settlement_references_are_preserved(tmp_path):
    root = preview_fixture(tmp_path)
    _dataset, _queue, _validation, preview, candidate = preview_for(root)
    entry = next(e for e in preview["entries"] if e["candidate_id"] == candidate["candidate_id"])
    refs = entry["downstream_references"]
    assert refs["500l-2030-05-01-2001"]["settlement"]["ids"] == ["st-a"]
    assert refs["500l-2030-05-01-1398620"]["settlement"]["count"] == 0


def test_dr_22_evaluation_references_are_preserved(tmp_path):
    root = preview_fixture(tmp_path)
    _dataset, _queue, _validation, preview, candidate = preview_for(root)
    entry = next(e for e in preview["entries"] if e["candidate_id"] == candidate["candidate_id"])
    refs = entry["downstream_references"]
    assert refs["500l-2030-05-01-2001"]["evaluation"]["ids"] == ["er-a"]


def test_dr_23_historical_canonical_ids_are_preserved(tmp_path):
    root = preview_fixture(tmp_path)
    before = load_dataset(str(root)).matches
    _dataset, _queue, _validation, preview, candidate = preview_for(root)
    after = load_dataset(str(root)).matches
    assert [m["id"] for m in before] == [m["id"] for m in after]
    entry = next(e for e in preview["entries"] if e["candidate_id"] == candidate["candidate_id"])
    assert entry["survivor_selection_reason"]
    assert "lexical" in entry["survivor_selection_reason"]
    assert "SIGNED_OWNER_APPROVAL_REQUIRED" in entry["blocked_reasons"]


# ---------------------------------------------------------------------------
# DR-24 / DR-25: existing guards remain fail-closed
# ---------------------------------------------------------------------------

def test_dr_24_planner_conflicts_remain_fail_closed():
    records = [
        match("500l-2030-05-01-2001", jczq_no="2001"),
        match("500l-2030-05-01-1398620"),
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(records, records)
    assert exc.value.category in {"AMBIGUOUS_CANONICAL_OWNERSHIP", "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"}


def test_dr_25_strong_number_protection_remains_intact():
    def observe(match_id, number, home="主队A", away="客队B"):
        return observe_source_row(
            {"id": match_id, "sport": "football", "league": "L", "date": "2030-05-01",
             "time": "20:00", "home": home, "away": away, "status": "upcoming",
             "jczq_no": number, "score": None, "odds": None},
            adapter="live_football",
        )

    assert detect_strong_number_conflict([
        observe("500l-2030-05-01-001", "001"),
        observe("500l-2030-05-01-002", "002", "主队C", "客队D"),
    ]) == ()
    conflicts = detect_strong_number_conflict([
        observe("500l-2030-05-01-001", "001"),
        observe("500l-2030-05-01-001", "002", "主队C", "客队D"),
    ])
    assert len(conflicts) == 1 and conflicts[0].severity == "REJECT"


# ---------------------------------------------------------------------------
# DR-26 / DR-27 / DR-28: reproducibility, honest availability, no accuracy claims
# ---------------------------------------------------------------------------

def test_dr_26_review_queue_is_reproducible_under_input_reordering(tmp_path):
    """
    Evidence and ordering are stable under record reordering.

    Candidate and dataset fingerprints intentionally track the input *bytes*, so a
    reordered file is a different dataset identity; the evidence projection below is
    what must stay identical.
    """
    root = high_evidence_fixture(tmp_path)
    dataset = load_dataset(str(root))
    forward = generate_candidates(dataset)

    reordered = list(reversed(list(dataset.matches)))
    shuffled_root = write_dataset(tmp_path / "shuffled", reordered)
    backward = generate_candidates(load_dataset(str(shuffled_root)))

    def strips(document):
        return [
            (
                c["candidate_class"],
                c["canonical_ids"],
                c["field_evidence_label"],
                c["evidence_quality"],
                c["contradictory_evidence"],
                sorted(c["secondary_classes"]),
            )
            for c in document["candidates"]
        ]

    assert forward["dataset_fingerprint"] != backward["dataset_fingerprint"]
    # Candidate IDs embed the dataset fingerprint, so ordering differs between the two
    # datasets; the evidence multiset is what must be identical.
    assert sorted(strips(forward)) == sorted(strips(backward))
    assert len(strips(forward)) == len(strips(backward))


def test_dr_27_missing_dataset_reports_input_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = main(["generate", "--data-dir", str(empty), "--quiet"])
    assert code == 2


def test_dr_28_no_precision_or_recall_without_independent_labels(tmp_path):
    root = high_evidence_fixture(tmp_path)
    dataset = load_dataset(str(root))
    queue = generate_candidates(dataset)
    validation = validate_reviews(queue, review_document(queue, []))
    assert validation["precision"] is None
    assert validation["recall"] is None
    assert "NOT_CLAIMED" in validation["accuracy_claim"]
    assert validation["label_status"] == "NOT_COMPUTABLE_NO_HUMAN_DECISIONS"


# ---------------------------------------------------------------------------
# DR-29 / DR-30: malformed input, protected paths
# ---------------------------------------------------------------------------

def test_dr_29_malformed_review_json_fails_closed(tmp_path):
    root = high_evidence_fixture(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8")
    code = main(["validate", "--data-dir", str(root), "--reviews", str(broken), "--quiet"])
    assert code == 2

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"schema_version": REVIEW_SCHEMA_VERSION}), encoding="utf-8")
    code = main(["validate", "--data-dir", str(root), "--reviews", str(wrong_shape), "--quiet"])
    assert code == 2


def test_dr_30_protected_production_paths_reject_writes(tmp_path):
    data_dir = os.path.join(ROOT, "data", "must-not-exist.json")
    with pytest.raises(ProtectedOutputError):
        validate_output_path(data_dir)
    with pytest.raises(ProtectedOutputError):
        validate_output_path(os.path.join(ROOT, "tools", "must-not-exist.json"))
    with pytest.raises(ProtectedOutputError):
        validate_output_path("")

    # The Git-ignored reports/ directory and any external path remain usable.
    assert validate_output_path(os.path.join(ROOT, "reports", "probe.json"))
    assert validate_output_path(str(tmp_path / "outside.json"))

    code = main([
        "generate", "--data-dir", str(high_evidence_fixture(tmp_path)),
        "--output", data_dir, "--quiet",
    ])
    assert code == EXIT_PROTECTED_PATH
    assert not os.path.exists(data_dir)


# ---------------------------------------------------------------------------
# DR-31 / DR-32: store integrity and no activation
# ---------------------------------------------------------------------------

def test_dr_31_seven_protected_stores_remain_byte_identical(tmp_path):
    root = high_evidence_fixture(tmp_path)
    before = store_hashes(root)
    dataset = load_dataset(root)
    command_generate(dataset, max_controls=2)
    command_validate(dataset, _write_reviews(tmp_path, generate_candidates(dataset)))
    command_preview(dataset, _write_reviews(tmp_path, generate_candidates(dataset)))
    assert store_hashes(root) == before


def _write_reviews(tmp_path, queue):
    path = os.path.join(str(tmp_path), f"reviews-{queue['dataset_fingerprint'][:8]}.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(review_document(queue, []), handle, ensure_ascii=False, sort_keys=True)
    return path


def test_dr_32_no_production_activation(tmp_path):
    from utils.runtime_mapping_policy import runtime_policy_status

    status = runtime_policy_status()
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"
    assert status["production_mapping_write_authorization"] == "NOT_GRANTED"

    root = high_evidence_fixture(tmp_path)
    queue = generate_candidates(load_dataset(root))
    assert queue["production_write_authorization"] is False
    preview = preview_reconciliation(
        load_dataset(root), queue, validate_reviews(queue, review_document(queue, []))
    )
    assert preview["production_write_authorization"] is False
    assert preview["decision"] == "NO-GO"
    text = open(os.path.join(ROOT, "tools", "real_sports_duplicate_review.py"), encoding="utf-8").read()
    for forbidden in ("import requests", "urllib", "scraper.refresh(", "socket"):
        assert forbidden not in text


# ---------------------------------------------------------------------------
# DR-33 / DR-34: exclusion behaviour in a synthetic Git repository
# ---------------------------------------------------------------------------

def test_dr_33_reports_exclusion_behaviour_in_a_synthetic_repository(tmp_path):
    """
    Behaviour test for the local exclude mechanism, independent of this checkout.

    A temporary Git repository is created, its local ``info/exclude`` is configured
    exactly as AGENTS.md requires, and the exclusion is then verified there. This runs
    identically on CI (where the developer checkout's local rule does not exist).
    """
    if shutil.which("git") is None:
        pytest.skip("git is required for the exclusion behaviour test")
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    exclude = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=str(repo), text=True
    ).strip()
    exclude_path = os.path.join(str(repo), exclude)
    with open(exclude_path, "a", encoding="ascii", newline="\n") as handle:
        handle.write("/reports/\n")
    with open(exclude_path, "a", encoding="ascii", newline="\n") as handle:
        handle.write("/reports/\n")  # idempotent second append must not duplicate at git level
    probe = subprocess.run(
        ["git", "check-ignore", "-v", "reports/_probe.md"],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert probe.returncode == 0
    assert "/reports/" in probe.stdout
    tracked = subprocess.check_output(
        ["git", "ls-files", "--", "reports/"], cwd=str(repo), text=True
    ).strip()
    assert tracked == ""


def test_dr_34_agents_md_documents_the_review_workflow():
    text = open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8").read()
    for needle in (
        "候选证据 ≠ 人工评审结论",
        "HUMAN_REVIEWED",
        "CONFLICTING_LABEL",
        "只允许预览",
        "surviving_canonical_id=null",
        "历史 `snapshot_id` / `settlement_id` / `evaluation_id` **不可变**",
        "重复评审工作流必须**真实执行**",
    ):
        assert needle in text, needle


# ---------------------------------------------------------------------------
# DR-35: CLI end-to-end on a synthetic dataset
# ---------------------------------------------------------------------------

def test_dr_35_cli_workflow_end_to_end(tmp_path):
    root = preview_fixture(tmp_path)
    queue = command_generate(load_dataset(root), max_controls=2)
    candidate = candidate_of_class(queue, CLASS_HIGH)

    reviews_path = os.path.join(str(tmp_path), "decisions.json")
    with open(reviews_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(review_document(queue, [review_record(candidate)]), handle,
                  ensure_ascii=False, sort_keys=True)

    output = os.path.join(str(tmp_path), "candidates.json")
    assert main(["generate", "--data-dir", root, "--output", output, "--quiet"]) == EXIT_OK
    assert os.path.isfile(output)

    assert main(["validate", "--data-dir", root, "--reviews", reviews_path, "--quiet"]) == EXIT_OK

    preview_path = os.path.join(str(tmp_path), "preview.json")
    assert main(["preview", "--data-dir", root, "--reviews", reviews_path,
                 "--output", preview_path, "--quiet"]) == EXIT_OK
    document = json.load(open(preview_path, encoding="utf-8"))
    assert document["kind"] == PREVIEW_KIND
    assert document["summary"]["proposed_same_event"] >= 1

    bad = os.path.join(str(tmp_path), "bad.json")
    with open(bad, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(review_document(queue, [review_record(candidate, decision="MERGE_NOW")]),
                  handle, ensure_ascii=False, sort_keys=True)
    assert main(["validate", "--data-dir", root, "--reviews", bad, "--quiet"]) == EXIT_REVIEW_REJECTED


# ---------------------------------------------------------------------------
# DR-36: real dataset (local only; CI has no private data)
# ---------------------------------------------------------------------------

REAL_CANONICAL = os.path.join(ROOT, "data", "daily_matches.json")


@pytest.mark.skipif(
    not os.path.isfile(REAL_CANONICAL),
    reason="no local real dataset (git-ignored); synthetic tests only in CI",
)
def test_dr_36_real_dataset_queue_is_read_only_and_unreviewed():
    from tools.real_sports_identity_audit import load_dataset as load_real, default_data_dir

    data_dir = default_data_dir()
    before = store_hashes(data_dir)
    dataset = load_real(data_dir)
    queue = generate_candidates(dataset)
    assert queue["counts"]["total"] > 0
    assert queue["human_labels_present"] is False
    assert queue["production_write_authorization"] is False
    assert all(c["reviewer_status"] == UNREVIEWED for c in queue["candidates"])
    assert dataset_fingerprint(dataset) == queue["dataset_fingerprint"]
    assert store_hashes(data_dir) == before
    assert verify_protected_stores(dataset) == before