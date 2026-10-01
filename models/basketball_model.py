"""
篮球预测模型
============
基于「得分分布」而非泊松进球：
- 用 Elo 差推算双方预期得分 (expected points)
- 用正态分布建模得分波动
- 支持：胜负、让分、大小分
"""
from __future__ import annotations

import math

import config


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    """正态分布累积概率 P(X <= x)"""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def expected_points(
    elo_self: float,
    elo_opp: float,
    base_total: float,
    is_home: bool,
    pace: float = 1.0,
) -> float:
    """
    计算单队预期得分。

    采用**对称的分差（margin）表述**：

        elo_margin   = (elo_self - elo_opp) / basketball_elo_scale * 10
        venue_margin = +basketball_home_advantage  (主队)
                       -basketball_home_advantage  (客队)
        points       = base_total / 2 + (elo_margin + venue_margin) / 2

    单位约定（重要）：
    - `basketball_home_advantage` 的单位是**篮球比分差（points）**，
      即「主队获得的预期净胜分加成」，不是 Elo 分数、不是百分比、
      也不是任意乘数。默认 `2.5` 就表示主队 +2.5 分的预期分差。
    - `basketball_elo_scale = 200` 的含义保持既有定义：
      200 分 Elo 差 ≈ 10 分预期分差。

    因此主客对的预期分差恒为：

        margin = elo 分差项 + 主场优势
               = (home_elo - away_elo) / basketball_elo_scale * 10
                 + basketball_home_advantage

    主场优势对称地把分数从客队移给主队（各 1/2），
    因此在 `pace = 1.0` 且无外部总分线时，两队预期得分之和恰为 `base_total`。
    """
    cfg = config.MODEL_CONFIG
    half = base_total / 2.0

    elo_margin = (elo_self - elo_opp) / cfg["basketball_elo_scale"] * 10.0
    venue_margin = (
        cfg["basketball_home_advantage"]
        if is_home
        else -cfg["basketball_home_advantage"]
    )

    points = half + (elo_margin + venue_margin) / 2.0
    points *= pace
    return max(60.0, points)


def predict_basketball(
    home_team: dict,
    away_team: dict,
    odds: dict | None = None,
    league: str = "",
) -> dict:
    """
    篮球比赛预测。
    返回结构对齐 predictor.predict_match，便于前端统一渲染。
    """
    cfg = config.MODEL_CONFIG

    # 联赛基准总分
    base_total = cfg["basketball_base_total"]
    if "CBA" in league:
        base_total = cfg["basketball_base_total_cba"]

    # 若盘口给出总分线 (yszf)，直接采用市场盘口（比联赛基准更准）
    if odds and odds.get("total_line"):
        try:
            line = float(odds["total_line"])
            if 100 < line < 300:
                base_total = line
        except (TypeError, ValueError):
            pass

    home_elo = home_team.get("elo_rating", 1700)
    away_elo = away_team.get("elo_rating", 1700)

    pace = cfg["basketball_pace"]
    exp_home = expected_points(home_elo, away_elo, base_total, True, pace)
    exp_away = expected_points(away_elo, home_elo, base_total, False, pace)

    # 修正：确保总分贴合基准
    total = exp_home + exp_away
    if total > 0:
        scale = base_total / total
        exp_home *= scale
        exp_away *= scale

    sigma = cfg["basketball_score_std"]

    # 胜负概率：分差的分布（两队独立正态，差也是正态）
    diff = exp_home - exp_away
    diff_sigma = sigma * math.sqrt(2.0)
    p_home = 1.0 - _norm_cdf(0.0, diff, diff_sigma)  # P(home_score > away_score)
    # 篮球无平局（加时），平局概率归入主客
    p_draw = 0.0
    p_away = 1.0 - p_home

    # 主客独赢盘「融合前」的模型胜负概率。
    # 这是模型自身的判断，是 model_probabilities / EV / Kelly 的权威来源，
    # 与下面的展示概率 p_home / p_away 解耦（对齐足球的 model_* 语义）。
    model_home = p_home
    model_draw = p_draw
    model_away = p_away

    # 总分的离散分布（10 分一档）
    total_sigma = diff_sigma
    totals_dist = {}
    for t in range(int(base_total - 60), int(base_total + 61), 5):
        # 用分差分布的对称性近似总分集中在基准附近
        prob = _norm_cdf(t + 2.5, base_total, total_sigma) - _norm_cdf(t - 2.5, base_total, total_sigma)
        totals_dist[t] = max(0.0, prob)
    s = sum(totals_dist.values())
    if s > 0:
        totals_dist = {k: v / s for k, v in totals_dist.items()}

    most_likely_total = max(totals_dist, key=totals_dist.get) if totals_dist else int(base_total)

    # 大小分（以盘口线或基准为准）
    over_line = base_total
    if odds and odds.get("total_line"):
        try:
            over_line = float(odds["total_line"])
        except (TypeError, ValueError):
            pass
    p_over = sum(p for t, p in totals_dist.items() if t > over_line)

    # 若市场给出大小分赔率，用其校准 p_over
    if odds and odds.get("over") and odds.get("under"):
        try:
            oo = float(odds["over"])
            uo = float(odds["under"])
            if oo > 1 and uo > 1:
                imp_o = (1 / oo) / ((1 / oo) + (1 / uo))
                p_over = p_over * 0.8 + imp_o * 0.2
        except (TypeError, ValueError):
            pass

    # 让分（主队视角）
    spread_line = round(-diff, 1)  # 负号：主队让分

    # 赔率融合
    market_probs = None
    if odds:
        try:
            ho = float(odds["home_win"])
            ao = float(odds["away_win"])
            if ho > 1 and ao > 1:
                imp_h, imp_a = 1 / ho, 1 / ao
                tot = imp_h + imp_a
                market_probs = (imp_h / tot, 0.0, imp_a / tot)
        except (KeyError, TypeError, ValueError):
            market_probs = None

    # 只有「展示用」的 p_home / p_away 会被主客独赢盘融合；
    # model_home / model_away 在此之后绝不再被修改。
    weight = cfg["odds_weight"]
    if market_probs and weight > 0:
        p_home = p_home * (1 - weight) + market_probs[0] * weight
        p_away = p_away * (1 - weight) + market_probs[2] * weight
        norm = p_home + p_away
        if norm > 0:
            p_home /= norm
            p_away /= norm

    # EV / Kelly
    ev_analysis = {
        "home": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
        "draw": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
        "away": {"ev": 0.0, "is_value": False, "kelly_pct": 0.0},
    }
    if odds:
        try:
            ho = float(odds.get("home_win", 0))
            ao = float(odds.get("away_win", 0))

            def kelly(model_p, o):
                if o <= 1.0:
                    return 0.0
                b = o - 1.0
                q = 1.0 - model_p
                f = (model_p * b - q) / b
                return max(0.0, f * cfg["kelly_fraction"])

            # EV / Kelly 一律使用「融合前」的模型概率，与赔率比较。
            # 若改用已含市场概率的展示概率，等价于拿市场与自身比较。
            ev_home = model_home * ho - 1
            ev_away = model_away * ao - 1
            thr = cfg["value_threshold"]
            ev_analysis["home"] = {
                "ev": round(ev_home * 100, 1),
                "is_value": ev_home > thr,
                "kelly_pct": round(kelly(model_home, ho) * 100, 2),
            }
            ev_analysis["away"] = {
                "ev": round(ev_away * 100, 1),
                "is_value": ev_away > thr,
                "kelly_pct": round(kelly(model_away, ao) * 100, 2),
            }
        except (ValueError, TypeError):
            pass

    # 悬念指数（分差越小越悬念）
    abs_diff = abs(diff)
    if abs_diff > 18:
        stars, label = 1, "实力悬殊"
    elif abs_diff > 11:
        stars, label = 2, "优势明显"
    elif abs_diff > 6:
        stars, label = 3, "互有攻守"
    elif abs_diff > 3:
        stars, label = 4, "势均力敌"
    else:
        stars, label = 5, "胜负难料"

    # 推荐比分区间
    top_scores = [
        {"score": f"{round(exp_home)}-{round(exp_away)}", "probability": round(p_home * 100, 1)},
        {"score": f"{round(exp_home)-3}-{round(exp_away)+3}", "probability": 12.0},
        {"score": f"{round(exp_home)+3}-{round(exp_away)-3}", "probability": 12.0},
    ]

    return {
        "sport": "basketball",
        "home_team": {
            "name": home_team.get("name", ""),
            "code": home_team.get("name", "")[:3],
            "flag": "",
            "strength": round(home_elo, 1),
        },
        "away_team": {
            "name": away_team.get("name", ""),
            "code": away_team.get("name", "")[:3],
            "flag": "",
            "strength": round(away_elo, 1),
        },
        "probabilities": {
            "home_win": round(p_home * 100),
            "draw": 0,
            "away_win": round(p_away * 100),
        },
        # 融合前的主客独赢盘模型概率（历史评估的权威字段）
        "model_probabilities": {
            "home_win": round(model_home * 100),
            "draw": 0,
            "away_win": round(model_away * 100),
        },
        "market": {
            "odds": odds or {},
            "implied_probabilities": (
                {
                    "home_win": round(market_probs[0] * 100),
                    "draw": None,
                    "away_win": round(market_probs[2] * 100),
                }
                if market_probs
                else {"home_win": None, "draw": None, "away_win": None}
            ),
            "weight": weight if market_probs else 0,
        },
        "top_scores": top_scores,
        "expected_goals": {
            "home": round(exp_home, 1),
            "away": round(exp_away, 1),
        },
        "goals_prediction": {
            "most_likely": most_likely_total,
            "over_2_5": round(p_over, 3),
            "under_2_5": round(1 - p_over, 3),
        },
        "basketball": {
            "expected_home_points": round(exp_home, 1),
            "expected_away_points": round(exp_away, 1),
            "expected_total": round(exp_home + exp_away, 1),
            "spread": spread_line,
            "over_line": round(over_line, 1),
            "over_prob": round(p_over * 100, 1),
            "under_prob": round((1 - p_over) * 100, 1),
        },
        "suspense": {"stars": stars, "label": label},
        "ev_analysis": ev_analysis,
        "key_info": {
            "home_notes": [f"联赛基准总分 {base_total:.0f}"],
            "away_notes": [],
            "home_injuries": [],
            "away_injuries": [],
            "home_key_players": [],
            "away_key_players": [],
            "home_rest_days": 0,
            "away_rest_days": 0,
        },
        "radar": {
            "labels": ["进攻火力 (OFF)", "防守壁垒 (DEF)", "战绩 (W)", "稳定性 (STB)", "经验 (EXP)", "机构战力 (ELO)"],
            "home": _basketball_radar(home_team),
            "away": _basketball_radar(away_team),
        },
    }


def _basketball_radar(team: dict) -> list[int]:
    """篮球六维雷达"""
    elo = team.get("elo_rating", 1700)
    elo_score = max(0, min(100, (elo - 1400) / 5.0))
    played = max(1, team.get("matches_played", 0))
    win_rate = team.get("wins", 0) / played * 100
    off = min(100, (team.get("goals_for", 0) / played) * 2.2) if team.get("matches_played") else 55
    dfn = min(100, 100 - (team.get("goals_against", 0) / played) * 2.2) if team.get("matches_played") else 55
    return [
        round(off),
        round(max(20, dfn)),
        round(win_rate),
        round(min(100, 40 + win_rate * 0.5)),
        round(min(100, played * 2)),
        round(elo_score),
    ]
