import copy
import json

import pytest

import config
from tools import nba_confirmed_result as tool
from utils import prediction_snapshots, refresh_state
from utils.daily_loader import BEIJING_TZ, override_beijing_now
from datetime import datetime


def _official_periods(scores):
    return [{"period": i, "periodType": "REGULAR" if i <= 4 else "OVERTIME", "score": s}
            for i, s in enumerate(scores, start=1)]


def _bdl_periods(prefix, scores):
    out = {}
    for i, s in enumerate(scores, start=1):
        out[f"{prefix}_q{i}" if i <= 4 else f"{prefix}_ot{i - 4}"] = s
    return out


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
                "gameStatusText": "Final", "period": 4,
                "homeTeam": {"teamId": 1610612753, "teamTricode": "ORL", "score": 107,
                             "periods": _official_periods([23, 29, 32, 23])},
                "awayTeam": {"teamId": 1610612749, "teamTricode": "MIL", "score": 99,
                             "periods": _official_periods([15, 22, 35, 27])}}
    bdl = {"id": 123, "datetime": official["gameTimeUTC"], "status_state": "final", "status": "Final",
           "postponed": False, "period": 4, "home_team": {"id": 22}, "visitor_team": {"id": 17},
           "home_team_score": 107, "visitor_team_score": 99,
           **_bdl_periods("home", [23, 29, 32, 23]), **_bdl_periods("visitor", [15, 22, 35, 27])}
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


def bulls_hawks(scene, *, home_scores=(33, 27, 24, 26, 15), away_scores=(24, 29, 30, 27, 7),
                period=5, final=True):
    """第二合成赛事（公牛/老鹰），镜像真实加时危害场景；纯 bind_result 测试无需落盘。"""
    m, s, o, b, kw, _ = copy.deepcopy(scene)
    m.update(id="synthetic-bulls", home="芝加哥公牛", away="亚特兰大老鹰", jczq_no="5310")
    m["kickoff_provenance"]["kickoff_source_event_id"] = "124"
    s.update(match_id=m["id"], home_team=m["home"], away_team=m["away"])
    s["snapshot_id"] = prediction_snapshots.snapshot_id_for(m["id"], s["model_version"])
    o.update(gameId="0022600037", period=period,
             gameStatus=3 if final else 2, gameStatusText="Final" if final else "OT1")
    o["homeTeam"] = {"teamId": 1610612741, "teamTricode": "CHI", "score": sum(home_scores),
                     "periods": _official_periods(home_scores)}
    o["awayTeam"] = {"teamId": 1610612737, "teamTricode": "ATL", "score": sum(away_scores),
                     "periods": _official_periods(away_scores)}
    b.update(id=124, period=period, home_team={"id": 5}, visitor_team={"id": 1},
             home_team_score=sum(home_scores), visitor_team_score=sum(away_scores),
             status_state="final" if final else "in_progress", status="Final" if final else "OT1")
    b.update(_bdl_periods("home", home_scores), **_bdl_periods("visitor", away_scores))
    kw["source_reference"] = "https://www.nba.com/game/atl-vs-chi-0022600037"
    return m, s, o, b, kw


def test_offline_preview_preserves_identity_and_all_stores(scene):
    before = refresh_state.store_digests(config.DATA_DIR)
    p = plan(scene)
    assert refresh_state.store_digests(config.DATA_DIR) == before
    m = scene[0]
    resolved = p["targets"][0]["resolved_match"]
    assert {k: v for k, v in resolved.items() if k not in {"status", "score", "result_provenance"}} == {
        k: v for k, v in m.items() if k not in {"status", "score", "result_provenance"}}
    assert resolved["score"] == {"ft": [107, 99]}
    assert resolved["result_provenance"]["final_period"] == 4
    assert resolved["result_provenance"]["overtime"] is False
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
    assert first["status"] == "COMPLETED"
    assert first["results"][0]["settlement_created"] and first["results"][0]["evaluation_created"]
    after = refresh_state.store_digests(config.DATA_DIR)
    assert [n for n in before if before[n] != after[n]] == [n for n in before if n in p["write_scope"]]
    assert json.loads((scene[-1]/"daily_matches.json").read_text(encoding="utf-8"))["matches"][1]["status"] == "upcoming"
    p2 = plan(scene)
    second = tool.execute(p2, expected_preview_sha256=p2["preview_sha256"], receipt_path=scene[-1]/"receipt2")
    assert not second["results"][0]["settlement_created"] and not second["results"][0]["evaluation_created"]
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
    assert tool.settlements.get_settlement(p["targets"][0]["settlement_id"]) is not None
    receipt = json.loads((scene[-1]/"receipt").read_text(encoding="utf-8"))
    assert receipt["status"] == "FAILED" and receipt["completed_targets"] == []
    with pytest.raises(ValueError, match="INCONSISTENT_RUNTIME"):
        plan(scene)


def test_conflicting_existing_canonical_result_is_not_repaired(scene):
    m, s, o, b, kw, _ = copy.deepcopy(scene)
    m.update(status="finished", score={"ft": [108, 99]})
    from utils.match_lifecycle import MatchLifecycleConflict
    with pytest.raises(MatchLifecycleConflict):
        tool.bind_result(m, s, o, b, **kw)


# ---- Workstream C: overtime hazard safeguards (deterministic synthetic cases) ----

def test_regulation_tie_is_not_final(scene):
    m, s, o, b, kw = bulls_hawks(scene, home_scores=(33, 27, 24, 26), away_scores=(24, 29, 30, 27), period=4)
    assert o["homeTeam"]["score"] == o["awayTeam"]["score"] == 110
    with pytest.raises(ValueError, match="REGULATION_TIE_NOT_FINAL"):
        tool.bind_result(m, s, o, b, **kw)


def test_overtime_in_progress_is_not_final(scene):
    m, s, o, b, kw = bulls_hawks(scene, final=False)
    with pytest.raises(ValueError, match="OVERTIME_IN_PROGRESS"):
        tool.bind_result(m, s, o, b, **kw)


def test_final_ot_score_confirmed_is_accepted(scene):
    m, s, o, b, kw = bulls_hawks(scene)
    resolved = tool.bind_result(m, s, o, b, **kw)
    assert resolved["status"] == "finished" and resolved["score"] == {"ft": [125, 117]}
    assert resolved["result_provenance"]["final_period"] == 5
    assert resolved["result_provenance"]["overtime"] is True
    assert resolved["result_provenance"]["nba_official_game_id"] == "0022600037"


def test_conflicting_final_sources_rejected(scene):
    m, s, o, b, kw = bulls_hawks(scene)
    b["visitor_team_score"] = 118
    with pytest.raises(ValueError, match="SCORE_CONFLICT"):
        tool.bind_result(m, s, o, b, **kw)


def test_invalid_final_team_orientation_rejected(scene):
    m, s, o, b, kw = bulls_hawks(scene)
    b["home_team"], b["visitor_team"] = b["visitor_team"], b["home_team"]
    with pytest.raises(ValueError, match="TEAM_ORIENTATION_MISMATCH"):
        tool.bind_result(m, s, o, b, **kw)


@pytest.mark.parametrize("mutation", ["official_sum", "bdl_sum", "missing_ot", "period_mismatch", "bad_period_type"])
def test_period_reconciliation_failures(scene, mutation):
    m, s, o, b, kw = bulls_hawks(scene)
    if mutation == "official_sum": o["homeTeam"]["periods"][4]["score"] = 16
    if mutation == "bdl_sum": b["home_ot1"] = 16
    if mutation == "missing_ot": del b["home_ot1"]
    if mutation == "period_mismatch": b["period"] = 4
    if mutation == "bad_period_type": o["homeTeam"]["periods"][4]["periodType"] = "REGULAR"
    reason = "PERIOD_MISMATCH" if mutation == "period_mismatch" else (
        "PERIOD_DATA_INCOMPLETE" if mutation in ("missing_ot", "bad_period_type") else "PERIOD_SCORE_RECONCILIATION_FAILED")
    with pytest.raises(ValueError, match=reason):
        tool.bind_result(m, s, o, b, **kw)


# ---- Workstream D: generic fixture identity and official reference validation ----

def test_second_fixture_binds_without_hardcoded_teams(scene):
    m, s, o, b, kw = bulls_hawks(scene)
    resolved = tool.bind_result(m, s, o, b, **kw)
    assert resolved["id"] == "synthetic-bulls"
    assert resolved["home"] == "芝加哥公牛" and resolved["away"] == "亚特兰大老鹰"


@pytest.mark.parametrize("reference", [
    "https://www.nba.com/game/chi-vs-atl-0022600037",
    "https://www.nba.com/game/atl-vs-chi-0022600038",
    "https://example.org/game/atl-vs-chi-0022600037",
    "https://www.nba.com/game/atl-vs-chi-0022600037?x=1",
])
def test_official_reference_must_match_canonical_fixture(scene, reference):
    m, s, o, b, kw = bulls_hawks(scene)
    kw["source_reference"] = reference
    with pytest.raises(ValueError, match="OFFICIAL_REFERENCE_MISMATCH"):
        tool.bind_result(m, s, o, b, **kw)


# ---- Workstream F: batch preview / single-operation execution ----

@pytest.fixture
def batch_scene(scene):
    m, s, o, b, kw, tmp = scene
    m2, s2, o2, b2, kw2 = bulls_hawks(scene)
    daily = json.loads((tmp/"daily_matches.json").read_text(encoding="utf-8"))
    daily["matches"].append(m2)
    (tmp/"daily_matches.json").write_text(json.dumps(daily), encoding="utf-8")
    snaps = json.loads((tmp/config.PREDICTION_SNAPSHOT_FILE).read_text(encoding="utf-8"))
    snaps["snapshots"][s2["snapshot_id"]] = s2
    (tmp/config.PREDICTION_SNAPSHOT_FILE).write_text(json.dumps(snaps), encoding="utf-8")
    owner = refresh_state.owner_record("synthetic")
    generation = refresh_state.begin(str(tmp), owner=owner)
    refresh_state.complete(str(tmp), owner=owner, generation=generation, outcome="COMPLETED")
    entries = [{"match_id": m["id"], "snapshot_id": s["snapshot_id"], "official": o, "bdl": b, **kw},
               {"match_id": m2["id"], "snapshot_id": s2["snapshot_id"], "official": o2, "bdl": b2, **kw2}]
    return entries, tmp


def test_batch_preview_and_single_operation_execution(batch_scene, monkeypatch):
    entries, tmp = batch_scene
    before = refresh_state.store_digests(config.DATA_DIR)
    gen_before = refresh_state.read_state(config.DATA_DIR)["generation"]
    p = tool.preview_batch(entries)
    assert len(p["targets"]) == 2 and p["network_requests"] == 0
    assert refresh_state.store_digests(config.DATA_DIR) == before
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    result = tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=tmp/"receipt")
    assert result["status"] == "COMPLETED" and len(result["results"]) == 2
    assert result["generation"] == gen_before + 1
    assert all(r["settlement_created"] and r["evaluation_created"] for r in result["results"])
    after = refresh_state.store_digests(config.DATA_DIR)
    assert [n for n in before if before[n] != after[n]] == [n for n in before if n in p["write_scope"]]
    receipt = json.loads((tmp/"receipt").read_text(encoding="utf-8"))
    assert receipt["status"] == "COMPLETED" and receipt["target_count"] == 2
    daily = json.loads((tmp/"daily_matches.json").read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in daily["matches"]}
    assert by_id["synthetic-target"]["score"] == {"ft": [107, 99]}
    assert by_id["synthetic-bulls"]["score"] == {"ft": [125, 117]}
    assert by_id["other"]["status"] == "upcoming"


def test_batch_duplicate_targets_rejected(batch_scene):
    entries, _ = batch_scene
    with pytest.raises(ValueError, match="DUPLICATE_TARGET"):
        tool.preview_batch([entries[0], copy.deepcopy(entries[0])])


def test_batch_partial_failure_reports_completed_targets(batch_scene, monkeypatch):
    entries, tmp = batch_scene
    p = tool.preview_batch(entries)
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    real_capture = tool.evaluation_rows.capture_evaluation_row
    calls = []
    def fail_second(snapshot, settlement):
        calls.append(settlement["match_id"])
        if len(calls) == 2:
            raise OSError("synthetic second-game failure")
        return real_capture(snapshot, settlement)
    monkeypatch.setattr(tool.evaluation_rows, "capture_evaluation_row", fail_second)
    with pytest.raises(OSError):
        tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=tmp/"receipt")
    assert len(calls) == 2
    assert refresh_state.read_state(config.DATA_DIR)["status"] == "FAILED"
    receipt = json.loads((tmp/"receipt").read_text(encoding="utf-8"))
    assert receipt["status"] == "FAILED"
    assert [t["match_id"] for t in receipt["completed_targets"]] == ["synthetic-target"]
    assert tool.settlements.get_settlement(p["targets"][0]["settlement_id"]) is not None
    assert tool.settlements.get_settlement(p["targets"][1]["settlement_id"]) is not None


def test_batch_stale_target_precheck_is_zero_write(batch_scene, monkeypatch):
    entries, tmp = batch_scene
    p = tool.preview_batch(entries)
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    daily = json.loads((tmp/"daily_matches.json").read_text(encoding="utf-8"))
    for m in daily["matches"]:
        if m["id"] == "synthetic-bulls":
            m["odds"]["home_win"] = 9.99
    (tmp/"daily_matches.json").write_text(json.dumps(daily), encoding="utf-8")
    monkeypatch.setattr(tool.refresh_state, "store_digests", lambda directory: p["store_hashes"])
    with pytest.raises(ValueError, match="IDENTITY_OR_ODDS_CHANGED"):
        tool.execute(p, expected_preview_sha256=p["preview_sha256"], receipt_path=tmp/"receipt")
    settlements_store = json.loads((tmp/config.SETTLEMENT_FILE).read_text(encoding="utf-8"))["settlements"]
    assert settlements_store == {}
    daily = json.loads((tmp/"daily_matches.json").read_text(encoding="utf-8"))
    assert all(m["status"] == "upcoming" for m in daily["matches"])