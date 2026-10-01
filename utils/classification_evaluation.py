"""
分类评估核心
============
在**不可变评估样本**（`data/evaluation_rows.json`）之上，计算纯分类指标：

    sample_count
    accuracy
    brier_score
    multiclass_log_loss

架构边界
--------
只消费评估样本行。**不读** daily_matches / prediction_snapshots / settlements /
team_strength，不读当前 Elo 或当前盘口，不调用 `predict_match` / `enrich_match`。
本模块是纯计算 + 读取层，不持久化任何指标，不改写评估样本。

概率来源（强制）
----------------
一律使用 `evaluation_row["model_probabilities"]`。
不使用 display_probabilities / market_implied_probabilities / market_odds /
expected_values，也不做任何概率融合。

历史概率量纲（强制）
--------------------
历史 schema 中概率是**整数百分点**（0..100），例如 `53` 表示 `0.53`。
本模块**不做** [0,1] 与 [0,100] 的自动判别：`0.60/0.20/0.20` 在历史 schema 下
属于畸形数据，会被显式拒绝。

指标约定（后续比较必须沿用同一约定）
------------------------------------
- Brier（多分类）：`mean( sum_k (p_k - y_k)^2 )`，**不**按类别数再除一次。
- LogLoss：自然对数，`mean( -ln(p_actual) )`，仅取对数时用 `epsilon = 1e-15` 裁剪。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

# 概率来源：唯一权威字段
PROBABILITY_SOURCE = "model_probabilities"

# 固定类别顺序（同时用于 argmax 的确定性并列裁决）
FOOTBALL_CLASSES: tuple[str, ...] = ("home_win", "draw", "away_win")
BASKETBALL_CLASSES: tuple[str, ...] = ("home_win", "away_win")

_CLASSES_BY_SPORT = {
    "football": FOOTBALL_CLASSES,
    "basketball": BASKETBALL_CLASSES,
}

# 历史概率为整数百分点，独立取整后总和可能为 99 / 100 / 101
MIN_PROBABILITY_TOTAL = 99.0
MAX_PROBABILITY_TOTAL = 101.0

# 仅用于对数计算的数值保护，不修改已归一化的分布
LOG_EPSILON = 1e-15


class ClassificationEvaluationError(Exception):
    """评估样本无法用于分类评估时抛出（显式失败，不静默跳过）。"""

    def __init__(
        self,
        reason: str,
        *,
        evaluation_id: str | None = None,
        match_id: str | None = None,
        sport: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.evaluation_id = evaluation_id
        self.match_id = match_id
        self.sport = sport
        self.model_name = model_name
        self.model_version = model_version
        self.detail = detail
        message = (
            f"分类评估错误({reason}) sport={sport} model={model_name} "
            f"version={model_version} match={match_id} evaluation={evaluation_id}"
        )
        if detail:
            message = f"{message} :: {detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 类别空间
# ---------------------------------------------------------------------------

def classes_for_sport(sport: str) -> tuple[str, ...]:
    """返回该运动的固定类别顺序；不支持的运动显式报错。"""
    classes = _CLASSES_BY_SPORT.get(sport)
    if classes is None:
        raise ClassificationEvaluationError("unsupported_sport", sport=sport, detail=f"sport={sport!r}")
    return classes


def _is_number(value: object) -> bool:
    """bool 在本语义下不算数值。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _probability_value(row: dict, key: str, classes: Sequence[str]) -> float:
    """取出并校验单个类别概率（百分点）。"""
    probabilities = row.get(PROBABILITY_SOURCE)
    if not isinstance(probabilities, dict):
        raise ClassificationEvaluationError(
            "missing_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"{PROBABILITY_SOURCE} 不是字典",
        )

    if key not in probabilities:
        raise ClassificationEvaluationError(
            "missing_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"缺少类别 {key}",
        )

    value = probabilities[key]
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ClassificationEvaluationError(
            "invalid_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"{key}={value!r}",
        )

    value = float(value)
    if value < 0.0 or value > 100.0:
        raise ClassificationEvaluationError(
            "invalid_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"{key}={value} 超出 [0, 100]",
        )
    return value


def _check_basketball_draw(row: dict) -> None:
    """
    篮球是二分类。模型可能写入兼容字段 `draw = 0`，忽略之；
    但若存在**非零**的 draw 概率，说明有实质概率质量会被丢弃 -> 视为畸形。
    """
    probabilities = row.get(PROBABILITY_SOURCE)
    if not isinstance(probabilities, dict) or "draw" not in probabilities:
        return

    value = probabilities["draw"]
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ClassificationEvaluationError(
            "invalid_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"draw={value!r}",
        )

    value = float(value)
    if value < 0.0 or value > 100.0:
        raise ClassificationEvaluationError(
            "invalid_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"draw={value} 超出 [0, 100]",
        )
    if value != 0.0:
        raise ClassificationEvaluationError(
            "nonzero_basketball_draw_probability",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"draw={value}",
        )


def normalize_class_probabilities(row: dict) -> tuple[tuple[str, ...], list[float]]:
    """
    把历史百分点概率规范化为「精确和为 1.0」的类别概率向量。

    过程：校验每个必需类别 -> 校验总和落在 [99, 101] -> 除以总和完成重归一化。
    只返回必需类别，不修改输入行。
    """
    classes = classes_for_sport(row.get("sport"))
    if row.get("sport") == "basketball":
        _check_basketball_draw(row)

    raw = [_probability_value(row, key, classes) for key in classes]

    total = sum(raw)
    if total < MIN_PROBABILITY_TOTAL or total > MAX_PROBABILITY_TOTAL:
        raise ClassificationEvaluationError(
            "invalid_probability_total",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"必需类别总和={total}，允许区间 [{MIN_PROBABILITY_TOTAL}, {MAX_PROBABILITY_TOTAL}]",
        )

    normalized = [value / total for value in raw]
    return classes, normalized


def _actual_class(row: dict, classes: Sequence[str]) -> str:
    outcome = row.get("actual_outcome")
    if outcome not in classes:
        raise ClassificationEvaluationError(
            "invalid_actual_outcome",
            evaluation_id=row.get("evaluation_id"),
            match_id=row.get("match_id"),
            sport=row.get("sport"),
            model_name=row.get("model_name"),
            model_version=row.get("model_version"),
            detail=f"actual_outcome={outcome!r}，允许类别 {list(classes)}",
        )
    return outcome


def _argmax_class(classes: Sequence[str], probabilities: Sequence[float]) -> str:
    """固定类别顺序的确定性 argmax：并列时取顺序在前的类别。"""
    best_index = 0
    for index in range(1, len(classes)):
        if probabilities[index] > probabilities[best_index]:
            best_index = index
    return classes[best_index]


# ---------------------------------------------------------------------------
# 单组评估
# ---------------------------------------------------------------------------

def _group_key(row: dict) -> tuple:
    return (row.get("sport"), row.get("model_name"), row.get("model_version"))


def validate_group(rows: Sequence[dict]) -> tuple[str, str, str]:
    """
    校验一组评估样本的**同质性**与 `evaluation_id` 唯一性。

    返回 (sport, model_name, model_version)。
    空组 -> `empty_group`；三者任一不一致 -> `mixed_group`；
    重复 `evaluation_id` -> `duplicate_evaluation_id`（不静默去重）。

    这是评估侧唯一的「分组合法性」定义，分类评估与校准诊断共用。
    """
    if not rows:
        raise ClassificationEvaluationError("empty_group", detail="评估组为空")

    key = _group_key(rows[0])
    if any(_group_key(row) != key for row in rows[1:]):
        raise ClassificationEvaluationError(
            "mixed_group",
            detail=f"期望 {key}，收到混合分组",
        )

    seen_ids: set = set()
    for row in rows:
        evaluation_id = row.get("evaluation_id")
        if evaluation_id is None:
            continue
        if evaluation_id in seen_ids:
            raise ClassificationEvaluationError(
                "duplicate_evaluation_id",
                evaluation_id=evaluation_id,
                match_id=row.get("match_id"),
                sport=row.get("sport"),
                model_name=row.get("model_name"),
                model_version=row.get("model_version"),
            )
        seen_ids.add(evaluation_id)
    return key


def validate_row(row: dict) -> tuple[tuple[str, ...], list[float], str]:
    """
    校验单行评估样本，返回 (类别顺序, 归一化概率, 实际类别)。

    概率值 / 概率总和 / 篮球 draw / 实际结果的校验语义在此统一定义，
    评估侧的任何派生分析都应经由本函数，避免出现第二套解释。
    """
    classes, probabilities = normalize_class_probabilities(row)
    return classes, probabilities, _actual_class(row, classes)


def evaluate_classification_group(rows: Iterable[dict]) -> dict:
    """
    对一组**同质**评估样本计算分类指标。

    要求该组内的 sport / model_name / model_version 完全一致，否则抛 `mixed_group`。
    传入空集合时抛出 `empty_group`（显式失败，不返回 NaN 或伪造的零值摘要）。

    返回值中的指标均为原始浮点数，不做取整、不转字符串、不转百分比。
    本函数不修改任何输入行。
    """
    rows = list(rows)
    key = validate_group(rows)

    correct_count = 0
    brier_total = 0.0
    log_loss_total = 0.0

    for row in rows:
        classes, probabilities, actual = validate_row(row)

        predicted = _argmax_class(classes, probabilities)
        if predicted == actual:
            correct_count += 1

        brier_total += sum(
            (probability - (1.0 if cls == actual else 0.0)) ** 2
            for cls, probability in zip(classes, probabilities)
        )

        actual_probability = probabilities[classes.index(actual)]
        clipped = max(LOG_EPSILON, min(1.0, actual_probability))
        log_loss_total += -math.log(clipped)

    sample_count = len(rows)
    return {
        "sport": key[0],
        "model_name": key[1],
        "model_version": key[2],
        "probability_source": PROBABILITY_SOURCE,
        "classes": list(classes_for_sport(key[0])),
        "sample_count": sample_count,
        "accuracy": correct_count / sample_count,
        "brier_score": brier_total / sample_count,
        "multiclass_log_loss": log_loss_total / sample_count,
    }


# ---------------------------------------------------------------------------
# 分组摘要
# ---------------------------------------------------------------------------

def build_classification_summaries(rows: Iterable[dict]) -> list[dict]:
    """
    按 (sport, model_name, model_version) 分组，各自独立计算指标。

    足球是三分类、篮球是二分类，因此**不产生**跨运动的混合指标。
    返回顺序按 sport / model_name / model_version 字典序（确定性，不按指标优劣排序）。
    输入为空时返回 `[]`（不伪造零值摘要）。
    """
    rows = list(rows)
    if not rows:
        return []

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)

    summaries = [
        evaluate_classification_group(groups[key])
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]
    return summaries


def evaluate_all_classification() -> list[dict]:
    """
    便捷入口：读取全部评估样本并计算分组摘要。

    只做「读取 + 纯计算」，不写入任何存储。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_classification_summaries(get_all_evaluation_rows())
