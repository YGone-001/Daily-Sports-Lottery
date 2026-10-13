"""
运行期激活授权 **隔离原型** 与影子执行
======================================
本模块是一个**原型**，用于验证未来「受控运行期映射激活」的授权工件结构、签名
验证、生命周期、有效期 / 撤销处理、回滚模拟与影子快照生成。

严格边界
--------
- **不激活任何运行期映射。** ``PRODUCTION_ACTIVATION = "NOT_ENABLED"``。
- **不连接生产刷新路径。** ``utils/scraper.py`` 等运行期模块**从不**导入本模块。
- **不写任何文件。** 审计日志是**内存**结构（追加式 + 哈希链），不做任何持久化。
  唯一的文件访问是**只读**地载入签名私钥（且必须位于仓库之外）。
- **不改写任何 Canonical ID、历史存储或生产数据。**
- **不自动授权。** 授权绝不由资格判定或所有者批准推导得出。
- 所有工件都是**合成**的；测试使用临时目录中的一次性密钥。

唯一允许的流程
--------------
::

    合成 Mapping Proposal
        -> 合成 Eligibility Decision
        -> RuntimeActivationAuthorization
        -> Shadow Activation Snapshot
        -> Shadow Runtime Evaluation

设计契约（字段名、用途域、允许范围、生命周期、审计事件）来自
``utils.runtime_activation_design``；本模块是它的**隔离原型实现**。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from utils.mapping_governance import (
    MappingProposal,
    _assert_private_key_path_is_external,
    _crypto_backend,
    generate_mapping_owner_keypair,
    load_trusted_public_key,
    public_key_id,
)
from utils.runtime_activation_design import (
    ACTIVATION_AUTHORIZATION_ALGORITHM,
    ACTIVATION_AUTHORIZATION_ARTIFACT,
    ACTIVATION_AUTHORIZATION_PURPOSE,
    ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS,
    ACTIVATION_AUTHORIZATION_SCHEMA_VERSION,
    ALLOWED_ACTIVATION_SCOPES,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
    RuntimeEligibilityResult,
    RuntimeMappingEligibilityDecision,
)
from utils.runtime_mapping_policy import (
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)
from utils.source_identity import (
    ScopedSourceIdentityKey,
    SourceObservation,
    canonical_json,
    fingerprint,
)

#: 原型标记。任何「这已经是生产能力」的解读都是错的。
PROTOTYPE_ONLY = True

#: 生产运行期激活状态：**未启用**。
PRODUCTION_ACTIVATION = RUNTIME_MAPPING_ACTIVATION

#: 生产运行期读取器：**未启用**。
PRODUCTION_RUNTIME_READER = "NOT_ENABLED"

#: 授权工件 schema 版本（设计契约）。
RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION = (
    ACTIVATION_AUTHORIZATION_SCHEMA_VERSION
)

#: 授权用途（域分离；与映射批准、生产修复用途均不同）。
RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE = ACTIVATION_AUTHORIZATION_PURPOSE

#: 授权工件名称。
RUNTIME_ACTIVATION_AUTHORIZATION_ARTIFACT = ACTIVATION_AUTHORIZATION_ARTIFACT

#: 签名算法（沿用既有 Ed25519 原语，不自实现密码学）。
RUNTIME_ACTIVATION_SIGNATURE_ALGORITHM = ACTIVATION_AUTHORIZATION_ALGORITHM

#: 原型允许的激活范围（最小权限：只读解析参与）。
PROTOTYPE_ALLOWED_ACTIVATION_SCOPES: tuple[str, ...] = tuple(
    ALLOWED_ACTIVATION_SCOPES
)

#: 明确禁止出现在激活范围中的字眼（写入 / 变更语义）。
FORBIDDEN_SCOPE_TOKENS: tuple[str, ...] = (
    "WRITE",
    "MUTATE",
    "REPAIR",
    "MIGRATE",
    "REWRITE",
    "DELETE",
    "CREATE_MAPPING",
)

#: 影子审计日志的创世前驱哈希。
GENESIS_PREVIOUS_HASH = "0" * 64

#: 允许为空的授权字段（其余字段在签名时必须非空）。
MAY_BE_EMPTY_FIELDS: tuple[str, ...] = (
    "approval_fingerprint",
    "runtime_snapshot_id",
    "revocation_reference",
    "reason",
)


# =============================================================================
# 异常
# =============================================================================

class ActivationPrototypeError(Exception):
    """原型错误基类。"""


class ActivationAuthorizationError(ActivationPrototypeError):
    """授权工件的构造 / 签名错误。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


class InvalidActivationTransitionError(ActivationPrototypeError):
    """非法的激活生命周期迁移。"""


class ShadowActivationSnapshotError(ActivationPrototypeError):
    """影子快照构建被拒绝。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


# =============================================================================
# 拒绝码（声明式）
# =============================================================================

#: 授权验证的拒绝码。``(code, description)``。
AUTHORIZATION_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("MALFORMED_ARTIFACT", "工件缺失、类型错误或缺少必填字段。"),
    ("SCHEMA_VERSION_UNSUPPORTED", "授权 schema 版本不受支持。"),
    ("PURPOSE_MISMATCH", "用途不是激活授权用途（域分离失败）。"),
    ("SIGNATURE_ALGORITHM_UNSUPPORTED", "签名算法不是 ed25519。"),
    ("ACTIVATION_SCOPE_UNSUPPORTED", "激活范围不在允许列表内。"),
    ("ACTIVATION_SCOPE_WRITE_FORBIDDEN", "激活范围含写入 / 变更语义。"),
    ("TRUST_ANCHOR_MISSING", "未显式提供可信公钥。"),
    ("SIGNER_KEY_UNTRUSTED", "签名密钥与可信公钥不一致。"),
    ("SIGNATURE_INVALID", "Ed25519 签名验证失败。"),
    ("ELIGIBILITY_DECISION_MISSING", "缺少资格判定。"),
    ("ELIGIBILITY_DECISION_ID_MISMATCH", "资格判定 ID 与工件绑定不一致。"),
    (
        "ELIGIBILITY_DECISION_FINGERPRINT_MISMATCH",
        "资格判定内容指纹与工件绑定不一致（判定被改动或过期）。",
    ),
    ("ELIGIBILITY_EVALUATOR_VERSION_MISMATCH", "资格评估器版本不一致。"),
    ("ELIGIBILITY_NOT_ELIGIBLE", "所绑定的资格判定并非 ELIGIBLE。"),
    ("POLICY_VERSION_MISMATCH", "策略版本不一致。"),
    ("PROPOSAL_MISSING", "缺少提案。"),
    ("PROPOSAL_ID_MISMATCH", "提案 ID 与工件绑定不一致。"),
    ("PROPOSAL_FINGERPRINT_MISMATCH", "提案内容指纹与工件绑定不一致。"),
    ("SCOPED_SOURCE_IDENTITY_KEY_MISMATCH", "作用域身份键与提案不一致。"),
    ("DATASET_IDENTITY_MISMATCH", "数据集身份不一致。"),
    ("BASELINE_STALE", "基线指纹已变化（陈旧授权）。"),
    ("MALFORMED_TIMESTAMP", "授权时间戳无法解析。"),
    ("NOT_YET_VALID", "当前时间早于授权生效时间。"),
    ("ACTIVATION_AUTHORIZATION_EXPIRED", "授权已过期。"),
    ("ACTIVATION_AUTHORIZATION_REVOKED", "授权已被撤销。"),
    ("REVOCATION_REFERENCE_MISMATCH", "撤销登记与工件的撤销引用不一致。"),
)

#: 拒绝码集合（声明式）。
AUTHORIZATION_REJECTION_CODE_SET: frozenset[str] = frozenset(
    code for code, _ in AUTHORIZATION_REJECTION_CODES
)

#: 表示「不可消费」的拒绝码（授权本身可能仍然真实）。
NON_CONSUMABLE_REJECTION_CODES: frozenset[str] = frozenset(
    {
        "ACTIVATION_AUTHORIZATION_EXPIRED",
        "ACTIVATION_AUTHORIZATION_REVOKED",
        "REVOCATION_REFERENCE_MISMATCH",
        "NOT_YET_VALID",
    }
)


# =============================================================================
# 授权工件
# =============================================================================

_ARTIFACT_FIELD_NAMES: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeActivationAuthorization:
    """
    运行期激活授权工件（**不可变**、可序列化、可哈希、确定性）。

    它与所有者批准是**不同**的工件、**不同**的用途域：
    批准回答「这份映射是否正确」，本工件回答「这份映射是否可以影响运行期」。

    ``runtime_snapshot_id`` 在签发时通常为空字符串：工件一经签名即不可变，
    快照归属由 ``ShadowActivationSnapshot`` 侧记录，绝不回填修改工件。
    """

    activation_id: str
    proposal_id: str
    proposal_content_fingerprint: str
    eligibility_decision_id: str
    eligibility_decision_fingerprint: str
    scoped_source_identity_key: str
    candidate_canonical_id: str
    dataset_identity: str
    expected_baseline_fingerprint: str
    authorized_scope: str
    authorizer: str
    authorized_at: str
    expires_at: str
    eligibility_evaluator_version: str
    policy_version: str
    authorization_schema_version: str
    signature_algorithm: str
    signer_public_key_id: str
    signature: str
    revocation_reference: str = ""
    purpose: str = ""
    operator: str = ""
    approval_fingerprint: str = ""
    runtime_snapshot_id: str = ""
    reason: str = ""

    # -- 序列化 -----------------------------------------------------------

    def as_dict(self) -> dict[str, str]:
        """稳定序列化：全部字段（含签名），值为字符串。"""
        return {
            field.name: str(getattr(self, field.name))
            for field in fields(self)
        }

    def signing_payload(self) -> bytes:
        """签名载荷：工件去掉 ``signature`` 后的规范 JSON（UTF-8）。"""
        payload = {k: v for k, v in self.as_dict().items() if k != "signature"}
        return canonical_json(payload).encode("utf-8")

    def fingerprint(self) -> str:
        """整件工件的确定性指纹（含签名）。"""
        return fingerprint(self.as_dict())

    def to_json(self) -> str:
        return canonical_json(self.as_dict())

    def identity_payload(self) -> dict[str, str]:
        """身份载荷：不含签名的字段（便于审计比对）。"""
        return {k: v for k, v in self.as_dict().items() if k != "signature"}

    def identity_fingerprint(self) -> str:
        return fingerprint(self.identity_payload())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RuntimeActivationAuthorization:
        """由字典重建工件；缺少必填字段即拒绝。"""
        if not isinstance(payload, Mapping):
            raise ActivationAuthorizationError(
                "activation authorization must be an object",
                rejection_codes=("MALFORMED_ARTIFACT",),
            )
        names = {field.name for field in fields(cls)}
        missing = sorted(
            name
            for name in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS
            if name not in payload
        )
        if missing:
            raise ActivationAuthorizationError(
                f"activation authorization is missing required fields: {missing}",
                rejection_codes=("MALFORMED_ARTIFACT",),
            )
        return cls(**{name: str(payload[name]) for name in names if name in payload})


#: 工件的全部字段名（声明顺序）。
ARTIFACT_FIELD_NAMES: tuple[str, ...] = tuple(
    field.name for field in fields(RuntimeActivationAuthorization)
)


# =============================================================================
# 时间工具
# =============================================================================

def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# =============================================================================
# 签发（合成密钥；只读载入私钥）
# =============================================================================

def generate_activation_authorizer_keypair(
    private_key_path: str, public_key_path: str
) -> dict:
    """
    生成一对**合成**授权人 Ed25519 密钥（仓库之外）。

    刻意复用既有密钥生成原语（同一 RFC 8032 实现 + 同一「私钥必须在仓库外」守卫），
    避免重复实现密码学或放宽密钥位置约束。实际部署应使用**独立于映射所有者**的密钥。
    """
    return generate_mapping_owner_keypair(private_key_path, public_key_path)


def build_activation_authorization_body(
    *,
    decision: RuntimeMappingEligibilityDecision,
    proposal: MappingProposal,
    dataset_identity: str,
    authorized_scope: str,
    authorizer: str,
    operator: str,
    authorized_at: str,
    expires_at: str,
    revocation_reference: str = "",
    approval_fingerprint: str = "",
    reason: str = "",
    activation_id: str | None = None,
) -> dict[str, str]:
    """
    组装**未签名**的授权主体。

    ``activation_id`` 缺省时由内容确定性派生（不含时钟、不含随机数）。
    """
    if not isinstance(decision, RuntimeMappingEligibilityDecision):
        raise ActivationAuthorizationError(
            "an eligibility decision is required",
            rejection_codes=("ELIGIBILITY_DECISION_MISSING",),
        )
    if not isinstance(proposal, MappingProposal):
        raise ActivationAuthorizationError(
            "a mapping proposal is required",
            rejection_codes=("PROPOSAL_MISSING",),
        )

    body = {
        "activation_id": "",
        "proposal_id": proposal.proposal_id,
        "proposal_content_fingerprint": proposal.content_fingerprint(),
        "eligibility_decision_id": decision.decision_id,
        "eligibility_decision_fingerprint": fingerprint(decision.as_dict()),
        "scoped_source_identity_key": proposal.source_key,
        "candidate_canonical_id": str(proposal.candidate_canonical_id or ""),
        "dataset_identity": dataset_identity,
        "expected_baseline_fingerprint": proposal.expected_baseline_fingerprint,
        "authorized_scope": authorized_scope,
        "authorizer": authorizer,
        "authorized_at": authorized_at,
        "expires_at": expires_at,
        "eligibility_evaluator_version": decision.evaluator_version,
        "policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "authorization_schema_version": RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION,
        "signature_algorithm": RUNTIME_ACTIVATION_SIGNATURE_ALGORITHM,
        "signer_public_key_id": "",
        "signature": "",
        "revocation_reference": revocation_reference,
        "purpose": RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE,
        "operator": operator,
        "approval_fingerprint": approval_fingerprint,
        "runtime_snapshot_id": "",
        "reason": reason,
    }
    if activation_id is None:
        activation_id = fingerprint(
            {k: v for k, v in body.items() if k not in ("signature", "signer_public_key_id")}
        )
    body["activation_id"] = str(activation_id)
    return body


def _assert_body_is_signable(body: Mapping[str, Any]) -> None:
    missing = [
        name
        for name in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS
        if name not in body
    ]
    if missing:
        raise ActivationAuthorizationError(
            f"activation authorization body is missing fields: {sorted(missing)}",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )
    # ``signature`` / ``signer_public_key_id`` 由签名步骤本身产生，此处不要求非空；
    # 其余必填字段（除显式允许为空者）必须非空。
    blank = sorted(
        name
        for name in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS
        if name not in MAY_BE_EMPTY_FIELDS
        and name not in ("signature", "signer_public_key_id")
        and not str(body.get(name) or "").strip()
    )
    if blank:
        raise ActivationAuthorizationError(
            f"activation authorization body has blank required fields: {blank}",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )

    scope = str(body.get("authorized_scope") or "")
    if scope not in PROTOTYPE_ALLOWED_ACTIVATION_SCOPES:
        raise ActivationAuthorizationError(
            f"activation scope {scope!r} is not an allowed prototype scope",
            rejection_codes=("ACTIVATION_SCOPE_UNSUPPORTED",),
        )
    upper = scope.upper()
    for token in FORBIDDEN_SCOPE_TOKENS:
        if token in upper:
            raise ActivationAuthorizationError(
                f"activation scope {scope!r} carries write/mutation semantics",
                rejection_codes=("ACTIVATION_SCOPE_WRITE_FORBIDDEN",),
            )

    authorized_at = _parse_timestamp(str(body.get("authorized_at") or ""))
    expires_at = _parse_timestamp(str(body.get("expires_at") or ""))
    if authorized_at is None or expires_at is None:
        raise ActivationAuthorizationError(
            "activation authorization requires parsable authorized_at and expires_at",
            rejection_codes=("MALFORMED_TIMESTAMP",),
        )
    if expires_at <= authorized_at:
        raise ActivationAuthorizationError(
            "activation authorization expiry must be strictly after authorized_at",
            rejection_codes=("MALFORMED_TIMESTAMP",),
        )


def sign_runtime_activation_authorization(
    private_key_path: str,
    body: Mapping[str, Any],
) -> RuntimeActivationAuthorization:
    """
    用**合成**授权人私钥对授权主体做分离签名（RFC 8032 Ed25519）。

    私钥必须位于仓库之外；缺失 ``cryptography`` 时 fail closed。
    """
    _assert_body_is_signable(body)
    backend = _crypto_backend()
    serialization = backend["serialization"]

    private_norm = _assert_private_key_path_is_external(private_key_path)
    with open(private_norm, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, backend["Ed25519PrivateKey"]):
        raise ActivationAuthorizationError(
            "activation authorizer private key is not an Ed25519 private key",
            rejection_codes=("SIGNATURE_ALGORITHM_UNSUPPORTED",),
        )

    raw_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = dict(body)
    payload["signer_public_key_id"] = public_key_id(raw_public)
    payload["signature"] = ""
    payload["signature_algorithm"] = RUNTIME_ACTIVATION_SIGNATURE_ALGORITHM

    signing_bytes = canonical_json(
        {k: v for k, v in payload.items() if k != "signature"}
    ).encode("utf-8")
    signature = key.sign(signing_bytes)
    payload["signature"] = base64.b64encode(signature).decode("ascii")
    artifact = RuntimeActivationAuthorization.from_dict(payload)
    if not artifact.signature.strip() or not artifact.signer_public_key_id.strip():
        raise ActivationAuthorizationError(
            "signed activation authorization must carry a signature and signer key id",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )
    return artifact


# =============================================================================
# 验证
# =============================================================================

@dataclass(frozen=True)
class ActivationVerificationContext:
    """验证所需的**全部**只读事实（不可变；无写入能力）。"""

    dataset_identity: str
    current_baseline_fingerprint: str
    current_time: str
    trusted_public_key_path: str | None = None
    eligibility_decision: RuntimeMappingEligibilityDecision | None = None
    proposal: MappingProposal | None = None
    #: 撤销登记：``(activation_id, revocation_reference)``。
    revocation_registry: tuple[tuple[str, str], ...] = ()
    expected_purpose: str = RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE
    expected_schema_version: str = RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {
            "dataset_identity": self.dataset_identity,
            "current_baseline_fingerprint": self.current_baseline_fingerprint,
            "current_time": self.current_time,
            "trust_anchor_supplied": self.trusted_public_key_path is not None,
            "eligibility_decision_id": (
                self.eligibility_decision.decision_id
                if self.eligibility_decision is not None
                else None
            ),
            "proposal_id": (
                self.proposal.proposal_id if self.proposal is not None else None
            ),
            "revocation_registry": [list(pair) for pair in self.revocation_registry],
            "expected_purpose": self.expected_purpose,
            "expected_schema_version": self.expected_schema_version,
        }


@dataclass(frozen=True)
class AuthorizationVerificationResult:
    """授权验证结果（不可变、可哈希、确定性）。"""

    activation_id: str
    verification_status: str
    rejection_codes: tuple[str, ...]
    authorization_fingerprint: str
    authorization_identity_fingerprint: str
    verified_scope: str
    eligibility_decision_fingerprint: str
    dataset_identity: str
    baseline_fingerprint: str
    verified_at: str

    @property
    def is_verified(self) -> bool:
        return self.verification_status == "VERIFIED"

    @property
    def is_consumable(self) -> bool:
        """是否可被影子快照消费（已验证且无过期 / 撤销类拒绝码）。"""
        return self.is_verified and not (
            set(self.rejection_codes) & NON_CONSUMABLE_REJECTION_CODES
        )

    def as_dict(self) -> dict:
        payload = {
            "activation_id": self.activation_id,
            "verification_status": self.verification_status,
            "rejection_codes": list(self.rejection_codes),
            "authorization_fingerprint": self.authorization_fingerprint,
            "authorization_identity_fingerprint": self.authorization_identity_fingerprint,
            "verified_scope": self.verified_scope,
            "eligibility_decision_fingerprint": self.eligibility_decision_fingerprint,
            "dataset_identity": self.dataset_identity,
            "baseline_fingerprint": self.baseline_fingerprint,
            "verified_at": self.verified_at,
        }
        payload["is_verified"] = self.is_verified
        payload["is_consumable"] = self.is_consumable
        return payload

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def _collect_rejections(
    authorization: RuntimeActivationAuthorization,
    context: ActivationVerificationContext,
) -> list[str]:
    """收集**全部**拒绝码（不做短路，便于一次性诊断）。"""
    rejections: list[str] = []

    # --- schema / 用途 / 算法 ---------------------------------------------
    if authorization.authorization_schema_version != context.expected_schema_version:
        rejections.append("SCHEMA_VERSION_UNSUPPORTED")
    if authorization.purpose != context.expected_purpose:
        rejections.append("PURPOSE_MISMATCH")
    if authorization.signature_algorithm != RUNTIME_ACTIVATION_SIGNATURE_ALGORITHM:
        rejections.append("SIGNATURE_ALGORITHM_UNSUPPORTED")

    # --- 范围 -------------------------------------------------------------
    scope = str(authorization.authorized_scope or "")
    if scope not in PROTOTYPE_ALLOWED_ACTIVATION_SCOPES:
        rejections.append("ACTIVATION_SCOPE_UNSUPPORTED")
    upper = scope.upper()
    if any(token in upper for token in FORBIDDEN_SCOPE_TOKENS):
        rejections.append("ACTIVATION_SCOPE_WRITE_FORBIDDEN")

    # --- 信任锚与签名 ------------------------------------------------------
    if not context.trusted_public_key_path:
        rejections.append("TRUST_ANCHOR_MISSING")
    else:
        try:
            trusted = load_trusted_public_key(context.trusted_public_key_path)
        except Exception:  # noqa: BLE001
            rejections.append("SIGNER_KEY_UNTRUSTED")
        else:
            if authorization.signer_public_key_id != trusted["public_key_id"]:
                rejections.append("SIGNER_KEY_UNTRUSTED")
            else:
                try:
                    signature = base64.b64decode(
                        str(authorization.signature), validate=True
                    )
                    trusted["key"].verify(signature, authorization.signing_payload())
                except Exception:  # noqa: BLE001
                    rejections.append("SIGNATURE_INVALID")

    # --- 资格判定绑定 ------------------------------------------------------
    decision = context.eligibility_decision
    if not isinstance(decision, RuntimeMappingEligibilityDecision):
        rejections.append("ELIGIBILITY_DECISION_MISSING")
    else:
        if authorization.eligibility_decision_id != decision.decision_id:
            rejections.append("ELIGIBILITY_DECISION_ID_MISMATCH")
        if authorization.eligibility_decision_fingerprint != fingerprint(
            decision.as_dict()
        ):
            rejections.append("ELIGIBILITY_DECISION_FINGERPRINT_MISMATCH")
        if authorization.eligibility_evaluator_version != decision.evaluator_version:
            rejections.append("ELIGIBILITY_EVALUATOR_VERSION_MISMATCH")
        if decision.eligibility_result != RuntimeEligibilityResult.ELIGIBLE.value:
            rejections.append("ELIGIBILITY_NOT_ELIGIBLE")

    if authorization.policy_version != RUNTIME_MAPPING_POLICY_VERSION:
        rejections.append("POLICY_VERSION_MISMATCH")

    # --- 提案绑定 ---------------------------------------------------------
    proposal = context.proposal
    if not isinstance(proposal, MappingProposal):
        rejections.append("PROPOSAL_MISSING")
    else:
        if authorization.proposal_id != proposal.proposal_id:
            rejections.append("PROPOSAL_ID_MISMATCH")
        if authorization.proposal_content_fingerprint != proposal.content_fingerprint():
            rejections.append("PROPOSAL_FINGERPRINT_MISMATCH")
        if authorization.scoped_source_identity_key != proposal.source_key:
            rejections.append("SCOPED_SOURCE_IDENTITY_KEY_MISMATCH")

    # --- 数据集与基线 -----------------------------------------------------
    if authorization.dataset_identity != context.dataset_identity:
        rejections.append("DATASET_IDENTITY_MISMATCH")
    if (
        authorization.expected_baseline_fingerprint
        != context.current_baseline_fingerprint
    ):
        rejections.append("BASELINE_STALE")

    # --- 有效期 -----------------------------------------------------------
    authorized_at = _parse_timestamp(authorization.authorized_at)
    expires_at = _parse_timestamp(authorization.expires_at)
    now = _parse_timestamp(context.current_time)
    if authorized_at is None or expires_at is None or now is None:
        rejections.append("MALFORMED_TIMESTAMP")
    else:
        if now < authorized_at:
            rejections.append("NOT_YET_VALID")
        if now >= expires_at:
            rejections.append("ACTIVATION_AUTHORIZATION_EXPIRED")

    # --- 撤销 -------------------------------------------------------------
    for activation_id, reference in context.revocation_registry:
        if str(activation_id) != authorization.activation_id:
            continue
        if str(reference) == str(authorization.revocation_reference):
            rejections.append("ACTIVATION_AUTHORIZATION_REVOKED")
        else:
            rejections.append("REVOCATION_REFERENCE_MISMATCH")

    return rejections


def verify_runtime_activation_authorization(
    authorization: RuntimeActivationAuthorization | Mapping[str, Any] | None,
    context: ActivationVerificationContext,
) -> AuthorizationVerificationResult:
    """
    **只读**验证一份运行期激活授权。

    检查：签名（可信公钥 / Ed25519 / 用途 / schema）、资格绑定（判定存在、指纹、
    评估器版本、策略版本）、提案绑定（ID、内容指纹、身份键）、数据集绑定
    （数据集身份、基线）、范围绑定（属于允许列表且不含写入语义）、有效期与撤销。

    绝不写文件、绝不创建绑定、绝不修改任何状态。
    """
    if authorization is None:
        return _rejected_result(
            activation_id="",
            codes=("MALFORMED_ARTIFACT",),
            context=context,
        )
    if isinstance(authorization, Mapping):
        try:
            authorization = RuntimeActivationAuthorization.from_dict(authorization)
        except ActivationAuthorizationError as exc:
            return _rejected_result(
                activation_id=str(authorization.get("activation_id") or ""),
                codes=exc.rejection_codes or ("MALFORMED_ARTIFACT",),
                context=context,
            )
    if not isinstance(authorization, RuntimeActivationAuthorization):
        return _rejected_result(
            activation_id="", codes=("MALFORMED_ARTIFACT",), context=context
        )

    rejections = sorted(set(_collect_rejections(authorization, context)))
    return AuthorizationVerificationResult(
        activation_id=authorization.activation_id,
        verification_status="REJECTED" if rejections else "VERIFIED",
        rejection_codes=tuple(rejections),
        authorization_fingerprint=authorization.fingerprint(),
        authorization_identity_fingerprint=authorization.identity_fingerprint(),
        verified_scope=authorization.authorized_scope if not rejections else "",
        eligibility_decision_fingerprint=(
            authorization.eligibility_decision_fingerprint
        ),
        dataset_identity=authorization.dataset_identity,
        baseline_fingerprint=authorization.expected_baseline_fingerprint,
        verified_at=context.current_time,
    )


def _rejected_result(
    *,
    activation_id: str,
    codes: Iterable[str],
    context: ActivationVerificationContext,
) -> AuthorizationVerificationResult:
    ordered = tuple(sorted({str(code) for code in codes}))
    return AuthorizationVerificationResult(
        activation_id=activation_id,
        verification_status="REJECTED",
        rejection_codes=ordered,
        authorization_fingerprint="",
        authorization_identity_fingerprint="",
        verified_scope="",
        eligibility_decision_fingerprint="",
        dataset_identity=context.dataset_identity,
        baseline_fingerprint=context.current_baseline_fingerprint,
        verified_at=context.current_time,
    )


# =============================================================================
# 生命周期（影子专用）
# =============================================================================

class ActivationLifecycleState(str, Enum):
    """激活生命周期状态（影子专用）。"""

    CREATED = "CREATED"
    AUTHORIZED = "AUTHORIZED"
    ACTIVE_SHADOW = "ACTIVE_SHADOW"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    DISABLED = "DISABLED"


#: **唯一权威**的允许迁移表。
ACTIVATION_LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    ActivationLifecycleState.CREATED.value: frozenset(
        {
            ActivationLifecycleState.AUTHORIZED.value,
            ActivationLifecycleState.DISABLED.value,
        }
    ),
    ActivationLifecycleState.AUTHORIZED.value: frozenset(
        {
            ActivationLifecycleState.ACTIVE_SHADOW.value,
            ActivationLifecycleState.EXPIRED.value,
            ActivationLifecycleState.REVOKED.value,
            ActivationLifecycleState.DISABLED.value,
        }
    ),
    ActivationLifecycleState.ACTIVE_SHADOW.value: frozenset(
        {
            ActivationLifecycleState.EXPIRED.value,
            ActivationLifecycleState.REVOKED.value,
            ActivationLifecycleState.DISABLED.value,
        }
    ),
    ActivationLifecycleState.EXPIRED.value: frozenset(
        {ActivationLifecycleState.DISABLED.value}
    ),
    ActivationLifecycleState.REVOKED.value: frozenset(
        {ActivationLifecycleState.DISABLED.value}
    ),
    ActivationLifecycleState.DISABLED.value: frozenset(),
}

#: 唯一允许被影子快照消费的状态。
CONSUMABLE_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {ActivationLifecycleState.ACTIVE_SHADOW.value}
)

#: 不允许激活的状态。
NON_ACTIVATABLE_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        ActivationLifecycleState.EXPIRED.value,
        ActivationLifecycleState.REVOKED.value,
        ActivationLifecycleState.DISABLED.value,
    }
)


def validate_activation_transition(from_state: str, to_state: str) -> None:
    """校验一次生命周期迁移；非法迁移一律拒绝（无强制绕过参数）。"""
    if from_state not in ACTIVATION_LIFECYCLE_TRANSITIONS:
        raise InvalidActivationTransitionError(f"unknown source state {from_state!r}")
    if to_state not in ACTIVATION_LIFECYCLE_TRANSITIONS:
        raise InvalidActivationTransitionError(f"unknown target state {to_state!r}")
    if to_state not in ACTIVATION_LIFECYCLE_TRANSITIONS[from_state]:
        raise InvalidActivationTransitionError(
            f"transition {from_state} -> {to_state} is not permitted"
        )


def derive_lifecycle_state(
    verification: AuthorizationVerificationResult,
) -> ActivationLifecycleState:
    """由验证结论推导生命周期状态（**绝不**自动进入 ACTIVE_SHADOW）。"""
    codes = set(verification.rejection_codes)
    if "ACTIVATION_AUTHORIZATION_REVOKED" in codes or (
        "REVOCATION_REFERENCE_MISMATCH" in codes
    ):
        return ActivationLifecycleState.REVOKED
    if "ACTIVATION_AUTHORIZATION_EXPIRED" in codes:
        return ActivationLifecycleState.EXPIRED
    if verification.is_verified:
        return ActivationLifecycleState.AUTHORIZED
    return ActivationLifecycleState.CREATED


@dataclass(frozen=True)
class ActivationLifecycleRecord:
    """一条激活生命周期记录（不可变）。"""

    activation_id: str
    state: str
    updated_at: str
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "activation_id": self.activation_id,
            "state": self.state,
            "updated_at": self.updated_at,
            "reason": self.reason,
        }


def advance_activation_lifecycle(
    record: ActivationLifecycleRecord,
    to_state: str,
    *,
    at: str,
    reason: str = "",
) -> ActivationLifecycleRecord:
    """按权威迁移表推进生命周期（只读返回新记录，绝不修改输入）。"""
    validate_activation_transition(record.state, to_state)
    return ActivationLifecycleRecord(
        activation_id=record.activation_id,
        state=to_state,
        updated_at=at,
        reason=reason,
    )


# =============================================================================
# 影子审计日志（内存、追加式、哈希链；**不写任何文件**）
# =============================================================================

#: 原型支持的合成审计事件。
SHADOW_AUDIT_EVENT_TYPES: tuple[str, ...] = (
    "ACTIVATION_AUTHORIZATION_CREATED",
    "ACTIVATION_AUTHORIZATION_VERIFIED",
    "ACTIVATION_SHADOW_ENABLED",
    "ACTIVATION_AUTHORIZATION_EXPIRED",
    "ACTIVATION_AUTHORIZATION_REVOKED",
    "ACTIVATION_SHADOW_DISABLED",
)


@dataclass(frozen=True)
class ShadowAuditEvent:
    """一条影子审计事件（不可变、可哈希、确定性）。"""

    sequence: int
    event_type: str
    activation_id: str
    occurred_at: str
    actor: str
    actor_role: str
    payload: tuple[tuple[str, str], ...]
    previous_entry_hash: str
    entry_hash: str

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type,
            "activation_id": self.activation_id,
            "occurred_at": self.occurred_at,
            "actor": self.actor,
            "actor_role": self.actor_role,
            "payload": dict(self.payload),
            "previous_entry_hash": self.previous_entry_hash,
            "entry_hash": self.entry_hash,
        }

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def _audit_entry_hash(payload: Mapping[str, Any]) -> str:
    return fingerprint({k: v for k, v in payload.items() if k != "entry_hash"})


class ShadowActivationAuditLog:
    """
    **内存**追加式哈希链审计日志（原型）。

    刻意**不**写任何文件：既避免与生产审计存储混淆，也让「影子执行绝不写盘」
    成为可结构性证明的性质。哈希链只证明内部一致性，**不是**外部信任锚。
    """

    def __init__(self) -> None:
        self._events: list[ShadowAuditEvent] = []

    # -- 只读视图 ---------------------------------------------------------

    def events(self) -> tuple[ShadowAuditEvent, ...]:
        return tuple(self._events)

    def event_types(self) -> tuple[str, ...]:
        return tuple(event.event_type for event in self._events)

    def chain_tip(self) -> str:
        return self._events[-1].entry_hash if self._events else GENESIS_PREVIOUS_HASH

    def verify(self) -> dict:
        """只读重算哈希链；任何不一致都抛 ``ActivationPrototypeError``。"""
        previous = GENESIS_PREVIOUS_HASH
        for index, event in enumerate(self._events):
            if event.sequence != index:
                raise ActivationPrototypeError(
                    f"shadow audit sequence gap at index {index}"
                )
            if event.previous_entry_hash != previous:
                raise ActivationPrototypeError(
                    f"shadow audit broken chain at index {index}"
                )
            if _audit_entry_hash(event.as_dict()) != event.entry_hash:
                raise ActivationPrototypeError(
                    f"shadow audit entry {index} was modified"
                )
            previous = event.entry_hash
        return {
            "kind": "SHADOW_ACTIVATION_AUDIT_LOG",
            "prototype_only": True,
            "entry_count": len(self._events),
            "chain_tip": previous,
            "integrity": "OK",
            "persisted": False,
            "trust_anchor": (
                "NONE — the hash chain proves internal consistency only; this log is "
                "in-memory, is never persisted, and is not an external trust anchor"
            ),
        }

    # -- 追加 -------------------------------------------------------------

    def append(
        self,
        *,
        event_type: str,
        activation_id: str,
        occurred_at: str,
        actor: str,
        actor_role: str,
        payload: Mapping[str, Any] | None = None,
    ) -> ShadowAuditEvent:
        """追加一条事件（只追加；绝不删除或改写既有条目）。"""
        if event_type not in SHADOW_AUDIT_EVENT_TYPES:
            raise ActivationPrototypeError(
                f"unsupported shadow audit event type {event_type!r}"
            )
        body = {
            "sequence": len(self._events),
            "event_type": event_type,
            "activation_id": str(activation_id),
            "occurred_at": str(occurred_at),
            "actor": str(actor),
            "actor_role": str(actor_role),
            "payload": {str(k): str(v) for k, v in dict(payload or {}).items()},
            "previous_entry_hash": self.chain_tip(),
        }
        entry_hash = _audit_entry_hash(body)
        body["entry_hash"] = entry_hash
        event = ShadowAuditEvent(
            sequence=body["sequence"],
            event_type=body["event_type"],
            activation_id=body["activation_id"],
            occurred_at=body["occurred_at"],
            actor=body["actor"],
            actor_role=body["actor_role"],
            payload=tuple(sorted(body["payload"].items())),
            previous_entry_hash=body["previous_entry_hash"],
            entry_hash=entry_hash,
        )
        self._events.append(event)
        return event


# =============================================================================
# 影子激活快照
# =============================================================================

@dataclass(frozen=True)
class ShadowActivationBinding:
    """快照中的一条绑定：源身份键 -> 候选 Canonical ID。"""

    activation_id: str
    source_identity_key: str
    candidate_canonical_id: str
    eligibility_decision_fingerprint: str
    authorized_scope: str

    def as_dict(self) -> dict:
        return {
            "activation_id": self.activation_id,
            "source_identity_key": self.source_identity_key,
            "candidate_canonical_id": self.candidate_canonical_id,
            "eligibility_decision_fingerprint": self.eligibility_decision_fingerprint,
            "authorized_scope": self.authorized_scope,
        }


@dataclass(frozen=True)
class ShadowActivationSnapshot:
    """
    **影子**激活快照（不可变、只读、确定性）。

    它**不是**生产运行期快照：没有任何生产代码读取它，``refresh()`` 从不引用它。
    """

    snapshot_id: str
    snapshot_fingerprint: str
    activation_ids: tuple[str, ...]
    baseline_fingerprint: str
    policy_version: str
    evaluator_version: str
    generated_at: str
    status: str
    dataset_identity: str = ""
    bindings: tuple[ShadowActivationBinding, ...] = ()

    @property
    def is_consumable(self) -> bool:
        return self.status in CONSUMABLE_LIFECYCLE_STATES

    def resolve(self, source_identity_key: str) -> ShadowActivationBinding | None:
        matches = [
            binding
            for binding in self.bindings
            if binding.source_identity_key == source_identity_key
        ]
        if len(matches) != 1:
            return None
        return matches[0]

    def as_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "activation_ids": list(self.activation_ids),
            "baseline_fingerprint": self.baseline_fingerprint,
            "policy_version": self.policy_version,
            "evaluator_version": self.evaluator_version,
            "generated_at": self.generated_at,
            "status": self.status,
            "dataset_identity": self.dataset_identity,
            "bindings": [binding.as_dict() for binding in self.bindings],
            "prototype_only": True,
            "is_production_authority": False,
        }

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def build_shadow_activation_snapshot(
    *,
    authorizations: Sequence[RuntimeActivationAuthorization],
    verification_context: ActivationVerificationContext | None = None,
    verification_contexts: Sequence[ActivationVerificationContext] | None = None,
    observations: Sequence[SourceObservation],
    generated_at: str,
    dataset_identity: str = "",
    status: str = ActivationLifecycleState.ACTIVE_SHADOW.value,
) -> ShadowActivationSnapshot:
    """
    构建**影子**激活快照（只读、确定性、不写任何文件）。

    每条授权都必须**对它自己的**已记录事实进行验证：可以传入单个
    ``verification_context``（对全部授权适用），或与 ``authorizations`` 按下标
    对齐的 ``verification_contexts``（多条授权时使用）。

    拒绝（``ShadowActivationSnapshotError``，附拒绝码）：
    签名无效、资格绑定无效、基线陈旧、授权过期或撤销、状态不可消费、
    绑定冲突（同一源身份键指向不同 Canonical ID）、绑定缺少对应观测。

    传入空授权序列是**合法**的：它产生一个没有绑定的快照（这正是「紧急停用」
    应当发布的内容），任何解析都只会得到 ``NOT_FOUND``。
    """
    if verification_context is None and verification_contexts is None:
        raise ShadowActivationSnapshotError(
            "a verification context (or per-authorization contexts) is required",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )
    if verification_context is not None and verification_contexts is not None:
        raise ShadowActivationSnapshotError(
            "supply either verification_context or verification_contexts, not both",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )
    if verification_contexts is not None and len(verification_contexts) != len(
        authorizations
    ):
        raise ShadowActivationSnapshotError(
            "verification_contexts must align with authorizations by index",
            rejection_codes=("MALFORMED_ARTIFACT",),
        )

    rejections: list[str] = []
    bindings: list[ShadowActivationBinding] = []

    if status not in CONSUMABLE_LIFECYCLE_STATES:
        rejections.append("SNAPSHOT_STATUS_NOT_CONSUMABLE")

    observed_keys = {
        ScopedSourceIdentityKey.for_observation(observation).source_key()
        for observation in observations
        if isinstance(observation, SourceObservation)
    }

    for index, authorization in enumerate(authorizations):
        context = (
            verification_context
            if verification_context is not None
            else verification_contexts[index]  # type: ignore[index]
        )
        verification = verify_runtime_activation_authorization(authorization, context)
        if not verification.is_consumable:
            rejections.extend(verification.rejection_codes)
            continue
        if authorization.scoped_source_identity_key not in observed_keys:
            rejections.append("SHADOW_BINDING_OBSERVATION_MISSING")
            continue
        bindings.append(
            ShadowActivationBinding(
                activation_id=authorization.activation_id,
                source_identity_key=authorization.scoped_source_identity_key,
                candidate_canonical_id=authorization.candidate_canonical_id,
                eligibility_decision_fingerprint=(
                    authorization.eligibility_decision_fingerprint
                ),
                authorized_scope=authorization.authorized_scope,
            )
        )

    # 同一源身份键必须唯一指向一个 Canonical ID。
    by_key: dict[str, set[str]] = {}
    for binding in bindings:
        by_key.setdefault(binding.source_identity_key, set()).add(
            binding.candidate_canonical_id
        )
    if any(len(targets) > 1 for targets in by_key.values()):
        rejections.append("AMBIGUOUS_SHADOW_BINDING")

    if rejections:
        raise ShadowActivationSnapshotError(
            "shadow activation snapshot rejected: " + ", ".join(sorted(set(rejections))),
            rejection_codes=tuple(sorted(set(rejections))),
        )

    ordered_bindings = tuple(sorted(bindings, key=lambda b: b.source_identity_key))
    payload = {
        "kind": "SHADOW_ACTIVATION_SNAPSHOT",
        "prototype_only": True,
        "activation_ids": sorted(binding.activation_id for binding in ordered_bindings),
        "baseline_fingerprint": (
            verification_context.current_baseline_fingerprint
            if verification_context is not None
            else verification_contexts[0].current_baseline_fingerprint  # type: ignore[index]
        ),
        "policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "generated_at": str(generated_at),
        "status": status,
        "dataset_identity": dataset_identity
        or (
            verification_context.dataset_identity
            if verification_context is not None
            else verification_contexts[0].dataset_identity  # type: ignore[index]
        ),
        "bindings": [binding.as_dict() for binding in ordered_bindings],
    }
    snapshot_fingerprint = fingerprint(payload)
    return ShadowActivationSnapshot(
        snapshot_id=snapshot_fingerprint,
        snapshot_fingerprint=snapshot_fingerprint,
        activation_ids=tuple(payload["activation_ids"]),
        baseline_fingerprint=payload["baseline_fingerprint"],
        policy_version=payload["policy_version"],
        evaluator_version=payload["evaluator_version"],
        generated_at=payload["generated_at"],
        status=payload["status"],
        dataset_identity=payload["dataset_identity"],
        bindings=ordered_bindings,
    )


# =============================================================================
# 影子运行期评估
# =============================================================================

class ShadowResolutionStatus(str, Enum):
    """影子解析状态。只有三种取值。"""

    RESOLVED_SHADOW = "RESOLVED_SHADOW"
    REJECTED_SHADOW = "REJECTED_SHADOW"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class ShadowRuntimeResolutionResult:
    """
    影子运行期解析结果。

    回答的是「**如果**这个激活快照存在，运行期解析会观察到什么？」，
    而**不是**「应用这个映射」。
    """

    source_identity_key: str
    candidate_canonical_id: str
    resolution_status: str
    snapshot_id: str
    decision_fingerprint: str

    @property
    def is_resolved(self) -> bool:
        return self.resolution_status == ShadowResolutionStatus.RESOLVED_SHADOW.value

    def as_dict(self) -> dict:
        return {
            "source_identity_key": self.source_identity_key,
            "candidate_canonical_id": self.candidate_canonical_id,
            "resolution_status": self.resolution_status,
            "snapshot_id": self.snapshot_id,
            "decision_fingerprint": self.decision_fingerprint,
            "prototype_only": True,
            "is_production_authority": False,
        }

    def to_json(self) -> str:
        return canonical_json(self.as_dict())


def evaluate_shadow_runtime_mapping(
    *,
    snapshot: ShadowActivationSnapshot,
    observation: SourceObservation | None,
) -> ShadowRuntimeResolutionResult:
    """
    **只读**影子运行期解析。

    绝不修改数据、绝不修复身份、绝不持久化、绝不改写 Canonical ID。
    """
    if not isinstance(snapshot, ShadowActivationSnapshot):
        return ShadowRuntimeResolutionResult(
            source_identity_key="",
            candidate_canonical_id="",
            resolution_status=ShadowResolutionStatus.REJECTED_SHADOW.value,
            snapshot_id="",
            decision_fingerprint="",
        )
    if not snapshot.is_consumable:
        return ShadowRuntimeResolutionResult(
            source_identity_key="",
            candidate_canonical_id="",
            resolution_status=ShadowResolutionStatus.REJECTED_SHADOW.value,
            snapshot_id=snapshot.snapshot_id,
            decision_fingerprint="",
        )
    if not isinstance(observation, SourceObservation):
        return ShadowRuntimeResolutionResult(
            source_identity_key="",
            candidate_canonical_id="",
            resolution_status=ShadowResolutionStatus.REJECTED_SHADOW.value,
            snapshot_id=snapshot.snapshot_id,
            decision_fingerprint="",
        )

    key = ScopedSourceIdentityKey.for_observation(observation)
    source_key = key.source_key()
    binding = snapshot.resolve(source_key)
    if binding is None:
        return ShadowRuntimeResolutionResult(
            source_identity_key=source_key,
            candidate_canonical_id="",
            resolution_status=ShadowResolutionStatus.NOT_FOUND.value,
            snapshot_id=snapshot.snapshot_id,
            decision_fingerprint="",
        )
    return ShadowRuntimeResolutionResult(
        source_identity_key=source_key,
        candidate_canonical_id=binding.candidate_canonical_id,
        resolution_status=ShadowResolutionStatus.RESOLVED_SHADOW.value,
        snapshot_id=snapshot.snapshot_id,
        decision_fingerprint=binding.eligibility_decision_fingerprint,
    )


# =============================================================================
# 只读状态视图
# =============================================================================

def shadow_activation_prototype_status() -> dict:
    """原型与激活边界的只读状态视图（供审计与报告）。"""
    return {
        "prototype_only": PROTOTYPE_ONLY,
        "activation_authorization_schema_version": (
            RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION
        ),
        "activation_authorization_artifact": RUNTIME_ACTIVATION_AUTHORIZATION_ARTIFACT,
        "activation_authorization_purpose": RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE,
        "signature_algorithm": RUNTIME_ACTIVATION_SIGNATURE_ALGORITHM,
        "allowed_activation_scopes": list(PROTOTYPE_ALLOWED_ACTIVATION_SCOPES),
        "lifecycle_states": [state.value for state in ActivationLifecycleState],
        "consumable_lifecycle_states": sorted(CONSUMABLE_LIFECYCLE_STATES),
        "audit_event_types": list(SHADOW_AUDIT_EVENT_TYPES),
        "audit_persisted": False,
        "rejection_code_count": len(AUTHORIZATION_REJECTION_CODE_SET),
        "production_activation": PRODUCTION_ACTIVATION,
        "production_runtime_reader": PRODUCTION_RUNTIME_READER,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "writes_files": False,
        "creates_bindings": False,
        "modifies_canonical_records": False,
        "consumed_by_production_runtime": False,
    }


# =============================================================================
# 导入时自洽性断言（fail closed）
# =============================================================================

def _assert_prototype_is_coherent() -> None:
    """导入时断言原型契约自洽。"""
    if not PROTOTYPE_ONLY:
        raise AssertionError("activation prototype must declare itself prototype-only")
    if PRODUCTION_ACTIVATION != "NOT_ENABLED":
        raise AssertionError("runtime activation must remain NOT_ENABLED")
    if PRODUCTION_RUNTIME_READER != "NOT_ENABLED":
        raise AssertionError("the runtime reader must remain NOT_ENABLED")

    # 用途域分离：激活授权用途必须与映射批准用途不同。
    from utils.mapping_governance import MAPPING_APPROVAL_PURPOSE

    if RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE == MAPPING_APPROVAL_PURPOSE:
        raise AssertionError(
            "activation authorization purpose must be domain-separated from approval"
        )

    # 范围必须非空、只读，且不含写入语义。
    if not PROTOTYPE_ALLOWED_ACTIVATION_SCOPES:
        raise AssertionError("at least one activation scope must be allowed")
    for scope in PROTOTYPE_ALLOWED_ACTIVATION_SCOPES:
        upper = scope.upper()
        for token in FORBIDDEN_SCOPE_TOKENS:
            if token in upper:
                raise AssertionError(
                    f"allowed activation scope {scope!r} carries write semantics"
                )

    # 工件字段必须覆盖设计契约声明的全部必填字段。
    declared = set(ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS)
    implemented = set(ARTIFACT_FIELD_NAMES)
    missing = declared - implemented
    if missing:
        raise AssertionError(
            f"activation artifact is missing design-declared fields: {sorted(missing)}"
        )

    # 生命周期：只有 ACTIVE_SHADOW 可消费；不可激活状态不得可消费。
    if CONSUMABLE_LIFECYCLE_STATES != {ActivationLifecycleState.ACTIVE_SHADOW.value}:
        raise AssertionError("only ACTIVE_SHADOW may be consumable")
    if CONSUMABLE_LIFECYCLE_STATES & NON_ACTIVATABLE_LIFECYCLE_STATES:
        raise AssertionError(
            "expired/revoked/disabled states must never be consumable"
        )
    for state in ActivationLifecycleState:
        if state.value not in ACTIVATION_LIFECYCLE_TRANSITIONS:
            raise AssertionError(f"lifecycle state {state.value!r} has no transitions")

    # 拒绝码唯一。
    codes = [code for code, _ in AUTHORIZATION_REJECTION_CODES]
    if len(set(codes)) != len(codes):
        raise AssertionError("activation rejection codes must be unique")
    if not NON_CONSUMABLE_REJECTION_CODES <= AUTHORIZATION_REJECTION_CODE_SET:
        raise AssertionError("non-consumable codes must be declared rejection codes")

    # 审计事件类型唯一。
    if len(set(SHADOW_AUDIT_EVENT_TYPES)) != len(SHADOW_AUDIT_EVENT_TYPES):
        raise AssertionError("shadow audit event types must be unique")


_assert_prototype_is_coherent()
