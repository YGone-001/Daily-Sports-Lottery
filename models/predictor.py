"""
比赛预测主模块（动态球队版）
==========================
- 足球：Elo -> 期望进球 -> 泊松 + Dixon-Coles -> 胜平负/比分/大小球
- 篮球：委托给 basketball_model
- 赔率融合 + EV/Kelly 价值分析
"""
from __future__ import annotations

import math

import config
from models.poisson_model import (
    expected_goals,
    expected_total_goals,
    score_matrix,
    top_scores,
    win_draw_loss,
)


# ---------------------------------------------------------------------------
# 赔率工具
# ---------------------------------------------------------------------------

def odds_to_probabilities(odds: dict) -> tuple[float, float, float] | None:
    """1X2 赔率 -> 去水后的隐含概率"""
    try:
        home = float(odds["home_win"])
        draw = float(odds["draw"])
        away = float(odds["away_win"])
    except (KeyError, TypeError, ValueError):
        return None
    if home <= 1 or draw <= 1 or away <= 1:
        return None
    implied = [1 / home, 1 / draw, 1 / away]
    total = sum(implied)
    return implied[0] / total, implied[1] / total, implied[2] / total


def blend_with_odds(model_probs, odds):
    """模型概率与市场隐含概率融合"""
    market = odds_to_probabilities(odds or {})
    weight = max(0.0, min(1.0, config.MODEL_CONFIG.get("odds_weight", 0.0)))
    if not market or weight <= 0:
        return model_probs, None
    blended = tuple(
        mp * (1 - weight) + kp * weight for mp, kp in zip(model_probs, market)
    )
    return blended, market


# ---------------------------------------------------------------------------
# 悬念指数
# ---------------------------------------------------------------------------

def suspense_index(p_home: float, p_draw: float, p_away: float, total_lambda: float = 2.5):
    probs = sorted([p_home, p_draw, p_away], reverse=True)
    gap = probs[0] - probs[1]
    score = probs[0] * 0.6 + gap * 0.4

    if score >= 0.55:
        stars = 1
    elif score >= 0.40:
        stars = 2
    elif score >= 0.32:
        stars = 3
    elif score >= 0.26:
        stars = 4
    else:
        stars = 5

    if total_lambda < 2.0 and stars < 5:
        stars += 1
    elif total_lambda >= 3.2 and stars > 1:
        stars -= 1

    labels = {1: "强弱悬殊", 2: "优势明显", 3: "暗藏杀机", 4: "势均力敌", 5: "生死苦战"}
    return stars, labels[stars]


# ---------------------------------------------------------------------------
# 足球预测
# ---------------------------------------------------------------------------

def predict_football(home_team: dict, away_team: dict, odds: dict | None = None) -> dict:
    cfg = config.MODEL_CONFIG
    scale = cfg["elo_scale"]

    home_elo = home_team.get("elo_rating", 1600)
    away_elo = away_team.get("elo_rating", 1600)

    # 主场优势体现在进攻加成上
    ha = cfg["home_advantage_elo"]
    elo_diff = (home_elo + ha - away_elo) / scale

    home_boost = 1.0 + elo_diff * cfg["attack_sensitivity"]
    away_boost = 1.0 - elo_diff * cfg["away_attack_sensitivity"]

    base_rate = cfg["base_goals_per_match"]

    lambda_home = expected_goals(
        attack=home_team.get("attack_rating", 0.5) * max(0.4, min(2.2, home_boost)),
        defense_opponent=away_team.get("defense_rating", 0.5),
        base_rate=base_rate,
    )
    lambda_away = expected_goals(
        attack=away_team.get("attack_rating", 0.5) * max(0.4, min(2.2, away_boost)),
        defense_opponent=home_team.get("defense_rating", 0.5),
        base_rate=base_rate,
    )

    # 大小球盘口校准
    if odds and "over_2_5" in odds and "under_2_5" in odds:
        try:
            o_odds = float(odds["over_2_5"])
            u_odds = float(odds["under_2_5"])
            if o_odds > 1 and u_odds > 1:
                p_over = (1 / o_odds) / ((1 / o_odds) + (1 / u_odds))
                low, high = 0.1, 7.0
                market_lambda = 2.7
                for _ in range(20):
                    mid = (low + high) / 2.0
                    p_u = math.exp(-mid) * (1 + mid + mid**2 / 2.0)
                    if 1.0 - p_u < p_over:
                        low = mid
                    else:
                        high = mid
                    market_lambda = mid
                cur = lambda_home + lambda_away
                if cur > 0:
                    sc = market_lambda / cur
                    lambda_home *= sc
                    lambda_away *= sc
        except (ValueError, TypeError):
            pass

    matrix = score_matrix(lambda_home, lambda_away, rho=cfg["dixon_coles_rho"])
    model_home, model_draw, model_away = win_draw_loss(matrix)
    (p_home, p_draw, p_away), market_probs = blend_with_odds(
        (model_home, model_draw, model_away), odds
    )

    # EV / Kelly
    ev_analysis = {
        "home": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
        "draw": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
        "away": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
    }

    if odds and market_probs:
        try:
            ho = float(odds.get("home_win", 0))
            do = float(odds.get("draw", 0))
            ao = float(odds.get("away_win", 0))
            frac = cfg["kelly_fraction"]
            thr = cfg["value_threshold"]

            def kelly(mp, o):
                if o <= 1.0:
                    return 0.0
                b = o - 1.0
                q = 1.0 - mp
                return max(0.0, ((mp * b - q) / b) * frac)

            for key, mp, o in (("home", model_home, ho), ("draw", model_draw, do), ("away", model_away, ao)):
                ev = mp * o - 1
                ev_analysis[key] = {
                    "ev": round(ev * 100, 1),
                    "is_value": ev > thr,
                    "kelly_pct": round(kelly(mp, o) * 100, 2),
                }
        except (ValueError, TypeError):
            pass

    top3 = top_scores(matrix, 3)
    stars, label = suspense_index(p_home, p_draw, p_away, lambda_home + lambda_away)
    goals_pred = expected_total_goals(lambda_home, lambda_away)

    return {
        "sport": "football",
        "home_team": {
            "name": home_team.get("name", ""),
            "code": (home_team.get("name", "") or "")[:3],
            "flag": home_team.get("flag", ""),
            "strength": round(home_elo + ha, 1),
        },
        "away_team": {
            "name": away_team.get("name", ""),
            "code": (away_team.get("name", "") or "")[:3],
            "flag": away_team.get("flag", ""),
            "strength": round(away_elo, 1),
        },
        "probabilities": {
            "home_win": round(p_home * 100),
            "draw": round(p_draw * 100),
            "away_win": round(p_away * 100),
        },
        "model_probabilities": {
            "home_win": round(model_home * 100),
            "draw": round(model_draw * 100),
            "away_win": round(model_away * 100),
        },
        "market": {
            "odds": odds or {},
            "implied_probabilities": {
                "home_win": round(market_probs[0] * 100) if market_probs else None,
                "draw": round(market_probs[1] * 100) if market_probs else None,
                "away_win": round(market_probs[2] * 100) if market_probs else None,
            },
            "weight": cfg["odds_weight"] if market_probs else 0,
        },
        "top_scores": top3,
        "expected_goals": {
            "home": round(lambda_home, 2),
            "away": round(lambda_away, 2),
        },
        "goals_prediction": goals_pred,
        "suspense": {"stars": stars, "label": label},
        "ev_analysis": ev_analysis,
        "key_info": {
            "home_notes": [f"Elo {home_elo:.0f}"],
            "away_notes": [f"Elo {away_elo:.0f}"],
            "home_injuries": [],
            "away_injuries": [],
            "home_key_players": [],
            "away_key_players": [],
            "home_rest_days": 0,
            "away_rest_days": 0,
        },
        "radar": {
            "labels": ["进攻火力 (ATT)", "防守壁垒 (DEF)", "近期状态 (FORM)", "大赛底蕴 (EXP)", "阵容深度 (DEP)", "机构战力 (ELO)"],
            "home": _football_radar(home_team),
            "away": _football_radar(away_team),
        },
    }


def _football_radar(team: dict) -> list[int]:
    """足球六维雷达"""
    elo = team.get("elo_rating", 1600)
    att = min(100, team.get("attack_rating", 0.5) * 100)
    dfn = min(100, team.get("defense_rating", 0.5) * 100)
    played = max(1, team.get("matches_played", 0))
    win_rate = team.get("wins", 0) / played * 100 if team.get("matches_played") else 50.0
    elo_score = max(0, min(100, (elo - 1300) / 6.0))
    deep = min(100, 50 + played * 2)
    return [
        round(att),
        round(dfn),
        round(win_rate),
        round(min(100, 45 + win_rate * 0.4)),
        round(deep),
        round(elo_score),
    ]


# ---------------------------------------------------------------------------
# 统一入口（按 sport 分发）
# ---------------------------------------------------------------------------

def predict_match(home_team: dict, away_team: dict, odds: dict | None = None, sport: str = "football", league: str = "") -> dict:
    if sport == "basketball":
        from models.basketball_model import predict_basketball
        return predict_basketball(home_team, away_team, odds, league)
    return predict_football(home_team, away_team, odds)
