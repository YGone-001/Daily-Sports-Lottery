"""
影子激活审计**隔离持久化原型**
==============================
本模块把影子激活模拟的审计事件持久化为一条**追加式哈希链**，用于演练：

- 重启恢复（从已记录事件重建历史）；
- 损坏检测（篡改、断链、序号缺口、重放、尾部撕裂）；
- 并发追加的完整性。

严格边界
--------
- **只在显式提供的隔离工作区中工作**；**没有默认的生产审计位置**。
- 拒绝：仓库路径、受保护的 Canonical 修复路径、证据备份目录、
  逃逸出授权工作区的路径、symlink / NTFS junction 逃逸、无法判定的包含关系。
- **绝不**把既有内存审计日志（``ShadowActivationAuditLog``）改造成生产写入器。
- **绝不**产生任何生产事件、**绝不**自动启用影子消费。
- 追加使用**有界等待**的跨进程锁（无强制解锁、无无界重试）与 ``fsync`` 持久化；
  任何完整性失败一律 fail closed，**不做静默截断修复**。

信任锚诚实性
------------
哈希链**不是**完整独立的防篡改体系。它**无法**单独证明：
对整链重写的真实性、对任何「合法链尾部截断」的检测、独立可信的事件序列。
本原型**不实现**可信检查点；因此**绝不**声称外部审计真实性。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields
from typing import Any, Mapping, Sequence

from utils.mapping_governance import _ledger_lock
from utils.runtime_activation_authorization import ActivationPrototypeError
from utils.source_identity import canonical_json, fingerprint

#: 审计存储原型版本。
SHADOW_AUDIT_STORE_VERSION = "1.0.0"

#: 审计文件名（**只在显式工作区内**）。
SHADOW_AUDIT_FILENAME = "shadow_activation_audit.jsonl"

#: 创世前驱哈希。
GENESIS_PREVIOUS_HASH = "0" * 64

#: 原型支持的合成审计事件（§13 要求的 11 类）。
SHADOW_AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "ACTIVATION_AUTHORIZATION_VERIFIED",
    "SHADOW_SNAPSHOT_REGISTERED",
    "SHADOW_SNAPSHOT_SELECTED",
    "SHADOW_SNAPSHOT_SWITCHED",
    "SHADOW_RUNTIME_READ_REJECTED",
    "SHADOW_RUNTIME_DISABLED",
    "SHADOW_ROLLBACK_REQUESTED",
    "SHADOW_ROLLBACK_COMPLETED",
    "SHADOW_ROLLBACK_REJECTED",
    "ACTIVATION_AUTHORIZATION_EXPIRED",
    "ACTIVATION_AUTHORIZATION_REVOKED",
)

#: 信任锚说明（如实声明限制）。
TRUST_ANCHOR_STATEMENT = (
    "NO EXTERNAL TRUST ANCHOR — the hash chain proves internal consistency only. It "
    "cannot establish authenticity against a full-chain rewrite, cannot detect every "
    "tail truncation of an otherwise valid chain, and is not an independent trusted "
    "event sequence. No trusted checkpoint is implemented by this prototype."
)

#: 完整性失败码。
AUDIT_INTEGRITY_CODES: tuple[tuple[str, str], ...] = (
    ("CORRUPT_EVENT_RECORD", "某条事件不是合法 JSON 对象。"),
    ("TORN_FINAL_RECORD", "最后一条记录不完整（写入被中断）；拒绝静默截断修复。"),
    ("SEQUENCE_GAP", "事件序号不连续。"),
    ("BROKEN_PREVIOUS_HASH", "前驱哈希绑定断裂。"),
    ("MODIFIED_ENTRY", "条目内容与其记录哈希不一致（被篡改）。"),
    ("DUPLICATE_EVENT_ID", "同一事件 ID 出现多次（重放）。"),
    ("UNSUPPORTED_EVENT_TYPE", "事件类型不在允许列表内。"),
)

#: 路径拒绝码。
AUDIT_PATH_CODES: tuple[tuple[str, str], ...] = (
    ("WORKSPACE_REQUIRED", "必须显式提供隔离工作区；没有默认审计位置。"),
    ("WORKSPACE_MISSING", "工作区不存在。"),
    ("REPOSITORY_PATH", "拒绝在仓库内部创建审计存储。"),
    ("PROTECTED_PATH", "拒绝受保护的 Canonical 修复路径 / 证据备份目录。"),
    ("CONTAINMENT_UNDECIDABLE", "无法判定路径包含关系；fail closed。"),
    ("PATH_ESCAPE", "路径逃逸出授权工作区（含 symlink / junction）。"),
)


class ShadowAuditPathError(ActivationPrototypeError):
    """审计路径不在授权隔离工作区内。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


class ShadowAuditIntegrityError(ActivationPrototypeError):
    """审计链完整性失败。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


# =============================================================================
# 事件记录
# =============================================================================

@dataclass(frozen=True)
class ShadowAuditRecord:
    """一条持久化审计事件（不可变、确定性、可哈希）。"""

    sequence: int
    event_id: str
    event_type: str
    activation_id: str
    occurred_at: str
    actor: str
    actor_role: str
    dataset_identity: str
    snapshot_id: str
    payload: tuple[tuple[str, str], ...]
    previous_entry_hash: str
    entry_hash: str

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "activation_id": self.activation_id,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "dataset_identity": self.dataset_identity,
            "snapshot_id": self.snapshot_id,
            "payload": dict(self.payload),
            "previous_entry_hash": self.previous_entry_hash,
            "entry_hash": self.entry_hash,
        }

    def content_payload(self) -> dict:
        """事件**语义内容**（不含位置与哈希）。"""
        return {
            "event_type": self.event_type,
            "activation_id": self.activation_id,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "dataset_identity": self.dataset_identity,
            "snapshot_id": self.snapshot_id,
            "payload": dict(self.payload),
        }

    def content_fingerprint(self) -> str:
        """事件内容指纹（§13.7）。"""
        return fingerprint(self.content_payload())

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def _entry_hash(payload: Mapping[str, Any]) -> str:
    return fingerprint({k: v for k, v in payload.items() if k != "entry_hash"})


# =============================================================================
# 隔离持久化审计存储
# =============================================================================

class ShadowActivationAuditStore:
    """
    隔离工作区内的追加式哈希链审计存储（**原型**）。

    绝不写生产路径、绝不删除既有事件、绝不静默修复损坏。
    """

    def __init__(
        self,
        workspace: str,
        *,
        dataset_identity: str = "",
        filename: str = SHADOW_AUDIT_FILENAME,
    ) -> None:
        self._workspace = _validate_workspace(workspace)
        self._filename = str(filename)
        self._path = _validate_audit_file(
            os.path.join(self._workspace, self._filename), self._workspace
        )
        self._lock_path = self._path + ".lock"
        self.dataset_identity = str(dataset_identity)

    # -- 只读视图 ---------------------------------------------------------

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def path(self) -> str:
        return self._path

    def exists(self) -> bool:
        return os.path.exists(self._path)

    def records(self) -> tuple[ShadowAuditRecord, ...]:
        """读取全部事件（只读；不完整即抛错，绝不静默截断）。"""
        return tuple(self._read_strict())

    def event_types(self) -> tuple[str, ...]:
        return tuple(record.event_type for record in self._read_strict())

    def chain_tip(self) -> str:
        records = self._read_strict()
        return records[-1].entry_hash if records else GENESIS_PREVIOUS_HASH

    def verify(self) -> dict:
        """
        **只读**完整性校验：重算哈希链、序号连续性、前驱绑定、事件 ID 唯一性。

        任何不一致都抛 ``ShadowAuditIntegrityError``（fail closed，绝不修复）。
        """
        records = self._read_strict()
        previous = GENESIS_PREVIOUS_HASH
        seen_event_ids: dict[str, int] = {}
        for index, record in enumerate(records):
            if record.sequence != index:
                raise ShadowAuditIntegrityError(
                    f"audit sequence gap at index {index}",
                    rejection_codes=("SEQUENCE_GAP",),
                )
            if record.previous_entry_hash != previous:
                raise ShadowAuditIntegrityError(
                    f"audit previous-entry hash broken at index {index}",
                    rejection_codes=("BROKEN_PREVIOUS_HASH",),
                )
            if _entry_hash(record.as_dict()) != record.entry_hash:
                raise ShadowAuditIntegrityError(
                    f"audit entry {index} was modified",
                    rejection_codes=("MODIFIED_ENTRY",),
                )
            if record.event_id in seen_event_ids:
                raise ShadowAuditIntegrityError(
                    f"audit entry {index} replays an already recorded event id",
                    rejection_codes=("DUPLICATE_EVENT_ID",),
                )
            seen_event_ids[record.event_id] = index
            previous = record.entry_hash

        return {
            "kind": "SHADOW_ACTIVATION_AUDIT_VERIFICATION",
            "prototype_only": True,
            "store_version": SHADOW_AUDIT_STORE_VERSION,
            "workspace": self._workspace,
            "path": self._path,
            "entry_count": len(records),
            "chain_tip": previous,
            "integrity": "OK",
            "dataset_identity": self.dataset_identity,
            "event_types": [record.event_type for record in records],
            "persisted": True,
            "production_event_emission": False,
            "trust_anchor": TRUST_ANCHOR_STATEMENT,
            "externally_authenticated": False,
            "trusted_checkpoint_implemented": False,
        }

    def recover(self) -> dict:
        """
        重启恢复：**只读**重建历史证据。

        **绝不**自动恢复活动影子状态，**绝不**自动重新发布可消费映射。
        """
        report = self.verify()
        return {
            "kind": "SHADOW_ACTIVATION_AUDIT_RECOVERY",
            "prototype_only": True,
            "store_version": SHADOW_AUDIT_STORE_VERSION,
            "workspace": self._workspace,
            "integrity": report["integrity"],
            "entry_count": report["entry_count"],
            "chain_tip": report["chain_tip"],
            "event_types": report["event_types"],
            "history_reconstructed": True,
            "active_shadow_state": "NOT_RESTORED",
            "consumable": False,
            "auto_activation_performed": False,
            "silent_truncation_repair_performed": False,
            "trust_anchor": report["trust_anchor"],
        }

    # -- 追加 -------------------------------------------------------------

    def append(
        self,
        *,
        event_type: str,
        occurred_at: str,
        actor: str,
        actor_role: str,
        activation_id: str = "",
        payload: Mapping[str, Any] | None = None,
        dataset_identity: str | None = None,
        snapshot_id: str = "",
    ) -> ShadowAuditRecord:
        """
        追加一条事件（只追加；绝不删除或改写既有条目）。

        - 并发安全：使用与治理账本同源的**有界等待**跨进程锁（无强制解锁、
          无无界重试）。
        - 崩溃安全：单行写入 + ``flush`` + ``fsync``。
        - 未知事件类型一律拒绝。
        """
        if event_type not in SHADOW_AUDIT_EVENT_TYPES:
            raise ShadowAuditIntegrityError(
                f"unsupported shadow audit event type {event_type!r}",
                rejection_codes=("UNSUPPORTED_EVENT_TYPE",),
            )

        with _ledger_lock(self._lock_path):
            records = self._read_strict()
            previous = records[-1].entry_hash if records else GENESIS_PREVIOUS_HASH
            sequence = len(records)

            body = {
                "sequence": sequence,
                "event_type": str(event_type),
                "activation_id": str(activation_id),
                "occurred_at": str(occurred_at),
                "actor": str(actor),
                "actor_role": str(actor_role),
                "dataset_identity": (
                    self.dataset_identity
                    if dataset_identity is None
                    else str(dataset_identity)
                ),
                "snapshot_id": str(snapshot_id),
                "payload": {
                    str(key): str(value)
                    for key, value in dict(payload or {}).items()
                },
                "previous_entry_hash": previous,
            }
            content_fingerprint = fingerprint(
                {
                    "event_type": body["event_type"],
                    "activation_id": body["activation_id"],
                    "occurred_at": body["occurred_at"],
                    "actor": body["actor"],
                    "actor_role": body["actor_role"],
                    "dataset_identity": body["dataset_identity"],
                    "snapshot_id": body["snapshot_id"],
                    "payload": body["payload"],
                }
            )
            # 事件 ID 绑定内容 + 位置：同一内容的合法重复事件得到不同 ID，
            # 而「同一条记录被复制」会被 verify() 的重复 ID 检查检出。
            body["event_id"] = fingerprint(
                {"content_fingerprint": content_fingerprint, "sequence": sequence}
            )
            body["entry_hash"] = _entry_hash(body)

            record = ShadowAuditRecord(
                sequence=body["sequence"],
                event_id=body["event_id"],
                event_type=body["event_type"],
                activation_id=body["activation_id"],
                occurred_at=body["occurred_at"],
                actor=body["actor"],
                actor_role=body["actor_role"],
                dataset_identity=body["dataset_identity"],
                snapshot_id=body["snapshot_id"],
                payload=tuple(sorted(body["payload"].items())),
                previous_entry_hash=body["previous_entry_hash"],
                entry_hash=body["entry_hash"],
            )

            with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(record.as_dict()))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    # -- 内部 -------------------------------------------------------------

    def _read_strict(self) -> list[ShadowAuditRecord]:
        if not self.exists():
            return []
        with open(self._path, "r", encoding="utf-8") as handle:
            raw = handle.read()
        lines = raw.split("\n")
        # 末尾换行是正常终止；只有「非空且非终止」的残行才算撕裂。
        if lines and lines[-1] == "":
            lines = lines[:-1]
        records: list[ShadowAuditRecord] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                is_last = index == len(lines) - 1
                raise ShadowAuditIntegrityError(
                    f"audit line {index} is not valid JSON: {exc}",
                    rejection_codes=(
                        ("TORN_FINAL_RECORD",) if is_last else ("CORRUPT_EVENT_RECORD",)
                    ),
                ) from exc
            if not isinstance(payload, dict):
                raise ShadowAuditIntegrityError(
                    f"audit line {index} is not an object",
                    rejection_codes=("CORRUPT_EVENT_RECORD",),
                )
            names = {field.name for field in fields(ShadowAuditRecord)}
            missing = sorted(name for name in names if name not in payload)
            if missing:
                raise ShadowAuditIntegrityError(
                    f"audit line {index} is missing fields {missing}",
                    rejection_codes=("CORRUPT_EVENT_RECORD",),
                )
            payload["payload"] = tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in dict(payload.get("payload") or {}).items()
                )
            )
            records.append(ShadowAuditRecord(**payload))
        return records


# =============================================================================
# 路径守卫（复用既有受保护路径原语，绝不绕过）
# =============================================================================

def _validate_workspace(workspace: str) -> str:
    """
    校验隔离工作区。

    复用既有受保护路径守卫（``tools.staging_canonical_repair.assert_safe_path``
    与三态 ``containment_relation``），**不重新实现**也不放宽任何保护。
    """
    if not workspace or not str(workspace).strip():
        raise ShadowAuditPathError(
            "an explicit isolated workspace is required; there is no default audit location",
            rejection_codes=("WORKSPACE_REQUIRED",),
        )
    candidate = os.path.abspath(str(workspace))
    if not os.path.isdir(candidate):
        raise ShadowAuditPathError(
            f"audit workspace does not exist: {candidate!r}",
            rejection_codes=("WORKSPACE_MISSING",),
        )

    # 1) utils 级守卫：拒绝仓库内部任何路径。
    real = os.path.realpath(candidate)
    repo_root = os.path.realpath(
        os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
    )
    if real == repo_root or real.startswith(repo_root + os.sep):
        raise ShadowAuditPathError(
            f"refusing to create a shadow audit store inside the repository ({repo_root})",
            rejection_codes=("REPOSITORY_PATH",),
        )

    # 2) tools 级守卫：受保护根 / 证据备份 / 三态包含关系（fail closed）。
    try:
        from tools.staging_canonical_repair import (  # noqa: PLC0415
            ProtectedPathViolationError,
            assert_safe_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ShadowAuditPathError(
            "the protected-path guard is unavailable; refusing to create an audit store "
            "(fail closed)",
            rejection_codes=("CONTAINMENT_UNDECIDABLE",),
        ) from exc
    try:
        return assert_safe_path(candidate, context="shadow audit workspace")
    except ProtectedPathViolationError as exc:
        message = str(exc)
        code = (
            "CONTAINMENT_UNDECIDABLE"
            if "cannot determine" in message
            else "PROTECTED_PATH"
        )
        raise ShadowAuditPathError(message, rejection_codes=(code,)) from exc


def _validate_audit_file(path: str, workspace: str) -> str:
    """校验审计文件位于授权工作区内（含 symlink / junction 逃逸检查）。"""
    try:
        from tools.staging_canonical_repair import (  # noqa: PLC0415
            ProtectedPathViolationError,
            assert_safe_path,
        )
    except Exception as exc:  # noqa: BLE001
        raise ShadowAuditPathError(
            "the protected-path guard is unavailable; refusing (fail closed)",
            rejection_codes=("CONTAINMENT_UNDECIDABLE",),
        ) from exc
    try:
        return assert_safe_path(
            path, context="shadow audit file", staging_root=workspace
        )
    except ProtectedPathViolationError as exc:
        message = str(exc)
        code = "PATH_ESCAPE" if "escapes" in message else "PROTECTED_PATH"
        raise ShadowAuditPathError(message, rejection_codes=(code,)) from exc


def shadow_audit_store_status() -> dict:
    """审计存储原型的只读状态视图。"""
    return {
        "shadow_audit_store_version": SHADOW_AUDIT_STORE_VERSION,
        "prototype_only": True,
        "audit_filename": SHADOW_AUDIT_FILENAME,
        "supported_event_types": list(SHADOW_AUDIT_EVENT_TYPES),
        "default_location": None,
        "production_event_emission": False,
        "trusted_checkpoint_implemented": False,
        "externally_authenticated": False,
        "trust_anchor": TRUST_ANCHOR_STATEMENT,
        "silent_truncation_repair": False,
        "deletes_prior_events": False,
        "in_memory_log_replaced": False,
    }


def _assert_audit_store_is_coherent() -> None:
    if len(set(SHADOW_AUDIT_EVENT_TYPES)) != len(SHADOW_AUDIT_EVENT_TYPES):
        raise AssertionError("shadow audit event types must be unique")
    for required in (
        "ACTIVATION_AUTHORIZATION_VERIFIED",
        "SHADOW_SNAPSHOT_REGISTERED",
        "SHADOW_SNAPSHOT_SELECTED",
        "SHADOW_SNAPSHOT_SWITCHED",
        "SHADOW_RUNTIME_READ_REJECTED",
        "SHADOW_RUNTIME_DISABLED",
        "SHADOW_ROLLBACK_REQUESTED",
        "SHADOW_ROLLBACK_COMPLETED",
        "SHADOW_ROLLBACK_REJECTED",
        "ACTIVATION_AUTHORIZATION_EXPIRED",
        "ACTIVATION_AUTHORIZATION_REVOKED",
    ):
        if required not in SHADOW_AUDIT_EVENT_TYPES:
            raise AssertionError(f"shadow audit event {required!r} is required")
    if "NO EXTERNAL TRUST ANCHOR" not in TRUST_ANCHOR_STATEMENT:
        raise AssertionError("the audit trust-anchor statement must stay honest")


_assert_audit_store_is_coherent()
