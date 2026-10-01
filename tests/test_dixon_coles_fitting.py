"""时间加权 Dixon-Coles rho 拟合的行为测试（纯计算 + 读取层）。"""
from __future__ import annotations

import copy
import math

import pytest

from models.poisson_model import poisson_pmf
from utils.dixon_coles_fitting import (
    DEFAULT_HALF_LIFE_DAYS,
    DixonColesFittingError,
    build_rho_candidates,
    build_rho_fit_summaries,
    dixon_coles_tau,
    fit_all_rho,
    fit_rho_group,
    is_valid_rho_for_observation,
    time_decay_weight,
    weighted_negative_log_likelihood,
)

MODEL_NAME = "elo-poisson-dixon-coles"
T0 = "2030-01-01T20:00:00+08:00"


def _row(
    evaluation_id: str = "e1",
    *,
    match_id: str = "m1",
    sport: str = "football",
    model_name: str = MODEL_NAME,
    model_version: str = "football-ad-1",
    lambda_home: float = 1.2,
    lambda_away: float = 0.8,
    home_goals: int = 1,
    away_goals: int = 0,
    kickoff_at: str | None = T0,
    **extra,
) -> dict:
    row = {
        "evaluation_id": evaluation_id,
        "match_id": match_id,
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


def _obs(lambda_home=1.2, lambda_away=0.8, home_goals=1, away_goals=0, weight=1.0):
    return (lambda_home, lambda_away, home_goals, away_goals, weight)


# ---------------------------------------------------------------------------
# tau 公式
# ---------------------------------------------------------------------------

def test_tau_formula(isolated_data_dir):
    lam, mu, rho = 1.2, 0.8, 0.1

    assert math.isclose(dixon_coles_tau(0, 0, lam, mu, rho), 1 - lam * mu * rho, rel_tol=1e-12)
    assert math.isclose(dixon_coles_tau(0, 1, lam, mu, rho), 1 + lam * rho, rel_tol=1e-12)
    assert math.isclose(dixon_coles_tau(1, 0, lam, mu, rho), 1 + mu * rho, rel_tol=1e-12)
    assert math.isclose(dixon_coles_tau(1, 1, lam, mu, rho), 1 - rho, rel_tol=1e-12)

    for score in [(2, 0), (0, 2), (2, 2), (3, 1), (4, 4)]:
        assert dixon_coles_tau(score[0], score[1], lam, mu, rho) == 1.0


def test_rho_zero_makes_tau_unit(isolated_data_dir):
    lam, mu = 1.2, 0.8

    for score in [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (0, 2), (2, 2)]:
        assert dixon_coles_tau(score[0], score[1], lam, mu, 0.0) == 1.0


def test_invalid_tau_candidate_rejected(isolated_data_dir):
    observation = _obs(lambda_home=5.0, lambda_away=5.0, home_goals=1, away_goals=0)

    # tau(0,1) = 1 + 5 * (-0.25) = -0.25 <= 0
    assert is_valid_rho_for_observation(observation, -0.25) is False
    with pytest.raises(DixonColesFittingError) as excinfo:
        weighted_negative_log_likelihood([observation], -0.25)
    assert excinfo.value.reason == "invalid_tau"

    # rho = 0 始终有效
    assert is_valid_rho_for_observation(observation, 0.0) is True


# ---------------------------------------------------------------------------
# 似然
# ---------------------------------------------------------------------------

def test_poisson_likelihood_at_rho_zero(isolated_data_dir):
    observation = _obs(lambda_home=1.2, lambda_away=0.8, home_goals=1, away_goals=0)

    expected_probability = poisson_pmf(1.2, 1) * poisson_pmf(0.8, 0)
    expected_nll = -math.log(expected_probability)

    assert math.isclose(
        weighted_negative_log_likelihood([observation], 0.0), expected_nll, rel_tol=1e-12
    )


def test_dixon_coles_likelihood_with_nonzero_rho(isolated_data_dir):
    from utils.dixon_coles_fitting import observation_probability

    observation = _obs(lambda_home=1.2, lambda_away=0.8, home_goals=1, away_goals=0)
    rho = 0.1

    independent = poisson_pmf(1.2, 1) * poisson_pmf(0.8, 0)
    # 1-0 的修正为 1 + μ * ρ
    assert math.isclose(
        observation_probability(observation, rho),
        independent * (1 + 0.8 * rho),
        rel_tol=1e-12,
    )


def test_weighted_nll_matches_manual_sum(isolated_data_dir):
    observations = [
        _obs(1.2, 0.8, 1, 0, weight=2.0),
        _obs(1.5, 1.1, 0, 0, weight=0.5),
    ]

    manual = 0.0
    for lam, mu, hg, ag, weight in observations:
        probability = poisson_pmf(lam, hg) * poisson_pmf(mu, ag)  # rho = 0 -> tau = 1
        manual += weight * -math.log(probability)

    assert math.isclose(
        weighted_negative_log_likelihood(observations, 0.0), manual, rel_tol=1e-12
    )


# ---------------------------------------------------------------------------
# 时间权重
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("age,expected", [(0.0, 1.0), (180.0, 0.5), (360.0, 0.25)])
def test_time_decay_weight(isolated_data_dir, age, expected):
    assert math.isclose(time_decay_weight(age, 180.0), expected, rel_tol=1e-12)


def test_time_decay_weight_recent_is_larger(isolated_data_dir):
    assert time_decay_weight(0.0) > time_decay_weight(90.0) > time_decay_weight(360.0)


@pytest.mark.parametrize("half_life", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_half_life_rejected(isolated_data_dir, half_life):
    with pytest.raises(DixonColesFittingError) as excinfo:
        time_decay_weight(10.0, half_life)
    assert excinfo.value.reason == "invalid_half_life"


# ---------------------------------------------------------------------------
# 网格
# ---------------------------------------------------------------------------

def test_grid_construction(isolated_data_dir):
    candidates = build_rho_candidates()

    assert len(candidates) == 101
    assert candidates[0] == -0.25
    assert math.isclose(candidates[-1], 0.25, rel_tol=1e-12)
    assert len(set(candidates)) == len(candidates)   # 无重复候选
    assert any(rho == 0.0 for rho in candidates)     # 0 恰好在网格上
    assert sum(1 for rho in candidates if rho == 0.0) == 1


def test_grid_index_construction_has_no_drift(isolated_data_dir):
    candidates = build_rho_candidates(-0.25, 0.25, 0.005)

    for index, rho in enumerate(candidates):
        assert math.isclose(rho, -0.25 + index * 0.005, rel_tol=1e-12, abs_tol=1e-15)


@pytest.mark.parametrize(
    "rho_min,rho_max,rho_step",
    [(0.0, 1.0, 0.0), (0.0, 1.0, -0.1), (0.5, -0.5, 0.1), (float("nan"), 1.0, 0.1)],
)
def test_grid_invalid_parameters(isolated_data_dir, rho_min, rho_max, rho_step):
    with pytest.raises(DixonColesFittingError) as excinfo:
        build_rho_candidates(rho_min, rho_max, rho_step)
    assert excinfo.value.reason == "invalid_grid"


# ---------------------------------------------------------------------------
# 拟合
# ---------------------------------------------------------------------------

def test_synthetic_rho_recovery(isolated_data_dir):
    """
    λ = μ = 1，11 场 0-0 与 9 场 1-0：
    解析最优 rho = (n_10 - n_00) / (n_00 + n_10) = -0.10。
    """
    rows = [
        _row(f"z{i}", lambda_home=1.0, lambda_away=1.0, home_goals=0, away_goals=0)
        for i in range(11)
    ] + [
        _row(f"o{i}", lambda_home=1.0, lambda_away=1.0, home_goals=1, away_goals=0)
        for i in range(9)
    ]

    summary = fit_rho_group(rows)

    assert summary["sample_count"] == 20
    assert abs(summary["fitted_rho"] - (-0.10)) <= 0.005 / 2 + 1e-9
    assert summary["weighted_nll"] < summary["rho_zero_weighted_nll"]


def test_fitted_rho_beats_zero(isolated_data_dir):
    rows = [
        _row(f"z{i}", lambda_home=1.0, lambda_away=1.0, home_goals=0, away_goals=0)
        for i in range(12)
    ] + [
        _row(f"o{i}", lambda_home=1.0, lambda_away=1.0, home_goals=1, away_goals=0)
        for i in range(8)
    ]

    summary = fit_rho_group(rows)

    assert summary["weighted_nll_improvement_vs_zero"] > 0
    assert math.isclose(
        summary["weighted_nll_improvement_vs_zero"],
        summary["rho_zero_weighted_nll"] - summary["weighted_nll"],
        rel_tol=1e-12,
    )


def test_tie_breaking_prefers_smaller_absolute_rho(isolated_data_dir):
    """全部为非低比分 -> 目标函数对 rho 恒定 -> 并列时取 |rho| 最小者。"""
    rows = [_row(f"n{i}", home_goals=2, away_goals=0) for i in range(5)]

    summary = fit_rho_group(rows)

    assert summary["fitted_rho"] == 0.0
    assert summary["rho_grid"]["valid_candidate_count"] == summary["rho_grid"]["candidate_count"]


def test_tie_breaking_prefers_smaller_numeric_rho(isolated_data_dir):
    """自定义对称网格（不含 0）：|rho| 相同 -> 取数值更小者。"""
    rows = [_row(f"n{i}", home_goals=2, away_goals=0) for i in range(3)]

    summary = fit_rho_group(rows, rho_min=-0.005, rho_max=0.005, rho_step=0.01)

    assert summary["rho_grid"]["candidate_count"] == 2
    assert summary["fitted_rho"] == -0.005


def test_reference_time_determinism(isolated_data_dir):
    rows = [
        _row("a", kickoff_at="2030-01-01T20:00:00+08:00"),
        _row("b", kickoff_at="2030-03-01T20:00:00+08:00"),
        _row("c", kickoff_at="2030-02-01T20:00:00+08:00"),
    ]

    first = fit_rho_group(rows)
    second = fit_rho_group(rows)

    assert first == second
    assert first["reference_kickoff_at"] == "2030-03-01T20:00:00+08:00"


def test_recent_observation_receives_larger_weight(isolated_data_dir):
    rows = [
        _row("recent", kickoff_at="2030-03-01T20:00:00+08:00"),
        _row("old", kickoff_at="2029-09-02T20:00:00+08:00"),   # 恰好 180 天前
    ]

    summary = fit_rho_group(rows)

    # 参考时间为最晚开赛时间：weight(recent)=1.0, weight(old)=0.5
    assert math.isclose(summary["effective_sample_weight"], 1.5, rel_tol=1e-6)


def test_low_score_sample_count(isolated_data_dir):
    scores = [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (3, 2)]
    rows = [_row(f"s{i}", home_goals=h, away_goals=a) for i, (h, a) in enumerate(scores)]

    summary = fit_rho_group(rows)

    assert summary["sample_count"] == 6
    assert summary["low_score_sample_count"] == 4


def test_no_valid_rho_candidates(isolated_data_dir):
    rows = [_row("x", lambda_home=250.0, lambda_away=250.0, home_goals=1, away_goals=0)]

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group(rows, rho_min=0.005, rho_max=0.25, rho_step=0.005)

    assert excinfo.value.reason == "no_valid_rho_candidates"


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

def test_group_separation(isolated_data_dir):
    rows = [
        _row("b1", model_version="baseline-1"),
        _row("f1", model_version="football-ad-1"),
    ]

    summaries = build_rho_fit_summaries(rows)

    assert len(summaries) == 2
    assert [s["model_version"] for s in summaries] == ["baseline-1", "football-ad-1"]
    assert all(s["sample_count"] == 1 for s in summaries)


def test_summaries_are_deterministically_ordered(isolated_data_dir):
    rows = [
        _row("f2", model_version="model-v2"),
        _row("b1", model_version="baseline-1"),
        _row("f1", model_version="football-ad-1"),
    ]

    forward = build_rho_fit_summaries(rows)
    backward = build_rho_fit_summaries(list(reversed(rows)))

    assert forward == backward
    assert [s["model_version"] for s in forward] == ["baseline-1", "football-ad-1", "model-v2"]


def test_mixed_group_rejected(isolated_data_dir):
    rows = [
        _row("a", model_version="baseline-1"),
        _row("b", model_version="football-ad-1"),
    ]

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group(rows)

    assert excinfo.value.reason == "mixed_group"


def test_empty_dataset_and_group(isolated_data_dir):
    assert build_rho_fit_summaries([]) == []

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([])

    assert excinfo.value.reason == "empty_group"


def test_duplicate_evaluation_id_rejected(isolated_data_dir):
    rows = [_row("dup"), _row("dup")]

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group(rows)

    assert excinfo.value.reason == "duplicate_evaluation_id"


def test_invalid_half_life_parameter_rejected(isolated_data_dir):
    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([_row()], half_life_days=0.0)

    assert excinfo.value.reason == "invalid_half_life"


# ---------------------------------------------------------------------------
# 篮球
# ---------------------------------------------------------------------------

def test_basketball_row_rejected_by_direct_fitter(isolated_data_dir):
    row = _row("bb", sport="basketball", model_name="elo-normal-points",
               model_version="baseline-1")

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([row])

    assert excinfo.value.reason == "unsupported_sport"


def test_basketball_group_not_summarized(isolated_data_dir):
    rows = [
        _row("f1", model_version="football-ad-1"),
        _row("bb1", sport="basketball", model_name="elo-normal-points",
             model_version="baseline-1"),
    ]

    summaries = build_rho_fit_summaries(rows)

    assert len(summaries) == 1
    assert summaries[0]["sport"] == "football"


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad", [None, True, 0, -1.0, float("nan"), float("inf"), "1.2"]
)
def test_invalid_expected_goals_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([_row(lambda_home=bad)])

    assert excinfo.value.reason in {"invalid_expected_goals", "missing_expected_goals"}


def test_missing_expected_goals_block_rejected(isolated_data_dir):
    row = _row()
    del row["expected_score_data"]

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([row])

    assert excinfo.value.reason == "missing_expected_goals"


@pytest.mark.parametrize("bad", [None, -1, 2.0, True, "2"])
def test_invalid_final_score_rejected(isolated_data_dir, bad):
    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([_row(home_goals=bad)])

    assert excinfo.value.reason in {"invalid_final_score", "missing_final_score"}


def test_missing_final_score_block_rejected(isolated_data_dir):
    row = _row()
    del row["final_score"]

    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([row])

    assert excinfo.value.reason == "missing_final_score"


@pytest.mark.parametrize("kickoff", [None, "", "not-a-date", "2030-13-45T99:99:99"])
def test_invalid_kickoff_rejected(isolated_data_dir, kickoff):
    with pytest.raises(DixonColesFittingError) as excinfo:
        fit_rho_group([_row(kickoff_at=kickoff)])

    assert excinfo.value.reason in {"missing_kickoff", "invalid_kickoff"}


# ---------------------------------------------------------------------------
# 纯度与摘要
# ---------------------------------------------------------------------------

def test_input_rows_not_mutated(isolated_data_dir):
    rows = [_row("a"), _row("b", home_goals=0, away_goals=0)]
    before = copy.deepcopy(rows)

    fit_rho_group(rows)
    build_rho_fit_summaries(rows)

    assert rows == before


def test_summary_schema(isolated_data_dir):
    summary = fit_rho_group([_row()])

    assert set(summary) == {
        "sport", "model_name", "model_version", "sample_count", "low_score_sample_count",
        "reference_kickoff_at", "half_life_days", "effective_sample_weight",
        "rho_grid", "fitted_rho", "weighted_nll", "rho_zero_weighted_nll",
        "weighted_nll_improvement_vs_zero",
    }
    assert set(summary["rho_grid"]) == {
        "minimum", "maximum", "step", "candidate_count", "valid_candidate_count",
    }
    assert summary["half_life_days"] == DEFAULT_HALF_LIFE_DAYS

    for forbidden in (
        "recommended_production_rho", "deploy", "winner", "best_model",
        "approved_rho", "grade", "score", "rank",
    ):
        assert forbidden not in summary


def test_repeated_fit_is_identical(isolated_data_dir):
    rows = [_row("a"), _row("b", home_goals=0, away_goals=1)]

    assert fit_rho_group(rows) == fit_rho_group(rows)


# ---------------------------------------------------------------------------
# 便捷入口（读取既有评估样本存储）
# ---------------------------------------------------------------------------

def test_convenience_function_matches_direct_fit(isolated_data_dir, make_match):
    from utils.daily_loader import enrich_match
    from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
    from utils.prediction_snapshots import capture_snapshot
    from utils.settlements import settle_snapshot

    match = enrich_match(make_match(id="m-rho"))
    snapshot, created = capture_snapshot(match)
    assert created is True
    settlement, settled = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                       score={"ft": [2, 1]})
    )
    assert settled is True
    capture_evaluation_row(snapshot, settlement)

    summaries = fit_all_rho()

    assert summaries == build_rho_fit_summaries(get_all_evaluation_rows())
    assert len(summaries) == 1
    assert summaries[0]["sport"] == "football"
    assert summaries[0]["sample_count"] == 1
    assert summaries[0]["model_version"] == "football-ad-1"
