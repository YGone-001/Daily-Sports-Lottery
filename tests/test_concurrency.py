import os
import sys
import time
import json
import threading
import subprocess
import concurrent.futures
from unittest import mock
import pytest

from app import app
from utils import scraper
import config
from utils.concurrency import acquire_refresh_lock, RefreshBusyError
from utils.daily_loader import save_json, load_json
from utils.atomic_json import atomic_write_json
from utils import team_strength
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match

def run_in_subprocess(data_dir, script_code):
    cmd = [sys.executable, "-c", script_code]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_t01_simultaneous_http_requests(isolated_data_dir):
    client = app.test_client()
    barrier = threading.Barrier(2)
    
    # Mock refresh to wait on barrier then sleep a bit
    original_refresh = scraper._refresh_impl
    
    def mocked_refresh(*args, **kwargs):
        # barrier not needed here, we just hold it slowly
        time.sleep(1)
        return {"added": 0, "total": 0}
        
    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        results = []
        def worker():
            results.append(client.post("/api/refresh").status_code)
            
        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        assert 200 in results
        assert 409 in results

def test_t02_http_and_scheduler_overlap(isolated_data_dir):
    client = app.test_client()
    barrier = threading.Barrier(2)
    
    def background_job():
        with acquire_refresh_lock():
            barrier.wait()
            time.sleep(0.5)
            
    t = threading.Thread(target=background_job)
    t.start()
    
    barrier.wait()
    resp = client.post("/api/refresh")
    assert resp.status_code == 409
    t.join()


def test_t03_cross_process_same_data_dir(isolated_data_dir):
    script = f"""
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
from utils.scraper import refresh
import utils.fetcher_500
utils.fetcher_500.fetch_live_basketball = lambda: []
utils.fetcher_500.fetch_live_matches = lambda: []
from utils.concurrency import RefreshBusyError

try:
    refresh()
    print("SUCCESS")
except RefreshBusyError:
    print("BUSY")
    sys.exit(1)
"""
    # Write script to tmp
    script_path = str(isolated_data_dir / "worker.py")
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(script)
        
    # We need one process to hold the lock
    hold_script = f"""
import os, sys, time
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"
from utils.concurrency import acquire_refresh_lock
with acquire_refresh_lock():
    print("READY")
    sys.stdout.flush()
    time.sleep(2)
"""
    p1 = run_in_subprocess(str(isolated_data_dir), hold_script)
    # Wait for ready
    p1.stdout.readline()
    
    p2 = run_in_subprocess(str(isolated_data_dir), script)
    out, err = p2.communicate()
    p1.terminate()
    
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
    time.sleep(1)
"""
    p1 = run_in_subprocess(str(d1), make_script(d1))
    p1.stdout.readline()
    
    p2 = run_in_subprocess(str(d2), make_script(d2))
    p2.stdout.readline()
    
    p1.wait()
    p2.wait()
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
    p1.communicate()
    
    # Should acquire fine now in this process
    with acquire_refresh_lock():
        pass


def test_t07_same_process_thread_contention(isolated_data_dir):
    barrier = threading.Barrier(2)
    results = []
    
    def worker():
        try:
            with acquire_refresh_lock():
                barrier.wait()
                time.sleep(0.5)
                results.append("SUCCESS")
        except RefreshBusyError:
            barrier.wait()
            results.append("BUSY")
            
    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    
    assert "SUCCESS" in results
    assert "BUSY" in results


def test_t08_atomic_replacement_failure_boundaries(isolated_data_dir):
    target = isolated_data_dir / "test.json"
    save_json("test.json", {"v": 1})
    
    # Mock os.replace to fail
    with mock.patch("os.replace", side_effect=OSError("Injected replace failure")):
        with pytest.raises(OSError):
            save_json("test.json", {"v": 2})
            
    # Should still be old valid JSON
    assert load_json("test.json") == {"v": 1}
    

def test_t09_concurrent_canonical_readers_under_atomic_writes(isolated_data_dir):
    stop = False
    
    def writer():
        i = 0
        while not stop:
            try:
                save_json("test.json", {"v": i})
            except PermissionError:
                pass
            i += 1
            
    def reader():
        while not stop:
            try:
                data = load_json("test.json")
                if data:
                    assert "v" in data
            except (PermissionError, FileNotFoundError):
                pass
            except json.JSONDecodeError:
                assert False, "Observed truncated JSON"


    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()
    time.sleep(1)
    stop = True
    t_writer.join()
    t_reader.join()


def test_t10_elo_double_application_prevention(isolated_data_dir, make_match, monkeypatch):
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    m = make_match(status="finished", home_score=2, away_score=0)
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m]})
    
    # Two refreshes sequentially shouldn't double apply
    scraper.refresh()
    elo1 = team_strength.get_team_profile("曼城")["elo_rating"]
    
    scraper.refresh()
    elo2 = team_strength.get_team_profile("曼城")["elo_rating"]
    
    assert elo1 == elo2


def test_t11_immutable_evidence_preservation(isolated_data_dir, make_match, monkeypatch):
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    m = make_match(status="upcoming")
    ensure_snapshot(m, {"home_win": 0.5})
    snaps_before = get_snapshots_for_match(m["id"])
    
    scraper.refresh()
    
    snaps_after = get_snapshots_for_match(m["id"])
    assert len(snaps_before) == len(snaps_after)


def test_t12_independent_writer_boundary(isolated_data_dir, make_match):
    m = make_match()
    # verify ensure_snapshot doesn't throw BusyError while refresh runs
    with acquire_refresh_lock():
        ensure_snapshot(m, {"home_win": 0.8})
    # It succeeds without locking


def test_t13_no_writes_on_busy(isolated_data_dir):
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": []})
    
    with acquire_refresh_lock():
        client = app.test_client()
        resp = client.post("/api/refresh")
        assert resp.status_code == 409
        
    data = load_json("daily_matches.json")
    assert "2030-01-01" in data["dates"]
