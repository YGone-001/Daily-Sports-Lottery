"""Workstream B/D 的只读产品面回归：评估摘要端点与状态新鲜度块。

全部使用隔离数据目录与合成样本（SYNTHETIC_TEST_VERIFIED）；
验证只读边界（GET 不写任何受保护存储）与口径约定（复用既有模块）。
"""
from __future__ import annotations

import json

import pytest

import app as server
from utils.classification_evaluation import validate_row
from test_basketball_operational_acceptance import write_stores, store_hashes
from test_nba_schedule_completeness import NOW, canonical
from utils.daily_loader import override_beijing_now


def _eval_row(evaluation_id, *, sport="football", outcome="home_win", implied=None,
              model_version=None, match_id=None):
    probs = (
        {"home_win": 55, "draw": 25, "away_win": 20}
        if sport == "football"
        else {"home_win": 60, "away_win": 40}
    )
    score = {"home": 2, "away": 1} if outcome == "home_win" else {"home": 0, "away": 0}
    row = {
        "evaluation_id": evaluation_id,
        "match_id": match_id or f"m-{evaluation_id[:6]}",
        "sport": sport,
        "league": "英超",
        "home_team": "主队",
        "away_team": "客队",
        "model_name": "elo-poisson-dixon-coles" if sport == "football" else "elo-normal-points",
        "model_version": model_version or (
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

    # Workstream D：互斥四分区 + 提供商核实二分
    assert bb["known_future_kickoff"] == 1
    assert bb["past_kickoff_stored_upcoming"] == 0
    assert bb["future_kickoff_unknown"] == 1  # 4307 的日期(05-15)在 NOW(05-15)当天 → 未定
    assert bb["unknown_kickoff_past_date"] == 0
    # 未携带提供商溯源 → 核实数不得编造
    assert bb["provider_schedule_verified"] == 0
    assert bb["provider_schedule_unverified"] == 1
    assert fresh["provider_evidence"] == "FROM_STORED_PROVENANCE"


def test_status_freshness_provider_verification_and_beijing_boundary(isolated_app):
    """北京日期边界：未知开赛 + 当日 → FUTURE_KICKOFF_UNKNOWN；昨日 → UNKNOWN_KICKOFF_PAST_DATE。

    提供商核实只认 kickoff_provenance = SCHEDULE_PROVIDER_VERIFIED。
    """
    from test_nba_schedule_completeness import NOW

    today = canonical()
    today.update(id="500j-2030-05-15-4301", jczq_no="4301", round="4301",
                 kickoff_time_known=False, time="00:00")
    yesterday = canonical()
    yesterday.update(id="500j-2030-05-14-4302", date="2030-05-14",
                     jczq_no="4302", round="4302",
                     kickoff_time_known=False, time="00:00")
    proven_future = canonical()
    proven_future.update(id="500j-2030-05-16-4306", date="2030-05-16", time="10:00",
                         kickoff_time_known=True, market_tracked=True,
                         odds={"home_win": 1.8, "away_win": 2.0},
                         kickoff_provenance={
                             "actual_kickoff_time_source": "balldontlie_nba_games",
                             "kickoff_source_event_id": "21718096",
                             "kickoff_source_observed_at": "2030-05-14T00:00:00+08:00",
                             "kickoff_source_scheduled_start_utc": "2030-05-16T02:00:00Z",
                             "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED",
                         })
    write_stores(isolated_app, [today, yesterday, proven_future])

    with override_beijing_now(NOW):
        resp = server.app.test_client().get("/api/status")
    assert resp.status_code == 200
    bb = resp.get_json()["freshness"]["per_sport"]["basketball"]
    assert bb["future_kickoff_unknown"] == 1        # 当日未定 → 未定（不是过去）
    assert bb["unknown_kickoff_past_date"] == 1     # 昨日未定 → 过期
    assert bb["past_kickoff_stored_upcoming"] == 0
    assert bb["known_future_kickoff"] == 1
    assert bb["provider_schedule_verified"] == 1    # 携带 SCHEDULE_PROVIDER_VERIFIED
    assert bb["provider_schedule_unverified"] == 0
    # 兼容字段不变式：verified + stale = 存储待赛总数
    assert bb["verified_future_count"] + bb["stale_upcoming_count"] == bb["stored_upcoming_count"]


def test_evaluation_summary_endpoint_is_get_only(isolated_app):
    client = server.app.test_client()
    resp = client.post("/api/evaluation-summary")
    assert resp.status_code == 405


def _seed_rows(directory, rows):
    import json as _json
    import os

    with open(os.path.join(str(directory), "evaluation_rows.json"), "w", encoding="utf-8") as fh:
        _json.dump({"rows": {r["evaluation_id"]: r for r in rows}, "version": 1},
                   fh, ensure_ascii=False, sort_keys=True)
    return rows


def _summary_models():
    resp = server.app.test_client().get("/api/evaluation-summary")
    assert resp.status_code == 200
    data = resp.get_json()
    return data, {(m["sport"], m["model_version"]): m for m in data["models"]}


IMPLIED_OK = {"home_win": 50, "draw": 28, "away_win": 22}


def test_market_baseline_isolates_multiple_model_versions(isolated_app):
    """MULTIPLE_MODEL_VERSIONS_ISOLATED：每个模型版本只消费自己的市场可比样本。"""
    # v1：2 行；v2：1 行。两版本同为足球但样本集不同。
    rows = [
        _eval_row("mv-1", implied=IMPLIED_OK, model_version="football-coldstart-1"),
        _eval_row("mv-2", implied=IMPLIED_OK, model_version="football-coldstart-1"),
        _eval_row("mv-3", implied=IMPLIED_OK, outcome="draw",
                  model_version="football-shadow-x"),
    ]
    _seed_rows(isolated_app, rows)
    before = store_hashes(isolated_app)

    data, models = _summary_models()
    assert store_hashes(isolated_app) == before  # EVALUATION_ENDPOINT_READ_ONLY

    v1 = models[("football", "football-coldstart-1")]
    v2 = models[("football", "football-shadow-x")]
    assert v1["sample_count"] == 2 and v1["market_baseline"]["same_fixture_samples"] == 2
    assert v2["sample_count"] == 1 and v2["market_baseline"]["same_fixture_samples"] == 1
    assert v1["market_baseline"]["cohort"]["model_version"] == "football-coldstart-1"
    assert v2["market_baseline"]["cohort"]["model_version"] == "football-shadow-x"


def test_market_baseline_excludes_missing_and_invalid_market_probabilities(isolated_app):
    """MISSING_MARKET_ODDS_EXCLUDED + INVALID_MARKET_PROBABILITIES_REJECTED。

    分类指标仍然统计全部 5 行；市场基线只吃有效隐含概率的行。
    """
    rows = [
        _eval_row("mk-1", implied=IMPLIED_OK),
        _eval_row("mk-2", implied=None),                                   # 缺失
        _eval_row("mk-3", implied={"home_win": True, "draw": 28, "away_win": 22}),  # bool
        _eval_row("mk-4", implied={"home_win": 0, "draw": 28, "away_win": 22}),     # 非正
        _eval_row("mk-5", implied={"home_win": "50", "draw": 28, "away_win": 22}),  # 字符串
    ]
    _seed_rows(isolated_app, rows)

    data, models = _summary_models()
    foot = models[("football", "football-coldstart-1")]
    assert foot["sample_count"] == 5          # 分类口径不受影响
    assert foot["market_baseline"]["same_fixture_samples"] == 1  # 只有 mk-1 合格


def test_market_baseline_paired_on_identical_fixture_identity(isolated_app):
    """SAME_FIXTURE_MODEL_MARKET_ALIGNMENT：模型与市场在完全相同的行上配对计分。"""
    # 两行：第一行模型与市场同选主胜且正确；第二行模型选主胜(正确)、市场选客胜(错误)。
    rows = [
        _eval_row("pa-1", implied={"home_win": 60, "draw": 20, "away_win": 20}),
        _eval_row("pa-2", implied={"home_win": 20, "draw": 20, "away_win": 60},
                  outcome="home_win"),
    ]
    _seed_rows(isolated_app, rows)

    data, models = _summary_models()
    mb = models[("football", "football-coldstart-1")]["market_baseline"]
    # 模型：2/2 = 1.0；市场：1/2 = 0.5 —— 同一组行、逐行配对
    assert mb["model_direction_accuracy"] == 1.0
    assert mb["market_direction_accuracy"] == 0.5
    assert mb["same_fixture_samples"] == 2
    assert mb["status"] == "VERIFIED"


def test_market_baseline_football_basketball_cohorts_isolated(isolated_app):
    """FOOTBALL_BASKETBALL_COHORTS_ISOLATED：跨运动不借样本。"""
    rows = [
        _eval_row("cb-1", implied=IMPLIED_OK),
        _eval_row("cb-2", sport="basketball", outcome="home_win",
                  implied={"home_win": 60, "away_win": 40}),
    ]
    _seed_rows(isolated_app, rows)

    data, models = _summary_models()
    foot = models[("football", "football-coldstart-1")]["market_baseline"]
    bb = models[("basketball", "basketball-margin-1")]["market_baseline"]
    assert foot["same_fixture_samples"] == 1
    assert bb["same_fixture_samples"] == 1
    assert foot["cohort"]["sport"] == "football"
    assert bb["cohort"]["sport"] == "basketball"


def test_market_baseline_computation_vs_provenance_status(isolated_app):
    """MARKET_COMPUTATION_VS_PROVENANCE_STATUS：计算完成与方向核实必须分开陈述。"""
    rows = [_eval_row("pv-1", implied=IMPLIED_OK)]
    _seed_rows(isolated_app, rows)

    resp = server.app.test_client().get("/api/evaluation-summary")
    assert resp.status_code == 200
    mb = resp.get_json()["models"][0]["market_baseline"]
    assert mb["status"] == "VERIFIED"  # 指计算完成
    assert mb["computation_status"] == "CALCULATION_COMPLETE"
    # 方向核实是另一维度：当前未获独立证据，必须如实标 UNRESOLVED / PENDING
    assert mb["market_source_identity"] == "MARKET_SOURCE_IDENTITY_UNRESOLVED"
    assert mb["independent_orientation_check"] == "INDEPENDENT_ORIENTATION_CHECK_PENDING"


def test_unverified_market_not_promoted(isolated_app):
    """UNVERIFIED_MARKET_NOT_PROMOTED：响应中不得出现「方向已核实」或模型优越声明。"""
    rows = [
        _eval_row("up-1", implied=IMPLIED_OK),
        _eval_row("up-2", implied=None),  # 无市场证据 → NOT_AVAILABLE 分支
    ]
    _seed_rows(isolated_app, rows)
    # 让两个版本各自成组，覆盖可用与不可用两个分支
    rows[1]["model_version"] = "football-shadow-x"
    rows[1]["evaluation_id"] = "up-2"
    _seed_rows(isolated_app, rows)

    resp = server.app.test_client().get("/api/evaluation-summary")
    assert resp.status_code == 200
    text = json.dumps(resp.get_json(), ensure_ascii=False)
    assert "MARKET_SOURCE_IDENTITY_VERIFIED" not in text
    assert "MARKET_SOURCE_IDENTITY_UNRESOLVED" in text
    assert "INDEPENDENT_ORIENTATION_CHECK_PENDING" in text
    # 不得出现模型优越的市场对比结论字段
    for forbidden in ("model_beats_market", "beats_market", "superior_to_market"):
        assert forbidden not in text
    for m in resp.get_json()["models"]:
        assert m["market_baseline"]["market_source_identity"] != "MARKET_SOURCE_IDENTITY_VERIFIED"


def _causal_row(evaluation_id, *, outcome="home_win", generated_at=None, settled_at=None,
                kickoff_at="2030-01-01T20:00:00+08:00", **kw):
    row = _eval_row(evaluation_id, outcome=outcome, **kw)
    row["prediction_generated_at"] = generated_at
    row["settled_at"] = settled_at
    row["kickoff_at"] = kickoff_at
    return row


def test_expanding_baseline_same_kickoff_fixtures_no_leakage():
    """SAME_KICKOFF_FIXTURES_NO_LEAKAGE：同开赛时刻的多场互不泄漏。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("sk-1", outcome="home_win",
                    generated_at="2030-01-01T12:00:00+08:00",
                    settled_at="2030-01-01T23:00:00+08:00",
                    kickoff_at="2030-01-01T20:00:00+08:00"),
        _causal_row("sk-2", outcome="home_win",
                    generated_at="2030-01-01T12:30:00+08:00",
                    settled_at="2030-01-01T23:00:00+08:00",
                    kickoff_at="2030-01-01T20:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    per = {p["evaluation_id"]: p for p in result["per_row"]}
    # sk-2 的预测（12:30）早于 sk-1 的结果可得时刻（23:00）→ 不得继承 sk-1 结果
    assert per["sk-2"]["prior_observation_count"] == 0
    assert per["sk-2"]["probabilities"] == {"home_win": 1 / 3, "draw": 1 / 3, "away_win": 1 / 3}


def test_expanding_baseline_earlier_kickoff_still_unsettled_excluded():
    """EARLIER_KICKOFF_STILL_UNSETTLED：更早开赛但未结算 → 不进入因果历史。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("ek-1", outcome="away_win",
                    generated_at="2030-01-01T08:00:00+08:00",
                    settled_at=None,  # 未结算：结果可得性无法建立
                    kickoff_at="2030-01-01T10:00:00+08:00"),
        _causal_row("ek-2", outcome="home_win",
                    generated_at="2030-01-05T12:00:00+08:00",
                    settled_at="2030-01-05T23:00:00+08:00",
                    kickoff_at="2030-01-05T20:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    per = {p["evaluation_id"]: p for p in result["per_row"]}
    # ek-1 虽然更早开赛，但没有可信的「结果已可得」边界 → 不计入 ek-2 的先验
    assert per["ek-2"]["prior_observation_count"] == 0
    assert result["causal_accounting"]["contributors_excluded_invalid_or_missing_settled_at"] == 1


def test_expanding_baseline_prior_result_known_before_prediction():
    """PRIOR_RESULT_KNOWN_BEFORE_PREDICTION：结果可得早于预测 → 正常进入因果历史。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("pr-1", outcome="away_win",
                    generated_at="2030-01-01T08:00:00+08:00",
                    settled_at="2030-01-01T23:00:00+08:00"),
        _causal_row("pr-2", outcome="home_win",
                    generated_at="2030-01-02T12:00:00+08:00",
                    settled_at="2030-01-02T23:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    per = {p["evaluation_id"]: p for p in result["per_row"]}
    # pr-2 预测时 pr-1 已结算（23:00 < 次日 12:00）→ 客胜计入先验
    assert per["pr-2"]["prior_observation_count"] == 1
    assert abs(per["pr-2"]["probabilities"]["away_win"] - (1 + 1) / 4) < 1e-9


def test_expanding_baseline_result_settled_after_prediction_excluded():
    """RESULT_SETTLED_AFTER_PREDICTION：结算晚于预测 → 不进入先验（含自身）。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("sa-1", outcome="home_win",
                    generated_at="2030-01-01T12:00:00+08:00",
                    settled_at="2030-01-02T23:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    # 自身结算在预测之后：先验仍为均匀
    assert result["per_row"][0]["prior_observation_count"] == 0
    assert result["causal_accounting"]["contributors_used"] == 0


def test_expanding_baseline_missing_settlement_timestamp():
    """MISSING_SETTLEMENT_TIMESTAMP：缺 settled_at 的结果不进入因果历史。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("ms-1", outcome="home_win",
                    generated_at="2030-01-01T08:00:00+08:00",
                    settled_at=None),
        _causal_row("ms-2", outcome="home_win",
                    generated_at="2030-01-02T12:00:00+08:00",
                    settled_at="2030-01-02T23:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    per = {p["evaluation_id"]: p for p in result["per_row"]}
    assert per["ms-2"]["prior_observation_count"] == 0
    assert result["causal_accounting"]["contributors_excluded_invalid_or_missing_settled_at"] == 1


def test_expanding_baseline_invalid_or_naive_timestamps():
    """INVALID_OR_NAIVE_TIMESTAMP：畸形/naive 时间戳一律失败关闭。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("iv-1", outcome="home_win",
                    generated_at="2030-01-01T08:00:00+08:00",
                    settled_at="2030-01-01T23:00:00"),  # naive → 无效
        _causal_row("iv-2", outcome="home_win",
                    generated_at="2030-01-02T12:00:00+08:00",
                    settled_at="not-a-date"),
        _causal_row("iv-3", outcome="away_win",
                    generated_at="2030-01-03T12:00:00+08:00",
                    settled_at="2030-01-03T23:00:00+08:00"),
    ]
    result = expanding_frequency_baseline(rows)
    per = {p["evaluation_id"]: p for p in result["per_row"]}
    # iv-1/iv-2 的结果不可用 → iv-3 的先验观测为 0
    assert per["iv-3"]["prior_observation_count"] == 0
    assert result["causal_accounting"]["contributors_excluded_invalid_or_missing_settled_at"] == 2


def test_expanding_baseline_first_fixture_uniform_prior():
    """FIRST_FIXTURE_UNIFORM_PRIOR：首行（无先验）为均匀分布。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [_causal_row("ff-1", outcome="home_win",
                        generated_at="2030-01-01T12:00:00+08:00",
                        settled_at="2030-01-02T23:00:00+08:00")]
    result = expanding_frequency_baseline(rows)
    assert result["per_row"][0]["probabilities"] == {
        "home_win": 1 / 3, "draw": 1 / 3, "away_win": 1 / 3,
    }


def test_expanding_baseline_input_order_invariance():
    """INPUT_ORDER_INVARIANCE：结果与输入顺序无关。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("io-1", outcome="home_win",
                    generated_at="2030-01-01T12:00:00+08:00",
                    settled_at="2030-01-01T23:00:00+08:00"),
        _causal_row("io-2", outcome="away_win",
                    generated_at="2030-01-02T12:00:00+08:00",
                    settled_at="2030-01-02T23:00:00+08:00"),
        _causal_row("io-3", outcome="draw",
                    generated_at="2030-01-03T12:00:00+08:00",
                    settled_at="2030-01-03T23:00:00+08:00"),
    ]
    forward = expanding_frequency_baseline(rows)
    reversed_result = expanding_frequency_baseline(list(reversed(rows)))
    assert forward["direction_accuracy"] == reversed_result["direction_accuracy"]
    assert forward["multiclass_brier"] == reversed_result["multiclass_brier"]
    assert forward["log_loss"] == reversed_result["log_loss"]
    assert [p["probabilities"] for p in forward["per_row"]] == [
        p["probabilities"] for p in reversed_result["per_row"]
    ]


def test_expanding_baseline_same_model_cohort_only():
    """SAME_MODEL_COHORT_ONLY：基线消费的是调用方给定的同组行（不做跨组混合判定）。"""
    from utils.evaluation_baselines import expanding_frequency_baseline

    rows = [
        _causal_row("co-1", outcome="home_win",
                    generated_at="2030-01-01T12:00:00+08:00",
                    settled_at="2030-01-01T23:00:00+08:00"),
        _causal_row("co-2", outcome="away_win", model_version="football-shadow-x",
                    generated_at="2030-01-02T12:00:00+08:00",
                    settled_at="2030-01-02T23:00:00+08:00"),
    ]
    # 函数按调用方给定行集计算；调用方（评估摘要/报告脚本）负责同组过滤。
    # 传入两行即计两行——此处锁定该契约，防止未来悄悄引入隐式过滤。
    result = expanding_frequency_baseline(rows)
    assert result["n"] == 2
    assert result["causal_accounting"]["contributors_used"] == 1  # co-1 结算早于 co-2 预测


def test_baseline_class_integrity_uniform_values():
    """FOOTBALL_THREE_CLASS / BASKETBALL_TWO_CLASS：均匀基线解析值必须精确。"""
    from utils.evaluation_baselines import uniform_baseline

    foot = uniform_baseline([_eval_row("u-1")])
    assert foot["multiclass_brier"] == pytest.approx(2 / 3)
    assert foot["log_loss"] == pytest.approx(__import__("math").log(3))

    bb = uniform_baseline([_eval_row("u-2", sport="basketball", outcome="home_win")])
    assert bb["multiclass_brier"] == pytest.approx(0.5)
    assert bb["log_loss"] == pytest.approx(__import__("math").log(2))
