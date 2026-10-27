"""
运行期映射资格评估（Runtime Mapping Eligibility）
=================================================
本模块回答**一个问题**，且只回答这一个问题：

    「一份已被所有者批准的作用域源映射，**在什么条件下**才可以参与未来的运行期
      身份解析？」

它**不**做任何别的事：

- 不修改 ``scraper.refresh()`` 行为；
- 不在运行期消费治理账本；
- 不创建绑定、不改写 Canonical ID、不改写任何历史数据；
- 不写任何文件（连一个字节都不写）；
- 不激活任何映射。

位置
----
``utils.ingestion_identity.validate_ingestion_identity`` 仍然是**唯一**的运行期
身份权威。本模块是它**未来**可选的前置闸门，本阶段只实现并验证该闸门本身：

    SourceObservation -> Identity Validation -> Mapping Proposal -> Owner Approval
        -> [ Runtime Eligibility Check ]  <-- 本模块（只读判定）
        -> （未来的可选消费；本阶段不存在）

判定完全由 ``utils.runtime_mapping_policy`` 的**声明式规则表**驱动：评估器只有
一个循环，逐条调用按规则码注册的纯函数，收集拒绝码。判定分支不散落在本模块里。

决策对象语义
------------
``RuntimeMappingEligibilityDecision`` 表达的是：

    「这份映射**是否会被允许**？」（would be allowed）

而**不是**：

    「这份映射**已经被应用**。」（has been applied）

``evaluated_at`` 是元数据，刻意**不**参与 ``decision_id`` —— 与治理账本的
``_decision_fingerprint`` 排除 ``occurred_at`` 的口径一致，因此同一组输入的判定
身份与时钟无关，重复评估天然幂等。

证据绑定与来源
--------------
判定不只看「签名是否有效」，还要求**证据本身自洽**：

- 当前 ``SourceObservation`` 必须真正被提案的证据覆盖（观测指纹属于提案的观测指纹
  集合；单一观测提案还要求观测引用与当前观测 ID 一致）；
- 由观测**独立推导**的作用域身份键与作用域必须与提案一致 —— 因此「用另一个观测的
  指纹签名的有效批准」无法把不一致的证据变成合格证据；
- **已记录**的提案指纹与治理状态必须存在且与当前提案一致；缺失即拒绝，
  绝不回退到提案自述状态（提案无法自证「已被批准」）；
- 治理事实的**来源**必须显式声明：调用方断言 / 隔离快照产生的判定是
  **条件式影子就绪判定**，绝不是生产授权；声称「独立验证过的账本证据」在本阶段
  一律阻断（没有代码路径能建立该证据）。

> 一条曾经为 ELIGIBLE 的判定**不是**永久授权：任何实质性的治理或 Canonical 基线
> 变化之后都必须重新评估。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    ACTIVE_STATES,
    GovernanceError,
    LEDGER_TRUST_ANCHOR_STATEMENT,
    MappingProposal,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    StaleBaselineError,
    approval_fingerprint,
    verify_mapping_approval,
)
from utils.authoritative_governance import (
    AuthoritativeGovernanceView,
    DEFAULT_GOVERNANCE_MAX_AGE_SECONDS,
    verify_authoritative_governance_view,
)
from utils.trusted_artifacts import TrustedArtifactError
from utils.runtime_mapping_policy import (
    ALLOWED_ACTIONS,
    ALLOWED_APPROVAL_PURPOSES,
    ALLOWED_APPROVAL_VERSIONS,
    ALLOWED_CONTEXT_EVIDENCE_PROVENANCE,
    ALLOWED_IDENTITY_KEY_VERSIONS,
    ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES,
    ALLOWED_PROPOSAL_STATES,
    ALLOWED_RUNTIME_IDENTITY_KEYS,
    ALLOWED_SOURCE_SCOPES,
    BLOCKED_ACTIONS,
    BLOCKED_CONTEXT_EVIDENCE_PROVENANCE,
    BLOCKED_IDENTITY_TYPES,
    BLOCKED_IDENTITY_STABILITIES,
    BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES,
    BLOCKED_RAW_IDENTIFIER_TYPES,
    CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
    CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
    EVALUATION_STORES,
    KNOWN_PROPOSAL_STATES,
    REJECTION_CODES,
    RUNTIME_ELIGIBILITY_RULES,
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
    RUNTIME_READ_ONLY_STORES,
    SETTLEMENT_STORES,
    SNAPSHOT_STORES,
    STORE_BLOCKED_EFFECTS,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    IdentifierStability,
    IdentityKeyKind,
    ProviderIdUniquenessScope,
    ScopedSourceIdentityKey,
    SourceObservation,
    canonical_json,
    fingerprint,
    identity_kind_is_sufficient,
)

#: 评估器版本。任何影响判定结果的变化都必须提升该版本。
#:
#: ``1.1.0``：新增证据绑定（观测 ↔ 提案）、记录指纹完整性、记录状态完整性与
#: 上下文证据来源规则；移除「缺失记录状态回退到提案自述状态」的路径。
#: ``1.2.0``：``VERIFIED_GOVERNANCE_EVIDENCE`` 只在签名权威视图逐项匹配时可接受。
RUNTIME_ELIGIBILITY_EVALUATOR_VERSION = "1.2.0"

#: 决策对象 schema 版本。
#:
#: ``1.1.0``：新增 ``evidence_provenance`` 字段（治理证据来源），
#: 使「条件式影子就绪判定」与「独立验证过的证据」在工件层面无法混淆。
RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION = "1.1.0"

#: 就绪清单 schema 版本。
RUNTIME_READINESS_MANIFEST_SCHEMA_VERSION = "1.0.0"

#: 就绪清单的类别标记：**只读分析工件**。
RUNTIME_READINESS_MANIFEST_KIND = "READ_ONLY_RUNTIME_READINESS"

#: 清单中禁止出现的键（任何「可执行」暗示都会让清单被误读为激活计划）。
FORBIDDEN_RUNTIME_MANIFEST_KEYS: tuple[str, ...] = (
    "apply",
    "activate",
    "enable",
    "execute",
    "executor",
    "runtime_plan",
    "bindings_to_create",
    "mutations",
)


class RuntimeEligibilityPolicyError(GovernanceError):
    """策略注册表与评估器不一致（fail closed）。"""


class RuntimeEligibilityResult(str, Enum):
    """运行期资格判定结果。只有两种取值，没有第三种。"""

    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# 运行期上下文（不可变输入）
# =============================================================================

@dataclass(frozen=True)
class GovernanceEvidenceProvenance:
    """
    治理上下文的**证据来源**契约（只读、不可变）。

    它回答四个问题，且刻意**不**把「自述的布尔值」当作「验证确实发生过」：

    1. 已记录的提案指纹 / 状态来自哪里？
    2. 这些信息指向哪个数据集与哪个基线？
    3. 结构性账本校验**是否真的**执行过？
    4. Ed25519 历史批准审计**是否真的**执行过？

    ``source`` 取值受策略注册表约束：

    - ``CALLER_ASSERTED_CONTEXT``：调用方自行断言 —— 由此产生的判定是
      **条件式影子就绪判定**，绝不是生产授权。
    - ``ISOLATED_SNAPSHOT_CONTEXT``：来自隔离的合成治理夹具快照；结构校验 / 批准审计
      可能已执行，但账本哈希链本身不是外部信任锚。
    - ``VERIFIED_GOVERNANCE_EVIDENCE``：声称由独立验证过的账本证据支撑。
      本阶段**没有**任何代码路径能建立该证据，因此该来源一律被阻断。

    ``structural_ledger_validation_performed`` 与
    ``authenticated_approval_audit_performed`` 是**自述声明**，评估器绝不据此升级信任；
    它们只用于如实报告「哪些验证据称执行过」，并在决策中连同
    ``verification_limitations`` 一起暴露。
    """

    source: str
    recorded_proposal_fingerprint_origin: str = ""
    recorded_proposal_state_origin: str = ""
    dataset_identity: str = ""
    baseline_fingerprint: str = ""
    structural_ledger_validation_performed: bool = False
    authenticated_approval_audit_performed: bool = False
    verification_limitations: tuple[str, ...] = ()
    ledger_trust_anchor: str = ""

    # -- 派生视图（只读） ---------------------------------------------------

    @property
    def is_conditional(self) -> bool:
        """该来源是否只支持**条件式**判定（未独立验证 -> 是）。"""
        return self.source != CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER

    @property
    def is_supported(self) -> bool:
        """该来源是否为本阶段可支持的来源。"""
        return self.source in ALLOWED_CONTEXT_EVIDENCE_PROVENANCE

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "recorded_proposal_fingerprint_origin": (
                self.recorded_proposal_fingerprint_origin
            ),
            "recorded_proposal_state_origin": self.recorded_proposal_state_origin,
            "dataset_identity": self.dataset_identity,
            "baseline_fingerprint": self.baseline_fingerprint,
            "structural_ledger_validation_performed": (
                self.structural_ledger_validation_performed
            ),
            "authenticated_approval_audit_performed": (
                self.authenticated_approval_audit_performed
            ),
            "verification_limitations": list(self.verification_limitations),
            "ledger_trust_anchor": self.ledger_trust_anchor,
            "is_conditional": self.is_conditional,
        }


def caller_asserted_provenance(
    *,
    dataset_identity: str = "",
    baseline_fingerprint: str = "",
    recorded_proposal_fingerprint_origin: str = "caller-supplied",
    recorded_proposal_state_origin: str = "caller-supplied",
) -> GovernanceEvidenceProvenance:
    """
    构造一份**调用方断言**来源（本阶段默认、且唯一无需外部文件即可构造的来源）。

    明确记录限制：没有结构性账本校验、没有历史批准审计、账本不是信任锚。
    """
    return GovernanceEvidenceProvenance(
        source=CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        recorded_proposal_fingerprint_origin=recorded_proposal_fingerprint_origin,
        recorded_proposal_state_origin=recorded_proposal_state_origin,
        dataset_identity=dataset_identity,
        baseline_fingerprint=baseline_fingerprint,
        structural_ledger_validation_performed=False,
        authenticated_approval_audit_performed=False,
        verification_limitations=(
            "The recorded governance facts were supplied by the caller; no structural "
            "ledger validation and no authenticated approval audit were performed.",
            "A conditional shadow-readiness decision is not a production authorization.",
        ),
        ledger_trust_anchor=LEDGER_TRUST_ANCHOR_STATEMENT,
    )


def isolated_snapshot_provenance(
    *,
    dataset_identity: str = "",
    baseline_fingerprint: str = "",
    recorded_proposal_fingerprint_origin: str = "",
    recorded_proposal_state_origin: str = "",
    structural_ledger_validation_performed: bool = False,
    authenticated_approval_audit_performed: bool = False,
    verification_limitations: Sequence[str] = (),
    ledger_trust_anchor: str = LEDGER_TRUST_ANCHOR_STATEMENT,
) -> GovernanceEvidenceProvenance:
    """
    构造一份**隔离快照**来源。

    ``ledger_trust_anchor`` 默认为账本自身的信任锚声明（哈希链只证明内部一致性，
    不是外部信任锚），且调用方无法静默把它改成「已防篡改」。
    """
    limitations = tuple(str(item) for item in verification_limitations)
    if not limitations:
        limitations = (
            "The recorded governance facts come from an isolated synthetic governance "
            "fixture; they are not production ledger authority.",
        )
    return GovernanceEvidenceProvenance(
        source=CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
        recorded_proposal_fingerprint_origin=recorded_proposal_fingerprint_origin,
        recorded_proposal_state_origin=recorded_proposal_state_origin,
        dataset_identity=dataset_identity,
        baseline_fingerprint=baseline_fingerprint,
        structural_ledger_validation_performed=bool(
            structural_ledger_validation_performed
        ),
        authenticated_approval_audit_performed=bool(
            authenticated_approval_audit_performed
        ),
        verification_limitations=limitations,
        ledger_trust_anchor=ledger_trust_anchor,
    )


@dataclass(frozen=True)
class RuntimeMappingContext:
    """
    评估所需的**全部**运行期事实（不可变）。

    刻意把「运行期知道什么」显式化，使评估成为纯函数：同样的上下文 + 同样的输入
    必然得到同样的判定。本类型不持有任何写入能力。

    ``evidence_provenance`` 是**必填**的：调用方必须显式声明治理事实的来源，
    否则无法区分「条件式影子就绪判定」与「独立验证过的证据」。
    """

    dataset_identity: str
    current_baseline_fingerprint: str
    evidence_provenance: GovernanceEvidenceProvenance
    #: 运行期已知的 Canonical ID 集合。
    canonical_ids: tuple[str, ...] = ()
    #: 显式可信的所有者公钥路径（**必须**由调用方提供；内嵌公钥永不作为信任锚）。
    trusted_public_key_path: str | None = None
    #: 运行期期望的批准人 / 操作者身份。
    expected_approver: str | None = None
    expected_operator: str | None = None
    #: 治理账本中记录的提案 ID / 指纹 / 当前状态（只读快照）。
    recorded_proposal_id: str | None = None
    recorded_proposal_fingerprint: str | None = None
    recorded_proposal_state: str | None = None
    #: 是否显式允许「新事件」模式（目标 Canonical ID 尚不存在）。
    allow_new_event: bool = False
    #: 运行期已知的源身份绑定：``(source_identity_key, canonical_id)``。
    known_source_bindings: tuple[tuple[str, str], ...] = ()
    #: 已被消费过的批准件：``(approval_fingerprint, source_identity_key)``。
    prior_approval_bindings: tuple[tuple[str, str], ...] = ()
    #: 待决歧义（源身份键或冲突类别）。
    pending_ambiguities: tuple[str, ...] = ()
    #: 若该映射生效，**必须**被改写的存储列表（空 = 无历史影响）。
    required_rewrite_targets: tuple[str, ...] = ()
    #: 历史引用索引：``(match_id, (store 指针, ...))``。
    historical_reference_index: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: 显式标志：该映射是否要求改写 Canonical ID / 要求迁移。
    canonical_rename_required: bool = False
    migration_required: bool = False
    #: 权威治理视图（仅在 ``VERIFIED_GOVERNANCE_EVIDENCE`` 来源下被验证与使用）。
    authoritative_view: AuthoritativeGovernanceView | None = None
    #: 验证权威视图证明的显式可信公钥路径。
    authoritative_view_trusted_public_key_path: str | None = None
    #: 权威视图允许的最大新鲜度（秒）。
    authoritative_view_max_age_seconds: float = DEFAULT_GOVERNANCE_MAX_AGE_SECONDS
    #: Opt-in approval integrity contract; caller assertions never satisfy it.
    require_authenticated_approval: bool = False

    # -- 派生视图（只读，纯函数） -------------------------------------------

    def canonical_id_set(self) -> frozenset[str]:
        return frozenset(str(cid) for cid in self.canonical_ids)

    def rewrite_targets(self) -> frozenset[str]:
        return frozenset(str(t) for t in self.required_rewrite_targets)

    def rewrite_effects(self) -> frozenset[str]:
        """把「需要改写的存储」翻译为**被阻断的效果**集合。"""
        targets = self.rewrite_targets()
        effects = {
            effect for store, effect in STORE_BLOCKED_EFFECTS if store in targets
        }
        return frozenset(effects)

    def referenced_stores_for(self, match_id: str) -> tuple[str, ...]:
        for candidate, pointers in self.historical_reference_index:
            if candidate == match_id:
                return tuple(pointers)
        return ()

    def as_dict(self) -> dict:
        return {
            "dataset_identity": self.dataset_identity,
            "current_baseline_fingerprint": self.current_baseline_fingerprint,
            "evidence_provenance": self.evidence_provenance.as_dict(),
            "canonical_ids": sorted(self.canonical_id_set()),
            "trust_anchor_supplied": self.trusted_public_key_path is not None,
            "expected_approver": self.expected_approver,
            "expected_operator": self.expected_operator,
            "recorded_proposal_id": self.recorded_proposal_id,
            "recorded_proposal_fingerprint": self.recorded_proposal_fingerprint,
            "recorded_proposal_state": self.recorded_proposal_state,
            "allow_new_event": self.allow_new_event,
            "known_source_bindings": [list(pair) for pair in self.known_source_bindings],
            "prior_approval_bindings": [list(pair) for pair in self.prior_approval_bindings],
            "pending_ambiguities": list(self.pending_ambiguities),
            "required_rewrite_targets": sorted(self.rewrite_targets()),
            "required_rewrite_effects": sorted(self.rewrite_effects()),
            "historical_reference_index": {
                match_id: list(pointers)
                for match_id, pointers in sorted(self.historical_reference_index)
            },
            "canonical_rename_required": self.canonical_rename_required,
            "migration_required": self.migration_required,
            "authoritative_view_supplied": self.authoritative_view is not None,
            "authoritative_view_fingerprint": (
                self.authoritative_view.fingerprint()
                if self.authoritative_view is not None
                else ""
            ),
            "authoritative_view_trust_anchor_supplied": (
                self.authoritative_view_trusted_public_key_path is not None
            ),
            "authoritative_view_max_age_seconds": (
                self.authoritative_view_max_age_seconds
            ),
        }


# =============================================================================
# 决策对象（不可变、可哈希、无执行方法）
# =============================================================================

@dataclass(frozen=True)
class RuntimeMappingEligibilityDecision:
    """
    一次运行期资格判定的**不可变**结果。

    只表达「是否会被允许」，**不**表达「已被应用」。刻意不提供任何变更或执行方法：
    只有纯访问器与确定性序列化。
    """

    schema_version: str
    decision_id: str
    proposal_id: str
    source_identity_key: str
    canonical_id: str
    eligibility_result: str
    rejection_codes: tuple[str, ...]
    evaluated_at: str
    evaluator_version: str
    evidence_fingerprint: str
    approval_fingerprint: str
    baseline_fingerprint: str
    #: 治理证据来源（见 ``GovernanceEvidenceProvenance``）。ELIGIBLE 且来源为
    #: 条件式（调用方断言 / 隔离快照）时，该判定**只是**条件式影子就绪判定，
    #: 绝不是生产授权。
    evidence_provenance: str = CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED

    # -- 视图 -------------------------------------------------------------

    @property
    def is_eligible(self) -> bool:
        return self.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value

    @property
    def is_conditional(self) -> bool:
        """
        该判定是否为**条件式**（治理事实未经独立验证）。

        只要来源不是独立验证过的账本证据，即为条件式 —— 无论结果是
        ELIGIBLE 还是 NOT_ELIGIBLE。
        """
        return self.evidence_provenance != CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER

    @property
    def is_production_authorization(self) -> bool:
        """
        是否构成生产授权。**永远为 False**：本阶段不存在生产映射授权，
        且独立验证过的证据来源不可达。
        """
        return False

    @property
    def rejection_reasons(self) -> tuple[str, ...]:
        return self.rejection_codes

    def identity_payload(self) -> dict:
        """
        判定**身份**载荷：刻意排除 ``evaluated_at``，使同一组输入的判定身份与时钟
        无关（重复评估幂等）。
        """
        return {
            "schema_version": self.schema_version,
            "evaluator_version": self.evaluator_version,
            "proposal_id": self.proposal_id,
            "source_identity_key": self.source_identity_key,
            "canonical_id": self.canonical_id,
            "eligibility_result": self.eligibility_result,
            "rejection_codes": list(self.rejection_codes),
            "evidence_fingerprint": self.evidence_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "baseline_fingerprint": self.baseline_fingerprint,
            "evidence_provenance": self.evidence_provenance,
        }

    def as_dict(self) -> dict:
        payload = self.identity_payload()
        payload["decision_id"] = self.decision_id
        payload["evaluated_at"] = self.evaluated_at
        payload["is_conditional"] = self.is_conditional
        payload["is_production_authorization"] = self.is_production_authorization
        return payload

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


# =============================================================================
# 评估事实（内部；不暴露、不持久化）
# =============================================================================

class _EvaluationFacts:
    """一次评估的全部只读事实（内部类型）。"""

    __slots__ = (
        "proposal",
        "approval",
        "observation",
        "context",
        "identity_key",
        "identity_kind",
        "identity_value",
        "identity_key_version",
        "identity_sufficient",
        "provider_scope",
        "raw_identifier_type",
        "identifier_stability",
        "observation_fingerprint",
        "observation_id",
        "derived_identity_key",
        "derived_scope",
        "approval_fp",
        "proposal_fingerprint",
        "canonical_id",
        "source_scope",
        "canonical_change_required",
        "rewrite_targets",
        "rewrite_effects",
        "approval_verification_error",
        "evaluation_time",
    )

    def __init__(
        self,
        *,
        proposal: MappingProposal | None,
        approval: Mapping[str, Any] | None,
        observation: SourceObservation | None,
        context: RuntimeMappingContext,
        evaluated_at: str | None = None,
    ) -> None:
        self.proposal = proposal
        self.approval = approval
        self.observation = observation
        self.context = context
        self.evaluation_time = str(evaluated_at or _utc_now())

        key: dict[str, str] = {}
        if isinstance(proposal, MappingProposal):
            key = proposal.identity_key_dict
        self.identity_key = key
        self.identity_kind = str(key.get("identity_kind", ""))
        self.identity_value = str(key.get("identity_value", ""))
        self.identity_key_version = str(key.get("identity_key_schema_version", ""))
        self.identity_sufficient = identity_kind_is_sufficient(self.identity_kind)
        self.provider_scope = str(key.get("provider_id_uniqueness_scope", ""))

        self.raw_identifier_type = (
            str(getattr(observation, "raw_provider_id_type", "")) if observation else ""
        )
        self.identifier_stability = (
            str(getattr(observation, "identifier_stability", "")) if observation else ""
        )
        self.observation_fingerprint = (
            observation.semantic_fingerprint()
            if observation is not None and hasattr(observation, "semantic_fingerprint")
            else ""
        )
        self.observation_id = (
            str(getattr(observation, "observation_id", "")) if observation else ""
        )
        # 由观测**独立推导**的身份键与作用域（绝不复用提案自述值）。
        if observation is not None and hasattr(observation, "stability_scope"):
            derived_key = ScopedSourceIdentityKey.for_observation(observation)
            self.derived_identity_key = derived_key.as_dict()
            self.derived_scope = dict(observation.stability_scope.as_dict())
        else:
            self.derived_identity_key = {}
            self.derived_scope = {}
        self.approval_fp = (
            approval_fingerprint(approval) if isinstance(approval, Mapping) else ""
        )
        self.proposal_fingerprint = (
            proposal.fingerprint() if isinstance(proposal, MappingProposal) else ""
        )
        self.canonical_id = (
            str(proposal.candidate_canonical_id or "")
            if isinstance(proposal, MappingProposal)
            else ""
        )
        self.source_scope = (
            (
                str(proposal.scope_dict.get("adapter", "")),
                str(proposal.scope_dict.get("provider_namespace", "")),
            )
            if isinstance(proposal, MappingProposal)
            else ("", "")
        )

        owner = (
            str(proposal.existing_canonical_owner or "")
            if isinstance(proposal, MappingProposal)
            else ""
        )
        self.canonical_change_required = bool(
            context.canonical_rename_required
            or (owner and self.canonical_id and owner != self.canonical_id)
        )
        self.rewrite_targets = context.rewrite_targets()
        self.rewrite_effects = context.rewrite_effects()

        self.approval_verification_error: str | None = None

    # -- 便捷视图 ---------------------------------------------------------

    def binding_canonical_ids_for_source(self) -> frozenset[str]:
        return frozenset(
            str(cid)
            for key, cid in self.context.known_source_bindings
            if key == canonical_json(self.identity_key)
        )

    def prior_bindings_for_approval(self) -> frozenset[str]:
        return frozenset(
            str(key)
            for fp, key in self.context.prior_approval_bindings
            if fp and fp == self.approval_fp
        )


# =============================================================================
# 规则检查（按规则码注册的纯函数）
# =============================================================================

def _approval_field(facts: _EvaluationFacts, name: str) -> Any:
    if not isinstance(facts.approval, Mapping):
        return None
    return facts.approval.get(name)


def _approval_absent(facts: _EvaluationFacts) -> bool:
    """
    批准件缺失 / 非对象 / 空对象。

    此时**只有** ``APPROVAL_PRESENT`` 负责报告 —— 字段级规则一律沉默，避免把一个
    清晰事实膨胀成一簇噪声拒绝码。
    """
    if not isinstance(facts.approval, Mapping):
        return True
    return not dict(facts.approval)


# ---------------------------------------------------------------------------
# 证据绑定检查（SourceObservation ↔ MappingProposal）
# ---------------------------------------------------------------------------

def _check_source_observation_in_proposal(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    当前观测必须**真正被提案的证据覆盖**。

    - 观测缺失 / 无有效指纹 -> 拒绝；
    - 提案的观测引用必须非空；
    - 观测指纹必须属于提案的观测指纹集合（提案可合法携带**多个**观测指纹，
      因此绝不要求它必须是第一个）；
    - 提案只携带单个观测指纹时，其观测引用必须与当前观测 ID 一致。
    """
    if not isinstance(facts.proposal, MappingProposal):
        return ()
    if not facts.observation_fingerprint or not facts.observation_id:
        return ("SOURCE_OBSERVATION_NOT_IN_PROPOSAL",)

    reference = str(facts.proposal.source_observation_ref or "").strip()
    if not reference:
        return ("SOURCE_OBSERVATION_NOT_IN_PROPOSAL",)

    fingerprints = tuple(str(f) for f in facts.proposal.observation_fingerprints)
    if facts.observation_fingerprint not in fingerprints:
        return ("SOURCE_OBSERVATION_NOT_IN_PROPOSAL",)

    if len(fingerprints) == 1 and reference != facts.observation_id:
        return ("SOURCE_OBSERVATION_NOT_IN_PROPOSAL",)
    return ()


def _check_source_identity_key_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    由 SourceObservation **独立推导**的作用域身份键必须与提案的身份键一致。

    因此「用另一个观测的指纹签名的有效批准」无法把不一致的证据变成合格证据。
    """
    if not isinstance(facts.proposal, MappingProposal):
        return ()
    if not facts.derived_identity_key:
        return ()
    if canonical_json(facts.derived_identity_key) != facts.proposal.source_key:
        return ("SOURCE_IDENTITY_KEY_MISMATCH",)
    return ()


def _check_source_scope_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    """观测的源作用域必须与提案声明的作用域一致（逐维比较可独立推导的维度）。"""
    if not isinstance(facts.proposal, MappingProposal):
        return ()
    if not facts.derived_scope:
        return ()
    claimed = facts.proposal.scope_dict
    for dimension in (
        "adapter",
        "sport",
        "event_date",
        "provider_namespace",
        "raw_identifier_type",
    ):
        if dimension not in claimed:
            continue
        if str(claimed.get(dimension, "")) != str(facts.derived_scope.get(dimension, "")):
            return ("SOURCE_SCOPE_MISMATCH",)
    return ()


def _check_identity_key_version_current(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.identity_key_version not in ALLOWED_IDENTITY_KEY_VERSIONS:
        return ("IDENTITY_KEY_VERSION_NOT_CURRENT",)
    return ()


def _check_identity_key_kind_allowed(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.identity_kind not in ALLOWED_RUNTIME_IDENTITY_KEYS:
        return ("IDENTITY_KIND_NOT_ALLOWED",)
    return ()


def _check_identity_evidence_sufficient(facts: _EvaluationFacts) -> tuple[str, ...]:
    if not facts.identity_sufficient:
        return ("IDENTITY_EVIDENCE_INSUFFICIENT",)
    return ()


def _check_unverified_provider_id_blocked(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value:
        return ("UNVERIFIED_PROVIDER_ID",)
    return ()


def _check_blocked_identity_type_rejected(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.identity_kind in BLOCKED_IDENTITY_TYPES and facts.identity_kind != (
        IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
    ):
        return ("IDENTITY_TYPE_BLOCKED",)
    return ()


def _check_provider_id_scope_verified(facts: _EvaluationFacts) -> tuple[str, ...]:
    # 只对**承载 provider 标识符**的身份键生效：竞彩编号身份与该作用域无关。
    if facts.identity_kind != IdentityKeyKind.RAW_PROVIDER_ID.value:
        return ()
    if facts.provider_scope not in ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES:
        return ("PROVIDER_ID_SCOPE_NOT_VERIFIED",)
    return ()


def _check_row_position_identity_blocked(facts: _EvaluationFacts) -> tuple[str, ...]:
    if (
        facts.identifier_stability == IdentifierStability.ROW_POSITION_UNSTABLE.value
        and IdentifierStability.ROW_POSITION_UNSTABLE.value in BLOCKED_IDENTITY_STABILITIES
    ):
        return ("ROW_POSITION_UNSTABLE",)
    return ()


def _check_raw_identifier_type_durable(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.raw_identifier_type and facts.raw_identifier_type in (
        BLOCKED_RAW_IDENTIFIER_TYPES
    ):
        return ("RAW_IDENTIFIER_TYPE_NOT_DURABLE",)
    return ()


def _check_source_fingerprint_deterministic(facts: _EvaluationFacts) -> tuple[str, ...]:
    value = facts.observation_fingerprint
    if not value or len(value) != 64:
        return ("SOURCE_FINGERPRINT_MISSING",)
    return ()


def _check_identity_not_ambiguous(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.identity_value in facts.context.pending_ambiguities:
        return ("IDENTITY_AMBIGUOUS",)
    if facts.identity_kind == IdentityKeyKind.AMBIGUOUS_SHARED_SOURCE_ID.value:
        return ("IDENTITY_AMBIGUOUS",)
    return ()


def _check_no_competing_canonical_owners(facts: _EvaluationFacts) -> tuple[str, ...]:
    if len(facts.binding_canonical_ids_for_source()) > 1:
        return ("CANONICAL_OWNER_CONFLICT",)
    return ()


def _check_proposal_present(facts: _EvaluationFacts) -> tuple[str, ...]:
    if not isinstance(facts.proposal, MappingProposal):
        return ("PROPOSAL_MISSING",)
    if not facts.proposal.proposal_id:
        return ("PROPOSAL_MISSING",)
    return ()


def _check_governance_evidence_provenance_supported(
    facts: _EvaluationFacts,
) -> tuple[str, ...]:
    """
    上下文必须声明一个本阶段**可支持**的治理证据来源。

    ``VERIFIED_GOVERNANCE_EVIDENCE`` 只在同时提供**已签名、未过期、事实逐项匹配**
    的权威治理视图时才被接受；仅声称该来源而没有可验证证明一律阻断。
    """
    provenance = facts.context.evidence_provenance
    if facts.context.require_authenticated_approval and (
        provenance is None or provenance.source != CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER
    ):
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if provenance is None:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if provenance.source == CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER:
        return _verified_authoritative_view_codes(facts)
    if provenance.source in BLOCKED_CONTEXT_EVIDENCE_PROVENANCE:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if provenance.source not in ALLOWED_CONTEXT_EVIDENCE_PROVENANCE:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    return ()


def _verified_authoritative_view_codes(facts: _EvaluationFacts) -> tuple[str, ...]:
    """验证 ``VERIFIED_GOVERNANCE_EVIDENCE`` 所需的签名视图与事实绑定。"""
    context = facts.context
    view = context.authoritative_view
    if not isinstance(view, AuthoritativeGovernanceView):
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if not context.authoritative_view_trusted_public_key_path:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    try:
        verify_authoritative_governance_view(
            view,
            trusted_public_key_path=context.authoritative_view_trusted_public_key_path,
            current_time=facts.evaluation_time,
            max_age_seconds=context.authoritative_view_max_age_seconds,
            require_authenticated_approvals=context.require_authenticated_approval,
        )
    except TrustedArtifactError:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if (
        view.dataset_identity != context.dataset_identity
        or view.baseline_fingerprint != context.current_baseline_fingerprint
    ):
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    if view.canonical_id_set() != context.canonical_id_set():
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    view_bindings = tuple(
        sorted((key, canonical_id) for key, canonical_id, _pid in view.active_bindings)
    )
    if tuple(sorted(context.known_source_bindings)) != view_bindings:
        return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)

    proposal = facts.proposal
    if not isinstance(proposal, MappingProposal):
        return ()
    recorded_id = context.recorded_proposal_id
    if recorded_id is None or str(recorded_id).strip() != proposal.proposal_id:
        return ("RECORDED_STATE_PROPOSAL_MISMATCH",)
    recorded_state = context.recorded_proposal_state
    if recorded_state is not None and view.state_of(proposal.proposal_id) != recorded_state:
        return ("RECORDED_STATE_PROPOSAL_MISMATCH",)
    recorded_fingerprint = context.recorded_proposal_fingerprint
    if recorded_fingerprint is not None and (
        view.content_fingerprint_of(proposal.proposal_id)
        != proposal.content_fingerprint()
    ):
        return ("PROPOSAL_FINGERPRINT_MISMATCH",)
    if not view.is_active_proposal(proposal.proposal_id):
        return ("MAPPING_STATE_NOT_ACTIVE",)
    if context.require_authenticated_approval:
        record = view.approved_record_for(proposal.proposal_id)
        if (record is None or record.approval_fingerprint != facts.approval_fp
            or record.source_identity_key != proposal.source_key
            or record.candidate_canonical_id != proposal.candidate_canonical_id
            or record.baseline_fingerprint != context.current_baseline_fingerprint):
            return ("EVIDENCE_PROVENANCE_UNSUPPORTED",)
    return ()


def _check_proposal_fingerprint_present(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    **必须**存在已记录的提案指纹。

    缺失 / 空白 / 非字符串一律拒绝 —— 绝不把「没有记录」当作「校验可选」。
    """
    recorded = facts.context.recorded_proposal_fingerprint
    if recorded is None:
        return ("PROPOSAL_FINGERPRINT_MISSING",)
    if not isinstance(recorded, str) or not recorded.strip():
        return ("PROPOSAL_FINGERPRINT_MISSING",)
    return ()


def _check_proposal_fingerprint_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    已记录的提案指纹必须与当前提案指纹逐字一致。

    格式不合法（非 64 位小写十六进制）同样按不一致处理。
    指纹匹配**只**说明与已记录值一致，并不证明账本本身可信（那是来源契约的事）。
    """
    recorded = facts.context.recorded_proposal_fingerprint
    if not isinstance(recorded, str) or not recorded.strip():
        return ()
    if not _is_sha256_hex(recorded):
        return ("PROPOSAL_FINGERPRINT_MISMATCH",)
    if recorded != facts.proposal_fingerprint:
        return ("PROPOSAL_FINGERPRINT_MISMATCH",)
    return ()


def _is_sha256_hex(value: str) -> bool:
    if len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _check_recorded_state_present(facts: _EvaluationFacts) -> tuple[str, ...]:
    """**必须**存在已记录的治理状态；缺失即拒绝（绝不回退到提案自述状态）。"""
    state = facts.context.recorded_proposal_state
    if state is None:
        return ("RECORDED_STATE_MISSING",)
    if not isinstance(state, str) or not state.strip():
        return ("RECORDED_STATE_MISSING",)
    return ()


def _check_recorded_state_known(facts: _EvaluationFacts) -> tuple[str, ...]:
    """已记录状态必须是已知状态之一。"""
    state = facts.context.recorded_proposal_state
    if not isinstance(state, str) or not state.strip():
        return ()
    if state not in KNOWN_PROPOSAL_STATES:
        return ("RECORDED_STATE_UNKNOWN",)
    return ()


def _check_recorded_state_proposal_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    """已记录状态必须对应**被评估的那个提案 ID**。"""
    if not isinstance(facts.proposal, MappingProposal):
        return ()
    recorded_id = facts.context.recorded_proposal_id
    if recorded_id is None or not str(recorded_id).strip():
        return ("RECORDED_STATE_PROPOSAL_MISMATCH",)
    if str(recorded_id) != facts.proposal.proposal_id:
        return ("RECORDED_STATE_PROPOSAL_MISMATCH",)
    return ()


def _check_proposal_state_active(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    已记录状态必须是活动终态。

    刻意**不再**回退到 ``proposal.status``：提案本身无法自证「已被批准」。
    """
    state = facts.context.recorded_proposal_state
    if state not in ALLOWED_PROPOSAL_STATES:
        return ("MAPPING_STATE_NOT_ACTIVE",)
    return ()


def _check_mapping_not_revoked(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.context.recorded_proposal_state == "REVOKED":
        return ("MAPPING_REVOKED",)
    return ()


def _check_mapping_not_superseded(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.context.recorded_proposal_state == "SUPERSEDED":
        return ("MAPPING_SUPERSEDED",)
    return ()


def _check_approval_present(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ("APPROVAL_MISSING",)
    return ()


def _check_trust_anchor_present(facts: _EvaluationFacts) -> tuple[str, ...]:
    if not facts.context.trusted_public_key_path:
        return ("TRUST_ANCHOR_MISSING",)
    return ()


def _check_approval_version_allowed(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    version = _approval_field(facts, "approval_schema_version")
    if version not in ALLOWED_APPROVAL_VERSIONS:
        return ("APPROVAL_LEGACY_VERSION",)
    return ()


def _check_approval_purpose_allowed(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    if _approval_field(facts, "purpose") not in ALLOWED_APPROVAL_PURPOSES:
        return ("APPROVAL_PURPOSE_MISMATCH",)
    return ()


def _check_approval_approver_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    signed = str(_approval_field(facts, "approver") or "")
    if not signed:
        return ("APPROVAL_WRONG_APPROVER",)
    expected = facts.context.expected_approver
    if expected is not None and signed != expected:
        return ("APPROVAL_WRONG_APPROVER",)
    return ()


def _check_approval_operator_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    signed = str(_approval_field(facts, "operator") or "")
    if not signed:
        return ("APPROVAL_WRONG_OPERATOR",)
    if isinstance(facts.proposal, MappingProposal) and signed != facts.proposal.operator:
        return ("APPROVAL_WRONG_OPERATOR",)
    expected = facts.context.expected_operator
    if expected is not None and signed != expected:
        return ("APPROVAL_WRONG_OPERATOR",)
    return ()


def _check_approval_signature_verifies(facts: _EvaluationFacts) -> tuple[str, ...]:
    """
    独立复核 Ed25519 签名与全部绑定（复用治理层的验证原语，绝不重复实现密码学）。

    这里只做「验证」，绝不追加账本、绝不产生任何状态变更。
    """
    if _approval_absent(facts):
        return ()
    if not isinstance(facts.proposal, MappingProposal):
        return ()
    if facts.approval_verification_error is not None:
        return (facts.approval_verification_error,)
    if not facts.context.trusted_public_key_path:
        # 缺少可信锚点时无法验证签名 -> 由 TRUST_ANCHOR_MISSING 负责报告。
        return ()
    try:
        verify_mapping_approval(
            facts.approval,
            proposal=facts.proposal,
            source_observation_fingerprint=facts.observation_fingerprint,
            dataset_identity=facts.context.dataset_identity,
            current_baseline_fingerprint=facts.context.current_baseline_fingerprint,
            trusted_public_key_path=facts.context.trusted_public_key_path,
        )
    except StaleBaselineError:
        return ("BASELINE_STALE",)
    except GovernanceError:
        return ("APPROVAL_SIGNATURE_INVALID",)
    return ()


def _check_baseline_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    expected = _approval_field(facts, "expected_baseline_fingerprint")
    if expected is None:
        return ()
    if expected != facts.context.current_baseline_fingerprint:
        return ("BASELINE_STALE",)
    return ()


def _check_dataset_identity_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    bound = _approval_field(facts, "dataset_identity")
    if bound is None:
        return ()
    if bound != facts.context.dataset_identity:
        return ("DATASET_IDENTITY_MISMATCH",)
    return ()


def _check_approval_not_replayed(facts: _EvaluationFacts) -> tuple[str, ...]:
    if _approval_absent(facts):
        return ()
    prior_keys = facts.prior_bindings_for_approval()
    if not prior_keys:
        return ()
    current = canonical_json(facts.identity_key)
    if any(key != current for key in prior_keys):
        return ("APPROVAL_REPLAYED",)
    return ()


def _check_action_allowed(facts: _EvaluationFacts) -> tuple[str, ...]:
    action = (
        facts.proposal.requested_action if isinstance(facts.proposal, MappingProposal) else ""
    )
    if not action:
        return ("ACTION_NOT_ALLOWED",)
    if action in BLOCKED_ACTIONS:
        return ("ACTION_NOT_ALLOWED",)
    if action not in ALLOWED_ACTIONS:
        return ("ACTION_NOT_ALLOWED",)
    return ()


def _check_source_scope_allowed(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.source_scope not in ALLOWED_SOURCE_SCOPES:
        return ("SOURCE_SCOPE_NOT_ALLOWED",)
    return ()


def _check_canonical_owner_exists_or_new_event(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.context.allow_new_event:
        return ()
    if not facts.canonical_id:
        return ("CANONICAL_OWNER_MISSING",)
    if facts.canonical_id not in facts.context.canonical_id_set():
        return ("CANONICAL_OWNER_MISSING",)
    return ()


def _check_no_canonical_ownership_conflict(facts: _EvaluationFacts) -> tuple[str, ...]:
    bound = facts.binding_canonical_ids_for_source()
    if len(bound) == 1 and facts.canonical_id not in bound:
        return ("CANONICAL_OWNERSHIP_CONFLICT",)
    return ()


def _check_no_canonical_id_change(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.canonical_change_required:
        return ("CANONICAL_ID_CHANGE_REQUIRED",)
    return ()


def _check_no_historical_rewrite(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.rewrite_targets:
        return ("HISTORICAL_REFERENCE_CONFLICT",)
    if facts.canonical_change_required:
        owner = (
            str(facts.proposal.existing_canonical_owner or "")
            if isinstance(facts.proposal, MappingProposal)
            else ""
        )
        referenced = set(facts.context.referenced_stores_for(owner))
        referenced |= set(facts.context.referenced_stores_for(facts.canonical_id))
        if referenced:
            return ("HISTORICAL_REFERENCE_CONFLICT",)
    return ()


def _check_no_snapshot_impact(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.rewrite_targets & SNAPSHOT_STORES:
        return ("SNAPSHOT_IMPACT",)
    return ()


def _check_no_settlement_impact(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.rewrite_targets & SETTLEMENT_STORES:
        return ("SETTLEMENT_IMPACT",)
    return ()


def _check_no_evaluation_impact(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.rewrite_targets & EVALUATION_STORES:
        return ("EVALUATION_IMPACT",)
    return ()


def _check_no_migration_required(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.context.migration_required:
        return ("MIGRATION_REQUIRED",)
    if "HISTORICAL_MIGRATION" in facts.rewrite_effects:
        return ("MIGRATION_REQUIRED",)
    return ()


def _check_no_pending_ambiguity(facts: _EvaluationFacts) -> tuple[str, ...]:
    if facts.context.pending_ambiguities:
        return ("PENDING_AMBIGUITY",)
    return ()


#: 规则码 -> 纯检查函数。**唯一**的分派表。
_RULE_CHECKS = {
    "SOURCE_OBSERVATION_IN_PROPOSAL": _check_source_observation_in_proposal,
    "SOURCE_IDENTITY_KEY_MATCHES": _check_source_identity_key_matches,
    "SOURCE_SCOPE_MATCHES": _check_source_scope_matches,
    "IDENTITY_KEY_VERSION_CURRENT": _check_identity_key_version_current,
    "IDENTITY_KEY_KIND_ALLOWED": _check_identity_key_kind_allowed,
    "IDENTITY_EVIDENCE_SUFFICIENT": _check_identity_evidence_sufficient,
    "UNVERIFIED_PROVIDER_ID_BLOCKED": _check_unverified_provider_id_blocked,
    "BLOCKED_IDENTITY_TYPE_REJECTED": _check_blocked_identity_type_rejected,
    "PROVIDER_ID_SCOPE_VERIFIED": _check_provider_id_scope_verified,
    "ROW_POSITION_IDENTITY_BLOCKED": _check_row_position_identity_blocked,
    "RAW_IDENTIFIER_TYPE_DURABLE": _check_raw_identifier_type_durable,
    "SOURCE_FINGERPRINT_DETERMINISTIC": _check_source_fingerprint_deterministic,
    "IDENTITY_NOT_AMBIGUOUS": _check_identity_not_ambiguous,
    "NO_COMPETING_CANONICAL_OWNERS": _check_no_competing_canonical_owners,
    "PROPOSAL_PRESENT": _check_proposal_present,
    "GOVERNANCE_EVIDENCE_PROVENANCE_SUPPORTED": (
        _check_governance_evidence_provenance_supported
    ),
    "PROPOSAL_FINGERPRINT_PRESENT": _check_proposal_fingerprint_present,
    "PROPOSAL_FINGERPRINT_MATCHES": _check_proposal_fingerprint_matches,
    "RECORDED_STATE_PRESENT": _check_recorded_state_present,
    "RECORDED_STATE_KNOWN": _check_recorded_state_known,
    "RECORDED_STATE_PROPOSAL_MATCHES": _check_recorded_state_proposal_matches,
    "PROPOSAL_STATE_ACTIVE": _check_proposal_state_active,
    "MAPPING_NOT_REVOKED": _check_mapping_not_revoked,
    "MAPPING_NOT_SUPERSEDED": _check_mapping_not_superseded,
    "APPROVAL_PRESENT": _check_approval_present,
    "TRUST_ANCHOR_PRESENT": _check_trust_anchor_present,
    "APPROVAL_VERSION_ALLOWED": _check_approval_version_allowed,
    "APPROVAL_PURPOSE_ALLOWED": _check_approval_purpose_allowed,
    "APPROVAL_APPROVER_MATCHES": _check_approval_approver_matches,
    "APPROVAL_OPERATOR_MATCHES": _check_approval_operator_matches,
    "APPROVAL_SIGNATURE_VERIFIES": _check_approval_signature_verifies,
    "BASELINE_MATCHES": _check_baseline_matches,
    "DATASET_IDENTITY_MATCHES": _check_dataset_identity_matches,
    "APPROVAL_NOT_REPLAYED": _check_approval_not_replayed,
    "ACTION_ALLOWED": _check_action_allowed,
    "SOURCE_SCOPE_ALLOWED": _check_source_scope_allowed,
    "CANONICAL_OWNER_EXISTS_OR_NEW_EVENT": _check_canonical_owner_exists_or_new_event,
    "NO_CANONICAL_OWNERSHIP_CONFLICT": _check_no_canonical_ownership_conflict,
    "NO_CANONICAL_ID_CHANGE": _check_no_canonical_id_change,
    "NO_HISTORICAL_REWRITE": _check_no_historical_rewrite,
    "NO_SNAPSHOT_IMPACT": _check_no_snapshot_impact,
    "NO_SETTLEMENT_IMPACT": _check_no_settlement_impact,
    "NO_EVALUATION_IMPACT": _check_no_evaluation_impact,
    "NO_MIGRATION_REQUIRED": _check_no_migration_required,
    "NO_PENDING_AMBIGUITY": _check_no_pending_ambiguity,
}


def _assert_rule_registry_is_complete() -> None:
    """导入时断言：声明式规则表与分派表必须一一对应（fail closed）。"""
    declared = {rule.code for rule in RUNTIME_ELIGIBILITY_RULES}
    registered = set(_RULE_CHECKS)
    if declared != registered:
        raise RuntimeEligibilityPolicyError(
            "runtime eligibility rule registry is incoherent: "
            f"missing checks={sorted(declared - registered)}, "
            f"unexpected checks={sorted(registered - declared)}"
        )
    for rule in RUNTIME_ELIGIBILITY_RULES:
        if rule.rejection_code not in REJECTION_CODES:
            raise RuntimeEligibilityPolicyError(
                f"rule {rule.code!r} declares an undeclared rejection code"
            )


_assert_rule_registry_is_complete()


# =============================================================================
# 评估入口
# =============================================================================

def evaluate_runtime_mapping_eligibility(
    proposal: MappingProposal | None,
    approval: Mapping[str, Any] | None,
    source_observation: SourceObservation | None,
    current_runtime_context: RuntimeMappingContext,
    *,
    evaluated_at: str | None = None,
) -> RuntimeMappingEligibilityDecision:
    """
    **纯只读**运行期资格判定。

    绝不：修改状态、创建绑定、写文件、修改 Canonical 记录、消费治理账本做写入、
    激活任何映射。判定完全由 ``RUNTIME_ELIGIBILITY_RULES`` 驱动。
    """
    if not isinstance(current_runtime_context, RuntimeMappingContext):
        raise RuntimeEligibilityPolicyError(
            "current_runtime_context must be a RuntimeMappingContext"
        )

    facts = _EvaluationFacts(
        proposal=proposal,
        approval=approval,
        observation=source_observation,
        context=current_runtime_context,
        evaluated_at=evaluated_at,
    )

    rejection: list[str] = []
    for rule in RUNTIME_ELIGIBILITY_RULES:
        codes = _RULE_CHECKS[rule.code](facts)
        for code in codes:
            if code not in REJECTION_CODES:
                raise RuntimeEligibilityPolicyError(
                    f"rule {rule.code!r} produced an undeclared rejection code {code!r}"
                )
            rejection.append(code)

    ordered = tuple(sorted(set(rejection)))
    result = (
        RuntimeEligibilityResult.ELIGIBLE.value
        if not ordered
        else RuntimeEligibilityResult.NOT_ELIGIBLE.value
    )

    payload = {
        "schema_version": RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
        "evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "proposal_id": facts.proposal.proposal_id if isinstance(
            facts.proposal, MappingProposal
        ) else "",
        "source_identity_key": canonical_json(facts.identity_key) if facts.identity_key else "",
        "canonical_id": facts.canonical_id,
        "eligibility_result": result,
        "rejection_codes": list(ordered),
        "evidence_fingerprint": facts.observation_fingerprint,
        "approval_fingerprint": facts.approval_fp,
        "baseline_fingerprint": current_runtime_context.current_baseline_fingerprint,
        "evidence_provenance": current_runtime_context.evidence_provenance.source,
    }

    return RuntimeMappingEligibilityDecision(
        schema_version=RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
        decision_id=fingerprint(payload),
        proposal_id=payload["proposal_id"],
        source_identity_key=payload["source_identity_key"],
        canonical_id=payload["canonical_id"],
        eligibility_result=result,
        rejection_codes=ordered,
        evaluated_at=evaluated_at or _utc_now(),
        evaluator_version=RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        evidence_fingerprint=facts.observation_fingerprint,
        approval_fingerprint=facts.approval_fp,
        baseline_fingerprint=current_runtime_context.current_baseline_fingerprint,
        evidence_provenance=current_runtime_context.evidence_provenance.source,
    )


# =============================================================================
# 运行期就绪清单（只读分析工件）
# =============================================================================

def build_runtime_readiness_manifest(
    *,
    fixture_identity: str,
    decisions: Sequence[RuntimeMappingEligibilityDecision],
    dataset_identity: str = "",
    current_baseline_fingerprint: str = "",
    identity_key_schema_versions: Sequence[str] = (IDENTITY_KEY_SCHEMA_VERSION,),
    approval_versions: Sequence[str] = (),
) -> dict:
    """
    汇总一组资格判定为**只读**运行期就绪清单。

    清单**不能执行**：它带 ``executable: False``，不含任何执行入口，并且任何试图把它
    当作激活计划的尝试都会被 ``assert_runtime_manifest_not_executable`` 拒绝。
    """
    ordered = sorted(decisions, key=lambda d: d.decision_id)
    eligible = [d for d in ordered if d.is_eligible]
    rejected = [d for d in ordered if not d.is_eligible]

    reason_histogram: dict[str, int] = {}
    for decision in rejected:
        for code in decision.rejection_codes:
            reason_histogram[code] = reason_histogram.get(code, 0) + 1

    provenance_histogram: dict[str, int] = {}
    for decision in ordered:
        provenance_histogram[decision.evidence_provenance] = (
            provenance_histogram.get(decision.evidence_provenance, 0) + 1
        )
    conditional = [d for d in ordered if d.is_conditional]

    manifest = {
        "kind": RUNTIME_READINESS_MANIFEST_KIND,
        "executable": False,
        "activation_api": None,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "production_mapping_write_authorization": (
            PRODUCTION_MAPPING_WRITE_AUTHORIZATION
        ),
        "production_authorization_granted": False,
        "manifest_schema_version": RUNTIME_READINESS_MANIFEST_SCHEMA_VERSION,
        "policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "decision_schema_version": RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
        "fixture_identity": fixture_identity,
        "dataset_identity": dataset_identity,
        "baseline_fingerprint": current_baseline_fingerprint,
        "evaluated_proposal_count": len(ordered),
        "eligible_count": len(eligible),
        "rejected_count": len(rejected),
        "conditional_decision_count": len(conditional),
        "verified_evidence_decision_count": len(ordered) - len(conditional),
        "evidence_provenance_histogram": {
            source: provenance_histogram[source]
            for source in sorted(provenance_histogram)
        },
        "conditional_decisions_are_production_authorization": False,
        "rejection_reason_histogram": {
            code: reason_histogram[code] for code in sorted(reason_histogram)
        },
        "identity_key_schema_versions": sorted({str(v) for v in identity_key_schema_versions}),
        "approval_versions": sorted({str(v) for v in approval_versions}),
        "decisions": [d.as_dict() for d in ordered],
        "decision_ids": [d.decision_id for d in ordered],
        "read_only_stores": list(RUNTIME_READ_ONLY_STORES),
        "forbidden_operations": list(FORBIDDEN_RUNTIME_MANIFEST_KEYS),
        "non_executable_marker": (
            "This manifest is a read-only readiness analysis. It activates nothing, "
            "contains no runtime binding operation, and grants no production "
            "authorization. An ELIGIBLE conditional decision is not permanent "
            "authorization: re-evaluation is required after any material governance or "
            "Canonical baseline change."
        ),
    }
    assert_runtime_manifest_not_executable(manifest)
    return manifest


def assert_runtime_manifest_not_executable(manifest: Mapping[str, Any]) -> None:
    """
    断言运行期就绪清单**不含任何激活 / 执行路径**（fail closed）。
    """
    if manifest.get("executable") is not False:
        raise RuntimeEligibilityPolicyError(
            "a runtime readiness manifest must never be executable"
        )
    if manifest.get("kind") != RUNTIME_READINESS_MANIFEST_KIND:
        raise RuntimeEligibilityPolicyError(
            "a runtime readiness manifest must declare its analysis-only kind"
        )
    if manifest.get("activation_api") is not None:
        raise RuntimeEligibilityPolicyError(
            "a runtime readiness manifest must not expose an activation API"
        )
    lowered = {str(key).lower() for key in manifest}
    offenders = sorted(lowered & {k.lower() for k in FORBIDDEN_RUNTIME_MANIFEST_KEYS})
    if offenders:
        raise RuntimeEligibilityPolicyError(
            f"a runtime readiness manifest must not contain executable keys: {offenders}"
        )
    for key, value in manifest.items():
        if callable(value):
            raise RuntimeEligibilityPolicyError(
                f"manifest key {key!r} exposes a callable activation path"
            )


def runtime_manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """就绪清单的确定性指纹（只读；本函数不激活、不签名、不批准）。"""
    return fingerprint(dict(manifest))


def runtime_eligibility_status() -> dict:
    """评估器与激活状态的只读视图（供审计与报告）。"""
    return {
        "runtime_eligibility_evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "decision_schema_version": RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
        "runtime_mapping_policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "production_mapping_write_authorization": (
            PRODUCTION_MAPPING_WRITE_AUTHORIZATION
        ),
        "production_authorization_granted": False,
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "rule_count": len(RUNTIME_ELIGIBILITY_RULES),
        "rejection_code_count": len(REJECTION_CODES),
        "allowed_context_evidence_provenance": sorted(
            ALLOWED_CONTEXT_EVIDENCE_PROVENANCE
        ),
        "blocked_context_evidence_provenance": sorted(
            BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
        ),
        "known_proposal_states": sorted(KNOWN_PROPOSAL_STATES),
        "blocked_provider_id_uniqueness_scopes": sorted(
            BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES
        ),
        "allowed_provider_id_uniqueness_scopes": sorted(
            ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES
        ),
        "conditional_decision_source": CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        "isolated_snapshot_decision_source": (
            CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
        ),
        "verified_evidence_source_reachable": False,
        "writes_files": False,
        "creates_bindings": False,
        "modifies_canonical_records": False,
    }
