"""
比赛跨源对账与身份连续性测试套件
================================
覆盖：
- 赛事编号归一化与强编号匹配
- 主客队对 fallback 匹配
- 强编号冲突与跨日期/跨运动隔离
- 开赛时间从事件身份中排除
- 占位符开赛时间语义 (kickoff_time_known=False) 与权威开赛时间升级
- 市场源中断时的生命周期连续性
- 赛前预测快照权威开赛时间门禁
- 完赛结算与评估样本单线演进
- 历史不可变存储完整性
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import config
from utils import daily_loader, odds_snapshots, prediction_snapshots, scraper, settlements
from utils.daily_loader import add_time_status, get_match_datetime
from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
from utils.match_identity import (
    competition_event_key,
    competition_number_for_match,
    is_kickoff_time_known,
    normalize_competition_number,
    same_event,
    team_event_key,
)
from utils.odds_snapshots import record_odds_snapshot
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match, settle_snapshot

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def test_competition_number_normalization():
    assert normalize_competition_number("周三007") == "7"
    assert normalize_competition_number("007") == "7"
    assert normalize_competition_number(7) == "7"
    assert normalize_competition_number("7") == "7"
    assert normalize_competition_number(0) == "0"
    assert normalize_competition_number("0") == "0"
    assert normalize_competition_number("000") == "0"
    assert normalize_competition_number("") is None
    assert normalize_competition_number(None) is None
    assert normalize_competition_number("第3轮") == "3"
    assert normalize_competition_number("无数字") is None


def test_competition_identity_excludes_kickoff_time():
    m1 = {"sport": "football", "date": "2030-05-01", "time": "00:00", "jczq_no": "007"}
    m2 = {"sport": "football", "date": "2030-05-01", "time": "20:00", "jczq_no": "007"}
    key1 = competition_event_key(m1)
    key2 = competition_event_key(m2)
    assert key1 == key2
    assert "00:00" not in key1
    assert "20:00" not in key1
    assert key1 == "football|2030-05-01|jczq|7"


def test_exact_team_identity_excludes_kickoff_time():
    m1 = {"sport": "football", "date": "2030-05-01", "time": "00:00", "home": "皇马", "away": "巴萨"}
    m2 = {"sport": "football", "date": "2030-05-01", "time": "20:00", "home": "皇马", "away": "巴萨"}
    key1 = team_event_key(m1)
    key2 = team_event_key(m2)
    assert key1 == key2
    assert "00:00" not in key1
    assert "20:00" not in key1
    assert key1 == "football|2030-05-01|teams|皇马|巴萨"


def test_strong_number_match():
    m1 = {"sport": "football", "date": "2030-05-01", "jczq_no": "007", "home": "队伍A", "away": "队伍B"}
    m2 = {"sport": "football", "date": "2030-05-01", "jczq_no": "7", "home": "队伍A", "away": "队伍B"}
    assert same_event(m1, m2) is True


def test_team_pair_fallback_match():
    m1 = {"sport": "football", "date": "2030-05-01", "time": "00:00", "home": "皇马", "away": "巴萨"}
    m2 = {"sport": "football", "date": "2030-05-01", "time": "20:00", "home": "皇马", "away": "巴萨"}
    assert same_event(m1, m2) is True


def test_conflicting_strong_numbers_do_not_match():
    # 即使队伍完全一致，编号冲突也绝不匹配，且绝不能退化为队伍匹配
    m1 = {"sport": "football", "date": "2030-05-01", "jczq_no": "007", "home": "皇马", "away": "巴萨"}
    m2 = {"sport": "football", "date": "2030-05-01", "jczq_no": "008", "home": "皇马", "away": "巴萨"}
    assert same_event(m1, m2) is False


def test_same_number_different_date_does_not_match():
    m1 = {"sport": "football", "date": "2030-05-01", "jczq_no": "007", "home": "皇马", "away": "巴萨"}
    m2 = {"sport": "football", "date": "2030-05-02", "jczq_no": "007", "home": "皇马", "away": "巴萨"}
    assert same_event(m1, m2) is False


def test_same_number_different_sport_does_not_match():
    m1 = {"sport": "football", "date": "2030-05-01", "jczq_no": "007", "home": "队A", "away": "队B"}
    m2 = {"sport": "basketball", "date": "2030-05-01", "jczq_no": "007", "home": "队A", "away": "队B"}
    assert same_event(m1, m2) is False


def test_same_date_home_different_away_does_not_match():
    m1 = {"sport": "football", "date": "2030-05-01", "home": "皇马", "away": "巴萨"}
    m2 = {"sport": "football", "date": "2030-05-01", "home": "皇马", "away": "马竞"}
    assert same_event(m1, m2) is False


def test_market_live_team_spelling_difference_reconciles_when_strong_number_matches():
    # 强编号相同时，即使队名书写不同也对账成功
    m1 = {"sport": "football", "date": "2030-05-01", "jczq_no": "007", "home": "皇家马德里", "away": "巴塞罗那"}
    m2 = {"sport": "football", "date": "2030-05-01", "jczq_no": "7", "home": "皇马", "away": "巴萨"}
    assert same_event(m1, m2) is True


def test_fetcher_rows_kickoff_known_markers():
    from utils.fetcher_500 import _parse_live, _parse_lq, _parse_wanchang, fetch_jczq_xml

    # 1. 验证 live football 解析带 kickoff_time_known = True
    html_live = (
        '<table><tr id="a1001" order="001" status="0">'
        "<td>周三001</td><td>西甲</td><td>第1轮</td><td>05-01 20:00</td><td>未</td>"
        '<td><span class="mainName">主队</span></td><td>-</td>'
        '<td><span class="mainName">客队</span></td><td>析</td>'
        "</tr></table>"
    )
    parsed_live = _parse_live(html_live, "a", "football")
    assert len(parsed_live) == 1
    assert parsed_live[0]["kickoff_time_known"] is True
    assert parsed_live[0]["time"] == "20:00"

    # 2. 验证 live basketball 解析带 kickoff_time_known = True
    html_lq = (
        "<script>"
        'var matchList = [["1", "2001", "", "2030-05-01", "19:30", "", "NBA", "", "", "常规赛", "", "", "", "湖人", "", "", "", "", "勇士", "", "", "", "-", "-", "", "", "301"]];'
        "var oddsList = {};"
        "</script>"
    )
    parsed_lq = _parse_lq(html_lq)
    assert len(parsed_lq) == 1
    assert parsed_lq[0]["kickoff_time_known"] is True
    assert parsed_lq[0]["time"] == "19:30"

    # 3. 验证完场解析带 kickoff_time_known = True
    html_wanchang = (
        '<table><tr id="w1">'
        "<td>中超</td><td>第1轮</td><td>05-01 15:30</td><td>完</td>"
        '<td><span class="mainName">主队</span></td>'
        "<td>2 - 1</td>"
        '<td><span class="mainName">客队</span></td>'
        "<td>1 - 0</td><td>析</td>"
        "</tr></table>"
    )
    parsed_wc = _parse_wanchang(html_wanchang)
    assert len(parsed_wc) == 1
    assert parsed_wc[0]["kickoff_time_known"] is True
    assert parsed_wc[0]["time"] == "15:30"

    # 4. 验证 fetch_jczq_xml 产出的行包含 kickoff_time_known = False 与 jczq_no
    from utils import fetcher_500
    xml_data = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<xml><m id="999" matchnum="007" date="2030-05-01" league="西甲" home="皇马" away="巴萨">'
        '<row win="1.90" lost="3.80" draw="3.40"/>'
        '</m></xml>'
    )
    fetcher_500._CACHE.clear()
    monkeypatch = None
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(fetcher_500, "_fetch_xml", lambda lot, play: xml_data)
    try:
        xml_matches = fetcher_500.fetch_jczq_xml("football")
        assert len(xml_matches) == 1
        assert xml_matches[0]["kickoff_time_known"] is False
        assert xml_matches[0]["jczq_no"] == "007"
        assert xml_matches[0]["time"] == "00:00"
    finally:
        mp.undo()
        fetcher_500._CACHE.clear()


def test_legacy_500j_0000_fallback_treated_as_unknown():
    legacy_m = {
        "id": "500j-2030-05-01-007",
        "sport": "football",
        "date": "2030-05-01",
        "time": "00:00",
        "round": "007",
        "home": "皇马",
        "away": "巴萨",
    }
    assert is_kickoff_time_known(legacy_m) is False
    assert get_match_datetime(legacy_m) is None


def test_genuine_live_midnight_treated_as_known():
    genuine_midnight = {
        "id": "500l-2030-05-01-007",
        "sport": "football",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": True,
        "home": "皇马",
        "away": "巴萨",
    }
    assert is_kickoff_time_known(genuine_midnight) is True
    dt = get_match_datetime(genuine_midnight)
    assert dt is not None
    assert dt.hour == 0 and dt.minute == 0
    assert dt.date() == datetime(2030, 5, 1).date()


def test_placeholder_semantics_status_and_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    m = {
        "id": "500j-2030-05-01-007",
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

    # 1. 在当天下午 15:00 判定状态，依然应为 upcoming
    eval_time = datetime(2030, 5, 1, 15, 0, tzinfo=BEIJING_TZ)
    with_status = add_time_status(m, now=eval_time)
    assert with_status["status"] == "upcoming"

    # 2. 开赛时间未知，禁止新建赛前预测快照
    snap, created = capture_snapshot(m, now=eval_time)
    assert snap is None
    assert created is False

    # 3. 赔率快照允许记录，但 kickoff_at 必须为 None
    odds_snap = record_odds_snapshot(m, now=eval_time)
    assert odds_snap is not None
    assert odds_snap["match_id"] == "500j-2030-05-01-007"
    assert odds_snap["kickoff_at"] is None


def test_market_fallback_to_live_with_market_available(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    # 首次刷新：仅有 market XML fallback
    market_row = {
        "id": "500j-2030-05-01-007",
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
    assert res1["total"] == 1
    assert res1["prediction_snapshots_added"] == 0
    assert res1["odds_snapshots_added"] == 1

    stored_matches = daily_loader.get_all_matches()
    assert len(stored_matches) == 1
    canon_id = stored_matches[0]["id"]
    assert canon_id == "500j-2030-05-01-007"
    assert stored_matches[0]["time"] == "00:00"
    assert stored_matches[0]["kickoff_time_known"] is False

    # 第二次刷新：live 赛程行出现（20:00, kickoff_time_known=True），market 也可用
    live_row = {
        "id": "500l-2030-05-01-9999",
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇家马德里",
        "away": "巴塞罗那",
        "score": None,
        "odds": None,
        "jczq_no": "007",
    }

    fresh_market = dict(market_row)
    fresh_market["odds"] = {"home_win": 1.95, "draw": 3.45, "away_win": 3.75}

    monkeypatch.setattr(
        "utils.fetcher_500.fetch_live_matches",
        lambda sport: [dict(live_row)] if sport == "football" else [],
    )
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_jczq_xml",
        lambda sport: [dict(fresh_market)] if sport == "football" else [],
    )

    res2 = scraper.refresh(verbose=False)
    assert res2["total"] == 1
    assert res2["added"] == 0
    assert res2["prediction_snapshots_added"] == 1

    updated_matches = daily_loader.get_all_matches()
    assert len(updated_matches) == 1
    # canonical id 保持不变！
    assert updated_matches[0]["id"] == canon_id
    assert updated_matches[0]["time"] == "20:00"
    assert updated_matches[0]["kickoff_time_known"] is True
    assert updated_matches[0]["market_tracked"] is True

    # 验证新生成的快照属于该原 canonical id
    snaps = get_snapshots_for_match(canon_id)
    assert len(snaps) == 1
    assert snaps[0]["match_id"] == canon_id
    assert "20:00" in snaps[0]["kickoff_at"]


def test_market_fallback_to_live_during_market_outage(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    # 首次刷新：仅有 market fallback
    market_row = {
        "id": "500j-2030-05-01-007",
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

    scraper.refresh(verbose=False)

    # 第二次刷新：live 出现，market 故障（空数据）
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
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda sport: [])

    res2 = scraper.refresh(verbose=False)
    assert res2["total"] == 1
    assert res2["added"] == 0

    matches = daily_loader.get_all_matches()
    assert len(matches) == 1
    m = matches[0]
    assert m["id"] == "500j-2030-05-01-007"
    assert m["time"] == "20:00"
    assert m["kickoff_time_known"] is True
    # 市场中断时，保留上一轮已知的有效盘口
    assert m["odds"] == {"home_win": 1.9, "draw": 3.4, "away_win": 3.8}


def test_known_kickoff_never_downgraded_by_later_market_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    # 既有比赛已是已知 20:00
    existing = [
        {
            "id": "500j-2030-05-01-007",
            "sport": "football",
            "league": "西甲",
            "date": "2030-05-01",
            "time": "20:00",
            "kickoff_time_known": True,
            "status": "live",
            "home": "皇马",
            "away": "巴萨",
            "score": {"ft": [1, 0]},
            "odds": {"home_win": 2.1, "draw": 3.2, "away_win": 3.5},
            "jczq_no": "007",
            "market_tracked": True,
        }
    ]
    daily_loader.save_json("daily_matches.json", {"matches": existing})

    # 后来收到市场源只有 00:00 占位符
    incoming_placeholder = [
        {
            "id": "500j-2030-05-01-007",
            "sport": "football",
            "league": "竞彩",
            "date": "2030-05-01",
            "time": "00:00",
            "kickoff_time_known": False,
            "status": "upcoming",
            "home": "皇马",
            "away": "巴萨",
            "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6},
            "jczq_no": "007",
        }
    ]

    merged, added, updated = scraper._merge(existing, incoming_placeholder)
    assert len(merged) == 1
    m = merged[0]
    # 开赛时间绝不降级为 00:00
    assert m["time"] == "20:00"
    assert m["kickoff_time_known"] is True
    # 状态与比分不被抹除
    assert m["status"] == "live"
    assert m["score"] == {"ft": [1, 0]}


def test_finished_lifecycle_continuity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    # 1. 市场 fallback
    market_row = {
        "id": "500j-2030-05-01-007",
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
    scraper.refresh(verbose=False)

    # 2. live 赛程出现并提供实际开赛时间 (20:00)
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
    scraper.refresh(verbose=False)

    canon_id = "500j-2030-05-01-007"
    snaps = get_snapshots_for_match(canon_id)
    assert len(snaps) == 1
    snap = snaps[0]

    # 3. 完赛 finished 2-1
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
    monkeypatch.setattr(
        "utils.fetcher_500.fetch_finished_matches",
        lambda: [dict(finished_row)],
    )

    res3 = scraper.refresh(verbose=False)
    assert res3["settlements_added"] == 1
    assert res3["evaluation_rows_added"] == 1
    assert res3["calibrated"] == 1

    # 验证结算只有 1 条且属于同一个 canonical match_id
    settlements_list = get_settlements_for_match(canon_id)
    assert len(settlements_list) == 1
    assert settlements_list[0]["match_id"] == canon_id
    assert settlements_list[0]["snapshot_id"] == snap["snapshot_id"]

    # 验证评估样本恰好 1 条
    eval_rows = get_all_evaluation_rows()
    assert len(eval_rows) == 1
    assert eval_rows[0]["match_id"] == canon_id

    # 验证再次刷新不重复结算或二次校准 Elo
    res4 = scraper.refresh(verbose=False)
    assert res4["settlements_added"] == 0
    assert res4["evaluation_rows_added"] == 0
    assert res4["calibrated"] == 0


def test_new_unpriced_unrelated_live_event_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    # 未跟踪、且无可用盘口的全新 live 比赛
    unpriced_live = {
        "id": "500l-2030-05-01-999",
        "sport": "football",
        "league": "未知杯赛",
        "date": "2030-05-01",
        "time": "18:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "队伍X",
        "away": "队伍Y",
        "odds": None,
    }

    monkeypatch.setattr(
        "utils.fetcher_500.fetch_live_matches",
        lambda sport: [dict(unpriced_live)] if sport == "football" else [],
    )
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

    res = scraper.refresh(verbose=False)
    assert res["total"] == 0
    assert res["sources"]["market_rejected"] == 1
    assert len(daily_loader.get_all_matches()) == 0


def test_matched_market_row_does_not_produce_second_fallback():
    # 模拟 live 行与 market 行同属于 007
    live_match = {
        "id": "500l-2030-05-01-007",
        "sport": "football",
        "date": "2030-05-01",
        "time": "20:00",
        "home": "皇马",
        "away": "巴萨",
        "jczq_no": "007",
        "odds": None,
    }
    market_row = {
        "id": "500j-2030-05-01-007",
        "sport": "football",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": False,
        "home": "皇马",
        "away": "巴萨",
        "jczq_no": "007",
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
    }

    incoming = [live_match]
    count, used = scraper._attach_odds(incoming, [market_row])
    assert count == 1
    assert live_match["odds"] is not None

    # 调用追加未消费候选
    added = scraper._append_unmatched_market_candidates(incoming, [market_row], used)
    # 因为该行已在 used 中且与 incoming 是同一事件，绝不产生第二个候选
    assert added == 0
    assert len(incoming) == 1


def test_exact_team_fallback_reconciliation():
    # 双方均无赛事编号，但队伍完全一致
    old_fallback = {
        "id": "500j-2030-05-01-teams",
        "sport": "football",
        "date": "2030-05-01",
        "time": "00:00",
        "kickoff_time_known": False,
        "home": "曼联",
        "away": "利物浦",
        "odds": {"home_win": 2.5, "draw": 3.4, "away_win": 2.7},
    }
    new_live = {
        "id": "500l-2030-05-01-live",
        "sport": "football",
        "date": "2030-05-01",
        "time": "21:30",
        "kickoff_time_known": True,
        "home": "曼联",
        "away": "利物浦",
        "odds": None,
    }

    merged, added, updated = scraper._merge([old_fallback], [new_live])
    assert len(merged) == 1
    assert added == 0
    assert updated == 1
    m = merged[0]
    # 保留原有的 canonical id
    assert m["id"] == "500j-2030-05-01-teams"
    # 升级真实开赛时间
    assert m["time"] == "21:30"
    assert m["kickoff_time_known"] is True
    # 保留已有盘口
    assert m["odds"] == {"home_win": 2.5, "draw": 3.4, "away_win": 2.7}


def test_existing_historical_artifacts_preserved_on_reconciliation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(daily_loader, "DAILY_FILE", "daily_matches.json")
    monkeypatch.setattr(scraper, "DAILY_FILE", "daily_matches.json")

    canon_id = "500j-2030-05-01-007"
    match_rec = {
        "id": canon_id,
        "sport": "football",
        "league": "西甲",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "皇马",
        "away": "巴萨",
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "jczq_no": "007",
        "market_tracked": True,
    }

    # 1. 种子：生成快照和赔率历史
    t1 = datetime(2030, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
    snap, _ = capture_snapshot(match_rec, now=t1)
    odds_snap = record_odds_snapshot(match_rec, now=t1)
    assert snap is not None
    assert odds_snap is not None

    # 2. 模拟比赛结束结算并生成评估样本
    t2 = datetime(2030, 5, 1, 23, 0, tzinfo=BEIJING_TZ)
    fin_match = dict(match_rec)
    fin_match["status"] = "finished"
    fin_match["score"] = {"ft": [2, 1]}
    st, _ = settle_snapshot(snap, fin_match, now=t2)
    assert st is not None
    ev_row, _ = capture_evaluation_row(snap, st, now=t2)
    assert ev_row is not None

    snap_id = snap["snapshot_id"]
    settle_id = st["settlement_id"]
    eval_id = ev_row["evaluation_id"]

    # 3. 产生一个新的 incoming 记录进行对账（例如主客队排名更新等）
    incoming_update = [
        {
            "id": "500l-2030-05-01-007",
            "sport": "football",
            "league": "西班牙甲级联赛",
            "date": "2030-05-01",
            "time": "20:00",
            "kickoff_time_known": True,
            "status": "finished",
            "home": "皇家马德里",
            "away": "巴塞罗那",
            "home_rank": 1,
            "away_rank": 2,
            "score": {"ft": [2, 1]},
            "jczq_no": "007",
            "odds": None,
        }
    ]

    merged, added, updated = scraper._merge([fin_match], incoming_update)
    assert len(merged) == 1
    # 终态比赛保持不变，幂等无变更
    assert updated == 0
    assert merged[0]["id"] == canon_id
    assert merged[0]["score"] == {"ft": [2, 1]}
    assert merged[0]["status"] == "finished"

    # 4. 验证不可变历史记录分毫未变
    snap_after = prediction_snapshots.get_snapshot(snap_id)
    assert snap_after == snap
    assert snap_after["home_team"] == "皇马"  # 保持快照时的旧队名

    odds_snaps_after = odds_snapshots.get_odds_history_for_match(canon_id)
    assert len(odds_snaps_after) == 1
    assert odds_snaps_after[0] == odds_snap

    settlements_after = settlements.get_settlements_for_match(canon_id)
    assert len(settlements_after) == 1
    assert settlements_after[0] == st

    eval_rows_after = get_all_evaluation_rows()
    assert len(eval_rows_after) == 1
    assert eval_rows_after[0] == ev_row


def test_model_versions_remain_unchanged():
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"
    assert config.MODEL_VERSIONS["basketball"] == "basketball-margin-1"
    assert config.MODEL_VERSION == "baseline-1"

