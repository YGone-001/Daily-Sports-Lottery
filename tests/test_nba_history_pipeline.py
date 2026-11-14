"""Phase 3-X Workstream B/C：NBA 历史完赛解析、确定性数据集与有界分页/频控（离线）。

全部测试在合成载荷与 mock HTTP 上运行：不依赖 API Key、真实网络或操作者磁盘。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import utils.nba_history as nh
import utils.nba_schedule as ns
from utils.nba_history import (
    assemble_historical_dataset,
    fetch_historical_games,
    parse_historical_games,
)

BJ = timezone.utc  # 测试统一用 UTC 构造，解析器内部转北京


@pytest.fixture(autouse=True)
def deterministic_pacing(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(ns.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(ns.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + max(0.0, s)))
    monkeypatch.setattr(ns, "_LAST_BDL_REQUEST_MONO", None)
    monkeypatch.setattr(ns, "_BDL_RETRY_NOT_BEFORE", 0.0)
    monkeypatch.delenv("DSL_BDL_API_KEY", raising=False)
    yield


def _final_game(gid=1001, home_id=2, away_id=20, hs=110, aws=100,
                utc="2026-10-20T23:00:00.000Z", season=2026, period=4,
                status_state="final", postponed=False, postseason=False):
    return {
        "id": gid, "date": utc[:10], "season": season, "status": "Final",
        "status_state": status_state, "period": period, "time": "Final",
        "postseason": postseason, "postponed": postponed,
        "home_team_score": hs, "visitor_team_score": aws, "datetime": utc,
        "home_team": {"id": home_id, "full_name": "Boston Celtics", "abbreviation": "BOS"},
        "visitor_team": {"id": away_id, "full_name": "New York Knicks", "abbreviation": "NYK"},
    }


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _page(games, next_cursor=None):
    return {"data": games, "meta": {"next_cursor": next_cursor, "per_page": 100}}


# ---------------------------------------------------------------------------
# 解析与身份映射
# ---------------------------------------------------------------------------

def test_valid_final_game_accepted_with_direction_and_identity():
    games, errors = parse_historical_games({"data": [_final_game()]},
                                           observed_at=datetime(2026, 11, 14, tzinfo=timezone.utc))
    assert not errors
    assert len(games) == 1
    g = games[0]
    assert g["home_code"] == "BOS" and g["away_code"] == "NYK"
    assert g["nba_home_team_id"] == "2" and g["nba_away_team_id"] == "20"
    assert g["home_final_score"] == 110 and g["away_final_score"] == 100
    assert g["season"] == 2026 and g["season_label"] == "2026-27"
    assert g["game_status"] == "final" and g["overtime_periods"] == 0
    assert g["postseason_flag"] is False
    assert g["source_content_digest"] and g["source_reference"]


def test_single_game_dict_response_supported():
    games, errors = parse_historical_games({"data": _final_game()})
    assert not errors and len(games) == 1


def test_overtime_periods_derived_from_period():
    games, _ = parse_historical_games({"data": [_final_game(period=6)]})
    assert games[0]["overtime_periods"] == 2


@pytest.mark.parametrize("override,reason", [
    ({"status_state": "scheduled"}, "GAME_NOT_CONFIRMED_FINAL"),
    ({"status_state": "in_progress"}, "GAME_NOT_CONFIRMED_FINAL"),
    ({"postponed": True}, "GAME_NOT_CONFIRMED_FINAL"),
    ({"hs": 110.0}, "MALFORMED_FINAL_SCORE"),
    ({"hs": "110"}, "MALFORMED_FINAL_SCORE"),
    ({"hs": -1}, "MALFORMED_FINAL_SCORE"),
    ({"hs": True}, "MALFORMED_FINAL_SCORE"),
    ({"period": 3}, "MALFORMED_PERIOD"),
    ({"home_id": 999}, "UNKNOWN_NBA_FRANCHISE_ID"),
    ({"away_id": 2}, "HOME_AWAY_SAME_TEAM"),
    ({"gid": 0}, "MISSING_GAME_ID"),
    ({"season": 12}, "MALFORMED_SEASON"),
    ({"utc": "not-a-date"}, "INVALID_EVENT_TIMESTAMP"),
])
def test_invalid_games_rejected(override, reason):
    games, errors = parse_historical_games({"data": [_final_game(**override)]})
    assert games == []
    assert any(reason in e for e in errors), errors


def test_missing_competitors_rejected():
    raw = _final_game()
    del raw["visitor_team"]
    games, errors = parse_historical_games({"data": [raw]})
    assert games == [] and any("MISSING_COMPETITORS" in e for e in errors)


def test_identical_duplicate_deduplicated():
    g = _final_game()
    games, errors = parse_historical_games({"data": [g, dict(g)]})
    assert len(games) == 1 and not errors


def test_conflicting_duplicate_rejected():
    games, errors = parse_historical_games({"data": [_final_game(hs=110), _final_game(hs=99)]})
    assert games == []
    assert any("CONFLICTING_DUPLICATE_GAME_ID" in e for e in errors)


def test_malformed_payload_rejected():
    assert parse_historical_games([]) == ([], ["MALFORMED_HISTORICAL_PAYLOAD"])
    assert parse_historical_games({"data": "x"})[1] == ["MALFORMED_HISTORICAL_PAYLOAD"]


def test_deterministic_sort_by_kickoff_then_id():
    a = _final_game(gid=2, utc="2026-10-21T23:00:00.000Z")
    b = _final_game(gid=1, utc="2026-10-20T23:00:00.000Z")
    games, _ = parse_historical_games({"data": [a, b]})
    assert [g["provider_game_id"] for g in games] == ["1", "2"]


# ---------------------------------------------------------------------------
# 数据集装配
# ---------------------------------------------------------------------------

def test_dataset_cutoff_excludes_future_games():
    games, _ = parse_historical_games({"data": [
        _final_game(gid=1, utc="2026-10-20T23:00:00.000Z"),
        _final_game(gid=2, utc="2026-11-20T23:00:00.000Z"),
    ]})
    ds = assemble_historical_dataset(games, cutoff_utc=datetime(2026, 11, 14, tzinfo=timezone.utc))
    assert ds["game_count"] == 1 and ds["excluded_post_cutoff"] == 1
    assert ds["production_store_write"] is False
    assert ds["canonical_market_modified"] is False


def test_dataset_regular_postseason_split_and_seasons():
    games, _ = parse_historical_games({"data": [
        _final_game(gid=1, postseason=False, season=2025),
        _final_game(gid=2, postseason=True, season=2025),
    ]})
    ds = assemble_historical_dataset(games, requested_seasons=[2025, 2026])
    assert ds["regular_season_count"] == 1 and ds["postseason_count"] == 1
    assert ds["seasons"] == [2025] and ds["requested_seasons"] == [2025, 2026]
    assert ds["distinct_team_count"] == 2


def test_dataset_global_conflict_removed():
    g1, _ = parse_historical_games({"data": [_final_game(hs=110)]})
    g2, _ = parse_historical_games({"data": [_final_game(hs=99)]})
    ds = assemble_historical_dataset(g1 + g2)
    assert ds["game_count"] == 0 and ds["conflicting_game_ids"] == ["1001"]


# ---------------------------------------------------------------------------
# 有界分页 / 频控 / 凭据门禁（mock HTTP）
# ---------------------------------------------------------------------------

def _install_responses(monkeypatch, responses):
    calls = []
    seq = list(responses)

    def fake_get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return seq.pop(0)

    monkeypatch.setattr(nh.requests, "get", fake_get)
    return calls


def test_default_deny_network_no_request(monkeypatch):
    calls = _install_responses(monkeypatch, [])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=False)
    assert games == [] and calls == []
    assert diag["status"] == "disabled_pending_authorization"
    assert diag["network_requests_made"] == 0
    assert diag["pagination_complete"] is False


def test_missing_credentials_no_request(monkeypatch):
    calls = _install_responses(monkeypatch, [])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True, api_key=None)
    assert games == [] and calls == []
    assert diag["status"] == "disabled_missing_credentials"


def test_complete_two_page_fetch(monkeypatch):
    calls = _install_responses(monkeypatch, [
        _Resp(_page([_final_game(gid=1)], next_cursor=5)),
        _Resp(_page([_final_game(gid=2, utc="2026-10-22T23:00:00.000Z")], next_cursor=None)),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="SECRET_TEST_KEY", max_elapsed_seconds=10000)
    assert len(games) == 2 and diag["pagination_complete"] is True
    assert diag["pages_fetched"] == 2 and diag["network_requests_made"] == 2
    assert diag["status"] == "ok"
    assert "cursor=5" in calls[1]["url"]
    # 凭据绝不进入诊断
    assert "SECRET_TEST_KEY" not in json.dumps(diag, ensure_ascii=False)


def test_incomplete_pagination_returns_zero_games(monkeypatch):
    _install_responses(monkeypatch, [
        _Resp(_page([_final_game(gid=1)], next_cursor=5)),
        _Resp({}, status=500),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == []
    assert diag["pagination_complete"] is False
    assert diag["coverage_status"] == "PAGINATION_INCOMPLETE"
    assert diag["pagination_failure_reason"] == "HTTP_500"


def test_conflicting_provider_id_across_pages(monkeypatch):
    _install_responses(monkeypatch, [
        _Resp(_page([_final_game(gid=1, hs=110)], next_cursor=5)),
        _Resp(_page([_final_game(gid=1, hs=99)], next_cursor=None)),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_complete"] is False
    assert diag["pagination_failure_reason"] == "CONFLICTING_PROVIDER_GAME_ID"


def test_cursor_cycle_detected(monkeypatch):
    _install_responses(monkeypatch, [
        _Resp(_page([_final_game(gid=1)], next_cursor=5)),
        _Resp(_page([_final_game(gid=2, utc="2026-10-22T23:00:00.000Z")], next_cursor=5)),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_failure_reason"] == "CURSOR_CYCLE"


def test_request_budget_exhausted(monkeypatch):
    _install_responses(monkeypatch, [
        _Resp(_page([_final_game(gid=1)], next_cursor=5)),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_requests=1, max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_failure_reason"] == "REQUEST_BUDGET_EXHAUSTED"
    assert diag["network_requests_made"] == 1


def test_malformed_meta_fails_closed(monkeypatch):
    _install_responses(monkeypatch, [_Resp({"data": [_final_game()]})])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_failure_reason"] == "MALFORMED_PAGINATION_META"


def test_429_bounded_retry_then_success(monkeypatch):
    calls = _install_responses(monkeypatch, [
        _Resp({}, status=429, headers={"Retry-After": "1"}),
        _Resp(_page([_final_game(gid=1)], next_cursor=None)),
    ])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert len(games) == 1 and diag["pagination_complete"] is True
    assert len(calls) == 2


def test_429_exhaustion_fails(monkeypatch):
    _install_responses(monkeypatch, [_Resp({}, status=429)] * 3)
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == [] and diag["pagination_failure_reason"] == "HTTP_429_RATE_LIMITED"


def test_401_not_retried(monkeypatch):
    calls = _install_responses(monkeypatch, [_Resp({}, status=401)])
    games, diag = fetch_historical_games(seasons=[2026], allow_network=True,
                                         api_key="k", max_elapsed_seconds=10000)
    assert games == [] and len(calls) == 1
    assert diag["pagination_failure_reason"] == "HTTP_401_UNAUTHORIZED"


@pytest.mark.parametrize("per_page", [0, 101, 1.5])
def test_per_page_validation(per_page):
    with pytest.raises(ValueError):
        fetch_historical_games(seasons=[2026], allow_network=False, per_page=per_page)


def test_empty_season_window():
    games, diag = fetch_historical_games(seasons=[], allow_network=False)
    assert games == [] and diag["pagination_failure_reason"] == "EMPTY_SEASON_WINDOW"