"""
抓取器集成测试：验证赛前预测快照在 refresh 流程中**自动捕获**，
无需访问任何预测页面或预测 API。全部数据源被替换为假数据。
"""
from __future__ import annotations

from datetime import timedelta

from utils import fetcher_500, scraper
from utils.daily_loader import get_match_datetime, load_json
from utils.prediction_snapshots import get_snapshots_for_match
from utils.team_strength import get_team_profile, update_from_result

LEAGUE = "英超"

# 默认给比赛一个完整的足球 1X2 盘口：市场准入要求可用盘口覆盖，
# 因此「被跟踪的比赛」在测试夹具里默认就是有盘口的。
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
# 主验收：refresh 单独即可保证捕获（不触碰任何 Flask 预测路由）
# ---------------------------------------------------------------------------

def test_refresh_captures_snapshot_without_route_access(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("up-1", odds={"home_win": 1.9, "draw": 3.5, "away_win": 4.0})])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 1
    snapshots = get_snapshots_for_match("up-1")
    assert len(snapshots) == 1
    assert snapshots[0]["match_id"] == "up-1"
    assert snapshots[0]["model_version"]  # 已写入模型版本


def test_second_refresh_is_idempotent(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("up-1")])

    first = scraper.refresh(verbose=False)
    second = scraper.refresh(verbose=False)

    assert first["prediction_snapshots_added"] == 1
    assert second["prediction_snapshots_added"] == 0
    assert len(get_snapshots_for_match("up-1")) == 1


def test_multiple_upcoming_matches_captured(isolated_data_dir, monkeypatch):
    _fake_sources(
        monkeypatch,
        [
            _match("up-a", home="曼城", away="阿森纳"),
            _match("up-b", home="利物浦", away="切尔西", date="2030-03-02"),
            _match(
                "up-c",
                sport="basketball",
                league="NBA",
                home="湖人",
                away="凯尔特人",
                odds={"home_win": 1.8, "away_win": 2.0, "total_line": 220.5},
            ),
        ],
    )

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 3
    for match_id in ("up-a", "up-b", "up-c"):
        snapshots = get_snapshots_for_match(match_id)
        assert len(snapshots) == 1
        assert snapshots[0]["match_id"] == match_id
    assert get_snapshots_for_match("up-c")[0]["sport"] == "basketball"


def test_tracked_match_survives_odds_loss(isolated_data_dir, monkeypatch):
    """
    一旦通过市场准入，比赛在盘口源暂时缺供时仍保持跟踪：
    留在 canonical 集合、保持 market_tracked、保留最后已知盘口，
    并且已有的预测快照不被重复创建。
    """
    _fake_sources(monkeypatch, [_match("up-tracked")])
    assert scraper.refresh(verbose=False)["prediction_snapshots_added"] == 1

    # 本轮盘口源没有该场（odds = None）
    _fake_sources(monkeypatch, [_match("up-tracked", odds=None)])
    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    stored = {m["id"]: m for m in load_json("daily_matches.json")["matches"]}
    assert "up-tracked" in stored
    assert stored["up-tracked"]["market_tracked"] is True
    assert stored["up-tracked"]["odds"]  # 保留最后已知盘口
    assert len(get_snapshots_for_match("up-tracked")) == 1


# ---------------------------------------------------------------------------
# 时序门禁：live / finished 不产生新快照（注入确定性 now）
# ---------------------------------------------------------------------------

def test_live_match_not_captured(isolated_data_dir):
    m = _match("live-1")
    live_now = get_match_datetime(m) + timedelta(minutes=10)

    assert scraper._capture_prediction_snapshots([m], now=live_now) == 0
    assert get_snapshots_for_match("live-1") == []


def test_finished_match_not_captured(isolated_data_dir):
    m = _match("fin-1", status="finished", score={"ft": [2, 1]})
    finished_now = get_match_datetime(m) + timedelta(minutes=200)

    assert scraper._capture_prediction_snapshots([m], now=finished_now) == 0
    assert get_snapshots_for_match("fin-1") == []


# ---------------------------------------------------------------------------
# 时序正确性：Elo 校准必须先于预测快照捕获
# ---------------------------------------------------------------------------

def test_calibration_precedes_prediction_capture(isolated_data_dir, monkeypatch):
    """
    本轮新完赛的结果必须先写入 Elo，随后生成的未来比赛快照应使用最新 Elo，
    而不是刷新前的过期值。这不是未来泄漏——该结果在目标比赛开赛前已发生。
    """
    finished = _match(
        "fin-1", home="曼城", away="阿森纳",
        date="2030-02-01", status="finished", score={"ft": [5, 0]},
    )
    upcoming = _match("up-1", home="曼城", away="利物浦", date="2030-03-01")
    _fake_sources(monkeypatch, [upcoming], finished=[finished])

    elo_before = get_team_profile("曼城", LEAGUE, "football")["elo_rating"]

    result = scraper.refresh(verbose=False)

    elo_after = get_team_profile("曼城", LEAGUE, "football")["elo_rating"]
    assert elo_after != elo_before, "完赛结果应改变球队 Elo"
    assert result["calibrated"] == 1
    assert result["prediction_snapshots_added"] == 1

    snap = get_snapshots_for_match("up-1")[0]
    assert snap["home_elo"] == elo_after
    assert snap["home_elo"] != elo_before


# ---------------------------------------------------------------------------
# 既有快照不可变
# ---------------------------------------------------------------------------

def test_existing_snapshot_not_regenerated(isolated_data_dir, monkeypatch):
    _fake_sources(monkeypatch, [_match("up-1")])

    assert scraper.refresh(verbose=False)["prediction_snapshots_added"] == 1
    original = get_snapshots_for_match("up-1")[0]

    # 实力状态发生变化
    update_from_result("曼城", "阿森纳", 6, 0, LEAGUE, "football")

    second = scraper.refresh(verbose=False)
    assert second["prediction_snapshots_added"] == 0

    after = get_snapshots_for_match("up-1")[0]
    assert after["snapshot_id"] == original["snapshot_id"]
    assert after["generated_at"] == original["generated_at"]
    assert after["home_elo"] == original["home_elo"]
    assert after["display_probabilities"] == original["display_probabilities"]


def test_capture_helper_exact_creation_semantics(isolated_data_dir):
    """同一比赛重复调用：首次计入 1，第二次计入 0，库中仅 1 条。"""
    m = _match("up-exact")
    now = get_match_datetime(m) - timedelta(hours=2)

    assert scraper._capture_prediction_snapshots([m], now=now) == 1
    assert scraper._capture_prediction_snapshots([m], now=now + timedelta(minutes=30)) == 0
    assert len(get_snapshots_for_match("up-exact")) == 1
