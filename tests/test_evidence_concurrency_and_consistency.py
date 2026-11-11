"""
Corrective regression set: concurrent evidence appends, snapshot writer
ownership, refresh-state consistency semantics, path containment and the
explicit operator persistence path.

Offline and deterministic: providers are stubbed, the runtime data directory is
a tmp_path, and every multi-process case uses real subprocesses.
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

from utils import evidence_store, prediction_snapshots, refresh_state, scraper  # noqa: E402
from utils import daily_loader  # noqa: E402


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


@pytest.fixture()
def empty_providers(monkeypatch):
    for name in (
        "fetch_live_basketball",
        "fetch_live_matches",
        "fetch_jczq_xml",
        "fetch_finished_matches",
    ):
        monkeypatch.setattr(f"utils.fetcher_500.{name}", lambda *a, **kw: [])


def _evidence_record(tag: str, **overrides):
    record = {
        "schema_version": "1.0.0",
        "candidate_id": f"NXD-er{tag}",
        "candidate_fingerprint": f"cf-{tag}",
        "dataset_fingerprint": "df-er",
        "canonical_ids": ["500j-2026-11-05-3316", "500lq-2026-11-05-周三316"],
        "decision": "SAME_EVENT",
        "reviewer": f"operator-{tag}",
        "decided_at": f"2026-11-05T15:0{tag}:00+08:00",
        "label_provenance": "HUMAN_REVIEWED",
        "evidence_reference": f"local://notes/{tag}",
        "notes": f"er probe {tag}",
    }
    record.update(overrides)
    return record


def _seed_seven(directory) -> None:
    shapes = {
        "prediction_snapshots.json": {"version": 1, "snapshots": {}},
        "odds_snapshots.json": {"version": 1, "snapshots": {}},
        "settlements.json": {"version": 1, "settlements": {}},
        "evaluation_rows.json": {"version": 1, "rows": {}},
        "team_strength.json": {"version": 1, "teams": {}},
        "calibrated.json": {"version": 1, "sports": {}},
    }
    for name, payload in shapes.items():
        with open(os.path.join(str(directory), name), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
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


# ---------------------------------------------------------------------------
# ER-01 / ER-02 — concurrent appends keep every decision
# ---------------------------------------------------------------------------

_EVIDENCE_CHILD = """
import json, os, sys, time
root, evidence_dir, barrier_dir, tag, record_json = sys.argv[1:6]
sys.path.insert(0, root)
from utils import evidence_store

open(os.path.join(barrier_dir, "start_" + tag), "w").close()
deadline = time.time() + 60
while time.time() < deadline:
    if len([n for n in os.listdir(barrier_dir) if n.startswith("start_")]) >= 2:
        break
    time.sleep(0.01)

record = json.loads(record_json)
try:
    result = evidence_store.append_record(record, configured_dir=evidence_dir)
    print(json.dumps({"tag": tag, "status": result["status"], "count": result["record_count"]}))
except Exception as exc:
    print(json.dumps({"tag": tag, "error": type(exc).__name__ + ": " + str(exc)}))
"""


def _run_two_appends(tmp_path, records):
    child = tmp_path / "ev_child.py"
    child.write_text(_EVIDENCE_CHILD, encoding="utf-8")
    barrier = tmp_path / "barrier"
    evidence = tmp_path / "evidence"
    barrier.mkdir(exist_ok=True)
    evidence.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}

    procs = []
    for tag, record in records:
        procs.append(
            subprocess.Popen(
                [sys.executable, "-X", "utf8", str(child), ROOT, str(evidence), str(barrier),
                 tag, json.dumps(record, ensure_ascii=False)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT, env=env,
            )
        )

    outcomes = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=180)
        line = (stdout.strip().splitlines() or [""])[-1]
        outcomes.append(json.loads(line) if line.startswith("{") else {"error": stderr[-200:]})
    return outcomes, evidence


def test_er_01_02_concurrent_appends_keep_every_decision(tmp_path):
    outcomes, evidence = _run_two_appends(
        tmp_path, [("1", _evidence_record("1")), ("2", _evidence_record("2"))]
    )
    assert [item.get("status") for item in outcomes] == ["APPENDED", "APPENDED"], outcomes

    key = evidence_store.pair_key(_evidence_record("1")["canonical_ids"])
    payload = evidence_store.load_pair(str(evidence), key)
    assert sorted(record["reviewer"] for record in payload["records"]) == [
        "operator-1",
        "operator-2",
    ]


# ---------------------------------------------------------------------------
# ER-03 — crash releases evidence writer ownership
# ---------------------------------------------------------------------------

def test_er_03_crash_releases_evidence_writer_ownership(tmp_path):
    from utils.concurrency import acquire_sidecar_lock

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    child = tmp_path / "hold.py"
    child.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from utils.concurrency import acquire_sidecar_lock\n"
        "from utils import evidence_store\n"
        f"with acquire_sidecar_lock(evidence_store.EVIDENCE_LOCK_NAME, data_dir=r'{evidence}', timeout=5):\n"
        "    print('HELD', flush=True)\n"
        "    time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(child)],
        stdout=subprocess.PIPE, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        assert "HELD" in proc.stdout.readline()
        proc.kill()
        proc.wait(timeout=20)
        result = evidence_store.append_record(_evidence_record("3"), configured_dir=str(evidence))
        assert result["status"] == "APPENDED"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)


# ---------------------------------------------------------------------------
# ER-04 / ER-07 — conflicting decisions retained, corrupt shard never rewritten
# ---------------------------------------------------------------------------

def test_er_04_conflicting_decisions_are_both_retained(tmp_path):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("4"), configured_dir=evidence)
    second = evidence_store.append_record(
        _evidence_record("4", decided_at="2026-11-05T16:00:00+08:00", decision="DIFFERENT_EVENT"),
        configured_dir=evidence,
    )
    assert second["conflict"] is True

    key = evidence_store.pair_key(_evidence_record("4")["canonical_ids"])
    payload = evidence_store.load_pair(evidence, key)
    assert len(payload["records"]) == 2

    summary = evidence_store.decision_summary(payload)
    assert summary["conflict"] is True
    assert summary["effective_decision"] is None
    assert summary["reason"] == "CONFLICTING_LABEL"


def test_er_07_corrupt_shard_is_never_overwritten(tmp_path):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("7"), configured_dir=evidence)
    key = evidence_store.pair_key(_evidence_record("7")["canonical_ids"])
    path = os.path.join(evidence, evidence_store.RECORD_DIRNAME, f"{key}.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"schema_version": "1.0.0", "pair_key": "WRONG", "records": []}')

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.append_record(
            _evidence_record("7", reviewer="other"), configured_dir=evidence
        )
    assert excinfo.value.code == "CORRUPT_EVIDENCE_FILE"
    assert "pair_key mismatch" in str(excinfo.value)

    with open(path, "r", encoding="utf-8") as handle:
        assert "WRONG" in handle.read(), "a corrupt shard must be preserved for forensics"


@pytest.mark.parametrize(
    "mutation, expected_fragment",
    [
        ("shard_schema", "unsupported shard schema"),
        ("shard_kind", "unexpected shard kind"),
        ("shard_canonical", "canonical ids do not match shard"),
        ("shard_not_list", "canonical_ids missing"),
        ("record_not_object", "not an object"),
        ("record_missing_field", "INCOMPLETE_RECORD"),
        ("record_pair", "record pair differs from shard"),
        ("record_decision", "UNSUPPORTED_DECISION"),
        ("record_prohibited", "PROHIBITED_REVIEW_FIELD"),
        ("record_reviewer", "AUTOMATED_REVIEWER_REJECTED"),
        ("duplicate_identity", "conflicting duplicate identity"),
    ],
)
def test_er_07c_stored_record_variants_are_rejected(tmp_path, mutation, expected_fragment):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("7c"), configured_dir=evidence)
    key = evidence_store.pair_key(_evidence_record("7c")["canonical_ids"])
    path = os.path.join(evidence, evidence_store.RECORD_DIRNAME, f"{key}.json")

    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    record = payload["records"][0]
    if mutation == "shard_schema":
        payload["schema_version"] = "9.9.9"
    elif mutation == "shard_kind":
        payload["kind"] = "SOMETHING_ELSE"
    elif mutation == "shard_canonical":
        payload["canonical_ids"] = ["500j-2026-11-05-9999", "500lq-2026-11-05-周三999"]
    elif mutation == "shard_not_list":
        payload["canonical_ids"] = "500j-2026-11-05-3316"
    elif mutation == "record_not_object":
        payload["records"] = ["not-an-object"]
    elif mutation == "record_missing_field":
        record.pop("evidence_reference")
    elif mutation == "record_pair":
        record["canonical_ids"] = ["500j-2026-11-05-9999", "500lq-2026-11-05-周三999"]
    elif mutation == "record_decision":
        record["decision"] = "MERGE_NOW"
    elif mutation == "record_prohibited":
        record["approved"] = True
    elif mutation == "record_reviewer":
        record["reviewer"] = "model"
    elif mutation == "duplicate_identity":
        clash = dict(record)
        clash["decision"] = "DIFFERENT_EVENT"
        payload["records"] = [record, clash]

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.load_pair(evidence, key)
    assert excinfo.value.code == "CORRUPT_EVIDENCE_FILE"
    assert expected_fragment in str(excinfo.value)


def test_er_07b_stored_record_violations_are_rejected(tmp_path):
    """A shard that is valid JSON but carries an invalid historical record is corrupt."""
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("7b"), configured_dir=evidence)
    key = evidence_store.pair_key(_evidence_record("7b")["canonical_ids"])
    path = os.path.join(evidence, evidence_store.RECORD_DIRNAME, f"{key}.json")

    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    payload["records"][0]["label_provenance"] = "AUTOMATED"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.load_pair(evidence, key)
    assert excinfo.value.code == "CORRUPT_EVIDENCE_FILE"
    assert "AUTOMATED_LABEL_REJECTED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# ER-08 / ER-09 — path containment through links and case aliases
# ---------------------------------------------------------------------------

def _make_directory_link(link_path: str, target: str) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link_path, target], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise OSError(result.stderr or result.stdout)
    else:
        os.symlink(target, link_path)


def test_er_08_09_link_escaping_into_repository_is_refused(tmp_path):
    link = tmp_path / "repo_link"
    try:
        _make_directory_link(str(link), ROOT)
    except OSError as exc:  # pragma: no cover - privileges vary by host
        pytest.skip(f"cannot create a directory link here: {exc}")

    # 1) 链接解析后落在仓库内 -> 拒绝
    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.resolve_evidence_dir(str(link / "reports" / "evidence"))
    assert excinfo.value.code == "EVIDENCE_DIR_INSIDE_REPOSITORY"

    # 2) 含 `..` 的别名在任何平台都必须按解析结果判定
    with pytest.raises(evidence_store.EvidenceStoreError):
        evidence_store.resolve_evidence_dir(
            os.path.join(ROOT, "reports", "..", "reports", "evidence")
        )

    # 3) 大小写别名只在大小写不敏感的文件系统上构成同一路径（Windows）
    if os.name == "nt":
        with pytest.raises(evidence_store.EvidenceStoreError):
            evidence_store.resolve_evidence_dir(ROOT.upper())
        with pytest.raises(evidence_store.EvidenceStoreError):
            evidence_store.export_copy(ROOT.upper())

    # 4) 真正外部的目录继续可用
    allowed = evidence_store.resolve_evidence_dir(str(tmp_path / "outside"))
    assert allowed.endswith("outside")


# ---------------------------------------------------------------------------
# ER-10 / ER-11 / ER-12 — refresh-state consistency semantics
# ---------------------------------------------------------------------------

def test_er_10_missing_protected_store_cannot_report_consistent(
    isolated_data_dir, empty_providers
):
    _seed_seven(isolated_data_dir)
    scraper.refresh(verbose=False)
    assert refresh_state.consistency(str(isolated_data_dir))["consistent"] is True

    os.remove(os.path.join(str(isolated_data_dir), "settlements.json"))
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "STORE_MISSING"
    assert "settlements.json" in report["missing"]


def test_er_11_malformed_marker_fails_closed(isolated_data_dir):
    marker = os.path.join(str(isolated_data_dir), refresh_state.STATE_FILENAME)

    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "REFRESH_STATE_UNREADABLE"
    assert report["decidable"] is False

    with open(marker, "w", encoding="utf-8") as handle:
        json.dump(
            {"schema_version": "1.0.0", "status": "PARTIAL", "generation": 1, "stores": {}},
            handle,
        )
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "UNKNOWN_REFRESH_STATUS"

    with open(marker, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": "1.0.0",
                "status": "COMPLETED",
                "generation": 1,
                "stores": {"daily_matches.json": "x"},
            },
            handle,
        )
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "INCOMPLETE_MARKER"


def test_er_12_generation_change_during_validation_is_rejected(
    isolated_data_dir, empty_providers, monkeypatch
):
    _seed_seven(isolated_data_dir)
    scraper.refresh(verbose=False)
    real = refresh_state.store_digests

    def bumping(data_dir):
        digests = real(data_dir)
        state = refresh_state.read_state(data_dir)
        state["generation"] = int(state["generation"]) + 1
        with open(refresh_state.state_path(data_dir), "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        return digests

    monkeypatch.setattr(refresh_state, "store_digests", bumping)
    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "GENERATION_CHANGED_DURING_READ"


def test_er_13_non_refresh_snapshot_write_invalidates_published_consistency(
    isolated_data_dir, empty_providers
):
    _seed_seven(isolated_data_dir)
    scraper.refresh(verbose=False)
    assert refresh_state.consistency(str(isolated_data_dir))["consistent"] is True

    match = {
        "id": "500j-2030-01-01-9001",
        "sport": "football",
        "league": "英超",
        "date": "2030-01-01",
        "time": "20:00",
        "status": "upcoming",
        "home": "曼城",
        "away": "阿森纳",
        "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0},
    }
    assert prediction_snapshots.create_snapshot(match, {}, model_version="probe-er13") is not None

    report = refresh_state.consistency(str(isolated_data_dir))
    assert report["consistent"] is False
    assert report["reason"] == "STORES_CHANGED_SINCE_MARKER"
    assert "prediction_snapshots.json" in report["mismatched"]


# ---------------------------------------------------------------------------
# ER-14 / ER-15 / ER-16 — snapshot writer ownership
# ---------------------------------------------------------------------------

_SNAPSHOT_CHILD = """
import json, sys
root, data_dir, match_id, version = sys.argv[1:5]
sys.path.insert(0, root)
import config
config.DATA_DIR = data_dir
from utils import prediction_snapshots
match = {
    "id": match_id, "sport": "football", "league": "英超",
    "date": "2030-01-01", "time": "20:00", "status": "upcoming",
    "home": "曼城", "away": "阿森纳",
    "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0},
}
snap = prediction_snapshots.create_snapshot(match, {}, model_version=version)
print("CREATED" if snap else "NONE")
"""


def _run_snapshot_child(data_dir, match_id: str, version: str):
    child = os.path.join(str(data_dir), "snap_child.py")
    with open(child, "w", encoding="utf-8") as handle:
        handle.write(_SNAPSHOT_CHILD)
    return subprocess.Popen(
        [sys.executable, "-X", "utf8", child, ROOT, str(data_dir), match_id, version],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
    )


def test_er_14_concurrent_snapshot_writers_keep_every_snapshot(isolated_data_dir):
    procs = [
        _run_snapshot_child(isolated_data_dir, "500j-2030-01-01-9001", "probe-a"),
        _run_snapshot_child(isolated_data_dir, "500j-2030-01-01-9002", "probe-b"),
    ]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=180)
        assert "CREATED" in stdout, stderr

    store = json.loads(
        (isolated_data_dir / "prediction_snapshots.json").read_text(encoding="utf-8")
    )
    assert len(store["snapshots"]) == 2, "a cross-process append must not be lost"


def test_er_15_snapshot_write_is_not_blocked_by_the_refresh_lock(isolated_data_dir):
    holder = isolated_data_dir / "hold_refresh.py"
    holder.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "import config\n"
        f"config.DATA_DIR = r'{isolated_data_dir}'\n"
        "from utils.concurrency import acquire_refresh_lock\n"
        "with acquire_refresh_lock():\n"
        "    print('HELD', flush=True)\n"
        "    time.sleep(45)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(holder)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        assert "HELD" in proc.stdout.readline()
        child = _run_snapshot_child(isolated_data_dir, "500j-2030-01-01-9003", "probe-c")
        stdout, stderr = child.communicate(timeout=120)
        assert "CREATED" in stdout, stderr
        store = json.loads(
            (isolated_data_dir / "prediction_snapshots.json").read_text(encoding="utf-8")
        )
        assert len(store["snapshots"]) == 1
    finally:
        proc.kill()
        proc.wait(timeout=20)


def test_er_16_refresh_materializing_snapshots_does_not_deadlock(
    isolated_data_dir, monkeypatch
):
    row = {
        "sport": "football",
        "league": "英超",
        "date": "2030-01-01",
        "time": "20:00",
        "status": "upcoming",
        "home": "曼城",
        "away": "阿森纳",
        "jczq_no": "001",
        "odds": {"home_win": 1.9, "draw": 3.5, "away_win": 4.0},
    }
    monkeypatch.setattr("utils.fetcher_500.fetch_live_matches", lambda *a, **kw: [dict(row)])

    result = scraper.refresh(verbose=False)
    assert result["prediction_snapshots_added"] >= 1
    assert refresh_state.read_state(str(isolated_data_dir))["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# ER-17 / ER-18 — candidate fingerprint binding
# ---------------------------------------------------------------------------

def test_er_17_candidate_fingerprint_drift_is_detected():
    record = _evidence_record("17")

    drifted = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint=record["dataset_fingerprint"],
        current_candidate_fingerprint="cf-drifted",
    )
    assert drifted["binding"] == evidence_store.BINDING_CANDIDATE_FINGERPRINT_CHANGED
    assert drifted["reattachment_required"] is True
    assert drifted["stored_candidate_fingerprint"] == "cf-17"

    matching = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint=record["dataset_fingerprint"],
        current_candidate_fingerprint="cf-17",
    )
    assert matching["binding"] == evidence_store.BINDING_BOUND


def test_er_18_pair_key_is_an_address_not_an_approval():
    record = _evidence_record("18")
    result = evidence_store.inspect_record(
        record,
        current_canonical_ids=record["canonical_ids"],
        current_candidate_id=record["candidate_id"],
        current_dataset_fingerprint="df-other",
    )
    assert result["pair_key"] == evidence_store.pair_key(record["canonical_ids"])
    assert result["binding"] == evidence_store.BINDING_STALE_FINGERPRINT
    assert result["auto_reattached"] is False
    assert result["production_authorized"] is False


# ---------------------------------------------------------------------------
# ER-19 / ER-23 — explicit operator persistence path
# ---------------------------------------------------------------------------

def test_er_19_operator_persist_requires_explicit_confirmation(tmp_path, monkeypatch):
    from tools import nba_human_review_workbench as wb

    refused = wb.command_persist(None, None, confirm=False)
    assert refused["status"] == "EXPLICIT_CONFIRMATION_REQUIRED"

    evidence = str(tmp_path / "evidence")
    monkeypatch.setattr(
        wb,
        "command_validate",
        lambda *a, **kw: {
            "real_data_available": True,
            "validation_status": "SUCCESS",
            "review_rejected_count": 0,
            "evidence_rejected_count": 0,
            "rejected": [],
            # A confirmed persist must carry the fingerprint it was validated
            # against; without it freshness can never be verified and the
            # operational status is `PARTIAL`, not `PERSISTED`.
            "dataset_fingerprint": "df-er",
            "accepted": {"NXD-er19": _evidence_record("19")},
        },
    )
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda data_dir: object())
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda dataset: "df-er")
    persisted = wb.command_persist(None, "reviews.json", evidence_dir=evidence, confirm=True)
    assert persisted["status"] == "PERSISTED"
    assert persisted["persisted_count"] == 1
    assert persisted["freshness_state"] == "UNCHANGED"
    assert persisted["freshness_verified"] is True
    assert persisted["review_evidence_only"] is True

    key = evidence_store.pair_key(_evidence_record("19")["canonical_ids"])
    assert evidence_store.load_pair(evidence, key)["records"][0]["reviewer"] == "operator-19"


def test_er_19b_persist_refuses_rejected_reviews(tmp_path, monkeypatch):
    from tools import nba_human_review_workbench as wb

    monkeypatch.setattr(
        wb,
        "command_validate",
        lambda *a, **kw: {
            "real_data_available": True,
            "validation_status": "REVIEW_REJECTED",
            "review_rejected_count": 1,
            "evidence_rejected_count": 0,
            "rejected": [{"reason_code": "STALE_CANDIDATE_FINGERPRINT"}],
            "accepted": {},
        },
    )
    result = wb.command_persist(
        None, "ignored.json", confirm=True, evidence_dir=str(tmp_path / "evidence")
    )
    assert result["status"] == "REJECTED"
    assert not (tmp_path / "evidence").exists()


def test_er_23_persist_never_grants_production_authorization(tmp_path, monkeypatch):
    from tools import nba_human_review_workbench as wb

    monkeypatch.setattr(
        wb,
        "command_validate",
        lambda *a, **kw: {
            "real_data_available": True,
            "validation_status": "SUCCESS",
            "review_rejected_count": 0,
            "evidence_rejected_count": 0,
            "rejected": [],
            "accepted": {"NXD-er23": _evidence_record("23")},
        },
    )
    result = wb.command_persist(
        None, "ignored.json", confirm=True, evidence_dir=str(tmp_path / "evidence")
    )
    assert result["production_write_authorization"] is False
    assert result["surviving_canonical_id"] is None
    assert result["decision"] == "NO-GO"
    assert os.path.isfile(os.path.join(str(tmp_path / "evidence"), ".evidence.lock"))


# ---------------------------------------------------------------------------
# ER-21 / ER-22 — export integrity and source retention
# ---------------------------------------------------------------------------

def test_er_21_export_is_validated_and_labelled_advisory(tmp_path):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("21"), configured_dir=evidence)
    manifest = evidence_store.export_copy(str(tmp_path / "backup"), configured_dir=evidence)

    assert manifest["export_status"] == "COMPLETED_ADVISORY"
    assert manifest["point_in_time_consistent"] is False
    assert manifest["file_count"] == 1
    for entry in manifest["files"]:
        assert entry["sha256"] == entry["source_sha256"]
        assert entry["stable"] is True


def test_er_21b_export_refuses_corrupt_shard(tmp_path):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_evidence_record("21b"), configured_dir=evidence)
    key = evidence_store.pair_key(_evidence_record("21b")["canonical_ids"])
    path = os.path.join(evidence, evidence_store.RECORD_DIRNAME, f"{key}.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not-json")

    with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
        evidence_store.export_copy(str(tmp_path / "backup2"), configured_dir=evidence)
    assert excinfo.value.code == "CORRUPT_EVIDENCE_FILE"


def test_er_22_source_retention_does_not_invent_review_provenance(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("external box score excerpt", encoding="utf-8")

    manifest = evidence_store.retain_source_copy(
        str(source), configured_dir=str(tmp_path / "evidence")
    )
    assert manifest["verified_by_human"] is False
    assert manifest["sets_review_provenance"] is False
    assert manifest["sets_verification_status"] is False
    assert "reviewer" not in manifest and "decided_at" not in manifest
    assert manifest["size"] == source.stat().st_size
    assert evidence_store.list_pairs(str(tmp_path / "evidence")) == []


# ---------------------------------------------------------------------------
# ER-24 — repository protected stores unchanged across a read-only boundary
#
# Integrity is a property of an *operation boundary*, not of a fixed digest:
# a legitimate refresh rewrites every protected store, so a historical
# constant is not an invariant.  The corrected contract observes the stores
# at T0, performs the read-only work under examination, and re-observes at T1.
# ---------------------------------------------------------------------------

#: Distinct outcomes. A differing digest alone is never evidence of an
#: unauthorized write, and "missing" is never equivalent to "unchanged".
PROTECTED_STORES_UNCHANGED = "PROTECTED_STORES_UNCHANGED"
UNAUTHORIZED_TEST_MUTATION = "UNAUTHORIZED_TEST_MUTATION"
EXTERNAL_CONCURRENT_REFRESH = "EXTERNAL_CONCURRENT_REFRESH"
HISTORICAL_HASH_MISMATCH = "HISTORICAL_HASH_MISMATCH"
PROTECTED_STORE_MISSING = "PROTECTED_STORE_MISSING"
PROTECTED_STORE_UNREADABLE = "PROTECTED_STORE_UNREADABLE"

_STORE_PRESENT = "PRESENT"
_STORE_MISSING = "MISSING"
_STORE_UNREADABLE = "UNREADABLE"


def _observe_protected_stores(directory) -> dict:
    """Byte-level observation of the seven protected stores (read-only)."""
    observation = {}
    for name in refresh_state.PROTECTED_STORES:
        path = os.path.join(str(directory), name)
        if not os.path.lexists(path):
            observation[name] = {"state": _STORE_MISSING, "sha256": None, "size": None}
            continue
        try:
            with open(path, "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            observation[name] = {
                "state": _STORE_UNREADABLE,
                "sha256": None,
                "size": None,
                "error": type(exc).__name__,
            }
            continue
        observation[name] = {
            "state": _STORE_PRESENT,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    return observation


def _refresh_generation(directory):
    """Advisory read of the refresh marker generation; never a write."""
    state = refresh_state.inspect_state(str(directory)).get("state") or {}
    generation = state.get("generation")
    return generation if isinstance(generation, int) else None


def _compare_protected_stores(before, after, *, directory, before_generation=None) -> dict:
    """
    Compare two observations of the same seven stores.

    A change is classified, never assumed: an advanced/`IN_PROGRESS` refresh
    marker means another writer was active, everything else is an unexplained
    mutation of the protected data.  Both fail the boundary — only the
    diagnostic differs.
    """
    deleted, appeared, replaced, unreadable = [], [], [], []
    for name in before:
        old, new = before[name], after[name]
        if new["state"] == _STORE_UNREADABLE:
            unreadable.append(name)
        elif old["state"] == _STORE_PRESENT and new["state"] == _STORE_MISSING:
            deleted.append(name)
        elif old["state"] == _STORE_MISSING and new["state"] == _STORE_PRESENT:
            appeared.append(name)
        elif old["state"] == _STORE_PRESENT and new["state"] == _STORE_PRESENT:
            if old["sha256"] != new["sha256"]:
                replaced.append(name)

    incomplete = sorted(
        name
        for name, seen in before.items()
        if seen["state"] != _STORE_PRESENT
    )
    changed = sorted(set(unreadable) | set(deleted) | set(appeared) | set(replaced))

    if unreadable:
        verdict = PROTECTED_STORE_UNREADABLE
    elif deleted or incomplete:
        verdict = PROTECTED_STORE_MISSING
    elif changed:
        marker_state = refresh_state.inspect_state(str(directory)).get("state") or {}
        concurrent = (
            marker_state.get("status") == refresh_state.STATUS_IN_PROGRESS
            or (
                before_generation is not None
                and marker_state.get("generation") != before_generation
            )
        )
        verdict = EXTERNAL_CONCURRENT_REFRESH if concurrent else UNAUTHORIZED_TEST_MUTATION
    else:
        verdict = PROTECTED_STORES_UNCHANGED

    return {
        "verdict": verdict,
        "changed": changed,
        "deleted": sorted(deleted),
        "appeared": sorted(appeared),
        "replaced": sorted(replaced),
        "unreadable": sorted(unreadable),
        "incomplete_at_t0": incomplete,
    }


def _read_only_boundary_operations(data_dir: str) -> dict:
    """
    The read-only work whose non-mutation property ER-24 actually verifies.

    These are the real read paths (`daily_loader` consumers and the advisory
    refresh-consistency reader). None of them may write a protected store.
    """
    matches = daily_loader.get_all_matches()
    meta = daily_loader.get_meta()
    report = refresh_state.consistency(str(data_dir))
    snapshots = (
        prediction_snapshots.get_snapshots_for_match(str(matches[0].get("id")))
        if matches and matches[0].get("id")
        else []
    )
    return {"matches": matches, "meta": meta, "consistency": report, "snapshots": snapshots}


def test_er_24_real_seven_stores_are_untouched():
    data = os.path.join(ROOT, "data")
    if not os.path.isfile(os.path.join(data, "daily_matches.json")):
        pytest.skip("no private runtime dataset in this checkout")

    t0_generation = _refresh_generation(data)
    t0 = _observe_protected_stores(data)

    incomplete = sorted(name for name, seen in t0.items() if seen["state"] != _STORE_PRESENT)
    assert not incomplete, (
        PROTECTED_STORE_MISSING
        + ": a real dataset is present but these protected stores are missing or "
        f"unreadable: {incomplete}"
    )

    observed = _read_only_boundary_operations(data)
    assert isinstance(observed["matches"], list)
    assert isinstance(observed["meta"], dict)
    assert observed["consistency"]["advisory"] is True

    t1 = _observe_protected_stores(data)
    report = _compare_protected_stores(
        t0, t1, directory=data, before_generation=t0_generation
    )
    assert report["verdict"] == PROTECTED_STORES_UNCHANGED, report


# ---------------------------------------------------------------------------
# ER-24 negative controls — isolated temporary stores only.
#
# No control below touches the repository's protected stores: every byte it
# mutates lives under pytest's `tmp_path`.
# ---------------------------------------------------------------------------

def _seed_protected_stores(directory, salt: str = "t0") -> None:
    for name in refresh_state.PROTECTED_STORES:
        with open(os.path.join(str(directory), name), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"store": name, "salt": salt}))


def test_er_24b_single_store_change_is_detected(tmp_path):
    _seed_protected_stores(tmp_path)
    t0 = _observe_protected_stores(tmp_path)
    (tmp_path / "settlements.json").write_text('{"tampered": true}', encoding="utf-8")
    report = _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)
    assert report["verdict"] == UNAUTHORIZED_TEST_MUTATION
    assert report["changed"] == ["settlements.json"]


def test_er_24c_multiple_store_changes_are_detected(tmp_path):
    _seed_protected_stores(tmp_path)
    t0 = _observe_protected_stores(tmp_path)
    for name in ("daily_matches.json", "odds_snapshots.json"):
        (tmp_path / name).write_text('{"tampered": true}', encoding="utf-8")
    report = _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)
    assert report["verdict"] == UNAUTHORIZED_TEST_MUTATION
    assert report["changed"] == ["daily_matches.json", "odds_snapshots.json"]


def test_er_24d_deleted_store_is_detected_and_is_not_unchanged(tmp_path):
    _seed_protected_stores(tmp_path)
    t0 = _observe_protected_stores(tmp_path)
    os.remove(os.path.join(str(tmp_path), "evaluation_rows.json"))
    after = _observe_protected_stores(tmp_path)
    report = _compare_protected_stores(t0, after, directory=tmp_path)
    assert report["verdict"] == PROTECTED_STORE_MISSING
    assert report["deleted"] == ["evaluation_rows.json"]
    assert after["evaluation_rows.json"]["state"] == _STORE_MISSING


def test_er_24e_partial_private_dataset_is_reported_not_skipped(tmp_path):
    _seed_protected_stores(tmp_path)
    os.remove(os.path.join(str(tmp_path), "team_strength.json"))
    t0 = _observe_protected_stores(tmp_path)
    report = _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)
    assert report["incomplete_at_t0"] == ["team_strength.json"]
    assert report["verdict"] == PROTECTED_STORE_MISSING


def test_er_24f_replacing_bytes_with_equal_length_is_detected(tmp_path):
    _seed_protected_stores(tmp_path)
    t0 = _observe_protected_stores(tmp_path)
    path = os.path.join(str(tmp_path), "calibrated.json")
    with open(path, "rb") as handle:
        payload = handle.read()
    with open(path, "wb") as handle:
        handle.write(payload[:-1] + (b" " if payload[-1:] != b" " else b"\n"))
    report = _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)
    assert report["verdict"] == UNAUTHORIZED_TEST_MUTATION
    assert report["changed"] == ["calibrated.json"]


def test_er_24g_independently_refreshed_starting_bytes_do_not_false_fail(tmp_path):
    """
    A legitimate refresh may leave *any* initial digest; only stability across
    the boundary is required.  This is exactly what a fixed historical
    constant cannot express.
    """
    _seed_protected_stores(tmp_path, salt="refreshed-2026-11-10")
    t0 = _observe_protected_stores(tmp_path)
    historical = "917912b5ad1988ad030f21b3478da012df9d51695fe10418fc70e2ec917c487c"
    mismatches = sorted(
        name for name, seen in t0.items() if seen["sha256"] == historical
    )
    assert mismatches == [], "fixture is not distinguishable from the historical constant"
    report = _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)
    assert report["verdict"] == PROTECTED_STORES_UNCHANGED
    assert report["verdict"] != HISTORICAL_HASH_MISMATCH


def test_er_24h_identical_t0_t1_bytes_pass(tmp_path):
    _seed_protected_stores(tmp_path)
    t0 = _observe_protected_stores(tmp_path)
    assert _compare_protected_stores(t0, _observe_protected_stores(tmp_path), directory=tmp_path)[
        "verdict"
    ] == PROTECTED_STORES_UNCHANGED


def test_er_24i_concurrent_refresh_is_attributed_not_misread_as_mutation(tmp_path):
    _seed_protected_stores(tmp_path)
    with open(os.path.join(str(tmp_path), refresh_state.STATE_FILENAME), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": refresh_state.STATE_SCHEMA_VERSION,
                "status": refresh_state.STATUS_COMPLETED,
                "generation": 7,
                "stores": {},
            },
            handle,
        )
    t0 = _observe_protected_stores(tmp_path)
    generation = _refresh_generation(tmp_path)

    with open(os.path.join(str(tmp_path), refresh_state.STATE_FILENAME), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": refresh_state.STATE_SCHEMA_VERSION,
                "status": refresh_state.STATUS_COMPLETED,
                "generation": 8,
                "stores": {},
            },
            handle,
        )
    (tmp_path / "daily_matches.json").write_text('{"refreshed": true}', encoding="utf-8")

    report = _compare_protected_stores(
        t0, _observe_protected_stores(tmp_path), directory=tmp_path, before_generation=generation
    )
    assert report["verdict"] == EXTERNAL_CONCURRENT_REFRESH
    assert report["changed"] == ["daily_matches.json"]
