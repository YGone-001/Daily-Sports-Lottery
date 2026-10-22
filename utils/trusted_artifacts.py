"""
通用签名信任工件（隔离原型，不构成生产授权）
============================================
本模块为运行期激活信任边界提供**统一的 Ed25519 签名工件**原语：

- 外部治理账本检查点（链尖 + 长度 + 时间）；
- 权威 Canonical 基线清单；
- 激活撤销清单；
- 权威治理视图证明；
- 源观测来源证明；
- 影子恢复证据。

刻意复用既有 ``cryptography`` 后端与「私钥必须在仓库外」守卫，不实现第二套密码学，
也不生成任何生产密钥。所有工件都带 ``artifact_type`` 做域分离，签名覆盖类型 + 载荷。
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    GovernanceError,
    _assert_private_key_path_is_external,
    _crypto_backend,
    load_trusted_public_key,
    public_key_id,
)
from utils.source_identity import canonical_json, fingerprint

#: 签名工件 schema 版本。
SIGNED_ARTIFACT_SCHEMA_VERSION = "1.0.0"

#: 支持的工件类型（域分离）。
TRUSTED_ARTIFACT_TYPES: tuple[str, ...] = (
    "GOVERNANCE_LEDGER_CHECKPOINT",
    "CANONICAL_BASELINE_MANIFEST",
    "ACTIVATION_REVOCATION_MANIFEST",
    "GOVERNANCE_VIEW_ATTESTATION",
    "SOURCE_OBSERVATION_ATTESTATION",
    "SHADOW_AUDIT_CHAIN_CHECKPOINT",
    "SHADOW_RECOVERY_EVIDENCE",
)

#: 签名长度（Ed25519）。
SIGNATURE_BYTES = 64

#: 拒绝码。
TRUSTED_ARTIFACT_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("TRUSTED_ARTIFACT_MALFORMED", "签名工件缺失或结构错误。"),
    ("TRUSTED_ARTIFACT_TYPE_MISMATCH", "工件类型与所需用途不一致。"),
    ("TRUSTED_ARTIFACT_SCHEMA_UNSUPPORTED", "工件 schema 版本不受支持。"),
    ("TRUSTED_ARTIFACT_TRUST_ANCHOR_MISSING", "未显式提供可信公钥。"),
    ("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED", "签名密钥与可信公钥不一致。"),
    ("TRUSTED_ARTIFACT_SIGNATURE_INVALID", "Ed25519 签名验证失败。"),
    ("TRUSTED_ARTIFACT_STALE", "工件已超过允许的新鲜度窗口。"),
    ("TRUSTED_ARTIFACT_FROM_FUTURE", "工件签发时间晚于受控当前时间。"),
)


class TrustedArtifactError(GovernanceError):
    """签名信任工件错误（fail closed）。"""

    def __init__(self, message: str, *, rejection_codes: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.rejection_codes = tuple(str(code) for code in rejection_codes)


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


@dataclass(frozen=True)
class SignedTrustedArtifact:
    """一份 Ed25519 分离签名的信任工件（不可变）。"""

    artifact_type: str
    payload_json: str
    signer_public_key_id: str
    signature: str
    schema_version: str = SIGNED_ARTIFACT_SCHEMA_VERSION

    # -- 视图 -------------------------------------------------------------

    def payload(self) -> dict:
        try:
            return json.loads(self.payload_json)
        except ValueError as exc:  # pragma: no cover - 构造期已校验
            raise TrustedArtifactError(
                "trusted artifact payload is not valid JSON",
                rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
            ) from exc

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "payload": self.payload(),
            "signer_public_key_id": self.signer_public_key_id,
            "signature": self.signature,
            "prototype_only": True,
            "is_production_authority": False,
        }

    def signing_payload(self) -> bytes:
        body = self.as_dict()
        body.pop("signature", None)
        body.pop("prototype_only", None)
        body.pop("is_production_authority", None)
        return canonical_json(body).encode("utf-8")

    def fingerprint(self) -> str:
        return fingerprint(self.as_dict())

    def to_json(self) -> str:
        return canonical_json(self.as_dict())

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SignedTrustedArtifact":
        if not isinstance(payload, Mapping):
            raise TrustedArtifactError(
                "trusted artifact must be an object",
                rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
            )
        names = {field.name for field in fields(cls)}
        missing = sorted(name for name in names if name not in payload)
        if missing:
            raise TrustedArtifactError(
                f"trusted artifact is missing fields: {missing}",
                rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
            )
        raw_payload = payload.get("payload")
        if not isinstance(raw_payload, (Mapping, list, str, int, float, bool)):
            raise TrustedArtifactError(
                "trusted artifact payload must be canonical JSON data",
                rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
            )
        return cls(
            artifact_type=str(payload["artifact_type"]),
            payload_json=canonical_json(raw_payload),
            signer_public_key_id=str(payload["signer_public_key_id"]),
            signature=str(payload["signature"]),
            schema_version=str(payload["schema_version"]),
        )


def sign_trusted_artifact(
    private_key_path: str,
    *,
    artifact_type: str,
    payload: Mapping[str, Any],
) -> SignedTrustedArtifact:
    """
    用仓库外的 Ed25519 私钥签发一份域分离的信任工件。

    私钥位置沿用既有守卫：仓库内路径一律拒绝。
    """
    if artifact_type not in TRUSTED_ARTIFACT_TYPES:
        raise TrustedArtifactError(
            f"unsupported trusted artifact type {artifact_type!r}",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    backend = _crypto_backend()
    serialization = backend["serialization"]
    private_norm = _assert_private_key_path_is_external(private_key_path)
    with open(private_norm, "rb") as handle:
        key = serialization.load_pem_private_key(handle.read(), password=None)
    if not isinstance(key, backend["Ed25519PrivateKey"]):
        raise TrustedArtifactError(
            "trusted artifact signer key is not Ed25519",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED",),
        )
    raw_public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    unsigned = SignedTrustedArtifact(
        artifact_type=artifact_type,
        payload_json=canonical_json(dict(payload)),
        signer_public_key_id=public_key_id(raw_public),
        signature="",
    )
    signature = key.sign(unsigned.signing_payload())
    return SignedTrustedArtifact(
        artifact_type=unsigned.artifact_type,
        payload_json=unsigned.payload_json,
        signer_public_key_id=unsigned.signer_public_key_id,
        signature=base64.b64encode(signature).decode("ascii"),
    )


def verify_trusted_artifact(
    artifact: SignedTrustedArtifact | Mapping[str, Any] | None,
    *,
    expected_artifact_type: str,
    trusted_public_key_path: str | None,
    current_time: str,
    max_age_seconds: float,
) -> dict:
    """
    **只读**验证签名工件：类型、schema、可信密钥、Ed25519、新鲜度。

    返回工件载荷；任何失败都抛 ``TrustedArtifactError``。
    """
    if artifact is None:
        raise TrustedArtifactError(
            "a signed trusted artifact is required",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    if isinstance(artifact, Mapping):
        artifact = SignedTrustedArtifact.from_dict(artifact)
    if not isinstance(artifact, SignedTrustedArtifact):
        raise TrustedArtifactError(
            "trusted artifact has an unsupported type",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    if artifact.schema_version != SIGNED_ARTIFACT_SCHEMA_VERSION:
        raise TrustedArtifactError(
            "trusted artifact schema is not supported",
            rejection_codes=("TRUSTED_ARTIFACT_SCHEMA_UNSUPPORTED",),
        )
    if artifact.artifact_type != expected_artifact_type:
        raise TrustedArtifactError(
            f"trusted artifact type {artifact.artifact_type!r} != "
            f"expected {expected_artifact_type!r}",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    if not trusted_public_key_path:
        raise TrustedArtifactError(
            "an explicitly trusted public key is required",
            rejection_codes=("TRUSTED_ARTIFACT_TRUST_ANCHOR_MISSING",),
        )
    try:
        trusted = load_trusted_public_key(trusted_public_key_path)
    except Exception as exc:  # noqa: BLE001
        raise TrustedArtifactError(
            "trusted public key is unavailable",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED",),
        ) from exc
    if artifact.signer_public_key_id != trusted["public_key_id"]:
        raise TrustedArtifactError(
            "trusted artifact signer does not match the trusted public key",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED",),
        )
    try:
        signature = base64.b64decode(artifact.signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustedArtifactError(
            "trusted artifact signature is not valid base64",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNATURE_INVALID",),
        ) from exc
    if len(signature) != SIGNATURE_BYTES:
        raise TrustedArtifactError(
            "trusted artifact signature has an unexpected length",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNATURE_INVALID",),
        )
    try:
        trusted["key"].verify(signature, artifact.signing_payload())
    except Exception as exc:  # noqa: BLE001
        raise TrustedArtifactError(
            "trusted artifact signature does not verify",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNATURE_INVALID",),
        ) from exc

    payload = artifact.payload()
    issued_at = _parse_timestamp(payload.get("issued_at"))
    now = _parse_timestamp(current_time)
    if issued_at is None or now is None:
        raise TrustedArtifactError(
            "trusted artifact requires parsable issued_at and current_time",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    if issued_at > now:
        raise TrustedArtifactError(
            "trusted artifact was issued in the future",
            rejection_codes=("TRUSTED_ARTIFACT_FROM_FUTURE",),
        )
    if max_age_seconds < 0:
        raise TrustedArtifactError(
            "max_age_seconds must be non-negative",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    if (now - issued_at).total_seconds() > max_age_seconds:
        raise TrustedArtifactError(
            "trusted artifact is stale",
            rejection_codes=("TRUSTED_ARTIFACT_STALE",),
        )
    return payload


def _assert_trusted_artifacts_are_coherent() -> None:
    if len(set(TRUSTED_ARTIFACT_TYPES)) != len(TRUSTED_ARTIFACT_TYPES):
        raise AssertionError("trusted artifact types must be unique")
    codes = [code for code, _ in TRUSTED_ARTIFACT_REJECTION_CODES]
    if len(set(codes)) != len(codes):
        raise AssertionError("trusted artifact rejection codes must be unique")


_assert_trusted_artifacts_are_coherent()
