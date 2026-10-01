"""
足球攻防独立化的测试。

Elo 只提供整体实力先验；攻击力由历史场均**进球**独立推导，防守力由历史场均**失球**
独立推导，样本量小时向 Elo 先验收缩。篮球行为保持不变。
"""
from __future__ import annotations

import math

import pytest

import config
from models.predictor import predict_football
from utils import team_strength
from utils.calibration_evaluation import build_calibration_summaries
from utils.classification_evaluation import build_classification_summaries
from utils.daily_loader import enrich_match, load_json, save_json
from utils.evaluation_rows import capture_evaluation_row, get_all_evaluation_rows
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.settlements import get_settlements_for_match, settle_snapshot
from utils.team_strength import (
    derive_football_attack_defense,
    elo_to_attack,
    elo_to_defense,
    get_team_profile,
    update_from_result,
)

BASE = config.MODEL_CONFIG["base_goals_per_match"]
PRIOR_MATCHES = config.MODEL_CONFIG["football_strength_prior_matches"]
SCALE = config.MODEL_CONFIG["football_goal_signal_scale"]


def _team(name: str, elo: float = 1600.0, attack: float = 0.5, defense: float = 0.5) -> dict:
    return {"name": name, "elo_rating": elo, "attack_rating": attack, "defense_rating": defense}


# ---------------------------------------------------------------------------
# 纯推导函数
# ---------------------------------------------------------------------------

def test_no_history_uses_elo_prior(isolated_data_dir):
    assert derive_football_attack_defense(1600.0, 0, 0, 0) == (
        elo_to_attack(1600.0),
        elo_to_defense(1600.0),
    )
    attack, defense = derive_football_attack_defense(1800.0)
    assert attack == elo_to_attack(1800.0)
    assert defense == elo_to_defense(1800.0)
    # 无证据时两个维度允许相等（不引入任意偏移）
    assert attack == defense


def test_neutral_historical_rates(isolated_data_dir):
    """场均进/失球都等于基准 -> 观测证据中性，结果约等于 0.5。"""
    attack, defense = derive_football_attack_defense(1600.0, 20, 27, 27)

    assert math.isclose(attack, 0.5, rel_tol=1e-9)
    assert math.isclose(defense, 0.5, rel_tol=1e-9)
    assert math.isclose(27 / 20, BASE, rel_tol=1e-12)


def test_strong_attack_weak_defense(isolated_data_dir):
    attack, defense = derive_football_attack_defense(1600.0, 20, 40, 35)

    assert attack > defense
    assert attack > 0.5
    assert defense < 0.5


def test_weak_attack_strong_defense(isolated_data_dir):
    attack, defense = derive_football_attack_defense(1600.0, 20, 15, 12)

    assert defense > attack
    assert attack < 0.5
    assert defense > 0.5


def test_independent_evidence_same_elo(isolated_data_dir):
    """同 Elo、同场次，但进/失球证据不同 -> 两个维度按各自证据分化。"""
    attacker = derive_football_attack_defense(1600.0, 20, 40, 35)
    defender = derive_football_attack_defense(1600.0, 20, 15, 12)

    assert attacker[0] > defender[0]      # 攻强
    assert attacker[1] < defender[1]      # 守弱


def test_attack_isolation(isolated_data_dir):
    """固定 Elo / N / GA，只改 GF -> 攻击变、防守完全不变。"""
    baseline = derive_football_attack_defense(1600.0, 20, 20, 20)
    changed = derive_football_attack_defense(1600.0, 20, 45, 20)

    assert changed[0] != baseline[0]
    assert changed[1] == baseline[1]


def test_defense_isolation(isolated_data_dir):
    """固定 Elo / N / GF，只改 GA -> 防守变、攻击完全不变。"""
    baseline = derive_football_attack_defense(1600.0, 20, 20, 20)
    changed = derive_football_attack_defense(1600.0, 20, 20, 5)

    assert changed[1] != baseline[1]
    assert changed[0] == baseline[0]


def test_shrinkage_toward_prior(isolated_data_dir):
    """相同进/失球率下，N 小更贴近 Elo 先验，N 大更贴近观测。"""
    small = derive_football_attack_defense(1600.0, 1, 2, 1)
    large = derive_football_attack_defense(1600.0, 20, 40, 20)

    prior_attack = elo_to_attack(1600.0)
    prior_defense = elo_to_defense(1600.0)

    assert abs(small[0] - prior_attack) < abs(large[0] - prior_attack)
    assert abs(small[1] - prior_defense) < abs(large[1] - prior_defense)
    assert large[0] > small[0]
    assert large[1] > small[1]


def test_evidence_weight_matches_formula(isolated_data_dir):
    """显式核对收缩权重与观测值公式。"""
    elo, n, gf, ga = 1600.0, 20, 40, 35
    attack, defense = derive_football_attack_defense(elo, n, gf, ga)

    weight = n / (n + PRIOR_MATCHES)
    observed_attack = 0.5 + SCALE * ((gf / n) / BASE - 1.0)
    observed_defense = 0.5 + SCALE * (1.0 - (ga / n) / BASE)

    expected_attack = elo_to_attack(elo) * (1 - weight) + observed_attack * weight
    expected_defense = elo_to_defense(elo) * (1 - weight) + observed_defense * weight

    assert math.isclose(attack, expected_attack, rel_tol=1e-12)
    assert math.isclose(defense, expected_defense, rel_tol=1e-12)


@pytest.mark.parametrize(
    "gf,ga",
    [(0, 0), (1000, 0), (0, 1000), (500, 500), (1, 999)],
)
def test_rating_bounds_and_finiteness(isolated_data_dir, gf, ga):
    attack, defense = derive_football_attack_defense(1600.0, 100, gf, ga)

    assert 0.25 <= attack <= 0.95
    assert 0.25 <= defense <= 0.95
    assert math.isfinite(attack) and math.isfinite(defense)


def test_negative_goal_totals_are_not_evidence(isolated_data_dir):
    """负的累计值不是有效证据，按 0 处理且不产生非有限结果。"""
    attack, defense = derive_football_attack_defense(1600.0, 10, -50, -50)

    assert math.isfinite(attack) and math.isfinite(defense)
    assert 0.25 <= attack <= 0.95 and 0.25 <= defense <= 0.95
    # 与显式传 0 的结果一致
    assert (attack, defense) == derive_football_attack_defense(1600.0, 10, 0, 0)


def test_zero_base_goal_falls_back_to_prior(isolated_data_dir, monkeypatch):
    """基准进球异常为 0 时不做除法，退回 Elo 先验。"""
    monkeypatch.setitem(config.MODEL_CONFIG, "base_goals_per_match", 0.0)

    attack, defense = derive_football_attack_defense(1600.0, 20, 40, 35)

    assert (attack, defense) == (elo_to_attack(1600.0), elo_to_defense(1600.0))


def test_derivation_is_deterministic(isolated_data_dir):
    first = derive_football_attack_defense(1725.5, 17, 31, 22)
    second = derive_football_attack_defense(1725.5, 17, 31, 22)

    assert first == second


# ---------------------------------------------------------------------------
# 结果更新
# ---------------------------------------------------------------------------

def test_result_update_uses_new_cumulative_stats(isolated_data_dir):
    """攻防必须由**更新后**的累计进/失球推导（能捕捉旧的错误顺序）。"""
    update_from_result("Home FC", "Away FC", 3, 1, "英超", "football")

    profile = get_team_profile("Home FC", "英超", "football")
    assert profile["matches_played"] == 1
    assert profile["goals_for"] == 3
    assert profile["goals_against"] == 1

    expected = derive_football_attack_defense(
        profile["elo_rating"], profile["matches_played"],
        profile["goals_for"], profile["goals_against"],
    )
    assert profile["attack_rating"] == round(expected[0], 3)
    assert profile["defense_rating"] == round(expected[1], 3)
    # 旧实现会写入纯 Elo 映射，与本轮证据无关
    assert profile["attack_rating"] != round(elo_to_attack(profile["elo_rating"]), 3)


def test_repeated_result_updates_do_not_drift(isolated_data_dir):
    for home_goals, away_goals in [(2, 0), (1, 1), (0, 3), (4, 2)]:
        update_from_result("Home FC", "Away FC", home_goals, away_goals, "英超", "football")

    for name in ("Home FC", "Away FC"):
        profile = get_team_profile(name, "英超", "football")
        expected = derive_football_attack_defense(
            profile["elo_rating"], profile["matches_played"],
            profile["goals_for"], profile["goals_against"],
        )
        assert profile["attack_rating"] == round(expected[0], 3)
        assert profile["defense_rating"] == round(expected[1], 3)

    home = get_team_profile("Home FC", "英超", "football")
    away = get_team_profile("Away FC", "英超", "football")
    assert home["goals_for"] + away["goals_for"] == sum(h + a for h, a in [(2, 0), (1, 1), (0, 3), (4, 2)])


def test_update_can_produce_independent_dimensions(isolated_data_dir):
    """大胜对手 -> 攻防维度出现分化，而不再恒等。"""
    update_from_result("Scorers", "Conceders", 6, 0, "英超", "football")

    scorers = get_team_profile("Scorers", "英超", "football")
    assert scorers["attack_rating"] > scorers["defense_rating"]


# ---------------------------------------------------------------------------
# 既有档案懒升级
# ---------------------------------------------------------------------------

LEGACY_KEY = "football|Legacy FC"


def _seed_legacy_profile() -> dict:
    legacy = {
        "name": "Legacy FC",
        "league": "英超",
        "sport": "football",
        "elo_rating": 1600.0,
        "attack_rating": 0.5,      # 旧的 Elo 单一映射产物
        "defense_rating": 0.5,
        "matches_played": 20,
        "wins": 10,
        "draws": 4,
        "losses": 6,
        "goals_for": 40,
        "goals_against": 20,
        "estimated": False,
    }
    save_json("team_strength.json", {"teams": {LEGACY_KEY: legacy}})
    team_strength.reload()
    return legacy


def test_legacy_profile_lazy_upgrade(isolated_data_dir):
    _seed_legacy_profile()

    profile = get_team_profile("Legacy FC", "英超", "football")

    expected = derive_football_attack_defense(1600.0, 20, 40, 20)
    assert profile["attack_rating"] == round(expected[0], 3)
    assert profile["defense_rating"] == round(expected[1], 3)
    assert profile["attack_rating"] != profile["defense_rating"]

    # 其他字段一律不动
    assert profile["elo_rating"] == 1600.0
    assert profile["matches_played"] == 20
    assert profile["wins"] == 10 and profile["draws"] == 4 and profile["losses"] == 6
    assert profile["goals_for"] == 40 and profile["goals_against"] == 20

    # 已持久化到可变运行时档案
    stored = load_json("team_strength.json")["teams"][LEGACY_KEY]
    assert stored["attack_rating"] == profile["attack_rating"]
    assert stored["defense_rating"] == profile["defense_rating"]


def test_lazy_upgrade_is_idempotent(isolated_data_dir):
    _seed_legacy_profile()

    first = get_team_profile("Legacy FC", "英超", "football")
    after_first = load_json("team_strength.json")
    second = get_team_profile("Legacy FC", "英超", "football")
    after_second = load_json("team_strength.json")

    assert first["attack_rating"] == second["attack_rating"]
    assert first["defense_rating"] == second["defense_rating"]
    assert after_first == after_second  # 第二次读取不再产生任何变更


# ---------------------------------------------------------------------------
# 篮球不受影响
# ---------------------------------------------------------------------------

def test_basketball_keeps_elo_mapping(isolated_data_dir):
    update_from_result("Lakers", "Celtics", 108, 101, "NBA", "basketball")

    lakers = get_team_profile("Lakers", "NBA", "basketball")
    celtics = get_team_profile("Celtics", "NBA", "basketball")

    for profile in (lakers, celtics):
        assert profile["attack_rating"] == round(elo_to_attack(profile["elo_rating"]), 3)
        assert profile["defense_rating"] == round(elo_to_defense(profile["elo_rating"]), 3)
        # 篮球不使用进球证据
        assert profile["attack_rating"] == profile["defense_rating"]

    assert config.MODEL_VERSIONS["basketball"] == "baseline-1"


# ---------------------------------------------------------------------------
# 预测器集成（不改预测器代码）
# ---------------------------------------------------------------------------

def test_predictor_consumes_independent_dimensions(isolated_data_dir):
    opponent = _team("Opponent")

    weak_home = _team("Weak Home", attack=0.50)
    strong_home = _team("Strong Home", attack=0.70)
    solid_away = _team("Solid Away", defense=0.75)

    baseline = predict_football(weak_home, opponent)
    stronger_attack = predict_football(strong_home, opponent)
    stronger_opponent_defense = predict_football(weak_home, solid_away)

    # 主场攻击更强 -> 主场期望进球更高
    assert stronger_attack["expected_goals"]["home"] > baseline["expected_goals"]["home"]
    # 客队防守更强 -> 主场期望进球更低
    assert stronger_opponent_defense["expected_goals"]["home"] < baseline["expected_goals"]["home"]


def test_predictor_probabilities_respond_to_dimensions(isolated_data_dir):
    opponent = _team("Opponent")

    baseline = predict_football(_team("Base", attack=0.50, defense=0.50), opponent)
    stronger = predict_football(_team("Strong", attack=0.75, defense=0.50), opponent)
    weaker = predict_football(_team("Weak", attack=0.30, defense=0.50), opponent)

    assert stronger["model_probabilities"]["home_win"] > baseline["model_probabilities"]["home_win"]
    assert weaker["model_probabilities"]["home_win"] < baseline["model_probabilities"]["home_win"]


# ---------------------------------------------------------------------------
# 版本与下游
# ---------------------------------------------------------------------------

def test_football_version_bump(isolated_data_dir, make_match):
    football, _c1 = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball, _c2 = capture_snapshot(
        enrich_match(make_match(id="m-bb", sport="basketball", league="NBA",
                                home="湖人", away="凯尔特人",
                                odds={"home_win": 1.80, "away_win": 2.00}))
    )

    assert football["model_version"] == "football-ad-1"
    assert basketball["model_version"] == "baseline-1"


def test_existing_baseline_snapshot_preserved(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-1"))
    baseline, created = capture_snapshot(match, model_version="baseline-1")
    assert created is True
    original = dict(baseline)

    current, created2 = capture_snapshot(enrich_match(make_match(id="m-1")))

    assert created2 is True
    assert current["model_version"] == "football-ad-1"
    assert current["snapshot_id"] != baseline["snapshot_id"]
    assert get_snapshots_for_match("m-1")[0] == original
    assert len(get_snapshots_for_match("m-1")) == 2


@pytest.mark.parametrize("status", ["live", "finished"])
def test_started_match_gets_no_retroactive_snapshot(isolated_data_dir, make_match, status):
    from datetime import timedelta

    from utils.daily_loader import get_match_datetime

    match = enrich_match(make_match(id="m-1"))
    capture_snapshot(match, model_version="baseline-1")
    kickoff = get_match_datetime(match)

    later = kickoff + timedelta(minutes=10 if status == "live" else 200)
    score = {"ft": [2, 1]} if status == "finished" else None
    result, created = capture_snapshot(dict(match, status=status, score=score), now=later)

    assert created is False
    assert result is None
    assert len(get_snapshots_for_match("m-1")) == 1


def test_downstream_propagation_of_new_version(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-down"))
    snapshot, created = capture_snapshot(match)
    assert created is True
    assert snapshot["model_version"] == "football-ad-1"

    settlement, settled = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                       score={"ft": [2, 1]})
    )
    assert settled is True
    assert settlement["model_version"] == "football-ad-1"

    row, materialized = capture_evaluation_row(snapshot, settlement)
    assert materialized is True
    assert row["model_version"] == "football-ad-1"
    assert get_settlements_for_match("m-down")[0]["model_version"] == "football-ad-1"


def test_analytics_group_new_version_separately(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-groups"))
    for version in ("baseline-1", "football-ad-1"):
        snapshot, _c = capture_snapshot(match, model_version=version)
        settlement, _s = settle_snapshot(
            snapshot, dict(match, status="finished", date="2020-01-01", time="20:00",
                           score={"ft": [2, 1]})
        )
        capture_evaluation_row(snapshot, settlement)

    rows = get_all_evaluation_rows()
    assert sorted(r["model_version"] for r in rows) == ["baseline-1", "football-ad-1"]

    assert sorted(s["model_version"] for s in build_classification_summaries(rows)) == [
        "baseline-1", "football-ad-1",
    ]
    assert sorted(s["model_version"] for s in build_calibration_summaries(rows)) == [
        "baseline-1", "football-ad-1",
    ]


# ---------------------------------------------------------------------------
# 市场准入回归
# ---------------------------------------------------------------------------

def test_market_admission_not_bypassed(isolated_data_dir, monkeypatch):
    from utils import fetcher_500, scraper

    def empty(sport):
        return []

    monkeypatch.setattr(fetcher_500, "fetch_live_matches",
                        lambda sport: [{"id": "m-unpriced", "sport": "football", "league": "英超",
                                        "date": "2030-03-01", "time": "20:00", "status": "upcoming",
                                        "home": "A", "away": "B", "home_rank": None,
                                        "away_rank": None, "score": None, "odds": None}])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", empty)
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", empty)

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert load_json("daily_matches.json")["matches"] == []
    assert get_snapshots_for_match("m-unpriced") == []
