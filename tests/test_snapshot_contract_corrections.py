"""Phase 3-Q 快照捕获契约修正、赛程默认拒绝与 NBA 证据核对回归测试集。"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import os
import pytest

import config
from utils.daily_loader import BEIJING_TZ, get_beijing_now, get_match_datetime, override_beijing_now
from utils.match_identity import is_kickoff_time_known
from utils.fetcher_500 import is_nba_league
from utils import prediction_snapshots, odds_snapshots, nba_schedule


# ---------------------------------------------------------------------------
# 1. Workstream A: 显式 now 参数时序穿透修复与全场景验证
# ---------------------------------------------------------------------------


def test_explicit_now_past_actual_clock_past_kickoff_rejected(tmp_path, monkeypatch):
    """
    Workstream A.2 / A.4 场景 1:
    调用方显式传入开赛前时间 (explicit_now = 07:59:58)，
    但物理/测试时钟已过开赛时间 (actual_clock = 08:00:05)。
    四项公共持久化 API 必须 fail-closed 全部拒绝，绝不补录伪赛前快照。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    kickoff_at = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)
    explicit_now = datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ)
    actual_capture_time = datetime(2030, 5, 15, 8, 0, 5, tzinfo=BEIJING_TZ)

    match = {
        "id": "500lq-2030-05-15-test1",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "status": "upcoming",
        "kickoff_time_known": True,
        "home_team": {"name": "湖人"},
        "away_team": {"name": "勇士"},
        "odds": {"sf": {"home_win": 1.85, "away_win": 1.95}},
    }

    with override_beijing_now(actual_capture_time):
        # 1. capture_snapshot
        res_capture = prediction_snapshots.capture_snapshot(dict(match), now=explicit_now)
        assert res_capture.snapshot is None
        assert res_capture.created is False
        assert res_capture.classification == prediction_snapshots.KICKOFF_REACHED_BEFORE_CAPTURE

        # 2. create_snapshot
        match2 = dict(match, id="500lq-2030-05-15-test2")
        res_create = prediction_snapshots.create_snapshot(dict(match2), now=explicit_now)
        assert res_create is None

        # 3. ensure_snapshot
        match3 = dict(match, id="500lq-2030-05-15-test3")
        res_ensure = prediction_snapshots.ensure_snapshot(dict(match3), now=explicit_now)
        assert res_ensure is None

        # 4. record_odds_snapshot
        match4 = dict(match, id="500lq-2030-05-15-test4")
        res_odds = odds_snapshots.record_odds_snapshot(dict(match4), now=explicit_now)
        assert res_odds is None


def test_explicit_now_model_crosses_kickoff_rejected(tmp_path, monkeypatch):
    """
    Workstream A.4 场景 2:
    调用方传入开赛前显式时间，决策时时钟亦早于开赛，
    但在耗时模型计算期间时钟越过开赛时间。
    预测快照必须被废弃并返回 KICKOFF_REACHED_DURING_MODEL_EVALUATION。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    explicit_now = datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ)

    clock_ticks = [
        datetime(2030, 5, 15, 7, 59, 59, tzinfo=BEIJING_TZ),  # decision time
        datetime(2030, 5, 15, 8, 0, 2, tzinfo=BEIJING_TZ),    # post-model calculation time
        datetime(2030, 5, 15, 8, 0, 3, tzinfo=BEIJING_TZ),    # post-lock time
    ]

    def mock_clock():
        return clock_ticks.pop(0) if clock_ticks else datetime(2030, 5, 15, 8, 0, 5, tzinfo=BEIJING_TZ)

    match = {
        "id": "500lq-2030-05-15-test-model-cross",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "status": "upcoming",
        "kickoff_time_known": True,
        "home_team": {"name": "湖人"},
        "away_team": {"name": "勇士"},
        "odds": {"sf": {"home_win": 1.85, "away_win": 1.95}},
    }

    with override_beijing_now(mock_clock):
        res = prediction_snapshots.capture_snapshot(match, now=explicit_now)
        assert res.snapshot is None
        assert res.created is False
        assert res.classification == prediction_snapshots.KICKOFF_REACHED_DURING_MODEL_EVALUATION


def test_explicit_now_lock_wait_crosses_kickoff_rejected(tmp_path, monkeypatch):
    """
    Workstream A.4 场景 3:
    决策时与模型计算时均在开赛前，但排队等待文件锁期间时钟越过开赛点。
    预测快照返回 KICKOFF_REACHED_DURING_LOCK_WAIT，赔率快照返回 None。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    explicit_now = datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ)

    pred_ticks = [
        datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ),  # decision time
        datetime(2030, 5, 15, 7, 59, 59, tzinfo=BEIJING_TZ),  # post-model time
        datetime(2030, 5, 15, 8, 0, 1, tzinfo=BEIJING_TZ),    # post-lock time
    ]

    def pred_mock_clock():
        return pred_ticks.pop(0) if pred_ticks else datetime(2030, 5, 15, 8, 0, 5, tzinfo=BEIJING_TZ)

    match = {
        "id": "500lq-2030-05-15-test-lock-cross",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "status": "upcoming",
        "kickoff_time_known": True,
        "home_team": {"name": "湖人"},
        "away_team": {"name": "勇士"},
        "odds": {"sf": {"home_win": 1.85, "away_win": 1.95}},
    }

    with override_beijing_now(pred_mock_clock):
        res = prediction_snapshots.capture_snapshot(match, now=explicit_now)
        assert res.snapshot is None
        assert res.created is False
        assert res.classification == prediction_snapshots.KICKOFF_REACHED_DURING_LOCK_WAIT

    odds_ticks = [
        datetime(2030, 5, 15, 7, 59, 58, tzinfo=BEIJING_TZ),  # decision time
        datetime(2030, 5, 15, 8, 0, 1, tzinfo=BEIJING_TZ),    # post-lock time
    ]

    def odds_mock_clock():
        return odds_ticks.pop(0) if odds_ticks else datetime(2030, 5, 15, 8, 0, 5, tzinfo=BEIJING_TZ)

    with override_beijing_now(odds_mock_clock):
        odds_res = odds_snapshots.record_odds_snapshot(dict(match, id="500lq-2030-05-15-odds-lock"), now=explicit_now)
        assert odds_res is None


def test_future_date_simulation_and_exact_equality(tmp_path, monkeypatch):
    """
    Workstream A.4 场景 4, 5, 6, 7, 8, 9, 10:
    - 场景 4/5: 合法离线未来模拟与严格赛前真实物理时钟捕获；
    - 场景 6: 精确等于开赛时间 (equality) 必须拒绝；
    - 场景 7: 既有不可变快照幂等返回 EXISTING_IMMUTABLE_SNAPSHOT；
    - 场景 8: 连续相同赔率/重放安全去重；
    - 场景 9: 开赛时间未知拒绝预测快照 (UNKNOWN_KICKOFF)；
    - 场景 10: 状态已为 live / finished 拒绝捕获。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    t_valid = datetime(2030, 5, 15, 7, 45, 0, tzinfo=BEIJING_TZ)
    kickoff = datetime(2030, 5, 15, 8, 0, 0, tzinfo=BEIJING_TZ)

    match = {
        "id": "500lq-2030-05-15-test-sim",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "status": "upcoming",
        "kickoff_time_known": True,
        "home_team": {"name": "湖人"},
        "away_team": {"name": "勇士"},
        "odds": {"sf": {"home_win": 1.85, "away_win": 1.95}},
    }

    with override_beijing_now(t_valid):
        # 场景 4/5: 正常赛前捕获成功
        snap, created = prediction_snapshots.capture_snapshot(match)
        assert snap is not None
        assert created is True

        odds_snap = odds_snapshots.record_odds_snapshot(match)
        assert isinstance(odds_snap, dict)

        # 场景 7: 既有不可变预测快照幂等返回
        snap_again, created_again = prediction_snapshots.capture_snapshot(match)
        assert snap_again is not None
        assert created_again is False

        # 场景 8: 赔率连续重复返回 None
        odds_dup = odds_snapshots.record_odds_snapshot(match)
        assert odds_dup is None

        # 场景 6: 精确等于开赛时间拒绝
        m_exact = dict(match, id="500lq-2030-05-15-exact")
        res_exact = prediction_snapshots.capture_snapshot(m_exact, now=kickoff)
        assert res_exact.snapshot is None and res_exact.created is False
        assert odds_snapshots.record_odds_snapshot(m_exact, now=kickoff) is None

        # 场景 9: 开赛时间未知
        m_unknown = dict(match, id="500j-2030-05-15-unk", time="00:00", kickoff_time_known=False)
        res_unk = prediction_snapshots.capture_snapshot(m_unknown)
        assert res_unk.classification == prediction_snapshots.UNKNOWN_KICKOFF

        # 场景 10: 源状态已为 live / finished
        m_live = dict(match, id="500lq-2030-05-15-live", status="live")
        assert prediction_snapshots.capture_snapshot(m_live).classification == prediction_snapshots.SOURCE_ALREADY_LIVE
        assert odds_snapshots.record_odds_snapshot(m_live) is None

        m_fin = dict(match, id="500lq-2030-05-15-fin", status="finished")
        assert prediction_snapshots.capture_snapshot(m_fin).classification == prediction_snapshots.SOURCE_ALREADY_FINISHED
        assert odds_snapshots.record_odds_snapshot(m_fin) is None


# ---------------------------------------------------------------------------
# 2. Workstream B: 快照捕获契约修正与二元解构兼容
# ---------------------------------------------------------------------------


def test_capture_result_contract_two_element_tuple():
    """
    Workstream B: CaptureResult 契约验证。
    - 严格为二元元组 (len == 2)；
    - 保证 snapshot, created = result 二元解构；
    - 下标访问仅支持 [0] 与 [1]；
    - result[2] 必须抛出 IndexError（不可伪称支持 [2]）；
    - 状态码通过属性 .classification / .reason / .status 暴露。
    """
    dummy_snap = {"snapshot_id": "test_sid", "match_id": "m1"}
    res = prediction_snapshots.CaptureResult(
        dummy_snap, True, prediction_snapshots.CAPTURED_BEFORE_KICKOFF
    )

    # 1. 长度与解构
    assert len(res) == 2
    snap, created = res
    assert snap == dummy_snap
    assert created is True

    # 2. 下标索引
    assert res[0] == dummy_snap
    assert res[1] is True
    with pytest.raises(IndexError):
        _ = res[2]

    # 3. 属性访问
    assert res.snapshot == dummy_snap
    assert res.created is True
    assert res.classification == prediction_snapshots.CAPTURED_BEFORE_KICKOFF
    assert res.reason == prediction_snapshots.CAPTURED_BEFORE_KICKOFF
    assert res.status == prediction_snapshots.CAPTURED_BEFORE_KICKOFF


def test_odds_snapshot_contract_dict_or_none(tmp_path, monkeypatch):
    """
    Workstream B: record_odds_snapshot 返回契约验证。
    - 成功落盘时返回完整 dict；
    - 被拒绝或重复时返回 None；
    - 绝对不返回 (None, False) 之类的元组。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    t_valid = datetime(2030, 5, 15, 7, 0, 0, tzinfo=BEIJING_TZ)

    match = {
        "id": "500lq-2030-05-15-odds-contract",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "08:00",
        "status": "upcoming",
        "kickoff_time_known": True,
        "home_team": {"name": "湖人"},
        "away_team": {"name": "勇士"},
        "odds": {"sf": {"home_win": 1.85, "away_win": 1.95}},
    }

    with override_beijing_now(t_valid):
        # 成功时返回 dict
        res_ok = odds_snapshots.record_odds_snapshot(match)
        assert isinstance(res_ok, dict)
        assert not isinstance(res_ok, tuple)

        # 重复时返回 None
        res_dup = odds_snapshots.record_odds_snapshot(match)
        assert res_dup is None
        assert not isinstance(res_dup, tuple)

        # 拒绝时返回 None
        match_live = dict(match, id="500lq-2030-05-15-odds-live", status="live")
        res_rej = odds_snapshots.record_odds_snapshot(match_live)
        assert res_rej is None
        assert not isinstance(res_rej, tuple)


# ---------------------------------------------------------------------------
# 3. Workstream C: 赛程网络抓取公共入口 Default-Deny
# ---------------------------------------------------------------------------


def test_schedule_fetch_default_deny_when_omitted(monkeypatch):
    """
    Workstream C: fetch_nba_schedule_observations 公共入口默认拒绝。
    - 当缺省 allow_network 参数时，默认行为必须是拒绝 (allow_network=False)；
    - 严禁调用底层 HTTP 网络客户端；
    - 返回 status == 'disabled_pending_authorization' 及明确错误码。
    """
    nba_schedule.clear_schedule_cache()
    http_called = False

    def mock_get(*args, **kwargs):
        nonlocal http_called
        http_called = True
        raise AssertionError("HTTP client must NOT be invoked when network permission is omitted!")

    monkeypatch.setattr(nba_schedule.requests, "get", mock_get)

    # 省略 allow_network 参数直接调用
    rows, diag = nba_schedule.fetch_nba_schedule_observations(["2026-11-13"])

    assert rows == []
    assert http_called is False
    assert diag["status"] == "disabled_pending_authorization"
    assert diag["automated_provider_operation"] == nba_schedule.AUTOMATED_OPERATION_DISABLED
    assert "PROVIDER_AUTOMATED_USE_UNAUTHORIZED" in diag["errors"]


def test_schedule_fetch_explicit_enable_and_mock_injection(monkeypatch):
    """
    Workstream C / Workstream G:
    当显式传入 allow_network=True 时，正常消费注入的测试夹具，
    并严格保持 NBA / WNBA 隔离契约。
    """
    nba_schedule.clear_schedule_cache()

    mock_payload = {
        "leagues": [{
            "id": "46",
            "uid": "s:40~l:46",
            "name": "National Basketball Association",
            "abbreviation": "NBA",
            "slug": "nba",
            "season": {"year": 2027, "displayName": "2026-27"},
        }],
        "events": [
            {
                "id": "401704999",
                "uid": "s:40~l:46~e:401704999",
                "date": "2030-05-15T00:00Z",
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
                "competitions": [{
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "13", "displayName": "Los Angeles Lakers", "abbreviation": "LAL"}},
                        {"homeAway": "away", "team": {"id": "9", "displayName": "Golden State Warriors", "abbreviation": "GSW"}},
                    ]
                }],
            },
            # WNBA 赛事应被严格过滤
            {
                "id": "401705888",
                "uid": "s:40~l:59~e:401705888",
                "date": "2030-05-15T00:00Z",
                "status": {"type": {"name": "STATUS_SCHEDULED"}},
                "competitions": [{
                    "competitors": [
                        {"homeAway": "home", "team": {"id": "1", "displayName": "New York Liberty", "abbreviation": "NYL"}},
                        {"homeAway": "away", "team": {"id": "2", "displayName": "Las Vegas Aces", "abbreviation": "LVA"}},
                    ]
                }],
            }
        ]
    }

    class MockResponse:
        status_code = 200
        def json(self):
            return mock_payload

    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **kw: MockResponse())

    t_now = datetime(2030, 5, 14, 12, 0, tzinfo=BEIJING_TZ)
    rows, diag = nba_schedule.fetch_nba_schedule_observations(
        ["2030-05-15"], now=t_now, allow_network=True
    )

    assert len(rows) == 1
    assert rows[0]["provider_game_id"] == "401704999"
    assert rows[0]["home_tricode"] == "LAL"
    assert rows[0]["away_tricode"] == "GSW"
    assert diag["status"] == "ok"


# ---------------------------------------------------------------------------
# 4. Workstream D: 赔率快照查询语义与历史查询契约（确定性合成测试）
# ---------------------------------------------------------------------------


def test_odds_history_query_semantics_synthetic(tmp_path, monkeypatch):
    """
    Workstream D / Phase 3-Q-R: 使用隔离合成数据集验证赔率快照查询语义。
    - 证明 get_odds_history_for_match(match_id) 正确返回按 captured_at 排序的历史列表；
    - 证明 latest_odds_snapshot(match_id) 正确返回最新一条记录；
    - 证明 get_odds_snapshot(key) 期望 snapshot_id，若误传 match_id 返回 None；
    - 证明直接对 store['snapshots']（字典类型）做遍历时获取的是键而非值，
      解释了此前 Phase 3-P 测量脚本报告 None 的原因。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    odds_snapshots._SNAPSHOT_STORE_CACHE = None

    mid = "test-nba-2030-05-15-4306"
    snap1 = {
        "snapshot_id": "snap-id-001",
        "match_id": mid,
        "captured_at": "2030-05-14T10:00:00+08:00",
        "odds_fingerprint": "fp001",
        "odds": {"home_win": 1.5, "away_win": 2.5},
    }
    snap2 = {
        "snapshot_id": "snap-id-002",
        "match_id": mid,
        "captured_at": "2030-05-14T15:00:00+08:00",
        "odds_fingerprint": "fp002",
        "odds": {"home_win": 1.6, "away_win": 2.4},
    }
    snap_other = {
        "snapshot_id": "snap-id-003",
        "match_id": "test-nba-other",
        "captured_at": "2030-05-14T12:00:00+08:00",
        "odds_fingerprint": "fp003",
        "odds": {"home_win": 1.8, "away_win": 2.0},
    }

    initial_store = {
        "schema_version": "1.0.0",
        "snapshots": {
            "snap-id-001": snap1,
            "snap-id-002": snap2,
            "snap-id-003": snap_other,
        },
    }
    odds_snapshots._save_store(initial_store)

    # 1. 查询该比赛历史
    history = odds_snapshots.get_odds_history_for_match(mid)
    assert len(history) == 2
    assert history[0]["snapshot_id"] == "snap-id-001"
    assert history[1]["snapshot_id"] == "snap-id-002"

    # 2. 查询最新一条
    latest = odds_snapshots.latest_odds_snapshot(mid)
    assert latest is not None
    assert latest["snapshot_id"] == "snap-id-002"

    # 3. get_odds_snapshot 区分 snapshot_id 与 match_id
    assert odds_snapshots.get_odds_snapshot("snap-id-001") is not None
    assert odds_snapshots.get_odds_snapshot(mid) is None

    # 4. 复现 Phase 3-P 的遍历缺陷：若误用 for s in store['snapshots'] (迭代出的是 str)
    raw_store = odds_snapshots._load_store()
    wrongly_filtered = [s for s in raw_store.get("snapshots", []) if isinstance(s, dict)]
    assert len(wrongly_filtered) == 0  # 证明由于 s 是 str，全部被过滤为 0


# ---------------------------------------------------------------------------
# 5. Workstream E: NBA 时序库存严谨分类算法（确定性合成测试）
# ---------------------------------------------------------------------------


def test_nba_temporal_inventory_partitioning_synthetic(tmp_path, monkeypatch):
    """
    Workstream E / Phase 3-Q-R: 使用隔离合成比赛验证时序库存五分类算法。
    - 绝不依赖 CI 环境中不存在的私有真实文件；
    - 验证 STORED_LIVE / STORED_FINISHED / STORED_UPCOMING 严格互斥；
    - 验证 STORED_UPCOMING 五分类在任何时钟下的完备性与无缝性。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

    fixed_clock = datetime(2030, 5, 15, 12, 0, tzinfo=BEIJING_TZ)
    today = fixed_clock.date()

    synthetic_matches = [
        # live
        {"id": "m-live-1", "sport": "basketball", "league": "NBA", "status": "live", "date": "2030-05-15", "time": "11:00"},
        # finished
        {"id": "m-fin-1", "sport": "basketball", "league": "NBA", "status": "finished", "date": "2030-05-15", "time": "09:00"},
        # upcoming - verified future kickoff (14:00 > 12:00)
        {"id": "m-up-v-future", "sport": "basketball", "league": "NBA", "status": "upcoming", "date": "2030-05-15", "time": "14:00", "kickoff_time_known": True},
        # upcoming - verified past kickoff (10:00 <= 12:00)
        {"id": "m-up-v-past", "sport": "basketball", "league": "NBA", "status": "upcoming", "date": "2030-05-15", "time": "10:00", "kickoff_time_known": True},
        # upcoming - unknown kickoff, future date (2030-05-16 > 2030-05-15)
        {"id": "m-up-u-future", "sport": "basketball", "league": "NBA", "status": "upcoming", "date": "2030-05-16", "time": "00:00", "kickoff_time_known": False},
        # upcoming - unknown kickoff, current date (2030-05-15 == 2030-05-15)
        {"id": "m-up-u-curr", "sport": "basketball", "league": "NBA", "status": "upcoming", "date": "2030-05-15", "time": "00:00", "kickoff_time_known": False},
        # upcoming - unknown kickoff, past date (2030-05-14 < 2030-05-15)
        {"id": "m-up-u-past", "sport": "basketball", "league": "NBA", "status": "upcoming", "date": "2030-05-14", "time": "00:00", "kickoff_time_known": False},
    ]

    with override_beijing_now(fixed_clock):
        now = get_beijing_now()
        today_eval = now.date()
        assert today_eval == today

        nba = [m for m in synthetic_matches if m.get("sport") == "basketball" and is_nba_league(m.get("league"))]
        assert len(nba) == 7

        live_count = sum(1 for m in nba if (m.get("status") or "").strip().lower() == "live")
        finished_count = sum(1 for m in nba if (m.get("status") or "").strip().lower() == "finished")
        upcoming = [m for m in nba if (m.get("status") or "upcoming") == "upcoming"]

        assert live_count == 1
        assert finished_count == 1
        assert len(upcoming) == 5
        assert live_count + finished_count + len(upcoming) == len(nba)

        v_future = 0
        v_past = 0
        u_future_date = 0
        u_current_date = 0
        u_past_date = 0

        for m in upcoming:
            k_known = is_kickoff_time_known(m)
            k_dt = get_match_datetime(m)
            m_date = datetime.strptime(m["date"], "%Y-%m-%d").date()

            if k_known and k_dt is not None:
                if k_dt > now:
                    v_future += 1
                else:
                    v_past += 1
            else:
                if m_date > today_eval:
                    u_future_date += 1
                elif m_date == today_eval:
                    u_current_date += 1
                else:
                    u_past_date += 1

        assert v_future == 1
        assert v_past == 1
        assert u_future_date == 1
        assert u_current_date == 1
        assert u_past_date == 1
        assert v_future + v_past + u_future_date + u_current_date + u_past_date == len(upcoming)


# ---------------------------------------------------------------------------
# 6. Workstream F: 进程内内存 TTL 缓存契约与时间戳保真
# ---------------------------------------------------------------------------


def test_in_memory_cache_contract_and_age_preservation(monkeypatch):
    """
    Workstream F: 验证 nba_schedule 使用进程内内存 TTL 缓存（非磁盘缓存），
    且缓存命中时不篡改 observed_at 抓取时间戳，仅计算 observation_age_seconds。
    """
    nba_schedule.clear_schedule_cache()

    mock_payload = {
        "leagues": [{
            "id": "46",
            "uid": "s:40~l:46",
            "abbreviation": "NBA",
            "slug": "nba",
            "season": {"year": 2027},
        }],
        "events": [{
            "id": "401704111",
            "uid": "s:40~l:46~e:401704111",
            "date": "2030-05-15T00:00Z",
            "status": {"type": {"name": "STATUS_SCHEDULED"}},
            "competitions": [{
                "competitors": [
                    {"homeAway": "home", "team": {"id": "1", "displayName": "Atlanta Hawks", "abbreviation": "ATL"}},
                    {"homeAway": "away", "team": {"id": "2", "displayName": "Boston Celtics", "abbreviation": "BOS"}},
                ]
            }],
        }]
    }

    call_count = 0

    class MockResponse:
        status_code = 200
        def json(self):
            return mock_payload

    def counting_get(*a, **kw):
        nonlocal call_count
        call_count += 1
        return MockResponse()

    monkeypatch.setattr(nba_schedule.requests, "get", counting_get)

    t_obs1 = datetime(2030, 5, 14, 10, 0, tzinfo=BEIJING_TZ)
    t_obs2 = datetime(2030, 5, 14, 10, 2, tzinfo=BEIJING_TZ)

    # 首次抓取：填充缓存
    rows1, diag1 = nba_schedule.fetch_nba_schedule_observations(["2030-05-15"], now=t_obs1, allow_network=True)
    assert call_count == 2  # queried yesterday + today
    assert diag1.get("cache_hit") is not True
    orig_observed_at = rows1[0]["observed_at"]

    # 二次抓取（TTL 300秒内）：命中进程内内存缓存
    rows2, diag2 = nba_schedule.fetch_nba_schedule_observations(["2030-05-15"], now=t_obs2, allow_network=True)
    assert call_count == 2  # 未新增 HTTP 请求
    assert diag2["cache_hit"] is True
    assert diag2["observation_origin"] == "CACHE"
    assert "observation_age_seconds" in diag2
    # 绝不为缓存行伪造新的 observed_at
    assert rows2[0]["observed_at"] == orig_observed_at


# ---------------------------------------------------------------------------
# 7. Workstream C: 快照证据时间戳真实性与防回拨/伪造测试
# ---------------------------------------------------------------------------


def test_prediction_generated_at_records_truthful_actual_clock(tmp_path, monkeypatch):
    """
    Workstream C / Phase 3-Q-R: 验证新生成的预测快照 generated_at 必须忠实记录
    实际物理/测试捕获时钟，严禁被调用方传入的过去或未来逻辑时间 now 伪造或回拨。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    prediction_snapshots._SNAPSHOT_STORE_CACHE = None

    actual_clock = datetime(2030, 5, 15, 9, 55, tzinfo=BEIJING_TZ)
    past_caller_now = datetime(2030, 5, 15, 9, 0, tzinfo=BEIJING_TZ)
    future_caller_now = datetime(2030, 5, 15, 9, 58, tzinfo=BEIJING_TZ)

    match1 = {
        "id": "500lq-2030-05-15-truthful-pred-1",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "10:00",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "status": "upcoming",
        "kickoff_time_known": True,
        "odds": {"home_win": 1.75, "away_win": 2.10},
    }

    match2 = {
        "id": "500lq-2030-05-15-truthful-pred-2",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "10:00",
        "home": "波士顿凯尔特人",
        "away": "迈阿密热火",
        "status": "upcoming",
        "kickoff_time_known": True,
        "odds": {"home_win": 1.50, "away_win": 2.60},
    }

    with override_beijing_now(actual_clock):
        # 1. 显式传入过去逻辑时间
        res1 = prediction_snapshots.capture_snapshot(dict(match1), now=past_caller_now)
        snap1, created1 = res1
        assert created1 is True
        assert snap1 is not None
        assert snap1["generated_at"] == actual_clock.isoformat()
        assert snap1["generated_at"] != past_caller_now.isoformat()

        # 2. 显式传入未来逻辑时间
        res2 = prediction_snapshots.capture_snapshot(dict(match2), now=future_caller_now)
        snap2, created2 = res2
        assert created2 is True
        assert snap2 is not None
        assert snap2["generated_at"] == actual_clock.isoformat()
        assert snap2["generated_at"] != future_caller_now.isoformat()


def test_odds_captured_at_records_truthful_actual_clock(tmp_path, monkeypatch):
    """
    Workstream C / Phase 3-Q-R: 验证新生成的赔率快照 captured_at 必须忠实记录
    实际物理/测试捕获时钟，严禁被调用方传入的 past/future now 伪造或回拨。
    同时验证重复观察幂等返回 None。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    odds_snapshots._SNAPSHOT_STORE_CACHE = None

    actual_clock = datetime(2030, 5, 15, 9, 55, tzinfo=BEIJING_TZ)
    past_caller_now = datetime(2030, 5, 15, 9, 0, tzinfo=BEIJING_TZ)
    future_caller_now = datetime(2030, 5, 15, 9, 58, tzinfo=BEIJING_TZ)

    match1 = {
        "id": "500lq-2030-05-15-truthful-odds-1",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "10:00",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "status": "upcoming",
        "kickoff_time_known": True,
        "odds": {"home_win": 1.75, "away_win": 2.10},
    }

    match2 = {
        "id": "500lq-2030-05-15-truthful-odds-2",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "10:00",
        "home": "波士顿凯尔特人",
        "away": "迈阿密热火",
        "status": "upcoming",
        "kickoff_time_known": True,
        "odds": {"home_win": 1.50, "away_win": 2.60},
    }

    with override_beijing_now(actual_clock):
        # 1. 显式传入过去逻辑时间
        res1 = odds_snapshots.record_odds_snapshot(dict(match1), now=past_caller_now)
        assert res1 is not None
        assert isinstance(res1, dict)
        assert res1["captured_at"] == actual_clock.isoformat()
        assert res1["captured_at"] != past_caller_now.isoformat()

        # 重复记录（未变更赔率）幂等返回 None
        repeat = odds_snapshots.record_odds_snapshot(dict(match1), now=past_caller_now)
        assert repeat is None

        # 2. 显式传入未来逻辑时间
        res2 = odds_snapshots.record_odds_snapshot(dict(match2), now=future_caller_now)
        assert res2 is not None
        assert isinstance(res2, dict)
        assert res2["captured_at"] == actual_clock.isoformat()
        assert res2["captured_at"] != future_caller_now.isoformat()
