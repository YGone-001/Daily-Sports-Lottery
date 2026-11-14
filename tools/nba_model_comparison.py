"""NBA V1 对 V2 影子模型只读对比工作流（Workstream H 用户可见证据）。

用法
----
    # 真实历史数据集 + 当前合格赛事的 V1/V2/市场对比
    python -m tools.nba_model_comparison --dataset reports/phase3x/historical-game-coverage/dataset.json \
        --as-of 2026-11-14T12:00:00+08:00 --output reports/phase3x/v1-v2-comparison/comparison.json \
        --markdown reports/phase3x/v1-v2-comparison/comparison.md

    # 仅导出 30 支球队真实实力清单
    python -m tools.nba_model_comparison --dataset <path> --strength-only --output <path>

    # 按时间切分的走查回测（回溯研究，非原始实时快照）
    python -m tools.nba_model_comparison --dataset <path> --backtest --output <path>

只读边界
--------
- 读取本地历史数据集与 ``data/daily_matches.json``（只读），不写任何受保护存储；
- 不发起网络请求（历史获取另由 ``utils.nba_history`` 在操作者授权下执行）；
- 不从 HTTP GET 物化影子预测；不替换生产模型；不改写历史快照/结算。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import config
from utils.fetcher_500 import is_nba_league
from utils.match_identity import is_kickoff_time_known
from utils.daily_loader import get_beijing_now, get_match_datetime
from utils.nba_schedule import BEIJING_TZ, resolve_canonical_team_code
from utils.nba_team_strength import compute_team_strength
from utils.nba_model_evaluation import walk_forward_backtest
from models.basketball_model import predict_basketball
from models.basketball_model_v2 import MODEL_VERSION as V2_VERSION, predict_from_inventory, STATUS_OK
from utils.team_strength import _estimate_elo


def _load_dataset(path: str | None) -> dict:
    if not path:
        return {"games": [], "game_count": 0}
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(data, dict) and "games" in data:
        return data
    if isinstance(data, list):
        return {"games": data, "game_count": len(data)}
    return {"games": [], "game_count": 0}


def _read_upcoming_nba_fixtures() -> list[dict]:
    path = Path(config.DATA_DIR) / "daily_matches.json"
    if not path.exists():
        return []
    daily = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for m in daily.get("matches", []):
        if m.get("sport") != "basketball" or not is_nba_league(m.get("league")):
            continue
        if str(m.get("status") or "upcoming").lower() != "upcoming":
            continue
        hc = resolve_canonical_team_code(m.get("home"))
        ac = resolve_canonical_team_code(m.get("away"))
        if not hc or not ac:
            continue
        out.append(dict(m, _home_code=hc, _away_code=ac))
    out.sort(key=lambda m: (m.get("date") or "", m.get("id") or ""))
    return out


def _v1_for_fixture(m: dict) -> dict:
    league = m.get("league") or ""
    home = {"name": m.get("home"), "elo_rating": _estimate_elo(league, None, "basketball", m.get("home")),
            "matches_played": 0}
    away = {"name": m.get("away"), "elo_rating": _estimate_elo(league, None, "basketball", m.get("away")),
            "matches_played": 0}
    return predict_basketball(home, away, m.get("odds"), league)


def _market_home(odds: dict | None) -> float | None:
    if not odds:
        return None
    try:
        ho, ao = float(odds["home_win"]), float(odds["away_win"])
    except (KeyError, TypeError, ValueError):
        return None
    if ho <= 1 or ao <= 1:
        return None
    ih, ia = 1 / ho, 1 / ao
    return ih / (ih + ia)


def build_comparison(dataset: dict, *, as_of: datetime, fixtures: list[dict], params: dict | None = None) -> dict:
    games = dataset.get("games", [])
    inventory = compute_team_strength(games, as_of=as_of, params=params)
    rows = []
    for m in fixtures:
        kickoff = get_match_datetime(m) if is_kickoff_time_known(m) else None
        v1 = _v1_for_fixture(m)
        v2 = predict_from_inventory(inventory, m["_home_code"], m["_away_code"],
                                    odds=m.get("odds"), fixture_kickoff=kickoff)
        market_home = _market_home(m.get("odds"))
        v1_raw = v1["model_probabilities"]["home_win"] / 100.0
        eligible = v2.get("status") == STATUS_OK
        v2_raw = v2["raw_model_probabilities"]["home_win"] if eligible else None
        rows.append({
            "canonical_match_id": m.get("id"),
            "fixture_date": m.get("date"),
            "home_team": m.get("home"),
            "away_team": m.get("away"),
            "home_code": m["_home_code"],
            "away_code": m["_away_code"],
            "verified_kickoff": kickoff.isoformat() if kickoff else None,
            "prediction_as_of": as_of.isoformat(),
            "v1_model_version": config.MODEL_VERSIONS["basketball"],
            "v1_home_probability": round(v1_raw, 4),
            "v1_away_probability": round(1 - v1_raw, 4),
            "v2_shadow_version": V2_VERSION,
            "v2_home_probability": round(v2_raw, 4) if v2_raw is not None else None,
            "v2_away_probability": round(1 - v2_raw, 4) if v2_raw is not None else None,
            "market_home_probability": round(market_home, 4) if market_home is not None else None,
            "market_away_probability": round(1 - market_home, 4) if market_home is not None else None,
            "v1_vs_market_gap": round(v1_raw - market_home, 4) if market_home is not None else None,
            "v2_vs_market_gap": round(v2_raw - market_home, 4) if (v2_raw is not None and market_home is not None) else None,
            "home_real_games_used": v2.get("home_games_used"),
            "away_real_games_used": v2.get("away_games_used"),
            "data_coverage_status": v2.get("data_coverage"),
            "shadow_eligibility": "ELIGIBLE" if eligible else "INSUFFICIENT_REAL_TEAM_DATA",
            "v2_status": v2.get("status"),
        })
    return {
        "workflow": "NBA_V1_V2_READONLY_COMPARISON",
        "as_of": as_of.isoformat(),
        "dataset_game_count": dataset.get("game_count", len(games)),
        "real_team_strength_coverage": f"{inventory['supported_team_count']}/{inventory['total_franchises']}",
        "supported_team_count": inventory["supported_team_count"],
        "stable_team_count": inventory["stable_team_count"],
        "eligible_fixture_count": sum(1 for r in rows if r["shadow_eligibility"] == "ELIGIBLE"),
        "insufficient_fixture_count": sum(1 for r in rows if r["shadow_eligibility"] != "ELIGIBLE"),
        "comparison": rows,
        "production_model_unchanged": True,
        "writes_protected_stores": False,
        "network_requests": 0,
        "promotion_authorized": False,
    }


def _markdown(comparison: dict) -> str:
    lines = ["# NBA V1 对 V2 影子模型只读对比", "",
             f"- as_of: `{comparison['as_of']}`",
             f"- 历史数据集比赛数: {comparison['dataset_game_count']}",
             f"- 真实实力覆盖: **{comparison['real_team_strength_coverage']}** "
             f"(稳定 {comparison['stable_team_count']})",
             f"- 合格赛事: {comparison['eligible_fixture_count']} / "
             f"不足: {comparison['insufficient_fixture_count']}", "",
             "| 赛事 | 日期 | 主 vs 客 | V1 主胜 | V2 主胜 | 市场主胜 | V2-市场 | 主/客真实场次 | 影子资格 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in comparison["comparison"]:
        v2h = f"{r['v2_home_probability']:.3f}" if r["v2_home_probability"] is not None else "—"
        mh = f"{r['market_home_probability']:.3f}" if r["market_home_probability"] is not None else "—"
        gap = f"{r['v2_vs_market_gap']:+.3f}" if r["v2_vs_market_gap"] is not None else "—"
        lines.append(
            f"| {r['canonical_match_id']} | {r['fixture_date']} | {r['home_team']} vs {r['away_team']} | "
            f"{r['v1_home_probability']:.3f} | {v2h} | {mh} | {gap} | "
            f"{r['home_real_games_used']}/{r['away_real_games_used']} | {r['shadow_eligibility']} |")
    lines += ["", "> V1 = `basketball-margin-1`（冷启动，未改动）；V2 = `basketball-margin-2-shadow`（真实历史实力）。",
              "> 影子模型不替换生产模型、不改写历史快照/结算；不足时显式 `INSUFFICIENT_REAL_TEAM_DATA`。"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help=" assembled 历史数据集 JSON 路径")
    parser.add_argument("--as-of", help="特征截止时刻 ISO（默认当前北京时间）")
    parser.add_argument("--strength-only", action="store_true", help="仅导出 30 支球队实力清单")
    parser.add_argument("--backtest", action="store_true", help="按时间切分的走查回测")
    parser.add_argument("--output", help="JSON 报告输出路径（reports/ 下）")
    parser.add_argument("--markdown", help="Markdown 对比表输出路径")
    parser.add_argument("--limit", type=int, default=0, help="限制对比赛事数（0=全部）")
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else get_beijing_now()
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=BEIJING_TZ)
    dataset = _load_dataset(args.dataset)

    if args.backtest:
        result = walk_forward_backtest(dataset.get("games", []))
    elif args.strength_only:
        result = compute_team_strength(dataset.get("games", []), as_of=as_of)
    else:
        fixtures = _read_upcoming_nba_fixtures()
        if args.limit > 0:
            fixtures = fixtures[:args.limit]
        result = build_comparison(dataset, as_of=as_of, fixtures=fixtures)
        if args.markdown:
            Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
            Path(args.markdown).write_text(_markdown(result), encoding="utf-8")

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())