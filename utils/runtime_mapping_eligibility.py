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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    GovernanceError,
    MappingProposal,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    StaleBaselineError,
    approval_fingerprint,
    verify_mapping_approval,
)
from utils.runtime_mapping_policy import (
    ALLOWED_ACTIONS,
    ALLOWED_APPROVAL_PURPOSES,
    ALLOWED_APPROVAL_VERSIONS,
    ALLOWED_IDENTITY_KEY_VERSIONS,
    ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES,
    ALLOWED_PROPOSAL_STATES,
    ALLOWED_RUNTIME_IDENTITY_KEYS,
    ALLOWED_SOURCE_SCOPES,
    BLOCKED_ACTIONS,
    BLOCKED_IDENTITY_TYPES,
    BLOCKED_IDENTITY_STABILITIES,
    BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES,
    BLOCKED_RAW_IDENTIFIER_TYPES,
    EVALUATION_STORES,
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
    SourceObservation,
    canonical_json,
    fingerprint,
    identity_kind_is_sufficient,
)

#: 评估器版本。任何影响判定结果的变化都必须提升该版本。
RUNTIME_ELIGIBILITY_EVALUATOR_VERSION = "1.0.0"

#: 决策对象 schema 版本。
RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION = "1.0.0"

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
class RuntimeMappingContext:
    """
    评估所需的**全部**运行期事实（不可变）。

    刻意把「运行期知道什么」显式化，使评估成为纯函数：同样的上下文 + 同样的输入
    必然得到同样的判定。本类型不持有任何写入能力。
    """

    dataset_identity: str
    current_baseline_fingerprint: str
    #: 运行期已知的 Canonical ID 集合。
    canonical_ids: tuple[str, ...] = ()
    #: 显式可信的所有者公钥路径（**必须**由调用方提供；内嵌公钥永不作为信任锚）。
    trusted_public_key_path: str | None = None
    #: 运行期期望的批准人 / 操作者身份。
    expected_approver: str | None = None
    expected_operator: str | None = None
    #: 治理账本中记录的提案指纹与当前状态（只读快照）。
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
            "canonical_ids": sorted(self.canonical_id_set()),
            "trust_anchor_supplied": self.trusted_public_key_path is not None,
            "expected_approver": self.expected_approver,
            "expected_operator": self.expected_operator,
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

    # -- 视图 -------------------------------------------------------------

    @property
    def is_eligible(self) -> bool:
        return self.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value

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
        }

    def as_dict(self) -> dict:
        payload = self.identity_payload()
        payload["decision_id"] = self.decision_id
        payload["evaluated_at"] = self.evaluated_at
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
        "approval_fp",
        "proposal_fingerprint",
        "canonical_id",
        "source_scope",
        "canonical_change_required",
        "rewrite_targets",
        "rewrite_effects",
        "approval_verification_error",
    )

    def __init__(
        self,
        *,
        proposal: MappingProposal | None,
        approval: Mapping[str, Any] | None,
        observation: SourceObservation | None,
        context: RuntimeMappingContext,
    ) -> None:
        self.proposal = proposal
        self.approval = approval
        self.observation = observation
        self.context = context

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


def _check_proposal_fingerprint_matches(facts: _EvaluationFacts) -> tuple[str, ...]:
    recorded = facts.context.recorded_proposal_fingerprint
    if recorded is None:
        return ()
    if recorded != facts.proposal_fingerprint:
        return ("PROPOSAL_FINGERPRINT_MISMATCH",)
    return ()


def _check_proposal_state_active(facts: _EvaluationFacts) -> tuple[str, ...]:
    state = facts.context.recorded_proposal_state
    if state is None:
        state = facts.proposal.status if isinstance(facts.proposal, MappingProposal) else ""
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
    "PROPOSAL_FINGERPRINT_MATCHES": _check_proposal_fingerprint_matches,
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

    manifest = {
        "kind": RUNTIME_READINESS_MANIFEST_KIND,
        "executable": False,
        "activation_api": None,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "production_mapping_write_authorization": (
            PRODUCTION_MAPPING_WRITE_AUTHORIZATION
        ),
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
            "This manifest is a read-only readiness analysis. It activates nothing and "
            "contains no runtime binding operation."
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
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "rule_count": len(RUNTIME_ELIGIBILITY_RULES),
        "rejection_code_count": len(REJECTION_CODES),
        "blocked_provider_id_uniqueness_scopes": sorted(
            BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES
        ),
        "allowed_provider_id_uniqueness_scopes": sorted(
            ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES
        ),
        "writes_files": False,
        "creates_bindings": False,
        "modifies_canonical_records": False,
    }
