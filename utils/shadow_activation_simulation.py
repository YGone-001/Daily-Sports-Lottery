"""
影子激活模拟：快照注册表、显式切换、紧急停用与确定性回滚（**隔离原型**）
========================================================================
本模块把「多个不可变影子快照 + 显式选择 / 切换 + 紧急停用 + 确定性回滚」建模为
**纯影子**抽象。它不激活任何生产运行期映射、不连接 ``refresh()``、不写生产数据、
不改写 Canonical ID。

核心约束
--------
- **快照不可变**，注册绝不修改其内容。
- **注册不产生权威**：注册**绝不**改变活动指针（无「最新即生效」策略，
  无「插入顺序即激活」）。
- **切换必须显式**。
- **快照 ID 与内容一一对应**：同 ID 不同内容一律拒绝。
- **重复登记同内容幂等**。
- **回滚不复活任何已撤销 / 已过期 / 基线不兼容的授权**。
- **回滚不得绕过紧急停用**。
- **回滚必须有已验证的持久审计提交**：没有审计存储、追加失败或提交结果
  不确定都不得报告成功的有效切换，且一律 fail closed。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from utils.runtime_activation_authorization import (
    ActivationPrototypeError,
    ActivationVerificationContext,
    ShadowActivationSnapshot,
)
from utils.runtime_mapping_policy import RUNTIME_MAPPING_POLICY_VERSION
from utils.runtime_mapping_eligibility import RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
from utils.shadow_activation_audit_store import (
    ShadowAuditCommitReceipt,
    ShadowAuditRecord,
)
from utils.shadow_runtime_reader import (
    PROTOTYPE_ONLY,
    PRODUCTION_ACTIVATION,
    PRODUCTION_RUNTIME_READER,
    ShadowReadContext,
    ShadowRuntimeReader,
)
from utils.source_identity import fingerprint
from utils.trusted_artifacts import TrustedArtifactError

#: 模拟层版本。
#:
#: ``1.2.0``：回滚要求**已验证的持久审计提交收据**；缺少审计存储、
#: 追加失败与提交结果不确定分别处理，任一情况都不得留下新的可消费快照。
#: ``1.3.0``：严格模式回滚要求外部签名审计链检查点（链尖 + 长度精确匹配）。
SHADOW_SIMULATION_VERSION = "1.3.0"

#: 注册表拒绝码。
REGISTRY_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("SNAPSHOT_MALFORMED", "对象不是影子激活快照。"),
    ("SNAPSHOT_FINGERPRINT_MISMATCH", "快照内容指纹无法独立复算或与记录不一致。"),
    ("SHADOW_SNAPSHOT_ID_CONFLICT", "同一快照 ID 对应了不同内容。"),
    ("SNAPSHOT_NOT_REGISTERED", "快照 ID 未登记。"),
)

#: 回滚拒绝码。
ROLLBACK_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("ROLLBACK_TARGET_NOT_REGISTERED", "目标快照未登记。"),
    ("ROLLBACK_TARGET_FINGERPRINT_MISMATCH", "目标快照指纹不可复算。"),
    ("ROLLBACK_DATASET_MISMATCH", "目标快照数据集身份与当前上下文不一致。"),
    ("ROLLBACK_BASELINE_STALE", "目标快照绑定的基线已变化。"),
    ("ROLLBACK_POLICY_VERSION_MISMATCH", "策略版本不一致。"),
    ("ROLLBACK_EVALUATOR_VERSION_MISMATCH", "资格评估器版本不一致。"),
    ("ROLLBACK_AUTHORIZATION_NOT_CONSUMABLE", "目标快照的授权在当前条件下不可消费。"),
    ("ROLLBACK_AUTHORIZATION_EVIDENCE_MISSING", "目标快照的授权缺少可复核证据。"),
    ("ROLLBACK_OWNERSHIP_CONFLICT", "目标快照与既有 Canonical 所有权冲突。"),
    ("ROLLBACK_READER_DISABLED", "读取器处于停用状态；回滚不得绕过紧急停用。"),
    ("ROLLBACK_RECONCILIATION_REQUIRES_DISABLED", "显式停用对账要求读取器保持停用。"),
    ("ROLLBACK_CLOCK_UNAVAILABLE", "未提供可解析的受控时钟。"),
    ("ROLLBACK_AUDIT_PERSISTENCE_FAILED", "审计事件无法持久化；回滚未提交（fail closed）。"),
    ("ROLLBACK_AUDIT_COMMIT_UNVERIFIED", "审计提交结果不确定或收据无法验证；已 fail closed。"),
    ("ROLLBACK_AUDIT_STORE_MISSING", "未配置持久审计存储；回滚绝不视为已提交。"),
    ("ROLLBACK_AUDIT_CHECKPOINT_MISSING", "严格模式要求外部签名审计链检查点。"),
    ("ROLLBACK_AUDIT_CHECKPOINT_INVALID", "外部审计检查点无法验证或与当前链不一致。"),
    ("ROLLBACK_COMMIT_APPLY_FAILED", "审计已提交但有效指针应用失败；已 fail closed。"),
)


class ShadowSnapshotError(ActivationPrototypeError):
    """影子快照注册表错误。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


# =============================================================================
# 快照注册表（影子专用、不可变、确定性）
# =============================================================================

class ShadowSnapshotRegistry:
    """
    影子快照注册表。

    - 快照一经注册即不可变（注册只保存引用，绝不修改内容）。
    - **注册绝不改变活动指针**；选择 / 切换必须显式调用 ``select()``。
    - 快照 ID 必须等于其内容指纹；同 ID 不同内容一律拒绝。
    - 重复登记完全相同的快照是幂等的。
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ShadowActivationSnapshot] = {}
        self._registration_order: list[str] = []
        self._active_id: str | None = None
        self._switch_count = 0

    # -- 注册 -------------------------------------------------------------

    def register(self, snapshot: ShadowActivationSnapshot) -> dict:
        """
        注册一个**已验证**的影子快照。**绝不**改变活动指针。

        返回确定性报告；拒绝时抛 ``ShadowSnapshotError``。
        """
        if not isinstance(snapshot, ShadowActivationSnapshot):
            raise ShadowSnapshotError(
                "only a ShadowActivationSnapshot may be registered",
                rejection_codes=("SNAPSHOT_MALFORMED",),
            )
        if (
            snapshot.snapshot_id != snapshot.snapshot_fingerprint
            or snapshot.content_fingerprint() != snapshot.snapshot_fingerprint
        ):
            raise ShadowSnapshotError(
                "snapshot content fingerprint is not reproducible; refusing to publish",
                rejection_codes=("SNAPSHOT_FINGERPRINT_MISMATCH",),
            )

        existing = self._by_id.get(snapshot.snapshot_id)
        if existing is not None:
            if existing.snapshot_fingerprint != snapshot.snapshot_fingerprint:
                raise ShadowSnapshotError(
                    "the same snapshot id is already registered with different content",
                    rejection_codes=("SHADOW_SNAPSHOT_ID_CONFLICT",),
                )
            return {
                "kind": "SHADOW_SNAPSHOT_REGISTRATION",
                "prototype_only": True,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_fingerprint": snapshot.snapshot_fingerprint,
                "already_registered": True,
                "idempotent": True,
                "registered_count": len(self._registration_order),
                "active_snapshot_id": self._active_id,
                "activation_performed": False,
            }

        self._by_id[snapshot.snapshot_id] = snapshot
        self._registration_order.append(snapshot.snapshot_id)
        return {
            "kind": "SHADOW_SNAPSHOT_REGISTRATION",
            "prototype_only": True,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_fingerprint": snapshot.snapshot_fingerprint,
            "already_registered": False,
            "idempotent": True,
            "registered_count": len(self._registration_order),
            "active_snapshot_id": self._active_id,
            "activation_performed": False,
        }

    # -- 只读视图 ---------------------------------------------------------

    def get(self, snapshot_id: str) -> ShadowActivationSnapshot | None:
        return self._by_id.get(str(snapshot_id))

    def fingerprint_of(self, snapshot_id: str) -> str | None:
        snapshot = self._by_id.get(str(snapshot_id))
        return snapshot.snapshot_fingerprint if snapshot is not None else None

    def history(self) -> tuple[str, ...]:
        """按**注册顺序**列出全部快照 ID（仅供检视；顺序不构成权威）。"""
        return tuple(self._registration_order)

    def snapshots(self) -> tuple[ShadowActivationSnapshot, ...]:
        return tuple(self._by_id[key] for key in self._registration_order)

    def __len__(self) -> int:
        return len(self._registration_order)

    @property
    def active_snapshot_id(self) -> str | None:
        return self._active_id

    def active(self) -> ShadowActivationSnapshot | None:
        if self._active_id is None:
            return None
        return self._by_id.get(self._active_id)

    def previous_snapshot_id(self) -> str | None:
        """活动快照之前的那个快照（注册顺序），用于回滚目标建议。"""
        if self._active_id is None:
            return None
        try:
            index = self._registration_order.index(self._active_id)
        except ValueError:
            return None
        return self._registration_order[index - 1] if index > 0 else None

    # -- 显式选择 / 切换 ---------------------------------------------------

    def select(self, snapshot_id: str) -> dict:
        """
        **显式**选择活动影子快照（未登记即拒绝）。

        返回确定性切换报告；切换**只**改变影子活动指针。
        """
        target = self._by_id.get(str(snapshot_id))
        if target is None:
            raise ShadowSnapshotError(
                f"shadow snapshot {snapshot_id!r} is not registered",
                rejection_codes=("SNAPSHOT_NOT_REGISTERED",),
            )
        if target.content_fingerprint() != target.snapshot_fingerprint:
            raise ShadowSnapshotError(
                "refusing to select a snapshot whose fingerprint is not reproducible",
                rejection_codes=("SNAPSHOT_FINGERPRINT_MISMATCH",),
            )
        previous = self._active_id
        self._active_id = target.snapshot_id
        self._switch_count += 1
        return {
            "kind": "SHADOW_SNAPSHOT_SWITCH",
            "prototype_only": True,
            "previous_snapshot_id": previous,
            "snapshot_id": target.snapshot_id,
            "snapshot_fingerprint": target.snapshot_fingerprint,
            "switched": previous != target.snapshot_id,
            "switch_count": self._switch_count,
            "latest_wins_applied": False,
            "production_flags_modified": False,
            "canonical_writes": 0,
        }

    def switch(self, snapshot_id: str) -> dict:
        """``select`` 的显式别名（语义完全相同）。"""
        return self.select(snapshot_id)


# =============================================================================
# 影子激活模拟器（停用 + 回滚）
# =============================================================================

@dataclass(frozen=True)
class ShadowRollbackReport:
    """确定性回滚报告（不可变）。"""

    status: str
    target_snapshot_id: str
    previous_snapshot_id: str | None
    effective_snapshot_id: str | None
    rejection_codes: tuple[str, ...]
    recovery_fingerprint: str
    checked_at: str
    audit_event_types: tuple[str, ...]
    #: 完成事件的持久化是回滚的提交点；``False`` 表示没有可验证的持久提交。
    audit_committed: bool = True
    #: 审计失败时是否已 fail closed 停用读取器（绝不留部分启用状态）。
    reader_disabled: bool = False
    #: 是否配置了持久审计存储（``None`` 绝不等价于成功持久化）。
    audit_store_attached: bool = False
    #: 提交结果：``NOT_ATTEMPTED`` / ``NOT_COMMITTED`` / ``COMMITTED`` / ``UNCERTAIN``。
    commit_outcome: str = "NOT_ATTEMPTED"
    #: 回读是否**观察到**对应的完成事件（不代表持久性已被确认）。
    completion_event_observed: bool = False
    #: 有效影子指针是否真的完成切换。
    pointer_applied: bool = False
    #: 已验证提交收据的指纹（未验证时为空）。
    receipt_fingerprint: str = ""
    #: 审计记录与有效状态之间可观察到的差异说明。
    discrepancy: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "ROLLBACK_COMPLETED"

    def as_dict(self) -> dict:
        return {
            "kind": "SHADOW_ROLLBACK_REPORT",
            "prototype_only": True,
            "shadow_only": True,
            "status": self.status,
            "target_snapshot_id": self.target_snapshot_id,
            "previous_snapshot_id": self.previous_snapshot_id,
            "effective_snapshot_id": self.effective_snapshot_id,
            "rejection_codes": list(self.rejection_codes),
            "recovery_fingerprint": self.recovery_fingerprint,
            "checked_at": self.checked_at,
            "audit_event_types": list(self.audit_event_types),
            "audit_committed": self.audit_committed,
            "reader_disabled": self.reader_disabled,
            "audit_store_attached": self.audit_store_attached,
            "commit_outcome": self.commit_outcome,
            "completion_event_observed": self.completion_event_observed,
            "pointer_applied": self.pointer_applied,
            "receipt_fingerprint": self.receipt_fingerprint,
            "discrepancy": self.discrepancy,
            "canonical_writes": 0,
            "dependent_store_writes": 0,
            "restores_revoked_authorization": False,
            "restores_expired_authorization": False,
            "reissues_owner_approval": False,
            "regenerates_activation_signature": False,
            "deletes_newer_snapshot_evidence": False,
            "bypasses_emergency_disable": False,
            "is_production_authority": False,
        }


def _parse_timestamp(value: Any):
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class _AuditEmission:
    """一次回滚审计追加的结果分类（内部、只读）。"""

    outcome: str  # COMMITTED / NOT_COMMITTED / UNCERTAIN / STORE_MISSING
    receipt: ShadowAuditCommitReceipt | None
    event_observed: bool


class ShadowActivationSimulator:
    """
    影子激活模拟器：在**隔离**的合成环境里演练停用与回滚。

    绝不修改 Canonical ID、绝不重建历史 JSON 存储、绝不恢复已撤销 / 已过期的授权、
    绝不重新签发所有者批准或激活签名、绝不删除较新的快照证据、绝不绕过紧急停用。
    """

    def __init__(
        self,
        *,
        registry: ShadowSnapshotRegistry,
        reader: ShadowRuntimeReader,
        audit_store: Any = None,
        require_trusted_audit_checkpoint: bool = False,
        audit_checkpoint_trusted_public_key_path: str | None = None,
        audit_checkpoint_max_age_seconds: float = 3600.0,
    ) -> None:
        self._registry = registry
        self._reader = reader
        self._audit = audit_store
        self._last_disable: dict | None = None
        self._require_trusted_audit_checkpoint = bool(
            require_trusted_audit_checkpoint
        )
        self._audit_checkpoint_trusted_public_key_path = (
            audit_checkpoint_trusted_public_key_path
        )
        self._audit_checkpoint_max_age_seconds = float(
            audit_checkpoint_max_age_seconds
        )

    # -- 只读视图 ---------------------------------------------------------

    @property
    def registry(self) -> ShadowSnapshotRegistry:
        return self._registry

    @property
    def reader(self) -> ShadowRuntimeReader:
        return self._reader

    def status(self) -> dict:
        return {
            "shadow_simulation_version": SHADOW_SIMULATION_VERSION,
            "prototype_only": PROTOTYPE_ONLY,
            "registered_snapshots": len(self._registry),
            "active_snapshot_id": self._registry.active_snapshot_id,
            "reader_enabled": self._reader.enabled,
            "reader_generation": self._reader.generation,
            "audit_store_attached": self._audit is not None,
            "require_trusted_audit_checkpoint": (
                self._require_trusted_audit_checkpoint
            ),
            "production_runtime_reader": PRODUCTION_RUNTIME_READER,
            "production_activation": PRODUCTION_ACTIVATION,
        }

    # -- 审计（可选） -----------------------------------------------------

    def _emit(
        self,
        event_type: str,
        *,
        at: str,
        actor: str,
        actor_role: str,
        activation_id: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if self._audit is None:
            return
        self._audit.append(
            event_type=event_type,
            activation_id=activation_id,
            occurred_at=at,
            actor=actor,
            actor_role=actor_role,
            payload=dict(payload or {}),
        )

    # -- 紧急停用 ---------------------------------------------------------

    def disable_shadow_runtime(
        self,
        *,
        at: str,
        actor: str = "shadow-operator",
        reason: str = "emergency disable",
    ) -> dict:
        """
        影子紧急停用。

        - 立即停止影子绑定消费；使活动影子指针失效；使此前取得的句柄失效（代际 +1）。
        - 保留历史快照与历史审计事件。
        - **不需要**任何激活授权或所有者签名。
        - 不做任何 Canonical / 依赖存储写入。
        - 内部失败时有效状态仍为「不可消费」（绝不部分启用）。
        """
        report = self._reader.disable(at=at, actor=actor, reason=reason)
        self._registry_invalidate_active()
        self._emit(
            "SHADOW_RUNTIME_DISABLED",
            at=at,
            actor=actor,
            actor_role="shadow-operator",
            payload={"reason": reason, "reader_generation": report["reader_generation"]},
        )
        self._last_disable = report
        return report

    def _registry_invalidate_active(self) -> None:
        """停用时让活动指针失效（保留全部历史快照供检视）。"""
        self._registry._active_id = None  # noqa: SLF001  (刻意的最小侵入式失效)

    # -- 确定性回滚 -------------------------------------------------------

    def rollback_shadow_snapshot(
        self,
        *,
        target_snapshot_id: str,
        read_context: ShadowReadContext,
        at: str,
        actor: str = "shadow-operator",
        reason: str = "deterministic rollback",
        audit_checkpoint: Any = None,
        reconcile_disabled_reader: bool = False,
    ) -> ShadowRollbackReport:
        """
        **确定性**回滚到显式指定的历史快照。

        目标快照**不**因为「曾经发布过」就自动安全：必须通过数据集、基线、版本、
        授权可消费性（含撤销与有效期）与所有权兼容性的**当前**校验。

        **提交点**：``SHADOW_ROLLBACK_COMPLETED`` 事件的**已验证持久提交**。
        回滚必须显式配置审计存储；``audit_store=None`` 绝不等价于成功持久化。
        只有该事件被回读验证为当前链尖、且收据与事件逐字段一致之后，才切换
        有效影子指针。

        审计结果分四类：未配置存储、未提交、已提交、结果不确定。失败或不确定
        一律 fail closed（停用读取器并失效活动指针），绝不留新的可消费快照，
        也绝不改写或截断任何既有审计字节。
        """
        # reconcile_disabled_reader is a separately requested shadow recovery
        # action: external checkpoint and current activation evidence are required,
        # and it cannot enable consumption. Guarded re_enable remains mandatory.
        previous = self._registry.active_snapshot_id

        # An explicit recovery reconciliation may select a currently valid target
        # while consumption remains disabled. It requires an external checkpoint;
        # ordinary rollback still rejects disabled readers (legacy contract).
        try:
            read_context = self._reader._authenticated_context(read_context)
        except TrustedArtifactError as exc:
            return self._rollback_report(status="ROLLBACK_REJECTED",
                target_snapshot_id=str(target_snapshot_id), previous=previous,
                effective=previous, rejection_codes=exc.rejection_codes,
                read_context=read_context, audit_event_types=(), audit_committed=False,
                reader_disabled=not self._reader.enabled,
                audit_store_attached=self._audit is not None, commit_outcome="NOT_ATTEMPTED")

        if self._audit is None:
            # 没有任何持久审计存储：回滚绝不视为已提交。
            return self._rollback_report(
                status="ROLLBACK_AUDIT_FAILED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=previous,
                rejection_codes=("ROLLBACK_AUDIT_STORE_MISSING",),
                read_context=read_context,
                audit_event_types=(),
                audit_committed=False,
                audit_store_attached=False,
                commit_outcome="NOT_ATTEMPTED",
                pointer_applied=False,
                discrepancy=(
                    "no persistent audit store is configured; no rollback transition "
                    "was attempted"
                ),
            )

        if self._require_trusted_audit_checkpoint or reconcile_disabled_reader:
            if (
                audit_checkpoint is None
                or not self._audit_checkpoint_trusted_public_key_path
            ):
                return self._rollback_report(
                    status="ROLLBACK_AUDIT_FAILED",
                    target_snapshot_id=str(target_snapshot_id),
                    previous=previous,
                    effective=previous,
                    rejection_codes=("ROLLBACK_AUDIT_CHECKPOINT_MISSING",),
                    read_context=read_context,
                    audit_event_types=(),
                    audit_committed=False,
                    audit_store_attached=True,
                    commit_outcome="NOT_ATTEMPTED",
                    discrepancy=(
                        "strict mode requires an externally signed audit chain "
                        "checkpoint; no transition was attempted"
                    ),
                )
            try:
                self._audit.verify_external_checkpoint(
                    audit_checkpoint,
                    trusted_public_key_path=(
                        self._audit_checkpoint_trusted_public_key_path
                    ),
                    current_time=read_context.current_time,
                    max_age_seconds=self._audit_checkpoint_max_age_seconds,
                )
            except Exception:  # noqa: BLE001
                return self._rollback_report(
                    status="ROLLBACK_AUDIT_FAILED",
                    target_snapshot_id=str(target_snapshot_id),
                    previous=previous,
                    effective=previous,
                    rejection_codes=("ROLLBACK_AUDIT_CHECKPOINT_INVALID",),
                    read_context=read_context,
                    audit_event_types=(),
                    audit_committed=False,
                    audit_store_attached=True,
                    commit_outcome="NOT_ATTEMPTED",
                    discrepancy=(
                        "external audit checkpoint does not match the current chain; "
                        "truncation or rewrite is possible; no transition was attempted"
                    ),
                )

        requested = self._emit_verified_event(
            "SHADOW_ROLLBACK_REQUESTED",
            at=at,
            actor=actor,
            payload={"target_snapshot_id": str(target_snapshot_id), "reason": reason},
        )
        if requested.outcome != "COMMITTED":
            code = (
                "ROLLBACK_AUDIT_PERSISTENCE_FAILED"
                if requested.outcome == "NOT_COMMITTED"
                else "ROLLBACK_AUDIT_COMMIT_UNVERIFIED"
            )
            return self._rollback_report(
                status="ROLLBACK_AUDIT_FAILED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=previous,
                rejection_codes=(code,),
                read_context=read_context,
                audit_event_types=(
                    ("SHADOW_ROLLBACK_REQUESTED",) if requested.event_observed else ()
                ),
                audit_committed=False,
                audit_store_attached=True,
                commit_outcome=requested.outcome,
                completion_event_observed=False,
                discrepancy=(
                    "request event observed in the audit store without a durable "
                    "acknowledgment"
                    if requested.event_observed
                    else ""
                ),
            )

        rejection_codes = self._rollback_rejections(
            target_snapshot_id=str(target_snapshot_id), read_context=read_context,
            reconcile_disabled_reader=reconcile_disabled_reader,
        )

        if rejection_codes:
            rejected = self._emit_verified_event(
                "SHADOW_ROLLBACK_REJECTED",
                at=at,
                actor=actor,
                payload={
                    "target_snapshot_id": str(target_snapshot_id),
                    "rejection_codes": ",".join(sorted(rejection_codes)),
                    "effective_snapshot_id": previous or "",
                },
            )
            codes = set(rejection_codes)
            if rejected.outcome == "NOT_COMMITTED":
                codes.add("ROLLBACK_AUDIT_PERSISTENCE_FAILED")
            elif rejected.outcome != "COMMITTED":
                codes.add("ROLLBACK_AUDIT_COMMIT_UNVERIFIED")
            audit_types = ["SHADOW_ROLLBACK_REQUESTED"]
            if rejected.event_observed:
                audit_types.append("SHADOW_ROLLBACK_REJECTED")
            return self._rollback_report(
                status="ROLLBACK_REJECTED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=previous,
                rejection_codes=tuple(sorted(codes)),
                read_context=read_context,
                audit_event_types=tuple(audit_types),
                audit_committed=(rejected.outcome == "COMMITTED"),
                audit_store_attached=True,
                commit_outcome="NOT_ATTEMPTED",
                discrepancy=(
                    ""
                    if rejected.outcome == "COMMITTED"
                    else "rejection event could not be durably committed"
                ),
            )

        # 提交点：先获得**已验证的持久收据**；失败或不确定则绝不切换有效指针。
        completed = self._emit_verified_event(
            "SHADOW_ROLLBACK_COMPLETED",
            at=at,
            actor=actor,
            payload={
                "target_snapshot_id": str(target_snapshot_id),
                "previous_snapshot_id": previous or "",
                "effective_snapshot_id": str(target_snapshot_id),
            },
        )
        if completed.outcome != "COMMITTED":
            self._fail_closed_after_audit_failure(
                at=at, actor=actor, reason="rollback audit commitment failed"
            )
            if completed.outcome == "NOT_COMMITTED":
                code = "ROLLBACK_AUDIT_PERSISTENCE_FAILED"
                discrepancy = (
                    "completion event was not observed in the audit store; "
                    "no effective pointer switch was applied"
                )
            else:
                code = "ROLLBACK_AUDIT_COMMIT_UNVERIFIED"
                discrepancy = (
                    "completion event is present in the audit store but its "
                    "durability was not acknowledged; no effective pointer switch "
                    "was applied"
                    if completed.event_observed
                    else "completion event durability is uncertain; the audit store "
                    "could not be conclusively read back; no effective pointer switch "
                    "was applied"
                )
            audit_types = ["SHADOW_ROLLBACK_REQUESTED"]
            if completed.event_observed:
                audit_types.append("SHADOW_ROLLBACK_COMPLETED")
            return self._rollback_report(
                status="ROLLBACK_AUDIT_FAILED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=self._registry.active_snapshot_id,
                rejection_codes=(code,),
                read_context=read_context,
                audit_event_types=tuple(audit_types),
                audit_committed=False,
                reader_disabled=True,
                audit_store_attached=True,
                commit_outcome=completed.outcome,
                completion_event_observed=completed.event_observed,
                pointer_applied=False,
                discrepancy=discrepancy,
            )

        # 收据已验证：有效指针切换是确定性的内存操作。
        try:
            self._registry.select(str(target_snapshot_id))
        except BaseException:  # noqa: BLE001  (理论上不可达；保守 fail closed)
            self._fail_closed_after_audit_failure(
                at=at, actor=actor, reason="rollback commit could not be applied"
            )
            return self._rollback_report(
                status="ROLLBACK_AUDIT_FAILED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=self._registry.active_snapshot_id,
                rejection_codes=("ROLLBACK_COMMIT_APPLY_FAILED",),
                read_context=read_context,
                audit_event_types=("SHADOW_ROLLBACK_REQUESTED",),
                audit_committed=True,
                reader_disabled=True,
                audit_store_attached=True,
                commit_outcome="COMMITTED",
                completion_event_observed=True,
                pointer_applied=False,
                discrepancy=(
                    "completion event was durably committed but the effective "
                    "pointer could not be applied"
                ),
            )

        effective = self._registry.active_snapshot_id
        receipt = completed.receipt
        return self._rollback_report(
            status="ROLLBACK_COMPLETED",
            target_snapshot_id=str(target_snapshot_id),
            previous=previous,
            effective=effective,
            rejection_codes=(),
            read_context=read_context,
            audit_event_types=(
                "SHADOW_ROLLBACK_REQUESTED",
                "SHADOW_ROLLBACK_COMPLETED",
            ),
            audit_committed=True,
            reader_disabled=not self._reader.enabled,
            audit_store_attached=True,
            commit_outcome="COMMITTED",
            completion_event_observed=True,
            pointer_applied=True,
            receipt_fingerprint=(
                fingerprint(receipt.as_dict()) if receipt is not None else ""
            ),
        )

    # -- 审计 / 失败路径 ---------------------------------------------------

    def _emit_verified_event(
        self, event_type: str, *, at: str, actor: str, payload: Mapping[str, Any]
    ) -> "_AuditEmission":
        """
        追加一条回滚审计事件并要求**可验证的持久提交收据**。

        不接受任何仅凭异常之外的「看起来成功」：必须回读同一存储、确认事件
        位于已验证链尖、且收据与其逐字段一致。否则显式区分未提交与不确定。
        """
        if self._audit is None:
            return _AuditEmission("STORE_MISSING", None, False)
        try:
            result = self._audit.append(
                event_type=event_type,
                activation_id="",
                occurred_at=at,
                actor=actor,
                actor_role="shadow-operator",
                payload=dict(payload),
            )
        except Exception:  # noqa: BLE001  (审计 I/O 与完整性失败)
            return self._classify_unacknowledged_event(
                event_type, at=at, actor=actor, payload=payload
            )
        if not isinstance(result, ShadowAuditRecord):
            return self._classify_unacknowledged_event(
                event_type, at=at, actor=actor, payload=payload
            )
        return self._verify_acknowledged_event(
            result, event_type, at=at, actor=actor, payload=payload
        )

    def _classify_unacknowledged_event(
        self, event_type: str, *, at: str, actor: str, payload: Mapping[str, Any]
    ) -> "_AuditEmission":
        """
        没有可靠成功确认时，以回读结果区分「未提交」与「结果不确定」。

        异常**绝不**足以证明事件未持久化：``fsync`` 可能在字节已到达存储后失败。
        """
        records, _identity = self._read_back()
        if records is None:
            return _AuditEmission("UNCERTAIN", None, False)
        observed = self._last_record_matches(
            records, event_type, at=at, actor=actor, payload=payload
        )
        return _AuditEmission(
            "UNCERTAIN" if observed else "NOT_COMMITTED", None, observed
        )

    def _verify_acknowledged_event(
        self,
        record: ShadowAuditRecord,
        event_type: str,
        *,
        at: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> "_AuditEmission":
        """回读并验证提交收据：逐字段对应、位于当前链尖、收据身份一致。"""
        records, identity = self._read_back()
        if records is None:
            return _AuditEmission("UNCERTAIN", None, False)
        try:
            receipt = self._audit.receipt_for(record)
        except Exception:  # noqa: BLE001  (不允许静默降级为「已提交」)
            return _AuditEmission("UNCERTAIN", None, False)
        if not isinstance(receipt, ShadowAuditCommitReceipt):
            return _AuditEmission("UNCERTAIN", None, False)
        if receipt.store_identity != identity or not receipt.matches(record):
            return _AuditEmission("UNCERTAIN", None, False)
        if record.sequence < 0 or record.sequence >= len(records):
            return _AuditEmission("UNCERTAIN", None, False)
        stored = records[record.sequence]
        if stored != record or record.sequence != len(records) - 1:
            return _AuditEmission("UNCERTAIN", None, False)
        if not self._record_matches(
            stored, event_type, at=at, actor=actor, payload=payload
        ):
            return _AuditEmission("UNCERTAIN", None, False)
        return _AuditEmission("COMMITTED", receipt, True)

    def _read_back(self):
        """回读并校验整个审计链；任何失败都返回 ``(None, None)``。"""
        try:
            records = self._audit.records()
            self._audit.verify()
            identity = self._audit.store_identity()
        except Exception:  # noqa: BLE001  (读取 / 校验失败即视为不可判定)
            return None, None
        if not isinstance(records, (tuple, list)):
            return None, None
        return tuple(records), str(identity)

    def _record_matches(
        self,
        record: ShadowAuditRecord,
        event_type: str,
        *,
        at: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> bool:
        expected_payload = {str(key): str(value) for key, value in payload.items()}
        return bool(
            record.event_type == event_type
            and record.occurred_at == str(at)
            and record.actor == str(actor)
            and record.activation_id == ""
            and record.snapshot_id == ""
            and dict(record.payload) == expected_payload
        )

    def _last_record_matches(
        self,
        records: tuple[ShadowAuditRecord, ...],
        event_type: str,
        *,
        at: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> bool:
        if not records:
            return False
        return self._record_matches(
            records[-1], event_type, at=at, actor=actor, payload=payload
        )

    def _fail_closed_after_audit_failure(
        self, *, at: str, actor: str, reason: str
    ) -> None:
        """审计失败后的非消费状态：停用读取器并让活动指针失效。"""
        self._reader.disable(at=at, actor=actor, reason=reason)
        self._registry_invalidate_active()

    # -- 内部 -------------------------------------------------------------

    def _rollback_rejections(
        self, *, target_snapshot_id: str, read_context: ShadowReadContext,
        reconcile_disabled_reader: bool = False,
    ) -> list[str]:
        rejections: list[str] = []

        if not self._reader.enabled and not reconcile_disabled_reader:
            rejections.append("ROLLBACK_READER_DISABLED")
        if self._reader.enabled and reconcile_disabled_reader:
            rejections.append("ROLLBACK_RECONCILIATION_REQUIRES_DISABLED")
        if _parse_timestamp(read_context.current_time) is None:
            rejections.append("ROLLBACK_CLOCK_UNAVAILABLE")

        target = self._registry.get(target_snapshot_id)
        if target is None:
            rejections.append("ROLLBACK_TARGET_NOT_REGISTERED")
            return rejections
        if target.content_fingerprint() != target.snapshot_fingerprint:
            rejections.append("ROLLBACK_TARGET_FINGERPRINT_MISMATCH")

        if target.dataset_identity != read_context.dataset_identity:
            rejections.append("ROLLBACK_DATASET_MISMATCH")
        if target.baseline_fingerprint != read_context.current_baseline_fingerprint:
            rejections.append("ROLLBACK_BASELINE_STALE")
        if target.policy_version != read_context.policy_version:
            rejections.append("ROLLBACK_POLICY_VERSION_MISMATCH")
        if target.evaluator_version != read_context.evaluator_version:
            rejections.append("ROLLBACK_EVALUATOR_VERSION_MISMATCH")

        known_ids = read_context.canonical_id_set()
        for binding in target.bindings:
            evidence = read_context.evidence_for(binding.activation_id)
            if evidence is None:
                rejections.append("ROLLBACK_AUTHORIZATION_EVIDENCE_MISSING")
                continue
            verification = self._reader._verify_authorization(
                evidence.authorization,
                ActivationVerificationContext(
                    dataset_identity=read_context.dataset_identity,
                    current_baseline_fingerprint=(
                        read_context.current_baseline_fingerprint
                    ),
                    current_time=read_context.current_time,
                    trusted_public_key_path=read_context.trusted_public_key_path,
                    eligibility_decision=evidence.decision,
                    proposal=evidence.proposal,
                    revocation_registry=read_context.revocation_registry,
                ),
                read_context=read_context,
            )
            if not verification.is_consumable:
                rejections.append("ROLLBACK_AUTHORIZATION_NOT_CONSUMABLE")
            existing_owners = read_context.existing_owners_for(
                binding.source_identity_key
            )
            if existing_owners and set(existing_owners) != {
                binding.candidate_canonical_id
            }:
                rejections.append("ROLLBACK_OWNERSHIP_CONFLICT")
            if known_ids and binding.candidate_canonical_id not in known_ids:
                rejections.append("ROLLBACK_OWNERSHIP_CONFLICT")

        return sorted(set(rejections))

    def _rollback_report(
        self,
        *,
        status: str,
        target_snapshot_id: str,
        previous: str | None,
        effective: str | None,
        rejection_codes: tuple[str, ...],
        read_context: ShadowReadContext,
        audit_event_types: tuple[str, ...],
        audit_committed: bool = True,
        reader_disabled: bool = False,
        audit_store_attached: bool = False,
        commit_outcome: str = "NOT_ATTEMPTED",
        completion_event_observed: bool = False,
        pointer_applied: bool = False,
        receipt_fingerprint: str = "",
        discrepancy: str = "",
    ) -> ShadowRollbackReport:
        target = self._registry.get(target_snapshot_id)
        recovery_fingerprint = fingerprint(
            {
                "kind": "SHADOW_ROLLBACK_RECOVERY",
                "status": status,
                "target_snapshot_id": target_snapshot_id,
                "target_snapshot_fingerprint": (
                    target.snapshot_fingerprint if target is not None else ""
                ),
                "previous_snapshot_id": previous or "",
                "effective_snapshot_id": effective or "",
                "dataset_identity": read_context.dataset_identity,
                "baseline_fingerprint": read_context.current_baseline_fingerprint,
                "policy_version": read_context.policy_version,
                "evaluator_version": read_context.evaluator_version,
                "rejection_codes": list(rejection_codes),
                "reader_generation": self._reader.generation,
            }
        )
        return ShadowRollbackReport(
            status=status,
            target_snapshot_id=target_snapshot_id,
            previous_snapshot_id=previous,
            effective_snapshot_id=effective,
            rejection_codes=rejection_codes,
            recovery_fingerprint=recovery_fingerprint,
            checked_at=read_context.current_time,
            audit_event_types=audit_event_types,
            audit_committed=audit_committed,
            reader_disabled=reader_disabled,
            audit_store_attached=audit_store_attached,
            commit_outcome=commit_outcome,
            completion_event_observed=completion_event_observed,
            pointer_applied=pointer_applied,
            receipt_fingerprint=receipt_fingerprint,
            discrepancy=discrepancy,
        )


def shadow_simulation_status() -> dict:
    """模拟层的只读状态视图。"""
    return {
        "shadow_simulation_version": SHADOW_SIMULATION_VERSION,
        "prototype_only": PROTOTYPE_ONLY,
        "production_runtime_reader": PRODUCTION_RUNTIME_READER,
        "production_activation": PRODUCTION_ACTIVATION,
        "policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "registry_rejection_codes": [code for code, _ in REGISTRY_REJECTION_CODES],
        "rollback_rejection_codes": [code for code, _ in ROLLBACK_REJECTION_CODES],
        "requires_durable_audit_store_for_rollback": True,
        "commit_receipt_required": True,
        "writes_files": False,
        "creates_bindings": False,
        "modifies_canonical_records": False,
        "consumed_by_production_runtime": False,
    }


def _assert_simulation_is_coherent() -> None:
    if not PROTOTYPE_ONLY:
        raise AssertionError("shadow simulation must declare itself prototype-only")
    if PRODUCTION_ACTIVATION != "NOT_ENABLED":
        raise AssertionError("runtime activation must remain NOT_ENABLED")
    if PRODUCTION_RUNTIME_READER != "NOT_ENABLED":
        raise AssertionError("the production runtime reader must remain NOT_ENABLED")
    for codes in (REGISTRY_REJECTION_CODES, ROLLBACK_REJECTION_CODES):
        values = [code for code, _ in codes]
        if len(set(values)) != len(values):
            raise AssertionError("shadow simulation rejection codes must be unique")


_assert_simulation_is_coherent()
