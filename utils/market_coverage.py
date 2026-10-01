"""
市场覆盖判定
============
应用跟踪的是**有真实盘口覆盖**的比赛，而不是全球记分板上的每一场。

本模块提供唯一的「是否有可用盘口」判定，供抓取器准入、测试与后续扩展复用，
避免在 scraper / app / tests 里各写一份不同定义。

准入依据是**市场证据**，不是联赛或球队名气：
小联赛只要真有盘口就是相关赛事；知名联赛若无可用盘口，则尚未成为被跟踪的市场事件。
本模块**不**维护任何联赛白名单 / 黑名单 / 球队热度规则。

价格校验
--------
价格必须同时满足：数值型（bool 不算）、有限（非 NaN / Infinity）、且 **> 1.0**。
仅有编号类字段（`matchnum` / `jczq_no`）或仅有盘口线（`total_line` / `handicap_line`）
都不构成覆盖；空字典、None 填充同样不构成。
"""
from __future__ import annotations

import math

# 赔率价格必须严格大于 1.0（1.0 意味着零收益，不是有效价格）
MIN_VALID_PRICE = 1.0

# 支持的完整市场定义（每个市场要求全部字段都是有效价格）
FOOTBALL_1X2_KEYS = ("home_win", "draw", "away_win")
FOOTBALL_TOTAL_KEYS = ("total_line", "over", "under")
FOOTBALL_TOTAL_ALT_KEYS = ("over_2_5", "under_2_5")
BASKETBALL_MONEYLINE_KEYS = ("home_win", "away_win")
BASKETBALL_TOTAL_KEYS = ("total_line", "over", "under")


def is_valid_price(value: object) -> bool:
    """有效赔率价格：数值型、有限、非 bool、且严格大于 1.0。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(float(value)) and float(value) > MIN_VALID_PRICE


def is_finite_number(value: object) -> bool:
    """有限数值（用于盘口线，不要求 > 1.0）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(float(value))


def _all_prices(odds: dict, keys: tuple[str, ...]) -> bool:
    return all(is_valid_price(odds.get(key)) for key in keys)


def _total_market(odds: dict, keys: tuple[str, ...]) -> bool:
    """完整大小盘：盘口线为有限数值，over / under 为有效价格。"""
    line_key, over_key, under_key = keys
    return (
        is_finite_number(odds.get(line_key))
        and is_valid_price(odds.get(over_key))
        and is_valid_price(odds.get(under_key))
    )


def football_has_one_x_two(odds: dict) -> bool:
    """足球胜平负：三路齐全才算完整。"""
    return _all_prices(odds, FOOTBALL_1X2_KEYS)


def football_has_total(odds: dict) -> bool:
    """
    足球大小球：接受 `total_line + over + under`，
    也兼容仓库既有的 `over_2_5 + under_2_5` 命名。
    不虚构盘口价格。
    """
    if _total_market(odds, FOOTBALL_TOTAL_KEYS):
        return True
    return _all_prices(odds, FOOTBALL_TOTAL_ALT_KEYS)


def basketball_has_moneyline(odds: dict) -> bool:
    """篮球胜负：主客两路齐全。"""
    return _all_prices(odds, BASKETBALL_MONEYLINE_KEYS)


def basketball_has_total(odds: dict) -> bool:
    """篮球大小分：盘口线 + over/under 价格齐全。仅有盘口线不算。"""
    return _total_market(odds, BASKETBALL_TOTAL_KEYS)


def has_usable_market_odds(match: dict) -> bool:
    """
    canonical 准入判定：该场比赛是否具备至少一个**完整**的支持盘口市场。

    足球：完整 1X2 或完整大小球。
    篮球：完整胜负 或 完整大小分。
    其他运动：不判定为覆盖。
    """
    odds = match.get("odds")
    if not isinstance(odds, dict) or not odds:
        return False

    sport = match.get("sport", "football")
    if sport == "basketball":
        return basketball_has_moneyline(odds) or basketball_has_total(odds)
    if sport == "football":
        return football_has_one_x_two(odds) or football_has_total(odds)
    return False
