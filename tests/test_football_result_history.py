"""Tests for the reusable football historical-result matcher and preview."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import config
from tools import football_result_workflow as cli
from utils import football_result_history as history
from utils.evaluation_rows import evaluation_id_for
from utils.prediction_snapshots import snapshot_id_for
from utils.settlements import settlement_id_for

ROOT = Path(__file__).resolve().parents[1]
STORES = (
    "daily_matches.json", "prediction_snapshots.json", "odds_snapshots.json",
    "settlements.json", "evaluation_rows.json", "team_strength.json", "calibrated.json",
)


def _match(mid, *, date="2026-01-02", home="主队", away="客队", status="finished",
           score=None, sport="football", league="测试联赛"):
    return {
        "id": mid, "sport": sport, "league": league, "date": date,
        "time": "20:00", "kickoff_time_known": True, "status": status,
        "home": home, "away": away, "score": score, "odds": None,
    }


def _snap(match, *, generated="2026-01-02T10:00:00+08:00",
          kickoff="2026-01-02T20:00:00+08:00"):
    sid = snapshot_id_for(match["id"], config.MODEL_VERSIONS["football"])
    return {
        "snapshot_id": sid, "match_id": match["id"], "sport": "football",
        "league": match["league"], "home_team": match["home"], "away_team": match["away"],
        "model_name": "elo-poisson-dixon-coles",
        "model_version": config.MODEL_VERSIONS["football"],
        "generated_at": generated, "kickoff_at": kickoff,
        "model_probabilities": {"home_win": 45, "draw": 30, "away_win": 25},
        "market_odds": {}, "market_implied_probabilities": {},
        "expected_values": {}, "expected_score_data": {},
    }


def _source_row(match, *, score=(2, 1), status="finished", date=None,
                home=None, away=None, row_id="500w-2026-01-02-0"):
    return {
        "id": row_id, "sport": "football", "league": match.get("league"),
        "date": date or match["date"], "time": "20:00",
        "kickoff_time_known": True, "status": status,
        "home": home if home is not None else match["home"],
        "away": away if away is not None else match["away"],
        "score": {"ft": list(score)} if score is not None else None,
        "odds": None,
    }


def _write_stores(data_dir: Path, matches, snapshots=None, settlements=None):
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshots = snapshots or {}
    settlements = settlements or {}
    payloads = {
        "daily_matches.json": {"matches": matches, "meta": {"total": len(matches)}},
        "prediction_snapshots.json": {"version": 1,
                                      "snapshots": {s["snapshot_id"]: s for s in snapshots.values()}},
        "odds_snapshots.json": {"version": 1, "snapshots": {}},
        "settlements.json": {"version": 1, "settlements": settlements},
        "evaluation_rows.json": {"version": 1, "rows": {}},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        ".refresh_state.json": {"generation": 7, "status": "COMPLETED", "stores": {}},
    }
    for name, value in payloads.items():
        (data_dir / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _hashes(data_dir: Path):
    return {name: hashlib.sha256((data_dir / name).read_bytes()).hexdigest() for name in STORES}


@pytest.fixture
def base(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    match = _match("500l-2026-01-02-1001")
    snap = _snap(match)
    _write_stores(data_dir, [match], {snap["snapshot_id"]: snap})
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    return data_dir, match, snap


def test_select_pending_targets_separates_snapshot_availability(base):
    data_dir, match, snap = base
    no_snap = _match("500l-2026-01-02-1002", home="甲队", away="乙队")
    valid = _match("500l-2026-01-02-1003", home="丙队", away="丁队",
                   score={"ft": [1, 0]})
    upcoming = _match("500l-2026-01-02-1004", home="戊队", away="己队", status="upcoming")
    basket = _match("500l-2026-01-02-1005", home="庚队", away="辛队", sport="basketball")
    _write_stores(data_dir, [match, no_snap, valid, upcoming, basket],
                  {snap["snapshot_id"]: snap})
    result = history.select_pending_targets(
        [match, no_snap, valid, upcoming, basket],
        {snap["snapshot_id"]: snap}, {}, max_targets=1,
    )
    assert result["status"] == "OK"
    assert result["eligible_total"] == 1
    assert result["target_count"] == 1
    assert result["targets"][0]["match_id"] == match["id"]
    assert result["not_selected_eligible"] == 0
    assert result["counts"]["no_pregame_snapshot"] == 1


def test_eligible_candidate_uses_existing_identity_functions(base):
    data_dir, match, snap = base
    source = _source_row(match)
    result = history.match_source_rows([source], [match], {snap["snapshot_id"]: snap}, {})
    assert result["status"] == "OK"
    assert result["counts"]["candidates"] == 1
    candidate = result["candidates"][0]
    assert candidate["match_id"] == match["id"]
    assert candidate["snapshot_id"] == snap["snapshot_id"]
    assert candidate["settlement_id"] == settlement_id_for(snap["snapshot_id"])
    assert candidate["evaluation_id"] == evaluation_id_for(
        snap["snapshot_id"], settlement_id_for(snap["snapshot_id"]))
    assert candidate["final_score"] == {"home": 2, "away": 1}
    assert candidate["actual_outcome"] == "home_win"


def test_reversed_orientation_is_rejected_not_swapped(base):
    data_dir, match, snap = base
    source = _source_row(match, home=match["away"], away=match["home"])
    result = history.match_source_rows([source], [match], {snap["snapshot_id"]: snap}, {})
    assert result["status"] == "BLOCKED"
    assert result["rejected"][0]["reason"] == history.STATUS_ORIENTATION
    assert result["candidates"] == []


def test_ambiguous_canonical_pair_is_rejected(base):
    data_dir, match, snap = base
    duplicate = dict(match)
    duplicate["id"] = "500l-2026-01-02-1001-dup"
    result = history.match_source_rows(
        [_source_row(match)], [match, duplicate], {snap["snapshot_id"]: snap}, {})
    assert result["status"] == "BLOCKED"
    assert result["rejected"][0]["reason"] == history.STATUS_AMBIGUOUS
    assert sorted(result["rejected"][0]["canonical_ids"]) == sorted(
        [match["id"], duplicate["id"]])


def test_no_pregame_snapshot_is_rejected(tmp_path):
    match = _match("500l-2026-01-02-1001")
    result = history.match_source_rows([_source_row(match)], [match], {}, {})
    assert result["status"] == "BLOCKED"
    assert result["rejected"][0]["reason"] == history.STATUS_NO_SNAPSHOT


def test_already_settled_is_rejected(base):
    data_dir, match, snap = base
    settlement = {
        "settlement_id": settlement_id_for(snap["snapshot_id"]),
        "snapshot_id": snap["snapshot_id"],
        "match_id": match["id"],
    }
    result = history.match_source_rows(
        [_source_row(match)], [match], {snap["snapshot_id"]: snap},
        {settlement["settlement_id"]: settlement})
    assert result["status"] == "BLOCKED"
    assert result["rejected"][0]["reason"] == history.STATUS_ALREADY_SETTLED


def test_duplicate_and_conflicting_source_rows_are_rejected(base):
    data_dir, match, snap = base
    duplicate = history.match_source_rows(
        [_source_row(match, row_id="500w-0"), _source_row(match, row_id="500w-1")],
        [match], {snap["snapshot_id"]: snap}, {})
    assert duplicate["rejected"][0]["reason"] == history.STATUS_DUPLICATE_SOURCE
    conflict = history.match_source_rows(
        [_source_row(match, score=(2, 1), row_id="500w-0"),
         _source_row(match, score=(0, 0), row_id="500w-1")],
        [match], {snap["snapshot_id"]: snap}, {})
    assert conflict["rejected"][0]["reason"] == history.STATUS_RESULT_CONFLICT


def test_non_final_and_invalid_score_are_rejected(base):
    data_dir, match, snap = base
    not_final = history.match_source_rows(
        [_source_row(match, status="live")], [match], {snap["snapshot_id"]: snap}, {})
    assert not_final["rejected"][0]["reason"] == history.STATUS_NOT_FINAL
    invalid = history.match_source_rows(
        [_source_row(match, score=None)], [match], {snap["snapshot_id"]: snap}, {})
    assert invalid["rejected"][0]["reason"] == history.STATUS_INVALID_SCORE


def test_canonical_result_conflict_is_rejected(base):
    data_dir, match, snap = base
    finished = dict(match, score={"ft": [1, 0]})
    result = history.match_source_rows(
        [_source_row(match, score=(2, 1))], [finished], {snap["snapshot_id"]: snap}, {})
    assert result["status"] == "BLOCKED"
    assert result["rejected"][0]["reason"] == history.STATUS_RESULT_CONFLICT


def test_preview_is_deterministic_and_read_only(base):
    data_dir, match, snap = base
    before = _hashes(data_dir)
    first = history.build_preview([_source_row(match)], data_dir,
                                  source_observed_at="2026-01-03T00:00:00+08:00")
    second = history.build_preview([_source_row(match)], data_dir,
                                   source_observed_at="2026-01-03T00:00:00+08:00")
    assert first["status"] == "PASS"
    assert first["preview_sha256"] == second["preview_sha256"]
    assert first["preview"]["generation"] == 7
    assert first["preview"]["network_requests"] == 0
    assert first["preview"]["canonical_reconciliation"] is False
    assert _hashes(data_dir) == before


def test_cli_select_and_preview_are_read_only(base, tmp_path, monkeypatch):
    data_dir, match, snap = base
    before = _hashes(data_dir)
    output = tmp_path / "select.json"
    assert cli.main(["select", "--data-dir", str(data_dir),
                     "--output", str(output)]) == 0
    selected = json.loads(output.read_text(encoding="utf-8"))
    assert selected["target_count"] == 1
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"source_observed_at": "2026-01-03T00:00:00+08:00",
                                    "rows": [_source_row(match)]}, ensure_ascii=False),
                        encoding="utf-8")
    preview_out = tmp_path / "preview.json"
    exit_code = cli.main(["preview", "--data-dir", str(data_dir),
                          "--evidence", str(evidence),
                          "--evidence-dir", str(tmp_path / "evidence-dir"),
                          "--output", str(preview_out)])
    assert exit_code == 0
    preview = json.loads(preview_out.read_text(encoding="utf-8"))
    assert preview["status"] == "PASS"
    assert preview["target_count"] == 1
    assert _hashes(data_dir) == before


def test_cli_acquire_requires_explicit_network_authorization(base, tmp_path, monkeypatch):
    data_dir, match, snap = base
    called = []
    from utils import fetcher_500
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches",
                        lambda date=None: called.append(date) or [])
    output = tmp_path / "blocked.json"
    code = cli.main(["acquire", "--data-dir", str(data_dir),
                     "--date", "2026-01-02",
                     "--evidence-dir", str(tmp_path / "evidence"),
                     "--output", str(output)])
    assert code == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["reason"] == "NETWORK_AUTHORIZATION_REQUIRED"
    assert payload["requests_performed"] == 0
    assert called == []


def test_cli_acquire_with_mock_retains_evidence_without_data_writes(base, tmp_path, monkeypatch):
    data_dir, match, snap = base
    before = _hashes(data_dir)
    from utils import fetcher_500
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches",
                        lambda date=None: [_source_row(match)])
    monkeypatch.setattr(fetcher_500, "source_diagnostics", lambda: {"wanchang": {"status": "ok"}})
    evidence_dir = tmp_path / "evidence"
    output = tmp_path / "acquire.json"
    code = cli.main(["acquire", "--data-dir", str(data_dir),
                     "--date", "2026-01-02", "--allow-network", "--max-requests", "1",
                     "--evidence-dir", str(evidence_dir), "--output", str(output)])
    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["requests_performed"] == 1
    assert payload["row_count"] == 1
    assert (evidence_dir / "manifest.json").is_file()
    assert list(evidence_dir.glob("*.rows.json"))
    assert _hashes(data_dir) == before
