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
# 4. Workstream D: NBA 赔率快照真实历史与查询语义核对
# ---------------------------------------------------------------------------

REAL_ODDS = os.path.join(config.DATA_DIR, "odds_snapshots.json")
REAL_CANONICAL = os.path.join(config.DATA_DIR, "daily_matches.json")


@pytest.mark.skipif(
    not os.path.isfile(REAL_ODDS),
    reason="no local real dataset (git-ignored); synthetic tests only in CI",
)
def test_odds_history_query_semantics_and_four_target_reconciliation():
    """
    Workstream D: 核对目标四场比赛在真实 odds_snapshots.json 中的存在性。
    - 证明 get_odds_history_for_match(match_id) 正确返回历史记录；
    - 证明之前 Phase 3-P 报告 None 是由于字典键遍历误用而非数据丢失；
    - 验证 4306~4309 每场均存在 1 条合法且不可变的赛前赔率观察。
    """
    target_ids = [
        "500j-2026-11-13-4306",
        "500j-2026-11-13-4307",
        "500j-2026-11-13-4308",
        "500j-2026-11-13-4309",
    ]

    for mid in target_ids:
        # 使用正确的按比赛 ID 查询历史 API
        history = odds_snapshots.get_odds_history_for_match(mid)
        assert len(history) == 1, f"Fixture {mid} must have exactly 1 odds snapshot"
        latest = odds_snapshots.latest_odds_snapshot(mid)
        assert latest is not None
        assert latest["match_id"] == mid
        assert latest["captured_at"] == "2026-11-12T15:05:45.652555+08:00"
        assert latest["odds_fingerprint"] is not None

        # 证明如果把 match_id 当作 snapshot_id 传入 get_odds_snapshot，会错误地返回 None
        wrong_query_result = odds_snapshots.get_odds_snapshot(mid)
        assert wrong_query_result is None, "get_odds_snapshot expects snapshot_id, not match_id"


# ---------------------------------------------------------------------------
# 5. Workstream E: NBA 时序库存严谨语义对账
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.path.isfile(REAL_CANONICAL),
    reason="no local real dataset (git-ignored); synthetic tests only in CI",
)
def test_nba_temporal_inventory_exact_reconciliation():
    """
    Workstream E: 24 场已存储待赛 NBA 赛事的五分类无缝对账。
    - STORED_UPCOMING: 24
    - VERIFIED_FUTURE_KICKOFF: 0
    - VERIFIED_PAST_KICKOFF: 2 (2026-10-28 11:00 与 2026-11-11 12:00)
    - UNKNOWN_KICKOFF_FUTURE_DATE: 0
    - UNKNOWN_KICKOFF_CURRENT_DATE: 4 (2026-11-13 的 4306~4309)
    - UNKNOWN_KICKOFF_PAST_DATE: 18 (历史 00:00 待赛场次)
    加上 14 场 live 与 15 场 finished，严格等于 53 场 NBA Canonical 总数。
    """
    import json
    dm_path = os.path.join(config.DATA_DIR, "daily_matches.json")
    with open(dm_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    matches = data.get("matches", [])
    nba = [m for m in matches if m.get("sport") == "basketball" and is_nba_league(m.get("league"))]
    assert len(nba) == 53

    live_count = sum(1 for m in nba if (m.get("status") or "").strip().lower() == "live")
    finished_count = sum(1 for m in nba if (m.get("status") or "").strip().lower() == "finished")
    upcoming = [m for m in nba if (m.get("status") or "upcoming") == "upcoming"]

    assert live_count == 14
    assert finished_count == 15
    assert len(upcoming) == 24
    assert live_count + finished_count + len(upcoming) == 53

    # 对账 24 场 upcoming 的时序五分类
    now = get_beijing_now()
    today = now.date()

    verified_future = 0
    verified_past = 0
    unknown_future_date = 0
    unknown_current_date = 0
    unknown_past_date = 0

    for m in upcoming:
        k_known = is_kickoff_time_known(m)
        k_dt = get_match_datetime(m)
        m_date = datetime.strptime(m["date"], "%Y-%m-%d").date()

        if k_known and k_dt is not None:
            if k_dt > now:
                verified_future += 1
            else:
                verified_past += 1
        else:
            if m_date > today:
                unknown_future_date += 1
            elif m_date == today:
                unknown_current_date += 1
            else:
                unknown_past_date += 1

    assert verified_future == 0
    assert verified_past == 2
    assert unknown_future_date == 0
    assert unknown_current_date == 4
    assert unknown_past_date == 18
    assert verified_future + verified_past + unknown_future_date + unknown_current_date + unknown_past_date == 24


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
