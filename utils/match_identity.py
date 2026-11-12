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

from datetime import datetime
import re
from typing import Any

SOURCE_KICKOFF_TIME_PRESENT = "SOURCE_KICKOFF_TIME_PRESENT"
SOURCE_KICKOFF_TIME_MISSING = "SOURCE_KICKOFF_TIME_MISSING"
SOURCE_KICKOFF_TIME_MALFORMED = "SOURCE_KICKOFF_TIME_MALFORMED"
SOURCE_KICKOFF_TIME_AMBIGUOUS = "SOURCE_KICKOFF_TIME_AMBIGUOUS"

_VALID_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_VALID_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK_FRAGMENT_RE = re.compile(r"\d{1,2}:\d{2}")
_MISSING_TIME_TOKENS = frozenset({
    "",
    "-",
    "--",
    "--:--",
    "tbd",
    "tba",
    "pending",
    "unknown",
    "null",
    "none",
    "n/a",
    "待定",
    "未知",
    "未定",
})


def is_valid_calendar_date(value: Any) -> bool:
    """校验 YYYY-MM-DD 格式与真实日历日期（拒绝 2026-02-30 等非法日期）。"""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not _VALID_DATE_RE.match(s):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def classify_kickoff_time_value(raw_time: Any) -> tuple[str, str, bool]:
    """
    分类源记录的原始开赛时间文本，避免缺省或非法字符串被当作真实 00:00。

    返回 ``(classification, normalized_hhmm, kickoff_time_known)``：
    - 缺失/占位（""、"--:--"、"待定" 等） -> ``SOURCE_KICKOFF_TIME_MISSING``, "00:00", False
    - 模糊/多值（含 ?、/、~、约、多个时钟片段） -> ``SOURCE_KICKOFF_TIME_AMBIGUOUS``, "00:00", False
    - 合法 24 小时 HH:MM（含显式 "00:00"） -> ``SOURCE_KICKOFF_TIME_PRESENT``, HH:MM, True
    - 其余非法格式（如 "25:99"、"8:30"） -> ``SOURCE_KICKOFF_TIME_MALFORMED``, "00:00", False
    """
    if raw_time is None or not isinstance(raw_time, str):
        return SOURCE_KICKOFF_TIME_MISSING, "00:00", False

    s = raw_time.strip()
    if s.lower() in _MISSING_TIME_TOKENS:
        return SOURCE_KICKOFF_TIME_MISSING, "00:00", False

    lower = s.lower()
    if (
        any(tok in s for tok in ("?", "？", "/", "~", "或", "约"))
        or len(_CLOCK_FRAGMENT_RE.findall(s)) > 1
        or "approx" in lower
    ):
        return SOURCE_KICKOFF_TIME_AMBIGUOUS, "00:00", False

    if _VALID_CLOCK_RE.match(s):
        return SOURCE_KICKOFF_TIME_PRESENT, s, True

    return SOURCE_KICKOFF_TIME_MALFORMED, "00:00", False


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

    - 若 kickoff_time_known 显式为 False，直接返回 False；
    - 历史兼容：500j- 且存在编号且 time == '00:00' 时，判定为 False；
    - 即便标志位为 True 或缺失，仍需校验 date（若存在）为合法日历日期且
      time 为合法 24 小时 HH:MM（缺省、空串、模糊值或越界时钟一律 fail-closed）。
    """
    if not isinstance(match, dict):
        return False

    if "kickoff_time_known" in match and not bool(match["kickoff_time_known"]):
        return False

    if "kickoff_time_known" not in match:
        mid = str(match.get("id") or "")
        if (
            mid.startswith("500j-")
            and match.get("time") == "00:00"
            and competition_number_for_match(match) is not None
        ):
            return False

    if "date" in match and not is_valid_calendar_date(match.get("date")):
        return False

    classification, _, known = classify_kickoff_time_value(match.get("time"))
    return classification == SOURCE_KICKOFF_TIME_PRESENT and known


def build_kickoff_time_provenance(match: dict, raw_value: str | None = None) -> dict:
    """构造 B.3 规定的只读开赛时间溯源诊断字段。"""
    if not isinstance(match, dict):
        return {
            "kickoff_time_source": "unknown",
            "kickoff_time_raw_value": None,
            "kickoff_time_known": False,
            "kickoff_time_timezone": "Asia/Shanghai",
            "kickoff_time_classification": SOURCE_KICKOFF_TIME_MISSING,
        }

    mid = str(match.get("id") or "")
    source = mid.split("-", 1)[0] if "-" in mid else (mid or "unknown")
    raw_val = raw_value if raw_value is not None else match.get("kickoff_time_raw_value", match.get("time"))
    known = is_kickoff_time_known(match)
    classification, _, _ = classify_kickoff_time_value(raw_val)

    if not known and classification == SOURCE_KICKOFF_TIME_PRESENT:
        if "date" in match and not is_valid_calendar_date(match.get("date")):
            classification = SOURCE_KICKOFF_TIME_MALFORMED
        else:
            classification = SOURCE_KICKOFF_TIME_MISSING

    return {
        "kickoff_time_source": source,
        "kickoff_time_raw_value": raw_val,
        "kickoff_time_known": known,
        "kickoff_time_timezone": "Asia/Shanghai",
        "kickoff_time_classification": classification,
    }


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
