"""分类评估核心的行为测试（纯计算 + 读取层）。"""
from __future__ import annotations

import copy
import math

import pytest

from utils.classification_evaluation import (
    BASKETBALL_CLASSES,
    FOOTBALL_CLASSES,
    PROBABILITY_SOURCE,
    ClassificationEvaluationError,
    build_classification_summaries,
    classes_for_sport,
    evaluate_all_classification,
    evaluate_classification_group,
    normalize_class_probabilities,
)

FOOTBALL_MODEL = "elo-poisson-dixon-coles"
BASKETBALL_MODEL = "elo-normal-points"

FOOTBALL_60_20_20 = {"home_win": 60, "draw": 20, "away_win": 20}
FOOTBALL_20_30_50 = {"home_win": 20, "draw": 30, "away_win": 50}


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


# ---------------------------------------------------------------------------
# 已知数值
# ---------------------------------------------------------------------------

def test_known_football_single_row(isolated_data_dir):
    summary = evaluate_classification_group([_row()])

    assert summary["sample_count"] == 1
    assert summary["accuracy"] == 1.0
    assert math.isclose(summary["brier_score"], 0.24, rel_tol=1e-12)
    assert math.isclose(summary["multiclass_log_loss"], -math.log(0.6), rel_tol=1e-12)
    assert summary["probability_source"] == PROBABILITY_SOURCE
    assert summary["classes"] == list(FOOTBALL_CLASSES)


def test_known_two_row_football(isolated_data_dir):
    rows = [
        _row(evaluation_id="a", probabilities=FOOTBALL_60_20_20, actual_outcome="home_win"),
        _row(evaluation_id="b", probabilities=FOOTBALL_20_30_50, actual_outcome="home_win"),
    ]
    summary = evaluate_classification_group(rows)

    assert summary["sample_count"] == 2
    assert summary["accuracy"] == 0.5
    # (0.24 + 0.98) / 2
    assert math.isclose(summary["brier_score"], 0.61, rel_tol=1e-12)
    # (-ln(0.6) + -ln(0.2)) / 2
    expected_log_loss = (-math.log(0.6) + -math.log(0.2)) / 2
    assert math.isclose(summary["multiclass_log_loss"], expected_log_loss, rel_tol=1e-12)


def test_basketball_single_row(isolated_data_dir):
    summary = evaluate_classification_group([_basketball_row()])

    assert summary["sample_count"] == 1
    assert summary["accuracy"] == 1.0
    # (0.6-1)^2 + (0.4-0)^2
    assert math.isclose(summary["brier_score"], 0.32, rel_tol=1e-12)
    assert math.isclose(summary["multiclass_log_loss"], -math.log(0.6), rel_tol=1e-12)
    assert summary["classes"] == list(BASKETBALL_CLASSES)
    assert "draw" not in summary["classes"]


def test_perfect_prediction(isolated_data_dir):
    summary = evaluate_classification_group(
        [_row(probabilities={"home_win": 100, "draw": 0, "away_win": 0}, actual_outcome="home_win")]
    )

    assert summary["accuracy"] == 1.0
    assert math.isclose(summary["brier_score"], 0.0, abs_tol=1e-15)
    assert math.isclose(summary["multiclass_log_loss"], 0.0, abs_tol=1e-15)


def test_zero_actual_probability(isolated_data_dir):
    summary = evaluate_classification_group(
        [_row(probabilities={"home_win": 100, "draw": 0, "away_win": 0}, actual_outcome="away_win")]
    )

    assert summary["accuracy"] == 0.0
    assert math.isclose(summary["brier_score"], 2.0, rel_tol=1e-12)
    assert math.isclose(summary["multiclass_log_loss"], -math.log(1e-15), rel_tol=1e-9)
    assert math.isfinite(summary["multiclass_log_loss"])


# ---------------------------------------------------------------------------
# 取整归一化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "probabilities",
    [
        {"home_win": 34, "draw": 33, "away_win": 34},   # total 101
        {"home_win": 33, "draw": 33, "away_win": 33},   # total 99
    ],
)
def test_rounded_totals_normalized_to_one(isolated_data_dir, probabilities):
    classes, normalized = normalize_class_probabilities(_row(probabilities=probabilities))

    assert classes == FOOTBALL_CLASSES
    assert math.isclose(sum(normalized), 1.0, rel_tol=1e-12)
    assert all(0.0 <= p <= 1.0 for p in normalized)

    summary = evaluate_classification_group([_row(probabilities=probabilities)])
    assert summary["sample_count"] == 1
    assert 0.0 <= summary["brier_score"] <= 2.0


def test_rounded_total_101_is_not_used_raw(isolated_data_dir):
    """必须先重归一化，而不是直接在 1.01 的分布上算指标。"""
    _classes, normalized = normalize_class_probabilities(
        _row(probabilities={"home_win": 34, "draw": 33, "away_win": 34})
    )
    assert math.isclose(normalized[0], 34 / 101, rel_tol=1e-12)
    assert not math.isclose(normalized[0], 0.34, rel_tol=1e-6)


def test_invalid_total_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(_row(probabilities={"home_win": 60, "draw": 20, "away_win": 10}))

    assert excinfo.value.reason == "invalid_probability_total"


def test_fraction_scale_rejected(isolated_data_dir):
    """历史 schema 是百分点；0..1 小数属于畸形，不做自动判别。"""
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(_row(probabilities={"home_win": 0.6, "draw": 0.2, "away_win": 0.2}))

    assert excinfo.value.reason == "invalid_probability_total"


# ---------------------------------------------------------------------------
# 概率值校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad",
    [None, "55", True, False, -1, 101, 150.5, float("nan"), float("inf"), float("-inf"), [], {}],
)
def test_invalid_probability_values(isolated_data_dir, bad):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(
            _row(probabilities={"home_win": bad, "draw": 20, "away_win": 20})
        )

    assert excinfo.value.reason in {"invalid_probability", "missing_probability"}


@pytest.mark.parametrize("missing", ["home_win", "draw", "away_win"])
def test_missing_football_class_rejected(isolated_data_dir, missing):
    probabilities = {k: v for k, v in FOOTBALL_60_20_20.items() if k != missing}

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(_row(probabilities=probabilities))

    assert excinfo.value.reason == "missing_probability"


@pytest.mark.parametrize("missing", ["home_win", "away_win"])
def test_missing_basketball_class_rejected(isolated_data_dir, missing):
    probabilities = {"home_win": 60, "draw": 0, "away_win": 40}
    del probabilities[missing]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(_basketball_row(probabilities=probabilities))

    assert excinfo.value.reason == "missing_probability"


def test_missing_probability_block_rejected(isolated_data_dir):
    row = _row()
    del row["model_probabilities"]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(row)

    assert excinfo.value.reason == "missing_probability"


def test_unsupported_sport_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(_row(sport="tennis"))

    assert excinfo.value.reason == "unsupported_sport"
    assert classes_for_sport("football") == FOOTBALL_CLASSES
    assert classes_for_sport("basketball") == BASKETBALL_CLASSES


# ---------------------------------------------------------------------------
# 篮球专属规则
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "probabilities",
    [
        {"home_win": 60, "away_win": 40},          # draw 缺失
        {"home_win": 60, "draw": 0, "away_win": 40},
        {"home_win": 60, "draw": 0.0, "away_win": 40},
    ],
)
def test_basketball_draw_compatibility_field_accepted(isolated_data_dir, probabilities):
    classes, normalized = normalize_class_probabilities(_basketball_row(probabilities=probabilities))

    assert classes == BASKETBALL_CLASSES
    assert len(normalized) == 2
    assert math.isclose(sum(normalized), 1.0, rel_tol=1e-12)


def test_basketball_nonzero_draw_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        normalize_class_probabilities(
            _basketball_row(probabilities={"home_win": 60, "draw": 5, "away_win": 40})
        )

    assert excinfo.value.reason == "nonzero_basketball_draw_probability"


def test_basketball_draw_result_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_classification_group([_basketball_row(actual_outcome="draw")])

    assert excinfo.value.reason == "invalid_actual_outcome"


def test_football_draw_result_accepted(isolated_data_dir):
    summary = evaluate_classification_group([_row(actual_outcome="draw")])

    assert summary["accuracy"] == 0.0
    assert summary["sample_count"] == 1


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

    first = evaluate_classification_group([base])
    second = evaluate_classification_group([noisy])

    assert first["accuracy"] == second["accuracy"]
    assert math.isclose(first["brier_score"], second["brier_score"], rel_tol=1e-12)
    assert math.isclose(first["multiclass_log_loss"], second["multiclass_log_loss"], rel_tol=1e-12)


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

    first = evaluate_classification_group([base])
    second = evaluate_classification_group([noisy])

    assert first == second


# ---------------------------------------------------------------------------
# 分组与完整性
# ---------------------------------------------------------------------------

def test_duplicate_evaluation_id_rejected(isolated_data_dir):
    rows = [
        _row(evaluation_id="dup", probabilities=FOOTBALL_60_20_20),
        _row(evaluation_id="dup", probabilities=FOOTBALL_20_30_50),
    ]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_classification_group(rows)

    assert excinfo.value.reason == "duplicate_evaluation_id"
    assert excinfo.value.evaluation_id == "dup"


def test_mixed_sport_group_rejected(isolated_data_dir):
    rows = [_row(evaluation_id="f1"), _basketball_row(evaluation_id="b1")]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_classification_group(rows)

    assert excinfo.value.reason == "mixed_group"


def test_mixed_model_version_group_rejected(isolated_data_dir):
    rows = [
        _row(evaluation_id="f1"),
        _row(evaluation_id="f2", model_version="model-v2"),
    ]

    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_classification_group(rows)

    assert excinfo.value.reason == "mixed_group"


def test_empty_group_rejected(isolated_data_dir):
    with pytest.raises(ClassificationEvaluationError) as excinfo:
        evaluate_classification_group([])

    assert excinfo.value.reason == "empty_group"


def test_empty_dataset_returns_empty_list(isolated_data_dir):
    assert build_classification_summaries([]) == []


def test_automatic_grouping(isolated_data_dir):
    rows = [
        _row(evaluation_id="f1"),
        _row(evaluation_id="f2", model_version="model-v2", probabilities=FOOTBALL_20_30_50),
        _basketball_row(evaluation_id="b1"),
    ]

    summaries = build_classification_summaries(rows)

    assert len(summaries) == 3
    keys = [(s["sport"], s["model_name"], s["model_version"]) for s in summaries]
    assert keys == sorted(keys)
    assert all(s["sample_count"] == 1 for s in summaries)
    # 足球三分类、篮球二分类，不产生跨运动混合指标
    by_sport = {s["sport"]: s for s in summaries}
    assert by_sport["football"]["classes"] == list(FOOTBALL_CLASSES)
    assert by_sport["basketball"]["classes"] == list(BASKETBALL_CLASSES)


def test_deterministic_ordering(isolated_data_dir):
    rows = [
        _basketball_row(evaluation_id="b1"),
        _row(evaluation_id="f2", model_version="model-v2", probabilities=FOOTBALL_20_30_50),
        _row(evaluation_id="f1"),
    ]

    forward = build_classification_summaries(rows)
    backward = build_classification_summaries(list(reversed(rows)))

    assert forward == backward
    keys = [(s["sport"], s["model_name"], s["model_version"]) for s in forward]
    assert keys == [
        ("basketball", BASKETBALL_MODEL, "baseline-1"),
        ("football", FOOTBALL_MODEL, "baseline-1"),
        ("football", FOOTBALL_MODEL, "model-v2"),
    ]


# ---------------------------------------------------------------------------
# 并列裁决
# ---------------------------------------------------------------------------

def test_tie_resolution_prefers_first_class(isolated_data_dir):
    tied = {"home_win": 40, "draw": 40, "away_win": 20}

    hit = evaluate_classification_group([_row(probabilities=tied, actual_outcome="home_win")])
    assert hit["accuracy"] == 1.0

    miss = evaluate_classification_group([_row(probabilities=tied, actual_outcome="draw")])
    assert miss["accuracy"] == 0.0


def test_basketball_tie_resolution(isolated_data_dir):
    tied = {"home_win": 50, "draw": 0, "away_win": 50}

    hit = evaluate_classification_group([_basketball_row(probabilities=tied, actual_outcome="home_win")])
    assert hit["accuracy"] == 1.0


# ---------------------------------------------------------------------------
# 纯度与不可变性
# ---------------------------------------------------------------------------

def test_input_rows_not_mutated(isolated_data_dir):
    rows = [
        _row(evaluation_id="a", probabilities=FOOTBALL_60_20_20, actual_outcome="home_win",
             display_probabilities={"home_win": 1, "draw": 1, "away_win": 98}),
        _row(evaluation_id="b", probabilities=FOOTBALL_20_30_50, actual_outcome="away_win"),
    ]
    before = copy.deepcopy(rows)

    build_classification_summaries(rows)
    evaluate_classification_group(rows[:1])

    assert rows == before


def test_repeated_evaluation_is_deterministic(isolated_data_dir):
    rows = [_row(evaluation_id="a"), _row(evaluation_id="b", probabilities=FOOTBALL_20_30_50)]

    assert build_classification_summaries(rows) == build_classification_summaries(rows)


def test_summary_has_no_extra_fields(isolated_data_dir):
    summary = evaluate_classification_group([_row()])

    assert set(summary) == {
        "sport", "model_name", "model_version", "probability_source",
        "classes", "sample_count", "accuracy", "brier_score", "multiclass_log_loss",
    }
    for forbidden in ("roi", "clv", "calibration", "rank", "grade", "winner", "best",
                      "precision", "recall", "f1", "auc", "confusion"):
        assert forbidden not in summary


# ---------------------------------------------------------------------------
# 便捷入口（读取既有评估样本存储）
# ---------------------------------------------------------------------------

def test_convenience_function_matches_direct_evaluation(isolated_data_dir, make_match):
    from utils.daily_loader import enrich_match
    from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
    from utils.prediction_snapshots import capture_snapshot
    from utils.settlements import settle_snapshot

    match = enrich_match(make_match(id="m-conv"))
    snapshot, created = capture_snapshot(match)
    assert created is True
    settlement, settled = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                       score={"ft": [2, 1]})
    )
    assert settled is True
    capture_evaluation_row(snapshot, settlement)

    summaries = evaluate_all_classification()

    assert summaries == build_classification_summaries(get_all_evaluation_rows())
    assert len(summaries) == 1
    assert summaries[0]["sample_count"] == 1
    assert summaries[0]["sport"] == "football"
    assert summaries[0]["probability_source"] == PROBABILITY_SOURCE
