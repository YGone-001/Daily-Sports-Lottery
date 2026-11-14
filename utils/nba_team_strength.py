"""NBA 真实球队实力与历史 Elo（确定性、只读、纯函数）。

用**已确认完赛**的真实比赛推导 30 支球队的实力特征，取代冷启动的队名哈希假设。

设计契约
--------
- 共同先验：每支球队从 documented ``prior_elo`` 起步，**不**用队名哈希作为实力信号。
- 严格时序：仅 ``game_end_utc < as_of`` 的比赛可更新实力；按开赛时间升序逐场更新。
- 显式 Elo 更新因子 ``k_factor``；主客区分（主场 Elo 加成 ``home_advantage_elo``）。
- 赛季过渡收缩：赛季切换时把 Elo 向先验收缩 ``season_shrinkage``（处理休赛期阵容变化）。
- 小样本：场次不足时保留 ``fallback_status``，由 V2 模型再做样本量收缩。
- 去重：同一 ``provider_game_id`` 绝不重复更新。
- 确定性：无随机、无内置 hash()、无当前时间、无文件系统状态；相同输入恒等输出。

统计口径警示
------------
``average_point_differential`` 是**场均净胜分**，**不是** NBA 每百回合进攻/防守/净效率。
Free Games 数据不含回合数输入，绝不把任何指标标注为 possession-adjusted。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Iterable

from utils.nba_schedule import BEIJING_TZ, NBA_FRANCHISE_MAPPINGS, convert_utc_to_beijing

ALL_NBA_CODES: tuple[str, ...] = tuple(sorted(e.canonical_code for e in NBA_FRANCHISE_MAPPINGS))
_CODE_TO_NAME: dict[str, str] = {e.canonical_code: e.official_name for e in NBA_FRANCHISE_MAPPINGS}

# documented 共同先验与 Elo 参数（人工维护，不从八场队列拟合）。
NBA_STRENGTH_PARAMS: dict[str, float | int] = {
    "prior_elo": 1500.0,
    "k_factor": 20.0,
    "home_advantage_elo": 50.0,
    "elo_scale": 400.0,
    "season_shrinkage": 0.25,
    "game_duration_minutes": 150,
    "overtime_increment_minutes": 15,
    "min_games_stable": 5,
}

COVERAGE_NO_GAMES = "NO_GAMES_BEFORE_CUTOFF"
COVERAGE_SMALL_SAMPLE = "SMALL_SAMPLE"
COVERAGE_SUPPORTED = "SUPPORTED"


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


def game_end_utc(game: dict, params: dict | None = None) -> datetime | None:
    """估算比赛结束时刻 = 开赛 + documented 时长（含加时增量）。

    BALLDONTLIE Free Games 不提供精确结束时刻；此为**显式假设**，用于保守时序门禁。
    回溯分析（as_of 远晚于比赛）不受影响；前瞻预测据此防止同日未结束比赛泄漏。
    """
    p = params or NBA_STRENGTH_PARAMS
    kickoff = _parse_utc(game.get("scheduled_start_utc"))
    if kickoff is None:
        return None
    ot = game.get("overtime_periods")
    ot = ot if type(ot) is int and ot >= 0 else 0
    minutes = int(p["game_duration_minutes"]) + ot * int(p["overtime_increment_minutes"])
    return kickoff + timedelta(minutes=minutes)


def expected_home_win(elo_home: float, elo_away: float, params: dict | None = None) -> float:
    """标准 Elo 预期主队胜率（含主场 Elo 加成）。"""
    p = params or NBA_STRENGTH_PARAMS
    ha = float(p["home_advantage_elo"])
    scale = float(p["elo_scale"])
    return 1.0 / (1.0 + 10 ** ((elo_away - (elo_home + ha)) / scale))


def _digest(values: Iterable[str]) -> str:
    payload = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_team_strength(
    games: Iterable[dict],
    *,
    as_of: datetime,
    params: dict | None = None,
) -> dict:
    """由真实完赛比赛推导 30 支球队的实力特征清单（纯函数，只读）。

    返回 ``{"as_of", "teams": {code: features}, "supported_team_count",
    "stable_team_count", "total_franchises", "games_used", "params", ...}``。
    每支球队都出现在结果中（即使 0 场），并带明确 ``fallback_status``。
    """
    p = dict(NBA_STRENGTH_PARAMS)
    if params:
        p.update(params)
    as_of_bj = as_of.astimezone(BEIJING_TZ) if as_of.tzinfo else as_of.replace(tzinfo=BEIJING_TZ)

    # 去重 + 时序过滤：仅保留结束时刻早于 as_of 的合法比赛。
    by_id: dict[str, dict] = {}
    for g in games:
        if not isinstance(g, dict):
            continue
        gid = g.get("provider_game_id")
        hc, ac = g.get("home_code"), g.get("away_code")
        if not gid or hc not in ALL_NBA_CODES or ac not in ALL_NBA_CODES or hc == ac:
            continue
        hs, aws = g.get("home_final_score"), g.get("away_final_score")
        if type(hs) is not int or type(aws) is not int or hs < 0 or aws < 0 or hs == aws:
            continue
        end = game_end_utc(g, p)
        if end is None or end >= as_of_bj:
            continue
        if gid in by_id:
            continue
        by_id[gid] = dict(g, _end_utc=end)

    ordered = sorted(by_id.values(), key=lambda g: (g["scheduled_start_utc"] or "", g["provider_game_id"]))

    prior = float(p["prior_elo"])
    k = float(p["k_factor"])
    shrink = float(p["season_shrinkage"])
    elo = {code: prior for code in ALL_NBA_CODES}
    log: dict[str, list[dict]] = {code: [] for code in ALL_NBA_CODES}
    current_season: int | None = None
    season_transitions = 0

    for g in ordered:
        season = g.get("season")
        if type(season) is int and current_season is not None and season != current_season:
            # 赛季过渡：向共同先验收缩（处理休赛期变化），不抹除已累积战绩。
            for code in ALL_NBA_CODES:
                elo[code] = prior + (elo[code] - prior) * (1.0 - shrink)
            season_transitions += 1
        if type(season) is int:
            current_season = season

        hc, ac = g["home_code"], g["away_code"]
        hs, aws = g["home_final_score"], g["away_final_score"]
        exp_h = expected_home_win(elo[hc], elo[ac], p)
        actual_h = 1.0 if hs > aws else 0.0
        delta = k * (actual_h - exp_h)
        elo[hc] += delta
        elo[ac] -= delta

        end_iso = g["_end_utc"].isoformat()
        for code, venue, opp, pf, pa in ((hc, "home", ac, hs, aws), (ac, "away", hc, aws, hs)):
            log[code].append({
                "provider_game_id": g["provider_game_id"],
                "season": season,
                "game_date": g.get("game_date"),
                "beijing_date": g.get("beijing_date"),
                "end_utc": end_iso,
                "venue": venue,
                "opponent": opp,
                "points_for": pf,
                "points_against": pa,
                "diff": pf - pa,
                "win": pf > pa,
            })

    teams: dict[str, dict] = {}
    supported = stable = 0
    for code in ALL_NBA_CODES:
        entries = log[code]
        n = len(entries)
        wins = sum(1 for e in entries if e["win"])
        losses = n - wins
        pf = sum(e["points_for"] for e in entries)
        pa = sum(e["points_against"] for e in entries)
        home = [e for e in entries if e["venue"] == "home"]
        away = [e for e in entries if e["venue"] == "away"]
        recent5 = entries[-5:]
        recent10 = entries[-10:]
        last_end = _parse_utc(entries[-1]["end_utc"]) if entries else None
        days_since = (as_of_bj - last_end).days if last_end else None

        if n == 0:
            coverage, fallback = COVERAGE_NO_GAMES, COVERAGE_NO_GAMES
        elif n < int(p["min_games_stable"]):
            coverage, fallback = COVERAGE_SMALL_SAMPLE, COVERAGE_SMALL_SAMPLE
        else:
            coverage, fallback = COVERAGE_SUPPORTED, COVERAGE_SUPPORTED
        if n >= 1:
            supported += 1
        if n >= int(p["min_games_stable"]):
            stable += 1

        teams[code] = {
            "team_id": code,
            "team_name": _CODE_TO_NAME[code],
            "actual_games_available": n,
            "games_used_before_cutoff": n,
            "wins": wins,
            "losses": losses,
            "win_percentage": round(wins / n, 4) if n else None,
            "average_points_for": round(pf / n, 3) if n else None,
            "average_points_against": round(pa / n, 3) if n else None,
            "average_point_differential": round((pf - pa) / n, 3) if n else None,
            "recent_5_game_record": {"wins": sum(1 for e in recent5 if e["win"]),
                                     "losses": sum(1 for e in recent5 if not e["win"]),
                                     "games": len(recent5)},
            "recent_10_game_record": {"wins": sum(1 for e in recent10 if e["win"]),
                                      "losses": sum(1 for e in recent10 if not e["win"]),
                                      "games": len(recent10)},
            "recent_10_point_differential": round(sum(e["diff"] for e in recent10) / len(recent10), 3) if recent10 else None,
            "home_record": {"wins": sum(1 for e in home if e["win"]),
                            "losses": sum(1 for e in home if not e["win"]), "games": len(home)},
            "away_record": {"wins": sum(1 for e in away if e["win"]),
                            "losses": sum(1 for e in away if not e["win"]), "games": len(away)},
            "last_game_at": entries[-1]["end_utc"] if entries else None,
            "days_since_last_completed_game": days_since,
            "back_to_back_indicator": (days_since is not None and days_since <= 1),
            "historical_elo": round(elo[code], 3),
            "features_as_of": as_of_bj.isoformat(),
            "data_coverage": coverage,
            "fallback_status": fallback,
            "source_evidence": {
                "games_used": n,
                "provider_game_ids": [e["provider_game_id"] for e in entries],
                "evidence_digest": _digest(e["provider_game_id"] for e in entries),
            },
        }

    return {
        "as_of": as_of_bj.isoformat(),
        "params": p,
        "total_franchises": len(ALL_NBA_CODES),
        "games_used": len(ordered),
        "season_transitions": season_transitions,
        "supported_team_count": supported,
        "stable_team_count": stable,
        "teams": teams,
        "metric_semantics": "point_differential_per_game_NOT_possession_adjusted",
        "production_store_write": False,
        "name_hash_used_as_strength": False,
    }