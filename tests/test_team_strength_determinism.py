"""
未知球队冷启动确定性测试。

内置字符串 hash 按进程加盐，因此同一输入在不同进程会得到不同初始 Elo；
本任务用 SHA-256 取代它，保证「同联赛 + 同队名 + 同代码配置 = 同冷启动 Elo」。
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys

import pytest

import config
from utils import team_strength
from utils.calibration_evaluation import build_calibration_summaries
from utils.classification_evaluation import build_classification_summaries
from utils.daily_loader import enrich_match, load_json, save_json
from utils.prediction_snapshots import capture_snapshot, get_snapshots_for_match
from utils.team_strength import (
    get_team_profile,
    stable_team_name_offset,
    update_from_result,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 未在 LEAGUE_ANCHORS 中的联赛 -> 走「联赛基准 + 确定性扰动」分支
UNANCHORED_LEAGUE = "英冠"
UNKNOWN_LEAGUE = "不存在联赛"

GOLDEN_VECTORS = [
    ("英超", "测试队A", -43),
    ("英超", "测试队B", -2),
    ("", "Unknown FC", 25),
    ("NBA", "Example Club", -49),
    ("中超", "上海测试", 57),
]


def _use_store(monkeypatch, path) -> None:
    monkeypatch.setattr(config, "DATA_DIR", str(path))
    team_strength.reload()


def _run_with_seed(seed: str, code: str) -> str:
    """在独立解释器 + 指定 PYTHONHASHSEED 下运行一段代码，返回 stdout。"""
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# 确定性哈希向量
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("league,name,expected", GOLDEN_VECTORS)
def test_golden_vectors(isolated_data_dir, league, name, expected):
    assert stable_team_name_offset(league, name) == expected


def test_offset_range(isolated_data_dir):
    leagues = ["英超", "西甲", "NBA", "CBA", "中超", "英冠", "", "未知联赛"]
    names = [f"球队{i}" for i in range(40)] + ["Unknown FC", "Example Club", "A", "B"]

    offsets = [stable_team_name_offset(lg, nm) for lg in leagues for nm in names]

    assert len(offsets) == len(leagues) * len(names)
    for offset in offsets:
        assert isinstance(offset, int)
        assert -60 <= offset <= 60


def test_same_process_repeatability(isolated_data_dir):
    for league, name, _expected in GOLDEN_VECTORS:
        first = stable_team_name_offset(league, name)
        assert stable_team_name_offset(league, name) == first
        assert stable_team_name_offset(league, name) == first


def test_pythonhashseed_independence(isolated_data_dir):
    """跨进程 + 不同 PYTHONHASHSEED 下稳定偏移必须完全一致。"""
    code = (
        "from utils.team_strength import stable_team_name_offset;"
        "print(stable_team_name_offset('\\u82f1\\u8d85', '\\u6d4b\\u8bd5\\u961fA'))"
    )

    outputs = {_run_with_seed(seed, code) for seed in ("1", "2", "12345")}

    assert outputs == {"-43"}


def test_no_builtin_hash_dependency(isolated_data_dir):
    """内置 hash 会随 PYTHONHASHSEED 变化，而稳定偏移不会。"""
    offset_code = (
        "from utils.team_strength import stable_team_name_offset;"
        "print(stable_team_name_offset('\\u82f1\\u8d85', '\\u6d4b\\u8bd5\\u961fA'))"
    )
    builtin_code = "print(hash('\\u82f1\\u8d85|\\u6d4b\\u8bd5\\u961fA'))"

    seeds = ("1", "2", "12345")
    offsets = {_run_with_seed(seed, offset_code) for seed in seeds}
    builtin_hashes = {_run_with_seed(seed, builtin_code) for seed in seeds}

    assert len(builtin_hashes) > 1     # 内置 hash 确实随进程加盐变化
    assert offsets == {"-43"}          # 稳定偏移不受影响


# ---------------------------------------------------------------------------
# 冷启动优先级
# ---------------------------------------------------------------------------

def test_anchor_priority(isolated_data_dir):
    """锚点命中时直接使用锚点 Elo，不叠加确定性扰动。"""
    profile = get_team_profile("曼城", "英超", "football")

    assert profile["elo_rating"] == 1980.0
    assert profile["elo_rating"] != 1980 + stable_team_name_offset("英超", "曼城")


def test_rank_priority_overrides_perturbation(isolated_data_dir):
    """有名次时走既有的名次公式，队名变化不影响结果。"""
    first = get_team_profile("甲队", UNANCHORED_LEAGUE, "football", rank=3)
    team_strength.reload()
    second = get_team_profile("完全不同队名", UNANCHORED_LEAGUE, "football", rank=3)

    base = config.LEAGUE_STRENGTH[UNANCHORED_LEAGUE]
    expected_adjust = max(-120.0, min(100.0, 80.0 - (3 - 1) * 5.5))

    assert first["elo_rating"] == round(base + expected_adjust, 1)
    assert second["elo_rating"] == first["elo_rating"]


def test_football_fallback_formula(isolated_data_dir):
    profile = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")

    base = config.LEAGUE_STRENGTH[UNANCHORED_LEAGUE]
    expected = base + stable_team_name_offset(UNANCHORED_LEAGUE, "甲队")

    assert profile["elo_rating"] == round(expected, 1)
    assert profile["estimated"] is True


def test_football_unknown_league_baseline(isolated_data_dir):
    profile = get_team_profile("甲队", UNKNOWN_LEAGUE, "football")

    expected = config.DEFAULT_LEAGUE_ELO + stable_team_name_offset(UNKNOWN_LEAGUE, "甲队")

    assert profile["elo_rating"] == round(expected, 1)


def test_basketball_unknown_league_baseline(isolated_data_dir):
    profile = get_team_profile("甲队", UNKNOWN_LEAGUE, "basketball")

    expected = 1700 + stable_team_name_offset(UNKNOWN_LEAGUE, "甲队")

    assert profile["elo_rating"] == round(expected, 1)


# ---------------------------------------------------------------------------
# 新档案跨存储可复现
# ---------------------------------------------------------------------------

PROFILE_FIELDS = (
    "elo_rating", "attack_rating", "defense_rating",
    "matches_played", "goals_for", "goals_against", "estimated",
)


@pytest.mark.parametrize("sport", ["football", "basketball"])
def test_new_profile_reproducible_across_stores(isolated_data_dir, monkeypatch, tmp_path, sport):
    _use_store(monkeypatch, tmp_path / "store-a")
    first = get_team_profile("甲队", UNANCHORED_LEAGUE, sport)

    _use_store(monkeypatch, tmp_path / "store-b")
    second = get_team_profile("甲队", UNANCHORED_LEAGUE, sport)

    for field in PROFILE_FIELDS:
        assert first[field] == second[field], field

    # 两个存储都实际落盘且内容一致
    for name in ("store-a", "store-b"):
        stored = load_json("team_strength.json")["teams"][f"{sport}|甲队"]
        assert stored["elo_rating"] == first["elo_rating"]


def test_clean_store_recreation_after_cache_reset(isolated_data_dir, monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path / "store-c")
    first = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")

    team_strength.reload()   # 丢弃内存缓存
    second = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")

    assert first["elo_rating"] == second["elo_rating"]
    assert first["attack_rating"] == second["attack_rating"]
    assert first["defense_rating"] == second["defense_rating"]


# ---------------------------------------------------------------------------
# 既有档案保持
# ---------------------------------------------------------------------------

def _seed_profile(key: str, profile: dict) -> None:
    save_json("team_strength.json", {"teams": {key: profile}})
    team_strength.reload()


def test_existing_football_profile_elo_preserved(isolated_data_dir):
    """既有档案的 Elo 不得因新的冷启动映射而被重算。"""
    base = config.LEAGUE_STRENGTH[UNANCHORED_LEAGUE]
    new_cold_start = round(base + stable_team_name_offset(UNANCHORED_LEAGUE, "甲队"), 1)
    stored_elo = new_cold_start + 17.5      # 刻意与新的冷启动值不同

    _seed_profile(
        "football|甲队",
        {
            "name": "甲队", "league": UNANCHORED_LEAGUE, "sport": "football",
            "elo_rating": stored_elo,
            "attack_rating": 0.5, "defense_rating": 0.5,
            "matches_played": 12, "wins": 6, "draws": 3, "losses": 3,
            "goals_for": 20, "goals_against": 15, "estimated": False,
        },
    )

    profile = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")

    assert profile["elo_rating"] == stored_elo
    assert profile["matches_played"] == 12
    # 足球攻防懒刷新仍按既有规则运行（由既有 Elo 与累计进失球推导）
    expected_attack, expected_defense = team_strength.derive_football_attack_defense(
        stored_elo, 12, 20, 15
    )
    assert profile["attack_rating"] == round(expected_attack, 3)
    assert profile["defense_rating"] == round(expected_defense, 3)


def test_existing_basketball_profile_elo_preserved(isolated_data_dir):
    stored_elo = 1813.4
    _seed_profile(
        "basketball|乙队",
        {
            "name": "乙队", "league": "NBA", "sport": "basketball",
            "elo_rating": stored_elo,
            "attack_rating": 0.6, "defense_rating": 0.6,
            "matches_played": 30, "wins": 20, "draws": 0, "losses": 10,
            "goals_for": 3300, "goals_against": 3100, "estimated": False,
        },
    )

    profile = get_team_profile("乙队", "NBA", "basketball")

    assert profile["elo_rating"] == stored_elo
    assert profile["attack_rating"] == 0.6
    assert profile["defense_rating"] == 0.6


def test_result_update_regression(isolated_data_dir):
    """结果更新语义不变，只是新档案从确定性初始 Elo 起步。"""
    before = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")
    base = config.LEAGUE_STRENGTH[UNANCHORED_LEAGUE]
    assert before["elo_rating"] == round(
        base + stable_team_name_offset(UNANCHORED_LEAGUE, "甲队"), 1
    )

    update_from_result("甲队", "乙队", 3, 1, UNANCHORED_LEAGUE, "football")

    home = get_team_profile("甲队", UNANCHORED_LEAGUE, "football")
    away = get_team_profile("乙队", UNANCHORED_LEAGUE, "football")

    assert home["matches_played"] == 1
    assert home["goals_for"] == 3 and home["goals_against"] == 1
    assert away["goals_for"] == 1 and away["goals_against"] == 3
    assert home["elo_rating"] != before["elo_rating"]
    assert home["estimated"] is False


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

def test_current_model_versions(isolated_data_dir):
    assert config.MODEL_VERSIONS["football"] == "football-coldstart-1"
    assert config.MODEL_VERSIONS["basketball"] == "basketball-coldstart-1"
    assert config.MODEL_VERSION == "baseline-1"


def test_snapshot_version_propagation(isolated_data_dir, make_match):
    football, _c1 = capture_snapshot(enrich_match(make_match(id="m-fb")))
    basketball, _c2 = capture_snapshot(
        enrich_match(make_match(id="m-bb", sport="basketball", league="NBA",
                                home="湖人", away="凯尔特人",
                                odds={"home_win": 1.80, "away_win": 2.00}))
    )

    assert football["model_version"] == "football-coldstart-1"
    assert basketball["model_version"] == "basketball-coldstart-1"


def test_historical_snapshots_preserved(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-1"))
    old_football, _a = capture_snapshot(match, model_version="football-ad-1")
    old_basketball, _b = capture_snapshot(
        enrich_match(make_match(id="m-bb", sport="basketball", league="NBA",
                                home="湖人", away="凯尔特人",
                                odds={"home_win": 1.80, "away_win": 2.00})),
        model_version="baseline-1",
    )
    before_football = dict(old_football)
    before_basketball = dict(old_basketball)

    current, created = capture_snapshot(enrich_match(make_match(id="m-1")))

    assert created is True
    assert current["model_version"] == "football-coldstart-1"
    stored = {s["model_version"]: s for s in get_snapshots_for_match("m-1")}
    assert stored["football-ad-1"] == before_football
    assert stored["football-coldstart-1"]["snapshot_id"] == current["snapshot_id"]

    basketball_stored = get_snapshots_for_match("m-bb")
    assert basketball_stored == [before_basketball]


@pytest.mark.parametrize("status", ["live", "finished"])
def test_started_match_not_backfilled(isolated_data_dir, make_match, status):
    from datetime import timedelta

    from utils.daily_loader import get_match_datetime

    match = enrich_match(make_match(id="m-1"))
    capture_snapshot(match, model_version="football-ad-1")
    kickoff = get_match_datetime(match)

    later = kickoff + timedelta(minutes=10 if status == "live" else 200)
    score = {"ft": [2, 1]} if status == "finished" else None
    result, created = capture_snapshot(dict(match, status=status, score=score), now=later)

    assert created is False
    assert result is None
    assert [s["model_version"] for s in get_snapshots_for_match("m-1")] == ["football-ad-1"]


# ---------------------------------------------------------------------------
# 下游分组
# ---------------------------------------------------------------------------

def _evaluation_row(make_match, *, match_id, version, sport="football"):
    from utils.evaluation_rows import capture_evaluation_row
    from utils.settlements import settle_snapshot

    overrides = {"id": match_id}
    if sport == "basketball":
        overrides.update(
            sport="basketball", league="NBA", home="湖人", away="凯尔特人",
            odds={"home_win": 1.80, "away_win": 2.00},
        )
        score = {"ft": [108, 101]}
    else:
        score = {"ft": [2, 1]}

    match = enrich_match(make_match(**overrides))
    snapshot, _c = capture_snapshot(match, model_version=version)
    settlement, _s = settle_snapshot(
        snapshot, dict(match, status="finished", date="2020-01-01", time="20:00", score=score)
    )
    capture_evaluation_row(snapshot, settlement)


def test_classification_grouping_regression(isolated_data_dir, make_match):
    from utils.evaluation_rows import get_all_evaluation_rows

    _evaluation_row(make_match, match_id="m-a", version="football-ad-1")
    _evaluation_row(make_match, match_id="m-b", version="football-coldstart-1")

    rows = get_all_evaluation_rows()
    versions = sorted(s["model_version"] for s in build_classification_summaries(rows))

    assert versions == ["football-ad-1", "football-coldstart-1"]


def test_calibration_grouping_regression(isolated_data_dir, make_match):
    from utils.evaluation_rows import get_all_evaluation_rows

    _evaluation_row(make_match, match_id="m-a", version="football-ad-1")
    _evaluation_row(make_match, match_id="m-b", version="football-coldstart-1")

    rows = get_all_evaluation_rows()
    versions = sorted(s["model_version"] for s in build_calibration_summaries(rows))

    assert versions == ["football-ad-1", "football-coldstart-1"]


def test_basketball_grouping_regression(isolated_data_dir, make_match):
    from utils.evaluation_rows import get_all_evaluation_rows

    _evaluation_row(make_match, match_id="m-b1", version="baseline-1", sport="basketball")
    _evaluation_row(make_match, match_id="m-b2", version="basketball-coldstart-1",
                    sport="basketball")

    rows = get_all_evaluation_rows()
    versions = sorted(s["model_version"] for s in build_classification_summaries(rows))

    assert versions == ["baseline-1", "basketball-coldstart-1"]


# ---------------------------------------------------------------------------
# 市场准入回归
# ---------------------------------------------------------------------------

def test_market_admission_regression(isolated_data_dir, monkeypatch):
    from utils import fetcher_500, scraper

    def fake_live(sport):
        return [{
            "id": "m-unpriced", "sport": "football", "league": "英超",
            "date": "2030-03-01", "time": "20:00", "status": "upcoming",
            "home": "A", "away": "B", "home_rank": None, "away_rank": None,
            "score": None, "odds": None,
        }]

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", fake_live)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])

    result = scraper.refresh(verbose=False)

    assert result["prediction_snapshots_added"] == 0
    assert load_json("daily_matches.json")["matches"] == []
    assert get_snapshots_for_match("m-unpriced") == []


# ---------------------------------------------------------------------------
# 纯度
# ---------------------------------------------------------------------------

def test_helper_does_not_depend_on_mutable_state(isolated_data_dir, monkeypatch, tmp_path):
    """纯函数：与 DATA_DIR / 缓存状态无关。"""
    first = stable_team_name_offset("英超", "测试队A")

    _use_store(monkeypatch, tmp_path / "store-pure")

    assert stable_team_name_offset("英超", "测试队A") == first == -43
    assert copy.deepcopy(GOLDEN_VECTORS)[0][2] == -43
