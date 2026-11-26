"""Football historical result matching and preview (read-only).

Reuses the existing ``fetcher_500.fetch_finished_matches`` source adapter and the
existing immutable prediction/settlement/evaluation identity functions. It does
not write any protected store and does not create a second settlement engine.

The finished feed (``wanchang.php``) assigns row-position IDs, which are not
stable identities. Source rows are therefore matched to existing Canonical
records by the exact ``(sport, date, home, away)`` team key only; reversed or
ambiguous pairs are rejected rather than auto-swapped.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from utils.daily_loader import get_match_datetime
from utils.evaluation_rows import evaluation_id_for
from utils.match_lifecycle import valid_full_time_score
from utils.settlements import settlement_id_for, is_valid_prematch

BEIJING = timezone(timedelta(hours=8), name="Asia/Shanghai")
PROVIDER = "500_com_wanchang"
WRITE_SCOPE = ("daily_matches.json", "settlements.json", "evaluation_rows.json")
STORE_NAMES = (
    "daily_matches.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
    "team_strength.json",
    "calibrated.json",
)

STATUS_ELIGIBLE = "ELIGIBLE"
STATUS_UNMATCHED = "SOURCE_ROW_UNMATCHED"
STATUS_AMBIGUOUS = "AMBIGUOUS_CANONICAL_MATCH"
STATUS_ORIENTATION = "ORIENTATION_CONFLICT"
STATUS_NOT_FINAL = "SOURCE_NOT_FINAL"
STATUS_INVALID_SCORE = "INVALID_FINAL_SCORE"
STATUS_ALREADY_SETTLED = "ALREADY_SETTLED"
STATUS_ALREADY_FINAL = "ALREADY_FINAL_UNSETTLED"
STATUS_NO_SNAPSHOT = "NO_VALID_PREGAME_SNAPSHOT"
STATUS_RESULT_CONFLICT = "RESULT_CONFLICT"
STATUS_DUPLICATE_SOURCE = "DUPLICATE_SOURCE_ROW"
STATUS_NOT_FOOTBALL = "NOT_FOOTBALL"


def _load(name: str, data_dir: Path) -> dict:
    path = Path(data_dir) / name
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig") as fh:
        value = json.load(fh)
    return value if isinstance(value, dict) else {}


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def store_hashes(data_dir: Path) -> dict:
    out = {}
    for name in STORE_NAMES:
        path = Path(data_dir) / name
        out[name] = _sha256_bytes(path.read_bytes()) if path.is_file() else None
    return out


def _snapshots_for(match_id: str, snapshots: dict) -> list[dict]:
    return [s for s in snapshots.values() if isinstance(s, dict) and s.get("match_id") == match_id]


def _valid_pregame_snapshots(match: dict, snapshots: dict) -> list[dict]:
    out = []
    for snap in _snapshots_for(match.get("id"), snapshots):
        if not snap.get("snapshot_id"):
            continue
        generated = snap.get("generated_at")
        snap_kickoff = snap.get("kickoff_at")
        if not isinstance(generated, str) or not generated:
            continue
        if not isinstance(snap_kickoff, str) or not snap_kickoff:
            continue
        if not is_valid_prematch(snap):
            continue
        out.append(snap)
    return out


def select_pending_targets(
    matches: list[dict],
    snapshots: dict,
    settlements: dict,
    *,
    max_targets: int | None = None,
    match_ids: list[str] | None = None,
) -> dict:
    """Select finished football records missing a valid score but having a pregame snapshot."""
    requested = {str(mid) for mid in (match_ids or [])}
    settled_match_ids = {s.get("match_id") for s in settlements.values() if isinstance(s, dict)}
    targets, rejected = [], []
    for match in matches:
        if not isinstance(match, dict) or match.get("sport") != "football":
            continue
        mid = str(match.get("id") or "")
        if requested and mid not in requested:
            continue
        if match.get("status") != "finished" or valid_full_time_score(match.get("score")):
            continue
        if mid in settled_match_ids:
            rejected.append({"match_id": mid, "reason": STATUS_ALREADY_SETTLED})
            continue
        snaps = _valid_pregame_snapshots(match, snapshots)
        if not snaps:
            rejected.append({"match_id": mid, "reason": STATUS_NO_SNAPSHOT})
            continue
        kickoff = get_match_datetime(match)
        targets.append({
            "match_id": mid,
            "date": match.get("date"),
            "league": match.get("league"),
            "home": match.get("home"),
            "away": match.get("away"),
            "kickoff_at": kickoff.isoformat() if kickoff else None,
            "snapshot_ids": [s.get("snapshot_id") for s in snaps],
            "current_status": match.get("status"),
            "current_score": match.get("score"),
        })
    targets.sort(key=lambda t: (t.get("date") or "", t.get("match_id") or ""))
    selected = targets if max_targets is None else targets[:max_targets]
    return {
        "status": "OK",
        "target_count": len(selected),
        "eligible_total": len(targets),
        "not_selected_eligible": max(0, len(targets) - len(selected)),
        "requested_dates": sorted({t["date"] for t in selected if t.get("date")}),
        "targets": selected,
        "rejected": rejected,
        "counts": {
            "finished_missing_score": len(targets) + len(rejected),
            "eligible": len(targets),
            "already_settled": sum(1 for r in rejected if r["reason"] == STATUS_ALREADY_SETTLED),
            "no_pregame_snapshot": sum(1 for r in rejected if r["reason"] == STATUS_NO_SNAPSHOT),
        },
    }


def _team_index(matches: list[dict]):
    exact = defaultdict(list)
    reverse = defaultdict(list)
    for match in matches:
        if not isinstance(match, dict) or match.get("sport") != "football":
            continue
        key = (match.get("date"), match.get("home"), match.get("away"))
        exact[key].append(match)
        reverse[(match.get("date"), match.get("away"), match.get("home"))].append(match)
    return exact, reverse


def match_source_rows(
    source_rows: list[dict],
    matches: list[dict],
    snapshots: dict,
    settlements: dict,
) -> dict:
    """Match finished-feed rows to Canonical records and build settlement candidates."""
    exact, reverse = _team_index(matches)
    settled_snapshot_ids = {
        s.get("snapshot_id") for s in settlements.values() if isinstance(s, dict)
    }
    settled_match_ids = {
        s.get("match_id") for s in settlements.values() if isinstance(s, dict)
    }

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    preliminary = []
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        if row.get("sport") not in (None, "football"):
            preliminary.append({"source_row": row, "reason": STATUS_NOT_FOOTBALL})
            continue
        if row.get("status") != "finished":
            preliminary.append({"source_row": row, "reason": STATUS_NOT_FINAL})
            continue
        if not valid_full_time_score(row.get("score")):
            preliminary.append({"source_row": row, "reason": STATUS_INVALID_SCORE})
            continue
        grouped[(row.get("date"), row.get("home"), row.get("away"))].append(row)

    rejected = list(preliminary)
    candidates = []
    for key, rows in sorted(grouped.items(), key=lambda item: tuple(str(x) for x in item[0])):
        scores = {tuple(r["score"]["ft"][:2]) for r in rows}
        if len(scores) > 1:
            rejected.append({"source_rows": rows, "reason": STATUS_RESULT_CONFLICT})
            continue
        if len(rows) > 1:
            rejected.append({"source_rows": rows, "reason": STATUS_DUPLICATE_SOURCE})
            continue
        row = rows[0]
        exact_matches = exact.get(key, [])
        reverse_matches = reverse.get(key, [])
        if len(exact_matches) > 1:
            rejected.append({"source_row": row, "reason": STATUS_AMBIGUOUS,
                             "canonical_ids": sorted(str(m.get("id")) for m in exact_matches)})
            continue
        if not exact_matches:
            if reverse_matches:
                rejected.append({"source_row": row, "reason": STATUS_ORIENTATION,
                                 "canonical_ids": sorted(str(m.get("id")) for m in reverse_matches)})
            else:
                rejected.append({"source_row": row, "reason": STATUS_UNMATCHED})
            continue
        match = exact_matches[0]
        mid = str(match.get("id"))
        source_score = [int(row["score"]["ft"][0]), int(row["score"]["ft"][1])]
        if valid_full_time_score(match.get("score")):
            canonical_score = [int(match["score"]["ft"][0]), int(match["score"]["ft"][1])]
            if canonical_score != source_score:
                rejected.append({"source_row": row, "match_id": mid, "reason": STATUS_RESULT_CONFLICT,
                                 "canonical_score": canonical_score, "source_score": source_score})
                continue
        if mid in settled_match_ids:
            rejected.append({"source_row": row, "match_id": mid, "reason": STATUS_ALREADY_SETTLED})
            continue
        snaps = _valid_pregame_snapshots(match, snapshots)
        if not snaps:
            rejected.append({"source_row": row, "match_id": mid, "reason": STATUS_NO_SNAPSHOT})
            continue
        for snap in snaps:
            snapshot_id = str(snap["snapshot_id"])
            settlement_id = settlement_id_for(snapshot_id)
            if settlement_id in settlements or snapshot_id in settled_snapshot_ids:
                rejected.append({"source_row": row, "match_id": mid,
                                 "reason": STATUS_ALREADY_SETTLED,
                                 "snapshot_id": snapshot_id})
                continue
            home, away = source_score
            actual_outcome = "home_win" if home > away else ("draw" if home == away else "away_win")
            candidates.append({
                "match_id": mid,
                "snapshot_id": snapshot_id,
                "settlement_id": settlement_id,
                "evaluation_id": evaluation_id_for(snapshot_id, settlement_id),
                "source_row_id": row.get("id"),
                "source_row_id_stability": "ROW_POSITION_UNSTABLE",
                "source_date": row.get("date"),
                "league": match.get("league"),
                "home": match.get("home"),
                "away": match.get("away"),
                "final_score": {"home": home, "away": away},
                "actual_outcome": actual_outcome,
                "current_status": match.get("status"),
                "current_score": match.get("score"),
                "already_final": valid_full_time_score(match.get("score")),
            })
    candidates.sort(key=lambda c: (c.get("source_date") or "", c["match_id"], c["snapshot_id"]))
    return {
        "status": "OK" if candidates else "BLOCKED",
        "reason": None if candidates else "NO_ELIGIBLE_CANDIDATES",
        "candidates": candidates,
        "rejected": rejected,
        "target_match_ids": sorted({c["match_id"] for c in candidates}),
        "counts": {
            "source_rows": len(source_rows),
            "candidates": len(candidates),
            "target_matches": len({c["match_id"] for c in candidates}),
            "rejected": len(rejected),
            "rejected_by_reason": dict(Counter(r["reason"] for r in rejected)),
        },
    }


def build_preview(source_rows: list[dict], data_dir: Path, *,
                  source_observed_at: str | None = None) -> dict:
    """Build a deterministic, read-only settlement preview."""
    data_dir = Path(data_dir)
    matches_doc = _load("daily_matches.json", data_dir)
    snap_doc = _load("prediction_snapshots.json", data_dir)
    settle_doc = _load("settlements.json", data_dir)
    matches = matches_doc.get("matches") or []
    snapshots = snap_doc.get("snapshots") or {}
    settlements = settle_doc.get("settlements") or {}
    matching = match_source_rows(source_rows, matches, snapshots, settlements)
    marker = _load(".refresh_state.json", data_dir)
    generation = marker.get("generation")
    payload = {
        "preview": {
            "provider": PROVIDER,
            "source_observed_at": source_observed_at,
            "generation": generation,
            "store_hashes": store_hashes(data_dir),
            "write_scope": list(WRITE_SCOPE),
            "network_requests": 0,
            "canonical_reconciliation": False,
            "candidates": matching["candidates"],
            "rejected": matching["rejected"],
            "counts": matching["counts"],
        },
        "status": "PASS" if matching["candidates"] else "BLOCKED",
        "reason": None if matching["candidates"] else matching["reason"],
        "target_match_ids": matching["target_match_ids"],
        "target_count": len(matching["candidates"]),
    }
    digest_payload = dict(payload["preview"])
    payload["preview_sha256"] = _sha256_bytes(
        json.dumps(digest_payload, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    )
    return payload


def evidence_endpoint(date: str) -> str:
    return f"https://live.500.com/wanchang.php?e={date}"


def valid_date(value: str) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False
