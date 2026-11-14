"""NBA 模型表现评估与按时间切分的走查回测（只读、确定性）。

指标
----
direction_accuracy / binary_brier / binary_log_loss / 去水市场对照 / 模型-市场差距。
模型与市场对照始终在**同一批合格比赛**上计算。

走查回测（RETROSPECTIVE_WALK_FORWARD_RESEARCH）
----------------------------------------------
按**北京比赛日**分块、扩张窗口：预测日期 D 的全部比赛时，实力清单只用
``game_end < D 当日 00:00`` 的历史比赛。因此：
- 被预测比赛自身绝不进入训练（无未来泄漏）；
- 同日比赛互不 informing（无同日泄漏）；
- 不做随机 shuffled 交叉验证。

回溯重构的预测**不是**原始实时赛前快照；原始八场队列作为已接受历史证据单独保留。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Iterable

from utils.nba_schedule import BEIJING_TZ, NBA_FRANCHISE_MAPPINGS
from utils.nba_team_strength import compute_team_strength, game_end_utc

_EPS = 1e-12
_CODE_TO_CHINESE = {e.canonical_code: e.chinese_names[0] for e in NBA_FRANCHISE_MAPPINGS}
REAL_NBA_LEAGUE_LABEL = "美国职业篮球联盟"


def directional_hit(p_home: float, home_win: bool) -> bool:
    return (p_home > 0.5) == bool(home_win)


def brier(p_home: float, y: int) -> float:
    return (p_home - y) ** 2


def log_loss(p_home: float, y: int) -> float:
    p = max(_EPS, min(1.0 - _EPS, p_home))
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def evaluate_rows(rows: Iterable[dict]) -> dict:
    """rows: [{"p_home": float, "y": 0|1, "market_home": float|None}]。"""
    rows = [r for r in rows if isinstance(r, dict) and r.get("p_home") is not None]
    n = len(rows)
    if n == 0:
        return {"sample_count": 0, "direction_accuracy": None, "binary_brier_score": None,
                "binary_log_loss": None, "market_comparable_count": 0,
                "normalized_market_brier": None, "normalized_market_log_loss": None,
                "mean_absolute_model_market_gap": None, "mean_signed_model_market_gap": None}
    hits = sum(1 for r in rows if directional_hit(r["p_home"], r["y"] == 1))
    brier_sum = sum(brier(r["p_home"], r["y"]) for r in rows)
    ll_sum = sum(log_loss(r["p_home"], r["y"]) for r in rows)
    market = [r for r in rows if r.get("market_home") is not None]
    mn = len(market)
    out = {
        "sample_count": n,
        "direction_accuracy": round(hits / n, 4),
        "direction_hits": hits,
        "binary_brier_score": round(brier_sum / n, 4),
        "binary_log_loss": round(ll_sum / n, 4),
        "market_comparable_count": mn,
        "normalized_market_brier": None,
        "normalized_market_log_loss": None,
        "mean_absolute_model_market_gap": None,
        "mean_signed_model_market_gap": None,
    }
    if mn:
        out["normalized_market_brier"] = round(sum(brier(r["market_home"], r["y"]) for r in market) / mn, 4)
        out["normalized_market_log_loss"] = round(sum(log_loss(r["market_home"], r["y"]) for r in market) / mn, 4)
        gaps = [r["p_home"] - r["market_home"] for r in market]
        out["mean_absolute_model_market_gap"] = round(sum(abs(g) for g in gaps) / mn, 4)
        out["mean_signed_model_market_gap"] = round(sum(gaps) / mn, 4)
    return out


def _v1_coldstart_probability(home_code: str, away_code: str, league_label: str) -> float | None:
    """复现生产 V1 冷启动（纯函数构造档案，不触球队库缓存/落盘）。"""
    from models.basketball_model import predict_basketball
    from utils.team_strength import _estimate_elo
    hn, an = _CODE_TO_CHINESE.get(home_code), _CODE_TO_CHINESE.get(away_code)
    if not hn or not an:
        return None
    home = {"name": hn, "elo_rating": _estimate_elo(league_label, None, "basketball", hn), "matches_played": 0}
    away = {"name": an, "elo_rating": _estimate_elo(league_label, None, "basketball", an), "matches_played": 0}
    res = predict_basketball(home, away, None, league_label)
    return res["model_probabilities"]["home_win"] / 100.0


def walk_forward_backtest(
    games: Iterable[dict],
    *,
    params: dict | None = None,
    include_v1: bool = True,
    league_label: str = REAL_NBA_LEAGUE_LABEL,
    min_history_games: int = 0,
) -> dict:
    """按北京比赛日分块的扩张窗口走查回测（V2 影子，可选 V1 冷启动对照）。"""
    from models.basketball_model_v2 import predict_from_inventory, STATUS_OK

    valid = []
    for g in games:
        if not isinstance(g, dict):
            continue
        if g.get("home_code") and g.get("away_code") and g.get("beijing_date") \
                and type(g.get("home_final_score")) is int and type(g.get("away_final_score")) is int:
            valid.append(g)
    valid.sort(key=lambda g: (g["scheduled_start_utc"] or "", g["provider_game_id"]))

    by_date: dict[str, list[dict]] = {}
    for g in valid:
        by_date.setdefault(g["beijing_date"], []).append(g)

    v2_rows, v1_rows, per_date = [], [], []
    insufficient = 0
    evaluated_games = 0
    for d in sorted(by_date):
        day_start = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=BEIJING_TZ)
        prior = [g for g in valid if (game_end_utc(g) or day_start) < day_start]
        if len(prior) < min_history_games:
            continue
        inventory = compute_team_strength(prior, as_of=day_start, params=params)
        date_v2, date_v1 = [], []
        for g in by_date[d]:
            y = 1 if g["home_final_score"] > g["away_final_score"] else 0
            pred = predict_from_inventory(inventory, g["home_code"], g["away_code"])
            if pred.get("status") != STATUS_OK:
                insufficient += 1
                continue
            p_home = pred["raw_model_probabilities"]["home_win"]
            row = {"p_home": p_home, "y": y, "market_home": None,
                   "provider_game_id": g["provider_game_id"], "beijing_date": d}
            v2_rows.append(row)
            date_v2.append(row)
            evaluated_games += 1
            if include_v1:
                p1 = _v1_coldstart_probability(g["home_code"], g["away_code"], league_label)
                if p1 is not None:
                    r1 = {"p_home": p1, "y": y, "market_home": None,
                          "provider_game_id": g["provider_game_id"], "beijing_date": d}
                    v1_rows.append(r1)
                    date_v1.append(r1)
        per_date.append({"beijing_date": d, "history_games": len(prior),
                         "evaluated": len(date_v2),
                         "v2": evaluate_rows(date_v2),
                         "v1": evaluate_rows(date_v1) if include_v1 else None})

    return {
        "research_label": "RETROSPECTIVE_WALK_FORWARD_RESEARCH",
        "is_original_realtime_snapshot": False,
        "split_strategy": "CHRONOLOGICAL_DATE_BLOCK_EXPANDING_WINDOW",
        "same_date_leakage": False,
        "future_result_leakage": False,
        "random_shuffled_cv": False,
        "total_input_games": len(valid),
        "evaluated_games": evaluated_games,
        "insufficient_coverage_skipped": insufficient,
        "game_dates_evaluated": len(per_date),
        "v2_shadow": evaluate_rows(v2_rows),
        "v1_coldstart": evaluate_rows(v1_rows) if include_v1 else None,
        "per_date": per_date,
        "promotion_authorized": False,
        "profitability_claimed": False,
    }