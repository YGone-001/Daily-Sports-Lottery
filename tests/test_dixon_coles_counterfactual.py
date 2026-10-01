"""Dixon-Coles 反事实 W/D/L 验证的行为测试（纯计算 + 读取层）。"""
from __future__ import annotations

import copy
import math

import pytest

import config
from models.poisson_model import score_matrix, win_draw_loss
from models.predictor import predict_football
from utils.classification_evaluation import LOG_EPSILON
from utils.dixon_coles_counterfactual import (
    CLASS_ORDER,
    DixonColesCounterfactualError,
    build_counterfactual_summaries,
    counterfactual_wdl_probabilities,
    multiclass_brier,
    multiclass_log_loss,
    validate_all_counterfactual,
    validate_counterfactual_group,
)
from utils.dixon_coles_fitting import DixonColesFittingError
from utils.dixon_coles_walkforward import (
    DixonColesWalkForwardError,
    validate_rho_walk_forward_group,
)

MODEL_NAME = "elo-poisson-dixon-coles"
K1 = "2030-01-01T20:00:00+08:00"
K2 = "2030-02-01T20:00:00+08:00"
K3 = "2030-03-01T20:00:00+08:00"
K4 = "2030-04-01T20:00:00+08:00"


def _row(
    evaluation_id: str,
    *,
    kickoff_at: str | None = K1,
    lambda_home: float = 1.2,
    lambda_away: float = 0.8,
    home_goals: int = 1,
    away_goals: int = 0,
    sport: str = "football",
    model_name: str = MODEL_NAME,
    model_version: str = "football-ad-1",
    actual_outcome: str | None = None,
    **extra,
) -> dict:
    row = {
        "evaluation_id": evaluation_id,
        "match_id": f"m-{evaluation_id}",
        "sport": sport,
        "model_name": model_name,
        "model_version": model_version,
        "expected_score_data": {
            "expected_goals": {"home": lambda_home, "away": lambda_away},
        },
        "final_score": {"home": home_goals, "away": away_goals},
        "kickoff_at": kickoff_at,
    }
    if actual_outcome is not None:
        row["actual_outcome"] = actual_outcome
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 概率重建
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lambda_home,lambda_away,rho",
    [(1.2, 0.8, -0.15), (1.6, 1.1, 0.0), (0.9, 1.4, 0.12), (2.2, 1.9, -0.05)],
)
def test_counterfactual_probabilities_are_valid_distribution(
    isolated_data_dir, lambda_home, lambda_away, rho
):
    probabilities = counterfactual_wdl_probabilities(lambda_home, lambda_away, rho)

    assert set(probabilities) == set(CLASS_ORDER)
    assert math.isclose(sum(probabilities.values()), 1.0, rel_tol=1e-12)
    for value in probabilities.values():
        assert math.isfinite(value)
        assert 0.0 <= value <= 1.0


def test_rho_zero_matches_independent_poisson_matrix(isolated_data_dir):
    lambda_home, lambda_away = 1.3, 0.9

    probabilities = counterfactual_wdl_probabilities(lambda_home, lambda_away, 0.0)
    matrix = score_matrix(lambda_home, lambda_away, rho=0.0)
    home, draw, away = win_draw_loss(matrix)

    assert probabilities == {"home_win": home, "draw": draw, "away_win": away}


def test_rho_changes_distribution(isolated_data_dir):
    lambda_home, lambda_away = 1.2, 0.8

    negative = counterfactual_wdl_probabilities(lambda_home, lambda_away, -0.15)
    zero = counterfactual_wdl_probabilities(lambda_home, lambda_away, 0.0)
    positive = counterfactual_wdl_probabilities(lambda_home, lambda_away, 0.15)

    assert any(
        not math.isclose(negative[cls], zero[cls], rel_tol=1e-12) for cls in CLASS_ORDER
    )
    assert any(
        not math.isclose(positive[cls], zero[cls], rel_tol=1e-12) for cls in CLASS_ORDER
    )


def test_invalid_rho_rejected(isolated_data_dir):
    with pytest.raises(DixonColesCounterfactualError) as excinfo:
        counterfactual_wdl_probabilities(5.0, 5.0, -0.25)

    assert excinfo.value.reason == "invalid_rho"


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), None, True, "1.2"])
def test_invalid_lambdas_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesCounterfactualError) as excinfo:
        counterfactual_wdl_probabilities(bad, 0.8, -0.15)

    assert excinfo.value.reason == "invalid_expected_goals"


def test_fixed_rho_reconstruction_matches_predictor(isolated_data_dir):
    """
    强制回归：predictor 的期望进球 + 线上固定 rho -> 重建概率 -> 四舍五入
    必须精确等于 predictor 的 model_probabilities。
    """
    home = {"name": "Home", "elo_rating": 1650.0, "attack_rating": 0.60, "defense_rating": 0.55}
    away = {"name": "Away", "elo_rating": 1580.0, "attack_rating": 0.52, "defense_rating": 0.58}

    prediction = predict_football(home, away)   # odds=None -> 无市场融合、无大小球校准
    lambda_home = prediction["expected_goals"]["home"]
    lambda_away = prediction["expected_goals"]["away"]

    reconstructed = counterfactual_wdl_probabilities(
        lambda_home, lambda_away, config.MODEL_CONFIG["dixon_coles_rho"]
    )
    rounded = {cls: round(reconstructed[cls] * 100) for cls in CLASS_ORDER}

    assert rounded == prediction["model_probabilities"]


# ---------------------------------------------------------------------------
# 指标定义
# ---------------------------------------------------------------------------

def test_multiclass_brier_known_example(isolated_data_dir):
    probabilities = {"home_win": 0.60, "draw": 0.25, "away_win": 0.15}

    assert math.isclose(multiclass_brier(probabilities, "home_win"), 0.245, rel_tol=1e-12)


def test_multiclass_log_loss_known_example(isolated_data_dir):
    probabilities = {"home_win": 0.60, "draw": 0.25, "away_win": 0.15}

    assert math.isclose(
        multiclass_log_loss(probabilities, "home_win"), -math.log(0.60), rel_tol=1e-12
    )


def test_log_loss_epsilon_protection(isolated_data_dir):
    probabilities = {"home_win": 0.0, "draw": 0.5, "away_win": 0.5}
    before = dict(probabilities)

    loss = multiclass_log_loss(probabilities, "home_win")

    assert math.isclose(loss, -math.log(LOG_EPSILON), rel_tol=1e-12)
    assert math.isfinite(loss)
    assert probabilities == before   # 概率向量本身未被修改


# ---------------------------------------------------------------------------
# 实际结果一致性
# ---------------------------------------------------------------------------

def test_inconsistent_actual_outcome_rejected(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2, home_goals=2, away_goals=1, actual_outcome="away_win"),
    ]

    with pytest.raises(DixonColesCounterfactualError) as excinfo:
        validate_counterfactual_group(rows, min_train_rows=1)

    assert excinfo.value.reason == "inconsistent_actual_outcome"


@pytest.mark.parametrize(
    "home_goals,away_goals,expected",
    [(2, 1, "home_win"), (1, 1, "draw"), (0, 2, "away_win")],
)
def test_actual_outcome_derived_from_score(isolated_data_dir, home_goals, away_goals, expected):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2, home_goals=home_goals, away_goals=away_goals,
             actual_outcome=expected),
    ]

    summary = validate_counterfactual_group(rows, min_train_rows=1)

    assert summary["evaluation_count"] == 1   # 一致时正常通过


# ---------------------------------------------------------------------------
# 时序复用
# ---------------------------------------------------------------------------

def _chronology() -> list[dict]:
    return [
        _row("a1", kickoff_at=K1, lambda_home=1.10, lambda_away=0.95, home_goals=0, away_goals=0),
        _row("b1", kickoff_at=K2, lambda_home=1.30, lambda_away=0.75, home_goals=1, away_goals=0),
        _row("b2", kickoff_at=K2, lambda_home=1.20, lambda_away=0.85, home_goals=0, away_goals=1),
        _row("c1", kickoff_at=K3, lambda_home=1.40, lambda_away=1.05, home_goals=1, away_goals=1),
        _row("d1", kickoff_at=K4, lambda_home=1.55, lambda_away=0.90, home_goals=2, away_goals=0),
    ]


def test_target_set_equals_score_likelihood_walk_forward(isolated_data_dir):
    rows = _chronology()

    counterfactual = validate_counterfactual_group(rows, min_train_rows=1)
    nll = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert counterfactual["evaluation_count"] == nll["evaluation_count"]
    assert counterfactual["warmup_skipped_count"] == nll["warmup_skipped_count"]
    assert counterfactual["target_bucket_count"] == nll["target_bucket_count"]
    assert counterfactual["sample_count"] == nll["sample_count"]


def test_rho_path_equals_score_likelihood_walk_forward(isolated_data_dir):
    rows = _chronology()

    counterfactual = validate_counterfactual_group(rows, min_train_rows=1)
    nll = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert len(counterfactual["rho_path"]) == len(nll["rho_path"])
    for cf_entry, nll_entry in zip(counterfactual["rho_path"], nll["rho_path"]):
        assert cf_entry["target_kickoff_at"] == nll_entry["target_kickoff_at"]
        assert cf_entry["train_count"] == nll_entry["train_count"]
        assert cf_entry["test_count"] == nll_entry["test_count"]
        assert (
            cf_entry["training_reference_kickoff_at"]
            == nll_entry["training_reference_kickoff_at"]
        )
        assert cf_entry["fitted_rho"] == nll_entry["fitted_rho"]


def test_same_kickoff_shares_one_fit_but_two_metric_contributions(isolated_data_dir):
    rows = _chronology()

    summary = validate_counterfactual_group(rows, min_train_rows=1)
    entry = summary["rho_path"][0]   # K1 桶预热跳过，首个合格桶即 K2（含 b1 与 b2）

    assert entry["target_kickoff_at"] == K2
    assert entry["test_count"] == 2
    assert entry["train_count"] == 1

    # 桶合计 = 两行各自贡献之和（用同样 rho 单独计算核对）
    rho = entry["fitted_rho"]
    manual_brier = 0.0
    manual_log_loss = 0.0
    for lambda_home, lambda_away, home_goals, away_goals in [
        (1.30, 0.75, 1, 0),
        (1.20, 0.85, 0, 1),
    ]:
        probabilities = counterfactual_wdl_probabilities(lambda_home, lambda_away, rho)
        actual = "home_win" if home_goals > away_goals else (
            "draw" if home_goals == away_goals else "away_win"
        )
        manual_brier += multiclass_brier(probabilities, actual)
        manual_log_loss += multiclass_log_loss(probabilities, actual)

    assert math.isclose(entry["walk_forward_fitted_brier"], manual_brier, rel_tol=1e-12)
    assert math.isclose(
        entry["walk_forward_fitted_log_loss"], manual_log_loss, rel_tol=1e-12
    )


def test_input_order_independence(isolated_data_dir):
    rows = _chronology()

    forward = validate_counterfactual_group(rows, min_train_rows=1)
    backward = validate_counterfactual_group(list(reversed(rows)), min_train_rows=1)

    assert forward == backward


# ---------------------------------------------------------------------------
# 聚合定义
# ---------------------------------------------------------------------------

def test_aggregate_definitions(isolated_data_dir):
    rows = _chronology()

    summary = validate_counterfactual_group(rows, min_train_rows=1)
    path = summary["rho_path"]
    count = summary["evaluation_count"]

    assert summary["walk_forward_fitted_total_brier"] == sum(
        e["walk_forward_fitted_brier"] for e in path
    )
    assert summary["fixed_rho_total_brier"] == sum(e["fixed_rho_brier"] for e in path)
    assert summary["rho_zero_total_brier"] == sum(e["rho_zero_brier"] for e in path)
    assert summary["walk_forward_fitted_total_log_loss"] == sum(
        e["walk_forward_fitted_log_loss"] for e in path
    )

    assert math.isclose(
        summary["walk_forward_fitted_mean_brier"],
        summary["walk_forward_fitted_total_brier"] / count,
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["fixed_rho_mean_log_loss"],
        summary["fixed_rho_total_log_loss"] / count,
        rel_tol=1e-12,
    )

    assert math.isclose(
        summary["brier_improvement_vs_fixed"],
        summary["fixed_rho_total_brier"] - summary["walk_forward_fitted_total_brier"],
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["brier_improvement_vs_zero"],
        summary["rho_zero_total_brier"] - summary["walk_forward_fitted_total_brier"],
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["log_loss_improvement_vs_fixed"],
        summary["fixed_rho_total_log_loss"] - summary["walk_forward_fitted_total_log_loss"],
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["log_loss_improvement_vs_zero"],
        summary["rho_zero_total_log_loss"] - summary["walk_forward_fitted_total_log_loss"],
        rel_tol=1e-12,
    )


def test_fixed_comparison_rho_default_and_override(isolated_data_dir):
    rows = _chronology()

    default_summary = validate_counterfactual_group(rows, min_train_rows=1)
    assert default_summary["fixed_comparison_rho"] == config.MODEL_CONFIG["dixon_coles_rho"]
    assert default_summary["fixed_comparison_rho"] == -0.15

    override = validate_counterfactual_group(rows, min_train_rows=1, fixed_comparison_rho=-0.05)
    assert override["fixed_comparison_rho"] == -0.05
    assert config.MODEL_CONFIG["dixon_coles_rho"] == -0.15   # 配置未被修改


def test_no_eligible_targets_returns_none_aggregates(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    summary = validate_counterfactual_group(rows, min_train_rows=20)

    assert summary["evaluation_count"] == 0
    assert summary["target_bucket_count"] == 0
    assert summary["warmup_skipped_count"] == 2
    assert summary["rho_path"] == []

    for key in (
        "walk_forward_fitted_total_brier", "fixed_rho_total_brier", "rho_zero_total_brier",
        "walk_forward_fitted_mean_brier", "fixed_rho_mean_brier", "rho_zero_mean_brier",
        "walk_forward_fitted_total_log_loss", "fixed_rho_total_log_loss",
        "rho_zero_total_log_loss", "walk_forward_fitted_mean_log_loss",
        "fixed_rho_mean_log_loss", "rho_zero_mean_log_loss",
        "brier_improvement_vs_fixed", "brier_improvement_vs_zero",
        "log_loss_improvement_vs_fixed", "log_loss_improvement_vs_zero",
    ):
        assert summary[key] is None, key


def test_empty_dataset_returns_empty_list(isolated_data_dir):
    assert build_counterfactual_summaries([]) == []

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_counterfactual_group([])

    assert excinfo.value.reason == "empty_group"


# ---------------------------------------------------------------------------
# 分组与完整性
# ---------------------------------------------------------------------------

def test_version_isolation(isolated_data_dir):
    rows = _chronology() + [
        dict(r, model_version="baseline-1", evaluation_id="x-" + r["evaluation_id"])
        for r in _chronology()[:3]
    ]

    summaries = build_counterfactual_summaries(rows, min_train_rows=1)

    assert len(summaries) == 2
    assert [s["model_version"] for s in summaries] == ["baseline-1", "football-ad-1"]
    assert summaries[0]["sample_count"] == 3
    assert summaries[1]["sample_count"] == 5
    # baseline-1 只有 3 行：K1 桶预热跳过，仅 K2 桶合格
    assert [e["train_count"] for e in summaries[0]["rho_path"]] == [1]


def test_global_duplicate_id_rejected_before_grouping(isolated_data_dir):
    rows = [
        _row("dup", kickoff_at=K1, model_version="baseline-1"),
        _row("dup", kickoff_at=K2, model_version="football-ad-1"),
    ]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        build_counterfactual_summaries(rows, min_train_rows=1)

    assert excinfo.value.reason == "duplicate_evaluation_id"


def test_basketball_not_summarized_but_rejected_directly(isolated_data_dir):
    football = _row("f1", kickoff_at=K1)
    basketball = _row("bb1", kickoff_at=K1, sport="basketball",
                      model_name="elo-normal-points", model_version="baseline-1")

    assert build_counterfactual_summaries([football, basketball], min_train_rows=1) == (
        build_counterfactual_summaries([football], min_train_rows=1)
    )

    with pytest.raises(DixonColesFittingError) as excinfo:
        validate_counterfactual_group([basketball], min_train_rows=1)

    assert excinfo.value.reason == "unsupported_sport"


def test_summaries_are_deterministically_ordered(isolated_data_dir):
    rows = [
        _row("f2", kickoff_at=K1, model_version="model-v2"),
        _row("b1", kickoff_at=K1, model_version="baseline-1"),
        _row("f1", kickoff_at=K1, model_version="football-ad-1"),
    ]

    forward = build_counterfactual_summaries(rows, min_train_rows=1)
    backward = build_counterfactual_summaries(list(reversed(rows)), min_train_rows=1)

    assert forward == backward
    assert [s["model_version"] for s in forward] == ["baseline-1", "football-ad-1", "model-v2"]


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------

def test_same_instant_different_offsets_form_one_bucket(isolated_data_dir):
    rows = [
        _row("w", kickoff_at="2030-01-01T10:00:00+08:00"),
        _row("x", kickoff_at="2030-01-01T20:00:00+08:00"),
        _row("y", kickoff_at="2030-01-01T12:00:00+00:00"),   # 与 x 同一瞬间
        _row("z", kickoff_at=K2),
    ]

    summary = validate_counterfactual_group(rows, min_train_rows=1)

    first = summary["rho_path"][0]
    assert first["test_count"] == 2      # x 与 y 同桶
    assert first["train_count"] == 1     # 只有 w
    # 桶键取排序后首行（x 与 y 时刻相等，按 evaluation_id 稳定排序）
    assert first["target_kickoff_at"] == "2030-01-01T20:00:00+08:00"


def test_mixed_naive_and_aware_rejected(isolated_data_dir):
    rows = [
        _row("naive", kickoff_at="2030-01-01T20:00:00"),
        _row("aware", kickoff_at=K2),
    ]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_counterfactual_group(rows, min_train_rows=1)

    assert excinfo.value.reason == "inconsistent_kickoff_timezone"


# ---------------------------------------------------------------------------
# 隔离性
# ---------------------------------------------------------------------------

def test_market_and_display_fields_isolated(isolated_data_dir):
    rows = _chronology()
    noisy = [
        dict(
            r,
            market_odds={"home_win": 1.01, "draw": 30.0, "away_win": 50.0},
            market_implied_probabilities={"home_win": 98, "draw": 1, "away_win": 1},
            display_probabilities={"home_win": 5, "draw": 90, "away_win": 5},
            expected_values={"home": {"ev": 99.9, "is_value": True, "kelly_pct": 50.0}},
        )
        for r in _chronology()
    ]

    assert validate_counterfactual_group(noisy, min_train_rows=1) == (
        validate_counterfactual_group(rows, min_train_rows=1)
    )


def test_stored_model_probabilities_isolated(isolated_data_dir):
    rows = _chronology()
    noisy = [
        dict(r, model_probabilities={"home_win": 90, "draw": 5, "away_win": 5})
        for r in _chronology()
    ]

    assert validate_counterfactual_group(noisy, min_train_rows=1) == (
        validate_counterfactual_group(rows, min_train_rows=1)
    )


def test_input_rows_not_mutated(isolated_data_dir):
    rows = _chronology()
    before = copy.deepcopy(rows)

    validate_counterfactual_group(rows, min_train_rows=1)
    build_counterfactual_summaries(rows, min_train_rows=1)

    assert rows == before


def test_repeatability(isolated_data_dir):
    rows = _chronology()

    assert validate_counterfactual_group(rows, min_train_rows=1) == (
        validate_counterfactual_group(rows, min_train_rows=1)
    )


def test_summary_schema_and_no_ranking_labels(isolated_data_dir):
    summary = validate_counterfactual_group(_chronology(), min_train_rows=1)

    assert set(summary) == {
        "sport", "model_name", "model_version", "sample_count", "min_train_rows",
        "warmup_skipped_count", "evaluation_count", "target_bucket_count",
        "half_life_days", "rho_grid", "fixed_comparison_rho",
        "walk_forward_fitted_total_brier", "fixed_rho_total_brier", "rho_zero_total_brier",
        "walk_forward_fitted_mean_brier", "fixed_rho_mean_brier", "rho_zero_mean_brier",
        "walk_forward_fitted_total_log_loss", "fixed_rho_total_log_loss",
        "rho_zero_total_log_loss", "walk_forward_fitted_mean_log_loss",
        "fixed_rho_mean_log_loss", "rho_zero_mean_log_loss",
        "brier_improvement_vs_fixed", "brier_improvement_vs_zero",
        "log_loss_improvement_vs_fixed", "log_loss_improvement_vs_zero",
        "rho_path",
    }
    assert set(summary["rho_path"][0]) == {
        "target_kickoff_at", "train_count", "test_count",
        "training_reference_kickoff_at", "fitted_rho",
        "walk_forward_fitted_brier", "fixed_rho_brier", "rho_zero_brier",
        "walk_forward_fitted_log_loss", "fixed_rho_log_loss", "rho_zero_log_loss",
    }

    for forbidden in (
        "winner", "best", "rank", "grade", "deployment_status", "recommended_rho",
        "accuracy", "expected_calibration_error", "ece",
    ):
        assert forbidden not in summary


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def test_convenience_function_matches_direct_builder(isolated_data_dir, make_match):
    from utils.daily_loader import enrich_match
    from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
    from utils.prediction_snapshots import capture_snapshot
    from utils.settlements import settle_snapshot

    for index, date in enumerate(("2030-06-01", "2030-06-02"), start=1):
        match = enrich_match(make_match(id=f"m-cf-{index}", date=date))
        snapshot, created = capture_snapshot(match)
        assert created is True
        settlement, settled = settle_snapshot(
            snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                           score={"ft": [2, 1]})
        )
        assert settled is True
        capture_evaluation_row(snapshot, settlement)

    summaries = validate_all_counterfactual(min_train_rows=1)

    assert summaries == build_counterfactual_summaries(
        get_all_evaluation_rows(), min_train_rows=1
    )
    assert len(summaries) == 1
    assert summaries[0]["sport"] == "football"
    assert summaries[0]["model_version"] == "football-ad-1"
    assert summaries[0]["evaluation_count"] == 1
