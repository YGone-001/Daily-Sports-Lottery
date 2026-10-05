"""
`utils.evidence_status` 单元测试
================================
覆盖：空运行数据 / 仅快照 / 快照+结算 / 完整评估链路 / 当前版本 / 历史版本隔离 /
门禁边界 / 重复身份 / 卡住快照 / 无快照完赛 / 结算缺评估 / 未完赛不计失败 /
确定性顺序 / 输入不可变 / 既有评估器复用。

全部为纯内存 / 临时目录测试，**无任何网络依赖**。
"""
from __future__ import annotations

import copy

import pytest

import config
from utils import evidence_status as es

FOOT_N = "elo-poisson-dixon-coles"
BB_N = "elo-normal-points"
FOOT_V = config.MODEL_VERSIONS["football"]
BB_V = config.MODEL_VERSIONS["basketball"]


# ---------------------------------------------------------------------------
# 夹具构造
# ---------------------------------------------------------------------------

def make_snapshot(mid="m1", sport="football", version=FOOT_V, name=FOOT_N, sid=None,
                  generated="2030-01-01T10:00:00+08:00", kickoff="2030-01-01T20:00:00+08:00",
                  probs=None):
    return {
        "snapshot_id": sid or f"snap-{mid}-{version}",
        "match_id": mid, "sport": sport, "model_name": name, "model_version": version,
        "generated_at": generated, "kickoff_at": kickoff,
        "model_probabilities": probs or {"home_win": 50, "draw": 25, "away_win": 25},
        "display_probabilities": {"home_win": 48, "draw": 26, "away_win": 26},
        "market_implied_probabilities": {"home_win": 45, "draw": 27, "away_win": 28},
        "market_odds": {"home_win": 2.0, "draw": 3.4, "away_win": 3.6},
    }


def make_settlement(mid="m1", sport="football", version=FOOT_V, name=FOOT_N,
                    sid=None, setid=None, outcome="home_win"):
    sid = sid or f"snap-{mid}-{version}"
    return {
        "settlement_id": setid or f"settle-{sid}",
        "snapshot_id": sid, "match_id": mid, "sport": sport,
        "model_name": name, "model_version": version,
        "prediction_generated_at": "2030-01-01T10:00:00+08:00",
        "kickoff_at": "2030-01-01T20:00:00+08:00",
        "final_score": {"home": 2, "away": 1},
        "actual_outcome": outcome, "result_fingerprint": "rf",
        "settled_at": "2030-01-01T22:00:00+08:00",
    }


def make_eval_row(mid="m1", sport="football", version=FOOT_V, name=FOOT_N,
                  sid=None, setid=None, eid=None, outcome="home_win", probs=None):
    sid = sid or f"snap-{mid}-{version}"
    setid = setid or f"settle-{sid}"
    return {
        "evaluation_id": eid or f"eval-{sid}",
        "snapshot_id": sid, "settlement_id": setid, "match_id": mid, "sport": sport,
        "model_name": name, "model_version": version,
        "prediction_generated_at": "2030-01-01T10:00:00+08:00",
        "kickoff_at": "2030-01-01T20:00:00+08:00",
        "model_probabilities": probs or {"home_win": 50, "draw": 25, "away_win": 25},
        "display_probabilities": {"home_win": 48, "draw": 26, "away_win": 26},
        "market_odds": {}, "market_implied_probabilities": {}, "expected_values": {},
        "expected_score_data": {}, "final_score": {"home": 2, "away": 1},
        "actual_outcome": outcome, "result_fingerprint": "rf",
    }


def make_match(mid="m1", status="upcoming", score=None, sport="football"):
    return {
        "id": mid, "sport": sport, "league": "L", "date": "2030-01-01", "time": "20:00",
        "status": status, "score": score, "home": "H", "away": "A",
    }


def _model(status, sport):
    return next(m for m in status["models"] if m["sport"] == sport)


def _odds(sid="o1", mid="m1"):
    return {"snapshot_id": sid, "match_id": mid, "odds": {"home_win": 2.0}}


# ---------------------------------------------------------------------------
# 空 / 零数据
# ---------------------------------------------------------------------------

def test_empty_runtime_zero_counts_no_exception():
    s = es.build_evidence_status()
    assert {m["sport"]: m["gate"] for m in s["models"]} == {"basketball": "none", "football": "none"}
    assert s["historical_models"] == []
    assert s["collection_ready"] is False
    assert s["pipeline_integrity"] is True
    assert s["evidence_review_ready"] is False
    assert s["diagnostics"] is None
    assert all(v == 0 for v in s["integrity"].values())


def test_get_evidence_status_on_empty_stores(isolated_data_dir):
    s = es.get_evidence_status()
    assert s["collection_ready"] is False
    assert s["pipeline_integrity"] is True
    assert s["diagnostics"] is None
    assert s["collection_evidence"] == {"prediction_snapshots": 0, "odds_snapshots": 0}


# ---------------------------------------------------------------------------
# 采集就绪 / 快照
# ---------------------------------------------------------------------------

def test_snapshot_only_reaches_gate_R():
    s = es.build_evidence_status(snapshots=[make_snapshot()], odds_snapshots=[_odds()])
    foot = _model(s, "football")
    assert foot["prediction_snapshots"] == 1 and foot["gate"] == "R"
    assert s["collection_ready"] is True


def test_collection_ready_requires_odds_snapshot():
    s = es.build_evidence_status(snapshots=[make_snapshot()])
    assert s["collection_ready"] is False


def test_basketball_current_version_isolated():
    bb = make_snapshot(mid="b1", sport="basketball", version=BB_V, name=BB_N,
                       probs={"home_win": 60, "away_win": 40})
    s = es.build_evidence_status(snapshots=[bb], odds_snapshots=[_odds("o1", "b1")])
    assert _model(s, "basketball")["prediction_snapshots"] == 1
    assert _model(s, "basketball")["gate"] == "R"
    assert _model(s, "football")["prediction_snapshots"] == 0


def test_historical_version_isolation():
    s = es.build_evidence_status(
        snapshots=[make_snapshot(version="football-ad-1", sid="h1")]
    )
    assert _model(s, "football")["prediction_snapshots"] == 0
    assert s["historical_models"] == [{
        "sport": "football", "model_name": FOOT_N, "model_version": "football-ad-1",
        "prediction_snapshots": 1, "settlements": 0, "evaluation_rows": 0,
    }]


# ---------------------------------------------------------------------------
# 门禁边界
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n,gate", [
    (0, "R"), (29, "R"), (30, "A"), (99, "A"), (100, "B"), (299, "B"), (300, "C"), (301, "C"),
])
def test_gate_boundaries(n, gate):
    rows = [make_eval_row(mid=f"m{i}", sid=f"s{i}", setid=f"t{i}", eid=f"e{i}") for i in range(n)]
    s = es.build_evidence_status(
        snapshots=[make_snapshot()], odds_snapshots=[_odds()], evaluation_rows=rows
    )
    foot = _model(s, "football")
    assert foot["evaluation_rows"] == n
    assert foot["gate"] == gate


def test_evidence_review_ready_only_at_B_or_C():
    rows_a = [make_eval_row(mid=f"m{i}", sid=f"s{i}", setid=f"t{i}", eid=f"e{i}") for i in range(30)]
    assert es.build_evidence_status(evaluation_rows=rows_a)["evidence_review_ready"] is False
    rows_b = [make_eval_row(mid=f"m{i}", sid=f"s{i}", setid=f"t{i}", eid=f"e{i}") for i in range(100)]
    assert es.build_evidence_status(evaluation_rows=rows_b)["evidence_review_ready"] is True


# ---------------------------------------------------------------------------
# 完整性：重复身份
# ---------------------------------------------------------------------------

def test_duplicate_snapshot_detected():
    s = es.build_evidence_status(snapshots=[make_snapshot(sid="dup"), make_snapshot(sid="dup")])
    assert s["integrity"]["duplicate_snapshots"] == 1
    assert s["pipeline_integrity"] is False


def test_duplicate_settlement_detected():
    s = es.build_evidence_status(settlements=[make_settlement(setid="dup"), make_settlement(setid="dup")])
    assert s["integrity"]["duplicate_settlements"] == 1
    assert s["pipeline_integrity"] is False


def test_duplicate_evaluation_row_detected():
    s = es.build_evidence_status(evaluation_rows=[make_eval_row(eid="dup"), make_eval_row(eid="dup")])
    assert s["integrity"]["duplicate_evaluation_rows"] == 1
    assert s["pipeline_integrity"] is False


# ---------------------------------------------------------------------------
# 完整性：卡住的快照 / 结算 / 评估
# ---------------------------------------------------------------------------

def test_finished_match_without_snapshot_is_expected_not_failure():
    m = make_match(mid="m9", status="finished", score={"ft": [2, 1]})
    s = es.build_evidence_status(matches=[m])
    assert s["integrity"]["finished_without_evaluation"] == 1
    assert s["integrity"]["finished_without_evaluation_never_snapshot"] == 1
    assert s["integrity"]["finished_without_evaluation_had_eligible_snapshot"] == 0
    assert s["pipeline_integrity"] is True


def test_eligible_finished_snapshot_without_settlement_fails_integrity():
    m = make_match(mid="m1", status="finished", score={"ft": [2, 1]})
    s = es.build_evidence_status(matches=[m], snapshots=[make_snapshot(mid="m1")])
    assert s["integrity"]["unsettled_eligible_snapshots"] == 1
    assert s["integrity"]["finished_without_evaluation_had_eligible_snapshot"] == 1
    assert s["pipeline_integrity"] is False


def test_upcoming_snapshot_not_counted_as_failed_settlement():
    m = make_match(mid="m1", status="upcoming")
    s = es.build_evidence_status(matches=[m], snapshots=[make_snapshot(mid="m1")])
    assert s["integrity"]["unsettled_eligible_snapshots"] == 0
    assert s["integrity"]["finished_without_evaluation"] == 0
    assert s["pipeline_integrity"] is True


def test_settlement_without_evaluation_fails_integrity():
    s = es.build_evidence_status(settlements=[make_settlement()])
    assert s["integrity"]["settlements_without_evaluation"] == 1
    assert s["pipeline_integrity"] is False


# ---------------------------------------------------------------------------
# 完整链路 / 快照+结算
# ---------------------------------------------------------------------------

def test_full_evaluation_path_is_healthy():
    m = make_match(mid="m1", status="finished", score={"ft": [2, 1]})
    s = es.build_evidence_status(
        matches=[m],
        snapshots=[make_snapshot(mid="m1")],
        odds_snapshots=[_odds()],
        settlements=[make_settlement(mid="m1")],
        evaluation_rows=[make_eval_row(mid="m1")],
    )
    assert s["integrity"]["unsettled_eligible_snapshots"] == 0
    assert s["integrity"]["finished_without_evaluation"] == 0
    assert s["integrity"]["settlements_without_evaluation"] == 0
    assert s["pipeline_integrity"] is True
    assert s["collection_ready"] is True
    assert _model(s, "football")["evaluation_rows"] == 1


def test_snapshot_plus_settlement_without_evaluation():
    m = make_match(mid="m1", status="finished", score={"ft": [2, 1]})
    s = es.build_evidence_status(
        matches=[m], snapshots=[make_snapshot(mid="m1")], settlements=[make_settlement(mid="m1")],
    )
    assert s["integrity"]["settlements_without_evaluation"] == 1
    assert s["pipeline_integrity"] is False


# ---------------------------------------------------------------------------
# 确定性 / 不可变
# ---------------------------------------------------------------------------

def test_deterministic_model_ordering():
    s = es.build_evidence_status()
    assert [m["sport"] for m in s["models"]] == ["basketball", "football"]


def test_input_immutability():
    snaps = [make_snapshot()]
    rows = [make_eval_row()]
    odds = [_odds()]
    before = copy.deepcopy((snaps, rows, odds))
    es.build_evidence_status(snapshots=snaps, evaluation_rows=rows, odds_snapshots=odds)
    assert (snaps, rows, odds) == before


# ---------------------------------------------------------------------------
# 既有评估器复用（不重算指标）
# ---------------------------------------------------------------------------

def test_reuses_existing_evaluators():
    from utils.calibration_evaluation import build_calibration_summaries
    from utils.classification_evaluation import build_classification_summaries
    from utils.temporal_evaluation import build_temporal_summaries

    rows = [make_eval_row(mid="m1", sid="s1", setid="t1", eid="e1")]
    s = es.build_evidence_status(evaluation_rows=rows)
    assert s["diagnostics"] is not None
    assert s["diagnostics"]["classification"] == build_classification_summaries(rows)
    assert s["diagnostics"]["calibration"] == build_calibration_summaries(rows)
    assert s["diagnostics"]["temporal"] == build_temporal_summaries(rows)
    assert s["diagnostics"]["classification"][0]["sample_count"] == 1


def test_diagnostics_none_when_no_rows():
    s = es.build_evidence_status(snapshots=[make_snapshot()])
    assert s["diagnostics"] is None


def test_diagnostics_disabled_flag():
    s = es.build_evidence_status(evaluation_rows=[make_eval_row()], with_diagnostics=False)
    assert s["diagnostics"] is None
