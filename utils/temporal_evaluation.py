"""
时序模型评估核心
================
在**不可变评估样本**（`data/evaluation_rows.json`）之上，回答：

    对某一条精确的 (sport, model_name, model_version) 谱系，
    它在最近若干个固定回看时间窗内的历史概率质量指标，
    与它的**全部可用历史**相比，是什么样子的？

指标（每个窗口与 all-time 各自一组）：

    sample_count
    accuracy
    brier_score              （多分类）
    multiclass_log_loss      （多分类，自然对数）
    macro_expected_calibration_error
    class_expected_calibration_error

架构边界
--------
本模块是**纯诊断的组合层**，只消费不可变评估样本行：

- **不重建历史**：不调用 `predict_match` / `enrich_match` / `expected_goals` /
  `score_matrix`，一律使用评估样本中已固化的 `model_probabilities`。
- **不重算公式**：概率归一化、类别定义、实际结果校验、argmax、Brier、LogLoss、
  LogLoss epsilon、10 分箱、one-vs-rest ECE、macro ECE、分组身份
  全部复用 `utils.classification_evaluation` 与 `utils.calibration_evaluation`。
- **不落盘**：所有摘要在内存中派生，不生成 `temporal_evaluation.json` 之类文件。
- **不排名 / 不推荐 / 不选模型**：只返回测量值。
- **不做跨版本增量比较**：不同模型版本往往覆盖不同历史区间，本层只并列事实摘要。

时间坐标（强制）
----------------
唯一时间坐标是评估样本的 `kickoff_at`（历史比赛开球时刻）。
**不**使用 prediction_generated_at / settled_at / materialized_at / 系统时钟 /
文件修改时间。因此同一批不可变样本，无论何时何地求值，结果完全一致。

参考时间：`reference_kickoff_at` = 该组内**最大的 kickoff_at**。
不使用 `datetime.now()` / `get_beijing_now()` / `time.time()`。

默认回看窗口：`30 / 90 / 180` 天，外加「全部历史」。
窗口有意重叠、相互嵌套，是**诊断**而非统计上独立的队列。

顺序与「数值完全一致」契约（重要）
----------------------------------
组内行按**确定性时序**排列：先按 `kickoff_at` 绝对瞬时，再按 `evaluation_id`。
所有指标都在这个**规范时序**上求值，因此：

    temporal["all_time"]  ==  evaluate_classification_group(该组规范时序)
    temporal["windows"]   ==  evaluate_classification_group(该窗口子集规范时序)

（校准指标同理，对应 `evaluate_calibration_group`。）
由于浮点求和与加数顺序有关，与既有评估器比较时必须使用**同一时序**；
本模块已把该时序固定为确定性时序，`all_time` 与每个窗口子集都不例外。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from utils.classification_evaluation import (
    PROBABILITY_SOURCE,
    evaluate_classification_group,
    validate_group,
)
from utils.calibration_evaluation import evaluate_calibration_group

# 默认回看窗口（天）。有意重叠 / 嵌套，仅作诊断。
DEFAULT_WINDOW_DAYS: tuple[int, ...] = (30, 90, 180)


class TemporalEvaluationError(Exception):
    """时序评估输入非法时抛出（显式失败，不静默跳过任何行）。"""

    def __init__(self, reason: str, *, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        message = f"时序评估错误({reason})"
        if detail:
            message = f"{message} :: {detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 时间坐标解析与规范化
# ---------------------------------------------------------------------------

def parse_kickoff(value: object) -> datetime:
    """
    解析评估样本的 `kickoff_at` 时间戳。

    只接受非空的 ISO 8601 字符串；`None` / 空串 / 非字符串 / 不可解析的字符串
    一律抛出 `invalid_kickoff_at`（不静默跳过该行）。

    兼容结尾 `Z`（按 UTC 处理）。时区信息原样保留，不做移除。
    """
    if not isinstance(value, str) or not value.strip():
        raise TemporalEvaluationError(
            "invalid_kickoff_at", detail=f"kickoff_at={value!r}"
        )

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"

    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:  # noqa: BLE001
        raise TemporalEvaluationError(
            "invalid_kickoff_at", detail=f"kickoff_at={value!r} ({exc})"
        ) from exc


def _instant(value: datetime) -> datetime:
    """
    把时间戳映射为可比较的绝对瞬时。

    时区感知时间戳统一换算到 UTC，因此不同 UTC 偏移按绝对瞬时比较；
    时区朴素时间戳原样返回（组内必须同质，见 `_prepare_entries`）。
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value


def _canonical_iso(value: datetime) -> str:
    """
    时间戳的确定性输出表示。

    时区感知时间戳规范化为 UTC ISO 字符串；朴素时间戳原样输出（无偏移后缀）。
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    return value.isoformat()


def _prepare_entries(rows: Sequence[dict]) -> list[tuple[datetime, datetime, dict]]:
    """
    解析并按时序排列一组评估样本。

    返回 `[(instant, parsed_dt, row), ...]`，升序：
    先按 `kickoff_at` 绝对瞬时，再按 `evaluation_id`（并列时为确定性次序）。

    同一组内不得混用「时区感知」与「时区朴素」时间戳，否则抛
    `inconsistent_kickoff_timezone`（不静默剥离时区）。
    """
    parsed: list[tuple[datetime, dict]] = []
    aware_flags: set[bool] = set()

    for row in rows:
        dt = parse_kickoff(row.get("kickoff_at"))
        aware_flags.add(dt.tzinfo is not None)
        parsed.append((dt, row))

    if len(aware_flags) > 1:
        raise TemporalEvaluationError(
            "inconsistent_kickoff_timezone",
            detail="同一组内混用了时区感知与时区朴素时间戳",
        )

    entries = [(_instant(dt), dt, row) for dt, row in parsed]
    entries.sort(key=lambda entry: (entry[0], str(entry[2].get("evaluation_id") or "")))
    return entries


# ---------------------------------------------------------------------------
# 窗口校验与选择
# ---------------------------------------------------------------------------

def validate_window_days(window_days: Iterable[int]) -> tuple[int, ...]:
    """
    校验回看窗口参数并返回**升序**元组。

    每个条目必须是**精确 int**（排除 bool）、`> 0`。
    拒绝：`0` / 负数 / 浮点 / 布尔 / 字符串 / 重复值 / 空集合，
    统一抛 `invalid_window_days`（不做静默归一化）。

    输出始终按数值升序，与调用方传入顺序无关：
    `(180, 30, 90)` -> `(30, 90, 180)`。
    """
    if window_days is None or isinstance(window_days, (str, bytes, bool)):
        raise TemporalEvaluationError("invalid_window_days", detail=f"{window_days!r}")

    try:
        values = list(window_days)
    except TypeError as exc:
        raise TemporalEvaluationError(
            "invalid_window_days", detail=f"不可迭代: {window_days!r}"
        ) from exc

    if not values:
        raise TemporalEvaluationError("invalid_window_days", detail="窗口集合为空")

    seen: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TemporalEvaluationError("invalid_window_days", detail=f"{value!r}")
        if value <= 0:
            raise TemporalEvaluationError("invalid_window_days", detail=f"{value!r}")
        if value in seen:
            raise TemporalEvaluationError("invalid_window_days", detail=f"重复值 {value!r}")
        seen.add(value)

    return tuple(sorted(values))


def select_trailing_window(
    entries: Sequence[tuple[datetime, datetime, dict]],
    reference_instant: datetime,
    window_days: int,
) -> list[tuple[datetime, datetime, dict]]:
    """
    从已解析的时间序列条目中选出 N 天回看窗口内的条目。

    成员判定基于**绝对瞬时**：

        reference_instant - N days <= entry_instant <= reference_instant

    下边界**包含**：恰好 N 天前的比赛属于该窗口；再早一秒则不属于。
    条目必须已由 `_prepare_entries` 解析（组内时区同质）。
    """
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days <= 0:
        raise TemporalEvaluationError("invalid_window_days", detail=f"{window_days!r}")

    lower_bound = reference_instant - timedelta(days=window_days)
    return [
        entry for entry in entries
        if lower_bound <= entry[0] <= reference_instant
    ]


# ---------------------------------------------------------------------------
# 紧凑指标构造
# ---------------------------------------------------------------------------

def compact_metrics(
    entries: Sequence[tuple[datetime, datetime, dict]],
) -> dict:
    """
    由一组已解析条目构造紧凑时序指标记录。

    分类指标来自 `evaluate_classification_group`，
    校准指标来自 `evaluate_calibration_group`，
    本函数只做**取用与打包**，不重新实现任何公式。

    `class_expected_calibration_error` 是确定性映射，键取自既有分类内核
    的类别顺序（足球 home_win / draw / away_win；篮球 home_win / away_win），
    值逐字取自既有校准内核的 per-class ECE。
    """
    rows = [entry[2] for entry in entries]

    classification = evaluate_classification_group(rows)
    calibration = evaluate_calibration_group(rows)

    classes = list(classification["classes"])
    class_calibration = calibration["class_calibration"]

    return {
        "sample_count": classification["sample_count"],
        "first_sample_kickoff_at": _canonical_iso(entries[0][1]),
        "last_sample_kickoff_at": _canonical_iso(entries[-1][1]),
        "accuracy": classification["accuracy"],
        "brier_score": classification["brier_score"],
        "multiclass_log_loss": classification["multiclass_log_loss"],
        "macro_expected_calibration_error": calibration[
            "macro_expected_calibration_error"
        ],
        "class_expected_calibration_error": {
            cls: class_calibration[cls]["expected_calibration_error"]
            for cls in classes
        },
    }


# ---------------------------------------------------------------------------
# 分组时序评估
# ---------------------------------------------------------------------------

def _group_key(row: dict) -> tuple:
    """与评估内核一致的分组身份：`(sport, model_name, model_version)`。"""
    return (row.get("sport"), row.get("model_name"), row.get("model_version"))


def evaluate_temporal_group(
    rows: Iterable[dict],
    *,
    window_days: Iterable[int] = DEFAULT_WINDOW_DAYS,
) -> dict:
    """
    对一组**同质**评估样本计算时序诊断摘要。

    组分组合法性（空组 / 混合分组 / 重复 evaluation_id）由既有
    `classification_evaluation.validate_group` 裁决，语义完全复用。

    返回结构（字段顺序固定，便于人工审阅）：

        sport / model_name / model_version / probability_source
        reference_kickoff_at
        all_time  -> 紧凑指标（全部样本）
        windows   -> [ {window_days, window_start_at, window_end_at, <紧凑指标>}, ... ]

    `windows` 按窗口天数升序，不按样本量或指标排序。
    本函数不修改任何输入行。
    """
    rows = list(rows)
    sport, model_name, model_version = validate_group(rows)

    entries = _prepare_entries(rows)
    reference_instant = entries[-1][0]

    windows = validate_window_days(window_days)

    window_records: list[dict] = []
    for days in windows:
        subset = select_trailing_window(entries, reference_instant, days)
        record = {
            "window_days": days,
            "window_start_at": _canonical_iso(reference_instant - timedelta(days=days)),
            "window_end_at": _canonical_iso(reference_instant),
        }
        record.update(compact_metrics(subset))
        window_records.append(record)

    return {
        "sport": sport,
        "model_name": model_name,
        "model_version": model_version,
        "probability_source": PROBABILITY_SOURCE,
        "reference_kickoff_at": _canonical_iso(reference_instant),
        "all_time": compact_metrics(entries),
        "windows": window_records,
    }


def build_temporal_summaries(
    rows: Iterable[dict],
    *,
    window_days: Iterable[int] = DEFAULT_WINDOW_DAYS,
) -> list[dict]:
    """
    按 `(sport, model_name, model_version)` 分组，各自独立计算时序诊断摘要。

    绝不跨运动、绝不跨模型版本混合。
    返回顺序按 sport / model_name / model_version 字典序（确定性，不按指标排序）。
    输入为空时返回 `[]`（不伪造空摘要）。
    """
    rows = list(rows)
    if not rows:
        return []

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)

    return [
        evaluate_temporal_group(groups[key], window_days=window_days)
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]


def evaluate_all_temporal(
    *,
    window_days: Iterable[int] = DEFAULT_WINDOW_DAYS,
) -> list[dict]:
    """
    便捷入口：读取全部评估样本并计算分组时序摘要。

    只经 `utils.evaluation_rows.get_all_evaluation_rows()` 读取，不直接读 JSON 文件，
    不写入任何存储。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_temporal_summaries(
        get_all_evaluation_rows(), window_days=window_days
    )
