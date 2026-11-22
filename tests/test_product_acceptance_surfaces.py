"""Workstream B/D 的只读产品面回归：评估摘要端点与状态新鲜度块。

全部使用隔离数据目录与合成样本（SYNTHETIC_TEST_VERIFIED）；
验证只读边界（GET 不写任何受保护存储）与口径约定（复用既有模块）。
"""
from __future__ import annotations

import pytest

import app as server
from utils.classification_evaluation import validate_row
from test_basketball_operational_acceptance import write_stores, store_hashes
from test_nba_schedule_completeness import NOW, canonical
from utils.daily_loader import override_beijing_now


def _eval_row(evaluation_id, *, sport="football", outcome="home_win", implied=None):
    probs = (
        {"home_win": 55, "draw": 25, "away_win": 20}
        if sport == "football"
        else {"home_win": 60, "away_win": 40}
    )
    score = {"home": 2, "away": 1} if outcome == "home_win" else {"home": 0, "away": 0}
    row = {
        "evaluation_id": evaluation_id,
        "match_id": f"m-{evaluation_id[:6]}",
        "sport": sport,
        "league": "英超",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": "elo-poisson-dixon-coles" if sport == "football" else "elo-normal-points",
        "model_version": (
            "football-coldstart-1" if sport == "football" else "basketball-margin-1"
        ),
        "model_probabilities": probs,
        "display_probabilities": dict(probs),
        "actual_outcome": outcome,
        "final_score": score,
        "prediction_generated_at": "2030-01-01T00:00:00+08:00",
        "kickoff_at": "2030-01-01T20:00:00+08:00",
        "settled_at": "2030-01-02T00:00:00+08:00",
        "market_odds": {"home_win": 1.8, "draw": 3.5, "away_win": 4.2},
    }
    if implied is not None:
        row["market_implied_probabilities"] = implied
    return row


@pytest.fixture
def isolated_app(isolated_data_dir, monkeypatch):
    write_stores(isolated_data_dir, [canonical()])
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITES_ENABLED", False)
    monkeypatch.setitem(server.app.config, "LOCAL_UI_REFRESH_ENABLED", False)
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITE_TOKEN", "")
    return isolated_data_dir


def test_evaluation_summary_groups_by_sport_and_version(isolated_app):
    rows = [
        _eval_row("e-foot-1", implied={"home_win": 50, "draw": 28, "away_win": 22}),
        _eval_row("e-foot-2", sport="football", outcome="draw",
                  implied={"home_win": 44, "draw": 30, "away_win": 26}),
        _eval_row("e-bb-1", sport="basketball", outcome="home_win"),
    ]
    # 样本必须通过项目统一校验（口径一致性）
    for r in rows:
        validate_row(r)

    import json as _json
    import os

    with open(os.path.join(str(isolated_app), "evaluation_rows.json"), "w", encoding="utf-8") as fh:
        _json.dump({"rows": {r["evaluation_id"]: r for r in rows}, "version": 1},
                   fh, ensure_ascii=False, sort_keys=True)

    before = store_hashes(isolated_app)
    client = server.app.test_client()
    resp = client.get("/api/evaluation-summary")
    assert resp.status_code == 200
    assert store_hashes(isolated_app) == before  # 只读：不写任何存储

    data = resp.get_json()
    assert data["probability_source"] == "model_probabilities"
    models = {(m["sport"], m["model_version"]): m for m in data["models"]}
    assert set(models) == {("football", "football-coldstart-1"), ("basketball", "basketball-margin-1")}

    foot = models[("football", "football-coldstart-1")]
    assert foot["sample_count"] == 2
    assert foot["accuracy"] == 0.5
    assert foot["market_baseline"]["status"] == "VERIFIED"
    assert foot["market_baseline"]["same_fixture_samples"] == 2
    assert 0 <= foot["market_baseline"]["model_direction_accuracy"] <= 1
    assert 0 <= foot["market_baseline"]["market_direction_accuracy"] <= 1

    bb = models[("basketball", "basketball-margin-1")]
    assert bb["sample_count"] == 1
    # 足球三分类与篮球二分类口径不同：各自独立成组，绝不混合
    assert foot["brier_score"] != bb["brier_score"]


def test_evaluation_summary_without_market_prices_marks_unavailable(isolated_app):
    import json as _json
    import os

    rows = [_eval_row("e-nomkt-1", implied=None)]
    with open(os.path.join(str(isolated_app), "evaluation_rows.json"), "w", encoding="utf-8") as fh:
        _json.dump({"rows": {r["evaluation_id"]: r for r in rows}, "version": 1},
                   fh, ensure_ascii=False, sort_keys=True)

    resp = server.app.test_client().get("/api/evaluation-summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["models"][0]["market_baseline"]["status"] == "MARKET_BASELINE_NOT_AVAILABLE"
    assert data["models"][0]["market_baseline"]["same_fixture_samples"] == 0


def test_status_freshness_block_distinguishes_verified_and_stale(isolated_app):
    from test_nba_schedule_completeness import NOW

    base = NOW
    known_future = canonical()
    known_future.update(id="500j-2030-05-16-4306", date="2030-05-16", time="10:00",
                        kickoff_time_known=True, market_tracked=True,
                        odds={"home_win": 1.8, "away_win": 2.0})
    unknown_stale = canonical()
    unknown_stale.update(id="500j-2030-05-15-4307", jczq_no="4307", round="4307",
                         kickoff_time_known=False, time="00:00",
                         odds={"home_win": 1.7, "away_win": 2.1})
    finished = canonical()
    finished.update(id="500j-2030-05-15-4308", jczq_no="4308", round="4308",
                    status="finished", kickoff_time_known=True, time="08:00",
                    score={"ft": [100, 95]}, odds={"home_win": 1.6, "away_win": 2.2})
    write_stores(isolated_app, [known_future, unknown_stale, finished])

    before = store_hashes(isolated_app)
    with override_beijing_now(base):
        resp = server.app.test_client().get("/api/status")
    assert resp.status_code == 200
    assert store_hashes(isolated_app) == before  # 只读

    fresh = resp.get_json()["freshness"]
    bb = fresh["per_sport"]["basketball"]
    # 已知开赛时间的未来赛事计入 verified_future；未知开赛时间不算未来赛事
    assert bb["verified_future_count"] == 1
    assert bb["stale_upcoming_count"] == 1
    assert bb["latest_market_event_date"] in ("2030-05-15", "2030-05-16")
    assert bb["stored_upcoming_count"] == 2
    # 完赛记录不属于 upcoming 队列
    foot = fresh["per_sport"]["football"]
    assert foot["verified_future_count"] == 0


def test_evaluation_summary_endpoint_is_get_only(isolated_app):
    client = server.app.test_client()
    resp = client.post("/api/evaluation-summary")
    assert resp.status_code == 405
