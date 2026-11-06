"""
Result integrity of the read-only reconciliation and of the persistence report.

Two defects are pinned here:

* a completed inspection containing corrupt evidence was reported as a clean
  reconciliation (``status == "SUCCESS"`` plus process exit code 0);
* a durable append whose post-write freshness check never ran was reported as
  an unqualified success (``status == "PERSISTED"`` plus exit code 0).

Everything runs against synthetic, isolated stores. A synthetic fixture never
proves genuine human-review acceptance.
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
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import nba_human_review_workbench as wb  # noqa: E402
from tools.real_sports_duplicate_review import REVIEW_SCHEMA_VERSION  # noqa: E402
from utils import evidence_store, refresh_state  # noqa: E402

DATE = "2030-05-01"
PAIR = ["500j-2030-05-01-6308", "500lq-2030-05-01-周六308"]
OTHER_PAIR = ["500j-2030-05-01-6309", "500lq-2030-05-01-周六309"]
REVIEWER = "operator-ri"
NOTE_MARKER = "PRIVATE-OPERATOR-NOTE-MUST-NOT-LEAK"
REFERENCE_MARKER = "local://secret-notes-must-not-leak"


# ---------------------------------------------------------------------------
# Synthetic fixtures
# ---------------------------------------------------------------------------

def write_dataset(root) -> str:
    """One NBA pair: the lottery feed lists the reversed order of the live feed."""
    root = str(root)
    os.makedirs(root, exist_ok=True)
    matches = [
        {
            "id": "500lq-2030-05-01-周六308",
            "sport": "basketball",
            "league": "NBA",
            "date": DATE,
            "time": "03:00",
            "home": "波特兰开拓者",
            "away": "犹他爵士",
            "status": "upcoming",
            "jczq_no": "周六308",
            "score": None,
            "odds": None,
        },
        {
            "id": "500j-2030-05-01-6308",
            "sport": "basketball",
            "league": "美国职业篮球联盟",
            "date": DATE,
            "time": "00:00",
            "home": "犹他爵士",
            "away": "波特兰开拓者",
            "status": "upcoming",
            "jczq_no": "6308",
            "score": None,
            "odds": None,
        },
    ]
    with open(os.path.join(root, "daily_matches.json"), "w", encoding="utf-8") as handle:
        json.dump({"matches": matches, "meta": {}}, handle, ensure_ascii=False, sort_keys=True)
    return root


def store_hashes(root) -> dict[str, str]:
    out = {}
    for name in refresh_state.PROTECTED_STORES:
        path = os.path.join(str(root), name)
        if not os.path.isfile(path):
            out[name] = "ABSENT"
            continue
        with open(path, "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def evidence_bytes(directory) -> dict[str, bytes]:
    out = {}
    for base, _dirs, files in os.walk(str(directory)):
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                out[os.path.relpath(path, str(directory))] = handle.read()
    return out


def durable_record(tag: str = "1", *, pair=None, **overrides) -> dict:
    record = {
        "schema_version": "1.0.0",
        "candidate_id": "NXD-synthetic",
        "candidate_fingerprint": "cf-1",
        "dataset_fingerprint": "df-1",
        "canonical_ids": list(pair or PAIR),
        "decision": "SAME_EVENT",
        "reviewer": REVIEWER,
        "decided_at": f"2026-11-06T0{tag}:00:00+08:00",
        "label_provenance": "HUMAN_REVIEWED",
        "evidence_reference": REFERENCE_MARKER,
        "notes": NOTE_MARKER,
    }
    record.update(overrides)
    return record


def shard_path(directory, pair) -> str:
    key = evidence_store.pair_key(pair)
    return os.path.join(str(directory), evidence_store.RECORD_DIRNAME, f"{key}.json")


def write_raw_shard(directory, pair, text: str) -> str:
    path = shard_path(directory, pair)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


class StoreShape:
    """Named evidence-store constructions used by the audit matrix."""

    @staticmethod
    def healthy(directory):
        evidence_store.append_record(durable_record(), configured_dir=directory)

    @staticmethod
    def corrupt_only(directory):
        write_raw_shard(directory, PAIR, '{"schema_version": "nope", "records": []}')

    @staticmethod
    def mixed(directory):
        StoreShape.healthy(directory)
        write_raw_shard(directory, OTHER_PAIR, "{ this is not json")

    @staticmethod
    def unsupported_schema(directory):
        write_raw_shard(
            directory,
            PAIR,
            json.dumps(
                {
                    "schema_version": "9.9.9",
                    "kind": "SOMETHING_ELSE",
                    "pair_key": evidence_store.pair_key(PAIR),
                    "canonical_ids": PAIR,
                    "records": [],
                }
            ),
        )

    @staticmethod
    def corrupt_history(directory):
        StoreShape.healthy(directory)
        path = shard_path(directory, PAIR)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload["records"][0]["label_provenance"] = "MODEL_GENERATED"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def empty(directory):
        os.makedirs(str(directory), exist_ok=True)


# ---------------------------------------------------------------------------
# A/B — audit result fail-closed matrix
# ---------------------------------------------------------------------------

SHAPES = [
    # label, builder, expected verdict, expected CLI exit, expected corrupt count
    ("healthy", StoreShape.healthy, wb.RECONCILIATION_CLEAN, 0, 0),
    ("corrupt_only", StoreShape.corrupt_only, wb.RECONCILIATION_WITH_CORRUPTION, 7, 1),
    ("mixed", StoreShape.mixed, wb.RECONCILIATION_WITH_CORRUPTION, 7, 1),
    ("unsupported_schema", StoreShape.unsupported_schema, wb.RECONCILIATION_WITH_CORRUPTION, 7, 1),
    ("corrupt_history", StoreShape.corrupt_history, wb.RECONCILIATION_WITH_CORRUPTION, 7, 1),
    ("empty", StoreShape.empty, wb.RECONCILIATION_CLEAN, 0, 0),
]


@pytest.fixture()
def data_dir(tmp_path):
    return write_dataset(tmp_path / "data")


def test_ri_01_healthy_store_reconciles_clean(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))
    before = evidence_bytes(evidence)

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    assert report["status"] == "SUCCESS"
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_CLEAN
    assert report["strict_acceptance_ready"] is True
    assert report["reconciliation_complete"] is True
    assert report["corrupt_shard_count"] == 0
    assert report["stored_record_count"] == 1
    assert report["evidence_coverage_status"] == "STORED_EVIDENCE_PRESENT"
    assert evidence_bytes(evidence) == before


def test_ri_02_missing_directory_is_input_unavailable(tmp_path):
    target = tmp_path / "never-provisioned"

    report = wb.command_audit_persisted(str(write_dataset(tmp_path / "data")),
                                        evidence_dir=str(target))

    assert report["status"] == "SUCCESS"  # the observation itself succeeded
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_INPUT_UNAVAILABLE
    assert report["reconciliation_complete"] is False
    assert report["strict_acceptance_ready"] is False
    # 观测成功不等于对账完成，更不等于有人工复核覆盖。
    assert report["evidence_coverage_status"] == "NO_STORED_EVIDENCE"
    assert report["stored_record_count"] == 0
    assert report["genuine_operator_decision_count"] == 0
    assert not target.exists(), "the read-only audit must not create the store"


def test_ri_03_empty_directory_is_clean_but_carries_no_coverage(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.empty(evidence)

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    assert report["reconciliation_verdict"] == wb.RECONCILIATION_CLEAN
    assert report["reconciliation_reason"] == "NO_SHARDS_PRESENT"
    # 干净的对账必须与"有人工复核证据"分开陈述。
    assert report["evidence_coverage_status"] == "NO_STORED_EVIDENCE"
    assert report["stored_record_count"] == 0
    assert report["bound_record_count"] == 0
    assert report["genuine_operator_decision_count"] == 0


def test_ri_04_corrupt_only_store_never_reports_clean(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.corrupt_only(str(evidence))
    path = shard_path(evidence, PAIR)
    before = open(path, "rb").read()

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    # 修复前：status=SUCCESS 且 CLI 退出码 0，调用方会把损坏读成"对账干净"。
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_WITH_CORRUPTION
    assert report["strict_acceptance_ready"] is False
    assert report["corrupt_shard_count"] == 1
    assert report["evidence_store_status"] == "UNREADABLE"
    assert open(path, "rb").read() == before, "a corrupt shard must never be rewritten"


def test_ri_05_mixed_store_keeps_valid_records_observable(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.mixed(str(evidence))

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    # 干净分片仍然可见，但整体不得被称为"已完全对账"。
    assert report["stored_record_count"] == 1
    assert report["stored_pair_count"] == 1
    assert report["corrupt_shard_count"] == 1
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_WITH_CORRUPTION
    assert report["reconciliation_complete"] is True
    assert report["strict_acceptance_ready"] is False
    assert report["operator_action_required_count"] >= 1
    states = {item["status"] for item in report["pairs"]}
    assert states == {"OK", "CORRUPT"}


def test_ri_06_unsupported_shard_schema_fails_closed(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.unsupported_schema(str(evidence))
    path = shard_path(evidence, PAIR)
    before = open(path, "rb").read()

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    assert report["corrupt_shard_count"] == 1
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_WITH_CORRUPTION
    assert report["pairs"][0]["status"] == "CORRUPT"
    assert report["pairs"][0]["reason_code"] == "CORRUPT_EVIDENCE_FILE"
    assert open(path, "rb").read() == before


def test_ri_07_corrupt_historical_record_fails_closed(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.corrupt_history(str(evidence))

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    assert report["corrupt_shard_count"] == 1
    assert report["stored_record_count"] == 0
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_WITH_CORRUPTION
    assert report["strict_acceptance_ready"] is False


@pytest.mark.parametrize("label,builder,verdict,exit_code,corrupt", SHAPES)
def test_ri_08_api_verdict_and_cli_exit_agree(
    tmp_path, data_dir, capsys, label, builder, verdict, exit_code, corrupt
):
    evidence = tmp_path / label
    builder(str(evidence))

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))
    assert report["reconciliation_verdict"] == verdict
    assert report["corrupt_shard_count"] == corrupt

    capsys.readouterr()
    code = wb.main(
        ["audit-persisted", "--data-dir", data_dir, "--evidence-dir", str(evidence)]
    )
    printed = capsys.readouterr().out.strip().splitlines()[-1]
    summary = json.loads(printed)
    assert code == exit_code
    # 决定性字段必须出现在 CLI 摘要里，操作者不必去翻详细 JSON。
    assert summary["reconciliation_verdict"] == verdict
    assert summary["corrupt_shard_count"] == corrupt
    assert summary["strict_acceptance_ready"] is (verdict == wb.RECONCILIATION_CLEAN)
    assert summary["production_authorized"] is False


def test_ri_09_cli_exit_codes_are_distinct_for_each_failure_class(tmp_path, data_dir):
    clean = tmp_path / "clean"
    StoreShape.healthy(str(clean))
    corrupt = tmp_path / "corrupt"
    StoreShape.corrupt_only(str(corrupt))
    absent = tmp_path / "absent"

    codes = {}
    for name, path in (("clean", clean), ("corrupt", corrupt), ("absent", absent)):
        codes[name] = wb.main(
            ["audit-persisted", "--data-dir", data_dir, "--evidence-dir", str(path)]
        )

    assert codes["clean"] == 0
    assert codes["corrupt"] == wb.EXIT_RECONCILIATION_NOT_CLEAN
    assert codes["absent"] == wb.EXIT_RECONCILIATION_NOT_CLEAN
    # 不得与既有含义复用的退出码冲突。
    assert wb.EXIT_RECONCILIATION_NOT_CLEAN not in (
        wb.EXIT_OK, wb.EXIT_INPUT_ERROR, wb.EXIT_PROTECTED_PATH, wb.EXIT_REVIEW_REJECTED
    )


def test_ri_10_unavailable_dataset_is_reported_not_guessed(tmp_path):
    report = wb.command_audit_persisted(str(tmp_path / "no-data"), evidence_dir=None)

    assert report["status"] == "NOT_AVAILABLE"
    assert report["audit_completed"] is False
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_INPUT_UNAVAILABLE
    assert report["strict_acceptance_ready"] is False


# ---------------------------------------------------------------------------
# B — durability vs freshness
# ---------------------------------------------------------------------------

def review_file(data_dir, tmp_path, name="reviews.json") -> str:
    """Operator-authored review file bound to the current synthetic dataset."""
    queue = wb.build_review_queue(wb.load_dataset(str(data_dir)))
    entry = queue["entries"][0]
    document = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "reviews": [
            {
                "candidate_id": entry["candidate_id"],
                "candidate_fingerprint": entry["candidate_fingerprint"],
                "dataset_fingerprint": entry["dataset_fingerprint"],
                "canonical_ids": list(entry["canonical_ids"]),
                "decision": "SAME_EVENT",
                "label_provenance": "HUMAN_REVIEWED",
                "reviewer": REVIEWER,
                "decided_at": "2030-05-02T00:00:00+08:00",
                "evidence_reference": REFERENCE_MARKER,
                "notes": NOTE_MARKER,
                "schema_version": REVIEW_SCHEMA_VERSION,
            }
        ],
    }
    path = os.path.join(str(tmp_path), name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, sort_keys=True)
    return path


def _fingerprint_sequence(monkeypatch, values):
    """
    Make ``dataset_fingerprint`` answer with a scripted sequence.

    The first call is the validation pass, the second is the post-write recheck.
    """
    calls = {"n": 0}

    def fake(dataset):
        index = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[index]

    monkeypatch.setattr(wb, "dataset_fingerprint", fake)
    return calls


def test_ri_11_verified_unchanged_dataset_is_the_only_clean_persist(
    tmp_path, data_dir, monkeypatch, capsys
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    real_fingerprint = wb.dataset_fingerprint(dataset)
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint])

    capsys.readouterr()
    code = wb.main(
        ["persist", "--data-dir", data_dir, "--reviews", reviews,
         "--evidence-dir", evidence, "--confirm"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == 0
    assert summary["status"] == "PERSISTED"
    assert summary["persistence_outcome"] == "PERSISTED"
    assert summary["verification_outcome"] == "VERIFIED_COMPLETE"
    assert summary["persisted_count"] == 1
    assert summary["freshness_state"] == "UNCHANGED"
    assert summary["freshness_verified"] is True
    assert summary["batch_atomic"] is False
    assert summary["production_authorized"] is False


def test_ri_12_dataset_change_during_persist_is_partial_and_unverified(
    tmp_path, data_dir, monkeypatch, capsys
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    real_fingerprint = wb.dataset_fingerprint(dataset)
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint, "df-after-refresh"])

    capsys.readouterr()
    code = wb.main(
        ["persist", "--data-dir", data_dir, "--reviews", reviews,
         "--evidence-dir", evidence, "--confirm"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == wb.EXIT_PERSIST_INCOMPLETE
    assert summary["status"] == "PARTIAL"
    assert summary["persistence_outcome"] == "PERSISTED"
    assert summary["verification_outcome"] == "INCOMPLETE"
    assert summary["freshness_state"] == "DATASET_CHANGED_DURING_PERSIST"
    assert summary["freshness_verified"] is False
    # 记录已耐久落盘：历史不可抹除，也不回滚。
    assert summary["persisted_count"] == 1


def test_ri_13_missing_validation_fingerprint_cannot_verify_freshness(
    tmp_path, data_dir, monkeypatch, capsys
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    real = wb.command_validate
    monkeypatch.setattr(
        wb, "command_validate", lambda *a, **kw: {**real(*a, **kw), "dataset_fingerprint": None}
    )

    capsys.readouterr()
    code = wb.main(
        ["persist", "--data-dir", data_dir, "--reviews", reviews,
         "--evidence-dir", evidence, "--confirm"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == wb.EXIT_PERSIST_INCOMPLETE
    assert summary["status"] == "PARTIAL"
    assert summary["freshness_state"] == "VALIDATION_FINGERPRINT_MISSING"
    assert summary["freshness_verified"] is False
    assert summary["persisted_count"] == 1


def test_ri_14_post_write_reload_failure_cannot_verify_freshness(
    tmp_path, data_dir, monkeypatch, capsys
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    real = wb._load_dataset_or_error
    calls = {"n": 0}

    def flaky(data_dir_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            raise wb.AuditInputError("dataset reload unavailable")
        return real(data_dir_arg)

    monkeypatch.setattr(wb, "_load_dataset_or_error", flaky)

    capsys.readouterr()
    code = wb.main(
        ["persist", "--data-dir", data_dir, "--reviews", reviews,
         "--evidence-dir", evidence, "--confirm"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == wb.EXIT_PERSIST_INCOMPLETE
    assert summary["status"] == "PARTIAL"
    assert summary["freshness_state"] == "RECHECK_UNAVAILABLE"
    assert summary["freshness_verified"] is False


def test_ri_15_post_write_fingerprint_unavailable_is_not_a_change(
    tmp_path, data_dir, monkeypatch
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    real_fingerprint = wb.dataset_fingerprint(dataset)
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint, None])

    result = wb.command_persist(
        data_dir, reviews, evidence_dir=evidence, confirm=True
    )

    # 无法计算不得被谎报成"数据集已变更"。
    assert result["freshness_state"] == "RECHECK_UNAVAILABLE"
    assert result["freshness_verified"] is False
    assert result["freshness_check_completed"] is False
    assert result["status"] == "PARTIAL"


def test_ri_16_persisted_bytes_survive_failed_freshness_validation(
    tmp_path, data_dir, monkeypatch
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    real = wb._load_dataset_or_error
    calls = {"n": 0}

    def flaky(data_dir_arg):
        calls["n"] += 1
        if calls["n"] > 1:
            raise wb.AuditInputError("dataset reload unavailable")
        return real(data_dir_arg)

    monkeypatch.setattr(wb, "_load_dataset_or_error", flaky)

    result = wb.command_persist(data_dir, reviews, evidence_dir=evidence, confirm=True)

    assert result["status"] == "PARTIAL"
    assert result["durable_record_written"] is True
    assert result["persisted_record_count"] == 1
    # 没有回滚、没有删除：字节仍在盘上，历史不可改写。
    pairs = evidence_store.list_pairs(evidence)
    assert len(pairs) == 1
    payload = evidence_store.load_pair(evidence, pairs[0]["pair_key"])
    assert len(payload["records"]) == 1
    assert payload["records"][0]["reviewer"] == REVIEWER


def test_ri_17_idempotent_duplicate_creates_no_fresh_evidence(
    tmp_path, data_dir, monkeypatch
):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    real_fingerprint = wb.dataset_fingerprint(dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint])

    first = wb.command_persist(data_dir, reviews, evidence_dir=evidence, confirm=True)
    assert first["persisted"][0]["outcome"] == "APPENDED"

    monkeypatch.undo()
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint])
    second = wb.command_persist(data_dir, reviews, evidence_dir=evidence, confirm=True)

    assert second["persisted"][0]["outcome"] == "IDEMPOTENT_DUPLICATE"
    assert second["persisted_count"] == 1
    assert len(evidence_store.load_pair(evidence, evidence_store.pair_key(PAIR))["records"]) == 1

    # 重复提交不产生新证据，也不能绕过指纹校验。
    monkeypatch.undo()
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint, None])
    third = wb.command_persist(data_dir, reviews, evidence_dir=evidence, confirm=True)
    assert third["freshness_verified"] is False
    assert third["status"] == "PARTIAL"


def test_ri_18_partial_multi_record_operation_keeps_what_was_written(
    tmp_path, data_dir, monkeypatch
):
    evidence = str(tmp_path / "evidence")
    evidence_store.append_record(
        durable_record("5", pair=PAIR), configured_dir=evidence
    )
    reviews = os.path.join(str(tmp_path), "reviews.json")
    first = durable_record("5", pair=PAIR, decision="SAME_EVENT")
    second = durable_record("5", pair=PAIR, decision="DIFFERENT_EVENT")
    fresh = durable_record("6", pair=OTHER_PAIR)

    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: None)
    monkeypatch.setattr(wb, "dataset_fingerprint", lambda _d: "df-1")
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
            "accepted": {"a": first, "b": second, "c": fresh},
        },
    )

    result = wb.command_persist(data_dir, reviews, evidence_dir=evidence, confirm=True)

    assert result["status"] == "PARTIAL"
    assert result["persistence_outcome"] == "PARTIAL"
    assert result["persisted_count"] == 2
    assert result["error_count"] == 1
    assert result["errors"][0]["reason_code"] == "CONFLICTING_REWRITE"
    assert result["batch_atomic"] is False
    assert result["durable_record_written"] is True


def test_ri_19_source_retention_failure_is_reported(tmp_path, data_dir, monkeypatch):
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    real_fingerprint = wb.dataset_fingerprint(dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint])

    result = wb.command_persist(
        data_dir,
        reviews,
        evidence_dir=evidence,
        confirm=True,
        source_references=(os.path.join(str(tmp_path), "missing-source.json"),),
    )

    assert result["error_count"] == 1
    assert result["errors"][0]["reason_code"] == "SOURCE_REFERENCE_NOT_FOUND"
    assert result["persisted_count"] == 1
    # 留存失败让整体验收不再是"已验证完成"，但已写入的决定仍然保留。
    assert result["status"] == "PARTIAL"
    assert result["durable_record_written"] is True


def test_ri_19b_attempted_write_that_wrote_nothing_exits_incomplete(
    tmp_path, data_dir, monkeypatch, capsys
):
    """A write attempt that appended nothing is not an input error."""
    evidence = str(tmp_path / "evidence")
    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    real_fingerprint = wb.dataset_fingerprint(dataset)
    _fingerprint_sequence(monkeypatch, [real_fingerprint])
    # `candidate_class` is a queue-side diagnostic the durable schema rejects, so a
    # record carrying it fails closed at append time.
    monkeypatch.setattr(
        wb, "_durable_record", lambda record: {**record, "candidate_class": "UNCLASSIFIED"}
    )

    capsys.readouterr()
    code = wb.main(
        ["persist", "--data-dir", data_dir, "--reviews", reviews,
         "--evidence-dir", evidence, "--confirm"]
    )
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert code == wb.EXIT_PERSIST_INCOMPLETE
    assert summary["status"] == "FAILED"
    assert summary["persistence_outcome"] == "FAILED"
    assert summary["persisted_count"] == 0
    assert summary["error_count"] == 1
    # 尝试过但没写下去：既不是"前置拒绝"，也不是"已验证完成"。
    assert summary["verification_outcome"] == "FAILED"


# ---------------------------------------------------------------------------
# C — concurrent audit and append
# ---------------------------------------------------------------------------
_CHILD = r'''
import json, os, sys, time
sys.path.insert(0, r"{root}")
from tools import nba_human_review_workbench as wb
from utils import evidence_store

ready, go, evidence, data_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
real = evidence_store.list_pairs
state = {{"calls": 0}}

def gated(evidence_dir=None):
    pairs = real(evidence_dir)
    state["calls"] += 1
    if state["calls"] == 1:
        # Explicit file barrier: the parent mutates the store only after this
        # point, i.e. strictly between enumeration and the per-shard reads.
        with open(ready, "w", encoding="utf-8") as handle:
            handle.write("ready")
        deadline = time.monotonic() + 60.0
        while not os.path.exists(go):
            if time.monotonic() > deadline:
                raise SystemExit("barrier timeout")
            time.sleep(0.01)
    return pairs

evidence_store.list_pairs = gated
doc = wb.command_audit_persisted(data_dir, evidence_dir=evidence)
print(json.dumps({{
    key: doc.get(key)
    for key in ("status", "reconciliation_verdict", "observation_stable",
                "stored_record_count", "vanished_shard_count", "corrupt_shard_count",
                "reconciliation_complete", "strict_acceptance_ready")
}}, ensure_ascii=False))
'''


def _wait_for(path, timeout=60.0):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(f"barrier not reached: {path}")
        time.sleep(0.01)


def _run_gated_audit(tmp_path, data_dir, evidence, mutate):
    script = tmp_path / "gated_audit.py"
    script.write_text(_CHILD.format(root=ROOT), encoding="utf-8")
    ready = tmp_path / "ready.flag"
    gate = tmp_path / "go.flag"
    env = {**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(script), str(ready), str(gate),
         str(evidence), str(data_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT, env=env,
    )
    try:
        _wait_for(ready)
        mutate()
        gate.write_text("go", encoding="utf-8")
        stdout, stderr = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, stderr
    return json.loads(stdout.strip().splitlines()[-1])


def test_ri_20_concurrent_append_cannot_produce_a_clean_verdict(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))

    observed = _run_gated_audit(
        tmp_path,
        data_dir,
        evidence,
        lambda: evidence_store.append_record(
            durable_record("7", pair=OTHER_PAIR), configured_dir=str(evidence)
        ),
    )

    # 修复前：这与单条读取之间的窗口没有防护，视图变了也照样报干净。
    assert observed["status"] == "SUCCESS"
    assert observed["observation_stable"] is False
    assert observed["reconciliation_verdict"] == wb.RECONCILIATION_INCOMPLETE
    assert observed["strict_acceptance_ready"] is False
    assert observed["reconciliation_complete"] is False


def test_ri_21_shard_removed_mid_audit_is_reported_not_crashed(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))
    path = shard_path(evidence, PAIR)

    observed = _run_gated_audit(
        tmp_path, data_dir, evidence, lambda: os.remove(path)
    )

    assert observed["status"] == "SUCCESS"
    assert observed["vanished_shard_count"] == 1
    assert observed["reconciliation_verdict"] == wb.RECONCILIATION_INCOMPLETE
    assert observed["reconciliation_complete"] is False


def test_ri_21b_store_removed_mid_audit_is_not_clean(tmp_path, data_dir, monkeypatch):
    """A store that disappears during the audit yields two equal (empty)
    enumerations; that must not be mistaken for a clean reconciliation."""
    import shutil

    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))
    real_list = evidence_store.list_pairs

    def vanishing(evidence_dir=None):
        real_list(evidence_dir)
        shutil.rmtree(str(evidence))
        return []

    monkeypatch.setattr(evidence_store, "list_pairs", vanishing)

    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))

    assert report["observation_stable"] is False
    assert report["stability_reason"] == "EVIDENCE_DIRECTORY_DISAPPEARED_DURING_AUDIT"
    assert report["reconciliation_verdict"] == wb.RECONCILIATION_INCOMPLETE
    assert report["strict_acceptance_ready"] is False


# ---------------------------------------------------------------------------
# D — read-only and non-disclosure invariants
# ---------------------------------------------------------------------------

def test_ri_22_read_only_audit_touches_nothing(tmp_path, data_dir):
    evidence = tmp_path / "evidence"
    StoreShape.mixed(str(evidence))
    store_before = store_hashes(data_dir)
    evidence_before = evidence_bytes(evidence)

    wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))
    wb.main(["audit-persisted", "--data-dir", data_dir, "--evidence-dir", str(evidence)])

    assert store_hashes(data_dir) == store_before
    assert evidence_bytes(evidence) == evidence_before


def test_ri_23_production_authorization_stays_false(tmp_path, data_dir, monkeypatch):
    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))
    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))
    assert report["executable"] is False
    assert report["apply_available"] is False
    assert report["production_write_authorization"] is False
    assert report["production_authorized"] is False
    assert report["surviving_canonical_id"] is None
    assert report["decision"] == "NO-GO"

    reviews = review_file(data_dir, tmp_path)
    dataset = wb.load_dataset(str(data_dir))
    monkeypatch.setattr(wb, "_load_dataset_or_error", lambda _d: dataset)
    _fingerprint_sequence(monkeypatch, [wb.dataset_fingerprint(dataset)])
    persisted = wb.command_persist(
        data_dir, reviews, evidence_dir=str(tmp_path / "ev2"), confirm=True
    )
    assert persisted["executable"] is False
    assert persisted["apply_available"] is False
    assert persisted["production_write_authorization"] is False
    assert persisted["production_authorized"] is False
    assert persisted["surviving_canonical_id"] is None
    assert persisted["decision"] == "NO-GO"


def test_ri_24_audit_output_never_echoes_record_content(tmp_path, data_dir, capsys):
    evidence = tmp_path / "evidence"
    StoreShape.healthy(str(evidence))
    markdown_path = os.path.join(str(tmp_path), "recon.md")

    capsys.readouterr()
    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))
    code = wb.main(
        ["audit-persisted", "--data-dir", data_dir, "--evidence-dir", str(evidence),
         "--markdown", markdown_path]
    )
    printed = capsys.readouterr().out
    with open(markdown_path, "r", encoding="utf-8") as handle:
        markdown = handle.read()

    assert code == 0
    blob = json.dumps(report, ensure_ascii=False) + printed + markdown
    for forbidden in (NOTE_MARKER, REFERENCE_MARKER, REVIEWER, '"reviewer"', '"notes"', '"decided_at"'):
        assert forbidden not in blob


def test_ri_25_corrupt_audit_output_never_echoes_record_content(tmp_path, data_dir, capsys):
    evidence = tmp_path / "evidence"
    StoreShape.corrupt_history(str(evidence))

    capsys.readouterr()
    report = wb.command_audit_persisted(data_dir, evidence_dir=str(evidence))
    wb.main(["audit-persisted", "--data-dir", data_dir, "--evidence-dir", str(evidence)])
    printed = capsys.readouterr().out

    blob = json.dumps(report, ensure_ascii=False) + printed
    for forbidden in (NOTE_MARKER, REFERENCE_MARKER, REVIEWER, '"reviewer"', '"notes"'):
        assert forbidden not in blob


# ---------------------------------------------------------------------------
# E — contract documentation guards
# ---------------------------------------------------------------------------

def test_ri_26_freshness_vocabulary_is_preserved():
    assert wb.FRESHNESS_UNCHANGED == "UNCHANGED"
    assert wb.FRESHNESS_CHANGED == "DATASET_CHANGED_DURING_PERSIST"
    assert wb.FRESHNESS_VALIDATION_FINGERPRINT_MISSING == "VALIDATION_FINGERPRINT_MISSING"
    assert wb.FRESHNESS_RECHECK_UNAVAILABLE == "RECHECK_UNAVAILABLE"
    # 裁决词表之外的状态不得存在。
    verdicts = {
        wb.RECONCILIATION_CLEAN,
        wb.RECONCILIATION_WITH_CORRUPTION,
        wb.RECONCILIATION_INCOMPLETE,
        wb.RECONCILIATION_INPUT_UNAVAILABLE,
    }
    assert wb.RECONCILIATION_STRICT_ACCEPTANCE == {wb.RECONCILIATION_CLEAN}
    assert len(verdicts) == 4


def test_ri_27_persist_requires_explicit_confirmation_before_any_field(tmp_path):
    refused = wb.command_persist(None, "x.json", confirm=False)
    assert refused["status"] == "EXPLICIT_CONFIRMATION_REQUIRED"
    assert refused["persistence_outcome"] == wb.PERSISTENCE_NOT_ATTEMPTED
    assert refused["durable_record_written"] is False
    assert refused["freshness_verified"] is False
    assert refused["strict_acceptance_ready"] is False
