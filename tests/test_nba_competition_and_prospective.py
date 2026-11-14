"""NBA 竞赛类型资格、真实前瞻门禁、对比语义与校正回测（离线、确定性、合成数据）。

覆盖更正工作流 B/C/D/E/F/G/H：
- 竞赛类型分类（常规赛 / NBA Cup 常规赛 / Championship / Play-In / Playoffs / 拒绝）。
- 1230 场常规赛不变式（30 队 × 82 场）的合成证明。
- 真实前瞻赛事门禁（权威开赛时间晚于 as_of；过期 / 未知 / 身份未决 / 反向）。
- 对比语义三分类与必备字段。
- 走查回测同队列、无泄漏、基线与消融。
- 日期界定分页完整性与失败即关闭。

全部使用合成夹具与隔离临时存储；不依赖 API Key、真实网络或操作者磁盘。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import utils.nba_history as nh
import utils.nba_schedule as ns
from utils.nba_history import (
    assemble_historical_dataset,
    classify_nba_competition,
    fetch_historical_games,
    parse_historical_games,
)
from utils.nba_team_strength import compute_team_strength
from utils.nba_model_evaluation import walk_forward_backtest
from models.basketball_model_v2 import predict_from_inventory
from tools.nba_model_comparison import (
    build_comparison,
    classify_fixture_eligibility,
    CLASS_PROSPECTIVE,
    CLASS_RETROSPECTIVE,
    CLASS_BLOCKED,
    FIXTURE_VERIFIED_FUTURE,
    FIXTURE_STALE,
    FIXTURE_KICKOFF_UNKNOWN,
    FIXTURE_IDENTITY_UNRESOLVED,
    FIXTURE_POSTGAME,
)

BJ = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 14, 12, 0, tzinfo=BJ)
REG_END = "2026-04-12"


@pytest.fixture(autouse=True)
def deterministic_pacing(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ns.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ns.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + max(0.0, s)))
    monkeypatch.setattr(ns, "_LAST_BDL_REQUEST_MONO", None)
    monkeypatch.setattr(ns, "_BDL_RETRY_NOT_BEFORE", 0.0)
    monkeypatch.delenv("DSL_BDL_API_KEY", raising=False)
    yield


def _raw(gid, home_id=2, away_id=20, hs=110, aws=100, utc="2025-11-01T23:00:00.000Z",
         season=2025, period=4, postseason=False, ist_stage=None, season_type=None, date=None):
    g = {
        "id": gid, "date": date or utc[:10], "season": season, "status": "Final",
        "status_state": "final", "period": period, "time": "Final", "postseason": postseason,
        "postponed": False, "home_team_score": hs, "visitor_team_score": aws, "datetime": utc,
        "home_team": {"id": home_id, "full_name": "Boston Celtics", "abbreviation": "BOS"},
        "visitor_team": {"id": away_id, "full_name": "New York Knicks", "abbreviation": "NYK"},
    }
    if ist_stage is not None:
        g["ist_stage"] = ist_stage
    if season_type is not None:
        g["season_type"] = season_type
    return g


# ---------------------------------------------------------------------------
# Workstream B：竞赛类型分类（7 个判定）
# ---------------------------------------------------------------------------

def test_normal_regular_game_accept():
    r = classify_nba_competition(_raw(1), regular_season_end_date=REG_END)
    assert r["competition_type"] == "NORMAL_REGULAR"
    assert r["counts_as_regular_season"] is True and r["reject"] is False


@pytest.mark.parametrize("stage", ["East Group A", "West Group C", "East Quarterfinal", "West Semifinal"])
def test_nba_cup_regular_game_accept(stage):
    r = classify_nba_competition(_raw(1, ist_stage=stage, utc="2025-12-01T23:00:00.000Z"),
                                 regular_season_end_date=REG_END)
    assert r["competition_type"] == "NBA_CUP_REGULAR"
    assert r["counts_as_regular_season"] is True and r["reject"] is False


def test_nba_cup_championship_exclude():
    r = classify_nba_competition(_raw(1, ist_stage="Championship", utc="2025-12-17T01:30:00.000Z"),
                                 regular_season_end_date=REG_END)
    assert r["competition_type"] == "NBA_CUP_CHAMPIONSHIP"
    assert r["counts_as_regular_season"] is False and r["reject"] is False


def test_play_in_exclude():
    r = classify_nba_competition(_raw(1, utc="2026-04-14T23:30:00.000Z", date="2026-04-14"),
                                 regular_season_end_date=REG_END)
    assert r["competition_type"] == "PLAY_IN"
    assert r["counts_as_regular_season"] is False and r["reject"] is False


def test_playoff_exclude():
    r = classify_nba_competition(_raw(1, postseason=True, utc="2026-04-20T23:00:00.000Z"),
                                 regular_season_end_date=REG_END)
    assert r["competition_type"] == "PLAYOFF"
    assert r["counts_as_regular_season"] is False and r["reject"] is False


def test_unsupported_competition_reject():
    r = classify_nba_competition(_raw(1, season_type="allstar"), regular_season_end_date=REG_END)
    assert r["competition_type"] == "UNSUPPORTED_COMPETITION" and r["reject"] is True


def test_ambiguous_classification_reject():
    r = classify_nba_competition(_raw(1, ist_stage="Mystery Round"), regular_season_end_date=REG_END)
    assert r["competition_type"] == "AMBIGUOUS_CLASSIFICATION" and r["reject"] is True


def test_season_type_regular_contradicts_championship_reject():
    r = classify_nba_competition(_raw(1, ist_stage="Championship", season_type="regular"),
                                 regular_season_end_date=REG_END)
    assert r["competition_type"] == "AMBIGUOUS_CLASSIFICATION" and r["reject"] is True


def test_explicit_season_type_playin_and_playoffs():
    assert classify_nba_competition(_raw(1, season_type="playin"))["competition_type"] == "PLAY_IN"
    assert classify_nba_competition(_raw(1, season_type="playoffs"))["competition_type"] == "PLAYOFF"


def test_parse_attaches_classification_and_rejects_ambiguous():
    games, errors = parse_historical_games({"data": [
        _raw(1),
        _raw(2, ist_stage="Championship", utc="2025-12-17T01:30:00.000Z"),
        _raw(3, ist_stage="Bogus Stage"),
    ]})
    assert len(games) == 2
    assert games[0]["competition_type"] == "NORMAL_REGULAR"
    assert games[0]["regular_season_eligible"] is True
    assert games[1]["competition_type"] == "NBA_CUP_CHAMPIONSHIP"
    assert games[1]["regular_season_eligible"] is False
    assert any("AMBIGUOUS_CLASSIFICATION:3" in e for e in errors)


def test_assemble_separates_eligible_and_preserves_excluded_evidence():
    games, _ = parse_historical_games({"data": [
        _raw(1),
        _raw(2, ist_stage="East Group A", utc="2025-11-15T23:00:00.000Z"),
        _raw(3, ist_stage="Championship", utc="2025-12-17T01:30:00.000Z"),
        _raw(4, utc="2026-04-14T23:30:00.000Z", date="2026-04-14"),
        _raw(5, postseason=True, utc="2026-04-20T23:00:00.000Z"),
    ]})
    ds = assemble_historical_dataset(games)
    assert ds["game_count"] == 2 and ds["regular_season_count"] == 2
    assert ds["excluded_competition_count"] == 3 and ds["total_completed_games"] == 5
    assert {g["provider_game_id"] for g in ds["excluded_competitions"]} == {"3", "4", "5"}
    assert ds["competition_breakdown"]["NBA_CUP_REGULAR"] == 1
    # 全量视图保留所有比赛作为证据。
    full = assemble_historical_dataset(games, regular_season_only=False)
    assert full["game_count"] == 5


# ---------------------------------------------------------------------------
# Workstream H：1230 场常规赛不变式（合成完美匹配赛程）
# ---------------------------------------------------------------------------

def test_regular_season_1230_sanity_check():
    teams = list(range(1, 31))
    raws = []
    gid = 0
    base = datetime(2025, 10, 21, tzinfo=timezone.utc)
    for r in range(82):                      # 82 轮，每轮 30 队完美匹配（15 场）
        for k in range(15):
            gid += 1
            h = teams[(2 * k + r) % 30]
            a = teams[(2 * k + 1 + r) % 30]
            utc = (base + timedelta(hours=gid)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            raws.append(_raw(gid, home_id=h, away_id=a, utc=utc))
    # 7 场非额外常规赛：1 Championship + 6 Play-In。
    raws.append(_raw(9001, ist_stage="Championship", utc="2025-12-17T01:30:00.000Z"))
    for i, d in enumerate(["2026-04-14", "2026-04-14", "2026-04-15", "2026-04-15", "2026-04-17", "2026-04-17"]):
        raws.append(_raw(9010 + i, utc=f"{d}T23:30:00.000Z", date=d))
    games, errors = parse_historical_games({"data": raws})
    assert not errors
    ds = assemble_historical_dataset(games)
    assert ds["game_count"] == 1230
    assert ds["excluded_competition_count"] == 7
    per_team = {}
    for g in ds["games"]:
        for c in (g["home_code"], g["away_code"]):
            per_team[c] = per_team.get(c, 0) + 1
    assert len(per_team) == 30
    assert set(per_team.values()) == {82}


# ---------------------------------------------------------------------------
# Workstream B/F：实力引擎与回测的资格防御守卫
# ---------------------------------------------------------------------------

def _g(gid, hc, ac, hs, aws, utc, season=2026, eligible=None):
    kickoff = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    g = {
        "provider_game_id": str(gid), "home_code": hc, "away_code": ac,
        "home_final_score": hs, "away_final_score": aws, "scheduled_start_utc": utc,
        "season": season, "overtime_periods": 0, "game_date": utc[:10],
        "beijing_date": kickoff.astimezone(BJ).strftime("%Y-%m-%d"),
    }
    if eligible is not None:
        g["regular_season_eligible"] = eligible
    return g


def test_strength_engine_skips_ineligible_competition():
    games = [
        _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z", eligible=True),
        _g(2, "BOS", "NYK", 120, 100, "2026-10-22T23:00:00.000Z", eligible=False),
    ]
    inv = compute_team_strength(games, as_of=AS_OF)
    assert inv["games_used"] == 1
    assert inv["teams"]["BOS"]["games_used_before_cutoff"] == 1


def test_walk_forward_excludes_ineligible_and_counts():
    games = [
        _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
        _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z"),
        _g(3, "NYK", "BOS", 98, 102, "2026-10-25T23:00:00.000Z", eligible=False),
    ]
    bt = walk_forward_backtest(games)
    assert bt["excluded_competition_count"] == 1
    assert bt["total_input_games"] == 2


# ---------------------------------------------------------------------------
# Workstream D：真实前瞻赛事门禁
# ---------------------------------------------------------------------------

def _fixture(mid="500j-2026-11-15-9999", date="2026-11-15", time="08:00", known=True,
             status="upcoming", hc="BOS", ac="NYK", home="波士顿凯尔特人", away="纽约尼克斯"):
    return {"id": mid, "date": date, "time": time, "kickoff_time_known": known,
            "status": status, "league": "美国职业篮球联盟", "home": home, "away": away,
            "odds": {"home_win": 1.5, "away_win": 2.5}, "_home_code": hc, "_away_code": ac}


# 2026-11-15 08:00 北京 = 2026-11-15T00:00Z；BDL 主队 BOS = bdl:2，客队 NYK = bdl:20。
_KICKOFF_UTC = "2026-11-15T00:00:00.000Z"


def _verified_fixture(**kw):
    f = _fixture(**kw)
    f.update({"market_tracked": True, "jczq_no": "7301", "kickoff_provenance": {
        "actual_kickoff_time_source": "balldontlie_nba_games",
        "kickoff_source_event_id": "21718200",
        "kickoff_source_observed_at": "2026-11-14T11:00:00+08:00",
        "kickoff_source_scheduled_start_utc": _KICKOFF_UTC,
        "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED"}})
    return f


def _observation(**kw):
    o = {"provider": "balldontlie_nba_games", "provider_game_id": "21718200",
         "status_state": "scheduled", "postponed": False, "game_status": "scheduled",
         "home_team_identifier": "bdl:2", "away_team_identifier": "bdl:20",
         "scheduled_start_utc": _KICKOFF_UTC, "observed_at": "2026-11-14T11:55:00+08:00"}
    o.update(kw)
    return o


def test_verified_future_fixture_accepted():
    gate = classify_fixture_eligibility(_verified_fixture(), AS_OF, provider_observation=_observation(),
                                        provider_binding_count=1)
    assert gate["fixture_eligibility"] == FIXTURE_VERIFIED_FUTURE
    assert gate["comparison_classification"] == CLASS_PROSPECTIVE
    assert gate["derived_temporal_status"] == "FUTURE"
    assert gate["time_to_kickoff_seconds"] > 0
    assert gate["disqualification_reason"] is None


def test_future_fixture_without_provider_evidence_blocked():
    gate = classify_fixture_eligibility(_fixture(), AS_OF)
    assert gate["fixture_eligibility"] != FIXTURE_VERIFIED_FUTURE
    assert gate["comparison_classification"] == CLASS_BLOCKED


def test_past_kickoff_not_prospective():
    f = _fixture(mid="500lq-2026-11-14-周五306", date="2026-11-14", time="08:00")
    gate = classify_fixture_eligibility(f, AS_OF)
    assert gate["fixture_eligibility"] == FIXTURE_STALE
    assert gate["comparison_classification"] == CLASS_RETROSPECTIVE
    assert gate["derived_temporal_status"] == "PAST"
    assert gate["time_to_kickoff_seconds"] < 0
    assert gate["disqualification_reason"] == "VERIFIED_KICKOFF_AT_OR_BEFORE_AS_OF"


def test_unknown_kickoff_not_prospective():
    gate = classify_fixture_eligibility(_fixture(time="00:00", known=False), AS_OF)
    assert gate["fixture_eligibility"] == FIXTURE_KICKOFF_UNKNOWN
    assert gate["comparison_classification"] == CLASS_BLOCKED
    assert gate["verified_kickoff"] is None
    assert gate["disqualification_reason"] == "KICKOFF_TIME_UNKNOWN"


def test_reversed_orientation_not_automerged():
    f = _fixture(mid="500lq-2026-11-15-周六306", date="2026-11-15", time="08:00")
    gate = classify_fixture_eligibility(f, AS_OF)
    assert gate["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert gate["comparison_classification"] == CLASS_BLOCKED
    assert gate["home_away_binding_status"] == "DISPLAY_ORDER_UNVERIFIED"
    assert gate["disqualification_reason"] == "REVERSED_ORIENTATION_UNRESOLVED"
    # 绝不自动交换主客。
    assert f["_home_code"] == "BOS" and f["_away_code"] == "NYK"


def test_team_identity_unresolved_blocked():
    gate = classify_fixture_eligibility(_fixture(hc=None), AS_OF)
    assert gate["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert gate["comparison_classification"] == CLASS_BLOCKED
    assert gate["disqualification_reason"] == "TEAM_IDENTITY_UNRESOLVED"


def test_postgame_retrospective_classification():
    gate = classify_fixture_eligibility(_fixture(status="finished"), AS_OF)
    assert gate["fixture_eligibility"] == FIXTURE_POSTGAME
    assert gate["comparison_classification"] == CLASS_RETROSPECTIVE


# ---------------------------------------------------------------------------
# Workstream E：对比语义与必备字段
# ---------------------------------------------------------------------------

_MANDATORY = [
    "canonical_match_id", "provider_game_id", "home_team", "away_team",
    "stored_status", "derived_temporal_status", "prediction_as_of", "verified_kickoff",
    "time_to_kickoff_seconds", "home_away_binding_status", "fixture_eligibility",
    "v1_probability", "v2_shadow_probability", "market_probability",
    "comparison_classification", "disqualification_reason",
]


def test_comparison_mandatory_fields_and_prospective():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
             _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z")]
    comp = build_comparison({"games": games, "game_count": 2}, as_of=AS_OF, fixtures=[_verified_fixture()],
                            observations={"500j-2026-11-15-9999": _observation()})
    row = comp["comparison"][0]
    for field in _MANDATORY:
        assert field in row, field
    assert row["comparison_classification"] == CLASS_PROSPECTIVE
    assert comp["genuine_future_fixture_count"] == 1
    assert comp["prospective_comparison_count"] == 1
    assert comp["writes_protected_stores"] is False and comp["network_requests"] == 0


def test_comparison_mixed_cohort_summary():
    games = [_g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
             _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z")]
    fixtures = [
        _verified_fixture(),                                          # future
        _fixture(mid="500lq-2026-11-14-周五306", date="2026-11-14"),    # stale
        _fixture(mid="500j-2026-11-15-1", time="00:00", known=False),  # unknown
        _fixture(mid="500j-2026-11-15-2", hc=None),                   # identity
    ]
    comp = build_comparison({"games": games, "game_count": 2}, as_of=AS_OF, fixtures=fixtures,
                            observations={"500j-2026-11-15-9999": _observation()})
    assert comp["genuine_future_fixture_count"] == 1
    assert comp["stale_stored_upcoming_count"] == 1
    assert comp["kickoff_unknown_count"] == 1
    assert comp["identity_unresolved_count"] == 1
    assert comp["retrospective_diagnostic_count"] == 1
    assert comp["prospective_blocked_count"] == 2


# ---------------------------------------------------------------------------
# Workstream F/G：同队列、无泄漏、基线、消融
# ---------------------------------------------------------------------------

def _cohort():
    return [
        _g(1, "BOS", "NYK", 110, 100, "2026-10-20T23:00:00.000Z"),
        _g(2, "BOS", "NYK", 105, 100, "2026-10-22T23:00:00.000Z"),
        _g(3, "NYK", "BOS", 98, 102, "2026-10-25T23:00:00.000Z"),
        _g(4, "BOS", "NYK", 120, 110, "2026-10-28T23:00:00.000Z"),
    ]


def test_v1_v2_identical_evaluation_cohort():
    bt = walk_forward_backtest(_cohort())
    assert bt["v1_v2_identical_cohort"] is True
    assert bt["v2_shadow"]["sample_count"] == bt["v1_coldstart"]["sample_count"]
    assert bt["eligible_game_count"] == bt["evaluated_games"]


def test_no_target_result_leakage():
    bt = walk_forward_backtest(_cohort())
    assert bt["no_target_result_in_features"] is True
    assert bt["same_date_leakage"] is False and bt["future_result_leakage"] is False


def test_baselines_present_and_fixed_50_brier():
    bt = walk_forward_backtest(_cohort())
    base = bt["baselines"]["fixed_50_percent"]
    assert base["sample_count"] == bt["v2_shadow"]["sample_count"]
    assert base["binary_brier_score"] == 0.25
    hp = bt["baselines"]["expanding_home_win_prior"]
    assert hp["sample_count"] == bt["v2_shadow"]["sample_count"]


def test_ablation_elo_only_vs_form_vs_complete():
    games = _cohort()
    elo_only = {"form_weight": 0.0, "rest_bonus_points": 0.0, "b2b_penalty_points": 0.0}
    elo_form = {"rest_bonus_points": 0.0, "b2b_penalty_points": 0.0}
    bt_full = walk_forward_backtest(games)
    bt_elo = walk_forward_backtest(games, v2_params=elo_only)
    bt_form = walk_forward_backtest(games, v2_params=elo_form)
    # 三种配置在同一队列上评估，且概率彼此不同（消融确实改变了模型）。
    assert bt_full["evaluated_games"] == bt_elo["evaluated_games"] == bt_form["evaluated_games"]
    full_p = bt_full["v2_shadow"]["binary_brier_score"]
    elo_p = bt_elo["v2_shadow"]["binary_brier_score"]
    form_p = bt_form["v2_shadow"]["binary_brier_score"]
    assert len({full_p, elo_p, form_p}) >= 2


def test_ablation_params_change_single_prediction():
    inv = compute_team_strength(_cohort(), as_of=AS_OF)
    full = predict_from_inventory(inv, "BOS", "NYK")
    elo_only = predict_from_inventory(inv, "BOS", "NYK",
                                      params={"form_weight": 0.0, "rest_bonus_points": 0.0,
                                              "b2b_penalty_points": 0.0})
    assert full["raw_model_probabilities"]["home_win"] != elo_only["raw_model_probabilities"]["home_win"]
    assert elo_only["margin_components"]["form"] == 0.0
    assert elo_only["margin_components"]["rest"] == 0.0


# ---------------------------------------------------------------------------
# Workstream C：日期界定分页完整性 / 失败即关闭
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


def _page(games, next_cursor=None):
    return {"data": games, "meta": {"next_cursor": next_cursor, "per_page": 100}}


def _install(monkeypatch, responses):
    calls = []
    seq = list(responses)

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return seq.pop(0)

    monkeypatch.setattr(nh.requests, "get", fake_get)
    return calls


def test_date_scoped_pagination_completeness(monkeypatch):
    calls = _install(monkeypatch, [
        _Resp(_page([_raw(1, season=2026, utc="2026-10-20T23:00:00.000Z")], next_cursor=7)),
        _Resp(_page([_raw(2, season=2026, utc="2026-10-22T23:00:00.000Z")], next_cursor=None)),
    ])
    games, diag = fetch_historical_games(
        seasons=[2026], start_date="2026-10-01", end_date="2026-10-31",
        season_type="regular", allow_network=True, api_key="SECRET_TEST_KEY",
        max_elapsed_seconds=10000)
    assert len(games) == 2 and diag["pagination_complete"] is True
    assert diag["start_date"] == "2026-10-01" and diag["end_date"] == "2026-10-31"
    assert diag["season_type"] == "regular"
    assert "start_date=2026-10-01" in calls[0] and "season_type=regular" in calls[0]
    assert "SECRET_TEST_KEY" not in json.dumps(diag, ensure_ascii=False)


def test_incomplete_window_fail_closed(monkeypatch):
    _install(monkeypatch, [
        _Resp(_page([_raw(1, season=2026, utc="2026-10-20T23:00:00.000Z")], next_cursor=7)),
        _Resp({}, status=500),
    ])
    games, diag = fetch_historical_games(
        seasons=[2026], start_date="2026-10-01", end_date="2026-10-31",
        allow_network=True, api_key="k", max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_complete"] is False
    assert diag["coverage_status"] == "PAGINATION_INCOMPLETE"


def test_uncertified_cursor_order_not_proof(monkeypatch):
    # 即便已捕获页含超出截止日的日期，未耗尽游标仍不得视为完整（失败即关闭，零比赛）。
    _install(monkeypatch, [
        _Resp(_page([_raw(1, season=2026, utc="2027-03-01T23:00:00.000Z")], next_cursor=7)),
    ])
    games, diag = fetch_historical_games(
        seasons=[2026], allow_network=True, api_key="k", max_requests=1, max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_complete"] is False
    assert diag["next_cursor_remaining"] == 7
    assert diag["pagination_failure_reason"] == "REQUEST_BUDGET_EXHAUSTED"


# ---------------------------------------------------------------------------
# Workstream H：生产模型不变 + 受保护存储零写入
# ---------------------------------------------------------------------------

def test_production_model_unchanged():
    assert config.MODEL_VERSIONS["basketball"] == "basketball-margin-1"


def test_correction_pipeline_writes_no_protected_stores(isolated_data_dir):
    stores = ["daily_matches.json", "prediction_snapshots.json", "odds_snapshots.json",
              "settlements.json", "evaluation_rows.json", "team_strength.json", "calibrated.json"]
    for name in stores:
        (isolated_data_dir / name).write_text(json.dumps({"version": 1, "seed": name}), encoding="utf-8")

    def digests():
        return {n: hashlib.sha256((isolated_data_dir / n).read_bytes()).hexdigest() for n in stores}

    before = digests()
    games, _ = parse_historical_games({"data": [_raw(1), _raw(2, ist_stage="Championship",
                                                              utc="2025-12-17T01:30:00.000Z")]})
    ds = assemble_historical_dataset(games)
    compute_team_strength(ds["games"], as_of=AS_OF)
    walk_forward_backtest(_cohort())
    build_comparison(ds, as_of=AS_OF, fixtures=[_fixture()])
    classify_fixture_eligibility(_fixture(), AS_OF)
    assert digests() == before