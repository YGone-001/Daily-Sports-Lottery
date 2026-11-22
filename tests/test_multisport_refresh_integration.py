"""
隔离式多运动（足球 + 篮球）refresh 集成验收。

一次真实 ``scraper.refresh()`` 同时摄入两种运动，对需求清单逐项测量：
源观测、市场候选准入、预测快照、赔率历史、结算与评估样本、刷新代号与状态；
未知开赛时间不得被伪造；Canonical 身份与主客方向保持；赛毕结算与评估闭环；
并精确验证每一步究竟改写了哪些存储文件（其余文件字节不变）。

全部数据源为受控合成数据，仅用于隔离测试（SYNTHETIC_TEST_VERIFIED）；
不触碰真实数据目录，不发起任何网络请求。
"""
from __future__ import annotations

from copy import deepcopy
import json
import os

import pytest

import config
from utils import (
    daily_loader,
    evaluation_rows as evaluation_store,
    fetcher_500,
    nba_schedule,
    prediction_snapshots,
    settlements as settlements_store,
    scraper,
)
from utils.daily_loader import override_beijing_now
from test_basketball_operational_acceptance import write_stores, store_hashes
from test_nba_schedule_completeness import NOW

FOOTBALL_ID = "500j-2030-05-15-3001"
BASKETBALL_ID = "500j-2030-05-16-4306"
UNKNOWN_KICKOFF_ID = "500j-2030-05-16-4307"


def _football(**kw) -> dict:
    base = {
        "id": FOOTBALL_ID,
        "sport": "football",
        "league": "英超",
        "date": "2030-05-15",
        "time": "21:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "曼城",
        "away": "阿森纳",
        "home_rank": None,
        "away_rank": None,
        "jczq_no": "3001",
        "round": "3001",
        "market_tracked": True,
        "score": None,
        "odds": None,
    }
    base.update(kw)
    return base


def _basketball(**kw) -> dict:
    base = {
        "id": BASKETBALL_ID,
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-16",
        "time": "10:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "home_rank": None,
        "away_rank": None,
        "jczq_no": "4306",
        "round": "4306",
        "market_tracked": True,
        "score": None,
        "odds": None,
    }
    base.update(kw)
    return base


FOOTBALL_ODDS = {
    # 足球同时带 1X2（spf）与大小球两类市场：合并口径要求两类行都被消费。
    "home_win": 1.90,
    "draw": 3.50,
    "away_win": 4.00,
    "total_line": 2.5,
    "over": 1.85,
    "under": 1.95,
    "matchnum": "3001",
}
BASKETBALL_ODDS = {
    # 篮球同时带胜负（sf）与大小分（dxf）两类市场。
    "home_win": 1.85,
    "away_win": 1.95,
    "total_line": 220.5,
    "over": 1.90,
    "under": 1.90,
    "matchnum": "4306",
}
UNKNOWN_KICKOFF_ODDS = {"home_win": 1.70, "away_win": 2.10, "matchnum": "4307"}


def _upcoming_sources() -> tuple[list[dict], list[dict]]:
    """权威赛程（无盘口）+ 独立 XML 盘口行（需经 _attach_odds 合并）。"""
    football = _football()
    basketball = _basketball()
    unknown = _basketball(
        id=UNKNOWN_KICKOFF_ID,
        jczq_no="4307",
        round="4307",
        time="00:00",
        kickoff_time_known=False,
        home="菲尼克斯太阳",
        away="丹佛掘金",
    )
    # 真实管道中盘口来自独立 XML 源：live 行只给身份与时间，盘口由 XML 行提供。
    for row in (football, basketball, unknown):
        row.pop("odds")
    football_xml = {**football, "odds": deepcopy(FOOTBALL_ODDS)}
    basketball_xml = {**basketball, "odds": deepcopy(BASKETBALL_ODDS)}
    unknown_xml = {**unknown, "odds": deepcopy(UNKNOWN_KICKOFF_ODDS)}
    live_rows = [football, basketball, unknown]
    xml_rows = [football_xml, basketball_xml, unknown_xml]
    return live_rows, xml_rows


def _finished_versions() -> list[dict]:
    football = _football(
        status="finished",
        score={"ft": [2, 1], "ht": [1, 0]},
        odds=deepcopy(FOOTBALL_ODDS),
    )
    basketball = _basketball(
        status="finished",
        score={"ft": [110, 104]},
        odds=deepcopy(BASKETBALL_ODDS),
    )
    return [football, basketball]


def _mock_sources(monkeypatch, live_rows: list[dict], xml_rows: list[dict]) -> None:
    monkeypatch.setattr(
        fetcher_500,
        "fetch_live_matches",
        lambda sport: [m for m in live_rows if m.get("sport") == "football"],
    )
    monkeypatch.setattr(
        fetcher_500,
        "fetch_live_basketball",
        lambda: [m for m in live_rows if m.get("sport") == "basketball"],
    )
    monkeypatch.setattr(
        fetcher_500,
        "fetch_jczq_xml",
        lambda sport: [m for m in xml_rows if m.get("sport") == sport],
    )
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])
    # 提供商赛程网络保持默认拒绝；并直接封死 HTTP 客户端以防任何意外外呼。
    monkeypatch.setattr(config, "ENABLE_NBA_SCHEDULE_NETWORK", False, raising=False)
    monkeypatch.setattr(
        nba_schedule.requests, "get", lambda *a, **k: pytest.fail("GET attempted network")
    )


def _read_marker(directory) -> dict:
    with open(os.path.join(str(directory), ".refresh_state.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _changed(before: dict, after: dict) -> set[str]:
    return {name for name in before if before[name] != after[name]}


# ---------------------------------------------------------------------------
# 主验收：一次 refresh 同时摄入足球 + 篮球，并精确对账每一步的文件改写
# ---------------------------------------------------------------------------

def test_combined_football_basketball_refresh_measurement(isolated_data_dir, monkeypatch):
    directory = isolated_data_dir
    write_stores(directory, [])
    live_rows, xml_rows = _upcoming_sources()
    _mock_sources(monkeypatch, live_rows, xml_rows)

    hashes_t0 = store_hashes(directory)

    # ---- Refresh 1：两种运动的未开赛赛事准入 + 预测快照 + 赔率历史 ----
    with override_beijing_now(NOW):
        r1 = scraper.refresh(verbose=False)

    assert r1["sources"]["live_football"] == 1
    assert r1["sources"]["live_basketball"] == 2
    assert r1["added"] == 3
    assert r1["prediction_snapshots_added"] == 2
    assert r1["settlements_added"] == 0
    assert r1["evaluation_rows_added"] == 0

    stored = {m["id"]: m for m in daily_loader.get_all_matches()}
    assert set(stored) == {FOOTBALL_ID, BASKETBALL_ID, UNKNOWN_KICKOFF_ID}
    # 主客方向与竞彩编号保持源声明，不得被改写
    assert stored[FOOTBALL_ID]["home"] == "曼城" and stored[FOOTBALL_ID]["away"] == "阿森纳"
    assert stored[FOOTBALL_ID]["jczq_no"] == "3001"
    assert stored[BASKETBALL_ID]["home"] == "洛杉矶湖人" and stored[BASKETBALL_ID]["away"] == "金州勇士"
    # 两类市场（1X2 + 大小 / 胜负 + 大小分）都在合并后的记录中
    for key, value in FOOTBALL_ODDS.items():
        assert stored[FOOTBALL_ID]["odds"][key] == value, key
    for key, value in BASKETBALL_ODDS.items():
        assert stored[BASKETBALL_ID]["odds"][key] == value, key

    # 未知开赛时间：落盘事实保持「未知」，绝不被伪造为已知
    assert stored[UNKNOWN_KICKOFF_ID]["kickoff_time_known"] is False
    assert stored[UNKNOWN_KICKOFF_ID]["time"] == "00:00"
    snap_ids = {s["match_id"] for s in prediction_snapshots.get_snapshots_for_match(UNKNOWN_KICKOFF_ID)}
    assert snap_ids == set()  # 未知开赛时间的赛事不生成赛前预测
    known_ids = {s["match_id"] for s in prediction_snapshots.get_snapshots_for_match(FOOTBALL_ID)}
    known_ids |= {s["match_id"] for s in prediction_snapshots.get_snapshots_for_match(BASKETBALL_ID)}
    assert known_ids == {FOOTBALL_ID, BASKETBALL_ID}

    marker = _read_marker(directory)
    assert marker["status"] == "COMPLETED"
    assert marker["generation"] == 1

    hashes_t1 = store_hashes(directory)
    # 球队档案（team_strength）在刷新内为未开赛赛事估算实力并持久化——这是刷新写路径，
    # 不是 GET 物化；无完赛时 calibrated.json 不变。
    assert _changed(hashes_t0, hashes_t1) == {
        "daily_matches.json",
        "team_strength.json",
        "prediction_snapshots.json",
        "odds_snapshots.json",
    }

    # ---- Refresh 2：完赛 -> 结算 + 评估；既有快照不可变 ----
    finished = _finished_versions()
    finished.append(deepcopy(live_rows[2]))  # 未知开赛时间的赛事保持原状（仍为 upcoming）
    _mock_sources(monkeypatch, finished, xml_rows)
    snapshots_before = {
        mid: prediction_snapshots.get_snapshots_for_match(mid)
        for mid in (FOOTBALL_ID, BASKETBALL_ID)
    }

    with override_beijing_now(NOW):
        r2 = scraper.refresh(verbose=False)

    assert r2["prediction_snapshots_added"] == 0
    assert r2["settlements_added"] == 2
    assert r2["evaluation_rows_added"] == 2

    for mid in (FOOTBALL_ID, BASKETBALL_ID):
        assert prediction_snapshots.get_snapshots_for_match(mid) == snapshots_before[mid]

    settlement_football = settlements_store.get_settlements_for_match(FOOTBALL_ID)[0]
    row_football = evaluation_store.get_evaluation_rows_for_match(FOOTBALL_ID)[0]
    assert row_football["snapshot_id"] == settlement_football["snapshot_id"]
    assert row_football["final_score"] == {"home": 2, "away": 1}
    row_basketball = evaluation_store.get_evaluation_rows_for_match(BASKETBALL_ID)[0]
    assert row_basketball["final_score"] == {"home": 110, "away": 104}

    marker = _read_marker(directory)
    assert marker["status"] == "COMPLETED"
    assert marker["generation"] == 2

    hashes_t2 = store_hashes(directory)
    assert _changed(hashes_t1, hashes_t2) == {
        "daily_matches.json",
        "team_strength.json",
        "calibrated.json",
        "settlements.json",
        "evaluation_rows.json",
    }

    # ---- Refresh 3：重复观测同一完赛结果 -> 幂等，零新增、零文件改写 ----
    with override_beijing_now(NOW):
        r3 = scraper.refresh(verbose=False)

    assert r3["added"] == 0
    assert r3["prediction_snapshots_added"] == 0
    assert r3["settlements_added"] == 0
    assert r3["evaluation_rows_added"] == 0
    assert store_hashes(directory) == hashes_t2


# ---------------------------------------------------------------------------
# 空源 ≠ 成功入库：全部源为空时，刷新完成但一切计数为零，绝不编造赛事
# ---------------------------------------------------------------------------

def test_empty_sources_complete_without_fabrication(isolated_data_dir, monkeypatch):
    directory = isolated_data_dir
    write_stores(directory, [])
    _mock_sources(monkeypatch, [], [])
    hashes_t0 = store_hashes(directory)

    with override_beijing_now(NOW):
        result = scraper.refresh(verbose=False)

    assert result["sources"]["live_football"] == 0
    assert result["sources"]["live_basketball"] == 0
    assert result["added"] == 0
    assert result["odds_snapshots_added"] == 0
    assert result["prediction_snapshots_added"] == 0
    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 0
    assert daily_loader.get_all_matches() == []
    # 空源批次在**任何**存储写入之前就提前返回（scraper._refresh_impl 的
    # 「if not incoming」守卫）：七个存储全部字节不变，只推进刷新标记。
    assert _changed(hashes_t0, store_hashes(directory)) == set()
    marker = _read_marker(directory)
    assert marker["status"] == "COMPLETED"
