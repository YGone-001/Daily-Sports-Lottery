# -*- coding: utf-8 -*-
"""Football V2 shadow candidate: causality, determinism, boundary and purity tests."""
from __future__ import annotations

import math

import pytest

from models.football_model_v2 import (
    GLOBAL_TOTAL_GOALS_PRIOR,
    LAPLACE_ALPHA,
    MODEL_NAME,
    MODEL_VERSION,
    league_environment_prior,
    predict_v2_probabilities,
    prepare_walk_forward_rows,
    walk_forward_v2,
)


def _row(mid, league="联赛A", gen="2026-01-05T10:00:00+08:00", settled="2026-01-06T22:00:00+08:00",
         total=3.0, lam_h=1.4, lam_a=1.2, actual="home_win"):
    return {
        "match_id": mid, "league": league,
        "prediction_generated_at": gen, "settled_at": settled,
        "total_goals": total, "lambda_home": lam_h, "lambda_away": lam_a,
        "actual_outcome": actual,
        "model_probabilities": {"home_win": 40, "draw": 28, "away_win": 32},
        "final_score": {"home": 2, "away": 1},
    }


def test_prior_causality_only_uses_rows_settled_before_as_of():
    history = [
        _row("m1", settled="2026-01-06T22:00:00+08:00", total=2.0),
        _row("m2", settled="2026-01-07T22:00:00+08:00", total=6.0),
    ]
    prior = league_environment_prior(history, "联赛A")
    # 只有两行都"已结算"才进入观测
    assert prior["observed_matches"] == 2
    assert math.isclose(prior["smoothed_prior_total_goals"],
                        (2.0 + 6.0 + LAPLACE_ALPHA * GLOBAL_TOTAL_GOALS_PRIOR) / (2 + LAPLACE_ALPHA))


def test_prior_shrinks_to_global_when_empty():
    prior = league_environment_prior([], "联赛B")
    assert prior["observed_matches"] == 0
    assert math.isclose(prior["smoothed_prior_total_goals"], GLOBAL_TOTAL_GOALS_PRIOR)


def test_predict_scales_total_toward_prior_and_keeps_ratio():
    v2 = predict_v2_probabilities(1.4, 1.2, 2.6)
    assert math.isclose(v2["scaled_lambda_home"] + v2["scaled_lambda_away"], 2.6, rel_tol=1e-9)
    ratio_before = 1.4 / 1.2
    ratio_after = v2["scaled_lambda_home"] / v2["scaled_lambda_away"]
    assert math.isclose(ratio_before, ratio_after, rel_tol=1e-9)
    s = v2["home_win"] + v2["draw"] + v2["away_win"]
    assert abs(s - 1.0) < 1e-9
    assert v2["home_win"] > 0 and v2["draw"] > 0 and v2["away_win"] > 0


def test_predict_scale_factor_is_bounded():
    extreme = predict_v2_probabilities(1.4, 1.2, 100.0)
    assert extreme["scale_factor"] <= 2.0
    tiny = predict_v2_probabilities(1.4, 1.2, 0.01)
    assert tiny["scale_factor"] >= 0.5


def test_probabilities_are_deterministic_and_odds_free():
    a = predict_v2_probabilities(1.4, 1.2, 2.7)
    b = predict_v2_probabilities(1.4, 1.2, 2.7)
    assert a == b
    # 模块不包含任何赔率输入：签名层面没有 odds 参数（纯模型边界）
    import inspect
    assert "odds" not in inspect.signature(predict_v2_probabilities).parameters


def test_prepare_walk_forward_excludes_rows_with_missing_features():
    rows = [_row("m1"), {"match_id": "m2", "league": "联赛A"},  # 缺 λ/时间
            _row("m3", actual="weird")]
    prepared, excluded = prepare_walk_forward_rows(rows)
    assert len(prepared) == 1 and excluded == 2


def test_walk_forward_never_uses_self_or_future_results():
    rows = [
        _row("m1", gen="2026-01-05T10:00:00+08:00", settled="2026-01-06T22:00:00+08:00"),
        _row("m2", gen="2026-01-05T11:00:00+08:00", settled="2026-01-06T23:00:00+08:00"),
        _row("m3", gen="2026-01-08T10:00:00+08:00", settled="2026-01-09T22:00:00+08:00"),
    ]
    totals = {"m1": 2.0, "m2": 4.0, "m3": 3.0}
    results = walk_forward_v2(rows, final_score_totals=totals)
    by_id = {r["match_id"]: r for r in results}
    # m1 / m2 预测时无任何已结算历史
    assert by_id["m1"]["prior_observed_matches"] == 0
    assert by_id["m2"]["prior_observed_matches"] == 0
    # m3 只能看到 m1、m2（两者都在 m3 预测前结算）
    assert by_id["m3"]["prior_observed_matches"] == 2
    # 顺序无关性
    results_rev = walk_forward_v2(list(reversed(rows)), final_score_totals=totals)
    assert {r["match_id"]: r["prior_observed_matches"] for r in results_rev} == \
        {r["match_id"]: r["prior_observed_matches"] for r in results}


def test_walk_forward_excludes_rows_without_final_scores_from_prior():
    rows = [
        _row("m1", gen="2026-01-05T10:00:00+08:00", settled="2026-01-06T22:00:00+08:00"),
        _row("m2", gen="2026-01-08T10:00:00+08:00", settled="2026-01-09T22:00:00+08:00"),
    ]
    # m1 没有真实总进球：绝不入池
    results = walk_forward_v2(rows, final_score_totals={})
    assert results[1]["prior_observed_matches"] == 0


def test_shadow_version_namespace_is_isolated():
    assert MODEL_VERSION == "football-v2-league-env-shadow"
    assert MODEL_VERSION not in ("football-coldstart-1", "basketball-margin-1")
    import config
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"
