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
from utils.trusted_artifacts import (
    SignedTrustedArtifact,
    TrustedArtifactError,
    sign_trusted_artifact,
    verify_trusted_artifact,
)

#: 审计存储原型版本。
#:
#: ``1.1.0``：追加前完整校验既有历史（哈希链、序号、事件 ID、事件形状、
#: 严格终止符、禁止空白物理行）；校验与加锁彻底分离，避免递归加锁。
#: ``1.2.0``：新增**持久提交收据**（存储身份 + 记录身份 + 持久化确认），
#: 供回滚提交边界回读验证；磁盘事件格式保持不变。
#: ``1.3.0``：新增**外部签名链检查点**，用于检测尾部截断与全链重写。
SHADOW_AUDIT_STORE_VERSION = "1.3.0"

#: 外部审计链检查点工件类型。
SHADOW_AUDIT_CHECKPOINT_TYPE = "SHADOW_AUDIT_CHAIN_CHECKPOINT"

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

#: 严格物理格式（文档化契约）。
#:
#: 审计文件由**且仅由**以单个 ``\n`` 终止的规范 JSON 行组成：
#: - 每条记录恰好一行，行内不含 ``\r``；
#: - 除每条记录末尾的 ``\n`` 之外，**不允许**任何空白物理行；
#: - 最后一条记录必须以 ``\n`` 终止（未终止即视为写入中断 / 撕裂）。
STRICT_AUDIT_FORMAT_STATEMENT = (
    "STRICT JSONL: every record is one canonical JSON line terminated by a single LF; "
    "CR is rejected, blank physical lines are rejected, and an unterminated final "
    "record is rejected as torn. Damaged history is never auto-repaired or truncated."
)

#: 完整性失败码。
AUDIT_INTEGRITY_CODES: tuple[tuple[str, str], ...] = (
    ("CORRUPT_EVENT_RECORD", "某条事件不是合法 JSON 对象或字段形状不受支持。"),
    ("TORN_FINAL_RECORD", "最后一条记录不完整（写入被中断）；拒绝静默截断修复。"),
    ("UNTERMINATED_FINAL_RECORD", "最后一条记录缺少 LF 终止符（未按严格格式终止）。"),
    ("UNEXPECTED_BLANK_LINE", "出现空白物理行；拒绝静默忽略或规范化。"),
    ("SEQUENCE_GAP", "事件序号不连续。"),
    ("BROKEN_PREVIOUS_HASH", "前驱哈希绑定断裂。"),
    ("MODIFIED_ENTRY", "条目内容与其记录哈希不一致（被篡改）。"),
    ("DUPLICATE_EVENT_ID", "同一事件 ID 出现多次（重放）。"),
    ("UNSUPPORTED_EVENT_TYPE", "事件类型不在允许列表内。"),
    ("CHECKPOINT_INVALID", "外部签名检查点无法验证或已过期。"),
    ("CHECKPOINT_MISMATCH", "外部检查点与当前链尖 / 长度不一致（截断或重写）。"),
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


@dataclass(frozen=True)
class ShadowAuditCommitReceipt:
    """
    一次**持久化审计追加**的最小提交收据。

    收据只证明「本隔离存储在该序号处记录了这个事件，且它是当前链尖」，
    绝不是外部可信锚：它无法证明整链未被重写，也不构成生产授权。
    """

    store_identity: str
    event_type: str
    event_id: str
    sequence: int
    entry_hash: str
    previous_entry_hash: str
    persisted: bool = True

    def as_dict(self) -> dict:
        return {
            "kind": "SHADOW_AUDIT_COMMIT_RECEIPT",
            "prototype_only": True,
            "store_identity": self.store_identity,
            "event_type": self.event_type,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "entry_hash": self.entry_hash,
            "previous_entry_hash": self.previous_entry_hash,
            "persisted": self.persisted,
            "external_trust_anchor": False,
        }

    def matches(self, record: ShadowAuditRecord) -> bool:
        """收据必须逐字段对应**同一条**记录，且声明已持久化。"""
        return bool(
            self.persisted
            and isinstance(record, ShadowAuditRecord)
            and self.event_type == record.event_type
            and self.event_id == record.event_id
            and self.sequence == record.sequence
            and self.entry_hash == record.entry_hash
            and self.previous_entry_hash == record.previous_entry_hash
        )


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
        lock_factory: Any = None,
    ) -> None:
        self._workspace = _validate_workspace(workspace)
        self._filename = str(filename)
        self._path = _validate_audit_file(
            os.path.join(self._workspace, self._filename), self._workspace
        )
        self._lock_path = self._path + ".lock"
        self.dataset_identity = str(dataset_identity)
        self._lock_factory = lock_factory or _ledger_lock

    # -- 只读视图 ---------------------------------------------------------

    @property
    def workspace(self) -> str:
        return self._workspace

    @property
    def path(self) -> str:
        return self._path

    def store_identity(self) -> str:
        """
        本隔离审计存储的确定性身份（不含任何秘密）。

        绑定工作区、审计文件路径、数据集身份与存储版本；用于让提交收据
        只被**同一**存储接受。
        """
        return fingerprint(
            {
                "kind": "SHADOW_ACTIVATION_AUDIT_STORE_IDENTITY",
                "store_version": SHADOW_AUDIT_STORE_VERSION,
                "workspace": self._workspace,
                "path": self._path,
                "dataset_identity": self.dataset_identity,
            }
        )

    def receipt_for(self, record: ShadowAuditRecord) -> ShadowAuditCommitReceipt:
        """为一条已追加记录生成提交收据（不重新验证；由调用方回读校验）。"""
        if not isinstance(record, ShadowAuditRecord):
            raise ShadowAuditIntegrityError(
                "a commit receipt can only be issued for a persisted audit record",
                rejection_codes=("CORRUPT_EVENT_RECORD",),
            )
        return ShadowAuditCommitReceipt(
            store_identity=self.store_identity(),
            event_type=record.event_type,
            event_id=record.event_id,
            sequence=record.sequence,
            entry_hash=record.entry_hash,
            previous_entry_hash=record.previous_entry_hash,
            persisted=True,
        )

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

    # -- 外部签名检查点 ---------------------------------------------------

    def checkpoint_payload(self, *, issued_at: str) -> dict:
        """当前完整链的检查点载荷（链尖 + 长度 + 存储身份 + 时间）。"""
        records = self._read_strict()
        self._validate_records(records)
        return {
            "subject_identity": self.store_identity(),
            "dataset_identity": self.dataset_identity,
            "chain_tip": (
                records[-1].entry_hash if records else GENESIS_PREVIOUS_HASH
            ),
            "entry_count": len(records),
            "issued_at": str(issued_at),
        }

    def export_checkpoint(
        self, signing_private_key_path: str, *, issued_at: str
    ) -> SignedTrustedArtifact:
        """
        用仓库外的审计权威私钥签发当前链检查点。

        只读：不追加、不修改任何审计事件。
        """
        return sign_trusted_artifact(
            signing_private_key_path,
            artifact_type=SHADOW_AUDIT_CHECKPOINT_TYPE,
            payload=self.checkpoint_payload(issued_at=issued_at),
        )

    def verify_external_checkpoint(
        self,
        checkpoint: SignedTrustedArtifact | Mapping[str, Any] | None,
        *,
        trusted_public_key_path: str | None,
        current_time: str,
        max_age_seconds: float,
    ) -> dict:
        """
        **只读**验证外部签名检查点与当前链**完全一致**。

        检查点长度/链尖必须精确匹配当前历史：任何尾部截断、尾部追加或全链重写
        都会导致不匹配并 fail closed。
        """
        try:
            payload = verify_trusted_artifact(
                checkpoint,
                expected_artifact_type=SHADOW_AUDIT_CHECKPOINT_TYPE,
                trusted_public_key_path=trusted_public_key_path,
                current_time=current_time,
                max_age_seconds=max_age_seconds,
            )
        except TrustedArtifactError as exc:
            raise ShadowAuditIntegrityError(
                f"external audit checkpoint is invalid: {exc}",
                rejection_codes=("CHECKPOINT_INVALID",),
            ) from exc
        if str(payload.get("subject_identity")) != self.store_identity():
            raise ShadowAuditIntegrityError(
                "external audit checkpoint does not bind this audit store",
                rejection_codes=("CHECKPOINT_MISMATCH",),
            )
        if str(payload.get("dataset_identity")) != self.dataset_identity:
            raise ShadowAuditIntegrityError(
                "external audit checkpoint dataset identity mismatch",
                rejection_codes=("CHECKPOINT_MISMATCH",),
            )
        records = self._read_strict()
        self._validate_records(records)
        expected_tip = records[-1].entry_hash if records else GENESIS_PREVIOUS_HASH
        if (
            int(payload.get("entry_count", -1)) != len(records)
            or str(payload.get("chain_tip")) != expected_tip
        ):
            raise ShadowAuditIntegrityError(
                "external audit checkpoint does not match the current chain "
                "(truncation, append or rewrite detected)",
                rejection_codes=("CHECKPOINT_MISMATCH",),
            )
        return payload

    def verify(self) -> dict:
        """
        **只读**完整性校验：重算哈希链、序号连续性、前驱绑定、事件 ID 唯一性、
        事件类型与记录形状。

        任何不一致都抛 ``ShadowAuditIntegrityError``（fail closed，绝不修复）。
        """
        records = self._read_strict()
        self._validate_records(records)
        previous = records[-1].entry_hash if records else GENESIS_PREVIOUS_HASH

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
            "strict_format": STRICT_AUDIT_FORMAT_STATEMENT,
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

        with self._lock_factory(self._lock_path):
            records = self._read_strict()
            # 追加**之前**完整校验既有历史（不重新加锁，避免递归死锁）。
            self._validate_records(records)
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
        """
        按**严格格式**读取全部记录（只读；不做任何修复）。

        绝不忽略空白物理行、绝不接受未终止的尾记录、绝不接受 ``\r``。
        """
        if not self.exists():
            return []
        with open(self._path, "rb") as handle:
            raw_bytes = handle.read()
        if not raw_bytes:
            return []
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ShadowAuditIntegrityError(
                "audit history is not valid UTF-8",
                rejection_codes=("CORRUPT_EVENT_RECORD",),
            ) from exc
        if "\r" in raw:
            raise ShadowAuditIntegrityError(
                "audit history uses CR characters; the strict format is LF-only",
                rejection_codes=("CORRUPT_EVENT_RECORD",),
            )
        if not raw.endswith("\n"):
            raise ShadowAuditIntegrityError(
                "audit history has an unterminated final record",
                rejection_codes=("TORN_FINAL_RECORD", "UNTERMINATED_FINAL_RECORD"),
            )
        body = raw[:-1]
        if body == "":
            raise ShadowAuditIntegrityError(
                "audit history contains a blank physical line",
                rejection_codes=("UNEXPECTED_BLANK_LINE",),
            )
        lines = body.split("\n")
        records: list[ShadowAuditRecord] = []
        names = {field.name for field in fields(ShadowAuditRecord)}
        for index, line in enumerate(lines):
            if not line.strip():
                raise ShadowAuditIntegrityError(
                    f"audit line {index} is a blank physical line",
                    rejection_codes=("UNEXPECTED_BLANK_LINE",),
                )
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
            missing = sorted(name for name in names if name not in payload)
            if missing:
                raise ShadowAuditIntegrityError(
                    f"audit line {index} is missing fields {missing}",
                    rejection_codes=("CORRUPT_EVENT_RECORD",),
                )
            unexpected = sorted(set(payload) - names)
            if unexpected:
                raise ShadowAuditIntegrityError(
                    f"audit line {index} has unsupported fields {unexpected}",
                    rejection_codes=("CORRUPT_EVENT_RECORD",),
                )
            try:
                payload["payload"] = tuple(
                    sorted(
                        (str(key), str(value))
                        for key, value in dict(payload.get("payload") or {}).items()
                    )
                )
                records.append(ShadowAuditRecord(**payload))
            except (TypeError, ValueError) as exc:
                raise ShadowAuditIntegrityError(
                    f"audit line {index} has an unsupported record shape: {exc}",
                    rejection_codes=("CORRUPT_EVENT_RECORD",),
                ) from exc
        return records

    def _validate_records(self, records: Sequence[ShadowAuditRecord]) -> None:
        """
        完整校验既有历史（**不加锁**；供 verify / append / recover 复用）。

        校验顺序固定：序号 -> 前驱哈希 -> 条目哈希 -> 事件 ID 唯一性 ->
        事件类型 -> 事件 ID 内容绑定。
        """
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
            if record.event_type not in SHADOW_AUDIT_EVENT_TYPES:
                raise ShadowAuditIntegrityError(
                    f"audit entry {index} has an unsupported event type",
                    rejection_codes=("UNSUPPORTED_EVENT_TYPE",),
                )
            expected_event_id = fingerprint(
                {
                    "content_fingerprint": record.content_fingerprint(),
                    "sequence": record.sequence,
                }
            )
            if record.event_id != expected_event_id:
                raise ShadowAuditIntegrityError(
                    f"audit entry {index} event id does not bind its content",
                    rejection_codes=("MODIFIED_ENTRY",),
                )
            previous = record.entry_hash


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
        "strict_format": STRICT_AUDIT_FORMAT_STATEMENT,
        "append_validates_complete_history": True,
        "trusted_checkpoint_supported": True,
        "trusted_checkpoint_type": SHADOW_AUDIT_CHECKPOINT_TYPE,
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
