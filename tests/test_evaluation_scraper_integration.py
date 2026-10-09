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
    """首次见到就已完赛且无盘口覆盖：不进入跟踪集合 -> 无快照 -> 无结算 -> 无评估样本。"""
    _fake_sources(monkeypatch, [_match("m-hist", status="finished", score={"ft": [3, 1]}, odds=None)])

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


# ---------------------------------------------------------------------------
# Authoritative Finality Enforcement Tests for Materialization
# ---------------------------------------------------------------------------

def test_materialize_rejects_live_match_with_legacy_settlement(isolated_data_dir, monkeypatch):
    """
    6. Actual `_materialize_evaluation_rows()` rejects a live canonical match
    with an existing historical settlement.
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match

    # Create snapshot and historical settlement
    m_upcoming = enrich_match(_match("m-live-hist"))
    snapshot, created = capture_snapshot(m_upcoming)
    
    # We forcefully create a historical settlement using the direct API
    m_finished = dict(m_upcoming, status="finished", score={"ft": [1, 0]})
    settlement, _ = settle_snapshot(snapshot, m_finished)
    
    # Now the canonical match is somehow 'live' (or remains 'live' in some stale state)
    canonical_live = dict(m_upcoming, status="live", score={"ft": [1, 0]})
    
    added = _materialize_evaluation_rows([canonical_live])
    assert added == 0
    assert len(get_evaluation_rows_for_match("m-live-hist")) == 0


def test_materialize_rejects_settlement_with_different_score(isolated_data_dir, monkeypatch):
    """
    7. Actual `_materialize_evaluation_rows()` rejects an explicitly finished match
    whose settlement has a different final score.
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match

    m_upcoming = enrich_match(_match("m-diff-score"))
    snapshot, created = capture_snapshot(m_upcoming)
    
    # Historical settlement score = 1-0
    m_finished_1 = dict(m_upcoming, status="finished", score={"ft": [1, 0]})
    settlement, _ = settle_snapshot(snapshot, m_finished_1)
    
    # Canonical match is finished but score = 1-1
    canonical_finished = dict(m_upcoming, status="finished", score={"ft": [1, 1]})
    added = _materialize_evaluation_rows([canonical_finished])
    
    assert added == 0
    assert len(get_evaluation_rows_for_match("m-diff-score")) == 0


def test_materialize_rejects_outcome_mismatch(isolated_data_dir, monkeypatch):
    """
    Correction D - Case 1: Outcome mismatch
    canonical score = 2-1
    settlement score = 2-1
    settlement fingerprint = correct
    settlement outcome = draw
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match
    from utils.atomic_json import load_json_file, atomic_write_json
    from utils.settlements import store_path

    m_upcoming = enrich_match(_match("m-diff-out"))
    snapshot, _ = capture_snapshot(m_upcoming)
    
    m_finished = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    settlement, _ = settle_snapshot(snapshot, m_finished)
    
    # Tamper with outcome only
    settlement["actual_outcome"] = "draw"
    
    store = load_json_file(store_path(), None)
    store["settlements"][settlement["settlement_id"]] = settlement
    atomic_write_json(store_path(), store)

    canonical_finished = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    added = _materialize_evaluation_rows([canonical_finished])
    
    assert added == 0
    assert len(get_evaluation_rows_for_match("m-diff-out")) == 0


def test_materialize_rejects_fingerprint_mismatch(isolated_data_dir, monkeypatch):
    """
    Correction D - Case 2: Fingerprint mismatch
    canonical score = 2-1
    settlement score = 2-1
    settlement outcome = home_win
    settlement fingerprint = invalid
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match
    from utils.atomic_json import load_json_file, atomic_write_json
    from utils.settlements import store_path

    m_upcoming = enrich_match(_match("m-diff-fp"))
    snapshot, _ = capture_snapshot(m_upcoming)
    
    m_finished = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    settlement, _ = settle_snapshot(snapshot, m_finished)
    
    # Tamper with fingerprint only
    settlement["result_fingerprint"] = "invalid_fingerprint_hash"
    
    store = load_json_file(store_path(), None)
    store["settlements"][settlement["settlement_id"]] = settlement
    atomic_write_json(store_path(), store)

    canonical_finished = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    added = _materialize_evaluation_rows([canonical_finished])
    
    assert added == 0
    assert len(get_evaluation_rows_for_match("m-diff-fp")) == 0


def test_materialize_valid_legacy_settlement_exactly_once(isolated_data_dir, monkeypatch):
    """
    Correction D - Case 3: Valid record
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match

    m_upcoming = enrich_match(_match("m-valid-hist"))
    snapshot, created = capture_snapshot(m_upcoming)
    
    m_finished = dict(m_upcoming, status="finished", score={"ft": [2, 0]})
    settlement, _ = settle_snapshot(snapshot, m_finished)
    
    # First materialization pass
    canonical_finished = dict(m_upcoming, status="finished", score={"ft": [2, 0]})
    added_first = _materialize_evaluation_rows([canonical_finished])
    
    assert added_first == 1
    evals = get_evaluation_rows_for_match("m-valid-hist")
    assert len(evals) == 1
    eval_row_first = evals[0].copy()
    
    # Second materialization pass (zero on replay)
    added_second = _materialize_evaluation_rows([canonical_finished])
    assert added_second == 0
    
    evals_second = get_evaluation_rows_for_match("m-valid-hist")
    assert len(evals_second) == 1
    assert evals_second[0] == eval_row_first


def test_materialize_rejects_settlement_with_different_fingerprint_or_outcome(isolated_data_dir, monkeypatch):
    """
    Restore previously accepted regression identity:
    Rejects a settlement whose result fingerprint or actual outcome disagrees 
    with the canonical result.
    """
    from utils.scraper import _materialize_evaluation_rows
    from utils.daily_loader import enrich_match
    from utils.atomic_json import load_json_file, atomic_write_json
    from utils.settlements import store_path

    m_upcoming = enrich_match(_match("m-diff-both"))
    snapshot, created = capture_snapshot(m_upcoming)
    
    m_finished_1 = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    settlement, _ = settle_snapshot(snapshot, m_finished_1)
    
    # Tamper with outcome to simulate mismatch
    settlement["actual_outcome"] = "draw"
    
    # Manually tamper with the storage
    store = load_json_file(store_path(), None)
    store["settlements"][settlement["settlement_id"]] = settlement
    atomic_write_json(store_path(), store)

    canonical_finished = dict(m_upcoming, status="finished", score={"ft": [2, 1]})
    added = _materialize_evaluation_rows([canonical_finished])
    
    assert added == 0
    assert len(get_evaluation_rows_for_match("m-diff-both")) == 0
