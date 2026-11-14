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
from utils.market_coverage import basketball_has_moneyline
from utils.nba_schedule import (
    BEIJING_TZ,
    PROVIDER_BALLDONTLIE_NBA_GAMES,
    resolve_canonical_team_code,
    resolve_provider_team_code,
)
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
    """读取存储为 upcoming 的 NBA 赛事（只读）。

    保留身份未决（队名无法映射）的赛事，交由资格门禁分类为 IDENTITY_UNRESOLVED，
    而不是静默丢弃——「存储为 upcoming」本身**不**等于真实前瞻赛事。
    """
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
        out.append(dict(m, _home_code=hc, _away_code=ac))
    out.sort(key=lambda m: (m.get("date") or "", m.get("id") or ""))
    return out


# 赛事资格分类（Workstream D）
FIXTURE_STORED_UPCOMING = "STORED_UPCOMING"
FIXTURE_VERIFIED_FUTURE = "VERIFIED_FUTURE_FIXTURE"
FIXTURE_STALE = "STALE_STORED_UPCOMING"
FIXTURE_KICKOFF_UNKNOWN = "KICKOFF_UNKNOWN"
FIXTURE_IDENTITY_UNRESOLVED = "IDENTITY_UNRESOLVED"
FIXTURE_POSTGAME = "POSTGAME_RETROSPECTIVE_COMPARISON"
FIXTURE_SCHEDULE_UNVERIFIED = "SCHEDULE_STATUS_UNVERIFIED"
FIXTURE_MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"

# 对比语义分类（Workstream E）
CLASS_PROSPECTIVE = "PROSPECTIVE_SHADOW_COMPARISON"
CLASS_RETROSPECTIVE = "RETROSPECTIVE_MODEL_DIAGNOSTIC"
CLASS_BLOCKED = "PROSPECTIVE_COMPARISON_BLOCKED"

# 本轮决策时刻之前多久以内的提供商观测才算「当前」排期证据。
FRESH_OBSERVATION_MAX_AGE_SECONDS = 900

_CLASS_FOR = {
    FIXTURE_VERIFIED_FUTURE: CLASS_PROSPECTIVE,
    FIXTURE_STALE: CLASS_RETROSPECTIVE,
    FIXTURE_POSTGAME: CLASS_RETROSPECTIVE,
    FIXTURE_KICKOFF_UNKNOWN: CLASS_BLOCKED,
    FIXTURE_IDENTITY_UNRESOLVED: CLASS_BLOCKED,
    FIXTURE_SCHEDULE_UNVERIFIED: CLASS_BLOCKED,
    FIXTURE_MARKET_UNAVAILABLE: CLASS_BLOCKED,
}


def _parse_aware(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _valid_provider_game_id(value) -> bool:
    return isinstance(value, str) and value.isdigit() and int(value) > 0


def provider_binding_counts(fixtures: list[dict]) -> dict[str, int]:
    """每个 BALLDONTLIE 赛事 ID 被多少条 Canonical 赛事声明（用于唯一绑定判定）。"""
    counts: dict[str, int] = {}
    for m in fixtures:
        prov = m.get("kickoff_provenance")
        gid = prov.get("kickoff_source_event_id") if isinstance(prov, dict) else None
        if gid:
            counts[str(gid)] = counts.get(str(gid), 0) + 1
    return counts


def classify_fixture_eligibility(
    m: dict,
    as_of: datetime,
    *,
    provider_observation: dict | None = None,
    provider_binding_count: int | None = None,
    canonical_owner_conflict: bool = False,
    max_observation_age_seconds: int = FRESH_OBSERVATION_MAX_AGE_SECONDS,
) -> dict:
    """对单场存储赛事判定真实前瞻资格（纯函数，只读，不改动任何存储）。

    ``VERIFIED_FUTURE_FIXTURE`` 要求全部满足：存储 upcoming、市场跟踪身份（Canonical ID +
    彩票编号 + ``market_tracked``）、双方球队身份、BALLDONTLIE 赛事 ID 有效且唯一绑定、
    溯源为 ``balldontlie_nba_games`` / ``SCHEDULE_PROVIDER_VERIFIED``、带时区开赛时间晚于
    ``as_of``、``as_of`` 之前不久取得的提供商观测仍为 ``scheduled`` 且未延期、观测主客与
    存储主客**完全一致**、胜负盘价格有效、无 Canonical 归属冲突。

    存储的历史排期观测**不**证明比赛在当前决策时刻仍按期进行；缺少当前观测时归为
    ``SCHEDULE_STATUS_UNVERIFIED`` 而不是默认已排期。绝不自动交换主客、绝不自动归并反向候选。
    """
    stored_status = str(m.get("status") or "upcoming").lower()
    hc, ac = m.get("_home_code"), m.get("_away_code")
    identity_ok = bool(hc and ac)
    known = is_kickoff_time_known(m)
    kickoff = get_match_datetime(m) if known else None
    mid = m.get("id") or ""
    source = mid.split("-", 1)[0] if "-" in mid else ""

    prov = m.get("kickoff_provenance") if isinstance(m.get("kickoff_provenance"), dict) else {}
    provider_game_id = prov.get("kickoff_source_event_id")
    provenance_ok = (prov.get("actual_kickoff_time_source") == PROVIDER_BALLDONTLIE_NBA_GAMES
                     and prov.get("kickoff_verification_status") == "SCHEDULE_PROVIDER_VERIFIED")
    provenance_kickoff = _parse_aware(prov.get("kickoff_source_scheduled_start_utc"))

    market_identity_ok = (m.get("market_tracked") is True and bool(mid)
                          and isinstance(m.get("jczq_no"), str) and bool(m["jczq_no"].strip()))
    odds = m.get("odds") if isinstance(m.get("odds"), dict) else {}
    market_ok = basketball_has_moneyline(odds)

    obs = provider_observation if isinstance(provider_observation, dict) else None
    obs_observed = _parse_aware(obs.get("observed_at")) if obs else None
    obs_kickoff = _parse_aware(obs.get("scheduled_start_utc")) if obs else None
    obs_hc = resolve_provider_team_code(obs.get("home_team_identifier")) if obs else None
    obs_ac = resolve_provider_team_code(obs.get("away_team_identifier")) if obs else None
    orientation_exact = bool(obs and identity_ok and obs_hc == hc and obs_ac == ac)

    if not identity_ok:
        binding = "IDENTITY_UNRESOLVED"
    elif source == "500lq":
        # 500lq 的 home 字段是显示第一位（§4.13.6），非已核验场地主队；方向未决。
        binding = "DISPLAY_ORDER_UNVERIFIED"
    elif orientation_exact:
        binding = "PROVIDER_VERIFIED"
    elif obs and identity_ok and obs_hc == ac and obs_ac == hc:
        binding = "PROVIDER_REVERSED"
    else:
        binding = "PROVIDER_ORDER_UNVERIFIED"

    if kickoff is not None:
        temporal = "FUTURE" if kickoff > as_of else "PAST"
        ttk = int((kickoff - as_of).total_seconds())
    else:
        temporal, ttk = "UNKNOWN", None

    obs_age = int((as_of - obs_observed).total_seconds()) if obs_observed else None
    if obs is None:
        source_status = "NO_CURRENT_PROVIDER_OBSERVATION"
    elif obs.get("postponed") is True or str(obs.get("status_state") or "").lower() == "postponed":
        source_status = "PROVIDER_POSTPONED"
    elif str(obs.get("status_state") or "").lower() != "scheduled" or obs.get("postponed") is not False:
        source_status = "PROVIDER_NOT_SCHEDULED"
    elif obs_observed is None or obs_age < 0:
        source_status = "PROVIDER_OBSERVATION_AFTER_AS_OF"
    elif obs_age > max_observation_age_seconds:
        source_status = "PROVIDER_OBSERVATION_STALE"
    else:
        source_status = "PROVIDER_SCHEDULED_CURRENT"

    def verdict():
        if stored_status in ("finished", "live"):
            return FIXTURE_POSTGAME, "STORED_STATUS_NOT_UPCOMING"
        if stored_status != "upcoming":
            return FIXTURE_SCHEDULE_UNVERIFIED, "STORED_STATUS_NOT_UPCOMING"
        if not identity_ok:
            return FIXTURE_IDENTITY_UNRESOLVED, "TEAM_IDENTITY_UNRESOLVED"
        if kickoff is None:
            return FIXTURE_KICKOFF_UNKNOWN, "KICKOFF_TIME_UNKNOWN"
        if kickoff <= as_of:
            return FIXTURE_STALE, "VERIFIED_KICKOFF_AT_OR_BEFORE_AS_OF"
        if binding == "DISPLAY_ORDER_UNVERIFIED":
            return FIXTURE_IDENTITY_UNRESOLVED, "REVERSED_ORIENTATION_UNRESOLVED"
        if not market_identity_ok:
            return FIXTURE_IDENTITY_UNRESOLVED, "MARKET_IDENTITY_UNESTABLISHED"
        if canonical_owner_conflict:
            return FIXTURE_IDENTITY_UNRESOLVED, "CANONICAL_OWNERSHIP_CONFLICT"
        if not _valid_provider_game_id(provider_game_id):
            return FIXTURE_SCHEDULE_UNVERIFIED, "PROVIDER_GAME_ID_MISSING"
        if not provenance_ok:
            return FIXTURE_SCHEDULE_UNVERIFIED, "SCHEDULE_PROVENANCE_UNVERIFIED"
        if provenance_kickoff is None or provenance_kickoff.astimezone(BEIJING_TZ) != kickoff:
            return FIXTURE_SCHEDULE_UNVERIFIED, "KICKOFF_PROVENANCE_MISMATCH"
        if provider_binding_count is None:
            return FIXTURE_IDENTITY_UNRESOLVED, "PROVIDER_BINDING_UNIQUENESS_UNVERIFIED"
        if provider_binding_count != 1:
            return FIXTURE_IDENTITY_UNRESOLVED, "DUPLICATE_PROVIDER_BINDING"
        if obs is None:
            return FIXTURE_SCHEDULE_UNVERIFIED, "FRESH_PROVIDER_OBSERVATION_UNAVAILABLE"
        if obs.get("provider") != PROVIDER_BALLDONTLIE_NBA_GAMES or str(obs.get("provider_game_id")) != provider_game_id:
            return FIXTURE_SCHEDULE_UNVERIFIED, "PROVIDER_OBSERVATION_EVENT_MISMATCH"
        if source_status != "PROVIDER_SCHEDULED_CURRENT":
            return FIXTURE_SCHEDULE_UNVERIFIED, source_status
        if binding != "PROVIDER_VERIFIED":
            return FIXTURE_IDENTITY_UNRESOLVED, "HOME_AWAY_ORIENTATION_UNVERIFIED"
        if obs_kickoff is None or obs_kickoff.astimezone(BEIJING_TZ) != kickoff:
            return FIXTURE_SCHEDULE_UNVERIFIED, "KICKOFF_CHANGED_SINCE_PROVENANCE"
        if not market_ok:
            return FIXTURE_MARKET_UNAVAILABLE, "MARKET_ODDS_INVALID"
        return FIXTURE_VERIFIED_FUTURE, None

    eligibility, reason = verdict()
    return {
        "stored_status": stored_status,
        "derived_temporal_status": temporal,
        "temporal_eligibility": temporal,
        "verified_kickoff": kickoff.isoformat() if kickoff else None,
        "time_to_kickoff_seconds": ttk,
        "home_away_binding_status": binding,
        "identity_confidence": "PROVIDER_VERIFIED" if binding == "PROVIDER_VERIFIED" else "UNVERIFIED",
        "source_status": source_status,
        "provider_observation_age_seconds": obs_age,
        "market_status": "VALID_MONEYLINE" if market_ok else "MONEYLINE_UNAVAILABLE",
        "fixture_eligibility": eligibility,
        "comparison_classification": _CLASS_FOR[eligibility],
        "disqualification_reason": reason,
        "provider_game_id": provider_game_id,
    }



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


def build_comparison(dataset: dict, *, as_of: datetime, fixtures: list[dict], params: dict | None = None,
                     observations: dict[str, dict] | None = None) -> dict:
    """``observations`` 按 Canonical match_id 提供本轮只读取得的提供商观测；缺省时无赛事可前瞻。"""
    games = dataset.get("games", [])
    inventory = compute_team_strength(games, as_of=as_of, params=params)
    counts = provider_binding_counts(fixtures)
    rows = []
    for m in fixtures:
        prov = m.get("kickoff_provenance") if isinstance(m.get("kickoff_provenance"), dict) else {}
        gid = prov.get("kickoff_source_event_id")
        gate = classify_fixture_eligibility(
            m, as_of, provider_observation=(observations or {}).get(m.get("id")),
            provider_binding_count=counts.get(str(gid)) if gid else None)
        kickoff = get_match_datetime(m) if is_kickoff_time_known(m) else None
        v1 = _v1_for_fixture(m)
        v2 = predict_from_inventory(inventory, m.get("_home_code"), m.get("_away_code"),
                                    odds=m.get("odds"), fixture_kickoff=kickoff)
        market_home = _market_home(m.get("odds"))
        v1_raw = v1["model_probabilities"]["home_win"] / 100.0
        eligible = v2.get("status") == STATUS_OK
        v2_raw = v2["raw_model_probabilities"]["home_win"] if eligible else None
        rows.append({
            "canonical_match_id": m.get("id"),
            "provider_game_id": gate["provider_game_id"],
            "fixture_date": m.get("date"),
            "home_team": m.get("home"),
            "away_team": m.get("away"),
            "home_code": m.get("_home_code"),
            "away_code": m.get("_away_code"),
            "stored_status": gate["stored_status"],
            "derived_temporal_status": gate["derived_temporal_status"],
            "verified_kickoff": gate["verified_kickoff"],
            "prediction_as_of": as_of.isoformat(),
            "time_to_kickoff_seconds": gate["time_to_kickoff_seconds"],
            "home_away_binding_status": gate["home_away_binding_status"],
            "identity_confidence": gate["identity_confidence"],
            "source_status": gate["source_status"],
            "market_status": gate["market_status"],
            "fixture_eligibility": gate["fixture_eligibility"],
            "comparison_classification": gate["comparison_classification"],
            "disqualification_reason": gate["disqualification_reason"],
            "v1_model_version": config.MODEL_VERSIONS["basketball"],
            "v1_home_probability": round(v1_raw, 4),
            "v1_away_probability": round(1 - v1_raw, 4),
            "v1_probability": round(v1_raw, 4),
            "v2_shadow_version": V2_VERSION,
            "v2_home_probability": round(v2_raw, 4) if v2_raw is not None else None,
            "v2_away_probability": round(1 - v2_raw, 4) if v2_raw is not None else None,
            "v2_shadow_probability": round(v2_raw, 4) if v2_raw is not None else None,
            "market_home_probability": round(market_home, 4) if market_home is not None else None,
            "market_away_probability": round(1 - market_home, 4) if market_home is not None else None,
            "market_probability": round(market_home, 4) if market_home is not None else None,
            "v1_vs_market_gap": round(v1_raw - market_home, 4) if market_home is not None else None,
            "v2_vs_market_gap": round(v2_raw - market_home, 4) if (v2_raw is not None and market_home is not None) else None,
            "home_real_games_used": v2.get("home_games_used"),
            "away_real_games_used": v2.get("away_games_used"),
            "data_coverage_status": v2.get("data_coverage"),
            "shadow_eligibility": "ELIGIBLE" if eligible else "INSUFFICIENT_REAL_TEAM_DATA",
            "v2_status": v2.get("status"),
        })

    def _count(key, value):
        return sum(1 for r in rows if r[key] == value)

    return {
        "workflow": "NBA_V1_V2_READONLY_COMPARISON",
        "as_of": as_of.isoformat(),
        "dataset_game_count": dataset.get("game_count", len(games)),
        "real_team_strength_coverage": f"{inventory['supported_team_count']}/{inventory['total_franchises']}",
        "supported_team_count": inventory["supported_team_count"],
        "stable_team_count": inventory["stable_team_count"],
        "eligible_fixture_count": sum(1 for r in rows if r["shadow_eligibility"] == "ELIGIBLE"),
        "insufficient_fixture_count": sum(1 for r in rows if r["shadow_eligibility"] != "ELIGIBLE"),
        "genuine_future_fixture_count": _count("fixture_eligibility", FIXTURE_VERIFIED_FUTURE),
        "prospective_comparison_count": _count("comparison_classification", CLASS_PROSPECTIVE),
        "retrospective_diagnostic_count": _count("comparison_classification", CLASS_RETROSPECTIVE),
        "prospective_blocked_count": _count("comparison_classification", CLASS_BLOCKED),
        "stale_stored_upcoming_count": _count("fixture_eligibility", FIXTURE_STALE),
        "kickoff_unknown_count": _count("fixture_eligibility", FIXTURE_KICKOFF_UNKNOWN),
        "identity_unresolved_count": _count("fixture_eligibility", FIXTURE_IDENTITY_UNRESOLVED),
        "schedule_status_unverified_count": _count("fixture_eligibility", FIXTURE_SCHEDULE_UNVERIFIED),
        "market_unavailable_count": _count("fixture_eligibility", FIXTURE_MARKET_UNAVAILABLE),
        "fixture_eligibility_breakdown": {
            k: _count("fixture_eligibility", k) for k in (
                FIXTURE_VERIFIED_FUTURE, FIXTURE_STALE, FIXTURE_KICKOFF_UNKNOWN,
                FIXTURE_IDENTITY_UNRESOLVED, FIXTURE_SCHEDULE_UNVERIFIED,
                FIXTURE_MARKET_UNAVAILABLE, FIXTURE_POSTGAME)
        },
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
             f"- 真实前瞻赛事 (VERIFIED_FUTURE_FIXTURE): **{comparison['genuine_future_fixture_count']}**",
             f"- 前瞻对比: {comparison['prospective_comparison_count']} / "
             f"回溯诊断: {comparison['retrospective_diagnostic_count']} / "
             f"前瞻被阻: {comparison['prospective_blocked_count']}",
             f"- 存储 upcoming 但已过期: {comparison['stale_stored_upcoming_count']} / "
             f"开赛时间未知: {comparison['kickoff_unknown_count']} / "
             f"身份未决: {comparison['identity_unresolved_count']} / "
             f"排期状态未核验: {comparison['schedule_status_unverified_count']} / "
             f"盘口不可用: {comparison['market_unavailable_count']}",
             f"- V2 可计算 (ELIGIBLE): {comparison['eligible_fixture_count']} / "
             f"不足: {comparison['insufficient_fixture_count']}", "",
             "| 赛事 | 日期 | 主 vs 客 | 资格 | 对比分类 | V1 主胜 | V2 主胜 | 市场主胜 | 主/客真实场次 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in comparison["comparison"]:
        v2h = f"{r['v2_home_probability']:.3f}" if r["v2_home_probability"] is not None else "—"
        mh = f"{r['market_home_probability']:.3f}" if r["market_home_probability"] is not None else "—"
        lines.append(
            f"| {r['canonical_match_id']} | {r['fixture_date']} | {r['home_team']} vs {r['away_team']} | "
            f"{r['fixture_eligibility']} | {r['comparison_classification']} | "
            f"{r['v1_home_probability']:.3f} | {v2h} | {mh} | "
            f"{r['home_real_games_used']}/{r['away_real_games_used']} |")
    lines += ["", "> V1 = `basketball-margin-1`（冷启动，未改动）；V2 = `basketball-margin-2-shadow`（真实历史实力）。",
              "> 仅 `VERIFIED_FUTURE_FIXTURE`（BALLDONTLIE 唯一绑定 + 已核验溯源 + 当前 scheduled 观测 + 主客完全一致 + 有效盘口 + 开赛晚于 as_of）才是真实前瞻对比；",
              "> 过期/历史赛事仅作 `RETROSPECTIVE_MODEL_DIAGNOSTIC`，开赛时间未知或身份未决为 `PROSPECTIVE_COMPARISON_BLOCKED`。",
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