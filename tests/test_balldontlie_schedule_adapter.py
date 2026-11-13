"""
Phase 3-R: BALLDONTLIE NBA 赛程适配器、30 支球队显式映射、开赛时间补全与赛前预测闭环测试。
涵盖响应解析、状态映射、时区转换、严格绑定、Default-Deny 守卫、凭据边界、速率限制与隔离端到端流程。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

import config
from utils import daily_loader, match_identity, nba_schedule, odds_snapshots, prediction_snapshots
from utils.daily_loader import BEIJING_TZ
from utils.nba_schedule import (
    CHANGE_FIRST_VERIFIED_KICKOFF,
    CHANGE_OFFICIAL_RESCHEDULE_CONFIRMED,
    CHANGE_POST_START_UPDATE_REJECTED,
    CHANGE_SCHEDULED_TIME_UNCHANGED,
    ENV_BDL_API_KEY,
    NBA_FRANCHISE_MAPPINGS,
    PROVIDER_BALLDONTLIE_NBA_GAMES,
    PROVIDER_ESPN_NBA_SCOREBOARD,
    PROVIDER_NBA_OFFICIAL_SCHEDULE,
    REASON_BEIJING_EVENT_DATE_MISMATCH,
    REASON_GAME_ALREADY_STARTED,
    REASON_HOME_AWAY_ORIENTATION_CONFLICT,
    REASON_MULTIPLE_OFFICIAL_CANDIDATES,
    REASON_OFFICIAL_GAME_NOT_FOUND,
    REASON_OFFICIAL_GAME_NOT_SCHEDULED,
    REASON_OFFICIAL_GAME_TIME_INVALID,
    REASON_OFFICIAL_GAME_TIME_MISSING,
    REASON_TEAM_ID_MAPPING_UNVERIFIED,
    apply_nba_schedule_time_enrichment,
    bind_schedule_observations_to_canonical,
    clear_schedule_cache,
    convert_utc_to_beijing,
    fetch_nba_schedule_observations,
    parse_balldontlie_nba_games,
    resolve_canonical_team_code,
    resolve_provider_team_code,
)


@pytest.fixture(autouse=True)
def deterministic_provider_pacing(monkeypatch):
    """Offline requests exercise pacing without physical multi-second sleeps."""
    clock = [0.0]
    monkeypatch.setattr(nba_schedule.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(nba_schedule.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(nba_schedule, "_LAST_BDL_REQUEST_MONO", None)
    monkeypatch.setattr(nba_schedule, "_BDL_RETRY_NOT_BEFORE", 0.0)
    clear_schedule_cache()
    yield
    clear_schedule_cache()


def _bdl_payload(games: list[dict], next_cursor: int | None = None, per_page: int = 25) -> dict:
    return {
        "data": games,
        "meta": {"next_cursor": next_cursor, "per_page": per_page},
    }


def _bdl_game(
    gid: int = 1053164,
    utc: str = "2026-11-13T00:30:00.000Z",
    status_state: str = "scheduled",
    status: str = "7:30 PM ET",
    postponed: bool = False,
    home_id: int = 23,
    home_abbr: str = "PHI",
    home_name: str = "76ers",
    home_full: str = "Philadelphia 76ers",
    visitor_id: int = 28,
    visitor_abbr: str = "TOR",
    visitor_name: str = "Raptors",
    visitor_full: str = "Toronto Raptors",
    season: int = 2026,
) -> dict:
    return {
        "id": gid,
        "date": utc[:10] if utc else "",
        "season": season,
        "status": status,
        "period": 0,
        "time": "",
        "postponed": postponed,
        "postseason": False,
        "home_team_score": 0,
        "visitor_team_score": 0,
        "datetime": utc,
        "status_state": status_state,
        "home_team": {
            "id": home_id,
            "conference": "East",
            "division": "Atlantic",
            "city": home_full.rsplit(" ", 1)[0] if " " in home_full else home_full,
            "name": home_name,
            "full_name": home_full,
            "abbreviation": home_abbr,
        },
        "visitor_team": {
            "id": visitor_id,
            "conference": "East",
            "division": "Atlantic",
            "city": visitor_full.rsplit(" ", 1)[0] if " " in visitor_full else visitor_full,
            "name": visitor_name,
            "full_name": visitor_full,
            "abbreviation": visitor_abbr,
        },
    }


# ---------------------------------------------------------------------------
# 1. BALLDONTLIE 载荷解析与字段归一化 (Workstream B)
# ---------------------------------------------------------------------------


def test_balldontlie_payload_parsing_valid():
    """解析标准合法 BALLDONTLIE 载荷，验证字段提取与时区转换。"""
    game = _bdl_game(
        gid=1053164,
        utc="2026-11-13T00:30:00.000Z",
        status_state="scheduled",
        home_id=23,
        home_abbr="PHI",
        visitor_id=28,
        visitor_abbr="TOR",
    )
    obs_time = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    rows, errs = parse_balldontlie_nba_games(_bdl_payload([game]), observed_at=obs_time)

    assert errs == []
    assert len(rows) == 1
    r = rows[0]
    assert r["provider"] == PROVIDER_BALLDONTLIE_NBA_GAMES
    assert r["provider_game_id"] == "1053164"
    assert r["league_uid"] == "balldontlie_nba"
    assert r["game_status"] == "scheduled"
    assert r["home_team_identifier"] == "bdl:23"
    assert r["away_team_identifier"] == "bdl:28"
    assert r["home_tricode"] == "PHI"
    assert r["away_tricode"] == "TOR"
    assert r["scheduled_start_utc"] == "2026-11-13T00:30:00.000Z"
    assert r["beijing_date"] == "2026-11-13"
    assert r["beijing_time"] == "08:30"
    assert r["time_parse_status"] == "VALID_UTC"
    assert r["observed_at"] == obs_time.isoformat()


def test_balldontlie_status_mapping_matrix():
    """验证 BALLDONTLIE 各种生命周期状态的规范映射。"""
    cases = [
        ("scheduled", False, "7:00 PM ET", "scheduled"),
        ("in_progress", False, "Q2 5:20", "live"),
        ("halftime", False, "Halftime", "unknown"),
        ("final", False, "Final", "final"),
        ("completed", False, "Final/OT", "unknown"),
        ("postponed", False, "PPD", "postponed"),
        ("scheduled", True, "Postponed", "postponed"),
        ("canceled", False, "Canceled", "canceled"),
        ("cancelled", False, "Cancelled", "canceled"),
        ("unknown_state", False, "TBD", "unknown"),
    ]
    for state, post_flag, text, expected in cases:
        g = _bdl_game(status_state=state, postponed=post_flag, status=text)
        rows, _ = parse_balldontlie_nba_games(_bdl_payload([g]))
        assert rows[0]["game_status"] == expected, f"Failed for {state}, {post_flag}, {text}"


def test_balldontlie_utc_conversion_and_invalid_handling():
    """验证 UTC 时间戳转换、无效格式以及缺失值的容错判定。"""
    # 正常 UTC 带毫秒与 Z
    d, t, dt, stat = convert_utc_to_beijing("2026-11-13T00:00:00.000Z")
    assert stat == "VALID_UTC"
    assert d == "2026-11-13"
    assert t == "08:00"

    # 跨日转换：UTC 2026-11-12 23:00 -> 北京 2026-11-13 07:00
    d, t, _, stat = convert_utc_to_beijing("2026-11-12T23:00:00Z")
    assert stat == "VALID_UTC"
    assert d == "2026-11-13"
    assert t == "07:00"

    # 缺失 UTC
    g_missing = _bdl_game(utc="")
    rows_m, _ = parse_balldontlie_nba_games(_bdl_payload([g_missing]))
    assert rows_m[0]["time_parse_status"] == "MISSING_UTC"
    assert rows_m[0]["beijing_date"] is None
    assert rows_m[0]["beijing_time"] is None

    # 无时区标识（非法 UTC）
    g_inv = _bdl_game(utc="2026-11-13T00:00:00")
    rows_inv, _ = parse_balldontlie_nba_games(_bdl_payload([g_inv]))
    assert rows_inv[0]["time_parse_status"] == "INVALID_UTC"


def test_balldontlie_payload_anomalies_and_duplicates():
    """验证畸变载荷、缺失字段与重复场次冲突检测。"""
    # 载荷非 dict 或无 data 数组
    rows, errs = parse_balldontlie_nba_games({"invalid": True})
    assert rows == []
    assert errs == ["MALFORMED_BALLDONTLIE_PAYLOAD"]

    # 缺失 game id
    bad_g1 = {"status_state": "scheduled", "home_team": {"id": 1}, "visitor_team": {"id": 2}}
    rows, errs = parse_balldontlie_nba_games(_bdl_payload([bad_g1]))
    assert "MISSING_GAME_ID" in errs

    # 缺失球队信息
    bad_g2 = {"id": 12345, "status_state": "scheduled"}
    rows, errs = parse_balldontlie_nba_games(_bdl_payload([bad_g2]))
    assert "MISSING_COMPETITORS:12345" in errs

    # 相同 ID 但对阵冲突的重复项
    g_a = _bdl_game(gid=999, home_id=1, visitor_id=2)
    g_b = _bdl_game(gid=999, home_id=3, visitor_id=4)
    rows_dup, errs_dup = parse_balldontlie_nba_games(_bdl_payload([g_a, g_b]))
    assert len(rows_dup) == 0
    assert "CONFLICTING_DUPLICATE_EVENT:999" in errs_dup

    # 相同 ID 且内容幂等的重复项
    rows_idem, errs_idem = parse_balldontlie_nba_games(_bdl_payload([g_a, g_a]))
    assert len(rows_idem) == 1
    assert errs_idem == []


# ---------------------------------------------------------------------------
# 2. 30 支 NBA 球队显式映射与标识符隔离 (Workstream C)
# ---------------------------------------------------------------------------


def test_balldontlie_30_franchises_strict_mapping():
    """验证全部 30 支 NBA 球队 BALLDONTLIE ID (1..30) 与规范代码的唯一精确映射。"""
    assert len(NBA_FRANCHISE_MAPPINGS) == 30

    expected_codes = [
        "ATL", "BOS", "BKN", "CHA", "CHI", "CLE", "DAL", "DEN", "DET", "GSW",
        "HOU", "IND", "LAC", "LAL", "MEM", "MIA", "MIL", "MIN", "NOP", "NYK",
        "OKC", "ORL", "PHI", "PHX", "POR", "SAC", "SAS", "TOR", "UTA", "WAS",
    ]

    for i in range(1, 31):
        bdl_id = str(i)
        code = resolve_provider_team_code(f"bdl:{bdl_id}")
        assert code is not None, f"BALLDONTLIE ID {bdl_id} failed to resolve"
        assert code == expected_codes[i - 1], f"Expected {expected_codes[i - 1]} for ID {bdl_id}, got {code}"
        # balldontlie: 前缀同样兼容
        assert resolve_provider_team_code(f"balldontlie:{bdl_id}") == code

    # 边界非法值
    assert resolve_provider_team_code("bdl:0") is None
    assert resolve_provider_team_code("bdl:31") is None
    assert resolve_provider_team_code("bdl:999") is None
    assert resolve_provider_team_code("bdl:") is None
    assert resolve_provider_team_code(None) is None


def test_provider_team_identifier_isolation():
    """证明各源的球队数字 ID 彼此命名空间隔离，绝不发生跨源交叉混淆。"""
    # 例如 ID 20：BALLDONTLIE 是 NYK (20)，ESPN 是 PHI (20)，NBA 官方 ID 20 不存在（官方为 10 位数）
    assert resolve_provider_team_code("bdl:20") == "NYK"
    assert resolve_provider_team_code("espn:20") == "PHI"
    assert resolve_provider_team_code("nba:20") is None
    # 裸数字字符串不做推断
    assert resolve_provider_team_code("20") is None


# ---------------------------------------------------------------------------
# 3. 严格一对一绑定与开赛时间安全补全 (Workstream D)
# ---------------------------------------------------------------------------


def _canonical_match(
    mid: str = "500j-2026-11-13-4306",
    home: str = "费城76人",
    away: str = "多伦多猛龙",
    date: str = "2026-11-13",
    time_str: str = "",
    status: str = "upcoming",
) -> dict:
    return {
        "id": mid,
        "sport": "basketball",
        "league": "NBA",
        "date": date,
        "time": time_str,
        "home": home,
        "away": away,
        "jczq_no": "4306",
        "status": status,
        "kickoff_time_known": bool(time_str),
        "odds": {"sf": {"home_win": 1.70, "away_win": 2.10}},
    }


def test_balldontlie_successful_canonical_binding_and_enrichment():
    """验证 BALLDONTLIE 观测与 Canonical 赛事的正确绑定与时间补全。"""
    c_match = _canonical_match(mid="500j-2026-11-13-4306", home="费城76人", away="多伦多猛龙")
    game = _bdl_game(
        gid=1053164,
        utc="2026-11-13T00:30:00.000Z",  # 北京时间 08:30
        home_id=23,  # PHI -> 费城76人
        visitor_id=28,  # TOR -> 多伦多猛龙
    )
    obs, _ = parse_balldontlie_nba_games(_bdl_payload([game]))

    now_test = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    binding = bind_schedule_observations_to_canonical([c_match], obs, now=now_test)

    assert binding["bound_count"] == 1
    assert binding["rejected_count"] == 0
    decision = binding["decisions"][0]
    assert decision["bound"] is True
    assert decision["beijing_time"] == "08:30"
    assert decision["change_classification"] == CHANGE_FIRST_VERIFIED_KICKOFF

    # 执行就地安全补全
    matches = [deepcopy(c_match)]
    res = apply_nba_schedule_time_enrichment(matches, obs, now=now_test)

    assert res["enriched"] == 1
    m = matches[0]
    assert m["time"] == "08:30"
    assert m["kickoff_time_known"] is True
    prov = m["kickoff_provenance"]
    assert prov["actual_kickoff_time_source"] == PROVIDER_BALLDONTLIE_NBA_GAMES
    assert prov["kickoff_source_event_id"] == "1053164"
    assert prov["kickoff_verification_status"] == "SCHEDULE_PROVIDER_VERIFIED"
    # 核心字段完全不变
    assert m["id"] == c_match["id"]
    assert m["home"] == c_match["home"]
    assert m["away"] == c_match["away"]
    assert m["odds"] == c_match["odds"]
    assert m["status"] == "upcoming"


def test_balldontlie_binding_rejection_reasons():
    """验证各类 fail-closed 绑定拒绝码。"""
    now_test = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)

    # 1. 主客反向拒绝 (HOME_AWAY_ORIENTATION_CONFLICT)
    c_match = _canonical_match(home="费城76人", away="多伦多猛龙")
    rev_game = _bdl_game(home_id=28, visitor_id=23)  # TOR 主, PHI 客
    obs_rev, _ = parse_balldontlie_nba_games(_bdl_payload([rev_game]))
    b_rev = bind_schedule_observations_to_canonical([c_match], obs_rev, now=now_test)
    assert b_rev["decisions"][0]["reason"] == REASON_HOME_AWAY_ORIENTATION_CONFLICT

    # 2. 北京日期不匹配 (BEIJING_EVENT_DATE_MISMATCH)
    mis_game = _bdl_game(utc="2026-11-14T00:30:00.000Z")  # 北京时间 11-14
    obs_mis, _ = parse_balldontlie_nba_games(_bdl_payload([mis_game]))
    b_mis = bind_schedule_observations_to_canonical([c_match], obs_mis, now=now_test)
    assert b_mis["decisions"][0]["reason"] == REASON_BEIJING_EVENT_DATE_MISMATCH

    # 3. 比赛已开赛拒绝 (GAME_ALREADY_STARTED)
    past_now = datetime(2026, 11, 13, 9, 0, tzinfo=BEIJING_TZ)  # 开赛 08:30 之后
    norm_game = _bdl_game(utc="2026-11-13T00:30:00.000Z")
    obs_norm, _ = parse_balldontlie_nba_games(_bdl_payload([norm_game]))
    b_past = bind_schedule_observations_to_canonical([c_match], obs_norm, now=past_now)
    assert b_past["decisions"][0]["reason"] == REASON_GAME_ALREADY_STARTED

    # 4. 比赛非 scheduled (OFFICIAL_GAME_NOT_SCHEDULED)
    fin_game = _bdl_game(status_state="final", status="Final")
    obs_fin, _ = parse_balldontlie_nba_games(_bdl_payload([fin_game]))
    b_fin = bind_schedule_observations_to_canonical([c_match], obs_fin, now=now_test)
    assert b_fin["decisions"][0]["reason"] == REASON_OFFICIAL_GAME_NOT_SCHEDULED

    # 5. 未找到赛事 (OFFICIAL_GAME_NOT_FOUND)
    other_game = _bdl_game(home_id=14, visitor_id=10)  # LAL vs GSW
    obs_other, _ = parse_balldontlie_nba_games(_bdl_payload([other_game]))
    b_not = bind_schedule_observations_to_canonical([c_match], obs_other, now=now_test)
    assert b_not["decisions"][0]["reason"] == REASON_OFFICIAL_GAME_NOT_FOUND


# ---------------------------------------------------------------------------
# 4. Default-Deny 网络守卫与凭据状态 (Workstream A & B)
# ---------------------------------------------------------------------------


def test_balldontlie_default_deny_network_guard(monkeypatch):
    """验证未显式授权网络时 (allow_network=False)，严格返回 disabled_pending_authorization。"""
    http_called = False

    def mock_get(*a, **kw):
        nonlocal http_called
        http_called = True
        raise AssertionError("Network HTTP must NOT be called when allow_network=False")

    monkeypatch.setattr(nba_schedule.requests, "get", mock_get)

    # 默认 allow_network=False
    rows, diag = fetch_nba_schedule_observations(["2026-11-13"])
    assert rows == []
    assert http_called is False
    assert diag["status"] == "disabled_pending_authorization"
    assert diag["provider"] == PROVIDER_BALLDONTLIE_NBA_GAMES
    assert "PROVIDER_AUTOMATED_USE_UNAUTHORIZED" in diag["errors"]


def test_balldontlie_missing_credentials_gate(monkeypatch):
    """验证即使 allow_network=True，缺少 DSL_BDL_API_KEY 时仍明确拒绝并报告凭据缺失。"""
    monkeypatch.delenv(ENV_BDL_API_KEY, raising=False)
    http_called = False

    def mock_get(*a, **kw):
        nonlocal http_called
        http_called = True
        raise AssertionError("Network HTTP must NOT be called without API key")

    monkeypatch.setattr(nba_schedule.requests, "get", mock_get)

    t_now = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    rows, diag = fetch_nba_schedule_observations(["2026-11-13"], now=t_now, allow_network=True)

    assert rows == []
    assert http_called is False
    assert diag["status"] == "disabled_missing_credentials"
    assert diag["coverage_status"] == "MISSING_API_KEY"
    assert diag["provider_authorization_status"] == "PROVIDER_CREDENTIALS_MISSING"
    assert "PROVIDER_CREDENTIALS_MISSING" in diag["errors"]


def test_balldontlie_mock_fetch_with_credentials_and_secret_redaction(monkeypatch):
    """验证携带合法凭据时的网络调用、请求头配置及凭据绝不泄露到诊断结构。"""
    clear_schedule_cache()
    api_key_secret = "super_secret_bdl_test_key_xyz987"
    captured_headers = {}

    class _MockResp:
        status_code = 200
        def json(self):
            return _bdl_payload([_bdl_game(gid=777001, home_id=14, visitor_id=10)])

    def _mock_get(url, headers=None, timeout=None):
        nonlocal captured_headers
        captured_headers = dict(headers or {})
        return _MockResp()

    monkeypatch.setattr(nba_schedule.requests, "get", _mock_get)

    t_now = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    rows, diag = fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=t_now,
        allow_network=True,
        api_key=api_key_secret,
    )

    assert len(rows) == 1
    assert rows[0]["provider_game_id"] == "777001"
    assert diag["status"] == "ok"
    assert diag["provider"] == PROVIDER_BALLDONTLIE_NBA_GAMES

    # 验证请求头正确传递 API Key
    assert captured_headers.get("Authorization") == api_key_secret

    # 验证诊断结构与观测字典中绝不暴露 API Key
    diag_str = json.dumps(diag)
    rows_str = json.dumps(rows)
    assert api_key_secret not in diag_str
    assert api_key_secret not in rows_str


def test_balldontlie_http_error_handling_matrix(monkeypatch):
    """验证 HTTP 401, 403, 429 与网络异常分类。"""
    clear_schedule_cache()
    t_now = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)

    # 401 Unauthorized
    class _Resp401:
        status_code = 401
    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **kw: _Resp401())
    rows, diag = fetch_nba_schedule_observations(["2026-11-13"], now=t_now, allow_network=True, api_key="bad_key")
    assert rows == []
    assert "HTTP_401_UNAUTHORIZED" in diag["errors"]
    assert diag["status"] == "unavailable"

    # 403 Forbidden
    clear_schedule_cache()
    class _Resp403:
        status_code = 403
    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **kw: _Resp403())
    rows, diag = fetch_nba_schedule_observations(["2026-11-13"], now=t_now, allow_network=True, api_key="forbidden_key")
    assert "HTTP_403_FORBIDDEN" in diag["errors"]

    # 429 Rate limited (带重试耗尽)
    clear_schedule_cache()
    call_count = 0
    class _Resp429:
        status_code = 429
    def _mock_429(*a, **kw):
        nonlocal call_count
        call_count += 1
        return _Resp429()
    monkeypatch.setattr(nba_schedule.requests, "get", _mock_429)
    rows, diag = fetch_nba_schedule_observations(["2026-11-13"], now=t_now, allow_network=True, api_key="rate_limit_key")
    assert call_count == 3  # 初次 + 2 次重试
    assert "HTTP_429_RATE_LIMITED" in diag["errors"]


def test_balldontlie_in_memory_cache_preserves_real_capture_time(monkeypatch):
    """验证 BALLDONTLIE 进程内缓存命中与观测真实抓取时间戳保真性。"""
    clear_schedule_cache()
    call_count = 0
    t_obs1 = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    t_obs2 = datetime(2026, 11, 12, 18, 1, tzinfo=BEIJING_TZ)

    class _MockResp:
        status_code = 200
        def json(self):
            return _bdl_payload([_bdl_game(gid=888001)])

    def _counting_get(*a, **kw):
        nonlocal call_count
        call_count += 1
        return _MockResp()

    monkeypatch.setattr(nba_schedule.requests, "get", _counting_get)

    # 首次抓取：网络请求并填充缓存
    rows1, diag1 = fetch_nba_schedule_observations(["2026-11-13"], now=t_obs1, allow_network=True, api_key="k")
    assert call_count == 1
    assert diag1.get("cache_hit") is False
    assert diag1["observation_origin"] == "NETWORK"
    first_obs_at = rows1[0]["observed_at"]

    # 二次抓取（TTL 90 秒内）：命中缓存，未发出额外网络请求
    rows2, diag2 = fetch_nba_schedule_observations(["2026-11-13"], now=t_obs2, allow_network=True, api_key="k")
    assert call_count == 1
    assert diag2["cache_hit"] is True
    assert diag2["observation_origin"] == "CACHE"
    assert rows2[0]["observed_at"] == first_obs_at  # 真实时间绝不被缓存时间覆盖
    assert diag2["checked_at"] == diag1["checked_at"]


# ---------------------------------------------------------------------------
# 5. 隔离端到端流程：开赛时间补全 -> 赛前预测闭环 -> 结算就绪 (Workstream E & F)
# ---------------------------------------------------------------------------


def test_isolated_balldontlie_pregame_prediction_and_settlement_closure(tmp_path, monkeypatch):
    """
    在隔离临时存储上端到端验证：
    1. 含有正规盘口的 Canonical NBA 赛事；
    2. 通过 BALLDONTLIE 获取开赛时间补全；
    3. 成功创建赛前预测快照 (generated_at < kickoff_at)；
    4. 重复抓取具备幂等性（不重复创建快照）；
    5. 完赛后结算流顺利流转。
    """
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))

    # 初始化 7 个隔离存储
    canonical_file = tmp_path / "daily_matches.json"
    pred_file = tmp_path / "prediction_snapshots.json"
    odds_file = tmp_path / "odds_snapshots.json"
    settle_file = tmp_path / "settlements.json"
    eval_file = tmp_path / "evaluation_rows.json"
    team_file = tmp_path / "team_strength.json"
    calib_file = tmp_path / "calibrated.json"

    raw_fixture = {
        "id": "500j-2030-05-15-4301",
        "sport": "basketball",
        "league": "NBA",
        "date": "2030-05-15",
        "time": "",  # 初始未知开赛时间
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "jczq_no": "4301",
        "status": "upcoming",
        "kickoff_time_known": False,
        "odds": {
            "sf": {"home_win": 1.80, "away_win": 2.00},
            "dxf": {"line": 224.5, "over": 1.90, "under": 1.90},
        },
        "market_tracked": True,
    }
    canonical_file.write_text(json.dumps([raw_fixture], ensure_ascii=False), encoding="utf-8")
    pred_file.write_text(json.dumps({"snapshots": {}}, ensure_ascii=False), encoding="utf-8")
    odds_file.write_text(json.dumps({"snapshots": {}}, ensure_ascii=False), encoding="utf-8")
    settle_file.write_text(json.dumps({"settlements": {}}, ensure_ascii=False), encoding="utf-8")
    eval_file.write_text(json.dumps({"evaluations": []}, ensure_ascii=False), encoding="utf-8")
    team_file.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
    calib_file.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")

    # BALLDONTLIE 赛程观测：LAL(14) vs GSW(10)，开赛 2030-05-15 02:30 UTC -> 北京时间 10:30
    game = _bdl_game(
        gid=990001,
        utc="2030-05-15T02:30:00.000Z",
        home_id=14,
        visitor_id=10,
    )
    obs, _ = parse_balldontlie_nba_games(_bdl_payload([game]))

    # Step 1: 开赛前时间补全 (当前时间为比赛日 09:00，比赛开赛为 10:30)
    t_pregame = datetime(2030, 5, 15, 9, 0, tzinfo=BEIJING_TZ)
    matches = [deepcopy(raw_fixture)]
    enrich_res = apply_nba_schedule_time_enrichment(matches, obs, now=t_pregame)
    assert enrich_res["enriched"] == 1
    enriched_match = matches[0]
    assert enriched_match["time"] == "10:30"
    assert enriched_match["kickoff_time_known"] is True

    # 将补全开赛时间的赛事写回隔离 canonical
    canonical_file.write_text(json.dumps([enriched_match], ensure_ascii=False), encoding="utf-8")

    # Step 2: 捕获赛前预测快照
    with daily_loader.override_beijing_now(t_pregame):
        snap_result = prediction_snapshots.capture_snapshot(
            daily_loader.enrich_match(dict(enriched_match)), now=t_pregame
        )
    assert snap_result.created is True
    assert snap_result.snapshot is not None
    snap = snap_result.snapshot
    assert snap["match_id"] == "500j-2030-05-15-4301"
    assert snap["sport"] == "basketball"
    assert "model_probabilities" in snap

    gen_dt = datetime.fromisoformat(snap["generated_at"]).astimezone(BEIJING_TZ)
    kickoff_dt = datetime(2030, 5, 15, 10, 30, tzinfo=BEIJING_TZ)
    assert gen_dt < kickoff_dt  # 严格验证快照生成于比赛开赛前！

    # Step 3: 重复捕获幂等性验证（不重复新建）
    snap_result2 = prediction_snapshots.capture_snapshot(
        daily_loader.enrich_match(dict(enriched_match)), now=t_pregame + timedelta(minutes=5)
    )
    assert snap_result2.created is False
    assert snap_result2.classification == prediction_snapshots.EXISTING_IMMUTABLE_SNAPSHOT
    store2 = prediction_snapshots._load_store()
    assert len(store2["snapshots"]) == 1

    # Step 4: 开赛后尝试补全或预测 -> 严格拒绝
    t_post_kickoff = datetime(2030, 5, 15, 11, 0, tzinfo=BEIJING_TZ)
    b_post = bind_schedule_observations_to_canonical([raw_fixture], obs, now=t_post_kickoff)
    assert b_post["decisions"][0]["reason"] == REASON_GAME_ALREADY_STARTED

    # Step 5: 完赛结算流转验证 (Workstream F)
    # 隔离合成赛果（不是现实比赛的已核验结果）
    finished_match = deepcopy(enriched_match)
    finished_match["status"] = "finished"
    finished_match["score"] = {"ft": [115, 108]}
    canonical_file.write_text(json.dumps([finished_match], ensure_ascii=False), encoding="utf-8")

    from utils.settlements import settle_snapshot, get_settlements_for_match
    settle_res, s_created = settle_snapshot(snap, finished_match, now=t_post_kickoff + timedelta(hours=3))
    assert s_created is True
    assert settle_res["settlement_id"] is not None
    assert settle_res["final_score"] == {"home": 115, "away": 108}
    assert settle_res["actual_outcome"] == "home_win"

    from utils.evaluation_rows import capture_evaluation_row
    eval_row, e_created = capture_evaluation_row(snap, settle_res, now=t_post_kickoff + timedelta(hours=3))
    assert e_created is True
    assert eval_row["evaluation_id"] is not None
    assert eval_row["sport"] == "basketball"


def test_zero_real_data_writes_during_suite():
    """验证在整个测试过程中，生产库中的 7 个数据文件完全未被触碰（若存在则字节哈希保真）。"""
    from tools.shadow_trusted_time_validation import protected_store_hashes
    hashes = protected_store_hashes()
    assert len(hashes) == 7
    for fn, h in hashes.items():
        if h == "<absent>":
            continue
        assert len(h) == 64
