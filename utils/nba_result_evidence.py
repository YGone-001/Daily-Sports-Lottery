"""Reusable, offline-by-default workflow for NBA final-result evidence.

The module contains only pure/read-only selection, planning, parsing and validation
helpers. Network acquisition lives in ``tools.nba_result_workflow`` and requires an
explicit ``--allow-network`` scope. No credentials, no writes to protected stores.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import config
from utils.daily_loader import get_beijing_now, get_match_datetime
from utils.fetcher_500 import is_nba_league
from utils.match_identity import competition_number_for_match
from utils.nba_schedule import resolve_canonical_team_code
from utils.result_delivery import BDL_BINDING_ESTABLISHED, build_basketball_result_delivery

PROVIDER_OFFICIAL = "nba_official_game_page"
PROVIDER_BDL = "balldontlie_nba_games"
SUPPORTED_PROVIDERS = (PROVIDER_OFFICIAL, PROVIDER_BDL)

STATUS_ELIGIBLE = "PROVIDER_BOUND_AND_PENDING"
STATUS_NOT_YET_FINAL = "PROVIDER_BOUND_BUT_NOT_YET_FINAL"
STATUS_UNBOUND = "PROVIDER_IDENTITY_NOT_ESTABLISHED"
STATUS_ALREADY_SETTLED = "ALREADY_SETTLED"
STATUS_IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
STATUS_RESULT_CONFLICT = "RESULT_CONFLICT"
STATUS_MISSING_SNAPSHOT = "MISSING_OR_INVALID_PREGAME_SNAPSHOT"
STATUS_NOT_NBA = "NOT_NBA"
STATUS_NOT_FOUND = "NOT_FOUND"

BDL_PACING_SECONDS = 12.1
OFFICIAL_PAGE_PACING_SECONDS = 1.0
DEFAULT_MAX_TARGETS = 3
MAX_TARGETS_LIMIT = 10

_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _store(name: str, key: str) -> dict[str, dict]:
    path = Path(config.DATA_DIR) / name
    if not path.is_file():
        return {}
    data = _read_json(path)
    value = data.get(key) if isinstance(data, Mapping) else None
    return {str(k): v for k, v in value.items() if isinstance(v, Mapping)} if isinstance(value, Mapping) else {}


def _load_state() -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    path = Path(config.DATA_DIR) / "daily_matches.json"
    matches = _read_json(path).get("matches") if path.is_file() else []
    return list(matches or []), _store(config.PREDICTION_SNAPSHOT_FILE, "snapshots"), \
        _store(config.SETTLEMENT_FILE, "settlements")


def _snapshot_for(match_id: str, snapshots: Mapping[str, dict], settled_snapshot_ids: set[str]) -> dict | None:
    own = [s for s in snapshots.values() if s.get("match_id") == match_id
           and str(s.get("snapshot_id")) not in settled_snapshot_ids]
    if not own:
        return None
    current_version = config.MODEL_VERSIONS.get("basketball")
    current = [s for s in own if s.get("model_version") == current_version]
    pool = current or own
    return sorted(pool, key=lambda s: (str(s.get("generated_at") or ""), str(s.get("snapshot_id") or "")))[-1]


def official_discovery_date_info(kickoff: datetime, match_date: Any) -> dict:
    """Choose the NBA official scoreboard date, or mark it ambiguous.

    NBA official scoreboards use the US local game date. For the normal NBA
    evening window the Beijing match date is one day ahead, but games with a
    UTC kickoff between 08:00 and 16:00 are near a date boundary. Those must
    be resolved by an explicit operator-supplied official game id/date rather
    than by guessing or by silently adding discovery requests.
    """
    try:
        utc = kickoff.astimezone(timezone.utc) if kickoff.tzinfo else kickoff.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return {"date": None, "status": "UNKNOWN", "candidates": [], "source": "UNKNOWN"}
    try:
        beijing_minus_one = (datetime.strptime(str(match_date), "%Y-%m-%d").date()
                             - timedelta(days=1)).isoformat()
    except (TypeError, ValueError):
        return {"date": None, "status": "AMBIGUOUS", "candidates": [],
                "source": "INVALID_MATCH_DATE"}
    candidates = sorted({
        beijing_minus_one,
        utc.date().isoformat(),
        (utc.date() - timedelta(days=1)).isoformat(),
    })
    hour = utc.hour + utc.minute / 60.0
    status = "SAFE" if (hour < 8.0 or hour >= 16.0) else "AMBIGUOUS"
    return {
        "date": beijing_minus_one,
        "status": status,
        "candidates": candidates,
        "source": "BEIJING_MATCH_DATE_MINUS_ONE",
        "utc_kickoff_hour": round(hour, 2),
    }


def apply_official_dates(targets: Iterable[Mapping], overrides: Mapping[str, str]) -> list[dict]:
    """Apply explicit operator date overrides without expanding the request plan."""
    out = []
    for target in targets:
        item = dict(target)
        date = overrides.get(item["match_id"])
        if date:
            item["official_discovery_date"] = date
            item["official_discovery_date_status"] = "SAFE"
            item["official_discovery_date_source"] = "EXPLICIT_OPERATOR_OVERRIDE"
        out.append(item)
    return out


def _target_from_match(match: Mapping, snapshot: Mapping, *, now: datetime) -> dict:
    kickoff = get_match_datetime(dict(match))
    prov = match.get("kickoff_provenance") or {}
    date_info = official_discovery_date_info(kickoff, match.get("date")) if kickoff else {
        "date": None, "status": "UNKNOWN", "candidates": [], "source": "UNKNOWN",
    }
    return {
        "match_id": str(match.get("id")),
        "league": match.get("league"),
        "match_date": match.get("date"),
        "kickoff_at": kickoff.isoformat() if kickoff else None,
        "kickoff_utc": kickoff.astimezone(timezone.utc).isoformat() if kickoff else None,
        "official_discovery_date": date_info["date"],
        "official_discovery_date_status": date_info["status"],
        "official_discovery_date_candidates": date_info["candidates"],
        "official_discovery_date_source": date_info["source"],
        "home": match.get("home"),
        "away": match.get("away"),
        "home_code": resolve_canonical_team_code(match.get("home")),
        "away_code": resolve_canonical_team_code(match.get("away")),
        "competition_number": competition_number_for_match(dict(match)),
        "market_tracked": match.get("market_tracked") is True,
        "stored_status": match.get("status"),
        "bdl_game_id": str(prov.get("kickoff_source_event_id") or "") or None,
        "provider": prov.get("actual_kickoff_time_source"),
        "provider_verification": prov.get("kickoff_verification_status"),
        "scheduled_start_utc": prov.get("kickoff_source_scheduled_start_utc"),
        "snapshot_id": snapshot.get("snapshot_id"),
        "snapshot_digest": fingerprint(snapshot),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "snapshot_kickoff_at": snapshot.get("kickoff_at"),
        "model_name": snapshot.get("model_name"),
        "model_version": snapshot.get("model_version"),
        "selection_status": STATUS_ELIGIBLE,
        "reason": None,
        "_kickoff_dt": kickoff,
        "_now": now,
    }


def _eligible(match: Mapping, snapshot: Mapping, *, now: datetime, duplicate_ids: set[str]) -> tuple[bool, str]:
    kickoff = get_match_datetime(dict(match))
    prov = match.get("kickoff_provenance") or {}
    if not is_nba_league(match.get("league")):
        return False, STATUS_NOT_NBA
    if match.get("market_tracked") is not True or competition_number_for_match(dict(match)) is None:
        return False, "TRACKED_MARKET_IDENTITY_REQUIRED"
    if prov.get("actual_kickoff_time_source") != PROVIDER_BDL \
            or prov.get("kickoff_verification_status") != "SCHEDULE_PROVIDER_VERIFIED" \
            or not prov.get("kickoff_source_event_id"):
        return False, STATUS_UNBOUND
    if str(prov.get("kickoff_source_event_id")) in duplicate_ids:
        return False, STATUS_IDENTITY_CONFLICT
    if kickoff is None:
        return False, "UNKNOWN_KICKOFF"
    if kickoff > now:
        return False, STATUS_NOT_YET_FINAL
    if snapshot.get("snapshot_id") != _snapshot_id_for(match.get("id"), snapshot.get("model_version")):
        return False, STATUS_MISSING_SNAPSHOT
    generated = _parse_aware(snapshot.get("generated_at"))
    snapshot_kickoff = _parse_aware(snapshot.get("kickoff_at"))
    if generated is None or generated >= kickoff or snapshot_kickoff != kickoff:
        return False, STATUS_MISSING_SNAPSHOT
    if snapshot.get("home_team") != match.get("home") or snapshot.get("away_team") != match.get("away"):
        return False, STATUS_MISSING_SNAPSHOT
    return True, STATUS_ELIGIBLE


def _snapshot_id_for(match_id: str, model_version: str | None) -> str | None:
    from utils.prediction_snapshots import snapshot_id_for
    try:
        return snapshot_id_for(str(match_id), str(model_version))
    except Exception:  # noqa: BLE001 - malformed identity is reported as missing snapshot
        return None


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _queue_summary(matches: list[dict], snapshots: Mapping[str, dict], settlements: Mapping[str, dict],
                   report: Mapping, now: datetime) -> dict:
    nba_matches = [m for m in matches if is_nba_league(m.get("league")) and m.get("sport") == "basketball"]
    bdl_ids = [str((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id"))
               for m in nba_matches if (m.get("kickoff_provenance") or {}).get("kickoff_source_event_id")]
    duplicate_ids = sorted({gid for gid in bdl_ids if bdl_ids.count(gid) > 1})
    settled_match_ids = {str(s.get("match_id")) for s in settlements.values() if s.get("match_id")}
    settle_by_snapshot: dict[str, int] = {}
    for s in settlements.values():
        sid = str(s.get("snapshot_id") or "")
        if sid:
            settle_by_snapshot[sid] = settle_by_snapshot.get(sid, 0) + 1
    result_conflicts = sorted(sid for sid, count in settle_by_snapshot.items() if count > 1)
    no_snapshot = [m["id"] for m in nba_matches
                   if not any(s.get("match_id") == m.get("id") for s in snapshots.values())]
    not_yet_final = 0
    for target in report.get("targets") or []:
        kickoff = get_match_datetime(dict(next((m for m in nba_matches if m.get("id") == target.get("match_id")), {})))
        if target.get("is_nba") and kickoff is not None and kickoff > now:
            not_yet_final += 1
    return {
        STATUS_ELIGIBLE: sum(1 for t in report.get("targets") or []
                             if t.get("is_nba")
                             and (t.get("provider_binding") or {}).get("state") == BDL_BINDING_ESTABLISHED),
        STATUS_NOT_YET_FINAL: not_yet_final,
        STATUS_UNBOUND: sum(1 for t in report.get("targets") or []
                            if t.get("is_nba")
                            and (t.get("provider_binding") or {}).get("state") != BDL_BINDING_ESTABLISHED),
        STATUS_ALREADY_SETTLED: len(settled_match_ids & {m["id"] for m in nba_matches}),
        STATUS_IDENTITY_CONFLICT: len(duplicate_ids),
        STATUS_RESULT_CONFLICT: len(result_conflicts),
        STATUS_MISSING_SNAPSHOT: len(no_snapshot),
        "duplicate_provider_game_ids": duplicate_ids,
        "conflicting_snapshot_ids": result_conflicts,
    }


def _requested_rejections(target_ids: Sequence[str], matches: list[dict], snapshots: Mapping[str, dict],
                          settlements: Mapping[str, dict], selected_ids: set[str], now: datetime) -> list[dict]:
    by_id = {m["id"]: m for m in matches}
    settled_match_ids = {str(s.get("match_id")) for s in settlements.values() if s.get("match_id")}
    settled_snapshot_ids = {str(s.get("snapshot_id")) for s in settlements.values() if s.get("snapshot_id")}
    duplicate_ids = {str((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id"))
                     for m in matches if (m.get("kickoff_provenance") or {}).get("kickoff_source_event_id")}
    duplicates = {gid for gid in duplicate_ids
                  if sum(1 for m in matches if str((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id")) == gid) > 1}
    out = []
    for mid in target_ids:
        if mid in selected_ids:
            continue
        match = by_id.get(mid)
        if match is None:
            out.append({"match_id": mid, "selection_status": STATUS_NOT_FOUND, "reason": "CANONICAL_MATCH_NOT_FOUND"})
            continue
        if match.get("sport") != "basketball" or not is_nba_league(match.get("league")):
            out.append({"match_id": mid, "selection_status": STATUS_NOT_NBA, "reason": "NOT_NBA_BASKETBALL"})
            continue
        if mid in settled_match_ids:
            out.append({"match_id": mid, "selection_status": STATUS_ALREADY_SETTLED, "reason": "SETTLEMENT_EXISTS"})
            continue
        snapshot = _snapshot_for(mid, snapshots, settled_snapshot_ids)
        if snapshot is None:
            out.append({"match_id": mid, "selection_status": STATUS_MISSING_SNAPSHOT,
                        "reason": "NO_PENDING_PREGAME_SNAPSHOT"})
            continue
        ok, reason = _eligible(match, snapshot, now=now, duplicate_ids=duplicates)
        out.append({"match_id": mid, "selection_status": STATUS_ELIGIBLE if ok else reason,
                    "reason": None if ok else reason})
    return out


def select_pending_targets(target_ids: Sequence[str] | None = None, *, max_targets: int = DEFAULT_MAX_TARGETS,
                           now: datetime | None = None) -> dict:
    """Select eligible pending NBA targets; never contacts providers."""
    now = now or get_beijing_now()
    requested_raw = list(target_ids) if target_ids else None
    requested = list(dict.fromkeys(requested_raw)) if requested_raw else None
    duplicates_removed = (len(requested_raw) - len(requested)) if requested_raw else 0
    if requested is not None and len(requested) > max_targets:
        return {
            "status": "REJECTED",
            "reason": "TARGET_LIMIT_EXCEEDED",
            "generated_at": now.isoformat(),
            "scope": "READ_ONLY_SELECTION",
            "network_requests_performed": 0,
            "max_targets": max_targets,
            "requested_count": len(requested_raw),
            "unique_requested_count": len(requested),
            "duplicates_removed": duplicates_removed,
            "targets": [],
            "not_selected_eligible": [],
            "rejected": [],
            "queue": {},
        }
    matches, snapshots, settlements = _load_state()
    report = build_basketball_result_delivery(
        matches, {"snapshots": snapshots}, {"settlements": settlements}, observations={}, now=now,
    )
    settled_snapshot_ids = {str(s.get("snapshot_id")) for s in settlements.values() if s.get("snapshot_id")}
    bdl_ids = [str((m.get("kickoff_provenance") or {}).get("kickoff_source_event_id"))
               for m in matches if (m.get("kickoff_provenance") or {}).get("kickoff_source_event_id")]
    duplicate_ids = {gid for gid in bdl_ids if bdl_ids.count(gid) > 1}

    candidates = []
    for target in report.get("targets") or []:
        if not target.get("is_nba"):
            continue
        mid = str(target.get("match_id"))
        if requested is not None and mid not in requested:
            continue
        if (target.get("provider_binding") or {}).get("state") != BDL_BINDING_ESTABLISHED:
            if requested is not None:
                candidates.append({"match_id": mid, "selection_status": STATUS_UNBOUND,
                                   "reason": "PRIOR_PROVIDER_BINDING_REQUIRED"})
            continue
        match = next((m for m in matches if m.get("id") == mid), None)
        snapshot = _snapshot_for(mid, snapshots, settled_snapshot_ids)
        if match is None or snapshot is None:
            candidates.append({"match_id": mid, "selection_status": STATUS_MISSING_SNAPSHOT,
                               "reason": "NO_PENDING_PREGAME_SNAPSHOT"})
            continue
        ok, reason = _eligible(match, snapshot, now=now, duplicate_ids=duplicate_ids)
        if not ok:
            candidates.append({"match_id": mid, "selection_status": reason, "reason": reason})
            continue
        candidates.append(_target_from_match(match, snapshot, now=now))

    eligible = [c for c in candidates if c.get("selection_status") == STATUS_ELIGIBLE]
    eligible.sort(key=lambda t: (t.get("kickoff_at") or "", t["match_id"]))
    if requested is None:
        eligible = eligible[:max_targets]
    accepted_ids = {t["match_id"] for t in eligible}
    not_selected = [c for c in candidates
                    if c["match_id"] not in accepted_ids and c.get("selection_status") == STATUS_ELIGIBLE]
    rejected = [c for c in candidates
                if c["match_id"] not in accepted_ids and c.get("selection_status") != STATUS_ELIGIBLE]
    if requested is not None:
        rejected.extend(_requested_rejections(requested, matches, snapshots, settlements, accepted_ids, now))
    seen = set()
    unique_rejected = []
    for item in rejected:
        if item["match_id"] in seen:
            continue
        seen.add(item["match_id"])
        unique_rejected.append(item)

    def clean(target: dict) -> dict:
        return {k: v for k, v in target.items() if not k.startswith("_")}

    return {
        "status": "OK",
        "reason": None,
        "generated_at": now.isoformat(),
        "scope": "READ_ONLY_SELECTION",
        "network_requests_performed": 0,
        "max_targets": max_targets,
        "requested_count": len(requested_raw) if requested_raw else None,
        "unique_requested_count": len(requested) if requested is not None else None,
        "duplicates_removed": duplicates_removed,
        "targets": [clean(t) for t in eligible],
        "not_selected_eligible": [clean(t) for t in not_selected],
        "rejected": [clean(t) for t in unique_rejected],
        "queue": _queue_summary(matches, snapshots, settlements, report, now),
    }


def build_request_plan(targets: Iterable[Mapping], *, allow_network: bool = False, max_requests: int = 0,
                       allowed_providers: Sequence[str] | None = None, max_cost: float = 0,
                       official_game_ids: Mapping[str, str] | None = None) -> dict:
    targets = [t for t in targets if t.get("selection_status", STATUS_ELIGIBLE) == STATUS_ELIGIBLE]
    official_game_ids = dict(official_game_ids or {})
    providers = list(allowed_providers or SUPPORTED_PROVIDERS)
    missing_official = [t for t in targets if t["match_id"] not in official_game_ids]
    ambiguous_dates = [t for t in missing_official
                       if t.get("official_discovery_date_status") != "SAFE"]
    discovery_dates = sorted({str(t.get("official_discovery_date")) for t in missing_official
                              if t.get("official_discovery_date")})
    requests = len(discovery_dates) + len(targets) + len(targets)
    status = "PLAN_READY"
    reason = None
    if not targets:
        status, reason = "NO_ELIGIBLE_TARGETS", "NO_ELIGIBLE_TARGETS"
    elif ambiguous_dates:
        status, reason = "OFFICIAL_DATE_AMBIGUOUS", "OFFICIAL_DISCOVERY_DATE_REQUIRES_EXPLICIT_OVERRIDE"
    elif any(p not in SUPPORTED_PROVIDERS for p in providers) or set(providers) != set(SUPPORTED_PROVIDERS):
        status, reason = "PROVIDER_SCOPE_INCOMPLETE", "BOTH_NBA_OFFICIAL_AND_BDL_REQUIRED"
    elif max_cost < 0:
        status, reason = "INVALID_COST_BUDGET", "MAX_COST_MUST_BE_NON_NEGATIVE"
    elif not allow_network:
        status, reason = "NETWORK_AUTHORIZATION_REQUIRED", "EXPLICIT_ALLOW_NETWORK_REQUIRED"
    elif max_requests < requests:
        status, reason = "REQUEST_BUDGET_INSUFFICIENT", "MAX_REQUESTS_BELOW_PLANNED"
    return {
        "status": status,
        "reason": reason,
        "allow_network": bool(allow_network),
        "target_count": len(targets),
        "targets": [t["match_id"] for t in targets],
        "discovery_dates": discovery_dates,
        "discovery_requests": len(discovery_dates),
        "official_discovery_date_statuses": {
            t["match_id"]: t.get("official_discovery_date_status") for t in targets
        },
        "ambiguous_discovery_targets": [t["match_id"] for t in ambiguous_dates],
        "official_page_requests": len(targets),
        "bdl_requests": len(targets),
        "requests_planned": requests,
        "max_requests": max_requests,
        "allowed_providers": providers,
        "max_cost_usd": max_cost,
        "expected_cost_usd": 0,
        "rate_limits": {
            "bdl_pacing_seconds_min": BDL_PACING_SECONDS,
            "official_page_pacing_seconds_min": OFFICIAL_PAGE_PACING_SECONDS,
        },
        "automatic_retry": False,
        "production_writes": 0,
    }


def extract_next_data(content: bytes) -> dict | None:
    match = _NEXT_DATA_RE.search(content.decode("utf-8", errors="replace"))
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _walk_game_nodes(obj: Any, found: list[dict]) -> None:
    if isinstance(obj, dict):
        if isinstance(obj.get("gameId"), str):
            found.append(obj)
        for value in obj.values():
            _walk_game_nodes(value, found)
    elif isinstance(obj, list):
        for value in obj:
            _walk_game_nodes(value, found)


def _tricode(node: Any) -> str | None:
    if isinstance(node, Mapping):
        for key in ("teamTricode", "tricode", "teamCode"):
            if isinstance(node.get(key), str):
                return node[key]
    return None


def parse_discovery_page(content: bytes) -> dict:
    next_data = extract_next_data(content)
    if next_data is None:
        return {"status": "NO_NEXT_DATA", "games": [], "pairs": {}}
    nodes: list[dict] = []
    _walk_game_nodes(next_data, nodes)
    games = {}
    for node in nodes:
        gid = node.get("gameId")
        home = _tricode(node.get("homeTeam"))
        away = _tricode(node.get("awayTeam"))
        if not gid or not home or not away:
            continue
        games.setdefault(gid, {"game_id": gid, "home": home, "away": away})
    pairs: dict[str, list[str]] = {}
    for game in games.values():
        pairs.setdefault(f"{game['away']}-{game['home']}", []).append(game["game_id"])
    return {"status": "OK" if games else "NO_GAMES", "games": list(games.values()), "pairs": pairs}


def resolve_official_game_ids(parsed: Mapping, targets: Iterable[Mapping]) -> tuple[dict[str, str], list[dict]]:
    mapping: dict[str, str] = {}
    unresolved = []
    pairs = parsed.get("pairs") or {}
    for target in targets:
        pair = f"{target['away_code']}-{target['home_code']}"
        ids = list(pairs.get(pair) or [])
        if len(ids) == 1:
            mapping[target["match_id"]] = ids[0]
        else:
            unresolved.append({"match_id": target["match_id"], "pair": pair,
                               "reason": "OFFICIAL_GAME_ID_UNRESOLVED", "candidates": ids})
    return mapping, unresolved


def official_page_url(target: Mapping, game_id: str) -> str:
    return (f"https://www.nba.com/game/{str(target['away_code']).lower()}"
            f"-vs-{str(target['home_code']).lower()}-{game_id}")


def validate_target_evidence(target: Mapping, official_path: str | Path, bdl_path: str | Path, *,
                             observed_at: str, source_reference: str) -> dict:
    """Reuse ``tools.nba_confirmed_result.bind_result``; never creates a second validator."""
    from tools.nba_confirmed_result import _official_game, bind_result

    try:
        official = _official_game(official_path)
        payload = _read_json(Path(bdl_path))
        bdl = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(official, Mapping) or not isinstance(bdl, Mapping):
            return {"qualified": False, "reason": "EVIDENCE_SCHEMA_INCOMPATIBLE"}
        matches, snapshots, _settlements = _load_state()
        match = next((m for m in matches if m.get("id") == target["match_id"]), None)
        snapshot = snapshots.get(str(target.get("snapshot_id")))
        if match is None or snapshot is None:
            return {"qualified": False, "reason": "TARGET_NOT_FOUND"}
        resolved = bind_result(match, snapshot, official, bdl,
                               observed_at=observed_at, source_reference=source_reference)
        provenance = resolved.get("result_provenance") or {}
        return {
            "qualified": True,
            "reason": None,
            "final_score": resolved.get("score", {}).get("ft"),
            "final_period": provenance.get("final_period"),
            "overtime": provenance.get("overtime"),
            "source_reference": provenance.get("source_reference"),
        }
    except ValueError as exc:
        return {"qualified": False, "reason": str(exc)}
    except KeyError as exc:
        return {"qualified": False, "reason": f"EVIDENCE_SCHEMA_INCOMPATIBLE:{exc}"}
    except TypeError as exc:
        return {"qualified": False, "reason": f"EVIDENCE_SCHEMA_INCOMPATIBLE:{type(exc).__name__}"}


def target_for_preview(entry: Mapping) -> dict:
    return {
        "match_id": entry["match_id"],
        "snapshot_id": entry["snapshot_id"],
        "official_page": entry["official_path"],
        "bdl_response": entry["bdl_path"],
        "observed_at": entry.get("source_observed_at") or entry.get("observed_at"),
        "source_reference": entry["source_reference"],
    }
