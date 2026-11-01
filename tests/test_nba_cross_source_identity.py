"""
tests/test_nba_cross_source_identity.py
=======================================
Phase 2-X regression suite: NBA cross-source identity discrepancy evidence.

Scope
-----
These tests verify the read-only candidate-evidence module
(``utils.basketball_identity_evidence``) and its offline audit tool
(``tools.nba_identity_discrepancy_audit``) against **synthetic fixtures** and, where the
repository dataset is present, against the real stores read-only.

What is deliberately NOT asserted
---------------------------------
No test claims that a candidate pair *is* the same real event. Candidate classes are
diagnostics; a human decision is the only thing that can label a pair, and this suite
never fabricates one.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from tools.nba_identity_discrepancy_audit import (
    CANDIDATE_ID_PREFIX,
    SCAN_KIND,
    _candidate_fingerprint,
    build_candidates,
    build_inventory,
    build_scan,
    command_explain,
    command_preview,
    command_scan,
    main,
)
from tools.real_sports_duplicate_review import (
    DECISION_SAME_EVENT,
    dataset_fingerprint,
    load_dataset,
    validate_reviews,
)
from tools.real_sports_identity_audit import AuditInputError
from tools.shadow_trust_chain_validation import production_boundary_audit
from tools.shadow_trusted_time_validation import protected_store_hashes
from utils.basketball_identity_evidence import (
    CLASS_BOTH,
    CLASS_DATE,
    CLASS_DISTINCT,
    CLASS_INSUFFICIENT,
    CONFIDENCE_CONTRADICTED,
    CONFIDENCE_SUPPORTED,
    CONFIDENCE_UNVERIFIED,
    METHOD_ENCODED_DIGIT,
    METHOD_MALFORMED,
    METHOD_WEEKDAY_PREFIX,
    ORIENTATION_REVERSED,
    ORIENTATION_SAME,
    OUTCOME_AMBIGUOUS,
    OUTCOME_CANDIDATE_DIFFERENT_EVENT,
    OUTCOME_CANDIDATE_SAME_EVENT,
    OUTCOME_CONTRADICTORY_EVIDENCE,
    OUTCOME_INSUFFICIENT_EVIDENCE,
    RELATION_EXACT,
    classify_cross_source_candidate,
    diagnostic_class,
    event_weekday_digit,
    interpret_lottery_number_evidence as interpret,
)
from utils.market_coverage import has_usable_market_odds
from utils.match_identity import (
    competition_number_for_match,
    normalize_competition_number,
    same_event,
)
from utils.scraper import _fill_missing_odds
from utils.source_identity import (
    CONFLICT_STRONG_NUMBER_CONFLICT,
    detect_strong_number_conflict,
    observe_source_row,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
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

DATE = "2030-05-01"


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


def lottery(record_id="500j-2030-05-01-6308", **overrides):
    payload = {
        "league": "美国职业篮球联盟",
        "time": "00:00",
        "home": "犹他爵士",
        "away": "波特兰开拓者",
        "jczq_no": "6308",
    }
    payload.update(overrides)
    return basketball(record_id, **payload)


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


def duplicate_pair_fixture(root):
    """One cross-namespace pair (reversed orientation) plus a genuinely different event."""
    return write_dataset(
        root,
        [
            basketball("500lq-2030-05-01-周六308"),
            lottery(),
            basketball("500lq-2030-05-01-周六309", home="洛杉矶湖人", away="金州勇士",
                       jczq_no="周六309"),
            lottery("500j-2030-05-01-6309", home="金州勇士", away="洛杉矶湖人",
                    jczq_no="6309"),
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


def leaves(root):
    """The two ids of the canonical pair under test, in deterministic order."""
    return ["500j-2030-05-01-6308", "500lq-2030-05-01-周六308"]


def candidate_for(root, canonical_ids=None):
    dataset = load_dataset(str(root))
    wanted = sorted(canonical_ids or leaves(root))
    for candidate in build_candidates(dataset):
        if candidate["canonical_ids"] == wanted:
            return candidate
    raise AssertionError(f"no candidate for {wanted}")


def review_document(queue, records):
    return {
        "schema_version": "1.0.0",
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "reviews": list(records),
    }


def review_record(candidate, decision=DECISION_SAME_EVENT, **overrides):
    record = {
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "dataset_fingerprint": candidate["dataset_fingerprint"],
        "canonical_ids": list(candidate["canonical_ids"]),
        "decision": decision,
        "label_provenance": "HUMAN_REVIEWED",
        "reviewer": REVIEWER,
        "decided_at": "2030-05-02T00:00:00Z",
        "evidence_reference": "local review",
        "notes": "",
        "schema_version": "1.0.0",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# NX-01 .. NX-07  number representation
# ---------------------------------------------------------------------------

def test_nx_01_number_representation_discrepancy_is_detected():
    display = interpret("周六308", sport="basketball", event_date=DATE,
                        source_namespace="500lq")
    encoded = interpret("6308", sport="basketball", event_date=DATE,
                        source_namespace="500j")
    assert display.method == METHOD_WEEKDAY_PREFIX
    assert encoded.method == METHOD_ENCODED_DIGIT
    assert display.interpreted_encoded == encoded.interpreted_encoded == "6308"
    assert display.weekday_digit == encoded.weekday_digit == 6

    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308"), lottery()
    )
    assert evidence.numbers_agree is True
    assert diagnostic_class(evidence) == CLASS_BOTH


def test_nx_02_unverified_conversion_remains_non_authoritative():
    alone = interpret("6308", sport="basketball", event_date=DATE, source_namespace="500j")
    assert alone.confidence == CONFIDENCE_UNVERIFIED
    assert alone.authoritative is False
    assert alone.schema_version == "1.0.0"

    evidence = classify_cross_source_candidate(basketball("500lq-2030-05-01-周六308"), lottery())
    assert evidence.authoritative is False
    assert evidence.is_approval is False
    assert evidence.authorizes_odds_merge is False
    assert evidence.authorizes_event_admission is False


def test_nx_03_supported_interpretation_retains_original_values():
    supported = interpret(
        "周六308",
        sport="basketball",
        event_date=DATE,
        source_namespace="500lq",
        counterpart_raw="6308",
    )
    assert supported.confidence == CONFIDENCE_SUPPORTED
    assert supported.raw == "周六308"          # original representation preserved
    assert supported.as_dict()["raw"] == "周六308"
    assert supported.converts is True
    assert supported.authoritative is False


def test_nx_04_malformed_number_cannot_become_false_certainty():
    for bad in ("周六31", "123456", "", "周一ab8"):
        result = interpret(bad, sport="basketball", event_date=DATE, source_namespace="500j")
        assert result.method == METHOD_MALFORMED
        assert result.converts is False
        assert result.interpreted_encoded is None
        assert result.authoritative is False

    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308", jczq_no="周六31"), lottery()
    )
    assert evidence.numbers_agree is None
    assert "numbers are not comparable" in " ".join(evidence.blockers)
    assert evidence.outcome != OUTCOME_CANDIDATE_SAME_EVENT


def test_nx_05_weekday_mismatch_is_explicitly_reported():
    # 2030-05-01 is a Wednesday (3); the number says 六 (6).
    assert event_weekday_digit(DATE) == 3
    result = interpret("周六308", sport="basketball", event_date=DATE, source_namespace="500lq")
    assert result.weekday_matches_event_date is False
    assert any("不一致" in item for item in result.contradictory_evidence)
    assert result.authoritative is False


def test_nx_06_cross_sport_numbers_remain_isolated():
    basketball_record = basketball("500j-2030-05-01-6308")
    football_record = basketball(
        "500lq-2030-05-01-周六308", sport="football", jczq_no="周六308"
    )
    evidence = classify_cross_source_candidate(basketball_record, football_record)
    assert evidence.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT
    assert diagnostic_class(evidence) == CLASS_DISTINCT
    assert any("sport mismatch" in item for item in evidence.contradictory_evidence)

    # The interpretation itself carries the sport it was computed for.
    assert interpret("6308", sport="football", event_date=DATE).sport == "football"


def test_nx_07_date_mismatch_rejects_candidate_equality():
    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308", date="2030-05-02"), lottery()
    )
    assert evidence.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT
    assert diagnostic_class(evidence) == CLASS_DATE


# ---------------------------------------------------------------------------
# NX-08 .. NX-10  home/away orientation
# ---------------------------------------------------------------------------

def test_nx_08_home_away_inversion_is_detected():
    evidence = classify_cross_source_candidate(basketball("500lq-2030-05-01-周六308"), lottery())
    assert evidence.orientation == ORIENTATION_REVERSED
    assert evidence.home_relation != RELATION_EXACT       # direct comparison differs
    assert evidence.aligned_home_relation == RELATION_EXACT
    assert evidence.aligned_away_relation == RELATION_EXACT


def test_nx_09_inversion_alone_does_not_authorize_equality():
    evidence = classify_cross_source_candidate(basketball("500lq-2030-05-01-周六308"), lottery())
    assert evidence.outcome == OUTCOME_AMBIGUOUS
    assert evidence.outcome != OUTCOME_CANDIDATE_SAME_EVENT
    assert "ORIENTATION_UNRESOLVED_NO_AUTOMATIC_SWAP" in evidence.blockers
    assert evidence.authoritative is False


def test_nx_10_market_odds_orientation_is_never_swapped():
    evidence = classify_cross_source_candidate(basketball("500lq-2030-05-01-周六308"), lottery())
    assert evidence.authorizes_odds_merge is False
    payload = evidence.as_dict()
    assert "swapped" not in payload
    assert "home_win" not in payload and "away_win" not in payload
    assert not hasattr(evidence, "swap")
    # The module exposes no write path at all.
    import utils.basketball_identity_evidence as module

    assert not any(
        name.startswith(("write", "apply", "merge", "swap"))
        for name in dir(module)
        if callable(getattr(module, name))
    )


# ---------------------------------------------------------------------------
# NX-11 .. NX-12  identity safety
# ---------------------------------------------------------------------------

def test_nx_11_verified_strong_number_conflict_remains_fail_closed():
    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308"),
        lottery(),
        {"strong_number_conflict": True},
    )
    assert evidence.outcome == OUTCOME_CONTRADICTORY_EVIDENCE
    assert evidence.authoritative is False

    base = observe_source_row(
        basketball("500j-2030-05-01-1301", jczq_no="1301"),
        adapter="jclq_sf",
        canonical_match_id="500j-2030-05-01-1301",
    )
    conflicting = observe_source_row(
        dict(basketball("500j-2030-05-01-1301", jczq_no="1302")),
        adapter="jclq_sf",
        canonical_match_id="500j-2030-05-01-1301",
    )
    assert base.derived_source_id == conflicting.derived_source_id
    assert base.competition_number_normalized != conflicting.competition_number_normalized
    conflicts = detect_strong_number_conflict([base, conflicting])
    assert [item.category for item in conflicts] == [CONFLICT_STRONG_NUMBER_CONFLICT]
    assert "REJECT" in json.dumps(conflicts[0].as_dict(), ensure_ascii=False)


def test_nx_12_missing_provider_id_does_not_become_verified_identity():
    from utils.source_identity import ScopedSourceIdentityKey

    observation = observe_source_row(
        basketball("500lq-2030-05-01-周六308"), adapter="live_basketball"
    )
    assert observation.raw_provider_id is None
    assert observation.raw_provider_id_present is False
    assert observation.provider_id_uniqueness_verified is False
    key = ScopedSourceIdentityKey.for_observation(observation)
    assert key.identity_kind != "RAW_PROVIDER_ID" or key.provider_id_uniqueness_scope != "VERIFIED_SCOPED"

    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308", jczq_no=""),
        lottery(jczq_no=""),
    )
    assert evidence.numbers_comparable is False
    assert evidence.numbers_agree is None
    assert evidence.authoritative is False


# ---------------------------------------------------------------------------
# NX-13 .. NX-17  determinism, binding, review independence
# ---------------------------------------------------------------------------

def test_nx_13_candidate_ordering_is_deterministic(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    first = build_candidates(load_dataset(root))
    second = build_candidates(load_dataset(root))
    assert [item["candidate_id"] for item in first] == [item["candidate_id"] for item in second]
    assert [item["canonical_ids"] for item in first] == [item["canonical_ids"] for item in second]
    assert first[0]["candidate_id"].startswith(CANDIDATE_ID_PREFIX)


def test_nx_14_candidate_fingerprint_binds_exact_source_evidence(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    candidate = candidate_for(root)
    altered = json.loads(json.dumps(candidate))
    altered["team_names"]["500lq-2030-05-01-周六308"]["home"] = "另一支球队"
    assert _candidate_fingerprint(altered) != candidate["candidate_fingerprint"]

    untouched = json.loads(json.dumps(candidate))
    untouched["reviewer_status"] = "SAME_EVENT"      # review bookkeeping must not bind
    untouched["review_provenance"] = "HUMAN_REVIEWED"
    assert _candidate_fingerprint(untouched) == candidate["candidate_fingerprint"]


def test_nx_15_human_labels_stay_independent_of_automated_diagnostics(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    scan = build_scan(load_dataset(root))
    candidate = scan["candidates"][0]
    queue = {
        "schema_version": "1.0.0",
        "dataset_fingerprint": scan["dataset_fingerprint"],
        "candidate_count": scan["candidate_count"],
        "candidates": scan["candidates"],
    }
    validation = validate_reviews(queue, review_document(queue, [review_record(candidate)]))
    assert validation["rejected"] == []
    assert validation["accepted"][candidate["candidate_id"]]["decision"] == DECISION_SAME_EVENT
    # The diagnostic class is unchanged by the human decision, and the queue stays UNREVIEWED.
    assert candidate["candidate_class"] == CLASS_BOTH
    assert candidate["reviewer_status"] == "UNREVIEWED"
    assert candidate["is_approval"] is False


def test_nx_16_stale_review_records_are_rejected(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    scan = build_scan(load_dataset(root))
    candidate = scan["candidates"][0]
    queue = {
        "schema_version": "1.0.0",
        "dataset_fingerprint": scan["dataset_fingerprint"],
        "candidate_count": scan["candidate_count"],
        "candidates": scan["candidates"],
    }
    stale_dataset = validate_reviews(
        queue,
        review_document(queue, [review_record(candidate, dataset_fingerprint="deadbeef")]),
    )
    assert stale_dataset["rejected"][0]["reason_code"] == "DATASET_FINGERPRINT_MISMATCH"
    assert stale_dataset["accepted"] == {}

    stale_evidence = validate_reviews(
        queue,
        review_document(queue, [review_record(candidate, candidate_fingerprint="deadbeef")]),
    )
    assert stale_evidence["rejected"][0]["reason_code"] == "STALE_CANDIDATE_FINGERPRINT"


def test_nx_17_canonical_impact_preview_cannot_apply(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    document = command_preview(root, None)
    assert document["executable"] is False
    assert document["apply_available"] is False
    assert document["production_write_authorization"] is False
    assert document["surviving_canonical_id"] is None
    assert all(entry["proposed_operations"] == [] for entry in document["entries"])
    assert all(entry["surviving_canonical_id"] is None for entry in document["entries"])


# ---------------------------------------------------------------------------
# NX-18 .. NX-20  historical references untouched
# ---------------------------------------------------------------------------

def test_nx_18_historical_prediction_references_unchanged(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    before = store_hashes(root)
    candidate = candidate_for(root)
    scan = build_scan(load_dataset(root))
    after = store_hashes(root)
    prediction_ids = {
        mid: candidate["prediction_references"][mid]["ids"]
        for mid in candidate["canonical_ids"]
    }
    assert prediction_ids["500j-2030-05-01-6308"] == ["ps-lot"]
    assert prediction_ids["500lq-2030-05-01-周六308"] == ["ps-live"]
    assert scan["candidates"]
    assert before == after


def test_nx_19_odds_snapshot_references_unchanged(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    before = store_hashes(root)
    candidate = candidate_for(root)
    assert candidate["odds_references"]["500j-2030-05-01-6308"]["ids"] == ["os-lot"]
    assert candidate["odds_references"]["500lq-2030-05-01-周六308"]["ids"] == ["os-live"]
    assert store_hashes(root) == before


def test_nx_20_settlement_and_evaluation_references_unchanged(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    before = store_hashes(root)
    candidate = candidate_for(root)
    assert candidate["settlement_references"]["500j-2030-05-01-6308"]["ids"] == ["st-lot"]
    assert candidate["evaluation_references"]["500j-2030-05-01-6308"]["ids"] == ["ev-lot"]
    assert candidate["settlement_references"]["500lq-2030-05-01-周六308"]["count"] == 0
    assert store_hashes(root) == before


# ---------------------------------------------------------------------------
# NX-21 .. NX-24  preserved earlier-phase contracts
# ---------------------------------------------------------------------------

def test_nx_21_existing_same_event_semantics_unchanged():
    base = basketball("500lq-2030-05-01-周六308")
    same_number = lottery(home="波特兰开拓者", away="犹他爵士")
    other_number = lottery("500j-2030-05-01-6399", jczq_no="6399")
    reversed_without_number = basketball(
        "500j-2030-05-01-6308", jczq_no="", home="犹他爵士", away="波特兰开拓者"
    )

    assert same_event(base, same_number) is False          # 308 vs 6308: numbers decide and differ
    assert same_event(base, lottery(jczq_no="308")) is True  # identical numbers -> same event
    assert same_event(base, other_number) is False        # both numbered -> numbers decide
    assert same_event(base, reversed_without_number) is False  # exact home/away required
    assert same_event(base, {"id": "x", "sport": "football", "date": DATE}) is False
    # normalisation itself is untouched: display form keeps only digits
    assert normalize_competition_number("周六308") == "308"
    assert normalize_competition_number("6308") == "6308"
    assert competition_number_for_match(lottery()) == "6308"


def test_nx_22_strong_number_conflict_detection_preserved():
    row_a = observe_source_row(
        basketball("500lq-2030-05-01-周六308", jczq_no="周六308"),
        adapter="live_basketball",
    )
    row_b = observe_source_row(
        basketball("500lq-2030-05-01-周六309", jczq_no="周六309"),
        adapter="live_basketball",
    )
    # Different source events in one scope must not be reported as a conflict.
    assert row_a.stability_scope.as_key() == row_b.stability_scope.as_key()
    assert detect_strong_number_conflict([row_a, row_b]) == ()


def test_nx_23_market_admission_contract_preserved():
    assert has_usable_market_odds(
        {"sport": "basketball", "odds": {"home_win": 1.9, "away_win": 1.9}}
    ) is True
    assert has_usable_market_odds(
        {"sport": "basketball", "odds": {"total_line": 220.5}}
    ) is False
    assert has_usable_market_odds(
        {"sport": "basketball", "odds": {"home_win": 0, "away_win": 1.9}}
    ) is False
    assert has_usable_market_odds({"sport": "basketball", "odds": {"matchnum": 6308}}) is False


def test_nx_24_invalid_odds_correction_preserved():
    target = {"home_win": 0.0, "away_win": 1.0, "total_line": float("nan")}
    changed = _fill_missing_odds(target, {"home_win": 1.6, "away_win": 1.9, "total_line": 220.5})
    assert changed is True
    assert target["home_win"] == 1.6
    assert target["away_win"] == 1.9
    assert target["total_line"] == 220.5

    valid = {"home_win": 1.6, "away_win": 1.9}
    assert _fill_missing_odds(valid, {"home_win": 1.75, "away_win": 2.1}) is False
    assert valid == {"home_win": 1.6, "away_win": 1.9}

    invalid_source = {"home_win": 0, "away_win": float("inf")}
    keep = {}
    assert _fill_missing_odds(keep, invalid_source) is False
    assert keep == {}


# ---------------------------------------------------------------------------
# NX-25 .. NX-28  honest gaps, protected data, activation, platforms
# ---------------------------------------------------------------------------

def test_nx_25_missing_real_dataset_reports_not_available(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    document = command_scan(str(empty), max_controls=1)
    assert document["real_data_available"] is False
    assert document["status"] == "NOT_AVAILABLE"
    assert document["candidate_count"] is None
    assert document["candidates"] == []
    assert document["decision"] == "NO-GO"
    with pytest.raises(AuditInputError):
        load_dataset(str(empty))


def test_nx_26_seven_protected_json_files_unchanged():
    before = protected_store_hashes()
    assert len(before) == 7
    document = command_scan(DATA_DIR, max_controls=1)
    scan = build_scan(load_dataset(DATA_DIR))
    assert document["candidate_count"] == scan["candidate_count"]
    assert protected_store_hashes() == before
    assert store_hashes(DATA_DIR) == before


def test_nx_27_no_production_activation():
    audit = production_boundary_audit()
    assert audit["production_activation"] == "NOT_ENABLED"
    scan = command_scan(DATA_DIR, max_controls=1)
    assert scan["production_write_authorization"] is False
    assert scan["executable"] is False
    assert scan["surviving_canonical_id"] is None
    assert scan["decision"] == "NO-GO"


def test_nx_28_ci_covers_both_platforms_and_output_is_byte_stable(tmp_path):
    ci = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    assert ci.count("os: [ubuntu-latest, windows-latest]") == 2

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert main(["scan", "--data-dir", DATA_DIR, "--output", str(first), "--max-controls", "1"]) == 0
    assert main(["scan", "--data-dir", DATA_DIR, "--output", str(second), "--max-controls", "1"]) == 0
    assert first.read_bytes() == second.read_bytes()


# ---------------------------------------------------------------------------
# Auxiliary: real-data inventory and explain command wiring
# ---------------------------------------------------------------------------

def test_nx_aux_inventory_and_explain_are_wired(tmp_path):
    root = duplicate_pair_fixture(tmp_path)
    inventory = build_inventory(load_dataset(root))
    assert inventory["basketball_records"] == 4
    assert inventory["nba_records"] == 4
    assert inventory["basketball_namespaces"] == {"500j": 2, "500lq": 2}

    candidate = candidate_for(root)
    explained = command_explain(root, candidate["candidate_id"])
    assert explained["found"] is True
    assert explained["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert explained["executable"] is False

    missing = command_explain(root, "NXD-does-not-exist")
    assert missing["found"] is False
    assert missing["candidate"] is None


def test_nx_aux_inventory_ignores_insufficient_outcome_in_candidates(tmp_path):
    """A pair whose team names are exclusive is not a candidate (fail closed)."""
    root = write_dataset(
        tmp_path,
        [
            basketball("500lq-2030-05-01-周六308", home="A队", away="B队"),
            lottery("500j-2030-05-01-6399", home="C队", away="D队", jczq_no="6399"),
        ],
    )
    dataset = load_dataset(root)
    assert build_candidates(dataset) == []
    evidence = classify_cross_source_candidate(
        basketball("500lq-2030-05-01-周六308", home="A队", away="B队"),
        lottery("500j-2030-05-01-6399", home="C队", away="D队", jczq_no="6399"),
    )
    assert evidence.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT
    assert evidence.outcome not in (OUTCOME_INSUFFICIENT_EVIDENCE, OUTCOME_CONTRADICTORY_EVIDENCE)
    assert diagnostic_class(evidence) in (CLASS_DISTINCT, CLASS_INSUFFICIENT)
    assert SCAN_KIND == "READ_ONLY_NBA_CROSS_SOURCE_DISCREPANCY_SCAN"
    assert dataset_fingerprint(dataset) == dataset_fingerprint(load_dataset(root))