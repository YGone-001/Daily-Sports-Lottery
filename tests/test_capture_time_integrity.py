"""
Phase 3-P — Pregame Evidence Capture-Time Integrity and Schedule Provider Acceptance Tests
==========================================================================================
验证重点：
1. 刷新启动时间与实际捕获时刻边界隔离（Workstream A & B）
2. 跨开赛时间的刷新/模型评估/锁等待门禁拒绝（Workstream B）
3. 开赛时间溯源内部矛盾在持久化写入边界的严格拒绝（Workstream C）
4. 第三方公开赛程源分类、未授权自动抓取安全禁用与平滑降级（Workstream D）
5. 严格 NBA 联赛身份校验、误导性子串与非 NBA 赛事隔离（Workstream E）
6. 赛程观测时效性、缓存时空语义与部分日期窗口抓取故障分类（Workstream F）
7. 孤立端到端正反演练与历史证据不可变性（Workstream G）
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import tempfile
import threading
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import config
from utils.daily_loader import BEIJING_TZ, override_beijing_now
from utils import (
    fetcher_500,
    match_identity,
    nba_schedule,
    odds_snapshots,
    prediction_snapshots,
    scraper,
)


@pytest.fixture
def isolated_data_dir(tmp_path):
    orig = config.DATA_DIR
    config.DATA_DIR = str(tmp_path)
    try:
        yield tmp_path
    finally:
        config.DATA_DIR = orig


def _make_nba_match(**overrides):
    base = {
        "id": "500j-2030-05-15-4306",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "home": "费城76人",
        "away": "多伦多猛龙",
        "status": "upcoming",
        "kickoff_time_known": True,
        "odds": {"sf": {"home_win": "1.80", "away_win": "2.00"}},
        "home_team": {"name": "费城76人", "elo": 1500},
        "away_team": {"name": "多伦多猛龙", "elo": 1500},
        "jczq_no": "4306",
    }
    base.update(overrides)
    return base


def _espn_payload(events, leagues=None):
    return {
        "leagues": leagues if leagues is not None else [
            {
                "id": "46",
                "uid": "s:40~l:46",
                "name": "National Basketball Association",
                "abbreviation": "NBA",
                "slug": "nba",
                "season": {"year": 2030, "displayName": "2029-30"},
            }
        ],
        "events": events,
    }


def _espn_event(
    gid="401909976",
    utc="2030-05-15T00:00Z",
    status_name="STATUS_SCHEDULED",
    uid=None,
    h_id="20",
    a_id="28",
    h_name="Philadelphia 76ers",
    a_name="Toronto Raptors",
):
    return {
        "id": gid,
        "uid": uid or f"s:40~l:46~e:{gid}",
        "date": utc,
        "status": {"type": {"name": status_name}},
        "competitions": [
            {
                "id": gid,
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {
                            "id": h_id,
                            "displayName": h_name,
                            "abbreviation": "PHI",
                        },
                    },
                    {
                        "homeAway": "away",
                        "team": {
                            "id": a_id,
                            "displayName": a_name,
                            "abbreviation": "TOR",
                        },
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. 刷新时间戳与实际捕获时刻边界隔离测试 (Workstream A & B)
# ---------------------------------------------------------------------------

def test_refresh_start_vs_actual_capture_time_reproduction(isolated_data_dir):
    """
    确定性复现缺陷与修复验证：
    kickoff = 2030-05-15T08:00:00+08:00
    refresh_started_at = 2030-05-15T07:59:58+08:00 (开赛前 2 秒)
    actual_attempt_at = 2030-05-15T08:00:05+08:00 (开赛后 5 秒)
    断言：即使刷新启动于开赛前，只要实际执行捕获时已跨越开赛时间，必须坚决拒绝！
    """
    kickoff = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)
    refresh_started_at = datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ)
    actual_attempt_at = datetime(2030, 5, 15, 8, 0, 5, tzinfo=BEIJING_TZ)

    m = _make_nba_match(kickoff_time_known=True, time="08:00", date="2030-05-15")

    with override_beijing_now(actual_attempt_at):
        # 1. 预测快照拒绝验证
        pred_res = prediction_snapshots.capture_snapshot(
            m, refresh_started_at=refresh_started_at
        )
        assert pred_res.created is False
        assert pred_res.classification == prediction_snapshots.KICKOFF_REACHED_BEFORE_CAPTURE
        assert pred_res.snapshot is None

        # 2. 赔率快照拒绝验证
        odds_snap = odds_snapshots.record_odds_snapshot(
            m, refresh_started_at=refresh_started_at
        )
        assert odds_snap is None


def test_cross_kickoff_refresh_pipeline_execution(isolated_data_dir, monkeypatch):
    """
    全刷新管道跨开赛时序测试：
    刷新于 07:59:50 启动，但抓取与预检完成后时钟推进到 08:00:10。
    断言：管道内不产生任何伪造的赛前预测与赔率快照！
    """
    kickoff = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)
    t_start = datetime(2030, 5, 15, 7, 59, 50, tzinfo=BEIJING_TZ)
    t_later = datetime(2030, 5, 15, 8, 0, 10, tzinfo=BEIJING_TZ)

    m = _make_nba_match(date="2030-05-15", time="08:00")
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])
    monkeypatch.setattr(
        fetcher_500,
        "fetch_jczq_xml",
        lambda sport: [deepcopy(m)] if sport == "basketball" else [],
    )

    current_simulated_time = t_start
    def _clock():
        return current_simulated_time

    with override_beijing_now(_clock):
        # 刷新启动时为 t_start
        # 模拟在抓取与合并后时钟推进到开赛后的 t_later
        def _advancing_calibrate(matches):
            nonlocal current_simulated_time
            current_simulated_time = t_later
            return 0

        monkeypatch.setattr(scraper, "_calibrate_from_finished", _advancing_calibrate)
        monkeypatch.setattr(scraper, "get_beijing_now", _clock)

        res = scraper.refresh(verbose=False)
        assert res["prediction_snapshots_added"] == 0
        assert res["odds_snapshots_added"] == 0


def test_cross_kickoff_model_calculation_rejection(isolated_data_dir, monkeypatch):
    """
    耗时模型计算跨越开赛时间门禁测试：
    决策时刻为 07:59:59，但在模型计算过程中耗时 3 秒，完成时已达 08:00:02。
    断言：必须坚决拒绝并返回 KICKOFF_REACHED_DURING_MODEL_EVALUATION！
    """
    kickoff = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)
    t_decision = datetime(2030, 5, 15, 7, 59, 59, tzinfo=BEIJING_TZ)
    t_post_model = datetime(2030, 5, 15, 8, 0, 2, tzinfo=BEIJING_TZ)

    current_simulated_time = t_decision
    def _clock():
        return current_simulated_time

    m = _make_nba_match(date="2030-05-15", time="08:00")

    from models import predictor
    orig_predict = predictor.predict_match
    def _slow_predict(*a, **kw):
        nonlocal current_simulated_time
        current_simulated_time = t_post_model
        return orig_predict(*a, **kw)

    monkeypatch.setattr(predictor, "predict_match", _slow_predict)

    with override_beijing_now(_clock):
        res = prediction_snapshots.capture_snapshot(m)
        assert res.created is False
        assert res.classification == prediction_snapshots.KICKOFF_REACHED_DURING_MODEL_EVALUATION


def test_cross_kickoff_lock_contention_rejection(isolated_data_dir, monkeypatch):
    """
    排队等待锁期间跨越开赛时间门禁测试：
    模型于 07:59:59.5 完成，但在等待跨进程写入锁时阻塞，获取到锁时已达 08:00:01。
    断言：必须坚决拒绝并返回 KICKOFF_REACHED_DURING_LOCK_WAIT！
    """
    kickoff = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)
    t_before = datetime(2030, 5, 15, 7, 59, 59, tzinfo=BEIJING_TZ)
    t_post_lock = datetime(2030, 5, 15, 8, 0, 1, tzinfo=BEIJING_TZ)

    current_simulated_time = t_before
    def _clock():
        return current_simulated_time

    m = _make_nba_match(date="2030-05-15", time="08:00")

    from utils import concurrency
    orig_acquire = concurrency.acquire_sidecar_lock
    class _SlowLock:
        def __init__(self, inner):
            self.inner = inner
        def __enter__(self):
            nonlocal current_simulated_time
            current_simulated_time = t_post_lock
            return self.inner.__enter__()
        def __exit__(self, *a):
            return self.inner.__exit__(*a)

    def _slow_acquire(*a, **kw):
        lease = orig_acquire(*a, **kw)
        return _SlowLock(lease)

    monkeypatch.setattr(prediction_snapshots, "acquire_sidecar_lock", _slow_acquire)

    with override_beijing_now(_clock):
        res = prediction_snapshots.capture_snapshot(m)
        assert res.created is False
        assert res.classification == prediction_snapshots.KICKOFF_REACHED_DURING_LOCK_WAIT


def test_pregame_snapshot_admission_and_immutability(isolated_data_dir):
    """
    真正赛前合规快照准入与不可变性：
    在开赛前半小时捕获，应成功创建且 generated_at < kickoff_at；
    再次调用返回 EXISTING_IMMUTABLE_SNAPSHOT 且数据不可修改。
    """
    t_early = datetime(2030, 5, 15, 7, 30, 0, tzinfo=BEIJING_TZ)
    m = _make_nba_match(date="2030-05-15", time="08:00")

    with override_beijing_now(t_early):
        res1 = prediction_snapshots.capture_snapshot(m)
        assert res1.created is True
        assert res1.classification == prediction_snapshots.CAPTURED_BEFORE_KICKOFF
        snap1 = res1.snapshot
        assert snap1["generated_at"] < snap1["kickoff_at"]

        # 重复调用
        res2 = prediction_snapshots.capture_snapshot(m)
        assert res2.created is False
        assert res2.classification == prediction_snapshots.EXISTING_IMMUTABLE_SNAPSHOT
        assert res2.snapshot["snapshot_id"] == snap1["snapshot_id"]


# ---------------------------------------------------------------------------
# 2. 开赛时间溯源矛盾持久化写入边界门禁测试 (Workstream C)
# ---------------------------------------------------------------------------

def test_provenance_conflict_enforced_at_write_boundary(isolated_data_dir):
    """
    持久化边界溯源矛盾拒绝测试：
    比赛记录带有内部矛盾的开赛时间溯源（例如实际源为 ESPN 但伪称 OFFICIAL_NBA_VERIFIED，
    或者声明的 UTC 与比赛时间对不上）。
    断言：即使 get_match_datetime 能够返回有效时间，持久化写入边界必须 fail closed 拒绝！
    """
    m = _make_nba_match(
        date="2030-05-15",
        time="08:00",
        kickoff_provenance={
            "canonical_origin_source": "500j",
            "actual_kickoff_time_source": "espn_nba_scoreboard",
            "kickoff_source_event_id": "401909976",
            "kickoff_source_observed_at": "2030-05-14T15:00:00+08:00",
            "kickoff_source_scheduled_start_utc": "2030-05-15T00:00:00Z",
            "kickoff_verification_status": "OFFICIAL_NBA_VERIFIED",  # 篡改：ESPN 源伪造官方验证
        },
    )
    t_pre = datetime(2030, 5, 15, 7, 0, 0, tzinfo=BEIJING_TZ)

    # 1. 纯诊断函数检出矛盾
    is_valid, prov_status = match_identity.validate_kickoff_provenance_consistency(m)
    assert is_valid is False
    assert prov_status == match_identity.PROVENANCE_CONFLICT

    with override_beijing_now(t_pre):
        # 2. 预测快照写入边界拒绝
        res = prediction_snapshots.capture_snapshot(m)
        assert res.created is False
        assert res.classification == prediction_snapshots.PROVENANCE_CONFLICT

        # 3. 赔率快照写入边界拒绝
        odds_snap = odds_snapshots.record_odds_snapshot(m)
        assert odds_snap is None


def test_legacy_kickoff_source_compatibility(isolated_data_dir):
    """
    历史合法数据源兼容性保证：
    传统足球或 500lq 篮球比赛无结构化 kickoff_provenance 字段，但具备有效真实开赛时间。
    断言：validate_kickoff_provenance_consistency 返回 PROVENANCE_NOT_ESTABLISHED 并允许赛前准入。
    """
    legacy_match = _make_nba_match(
        id="500lq-2030-05-15-4306",
        date="2030-05-15",
        time="08:00",
        kickoff_provenance=None,
    )
    t_pre = datetime(2030, 5, 15, 7, 0, 0, tzinfo=BEIJING_TZ)

    is_valid, prov_status = match_identity.validate_kickoff_provenance_consistency(legacy_match)
    assert is_valid is True
    assert prov_status == match_identity.PROVENANCE_NOT_ESTABLISHED

    with override_beijing_now(t_pre):
        res = prediction_snapshots.capture_snapshot(legacy_match)
        assert res.created is True
        assert res.classification == prediction_snapshots.CAPTURED_BEFORE_KICKOFF


# ---------------------------------------------------------------------------
# 3. 第三方公开赛程源契约与自动抓取安全门禁 (Workstream D)
# ---------------------------------------------------------------------------

def test_third_party_provider_classification_and_disabled_network_safety():
    """
    第三方赛程提供商契约与未授权安全禁用：
    1. 提供商分类常量语义严谨
    2. 当 allow_network=False 时，自动禁用网络请求并暴露 DISABLED_PENDING_AUTHORIZATION
    3. 离线赛程解析完全不受影响
    """
    assert nba_schedule.PROVIDER_TYPE_ESPN_THIRD_PARTY == "ESPN_THIRD_PARTY_SCHEDULE_SOURCE"
    assert nba_schedule.PROVIDER_TYPE_UNSUPPORTED_PUBLIC == "UNSUPPORTED_PUBLIC_ENDPOINT"
    assert nba_schedule.PROVIDER_AUTH_UNVERIFIED == "PROVIDER_USAGE_AUTHORIZATION_UNVERIFIED"
    assert nba_schedule.ENABLE_NBA_SCHEDULE_NETWORK is False

    rows, diag = nba_schedule.fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ),
        allow_network=False,
    )
    assert rows == []
    assert diag["status"] == "disabled_pending_authorization"
    assert diag["automated_provider_operation"] == nba_schedule.AUTOMATED_OPERATION_DISABLED
    assert "PROVIDER_AUTOMATED_USE_UNAUTHORIZED" in diag["errors"]


# ---------------------------------------------------------------------------
# 4. 严格 NBA 联赛身份校验矩阵 (Workstream E)
# ---------------------------------------------------------------------------

def test_strict_league_identity_validation_matrix():
    """
    涵盖 Workstream E 要求的完整联赛与载荷身份判定矩阵：
    - 合法 NBA 载荷
    - 显式 WNBA 过滤
    - 缺失 league_uid
    - 缺失 event_uid
    - 误导性子串 (l:460)
    - 矛盾联赛元数据
    - 缺失球队 ID
    - 相互冲突的重复事件 ID
    """
    obs_time = datetime(2030, 5, 14, 18, 0, tzinfo=BEIJING_TZ)

    # 1. 合法 NBA 载荷正常解析
    valid_payload = _espn_payload([_espn_event(gid="1001", utc="2030-05-15T00:00Z")])
    rows, errs = nba_schedule.parse_espn_nba_scoreboard(valid_payload, observed_at=obs_time)
    assert len(rows) == 1 and errs == []

    # 2. 显式 WNBA 过滤
    wnba_payload = _espn_payload(
        [_espn_event(gid="1002", utc="2030-05-15T00:00Z", uid="s:40~l:59~e:1002")],
        leagues=[{"id": "59", "uid": "s:40~l:59", "abbreviation": "WNBA", "slug": "wnba"}],
    )
    rows_w, errs_w = nba_schedule.parse_espn_nba_scoreboard(wnba_payload, observed_at=obs_time)
    assert rows_w == [] and "WNBA_LEAGUE_FILTERED" in errs_w

    # 3. 缺失 league_uid
    no_uid_payload = _espn_payload(
        [_espn_event(gid="1003")],
        leagues=[{"id": "46", "abbreviation": "NBA"}],
    )
    rows_nu, errs_nu = nba_schedule.parse_espn_nba_scoreboard(no_uid_payload, observed_at=obs_time)
    assert rows_nu == [] and "MISSING_LEAGUE_IDENTITY" in errs_nu

    # 4. 缺失 event_uid
    ev_no_uid = _espn_event(gid="1004")
    ev_no_uid["uid"] = ""
    rows_neu, errs_neu = nba_schedule.parse_espn_nba_scoreboard(_espn_payload([ev_no_uid]), observed_at=obs_time)
    assert rows_neu == [] and "MISSING_EVENT_UID" in errs_neu

    # 5. 误导性子串 (例如 l:460 包含 46 但不是 l:46)
    misleading_payload = _espn_payload(
        [_espn_event(gid="1005", uid="s:40~l:460~e:1005")],
        leagues=[{"id": "460", "uid": "s:40~l:460", "abbreviation": "OTHER", "slug": "other"}],
    )
    rows_m, errs_m = nba_schedule.parse_espn_nba_scoreboard(misleading_payload, observed_at=obs_time)
    assert rows_m == [] and "MISLEADING_LEAGUE_SUBSTRING" in errs_m

    # 6. 矛盾元数据 (abbr 是 NBA 但 uid 是非 NBA)
    contradictory_payload = _espn_payload(
        [_espn_event(gid="1006")],
        leagues=[{"id": "59", "uid": "s:40~l:59", "abbreviation": "NBA", "slug": "nba"}],
    )
    rows_c, errs_c = nba_schedule.parse_espn_nba_scoreboard(contradictory_payload, observed_at=obs_time)
    assert rows_c == [] and "CONTRADICTORY_LEAGUE_METADATA" in errs_c

    # 7. 缺失球队 ID
    ev_no_team = _espn_event(gid="1007")
    ev_no_team["competitions"][0]["competitors"][0]["team"]["id"] = ""
    rows_nt, errs_nt = nba_schedule.parse_espn_nba_scoreboard(_espn_payload([ev_no_team]), observed_at=obs_time)
    assert rows_nt == [] and "MISSING_TEAM_ID:1007" in errs_nt

    # 8. 冲突的重复事件 ID (同 gid 但主客队对不同)
    ev_dup_a = _espn_event(gid="1008", h_id="20", a_id="28")
    ev_dup_b = _espn_event(gid="1008", h_id="21", a_id="22")
    rows_dup, errs_dup = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([ev_dup_a, ev_dup_b]), observed_at=obs_time
    )
    assert len(rows_dup) == 1
    assert "CONFLICTING_DUPLICATE_EVENT:1008" in errs_dup


# ---------------------------------------------------------------------------
# 5. 赛程观测时效性与部分日期抓取故障分类 (Workstream F)
# ---------------------------------------------------------------------------

def test_schedule_observation_freshness_and_cache(monkeypatch):
    """
    观测新鲜度与缓存时空语义：
    1. 首次网络抓取包含真实的 request_started_at 与 response_received_at
    2. 缓存命中时暴露 cache_hit=True 与 observation_age_seconds
    3. 缓存命中绝不伪造行内 observed_at！
    """
    nba_schedule.clear_schedule_cache()
    t_obs = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)

    mock_resp = {
        "leagues": [{"id": "46", "uid": "s:40~l:46", "name": "NBA", "abbreviation": "NBA"}],
        "events": [_espn_event(gid="2001", utc="2026-11-13T00:00Z")],
    }
    class _MockResp:
        status_code = 200
        def json(self):
            return mock_resp

    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **kw: _MockResp())

    # 首次抓取
    rows1, diag1 = nba_schedule.fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=t_obs,
        allow_network=True,
        provider=nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD,
    )
    assert diag1["cache_hit"] is False
    assert diag1["observation_origin"] == "NETWORK"
    assert "request_started_at" in diag1
    assert len(rows1) == 1
    first_row_obs_at = rows1[0]["observed_at"]

    # 再次抓取（命中缓存）
    t_later = t_obs + timedelta(seconds=15)
    rows2, diag2 = nba_schedule.fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=t_later,
        allow_network=True,
        provider=nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD,
    )
    assert diag2["cache_hit"] is True
    assert diag2["observation_origin"] == "CACHE"
    assert diag2["observation_age_seconds"] >= 0.0
    # 行内 observed_at 保持真实历史时间，绝不被刷新时刻覆盖
    assert rows2[0]["observed_at"] == first_row_obs_at

    nba_schedule.clear_schedule_cache()


def test_partial_date_window_failure_classification(monkeypatch):
    """
    部分日期窗口抓取故障分类：
    当一个日期请求成功，另一个日期请求失败时：
    1. coverage_status 必须明确分类为 PARTIAL_DATE_FETCH
    2. 成功日期的数据正常保留，失败日期显式暴露在 failed_us_dates 中
    3. 绝不将缺失日期的网络失败当成 0 场比赛！
    """
    nba_schedule.clear_schedule_cache()
    t_obs = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)

    class _RespOK:
        status_code = 200
        def json(self):
            return _espn_payload([_espn_event(gid="3001", utc="2026-11-13T00:00Z")])

    class _RespFail:
        status_code = 500

    def _mock_get(url, *a, **kw):
        if "dates=20261112" in url:
            return _RespOK()
        return _RespFail()

    monkeypatch.setattr(nba_schedule.requests, "get", _mock_get)

    rows, diag = nba_schedule.fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=t_obs,
        allow_network=True,
        provider=nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD,
    )
    assert diag["coverage_status"] == "PARTIAL_DATE_FETCH"
    assert len(diag["successful_us_dates"]) > 0
    assert len(diag["failed_us_dates"]) > 0
    assert len(rows) == 1

    nba_schedule.clear_schedule_cache()
