"""NBA 历史完赛数据采集与确定性分析数据集（只读，独立于七个受保护存储）。

职责边界
--------
- 复用 ``utils.nba_schedule`` 的 30 支球队编码、UTC 转换、状态映射与**进程内共享频控**，
  但实现独立的「带比分」历史完赛解析器（赛程补全解析器不提取比分）。
- 仅接受**已确认完赛**（``status_state == final``、非延期、整数比分、主客不同、
  已知 NBA 球队 ID、唯一提供商比赛身份、合法赛季与时间戳）的比赛。
- 默认拒绝网络（``allow_network=False``）；缺少凭据明确返回 ``disabled_missing_credentials``。
- 产物是**只读分析数据集**，绝不写入 ``data/*.json`` 七个受保护存储，
  绝不改动 Canonical 500.com 市场记录、``same_event()`` 或彩票编号。

时间完整性
----------
区分「现在回溯取得的历史赛果」与「未来实时预测前真正可得的赛果」。
``source_observed_at`` 是本次检索时刻，**不**证明该赛果在历史预测时刻已被本应用观测。
前瞻预测只允许使用 ``scheduled_start_utc < cutoff`` 的输入比赛。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests

import config
import utils.nba_schedule as _ns
from utils.nba_schedule import (
    BALLDONTLIE_GAMES_URL,
    BEIJING_TZ,
    ENV_BDL_API_KEY,
    PROVIDER_BALLDONTLIE_NBA_GAMES,
    PROVIDER_TYPE_DOCUMENTED_AUTHORIZED,
    PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY,
    convert_utc_to_beijing,
    resolve_provider_team_code,
)

PROVIDER = PROVIDER_BALLDONTLIE_NBA_GAMES

# 历史完赛解析拒绝码
REASON_MALFORMED_PAYLOAD = "MALFORMED_HISTORICAL_PAYLOAD"
REASON_MALFORMED_GAME = "MALFORMED_GAME"
REASON_MISSING_GAME_ID = "MISSING_GAME_ID"
REASON_MISSING_COMPETITORS = "MISSING_COMPETITORS"
REASON_UNKNOWN_FRANCHISE = "UNKNOWN_NBA_FRANCHISE_ID"
REASON_SAME_TEAM = "HOME_AWAY_SAME_TEAM"
REASON_NOT_FINAL = "GAME_NOT_CONFIRMED_FINAL"
REASON_MALFORMED_SCORE = "MALFORMED_FINAL_SCORE"
REASON_MALFORMED_PERIOD = "MALFORMED_PERIOD"
REASON_MALFORMED_SEASON = "MALFORMED_SEASON"
REASON_INVALID_TIMESTAMP = "INVALID_EVENT_TIMESTAMP"
REASON_CONFLICTING_DUPLICATE = "CONFLICTING_DUPLICATE_GAME_ID"
REASON_UNSUPPORTED_COMPETITION = "UNSUPPORTED_COMPETITION"
REASON_AMBIGUOUS_CLASSIFICATION = "AMBIGUOUS_CLASSIFICATION"

# 数据集 / 获取门禁状态
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED_AUTH = "disabled_pending_authorization"
STATUS_DISABLED_CREDS = "disabled_missing_credentials"

_MIN_SEASON = 1946
_MAX_SEASON = 2100

# NBA 竞赛类型分类（常规赛资格）。标准常规赛 = 30 队 × 82 场 / 2 = 1230 场。
COMP_NORMAL_REGULAR = "NORMAL_REGULAR"
COMP_NBA_CUP_REGULAR = "NBA_CUP_REGULAR"
COMP_NBA_CUP_CHAMPIONSHIP = "NBA_CUP_CHAMPIONSHIP"
COMP_PLAY_IN = "PLAY_IN"
COMP_PLAYOFF = "PLAYOFF"
COMP_PRESEASON = "PRESEASON"
COMP_UNSUPPORTED = "UNSUPPORTED_COMPETITION"
COMP_AMBIGUOUS = "AMBIGUOUS_CLASSIFICATION"

# 官方 NBA 常规赛最后一日（美方当地日期）。Play-In 紧随其后，不计入常规赛战绩。
# 该映射人工维护、按官方赛历 grounding；新增**已完赛**赛季前必须补充其末日，
# 否则该赛季的 Play-In 无法被日期边界识别（早期赛季比赛不受影响）。
NBA_REGULAR_SEASON_END_DATES: dict[int, str] = {
    2025: "2026-04-12",
}

# 官方 NBA 常规赛揭幕日（美方当地日期，按官方赛程公告 grounding）。早于该日且无
# 季后赛 / NBA Cup 标记的记录是季前赛：响应不携带 season_type，缺少此边界时季前赛会被
# 默认判成常规赛。
NBA_REGULAR_SEASON_START_DATES: dict[int, str] = {
    2025: "2025-10-21",
    2026: "2026-10-20",
}

_IST_REGULAR_MARKERS = ("group", "quarterfinal", "quarter-final", "semifinal", "semi-final", "knockout")

_OFFICIAL_RULES: dict[str, str] = {
    COMP_NORMAL_REGULAR: "NBA regular season: 30 teams x 82 games / 2 = 1230 unique games",
    COMP_NBA_CUP_REGULAR: "NBA Cup group stage and knockout rounds (except the Championship) count toward regular-season standings",
    COMP_NBA_CUP_CHAMPIONSHIP: "NBA Cup Championship is a standalone final that does not count toward regular-season standings",
    COMP_PLAY_IN: "NBA Play-In Tournament determines playoff seeding and is not part of the regular season",
    COMP_PLAYOFF: "NBA Playoffs are postseason and not part of the regular season",
    COMP_PRESEASON: "NBA preseason games are exhibitions and not part of the regular season",
    COMP_UNSUPPORTED: "unsupported competition type",
    COMP_AMBIGUOUS: "ambiguous competition classification",
}


def _ist_is_regular_stage(ist_norm: str) -> bool:
    return any(marker in ist_norm for marker in _IST_REGULAR_MARKERS)


def _classification(competition_type: str, eligible: bool, source: str, reason: str, *, reject: bool) -> dict:
    return {
        "competition_type": competition_type,
        "counts_as_regular_season": eligible,
        "classification_source": source,
        "classification_reason": reason,
        "official_rule": _OFFICIAL_RULES[competition_type],
        "reject": reject,
    }


def classify_nba_competition(
    raw: dict,
    *,
    regular_season_end_date: str | None = None,
    regular_season_start_date: str | None = None,
) -> dict:
    """对单场提供商记录判定 NBA 竞赛类型与常规赛资格（纯函数，只读，确定性）。

    优先级：显式 ``season_type``（若存在）→ ``postseason`` 布尔 → ``ist_stage`` →
    常规赛揭幕日边界（识别季前赛）→ 常规赛末日日期边界（识别 Play-In）→ 默认常规赛。

    - NBA Cup 小组赛与淘汰赛（除 Championship）**计入**常规赛战绩，予以保留。
    - NBA Cup Championship、Play-In、Playoffs **不计入**常规赛，排除但保留原始证据。
    - 未知 ``season_type`` / 无法识别的 ``ist_stage`` / 自相矛盾的分类一律拒绝（fail closed）。

    当前 BALLDONTLIE Games 响应**不含** ``season_type`` 字段（它只是查询参数），
    因此实际分类依赖 ``postseason`` + ``ist_stage`` + 常规赛末日日期边界。
    ``regular_season_end_date`` 为美方当地日期字符串（``YYYY-MM-DD``）；缺省时
    日期边界检测不可用，无标记比赛按默认常规赛处理（仅对早期赛季比赛成立）。
    """
    if not isinstance(raw, dict):
        return _classification(COMP_AMBIGUOUS, False, "malformed", "raw record is not a dict", reject=True)

    st = raw.get("season_type")
    st_norm = st.strip().lower() if isinstance(st, str) and st.strip() else None
    postseason = raw.get("postseason")
    ist = raw.get("ist_stage")
    ist_norm = ist.strip().lower() if isinstance(ist, str) and ist.strip() else None
    game_date = str(raw.get("date") or "")

    if st_norm is not None:
        if st_norm in ("playoffs", "playoff"):
            return _classification(COMP_PLAYOFF, False, "season_type",
                                   "season_type=playoffs is postseason", reject=False)
        if st_norm in ("playin", "play-in", "play_in"):
            return _classification(COMP_PLAY_IN, False, "season_type",
                                   "season_type=playin is the Play-In Tournament", reject=False)
        if st_norm == "preseason":
            return _classification(COMP_PRESEASON, False, "season_type",
                                   "season_type=preseason is an exhibition game", reject=False)
        if st_norm == "regular":
            if ist_norm and "championship" in ist_norm:
                return _classification(COMP_AMBIGUOUS, False, "season_type",
                                       "season_type=regular contradicts ist_stage=Championship", reject=True)
            if ist_norm:
                if _ist_is_regular_stage(ist_norm):
                    return _classification(COMP_NBA_CUP_REGULAR, True, "season_type",
                                           "season_type=regular with NBA Cup stage counts toward standings", reject=False)
                return _classification(COMP_AMBIGUOUS, False, "season_type",
                                       f"unrecognized ist_stage={ist_norm} with season_type=regular", reject=True)
            return _classification(COMP_NORMAL_REGULAR, True, "season_type",
                                   "season_type=regular standard game", reject=False)
        if st_norm != "ist":
            return _classification(COMP_UNSUPPORTED, False, "season_type",
                                   f"unsupported season_type={st_norm}", reject=True)
        # season_type=ist：继续走 ist_stage 判定。

    if postseason is True:
        return _classification(COMP_PLAYOFF, False, "postseason_flag",
                               "postseason=true is postseason", reject=False)

    if ist_norm:
        if "championship" in ist_norm:
            return _classification(COMP_NBA_CUP_CHAMPIONSHIP, False, "ist_stage",
                                   "NBA Cup Championship does not count toward standings", reject=False)
        if _ist_is_regular_stage(ist_norm):
            return _classification(COMP_NBA_CUP_REGULAR, True, "ist_stage",
                                   "NBA Cup group/knockout stage counts toward standings", reject=False)
        return _classification(COMP_AMBIGUOUS, False, "ist_stage",
                               f"unrecognized ist_stage={ist_norm}", reject=True)

    if regular_season_start_date and game_date and game_date < regular_season_start_date:
        return _classification(COMP_PRESEASON, False, "regular_season_date_boundary",
                               "game date before documented regular-season opening day with no IST/postseason marker => preseason",
                               reject=False)

    if regular_season_end_date and game_date and game_date > regular_season_end_date:
        return _classification(COMP_PLAY_IN, False, "regular_season_date_boundary",
                               "game date after documented regular-season final day with no IST/postseason marker => Play-In",
                               reject=False)

    return _classification(COMP_NORMAL_REGULAR, True, "default_regular",
                           "no postseason/IST marker and within the regular-season window", reject=False)


def _content_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_int(value: Any) -> bool:
    return type(value) is int


def parse_historical_games(
    payload: dict,
    *,
    source_reference: str = BALLDONTLIE_GAMES_URL,
    observed_at: datetime | None = None,
    regular_season_end_dates: dict[int, str] | None = None,
    regular_season_start_dates: dict[int, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """解析 BALLDONTLIE Games 载荷为**已确认完赛**的历史比赛观测。

    支持列表响应（``data`` 为 list）与单场响应（``data`` 为 dict）。
    严格保留主客方向（``home_team`` 为主、``visitor_team`` 为客）、UTC 开赛时间、
    提供商赛季约定与 30 支球队标识；按提供商 ``id`` 去重，内容冲突拒绝该 ID。
    每场附带 NBA 竞赛类型分类与常规赛资格；未知/矛盾分类拒绝该场（fail closed）。
    返回 (games, errors)；games 按 (scheduled_start_utc, provider_game_id) 确定性排序。
    """
    if not isinstance(payload, dict):
        return [], [REASON_MALFORMED_PAYLOAD]
    data = payload.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [], [REASON_MALFORMED_PAYLOAD]

    end_dates = dict(NBA_REGULAR_SEASON_END_DATES)
    if regular_season_end_dates:
        end_dates.update(regular_season_end_dates)
    start_dates = dict(NBA_REGULAR_SEASON_START_DATES)
    if regular_season_start_dates:
        start_dates.update(regular_season_start_dates)

    obs_iso = (observed_at or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ).isoformat()
    games: list[dict] = []
    errors: list[str] = []
    seen: dict[str, dict] = {}

    for raw in data:
        if not isinstance(raw, dict):
            errors.append(REASON_MALFORMED_GAME)
            continue
        gid = raw.get("id")
        if not _is_int(gid) or gid <= 0:
            errors.append(REASON_MISSING_GAME_ID)
            continue
        gid_str = str(gid)

        home = raw.get("home_team")
        away = raw.get("visitor_team")
        if not isinstance(home, dict) or not isinstance(away, dict):
            errors.append(f"{REASON_MISSING_COMPETITORS}:{gid_str}")
            continue
        h_id, a_id = home.get("id"), away.get("id")
        if not _is_int(h_id) or not _is_int(a_id) or h_id <= 0 or a_id <= 0:
            errors.append(f"{REASON_MISSING_COMPETITORS}:{gid_str}")
            continue
        h_code = resolve_provider_team_code(f"bdl:{h_id}")
        a_code = resolve_provider_team_code(f"bdl:{a_id}")
        if h_code is None or a_code is None:
            errors.append(f"{REASON_UNKNOWN_FRANCHISE}:{gid_str}")
            continue
        if h_code == a_code:
            errors.append(f"{REASON_SAME_TEAM}:{gid_str}")
            continue

        # 仅接受已确认完赛：规范生命周期字段 final、非延期、非取消。
        state = str(raw.get("status_state") or "").strip().lower()
        postponed = raw.get("postponed")
        if state != "final" or postponed is not False:
            errors.append(f"{REASON_NOT_FINAL}:{gid_str}")
            continue

        h_score, a_score = raw.get("home_team_score"), raw.get("visitor_team_score")
        if not _is_int(h_score) or not _is_int(a_score) or h_score < 0 or a_score < 0:
            errors.append(f"{REASON_MALFORMED_SCORE}:{gid_str}")
            continue
        period = raw.get("period")
        if not _is_int(period) or period < 4:
            errors.append(f"{REASON_MALFORMED_PERIOD}:{gid_str}")
            continue

        season = raw.get("season")
        if not _is_int(season) or not (_MIN_SEASON <= season <= _MAX_SEASON):
            errors.append(f"{REASON_MALFORMED_SEASON}:{gid_str}")
            continue

        raw_utc = raw.get("datetime")
        bj_date, bj_time, kickoff_dt, parse_status = convert_utc_to_beijing(raw_utc)
        if parse_status != "VALID_UTC" or kickoff_dt is None:
            errors.append(f"{REASON_INVALID_TIMESTAMP}:{gid_str}")
            continue

        postseason = raw.get("postseason")
        if type(postseason) is not bool:
            errors.append(f"{REASON_MALFORMED_GAME}:{gid_str}")
            continue

        game = {
            "provider": PROVIDER,
            "provider_game_id": gid_str,
            "nba_home_team_id": str(h_id),
            "nba_away_team_id": str(a_id),
            "home_code": h_code,
            "away_code": a_code,
            "home_team_name": str(home.get("full_name") or ""),
            "away_team_name": str(away.get("full_name") or ""),
            "season": season,
            "season_label": f"{season}-{str(season + 1)[-2:]}",
            "game_date": str(raw.get("date") or ""),
            "scheduled_start_utc": raw_utc.strip() if isinstance(raw_utc, str) else raw_utc,
            "kickoff_utc_iso": kickoff_dt.astimezone(BEIJING_TZ).isoformat(),
            "beijing_date": bj_date,
            "beijing_time": bj_time,
            "game_status": "final",
            "home_final_score": h_score,
            "away_final_score": a_score,
            "period": period,
            "overtime_periods": max(0, period - 4),
            "postseason_flag": postseason,
            "ist_stage": raw.get("ist_stage") if isinstance(raw.get("ist_stage"), str) else None,
            "season_type": raw.get("season_type") if isinstance(raw.get("season_type"), str) else None,
            "source_observed_at": obs_iso,
            "source_reference": source_reference,
            "source_content_digest": _content_digest(raw),
        }

        classification = classify_nba_competition(raw, regular_season_end_date=end_dates.get(season),
                                                  regular_season_start_date=start_dates.get(season))
        if classification["reject"]:
            code = (REASON_UNSUPPORTED_COMPETITION
                    if classification["competition_type"] == COMP_UNSUPPORTED
                    else REASON_AMBIGUOUS_CLASSIFICATION)
            errors.append(f"{code}:{gid_str}")
            continue
        game["competition_type"] = classification["competition_type"]
        game["regular_season_eligible"] = classification["counts_as_regular_season"]
        game["classification_source"] = classification["classification_source"]
        game["classification_reason"] = classification["classification_reason"]
        game["official_rule"] = classification["official_rule"]

        if gid_str in seen:
            if seen[gid_str]["source_content_digest"] != game["source_content_digest"]:
                errors.append(f"{REASON_CONFLICTING_DUPLICATE}:{gid_str}")
                games = [g for g in games if g["provider_game_id"] != gid_str]
                seen.pop(gid_str, None)
                continue
            continue
        seen[gid_str] = game
        games.append(game)

    games.sort(key=lambda g: (g["scheduled_start_utc"] or "", g["provider_game_id"]))
    return games, errors


def assemble_historical_dataset(
    games: Iterable[dict],
    *,
    requested_seasons: Iterable[int] | None = None,
    cutoff_utc: datetime | None = None,
    regular_season_only: bool = True,
) -> dict:
    """把已解析的历史比赛装配为确定性只读分析数据集。

    - 全局按 ``provider_game_id`` 去重；内容冲突的 ID 被剔除并计数。
    - 若提供 ``cutoff_utc``，剔除 ``scheduled_start_utc >= cutoff`` 的比赛（防未来泄漏）。
    - 按 NBA 竞赛类型分类区分常规赛资格；``regular_season_only=True``（默认）时
      ``games`` 仅含常规赛资格比赛，NBA Cup Championship / Play-In / Playoffs 被排除
      但**完整保留**在 ``excluded_competitions`` 中作为原始证据。
    - 赛季按提供商约定（``season`` 字段），不按日历年。
    - 绝不写入任何受保护存储；返回纯数据结构。
    """
    by_id: dict[str, dict] = {}
    conflicts: set[str] = set()
    post_cutoff = 0
    for g in games:
        if not isinstance(g, dict) or not g.get("provider_game_id"):
            continue
        if cutoff_utc is not None:
            kickoff = _parse_utc(g.get("scheduled_start_utc"))
            if kickoff is None or kickoff >= cutoff_utc:
                post_cutoff += 1
                continue
        gid = g["provider_game_id"]
        if gid in by_id:
            if by_id[gid].get("source_content_digest") != g.get("source_content_digest"):
                conflicts.add(gid)
                by_id.pop(gid, None)
            continue
        by_id[gid] = g

    ordered = sorted(by_id.values(), key=lambda g: (g["scheduled_start_utc"] or "", g["provider_game_id"]))
    eligible: list[dict] = []
    excluded: list[dict] = []
    breakdown: dict[str, int] = {}
    for g in ordered:
        ctype = g.get("competition_type", "UNCLASSIFIED")
        breakdown[ctype] = breakdown.get(ctype, 0) + 1
        flag = g.get("regular_season_eligible")
        is_eligible = (not g.get("postseason_flag", False)) if flag is None else bool(flag)
        (eligible if is_eligible else excluded).append(g)

    selected = eligible if regular_season_only else ordered
    seasons = sorted({g["season"] for g in selected})
    teams = sorted({g["home_code"] for g in selected} | {g["away_code"] for g in selected})
    playoff_count = sum(1 for g in ordered if g.get("postseason_flag"))
    dates = [g["game_date"] for g in selected if g.get("game_date")]
    return {
        "provider": PROVIDER,
        "dataset_kind": "NBA_HISTORICAL_COMPLETED_GAMES",
        "regular_season_only": regular_season_only,
        "game_count": len(selected),
        "regular_season_count": len(eligible),
        "postseason_count": playoff_count,
        "excluded_competition_count": len(excluded),
        "total_completed_games": len(ordered),
        "competition_breakdown": dict(sorted(breakdown.items())),
        "seasons": seasons,
        "season_labels": [f"{s}-{str(s + 1)[-2:]}" for s in seasons],
        "requested_seasons": sorted(requested_seasons) if requested_seasons else seasons,
        "distinct_team_count": len(teams),
        "teams_with_games": teams,
        "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
        "cutoff_utc": cutoff_utc.astimezone(BEIJING_TZ).isoformat() if cutoff_utc else None,
        "excluded_post_cutoff": post_cutoff,
        "conflicting_game_ids": sorted(conflicts),
        "games": selected,
        "excluded_competitions": excluded,
        "production_store_write": False,
        "canonical_market_modified": False,
    }


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(BEIJING_TZ)


def _not_attempted(reason: str, *, seasons, start_date, end_date, per_page) -> dict:
    return {
        "provider": PROVIDER,
        "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY,
        "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED,
        "status": STATUS_UNAVAILABLE,
        "coverage_status": reason,
        "reachable": False,
        "requested_seasons": list(seasons),
        "start_date": start_date,
        "end_date": end_date,
        "requested_page_size": per_page,
        "pages_fetched": 0,
        "provider_rows_observed": 0,
        "unique_games_observed": 0,
        "final_games_accepted": 0,
        "duplicates_deduplicated": 0,
        "conflicting_game_ids": [],
        "next_cursor_remaining": None,
        "pagination_complete": False,
        "pagination_failure_reason": reason,
        "network_requests_made": 0,
        "errors": [reason],
        "rate_limit_scope": "PROCESS_LOCAL",
        "global_account_limit_guaranteed": False,
    }


def _build_query(seasons, start_date, end_date, per_page, postseason, season_type=None) -> str:
    parts = [f"seasons[]={s}" for s in seasons]
    if start_date:
        parts.append(f"start_date={start_date}")
    if end_date:
        parts.append(f"end_date={end_date}")
    # postseason 不是 Games 端点的文档化参数；None 时不发送。
    if postseason is not None:
        parts.append(f"postseason={'true' if postseason else 'false'}")
    if season_type:
        parts.append(f"season_type={season_type}")
    parts.append(f"per_page={per_page}")
    return BALLDONTLIE_GAMES_URL + "?" + "&".join(parts)


def _paginate_games(base_url: str, *, api_key: str, timeout: float, max_requests: int, deadline: float) -> dict:
    """跟随 ``meta.next_cursor`` 直到明确结束；共享进程内频控；保留逐页证据。

    返回逐页载荷（含规范 JSON 摘要与接收时刻）、首次出现页、冲突 ID、失败原因与请求数。
    ``complete`` 只在游标明确结束且无任何失败时为真。
    """
    pages: list[dict] = []
    raw_by_id: dict[str, dict] = {}
    first_page: dict[str, int] = {}
    conflicts: set[str] = set()
    cursor, seen_cursors = None, set()
    observed = duplicates = attempts = 0
    failure = None
    complete = False
    received_at: datetime | None = None

    while not complete and failure is None:
        url = base_url + (f"&cursor={cursor}" if cursor is not None else "")
        response = None
        for retry in range(3):
            if not _ns._BDL_REQUEST_LOCK.acquire(timeout=max(0.0, deadline - time.monotonic())):
                failure = "TIME_BUDGET_EXHAUSTED"
                break
            try:
                current = time.monotonic()
                ready = max(current, _ns._BDL_RETRY_NOT_BEFORE,
                            (_ns._LAST_BDL_REQUEST_MONO + _ns.BDL_PACING_INTERVAL)
                            if _ns._LAST_BDL_REQUEST_MONO is not None else current)
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
                _ns._LAST_BDL_REQUEST_MONO = time.monotonic()
                try:
                    response = requests.get(url, headers={"Authorization": api_key},
                                            timeout=min(timeout, remaining))
                    received_at = datetime.now(BEIJING_TZ)
                except Exception:
                    failure = "REQUEST_FAILED"
                    break
                if response.status_code == 429:
                    delay = max(min(2 ** retry, 60), _ns._retry_after_seconds(
                        getattr(response, "headers", {}).get("Retry-After"), received_at))
                    _ns._BDL_RETRY_NOT_BEFORE = time.monotonic() + delay
            finally:
                _ns._BDL_REQUEST_LOCK.release()
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
        meta = payload.get("meta")
        page_index = len(pages)
        pages.append({
            "page_index": page_index,
            "url": url,
            "request_cursor": cursor,
            "status_code": response.status_code,
            "received_at": received_at.isoformat(),
            "row_count": len(payload["data"]),
            "next_cursor": meta.get("next_cursor") if isinstance(meta, dict) else None,
            "payload_sha256": _content_digest(payload),
            "payload": payload,
        })
        observed += len(payload["data"])
        for raw in payload["data"]:
            if not isinstance(raw, dict) or not _is_int(raw.get("id")) or raw["id"] <= 0:
                failure = "MALFORMED_GAME_ID"
                continue
            gid = str(raw["id"])
            if gid in raw_by_id:
                if raw_by_id[gid] != raw:
                    conflicts.add(gid)
                    failure = "CONFLICTING_PROVIDER_GAME_ID"
                else:
                    duplicates += 1
            else:
                raw_by_id[gid] = raw
                first_page[gid] = page_index
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

    return {
        "pages": pages,
        "raw_by_id": raw_by_id,
        "first_page": first_page,
        "conflicts": conflicts,
        "observed": observed,
        "duplicates": duplicates,
        "attempts": attempts,
        "failure": failure,
        "complete": complete and failure is None,
        "next_cursor_remaining": cursor,
        "received_at": received_at,
    }


def _page_evidence(pages: list[dict]) -> list[dict]:
    return [{k: v for k, v in p.items() if k != "payload"} for p in pages]


def fetch_historical_games(
    *,
    seasons: Iterable[int],
    start_date: str | None = None,
    end_date: str | None = None,
    postseason: bool | None = False,
    season_type: str | None = None,
    allow_network: bool = False,
    api_key: str | None = None,
    timeout: float = 5.0,
    per_page: int = 100,
    max_requests: int = 10,
    max_elapsed_seconds: float = 120.0,
    cutoff_utc: datetime | None = None,
    now: datetime | None = None,
    regular_season_end_dates: dict[int, str] | None = None,
) -> tuple[list[dict], dict]:
    """有界、默认拒绝网络的 BALLDONTLIE 历史完赛获取。

    复用 ``nba_schedule`` 的进程内共享频控锁与间隔（同一账户配额跨赛程/历史调用共享）。
    分页跟随 ``meta.next_cursor`` 直到明确结束；后页失败/冲突/预算耗尽一律
    ``PAGINATION_INCOMPLETE`` 且**返回零条可用比赛**。绝不回退 ESPN 或付费端点。
    """
    seasons = sorted({int(s) for s in seasons})
    if not seasons:
        return [], _not_attempted("EMPTY_SEASON_WINDOW", seasons=seasons, start_date=start_date,
                                  end_date=end_date, per_page=per_page)
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be an integer in [1, 100]")
    if type(max_requests) is not int or max_requests < 1:
        raise ValueError("max_requests must be a positive integer")

    now_bj = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    effective_key = api_key or os.environ.get(ENV_BDL_API_KEY)

    if not allow_network:
        diag = _not_attempted("NETWORK_DISABLED", seasons=seasons, start_date=start_date,
                              end_date=end_date, per_page=per_page)
        diag["status"] = STATUS_DISABLED_AUTH
        diag["provider_authorization_status"] = "API_KEY_CONFIGURED" if effective_key else "PROVIDER_CREDENTIALS_MISSING"
        diag["automated_provider_operation"] = _ns.AUTOMATED_OPERATION_DISABLED
        diag["errors"] = ["PROVIDER_AUTOMATED_USE_UNAUTHORIZED"]
        diag["checked_at"] = now_bj.isoformat()
        return [], diag

    if not effective_key:
        diag = _not_attempted("PROVIDER_CREDENTIALS_MISSING", seasons=seasons, start_date=start_date,
                              end_date=end_date, per_page=per_page)
        diag["status"] = STATUS_DISABLED_CREDS
        diag["provider_authorization_status"] = "PROVIDER_CREDENTIALS_MISSING"
        diag["automated_provider_operation"] = _ns.AUTOMATED_OPERATION_DISABLED
        diag["checked_at"] = now_bj.isoformat()
        return [], diag

    base_url = _build_query(seasons, start_date, end_date, per_page, postseason, season_type)
    started_at = now_bj
    run = _paginate_games(base_url, api_key=effective_key, timeout=timeout, max_requests=max_requests,
                          deadline=time.monotonic() + max_elapsed_seconds)
    received_at = run["received_at"] or started_at

    errors: list[str] = []
    by_id: dict[str, dict] = {}
    for page in run["pages"]:
        rows, parse_errors = parse_historical_games(
            page["payload"], source_reference=page["url"],
            observed_at=datetime.fromisoformat(page["received_at"]),
            regular_season_end_dates=regular_season_end_dates)
        errors.extend(parse_errors)
        for r in rows:
            gid = r["provider_game_id"]
            if gid not in run["conflicts"] and run["first_page"].get(gid) == page["page_index"]:
                by_id[gid] = r

    failure = run["failure"]
    if failure:
        errors.append(failure)
    complete = run["complete"]
    games = sorted(by_id.values(), key=lambda g: (g["scheduled_start_utc"] or "", g["provider_game_id"])) if complete else []
    if complete and cutoff_utc is not None:
        games = [g for g in games if (_parse_utc(g["scheduled_start_utc"]) or cutoff_utc) < cutoff_utc]

    diag = {
        "provider": PROVIDER,
        "provider_type": PROVIDER_TYPE_DOCUMENTED_THIRD_PARTY,
        "provider_endpoint_type": PROVIDER_TYPE_DOCUMENTED_AUTHORIZED,
        "provider_authorization_status": "API_KEY_CONFIGURED",
        "automated_provider_operation": _ns.AUTOMATED_OPERATION_ENABLED,
        "status": (STATUS_OK if games else STATUS_EMPTY) if complete else STATUS_UNAVAILABLE,
        "coverage_status": "COMPLETE_SEASON_FETCH" if complete else "PAGINATION_INCOMPLETE",
        "reachable": len(run["pages"]) > 0,
        "request_started_at": started_at.isoformat(),
        "response_received_at": received_at.isoformat(),
        "checked_at": received_at.isoformat(),
        "observation_origin": "NETWORK",
        "cache_hit": False,
        "requested_seasons": seasons,
        "start_date": start_date,
        "end_date": end_date,
        "postseason": postseason,
        "season_type": season_type,
        "requested_page_size": per_page,
        "pages_fetched": len(run["pages"]),
        "provider_rows_observed": run["observed"],
        "unique_games_observed": len(run["raw_by_id"]),
        "final_games_accepted": len(games),
        "duplicates_deduplicated": run["duplicates"],
        "conflicting_game_ids": sorted(run["conflicts"]),
        "next_cursor_remaining": run["next_cursor_remaining"],
        "pagination_complete": complete,
        "pagination_failure_reason": failure,
        "network_requests_made": run["attempts"],
        "max_requests": max_requests,
        "max_elapsed_seconds": max_elapsed_seconds,
        "page_evidence": _page_evidence(run["pages"]),
        "errors": errors,
        "rate_limit_scope": "PROCESS_LOCAL",
        "global_account_limit_guaranteed": False,
    }
    return games, diag


# ---------------------------------------------------------------------------
# 当前赛季历史窗口：有界日期分区规划、共享预算获取与认证对账
# ---------------------------------------------------------------------------

WINDOW_VERIFIED = "VERIFIED"
WINDOW_UNRESOLVED = "UNRESOLVED"

PARTITION_ALL_NON_PRESEASON = "ALL_NON_PRESEASON_TYPES"
PARTITION_IST_CROSSCHECK = "NBA_CUP_IST_CROSSCHECK"

_MATERIAL_FIELDS = ("nba_home_team_id", "nba_away_team_id", "home_final_score", "away_final_score",
                    "scheduled_start_utc", "season", "period", "postseason_flag", "ist_stage")


def plan_history_window(
    *,
    season: int,
    cutoff: datetime,
    season_start_date: str | None = None,
    include_ist_crosscheck: bool = True,
    per_page: int = 100,
    max_requests: int = 10,
    max_elapsed_seconds: float = 120.0,
    expected_primary_games: int | None = None,
) -> dict:
    """规划覆盖「开赛早于 cutoff 的全部比赛」的有界日期分区（纯函数，不联网）。

    - 主分区**省略** ``season_type``：提供商文档规定省略时返回全部非季前赛类型，
      因此 NBA Cup 小组/淘汰赛不会因 ``season_type=regular`` 的未知语义被静默漏掉；
      Championship / Play-In / Playoffs 由竞赛分类器排除。不发送未文档化的 ``postseason``。
    - 可选 ``season_type=ist`` 交叉核验分区只用于证明主分区未漏 NBA Cup 比赛，
      其结果按 ID 去重、**不**作为额外来源叠加计数。
    - 日期过滤时区语义未文档化：起始日取揭幕日前一日，结束日取 cutoff 在 UTC 与北京
      时区下的较晚日期（美方当地日期不晚于 UTC 日期），再按 ``scheduled_start_utc < cutoff``
      严格过滤，任何语义下都不会漏掉零点附近的比赛。
    - 请求预算含重试与交叉核验分区，在所有分区间共享。
    """
    if cutoff.tzinfo is None:
        raise ValueError("cutoff must be timezone-aware")
    if type(per_page) is not int or not 1 <= per_page <= 100:
        raise ValueError("per_page must be an integer in [1, 100]")
    start = season_start_date or NBA_REGULAR_SEASON_START_DATES.get(season)
    if not start:
        raise ValueError(f"no documented regular-season start date for season {season}")
    start_date = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    cutoff_utc = cutoff.astimezone(timezone.utc)
    end_date = max(cutoff_utc.date(), cutoff.astimezone(BEIJING_TZ).date()).isoformat()

    partitions = [{
        "partition_id": PARTITION_ALL_NON_PRESEASON,
        "role": "PRIMARY_COVERAGE",
        "season_type": None,
        "coverage_semantics": "season_type omitted: provider documents that all non-preseason game types are returned",
        "url": _build_query([season], start_date, end_date, per_page, None, None),
    }]
    if include_ist_crosscheck:
        partitions.append({
            "partition_id": PARTITION_IST_CROSSCHECK,
            "role": "SUBSET_CROSSCHECK",
            "season_type": "ist",
            "coverage_semantics": "NBA Cup games only; every returned ID must already appear in the primary partition",
            "url": _build_query([season], start_date, end_date, per_page, None, "ist"),
        })

    def _pages_for(n: int | None) -> int:
        # 满页可能带回游标而下一页为空，因此整除时多算一页。
        return 1 if not n else n // per_page + 1

    estimated = _pages_for(expected_primary_games) + (1 if include_ist_crosscheck else 0)
    min_elapsed = max(0, estimated - 1) * _ns.BDL_PACING_INTERVAL
    plan = {
        "provider": "BALLDONTLIE",
        "provider_id": PROVIDER,
        "season": season,
        "season_label": f"{season}-{str(season + 1)[-2:]}",
        "historical_cutoff": cutoff.isoformat(),
        "historical_cutoff_utc": cutoff_utc.isoformat(),
        "regular_season_start_date": start,
        "start_date": start_date,
        "end_date": end_date,
        "season_type_coverage": [p["partition_id"] + ":" + (p["season_type"] or "omitted") for p in partitions],
        "partitions": partitions,
        "max_requests": max_requests,
        "per_page": per_page,
        "max_elapsed_seconds": max_elapsed_seconds,
        "pacing_interval": _ns.BDL_PACING_INTERVAL,
        "estimated_requests_without_retries": estimated,
        "retry_headroom": max_requests - estimated,
        "minimum_elapsed_seconds": round(min_elapsed, 1),
        "budget_feasible": estimated <= max_requests and min_elapsed <= max_elapsed_seconds,
        "operation": "READ_ONLY_HISTORICAL_COLLECTION",
        "real_production_writes": "PROHIBITED",
    }
    plan["plan_sha256"] = _content_digest(plan)
    return plan


def fetch_history_window(
    plan: dict,
    *,
    allow_network: bool = False,
    api_key: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """按已批准的规划逐分区获取（默认拒绝网络）；请求与耗时预算在分区间共享。

    任一分区失败后不再发起后续分区请求；失败分区绝不标记为完整。
    """
    base = {"plan_sha256": plan.get("plan_sha256"), "partitions": [], "network_requests_made": 0,
            "rate_limit_scope": "PROCESS_LOCAL", "global_account_limit_guaranteed": False}
    effective_key = api_key or os.environ.get(ENV_BDL_API_KEY)
    if not allow_network:
        return dict(base, status=STATUS_DISABLED_AUTH, errors=["PROVIDER_AUTOMATED_USE_UNAUTHORIZED"])
    if not effective_key:
        return dict(base, status=STATUS_DISABLED_CREDS, errors=["PROVIDER_CREDENTIALS_MISSING"])

    deadline = time.monotonic() + float(plan["max_elapsed_seconds"])
    remaining = int(plan["max_requests"])
    results: list[dict] = []
    halted = None
    for part in plan["partitions"]:
        if halted is not None:
            results.append({"partition_id": part["partition_id"], "season_type": part["season_type"],
                            "complete": False, "failure": "NOT_ATTEMPTED_AFTER_" + halted, "attempts": 0,
                            "pages": [], "conflicting_game_ids": [], "next_cursor_remaining": None})
            continue
        if remaining <= 0:
            halted = "REQUEST_BUDGET_EXHAUSTED"
            results.append({"partition_id": part["partition_id"], "season_type": part["season_type"],
                            "complete": False, "failure": halted, "attempts": 0, "pages": [],
                            "conflicting_game_ids": [], "next_cursor_remaining": None})
            continue
        run = _paginate_games(part["url"], api_key=effective_key, timeout=timeout,
                              max_requests=remaining, deadline=deadline)
        remaining -= run["attempts"]
        results.append({
            "partition_id": part["partition_id"],
            "season_type": part["season_type"],
            "complete": run["complete"],
            "failure": run["failure"],
            "attempts": run["attempts"],
            "pages": run["pages"],
            "unique_games_observed": len(run["raw_by_id"]),
            "duplicates_deduplicated": run["duplicates"],
            "conflicting_game_ids": sorted(run["conflicts"]),
            "next_cursor_remaining": run["next_cursor_remaining"],
        })
        if not run["complete"]:
            halted = run["failure"] or "PAGINATION_INCOMPLETE"
    total = sum(r["attempts"] for r in results)
    ok = all(r["complete"] for r in results) and len(results) == len(plan["partitions"])
    return dict(base, status=STATUS_OK if ok else STATUS_UNAVAILABLE, partitions=results,
                network_requests_made=total, errors=[r["failure"] for r in results if r["failure"]])


def _material(game: dict) -> dict:
    return {k: game.get(k) for k in _MATERIAL_FIELDS}


def certify_history_window(plan: dict, fetched: dict, *, previous_games: Iterable[dict] | None = None) -> dict:
    """认证当前赛季历史窗口并与既有捕获对账（纯函数，只读，不联网）。

    只有全部分区游标耗尽、截止前比赛全部确认完赛、无冲突、NBA Cup 交叉核验无缺口、
    既有捕获全部被重新确认时才给出 ``VERIFIED``；否则 ``UNRESOLVED`` 并列出原因。
    冲突证据并列保留，绝不覆盖。
    """
    season = plan["season"]
    cutoff = _parse_utc(plan["historical_cutoff_utc"])
    reasons: list[str] = []
    parts = {p["partition_id"]: p for p in fetched.get("partitions", [])}
    for planned in plan["partitions"]:
        got = parts.get(planned["partition_id"])
        if got is None or not got.get("complete"):
            reasons.append(f"PARTITION_INCOMPLETE:{planned['partition_id']}")
        elif got.get("conflicting_game_ids"):
            reasons.append(f"CONFLICTING_PROVIDER_GAME_ID:{planned['partition_id']}")
    if fetched.get("plan_sha256") != plan.get("plan_sha256"):
        reasons.append("PLAN_DIGEST_MISMATCH")

    primary = parts.get(PARTITION_ALL_NON_PRESEASON) or {"pages": []}
    primary_raw: dict[str, dict] = {}
    for page in primary["pages"]:
        for raw in page["payload"].get("data", []):
            if isinstance(raw, dict) and _is_int(raw.get("id")):
                primary_raw.setdefault(str(raw["id"]), raw)

    post_cutoff = 0
    postponed: list[str] = []
    not_final: list[str] = []
    pre_cutoff_ids: set[str] = set()
    for gid, raw in primary_raw.items():
        if raw.get("season") != season:
            reasons.append(f"UNEXPECTED_SEASON:{gid}")
            continue
        kickoff = _parse_utc(raw.get("datetime"))
        if kickoff is None:
            reasons.append(f"INVALID_EVENT_TIMESTAMP:{gid}")
            continue
        if kickoff >= cutoff:
            post_cutoff += 1
            continue
        pre_cutoff_ids.add(gid)
        state = str(raw.get("status_state") or "").strip().lower()
        if raw.get("postponed") is True or state in ("postponed", "canceled"):
            postponed.append(gid)
        elif state != "final":
            not_final.append(gid)
    if not_final:
        reasons.append("PRE_CUTOFF_GAME_NOT_FINAL")

    games: list[dict] = []
    for page in primary["pages"]:
        rows, errs = parse_historical_games(page["payload"], source_reference=page["url"],
                                            observed_at=datetime.fromisoformat(page["received_at"]))
        for e in errs:
            gid = e.rsplit(":", 1)[-1]
            if gid in pre_cutoff_ids and gid not in postponed and not e.startswith(REASON_NOT_FINAL):
                reasons.append(f"PRE_CUTOFF_PARSE_REJECTED:{e}")
        games.extend(r for r in rows if r["provider_game_id"] in pre_cutoff_ids)
    dataset = assemble_historical_dataset(games, requested_seasons=[season], cutoff_utc=cutoff)
    if dataset["conflicting_game_ids"]:
        reasons.append("DATASET_CONFLICTING_GAME_IDS")

    crosscheck = None
    ist = parts.get(PARTITION_IST_CROSSCHECK)
    if ist is not None:
        ist_raw: dict[str, dict] = {}
        for page in ist["pages"]:
            for raw in page["payload"].get("data", []):
                if isinstance(raw, dict) and _is_int(raw.get("id")):
                    ist_raw.setdefault(str(raw["id"]), raw)
        missing = sorted(set(ist_raw) - set(primary_raw))
        mismatched = sorted(g for g in set(ist_raw) & set(primary_raw) if ist_raw[g] != primary_raw[g])
        if missing:
            reasons.append("NBA_CUP_SCOPE_GAP")
        if mismatched:
            reasons.append("OVERLAPPING_CATEGORY_CONFLICT")
        crosscheck = {
            "ist_ids_observed": len(ist_raw),
            "overlap_deduplicated": len(set(ist_raw) & set(primary_raw)),
            "ist_ids_missing_from_primary": missing,
            "ist_ids_content_mismatch": mismatched,
            "counted_once": True,
        }

    certified = {g["provider_game_id"]: g for g in dataset["games"] + dataset["excluded_competitions"]}
    reconciliation = None
    if previous_games is not None:
        old = {}
        for g in previous_games:
            if not isinstance(g, dict) or g.get("season") != season:
                continue
            kickoff = _parse_utc(g.get("scheduled_start_utc"))
            if kickoff is not None and kickoff < cutoff:
                old[str(g["provider_game_id"])] = g
        old_ids, new_ids = set(old), set(certified)
        conflicting = sorted(g for g in old_ids & new_ids if _material(old[g]) != _material(certified[g]))
        scores_changed = sorted(
            g for g in old_ids & new_ids
            if (old[g].get("home_final_score"), old[g].get("away_final_score"))
            != (certified[g].get("home_final_score"), certified[g].get("away_final_score")))
        missing_old = sorted(old_ids - new_ids)
        if missing_old:
            reasons.append("PREVIOUS_EVIDENCE_NOT_RECONFIRMED")
        if conflicting:
            reasons.append("PREVIOUS_EVIDENCE_CONFLICT")
        reconciliation = {
            "old_provider_game_ids": sorted(old_ids),
            "new_provider_game_ids": sorted(new_ids),
            "missing_old_ids": missing_old,
            "newly_discovered_ids": sorted(new_ids - old_ids),
            "conflicting_ids": conflicting,
            "conflicting_records": [{"provider_game_id": g, "previous": _material(old[g]),
                                     "certified": _material(certified[g])} for g in conflicting],
            "existing_scores_changed": scores_changed,
            "content_digest_changed_ids": sorted(
                g for g in old_ids & new_ids
                if old[g].get("source_content_digest") != certified[g].get("source_content_digest")),
            "new_regular_season_games": sorted(
                g for g in new_ids - old_ids if certified[g].get("regular_season_eligible")),
        }

    complete = not reasons
    return {
        "season": season,
        "season_label": plan.get("season_label"),
        "historical_cutoff": plan["historical_cutoff"],
        "plan_sha256": plan.get("plan_sha256"),
        "CURRENT_SEASON_WINDOW_COMPLETE": WINDOW_VERIFIED if complete else WINDOW_UNRESOLVED,
        "window_complete": complete,
        "reasons": sorted(set(reasons)),
        "page_completion_evidence": [
            {"partition_id": p["partition_id"], "complete": p.get("complete"), "failure": p.get("failure"),
             "next_cursor_remaining": p.get("next_cursor_remaining"),
             "pages": _page_evidence(p.get("pages", []))}
            for p in fetched.get("partitions", [])],
        "primary_unique_games": len(primary_raw),
        "pre_cutoff_games": len(pre_cutoff_ids),
        "post_cutoff_games_excluded": post_cutoff,
        "pre_cutoff_postponed_ids": sorted(postponed),
        "pre_cutoff_not_final_ids": sorted(not_final),
        "regular_season_games": dataset["regular_season_count"],
        "excluded_competition_count": dataset["excluded_competition_count"],
        "competition_breakdown": dataset["competition_breakdown"],
        "ist_crosscheck": crosscheck,
        "reconciliation": reconciliation,
        "dataset": dataset,
        "production_store_write": False,
    }
