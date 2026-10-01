"""
抓取器集成测试：验证评估样本在 refresh 流程中自动物化，
且绝不重建缺失的历史数据。全部数据源被替换为假数据。
"""
from __future__ import annotations

from utils import fetcher_500, scraper
from utils.daily_loader import enrich_match
from utils.evaluation_rows import get_evaluation_rows_for_match
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match, settle_snapshot

LEAGUE = "英超"


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
    odds: dict | None = None,
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
        "odds": odds,
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
    # Refresh A：未开赛 -> 生成预测快照，无结算、无评估样本
    _fake_sources(monkeypatch, [_match("m-1")])
    a = scraper.refresh(verbose=False)

    assert a["prediction_snapshots_added"] == 1
    assert a["settlements_added"] == 0
    assert a["evaluation_rows_added"] == 0

    snapshot = get_snapshots_for_match("m-1")[0]

    # Refresh B：完赛 2-1 -> 结算 + 评估样本
    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 1]})])
    b = scraper.refresh(verbose=False)

    assert b["prediction_snapshots_added"] == 0
    assert b["settlements_added"] == 1
    assert b["evaluation_rows_added"] == 1

    settlement = get_settlements_for_match("m-1")[0]
    rows = get_evaluation_rows_for_match("m-1")
    assert len(rows) == 1
    assert rows[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert rows[0]["settlement_id"] == settlement["settlement_id"]
    assert rows[0]["final_score"] == {"home": 2, "away": 1}
    assert rows[0]["home_elo"] == snapshot["home_elo"]

    # Refresh C：同一完赛结果重复观测 -> 不新增任何东西
    c = scraper.refresh(verbose=False)

    assert c["settlements_added"] == 0
    assert c["evaluation_rows_added"] == 0
    assert len(get_snapshots_for_match("m-1")) == 1
    assert len(get_settlements_for_match("m-1")) == 1
    assert len(get_evaluation_rows_for_match("m-1")) == 1


def test_first_seen_finished_creates_nothing(isolated_data_dir, monkeypatch):
    """首次见到就已完赛的比赛：无快照 -> 无结算 -> 无评估样本。"""
    _fake_sources(monkeypatch, [_match("m-hist", status="finished", score={"ft": [3, 1]})])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 0
    assert get_snapshots_for_match("m-hist") == []
    assert get_settlements_for_match("m-hist") == []
    assert get_evaluation_rows_for_match("m-hist") == []


def test_existing_settlement_backfill(isolated_data_dir, monkeypatch):
    """
    结算在评估层出现之前就已存在时，下一次正常刷新应补齐评估样本，
    且不重建结算、不重跑预测。
    """
    enriched = enrich_match(_match("m-back", status="upcoming"))
    snapshot, created = capture_snapshot(enriched)
    assert created is True
    finished = dict(enriched, status="finished", date="2020-01-01", time="20:00",
                    score={"ft": [2, 1]})
    settlement, settled = settle_snapshot(snapshot, finished)
    assert settled is True
    assert get_evaluation_rows_for_match("m-back") == []

    _fake_sources(monkeypatch, [_match("m-back", status="finished", score={"ft": [2, 1]})])
    result = scraper.refresh(verbose=False)

    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 1

    rows = get_evaluation_rows_for_match("m-back")
    assert len(rows) == 1
    assert rows[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert rows[0]["settlement_id"] == settlement["settlement_id"]


def test_materialization_ignores_current_state_changes(isolated_data_dir, monkeypatch):
    """物化后改动当前球队实力，评估样本保持不变。"""
    from utils.evaluation_rows import get_evaluation_row
    from utils.team_strength import update_from_result

    _fake_sources(monkeypatch, [_match("m-1")])
    scraper.refresh(verbose=False)
    _fake_sources(monkeypatch, [_match("m-1", status="finished", score={"ft": [2, 1]})])
    scraper.refresh(verbose=False)

    original = get_evaluation_rows_for_match("m-1")[0]

    update_from_result("曼城", "阿森纳", 7, 0, LEAGUE, "football")
    replay = scraper.refresh(verbose=False)

    assert replay["evaluation_rows_added"] == 0
    after = get_evaluation_row(original["evaluation_id"])
    assert after == original


def test_metrics_preserved_in_refresh_result(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("m-1")])
    result = scraper.refresh(verbose=False)

    for key in (
        "added", "updated", "calibrated",
        "odds_snapshots_added", "prediction_snapshots_added",
        "settlements_added", "evaluation_rows_added", "total",
    ):
        assert key in result, f"缺少既有字段: {key}"


def test_basketball_evaluation_row(isolated_data_dir, monkeypatch):
    _fake_sources(
        monkeypatch,
        [_match("m-bb", sport="basketball", league="NBA", home="湖人", away="凯尔特人",
                odds={"home_win": 1.8, "away_win": 2.0})],
    )
    scraper.refresh(verbose=False)

    _fake_sources(
        monkeypatch,
        [_match("m-bb", sport="basketball", league="NBA", home="湖人", away="凯尔特人",
                status="finished", score={"ft": [108, 101]})],
    )
    result = scraper.refresh(verbose=False)

    assert result["settlements_added"] == 1
    assert result["evaluation_rows_added"] == 1

    row = get_evaluation_rows_for_match("m-bb")[0]
    assert row["sport"] == "basketball"
    assert row["final_score"] == {"home": 108, "away": 101}
    assert row["actual_outcome"] == "home_win"
