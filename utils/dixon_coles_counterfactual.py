"""
Dixon-Coles 反事实 W/D/L 验证
=============================
在既有的**无泄漏走查时序**之上，把验证从「比分似然」扩展到「比赛结果概率质量」：

对每场合格的历史目标比赛，用**开赛前冻结的历史期望进球**重建胜平负概率，
再与真实历史结果比较 Brier 与多分类 LogLoss：

    1. 仅用严格更早比赛拟合出的 rho（走查拟合）
    2. 当前线上固定 rho（默认 -0.15）
    3. rho = 0（独立泊松）

关键隔离原则
------------
三种策略之间**只有 rho 不同**：λ、比分矩阵实现、比分上限、归一化、W/D/L 聚合、
实际结果全部一致。因此比较隔离出的正是 Dixon-Coles 的 rho。

本任务仍是**评估层**：不部署 rho、不修改线上概率、不落盘、不接入 refresh。
"""
from __future__ import annotations

import math
from typing import Iterable

from models.poisson_model import score_matrix, win_draw_loss
from utils.classification_evaluation import LOG_EPSILON
from utils.dixon_coles_fitting import (
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_RHO_MAX,
    DEFAULT_RHO_MIN,
    DEFAULT_RHO_STEP,
    is_valid_rho_for_lambdas,
)
from utils.dixon_coles_walkforward import (
    DEFAULT_MIN_TRAIN_ROWS,
    DixonColesWalkForwardError,
    build_walk_forward_fits,
    build_walk_forward_plan,
    check_global_duplicate_evaluation_ids,
    resolve_fixed_comparison_rho,
)

# 足球类别顺序（与分类评估一致）
CLASS_ORDER: tuple[str, ...] = ("home_win", "draw", "away_win")

# 三种策略的固定顺序
POLICY_WALK_FORWARD = "walk_forward_fitted"
POLICY_FIXED = "fixed_rho"
POLICY_ZERO = "rho_zero"
POLICY_ORDER: tuple[str, ...] = (POLICY_WALK_FORWARD, POLICY_FIXED, POLICY_ZERO)


class DixonColesCounterfactualError(DixonColesWalkForwardError):
    """反事实 W/D/L 验证层面的错误（继承走查验证错误，进而继承拟合错误）。"""


def _is_positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def counterfactual_wdl_probabilities(
    lambda_home: float,
    lambda_away: float,
    rho: float,
) -> dict:
    """
    用**现有**比分矩阵路径重建 W/D/L 概率（不做任何四舍五入）：

        score_matrix(λ_home, λ_away, rho=rho)   # 使用既有默认 max_goals
            -> win_draw_loss(...)
            -> {"home_win": p, "draw": p, "away_win": p}

    不指定不同的 max_goals、不重写泊松 PMF / 归一化 / Dixon-Coles tau。
    rho 必须先通过既有有效性判据，不做裁剪、不静默替换为 0。
    """
    if not _is_positive_number(lambda_home) or not _is_positive_number(lambda_away):
        raise DixonColesCounterfactualError(
            "invalid_expected_goals",
            detail=f"lambda_home={lambda_home!r} lambda_away={lambda_away!r}",
        )
    if not is_valid_rho_for_lambdas(lambda_home, lambda_away, rho):
        raise DixonColesCounterfactualError(
            "invalid_rho",
            detail=f"rho={rho} 在 λ=({lambda_home}, {lambda_away}) 上使低比分修正非正",
        )

    matrix = score_matrix(lambda_home, lambda_away, rho=rho)
    home_win, draw, away_win = win_draw_loss(matrix)
    return {"home_win": home_win, "draw": draw, "away_win": away_win}


def multiclass_brier(probabilities: dict, actual_outcome: str) -> float:
    """
    多分类 Brier：`Σ_k (p_k - y_k)^2`，**不**再按类别数除一次。

    与分类评估使用同一约定。
    """
    return sum(
        (probabilities[cls] - (1.0 if cls == actual_outcome else 0.0)) ** 2
        for cls in CLASS_ORDER
    )


def multiclass_log_loss(probabilities: dict, actual_outcome: str) -> float:
    """
    多分类 LogLoss：`-ln(p_actual)`，自然对数。

    仅在对数求值时用 `1e-15` 裁剪（与分类评估同一常数），不修改概率分布本身。
    """
    actual_probability = probabilities[actual_outcome]
    clipped = max(LOG_EPSILON, min(1.0, actual_probability))
    return -math.log(clipped)


def _derive_outcome(home_goals: int, away_goals: int) -> str:
    if home_goals > away_goals:
        return "home_win"
    if home_goals == away_goals:
        return "draw"
    return "away_win"


def _resolve_actual_outcome(record: dict) -> str:
    """
    由比分推导实际类别；若行内 `actual_outcome` 存在但与比分冲突，显式报错。

    不静默偏向任何一个字段。
    """
    derived = _derive_outcome(record["home_goals"], record["away_goals"])
    stated = record["row"].get("actual_outcome")
    if stated is not None and stated != derived:
        raise DixonColesCounterfactualError(
            "inconsistent_actual_outcome",
            evaluation_id=record["evaluation_id"],
            match_id=record["row"].get("match_id"),
            detail=(
                f"actual_outcome={stated!r} 与比分 "
                f"{record['home_goals']}-{record['away_goals']}（{derived}）不符"
            ),
        )
    return derived


def _policy_probabilities(record: dict, policy: str, fitted_rho: float, fixed_rho: float) -> dict:
    rho = {
        POLICY_WALK_FORWARD: fitted_rho,
        POLICY_FIXED: fixed_rho,
        POLICY_ZERO: 0.0,
    }[policy]
    if not is_valid_rho_for_lambdas(record["lambda_home"], record["lambda_away"], rho):
        raise DixonColesCounterfactualError(
            "invalid_rho",
            evaluation_id=record["evaluation_id"],
            detail=f"策略 {policy} 的 rho={rho} 在目标 λ 上使低比分修正非正",
        )
    return counterfactual_wdl_probabilities(record["lambda_home"], record["lambda_away"], rho)


def validate_counterfactual_group(
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
    对一组**同质**足球评估样本做反事实 W/D/L 验证。

    时序完全复用 `build_walk_forward_plan` / `build_walk_forward_fits`，
    因此合格目标集合、rho 路径、预热计数与分数似然走查验证**完全一致**。

    返回纯内存摘要，不落盘；无合格目标时所有 total / mean / improvement 均为 `None`。
    """
    plan = build_walk_forward_plan(
        rows,
        min_train_rows=min_train_rows,
        half_life_days=half_life_days,
        rho_min=rho_min,
        rho_max=rho_max,
        rho_step=rho_step,
    )
    fixed_rho = resolve_fixed_comparison_rho(fixed_comparison_rho)

    # 所有行（含预热跳过的）都必须通过与 final_score 一致的实际结果校验
    for record in plan["records"]:
        _resolve_actual_outcome(record)

    fits = build_walk_forward_fits(plan)

    evaluation_count = 0
    brier_total = {policy: 0.0 for policy in POLICY_ORDER}
    log_loss_total = {policy: 0.0 for policy in POLICY_ORDER}
    rho_path: list[dict] = []

    for fit in fits:
        bucket_brier = {policy: 0.0 for policy in POLICY_ORDER}
        bucket_log_loss = {policy: 0.0 for policy in POLICY_ORDER}

        for record in fit["records"]:
            actual_outcome = _resolve_actual_outcome(record)
            for policy in POLICY_ORDER:
                probabilities = _policy_probabilities(
                    record, policy, fit["fitted_rho"], fixed_rho
                )
                bucket_brier[policy] += multiclass_brier(probabilities, actual_outcome)
                bucket_log_loss[policy] += multiclass_log_loss(probabilities, actual_outcome)

        evaluation_count += len(fit["records"])
        for policy in POLICY_ORDER:
            brier_total[policy] += bucket_brier[policy]
            log_loss_total[policy] += bucket_log_loss[policy]

        rho_path.append(
            {
                "target_kickoff_at": fit["kickoff"].isoformat(),
                "train_count": fit["train_count"],
                "test_count": len(fit["records"]),
                "training_reference_kickoff_at": fit["training_reference_kickoff_at"],
                "fitted_rho": fit["fitted_rho"],
                "walk_forward_fitted_brier": bucket_brier[POLICY_WALK_FORWARD],
                "fixed_rho_brier": bucket_brier[POLICY_FIXED],
                "rho_zero_brier": bucket_brier[POLICY_ZERO],
                "walk_forward_fitted_log_loss": bucket_log_loss[POLICY_WALK_FORWARD],
                "fixed_rho_log_loss": bucket_log_loss[POLICY_FIXED],
                "rho_zero_log_loss": bucket_log_loss[POLICY_ZERO],
            }
        )

    target_bucket_count = len(rho_path)
    if evaluation_count:
        brier_mean = {p: brier_total[p] / evaluation_count for p in POLICY_ORDER}
        log_loss_mean = {p: log_loss_total[p] / evaluation_count for p in POLICY_ORDER}
        brier_improvement_vs_fixed = brier_total[POLICY_FIXED] - brier_total[POLICY_WALK_FORWARD]
        brier_improvement_vs_zero = brier_total[POLICY_ZERO] - brier_total[POLICY_WALK_FORWARD]
        log_loss_improvement_vs_fixed = (
            log_loss_total[POLICY_FIXED] - log_loss_total[POLICY_WALK_FORWARD]
        )
        log_loss_improvement_vs_zero = (
            log_loss_total[POLICY_ZERO] - log_loss_total[POLICY_WALK_FORWARD]
        )
    else:
        # 尚无可用目标：返回 None，而不是 NaN 或伪造的零值
        brier_mean = {p: None for p in POLICY_ORDER}
        log_loss_mean = {p: None for p in POLICY_ORDER}
        brier_improvement_vs_fixed = None
        brier_improvement_vs_zero = None
        log_loss_improvement_vs_fixed = None
        log_loss_improvement_vs_zero = None

    def _total(values: dict) -> dict:
        return {p: (values[p] if evaluation_count else None) for p in POLICY_ORDER}

    brier_total_out = _total(brier_total)
    log_loss_total_out = _total(log_loss_total)

    return {
        "sport": plan["sport"],
        "model_name": plan["model_name"],
        "model_version": plan["model_version"],
        "sample_count": len(plan["records"]),
        "min_train_rows": plan["min_train_rows"],
        "warmup_skipped_count": plan["warmup_skipped_count"],
        "evaluation_count": evaluation_count,
        "target_bucket_count": target_bucket_count,
        "half_life_days": plan["half_life_days"],
        "rho_grid": plan["rho_grid"],
        "fixed_comparison_rho": fixed_rho,
        "walk_forward_fitted_total_brier": brier_total_out[POLICY_WALK_FORWARD],
        "fixed_rho_total_brier": brier_total_out[POLICY_FIXED],
        "rho_zero_total_brier": brier_total_out[POLICY_ZERO],
        "walk_forward_fitted_mean_brier": brier_mean[POLICY_WALK_FORWARD],
        "fixed_rho_mean_brier": brier_mean[POLICY_FIXED],
        "rho_zero_mean_brier": brier_mean[POLICY_ZERO],
        "walk_forward_fitted_total_log_loss": log_loss_total_out[POLICY_WALK_FORWARD],
        "fixed_rho_total_log_loss": log_loss_total_out[POLICY_FIXED],
        "rho_zero_total_log_loss": log_loss_total_out[POLICY_ZERO],
        "walk_forward_fitted_mean_log_loss": log_loss_mean[POLICY_WALK_FORWARD],
        "fixed_rho_mean_log_loss": log_loss_mean[POLICY_FIXED],
        "rho_zero_mean_log_loss": log_loss_mean[POLICY_ZERO],
        "brier_improvement_vs_fixed": brier_improvement_vs_fixed,
        "brier_improvement_vs_zero": brier_improvement_vs_zero,
        "log_loss_improvement_vs_fixed": log_loss_improvement_vs_fixed,
        "log_loss_improvement_vs_zero": log_loss_improvement_vs_zero,
        "rho_path": rho_path,
    }


def build_counterfactual_summaries(rows: Iterable[dict], **kwargs) -> list[dict]:
    """
    按 (sport, model_name, model_version) 分组做反事实验证，**只输出足球分组**。

    分组前先做 evaluation_id 全局重复检测；篮球分组跳过。
    返回顺序按 sport / model_name / model_version 字典序，不按指标优劣排序。
    输入为空时返回 `[]`。
    """
    rows = list(rows)
    if not rows:
        return []

    check_global_duplicate_evaluation_ids(rows)

    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        if row.get("sport") != "football":
            continue
        key = (row.get("sport"), row.get("model_name"), row.get("model_version"))
        groups.setdefault(key, []).append(row)

    return [
        validate_counterfactual_group(groups[key], **kwargs)
        for key in sorted(groups, key=lambda k: (str(k[0]), str(k[1]), str(k[2])))
    ]


def validate_all_counterfactual(**kwargs) -> list[dict]:
    """
    便捷入口：读取全部评估样本并做分组反事实验证（只输出足球分组）。

    只做「读取 + 纯计算」，不写入任何存储，也不改动线上模型配置。
    """
    from utils.evaluation_rows import get_all_evaluation_rows

    return build_counterfactual_summaries(get_all_evaluation_rows(), **kwargs)


__all__ = [
    "CLASS_ORDER",
    "POLICY_FIXED",
    "POLICY_ORDER",
    "POLICY_WALK_FORWARD",
    "POLICY_ZERO",
    "DixonColesCounterfactualError",
    "build_counterfactual_summaries",
    "counterfactual_wdl_probabilities",
    "multiclass_brier",
    "multiclass_log_loss",
    "validate_all_counterfactual",
    "validate_counterfactual_group",
]
