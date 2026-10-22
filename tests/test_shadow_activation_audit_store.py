"""
影子激活审计隔离持久化测试套件（SG-30 .. SG-39）
==============================================
覆盖：
- 重启恢复（历史重建，但**绝不**自动启用影子消费）；
- 篡改 / 断链 / 重放 / 撕裂 / 截断一律 fail closed，绝不静默修复；
- 并发追加保持完整性；
- 所有审计文件位于授权临时工作区内；
- symlink / NTFS junction 逃逸与路径穿越被拒绝；
- 信任锚诚实性（哈希链不得被误报为外部可信）。

全部在显式提供的隔离临时工作区内执行，绝不触碰生产数据。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from utils.shadow_activation_audit_store import (
    AUDIT_INTEGRITY_CODES,
    GENESIS_PREVIOUS_HASH,
    SHADOW_AUDIT_EVENT_TYPES,
    SHADOW_AUDIT_FILENAME,
    SHADOW_AUDIT_STORE_VERSION,
    TRUST_ANCHOR_STATEMENT,
    ShadowActivationAuditStore,
    ShadowAuditIntegrityError,
    ShadowAuditPathError,
    _entry_hash,
    shadow_audit_store_status,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_DATA_DIR = os.path.join(REPO_ROOT, "data")


def _store(tmp_path, name="audit_ws", **kwargs) -> ShadowActivationAuditStore:
    workspace = tmp_path / name
    workspace.mkdir(exist_ok=True)
    return ShadowActivationAuditStore(str(workspace), **kwargs)


def _seed(store: ShadowActivationAuditStore, count: int = 5) -> None:
    for index in range(count):
        store.append(
            event_type=SHADOW_AUDIT_EVENT_TYPES[index % len(SHADOW_AUDIT_EVENT_TYPES)],
            occurred_at=f"2030-01-{index + 1:02d}T00:00:00+00:00",
            actor="shadow-operator",
            actor_role="shadow-operator",
            activation_id=f"activation-{index}",
            payload={"index": str(index)},
        )


def _read_lines(store: ShadowActivationAuditStore) -> list[str]:
    with open(store.path, "r", encoding="utf-8") as handle:
        return [line for line in handle.read().split("\n") if line.strip()]


def _write_lines(store: ShadowActivationAuditStore, lines) -> None:
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")


def _make_directory_link(target: str, link: str) -> None:
    """创建目录链接：Windows 用 NTFS junction（无需管理员），POSIX 用 symlink。"""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(target, link)
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_directory_link(link: str) -> None:
    """移除目录链接本身，**绝不**递归删除其目标。"""
    try:
        os.rmdir(link)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# SG-30
# ---------------------------------------------------------------------------

def test_sg_30_audit_history_survives_synthetic_restart(tmp_path):
    """SG-30: 审计历史在合成重启后仍然可用。"""
    store = _store(tmp_path, dataset_identity="ds-restart")
    _seed(store, 6)
    before = store.verify()
    assert before["integrity"] == "OK"
    assert before["entry_count"] == 6

    # 模拟重启：新实例读取同一隔离工作区。
    reopened = ShadowActivationAuditStore(store.workspace, dataset_identity="ds-restart")
    after = reopened.verify()
    assert after["entry_count"] == before["entry_count"]
    assert after["chain_tip"] == before["chain_tip"]
    assert after["event_types"] == before["event_types"]
    assert [record.entry_hash for record in reopened.records()] == [
        record.entry_hash for record in store.records()
    ]
    # 追加在重启后继续，序号与哈希链保持连续。
    resumed = reopened.append(
        event_type="SHADOW_SNAPSHOT_REGISTERED",
        occurred_at="2030-02-01T00:00:00+00:00",
        actor="shadow-operator",
        actor_role="shadow-operator",
    )
    assert resumed.sequence == 6
    assert resumed.previous_entry_hash == before["chain_tip"]
    assert reopened.verify()["integrity"] == "OK"


# ---------------------------------------------------------------------------
# SG-31 .. SG-34 — 损坏检测
# ---------------------------------------------------------------------------

def test_sg_31_modified_audit_entries_fail_verification(tmp_path):
    """SG-31: 被修改的审计条目验证失败。"""
    store = _store(tmp_path)
    _seed(store, 3)
    lines = _read_lines(store)
    payload = json.loads(lines[1])
    payload["actor"] = "attacker"
    lines[1] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write_lines(store, lines)

    with pytest.raises(ShadowAuditIntegrityError) as exc:
        store.verify()
    assert "MODIFIED_ENTRY" in exc.value.rejection_codes


def test_sg_32_broken_previous_entry_hashes_fail(tmp_path):
    """SG-32: 前驱哈希绑定断裂被检出。"""
    store = _store(tmp_path)
    _seed(store, 3)
    lines = _read_lines(store)
    payload = json.loads(lines[-1])
    payload["previous_entry_hash"] = "f" * 64
    lines[-1] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write_lines(store, lines)

    with pytest.raises(ShadowAuditIntegrityError) as exc:
        store.verify()
    assert "BROKEN_PREVIOUS_HASH" in exc.value.rejection_codes

    # 序号缺口同样被检出。
    store2 = _store(tmp_path, "audit_ws_seq")
    _seed(store2, 3)
    lines2 = _read_lines(store2)
    payload2 = json.loads(lines2[2])
    payload2["sequence"] = 7
    payload2["entry_hash"] = _entry_hash(payload2)
    lines2[2] = json.dumps(payload2, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write_lines(store2, lines2)
    with pytest.raises(ShadowAuditIntegrityError) as exc2:
        store2.verify()
    assert "SEQUENCE_GAP" in exc2.value.rejection_codes


def test_sg_33_duplicate_event_replay_is_detected(tmp_path):
    """SG-33: 重复事件（同 event_id）被检出为重放。"""
    store = _store(tmp_path)
    _seed(store, 2)
    records = store.records()
    replayed = records[0].as_dict()
    replayed["sequence"] = 2
    replayed["previous_entry_hash"] = records[1].entry_hash
    replayed["entry_hash"] = _entry_hash(replayed)
    lines = _read_lines(store)
    lines.append(
        json.dumps(replayed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    _write_lines(store, lines)

    with pytest.raises(ShadowAuditIntegrityError) as exc:
        store.verify()
    assert "DUPLICATE_EVENT_ID" in exc.value.rejection_codes


def test_sg_34_truncated_or_torn_records_fail_closed(tmp_path):
    """SG-34: 截断 / 撕裂记录 fail closed，且绝不静默修复。"""
    # (a) 尾部撕裂（写入被中断）。
    torn = _store(tmp_path, "audit_ws_torn")
    _seed(torn, 2)
    with open(torn.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"sequence": 2, "event_type": "SHADOW_SNAPSHOT_REG')
    with open(torn.path, "rb") as handle:
        torn_bytes = handle.read()
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        torn.verify()
    assert "TORN_FINAL_RECORD" in exc.value.rejection_codes
    # 绝不静默截断修复：文件逐字节不变。
    with open(torn.path, "rb") as handle:
        assert handle.read() == torn_bytes

    # (b) 中间行损坏。
    corrupt = _store(tmp_path, "audit_ws_corrupt")
    _seed(corrupt, 3)
    lines = _read_lines(corrupt)
    lines[1] = "{not-json"
    _write_lines(corrupt, lines)
    with pytest.raises(ShadowAuditIntegrityError) as exc2:
        corrupt.verify()
    assert "CORRUPT_EVENT_RECORD" in exc2.value.rejection_codes

    # (c) 文件被整体截断到只剩半行。
    truncated = _store(tmp_path, "audit_ws_trunc")
    _seed(truncated, 2)
    with open(truncated.path, "r", encoding="utf-8") as handle:
        content = handle.read()
    with open(truncated.path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content[: len(content) // 2])
    with pytest.raises(ShadowAuditIntegrityError):
        truncated.verify()

    # 完整性失败码注册表自洽。
    assert len({code for code, _ in AUDIT_INTEGRITY_CODES}) == len(AUDIT_INTEGRITY_CODES)


# ---------------------------------------------------------------------------
# SG-35 .. SG-37 — 并发与路径安全
# ---------------------------------------------------------------------------

def test_sg_35_concurrent_appends_preserve_integrity(tmp_path):
    """SG-35: 并发追加保持完整性（或有界拒绝，绝不损坏）。"""
    store = _store(tmp_path, dataset_identity="ds-concurrent")
    threads_count = 4
    per_thread = 5
    errors: list[BaseException] = []

    def worker(worker_index: int) -> None:
        try:
            for index in range(per_thread):
                store.append(
                    event_type="SHADOW_SNAPSHOT_REGISTERED",
                    occurred_at="2030-01-15T00:00:00+00:00",
                    actor=f"worker-{worker_index}",
                    actor_role="shadow-operator",
                    activation_id=f"activation-{worker_index}-{index}",
                    payload={"worker": str(worker_index), "index": str(index)},
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(index,))
        for index in range(threads_count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = store.records()
    # 无论是否发生有界拒绝，链本身必须完整且序号连续。
    report = store.verify()
    assert report["integrity"] == "OK"
    assert report["entry_count"] == len(records)
    assert [record.sequence for record in records] == list(range(len(records)))
    assert len({record.entry_hash for record in records}) == len(records)
    # 成功追加的记录数 + 失败次数 = 期望总数（无静默丢失）。
    assert len(records) + len(errors) == threads_count * per_thread


def test_sg_36_all_audit_files_remain_inside_the_authorized_workspace(tmp_path):
    """SG-36: 所有审计文件都位于授权工作区内。"""
    workspace = tmp_path / "audit_ws_files"
    workspace.mkdir()
    store = ShadowActivationAuditStore(str(workspace), dataset_identity="ds-files")
    _seed(store, 3)
    store.verify()

    real_workspace = os.path.realpath(str(workspace))
    assert os.path.realpath(store.path).startswith(real_workspace + os.sep)
    created = sorted(os.listdir(str(workspace)))
    assert SHADOW_AUDIT_FILENAME in created
    # 工作区之外（尤其是仓库 data/）没有产生任何文件。
    # ``store.workspace`` 由既有路径守卫返回（已按平台规则规范化大小写）。
    assert os.path.normcase(store.workspace) == os.path.normcase(real_workspace)
    assert not os.path.exists(os.path.join(SOURCE_DATA_DIR, SHADOW_AUDIT_FILENAME))
    assert not os.path.exists(
        os.path.join(REPO_ROOT, SHADOW_AUDIT_FILENAME)
    )
    status = shadow_audit_store_status()
    assert status["default_location"] is None
    assert status["audit_filename"] == SHADOW_AUDIT_FILENAME
    assert SHADOW_AUDIT_STORE_VERSION == "1.2.0"


def test_sg_37_symlink_and_junction_escapes_are_rejected(tmp_path):
    """SG-37: symlink / NTFS junction 逃逸与路径穿越被拒绝。"""
    workspace = tmp_path / "audit_ws_links"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # (a) 纯路径穿越（与平台无关）。
    with pytest.raises(ShadowAuditPathError) as exc_traversal:
        ShadowActivationAuditStore(str(workspace), filename="../outside/audit.jsonl")
    assert "PATH_ESCAPE" in exc_traversal.value.rejection_codes

    # (b) 通过目录链接逃逸（Windows 用 junction，POSIX 用 symlink）。
    link = str(workspace / "escape_link")
    _make_directory_link(str(outside), link)
    try:
        assert os.path.isdir(link), "the directory link must be usable for this test"
        with pytest.raises(ShadowAuditPathError) as exc_link:
            ShadowActivationAuditStore(str(workspace), filename="escape_link/audit.jsonl")
        assert set(exc_link.value.rejection_codes) & {"PATH_ESCAPE", "PROTECTED_PATH"}
    finally:
        _remove_directory_link(link)
    # 链接的目标目录绝不被删除。
    assert os.path.isdir(str(outside))

    # (c) 指向仓库内部的链接必须被拒绝。
    link_to_repo = str(workspace / "repo_link")
    _make_directory_link(SOURCE_DATA_DIR, link_to_repo)
    try:
        with pytest.raises(ShadowAuditPathError) as exc_repo:
            ShadowActivationAuditStore(str(workspace), filename="repo_link/audit.jsonl")
        assert set(exc_repo.value.rejection_codes) & {
            "PATH_ESCAPE",
            "PROTECTED_PATH",
            "REPOSITORY_PATH",
        }
    finally:
        _remove_directory_link(link_to_repo)

    # (d) 工作区本身不得位于仓库内部，且必须显式存在。
    with pytest.raises(ShadowAuditPathError) as exc_repo_ws:
        ShadowActivationAuditStore(os.path.join(REPO_ROOT, "synthetic_audit_ws"))
    assert set(exc_repo_ws.value.rejection_codes) & {
        "REPOSITORY_PATH",
        "PROTECTED_PATH",
        "WORKSPACE_MISSING",
    }
    with pytest.raises(ShadowAuditPathError) as exc_missing:
        ShadowActivationAuditStore(str(tmp_path / "does-not-exist"))
    assert "WORKSPACE_MISSING" in exc_missing.value.rejection_codes
    with pytest.raises(ShadowAuditPathError) as exc_empty:
        ShadowActivationAuditStore("")
    assert "WORKSPACE_REQUIRED" in exc_empty.value.rejection_codes


# ---------------------------------------------------------------------------
# SG-38 / SG-39 — 恢复安全与信任锚诚实性
# ---------------------------------------------------------------------------

def test_sg_38_audit_recovery_never_enables_shadow_consumption(tmp_path):
    """SG-38: 审计恢复绝不自动启用影子消费。"""
    store = _store(tmp_path, dataset_identity="ds-recovery")
    _seed(store, 4)

    recovery = store.recover()
    assert recovery["history_reconstructed"] is True
    assert recovery["active_shadow_state"] == "NOT_RESTORED"
    assert recovery["consumable"] is False
    assert recovery["auto_activation_performed"] is False
    assert recovery["silent_truncation_repair_performed"] is False
    assert recovery["entry_count"] == 4

    # 恢复之后，新构造的影子读取器默认仍然没有任何活动映射。
    from utils.shadow_runtime_reader import ShadowRuntimeReader

    reader = ShadowRuntimeReader()
    assert reader.enabled is True
    assert reader.status()["registry_attached"] is False

    # 审计存储不提供任何「发布 / 启用」入口。
    for forbidden in ("activate", "publish", "enable", "restore_active"):
        assert not hasattr(store, forbidden), forbidden


def test_sg_39_hash_chain_is_not_misreported_as_externally_trusted(tmp_path):
    """SG-39: 结构有效的哈希链不得被误报为外部可信。"""
    store = _store(tmp_path)
    _seed(store, 3)
    report = store.verify()

    assert report["integrity"] == "OK"
    assert report["externally_authenticated"] is False
    assert report["trusted_checkpoint_implemented"] is False
    assert report["production_event_emission"] is False
    assert "NO EXTERNAL TRUST ANCHOR" in report["trust_anchor"]
    assert report["trust_anchor"] == TRUST_ANCHOR_STATEMENT

    status = shadow_audit_store_status()
    assert status["externally_authenticated"] is False
    assert status["trusted_checkpoint_implemented"] is False
    assert status["trust_anchor"] == TRUST_ANCHOR_STATEMENT
    assert status["silent_truncation_repair"] is False
    assert status["deletes_prior_events"] is False
    assert status["in_memory_log_replaced"] is False
    # 既有内存审计日志没有被改造成生产写入器。
    with open(
        os.path.join(REPO_ROOT, "utils", "runtime_activation_authorization.py"),
        encoding="utf-8",
    ) as handle:
        activation_source = handle.read()
    assert "fsync" not in activation_source
    assert "ShadowActivationAuditStore" not in activation_source

    # 事件目录覆盖 §13 要求的全部 11 类。
    assert len(SHADOW_AUDIT_EVENT_TYPES) == 11
    assert len(set(SHADOW_AUDIT_EVENT_TYPES)) == 11
    assert GENESIS_PREVIOUS_HASH == "0" * 64
    assert len({code for code, _ in AUDIT_INTEGRITY_CODES}) == len(AUDIT_INTEGRITY_CODES)


# ---------------------------------------------------------------------------
# 补充不变量
# ---------------------------------------------------------------------------

def test_unsupported_event_types_and_corrupt_shapes_are_rejected(tmp_path):
    """补充：未知事件类型、缺字段记录、非对象记录一律拒绝。"""
    store = _store(tmp_path)
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        store.append(
            event_type="NOT_A_REAL_EVENT",
            occurred_at="2030-01-01T00:00:00+00:00",
            actor="x",
            actor_role="y",
        )
    assert "UNSUPPORTED_EVENT_TYPE" in exc.value.rejection_codes
    assert store.exists() is False

    _seed(store, 1)
    lines = _read_lines(store)
    lines.append('["not", "an", "object"]')
    _write_lines(store, lines)
    with pytest.raises(ShadowAuditIntegrityError) as exc2:
        store.verify()
    assert "CORRUPT_EVENT_RECORD" in exc2.value.rejection_codes

    # 缺字段记录。
    store2 = _store(tmp_path, "audit_ws_missing")
    _seed(store2, 1)
    lines2 = _read_lines(store2)
    payload = json.loads(lines2[0])
    payload.pop("actor_role")
    lines2[0] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _write_lines(store2, lines2)
    with pytest.raises(ShadowAuditIntegrityError) as exc3:
        store2.verify()
    assert "CORRUPT_EVENT_RECORD" in exc3.value.rejection_codes


def test_audit_chain_tip_and_hashes_are_deterministic(tmp_path):
    """补充：同输入产生同一哈希链（确定性）。"""
    first = _store(tmp_path, "audit_ws_det_a")
    second = _store(tmp_path, "audit_ws_det_b")
    _seed(first, 4)
    _seed(second, 4)

    assert [r.entry_hash for r in first.records()] == [
        r.entry_hash for r in second.records()
    ]
    assert first.chain_tip() == second.chain_tip()
    assert hashlib.sha256(first.chain_tip().encode("utf-8")).hexdigest()
    # 事件 ID 绑定内容与位置：不同工作区的相同事件得到相同 ID。
    assert [r.event_id for r in first.records()] == [
        r.event_id for r in second.records()
    ]
