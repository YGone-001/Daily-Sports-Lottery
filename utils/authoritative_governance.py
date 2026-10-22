"""
权威治理视图（隔离原型，生产标志仍关闭）
========================================
本模块把「隔离治理账本 + 外部签名检查点 + 签名 Canonical 基线清单 +
签名激活撤销清单」组合成一个**只读、可验证的权威治理视图**。

它回答：在给定受控时间与显式信任锚下，当前治理事实（提案状态 / 内容指纹 /
活动绑定 / 撤销 / Canonical 集合 / 基线）是什么，并由一个独立的
``GOVERNANCE_VIEW_ATTESTATION`` 签名工件背书。

边界
----
- 不实现生产账本读取服务、不写任何生产数据、不启用生产激活。
- 账本仍必须在仓库之外；检查点、基线清单、撤销清单都必须由显式可信公钥验证。
- 检查点要求与账本**完全一致**（链尖 + 长度）：任何尾部截断或全链重写都会
  在视图构建时被检出。
- 视图签名只证明「该权威组件在受控时间观察到了这些事实」，不是外部共识。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    ACTIVE_STATES,
    GovernanceLedger,
    LEDGER_TRUST_ANCHOR_STATEMENT,
    audit_historical_approvals,
)
from utils.source_identity import canonical_json, fingerprint
from utils.trusted_artifacts import (
    SignedTrustedArtifact,
    TrustedArtifactError,
    sign_trusted_artifact,
    verify_trusted_artifact,
)

#: 权威治理视图 schema 版本。
AUTHORITATIVE_GOVERNANCE_VIEW_SCHEMA_VERSION = "1.0.0"

#: 工件类型。
LEDGER_CHECKPOINT_TYPE = "GOVERNANCE_LEDGER_CHECKPOINT"
CANONICAL_MANIFEST_TYPE = "CANONICAL_BASELINE_MANIFEST"
REVOCATION_MANIFEST_TYPE = "ACTIVATION_REVOCATION_MANIFEST"
GOVERNANCE_VIEW_ATTESTATION_TYPE = "GOVERNANCE_VIEW_ATTESTATION"

#: 默认新鲜度窗口（秒）。调用方必须显式给出；此处仅作为构造默认值。
DEFAULT_GOVERNANCE_MAX_AGE_SECONDS = 3600


@dataclass(frozen=True)
class AuthoritativeGovernanceView:
    """
    一份经签名背书的权威治理事实视图（不可变、只读）。

    ``attestation`` 的载荷逐字段覆盖下述派生事实；调用方不得单独修改任何字段。
    """

    dataset_identity: str
    baseline_fingerprint: str
    canonical_ids: tuple[str, ...]
    proposal_states: tuple[tuple[str, str], ...]
    proposal_content_fingerprints: tuple[tuple[str, str], ...]
    active_bindings: tuple[tuple[str, str, str], ...]
    revocations: tuple[tuple[str, str], ...]
    ledger_identity: str
    ledger_chain_tip: str
    ledger_entry_count: int
    approval_audit: str
    verified_at: str
    attestation: SignedTrustedArtifact

    # -- 只读查询 ---------------------------------------------------------

    def state_of(self, proposal_id: str) -> str | None:
        for candidate, state in self.proposal_states:
            if candidate == proposal_id:
                return state
        return None

    def content_fingerprint_of(self, proposal_id: str) -> str | None:
        for candidate, value in self.proposal_content_fingerprints:
            if candidate == proposal_id:
                return value
        return None

    def is_active_proposal(self, proposal_id: str) -> bool:
        return self.state_of(proposal_id) in ACTIVE_STATES

    def active_binding_for(self, source_identity_key: str) -> str | None:
        owners = self.active_binding_owners_for(source_identity_key)
        if len(owners) != 1:
            return None
        return owners[0]

    def active_binding_owners_for(self, source_identity_key: str) -> tuple[str, ...]:
        """该源键的全部活动 Canonical 所有者（去重、排序；保留冲突可见性）。"""
        return tuple(
            sorted(
                {
                    canonical_id
                    for key, canonical_id, _proposal_id in self.active_bindings
                    if key == source_identity_key
                }
            )
        )

    def revocation_ids(self) -> frozenset[str]:
        return frozenset(activation_id for activation_id, _reference in self.revocations)

    def canonical_id_set(self) -> frozenset[str]:
        return frozenset(self.canonical_ids)

    # -- 签名载荷 ---------------------------------------------------------

    def facts_payload(self) -> dict:
        return {
            "kind": "AUTHORITATIVE_GOVERNANCE_VIEW",
            "schema_version": AUTHORITATIVE_GOVERNANCE_VIEW_SCHEMA_VERSION,
            "dataset_identity": self.dataset_identity,
            "baseline_fingerprint": self.baseline_fingerprint,
            "canonical_ids": sorted(self.canonical_ids),
            "proposal_states": [list(pair) for pair in self.proposal_states],
            "proposal_content_fingerprints": [
                list(pair) for pair in self.proposal_content_fingerprints
            ],
            "active_bindings": [list(triple) for triple in self.active_bindings],
            "revocations": [list(pair) for pair in self.revocations],
            "ledger_identity": self.ledger_identity,
            "ledger_chain_tip": self.ledger_chain_tip,
            "ledger_entry_count": self.ledger_entry_count,
            "approval_audit": self.approval_audit,
            "verified_at": self.verified_at,
            "ledger_trust_anchor": LEDGER_TRUST_ANCHOR_STATEMENT,
            "prototype_only": True,
            "is_production_authority": False,
        }

    def fingerprint(self) -> str:
        return fingerprint(self.facts_payload())

    def attestation_payload(self) -> dict:
        payload = self.facts_payload()
        payload["issued_at"] = self.verified_at
        payload["view_fingerprint"] = fingerprint(
            {k: v for k, v in payload.items() if k not in ("issued_at",)}
        )
        return payload

    def as_dict(self) -> dict:
        payload = self.facts_payload()
        payload["view_fingerprint"] = self.fingerprint()
        payload["attestation"] = self.attestation.as_dict()
        return payload


def ledger_identity(ledger: GovernanceLedger) -> str:
    """治理账本的确定性身份（路径 + 数据集 + 契约）。"""
    return fingerprint(
        {
            "kind": "GOVERNANCE_LEDGER_IDENTITY",
            "path": ledger.path,
            "dataset_identity": ledger.dataset_identity,
        }
    )


def _normalize_pairs(values: Sequence[Any], *, code: str) -> tuple[tuple[str, str], ...]:
    pairs: set[tuple[str, str]] = set()
    for item in values:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise TrustedArtifactError(
                "trusted artifact contains a malformed pair",
                rejection_codes=(code,),
            )
        pairs.add((str(item[0]), str(item[1])))
    return tuple(sorted(pairs))


def build_authoritative_governance_view(
    *,
    ledger_path: str,
    dataset_identity: str,
    current_baseline_fingerprint: str,
    trusted_owner_public_key_path: str,
    ledger_checkpoint: SignedTrustedArtifact | Mapping[str, Any],
    ledger_checkpoint_trusted_public_key_path: str,
    canonical_manifest: SignedTrustedArtifact | Mapping[str, Any],
    canonical_manifest_trusted_public_key_path: str,
    revocation_manifest: SignedTrustedArtifact | Mapping[str, Any],
    revocation_manifest_trusted_public_key_path: str,
    view_signing_private_key_path: str,
    current_time: str,
    max_age_seconds: float = DEFAULT_GOVERNANCE_MAX_AGE_SECONDS,
) -> AuthoritativeGovernanceView:
    """
    构建权威治理视图。

    所有外部工件必须显式提供并验证；任何缺失、签名失败、过期、类型不符、
    链尖/长度不一致都会 fail closed。
    """
    if not dataset_identity or not current_baseline_fingerprint:
        raise TrustedArtifactError(
            "dataset identity and Canonical baseline are required",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    ledger = GovernanceLedger(ledger_path, dataset_identity=dataset_identity)
    if not ledger.exists():
        raise TrustedArtifactError(
            "the authoritative governance ledger does not exist",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    entries = ledger.entries()
    structural = ledger.verify()

    checkpoint_payload = verify_trusted_artifact(
        ledger_checkpoint,
        expected_artifact_type=LEDGER_CHECKPOINT_TYPE,
        trusted_public_key_path=ledger_checkpoint_trusted_public_key_path,
        current_time=current_time,
        max_age_seconds=max_age_seconds,
    )
    identity = ledger_identity(ledger)
    if str(checkpoint_payload.get("ledger_identity")) != identity:
        raise TrustedArtifactError(
            "ledger checkpoint does not bind this ledger identity",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    if str(checkpoint_payload.get("dataset_identity")) != dataset_identity:
        raise TrustedArtifactError(
            "ledger checkpoint dataset identity mismatch",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    expected_tip = entries[-1].entry_hash if entries else "0" * 64
    if (
        int(checkpoint_payload.get("entry_count", -1)) != len(entries)
        or str(checkpoint_payload.get("chain_tip")) != expected_tip
    ):
        raise TrustedArtifactError(
            "ledger checkpoint does not match the current chain tip / length",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNATURE_INVALID",),
        )

    audit = audit_historical_approvals(
        ledger,
        trusted_public_key_path=trusted_owner_public_key_path,
        dataset_identity=dataset_identity,
    )

    canonical_payload = verify_trusted_artifact(
        canonical_manifest,
        expected_artifact_type=CANONICAL_MANIFEST_TYPE,
        trusted_public_key_path=canonical_manifest_trusted_public_key_path,
        current_time=current_time,
        max_age_seconds=max_age_seconds,
    )
    if str(canonical_payload.get("dataset_identity")) != dataset_identity:
        raise TrustedArtifactError(
            "Canonical baseline manifest dataset identity mismatch",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    if (
        str(canonical_payload.get("baseline_fingerprint"))
        != current_baseline_fingerprint
    ):
        raise TrustedArtifactError(
            "Canonical baseline manifest does not bind the current baseline",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    canonical_ids = tuple(
        sorted({str(value) for value in canonical_payload.get("canonical_ids") or ()})
    )
    if not canonical_ids:
        raise TrustedArtifactError(
            "Canonical baseline manifest must contain at least one Canonical ID",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )

    revocation_payload = verify_trusted_artifact(
        revocation_manifest,
        expected_artifact_type=REVOCATION_MANIFEST_TYPE,
        trusted_public_key_path=revocation_manifest_trusted_public_key_path,
        current_time=current_time,
        max_age_seconds=max_age_seconds,
    )
    if str(revocation_payload.get("dataset_identity")) != dataset_identity:
        raise TrustedArtifactError(
            "activation revocation manifest dataset identity mismatch",
            rejection_codes=("TRUSTED_ARTIFACT_TYPE_MISMATCH",),
        )
    revocations = _normalize_pairs(
        revocation_payload.get("revocations") or (),
        code="TRUSTED_ARTIFACT_MALFORMED",
    )

    latest: dict[str, Any] = {}
    for entry in entries:
        latest[entry.proposal_id] = entry
    states = tuple(sorted(ledger.proposal_states().items()))
    state_by_id = dict(states)
    content_fingerprints = tuple(
        sorted(
            (proposal_id, entry.proposal_content_fingerprint)
            for proposal_id, entry in latest.items()
        )
    )
    active_records: list[tuple[str, str, str]] = []
    for proposal_id, entry in latest.items():
        if state_by_id.get(proposal_id) not in ACTIVE_STATES:
            continue
        if entry.binding_source_key and entry.binding_canonical_id:
            active_records.append(
                (
                    str(entry.binding_source_key),
                    str(entry.binding_canonical_id),
                    str(proposal_id),
                )
            )
    active = tuple(sorted(active_records))

    unsigned = AuthoritativeGovernanceView(
        dataset_identity=dataset_identity,
        baseline_fingerprint=current_baseline_fingerprint,
        canonical_ids=canonical_ids,
        proposal_states=states,
        proposal_content_fingerprints=content_fingerprints,
        active_bindings=active,
        revocations=revocations,
        ledger_identity=identity,
        ledger_chain_tip=expected_tip,
        ledger_entry_count=len(entries),
        approval_audit=str(audit["verification_category"]),
        verified_at=str(current_time),
        attestation=SignedTrustedArtifact(
            artifact_type=GOVERNANCE_VIEW_ATTESTATION_TYPE,
            payload_json="{}",
            signer_public_key_id="",
            signature="",
        ),
    )
    attestation = sign_trusted_artifact(
        view_signing_private_key_path,
        artifact_type=GOVERNANCE_VIEW_ATTESTATION_TYPE,
        payload=unsigned.attestation_payload(),
    )
    view = AuthoritativeGovernanceView(
        dataset_identity=unsigned.dataset_identity,
        baseline_fingerprint=unsigned.baseline_fingerprint,
        canonical_ids=unsigned.canonical_ids,
        proposal_states=unsigned.proposal_states,
        proposal_content_fingerprints=unsigned.proposal_content_fingerprints,
        active_bindings=unsigned.active_bindings,
        revocations=unsigned.revocations,
        ledger_identity=unsigned.ledger_identity,
        ledger_chain_tip=unsigned.ledger_chain_tip,
        ledger_entry_count=unsigned.ledger_entry_count,
        approval_audit=unsigned.approval_audit,
        verified_at=unsigned.verified_at,
        attestation=attestation,
    )
    verify_authoritative_governance_view(
        view,
        trusted_public_key_path=ledger_checkpoint_trusted_public_key_path,
        current_time=current_time,
        max_age_seconds=max_age_seconds,
    )
    return view


def verify_authoritative_governance_view(
    view: AuthoritativeGovernanceView | None,
    *,
    trusted_public_key_path: str | None,
    current_time: str,
    max_age_seconds: float = DEFAULT_GOVERNANCE_MAX_AGE_SECONDS,
) -> dict:
    """
    验证视图签名与其自洽性（只读）。

    签名必须由显式可信公钥验证；载荷必须逐字段复现视图事实，否则拒绝。
    """
    if not isinstance(view, AuthoritativeGovernanceView):
        raise TrustedArtifactError(
            "an authoritative governance view is required",
            rejection_codes=("TRUSTED_ARTIFACT_MALFORMED",),
        )
    payload = verify_trusted_artifact(
        view.attestation,
        expected_artifact_type=GOVERNANCE_VIEW_ATTESTATION_TYPE,
        trusted_public_key_path=trusted_public_key_path,
        current_time=current_time,
        max_age_seconds=max_age_seconds,
    )
    expected = view.attestation_payload()
    if canonical_json(payload) != canonical_json(expected):
        raise TrustedArtifactError(
            "authoritative governance view attestation does not match its facts",
            rejection_codes=("TRUSTED_ARTIFACT_SIGNATURE_INVALID",),
        )
    return payload


def _assert_authoritative_governance_is_coherent() -> None:
    if AUTHORITATIVE_GOVERNANCE_VIEW_SCHEMA_VERSION != "1.0.0":
        raise AssertionError("authoritative governance view schema version drifted")
    for required in (
        LEDGER_CHECKPOINT_TYPE,
        CANONICAL_MANIFEST_TYPE,
        REVOCATION_MANIFEST_TYPE,
        GOVERNANCE_VIEW_ATTESTATION_TYPE,
    ):
        if not required:
            raise AssertionError("authoritative governance artifact types must be set")


_assert_authoritative_governance_is_coherent()
