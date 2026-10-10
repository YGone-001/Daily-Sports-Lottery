"""
每日赛事统一数据层
==================
职责：
1. 读取/缓存 data/daily_matches.json（由抓取器写入）
2. 提供按日期、状态、赛事类别的查询
3. 逻辑比赛日处理（北京时间 6 点切割）
4. 为每场比赛补充时间状态、赔率、球队实力

替代原 utils/data_loader.py 的世界杯专用逻辑。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import config
from utils.match_identity import is_kickoff_time_known

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
DAILY_FILE = "daily_matches.json"


# ---------------------------------------------------------------------------
# 基础读写
# ---------------------------------------------------------------------------

def _path(filename: str) -> str:
    return os.path.join(config.DATA_DIR, filename)


def load_json(filename: str) -> dict:
    filepath = _path(filename)
    if not os.path.exists(filepath):
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(filename: str, data: dict) -> None:
    from utils.atomic_json import atomic_write_json
    filepath = _path(filename)
    atomic_write_json(filepath, data)


def get_beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


# ---------------------------------------------------------------------------
# 比赛日逻辑
# ---------------------------------------------------------------------------

def get_logical_matchday(now: datetime | None = None) -> str:
    """返回逻辑比赛日 (YYYY-MM-DD)。凌晨 cutoff 前归前一天。"""
    now = (now or get_beijing_now()).astimezone(BEIJING_TZ)
    if now.hour < config.MATCHDAY_CUTOFF_HOUR:
        return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.date().strftime("%Y-%m-%d")


def get_match_datetime(match: dict) -> datetime | None:
    """解析比赛开赛时间（北京时间）。开赛时间未知时返回 None。"""
    if not is_kickoff_time_known(match):
        return None
    try:
        date_str = match.get("date", "")
        time_str = match.get("time", "00:00") or "00:00"
        if not date_str:
            return None
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=BEIJING_TZ)
    except (ValueError, TypeError):
        return None


def add_time_status(match: dict, now: datetime | None = None) -> dict:
    """为比赛补充实时状态 (upcoming/live/finished)"""
    now = (now or get_beijing_now()).astimezone(BEIJING_TZ)
    m = dict(match)

    # 抓取源已明确标记完赛的，直接沿用
    if match.get("status") == "finished" and match.get("score"):
        m["status"] = "finished"
        return m

    start = get_match_datetime(m)
    if not start:
        m["status"] = match.get("status", "upcoming")
        return m

    duration = 135 if m.get("sport") == "basketball" else 120
    end = start + timedelta(minutes=duration)

    if now < start:
        m["status"] = "upcoming"
    elif now <= end:
        m["status"] = "live"
    else:
        m["status"] = "finished"
    return m


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------

def get_all_matches() -> list[dict]:
    """返回全部已抓取赛事"""
    data = load_json(DAILY_FILE)
    return data.get("matches", [])


def get_matches_by_date(date: str | None = None, sport: str | None = None) -> list[dict]:
    """
    按日期与类别查询。
    date=None -> 逻辑今天；date='all' -> 全部
    """
    matches = get_all_matches()
    if date != "all":
        target = date or get_logical_matchday()
        matches = [m for m in matches if m.get("date") == target]
    if sport:
        matches = [m for m in matches if m.get("sport") == sport]
    return matches


def get_match_by_id(match_id: str) -> dict:
    for m in get_all_matches():
        if m.get("id") == match_id:
            return m
    return {}


def get_available_dates() -> list[str]:
    """返回有赛事的日期列表（降序）"""
    dates = {m.get("date") for m in get_all_matches() if m.get("date")}
    return sorted(dates, reverse=True)


def get_available_dates_with_upcoming() -> set[str]:
    """
    返回「至少含一场未完赛比赛」的日期集合。
    命中该集合的日期才是可预测（有投注价值）的比赛日。
    """
    out: set[str] = set()
    for m in get_all_matches():
        if m.get("status") != "finished" and m.get("date"):
            out.add(m["date"])
    return out


def get_default_date() -> str:
    """
    首页默认日期选择（面向「可投注」场景，而非「看赛果」）：
    1. 优先最近一个「仍可下注」的比赛日 —— 即含未完赛且开赛时间未过的赛事
    2. 其次逻辑今天
    3. 再其次最近的可投注日
    4. 兜底取最新日期
    """
    dates = get_available_dates()          # 降序
    if not dates:
        return get_logical_matchday()

    today = get_logical_matchday()
    now = get_beijing_now()

    # 1. 真正可投注的比赛日（有未完赛且尚未开赛的比赛）
    actionable_days: set[str] = set()
    for m in get_all_matches():
        if m.get("status") == "finished":
            continue
        start = get_match_datetime(m)
        if start and now < start:
            actionable_days.add(m.get("date") or "")

    if actionable_days:
        # 取离今天最近的可投注日（优先今天及以后）
        def _dist(d: str) -> tuple[int, int]:
            try:
                delta = (
                    datetime.strptime(d, "%Y-%m-%d").date()
                    - datetime.strptime(today, "%Y-%m-%d").date()
                ).days
            except ValueError:
                return (1, 9999)
            # 今天/未来排前面，且越近越优先
            return (0 if delta >= 0 else 1, abs(delta))

        return sorted(actionable_days, key=_dist)[0]

    # 2. 逻辑今天有数据就用今天
    if today in dates:
        return today

    # 3. 有未完赛赛事的日期
    partial = get_available_dates_with_upcoming()
    if partial:
        return sorted(partial, key=_dist)[0] if partial else dates[0]

    return dates[0]


def get_leagues(sport: str | None = None) -> list[str]:
    """返回赛事涉及的联赛列表"""
    leagues = set()
    for m in get_all_matches():
        if sport and m.get("sport") != sport:
            continue
        if m.get("league"):
            leagues.add(m["league"])
    return sorted(leagues)


def get_meta() -> dict:
    """返回抓取元信息"""
    data = load_json(DAILY_FILE)
    return data.get("meta", {})


# ---------------------------------------------------------------------------
# 富化（补充球队信息与预测）
# ---------------------------------------------------------------------------

def enrich_match(match: dict, now: datetime | None = None) -> dict:
    """
    为比赛补充：
    - 时间状态
    - 主客队实力数据（自动匹配 Elo）
    - 模型预测结果
    """
    from utils.team_strength import get_team_profile

    m = add_time_status(dict(match), now)

    home_name = m.get("home", "")
    away_name = m.get("away", "")
    league = m.get("league", "")
    sport = m.get("sport", "football")

    m["home_team"] = get_team_profile(home_name, league, sport, rank=m.get("home_rank"))
    m["away_team"] = get_team_profile(away_name, league, sport, rank=m.get("away_rank"))

    return m


def get_matches_grouped(date: str | None = None, sport: str | None = None) -> list[dict]:
    """
    返回按联赛分组的比赛（含预测），供列表页使用。
    结构: [{league, sport, matches: [...]}, ...]
    """
    from models.predictor import predict_match

    matches = get_matches_by_date(date, sport)
    now = get_beijing_now()

    groups: dict[str, dict] = {}
    for raw in matches:
        m = enrich_match(raw, now)

        # 附加预测（待开赛或进行中才需要）
        if m.get("status") != "finished":
            try:
                m["prediction"] = predict_match(
                    m["home_team"],
                    m["away_team"],
                    odds=m.get("odds"),
                    sport=m.get("sport", "football"),
                    league=m.get("league", ""),
                )
            except Exception:  # noqa: BLE001
                m["prediction"] = None

        key = f"{m.get('sport')}|{m.get('league')}"
        if key not in groups:
            groups[key] = {
                "league": m.get("league", "其他"),
                "sport": m.get("sport", "football"),
                "matches": [],
            }
        groups[key]["matches"].append(m)

    # 组内按时间排序
    for g in groups.values():
        g["matches"].sort(key=lambda x: (x.get("date", ""), x.get("time", "00:00")))

    # 组间按最早开赛时间排序
    result = sorted(
        groups.values(),
        key=lambda g: min(
            (m.get("date", "") + m.get("time", "00:00")) for m in g["matches"]
        ),
    )
    return result


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def get_daily_stats(date: str | None = None, sport: str | None = None) -> dict:
    """当日赛事概览统计"""
    matches = [add_time_status(m) for m in get_matches_by_date(date, sport)]
    total = len(matches)
    finished = [m for m in matches if m["status"] == "finished"]
    live = [m for m in matches if m["status"] == "live"]
    upcoming = [m for m in matches if m["status"] == "upcoming"]
    with_odds = [m for m in matches if m.get("odds")]

    return {
        "total": total,
        "finished": len(finished),
        "live": len(live),
        "upcoming": len(upcoming),
        "with_odds": len(with_odds),
    }
