"""
篮球主场优势 / 预期分差语义测试
================================
`basketball_home_advantage` 的单位是**篮球比分差（points）**：
默认 `2.5` 必须真正产生 +2.5 的预期主客分差，而不是约 0.62。

同时必须保持：
    basketball_elo_scale = 200  ->  200 分 Elo 差 ≈ 10 分预期分差
    expected_home_points + expected_away_points = basketball_base_total
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import config
from models.basketball_model import expected_points, predict_basketball
from models.predictor import predict_match
from utils.calibration_evaluation import build_calibration_summaries
from utils.classification_evaluation import build_classification_summaries
from utils.daily_loader import enrich_match
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.temporal_evaluation import build_temporal_summaries

CURRENT_BASKETBALL_VERSION = "basketball-margin-1"
PREVIOUS_BASKETBALL_VERSION = "basketball-modelprob-1"
OLDEST_BASKETBALL_VERSION = "basketball-coldstart-1"

BEIJING_TZ = timezone(timedelta(hours=8))

TOL = 1e-9


def _team(name: str, elo: float) -> dict:
    return {
        "name": name,
        "elo_rating": elo,
        "matches_played": 20,
        "wins": 10,
        "goals_for": 2150,
        "goals_against": 2150,
    }


HOME_1700 = _team("湖人", 1700)
AWAY_1700 = _team("凯尔特人", 1700)
HOME_1900 = _team("湖人", 1900)
HOME_1500 = _team("湖人", 1500)

ODDS_HOME_FAV = {"home_win": 1.40, "away_win": 3.10}
ODDS_AWAY_FAV = {"home_win": 3.10, "away_win": 1.40}


def _pair_margin(home_elo: float, away_elo: float, base_total: float = 215.0,
                 pace: float = 1.0) -> tuple[float, float, float]:
    home = expected_points(home_elo, away_elo, base_total, True, pace)
    away = expected_points(away_elo, home_elo, base_total, False, pace)
    return home, away, home - away


def _predicted_margin(prediction: dict) -> float:
    basket = prediction["basketball"]
    return basket["expected_home_points"] - basket["expected_away_points"]


# ---------------------------------------------------------------------------
# 纯函数：expected_points
# ---------------------------------------------------------------------------

def test_equal_elo_pair_points_and_margin():
    home, away, margin = _pair_margin(1700, 1700)

    assert abs(home - 108.75) < TOL
    assert abs(away - 106.25) < TOL
    assert abs(home + away - 215.0) < TOL
    assert abs(margin - 2.5) < TOL


def test_plus_200_elo_margin():
    home, away, margin = _pair_margin(1900, 1700)

    assert abs(margin - 12.5) < TOL
    assert abs(home + away - 215.0) < TOL


def test_minus_200_elo_margin():
    home, away, margin = _pair_margin(1500, 1700)

    assert abs(margin - (-7.5)) < TOL
    assert abs(home + away - 215.0) < TOL


def test_zero_home_advantage(monkeypatch):
    monkeypatch.setitem(config.MODEL_CONFIG, "basketball_home_advantage", 0)

    home, away, margin = _pair_margin(1700, 1700)

    assert abs(margin) < TOL
    assert abs(home - 107.5) < TOL
    assert abs(away - 107.5) < TOL
    assert abs(home + away - 215.0) < TOL


def test_custom_home_advantage_is_direct_points(monkeypatch):
    monkeypatch.setitem(config.MODEL_CONFIG, "basketball_home_advantage", 6.0)

    home, away, margin = _pair_margin(1700, 1700)

    assert abs(margin - 6.0) < TOL
    assert abs(home - 110.5) < TOL
    assert abs(away - 104.5) < TOL


def test_elo_scale_still_means_10_points_per_200(monkeypatch):
    monkeypatch.setitem(config.MODEL_CONFIG, "basketball_home_advantage", 0)

    home, away, margin = _pair_margin(1900, 1700)

    assert abs(margin - 10.0) < TOL
    assert config.MODEL_CONFIG["basketball_elo_scale"] == 200.0


# ---------------------------------------------------------------------------
# 集成预测
# ---------------------------------------------------------------------------

def test_integrated_equal_elo():
    pred = predict_basketball(HOME_1700, AWAY_1700, odds=None, league="NBA")
    basket = pred["basketball"]

    assert abs(basket["spread"] - (-2.5)) < TOL
    assert abs(basket["expected_total"] - 215.0) < TOL
    assert abs(_predicted_margin(pred) - 2.5) <= 0.15

    model = pred["model_probabilities"]
    assert model["draw"] == 0
    assert model["home_win"] > 50
    assert model["away_win"] < 50
    assert model["home_win"] > model["away_win"]


def test_integrated_plus_200_elo():
    pred = predict_basketball(HOME_1900, AWAY_1700, odds=None, league="NBA")

    assert abs(pred["basketball"]["spread"] - (-12.5)) < TOL
    assert abs(pred["basketball"]["expected_total"] - 215.0) < TOL
    assert abs(_predicted_margin(pred) - 12.5) <= 0.15


def test_integrated_minus_200_elo():
    pred = predict_basketball(HOME_1500, AWAY_1700, odds=None, league="NBA")

    assert abs(pred["basketball"]["spread"] - 7.5) < TOL
    assert abs(pred["basketball"]["expected_total"] - 215.0) < TOL
    assert abs(_predicted_margin(pred) - (-7.5)) <= 0.15


def test_total_line_margin_invariance():
    """总分线改变总分，但不得改变主客预期分差。"""
    low = predict_basketball(
        HOME_1700, AWAY_1700,
        odds={"home_win": 1.9, "away_win": 1.9, "total_line": 210.5}, league="NBA",
    )
    high = predict_basketball(
        HOME_1700, AWAY_1700,
        odds={"home_win": 1.9, "away_win": 1.9, "total_line": 230.5}, league="NBA",
    )

    assert low["basketball"]["over_line"] == 210.5
    assert high["basketball"]["over_line"] == 230.5
    assert abs(low["basketball"]["expected_total"] - 210.5) < TOL
    assert abs(high["basketball"]["expected_total"] - 230.5) < TOL
    assert low["basketball"]["expected_total"] != high["basketball"]["expected_total"]

    assert abs(low["basketball"]["spread"] - (-2.5)) < TOL
    assert abs(high["basketball"]["spread"] - (-2.5)) < TOL
    assert abs(_predicted_margin(low) - 2.5) <= 0.15
    assert abs(_predicted_margin(high) - 2.5) <= 0.15


def test_cba_baseline_and_margin():
    pred = predict_basketball(HOME_1700, AWAY_1700, odds=None, league="CBA")

    assert abs(pred["basketball"]["expected_total"] - 205.0) < TOL
    assert abs(pred["basketball"]["spread"] - (-2.5)) < TOL


def test_over_under_still_consistent():
    odds = {"home_win": 1.9, "away_win": 1.9, "total_line": 220.5,
            "over": 1.90, "under": 1.90}
    pred = predict_basketball(HOME_1700, AWAY_1700, odds=odds, league="NBA")
    basket = pred["basketball"]

    assert basket["over_line"] == 220.5
    assert 0.0 <= basket["over_prob"] <= 100.0
    assert abs(basket["over_prob"] + basket["under_prob"] - 100.0) <= 0.2
    assert abs(pred["basketball"]["spread"] - (-2.5)) < TOL


# ---------------------------------------------------------------------------
# 概率语义回归（上一任务冻结的契约）
# ---------------------------------------------------------------------------

def test_moneyline_does_not_change_model_probabilities():
    a = predict_basketball(HOME_1700, AWAY_1700, odds=None, league="NBA")
    b = predict_basketball(HOME_1700, AWAY_1700, odds=ODDS_HOME_FAV, league="NBA")
    c = predict_basketball(HOME_1700, AWAY_1700, odds=ODDS_AWAY_FAV, league="NBA")

    assert a["model_probabilities"] == b["model_probabilities"]
    assert a["model_probabilities"] == c["model_probabilities"]
    assert b["probabilities"] != c["probabilities"]


def test_ev_kelly_still_use_model_probability():
    odds = ODDS_AWAY_FAV
    pred = predict_basketball(HOME_1700, AWAY_1700, odds=odds, league="NBA")

    ho = float(odds["home_win"])
    model_home = (pred["ev_analysis"]["home"]["ev"] / 100 + 1) / ho
    assert abs(model_home - pred["model_probabilities"]["home_win"] / 100) <= 0.005

    display_home = pred["probabilities"]["home_win"] / 100
    assert abs(model_home - display_home) > 0.01

    frac = config.MODEL_CONFIG["kelly_fraction"]
    b = ho - 1.0
    expected_kelly = max(0.0, ((model_home * b - (1 - model_home)) / b) * frac) * 100
    assert abs(pred["ev_analysis"]["home"]["kelly_pct"] - expected_kelly) <= 0.05
    assert frac == 0.25
    assert config.MODEL_CONFIG["value_threshold"] == 0.05


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

def test_basketball_current_version():
    assert config.MODEL_VERSIONS["basketball"] == CURRENT_BASKETBALL_VERSION


def test_football_version_unchanged():
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"


def test_global_fallback_unchanged():
    assert config.MODEL_VERSION == "baseline-1"


def test_historical_versions_preserved():
    assert CURRENT_BASKETBALL_VERSION != PREVIOUS_BASKETBALL_VERSION
    assert PREVIOUS_BASKETBALL_VERSION != OLDEST_BASKETBALL_VERSION


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def _basketball_match(make_match, **overrides):
    kwargs = dict(
        id="500lq-2030-01-01-7",
        sport="basketball",
        league="NBA",
        home="湖人",
        away="凯尔特人",
        odds={"home_win": 3.10, "away_win": 1.40},
    )
    kwargs.update(overrides)
    return make_match(**kwargs)


def _predict_enriched(match: dict) -> dict:
    return predict_match(
        match["home_team"], match["away_team"], odds=match.get("odds"),
        sport=match.get("sport", "football"), league=match.get("league", ""),
    )


def test_snapshot_uses_current_version(isolated_data_dir, make_match):
    m = enrich_match(_basketball_match(make_match))
    prediction = _predict_enriched(m)
    snap, created = capture_snapshot(m, prediction)

    assert created is True
    assert snap["model_version"] == CURRENT_BASKETBALL_VERSION
    assert snap["model_probabilities"] == prediction["model_probabilities"]
    assert snap["display_probabilities"] == prediction["probabilities"]


def test_historical_snapshot_immutability(isolated_data_dir, make_match):
    m = enrich_match(_basketball_match(make_match))
    prediction = _predict_enriched(m)

    old, old_created = capture_snapshot(
        m, prediction, model_version=PREVIOUS_BASKETBALL_VERSION
    )
    assert old_created is True
    old_copy = copy.deepcopy(old)

    new, new_created = capture_snapshot(m, prediction)
    assert new_created is True
    assert new["model_version"] == CURRENT_BASKETBALL_VERSION

    snapshots = get_snapshots_for_match(m["id"])
    assert len(snapshots) == 2
    preserved = [
        s for s in snapshots if s["model_version"] == PREVIOUS_BASKETBALL_VERSION
    ]
    assert len(preserved) == 1
    assert preserved[0] == old_copy


def test_started_match_not_backfilled(isolated_data_dir, make_match):
    now = datetime(2030, 5, 1, 20, 30, tzinfo=BEIJING_TZ)

    live = enrich_match(
        _basketball_match(make_match, id="bm-live", status="live",
                          date="2030-05-01", time="20:00"),
        now,
    )
    snap, created = capture_snapshot(live, now=now)
    assert snap is None
    assert created is False
    assert get_snapshots_for_match("bm-live") == []

    finished = enrich_match(
        _basketball_match(make_match, id="bm-fin", status="finished",
                          date="2030-04-01", time="20:00",
                          score={"ft": [110, 104]}),
        now,
    )
    snap2, created2 = capture_snapshot(finished, now=now)
    assert snap2 is None
    assert created2 is False
    assert get_snapshots_for_match("bm-fin") == []


# ---------------------------------------------------------------------------
# 评估版本隔离
# ---------------------------------------------------------------------------

def _eval_row(eid: str, version: str, kickoff: str, outcome: str) -> dict:
    return {
        "evaluation_id": eid,
        "match_id": f"m-{eid}",
        "sport": "basketball",
        "model_name": "elo-normal-points",
        "model_version": version,
        "kickoff_at": kickoff,
        "model_probabilities": {"home_win": 56, "away_win": 44},
        "actual_outcome": outcome,
    }


def _history_and_current_rows():
    return [
        _eval_row("prev-1", PREVIOUS_BASKETBALL_VERSION,
                  "2030-01-01T00:00:00+00:00", "home_win"),
        _eval_row("prev-2", PREVIOUS_BASKETBALL_VERSION,
                  "2030-02-01T00:00:00+00:00", "away_win"),
        _eval_row("cur-1", CURRENT_BASKETBALL_VERSION,
                  "2030-05-01T00:00:00+00:00", "home_win"),
        _eval_row("cur-2", CURRENT_BASKETBALL_VERSION,
                  "2030-05-20T00:00:00+00:00", "home_win"),
        _eval_row("cur-3", CURRENT_BASKETBALL_VERSION,
                  "2030-06-01T00:00:00+00:00", "away_win"),
    ]


def test_classification_version_isolation(isolated_data_dir):
    by_version = {
        s["model_version"]: s
        for s in build_classification_summaries(_history_and_current_rows())
    }
    assert set(by_version) == {PREVIOUS_BASKETBALL_VERSION, CURRENT_BASKETBALL_VERSION}
    assert by_version[PREVIOUS_BASKETBALL_VERSION]["sample_count"] == 2
    assert by_version[CURRENT_BASKETBALL_VERSION]["sample_count"] == 3


def test_calibration_version_isolation(isolated_data_dir):
    by_version = {
        s["model_version"]: s
        for s in build_calibration_summaries(_history_and_current_rows())
    }
    assert set(by_version) == {PREVIOUS_BASKETBALL_VERSION, CURRENT_BASKETBALL_VERSION}
    assert by_version[PREVIOUS_BASKETBALL_VERSION]["sample_count"] == 2
    assert by_version[CURRENT_BASKETBALL_VERSION]["sample_count"] == 3
    assert set(by_version[CURRENT_BASKETBALL_VERSION]["classes"]) == {
        "home_win", "away_win",
    }


def test_temporal_version_isolation(isolated_data_dir):
    by_version = {
        s["model_version"]: s
        for s in build_temporal_summaries(_history_and_current_rows())
    }
    assert set(by_version) == {PREVIOUS_BASKETBALL_VERSION, CURRENT_BASKETBALL_VERSION}

    previous = by_version[PREVIOUS_BASKETBALL_VERSION]
    current = by_version[CURRENT_BASKETBALL_VERSION]

    assert previous["reference_kickoff_at"] == "2030-02-01T00:00:00+00:00"
    assert current["reference_kickoff_at"] == "2030-06-01T00:00:00+00:00"
    assert previous["all_time"]["sample_count"] == 2
    assert current["all_time"]["sample_count"] == 3
    assert [w["window_days"] for w in previous["windows"]] == [30, 90, 180]
    assert [w["window_days"] for w in current["windows"]] == [30, 90, 180]


# ---------------------------------------------------------------------------
# 不可变
# ---------------------------------------------------------------------------

def test_expected_points_and_prediction_do_not_mutate_inputs():
    home = copy.deepcopy(HOME_1700)
    away = copy.deepcopy(AWAY_1700)
    odds = copy.deepcopy(ODDS_AWAY_FAV)
    config_before = copy.deepcopy(config.MODEL_CONFIG)

    expected_points(1700, 1700, 215.0, True, 1.0)
    predict_basketball(home, away, odds, "NBA")

    assert home == HOME_1700
    assert away == AWAY_1700
    assert odds == ODDS_AWAY_FAV
    assert config.MODEL_CONFIG == config_before
