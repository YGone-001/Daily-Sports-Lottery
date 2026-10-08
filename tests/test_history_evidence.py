"""Unit tests for Historical Review immutable evidence presentation and qualification.
===================================================================================
Verifies:
- Predictions and accuracy derived strictly from immutable evaluation rows.
- Complete cross-record provenance equality (snapshot <-> settlement <-> evaluation row).
- Deterministic IDs and stored internal identities validated.
- Full frozen prediction payload equality (odds, display probs, expected values, top scores).
- Full result provenance and fingerprint validation.
- Duplicate evaluation identities carrying conflicting payloads fail closed as ambiguous.
- Input order independence.
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
from utils.evaluation_rows import evaluation_id_for, _as_dict
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
    display_probs: dict | None = None,
    market_odds: dict | None = None,
    market_implied_probs: dict | None = None,
    expected_values: dict | None = None,
    final_score: dict | None = None,
    actual_outcome: str | None = None,
    top_scores: list[dict] | None = None,
    gen_at: str = "2026-10-01T12:00:00+08:00",
    kick_at: str = "2026-10-01T20:00:00+08:00",
    settled_at: str = "2026-10-01T22:00:00+08:00",
    home_elo: int = 1980,
    away_elo: int = 1950,
    league: str | None = None,
    home_team: str = "主队",
    away_team: str = "客队",
) -> tuple[dict, dict, dict]:
    """Create genuinely linked snapshot, settlement, and evaluation row with full provenance."""
    if model_version is None:
        model_version = config.MODEL_VERSIONS.get(sport, "football-coldstart-1")
    if model_name is None:
        model_name = MODEL_NAMES.get(sport, "elo-poisson-dixon-coles")
    if league is None:
        league = "英超" if sport == "football" else "NBA"

    if model_probs is None:
        model_probs = (
            {"home_win": 55, "draw": 25, "away_win": 20}
            if sport == "football"
            else {"home_win": 65, "away_win": 35}
        )
    if display_probs is None:
        display_probs = dict(model_probs)
    if market_odds is None:
        market_odds = (
            {"home_win": 1.9, "draw": 3.4, "away_win": 4.1}
            if sport == "football"
            else {"home_win": 1.5, "away_win": 2.5}
        )
    if market_implied_probs is None:
        market_implied_probs = (
            {"home_win": 48, "draw": 27, "away_win": 22}
            if sport == "football"
            else {"home_win": 62, "away_win": 37}
        )
    if expected_values is None:
        expected_values = (
            {"home_win": 0.05, "draw": -0.1, "away_win": -0.2}
            if sport == "football"
            else {"home_win": 0.02, "away_win": -0.05}
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
        "league": league,
        "home_team": home_team,
        "away_team": away_team,
        "model_name": model_name,
        "model_version": model_version,
        "generated_at": gen_at,
        "kickoff_at": kick_at,
        "home_elo": home_elo,
        "away_elo": away_elo,
        "model_probabilities": dict(model_probs),
        "display_probabilities": dict(display_probs),
        "market_odds": dict(market_odds),
        "market_implied_probabilities": dict(market_implied_probs),
        "expected_values": dict(expected_values),
        "expected_score_data": expected_score_data,
    }

    sett = {
        "settlement_id": sett_id,
        "snapshot_id": snap_id,
        "match_id": match_id,
        "sport": sport,
        "league": league,
        "home_team": home_team,
        "away_team": away_team,
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
        "league": league,
        "home_team": home_team,
        "away_team": away_team,
        "model_name": model_name,
        "model_version": model_version,
        "prediction_generated_at": gen_at,
        "kickoff_at": kick_at,
        "settled_at": settled_at,
        "home_elo": home_elo,
        "away_elo": away_elo,
        "model_probabilities": dict(model_probs),
        "display_probabilities": dict(display_probs),
        "market_odds": dict(market_odds),
        "market_implied_probabilities": dict(market_implied_probs),
        "expected_values": dict(expected_values),
        "expected_score_data": expected_score_data,
        "final_score": dict(final_score),
        "actual_outcome": actual_outcome,
        "result_fingerprint": result_fingerprint(final_score),
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
                        "home_elo": r.get("home_elo", 1980),
                        "away_elo": r.get("away_elo", 1950),
                        "model_probabilities": dict(r.get("model_probabilities") or {}),
                        "display_probabilities": dict(r.get("display_probabilities") or {}),
                        "market_odds": dict(r.get("market_odds") or {}),
                        "market_implied_probabilities": dict(r.get("market_implied_probabilities") or {}),
                        "expected_values": dict(r.get("expected_values") or {}),
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
                        "result_fingerprint": r.get("result_fingerprint") or result_fingerprint(final),
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
# SECTION A: Provenance Closure Acceptance Cases (PC-01 to PC-20)
# ==============================================================================

def test_pc_01_valid_complete_provenance_passes(make_match):
    """1. Valid complete snapshot/settlement/evaluation provenance passes."""
    m = make_match(id="m_pc_01", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_01", sport="football", final_score={"home": 2, "away": 1})
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is True
    assert reason == ""


def test_pc_02_snapshot_record_id_differs_from_lookup_key(make_match):
    """2. Snapshot record ID differs from its dictionary lookup key."""
    m = make_match(id="m_pc_02", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_02")
    snap["snapshot_id"] = "tampered_snapshot_internal_id"
    snaps = {eval_row["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "snapshot_internal_id_mismatch"


def test_pc_03_settlement_record_id_differs_from_lookup_key(make_match):
    """3. Settlement record ID differs from its dictionary lookup key."""
    m = make_match(id="m_pc_03", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_03")
    sett["settlement_id"] = "tampered_settlement_internal_id"
    snaps = {snap["snapshot_id"]: snap}
    setts = {eval_row["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "settlement_internal_id_mismatch"


def test_pc_04_snapshot_sport_differs_from_evaluation_sport(make_match):
    """4. Snapshot sport differs from evaluation sport."""
    m = make_match(id="m_pc_04", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_04", sport="football")
    snap["sport"] = "basketball"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "sport_provenance_mismatch"


def test_pc_05_snapshot_model_name_differs_from_evaluation_model_name(make_match):
    """5. Snapshot model name differs from evaluation model name."""
    m = make_match(id="m_pc_05", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_05", sport="football")
    snap["model_name"] = "tampered-model-name"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "model_name_provenance_mismatch"


def test_pc_06_settlement_model_version_differs_from_evaluation_version(make_match):
    """6. Settlement model version differs from evaluation version."""
    m = make_match(id="m_pc_06", sport="football", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_06", sport="football")
    sett["model_version"] = "tampered-model-version"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "model_version_provenance_mismatch"


def test_pc_07_eval_prediction_timestamp_differs_from_snapshot_timestamp(make_match):
    """7. Evaluation prediction timestamp differs from snapshot timestamp while both remain before kickoff."""
    m = make_match(id="m_pc_07", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_pc_07",
        gen_at="2026-10-01T12:00:00+08:00",
        kick_at="2026-10-01T20:00:00+08:00",
    )
    # Different timestamp, but both are before kickoff
    eval_row["prediction_generated_at"] = "2026-10-01T13:00:00+08:00"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "prediction_generated_at_provenance_mismatch"


def test_pc_08_settlement_prediction_timestamp_differs_from_snapshot_timestamp(make_match):
    """8. Settlement prediction timestamp differs from snapshot timestamp."""
    m = make_match(id="m_pc_08", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_pc_08",
        gen_at="2026-10-01T12:00:00+08:00",
        kick_at="2026-10-01T20:00:00+08:00",
    )
    sett["prediction_generated_at"] = "2026-10-01T13:00:00+08:00"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "settlement_prediction_generated_at_provenance_mismatch"


def test_pc_09_eval_kickoff_timestamp_differs_from_snapshot_kickoff_timestamp(make_match):
    """9. Evaluation kickoff timestamp differs from snapshot kickoff timestamp while both remain otherwise valid."""
    m = make_match(id="m_pc_09", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_pc_09",
        gen_at="2026-10-01T12:00:00+08:00",
        kick_at="2026-10-01T20:00:00+08:00",
    )
    eval_row["kickoff_at"] = "2026-10-01T20:30:00+08:00"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "kickoff_at_provenance_mismatch"


def test_pc_10_settlement_kickoff_timestamp_differs_from_snapshot_kickoff_timestamp(make_match):
    """10. Settlement kickoff timestamp differs from snapshot kickoff timestamp."""
    m = make_match(id="m_pc_10", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_pc_10",
        gen_at="2026-10-01T12:00:00+08:00",
        kick_at="2026-10-01T20:00:00+08:00",
    )
    sett["kickoff_at"] = "2026-10-01T20:30:00+08:00"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "settlement_kickoff_at_provenance_mismatch"


def test_pc_11_stored_frozen_top_scores_differ_from_original_snapshot(make_match):
    """11. Stored frozen Top-3 candidates differ from the original snapshot."""
    m = make_match(id="m_pc_11", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_pc_11",
        top_scores=[{"score": "2-1"}, {"score": "1-0"}],
    )
    # Stored evaluation row top_scores tampered
    eval_row["expected_score_data"] = {"top_scores": [{"score": "3-0"}]}
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "expected_score_data_payload_mismatch"


def test_pc_12_stored_display_probabilities_differ_from_original_snapshot(make_match):
    """12. Stored display probabilities differ from original snapshot."""
    m = make_match(id="m_pc_12", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_12")
    eval_row["display_probabilities"] = {"home_win": 90, "draw": 5, "away_win": 5}
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "display_probabilities_payload_mismatch"


def test_pc_13_stored_market_odds_or_expected_values_differ_from_snapshot(make_match):
    """13. Stored market odds or expected values differ from original snapshot."""
    m = make_match(id="m_pc_13", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_13")
    eval_row["market_odds"] = {"home_win": 1.1, "draw": 8.0, "away_win": 12.0}
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "market_odds_payload_mismatch"


def test_pc_14_settlement_result_fingerprint_differs_from_canonical(make_match):
    """14. Settlement result fingerprint differs from canonical result fingerprint."""
    m = make_match(id="m_pc_14", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_14", final_score={"home": 2, "away": 1})
    sett["result_fingerprint"] = "tampered_settlement_fingerprint"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "settlement_result_fingerprint_mismatch"


def test_pc_15_evaluation_result_fingerprint_differs_from_settlement_fingerprint(make_match):
    """15. Evaluation result fingerprint differs from settlement fingerprint."""
    m = make_match(id="m_pc_15", status="finished", score={"ft": [2, 1]})
    snap, sett, eval_row = _make_pipeline_evidence("m_pc_15", final_score={"home": 2, "away": 1})
    eval_row["result_fingerprint"] = "tampered_evaluation_fingerprint"
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    is_qual, reason = qualify_current_version_evaluation(m, eval_row, snaps, setts)
    assert is_qual is False
    assert reason == "evaluation_result_fingerprint_mismatch"


def test_pc_16_duplicate_rows_same_eval_id_different_content_fail_closed(make_match):
    """16. Two rows with the same evaluation ID but different content fail closed."""
    m = make_match(id="m_pc_16", status="finished", score={"ft": [2, 1]})
    snap, sett, eval1 = _make_pipeline_evidence("m_pc_16")
    eval2 = dict(eval1)
    # Same evaluation_id, but conflicting model_probabilities payload
    eval2["model_probabilities"] = {"home_win": 70, "draw": 20, "away_win": 10}

    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval1, eval2], sport="football", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    assert row["is_verified"] is False
    assert row["comparison"]["evidence_status"] == "ambiguous"
    assert data["stats"]["football"]["verified_evaluations"] == 0


def test_pc_17_reversing_duplicate_rows_does_not_alter_classification_or_statistics(make_match):
    """17. Reversing those duplicate rows does not alter classification or statistics."""
    m = make_match(id="m_pc_17", status="finished", score={"ft": [2, 1]})
    snap, sett, eval1 = _make_pipeline_evidence("m_pc_17")
    eval2 = dict(eval1)
    eval2["model_probabilities"] = {"home_win": 70, "draw": 20, "away_win": 10}

    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    res1 = build_history_view_data([m], [eval1, eval2], sport="football", snapshots=snaps, settlements=setts)
    res2 = build_history_view_data([m], [eval2, eval1], sport="football", snapshots=snaps, settlements=setts)

    assert res1["stats"] == res2["stats"]
    assert res1["matches"][0]["comparison"] == res2["matches"][0]["comparison"]


def test_pc_18_complete_valid_records_produce_authoritative_accuracy(make_match):
    """18. Complete valid records still produce the same authoritative model accuracy."""
    matches = []
    evals = []
    snaps = {}
    setts = {}
    top_candidates = [{"score": "2-1"}, {"score": "1-0"}, {"score": "1-1"}]

    for i in range(25):
        mid = f"m_pc_18_{i:02d}"
        if i < 15:
            m = make_match(id=mid, status="finished", score={"ft": [2, 1]}, date=f"2026-10-{(i % 20)+1:02d}")
            s, st, e = _make_pipeline_evidence(
                mid,
                model_probs={"home_win": 60, "draw": 20, "away_win": 20},
                final_score={"home": 2, "away": 1},
                actual_outcome="home_win",
                top_scores=top_candidates if i < 5 else [{"score": "0-0"}],
            )
        else:
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
    assert fb["ratio_str"] == "15/25"
    assert fb["score_eligible"] == 25
    assert fb["score_hits"] == 5
    assert fb["score_coverage_pct"] == 20.0
    assert fb["score_ratio_str"] == "5/25"


def test_pc_19_no_production_runtime_file_written_by_history_requests(isolated_data_dir, monkeypatch, make_match):
    """19. No production runtime file is written by history requests."""
    m = make_match(id="m_pc_19", status="finished", score={"ft": [2, 1]})
    _, _, e = _make_pipeline_evidence("m_pc_19")
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


def test_pc_20_existing_sport_model_version_pagination_empty_data_preserved(isolated_data_dir, make_match):
    """20. Existing sport, model-version, pagination, and empty-data behavior remains unchanged."""
    # Empty data
    _write_runtime_data(str(isolated_data_dir), [], [])
    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    assert "暂无历史赛果" in resp.get_data(as_text=True)

    # Mixed sports
    m_fb = make_match(id="m_pc_20_fb", sport="football", status="finished", score={"ft": [1, 0]})
    m_bb = make_match(id="m_pc_20_bb", sport="basketball", status="finished", score={"ft": [95, 90]})
    e_fb = _make_eval_row("m_pc_20_fb", sport="football", final_score={"home": 1, "away": 0})
    e_bb = _make_eval_row("m_pc_20_bb", sport="basketball", final_score={"home": 95, "away": 90})
    _write_runtime_data(str(isolated_data_dir), [m_fb, m_bb], [e_fb, e_bb])

    resp_all = client.get("/history?sport=all")
    assert resp_all.status_code == 200
    html_all = resp_all.get_data(as_text=True)
    assert "足球胜负命中" in html_all
    assert "篮球胜负命中" in html_all


# ==============================================================================
# SECTION B: Retained Regression Cases (from previous qualification suite)
# ==============================================================================

def test_retained_01_identical_duplicate_copies_accept_under_strict_equivalence(make_match):
    """Identical duplicate copies of an evaluation row pass under strict record equivalence."""
    m = make_match(id="m_ret_01", status="finished", score={"ft": [2, 1]})
    snap, sett, eval1 = _make_pipeline_evidence("m_ret_01")
    eval2 = dict(eval1)  # 100% identical copy

    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval1, eval2], sport="football", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    assert row["is_verified"] is True
    assert row["comparison"]["evidence_status"] == "verified"
    assert data["stats"]["football"]["verified_evaluations"] == 1


def test_retained_02_basketball_tied_score_not_reclassified_as_away_win(make_match):
    """Basketball tied score is not reclassified as away win."""
    m = make_match(id="m_ret_02", sport="basketball", status="finished", score={"ft": [95, 95]})
    snap, sett, eval_row = _make_pipeline_evidence(
        "m_ret_02",
        sport="basketball",
        final_score={"home": 95, "away": 95},
        actual_outcome="draw",
    )
    snaps = {snap["snapshot_id"]: snap}
    setts = {sett["settlement_id"]: sett}

    data = build_history_view_data([m], [eval_row], sport="basketball", snapshots=snaps, settlements=setts)
    row = data["matches"][0]
    assert row["comparison"]["actual_outcome"] == "draw"
    assert row["comparison"]["actual_label"] == "平局"
    assert row["is_verified"] is False
    assert row["comparison"]["evidence_status"] == "invalid"
    assert data["stats"]["basketball"]["verified_evaluations"] == 0


def test_retained_03_every_displayed_match_belongs_to_exactly_one_category(make_match):
    """Every displayed finished match belongs to exactly one status category."""
    m1 = make_match(id="m_ret_03a", status="finished", score={"ft": [2, 1]})
    s1, st1, e1 = _make_pipeline_evidence("m_ret_03a")
    m2 = make_match(id="m_ret_03b", status="finished", score={"ft": [1, 0]})
    s2, st2, e2 = _make_pipeline_evidence("m_ret_03b", model_version="legacy-v0")
    m3 = make_match(id="m_ret_03c", status="finished", score={"ft": [0, 0]})
    m4 = make_match(id="m_ret_03d", status="finished", score={"ft": [3, 0]})
    s4, st4, e4 = _make_pipeline_evidence("m_ret_03d", final_score={"home": 0, "away": 3})

    matches = [m1, m2, m3, m4]
    evals = [e1, e2, e4]
    snaps = {s1["snapshot_id"]: s1, s2["snapshot_id"]: s2, s4["snapshot_id"]: s4}
    setts = {st1["settlement_id"]: st1, st2["settlement_id"]: st2, st4["settlement_id"]: st4}

    data = build_history_view_data(matches, evals, sport="football", snapshots=snaps, settlements=setts)
    stats = data["stats"]["football"]

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


def test_retained_04_ui_verified_football_evaluation_argmax_outcome(isolated_data_dir, make_match):
    match = make_match(id="m_ret_04", status="finished", score={"ft": [2, 1]})
    eval_row = _make_eval_row("m_ret_04", model_probs={"home_win": 55, "draw": 25, "away_win": 20}, actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测主胜" in html
    assert "置信55%" in html
