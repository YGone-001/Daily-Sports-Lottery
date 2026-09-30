"""
智能投注策略
============
扫描当日全盘，找出正 EV 漏洞，生成最优串关组合。
支持足球（胜平负）与篮球（胜负）。
"""
from __future__ import annotations

import itertools

from models.predictor import predict_match
from utils.daily_loader import (
    add_time_status,
    get_beijing_now,
    get_match_datetime,
    get_matches_by_date,
)


def _collect_value_bets(sport: str | None = None) -> list[dict]:
    """
    扫描所有「尚未开赛且有赔率」的比赛，收集正 EV 选项。

    注意：跨全部日期扫描，而非仅当日 —— 竞彩通常提前一天开售，
    只扫当日会漏掉绝大多数可投注盘口。
    """
    from utils.team_strength import get_team_profile

    bets: list[dict] = []
    matches = get_matches_by_date("all", sport)
    now = get_beijing_now()

    for raw in matches:
        m = add_time_status(raw)
        # 只要「还没开赛」的（含跨日盘口）
        if m.get("status") not in ("upcoming", "live"):
            continue
        start = get_match_datetime(m)
        if start and start <= now and m.get("status") == "live":
            pass  # 进行中的仍可作参考，但优先未开赛
        odds = m.get("odds")
        if not odds:
            continue
        # 必须有 1X2 赔率才能算价值
        if not (odds.get("home_win") and odds.get("away_win")):
            continue

        home_team = get_team_profile(m.get("home", ""), m.get("league", ""), m.get("sport", "football"), m.get("home_rank"))
        away_team = get_team_profile(m.get("away", ""), m.get("league", ""), m.get("sport", "football"), m.get("away_rank"))

        pred = predict_match(home_team, away_team, odds, m.get("sport", "football"), m.get("league", ""))
        ev = pred.get("ev_analysis", {})

        outcomes = [("home", "主胜", "home_win"), ("draw", "平局", "draw"), ("away", "客胜", "away_win")]
        if m.get("sport") == "basketball":
            outcomes = [("home", "主胜", "home_win"), ("away", "客胜", "away_win")]

        for key, label, odds_key in outcomes:
            a = ev.get(key, {})
            if not a.get("is_value"):
                continue
            try:
                odd_val = float(odds.get(odds_key, 0))
            except (TypeError, ValueError):
                continue
            if odd_val <= 1:
                continue

            prob = pred["model_probabilities"].get(odds_key, 0) / 100.0
            bets.append(
                {
                    "match_id": m.get("id"),
                    "sport": m.get("sport", "football"),
                    "league": m.get("league", ""),
                    "match_name": f"{m.get('home')} vs {m.get('away')}",
                    "date": m.get("date", ""),
                    "time": m.get("time", ""),
                    "outcome": label,
                    "outcome_key": key,
                    "odds": round(odd_val, 2),
                    "prob": round(prob, 4),
                    "ev": a.get("ev", 0.0),
                    "kelly_pct": a.get("kelly_pct", 0.0),
                }
            )

    bets.sort(key=lambda x: x["ev"], reverse=True)
    return bets


def _best_combo(bets: list[dict], size: int, label: str) -> dict | None:
    """从候选集中挑最优 N 串一"""
    if len(bets) < size:
        return None

    best = None
    best_score = -1e9

    for combo in itertools.combinations(bets, size):
        # 同一场比赛不能重复选
        if len({c["match_id"] for c in combo}) < size:
            continue

        comb_odds = 1.0
        comb_prob = 1.0
        for c in combo:
            comb_odds *= c["odds"]
            comb_prob *= c["prob"]

        comb_ev = comb_prob * comb_odds - 1
        if comb_ev <= 0:
            continue

        # 稳定性筛选：EV 高且命中率不太低
        score = comb_ev * (comb_prob ** 0.5)
        if score > best_score:
            best_score = score
            b = comb_odds - 1
            q = 1.0 - comb_prob
            kelly = ((comb_prob * b - q) / b) if b > 0 else 0
            kelly_pct = max(0.0, kelly * 0.25) * 100

            best = {
                "type": label,
                "legs": [dict(c) for c in combo],
                "combined_odds": round(comb_odds, 2),
                "combined_prob": round(comb_prob * 100, 2),
                "ev": round(comb_ev * 100, 2),
                "kelly_pct": round(kelly_pct, 2),
            }

    return best


def generate_accumulators(sport: str | None = "football") -> dict:
    """
    生成策略推荐。
    返回: {value_bets_count, single_bets, best_double, best_treble, ...}
    """
    bets = _collect_value_bets(sport)

    return {
        "value_bets_count": len(bets),
        "single_bets": bets[:10],
        "best_double": _best_combo(bets, 2, "二串一 (Double)"),
        "best_treble": _best_combo(bets, 3, "三串一 (Treble)"),
        "best_four": _best_combo(bets, 4, "四串一 (4-Fold)"),
    }
