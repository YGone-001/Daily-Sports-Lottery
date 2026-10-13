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
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from utils.runtime_activation_authorization import (
    ActivationPrototypeError,
    ActivationVerificationContext,
    ShadowActivationSnapshot,
    verify_runtime_activation_authorization,
)
from utils.runtime_mapping_policy import RUNTIME_MAPPING_POLICY_VERSION
from utils.runtime_mapping_eligibility import RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
from utils.shadow_runtime_reader import (
    PROTOTYPE_ONLY,
    PRODUCTION_ACTIVATION,
    PRODUCTION_RUNTIME_READER,
    ShadowReadContext,
    ShadowRuntimeReader,
)
from utils.source_identity import fingerprint

#: 模拟层版本。
SHADOW_SIMULATION_VERSION = "1.0.0"

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
    ("ROLLBACK_CLOCK_UNAVAILABLE", "未提供可解析的受控时钟。"),
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
    ) -> None:
        self._registry = registry
        self._reader = reader
        self._audit = audit_store
        self._last_disable: dict | None = None

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
    ) -> ShadowRollbackReport:
        """
        **确定性**回滚到显式指定的历史快照。

        目标快照**不**因为「曾经发布过」就自动安全：必须通过数据集、基线、版本、
        授权可消费性（含撤销与有效期）与所有权兼容性的**当前**校验。

        任何失败都不会改变有效影子状态（绝不部分启用）。
        """
        self._emit(
            "SHADOW_ROLLBACK_REQUESTED",
            at=at,
            actor=actor,
            actor_role="shadow-operator",
            payload={"target_snapshot_id": str(target_snapshot_id), "reason": reason},
        )

        rejection_codes = self._rollback_rejections(
            target_snapshot_id=str(target_snapshot_id), read_context=read_context
        )
        previous = self._registry.active_snapshot_id

        if rejection_codes:
            self._emit(
                "SHADOW_ROLLBACK_REJECTED",
                at=at,
                actor=actor,
                actor_role="shadow-operator",
                payload={
                    "target_snapshot_id": str(target_snapshot_id),
                    "rejection_codes": ",".join(sorted(rejection_codes)),
                    "effective_snapshot_id": previous or "",
                },
            )
            return self._rollback_report(
                status="ROLLBACK_REJECTED",
                target_snapshot_id=str(target_snapshot_id),
                previous=previous,
                effective=previous,
                rejection_codes=tuple(sorted(rejection_codes)),
                read_context=read_context,
                audit_event_types=(
                    "SHADOW_ROLLBACK_REQUESTED",
                    "SHADOW_ROLLBACK_REJECTED",
                ),
            )

        self._registry.select(str(target_snapshot_id))
        effective = self._registry.active_snapshot_id
        self._emit(
            "SHADOW_ROLLBACK_COMPLETED",
            at=at,
            actor=actor,
            actor_role="shadow-operator",
            payload={
                "target_snapshot_id": str(target_snapshot_id),
                "previous_snapshot_id": previous or "",
                "effective_snapshot_id": effective or "",
            },
        )
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
        )

    # -- 内部 -------------------------------------------------------------

    def _rollback_rejections(
        self, *, target_snapshot_id: str, read_context: ShadowReadContext
    ) -> list[str]:
        rejections: list[str] = []

        if not self._reader.enabled:
            rejections.append("ROLLBACK_READER_DISABLED")
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
            verification = verify_runtime_activation_authorization(
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
            )
            if not verification.is_consumable:
                rejections.append("ROLLBACK_AUTHORIZATION_NOT_CONSUMABLE")
            existing_owner = read_context.existing_owner_for(binding.source_identity_key)
            if (
                existing_owner is not None
                and existing_owner != binding.candidate_canonical_id
            ):
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
