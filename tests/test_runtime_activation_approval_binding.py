"""B-07 executable baseline and authenticated-approval adversarial regression."""
from dataclasses import replace
import ast
import json
from pathlib import Path

import pytest

from tools.shadow_trusted_time_validation import SyntheticTimeScenario, NOW
from tools.shadow_approval_binding_validation import SyntheticApprovalScenario, run_acceptance
from tools.shadow_trusted_time_validation import protected_store_hashes
from utils.mapping_governance import (
    approval_fingerprint, GovernanceError, sign_mapping_approval, build_approval_body,
    proposal_from_observations, load_trusted_public_key,
    _decision_fingerprint, _replay_key_of, _transition_identity, _entry_hash,
)
from utils.runtime_activation_authorization import (
    build_activation_authorization_body, sign_runtime_activation_authorization,
    StrictActivationVerifier, build_approval_bound_authorization_body, ActivationAuthorizationError,
)
from utils.source_identity import fingerprint
from utils.authoritative_governance import (
    build_authoritative_governance_view, verify_authoritative_governance_view, ledger_identity,
)
from utils.trusted_artifacts import sign_trusted_artifact, TrustedArtifactError
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowReenablementError
from utils.runtime_mapping_eligibility import evaluate_runtime_mapping_eligibility, caller_asserted_provenance


@pytest.fixture
def baseline(tmp_path):
    return SyntheticTimeScenario(tmp_path / "baseline")


def activation(scene, *, decision=None, approval=None):
    decision = decision or scene.decision
    return sign_runtime_activation_authorization(scene.env.authorizer_private,
        build_activation_authorization_body(decision=decision, proposal=scene.env.proposal,
            dataset_identity=scene.context.dataset_identity, authorized_scope=scene.auth.authorized_scope,
            authorizer="synthetic-authorizer", operator="shadow-operator", authorized_at=NOW,
            expires_at="2030-01-15T00:10:00Z", approval_fingerprint=approval or ""))


def test_baseline_ab01_empty_approval_is_accepted(baseline):
    assert baseline.auth.approval_fingerprint == ""
    assert baseline.decision.approval_fingerprint == approval_fingerprint(baseline.env.approval)
    assert baseline.ledger.entries()[-1].to_state == "APPROVED"
    assert baseline.strict.verify(baseline.auth, baseline.context, time_evidence=baseline.token).is_consumable


def test_baseline_ab02_wrong_approval_is_accepted(baseline):
    auth = activation(baseline, approval="unrelated-owner-approval")
    assert baseline.strict.verify(auth, baseline.context, time_evidence=baseline.token).is_consumable


def test_baseline_ab03_matching_caller_assertion_is_accepted(baseline):
    decision = replace(baseline.env.decision, approval_fingerprint="caller-controlled")
    decision = replace(decision, decision_id=fingerprint(decision.identity_payload()))
    auth = activation(baseline, decision=decision, approval="caller-controlled")
    context = replace(baseline.context, eligibility_decision=decision, require_verified_governance=False)
    assert baseline.strict.verify(auth, context, time_evidence=baseline.token).is_consumable


def test_baseline_ab04_reader_already_rejects_superseded_state(baseline):
    baseline.ledger.append(proposal=baseline.env.proposal, to_state="SUPERSEDED",
        actor="shadow-owner", actor_role="owner", reason="explicit synthetic withdrawal",
        baseline_fingerprint=baseline.context.current_baseline_fingerprint)
    view = baseline.governance_view()
    result = baseline.read(authoritative_view=view)
    assert "GOVERNANCE_STATE_NOT_ACTIVE" in result.rejection_codes
    # Standalone time-only verification has no current view input at baseline.
    assert baseline.strict.verify(baseline.auth, baseline.context, time_evidence=baseline.token).is_consumable


def test_baseline_ab05_correct_current_approval_is_accepted(baseline):
    auth = activation(baseline, approval=approval_fingerprint(baseline.env.approval))
    assert baseline.strict.verify(auth, baseline.context, time_evidence=baseline.token).is_consumable
    assert baseline.read().is_resolved


@pytest.fixture
def scene(tmp_path):
    return SyntheticApprovalScenario(tmp_path / "approval")


def signed_activation(scene, **changes):
    return sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), **changes})


def sign_view(scene, view):
    return replace(view, attestation=sign_trusted_artifact(scene.governance_private,
        artifact_type="GOVERNANCE_VIEW_ATTESTATION", payload=view.attestation_payload()))


def rewrite_ledger(scene, mutation, *, rehash=True):
    """Hostile synthetic ledger edits; real checkpoint and owner audit must still reject."""
    entries = [entry.as_dict() for entry in scene.ledger.entries()]
    mutation(entries)
    previous = "0" * 64
    if rehash:
        for entry in entries:
            entry["previous_entry_hash"] = previous
            entry["decision_fingerprint"] = _decision_fingerprint(entry)
            entry["replay_key"] = _replay_key_of(entry)
            entry["transition_id"] = _transition_identity(entry)
            entry["entry_hash"] = _entry_hash(entry)
            previous = entry["entry_hash"]
    Path(scene.ledger.path).write_text("".join(json.dumps(entry, sort_keys=True)+"\n" for entry in entries), encoding="utf-8")


def rebuild(scene, **overrides):
    entries = scene.ledger.entries()
    checkpoint = sign_trusted_artifact(scene.checkpoint_private, artifact_type="GOVERNANCE_LEDGER_CHECKPOINT",
        payload={"dataset_identity": scene.context.dataset_identity, "ledger_identity": ledger_identity(scene.ledger),
                 "entry_count": len(entries), "chain_tip": entries[-1].entry_hash, "issued_at": NOW})
    return build_authoritative_governance_view(**{**scene.governance_inputs, "ledger_checkpoint": checkpoint, **overrides})


def test_ab01_empty_activation_approval_rejected(scene):
    result = scene.verify(signed_activation(scene, approval_fingerprint=""))
    assert "ACTIVATION_APPROVAL_MISSING" in result.rejection_codes
    assert result.signature_valid and not result.is_consumable


def test_ab02_wrong_activation_approval_rejected(scene):
    result = scene.verify(signed_activation(scene, approval_fingerprint="0" * 64))
    assert "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in result.rejection_codes
    assert result.signature_valid and not result.is_consumable


def test_ab03_matching_caller_assertions_do_not_authenticate_approval(scene):
    decision = replace(scene.decision, approval_fingerprint="0" * 64)
    decision = replace(decision, decision_id=fingerprint(decision.identity_payload()))
    auth = activation(scene, decision=decision, approval="0" * 64)
    missing = scene.strict.verify(auth, replace(scene.context, eligibility_decision=decision), time_evidence=scene.token)
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in missing.rejection_codes
    mismatched = scene.verify(auth, eligibility_decision=decision)
    assert "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in mismatched.rejection_codes


def test_ab04_noncurrent_approval_rejected(scene):
    scene.supersede()
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.verify().rejection_codes
    assert not scene.read().is_resolved


def test_ab05_correct_authenticated_approval_accepted(scene):
    result = scene.verify()
    assert result.is_consumable and result.signature_valid and result.trusted_time_verified
    record = scene.view.approved_record_for(scene.env.proposal.proposal_id)
    assert record.approval_fingerprint == approval_fingerprint(scene.env.approval)
    assert record.approval_fingerprint == scene.decision.approval_fingerprint == scene.auth.approval_fingerprint
    assert record.approved_transition_identity == scene.ledger.entries()[-1].transition_id
    assert record.ledger_entry_sequence == scene.ledger.entries()[-1].sequence


def test_ab06_forged_retained_owner_signature_rejected(scene):
    def mutate(entries):
        entries[-1]["approval_artifact"]["approval"]["signature"] = "A" * 88
        entries[-1]["approval_fingerprint"] = approval_fingerprint(entries[-1]["approval_artifact"])
    rewrite_ledger(scene, mutate)
    with pytest.raises(GovernanceError):
        rebuild(scene)


def test_ab07_wrong_owner_key_rejected(scene):
    body = {key: value for key, value in scene.env.approval.items() if key != "approval"}
    wrong = sign_mapping_approval(scene.checkpoint_private, body)
    def mutate(entries):
        entries[-1]["approval_artifact"] = wrong
        entries[-1]["approval_fingerprint"] = approval_fingerprint(wrong)
    rewrite_ledger(scene, mutate)
    with pytest.raises(GovernanceError, match="untrusted signing key"):
        rebuild(scene)


def test_ab08_wrong_owner_purpose_rejected(scene):
    def mutate(entries):
        entries[-1]["approval_artifact"]["purpose"] = "RUNTIME_ACTIVATION_TIME_ATTESTATION"
        entries[-1]["approval_fingerprint"] = approval_fingerprint(entries[-1]["approval_artifact"])
    rewrite_ledger(scene, mutate)
    with pytest.raises(GovernanceError, match="purpose"):
        rebuild(scene)


def test_ab09_ledger_approval_fingerprint_mismatch_rejected(scene):
    rewrite_ledger(scene, lambda rows: rows[-1].update(approval_fingerprint="0" * 64))
    with pytest.raises(GovernanceError, match="fingerprint"):
        rebuild(scene)


def test_ab10_missing_retained_approval_rejected(scene):
    rewrite_ledger(scene, lambda rows: rows[-1].update(approval_artifact=None))
    with pytest.raises(GovernanceError, match="without retained approval"):
        rebuild(scene)


def test_ab11_tampered_ledger_hash_rejected(scene):
    rewrite_ledger(scene, lambda rows: rows[-1].update(reason="tampered"), rehash=False)
    with pytest.raises(GovernanceError, match="hash mismatch"):
        rebuild(scene)


@pytest.mark.parametrize("checkpoint", [None, "forged"])
def test_ab12_signed_checkpoint_required(scene, checkpoint):
    value = None if checkpoint is None else replace(scene.governance_inputs["ledger_checkpoint"], signature="A" * 88)
    with pytest.raises(GovernanceError):
        rebuild(scene, ledger_checkpoint=value)
    with pytest.raises(GovernanceError):
        rebuild(scene, ledger_checkpoint_trusted_public_key_path=scene.governance_public)


def test_ab13_wrong_dataset_approval_rejected(scene):
    def mutate(entries):
        entries[-1]["approval_artifact"]["dataset_identity"] = "another-dataset"
        entries[-1]["approval_fingerprint"] = approval_fingerprint(entries[-1]["approval_artifact"])
    rewrite_ledger(scene, mutate)
    with pytest.raises(GovernanceError, match="dataset"):
        rebuild(scene)
    assert not scene.verify(dataset_identity="another-dataset").is_consumable


def test_ab14_wrong_canonical_baseline_rejected(scene):
    result = scene.verify(current_baseline_fingerprint="stale-baseline")
    assert "BASELINE_STALE" in result.rejection_codes
    assert "ACTIVATION_APPROVAL_SCOPE_MISMATCH" in result.rejection_codes


def test_view_builder_cannot_publish_old_approval_under_new_baseline(scene):
    canonical = sign_trusted_artifact(scene.checkpoint_private, artifact_type="CANONICAL_BASELINE_MANIFEST",
        payload={"dataset_identity": scene.context.dataset_identity, "baseline_fingerprint": "new-baseline",
                 "canonical_ids": list(scene.view.canonical_ids), "issued_at": NOW})
    with pytest.raises(TrustedArtifactError):
        rebuild(scene, current_baseline_fingerprint="new-baseline", canonical_manifest=canonical)


def test_ab15_wrong_source_identity_rejected(scene):
    result = scene.verify(signed_activation(scene, scoped_source_identity_key="another-source"))
    assert "ACTIVATION_APPROVAL_SCOPE_MISMATCH" in result.rejection_codes
    rewrite_ledger(scene, lambda rows: rows[-1].update(binding_source_key=scene.env.other_observation.observation_id))
    with pytest.raises(GovernanceError):
        rebuild(scene)


def test_ab16_wrong_canonical_candidate_rejected(scene):
    result = scene.verify(signed_activation(scene, candidate_canonical_id="other-canonical"))
    assert "ACTIVATION_APPROVAL_SCOPE_MISMATCH" in result.rejection_codes
    rewrite_ledger(scene, lambda rows: rows[-1].update(binding_canonical_id="other-canonical"))
    with pytest.raises(GovernanceError, match="scope"):
        rebuild(scene)


def test_ab17_eligibility_approval_mismatch_rejected(scene):
    decision = replace(scene.decision, approval_fingerprint="0" * 64)
    decision = replace(decision, decision_id=fingerprint(decision.identity_payload()))
    auth = activation(scene, decision=decision, approval=scene.auth.approval_fingerprint)
    assert "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in scene.verify(auth, eligibility_decision=decision).rejection_codes


def test_ab18_missing_or_ineligible_decision_rejected(scene):
    assert "ELIGIBILITY_DECISION_MISSING" in scene.verify(eligibility_decision=None).rejection_codes
    decision = replace(scene.decision, eligibility_result="NOT_ELIGIBLE")
    assert not scene.verify(eligibility_decision=decision).is_consumable


def test_ab19_expiry_cannot_be_overridden_by_approval(scene):
    scene.clock.advance(600)
    scene.token = scene.attestation(generation=11)
    result = scene.verify(current_time=NOW)
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in result.rejection_codes
    assert result.signature_valid and not result.is_consumable


def test_ab20_current_signed_revocation_remains_authoritative(scene):
    view = scene.governance_view([(scene.auth.activation_id, scene.auth.revocation_reference)])
    result = scene.verify(view=view, revocation_registry=())
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in result.rejection_codes
    assert not scene.read(authoritative_view=view).is_resolved


def test_ab21_lifetime_ceiling_remains_enforced(scene):
    result = scene.verify(scene.bound_authorization(expires_at="2030-01-16T00:00:01Z"))
    assert "ACTIVATION_LIFETIME_EXCEEDED" in result.rejection_codes


def test_ab22_untrusted_view_signer_and_missing_anchor_rejected(scene):
    forged = replace(scene.view, attestation=sign_trusted_artifact(scene.checkpoint_private,
        artifact_type="GOVERNANCE_VIEW_ATTESTATION", payload=scene.view.attestation_payload()))
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in scene.verify(view=forged).rejection_codes
    with pytest.raises(ValueError):
        StrictActivationVerifier(scene.time, require_authenticated_approval=True)


def test_ab23_tampered_approval_facts_rejected(scene):
    record = replace(scene.view.approved_records[0], approval_fingerprint="0" * 64)
    tampered = replace(scene.view, approved_records=(record,))
    assert not scene.verify(view=tampered).is_consumable


def test_ab24_legacy_view_cannot_satisfy_strict_approval(scene):
    legacy = sign_view(scene, replace(scene.view, schema_version="1.0.0", approved_records=()))
    verify_authoritative_governance_view(legacy, trusted_public_key_path=scene.governance_public, current_time=NOW)
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in scene.verify(view=legacy).rejection_codes


def test_ab25_real_replacement_approval_invalidates_old_authorization(scene):
    old_view = scene.view
    scene.supersede()
    new_proposal = proposal_from_observations([scene.env.observation],
        requested_action=scene.env.proposal.requested_action,
        candidate_canonical_id=scene.env.proposal.candidate_canonical_id,
        existing_canonical_owner=scene.env.proposal.existing_canonical_owner,
        ingestion_plan_reference="planner:ACCEPTED", expected_baseline_fingerprint=scene.context.current_baseline_fingerprint,
        reason="separately approved replacement", identity_evidence_type=scene.env.proposal.identity_evidence_type,
        operator="shadow-operator")
    owner_private = str(Path(scene.env.owner_public).with_name("owner_private.pem"))
    approval = sign_mapping_approval(owner_private, build_approval_body(proposal=new_proposal,
        source_observation_fingerprint=scene.env.observation.semantic_fingerprint(), dataset_identity=scene.context.dataset_identity,
        operator="shadow-operator", approver="shadow-owner", approved_at=NOW, reason="replacement approved"))
    for state, actor, role in (("PROPOSED", "shadow-operator", "operator"), ("APPROVED", "shadow-owner", "owner")):
        scene.ledger.append(proposal=new_proposal, to_state=state, actor=actor, actor_role=role,
            reason="replacement", baseline_fingerprint=scene.context.current_baseline_fingerprint,
            **({"approval": approval, "trusted_public_key_path": scene.env.owner_public} if state == "APPROVED" else {}))
    scene.view = scene.governance_view()
    assert scene.view.approved_record_for(scene.env.proposal.proposal_id) is None
    assert scene.view.approved_record_for(new_proposal.proposal_id).approval_fingerprint == approval_fingerprint(approval)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.verify().rejection_codes
    # Once the current view is observed, an older still-fresh signed view cannot roll it back.
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.verify(view=old_view).rejection_codes
    current_context = replace(scene.eligibility_context, authoritative_view=scene.view,
        recorded_proposal_id=new_proposal.proposal_id, recorded_proposal_fingerprint=new_proposal.fingerprint(),
        known_source_bindings=tuple((key, can) for key, can, _ in scene.view.active_bindings))
    decision = evaluate_runtime_mapping_eligibility(new_proposal, approval, scene.env.observation,
        current_context, evaluated_at=NOW)
    assert decision.is_eligible
    body = build_approval_bound_authorization_body(verifier=scene.strict, governance_view=scene.view,
        time_evidence=scene.token, decision=decision, proposal=new_proposal,
        dataset_identity=scene.context.dataset_identity, authorized_scope=scene.auth.authorized_scope,
        authorizer="synthetic-authorizer", operator="shadow-operator", authorized_at=NOW,
        expires_at="2030-01-15T00:10:00Z")
    current_auth = sign_runtime_activation_authorization(scene.env.authorizer_private, body)
    result = scene.strict.verify(current_auth, replace(scene.context, proposal=new_proposal,
        eligibility_decision=decision), time_evidence=scene.token, governance_view=scene.view)
    assert result.is_consumable


def test_ab26_legacy_standalone_inspection_is_non_authoritative(scene):
    result = ShadowRuntimeReader().read(observation=scene.env.observation, snapshot=scene.snapshot,
        read_context=scene.read_context(current_time=NOW))
    assert result.is_resolved and result.is_standalone_inspection
    assert not result.is_production_authorization


def test_ab27_reader_rejects_approval_mismatch(scene):
    wrong = signed_activation(scene, approval_fingerprint="0" * 64)
    evidence = replace(scene.evidence, authorization=wrong)
    result = scene.read(evidence=(evidence,))
    assert "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in result.rejection_codes
    assert not result.is_resolved


def test_ab28_reader_resolves_current_authenticated_approval(scene):
    assert scene.read(current_time="invalid").is_resolved
    assert scene.read().checked_at == NOW


def test_ab29_reenable_rejects_obsolete_approval(scene):
    recovery = scene.audited_recovery()
    scene.reader.disable(at=NOW)
    scene.supersede()
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=recovery)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in exc.value.rejection_codes
    assert not scene.reader.enabled


@pytest.mark.parametrize("disabled", [False, True])
def test_ab30_rollback_cannot_revive_obsolete_approval(scene, disabled):
    simulator = scene.simulator()
    if disabled:
        simulator.disable_shadow_runtime(at=NOW)
    scene.supersede()
    previous = scene.registry.active_snapshot_id
    result = simulator.rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint(),
        reconcile_disabled_reader=disabled)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in result.rejection_codes
    assert scene.registry.active_snapshot_id == previous
    assert scene.reader.enabled is not disabled


def test_ab31_signed_governance_and_recovery_compatible(scene):
    recovery = scene.audited_recovery()
    assert scene.reader.enabled and scene.read().is_resolved
    assert recovery.payload()["selected_snapshot_id"] == scene.registry.active_snapshot_id


def test_ab32_demo_preserves_all_seven_stores(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path)
    assert result["success"] and len(result["checks"]) == 12
    assert before == protected_store_hashes()


def test_ab33_no_production_integration():
    root = Path(__file__).resolve().parents[1]
    for path in (root / "app.py", root / "utils/scraper.py", root / "utils/ingestion_identity.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "shadow_approval_binding_validation" not in (node.module or "")
                assert all(alias.name != "build_approval_bound_authorization_body" for alias in node.names)
    from utils.runtime_mapping_policy import RUNTIME_MAPPING_ACTIVATION, RUNTIME_MAPPING_WRITE_AUTHORIZATION
    assert RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED" and RUNTIME_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"


def test_ab34_cross_platform_ci_matrix_retained():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert workflow.count("os: [ubuntu-latest, windows-latest]") == 2
    assert 'python-version: ["3.12"]' in workflow


def test_independent_five_signer_roles(scene):
    paths = (scene.env.owner_public, scene.checkpoint_public, scene.governance_public,
             scene.env.authorizer_public, scene.public)
    assert len({load_trusted_public_key(path)["public_key_id"] for path in paths}) == 5


@pytest.mark.parametrize("missing", ["registry", "time", "governance", "verifier"])
def test_incomplete_strict_reader_configuration_rejected(scene, missing):
    kwargs = dict(registry=scene.registry, require_authenticated_approval=True,
        require_authoritative_governance=True, require_trusted_time=True, strict_activation_verifier=scene.strict)
    if missing == "registry": kwargs["registry"] = None
    if missing == "time": kwargs["require_trusted_time"] = False
    if missing == "governance": kwargs["require_authoritative_governance"] = False
    if missing == "verifier": kwargs["strict_activation_verifier"] = StrictActivationVerifier(scene.time)
    with pytest.raises(ValueError):
        ShadowRuntimeReader(**kwargs)


@pytest.mark.parametrize("provided", ["", "0" * 64])
def test_issuance_derives_fingerprint_and_rejects_inconsistent_input(scene, provided):
    with pytest.raises(ActivationAuthorizationError) as exc:
        scene.bound_authorization(approval_fingerprint=provided)
    assert "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in exc.value.rejection_codes


def test_eligibility_rejects_other_signed_approval_for_same_proposal(scene):
    owner_private = str(Path(scene.env.owner_public).with_name("owner_private.pem"))
    body = {key: value for key, value in scene.env.approval.items() if key != "approval"}
    other = sign_mapping_approval(owner_private, {**body, "reason": "another signed owner decision"})
    decision = evaluate_runtime_mapping_eligibility(scene.env.proposal, other, scene.env.observation,
        scene.eligibility_context, evaluated_at=NOW)
    assert not decision.is_eligible and "EVIDENCE_PROVENANCE_UNSUPPORTED" in decision.rejection_codes


def test_strict_eligibility_never_upgrades_caller_assertions(scene):
    context = replace(scene.eligibility_context, evidence_provenance=caller_asserted_provenance(
        dataset_identity=scene.context.dataset_identity, baseline_fingerprint=scene.context.current_baseline_fingerprint))
    decision = evaluate_runtime_mapping_eligibility(scene.env.proposal, scene.env.approval,
        scene.env.observation, context, evaluated_at=NOW)
    assert not decision.is_eligible and decision.is_conditional


@pytest.mark.parametrize("changes", [{"decision_id": "0" * 64}, {"schema_version": "unsupported"},
                                    {"evaluator_version": "unsupported"}, {"evidence_fingerprint": "0" * 64}])
def test_decision_identity_and_versions_recomputed(scene, changes):
    decision = replace(scene.decision, **changes)
    auth = activation(scene, decision=decision, approval=scene.auth.approval_fingerprint)
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in scene.verify(auth, eligibility_decision=decision).rejection_codes


@pytest.mark.parametrize("records", ["missing", "duplicate", "malformed"])
def test_missing_or_contradictory_approval_records_rejected(scene, records):
    value = {"missing": (), "duplicate": scene.view.approved_records * 2, "malformed": ("invalid",)}[records]
    view = replace(scene.view, approved_records=value)
    if records != "malformed":
        view = sign_view(scene, view)
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in scene.verify(view=view).rejection_codes


def test_same_ledger_state_cannot_equivocate_approval_identity(scene):
    record = replace(scene.view.approved_records[0], approval_fingerprint="0" * 64)
    view = sign_view(scene, replace(scene.view, approved_records=(record,)))
    decision = replace(scene.decision, approval_fingerprint="0" * 64)
    decision = replace(decision, decision_id=fingerprint(decision.identity_payload()))
    auth = activation(scene, decision=decision, approval="0" * 64)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.verify(auth, view=view, eligibility_decision=decision).rejection_codes


def test_ledger_state_machine_contradiction_rejected(scene):
    rewrite_ledger(scene, lambda entries: entries[-1].update(from_state="REVIEW_REQUIRED"))
    with pytest.raises(GovernanceError, match="contradictory"):
        rebuild(scene)


def test_ledger_change_during_approval_audit_fails_closed(scene, monkeypatch):
    import utils.authoritative_governance as module
    original = module.audit_historical_approvals
    def changing(ledger, **kwargs):
        result = original(ledger, **kwargs)
        scene.ledger.append(proposal=scene.env.proposal, to_state="SUPERSEDED", actor="shadow-owner",
            actor_role="owner", reason="concurrent state change", baseline_fingerprint=scene.context.current_baseline_fingerprint)
        return result
    monkeypatch.setattr(module, "audit_historical_approvals", changing)
    with pytest.raises(TrustedArtifactError, match="changed during"):
        rebuild(scene)


def test_stale_approval_view_uses_authenticated_time(scene):
    scene.clock.advance(3601)
    scene.token = scene.attestation(generation=11)
    result = scene.verify(current_time=NOW)
    assert "TRUSTED_ARTIFACT_STALE" in result.rejection_codes
    assert not result.is_consumable


def test_approval_view_continuity_cannot_silently_reset(scene):
    object.__setattr__(scene.strict, "_approval_view_state", None)
    assert "ACTIVATION_APPROVAL_NOT_AUTHENTICATED" in scene.verify().rejection_codes


def test_snapshot_must_bind_the_exact_eligible_decision(scene):
    binding = replace(scene.snapshot.bindings[0], eligibility_decision_fingerprint="0" * 64)
    changed = replace(scene.snapshot, bindings=(binding,))
    digest = changed.content_fingerprint()
    changed = replace(changed, snapshot_id=digest, snapshot_fingerprint=digest)
    scene.registry.register(changed)
    scene.registry.select(changed.snapshot_id)
    result = scene.reader.read(observation=scene.env.observation, snapshot=changed,
        read_context=scene.read_context(), observation_attestation=scene.observation_attestation)
    assert not result.is_resolved and "AUTHORIZATION_NOT_CONSUMABLE" in result.rejection_codes


def test_demo_failure_returns_nonzero(monkeypatch, capsys):
    import tools.shadow_approval_binding_validation as module
    monkeypatch.setattr(module, "run_acceptance", lambda root: {"success": False})
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["success"] is False
