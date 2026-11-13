"""Offline pagination, pacing and normal-refresh acceptance with controlled clocks."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

import config
from utils import daily_loader, nba_schedule as ns, prediction_snapshots, scraper
from utils.daily_loader import BEIJING_TZ, override_beijing_now
from test_balldontlie_schedule_adapter import _bdl_game, _bdl_payload

NOW = datetime(2030, 5, 15, 7, tzinfo=BEIJING_TZ)


class Clock:
    def __init__(self):
        self.value = 0.0
        self.waits = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds


class Response:
    def __init__(self, payload=None, code=200, headers=None):
        self.payload, self.status_code, self.headers = payload, code, headers or {}

    def json(self):
        return deepcopy(self.payload)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(ns.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(ns.time, "sleep", clock.sleep)
    monkeypatch.setattr(ns, "_LAST_BDL_REQUEST_MONO", None)
    monkeypatch.setattr(ns, "_BDL_RETRY_NOT_BEFORE", 0.0, raising=False)
    monkeypatch.delenv("DSL_BDL_API_KEY", raising=False)
    monkeypatch.setattr(ns.requests, "get", lambda *a, **k: pytest.fail("real network forbidden"))
    ns.clear_schedule_cache()
    yield clock
    ns.clear_schedule_cache()


def fetch(monkeypatch, responses, **kwargs):
    calls = []
    def get(url, **kw):
        calls.append((url, ns.time.monotonic(), kw))
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    monkeypatch.setattr(ns.requests, "get", get)
    rows, diag = ns.fetch_nba_schedule_observations(
        ["2030-05-15"], now=NOW, allow_network=True, api_key="synthetic-secret", **kwargs)
    return rows, diag, calls


def game(gid=1, **kwargs):
    return _bdl_game(gid=gid, utc="2030-05-15T00:30:00Z", **kwargs)


def test_two_pages_preserve_filters_and_pace(monkeypatch):
    rows, d, calls = fetch(monkeypatch, [Response(_bdl_payload([game(i) for i in range(1, 27)], 26)),
                                       Response(_bdl_payload([game(27)]))])
    assert len(rows) == 27
    assert d["pagination_complete"] is True
    assert d["pages_fetched"] == 2
    assert d["provider_rows_observed"] == d["unique_games_observed"] == 27
    assert d["requested_page_size"] == 100
    assert "per_page=100" in calls[0][0] and "cursor=26" in calls[1][0]
    assert "dates[]=2030-05-14" in calls[1][0] and "dates[]=2030-05-15" in calls[1][0]
    assert calls[1][1] - calls[0][1] >= 12.1


@pytest.mark.parametrize("terminal", [{"next_cursor": None}, {}])
def test_one_page_and_empty_terminal(monkeypatch, terminal):
    rows, d, _ = fetch(monkeypatch, [Response({"data": [], "meta": terminal})])
    assert not rows and d["pagination_complete"] and d["status"] == "empty"


def test_empty_second_page(monkeypatch):
    rows, d, _ = fetch(monkeypatch, [Response(_bdl_payload([game()], 1)), Response(_bdl_payload([]))])
    assert len(rows) == 1 and d["pagination_complete"] and d["pages_fetched"] == 2


def test_full_maximum_page_followed_by_more_games(monkeypatch):
    rows, d, _ = fetch(monkeypatch, [Response(_bdl_payload([game(i) for i in range(1, 101)], 100, 100)),
                                   Response(_bdl_payload([game(101)], None, 100))])
    assert len(rows) == 101 and d["pagination_complete"] and d["pages_fetched"] == 2


@pytest.mark.parametrize("cursor", [True, -1, 1.5, "2", {}, "synthetic-secret"])
def test_malformed_cursor(monkeypatch, cursor):
    rows, d, calls = fetch(monkeypatch, [Response(_bdl_payload([game()], cursor))])
    assert not rows and not d["pagination_complete"] and len(calls) == 1
    assert d["pagination_failure_reason"] == "MALFORMED_CURSOR"
    assert "synthetic-secret" not in json.dumps(d)


def test_cursor_cycle(monkeypatch):
    rows, d, calls = fetch(monkeypatch, [Response(_bdl_payload([game()], 1)),
                                       Response(_bdl_payload([game(2)], 1))])
    assert not rows and len(calls) == 2
    assert d["pagination_failure_reason"] == "CURSOR_CYCLE"


@pytest.mark.parametrize("change", [{"home_team": {"id": 14}}, {"status_state": "final"},
                                    {"datetime": "2030-05-15T01:30:00Z"}])
def test_conflicting_duplicate_is_rejected(monkeypatch, change):
    other = {**game(), **change}
    rows, d, _ = fetch(monkeypatch, [Response(_bdl_payload([game()], 1)), Response(_bdl_payload([other]))])
    assert not rows and d["conflicting_game_ids"] == ["1"]
    assert not d["pagination_complete"]


def test_identical_duplicate_deduplicated(monkeypatch):
    rows, d, _ = fetch(monkeypatch, [Response(_bdl_payload([game()], 1)), Response(_bdl_payload([game()]))])
    assert len(rows) == 1 and d["duplicates_deduplicated"] == 1
    assert d["provider_rows_observed"] == 2 and d["unique_games_observed"] == 1


@pytest.mark.parametrize("failure", [Response(code=503), RuntimeError("synthetic-secret"), Response({"bad": []})])
def test_later_page_failure(monkeypatch, failure):
    rows, d, _ = fetch(monkeypatch, [Response(_bdl_payload([game()], 1)), failure])
    assert not rows and d["coverage_status"] == "PAGINATION_INCOMPLETE"
    assert not d["pagination_complete"] and d["next_cursor_remaining"] == 1
    assert "synthetic-secret" not in json.dumps(d)


@pytest.mark.parametrize("size", [0, 101, True, 1.5])
def test_page_size_boundaries(monkeypatch, size):
    with pytest.raises(ValueError):
        fetch(monkeypatch, [], per_page=size)


@pytest.mark.parametrize("size", [1, 100])
def test_valid_page_sizes(monkeypatch, size):
    _, d, calls = fetch(monkeypatch, [Response(_bdl_payload([]))], per_page=size)
    assert d["requested_page_size"] == size and f"per_page={size}" in calls[0][0]


def test_request_and_time_budgets(monkeypatch):
    for budget in ({"max_requests": 1}, {"max_elapsed_seconds": 5}):
        ns.clear_schedule_cache()
        rows, d, calls = fetch(monkeypatch, [Response(_bdl_payload([game()], 1))], **budget)
        assert not rows and len(calls) <= 1 and not d["pagination_complete"]
        assert "BUDGET_EXHAUSTED" in d["pagination_failure_reason"]


def test_dispatch_lock_wait_is_bounded(monkeypatch):
    class BusyLock:
        def acquire(self, *, timeout):
            assert 0 <= timeout <= 5
            return False
    monkeypatch.setattr(ns, "_BDL_REQUEST_LOCK", BusyLock())
    _, d, calls = fetch(monkeypatch, [], max_elapsed_seconds=5)
    assert not calls and d["pagination_failure_reason"] == "TIME_BUDGET_EXHAUSTED"


def test_free_tier_sequential_calls_and_cache(monkeypatch):
    times = []
    def get(*a, **kw):
        times.append(ns.time.monotonic())
        return Response(_bdl_payload([game()]))
    monkeypatch.setattr(ns.requests, "get", get)
    for _ in range(7):
        ns.clear_schedule_cache()
        ns.fetch_nba_schedule_observations(["2030-05-15"], now=NOW, allow_network=True, api_key="k")
    assert all(b - a >= 60 for a, b in zip(times, times[5:]))
    _, cached = ns.fetch_nba_schedule_observations(["2030-05-15"], now=NOW, allow_network=True, api_key="k")
    assert len(times) == 7 and cached["cache_hit"] and cached["network_requests_made"] == 0


def test_429_retry_after_and_exhaustion(monkeypatch):
    rows, d, calls = fetch(monkeypatch, [Response(code=429, headers={"Retry-After": "30"}),
                                       Response(code=429), Response(code=429)])
    assert not rows and len(calls) == 3 and calls[1][1] - calls[0][1] >= 30
    assert calls[2][1] - calls[1][1] >= 12.1
    assert "HTTP_429_RATE_LIMITED" in d["errors"]


def test_retry_after_http_date(monkeypatch):
    from email.utils import format_datetime
    with override_beijing_now(NOW):
        _, d, calls = fetch(monkeypatch, [Response(code=429, headers={
            "Retry-After": format_datetime(NOW + timedelta(seconds=35))}), Response(_bdl_payload([]))])
    assert d["pagination_complete"] and calls[1][1] - calls[0][1] >= 35


def test_retry_after_beyond_budget_does_not_retry_early(monkeypatch):
    _, d, calls = fetch(monkeypatch, [Response(code=429, headers={"Retry-After": "600"})])
    assert len(calls) == 1 and d["pagination_failure_reason"] == "TIME_BUDGET_EXHAUSTED"
    # A separate call shares the same cooldown; clearing the cache cannot bypass it.
    ns.clear_schedule_cache()
    _, d, calls = fetch(monkeypatch, [])
    assert not calls and d["pagination_failure_reason"] == "TIME_BUDGET_EXHAUSTED"


def test_retries_and_pages_share_pacing(monkeypatch):
    _, d, calls = fetch(monkeypatch, [Response(code=429), Response(_bdl_payload([game()], 1)),
                                    Response(code=429), Response(_bdl_payload([]))])
    assert d["pagination_complete"] and d["network_requests_made"] == 4
    assert all(b[1] - a[1] >= 12.1 - 1e-9 for a, b in zip(calls, calls[1:]))
    assert d["rate_limit_scope"] == "PROCESS_LOCAL" and not d["global_account_limit_guaranteed"]


def test_process_local_limiter_does_not_coordinate_independent_processes():
    import subprocess
    import sys
    script = '''
from utils import nba_schedule as ns
from unittest.mock import patch
from datetime import datetime
ns.requests.get = lambda *a, **kw: type('R', (), {'status_code':200, 'json': lambda s: {'data':[], 'meta':{}}})()
with patch.object(ns.time, 'monotonic', return_value=0):
    rows, d = ns.fetch_nba_schedule_observations(['2030-05-15'], now=datetime.fromisoformat('2030-05-15T07:00:00+08:00'), allow_network=True, api_key='synthetic')
print(ns._LAST_BDL_REQUEST_MONO, d['network_requests_made'], d['global_account_limit_guaranteed'])
'''
    outputs = [subprocess.check_output([sys.executable, "-c", script], text=True).strip() for _ in range(2)]
    assert outputs == ["0 1 False", "0 1 False"]


@pytest.mark.parametrize("kwargs", [{"status_state": "scheduled", "postponed": "false"},
                                   {"status_state": "scheduled", "status": "Final"},
                                   {"status_state": "unknown", "status": "7:30 PM ET"}])
def test_anomalous_status_cannot_create_scheduled_evidence(kwargs):
    rows, _ = ns.parse_balldontlie_nba_games(_bdl_payload([game(**kwargs)]))
    assert rows[0]["game_status"] == "unknown"


@pytest.mark.parametrize("code", [401, 403])
def test_auth_never_retried(monkeypatch, code):
    _, d, calls = fetch(monkeypatch, [Response(code=code)])
    assert len(calls) == 1 and d["status"] == "unavailable"


@pytest.mark.parametrize("state", ["scheduled", "in_progress", "final", "postponed", "canceled",
                                  "delayed", "suspended", "abandoned", "unknown", "in_schedule", ""])
def test_status_state_is_authoritative(state):
    rows, _ = ns.parse_balldontlie_nba_games(_bdl_payload([game(status_state=state)]))
    expected = {"in_progress": "live", "in_schedule": "unknown", "": "unknown"}.get(state, state)
    assert rows[0]["game_status"] == expected
    matches = [canonical()]
    result = ns.apply_nba_schedule_time_enrichment(matches, rows, now=NOW)
    assert result["enriched"] == (1 if state == "scheduled" else 0)


def canonical():
    return {"id": "500j-2030-05-15-4306", "sport": "basketball", "league": "NBA",
            "date": "2030-05-15", "time": "", "kickoff_time_known": False,
            "status": "upcoming", "home": "费城76人", "away": "多伦多猛龙",
            "jczq_no": "4306", "round": "4306", "market_tracked": True,
            "score": None, "odds": {"home_win": 1.8, "away_win": 2.0, "matchnum": "4306"}}


@pytest.fixture
def refresh_env(isolated_data_dir, monkeypatch):
    from test_basketball_operational_acceptance import write_stores
    write_stores(isolated_data_dir, [canonical()])
    monkeypatch.setattr(config, "ENABLE_NBA_SCHEDULE_NETWORK", True, raising=False)
    monkeypatch.setenv("DSL_BDL_API_KEY", "synthetic-secret")
    monkeypatch.setattr(scraper.fetcher_500, "fetch_live_matches", lambda *a: [])
    monkeypatch.setattr(scraper.fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(scraper.fetcher_500, "fetch_jczq_xml",
                        lambda sport: [canonical()] if sport == "basketball" else [])
    monkeypatch.setattr(scraper.fetcher_500, "fetch_finished_matches", lambda: [])
    monkeypatch.setattr(ns.requests, "get", lambda *a, **k: Response(_bdl_payload([game()])))
    return isolated_data_dir


def test_normal_refresh_durable_pregame_and_settlement(refresh_env, monkeypatch):
    with override_beijing_now(NOW):
        first = scraper.refresh(verbose=False)
        assert first["nba_schedule_enriched"] == first["prediction_snapshots_added"] == 1
        stored = daily_loader.load_json("daily_matches.json")["matches"][0]
        for key in ("id", "home", "away", "odds", "status", "score", "jczq_no"):
            assert stored[key] == canonical()[key]
        snapshot = prediction_snapshots.get_snapshots_for_match(stored["id"])[0]
        assert stored["kickoff_provenance"]["actual_kickoff_time_source"] == "balldontlie_nba_games"
        assert stored["kickoff_provenance"]["kickoff_verification_status"] == "SCHEDULE_PROVIDER_VERIFIED"
        assert snapshot["model_version"]
        assert datetime.fromisoformat(snapshot["generated_at"]) == NOW < daily_loader.get_match_datetime(stored)
        before = (refresh_env / "prediction_snapshots.json").read_bytes()
        second = scraper.refresh(verbose=False)
        assert second["prediction_snapshots_added"] == 0
        assert (refresh_env / "prediction_snapshots.json").read_bytes() == before
        assert prediction_snapshots.get_snapshot(snapshot["snapshot_id"]) == snapshot
    # A later controlled source result enters the same orchestration; no Canonical file replacement.
    final = {**canonical(), "status": "finished", "score": {"ft": [115, 108]}}
    monkeypatch.setattr(scraper.fetcher_500, "fetch_jczq_xml", lambda *a: [])
    monkeypatch.setattr(scraper.fetcher_500, "fetch_finished_matches", lambda: [final])
    with override_beijing_now(NOW + timedelta(hours=5)):
        result = scraper.refresh(verbose=False)
        assert result["settlements_added"] == result["evaluation_rows_added"] == 1
        again = scraper.refresh(verbose=False)
        assert again["settlements_added"] == again["evaluation_rows_added"] == 0
    assert (refresh_env / "prediction_snapshots.json").read_bytes() == before


def test_previously_enriched_time_requires_scheduled_provider_evidence(refresh_env, monkeypatch):
    stored = canonical()
    rows, _ = ns.parse_balldontlie_nba_games(_bdl_payload([game()]), observed_at=NOW)
    ns.apply_nba_schedule_time_enrichment([stored], rows, now=NOW)
    daily_loader.save_json("daily_matches.json", {"matches": [stored]})
    monkeypatch.setattr(ns.requests, "get", lambda *a, **k: Response(_bdl_payload([game(status_state="unknown")])))
    with override_beijing_now(NOW):
        result = scraper.refresh(verbose=False)
    assert result["prediction_snapshots_added"] == 0
    assert daily_loader.load_json("daily_matches.json")["matches"][0]["status"] == "upcoming"


def test_normal_refresh_schedule_request_crosses_kickoff(refresh_env, monkeypatch):
    wall_clock = [NOW]
    def delayed_response(*a, **kw):
        wall_clock[0] = NOW + timedelta(hours=2)
        return Response(_bdl_payload([game()]))
    monkeypatch.setattr(ns.requests, "get", delayed_response)
    with override_beijing_now(lambda: wall_clock[0]):
        result = scraper.refresh(verbose=False)
    assert result["nba_schedule_enriched"] == result["prediction_snapshots_added"] == 0
    assert daily_loader.load_json("prediction_snapshots.json")["snapshots"] == {}


def test_market_time_does_not_erase_verified_bdl_provenance():
    from utils.match_lifecycle import resolve_match_update
    stored = canonical()
    rows, _ = ns.parse_balldontlie_nba_games(_bdl_payload([game()]), observed_at=NOW)
    ns.apply_nba_schedule_time_enrichment([stored], rows, now=NOW)
    incoming = {**canonical(), "time": "09:00", "kickoff_time_known": True}
    updated = resolve_match_update(deepcopy(stored), incoming)
    assert updated["time"] == stored["time"]
    assert updated["kickoff_provenance"] == stored["kickoff_provenance"]


@pytest.mark.parametrize("scenario", ["missing_credentials", "network_disabled", "unavailable",
                                      "late", "ambiguous", "failed_pagination", "unknown_status"])
def test_normal_refresh_fail_closed(refresh_env, monkeypatch, scenario):
    if scenario == "missing_credentials":
        monkeypatch.delenv("DSL_BDL_API_KEY")
    elif scenario == "network_disabled":
        monkeypatch.setattr(config, "ENABLE_NBA_SCHEDULE_NETWORK", False)
    elif scenario == "unavailable":
        monkeypatch.setattr(ns.requests, "get", lambda *a, **k: Response(code=503))
    elif scenario == "failed_pagination":
        responses = [Response(_bdl_payload([game()], 1)), Response(code=503)]
        monkeypatch.setattr(ns.requests, "get", lambda *a, **k: responses.pop(0))
    elif scenario == "ambiguous":
        monkeypatch.setattr(ns.requests, "get", lambda *a, **k: Response(_bdl_payload([game(), game(2)])))
    elif scenario == "unknown_status":
        monkeypatch.setattr(ns.requests, "get", lambda *a, **k: Response(_bdl_payload([game(status_state="unknown")])))
    with override_beijing_now(NOW + timedelta(hours=2) if scenario == "late" else NOW):
        result = scraper.refresh(verbose=False)
    assert result["nba_schedule_enriched"] == result["prediction_snapshots_added"] == 0
    assert daily_loader.load_json("prediction_snapshots.json")["snapshots"] == {}
    if scenario == "failed_pagination":
        assert result["nba_schedule_diagnostics"]["coverage_status"] == "PAGINATION_INCOMPLETE"
