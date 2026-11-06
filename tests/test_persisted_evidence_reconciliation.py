"""
Persisted-evidence reconciliation against the current NBA candidate dataset.

All candidates are synthetic and injected through the workbench's own entry
points: CI has no private dataset, and a synthetic fixture must never be
mistaken for genuine operator acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import nba_human_review_workbench as wb  # noqa: E402
from utils import evidence_store, refresh_state  # noqa: E402

PAIR = ["500j-2026-11-05-3316", "500lq-2026-11-05-周三316"]


def _record(tag: str = "1", **overrides) -> dict:
    record = {
        "schema_version": "1.0.0",
        "candidate_id": "NXD-test1",
        "candidate_fingerprint": "cf-1",
        "dataset_fingerprint": "df-1",
        "canonical_ids": list(PAIR),
        "decision": "SAME_EVENT",
        "reviewer": "operator-a",
        "decided_at": f"2026-11-06T0{tag}:00:00+08:00",
        "label_provenance": "HUMAN_REVIEWED",
        "evidence_reference": "local://secret-notes/should-not-leak",
        "notes": "PRIVATE-OPERATOR-NOTE",
    }
    record.update(overrides)
    return record


def _candidate(**overrides) -> dict:
    candidate = {
        "candidate_id": "NXD-test1",
        "candidate_fingerprint": "cf-1",
        "dataset_fingerprint": "df-1",
        "canonical_ids": list(PAIR),
        "nba_scope": True,
    }
    candidate.update(overrides)
    return candidate


@pytest.fixture()
def audit_env(tmp_path, monkeypatch):
    """Inject a synthetic dataset into the workbench entry points."""
    state = {"dataset_fingerprint": "df-1", "candidates": [_candidate()]}

    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda data_dir: object())
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda dataset: state["dataset_fingerprint"])
    monkeypatch.setattr(wb, "build_candidates", lambda dataset: list(state["candidates"]))

    def run(**kwargs):
        return wb.command_audit_persisted(None, **kwargs)

    return {"evidence": str(tmp_path / "evidence"), "state": state, "run": run}


# ---------------------------------------------------------------------------
# Binding classifications
# ---------------------------------------------------------------------------

def test_pr_01_bound_record_with_all_fingerprints_matching(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["status"] == "SUCCESS"
    assert report["evidence_store_status"] == "READABLE"
    assert report["stored_pair_count"] == 1
    assert report["stored_record_count"] == 1
    assert report["bound_record_count"] == 1
    assert report["pairs"][0]["current_binding"] == "BOUND_TO_CURRENT_CANDIDATE"
    assert report["pairs"][0]["persisted_state"] == wb.PERSISTED_BOUND
    # 绑定不等于获批：仍需人工复核，且永不授权生产。
    assert report["pairs"][0]["required_next_action"] == wb.ACTION_OPERATOR_REVIEW_REQUIRED
    assert report["production_authorized"] is False
    assert report["decision"] == "NO-GO"


def test_pr_02_dataset_fingerprint_change_is_stale(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["state"]["dataset_fingerprint"] = "df-2"

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["stale_record_count"] == 1
    assert report["bound_record_count"] == 0
    assert report["pairs"][0]["current_binding"] == "STALE_DATASET_FINGERPRINT"
    assert report["pairs"][0]["persisted_state"] == wb.PERSISTED_STALE
    assert report["pairs"][0]["required_next_action"] == wb.ACTION_REATTACH_REQUIRED


def test_pr_03_candidate_fingerprint_change_is_detected(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["state"]["candidates"] = [_candidate(candidate_fingerprint="cf-2")]

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["pairs"][0]["current_binding"] == "CANDIDATE_FINGERPRINT_CHANGED"
    assert report["stale_record_count"] == 1


def test_pr_04_candidate_id_change_is_detected(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["state"]["candidates"] = [_candidate(candidate_id="NXD-test2")]

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["pairs"][0]["current_binding"] == "CANDIDATE_ID_CHANGED"
    assert report["stale_record_count"] == 1


def test_pr_05_06_canonical_pair_change_or_absence_is_missing(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["state"]["candidates"] = [
        _candidate(canonical_ids=["500j-2026-11-05-3317", "500lq-2026-11-05-周三317"])
    ]

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["stored_pair_count"] == 1
    assert report["missing_candidate_count"] == 1
    assert report["pairs"][0]["current_binding"] == "CANDIDATE_NOT_IN_CURRENT_DATASET"
    assert report["pairs"][0]["persisted_state"] == wb.PERSISTED_STALE
    # 候选侧也必须显式给出"无已存证据"，不得猜测绑定。
    assert report["candidates"][0]["persisted_evidence_state"] == wb.PERSISTED_NO_STORED_EVIDENCE


def test_pr_07_multiple_candidates_sharing_pair_is_ambiguous(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["state"]["candidates"] = [
        _candidate(),
        _candidate(candidate_id="NXD-test9", candidate_fingerprint="cf-9"),
    ]

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["ambiguous_pair_count"] == 1
    assert report["pairs"][0]["current_binding"] == "AMBIGUOUS_IDENTITY"
    assert report["pairs"][0]["persisted_state"] == wb.PERSISTED_AMBIGUOUS
    assert report["bound_record_count"] == 0


def test_pr_08_contradictory_decisions_are_preserved(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    evidence_store.append_record(
        _record("2", decision="DIFFERENT_EVENT"), configured_dir=audit_env["evidence"]
    )

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["stored_record_count"] == 2
    assert report["conflicting_pair_count"] == 1
    assert report["pairs"][0]["conflict"] is True
    assert report["pairs"][0]["decision_count"] == 2
    assert report["pairs"][0]["persisted_state"] == wb.PERSISTED_CONFLICT
    assert report["pairs"][0]["required_next_action"] == wb.ACTION_ADJUDICATE_REQUIRED


def test_pr_09_corrupt_shard_fails_closed_without_repair(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    key = evidence_store.pair_key(PAIR)
    path = os.path.join(audit_env["evidence"], evidence_store.RECORD_DIRNAME, f"{key}.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write('{"schema_version": "nope", "records": []}')
    before = pathlib_read(path)

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["corrupt_shard_count"] == 1
    assert report["stored_record_count"] == 0
    assert report["pairs"][0]["status"] == "CORRUPT"
    assert pathlib_read(path) == before, "a corrupt shard must never be repaired or rewritten"


def pathlib_read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_pr_10_missing_evidence_directory_is_reported_not_created(tmp_path, audit_env):
    target = tmp_path / "does-not-exist"

    report = audit_env["run"](evidence_dir=str(target))

    assert report["status"] == "SUCCESS"
    assert report["evidence_store_status"] == "NOT_PROVISIONED"
    assert report["evidence_directory_exists"] is False
    assert report["stored_pair_count"] == 0
    assert not target.exists(), "read-only reconciliation must not create the store"


def test_pr_11_evidence_survives_process_restart(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from utils import evidence_store as es\n"
        f"pairs = es.list_pairs(r'{audit_env['evidence']}')\n"
        f"payload = es.load_pair(r'{audit_env['evidence']}', pairs[0]['pair_key'])\n"
        "print(json.dumps({'pairs': len(pairs), 'records': len(payload['records'])}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        capture_output=True, text=True, cwd=ROOT,
        env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"pairs": 1, "records": 1}


def test_pr_12_identical_retry_does_not_duplicate(audit_env):
    first = evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    second = evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])

    assert first["status"] == "APPENDED"
    assert second["status"] == "IDEMPOTENT_DUPLICATE"

    report = audit_env["run"](evidence_dir=audit_env["evidence"])
    assert report["stored_record_count"] == 1


def test_pr_13_concurrent_reads_never_claim_unsafe_binding(audit_env, tmp_path):
    """Readers may observe 0..N records, but never more than actually exist."""
    evidence_dir = audit_env["evidence"]
    os.makedirs(evidence_dir, exist_ok=True)
    child = tmp_path / "audit_child.py"
    child.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from tools import nba_human_review_workbench as wb\n"
        "from utils import evidence_store\n"
        f"wb._load_dataset_or_error = lambda d: object()\n"
        f"wb.dataset_fingerprint = lambda ds: 'df-1'\n"
        "wb.build_candidates = lambda ds: [{\n"
        "  'candidate_id': 'NXD-test1', 'candidate_fingerprint': 'cf-1',\n"
        "  'dataset_fingerprint': 'df-1', 'nba_scope': True,\n"
        f"  'canonical_ids': {PAIR!r},\n"
        "}]\n"
        f"doc = wb.command_audit_persisted(None, evidence_dir=r'{evidence_dir}')\n"
        "print(json.dumps({'records': doc['stored_record_count'],"
        " 'status': doc['status']}))\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
    reads = [
        subprocess.Popen([sys.executable, "-X", "utf8", str(child)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         cwd=ROOT, env=env)
        for _ in range(3)
    ]
    for _ in range(3):
        evidence_store.append_record(_record("3"), configured_dir=evidence_dir)
        evidence_store.append_record(_record("4", decision="DIFFERENT_EVENT"),
                                     configured_dir=evidence_dir)

    observed = []
    for proc in reads:
        stdout, stderr = proc.communicate(timeout=180)
        assert proc.returncode == 0, stderr
        observed.append(json.loads(stdout.strip().splitlines()[-1]))

    final = len(evidence_store.load_pair(evidence_dir, evidence_store.pair_key(PAIR))["records"])
    for item in observed:
        assert item["status"] == "SUCCESS"
        assert 0 <= item["records"] <= final


def test_pr_14_partial_persistence_is_observable(tmp_path, monkeypatch):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(_record("5"), configured_dir=evidence)
    # Same (reviewer, decided_at) with different content -> conflicting rewrite.
    blocked = _record("5", decision="DIFFERENT_EVENT")
    fresh = _record("6")

    monkeypatch.setattr(
        wb,
        "command_validate",
        lambda *a, **kw: {
            "real_data_available": True,
            "validation_status": "SUCCESS",
            "review_rejected_count": 0,
            "evidence_rejected_count": 0,
            "rejected": [],
            "dataset_fingerprint": "df-1",
            "accepted": {"a": blocked, "b": fresh},
        },
    )
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda data_dir: object())
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda ds: "df-1")

    result = wb.command_persist(None, "x.json", evidence_dir=evidence, confirm=True)

    assert result["status"] == "PARTIAL"
    assert result["persisted_count"] == 1
    assert result["error_count"] == 1
    assert {item["outcome"] for item in result["persisted"]} == {"APPENDED"}
    assert result["errors"][0]["reason_code"] == "CONFLICTING_REWRITE"
    assert result["batch_atomic"] is False


def test_pr_15_source_retention_does_not_create_decisions(tmp_path, audit_env):
    source = tmp_path / "evidence-source.json"
    source.write_text('{"note": "operator supplied"}', encoding="utf-8")
    manifest = evidence_store.retain_source_copy(
        str(source), configured_dir=audit_env["evidence"]
    )

    assert manifest["verified_by_human"] is False
    report = audit_env["run"](evidence_dir=audit_env["evidence"])
    assert report["stored_record_count"] == 0
    assert report["genuine_operator_decision_count"] == 0


def _digests(directory: str) -> dict[str, str]:
    out = {}
    for name in refresh_state.PROTECTED_STORES:
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            out[name] = "ABSENT"
            continue
        with open(path, "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def test_pr_16_reconciliation_changes_no_protected_store(isolated_data_dir, audit_env):
    for name in refresh_state.PROTECTED_STORES:
        with open(os.path.join(str(isolated_data_dir), name), "w", encoding="utf-8") as handle:
            json.dump({"version": 1}, handle)

    before = _digests(str(isolated_data_dir))
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    audit_env["run"](evidence_dir=audit_env["evidence"])

    assert _digests(str(isolated_data_dir)) == before


def test_pr_17_outputs_do_not_disclose_record_content(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    report = audit_env["run"](evidence_dir=audit_env["evidence"])
    markdown = wb.render_audit_persisted_markdown(report)

    serialized = json.dumps(report, ensure_ascii=False) + markdown
    assert "PRIVATE-OPERATOR-NOTE" not in serialized
    assert "secret-notes" not in serialized
    # 结构性断言：报告里不得出现 reviewer / notes 字段本身。
    assert '"reviewer"' not in serialized
    assert '"notes"' not in serialized
    assert '"decided_at"' not in serialized


def test_pr_18_synthetic_records_cannot_satisfy_genuine_acceptance(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])

    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["stored_record_count"] == 1
    assert report["genuine_operator_decision_count"] == 0
    assert "no operator-authored file was supplied" in report["genuine_operator_decision_note"]
    assert report["stored_provenance_classification"] == "SELF_DECLARED_LOCAL_HUMAN_REVIEW"


def test_pr_19_production_flags_remain_false(audit_env):
    evidence_store.append_record(_record(), configured_dir=audit_env["evidence"])
    report = audit_env["run"](evidence_dir=audit_env["evidence"])

    assert report["executable"] is False
    assert report["apply_available"] is False
    assert report["production_write_authorization"] is False
    assert report["production_authorized"] is False
    assert report["surviving_canonical_id"] is None
    assert report["decision"] == "NO-GO"


def test_pr_20_market_admission_and_ui_contracts_unchanged(isolated_data_dir):
    from utils import daily_loader
    from utils.market_coverage import has_usable_market_odds

    assert has_usable_market_odds({"odds": {}}) is False
    assert has_usable_market_odds(
        {"sport": "basketball", "odds": {"home_win": 0, "away_win": 2.0}}
    ) is False
    assert has_usable_market_odds(
        {"sport": "basketball", "odds": {"home_win": 1.5, "away_win": 2.4}}
    ) is True
    # 足球缺平局价 -> 不是完整 1X2，不得准入。
    assert has_usable_market_odds({"odds": {"home_win": 1.5, "away_win": 2.4}}) is False

    daily_loader.save_json(
        "daily_matches.json",
        {
            "meta": {"last_refresh": "2026-11-06T09:00:00+08:00", "sources": {}, "total": 1},
            "matches": [
                {
                    "id": "500lq-2026-11-06-周四306",
                    "sport": "basketball",
                    "league": "NBA",
                    "date": "2026-11-06",
                    "time": "10:00",
                    "status": "upcoming",
                    "home": "奥兰多魔术",
                    "away": "菲尼克斯太阳",
                    "odds": {"home_win": 1.9, "away_win": 1.9},
                }
            ],
        },
    )
    by_sport = daily_loader.get_available_dates_by_sport()
    assert by_sport["basketball"] == ["2026-11-06"]
    assert by_sport["football"] == []


# ---------------------------------------------------------------------------
# Persist retry / busy / restart
# ---------------------------------------------------------------------------

def test_pr_21_busy_evidence_writer_is_reported(tmp_path):
    from utils.concurrency import acquire_sidecar_lock

    evidence = str(tmp_path / "evidence")
    os.makedirs(evidence, exist_ok=True)
    with acquire_sidecar_lock(
        evidence_store.EVIDENCE_LOCK_NAME, data_dir=evidence, timeout=5
    ):
        with pytest.raises(evidence_store.EvidenceStoreError) as excinfo:
            evidence_store.append_record(_record(), configured_dir=evidence)
    assert excinfo.value.code == "EVIDENCE_WRITER_BUSY"


def test_pr_22_repeated_persist_after_restart_is_idempotent(tmp_path, monkeypatch):
    evidence = str(tmp_path / "evidence")
    payload = {
        "real_data_available": True,
        "validation_status": "SUCCESS",
        "review_rejected_count": 0,
        "evidence_rejected_count": 0,
        "rejected": [],
        "dataset_fingerprint": "df-1",
        "accepted": {"a": _record()},
    }
    monkeypatch.setattr(wb, "command_validate", lambda *a, **kw: dict(payload))
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda data_dir: object())
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda ds: "df-1")

    first = wb.command_persist(None, "x.json", evidence_dir=evidence, confirm=True)
    assert first["status"] == "PERSISTED"
    assert first["freshness_state"] == "UNCHANGED"

    # Fresh process would re-run the same command; emulate by calling again.
    second = wb.command_persist(None, "x.json", evidence_dir=evidence, confirm=True)
    assert second["status"] == "PERSISTED"
    assert second["persisted"][0]["outcome"] == "IDEMPOTENT_DUPLICATE"

    assert len(
        evidence_store.load_pair(evidence, evidence_store.pair_key(PAIR))["records"]
    ) == 1


def test_pr_23_freshness_race_between_validate_and_persist(tmp_path, monkeypatch):
    """A dataset change during persistence must not be reported as current."""
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
            "dataset_fingerprint": "df-old",
            "accepted": {"a": _record(dataset_fingerprint="df-old")},
        },
    )
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda data_dir: object())
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda ds: "df-new")

    result = wb.command_persist(None, "x.json", evidence_dir=evidence, confirm=True)

    assert result["freshness_state"] == "DATASET_CHANGED_DURING_PERSIST"
    assert result["status"] == "PARTIAL"
    assert result["dataset_fingerprint_at_validation"] == "df-old"
    assert result["dataset_fingerprint_after_persist"] == "df-new"
    # 记录已落盘且历史不可抹除，但不得声称"当前有效"。
    assert result["persisted_count"] == 1
