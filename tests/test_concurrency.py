import os
import sys
import time
import json
import threading
import subprocess
import concurrent.futures
from unittest import mock
import pytest
import errno

import config
from app import app
from utils import scraper
from utils.concurrency import acquire_refresh_lock, RefreshBusyError
from utils.daily_loader import save_json, load_json
from utils.atomic_json import atomic_write_json
from utils import team_strength
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match
from utils.evaluation_rows import get_all_evaluation_rows

# BC-01: Global network isolation for all synthetic refresh tests
@pytest.fixture(autouse=True)
def guard_network(monkeypatch):
    """Fails the test if any real network boundary is accessed."""
    def block_network(*args, **kwargs):
        raise RuntimeError("Test attempted to access the network!")
    monkeypatch.setattr("utils.fetcher_500.requests.get", block_network)
    monkeypatch.setattr("urllib.request.urlopen", block_network)
    
@pytest.fixture
def mock_all_providers(monkeypatch):
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

def run_in_subprocess(data_dir, script_code):
    cmd = [sys.executable, "-c", script_code]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# BC-03 Deterministic HTTP Contention
def test_t01_simultaneous_http_requests(isolated_data_dir, mock_all_providers):
    client = app.test_client()
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    
    original_refresh = scraper._refresh_impl
    
    def mocked_refresh(*args, **kwargs):
        barrier_entry.wait(timeout=2)
        barrier_exit.wait(timeout=2)
        return {"added": 0, "total": 0}
        
    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        results = []
        def worker1():
            results.append(client.post("/api/refresh").status_code)
            
        def worker2():
            # Wait for worker1 to enter the locked section
            barrier_entry.wait(timeout=2)
            results.append(client.post("/api/refresh").status_code)
            barrier_exit.wait(timeout=2)
            
        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        # We must see one 200 and one 409
        assert sorted(results) == [200, 409]


# BC-04 Both Scheduler/HTTP Directions
def test_t02_http_and_scheduler_overlap(isolated_data_dir, mock_all_providers):
    client = app.test_client()
    
    # Scenario A: Scheduler holds, HTTP gets 409
    event_scheduler_holding = threading.Event()
    event_scheduler_release = threading.Event()
    
    def scheduler_job():
        with acquire_refresh_lock():
            event_scheduler_holding.set()
            event_scheduler_release.wait(timeout=2)
            
    t1 = threading.Thread(target=scheduler_job)
    t1.start()
    
    assert event_scheduler_holding.wait(timeout=2)
    resp1 = client.post("/api/refresh")
    assert resp1.status_code == 409
    assert resp1.get_json()["status"] == "busy"
    
    event_scheduler_release.set()
    t1.join()
    
    # Scenario B: HTTP holds, Scheduler skips (by throwing RefreshBusyError)
    event_http_holding = threading.Event()
    event_http_release = threading.Event()
    
    original_refresh = scraper._refresh_impl
    def mocked_refresh(*args, **kwargs):
        event_http_holding.set()
        event_http_release.wait(timeout=2)
        return {"added": 0, "total": 0}
        
    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        def http_job():
            client.post("/api/refresh")
            
        t2 = threading.Thread(target=http_job)
        t2.start()
        
        assert event_http_holding.wait(timeout=2)
        
        # Now run scheduler (which wraps _refresh_impl without lock inside it? 
        # No, scheduler calls scraper.refresh() directly which locks.
        # But wait! scraper.refresh() is locked, but since _refresh_impl is mocked,
        # calling scraper.refresh() will try to acquire lock and fail.
        with pytest.raises(scraper.RefreshBusyError):
            scraper.refresh()
            
        event_http_release.set()
        t2.join()


# BC-05 Cross-Process Test Lifecycle
def test_t03_cross_process_same_data_dir(isolated_data_dir):
    script = f"""
import os, sys
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
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
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
        
    hold_script = f"""
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    print("READY")
    sys.stdout.flush()
    time.sleep(10)
"""
    p1 = run_in_subprocess(str(isolated_data_dir), hold_script)
    line = p1.stdout.readline()
    assert "READY" in line
    
    p2 = run_in_subprocess(str(isolated_data_dir), script)
    out, err = p2.communicate(timeout=2)
    
    p1.terminate()
    p1.wait(timeout=2)
    
    assert p2.returncode == 1
    assert "BUSY" in out


def test_t04_cross_process_different_data_dir(tmp_path):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    
    def make_script(d):
        return f"""
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(d)}"
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    print("READY")
    sys.stdout.flush()
    time.sleep(2)
"""
    p1 = run_in_subprocess(str(d1), make_script(d1))
    line1 = p1.stdout.readline()
    assert "READY" in line1
    
    p2 = run_in_subprocess(str(d2), make_script(d2))
    line2 = p2.stdout.readline()
    assert "READY" in line2
    
    p1.wait(timeout=5)
    p2.wait(timeout=5)
    assert p1.returncode == 0
    assert p2.returncode == 0


def test_t05_exception_releases_held_lock(isolated_data_dir):
    with pytest.raises(ValueError):
        with acquire_refresh_lock():
            raise ValueError("Test")
            
    # Should acquire fine now
    with acquire_refresh_lock():
        pass


def test_t06_process_crash_releases_lock(isolated_data_dir):
    script = f"""
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    print("READY")
    sys.stdout.flush()
    os._exit(1)  # Hard crash
"""
    p1 = run_in_subprocess(str(isolated_data_dir), script)
    out, err = p1.communicate(timeout=2)
    assert "READY" in out
    
    # Should acquire fine now in this process
    with acquire_refresh_lock():
        pass


def test_t07_same_process_thread_contention(isolated_data_dir):
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    results = []
    
    def worker1():
        try:
            with acquire_refresh_lock():
                barrier_entry.wait(timeout=2)
                barrier_exit.wait(timeout=2)
                results.append("SUCCESS")
        except RefreshBusyError:
            results.append("BUSY")
            
    def worker2():
        barrier_entry.wait(timeout=2)
        try:
            with acquire_refresh_lock():
                results.append("SUCCESS")
        except RefreshBusyError:
            results.append("BUSY")
        barrier_exit.wait(timeout=2)
            
    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    assert sorted(results) == ["BUSY", "SUCCESS"]


# BC-06 Atomic Replacement Failure Boundaries
def test_t08_atomic_replacement_failure_boundaries(isolated_data_dir):
    target = isolated_data_dir / "test.json"
    save_json("test.json", {"v": 1})
    
    # Mock os.replace to fail
    with mock.patch("os.replace", side_effect=OSError("Injected replace failure")):
        with pytest.raises(OSError):
            save_json("test.json", {"v": 2})
            
    # Original file is fully intact
    assert load_json("test.json") == {"v": 1}


# BC-07 Canonical Reader Concurrency
def test_t09_concurrent_canonical_readers_under_atomic_writes(isolated_data_dir):
    stop = False
    
    def writer():
        for i in range(50):
            try:
                save_json("test.json", {"v": i})
            except PermissionError:
                # Windows might lock momentarily during replace
                pass
            time.sleep(0.01)
            
    def reader():
        for _ in range(50):
            try:
                data = load_json("test.json")
                if data:
                    assert "v" in data
            except (PermissionError, FileNotFoundError):
                pass
            except json.JSONDecodeError:
                assert False, "Observed truncated JSON"
            time.sleep(0.01)

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()
    t_writer.join(timeout=2)
    t_reader.join(timeout=2)


# BC-02 Real Elo Concurrency Test
def test_t10_elo_double_application_prevention(isolated_data_dir, make_match, monkeypatch):
    from utils.match_lifecycle import valid_full_time_score
    m = make_match(status="finished", score={"ft": [2, 0]})
    assert valid_full_time_score(m.get("score"))
    
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m]})
    
    # Setup network mocks
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [m])
    
    # Verify starting Elo
    elo_initial = team_strength.get_team_profile(m["home"])["elo_rating"]
    
    # Run refresh exactly once
    scraper.refresh()
    elo_after_one = team_strength.get_team_profile(m["home"])["elo_rating"]
    assert elo_after_one > elo_initial
    
    # Run refresh again sequentially
    scraper.refresh()
    elo_after_two = team_strength.get_team_profile(m["home"])["elo_rating"]
    assert elo_after_two == elo_after_one # No double apply
    
    # Test concurrency: ensure the second thread gets rejected and doesn't mutate
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    
    # We must patch scraper._refresh_impl to block during calibration
    original_refresh = scraper._refresh_impl
    def mocked_refresh(*args, **kwargs):
        barrier_entry.wait(timeout=2)
        res = original_refresh(*args, **kwargs)
        barrier_exit.wait(timeout=2)
        return res
        
    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        results = []
        def worker1():
            try:
                scraper.refresh()
                results.append("SUCCESS")
            except RefreshBusyError:
                pass
                
        def worker2():
            barrier_entry.wait(timeout=2)
            try:
                scraper.refresh()
                results.append("SUCCESS")
            except RefreshBusyError:
                results.append("BUSY")
            barrier_exit.wait(timeout=2)
            
        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        assert sorted(results) == ["BUSY", "SUCCESS"]
        elo_after_conc = team_strength.get_team_profile(m["home"])["elo_rating"]
        assert elo_after_conc == elo_after_one


# BC-08 Immutable Evidence Preservation
def test_t11_immutable_evidence_preservation(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="upcoming")
    ensure_snapshot(m, {"home_win": 0.5})
    
    def fingerprint(data):
        return json.dumps(data, sort_keys=True)
        
    snaps_before = get_snapshots_for_match(m["id"])
    fp_before = fingerprint(snaps_before)
    
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])
    
    scraper.refresh()
    
    snaps_after = get_snapshots_for_match(m["id"])
    fp_after = fingerprint(snaps_after)
    
    assert len(snaps_before) > 0
    assert fp_before == fp_after


def test_t12_independent_writer_boundary(isolated_data_dir, make_match):
    m = make_match()
    # verify ensure_snapshot doesn't throw BusyError while refresh runs
    with acquire_refresh_lock():
        ensure_snapshot(m, {"home_win": 0.8})
    # It succeeds without locking


# BC-10 No Writes on Busy
def test_t13_no_writes_on_busy(isolated_data_dir, monkeypatch):
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": []})
    
    def fingerprint_file(name):
        try:
            return json.dumps(load_json(name), sort_keys=True)
        except FileNotFoundError:
            return None
            
    fp_daily = fingerprint_file("daily_matches.json")
    fp_team = fingerprint_file("team_strength.json")
    fp_pred = fingerprint_file("prediction_snapshots.json")
    
    with acquire_refresh_lock():
        client = app.test_client()
        resp = client.post("/api/refresh")
        assert resp.status_code == 409
        
    assert fingerprint_file("daily_matches.json") == fp_daily
    assert fingerprint_file("team_strength.json") == fp_team
    assert fingerprint_file("prediction_snapshots.json") == fp_pred


# BC-11 Coordinator Edge Cases
def test_t16_coordinator_edge_cases(isolated_data_dir):
    # Lock contention classification
    with acquire_refresh_lock():
        with pytest.raises(RefreshBusyError):
            with acquire_refresh_lock():
                pass

    # Lockfile unexpected I/O error
    # We patch os.open to raise a generic OSError (not EACCES/EDEADLOCK)
    with mock.patch("os.open", side_effect=OSError(errno.EIO, "I/O Error")):
        with pytest.raises(OSError):
            with acquire_refresh_lock():
                pass
