"""Unit tests for Historical Review immutable evidence presentation and qualification.
===================================================================================
Verifies that:
- Predictions and accuracy are derived strictly from immutable evaluation rows.
- /history never invokes predict_match(...) or recomputes after kickoff.
- Finished matches without valid prematch evidence remain visible but excluded from accuracy.
- Qualification enforces strict unbroken evidence chain (snapshot, settlement, eval).
- Ambiguous or conflicting records fail closed.
- Strict score handling and factual outcome semantics (including basketball draws).
- Complete exclusive partition across visible matches.
- Frozen top-score coverage uses stored candidate sets.
- Denominators are consistent across sport filters and row limits.
"""
from __future__ import annotations

import json
import os
import pytest

import config
from utils.match_lifecycle import valid_full_time_score
from utils.prediction_snapshots import (
    MODEL_NAMES,
    snapshot_id_for,
)
from utils.settlements import (
    outcome_from_score,
    result_fingerprint,
    settlement_id_for,
)
from utils.evaluation_rows import evaluation_id_for
from utils.history_evidence import (
    build_history_view_data,
    qualify_current_version_evaluation,
)


def _make_pipeline_evidence(
    match_id: str,
    sport: str = "football",
    model_version: str | None = None,
    model_name: str | None = None,
    model_probs: dict | None = None,
    final_score: dict | None = None,
    actual_outcome: str | None = None,
    top_scores: list[dict] | None = None,
    gen_at: str = "2026-10-01T12:00:00+08:00",
    kick_at: str = "2026-10-01T20:00:00+08:00",
    settled_at: str = "2026-10-01T22:00:00+08:00",
) -> tuple[dict, dict, dict]:
    """Create genuinely linked snapshot, settlement, and evaluation row."""
    if model_version is None:
        model_version = config.MODEL_VERSIONS.get(sport, "football-coldstart-1")
    if model_name is None:
        model_name = MODEL_NAMES.get(sport, "elo-poisson-dixon-coles")
    if model_probs is None:
        model_probs = (
            {"home_win": 55, "draw": 25, "away_win": 20}
            if sport == "football"
            else {"home_win": 65, "away_win": 35}
        )
    if final_score is None:
        final_score = {"home": 2, "away": 1} if sport == "football" else {"home": 102, "away": 98}
    if actual_outcome is None:
        actual_outcome = outcome_from_score(final_score)

    snap_id = snapshot_id_for(match_id, model_version)
    sett_id = settlement_id_for(snap_id)
    eval_id = evaluation_id_for(snap_id, sett_id)

    expected_score_data = {}
    if top_scores is not None:
        expected_score_data["top_scores"] = top_scores

    snap = {
        "snapshot_id": snap_id,
        "match_id": match_id,
        "sport": sport,
        "league": "英超" if sport == "football" else "NBA",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": model_name,
        "model_version": model_version,
        "generated_at": gen_at,
        "kickoff_at": kick_at,
        "model_probabilities": dict(model_probs),
        "expected_score_data": expected_score_data,
    }

    sett = {
        "settlement_id": sett_id,
        "snapshot_id": snap_id,
        "match_id": match_id,
        "sport": sport,
        "league": "英超" if sport == "football" else "NBA",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": model_name,
        "model_version": model_version,
        "prediction_generated_at": gen_at,
        "kickoff_at": kick_at,
        "settled_at": settled_at,
        "final_score": dict(final_score),
        "actual_outcome": actual_outcome,
        "result_fingerprint": result_fingerprint(final_score),
    }

    eval_row = {
        "evaluation_id": eval_id,
        "snapshot_id": snap_id,
        "settlement_id": sett_id,
        "match_id": match_id,
        "sport": sport,
        "league": "英超" if sport == "football" else "NBA",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": model_name,
        "model_version": model_version,
        "prediction_generated_at": gen_at,
        "kickoff_at": kick_at,
        "model_probabilities": dict(model_probs),
        "final_score": dict(final_score),
        "actual_outcome": actual_outcome,
        "expected_score_data": expected_score_data,
    }

    return snap, sett, eval_row


def _write_runtime_data(
    data_dir: str,
    matches: list[dict],
    eval_rows: list[dict] | None = None,
    snapshots: list[dict] | dict | None = None,
    settlements: list[dict] | dict | None = None,
) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "daily_matches.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": {}, "matches": matches}, fh, ensure_ascii=False)

    if eval_rows is not None:
        row_dict = {
            r["evaluation_id"]: r
            for r in eval_rows
            if isinstance(r, dict) and r.get("evaluation_id")
        }
        with open(os.path.join(data_dir, config.EVALUATION_ROW_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rows": row_dict}, fh, ensure_ascii=False)

    # Snapshots
    if snapshots is not None:
        if isinstance(snapshots, list):
            s_dict = {
                s["snapshot_id"]: s
                for s in snapshots
                if isinstance(s, dict) and s.get("snapshot_id")
            }
        else:
            s_dict = snapshots
    else:
        s_dict = {}
        if eval_rows is not None:
            for r in eval_rows:
                if isinstance(r, dict) and r.get("snapshot_id"):
                    s_dict[r["snapshot_id"]] = {
                        "snapshot_id": r["snapshot_id"],
                        "match_id": r.get("match_id", ""),
                        "sport": r.get("sport", "football"),
                        "league": r.get("league", "英超"),
                        "home_team": r.get("home_team", "主队"),
                        "away_team": r.get("away_team", "客队"),
                        "model_name": r.get("model_name", MODEL_NAMES.get(r.get("sport", "football"), "")),
                        "model_version": r.get("model_version", ""),
                        "generated_at": r.get("prediction_generated_at", "2026-10-01T12:00:00+08:00"),
                        "kickoff_at": r.get("kickoff_at", "2026-10-01T20:00:00+08:00"),
                        "model_probabilities": dict(r.get("model_probabilities") or {}),
                        "expected_score_data": dict(r.get("expected_score_data") or {}),
                    }
    with open(os.path.join(data_dir, config.PREDICTION_SNAPSHOT_FILE), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "snapshots": s_dict}, fh, ensure_ascii=False)

    # Settlements
    if settlements is not None:
        if isinstance(settlements, list):
            st_dict = {
                s["settlement_id"]: s
                for s in settlements
                if isinstance(s, dict) and s.get("settlement_id")
            }
        else:
            st_dict = settlements
    else:
        st_dict = {}
        if eval_rows is not None:
            for r in eval_rows:
                if isinstance(r, dict) and r.get("settlement_id"):
                    final = dict(r.get("final_score") or {"home": 2, "away": 1})
                    st_dict[r["settlement_id"]] = {
                        "settlement_id": r["settlement_id"],
                        "snapshot_id": r.get("snapshot_id", ""),
                        "match_id": r.get("match_id", ""),
                        "sport": r.get("sport", "football"),
                        "league": r.get("league", "英超"),
                        "home_team": r.get("home_team", "主队"),
                        "away_team": r.get("away_team", "客队"),
                        "model_name": r.get("model_name", MODEL_NAMES.get(r.get("sport", "football"), "")),
                        "model_version": r.get("model_version", ""),
                        "prediction_generated_at": r.get("prediction_generated_at", "2026-10-01T12:00:00+08:00"),
                        "kickoff_at": r.get("kickoff_at", "2026-10-01T20:00:00+08:00"),
                        "settled_at": "2026-10-01T22:00:00+08:00",
                        "final_score": final,
                        "actual_outcome": r.get("actual_outcome", "home_win"),
                        "result_fingerprint": result_fingerprint(final),
                    }
    with open(os.path.join(data_dir, config.SETTLEMENT_FILE), "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "settlements": st_dict}, fh, ensure_ascii=False)


def _make_eval_row(
    match_id: str,
    sport: str = "football",
    model_version: str | None = None,
    model_name: str | None = None,
    model_probs: dict | None = None,
    actual_outcome: str | None = None,
    final_score: dict | None = None,
    top_scores: list[dict] | None = None,
    gen_at: str = "2026-10-01T12:00:00+08:00",
    kick_at: str = "2026-10-01T20:00:00+08:00",
) -> dict:
    _, _, eval_row = _make_pipeline_evidence(
        match_id=match_id,
        sport=sport,
        model_version=model_version,
        model_name=model_name,
        model_probs=model_probs,
        final_score=final_score,
        actual_outcome=actual_outcome,
        top_scores=top_scores,
        gen_at=gen_at,
        kick_at=kick_at,
    )
    return eval_row


# ==============================================================================
# SECTION A: 20 Mandatory Acceptance Test Cases
# ==============================================================================

def test_mandatory_01_valid_snapshot_settlement_evaluation_qualifies(make_match):
    """1. Valid snapshot + settlement + evaluation qualifies."""
    m = make_match(id="m_val_01", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_01", sport="football", final_score={"home": 2, "away": 1})
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is True
    assert reason == ""

    data = build_history_view_data([m], [eval_row], sport="football", snapshots=snaps, settlements=setts)
    assert data["matches"][0]["is_verified"] is True
    assert data["matches"][0]["comparison"]["evidence_status"] == "verified"


def test_mandatory_02_evaluation_exists_referenced_snapshot_missing(make_match):
    """2. Evaluation exists but referenced snapshot is missing."""
    m = make_match(id="m_val_02", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_02")
    snaps = {}  # Missing snapshot
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "referenced_snapshot_missing"

    data = build_history_view_data([m], [eval_row], sport="football", snapshots=snaps, settlements=setts)
    assert data["matches"][0]["is_verified"] is False
    assert data["matches"][0]["comparison"]["evidence_status"] == "invalid"
    assert data["stats"]["football"]["verified_evaluations"] == 0


def test_mandatory_03_evaluation_exists_referenced_settlement_missing(make_match):
    """3. Evaluation exists but referenced settlement is missing."""
    m = make_match(id="m_val_03", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_03")
    snaps = {snap["snapshot_id"]: snap}
    setts = {}  # Missing settlement

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "referenced_settlement_missing"

    data = build_history_view_data([m], [eval_row], sport="football", snapshots=snaps, settlements=setts)
    assert data["matches"][0]["is_verified"] is False
    assert data["matches"][0]["comparison"]["evidence_status"] == "invalid"


def test_mandatory_04_evaluation_match_id_mismatch(make_match):
    """4. Evaluation match ID mismatches the canonical match."""
    m = make_match(id="m_val_04_real", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_04_other")
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "match_id_mismatch"


def test_mandatory_05_evaluation_sport_mismatch(make_match):
    """5. Evaluation sport mismatches the canonical sport."""
    m = make_match(id="m_val_05", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_05", sport="basketball")
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "sport_mismatch"


def test_mandatory_06_correct_version_incorrect_model_name(make_match):
    """6. Correct version but incorrect model name."""
    m = make_match(id="m_val_06", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_06", model_name="unauthorized-model")
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "model_name_mismatch"


def test_mandatory_07_prediction_generated_after_kickoff(make_match):
    """7. Prematch prediction generated after kickoff."""
    m = make_match(id="m_val_07", status="finished", score={"ft": [2, 1]})
    # Generated at 21:00, kickoff was at 20:00 (post-kickoff leakage)
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_val_07",
        gen_at="2026-10-01T21:00:00+08:00",
        kick_at="2026-10-01T20:00:00+08:00",
    )
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "invalid_prematch_timestamps"


def test_mandatory_08_missing_or_malformed_prematch_timestamps(make_match):
    """8. Missing or malformed prematch timestamps."""
    m = make_match(id="m_val_08", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_08")
    eval_row["prediction_generated_at"] = "invalid-date-string"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "invalid_prematch_timestamps"


def test_mandatory_09_evaluation_probabilities_differ_from_snapshot(make_match):
    """9. Evaluation model probabilities differ from the immutable snapshot."""
    m = make_match(id="m_val_09", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_09")
    eval_row["model_probabilities"] = {"home_win": 70, "draw": 20, "away_win": 10}
    snap["model_probabilities"] = {"home_win": 55, "draw": 25, "away_win": 20}
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "probabilities_snapshot_mismatch"


def test_mandatory_10_invalid_or_incomplete_probability_vector(make_match):
    """10. Invalid or incomplete probability vector."""
    m = make_match(id="m_val_10", status="finished", score={"ft": [2, 1]})
    # Does not sum to [99, 101]
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_val_10",
        model_probs={"home_win": 140, "draw": 20, "away_win": 10},
    )
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "classification_validation_failed"


def test_mandatory_11_evaluation_actual_outcome_disagrees_with_canonical_score(make_match):
    """11. Evaluation actual outcome disagrees with valid canonical final score."""
    m = make_match(id="m_val_11", status="finished", score={"ft": [2, 1]})  # home_win
    snap, sett, eval_row = _make_pipeline_evidence("m_val_11", final_score={"home": 2, "away": 1})
    eval_row["actual_outcome"] = "away_win"  # Outcome disagreement
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "score_or_outcome_invalid"


def test_mandatory_12_settlement_actual_outcome_disagrees_with_evaluation(make_match):
    """12. Settlement actual outcome disagrees with evaluation."""
    m = make_match(id="m_val_12", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_val_12", final_score={"home": 2, "away": 1})
    sett["actual_outcome"] = "draw"  # Settlement disagrees
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "score_or_outcome_invalid"


def test_mandatory_13_two_distinct_evaluation_identities_fail_closed_as_ambiguous(make_match):
    """13. Two distinct evaluation identities with the same model probabilities and outcome."""
    m = make_match(id="m_val_13", status="finished", score={"ft": [2, 1]})
    snap, sett, eval1 = _make_pipeline_evidence("m_val_13")
    eval2 = dict(eval1)
    eval2["evaluation_id"] = "eval-distinct-second-id"  # Distinct identity

    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval1, eval2], sport="football", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    assert row["is_verified"] is False
    assert row["comparison"]["evidence_status"] == "ambiguous"
    assert data["stats"]["football"]["verified_evaluations"] == 0


def test_mandatory_14_multiple_historical_current_records_cannot_inflate_denominator(make_match):
    """14. Multiple historical/current records cannot inflate a current-model denominator."""
    m = make_match(id="m_val_14", status="finished", score={"ft": [2, 1]})
    _, _, e_curr = _make_pipeline_evidence("m_val_14", model_version=config.MODEL_VERSIONS["football"])
    _, _, e_hist = _make_pipeline_evidence("m_val_14", model_version="legacy-v0")

    data = build_history_view_data([m], [e_curr, e_hist], sport="football")
    # Multiple distinct identities fail closed as ambiguous, never inflating denominator
    assert data["stats"]["football"]["verified_evaluations"] == 0
    assert data["stats"]["football"]["ambiguous_evaluations"] == 1


def test_mandatory_15_malformed_final_score_safely_excluded(make_match):
    """15. Malformed final score remains safely excluded without crashing."""
    m = make_match(id="m_val_15", status="finished", score={"ft": ["2", "1"]})  # Malformed string score
    assert valid_full_time_score(m.get("score")) is False

    snap, sett, eval_row = _make_pipeline_evidence("m_val_15")
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval_row], sport="football", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    assert row["is_verified"] is False
    assert row["comparison"]["evidence_status"] == "invalid"
    assert data["stats"]["football"]["verified_evaluations"] == 0


def test_mandatory_16_basketball_tied_score_not_reclassified_as_away_win(make_match):
    """16. Basketball tied score is not reclassified as away win."""
    m = make_match(id="m_val_16", sport="basketball", status="finished", score={"ft": [95, 95]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_val_16",
        sport="basketball",
        final_score={"home": 95, "away": 95},
        actual_outcome="draw",
    )
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval_row], sport="basketball", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    # Factual outcome must be draw, NOT converted to away_win
    assert row["comparison"]["actual_outcome"] == "draw"
    assert row["comparison"]["actual_label"] == "平局"
    # Excluded from verified two-class evaluation
    assert row["is_verified"] is False
    assert row["comparison"]["evidence_status"] == "invalid"
    assert data["stats"]["basketball"]["verified_evaluations"] == 0


def test_mandatory_17_every_displayed_match_belongs_to_exactly_one_category(make_match):
    """17. Every displayed finished match belongs to exactly one status category."""
    # 1 verified
    m1 = make_match(id="m17_a", status="finished", score={"ft": [2, 1]})
    s1, st1, e1 = _make_pipeline_evidence("m17_a")
    # 1 historical
    m2 = make_match(id="m17_b", status="finished", score={"ft": [1, 0]})
    s2, st2, e2 = _make_pipeline_evidence("m17_b", model_version="legacy-v0")
    # 1 no_evidence
    m3 = make_match(id="m17_c", status="finished", score={"ft": [0, 0]})
    # 1 invalid (score mismatch)
    m4 = make_match(id="m17_d", status="finished", score={"ft": [3, 0]})
    s4, st4, e4 = _make_pipeline_evidence("m17_d", final_score={"home": 0, "away": 3})

    matches = [m1, m2, m3, m4]
    evals = [e1, e2, e4]
    snaps = {s1["snapshot_id"]: s1, s2["snapshot_id"]: s2, s4["snapshot_id"]: s4}
    setts = {st1["settlement_id"]: st1, st2["settlement_id"]: st2, st4["settlement_id"]: st4}

    data = build_history_view_data(matches, evals, sport="football", snapshots=snaps, settlements=setts)
    stats = data["stats"]["football"]

    # Invariant: verified + historical + no_evidence + ambiguous_invalid == total_finished
    assert stats["verified_evaluations"] == 1
    assert stats["historical_evaluations"] == 1
    assert stats["no_evidence_matches"] == 1
    assert stats["ambiguous_evaluations"] == 1
    assert stats["total_finished"] == 4

    partition_sum = (
        stats["verified_evaluations"]
        + stats["historical_evaluations"]
        + stats["no_evidence_matches"]
        + stats["ambiguous_evaluations"]
    )
    assert partition_sum == stats["total_finished"]


def test_mandatory_18_reversing_evaluation_input_order_preserves_results(make_match):
    """18. Reversing evaluation input order does not change results."""
    m1 = make_match(id="m18_a", status="finished", score={"ft": [2, 1]})
    m2 = make_match(id="m18_b", status="finished", score={"ft": [0, 1]})
    s1, st1, e1 = _make_pipeline_evidence("m18_a")
    s2, st2, e2 = _make_pipeline_evidence("m18_b")

    matches = [m1, m2]
    evals_forward = [e1, e2]
    evals_reversed = [e2, e1]
    snaps = {s1["snapshot_id"]: s1, s2["snapshot_id"]: s2}
    setts = {st1["settlement_id"]: st1, st2["settlement_id"]: st2}

    res1 = build_history_view_data(matches, evals_forward, sport="football", snapshots=snaps, settlements=setts)
    res2 = build_history_view_data(matches, evals_reversed, sport="football", snapshots=snaps, settlements=setts)

    assert res1["stats"] == res2["stats"]
    assert [m["id"] for m in res1["matches"]] == [m["id"] for m in res2["matches"]]
    for r1, r2 in zip(res1["matches"], res2["matches"]):
        assert r1["comparison"] == r2["comparison"]


def test_mandatory_19_no_prediction_recomputation_or_runtime_writes(isolated_data_dir, monkeypatch, make_match):
    """19. No prediction recomputation or runtime writes occur."""
    m = make_match(id="m19", status="finished", score={"ft": [2, 1]})
    _, _, e = _make_pipeline_evidence("m19")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    import models.predictor

    def mock_predict(*args, **kwargs):
        raise RuntimeError("CRITICAL: predict_match was called by /history!")

    monkeypatch.setattr(models.predictor, "predict_match", mock_predict)
    if hasattr(app_module, "predict_match"):
        monkeypatch.setattr(app_module, "predict_match", mock_predict)

    files_before = set(os.listdir(str(isolated_data_dir)))

    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200

    files_after = set(os.listdir(str(isolated_data_dir)))
    assert files_before == files_after


def test_mandatory_20_cohort_25_matches_preserves_authoritative_accuracy_and_top3_coverage(make_match):
    """20. The original valid 25-match-style cohort preserves the same authoritative accuracy and frozen Top-3 coverage semantics."""
    matches = []
    evals = []
    snaps = {}
    setts = {}
    top_candidates = [{"score": "2-1"}, {"score": "1-0"}, {"score": "1-1"}]

    for i in range(25):
        mid = f"m20_{i:02d}"
        if i < 15:
            # 15 hits: pred home_win, actual home_win 2-1
            m = make_match(id=mid, status="finished", score={"ft": [2, 1]}, date=f"2026-10-{(i % 20)+1:02d}")
            s, st, e = _make_pipeline_evidence(
                mid,
                model_probs={"home_win": 60, "draw": 20, "away_win": 20},
                final_score={"home": 2, "away": 1},
                actual_outcome="home_win",
                top_scores=top_candidates if i < 5 else [{"score": "0-0"}],
            )
        else:
            # 10 misses: pred away_win, actual home_win 2-1
            m = make_match(id=mid, status="finished", score={"ft": [2, 1]}, date=f"2026-10-{(i % 20)+1:02d}")
            s, st, e = _make_pipeline_evidence(
                mid,
                model_probs={"home_win": 20, "draw": 20, "away_win": 60},
                final_score={"home": 2, "away": 1},
                actual_outcome="home_win",
                top_scores=[{"score": "0-2"}],
            )
        matches.append(m)
        evals.append(e)
        snaps[s["snapshot_id"]] = s
        setts[st["settlement_id"]] = st

    data = build_history_view_data(matches, evals, sport="football", snapshots=snaps, settlements=setts)
    fb = data["stats"]["football"]

    assert fb["total_finished"] == 25
    assert fb["verified_evaluations"] == 25
    assert fb["verified_hits"] == 15
    assert fb["accuracy_pct"] == 60.0
    assert fb["accuracy_str"] == "60.0%"
    assert fb["ratio_str"] == "15/25"

    assert fb["score_eligible"] == 25
    assert fb["score_hits"] == 5
    assert fb["score_coverage_pct"] == 20.0
    assert fb["score_coverage_str"] == "20.0%"
    assert fb["score_ratio_str"] == "5/25"


# ==============================================================================
# SECTION B: Full UI and Endpoint Integration Regressions
# ==============================================================================

def test_ui_01_verified_football_evaluation_argmax_outcome(isolated_data_dir, make_match):
    match = make_match(id="m_ui_1", status="finished", score={"ft": [2, 1]})
    eval_row = _make_eval_row("m_ui_1", model_probs={"home_win": 55, "draw": 25, "away_win": 20}, actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测主胜" in html
    assert "置信55%" in html


def test_ui_02_original_prematch_confidence_displayed(isolated_data_dir, make_match):
    match = make_match(id="m_ui_2", status="finished", score={"ft": [0, 2]})
    eval_row = _make_eval_row("m_ui_2", model_probs={"home_win": 20, "draw": 30, "away_win": 50}, actual_outcome="away_win", final_score={"home": 0, "away": 2})
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "置信50%" in html
    assert "预测客胜" in html


def test_ui_03_finished_match_without_prematch_snapshot_remains_visible(isolated_data_dir, make_match):
    m_no_snap = make_match(id="m_ui_3_unverified", status="finished", score={"ft": [3, 2]}, home="阿森纳", away="切尔西")
    _write_runtime_data(str(isolated_data_dir), [m_no_snap], [])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "阿森纳" in html
    assert "切尔西" in html
    assert "3-2" in html
    assert "无赛前验证预测" in html


def test_ui_04_missing_snapshot_does_not_enter_verified_denominator(isolated_data_dir, make_match):
    m1 = make_match(id="m_ui_4a", status="finished", score={"ft": [1, 0]})
    e1 = _make_eval_row("m_ui_4a", model_probs={"home_win": 60, "draw": 20, "away_win": 20}, actual_outcome="home_win", final_score={"home": 1, "away": 0})

    m2 = make_match(id="m_ui_4b", status="finished", score={"ft": [2, 2]})  # No evaluation

    _write_runtime_data(str(isolated_data_dir), [m1, m2], [e1])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "100.0%" in html
    assert "(1/1)" in html
    assert "1场未验证" in html


def test_ui_05_historical_model_versions_separately_labeled(isolated_data_dir, make_match):
    m = make_match(id="m_ui_5", status="finished", score={"ft": [2, 0]})
    e_hist = _make_eval_row("m_ui_5", model_version="football-legacy-v0", actual_outcome="home_win", final_score={"home": 2, "away": 0})
    _write_runtime_data(str(isolated_data_dir), [m], [e_hist])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "历史版本 (football-legacy-v0)" in html
    assert "0/0" in html


def test_ui_06_football_draw_outcomes_included_correctly(isolated_data_dir, make_match):
    m = make_match(id="m_ui_6", status="finished", score={"ft": [1, 1]})
    e = _make_eval_row("m_ui_6", model_probs={"home_win": 25, "draw": 50, "away_win": 25}, actual_outcome="draw", final_score={"home": 1, "away": 1})
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测平局" in html
    assert "100.0%" in html


def test_ui_07_basketball_filtering_does_not_apply_three_way_rules(isolated_data_dir, make_match):
    m_bb = make_match(id="m_ui_7_bb", sport="basketball", status="finished", score={"ft": [102, 98]})
    e_bb = _make_eval_row(
        "m_ui_7_bb",
        sport="basketball",
        model_version=config.MODEL_VERSIONS["basketball"],
        model_probs={"home_win": 65, "away_win": 35},
        actual_outcome="home_win",
        final_score={"home": 102, "away": 98},
    )
    _write_runtime_data(str(isolated_data_dir), [m_bb], [e_bb])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=basketball")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测主胜" in html
    assert "100.0%" in html
    assert "不适用" in html


def test_ui_08_football_frozen_top_score_coverage_uses_stored_candidates(isolated_data_dir, make_match):
    m1 = make_match(id="m_ui_8a", status="finished", score={"ft": [2, 1]})
    e1 = _make_eval_row(
        "m_ui_8a",
        actual_outcome="home_win",
        final_score={"home": 2, "away": 1},
        top_scores=[{"score": "2-1"}, {"score": "1-0"}, {"score": "1-1"}],
    )
    m2 = make_match(id="m_ui_8b", status="finished", score={"ft": [3, 0]})
    e2 = _make_eval_row(
        "m_ui_8b",
        actual_outcome="home_win",
        final_score={"home": 3, "away": 0},
        top_scores=[{"score": "2-1"}, {"score": "1-0"}, {"score": "1-1"}],
    )
    _write_runtime_data(str(isolated_data_dir), [m1, m2], [e1, e2])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "比分覆盖" in html
    assert "比分未覆盖" in html
    assert "50.0%" in html
    assert "(1/2)" in html


def test_ui_09_pagination_200_row_limit_and_denominator_consistency(isolated_data_dir, make_match):
    matches = []
    evals = []
    for i in range(205):
        mid = f"m_ui_9_{i:03d}"
        m = make_match(id=mid, status="finished", score={"ft": [1, 0]}, date=f"2026-09-{(i % 28)+1:02d}")
        e = _make_eval_row(mid, actual_outcome="home_win", final_score={"home": 1, "away": 0})
        matches.append(m)
        evals.append(e)

    _write_runtime_data(str(isolated_data_dir), matches, evals)

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "(200/200)" in html
    assert "共 205 场完赛" in html


def test_ui_10_frontend_filters_work(isolated_data_dir, make_match):
    m_fb = make_match(id="m_ui_10_fb", sport="football", status="finished", score={"ft": [1, 0]})
    m_bb = make_match(id="m_ui_10_bb", sport="basketball", status="finished", score={"ft": [95, 90]})
    e_fb = _make_eval_row("m_ui_10_fb", sport="football", final_score={"home": 1, "away": 0})
    e_bb = _make_eval_row("m_ui_10_bb", sport="basketball", final_score={"home": 95, "away": 90})

    _write_runtime_data(str(isolated_data_dir), [m_fb, m_bb], [e_fb, e_bb])

    import app as app_module
    client = app_module.app.test_client()

    resp_all = client.get("/history?sport=all")
    assert resp_all.status_code == 200
    html_all = resp_all.get_data(as_text=True)
    assert "足球胜负命中" in html_all
    assert "篮球胜负命中" in html_all

    resp_fb = client.get("/history?sport=football")
    assert resp_fb.status_code == 200
    html_fb = resp_fb.get_data(as_text=True)
    assert "chip-on" in html_fb

    resp_bb = client.get("/history?sport=basketball")
    assert resp_bb.status_code == 200
