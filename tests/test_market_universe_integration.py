"""
市场准入集成测试：验证 canonical 日常比赛集合只包含有可用盘口覆盖的赛事，
并且已准入比赛的完整生命周期不受盘口源临时缺供影响。

全部数据源被替换为假数据，不访问网络。
"""
from __future__ import annotations

import pytest

from utils import fetcher_500, scraper
from utils.daily_loader import enrich_match, get_logical_matchday, load_json, save_json
from utils.evaluation_rows import get_evaluation_rows_for_match
from utils.odds_snapshots import get_odds_history_for_match
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match

FOOTBALL_1X2 = {"home_win": 1.90, "draw": 3.50, "away_win": 4.00}
BASKETBALL_ML = {"home_win": 1.80, "away_win": 2.00}


def _match(
    match_id: str,
    *,
    sport: str = "football",
    league: str = "英超",
    home: str = "曼城",
    away: str = "阿森纳",
    date: str = "2030-03-01",
    time: str = "20:00",
    status: str = "upcoming",
    score: dict | None = None,
    odds: dict | None = FOOTBALL_1X2,
    jczq_no: str | None = None,
) -> dict:
    return {
        "id": match_id,
        "sport": sport,
        "league": league,
        "date": date,
        "time": time,
        "status": status,
        "home": home,
        "away": away,
        "home_rank": None,
        "away_rank": None,
        "score": score,
        "odds": dict(odds) if isinstance(odds, dict) else None,
        "jczq_no": jczq_no,
    }


def _fake_sources(monkeypatch, upcoming, finished=None, market_rows=None) -> None:
    """把全部网络数据源替换为受控假数据。"""
    def fake_live_matches(sport):
        return [m for m in upcoming if m.get("sport") == "football"]

    def fake_live_basketball():
        return [m for m in upcoming if m.get("sport") == "basketball"]

    def fake_jczq_xml(sport):
        return [r for r in (market_rows or []) if r.get("sport", "football") == sport]

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", fake_live_matches)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", fake_live_basketball)
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", fake_jczq_xml)
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: list(finished or []))


def _write_daily(matches: list[dict]) -> None:
    save_json("daily_matches.json", {"meta": {}, "matches": matches})


def _stored() -> dict[str, dict]:
    return {m["id"]: m for m in load_json("daily_matches.json").get("matches", [])}


# ---------------------------------------------------------------------------
# 广域源过滤
# ---------------------------------------------------------------------------

def test_only_market_covered_matches_are_tracked(isolated_data_dir, monkeypatch):
    """广域源给出 A / B / C，只有 B 拿到完整盘口 -> canonical 集合只保留 B。"""
    a = _match("m-a", home="A队", away="A2队", jczq_no="周三001", odds=None)
    b = _match("m-b", home="B队", away="B2队", jczq_no="周三002", odds=None)
    c = _match("m-c", home="C队", away="C2队", jczq_no="周三003", odds=None)
    market_b = _match("odds-b", home="B队", away="B2队", time="00:00",
                      jczq_no="周三002", odds=FOOTBALL_1X2)
    _fake_sources(monkeypatch, [a, b, c], market_rows=[market_b])

    scraper.refresh(verbose=False)

    stored = _stored()
    assert list(stored) == ["m-b"]
    assert stored["m-b"]["market_tracked"] is True
    assert stored["m-b"]["odds"] == FOOTBALL_1X2
    assert stored["m-b"]["time"] == "20:00"  # 保留 live 行的时间信息


# ---------------------------------------------------------------------------
# 准入规则
# ---------------------------------------------------------------------------

def test_football_complete_market_admitted(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-fb", odds={"home_win": 1.82, "draw": 3.55, "away_win": 4.30})])

    scraper.refresh(verbose=False)

    stored = _stored()
    assert list(stored) == ["m-fb"]
    assert stored["m-fb"]["market_tracked"] is True


def test_football_incomplete_market_rejected(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-fb", odds={"home_win": 1.82, "away_win": 4.30})])

    result = scraper.refresh(verbose=False)

    assert _stored() == {}
    assert result["sources"]["market_rejected"] == 1


def test_basketball_moneyline_admitted(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-bb", sport="basketball", league="NBA",
                                       home="湖人", away="凯尔特人", odds=BASKETBALL_ML)])

    scraper.refresh(verbose=False)

    assert _stored()["m-bb"]["market_tracked"] is True


def test_basketball_total_market_admitted(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-bb", sport="basketball", league="NBA",
                                       home="湖人", away="凯尔特人",
                                       odds={"total_line": 220.5, "over": 1.90, "under": 1.90})])

    scraper.refresh(verbose=False)

    assert _stored()["m-bb"]["market_tracked"] is True


@pytest.mark.parametrize("odds", [{"total_line": 220.5}, {"over": 1.90}, {"under": 1.90}])
def test_incomplete_total_rejected(isolated_data_dir, monkeypatch, odds):
    _fake_sources(monkeypatch, [_match("m-bb", sport="basketball", league="NBA",
                                       home="湖人", away="凯尔特人", odds=odds)])

    scraper.refresh(verbose=False)

    assert _stored() == {}


@pytest.mark.parametrize(
    "bad",
    [None, True, float("nan"), float("inf"), 0, 1.0, -2.0, "2.10"],
)
def test_invalid_price_does_not_admit(isolated_data_dir, monkeypatch, bad):
    _fake_sources(monkeypatch, [_match("m-fb", odds={"home_win": bad, "draw": 3.50, "away_win": 4.00})])

    scraper.refresh(verbose=False)

    assert _stored() == {}


def test_competition_number_alone_is_not_coverage(isolated_data_dir, monkeypatch):
    a = _match("m-1", jczq_no="周三001", odds=None)
    b = _match("m-2", home="利物浦", away="切尔西", jczq_no="周三002", odds={"matchnum": "002"})
    _fake_sources(monkeypatch, [a, b])

    scraper.refresh(verbose=False)

    assert _stored() == {}


# ---------------------------------------------------------------------------
# 生命周期连续性
# ---------------------------------------------------------------------------

def test_tracked_match_survives_odds_loss(isolated_data_dir, monkeypatch):
    """upcoming 有盘口 -> 准入；随后 live 且盘口缺失 -> 保留、状态更新、保留旧盘口。"""
    _fake_sources(monkeypatch, [_match("m-1")])
    assert scraper.refresh(verbose=False)["prediction_snapshots_added"] == 1

    _fake_sources(monkeypatch, [_match("m-1", status="live", odds=None)])
    scraper.refresh(verbose=False)

    stored = _stored()
    assert list(stored) == ["m-1"]
    assert stored["m-1"]["market_tracked"] is True
    assert stored["m-1"]["status"] == "live"
    assert stored["m-1"]["odds"] == FOOTBALL_1X2  # 最后已知盘口被保留


def test_tracked_match_reaches_finished_without_new_odds(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-1")])
    scraper.refresh(verbose=False)

    _fake_sources(monkeypatch, [_match("m-1", status="live", odds=None)])
    scraper.refresh(verbose=False)

    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 1]}, odds=None)])
    result = scraper.refresh(verbose=False)

    stored = _stored()
    assert list(stored) == ["m-1"]
    assert stored["m-1"]["market_tracked"] is True
    assert result["settlements_added"] == 1
    assert result["evaluation_rows_added"] == 1
    assert len(get_snapshots_for_match("m-1")) == 1
    assert len(get_settlements_for_match("m-1")) == 1
    assert len(get_evaluation_rows_for_match("m-1")) == 1


def test_market_source_outage(isolated_data_dir, monkeypatch):
    """盘口源故障：已跟踪比赛存活；新的无盘口全球比赛不得准入。"""
    _fake_sources(monkeypatch, [_match("m-1")])
    scraper.refresh(verbose=False)

    _fake_sources(monkeypatch, [
        _match("m-1", status="live", odds=None),
        _match("m-2", home="新队", away="另一队", date="2030-03-05", odds=None),
    ])
    scraper.refresh(verbose=False)

    stored = _stored()
    assert "m-1" in stored
    assert stored["m-1"]["market_tracked"] is True
    assert "m-2" not in stored


# ---------------------------------------------------------------------------
# 历史集合迁移
# ---------------------------------------------------------------------------

def test_legacy_cleanup(isolated_data_dir, monkeypatch):
    """无标记且无盘口的历史行被清理；无标记但有盘口的历史行被升级为已跟踪。"""
    _write_daily([
        _match("legacy-a", date="2030-09-01", odds=None),
        _match("legacy-b", date="2030-09-02", home="利物浦", away="切尔西", odds=FOOTBALL_1X2),
    ])
    _fake_sources(monkeypatch, [_match("m-new", date="2030-09-03", home="热刺", away="曼联")])

    result = scraper.refresh(verbose=False)

    stored = _stored()
    assert "legacy-a" not in stored
    assert "legacy-b" in stored
    assert stored["legacy-b"]["market_tracked"] is True
    assert result["sources"]["market_tracked_removed"] == 1


def test_historical_stores_preserved_on_cleanup(isolated_data_dir, monkeypatch):
    """日常集合清理不得删除历史不可变存储。"""
    legacy = _match("legacy-a", date="2030-09-01", odds=None)
    snapshot, created = capture_snapshot(enrich_match(legacy))
    assert created is True

    _write_daily([legacy])
    _fake_sources(monkeypatch, [_match("m-new", date="2030-09-03", home="热刺", away="曼联")])

    scraper.refresh(verbose=False)

    assert "legacy-a" not in _stored()
    assert len(get_snapshots_for_match("legacy-a")) == 1
    assert get_snapshots_for_match("legacy-a")[0]["snapshot_id"] == snapshot["snapshot_id"]


# ---------------------------------------------------------------------------
# 赔率 / live 源对账
# ---------------------------------------------------------------------------

def test_unmatched_market_row_becomes_fallback_candidate(isolated_data_dir, monkeypatch):
    """广域 live 源没有该事件，但赔率源有完整盘口 -> 作为兜底候选准入一次。"""
    market = _match("odds-1", date="2030-04-01", home="拜仁", away="多特",
                    jczq_no="周三005", odds=FOOTBALL_1X2)
    _fake_sources(monkeypatch, [], market_rows=[market])

    result = scraper.refresh(verbose=False)

    stored = _stored()
    assert list(stored) == ["odds-1"]
    assert stored["odds-1"]["market_tracked"] is True
    assert result["sources"]["market_candidates"] == 1


def test_matched_market_row_does_not_duplicate(isolated_data_dir, monkeypatch):
    """按竞彩编号挂载成功时，不得再产生第二条 00:00 兜底记录。"""
    live = _match("live-1", date="2030-05-01", home="皇马", away="巴萨",
                  jczq_no="周三007", odds=None)
    market = _match("odds-1", date="2030-05-01", home="皇马", away="巴萨", time="00:00",
                    jczq_no="周三007", odds=FOOTBALL_1X2)
    _fake_sources(monkeypatch, [live], market_rows=[market])

    result = scraper.refresh(verbose=False)

    stored = _stored()
    assert len(stored) == 1
    assert stored["live-1"]["time"] == "20:00"      # 保留 live 行（更完整的时间/状态）
    assert stored["live-1"]["market_tracked"] is True
    assert stored["live-1"]["odds"] == FOOTBALL_1X2
    assert result["sources"]["market_candidates"] == 0


def test_date_home_fallback_does_not_duplicate(isolated_data_dir, monkeypatch):
    """编号缺失时按 (日期, 主队) 匹配，挂载成功后同样不得重复入册。"""
    live = _match("live-2", date="2030-05-02", home="尤文", away="国米", odds=None)
    market = _match("odds-2", date="2030-05-02", home="尤文", away="国米", time="00:00",
                    odds=FOOTBALL_1X2)
    _fake_sources(monkeypatch, [live], market_rows=[market])

    result = scraper.refresh(verbose=False)

    stored = _stored()
    assert len(stored) == 1
    assert "live-2" in stored
    assert stored["live-2"]["odds"] == FOOTBALL_1X2
    assert result["sources"]["market_candidates"] == 0


# ---------------------------------------------------------------------------
# 下游污染防护
# ---------------------------------------------------------------------------

def test_unpriced_global_match_creates_no_downstream_artifacts(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-nomarket", odds=None)])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert result["odds_snapshots_added"] == 0
    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 0
    assert _stored() == {}
    assert get_snapshots_for_match("m-nomarket") == []
    assert get_odds_history_for_match("m-nomarket") == []
    assert get_settlements_for_match("m-nomarket") == []
    assert get_evaluation_rows_for_match("m-nomarket") == []


def test_first_seen_finished_unpriced_not_tracked(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [], finished=[_match("m-hist", status="finished",
                                                    score={"ft": [3, 1]}, odds=None)])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 0
    assert _stored() == {}
    assert get_snapshots_for_match("m-hist") == []
    assert get_settlements_for_match("m-hist") == []
    assert get_evaluation_rows_for_match("m-hist") == []


# ---------------------------------------------------------------------------
# 首页 / 列表 API
# ---------------------------------------------------------------------------

def test_api_lists_only_market_covered_matches(isolated_data_dir, monkeypatch):
    today = get_logical_matchday()
    covered = _match("m-covered", date=today, odds=FOOTBALL_1X2)
    unpriced = _match("m-unpriced", date=today, home="利物浦", away="切尔西", odds=None)
    _fake_sources(monkeypatch, [covered, unpriced])

    scraper.refresh(verbose=False)

    import app as app_module

    client = app_module.app.test_client()
    for path in ("/api/today", f"/api/matches?date={today}"):
        payload = client.get(path).get_json()
        ids = {m["id"] for m in payload}
        assert "m-covered" in ids, path
        assert "m-unpriced" not in ids, path
