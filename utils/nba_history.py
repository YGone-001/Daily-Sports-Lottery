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
from datetime import datetime, timedelta
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

# 数据集 / 获取门禁状态
STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED_AUTH = "disabled_pending_authorization"
STATUS_DISABLED_CREDS = "disabled_missing_credentials"

_MIN_SEASON = 1946
_MAX_SEASON = 2100


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
) -> tuple[list[dict], list[str]]:
    """解析 BALLDONTLIE Games 载荷为**已确认完赛**的历史比赛观测。

    支持列表响应（``data`` 为 list）与单场响应（``data`` 为 dict）。
    严格保留主客方向（``home_team`` 为主、``visitor_team`` 为客）、UTC 开赛时间、
    提供商赛季约定与 30 支球队标识；按提供商 ``id`` 去重，内容冲突拒绝该 ID。
    返回 (games, errors)；games 按 (scheduled_start_utc, provider_game_id) 确定性排序。
    """
    if not isinstance(payload, dict):
        return [], [REASON_MALFORMED_PAYLOAD]
    data = payload.get("data")
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return [], [REASON_MALFORMED_PAYLOAD]

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
            "source_observed_at": obs_iso,
            "source_reference": source_reference,
            "source_content_digest": _content_digest(raw),
        }

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
) -> dict:
    """把已解析的历史比赛装配为确定性只读分析数据集。

    - 全局按 ``provider_game_id`` 去重；内容冲突的 ID 被剔除并计数。
    - 若提供 ``cutoff_utc``，剔除 ``scheduled_start_utc >= cutoff`` 的比赛（防未来泄漏）。
    - 区分常规赛与季后赛；赛季按提供商约定（``season`` 字段），不按日历年。
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
    seasons = sorted({g["season"] for g in ordered})
    teams = sorted({g["home_code"] for g in ordered} | {g["away_code"] for g in ordered})
    regular = [g for g in ordered if not g["postseason_flag"]]
    playoff = [g for g in ordered if g["postseason_flag"]]
    dates = [g["game_date"] for g in ordered if g.get("game_date")]
    return {
        "provider": PROVIDER,
        "dataset_kind": "NBA_HISTORICAL_COMPLETED_GAMES",
        "game_count": len(ordered),
        "regular_season_count": len(regular),
        "postseason_count": len(playoff),
        "seasons": seasons,
        "season_labels": [f"{s}-{str(s + 1)[-2:]}" for s in seasons],
        "requested_seasons": sorted(requested_seasons) if requested_seasons else seasons,
        "distinct_team_count": len(teams),
        "teams_with_games": teams,
        "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
        "cutoff_utc": cutoff_utc.astimezone(BEIJING_TZ).isoformat() if cutoff_utc else None,
        "excluded_post_cutoff": post_cutoff,
        "conflicting_game_ids": sorted(conflicts),
        "games": ordered,
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


def _build_query(seasons, start_date, end_date, per_page, postseason) -> str:
    parts = [f"seasons[]={s}" for s in seasons]
    if start_date:
        parts.append(f"start_date={start_date}")
    if end_date:
        parts.append(f"end_date={end_date}")
    parts.append(f"postseason={'true' if postseason else 'false'}")
    parts.append(f"per_page={per_page}")
    return BALLDONTLIE_GAMES_URL + "?" + "&".join(parts)


def fetch_historical_games(
    *,
    seasons: Iterable[int],
    start_date: str | None = None,
    end_date: str | None = None,
    postseason: bool = False,
    allow_network: bool = False,
    api_key: str | None = None,
    timeout: float = 5.0,
    per_page: int = 100,
    max_requests: int = 10,
    max_elapsed_seconds: float = 120.0,
    cutoff_utc: datetime | None = None,
    now: datetime | None = None,
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

    base_url = _build_query(seasons, start_date, end_date, per_page, postseason)
    started_mono = time.monotonic()
    deadline = started_mono + max_elapsed_seconds
    started_at = now_bj
    received_at = started_at

    by_id: dict[str, dict] = {}
    raw_by_id: dict[str, dict] = {}
    conflicts: set[str] = set()
    cursor, seen_cursors = None, set()
    pages = observed = duplicates = attempts = 0
    errors: list[str] = []
    failure = None
    complete = False

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
                    response = requests.get(url, headers={"Authorization": effective_key},
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
        pages += 1
        observed += len(payload["data"])
        rows, parse_errors = parse_historical_games(payload, source_reference=url, observed_at=received_at)
        if parse_errors:
            errors.extend(parse_errors)
        row_map = {r["provider_game_id"]: r for r in rows}
        for raw in payload["data"]:
            if not isinstance(raw, dict) or not _is_int(raw.get("id")) or raw["id"] <= 0:
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
        "reachable": pages > 0,
        "request_started_at": started_at.isoformat(),
        "response_received_at": received_at.isoformat(),
        "checked_at": received_at.isoformat(),
        "observation_origin": "NETWORK",
        "cache_hit": False,
        "requested_seasons": seasons,
        "start_date": start_date,
        "end_date": end_date,
        "postseason": postseason,
        "requested_page_size": per_page,
        "pages_fetched": pages,
        "provider_rows_observed": observed,
        "unique_games_observed": len(raw_by_id),
        "final_games_accepted": len(games),
        "duplicates_deduplicated": duplicates,
        "conflicting_game_ids": sorted(conflicts),
        "next_cursor_remaining": cursor,
        "pagination_complete": complete,
        "pagination_failure_reason": failure,
        "network_requests_made": attempts,
        "max_requests": max_requests,
        "max_elapsed_seconds": max_elapsed_seconds,
        "errors": errors,
        "rate_limit_scope": "PROCESS_LOCAL",
        "global_account_limit_guaranteed": False,
    }
    return games, diag