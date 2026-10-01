"""概率校准诊断的行为测试（纯计算 + 读取层）。"""
from __future__ import annotations

import copy
import math

import pytest

from utils.calibration_evaluation import (
    BIN_COUNT,
    bin_bounds,
    bin_index,
    build_calibration_summaries,
    build_class_calibration,
    evaluate_all_calibration,
    evaluate_calibration_group,
)
from utils.classification_evaluation import (
    ClassificationEvaluationError,
    FOOTBALL_CLASSES,
    PROBABILITY_SOURCE,
)

FOOTBALL_MODEL = "elo-poisson-dixon-coles"
BASKETBALL_MODEL = "elo-normal-points"

FOOTBALL_60_20_20 = {"home_win": 60, "draw": 20, "away_win": 20}
FOOTBALL_50_25_25 = {"home_win": 50, "draw": 25, "away_win": 25}


def _row(
    *,
    evaluation_id: str = "e1",
    match_id: str = "m1",
    sport: str = "football",
    model_name: str = FOOTBALL_MODEL,
    model_version: str = "baseline-1",
    probabilities: dict | None = None,
    actual_outcome: str = "home_win",
    **extra,
) -> dict:
    row = {
        "evaluation_id": evaluation_id,
        "match_id": match_id,
        "sport": sport,
        "model_name": model_name,
        "model_version": model_version,
        "model_probabilities": dict(probabilities if probabilities is not None else FOOTBALL_60_20_20),
        "actual_outcome": actual_outcome,
    }
    row.update(extra)
    return row


def _basketball_row(**kwargs) -> dict:
    kwargs.setdefault("sport", "basketball")
    kwargs.setdefault("model_name", BASKETBALL_MODEL)
    kwargs.setdefault("probabilities", {"home_win": 60, "draw": 0, "away_win": 40})
    return _row(**kwargs)


def _class_bins(summary: dict, cls: str) -> dict[int, dict]:
    return {b["bin_index"]: b for b in summary["class_calibration"][cls]["bins"]}


# ---------------------------------------------------------------------------
# 已知数值
# ---------------------------------------------------------------------------

def test_known_single_class_bin(isolated_data_dir):
    """两行 home_win = 60%，实际 1 次命中 -> bin 6, gap 0.1, ECE 0.1。"""
    rows = [
        _row(evaluation_id="a", probabilities=FOOTBALL_60_20_20, actual_outcome="home_win"),
        _row(evaluation_id="b", probabilities=FOOTBALL_60_20_20, actual_outcome="away_win"),
    ]

    summary = evaluate_calibration_group(rows)

    assert summary["sample_count"] == 2
    assert summary["bin_count"] == BIN_COUNT
    assert summary["probability_source"] == PROBABILITY_SOURCE

    home = _class_bins(summary, "home_win")
    occupied = [b for b in home.values() if b["count"] > 0]
    assert len(occupied) == 1
    assert occupied[0]["bin_index"] == 6
    assert occupied[0]["count"] == 2
    assert math.isclose(occupied[0]["mean_predicted_probability"], 0.6, rel_tol=1e-12)
    assert math.isclose(occupied[0]["observed_frequency"], 0.5, rel_tol=1e-12)
    assert math.isclose(occupied[0]["calibration_gap"], 0.1, rel_tol=1e-12)

    assert math.isclose(
        summary["class_calibration"]["home_win"]["expected_calibration_error"],
        0.1,
        rel_tol=1e-12,
    )


def test_perfectly_calibrated_class(isolated_data_dir):
    """10 行 home_win = 50%，实际命中 5 次 -> gap 0, ECE 0。"""
    rows = [
        _row(evaluation_id=f"e{i}", probabilities=FOOTBALL_50_25_25,
             actual_outcome="home_win" if i < 5 else "draw")
        for i in range(10)
    ]

    summary = evaluate_calibration_group(rows)

    home = _class_bins(summary, "home_win")
    occupied = [b for b in home.values() if b["count"] > 0]
    assert len(occupied) == 1
    assert occupied[0]["bin_index"] == 5
    assert occupied[0]["count"] == 10
    assert math.isclose(occupied[0]["mean_predicted_probability"], 0.5, rel_tol=1e-12)
    assert math.isclose(occupied[0]["observed_frequency"], 0.5, rel_tol=1e-12)
    assert math.isclose(occupied[0]["calibration_gap"], 0.0, abs_tol=1e-15)
    assert math.isclose(
        summary["class_calibration"]["home_win"]["expected_calibration_error"],
        0.0,
        abs_tol=1e-15,
    )


def test_weighted_ece_weights_bins_by_count(isolated_data_dir):
    """2 条在 0.2 箱（频率 0），8 条在 0.7 箱（频率 0.75）-> ECE = 0.08。"""
    predicted = [0.2, 0.2] + [0.7] * 8
    observed = [0, 0] + [1] * 6 + [0] * 2

    result = build_class_calibration(predicted, observed)

    bins = {b["bin_index"]: b for b in result["bins"]}
    assert bins[2]["count"] == 2
    assert math.isclose(bins[2]["mean_predicted_probability"], 0.2, rel_tol=1e-12)
    assert math.isclose(bins[2]["observed_frequency"], 0.0, abs_tol=1e-15)
    assert bins[7]["count"] == 8
    assert math.isclose(bins[7]["mean_predicted_probability"], 0.7, rel_tol=1e-12)
    assert math.isclose(bins[7]["observed_frequency"], 0.75, rel_tol=1e-12)

    expected = (2 / 10) * abs(0.2 - 0.0) + (8 / 10) * abs(0.7 - 0.75)
    assert math.isclose(result["expected_calibration_error"], expected, rel_tol=1e-12)
    assert math.isclose(result["expected_calibration_error"], 0.08, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 分箱
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "probability,expected",
    [(0.00, 0), (0.09, 0), (0.10, 1), (0.20, 2), (0.50, 5), (0.59, 5),
     (0.60, 6), (0.90, 9), (0.99, 9), (1.00, 9)],
)
def test_bin_assignment(isolated_data_dir, probability, expected):
    assert bin_index(probability) == expected


def test_bin_bounds(isolated_data_dir):
    assert bin_bounds(0) == (0.0, 0.1)
    assert bin_bounds(5) == (0.5, 0.6)
    assert bin_bounds(9) == (0.9, 1.0)


def test_probability_100_maps_to_last_bin(isolated_data_dir):
    summary = evaluate_calibration_group(
        [_row(probabilities={"home_win": 100, "draw": 0, "away_win": 0}, actual_outcome="home_win")]
    )

    bins = _class_bins(summary, "home_win")
    assert bins[9]["count"] == 1
    assert math.isclose(bins[9]["mean_predicted_probability"], 1.0, rel_tol=1e-12)
    assert len(summary["class_calibration"]["home_win"]["bins"]) == BIN_COUNT


def test_empty_bins_are_none_not_nan(isolated_data_dir):
    summary = evaluate_calibration_group([_row()])

    for cls in FOOTBALL_CLASSES:
        for entry in summary["class_calibration"][cls]["bins"]:
            if entry["count"] == 0:
                assert entry["mean_predicted_probability"] is None
                assert entry["observed_frequency"] is None
                assert entry["calibration_gap"] is None
            else:
                assert math.isfinite(entry["mean_predicted_probability"])
                assert math.isfinite(entry["observed_frequency"])
                assert math.isfinite(entry["calibration_gap"])


# ---------------------------------------------------------------------------
# 取整归一化
# ---------------------------------------------------------------------------

def test_rounded_probabilities_are_normalized_before_binning(isolated_data_dir):
    """34/33/34 先归一化到和为 1.0，再按归一化值分箱（不是 0.34/0.33/0.34）。"""
    summary = evaluate_calibration_group(
        [_row(probabilities={"home_win": 34, "draw": 33, "away_win": 34}, actual_outcome="home_win")]
    )

    home = _class_bins(summary, "home_win")
    occupied = [b for b in home.values() if b["count"] > 0]
    assert len(occupied) == 1
    assert occupied[0]["bin_index"] == 3
    assert math.isclose(occupied[0]["mean_predicted_probability"], 34 / 101, rel_tol=1e-12)
    assert not math.isclose(occupied[0]["mean_predicted_probability"], 0.34, rel_tol=1e-6)

    for cls in FOOTBALL_CLASSES:
        bins = _class_bins(summary, cls)
        assert sum(b["count"] for b in bins.values()) == 1


# ---------------------------------------------------------------------------
# 类别空间
# ---------------------------------------------------------------------------

def test_football_classes_each_get_ten_bins(isolated_data_dir):
    summary = evaluate_calibration_group([_row()])

    assert summary["classes"] == list(FOOTBALL_CLASSES)
    assert list(summary["class_calibration"]) == list(FOOTBALL_CLASSES)
    for cls in FOOTBALL_CLASSES:
        assert len(summary["class_calibration"][cls]["bins"]) == BIN_COUNT


def test_basketball_has_two_classes(isolated_data_dir):
    summary = evaluate_calibration_group([_basketball_row()])

    assert summary["classes"] == ["home_win", "away_win"]
    assert "draw" not in summary["class_calibration"]
    assert len(summary["class_calibration"]["home_win"]["bins"]) == BIN_COUNT


def test_basketball_nonzero_draw_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(
            [_basketball_row(probabilities={"home_win": 55, "draw": 5, "away_win": 40})]
        )

    assert excinfo.value.reason == "nonzero_basketball_draw_probability"


# ---------------------------------------------------------------------------
# 校验（复用分类评估语义）
# ---------------------------------------------------------------------------

def test_invalid_football_actual_outcome_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group([_row(actual_outcome="unknown")])

    assert excinfo.value.reason == "invalid_actual_outcome"


def test_basketball_draw_actual_outcome_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group([_basketball_row(actual_outcome="draw")])

    assert excinfo.value.reason == "invalid_actual_outcome"


@pytest.mark.parametrize(
    "bad",
    [None, "55", True, -1, 101, float("nan"), float("inf"), float("-inf")],
)
def test_invalid_probability_values_rejected(isolated_data_dir, bad):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(
            [_row(probabilities={"home_win": bad, "draw": 20, "away_win": 20})]
        )

    assert excinfo.value.reason in {"invalid_probability", "missing_probability"}


def test_missing_class_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group([_row(probabilities={"home_win": 60, "away_win": 40})])

    assert excinfo.value.reason == "missing_probability"


def test_invalid_total_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(
            [_row(probabilities={"home_win": 60, "draw": 20, "away_win": 10})]
        )

    assert excinfo.value.reason == "invalid_probability_total"


def test_fraction_scale_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(
            [_row(probabilities={"home_win": 0.6, "draw": 0.2, "away_win": 0.2})]
        )

    assert excinfo.value.reason == "invalid_probability_total"


def test_unsupported_sport_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group([_row(sport="tennis")])

    assert excinfo.value.reason == "unsupported_sport"


# ---------------------------------------------------------------------------
# 概率来源隔离
# ---------------------------------------------------------------------------

def test_display_probabilities_ignored(isolated_data_dir):
    base = _row(probabilities=FOOTBALL_60_20_20, actual_outcome="home_win")
    noisy = _row(
        evaluation_id="e2",
        probabilities=FOOTBALL_60_20_20,
        actual_outcome="home_win",
        display_probabilities={"home_win": 5, "draw": 90, "away_win": 5},
    )

    assert evaluate_calibration_group([base]) == evaluate_calibration_group([noisy])


def test_market_fields_ignored(isolated_data_dir):
    base = _row(probabilities=FOOTBALL_60_20_20, actual_outcome="home_win")
    noisy = _row(
        evaluation_id="e2",
        probabilities=FOOTBALL_60_20_20,
        actual_outcome="home_win",
        market_odds={"home_win": 1.01, "draw": 30.0, "away_win": 50.0},
        market_implied_probabilities={"home_win": 98, "draw": 1, "away_win": 1},
        expected_values={"home": {"ev": 99.9, "is_value": True, "kelly_pct": 50.0}},
    )

    assert evaluate_calibration_group([base]) == evaluate_calibration_group([noisy])


# ---------------------------------------------------------------------------
# 分组
# ---------------------------------------------------------------------------

def test_duplicate_evaluation_id_rejected(isolated_data_dir):
    rows = [_row(evaluation_id="dup"), _row(evaluation_id="dup", actual_outcome="draw")]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(rows)

    assert excinfo.value.reason == "duplicate_evaluation_id"


def test_mixed_model_version_group_rejected(isolated_data_dir):
    rows = [_row(evaluation_id="a"), _row(evaluation_id="b", model_version="model-v2")]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(rows)

    assert excinfo.value.reason == "mixed_group"


def test_mixed_sport_group_rejected(isolated_data_dir):
    rows = [_row(evaluation_id="a"), _basketball_row(evaluation_id="b")]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group(rows)

    assert excinfo.value.reason == "mixed_group"


def test_empty_group_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_calibration_group([])

    assert excinfo.value.reason == "empty_group"


def test_empty_dataset_returns_empty_list(isolated_data_dir):
    assert build_calibration_summaries([]) == []


def test_automatic_grouping(isolated_data_dir):
    rows = [
        _row(evaluation_id="f1"),
        _row(evaluation_id="f2", model_version="model-v2"),
        _basketball_row(evaluation_id="b1"),
    ]

    summaries = build_calibration_summaries(rows)

    assert len(summaries) == 3
    keys = [(s["sport"], s["model_name"], s["model_version"]) for s in summaries]
    assert keys == sorted(keys)
    by_sport = {s["sport"]: s for s in summaries}
    assert by_sport["football"]["classes"] == list(FOOTBALL_CLASSES)
    assert by_sport["basketball"]["classes"] == ["home_win", "away_win"]


def test_deterministic_ordering(isolated_data_dir):
    rows = [
        _basketball_row(evaluation_id="b1"),
        _row(evaluation_id="f2", model_version="model-v2"),
        _row(evaluation_id="f1"),
    ]

    forward = build_calibration_summaries(rows)
    backward = build_calibration_summaries(list(reversed(rows)))

    assert forward == backward
    assert [(s["sport"], s["model_name"], s["model_version"]) for s in forward] == [
        ("basketball", BASKETBALL_MODEL, "baseline-1"),
        ("football", FOOTBALL_MODEL, "baseline-1"),
        ("football", FOOTBALL_MODEL, "model-v2"),
    ]


# ---------------------------------------------------------------------------
# Macro ECE
# ---------------------------------------------------------------------------

def test_macro_ece_football(isolated_data_dir):
    rows = [
        _row(evaluation_id="a", probabilities=FOOTBALL_60_20_20, actual_outcome="home_win"),
        _row(evaluation_id="b", probabilities=FOOTBALL_60_20_20, actual_outcome="away_win"),
    ]

    summary = evaluate_calibration_group(rows)
    errors = [summary["class_calibration"][cls]["expected_calibration_error"] for cls in FOOTBALL_CLASSES]

    # home 0.1 / draw 0.2 / away 0.3
    assert math.isclose(errors[0], 0.1, rel_tol=1e-12)
    assert math.isclose(errors[1], 0.2, rel_tol=1e-12)
    assert math.isclose(errors[2], 0.3, rel_tol=1e-12)
    assert math.isclose(
        summary["macro_expected_calibration_error"],
        (0.1 + 0.2 + 0.3) / 3,
        rel_tol=1e-12,
    )


def test_macro_ece_basketball_divides_by_two(isolated_data_dir):
    summary = evaluate_calibration_group([_basketball_row()])

    home = summary["class_calibration"]["home_win"]["expected_calibration_error"]
    away = summary["class_calibration"]["away_win"]["expected_calibration_error"]
    assert math.isclose(home, 0.4, rel_tol=1e-12)
    assert math.isclose(away, 0.4, rel_tol=1e-12)
    assert math.isclose(summary["macro_expected_calibration_error"], (home + away) / 2, rel_tol=1e-12)


# ---------------------------------------------------------------------------
# 纯度与不可变性
# ---------------------------------------------------------------------------

def test_input_rows_not_mutated(isolated_data_dir):
    rows = [
        _row(evaluation_id="a", display_probabilities={"home_win": 1, "draw": 1, "away_win": 98}),
        _row(evaluation_id="b", probabilities=FOOTBALL_50_25_25, actual_outcome="draw"),
    ]
    before = copy.deepcopy(rows)

    build_calibration_summaries(rows)
    evaluate_calibration_group(rows[:1])

    assert rows == before


def test_repeated_evaluation_is_deterministic(isolated_data_dir):
    rows = [_row(evaluation_id="a"), _row(evaluation_id="b", actual_outcome="draw")]

    assert build_calibration_summaries(rows) == build_calibration_summaries(rows)


def test_summary_has_no_extra_fields(isolated_data_dir):
    summary = evaluate_calibration_group([_row()])

    assert set(summary) == {
        "sport", "model_name", "model_version", "probability_source",
        "bin_count", "sample_count", "classes", "class_calibration",
        "macro_expected_calibration_error",
    }
    for forbidden in ("grade", "score", "rating", "good", "bad",
                      "well_calibrated", "poorly_calibrated", "best_model",
                      "top_label_ece", "reliability_diagram"):
        assert forbidden not in summary


# ---------------------------------------------------------------------------
# 便捷入口（读取既有评估样本存储）
# ---------------------------------------------------------------------------

def test_convenience_function_matches_direct_evaluation(isolated_data_dir, make_match):
    from utils.daily_loader import enrich_match
    from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
    from utils.prediction_snapshots import capture_snapshot
    from utils.settlements import settle_snapshot

    match = enrich_match(make_match(id="m-cal"))
    snapshot, created = capture_snapshot(match)
    assert created is True
    settlement, settled = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                       score={"ft": [2, 1]})
    )
    assert settled is True
    capture_evaluation_row(snapshot, settlement)

    summaries = evaluate_all_calibration()

    assert summaries == build_calibration_summaries(get_all_evaluation_rows())
    assert len(summaries) == 1
    assert summaries[0]["sample_count"] == 1
    assert summaries[0]["sport"] == "football"
    assert summaries[0]["bin_count"] == BIN_COUNT
