"""
B-01 ~ B-05 生产阻断项整改验收测试（隔离原型）
================================================
覆盖整改顺序中的前五项：

- B-01/B-02：权威治理视图（账本 + 签名检查点 + Canonical 清单 + 撤销清单）
  被资格评估器与严格读取器使用；调用方伪造的“已独立验证”来源被阻断。
- B-03：严格读取器要求签名的源观测来源证明。
- B-04：严格回滚要求外部签名审计链检查点，可检测尾部截断。
- B-05：严格再启用要求已验证的恢复证据（事件/指针差异必须为 NONE）。

所有密钥都在 pytest 临时目录（仓库外）生成；绝不启用生产激活。
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from tools.shadow_runtime_activation import (
    ALT_CANONICAL_ID,
    CANONICAL_ID,
    DATASET,
    REVOCATION_REFERENCE,
    build_synthetic_shadow_fixture,
)
from utils.authoritative_governance import (
    CANONICAL_MANIFEST_TYPE,
    GOVERNANCE_VIEW_ATTESTATION_TYPE,
    LEDGER_CHECKPOINT_TYPE,
    REVOCATION_MANIFEST_TYPE,
    build_authoritative_governance_view,
    ledger_identity,
    verify_authoritative_governance_view,
)
from utils.mapping_governance import (
    MappingState,
    MissingApprovalEvidenceError,
    generate_mapping_owner_keypair,
    GovernanceLedger,
    load_trusted_public_key,
    reconstruct_proposal_from_evidence,
)
from utils.observation_authority import (
    ObservationAttestationError,
    sign_observation_attestation,
    verify_observation_attestation,
)
from utils.recovery_authority import (
    DIVERGENCE_EVENT_WITHOUT_POINTER,
    RecoveryEvidenceError,
    build_recovery_evidence,
    verify_recovery_evidence,
)
from utils.runtime_activation_authorization import (
    ActivationVerificationContext,
    build_activation_authorization_body,
    build_shadow_activation_snapshot,
    sign_runtime_activation_authorization,
    verify_runtime_activation_authorization,
)
from utils.runtime_mapping_eligibility import (
    CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
    GovernanceEvidenceProvenance,
    RuntimeMappingContext,
    evaluate_runtime_mapping_eligibility,
)
from utils.shadow_activation_audit_store import ShadowActivationAuditStore
from utils.shadow_activation_simulation import (
    ShadowActivationSimulator,
    ShadowSnapshotRegistry,
)
from utils.shadow_runtime_reader import (
    ShadowAuthorizationEvidence,
    ShadowRuntimeReader,
    ShadowReenablementError,
)
from utils.trusted_artifacts import TrustedArtifactError, sign_trusted_artifact

WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("trust_hardening_env")
    return build_synthetic_shadow_fixture(str(workspace))


def _keys(tmp_path, name: str) -> tuple[str, str]:
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    private = str(workspace / "authority_private.pem")
    public = str(workspace / "authority_public.pem")
    generate_mapping_owner_keypair(private, public)
    return private, public


def _build_governance(
    env,
    tmp_path,
    *,
    canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
    revoked_activation_id: str = "",
):
    """构建账本、签名检查点 / 清单与权威视图（全部隔离）。"""
    authority_private, authority_public = _keys(tmp_path, "authority")
    view_private, view_public = _keys(tmp_path, "view_authority")
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = GovernanceLedger(
        str(ledger_dir / "mapping_governance_ledger.jsonl"),
        dataset_identity=DATASET,
    )
    baseline = env.snapshot.baseline_fingerprint
    ledger.append(
        proposal=env.proposal,
        to_state=MappingState.PROPOSED.value,
        actor="shadow-operator",
        actor_role="operator",
        reason="proposed for authoritative view",
        baseline_fingerprint=baseline,
    )
    ledger.append(
        proposal=env.proposal,
        to_state=MappingState.APPROVED.value,
        actor="shadow-owner",
        actor_role="owner",
        reason="owner approved for authoritative view",
        baseline_fingerprint=baseline,
        approval=env.approval,
        trusted_public_key_path=env.owner_public,
        source_observation_fingerprint=env.observation.semantic_fingerprint(),
    )
    entries = ledger.entries()
    checkpoint = sign_trusted_artifact(
        authority_private,
        artifact_type=LEDGER_CHECKPOINT_TYPE,
        payload={
            "ledger_identity": ledger_identity(ledger),
            "dataset_identity": DATASET,
            "chain_tip": entries[-1].entry_hash,
            "entry_count": len(entries),
            "issued_at": WITHIN_WINDOW,
        },
    )
    canonical = sign_trusted_artifact(
        authority_private,
        artifact_type=CANONICAL_MANIFEST_TYPE,
        payload={
            "dataset_identity": DATASET,
            "baseline_fingerprint": baseline,
            "canonical_ids": list(canonical_ids),
            "issued_at": WITHIN_WINDOW,
        },
    )
    revocations = (
        [(revoked_activation_id, REVOCATION_REFERENCE)]
        if revoked_activation_id
        else []
    )
    revocation = sign_trusted_artifact(
        authority_private,
        artifact_type=REVOCATION_MANIFEST_TYPE,
        payload={
            "dataset_identity": DATASET,
            "revocations": revocations,
            "issued_at": WITHIN_WINDOW,
        },
    )
    view = build_authoritative_governance_view(
        ledger_path=ledger.path,
        dataset_identity=DATASET,
        current_baseline_fingerprint=baseline,
        trusted_owner_public_key_path=env.owner_public,
        ledger_checkpoint=checkpoint,
        ledger_checkpoint_trusted_public_key_path=authority_public,
        canonical_manifest=canonical,
        canonical_manifest_trusted_public_key_path=authority_public,
        revocation_manifest=revocation,
        revocation_manifest_trusted_public_key_path=authority_public,
        view_signing_private_key_path=view_private,
        view_trusted_public_key_path=view_public,
        current_time=WITHIN_WINDOW,
    )
    return {
        "view": view,
        "authority_private": authority_private,
        "authority_public": authority_public,
        "view_private": view_private,
        "view_public": view_public,
        "ledger": ledger,
        "baseline": baseline,
        "checkpoint": checkpoint,
        "canonical": canonical,
        "revocation": revocation,
    }


def _verified_decision(env, governance):
    view = governance["view"]
    context = RuntimeMappingContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=governance["baseline"],
        evidence_provenance=GovernanceEvidenceProvenance(
            source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
            dataset_identity=DATASET,
            baseline_fingerprint=governance["baseline"],
            structural_ledger_validation_performed=True,
            authenticated_approval_audit_performed=True,
            ledger_trust_anchor="isolated signed governance view",
        ),
        canonical_ids=view.canonical_ids,
        trusted_public_key_path=env.owner_public,
        expected_approver="shadow-owner",
        expected_operator="shadow-operator",
        recorded_proposal_id=env.proposal.proposal_id,
        recorded_proposal_fingerprint=env.proposal.fingerprint(),
        recorded_proposal_state=MappingState.APPROVED.value,
        known_source_bindings=tuple(
            (key, canonical_id) for key, canonical_id, _ in view.active_bindings
        ),
        authoritative_view=view,
        authoritative_view_trusted_public_key_path=governance["view_public"],
    )
    return evaluate_runtime_mapping_eligibility(
        env.proposal,
        env.approval,
        env.observation,
        context,
        evaluated_at=WITHIN_WINDOW,
    )


def _verified_authorization(env, decision):
    return sign_runtime_activation_authorization(
        env.authorizer_private,
        build_activation_authorization_body(
            decision=decision,
            proposal=env.proposal,
            dataset_identity=DATASET,
            authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
            authorizer="shadow-authorizer",
            operator="shadow-operator",
            authorized_at="2030-01-01T00:00:00+00:00",
            expires_at="2030-02-01T00:00:00+00:00",
            revocation_reference=REVOCATION_REFERENCE,
            reason="verified governance activation",
        ),
    )


def _strict_snapshot(env, authorization, decision):
    context = ActivationVerificationContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
        current_time=WITHIN_WINDOW,
        trusted_public_key_path=env.authorizer_public,
        eligibility_decision=decision,
        proposal=env.proposal,
        require_verified_governance=True,
    )
    return build_shadow_activation_snapshot(
        authorizations=[authorization],
        verification_context=context,
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )


def _strict_context(env, governance, authorization, decision, snapshot):
    return env.read_context(
        evidence=(
            ShadowAuthorizationEvidence(
                authorization=authorization,
                decision=decision,
                proposal=env.proposal,
            ),
        ),
        authoritative_view=governance["view"],
        authoritative_view_trusted_public_key_path=governance["view_public"],
        observation_trusted_public_key_path=governance["authority_public"],
        recovery_trusted_public_key_path=governance["authority_public"],
        revocation_registry=(),
    )


# ---------------------------------------------------------------------------
# B-01 / B-02
# ---------------------------------------------------------------------------

def test_b01_signed_view_allows_verified_eligibility(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    decision = _verified_decision(env, governance)
    assert decision.eligibility_result == "ELIGIBLE"
    assert decision.evidence_provenance == (
        CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER
    )
    assert decision.is_conditional is False
    assert decision.is_production_authorization is False


def test_b01_self_asserted_or_tampered_view_is_rejected(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    view = governance["view"]
    base_context = RuntimeMappingContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=governance["baseline"],
        evidence_provenance=GovernanceEvidenceProvenance(
            source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
        ),
        canonical_ids=view.canonical_ids,
        trusted_public_key_path=env.owner_public,
        expected_approver="shadow-owner",
        expected_operator="shadow-operator",
        recorded_proposal_id=env.proposal.proposal_id,
        recorded_proposal_fingerprint=env.proposal.fingerprint(),
        recorded_proposal_state=MappingState.APPROVED.value,
        known_source_bindings=tuple(
            (key, canonical_id) for key, canonical_id, _ in view.active_bindings
        ),
    )
    # 仅声称 VERIFIED、没有签名视图 -> 阻断。
    missing = evaluate_runtime_mapping_eligibility(
        env.proposal,
        env.approval,
        env.observation,
        base_context,
        evaluated_at=WITHIN_WINDOW,
    )
    assert missing.eligibility_result == "NOT_ELIGIBLE"
    assert "EVIDENCE_PROVENANCE_UNSUPPORTED" in missing.rejection_codes

    # 篡改视图事实 -> 证明与视图不一致 -> 阻断。
    tampered_view = dataclasses.replace(
        view,
        proposal_states=((env.proposal.proposal_id, MappingState.REVOKED.value),),
    )
    tampered_context = dataclasses.replace(
        base_context,
        authoritative_view=tampered_view,
        authoritative_view_trusted_public_key_path=governance["view_public"],
    )
    tampered = evaluate_runtime_mapping_eligibility(
        env.proposal,
        env.approval,
        env.observation,
        tampered_context,
        evaluated_at=WITHIN_WINDOW,
    )
    assert tampered.eligibility_result == "NOT_ELIGIBLE"
    assert "EVIDENCE_PROVENANCE_UNSUPPORTED" in tampered.rejection_codes


def test_b01_strict_activation_requires_verified_governance(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    decision = _verified_decision(env, governance)
    authorization = _verified_authorization(env, decision)

    strict = verify_runtime_activation_authorization(
        authorization,
        ActivationVerificationContext(
            dataset_identity=DATASET,
            current_baseline_fingerprint=governance["baseline"],
            current_time=WITHIN_WINDOW,
            trusted_public_key_path=env.authorizer_public,
            eligibility_decision=decision,
            proposal=env.proposal,
            require_verified_governance=True,
        ),
    )
    assert strict.is_consumable is True

    # 用调用方断言的资格判定签发同一授权 -> 严格模式拒绝。
    caller_authorization = _verified_authorization(env, env.decision)
    rejected = verify_runtime_activation_authorization(
        caller_authorization,
        ActivationVerificationContext(
            dataset_identity=DATASET,
            current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
            current_time=WITHIN_WINDOW,
            trusted_public_key_path=env.authorizer_public,
            eligibility_decision=env.decision,
            proposal=env.proposal,
            require_verified_governance=True,
        ),
    )
    assert rejected.is_consumable is False
    assert "GOVERNANCE_EVIDENCE_NOT_VERIFIED" in rejected.rejection_codes


# ---------------------------------------------------------------------------
# B-02 reader authoritative ownership / revocation
# ---------------------------------------------------------------------------

def test_b02_strict_reader_uses_authoritative_canonical_view(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    decision = _verified_decision(env, governance)
    authorization = _verified_authorization(env, decision)
    snapshot = _strict_snapshot(env, authorization, decision)

    # 第二个权威视图只承认 ALT —— 读取器必须使用权威 Canonical 集合。
    restricted = _build_governance(
        env, tmp_path / "restricted", canonical_ids=(ALT_CANONICAL_ID,)
    )
    registry = ShadowSnapshotRegistry()
    registry.register(snapshot)
    registry.select(snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry,
        require_authoritative_governance=True,
        require_observation_attestation=True,
    )
    attestation = sign_observation_attestation(
        restricted["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    result = reader.read(
        observation=env.observation,
        snapshot=snapshot,
        read_context=_strict_context(
            env, restricted, authorization, decision, snapshot
        ),
        observation_attestation=attestation,
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "CANONICAL_OWNER_UNKNOWN" in result.rejection_codes


def test_b02_strict_reader_uses_authoritative_revocations(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    decision = _verified_decision(env, governance)
    authorization = _verified_authorization(env, decision)
    snapshot = _strict_snapshot(env, authorization, decision)

    revoked = _build_governance(
        env,
        tmp_path / "revoked",
        revoked_activation_id=authorization.activation_id,
    )
    registry = ShadowSnapshotRegistry()
    registry.register(snapshot)
    registry.select(snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry,
        require_authoritative_governance=True,
        require_observation_attestation=True,
    )
    attestation = sign_observation_attestation(
        revoked["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    result = reader.read(
        observation=env.observation,
        snapshot=snapshot,
        read_context=_strict_context(
            env, revoked, authorization, decision, snapshot
        ),
        observation_attestation=attestation,
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "AUTHORIZATION_NOT_CONSUMABLE" in result.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in result.rejection_codes


# ---------------------------------------------------------------------------
# B-03 observation provenance
# ---------------------------------------------------------------------------

def test_b03_strict_reader_requires_observation_attestation(env, tmp_path):
    governance = _build_governance(env, tmp_path)
    decision = _verified_decision(env, governance)
    authorization = _verified_authorization(env, decision)
    snapshot = _strict_snapshot(env, authorization, decision)
    registry = ShadowSnapshotRegistry()
    registry.register(snapshot)
    registry.select(snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry,
        require_authoritative_governance=True,
        require_observation_attestation=True,
    )
    context = _strict_context(env, governance, authorization, decision, snapshot)

    missing = reader.read(
        observation=env.observation,
        snapshot=snapshot,
        read_context=context,
    )
    assert "OBSERVATION_PROVENANCE_MISSING" in missing.rejection_codes

    attestation = sign_observation_attestation(
        governance["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    valid = reader.read(
        observation=env.observation,
        snapshot=snapshot,
        read_context=context,
        observation_attestation=attestation,
    )
    assert valid.resolution_status == "RESOLVED_SHADOW"
    assert valid.candidate_canonical_id == CANONICAL_ID

    tampered = dataclasses.replace(attestation, payload_json="{}")
    invalid = reader.read(
        observation=env.observation,
        snapshot=snapshot,
        read_context=context,
        observation_attestation=tampered,
    )
    assert "OBSERVATION_PROVENANCE_INVALID" in invalid.rejection_codes


# ---------------------------------------------------------------------------
# B-04 audit chain checkpoint
# ---------------------------------------------------------------------------

def _audit_store(tmp_path, name: str) -> ShadowActivationAuditStore:
    workspace = tmp_path / name
    workspace.mkdir()
    return ShadowActivationAuditStore(str(workspace), dataset_identity=DATASET)


def _registry_reader(env):
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    return registry, ShadowRuntimeReader(registry=registry)


def test_b04_strict_rollback_requires_external_checkpoint(env, tmp_path):
    authority_private, authority_public = _keys(tmp_path, "audit_authority")
    store = _audit_store(tmp_path, "b04")
    registry, reader = _registry_reader(env)
    strict = ShadowActivationSimulator(
        registry=registry,
        reader=reader,
        audit_store=store,
        require_trusted_audit_checkpoint=True,
        audit_checkpoint_trusted_public_key_path=authority_public,
    )
    missing = strict.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert missing.status == "ROLLBACK_AUDIT_FAILED"
    assert "ROLLBACK_AUDIT_CHECKPOINT_MISSING" in missing.rejection_codes
    assert registry.active_snapshot_id == env.snapshot.snapshot_id
    assert store.exists() is False


def test_b04_checkpoint_detects_tail_truncation(env, tmp_path):
    authority_private, authority_public = _keys(tmp_path, "audit_authority")
    store = _audit_store(tmp_path, "b04_trunc")
    registry, reader = _registry_reader(env)
    permissive = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    assert permissive.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    ).status == "ROLLBACK_COMPLETED"
    checkpoint = store.export_checkpoint(
        authority_private, issued_at=WITHIN_WINDOW
    )
    strict = ShadowActivationSimulator(
        registry=registry,
        reader=reader,
        audit_store=store,
        require_trusted_audit_checkpoint=True,
        audit_checkpoint_trusted_public_key_path=authority_public,
    )
    # 截断最后一条完整记录：检查点必须检出。
    with open(store.path, "r", encoding="utf-8") as handle:
        lines = [line for line in handle.read().split("\n") if line]
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines[:-1]:
            handle.write(line + "\n")
    report = strict.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
        audit_checkpoint=checkpoint,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert "ROLLBACK_AUDIT_CHECKPOINT_INVALID" in report.rejection_codes
    assert report.audit_committed is False


def test_b04_checkpoint_signature_and_freshness_are_enforced(env, tmp_path):
    authority_private, authority_public = _keys(tmp_path, "audit_authority")
    other_private, other_public = _keys(tmp_path, "other_authority")
    store = _audit_store(tmp_path, "b04_sig")
    registry, reader = _registry_reader(env)
    permissive = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    permissive.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    checkpoint = store.export_checkpoint(
        authority_private, issued_at=WITHIN_WINDOW
    )
    strict = ShadowActivationSimulator(
        registry=registry,
        reader=reader,
        audit_store=store,
        require_trusted_audit_checkpoint=True,
        audit_checkpoint_trusted_public_key_path=other_public,
    )
    report = strict.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
        audit_checkpoint=checkpoint,
    )
    assert "ROLLBACK_AUDIT_CHECKPOINT_INVALID" in report.rejection_codes

    strict._audit_checkpoint_trusted_public_key_path = authority_public
    stale = strict.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, current_time="2030-01-15T02:00:00+00:00"),
        at="2030-01-15T02:00:00+00:00",
        audit_checkpoint=checkpoint,
    )
    assert "ROLLBACK_AUDIT_CHECKPOINT_INVALID" in stale.rejection_codes


# ---------------------------------------------------------------------------
# B-05 recovery evidence before re-enable
# ---------------------------------------------------------------------------

def test_b05_strict_reenable_requires_recovery_evidence(env, tmp_path):
    authority_private, authority_public = _keys(tmp_path, "recovery_authority")
    store = _audit_store(tmp_path, "b05")
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry,
        audit_store=store,
        require_recovery_evidence_for_reenable=True,
    )
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    assert simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    ).status == "ROLLBACK_COMPLETED"
    checkpoint = store.export_checkpoint(
        authority_private, issued_at=WITHIN_WINDOW
    )
    reader.disable(at=WITHIN_WINDOW, reason="simulated emergency disable")
    registry.select(env.snapshot.snapshot_id)
    context = _ctx(
        env,
        recovery_trusted_public_key_path=authority_public,
    )
    with pytest.raises(ShadowReenablementError) as missing:
        reader.re_enable(
            at=WITHIN_WINDOW, read_context=context, evidence=_evidence(env)
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_MISSING" in missing.value.rejection_codes

    evidence = build_recovery_evidence(
        audit_store=store,
        audit_checkpoint=checkpoint,
        checkpoint_trusted_public_key_path=authority_public,
        signing_private_key_path=authority_private,
        current_time=WITHIN_WINDOW,
        recovery_actor="shadow-recovery-operator",
        selected_snapshot_id=registry.active_snapshot_id,
        effective_snapshot_id=registry.active_snapshot_id,
        max_age_seconds=3600,
    )
    resumed = reader.re_enable(
        at=WITHIN_WINDOW,
        read_context=context,
        evidence=_evidence(env),
        recovery_evidence=evidence,
    )
    assert resumed["enabled"] is True
    assert resumed["recovery_evidence_fingerprint"]


def test_b05_divergence_blocks_reenable(env, tmp_path):
    authority_private, authority_public = _keys(tmp_path, "recovery_authority")
    store = _audit_store(tmp_path, "b05_div")
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry,
        audit_store=store,
        require_recovery_evidence_for_reenable=True,
    )
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    checkpoint = store.export_checkpoint(
        authority_private, issued_at=WITHIN_WINDOW
    )
    divergent = build_recovery_evidence(
        audit_store=store,
        audit_checkpoint=checkpoint,
        checkpoint_trusted_public_key_path=authority_public,
        signing_private_key_path=authority_private,
        current_time=WITHIN_WINDOW,
        recovery_actor="shadow-recovery-operator",
        selected_snapshot_id=registry.active_snapshot_id,
        effective_snapshot_id="unexpected-snapshot",
        max_age_seconds=3600,
    )
    assert (
        divergent.payload()["divergence"] == DIVERGENCE_EVENT_WITHOUT_POINTER
    )
    reader.disable(at=WITHIN_WINDOW, reason="simulated emergency disable")
    registry.select(env.snapshot.snapshot_id)
    context = _ctx(env, recovery_trusted_public_key_path=authority_public)
    with pytest.raises(ShadowReenablementError) as exc:
        reader.re_enable(
            at=WITHIN_WINDOW,
            read_context=context,
            evidence=_evidence(env),
            recovery_evidence=divergent,
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


# ---------------------------------------------------------------------------
# TI-01 ~ TI-23 — trust-boundary acceptance matrix
# ---------------------------------------------------------------------------

def _rebuild(env, governance, **overrides):
    """用同一组已签名工件重新构建视图（仅覆盖指定参数）。"""
    kwargs = dict(
        ledger_path=governance["ledger"].path,
        dataset_identity=DATASET,
        current_baseline_fingerprint=governance["baseline"],
        trusted_owner_public_key_path=env.owner_public,
        ledger_checkpoint=governance["checkpoint"],
        ledger_checkpoint_trusted_public_key_path=governance["authority_public"],
        canonical_manifest=governance["canonical"],
        canonical_manifest_trusted_public_key_path=governance["authority_public"],
        revocation_manifest=governance["revocation"],
        revocation_manifest_trusted_public_key_path=governance["authority_public"],
        view_signing_private_key_path=governance["view_private"],
        view_trusted_public_key_path=governance["view_public"],
        current_time=WITHIN_WINDOW,
    )
    kwargs.update(overrides)
    return build_authoritative_governance_view(**kwargs)


def test_ti_01_separate_checkpoint_and_view_signers_are_supported(env, tmp_path):
    """TI-01: 检查点签名者与视图签名者可以是不同密钥。"""
    governance = _build_governance(env, tmp_path)
    checkpoint_key = load_trusted_public_key(governance["authority_public"])
    view_key = load_trusted_public_key(governance["view_public"])
    assert checkpoint_key["public_key_id"] != view_key["public_key_id"]
    assert governance["view"].attestation.signer_public_key_id == (
        view_key["public_key_id"]
    )
    # 检查点仍由检查点密钥签发。
    assert governance["checkpoint"].signer_public_key_id == (
        checkpoint_key["public_key_id"]
    )


def test_ti_02_invalid_view_signer_is_rejected(env, tmp_path):
    """TI-02: 视图签名者不在视图信任锚内 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    other_private, _other_public = _keys(tmp_path, "ti02_other")
    with pytest.raises(TrustedArtifactError) as exc:
        _rebuild(env, governance, view_signing_private_key_path=other_private)
    assert "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED" in exc.value.rejection_codes


def test_ti_03_trust_anchor_role_substitution_is_rejected(env, tmp_path):
    """TI-03: 交换检查点 / 视图信任锚 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    with pytest.raises(TrustedArtifactError) as checkpoint_swap:
        _rebuild(
            env,
            governance,
            ledger_checkpoint_trusted_public_key_path=governance["view_public"],
        )
    assert "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED" in (
        checkpoint_swap.value.rejection_codes
    )
    with pytest.raises(TrustedArtifactError) as view_swap:
        _rebuild(
            env,
            governance,
            view_trusted_public_key_path=governance["authority_public"],
        )
    assert "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED" in view_swap.value.rejection_codes


def test_ti_04_tampered_governance_facts_are_rejected(env, tmp_path):
    """TI-04: 修改已签名视图的提案状态 / 所有权 / 撤销事实 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    view = governance["view"]
    for tampered in (
        dataclasses.replace(
            view,
            proposal_states=((env.proposal.proposal_id, MappingState.REVOKED.value),),
        ),
        dataclasses.replace(
            view,
            active_bindings=(("forged-key", "forged-canonical", "forged-proposal"),),
        ),
        dataclasses.replace(
            view, revocations=(("forged-activation", "forged-revocation"),)
        ),
    ):
        with pytest.raises(TrustedArtifactError) as exc:
            verify_authoritative_governance_view(
                tampered,
                trusted_public_key_path=governance["view_public"],
                current_time=WITHIN_WINDOW,
                max_age_seconds=3600,
            )
        assert "TRUSTED_ARTIFACT_SIGNATURE_INVALID" in exc.value.rejection_codes


def test_ti_05_valid_signed_checkpoint_is_accepted(env, tmp_path):
    """TI-05: 与当前链完全一致的有效签名检查点被接受。"""
    authority_private, authority_public = _keys(tmp_path, "ti05_authority")
    store = _audit_store(tmp_path, "ti05")
    registry, _reader = _registry_reader(env)
    ShadowActivationSimulator(
        registry=registry, reader=ShadowRuntimeReader(registry=registry), audit_store=store
    ).rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    checkpoint = store.export_checkpoint(authority_private, issued_at=WITHIN_WINDOW)
    payload = store.verify_external_checkpoint(
        checkpoint,
        trusted_public_key_path=authority_public,
        current_time=WITHIN_WINDOW,
        max_age_seconds=3600,
    )
    assert payload["chain_tip"] == store.chain_tip()
    assert int(payload["entry_count"]) == len(store.records())


def test_ti_07_checkpoint_ledger_identity_mismatch_is_rejected(env, tmp_path):
    """TI-07: 检查点绑定其他账本身份 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    payload = dict(governance["checkpoint"].payload())
    payload["ledger_identity"] = "0" * 64
    payload["issued_at"] = WITHIN_WINDOW
    forged = sign_trusted_artifact(
        governance["authority_private"],
        artifact_type=LEDGER_CHECKPOINT_TYPE,
        payload=payload,
    )
    with pytest.raises(TrustedArtifactError) as exc:
        _rebuild(env, governance, ledger_checkpoint=forged)
    assert "TRUSTED_ARTIFACT_TYPE_MISMATCH" in exc.value.rejection_codes


def test_ti_08_missing_historical_approval_evidence_is_rejected():
    """TI-08: 缺失历史批准 / 提案证据 -> 独立复核拒绝。"""
    with pytest.raises(MissingApprovalEvidenceError):
        reconstruct_proposal_from_evidence({})
    with pytest.raises(MissingApprovalEvidenceError):
        reconstruct_proposal_from_evidence(
            {
                "proposal_schema_version": "1.0.0",
                "source_identity_scope": {},
                "scoped_source_identity_key": {},
                "candidate_canonical_id": "canon-x",
                "identity_evidence_type": "x",
                "observation_fingerprints": [],
                "ingestion_plan_reference": "x",
                "reason": "x",
                "expected_baseline_fingerprint": "b",
                "requested_action": "BIND_SOURCE_TO_EXISTING_CANONICAL",
            }
        )


def test_ti_09_wrong_dataset_identity_is_rejected(env, tmp_path):
    """TI-09: 视图 / 工件的数据集身份不一致 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    with pytest.raises(TrustedArtifactError):
        _rebuild(env, governance, dataset_identity="another-dataset")


def test_ti_10_stale_canonical_baseline_is_rejected(env, tmp_path):
    """TI-10: Canonical 基线清单与当前基线不一致 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    with pytest.raises(TrustedArtifactError) as exc:
        _rebuild(
            env, governance, current_baseline_fingerprint="baseline-stale"
        )
    assert "TRUSTED_ARTIFACT_TYPE_MISMATCH" in exc.value.rejection_codes


def test_ti_12_missing_strict_governance_evidence_is_rejected(env):
    """TI-12: 严格读取器缺少权威治理视图 -> 拒绝。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(
        registry=registry, require_authoritative_governance=True
    )
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "AUTHORITATIVE_GOVERNANCE_VIEW_MISSING" in result.rejection_codes


def test_ti_15_empty_ownership_manifest_cannot_become_authority(env, tmp_path):
    """TI-15: 空 Canonical 清单不得被当作完整的生产所有权集合。"""
    governance = _build_governance(env, tmp_path)
    empty = sign_trusted_artifact(
        governance["authority_private"],
        artifact_type=CANONICAL_MANIFEST_TYPE,
        payload={
            "dataset_identity": DATASET,
            "baseline_fingerprint": governance["baseline"],
            "canonical_ids": [],
            "issued_at": WITHIN_WINDOW,
        },
    )
    with pytest.raises(TrustedArtifactError) as exc:
        _rebuild(env, governance, canonical_manifest=empty)
    assert "TRUSTED_ARTIFACT_MALFORMED" in exc.value.rejection_codes


def test_ti_15b_resigned_empty_ownership_view_is_rejected(env, tmp_path):
    """TI-15b: 即使签名有效，空所有权视图也不得充当完整权威证据。"""
    governance = _build_governance(env, tmp_path)
    empty_view = dataclasses.replace(governance["view"], canonical_ids=())
    attestation = sign_trusted_artifact(
        governance["view_private"],
        artifact_type=GOVERNANCE_VIEW_ATTESTATION_TYPE,
        payload=empty_view.attestation_payload(),
    )
    empty_view = dataclasses.replace(empty_view, attestation=attestation)
    with pytest.raises(TrustedArtifactError) as exc:
        verify_authoritative_governance_view(
            empty_view,
            trusted_public_key_path=governance["view_public"],
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
        )
    assert "TRUSTED_ARTIFACT_MALFORMED" in exc.value.rejection_codes


def test_ti_16b_observation_attestation_is_dataset_bound(env, tmp_path):
    """TI-16b: 观测来源证明必须绑定数据集身份，不能被跨数据集重用。"""
    governance = _build_governance(env, tmp_path)
    attestation = sign_observation_attestation(
        governance["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    with pytest.raises(ObservationAttestationError) as exc:
        verify_observation_attestation(
            attestation,
            env.observation,
            trusted_public_key_path=governance["authority_public"],
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity="another-dataset",
            expected_adapter="market_xml",
        )
    assert "OBSERVATION_ATTESTATION_MISMATCH" in exc.value.rejection_codes


def test_ti_18_cross_sport_observation_confusion_is_rejected(env, tmp_path):
    """TI-18: 用同一签名观测伪装成另一运动 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    attestation = sign_observation_attestation(
        governance["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    other_sport = dataclasses.replace(env.observation, sport="basketball")
    with pytest.raises(ObservationAttestationError) as exc:
        verify_observation_attestation(
            attestation,
            other_sport,
            trusted_public_key_path=governance["authority_public"],
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity=DATASET,
            expected_adapter="market_xml",
        )
    assert "OBSERVATION_ATTESTATION_MISMATCH" in exc.value.rejection_codes


def test_ti_19_cross_date_observation_confusion_is_rejected(env, tmp_path):
    """TI-19: 用同一签名观测伪装成另一日期 -> 拒绝。"""
    governance = _build_governance(env, tmp_path)
    attestation = sign_observation_attestation(
        governance["authority_private"],
        env.observation,
        dataset_identity=DATASET,
        adapter="market_xml",
        captured_at=WITHIN_WINDOW,
    )
    other_date = dataclasses.replace(
        env.observation, event_date="2030-05-02"
    )
    with pytest.raises(ObservationAttestationError) as exc:
        verify_observation_attestation(
            attestation,
            other_date,
            trusted_public_key_path=governance["authority_public"],
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity=DATASET,
            expected_adapter="market_xml",
        )
    assert "OBSERVATION_ATTESTATION_MISMATCH" in exc.value.rejection_codes


def test_ti_21_stale_or_forged_recovery_evidence_is_rejected(env, tmp_path):
    """TI-21: 过期 / 错误签名 / 篡改的恢复证据 -> 拒绝。"""
    authority_private, authority_public = _keys(tmp_path, "ti21_authority")
    _other_private, other_public = _keys(tmp_path, "ti21_other")
    store = _audit_store(tmp_path, "ti21")
    registry, _reader = _registry_reader(env)
    ShadowActivationSimulator(
        registry=registry,
        reader=ShadowRuntimeReader(registry=registry),
        audit_store=store,
    ).rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    checkpoint = store.export_checkpoint(authority_private, issued_at=WITHIN_WINDOW)
    evidence = build_recovery_evidence(
        audit_store=store,
        audit_checkpoint=checkpoint,
        checkpoint_trusted_public_key_path=authority_public,
        signing_private_key_path=authority_private,
        current_time=WITHIN_WINDOW,
        recovery_actor="shadow-recovery-operator",
        selected_snapshot_id=registry.active_snapshot_id,
        effective_snapshot_id=registry.active_snapshot_id,
        max_age_seconds=3600,
    )
    assert verify_recovery_evidence(
        evidence,
        trusted_public_key_path=authority_public,
        current_time=WITHIN_WINDOW,
        max_age_seconds=3600,
        expected_dataset_identity=DATASET,
    )["divergence"] == "NONE"

    with pytest.raises(RecoveryEvidenceError):
        verify_recovery_evidence(
            evidence,
            trusted_public_key_path=other_public,
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity=DATASET,
        )
    with pytest.raises(RecoveryEvidenceError) as stale:
        verify_recovery_evidence(
            evidence,
            trusted_public_key_path=authority_public,
            current_time="2030-01-15T02:00:00+00:00",
            max_age_seconds=3600,
            expected_dataset_identity=DATASET,
        )
    assert "RECOVERY_EVIDENCE_INVALID" in stale.value.rejection_codes
    forged = dataclasses.replace(evidence, signature="")
    with pytest.raises(RecoveryEvidenceError):
        verify_recovery_evidence(
            forged,
            trusted_public_key_path=authority_public,
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity=DATASET,
        )
    with pytest.raises(RecoveryEvidenceError) as wrong_dataset:
        verify_recovery_evidence(
            evidence,
            trusted_public_key_path=authority_public,
            current_time=WITHIN_WINDOW,
            max_age_seconds=3600,
            expected_dataset_identity="another-dataset",
        )
    assert "RECOVERY_EVIDENCE_INVALID" in wrong_dataset.value.rejection_codes


def test_ti_23_strict_mode_defaults_are_not_production_apis():
    """TI-23: 严格模式默认为关闭，且不构成生产运行期 API。"""
    reader = ShadowRuntimeReader()
    status = reader.status()
    assert status["require_authoritative_governance"] is False
    assert status["require_observation_attestation"] is False
    assert status["require_recovery_evidence_for_reenable"] is False
    assert status["production_runtime_reader"] == "NOT_ENABLED"
    assert status["production_activation"] == "NOT_ENABLED"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_new_authority_modules_remain_shadow_only():
    """新增信任组件不得进入生产运行期，且生产激活标志保持关闭。"""
    from utils.runtime_activation_design import activation_design_status
    from utils.runtime_mapping_policy import (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        RUNTIME_MAPPING_ACTIVATION,
    )

    assert RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED"
    assert PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"
    status = activation_design_status()
    assert status["activation_implementation_status"] == "NOT_IMPLEMENTED"
    assert status["activation_enable_api"] is None
    assert status["runtime_reader_api"] is None

    forbidden = (
        "authoritative_governance",
        "observation_authority",
        "recovery_authority",
        "trusted_artifacts",
    )
    for path in (
        os.path.join(REPO_ROOT, "app.py"),
        os.path.join(REPO_ROOT, "utils", "scraper.py"),
        os.path.join(REPO_ROOT, "utils", "daily_loader.py"),
        os.path.join(REPO_ROOT, "utils", "ingestion_identity.py"),
    ):
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for module in forbidden:
            assert module not in source, (path, module)


def _ctx(env, **overrides):
    return env.read_context(**overrides)


def _evidence(env) -> ShadowAuthorizationEvidence:
    return ShadowAuthorizationEvidence(
        authorization=env.authorization,
        decision=env.decision,
        proposal=env.proposal,
    )
