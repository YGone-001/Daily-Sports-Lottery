"""NBA 真实前瞻 V1/V2 影子对比捕获（只写本机 reports/ 下的不可变工件）。

用法
----
    # 捕获（默认拒绝网络 → 无当前提供商观测 → BLOCKED，不产出工件）
    python -m tools.nba_shadow_capture capture --dataset <certified-dataset.json> \
        --match-id <canonical_id> --output-dir reports/phase3y/prospective-shadow [--allow-network]

    # 校验既有工件内容摘要
    python -m tools.nba_shadow_capture verify <artifact.json>

边界
----
- 只读 ``data/daily_matches.json``；提供商绑定只在内存副本上执行，绝不写七个受保护存储；
- 工件按 ``snapshot_identity`` 独占创建，已存在且内容不同即拒绝（不可覆盖）；
- 历史数据集必须已认证；V2 只使用 ``game_end < prediction_as_of`` 的真实完赛；
- ``prediction_as_of <= generated_at < verified_kickoff``；赛后不得重建预测；
- 不替换生产模型、不写 ``prediction_snapshots.json``、不结算、不评估写库。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from models.basketball_model_v2 import MODEL_VERSION as V2_VERSION, STATUS_OK, predict_from_inventory
from tools.nba_model_comparison import (
    FIXTURE_VERIFIED_FUTURE,
    _market_home,
    _read_upcoming_nba_fixtures,
    _v1_for_fixture,
    classify_fixture_eligibility,
    provider_binding_counts,
)
from utils.daily_loader import get_beijing_now
from utils.nba_schedule import (
    BEIJING_TZ,
    PROVIDER_BALLDONTLIE_NBA_GAMES,
    apply_nba_schedule_time_enrichment,
    fetch_nba_schedule_observations,
)
from utils.nba_team_strength import compute_team_strength, game_end_utc
from utils.odds_snapshots import latest_odds_snapshot
from utils.prediction_snapshots import get_snapshots_for_match

ARTIFACT_KIND = "NBA_PROSPECTIVE_SHADOW_COMPARISON"
SCHEMA_VERSION = "1.0.0"
CAPTURE_MODE = "CONTEMPORANEOUS_PROSPECTIVE_CAPTURE"


class ShadowCaptureBlocked(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _digest(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _aware(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def content_sha256(artifact: dict) -> str:
    return _digest({k: v for k, v in artifact.items() if k != "content_sha256"})


def market_observation_from_odds_snapshot(snapshot: dict) -> dict:
    odds = snapshot.get("odds") or {}
    obs = {
        "canonical_match_id": snapshot.get("match_id"),
        "lottery_competition_number": snapshot.get("jczq_no"),
        "home_team": snapshot.get("home_team"),
        "away_team": snapshot.get("away_team"),
        "market_type": "sf",
        "home_win_odds": odds.get("home_win"),
        "away_win_odds": odds.get("away_win"),
        "market_source": snapshot.get("source") or "500.com",
        "market_observed_at": snapshot.get("captured_at"),
    }
    obs["market_content_digest"] = _digest({k: v for k, v in obs.items() if k != "market_content_digest"})
    return obs


def verify_market_observation(
    fixture: dict,
    market_observation: dict | None,
    *,
    as_of: datetime,
) -> dict:
    """按比赛级别精确校验市场盘口观察证据（Workstream B.1）。"""
    if not isinstance(market_observation, dict):
        raise ShadowCaptureBlocked("MISSING_MATCH_ODDS_PROVENANCE")

    canonical_id = fixture.get("id")
    if market_observation.get("canonical_match_id") != canonical_id:
        raise ShadowCaptureBlocked("MARKET_EVENT_IDENTITY_MISMATCH")

    # 校验彩票编号一致性（如存在）
    obs_no = market_observation.get("lottery_competition_number")
    fix_no = fixture.get("jczq_no")
    if obs_no and fix_no and str(obs_no) != str(fix_no):
        raise ShadowCaptureBlocked("MARKET_EVENT_IDENTITY_MISMATCH")

    # 校验双方球队身份一致性
    if market_observation.get("home_team") != fixture.get("home") or \
            market_observation.get("away_team") != fixture.get("away"):
        raise ShadowCaptureBlocked("MARKET_EVENT_IDENTITY_MISMATCH")

    # 校验盘口赔率数值与观察完全一致
    fixture_odds = fixture.get("odds") or {}
    fix_hw = fixture_odds.get("home_win")
    fix_aw = fixture_odds.get("away_win")
    obs_hw = market_observation.get("home_win_odds")
    obs_aw = market_observation.get("away_win_odds")
    if obs_hw is None or obs_aw is None or fix_hw is None or fix_aw is None:
        raise ShadowCaptureBlocked("MISSING_MATCH_ODDS_PROVENANCE")
    try:
        if abs(float(obs_hw) - float(fix_hw)) > 1e-6 or abs(float(obs_aw) - float(fix_aw)) > 1e-6:
            raise ShadowCaptureBlocked("ODDS_VALUES_DO_NOT_MATCH_OBSERVATION")
    except (ValueError, TypeError):
        raise ShadowCaptureBlocked("ODDS_VALUES_DO_NOT_MATCH_OBSERVATION")

    # 校验观察时间戳早于或等于预测决策时刻
    market_at = _aware(market_observation.get("market_observed_at"))
    if market_at is None:
        raise ShadowCaptureBlocked("MISSING_MATCH_ODDS_PROVENANCE")
    if market_at > as_of:
        raise ShadowCaptureBlocked("MARKET_OBSERVATION_AFTER_PREDICTION")

    # 校验内容摘要完整性
    expected_digest = _digest({k: v for k, v in market_observation.items() if k != "market_content_digest"})
    given_digest = market_observation.get("market_content_digest")
    if given_digest is not None and given_digest != expected_digest:
        raise ShadowCaptureBlocked("ODDS_VALUES_DO_NOT_MATCH_OBSERVATION")

    obs = dict(market_observation)
    obs["market_observed_at"] = market_at.isoformat()
    obs["market_content_digest"] = expected_digest
    return obs


def count_canonical_owners(provider_game_id: str | int, canonical_matches: list[dict]) -> int:
    """跨全部 Canonical 状态统计 BDL 赛事 ID 的拥有者数量（Workstream B.2）。"""
    gid_str = str(provider_game_id)
    count = 0
    for match in canonical_matches:
        prov = match.get("kickoff_provenance")
        if isinstance(prov, dict):
            if str(prov.get("kickoff_source_event_id") or "") == gid_str:
                count += 1
        elif str(match.get("provider_game_id") or "") == gid_str:
            count += 1
    return count


def has_unresolved_reversed_orientation(fixture: dict, canonical_matches: list[dict]) -> bool:
    """检查是否存在未决的主客反向记录（Workstream B.2）。"""
    home = fixture.get("home")
    away = fixture.get("away")
    date = fixture.get("date")
    sport = fixture.get("sport") or "basketball"
    for m in canonical_matches:
        m_sport = m.get("sport") or "basketball"
        if m_sport != sport or m.get("date") != date:
            continue
        if m.get("home") == away and m.get("away") == home:
            return True
        if m.get("_orientation_status") == "REVERSED_HOME_AWAY":
            return True
    return False


def build_shadow_artifact(
    fixture: dict,
    observation: dict | None,
    dataset: dict,
    *,
    as_of: datetime,
    provider_binding_count: int | None = None,
    selected_provider_game_id_owner_count: int | None = None,
    all_canonical_matches: list[dict] | None = None,
    market_observation: dict | None = None,
    market_observed_at: datetime | str | None = None,
    canonical_owner_conflict: bool = False,
    generated_at: datetime | None = None,
) -> dict:
    """构造单场前瞻影子对比工件（纯计算，不写盘）；任一前置条件不满足即 ``ShadowCaptureBlocked``。"""
    if as_of.tzinfo is None:
        raise ShadowCaptureBlocked("AS_OF_NOT_TIMEZONE_AWARE")
    cert = dataset.get("certification") if isinstance(dataset.get("certification"), dict) else {}
    if dataset.get("regular_season_only") is not True \
            or cert.get("REGULAR_SEASON_DATASET_CERTIFIED") != "VERIFIED" \
            or cert.get("CURRENT_SEASON_WINDOW_COMPLETE") != "VERIFIED":
        raise ShadowCaptureBlocked("HISTORICAL_DATASET_NOT_CERTIFIED")
    window_cutoff = _aware(dataset.get("cutoff_utc"))
    if window_cutoff is None or window_cutoff > as_of:
        raise ShadowCaptureBlocked("HISTORICAL_WINDOW_CUTOFF_INVALID")

    gate = classify_fixture_eligibility(fixture, as_of, provider_observation=observation,
                                        provider_binding_count=provider_binding_count,
                                        canonical_owner_conflict=canonical_owner_conflict)
    if gate["fixture_eligibility"] != FIXTURE_VERIFIED_FUTURE:
        raise ShadowCaptureBlocked(f"{gate['fixture_eligibility']}:{gate['disqualification_reason']}")
    kickoff = _aware(gate["verified_kickoff"])
    generated = (generated_at or get_beijing_now()).astimezone(BEIJING_TZ)
    if generated < as_of:
        raise ShadowCaptureBlocked("AS_OF_AFTER_GENERATION_TIME")
    if not generated < kickoff:
        raise ShadowCaptureBlocked("PREDICTION_NOT_BEFORE_KICKOFF")

    # B.1 比赛级别盘口证据精确解析与校验
    if market_observation is not None:
        verified_market_obs = verify_market_observation(fixture, market_observation, as_of=as_of)
    else:
        snap = latest_odds_snapshot(fixture.get("id"))
        if snap is not None:
            candidate_obs = market_observation_from_odds_snapshot(snap)
            verified_market_obs = verify_market_observation(fixture, candidate_obs, as_of=as_of)
        else:
            raise ShadowCaptureBlocked("MISSING_MATCH_ODDS_PROVENANCE")

    market_at = _aware(verified_market_obs["market_observed_at"])
    if market_at is None or market_at > as_of:
        raise ShadowCaptureBlocked("MARKET_OBSERVATION_NOT_BEFORE_AS_OF")

    games = dataset.get("games", [])
    if any(_aware(g.get("scheduled_start_utc")) is None for g in games):
        raise ShadowCaptureBlocked("HISTORICAL_INPUT_TIMESTAMP_INVALID")
    used = [g for g in games if g.get("regular_season_eligible") is not False
            and (end := game_end_utc(g)) is not None and end < as_of]
    max_end = max(game_end_utc(g) for g in used) if used else None
    inventory = compute_team_strength(used, as_of=as_of)

    hc, ac = fixture["_home_code"], fixture["_away_code"]
    v1 = _v1_for_fixture(fixture)
    v2 = predict_from_inventory(inventory, hc, ac, odds=fixture.get("odds"), fixture_kickoff=kickoff)
    if v2.get("status") != STATUS_OK:
        raise ShadowCaptureBlocked("INSUFFICIENT_REAL_TEAM_DATA")
    odds = fixture["odds"]
    market_home = _market_home(odds)
    if market_home is None or not math.isfinite(market_home):
        raise ShadowCaptureBlocked("MARKET_ODDS_INVALID")

    canonical_id = fixture["id"]
    provider_game_id = gate["provider_game_id"]

    # B.2 全量 Canonical 归属唯一性校验
    if all_canonical_matches is not None:
        mem_canonical = [fixture if m.get("id") == fixture.get("id") else m for m in all_canonical_matches]
        if not any(m.get("id") == fixture.get("id") for m in mem_canonical):
            mem_canonical.append(fixture)
        if has_unresolved_reversed_orientation(fixture, mem_canonical):
            raise ShadowCaptureBlocked("REVERSED_HOME_AWAY_REJECTED")
        owner_count = count_canonical_owners(provider_game_id, mem_canonical)
        if owner_count != 1:
            raise ShadowCaptureBlocked("CANONICAL_OWNER_CONFLICT")
    elif selected_provider_game_id_owner_count is not None:
        if selected_provider_game_id_owner_count != 1:
            raise ShadowCaptureBlocked("CANONICAL_OWNER_CONFLICT")
    elif provider_binding_count is not None and provider_binding_count != 1:
        raise ShadowCaptureBlocked("CANONICAL_OWNER_CONFLICT")

    v1_version = config.MODEL_VERSIONS["basketball"]
    identity = _digest(f"{canonical_id}|{provider_game_id}|{v1_version}|{V2_VERSION}|{as_of.isoformat()}")

    # 对比既有生产 V1 快照（如有，只读核对，绝不改写）
    prod_snaps = get_snapshots_for_match(canonical_id)
    prod_v1 = prod_snaps[-1] if prod_snaps else None

    market_observation_record = {
        "source": verified_market_obs.get("market_source") or "500.com",
        "canonical_match_id": canonical_id,
        "lottery_competition_number": fixture.get("jczq_no"),
        "home_team": fixture.get("home"),
        "away_team": fixture.get("away"),
        "market_type": verified_market_obs.get("market_type") or "sf",
        "observed_at": market_at.isoformat(),
        "observation_age_seconds": int((as_of - market_at).total_seconds()),
        "moneyline_odds": {"home_win": odds["home_win"], "away_win": odds["away_win"]},
        "normalized_probabilities": {"home_win": round(market_home, 6), "away_win": round(1 - market_home, 6)},
        "market_content_digest": verified_market_obs.get("market_content_digest"),
    }
    fixture_view = {k: fixture.get(k) for k in ("id", "date", "time", "home", "away", "league", "jczq_no",
                                                  "market_tracked", "status", "kickoff_provenance")}
    v1_raw = v1["model_probabilities"]["home_win"] / 100.0
    artifact = {
        "artifact_kind": ARTIFACT_KIND,
        "schema_version": SCHEMA_VERSION,
        "capture_mode": CAPTURE_MODE,
        "snapshot_identity": identity,
        "canonical_match_id": canonical_id,
        "bdl_provider_game_id": provider_game_id,
        "home_team": fixture.get("home"),
        "away_team": fixture.get("away"),
        "home_code": hc,
        "away_code": ac,
        "model_versions": {"v1": v1_version, "v2_shadow": V2_VERSION},
        "prediction_as_of": as_of.isoformat(),
        "generated_at": generated.isoformat(),
        "verified_kickoff": kickoff.isoformat(),
        "scheduled_kickoff_utc": observation.get("scheduled_start_utc"),
        "scheduled_kickoff_beijing": kickoff.astimezone(BEIJING_TZ).isoformat(),
        "time_to_kickoff_seconds_at_generation": int((kickoff - generated).total_seconds()),
        "source_provenance": {
            "schedule_provider": PROVIDER_BALLDONTLIE_NBA_GAMES,
            "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
            "official_nba_verified": False,
            "source_observed_at": observation.get("observed_at"),
            "source_status_state": observation.get("status_state"),
            "source_postponed": observation.get("postponed"),
            "eligibility_gate": gate,
        },
        "input_dataset_digest": _digest([g.get("source_content_digest") or g.get("provider_game_id")
                                         for g in games]),
        "input_dataset_game_count": len(games),
        "historical_window_cutoff": window_cutoff.isoformat(),
        "historical_dataset_certification": cert,
        "input_fixture_digest": _digest(fixture_view),
        "market_observation_digest": _digest(market_observation_record),
        "temporal_input_verification": {
            "rule": "historical_game_end < prediction_as_of",
            "inputs_eligible_before_as_of": len(used),
            "inputs_excluded_at_or_after_as_of": len(games) - len(used),
            "strength_engine_games_used": inventory["games_used"],
            "latest_input_game_end": max_end.isoformat() if max_end else None,
            "satisfied": max_end is None or max_end < as_of,
            "used_game_ids_digest": _digest(sorted(g["provider_game_id"] for g in used)),
            "reconstructed_historical_research": False,
        },
        "original_probabilities": {
            "v1_raw": {"home_win": round(v1_raw, 6), "away_win": round(1 - v1_raw, 6)},
            "v1_display": {"home_win": v1["probabilities"]["home_win"] / 100.0,
                           "away_win": v1["probabilities"]["away_win"] / 100.0},
            "v2_raw": {"home_win": round(v2["raw_model_probabilities"]["home_win"], 6),
                       "away_win": round(v2["raw_model_probabilities"]["away_win"], 6)},
            "v2_display": {"home_win": round(v2["display_probabilities"]["home_win"], 6),
                           "away_win": round(v2["display_probabilities"]["away_win"], 6)},
        },
        "v2_inputs": {
            "home_historical_games_used": v2["home_games_used"],
            "away_historical_games_used": v2["away_games_used"],
            "home_historical_elo": v2.get("home_historical_elo"),
            "away_historical_elo": v2.get("away_historical_elo"),
            "margin_components": v2.get("margin_components"),
        },
        "market_observation": market_observation_record,
        "production_v1_comparison": {
            "snapshot_exists": prod_v1 is not None,
            "production_snapshot_id": prod_v1.get("snapshot_id") if prod_v1 else None,
            "production_model_version": prod_v1.get("model_version") if prod_v1 else None,
            "production_probabilities": prod_v1.get("probabilities") if prod_v1 else None,
            "identical_to_recomputed_v1": (prod_v1.get("probabilities") == v1.get("probabilities")) if prod_v1 else None,
        },
        "production_model_unchanged": True,
        "writes_protected_stores": False,
        "promotion_authorized": False,
    }
    artifact["content_sha256"] = content_sha256(artifact)
    return artifact


def _inside(path: Path, root: Path) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    r = os.path.normcase(os.path.realpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def write_shadow_artifact(artifact: dict, output_dir: str | Path) -> tuple[Path, str]:
    """独占创建工件文件；同一身份已存在且内容一致 → ``IDEMPOTENT_EXISTING``，否则拒绝覆盖。"""
    out = Path(output_dir)
    if _inside(out, Path(config.DATA_DIR)):
        raise ShadowCaptureBlocked("OUTPUT_DIR_INSIDE_DATA_DIR")
    if artifact.get("content_sha256") != content_sha256(artifact):
        raise ShadowCaptureBlocked("ARTIFACT_CONTENT_DIGEST_MISMATCH")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{artifact['snapshot_identity']}.json"
    body = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raise ShadowCaptureBlocked("EXISTING_ARTIFACT_TAMPERED")

        # 1. 重新计算已有文件内容摘要并验证篡改
        recomputed_sha = content_sha256(existing)
        if recomputed_sha != existing.get("content_sha256"):
            raise ShadowCaptureBlocked("EXISTING_ARTIFACT_TAMPERED")

        # 2. 校验原始快照身份
        if existing.get("snapshot_identity") != artifact["snapshot_identity"]:
            raise ShadowCaptureBlocked("SHADOW_ARTIFACT_IMMUTABLE")

        # 3. 完整内容逐字段对比，拒绝不一致修改，永不静默覆盖
        if existing != artifact:
            raise ShadowCaptureBlocked("SHADOW_ARTIFACT_IMMUTABLE")

        return path, "IDEMPOTENT_EXISTING"
    return path, "CREATED"


def verify_shadow_artifact(path: str | Path) -> dict:
    artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    recomputed = content_sha256(artifact)
    return {"path": str(path), "snapshot_identity": artifact.get("snapshot_identity"),
            "content_sha256": artifact.get("content_sha256"), "recomputed_sha256": recomputed,
            "valid": recomputed == artifact.get("content_sha256")}


def evaluate_shadow_artifact(artifact: dict, final_result: dict) -> dict:
    """只读评估契约：按同一 Canonical ID + BDL 赛事 ID 连接原始概率与官方确认赛果，不写任何存储。"""
    if artifact.get("content_sha256") != content_sha256(artifact):
        return {"status": "REJECTED", "reason": "ARTIFACT_CONTENT_DIGEST_MISMATCH"}
    if (final_result.get("canonical_match_id") != artifact["canonical_match_id"]
            or str(final_result.get("provider_game_id")) != artifact["bdl_provider_game_id"]):
        return {"status": "REJECTED", "reason": "EVENT_IDENTITY_MISMATCH"}
    if str(final_result.get("status_state") or "").lower() != "final" or final_result.get("officially_confirmed") is not True:
        return {"status": "PENDING", "reason": "RESULT_NOT_OFFICIALLY_CONFIRMED"}
    hs, aws = final_result.get("home_score"), final_result.get("away_score")
    if type(hs) is not int or type(aws) is not int or hs == aws:
        return {"status": "REJECTED", "reason": "MALFORMED_FINAL_SCORE"}
    y = 1.0 if hs > aws else 0.0
    probs = artifact["original_probabilities"]
    market = artifact["market_observation"]["normalized_probabilities"]["home_win"]

    def score(p):
        p = min(max(p, 1e-12), 1 - 1e-12)
        return {"home_probability": p, "binary_brier": round((p - y) ** 2, 6),
                "log_loss": round(-(y * math.log(p) + (1 - y) * math.log(1 - p)), 6),
                "direction_correct": (p > 0.5) == (y == 1.0)}

    return {"status": "EVALUATED", "snapshot_identity": artifact["snapshot_identity"],
            "content_sha256": artifact["content_sha256"], "home_won": y == 1.0,
            "v1": score(probs["v1_raw"]["home_win"]), "v2_shadow": score(probs["v2_raw"]["home_win"]),
            "market": score(market), "writes_protected_stores": False}


def _capture(args) -> dict:
    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8-sig"))
    fixtures = _read_upcoming_nba_fixtures()
    target = next((m for m in fixtures if m.get("id") == args.match_id), None)
    if target is None:
        return {"status": "BLOCKED", "reason": "TARGET_NOT_IN_STORED_UPCOMING_NBA"}
    daily = json.loads((Path(config.DATA_DIR) / "daily_matches.json").read_text(encoding="utf-8-sig"))
    all_canonical_matches = daily.get("matches", [])
    if not args.allow_network:
        return {"status": "BLOCKED", "reason": "FRESH_PROVIDER_OBSERVATION_UNAVAILABLE",
                "network_requests": 0}
    observations, diag = fetch_nba_schedule_observations([target["date"]], allow_network=True, max_requests=3)
    safe_diag = {k: diag.get(k) for k in ("status", "coverage_status", "rows", "errors", "checked_at")}
    # 提供商绑定只在内存副本上执行，绝不回写受保护存储。
    copies = copy.deepcopy(fixtures)
    as_of = get_beijing_now()
    enrichment = apply_nba_schedule_time_enrichment(copies, observations, now=as_of)
    decision = next((d for d in enrichment["decisions"] if d["match_id"] == args.match_id), None)
    bound = next(m for m in copies if m.get("id") == args.match_id)
    obs = decision.get("observation") if decision and decision.get("bound") else None
    counts = provider_binding_counts(copies)
    gid = (bound.get("kickoff_provenance") or {}).get("kickoff_source_event_id")

    # B.1 尝试从单场赔率历史存储中获取真实赛前盘口证据
    snap = latest_odds_snapshot(target["id"])
    market_obs = market_observation_from_odds_snapshot(snap) if snap else None

    try:
        artifact = build_shadow_artifact(bound, obs, dataset, as_of=as_of,
                                         provider_binding_count=counts.get(str(gid)) if gid else None,
                                         all_canonical_matches=all_canonical_matches,
                                         market_observation=market_obs)
        path, outcome = write_shadow_artifact(artifact, args.output_dir)
    except ShadowCaptureBlocked as exc:
        return {"status": "BLOCKED", "reason": exc.reason, "provider_diagnostics": safe_diag,
                "binding_reason": decision.get("reason") if decision else None}
    return {"status": "CAPTURED", "outcome": outcome, "path": str(path),
            "snapshot_identity": artifact["snapshot_identity"], "content_sha256": artifact["content_sha256"],
            "provider_diagnostics": safe_diag}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--dataset", required=True)
    cap.add_argument("--match-id", required=True)
    cap.add_argument("--output-dir", required=True)
    cap.add_argument("--allow-network", action="store_true")
    ver = sub.add_parser("verify")
    ver.add_argument("artifact")
    args = parser.parse_args()
    result = _capture(args) if args.command == "capture" else verify_shadow_artifact(args.artifact)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "verify":
        return 0 if result["valid"] else 9
    return 0 if result["status"] == "CAPTURED" else 3


if __name__ == "__main__":
    sys.exit(main())
