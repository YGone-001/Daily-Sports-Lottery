"""
抓取器集成测试：验证结算在 refresh 流程中自动完成，
且**绝不**为已开赛/已完赛比赛回溯生成预测。全部数据源被替换为假数据。
"""
from __future__ import annotations

from datetime import timedelta

from utils import fetcher_500, scraper
from utils.daily_loader import get_match_datetime
from utils.prediction_snapshots import get_snapshots_for_match
from utils.settlements import get_settlements_for_match

LEAGUE = "英超"

# 默认给比赛一个完整的足球 1X2 盘口：市场准入要求可用盘口覆盖。
_DEFAULT_ODDS = {"home_win": 1.90, "draw": 3.50, "away_win": 4.00}


def _match(
    match_id: str,
    *,
    sport: str = "football",
    league: str = LEAGUE,
    home: str = "曼城",
    away: str = "阿森纳",
    date: str = "2030-03-01",
    time: str = "20:00",
    status: str = "upcoming",
    score: dict | None = None,
    odds: dict | None = _DEFAULT_ODDS,
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
    }


def _fake_sources(monkeypatch, upcoming: list[dict], finished: list[dict] | None = None) -> None:
    """把全部网络数据源替换为受控假数据。"""
    def fake_live_matches(sport):
        return [m for m in upcoming if m.get("sport") == "football"]

    def fake_live_basketball():
        return [m for m in upcoming if m.get("sport") == "basketball"]

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", fake_live_matches)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", fake_live_basketball)
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: list(finished or []))


# ---------------------------------------------------------------------------
# 主验收：upcoming -> finished 生命周期
# ---------------------------------------------------------------------------

def test_upcoming_to_finished_lifecycle(isolated_data_dir, monkeypatch):
    # Refresh A：一场未来的未开赛比赛 -> 生成赛前预测快照，无结算
    _fake_sources(monkeypatch, [_match("m-1")])
    a = scraper.refresh(verbose=False)

    assert a["prediction_snapshots_added"] == 1
    assert a["settlements_added"] == 0

    snapshots = get_snapshots_for_match("m-1")
    assert len(snapshots) == 1
    snapshot_id = snapshots[0]["snapshot_id"]

    # Refresh B：同一场比赛现在完赛 2-1 -> 结算，不产生新预测
    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 1]})])
    b = scraper.refresh(verbose=False)

    assert b["prediction_snapshots_added"] == 0
    assert b["settlements_added"] == 1

    settlements = get_settlements_for_match("m-1")
    assert len(settlements) == 1
    assert settlements[0]["snapshot_id"] == snapshot_id
    assert settlements[0]["final_score"] == {"home": 2, "away": 1}
    assert settlements[0]["actual_outcome"] == "home_win"

    # Refresh C：同一完赛结果重复观测 -> 不新增任何东西
    c = scraper.refresh(verbose=False)

    assert c["prediction_snapshots_added"] == 0
    assert c["settlements_added"] == 0
    assert len(get_snapshots_for_match("m-1")) == 1
    assert len(get_settlements_for_match("m-1")) == 1


def test_first_seen_finished_creates_nothing(isolated_data_dir, monkeypatch):
    """
    首次见到就已完赛、且**无可用盘口覆盖**的比赛：
    不进入 canonical 集合 -> 无快照 -> 无结算，也绝不回溯生成预测。
    """
    _fake_sources(monkeypatch, [_match("m-hist", status="finished", score={"ft": [2, 1]}, odds=None)])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert result["settlements_added"] == 0
    assert get_snapshots_for_match("m-hist") == []
    assert get_settlements_for_match("m-hist") == []


def test_conflict_detected_and_existing_record_kept(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-1")])
    scraper.refresh(verbose=False)

    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 1]})])
    assert scraper.refresh(verbose=False)["settlements_added"] == 1

    # 上游随后报告了不同的结果
    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 2]})])
    conflicted = scraper.refresh(verbose=False)

    assert conflicted["settlements_added"] == 0
    settlements = get_settlements_for_match("m-1")
    assert len(settlements) == 1
    assert settlements[0]["final_score"] == {"home": 2, "away": 1}
    assert settlements[0]["actual_outcome"] == "home_win"


def test_conflict_does_not_block_other_matches(isolated_data_dir, monkeypatch):
    """一场比赛发生结算冲突时，其他比赛的结算仍应继续完成。"""
    _fake_sources(monkeypatch, [_match("m-1"), _match("m-2", home="利物浦", away="切尔西", date="2030-03-02")])
    scraper.refresh(verbose=False)

    _fake_sources(
        monkeypatch,
        [
            _match("m-1", status="finished", score={"ft": [2, 1]}),
            _match("m-2", home="利物浦", away="切尔西", date="2030-03-02",
                   status="finished", score={"ft": [0, 3]}),
        ],
    )
    assert scraper.refresh(verbose=False)["settlements_added"] == 2

    # m-1 结果发生变化（冲突），m-2 保持不变
    _fake_sources(
        monkeypatch,
        [
            _match("m-1", status="finished", score={"ft": [3, 3]}),
            _match("m-2", home="利物浦", away="切尔西", date="2030-03-02",
                   status="finished", score={"ft": [0, 3]}),
        ],
    )
    result = scraper.refresh(verbose=False)

    assert result["settlements_added"] == 0
    assert get_settlements_for_match("m-1")[0]["final_score"] == {"home": 2, "away": 1}
    assert get_settlements_for_match("m-2")[0]["final_score"] == {"home": 0, "away": 3}


# ---------------------------------------------------------------------------
# 时序门禁（注入确定性 now）
# ---------------------------------------------------------------------------

def test_live_match_not_settled(isolated_data_dir, make_match):
    from utils.daily_loader import enrich_match
    from utils.prediction_snapshots import capture_snapshot

    m = enrich_match(make_match(id="m-live"))
    snapshot, created = capture_snapshot(m)
    assert created is True

    kickoff = get_match_datetime(m)
    live_match = dict(m, score={"ft": [2, 1]})

    assert scraper._settle_finished_matches([live_match], now=kickoff + timedelta(minutes=10)) == 0
    assert scraper._settle_finished_matches([live_match], now=kickoff - timedelta(hours=2)) == 0
    assert get_settlements_for_match("m-live") == []


def test_metrics_preserved_in_refresh_result(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-1", odds=None)])
    result = scraper.refresh(verbose=False)

    for key in (
        "added", "updated", "calibrated",
        "odds_snapshots_added", "prediction_snapshots_added", "settlements_added", "total",
    ):
        assert key in result, f"缺少既有字段: {key}"
    assert result["odds_snapshots_added"] == 0
