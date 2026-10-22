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
    LEDGER_CHECKPOINT_TYPE,
    REVOCATION_MANIFEST_TYPE,
    build_authoritative_governance_view,
    ledger_identity,
)
from utils.mapping_governance import (
    MappingState,
    generate_mapping_owner_keypair,
    GovernanceLedger,
)
from utils.observation_authority import sign_observation_attestation
from utils.recovery_authority import (
    DIVERGENCE_EVENT_WITHOUT_POINTER,
    build_recovery_evidence,
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
from utils.trusted_artifacts import sign_trusted_artifact

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
        view_signing_private_key_path=authority_private,
        current_time=WITHIN_WINDOW,
    )
    return {
        "view": view,
        "authority_private": authority_private,
        "authority_public": authority_public,
        "ledger": ledger,
        "baseline": baseline,
        "checkpoint": checkpoint,
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
        authoritative_view_trusted_public_key_path=governance["authority_public"],
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
        authoritative_view_trusted_public_key_path=governance["authority_public"],
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
        authoritative_view_trusted_public_key_path=governance["authority_public"],
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
        registry=registry, require_recovery_evidence_for_reenable=True
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
        registry=registry, require_recovery_evidence_for_reenable=True
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
