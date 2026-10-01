"""
比赛跨源身份与对账辅助模块
==========================
负责：
1. 竞彩赛事编号归一化与提取
2. 权威开赛时间已知性判定
3. 赛事强编号键与主客队对 fallback 键构造
4. 跨源赛事对账判等（same_event）
"""
from __future__ import annotations

from typing import Any


def normalize_competition_number(value: Any) -> str | None:
    """
    归一化竞彩赛事编号：
    - '周三007' -> '7'
    - '007'     -> '7'
    - 7         -> '7'
    - 0         -> '0'
    - 空或无数字 -> None
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    stripped = digits.lstrip("0")
    return stripped if stripped else "0"


def competition_number_for_match(match: dict) -> str | None:
    """
    提取比赛的竞彩赛事编号。
    优先级：
    1. match['jczq_no']
    2. match['odds']['matchnum']
    3. 历史兼容：当记录属于 market XML fallback（如 id 以 '500j-' 开头）时的 match['round']
    """
    if not isinstance(match, dict):
        return None

    no = match.get("jczq_no")
    if no:
        norm = normalize_competition_number(no)
        if norm is not None:
            return norm

    odds = match.get("odds")
    if isinstance(odds, dict):
        matchnum = odds.get("matchnum")
        if matchnum:
            norm = normalize_competition_number(matchnum)
            if norm is not None:
                return norm

    mid = str(match.get("id") or "")
    if mid.startswith("500j-"):
        rnd = match.get("round")
        if rnd:
            norm = normalize_competition_number(rnd)
            if norm is not None:
                return norm

    return None


def is_kickoff_time_known(match: dict) -> bool:
    """
    判定该比赛记录的开赛时间是否权威已知。

    - 显式包含 kickoff_time_known 字段时，以其布尔值为准；
    - 未显式包含时，对同时满足：
      1. market-fallback 结构（id 以 '500j-' 开头）
      2. 存在赛事编号
      3. time == '00:00'
      的历史记录，判定为 False；
    - 其余情况视为 True。
    """
    if not isinstance(match, dict):
        return False

    if "kickoff_time_known" in match:
        return bool(match["kickoff_time_known"])

    mid = str(match.get("id") or "")
    if (
        mid.startswith("500j-")
        and match.get("time") == "00:00"
        and competition_number_for_match(match) is not None
    ):
        return False

    return True


def competition_event_key(match: dict) -> str | None:
    """
    构造强赛事编号键：sport|date|jczq|cno。
    开赛时间不参与。
    """
    if not isinstance(match, dict):
        return None
    sport = match.get("sport")
    date = match.get("date")
    cno = competition_number_for_match(match)
    if not sport or not date or cno is None:
        return None
    return f"{sport}|{date}|jczq|{cno}"


def team_event_key(match: dict) -> str | None:
    """
    构造主客队对 fallback 键：sport|date|teams|home|away。
    开赛时间不参与，主客队均不可为空。
    """
    if not isinstance(match, dict):
        return None
    sport = match.get("sport")
    date = match.get("date")
    home = match.get("home")
    away = match.get("away")
    if not sport or not date or not home or not away:
        return None
    return f"{sport}|{date}|teams|{home}|{away}"


def same_event(match_a: dict, match_b: dict) -> bool:
    """
    跨数据源判定两条记录是否代表同一场真实赛事：
    1. sport 必须完全一致；
    2. date 必须完全一致；
    3. 若两方均拥有强赛事编号：当且仅当强赛事编号一致时为同一赛事；
       （双方均有编号但不一致时，绝不退化为主客队匹配）
    4. 若一方或双方缺少强赛事编号：退化为精确的主客队匹配 (sport, date, home, away)。
    开赛时间不参与匹配。
    """
    if not isinstance(match_a, dict) or not isinstance(match_b, dict):
        return False

    if match_a.get("sport") != match_b.get("sport"):
        return False

    if match_a.get("date") != match_b.get("date"):
        return False

    cno_a = competition_number_for_match(match_a)
    cno_b = competition_number_for_match(match_b)

    if cno_a is not None and cno_b is not None:
        return cno_a == cno_b

    key_a = team_event_key(match_a)
    key_b = team_event_key(match_b)
    if key_a is None or key_b is None:
        return False
    return key_a == key_b
