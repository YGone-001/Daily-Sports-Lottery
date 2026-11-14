"""Phase 3-X Workstream D/E/F/G/H：真实实力引擎、V2 影子模型、评估回测、只读对比、
V1 不变与受保护存储边界（全部离线、确定性、合成数据）。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from utils import nba_team_strength as ts
from utils.nba_team_strength import (
    ALL_NBA_CODES,
    COVERAGE_NO_GAMES,
    COVERAGE_SUPPORTED,
    compute_team_strength,
    expected_home_win,
    game_end_utc,
)
from models.basketball_model import predict_basketball
from models.basketball_model_v2 import (
    MODEL_VERSION as V2_VERSION,
    STATUS_INSUFFICIENT,
    STATUS_OK,
    predict_basketball_v2,
    predict_from_inventory,
)
from utils.nba_model_evaluation import (
    brier,
    directional_hit,
    evaluate_rows,
    log_loss,
    walk_forward_backtest,
)
from tools.nba_model_comparison import build_comparison, _read_upcoming_nba_fixtures

BJ = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=BJ)


def _g(gid, hc, ac, hs, aws, utc, season=2026, ot=0):
    kickoff = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    bj = kickoff.astimezone(BJ)
    return {
        "provider_game_id": str(gid), "home_code": hc, "away_code": ac,
        "home_final_score": hs, "away_final_score": aws,
        "scheduled_start_utc": utc, "season": season, "overtime_periods": ot,
        "game_date": utc[:10], "beijing_date": bj.strftime("%Y-%m-%d"),
    }


def _features(code, n, elo, diff=0.0, last=None, coverage=COVERAGE_SUPPORTED):
    return {"team_id": code, "team_name": code, "games_used_before_cutoff": n,
            "historical_elo": elo, "recent_10_point_differential": diff,
            "last_game_at": last, "data_coverage": coverage}


# ---------------------------------------------------------------------------
# Workstream D：实力引擎
# ---------------------------------------------------------------------------

def test_winner_elo_rises_loser_falls():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z")]
    inv = compute_team_strength(games, as_of=AS_OF)
    assert inv["teams"]["BOS"]["historical_elo"] > 1500.0
    assert inv["teams"]["NYK"]["historical_elo"] < 1500.0
    assert inv["supported_team_count"] == 2


def test_home_advantage_in_expected_win():
    assert expected_home_win(1500, 1500) > 0.5


def test_future_game_excluded_no_leakage():
    # 开赛 2026-10-21 07:00 北京，结束更晚；as_of 取当日 00:00 -> 排除
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z")]
    early = datetime(2026, 10, 21, 0, 0, tzinfo=BJ)
    inv = compute_team_strength(games, as_of=early)
    assert inv["games_used"] == 0
    assert inv["teams"]["BOS"]["games_used_before_cutoff"] == 0


def test_duplicate_game_not_double_counted():
    g = _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z")
    inv = compute_team_strength([g, dict(g)], as_of=AS_OF)
    assert inv["teams"]["BOS"]["games_used_before_cutoff"] == 1


def test_tie_rejected():
    inv = compute_team_strength([_g(1, "BOS", "NYK", 100, 100, "2026-10-20T23:00:00.000Z")], as_of=AS_OF)
    assert inv["games_used"] == 0


def test_season_transition_shrinkage():
    games = [
        _g(1, "BOS", "NYK", 110, 100, "2026-04-20T23:00:00.000Z", season=2025),
        _g(2, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z", season=2026),
    ]
    inv = compute_team_strength(games, as_of=AS_OF)
    assert inv["season_transitions"] == 1
    # 收缩后第二场胜利带来的 Elo 低于无收缩情形
    no_shrink = compute_team_strength(games, as_of=AS_OF, params={"season_shrinkage": 0.0})
    assert inv["teams"]["BOS"]["historical_elo"] < no_shrink["teams"]["BOS"]["historical_elo"]


def test_all_30_franchises_present_even_with_no_games():
    inv = compute_team_strength([], as_of=AS_OF)
    assert set(inv["teams"]) == set(ALL_NBA_CODES)
    assert len(ALL_NBA_CODES) == 30
    assert inv["supported_team_count"] == 0
    assert all(t["data_coverage"] == COVERAGE_NO_GAMES for t in inv["teams"].values())


def test_feature_records_and_rest():
    games = [
        _g(1, "BOS", "NYK", 110, 100, "2026-11-10T23:00:00.000Z"),
        _g(2, "BOS", "IND", 120, 110, "2026-11-12T23:00:00.000Z"),
    ]
    inv = compute_team_strength(games, as_of=AS_OF)
    bos = inv["teams"]["BOS"]
    assert bos["wins"] == 2 and bos["win_percentage"] == 1.0
    assert bos["home_record"]["games"] == 2
    assert bos["recent_5_game_record"]["wins"] == 2
    assert bos["average_point_differential"] > 0
    assert bos["days_since_last_completed_game"] is not None


def test_deterministic_across_runs():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z")]
    a = compute_team_strength(games, as_of=AS_OF)
    b = compute_team_strength(games, as_of=AS_OF)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_no_name_hash_and_not_possession_adjusted():
    inv = compute_team_strength([], as_of=AS_OF)
    assert inv["name_hash_used_as_strength"] is False
    assert "NOT_possession_adjusted" in inv["metric_semantics"]


def test_game_end_estimation_includes_overtime():
    g = _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z", ot=2)
    end = game_end_utc(g)
    kickoff = datetime.fromisoformat("2026-10-20T23:00:00+00:00")
    assert end == kickoff.astimezone(BJ) + timedelta(minutes=150 + 30)


# ---------------------------------------------------------------------------
# Workstream E：V2 影子模型
# ---------------------------------------------------------------------------

def test_v2_probability_validity_and_sum():
    hf = _features("BOS", 20, 1600)
    af = _features("NYK", 20, 1500)
    res = predict_basketball_v2(hf, af, odds={"home_win": 1.8, "away_win": 2.0},
                                fixture_kickoff=AS_OF)
    raw = res["raw_model_probabilities"]
    assert res["status"] == STATUS_OK
    assert 0 <= raw["home_win"] <= 1 and 0 <= raw["away_win"] <= 1
    assert abs(raw["home_win"] + raw["away_win"] - 1) < 1e-9


def test_v2_insufficient_when_no_games():
    res = predict_basketball_v2(_features("BOS", 0, 1500), _features("NYK", 20, 1500))
    assert res["status"] == STATUS_INSUFFICIENT
    assert res["raw_model_probabilities"] is None


def test_v2_small_sample_flag():
    res = predict_basketball_v2(_features("BOS", 2, 1550), _features("NYK", 20, 1500))
    assert res["status"] == STATUS_OK and res["small_sample"] is True


def test_v2_stronger_team_higher_home_probability():
    strong = predict_basketball_v2(_features("BOS", 20, 1650), _features("NYK", 20, 1500))
    weak = predict_basketball_v2(_features("BOS", 20, 1500), _features("NYK", 20, 1650))
    assert strong["raw_model_probabilities"]["home_win"] > weak["raw_model_probabilities"]["home_win"]


def test_v2_home_court_advantage_equal_teams():
    res = predict_basketball_v2(_features("BOS", 20, 1500), _features("NYK", 20, 1500))
    assert res["raw_model_probabilities"]["home_win"] > 0.5


def test_v2_raw_display_market_distinction():
    res = predict_basketball_v2(_features("BOS", 20, 1600), _features("NYK", 20, 1500),
                                odds={"home_win": 1.5, "away_win": 2.6})
    assert res["raw_model_probabilities"]["home_win"] != res["display_probabilities"]["home_win"]
    assert res["market_implied_probabilities"]["home_win"] is not None


def test_v2_deterministic_and_isolated():
    hf, af = _features("BOS", 20, 1600), _features("NYK", 20, 1500)
    a = predict_basketball_v2(hf, af)
    b = predict_basketball_v2(hf, af)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["production_model_unchanged"] is True
    assert a["writes_production_stores"] is False
    assert a["model_version"] == V2_VERSION == "basketball-margin-2-shadow"


def test_v2_back_to_back_penalty():
    kick = datetime(2026, 11, 15, 8, 0, tzinfo=BJ)
    rested = _features("BOS", 20, 1500, last="2026-11-10T08:00:00+08:00")
    b2b = _features("BOS", 20, 1500, last="2026-11-14T20:00:00+08:00")
    opp = _features("NYK", 20, 1500, last="2026-11-10T08:00:00+08:00")
    p_rested = predict_basketball_v2(rested, opp, fixture_kickoff=kick)["raw_model_probabilities"]["home_win"]
    p_b2b = predict_basketball_v2(b2b, opp, fixture_kickoff=kick)["raw_model_probabilities"]["home_win"]
    assert p_rested > p_b2b


def test_predict_from_inventory_missing_team():
    inv = compute_team_strength([], as_of=AS_OF)
    res = predict_from_inventory(inv, "BOS", "NYK")
    assert res["status"] == STATUS_INSUFFICIENT


# ---------------------------------------------------------------------------
# Workstream G：指标与走查回测
# ---------------------------------------------------------------------------

def test_metric_primitives():
    assert brier(0.8, 1) == pytest.approx(0.04)
    assert directional_hit(0.7, True) is True
    assert directional_hit(0.7, False) is False
    assert log_loss(0.9, 1) < log_loss(0.6, 1)


def test_evaluate_rows_empty():
    m = evaluate_rows([])
    assert m["sample_count"] == 0 and m["direction_accuracy"] is None


def test_evaluate_rows_market_comparable():
    rows = [{"p_home": 0.7, "y": 1, "market_home": 0.6},
            {"p_home": 0.4, "y": 0, "market_home": 0.45}]
    m = evaluate_rows(rows)
    assert m["sample_count"] == 2 and m["market_comparable_count"] == 2
    assert m["direction_accuracy"] == 1.0
    assert m["normalized_market_brier"] is not None
    assert m["mean_signed_model_market_gap"] is not None


def test_walk_forward_no_leakage_flags():
    games = [
        _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
        _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z"),
        _g(3, "NYK", "BOS", 98, 102, "2026-10-25T23:00:00.000Z"),
    ]
    bt = walk_forward_backtest(games)
    assert bt["research_label"] == "RETROSPECTIVE_WALK_FORWARD_RESEARCH"
    assert bt["same_date_leakage"] is False and bt["future_result_leakage"] is False
    assert bt["random_shuffled_cv"] is False and bt["is_original_realtime_snapshot"] is False
    assert bt["evaluated_games"] >= 1
    assert bt["promotion_authorized"] is False and bt["profitability_claimed"] is False


def test_walk_forward_first_date_insufficient():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z")]
    bt = walk_forward_backtest(games)
    # 首日无历史 -> 不足跳过
    assert bt["evaluated_games"] == 0 and bt["insufficient_coverage_skipped"] == 1


# ---------------------------------------------------------------------------
# Workstream F/H：只读对比 + V1 不变
# ---------------------------------------------------------------------------

def _fixture(home="波士顿凯尔特人", away="纽约尼克斯", hc="BOS", ac="NYK"):
    return {"id": "500j-2026-11-15-9999", "date": "2026-11-15", "home": home, "away": away,
            "league": "美国职业篮球联盟", "status": "upcoming", "time": "08:00",
            "kickoff_time_known": True, "odds": {"home_win": 1.5, "away_win": 2.5},
            "_home_code": hc, "_away_code": ac}


def test_comparison_required_fields_and_eligible():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
             _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z")]
    ds = {"games": games, "game_count": 2}
    comp = build_comparison(ds, as_of=AS_OF, fixtures=[_fixture()])
    row = comp["comparison"][0]
    for field in ["canonical_match_id", "fixture_date", "home_team", "away_team",
                  "verified_kickoff", "prediction_as_of", "v1_model_version",
                  "v1_home_probability", "v2_shadow_version", "v2_home_probability",
                  "market_home_probability", "v1_vs_market_gap", "v2_vs_market_gap",
                  "home_real_games_used", "away_real_games_used", "data_coverage_status",
                  "shadow_eligibility"]:
        assert field in row
    assert row["shadow_eligibility"] == "ELIGIBLE"
    assert row["v2_home_probability"] is not None
    assert comp["writes_protected_stores"] is False and comp["network_requests"] == 0


def test_comparison_insufficient_fixture():
    comp = build_comparison({"games": [], "game_count": 0}, as_of=AS_OF, fixtures=[_fixture()])
    row = comp["comparison"][0]
    assert row["shadow_eligibility"] == "INSUFFICIENT_REAL_TEAM_DATA"
    assert row["v2_home_probability"] is None
    assert comp["insufficient_fixture_count"] == 1


def test_v1_model_version_unchanged():
    assert config.MODEL_VERSIONS["basketball"] == "basketball-margin-1"
    home = {"name": "波士顿凯尔特人", "elo_rating": 1700, "matches_played": 0}
    away = {"name": "纽约尼克斯", "elo_rating": 1700, "matches_played": 0}
    res = predict_basketball(home, away, None, "美国职业篮球联盟")
    # V1 版本由快照层 stamped；模型函数本身保持冷启动概率契约不变。
    assert "model_probabilities" in res and "probabilities" in res
    again = predict_basketball(home, away, None, "美国职业篮球联盟")
    assert res["model_probabilities"] == again["model_probabilities"]
    assert res["model_probabilities"]["home_win"] + res["model_probabilities"]["away_win"] == 100


def test_read_upcoming_fixtures_from_isolated_store(isolated_data_dir):
    daily = {"matches": [
        {"id": "500j-2026-11-15-1", "sport": "basketball", "league": "美国职业篮球联盟",
         "status": "upcoming", "home": "波士顿凯尔特人", "away": "纽约尼克斯",
         "date": "2026-11-15", "time": "08:00"},
        {"id": "500j-2026-11-15-2", "sport": "basketball", "league": "美国职业篮球联盟",
         "status": "finished", "home": "波士顿凯尔特人", "away": "纽约尼克斯",
         "date": "2026-11-15", "time": "08:00"},
        {"id": "500w-2026-11-15-3", "sport": "football", "league": "英超",
         "status": "upcoming", "home": "曼城", "away": "阿森纳", "date": "2026-11-15", "time": "20:00"},
    ]}
    (isolated_data_dir / "daily_matches.json").write_text(json.dumps(daily), encoding="utf-8")
    fixtures = _read_upcoming_nba_fixtures()
    assert len(fixtures) == 1 and fixtures[0]["_home_code"] == "BOS"


# ---------------------------------------------------------------------------
# 受保护存储边界（§4.24）：分析管线零写入
# ---------------------------------------------------------------------------

def test_pipeline_writes_no_protected_stores(isolated_data_dir):
    stores = ["daily_matches.json", "prediction_snapshots.json", "odds_snapshots.json",
              "settlements.json", "evaluation_rows.json", "team_strength.json", "calibrated.json"]
    for name in stores:
        (isolated_data_dir / name).write_text(json.dumps({"version": 1, "seed": name}), encoding="utf-8")

    def digests():
        return {n: hashlib.sha256((isolated_data_dir / n).read_bytes()).hexdigest() for n in stores}

    before = digests()
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
             _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z")]
    inv = compute_team_strength(games, as_of=AS_OF)
    predict_from_inventory(inv, "BOS", "NYK", odds={"home_win": 1.5, "away_win": 2.5})
    walk_forward_backtest(games)
    build_comparison({"games": games, "game_count": 2}, as_of=AS_OF, fixtures=[_fixture()])
    _read_upcoming_nba_fixtures()
    assert digests() == before