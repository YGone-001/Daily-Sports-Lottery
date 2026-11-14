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


def build_shadow_artifact(
    fixture: dict,
    observation: dict | None,
    dataset: dict,
    *,
    as_of: datetime,
    provider_binding_count: int | None,
    market_observed_at: datetime | str | None,
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
    market_at = _aware(market_observed_at)
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
    v1_version = config.MODEL_VERSIONS["basketball"]
    identity = _digest(f"{canonical_id}|{provider_game_id}|{v1_version}|{V2_VERSION}|{as_of.isoformat()}")
    market_observation = {
        "source": "data/daily_matches.json (stored 500.com market, read-only)",
        "observed_at": market_at.isoformat(),
        "observation_age_seconds": int((as_of - market_at).total_seconds()),
        "moneyline_odds": {"home_win": odds["home_win"], "away_win": odds["away_win"]},
        "normalized_probabilities": {"home_win": round(market_home, 6), "away_win": round(1 - market_home, 6)},
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
        "market_observation_digest": _digest(market_observation),
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
        "market_observation": market_observation,
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
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("content_sha256") == artifact["content_sha256"]:
            return path, "IDEMPOTENT_EXISTING"
        raise ShadowCaptureBlocked("SHADOW_ARTIFACT_IMMUTABLE")
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
    market_observed_at = (daily.get("meta") or {}).get("last_refresh")
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
    try:
        artifact = build_shadow_artifact(bound, obs, dataset, as_of=as_of,
                                         provider_binding_count=counts.get(str(gid)) if gid else None,
                                         market_observed_at=market_observed_at)
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
