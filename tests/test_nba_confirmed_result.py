import copy
import json

import pytest

import config
from tools import nba_confirmed_result as tool
from utils import prediction_snapshots, refresh_state
from utils.daily_loader import BEIJING_TZ, override_beijing_now
from datetime import datetime


@pytest.fixture
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", False)
    match = {"id": "synthetic-target", "sport": "basketball", "date": "2026-01-02",
             "time": "08:00", "kickoff_time_known": True, "status": "upcoming",
             "home": "奥兰多魔术", "away": "密尔沃基雄鹿", "jczq_no": "5306",
             "market_tracked": True, "odds": {"home_win": 1.12, "away_win": 3.5},
             "kickoff_provenance": {"actual_kickoff_time_source": "balldontlie_nba_games",
               "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
               "kickoff_source_event_id": "123", "kickoff_source_scheduled_start_utc": "2026-01-02T00:00:00Z"}}
    snap = {"match_id": match["id"], "model_version": "basketball-margin-1", "sport": "basketball",
            "generated_at": "2026-01-01T22:00:00+08:00", "kickoff_at": "2026-01-02T08:00:00+08:00",
            "home_team": match["home"], "away_team": match["away"], "model_name": "elo-normal-points",
            "model_probabilities": {"home_win": 54, "away_win": 46, "draw": 0}}
    snap["snapshot_id"] = prediction_snapshots.snapshot_id_for(match["id"], snap["model_version"])
    official = {"gameId": "0022600001", "gameTimeUTC": "2026-01-02T00:00:00Z", "gameStatus": 3,
                "gameStatusText": "Final", "homeTeam": {"teamId": 1610612753, "teamTricode": "ORL", "score": 107},
                "awayTeam": {"teamId": 1610612749, "teamTricode": "MIL", "score": 99}}
    bdl = {"id": 123, "datetime": official["gameTimeUTC"], "status_state": "final", "status": "Final",
           "postponed": False, "home_team": {"id": 22}, "visitor_team": {"id": 17},
           "home_team_score": 107, "visitor_team_score": 99}
    for name in refresh_state.PROTECTED_STORES:
        value = {"sentinel": name}
        if name == "daily_matches.json": value = {"matches": [match, dict(match, id="other", kickoff_provenance={})]}
        if name == config.PREDICTION_SNAPSHOT_FILE: value = {"snapshots": {snap["snapshot_id"]: snap}}
        if name == config.SETTLEMENT_FILE: value = {"settlements": {}}
        if name == config.EVALUATION_ROW_FILE: value = {"rows": {}}
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    owner = refresh_state.owner_record("synthetic")
    generation = refresh_state.begin(str(tmp_path), owner=owner)
    refresh_state.complete(str(tmp_path), owner=owner, generation=generation, outcome="COMPLETED")
    with override_beijing_now(datetime(2026, 1, 2, 14, tzinfo=BEIJING_TZ)):
        yield match, snap, official, bdl, {"observed_at": "2026-01-02T13:00:00+08:00",
            "source_reference": "https://www.nba.com/game/mil-vs-orl-0022600001"}, tmp_path


def plan(scene):
    m, s, o, b, kw, _ = scene
    return tool.preview(m["id"], s["snapshot_id"], o, b, **kw)


def test_offline_preview_preserves_identity_and_all_stores(scene):
    before = refresh_state.store_digests(config.DATA_DIR)
    p = plan(scene)
    assert refresh_state.store_digests(config.DATA_DIR) == before
    m = scene[0]
    assert {k: v for k, v in p["resolved_match"].items() if k not in {"status", "score", "result_provenance"}} == {
        k: v for k, v in m.items() if k not in {"status", "score", "result_provenance"}}
    assert p["resolved_match"]["score"] == {"ft": [107, 99]}
    assert plan(scene) == p


@pytest.mark.parametrize("mutation", ["bdl_id", "orientation", "score", "tied", "not_final", "kickoff", "snapshot", "reference", "untracked", "boolean_score"])
def test_binding_rejects_unresolved_or_conflicting_evidence(scene, mutation):
    m, s, o, b, kw, _ = copy.deepcopy(scene)
    if mutation == "bdl_id": b["id"] = 124
    if mutation == "orientation": b["home_team"]["id"] = 17
    if mutation == "score": b["home_team_score"] = 108
    if mutation == "tied": o["awayTeam"]["score"] = b["visitor_team_score"] = 107
    if mutation == "not_final": o["gameStatus"] = 2
    if mutation == "kickoff": b["datetime"] = "2026-01-03T00:00:00Z"
    if mutation == "snapshot": s["generated_at"] = s["kickoff_at"]
    if mutation == "reference": kw["source_reference"] = "https://example.org/fake"
    if mutation == "untracked": m["market_tracked"] = False
    if mutation == "boolean_score": o["homeTeam"]["score"] = b["home_team_score"] = True
    with pytest.raises(ValueError): tool.bind_result(m, s, o, b, **kw)


def test_separate_execution_gate_and_stale_plan(scene, monkeypatch):
    p = plan(scene); before = refresh_state.store_digests(config.DATA_DIR)
    with pytest.raises(ValueError, match="RUNTIME_WRITES_DISABLED"):
        tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=scene[-1]/"receipt")
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    (scene[-1]/"team_strength.json").write_text('{"changed": true}')
    with pytest.raises(ValueError, match="INCONSISTENT_RUNTIME|STALE_STORES"):
        tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=scene[-1]/"receipt")
    assert not (scene[-1]/"receipt").exists()
    assert before[config.SETTLEMENT_FILE] == refresh_state.store_digests(config.DATA_DIR)[config.SETTLEMENT_FILE]


def test_real_workflow_in_isolation_and_idempotent_history(scene, monkeypatch):
    p = plan(scene); before = refresh_state.store_digests(config.DATA_DIR)
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    first = tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=scene[-1]/"receipt")
    assert first["settlement_created"] and first["evaluation_created"]
    after = refresh_state.store_digests(config.DATA_DIR)
    assert [n for n in before if before[n] != after[n]] == [n for n in before if n in p["write_scope"]]
    assert json.loads((scene[-1]/"daily_matches.json").read_text(encoding="utf-8"))["matches"][1]["status"] == "upcoming"
    p2 = plan(scene)
    second = tool.execute(p2, expected_preview_sha256=p2["preview_sha256"], receipt_path=scene[-1]/"receipt2")
    assert not second["settlement_created"] and not second["evaluation_created"]
    assert refresh_state.store_digests(config.DATA_DIR) == after
    assert refresh_state.consistency(config.DATA_DIR)["consistent"]


def test_partial_failure_marks_failed_without_rollback_or_retry(scene, monkeypatch):
    p = plan(scene)
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    calls = []
    def fail(*args, **kwargs):
        calls.append(True)
        raise OSError("synthetic write failure")
    monkeypatch.setattr(tool.evaluation_rows, "capture_evaluation_row", fail)
    with pytest.raises(OSError):
        tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=scene[-1]/"receipt")
    assert len(calls) == 1
    assert refresh_state.read_state(config.DATA_DIR)["status"] == "FAILED"
    assert tool.settlements.get_settlement(p["settlement_id"]) is not None
    with pytest.raises(ValueError, match="INCONSISTENT_RUNTIME"):
        plan(scene)


def test_conflicting_existing_canonical_result_is_not_repaired(scene):
    m, s, o, b, kw, _ = copy.deepcopy(scene)
    m.update(status="finished", score={"ft": [108, 99]})
    from utils.match_lifecycle import MatchLifecycleConflict
    with pytest.raises(MatchLifecycleConflict):
        tool.bind_result(m, s, o, b, **kw)
