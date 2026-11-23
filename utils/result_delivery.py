"""篮球赛果投递诊断与证据请求
============================
只读、离线：只消费进程内源观测与既有存储，不联网、不写任何存储、不创建结算。

核心事实：篮球赛果只能来自 ``fetch_live_basketball`` 的即时列表；赛事一旦离开该列表，
系统就没有篮球完场源（``wanchang`` 解析器固定产出足球记录）。本模块把「某个 pending
目标为什么没有赛果」拆成互斥的来源类别，并为 NBA 目标生成精确的绑定/外部证据请求。
候选级跨源证据只是诊断，永远不是 Canonical 判定或结算授权。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from utils.basketball_identity_evidence import (
    OUTCOME_AMBIGUOUS,
    OUTCOME_CANDIDATE_SAME_EVENT,
    OUTCOME_CONTRADICTORY_EVIDENCE,
    classify_cross_source_candidate,
    diagnostic_class,
)
from utils.daily_loader import BEIJING_TZ, get_beijing_now, get_match_datetime
from utils.fetcher_500 import is_nba_league, last_source_observation
from utils.match_identity import competition_number_for_match, same_event
from utils.match_lifecycle import is_finalized_match, valid_full_time_score
from utils.nba_schedule import resolve_canonical_team_code

NO_SOURCE_OBSERVATION = "NO_SOURCE_OBSERVATION"
SOURCE_REQUEST_FAILED = "SOURCE_REQUEST_FAILED"
SOURCE_RETURNED_EMPTY = "SOURCE_RETURNED_EMPTY"
SOURCE_FIXTURE_NOT_PRESENT = "SOURCE_FIXTURE_NOT_PRESENT"
SOURCE_RECORD_PARSED_BUT_NOT_MATCHED = "SOURCE_RECORD_PARSED_BUT_NOT_MATCHED"
SOURCE_RETURNED_NON_FINAL_RESULT = "SOURCE_RETURNED_NON_FINAL_RESULT"
SOURCE_RETURNED_CONFIRMED_FINAL_RESULT = "SOURCE_RETURNED_CONFIRMED_FINAL_RESULT"

RESULT_DELIVERY_STATUSES = (
    NO_SOURCE_OBSERVATION,
    SOURCE_REQUEST_FAILED,
    SOURCE_RETURNED_EMPTY,
    SOURCE_FIXTURE_NOT_PRESENT,
    SOURCE_RECORD_PARSED_BUT_NOT_MATCHED,
    SOURCE_RETURNED_NON_FINAL_RESULT,
    SOURCE_RETURNED_CONFIRMED_FINAL_RESULT,
)

BASKETBALL_RESULT_SOURCE = "live_basketball"
GENERIC_FINISHED_SOURCE = "wanchang"

BDL_PROVIDER = "balldontlie_nba_games"
BDL_BINDING_ESTABLISHED = "BDL_PROVIDER_BINDING_ESTABLISHED"
BDL_BINDING_INCOMPLETE = "BDL_PROVIDER_BINDING_INCOMPLETE"
BDL_BINDING_ABSENT = "BDL_PROVIDER_BINDING_ABSENT"

NON_NBA_RESULT_PATH = "SOURCE_NOT_AVAILABLE"
NBA_RESULT_PATH_AWAITING_EVIDENCE = "AWAITING_EXTERNAL_CONFIRMED_RESULT_EVIDENCE"
NBA_RESULT_PATH_PRIOR_BINDING = "PRIOR_PROVIDER_BINDING_REQUIRED"


def _mapping_store(store: Any, key: str) -> dict[str, dict]:
    if isinstance(store, Mapping):
        raw = store.get(key) if isinstance(store.get(key), Mapping) else store
        return {
            str(item_id): item
            for item_id, item in raw.items()
            if isinstance(item, Mapping)
        }
    if isinstance(store, (list, tuple)):
        out: dict[str, dict] = {}
        for item in store:
            if not isinstance(item, Mapping):
                continue
            item_id = item.get("snapshot_id") or item.get("settlement_id")
            if item_id:
                out[str(item_id)] = item
        return out
    return {}


def _source_entry(observations: Any, name: str) -> dict | None:
    if not isinstance(observations, Mapping):
        return None
    source = observations.get("sources") if isinstance(observations.get("sources"), Mapping) else observations
    entry = source.get(name)
    return entry if isinstance(entry, Mapping) else None


def _row_summary(row: Mapping[str, Any]) -> dict:
    score = row.get("score")
    ft = score.get("ft")[:2] if isinstance(score, Mapping) and isinstance(score.get("ft"), (list, tuple)) else None
    return {
        "id": row.get("id"),
        "status": row.get("status"),
        "date": row.get("date"),
        "time": row.get("time"),
        "league": row.get("league"),
        "home": row.get("home"),
        "away": row.get("away"),
        "final_score": list(ft) if ft else None,
        "valid_final_score": bool(row.get("status") == "finished" and valid_full_time_score(score)),
    }


def _candidate_evidence(target: Mapping[str, Any], row: Mapping[str, Any]) -> dict | None:
    if row.get("sport") != "basketball":
        return None
    if same_event(target, row):
        return None
    if str(row.get("date") or "") != str(target.get("date") or ""):
        return None
    evidence = classify_cross_source_candidate(row, target)
    if evidence.outcome not in (
        OUTCOME_CANDIDATE_SAME_EVENT,
        OUTCOME_AMBIGUOUS,
        OUTCOME_CONTRADICTORY_EVIDENCE,
    ):
        return None
    payload = evidence.as_dict()
    return {
        "observed": _row_summary(row),
        "outcome": payload["outcome"],
        "diagnostic_class": diagnostic_class(evidence),
        "orientation": payload["orientation"],
        "numbers_comparable": payload["numbers_comparable"],
        "numbers_agree": payload["numbers_agree"],
        "authoritative": payload["authoritative"],
        "is_approval": payload["is_approval"],
        "supporting_evidence": payload["supporting_evidence"],
        "contradictory_evidence": payload["contradictory_evidence"],
        "blockers": payload["blockers"],
    }


def classify_target_result_delivery(
    target: Mapping[str, Any],
    observations: Mapping[str, Any] | None,
) -> dict:
    """把一个 pending 目标映射到唯一的来源类别；只按 ``same_event`` 认领行。"""
    entry = _source_entry(observations, BASKETBALL_RESULT_SOURCE)
    if entry is None:
        return {
            "status": NO_SOURCE_OBSERVATION,
            "source": BASKETBALL_RESULT_SOURCE,
            "source_status": None,
            "source_checked_at": None,
            "matched_row_count": 0,
            "candidates": [],
        }

    status = entry.get("status")
    rows = [row for row in (entry.get("rows") or []) if isinstance(row, Mapping)]
    base = {
        "source": BASKETBALL_RESULT_SOURCE,
        "source_status": status,
        "source_checked_at": entry.get("checked_at"),
    }

    if status == "unavailable":
        return dict(base, status=SOURCE_REQUEST_FAILED, matched_row_count=0, candidates=[])

    matched = [row for row in rows if same_event(target, row)]
    candidates = [
        candidate
        for row in rows
        if not same_event(target, row)
        for candidate in [_candidate_evidence(target, row)]
        if candidate is not None
    ][:3]

    if not matched:
        if candidates:
            return dict(
                base,
                status=SOURCE_RECORD_PARSED_BUT_NOT_MATCHED,
                matched_row_count=0,
                candidates=candidates,
            )
        if not rows:
            return dict(base, status=SOURCE_RETURNED_EMPTY, matched_row_count=0, candidates=[])
        return dict(base, status=SOURCE_FIXTURE_NOT_PRESENT, matched_row_count=0, candidates=[])

    finals = [row for row in matched if row.get("status") == "finished" and valid_full_time_score(row.get("score"))]
    final_scores = sorted({tuple(row["score"]["ft"][:2]) for row in finals})
    if len(final_scores) == 1 and len(finals) == len(matched):
        row = finals[0]
        return dict(
            base,
            status=SOURCE_RETURNED_CONFIRMED_FINAL_RESULT,
            matched_row_count=len(matched),
            final_score=list(final_scores[0]),
            matched_row=_row_summary(row),
            candidates=candidates,
        )

    conflict = "CONFLICTING_FINAL_RESULTS" if len(final_scores) > 1 else None
    return dict(
        base,
        status=SOURCE_RETURNED_NON_FINAL_RESULT,
        matched_row_count=len(matched),
        non_final_rows=[_row_summary(row) for row in matched[:3]],
        final_score=list(final_scores[0]) if len(final_scores) == 1 else None,
        conflict=conflict,
        candidates=candidates,
    )


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _snapshot_binding_ready(match: Mapping[str, Any], snapshots: list[Mapping[str, Any]]) -> bool:
    kickoff = get_match_datetime(dict(match))
    if kickoff is None:
        return False
    for snapshot in snapshots:
        if snapshot.get("match_id") != match.get("id"):
            continue
        if _parse_aware(snapshot.get("kickoff_at")) != kickoff:
            continue
        generated = _parse_aware(snapshot.get("generated_at"))
        if generated is not None and generated < kickoff:
            return True
    return False


def _provider_binding(match: Mapping[str, Any], snapshots: list[Mapping[str, Any]]) -> dict:
    kickoff = get_match_datetime(dict(match))
    provenance = match.get("kickoff_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    missing: list[str] = []
    if provenance.get("actual_kickoff_time_source") != BDL_PROVIDER:
        missing.append("kickoff_provenance.actual_kickoff_time_source")
    if provenance.get("kickoff_verification_status") != "SCHEDULE_PROVIDER_VERIFIED":
        missing.append("kickoff_provenance.kickoff_verification_status")
    game_id = provenance.get("kickoff_source_event_id")
    if not isinstance(game_id, str) or not game_id.strip():
        missing.append("kickoff_provenance.kickoff_source_event_id")
    scheduled = provenance.get("kickoff_source_scheduled_start_utc")
    if _parse_aware(scheduled) is None or kickoff is None or _parse_aware(scheduled) != kickoff:
        missing.append("kickoff_provenance.kickoff_source_scheduled_start_utc")
    if match.get("market_tracked") is not True:
        missing.append("market_tracked")
    if competition_number_for_match(dict(match)) is None:
        missing.append("competition_number")
    if kickoff is None:
        missing.append("authoritative_kickoff_time")
    if not _snapshot_binding_ready(match, snapshots):
        missing.append("eligible_pregame_snapshot")

    if provenance.get("actual_kickoff_time_source") == BDL_PROVIDER and not missing:
        state = BDL_BINDING_ESTABLISHED
    elif provenance:
        state = BDL_BINDING_INCOMPLETE
    else:
        state = BDL_BINDING_ABSENT
    return {
        "state": state,
        "provider": BDL_PROVIDER,
        "provider_game_id": game_id if isinstance(game_id, str) and game_id.strip() else None,
        "missing_fields": missing,
    }


def _official_reference_template(match: Mapping[str, Any]) -> str:
    home = resolve_canonical_team_code(match.get("home")) or "<home-team-code>"
    away = resolve_canonical_team_code(match.get("away")) or "<away-team-code>"
    return f"https://www.nba.com/game/{away.lower()}-vs-{home.lower()}-<gameId>"


def _nba_evidence_request(match: Mapping[str, Any], binding: dict) -> dict:
    missing = list(binding["missing_fields"])
    request = {
        "validator": "tools.nba_confirmed_result.bind_result",
        "binding_state": binding["state"],
        "provider": BDL_PROVIDER,
        "provider_game_id": binding["provider_game_id"],
        "required_local_fields": missing,
        "required_external_inputs": [
            {
                "name": "BALLDONTLIE_GAMES_RESPONSE_WITH_FINAL_STATUS_AND_PERIODS",
                "requirements": [
                    "id equals stored kickoff_source_event_id",
                    "status_state == final and status == Final and postponed == False",
                    "datetime equals canonical kickoff in UTC",
                    "home/visitor team IDs match the 30-team mapping",
                    "home_team_score/visitor_team_score and q1..q4/ot1..ot3 reconcile",
                ],
            },
            {
                "name": "NBA_OFFICIAL_GAME_PAGE_WITH_GAME_ID_AND_FINAL_STATUS",
                "requirements": [
                    "gameStatus == 3 and gameStatusText matches Final or Final/OT",
                    "period and both periods arrays are complete",
                    "team IDs/tricodes match the same 30-team mapping",
                    "official scores equal BDL scores and are not tied",
                ],
            },
        ],
        "official_reference_url_template": _official_reference_template(match),
        "evidence_authorization": "NOT_GRANTED",
        "network_requests_performed": 0,
    }
    if binding["state"] == BDL_BINDING_ESTABLISHED and not missing:
        request["state"] = NBA_RESULT_PATH_AWAITING_EVIDENCE
        request["missing_fields"] = [
            "OFFICIAL_NBA_GAME_ID_NOT_STORED",
            "CONFIRMED_FINAL_RESULT_PAYLOAD_NOT_RETRIEVED",
            "EXTERNAL_EVIDENCE_NOT_AUTHORIZED",
        ]
    else:
        request["state"] = NBA_RESULT_PATH_PRIOR_BINDING
        request["missing_fields"] = missing + ["EXTERNAL_EVIDENCE_NOT_AUTHORIZED"]
    return request


def _non_nba_result_path(match: Mapping[str, Any]) -> dict:
    return {
        "state": NON_NBA_RESULT_PATH,
        "reason": "NO_APPROVED_COMPETITION_COMPATIBLE_RESULT_SOURCE",
        "nba_path_applicable": False,
        "league": match.get("league"),
        "network_requests_performed": 0,
    }


def _pending_reason(match: Mapping[str, Any], now: datetime) -> str | None:
    status = str(match.get("status") or "").strip().lower()
    if status == "live":
        return "STORED_LIVE_WITHOUT_FINAL_RESULT"
    if status == "finished":
        return "FINAL_RECORD_WITHOUT_VALID_SCORE"
    kickoff = get_match_datetime(dict(match))
    match_date = str(match.get("date") or "")
    if kickoff is not None:
        if now >= kickoff:
            return "KICKOFF_PASSED_WITHOUT_FINAL_RESULT"
        return None
    if not match_date:
        return "UNKNOWN_KICKOFF_AND_DATE"
    try:
        day = datetime.fromisoformat(match_date).date()
    except ValueError:
        return "UNKNOWN_KICKOFF_AND_INVALID_DATE"
    if day < now.date():
        return "UNKNOWN_KICKOFF_PAST_DATE"
    return "UNKNOWN_KICKOFF_CURRENT_DATE"


def _observation_summary(observations: Any, *, scope: str) -> dict:
    live = _source_entry(observations, BASKETBALL_RESULT_SOURCE)
    generic = _source_entry(observations, GENERIC_FINISHED_SOURCE)
    generic_rows = [row for row in (generic or {}).get("rows") or [] if isinstance(row, Mapping)]
    generic_sports = sorted({str(row.get("sport")) for row in generic_rows if row.get("sport")})
    if generic is None:
        finished_scope = "UNKNOWN"
    elif not generic_rows:
        finished_scope = "NO_ROWS_OBSERVED"
    elif generic_sports == ["football"]:
        finished_scope = "FOOTBALL_ONLY"
    elif "basketball" in generic_sports:
        finished_scope = "INCLUDES_BASKETBALL"
    else:
        finished_scope = "OTHER_ONLY"

    def shape(entry: dict | None) -> dict | None:
        if entry is None:
            return None
        rows = [row for row in entry.get("rows") or [] if isinstance(row, Mapping)]
        return {
            "status": entry.get("status"),
            "checked_at": entry.get("checked_at"),
            "row_count": len(rows),
            "basketball_rows": sum(1 for row in rows if row.get("sport") == "basketball"),
            "football_rows": sum(1 for row in rows if row.get("sport") == "football"),
            "final_basketball_rows": sum(
                1
                for row in rows
                if row.get("sport") == "basketball"
                and row.get("status") == "finished"
                and valid_full_time_score(row.get("score"))
            ),
        }

    return {
        "scope": scope,
        "shared_across_processes": False,
        "available": bool(live or generic),
        "basketball_result_source": shape(live),
        "generic_finished_source": shape(generic),
        "generic_finished_source_sports": generic_sports,
        "generic_finished_source_scope": finished_scope,
        "basketball_finished_source_available": finished_scope == "INCLUDES_BASKETBALL",
    }


def _default_store(module_name: str) -> dict:
    from importlib import import_module

    module = import_module(f"utils.{module_name}")
    return module._load_store()


def build_basketball_result_delivery(
    matches: list[Mapping[str, Any]],
    snapshots: Any = None,
    settlements: Any = None,
    observations: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """构建 pending 篮球目标的赛果投递诊断与证据请求（只读、确定性）。"""
    clock = (now or get_beijing_now()).astimezone(BEIJING_TZ)
    observation_scope = "PROCESS_LOCAL" if observations is None else "CALLER_SUPPLIED_SNAPSHOT"
    source_observations = last_source_observation() if observations is None else observations
    if snapshots is None:
        snapshots = _default_store("prediction_snapshots")
    if settlements is None:
        settlements = _default_store("settlements")
    snapshots_by_id = _mapping_store(snapshots, "snapshots")
    settlements_by_id = _mapping_store(settlements, "settlements")

    snapshots_by_match: dict[str, list[dict]] = {}
    settled_snapshot_ids: set[str] = set()
    legacy_settled_match_ids: set[str] = set()
    for snapshot in snapshots_by_id.values():
        match_id = snapshot.get("match_id")
        if match_id:
            snapshots_by_match.setdefault(str(match_id), []).append(snapshot)
    for settlement in settlements_by_id.values():
        snapshot_id = settlement.get("snapshot_id")
        match_id = settlement.get("match_id")
        if snapshot_id:
            settled_snapshot_ids.add(str(snapshot_id))
        elif match_id:
            legacy_settled_match_ids.add(str(match_id))

    counts = {status: 0 for status in RESULT_DELIVERY_STATUSES}
    targets: list[dict] = []
    finalized_unsettled = 0
    not_yet_due = 0
    no_pending_snapshot = 0

    for match in matches:
        if not isinstance(match, Mapping) or match.get("sport") != "basketball":
            continue
        match_id = str(match.get("id") or "")
        if not match_id:
            continue
        own_snapshots = snapshots_by_match.get(match_id) or []
        if not own_snapshots:
            no_pending_snapshot += 1
            continue
        pending_snapshots = [
            snapshot
            for snapshot in own_snapshots
            if str(snapshot.get("snapshot_id")) not in settled_snapshot_ids
            and match_id not in legacy_settled_match_ids
        ]
        if not pending_snapshots:
            continue
        if is_finalized_match(dict(match)):
            finalized_unsettled += 1
            continue
        reason = _pending_reason(match, clock)
        if reason is None:
            not_yet_due += 1
            continue

        delivery = classify_target_result_delivery(match, source_observations)
        counts[delivery["status"]] += 1
        nba = is_nba_league(match.get("league"))
        binding = _provider_binding(match, pending_snapshots) if nba else None
        target = {
            "match_id": match_id,
            "sport": "basketball",
            "league": match.get("league"),
            "date": match.get("date"),
            "time": match.get("time"),
            "home": match.get("home"),
            "away": match.get("away"),
            "jczq_no": match.get("jczq_no"),
            "stored_status": match.get("status"),
            "pending_reason": reason,
            "kickoff_time_known": get_match_datetime(dict(match)) is not None,
            "pending_snapshot_count": len(pending_snapshots),
            "is_nba": nba,
            "result_delivery": delivery,
        }
        if nba:
            target["provider_binding"] = binding
            target["evidence_request"] = _nba_evidence_request(match, binding)
            target["result_path"] = target["evidence_request"]["state"]
        else:
            target["provider_binding"] = None
            target["result_path"] = _non_nba_result_path(match)
        targets.append(target)

    observation_summary = _observation_summary(source_observations, scope=observation_scope)
    nba_targets = [target for target in targets if target["is_nba"]]
    non_nba_targets = [target for target in targets if not target["is_nba"]]
    return {
        "generated_at": clock.isoformat(),
        "scope": "READ_ONLY_DIAGNOSTIC",
        "network_requests_performed": 0,
        "writes_performed": 0,
        "observations": observation_summary,
        "pending_total": len(targets),
        "nba_pending": len(nba_targets),
        "non_nba_pending": len(non_nba_targets),
        "status_counts": counts,
        "confirmed_final_result_targets": counts[SOURCE_RETURNED_CONFIRMED_FINAL_RESULT],
        "finalized_without_settlement": finalized_unsettled,
        "not_yet_due": not_yet_due,
        "basketball_matches_without_pending_snapshot": no_pending_snapshot,
        "nba_pending_with_provider_binding": sum(
            1
            for target in nba_targets
            if target["provider_binding"]["state"] == BDL_BINDING_ESTABLISHED
        ),
        "nba_pending_without_provider_binding": sum(
            1
            for target in nba_targets
            if target["provider_binding"]["state"] != BDL_BINDING_ESTABLISHED
        ),
        "targets": targets,
        "notes": [
            "live_basketball 是唯一篮球赛果来源；赛事离开即时列表后不存在篮球完场源。",
            "wanchang 解析器固定产出足球记录，不投递篮球赛果。",
            "跨源候选证据只是诊断，不授权身份、赔率合并、入住或结算。",
            "外部 NBA 证据未获授权时，真实赛后闭环保持 BLOCKED。",
        ],
    }


def summarize_result_delivery(report: Mapping[str, Any], *, sample_limit: int = 5) -> dict:
    """压缩为 /api/status 可承载的摘要；完整目标清单留在只读 CLI 报告中。"""
    targets = list(report.get("targets") or [])
    sample = []
    for target in targets[:sample_limit]:
        sample.append(
            {
                "match_id": target.get("match_id"),
                "league": target.get("league"),
                "date": target.get("date"),
                "is_nba": target.get("is_nba"),
                "pending_reason": target.get("pending_reason"),
                "result_delivery_status": (target.get("result_delivery") or {}).get("status"),
                "result_path": (target.get("result_path") or {}).get("state")
                if isinstance(target.get("result_path"), Mapping)
                else target.get("result_path"),
            }
        )
    return {
        "generated_at": report.get("generated_at"),
        "scope": report.get("scope"),
        "observations": report.get("observations"),
        "pending_total": report.get("pending_total"),
        "nba_pending": report.get("nba_pending"),
        "non_nba_pending": report.get("non_nba_pending"),
        "status_counts": report.get("status_counts"),
        "confirmed_final_result_targets": report.get("confirmed_final_result_targets"),
        "finalized_without_settlement": report.get("finalized_without_settlement"),
        "not_yet_due": report.get("not_yet_due"),
        "nba_pending_with_provider_binding": report.get("nba_pending_with_provider_binding"),
        "nba_pending_without_provider_binding": report.get("nba_pending_without_provider_binding"),
        "sample_targets": sample,
        "truncated": len(targets) > sample_limit,
        "notes": report.get("notes"),
    }
