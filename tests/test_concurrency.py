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
from app import app, start_background_scraper
from utils import scraper
from utils.concurrency import acquire_refresh_lock, RefreshBusyError, _THREAD_GUARDS
from utils.daily_loader import save_json, load_json, _path
from utils.atomic_json import atomic_write_json
from utils import team_strength
from utils.prediction_snapshots import ensure_snapshot, get_snapshots_for_match
from utils.odds_snapshots import get_odds_history_for_match
from utils.settlements import get_settlements_for_match
from utils.evaluation_rows import get_all_evaluation_rows, build_evaluation_row
from utils.match_lifecycle import valid_full_time_score

class NetworkViolationError(RuntimeError):
    pass

@pytest.fixture(autouse=True)
def guard_network(monkeypatch):
    """BD-09: Fail-Closed Network Isolation"""
    def block_network(*args, **kwargs):
        raise NetworkViolationError("Test attempted to access the network!")

    import requests
    monkeypatch.setattr(requests, "get", block_network)
    monkeypatch.setattr(requests, "post", block_network)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", block_network)

@pytest.fixture(autouse=True)
def mock_all_providers(monkeypatch):
    """BD-09: Mock all provider entry points."""
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

def run_in_subprocess(data_dir, script_code):
    cmd = [sys.executable, "-c", script_code]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(__file__))
    return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


# BD-03: AC-T01 Deterministic HTTP Contention
def test_t01_simultaneous_http_requests(isolated_data_dir):
    client = app.test_client()
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)

    results = []
    exceptions = []

    original_refresh = scraper._refresh_impl
    def mocked_refresh(*args, **kwargs):
        barrier_entry.wait(timeout=5)
        barrier_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh):
        def worker1():
            try:
                results.append(client.post("/api/refresh").status_code)
            except Exception as e:
                exceptions.append(e)

        def worker2():
            try:
                barrier_entry.wait(timeout=5)
                results.append(client.post("/api/refresh").status_code)
                barrier_exit.wait(timeout=5)
            except Exception as e:
                exceptions.append(e)

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not t1.is_alive()
        assert not t2.is_alive()
        assert not exceptions
        assert sorted(results) == [200, 409]


# BD-04: AC-T02 Actual Scheduler Busy Handler
def test_t02_http_and_scheduler_overlap(isolated_data_dir, monkeypatch):
    client = app.test_client()

    # Extract the actual job() function from start_background_scraper
    # We can do this by mocking threading.Thread and capturing the target
    job_func = [None]
    original_thread = threading.Thread
    def mock_thread(target, daemon=False):
        job_func[0] = target
        return mock.Mock()

    with mock.patch("threading.Thread", side_effect=mock_thread):
        with mock.patch("config.DEBUG", False):
            start_background_scraper()

    job = job_func[0]
    assert job is not None

    # Scenario A: Scheduler holds, HTTP gets 409
    scheduler_entry = threading.Event()
    scheduler_exit = threading.Event()

    def mocked_refresh_a(*args, **kwargs):
        scheduler_entry.set()
        scheduler_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    class BreakLoop(Exception):
        pass

    def mock_sleep_a(secs):
        if secs == config.SCRAPE_INTERVAL_SECONDS:
            raise BreakLoop()

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh_a):
        with mock.patch("time.sleep", side_effect=mock_sleep_a):
            def run_scheduler_a():
                try:
                    job()
                except BreakLoop:
                    pass

            t1 = threading.Thread(target=run_scheduler_a)
            t1.start()

            assert scheduler_entry.wait(timeout=5)
            resp = client.post("/api/refresh")
            assert resp.status_code == 409
            assert resp.get_json()["status"] == "busy"

            scheduler_exit.set()
            t1.join(timeout=5)
            assert not t1.is_alive()

    # Scenario B: HTTP holds, Scheduler skips
    http_entry = threading.Event()
    http_exit = threading.Event()

    def mocked_refresh_b(*args, **kwargs):
        http_entry.set()
        http_exit.wait(timeout=5)
        return {"added": 0, "total": 0}

    def mock_sleep_b(secs):
        if secs == config.SCRAPE_INTERVAL_SECONDS:
            raise BreakLoop()

    with mock.patch("utils.scraper._refresh_impl", side_effect=mocked_refresh_b):
        def http_job():
            client.post("/api/refresh")

        t2 = threading.Thread(target=http_job)
        t2.start()

        assert http_entry.wait(timeout=5)

        # Now run scheduler. It should catch RefreshBusyError and skip.
        with mock.patch("builtins.print") as mock_print:
            with mock.patch("time.sleep", side_effect=mock_sleep_b):
                try:
                    job()
                except BreakLoop:
                    pass
            # Verify the specific skip message was printed
            mock_print.assert_any_call("[Auto-Sync] 跳过: 另一个刷新进程正在运行 (RefreshBusyError)")

        http_exit.set()
        t2.join(timeout=5)
        assert not t2.is_alive()


# BD-05: AC-T03/04/06 Cross-Process Lifecycles
def test_t03_cross_process_same_data_dir(isolated_data_dir):
    script = f"""
import os, sys
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import config
config.DATA_DIR = r"{str(isolated_data_dir)}"

# Network isolation for subprocess
def block(*args, **kwargs): raise RuntimeError("Net Blocked")
import urllib.request, requests
urllib.request.urlopen = block
requests.get = block

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
    time.sleep(15)
"""
    p1 = run_in_subprocess(str(isolated_data_dir), hold_script)
    line = p1.stdout.readline()
    assert "READY" in line

    p2 = run_in_subprocess(str(isolated_data_dir), script)
    out, err = p2.communicate(timeout=5)

    p1.terminate()
    p1.wait(timeout=5)

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
    time.sleep(5)
"""
    p1 = run_in_subprocess(str(d1), make_script(d1))
    assert "READY" in p1.stdout.readline()

    p2 = run_in_subprocess(str(d2), make_script(d2))
    assert "READY" in p2.stdout.readline()

    p1.terminate()
    p2.terminate()
    p1.wait(timeout=5)
    p2.wait(timeout=5)
    # Exited via terminate, return code might be non-zero depending on OS, we just check they ran independently
    assert True


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
    out, err = p1.communicate(timeout=5)
    assert "READY" in out

    # Should acquire fine now in this process
    with acquire_refresh_lock():
        pass


# BD-10: AC-T16 Coordinator Edge Cases (Thread Contention and I/O Errors)
def test_t07_t16_coordinator_edge_cases(isolated_data_dir):
    # Same process thread contention (AC-T07)
    barrier_entry = threading.Barrier(2)
    barrier_exit = threading.Barrier(2)
    results = []

    def worker1():
        try:
            with acquire_refresh_lock():
                barrier_entry.wait(timeout=5)
                barrier_exit.wait(timeout=5)
                results.append("SUCCESS")
        except RefreshBusyError:
            results.append("BUSY")

    def worker2():
        barrier_entry.wait(timeout=5)
        try:
            with acquire_refresh_lock():
                results.append("SUCCESS")
        except RefreshBusyError:
            results.append("BUSY")
        barrier_exit.wait(timeout=5)

    t1 = threading.Thread(target=worker1)
    t2 = threading.Thread(target=worker2)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert sorted(results) == ["BUSY", "SUCCESS"]

    # Unexpected open failure
    with mock.patch("os.open", side_effect=OSError(errno.EIO, "I/O Error")):
        with pytest.raises(OSError, match="I/O Error"):
            with acquire_refresh_lock():
                pass

    # Lock release failure (simulating file close throwing OSError)
    # The lock should still release the local thread guard
    with mock.patch("os.close", side_effect=OSError(errno.EIO, "Close Error")):
        with pytest.raises(OSError, match="Close Error"):
            with acquire_refresh_lock():
                pass

    # We should still be able to acquire the lock because thread_guard was released
    with acquire_refresh_lock():
        pass


# BD-03: AC-T08 Atomic Replacement Fault Matrix
def test_t08_atomic_replacement_failure_boundaries(isolated_data_dir):
    target = "test.json"
    target_path = _path(target)
    save_json(target, {"v": 1})

    # A. Failure before replacement (json.dump throws error)
    class FailDump:
        def __dict__(self): raise ValueError("Serialization Failed")

    with pytest.raises(TypeError):
        save_json(target, {"v": FailDump()})

    assert load_json(target) == {"v": 1}
    assert not any(f.startswith(".tmp-") for f in os.listdir(isolated_data_dir))

    # B. Replacement fails (os.replace throws)
    with mock.patch("os.replace", side_effect=PermissionError("Locked")):
        with pytest.raises(PermissionError):
            save_json(target, {"v": 2})

    assert load_json(target) == {"v": 1}
    assert not any(f.startswith(".tmp-") for f in os.listdir(isolated_data_dir))

    # C. Failure immediately after successful replacement
    original_replace = os.replace
    def mock_replace(src, dst):
        original_replace(src, dst)
        raise OSError("Failed right after replace")

    with mock.patch("os.replace", side_effect=mock_replace):
        with pytest.raises(OSError):
            save_json(target, {"v": 3})

    assert load_json(target) == {"v": 3}


# BD-06: AC-T09 Canonical Reader Concurrency
def test_t09_concurrent_canonical_readers_under_atomic_writes(isolated_data_dir):
    stop = threading.Event()
    writer_errs = []
    reader_errs = []

    save_json("test.json", {"v": -1})

    def writer():
        for i in range(100):
            if stop.is_set(): break
            try:
                save_json("test.json", {"v": i})
            except PermissionError:
                # Expected Windows contention on replace
                pass
            except Exception as e:
                writer_errs.append(e)

    def reader():
        for _ in range(200):
            if stop.is_set(): break
            try:
                data = load_json("test.json")
                if data:
                    assert "v" in data
            except (PermissionError, FileNotFoundError):
                pass
            except Exception as e:
                reader_errs.append(e)

    t_writer = threading.Thread(target=writer)
    t_reader = threading.Thread(target=reader)
    t_writer.start()
    t_reader.start()

    t_writer.join(timeout=5)
    stop.set()
    t_reader.join(timeout=5)

    assert not t_writer.is_alive()
    assert not t_reader.is_alive()

    assert not writer_errs
    assert not reader_errs
    # Final state is valid
    assert "v" in load_json("test.json")


# BD-01: AC-T10 Fresh Elo Contention Must Be Real
def test_t10_fresh_elo_contention(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="finished", score={"ft": [2, 0]})
    assert valid_full_time_score(m.get("score"))

    # Ensure ID absent from calibrated
    assert m["id"] not in load_json("calibrated.json").get("ids", [])

    # Capture initial Elo
    elo_initial = team_strength.get_team_profile(m["home"])["elo_rating"]

    # Mock providers to provide our match for calibration
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [m])

    entry_event = threading.Event()
    exit_event = threading.Event()

    # We patch team_strength.update_team_rating to block right before it commits, proving it's IN the calibration path
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
            try:
                scraper.refresh()
                results.append("R1_SUCCESS")
            except Exception as e:
                results.append(e)

        t1 = threading.Thread(target=first_refresh)
        t1.start()

        # Wait for first refresh to reach calibration
        assert entry_event.wait(timeout=5)

        # Start competing refresh
        def second_refresh():
            try:
                scraper.refresh()
                results.append("R2_SUCCESS")
            except RefreshBusyError:
                results.append("R2_BUSY")
            except Exception as e:
                results.append(e)

        t2 = threading.Thread(target=second_refresh)
        t2.start()
        t2.join(timeout=5)
        assert not t2.is_alive()

        # Release first refresh
        exit_event.set()
        t1.join(timeout=5)
        assert not t1.is_alive()

        # Assertions
        assert "R2_BUSY" in results
        assert "R1_SUCCESS" in results
        assert calib_count[0] == 1 # update_from_result is called once per match

        # Match ID recorded exactly once
        calibrated_ids = load_json("calibrated.json").get("ids", [])
        print("CALIBRATED:", calibrated_ids, load_json("calibrated.json")); assert calibrated_ids.count(m["id"]) == 1

        # Initial Elo != Final Elo
        elo_final = team_strength.get_team_profile(m["home"])["elo_rating"]
        assert elo_final != elo_initial

        # Sequential replay does no second calibration
        calib_count[0] = 0
        scraper.refresh()
        assert calib_count[0] == 0
        elo_replay = team_strength.get_team_profile(m["home"])["elo_rating"]
        assert elo_replay == elo_final


# BD-02: AC-T11 Complete Four-Store Immutable Evidence Test
def test_t11_immutable_evidence_preservation(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="upcoming", odds={"home_win": 1.5})

    # 1. Seed prediction_snapshots
    ensure_snapshot(m, {"home_win": 0.5})
    snap = get_snapshots_for_match(m["id"])[0]

    # 2. Seed odds_snapshots
    from utils.odds_snapshots import record_odds_snapshot
    record_odds_snapshot(m)

    # 3. Seed settlements
    from utils.settlements import settle_snapshot
    m_fin = make_match(id=m["id"], status="finished", score={"ft": [2, 0]})
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m_fin]})
    settle_snapshot(snap, m_fin)

    # 4. Seed evaluation_rows
    from utils.evaluation_rows import capture_evaluation_row
    from utils.settlements import get_settlements_for_match
    sett = get_settlements_for_match(m["id"])[0]
    capture_evaluation_row(snap, sett)

    def fingerprint(data):
        return json.dumps(data, sort_keys=True)

    snaps_before = get_snapshots_for_match(m["id"])
    odds_before = get_odds_history_for_match(m["id"])
    sett_before = get_settlements_for_match(m["id"])
    eval_before = get_all_evaluation_rows()

    assert len(snaps_before) >= 1
    assert len(odds_before) >= 1
    assert len(sett_before) >= 1
    assert len(eval_before) >= 1

    fp_snaps_before = fingerprint(snaps_before)
    fp_odds_before = fingerprint(odds_before)
    fp_sett_before = fingerprint(sett_before)
    fp_eval_before = fingerprint(eval_before)

    # Mock network but allow it to process an admissible match
    m2 = make_match(id="500w-2030-01-01-99", home="A", away="B", status="upcoming")
    monkeypatch.setattr("utils.fetcher_500.fetch_live_basketball", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda: [m2])
    monkeypatch.setattr("utils.fetcher_500.fetch_jczq_xml", lambda: [])
    monkeypatch.setattr("utils.fetcher_500.fetch_finished_matches", lambda: [])

    scraper.refresh()

    # Verify original IDs not deleted and payload identical
    assert fingerprint(get_snapshots_for_match(m["id"])) == fp_snaps_before
    assert fingerprint(get_odds_history_for_match(m["id"])) == fp_odds_before
    assert fingerprint(get_settlements_for_match(m["id"])) == fp_sett_before
    assert fingerprint(get_all_evaluation_rows()[:len(eval_before)]) == fp_eval_before


def test_t12_independent_writer_boundary(isolated_data_dir, make_match):
    m = make_match()

    entry_event = threading.Event()
    exit_event = threading.Event()

    def holder():
        with acquire_refresh_lock():
            entry_event.set()
            exit_event.wait(timeout=5)

    t1 = threading.Thread(target=holder)
    t1.start()

    assert entry_event.wait(timeout=5)

    # Ensure independent writer works concurrently
    ensure_snapshot(m, {"home_win": 0.8})
    assert len(get_snapshots_for_match(m["id"])) == 1

    exit_event.set()
    t1.join(timeout=5)
    assert not t1.is_alive()


# BD-08: AC-T13 All Stores Unchanged on Busy
def test_t13_no_writes_on_busy(isolated_data_dir, make_match, monkeypatch):
    m = make_match(status="upcoming", odds={"home_win": 1.5})
    # Seed 7 stores
    save_json("daily_matches.json", {"dates": ["2030-01-01"], "matches": [m]})
    team_strength.get_team_profile("Test")
    save_json("calibrated.json", {"ids": []})
    ensure_snapshot(m, {"home_win": 0.5})
    snap = get_snapshots_for_match(m["id"])[0]
    from utils.odds_snapshots import record_odds_snapshot
    record_odds_snapshot(m)

    from utils.settlements import settle_snapshot
    m_fin = make_match(id=m["id"], status="finished", score={"ft": [2, 0]})
    settle_snapshot(snap, m_fin)

    from utils.evaluation_rows import capture_evaluation_row
    from utils.settlements import get_settlements_for_match
    sett = get_settlements_for_match(m["id"])[0]
    capture_evaluation_row(snap, sett)

    def fp_file(name):
        try:
            return json.dumps(load_json(name), sort_keys=True)
        except FileNotFoundError:
            return None

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

    for f in files:
        assert fps_before[f] == fps_after[f]
