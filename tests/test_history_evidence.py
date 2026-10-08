"""Unit tests for Historical Review immutable evidence presentation.
===================================================================
Verifies that:
- Predictions and accuracy are derived strictly from immutable evaluation rows.
- /history never invokes predict_match(...) or recomputes after kickoff.
- Finished matches without snapshots remain visible but excluded from accuracy.
- Frozen top-score coverage uses stored candidate sets.
- Denominators are consistent across sport filters and row limits.
"""
from __future__ import annotations

import json
import os
import pytest

import config


def _write_runtime_data(data_dir: str, matches: list[dict], eval_rows: list[dict] | None = None) -> None:
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "daily_matches.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": {}, "matches": matches}, fh, ensure_ascii=False)

    if eval_rows is not None:
        row_dict = {r["evaluation_id"]: r for r in eval_rows}
        with open(os.path.join(data_dir, config.EVALUATION_ROW_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "rows": row_dict}, fh, ensure_ascii=False)


def _make_eval_row(
    match_id: str,
    sport: str = "football",
    model_version: str | None = None,
    model_probs: dict | None = None,
    actual_outcome: str = "home_win",
    final_score: dict | None = None,
    top_scores: list[dict] | None = None,
) -> dict:
    if model_version is None:
        model_version = config.MODEL_VERSIONS.get(sport, "football-coldstart-1")
    if model_probs is None:
        model_probs = {"home_win": 55, "draw": 25, "away_win": 20} if sport == "football" else {"home_win": 65, "away_win": 35}
    if final_score is None:
        final_score = {"home": 2, "away": 1}

    eval_id = f"eval-{match_id}-{model_version}"
    expected_score_data = {}
    if top_scores is not None:
        expected_score_data["top_scores"] = top_scores

    return {
        "evaluation_id": eval_id,
        "snapshot_id": f"snap-{match_id}",
        "settlement_id": f"set-{match_id}",
        "match_id": match_id,
        "sport": sport,
        "league": "英超" if sport == "football" else "NBA",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": "elo-poisson-dixon-coles" if sport == "football" else "elo-normal-points",
        "model_version": model_version,
        "prediction_generated_at": "2026-10-01T12:00:00+08:00",
        "kickoff_at": "2026-10-01T20:00:00+08:00",
        "model_probabilities": model_probs,
        "final_score": final_score,
        "actual_outcome": actual_outcome,
        "expected_score_data": expected_score_data,
    }


def test_1_verified_football_evaluation_argmax_outcome(isolated_data_dir, make_match):
    match = make_match(id="m1", status="finished", score={"ft": [2, 1]})
    eval_row = _make_eval_row("m1", model_probs={"home_win": 55, "draw": 25, "away_win": 20}, actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测主胜" in html
    assert "置信55%" in html


def test_2_route_never_calls_predict_match(isolated_data_dir, monkeypatch, make_match):
    match = make_match(id="m2", status="finished", score={"ft": [2, 1]})
    eval_row = _make_eval_row("m2", actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import models.predictor
    def mock_predict(*args, **kwargs):
        raise RuntimeError("CRITICAL: predict_match was called by /history!")

    monkeypatch.setattr(models.predictor, "predict_match", mock_predict)

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200


def test_3_original_prematch_confidence_displayed(isolated_data_dir, make_match):
    match = make_match(id="m3", status="finished", score={"ft": [0, 2]})
    eval_row = _make_eval_row("m3", model_probs={"home_win": 20, "draw": 30, "away_win": 50}, actual_outcome="away_win")
    _write_runtime_data(str(isolated_data_dir), [match], [eval_row])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "置信50%" in html
    assert "预测客胜" in html


def test_4_evaluation_accuracy_matches_probability_argmax(isolated_data_dir, make_match):
    # 2 correct (m4a, m4b) + 1 incorrect (m4c) -> 2/3 = 66.7%
    m1 = make_match(id="m4a", status="finished", score={"ft": [1, 0]})
    e1 = _make_eval_row("m4a", model_probs={"home_win": 60, "draw": 20, "away_win": 20}, actual_outcome="home_win")

    m2 = make_match(id="m4b", status="finished", score={"ft": [0, 0]})
    e2 = _make_eval_row("m4b", model_probs={"home_win": 20, "draw": 50, "away_win": 30}, actual_outcome="draw")

    m3 = make_match(id="m4c", status="finished", score={"ft": [0, 1]})
    e3 = _make_eval_row("m4c", model_probs={"home_win": 50, "draw": 30, "away_win": 20}, actual_outcome="away_win")

    _write_runtime_data(str(isolated_data_dir), [m1, m2, m3], [e1, e2, e3])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "66.7%" in html
    assert "(2/3)" in html


def test_5_finished_match_without_prematch_snapshot_remains_visible(isolated_data_dir, make_match):
    m_no_snap = make_match(id="m5_unverified", status="finished", score={"ft": [3, 2]}, home="阿森纳", away="切尔西")
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


def test_6_missing_snapshot_does_not_enter_verified_denominator(isolated_data_dir, make_match):
    # 1 verified match (hit) + 1 unverified match -> denominator must be 1, accuracy 100.0% (1/1)
    m1 = make_match(id="m6a", status="finished", score={"ft": [1, 0]})
    e1 = _make_eval_row("m6a", model_probs={"home_win": 60, "draw": 20, "away_win": 20}, actual_outcome="home_win")

    m2 = make_match(id="m6b", status="finished", score={"ft": [2, 2]}) # No evaluation

    _write_runtime_data(str(isolated_data_dir), [m1, m2], [e1])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "100.0%" in html
    assert "(1/1)" in html
    assert "1场无证据" in html


def test_7_missing_settlement_does_not_fabricate_evaluation(isolated_data_dir, make_match):
    # Match exists in daily_matches, but evaluation row is missing
    m = make_match(id="m7", status="finished", score={"ft": [1, 1]})
    _write_runtime_data(str(isolated_data_dir), [m], [])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "无赛前验证预测" in html
    assert "0/0" in html


def test_8_historical_model_versions_separately_labeled(isolated_data_dir, make_match):
    m = make_match(id="m8", status="finished", score={"ft": [2, 0]})
    e_hist = _make_eval_row("m8", model_version="football-legacy-v0", actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [m], [e_hist])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "历史版本 (football-legacy-v0)" in html
    # Current model verified evaluations count must be 0
    assert "0/0" in html


def test_9_ambiguous_evidence_excluded_from_denominator(isolated_data_dir, make_match):
    m = make_match(id="m9", status="finished", score={"ft": [1, 0]})
    # Two conflicting rows for m9
    e1 = _make_eval_row("m9", model_version=config.MODEL_VERSIONS["football"], model_probs={"home_win": 70, "draw": 20, "away_win": 10})
    e2 = _make_eval_row("m9", model_version=config.MODEL_VERSIONS["football"], model_probs={"home_win": 20, "draw": 20, "away_win": 60})
    e2["evaluation_id"] = "eval-m9-conflict"

    _write_runtime_data(str(isolated_data_dir), [m], [e1, e2])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "证据冲突 / 排除" in html
    assert "(0/0)" in html


def test_10_football_draw_outcomes_included_correctly(isolated_data_dir, make_match):
    m = make_match(id="m10", status="finished", score={"ft": [1, 1]})
    e = _make_eval_row("m10", model_probs={"home_win": 25, "draw": 50, "away_win": 25}, actual_outcome="draw")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测平局" in html
    assert "100.0%" in html


def test_11_football_away_wins_included_correctly(isolated_data_dir, make_match):
    m = make_match(id="m11", status="finished", score={"ft": [1, 3]})
    e = _make_eval_row("m11", model_probs={"home_win": 20, "draw": 20, "away_win": 60}, actual_outcome="away_win")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "✓ 命中" in html
    assert "预测客胜" in html
    assert "100.0%" in html


def test_12_basketball_filtering_does_not_apply_three_way_rules(isolated_data_dir, make_match):
    m_bb = make_match(id="m12_bb", sport="basketball", status="finished", score={"ft": [102, 98]})
    e_bb = _make_eval_row(
        "m12_bb",
        sport="basketball",
        model_version=config.MODEL_VERSIONS["basketball"],
        model_probs={"home_win": 65, "away_win": 35},
        actual_outcome="home_win",
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
    assert "不适用" in html # Score coverage not applicable to basketball


def test_13_football_frozen_top_score_coverage_uses_stored_candidates(isolated_data_dir, make_match):
    # Match 1: 2-1 (candidate hit)
    m1 = make_match(id="m13a", status="finished", score={"ft": [2, 1]})
    e1 = _make_eval_row(
        "m13a",
        actual_outcome="home_win",
        final_score={"home": 2, "away": 1},
        top_scores=[{"score": "2-1"}, {"score": "1-0"}, {"score": "1-1"}],
    )
    # Match 2: 3-0 (candidate miss)
    m2 = make_match(id="m13b", status="finished", score={"ft": [3, 0]})
    e2 = _make_eval_row(
        "m13b",
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


def test_14_missing_top_score_candidates_unavailable_not_wrong(isolated_data_dir, make_match):
    m = make_match(id="m14", status="finished", score={"ft": [2, 0]})
    e = _make_eval_row("m14", actual_outcome="home_win", top_scores=[]) # Empty top scores
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Score coverage denominator is 0, so shows 暂无
    assert "(0/0)" in html


def test_15_mixed_sport_and_empty_evidence_views_render(isolated_data_dir):
    _write_runtime_data(str(isolated_data_dir), [], [])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "暂无历史赛果" in html


def test_16_repeated_get_requests_are_deterministic(isolated_data_dir, make_match):
    m = make_match(id="m16", status="finished", score={"ft": [2, 1]})
    e = _make_eval_row("m16", actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    res1 = client.get("/history").get_data(as_text=True)
    res2 = client.get("/history").get_data(as_text=True)
    assert res1 == res2


def test_17_no_runtime_files_written_by_history_requests(isolated_data_dir, make_match):
    m = make_match(id="m17", status="finished", score={"ft": [1, 0]})
    e = _make_eval_row("m17", actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    files_before = set(os.listdir(str(isolated_data_dir)))

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    assert resp.status_code == 200

    files_after = set(os.listdir(str(isolated_data_dir)))
    assert files_before == files_after


def test_18_pagination_200_row_limit_and_denominator_consistency(isolated_data_dir, make_match):
    # Create 205 finished matches
    matches = []
    evals = []
    for i in range(205):
        mid = f"m18_{i:03d}"
        m = make_match(id=mid, status="finished", score={"ft": [1, 0]}, date=f"2026-09-{(i % 28)+1:02d}")
        e = _make_eval_row(mid, actual_outcome="home_win")
        matches.append(m)
        evals.append(e)

    _write_runtime_data(str(isolated_data_dir), matches, evals)

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history?sport=football")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Visible matches capped at 200, so denominator is 200
    assert "(200/200)" in html
    assert "共 205 场完赛" in html


def test_19_frontend_filters_work(isolated_data_dir, make_match):
    m_fb = make_match(id="m19_fb", sport="football", status="finished", score={"ft": [1, 0]})
    m_bb = make_match(id="m19_bb", sport="basketball", status="finished", score={"ft": [95, 90]})
    e_fb = _make_eval_row("m19_fb", sport="football")
    e_bb = _make_eval_row("m19_bb", sport="basketball")

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


def test_20_historical_rows_cannot_be_recomputed_postmatch(isolated_data_dir, make_match):
    # Stored pre-match prediction was away_win (incorrect)
    m = make_match(id="m20", status="finished", score={"ft": [2, 0]}, home="主队", away="客队")
    e = _make_eval_row("m20", model_probs={"home_win": 20, "draw": 20, "away_win": 60}, actual_outcome="home_win")
    _write_runtime_data(str(isolated_data_dir), [m], [e])

    import app as app_module
    client = app_module.app.test_client()
    resp = client.get("/history")
    html = resp.get_data(as_text=True)

    # Must reflect frozen snapshot error (away_win vs home_win -> 偏差), NOT a recomputed hit
    assert "✗ 偏差" in html
    assert "预测客胜" in html
