"""
tests/test_basketball_result_delivery.py
========================================
篮球赛果投递诊断与「即时列表过期」缺陷的确定性复现。

全部数据源为受控合成数据（SYNTHETIC_TEST_VERIFIED）：不发起网络请求、
不触碰 ``data/``、不产生任何生产写入。两个真实 ``scraper.refresh()`` 周期
在临时存储上证明：

    BASKETBALL_PREGAME_FIXTURE_CAPTURED      赛前：赛事准入 + 不可变预测快照
    BASKETBALL_LIVE_SOURCE_STOPS_LISTING     赛后：即时源不再列出该赛事
    ORIGINAL_CANONICAL_FIXTURE_REMAINS_UPCOMING  Canonical 记录保持 upcoming
    GENERIC_FINISHED_SOURCE_HAS_FOOTBALL_ONLY 完场源只产出足球记录
    NO_BASKETBALL_FINAL_RESULT_DELIVERED      没有任何篮球赛果被投递
    NO_RETROACTIVE_PREDICTION                 不回溯生成预测
    NO_UNVERIFIED_SETTLEMENT                  不产生未经验证的结算/评估
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from utils import (
    daily_loader,
    evaluation_rows as evaluation_store,
    fetcher_500,
    prediction_snapshots,
    scraper,
    settlements as settlements_store,
)
from utils.daily_loader import override_beijing_now
from utils.fetcher_500 import reset_source_observation, last_source_observation
from utils.match_identity import same_event
from utils.result_delivery import (
    BASKETBALL_RESULT_SOURCE,
    BDL_BINDING_ABSENT,
    BDL_BINDING_ESTABLISHED,
    BDL_BINDING_INCOMPLETE,
    GENERIC_FINISHED_SOURCE,
    NBA_RESULT_PATH_AWAITING_EVIDENCE,
    NBA_RESULT_PATH_PRIOR_BINDING,
    NON_NBA_RESULT_PATH,
    NO_SOURCE_OBSERVATION,
    RESULT_VERIFICATION_FULL_TWO_SOURCE,
    RESULT_VERIFICATION_INDEPENDENT,
    RESULT_VERIFICATION_NOT_VERIFIED,
    RESULT_VERIFICATION_SOURCE_REPORTED,
    SOURCE_FIXTURE_NOT_PRESENT,
    SOURCE_RECORD_PARSED_BUT_NOT_MATCHED,
    SOURCE_REQUEST_FAILED,
    SOURCE_RETURNED_CONFIRMED_FINAL_RESULT,
    SOURCE_RETURNED_EMPTY,
    SOURCE_RETURNED_NON_FINAL_RESULT,
    build_basketball_result_delivery,
    classify_result_verification,
    classify_target_result_delivery,
    summarize_result_delivery,
)
from test_basketball_operational_acceptance import write_stores

BEIJING = timezone(timedelta(hours=8))
NBA_DATE = "2030-05-16"
NBA_NO = "7312"
NBA_ID = f"500lq-{NBA_DATE}-{NBA_NO}"
KICKOFF = datetime(2030, 5, 16, 10, 0, tzinfo=BEIJING)
BEFORE_KICKOFF = datetime(2030, 5, 15, 12, 0, tzinfo=BEIJING)
AFTER_KICKOFF = datetime(2030, 5, 17, 12, 0, tzinfo=BEIJING)
FOOTBALL_ONLY = "FOOTBALL_ONLY"


# ---------------------------------------------------------------------------
# 分类器夹具
# ---------------------------------------------------------------------------

def nba_target(**overrides):
    record = {
        "id": NBA_ID,
        "sport": "basketball",
        "league": "NBA",
        "date": NBA_DATE,
        "time": "10:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "jczq_no": NBA_NO,
        "market_tracked": True,
        "odds": {"home_win": 1.85, "away_win": 1.95, "matchnum": NBA_NO},
    }
    record.update(overrides)
    return record


def live_row(**overrides):
    record = {
        "id": f"500lq-{NBA_DATE}-周日312",
        "sport": "basketball",
        "league": "NBA",
        "round": "",
        "date": NBA_DATE,
        "time": "10:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "score": None,
        "odds": None,
        "jczq_no": "周日312",
    }
    record.update(overrides)
    return record


def observations(rows=None, *, status="ok", checked_at="2030-05-17T12:00:00+08:00"):
    rows = list(rows or [])
    return {
        BASKETBALL_RESULT_SOURCE: {
            "checked_at": checked_at,
            "status": status,
            "count": len(rows),
            "rows": rows,
        }
    }


def snapshot(match_id, *, kickoff_at=KICKOFF.isoformat(),
             generated_at="2030-05-15T12:00:00+08:00", sid=None,
             model_version="basketball-margin-1"):
    return {
        "snapshot_id": sid or f"snap-{match_id}",
        "match_id": match_id,
        "slot": "pre_match",
        "model_version": model_version,
        "kickoff_at": kickoff_at,
        "generated_at": generated_at,
    }


# ---------------------------------------------------------------------------
# RD-01 … RD-07: 七类来源类别互斥且 fail closed
# ---------------------------------------------------------------------------

def test_rd_01_no_observation_is_not_a_clean_result():
    delivery = classify_target_result_delivery(nba_target(), None)
    assert delivery["status"] == NO_SOURCE_OBSERVATION
    assert delivery["matched_row_count"] == 0
    assert delivery["candidates"] == []
    # 未知来源同样不得被读作「没有缺口」
    assert classify_target_result_delivery(nba_target(), {"other": {}})["status"] == NO_SOURCE_OBSERVATION


def test_rd_02_source_request_failure_is_distinct_from_empty():
    failed = classify_target_result_delivery(
        nba_target(), observations(status="unavailable")
    )
    assert failed["status"] == SOURCE_REQUEST_FAILED
    assert failed["source_status"] == "unavailable"


def test_rd_03_empty_source_is_reported_as_empty():
    empty = classify_target_result_delivery(nba_target(), observations(status="empty"))
    assert empty["status"] == SOURCE_RETURNED_EMPTY
    assert empty["matched_row_count"] == 0


def test_rd_04_fixture_absent_is_not_reported_as_a_missing_provider():
    other_date = live_row(date="2030-05-17", jczq_no="周日401")
    football = {"id": "500w-2030-05-16-1", "sport": "football", "status": "finished",
                "date": NBA_DATE, "home": "甲队", "away": "乙队"}
    delivery = classify_target_result_delivery(
        nba_target(), observations([other_date, football])
    )
    assert delivery["status"] == SOURCE_FIXTURE_NOT_PRESENT
    assert delivery["matched_row_count"] == 0
    assert delivery["candidates"] == []


def test_rd_05_parsed_but_unmatched_row_reports_candidate_evidence_only():
    # 同一场赛事以另一种编号书写形态 + 反向显示顺序出现：只按 same_event 认领，
    # 候选证据参与诊断，但永远不是授权。
    row = live_row(home="金州勇士", away="洛杉矶湖人")
    assert same_event(nba_target(), row) is False
    delivery = classify_target_result_delivery(nba_target(), observations([row]))
    assert delivery["status"] == SOURCE_RECORD_PARSED_BUT_NOT_MATCHED
    assert delivery["matched_row_count"] == 0
    assert len(delivery["candidates"]) == 1
    candidate = delivery["candidates"][0]
    assert candidate["outcome"] in ("CANDIDATE_SAME_EVENT", "AMBIGUOUS", "CONTRADICTORY_EVIDENCE")
    assert candidate["orientation"] == "REVERSED_HOME_AWAY"
    assert candidate["numbers_comparable"] is True
    assert candidate["numbers_agree"] is True
    assert candidate["authoritative"] is False
    assert candidate["is_approval"] is False


def test_rd_06_non_final_and_conflicting_finals_never_count_as_result():
    live = live_row(status="live", score={"ft": [88, 80]}, jczq_no=NBA_NO)
    non_final = classify_target_result_delivery(nba_target(), observations([live]))
    assert non_final["status"] == SOURCE_RETURNED_NON_FINAL_RESULT
    assert non_final["conflict"] is None

    finished_no_score = live_row(status="finished", score=None, jczq_no=NBA_NO)
    assert classify_target_result_delivery(
        nba_target(), observations([finished_no_score])
    )["status"] == SOURCE_RETURNED_NON_FINAL_RESULT

    first = live_row(id="row-a", status="finished", score={"ft": [110, 104]}, jczq_no=NBA_NO)
    second = live_row(id="row-b", status="finished", score={"ft": [111, 104]}, jczq_no=NBA_NO)
    conflict = classify_target_result_delivery(nba_target(), observations([first, second]))
    assert conflict["status"] == SOURCE_RETURNED_NON_FINAL_RESULT
    assert conflict["conflict"] == "CONFLICTING_FINAL_RESULTS"


def test_rd_07_single_consistent_final_result_is_delivered():
    row = live_row(status="finished", score={"ft": [110, 104]}, jczq_no=NBA_NO)
    delivery = classify_target_result_delivery(nba_target(), observations([row]))
    assert delivery["status"] == SOURCE_RETURNED_CONFIRMED_FINAL_RESULT
    assert delivery["final_score"] == [110, 104]
    assert delivery["matched_row_count"] == 1


def test_rd_07b_source_reported_final_is_not_independent_official_verification():
    """单一即时源报告只给 SOURCE_REPORTED_FINAL，独立核验必须另有官方 + BDL 证据。"""
    row = live_row(status="finished", score={"ft": [110, 104]}, jczq_no=NBA_NO)
    snap = snapshot(NBA_ID)
    report = build_basketball_result_delivery(
        [nba_target()],
        {"snapshots": {snap["snapshot_id"]: snap}},
        {"settlements": {}},
        observations([row]),
        now=AFTER_KICKOFF,
    )
    target = report["targets"][0]
    assert target["result_delivery"]["status"] == SOURCE_RETURNED_CONFIRMED_FINAL_RESULT
    assert target["verification_class"] == RESULT_VERIFICATION_SOURCE_REPORTED
    assert target["verification_class"] != RESULT_VERIFICATION_INDEPENDENT
    assert report["stored_final_result_verification_counts"][RESULT_VERIFICATION_INDEPENDENT] == 0

    official = {
        "id": "500j-2030-05-16-7313",
        "sport": "basketball",
        "league": "NBA",
        "date": NBA_DATE,
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "status": "finished",
        "score": {"ft": [110, 104]},
        "result_provenance": {
            "provider": "nba_official_game_page",
            "nba_official_game_id": "0022600153",
            "balldontlie_game_id": "21718162",
        },
    }
    assert classify_result_verification(official) == RESULT_VERIFICATION_INDEPENDENT
    assert classify_result_verification(
        official,
        {"status": SOURCE_RETURNED_CONFIRMED_FINAL_RESULT, "final_score": [110, 104]},
    ) == RESULT_VERIFICATION_FULL_TWO_SOURCE
    assert classify_result_verification(
        official,
        {"status": SOURCE_RETURNED_CONFIRMED_FINAL_RESULT, "final_score": [111, 104]},
    ) == RESULT_VERIFICATION_INDEPENDENT
    assert classify_result_verification({"status": "upcoming"}) == RESULT_VERIFICATION_NOT_VERIFIED

    with_official = build_basketball_result_delivery(
        [official, nba_target()],
        {"snapshots": {snap["snapshot_id"]: snap}},
        {"settlements": {}},
        observations([row]),
        now=AFTER_KICKOFF,
    )
    assert with_official["stored_final_result_verification_counts"][RESULT_VERIFICATION_INDEPENDENT] == 1


# ---------------------------------------------------------------------------
# RD-08: 通用完场源固定产出足球记录
# ---------------------------------------------------------------------------

WANCHANG_HTML = """
<table><tr>
<td>英超</td><td>第1轮</td><td>05-16 20:00</td><td>完</td>
<td><span class="mainName">甲队</span></td><td>2 - 1</td>
<td><span class="mainName">乙队</span></td><td>1 - 0</td>
</tr></table>
"""


class _HtmlResp:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        # 即时篮球页按 gb18030 解码响应字节（与真实源一致）
        self.content = text.encode("gb18030")
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html"}
        self.encoding = "gb2312"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_rd_08_generic_finished_source_records_football_only(monkeypatch):
    fetcher_500._CACHE.clear()
    reset_source_observation()
    monkeypatch.setattr(
        fetcher_500.requests, "get", lambda *a, **k: _HtmlResp(WANCHANG_HTML)
    )
    rows = fetcher_500.fetch_finished_matches()
    assert rows and all(row["sport"] == "football" for row in rows)

    observed = last_source_observation()
    assert GENERIC_FINISHED_SOURCE in observed
    assert {row["sport"] for row in observed[GENERIC_FINISHED_SOURCE]["rows"]} == {"football"}

    report = build_basketball_result_delivery([], {}, {})
    assert report["observations"]["generic_finished_source_scope"] == FOOTBALL_ONLY
    assert report["observations"]["basketball_finished_source_available"] is False
    fetcher_500._CACHE.clear()
    reset_source_observation()


# ---------------------------------------------------------------------------
# RD-09 … RD-11: pending 选择、来源路径与 provider 绑定
# ---------------------------------------------------------------------------

def test_rd_09_pending_selection_separates_nba_and_non_nba():
    reset_source_observation()
    german = nba_target(
        id="500lq-2030-05-16-周日310",
        league="德篮甲",
        jczq_no="周日310",
        status="live",
        home="拜仁慕尼黑",
        away="柏林阿尔巴",
    )
    future = nba_target(
        id="500j-2030-05-18-7420",
        date="2030-05-18",
        time="10:00",
        jczq_no="7420",
        home="波士顿凯尔特人",
        away="迈阿密热火",
    )
    pending_snapshot = snapshot(NBA_ID)
    report = build_basketball_result_delivery(
        [nba_target(), german, future],
        {"snapshots": {pending_snapshot["snapshot_id"]: pending_snapshot,
                       "snap-de": snapshot(german["id"]),
                       "snap-future": snapshot(future["id"], kickoff_at="2030-05-18T10:00:00+08:00")}},
        {"settlements": {}},
        now=AFTER_KICKOFF,
    )
    assert report["pending_total"] == 2
    assert report["nba_pending"] == 1
    assert report["non_nba_pending"] == 1
    assert report["not_yet_due"] == 1
    assert report["status_counts"][NO_SOURCE_OBSERVATION] == 2

    by_id = {target["match_id"]: target for target in report["targets"]}
    nba = by_id[NBA_ID]
    assert nba["pending_reason"] == "KICKOFF_PASSED_WITHOUT_FINAL_RESULT"
    assert nba["provider_binding"]["state"] == BDL_BINDING_ABSENT
    assert nba["result_path"] == NBA_RESULT_PATH_PRIOR_BINDING
    assert nba["evidence_request"]["network_requests_performed"] == 0
    assert nba["evidence_request"]["evidence_authorization"] == "NOT_GRANTED"

    de = by_id[german["id"]]
    assert de["is_nba"] is False
    assert de["provider_binding"] is None
    assert de["result_path"]["state"] == NON_NBA_RESULT_PATH
    assert de["result_path"]["nba_path_applicable"] is False

    summary = summarize_result_delivery(report, sample_limit=1)
    assert summary["truncated"] is True
    assert len(summary["sample_targets"]) == 1


def test_rd_10_settled_snapshots_are_not_pending():
    snap = snapshot(NBA_ID)
    report = build_basketball_result_delivery(
        [nba_target()],
        {"snapshots": {snap["snapshot_id"]: snap}},
        {"settlements": {"s1": {"settlement_id": "s1", "snapshot_id": snap["snapshot_id"],
                                "match_id": NBA_ID}}},
        now=AFTER_KICKOFF,
    )
    assert report["pending_total"] == 0


def test_rd_11_established_provider_binding_requests_external_evidence_only():
    prov = {
        "actual_kickoff_time_source": "balldontlie_nba_games",
        "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
        "kickoff_source_event_id": "bdl:12",
        "kickoff_source_scheduled_start_utc": "2030-05-16T02:00:00+00:00",
    }
    snap = snapshot(NBA_ID)
    report = build_basketball_result_delivery(
        [nba_target(kickoff_provenance=prov)],
        {"snapshots": {snap["snapshot_id"]: snap}},
        {"settlements": {}},
        now=AFTER_KICKOFF,
    )
    target = report["targets"][0]
    assert target["provider_binding"]["state"] == BDL_BINDING_ESTABLISHED
    assert target["provider_binding"]["provider_game_id"] == "bdl:12"
    assert target["provider_binding"]["missing_fields"] == []
    request = target["evidence_request"]
    assert request["state"] == NBA_RESULT_PATH_AWAITING_EVIDENCE
    assert "CONFIRMED_FINAL_RESULT_PAYLOAD_NOT_RETRIEVED" in request["missing_fields"]
    assert "EXTERNAL_EVIDENCE_NOT_AUTHORIZED" in request["missing_fields"]

    incomplete = dict(prov)
    incomplete.pop("kickoff_source_event_id")
    report2 = build_basketball_result_delivery(
        [nba_target(kickoff_provenance=incomplete)],
        {"snapshots": {snap["snapshot_id"]: snap}},
        {"settlements": {}},
        now=AFTER_KICKOFF,
    )
    target2 = report2["targets"][0]
    assert target2["provider_binding"]["state"] == BDL_BINDING_INCOMPLETE
    assert "kickoff_provenance.kickoff_source_event_id" in target2["provider_binding"]["missing_fields"]
    assert target2["result_path"] == NBA_RESULT_PATH_PRIOR_BINDING


# ---------------------------------------------------------------------------
# Workstream A 主验收：两个真实 refresh 周期复现「即时列表过期」缺陷
# ---------------------------------------------------------------------------

LIVE_NBA_HTML = """
<script>
var matchList=[
 ["1","249001","x","2030-05-16","10:00","","NBA","","","","","","","洛杉矶湖人","","","","","金州勇士","","","","","","","","7312"]
];
var oddsList={"249001":[["1","官方","1.85","1.95"]]};
</script>
"""

LIVE_EMPTY_HTML = """
<script>
var matchList=[];
var oddsList={};
</script>
"""

EMPTY_XML = '<?xml version="1.0" encoding="utf-8"?><xml></xml>'


def _patch_sources(monkeypatch, *, live_html: str):
    fetcher_500._CACHE.clear()
    reset_source_observation()

    def fake_get(url, **kwargs):
        if "wanchang" in str(url):
            return _HtmlResp(WANCHANG_HTML)
        return _HtmlResp(live_html)

    monkeypatch.setattr(fetcher_500.requests, "get", fake_get)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    # 竞彩 XML 对两种运动都返回空集：本场景的盘口只来自即时比分行。
    monkeypatch.setattr(fetcher_500, "_fetch_xml", lambda lot, play: EMPTY_XML)


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def test_two_refreshes_reproduce_expired_live_source_gap(isolated_data_dir, monkeypatch):
    directory = isolated_data_dir
    write_stores(directory, [])

    # ---- Refresh 1：赛前，赛事仍在即时列表中 ----
    _patch_sources(monkeypatch, live_html=LIVE_NBA_HTML)
    with override_beijing_now(BEFORE_KICKOFF):
        first = scraper.refresh(verbose=False)

    assert first["sources"]["live_basketball"] == 1
    assert first["added"] == 1
    stored = {m["id"]: m for m in daily_loader.get_all_matches()}
    assert len(stored) == 1
    canonical_id, record = next(iter(stored.items()))
    assert record["sport"] == "basketball" and record["status"] == "upcoming"
    assert record["home"] == "洛杉矶湖人" and record["away"] == "金州勇士"
    assert record["jczq_no"] == NBA_NO

    snapshots_t1 = prediction_snapshots.get_snapshots_for_match(canonical_id)
    # BASKETBALL_PREGAME_FIXTURE_CAPTURED
    assert len(snapshots_t1) == 1
    pregame = snapshots_t1[0]
    assert datetime.fromisoformat(pregame["kickoff_at"]) == KICKOFF
    assert datetime.fromisoformat(pregame["generated_at"]) < KICKOFF

    # ---- Refresh 2：赛后，即时源不再列出该赛事；完场源只给足球 ----
    _patch_sources(monkeypatch, live_html=LIVE_EMPTY_HTML)
    with override_beijing_now(AFTER_KICKOFF):
        second = scraper.refresh(verbose=False)

    assert second["sources"]["live_basketball"] == 0            # 即时列表已过期
    assert second["sources"]["wanchang"] >= 1
    assert second["prediction_snapshots_added"] == 0
    assert second["settlements_added"] == 0
    assert second["evaluation_rows_added"] == 0

    observed = last_source_observation()
    assert observed["live_basketball"]["status"] == "empty"
    assert observed["live_basketball"]["rows"] == []
    assert {row["sport"] for row in observed[GENERIC_FINISHED_SOURCE]["rows"]} == {"football"}

    # ORIGINAL_CANONICAL_FIXTURE_REMAINS_UPCOMING
    after = {m["id"]: m for m in daily_loader.get_all_matches()}
    assert after[canonical_id]["status"] == "upcoming"
    assert after[canonical_id]["score"] is None
    assert after[canonical_id]["time"] == "10:00"
    assert after[canonical_id]["odds"] == {"home_win": 1.85, "away_win": 1.95}

    # NO_RETROACTIVE_PREDICTION
    assert prediction_snapshots.get_snapshots_for_match(canonical_id) == snapshots_t1

    # NO_UNVERIFIED_SETTLEMENT
    assert settlements_store.get_settlements_for_match(canonical_id) == []
    assert evaluation_store.get_evaluation_rows_for_match(canonical_id) == []

    # NO_BASKETBALL_FINAL_RESULT_DELIVERED + GENERIC_FINISHED_SOURCE_HAS_FOOTBALL_ONLY
    report = build_basketball_result_delivery(
        list(after.values()),
        _read(os.path.join(str(directory), "prediction_snapshots.json")),
        _read(os.path.join(str(directory), "settlements.json")),
        now=AFTER_KICKOFF,
    )
    assert report["pending_total"] == 1
    target = report["targets"][0]
    assert target["match_id"] == canonical_id
    assert target["result_delivery"]["status"] == SOURCE_RETURNED_EMPTY
    assert report["status_counts"][SOURCE_RETURNED_CONFIRMED_FINAL_RESULT] == 0
    assert report["observations"]["generic_finished_source_scope"] == FOOTBALL_ONLY
    assert report["observations"]["basketball_finished_source_available"] is False
    assert target["pending_reason"] == "KICKOFF_PASSED_WITHOUT_FINAL_RESULT"
    assert target["provider_binding"]["state"] == BDL_BINDING_ABSENT
    assert target["result_path"] == NBA_RESULT_PATH_PRIOR_BINDING


def test_expired_fixture_is_reported_as_fixture_not_present_when_rows_remain(
    isolated_data_dir, monkeypatch
):
    """来源仍有行、但目标赛事不在其中：必须是 FIXTURE_NOT_PRESENT，不是解析失败。"""
    directory = isolated_data_dir
    write_stores(directory, [])
    _patch_sources(monkeypatch, live_html=LIVE_NBA_HTML)
    with override_beijing_now(BEFORE_KICKOFF):
        scraper.refresh(verbose=False)
    canonical_id = next(iter({m["id"] for m in daily_loader.get_all_matches()}))
    stored = daily_loader.get_all_matches()

    other = {
        "id": f"500lq-{NBA_DATE}-周日399",
        "sport": "basketball",
        "league": "NBA",
        "date": NBA_DATE,
        "time": "12:00",
        "status": "upcoming",
        "home": "波士顿凯尔特人",
        "away": "迈阿密热火",
        "jczq_no": "周日399",
    }
    report = build_basketball_result_delivery(
        stored,
        _read(os.path.join(str(directory), "prediction_snapshots.json")),
        _read(os.path.join(str(directory), "settlements.json")),
        observations(rows=[other]),
        now=AFTER_KICKOFF,
    )
    assert report["targets"][0]["match_id"] == canonical_id
    assert report["targets"][0]["result_delivery"]["status"] == SOURCE_FIXTURE_NOT_PRESENT
