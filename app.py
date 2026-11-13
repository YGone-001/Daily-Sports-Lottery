"""
每日实时体彩预测系统
====================
Flask 应用入口。

页面：
  /            今日赛事预测（足球 + 篮球）
  /match/<id>  单场深度分析
  /history     历史赛果与复盘
  /strategy    智能策略舱（价值投注 + 串关）

API：
  /api/today                    今日赛事
  /api/matches?date=&sport=     按日期/类别查询
  /api/match/<id>               单场详情
  /api/match/<id>/snapshots     该场赛前预测快照（只读）
  /api/match/<id>/odds-history  该场赛前赔率历史（只读）
  /api/match/<id>/settlements   该场结算记录（只读）
  /api/dates                    可用日期
  /api/strategy                 策略推荐
  /api/refresh                  手动触发抓取
  /api/status                   系统状态
  /api/history                  历史复盘的不可变证据视图（只读）
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

import config
from models.predictor import predict_match
from models.strategy import generate_accumulators
from utils import daily_loader, scraper
from utils.daily_loader import (
    enrich_match,
    get_available_dates,
    get_beijing_now,
    get_daily_stats,
    get_default_date,
    get_leagues,
    get_match_by_id,
    get_matches_by_date,
    get_matches_grouped,
    get_meta,
)
from utils.history_evidence import build_history_view_data
from utils.odds_snapshots import get_odds_history_for_match
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match

app = Flask(__name__)
app.config.from_object(config)

_PROJECT_ROOT = Path(__file__).resolve().parent
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "dist"


def _serve_frontend() -> bool:
    """Whether this process should serve the compiled React application."""
    return (
        os.environ.get("SERVE_FRONTEND") == "1"
        and (_FRONTEND_DIST / "index.html").is_file()
    )


def _frontend_index():
    return send_from_directory(_FRONTEND_DIST, "index.html")


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if _serve_frontend():
        return _frontend_index()

    date = request.args.get("date")
    sport = request.args.get("sport")  # football / basketball / None(全部)
    if sport == "all":
        sport = None

    # 默认显示「可投注的比赛日」：优先逻辑今天，其次最近有未完赛赛事的日期
    display_date = date or get_default_date()

    groups = get_matches_grouped(display_date, sport)
    # 为列表中已生成预测的未开赛比赛固化赛前快照（幂等，不覆盖既有记录）
    for group in groups:
        for m in group["matches"]:
            ensure_snapshot(m, m.get("prediction"))
    stats = get_daily_stats(display_date, sport)
    dates = get_available_dates()

    return render_template(
        "index.html",
        groups=groups,
        stats=stats,
        dates=dates,
        display_date=display_date,
        current_sport=sport or "all",
        meta=get_meta(),
        active_page="index",
    )


@app.route("/match/<match_id>")
def match_detail(match_id):
    if _serve_frontend():
        return _frontend_index()

    raw = get_match_by_id(match_id)
    if not raw:
        return render_template("match.html", match=None, active_page="index"), 404

    m = enrich_match(raw)
    pred = predict_match(
        m["home_team"],
        m["away_team"],
        odds=m.get("odds"),
        sport=m.get("sport", "football"),
        league=m.get("league", ""),
    )
    m["prediction"] = pred
    # 未开赛比赛：固化赛前快照（已存在则原样保留）
    ensure_snapshot(m, pred)
    return render_template("match.html", match=m, active_page="index")


@app.route("/history")
def history():
    if _serve_frontend():
        return _frontend_index()

    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    matches = get_matches_by_date("all", sport)
    view_data = build_history_view_data(matches, sport=sport)

    return render_template(
        "history.html",
        matches=view_data["matches"],
        total=view_data["total_finished"],
        stats=view_data["stats"],
        current_sport=sport or "all",
        active_page="history",
    )


@app.route("/strategy")
def strategy():
    if _serve_frontend():
        return _frontend_index()

    return render_template("strategy.html", active_page="strategy")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/assets/<path:filename>")
def frontend_assets(filename):
    """Serve hashed build assets only when the compiled frontend is enabled."""
    if not _serve_frontend():
        return "Not Found", 404
    return send_from_directory(_FRONTEND_DIST / "assets", filename)

@app.route("/api/today")
def api_today():
    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    matches = get_matches_by_date(None, sport)
    out = []
    for raw in matches:
        m = enrich_match(raw)
        m["prediction"] = predict_match(
            m["home_team"], m["away_team"],
            odds=m.get("odds"), sport=m.get("sport", "football"), league=m.get("league", ""),
        )
        ensure_snapshot(m, m["prediction"])
        out.append(m)
    return jsonify(out)


@app.route("/api/matches")
def api_matches():
    date = request.args.get("date")
    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    matches = get_matches_by_date(date, sport)
    out = []
    for raw in matches:
        m = enrich_match(raw)
        m["prediction"] = predict_match(
            m["home_team"], m["away_team"],
            odds=m.get("odds"), sport=m.get("sport", "football"), league=m.get("league", ""),
        )
        ensure_snapshot(m, m["prediction"])
        out.append(m)
    return jsonify(out)


@app.route("/api/match/<match_id>")
def api_match(match_id):
    raw = get_match_by_id(match_id)
    if not raw:
        return jsonify({"error": "Match not found"}), 404
    m = enrich_match(raw)
    m["prediction"] = predict_match(
        m["home_team"], m["away_team"],
        odds=m.get("odds"), sport=m.get("sport", "football"), league=m.get("league", ""),
    )
    ensure_snapshot(m, m["prediction"])
    return jsonify(m)


@app.route("/api/match/<match_id>/snapshots")
def api_match_snapshots(match_id):
    """只读：返回该场已固化的全部赛前预测快照（无记录时返回空列表）。"""
    return jsonify(get_snapshots_for_match(match_id))


@app.route("/api/match/<match_id>/odds-history")
def api_match_odds_history(match_id):
    """
    只读：返回该场已捕获的赛前赔率历史，按 captured_at 升序（最早在前）。
    无记录时返回空列表；本接口不创建、不重算、不改写任何历史。
    """
    return jsonify(get_odds_history_for_match(match_id))


@app.route("/api/match/<match_id>/settlements")
def api_match_settlements(match_id):
    """
    只读：返回该场全部结算记录（按生成时间 / 模型版本 / 结算 ID 确定性排序）。
    无记录时返回空列表；本接口不生成预测、不创建结算、不改写任何记录。
    """
    return jsonify(get_settlements_for_match(match_id))


@app.route("/api/dates")
def api_dates():
    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    leagues = {
        "football": get_leagues("football"),
        "basketball": get_leagues("basketball"),
    }
    return jsonify(
        {
            # 未指定 sport 时保持既有语义（全部比赛日）；指定时只返回该运动的比赛日。
            "dates": get_available_dates(sport),
            "dates_by_sport": daily_loader.get_available_dates_by_sport(),
            "leagues": leagues,
        }
    )


@app.route("/api/strategy")
def api_strategy():
    sport = request.args.get("sport", "football")
    if sport == "all":
        sport = None
    result = generate_accumulators(sport=sport)
    return jsonify(result)


@app.route("/api/refresh", methods=["GET", "POST"])
def api_refresh():
    try:
        stats = scraper.refresh(verbose=False)
        return jsonify(stats)
    except scraper.RefreshBusyError as exc:
        return (
            jsonify(
                {
                    "status": "busy",
                    "message": "Refresh in progress",
                    # 所有权是**建议性**元数据：锁是否释放只由 OS 锁决定。
                    "active_writer": getattr(exc, "owner", None),
                }
            ),
            409,
        )


@app.route("/api/status")
def api_status():
    from utils.team_strength import stats as strength_stats

    return jsonify(
        {
            "meta": get_meta(),
            "dates": get_available_dates(),
            "team_pool": strength_stats(),
            "today_stats": get_daily_stats(),
            "source_health": build_source_health(),
        }
    )


def build_source_health() -> dict:
    """
    篮球同步的只读诊断：每个数据源的**阶段**与计数，而不是一个笼统的成功标志。

    - 只暴露状态、计数与时间戳，不暴露 URL、凭证或原始报文；
    - ``age_seconds`` 由读取时刻计算，缓存命中不会把旧结果伪装成「刚刚成功」；
    - ``displayed`` 说明「已存储」与「当前默认日期可见」是两件事；
    - ``coverage`` 说明「最近一次刷新成功」与「当前源赛事是否都已入库」是两件事
      （AGENTS.md §4.26）：刷新标记成功**不**等价于源侧无缺口。
    """
    from utils.fetcher_500 import source_diagnostics, is_nba_league
    from utils.market_coverage import has_usable_market_odds

    diagnostics = source_diagnostics()
    now = get_beijing_now()
    sources: dict[str, dict] = {}
    for name in ("live_basketball", "jclq_sf", "jclq_dxf"):
        entry = dict(diagnostics.get(name) or {"status": "not_probed"})
        checked_at = entry.get("checked_at")
        age_seconds = None
        if checked_at:
            try:
                age_seconds = int((now - datetime.fromisoformat(checked_at)).total_seconds())
            except (TypeError, ValueError):
                age_seconds = None
        entry["age_seconds"] = age_seconds
        entry["observation_scope"] = "PROCESS_LOCAL"
        sources[name] = entry

    basketball = [m for m in daily_loader.get_all_matches() if m.get("sport") == "basketball"]
    nba = [m for m in basketball if is_nba_league(m.get("league"))]
    default_date = get_default_date()
    return {
        "sources": sources,
        "stored": {
            "basketball": len(basketball),
            "nba": len(nba),
            "basketball_with_valid_market": sum(1 for m in basketball if has_usable_market_odds(m)),
        },
        "displayed": {
            "default_date": default_date,
            "basketball_on_default_date": len(daily_loader.get_matches_by_date(default_date, "basketball")),
            "nba_on_default_date": sum(
                1 for m in basketball if m.get("date") == default_date and is_nba_league(m.get("league"))
            ),
        },
        "basketball_dates": get_available_dates("basketball")[:7],
        "nba_dates": sorted({m.get("date") for m in nba if m.get("date")}, reverse=True)[:7],
        "coverage": build_nba_coverage(nba, get_meta().get("last_refresh")),
        "pregame_readiness": build_nba_pregame_readiness(nba),
    }


#: 源覆盖诊断的状态词表。二者互斥，且**不得**由 `refresh_status` 推断。
COVERAGE_KNOWN = "KNOWN"
COVERAGE_UNKNOWN = "UNKNOWN"

#: 构造「当前源观测」的篮球数据源：缺任一个都无法断言覆盖已完整。
BASKETBALL_OBSERVATION_SOURCES = ("live_basketball", "jczq_odds_basketball")


def _parse_timestamp(value):
    """宽容解析 ISO 时间戳；不可解析即 ``None``（不可判定，绝不猜测）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _norm_source_id(record: dict) -> str | None:
    raw = record.get("id")
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def classify_observed_fixtures(rows, stored) -> dict:
    """
    把源观测行分为「已入库 / 合格未入库 / 无可用盘口」，并显式分离身份未决行。

    判等只走 ``same_event``（AGENTS.md §4.7/§4.16）：不推断队名别名、不交换主客、
    不合并跨源赔率字段。同一事件在观测中出现多条记录时，先用连通分量与团校验
    建立无歧义的事件集合；只要该集合中至少一条**独立**满足 ``has_usable_market_odds()``，
    该事件即归类为合格，结果与观测行顺序无关。
    """
    from utils.market_coverage import has_usable_market_odds
    from utils.match_identity import competition_event_key, same_event, team_event_key

    rows_list = list(rows or ())
    stored_list = list(stored or ())

    valid_indices: list[int] = []
    unresolved_indices: set[int] = set()
    reasons: set[str] = set()

    for idx, row in enumerate(rows_list):
        if not isinstance(row, dict):
            unresolved_indices.add(idx)
            reasons.add("MALFORMED_SOURCE_ROW")
            continue
        if competition_event_key(row) is None and team_event_key(row) is None:
            unresolved_indices.add(idx)
            reasons.add("INADEQUATE_EVENT_IDENTITY")
            continue
        valid_indices.append(idx)

    stored_by_id: dict[str, list[int]] = {}
    for s_idx, s_match in enumerate(stored_list):
        if not isinstance(s_match, dict):
            continue
        sid = _norm_source_id(s_match)
        if sid is not None:
            stored_by_id.setdefault(sid, []).append(s_idx)

    alias_groups: dict[str, list[int]] = {}
    alias_conflicted: set[int] = set()
    for idx in valid_indices:
        row = rows_list[idx]
        rid = _norm_source_id(row)
        if rid is None:
            continue
        alias_groups.setdefault(rid, []).append(idx)
        for s_idx in stored_by_id.get(rid, ()):
            if not same_event(stored_list[s_idx], row):
                alias_conflicted.add(idx)
                reasons.add("SAME_SOURCE_ALIAS_DIFFERENT_EVENT")

    for members in alias_groups.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not same_event(rows_list[members[i]], rows_list[members[j]]):
                    alias_conflicted.add(members[i])
                    alias_conflicted.add(members[j])
                    reasons.add("SAME_SOURCE_ALIAS_DIFFERENT_EVENT")

    parent = {idx: idx for idx in valid_indices}

    def _find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    row_buckets: dict[tuple, list[int]] = {}
    for idx in valid_indices:
        row = rows_list[idx]
        row_buckets.setdefault((row.get("sport"), row.get("date")), []).append(idx)

    for bucket in row_buckets.values():
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                if same_event(rows_list[a], rows_list[b]):
                    _union(a, b)

    components: dict[int, list[int]] = {}
    for idx in valid_indices:
        components.setdefault(_find(idx), []).append(idx)

    stored_buckets: dict[tuple, list[int]] = {}
    for s_idx, s_match in enumerate(stored_list):
        if isinstance(s_match, dict):
            stored_buckets.setdefault((s_match.get("sport"), s_match.get("date")), []).append(s_idx)

    already = 0
    pending = 0
    ineligible = 0

    for comp in components.values():
        if any(idx in alias_conflicted for idx in comp):
            unresolved_indices.update(comp)
            continue

        is_clique = all(
            same_event(rows_list[comp[i]], rows_list[comp[j]])
            for i in range(len(comp))
            for j in range(i + 1, len(comp))
        )
        if not is_clique:
            unresolved_indices.update(comp)
            reasons.add("NON_CLIQUE_COMPONENT")
            continue

        owner_sets: set[tuple[int, ...]] = set()
        for idx in comp:
            row = rows_list[idx]
            candidates = stored_buckets.get((row.get("sport"), row.get("date")), ())
            owners = tuple(
                s_idx for s_idx in candidates if same_event(stored_list[s_idx], row)
            )
            owner_sets.add(owners)

        if len(owner_sets) != 1:
            unresolved_indices.update(comp)
            reasons.add("COMPONENT_OWNER_INCONSISTENCY")
            continue

        owners = next(iter(owner_sets))
        if len(owners) > 1:
            unresolved_indices.update(comp)
            reasons.add("MULTIPLE_CANONICAL_OWNERS")
            continue

        if len(owners) == 1:
            already += 1
        elif any(has_usable_market_odds(rows_list[idx]) for idx in comp):
            pending += 1
        else:
            ineligible += 1

    unresolved_count = len(unresolved_indices)
    if unresolved_count > 0:
        return {
            "classification_status": "IDENTITY_UNRESOLVED",
            "raw_source_rows": len(rows_list),
            "distinct_established_events": None,
            "already_ingested_events": None,
            "market_eligible_pending_events": None,
            "market_ineligible_events": None,
            "identity_unresolved_rows": unresolved_count,
            "unresolved_reasons": sorted(reasons),
        }

    return {
        "classification_status": "RESOLVED",
        "raw_source_rows": len(rows_list),
        "distinct_established_events": already + pending + ineligible,
        "already_ingested_events": already,
        "market_eligible_pending_events": pending,
        "market_ineligible_events": ineligible,
        "identity_unresolved_rows": 0,
        "unresolved_reasons": [],
    }


def _classify_observed_fixtures(rows, stored):
    """兼容三元组返回接口；存在未决身份时返回 ``(None, None, None)``。"""
    summary = classify_observed_fixtures(rows, stored)
    return (
        summary["already_ingested_events"],
        summary["market_eligible_pending_events"],
        summary["market_ineligible_events"],
    )


def build_nba_coverage(stored_nba, last_refresh_at=None) -> dict:
    """
    NBA 源覆盖诊断：把「最近一次刷新成功」与「当前源赛事是否已入库」分开陈述。

    五种状态必须分别可见（AGENTS.md §4.26）：

    - ``LAST_REFRESH_SUCCESS``：``meta.last_refresh``（刷新最后一次成功落盘的时刻）；
    - ``CURRENT_SOURCE_OBSERVATION``：本轮进程**真实抓取**到的行（进程内局部状态，缓存命中不算）；
    - ``SOURCE_FIXTURES_NOT_YET_INGESTED``：源有、库无，且盘口合格；
    - ``MARKET_INELIGIBLE_FIXTURES``：源有、库无，按既有准入契约被过滤；
    - ``POST_REFRESH_INGESTION_VERIFIED``：观测**晚于**最近一次刷新且无合格缺口、无身份未决。

    观测缺失、源不可达/为空、观测早于数据集、或存在未决身份时，计数一律返回 ``None``
    并置 ``coverage_status = UNKNOWN``——**绝不**把「没观测/未决」写成「零缺口」。
    """
    from utils.fetcher_500 import is_nba_league, last_source_observation

    observation = last_source_observation()
    sources: dict[str, dict] = {}
    observed_at = None
    observed_rows: list[dict] = []
    states: list[str] = []

    for name in BASKETBALL_OBSERVATION_SOURCES:
        entry = observation.get(name)
        if not entry:
            states.append("NOT_OBSERVED")
            sources[name] = {
                "status": "not_observed",
                "checked_at": None,
                "rows": 0,
                "scope": "PROCESS_LOCAL",
            }
            continue
        status = entry.get("status")
        checked_at = entry.get("checked_at")
        sources[name] = {
            "status": status,
            "checked_at": checked_at,
            "rows": entry.get("count", 0),
            "scope": "PROCESS_LOCAL",
        }
        states.append(str(status or "unknown"))
        stamp = _parse_timestamp(checked_at)
        if stamp is not None and (observed_at is None or stamp < observed_at):
            observed_at = stamp
        for row in entry.get("rows") or []:
            if row.get("sport") == "basketball":
                observed_rows.append(row)

    has_unobserved = "NOT_OBSERVED" in states
    sources_healthy = (not has_unobserved) and all(state == "ok" for state in states)
    if has_unobserved:
        obs_unavailable_reason = "SOURCE_NOT_OBSERVED"
    elif not sources_healthy:
        obs_unavailable_reason = "SOURCE_NOT_HEALTHY"
    elif observed_at is None:
        obs_unavailable_reason = "OBSERVATION_TIMESTAMP_UNUSABLE"
    else:
        obs_unavailable_reason = None

    coverage = {
        "coverage_status": COVERAGE_UNKNOWN,
        "coverage_reason": None,
        "classification_status": "NOT_EVALUATED",
        "last_refresh_success": {"at": last_refresh_at},
        "current_source_observation": {
            "state": "NOT_OBSERVED" if has_unobserved else "OBSERVED",
            "scope": "PROCESS_LOCAL",
            "shared_across_processes": False,
            "persistent": False,
            "available": obs_unavailable_reason is None,
            "unavailable_reason": obs_unavailable_reason,
            "observed_at": observed_at.isoformat() if observed_at else None,
            "sources": sources,
        },
        "raw_nba_source_rows": None,
        "distinct_established_nba_events": None,
        "already_ingested_events": None,
        "market_eligible_pending_events": None,
        "market_ineligible_events": None,
        "identity_unresolved_rows": None,
        "unresolved_reasons": [],
        "source_nba_fixtures_observed": None,
        "source_fixtures_not_yet_ingested": None,
        "already_ingested_fixtures": None,
        "market_ineligible_fixtures": None,
        "non_nba_basketball_fixtures": None,
        "post_refresh_ingestion_verified": False,
    }

    if obs_unavailable_reason is not None:
        coverage["coverage_reason"] = obs_unavailable_reason
        return coverage

    refresh_at = _parse_timestamp(last_refresh_at)
    if refresh_at is None:
        coverage["coverage_reason"] = "LAST_REFRESH_TIMESTAMP_UNUSABLE"
        return coverage
    if observed_at <= refresh_at:
        coverage["coverage_reason"] = "OBSERVATION_PREDATES_LAST_REFRESH"
        return coverage

    nba_rows = [row for row in observed_rows if is_nba_league(row.get("league"))]
    stored_nba_only = [
        m for m in (stored_nba or ())
        if isinstance(m, dict)
        and m.get("sport") in (None, "basketball")
        and (not m.get("league") or is_nba_league(m.get("league")))
    ]
    summary = classify_observed_fixtures(nba_rows, stored_nba_only)

    if summary["identity_unresolved_rows"] > 0:
        coverage.update(
            coverage_status=COVERAGE_UNKNOWN,
            coverage_reason="IDENTITY_UNRESOLVED",
            classification_status=summary["classification_status"],
            raw_nba_source_rows=summary["raw_source_rows"],
            source_nba_fixtures_observed=summary["raw_source_rows"],
            identity_unresolved_rows=summary["identity_unresolved_rows"],
            unresolved_reasons=summary["unresolved_reasons"],
            non_nba_basketball_fixtures=len(observed_rows) - len(nba_rows),
            post_refresh_ingestion_verified=False,
        )
        return coverage

    pending = summary["market_eligible_pending_events"]
    coverage.update(
        coverage_status=COVERAGE_KNOWN,
        classification_status=summary["classification_status"],
        raw_nba_source_rows=summary["raw_source_rows"],
        distinct_established_nba_events=summary["distinct_established_events"],
        already_ingested_events=summary["already_ingested_events"],
        market_eligible_pending_events=pending,
        market_ineligible_events=summary["market_ineligible_events"],
        identity_unresolved_rows=0,
        unresolved_reasons=[],
        source_nba_fixtures_observed=summary["raw_source_rows"],
        source_fixtures_not_yet_ingested=pending,
        already_ingested_fixtures=summary["already_ingested_events"],
        market_ineligible_fixtures=summary["market_ineligible_events"],
        non_nba_basketball_fixtures=len(observed_rows) - len(nba_rows),
        post_refresh_ingestion_verified=pending == 0,
    )
    return coverage


def build_nba_pregame_readiness(nba_matches, now=None) -> dict:
    """
    严格只读：统计并分类 NBA 赛前预测快照就绪状态，显式区分存储态 status 与
    观测时刻生效状态（``effective_status``），绝不凭时间流逝自发推断完赛或写存储。
    """
    from datetime import timedelta
    from utils import prediction_snapshots
    from utils.daily_loader import BEIJING_TZ, get_beijing_now, get_match_datetime
    from utils.match_identity import build_kickoff_time_provenance, is_valid_calendar_date

    now_bj = (now or get_beijing_now()).astimezone(BEIJING_TZ)
    matches = [m for m in (nba_matches or ()) if isinstance(m, dict)]

    # 仅在快照存储已存在时读取只读索引；不存在时保持空集合而不触发文件创建。
    snap_ids_by_match: dict[str, int] = {}
    if os.path.exists(prediction_snapshots.store_path()):
        for m in matches:
            mid = str(m.get("id") or "")
            if mid:
                snap_ids_by_match[mid] = len(prediction_snapshots.get_snapshots_for_match(mid))

    items: list[dict] = []
    upcoming_total = 0
    kickoff_known_upcoming = 0
    kickoff_unknown_upcoming = 0
    prediction_ready = 0
    prediction_snapshot_present = 0
    prediction_blocked_unknown = 0
    prediction_blocked_ineligible = 0
    effective_status_counts: dict[str, int] = {
        "FUTURE_UPCOMING_VERIFIED_KICKOFF": 0,
        "UPCOMING_UNKNOWN_KICKOFF": 0,
        "PAST_KICKOFF_STORED_UPCOMING": 0,
        "FINISHED_SOURCE_CONFIRMED": 0,
        "LIVE_SOURCE_CONFIRMED": 0,
        "HISTORICAL_STATUS_STALE": 0,
        "STATUS_NOT_ESTABLISHED": 0,
    }
    prediction_capture_eligible_now_count = 0

    for m in matches:
        mid = str(m.get("id") or "")
        raw_status = str(m.get("status") or "upcoming").strip().lower() or "upcoming"
        provenance = build_kickoff_time_provenance(m)
        kickoff_dt = get_match_datetime(m)
        m_date_str = str(m.get("date") or "").strip()
        m_date_obj = datetime.strptime(m_date_str, "%Y-%m-%d").date() if is_valid_calendar_date(m_date_str) else None

        snap_count = snap_ids_by_match.get(mid, 0)
        has_snapshot = snap_count > 0
        if has_snapshot:
            prediction_snapshot_present += 1

        if raw_status == "upcoming":
            upcoming_total += 1
            if provenance["kickoff_time_known"]:
                kickoff_known_upcoming += 1
            else:
                kickoff_unknown_upcoming += 1

        eligible_now = False
        if raw_status == "live":
            item_status = "LIVE_WITH_PREGAME_PREDICTION" if has_snapshot else "LIVE_NO_PREGAME_PREDICTION"
            eff_status = "LIVE_SOURCE_CONFIRMED"
            eff_reason = "SOURCE_CONFIRMED_LIVE"
            prediction_blocked_ineligible += 1
        elif raw_status == "finished":
            item_status = "FINISHED_WITH_HISTORICAL_PREDICTION" if has_snapshot else "FINISHED_NO_HISTORICAL_PREDICTION"
            eff_status = "FINISHED_SOURCE_CONFIRMED"
            eff_reason = "SOURCE_CONFIRMED_FINISHED"
            prediction_blocked_ineligible += 1
        elif raw_status != "upcoming":
            item_status = "BLOCKED_INELIGIBLE_STATUS"
            eff_status = "STATUS_NOT_ESTABLISHED"
            eff_reason = "NON_STANDARD_OR_MISSING_STORED_STATUS"
            prediction_blocked_ineligible += 1
        elif kickoff_dt is None:
            item_status = "BLOCKED_UNKNOWN_KICKOFF_TIME"
            prediction_blocked_unknown += 1
            if m_date_obj is not None and m_date_obj < now_bj.date():
                eff_status = "HISTORICAL_STATUS_STALE"
                eff_reason = "PRIOR_DATE_UNKNOWN_KICKOFF_STORED_UPCOMING"
            else:
                eff_status = "UPCOMING_UNKNOWN_KICKOFF"
                eff_reason = "KICKOFF_TIME_UNKNOWN_ON_OR_AFTER_TODAY"
        elif now_bj >= kickoff_dt:
            item_status = "BLOCKED_PAST_KICKOFF"
            prediction_blocked_ineligible += 1
            if kickoff_dt.date() < now_bj.date() or (now_bj - kickoff_dt) >= timedelta(hours=12):
                eff_status = "HISTORICAL_STATUS_STALE"
                eff_reason = "PRIOR_KICKOFF_STORED_UPCOMING_UNCONFIRMED"
            else:
                eff_status = "PAST_KICKOFF_STORED_UPCOMING"
                eff_reason = "RECENT_KICKOFF_ELAPSED_SOURCE_UNCONFIRMED"
        elif has_snapshot:
            item_status = "READY_WITH_PREDICTION_SNAPSHOT"
            eff_status = "FUTURE_UPCOMING_VERIFIED_KICKOFF"
            eff_reason = "BEFORE_VERIFIED_KICKOFF_SNAPSHOT_PRESENT"
            prediction_ready += 1
        else:
            item_status = "ELIGIBLE_NOT_YET_MATERIALIZED"
            eff_status = "FUTURE_UPCOMING_VERIFIED_KICKOFF"
            eff_reason = "BEFORE_VERIFIED_KICKOFF_ELIGIBLE_FOR_CAPTURE"
            eligible_now = True
            prediction_ready += 1
            prediction_capture_eligible_now_count += 1

        effective_status_counts[eff_status] += 1

        items.append(
            {
                "match_id": mid,
                "date": m.get("date"),
                "time": m.get("time"),
                "stored_status": raw_status,
                "status": item_status,
                "effective_status": eff_status,
                "effective_status_reason": eff_reason,
                "kickoff_time_verification_status": provenance["kickoff_verification_status"],
                "prediction_capture_eligible_now": eligible_now,
                "prediction_snapshot_present": has_snapshot,
                "has_prediction_snapshot": has_snapshot,
                "prediction_snapshot_count": snap_count,
                "kickoff_at": kickoff_dt.isoformat() if kickoff_dt is not None else None,
                **provenance,
            }
        )

    return {
        "read_only": True,
        "evaluated_at": now_bj.isoformat(),
        "nba_total": len(items),
        "upcoming_total": upcoming_total,
        "kickoff_time_known": kickoff_known_upcoming,
        "kickoff_time_unknown": kickoff_unknown_upcoming,
        "prediction_ready": prediction_ready,
        "prediction_capture_eligible_now_count": prediction_capture_eligible_now_count,
        "prediction_snapshot_present": prediction_snapshot_present,
        "prediction_blocked_unknown_kickoff": prediction_blocked_unknown,
        "prediction_blocked_ineligible": prediction_blocked_ineligible,
        "historical_status_stale_count": effective_status_counts["HISTORICAL_STATUS_STALE"],
        "effective_status_counts": effective_status_counts,
        "items": items,
    }


@app.route("/api/history")
def api_history():
    """只读：返回与 ``/history`` 页面一致的不可变赛前证据视图。

    该接口特意复用 ``build_history_view_data``，避免任何客户端或调用方
    以当前模型重算已完赛赛事的历史结论。
    """
    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    matches = get_matches_by_date("all", sport)
    return jsonify(build_history_view_data(matches, sport=sport))


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _compare(match: dict, pred: dict) -> dict:
    """预测 vs 实际"""
    score = match.get("score") or {}
    ft = score.get("ft")
    if not (isinstance(ft, list) and len(ft) >= 2):
        return {}

    hg, ag = int(ft[0]), int(ft[1])
    actual = "home_win" if hg > ag else ("draw" if hg == ag else "away_win")
    probs = pred.get("probabilities", {})
    predicted = max(probs, key=lambda k: probs.get(k, 0)) if probs else ""

    labels = {"home_win": "主胜", "draw": "平局", "away_win": "客胜"}
    score_str = f"{hg}-{ag}"
    hit_scores = [s.get("score") for s in pred.get("top_scores", [])]

    return {
        "actual_score": score_str,
        "actual_outcome": actual,
        "actual_label": labels.get(actual, actual),
        "predicted_outcome": predicted,
        "predicted_label": labels.get(predicted, predicted),
        "correct": predicted == actual,
        "confidence": probs.get(predicted, 0),
        "actual_prob": probs.get(actual, 0),
        "score_hit": score_str in hit_scores,
    }


# ---------------------------------------------------------------------------
# 后台定时抓取
# ---------------------------------------------------------------------------

def start_background_scraper():
    """后台守护线程：定时抓取 500.com 赛事"""

    def job():
        print("\n[Auto-Sync] 后台抓取线程启动")
        print(f"[Auto-Sync] 间隔 {config.SCRAPE_INTERVAL_SECONDS} 秒")
        time.sleep(3)
        while True:
            try:
                print(f"\n[Auto-Sync] {get_beijing_now().strftime('%H:%M:%S')} 开始抓取...")
                stats = scraper.refresh(verbose=True)
                print(f"[Auto-Sync] 完成: {stats.get('added')} 新增 / {stats.get('total')} 总计")
            except scraper.RefreshBusyError as exc:
                # 不再静默跳过：明确写出是谁持有写入者锁，便于判断本实例是否在吃旧数据。
                owner = getattr(exc, "owner", None) or {}
                holder = (
                    f"pid={owner.get('pid')} host={owner.get('host')} "
                    f"action={owner.get('action')} since={owner.get('started_at')}"
                    if owner
                    else "unknown (no in-progress marker)"
                )
                print(
                    "[Auto-Sync] 跳过: 另一个写入者正在刷新 (RefreshBusyError) "
                    f"持有者: {holder}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[Auto-Sync] 异常: {exc}")
            time.sleep(config.SCRAPE_INTERVAL_SECONDS)

    if __import__("os").environ.get("WERKZEUG_RUN_MAIN") == "true" or not config.DEBUG:
        t = threading.Thread(target=job, daemon=True)
        t.start()


if __name__ == "__main__":
    print("=" * 60)
    print("  每日实时体彩预测系统")
    print("=" * 60)
    start_background_scraper()
    print(f"[启动] http://{config.HOST}:{config.PORT}")
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
