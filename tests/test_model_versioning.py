"""
运动专属模型版本基础设施的测试。

足球与篮球各自独立版本化：只改一个运动时，另一个运动的版本与既有快照不受影响。
默认配置下两者都是 `baseline-1`，因此本任务不改变任何既有预测的身份。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import config
from utils import fetcher_500, scraper
from utils.calibration_evaluation import build_calibration_summaries
from utils.classification_evaluation import build_classification_summaries
from utils.daily_loader import enrich_match, get_match_datetime, load_json
from utils.evaluation_rows import (
    capture_evaluation_row,
    get_all_evaluation_rows,
    get_evaluation_row,
)
from utils.prediction_snapshots import (
    capture_snapshot,
    current_model_version_for,
    get_snapshot,
    get_snapshots_for_match,
    resolve_model_version,
    snapshot_exists,
    snapshot_id_for,
)
from utils.settlements import get_settlement, get_settlements_for_match, settle_snapshot

FOOTBALL_1X2 = {"home_win": 1.90, "draw": 3.50, "away_win": 4.00}
BASKETBALL_ML = {"home_win": 1.80, "away_win": 2.00}

# 当前配置的默认版本（足球已按攻防分离提升；篮球保持不变）
FOOTBALL_CURRENT = config.MODEL_VERSIONS["football"]
BASKETBALL_CURRENT = config.MODEL_VERSIONS["basketball"]

# 身份哈希格式未变：sha256("<match_id>|prematch|baseline-1")
BASELINE_ID_FOR_M1 = "5b113d02c28ee76e968555395d912882dedb5164eb3851333d93a4ff426c042d"


def _bump(monkeypatch, sport: str, version: str) -> None:
    monkeypatch.setitem(config.MODEL_VERSIONS, sport, version)


def _basketball(make_match, match_id="m-bb", **overrides):
    overrides.setdefault("sport", "basketball")
    overrides.setdefault("league", "NBA")
    overrides.setdefault("home", "湖人")
    overrides.setdefault("away", "凯尔特人")
    overrides.setdefault("odds", dict(BASKETBALL_ML))
    return enrich_match(make_match(id=match_id, **overrides))


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


def _fake_sources(monkeypatch, upcoming) -> None:
    def fake_live_matches(sport):
        return [m for m in upcoming if m.get("sport") == "football"]

    def fake_live_basketball():
        return [m for m in upcoming if m.get("sport") == "basketball"]

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", fake_live_matches)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", fake_live_basketball)
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])


# ---------------------------------------------------------------------------
# 配置与解析
# ---------------------------------------------------------------------------

def test_default_current_versions(isolated_data_dir):
    assert config.MODEL_VERSIONS["football"] == "football-ad-1"
    assert config.MODEL_VERSIONS["basketball"] == "baseline-1"
    assert current_model_version_for("football") == "football-ad-1"
    assert current_model_version_for("basketball") == "baseline-1"
    # 全局兼容回退保持 baseline-1，未随足球版本变化
    assert config.MODEL_VERSION == "baseline-1"


def test_baseline_snapshot_id_is_unchanged(isolated_data_dir):
    """身份哈希输入格式未变，既有 match_id + baseline-1 的快照 ID 保持可复现。"""
    assert snapshot_id_for("m-1", "baseline-1") == BASELINE_ID_FOR_M1
    assert snapshot_id_for("m-1", "baseline-1", "prematch") == BASELINE_ID_FOR_M1


def test_resolver_precedence(isolated_data_dir):
    # 1) 显式版本始终优先
    assert resolve_model_version("manual-v7", sport="football") == "manual-v7"
    # 2) 运动当前版本
    assert resolve_model_version(None, sport="football") == FOOTBALL_CURRENT
    assert resolve_model_version(sport="basketball") == BASKETBALL_CURRENT
    # 3) 全局兼容回退
    assert resolve_model_version() == config.MODEL_VERSION
    assert resolve_model_version(None, None) == config.MODEL_VERSION


def test_resolver_unknown_sport_falls_back(isolated_data_dir):
    """未识别的运动沿用全局兼容回退，不臆造版本号、不新增报错。"""
    assert current_model_version_for("tennis") == config.MODEL_VERSION
    assert resolve_model_version(None, sport="tennis") == config.MODEL_VERSION
    assert resolve_model_version("explicit", sport="tennis") == "explicit"


# ---------------------------------------------------------------------------
# 运动专属版本
# ---------------------------------------------------------------------------

def test_football_only_version_change(isolated_data_dir, make_match, monkeypatch):
    _bump(monkeypatch, "football", "football-v2")

    football, _c1 = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball, _c2 = capture_snapshot(_basketball(make_match))

    assert football["model_version"] == "football-v2"
    assert basketball["model_version"] == "baseline-1"


def test_basketball_only_version_change(isolated_data_dir, make_match, monkeypatch):
    _bump(monkeypatch, "basketball", "basketball-v2")

    football, _c1 = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball, _c2 = capture_snapshot(_basketball(make_match))

    assert football["model_version"] == FOOTBALL_CURRENT
    assert basketball["model_version"] == "basketball-v2"


def test_explicit_override_wins(isolated_data_dir, make_match, monkeypatch):
    _bump(monkeypatch, "football", "football-v2")

    snapshot, created = capture_snapshot(
        enrich_match(make_match(id="m-fb")), model_version="historical-test-version"
    )

    assert created is True
    assert snapshot["model_version"] == "historical-test-version"


# ---------------------------------------------------------------------------
# 快照身份与幂等
# ---------------------------------------------------------------------------

def test_baseline_idempotency_unchanged(isolated_data_dir, make_match):
    first, created1 = capture_snapshot(enrich_match(make_match(id="m-1")))
    second, created2 = capture_snapshot(enrich_match(make_match(id="m-1")))

    assert created1 is True
    assert created2 is False
    assert first["model_version"] == FOOTBALL_CURRENT
    assert second["generated_at"] == first["generated_at"]
    assert len(get_snapshots_for_match("m-1")) == 1


def test_version_bump_creates_new_snapshot(isolated_data_dir, make_match, monkeypatch):
    first, created1 = capture_snapshot(enrich_match(make_match(id="m-1")))
    assert created1 is True
    original = dict(first)

    _bump(monkeypatch, "football", "football-v2")
    second, created2 = capture_snapshot(enrich_match(make_match(id="m-1")))

    assert created2 is True
    assert second["model_version"] == "football-v2"
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["match_id"] == first["match_id"]
    assert len(get_snapshots_for_match("m-1")) == 2
    # 旧快照逐字未变
    assert get_snapshot(first["snapshot_id"]) == original


def test_version_bump_does_not_affect_other_sport(isolated_data_dir, make_match, monkeypatch):
    """足球版本提升不得影响篮球：篮球既不产生新快照，也不被改版本。"""
    football, fc = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball, bc = capture_snapshot(_basketball(make_match))
    assert fc is True and bc is True
    assert football["model_version"] == FOOTBALL_CURRENT
    assert basketball["model_version"] == BASKETBALL_CURRENT

    _bump(monkeypatch, "football", "football-v2")

    football2, fc2 = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball2, bc2 = capture_snapshot(_basketball(make_match))

    assert fc2 is True
    assert football2["model_version"] == "football-v2"
    assert bc2 is False
    assert basketball2["snapshot_id"] == basketball["snapshot_id"]
    assert basketball2["model_version"] == BASKETBALL_CURRENT
    assert len(get_snapshots_for_match("m-fb")) == 2
    assert len(get_snapshots_for_match("m-bb")) == 1


@pytest.mark.parametrize("status", ["live", "finished"])
def test_started_match_version_bump_rejected(isolated_data_dir, make_match, monkeypatch, status):
    match = enrich_match(make_match(id="m-1"))
    snapshot, created = capture_snapshot(match)
    assert created is True

    _bump(monkeypatch, "football", "football-v2")
    kickoff = get_match_datetime(match)
    later = kickoff + timedelta(minutes=10 if status == "live" else 200)
    score = {"ft": [2, 1]} if status == "finished" else None

    result, created2 = capture_snapshot(dict(match, status=status, score=score), now=later)

    assert created2 is False
    assert result is None
    assert len(get_snapshots_for_match("m-1")) == 1
    assert get_snapshot(snapshot["snapshot_id"]) is not None


def test_snapshot_exists_semantics(isolated_data_dir, make_match, monkeypatch):
    capture_snapshot(enrich_match(make_match(id="m-1")))
    capture_snapshot(enrich_match(make_match(id="m-legacy")), model_version="baseline-1")

    # 显式版本
    assert snapshot_exists("m-1", FOOTBALL_CURRENT) is True
    assert snapshot_exists("m-1", "baseline-1") is False
    # 运动感知默认
    assert snapshot_exists("m-1", sport="football") is True
    # 无运动上下文 -> 全局兼容回退（baseline-1）
    assert snapshot_exists("m-1") is False
    assert snapshot_exists("m-legacy") is True

    _bump(monkeypatch, "football", "football-v2")

    assert snapshot_exists("m-1", sport="football") is False   # football-v2 尚未创建
    assert snapshot_exists("m-1", FOOTBALL_CURRENT) is True    # 原版本快照仍在
    assert snapshot_exists("m-legacy") is True                 # 全局回退仍指向 baseline-1


# ---------------------------------------------------------------------------
# 抓取器集成
# ---------------------------------------------------------------------------

def test_scraper_version_bump_metric(isolated_data_dir, monkeypatch):
    """两场有盘口比赛 -> 2；仅提升足球版本后 -> 1（篮球不重复创建）。"""
    _fake_sources(monkeypatch, [
        _match("m-fb"),
        _match("m-bb", sport="basketball", league="NBA", home="湖人", away="凯尔特人",
               odds=dict(BASKETBALL_ML)),
    ])

    first = scraper.refresh(verbose=False)
    assert first["prediction_snapshots_added"] == 2

    _bump(monkeypatch, "football", "football-v2")

    second = scraper.refresh(verbose=False)
    assert second["prediction_snapshots_added"] == 1
    assert len(get_snapshots_for_match("m-fb")) == 2
    assert len(get_snapshots_for_match("m-bb")) == 1

    versions = sorted(s["model_version"] for s in get_snapshots_for_match("m-fb"))
    assert versions == sorted([FOOTBALL_CURRENT, "football-v2"])


def test_market_admission_not_bypassed_by_version_change(isolated_data_dir, monkeypatch):
    """版本变化不得绕过市场准入：无盘口全球比赛仍不进入 canonical 集合。"""
    _bump(monkeypatch, "football", "football-v2")
    _fake_sources(monkeypatch, [_match("m-unpriced", odds=None)])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert load_json("daily_matches.json")["matches"] == []
    assert get_snapshots_for_match("m-unpriced") == []


# ---------------------------------------------------------------------------
# 下游兼容
# ---------------------------------------------------------------------------

def _two_version_artifacts(make_match, match_id="m-multi"):
    """为同一场足球比赛创建两个合法赛前快照并结算、物化评估样本。"""
    match = enrich_match(make_match(id=match_id))
    baseline, c1 = capture_snapshot(match, model_version="baseline-1")
    upgraded, c2 = capture_snapshot(match, model_version="football-v2")
    assert c1 is True and c2 is True

    finished = dict(match, status="finished", date="2020-01-01", time="20:00",
                    score={"ft": [2, 1]})
    settle_a, sa = settle_snapshot(baseline, finished)
    settle_b, sb = settle_snapshot(upgraded, finished)
    assert sa is True and sb is True

    row_a, ra = capture_evaluation_row(baseline, settle_a)
    row_b, rb = capture_evaluation_row(upgraded, settle_b)
    assert ra is True and rb is True
    return match, (baseline, settle_a, row_a), (upgraded, settle_b, row_b)


def test_multi_version_settlement(isolated_data_dir, make_match):
    match, (baseline, settle_a, _row_a), (upgraded, settle_b, _row_b) = _two_version_artifacts(make_match)

    settlements = get_settlements_for_match(match["id"])
    assert len(settlements) == 2
    assert {s["model_version"] for s in settlements} == {"baseline-1", "football-v2"}
    assert {s["snapshot_id"] for s in settlements} == {baseline["snapshot_id"], upgraded["snapshot_id"]}
    assert all(s["final_score"] == {"home": 2, "away": 1} for s in settlements)


def test_multi_version_evaluation_rows(isolated_data_dir, make_match):
    match, (_b, _sa, row_a), (_u, _sb, row_b) = _two_version_artifacts(make_match, match_id="m-multi-eval")

    rows = get_all_evaluation_rows()
    assert len(rows) == 2
    assert {r["model_version"] for r in rows} == {"baseline-1", "football-v2"}
    assert {r["evaluation_id"] for r in rows} == {row_a["evaluation_id"], row_b["evaluation_id"]}
    assert all(r["match_id"] == match["id"] for r in rows)


def test_analytics_grouping_separates_versions(isolated_data_dir, make_match):
    _two_version_artifacts(make_match, match_id="m-multi-group")
    rows = get_all_evaluation_rows()

    classification = build_classification_summaries(rows)
    assert sorted(s["model_version"] for s in classification) == ["baseline-1", "football-v2"]
    assert all(s["sample_count"] == 1 for s in classification)

    calibration = build_calibration_summaries(rows)
    assert sorted(s["model_version"] for s in calibration) == ["baseline-1", "football-v2"]


def test_historical_artifacts_preserved_after_version_change(isolated_data_dir, make_match, monkeypatch):
    match = enrich_match(make_match(id="m-hist"))
    snapshot, _c = capture_snapshot(match)
    settlement, _s = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                       score={"ft": [2, 1]})
    )
    row, _r = capture_evaluation_row(snapshot, settlement)

    before_snapshot = dict(snapshot)
    before_settlement = dict(settlement)
    before_row = dict(row)

    _bump(monkeypatch, "football", "football-v2")

    assert get_snapshot(snapshot["snapshot_id"]) == before_snapshot
    assert get_settlement(settlement["settlement_id"]) == before_settlement
    assert get_evaluation_row(row["evaluation_id"]) == before_row
    assert get_snapshots_for_match("m-hist") == [before_snapshot]
