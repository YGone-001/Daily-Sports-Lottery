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
7. **选择先于消费**。注册表驱动的读取只消费**显式选择**的活动影子快照；
   登记、传入快照对象或传入快照 ID 都不得覆盖活动指针。独立（未附加注册表）
   读取仅用于历史快照检视，不得被表述为当前激活权威。
8. **再启用需要新证据**。停用后的恢复必须重新验证一份仍可消费的激活授权，
   并且该授权必须已被当前选中的快照引用；裸时间戳 / 理由字符串不足以恢复。

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
from threading import RLock
from typing import Any, Mapping, Sequence

from utils.mapping_governance import ACTIVE_STATES, MappingProposal
from utils.authoritative_governance import (
    AuthoritativeGovernanceView,
    DEFAULT_GOVERNANCE_MAX_AGE_SECONDS,
    verify_authoritative_governance_view,
)
from utils.observation_authority import (
    ObservationAttestationError,
    verify_observation_attestation,
)
from utils.recovery_authority import (
    RecoveryEvidenceError,
    recovery_state_fingerprint,
    verify_recovery_evidence,
)
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError
from utils.runtime_activation_authorization import (
    ActivationPrototypeError,
    ActivationVerificationContext,
    StrictActivationVerifier,
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
#:
#: ``1.1.0``：注册表驱动的读取强制显式选择权威、完整所有权冲突判定、
#: 停用后的再启用必须携带已校验的激活证据、句柄有效性显式限定为代际校验。
#: ``1.2.0``：新增严格模式（权威治理视图、观测来源证明、恢复证据前置）。
#: ``1.3.0``：严格再启用要求恢复证据与**当前**审计存储身份、链尖、条目数、
#: 完成事件、选中快照与有效指针对齐；空历史/旧状态一律拒绝。
SHADOW_READER_VERSION = "1.3.0"

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
    ("SNAPSHOT_NOT_SELECTED", "快照已登记但未被显式选择为活动影子快照。"),
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
    ("AUTHORITATIVE_GOVERNANCE_VIEW_MISSING", "严格模式要求已签名的权威治理视图。"),
    ("AUTHORITATIVE_GOVERNANCE_VIEW_INVALID", "权威治理视图无法验证或与当前上下文不一致。"),
    ("GOVERNANCE_STATE_NOT_ACTIVE", "权威治理视图显示该提案已不在活动状态。"),
    ("GOVERNANCE_CONTENT_MISMATCH", "权威治理视图的提案内容指纹与授权不一致。"),
    ("OBSERVATION_PROVENANCE_MISSING", "严格模式要求源观测来源证明。"),
    ("OBSERVATION_PROVENANCE_INVALID", "源观测来源证明无法验证。"),
)

#: 拒绝码集合。
READER_REJECTION_CODE_SET: frozenset[str] = frozenset(
    code for code, _ in READER_REJECTION_CODES
)


class ShadowReaderError(ActivationPrototypeError):
    """影子读取器错误。"""


#: 再启用拒绝码（停用后的恢复必须重新验证证据）。
REENABLE_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("RE_ENABLE_READER_NOT_DISABLED", "读取器当前未停用；再启用没有意义。"),
    ("RE_ENABLE_REGISTRY_REQUIRED", "未附加注册表，无法确定被选中的目标快照。"),
    ("RE_ENABLE_NO_SELECTED_SNAPSHOT", "没有显式选择的影子快照，拒绝再启用。"),
    ("RE_ENABLE_SNAPSHOT_FINGERPRINT_MISMATCH", "被选中快照的指纹不可复算。"),
    ("RE_ENABLE_SNAPSHOT_NOT_CONSUMABLE", "被选中快照状态不可消费。"),
    ("RE_ENABLE_DATASET_MISMATCH", "被选中快照的数据集身份与当前上下文不一致。"),
    ("RE_ENABLE_BASELINE_STALE", "被选中快照的 Canonical 基线已变化。"),
    ("RE_ENABLE_POLICY_VERSION_MISMATCH", "被选中快照的策略版本不一致。"),
    ("RE_ENABLE_EVALUATOR_VERSION_MISMATCH", "被选中快照的评估器版本不一致。"),
    ("RE_ENABLE_ACTIVATION_NOT_SELECTED", "所提供授权未被当前选中的快照引用。"),
    ("RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE", "所提供授权在当前条件下不可消费。"),
    ("RE_ENABLE_CLOCK_UNAVAILABLE", "未提供可解析的受控时钟。"),
    ("RE_ENABLE_EVIDENCE_MALFORMED", "再启用证据缺失或类型错误。"),
    ("RE_ENABLE_RECOVERY_STATE_SOURCE_MISSING", "严格恢复缺少受信任的当前审计状态源。"),
    ("RE_ENABLE_RECOVERY_STATE_INVALID", "当前审计状态无法独立读取或校验。"),
    ("RE_ENABLE_RECOVERY_STATE_MISMATCH", "恢复证据与当前审计/快照状态不一致。"),
    ("RE_ENABLE_RECOVERY_EMPTY_HISTORY", "空审计历史不得证明已恢复选中激活。"),
    ("RE_ENABLE_RECOVERY_EVIDENCE_MISSING", "严格模式要求已验证的审计恢复证据。"),
    ("RE_ENABLE_RECOVERY_EVIDENCE_INVALID", "恢复证据无法验证、已过期或未消解差异。"),
    ("RE_ENABLE_AUTHORITATIVE_VIEW_MISSING", "严格模式要求已签名的权威治理视图。"),
    ("RE_ENABLE_AUTHORITATIVE_VIEW_INVALID", "权威治理视图无法验证或不匹配。"),
    ("RE_ENABLE_GOVERNANCE_STATE_NOT_ACTIVE", "权威视图显示该提案已不活动。"),
    ("RE_ENABLE_GOVERNANCE_CONTENT_MISMATCH", "权威视图的提案内容指纹不匹配。"),
)

#: 再启用拒绝码集合。
REENABLE_REJECTION_CODE_SET: frozenset[str] = frozenset(
    code for code, _ in REENABLE_REJECTION_CODES
)


class ShadowReenablementError(ShadowReaderError):
    """停用后的再启用缺少可验证的授权证据。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


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
    #: 严格模式：已签名、未过期的权威治理视图。
    authoritative_view: AuthoritativeGovernanceView | None = None
    authoritative_view_trusted_public_key_path: str | None = None
    authoritative_view_max_age_seconds: float = DEFAULT_GOVERNANCE_MAX_AGE_SECONDS
    #: 严格模式：源观测来源证明的可信公钥与新鲜度。
    observation_trusted_public_key_path: str | None = None
    observation_max_age_seconds: float = 3600.0
    #: 严格模式：恢复证据的可信公钥与新鲜度。
    recovery_trusted_public_key_path: str | None = None
    recovery_max_age_seconds: float = 3600.0
    #: Strict shadow time evidence; trust roots and policy belong to the reader.
    trusted_time_evidence: SignedTrustedArtifact | Mapping[str, Any] | None = None
    #: 原始签名生命周期证据；派生的只读结果不能替代它。
    lifecycle_evidence: Any = None
    #: 独立撤销权威的原始完整状态与会话检查点。
    revocation_evidence: Any = None

    def evidence_for(self, activation_id: str) -> ShadowAuthorizationEvidence | None:
        for record in self.evidence:
            if record.authorization.activation_id == activation_id:
                return record
        return None

    def existing_owners_for(self, source_identity_key: str) -> tuple[str, ...]:
        """
        收集某源身份键关联的**全部** Canonical 所有者（去重、排序）。

        绝不返回「第一个匹配」：调用方必须按集合判定冲突，结果与输入顺序无关。
        空元组表示当前没有记录到任何所有者（按当前影子契约视为缺少证据）。
        """
        owners = {
            str(canonical_id)
            for key, canonical_id in self.known_source_bindings
            if key == source_identity_key
        }
        return tuple(sorted(owners))

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
            "authoritative_view_supplied": self.authoritative_view is not None,
            "authoritative_view_fingerprint": (
                self.authoritative_view.fingerprint()
                if self.authoritative_view is not None
                else ""
            ),
            "authoritative_view_trust_anchor_supplied": (
                self.authoritative_view_trusted_public_key_path is not None
            ),
            "observation_trust_anchor_supplied": (
                self.observation_trusted_public_key_path is not None
            ),
            "recovery_trust_anchor_supplied": (
                self.recovery_trusted_public_key_path is not None
            ),
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
    #: 快照权威来源：``REGISTRY_SELECTED``（注册表驱动的活动消费）或
    #: ``STANDALONE_INSPECTION``（独立历史检视；**不**主张当前激活权威）。
    snapshot_authority: str = "STANDALONE_INSPECTION"

    @property
    def is_shadow_only(self) -> bool:
        return True

    @property
    def is_production_authorization(self) -> bool:
        return False

    @property
    def is_standalone_inspection(self) -> bool:
        """独立检视结果绝不能被当作注册表活动快照的消费权威。"""
        return self.snapshot_authority == "STANDALONE_INSPECTION"

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
            "snapshot_authority": self.snapshot_authority,
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

    def __init__(
        self,
        *,
        registry: Any = None,
        reader_id: str = "shadow-reader",
        audit_store: Any = None,
        require_authoritative_governance: bool = False,
        require_observation_attestation: bool = False,
        require_recovery_evidence_for_reenable: bool = False,
        require_trusted_time: bool = False,
        strict_activation_verifier: StrictActivationVerifier | None = None,
        require_authenticated_approval: bool = False,
        require_verified_supersession: bool = False,
        supersession_verifier: Any = None,
        require_independent_revocation: bool = False,
        revocation_verifier: Any = None,
    ) -> None:
        if require_independent_revocation:
            from utils.activation_revocation_authority import IndependentRevocationVerifier
            from utils.shadow_activation_simulation import ShadowSnapshotRegistry
            if (not isinstance(registry, ShadowSnapshotRegistry) or not require_authoritative_governance
                or not require_authenticated_approval or not require_trusted_time
                or not isinstance(revocation_verifier, IndependentRevocationVerifier)
                or revocation_verifier.strict_activation_verifier is not strict_activation_verifier):
                raise ValueError("independent revocation requires registry, governance, approval and the same strict time verifier")
        elif revocation_verifier is not None:
            raise ValueError("revocation verifier requires explicit independent revocation mode")
        self._require_independent_revocation = bool(require_independent_revocation)
        self._revocation_verifier = revocation_verifier
        self._revocation_generation = None
        self._revocation_lock = RLock()
        if require_verified_supersession:
            from utils.activation_supersession import ActivationSupersessionVerifier
            from utils.shadow_activation_simulation import ShadowSnapshotRegistry
            if (not isinstance(registry, ShadowSnapshotRegistry) or not require_authoritative_governance
                or not require_authenticated_approval or not require_trusted_time
                or not isinstance(supersession_verifier, ActivationSupersessionVerifier)
                or supersession_verifier.strict_activation_verifier is not strict_activation_verifier):
                raise ValueError("strict supersession requires registry, governance, approval, time and the same verifier")
        elif supersession_verifier is not None:
            raise ValueError("supersession verifier requires explicit strict supersession mode")
        self._require_verified_supersession = bool(require_verified_supersession)
        self._supersession_verifier = supersession_verifier
        if require_independent_revocation and require_verified_supersession and (
            revocation_verifier._keys[3] != supersession_verifier._keys[0]
            or revocation_verifier._keys[2] != supersession_verifier._keys[2]
            or revocation_verifier.dataset_identity != supersession_verifier.dataset_identity
        ):
            raise ValueError("revocation and lifecycle must agree on dataset, activation and lifecycle trust roots")
        if require_authenticated_approval and (
            registry is None or not require_trusted_time or not require_authoritative_governance
            or not isinstance(strict_activation_verifier, StrictActivationVerifier)
            or not strict_activation_verifier.require_authenticated_approval
        ):
            raise ValueError("authenticated approval reader requires registry, governance and strict trusted time")
        if (isinstance(strict_activation_verifier, StrictActivationVerifier)
            and strict_activation_verifier.require_authenticated_approval and not require_authenticated_approval):
            raise ValueError("approval verifier requires explicit authenticated approval reader mode")
        self._registry = registry
        self._require_authenticated_approval = bool(require_authenticated_approval)
        self._require_trusted_time = bool(require_trusted_time)
        self._strict_activation_verifier = strict_activation_verifier
        self._reader_id = str(reader_id)
        self._enabled = True
        self._generation = 0
        #: 严格恢复所需的**只读**当前审计状态源（绝不写入）。
        self._audit_store = audit_store
        self._require_authoritative_governance = bool(
            require_authoritative_governance
        )
        self._require_observation_attestation = bool(
            require_observation_attestation
        )
        self._require_recovery_evidence_for_reenable = bool(
            require_recovery_evidence_for_reenable or require_trusted_time
        )

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
        registry_backed = self._registry is not None
        return {
            "reader_id": self._reader_id,
            "require_trusted_time": self._require_trusted_time,
            "require_authenticated_approval": self._require_authenticated_approval,
            "require_verified_supersession": self._require_verified_supersession,
            "require_independent_revocation": self._require_independent_revocation,
            "shadow_reader_version": SHADOW_READER_VERSION,
            "prototype_only": True,
            "enabled": self._enabled,
            "generation": self._generation,
            "registry_attached": registry_backed,
            "snapshot_authority": (
                "REGISTRY_SELECTED" if registry_backed else "STANDALONE_INSPECTION"
            ),
            "requires_explicit_selection": registry_backed,
            "re_enable_requires_verified_evidence": True,
            "require_authoritative_governance": (
                self._require_authoritative_governance
            ),
            "require_observation_attestation": (
                self._require_observation_attestation
            ),
            "require_recovery_evidence_for_reenable": (
                self._require_recovery_evidence_for_reenable
            ),
            "recovery_audit_store_attached": self._audit_store is not None,
            "production_runtime_reader": PRODUCTION_RUNTIME_READER,
            "production_activation": PRODUCTION_ACTIVATION,
            "writes_files": False,
            "creates_bindings": False,
            "modifies_canonical_records": False,
            "consumed_by_production_runtime": False,
        }

    def is_generation_current(self, result: ShadowReadResult) -> bool:
        """
        **仅代际**有效性检查（文档化限制）。

        只回答「该结果是否由**当前启用且当前代际**的读取器产生」，
        **不**校验授权有效期、撤销、基线、快照选择或所有权冲突。

        因此它**不是**当前授权有效性的证明：治理状态可能已经变化，而旧结果
        仍然「代际有效」。需要当前授权结论时必须重新执行 ``read()``。
        独立撤销模式额外使已观察到的新撤销状态失效旧句柄；仍不证明全局当前性。
        """
        if self._require_independent_revocation and (
            not self._revocation_verifier.continuity_healthy
            or self._revocation_generation != self._revocation_verifier.generation
        ):
            return False
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

    def re_enable(
        self,
        *,
        at: str,
        read_context: ShadowReadContext,
        evidence: ShadowAuthorizationEvidence,
        recovery_evidence: SignedTrustedArtifact | Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> dict:
        """
        停用后的**受限**再启用（SHADOW ONLY）。

        不是「翻转开关」：必须同时提供当前只读上下文与一份可复核的激活证据，
        并在恢复消费前重新执行：

        1. 受控时钟可用；
        2. 注册表存在且**显式选择**了一个活动影子快照；
        3. 该快照指纹可复算、状态可消费、数据集 / 基线 / 策略 / 评估器与当前一致；
        4. 所提供授权确实被该选中快照引用且绑定一致；
        5. 该授权在当前时钟、撤销登记与信任锚下仍**可消费**。

        任一项失败即抛 ``ShadowReenablementError``，读取器保持停用，且代际不变。
        成功时代际 +1，此前取得的所有句柄立即失效。

        绝不签发任何授权、绝不构成生产激活、绝不要求新的生产签名密钥。
        """
        if self._enabled:
            raise ShadowReenablementError(
                "the shadow reader is not disabled; re-enablement is not applicable",
                rejection_codes=("RE_ENABLE_READER_NOT_DISABLED",),
            )
        try:
            read_context = self._authenticated_context(read_context)
        except TrustedArtifactError as exc:
            raise ShadowReenablementError("trusted time required for re-enablement",
                rejection_codes=exc.rejection_codes) from exc
        rejections = self._reenable_rejections(
            read_context=read_context,
            evidence=evidence,
            recovery_evidence=recovery_evidence,
        )
        if rejections:
            raise ShadowReenablementError(
                "shadow re-enablement rejected: " + ", ".join(sorted(set(rejections))),
                rejection_codes=tuple(sorted(set(rejections))),
            )

        active = self._registry.active()  # type: ignore[union-attr]
        previous_generation = self._generation
        self._enabled = True
        self._generation = previous_generation + 1
        report = {
            "kind": "SHADOW_RUNTIME_REENABLE_RESULT",
            "prototype_only": True,
            "shadow_only": True,
            "reader_id": self._reader_id,
            "enabled": True,
            "previous_generation": previous_generation,
            "reader_generation": self._generation,
            "at": str(at),
            "reason": str(reason),
            "snapshot_id": active.snapshot_id if active is not None else "",
            "activation_id": evidence.authorization.activation_id,
            "verified_at": read_context.current_time,
            "requires_activation_authorization": True,
            "requires_owner_signing_key": False,
            "production_activation": PRODUCTION_ACTIVATION,
            "grants_production_authorization": False,
            "partial_enablement": False,
            "stale_handles_invalidated": True,
            "recovery_evidence_fingerprint": (
                recovery_evidence.fingerprint()
                if isinstance(recovery_evidence, SignedTrustedArtifact)
                else ""
            ),
            "authoritative_view_fingerprint": (
                read_context.authoritative_view.fingerprint()
                if isinstance(
                    read_context.authoritative_view, AuthoritativeGovernanceView
                )
                else ""
            ),
        }
        report["fingerprint"] = fingerprint(report)
        return report

    def _reenable_rejections(
        self,
        *,
        read_context: ShadowReadContext,
        evidence: ShadowAuthorizationEvidence,
        recovery_evidence: SignedTrustedArtifact | Mapping[str, Any] | None = None,
    ) -> list[str]:
        """再启用前的完整只读校验（绝不修改状态）。"""
        rejections: list[str] = []
        if not isinstance(read_context, ShadowReadContext):
            return ["RE_ENABLE_EVIDENCE_MALFORMED"]
        if _parse_timestamp(read_context.current_time) is None:
            rejections.append("RE_ENABLE_CLOCK_UNAVAILABLE")
        if not isinstance(evidence, ShadowAuthorizationEvidence):
            rejections.append("RE_ENABLE_EVIDENCE_MALFORMED")
            return sorted(set(rejections))
        authorization = evidence.authorization
        if not isinstance(authorization, RuntimeActivationAuthorization):
            rejections.append("RE_ENABLE_EVIDENCE_MALFORMED")
            return sorted(set(rejections))

        if self._registry is None:
            rejections.append("RE_ENABLE_REGISTRY_REQUIRED")
            return sorted(set(rejections))
        active = self._registry.active()
        if active is None:
            rejections.append("RE_ENABLE_NO_SELECTED_SNAPSHOT")
            return sorted(set(rejections))

        if (
            active.snapshot_id != active.snapshot_fingerprint
            or active.content_fingerprint() != active.snapshot_fingerprint
        ):
            rejections.append("RE_ENABLE_SNAPSHOT_FINGERPRINT_MISMATCH")
        if not active.is_consumable:
            rejections.append("RE_ENABLE_SNAPSHOT_NOT_CONSUMABLE")
        if active.dataset_identity != read_context.dataset_identity:
            rejections.append("RE_ENABLE_DATASET_MISMATCH")
        if active.baseline_fingerprint != read_context.current_baseline_fingerprint:
            rejections.append("RE_ENABLE_BASELINE_STALE")
        if active.policy_version != read_context.policy_version:
            rejections.append("RE_ENABLE_POLICY_VERSION_MISMATCH")
        if active.evaluator_version != read_context.evaluator_version:
            rejections.append("RE_ENABLE_EVALUATOR_VERSION_MISMATCH")

        if self._require_independent_revocation:
            try:
                self._verify_independent_revocations(read_context, snapshot=active, authorization=authorization)
            except TrustedArtifactError as exc:
                rejections.extend(exc.rejection_codes)

        if self._require_verified_supersession:
            try:
                lifecycle = self._verify_current_lifecycle(read_context, active)
                current = lifecycle.current_for(authorization.scoped_source_identity_key)
                if (current is None or current.activation_id != authorization.activation_id
                    or current.authorization_fingerprint != authorization.fingerprint()):
                    rejections.append("LIFECYCLE_ACTIVATION_NOT_CURRENT")
            except TrustedArtifactError as exc:
                rejections.extend(exc.rejection_codes)

        matching = tuple(
            binding
            for binding in active.bindings
            if binding.activation_id == authorization.activation_id
        )
        if not matching:
            rejections.append("RE_ENABLE_ACTIVATION_NOT_SELECTED")
        else:
            for binding in matching:
                if (
                    binding.source_identity_key
                    != authorization.scoped_source_identity_key
                    or binding.candidate_canonical_id
                    != authorization.candidate_canonical_id
                ):
                    rejections.append("RE_ENABLE_ACTIVATION_NOT_SELECTED")
                if self._require_authenticated_approval and (
                    binding.eligibility_decision_fingerprint != authorization.eligibility_decision_fingerprint
                    or binding.authorized_scope != authorization.authorized_scope
                ):
                    rejections.append("RE_ENABLE_ACTIVATION_NOT_SELECTED")

        governance_view: AuthoritativeGovernanceView | None = None
        if self._require_authoritative_governance:
            candidate = read_context.authoritative_view
            if (
                not isinstance(candidate, AuthoritativeGovernanceView)
                or not read_context.authoritative_view_trusted_public_key_path
            ):
                rejections.append("RE_ENABLE_AUTHORITATIVE_VIEW_MISSING")
            else:
                try:
                    verify_authoritative_governance_view(
                        candidate,
                        trusted_public_key_path=(
                            read_context.authoritative_view_trusted_public_key_path
                        ),
                        current_time=read_context.current_time,
                        max_age_seconds=(
                            read_context.authoritative_view_max_age_seconds
                        ),
                    )
                except TrustedArtifactError:
                    rejections.append("RE_ENABLE_AUTHORITATIVE_VIEW_INVALID")
                else:
                    if (
                        candidate.dataset_identity != read_context.dataset_identity
                        or candidate.baseline_fingerprint
                        != read_context.current_baseline_fingerprint
                    ):
                        rejections.append("RE_ENABLE_AUTHORITATIVE_VIEW_INVALID")
                    else:
                        governance_view = candidate
                        proposal_id = authorization.proposal_id
                        if not candidate.is_active_proposal(proposal_id):
                            rejections.append(
                                "RE_ENABLE_GOVERNANCE_STATE_NOT_ACTIVE"
                            )
                        if (
                            candidate.content_fingerprint_of(proposal_id)
                            != authorization.proposal_content_fingerprint
                        ):
                            rejections.append(
                                "RE_ENABLE_GOVERNANCE_CONTENT_MISMATCH"
                            )

        if self._require_recovery_evidence_for_reenable:
            if self._audit_store is None:
                # 调用方断言（例如 expected_store_identity）不得替代实际审计证据。
                rejections.append("RE_ENABLE_RECOVERY_STATE_SOURCE_MISSING")
            else:
                try:
                    recovery_payload = verify_recovery_evidence(
                        recovery_evidence,
                        trusted_public_key_path=(
                            read_context.recovery_trusted_public_key_path
                        ),
                        current_time=read_context.current_time,
                        max_age_seconds=read_context.recovery_max_age_seconds,
                        expected_dataset_identity=read_context.dataset_identity,
                    )
                except RecoveryEvidenceError:
                    rejections.append(
                        "RE_ENABLE_RECOVERY_EVIDENCE_MISSING"
                        if recovery_evidence is None
                        else "RE_ENABLE_RECOVERY_EVIDENCE_INVALID"
                    )
                else:
                    try:
                        records = tuple(self._audit_store.records())
                        self._audit_store.verify()
                        store_identity = str(self._audit_store.store_identity())
                        chain_tip = str(self._audit_store.chain_tip())
                        entry_count = len(records)
                    except Exception:  # noqa: BLE001
                        rejections.append("RE_ENABLE_RECOVERY_STATE_INVALID")
                    else:
                        if not records:
                            rejections.append("RE_ENABLE_RECOVERY_EMPTY_HISTORY")
                        else:
                            completion = records[-1]
                            completion_event_id = (
                                completion.event_id
                                if completion.event_type
                                == "SHADOW_ROLLBACK_COMPLETED"
                                else ""
                            )
                            expected_state = {
                                "dataset_identity": read_context.dataset_identity,
                                "store_identity": store_identity,
                                "chain_tip": chain_tip,
                                "entry_count": entry_count,
                                "completion_event_id": completion_event_id,
                                "selected_snapshot_id": active.snapshot_id,
                                "effective_snapshot_id": active.snapshot_id,
                                "divergence": "NONE",
                            }
                            mismatch = any(
                                str(recovery_payload.get(key, "")) != str(value)
                                for key, value in expected_state.items()
                            )
                            if mismatch or str(
                                recovery_payload.get("recovery_generation", "")
                            ) != recovery_state_fingerprint(expected_state):
                                rejections.append(
                                    "RE_ENABLE_RECOVERY_STATE_MISMATCH"
                                )

        verification = self._verify_authorization(
            authorization,
            ActivationVerificationContext(
                dataset_identity=read_context.dataset_identity,
                current_baseline_fingerprint=read_context.current_baseline_fingerprint,
                current_time=read_context.current_time,
                trusted_public_key_path=read_context.trusted_public_key_path,
                eligibility_decision=evidence.decision,
                proposal=evidence.proposal,
                revocation_registry=(
                    governance_view.revocations
                    if governance_view is not None
                    else read_context.revocation_registry
                ),
                require_verified_governance=(
                    self._require_authoritative_governance
                ),
            ),
            read_context=read_context,
        )
        if not verification.is_consumable:
            rejections.append("RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE")
            rejections.extend(verification.rejection_codes)

        return sorted(set(rejections))

    # -- 缓存键（仅用于可观测性；绝不用于跳过校验） --------------------------

    def _authenticated_context(self, context: ShadowReadContext) -> ShadowReadContext:
        if not self._require_trusted_time:
            return context
        if not isinstance(context, ShadowReadContext):
            raise TrustedArtifactError("malformed strict read context",
                rejection_codes=("TRUSTED_TIME_CONTEXT_INVALID",))
        if not isinstance(self._strict_activation_verifier, StrictActivationVerifier):
            raise TrustedArtifactError("strict time verifier missing",
                rejection_codes=("TRUSTED_TIME_VERIFIER_MISSING",))
        observation = self._strict_activation_verifier.time_verifier.verify(
            context.trusted_time_evidence, dataset_identity=context.dataset_identity)
        return replace(context, current_time=observation.effective_time)

    def _verify_authorization(self, authorization, context, *, read_context):
        if self._require_trusted_time:
            context = replace(context, require_verified_governance=(
                context.require_verified_governance or self._require_authenticated_approval))
            result = self._strict_activation_verifier.verify(authorization, context,
                time_evidence=read_context.trusted_time_evidence,
                governance_view=read_context.authoritative_view)
        else:
            result = verify_runtime_activation_authorization(authorization, context)
        if self._require_independent_revocation:
            try:
                self._verify_independent_revocations(read_context, authorization=authorization)
            except TrustedArtifactError as exc:
                return replace(result, verification_status="REJECTED", verified_scope="",
                    rejection_codes=tuple(sorted(set(result.rejection_codes) | set(exc.rejection_codes))))
        return result

    def _verify_independent_revocations(self, context, *, snapshot=None, authorization=None):
        with self._revocation_lock:
            return self._check_independent_revocations(context, snapshot=snapshot, authorization=authorization)

    def _check_independent_revocations(self, context, *, snapshot=None, authorization=None):
        verified = self._revocation_verifier.verify(context.revocation_evidence, read_context=context)
        if verified.continuity_generation != self._revocation_verifier.generation:
            raise TrustedArtifactError("revocation state changed during verification",
                rejection_codes=("REVOCATION_CONCURRENT_STATE_CHANGED",))
        if self._revocation_generation != verified.continuity_generation:
            self._revocation_generation = verified.continuity_generation
            self._generation += 1
        if authorization is not None and (
            authorization.activation_id, authorization.fingerprint()
        ) not in verified.authorization_inventory:
            raise TrustedArtifactError("authorization differs from complete authenticated revocation scope",
                rejection_codes=("REVOCATION_ACTIVATION_ID_CONFLICT",))
        revoked_ids = {aid for aid, _ in verified.revoked_authorizations}
        if ((snapshot is not None and any(b.activation_id in revoked_ids for b in snapshot.bindings))
            or (authorization is not None and verified.is_revoked(authorization))):
            raise TrustedArtifactError("independently authenticated activation revocation",
                rejection_codes=("ACTIVATION_AUTHORIZATION_REVOKED",))
        return verified

    def _verify_current_lifecycle(self, context, snapshot):
        return self._supersession_verifier.verify_snapshot(context.lifecycle_evidence,
            read_context=context, snapshot=snapshot)

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
        observation_attestation: SignedTrustedArtifact | Mapping[str, Any] | None = None,
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

        try:
            read_context = self._authenticated_context(read_context)
        except TrustedArtifactError as exc:
            return self._result(source_key="", canonical_id="",
                status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                snapshot_fingerprint="", cache_key="",
                read_context=replace(read_context, current_time=""),
                rejection_codes=exc.rejection_codes)

        rejections: list[str] = []
        resolved_snapshot = snapshot

        # 1. 读取器状态与时钟
        if not self._enabled:
            rejections.append("READER_DISABLED")
        if _parse_timestamp(read_context.current_time) is None:
            rejections.append("CLOCK_UNAVAILABLE")

        # 1b. 严格模式：权威治理视图与观测来源证明
        governance_view: AuthoritativeGovernanceView | None = None
        if self._require_authoritative_governance:
            candidate_view = read_context.authoritative_view
            if (
                not isinstance(candidate_view, AuthoritativeGovernanceView)
                or not read_context.authoritative_view_trusted_public_key_path
            ):
                rejections.append("AUTHORITATIVE_GOVERNANCE_VIEW_MISSING")
            else:
                try:
                    verify_authoritative_governance_view(
                        candidate_view,
                        trusted_public_key_path=(
                            read_context.authoritative_view_trusted_public_key_path
                        ),
                        current_time=read_context.current_time,
                        max_age_seconds=(
                            read_context.authoritative_view_max_age_seconds
                        ),
                    )
                except TrustedArtifactError:
                    rejections.append("AUTHORITATIVE_GOVERNANCE_VIEW_INVALID")
                else:
                    if (
                        candidate_view.dataset_identity
                        != read_context.dataset_identity
                        or candidate_view.baseline_fingerprint
                        != read_context.current_baseline_fingerprint
                    ):
                        rejections.append("AUTHORITATIVE_GOVERNANCE_VIEW_INVALID")
                    else:
                        governance_view = candidate_view
        if self._require_observation_attestation:
            if observation_attestation is None:
                rejections.append("OBSERVATION_PROVENANCE_MISSING")
            else:
                try:
                    verify_observation_attestation(
                        observation_attestation,
                        observation,
                        trusted_public_key_path=(
                            read_context.observation_trusted_public_key_path
                        ),
                        current_time=read_context.current_time,
                        max_age_seconds=read_context.observation_max_age_seconds,
                        expected_dataset_identity=read_context.dataset_identity,
                    )
                except ObservationAttestationError:
                    rejections.append("OBSERVATION_PROVENANCE_INVALID")

        # 2. 快照
        if (
            snapshot is not None
            and snapshot_id is not None
            and isinstance(snapshot, ShadowActivationSnapshot)
            and str(snapshot_id) != snapshot.snapshot_id
        ):
            # 显式传入的两份快照身份互相矛盾：绝不任选其一。
            rejections.append("SNAPSHOT_MALFORMED")
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
                if self._registry is not None and (
                    resolved_snapshot.snapshot_id
                    != self._registry.active_snapshot_id
                ):
                    # 注册表驱动：只消费**显式选择**的活动影子快照。
                    rejections.append("SNAPSHOT_NOT_SELECTED")
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

        if self._require_independent_revocation:
            try:
                self._verify_independent_revocations(read_context, snapshot=resolved_snapshot)
            except TrustedArtifactError as exc:
                cache_key = self.cache_key(snapshot=resolved_snapshot, read_context=read_context,
                    source_identity_key=source_key)
                return self._result(source_key=source_key, canonical_id="",
                    status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                    snapshot_fingerprint=snapshot_fingerprint, cache_key=cache_key,
                    read_context=read_context, rejection_codes=exc.rejection_codes)
            cache_key = self.cache_key(snapshot=resolved_snapshot, read_context=read_context,
                source_identity_key=source_key)

        # Strict lifecycle is checked even for an unknown observation. Governance
        # ambiguity must never be downgraded to NOT_FOUND by an empty match set.
        if self._require_verified_supersession:
            try:
                self._verify_current_lifecycle(read_context, resolved_snapshot)
            except TrustedArtifactError as exc:
                return self._result(source_key=source_key, canonical_id="",
                    status=ShadowResolutionStatus.REJECTED_SHADOW.value,
                    snapshot_fingerprint=snapshot_fingerprint, cache_key=cache_key,
                    read_context=read_context, rejection_codes=exc.rejection_codes)

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

        # 6. 既有 Canonical 所有权兼容性（**全部**所有者参与判定，顺序无关）
        owner_conflicts: list[str] = []
        if governance_view is not None:
            existing_owners = governance_view.active_binding_owners_for(source_key)
            known_ids = governance_view.canonical_id_set()
        else:
            existing_owners = read_context.existing_owners_for(source_key)
            known_ids = read_context.canonical_id_set()
        if existing_owners and set(existing_owners) != {binding.candidate_canonical_id}:
            owner_conflicts.append("OWNERSHIP_CONFLICT")
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
        effective_revocations = (
            governance_view.revocations
            if governance_view is not None
            else read_context.revocation_registry
        )
        for candidate in matches:
            evidence = read_context.evidence_for(candidate.activation_id)
            if evidence is None:
                revalidation_codes.append("SNAPSHOT_AUTHORIZATION_MISSING")
                continue
            if candidate.activation_id not in resolved_snapshot.activation_ids:
                revalidation_codes.append("SNAPSHOT_MALFORMED")
                continue
            if governance_view is not None:
                proposal_id = evidence.authorization.proposal_id
                if not governance_view.is_active_proposal(proposal_id):
                    revalidation_codes.append("GOVERNANCE_STATE_NOT_ACTIVE")
                if (
                    governance_view.content_fingerprint_of(proposal_id)
                    != evidence.authorization.proposal_content_fingerprint
                ):
                    revalidation_codes.append("GOVERNANCE_CONTENT_MISMATCH")
            verification = self._verify_authorization(
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
                    revocation_registry=effective_revocations,
                    require_verified_governance=(
                        self._require_authoritative_governance
                    ),
                ),
                read_context=read_context,
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

            if self._require_authenticated_approval and (
                candidate.eligibility_decision_fingerprint != evidence.authorization.eligibility_decision_fingerprint
                or candidate.authorized_scope != evidence.authorization.authorized_scope
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
            snapshot_authority=(
                "REGISTRY_SELECTED"
                if self._registry is not None
                else "STANDALONE_INSPECTION"
            ),
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
    reenable_codes = [code for code, _ in REENABLE_REJECTION_CODES]
    if len(set(reenable_codes)) != len(reenable_codes):
        raise AssertionError("shadow re-enablement rejection codes must be unique")
    result_fields = {field.name for field in fields(ShadowReadResult)}
    parent_fields = {field.name for field in fields(ShadowRuntimeResolutionResult)}
    if not parent_fields <= result_fields:
        raise AssertionError(
            "ShadowReadResult must remain backward compatible with "
            "ShadowRuntimeResolutionResult"
        )


_assert_reader_is_coherent()
