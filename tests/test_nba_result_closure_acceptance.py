"""
隔离赛果闭环验收（Workstream E）：真实捕获的赛前快照 + 既有 NBA 绑定工具。

全部七个存储位于 ``tmp_path``；不发起网络请求、不触碰生产数据。验收条件逐条断言：

ORIGINAL_SNAPSHOT_UNCHANGED / FINAL_RESULT_SOURCE_IDENTIFIED / GAME_IDENTITY_VERIFIED /
HOME_AWAY_ORIENTATION_VERIFIED / FINAL_SCORE_VALIDATED / OVERTIME_RULES_PRESERVED /
SETTLEMENT_CREATED_ONCE / EVALUATION_CREATED_ONCE / REPEATED_APPLICATION_IDEMPOTENT /
NO_UNRELATED_CANONICAL_MODIFICATIONS / NO_RETROSPECTIVE_PREDICTION_CREATION。

德篮甲 fixture 单独验证 fail closed：不进入 NBA 路径，只报告
``GERMAN_BASKETBALL_RESULT_DELIVERY = SOURCE_NOT_AVAILABLE``。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from tools import nba_confirmed_result as tool
from utils import evaluation_rows, prediction_snapshots, refresh_state, settlements
from utils.daily_loader import override_beijing_now
from utils.match_identity import same_event
from utils.result_delivery import (
    NON_NBA_RESULT_PATH,
    build_basketball_result_delivery,
)

from test_basketball_operational_acceptance import football_match, write_stores


BEIJING = timezone(timedelta(hours=8))
NBA_ID = "500j-2030-05-16-7312"
GERMAN_ID = "500lq-2030-05-16-7313"
NBA_KICKOFF = datetime(2030, 5, 16, 10, 0, tzinfo=BEIJING)
GERMAN_KICKOFF = datetime(2030, 5, 16, 19, 35, tzinfo=BEIJING)
PREGAME_CLOCK = datetime(2030, 5, 15, 12, 0, tzinfo=BEIJING)
POSTGAME_CLOCK = datetime(2030, 5, 17, 14, 0, tzinfo=BEIJING)
OBSERVED_AT = "2030-05-16T13:00:00+08:00"


def _official_periods(scores):
    return [
        {"period": i, "periodType": "REGULAR" if i <= 4 else "OVERTIME", "score": s}
        for i, s in enumerate(scores, start=1)
    ]


def _bdl_periods(prefix, scores):
    out = {}
    for i, s in enumerate(scores, start=1):
        out[f"{prefix}_q{i}" if i <= 4 else f"{prefix}_ot{i - 4}"] = s
    return out


def _nba_match():
    return {
        "id": NBA_ID,
        "sport": "basketball",
        "league": "美国职业篮球联盟",
        "round": "7312",
        "date": "2030-05-16",
        "time": "10:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "奥兰多魔术",
        "away": "密尔沃基雄鹿",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "market_tracked": True,
        "jczq_no": "周日312",
        "odds": {"home_win": 1.70, "away_win": 2.10, "matchnum": "周日312"},
        "kickoff_provenance": {
            "actual_kickoff_time_source": "balldontlie_nba_games",
            "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
            "kickoff_source_event_id": "123",
            "kickoff_source_observed_at": "2030-05-15T11:00:00+08:00",
            "kickoff_source_scheduled_start_utc": "2030-05-16T02:00:00Z",
        },
    }


def _german_match():
    return {
        "id": GERMAN_ID,
        "sport": "basketball",
        "league": "德篮甲",
        "round": "7313",
        "date": "2030-05-16",
        "time": "19:35",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "拜仁慕尼黑",
        "away": "柏林阿尔巴",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "market_tracked": True,
        "jczq_no": "周日310",
        "odds": {"home_win": 1.55, "away_win": 2.45, "matchnum": "周日310"},
    }


def _basketball_prediction(match):
    return {
        "model_probabilities": {"home_win": 56, "away_win": 44, "draw": 0},
        "probabilities": {"home_win": 56, "away_win": 44, "draw": 0},
        "market": {"odds": match.get("odds") or {}, "implied_probabilities": {}},
        "basketball": {
            "expected_home_points": 112.5,
            "expected_away_points": 108.0,
            "expected_total": 220.5,
            "spread": -4.5,
        },
    }


def _final_evidence(*, home_scores=(25, 30, 28, 24, 10), away_scores=(22, 26, 31, 26, 7),
                    period=5, game_status=3, game_status_text=None, game_id="0022600153"):
    official = {
        "gameId": game_id,
        "gameTimeUTC": "2030-05-16T02:00:00Z",
        "gameStatus": game_status,
        "gameStatusText": game_status_text or ("Final/OT" if period == 5 else "Final"),
        "period": period,
        "homeTeam": {
            "teamId": 1610612753,
            "teamTricode": "ORL",
            "score": sum(home_scores),
            "periods": _official_periods(home_scores),
        },
        "awayTeam": {
            "teamId": 1610612749,
            "teamTricode": "MIL",
            "score": sum(away_scores),
            "periods": _official_periods(away_scores),
        },
    }
    bdl = {
        "id": 123,
        "datetime": official["gameTimeUTC"],
        "status_state": "final" if game_status == 3 else "in_progress",
        "status": "Final" if game_status == 3 else "OT1",
        "postponed": False,
        "period": period,
        "home_team": {"id": 22},
        "visitor_team": {"id": 17},
        "home_team_score": sum(home_scores),
        "visitor_team_score": sum(away_scores),
        **_bdl_periods("home", home_scores),
        **_bdl_periods("visitor", away_scores),
    }
    return official, bdl


@pytest.fixture()
def scene(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", False)

    nba = _nba_match()
    german = _german_match()
    football = football_match("500w-2030-05-16-3001", date="2030-05-16")
    write_stores(tmp_path, [nba, german, football])

    with override_beijing_now(PREGAME_CLOCK):
        nba_capture = prediction_snapshots.capture_snapshot(nba, _basketball_prediction(nba))
        german_capture = prediction_snapshots.capture_snapshot(german, _basketball_prediction(german))
    assert nba_capture.created and german_capture.created
    assert nba_capture.classification == prediction_snapshots.CAPTURED_BEFORE_KICKOFF

    # 真实捕获会写侧车锁与快照库；为预览建立与当前七库摘要一致的完成标记。
    owner = refresh_state.owner_record("pytest-acceptance")
    generation = refresh_state.begin(str(tmp_path), owner=owner)
    refresh_state.complete(str(tmp_path), owner=owner, generation=generation, outcome="COMPLETED")

    baseline_digests = refresh_state.store_digests(str(tmp_path))
    baseline_daily = json.loads((tmp_path / "daily_matches.json").read_text(encoding="utf-8"))
    yield {
        "tmp": tmp_path,
        "nba": nba,
        "german": german,
        "football": football,
        "nba_snapshot": nba_capture.snapshot,
        "german_snapshot": german_capture.snapshot,
        "baseline_digests": baseline_digests,
        "baseline_daily": baseline_daily,
    }


def _plan(scene):
    official, bdl = _final_evidence()
    return tool.preview(
        scene["nba"]["id"],
        scene["nba_snapshot"]["snapshot_id"],
        official,
        bdl,
        observed_at=OBSERVED_AT,
        source_reference="https://www.nba.com/game/mil-vs-orl-0022600153",
    )


def test_isolated_result_closure_acceptance(scene, monkeypatch):
    with override_beijing_now(POSTGAME_CLOCK):
        plan = _plan(scene)
    assert plan["network_requests"] == 0 and plan["canonical_reconciliation"] is False
    assert plan["targets"][0]["resolved_match"]["score"] == {"ft": [117, 112]}
    assert refresh_state.store_digests(str(scene["tmp"])) == scene["baseline_digests"]

    monkeypatch.setattr(config, "RUNTIME_WRITES_ENABLED", True)
    with override_beijing_now(POSTGAME_CLOCK):
        first = tool.execute(
            plan,
            expected_preview_sha256=plan["preview_sha256"],
            receipt_path=scene["tmp"] / "receipt-1.json",
        )
    assert first["status"] == "COMPLETED"
    result = first["results"][0]
    assert result["settlement_created"] is True and result["evaluation_created"] is True

    daily_after = json.loads((scene["tmp"] / "daily_matches.json").read_text(encoding="utf-8"))
    by_id = {m["id"]: m for m in daily_after["matches"]}
    bound = by_id[NBA_ID]

    # FINAL_RESULT_SOURCE_IDENTIFIED：双源标识与引用均记录，人工复核标记为 false。
    provenance = bound["result_provenance"]
    assert provenance["provider"] == "nba_official_game_page"
    assert provenance["nba_official_game_id"] == "0022600153"
    assert provenance["balldontlie_game_id"] == "123"
    assert provenance["source_reference"] == "https://www.nba.com/game/mil-vs-orl-0022600153"
    assert provenance["human_reviewed"] is False
    assert "approved" not in provenance and "signature" not in provenance

    # GAME_IDENTITY_VERIFIED：身份未改写，BDL 绑定仍指向既有 kickoff_source_event_id。
    assert bound["id"] == NBA_ID
    assert same_event(scene["nba"], bound)
    assert bound["kickoff_provenance"]["kickoff_source_event_id"] == "123"
    assert bound["jczq_no"] == "周日312"

    # HOME_AWAY_ORIENTATION_VERIFIED：主客未交换，得分与方向一致。
    assert (bound["home"], bound["away"]) == (scene["nba"]["home"], scene["nba"]["away"])
    assert bound["score"] == {"ft": [117, 112]}
    assert provenance["final_period"] == 5 and provenance["overtime"] is True

    # FINAL_SCORE_VALIDATED：逐节加总与总分一致（两源相同）。
    official, bdl = _final_evidence()
    assert sum(entry["score"] for entry in official["homeTeam"]["periods"]) == official["homeTeam"]["score"] == 117
    assert sum(entry["score"] for entry in official["awayTeam"]["periods"]) == official["awayTeam"]["score"] == 112
    assert bdl["home_team_score"] == 117 and bdl["visitor_team_score"] == 112

    # OVERTIME_RULES_PRESERVED：终场加时被接受；常规时间平局与加时进行中均 fail closed。
    tied_official, tied_bdl = _final_evidence(
        home_scores=(28, 27, 27, 28), away_scores=(27, 28, 28, 27),
        period=4, game_status=3, game_status_text="Final",
    )
    with override_beijing_now(POSTGAME_CLOCK):
        with pytest.raises(ValueError, match="REGULATION_TIE_NOT_FINAL"):
            tool.bind_result(
                scene["nba"], scene["nba_snapshot"], tied_official, tied_bdl,
                observed_at=OBSERVED_AT, source_reference="https://www.nba.com/game/mil-vs-orl-0022600153",
            )
        overtime_official, overtime_bdl = _final_evidence(game_status=2, game_status_text="OT1")
        with pytest.raises(ValueError, match="OVERTIME_IN_PROGRESS"):
            tool.bind_result(
                scene["nba"], scene["nba_snapshot"], overtime_official, overtime_bdl,
                observed_at=OBSERVED_AT, source_reference="https://www.nba.com/game/mil-vs-orl-0022600153",
            )

    # SETTLEMENT_CREATED_ONCE / EVALUATION_CREATED_ONCE：各恰好一条。
    assert len(settlements.get_settlements_for_match(NBA_ID)) == 1
    assert len(evaluation_rows.get_evaluation_rows_for_match(NBA_ID)) == 1
    assert settlements.get_settlement(result["settlement_id"]) is not None
    assert evaluation_rows.get_evaluation_row(result["evaluation_id"]) is not None

    # ORIGINAL_SNAPSHOT_UNCHANGED：不可变快照逐字节保留。
    after_first = refresh_state.store_digests(str(scene["tmp"]))
    stored_snapshot = prediction_snapshots.get_snapshot(scene["nba_snapshot"]["snapshot_id"])
    assert stored_snapshot == scene["nba_snapshot"]
    assert after_first[config.PREDICTION_SNAPSHOT_FILE] == scene["baseline_digests"][config.PREDICTION_SNAPSHOT_FILE]
    assert after_first[config.ODDS_SNAPSHOT_FILE] == scene["baseline_digests"][config.ODDS_SNAPSHOT_FILE]

    # 写入范围之外的四库字节不变；写入的三库之外的记录保持不变。
    changed = {name for name, digest in scene["baseline_digests"].items() if after_first[name] != digest}
    assert changed == set(tool.WRITE_SCOPE)

    # NO_UNRELATED_CANONICAL_MODIFICATIONS：其他赛事与原记录一致。
    for match in scene["baseline_daily"]["matches"]:
        if match["id"] == NBA_ID:
            continue
        assert by_id[match["id"]] == match
    unchanged_keys = {k: v for k, v in bound.items()
                      if k not in {"status", "score", "result_provenance"}}
    original_keys = {k: v for k, v in scene["nba"].items()
                     if k not in {"status", "score", "result_provenance"}}
    assert unchanged_keys == original_keys

    # REPEATED_APPLICATION_IDEMPOTENT：再次执行不新增结算/评估，七库字节不变。
    with override_beijing_now(POSTGAME_CLOCK):
        plan_again = _plan(scene)
        second = tool.execute(
            plan_again,
            expected_preview_sha256=plan_again["preview_sha256"],
            receipt_path=scene["tmp"] / "receipt-2.json",
        )
    assert second["status"] == "COMPLETED"
    assert second["results"][0]["settlement_created"] is False
    assert second["results"][0]["evaluation_created"] is False
    assert refresh_state.store_digests(str(scene["tmp"])) == after_first
    assert len(settlements.get_settlements_for_match(NBA_ID)) == 1
    assert len(evaluation_rows.get_evaluation_rows_for_match(NBA_ID)) == 1

    # NO_RETROSPECTIVE_PREDICTION_CREATION：开赛后新建快照被拒绝且不落盘。
    with override_beijing_now(POSTGAME_CLOCK):
        late = prediction_snapshots.capture_snapshot(
            scene["nba"], _basketball_prediction(scene["nba"]),
            model_version="basketball-margin-2-shadow",
        )
    assert late.created is False
    assert late.classification == prediction_snapshots.KICKOFF_REACHED_BEFORE_CAPTURE
    final_digests = refresh_state.store_digests(str(scene["tmp"]))
    assert final_digests[config.PREDICTION_SNAPSHOT_FILE] == scene["baseline_digests"][config.PREDICTION_SNAPSHOT_FILE]


def test_german_fixture_fails_closed_outside_nba_path(scene):
    report = build_basketball_result_delivery(
        [scene["german"]],
        snapshots={"snapshots": {scene["german_snapshot"]["snapshot_id"]: scene["german_snapshot"]}},
        settlements={"settlements": {}},
        observations={},
        now=POSTGAME_CLOCK,
    )
    assert report["pending_total"] == 1
    assert report["nba_pending"] == 0 and report["non_nba_pending"] == 1
    target = report["targets"][0]
    assert target["is_nba"] is False
    assert target["provider_binding"] is None
    assert target["result_path"]["state"] == NON_NBA_RESULT_PATH == "SOURCE_NOT_AVAILABLE"
    assert target["result_path"]["reason"] == "NO_APPROVED_COMPETITION_COMPATIBLE_RESULT_SOURCE"
    assert target["result_path"]["nba_path_applicable"] is False
    assert report["network_requests_performed"] == 0
    assert report["writes_performed"] == 0

    # 把德篮甲记录送进 NBA 绑定器必须被现有校验器拒绝，而不是被“借用”NBA 证据关闭。
    german_snapshot = copy.deepcopy(scene["german_snapshot"])
    with override_beijing_now(POSTGAME_CLOCK):
        with pytest.raises(ValueError, match="PRIOR_PROVIDER_BINDING_REQUIRED"):
            tool.bind_result(
                scene["german"], german_snapshot, {}, {},
                observed_at=OBSERVED_AT,
                source_reference="https://www.nba.com/game/mil-vs-orl-0022600153",
            )
    assert refresh_state.store_digests(str(scene["tmp"])) == scene["baseline_digests"]
