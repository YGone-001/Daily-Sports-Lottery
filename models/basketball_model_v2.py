"""实验篮球模型 V2（``basketball-margin-2-shadow``）——严格隔离的影子候选。

消费 ``utils.nba_team_strength`` 的真实历史实力特征，**不**替换生产模型
``basketball-margin-1``，**不**改写历史快照/结算，**不**写入生产球队库或 Canonical 快照存储，
**不**改动 ``same_event()``。

概率三层语义与生产模型一致：
    raw_model_probabilities   模型自身判断（融合前，EV/评估权威来源）
    display_probabilities     与市场隐含概率融合后的展示值
    market_implied_probabilities  去水后的市场隐含概率

实力不足时显式返回 ``INSUFFICIENT_REAL_TEAM_DATA``，绝不回退到伪造的改进。
参数为 documented 固定先验，**不**从原始八场队列拟合；任何未来拟合必须用按时间切分的
历史训练区间，禁止对同日比赛做随机 train/test 泄漏。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import config
from utils.nba_schedule import BEIJING_TZ
from utils.nba_team_strength import (
    COVERAGE_NO_GAMES,
    NBA_STRENGTH_PARAMS,
    _parse_utc,
)

MODEL_NAME = "basketball-margin-2-shadow"
MODEL_VERSION = "basketball-margin-2-shadow"

STATUS_OK = "OK"
STATUS_INSUFFICIENT = "INSUFFICIENT_REAL_TEAM_DATA"

# documented 固定先验（人工维护，非八场队列拟合）。
V2_PARAMS: dict[str, float] = {
    "prior_matches": 10.0,        # 样本量收缩的等效先验场次
    "form_weight": 0.15,          # 近 10 场场均净胜分差的权重
    "rest_bonus_points": 1.0,     # 休息天数优势（每多休息 1 天，封顶 3 天）
    "rest_cap_days": 3.0,
    "b2b_penalty_points": 1.5,    # 背靠背作战罚分
}


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def _market_implied(odds: dict | None) -> tuple[float, float] | None:
    if not odds:
        return None
    try:
        ho, ao = float(odds["home_win"]), float(odds["away_win"])
    except (KeyError, TypeError, ValueError):
        return None
    if ho <= 1 or ao <= 1:
        return None
    ih, ia = 1.0 / ho, 1.0 / ao
    tot = ih + ia
    return ih / tot, ia / tot


def _shrunk_elo(features: dict, prior: float, prior_matches: float) -> float:
    elo = float(features.get("historical_elo", prior))
    n = int(features.get("games_used_before_cutoff", 0) or 0)
    weight = n / (n + prior_matches) if (n + prior_matches) > 0 else 0.0
    return prior + (elo - prior) * weight


def _rest_days(features: dict, fixture_kickoff: datetime | None) -> float | None:
    if fixture_kickoff is None:
        return None
    last = _parse_utc(features.get("last_game_at"))
    if last is None:
        return None
    return max(0.0, (fixture_kickoff - last).total_seconds() / 86400.0)


def predict_basketball_v2(
    home_features: dict,
    away_features: dict,
    *,
    odds: dict | None = None,
    fixture_kickoff: datetime | None = None,
    params: dict | None = None,
) -> dict:
    """由真实实力特征计算 V2 影子胜负概率（纯函数，只读，确定性）。

    任一队 ``games_used_before_cutoff == 0`` -> ``INSUFFICIENT_REAL_TEAM_DATA``，
    不产生伪造概率。小样本（1..4 场）仍出概率但带 ``small_sample`` 不确定标记。
    """
    p = dict(V2_PARAMS)
    if params:
        p.update(params)
    cfg = config.MODEL_CONFIG
    prior = float(NBA_STRENGTH_PARAMS["prior_elo"])

    home_n = int(home_features.get("games_used_before_cutoff", 0) or 0)
    away_n = int(away_features.get("games_used_before_cutoff", 0) or 0)
    base = {
        "model_name": MODEL_NAME,
        "model_version": MODEL_VERSION,
        "sport": "basketball",
        "home_team": home_features.get("team_name", ""),
        "away_team": away_features.get("team_name", ""),
        "home_code": home_features.get("team_id", ""),
        "away_code": away_features.get("team_id", ""),
        "production_model_unchanged": True,
        "writes_production_stores": False,
    }
    if home_n == 0 or away_n == 0:
        return dict(base, status=STATUS_INSUFFICIENT,
                    reason=STATUS_INSUFFICIENT,
                    home_games_used=home_n, away_games_used=away_n,
                    raw_model_probabilities=None, display_probabilities=None,
                    market_implied_probabilities=None)

    eff_home = _shrunk_elo(home_features, prior, p["prior_matches"])
    eff_away = _shrunk_elo(away_features, prior, p["prior_matches"])

    # Elo 差 -> 预期分差（沿用 documented basketball_elo_scale：200 Elo ≈ 10 分）。
    elo_margin = (eff_home - eff_away) / cfg["basketball_elo_scale"] * 10.0
    home_court = cfg["basketball_home_advantage"]

    # 近期状态：近 10 场场均净胜分差（小样本时该字段可能为 None，按 0 处理）。
    h_form = home_features.get("recent_10_point_differential")
    a_form = away_features.get("recent_10_point_differential")
    form_term = p["form_weight"] * ((h_form or 0.0) - (a_form or 0.0))

    # 休息 / 背靠背（相对本场开赛时刻）。
    rest_term = 0.0
    h_rest = _rest_days(home_features, fixture_kickoff)
    a_rest = _rest_days(away_features, fixture_kickoff)
    if h_rest is not None and a_rest is not None:
        rest_diff = min(p["rest_cap_days"], max(-p["rest_cap_days"], h_rest - a_rest))
        rest_term += p["rest_bonus_points"] * rest_diff
        h_b2b = h_rest <= 1.0
        a_b2b = a_rest <= 1.0
        if a_b2b and not h_b2b:
            rest_term += p["b2b_penalty_points"]
        elif h_b2b and not a_b2b:
            rest_term -= p["b2b_penalty_points"]

    margin = elo_margin + home_court + form_term + rest_term
    diff_sigma = cfg["basketball_score_std"] * math.sqrt(2.0)
    p_home = 1.0 - _norm_cdf(0.0, margin, diff_sigma)
    p_home = max(0.0, min(1.0, p_home))
    p_away = 1.0 - p_home

    raw = {"home_win": p_home, "draw": 0.0, "away_win": p_away}
    market = _market_implied(odds)
    market_probs = ({"home_win": market[0], "draw": None, "away_win": market[1]}
                    if market else {"home_win": None, "draw": None, "away_win": None})

    weight = cfg["odds_weight"]
    if market and weight > 0:
        dh = p_home * (1 - weight) + market[0] * weight
        da = p_away * (1 - weight) + market[1] * weight
        norm = dh + da
        if norm > 0:
            dh, da = dh / norm, da / norm
    else:
        dh, da = p_home, p_away
    display = {"home_win": dh, "draw": 0.0, "away_win": da}

    small_sample = home_n < int(NBA_STRENGTH_PARAMS["min_games_stable"]) or \
        away_n < int(NBA_STRENGTH_PARAMS["min_games_stable"])
    return dict(
        base,
        status=STATUS_OK,
        home_games_used=home_n,
        away_games_used=away_n,
        home_historical_elo=home_features.get("historical_elo"),
        away_historical_elo=away_features.get("historical_elo"),
        home_effective_elo=round(eff_home, 3),
        away_effective_elo=round(eff_away, 3),
        expected_margin=round(margin, 3),
        margin_components={"elo": round(elo_margin, 3), "home_court": home_court,
                           "form": round(form_term, 3), "rest": round(rest_term, 3)},
        small_sample=small_sample,
        data_coverage={"home": home_features.get("data_coverage"),
                       "away": away_features.get("data_coverage")},
        raw_model_probabilities=raw,
        display_probabilities=display,
        market_implied_probabilities=market_probs,
        market_blend_weight=weight if market else 0.0,
    )


def predict_from_inventory(
    inventory: dict,
    home_code: str,
    away_code: str,
    *,
    odds: dict | None = None,
    fixture_kickoff: datetime | None = None,
    params: dict | None = None,
) -> dict:
    """从实力清单按球队编码取特征并预测；编码缺失 -> INSUFFICIENT_REAL_TEAM_DATA。"""
    teams = inventory.get("teams", {}) if isinstance(inventory, dict) else {}
    hf, af = teams.get(home_code), teams.get(away_code)
    if not isinstance(hf, dict) or not isinstance(af, dict):
        return {"model_name": MODEL_NAME, "model_version": MODEL_VERSION,
                "status": STATUS_INSUFFICIENT, "reason": "TEAM_NOT_IN_INVENTORY",
                "home_code": home_code, "away_code": away_code,
                "raw_model_probabilities": None, "display_probabilities": None,
                "market_implied_probabilities": None, "production_model_unchanged": True,
                "writes_production_stores": False}
    return predict_basketball_v2(hf, af, odds=odds, fixture_kickoff=fixture_kickoff, params=params)