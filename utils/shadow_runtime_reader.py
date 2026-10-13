"""
影子运行期读取器（**隔离原型**，SHADOW ONLY）
============================================
本模块实现一个**只读**的影子运行期读取器：它回答

    「**如果**这个影子激活快照存在，运行期解析会观察到什么？」

而**绝不**回答「应用这个映射」。它不激活任何运行期映射、不连接 ``refresh()``、
不写任何文件、不创建绑定、不改写任何 Canonical ID。

关键安全性质
------------
1. **frozen dataclass 不等于可信**。手工构造或反序列化出来的快照**必须**通过
   内容指纹独立复算，否则一律拒绝（``SNAPSHOT_FINGERPRINT_MISMATCH``）。
2. **读时再验证**。快照创建时的授权验证**不足以**支撑无限期使用：每次读取都
   用**当前**受控时钟、**当前**撤销证据、**当前**基线重新验证该绑定背后的
   **全部**授权；任一不可消费即拒绝。
3. **只接受观测，不接受裸源 ID**。作用域源身份必须由 ``SourceObservation``
   实际推导得出。
4. **所有权冲突绝不静默降级**。同一源身份键指向不同 Canonical 目标时返回
   ``REJECTED_SHADOW``，绝不退化为 ``NOT_FOUND``。
5. **不缓存权威**。缓存键只用于可观测性；每次读取都执行完整校验。
6. **停用即刻生效**。紧急停用后所有读取（含此前取得的句柄）一律不可消费。

状态语义（与 2-F 保持一致）
---------------------------
``RESOLVED_SHADOW``
    全部校验通过，且恰好存在**一个**兼容绑定。
``NOT_FOUND``
    选中的有效快照中**没有**匹配该有效观测的绑定。
``REJECTED_SHADOW``
    输入、快照、授权、身份、所有权或治理校验失败。
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from utils.mapping_governance import MappingProposal
from utils.runtime_activation_authorization import (
    ActivationPrototypeError,
    ActivationVerificationContext,
    RuntimeActivationAuthorization,
    ShadowActivationBinding,
    ShadowActivationSnapshot,
    ShadowResolutionStatus,
    ShadowRuntimeResolutionResult,
    verify_runtime_activation_authorization,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
    RuntimeMappingEligibilityDecision,
)
from utils.runtime_mapping_policy import RUNTIME_MAPPING_POLICY_VERSION
from utils.source_identity import (
    ScopedSourceIdentityKey,
    SourceObservation,
    canonical_json,
    fingerprint,
)

#: 读取器版本。
SHADOW_READER_VERSION = "1.0.0"

#: 原型标记。
PROTOTYPE_ONLY = True

#: 生产运行期读取器：**未启用**。
PRODUCTION_RUNTIME_READER = "NOT_ENABLED"

#: 生产运行期激活：**未启用**。
PRODUCTION_ACTIVATION = "NOT_ENABLED"


# =============================================================================
# 拒绝码（声明式）
# =============================================================================

#: 读取器拒绝码：``(code, description)``。
READER_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("READER_DISABLED", "影子读取器已被紧急停用。"),
    ("CLOCK_UNAVAILABLE", "未提供可解析的受控时钟。"),
    ("SNAPSHOT_NOT_PROVIDED", "未提供快照，且无法通过快照 ID 解析。"),
    ("SNAPSHOT_NOT_REGISTERED", "快照 ID 未在影子注册表中登记。"),
    ("SNAPSHOT_MALFORMED", "快照类型错误或缺少必要身份。"),
    ("SNAPSHOT_FINGERPRINT_MISMATCH", "快照内容指纹无法独立复算或与记录不一致。"),
    ("SNAPSHOT_NOT_CONSUMABLE", "快照状态不可消费。"),
    ("DATASET_IDENTITY_MISMATCH", "快照数据集身份与当前运行期上下文不一致。"),
    ("BASELINE_STALE", "快照绑定的 Canonical 基线已变化。"),
    ("POLICY_VERSION_MISMATCH", "策略版本不一致。"),
    ("EVALUATOR_VERSION_MISMATCH", "资格评估器版本不一致。"),
    ("OBSERVATION_MALFORMED", "观测缺失或类型错误。"),
    ("OBSERVATION_IDENTITY_UNAVAILABLE", "无法由观测推导出作用域源身份。"),
    ("SNAPSHOT_AUTHORIZATION_MISSING", "绑定引用的激活授权没有可复核证据。"),
    ("AUTHORIZATION_NOT_CONSUMABLE", "绑定背后的授权在读取时已不可消费。"),
    ("OWNERSHIP_CONFLICT", "同一源身份键存在不兼容的 Canonical 所有权声明。"),
    ("CANONICAL_OWNER_UNKNOWN", "绑定目标不是当前已知的 Canonical 所有者。"),
)

#: 拒绝码集合。
READER_REJECTION_CODE_SET: frozenset[str] = frozenset(
    code for code, _ in READER_REJECTION_CODES
)


class ShadowReaderError(ActivationPrototypeError):
    """影子读取器错误。"""


# =============================================================================
# 输入契约
# =============================================================================

@dataclass(frozen=True)
class ShadowAuthorizationEvidence:
    """
    一条激活授权**及其可复核的治理证据**（只读）。

    读时再验证需要重新执行 Ed25519 验证与全部绑定校验，因此必须同时提供
    资格判定与提案。
    """

    authorization: RuntimeActivationAuthorization
    decision: RuntimeMappingEligibilityDecision
    proposal: MappingProposal

    def as_dict(self) -> dict:
        return {
            "activation_id": self.authorization.activation_id,
            "decision_id": self.decision.decision_id,
            "proposal_id": self.proposal.proposal_id,
        }


@dataclass(frozen=True)
class ShadowReadContext:
    """
    一次影子读取所需的**全部**只读运行期事实（不可变；无写入能力）。

    ``current_time`` 是**显式提供**的受控合成时钟；绝不使用工件自述时间，
    也绝不回退到不受控的系统时钟。
    """

    dataset_identity: str
    current_baseline_fingerprint: str
    current_time: str
    trusted_public_key_path: str | None = None
    policy_version: str = RUNTIME_MAPPING_POLICY_VERSION
    evaluator_version: str = RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
    #: 当前撤销登记：``(activation_id, revocation_reference)``。
    revocation_registry: tuple[tuple[str, str], ...] = ()
    #: 当前已知的 Canonical ID 集合（所有权兼容性检查）。
    known_canonical_ids: tuple[str, ...] = ()
    #: 当前已知的源身份绑定：``(source_identity_key, canonical_id)``。
    known_source_bindings: tuple[tuple[str, str], ...] = ()
    #: 可复核的治理证据集合。
    evidence: tuple[ShadowAuthorizationEvidence, ...] = ()

    def evidence_for(self, activation_id: str) -> ShadowAuthorizationEvidence | None:
        for record in self.evidence:
            if record.authorization.activation_id == activation_id:
                return record
        return None

    def existing_owner_for(self, source_identity_key: str) -> str | None:
        for key, canonical_id in self.known_source_bindings:
            if key == source_identity_key:
                return str(canonical_id)
        return None

    def canonical_id_set(self) -> frozenset[str]:
        return frozenset(str(value) for value in self.known_canonical_ids)

    def as_dict(self) -> dict:
        return {
            "dataset_identity": self.dataset_identity,
            "current_baseline_fingerprint": self.current_baseline_fingerprint,
            "current_time": self.current_time,
            "trust_anchor_supplied": self.trusted_public_key_path is not None,
            "policy_version": self.policy_version,
            "evaluator_version": self.evaluator_version,
            "revocation_registry": [list(pair) for pair in self.revocation_registry],
            "known_canonical_ids": sorted(self.canonical_id_set()),
            "known_source_bindings": [list(pair) for pair in self.known_source_bindings],
            "evidence": [record.as_dict() for record in self.evidence],
        }


# =============================================================================
# 输出契约（向后兼容的增强型只读结果）
# =============================================================================

@dataclass(frozen=True)
class ShadowReadResult(ShadowRuntimeResolutionResult):
    """
    影子读取结果。

    继承 2-F 的 ``ShadowRuntimeResolutionResult``（字段与语义完全保持），
    并**追加**只读诊断字段，因此对既有调用方向后兼容。
    """

    rejection_codes: tuple[str, ...] = ()
    authorization_ids: tuple[str, ...] = ()
    snapshot_fingerprint: str = ""
    cache_key: str = ""
    reader_generation: int = 0
    checked_at: str = ""

    @property
    def is_shadow_only(self) -> bool:
        return True

    @property
    def is_production_authorization(self) -> bool:
        return False

    def as_dict(self) -> dict:
        payload = {
            "source_identity_key": self.source_identity_key,
            "candidate_canonical_id": self.candidate_canonical_id,
            "resolution_status": self.resolution_status,
            "snapshot_id": self.snapshot_id,
            "decision_fingerprint": self.decision_fingerprint,
            "rejection_codes": list(self.rejection_codes),
            "authorization_ids": list(self.authorization_ids),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "cache_key": self.cache_key,
            "reader_generation": self.reader_generation,
            "checked_at": self.checked_at,
            "prototype_only": True,
            "shadow_only": True,
            "is_production_authority": False,
        }
        return payload

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# =============================================================================
# 影子运行期读取器
# =============================================================================

class ShadowRuntimeReader:
    """
    **只读**影子运行期读取器。

    - 绝不修改观测、快照或任何外部状态。
    - 绝不写文件、绝不创建绑定、绝不改写 Canonical ID。
    - 停用后立即不可消费；句柄以 ``reader_generation`` 为准判定是否仍然有效。
    """

    def __init__(self, *, registry: Any = None, reader_id: str = "shadow-reader") -> None:
        self._registry = registry
        self._reader_id = str(reader_id)
        self._enabled = True
        self._generation = 0

    # -- 只读状态 ---------------------------------------------------------

    @property
    def reader_id(self) -> str:
        return self._reader_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def generation(self) -> int:
        return self._generation

    def status(self) -> dict:
        return {
            "reader_id": self._reader_id,
            "shadow_reader_version": SHADOW_READER_VERSION,
            "prototype_only": True,
            "enabled": self._enabled,
            "generation": self._generation,
            "registry_attached": self._registry is not None,
            "production_runtime_reader": PRODUCTION_RUNTIME_READER,
            "production_activation": PRODUCTION_ACTIVATION,
            "writes_files": False,
            "creates_bindings": False,
            "modifies_canonical_records": False,
            "consumed_by_production_runtime": False,
        }

    def is_result_current(self, result: ShadowReadResult) -> bool:
        """
        句柄有效性策略（文档化）：只有携带**当前代际**且读取器仍启用的结果才可消费。

        紧急停用会递增代际，因此此前取得的任何结果都立即失效。
        """
        return self._enabled and result.reader_generation == self._generation

    # -- 紧急停用 ---------------------------------------------------------

    def disable(
        self,
        *,
        at: str,
        actor: str = "shadow-operator",
        reason: str = "emergency disable",
    ) -> dict:
        """
        影子紧急停用（确定性；**不需要**任何激活授权或所有者签名）。

        fail-closed：先把状态置为停用，再计算报告 —— 即使报告计算失败，
        有效读取状态也已经是「不可消费」。绝不部分启用。
        """
        previous_generation = self._generation
        self._enabled = False
        self._generation = previous_generation + 1
        report = {
            "kind": "SHADOW_RUNTIME_DISABLE_RESULT",
            "prototype_only": True,
            "reader_id": self._reader_id,
            "disabled": True,
            "enabled": self._enabled,
            "previous_generation": previous_generation,
            "reader_generation": self._generation,
            "at": str(at),
            "actor": str(actor),
            "actor_role": "shadow-operator",
            "reason": str(reason),
            "handles_invalidated": True,
            "history_preserved": True,
            "canonical_writes": 0,
            "dependent_store_writes": 0,
            "requires_activation_authorization": False,
            "requires_owner_signing_key": False,
            "partial_enablement": False,
        }
        report["fingerprint"] = fingerprint(report)
        return report

    def re_enable(self, *, at: str, reason: str = "") -> dict:
        """
        影子重新启用。

        **必须**由调用方在完成新的授权流程后显式调用；本方法本身不签发任何授权，
        也不构成生产激活。
        """
        previous_generation = self._generation
        self._enabled = True
        self._generation = previous_generation + 1
        report = {
            "kind": "SHADOW_RUNTIME_ENABLE_RESULT",
            "prototype_only": True,
            "reader_id": self._reader_id,
            "enabled": True,
            "previous_generation": previous_generation,
            "reader_generation": self._generation,
            "at": str(at),
            "reason": str(reason),
            "production_activation": PRODUCTION_ACTIVATION,
            "grants_production_authorization": False,
        }
        report["fingerprint"] = fingerprint(report)
        return report

    # -- 缓存键（仅用于可观测性；绝不用于跳过校验） --------------------------

    def cache_key(
        self,
        *,
        snapshot: ShadowActivationSnapshot | None,
        read_context: ShadowReadContext,
        source_identity_key: str = "",
    ) -> str:
        """
        缓存键：绑定快照指纹、数据集、基线、策略 / 评估器版本、授权身份、
        相关撤销上下文与读取器代际。

        刻意**只**作为身份标识：读取器每次都执行完整校验，缓存绝不构成权威。
        """
        relevant_revocations: list[list[str]] = []
        if snapshot is not None:
            for binding in snapshot.bindings:
                if source_identity_key and binding.source_identity_key != source_identity_key:
                    continue
                for activation_id, reference in read_context.revocation_registry:
                    if activation_id == binding.activation_id:
                        relevant_revocations.append([activation_id, reference])
        return fingerprint(
            {
                "kind": "SHADOW_READ_CACHE_KEY",
                "snapshot_fingerprint": (
                    snapshot.snapshot_fingerprint if snapshot is not None else ""
                ),
                "dataset_identity": read_context.dataset_identity,
                "baseline_fingerprint": read_context.current_baseline_fingerprint,
                "policy_version": read_context.policy_version,
                "evaluator_version": read_context.evaluator_version,
                "source_identity_key": source_identity_key,
                "authorization_ids": sorted(
                    binding.activation_id for binding in snapshot.bindings
                )
                if snapshot is not None
                else [],
                "relevant_revocations": sorted(relevant_revocations),
                "reader_generation": self._generation,
                "reader_enabled": self._enabled,
            }
        )

    # -- 读取 -------------------------------------------------------------

    def read(
        self,
        *,
        observation: SourceObservation | None,
        read_context: ShadowReadContext,
        snapshot: ShadowActivationSnapshot | None = None,
        snapshot_id: str | None = None,
    ) -> ShadowReadResult:
        """
        **只读**影子读取。校验顺序（确定性）：

        1. 读取器状态与受控时钟；
        2. 快照（存在性、类型、**内容指纹独立复算**、注册表成员、状态）；
        3. 快照与当前上下文的兼容性（数据集、基线、策略 / 评估器版本）；
        4. 观测（类型 + 作用域源身份推导）；
        5. 该源身份的绑定（唯一性 / 所有权冲突）；
        6. 与既有 Canonical 所有权的兼容性；
        7. **读时再验证**绑定背后的全部授权（含撤销与有效期）；
        8. 返回确定性结果。
        """
        if not isinstance(read_context, ShadowReadContext):
            raise ShadowReaderError("read_context must be a ShadowReadContext")

        rejections: list[str] = []
        resolved_snapshot = snapshot

        # 1. 读取器状态与时钟
        if not self._enabled:
            rejections.append("READER_DISABLED")
        if _parse_timestamp(read_context.current_time) is None:
            rejections.append("CLOCK_UNAVAILABLE")

        # 2. 快照
        if resolved_snapshot is None:
            if snapshot_id is None or self._registry is None:
                rejections.append("SNAPSHOT_NOT_PROVIDED")
            else:
                resolved_snapshot = self._registry.get(str(snapshot_id))
                if resolved_snapshot is None:
                    rejections.append("SNAPSHOT_NOT_REGISTERED")

        if resolved_snapshot is not None:
            if not isinstance(resolved_snapshot, ShadowActivationSnapshot):
                rejections.append("SNAPSHOT_MALFORMED")
                resolved_snapshot = None
            else:
                if (
                    resolved_snapshot.snapshot_id
                    != resolved_snapshot.snapshot_fingerprint
                ):
                    rejections.append("SNAPSHOT_FINGERPRINT_MISMATCH")
                if (
                    resolved_snapshot.content_fingerprint()
                    != resolved_snapshot.snapshot_fingerprint
                ):
                    rejections.append("SNAPSHOT_FINGERPRINT_MISMATCH")
                if self._registry is not None:
                    registered = self._registry.fingerprint_of(
                        resolved_snapshot.snapshot_id
                    )
                    if registered != resolved_snapshot.snapshot_fingerprint:
                        rejections.append("SNAPSHOT_NOT_REGISTERED")
                if not resolved_snapshot.is_consumable:
                    rejections.append("SNAPSHOT_NOT_CONSUMABLE")
                if resolved_snapshot.dataset_identity != read_context.dataset_identity:
                    rejections.append("DATASET_IDENTITY_MISMATCH")
                if (
                    resolved_snapshot.baseline_fingerprint
                    != read_context.current_baseline_fingerprint
                ):
                    rejections.append("BASELINE_STALE")
                if resolved_snapshot.policy_version != read_context.policy_version:
                    rejections.append("POLICY_VERSION_MISMATCH")
                if resolved_snapshot.evaluator_version != read_context.evaluator_version:
                    rejections.append("EVALUATOR_VERSION_MISMATCH")

        # 4. 观测
        source_key = ""
        if not isinstance(observation, SourceObservation):
            rejections.append("OBSERVATION_MALFORMED")
        else:
            source_key = ScopedSourceIdentityKey.for_observation(observation).source_key()
            if not source_key:
                rejections.append("OBSERVATION_IDENTITY_UNAVAILABLE")

        snapshot_fingerprint = (
            resolved_snapshot.snapshot_fingerprint if resolved_snapshot is not None else ""
        )
        cache_key = self.cache_key(
            snapshot=resolved_snapshot,
            read_context=read_context,
            source_identity_key=source_key,
        )

        if rejections:
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=rejections,
            )

        # 5. 绑定
        matches = resolved_snapshot.bindings_for(source_key)
        if not matches:
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.NOT_FOUND.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=(),
            )

        targets = {binding.candidate_canonical_id for binding in matches}
        if len(targets) != 1:
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=("OWNERSHIP_CONFLICT",),
                authorization_ids=tuple(b.activation_id for b in matches),
            )

        binding = resolved_snapshot.resolve(source_key)
        if binding is None:  # 理论上不可达；保守 fail closed
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=("OWNERSHIP_CONFLICT",),
            )

        # 6. 既有 Canonical 所有权兼容性
        owner_conflicts: list[str] = []
        existing_owner = read_context.existing_owner_for(source_key)
        if existing_owner is not None and existing_owner != binding.candidate_canonical_id:
            owner_conflicts.append("OWNERSHIP_CONFLICT")
        known_ids = read_context.canonical_id_set()
        if known_ids and binding.candidate_canonical_id not in known_ids:
            owner_conflicts.append("CANONICAL_OWNER_UNKNOWN")
        if owner_conflicts:
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=owner_conflicts,
                authorization_ids=tuple(b.activation_id for b in matches),
            )

        # 7. 读时再验证：绑定背后的**全部**授权都必须仍然可消费
        revalidation_codes: list[str] = []
        for candidate in matches:
            evidence = read_context.evidence_for(candidate.activation_id)
            if evidence is None:
                revalidation_codes.append("SNAPSHOT_AUTHORIZATION_MISSING")
                continue
            if candidate.activation_id not in resolved_snapshot.activation_ids:
                revalidation_codes.append("SNAPSHOT_MALFORMED")
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
                revalidation_codes.append("AUTHORIZATION_NOT_CONSUMABLE")
                revalidation_codes.extend(verification.rejection_codes)
            # 绑定必须与再验证后的授权一致。
            if (
                evidence.authorization.candidate_canonical_id
                != candidate.candidate_canonical_id
                or evidence.authorization.scoped_source_identity_key != source_key
            ):
                revalidation_codes.append("AUTHORIZATION_NOT_CONSUMABLE")

        if revalidation_codes:
            return self._result(
                source_key=source_key,
                canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint=snapshot_fingerprint,
                cache_key=cache_key,
                read_context=read_context,
                rejection_codes=revalidation_codes,
                authorization_ids=tuple(b.activation_id for b in matches),
            )

        # 8. 解析成功
        return self._result(
            source_key=source_key,
            canonical_id=binding.candidate_canonical_id,
            status=ShadowResolutionStatus.RESOLVED_SHADOW.value,
            snapshot_fingerprint=snapshot_fingerprint,
            cache_key=cache_key,
            read_context=read_context,
            rejection_codes=(),
            authorization_ids=tuple(b.activation_id for b in matches),
            decision_fingerprint=binding.eligibility_decision_fingerprint,
        )

    # -- 内部 -------------------------------------------------------------

    def _result(
        self,
        *,
        source_key: str,
        canonical_id: str,
        status: str,
        snapshot_fingerprint: str,
        cache_key: str,
        read_context: ShadowReadContext,
        rejection_codes: Sequence[str] = (),
        authorization_ids: Sequence[str] = (),
        decision_fingerprint: str = "",
    ) -> ShadowReadResult:
        return ShadowReadResult(
            source_identity_key=source_key,
            candidate_canonical_id=canonical_id,
            resolution_status=status,
            snapshot_id=snapshot_fingerprint,
            decision_fingerprint=decision_fingerprint,
            rejection_codes=tuple(sorted({str(code) for code in rejection_codes})),
            authorization_ids=tuple(sorted({str(value) for value in authorization_ids})),
            snapshot_fingerprint=snapshot_fingerprint,
            cache_key=cache_key,
            reader_generation=self._generation,
            checked_at=read_context.current_time,
        )


# =============================================================================
# 便捷入口
# =============================================================================

def read_shadow_runtime_mapping(
    *,
    observation: SourceObservation | None,
    snapshot: ShadowActivationSnapshot | None,
    read_context: ShadowReadContext,
    reader: ShadowRuntimeReader | None = None,
) -> ShadowReadResult:
    """一次性影子读取（只读；不写任何状态）。"""
    active_reader = reader if reader is not None else ShadowRuntimeReader()
    return active_reader.read(
        observation=observation, read_context=read_context, snapshot=snapshot
    )


def shadow_reader_status() -> dict:
    """读取器与激活边界的只读状态视图。"""
    return {
        "shadow_reader_version": SHADOW_READER_VERSION,
        "prototype_only": PROTOTYPE_ONLY,
        "production_runtime_reader": PRODUCTION_RUNTIME_READER,
        "production_activation": PRODUCTION_ACTIVATION,
        "rejection_code_count": len(READER_REJECTION_CODE_SET),
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "writes_files": False,
        "creates_bindings": False,
        "modifies_canonical_records": False,
        "consumed_by_production_runtime": False,
    }


def _assert_reader_is_coherent() -> None:
    if not PROTOTYPE_ONLY:
        raise AssertionError("shadow reader must declare itself prototype-only")
    if PRODUCTION_RUNTIME_READER != "NOT_ENABLED":
        raise AssertionError("the production runtime reader must remain NOT_ENABLED")
    if PRODUCTION_ACTIVATION != "NOT_ENABLED":
        raise AssertionError("runtime activation must remain NOT_ENABLED")
    codes = [code for code, _ in READER_REJECTION_CODES]
    if len(set(codes)) != len(codes):
        raise AssertionError("shadow reader rejection codes must be unique")
    result_fields = {field.name for field in fields(ShadowReadResult)}
    parent_fields = {field.name for field in fields(ShadowRuntimeResolutionResult)}
    if not parent_fields <= result_fields:
        raise AssertionError(
            "ShadowReadResult must remain backward compatible with "
            "ShadowRuntimeResolutionResult"
        )


_assert_reader_is_coherent()
