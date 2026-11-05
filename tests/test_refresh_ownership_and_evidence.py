"""
Refresh writer ownership, cross-store consistency and durable review evidence.

These tests are offline and deterministic:

- providers are monkeypatched to return nothing (no network);
- the runtime data directory is the ``isolated_data_dir`` tmp_path;
- multi-process cases use real subprocesses, not just threads.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402
from utils import evidence_store, refresh_state, scraper  # noqa: E402
from utils import daily_loader, fetcher_500  # noqa: E402
from utils.concurrency import RefreshBusyError, acquire_refresh_lock, active_writer  # noqa: E402
from utils.market_coverage import has_usable_market_odds  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def block(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(requests, "get", block)
    monkeypatch.setattr(requests, "post", block)
    monkeypatch.setattr(requests.Session, "get", block)
    monkeypatch.setattr(requests.Session, "post", block)


@pytest.fixture(autouse=True)
def reset_fetcher_cache():
    """fetcher 的进程内缓存/诊断跨用例共享，必须逐例清空。"""
    fetcher_500._CACHE.clear()
    fetcher_500._DIAGNOSTICS.clear()
    yield
    fetcher_500._CACHE.clear()
    fetcher_500._DIAGNOSTICS.clear()


@pytest.fixture()
def empty_providers(monkeypatch):
    """Explicit: only the tests that drive scraper.refresh need空供给。"""
    for name in (
        "fetch_live_basketball",
        "fetch_live_matches",
        "fetch_jczq_xml",
        "fetch_finished_matches",
    ):
        monkeypatch.setattr(f"utils.fetcher_500.{name}", lambda *a, **kw: [])
    return None


def _run(code: str, data_dir: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=ROOT,
    )


def _child(head: str) -> str:
    return (
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "import config\n"
        f"config.DATA_DIR = r'{head}'\n"
    )


def _seed_store(directory) -> None:
    """One already-tracked upcoming match, so a refresh has something to rewrite."""
    daily_loader.save_json(
        "daily_matches.json",
        {
            "meta": {"last_refresh": "2026-11-05T09:00:00+08:00", "sources": {}, "total": 1},
            "matches": [
                {
                    "id": "500j-2026-11-05-3316",
                    "sport": "basketball",
                    "league": "美国职业篮球联盟",
                    "date": "2026-11-05",
                    "time": "00:00",
                    "status": "upcoming",
                    "home": "洛杉矶快船",
                    "away": "迈阿密热火",
                    "market_tracked": True,
                    "odds": {"home_win": 2.78, "away_win": 1.22},
                }
            ],
        },
    )


def _store_hashes(directory) -> dict[str, str]:
    out = {}
    for name in refresh_state.PROTECTED_STORES:
        path = os.path.join(str(directory), name)
        if os.path.exists(path):
            with open(path, "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# Refresh ownership and generation marker
# ---------------------------------------------------------------------------

def test_ro_01_refresh_publishes_generation_and_digests(isolated_data_dir, empty_providers):
    _seed_store(isolated_data_dir)
    scraper.refresh(verbose=False)

    state = refresh_state.read_state(str(isolated_data_dir))
    assert state is not None
    assert state["status"] == refresh_state.STATUS_COMPLETED
    assert state["generation"] == 1
    assert state["owner"]["pid"] > 0
    assert state["owner"]["action"] == "refresh"
    assert state["stores"], "completed refresh must record store digests"

    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["decidable"] is True
    assert report["consistent"] is True
    assert report["reason"] == "CONSISTENT_SNAPSHOT"


def test_ro_02_second_refresh_advances_generation(isolated_data_dir, empty_providers):
    scraper.refresh(verbose=False)
    first = refresh_state.read_state(str(isolated_data_dir))["generation"]
    scraper.refresh(verbose=False)
    second = refresh_state.read_state(str(isolated_data_dir))["generation"]
    assert second == first + 1


def test_ro_03_mixed_snapshot_is_detected(isolated_data_dir, empty_providers):
    _seed_store(isolated_data_dir)
    scraper.refresh(verbose=False)
    assert refresh_state.consistency(str(isolated_data_dir))["consistent"] is True

    # Simulate a half-written generation: one store moves on without the marker.
    path = os.path.join(str(isolated_data_dir), "daily_matches.json")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["meta"]["total"] = payload["meta"].get("total", 0) + 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)

    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "STORES_CHANGED_SINCE_MARKER"
    assert "daily_matches.json" in report["mismatched"]


def test_ro_04_in_progress_marker_never_claims_consistency(isolated_data_dir):
    owner = refresh_state.owner_record("refresh")
    refresh_state.begin(str(isolated_data_dir), owner=owner)

    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["decidable"] is True
    assert report["consistent"] is False
    assert report["reason"] == "REFRESH_IN_PROGRESS"
    assert report["status"] == refresh_state.STATUS_IN_PROGRESS


def test_ro_05_missing_marker_is_undecidable_not_consistent(isolated_data_dir):
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["decidable"] is False
    assert report["consistent"] is False
    assert report["reason"] == "NO_REFRESH_STATE_MARKER"


def test_ro_06_refresh_failure_records_failed_state(isolated_data_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(scraper, "_refresh_impl", boom)

    with pytest.raises(RuntimeError):
        scraper.refresh(verbose=False)

    state = refresh_state.read_state(str(isolated_data_dir))
    assert state["status"] == refresh_state.STATUS_FAILED
    assert "provider exploded" in state["error"]

    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "REFRESH_FAILED"


# ---------------------------------------------------------------------------
# Real multi-process behaviour
# ---------------------------------------------------------------------------

def test_ro_07_busy_error_reports_the_active_writer(isolated_data_dir):
    holder = _child(str(isolated_data_dir)) + (
        "import sys, time\n"
        "from utils import refresh_state\n"
        "from utils.concurrency import acquire_refresh_lock\n"
        "with acquire_refresh_lock() as lease:\n"
        "    lease.begin()\n"
        "    sys.stdout.write(str(os.getpid()) if False else str(refresh_state.read_state(config.DATA_DIR)['owner']['pid']))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(20)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", holder],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
    )
    try:
        # Wait until the holder published its marker.
        deadline = time.time() + 20
        owner = None
        while time.time() < deadline:
            owner = active_writer(str(isolated_data_dir))
            if owner:
                break
            time.sleep(0.1)
        assert owner is not None, "holder did not publish an in-progress marker"
        assert owner["pid"] > 0

        probe = _child(str(isolated_data_dir)) + (
            "import json\n"
            "from utils.concurrency import acquire_refresh_lock, RefreshBusyError\n"
            "try:\n"
            "    with acquire_refresh_lock():\n"
            "        print('ACQUIRED')\n"
            "except RefreshBusyError as exc:\n"
            "    print('BUSY', bool(exc.owner), (exc.owner or {}).get('pid'))\n"
        )
        result = _run(probe, str(isolated_data_dir))
        assert result.returncode == 0, result.stderr
        assert "ACQUIRED" not in result.stdout
        assert "BUSY True" in result.stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_ro_08_second_instance_never_writes_the_stores(isolated_data_dir, empty_providers):
    _seed_store(isolated_data_dir)
    scraper.refresh(verbose=False)
    before = _store_hashes(isolated_data_dir)
    generation_before = refresh_state.read_state(str(isolated_data_dir))["generation"]

    holder = _child(str(isolated_data_dir)) + (
        "import sys, time\n"
        "from utils.concurrency import acquire_refresh_lock\n"
        "with acquire_refresh_lock() as lease:\n"
        "    lease.begin()\n"
        "    time.sleep(15)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", holder],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
    )
    try:
        deadline = time.time() + 20
        while time.time() < deadline and not active_writer(str(isolated_data_dir)):
            time.sleep(0.1)
        assert active_writer(str(isolated_data_dir)) is not None

        # 持有者已经开新的一代；被拒实例不得再推进它。
        generation_while_held = refresh_state.read_state(str(isolated_data_dir))["generation"]
        assert generation_while_held >= generation_before

        probe = _child(str(isolated_data_dir)) + (
            "from utils.concurrency import acquire_refresh_lock, RefreshBusyError\n"
            "try:\n"
            "    with acquire_refresh_lock():\n"
            "        print('ACQUIRED')\n"
            "except RefreshBusyError:\n"
            "    print('BUSY')\n"
        )
        result = _run(probe, str(isolated_data_dir))
        assert "BUSY" in result.stdout

        # The rejected instance must not have touched any protected store.
        assert _store_hashes(isolated_data_dir) == before
        assert (
            refresh_state.read_state(str(isolated_data_dir))["generation"]
            == generation_while_held
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_ro_09_crash_releases_the_writer_lock(isolated_data_dir):
    child = _child(str(isolated_data_dir)) + (
        "import time\n"
        "from utils.concurrency import acquire_refresh_lock\n"
        "with acquire_refresh_lock() as lease:\n"
        "    lease.begin()\n"
        "    print('HELD', flush=True)\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
    )
    try:
        line = proc.stdout.readline()
        assert "HELD" in line

        # Hard kill: no cleanup handler runs, the OS must drop the byte lock.
        proc.kill()
        proc.wait(timeout=15)

        probe = _child(str(isolated_data_dir)) + (
            "from utils.concurrency import acquire_refresh_lock, RefreshBusyError\n"
            "try:\n"
            "    with acquire_refresh_lock() as lease:\n"
            "        lease.begin()\n"
            "        print('ACQUIRED')\n"
            "except RefreshBusyError:\n"
            "    print('BUSY')\n"
        )
        result = _run(probe, str(isolated_data_dir))
        assert "ACQUIRED" in result.stdout, result.stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)


def test_ro_10_refresh_adds_only_runtime_metadata_files(isolated_data_dir, empty_providers):
    _seed_store(isolated_data_dir)
    before = set(os.listdir(str(isolated_data_dir)))
    scraper.refresh(verbose=False)
    after = set(os.listdir(str(isolated_data_dir)))
    assert after - before <= {".refresh.lock", ".refresh_state.json"}
    # 七个受保护存储与刷新元数据之外，不得出现任何其它运行时产物。
    assert not any(name.endswith(".tmp") for name in after)


# ---------------------------------------------------------------------------
# Durable evidence store
# ---------------------------------------------------------------------------

def _record(**overrides):
    record = {
        "schema_version": "1.0.0",
        "candidate_id": "NXD-test0001",
        "candidate_fingerprint": "cf-1",
        "dataset_fingerprint": "df-1",
        "canonical_ids": ["500j-2026-11-05-3316", "500lq-2026-11-05-周三316"],
        "decision": "SAME_EVENT",
        "reviewer": "YGone",
        "decided_at": "2026-11-05T13:00:00+08:00",
        "label_provenance": "HUMAN_REVIEWED",
        "evidence_reference": "local://notes/3316",
        "notes": "inspected source pages",
    }
    record.update(overrides)
    return record


def test_ro_11_evidence_survives_process_restart(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    assert evidence_store.append_record(_record(), configured_dir=evidence_dir)["status"] == "APPENDED"

    code = (
        "import json, sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from utils import evidence_store as es\n"
        f"pairs = es.list_pairs(r'{evidence_dir}')\n"
        "key = pairs[0]['pair_key']\n"
        f"payload = es.load_pair(r'{evidence_dir}', key)\n"
        "print(json.dumps({'pairs': len(pairs), 'records': len(payload['records']),"
        " 'decision': payload['records'][0]['decision']}))\n"
    )
    result = _run(code, evidence_dir)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"pairs": 1, "records": 1, "decision": "SAME_EVENT"}


def test_ro_12_corrupt_evidence_is_rejected_not_reset(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    evidence_store.append_record(_record(), configured_dir=evidence_dir)
    key = evidence_store.pair_key(_record()["canonical_ids"])
    path = os.path.join(evidence_dir, evidence_store.RECORD_DIRNAME, f"{key}.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not-json")

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.load_pair(evidence_dir, key)
    assert excinfo.value.code == "CORRUPT_EVIDENCE_FILE"

    # 损坏文件不得被静默覆盖。
    with pytest.raises(evidence_store.EvidenceStoreError):
        evidence_store.append_record(_record(), configured_dir=evidence_dir)
    with open(path, "r", encoding="utf-8") as handle:
        assert handle.read() == "{not-json"


def test_ro_13_automated_or_prohibited_records_are_rejected(tmp_path):
    evidence_dir = str(tmp_path / "evidence")

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(
            _record(label_provenance="AUTOMATED"), configured_dir=evidence_dir
        )
    assert excinfo.value.code == "AUTOMATED_LABEL_REJECTED"

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(_record(reviewer="model"), configured_dir=evidence_dir)
    assert excinfo.value.code == "AUTOMATED_REVIEWER_REJECTED"

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(_record(approved=True), configured_dir=evidence_dir)
    assert excinfo.value.code == "PROHIBITED_REVIEW_FIELD"

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(_record(decision="MERGE_NOW"), configured_dir=evidence_dir)
    assert excinfo.value.code == "UNSUPPORTED_DECISION"

    assert evidence_store.list_pairs(evidence_dir) == []


def test_ro_14_conflicting_decisions_are_never_resolved_by_recency(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    evidence_store.append_record(_record(), configured_dir=evidence_dir)
    result = evidence_store.append_record(
        _record(decision="DIFFERENT_EVENT", decided_at="2026-11-05T14:00:00+08:00"),
        configured_dir=evidence_dir,
    )
    assert result["status"] == "APPENDED"
    assert result["conflict"] is True

    key = evidence_store.pair_key(_record()["canonical_ids"])
    summary = evidence_store.decision_summary(evidence_store.load_pair(evidence_dir, key))
    assert summary["conflict"] is True
    assert summary["effective_decision"] is None
    assert summary["reason"] == "CONFLICTING_LABEL"
    assert summary["record_count"] == 2


def test_ro_15_conflicting_rewrite_is_rejected(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    evidence_store.append_record(_record(), configured_dir=evidence_dir)

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(
            _record(decision="INSUFFICIENT_EVIDENCE"), configured_dir=evidence_dir
        )
    assert excinfo.value.code == "CONFLICTING_REWRITE"


def test_ro_16_idempotent_duplicate_is_not_stored_twice(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    evidence_store.append_record(_record(), configured_dir=evidence_dir)
    second = evidence_store.append_record(_record(), configured_dir=evidence_dir)
    assert second["status"] == "IDEMPOTENT_DUPLICATE"
    assert second["record_count"] == 1


def test_ro_17_stale_candidate_and_fingerprint_are_classified():
    record = _record()

    bound = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint=record["dataset_fingerprint"],
    )
    assert bound["binding"] == evidence_store.BINDING_BOUND
    assert bound["reattachment_required"] is False
    assert bound["auto_reattached"] is False
    assert bound["production_authorized"] is False

    stale_fp = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint="df-2",
    )
    assert stale_fp["binding"] == evidence_store.BINDING_STALE_FINGERPRINT
    assert stale_fp["reattachment_required"] is True
    assert stale_fp["next_required_action"] == "REATTACH_BY_OPERATOR_REQUIRED"

    moved = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id="NXD-test0002",
        current_dataset_fingerprint="df-2",
    )
    assert moved["binding"] == evidence_store.BINDING_CANDIDATE_CHANGED
    assert moved["persisted"] is True
    assert moved["valid_for_current_dataset"] is False


def test_ro_18_ambiguous_identity_is_rejected_not_guessed():
    record = _record()
    ambiguous = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint=record["dataset_fingerprint"],
        candidates_sharing_pair=2,
    )
    assert ambiguous["binding"] == evidence_store.BINDING_AMBIGUOUS
    assert ambiguous["auto_reattached"] is False
    assert ambiguous["reattachment_required"] is True

    changed = evidence_store.inspect_record(
        record,
        current_canonical_ids=["500j-2026-11-05-3317", "500lq-2026-11-05-周三317"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint=record["dataset_fingerprint"],
    )
    assert changed["binding"] == evidence_store.BINDING_CANONICAL_CHANGED


def test_ro_19_evidence_directory_inside_repository_is_refused():
    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.resolve_evidence_dir(os.path.join(ROOT, "reports", "evidence"))
    assert excinfo.value.code == "EVIDENCE_DIR_INSIDE_REPOSITORY"

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.export_copy(os.path.join(ROOT, "evidence-export"))
    assert excinfo.value.code == "EXPORT_INTO_REPOSITORY"


def test_ro_20_export_is_read_only_and_hashed(tmp_path):
    evidence_dir = str(tmp_path / "evidence")
    evidence_store.append_record(_record(), configured_dir=evidence_dir)
    target = str(tmp_path / "backup")

    manifest = evidence_store.export_copy(target, configured_dir=evidence_dir)
    assert manifest["file_count"] == 1
    assert manifest["read_only"] is True
    assert manifest["no_decisions_changed"] is True

    exported = os.path.join(target, manifest["files"][0]["file"])
    with open(exported, "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == manifest["files"][0]["sha256"]
    assert os.path.isfile(os.path.join(target, "export_manifest.json"))


# ---------------------------------------------------------------------------
# NBA feed classification and visibility
# ---------------------------------------------------------------------------

def test_ro_21_nba_league_label_variants_are_equivalent():
    assert fetcher_500.is_nba_league("NBA") is True
    assert fetcher_500.is_nba_league("美国职业篮球联盟") is True
    assert fetcher_500.is_nba_league("德篮甲") is False
    assert fetcher_500.is_nba_league(None) is False


def test_ro_22_nba_fixture_without_market_is_filtered_not_failed():
    nba_no_market = {
        "sport": "basketball",
        "league": "NBA",
        "date": "2026-11-05",
        "time": "09:00",
        "home": "犹他爵士",
        "away": "新奥尔良鹈鹕",
        "status": "upcoming",
        "odds": {},
    }
    nba_with_market = {
        "sport": "basketball",
        "league": "NBA",
        "date": "2026-11-05",
        "time": "11:30",
        "home": "迈阿密热火",
        "away": "洛杉矶快船",
        "status": "upcoming",
        "odds": {"home_win": 1.22, "away_win": 2.78},
    }
    assert has_usable_market_odds(nba_no_market) is False
    assert has_usable_market_odds(nba_with_market) is True
    # 分类差别必须体现在「无可用盘口」，而不是「取数失败」。
    assert fetcher_500.is_nba_league(nba_no_market["league"]) is True


def test_ro_23_provider_failure_classification(isolated_data_dir, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("utils.fetcher_500.requests.get", unavailable)

    rows = fetcher_500.fetch_live_basketball()
    assert rows == []
    diag = fetcher_500.source_diagnostics()["live_basketball"]
    assert diag["status"] == "unavailable"
    assert diag["rows"] == 0


def test_ro_24_empty_feed_is_distinguished_from_failure(isolated_data_dir, monkeypatch):
    class _FakeResponse:
        status_code = 200
        content = "<html></html>".encode("gb18030")
        text = "<html></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr("utils.fetcher_500.requests.get", lambda *a, **kw: _FakeResponse())
    rows = fetcher_500.fetch_live_basketball()
    assert rows == []
    diag = fetcher_500.source_diagnostics()["live_basketball"]
    assert diag["status"] == "empty"


def test_ro_25_basketball_date_visibility_contract(isolated_data_dir):
    delta = daily_loader.save_json

    delta("daily_matches.json", {
        "meta": {"last_refresh": "2026-11-05T10:00:00+08:00", "sources": {}, "total": 2},
        "matches": [
            {
                "id": "500lq-2026-11-05-周三316",
                "sport": "basketball",
                "league": "NBA",
                "date": "2026-11-05",
                "time": "11:30",
                "status": "upcoming",
                "home": "迈阿密热火",
                "away": "洛杉矶快船",
                "odds": {"home_win": 1.22, "away_win": 2.78},
            },
            {
                "id": "500w-2026-11-06-1",
                "sport": "football",
                "league": "英超",
                "date": "2026-11-06",
                "time": "20:00",
                "status": "upcoming",
                "home": "曼城",
                "away": "阿森纳",
                "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0},
            },
        ],
    })

    by_sport = daily_loader.get_available_dates_by_sport()
    assert set(by_sport) >= {"football", "basketball"}
    assert by_sport["basketball"] == ["2026-11-05"]
    assert by_sport["football"] == ["2026-11-06"]
    # 选择篮球时不得停留在没有篮球比赛的日期。
    assert "2026-11-06" not in by_sport["basketball"]
    assert daily_loader.get_available_dates(sport="basketball") == ["2026-11-05"]
