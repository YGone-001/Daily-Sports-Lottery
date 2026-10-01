"""
概率校准诊断
============
在**不可变评估样本**（`data/evaluation_rows.json`）之上，回答一个与分类指标不同的问题：

    当历史模型给某个结果分配了概率 p 时，该结果实际发生了多频繁？

架构边界
--------
只消费评估样本行；不读 daily_matches / prediction_snapshots / settlements /
当前 Elo / 当前盘口，也不调用 `predict_match` / `enrich_match`。
本模块是纯诊断：**不修改任何概率**，不拟合任何校准模型，不产出校准后的概率。

概率来源（强制）
----------------
只使用 `evaluation_row["model_probabilities"]`（历史 0..100 百分点），
不使用 display_probabilities / market_implied_probabilities / market_odds /
expected_values，也不做任何概率融合。

校验复用
--------
概率值、概率总和、篮球 draw、实际结果的校验语义统一复用
`utils.classification_evaluation`（`validate_group` / `validate_row` /
`normalize_class_probabilities` / `classes_for_sport`），不另立一套解释。

诊断定义
--------
对每个类别做 one-vs-rest 校准：每条评估样本对每个必需类别贡献一个
`(预测概率 p_k, 观测结果 y_k)` 二元观测。

- 固定 10 个等宽分箱，`index = min(int(p * 10), 9)`（最后一箱包含 1.0）。
- `mean_predicted_probability` = 箱内预测概率均值（置信度）。
- `observed_frequency` = 箱内观测结果均值（实际频率）。
- `calibration_gap` = |置信度 - 实际频率|。
- 类别 ECE = Σ_b (n_b / N) * |confidence_b - frequency_b|（空箱贡献 0）。
- macro ECE = 该运动必需类别 ECE 的算术平均。

诊断结果仅为观察值，不含任何评级 / 分数 / 排名。
"""
from __future__ import annotations

from typing import Iterable, Sequence

from utils.classification_evaluation import (
    PROBABILITY_SOURCE,
    classes_for_sport,
    validate_group,
    validate_row,
)

# 固定等宽分箱数量（不做自适应 / 分位数分箱）
BIN_COUNT = 10


def bin_index(probability: float) -> int:
    """
    确定性分箱：`min(int(p * 10), 9)`，最后一箱包含 1.0。

    0.00 -> 0 ; 0.09 -> 0 ; 0.10 -> 1 ; 0.59 -> 5 ; 0.60 -> 6 ; 0.90 -> 9 ; 1.00 -> 9
    """
    return min(int(probability * BIN_COUNT), BIN_COUNT - 1)


def bin_bounds(index: int) -> tuple[float, float]:
    """
    分箱边界。0..8 号为 [lower, upper)，9 号为 [0.9, 1.0]。
    """
    return index / BIN_COUNT, (index + 1) / BIN_COUNT


def build_class_calibration(
    predicted: Sequence[float],
    observed: Sequence[int],
) -> dict:
    """
    汇总单个类别的 one-vs-rest 校准观测。

    `predicted` 为该类别在每条样本上的归一化预测概率，
    `observed` 为对应的二元观测结果（1 = 实际就是该类别）。

    返回 `{"expected_calibration_error": float, "bins": [10 个分箱]}`。
    空箱的均值 / 频率 / 差距为 `None`（不使用 NaN）。
    """
    if len(predicted) != len(observed):
        raise ValueError("predicted 与 observed 长度必须一致")

    counts = [0] * BIN_COUNT
    predicted_sums = [0.0] * BIN_COUNT
    observed_sums = [0] * BIN_COUNT

    for probability, outcome in zip(predicted, observed):
        index = bin_index(probability)
        counts[index] += 1
        predicted_sums[index] += probability
        observed_sums[index] += outcome

    bins: list[dict] = []
    expected_calibration_error = 0.0
    total = len(predicted)

    for index in range(BIN_COUNT):
        lower, upper = bin_bounds(index)
        count = counts[index]
        if count == 0:
            bins.append(
                {
                    "bin_index": index,
                    "lower_bound": lower,
                    "upper_bound": upper,
                    "count": 0,
                    "mean_predicted_probability": None,
                    "observed_frequency": None,
                    "calibration_gap": None,
                }
            )
            continue

        mean_predicted = predicted_sums[index] / count
        observed_frequency = observed_sums[index] / count
        gap = abs(mean_predicted - observed_frequency)
        if total:
            expected_calibration_error += (count / total) * gap

        bins.append(
            {
                "bin_index": index,
                "lower_bound": lower,
                "upper_bound": upper,
                "count": count,
                "mean_predicted_probability": mean_predicted,
                "observed_frequency": observed_frequency,
                "calibration_gap": gap,
            }
        )

    return {
        "expected_calibration_error": expected_calibration_error,
        "bins": bins,
    }


def evaluate_calibration_group(rows: Iterable[dict]) -> dict:
    """
    对一组**同质**评估样本计算类别校准诊断。

    要求组内 sport / model_name / model_version 完全一致（否则 `mixed_group`），
    `evaluation_id` 不得重复，空组抛 `empty_group`。
    返回值均为原始浮点数，不取整、不转字符串、不转百分比；不修改输入行。
    """
    rows = list(rows)
    key = validate_group(rows)

    classes = classes_for_sport(key[0])
    sample_count = len(rows)

    predicted_by_class: dict[str, list[float]] = {cls: [] for cls in classes}
    observed_by_class: dict[str, list[int]] = {cls: [] for cls in classes}

    for row in rows:
        row_classes, probabilities, actual = validate_row(row)
        for cls, probability in zip(row_classes, probabilities):
            predicted_by_class[cls].append(probability)
            observed_by_class[cls].append(1 if actual == cls else 0)

    class_calibration = {
        cls: build_class_calibration(predicted_by_class[cls], observed_by_class[cls])
        for cls in classes
    }

    class_errors = [class_calibration[cls]["expected_calibration_error"] for cls in classes]
    macro = sum(class_errors) / len(class_errors) if class_errors else 0.0

    return {
        "sport": key[0],
        "model_name": key[1],
        "model_version": key[2],
        "probability_source": PROBABILITY_SOURCE,
        "bin_count": BIN_COUNT,
        "sample_count": sample_count,
        "classes": list(classes),
        "class_calibration": class_calibration,
        "macro_expected_calibration_error": macro,
    }


def build_calibration_summaries(rows: Iterable[dict]) -> list[dict]:
    """
    按 (sport, model_name, model_version) 分组，各自独立计算校准诊断。

    足球三分类、篮球二分类，不产生跨运动或跨模型版本的混合诊断。
    返回顺序按 sport / model_name / model_version 字典序（确定性，不按 ECE 优劣排序）。
    输入为空时返回 `[]`。
    """
    rows = list(rows)
    if not rows:
        return []

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row.get("sport"), row.get("model_name"), row.get("model_version"))
        groups.setdefault(key, []).append(row)

    return [
        evaluate_calibration_group(groups[key])
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]


def evaluate_all_calibration() -> list[dict]:
    """
    便捷入口：读取全部评估样本并计算分组校准诊断。

    只做「读取 + 纯计算」，不写入任何存储。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_calibration_summaries(get_all_evaluation_rows())
