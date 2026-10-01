"""
Dixon-Coles 走查验证（无泄漏的时序外样本检验）
============================================
回答一个问题：

    如果 rho 只用「在目标比赛开赛**之前**已经完赛」的足球比赛拟合出来，
    那么它给未来比赛打出的分数似然，与当前固定 rho、以及独立泊松相比如何？

这是**评估层**：不部署拟合出的 rho，不修改线上预测数学。

无泄漏规则（强制）
------------------
对开赛时间为 `T` 的目标比赛，训练集严格为：

    training kickoff_at < T        （**严格小于**，不是 <=）

同一时刻开赛的多场比赛构成同一个**目标桶**，彼此不得互相训练；
该桶内所有行都用「仅由 `kickoff_at < T` 的行拟合出的同一个 rho」打分。

分组
----
按 `(sport, model_name, model_version)` 独立处理：不同版本的历史 λ 由不同的模型产出，
绝不能互相训练或混合评估。仅支持足球。

复用
----
网格、tau 规则、泊松似然、时间衰减、候选有效性、并列裁决全部复用
`utils.dixon_coles_fitting`，本模块不实现第二套拟合器或第二套 tau 约定。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Iterable

import config
from utils.dixon_coles_fitting import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_RHO_MAX,
    DEFAULT_RHO_MIN,
    DEFAULT_RHO_STEP,
    DixonColesFittingError,
    extract_fitting_row,
    fit_rho_group,
    is_valid_rho_for_observation,
    weighted_negative_log_likelihood,
)

# 走查验证的默认预热样本量：训练行数不足该值的桶不拟合、不打分
DEFAULT_MIN_TRAIN_ROWS = 20


class DixonColesWalkForwardError(DixonColesFittingError):
    """
    走查验证层面的错误。

    继承自 `DixonColesFittingError`，因此调用方既可以只捕获基类，
    也可以单独捕获走查验证特有的问题；历史行校验仍沿用拟合核心的语义。
    """


def _is_finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(
        float(value)
    )


def _row_context(row: dict) -> dict:
    return {
        "evaluation_id": row.get("evaluation_id"),
        "match_id": row.get("match_id"),
        "model_name": row.get("model_name"),
        "model_version": row.get("model_version"),
    }


def _resolve_fixed_rho(fixed_comparison_rho: float | None) -> float:
    """
    解析对照用的固定 rho：显式参数优先，否则用线上配置值。

    本函数只读取配置，不修改配置。
    """
    value = (
        config.MODEL_CONFIG["dixon_coles_rho"]
        if fixed_comparison_rho is None
        else fixed_comparison_rho
    )
    if not _is_finite_number(value):
        raise DixonColesWalkForwardError(
            "invalid_fixed_rho", detail=f"fixed_comparison_rho={value!r}"
        )
    return float(value)


def _validate_min_train_rows(min_train_rows: object) -> int:
    if not isinstance(min_train_rows, int) or isinstance(min_train_rows, bool):
        raise DixonColesWalkForwardError(
            "invalid_min_train_rows", detail=f"min_train_rows={min_train_rows!r}"
        )
    if min_train_rows < 1:
        raise DixonColesWalkForwardError(
            "invalid_min_train_rows", detail=f"min_train_rows={min_train_rows}"
        )
    return int(min_train_rows)


def _check_global_duplicates(rows: list[dict]) -> None:
    """
    在分组之前扫描**全部**输入行，防止同一 evaluation_id 通过不同 model_version 逃避检测。
    """
    seen: set = set()
    for row in rows:
        evaluation_id = row.get("evaluation_id")
        if evaluation_id is None:
            continue
        if evaluation_id in seen:
            raise DixonColesWalkForwardError(
                "duplicate_evaluation_id", **_row_context(row), detail=str(evaluation_id)
            )
        seen.add(evaluation_id)


def _check_timezone_consistency(kickoffs: list[datetime]) -> None:
    """
    同一组内的开赛时间必须可安全比较：要么全是 aware，要么全是 naive。

    aware 时间戳允许使用不同的 UTC 偏移（它们代表绝对时刻，可直接比较）；
    但不能与 naive 时间戳混用。
    """
    aware = [kickoff.tzinfo is not None for kickoff in kickoffs]
    if any(aware) and not all(aware):
        raise DixonColesWalkForwardError(
            "inconsistent_kickoff_timezone",
            detail="同一组内混用了 timezone-aware 与 timezone-naive 的开赛时间",
        )


def validate_rho_walk_forward_group(
    rows: Iterable[dict],
    *,
    min_train_rows: int = DEFAULT_MIN_TRAIN_ROWS,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    rho_min: float = DEFAULT_RHO_MIN,
    rho_max: float = DEFAULT_RHO_MAX,
    rho_step: float = DEFAULT_RHO_STEP,
    fixed_comparison_rho: float | None = None,
) -> dict:
    """
    对一组**同质**足球评估样本做时序走查验证。

    流程：
      1. 校验行（复用拟合核心的历史行校验）
      2. 按 (kickoff_at, evaluation_id) 排序，构造精确开赛时刻桶
      3. 对每个桶 T：训练集 = 所有 kickoff_at < T 的行（扩张窗口）
      4. 训练行数 < min_train_rows -> 计入 warmup_skipped_count，不拟合、不打分
      5. 否则用训练集拟合 rho，并用它 + 固定 rho + rho=0 给该桶的每一行打分

    目标观测权重恒为 1.0（时间衰减只属于训练拟合）。返回纯内存摘要，不落盘。
    """
    rows = list(rows)
    if not rows:
        raise DixonColesWalkForwardError("empty_group", detail="走查验证组为空")

    min_train_rows = _validate_min_train_rows(min_train_rows)
    fixed_rho = _resolve_fixed_rho(fixed_comparison_rho)

    first = rows[0]
    key = (first.get("sport"), first.get("model_name"), first.get("model_version"))
    if any(
        (row.get("sport"), row.get("model_name"), row.get("model_version")) != key
        for row in rows[1:]
    ):
        raise DixonColesWalkForwardError("mixed_group", detail=f"期望 {key}，收到混合分组")

    seen_ids: set = set()
    records: list[dict] = []
    for row in rows:
        evaluation_id = row.get("evaluation_id")
        if evaluation_id is not None:
            if evaluation_id in seen_ids:
                raise DixonColesWalkForwardError(
                    "duplicate_evaluation_id", **_row_context(row), detail=str(evaluation_id)
                )
            seen_ids.add(evaluation_id)

        lambda_home, lambda_away, home_goals, away_goals, kickoff = extract_fitting_row(row)
        records.append(
            {
                "row": row,
                "evaluation_id": evaluation_id,
                "kickoff": kickoff,
                "observation": (lambda_home, lambda_away, home_goals, away_goals, 1.0),
            }
        )

    _check_timezone_consistency([record["kickoff"] for record in records])

    # 排序保证与输入顺序无关；开赛时刻相同的行按 evaluation_id 稳定排序
    records.sort(key=lambda record: (record["kickoff"], str(record["evaluation_id"])))

    # 精确开赛时刻桶
    buckets: list[tuple[datetime, int, list[dict]]] = []
    for index, record in enumerate(records):
        if buckets and buckets[-1][0] == record["kickoff"]:
            buckets[-1][2].append(record)
        else:
            buckets.append((record["kickoff"], index, [record]))

    warmup_skipped_count = 0
    evaluation_count = 0
    fitted_total = 0.0
    fixed_total = 0.0
    zero_total = 0.0
    rho_path: list[dict] = []

    for kickoff, start_index, bucket_records in buckets:
        train_rows = [record["row"] for record in records[:start_index]]
        if len(train_rows) < min_train_rows:
            warmup_skipped_count += len(bucket_records)
            continue

        fit = fit_rho_group(
            train_rows,
            half_life_days=half_life_days,
            rho_min=rho_min,
            rho_max=rho_max,
            rho_step=rho_step,
        )
        fitted_rho = fit["fitted_rho"]

        bucket_fitted = 0.0
        bucket_fixed = 0.0
        bucket_zero = 0.0
        for record in bucket_records:
            observation = record["observation"]
            if not is_valid_rho_for_observation(observation, fixed_rho):
                raise DixonColesWalkForwardError(
                    "invalid_fixed_rho",
                    evaluation_id=record["evaluation_id"],
                    detail=f"fixed_comparison_rho={fixed_rho} 在目标 λ 上使低比分修正非正",
                )
            bucket_fitted += weighted_negative_log_likelihood([observation], fitted_rho)
            bucket_fixed += weighted_negative_log_likelihood([observation], fixed_rho)
            bucket_zero += weighted_negative_log_likelihood([observation], 0.0)

        evaluation_count += len(bucket_records)
        fitted_total += bucket_fitted
        fixed_total += bucket_fixed
        zero_total += bucket_zero

        rho_path.append(
            {
                "target_kickoff_at": kickoff.isoformat(),
                "train_count": len(train_rows),
                "test_count": len(bucket_records),
                "training_reference_kickoff_at": fit["reference_kickoff_at"],
                "fitted_rho": fitted_rho,
                "fitted_test_nll": bucket_fitted,
                "fixed_rho_test_nll": bucket_fixed,
                "rho_zero_test_nll": bucket_zero,
            }
        )

    target_bucket_count = len(rho_path)
    if evaluation_count:
        fitted_mean = fitted_total / evaluation_count
        fixed_mean = fixed_total / evaluation_count
        zero_mean = zero_total / evaluation_count
        improvement_vs_fixed = fixed_total - fitted_total
        improvement_vs_zero = zero_total - fitted_total
    else:
        # 尚无可用目标：返回 None，而不是 NaN 或伪造的零值
        fitted_mean = fixed_mean = zero_mean = None
        improvement_vs_fixed = improvement_vs_zero = None

    return {
        "sport": key[0],
        "model_name": key[1],
        "model_version": key[2],
        "sample_count": len(records),
        "min_train_rows": min_train_rows,
        "warmup_skipped_count": warmup_skipped_count,
        "evaluation_count": evaluation_count,
        "target_bucket_count": target_bucket_count,
        "half_life_days": float(half_life_days),
        "rho_grid": {
            "minimum": float(rho_min),
            "maximum": float(rho_max),
            "step": float(rho_step),
        },
        "fixed_comparison_rho": fixed_rho,
        "walk_forward_fitted_total_nll": fitted_total if evaluation_count else None,
        "fixed_rho_total_nll": fixed_total if evaluation_count else None,
        "rho_zero_total_nll": zero_total if evaluation_count else None,
        "walk_forward_fitted_mean_nll": fitted_mean,
        "fixed_rho_mean_nll": fixed_mean,
        "rho_zero_mean_nll": zero_mean,
        "nll_improvement_vs_fixed": improvement_vs_fixed,
        "nll_improvement_vs_zero": improvement_vs_zero,
        "rho_path": rho_path,
    }


def build_rho_walk_forward_summaries(rows: Iterable[dict], **kwargs) -> list[dict]:
    """
    按 (sport, model_name, model_version) 分组做走查验证，**只输出足球分组**。

    在分组之前先对**全部**输入行做 evaluation_id 全局重复检测，
    因此同一 ID 即使被篡改了 model_version 也无法逃避检测。
    返回顺序按 sport / model_name / model_version 字典序，不按验证结果排序。
    输入为空时返回 `[]`。
    """
    rows = list(rows)
    if not rows:
        return []

    _check_global_duplicates(rows)

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("sport") != "football":
            continue
        key = (row.get("sport"), row.get("model_name"), row.get("model_version"))
        groups.setdefault(key, []).append(row)

    return [
        validate_rho_walk_forward_group(groups[key], **kwargs)
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]


def validate_all_rho_walk_forward(**kwargs) -> list[dict]:
    """
    便捷入口：读取全部评估样本并做分组走查验证（只输出足球分组）。

    只做「读取 + 纯计算」，不写入任何存储，也不改动线上模型配置。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_rho_walk_forward_summaries(get_all_evaluation_rows(), **kwargs)
