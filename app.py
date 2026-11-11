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


def _classify_observed_fixtures(rows, stored):
    """
    把源观测行分为「已入库 / 合格未入库 / 无可用盘口」。

    判等只走 ``same_event``（AGENTS.md §4.7/§4.16）：不比较队名相似度、不按联赛名
    计数、不推断主客方向。同一事件在同一批观测里出现多次（直播行 + 竞彩行）时，
    先与已存记录判等；未入库的行之间同样按 ``same_event`` 去重，避免重复计数。
    """
    from utils.match_identity import same_event
    from utils.market_coverage import has_usable_market_odds

    already = 0
    pending = 0
    ineligible = 0
    pending_rows: list[dict] = []
    for row in rows:
        if any(same_event(row, stored_match) for stored_match in stored):
            already += 1
            continue
        if any(same_event(row, other) for other in pending_rows):
            continue
        if has_usable_market_odds(row):
            pending += 1
            pending_rows.append(row)
        else:
            ineligible += 1
            pending_rows.append(row)
    return already, pending, ineligible


def build_nba_coverage(stored_nba, last_refresh_at=None) -> dict:
    """
    NBA 源覆盖诊断：把「最近一次刷新成功」与「当前源赛事是否已入库」分开陈述。

    五种状态必须分别可见（AGENTS.md §4.26）：

    - ``LAST_REFRESH_SUCCESS``：``meta.last_refresh``（刷新最后一次成功落盘的时刻）；
    - ``CURRENT_SOURCE_OBSERVATION``：本轮进程**真实抓取**到的行（缓存命中不算）；
    - ``SOURCE_FIXTURES_NOT_YET_INGESTED``：源有、库无，且盘口合格；
    - ``MARKET_INELIGIBLE_FIXTURES``：源有、库无，按既有准入契约被过滤；
    - ``POST_REFRESH_INGESTION_VERIFIED``：观测**晚于**最近一次刷新且无合格缺口。

    观测缺失、源不可达/为空、或观测早于数据集时，计数一律返回 ``None`` 并置
    ``coverage_status = UNKNOWN``——**绝不**把「没观测」写成「零缺口」。
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
            sources[name] = {"status": "not_observed", "checked_at": None, "rows": 0}
            continue
        status = entry.get("status")
        checked_at = entry.get("checked_at")
        sources[name] = {"status": status, "checked_at": checked_at, "rows": entry.get("count", 0)}
        states.append(str(status or "unknown"))
        stamp = _parse_timestamp(checked_at)
        if stamp is not None and (observed_at is None or stamp < observed_at):
            observed_at = stamp
        for row in entry.get("rows") or []:
            if row.get("sport") == "basketball":
                observed_rows.append(row)

    coverage = {
        "coverage_status": COVERAGE_UNKNOWN,
        "coverage_reason": None,
        "last_refresh_success": {"at": last_refresh_at},
        "current_source_observation": {
            "state": "NOT_OBSERVED" if "NOT_OBSERVED" in states else "OBSERVED",
            "observed_at": observed_at.isoformat() if observed_at else None,
            "sources": sources,
        },
        "source_nba_fixtures_observed": None,
        "source_fixtures_not_yet_ingested": None,
        "already_ingested_fixtures": None,
        "market_ineligible_fixtures": None,
        "non_nba_basketball_fixtures": None,
        "post_refresh_ingestion_verified": False,
    }

    if "NOT_OBSERVED" in states:
        coverage["coverage_reason"] = "SOURCE_NOT_OBSERVED"
        return coverage
    if any(state != "ok" for state in states):
        coverage["coverage_reason"] = "SOURCE_NOT_HEALTHY"
        return coverage
    if observed_at is None:
        coverage["coverage_reason"] = "OBSERVATION_TIMESTAMP_UNUSABLE"
        return coverage

    refresh_at = _parse_timestamp(last_refresh_at)
    if refresh_at is None:
        coverage["coverage_reason"] = "LAST_REFRESH_TIMESTAMP_UNUSABLE"
        return coverage
    if observed_at <= refresh_at:
        coverage["coverage_reason"] = "OBSERVATION_PREDATES_LAST_REFRESH"
        return coverage

    nba_rows = [row for row in observed_rows if is_nba_league(row.get("league"))]
    already, pending, ineligible = _classify_observed_fixtures(nba_rows, stored_nba)

    coverage.update(
        coverage_status=COVERAGE_KNOWN,
        source_nba_fixtures_observed=len(nba_rows),
        source_fixtures_not_yet_ingested=pending,
        already_ingested_fixtures=already,
        market_ineligible_fixtures=ineligible,
        non_nba_basketball_fixtures=len(observed_rows) - len(nba_rows),
        post_refresh_ingestion_verified=pending == 0,
    )
    return coverage


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
