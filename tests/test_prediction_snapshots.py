"""赛前预测快照的行为测试。"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import config
from utils.daily_loader import enrich_match, get_match_datetime
from utils.prediction_snapshots import (
    SNAPSHOT_SLOT,
    create_snapshot,
    ensure_snapshot,
    get_snapshot,
    get_snapshots_for_match,
    snapshot_exists,
    snapshot_id_for,
    store_path,
)

REQUIRED_FIELDS = (
    "snapshot_id",
    "match_id",
    "sport",
    "league",
    "home_team",
    "away_team",
    "match_date",
    "match_time",
    "kickoff_at",
    "generated_at",
    "model_name",
    "model_version",
    "home_elo",
    "away_elo",
    "model_probabilities",
    "display_probabilities",
    "expected_values",
    "market_odds",
    "market_implied_probabilities",
    "expected_score_data",
)


def test_snapshot_created_for_upcoming_match(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    snap = ensure_snapshot(m)

    assert snap is not None
    assert snap["match_id"] == m["id"]
    assert snap["model_version"] == config.MODEL_VERSION
    assert os.path.exists(store_path())
    assert len(get_snapshots_for_match(m["id"])) == 1
    assert snapshot_exists(m["id"]) is True


def test_snapshot_schema_fields(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    snap = ensure_snapshot(m)

    for key in REQUIRED_FIELDS:
        assert key in snap, f"缺少字段: {key}"

    assert snap["home_elo"] is not None and snap["away_elo"] is not None
    assert set(snap["expected_score_data"]) >= {"expected_goals", "top_scores", "goals_prediction"}


def test_basketball_schema(isolated_data_dir, make_match):
    m = enrich_match(
        make_match(
            id="500lq-2030-01-01-0",
            sport="basketball",
            league="NBA",
            home="湖人",
            away="凯尔特人",
            odds={"home_win": 1.8, "away_win": 2.0, "total_line": 220.5},
        )
    )
    snap = ensure_snapshot(m)

    assert snap["sport"] == "basketball"
    assert set(snap["expected_score_data"]) >= {
        "expected_home_points",
        "expected_away_points",
        "expected_total",
        "spread",
        "over_line",
        "over_prob",
        "under_prob",
    }


def test_snapshot_identity_is_deterministic(isolated_data_dir):
    a = snapshot_id_for("m1", "baseline-1")
    b = snapshot_id_for("m1", "baseline-1")
    assert a == b
    assert len(a) == 64  # sha256 hexdigest
    assert snapshot_id_for("m1", "baseline-2") != a
    assert snapshot_id_for("m2", "baseline-1") != a
    assert SNAPSHOT_SLOT == "prematch"


def test_idempotent_snapshot(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    first = ensure_snapshot(m)

    for _ in range(4):
        again = ensure_snapshot(enrich_match(make_match()))
        assert again["snapshot_id"] == first["snapshot_id"]
        assert again["generated_at"] == first["generated_at"]

    assert len(get_snapshots_for_match(m["id"])) == 1


def test_snapshot_immutable_after_elo_change(isolated_data_dir, make_match):
    from utils import team_strength

    m = enrich_match(make_match())
    original = ensure_snapshot(m)
    assert original is not None

    # 赛后更新 Elo（模拟 23:00 的实力变更）
    team_strength.update_from_result("曼城", "阿森纳", 5, 0, "英超", "football")

    later = ensure_snapshot(enrich_match(make_match()))
    assert later["home_elo"] == original["home_elo"]
    assert later["generated_at"] == original["generated_at"]
    assert later["display_probabilities"] == original["display_probabilities"]

    stored = get_snapshot(original["snapshot_id"])
    assert stored["home_elo"] == original["home_elo"]


def test_live_match_rejected(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    live_now = get_match_datetime(m) + timedelta(minutes=10)

    assert create_snapshot(m, now=live_now) is None
    assert get_snapshots_for_match(m["id"]) == []


def test_finished_match_rejected(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    finished_now = get_match_datetime(m) + timedelta(minutes=200)

    assert create_snapshot(m, now=finished_now) is None
    assert get_snapshots_for_match(m["id"]) == []


def test_existing_snapshot_readable_after_finish(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    snap = ensure_snapshot(m)
    finished_now = get_match_datetime(m) + timedelta(minutes=200)

    # 状态已完赛，但既有快照仍可读且不被改写
    again = create_snapshot(enrich_match(make_match()), now=finished_now)
    assert again["snapshot_id"] == snap["snapshot_id"]
    assert get_snapshot(snap["snapshot_id"]) is not None


def test_stable_serialization(isolated_data_dir, make_match):
    from utils.atomic_json import atomic_write_json

    m = enrich_match(make_match())
    ensure_snapshot(m)

    path = store_path()
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()

    parsed = json.loads(raw)  # 有效 JSON 且可重载
    assert "snapshots" in parsed
    assert len(parsed["snapshots"]) == 1

    atomic_write_json(path, parsed)  # 重新落盘
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == raw  # sort_keys 保证字节级稳定


def test_store_file_is_lazy(isolated_data_dir):
    assert not os.path.exists(store_path())
    assert get_snapshots_for_match("nope") == []  # 纯读取不落盘
    assert not os.path.exists(store_path())
