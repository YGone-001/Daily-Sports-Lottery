"""离线单场 NBA 赛果绑定；preview 不写运行库，execute 需另获操作者许可。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

import config
from utils import evaluation_rows, prediction_snapshots, refresh_state, settlements
from utils.atomic_json import atomic_write_json
from utils.concurrency import acquire_refresh_lock, acquire_sidecar_lock
from utils.daily_loader import get_beijing_now, get_match_datetime
from utils.match_identity import competition_number_for_match, same_event
from utils.match_lifecycle import resolve_match_update
from utils.nba_schedule import resolve_canonical_team_code, resolve_provider_team_code


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(dt.tzinfo is not None, "TIMEZONE_REQUIRED")
    return dt


def bind_result(canonical, snapshot, official, bdl, *, observed_at, source_reference):
    """纯函数：依赖已存 BDL 赛事身份，不使用500反向候选或编号解释。"""
    _require(canonical.get("sport") == snapshot.get("sport") == "basketball", "SPORT_MISMATCH")
    _require(canonical.get("market_tracked") is True and competition_number_for_match(canonical) is not None,
             "TRACKED_MARKET_IDENTITY_REQUIRED")
    _require(canonical.get("id") == snapshot.get("match_id"), "SNAPSHOT_MATCH_MISMATCH")
    _require(snapshot.get("snapshot_id") == prediction_snapshots.snapshot_id_for(
        canonical["id"], snapshot["model_version"]), "SNAPSHOT_ID_MISMATCH")
    kickoff = get_match_datetime(canonical)
    _require(kickoff is not None and _time(snapshot["generated_at"]) < kickoff
             and _time(snapshot["kickoff_at"]) == kickoff, "INVALID_PREGAME_TIME")
    provenance = canonical.get("kickoff_provenance") or {}
    _require(provenance.get("actual_kickoff_time_source") == "balldontlie_nba_games"
             and provenance.get("kickoff_verification_status") == "SCHEDULE_PROVIDER_VERIFIED",
             "PRIOR_PROVIDER_BINDING_REQUIRED")
    _require(type(bdl.get("id")) is int and str(bdl["id"]) == provenance.get("kickoff_source_event_id"),
             "PROVIDER_GAME_ID_MISMATCH")
    _require(_time(provenance["kickoff_source_scheduled_start_utc"]) == kickoff
             and _time(official["gameTimeUTC"]) == kickoff and _time(bdl["datetime"]) == kickoff,
             "SOURCE_KICKOFF_MISMATCH")
    _require(_time(observed_at) >= kickoff and _time(observed_at) <= get_beijing_now(),
             "INVALID_OBSERVATION_TIME")
    gid = official.get("gameId")
    _require(isinstance(gid, str) and re.fullmatch(r"[0-9]{10}", gid) is not None
             and source_reference == f"https://www.nba.com/game/mil-vs-orl-{gid}",
             "OFFICIAL_REFERENCE_MISMATCH")
    # This intentionally scoped workflow supports only the operator-selected Magic/Bucks fixture.
    _require(official.get("homeTeam", {}).get("teamTricode") == "ORL"
             and official.get("awayTeam", {}).get("teamTricode") == "MIL", "UNSUPPORTED_FIXTURE")
    _require(type(official.get("gameStatus")) is int and official["gameStatus"] == 3
             and official.get("gameStatusText") == "Final"
             and bdl.get("status_state") == "final" and bdl.get("status") == "Final"
             and bdl.get("postponed") is False, "FINAL_CONFIRMATION_REQUIRED")
    scores = []
    for side, other, score_key in [("home", "home", "home_team_score"),
                                    ("away", "visitor", "visitor_team_score")]:
        code = resolve_canonical_team_code(canonical.get(side))
        _require(snapshot.get(side + "_team") == canonical.get(side), "SNAPSHOT_ORIENTATION_MISMATCH")
        nt = official[side + "Team"]; bt = bdl[other + "_team"]
        _require(type(nt.get("teamId")) is int and type(bt.get("id")) is int, "TEAM_ID_REQUIRED")
        _require(code is not None and code == resolve_provider_team_code(f"nba:{nt['teamId']}")
                 == resolve_provider_team_code(f"bdl:{bt['id']}") == nt.get("teamTricode"),
                 "TEAM_ORIENTATION_MISMATCH")
        score = nt.get("score")
        _require(type(score) is int and score >= 0 and type(bdl.get(score_key)) is int
                 and score == bdl[score_key], "SCORE_CONFLICT")
        scores.append(score)
    _require(scores[0] != scores[1], "TIED_NBA_FINAL_REJECTED")
    incoming = copy.deepcopy(canonical)
    incoming.update(status="finished", score={"ft": scores})
    _require(same_event(canonical, incoming), "IDENTITY_CHANGED")
    resolved = resolve_match_update(copy.deepcopy(canonical), incoming)
    _require(resolved.get("status") == "finished" and resolved.get("score", {}).get("ft") == scores,
             "RESULT_NOT_ACCEPTED")
    resolved["result_provenance"] = {
        "provider": "nba_official_game_page", "nba_official_game_id": gid,
        "balldontlie_game_id": str(bdl["id"]), "source_reference": source_reference,
        "observed_at": observed_at, "official_content_sha256": fingerprint(official),
        "bdl_content_sha256": fingerprint(bdl), "human_reviewed": False,
    }
    return resolved


def _read(path):
    # Strict read: do not invoke corruption quarantine on production evidence.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def preview(match_id, snapshot_id, official, bdl, observed_at, source_reference):
    directory = Path(config.DATA_DIR)
    _require(refresh_state.consistency(str(directory))["consistent"], "INCONSISTENT_RUNTIME")
    daily = _read(directory / "daily_matches.json")
    matches = daily["matches"]
    ids = [m["id"] for m in matches]
    _require(len(ids) == len(set(ids)), "CANONICAL_IDS_NOT_UNIQUE")
    target = next(m for m in matches if m["id"] == match_id)
    snapshot = _read(directory / config.PREDICTION_SNAPSHOT_FILE)["snapshots"][snapshot_id]
    resolved = bind_result(target, snapshot, official, bdl, observed_at=observed_at,
                           source_reference=source_reference)
    _require(sum((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id") == str(bdl["id"])
                 for m in matches) == 1, "AMBIGUOUS_PROVIDER_BINDING")
    final_score = settlements.extract_final_score(resolved)
    sid = settlements.settlement_id_for(snapshot_id)
    existing = _read(directory / config.SETTLEMENT_FILE)["settlements"].get(sid)
    if existing:
        _require(existing == settlements.build_settlement(snapshot, resolved, final_score,
                 settled_at=_time(existing["settled_at"])), "EXISTING_SETTLEMENT_CONFLICT")
    candidate = existing or settlements.build_settlement(snapshot, resolved, final_score)
    row = evaluation_rows.build_evaluation_row(snapshot, candidate)
    current_row = _read(directory / config.EVALUATION_ROW_FILE)["rows"].get(row["evaluation_id"])
    _require(not current_row or current_row["source_fingerprint"] == row["source_fingerprint"],
             "EXISTING_EVALUATION_CONFLICT")
    body = {"match_id": match_id, "snapshot_id": snapshot_id, "snapshot_digest": fingerprint(snapshot),
            "resolved_match": resolved, "store_hashes": refresh_state.store_digests(str(directory)),
            "generation": (refresh_state.read_state(str(directory)) or {}).get("generation"),
            "settlement_id": sid, "evaluation_id": row["evaluation_id"],
            "write_scope": ["daily_matches.json", config.SETTLEMENT_FILE, config.EVALUATION_ROW_FILE],
            "network_requests": 0, "canonical_reconciliation": False}
    _require(len(body["store_hashes"]) == 7, "SEVEN_STORES_REQUIRED")
    return dict(body, preview_sha256=fingerprint(body))


def execute(plan, *, expected_preview_sha256, receipt_path):
    """执行已复核预览；软件开关和摘要不替代操作者独立批准。"""
    _require(config.RUNTIME_WRITES_ENABLED, "RUNTIME_WRITES_DISABLED")
    body = {k: v for k, v in plan.items() if k != "preview_sha256"}
    _require(fingerprint(body) == plan["preview_sha256"] == expected_preview_sha256, "PREVIEW_MISMATCH")
    _require(plan["write_scope"] == ["daily_matches.json", config.SETTLEMENT_FILE, config.EVALUATION_ROW_FILE],
             "WRITE_SCOPE_MISMATCH")
    with acquire_refresh_lock(action="single_nba_confirmed_result") as lease:
        with acquire_sidecar_lock(lock_name=".snapshots.lock"):
            _require(refresh_state.consistency(config.DATA_DIR)["consistent"], "INCONSISTENT_RUNTIME")
            _require(refresh_state.store_digests(config.DATA_DIR) == plan["store_hashes"], "STALE_STORES")
            _require((refresh_state.read_state(config.DATA_DIR) or {}).get("generation") == plan["generation"],
                     "STALE_GENERATION")
            # Receipt creation is exclusive; failures never cause an automatic retry.
            with Path(receipt_path).open("x", encoding="utf-8") as fh:
                json.dump({"preview_sha256": expected_preview_sha256, "started_at": get_beijing_now().isoformat()}, fh)
            lease.begin()
            try:
                directory = Path(config.DATA_DIR)
                daily = _read(directory / "daily_matches.json")
                snapshot = _read(directory / config.PREDICTION_SNAPSHOT_FILE)["snapshots"][plan["snapshot_id"]]
                _require(fingerprint(snapshot) == plan["snapshot_digest"], "SNAPSHOT_CHANGED")
                for index, match in enumerate(daily["matches"]):
                    if match["id"] == plan["match_id"]:
                        unchanged = lambda row: {k: v for k, v in row.items()
                                                 if k not in {"status", "score", "result_provenance"}}
                        _require(unchanged(match) == unchanged(plan["resolved_match"]), "IDENTITY_OR_ODDS_CHANGED")
                        daily["matches"][index] = plan["resolved_match"]
                        break
                atomic_write_json(str(directory / "daily_matches.json"), daily)
                settlement, sc = settlements.settle_snapshot(snapshot, plan["resolved_match"])
                row, ec = evaluation_rows.capture_evaluation_row(snapshot, settlement)
                _require(settlement is not None and row is not None, "SETTLEMENT_NOT_CREATED")
                after = refresh_state.store_digests(config.DATA_DIR)
                _require(all(after[n] == h for n, h in plan["store_hashes"].items()
                             if n not in plan["write_scope"]), "UNEXPECTED_STORE_MUTATION")
                marker = lease.complete()
                return {"settlement_id": settlement["settlement_id"], "evaluation_id": row["evaluation_id"],
                        "settlement_created": sc, "evaluation_created": ec, "generation": marker["generation"]}
            except BaseException:
                lease.complete(outcome=refresh_state.STATUS_FAILED, error="SINGLE_NBA_RESULT_FAILED")
                raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--official-page", required=True)
    parser.add_argument("--bdl-response", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--source-reference", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-preview-sha256")
    parser.add_argument("--receipt")
    args = parser.parse_args()
    page = Path(args.official_page).read_text(encoding="utf-8")
    raw = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    _require(raw is not None, "OFFICIAL_PAGE_DATA_MISSING")
    official = json.loads(raw.group(1))["props"]["pageProps"]["game"]
    plan = preview(args.match_id, args.snapshot_id, official, _read(args.bdl_response)["data"],
                   args.observed_at, args.source_reference)
    if args.execute:
        _require(args.expected_preview_sha256 and args.receipt, "EXECUTION_SCOPE_REQUIRED")
        result = execute(plan, expected_preview_sha256=args.expected_preview_sha256, receipt_path=args.receipt)
    else:
        result = plan
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
