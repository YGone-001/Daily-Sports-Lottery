"""Dixon-Coles 走查验证的行为测试（纯计算 + 读取层）。"""
from __future__ import annotations

import copy
import math

import pytest

import config
from utils.dixon_coles_fitting import (
    DixonColesFittingError,
    observation_probability,
)
from utils.dixon_coles_walkforward import (
    DEFAULT_MIN_TRAIN_ROWS,
    DixonColesWalkForwardError,
    build_rho_walk_forward_summaries,
    validate_all_rho_walk_forward,
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
    row.update(extra)
    return row


def _path(summary: dict) -> list[dict]:
    return summary["rho_path"]


# ---------------------------------------------------------------------------
# 无泄漏时序
# ---------------------------------------------------------------------------

def test_strict_past_only_training(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert summary["warmup_skipped_count"] == 1        # A 无训练数据
    assert summary["evaluation_count"] == 2
    assert summary["target_bucket_count"] == 2

    first, second = _path(summary)
    assert first["target_kickoff_at"] == K2
    assert first["train_count"] == 1                   # 只有 A
    assert second["target_kickoff_at"] == K3
    assert second["train_count"] == 2                  # A + B，C 不在自己的训练集里


def test_same_kickoff_leakage_protection(isolated_data_dir):
    """同一开赛时刻的多场比赛共用一个「仅由更早行」拟合出的 rho。"""
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K2),
        _row("D", kickoff_at=K3),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert summary["target_bucket_count"] == 2
    assert summary["evaluation_count"] == 3

    shared, later = _path(summary)
    assert shared["target_kickoff_at"] == K2
    assert shared["train_count"] == 1                  # 只有 A
    assert shared["test_count"] == 2                   # B 与 C 共用一个拟合
    assert later["target_kickoff_at"] == K3
    assert later["train_count"] == 3                   # A + B + C，D 不训练自己


def test_expanding_window_uses_all_strictly_earlier_rows(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3),
        _row("D", kickoff_at=K4),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    counts = [entry["train_count"] for entry in _path(summary)]
    assert counts == [1, 2, 3]                         # 扩张窗口，不是固定滚动窗口


def test_reference_time_matches_latest_earlier_kickoff(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    first, second = _path(summary)
    assert first["training_reference_kickoff_at"] == K1
    assert second["training_reference_kickoff_at"] == K2


def test_per_bucket_refit_changes_rho_path(isolated_data_dir):
    """不同桶的训练集不同 -> 拟合 rho 会随时间变化，而不是一次性全量拟合后复用。"""
    rows = (
        [_row(f"z{i}", kickoff_at=K1, lambda_home=1.0, lambda_away=1.0,
              home_goals=0, away_goals=0) for i in range(11)]
        + [_row(f"o{i}", kickoff_at=K1, lambda_home=1.0, lambda_away=1.0,
                home_goals=1, away_goals=0) for i in range(9)]
        + [_row(f"p{i}", kickoff_at=K2, lambda_home=1.0, lambda_away=1.0,
                home_goals=1, away_goals=0) for i in range(20)]
        + [_row("q", kickoff_at=K3, lambda_home=1.0, lambda_away=1.0,
                home_goals=1, away_goals=0)]
    )

    summary = validate_rho_walk_forward_group(rows, min_train_rows=20)

    assert summary["warmup_skipped_count"] == 20
    assert summary["evaluation_count"] == 21
    assert summary["target_bucket_count"] == 2

    first, second = _path(summary)
    # 第一个桶训练集 = 11x0-0 + 9x1-0 -> 解析最优 -0.10
    assert math.isclose(first["fitted_rho"], -0.10, abs_tol=1e-9)
    # 第二个桶训练集再加入 20 场 1-0 -> 最优推到网格上界
    assert second["fitted_rho"] == 0.25
    assert first["fitted_rho"] != second["fitted_rho"]
    assert second["train_count"] == 40


def test_input_order_independence(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K2, home_goals=0, away_goals=1),
        _row("D", kickoff_at=K3, home_goals=0, away_goals=0),
        _row("E", kickoff_at=K4, home_goals=2, away_goals=2),
    ]

    forward = validate_rho_walk_forward_group(rows, min_train_rows=1)
    backward = validate_rho_walk_forward_group(list(reversed(rows)), min_train_rows=1)

    assert forward == backward


# ---------------------------------------------------------------------------
# 预热与空结果
# ---------------------------------------------------------------------------

def test_warmup_skips_early_buckets(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=2)

    assert summary["warmup_skipped_count"] == 2        # A、B 桶训练不足
    assert summary["evaluation_count"] == 1
    assert summary["target_bucket_count"] == 1
    assert _path(summary)[0]["target_kickoff_at"] == K3


def test_no_eligible_targets_returns_none_aggregates(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=DEFAULT_MIN_TRAIN_ROWS)

    assert summary["evaluation_count"] == 0
    assert summary["target_bucket_count"] == 0
    assert summary["warmup_skipped_count"] == 2
    assert summary["walk_forward_fitted_total_nll"] is None
    assert summary["fixed_rho_total_nll"] is None
    assert summary["rho_zero_total_nll"] is None
    assert summary["walk_forward_fitted_mean_nll"] is None
    assert summary["fixed_rho_mean_nll"] is None
    assert summary["rho_zero_mean_nll"] is None
    assert summary["nll_improvement_vs_fixed"] is None
    assert summary["nll_improvement_vs_zero"] is None
    assert summary["rho_path"] == []

    for value in (
        summary["walk_forward_fitted_mean_nll"],
        summary["nll_improvement_vs_fixed"],
    ):
        assert value is None  # 不是 NaN


def test_empty_dataset_returns_empty_list(isolated_data_dir):
    assert build_rho_walk_forward_summaries([]) == []

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group([])

    assert excinfo.value.reason == "empty_group"


# ---------------------------------------------------------------------------
# 目标打分
# ---------------------------------------------------------------------------

def test_target_scored_with_unit_weight(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2, lambda_home=1.4, lambda_away=1.1)]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)
    entry = _path(summary)[0]

    observation = (1.4, 1.1, 1, 0, 1.0)
    expected = -math.log(observation_probability(observation, entry["fitted_rho"]))

    assert math.isclose(entry["fitted_test_nll"], expected, rel_tol=1e-12)


def test_fixed_comparison_rho_defaults_to_configuration(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert summary["fixed_comparison_rho"] == config.MODEL_CONFIG["dixon_coles_rho"]
    assert summary["fixed_comparison_rho"] == -0.15


def test_explicit_fixed_rho_override(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    summary = validate_rho_walk_forward_group(
        rows, min_train_rows=1, fixed_comparison_rho=-0.05
    )

    assert summary["fixed_comparison_rho"] == -0.05
    # 配置本身未被修改
    assert config.MODEL_CONFIG["dixon_coles_rho"] == -0.15


def test_invalid_fixed_rho_rejected(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group(rows, min_train_rows=1, fixed_comparison_rho="x")

    assert excinfo.value.reason == "invalid_fixed_rho"


def test_rho_zero_is_always_scored(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2, lambda_home=1.5, lambda_away=0.9)]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)
    entry = _path(summary)[0]

    observation = (1.5, 0.9, 1, 0, 1.0)
    expected = -math.log(observation_probability(observation, 0.0))

    assert math.isclose(entry["rho_zero_test_nll"], expected, rel_tol=1e-12)


def test_bucket_sum_and_counts(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2, home_goals=1, away_goals=0),
        _row("C", kickoff_at=K2, home_goals=0, away_goals=1),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)
    entry = _path(summary)[0]

    observation_b = (1.2, 0.8, 1, 0, 1.0)
    observation_c = (1.2, 0.8, 0, 1, 1.0)
    manual = (
        -math.log(observation_probability(observation_b, entry["fitted_rho"]))
        + -math.log(observation_probability(observation_c, entry["fitted_rho"]))
    )

    assert math.isclose(entry["fitted_test_nll"], manual, rel_tol=1e-12)
    assert entry["test_count"] == 2
    assert summary["evaluation_count"] == 2
    assert summary["target_bucket_count"] == 1


def test_aggregate_definitions(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3, home_goals=0, away_goals=0),
        _row("D", kickoff_at=K4, home_goals=2, away_goals=1),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)
    path = _path(summary)
    count = summary["evaluation_count"]

    assert summary["walk_forward_fitted_total_nll"] == sum(e["fitted_test_nll"] for e in path)
    assert summary["fixed_rho_total_nll"] == sum(e["fixed_rho_test_nll"] for e in path)
    assert summary["rho_zero_total_nll"] == sum(e["rho_zero_test_nll"] for e in path)

    assert math.isclose(
        summary["walk_forward_fitted_mean_nll"],
        summary["walk_forward_fitted_total_nll"] / count,
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["fixed_rho_mean_nll"], summary["fixed_rho_total_nll"] / count, rel_tol=1e-12
    )
    assert math.isclose(
        summary["rho_zero_mean_nll"], summary["rho_zero_total_nll"] / count, rel_tol=1e-12
    )
    assert math.isclose(
        summary["nll_improvement_vs_fixed"],
        summary["fixed_rho_total_nll"] - summary["walk_forward_fitted_total_nll"],
        rel_tol=1e-12,
    )
    assert math.isclose(
        summary["nll_improvement_vs_zero"],
        summary["rho_zero_total_nll"] - summary["walk_forward_fitted_total_nll"],
        rel_tol=1e-12,
    )


def test_rho_path_is_chronological(isolated_data_dir):
    rows = [
        _row("D", kickoff_at=K4),
        _row("A", kickoff_at=K1),
        _row("C", kickoff_at=K3),
        _row("B", kickoff_at=K2),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    targets = [entry["target_kickoff_at"] for entry in _path(summary)]
    assert targets == [K2, K3, K4]


# ---------------------------------------------------------------------------
# 分组与重复
# ---------------------------------------------------------------------------

def test_version_isolation(isolated_data_dir):
    rows = [
        _row("a1", kickoff_at=K1, model_version="baseline-1"),
        _row("a2", kickoff_at=K2, model_version="baseline-1"),
        _row("b1", kickoff_at=K1, model_version="football-ad-1"),
        _row("b2", kickoff_at=K2, model_version="football-ad-1"),
        _row("b3", kickoff_at=K3, model_version="football-ad-1"),
    ]

    summaries = build_rho_walk_forward_summaries(rows, min_train_rows=1)

    assert len(summaries) == 2
    assert [s["model_version"] for s in summaries] == ["baseline-1", "football-ad-1"]

    baseline, upgraded = summaries
    assert baseline["sample_count"] == 2
    assert baseline["target_bucket_count"] == 1
    # 训练集绝不跨版本
    assert _path(baseline)[0]["train_count"] == 1
    assert upgraded["sample_count"] == 3
    assert [entry["train_count"] for entry in _path(upgraded)] == [1, 2]


def test_global_duplicate_id_across_versions_rejected(isolated_data_dir):
    """同一 evaluation_id 即使篡改 model_version 也不能逃避重复检测。"""
    rows = [
        _row("dup", kickoff_at=K1, model_version="baseline-1"),
        _row("dup", kickoff_at=K2, model_version="football-ad-1"),
    ]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        build_rho_walk_forward_summaries(rows, min_train_rows=1)

    assert excinfo.value.reason == "duplicate_evaluation_id"
    assert excinfo.value.evaluation_id == "dup"


def test_duplicate_id_within_group_rejected(isolated_data_dir):
    rows = [_row("dup", kickoff_at=K1), _row("dup", kickoff_at=K2)]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert excinfo.value.reason == "duplicate_evaluation_id"


def test_mixed_group_rejected(isolated_data_dir):
    rows = [
        _row("a", kickoff_at=K1, model_version="baseline-1"),
        _row("b", kickoff_at=K2, model_version="football-ad-1"),
    ]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert excinfo.value.reason == "mixed_group"


def test_summaries_are_deterministically_ordered(isolated_data_dir):
    rows = [
        _row("f2", kickoff_at=K1, model_version="model-v2"),
        _row("b1", kickoff_at=K1, model_version="baseline-1"),
        _row("f1", kickoff_at=K1, model_version="football-ad-1"),
    ]

    forward = build_rho_walk_forward_summaries(rows, min_train_rows=1)
    backward = build_rho_walk_forward_summaries(list(reversed(rows)), min_train_rows=1)

    assert forward == backward
    assert [s["model_version"] for s in forward] == ["baseline-1", "football-ad-1", "model-v2"]


# ---------------------------------------------------------------------------
# 篮球
# ---------------------------------------------------------------------------

def test_basketball_not_summarized_but_rejected_directly(isolated_data_dir):
    rows = [
        _row("f1", kickoff_at=K1),
        _row("bb1", kickoff_at=K1, sport="basketball", model_name="elo-normal-points",
             model_version="baseline-1"),
    ]

    assert build_rho_walk_forward_summaries(rows, min_train_rows=1) == [
        s for s in build_rho_walk_forward_summaries([rows[0]], min_train_rows=1)
    ]

    with pytest.raises(DixonColesFittingError) as excinfo:
        validate_rho_walk_forward_group([rows[1]], min_train_rows=1)

    assert excinfo.value.reason == "unsupported_sport"


# ---------------------------------------------------------------------------
# 时间戳
# ---------------------------------------------------------------------------

def test_offset_timestamps_compared_chronologically(isolated_data_dir):
    """+08:00 的 09:00 实际早于 UTC 的 05:00；排序必须按绝对时刻而非字符串。"""
    rows = [
        _row("late", kickoff_at="2030-01-01T05:00:00+00:00"),
        _row("early", kickoff_at="2030-01-01T09:00:00+08:00"),
    ]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)
    entry = _path(summary)[0]

    assert entry["target_kickoff_at"] == "2030-01-01T05:00:00+00:00"
    assert entry["training_reference_kickoff_at"] == "2030-01-01T09:00:00+08:00"
    assert entry["train_count"] == 1


def test_mixed_naive_and_aware_kickoffs_rejected(isolated_data_dir):
    rows = [
        _row("naive", kickoff_at="2030-01-01T20:00:00"),
        _row("aware", kickoff_at=K2),
    ]

    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert excinfo.value.reason == "inconsistent_kickoff_timezone"


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, True, 0, -1.0, float("nan"), "1.2"])
def test_invalid_lambda_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesFittingError) as excinfo:
        validate_rho_walk_forward_group([_row("a", lambda_home=bad)], min_train_rows=1)

    assert excinfo.value.reason in {"invalid_expected_goals", "missing_expected_goals"}


@pytest.mark.parametrize("bad", [None, -1, 2.0, True, "2"])
def test_invalid_final_score_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesFittingError) as excinfo:
        validate_rho_walk_forward_group([_row("a", home_goals=bad)], min_train_rows=1)

    assert excinfo.value.reason in {"invalid_final_score", "missing_final_score"}


@pytest.mark.parametrize("kickoff", [None, "", "not-a-date"])
def test_invalid_kickoff_rejected(isolated_data_dir, kickoff):
    with pytest.raises(DixonColesFittingError) as excinfo:
        validate_rho_walk_forward_group([_row("a", kickoff_at=kickoff)], min_train_rows=1)

    assert excinfo.value.reason in {"missing_kickoff", "invalid_kickoff"}


@pytest.mark.parametrize("bad", [0, -1, 2.5, True, "3", None])
def test_invalid_min_train_rows_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesWalkForwardError) as excinfo:
        validate_rho_walk_forward_group([_row("a")], min_train_rows=bad)

    assert excinfo.value.reason == "invalid_min_train_rows"


# ---------------------------------------------------------------------------
# 纯度与摘要
# ---------------------------------------------------------------------------

def test_input_rows_not_mutated(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]
    before = copy.deepcopy(rows)

    validate_rho_walk_forward_group(rows, min_train_rows=1)
    build_rho_walk_forward_summaries(rows, min_train_rows=1)

    assert rows == before


def test_repeatability(isolated_data_dir):
    rows = [
        _row("A", kickoff_at=K1),
        _row("B", kickoff_at=K2),
        _row("C", kickoff_at=K3, home_goals=0, away_goals=0),
    ]

    assert validate_rho_walk_forward_group(rows, min_train_rows=1) == (
        validate_rho_walk_forward_group(rows, min_train_rows=1)
    )


def test_summary_schema_and_no_ranking_labels(isolated_data_dir):
    rows = [_row("A", kickoff_at=K1), _row("B", kickoff_at=K2)]

    summary = validate_rho_walk_forward_group(rows, min_train_rows=1)

    assert set(summary) == {
        "sport", "model_name", "model_version", "sample_count", "min_train_rows",
        "warmup_skipped_count", "evaluation_count", "target_bucket_count",
        "half_life_days", "rho_grid", "fixed_comparison_rho",
        "walk_forward_fitted_total_nll", "fixed_rho_total_nll", "rho_zero_total_nll",
        "walk_forward_fitted_mean_nll", "fixed_rho_mean_nll", "rho_zero_mean_nll",
        "nll_improvement_vs_fixed", "nll_improvement_vs_zero", "rho_path",
    }
    assert set(_path(summary)[0]) == {
        "target_kickoff_at", "train_count", "test_count",
        "training_reference_kickoff_at", "fitted_rho",
        "fitted_test_nll", "fixed_rho_test_nll", "rho_zero_test_nll",
    }

    for forbidden in (
        "winner", "best", "grade", "rank", "recommended", "deploy",
        "production_rho", "accuracy", "brier_score", "multiclass_log_loss",
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
        match = enrich_match(make_match(id=f"m-wf-{index}", date=date))
        snapshot, created = capture_snapshot(match)
        assert created is True
        settlement, settled = settle_snapshot(
            snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                           score={"ft": [2, 1]})
        )
        assert settled is True
        capture_evaluation_row(snapshot, settlement)

    summaries = validate_all_rho_walk_forward(min_train_rows=1)

    assert summaries == build_rho_walk_forward_summaries(
        get_all_evaluation_rows(), min_train_rows=1
    )
    assert len(summaries) == 1
    assert summaries[0]["sport"] == "football"
    assert summaries[0]["model_version"] == "football-ad-1"
    assert summaries[0]["evaluation_count"] == 1
    assert summaries[0]["target_bucket_count"] == 1
