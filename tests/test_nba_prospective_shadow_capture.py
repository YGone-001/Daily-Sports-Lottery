"""NBA 当前赛季窗口认证、提供商支撑的前瞻门禁与不可变影子捕获（离线、确定性、合成数据）。

全部使用合成夹具、注入时钟与隔离临时目录；不依赖 API Key、真实网络或操作者磁盘。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import utils.nba_history as nh
import utils.nba_schedule as ns
from models.basketball_model import predict_basketball
from tools import nba_shadow_capture as cap
from tools.nba_model_comparison import (
    CLASS_BLOCKED,
    CLASS_PROSPECTIVE,
    CLASS_RETROSPECTIVE,
    FIXTURE_IDENTITY_UNRESOLVED,
    FIXTURE_KICKOFF_UNKNOWN,
    FIXTURE_MARKET_UNAVAILABLE,
    FIXTURE_SCHEDULE_UNVERIFIED,
    FIXTURE_STALE,
    FIXTURE_VERIFIED_FUTURE,
    _v1_for_fixture,
    build_comparison,
    classify_fixture_eligibility,
)
from utils.nba_history import (
    PARTITION_ALL_NON_PRESEASON,
    PARTITION_IST_CROSSCHECK,
    WINDOW_UNRESOLVED,
    WINDOW_VERIFIED,
    certify_history_window,
    classify_nba_competition,
    fetch_history_window,
    plan_history_window,
)

BJ = timezone(timedelta(hours=8))
CUTOFF = datetime(2026, 11, 14, 12, 0, tzinfo=BJ)          # = 2026-11-14T04:00Z
STORES = ["daily_matches.json", "prediction_snapshots.json", "odds_snapshots.json",
          "settlements.json", "evaluation_rows.json", "team_strength.json", "calibrated.json"]


@pytest.fixture(autouse=True)
def deterministic_pacing(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ns.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ns.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + max(0.0, s)))
    monkeypatch.setattr(ns, "_LAST_BDL_REQUEST_MONO", None)
    monkeypatch.setattr(ns, "_BDL_RETRY_NOT_BEFORE", 0.0)
    monkeypatch.delenv("DSL_BDL_API_KEY", raising=False)
    yield


def _raw(gid, *, utc, home_id=2, away_id=20, hs=110, aws=100, season=2026, state="final",
         ist_stage=None, postponed=False, season_type=None):
    g = {"id": gid, "date": (datetime.fromisoformat(utc.replace("Z", "+00:00")) - timedelta(hours=5)).strftime("%Y-%m-%d"),
         "season": season, "status": "Final" if state == "final" else "7:00 pm ET",
         "status_state": state, "period": 4 if state == "final" else 0, "time": "Final",
         "postseason": False, "postponed": postponed,
         "home_team_score": hs if state == "final" else 0, "visitor_team_score": aws if state == "final" else 0,
         "datetime": utc, "ist_stage": ist_stage,
         "home_team": {"id": home_id, "full_name": "Boston Celtics", "abbreviation": "BOS"},
         "visitor_team": {"id": away_id, "full_name": "New York Knicks", "abbreviation": "NYK"}}
    if season_type is not None:
        g["season_type"] = season_type
    return g


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}

    def json(self):
        return self._payload


def _page(games, next_cursor=None):
    meta = {"per_page": 100}
    if next_cursor is not None:
        meta["next_cursor"] = next_cursor
    return {"data": games, "meta": meta}


def _install(monkeypatch, responses):
    calls, seq = [], list(responses)

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return seq.pop(0)

    monkeypatch.setattr(nh.requests, "get", fake_get)
    return calls


def _plan(**kw):
    kw.setdefault("expected_primary_games", 150)
    return plan_history_window(season=2026, cutoff=CUTOFF, **kw)


def _run(monkeypatch, primary_pages, ist_pages=None, **plan_kw):
    plan = _plan(**plan_kw)
    calls = _install(monkeypatch, [_Resp(p) if isinstance(p, dict) else p
                                   for p in list(primary_pages) + list(ist_pages or [])])
    fetched = fetch_history_window(plan, allow_network=True, api_key="SECRET_TEST_KEY")
    return plan, fetched, calls


G_REG = _raw(101, utc="2026-10-21T23:30:00.000Z")
G_CUP = _raw(102, utc="2026-11-07T00:00:00.000Z", ist_stage="East Group A")
G_FUT = _raw(103, utc="2026-11-15T00:30:00.000Z", state="scheduled")


# ---------------------------------------------------------------------------
# 提供商获取范围（不假设 season_type=regular 完整）
# ---------------------------------------------------------------------------

def test_plan_scope_omits_regular_and_undocumented_postseason():
    plan = _plan()
    urls = [p["url"] for p in plan["partitions"]]
    assert "season_type" not in urls[0] and "postseason" not in urls[0]
    assert "season_type=ist" in urls[1]
    assert all("season_type=regular" not in u for u in urls)
    assert plan["start_date"] == "2026-10-19" and plan["end_date"] == "2026-11-14"
    assert plan["operation"] == "READ_ONLY_HISTORICAL_COLLECTION"
    assert plan["real_production_writes"] == "PROHIBITED"
    assert plan["budget_feasible"] is True and plan["pacing_interval"] >= 12.1
    assert len(plan["plan_sha256"]) == 64


def test_date_scoped_query_preserves_nba_cup(monkeypatch):
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, G_CUP, G_FUT])], [_page([G_CUP])])
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_VERIFIED
    ids = {g["provider_game_id"]: g for g in cert["dataset"]["games"]}
    assert ids["102"]["competition_type"] == "NBA_CUP_REGULAR"
    assert cert["regular_season_games"] == 2


test_nba_cup_scope_not_silently_excluded = test_date_scoped_query_preserves_nba_cup


def test_regular_only_not_assumed_complete(monkeypatch):
    # 交叉核验返回了主分区没有的 NBA Cup 比赛 → 主分区不完整，绝不认证。
    missing_cup = _raw(104, utc="2026-11-11T00:00:00.000Z", ist_stage="West Group B")
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, G_CUP])], [_page([G_CUP, missing_cup])])
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED
    assert "NBA_CUP_SCOPE_GAP" in cert["reasons"]
    assert cert["ist_crosscheck"]["ist_ids_missing_from_primary"] == ["104"]


def test_nba_cup_championship_excluded(monkeypatch):
    champ = _raw(105, utc="2026-11-13T01:30:00.000Z", ist_stage="Championship")
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, champ])], [_page([champ])])
    cert = certify_history_window(plan, fetched)
    assert cert["window_complete"] is True
    assert {g["provider_game_id"] for g in cert["dataset"]["games"]} == {"101"}
    assert cert["competition_breakdown"]["NBA_CUP_CHAMPIONSHIP"] == 1
    assert cert["excluded_competition_count"] == 1


def test_play_in_excluded():
    r = classify_nba_competition(_raw(1, utc="2027-04-14T23:30:00.000Z"),
                                 regular_season_end_date="2027-04-11")
    assert r["competition_type"] == "PLAY_IN" and r["counts_as_regular_season"] is False


def test_preseason_excluded_by_type_and_date_boundary():
    by_type = classify_nba_competition(_raw(1, utc="2026-10-10T23:00:00.000Z", season_type="preseason"))
    by_date = classify_nba_competition(_raw(1, utc="2026-10-10T23:00:00.000Z"),
                                       regular_season_start_date="2026-10-20")
    for r in (by_type, by_date):
        assert r["competition_type"] == "PRESEASON"
        assert r["counts_as_regular_season"] is False and r["reject"] is False


def test_overlapping_categories_deduplicated(monkeypatch):
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, G_CUP])], [_page([G_CUP])])
    cert = certify_history_window(plan, fetched)
    assert cert["ist_crosscheck"]["overlap_deduplicated"] == 1
    assert cert["ist_crosscheck"]["counted_once"] is True
    assert cert["regular_season_games"] == 2


def test_overlapping_category_content_conflict_rejected(monkeypatch):
    altered = dict(G_CUP, home_team_score=99)
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, G_CUP])], [_page([altered])])
    cert = certify_history_window(plan, fetched)
    assert "OVERLAPPING_CATEGORY_CONFLICT" in cert["reasons"]
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED


def test_unknown_competition_rejected(monkeypatch):
    weird = _raw(106, utc="2026-11-01T23:00:00.000Z", ist_stage="Mystery Round")
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, weird])], [_page([])])
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED
    assert any("AMBIGUOUS_CLASSIFICATION:106" in r for r in cert["reasons"])
    assert "106" not in {g["provider_game_id"] for g in cert["dataset"]["games"]}


# ---------------------------------------------------------------------------
# 截止边界、分页完整、预算与失败即关闭
# ---------------------------------------------------------------------------

def test_cutoff_boundary_near_midnight(monkeypatch):
    before = _raw(107, utc="2026-11-14T03:59:00.000Z")
    at_cutoff = _raw(108, utc="2026-11-14T04:00:00.000Z", state="scheduled")
    plan, fetched, _ = _run(monkeypatch, [_page([before, at_cutoff])], [_page([])])
    cert = certify_history_window(plan, fetched)
    ids = {g["provider_game_id"] for g in cert["dataset"]["games"]}
    assert ids == {"107"} and cert["post_cutoff_games_excluded"] == 1
    assert cert["window_complete"] is True


def test_pre_cutoff_game_not_final_unresolved(monkeypatch):
    stuck = _raw(109, utc="2026-11-13T00:00:00.000Z", state="in_progress")
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, stuck])], [_page([])])
    cert = certify_history_window(plan, fetched)
    assert "PRE_CUTOFF_GAME_NOT_FINAL" in cert["reasons"]
    assert cert["pre_cutoff_not_final_ids"] == ["109"]


def test_date_window_complete_cursor_exhaustion(monkeypatch):
    plan, fetched, calls = _run(monkeypatch, [_page([G_REG], next_cursor=50), _page([G_CUP])],
                                [_page([G_CUP])])
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_VERIFIED
    primary = cert["page_completion_evidence"][0]
    assert primary["partition_id"] == PARTITION_ALL_NON_PRESEASON
    assert [p["request_cursor"] for p in primary["pages"]] == [None, 50]
    assert primary["pages"][-1]["next_cursor"] is None and primary["next_cursor_remaining"] is None
    assert all(len(p["payload_sha256"]) == 64 and p["received_at"] for p in primary["pages"])
    assert "cursor=50" in calls[1]
    assert "SECRET_TEST_KEY" not in json.dumps(cert, ensure_ascii=False, default=str)


def test_bounded_request_budget(monkeypatch):
    plan, fetched, calls = _run(monkeypatch, [_page([G_REG], next_cursor=50), _page([G_CUP])],
                                [_page([G_CUP])], max_requests=2)
    assert len(calls) == 2 and fetched["network_requests_made"] == 2
    ist = next(p for p in fetched["partitions"] if p["partition_id"] == PARTITION_IST_CROSSCHECK)
    assert ist["complete"] is False and ist["failure"] == "REQUEST_BUDGET_EXHAUSTED"
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED


def test_budget_includes_retries(monkeypatch):
    plan, fetched, calls = _run(monkeypatch, [_Resp({}, status=429), _Resp({}, status=429),
                                              _page([G_REG])], [], max_requests=3)
    assert len(calls) == 3
    ist = next(p for p in fetched["partitions"] if p["partition_id"] == PARTITION_IST_CROSSCHECK)
    assert ist["complete"] is False
    assert certify_history_window(plan, fetched)["window_complete"] is False


def test_incomplete_partition_fail_closed(monkeypatch):
    plan, fetched, calls = _run(monkeypatch, [_page([G_REG], next_cursor=50), _Resp({}, status=500)], [])
    assert len(calls) == 2
    parts = {p["partition_id"]: p for p in fetched["partitions"]}
    assert parts[PARTITION_ALL_NON_PRESEASON]["complete"] is False
    assert parts[PARTITION_IST_CROSSCHECK]["failure"].startswith("NOT_ATTEMPTED_AFTER_")
    cert = certify_history_window(plan, fetched)
    assert cert["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED
    assert "PARTITION_INCOMPLETE:ALL_NON_PRESEASON_TYPES" in cert["reasons"]


def test_cursor_cycle_fail_closed(monkeypatch):
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG], next_cursor=50), _page([G_CUP], next_cursor=50)], [])
    assert fetched["partitions"][0]["failure"] == "CURSOR_CYCLE"
    assert certify_history_window(plan, fetched)["window_complete"] is False


def test_window_fetch_default_deny_and_missing_credentials(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(nh.requests, "get", boom)
    plan = _plan()
    denied = fetch_history_window(plan)
    assert denied["status"] == "disabled_pending_authorization" and denied["network_requests_made"] == 0
    missing = fetch_history_window(plan, allow_network=True)
    assert missing["status"] == "disabled_missing_credentials"
    assert certify_history_window(plan, denied)["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED


def test_reconciliation_with_previous_capture(monkeypatch):
    plan, fetched, _ = _run(monkeypatch, [_page([G_REG, G_CUP, _raw(110, utc="2026-11-12T00:00:00.000Z")])],
                            [_page([G_CUP])])
    cert0 = certify_history_window(plan, fetched)
    previous = [g for g in cert0["dataset"]["games"] if g["provider_game_id"] != "110"]
    clean = certify_history_window(plan, fetched, previous_games=previous)
    rec = clean["reconciliation"]
    assert rec["newly_discovered_ids"] == ["110"] and rec["new_regular_season_games"] == ["110"]
    assert rec["missing_old_ids"] == [] and rec["conflicting_ids"] == []
    assert clean["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_VERIFIED

    changed = [dict(previous[0], home_final_score=1)] + previous[1:]
    ghost = dict(previous[0], provider_game_id="999")
    bad = certify_history_window(plan, fetched, previous_games=changed + [ghost])
    rec = bad["reconciliation"]
    assert rec["existing_scores_changed"] == ["101"] and rec["conflicting_ids"] == ["101"]
    assert rec["conflicting_records"][0]["previous"]["home_final_score"] == 1
    assert rec["conflicting_records"][0]["certified"]["home_final_score"] == 110
    assert rec["missing_old_ids"] == ["999"]
    assert bad["CURRENT_SEASON_WINDOW_COMPLETE"] == WINDOW_UNRESOLVED


# ---------------------------------------------------------------------------
# 提供商支撑的前瞻门禁
# ---------------------------------------------------------------------------

AS_OF = datetime(2026, 11, 16, 6, 0, tzinfo=BJ)
KICKOFF_UTC = "2026-11-16T00:30:00.000Z"          # 北京 08:30


def _fixture(**kw):
    f = {"id": "500j-2026-11-16-1301", "date": "2026-11-16", "time": "08:30", "kickoff_time_known": True,
         "status": "upcoming", "league": "美国职业篮球联盟", "home": "波士顿凯尔特人", "away": "纽约尼克斯",
         "market_tracked": True, "jczq_no": "1301", "odds": {"home_win": 1.55, "away_win": 2.35},
         "_home_code": "BOS", "_away_code": "NYK",
         "kickoff_provenance": {"actual_kickoff_time_source": "balldontlie_nba_games",
                                "kickoff_source_event_id": "21718300",
                                "kickoff_source_observed_at": "2026-11-16T05:58:00+08:00",
                                "kickoff_source_scheduled_start_utc": KICKOFF_UTC,
                                "kickoff_verification_status": "SCHEDULE_PROVIDER_VERIFIED"}}
    f.update(kw)
    return f


def _obs(**kw):
    o = {"provider": "balldontlie_nba_games", "provider_game_id": "21718300", "status_state": "scheduled",
         "postponed": False, "game_status": "scheduled", "home_team_identifier": "bdl:2",
         "away_team_identifier": "bdl:20", "scheduled_start_utc": KICKOFF_UTC,
         "observed_at": "2026-11-16T05:58:00+08:00"}
    o.update(kw)
    return o


def _gate(fixture=None, obs="default", count=1, **kw):
    return classify_fixture_eligibility(fixture or _fixture(), kw.pop("as_of", AS_OF),
                                        provider_observation=_obs() if obs == "default" else obs,
                                        provider_binding_count=count, **kw)


def _prov(**kw):
    p = dict(_fixture()["kickoff_provenance"])
    p.update(kw)
    return p


def test_future_with_verified_all_fields_accepted():
    g = _gate()
    assert g["fixture_eligibility"] == FIXTURE_VERIFIED_FUTURE
    assert g["comparison_classification"] == CLASS_PROSPECTIVE and g["disqualification_reason"] is None
    # 来源状态、身份置信与时序资格分别输出。
    assert g["source_status"] == "PROVIDER_SCHEDULED_CURRENT"
    assert g["identity_confidence"] == "PROVIDER_VERIFIED"
    assert g["temporal_eligibility"] == "FUTURE"
    assert g["provider_game_id"] == "21718300"


@pytest.mark.parametrize("prov_patch", [{"kickoff_source_event_id": None}, {"kickoff_source_event_id": "abc"},
                                        {"kickoff_source_event_id": "0"}])
def test_future_without_provider_id_blocked(prov_patch):
    g = _gate(_fixture(kickoff_provenance=_prov(**prov_patch)))
    assert g["fixture_eligibility"] == FIXTURE_SCHEDULE_UNVERIFIED
    assert g["comparison_classification"] == CLASS_BLOCKED
    assert g["disqualification_reason"] == "PROVIDER_GAME_ID_MISSING"


test_real_provider_id_required = test_future_without_provider_id_blocked


@pytest.mark.parametrize("prov_patch", [{"actual_kickoff_time_source": "espn_nba_scoreboard"},
                                        {"kickoff_verification_status": "OFFICIAL_NBA_VERIFIED"},
                                        {"kickoff_verification_status": "NOT_ESTABLISHED"}])
def test_future_without_verified_provenance_blocked(prov_patch):
    g = _gate(_fixture(kickoff_provenance=_prov(**prov_patch)))
    assert g["fixture_eligibility"] == FIXTURE_SCHEDULE_UNVERIFIED
    assert g["disqualification_reason"] == "SCHEDULE_PROVENANCE_UNVERIFIED"


test_verified_schedule_provenance_required = test_future_without_verified_provenance_blocked


def test_future_with_provenance_kickoff_mismatch_blocked():
    g = _gate(_fixture(kickoff_provenance=_prov(kickoff_source_scheduled_start_utc="2026-11-16T01:00:00.000Z")))
    assert g["disqualification_reason"] == "KICKOFF_PROVENANCE_MISMATCH"


def test_future_without_provenance_at_all_blocked():
    f = _fixture()
    f.pop("kickoff_provenance")
    assert _gate(f)["comparison_classification"] == CLASS_BLOCKED


@pytest.mark.parametrize("patch", [{"market_tracked": False}, {"market_tracked": None}, {"jczq_no": ""},
                                   {"jczq_no": None}])
def test_future_without_market_identity_blocked(patch):
    g = _gate(_fixture(**patch))
    assert g["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert g["disqualification_reason"] == "MARKET_IDENTITY_UNESTABLISHED"


test_market_identity_required = test_future_without_market_identity_blocked


def test_future_with_unverified_home_away_blocked():
    reversed_obs = _obs(home_team_identifier="bdl:20", away_team_identifier="bdl:2")
    g = _gate(obs=reversed_obs)
    assert g["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert g["home_away_binding_status"] == "PROVIDER_REVERSED"
    assert g["disqualification_reason"] == "HOME_AWAY_ORIENTATION_UNVERIFIED"
    other = _gate(obs=_obs(away_team_identifier="bdl:14"))
    assert other["disqualification_reason"] == "HOME_AWAY_ORIENTATION_UNVERIFIED"


def test_exact_home_away_direction_required_500lq_never_swapped():
    f = _fixture(id="500lq-2026-11-16-周日301")
    g = _gate(f)
    assert g["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert g["home_away_binding_status"] == "DISPLAY_ORDER_UNVERIFIED"
    assert f["_home_code"] == "BOS" and f["odds"]["home_win"] == 1.55


test_exact_home_away_direction_required = test_future_with_unverified_home_away_blocked


def test_future_with_stale_provider_status_blocked():
    g = _gate(obs=_obs(observed_at="2026-11-16T05:00:00+08:00"))
    assert g["fixture_eligibility"] == FIXTURE_SCHEDULE_UNVERIFIED
    assert g["disqualification_reason"] == "PROVIDER_OBSERVATION_STALE"
    assert g["provider_observation_age_seconds"] == 3600


def test_observation_after_as_of_not_treated_as_prior_information():
    g = _gate(obs=_obs(observed_at="2026-11-16T06:05:00+08:00"))
    assert g["disqualification_reason"] == "PROVIDER_OBSERVATION_AFTER_AS_OF"


@pytest.mark.parametrize("patch", [{"postponed": True}, {"status_state": "postponed"}])
def test_future_with_postponed_status_blocked(patch):
    g = _gate(obs=_obs(**patch))
    assert g["fixture_eligibility"] == FIXTURE_SCHEDULE_UNVERIFIED
    assert g["disqualification_reason"] == "PROVIDER_POSTPONED"


@pytest.mark.parametrize("obs", [None, "in_progress", "delayed", "unknown"])
def test_current_scheduled_status_required(obs):
    o = None if obs is None else _obs(status_state=obs)
    g = _gate(obs=o)
    assert g["fixture_eligibility"] == FIXTURE_SCHEDULE_UNVERIFIED
    assert g["comparison_classification"] == CLASS_BLOCKED


def test_observation_for_different_event_blocked():
    assert _gate(obs=_obs(provider_game_id="21718301"))["disqualification_reason"] == \
        "PROVIDER_OBSERVATION_EVENT_MISMATCH"


def test_kickoff_changed_since_provenance_blocked():
    g = _gate(obs=_obs(scheduled_start_utc="2026-11-16T01:30:00.000Z"))
    assert g["disqualification_reason"] == "KICKOFF_CHANGED_SINCE_PROVENANCE"


@pytest.mark.parametrize("count,reason", [(2, "DUPLICATE_PROVIDER_BINDING"),
                                          (None, "PROVIDER_BINDING_UNIQUENESS_UNVERIFIED")])
def test_future_with_duplicate_provider_binding_blocked(count, reason):
    g = _gate(count=count)
    assert g["fixture_eligibility"] == FIXTURE_IDENTITY_UNRESOLVED
    assert g["disqualification_reason"] == reason


def test_duplicate_binding_detected_across_fixture_list():
    twin = _fixture(id="500j-2026-11-16-1302", jczq_no="1302")
    comp = build_comparison({"games": [], "game_count": 0}, as_of=AS_OF, fixtures=[_fixture(), twin],
                            observations={"500j-2026-11-16-1301": _obs(), "500j-2026-11-16-1302": _obs()})
    assert {r["disqualification_reason"] for r in comp["comparison"]} == {"DUPLICATE_PROVIDER_BINDING"}
    assert comp["genuine_future_fixture_count"] == 0


def test_canonical_ownership_conflict_blocked():
    assert _gate(canonical_owner_conflict=True)["disqualification_reason"] == "CANONICAL_OWNERSHIP_CONFLICT"


def test_future_with_unknown_kickoff_blocked():
    g = _gate(_fixture(time="00:00", kickoff_time_known=False))
    assert g["fixture_eligibility"] == FIXTURE_KICKOFF_UNKNOWN
    assert g["comparison_classification"] == CLASS_BLOCKED


def test_future_with_past_kickoff_retrospective():
    g = _gate(as_of=datetime(2026, 11, 16, 9, 0, tzinfo=BJ))
    assert g["fixture_eligibility"] == FIXTURE_STALE
    assert g["comparison_classification"] == CLASS_RETROSPECTIVE


def test_prospective_as_of_before_kickoff():
    at_kickoff = _gate(as_of=datetime(2026, 11, 16, 8, 30, tzinfo=BJ),
                       obs=_obs(observed_at="2026-11-16T08:25:00+08:00"))
    assert at_kickoff["fixture_eligibility"] == FIXTURE_STALE
    just_before = _gate(as_of=datetime(2026, 11, 16, 8, 29, tzinfo=BJ),
                        obs=_obs(observed_at="2026-11-16T08:25:00+08:00"))
    assert just_before["fixture_eligibility"] == FIXTURE_VERIFIED_FUTURE


@pytest.mark.parametrize("odds", [{"home_win": 1.0, "away_win": 2.0}, {"home_win": 1.5}, {}])
def test_invalid_market_odds_market_unavailable(odds):
    g = _gate(_fixture(odds=odds))
    assert g["fixture_eligibility"] == FIXTURE_MARKET_UNAVAILABLE
    assert g["market_status"] == "MONEYLINE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# 不可变影子工件
# ---------------------------------------------------------------------------

def _hist(gid, hc_id, ac_id, hs, aws, utc, season=2026):
    games, errors = nh.parse_historical_games({"data": [_raw(gid, utc=utc, home_id=hc_id, away_id=ac_id,
                                                              hs=hs, aws=aws, season=season)]})
    assert not errors
    return games[0]


def _dataset(extra=()):
    games = [_hist(1, 2, 20, 112, 100, "2026-10-21T23:30:00.000Z"),
             _hist(2, 20, 2, 99, 104, "2026-10-30T23:30:00.000Z"),
             _hist(3, 2, 14, 120, 101, "2026-11-05T00:00:00.000Z"),
             _hist(4, 20, 14, 95, 110, "2026-11-10T00:00:00.000Z")] + list(extra)
    ds = nh.assemble_historical_dataset(games, cutoff_utc=None)
    ds["cutoff_utc"] = CUTOFF.isoformat()
    ds["certification"] = {"REGULAR_SEASON_DATASET_CERTIFIED": "VERIFIED",
                           "CURRENT_SEASON_WINDOW_COMPLETE": "VERIFIED"}
    return ds


GEN = datetime(2026, 11, 16, 6, 0, 30, tzinfo=BJ)
MARKET_AT = "2026-11-16T05:30:00+08:00"


def _market_obs(f=None, **kw):
    f = f or _fixture()
    odds = f.get("odds") or {}
    obs = {
        "canonical_match_id": f.get("id"),
        "lottery_competition_number": f.get("jczq_no"),
        "home_team": f.get("home"),
        "away_team": f.get("away"),
        "market_type": "sf",
        "home_win_odds": odds.get("home_win"),
        "away_win_odds": odds.get("away_win"),
        "market_source": "500.com",
        "market_observed_at": MARKET_AT,
    }
    obs.update(kw)
    obs["market_content_digest"] = cap._digest({k: v for k, v in obs.items() if k != "market_content_digest"})
    return obs


def _artifact(**kw):
    kw.setdefault("generated_at", GEN)
    fixture = kw.pop("fixture", _fixture())
    if "market_observation" not in kw and "market_observed_at" not in kw:
        kw["market_observation"] = _market_obs(fixture)
    elif "market_observed_at" in kw and "market_observation" not in kw:
        m_at = kw.pop("market_observed_at")
        if m_at is not None:
            kw["market_observation"] = _market_obs(fixture, market_observed_at=m_at)
        else:
            kw["market_observation"] = None
    kw.setdefault("provider_binding_count", 1)
    obs = kw.pop("observation", _obs())
    dataset = kw.pop("dataset", _dataset())
    return cap.build_shadow_artifact(fixture, obs, dataset, as_of=kw.pop("as_of", AS_OF), **kw)


def test_shadow_artifact_contents():
    a = _artifact()
    for field in ("snapshot_identity", "model_versions", "input_dataset_digest", "input_fixture_digest",
                  "original_probabilities", "market_observation", "generated_at", "verified_kickoff",
                  "source_provenance", "content_sha256", "prediction_as_of", "scheduled_kickoff_utc",
                  "scheduled_kickoff_beijing", "market_observation_digest", "bdl_provider_game_id"):
        assert field in a, field
    assert a["model_versions"] == {"v1": "basketball-margin-1", "v2_shadow": "basketball-margin-2-shadow"}
    assert a["capture_mode"] == "CONTEMPORANEOUS_PROSPECTIVE_CAPTURE"
    assert a["source_provenance"]["official_nba_verified"] is False
    p = a["original_probabilities"]
    for k in ("v1_raw", "v2_raw", "v2_display"):
        assert abs(p[k]["home_win"] + p[k]["away_win"] - 1.0) < 1e-9
    assert a["v2_inputs"]["home_historical_games_used"] == 3
    assert a["content_sha256"] == cap.content_sha256(a)
    assert a["writes_protected_stores"] is False and a["promotion_authorized"] is False


def test_prospective_capture_requires_generation_before_kickoff():
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(generated_at=datetime(2026, 11, 16, 8, 30, tzinfo=BJ))
    assert exc.value.reason == "PREDICTION_NOT_BEFORE_KICKOFF"
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(generated_at=datetime(2026, 11, 16, 5, 59, tzinfo=BJ))
    assert exc.value.reason == "AS_OF_AFTER_GENERATION_TIME"


@pytest.mark.parametrize("kw,reason", [
    ({"observation": None}, "SCHEDULE_STATUS_UNVERIFIED:FRESH_PROVIDER_OBSERVATION_UNAVAILABLE"),
    ({"market_observed_at": "2026-11-16T06:10:00+08:00"}, "MARKET_OBSERVATION_AFTER_PREDICTION"),
    ({"market_observed_at": None}, "MISSING_MATCH_ODDS_PROVENANCE"),
])
def test_capture_blocked_preconditions(kw, reason):
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(**kw)
    assert exc.value.reason == reason


@pytest.mark.parametrize("cert", [{}, {"REGULAR_SEASON_DATASET_CERTIFIED": "VERIFIED",
                                       "CURRENT_SEASON_WINDOW_COMPLETE": "UNRESOLVED"},
                                  {"REGULAR_SEASON_DATASET_CERTIFIED": "PARTIAL",
                                   "CURRENT_SEASON_WINDOW_COMPLETE": "VERIFIED"}])
def test_capture_requires_certified_history(cert):
    ds = _dataset()
    ds["certification"] = cert
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(dataset=ds)
    assert exc.value.reason == "HISTORICAL_DATASET_NOT_CERTIFIED"


def test_capture_insufficient_real_team_data_blocked():
    ds = _dataset()
    ds["games"] = [g for g in ds["games"] if g["home_code"] != "BOS" and g["away_code"] != "BOS"]
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(dataset=ds)
    assert exc.value.reason == "INSUFFICIENT_REAL_TEAM_DATA"


def test_historical_results_no_future_leakage():
    base = _artifact()
    # 一场在 as_of 之后才结束（甚至尚未开赛）的悬殊比分绝不能影响 V2。
    leak = _hist(9, 2, 20, 150, 60, "2026-11-16T03:00:00.000Z")
    leaky = _artifact(dataset=_dataset(extra=[leak]))
    assert leaky["original_probabilities"]["v2_raw"] == base["original_probabilities"]["v2_raw"]
    tv = leaky["temporal_input_verification"]
    assert tv["satisfied"] is True and tv["inputs_excluded_at_or_after_as_of"] == 1
    assert datetime.fromisoformat(tv["latest_input_game_end"]) < AS_OF
    assert tv["reconstructed_historical_research"] is False


def test_shadow_snapshot_immutable(tmp_path_factory):
    out = tmp_path_factory.mktemp("prospective-shadow")
    a = _artifact()
    path, outcome = cap.write_shadow_artifact(a, out)
    assert outcome == "CREATED" and path.name == f"{a['snapshot_identity']}.json"
    original = path.read_bytes()
    again, outcome = cap.write_shadow_artifact(a, out)
    assert outcome == "IDEMPOTENT_EXISTING" and again == path
    # 同一身份（同场、同模型、同 as_of）但内容不同 → 拒绝覆盖。
    altered = _artifact(generated_at=GEN + timedelta(seconds=5))
    assert altered["snapshot_identity"] == a["snapshot_identity"]
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        cap.write_shadow_artifact(altered, out)
    assert exc.value.reason == "SHADOW_ARTIFACT_IMMUTABLE"
    assert path.read_bytes() == original
    assert cap.verify_shadow_artifact(path)["valid"] is True
    tampered = dict(a)
    tampered["original_probabilities"] = {"v1_raw": {"home_win": 0.99, "away_win": 0.01}}
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        cap.write_shadow_artifact(tampered, tmp_path_factory.mktemp("other"))
    assert exc.value.reason == "ARTIFACT_CONTENT_DIGEST_MISMATCH"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["original_probabilities"]["v2_raw"]["home_win"] = 0.5
    path.write_text(json.dumps(doc), encoding="utf-8")
    assert cap.verify_shadow_artifact(path)["valid"] is False


def test_shadow_capture_no_production_writes(isolated_data_dir, tmp_path_factory):
    for name in STORES:
        (isolated_data_dir / name).write_text(json.dumps({"version": 1, "seed": name}), encoding="utf-8")

    def digests():
        return {n: hashlib.sha256((isolated_data_dir / n).read_bytes()).hexdigest() for n in STORES}

    before = digests()
    a = _artifact()
    cap.write_shadow_artifact(a, tmp_path_factory.mktemp("reports"))
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        cap.write_shadow_artifact(a, isolated_data_dir / "shadow")
    assert exc.value.reason == "OUTPUT_DIR_INSIDE_DATA_DIR"
    assert digests() == before
    assert not (isolated_data_dir / "shadow").exists()


def test_cli_capture_default_deny_writes_nothing(isolated_data_dir, tmp_path_factory, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(nh.requests, "get", boom)
    monkeypatch.setattr(ns.requests, "get", boom)
    daily = {"meta": {"last_refresh": MARKET_AT}, "matches": [dict(
        {k: v for k, v in _fixture().items() if not k.startswith("_")}, sport="basketball")]}
    (isolated_data_dir / "daily_matches.json").write_text(json.dumps(daily, ensure_ascii=False), encoding="utf-8")
    ds_path = tmp_path_factory.mktemp("ds") / "dataset.json"
    ds_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    out = tmp_path_factory.mktemp("out")
    before = (isolated_data_dir / "daily_matches.json").read_bytes()
    result = cap._capture(argparse.Namespace(dataset=str(ds_path), match_id="500j-2026-11-16-1301",
                                             output_dir=str(out), allow_network=False))
    assert result["status"] == "BLOCKED" and result["reason"] == "FRESH_PROVIDER_OBSERVATION_UNAVAILABLE"
    assert result["network_requests"] == 0
    assert list(out.iterdir()) == []
    assert (isolated_data_dir / "daily_matches.json").read_bytes() == before


def test_cli_capture_binds_in_memory_and_writes_only_artifact(isolated_data_dir, tmp_path_factory, monkeypatch):
    # 存储记录尚无 BDL 溯源且开赛时间未知：绑定只发生在内存副本上，受保护存储字节不变。
    stored = {k: v for k, v in _fixture(time="00:00", kickoff_time_known=False).items()
              if not k.startswith("_") and k != "kickoff_provenance"}
    daily = {"meta": {"last_refresh": MARKET_AT}, "matches": [dict(stored, sport="basketball")]}
    for name in STORES:
        (isolated_data_dir / name).write_text(json.dumps({"seed": name}), encoding="utf-8")
    odds_snap = {
        "match_id": "500j-2026-11-16-1301",
        "jczq_no": "1301",
        "home_team": "波士顿凯尔特人",
        "away_team": "纽约尼克斯",
        "captured_at": MARKET_AT,
        "source": "500.com",
        "odds": {"home_win": 1.55, "away_win": 2.35},
        "odds_fingerprint": "fake",
        "snapshot_id": "fake_odds_snap",
    }
    (isolated_data_dir / "odds_snapshots.json").write_text(
        json.dumps({"version": 1, "snapshots": {"fake_odds_snap": odds_snap}}, ensure_ascii=False),
        encoding="utf-8")
    (isolated_data_dir / "daily_matches.json").write_text(json.dumps(daily, ensure_ascii=False), encoding="utf-8")
    before = {n: (isolated_data_dir / n).read_bytes() for n in STORES}
    observation = _obs(beijing_date="2026-11-16", time_parse_status="VALID_UTC", league_uid="balldontlie_nba")
    calls = []
    monkeypatch.setattr(cap, "fetch_nba_schedule_observations",
                        lambda dates, **kw: calls.append((dates, kw)) or ([observation], {"status": "ok", "rows": 1}))
    monkeypatch.setattr(cap, "get_beijing_now", lambda: AS_OF)
    ds_path = tmp_path_factory.mktemp("ds") / "dataset.json"
    ds_path.write_text(json.dumps(_dataset()), encoding="utf-8")
    out = tmp_path_factory.mktemp("out")
    result = cap._capture(argparse.Namespace(dataset=str(ds_path), match_id="500j-2026-11-16-1301",
                                             output_dir=str(out), allow_network=True))
    assert result["status"] == "CAPTURED", result
    assert calls[0][0] == ["2026-11-16"] and calls[0][1]["max_requests"] == 3
    artifact = json.loads((out / f"{result['snapshot_identity']}.json").read_text(encoding="utf-8"))
    assert artifact["bdl_provider_game_id"] == "21718300"
    assert artifact["verified_kickoff"] == "2026-11-16T08:30:00+08:00"
    assert cap.verify_shadow_artifact(out / f"{result['snapshot_identity']}.json")["valid"] is True
    assert {n: (isolated_data_dir / n).read_bytes() for n in STORES} == before


def test_v1_production_model_unchanged():
    assert config.MODEL_VERSIONS["basketball"] == "basketball-margin-1"
    a = _artifact()
    v1 = _v1_for_fixture(_fixture())
    assert a["original_probabilities"]["v1_raw"]["home_win"] == round(v1["model_probabilities"]["home_win"] / 100, 6)
    home = {"name": "波士顿凯尔特人", "elo_rating": 1700, "matches_played": 0}
    away = {"name": "纽约尼克斯", "elo_rating": 1700, "matches_played": 0}
    assert predict_basketball(home, away, None, "美国职业篮球联盟")["model_probabilities"] == \
        predict_basketball(home, away, None, "美国职业篮球联盟")["model_probabilities"]


# ---------------------------------------------------------------------------
# 只读评估契约
# ---------------------------------------------------------------------------

def _result(**kw):
    r = {"canonical_match_id": "500j-2026-11-16-1301", "provider_game_id": "21718300",
         "status_state": "final", "officially_confirmed": True, "home_score": 108, "away_score": 101}
    r.update(kw)
    return r


def test_evaluation_join_contract():
    a = _artifact()
    ev = cap.evaluate_shadow_artifact(a, _result())
    assert ev["status"] == "EVALUATED" and ev["home_won"] is True
    for k in ("v1", "v2_shadow", "market"):
        assert 0.0 <= ev[k]["binary_brier"] <= 1.0
    assert ev["writes_protected_stores"] is False
    assert cap.evaluate_shadow_artifact(a, _result(provider_game_id="1"))["reason"] == "EVENT_IDENTITY_MISMATCH"
    assert cap.evaluate_shadow_artifact(a, _result(officially_confirmed=False))["status"] == "PENDING"
    assert cap.evaluate_shadow_artifact(a, _result(away_score=108))["reason"] == "MALFORMED_FINAL_SCORE"
    tampered = dict(a, original_probabilities={"v1_raw": {"home_win": 0.9, "away_win": 0.1}})
    assert cap.evaluate_shadow_artifact(tampered, _result())["reason"] == "ARTIFACT_CONTENT_DIGEST_MISMATCH"


# ---------------------------------------------------------------------------
# 三项最小捕获修正回归用例（Workstream B & Section 8）
# ---------------------------------------------------------------------------

def test_per_fixture_odds_provenance():
    # 1. 缺失单场市场观察直接拒绝
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(market_observation=None)
    assert exc.value.reason == "MISSING_MATCH_ODDS_PROVENANCE"

    # 2. Canonical ID 不匹配拒绝
    bad_id_obs = _market_obs(canonical_match_id="other-match-id")
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(market_observation=bad_id_obs)
    assert exc.value.reason == "MARKET_EVENT_IDENTITY_MISMATCH"

    # 3. 双方球队不匹配拒绝
    bad_teams_obs = _market_obs(home_team="金州勇士")
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(market_observation=bad_teams_obs)
    assert exc.value.reason == "MARKET_EVENT_IDENTITY_MISMATCH"

    # 4. 盘口赔率不一致拒绝
    bad_odds_obs = _market_obs(home_win_odds=9.99)
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(market_observation=bad_odds_obs)
    assert exc.value.reason == "ODDS_VALUES_DO_NOT_MATCH_OBSERVATION"

    # 5. 观察时刻晚于预测 as_of 拒绝
    late_obs = _market_obs(market_observed_at="2026-11-16T06:10:00+08:00")
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(market_observation=late_obs)
    assert exc.value.reason == "MARKET_OBSERVATION_AFTER_PREDICTION"


def test_global_refresh_timestamp_not_accepted_as_odds_evidence(monkeypatch):
    monkeypatch.setattr(cap, "latest_odds_snapshot", lambda match_id: None)
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        cap.build_shadow_artifact(_fixture(), _obs(), _dataset(), as_of=AS_OF, generated_at=GEN,
                                  market_observed_at="2026-11-16T05:30:00+08:00", provider_binding_count=1)
    assert exc.value.reason == "MISSING_MATCH_ODDS_PROVENANCE"


def test_canonical_owner_uniqueness_across_statuses():
    canonical_matches = [
        _fixture(id="500j-2026-11-16-1301", status="upcoming"),
        _fixture(id="500j-2026-11-15-1200", status="finished", jczq_no="1200"),
    ]
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(all_canonical_matches=canonical_matches)
    assert exc.value.reason == "CANONICAL_OWNER_CONFLICT"


def test_reversed_home_away_rejected():
    reversed_match = dict(_fixture(id="500lq-2026-11-16-1301"))
    reversed_match["home"] = _fixture()["away"]
    reversed_match["away"] = _fixture()["home"]
    canonical_matches = [_fixture(), reversed_match]
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(all_canonical_matches=canonical_matches)
    assert exc.value.reason == "REVERSED_HOME_AWAY_REJECTED"


def test_existing_shadow_artifact_content_rehashed(tmp_path_factory):
    out = tmp_path_factory.mktemp("shadow-rehash")
    a = _artifact()
    path, outcome = cap.write_shadow_artifact(a, out)
    assert outcome == "CREATED"

    # 未篡改的幂等写入
    again, outcome = cap.write_shadow_artifact(a, out)
    assert outcome == "IDEMPOTENT_EXISTING"

    # 篡改落盘文件内容
    data = json.loads(path.read_text(encoding="utf-8"))
    data["original_probabilities"]["v2_raw"]["home_win"] = 0.999
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        cap.write_shadow_artifact(a, out)
    assert exc.value.reason == "EXISTING_ARTIFACT_TAMPERED"


def test_certified_historical_input_bound():
    ds = _dataset()
    ds["certification"] = {"REGULAR_SEASON_DATASET_CERTIFIED": "PARTIAL", "CURRENT_SEASON_WINDOW_COMPLETE": "VERIFIED"}
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(dataset=ds)
    assert exc.value.reason == "HISTORICAL_DATASET_NOT_CERTIFIED"


def test_current_season_cutoff_not_overstated():
    ds = _dataset()
    ds["cutoff_utc"] = "2026-11-17T00:00:00+00:00"
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(dataset=ds)
    assert exc.value.reason == "HISTORICAL_WINDOW_CUTOFF_INVALID"


def test_verified_future_fixture_required():
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(as_of=datetime(2026, 11, 16, 9, 0, tzinfo=BJ))
    assert exc.value.reason.startswith("STALE_STORED_UPCOMING")


def test_real_pregame_timestamp_order():
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(generated_at=datetime(2026, 11, 16, 5, 59, tzinfo=BJ))
    assert exc.value.reason == "AS_OF_AFTER_GENERATION_TIME"


def test_no_postgame_reconstruction():
    with pytest.raises(cap.ShadowCaptureBlocked) as exc:
        _artifact(generated_at=datetime(2026, 11, 16, 8, 30, tzinfo=BJ))
    assert exc.value.reason == "PREDICTION_NOT_BEFORE_KICKOFF"


# 规范全大写测试场景映射
test_PER_FIXTURE_ODDS_PROVENANCE = test_per_fixture_odds_provenance
test_GLOBAL_REFRESH_TIMESTAMP_NOT_ACCEPTED_AS_ODDS_EVIDENCE = test_global_refresh_timestamp_not_accepted_as_odds_evidence
test_CANONICAL_OWNER_UNIQUENESS_ACROSS_STATUSES = test_canonical_owner_uniqueness_across_statuses
test_REVERSED_HOME_AWAY_REJECTED = test_reversed_home_away_rejected
test_EXISTING_SHADOW_ARTIFACT_CONTENT_REHASHED = test_existing_shadow_artifact_content_rehashed
test_CERTIFIED_HISTORICAL_INPUT_BOUND = test_certified_historical_input_bound
test_CURRENT_SEASON_CUTOFF_NOT_OVERSTATED = test_current_season_cutoff_not_overstated
test_VERIFIED_FUTURE_FIXTURE_REQUIRED = test_verified_future_fixture_required
test_REAL_PREGAME_TIMESTAMP_ORDER = test_real_pregame_timestamp_order
test_NO_POSTGAME_RECONSTRUCTION = test_no_postgame_reconstruction
test_PRODUCTION_V1_UNCHANGED = test_v1_production_model_unchanged
test_SHADOW_OUTPUT_OUTSIDE_PRODUCTION_STORES = test_shadow_capture_no_production_writes

