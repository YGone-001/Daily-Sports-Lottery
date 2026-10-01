"""
抓取器集成测试：验证赔率历史在正常 refresh 流程中自动捕获。

全部数据源均被替换为假数据，测试不依赖任何网络访问。
"""
from __future__ import annotations

from utils import fetcher_500, scraper
from utils.daily_loader import load_json
from utils.odds_snapshots import get_odds_history_for_match

MATCH_ID = "500j-2030-03-01-3001"
ODDS_A = {"home_win": 2.10, "draw": 3.20, "away_win": 3.40, "matchnum": "3001"}
ODDS_B = {"home_win": 1.95, "draw": 3.30, "away_win": 3.80, "matchnum": "3001"}


def _fake_sources(monkeypatch, current: dict) -> None:
    """把所有网络数据源替换为受控假数据。"""
    base = {
        "id": MATCH_ID,
        "sport": "football",
        "league": "英超",
        "date": "2030-03-01",
        "time": "20:00",
        "status": "upcoming",
        "home": "曼城",
        "away": "阿森纳",
        "home_rank": None,
        "away_rank": None,
        "score": None,
    }

    def fake_live(sport):
        if sport != "football":
            return []
        return [dict(base, odds=dict(current["odds"]))]

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", fake_live)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])


def test_refresh_captures_only_changed_pre_match_odds(isolated_data_dir, monkeypatch):
    current = {"odds": dict(ODDS_A)}
    _fake_sources(monkeypatch, current)

    # refresh #1：首次观察 -> 新增 1 条
    r1 = scraper.refresh(verbose=False)
    assert r1["odds_snapshots_added"] == 1
    assert len(get_odds_history_for_match(MATCH_ID)) == 1

    # refresh #2：盘口未变 -> 不新增
    r2 = scraper.refresh(verbose=False)
    assert r2["odds_snapshots_added"] == 0
    assert len(get_odds_history_for_match(MATCH_ID)) == 1

    # refresh #3：盘口变化 -> 恰好新增 1 条
    current["odds"] = dict(ODDS_B)
    r3 = scraper.refresh(verbose=False)
    assert r3["odds_snapshots_added"] == 1

    history = get_odds_history_for_match(MATCH_ID)
    assert len(history) == 2
    assert history[0]["odds"] == ODDS_A
    assert history[1]["odds"] == ODDS_B


def test_refresh_keeps_latest_odds_in_match_record(isolated_data_dir, monkeypatch):
    """赔率历史是附加层：daily_matches.json 仍保存最新盘口。"""
    current = {"odds": dict(ODDS_A)}
    _fake_sources(monkeypatch, current)
    scraper.refresh(verbose=False)

    current["odds"] = dict(ODDS_B)
    scraper.refresh(verbose=False)

    stored = {m["id"]: m for m in load_json("daily_matches.json")["matches"]}
    assert stored[MATCH_ID]["odds"]["home_win"] == 1.95
    assert len(get_odds_history_for_match(MATCH_ID)) == 2


def test_refresh_without_odds_reports_zero(isolated_data_dir, monkeypatch):
    current = {"odds": None}
    _fake_sources(monkeypatch, current)

    result = scraper.refresh(verbose=False)
    assert result["odds_snapshots_added"] == 0
    assert get_odds_history_for_match(MATCH_ID) == []
