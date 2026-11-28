"""
Football V2 shadow candidate —— 联赛环境校准的泊松（隔离实验，不进生产）
=====================================================================

Phase 6-B Workstream D。V1 数学审计（reports/phase6b/football-v1-mathematical-audit/）
确认的主要限制：

- 期望进球分布被结构性压缩（λ_home std ≈ 0.13、draw 概率全队列 0.26–0.30）；
- Elo → 攻防评分斜率仅 0.15/400，且 1165/1168 队样本量低于先验收缩阈值；
- 模型完全没有联赛进球环境项（base_rate=1.35 全局常数）；
- Dixon-Coles rho 只移动平局"水平"，不改变平局"排序"（AUC 对 rho 不敏感）。

本候选 **只** 做一件事：用**因果（settled_at < prediction_generated_at）的联赛
场均总进球先验**重校 λ 总量，再复用生产 `models.poisson_model` 的比分矩阵与
Dixon-Coles 修正重推胜平负。不引入外部依赖、不拟合自由参数（平滑 α=1 与全局
先验 2.7 = base_rate×2 都是文档化常数）、绝不使用赔率（保持纯模型边界）、
绝不改写生产模型或历史快照。

边界（不可绕过）：
- 这是 `RETROSPECTIVE_WALK_FORWARD_RESEARCH` 的评估对象，不是生产模型；
- 不替换 `football-coldstart-1`，不改 `config.MODEL_VERSIONS`；
- 生产路径（scraper / predictor）不 import 本模块；
- 评估只能使用不可变评估行中**预测时刻已存在**的特征
  （λ、联赛、Elo、settled_at、比分）；攻防评分未持久化，故本候选不使用。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from models.poisson_model import score_matrix, win_draw_loss
import config

#: 影子候选显式版本号（与生产版本命名空间隔离）
MODEL_VERSION = "football-v2-league-env-shadow"
MODEL_NAME = "elo-poisson-dixon-coles-league-env"

#: 拉普拉斯平滑 α（与既有因果基线一致的文档化常数，不从队列拟合）
LAPLACE_ALPHA = 1.0
#: 全局总进球先验 = base_rate × 2（文档化常数）
GLOBAL_TOTAL_GOALS_PRIOR = 2.7


def league_environment_prior(
    history_rows: Sequence[dict],
    league: str,
    *,
    alpha: float = LAPLACE_ALPHA,
    global_prior: float = GLOBAL_TOTAL_GOALS_PRIOR,
) -> dict:
    """
    因果联赛环境先验：只用 ``settled_at < as_of`` 的已结算行。

    ``history_rows`` 的每行需要 ``league``、``total_goals``、``settled_at``
    （ISO 带时区）与 ``prediction_generated_at``。返回先验总进球与观测数；
    观测不足时按 Laplace α 向全局先验收缩（绝不返回 NaN 或伪造观测数）。
    """
    totals: list[float] = []
    for row in history_rows:
        if str(row.get("league") or "") != str(league or ""):
            continue
        total = row.get("total_goals")
        if total is None or not isinstance(total, (int, float)) or total < 0:
            continue
        totals.append(float(total))
    k = len(totals)
    mean_observed = (sum(totals) / k) if k else None
    prior = (sum(totals) + alpha * global_prior) / (k + alpha) if k or alpha else global_prior
    return {
        "league": league,
        "observed_matches": k,
        "observed_mean_total_goals": mean_observed,
        "smoothed_prior_total_goals": prior,
        "global_prior_total_goals": global_prior,
        "alpha": alpha,
    }


def predict_v2_probabilities(
    lambda_home: float,
    lambda_away: float,
    prior_total_goals: float,
    *,
    rho: float | None = None,
) -> dict:
    """
    用联赛环境先验重校 λ 总量后，经生产比分矩阵重推胜平负（纯函数）。

    - 只缩放总量（保持 V1 的主客比例），不改动 λ 的相对结构；
    - 缩放因子有界 [0.5, 2.0]，防止极端先验扭曲；
    - 比分矩阵与 Dixon-Coles 修正完全复用生产 ``models.poisson_model``；
    - 返回概率为纯模型概率（**绝不**融合赔率）。
    """
    if rho is None:
        rho = config.MODEL_CONFIG["dixon_coles_rho"]
    lam_h = max(0.05, float(lambda_home))
    lam_a = max(0.05, float(lambda_away))
    current_total = lam_h + lam_a
    if current_total <= 0:
        return {"home_win": 0.0, "draw": 0.0, "away_win": 0.0,
                "scaled_lambda_home": lam_h, "scaled_lambda_away": lam_a,
                "scale_factor": 1.0, "prior_total_goals": prior_total_goals}
    raw_scale = float(prior_total_goals) / current_total
    scale = max(0.5, min(2.0, raw_scale))
    scaled_h = max(0.05, min(6.0, lam_h * scale))
    scaled_a = max(0.05, min(6.0, lam_a * scale))
    matrix = score_matrix(scaled_h, scaled_a, rho=rho)
    home, draw, away = win_draw_loss(matrix)
    return {
        "home_win": home,
        "draw": draw,
        "away_win": away,
        "scaled_lambda_home": scaled_h,
        "scaled_lambda_away": scaled_a,
        "scale_factor": scale,
        "prior_total_goals": prior_total_goals,
    }


def prepare_walk_forward_rows(eval_rows: Iterable[dict]) -> list[dict]:
    """
    把不可变评估行转成走查输入（只取预测时刻已存在的特征）。

    输出行需要 ``prediction_generated_at``（ISO 带时区）、``settled_at``、
    ``league``、``expected_score_data.expected_goals.{home,away}``、
    ``model_probabilities``（百分比刻度）与 ``actual_outcome``。
    缺少任一项的行被排除并计数——绝不补造特征。
    """
    prepared, excluded = [], 0
    for row in eval_rows:
        eg = (row.get("expected_score_data") or {}).get("expected_goals") or {}
        lam_h, lam_a = eg.get("home"), eg.get("away")
        probs = row.get("model_probabilities") or {}
        if lam_h is None or lam_a is None or not row.get("league") \
                or not row.get("prediction_generated_at") or not row.get("settled_at") \
                or row.get("actual_outcome") not in ("home_win", "draw", "away_win") \
                or probs.get("home_win") is None:
            excluded += 1
            continue
        lam_h, lam_a = float(lam_h), float(lam_a)
        prepared.append({
            "match_id": row.get("match_id"),
            "league": str(row.get("league")),
            "prediction_generated_at": str(row.get("prediction_generated_at")),
            "settled_at": str(row.get("settled_at")),
            "lambda_home": lam_h,
            "lambda_away": lam_a,
            "total_goals": float(lam_h) + float(lam_a),  # 占位；走查时用真实比分
            "actual_outcome": row.get("actual_outcome"),
            "v1_probabilities": {
                "home_win": float(probs.get("home_win", 0)) / 100.0,
                "draw": float(probs.get("draw", 0)) / 100.0,
                "away_win": float(probs.get("away_win", 0)) / 100.0,
            },
        })
    return prepared, excluded


def walk_forward_v2(
    prepared: Sequence[dict],
    *,
    final_score_totals: dict[str, float] | None = None,
) -> list[dict]:
    """
    严格时序走查：对每行，先验只来自 ``settled_at < 当前行 prediction_generated_at``
    的**真实全场比分**（``final_score_totals[match_id]``）；同一行自身永远不在
    自己的先验里；输入顺序无关（内部按预测时刻排序）。

    ``final_score_totals`` 映射 match_id -> 真实总进球（必须来自评估行的
    ``final_score`` 字段，由调用方提供；缺失的行只作评分对象、不入先验）。
    """
    ordered = sorted(prepared, key=lambda r: (r["prediction_generated_at"], r["match_id"]))
    # 以预测时刻为界维护"已结算历史"池
    history: list[dict] = []
    results: list[dict] = []
    import bisect  # local import to keep module import surface minimal

    settled_sorted: list[tuple[str, dict]] = []  # (settled_at, row)

    for row in ordered:
        gen = row["prediction_generated_at"]
        # 推进已结算池：settled_at < gen 严格
        while settled_sorted and settled_sorted[0][0] < gen:
            _, settled_row = settled_sorted.pop(0)
            history.append(settled_row)
        prior = league_environment_prior(history, row["league"])
        v2 = predict_v2_probabilities(row["lambda_home"], row["lambda_away"],
                                      prior["smoothed_prior_total_goals"])
        results.append({
            "match_id": row["match_id"],
            "league": row["league"],
            "prediction_generated_at": gen,
            "prior_observed_matches": prior["observed_matches"],
            "prior_total_goals": prior["smoothed_prior_total_goals"],
            "v1_probabilities": row["v1_probabilities"],
            "v2_probabilities": {"home_win": v2["home_win"], "draw": v2["draw"],
                                 "away_win": v2["away_win"]},
            "scale_factor": v2["scale_factor"],
            "actual_outcome": row["actual_outcome"],
        })
        # 当前行结算后可入池（真实比分；缺失则不入）
        total = (final_score_totals or {}).get(row["match_id"])
        if total is not None:
            entry = dict(row)
            entry["total_goals"] = float(total)
            settled_sorted.append((row["settled_at"], entry))
            settled_sorted.sort(key=lambda pair: pair[0])
    return results
