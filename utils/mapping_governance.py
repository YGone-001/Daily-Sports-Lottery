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
    PROVENANCE_SCHEMA_VERSION,
    SourceObservation,
    canonical_json,
    fingerprint,
    sha256_text,
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

PROPOSAL_SCHEMA_VERSION = "1.0.0"

#: 提案必填字段（schema 契约）。
PROPOSAL_REQUIRED_FIELDS: tuple[str, ...] = (
    "proposal_schema_version",
    "proposal_id",
    "source_observation_ref",
    "source_identity_scope",
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
    """

    proposal_schema_version: str
    proposal_id: str
    source_observation_ref: str
    source_identity_scope: tuple[tuple[str, str], ...]
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
    def source_key(self) -> str:
        """源身份作用域键（用于活动绑定冲突检测）。"""
        return canonical_json(self.scope_dict)

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
    conflict_category: str | None = None,
    status: str = MappingState.PROPOSED.value,
    operator: str = "",
) -> MappingProposal:
    """
    构造一条提案。``proposal_id`` 由**内容**确定性派生（与输入顺序无关）。
    """
    scope_pairs = tuple(
        sorted((str(k), str(v)) for k, v in dict(source_identity_scope).items())
    )
    proposal_id = fingerprint(
        {
            "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
            "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
            "source_observation_ref": source_observation_ref,
            "source_identity_scope": dict(scope_pairs),
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
    conflict_category: str | None = None,
    operator: str = "",
    status: str = MappingState.PROPOSED.value,
) -> MappingProposal:
    """
    由一组源观测构造提案（只读；不修改观测）。

    作用域取自**首条**观测（同一提案的观测必须共享同一作用域，否则请分别提案）。
    """
    if not observations:
        raise ProposalValidationError("at least one observation is required")
    scope = observations[0].stability_scope.as_dict()
    scopes = {tuple(sorted(o.stability_scope.as_dict().items())) for o in observations}
    if len(scopes) > 1:
        raise ProposalValidationError(
            "observations in one proposal must share a single source identity scope"
        )
    return build_proposal(
        source_observation_ref=observations[0].observation_id,
        source_identity_scope=scope,
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
        conflict_category=conflict_category,
        status=status,
        operator=operator,
    )


# =============================================================================
# 所有者批准信任边界（Ed25519）
# =============================================================================

MAPPING_APPROVAL_SCHEMA_VERSION = "1.0.0"

#: 域分离用途串。刻意与生产修复授权（``PRODUCTION_CANONICAL_DEDUP_APPLY``）
#: 完全不同，因此生产修复的授权件**永远无法**被当作映射批准件使用。
MAPPING_APPROVAL_PURPOSE = "CANONICAL_MAPPING_GOVERNANCE_APPROVE"

#: 建立签名机制：RFC 8032 Ed25519（PyCA ``cryptography``）。不自实现密码学。
APPROVAL_ALGORITHM = "ed25519"
APPROVAL_FIELD = "approval"
APPROVAL_PUBLIC_KEY_BYTES = 32
APPROVAL_SIGNATURE_BYTES = 64

#: 批准件必须绑定的字段。
APPROVAL_BOUND_FIELDS: tuple[str, ...] = (
    "proposal_id",
    "proposal_content_fingerprint",
    "source_observation_fingerprint",
    "source_identity_scope",
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
) -> None:
    """
    独立验证一份所有者批准件。

    拒绝（fail closed）：
    - 缺失 / 未签名 / 空签名的批准尝试；
    - 非映射用途的批准件（例如生产修复授权件）；
    - 任何被篡改的字段（签名校验失败）；
    - 不可信或非 Ed25519 的公钥；
    - 绑定不匹配（提案、内容指纹、观测指纹、作用域、目标 Canonical ID、动作、数据集）；
    - 基线指纹已变化（陈旧批准，必须重新验证）。
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
            f"'{MAPPING_APPROVAL_SCHEMA_VERSION}'"
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
    """账本中的一条 append-only 条目。"""

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
            "ledger_path": self.path,
            "entry_count": len(entries),
            "chain_tip": previous,
            "integrity": "OK",
            "trust_anchor": (
                "NONE — the hash chain proves internal consistency only; it is not an "
                "independently trusted signature or checkpoint"
            ),
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

    def active_bindings(self) -> dict[str, dict[str, str]]:
        """
        活动绑定视图（只读）：源身份作用域键 -> {canonical_id, proposal_id}。

        仅 ``APPROVED`` 且动作是绑定动作的提案计入。
        """
        states = self.proposal_states()
        bindings: dict[str, dict[str, str]] = {}
        for proposal_id, state in sorted(states.items()):
            if state not in ACTIVE_STATES:
                continue
            record = self._binding_record_for(proposal_id)
            if record is None:
                continue
            key, canonical_id = record
            bindings[key] = {"canonical_id": canonical_id, "proposal_id": proposal_id}
        return bindings

    def _binding_record_for(self, proposal_id: str) -> tuple[str, str] | None:
        """从账本中恢复某提案的绑定记录（源身份键 + 目标 Canonical ID）。"""
        for entry in self.entries():
            if entry.proposal_id != proposal_id:
                continue
            payload = entry.as_dict()
            key = payload.get("binding_source_key")
            canonical = payload.get("binding_canonical_id")
            if key and canonical:
                return str(key), str(canonical)
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

        失败一律 fail closed：非法迁移、冲突活动绑定、重放、断链、缺批准。
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
                )

            if expected_previous_hash is not None and expected_previous_hash != previous:
                raise LedgerReplayError(
                    "The transition was built against a different ledger tip "
                    f"({expected_previous_hash!r} != {previous!r}); refusing to replay it"
                )

            if to_state in ACTIVE_STATES and proposal.is_binding_action:
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
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_EX)
        except OSError:
            os.close(self._fd)
            self._fd = None
            self._guard.release()
            raise
        return self

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
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "approval_purpose": MAPPING_APPROVAL_PURPOSE,
        "production_repair_purpose": "PRODUCTION_CANONICAL_DEDUP_APPLY",
        "ledger_is_production_authority": False,
        "runtime_identity_authority": "utils.ingestion_identity.validate_ingestion_identity",
    }
