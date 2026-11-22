"""
评估基线（Phase 4-D Workstream B）
=================================
在**不可变评估样本**之上计算与既有分类指标可比的简单基线，并做配对（同 fixture）
差异与配对自助法（bootstrap）区间。与 ``utils/classification_evaluation`` 共用
概率校验与类别约定，不另立口径。

边界与防泄漏
------------
- 扩展历史频率基线（因果口径，Phase 5-A 修正）：一个历史结果影响后续预测的
  唯一准入条件是 ``prior_result_known_at (settled_at) < current prediction_generated_at``；
  settled_at 缺失/畸形/naive 的结果不进入因果历史；同一开赛时刻互不泄漏；
  输入顺序无关。绝不使用最终赛季频率回填历史行，也绝不从 kickoff 推断可得性。
- 固定先验基线：足球 (home, draw, away) = (0.45, 0.25, 0.30)、篮球 (0.55, 0.45)，
  为**文档化的通用先验假设**（非本队列拟合值）。
- 市场基线：仅在调用方显式给定的行上计算（调用方负责市场证据有效性），
  本模块不判定市场方向可信度。
- 纯读取 + 纯计算：不写任何存储，不读 daily_matches，不重算模型。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Sequence

from utils.classification_evaluation import classes_for_sport, validate_row

# 文档化固定先验（假设与出处见模块 docstring；不从本队列拟合）
FIXED_PRIOR = {
    "football": {"home_win": 0.45, "draw": 0.25, "away_win": 0.30},
    "basketball": {"home_win": 0.55, "away_win": 0.45},
}

BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261122  # 固定种子：结果可复现


def _probs_for_row(row: dict):
    classes, probs, actual = validate_row(row)
    return classes, dict(zip(classes, probs)), actual


def uniform_baseline(rows: Iterable[dict]) -> dict:
    """均匀概率基线：Brier/LogLoss 逐行可定义；方向命中率为 1/K（随机并列期望）。"""
    rows = list(rows)
    if not rows:
        return {"n": 0}
    classes = classes_for_sport(rows[0]["sport"])
    k = len(classes)
    p = {c: 1.0 / k for c in classes}
    brier = 0.0
    logloss = 0.0
    correct = 0
    for r in rows:
        _c, _p, actual = _probs_for_row(r)
        brier += sum((p[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
        logloss += -math.log(max(p[actual], 1e-15))
        # 均匀分布无信息：方向命中的期望为 1/K（并列随机裁决）
        correct += 1.0 / k
    n = len(rows)
    return {
        "n": n,
        "direction_accuracy": correct / n,
        "multiclass_brier": brier / n,
        "log_loss": logloss / n,
    }


def expanding_frequency_baseline(rows: Iterable[dict]) -> dict:
    """因果扩展频率基线（Phase 5-A Workstream A 修正后的口径）。

    一个历史结果要影响后续预测，必须证明**结果在预测时刻已可得**：

        prior_result_known_at (settled_at) < current prediction_generated_at

    - ``settled_at`` 是保守的「应用已知」边界（结算创建时刻；结果实际可得更早，
      因此只会**少**用先验，不会泄漏）。``materialized_at`` 是评估物化时刻，
      **不得**当作赛果观测时刻使用。
    - ``settled_at`` 缺失/畸形/naive → 该行结果**不进入**因果频率历史（该行仍被评分）。
    - ``prediction_generated_at`` 缺失/畸形/naive → 该行无法建立因果时间线，
      以均匀先验评分并如实计数。
    - 同一开赛时刻的多场比赛互不泄漏（排序只按可得时刻与预测时刻，不按 kickoff）；
      输入顺序无关（内部按 (prediction_generated_at, evaluation_id) 确定性排序）。
    """
    rows = list(rows)
    if not rows:
        return {"n": 0}

    parsed = []
    for r in rows:
        classes, probs, actual = validate_row(r)
        gen = _parse_aware_dt(r.get("prediction_generated_at"))
        settled = _parse_aware_dt(r.get("settled_at"))
        parsed.append({
            "row": r,
            "classes": classes,
            "model_probabilities": dict(zip(classes, probs)),
            "actual": actual,
            "generated_at": gen,
            "settled_at": settled,
            "evaluation_id": r.get("evaluation_id") or "",
        })

    k = len(classes)
    sentinel_max = datetime.max.replace(tzinfo=timezone.utc)

    # 评分时间线：按预测时刻排序（缺失者排最后，仅以均匀先验评分——
    # 预测时刻未知时无法建立「当时已知什么」的因果视图）。
    scored = sorted(
        parsed,
        key=lambda e: (
            e["generated_at"] is None,
            e["generated_at"] or sentinel_max,
            e["evaluation_id"],
        ),
    )
    # 可贡献的因果历史：按 settled_at 排序的有效结算行
    contributors = sorted(
        [e for e in parsed if e["settled_at"] is not None],
        key=lambda e: (e["settled_at"], e["evaluation_id"]),
    )

    counts = {c: 0 for c in classes}
    total = 0
    contributor_index = 0
    brier = 0.0
    logloss = 0.0
    correct = 0.0
    per_row = []
    rows_without_valid_generated_at = 0
    for e in scored:
        if e["generated_at"] is None:
            # 预测时刻未知：无法建立因果视图 → 均匀先验（不计入任何先验观测）
            rows_without_valid_generated_at += 1
            p = {c: 1.0 / k for c in classes}
            prior_count = None
        else:
            # 指针推进：把 settled_at < 当前预测时刻 的结果纳入因果历史
            while (
                contributor_index < len(contributors)
                and contributors[contributor_index]["settled_at"] < e["generated_at"]
            ):
                counts[contributors[contributor_index]["actual"]] += 1
                total += 1
                contributor_index += 1
            denom = total + k  # Laplace α=1
            p = {c: (counts[c] + 1) / denom for c in classes}
            prior_count = total
        brier += sum((p[c] - (1.0 if c == e["actual"] else 0.0)) ** 2 for c in classes)
        logloss += -math.log(max(p[e["actual"]], 1e-15))
        pred = max(classes, key=lambda c: p[c])
        correct += pred == e["actual"]
        per_row.append({
            "evaluation_id": e["evaluation_id"],
            "prediction_generated_at": e["row"].get("prediction_generated_at"),
            "settled_at": e["row"].get("settled_at"),
            "prior_observation_count": prior_count,
            "probabilities": p,
            "predicted": pred,
            "correct": pred == e["actual"],
        })
    n = len(rows)
    contributors_excluded = sum(1 for e in parsed if e["settled_at"] is None)
    return {
        "n": n,
        "direction_accuracy": correct / n,
        "multiclass_brier": brier / n,
        "log_loss": logloss / n,
        "causal_accounting": {
            "boundary_rule": "settled_at < prediction_generated_at",
            "contributors_used": total,
            "contributors_excluded_invalid_or_missing_settled_at": contributors_excluded,
            "rows_scored_without_valid_generated_at": rows_without_valid_generated_at,
        },
        "per_row": per_row,
    }


def _parse_aware_dt(value) -> "datetime | None":
    """解析 ISO 时间；naive（无时区）或非法 → None（失败关闭）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None
    return dt


def fixed_prior_baseline(rows: Iterable[dict]) -> dict:
    """文档化固定先验基线（不从本队列拟合）。"""
    rows = list(rows)
    if not rows:
        return {"n": 0}
    classes = classes_for_sport(rows[0]["sport"])
    prior = FIXED_PRIOR[rows[0]["sport"]]
    total = sum(prior[c] for c in classes)
    p = {c: prior[c] / total for c in classes}
    brier = 0.0
    logloss = 0.0
    correct = 0
    for r in rows:
        _c, _p, actual = _probs_for_row(r)
        brier += sum((p[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
        logloss += -math.log(max(p[actual], 1e-15))
        correct += max(classes, key=lambda c: p[c]) == actual
    n = len(rows)
    return {
        "n": n,
        "probabilities": p,
        "direction_accuracy": correct / n,
        "multiclass_brier": brier / n,
        "log_loss": logloss / n,
    }


def market_baseline_scores(rows: Iterable[dict]) -> dict | None:
    """市场隐含概率基线（仅在调用方给定的、市场证据齐全的行上计算）。"""
    rows = list(rows)
    if not rows:
        return None
    classes = classes_for_sport(rows[0]["sport"])
    brier = 0.0
    logloss = 0.0
    correct = 0
    for r in rows:
        imp = r["market_implied_probabilities"]
        total = sum(float(imp[c]) for c in classes)
        p = {c: float(imp[c]) / total for c in classes}
        _c, _p, actual = _probs_for_row(r)
        brier += sum((p[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
        logloss += -math.log(max(p[actual], 1e-15))
        correct += max(classes, key=lambda c: p[c]) == actual
    n = len(rows)
    return {
        "n": n,
        "direction_accuracy": correct / n,
        "multiclass_brier": brier / n,
        "log_loss": logloss / n,
    }


def model_scores(rows: Iterable[dict]) -> dict:
    """模型侧指标（与基线同一套计算，保证配对可比）。"""
    rows = list(rows)
    if not rows:
        return {"n": 0}
    classes = classes_for_sport(rows[0]["sport"])
    brier = 0.0
    logloss = 0.0
    correct = 0
    for r in rows:
        _c, p, actual = _probs_for_row(r)
        brier += sum((p[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
        logloss += -math.log(max(p[actual], 1e-15))
        correct += max(classes, key=lambda c: p[c]) == actual
    n = len(rows)
    return {
        "n": n,
        "direction_accuracy": correct / n,
        "multiclass_brier": brier / n,
        "log_loss": logloss / n,
    }


def paired_bootstrap(rows: list[dict], model_prob_fn, baseline_prob_fn, iterations: int = BOOTSTRAP_ITERATIONS) -> dict:
    """配对自助法：同一行集合上有放回重采样，保留逐行配对关系。

    返回方向命中差、Brier 差、LogLoss 差的 95% 区间（2.5% / 97.5% 分位）
    与点估计。``model_prob_fn(row) -> dict`` 与 ``baseline_prob_fn(row) -> dict``
    由调用方给定（保证每行配对）。
    """
    import random

    rows = list(rows)
    n = len(rows)
    if n == 0:
        return {"n": 0}
    classes = classes_for_sport(rows[0]["sport"])

    def _scores(subset):
        acc_m = acc_b = 0
        brier_m = brier_b = log_m = log_b = 0.0
        for r in subset:
            _c, _p, actual = _probs_for_row(r)
            pm = model_prob_fn(r)
            pb = baseline_prob_fn(r)
            acc_m += max(classes, key=lambda c: pm[c]) == actual
            acc_b += max(classes, key=lambda c: pb[c]) == actual
            brier_m += sum((pm[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
            brier_b += sum((pb[c] - (1.0 if c == actual else 0.0)) ** 2 for c in classes)
            log_m += -math.log(max(pm[actual], 1e-15))
            log_b += -math.log(max(pb[actual], 1e-15))
        m = len(subset)
        return {
            "acc_diff": (acc_m - acc_b) / m,
            "brier_diff": (brier_m - brier_b) / m,  # 负值 = 模型更优
            "logloss_diff": (log_m - log_b) / m,
        }

    rng = random.Random(BOOTSTRAP_SEED)
    point = _scores(rows)
    samples = {"acc_diff": [], "brier_diff": [], "logloss_diff": []}
    for _ in range(iterations):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        s = _scores(sample)
        for key in samples:
            samples[key].append(s[key])

    def ci(values):
        values = sorted(values)
        lo = values[int(0.025 * len(values))]
        hi = values[min(len(values) - 1, int(0.975 * len(values)))]
        return [round(lo, 4), round(hi, 4)]

    return {
        "n": n,
        "iterations": iterations,
        "seed": BOOTSTRAP_SEED,
        "point_estimate": {k: round(v, 4) for k, v in point.items()},
        "ci95": {k: ci(v) for k, v in samples.items()},
        "note": "配对重采样保留逐行配对；日期/球队相关性对独立性的限制见报告披露",
    }
