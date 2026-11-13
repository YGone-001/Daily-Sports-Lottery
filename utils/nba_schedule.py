"""
NBA 赛程时间补全适配器与严格身份绑定
======================================
职责边界：
1. 默认使用文档化 BALLDONTLIE NBA Games API（需要凭据与独立网络许可），
   同时兼容离线 ESPN / NBA 官方 `scheduleLeagueV2` 载荷解析；无隐藏网络回退。
   注意：不绕过 `cdn.nba.com` / `data.nba.com` 的 HTTP 403 访问限制。
2. 仅作「开赛时间（time-only）补全」：
   - 不得作为新市场候选来源（不向 Canonical 新增赛事）；
   - 不得改动 `same_event()` / `jczq_no` / Canonical ID / 队名 / 主客 / 赔率 / 比分 / 结算；
   - 严格通过显式 30 支 NBA 球队编码表完成双侧一一绑定，零模糊匹配、零自动换主客；
   - 失败即 fail-closed 返回明确的拒绝原因码。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import re
import time
import threading
import math
from email.utils import parsedate_to_datetime
from typing import Any, Iterable

import requests

import config
from utils.fetcher_500 import is_nba_league
from utils.match_identity import is_kickoff_time_known, is_valid_calendar_date

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

PROVIDER_ESPN_NBA_SCOREBOARD = "espn_nba_scoreboard"
PROVIDER_NBA_OFFICIAL_SCHEDULE = "nba_official_schedule"
PROVIDER_BALLDONTLIE_NBA_GAMES = "balldontlie_nba_games"

# 数据源分类与接入合规契约 (Workstream D)
PROVIDER_TYPE_NBA_OFFICIAL = "NBA_OFFICIAL_DIRECT_SOURCE"
PROVIDER_TYPE_ESPN_THIRD_PARTY = "ESPN_THIRD_PARTY_SCHEDULE_SOURCE"
PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY = "DOCUMENTED_THIRD_PARTY_NBA_SCHEDULE_API"
PROVIDER_TYPE_DOCUMENTED_AUTHORIZED = "DOCUMENTED_AUTHORIZED_SOURCE"
PROVIDER_TYPE_UNSUPPORTED_PUBLIC = "UNSUPPORTED_PUBLIC_ENDPOINT"
PROVIDER_AUTH_UNVERIFIED = "PROVIDER_USAGE_AUTHORIZATION_UNVERIFIED"

AUTOMATED_OPERATION_ENABLED = "ENABLED_WITH_AUTHORIZATION"
AUTOMATED_OPERATION_DISABLED = "DISABLED_PENDING_AUTHORIZATION"

# NBA 第三方公开赛程网络抓取控制（自动化生产授权未确立前默认禁用）
ENABLE_NBA_SCHEDULE_NETWORK = os.environ.get("DSL_ENABLE_NBA_SCHEDULE_NETWORK", "false").lower() in ("true", "1", "yes")

ESPN_NBA_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
OFFICIAL_NBA_SCHEDULE_URL = "https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json"
BALLDONTLIE_GAMES_URL = "https://api.balldontlie.io/v1/games"
ENV_BDL_API_KEY = "DSL_BDL_API_KEY"
DEFAULT_NBA_SCHEDULE_PROVIDER = PROVIDER_BALLDONTLIE_NBA_GAMES

# Workstream B.3: 12 类 fail-closed 绑定拒绝码
REASON_OFFICIAL_SCHEDULE_UNAVAILABLE = "OFFICIAL_SCHEDULE_UNAVAILABLE"
REASON_OFFICIAL_GAME_NOT_FOUND = "OFFICIAL_GAME_NOT_FOUND"
REASON_OFFICIAL_GAME_TIME_MISSING = "OFFICIAL_GAME_TIME_MISSING"
REASON_OFFICIAL_GAME_TIME_INVALID = "OFFICIAL_GAME_TIME_INVALID"
REASON_OFFICIAL_GAME_NOT_SCHEDULED = "OFFICIAL_GAME_NOT_SCHEDULED"
REASON_TEAM_ID_MAPPING_UNVERIFIED = "TEAM_ID_MAPPING_UNVERIFIED"
REASON_HOME_AWAY_ORIENTATION_CONFLICT = "HOME_AWAY_ORIENTATION_CONFLICT"
REASON_BEIJING_EVENT_DATE_MISMATCH = "BEIJING_EVENT_DATE_MISMATCH"
REASON_MULTIPLE_OFFICIAL_CANDIDATES = "MULTIPLE_OFFICIAL_CANDIDATES"
REASON_MULTIPLE_CANONICAL_CANDIDATES = "MULTIPLE_CANONICAL_CANDIDATES"
REASON_TIME_PROVENANCE_CONFLICT = "TIME_PROVENANCE_CONFLICT"
REASON_GAME_ALREADY_STARTED = "GAME_ALREADY_STARTED"

# Workstream D: 排期时间变更分类
CHANGE_FIRST_VERIFIED_KICKOFF = "FIRST_VERIFIED_KICKOFF"
CHANGE_SCHEDULED_TIME_UNCHANGED = "SCHEDULED_TIME_UNCHANGED"
CHANGE_OFFICIAL_RESCHEDULE_CONFIRMED = "OFFICIAL_RESCHEDULE_CONFIRMED"
CHANGE_SCHEDULE_CONFLICT_UNRESOLVED = "SCHEDULE_CONFLICT_UNRESOLVED"
CHANGE_POST_START_UPDATE_REJECTED = "POST_START_UPDATE_REJECTED"

_SCHEDULE_CACHE: dict[str, tuple[float, list[dict], dict]] = {}
SCHEDULE_CACHE_TTL = 90

_UTC_EXPLICIT_OFFSET_RE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


@dataclass(frozen=True)
class NbaFranchiseMapping:
    canonical_code: str
    espn_team_id: str
    nba_team_id: str
    official_name: str
    chinese_names: tuple[str, ...]
    balldontlie_team_id: str = ""


# 严格枚举的 30 支 NBA 球队映射（来源：NBA 官方球队编码与 ESPN NBA Teams API + 库内 500.com 中文队名 + BALLDONTLIE Team ID）
NBA_FRANCHISE_MAPPINGS: tuple[NbaFranchiseMapping, ...] = (
    NbaFranchiseMapping("ATL", "1", "1610612737", "Atlanta Hawks", ("亚特兰大老鹰",), "1"),
    NbaFranchiseMapping("BOS", "2", "1610612738", "Boston Celtics", ("波士顿凯尔特人",), "2"),
    NbaFranchiseMapping("BKN", "17", "1610612751", "Brooklyn Nets", ("布鲁克林篮网", "布鲁克林篮网队"), "3"),
    NbaFranchiseMapping("CHA", "30", "1610612766", "Charlotte Hornets", ("夏洛特黄蜂",), "4"),
    NbaFranchiseMapping("CHI", "4", "1610612741", "Chicago Bulls", ("芝加哥公牛",), "5"),
    NbaFranchiseMapping("CLE", "5", "1610612739", "Cleveland Cavaliers", ("克利夫兰骑士", "克里夫兰骑士"), "6"),
    NbaFranchiseMapping("DAL", "6", "1610612742", "Dallas Mavericks", ("达拉斯独行侠", "达拉斯小牛"), "7"),
    NbaFranchiseMapping("DEN", "7", "1610612743", "Denver Nuggets", ("丹佛掘金",), "8"),
    NbaFranchiseMapping("DET", "8", "1610612765", "Detroit Pistons", ("底特律活塞",), "9"),
    NbaFranchiseMapping("GSW", "9", "1610612744", "Golden State Warriors", ("金州勇士",), "10"),
    NbaFranchiseMapping("HOU", "10", "1610612745", "Houston Rockets", ("休斯顿火箭",), "11"),
    NbaFranchiseMapping("IND", "11", "1610612754", "Indiana Pacers", ("印第安纳步行者", "印第安那步行者"), "12"),
    NbaFranchiseMapping("LAC", "12", "1610612746", "LA Clippers", ("洛杉矶快船",), "13"),
    NbaFranchiseMapping("LAL", "13", "1610612747", "Los Angeles Lakers", ("洛杉矶湖人",), "14"),
    NbaFranchiseMapping("MEM", "29", "1610612763", "Memphis Grizzlies", ("孟菲斯灰熊",), "15"),
    NbaFranchiseMapping("MIA", "14", "1610612748", "Miami Heat", ("迈阿密热火",), "16"),
    NbaFranchiseMapping("MIL", "15", "1610612749", "Milwaukee Bucks", ("密尔沃基雄鹿",), "17"),
    NbaFranchiseMapping("MIN", "16", "1610612750", "Minnesota Timberwolves", ("明尼苏达森林狼",), "18"),
    NbaFranchiseMapping("NOP", "3", "1610612740", "New Orleans Pelicans", ("新奥尔良鹈鹕",), "19"),
    NbaFranchiseMapping("NYK", "18", "1610612752", "New York Knicks", ("纽约尼克斯",), "20"),
    NbaFranchiseMapping("OKC", "25", "1610612760", "Oklahoma City Thunder", ("俄克拉荷马城雷霆", "俄克拉荷马雷霆"), "21"),
    NbaFranchiseMapping("ORL", "19", "1610612753", "Orlando Magic", ("奥兰多魔术",), "22"),
    NbaFranchiseMapping("PHI", "20", "1610612755", "Philadelphia 76ers", ("费城76人",), "23"),
    NbaFranchiseMapping("PHX", "21", "1610612756", "Phoenix Suns", ("菲尼克斯太阳",), "24"),
    NbaFranchiseMapping("POR", "22", "1610612757", "Portland Trail Blazers", ("波特兰开拓者",), "25"),
    NbaFranchiseMapping("SAC", "23", "1610612758", "Sacramento Kings", ("萨克拉门托国王",), "26"),
    NbaFranchiseMapping("SAS", "24", "1610612759", "San Antonio Spurs", ("圣安东尼奥马刺",), "27"),
    NbaFranchiseMapping("TOR", "28", "1610612761", "Toronto Raptors", ("多伦多猛龙",), "28"),
    NbaFranchiseMapping("UTA", "26", "1610612762", "Utah Jazz", ("犹他爵士",), "29"),
    NbaFranchiseMapping("WAS", "27", "1610612764", "Washington Wizards", ("华盛顿奇才",), "30"),
)

_CHINESE_NAME_TO_FRANCHISE: dict[str, str] = {
    name: entry.canonical_code
    for entry in NBA_FRANCHISE_MAPPINGS
    for name in entry.chinese_names
}

_PROVIDER_TEAM_ID_TO_FRANCHISE: dict[str, str] = {}
for _entry in NBA_FRANCHISE_MAPPINGS:
    _PROVIDER_TEAM_ID_TO_FRANCHISE[f"espn:{_entry.espn_team_id}"] = _entry.canonical_code
    _PROVIDER_TEAM_ID_TO_FRANCHISE[f"nba:{_entry.nba_team_id}"] = _entry.canonical_code
    if _entry.balldontlie_team_id:
        _PROVIDER_TEAM_ID_TO_FRANCHISE[f"bdl:{_entry.balldontlie_team_id}"] = _entry.canonical_code
        _PROVIDER_TEAM_ID_TO_FRANCHISE[f"balldontlie:{_entry.balldontlie_team_id}"] = _entry.canonical_code


def resolve_canonical_team_code(name: Any) -> str | None:
    """仅接受显式登记的 NBA 中文队名，不做模糊匹配。"""
    if not isinstance(name, str):
        return None
    return _CHINESE_NAME_TO_FRANCHISE.get(name.strip())


def resolve_provider_team_code(identifier: Any) -> str | None:
    """仅接受显式登记的 `espn:<id>` 或 `nba:<id>` 球队标识符。"""
    if not isinstance(identifier, str):
        return None
    return _PROVIDER_TEAM_ID_TO_FRANCHISE.get(identifier.strip())


def convert_utc_to_beijing(utc_value: Any) -> tuple[str | None, str | None, datetime | None, str]:
    """
    将带明确时区偏移的 UTC ISO 时间戳转为北京时间。

    - 缺失值 -> `(None, None, None, "MISSING_UTC")`
    - 无时区偏移、无时钟部分或非法日历日期 -> `(None, None, None, "INVALID_UTC")`
    - 成功 -> `(YYYY-MM-DD, HH:MM, dt_beijing, "VALID_UTC")`
    """
    if utc_value is None:
        return None, None, None, "MISSING_UTC"
    if not isinstance(utc_value, str):
        return None, None, None, "INVALID_UTC"
    raw = utc_value.strip()
    if not raw:
        return None, None, None, "MISSING_UTC"

    if "T" not in raw and " " not in raw:
        return None, None, None, "INVALID_UTC"
    if not _UTC_EXPLICIT_OFFSET_RE.search(raw):
        return None, None, None, "INVALID_UTC"

    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None, None, None, "INVALID_UTC"

    if parsed.tzinfo is None:
        return None, None, None, "INVALID_UTC"

    bj_dt = parsed.astimezone(BEIJING_TZ)
    return bj_dt.strftime("%Y-%m-%d"), bj_dt.strftime("%H:%M"), bj_dt, "VALID_UTC"


def _map_espn_status(status_name: Any) -> str:
    raw = str(status_name or "").strip().upper()
    if raw == "STATUS_SCHEDULED":
        return "scheduled"
    if raw in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME", "STATUS_END_PERIOD"):
        return "live"
    if raw == "STATUS_FINAL":
        return "final"
    if raw == "STATUS_POSTPONED":
        return "postponed"
    if raw == "STATUS_CANCELED":
        return "canceled"
    return "unknown"


def _map_nba_official_status(raw_status: Any, postponed_status: Any, status_text: Any) -> str:
    post_flag = str(postponed_status or "").strip().upper()
    text_lower = str(status_text or "").strip().lower()
    if post_flag in ("Y", "A", "POSTPONED") or "ppd" in text_lower or "postpon" in text_lower:
        return "postponed"
    if "cancel" in text_lower or post_flag in ("C", "CANCELED"):
        return "canceled"
    if raw_status == 1 or str(raw_status) == "1":
        return "scheduled"
    if raw_status == 2 or str(raw_status) == "2":
        return "live"
    if raw_status == 3 or str(raw_status) == "3":
        return "final"
    return "unknown"


def _map_balldontlie_status(status_state: Any, postponed: Any, status_text: Any) -> str:
    # status is display text, never positive evidence for a scheduled game.
    state = str(status_state or "").strip().lower()
    text = str(status_text or "").strip().lower()
    if postponed is True or "postpon" in text:
        return "postponed"
    if "cancel" in text:
        return "canceled"
    states = {"scheduled", "in_progress", "final", "postponed", "canceled",
              "delayed", "suspended", "abandoned", "unknown"}
    if state not in states or type(postponed) is not bool:
        return "unknown"
    if state == "scheduled" and any(token in text for token in
                                     ("final", "live", "halftime", "qtr", "delayed", "suspended", "abandoned")):
        return "unknown"
    return "live" if state == "in_progress" else state


def parse_espn_nba_scoreboard(
    payload: dict,
    *,
    source_reference: str = ESPN_NBA_SCOREBOARD_URL,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    """解析公开 ESPN NBA Scoreboard API 载荷为标准观测结构；严格校验 NBA 联赛身份并过滤非 NBA 赛事。"""
    if not isinstance(payload, dict):
        return [], ["MALFORMED_PAYLOAD"]

    obs_iso = (observed_at or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).isoformat()

    # 严格联赛标识校验 (Workstream E)
    leagues = payload.get("leagues")
    if not isinstance(leagues, list) or not leagues:
        return [], ["MISSING_LEAGUE_IDENTITY"]

    league_obj = leagues[0]
    if not isinstance(league_obj, dict):
        return [], ["MALFORMED_LEAGUE_IDENTITY"]

    league_uid = str(league_obj.get("uid") or "").strip()
    league_abbr = str(league_obj.get("abbreviation") or "").strip().upper()
    league_slug = str(league_obj.get("slug") or "").strip().lower()

    if not league_uid or not league_abbr:
        return [], ["MISSING_LEAGUE_IDENTITY"]

    uid_parts = league_uid.split("~")

    # 1. 矛盾元数据检查（如缩写/slug 是 NBA 但 uid 是非 NBA，或相反）
    is_nba_uid = ("s:40" in uid_parts and "l:46" in uid_parts)
    is_nba_abbr = (league_abbr == "NBA" or league_slug == "nba")
    if is_nba_uid != is_nba_abbr:
        return [], ["CONTRADICTORY_LEAGUE_METADATA"]

    # 2. 误导性子串检查（如 l:460 或 l:146，虽包含 "46" 但不是真正的 NBA l:46）
    if "46" in league_uid and "l:46" not in uid_parts:
        return [], ["MISLEADING_LEAGUE_SUBSTRING"]

    # 3. 显式 WNBA 过滤
    if league_abbr == "WNBA" or "l:59" in uid_parts or league_slug == "wnba":
        return [], ["WNBA_LEAGUE_FILTERED"]

    if not is_nba_uid or not is_nba_abbr:
        return [], ["NON_NBA_LEAGUE"]

    observations: list[dict] = []
    errors: list[str] = []
    seen_events: dict[str, dict] = {}

    for ev in payload.get("events", []) or []:
        if not isinstance(ev, dict):
            errors.append("MALFORMED_EVENT")
            continue

        ev_uid = str(ev.get("uid") or "").strip()
        if not ev_uid:
            errors.append("MISSING_EVENT_UID")
            continue

        ev_parts = ev_uid.split("~")
        if "s:40" not in ev_parts or "l:46" not in ev_parts:
            # 显式过滤非 NBA（如 WNBA l:59）赛事事件
            continue

        gid = ev.get("id")
        if gid is None or not str(gid).strip():
            errors.append("MISSING_GAME_ID")
            continue
        gid_str = str(gid).strip()

        comps = ev.get("competitions") or []
        comp = comps[0] if comps and isinstance(comps[0], dict) else {}
        competitors = comp.get("competitors") or []
        home = next((c for c in competitors if isinstance(c, dict) and c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if isinstance(c, dict) and c.get("homeAway") == "away"), None)
        if not home or not away:
            errors.append(f"MISSING_COMPETITORS:{gid_str}")
            continue

        h_team = home.get("team") or {}
        a_team = away.get("team") or {}
        h_id = str(h_team.get("id") or "").strip()
        a_id = str(a_team.get("id") or "").strip()
        if not h_id or not a_id:
            errors.append(f"MISSING_TEAM_ID:{gid_str}")
            continue

        raw_utc = ev.get("date")
        bj_date, bj_time, _, parse_status = convert_utc_to_beijing(raw_utc)
        raw_status_name = ((ev.get("status") or {}).get("type") or {}).get("name")
        season_obj = ev.get("season") or (league_obj.get("season") if isinstance(league_obj, dict) else {}) or {}
        season_year = season_obj.get("year")
        season_label = f"{int(season_year) - 1}-{str(int(season_year))[-2:]}" if isinstance(season_year, int) else str(season_obj.get("displayName") or "")

        obs_entry = {
            "provider": PROVIDER_ESPN_NBA_SCOREBOARD,
            "provider_game_id": gid_str,
            "season": season_label,
            "league_uid": ev_uid,
            "game_status": _map_espn_status(raw_status_name),
            "raw_status": str(raw_status_name or ""),
            "home_team_identifier": f"espn:{h_id}",
            "away_team_identifier": f"espn:{a_id}",
            "home_team_name": str(h_team.get("displayName") or ""),
            "away_team_name": str(a_team.get("displayName") or ""),
            "home_tricode": str(h_team.get("abbreviation") or ""),
            "away_tricode": str(a_team.get("abbreviation") or ""),
            "scheduled_start_utc": raw_utc.strip() if isinstance(raw_utc, str) and parse_status == "VALID_UTC" else (raw_utc if isinstance(raw_utc, str) else None),
            "beijing_date": bj_date,
            "beijing_time": bj_time,
            "time_parse_status": parse_status,
            "observed_at": obs_iso,
            "source_reference": source_reference,
        }

        # 检查同载荷内重复赛事 ID 及冲突 (Workstream E)
        if gid_str in seen_events:
            prev = seen_events[gid_str]
            if (
                prev["home_team_identifier"] != obs_entry["home_team_identifier"]
                or prev["away_team_identifier"] != obs_entry["away_team_identifier"]
                or prev["scheduled_start_utc"] != obs_entry["scheduled_start_utc"]
            ):
                errors.append(f"CONFLICTING_DUPLICATE_EVENT:{gid_str}")
                continue
            # 相同内容幂等忽略
            continue

        seen_events[gid_str] = obs_entry
        observations.append(obs_entry)

    return observations, errors


def parse_nba_official_schedule(
    payload: dict,
    *,
    source_reference: str = OFFICIAL_NBA_SCHEDULE_URL,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    """解析 NBA 官方 `scheduleLeagueV2` JSON 结构；只接受 leagueId == '00'（NBA）。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("leagueSchedule"), dict):
        return [], ["MALFORMED_OFFICIAL_SCHEDULE"]

    sched = payload["leagueSchedule"]
    league_id = str(sched.get("leagueId") or "").strip()
    if league_id != "00":
        return [], ["NON_NBA_LEAGUE_ID"]

    season_year = str(sched.get("seasonYear") or "")
    obs_iso = (observed_at or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).isoformat()
    observations: list[dict] = []
    errors: list[str] = []

    for gd in sched.get("gameDates", []) or []:
        if not isinstance(gd, dict):
            continue
        for g in gd.get("games", []) or []:
            if not isinstance(g, dict):
                errors.append("MALFORMED_GAME")
                continue
            gid = g.get("gameId")
            if gid is None or not str(gid).strip():
                errors.append("MISSING_GAME_ID")
                continue
            gid_str = str(gid).strip()

            h_team = g.get("homeTeam") or {}
            a_team = g.get("awayTeam") or {}
            h_id = str(h_team.get("teamId") or "").strip()
            a_id = str(a_team.get("teamId") or "").strip()
            if not h_id or not a_id or h_id == "0" or a_id == "0":
                errors.append(f"MISSING_TEAM_ID:{gid_str}")
                continue

            raw_utc = g.get("gameDateTimeUTC")
            bj_date, bj_time, _, parse_status = convert_utc_to_beijing(raw_utc)
            observations.append(
                {
                    "provider": PROVIDER_NBA_OFFICIAL_SCHEDULE,
                    "provider_game_id": gid_str,
                    "season": season_year,
                    "league_uid": league_id,
                    "game_status": _map_nba_official_status(
                        g.get("gameStatus"), g.get("postponedStatus"), g.get("gameStatusText")
                    ),
                    "raw_status": str(g.get("gameStatusText") or g.get("gameStatus") or ""),
                    "home_team_identifier": f"nba:{h_id}",
                    "away_team_identifier": f"nba:{a_id}",
                    "home_team_name": f"{h_team.get('teamCity', '')} {h_team.get('teamName', '')}".strip(),
                    "away_team_name": f"{a_team.get('teamCity', '')} {a_team.get('teamName', '')}".strip(),
                    "home_tricode": str(h_team.get("teamTricode") or ""),
                    "away_tricode": str(a_team.get("teamTricode") or ""),
                    "scheduled_start_utc": raw_utc.strip() if isinstance(raw_utc, str) and parse_status == "VALID_UTC" else (raw_utc if isinstance(raw_utc, str) else None),
                    "beijing_date": bj_date,
                    "beijing_time": bj_time,
                    "time_parse_status": parse_status,
                    "observed_at": obs_iso,
                    "source_reference": source_reference,
                }
            )

    return observations, errors


def parse_balldontlie_nba_games(
    payload: dict,
    *,
    source_reference: str = BALLDONTLIE_GAMES_URL,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    """
    解析官方文档化的 BALLDONTLIE NBA Games API 响应载荷为标准赛程观测结构。
    严格保留主客方向（home_team 为主，visitor_team 为客）、UTC 开赛时间与 30 支球队标识。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [], ["MALFORMED_BALLDONTLIE_PAYLOAD"]

    obs_iso = (observed_at or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).isoformat()
    observations: list[dict] = []
    errors: list[str] = []
    seen_events: dict[str, dict] = {}

    for g in payload.get("data", []) or []:
        if not isinstance(g, dict):
            errors.append("MALFORMED_GAME")
            continue

        gid = g.get("id")
        if gid is None or not str(gid).strip():
            errors.append("MISSING_GAME_ID")
            continue
        gid_str = str(gid).strip()

        home_team = g.get("home_team")
        visitor_team = g.get("visitor_team")
        if not isinstance(home_team, dict) or not isinstance(visitor_team, dict):
            errors.append(f"MISSING_COMPETITORS:{gid_str}")
            continue

        h_id = str(home_team.get("id") or "").strip()
        a_id = str(visitor_team.get("id") or "").strip()
        if not h_id or not a_id or h_id == "0" or a_id == "0":
            errors.append(f"MISSING_TEAM_ID:{gid_str}")
            continue

        raw_utc = g.get("datetime")
        bj_date, bj_time, _, parse_status = convert_utc_to_beijing(raw_utc)
        game_status = _map_balldontlie_status(g.get("status_state"), g.get("postponed"), g.get("status"))
        season_val = g.get("season")
        season_str = str(season_val) if season_val is not None else ""

        obs_entry = {
            "provider": PROVIDER_BALLDONTLIE_NBA_GAMES,
            "provider_game_id": gid_str,
            "season": season_str,
            "league_uid": "balldontlie_nba",
            "game_status": game_status,
            "raw_status": str(g.get("status") or ""),
            "status_state": g.get("status_state"),
            "postponed": g.get("postponed"),
            "source_date": g.get("date"),
            "home_team_identifier": f"bdl:{h_id}",
            "away_team_identifier": f"bdl:{a_id}",
            "home_team_name": str(home_team.get("full_name") or f"{home_team.get('city', '')} {home_team.get('name', '')}".strip()),
            "away_team_name": str(visitor_team.get("full_name") or f"{visitor_team.get('city', '')} {visitor_team.get('name', '')}".strip()),
            "home_tricode": str(home_team.get("abbreviation") or ""),
            "away_tricode": str(visitor_team.get("abbreviation") or ""),
            "scheduled_start_utc": raw_utc.strip() if isinstance(raw_utc, str) and parse_status == "VALID_UTC" else (raw_utc if isinstance(raw_utc, str) else None),
            "beijing_date": bj_date,
            "beijing_time": bj_time,
            "time_parse_status": parse_status,
            "observed_at": obs_iso,
            "source_reference": source_reference,
        }

        if gid_str in seen_events:
            prev = seen_events[gid_str]
            if prev != obs_entry:
                errors.append(f"CONFLICTING_DUPLICATE_EVENT:{gid_str}")
                observations = [r for r in observations if r["provider_game_id"] != gid_str]
                continue
            continue

        seen_events[gid_str] = obs_entry
        observations.append(obs_entry)

    return observations, errors


def clear_schedule_cache() -> None:
    _SCHEDULE_CACHE.clear()


_LAST_BDL_REQUEST_MONO: float | None = None
_BDL_RETRY_NOT_BEFORE = 0.0
_BDL_REQUEST_LOCK = threading.Lock()
# Strictly fewer than six starts in any 60-second window, including page/retry starts.
BDL_PACING_INTERVAL = max(12.1, float(os.environ.get("DSL_BDL_PACING_INTERVAL", "12.1")))


def _retry_after_seconds(value: Any, received_at: datetime) -> float:
    try:
        if str(value).strip().isdigit():
            return float(value)
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is not None:
            return max(0.0, (parsed - received_at).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    return 0.0


def _pagination_not_attempted(per_page, reason):
    return {"requested_date_window": [], "requested_page_size": per_page,
            "pages_fetched": 0, "provider_rows_observed": 0, "unique_games_observed": 0,
            "duplicates_deduplicated": 0, "conflicting_game_ids": [], "next_cursor_remaining": None,
            "pagination_complete": False, "pagination_failure_reason": reason, "network_requests_made": 0}


def _fetch_bdl_pages(query_days, *, api_key, timeout, per_page, max_requests, max_elapsed_seconds):
    """Bounded, process-local HTTP dispatch; partial evidence never leaves as usable rows."""
    from utils.daily_loader import get_beijing_now

    global _LAST_BDL_REQUEST_MONO, _BDL_RETRY_NOT_BEFORE
    started = time.monotonic()
    deadline = started + max_elapsed_seconds
    started_at = get_beijing_now()
    base_url = BALLDONTLIE_GAMES_URL + "?" + "&".join(f"dates[]={q}" for q in query_days)
    base_url += f"&per_page={per_page}"
    by_id, raw_by_id, conflicts = {}, {}, set()
    cursor, seen_cursors = None, set()
    pages = observed = duplicates = attempts = 0
    errors = []
    failure = None
    complete = False
    received_at = started_at
    while not complete and failure is None:
        url = base_url + (f"&cursor={cursor}" if cursor is not None else "")
        response = None
        for retry in range(3):
            # Serial dispatch shares quota across calls, pages and retries. Cache clear does not reset it.
            if not _BDL_REQUEST_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
                failure = "TIME_BUDGET_EXHAUSTED"
                break
            try:
                current = time.monotonic()
                ready = max(current, _BDL_RETRY_NOT_BEFORE,
                            (_LAST_BDL_REQUEST_MONO + BDL_PACING_INTERVAL)
                            if _LAST_BDL_REQUEST_MONO is not None else current)
                if attempts >= max_requests:
                    failure = "REQUEST_BUDGET_EXHAUSTED"
                    break
                if ready >= deadline:
                    failure = "TIME_BUDGET_EXHAUSTED"
                    break
                while time.monotonic() < ready:
                    time.sleep(ready - time.monotonic())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "TIME_BUDGET_EXHAUSTED"
                    break
                attempts += 1
                _LAST_BDL_REQUEST_MONO = time.monotonic()
                try:
                    response = requests.get(url, headers={"Authorization": api_key}, timeout=min(timeout, remaining))
                    received_at = get_beijing_now()
                except Exception:
                    # Exception messages/response bodies can echo Authorization: never emit them.
                    failure = "REQUEST_FAILED"
                    break
                if response.status_code == 429:
                    delay = max(min(2 ** retry, 60), _retry_after_seconds(
                        getattr(response, "headers", {}).get("Retry-After"), received_at))
                    _BDL_RETRY_NOT_BEFORE = time.monotonic() + delay
            finally:
                _BDL_REQUEST_LOCK.release()
            if response.status_code != 429:
                break
            if retry == 2:
                failure = "HTTP_429_RATE_LIMITED"
        if failure:
            break
        if response.status_code != 200:
            failure = {401: "HTTP_401_UNAUTHORIZED", 403: "HTTP_403_FORBIDDEN"}.get(
                response.status_code, f"HTTP_{response.status_code}")
            break
        try:
            payload = response.json()
        except Exception:
            failure = "MALFORMED_JSON"
            break
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            failure = "MALFORMED_BALLDONTLIE_PAYLOAD"
            break
        pages += 1
        observed += len(payload["data"])
        rows, parse_errors = parse_balldontlie_nba_games(payload, source_reference=url, observed_at=received_at)
        if parse_errors:
            # Do not echo untrusted provider identifiers into diagnostics.
            failure = "PROVIDER_PARSE_FAILED"
        row_map = {r["provider_game_id"]: r for r in rows}
        for raw in payload["data"]:
            if not isinstance(raw, dict) or type(raw.get("id")) is not int or raw["id"] <= 0:
                failure = "MALFORMED_GAME_ID"
                continue
            gid = str(raw["id"])
            if gid in raw_by_id:
                if raw_by_id[gid] != raw:
                    conflicts.add(gid)
                    by_id.pop(gid, None)
                    failure = "CONFLICTING_PROVIDER_GAME_ID"
                else:
                    duplicates += 1
            else:
                raw_by_id[gid] = raw
                if gid in row_map:
                    by_id[gid] = row_map[gid]
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            failure = failure or "MALFORMED_PAGINATION_META"
            break
        next_cursor = meta.get("next_cursor")
        if next_cursor is not None and (type(next_cursor) is not int or next_cursor < 0):
            failure = failure or "MALFORMED_CURSOR"
            break
        if next_cursor is not None and next_cursor in seen_cursors:
            failure = failure or "CURSOR_CYCLE"
            cursor = next_cursor
            break
        cursor = next_cursor
        if next_cursor is None:
            complete = failure is None
        else:
            seen_cursors.add(next_cursor)
        if time.monotonic() >= deadline:
            failure, complete = "TIME_BUDGET_EXHAUSTED", False
    if failure:
        errors.append(failure)
    complete = complete and failure is None
    observations = sorted(by_id.values(), key=lambda r: (r.get("beijing_date") or "", r["provider_game_id"])) if complete else []
    diag = {
        "provider": PROVIDER_BALLDONTLIE_NBA_GAMES,
        "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY,
        "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED,
        "provider_authorization_status": "API_KEY_CONFIGURED",
        "automated_provider_operation": AUTOMATED_OPERATION_ENABLED,
        "status": ("ok" if observations else "empty") if complete else "unavailable",
        "coverage_status": "COMPLETE_DATE_FETCH" if complete else "PAGINATION_INCOMPLETE",
        "reachable": pages > 0,
        "request_started_at": started_at.isoformat(), "response_received_at": received_at.isoformat(),
        "checked_at": received_at.isoformat(), "observation_origin": "NETWORK", "cache_hit": False,
        "observation_age_seconds": 0.0,
        "requested_us_dates": query_days, "queried_us_dates": query_days,
        "successful_us_dates": query_days if complete else [], "failed_us_dates": [] if complete else query_days,
        "rows": len(observations), "errors": errors,
        "requested_date_window": query_days, "requested_page_size": per_page,
        "pages_fetched": pages, "provider_rows_observed": observed,
        "unique_games_observed": len(raw_by_id), "duplicates_deduplicated": duplicates,
        "conflicting_game_ids": sorted(conflicts), "next_cursor_remaining": cursor,
        "pagination_complete": complete, "pagination_failure_reason": failure,
        "network_requests_made": attempts, "max_requests": max_requests,
        "max_elapsed_seconds": max_elapsed_seconds, "rate_limit_scope": "PROCESS_LOCAL",
        "global_account_limit_guaranteed": False,
    }
    return observations, diag


def fetch_nba_schedule_observations(
    beijing_dates: Iterable[str] | None = None,
    *,
    timeout: float = 5.0,
    now: datetime | None = None,
    allow_network: bool = False,
    provider: str = PROVIDER_BALLDONTLIE_NBA_GAMES,
    api_key: str | None = None,
    per_page: int = 100,
    max_requests: int = 10,
    max_elapsed_seconds: float = 120.0,
) -> tuple[list[dict], dict]:
    """
    通过公开端点获取覆盖指定北京时间日期窗口的 NBA 赛程观测。
    在未经明确授权前，公开端点默认拒绝外网抓取 (Default-Deny, allow_network=False)。
    默认接入文档化的第三方 BALLDONTLIE NBA API，严格区分授权状态与凭据状态。
    """
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be an integer in [1, 100]")
    if type(max_requests) is not int or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0
               for v in (timeout, max_elapsed_seconds)):
        raise ValueError("request and elapsed time budgets must be finite and positive")
    now_bj = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    today = now_bj.date()
    min_d = today - timedelta(days=1)
    max_d = today + timedelta(days=7)

    raw_dates = sorted({d for d in (beijing_dates or []) if is_valid_calendar_date(d)})
    if not raw_dates:
        dates_set = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    else:
        dates_set = [
            d for d in raw_dates
            if min_d <= datetime.strptime(d, "%Y-%m-%d").date() <= max_d
        ]
        if not dates_set:
            is_bdl = (provider == PROVIDER_BALLDONTLIE_NBA_GAMES)
            return [], {
                **_pagination_not_attempted(per_page, "EMPTY_DATE_WINDOW"),
                "provider": provider,
                "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY if is_bdl else PROVIDER_TYPE_ESPN_THIRD_PARTY,
                "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED if is_bdl else PROVIDER_TYPE_UNSUPPORTED_PUBLIC,
                "provider_authorization_status": ("API_KEY_CONFIGURED" if (api_key or os.environ.get(ENV_BDL_API_KEY)) else "PROVIDER_CREDENTIALS_MISSING") if is_bdl else PROVIDER_AUTH_UNVERIFIED,
                "automated_provider_operation": AUTOMATED_OPERATION_ENABLED if allow_network else AUTOMATED_OPERATION_DISABLED,
                "status": "empty",
                "coverage_status": "EMPTY_DATE_WINDOW",
                "reachable": False,
                "requested_us_dates": [],
                "successful_us_dates": [],
                "failed_us_dates": [],
                "queried_us_dates": [],
                "rows": 0,
                "errors": [],
                "checked_at": now_bj.isoformat(),
                "note": "NO_NEAR_TERM_DATES",
                "official_nba_cdn_status": "http_403_not_accessible_without_bypass",
            }

    # 检查自动化网络请求许可 (Workstream D: Default-Deny)
    is_bdl = (provider == PROVIDER_BALLDONTLIE_NBA_GAMES)
    if not allow_network:
        return [], {
            **_pagination_not_attempted(per_page, "NETWORK_DISABLED"),
            "provider": provider,
            "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY if is_bdl else PROVIDER_TYPE_ESPN_THIRD_PARTY,
            "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED if is_bdl else PROVIDER_TYPE_UNSUPPORTED_PUBLIC,
            "provider_authorization_status": ("API_KEY_CONFIGURED" if (api_key or os.environ.get(ENV_BDL_API_KEY)) else "PROVIDER_CREDENTIALS_MISSING") if is_bdl else PROVIDER_AUTH_UNVERIFIED,
            "automated_provider_operation": AUTOMATED_OPERATION_DISABLED,
            "status": "disabled_pending_authorization",
            "coverage_status": "UNAUTHORIZED_DATE_FETCH",
            "reachable": False,
            "requested_us_dates": [],
            "successful_us_dates": [],
            "failed_us_dates": [],
            "queried_us_dates": [],
            "rows": 0,
            "errors": ["PROVIDER_AUTOMATED_USE_UNAUTHORIZED"],
            "checked_at": now_bj.isoformat(),
            "note": "AUTOMATED_NETWORK_FETCH_DISABLED_PENDING_EXPLICIT_AUTHORIZATION",
            "official_nba_cdn_status": "http_403_not_accessible_without_bypass",
        }

    # BALLDONTLIE 凭据检查：若允许联网但缺少 API Key，明确返回 disabled_missing_credentials
    effective_api_key = api_key or os.environ.get(ENV_BDL_API_KEY)
    if is_bdl and not effective_api_key:
        return [], {
            **_pagination_not_attempted(per_page, "PROVIDER_CREDENTIALS_MISSING"),
            "provider": PROVIDER_BALLDONTLIE_NBA_GAMES,
            "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY,
            "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED,
            "provider_authorization_status": "PROVIDER_CREDENTIALS_MISSING",
            "automated_provider_operation": AUTOMATED_OPERATION_DISABLED,
            "status": "disabled_missing_credentials",
            "coverage_status": "MISSING_API_KEY",
            "reachable": False,
            "requested_us_dates": [],
            "successful_us_dates": [],
            "failed_us_dates": [],
            "queried_us_dates": [],
            "rows": 0,
            "errors": ["PROVIDER_CREDENTIALS_MISSING"],
            "checked_at": now_bj.isoformat(),
            "note": "BALLDONTLIE_API_KEY_REQUIRED",
            "official_nba_cdn_status": "http_403_not_accessible_without_bypass",
        }

    query_days: list[str] = []
    seen_q: set[str] = set()
    for d_str in dates_set:
        d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
        for candidate in (d_obj - timedelta(days=1), d_obj):
            q = candidate.strftime("%Y-%m-%d") if is_bdl else candidate.strftime("%Y%m%d")
            if q not in seen_q:
                seen_q.add(q)
                query_days.append(q)

    cache_key = f"{provider}:{per_page}:{','.join(query_days)}"
    now_mono = time.monotonic()
    cached = _SCHEDULE_CACHE.get(cache_key)
    if cached and (now_mono - cached[0]) < SCHEDULE_CACHE_TTL:
        cached_mono, cached_rows, cached_diag = cached
        diag = dict(cached_diag)
        diag["cache_hit"] = True
        diag["observation_origin"] = "CACHE"
        diag["observation_age_seconds"] = round(now_mono - cached_mono, 3)
        diag["network_requests_made"] = 0
        # 绝不为缓存行伪造新的 observed_at，保留其实际抓取观测时间
        return [dict(item) for item in cached_rows], diag

    by_gid: dict[str, dict] = {}
    errors: list[str] = []
    successful_dates: list[str] = []
    failed_dates: list[str] = []
    req_started_at = datetime.now(BEIJING_TZ)

    if is_bdl:
        observations, diag = _fetch_bdl_pages(
            query_days, api_key=effective_api_key, timeout=timeout, per_page=per_page,
            max_requests=max_requests, max_elapsed_seconds=max_elapsed_seconds,
        )
        if diag["pagination_complete"]:
            _SCHEDULE_CACHE[cache_key] = (time.monotonic(), [dict(r) for r in observations], dict(diag))
        return observations, diag

    for q in query_days:
        url = f"{ESPN_NBA_SCOREBOARD_URL}?dates={q}"
        try:
            resp = requests.get(url, timeout=timeout)
            resp_received_at = datetime.now(BEIJING_TZ)
            if resp.status_code != 200:
                errors.append(f"HTTP_{resp.status_code}:{q}")
                failed_dates.append(q)
                continue
            rows, p_errors = parse_espn_nba_scoreboard(
                resp.json(),
                source_reference=url,
                observed_at=resp_received_at,
            )
            errors.extend(p_errors)
            successful_dates.append(q)
            for row in rows:
                by_gid[row["provider_game_id"]] = row
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}:{q}")
            failed_dates.append(q)

    reachable = len(successful_dates) > 0
    if not successful_dates:
        coverage_status = "FAILED_DATE_FETCH"
    elif failed_dates:
        coverage_status = "PARTIAL_DATE_FETCH"
    else:
        coverage_status = "COMPLETE_DATE_FETCH"

    observations = sorted(by_gid.values(), key=lambda x: (x.get("beijing_date") or "", x.get("provider_game_id") or ""))
    status = "unavailable" if not reachable else ("ok" if observations else "empty")
    diag = {
        "provider": PROVIDER_ESPN_NBA_SCOREBOARD,
        "provider_type": PROVIDER_TYPE_ESPN_THIRD_PARTY,
        "provider_endpoint_type": PROVIDER_TYPE_UNSUPPORTED_PUBLIC,
        "provider_authorization_status": PROVIDER_AUTH_UNVERIFIED,
        "automated_provider_operation": AUTOMATED_OPERATION_ENABLED,
        "status": status,
        "coverage_status": coverage_status,
        "reachable": reachable,
        "request_started_at": req_started_at.isoformat(),
        "response_received_at": datetime.now(BEIJING_TZ).isoformat(),
        "observation_origin": "NETWORK",
        "cache_hit": False,
        "observation_age_seconds": 0.0,
        "requested_us_dates": query_days,
        "successful_us_dates": successful_dates,
        "failed_us_dates": failed_dates,
        "queried_us_dates": query_days,
        "rows": len(observations),
        "errors": errors,
        "checked_at": datetime.now(BEIJING_TZ).isoformat(),
        "official_nba_cdn_status": "http_403_not_accessible_without_bypass",
    }
    _SCHEDULE_CACHE[cache_key] = (now_mono, [dict(item) for item in observations], dict(diag))
    return observations, diag


def bind_schedule_observations_to_canonical(
    canonical_matches: list[dict],
    observations: list[dict],
    *,
    now: datetime | None = None,
    schedule_available: bool = True,
) -> dict:
    """
    将外部 NBA 赛程观测与既有 Canonical NBA 赛事做严格只读一对一绑定。

    只产出诊断决策与时间补全提案；不修改任何输入字典，不新建 Canonical 赛事。
    """
    now_bj = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)

    nba_canonical = [
        m for m in (canonical_matches or [])
        if isinstance(m, dict)
        and m.get("sport") == "basketball"
        and is_nba_league(m.get("league"))
    ]

    # 检测重复 Canonical 归属（同一 (date, home_code, away_code) 被多条 Canonical 声明）
    canonical_group_counts: dict[tuple[str, str, str], int] = {}
    for m in nba_canonical:
        hc = resolve_canonical_team_code(m.get("home"))
        ac = resolve_canonical_team_code(m.get("away"))
        d = str(m.get("date") or "").strip()
        if hc and ac and is_valid_calendar_date(d):
            canonical_group_counts[(d, hc, ac)] = canonical_group_counts.get((d, hc, ac), 0) + 1

    decisions: list[dict] = []
    if not schedule_available:
        for m in nba_canonical:
            decisions.append({
                "match_id": str(m.get("id") or ""),
                "bound": False,
                "reason": REASON_OFFICIAL_SCHEDULE_UNAVAILABLE,
                "change_classification": None,
                "observation": None,
            })
        return {"decisions": decisions, "bound_count": 0, "rejected_count": len(decisions)}

    # 预处理有效的 NBA 观测（拒绝非 NBA league_uid）
    valid_nba_obs: list[dict] = []
    for obs in (observations or []):
        if not isinstance(obs, dict):
            continue
        prov = obs.get("provider")
        luid = str(obs.get("league_uid") or "").strip()
        if prov == PROVIDER_NBA_OFFICIAL_SCHEDULE and luid and luid != "00":
            continue
        if prov == PROVIDER_ESPN_NBA_SCOREBOARD:
            luid_parts = luid.split("~")
            if "s:40" not in luid_parts or "l:46" not in luid_parts:
                continue
        if prov == PROVIDER_BALLDONTLIE_NBA_GAMES and luid and luid != "balldontlie_nba":
            continue
        valid_nba_obs.append(obs)

    for m in nba_canonical:
        mid = str(m.get("id") or "")
        m_date = str(m.get("date") or "").strip()
        hc = resolve_canonical_team_code(m.get("home"))
        ac = resolve_canonical_team_code(m.get("away"))

        if not hc or not ac:
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_TEAM_ID_MAPPING_UNVERIFIED,
                "change_classification": None,
                "observation": None,
            })
            continue

        if not is_valid_calendar_date(m_date):
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_OFFICIAL_GAME_TIME_INVALID,
                "change_classification": None,
                "observation": None,
            })
            continue

        if canonical_group_counts.get((m_date, hc, ac), 0) > 1:
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_MULTIPLE_CANONICAL_CANDIDATES,
                "change_classification": None,
                "observation": None,
            })
            continue

        can_date_obj = datetime.strptime(m_date, "%Y-%m-%d").date()

        same_dir_exact_date: list[dict] = []
        same_dir_near_date_mismatch: list[dict] = []
        reversed_near: list[dict] = []
        same_dir_missing_or_bad_time: list[dict] = []
        unverified_team_obs = False

        for obs in valid_nba_obs:
            oh = resolve_provider_team_code(obs.get("home_team_identifier"))
            oa = resolve_provider_team_code(obs.get("away_team_identifier"))
            bj_date = obs.get("beijing_date")
            p_status = obs.get("time_parse_status")

            if oh is None or oa is None:
                # 仅当在同日期窗口内有一侧恰好重合时记录 TEAM_ID_MAPPING_UNVERIFIED
                if (bj_date == m_date) and (oh in (hc, ac) or oa in (hc, ac)):
                    unverified_team_obs = True
                continue

            if oh == ac and oa == hc:
                if bj_date and is_valid_calendar_date(bj_date):
                    obs_d = datetime.strptime(bj_date, "%Y-%m-%d").date()
                    if abs((obs_d - can_date_obj).days) <= 1:
                        reversed_near.append(obs)
                else:
                    reversed_near.append(obs)
                continue

            if not (oh == hc and oa == ac):
                continue

            if p_status in ("MISSING_UTC", "INVALID_UTC") or not bj_date:
                same_dir_missing_or_bad_time.append(obs)
                continue

            if bj_date == m_date:
                same_dir_exact_date.append(obs)
            else:
                obs_d = datetime.strptime(bj_date, "%Y-%m-%d").date()
                if abs((obs_d - can_date_obj).days) <= 1:
                    same_dir_near_date_mismatch.append(obs)

        # 去重同一 (provider, provider_game_id, scheduled_start_utc, game_status) 副本；
        # 若同天同对阵存在多个不同官方场次或互相冲突的时间，按 MULTIPLE_OFFICIAL_CANDIDATES 拒绝
        unique_signatures = {
            (
                o.get("provider"),
                o.get("provider_game_id"),
                o.get("scheduled_start_utc"),
                o.get("game_status"),
            )
            for o in same_dir_exact_date
        }
        if len(unique_signatures) > 1:
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_MULTIPLE_OFFICIAL_CANDIDATES,
                "change_classification": CHANGE_SCHEDULE_CONFLICT_UNRESOLVED,
                "observation": None,
            })
            continue

        if not same_dir_exact_date:
            if same_dir_missing_or_bad_time:
                bad_obs = same_dir_missing_or_bad_time[0]
                if bad_obs.get("game_status") in ("postponed", "canceled"):
                    reason = REASON_OFFICIAL_GAME_NOT_SCHEDULED
                elif bad_obs.get("time_parse_status") == "MISSING_UTC":
                    reason = REASON_OFFICIAL_GAME_TIME_MISSING
                else:
                    reason = REASON_OFFICIAL_GAME_TIME_INVALID
            elif reversed_near:
                reason = REASON_HOME_AWAY_ORIENTATION_CONFLICT
            elif same_dir_near_date_mismatch:
                reason = REASON_BEIJING_EVENT_DATE_MISMATCH
            elif unverified_team_obs:
                reason = REASON_TEAM_ID_MAPPING_UNVERIFIED
            else:
                reason = REASON_OFFICIAL_GAME_NOT_FOUND
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": reason,
                "change_classification": None,
                "observation": None,
            })
            continue

        obs = same_dir_exact_date[0]
        if obs.get("game_status") != "scheduled":
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_OFFICIAL_GAME_NOT_SCHEDULED,
                "change_classification": CHANGE_POST_START_UPDATE_REJECTED if obs.get("game_status") in ("live", "final") else None,
                "observation": obs,
            })
            continue

        _, _, kickoff_dt, p_stat = convert_utc_to_beijing(obs.get("scheduled_start_utc"))
        if p_stat == "MISSING_UTC":
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_OFFICIAL_GAME_TIME_MISSING,
                "change_classification": None,
                "observation": obs,
            })
            continue
        if p_stat != "VALID_UTC" or kickoff_dt is None:
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_OFFICIAL_GAME_TIME_INVALID,
                "change_classification": None,
                "observation": obs,
            })
            continue

        stored_status = str(m.get("status") or "upcoming").strip().lower()
        if stored_status in ("live", "finished") or now_bj >= kickoff_dt:
            decisions.append({
                "match_id": mid,
                "bound": False,
                "reason": REASON_GAME_ALREADY_STARTED,
                "change_classification": CHANGE_POST_START_UPDATE_REJECTED,
                "observation": obs,
            })
            continue

        new_time = kickoff_dt.strftime("%H:%M")
        existing_known = is_kickoff_time_known(m)
        existing_time = str(m.get("time") or "").strip()
        existing_prov = m.get("kickoff_provenance") if isinstance(m.get("kickoff_provenance"), dict) else None

        if not existing_known:
            change_cls = CHANGE_FIRST_VERIFIED_KICKOFF
        elif existing_time == new_time:
            change_cls = CHANGE_SCHEDULED_TIME_UNCHANGED
        else:
            # 已有另一来源已知时间但与之冲突 -> 判定来源/变更合法性
            if existing_prov and existing_prov.get("actual_kickoff_time_source") == obs.get("provider") and existing_prov.get("kickoff_source_event_id") == obs.get("provider_game_id"):
                change_cls = CHANGE_OFFICIAL_RESCHEDULE_CONFIRMED
            else:
                decisions.append({
                    "match_id": mid,
                    "bound": False,
                    "reason": REASON_TIME_PROVENANCE_CONFLICT,
                    "change_classification": CHANGE_SCHEDULE_CONFLICT_UNRESOLVED,
                    "observation": obs,
                })
                continue

        decisions.append({
            "match_id": mid,
            "bound": True,
            "reason": "VERIFIED_TIME_BINDING",
            "change_classification": change_cls,
            "beijing_date": m_date,
            "beijing_time": new_time,
            "kickoff_at": kickoff_dt.isoformat(),
            "observation": obs,
        })

    bound_count = sum(1 for d in decisions if d["bound"])
    return {
        "decisions": decisions,
        "bound_count": bound_count,
        "rejected_count": len(decisions) - bound_count,
    }


def apply_nba_schedule_time_enrichment(
    canonical_matches: list[dict],
    observations: list[dict],
    *,
    now: datetime | None = None,
    schedule_available: bool = True,
    conflicted_match_ids: set[str] | None = None,
) -> dict:
    """
    就地仅更新已绑定的 Canonical NBA 记录的时间与溯源元数据。

    硬约束：绝不修改 id、sport、league、date、home、away、jczq_no、round、
    odds、score、status，绝不新增记录，绝不删除记录。
    """
    binding = bind_schedule_observations_to_canonical(
        canonical_matches,
        observations,
        now=now,
        schedule_available=schedule_available,
    )
    by_mid = {str(m.get("id") or ""): m for m in canonical_matches if isinstance(m, dict)}
    blocked_ids = set(conflicted_match_ids or ())

    enriched = 0
    unchanged = 0
    rescheduled = 0
    rejected = 0

    for dec in binding["decisions"]:
        mid = dec["match_id"]
        if mid in blocked_ids and dec["bound"]:
            dec["bound"] = False
            dec["reason"] = REASON_TIME_PROVENANCE_CONFLICT
            dec["change_classification"] = CHANGE_SCHEDULE_CONFLICT_UNRESOLVED
        if not dec["bound"]:
            rejected += 1
            continue

        m = by_mid.get(mid)
        obs = dec.get("observation") or {}
        if m is None:
            continue

        prov_ver_status = (
            "OFFICIAL_NBA_VERIFIED"
            if obs.get("provider") == PROVIDER_NBA_OFFICIAL_SCHEDULE
            else "SCHEDULE_PROVIDER_VERIFIED"
        )
        new_prov = {
            "actual_kickoff_time_source": obs.get("provider"),
            "kickoff_source_event_id": obs.get("provider_game_id"),
            "kickoff_source_observed_at": obs.get("observed_at"),
            "kickoff_source_scheduled_start_utc": obs.get("scheduled_start_utc"),
            "kickoff_verification_status": prov_ver_status,
        }

        cls = dec["change_classification"]
        if cls == CHANGE_FIRST_VERIFIED_KICKOFF:
            m["time"] = dec["beijing_time"]
            m["kickoff_time_known"] = True
            m["kickoff_provenance"] = new_prov
            enriched += 1
        elif cls == CHANGE_OFFICIAL_RESCHEDULE_CONFIRMED:
            m["time"] = dec["beijing_time"]
            m["kickoff_time_known"] = True
            m["kickoff_provenance"] = new_prov
            rescheduled += 1
        elif cls == CHANGE_SCHEDULED_TIME_UNCHANGED:
            if not isinstance(m.get("kickoff_provenance"), dict):
                m["kickoff_provenance"] = new_prov
                enriched += 1
            else:
                unchanged += 1

    return {
        "enriched": enriched,
        "rescheduled": rescheduled,
        "unchanged": unchanged,
        "rejected": rejected,
        "decisions": binding["decisions"],
    }
