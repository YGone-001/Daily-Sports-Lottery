"""
完赛赛事终态保护与数据完整性测试套件
====================================
覆盖：
- 终场比分合法性判断 (valid_full_time_score)
- 完赛终态识别 (is_finalized_match)
- 正常完赛与直接完赛流转
- 不完整完赛记录的补全修复
- 幂等完赛重报 (updated = 0)
- 终态对 stale live/upcoming/placeholder 的保护
- 完赛比分冲突检测 (MatchLifecycleConflict) 与保持原记录
- 辅助比分字段差异不触发冲突
- 未终态赛事的可变状态修正（非全局单调状态）
- updated 计数器的准确契约（元数据更新、多字段单计、跨赛事累计、无变动0）
- 端到端生命周期连续性、重复刷新、stale 刷新、冲突刷新
- 下游 Elo 校准、结算、评估样本单线完整性
- 篮球终态保护
- 历史记录不可变性与模型版本稳定性
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

import config
from utils import daily_loader, odds_snapshots, prediction_snapshots, scraper, settlements
from utils.daily_loader import load_json, save_json
from utils.evaluation_rows import get_all_evaluation_rows
from utils.match_lifecycle import (
    MatchLifecycleConflict,
    is_finalized_match,
    resolve_match_update,
    valid_full_time_score,
)
from utils.odds_snapshots import get_odds_history_for_match
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


# ---------------------------------------------------------------------------
# 1. 终场比分合法性判断
# ---------------------------------------------------------------------------

def test_valid_full_time_score_accepted():
    assert valid_full_time_score({"ft": [0, 0]}) is True
    assert valid_full_time_score({"ft": [2, 1]}) is True
    assert valid_full_time_score({"ft": [105, 99]}) is True
    assert valid_full_time_score({"ft": (3, 0)}) is True
    # 允许附加辅助字段
    assert valid_full_time_score({"ft": [2, 1], "ht": [1, 0]}) is True


def test_valid_full_time_score_rejected():
    assert valid_full_time_score(None) is False
    assert valid_full_time_score({}) is False
    assert valid_full_time_score({"ft": None}) is False
    assert valid_full_time_score({"ft": []}) is False
    assert valid_full_time_score({"ft": [2]}) is False
    assert valid_full_time_score({"ft": ["2", 1]}) is False
    assert valid_full_time_score({"ft": [2.0, 1]}) is False
    assert valid_full_time_score({"ft": [True, 1]}) is False
    assert valid_full_time_score({"ft": [1, False]}) is False
    assert valid_full_time_score({"ft": [-1, 0]}) is False
    assert valid_full_time_score({"ft": [2, -3]}) is False
    assert valid_full_time_score("2-1") is False


# ---------------------------------------------------------------------------
# 2. 完赛终态识别
# ---------------------------------------------------------------------------

def test_is_finalized_match():
    # finished + valid score -> True
    assert is_finalized_match({"status": "finished", "score": {"ft": [2, 1]}}) is True
    # finished + missing/invalid score -> False
    assert is_finalized_match({"status": "finished", "score": None}) is False
    assert is_finalized_match({"status": "finished", "score": {}}) is False
    assert is_finalized_match({"status": "finished", "score": {"ft": ["2", 1]}}) is False
    # live / upcoming + valid score -> False
    assert is_finalized_match({"status": "live", "score": {"ft": [1, 0]}}) is False
    assert is_finalized_match({"status": "upcoming", "score": {"ft": [0, 0]}}) is False
    assert is_finalized_match(None) is False


# ---------------------------------------------------------------------------
# 3. 正常流转、直接流转与不完整记录修复
# ---------------------------------------------------------------------------

def test_normal_finalization_live_to_finished():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "live",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [1, 0]},
    }
    incoming = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [2, 1]}
    assert is_finalized_match(merged[0]) is True


def test_direct_finalization_upcoming_to_finished():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
    }
    incoming = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [2, 1]}
    assert is_finalized_match(merged[0]) is True


def test_incomplete_finished_repair():
    # 之前标记完赛但缺失比分或比分无效，允许后续合法数据补全
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
    }
    incoming = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [2, 1]}
    assert is_finalized_match(merged[0]) is True


# ---------------------------------------------------------------------------
# 4. 幂等完赛重报与终态保护
# ---------------------------------------------------------------------------

def test_identical_final_replay_is_noop():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "market_tracked": True,
    }
    incoming = {
        "id": "m1-new",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["score"] == {"ft": [2, 1]}


def test_finished_to_live_protection():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "market_tracked": True,
    }
    stale_live = {
        "id": "m1-live",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "live",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [1, 0]},
    }
    merged, added, updated = scraper._merge([existing], [stale_live])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [2, 1]}


def test_finished_to_upcoming_protection():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "market_tracked": True,
    }
    stale_upcoming = {
        "id": "m1-up",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
    }
    merged, added, updated = scraper._merge([existing], [stale_upcoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [2, 1]}


def test_finished_to_market_placeholder_protection():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "market_tracked": True,
    }
    placeholder_incoming = {
        "id": "500j-2030-05-01-m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "odds": {"home_win": 2.5, "draw": 3.1, "away_win": 3.0},
    }
    merged, added, updated = scraper._merge([existing], [placeholder_incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    m = merged[0]
    assert m["time"] == "20:00"
    assert m["kickoff_time_known"] is True
    assert m["status"] == "finished"
    assert m["score"] == {"ft": [2, 1]}
    assert m["odds"] == {"home_win": 1.9, "draw": 3.4, "away_win": 3.8}


# ---------------------------------------------------------------------------
# 5. 完赛比分冲突与辅助字段
# ---------------------------------------------------------------------------

def test_conflicting_final_result():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "market_tracked": True,
    }
    conflicting_incoming = {
        "id": "m1-conf",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 2]},
    }
    # 纯函数层应抛出 MatchLifecycleConflict
    with pytest.raises(MatchLifecycleConflict) as exc_info:
        resolve_match_update(dict(existing), conflicting_incoming)
    assert exc_info.value.reason == "conflicting_final_score"
    assert exc_info.value.existing_score == [2, 1]
    assert exc_info.value.incoming_score == [2, 2]

    # scraper._merge 层应捕获异常并保留原记录，不崩溃，updated=0
    merged, added, updated = scraper._merge([existing], [conflicting_incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["score"] == {"ft": [2, 1]}


def test_same_ft_different_auxiliary_metadata():
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "market_tracked": True,
    }
    aux_incoming = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1], "ht": [1, 0]},
    }
    # 不产生冲突，保持原有 canonical 记录不变，updated=0
    merged, added, updated = scraper._merge([existing], [aux_incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["score"] == {"ft": [2, 1]}


# ---------------------------------------------------------------------------
# 6. 未终态赛事的可变状态修正（非全局单调状态）
# ---------------------------------------------------------------------------

def test_pre_final_status_correction_not_globally_monotonic():
    # live 比赛由于延期被源修正为 upcoming，在未完赛前允许正常修正
    existing = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "live",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
        "market_tracked": True,
    }
    delayed_incoming = {
        "id": "m1",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
    }
    merged, added, updated = scraper._merge([existing], [delayed_incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    assert merged[0]["status"] == "upcoming"


# ---------------------------------------------------------------------------
# 7. updated 计数器的准确契约
# ---------------------------------------------------------------------------

def test_metadata_only_updated_count():
    existing = {
        "id": "m1",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "home_rank": None,
        "away_rank": None,
        "market_tracked": True,
    }
    incoming = {
        "id": "m1",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "home_rank": 1,
        "away_rank": 2,
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    assert merged[0]["home_rank"] == 1


def test_multiple_changes_on_one_match_counts_as_one():
    existing = {
        "id": "m1",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
        "odds": None,
        "market_tracked": True,
    }
    incoming = {
        "id": "m1",
        "sport": "football",
        "league": "西班牙甲级联赛",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "live",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [1, 0]},
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "home_rank": 1,
    }
    merged, added, updated = scraper._merge([existing], [incoming])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1


def test_two_existing_matches_changed_counts_as_two():
    m1 = {
        "id": "m1", "sport": "football", "date": "2030-05-01", "time": "20:00",
        "kickoff_time_known": True, "status": "upcoming", "home": "A1", "away": "B1", "market_tracked": True,
    }
    m2 = {
        "id": "m2", "sport": "football", "date": "2030-05-01", "time": "20:00",
        "kickoff_time_known": True, "status": "upcoming", "home": "A2", "away": "B2", "market_tracked": True,
    }
    inc1 = dict(m1, status="live")
    inc2 = dict(m2, status="live")
    merged, added, updated = scraper._merge([m1, m2], [inc1, inc2])
    assert len(merged) == 2
    assert added == 0
    assert updated == 2


def test_noop_updated_count():
    m = {
        "id": "m1", "sport": "football", "date": "2030-05-01", "time": "20:00",
        "kickoff_time_known": True, "status": "upcoming", "home": "A", "away": "B", "market_tracked": True,
    }
    merged, added, updated = scraper._merge([m], [copy.deepcopy(m)])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0


# ---------------------------------------------------------------------------
# 8. 端到端生命周期连续性、stale 刷新、冲突刷新与下游完整性
# ---------------------------------------------------------------------------

def test_end_to_end_finalized_integrity_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    canon_id = "500j-2030-05-01-007"

    # Step 1: Market fallback 入库
    market_row = {
        "id": canon_id,
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "jczq_no": "007",
    }
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda sport: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_jczq_xml",
        lambda sport: [dict(market_row)] if sport == "football" else [],
    )
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])
    res1 = scraper.refresh(verbose=False)
    assert res1["added"] == 1
    assert res1["prediction_snapshots_added"] == 0

    # Step 2: Live 赛程到来，提供权威时间 20:00
    live_row = {
        "id": "500l-2030-05-01-007",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "score": None,
        "odds": None,
        "jczq_no": "007",
    }
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_live_matches",
        lambda sport: [dict(live_row)] if sport == "football" else [],
    )
    res2 = scraper.refresh(verbose=False)
    assert res2["prediction_snapshots_added"] == 1

    # Step 3: 完赛 finished 2-1
    finished_row = {
        "id": "500w-2030-05-01-007",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 1]},
        "odds": None,
        "jczq_no": "007",
    }
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda sport: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [dict(finished_row)])

    res3 = scraper.refresh(verbose=False)
    assert res3["calibrated"] == 1
    assert res3["settlements_added"] == 1
    assert res3["evaluation_rows_added"] == 1

    matches = daily_loader.get_all_matches()
    assert len(matches) == 1
    assert matches[0]["id"] == canon_id
    assert matches[0]["status"] == "finished"
    assert matches[0]["score"] == {"ft": [2, 1]}

    # Step 4: 重复完赛刷新（Identical Repeat）
    res4 = scraper.refresh(verbose=False)
    assert res4["calibrated"] == 0
    assert res4["settlements_added"] == 0
    assert res4["evaluation_rows_added"] == 0
    assert res4["updated"] == 0

    # Step 5: 出现陈旧 live 1-0 数据（Stale Live Refresh）
    stale_live_row = {
        "id": "500l-2030-05-01-007",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "live",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [1, 0]},
        "odds": None,
        "jczq_no": "007",
    }
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_live_matches",
        lambda sport: [dict(stale_live_row)] if sport == "football" else [],
    )
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

    res5 = scraper.refresh(verbose=False)
    assert res5["updated"] == 0
    assert res5["calibrated"] == 0
    assert res5["settlements_added"] == 0
    assert res5["evaluation_rows_added"] == 0

    matches_after_stale = daily_loader.get_all_matches()
    assert matches_after_stale[0]["status"] == "finished"
    assert matches_after_stale[0]["score"] == {"ft": [2, 1]}

    # Step 6: 出现冲突完赛比分 finished 2-2（Conflicting Final Refresh）
    conflicting_row = {
        "id": "500w-2030-05-01-007",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "皇马",
        "away": "巴萨",
        "score": {"ft": [2, 2]},
        "odds": None,
        "jczq_no": "007",
    }
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda sport: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [dict(conflicting_row)])

    res6 = scraper.refresh(verbose=False)
    # 冲突被记录，原记录保持，不新增校准与结算
    assert res6["updated"] == 0
    assert res6["calibrated"] == 0
    assert res6["settlements_added"] == 0
    assert res6["evaluation_rows_added"] == 0

    matches_after_conf = daily_loader.get_all_matches()
    assert matches_after_conf[0]["score"] == {"ft": [2, 1]}
    assert len(get_settlements_for_match(canon_id)) == 1
    assert len(get_all_evaluation_rows()) == 1


# ---------------------------------------------------------------------------
# 9. 篮球终态保护
# ---------------------------------------------------------------------------

def test_basketball_final_integrity():
    existing = {
        "id": "bb-1",
        "sport": "basketball",
        "date": "2030-05-01",
        "time": "19:30",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "湖人",
        "away": "勇士",
        "score": {"ft": [108, 102]},
        "market_tracked": True,
    }
    stale_live = {
        "id": "bb-1",
        "sport": "basketball",
        "date": "2030-05-01",
        "time": "19:30",
        "kickoff_time_known": True,
        "status": "live",
        "home": "湖人",
        "away": "勇士",
        "score": {"ft": [76, 70]},
    }
    merged, added, updated = scraper._merge([existing], [stale_live])
    assert len(merged) == 1
    assert added == 0
    assert updated == 0
    assert merged[0]["status"] == "finished"
    assert merged[0]["score"] == {"ft": [108, 102]}


# ---------------------------------------------------------------------------
# 10. 历史不可变性、市场准入与模型版本稳定性
# ---------------------------------------------------------------------------

def test_historical_immutability(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    canon_id = "m-hist"
    match_rec = {
        "id": canon_id,
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "market_tracked": True,
    }
    t = datetime(2030, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
    snap, _ = capture_snapshot(match_rec, now=t)
    odds_snap = odds_snapshots.record_odds_snapshot(match_rec, now=t)

    fin = dict(match_rec, status="finished", score={"ft": [2, 1]})
    st, _ = settlements.settle_snapshot(snap, fin, now=t)
    ev, _ = daily_loader.load_json("evaluation_rows.json"), None
    from utils.evaluation_rows import capture_evaluation_row
    ev_row, _ = capture_evaluation_row(snap, st, now=t)

    # 深度拷贝全部历史数据
    before_snaps = copy.deepcopy(daily_loader.load_json("prediction_snapshots.json"))
    before_odds = copy.deepcopy(daily_loader.load_json("odds_snapshots.json"))
    before_settle = copy.deepcopy(daily_loader.load_json("settlements.json"))
    before_eval = copy.deepcopy(daily_loader.load_json("evaluation_rows.json"))

    # 提供陈旧 live 与冲突 finished 刷新
    stale_live = dict(fin, status="live", score={"ft": [1, 0]})
    scraper._merge([fin], [stale_live])

    conf_fin = dict(fin, status="finished", score={"ft": [2, 2]})
    scraper._merge([fin], [conf_fin])

    # 验证历史 JSON 完全相等
    assert daily_loader.load_json("prediction_snapshots.json") == before_snaps
    assert daily_loader.load_json("odds_snapshots.json") == before_odds
    assert daily_loader.load_json("settlements.json") == before_settle
    assert daily_loader.load_json("evaluation_rows.json") == before_eval


def test_market_admission_regression_unpriced_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    unpriced = {
        "id": "new-unpriced",
        "sport": "football",
        "date": "2030-05-01",
        "time": "18:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "队伍A",
        "away": "队伍B",
        "odds": None,
    }
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_live_matches",
        lambda s: [dict(unpriced)],
    )
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda s: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

    res = scraper.refresh(verbose=False)
    assert res["total"] == 0
    assert res["sources"]["market_rejected"] == 1


def test_model_versions_remain_unchanged():
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"
    assert config.MODEL_VERSIONS["basketball"] == "basketball-coldstart-1"
    assert config.MODEL_VERSION == "baseline-1"
