"""
动态球队实力库
==============
解决"每日体彩球队池动态变化"问题。

策略（按优先级）：
1. 本地缓存 team_strength.json 中有记录 -> 直接使用
2. 联赛 Elo 基准 + 排名调整 -> 生成合理初始实力
3. 比赛结果反推（滚动更新 Elo）-> 越打越准

不依赖任何需要密钥的外部 API，完全自给自足。
"""
from __future__ import annotations

import json
import math
import os

import config
from utils.daily_loader import load_json, save_json

STRENGTH_FILE = "team_strength.json"

# 内存缓存
_CACHE: dict | None = None

# ---------------------------------------------------------------------------
# 联赛内强队锚点（用于生成实力梯度）
# 格式: 联赛 -> [(队名关键词, Elo), ...]，按 Elo 降序
# ---------------------------------------------------------------------------
LEAGUE_ANCHORS: dict[str, list[tuple[str, float]]] = {
    "英超": [("曼城", 1980), ("阿森纳", 1950), ("利物浦", 1940), ("维拉", 1880),
             ("热刺", 1860), ("切尔西", 1850), ("曼联", 1840), ("纽卡", 1830),
             ("布莱顿", 1800), ("西汉姆", 1780), ("水晶宫", 1760), ("富勒姆", 1750),
             ("布伦特福德", 1740), ("狼队", 1720), ("伯恩茅斯", 1720), ("埃弗顿", 1710),
             ("诺丁汉", 1700), ("卢顿", 1620), ("伯恩利", 1620), ("谢菲联", 1600)],
    "西甲": [("皇马", 1980), ("巴萨", 1940), ("马竞", 1900), ("赫罗纳", 1830),
             ("毕尔巴鄂", 1830), ("皇家社会", 1820), ("贝蒂斯", 1790), ("巴萨", 1940),
             ("瓦伦西亚", 1740), ("比利亚雷亚尔", 1760), ("塞维利亚", 1740)],
    "意甲": [("国米", 1950), ("AC米兰", 1900), ("尤文", 1890), ("那不勒斯", 1870),
             ("亚特兰大", 1860), ("罗马", 1840), ("拉齐奥", 1810), ("佛罗伦萨", 1790)],
    "德甲": [("拜仁", 1970), ("勒沃库森", 1930), ("多特", 1890), ("莱比锡", 1880),
             ("斯图加特", 1840), ("法兰克福", 1800)],
    "法甲": [("巴黎", 1960), ("摩纳哥", 1850), ("马赛", 1820), ("里尔", 1820),
             ("里昂", 1800), ("尼斯", 1790)],
    "荷甲": [("埃因霍温", 1860), ("费耶诺德", 1840), ("阿贾克斯", 1830)],
    "葡超": [("本菲卡", 1870), ("波尔图", 1860), ("里斯本", 1850), ("布拉加", 1800)],
    "中超": [("上海海港", 1720), ("上海申花", 1700), ("山东泰山", 1690),
             ("成都蓉城", 1680), ("北京国安", 1670), ("浙江", 1650),
             ("天津津门虎", 1640), ("武汉三镇", 1630), ("长春亚泰", 1600)],
    "日职": [("横滨水手", 1780), ("川崎前锋", 1760), ("神户胜利船", 1760),
             ("鹿岛鹿角", 1750), ("浦和红钻", 1740), ("广岛三箭", 1750)],
}


def _match_anchor(name: str, league: str) -> float | None:
    """在联赛锚点里按队名关键词匹配，返回锚定 Elo"""
    anchors = LEAGUE_ANCHORS.get(league)
    if not anchors:
        # 模糊匹配联赛名（如 '英超' 在 '英格兰超级联赛' 中）
        for lg, a in LEAGUE_ANCHORS.items():
            if lg in league or league in lg:
                anchors = a
                break
    if not anchors:
        return None
    for keyword, elo in anchors:
        if keyword in name or name in keyword:
            return elo
    return None


# ---------------------------------------------------------------------------
# 加载与保存
# ---------------------------------------------------------------------------

def _load() -> dict:
    global _CACHE
    if _CACHE is None:
        data = load_json(STRENGTH_FILE)
        _CACHE = data.get("teams", {})
    return _CACHE


def _save() -> None:
    if _CACHE is not None:
        save_json(STRENGTH_FILE, {"teams": _CACHE})


def reload() -> None:
    global _CACHE
    _CACHE = None


# ---------------------------------------------------------------------------
# Elo -> 攻防评分换算
# ---------------------------------------------------------------------------

def elo_to_attack(defense_elo: float) -> float:
    """把 Elo 映射到 0-1 的攻击力评分"""
    # 1600 -> 0.5, 每 400 分偏移 0.15
    val = 0.5 + (defense_elo - 1600.0) / 400.0 * 0.15
    return max(0.25, min(0.95, val))


def elo_to_defense(elo: float) -> float:
    """把 Elo 映射到 0-1 的防守力评分"""
    val = 0.5 + (elo - 1600.0) / 400.0 * 0.15
    return max(0.25, min(0.95, val))


# ---------------------------------------------------------------------------
# 实力档案生成
# ---------------------------------------------------------------------------

def _estimate_elo(league: str, rank: int | None, sport: str, name: str = "") -> float:
    """
    估算球队初始 Elo。
    优先级：
    1. 联赛锚点匹配（已知强队 -> 精确 Elo）
    2. 联赛基准 + 排名调整
    3. 联赛基准（含队名哈希微扰，避免同联赛球队分数完全相同）
    """
    # 1. 锚点匹配
    anchor = _match_anchor(name, league) if name else None
    if anchor is not None:
        return anchor

    table = dict(config.LEAGUE_STRENGTH)
    base = table.get(league, config.DEFAULT_LEAGUE_ELO)

    if sport == "basketball" and league not in table:
        base = 1700

    # 2. 排名调整
    if rank is not None and rank > 0:
        adjust = 80.0 - (rank - 1) * 5.5
        adjust = max(-120.0, min(100.0, adjust))
        base += adjust
    elif name:
        # 3. 无名次时用队名哈希产生 -60 ~ +60 的稳定微扰
        #    保证同联赛球队之间有区分度，且同一队每次都一致
        h = abs(hash(f"{league}|{name}")) % 121 - 60
        base += h

    return base


def get_team_profile(
    name: str,
    league: str = "",
    sport: str = "football",
    rank: int | None = None,
) -> dict:
    """
    获取球队实力档案。未知球队自动生成并缓存。
    """
    if not name:
        name = "未知球队"

    teams = _load()
    key = f"{sport}|{name}"

    if key in teams:
        profile = dict(teams[key])
        # 联赛可能已知，补全
        if league and not profile.get("league"):
            profile["league"] = league
        return profile

    # 新球队 -> 按联赛基准估算
    elo = _estimate_elo(league, rank, sport, name)
    profile = {
        "name": name,
        "league": league,
        "sport": sport,
        "elo_rating": round(elo, 1),
        "attack_rating": round(elo_to_attack(elo), 3),
        "defense_rating": round(elo_to_defense(elo), 3),
        "matches_played": 0,
        "wins": 0,
        "draws": 0,
        "losses": 0,
        "goals_for": 0,
        "goals_against": 0,
        "estimated": True,  # 标记为估算值
    }
    teams[key] = profile
    _save()
    return dict(profile)


# ---------------------------------------------------------------------------
# Elo 滚动更新（用真实赛果校准）
# ---------------------------------------------------------------------------

def expected_result(elo_a: float, elo_b: float) -> float:
    """Elo 预期胜率"""
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / config.MODEL_CONFIG["elo_scale"]))


def update_from_result(
    home_name: str,
    away_name: str,
    home_goals: int,
    away_goals: int,
    league: str = "",
    sport: str = "football",
    k: float = 20.0,
) -> None:
    """
    用一场真实赛果更新双方 Elo（含进球数修正）。
    """
    home = get_team_profile(home_name, league, sport)
    away = get_team_profile(away_name, league, sport)

    elo_h = home["elo_rating"]
    elo_a = away["elo_rating"]

    # 主场优势
    ha = config.MODEL_CONFIG["home_advantage_elo"]
    exp_h = expected_result(elo_h + ha, elo_a)

    if home_goals > away_goals:
        actual_h = 1.0
    elif home_goals == away_goals:
        actual_h = 0.5
    else:
        actual_h = 0.0

    # 进球差放大系数（大胜提升更多）
    goal_diff = abs(home_goals - away_goals)
    multiplier = 1.0 + math.log1p(goal_diff) * 0.4

    delta = k * multiplier * (actual_h - exp_h)

    teams = _load()
    hk = f"{sport}|{home_name}"
    ak = f"{sport}|{away_name}"

    teams[hk]["elo_rating"] = round(elo_h + delta, 1)
    teams[ak]["elo_rating"] = round(elo_a - delta, 1)

    # 更新攻防评分
    for kk, elo in ((hk, teams[hk]["elo_rating"]), (ak, teams[ak]["elo_rating"])):
        teams[kk]["attack_rating"] = round(elo_to_attack(elo), 3)
        teams[kk]["defense_rating"] = round(elo_to_defense(elo), 3)

    # 更新战绩
    for kk, gf, ga in ((hk, home_goals, away_goals), (ak, away_goals, home_goals)):
        teams[kk]["matches_played"] = teams[kk].get("matches_played", 0) + 1
        teams[kk]["goals_for"] = teams[kk].get("goals_for", 0) + gf
        teams[kk]["goals_against"] = teams[kk].get("goals_against", 0) + ga
        if gf > ga:
            teams[kk]["wins"] = teams[kk].get("wins", 0) + 1
        elif gf == ga:
            teams[kk]["draws"] = teams[kk].get("draws", 0) + 1
        else:
            teams[kk]["losses"] = teams[kk].get("losses", 0) + 1
        teams[kk]["estimated"] = False

    _save()


def get_league_baseline(league: str) -> float:
    """返回联赛 Elo 基准，供模型判断强弱"""
    return config.LEAGUE_STRENGTH.get(league, config.DEFAULT_LEAGUE_ELO)


def stats() -> dict:
    """球队库统计"""
    teams = _load()
    estimated = sum(1 for t in teams.values() if t.get("estimated"))
    return {
        "total": len(teams),
        "estimated": estimated,
        "calibrated": len(teams) - estimated,
    }
