"""Isolated tests for the reusable NBA result-evidence workflow."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from tools import nba_result_workflow as workflow
from utils import nba_result_evidence as evidence
from utils import prediction_snapshots, refresh_state

BEIJING = timezone(timedelta(hours=8))
NOW = datetime(2026, 1, 3, 12, 0, tzinfo=BEIJING)
KICKOFF_1 = "2026-01-02T08:00:00+08:00"
KICKOFF_2 = "2026-01-02T10:00:00+08:00"
UTC_1 = "2026-01-02T00:00:00Z"
UTC_2 = "2026-01-02T02:00:00Z"


def _periods(scores):
    return [{"period": i, "periodType": "REGULAR" if i <= 4 else "OVERTIME", "score": s}
            for i, s in enumerate(scores, start=1)]


def _bdl_periods(prefix, scores):
    out = {}
    for i, s in enumerate(scores, start=1):
        out[f"{prefix}_q{i}" if i <= 4 else f"{prefix}_ot{i - 4}"] = s
    return out


def _match(mid, *, code, home, away, number, bdl_id, start_utc, kickoff_time):
    return {
        "id": mid, "sport": "basketball", "league": "美国职业篮球联盟",
        "date": "2026-01-02", "time": kickoff_time, "kickoff_time_known": True,
        "status": "upcoming", "home": home, "away": away, "jczq_no": number,
        "market_tracked": True, "odds": {"home_win": 1.8, "away_win": 2.0},
        "kickoff_provenance": {
            "actual_kickoff_time_source": "balldontlie_nba_games",
            "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
            "kickoff_source_event_id": str(bdl_id),
            "kickoff_source_scheduled_start_utc": start_utc,
        },
    }


def _snapshot(match, kickoff):
    snap = {
        "match_id": match["id"], "sport": "basketball", "league": match["league"],
        "model_name": "elo-normal-points", "model_version": "basketball-margin-1",
        "generated_at": "2026-01-01T22:00:00+08:00", "kickoff_at": kickoff,
        "home_team": match["home"], "away_team": match["away"],
        "model_probabilities": {"home_win": 58, "away_win": 42, "draw": 0},
        "market_odds": {"home_win": 1.8, "away_win": 2.0},
    }
    snap["snapshot_id"] = prediction_snapshots.snapshot_id_for(match["id"], snap["model_version"])
    return snap


def _official(game_id, utc, period, home, away):
    home_scores, away_scores, home_team, away_team = home
    return {
        "gameId": game_id, "gameTimeUTC": utc, "gameStatus": 3,
        "gameStatusText": "Final" if period == 4 else ("Final/OT" if period == 5 else f"Final/{period - 4}OT"),
        "period": period,
        "homeTeam": {"teamId": home_team[0], "teamTricode": home_team[1],
                     "score": sum(home_scores), "periods": _periods(home_scores)},
        "awayTeam": {"teamId": away_team[0], "teamTricode": away_team[1],
                     "score": sum(away_scores), "periods": _periods(away_scores)},
    }


def _bdl(game_id, utc, period, home, away):
    home_scores, away_scores, home_team, away_team = home
    bdl = {
        "id": int(game_id), "datetime": utc, "status_state": "final", "status": "Final",
        "postponed": False, "period": period,
        "home_team": {"id": home_team[2]}, "visitor_team": {"id": away_team[2]},
        "home_team_score": sum(home_scores), "visitor_team_score": sum(away_scores),
    }
    bdl.update(_bdl_periods("home", home_scores))
    bdl.update(_bdl_periods("visitor", away_scores))
    return bdl


def _discovery_html(pairs):
    nodes = [{"gameId": gid, "homeTeam": {"teamTricode": home}, "awayTeam": {"teamTricode": away}}
             for gid, home, away in pairs]
    next_data = {"props": {"pageProps": {"games": nodes}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'.encode()


def _official_html(game):
    next_data = {"props": {"pageProps": {"game": game}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'.encode()


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakeSession:
    def __init__(self, responses, clock):
        self.responses = responses
        self.clock = clock
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, self.clock.t))
        content = self.responses.get(url)
        if content is None:
            return FakeResponse(b"", 500)
        return FakeResponse(content, 200)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    evidence_dir = tmp_path / "evidence"
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    match1 = _match("500j-2026-01-02-7312", code="ORL", home="奥兰多魔术", away="密尔沃基雄鹿",
                    number="7312", bdl_id=123, start_utc=UTC_1, kickoff_time="08:00")
    match2 = _match("500j-2026-01-02-7313", code="CHI", home="芝加哥公牛", away="亚特兰大老鹰",
                    number="7313", bdl_id=124, start_utc=UTC_2, kickoff_time="10:00")
    unbound = _match("500j-2026-01-02-7314", code="BOS", home="波士顿凯尔特人", away="迈阿密热火",
                     number="7314", bdl_id=125, start_utc=UTC_1, kickoff_time="08:00")
    unbound["kickoff_provenance"] = {}
    football = {"id": "500w-2026-01-02-001", "sport": "football", "league": "英超",
                "date": "2026-01-02", "time": "20:00", "status": "upcoming",
                "home": "曼城", "away": "阿森纳", "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0}}
    snap1, snap2 = _snapshot(match1, KICKOFF_1), _snapshot(match2, KICKOFF_2)
    snap3 = _snapshot(unbound, KICKOFF_1)
    official1 = _official("0022600001", UTC_1, 4,
                          ([23, 29, 32, 23], [15, 22, 35, 27],
                           (1610612753, "ORL", 22), (1610612749, "MIL", 17)),
                          None)
    official2 = _official("0022600037", UTC_2, 5,
                          ([33, 27, 24, 26, 15], [24, 29, 30, 27, 7],
                           (1610612741, "CHI", 5), (1610612737, "ATL", 1)),
                          None)
    bdl1 = _bdl(123, UTC_1, 4,
                ([23, 29, 32, 23], [15, 22, 35, 27], (None, None, 22), (None, None, 17)),
                None)
    bdl2 = _bdl(124, UTC_2, 5,
                ([33, 27, 24, 26, 15], [24, 29, 30, 27, 7], (None, None, 5), (None, None, 1)),
                None)
    matches = [match1, match2, unbound, football]
    snapshots = {snap1["snapshot_id"]: snap1, snap2["snapshot_id"]: snap2, snap3["snapshot_id"]: snap3}
    (data_dir / "daily_matches.json").write_text(json.dumps({"matches": matches}), encoding="utf-8")
    (data_dir / config.PREDICTION_SNAPSHOT_FILE).write_text(json.dumps({"snapshots": snapshots}), encoding="utf-8")
    (data_dir / config.SETTLEMENT_FILE).write_text(json.dumps({"settlements": {}}), encoding="utf-8")
    (data_dir / config.EVALUATION_ROW_FILE).write_text(json.dumps({"rows": {}}), encoding="utf-8")
    (data_dir / "odds_snapshots.json").write_text("{}", encoding="utf-8")
    (data_dir / "team_strength.json").write_text("{}", encoding="utf-8")
    (data_dir / "calibrated.json").write_text("{}", encoding="utf-8")
    owner = refresh_state.owner_record("synthetic")
    generation = refresh_state.begin(str(data_dir), owner=owner)
    refresh_state.complete(str(data_dir), owner=owner, generation=generation, outcome="COMPLETED")
    clock = FakeClock()
    monkeypatch.setattr(workflow.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(workflow.time, "sleep", clock.sleep)
    responses = {
        "https://www.nba.com/games?date=2026-01-01": _discovery_html([
            ("0022600001", "ORL", "MIL"),
            ("0022600037", "CHI", "ATL"),
        ]),
        "https://www.nba.com/game/mil-vs-orl-0022600001": _official_html(official1),
        "https://www.nba.com/game/atl-vs-chi-0022600037": _official_html(official2),
        "https://api.balldontlie.io/v1/games/123": json.dumps({"data": bdl1}).encode(),
        "https://api.balldontlie.io/v1/games/124": json.dumps({"data": bdl2}).encode(),
    }
    return {
        "matches": matches, "snapshots": snapshots, "official1": official1, "official2": official2,
        "bdl1": bdl1, "bdl2": bdl2, "responses": responses, "data_dir": data_dir,
        "evidence_dir": evidence_dir, "clock": clock,
    }


def _selection(max_targets=2):
    return evidence.select_pending_targets(max_targets=max_targets, now=NOW)


def _acquire(scene, tmp_path, *, allow_network=True, max_requests=5, credential="secret-key",
             responses=None, clock=None, official_ids=None):
    clock = clock or scene["clock"]
    session = FakeSession(responses if responses is not None else scene["responses"], clock)
    result = workflow._run_acquire(
        _selection(), evidence_dir=scene["evidence_dir"], allow_network=allow_network,
        max_requests=max_requests, allowed_providers=[evidence.PROVIDER_OFFICIAL, evidence.PROVIDER_BDL],
        max_cost=0, official_game_ids=official_ids or {}, pacing_seconds=12.1,
        session=session, credential=credential,
    )
    return result, session, clock


def test_selection_excludes_unbound_and_football(scene):
    result = _selection(max_targets=5)
    ids = {t["match_id"] for t in result["targets"]}
    assert ids == {"500j-2026-01-02-7312", "500j-2026-01-02-7313"}
    assert result["queue"]["PROVIDER_IDENTITY_NOT_ESTABLISHED"] == 1
    assert not any(t["match_id"].endswith("7314") for t in result["targets"])
    assert not any(t["match_id"].startswith("500w") for t in result["targets"])

    bounded = _selection(max_targets=1)
    assert len(bounded["targets"]) == 1
    assert len(bounded["not_selected_eligible"]) == 1

    explicit = evidence.select_pending_targets(["500j-2026-01-02-7314", "500w-2026-01-02-001"], now=NOW)
    statuses = {r["match_id"]: r["selection_status"] for r in explicit["rejected"]}
    assert statuses["500j-2026-01-02-7314"] == evidence.STATUS_UNBOUND
    assert statuses["500w-2026-01-02-001"] == evidence.STATUS_NOT_NBA


def test_plan_requires_authorization_and_budget(scene):
    targets = _selection()["targets"]
    no_auth = evidence.build_request_plan(targets, allow_network=False, max_requests=0)
    assert no_auth["status"] == "NETWORK_AUTHORIZATION_REQUIRED"
    assert no_auth["requests_planned"] == 5
    too_small = evidence.build_request_plan(targets, allow_network=True, max_requests=4)
    assert too_small["status"] == "REQUEST_BUDGET_INSUFFICIENT"
    ready = evidence.build_request_plan(targets, allow_network=True, max_requests=5)
    assert ready["status"] == "PLAN_READY"
    assert ready["expected_cost_usd"] == 0


def test_acquire_without_network_authorization_performs_zero_requests(scene, tmp_path):
    result, session, _ = _acquire(scene, tmp_path, allow_network=False, max_requests=0)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "NETWORK_AUTHORIZATION_REQUIRED"
    assert result["requests_performed"] == 0
    assert session.calls == []


def test_acquire_enforces_budget_before_network(scene, tmp_path):
    result, session, _ = _acquire(scene, tmp_path, allow_network=True, max_requests=4)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "REQUEST_BUDGET_INSUFFICIENT"
    assert session.calls == []


def test_acquire_discovers_retains_and_validates(scene, tmp_path):
    result, session, clock = _acquire(scene, tmp_path)
    assert result["status"] == "OK", result
    assert set(result["accepted"]) == {"500j-2026-01-02-7312", "500j-2026-01-02-7313"}
    assert result["requests_performed"] == 5
    assert result["cache_hits"] == 0
    assert len(session.calls) == 5
    evidence_dir = scene["evidence_dir"]
    assert list((evidence_dir / "nba-official").glob("nba-discovery-2026-01-01*.response"))
    assert (evidence_dir / "nba-official/nba-game-0022600001.response").is_file()
    assert (evidence_dir / "balldontlie/bdl-123.response").is_file()
    manifest = json.loads((evidence_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["requests_performed"] == 5
    assert manifest["targets"]["500j-2026-01-02-7313"]["validation"]["overtime"] is True
    for path in evidence_dir.rglob("*"):
        if path.is_file():
            assert b"secret-key" not in path.read_bytes()
    bdl_calls = [t for url, t in session.calls if "api.balldontlie.io" in url]
    assert bdl_calls[1] - bdl_calls[0] >= 12.1
    assert bdl_calls[1] - bdl_calls[0] >= 12.1 + workflow.PACING_MARGIN_SECONDS - 1e-9


def test_acquire_failure_persists_sanitized_request_diagnostics(scene, tmp_path):
    """早期发现页失败也必须保留请求诊断，而不是只返回一个原因字符串。"""
    result, session, _ = _acquire(scene, tmp_path, responses={})
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "OFFICIAL_DISCOVERY_FETCH_FAILED"
    assert result["requests_performed"] == 1
    assert [url for url, _ in session.calls] == ["https://www.nba.com/games?date=2026-01-01"]
    assert result["requests"][0]["http_status"] == 500
    assert result["first_request"]["endpoint"] == "https://www.nba.com/games?date=2026-01-01"
    run_path = scene["evidence_dir"] / "runs" / f"{result['run_id']}.json"
    assert run_path.is_file()
    persisted = json.loads(run_path.read_text(encoding="utf-8"))
    assert persisted["reason"] == "OFFICIAL_DISCOVERY_FETCH_FAILED"
    assert persisted["requests"][0]["http_status"] == 500
    assert b"secret-key" not in run_path.read_bytes()


def test_acquire_exception_diagnostics_are_sanitized(scene, tmp_path):
    """异常只保留异常类型，不保留异常正文（可能含敏感信息）。"""
    class RaisingSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            raise RuntimeError("boom secret-key must not be persisted")

    session = RaisingSession()
    result = workflow._run_acquire(
        _selection(), evidence_dir=scene["evidence_dir"], allow_network=True,
        max_requests=5, allowed_providers=[evidence.PROVIDER_OFFICIAL, evidence.PROVIDER_BDL],
        max_cost=0, official_game_ids={}, pacing_seconds=12.1,
        session=session, credential="secret-key",
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "OFFICIAL_DISCOVERY_FETCH_FAILED"
    assert result["requests"][0]["error_type"] == "RuntimeError"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "boom" not in serialized
    assert "secret-key" not in serialized


def test_acquire_reuses_validated_cache(scene, tmp_path):
    first, _, _ = _acquire(scene, tmp_path)
    assert first["status"] == "OK"
    second, session, _ = _acquire(scene, tmp_path, credential="", responses={})
    assert second["status"] == "OK"
    assert second["requests_performed"] == 0
    assert second["cache_hits"] == 2
    assert session.calls == []
    assert set(second["accepted"]) == set(first["accepted"])


def test_missing_credentials_blocks_before_network(scene, tmp_path):
    result, session, _ = _acquire(scene, tmp_path, credential="")
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "BLOCKED_MISSING_CREDENTIALS"
    assert result["requests_performed"] == 0
    assert session.calls == []


def test_rejected_target_is_excluded_and_preview_uses_only_accepted(scene, tmp_path):
    swapped = _official_html({**scene["official2"],
                              "homeTeam": scene["official2"]["awayTeam"],
                              "awayTeam": scene["official2"]["homeTeam"]})
    responses = dict(scene["responses"])
    responses["https://www.nba.com/game/atl-vs-chi-0022600037"] = swapped
    result, _, _ = _acquire(scene, tmp_path, responses=responses)
    assert result["status"] == "OK"
    assert result["accepted"] == ["500j-2026-01-02-7312"]
    assert any(r.get("match_id") == "500j-2026-01-02-7313" for r in result["rejected"])
    assert any("TEAM_ORIENTATION_MISMATCH" in str(r.get("reason")) for r in result["rejected"])

    preview = workflow._run_preview(scene["evidence_dir"])
    assert preview["status"] == "PASS"
    assert preview["target_count"] == 1


def test_preview_passes_then_rejects_inconsistent_runtime(scene, tmp_path):
    result, _, _ = _acquire(scene, tmp_path)
    assert result["status"] == "OK"
    preview = workflow._run_preview(scene["evidence_dir"])
    assert preview["status"] == "PASS"
    assert preview["target_count"] == 2
    assert preview["preview_sha256"]

    daily = json.loads((scene["data_dir"] / "daily_matches.json").read_text(encoding="utf-8"))
    daily["matches"][0]["unexpected"] = "drift"
    (scene["data_dir"] / "daily_matches.json").write_text(json.dumps(daily), encoding="utf-8")
    stale = workflow._run_preview(scene["evidence_dir"])
    assert stale["status"] == "BLOCKED"
    assert "INCONSISTENT_RUNTIME" in str(stale["reason"])


def test_discovery_parser_and_unresolved_pair(scene):
    parsed = evidence.parse_discovery_page(scene["responses"]["https://www.nba.com/games?date=2026-01-01"])
    assert parsed["status"] == "OK"
    assert parsed["pairs"]["MIL-ORL"] == ["0022600001"]
    mapping, unresolved = evidence.resolve_official_game_ids(parsed, _selection()["targets"])
    assert mapping["500j-2026-01-02-7312"] == "0022600001"
    assert unresolved == []

    missing = _discovery_html([("0022600001", "ORL", "MIL")])
    parsed_missing = evidence.parse_discovery_page(missing)
    _mapping, unresolved_missing = evidence.resolve_official_game_ids(parsed_missing, _selection()["targets"])
    assert any(item["match_id"] == "500j-2026-01-02-7313" for item in unresolved_missing)


def test_validation_agreement_and_overtime(scene, tmp_path):
    evidence_dir = tmp_path / "direct"
    official_path = evidence_dir / "official.response"
    bdl_path = evidence_dir / "bdl.response"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    official_path.write_bytes(_official_html(scene["official2"]))
    bdl_path.write_bytes(json.dumps({"data": scene["bdl2"]}).encode())
    target = {t["match_id"]: t for t in _selection()["targets"]}["500j-2026-01-02-7313"]
    ok = evidence.validate_target_evidence(
        target, official_path, bdl_path,
        observed_at="2026-01-03T00:00:00+08:00",
        source_reference="https://www.nba.com/game/atl-vs-chi-0022600037",
    )
    assert ok["qualified"] is True
    assert ok["overtime"] is True
    assert ok["final_score"] == [125, 117]

    bad_bdl = {**scene["bdl2"], "home_team_score": 999}
    bad_path = evidence_dir / "bad.response"
    bad_path.write_bytes(json.dumps({"data": bad_bdl}).encode())
    bad = evidence.validate_target_evidence(
        target, official_path, bad_path,
        observed_at="2026-01-03T00:00:00+08:00",
        source_reference="https://www.nba.com/game/atl-vs-chi-0022600037",
    )
    assert bad["qualified"] is False
    assert "SCORE_CONFLICT" in bad["reason"]


def test_explicit_official_ids_skip_discovery(scene, tmp_path):
    result, session, _ = _acquire(
        scene, tmp_path, max_requests=4,
        official_ids={"500j-2026-01-02-7312": "0022600001", "500j-2026-01-02-7313": "0022600037"},
    )
    assert result["status"] == "OK"
    assert result["requests_performed"] == 4
    assert not any("nba.com/games?date=" in url for url, _ in session.calls)


def test_credential_not_in_stdout_or_stderr(scene, monkeypatch, capsys):
    secret = "synthetic-stdout-secret"
    responses = dict(scene["responses"])
    responses["https://www.nba.com/games?date=2026-01-01"] += secret.encode()
    clock = scene["clock"]
    monkeypatch.setattr(workflow, "_session", lambda: FakeSession(responses, clock))
    monkeypatch.setattr(workflow, "_load_credential", lambda: secret)
    exit_code = workflow.main([
        "acquire", "--data-dir", str(scene["data_dir"]),
        "--evidence-dir", str(scene["evidence_dir"]),
        "--max-targets", "2", "--allow-network", "--max-requests", "5",
    ])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert secret not in captured.out
    assert secret not in captured.err


def _protected_hashes(data_dir: Path) -> dict[str, str]:
    names = [
        "daily_matches.json", config.PREDICTION_SNAPSHOT_FILE, "odds_snapshots.json",
        config.SETTLEMENT_FILE, config.EVALUATION_ROW_FILE, "team_strength.json",
        "calibrated.json", ".refresh_state.json",
    ]
    return {name: hashlib.sha256((data_dir / name).read_bytes()).hexdigest() for name in names}


@pytest.mark.parametrize("command", ["select", "plan", "preview", "acquire", "run"])
def test_output_inside_data_dir_rejected_for_every_command(scene, tmp_path, command):
    before = _protected_hashes(scene["data_dir"])
    target = scene["data_dir"] / "daily_matches.json"
    with pytest.raises(SystemExit):
        workflow.main([
            command, "--data-dir", str(scene["data_dir"]),
            "--evidence-dir", str(scene["evidence_dir"]),
            "--max-targets", "1", "--output", str(target),
        ])
    assert _protected_hashes(scene["data_dir"]) == before


def test_protected_path_alias_and_valid_report_output(scene, tmp_path):
    before = _protected_hashes(scene["data_dir"])
    alias = scene["data_dir"] / "nested" / ".." / "daily_matches.json"
    with pytest.raises(SystemExit):
        workflow.main([
            "select", "--data-dir", str(scene["data_dir"]),
            "--evidence-dir", str(scene["evidence_dir"]),
            "--output", str(alias),
        ])
    assert _protected_hashes(scene["data_dir"]) == before

    hardlink = tmp_path / "hardlink.json"
    try:
        os.link(scene["data_dir"] / "daily_matches.json", hardlink)
    except OSError:
        hardlink = None
    if hardlink is not None:
        with pytest.raises(SystemExit):
            workflow.main([
                "select", "--data-dir", str(scene["data_dir"]),
                "--evidence-dir", str(scene["evidence_dir"]),
                "--output", str(hardlink),
            ])
        assert _protected_hashes(scene["data_dir"]) == before

    output = tmp_path / "reports" / "selection.json"
    assert workflow.main([
        "select", "--data-dir", str(scene["data_dir"]),
        "--evidence-dir", str(scene["evidence_dir"]),
        "--max-targets", "1", "--output", str(output),
    ]) == 0
    assert output.is_file()
    assert _protected_hashes(scene["data_dir"]) == before


@pytest.mark.parametrize("source", ["discovery", "official", "bdl"])
def test_credential_detected_before_persistence(scene, tmp_path, source, capsys):
    secret = "synthetic-secret-value"
    responses = dict(scene["responses"])
    if source == "discovery":
        responses["https://www.nba.com/games?date=2026-01-01"] += secret.encode()
    elif source == "official":
        responses["https://www.nba.com/game/mil-vs-orl-0022600001"] += secret.encode()
    else:
        responses["https://api.balldontlie.io/v1/games/123"] += secret.encode()
    result, session, _ = _acquire(scene, tmp_path, responses=responses, credential=secret)
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "CREDENTIAL_LEAK_DETECTED"
    evidence_dir = scene["evidence_dir"]
    if source == "discovery":
        assert not list((evidence_dir / "nba-official").glob("nba-discovery-*"))
    elif source == "official":
        assert not (evidence_dir / "nba-official/nba-game-0022600001.response").exists()
    else:
        assert not (evidence_dir / "balldontlie/bdl-123.response").exists()
    assert not (evidence_dir / "manifest.json").exists()
    for path in evidence_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    assert secret not in json.dumps(result)


def test_explicit_target_limit_and_duplicates(scene):
    ids = ["500j-2026-01-02-7312", "500j-2026-01-02-7313", "500j-2026-01-02-7314"]
    over = evidence.select_pending_targets(ids, max_targets=2, now=NOW)
    assert over["status"] == "REJECTED"
    assert over["reason"] == "TARGET_LIMIT_EXCEEDED"
    assert over["targets"] == []
    assert over["requested_count"] == 3

    duplicates = evidence.select_pending_targets(
        ["500j-2026-01-02-7312", "500j-2026-01-02-7312", "500j-2026-01-02-7313"],
        max_targets=2, now=NOW,
    )
    assert duplicates["status"] == "OK"
    assert duplicates["duplicates_removed"] == 1
    assert len(duplicates["targets"]) == 2

    exit_code = workflow.main([
        "run", "--data-dir", str(scene["data_dir"]),
        "--evidence-dir", str(scene["evidence_dir"]),
        "--target-match-id", ids[0], "--target-match-id", ids[1],
        "--target-match-id", ids[2], "--max-targets", "2",
        "--allow-network", "--max-requests", "10",
    ])
    assert exit_code == 2


def test_cache_retains_original_observation_time(scene, tmp_path):
    first, _, _ = _acquire(scene, tmp_path)
    assert first["status"] == "OK"
    manifest = json.loads((scene["evidence_dir"] / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["targets"]["500j-2026-01-02-7312"]
    assert entry["source_observed_at"] == max(entry["official_observed_at"], entry["bdl_observed_at"])
    assert entry["cache_reused_at"] is None
    original = entry["source_observed_at"]

    second, session, _ = _acquire(scene, tmp_path, credential="", responses={})
    assert second["status"] == "OK"
    assert second["requests_performed"] == 0
    reused = json.loads((scene["evidence_dir"] / "manifest.json").read_text(encoding="utf-8"))[
        "targets"]["500j-2026-01-02-7312"]
    assert reused["source_observed_at"] == original
    assert reused["cache_reused_at"] is not None
    assert session.calls == []


def test_cache_integrity_rejections(scene, tmp_path):
    first, _, _ = _acquire(scene, tmp_path)
    assert first["status"] == "OK"
    manifest_path = scene["evidence_dir"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["targets"]["500j-2026-01-02-7312"]
    assert workflow._cache_rejection_reason(entry, {
        "snapshot_digest": "changed", "bdl_game_id": entry["bdl_game_id"],
        "home_code": entry["home_code"], "away_code": entry["away_code"],
    }) == "CACHE_SNAPSHOT_CHANGED"
    assert workflow._cache_rejection_reason(entry, {
        "snapshot_digest": entry["snapshot_digest"], "bdl_game_id": "999",
        "home_code": entry["home_code"], "away_code": entry["away_code"],
    }) == "CACHE_PROVIDER_IDENTITY_CHANGED"

    entry.pop("source_observed_at")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    blocked, session, _ = _acquire(scene, tmp_path, credential="", responses={})
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "CACHE_INTEGRITY_FAILURE"
    assert blocked["requests_performed"] == 0
    assert session.calls == []


def test_cache_modified_response_rejected(scene, tmp_path):
    first, _, _ = _acquire(scene, tmp_path)
    assert first["status"] == "OK"
    official = scene["evidence_dir"] / "nba-official/nba-game-0022600001.response"
    official.write_bytes(official.read_bytes() + b"\n")
    blocked, _, _ = _acquire(scene, tmp_path, credential="", responses={})
    assert blocked["status"] == "BLOCKED"
    assert blocked["reason"] == "CACHE_INTEGRITY_FAILURE"
    assert any(item["reason"] == "CACHE_RESPONSE_MODIFIED" for item in blocked["cache_rejections"])


def test_unrelated_manifest_target_not_previewed(scene, tmp_path):
    result, _, _ = _acquire(scene, tmp_path)
    assert result["status"] == "OK"
    manifest_path = scene["evidence_dir"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["targets"]["unrelated-target"] = dict(
        next(iter(manifest["targets"].values())), match_id="unrelated-target")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    preview = workflow._run_preview(scene["evidence_dir"])
    assert preview["status"] == "PASS"
    assert preview["target_count"] == 2
    one = workflow._run_preview(scene["evidence_dir"], target_ids=["500j-2026-01-02-7312"])
    assert one["status"] == "PASS"
    assert one["target_count"] == 1
    mismatch = workflow._run_preview(scene["evidence_dir"], target_ids=["missing"])
    assert mismatch["status"] == "BLOCKED"
    assert mismatch["reason"] == "PREVIEW_TARGET_SET_MISMATCH"


def test_official_discovery_date_boundaries_and_override(scene):
    utc = timezone.utc
    safe_midnight = evidence.official_discovery_date_info(datetime(2026, 11, 23, 0, 0, tzinfo=utc), "2026-11-23")
    assert safe_midnight["status"] == "SAFE"
    assert safe_midnight["date"] == "2026-11-22"
    assert "2026-11-23" in safe_midnight["candidates"]

    assert evidence.official_discovery_date_info(datetime(2026, 11, 23, 7, 59, tzinfo=utc), "2026-11-23")["status"] == "SAFE"
    assert evidence.official_discovery_date_info(datetime(2026, 11, 23, 8, 0, tzinfo=utc), "2026-11-23")["status"] == "AMBIGUOUS"
    assert evidence.official_discovery_date_info(datetime(2026, 11, 23, 15, 59, tzinfo=utc), "2026-11-23")["status"] == "AMBIGUOUS"
    assert evidence.official_discovery_date_info(datetime(2026, 11, 23, 16, 0, tzinfo=utc), "2026-11-23")["status"] == "SAFE"

    target = {
        "match_id": "x", "selection_status": evidence.STATUS_ELIGIBLE,
        "official_discovery_date": "2026-11-22", "official_discovery_date_status": "AMBIGUOUS",
    }
    blocked = evidence.build_request_plan([target], allow_network=True, max_requests=10)
    assert blocked["status"] == "OFFICIAL_DATE_AMBIGUOUS"
    overridden = evidence.apply_official_dates([target], {"x": "2026-11-22"})
    assert overridden[0]["official_discovery_date_status"] == "SAFE"
    ready = evidence.build_request_plan(overridden, allow_network=True, max_requests=10)
    assert ready["status"] == "PLAN_READY"
