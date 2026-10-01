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
  /api/dates                    可用日期
  /api/strategy                 策略推荐
  /api/refresh                  手动触发抓取
  /api/status                   系统状态
"""
from __future__ import annotations

import threading
import time

from flask import Flask, jsonify, render_template, request

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
from utils.odds_snapshots import get_odds_history_for_match
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match

app = Flask(__name__)
app.config.from_object(config)


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@app.route("/")
def index():
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
    now = get_beijing_now()

    return render_template(
        "index.html",
        groups=groups,
        stats=stats,
        dates=dates,
        display_date=display_date,
        current_sport=sport or "all",
        meta=get_meta(),
        beijing_now=now.strftime("%Y-%m-%d %H:%M"),
        active_page="index",
    )


@app.route("/match/<match_id>")
def match_detail(match_id):
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
    sport = request.args.get("sport")
    if sport == "all":
        sport = None
    matches = [
        enrich_match(m) for m in get_matches_by_date("all", sport)
    ]
    finished = [m for m in matches if m.get("status") == "finished" and m.get("score")]
    finished.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)

    # 附带预测 vs 实际
    for m in finished:
        pred = predict_match(
            m["home_team"], m["away_team"],
            odds=m.get("odds"), sport=m.get("sport", "football"), league=m.get("league", ""),
        )
        m["prediction"] = pred
        m["comparison"] = _compare(m, pred)

    return render_template(
        "history.html",
        matches=finished[:200],
        total=len(finished),
        current_sport=sport or "all",
        active_page="history",
    )


@app.route("/strategy")
def strategy():
    return render_template("strategy.html", active_page="strategy")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

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


@app.route("/api/dates")
def api_dates():
    return jsonify(
        {
            "dates": get_available_dates(),
            "leagues": {
                "football": get_leagues("football"),
                "basketball": get_leagues("basketball"),
            },
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
    stats = scraper.refresh(verbose=False)
    return jsonify(stats)


@app.route("/api/status")
def api_status():
    from utils.team_strength import stats as strength_stats

    return jsonify(
        {
            "meta": get_meta(),
            "dates": get_available_dates(),
            "team_pool": strength_stats(),
            "today_stats": get_daily_stats(),
        }
    )


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
