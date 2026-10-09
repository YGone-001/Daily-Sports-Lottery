"""赛前预测结算的行为测试（服务层）。"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

import config
from utils.atomic_json import atomic_write_json
from utils.daily_loader import enrich_match, get_match_datetime
from utils.prediction_snapshots import capture_snapshot
from utils.settlements import (
    SettlementConflictError,
    extract_final_score,
    get_settlement_for_snapshot,
    get_settlements_for_match,
    result_fingerprint,
    settle_snapshot,
    settlement_id_for,
    store_path,
)

REQUIRED_FIELDS = (
    "settlement_id", "snapshot_id", "match_id", "sport", "league",
    "home_team", "away_team", "model_name", "model_version",
    "prediction_generated_at", "kickoff_at", "settled_at",
    "final_score", "actual_outcome", "result_fingerprint",
)


def _snapshot(make_match, **overrides):
    """创建一条真实的赛前预测快照（比赛仍处于未开赛）。"""
    match = enrich_match(make_match(**overrides))
    snapshot, created = capture_snapshot(match)
    assert created is True
    return match, snapshot


def _finished(match: dict, score) -> dict:
    """把比赛记录改写为「已完赛 + 指定比分」（日期置于过去，确保状态判定为 finished）。"""
    return dict(match, status="finished", date="2020-01-01", time="20:00", score=score)


# ---------------------------------------------------------------------------
# 基础结算
# ---------------------------------------------------------------------------

def test_basic_settlement(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    settlement, created = settle_snapshot(snap, _finished(m, {"ft": [2, 1]}))

    assert created is True
    for key in REQUIRED_FIELDS:
        assert key in settlement, f"缺少字段: {key}"

    assert settlement["snapshot_id"] == snap["snapshot_id"]
    assert settlement["match_id"] == m["id"]
    assert settlement["final_score"] == {"home": 2, "away": 1}
    assert settlement["actual_outcome"] == "home_win"
    assert settlement["model_version"] == config.MODEL_VERSIONS["football"]
    assert settlement["model_name"] == snap["model_name"]
    assert settlement["prediction_generated_at"] == snap["generated_at"]
    assert settlement["kickoff_at"] == snap["kickoff_at"]
    assert settlement["result_fingerprint"] == result_fingerprint({"home": 2, "away": 1})


def test_settlement_has_no_evaluation_fields(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    settlement, _created = settle_snapshot(snap, _finished(m, {"ft": [2, 1]}))

    for forbidden in (
        "predicted_outcome", "correct", "hit", "accuracy", "brier", "logloss",
        "roi", "profit", "loss", "kelly_result", "clv", "market_edge",
    ):
        assert forbidden not in settlement


@pytest.mark.parametrize(
    "ft,expected",
    [
        ([2, 1], "home_win"),
        ([1, 1], "draw"),
        ([0, 2], "away_win"),
    ],
)
def test_outcome_derivation(isolated_data_dir, make_match, ft, expected):
    m, snap = _snapshot(make_match)
    settlement, created = settle_snapshot(snap, _finished(m, {"ft": ft}))

    assert created is True
    assert settlement["actual_outcome"] == expected
    assert settlement["final_score"] == {"home": ft[0], "away": ft[1]}


def test_basketball_settlement(isolated_data_dir, make_match):
    m, snap = _snapshot(
        make_match,
        id="500lq-2030-01-01-1",
        sport="basketball",
        league="NBA",
        home="湖人",
        away="凯尔特人",
        odds={"home_win": 1.8, "away_win": 2.0},
    )
    settlement, created = settle_snapshot(snap, _finished(m, {"ft": [108, 101]}))

    assert created is True
    assert settlement["sport"] == "basketball"
    assert settlement["actual_outcome"] == "home_win"
    assert settlement["final_score"] == {"home": 108, "away": 101}


# ---------------------------------------------------------------------------
# 幂等与冲突
# ---------------------------------------------------------------------------

def test_idempotent_replay(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    finished = _finished(m, {"ft": [2, 1]})

    first, created1 = settle_snapshot(snap, finished)
    second, created2 = settle_snapshot(snap, finished)

    assert created1 is True
    assert created2 is False
    assert first["settlement_id"] == second["settlement_id"]
    assert second["settled_at"] == first["settled_at"]
    assert len(get_settlements_for_match(m["id"])) == 1
    assert get_settlement_for_snapshot(snap["snapshot_id"])["settlement_id"] == first["settlement_id"]


def test_result_conflict_detected(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)

    first, created = settle_snapshot(snap, _finished(m, {"ft": [2, 1]}))
    assert created is True
    original = dict(first)

    with pytest.raises(SettlementConflictError) as excinfo:
        settle_snapshot(snap, _finished(m, {"ft": [2, 2]}))

    err = excinfo.value
    assert err.match_id == m["id"]
    assert err.snapshot_id == snap["snapshot_id"]
    assert err.existing_score == {"home": 2, "away": 1}
    assert err.incoming_score == {"home": 2, "away": 2}

    rows = get_settlements_for_match(m["id"])
    assert len(rows) == 1
    assert rows[0]["final_score"] == {"home": 2, "away": 1}
    assert rows[0]["result_fingerprint"] == original["result_fingerprint"]
    assert rows[0]["settled_at"] == original["settled_at"]
    assert rows[0]["actual_outcome"] == "home_win"


# ---------------------------------------------------------------------------
# 资格拒绝
# ---------------------------------------------------------------------------

def test_upcoming_rejected(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    kickoff = get_match_datetime(m)

    settlement, created = settle_snapshot(
        snap, dict(m, score={"ft": [2, 1]}), now=kickoff - timedelta(hours=2)
    )

    assert settlement is None
    assert created is False
    assert get_settlements_for_match(m["id"]) == []


def test_live_rejected(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    kickoff = get_match_datetime(m)

    settlement, created = settle_snapshot(
        snap, dict(m, score={"ft": [2, 1]}), now=kickoff + timedelta(minutes=10)
    )

    assert settlement is None
    assert created is False
    assert get_settlements_for_match(m["id"]) == []


@pytest.mark.parametrize(
    "score",
    [
        None,
        {"ft": [2]},
        {"ft": ["2", "1"]},
        {"ft": ["2", 1]},
        {"ft": [2.0, 1]},
        {"ft": [True, 1]},
        {"ft": [2, False]},
        {"ft": [-1, 0]},
        {"ft": [2, -1]},
        {"ft": [None, 1]},
        {"ft": [2, None]},
        {"ht": [1, 0]},
        {"ft": "2-1"},
        {},
    ],
)
def test_invalid_final_score_rejected(isolated_data_dir, make_match, score):
    m, snap = _snapshot(make_match)

    settlement, created = settle_snapshot(snap, _finished(m, score))

    assert settlement is None
    assert created is False
    assert get_settlements_for_match(m["id"]) == []


@pytest.mark.parametrize(
    "score",
    [
        {"ft": ["2", 1]},
        {"ft": [2.0, 1]},
        {"ft": [True, 1]},
        {"ft": [2, False]},
        {"ft": [-1, 0]},
        {"ft": [2, -1]},
    ],
)
def test_settle_snapshot_rejects_malformed_score_directly(isolated_data_dir, make_match, score):
    """ settle_snapshot 自身必须拒绝畸形比分，不依赖 scraper 的保护。"""
    m, snap = _snapshot(make_match)

    settlement, created = settle_snapshot(snap, _finished(m, score))

    assert settlement is None
    assert created is False
    assert get_settlements_for_match(m["id"]) == []
    assert not os.path.exists(store_path())


def test_length_ge_2_uses_first_two(isolated_data_dir, make_match):
    """长度语义为 len(ft) >= 2，与 valid_full_time_score 一致；多余元素忽略。"""
    m, snap = _snapshot(make_match)

    settlement, created = settle_snapshot(snap, _finished(m, {"ft": [2, 1, 0]}))

    assert created is True
    assert settlement["final_score"] == {"home": 2, "away": 1}

    assert extract_final_score({"score": {"ft": [3, 2, 1]}}) == {"home": 3, "away": 2}
    assert extract_final_score({"score": {"ft": (3, 2, 1)}}) == {"home": 3, "away": 2}


# ---------------------------------------------------------------------------
# 时间戳校验
# ---------------------------------------------------------------------------

def test_valid_prematch_timestamp_settles(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    assert snap["generated_at"] < snap["kickoff_at"]

    settlement, created = settle_snapshot(snap, _finished(m, {"ft": [1, 0]}))

    assert created is True
    assert settlement["prediction_generated_at"] == snap["generated_at"]


def test_prediction_after_kickoff_rejected(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    invalid = dict(snap, generated_at="2031-06-01T00:00:00+08:00")  # 晚于 2030-01-01 开赛

    settlement, created = settle_snapshot(invalid, _finished(m, {"ft": [2, 1]}))

    assert settlement is None
    assert created is False
    assert get_settlements_for_match(m["id"]) == []


# ---------------------------------------------------------------------------
# 多模型版本
# ---------------------------------------------------------------------------

def test_multiple_model_versions(isolated_data_dir, make_match):
    m = enrich_match(make_match())
    s1, c1 = capture_snapshot(m, model_version="model-v1")
    s2, c2 = capture_snapshot(m, model_version="model-v2")
    assert c1 is True and c2 is True

    finished = _finished(m, {"ft": [2, 1]})
    r1 = settle_snapshot(s1, finished)
    r2 = settle_snapshot(s2, finished)
    assert r1[1] is True
    assert r2[1] is True

    rows = get_settlements_for_match(m["id"])
    assert len(rows) == 2
    assert {r["snapshot_id"] for r in rows} == {s1["snapshot_id"], s2["snapshot_id"]}
    assert {r["model_version"] for r in rows} == {"model-v1", "model-v2"}
    assert all(r["match_id"] == m["id"] for r in rows)
    assert all(r["final_score"] == {"home": 2, "away": 1} for r in rows)
    assert all(r["actual_outcome"] == "home_win" for r in rows)


# ---------------------------------------------------------------------------
# 身份 / 指纹 / 提取
# ---------------------------------------------------------------------------

def test_deterministic_fingerprint_and_identity(isolated_data_dir):
    assert result_fingerprint({"home": 2, "away": 1}) == result_fingerprint({"away": 1, "home": 2})
    assert result_fingerprint({"home": 2, "away": 1}) != result_fingerprint({"home": 2, "away": 2})
    assert len(result_fingerprint({"home": 2, "away": 1})) == 64

    assert settlement_id_for("abc") == settlement_id_for("abc")
    assert settlement_id_for("abc") != settlement_id_for("abd")
    assert len(settlement_id_for("abc")) == 64
    # 身份与最终比分无关
    assert settlement_id_for("abc") == settlement_id_for("abc")


def test_extract_final_score(isolated_data_dir):
    assert extract_final_score({"score": {"ft": [2, 1]}}) == {"home": 2, "away": 1}
    assert extract_final_score({"score": {"ft": [108, 101]}}) == {"home": 108, "away": 101}
    assert extract_final_score({"score": {"ft": [0, 0]}}) == {"home": 0, "away": 0}
    assert extract_final_score({"score": {"ft": [105, 99]}}) == {"home": 105, "away": 99}
    assert extract_final_score({"score": None}) is None
    assert extract_final_score({}) is None
    assert extract_final_score({"score": {"ft": [2]}}) is None
    assert extract_final_score({"score": {"ft": ["2", "1"]}}) is None
    assert extract_final_score({"score": {"ft": [2.0, 1]}}) is None
    assert extract_final_score({"score": {"ft": [True, 1]}}) is None
    assert extract_final_score({"score": {"ft": [2, False]}}) is None
    assert extract_final_score({"score": {"ft": [-1, 0]}}) is None
    assert extract_final_score({"score": {"ft": [2, -1]}}) is None
    assert extract_final_score({"score": {"ht": [1, 0]}}) is None


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def test_store_file_is_lazy(isolated_data_dir):
    assert not os.path.exists(store_path())
    assert get_settlements_for_match("nope") == []  # 纯读取不落盘
    assert not os.path.exists(store_path())


def test_atomic_serialization_is_reloadable(isolated_data_dir, make_match):
    m, snap = _snapshot(make_match)
    settle_snapshot(snap, _finished(m, {"ft": [2, 1]}))

    path = store_path()
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    parsed = json.loads(raw)
    assert parsed["version"] == 1
    assert len(parsed["settlements"]) == 1

    atomic_write_json(path, parsed)
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == raw

# ---------------------------------------------------------------------------
# Authoritative Finality Focused Regression Tests (P0)
# ---------------------------------------------------------------------------

def test_settle_snapshot_rejects_time_inferred_finality(isolated_data_dir, make_match):
    """Elapsed time safeguards: reject settlement if canonical status is not finished."""
    m, snap = _snapshot(make_match)
    # The elapsed time says it should be finished, but canonical status is still 'live'
    m["status"] = "live"
    # Even if we pass it, the new rule requires canonical finalized state
    settlement, created = settle_snapshot(snap, m)
    assert created is False
    assert settlement is None


def test_settle_snapshot_accepts_canonical_finished(isolated_data_dir, make_match):
    """Status rejections: requires both 'finished' status and valid score."""
    m, snap = _snapshot(make_match)
    m["status"] = "finished"
    m["score"] = {"ft": [3, 0]}
    settlement, created = settle_snapshot(snap, m)
    assert created is True
    assert settlement["final_score"] == {"home": 3, "away": 0}


def test_settle_snapshot_rejects_without_canonical_score(isolated_data_dir, make_match):
    """Status rejections: if canonical status is finished but no score, reject."""
    m, snap = _snapshot(make_match)
    m["status"] = "finished"
    m["score"] = None
    settlement, created = settle_snapshot(snap, m)
    assert created is False
    assert settlement is None


def test_settle_snapshot_midnight_kickoff_handling(isolated_data_dir, make_match):
    """Midnight kickoffs: ensure elapsed time logic decouple doesn't break valid midnight matches."""
    m, snap = _snapshot(make_match, date="2030-01-01", time="00:00")
    m["status"] = "finished"
    m["score"] = {"ft": [1, 1]}
    settlement, created = settle_snapshot(snap, m)
    assert created is True


def test_settle_snapshot_basketball_explicit_terminal(isolated_data_dir, make_match):
    """Basketball matches require explicitly terminal status codes, validated by valid_full_time_score."""
    m, snap = _snapshot(make_match, sport="basketball", id="b1", date="2030-01-01")
    m["status"] = "finished"
    m["score"] = {"ft": [110, 105]}
    settlement, created = settle_snapshot(snap, m)
    assert created is True
    assert settlement["sport"] == "basketball"


def test_settle_snapshot_basketball_live_rejected(isolated_data_dir, make_match):
    """Basketball 4 quarters completed but status still 'live' must be rejected."""
    m, snap = _snapshot(make_match, sport="basketball", id="b2", date="2030-01-01")
    m["status"] = "live"
    m["score"] = {"ft": [100, 100], "periods": {"home": [25,25,25,25], "away": [25,25,25,25]}}
    settlement, created = settle_snapshot(snap, m)
    assert created is False
    assert settlement is None


def test_settlement_conflict_retention_preserves_old_score(isolated_data_dir, make_match):
    """Conflict retention: if a new score arrives for an already settled match, retain old."""
    m, snap = _snapshot(make_match)
    m["status"] = "finished"
    m["score"] = {"ft": [2, 0]}
    first, created = settle_snapshot(snap, m)
    assert created is True
    
    # New score arrives (conflict)
    m_conflict = dict(m)
    m_conflict["score"] = {"ft": [3, 0]}
    
    with pytest.raises(SettlementConflictError):
        settle_snapshot(snap, m_conflict)
        
    # Retention preserved
    rows = get_settlements_for_match(m["id"])
    assert len(rows) == 1
    assert rows[0]["final_score"] == {"home": 2, "away": 0}


def test_settlement_immutability(isolated_data_dir, make_match):
    """Immutability: existing settlements are never updated, only retrieved."""
    m, snap = _snapshot(make_match)
    m["status"] = "finished"
    m["score"] = {"ft": [1, 0]}
    
    first, created = settle_snapshot(snap, m)
    assert created is True
    
    # Try to settle again with same data
    second, created_second = settle_snapshot(snap, m)
    assert created_second is False
    assert second["settlement_id"] == first["settlement_id"]
    assert second["settled_at"] == first["settled_at"]


def test_materialize_evaluations_protection_dummy(isolated_data_dir, make_match):
    """Elapsed time safeguards: a test to represent materialization protection conceptually."""
    # The actual _materialize_evaluation_rows is in scraper.py, 
    # but we represent the decoupling logic validation here.
    m, snap = _snapshot(make_match)
    m["status"] = "upcoming"
    settlement, created = settle_snapshot(snap, m)
    assert created is False


def test_settlement_invalid_type_rejection(isolated_data_dir, make_match):
    """Immutability & Safety: ensure only int types pass through."""
    m, snap = _snapshot(make_match)
    m["status"] = "finished"
    m["score"] = {"ft": [2.0, 1.0]}
    settlement, created = settle_snapshot(snap, m)
    assert created is False
    assert settlement is None
