"""评估样本的行为测试（服务层）。"""
from __future__ import annotations

import json
import os

import pytest

from utils.atomic_json import atomic_write_json
from utils.daily_loader import enrich_match
from utils.evaluation_rows import (
    EvaluationIntegrityError,
    capture_evaluation_row,
    evaluation_id_for,
    get_all_evaluation_rows,
    get_evaluation_row,
    get_evaluation_row_for_snapshot,
    get_evaluation_rows_for_match,
    source_fingerprint,
    store_path,
)
from utils.prediction_snapshots import capture_snapshot
from utils.settlements import settle_snapshot
from utils.team_strength import get_team_profile, update_from_result

REQUIRED_FIELDS = (
    "evaluation_id", "snapshot_id", "settlement_id", "match_id",
    "sport", "league", "home_team", "away_team",
    "model_name", "model_version",
    "prediction_generated_at", "kickoff_at", "settled_at",
    "home_elo", "away_elo",
    "model_probabilities", "display_probabilities",
    "market_odds", "market_implied_probabilities",
    "expected_values", "expected_score_data",
    "final_score", "actual_outcome", "result_fingerprint",
    "source_fingerprint", "materialized_at",
)

FORBIDDEN_FIELDS = (
    "correct", "hit", "accuracy", "brier", "brier_score",
    "logloss", "log_loss", "roi", "profit", "loss", "clv",
    "predicted_outcome", "model_pick", "recommended_side",
)


def _pair(make_match, *, match_id="m-eval", ft=(2, 1), model_version=None, **overrides):
    """构造一条真实的「预测快照 + 结算」组合。"""
    match = enrich_match(make_match(id=match_id, **overrides))
    snapshot, created = capture_snapshot(match, model_version=model_version)
    assert created is True
    finished = dict(match, status="finished", date="2020-01-01", time="20:00", score={"ft": list(ft)})
    settlement, settled = settle_snapshot(snapshot, finished)
    assert settled is True
    return match, snapshot, settlement


# ---------------------------------------------------------------------------
# 基础物化
# ---------------------------------------------------------------------------

def test_basic_materialization(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)
    row, created = capture_evaluation_row(snapshot, settlement)

    assert created is True
    for key in REQUIRED_FIELDS:
        assert key in row, f"缺少字段: {key}"

    assert row["snapshot_id"] == snapshot["snapshot_id"]
    assert row["settlement_id"] == settlement["settlement_id"]
    assert row["match_id"] == snapshot["match_id"]
    assert row["evaluation_id"] == evaluation_id_for(snapshot["snapshot_id"], settlement["settlement_id"])
    assert row["source_fingerprint"]
    assert row["materialized_at"]
    assert len(get_evaluation_rows_for_match(snapshot["match_id"])) == 1


def test_historical_fields_copied_exactly(isolated_data_dir, make_match):
    """
    快照之后当前球队实力发生变化，评估样本必须仍保留**历史**快照值
    （证明未读取当前状态、未重跑模型）。
    """
    _match, snapshot, settlement = _pair(make_match)
    row, created = capture_evaluation_row(snapshot, settlement)
    assert created is True

    update_from_result("曼城", "阿森纳", 6, 0, "英超", "football")

    stored = get_evaluation_row(row["evaluation_id"])
    assert stored["home_elo"] == snapshot["home_elo"]
    assert stored["away_elo"] == snapshot["away_elo"]
    assert stored["model_probabilities"] == snapshot["model_probabilities"]
    assert stored["display_probabilities"] == snapshot["display_probabilities"]
    assert stored["market_odds"] == snapshot["market_odds"]
    assert stored["market_implied_probabilities"] == snapshot["market_implied_probabilities"]
    assert stored["expected_values"] == snapshot["expected_values"]
    assert stored["expected_score_data"] == snapshot["expected_score_data"]

    # 当前实力已变，历史值必须与之不同
    assert stored["home_elo"] != get_team_profile("曼城", "英超", "football")["elo_rating"]


def test_result_fields_copied_from_settlement(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match, ft=(0, 2))
    row, _created = capture_evaluation_row(snapshot, settlement)

    assert row["final_score"] == settlement["final_score"] == {"home": 0, "away": 2}
    assert row["actual_outcome"] == settlement["actual_outcome"] == "away_win"
    assert row["result_fingerprint"] == settlement["result_fingerprint"]
    assert row["settled_at"] == settlement["settled_at"]


def test_prediction_timestamp_mapping(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)
    row, _created = capture_evaluation_row(snapshot, settlement)

    assert row["prediction_generated_at"] == snapshot["generated_at"]
    assert row["kickoff_at"] == snapshot["kickoff_at"]


def test_idempotency(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)

    first, created1 = capture_evaluation_row(snapshot, settlement)
    second, created2 = capture_evaluation_row(snapshot, settlement)

    assert created1 is True
    assert created2 is False
    assert first["evaluation_id"] == second["evaluation_id"]
    assert second["materialized_at"] == first["materialized_at"]
    assert len(get_evaluation_rows_for_match(snapshot["match_id"])) == 1


# ---------------------------------------------------------------------------
# 拼接 / 溯源校验
# ---------------------------------------------------------------------------

def test_join_mismatch_snapshot_id(isolated_data_dir, make_match):
    _m1, snapshot_a, _s_a = _pair(make_match, match_id="m-a")
    _m2, _snapshot_b, settlement_b = _pair(make_match, match_id="m-b", date="2030-02-01")

    with pytest.raises(EvaluationIntegrityError) as excinfo:
        capture_evaluation_row(snapshot_a, settlement_b)

    assert excinfo.value.reason == "snapshot_id_mismatch"
    assert get_evaluation_rows_for_match("m-a") == []
    assert get_all_evaluation_rows() == []


def test_join_mismatch_match_id(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)

    with pytest.raises(EvaluationIntegrityError) as excinfo:
        capture_evaluation_row(snapshot, dict(settlement, match_id="somewhere-else"))

    assert excinfo.value.reason == "match_id_mismatch"
    assert get_all_evaluation_rows() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_version", "tampered-version"),
        ("model_name", "tampered-model"),
        ("prediction_generated_at", "2029-01-01T00:00:00+08:00"),
        ("kickoff_at", "2031-01-01T20:00:00+08:00"),
    ],
)
def test_provenance_mismatch(isolated_data_dir, make_match, field, value):
    _match, snapshot, settlement = _pair(make_match)

    with pytest.raises(EvaluationIntegrityError) as excinfo:
        capture_evaluation_row(snapshot, dict(settlement, **{field: value}))

    assert excinfo.value.reason == "provenance_mismatch"
    assert get_all_evaluation_rows() == []


def test_invalid_prematch_timestamp_rejected(isolated_data_dir, make_match):
    """生成时间晚于开赛时间的快照不得作为有效赛前证据物化。"""
    _match, snapshot, settlement = _pair(make_match)
    late = "2031-06-01T00:00:00+08:00"

    invalid_snapshot = dict(snapshot, generated_at=late)
    invalid_settlement = dict(settlement, prediction_generated_at=late)

    with pytest.raises(EvaluationIntegrityError) as excinfo:
        capture_evaluation_row(invalid_snapshot, invalid_settlement)

    assert excinfo.value.reason == "invalid_prematch_timestamp"
    assert get_all_evaluation_rows() == []


# ---------------------------------------------------------------------------
# 源指纹与完整性冲突
# ---------------------------------------------------------------------------

def test_source_fingerprint_stability(isolated_data_dir):
    a = {"home_win": 0.55, "draw": 0.25, "away_win": 0.20}
    b = {"away_win": 0.20, "home_win": 0.55, "draw": 0.25}
    c = {"home_win": 0.56, "draw": 0.25, "away_win": 0.20}

    assert source_fingerprint(a) == source_fingerprint(b)
    assert source_fingerprint(a) != source_fingerprint(c)
    assert len(source_fingerprint(a)) == 64
    assert source_fingerprint(a) == source_fingerprint(a)


def test_integrity_conflict(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)
    row, created = capture_evaluation_row(snapshot, settlement)
    assert created is True
    original = dict(row)

    # 同一 evaluation_id，但源内容被改动
    tampered = dict(settlement, final_score={"home": 9, "away": 9})
    with pytest.raises(EvaluationIntegrityError) as excinfo:
        capture_evaluation_row(snapshot, tampered)

    err = excinfo.value
    assert err.reason == "source_fingerprint_conflict"
    assert err.evaluation_id == row["evaluation_id"]
    assert err.snapshot_id == snapshot["snapshot_id"]
    assert err.settlement_id == settlement["settlement_id"]
    assert err.match_id == snapshot["match_id"]

    assert len(get_evaluation_rows_for_match(snapshot["match_id"])) == 1
    after = get_evaluation_row(row["evaluation_id"])
    assert after["final_score"] == {"home": 2, "away": 1}
    assert after["source_fingerprint"] == original["source_fingerprint"]
    assert after["materialized_at"] == original["materialized_at"]


# ---------------------------------------------------------------------------
# 缺失一方 -> 不物化
# ---------------------------------------------------------------------------

def test_no_settlement_produces_nothing(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-nosettle"))
    snapshot, created = capture_snapshot(match)
    assert created is True

    assert capture_evaluation_row(snapshot, None) == (None, False)
    assert capture_evaluation_row(None, {"settlement_id": "x"}) == (None, False)
    assert get_evaluation_rows_for_match("m-nosettle") == []


# ---------------------------------------------------------------------------
# 运动类型 / 多模型版本
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides,ft,expected",
    [
        ({}, (2, 1), "home_win"),
        (
            {"id": "500lq-2030-01-01-1", "sport": "basketball", "league": "NBA",
             "home": "湖人", "away": "凯尔特人", "odds": {"home_win": 1.8, "away_win": 2.0}},
            (108, 101),
            "home_win",
        ),
    ],
)
def test_football_and_basketball(isolated_data_dir, make_match, overrides, ft, expected):
    opts = dict(overrides)
    match_id = opts.pop("id", "m-football")
    _match, snapshot, settlement = _pair(make_match, match_id=match_id, ft=ft, **opts)
    row, created = capture_evaluation_row(snapshot, settlement)

    assert created is True
    assert row["sport"] == snapshot["sport"]
    assert row["final_score"] == {"home": ft[0], "away": ft[1]}
    assert row["actual_outcome"] == expected


def test_multiple_model_versions(isolated_data_dir, make_match):
    match = enrich_match(make_match(id="m-multi"))
    s_a, c_a = capture_snapshot(match, model_version="model-A")
    s_b, c_b = capture_snapshot(match, model_version="model-B")
    assert c_a is True and c_b is True

    finished = dict(match, status="finished", date="2020-01-01", time="20:00", score={"ft": [2, 1]})
    settle_a, sa = settle_snapshot(s_a, finished)
    settle_b, sb = settle_snapshot(s_b, finished)
    assert sa is True and sb is True

    r_a, ca = capture_evaluation_row(s_a, settle_a)
    r_b, cb = capture_evaluation_row(s_b, settle_b)
    assert ca is True and cb is True

    rows = get_evaluation_rows_for_match("m-multi")
    assert len(rows) == 2
    assert {r["model_version"] for r in rows} == {"model-A", "model-B"}
    assert {r["snapshot_id"] for r in rows} == {s_a["snapshot_id"], s_b["snapshot_id"]}
    assert {r["settlement_id"] for r in rows} == {settle_a["settlement_id"], settle_b["settlement_id"]}
    assert len({r["evaluation_id"] for r in rows}) == 2
    assert all(r["match_id"] == "m-multi" for r in rows)
    assert all(r["final_score"] == {"home": 2, "away": 1} for r in rows)


# ---------------------------------------------------------------------------
# 不含指标字段
# ---------------------------------------------------------------------------

def test_no_metric_fields(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)
    row, _created = capture_evaluation_row(snapshot, settlement)

    lowered = {key.lower() for key in row}
    for name in FORBIDDEN_FIELDS:
        assert name not in lowered, f"不应出现指标字段: {name}"


# ---------------------------------------------------------------------------
# 查询与持久化
# ---------------------------------------------------------------------------

def test_query_helpers_and_ordering(isolated_data_dir, make_match):
    _m1, s1, st1 = _pair(make_match, match_id="m-q1")
    _m2, s2, st2 = _pair(make_match, match_id="m-q2", date="2030-02-01", home="利物浦", away="切尔西")
    capture_evaluation_row(s1, st1)
    capture_evaluation_row(s2, st2)

    assert get_evaluation_row_for_snapshot(s1["snapshot_id"])["match_id"] == "m-q1"
    assert get_evaluation_row_for_snapshot("unknown") is None

    all_rows = get_all_evaluation_rows()
    assert len(all_rows) == 2
    keys = [
        (r["prediction_generated_at"], r["match_id"], r["model_version"], r["evaluation_id"])
        for r in all_rows
    ]
    assert keys == sorted(keys)


def test_store_file_is_lazy(isolated_data_dir):
    assert not os.path.exists(store_path())
    assert get_all_evaluation_rows() == []
    assert get_evaluation_rows_for_match("nope") == []
    assert not os.path.exists(store_path())


def test_atomic_serialization_is_reloadable(isolated_data_dir, make_match):
    _match, snapshot, settlement = _pair(make_match)
    capture_evaluation_row(snapshot, settlement)

    path = store_path()
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    parsed = json.loads(raw)
    assert parsed["version"] == 1
    assert len(parsed["rows"]) == 1

    atomic_write_json(path, parsed)
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == raw


def test_deterministic_evaluation_id(isolated_data_dir):
    assert evaluation_id_for("s1", "t1") == evaluation_id_for("s1", "t1")
    assert evaluation_id_for("s1", "t1") != evaluation_id_for("s1", "t2")
    assert evaluation_id_for("s1", "t1") != evaluation_id_for("s2", "t1")
    assert len(evaluation_id_for("s1", "t1")) == 64
