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
                home=None, away=None, row_id="500w-2026-01-02-0",
                time_override=None, league_override=None):
    return {
        "id": row_id, "sport": "football", "league": league_override or match.get("league"),
        "date": date or match["date"], "time": time_override or "20:00",
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


# ---------------------------------------------------------------------------
# Phase 6-B Workstream B：目标集绑定、严格证据门、原始证据留存
# ---------------------------------------------------------------------------

def test_out_of_scope_target_is_rejected_not_silently_kept(base):
    data_dir, match, snap = base
    source = _source_row(match)
    result = history.match_source_rows(
        [source], [match], {snap["snapshot_id"]: snap}, {},
        target_match_ids={"500l-OTHER-ID"},
    )
    assert result["candidates"] == []
    reasons = {r["reason"] for r in result["rejected"]}
    assert history.STATUS_OUT_OF_SCOPE in reasons
    in_scope = history.match_source_rows(
        [source], [match], {snap["snapshot_id"]: snap}, {},
        target_match_ids={match["id"]},
    )
    assert len(in_scope["candidates"]) == 1


def test_prediction_must_be_strictly_before_kickoff(base):
    data_dir, match, snap = base
    source = _source_row(match)
    equal_time = dict(snap, generated_at="2026-01-02T20:00:00+08:00")
    result = history.match_source_rows(
        [source], [match], {equal_time["snapshot_id"]: equal_time}, {})
    assert result["candidates"] == []
    assert history.STATUS_NOT_STRICTLY_PREGAME in {r["reason"] for r in result["rejected"]}

    naive = dict(snap, generated_at="2026-01-02T10:00:00")  # naive -> fail closed
    result2 = history.match_source_rows(
        [source], [match], {naive["snapshot_id"]: naive}, {})
    assert history.STATUS_NOT_STRICTLY_PREGAME in {r["reason"] for r in result2["rejected"]}

    missing = dict(snap, kickoff_at=None)
    result3 = history.match_source_rows(
        [source], [match], {missing["snapshot_id"]: missing}, {})
    # 快照侧缺 kickoff 时既有快照有效性门先拒绝（同样 fail closed）
    assert {history.STATUS_NOT_STRICTLY_PREGAME, history.STATUS_NO_SNAPSHOT} & \
        {r["reason"] for r in result3["rejected"]}


def test_snapshot_identity_mismatch_is_rejected(base):
    data_dir, match, snap = base
    source = _source_row(match)
    wrong_home = dict(snap, home_team="别的队")
    result = history.match_source_rows(
        [source], [match], {wrong_home["snapshot_id"]: wrong_home}, {})
    assert history.STATUS_SNAPSHOT_IDENTITY in {r["reason"] for r in result["rejected"]}

    wrong_sport = dict(snap, sport="basketball")
    result2 = history.match_source_rows(
        [source], [match], {wrong_sport["snapshot_id"]: wrong_sport}, {})
    assert history.STATUS_SNAPSHOT_IDENTITY in {r["reason"] for r in result2["rejected"]}


def test_source_kickoff_and_competition_evidence_required(base):
    data_dir, match, snap = base
    source = _source_row(match)
    mismatched_time = _source_row(match, time_override="21:00")
    result = history.match_source_rows(
        [mismatched_time], [match], {snap["snapshot_id"]: snap}, {})
    assert history.STATUS_KICKOFF_CONFLICT in {r["reason"] for r in result["rejected"]}

    unknown_time = _source_row(match)
    unknown_time["kickoff_time_known"] = False
    result2 = history.match_source_rows(
        [unknown_time], [match], {snap["snapshot_id"]: snap}, {})
    assert history.STATUS_KICKOFF_UNAVAILABLE in {r["reason"] for r in result2["rejected"]}

    other_league = _source_row(match, league_override="另一个联赛")
    result3 = history.match_source_rows(
        [other_league], [match], {snap["snapshot_id"]: snap}, {})
    assert history.STATUS_COMPETITION_CONFLICT in {r["reason"] for r in result3["rejected"]}


def test_preview_target_scope_is_explicit(base):
    data_dir, match, snap = base
    source = _source_row(match)
    unbound = history.build_preview([source], data_dir)
    assert unbound["preview"]["target_scope"] == "UNBOUND"
    assert unbound["preview"]["approved_target_match_ids"] is None
    bound = history.build_preview([source], data_dir,
                                  target_match_ids={match["id"], "500l-OTHER"})
    assert bound["preview"]["target_scope"] == "BOUND"
    assert bound["preview"]["approved_target_match_ids"] == sorted({match["id"], "500l-OTHER"})
    assert bound["preview"]["parser_version"] == history.PARSER_VERSION


def test_preview_consumed_evidence_enters_digest(base):
    data_dir, match, snap = base
    source = _source_row(match)
    plain = history.build_preview([source], data_dir)
    with_evidence = history.build_preview(
        [source], data_dir,
        consumed_evidence=[{"path": "x.rows.json", "sha256": "ab" * 32,
                            "verified_against_manifest": True}])
    assert plain["preview_sha256"] != with_evidence["preview_sha256"]
    assert with_evidence["preview"]["consumed_evidence"][0]["sha256"] == "ab" * 32


def test_acquire_binds_targets_and_rejects_unknown_ids(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    match = _match("500l-2026-01-02-1001")
    _write_stores(data_dir, [match])
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    result = cli._acquire(data_dir, evidence_dir, ["2026-01-02"],
                          allow_network=False, max_requests=1,
                          match_ids=["500l-DOES-NOT-EXIST"])
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "NETWORK_AUTHORIZATION_REQUIRED"
    # 超预算前先拒绝网络，但显式目标超限在授权网络时必须拒绝
    result2 = cli._acquire(data_dir, evidence_dir, ["2026-01-02"],
                           allow_network=True, max_requests=1,
                           match_ids=[f"500l-x-{i}" for i in range(50)])
    assert result2["status"] == "BLOCKED"
    assert result2["reason"] == "TARGET_LIMIT_EXCEEDED"


def test_acquire_persists_raw_response_with_metadata(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    match = _match("500l-2026-01-02-1001")
    _write_stores(data_dir, [match])
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    from utils import fetcher_500
    fake_rows = [{
        "id": "500w-0", "sport": "football", "league": "测试联赛", "round": "",
        "date": "2026-01-02", "time": "20:00", "kickoff_time_known": True,
        "status": "finished", "home": "主队", "away": "客队",
        "score": {"ft": [2, 1]}, "odds": None,
    }]
    fake_raw = {
        "requested_date": "2026-01-02",
        "request_started_at": "2026-01-02T10:00:00+08:00",
        "response_observed_at": "2026-01-02T10:00:01+08:00",
        "http_status": 200, "error_type": None,
        "final_url": "https://live.500.com/wanchang.php?e=2026-01-02",
        "redirect_count": 0,
        "response_sha256": "cd" * 32,
        "response_byte_count": 11,
        "response_bytes": b"<html>ok</html>",
    }
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda date=None: fake_rows)
    monkeypatch.setattr(fetcher_500, "last_wanchang_raw_observation", lambda: dict(fake_raw))
    monkeypatch.setattr(fetcher_500, "wanchang_raw_response_bytes", lambda: fake_raw["response_bytes"])
    monkeypatch.setattr(fetcher_500, "source_diagnostics",
                        lambda: {"wanchang": {"status": "ok"}})

    result = cli._acquire(data_dir, evidence_dir, ["2026-01-02"],
                          allow_network=True, max_requests=1, match_ids=[match["id"]])
    assert result["status"] == "OK"
    entry = result["requests"][0]
    for key in ("actual_request_started_at", "actual_response_observed_at", "http_status",
                "response_sha256", "redirect_count", "parser_version", "observed_date_window"):
        assert key in entry
    assert entry["observed_date_window"]["min"] == "2026-01-02"
    assert result["approved_target_match_ids"] == [match["id"]]
    raw_path = Path(entry["raw_evidence_path"])
    assert raw_path.is_file() and raw_path.read_bytes() == b"<html>ok</html>"

    # 预览从 manifest 绑定目标集并复核证据摘要
    preview = cli._preview(data_dir, evidence_dir, None)
    assert preview["target_scope"] == "BOUND_MANIFEST"
    assert preview["preview"]["target_scope"] == "BOUND"
    assert preview["preview"]["consumed_evidence"][0]["verified_against_manifest"] is True


def test_preview_fails_closed_on_evidence_digest_mismatch(tmp_path):
    data_dir = tmp_path / "data"
    match = _match("500l-2026-01-02-1001")
    _write_stores(data_dir, [match])
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    manifest = {
        "approved_target_match_ids": [match["id"]],
        "evidence_files": [str(evidence_dir / "stub.rows.json")],
        "requests": [{"evidence_path": str(evidence_dir / "stub.rows.json"),
                      "evidence_sha256": "00" * 32}],
    }
    (evidence_dir / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8", )
    (evidence_dir / "stub.rows.json").write_text('{"rows": [], "source_observed_at": null}',
                                                 encoding="utf-8")
    result = cli._preview(data_dir, evidence_dir, None)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "EVIDENCE_DIGEST_MISMATCH"


def test_fetcher_raw_observation_defaults_empty_and_is_read_only():
    from utils import fetcher_500
    snapshot_before = dict(fetcher_500._LAST_WANCHANG_RAW)
    meta = fetcher_500.last_wanchang_raw_observation()
    assert "response_bytes" not in meta
    assert dict(fetcher_500._LAST_WANCHANG_RAW) == snapshot_before
