"""
Canonical 映射治理（Canonical Mapping Governance）
================================================
把「提案」「审批」「账本」三层信任边界显式建模，并且**只提供治理能力，不提供
任何 Canonical 数据写入路径**。

核心不变量
----------
1. **源观测不是绑定或改写 Canonical 身份的依据。**
2. **提案不是已批准映射。** ``PROPOSED`` / ``REVIEW_REQUIRED`` 都不授权任何变更。
3. **已批准映射不是改写历史 Canonical 数据的许可。** 本模块不执行任何迁移。
4. 本模块**不**被 ``utils/scraper.py`` 引用；它既不是运行期权威，也不是额外的
   生产写入路径。运行期身份权威仍是 ``utils.ingestion_identity`` 的摄取预检。

信任边界分层
------------
====================  ==========================================================
operator              准备提案的人（``actor_role = "operator"``）
owner                 持有仓库外私钥、可独立验证的批准人（``actor_role = "owner"``）
approval artifact     与提案 / 观测指纹 / Canonical ID / 动作 / 数据集身份绑定的
                      Ed25519 分离签名；**绝不**复用生产修复授权件
ledger                隔离的 append-only 哈希链账本；哈希链只证明**内部一致性**，
                      在没有独立可信签名/检查点之前，不得称为「防篡改」
====================  ==========================================================

生产授权状态
------------
``PRODUCTION_MAPPING_WRITE_AUTHORIZATION = NOT_GRANTED``
``RUNTIME_LEDGER_ENFORCEMENT = NOT_ENABLED``

本模块产出的任何治理工件都**不得**被误认为已授权的生产迁移。
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    LEGACY_IDENTITY_KEY_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION,
    SUFFICIENT_IDENTITY_KEY_KINDS,
    IdentityKeyKind,
    ScopedSourceIdentityKey,
    SourceObservation,
    canonical_json,
    fingerprint,
)

#: 治理 schema 版本。
GOVERNANCE_SCHEMA_VERSION = "1.0.0"

#: 生产映射写入授权状态：本阶段**未授权**。
PRODUCTION_MAPPING_WRITE_AUTHORIZATION = "NOT_GRANTED"

#: 运行期账本强制状态：本阶段**未启用**。
RUNTIME_LEDGER_ENFORCEMENT = "NOT_ENABLED"

#: 账本创世前驱哈希（64 个 0）。
GENESIS_PREVIOUS_HASH = "0" * 64

#: 账本文件名（仅作为**显式**工作区下的默认文件名，绝不默认指向生产 data/）。
LEDGER_FILENAME = "mapping_governance_ledger.jsonl"

#: 仓库根目录（用于拒绝把治理工件写进仓库）。
REPO_ROOT = os.path.realpath(
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
)


# =============================================================================
# 异常
# =============================================================================

class GovernanceError(Exception):
    """治理层错误基类。"""


class ProposalValidationError(GovernanceError):
    """提案结构或取值不合法。"""


class InvalidTransitionError(GovernanceError):
    """请求的状态迁移不在权威迁移表中。"""


class ConflictingBindingError(GovernanceError):
    """同一源身份出现冲突的**活动**绑定。"""


class LedgerIntegrityError(GovernanceError):
    """账本哈希链断裂、条目损坏或无法解析。"""


class LedgerReplayError(GovernanceError):
    """检测到重放：同一条决策被以不同载荷重复提交。"""


class LedgerPathError(GovernanceError):
    """账本路径不在授权的隔离工作区内。"""


class SignatureVerificationError(GovernanceError):
    """批准件缺失、未签名、被篡改、密钥不可信或无法验证。"""


class StaleBaselineError(GovernanceError):
    """基线指纹在提案之后发生变化；批准不可复用于新基线。"""


class LegacyBindingKeyError(GovernanceError):
    """
    遇到旧契约的绑定键（仅由作用域元数据构成、无判别性身份证据）。

    旧键**不得**被静默重新解释为新的身份键含义，也不得据以授权活动绑定。
    """


class MissingApprovalEvidenceError(GovernanceError):
    """
    APPROVED 记录缺少可独立复核的批准证据（签名工件或不可变提案证据）。

    fail closed：缺少证据时绝不声称已完成密码学批准核验。
    """


class ActorBindingError(GovernanceError):
    """账本主体身份与已签名批准件中的主体身份不一致。"""


# =============================================================================
# 绑定键契约兼容性
# =============================================================================

#: 绑定键契约分类。
BINDING_KEY_CURRENT = "CURRENT"
BINDING_KEY_LEGACY_SCOPE_ONLY = "LEGACY_SCOPE_ONLY"
BINDING_KEY_LEGACY_VERSION = "LEGACY_VERSION"
BINDING_KEY_UNKNOWN = "UNKNOWN"

#: 旧契约（仅作用域元数据）的字段集合。
_LEGACY_SCOPE_FIELDS = frozenset(
    {"adapter", "sport", "event_date", "provider_namespace", "raw_identifier_type"}
)


def classify_binding_key(key: object) -> str:
    """
    判定一条已记录绑定键所属的契约。

    - ``CURRENT``：带 ``identity_key_schema_version`` 且版本等于当前契约。
    - ``LEGACY_VERSION``：带版本标记但版本不是当前契约。
    - ``LEGACY_SCOPE_ONLY``：旧契约 —— 只有作用域元数据，没有判别性身份证据。
    - ``UNKNOWN``：无法解析。

    **绝不**把旧键静默重新解释为当前身份键。
    """
    if not isinstance(key, str) or not key.strip():
        return BINDING_KEY_UNKNOWN
    try:
        payload = json.loads(key)
    except (ValueError, TypeError):
        return BINDING_KEY_UNKNOWN
    if not isinstance(payload, dict):
        return BINDING_KEY_UNKNOWN

    version = payload.get("identity_key_schema_version")
    if version is not None:
        return (
            BINDING_KEY_CURRENT
            if version == IDENTITY_KEY_SCHEMA_VERSION
            else BINDING_KEY_LEGACY_VERSION
        )
    if _LEGACY_SCOPE_FIELDS.issubset(set(payload)):
        return BINDING_KEY_LEGACY_SCOPE_ONLY
    return BINDING_KEY_UNKNOWN


def binding_key_is_current(key: object) -> bool:
    return classify_binding_key(key) == BINDING_KEY_CURRENT


# =============================================================================
# 状态机（权威迁移表）
# =============================================================================

class MappingState(str, Enum):
    """映射治理生命周期状态。"""

    OBSERVED = "OBSERVED"
    PROPOSED = "PROPOSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"


#: **唯一权威**的允许迁移表。
#:
#: - ``OBSERVED`` 不等于 ``PROPOSED``（必须显式提案）。
#: - ``PROPOSED`` 不等于 ``APPROVED``（必须显式所有者批准）。
#: - ``REVIEW_REQUIRED`` 不授权任何变更。
#: - ``REJECTED`` **不能**变成 ``APPROVED``（不得通过编辑同一对象翻案）。
#: - ``REVOKED`` 不得再处于活动状态。
#: - ``SUPERSEDED`` 为终态，历史证据保留。
#:
#: 刻意**没有**通用的 force 迁移入口。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    MappingState.OBSERVED.value: frozenset(
        {MappingState.PROPOSED.value, MappingState.REJECTED.value}
    ),
    MappingState.PROPOSED.value: frozenset(
        {
            MappingState.REVIEW_REQUIRED.value,
            MappingState.APPROVED.value,
            MappingState.REJECTED.value,
        }
    ),
    MappingState.REVIEW_REQUIRED.value: frozenset(
        {MappingState.APPROVED.value, MappingState.REJECTED.value}
    ),
    MappingState.APPROVED.value: frozenset(
        {MappingState.REVOKED.value, MappingState.SUPERSEDED.value}
    ),
    MappingState.REJECTED.value: frozenset({MappingState.SUPERSEDED.value}),
    MappingState.REVOKED.value: frozenset({MappingState.SUPERSEDED.value}),
    MappingState.SUPERSEDED.value: frozenset(),
}

#: 表示「活动（可生效）」的终态集合。仅 ``APPROVED``。
ACTIVE_STATES: frozenset[str] = frozenset({MappingState.APPROVED.value})

#: 需要所有者批准才能进入的状态。
OWNER_APPROVAL_REQUIRED_STATES: frozenset[str] = frozenset(
    {MappingState.APPROVED.value}
)


def validate_transition(from_state: str, to_state: str) -> None:
    """
    校验一次状态迁移。非法迁移一律拒绝（fail closed），无强制绕过参数。
    """
    if from_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"Unknown source state '{from_state}'")
    if to_state not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"Unknown target state '{to_state}'")
    if to_state not in ALLOWED_TRANSITIONS[from_state]:
        raise InvalidTransitionError(
            f"Transition {from_state} -> {to_state} is not permitted by the "
            "authoritative transition registry"
        )


def is_terminal(state: str) -> bool:
    return not ALLOWED_TRANSITIONS.get(state, frozenset())


def reachable_states(from_state: str) -> tuple[str, ...]:
    """从某状态出发可直接到达的状态（只读，供审计）。"""
    return tuple(sorted(ALLOWED_TRANSITIONS.get(from_state, frozenset())))


# =============================================================================
# 提案契约
# =============================================================================

class ProposalAction(str, Enum):
    """允许的提案动作。提案**不执行**这些动作。"""

    BIND_SOURCE_TO_EXISTING_CANONICAL = "BIND_SOURCE_TO_EXISTING_CANONICAL"
    REGISTER_NEW_SOURCE_IDENTITY = "REGISTER_NEW_SOURCE_IDENTITY"
    REJECT_IDENTITY_CLAIM = "REJECT_IDENTITY_CLAIM"
    FLAG_AMBIGUOUS_IDENTITY = "FLAG_AMBIGUOUS_IDENTITY"
    NO_ACTION = "NO_ACTION"


#: 需要绑定到具体 Canonical ID 的动作。
BINDING_ACTIONS: frozenset[str] = frozenset(
    {ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value}
)

#: 刻意**不存在**任何「改写既有 Canonical ID」的动作。
FORBIDDEN_ACTION_TOKENS: tuple[str, ...] = (
    "REWRITE",
    "RENAME",
    "MIGRATE",
    "DELETE",
    "MERGE_CANONICAL",
)

PROPOSAL_SCHEMA_VERSION = "2.0.0"

#: 提案必填字段（schema 契约）。
PROPOSAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "proposal_schema_version",
    "proposal_id",
    "source_observation_ref",
    "source_identity_scope",
    "scoped_source_identity_key",
    "candidate_canonical_id",
    "existing_canonical_owner",
    "identity_evidence_type",
    "observation_fingerprints",
    "ingestion_plan_reference",
    "reason",
    "conflict_category",
    "expected_baseline_fingerprint",
    "requested_action",
    "status",
)


@dataclass(frozen=True)
class MappingProposal:
    """
    一条映射提案（不可变）。

    ``status`` 默认 ``PROPOSED``；``OBSERVED`` 只表示「已观测」，绝不等于已提案，
    更不等于已批准。

    绑定身份由 ``scoped_source_identity_key`` 承载 —— 宽泛的 ``source_identity_scope``
    只描述**域**，绝不单独构成绑定键。
    """

    proposal_schema_version: str
    proposal_id: str
    source_observation_ref: str
    source_identity_scope: tuple[tuple[str, str], ...]
    scoped_source_identity_key: tuple[tuple[str, str], ...]
    candidate_canonical_id: str | None
    existing_canonical_owner: str | None
    identity_evidence_type: str
    observation_fingerprints: tuple[str, ...]
    ingestion_plan_reference: str
    reason: str
    conflict_category: str | None
    expected_baseline_fingerprint: str
    requested_action: str
    status: str
    operator: str = ""

    # -- 视图 -------------------------------------------------------------

    @property
    def scope_dict(self) -> dict[str, str]:
        return dict(self.source_identity_scope)

    @property
    def identity_key_dict(self) -> dict[str, str]:
        return dict(self.scoped_source_identity_key)

    @property
    def identity_key_kind(self) -> str:
        return self.identity_key_dict.get("identity_kind", "")

    @property
    def identity_key_value(self) -> str:
        return self.identity_key_dict.get("identity_value", "")

    @property
    def identity_key_sufficient(self) -> bool:
        """绑定键是否承载足以授权活动绑定的判别性身份证据。"""
        return self.identity_key_kind in SUFFICIENT_IDENTITY_KEY_KINDS

    @property
    def identity_key_fingerprint(self) -> str:
        return fingerprint(self.identity_key_dict)

    @property
    def source_key(self) -> str:
        """
        **绑定身份键**：由判别性身份证据构成，用于活动绑定冲突检测。

        绝不是「仅作用域元数据」。
        """
        return canonical_json(self.identity_key_dict)

    @property
    def is_binding_action(self) -> bool:
        return self.requested_action in BINDING_ACTIONS

    @property
    def requires_owner_approval(self) -> bool:
        return self.status in OWNER_APPROVAL_REQUIRED_STATES or self.is_binding_action

    # -- 指纹 -------------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "proposal_schema_version": self.proposal_schema_version,
            "proposal_id": self.proposal_id,
            "source_observation_ref": self.source_observation_ref,
            "source_identity_scope": self.scope_dict,
            "scoped_source_identity_key": self.identity_key_dict,
            "candidate_canonical_id": self.candidate_canonical_id,
            "existing_canonical_owner": self.existing_canonical_owner,
            "identity_evidence_type": self.identity_evidence_type,
            "observation_fingerprints": list(self.observation_fingerprints),
            "ingestion_plan_reference": self.ingestion_plan_reference,
            "reason": self.reason,
            "conflict_category": self.conflict_category,
            "expected_baseline_fingerprint": self.expected_baseline_fingerprint,
            "requested_action": self.requested_action,
            "status": self.status,
            "operator": self.operator,
        }

    def fingerprint(self) -> str:
        """提案指纹（含 status，用于账本绑定）。"""
        return fingerprint(self.as_dict())

    def content_fingerprint(self) -> str:
        """
        提案**内容**指纹：刻意排除 ``status``。

        提案内容不变时，状态流转不改变内容指纹；因此批准件可以绑定内容而
        不必绑定某个瞬时状态。
        """
        payload = self.as_dict()
        payload.pop("status", None)
        return fingerprint(payload)

    def evidence_payload(self) -> dict:
        """
        不可变提案证据（供历史批准独立复核）。

        只包含重建与复核该提案所需的字段；不含任何私钥或瞬时状态。
        """
        payload = self.as_dict()
        payload.pop("status", None)
        return payload


def build_proposal(
    *,
    source_observation_ref: str,
    source_identity_scope: Mapping[str, Any],
    candidate_canonical_id: str | None,
    existing_canonical_owner: str | None,
    identity_evidence_type: str,
    observation_fingerprints: Iterable[str],
    ingestion_plan_reference: str,
    reason: str,
    expected_baseline_fingerprint: str,
    requested_action: str,
    identity_key: ScopedSourceIdentityKey | None = None,
    scoped_source_identity_key: Mapping[str, Any] | None = None,
    conflict_category: str | None = None,
    status: str = MappingState.PROPOSED.value,
    operator: str = "",
) -> MappingProposal:
    """
    构造一条提案。``proposal_id`` 由**内容**确定性派生（与输入顺序无关）。

    ``identity_key`` 是**绑定身份**的权威来源；只给出 ``scoped_source_identity_key``
    字典（例如从账本证据重建时）同样接受。绑定动作必须携带**充分**的身份键。
    """
    if identity_key is not None:
        key_dict = identity_key.as_dict()
        scope_dict = identity_key.scope.as_dict()
    elif scoped_source_identity_key is not None:
        key_dict = {str(k): str(v) for k, v in dict(scoped_source_identity_key).items()}
        scope_dict = {str(k): str(v) for k, v in dict(source_identity_scope).items()}
    else:
        raise ProposalValidationError(
            "A scoped source identity key is required; broad scope metadata alone "
            "never constitutes a binding identity"
        )

    scope_pairs = tuple(sorted((str(k), str(v)) for k, v in scope_dict.items()))
    key_pairs = tuple(sorted(key_dict.items()))
    proposal_id = fingerprint(
        {
            "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
            "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
            "source_observation_ref": source_observation_ref,
            "source_identity_scope": dict(scope_pairs),
            "scoped_source_identity_key": dict(key_pairs),
            "candidate_canonical_id": candidate_canonical_id,
            "existing_canonical_owner": existing_canonical_owner,
            "identity_evidence_type": identity_evidence_type,
            "observation_fingerprints": sorted(str(f) for f in observation_fingerprints),
            "ingestion_plan_reference": ingestion_plan_reference,
            "reason": reason,
            "conflict_category": conflict_category,
            "expected_baseline_fingerprint": expected_baseline_fingerprint,
            "requested_action": requested_action,
        }
    )
    proposal = MappingProposal(
        proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        proposal_id=proposal_id,
        source_observation_ref=source_observation_ref,
        source_identity_scope=scope_pairs,
        scoped_source_identity_key=key_pairs,
        candidate_canonical_id=candidate_canonical_id,
        existing_canonical_owner=existing_canonical_owner,
        identity_evidence_type=identity_evidence_type,
        observation_fingerprints=tuple(sorted(str(f) for f in observation_fingerprints)),
        ingestion_plan_reference=ingestion_plan_reference,
        reason=reason,
        conflict_category=conflict_category,
        expected_baseline_fingerprint=expected_baseline_fingerprint,
        requested_action=requested_action,
        status=status,
        operator=operator,
    )
    validate_proposal(proposal)
    return proposal


def validate_proposal(proposal: MappingProposal) -> None:
    """
    校验提案契约。

    拒绝：未知动作、任何「改写既有 Canonical ID」的动作、绑定动作缺少目标
    Canonical ID、缺少必填证据、以及试图直接以 APPROVED 状态构造提案。
    """
    if not isinstance(proposal, MappingProposal):
        raise ProposalValidationError("proposal must be a MappingProposal")

    if proposal.proposal_schema_version != PROPOSAL_SCHEMA_VERSION:
        raise ProposalValidationError(
            f"proposal_schema_version must be '{PROPOSAL_SCHEMA_VERSION}'"
        )

    action = proposal.requested_action
    known_actions = {a.value for a in ProposalAction}
    if action not in known_actions:
        raise ProposalValidationError(f"Unknown requested_action '{action}'")

    upper = action.upper()
    for token in FORBIDDEN_ACTION_TOKENS:
        if token in upper:
            raise ProposalValidationError(
                f"requested_action '{action}' would rewrite Canonical identity; "
                "no such action is authorized"
            )

    if action in BINDING_ACTIONS:
        if not proposal.candidate_canonical_id:
            raise ProposalValidationError(
                "BIND_SOURCE_TO_EXISTING_CANONICAL requires a candidate_canonical_id"
            )
        if not proposal.existing_canonical_owner:
            raise ProposalValidationError(
                "BIND_SOURCE_TO_EXISTING_CANONICAL requires an existing_canonical_owner"
            )
        # 绑定身份必须由**判别性**身份证据支撑：行位置派生、含义不可确认或缺号
        # 的观测只能被观测 / 复核，绝不能授权活动绑定。
        if not proposal.identity_key_sufficient:
            raise ProposalValidationError(
                "A binding action requires sufficient scoped source identity evidence; "
                f"identity_kind='{proposal.identity_key_kind}' is not sufficient "
                "(ambiguous, unstable or position-derived evidence cannot authorize an "
                "active binding)"
            )

    if not proposal.source_observation_ref:
        raise ProposalValidationError("source_observation_ref is required")
    if not proposal.observation_fingerprints:
        raise ProposalValidationError("at least one observation fingerprint is required")
    if not proposal.ingestion_plan_reference:
        raise ProposalValidationError("ingestion_plan_reference is required")
    if not proposal.expected_baseline_fingerprint:
        raise ProposalValidationError("expected_baseline_fingerprint is required")
    if not proposal.source_identity_scope:
        raise ProposalValidationError("source_identity_scope is required")
    if not proposal.scoped_source_identity_key:
        raise ProposalValidationError("scoped_source_identity_key is required")
    if not proposal.identity_key_kind or not proposal.identity_key_value:
        raise ProposalValidationError(
            "scoped_source_identity_key must carry an identity_kind and identity_value"
        )
    if not proposal.reason:
        raise ProposalValidationError("reason is required")

    if proposal.status == MappingState.APPROVED.value:
        raise ProposalValidationError(
            "A proposal may never be constructed directly in APPROVED state; "
            "APPROVED is reachable only through a verified owner approval"
        )
    if proposal.status not in ALLOWED_TRANSITIONS:
        raise ProposalValidationError(f"Unknown proposal status '{proposal.status}'")


def proposal_from_observations(
    observations: Sequence[SourceObservation],
    *,
    requested_action: str,
    candidate_canonical_id: str | None,
    existing_canonical_owner: str | None,
    ingestion_plan_reference: str,
    expected_baseline_fingerprint: str,
    reason: str,
    identity_evidence_type: str,
    identity_key: ScopedSourceIdentityKey | None = None,
    conflict_category: str | None = None,
    operator: str = "",
    status: str = MappingState.PROPOSED.value,
) -> MappingProposal:
    """
    由一组源观测构造提案（只读；不修改观测）。

    ``identity_key`` 未显式给出时，由**首条**观测推导。同一提案的观测必须共享同一
    身份键（否则请分别提案）—— 这正是「不同赛事不得共用同一绑定身份」的保障。
    """
    if not observations:
        raise ProposalValidationError("at least one observation is required")

    keys = [
        (identity_key if identity_key is not None else ScopedSourceIdentityKey.for_observation(o))
        for o in observations
    ]
    key_signatures = {k.source_key() for k in keys}
    if len(key_signatures) > 1:
        raise ProposalValidationError(
            "observations in one proposal must resolve to a single scoped source "
            "identity key; distinct events must be proposed separately"
        )
    resolved_key = keys[0]

    return build_proposal(
        source_observation_ref=observations[0].observation_id,
        source_identity_scope=resolved_key.scope.as_dict(),
        candidate_canonical_id=candidate_canonical_id,
        existing_canonical_owner=existing_canonical_owner,
        identity_evidence_type=identity_evidence_type,
        observation_fingerprints=[
            o.semantic_fingerprint() for o in observations
        ],
        ingestion_plan_reference=ingestion_plan_reference,
        reason=reason,
        expected_baseline_fingerprint=expected_baseline_fingerprint,
        requested_action=requested_action,
        identity_key=resolved_key,
        conflict_category=conflict_category,
        status=status,
        operator=operator,
    )


# =============================================================================
# 所有者批准信任边界（Ed25519）
# =============================================================================

MAPPING_APPROVAL_SCHEMA_VERSION = "2.0.0"

#: 域分离用途串。刻意与生产修复授权（``PRODUCTION_CANONICAL_DEDUP_APPLY``）
#: 完全不同，因此生产修复的授权件**永远无法**被当作映射批准件使用。
MAPPING_APPROVAL_PURPOSE = "CANONICAL_MAPPING_GOVERNANCE_APPROVE"

#: 建立签名机制：RFC 8032 Ed25519（PyCA ``cryptography``）。不自实现密码学。
APPROVAL_ALGORITHM = "ed25519"
APPROVAL_FIELD = "approval"
APPROVAL_PUBLIC_KEY_BYTES = 32
APPROVAL_SIGNATURE_BYTES = 64

#: 批准件必须绑定的字段。
#:
#: ``scoped_source_identity_key`` 是本次修正新增的绑定项：批准件必须绑定**判别性
#: 身份证据**，而不只是宽泛的作用域元数据。因此批准契约版本提升为 ``2.0.0``，
#: 旧版本批准件一律按不兼容契约拒绝（绝不静默重新解释、绝不静默迁移）。
APPROVAL_BOUND_FIELDS: tuple[str, ...] = (
    "proposal_id",
    "proposal_content_fingerprint",
    "source_observation_fingerprint",
    "source_identity_scope",
    "scoped_source_identity_key",
    "candidate_canonical_id",
    "requested_action",
    "dataset_identity",
    "expected_baseline_fingerprint",
    "operator",
    "approver",
    "approved_at",
    "reason",
    "purpose",
)


def _crypto_backend() -> dict:
    """导入既有 Ed25519 实现，否则 fail closed（不自实现密码学）。"""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(
            "Ed25519 backend ('cryptography') is unavailable; refusing to accept any "
            "mapping approval."
        ) from exc
    return {
        "Ed25519PrivateKey": Ed25519PrivateKey,
        "Ed25519PublicKey": Ed25519PublicKey,
        "serialization": serialization,
    }


def public_key_id(raw_public_key: bytes) -> str:
    """可信公钥的稳定标识：``sha256:<hex>``。"""
    import hashlib

    return "sha256:" + hashlib.sha256(raw_public_key).hexdigest()


def approval_signing_payload(approval: Mapping[str, Any]) -> bytes:
    """
    批准签名的确定性规范载荷：批准件**去掉** ``approval`` 块后的规范 JSON。

    因此签名绑定**全部**声明字段：用途、提案 ID、提案内容指纹、观测指纹、
    源身份作用域、候选 Canonical ID、请求动作、数据集身份、期望基线指纹、
    操作者身份、批准者身份、批准时间与理由。
    """
    body = {k: v for k, v in approval.items() if k != APPROVAL_FIELD}
    return canonical_json(body).encode("utf-8")


def approval_fingerprint(approval: Mapping[str, Any]) -> str:
    """整件批准件的完整性指纹。"""
    return fingerprint(dict(approval))


def _assert_private_key_path_is_external(path: str) -> str:
    """所有者私钥**绝不**可以位于仓库或应用工作区内。"""
    if not path or not str(path).strip():
        raise SignatureVerificationError("A private signing key path is required")
    norm = os.path.realpath(os.path.abspath(path))
    if norm == REPO_ROOT or norm.startswith(REPO_ROOT + os.sep):
        raise SignatureVerificationError(
            "Refusing to use a mapping owner private key stored inside the repository "
            f"({REPO_ROOT}). Provision the owner key outside the repository and the "
            "application workspace, and share only the public half."
        )
    return norm


def generate_mapping_owner_keypair(private_key_path: str, public_key_path: str) -> dict:
    """
    在仓库**之外**生成一对新的 Ed25519 所有者密钥。

    私钥（PKCS#8）由所有者持有、绝不入库；公钥（SubjectPublicKeyInfo）可分发并
    作为可信公钥固定。
    """
    backend = _crypto_backend()
    serialization = backend["serialization"]

    private_norm = _assert_private_key_path_is_external(private_key_path)
    if not public_key_path or not str(public_key_path).strip():
        raise SignatureVerificationError("A public key output path is required")
    public_norm = os.path.realpath(os.path.abspath(public_key_path))

    private_key = backend["Ed25519PrivateKey"].generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    for target, blob in ((private_norm, private_pem), (public_norm, public_pem)):
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(blob)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass

    raw_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "private_key_path": private_norm,
        "public_key_path": public_norm,
        "public_key_id": public_key_id(raw_public),
        "algorithm": APPROVAL_ALGORITHM,
    }


def load_trusted_public_key(public_key_path: str) -> dict:
    """
    载入可信公钥。不可读、畸形或非 Ed25519 一律 fail closed。
    """
    backend = _crypto_backend()
    serialization = backend["serialization"]
    if not public_key_path or not os.path.exists(public_key_path):
        raise SignatureVerificationError(
            f"Trusted mapping public key not found: {public_key_path!r}"
        )
    with open(public_key_path, "rb") as handle:
        pem = handle.read()
    try:
        key = serialization.load_pem_public_key(pem)
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(
            f"Trusted mapping public key is malformed: {exc}"
        ) from exc
    if not isinstance(key, backend["Ed25519PublicKey"]):
        raise SignatureVerificationError(
            "Trusted mapping public key is not an Ed25519 public key; refusing to trust it"
        )
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {"key": key, "raw": raw, "public_key_id": public_key_id(raw)}


def sign_mapping_approval(
    private_key_path: str,
    approval_body: Mapping[str, Any],
) -> dict:
    """
    用所有者私钥对批准件做分离签名。

    ``approval_body`` 必须已经包含全部绑定字段（``purpose`` 固定为映射用途）。
    """
    backend = _crypto_backend()
    serialization = backend["serialization"]

    private_norm = _assert_private_key_path_is_external(private_key_path)
    with open(private_norm, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, backend["Ed25519PrivateKey"]):
        raise SignatureVerificationError(
            "Mapping owner private key is not an Ed25519 private key"
        )

    body = dict(approval_body)
    body["purpose"] = MAPPING_APPROVAL_PURPOSE
    body["algorithm"] = APPROVAL_ALGORITHM
    missing = [f for f in APPROVAL_BOUND_FIELDS if f not in body]
    if missing:
        raise SignatureVerificationError(
            f"Approval body is missing bound fields: {sorted(missing)}"
        )

    raw_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    body["public_key_id"] = public_key_id(raw_public)
    signature = key.sign(approval_signing_payload(body))
    body[APPROVAL_FIELD] = {
        "algorithm": APPROVAL_ALGORITHM,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return body


def build_approval_body(
    *,
    proposal: MappingProposal,
    source_observation_fingerprint: str,
    dataset_identity: str,
    operator: str,
    approver: str,
    approved_at: str,
    reason: str,
) -> dict:
    """
    组装批准件主体（**未签名**）。

    刻意把 ``operator`` 与 ``approver`` 分开：所有者决策必须可与准备提案的操作者
    区分开。``proposal_content_fingerprint`` 绑定提案内容而非瞬时状态。
    """
    if not approver or not str(approver).strip():
        raise SignatureVerificationError("An approver identity is required")
    if approver == operator:
        raise SignatureVerificationError(
            "The owner approval must be distinguishable from the operator preparing "
            "the proposal; approver and operator cannot be the same identity"
        )
    return {
        "approval_schema_version": MAPPING_APPROVAL_SCHEMA_VERSION,
        "purpose": MAPPING_APPROVAL_PURPOSE,
        "proposal_id": proposal.proposal_id,
        "proposal_content_fingerprint": proposal.content_fingerprint(),
        "source_observation_fingerprint": source_observation_fingerprint,
        "source_identity_scope": proposal.scope_dict,
        "scoped_source_identity_key": proposal.identity_key_dict,
        "candidate_canonical_id": proposal.candidate_canonical_id,
        "requested_action": proposal.requested_action,
        "dataset_identity": dataset_identity,
        "expected_baseline_fingerprint": proposal.expected_baseline_fingerprint,
        "operator": operator,
        "approver": approver,
        "approved_at": approved_at,
        "reason": reason,
    }


def verify_mapping_approval(
    approval: Mapping[str, Any] | None,
    *,
    proposal: MappingProposal,
    source_observation_fingerprint: str,
    dataset_identity: str,
    current_baseline_fingerprint: str,
    trusted_public_key_path: str,
    expected_actor: str | None = None,
    expected_operator: str | None = None,
) -> None:
    """
    独立验证一份所有者批准件。

    拒绝（fail closed）：
    - 缺失 / 未签名 / 空签名的批准尝试；
    - 非映射用途的批准件（例如生产修复授权件）；
    - 旧契约版本的批准件（不兼容，绝不静默迁移）；
    - 任何被篡改的字段（签名校验失败）；
    - 不可信或非 Ed25519 的公钥；
    - 绑定不匹配（提案、内容指纹、观测指纹、作用域、**判别性身份键**、目标
      Canonical ID、动作、数据集）；
    - **主体归属不匹配**：``expected_actor`` 必须等于已签名的 ``approver``；
      ``expected_operator`` 必须等于已签名的 ``operator``；
    - 基线指纹已变化（陈旧批准，必须重新验证）。

    ``expected_actor`` / ``expected_operator`` 由调用方给出（账本主体、提案操作者），
    使「账本记录的主体」与「被签名的身份」无法被拆开。
    """
    if not approval:
        raise SignatureVerificationError(
            "An owner approval artifact is required; unsigned approval attempts are refused"
        )
    if not isinstance(approval, Mapping):
        raise SignatureVerificationError("Approval artifact must be an object")

    if approval.get("approval_schema_version") != MAPPING_APPROVAL_SCHEMA_VERSION:
        raise SignatureVerificationError(
            "Approval artifact schema is not the mapping governance schema "
            f"'{MAPPING_APPROVAL_SCHEMA_VERSION}'; legacy approval contracts are "
            "incompatible and are never silently migrated"
        )

    purpose = approval.get("purpose")
    if purpose != MAPPING_APPROVAL_PURPOSE:
        raise SignatureVerificationError(
            f"Approval purpose '{purpose}' is not '{MAPPING_APPROVAL_PURPOSE}'; "
            "an authorization granted for another class of action can never be "
            "replayed as a mapping approval"
        )

    signature_block = approval.get(APPROVAL_FIELD)
    if not isinstance(signature_block, Mapping):
        raise SignatureVerificationError(
            "Approval artifact carries no signature block; unsigned approvals are refused"
        )
    if signature_block.get("algorithm") != APPROVAL_ALGORITHM:
        raise SignatureVerificationError(
            f"Approval algorithm must be '{APPROVAL_ALGORITHM}'"
        )

    # --- 绑定校验（先于任何签名数学） ------------------------------------
    if approval.get("proposal_id") != proposal.proposal_id:
        raise SignatureVerificationError("Approval does not bind this proposal_id")
    if approval.get("proposal_content_fingerprint") != proposal.content_fingerprint():
        raise SignatureVerificationError(
            "Approval does not bind the proposal content fingerprint"
        )
    if approval.get("source_observation_fingerprint") != source_observation_fingerprint:
        raise SignatureVerificationError(
            "Approval does not bind the source observation fingerprint"
        )
    if dict(approval.get("source_identity_scope") or {}) != proposal.scope_dict:
        raise SignatureVerificationError(
            "Approval does not bind the source identity scope"
        )
    if dict(approval.get("scoped_source_identity_key") or {}) != proposal.identity_key_dict:
        raise SignatureVerificationError(
            "Approval does not bind the scoped source identity key"
        )
    if approval.get("candidate_canonical_id") != proposal.candidate_canonical_id:
        raise SignatureVerificationError(
            "Approval does not bind the candidate Canonical ID"
        )
    if approval.get("requested_action") != proposal.requested_action:
        raise SignatureVerificationError("Approval does not bind the requested action")
    if approval.get("dataset_identity") != dataset_identity:
        raise SignatureVerificationError(
            "Approval does not bind this dataset / fixture identity"
        )
    if approval.get("approver") == approval.get("operator"):
        raise SignatureVerificationError(
            "Approval approver and operator must be distinct identities"
        )
    if not str(approval.get("approver") or "").strip():
        raise SignatureVerificationError(
            "Approval approver identity is blank; a signed decision must be attributable"
        )
    if not str(approval.get("operator") or "").strip():
        raise SignatureVerificationError(
            "Approval operator identity is blank; a signed decision must be attributable"
        )

    # --- 主体归属（账本主体 / 提案操作者 必须与被签名的身份一致） ----------
    if expected_actor is not None and approval.get("approver") != expected_actor:
        raise ActorBindingError(
            "The recorded decision actor does not match the signed approver "
            f"(actor={expected_actor!r}, signed approver={approval.get('approver')!r}); "
            "a signed decision can never be attributed to a different identity"
        )
    if expected_operator is not None and approval.get("operator") != expected_operator:
        raise ActorBindingError(
            "The signed operator does not match the proposal operator "
            f"(proposal operator={expected_operator!r}, "
            f"signed operator={approval.get('operator')!r})"
        )

    expected_baseline = approval.get("expected_baseline_fingerprint")
    if expected_baseline != current_baseline_fingerprint:
        raise StaleBaselineError(
            "The Canonical baseline fingerprint changed after this proposal was "
            "created; the approval cannot be reused against the new baseline"
        )
    if expected_baseline != proposal.expected_baseline_fingerprint:
        raise SignatureVerificationError(
            "Approval baseline binding does not match the proposal baseline binding"
        )

    # --- 签名校验 --------------------------------------------------------
    trusted = load_trusted_public_key(trusted_public_key_path)
    claimed_key_id = approval.get("public_key_id")
    if claimed_key_id != trusted["public_key_id"]:
        raise SignatureVerificationError(
            "Approval was produced by an untrusted signing key "
            f"(claimed {claimed_key_id!r}, trusted {trusted['public_key_id']!r})"
        )

    try:
        signature = base64.b64decode(str(signature_block.get("signature", "")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureVerificationError(
            f"Approval signature is not valid base64: {exc}"
        ) from exc
    if len(signature) != APPROVAL_SIGNATURE_BYTES:
        raise SignatureVerificationError(
            f"Approval signature has unexpected length {len(signature)}"
        )

    try:
        trusted["key"].verify(signature, approval_signing_payload(approval))
    except Exception as exc:  # noqa: BLE001
        raise SignatureVerificationError(
            "Owner approval signature does not verify against the canonical payload"
        ) from exc


# =============================================================================
# 隔离治理账本（append-only 哈希链）
# =============================================================================

def _assert_isolated_ledger_path(path: str) -> str:
    """
    账本路径必须显式给出，且必须位于仓库**之外**的隔离工作区。

    刻意拒绝：空路径、仓库内部任何路径（含 ``data/``）。这样保证治理工件永远不会
    被误当作生产数据，也不会被意外提交。
    """
    if not path or not str(path).strip():
        raise LedgerPathError(
            "A governance ledger path must be configured explicitly; there is no "
            "default ledger location"
        )
    norm = os.path.realpath(os.path.abspath(path))
    if norm == REPO_ROOT or norm.startswith(REPO_ROOT + os.sep):
        raise LedgerPathError(
            "Refusing to create a governance ledger inside the repository "
            f"({REPO_ROOT}). Use an isolated temporary workspace outside the "
            "repository; the ledger is never production authority."
        )
    return norm


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class LedgerEntry:
    """
    账本中的一条 append-only 条目。

    ``approval_artifact`` 与 ``proposal_evidence`` 使**独立的历史批准复核**成为可能：
    审计者仅凭账本 + 明确可信的公钥即可重算 Ed25519 签名并核对全部绑定，无需任何
    外部数据库。它们位于被哈希覆盖的条目内容之内，因此篡改会破坏哈希链。
    绝不包含任何私钥。
    """

    sequence: int
    transition_id: str
    decision_fingerprint: str
    replay_key: str
    proposal_id: str
    from_state: str
    to_state: str
    actor: str
    actor_role: str
    occurred_at: str
    reason: str
    proposal_fingerprint: str
    proposal_content_fingerprint: str
    evidence_fingerprint: str
    baseline_fingerprint: str
    approval_fingerprint: str | None
    previous_entry_hash: str
    entry_hash: str
    binding_source_key: str = ""
    binding_canonical_id: str | None = None
    binding_source_key_contract: str = ""
    approval_artifact: dict | None = None
    proposal_evidence: dict | None = None

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "transition_id": self.transition_id,
            "decision_fingerprint": self.decision_fingerprint,
            "replay_key": self.replay_key,
            "proposal_id": self.proposal_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "occurred_at": self.occurred_at,
            "reason": self.reason,
            "proposal_fingerprint": self.proposal_fingerprint,
            "proposal_content_fingerprint": self.proposal_content_fingerprint,
            "evidence_fingerprint": self.evidence_fingerprint,
            "baseline_fingerprint": self.baseline_fingerprint,
            "approval_fingerprint": self.approval_fingerprint,
            "binding_source_key": self.binding_source_key,
            "binding_canonical_id": self.binding_canonical_id,
            "binding_source_key_contract": self.binding_source_key_contract,
            "approval_artifact": self.approval_artifact,
            "proposal_evidence": self.proposal_evidence,
            "previous_entry_hash": self.previous_entry_hash,
            "entry_hash": self.entry_hash,
        }


#: 构成一次「决策」的字段（不含派生哈希、账本位置、前驱绑定与发生时刻）。
#:
#: 刻意**排除** ``occurred_at``：同一条决策的重复提交必须被识别为重复（幂等
#: no-op），而不是因为时间戳不同被误当作一条新决策。原始时间戳由第一条记录保留，
#: 后续重复提交不会改写它。
_DECISION_FIELDS: tuple[str, ...] = (
    "proposal_id",
    "to_state",
    "actor",
    "actor_role",
    "reason",
    "proposal_fingerprint",
    "proposal_content_fingerprint",
    "evidence_fingerprint",
    "baseline_fingerprint",
    "approval_fingerprint",
    "binding_source_key",
    "binding_canonical_id",
)


def _decision_payload(payload: Mapping[str, Any]) -> dict:
    return {key: payload.get(key) for key in _DECISION_FIELDS}


def _decision_fingerprint(payload: Mapping[str, Any]) -> str:
    """
    决策身份：**与账本位置、前驱哈希和来源状态无关**。

    因此同一条决策的重复提交（哪怕当前状态已经改变）会被识别为重复，
    而不是被误当作一次新的、非法的状态迁移。
    """
    return fingerprint(_decision_payload(payload))


def _transition_identity(payload: Mapping[str, Any]) -> str:
    """已记录的迁移身份：决策身份 + 迁移发生时的来源状态。"""
    body = _decision_payload(payload)
    body["from_state"] = payload.get("from_state")
    return fingerprint(body)


def _replay_key_of(payload: Mapping[str, Any]) -> str:
    """
    重放键：同一提案、同一目标状态、同一主体、同一批准件与同一基线。

    同键但决策指纹不同 = 同一条决策被以不同载荷（例如不同时间戳）重复提交
    -> 显式拒绝（绝不追加第二条互相冲突的活动决策）。
    """
    return fingerprint(
        {
            "proposal_id": payload.get("proposal_id"),
            "to_state": payload.get("to_state"),
            "actor": payload.get("actor"),
            "actor_role": payload.get("actor_role"),
            "approval_fingerprint": payload.get("approval_fingerprint"),
            "baseline_fingerprint": payload.get("baseline_fingerprint"),
        }
    )


def _entry_hash(payload: Mapping[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "entry_hash"}
    return fingerprint(body)


class GovernanceLedger:
    """
    隔离的 append-only 治理账本。

    实现要点
    --------
    - 路径必须显式配置，且必须在仓库之外（见 ``_assert_isolated_ledger_path``）。
    - 仅由调用方显式写入；``refresh()`` **从不**触碰它。
    - 原子写入（同目录临时文件 + ``os.replace``）。
    - 跨进程/跨线程互斥（独立的账本锁，与刷新锁无关）。
    - 前驱哈希绑定；断裂即 ``LedgerIntegrityError``。
    - 重放检测：同一决策以不同载荷重复提交 -> ``LedgerReplayError``；
      完全相同的提交 -> 幂等 no-op。
    - 冲突活动绑定检测：同一源身份不得同时拥有两个不同的活动 Canonical 绑定。
    - 保留被拒绝与被取代的证据（append-only，永不删除）。

    **哈希链只证明内部一致性**（可发现意外或可检测的篡改），在没有独立可信签名或
    检查点之前，它不是外部信任锚，也不得被描述为「防篡改」。
    """

    def __init__(self, path: str, *, dataset_identity: str = "") -> None:
        self.path = _assert_isolated_ledger_path(path)
        self.dataset_identity = dataset_identity
        self._lock_path = self.path + ".lock"

    # -- 读取（只读） ------------------------------------------------------

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def raw_lines(self) -> tuple[str, ...]:
        if not self.exists():
            return ()
        with open(self.path, "r", encoding="utf-8") as handle:
            return tuple(line for line in handle.read().splitlines() if line.strip())

    def entries(self) -> tuple[LedgerEntry, ...]:
        """读取全部条目（只读；不做完整性判定）。"""
        out: list[LedgerEntry] = []
        for index, line in enumerate(self.raw_lines()):
            try:
                payload = json.loads(line)
            except Exception as exc:  # noqa: BLE001
                raise LedgerIntegrityError(
                    f"Ledger line {index} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise LedgerIntegrityError(f"Ledger line {index} is not an object")
            try:
                out.append(LedgerEntry(**payload))
            except TypeError as exc:
                raise LedgerIntegrityError(
                    f"Ledger line {index} has an unexpected shape: {exc}"
                ) from exc
        return tuple(out)

    def verify(self) -> dict:
        """
        只读完整性校验：重算哈希链、检查序号连续性、检测断裂与重放。

        返回确定性报告；任何不一致都抛 ``LedgerIntegrityError``。
        """
        entries = self.entries()
        previous = GENESIS_PREVIOUS_HASH
        seen_transitions: dict[str, str] = {}
        seen_replay_keys: dict[str, str] = {}

        for index, entry in enumerate(entries):
            if entry.sequence != index:
                raise LedgerIntegrityError(
                    f"Ledger sequence gap at index {index}: entry sequence is {entry.sequence}"
                )
            if entry.previous_entry_hash != previous:
                raise LedgerIntegrityError(
                    f"Broken hash chain at index {index}: previous_entry_hash "
                    f"{entry.previous_entry_hash!r} != expected {previous!r}"
                )
            recomputed = _entry_hash(entry.as_dict())
            if recomputed != entry.entry_hash:
                raise LedgerIntegrityError(
                    f"Ledger entry {index} hash mismatch: content was modified"
                )
            expected_transition_id = _transition_identity(entry.as_dict())
            if expected_transition_id != entry.transition_id:
                raise LedgerIntegrityError(
                    f"Ledger entry {index} transition identity mismatch"
                )
            expected_decision = _decision_fingerprint(entry.as_dict())
            if expected_decision != entry.decision_fingerprint:
                raise LedgerIntegrityError(
                    f"Ledger entry {index} decision fingerprint mismatch"
                )
            expected_replay_key = _replay_key_of(entry.as_dict())
            if expected_replay_key != entry.replay_key:
                raise LedgerIntegrityError(
                    f"Ledger entry {index} replay key mismatch"
                )
            if entry.transition_id in seen_transitions:
                raise LedgerIntegrityError(
                    f"Ledger entry {index} duplicates transition identity"
                )
            seen_transitions[entry.transition_id] = entry.entry_hash
            if entry.replay_key in seen_replay_keys:
                raise LedgerReplayError(
                    f"Ledger entry {index} replays an already recorded decision"
                )
            seen_replay_keys[entry.replay_key] = entry.entry_hash
            previous = entry.entry_hash

        return {
            "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
            "verification_category": VERIFICATION_STRUCTURAL_LEDGER_INTEGRITY,
            "ledger_path": self.path,
            "entry_count": len(entries),
            "chain_tip": previous,
            "integrity": "OK",
            "binding_key_contracts": self.binding_key_contracts(),
            "incomplete_approved_entries": list(self.incomplete_approved_entries()),
            "trust_anchor": LEDGER_TRUST_ANCHOR_STATEMENT,
        }

    # -- 状态视图（只读） --------------------------------------------------

    def proposal_states(self) -> dict[str, str]:
        """每个提案的当前状态（由最后一条迁移推导；只读）。"""
        states: dict[str, str] = {}
        for entry in self.entries():
            states[entry.proposal_id] = entry.to_state
        return states

    def state_of(self, proposal_id: str) -> str | None:
        return self.proposal_states().get(proposal_id)

    def history_of(self, proposal_id: str) -> tuple[LedgerEntry, ...]:
        """某提案的完整迁移历史（只读；证据永不被静默抹除）。"""
        return tuple(e for e in self.entries() if e.proposal_id == proposal_id)

    def active_bindings(self, *, require_current_keys: bool = True) -> dict[str, dict[str, str]]:
        """
        活动绑定视图（只读）：**绑定身份键** -> {canonical_id, proposal_id}。

        仅 ``APPROVED`` 且动作是绑定动作的提案计入。

        ``require_current_keys=True``（默认）时，一旦发现由**旧契约**绑定键支撑的
        活动绑定即抛 ``LegacyBindingKeyError`` —— 旧键不得被静默重新解释为新的身份键
        含义，也不得据以主张活动授权。设为 ``False`` 时只返回当前契约的绑定，
        旧键另行通过 ``legacy_active_bindings()`` 只读检视（历史可审计）。
        """
        states = self.proposal_states()
        bindings: dict[str, dict[str, str]] = {}
        for proposal_id, state in sorted(states.items()):
            if state not in ACTIVE_STATES:
                continue
            record = self._binding_record_for(proposal_id)
            if record is None:
                continue
            key, canonical_id, contract = record
            if contract != BINDING_KEY_CURRENT:
                if require_current_keys:
                    raise LegacyBindingKeyError(
                        "An active binding is backed by a legacy binding key contract "
                        f"({contract}); it cannot be reinterpreted as the corrected "
                        "scoped source identity key. Re-approval under the current "
                        "contract is required."
                    )
                continue
            bindings[key] = {"canonical_id": canonical_id, "proposal_id": proposal_id}
        return bindings

    def legacy_active_bindings(self) -> tuple[dict[str, str], ...]:
        """
        只读检视：由**旧契约**绑定键支撑的活动绑定（历史审计，不构成授权）。
        """
        states = self.proposal_states()
        legacy: list[dict[str, str]] = []
        for proposal_id, state in sorted(states.items()):
            if state not in ACTIVE_STATES:
                continue
            record = self._binding_record_for(proposal_id)
            if record is None:
                continue
            key, canonical_id, contract = record
            if contract == BINDING_KEY_CURRENT:
                continue
            legacy.append(
                {
                    "proposal_id": proposal_id,
                    "binding_source_key": key,
                    "binding_key_contract": contract,
                    "canonical_id": canonical_id,
                }
            )
        return tuple(legacy)

    def binding_key_contracts(self) -> dict[str, int]:
        """只读：账本中出现的绑定键契约分布。"""
        counts: dict[str, int] = {}
        for entry in self.entries():
            key = entry.binding_source_key
            if not key:
                continue
            contract = entry.binding_source_key_contract or classify_binding_key(key)
            counts[contract] = counts.get(contract, 0) + 1
        return counts

    def incomplete_approved_entries(self) -> tuple[int, ...]:
        """
        只读：缺少可独立复核批准证据的 APPROVED 条目序号。

        新增记录永不缺证据；该视图用于检出旧记录并驱动 fail-closed 审计。
        """
        return tuple(
            entry.sequence
            for entry in self.entries()
            if entry.to_state in ACTIVE_STATES
            and (not entry.approval_artifact or not entry.proposal_evidence)
        )

    def _binding_record_for(self, proposal_id: str) -> tuple[str, str, str] | None:
        """从账本中恢复某提案的绑定记录（身份键 + 目标 Canonical ID + 契约分类）。"""
        for entry in self.entries():
            if entry.proposal_id != proposal_id:
                continue
            key = entry.binding_source_key
            canonical = entry.binding_canonical_id
            if key and canonical:
                contract = entry.binding_source_key_contract or classify_binding_key(key)
                return str(key), str(canonical), contract
        return None

    # -- 写入 --------------------------------------------------------------

    def append(
        self,
        *,
        proposal: MappingProposal,
        to_state: str,
        actor: str,
        actor_role: str,
        reason: str,
        baseline_fingerprint: str,
        evidence_fingerprint: str = "",
        approval: Mapping[str, Any] | None = None,
        trusted_public_key_path: str | None = None,
        source_observation_fingerprint: str | None = None,
        occurred_at: str | None = None,
        expected_previous_hash: str | None = None,
    ) -> tuple[LedgerEntry, bool]:
        """
        追加一次状态迁移。返回 ``(entry, appended)``；``appended=False`` 表示幂等
        no-op（完全相同的提交）。

        失败一律 fail closed：非法迁移、冲突活动绑定、重放、断链、缺批准、
        主体归属不匹配、旧契约绑定键。
        """
        validate_proposal(proposal)

        with _ledger_lock(self._lock_path):
            entries = self.entries()
            if entries:
                self.verify()

            previous = entries[-1].entry_hash if entries else GENESIS_PREVIOUS_HASH

            prior = [e for e in entries if e.proposal_id == proposal.proposal_id]
            if prior:
                current_state = prior[-1].to_state
            else:
                current_state = MappingState.OBSERVED.value
                if to_state != proposal.status:
                    raise InvalidTransitionError(
                        f"The first recorded transition for a new proposal must match "
                        f"its declared status '{proposal.status}', got '{to_state}'"
                    )

            if not str(actor or "").strip():
                raise ActorBindingError(
                    "A decision actor identity is required; every transition must be "
                    "attributable"
                )

            key_contract = classify_binding_key(proposal.source_key)
            if key_contract != BINDING_KEY_CURRENT:
                raise LegacyBindingKeyError(
                    "The proposal carries a binding key that is not on the current "
                    f"contract ({key_contract}); it is never silently reinterpreted"
                )

            payload: dict[str, Any] = {
                "sequence": len(entries),
                "proposal_id": proposal.proposal_id,
                "from_state": current_state,
                "to_state": to_state,
                "actor": actor,
                "actor_role": actor_role,
                "occurred_at": occurred_at or _utc_now(),
                "reason": reason,
                "proposal_fingerprint": proposal.fingerprint(),
                "proposal_content_fingerprint": proposal.content_fingerprint(),
                "evidence_fingerprint": evidence_fingerprint
                or fingerprint(list(proposal.observation_fingerprints)),
                "baseline_fingerprint": baseline_fingerprint,
                "approval_fingerprint": (
                    approval_fingerprint(approval) if approval else None
                ),
                "binding_source_key": proposal.source_key,
                "binding_canonical_id": proposal.candidate_canonical_id,
                "binding_source_key_contract": key_contract,
                "approval_artifact": dict(approval) if approval else None,
                "proposal_evidence": (
                    proposal.evidence_payload() if to_state in ACTIVE_STATES else None
                ),
            }

            # --- 重复提交处理（先于状态迁移校验） --------------------------
            # 完全相同的决策 -> 幂等 no-op（即使当前状态已推进）。
            decision_fp = _decision_fingerprint(payload)
            replay_key = _replay_key_of(payload)
            for entry in entries:
                if entry.decision_fingerprint == decision_fp:
                    return entry, False
            # 同一条决策被以不同载荷重复提交 -> 显式拒绝。
            for entry in entries:
                if entry.replay_key == replay_key:
                    raise LedgerReplayError(
                        "This decision was already recorded with different payload "
                        f"metadata (entry {entry.sequence}); refusing to append a "
                        "second conflicting decision"
                    )

            validate_transition(current_state, to_state)

            if to_state in OWNER_APPROVAL_REQUIRED_STATES:
                if actor_role != "owner":
                    raise SignatureVerificationError(
                        f"State '{to_state}' requires an owner decision; "
                        f"actor_role '{actor_role}' is not authorized"
                    )
                if not trusted_public_key_path:
                    raise SignatureVerificationError(
                        "A trusted owner public key path is required to verify an approval"
                    )
                verify_mapping_approval(
                    approval,
                    proposal=proposal,
                    source_observation_fingerprint=(
                        source_observation_fingerprint
                        or (
                            proposal.observation_fingerprints[0]
                            if proposal.observation_fingerprints
                            else ""
                        )
                    ),
                    dataset_identity=self.dataset_identity,
                    current_baseline_fingerprint=baseline_fingerprint,
                    trusted_public_key_path=trusted_public_key_path,
                    # 主体归属：账本主体必须是被签名的批准人，且被签名的操作者必须
                    # 是提案的操作者。仅靠 actor_role="owner" 不足以证明身份。
                    expected_actor=actor,
                    expected_operator=proposal.operator,
                )

            if expected_previous_hash is not None and expected_previous_hash != previous:
                raise LedgerReplayError(
                    "The transition was built against a different ledger tip "
                    f"({expected_previous_hash!r} != {previous!r}); refusing to replay it"
                )

            if to_state in ACTIVE_STATES and proposal.is_binding_action:
                if not proposal.identity_key_sufficient:
                    raise ProposalValidationError(
                        "An active binding requires sufficient scoped source identity "
                        f"evidence; identity_kind='{proposal.identity_key_kind}' is not "
                        "sufficient"
                    )
                existing = self.active_bindings().get(proposal.source_key)
                if existing and existing["canonical_id"] != proposal.candidate_canonical_id:
                    raise ConflictingBindingError(
                        "Source identity is already actively bound to Canonical ID "
                        f"'{existing['canonical_id']}' by proposal "
                        f"'{existing['proposal_id']}'; a separately governed "
                        "supersession decision is required before rebinding"
                    )

            payload["decision_fingerprint"] = decision_fp
            payload["replay_key"] = replay_key
            payload["transition_id"] = _transition_identity(payload)
            payload["previous_entry_hash"] = previous
            payload["entry_hash"] = _entry_hash(payload)

            entry = LedgerEntry(**payload)
            self._write_all(entries + (entry,))
            return entry, True

    # -- 内部持久化 --------------------------------------------------------

    def _write_all(self, entries: Sequence[LedgerEntry]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                for entry in entries:
                    handle.write(canonical_json(entry.as_dict()))
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


#: 账本锁的最长等待时间（秒）。跨进程竞争时以非阻塞尝试 + 截止时间重试实现，
#: 而不是依赖 ``msvcrt.LK_LOCK`` 固定长度的重试窗口（该窗口在慢速文件系统或
#: 安全软件扫描下可能不足，从而让一次合法写入假性失败）。
LEDGER_LOCK_TIMEOUT_SECONDS = 120.0

#: 两次非阻塞加锁尝试之间的间隔（秒）。
LEDGER_LOCK_POLL_SECONDS = 0.05


class _ledger_lock:
    """
    账本专用的跨进程 + 跨线程互斥锁。

    刻意**不复用** ``utils/concurrency.acquire_refresh_lock``：后者绑定
    ``config.DATA_DIR`` 且属于刷新锁语义，二者不得相互影响。
    """

    _thread_guards: dict[str, Any] = {}
    _thread_guards_lock = None

    def __init__(self, lock_path: str) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None
        self._guard = None

    def __enter__(self):
        import threading

        if _ledger_lock._thread_guards_lock is None:
            _ledger_lock._thread_guards_lock = threading.Lock()
        with _ledger_lock._thread_guards_lock:
            guard = _ledger_lock._thread_guards.get(self._lock_path)
            if guard is None:
                guard = threading.Lock()
                _ledger_lock._thread_guards[self._lock_path] = guard
        self._guard = guard
        self._guard.acquire()

        directory = os.path.dirname(self._lock_path) or "."
        os.makedirs(directory, exist_ok=True)
        self._fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        st = os.fstat(self._fd)
        if st.st_size == 0:
            os.write(self._fd, b"0")
        os.lseek(self._fd, 0, os.SEEK_SET)

        try:
            self._acquire_os_lock()
        except BaseException:
            os.close(self._fd)
            self._fd = None
            self._guard.release()
            raise
        return self

    def _acquire_os_lock(self) -> None:
        """获取 OS 级排他锁；以截止时间为界重试，超时才放弃。"""
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + LEDGER_LOCK_TIMEOUT_SECONDS
            while True:
                os.lseek(self._fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(LEDGER_LOCK_POLL_SECONDS)
        else:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_EX)

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._fd is not None:
                try:
                    if os.name == "nt":
                        import msvcrt

                        os.lseek(self._fd, 0, os.SEEK_SET)
                        msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            if self._guard is not None:
                self._guard.release()
        return False


# =============================================================================
# 历史批准审计（与结构性账本校验严格分离）
# =============================================================================

#: 结构性账本校验：序号 / 前驱哈希 / 条目哈希 / 重放 / 结构一致性。
VERIFICATION_STRUCTURAL_LEDGER_INTEGRITY = "STRUCTURAL_LEDGER_INTEGRITY"

#: 密码学批准审计：可信公钥 + 可恢复签名证据 + 主体归属 + 绑定核验。
VERIFICATION_AUTHENTICATED_MAPPING_APPROVAL_AUDIT = (
    "AUTHENTICATED_MAPPING_APPROVAL_AUDIT"
)

#: 当前映射资格：历史批准对**当前**基线是否仍然有效。
VERIFICATION_CURRENT_MAPPING_ELIGIBILITY = "CURRENT_MAPPING_ELIGIBILITY"

#: 账本自身的信任锚说明。哈希链不是外部信任锚。
LEDGER_TRUST_ANCHOR_STATEMENT = (
    "NONE — the hash chain proves internal consistency only; it is not an "
    "independently trusted signature or checkpoint, and it does not by itself detect "
    "tail truncation or a full-chain rewrite"
)


def reconstruct_proposal_from_evidence(evidence: Mapping[str, Any]) -> MappingProposal:
    """
    由账本保留的**不可变提案证据**重建提案（只读）。

    重建出的 ``proposal_id`` / 内容指纹必须与账本记录一致，否则证据不可信。
    """
    if not isinstance(evidence, Mapping):
        raise MissingApprovalEvidenceError("Proposal evidence is missing or malformed")
    required = (
        "proposal_schema_version",
        "source_identity_scope",
        "scoped_source_identity_key",
        "candidate_canonical_id",
        "identity_evidence_type",
        "observation_fingerprints",
        "ingestion_plan_reference",
        "reason",
        "expected_baseline_fingerprint",
        "requested_action",
        "operator",
    )
    missing = [field for field in required if field not in evidence]
    if missing:
        raise MissingApprovalEvidenceError(
            f"Proposal evidence is incomplete; missing {sorted(missing)}"
        )
    return build_proposal(
        source_observation_ref=str(evidence.get("source_observation_ref") or ""),
        source_identity_scope=dict(evidence["source_identity_scope"]),
        scoped_source_identity_key=dict(evidence["scoped_source_identity_key"]),
        candidate_canonical_id=evidence.get("candidate_canonical_id"),
        existing_canonical_owner=evidence.get("existing_canonical_owner"),
        identity_evidence_type=str(evidence["identity_evidence_type"]),
        observation_fingerprints=list(evidence["observation_fingerprints"]),
        ingestion_plan_reference=str(evidence["ingestion_plan_reference"]),
        reason=str(evidence["reason"]),
        expected_baseline_fingerprint=str(evidence["expected_baseline_fingerprint"]),
        requested_action=str(evidence["requested_action"]),
        conflict_category=evidence.get("conflict_category"),
        operator=str(evidence.get("operator") or ""),
    )


def audit_historical_approvals(
    ledger: GovernanceLedger,
    *,
    trusted_public_key_path: str | None,
    dataset_identity: str | None = None,
) -> dict:
    """
    **独立的历史批准审计**：仅凭账本 + 明确可信的公钥重算并核验每一份所有者批准。

    与结构性校验严格分离（``ledger.verify()`` 只做结构性判定）。本函数：

    1. 先做结构性校验（哈希链 / 重放 / 结构）；
    2. 对每一条 ``APPROVED`` 记录要求保留的批准工件与不可变提案证据齐备
       —— 缺失即 fail closed（``MissingApprovalEvidenceError``）；
    3. 用**记录中的原始基线**重新验证 Ed25519 签名与全部绑定（历史真实性）；
    4. 校验主体归属：账本主体 == 被签名的批准人；被签名的操作者 == 提案操作者。

    ``trusted_public_key_path`` **必须**由调用方提供。缺失时**绝不**退化为信任
    批准件内嵌的公钥 —— 那会把信任锚交到被审计对象自己手里。

    历史真实性与「当前是否可执行」是两件事：本函数只回答前者。
    """
    if not trusted_public_key_path:
        raise SignatureVerificationError(
            "An explicitly trusted public key is required for an authenticated approval "
            "audit; an embedded public key is never accepted as the trust anchor"
        )

    structural = ledger.verify()
    entries = ledger.entries()
    verified: list[dict] = []
    failures: list[dict] = []

    for entry in entries:
        if entry.to_state not in ACTIVE_STATES:
            continue
        if not entry.approval_artifact or not entry.proposal_evidence:
            raise MissingApprovalEvidenceError(
                f"Ledger entry {entry.sequence} is an APPROVED decision without "
                "retained approval evidence; authenticated verification is impossible"
            )

        try:
            proposal = reconstruct_proposal_from_evidence(entry.proposal_evidence)
            if proposal.proposal_id != entry.proposal_id:
                raise SignatureVerificationError(
                    f"Ledger entry {entry.sequence} proposal evidence does not "
                    "reproduce the recorded proposal identity"
                )
            if proposal.content_fingerprint() != entry.proposal_content_fingerprint:
                raise SignatureVerificationError(
                    f"Ledger entry {entry.sequence} proposal evidence does not "
                    "reproduce the recorded content fingerprint"
                )
            if approval_fingerprint(entry.approval_artifact) != entry.approval_fingerprint:
                raise SignatureVerificationError(
                    f"Ledger entry {entry.sequence} retained approval artifact does not "
                    "match the recorded approval fingerprint"
                )
            if not binding_key_is_current(entry.binding_source_key):
                raise LegacyBindingKeyError(
                    f"Ledger entry {entry.sequence} is bound under a legacy binding key "
                    "contract and cannot be reinterpreted under the corrected identity key"
                )
            observation_fingerprint = (
                proposal.observation_fingerprints[0]
                if proposal.observation_fingerprints
                else ""
            )
            verify_mapping_approval(
                entry.approval_artifact,
                proposal=proposal,
                source_observation_fingerprint=observation_fingerprint,
                dataset_identity=(
                    dataset_identity
                    if dataset_identity is not None
                    else ledger.dataset_identity
                ),
                # 历史真实性：以**记录中的原始基线**为准，而非当前基线。
                current_baseline_fingerprint=entry.baseline_fingerprint,
                trusted_public_key_path=trusted_public_key_path,
                expected_actor=entry.actor,
                expected_operator=proposal.operator,
            )
        except GovernanceError as exc:
            # fail closed：保留原始异常类型并附上条目定位，绝不把失败降级为聚合报告。
            raise type(exc)(
                f"Ledger entry {entry.sequence} (proposal {entry.proposal_id[:12]}...): "
                f"{exc}"
            ) from exc

        verified.append(
            {
                "sequence": entry.sequence,
                "proposal_id": entry.proposal_id,
                "actor": entry.actor,
                "approver": entry.approval_artifact.get("approver"),
                "operator": entry.approval_artifact.get("operator"),
                "candidate_canonical_id": entry.binding_canonical_id,
                "scoped_source_identity_key": entry.binding_source_key,
                "recorded_baseline_fingerprint": entry.baseline_fingerprint,
                "approval_fingerprint": entry.approval_fingerprint,
                "public_key_id": entry.approval_artifact.get("public_key_id"),
            }
        )

    if failures:
        raise MissingApprovalEvidenceError(
            "Authenticated approval audit failed for "
            f"{len(failures)} decision(s): {failures}"
        )

    trusted = load_trusted_public_key(trusted_public_key_path)
    return {
        "verification_category": VERIFICATION_AUTHENTICATED_MAPPING_APPROVAL_AUDIT,
        "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
        "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
        "ledger_path": ledger.path,
        "structural_integrity": structural["integrity"],
        "trust_anchor": f"trusted public key {trusted['public_key_id']}",
        "approved_decisions": len(verified),
        "verified": verified,
        "failures": [],
    }


def mapping_eligibility(
    ledger: GovernanceLedger,
    *,
    current_baseline_fingerprint: str,
) -> dict:
    """
    **当前映射资格**（只读）：历史批准对**当前**基线是否仍然有效。

    一份历史批准可以依然「真实」（签名有效、绑定完整），却因为 Canonical 基线
    已经变化而**不再可执行**。二者必须分开报告：一份有效的旧签名**绝不**自动
    授权对新基线采取行动。
    """
    states = ledger.proposal_states()
    eligible: list[dict] = []
    stale: list[dict] = []

    for entry in ledger.entries():
        if entry.to_state not in ACTIVE_STATES:
            continue
        if states.get(entry.proposal_id) not in ACTIVE_STATES:
            continue
        record = {
            "sequence": entry.sequence,
            "proposal_id": entry.proposal_id,
            "candidate_canonical_id": entry.binding_canonical_id,
            "recorded_baseline_fingerprint": entry.baseline_fingerprint,
        }
        if entry.baseline_fingerprint == current_baseline_fingerprint:
            eligible.append(record)
        else:
            stale.append(
                {
                    **record,
                    "current_baseline_fingerprint": current_baseline_fingerprint,
                    "detail": (
                        "historical approval authenticity may still hold, but it does "
                        "not authorize action against the new baseline"
                    ),
                }
            )

    return {
        "verification_category": VERIFICATION_CURRENT_MAPPING_ELIGIBILITY,
        "current_baseline_fingerprint": current_baseline_fingerprint,
        "eligible_count": len(eligible),
        "stale_count": len(stale),
        "eligible": eligible,
        "stale": stale,
    }


# =============================================================================
# 冲突与重放治理（只读判定）
# =============================================================================

#: 冲突类别 -> 建议的治理动作与目标状态。
CONFLICT_GOVERNANCE_RULES: dict[str, dict[str, str]] = {
    "CROSS_SPORT_SHARED_NAMESPACE": {
        "requested_action": ProposalAction.FLAG_AMBIGUOUS_IDENTITY.value,
        "target_state": MappingState.REVIEW_REQUIRED.value,
        "detail": (
            "同一源 ID 字符串跨运动/跨日期出现；两者必须保持为不同的作用域身份，"
            "共享的旧 500j 字符串不得静默授权两个不同的 Canonical 所有者。"
        ),
    },
    "ROW_POSITION_REUSE": {
        "requested_action": ProposalAction.FLAG_AMBIGUOUS_IDENTITY.value,
        "target_state": MappingState.REVIEW_REQUIRED.value,
        "detail": (
            "行位置派生的标识符被复用于不同比赛；不得自动覆盖任何既有映射。"
        ),
    },
    "STRONG_NUMBER_CONFLICT": {
        "requested_action": ProposalAction.REJECT_IDENTITY_CLAIM.value,
        "target_state": MappingState.REJECTED.value,
        "detail": "同一源身份试图代表两个强编号互斥的事件；直接拒绝。",
    },
    "CANONICAL_OWNER_CONFLICT": {
        "requested_action": ProposalAction.FLAG_AMBIGUOUS_IDENTITY.value,
        "target_state": MappingState.REVIEW_REQUIRED.value,
        "detail": (
            "同一源身份被提案给两个不同的 Canonical ID；需要治理决策，"
            "绝不自动采用最新一次声明。"
        ),
    },
}

#: 目标状态由规则表给出；APPROVED 永远不在其中（歧义不得自动变为活动）。
def _assert_no_conflict_rule_targets_approved() -> None:
    offenders = sorted(
        category
        for category, rule in CONFLICT_GOVERNANCE_RULES.items()
        if rule["target_state"] == MappingState.APPROVED.value
    )
    if offenders:
        raise GovernanceError(
            "conflict governance must never target APPROVED; offending categories: "
            f"{offenders}"
        )


_assert_no_conflict_rule_targets_approved()


def governance_rule_for(conflict_category: str) -> dict[str, str] | None:
    """取某冲突类别的治理规则（只读）。"""
    return CONFLICT_GOVERNANCE_RULES.get(conflict_category)


def conflict_scope(conflict: Any, members: Sequence[SourceObservation]) -> dict[str, str]:
    """
    歧义提案的源身份作用域。

    一条冲突（例如跨运动共享的旧 ``500j`` 字符串）**天然跨多个作用域**，因此不能
    伪装成单一声明作用域。这里显式记录：这是一条「共享源标识符歧义」的作用域，
    并列出全部成员作用域。
    """
    member_scopes = sorted(
        {canonical_json(o.stability_scope.as_dict()) for o in members}
    )
    return {
        "scope_kind": "AMBIGUOUS_SHARED_SOURCE_ID",
        "conflict_category": str(getattr(conflict, "category", "")),
        "shared_source_id": str(getattr(conflict, "subject", "")),
        "member_scopes": canonical_json(member_scopes),
    }


def proposal_for_conflict(
    conflict: Any,
    members: Sequence[SourceObservation],
    *,
    ingestion_plan_reference: str,
    expected_baseline_fingerprint: str,
    identity_evidence_type: str = "source_alias",
    candidate_canonical_id: str | None = None,
    existing_canonical_owner: str | None = None,
    operator: str = "",
) -> MappingProposal:
    """
    为一条冲突分类结论构造**非活动**提案。

    目标状态由 ``CONFLICT_GOVERNANCE_RULES`` 决定，且**永不**是 ``APPROVED``。
    """
    category = str(getattr(conflict, "category", ""))
    rule = governance_rule_for(category)
    if rule is None:
        raise ProposalValidationError(f"Unknown conflict category '{category}'")
    if rule["target_state"] == MappingState.APPROVED.value:
        raise ProposalValidationError("conflict governance must never propose APPROVED")

    fingerprints = [o.semantic_fingerprint() for o in members]
    if not fingerprints:
        fingerprints = [fingerprint({"conflict_subject": str(getattr(conflict, "subject", ""))})]

    return build_proposal(
        source_observation_ref=(
            members[0].observation_id
            if members
            else str(getattr(conflict, "subject", ""))
        ),
        source_identity_scope=conflict_scope(conflict, members),
        candidate_canonical_id=candidate_canonical_id,
        existing_canonical_owner=existing_canonical_owner,
        identity_evidence_type=identity_evidence_type,
        observation_fingerprints=fingerprints,
        ingestion_plan_reference=ingestion_plan_reference,
        reason=rule["detail"],
        expected_baseline_fingerprint=expected_baseline_fingerprint,
        requested_action=rule["requested_action"],
        # 歧义身份键**永远不充分**，因此这类提案只能复核 / 拒绝，不能活动绑定。
        identity_key=ScopedSourceIdentityKey.for_ambiguous_source(
            shared_source_id=str(getattr(conflict, "subject", "")),
            member_scopes=[o.stability_scope.as_dict() for o in members],
            conflict_category=category,
        ),
        conflict_category=category,
        # 声明的初始状态是 PROPOSED：``OBSERVED`` 不等于已提案（§9.1），
        # 冲突必须先被显式提案，再由治理流程推进到 REVIEW_REQUIRED / REJECTED。
        status=MappingState.PROPOSED.value,
        operator=operator,
    )


def detect_owner_conflicts(
    proposals: Sequence[MappingProposal],
) -> tuple[dict[str, str], ...]:
    """
    检测「一个源身份被提案给多个不同 Canonical ID」（只读）。

    绝不自动选择最新声明；只报告冲突。
    """
    by_source: dict[str, set[str]] = {}
    for proposal in proposals:
        if proposal.is_binding_action and proposal.candidate_canonical_id:
            by_source.setdefault(proposal.source_key, set()).add(
                proposal.candidate_canonical_id
            )
    conflicts: list[dict[str, str]] = []
    for source_key in sorted(by_source):
        owners = by_source[source_key]
        if len(owners) > 1:
            conflicts.append(
                {
                    "category": "CANONICAL_OWNER_CONFLICT",
                    "source_key": source_key,
                    "competing_canonical_ids": ",".join(sorted(owners)),
                    "detail": (
                        "One source identity is proposed for multiple Canonical IDs; "
                        "automatic latest-claim selection is forbidden."
                    ),
                }
            )
    return tuple(conflicts)


def governance_status() -> dict:
    """治理层的授权状态（只读；供审计与报告使用）。"""
    return {
        "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
        "approval_schema_version": MAPPING_APPROVAL_SCHEMA_VERSION,
        "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
        "legacy_identity_key_schema_version": LEGACY_IDENTITY_KEY_SCHEMA_VERSION,
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "approval_purpose": MAPPING_APPROVAL_PURPOSE,
        "production_repair_purpose": "PRODUCTION_CANONICAL_DEDUP_APPLY",
        "ledger_is_production_authority": False,
        "runtime_identity_authority": "utils.ingestion_identity.validate_ingestion_identity",
        "structural_verification_category": VERIFICATION_STRUCTURAL_LEDGER_INTEGRITY,
        "authenticated_audit_category": VERIFICATION_AUTHENTICATED_MAPPING_APPROVAL_AUDIT,
        "eligibility_category": VERIFICATION_CURRENT_MAPPING_ELIGIBILITY,
        "ledger_trust_anchor": LEDGER_TRUST_ANCHOR_STATEMENT,
    }
