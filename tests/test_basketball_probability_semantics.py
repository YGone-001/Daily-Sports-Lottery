"""
篮球概率语义测试
================
篮球 `model_probabilities` 必须与足球保持同一语义：

    model_probabilities = 主客独赢盘融合**前**的模型胜负概率
    probabilities       = 融合**后**的展示概率
    EV / Kelly          = 用融合前的模型概率与市场价格比较

同时覆盖：快照语义分离、历史快照不可变、开赛计时门禁、
新旧篮球模型版本的评估隔离、输入与配置不可变。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import config
from models.basketball_model import predict_basketball
from models.predictor import predict_football, predict_match
from utils.calibration_evaluation import build_calibration_summaries
from utils.classification_evaluation import build_classification_summaries
from utils.daily_loader import enrich_match
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.temporal_evaluation import build_temporal_summaries

OLD_BASKETBALL_VERSION = "basketball-coldstart-1"
PREVIOUS_BASKETBALL_VERSION = "basketball-modelprob-1"
NEW_BASKETBALL_VERSION = "basketball-margin-1"

# 确定性球队档案（不使用运行时实力库）
HOME = {
    "name": "湖人",
    "elo_rating": 1780,
    "matches_played": 20,
    "wins": 14,
    "goals_for": 2200,
    "goals_against": 2100,
}
AWAY = {
    "name": "凯尔特人",
    "elo_rating": 1740,
    "matches_played": 20,
    "wins": 12,
    "goals_for": 2150,
    "goals_against": 2130,
}

ODDS_HOME_FAV = {"home_win": 1.40, "away_win": 3.10}
ODDS_AWAY_FAV = {"home_win": 3.10, "away_win": 1.40}
ODDS_NEAR_THRESHOLD = {"home_win": 1.75, "away_win": 2.03}

BEIJING_TZ = timezone(timedelta(hours=8))


def _market_home(odds: dict) -> float:
    imp_h = 1.0 / float(odds["home_win"])
    imp_a = 1.0 / float(odds["away_win"])
    return imp_h / (imp_h + imp_a)


# ---------------------------------------------------------------------------
# 基础语义
# ---------------------------------------------------------------------------

def test_no_odds_display_equals_model():
    pred = predict_basketball(HOME, AWAY, odds=None, league="NBA")

    assert pred["probabilities"] == pred["model_probabilities"]
    assert pred["probabilities"]["draw"] == 0
    assert pred["model_probabilities"]["draw"] == 0
    assert pred["market"]["implied_probabilities"] == {
        "home_win": None,
        "draw": None,
        "away_win": None,
    }
    assert pred["market"]["weight"] == 0


def test_moneyline_does_not_alter_model_probability():
    """强制：融合不得改变 model_probabilities。"""
    without = predict_basketball(HOME, AWAY, odds=None, league="NBA")
    with_market = predict_basketball(HOME, AWAY, odds=ODDS_AWAY_FAV, league="NBA")

    assert with_market["model_probabilities"] == without["model_probabilities"]


def test_different_moneyline_prices_do_not_alter_model_probability():
    a = predict_basketball(HOME, AWAY, odds=ODDS_HOME_FAV, league="NBA")
    b = predict_basketball(HOME, AWAY, odds=ODDS_AWAY_FAV, league="NBA")

    assert a["model_probabilities"] == b["model_probabilities"]
    assert a["probabilities"] != b["probabilities"]


def test_display_differs_from_model_when_market_differs():
    pred = predict_basketball(HOME, AWAY, odds=ODDS_AWAY_FAV, league="NBA")

    assert pred["probabilities"] != pred["model_probabilities"]


# ---------------------------------------------------------------------------
# 融合公式与归一化
# ---------------------------------------------------------------------------

def test_display_blend_formula(monkeypatch):
    weight = 0.5
    monkeypatch.setitem(config.MODEL_CONFIG, "odds_weight", weight)

    pred = predict_basketball(HOME, AWAY, odds=ODDS_AWAY_FAV, league="NBA")

    model = pred["model_probabilities"]
    market = pred["market"]["implied_probabilities"]
    display = pred["probabilities"]

    expected_home = (model["home_win"] / 100) * (1 - weight) + (market["home_win"] / 100) * weight
    expected_away = (model["away_win"] / 100) * (1 - weight) + (market["away_win"] / 100) * weight

    assert abs(display["home_win"] / 100 - expected_home) <= 0.011
    assert abs(display["away_win"] / 100 - expected_away) <= 0.011

    # 归一化保持：百分点和仍落在既有容许区间
    assert 99 <= display["home_win"] + display["away_win"] <= 101


def test_zero_weight_display_equals_model(monkeypatch):
    monkeypatch.setitem(config.MODEL_CONFIG, "odds_weight", 0)

    pred = predict_basketball(HOME, AWAY, odds=ODDS_AWAY_FAV, league="NBA")

    assert pred["probabilities"] == pred["model_probabilities"]
    # 市场隐含概率仍然暴露
    assert pred["market"]["implied_probabilities"]["home_win"] is not None
    assert pred["market"]["implied_probabilities"]["away_win"] is not None


def test_model_probability_sum():
    pred = predict_basketball(HOME, AWAY, odds=ODDS_HOME_FAV, league="NBA")
    mp = pred["model_probabilities"]

    assert mp["draw"] == 0
    assert 99 <= mp["home_win"] + mp["away_win"] <= 101

    # 融合前内部二元概率和为 1：用 EV 反推模型概率并核对
    ho = float(ODDS_HOME_FAV["home_win"])
    ao = float(ODDS_HOME_FAV["away_win"])
    model_home = (pred["ev_analysis"]["home"]["ev"] / 100 + 1) / ho
    model_away = (pred["ev_analysis"]["away"]["ev"] / 100 + 1) / ao
    assert abs((model_home + model_away) - 1.0) <= 0.005


def test_market_probability_sum():
    pred = predict_basketball(HOME, AWAY, odds=ODDS_HOME_FAV, league="NBA")
    implied = pred["market"]["implied_probabilities"]

    assert implied["draw"] is None
    assert 99 <= implied["home_win"] + implied["away_win"] <= 101
    assert abs(_market_home(ODDS_HOME_FAV) - implied["home_win"] / 100) <= 0.005


# ---------------------------------------------------------------------------
# EV / Kelly / 价值判定
# ---------------------------------------------------------------------------

def test_ev_uses_model_probability():
    """强制：EV 必须来自融合前的模型概率，而不是展示概率。"""
    odds = ODDS_AWAY_FAV
    pred = predict_basketball(HOME, AWAY, odds=odds, league="NBA")

    ho = float(odds["home_win"])
    ao = float(odds["away_win"])

    model_home = (pred["ev_analysis"]["home"]["ev"] / 100 + 1) / ho
    model_away = (pred["ev_analysis"]["away"]["ev"] / 100 + 1) / ao

    assert abs(model_home - pred["model_probabilities"]["home_win"] / 100) <= 0.005
    assert abs(model_away - pred["model_probabilities"]["away_win"] / 100) <= 0.005

    # 与「展示概率 EV」明确不同（该场景下市场与模型差异显著）
    display_home = pred["probabilities"]["home_win"] / 100
    assert abs(model_home - display_home) > 0.02


def test_kelly_uses_model_probability():
    odds = ODDS_AWAY_FAV
    pred = predict_basketball(HOME, AWAY, odds=odds, league="NBA")

    ho = float(odds["home_win"])
    frac = config.MODEL_CONFIG["kelly_fraction"]

    model_home = (pred["ev_analysis"]["home"]["ev"] / 100 + 1) / ho
    b = ho - 1.0
    q = 1.0 - model_home
    expected = max(0.0, ((model_home * b - q) / b) * frac) * 100

    assert abs(pred["ev_analysis"]["home"]["kelly_pct"] - expected) <= 0.05

    display_home = pred["probabilities"]["home_win"] / 100
    q_display = 1.0 - display_home
    kelly_from_display = max(0.0, ((display_home * b - q_display) / b) * frac) * 100
    assert abs(kelly_from_display - pred["ev_analysis"]["home"]["kelly_pct"]) > 0.3


def test_value_threshold_uses_model_ev():
    thr = config.MODEL_CONFIG["value_threshold"]
    odds = ODDS_NEAR_THRESHOLD
    ho = float(odds["home_win"])

    pred = predict_basketball(HOME, AWAY, odds=odds, league="NBA")

    model_home = (pred["ev_analysis"]["home"]["ev"] / 100 + 1) / ho
    model_ev = model_home * ho - 1
    display_home = pred["probabilities"]["home_win"] / 100
    display_ev = display_home * ho - 1

    # 该场景下模型 EV 与展示 EV 跨越阈值的方向不同：必须跟随模型 EV
    assert model_ev > thr
    assert display_ev < thr
    assert pred["ev_analysis"]["home"]["is_value"] is True

    # 阈值配置没有被改动
    assert thr == 0.05


# ---------------------------------------------------------------------------
# 未改动的子系统
# ---------------------------------------------------------------------------

def test_total_line_semantics_unchanged():
    no_line = predict_basketball(HOME, AWAY, odds={"home_win": 1.80, "away_win": 2.00},
                                 league="NBA")
    with_line = predict_basketball(
        HOME, AWAY,
        odds={"home_win": 1.80, "away_win": 2.00, "total_line": 225.5},
        league="NBA",
    )

    assert no_line["basketball"]["over_line"] != 225.5
    assert with_line["basketball"]["over_line"] == 225.5
    assert round(with_line["basketball"]["expected_total"], 1) == 225.5
    assert with_line["basketball"]["expected_home_points"] > 0
    assert with_line["basketball"]["expected_away_points"] > 0
    # 总分线不参与胜负概率
    assert with_line["model_probabilities"] == no_line["model_probabilities"]


def test_over_under_semantics_unchanged():
    odds = {"home_win": 1.80, "away_win": 2.00, "total_line": 220.5,
            "over": 1.90, "under": 1.90}
    pred = predict_basketball(HOME, AWAY, odds=odds, league="NBA")
    basket = pred["basketball"]

    assert basket["over_line"] == 220.5
    assert 0.0 <= basket["over_prob"] <= 100.0
    assert 0.0 <= basket["under_prob"] <= 100.0
    assert abs(basket["over_prob"] + basket["under_prob"] - 100.0) <= 0.2
    assert pred["goals_prediction"]["over_2_5"] == round(basket["over_prob"] / 100, 3)


def test_football_semantics_regression():
    home = {"name": "皇马", "elo_rating": 1950, "attack_rating": 0.78,
            "defense_rating": 0.72}
    away = {"name": "巴萨", "elo_rating": 1620, "attack_rating": 0.45,
            "defense_rating": 0.45}
    odds = {"home_win": 3.60, "draw": 3.50, "away_win": 1.85}

    pred = predict_football(home, away, odds)

    # 足球：model_probabilities 未被融合，probabilities 为融合后展示值
    assert pred["model_probabilities"] != pred["probabilities"]

    weight = config.MODEL_CONFIG["odds_weight"]
    model = pred["model_probabilities"]
    implied = pred["market"]["implied_probabilities"]
    for key in ("home_win", "draw", "away_win"):
        blended = model[key] * (1 - weight) + implied[key] * weight
        assert abs(pred["probabilities"][key] - blended) <= 1.0


# ---------------------------------------------------------------------------
# 模型版本
# ---------------------------------------------------------------------------

def test_basketball_current_version():
    assert config.MODEL_VERSIONS["basketball"] == NEW_BASKETBALL_VERSION


def test_football_version_unchanged():
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"


def test_global_fallback_unchanged():
    assert config.MODEL_VERSION == "baseline-1"


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def _basketball_match(make_match, **overrides):
    kwargs = dict(
        id="500lq-2030-01-01-9",
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
        match["home_team"],
        match["away_team"],
        odds=match.get("odds"),
        sport=match.get("sport", "football"),
        league=match.get("league", ""),
    )


def test_snapshot_semantic_separation(isolated_data_dir, make_match):
    m = enrich_match(_basketball_match(make_match))
    prediction = _predict_enriched(m)
    snap, created = capture_snapshot(m, prediction)

    assert created is True
    assert snap["model_version"] == NEW_BASKETBALL_VERSION
    assert snap["model_probabilities"] == prediction["model_probabilities"]
    assert snap["display_probabilities"] == prediction["probabilities"]
    assert snap["model_probabilities"] != snap["display_probabilities"]


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
    assert new["model_version"] == NEW_BASKETBALL_VERSION

    snapshots = get_snapshots_for_match(m["id"])
    assert len(snapshots) == 2
    preserved = [
        s for s in snapshots if s["model_version"] == PREVIOUS_BASKETBALL_VERSION
    ]
    assert len(preserved) == 1
    assert preserved[0] == old_copy


def test_started_match_not_backfilled(isolated_data_dir, make_match):
    """进行中 / 已完赛的比赛不得被追溯补建新版本快照（时序门禁权威）。"""
    now = datetime(2030, 5, 1, 20, 30, tzinfo=BEIJING_TZ)

    live = enrich_match(
        _basketball_match(
            make_match, id="bb-live", status="live",
            date="2030-05-01", time="20:00",
        ),
        now,
    )
    snap, created = capture_snapshot(live, now=now)
    assert snap is None
    assert created is False
    assert get_snapshots_for_match("bb-live") == []

    finished = enrich_match(
        _basketball_match(
            make_match, id="bb-fin", status="finished",
            date="2030-04-01", time="20:00", score={"ft": [108, 101]},
        ),
        now,
    )
    snap2, created2 = capture_snapshot(finished, now=now)
    assert snap2 is None
    assert created2 is False
    assert get_snapshots_for_match("bb-fin") == []


# ---------------------------------------------------------------------------
# 评估版本隔离
# ---------------------------------------------------------------------------

def _basketball_eval_row(eid, version, kickoff, outcome, prob=(60, 40)):
    home_win, away_win = prob
    return {
        "evaluation_id": eid,
        "match_id": f"m-{eid}",
        "sport": "basketball",
        "model_name": "elo-normal-points",
        "model_version": version,
        "kickoff_at": kickoff,
        "model_probabilities": {"home_win": home_win, "away_win": away_win},
        "actual_outcome": outcome,
    }


def _two_version_rows():
    return [
        _basketball_eval_row("old-1", OLD_BASKETBALL_VERSION,
                             "2030-01-01T00:00:00+00:00", "home_win", (60, 40)),
        _basketball_eval_row("old-2", OLD_BASKETBALL_VERSION,
                             "2030-02-01T00:00:00+00:00", "away_win", (55, 45)),
        _basketball_eval_row("new-1", NEW_BASKETBALL_VERSION,
                             "2030-05-01T00:00:00+00:00", "home_win", (70, 30)),
        _basketball_eval_row("new-2", NEW_BASKETBALL_VERSION,
                             "2030-05-20T00:00:00+00:00", "home_win", (65, 35)),
        _basketball_eval_row("new-3", NEW_BASKETBALL_VERSION,
                             "2030-06-01T00:00:00+00:00", "away_win", (50, 50)),
    ]


def test_classification_version_isolation(isolated_data_dir):
    summaries = build_classification_summaries(_two_version_rows())
    by_version = {s["model_version"]: s for s in summaries}

    assert set(by_version) == {OLD_BASKETBALL_VERSION, NEW_BASKETBALL_VERSION}
    assert by_version[OLD_BASKETBALL_VERSION]["sample_count"] == 2
    assert by_version[NEW_BASKETBALL_VERSION]["sample_count"] == 3


def test_calibration_version_isolation(isolated_data_dir):
    summaries = build_calibration_summaries(_two_version_rows())
    by_version = {s["model_version"]: s for s in summaries}

    assert set(by_version) == {OLD_BASKETBALL_VERSION, NEW_BASKETBALL_VERSION}
    assert by_version[OLD_BASKETBALL_VERSION]["sample_count"] == 2
    assert by_version[NEW_BASKETBALL_VERSION]["sample_count"] == 3
    assert set(by_version[NEW_BASKETBALL_VERSION]["classes"]) == {"home_win", "away_win"}


def test_temporal_version_isolation(isolated_data_dir):
    summaries = build_temporal_summaries(_two_version_rows())
    by_version = {s["model_version"]: s for s in summaries}

    assert set(by_version) == {OLD_BASKETBALL_VERSION, NEW_BASKETBALL_VERSION}

    old = by_version[OLD_BASKETBALL_VERSION]
    new = by_version[NEW_BASKETBALL_VERSION]

    assert old["reference_kickoff_at"] == "2030-02-01T00:00:00+00:00"
    assert new["reference_kickoff_at"] == "2030-06-01T00:00:00+00:00"
    assert old["all_time"]["sample_count"] == 2
    assert new["all_time"]["sample_count"] == 3
    assert [w["window_days"] for w in old["windows"]] == [30, 90, 180]
    assert [w["window_days"] for w in new["windows"]] == [30, 90, 180]


# ---------------------------------------------------------------------------
# 输入与配置不可变
# ---------------------------------------------------------------------------

def test_prediction_does_not_mutate_inputs_or_config():
    home = copy.deepcopy(HOME)
    away = copy.deepcopy(AWAY)
    odds = copy.deepcopy(ODDS_AWAY_FAV)

    config_before = copy.deepcopy(config.MODEL_CONFIG)

    predict_basketball(home, away, odds, "NBA")

    assert home == HOME
    assert away == AWAY
    assert odds == ODDS_AWAY_FAV
    assert config.MODEL_CONFIG == config_before
