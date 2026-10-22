"""
源观测来源认证（隔离原型）
==========================
本模块为 ``SourceObservation`` 提供**外部签名来源证明**：

- 观测语义指纹 + adapter + 采集时间 + 签发时间被一份 Ed25519 工件覆盖；
- 运行期严格模式只接受由显式可信观测权威签发、且在允许新鲜度窗口内的观测。

边界：不实现生产采集管线、不修改 ``SourceObservation`` 身份算法、
不生成生产密钥；签名只证明“可信采集组件声称采集了这些字段”，
不证明上游网站内容的真实性。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from utils.source_identity import SourceObservation
from utils.trusted_artifacts import (
    SignedTrustedArtifact,
    TrustedArtifactError,
    sign_trusted_artifact,
    verify_trusted_artifact,
)

#: 工件类型。
SOURCE_OBSERVATION_ATTESTATION_TYPE = "SOURCE_OBSERVATION_ATTESTATION"

#: 拒绝码。
OBSERVATION_ATTESTATION_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("OBSERVATION_ATTESTATION_MISSING", "严格模式要求源观测来源证明。"),
    ("OBSERVATION_ATTESTATION_INVALID", "源观测来源证明无法验证。"),
    ("OBSERVATION_ATTESTATION_MISMATCH", "来源证明未绑定当前观测。"),
    ("OBSERVATION_ATTESTATION_ADAPTER_MISMATCH", "来源证明的 adapter 不一致。"),
)


class ObservationAttestationError(TrustedArtifactError):
    """源观测来源证明错误。"""


def sign_observation_attestation(
    private_key_path: str,
    observation: SourceObservation,
    *,
    adapter: str,
    captured_at: str,
) -> SignedTrustedArtifact:
    """
    由可信采集权威为一次观测签发来源证明（私钥必须在仓库外）。
    """
    if not isinstance(observation, SourceObservation):
        raise ObservationAttestationError(
            "only a SourceObservation can be attested",
            rejection_codes=("OBSERVATION_ATTESTATION_INVALID",),
        )
    if not adapter or not str(adapter).strip():
        raise ObservationAttestationError(
            "an adapter identity is required",
            rejection_codes=("OBSERVATION_ATTESTATION_ADAPTER_MISMATCH",),
        )
    return sign_trusted_artifact(
        private_key_path,
        artifact_type=SOURCE_OBSERVATION_ATTESTATION_TYPE,
        payload={
            "observation_fingerprint": observation.semantic_fingerprint(),
            "observation_id": str(getattr(observation, "observation_id", "")),
            "adapter": str(adapter),
            "captured_at": str(captured_at),
            "issued_at": str(captured_at),
        },
    )


def verify_observation_attestation(
    attestation: SignedTrustedArtifact | Mapping[str, Any] | None,
    observation: SourceObservation | None,
    *,
    trusted_public_key_path: str | None,
    current_time: str,
    max_age_seconds: float,
    expected_adapter: str | None = None,
) -> dict:
    """
    **只读**验证来源证明：签名、新鲜度、观测指纹与 adapter 绑定。
    """
    if attestation is None:
        raise ObservationAttestationError(
            "strict mode requires a signed observation attestation",
            rejection_codes=("OBSERVATION_ATTESTATION_MISSING",),
        )
    if not isinstance(observation, SourceObservation):
        raise ObservationAttestationError(
            "observation is missing or malformed",
            rejection_codes=("OBSERVATION_ATTESTATION_INVALID",),
        )
    try:
        payload = verify_trusted_artifact(
            attestation,
            expected_artifact_type=SOURCE_OBSERVATION_ATTESTATION_TYPE,
            trusted_public_key_path=trusted_public_key_path,
            current_time=current_time,
            max_age_seconds=max_age_seconds,
        )
    except TrustedArtifactError as exc:
        raise ObservationAttestationError(
            f"observation attestation rejected: {exc}",
            rejection_codes=("OBSERVATION_ATTESTATION_INVALID",),
        ) from exc
    if str(payload.get("observation_fingerprint")) != observation.semantic_fingerprint():
        raise ObservationAttestationError(
            "observation attestation does not bind this observation",
            rejection_codes=("OBSERVATION_ATTESTATION_MISMATCH",),
        )
    expected_id = str(getattr(observation, "observation_id", ""))
    if str(payload.get("observation_id", "")) != expected_id:
        raise ObservationAttestationError(
            "observation attestation does not bind this observation id",
            rejection_codes=("OBSERVATION_ATTESTATION_MISMATCH",),
        )
    observed_adapter = str(getattr(observation.stability_scope, "adapter", ""))
    if expected_adapter is not None and str(payload.get("adapter")) != expected_adapter:
        raise ObservationAttestationError(
            "observation attestation adapter mismatch",
            rejection_codes=("OBSERVATION_ATTESTATION_ADAPTER_MISMATCH",),
        )
    if observed_adapter and str(payload.get("adapter")) != observed_adapter:
        raise ObservationAttestationError(
            "observation attestation does not bind the observation adapter",
            rejection_codes=("OBSERVATION_ATTESTATION_ADAPTER_MISMATCH",),
        )
    return payload


def _assert_observation_authority_is_coherent() -> None:
    codes = [code for code, _ in OBSERVATION_ATTESTATION_REJECTION_CODES]
    if len(set(codes)) != len(codes):
        raise AssertionError("observation attestation rejection codes must be unique")


_assert_observation_authority_is_coherent()
