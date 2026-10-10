import os
import sys
import time
import json
import threading
import subprocess
from unittest import mock
import pytest
import errno
import urllib.request
import requests

import config
from app import app, start_background_scraper
from utils import scraper
from utils.concurrency import acquire_refresh_lock, RefreshBusyError, _THREAD_GUARDS
from utils.daily_loader import save_json, load_json, _path
from utils.atomic_json import atomic_write_json
from utils import team_strength
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match
from utils.odds_snapshots import get_odds_history_for_match, record_odds_snapshot
from utils.settlements import get_settlements_for_match, settle_snapshot
from utils.evaluation_rows import get_all_evaluation_rows, capture_evaluation_row
from utils.match_lifecycle import valid_full_time_score

class NetworkViolationError(RuntimeError):
    pass

@pytest.fixture(autouse=True)
def guard_network(monkeypatch, request):
    """BE-05: Fail-Closed Network Guard"""
    violations = []

    def block_network(*args, **kwargs):
        violations.append("Network access attempted!")
        raise NetworkViolationError("Network access attempted!")

    monkeypatch.setattr(requests, "get", block_network)
    monkeypatch.setattr(requests, "post", block_network)
    monkeypatch.setattr(requests.Session, "get", block_network)
    monkeypatch.setattr(requests.Session, "post", block_network)
    monkeypatch.setattr(urllib.request, "urlopen", block_network)

    yield

    assert not violations, f"Network Guard caught violations: {violations}"

@pytest.fixture(autouse=True)
def mock_all_providers(monkeypatch):
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda *a, **kw: [])

def run_in_subprocess(data_dir, script_code):
    cmd = [sys.executable, "-c", script_code]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# BE-07: AC-T01 HTTP 200/409 preserved
def test_t01_simultaneous_http_requests(isolated_data_dir):
    client = app.test_client()
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    results, exceptions = [], []

    original_refresh = scraper._refresh_impl
    def mocked_refresh(*args, **kwargs):
        barrier_entry.wait(timeout=5)
        barrier_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        def worker1():
            try: results.append(client.post("/api/refresh").status_code)
            except Exception as e: exceptions.append(e)

        def worker2():
            try:
                barrier_entry.wait(timeout=5)
                results.append(client.post("/api/refresh").status_code)
                barrier_exit.wait(timeout=5)
            except Exception as e: exceptions.append(e)

        t1, t2 = threading.Thread(target=worker1), threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not t1.is_alive() and not t2.is_alive()
        assert not exceptions
        assert sorted(results) == [200, 409]


# BE-07: AC-T02 Actual scheduler busy handling preserved
def test_t02_http_and_scheduler_overlap(isolated_data_dir, monkeypatch):
    client = app.test_client()
    job_func = [None]
    def mock_thread(target, daemon=False):
        job_func[0] = target
        return mock.Mock()

    with mock.patch("threading.Thread", side_effect=mock_thread):
        with mock.patch("config.DEBUG", False):
            start_background_scraper()

    job = job_func[0]
    assert job is not None

    scheduler_entry, scheduler_exit = threading.Event(), threading.Event()
    def mocked_refresh_a(*args, **kwargs):
        scheduler_entry.set()
        scheduler_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    class BreakLoop(Exception): pass
    def mock_sleep_a(secs):
        if secs == config.SCRAPE_INTERVAL_SECONDS: raise BreakLoop()

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh_a):
        with mock.patch("time.sleep", side_effect=mock_sleep_a):
            def run_scheduler_a():
                try: job()
                except BreakLoop: pass
            t1 = threading.Thread(target=run_scheduler_a)
            t1.start()
            assert scheduler_entry.wait(timeout=5)
            resp = client.post("/api/refresh")
            assert resp.status_code == 409
            scheduler_exit.set()
            t1.join(timeout=5)
            assert not t1.is_alive()

    http_entry, http_exit = threading.Event(), threading.Event()
    def mocked_refresh_b(*args, **kwargs):
        http_entry.set()
        http_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh_b):
        def http_job(): client.post("/api/refresh")
        t2 = threading.Thread(target=http_job)
        t2.start()
        assert http_entry.wait(timeout=5)
        with mock.patch("builtins.print") as mock_print:
            with mock.patch("time.sleep", side_effect=mock_sleep_a):
                try: job()
                except BreakLoop: pass
            # We just verify it executes without crashing on busy
        http_exit.set()
        t2.join(timeout=5)
        assert not t2.is_alive()


# BE-02: Bound All Subprocess Handshakes (AC-T03)
def test_t03_cross_process_same_data_dir(isolated_data_dir):
    ready_file = str(isolated_data_dir / "ready.txt")
    script = f"""# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"

def block(*args, **kwargs):
    print("NET_VIOLATION")
    sys.exit(2)
import urllib.request, requests
urllib.request.urlopen = block
requests.get = block
requests.post = block
requests.Session.get = block

from utils.concurrency import acquire_refresh_lock, RefreshBusyError
try:
    with acquire_refresh_lock():
        pass
    print("SUCCESS")
except RefreshBusyError:
    print("BUSY")
    sys.exit(1)
"""
    script_path = str(isolated_data_dir / "worker.py")
    with open(script_path, "w", encoding="utf-8") as f: f.write(script)

    hold_script = f"""# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    with open(r"{ready_file}", "w") as f: f.write("READY")
    time.sleep(15)
"""
    p1 = run_in_subprocess(str(isolated_data_dir), hold_script)
    try:
        # Bounded wait for ready_file
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(ready_file): break
            time.sleep(0.1)
        assert os.path.exists(ready_file)

        p2 = run_in_subprocess(str(isolated_data_dir), script)
        try:
            out, err = p2.communicate(timeout=5)
            assert p2.returncode == 1
            assert "BUSY" in out
            assert "NET_VIOLATION" not in out
        finally:
            if p2.poll() is None:
                p2.terminate()
                p2.wait(timeout=2)
    finally:
        p1.terminate()
        p1.wait(timeout=5)


# BE-02: Bound All Subprocess Handshakes (AC-T04)
def test_t04_cross_process_different_data_dir(tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    r1 = d1 / "ready.txt"
    r2 = d2 / "ready.txt"
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)

    def make_script(d, r, wait_time):
        return f"""# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(d)}"

def block(*args, **kwargs):
    print("NET_VIOLATION")
    sys.exit(2)
import urllib.request, requests
urllib.request.urlopen = block
requests.get = block
requests.post = block
requests.Session.get = block

from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    with open(r"{str(r)}", "w") as f: f.write("READY")
    time.sleep({wait_time})
"""
    p1 = run_in_subprocess(str(d1), make_script(d1, r1, 10))
    p2 = run_in_subprocess(str(d2), make_script(d2, r2, 2))
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(r1) and os.path.exists(r2): break
            time.sleep(0.1)
        assert os.path.exists(r1) and os.path.exists(r2)

        # p2 will exit in 2 seconds, p1 holds for 10
        out2, err2 = p2.communicate(timeout=5)
        assert p2.returncode == 0
    finally:
        p1.terminate()
        p1.wait(timeout=5)


# BE-01: AC-T05 Restore Exception-Release Test
def test_t05_exception_releases_held_lock(isolated_data_dir):
    class InjectionError(Exception): pass

    original_refresh = scraper._refresh_impl
    def mocked_refresh(*args, **kwargs):
        raise InjectionError("Injected inside protected body")

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        with pytest.raises(InjectionError):
            scraper.refresh()

    # Verify another thread can acquire
    results = []
    def acq_thread():
        try:
            with acquire_refresh_lock():
                results.append("SUCCESS")
        except Exception:
            pass
    t = threading.Thread(target=acq_thread)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    assert results == ["SUCCESS"]

    # Verify separate process can acquire
    script = f"""# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
def block(*args, **kwargs): pass
import urllib.request, requests
urllib.request.urlopen = block
requests.get = block
requests.post = block
requests.Session.get = block
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    print("SUCCESS")
"""
    p = run_in_subprocess(str(isolated_data_dir), script)
    out, err = p.communicate(timeout=2)
    assert p.returncode == 0
    assert "SUCCESS" in out


# BE-02: AC-T06 Bound Subprocess Handshakes
def test_t06_process_crash_releases_lock(isolated_data_dir):
    ready_file = str(isolated_data_dir / "ready.txt")
    script = f"""# -*- coding: utf-8 -*-
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"

def block(*args, **kwargs): pass
import urllib.request, requests
urllib.request.urlopen = block
requests.get = block
requests.post = block
requests.Session.get = block

from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    with open(r"{ready_file}", "w") as f: f.write("READY")
    os._exit(1)  # Hard crash
"""
    p1 = run_in_subprocess(str(isolated_data_dir), script)
    try:
        p1.communicate(timeout=5)
        assert os.path.exists(ready_file)
    finally:
        if p1.poll() is None: p1.terminate(); p1.wait(timeout=2)

    with acquire_refresh_lock():
        pass


# BE-07: AC-T07 Process-local thread exclusion preserved
def test_t07_coordinator_edge_cases(isolated_data_dir):
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    results = []
    def worker1():
        try:
            with acquire_refresh_lock():
                barrier_entry.wait(timeout=5)
                barrier_exit.wait(timeout=5)
                results.append("SUCCESS")
        except RefreshBusyError: results.append("BUSY")
    def worker2():
        barrier_entry.wait(timeout=5)
        try:
            with acquire_refresh_lock(): results.append("SUCCESS")
        except RefreshBusyError: results.append("BUSY")
        barrier_exit.wait(timeout=5)
    t1, t2 = threading.Thread(target=worker1), threading.Thread(target=worker2)
    t1.start(); t2.start()
    t1.join(timeout=5); t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()
    assert sorted(results) == ["BUSY", "SUCCESS"]

    with mock.patch("os.open", side_effect=OSError(errno.EIO, "I/O Error")):
        with pytest.raises(OSError, match="I/O Error"):
            with acquire_refresh_lock(): pass

    with mock.patch("os.close", side_effect=OSError(errno.EIO, "Close Error")):
        with pytest.raises(OSError, match="Close Error"):
            with acquire_refresh_lock(): pass
    with acquire_refresh_lock(): pass


# BE-07: AC-T08 Three atomic replacement boundaries preserved
def test_t08_atomic_replacement_failure_boundaries(isolated_data_dir):
    target = "test.json"
    save_json(target, {"v": 1})
    class FailDump:
        def __dict__(self): raise ValueError("Serialization Failed")
    with pytest.raises(TypeError):
        save_json(target, {"v": FailDump()})
    assert load_json(target) == {"v": 1}
    assert not any(f.startswith(".tmp-") for f in os.listdir(isolated_data_dir))

    with mock.patch("os.replace", side_effect=PermissionError("Locked")):
        with pytest.raises(PermissionError):
            save_json(target, {"v": 2})
    assert load_json(target) == {"v": 1}
    assert not any(f.startswith(".tmp-") for f in os.listdir(isolated_data_dir))

    original_replace = os.replace
    def mock_replace(src, dst):
        original_replace(src, dst)
        raise OSError("Failed right after replace")
    with mock.patch("os.replace", side_effect=mock_replace):
        with pytest.raises(OSError):
            save_json(target, {"v": 3})
    assert load_json(target) == {"v": 3}


# BE-06: AC-T09 Deterministic Atomic Reader Concurrency
def test_t09_concurrent_canonical_readers_under_atomic_writes(isolated_data_dir):
    barrier = threading.Barrier(2)
    writer_errs, reader_errs = [], []
    save_json("test.json", {"v": -1})

    def writer():
        barrier.wait(timeout=5)
        for i in range(100):
            try: save_json("test.json", {"v": i})
            except PermissionError: pass
            except Exception as e: writer_errs.append(e)

    def reader():
        barrier.wait(timeout=5)
        for _ in range(300):
            try:
                data = load_json("test.json")
                if data: assert "v" in data
            except (PermissionError, FileNotFoundError): pass
            except Exception as e: reader_errs.append(e)

    t_writer, t_reader = threading.Thread(target=writer), threading.Thread(target=reader)
    t_writer.start(); t_reader.start()
    t_writer.join(timeout=5); t_reader.join(timeout=5)

    assert not t_writer.is_alive() and not t_reader.is_alive()
    assert not writer_errs and not reader_errs
    assert "v" in load_json("test.json")


# BE-03: AC-T10 Verify Exact Elo Results
def test_t10_fresh_elo_contention(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="finished", score={"ft": [2, 0]})
    assert valid_full_time_score(m.get("score"))

    assert m["id"] not in load_json("calibrated.json").get("ids", [])

    prof_home = team_strength.get_team_profile(m["home"])
    prof_away = team_strength.get_team_profile(m["away"])
    elo_initial_home = prof_home["elo_rating"]
    elo_initial_away = prof_away["elo_rating"]
    match_count_home = prof_home.get("matches_played", 0)
    match_count_away = prof_away.get("matches_played", 0)

    # Calculate expected Elo precisely
    import math
    from utils.team_strength import expected_result

    ha = config.MODEL_CONFIG["home_advantage_elo"]
    exp_h = expected_result(elo_initial_home + ha, elo_initial_away)

    goal_diff = 2
    multiplier = 1.0 + math.log1p(goal_diff) * 0.4
    delta = 20.0 * multiplier * (1.0 - exp_h)

    exp_final_home = round(elo_initial_home + delta, 1)
    exp_final_away = round(elo_initial_away - delta, 1)

    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [m])

    entry_event, exit_event = threading.Event(), threading.Event()
    original_update = team_strength.update_from_result
    calib_count = [0]

    def mocked_update(*args, **kwargs):
        calib_count[0] += 1
        entry_event.set()
        exit_event.wait(timeout=5)
        return original_update(*args, **kwargs)

    with mock.patch("builtins.print"), mock.patch("utils.scraper.update_from_result", side_effect=mocked_update):
        results = []
        def first_refresh():
            try: scraper.refresh(); results.append("R1_SUCCESS")
            except Exception as e: results.append(e)

        t1 = threading.Thread(target=first_refresh)
        t1.start()
        assert entry_event.wait(timeout=5)

        def second_refresh():
            try: scraper.refresh(); results.append("R2_SUCCESS")
            except RefreshBusyError: results.append("R2_BUSY")
            except Exception as e: results.append(e)

        t2 = threading.Thread(target=second_refresh)
        t2.start()
        t2.join(timeout=5)
        assert not t2.is_alive()

        exit_event.set()
        t1.join(timeout=5)
        assert not t1.is_alive()

        assert "R2_BUSY" in results and "R1_SUCCESS" in results
        assert calib_count[0] == 1

        calibrated_ids = load_json("calibrated.json").get("ids", [])
        assert calibrated_ids.count(m["id"]) == 1

        prof_home_after = team_strength.get_team_profile(m["home"])
        prof_away_after = team_strength.get_team_profile(m["away"])

        assert prof_home_after["elo_rating"] == exp_final_home
        assert prof_away_after["elo_rating"] == exp_final_away
        assert prof_home_after.get("matches_played", 0) == match_count_home + 1
        assert prof_away_after.get("matches_played", 0) == match_count_away + 1

        calib_count[0] = 0
        scraper.refresh()
        assert calib_count[0] == 0
        assert team_strength.get_team_profile(m["home"])["elo_rating"] == exp_final_home
        assert team_strength.get_team_profile(m["home"]).get("matches_played", 0) == match_count_home + 1


# BE-04: AC-T11 Strengthen Four-Store Immutable Evidence
def test_t11_immutable_evidence_preservation(isolated_data_dir, make_match, monkeypatch):
    m = make_match(id="old-m1", status="upcoming", odds={"home_win": 1.5})

    ensure_snapshot(m, {"home_win": 0.5})
    snap = get_snapshots_for_match(m["id"])[0]
    record_odds_snapshot(m)
    m_fin = make_match(id=m["id"], status="finished", score={"ft": [2, 0]})
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m_fin]})
    settle_snapshot(snap, m_fin)
    sett = get_settlements_for_match(m["id"])[0]
    capture_evaluation_row(snap, sett)

    def extract_id_map(items, id_key):
        return {item[id_key]: json.dumps(item, sort_keys=True) for item in items}

    map_snaps_before = extract_id_map(get_snapshots_for_match(m["id"]), "snapshot_id")
    map_odds_before = extract_id_map(get_odds_history_for_match(m["id"]), "snapshot_id")
    map_sett_before = extract_id_map(get_settlements_for_match(m["id"]), "settlement_id")
    map_eval_before = extract_id_map(get_all_evaluation_rows(), "evaluation_id")

    assert len(map_snaps_before) >= 1
    assert len(map_odds_before) >= 1
    assert len(map_sett_before) >= 1
    assert len(map_eval_before) >= 1

    # New match to trigger canonical admission
    m2 = make_match(id="new-m2", home="A", away="B", status="upcoming", odds={"home_win": 2.0, "draw": 3.0, "away_win": 4.0})
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda *a, **kw: [m2])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda *a, **kw: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda *a, **kw: [])

    scraper.refresh()
    print("SNAPS AFTER:", get_snapshots_for_match(m2["id"]))

    map_snaps_after = extract_id_map(get_snapshots_for_match(m["id"]), "snapshot_id")
    map_odds_after = extract_id_map(get_odds_history_for_match(m["id"]), "snapshot_id")
    map_sett_after = extract_id_map(get_settlements_for_match(m["id"]), "settlement_id")
    map_eval_after = extract_id_map([e for e in get_all_evaluation_rows() if e.get("match_id") == m["id"]], "evaluation_id")

    # Verify existing retained and unchanged
    for k, v in map_snaps_before.items(): assert map_snaps_after[k] == v
    for k, v in map_odds_before.items(): assert map_odds_after[k] == v
    for k, v in map_sett_before.items(): assert map_sett_after[k] == v
    for k, v in map_eval_before.items(): assert map_eval_after[k] == v

    # Assert duplicate IDs = 0
    all_evals = get_all_evaluation_rows()
    assert len([e["evaluation_id"] for e in all_evals]) == len(set([e["evaluation_id"] for e in all_evals]))

    # Assert new canonical admission demonstrated
    assert len(get_snapshots_for_match(m2["id"])) > 0


# BE-07: AC-T12 Independent writer residual-risk exposure preserved
def test_t12_independent_writer_boundary(isolated_data_dir, make_match):
    m = make_match()
    entry_event, exit_event = threading.Event(), threading.Event()
    def holder():
        with acquire_refresh_lock():
            entry_event.set()
            exit_event.wait(timeout=5)
    t1 = threading.Thread(target=holder)
    t1.start()
    assert entry_event.wait(timeout=5)
    ensure_snapshot(m, {"home_win": 0.8})
    assert len(get_snapshots_for_match(m["id"])) == 1
    exit_event.set()
    t1.join(timeout=5)
    assert not t1.is_alive()


# BE-07: AC-T13 All seven application stores unchanged on Busy preserved
def test_t13_no_writes_on_busy(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="upcoming", odds={"home_win": 1.5})
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m]})
    team_strength.get_team_profile("Test")
    save_json("calibrated.json", {"ids": []})
    ensure_snapshot(m, {"home_win": 0.5})
    snap = get_snapshots_for_match(m["id"])[0]
    record_odds_snapshot(m)
    m_fin = make_match(id=m["id"], status="finished", score={"ft": [2, 0]})
    settle_snapshot(snap, m_fin)
    sett = get_settlements_for_match(m["id"])[0]
    capture_evaluation_row(snap, sett)

    def fp_file(name):
        try: return json.dumps(load_json(name), sort_keys=True)
        except FileNotFoundError: return None

    files = [
        "daily_matches.json", "team_strength.json", "calibrated.json",
        "prediction_snapshots.json", "odds_snapshots.json", "settlements.json",
        "evaluation_rows.json"
    ]
    fps_before = {f: fp_file(f) for f in files}

    with acquire_refresh_lock():
        client = app.test_client()
        resp = client.post("/api/refresh")
        assert resp.status_code == 409

    fps_after = {f: fp_file(f) for f in files}
    for f in files: assert fps_before[f] == fps_after[f]
