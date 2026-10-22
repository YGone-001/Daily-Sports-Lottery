"""
影子恢复证据（隔离原型）
========================
本模块把「不确定提交后的恢复」表示为一份**外部签名工件**：

- 绑定审计存储身份、经外部检查点认证的链尖与长度；
- 明确区分 ``NONE`` / ``EVENT_WITHOUT_POINTER`` / ``POINTER_WITHOUT_EVENT``；
- 只有 ``NONE`` 才允许后续再启用。

边界：不实现生产恢复服务、不自动激活任何映射、不修改审计字节。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from utils.source_identity import fingerprint
from utils.trusted_artifacts import (
    SignedTrustedArtifact,
    TrustedArtifactError,
    sign_trusted_artifact,
    verify_trusted_artifact,
)

#: 工件类型。
RECOVERY_EVIDENCE_TYPE = "SHADOW_RECOVERY_EVIDENCE"

#: 差异分类。
DIVERGENCE_NONE = "NONE"
DIVERGENCE_EVENT_WITHOUT_POINTER = "EVENT_WITHOUT_POINTER"
DIVERGENCE_POINTER_WITHOUT_EVENT = "POINTER_WITHOUT_EVENT"

#: 拒绝码。
RECOVERY_EVIDENCE_REJECTION_CODES: tuple[tuple[str, str], ...] = (
    ("RECOVERY_EVIDENCE_MISSING", "严格模式要求已验证的恢复证据。"),
    ("RECOVERY_EVIDENCE_INVALID", "恢复证据无法验证或已过期。"),
    ("RECOVERY_EVIDENCE_DIVERGENCE", "审计事件与有效指针存在未消解的差异。"),
)


class RecoveryEvidenceError(TrustedArtifactError):
    """恢复证据错误。"""


def build_recovery_evidence(
    *,
    audit_store: Any,
    audit_checkpoint: SignedTrustedArtifact | Mapping[str, Any] | None,
    checkpoint_trusted_public_key_path: str | None,
    signing_private_key_path: str,
    current_time: str,
    recovery_actor: str,
    effective_snapshot_id: str,
    max_age_seconds: float,
) -> SignedTrustedArtifact:
    """
    构建恢复证据（只读）：先验证外部检查点，再判定事件/指针差异，最后签名。
    """
    if audit_store is None:
        raise RecoveryEvidenceError(
            "a persistent audit store is required for recovery evidence",
            rejection_codes=("RECOVERY_EVIDENCE_MISSING",),
        )
    try:
        audit_store.verify_external_checkpoint(
            audit_checkpoint,
            trusted_public_key_path=checkpoint_trusted_public_key_path,
            current_time=current_time,
            max_age_seconds=max_age_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise RecoveryEvidenceError(
            f"audit chain checkpoint verification failed: {exc}",
            rejection_codes=("RECOVERY_EVIDENCE_INVALID",),
        ) from exc
    records = audit_store.records()
    completion = records[-1] if records else None
    completion_present = bool(
        completion is not None
        and completion.event_type == "SHADOW_ROLLBACK_COMPLETED"
    )
    audit_effective = ""
    if completion_present:
        audit_effective = str(dict(completion.payload).get("effective_snapshot_id", ""))
    if completion_present and audit_effective == str(effective_snapshot_id):
        divergence = DIVERGENCE_NONE
    elif completion_present and audit_effective != str(effective_snapshot_id):
        divergence = DIVERGENCE_EVENT_WITHOUT_POINTER
    elif not completion_present and str(effective_snapshot_id):
        divergence = DIVERGENCE_POINTER_WITHOUT_EVENT
    else:
        divergence = DIVERGENCE_NONE

    chain_tip = records[-1].entry_hash if records else "0" * 64
    payload = {
        "store_identity": audit_store.store_identity(),
        "dataset_identity": getattr(audit_store, "dataset_identity", ""),
        "chain_tip": chain_tip,
        "entry_count": len(records),
        "completion_event_present": completion_present,
        "audit_effective_snapshot_id": audit_effective,
        "effective_snapshot_id": str(effective_snapshot_id),
        "divergence": divergence,
        "verified_at": str(current_time),
        "issued_at": str(current_time),
        "recovery_actor": str(recovery_actor),
        "prototype_only": True,
    }
    payload["recovery_fingerprint"] = fingerprint(
        {k: v for k, v in payload.items() if k != "issued_at"}
    )
    return sign_trusted_artifact(
        signing_private_key_path,
        artifact_type=RECOVERY_EVIDENCE_TYPE,
        payload=payload,
    )


def verify_recovery_evidence(
    evidence: SignedTrustedArtifact | Mapping[str, Any] | None,
    *,
    trusted_public_key_path: str | None,
    current_time: str,
    max_age_seconds: float,
) -> dict:
    """
    **只读**验证恢复证据：签名、新鲜度与 ``divergence == NONE``。
    """
    if evidence is None:
        raise RecoveryEvidenceError(
            "strict mode requires verified recovery evidence",
            rejection_codes=("RECOVERY_EVIDENCE_MISSING",),
        )
    try:
        payload = verify_trusted_artifact(
            evidence,
            expected_artifact_type=RECOVERY_EVIDENCE_TYPE,
            trusted_public_key_path=trusted_public_key_path,
            current_time=current_time,
            max_age_seconds=max_age_seconds,
        )
    except TrustedArtifactError as exc:
        raise RecoveryEvidenceError(
            f"recovery evidence rejected: {exc}",
            rejection_codes=("RECOVERY_EVIDENCE_INVALID",),
        ) from exc
    if str(payload.get("divergence")) != DIVERGENCE_NONE:
        raise RecoveryEvidenceError(
            "event/pointer divergence must be resolved before re-enablement",
            rejection_codes=("RECOVERY_EVIDENCE_DIVERGENCE",),
        )
    if not str(payload.get("store_identity", "")).strip():
        raise RecoveryEvidenceError(
            "recovery evidence does not bind an audit store identity",
            rejection_codes=("RECOVERY_EVIDENCE_INVALID",),
        )
    return payload


def _assert_recovery_authority_is_coherent() -> None:
    codes = [code for code, _ in RECOVERY_EVIDENCE_REJECTION_CODES]
    if len(set(codes)) != len(codes):
        raise AssertionError("recovery evidence rejection codes must be unique")
    if {DIVERGENCE_NONE, DIVERGENCE_EVENT_WITHOUT_POINTER, DIVERGENCE_POINTER_WITHOUT_EVENT} != {
        "NONE",
        "EVENT_WITHOUT_POINTER",
        "POINTER_WITHOUT_EVENT",
    }:
        raise AssertionError("recovery divergence classifications drifted")


_assert_recovery_authority_is_coherent()
