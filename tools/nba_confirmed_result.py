"""离线 NBA 赛果绑定（单场或同批多场）；preview 不写运行库，execute 需另获操作者许可。"""
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

WRITE_SCOPE = ["daily_matches.json", config.SETTLEMENT_FILE, config.EVALUATION_ROW_FILE]


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


def _validate_period_reconciliation(official, bdl, period):
    """逐节对账：官方 periods 数组与 BDL 分节字段必须各自加总等于总分，绝不估算加时得分。"""
    for side, other, score_key, prefix in (("home", "home", "home_team_score", "home"),
                                           ("away", "visitor", "visitor_team_score", "visitor")):
        nt = official[side + "Team"]
        periods = nt.get("periods")
        _require(isinstance(periods, list) and len(periods) == period, "PERIOD_DATA_INCOMPLETE")
        official_total = 0
        for idx, entry in enumerate(periods, start=1):
            value = entry.get("score") if isinstance(entry, dict) else None
            _require(type(value) is int and value >= 0 and entry.get("period") == idx
                     and entry.get("periodType") == ("REGULAR" if idx <= 4 else "OVERTIME"),
                     "PERIOD_DATA_INCOMPLETE")
            official_total += value
        _require(official_total == nt.get("score"), "PERIOD_SCORE_RECONCILIATION_FAILED")
        bdl_total = 0
        for idx in range(1, period + 1):
            key = f"{prefix}_q{idx}" if idx <= 4 else f"{prefix}_ot{idx - 4}"
            value = bdl.get(key)
            _require(type(value) is int and value >= 0, "PERIOD_DATA_INCOMPLETE")
            bdl_total += value
        _require(bdl_total == bdl.get(score_key), "PERIOD_SCORE_RECONCILIATION_FAILED")


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
    _require(isinstance(gid, str) and re.fullmatch(r"[0-9]{10}", gid) is not None,
             "OFFICIAL_GAME_ID_MALFORMED")
    period = official.get("period")
    is_final = (type(official.get("gameStatus")) is int and official["gameStatus"] == 3
                and official.get("gameStatusText") == "Final"
                and bdl.get("status_state") == "final" and bdl.get("status") == "Final"
                and bdl.get("postponed") is False)
    if not is_final:
        # 加时进行中（period>=5）与常规时间未完结必须分别陈述，不得统称未确认。
        _require(False, "OVERTIME_IN_PROGRESS" if type(period) is int and period >= 5
                 else "FINAL_CONFIRMATION_REQUIRED")
    _require(type(period) is int and type(bdl.get("period")) is int, "PERIOD_STRUCTURE_REQUIRED")
    _require(period >= 4 and period == bdl["period"], "PERIOD_MISMATCH")
    scores = []
    codes = {}
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
        codes[side] = code
        scores.append(score)
    if scores[0] == scores[1]:
        # 常规时间结束平局不是终场（加时必然随后）；绝不把平局自动转成终场比分。
        _require(False, "REGULATION_TIE_NOT_FINAL" if period == 4 else "TIED_NBA_FINAL_REJECTED")
    # URL 必须由 Canonical 锚定的队码与官方赛事 ID 构造，拒绝任意引用。
    _require(source_reference == f"https://www.nba.com/game/{codes['away'].lower()}-vs-{codes['home'].lower()}-{gid}",
             "OFFICIAL_REFERENCE_MISMATCH")
    _validate_period_reconciliation(official, bdl, period)
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
        "final_period": period, "overtime": period > 4,
    }
    return resolved


def _read(path):
    # Strict read: do not invoke corruption quarantine on production evidence.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _preview_target(matches, snapshots, settlement_store, row_store, entry):
    target = next((m for m in matches if m["id"] == entry["match_id"]), None)
    _require(target is not None, "CANONICAL_MATCH_NOT_FOUND")
    snapshot = snapshots.get(entry["snapshot_id"])
    _require(snapshot is not None, "SNAPSHOT_NOT_FOUND")
    resolved = bind_result(target, snapshot, entry["official"], entry["bdl"],
                           observed_at=entry["observed_at"], source_reference=entry["source_reference"])
    _require(sum((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id") == str(entry["bdl"]["id"])
                 for m in matches) == 1, "AMBIGUOUS_PROVIDER_BINDING")
    final_score = settlements.extract_final_score(resolved)
    sid = settlements.settlement_id_for(entry["snapshot_id"])
    existing = settlement_store.get(sid)
    if existing:
        _require(existing == settlements.build_settlement(snapshot, resolved, final_score,
                 settled_at=_time(existing["settled_at"])), "EXISTING_SETTLEMENT_CONFLICT")
    candidate = existing or settlements.build_settlement(snapshot, resolved, final_score)
    row = evaluation_rows.build_evaluation_row(snapshot, candidate)
    current_row = row_store.get(row["evaluation_id"])
    _require(not current_row or current_row["source_fingerprint"] == row["source_fingerprint"],
             "EXISTING_EVALUATION_CONFLICT")
    return {"match_id": entry["match_id"], "snapshot_id": entry["snapshot_id"],
            "snapshot_digest": fingerprint(snapshot), "resolved_match": resolved,
            "settlement_id": sid, "evaluation_id": row["evaluation_id"]}


def preview_batch(entries):
    """同一批准下的多场预览：一份 store_hashes/generation/preview_sha256 覆盖全部目标。"""
    _require(isinstance(entries, list) and entries, "TARGETS_REQUIRED")
    directory = Path(config.DATA_DIR)
    _require(refresh_state.consistency(str(directory))["consistent"], "INCONSISTENT_RUNTIME")
    daily = _read(directory / "daily_matches.json")
    matches = daily["matches"]
    ids = [m["id"] for m in matches]
    _require(len(ids) == len(set(ids)), "CANONICAL_IDS_NOT_UNIQUE")
    snapshots = _read(directory / config.PREDICTION_SNAPSHOT_FILE)["snapshots"]
    settlement_store = _read(directory / config.SETTLEMENT_FILE)["settlements"]
    row_store = _read(directory / config.EVALUATION_ROW_FILE)["rows"]
    seen_matches, seen_snapshots, seen_bdl = set(), set(), set()
    targets = []
    for entry in entries:
        mid, sid, bid = entry["match_id"], entry["snapshot_id"], str(entry["bdl"]["id"])
        _require(mid not in seen_matches and sid not in seen_snapshots and bid not in seen_bdl,
                 "DUPLICATE_TARGET")
        seen_matches.add(mid); seen_snapshots.add(sid); seen_bdl.add(bid)
        targets.append(_preview_target(matches, snapshots, settlement_store, row_store, entry))
    body = {"targets": targets,
            "store_hashes": refresh_state.store_digests(str(directory)),
            "generation": (refresh_state.read_state(str(directory)) or {}).get("generation"),
            "write_scope": list(WRITE_SCOPE), "network_requests": 0, "canonical_reconciliation": False}
    _require(len(body["store_hashes"]) == 7, "SEVEN_STORES_REQUIRED")
    return dict(body, preview_sha256=fingerprint(body))


def preview(match_id, snapshot_id, official, bdl, observed_at, source_reference):
    return preview_batch([{"match_id": match_id, "snapshot_id": snapshot_id, "official": official,
                           "bdl": bdl, "observed_at": observed_at, "source_reference": source_reference}])


def execute(plan, *, expected_preview_sha256, receipt_path):
    """执行已复核预览（单场或同批多场，一次操作）；软件开关和摘要不替代操作者独立批准。"""
    _require(config.RUNTIME_WRITES_ENABLED, "RUNTIME_WRITES_DISABLED")
    body = {k: v for k, v in plan.items() if k != "preview_sha256"}
    _require(fingerprint(body) == plan["preview_sha256"] == expected_preview_sha256, "PREVIEW_MISMATCH")
    _require(plan["write_scope"] == WRITE_SCOPE, "WRITE_SCOPE_MISMATCH")
    targets = plan["targets"]
    _require(isinstance(targets, list) and targets, "TARGETS_REQUIRED")
    with acquire_refresh_lock(action="nba_confirmed_result") as lease:
        with acquire_sidecar_lock(lock_name=".snapshots.lock"):
            _require(refresh_state.consistency(config.DATA_DIR)["consistent"], "INCONSISTENT_RUNTIME")
            _require(refresh_state.store_digests(config.DATA_DIR) == plan["store_hashes"], "STALE_STORES")
            _require((refresh_state.read_state(config.DATA_DIR) or {}).get("generation") == plan["generation"],
                     "STALE_GENERATION")
            # Receipt creation is exclusive; failures never cause an automatic retry.
            receipt = {"preview_sha256": expected_preview_sha256,
                       "started_at": get_beijing_now().isoformat(), "target_count": len(targets)}
            with Path(receipt_path).open("x", encoding="utf-8") as fh:
                json.dump(receipt, fh)
            lease.begin()
            results = []
            try:
                directory = Path(config.DATA_DIR)
                daily = _read(directory / "daily_matches.json")
                snapshots = _read(directory / config.PREDICTION_SNAPSHOT_FILE)["snapshots"]
                by_id = {m["id"]: i for i, m in enumerate(daily["matches"])}
                unchanged = lambda row: {k: v for k, v in row.items()
                                         if k not in {"status", "score", "result_provenance"}}
                # 整批预检先于任何写入：任一目标失配即零写入失败。
                for t in targets:
                    snapshot = snapshots.get(t["snapshot_id"])
                    _require(snapshot is not None and fingerprint(snapshot) == t["snapshot_digest"],
                             "SNAPSHOT_CHANGED")
                    index = by_id.get(t["match_id"])
                    _require(index is not None
                             and unchanged(daily["matches"][index]) == unchanged(t["resolved_match"]),
                             "IDENTITY_OR_ODDS_CHANGED")
                for t in targets:
                    daily["matches"][by_id[t["match_id"]]] = t["resolved_match"]
                atomic_write_json(str(directory / "daily_matches.json"), daily)
                for t in targets:
                    snapshot = snapshots[t["snapshot_id"]]
                    settlement, sc = settlements.settle_snapshot(snapshot, t["resolved_match"])
                    row, ec = evaluation_rows.capture_evaluation_row(snapshot, settlement)
                    _require(settlement is not None and row is not None, "SETTLEMENT_NOT_CREATED")
                    results.append({"match_id": t["match_id"], "settlement_id": settlement["settlement_id"],
                                    "evaluation_id": row["evaluation_id"],
                                    "settlement_created": sc, "evaluation_created": ec})
                after = refresh_state.store_digests(config.DATA_DIR)
                _require(all(after[n] == h for n, h in plan["store_hashes"].items()
                             if n not in plan["write_scope"]), "UNEXPECTED_STORE_MUTATION")
                marker = lease.complete()
                receipt.update(status="COMPLETED", results=results, generation=marker["generation"])
                atomic_write_json(str(receipt_path), receipt)
                return {"status": "COMPLETED", "results": results, "generation": marker["generation"]}
            except BaseException as exc:
                lease.complete(outcome=refresh_state.STATUS_FAILED, error="NBA_RESULT_BATCH_FAILED")
                receipt.update(status="FAILED", error=str(exc), completed_targets=results)
                try:
                    atomic_write_json(str(receipt_path), receipt)
                except OSError:
                    pass
                raise


def _official_game(page_path):
    page = Path(page_path).read_text(encoding="utf-8")
    raw = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    _require(raw is not None, "OFFICIAL_PAGE_DATA_MISSING")
    return json.loads(raw.group(1))["props"]["pageProps"]["game"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match-id")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--official-page")
    parser.add_argument("--bdl-response")
    parser.add_argument("--observed-at")
    parser.add_argument("--source-reference")
    parser.add_argument("--targets-file",
                        help="同批多场目标 JSON 数组：match_id/snapshot_id/official_page/bdl_response/observed_at/source_reference")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-preview-sha256")
    parser.add_argument("--receipt")
    args = parser.parse_args()
    if args.targets_file:
        entries = [{"match_id": item["match_id"], "snapshot_id": item["snapshot_id"],
                    "official": _official_game(item["official_page"]),
                    "bdl": _read(item["bdl_response"])["data"],
                    "observed_at": item["observed_at"], "source_reference": item["source_reference"]}
                   for item in _read(args.targets_file)]
        plan = preview_batch(entries)
    else:
        _require(all([args.match_id, args.snapshot_id, args.official_page, args.bdl_response,
                      args.observed_at, args.source_reference]), "SINGLE_TARGET_ARGS_REQUIRED")
        plan = preview(args.match_id, args.snapshot_id, _official_game(args.official_page),
                       _read(args.bdl_response)["data"], args.observed_at, args.source_reference)
    if args.execute:
        _require(args.expected_preview_sha256 and args.receipt, "EXECUTION_SCOPE_REQUIRED")
        result = execute(plan, expected_preview_sha256=args.expected_preview_sha256, receipt_path=args.receipt)
    else:
        result = plan
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()