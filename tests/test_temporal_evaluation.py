"""
时序模型评估核心的行为测试。

覆盖：空输入 / 空组、窗口参数校验与排序、参考时间、边界包含、
嵌套窗口计数、kickoff 校验、时区等价与混用拒绝、输入顺序无关性、
all-time 与逐窗口的「与既有评估器数值完全一致」、
类别空间、模型版本隔离、概率来源隔离、既有概率校验、
重复 evaluation_id、混合分组、输入不可变性、便捷入口。
"""
from __future__ import annotations

import copy
import random
from datetime import datetime

import pytest

import config
from utils import evaluation_rows as evaluation_rows_module
from utils import temporal_evaluation
from utils.atomic_json import atomic_write_json
from utils.calibration_evaluation import evaluate_calibration_group
from utils.classification_evaluation import (
    ClassificationEvaluationError,
    evaluate_classification_group,
)
from utils.evaluation_rows import get_all_evaluation_rows
from utils.temporal_evaluation import (
    DEFAULT_WINDOW_DAYS,
    TemporalEvaluationError,
    build_temporal_summaries,
    evaluate_all_temporal,
    evaluate_temporal_group,
    parse_kickoff,
    select_trailing_window,
    validate_window_days,
)

FOOTBALL_MODEL = "elo-poisson-dixon-coles"
BASKETBALL_MODEL = "elo-normal-points"

FOOTBALL_50_30_20 = {"home_win": 50, "draw": 30, "away_win": 20}
BASKETBALL_60_40 = {"home_win": 60, "away_win": 40}


# ---------------------------------------------------------------------------
# 合成评估样本
# ---------------------------------------------------------------------------

def _row(
    *,
    evaluation_id: str,
    kickoff_at: str,
    sport: str = "football",
    model_name: str = FOOTBALL_MODEL,
    model_version: str = "football-coldstart-1",
    probabilities: dict | None = None,
    actual_outcome: str = "home_win",
    **extra,
) -> dict:
    if probabilities is None:
        probabilities = (
            dict(BASKETBALL_60_40) if sport == "basketball" else dict(FOOTBALL_50_30_20)
        )
    row = {
        "evaluation_id": evaluation_id,
        "match_id": f"m-{evaluation_id}",
        "sport": sport,
        "model_name": model_name,
        "model_version": model_version,
        "kickoff_at": kickoff_at,
        "model_probabilities": probabilities,
        "actual_outcome": actual_outcome,
    }
    row.update(extra)
    return row


def _football_rows(kickoffs, *, version="football-coldstart-1", model=FOOTBALL_MODEL):
    """由 (kickoff, outcome, probabilities) 元组构造足球行。"""
    rows = []
    for index, spec in enumerate(kickoffs):
        kickoff, outcome = spec[0], spec[1]
        probs = spec[2] if len(spec) > 2 else None
        rows.append(
            _row(
                evaluation_id=f"f{index}",
                kickoff_at=kickoff,
                model_name=model,
                model_version=version,
                actual_outcome=outcome,
                probabilities=probs,
            )
        )
    return rows


REF = "2030-04-01T12:00:00+00:00"

# 参考时间内 / 外的嵌套分布（相对参考时间的天数已手工核验）。
# 有意按**时序升序**书写：与模块的规范时序一致，因此与既有评估器的
# 数值一致性比较是确定性的（浮点求和与加数顺序有关）。
NESTED_SPECS = [
    ("2029-06-01T12:00:00+00:00", "home_win"),   # -304d          all
    ("2029-12-01T12:00:00+00:00", "away_win"),   # -121d          180/all
    ("2030-01-01T12:00:00+00:00", "draw"),       # -90d（恰好）    90/180/all
    ("2030-02-20T12:00:00+00:00", "home_win"),   # -40d           90/180/all
    ("2030-03-02T12:00:00+00:00", "draw"),       # -30d（恰好）    30/90/180/all
    ("2030-03-15T12:00:00+00:00", "away_win"),   # -17d           30/90/180/all
    ("2030-04-01T12:00:00+00:00", "home_win"),   # ref            30/90/180/all
]
NESTED_ROWS = _football_rows(NESTED_SPECS)


def _window(result, days):
    for record in result["windows"]:
        if record["window_days"] == days:
            return record
    raise AssertionError(f"未找到 {days} 天窗口")


def _rows_with_kickoffs(kickoffs):
    return [row for row in NESTED_ROWS if row["kickoff_at"] in kickoffs]


# ---------------------------------------------------------------------------
# 空输入
# ---------------------------------------------------------------------------

def test_build_temporal_summaries_empty_returns_empty_list():
    assert build_temporal_summaries([]) == []


def test_direct_empty_group_preserves_empty_group_failure():
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group([])
    assert exc_info.value.reason == "empty_group"


# ---------------------------------------------------------------------------
# 窗口参数
# ---------------------------------------------------------------------------

def test_default_windows_are_30_90_180_ascending():
    assert DEFAULT_WINDOW_DAYS == (30, 90, 180)
    result = evaluate_temporal_group(NESTED_ROWS)
    assert [w["window_days"] for w in result["windows"]] == [30, 90, 180]


def test_custom_window_order_is_normalized_to_ascending():
    default_result = evaluate_temporal_group(NESTED_ROWS)
    custom_result = evaluate_temporal_group(NESTED_ROWS, window_days=(180, 30, 90))

    assert [w["window_days"] for w in custom_result["windows"]] == [30, 90, 180]
    assert custom_result["windows"] == default_result["windows"]
    assert custom_result["all_time"] == default_result["all_time"]


@pytest.mark.parametrize(
    "bad",
    [
        0,
        -1,
        30.0,
        True,
        "30",
        (30, 30),
        (),
        None,
        (30, 0),
    ],
)
def test_invalid_window_values_rejected(bad):
    with pytest.raises(TemporalEvaluationError) as exc_info:
        validate_window_days(bad)
    assert exc_info.value.reason == "invalid_window_days"

    with pytest.raises(TemporalEvaluationError) as exc_info2:
        evaluate_temporal_group(NESTED_ROWS, window_days=bad)
    assert exc_info2.value.reason == "invalid_window_days"


def test_validate_window_days_returns_sorted_tuple():
    assert validate_window_days((180, 30, 90)) == (30, 90, 180)
    assert validate_window_days([7]) == (7,)


# ---------------------------------------------------------------------------
# 参考时间与时间坐标
# ---------------------------------------------------------------------------

def test_reference_is_max_kickoff():
    rows = _football_rows(
        [
            ("2030-01-01T00:00:00+00:00", "home_win"),
            ("2030-02-01T00:00:00+00:00", "away_win"),
            ("2030-04-01T00:00:00+00:00", "draw"),
        ]
    )
    result = evaluate_temporal_group(rows)
    assert result["reference_kickoff_at"] == "2030-04-01T00:00:00+00:00"


def test_reference_independent_of_input_order():
    rows = _football_rows(
        [
            ("2030-01-01T00:00:00+00:00", "home_win"),
            ("2030-02-01T00:00:00+00:00", "away_win"),
            ("2030-04-01T00:00:00+00:00", "draw"),
        ]
    )
    reversed_rows = list(reversed(rows))
    assert (
        evaluate_temporal_group(rows)["reference_kickoff_at"]
        == evaluate_temporal_group(reversed_rows)["reference_kickoff_at"]
        == "2030-04-01T00:00:00+00:00"
    )


def test_no_wall_clock_dependency(monkeypatch):
    """把 datetime.now() 变成硬失败：任何系统时钟依赖都会被立刻暴露。"""

    class _NoWallClock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            raise AssertionError("时序评估不得使用系统时钟")

        @classmethod
        def utcnow(cls):  # noqa: D102
            raise AssertionError("时序评估不得使用系统时钟")

    monkeypatch.setattr(temporal_evaluation, "datetime", _NoWallClock)

    result = evaluate_temporal_group(NESTED_ROWS)
    assert result["reference_kickoff_at"] == REF
    assert [w["window_days"] for w in result["windows"]] == [30, 90, 180]


def test_naive_timestamps_supported_and_deterministic():
    rows = _football_rows(
        [
            ("2030-01-01T00:00:00", "home_win"),
            ("2030-04-01T12:00:00", "draw"),
        ]
    )
    result = evaluate_temporal_group(rows)
    assert result["reference_kickoff_at"] == "2030-04-01T12:00:00"
    assert result["all_time"]["last_sample_kickoff_at"] == "2030-04-01T12:00:00"


# ---------------------------------------------------------------------------
# 窗口边界
# ---------------------------------------------------------------------------

def test_exact_boundary_inclusion():
    rows = _football_rows(
        [
            ("2030-04-01T12:00:00+00:00", "home_win"),  # reference
            ("2030-03-02T12:00:00+00:00", "draw"),      # 恰好 30 天前 -> 包含
            ("2030-03-02T11:59:59+00:00", "away_win"),  # 早一秒 -> 排除
        ]
    )
    result = evaluate_temporal_group(rows)

    assert result["reference_kickoff_at"] == REF
    window_30 = _window(result, 30)
    assert window_30["window_start_at"] == "2030-03-02T12:00:00+00:00"
    assert window_30["window_end_at"] == REF
    assert window_30["sample_count"] == 2
    assert window_30["first_sample_kickoff_at"] == "2030-03-02T12:00:00+00:00"

    # 早一秒的记录仍在 90 天窗口与全部历史中
    assert _window(result, 90)["sample_count"] == 3
    assert result["all_time"]["sample_count"] == 3


def test_nested_window_counts():
    result = evaluate_temporal_group(NESTED_ROWS)

    counts = {w["window_days"]: w["sample_count"] for w in result["windows"]}
    assert counts == {30: 3, 90: 5, 180: 6}
    assert counts[30] < counts[90] < counts[180] <= result["all_time"]["sample_count"]
    assert result["all_time"]["sample_count"] == 7


def test_select_trailing_window_is_inclusive_and_pure():
    entries = temporal_evaluation._prepare_entries(NESTED_ROWS)
    reference = entries[-1][0]

    assert len(select_trailing_window(entries, reference, 30)) == 3
    assert len(select_trailing_window(entries, reference, 90)) == 5
    assert len(select_trailing_window(entries, reference, 180)) == 6

    with pytest.raises(TemporalEvaluationError):
        select_trailing_window(entries, reference, 0)


# ---------------------------------------------------------------------------
# 时间戳校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345, [], "2030-13-45T00:00:00"])
def test_invalid_kickoff_rejected(bad):
    with pytest.raises(TemporalEvaluationError) as exc_info:
        parse_kickoff(bad)
    assert exc_info.value.reason == "invalid_kickoff_at"


def test_invalid_kickoff_in_group_fails_loudly():
    rows = [
        _row(evaluation_id="ok", kickoff_at="2030-01-01T00:00:00+00:00"),
        _row(evaluation_id="bad", kickoff_at="not-a-date"),
    ]
    with pytest.raises(TemporalEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "invalid_kickoff_at"


# ---------------------------------------------------------------------------
# 时区
# ---------------------------------------------------------------------------

def test_timezone_offsets_are_absolute_instants():
    rows = _football_rows(
        [
            ("2030-05-01T13:00:00+08:00", "home_win"),  # 05:00Z（更早，但字符串更大）
            ("2030-05-01T20:00:00+08:00", "draw"),      # 12:00Z
            ("2030-05-01T12:00:00+00:00", "away_win"),  # 12:00Z（同一瞬时）
        ]
    )
    result = evaluate_temporal_group(rows)

    # 参考时间是绝对最新的瞬时，而不是字典序最大的字符串
    assert result["reference_kickoff_at"] == "2030-05-01T12:00:00+00:00"
    assert result["all_time"]["first_sample_kickoff_at"] == "2030-05-01T05:00:00+00:00"
    assert result["all_time"]["last_sample_kickoff_at"] == "2030-05-01T12:00:00+00:00"
    assert result["all_time"]["sample_count"] == 3


def test_equivalent_offsets_produce_identical_output():
    a = evaluate_temporal_group(
        _football_rows([("2030-05-01T20:00:00+08:00", "home_win")])
    )
    b = evaluate_temporal_group(
        _football_rows([("2030-05-01T12:00:00+00:00", "home_win")])
    )
    assert a == b


def test_mixed_naive_and_aware_rejected():
    rows = [
        _row(evaluation_id="naive", kickoff_at="2030-05-01T20:00:00"),
        _row(evaluation_id="aware", kickoff_at="2030-05-01T20:00:00+08:00"),
    ]
    with pytest.raises(TemporalEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "inconsistent_kickoff_timezone"


# ---------------------------------------------------------------------------
# 顺序无关性
# ---------------------------------------------------------------------------

def test_input_order_independence():
    chronological = list(NESTED_ROWS)
    reverse = list(reversed(chronological))

    shuffled = list(chronological)
    random.Random(20261001).shuffle(shuffled)

    baseline = evaluate_temporal_group(chronological)
    assert evaluate_temporal_group(reverse) == baseline
    assert evaluate_temporal_group(shuffled) == baseline


# ---------------------------------------------------------------------------
# 与既有评估器的数值一致性
# ---------------------------------------------------------------------------

def test_all_time_classification_equals_direct_evaluator():
    result = evaluate_temporal_group(NESTED_ROWS)
    direct = evaluate_classification_group(NESTED_ROWS)

    all_time = result["all_time"]
    assert all_time["sample_count"] == direct["sample_count"]
    assert all_time["accuracy"] == direct["accuracy"]
    assert all_time["brier_score"] == direct["brier_score"]
    assert all_time["multiclass_log_loss"] == direct["multiclass_log_loss"]


def test_all_time_calibration_equals_direct_evaluator():
    result = evaluate_temporal_group(NESTED_ROWS)
    direct = evaluate_calibration_group(NESTED_ROWS)

    all_time = result["all_time"]
    assert all_time["sample_count"] == direct["sample_count"]
    assert (
        all_time["macro_expected_calibration_error"]
        == direct["macro_expected_calibration_error"]
    )
    for cls, ece in all_time["class_expected_calibration_error"].items():
        assert ece == direct["class_calibration"][cls]["expected_calibration_error"]


def test_metrics_use_canonical_order_regardless_of_input_order():
    """
    数值一致性契约：指标在**规范时序**上求值，因此
    与既有评估器在规范时序下的比较逐位一致，且与调用方传入顺序无关。
    """
    canonical = evaluate_temporal_group(NESTED_ROWS)

    shuffled_rows = list(NESTED_ROWS)
    random.Random(4242).shuffle(shuffled_rows)
    shuffled = evaluate_temporal_group(shuffled_rows)

    direct = evaluate_classification_group(NESTED_ROWS)

    assert shuffled["all_time"] == canonical["all_time"]
    assert canonical["all_time"]["accuracy"] == direct["accuracy"]
    assert canonical["all_time"]["brier_score"] == direct["brier_score"]
    assert canonical["all_time"]["multiclass_log_loss"] == direct["multiclass_log_loss"]


@pytest.mark.parametrize("days", [30, 90, 180])
def test_window_metrics_equal_direct_evaluators(days):
    result = evaluate_temporal_group(NESTED_ROWS)
    window = _window(result, days)

    if days == 30:
        subset = _rows_with_kickoffs(
            {
                "2030-04-01T12:00:00+00:00",
                "2030-03-15T12:00:00+00:00",
                "2030-03-02T12:00:00+00:00",
            }
        )
    elif days == 90:
        subset = _rows_with_kickoffs(
            {
                "2030-04-01T12:00:00+00:00",
                "2030-03-15T12:00:00+00:00",
                "2030-03-02T12:00:00+00:00",
                "2030-02-20T12:00:00+00:00",
                "2030-01-01T12:00:00+00:00",
            }
        )
    else:
        subset = _rows_with_kickoffs(
            {
                "2030-04-01T12:00:00+00:00",
                "2030-03-15T12:00:00+00:00",
                "2030-03-02T12:00:00+00:00",
                "2030-02-20T12:00:00+00:00",
                "2030-01-01T12:00:00+00:00",
                "2029-12-01T12:00:00+00:00",
            }
        )

    assert window["sample_count"] == len(subset)

    classification = evaluate_classification_group(subset)
    assert window["accuracy"] == classification["accuracy"]
    assert window["brier_score"] == classification["brier_score"]
    assert window["multiclass_log_loss"] == classification["multiclass_log_loss"]

    calibration = evaluate_calibration_group(subset)
    assert (
        window["macro_expected_calibration_error"]
        == calibration["macro_expected_calibration_error"]
    )
    for cls, ece in window["class_expected_calibration_error"].items():
        assert ece == calibration["class_calibration"][cls]["expected_calibration_error"]


# ---------------------------------------------------------------------------
# 类别空间
# ---------------------------------------------------------------------------

def test_football_class_mapping():
    result = evaluate_temporal_group(NESTED_ROWS)
    assert set(result["all_time"]["class_expected_calibration_error"]) == {
        "home_win",
        "draw",
        "away_win",
    }
    for window in result["windows"]:
        assert set(window["class_expected_calibration_error"]) == {
            "home_win",
            "draw",
            "away_win",
        }


def test_basketball_class_mapping():
    rows = [
        _row(
            evaluation_id=f"b{i}",
            kickoff_at=f"2030-0{i + 1}-01T12:00:00+00:00",
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            model_version="basketball-coldstart-1",
            actual_outcome="home_win",
        )
        for i in range(3)
    ]
    result = evaluate_temporal_group(rows)
    assert set(result["all_time"]["class_expected_calibration_error"]) == {
        "home_win",
        "away_win",
    }


# ---------------------------------------------------------------------------
# 分组隔离
# ---------------------------------------------------------------------------

def test_football_model_version_isolation():
    ad_v1 = _football_rows(
        [
            ("2030-01-01T00:00:00+00:00", "home_win"),
            ("2030-01-05T00:00:00+00:00", "away_win"),
        ],
        version="football-ad-1",
    )
    cold = _football_rows(
        [
            ("2030-03-01T00:00:00+00:00", "draw"),
            ("2030-03-02T00:00:00+00:00", "home_win"),
            ("2030-03-03T00:00:00+00:00", "draw"),
        ],
        version="football-coldstart-1",
    )

    summaries = build_temporal_summaries(ad_v1 + cold)
    assert len(summaries) == 2

    by_version = {s["model_version"]: s for s in summaries}
    assert set(by_version) == {"football-ad-1", "football-coldstart-1"}

    assert by_version["football-ad-1"]["all_time"]["sample_count"] == 2
    assert by_version["football-coldstart-1"]["all_time"]["sample_count"] == 3
    # 两个版本的参考时间各自独立
    assert by_version["football-ad-1"]["reference_kickoff_at"] == "2030-01-05T00:00:00+00:00"
    assert (
        by_version["football-coldstart-1"]["reference_kickoff_at"]
        == "2030-03-03T00:00:00+00:00"
    )

    # 升序排列：ad-1 < coldstart-1
    assert [s["model_version"] for s in summaries] == [
        "football-ad-1",
        "football-coldstart-1",
    ]


def test_basketball_model_version_isolation():
    baseline = [
        _row(
            evaluation_id="bb-base",
            kickoff_at="2030-02-01T00:00:00+00:00",
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            model_version="baseline-1",
            actual_outcome="away_win",
        )
    ]
    coldstart = [
        _row(
            evaluation_id="bb-cold",
            kickoff_at="2030-03-01T00:00:00+00:00",
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            model_version="basketball-coldstart-1",
            actual_outcome="home_win",
        ),
        _row(
            evaluation_id="bb-cold2",
            kickoff_at="2030-03-02T00:00:00+00:00",
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            model_version="basketball-coldstart-1",
            actual_outcome="away_win",
        ),
    ]

    summaries = build_temporal_summaries(baseline + coldstart)
    by_version = {s["model_version"]: s for s in summaries}
    assert set(by_version) == {"baseline-1", "basketball-coldstart-1"}
    assert by_version["baseline-1"]["all_time"]["sample_count"] == 1
    assert by_version["basketball-coldstart-1"]["all_time"]["sample_count"] == 2


def test_sport_isolation():
    football = _football_rows([("2030-01-01T00:00:00+00:00", "home_win")])
    basketball = [
        _row(
            evaluation_id="bb",
            kickoff_at="2030-01-02T00:00:00+00:00",
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            model_version="basketball-coldstart-1",
        )
    ]
    summaries = build_temporal_summaries(football + basketball)
    assert [s["sport"] for s in summaries] == ["basketball", "football"]


# ---------------------------------------------------------------------------
# 概率来源隔离与既有校验
# ---------------------------------------------------------------------------

def test_probability_source_isolation():
    base = _football_rows([("2030-01-01T00:00:00+00:00", "home_win")])

    perturbed = copy.deepcopy(base)
    perturbed[0]["display_probabilities"] = {"home_win": 90, "draw": 5, "away_win": 5}
    perturbed[0]["market_odds"] = {"home_win": 1.01}
    perturbed[0]["market_implied_probabilities"] = {"home_win": 99, "draw": 1}
    perturbed[0]["expected_values"] = {"home_win": 999.0}

    assert evaluate_temporal_group(base) == evaluate_temporal_group(perturbed)


@pytest.mark.parametrize(
    "probabilities,expected_reason",
    [
        ({"home_win": 50, "draw": 30}, "missing_probability"),
        ({"home_win": 50, "draw": 30, "away_win": "x"}, "invalid_probability"),
        ({"home_win": 50, "draw": 30, "away_win": 5}, "invalid_probability_total"),
    ],
)
def test_existing_probability_validation_is_reused(probabilities, expected_reason):
    rows = [_row(evaluation_id="p1", kickoff_at=REF, probabilities=probabilities)]
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == expected_reason


def test_nonzero_basketball_draw_rejected():
    rows = [
        _row(
            evaluation_id="bb",
            kickoff_at=REF,
            sport="basketball",
            model_name=BASKETBALL_MODEL,
            probabilities={"home_win": 60, "draw": 5, "away_win": 35},
        )
    ]
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "nonzero_basketball_draw_probability"


def test_invalid_actual_outcome_rejected():
    rows = [_row(evaluation_id="o1", kickoff_at=REF, actual_outcome="nonsense")]
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "invalid_actual_outcome"


# ---------------------------------------------------------------------------
# 分组校验
# ---------------------------------------------------------------------------

def test_duplicate_evaluation_id_rejected():
    rows = [
        _row(evaluation_id="dup", kickoff_at="2030-01-01T00:00:00+00:00"),
        _row(evaluation_id="dup", kickoff_at="2030-02-01T00:00:00+00:00"),
    ]
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "duplicate_evaluation_id"


def test_mixed_group_rejected():
    rows = [
        _row(evaluation_id="a", kickoff_at="2030-01-01T00:00:00+00:00", model_version="v1"),
        _row(evaluation_id="b", kickoff_at="2030-02-01T00:00:00+00:00", model_version="v2"),
    ]
    with pytest.raises(ClassificationEvaluationError) as exc_info:
        evaluate_temporal_group(rows)
    assert exc_info.value.reason == "mixed_group"


# ---------------------------------------------------------------------------
# 不可变性
# ---------------------------------------------------------------------------

def test_input_rows_are_not_mutated():
    rows = _football_rows(
        [
            ("2030-01-01T00:00:00+00:00", "home_win"),
            ("2030-03-01T00:00:00+00:00", "away_win"),
            ("2030-04-01T00:00:00+00:00", "draw"),
        ]
    )
    snapshot = copy.deepcopy(rows)

    evaluate_temporal_group(rows)
    build_temporal_summaries(rows)

    assert rows == snapshot


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def test_evaluate_all_temporal_matches_pure_builder(isolated_data_dir):
    rows = _football_rows(
        [
            ("2030-01-01T00:00:00+00:00", "home_win"),
            ("2030-03-01T00:00:00+00:00", "away_win"),
            ("2030-04-01T00:00:00+00:00", "draw"),
        ]
    )
    store = {
        "version": 1,
        "rows": {row["evaluation_id"]: row for row in rows},
    }
    atomic_write_json(evaluation_rows_module.store_path(), store)

    loaded = get_all_evaluation_rows()
    assert len(loaded) == 3

    assert evaluate_all_temporal() == build_temporal_summaries(loaded)
    assert evaluate_all_temporal(window_days=(90,)) == build_temporal_summaries(
        loaded, window_days=(90,)
    )


def test_evaluate_all_temporal_empty_store(isolated_data_dir):
    assert config.EVALUATION_ROW_FILE == "evaluation_rows.json"
    assert evaluate_all_temporal() == []
