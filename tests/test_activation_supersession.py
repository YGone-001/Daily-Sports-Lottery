"""SS-01..SS-44 与附加对抗测试；所有密钥/工件只存在于临时合成目录。"""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import ast
import itertools

import pytest

from tools.shadow_activation_supersession_validation import SyntheticSupersessionScenario, run_acceptance
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.activation_supersession import (
    ActivationSupersessionVerifier, AuthorizationLifecycleEvidence,
    SUPERSESSION_PURPOSE, STATE_PURPOSE, CHECKPOINT_PURPOSE, checkpoint_payload,
)
from utils.runtime_activation_authorization import sign_runtime_activation_authorization, ShadowActivationSnapshotError
from utils.shadow_activation_simulation import ShadowSnapshotRegistry
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowAuthorizationEvidence, ShadowReenablementError
from utils.trusted_artifacts import sign_trusted_artifact, TrustedArtifactError


@pytest.fixture
def scene(tmp_path):
    return SyntheticSupersessionScenario(tmp_path / "supersession")


def rejected(scene, bundle, code=None, **context):
    with pytest.raises(TrustedArtifactError) as exc:
        scene.verified(bundle, **context)
    if code:
        assert code in exc.value.rejection_codes, exc.value.rejection_codes
    return exc.value.rejection_codes


def signed_event(scene, **changes):
    return sign_trusted_artifact(scene.lifecycle_private, artifact_type=SUPERSESSION_PURPOSE,
        payload={**scene.event.payload(), **changes})


def test_ss01_expired_predecessor_does_not_block_current(scene):
    scene.clock.advance(6)
    scene.token = scene.attestation(generation=11)
    assert scene.read().is_resolved
    old = next(e for e in scene.verified().entries if e.activation_id == scene.old.activation_id)
    assert old.expired and old.selection == "SUPERSEDED"


def test_ss02_revoked_predecessor_does_not_block_current(scene):
    scene.view = scene.governance_view(((scene.old.activation_id, scene.old.revocation_reference),))
    scene.bundle = scene.make_bundle()
    assert scene.read(revocation_registry=()).is_resolved
    assert any(e.revoked and e.selection == "SUPERSEDED" for e in scene.verified().entries)


@pytest.mark.parametrize("revoked", [False, True], ids=["SS-03-expired", "SS-04-revoked"])
def test_ss03_ss04_no_authenticated_supersession_fails_closed(scene, revoked):
    if revoked:
        scene.view = scene.governance_view(((scene.old.activation_id, scene.old.revocation_reference),))
    else:
        scene.clock.advance(6)
        scene.token = scene.attestation(generation=11)
    bundle = scene.make_bundle(transitions=(), current=(scene.old.activation_id, scene.auth.activation_id))
    rejected(scene, bundle, "LIFECYCLE_AMBIGUOUS_CURRENT")
    assert not scene.read(lifecycle_evidence=bundle).is_resolved


def test_ss05_unrelated_current_rejects(scene):
    bundle = scene.make_bundle(transitions=(), current=(scene.old.activation_id, scene.auth.activation_id))
    rejected(scene, bundle, "LIFECYCLE_AMBIGUOUS_CURRENT")


def test_ss06_signed_explicit_supersession_accepted(scene):
    view = scene.verified()
    assert view.current_for(scene.auth.scoped_source_identity_key).activation_id == scene.auth.activation_id
    assert scene.read().authorization_ids == (scene.auth.activation_id,)


def test_ss07_missing_lifecycle_evidence_rejected(scene):
    result = scene.read(lifecycle_evidence=None)
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "LIFECYCLE_EVIDENCE_MISSING" in result.rejection_codes
    assert not scene.read(lifecycle_evidence=scene.verified()).is_resolved


def test_ss08_forged_signature_rejected(scene):
    forged = replace(scene.event, signature="A" * 88)
    rejected(scene, scene.make_bundle(transitions=(forged,)), "TRUSTED_ARTIFACT_SIGNATURE_INVALID")


def test_ss09_wrong_lifecycle_signer_rejected(scene):
    event = sign_trusted_artifact(scene.env.authorizer_private, artifact_type=SUPERSESSION_PURPOSE,
        payload=scene.event.payload())
    rejected(scene, scene.make_bundle(transitions=(event,)), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


@pytest.mark.parametrize("outer", [True, False])
def test_ss10_wrong_purpose_rejected(scene, outer):
    event = (sign_trusted_artifact(scene.lifecycle_private, artifact_type=STATE_PURPOSE, payload=scene.event.payload())
             if outer else signed_event(scene, purpose=STATE_PURPOSE))
    rejected(scene, scene.make_bundle(transitions=(event,)),
        "TRUSTED_ARTIFACT_TYPE_MISMATCH" if outer else "LIFECYCLE_CONTRACT_INVALID")


@pytest.mark.parametrize("field,value,code", [
    ("dataset_identity", "other-dataset", "LIFECYCLE_DATASET_MISMATCH"),
    ("baseline_fingerprint", "old-baseline", "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
    ("source_identity_key", "unrelated-source", "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
    ("canonical_id", "other-owner", "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
    ("approval_fingerprint", "0" * 64, "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
    ("eligibility_decision_fingerprint", "0" * 64, "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
    ("authorized_scope", "OTHER_SCOPE", "LIFECYCLE_TRANSITION_BINDING_MISMATCH"),
], ids=["SS-11", "SS-12", "SS-13", "SS-14", "SS-15", "SS-16", "SS-17"])
def test_ss11_to_ss17_transition_binding(scene, field, value, code):
    rejected(scene, scene.make_bundle(transitions=(signed_event(scene, **{field: value}),)), code)


@pytest.mark.parametrize("signature_only", [False, True])
def test_ss18_same_activation_id_different_bytes_rejected(scene, signature_only):
    changed = (replace(scene.old, signature="A" * 88) if signature_only else
        sign_runtime_activation_authorization(scene.env.authorizer_private,
            {**scene.old.as_dict(), "reason": "different content, same activation ID"}))
    evidence = ShadowAuthorizationEvidence(changed, scene.decision, scene.env.proposal)
    rejected(scene, scene.make_bundle(authorizations=(scene.old_evidence, evidence, scene.evidence)),
        "LIFECYCLE_ACTIVATION_ID_CONFLICT")


def test_ss19_identical_event_and_authorization_are_idempotent(scene):
    bundle = scene.make_bundle(transitions=(scene.event, scene.event),
        authorizations=(scene.evidence, scene.old_evidence, scene.old_evidence))
    assert scene.verified(bundle) == scene.verified()


def test_ss20_same_transition_id_with_different_content_rejected(scene):
    other = signed_event(scene, reason="conflicting reason")
    rejected(scene, scene.make_bundle(transitions=(scene.event, other)), "LIFECYCLE_TRANSITION_ID_CONFLICT")


def test_ss21_cycle_rejected_independent_of_order(scene):
    back = scene.transition(scene.auth, scene.old, "B-to-A", 3)
    for events in itertools.permutations((scene.event, back)):
        rejected(scene, scene.make_bundle(transitions=events, current=(), sequence=3), "LIFECYCLE_CYCLE")


def chain(scene):
    auth = scene.bound_authorization(reason="current C")
    evidence = ShadowAuthorizationEvidence(auth, scene.decision, scene.env.proposal)
    event = scene.transition(scene.auth, auth, "B-to-C", 3)
    return auth, evidence, event


def test_ss22_conflicting_successors_rejected(scene):
    auth, evidence, _ = chain(scene)
    conflict = scene.transition(scene.old, auth, "A-to-C", 3)
    rejected(scene, scene.make_bundle(authorizations=(scene.old_evidence, scene.evidence, evidence),
        transitions=(scene.event, conflict), current=(scene.auth.activation_id, auth.activation_id), sequence=3),
        "LIFECYCLE_SUCCESSOR_CONFLICT")


def test_ss23_untrusted_state_rejected(scene):
    state = sign_trusted_artifact(scene.governance_private, artifact_type=STATE_PURPOSE, payload=scene.bundle.state.payload())
    rejected(scene, replace(scene.bundle, state=state), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


def test_ss24_old_signed_state_after_newer_rejected(scene):
    old = scene.make_bundle(authorizations=(scene.old_evidence,), transitions=(), current=(scene.old.activation_id,), sequence=1)
    assert scene.verified(old).sequence == 1
    assert scene.verified().sequence == 2
    rejected(scene, old, "LIFECYCLE_STATE_ROLLBACK")


def test_ss25_current_signed_revocation_cannot_be_overridden(scene):
    scene.view = scene.governance_view(((scene.auth.activation_id, scene.auth.revocation_reference),))
    scene.bundle = scene.make_bundle()
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read(revocation_registry=()).rejection_codes
    # Even a later validly signed view cannot erase an observed revocation.
    scene.view = scene.governance_view(())
    rejected(scene, scene.make_bundle(sequence=3), "LIFECYCLE_REVOCATION_REMOVED")


def test_ss26_current_expiry_rejects_despite_caller_earlier_time(scene):
    scene.clock.advance(600)
    scene.token = scene.attestation(generation=11)
    scene.view = scene.governance_view()
    # Fresh attestations cannot extend the authorization's own lifetime.
    scene.event = scene.transition(scene.old, scene.auth, "A-to-B", 2)
    scene.bundle = scene.make_bundle(sequence=3)
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in scene.read(current_time=NOW).rejection_codes


def test_ss27_obsolete_approval_rejected(scene):
    scene.supersede()
    scene.bundle = scene.make_bundle()
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.read().rejection_codes


def test_ss28_old_canonical_baseline_rejected(scene):
    rejected(scene, scene.make_bundle(baseline_fingerprint="obsolete"), "LIFECYCLE_BASELINE_MISMATCH")
    assert not scene.read(current_baseline_fingerprint="new-baseline").is_resolved


def test_ss29_chain_resolves_only_C(scene):
    assert scene.read().is_resolved
    auth, evidence, event = chain(scene)
    scene.bundle = scene.make_bundle(authorizations=(*scene.bundle.authorizations, evidence),
        transitions=(scene.event, event), current=(auth.activation_id,), sequence=3)
    snapshot = scene.snapshot_for((auth,))
    scene.registry.register(snapshot)
    scene.registry.select(snapshot.snapshot_id)
    result = scene.read()
    assert result.is_resolved and result.authorization_ids == (auth.activation_id,)
    assert sum(e.selection == "SUPERSEDED" for e in scene.verified().entries) == 2


def test_ss30_replayed_A_after_C_rejected(scene):
    auth, evidence, event = chain(scene)
    scene.bundle = scene.make_bundle(authorizations=(*scene.bundle.authorizations, evidence),
        transitions=(scene.event, event), current=(auth.activation_id,), sequence=3)
    scene.verified()
    scene.registry.select(scene.old_snapshot.snapshot_id)
    assert "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in scene.read().rejection_codes
    replay = scene.make_bundle(authorizations=(scene.old_evidence,), transitions=(),
        current=(scene.old.activation_id,), sequence=4)
    rejected(scene, replay, "LIFECYCLE_HISTORY_REMOVED")


def test_ss31_history_inspectable_nonconsumable_immutable(scene):
    assert scene.bundle.authorizations[0].authorization == scene.old
    assert scene.verified().current_for(scene.old.scoped_source_identity_key).activation_id != scene.old.activation_id
    with pytest.raises(FrozenInstanceError):
        scene.bundle.authorizations = ()
    scene.registry.select(scene.old_snapshot.snapshot_id)
    assert not scene.read().is_resolved


def test_ss32_registration_order_not_authority(scene):
    for snapshots in itertools.permutations((scene.snapshot, scene.old_snapshot)):
        registry = ShadowSnapshotRegistry()
        for snapshot in snapshots:
            registry.register(snapshot)
        assert registry.active() is None
        registry.select(scene.snapshot.snapshot_id)
        scene.reader._registry = registry
        assert scene.reader.read(observation=scene.env.observation, snapshot=registry.active(),
            read_context=scene.read_context(), observation_attestation=scene.observation_attestation).is_resolved


def test_ss33_content_order_not_authority(scene):
    for evidence in itertools.permutations(scene.bundle.authorizations):
        view = scene.verified(scene.make_bundle(authorizations=evidence))
        assert view == scene.verified()
    # Effective snapshot construction remains deterministic and has only B.
    assert scene.snapshot_for((scene.auth,)).content_fingerprint() == scene.snapshot.content_fingerprint()
    combined = scene.snapshot_for((scene.old, scene.auth))
    for bindings in itertools.permutations(combined.bindings):
        snapshot = replace(combined, bindings=bindings)
        snapshot = replace(snapshot, snapshot_id=snapshot.content_fingerprint(), snapshot_fingerprint=snapshot.content_fingerprint())
        scene.registry.register(snapshot)
        scene.registry.select(snapshot.snapshot_id)
        assert "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in scene.read().rejection_codes


def test_ss34_strict_reader_current_B_and_not_found(scene):
    assert scene.read().is_resolved
    result = scene.reader.read(observation=scene.env.other_observation, snapshot=scene.snapshot,
        read_context=scene.read_context(), observation_attestation=None)
    assert result.resolution_status == "REJECTED_SHADOW"  # provenance remains mandatory
    assert scene.reader.status()["require_verified_supersession"]


def test_ss35_ambiguous_lifecycle_never_not_found(scene):
    bad = scene.make_bundle(transitions=(), current=(scene.old.activation_id, scene.auth.activation_id))
    result = scene.read(lifecycle_evidence=bad)
    assert result.resolution_status == "REJECTED_SHADOW" and "LIFECYCLE_AMBIGUOUS_CURRENT" in result.rejection_codes


def test_ss36_reenable_rejects_superseded_A(scene):
    scene.reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.old_evidence)
    assert "LIFECYCLE_ACTIVATION_NOT_CURRENT" in exc.value.rejection_codes
    assert not scene.reader.enabled


def test_ss37_rollback_obsolete_snapshot_rejected(scene):
    report = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.old_snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint())
    assert not report.succeeded and "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in report.rejection_codes
    assert scene.registry.active_snapshot_id == scene.snapshot.snapshot_id


def test_ss38_emergency_disable_needs_no_signature(scene):
    previous = scene.read()
    scene.bundle = None
    scene.simulator().disable_shadow_runtime(at=NOW)
    assert not scene.reader.enabled and not scene.reader.is_generation_current(previous)
    assert not scene.read(lifecycle_evidence=None, trusted_time_evidence=None).is_resolved


def test_ss39_time_and_max_lifetime_enforced(scene):
    assert "TRUSTED_TIME_EVIDENCE_MISSING" in scene.read(trusted_time_evidence=None).rejection_codes
    auth = scene.bound_authorization(expires_at="2030-01-16T00:00:01Z")
    evidence = ShadowAuthorizationEvidence(auth, scene.decision, scene.env.proposal)
    rejected(scene, scene.make_bundle(authorizations=(evidence,), transitions=(), current=(auth.activation_id,)),
        "ACTIVATION_LIFETIME_EXCEEDED")


def test_ss40_recovery_state_still_mandatory(scene):
    scene.reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence)
    assert "RE_ENABLE_RECOVERY_EVIDENCE_MISSING" in exc.value.rejection_codes
    assert not scene.reader.enabled


def test_ss41_production_imports_remain_isolated():
    root = Path(__file__).resolve().parents[1]
    for filename in ("utils/scraper.py", "app.py", "utils/ingestion_identity.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8-sig"))
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
        assert not any("activation_supersession" in module or "shadow_runtime_reader" in module for module in modules)


def test_ss42_functional_demo_and_seven_store_integrity(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path / "functional")
    assert result["success"] and all(result["checks"].values())
    assert before == protected_store_hashes() == result["seven_store_sha256"]


def test_ss43_existing_approval_and_activation_engine_reused(scene):
    assert scene.verify().is_consumable
    assert not scene.verify(sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), "approval_fingerprint": "wrong"})).is_consumable
    # Full existing AB/AA suites are run separately and in the complete CI suite.


def test_ss44_cross_platform_ci_contract():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert workflow.count("os: [ubuntu-latest, windows-latest]") == 2
    assert 'python-version: ["3.12"]' in workflow
    assert 'python -m pytest -q -rs' in workflow
    # Configuration evidence only. Actual four-job results must come from GitHub.


@pytest.mark.parametrize("field,value", [("registry", None), ("require_authoritative_governance", False),
    ("require_authenticated_approval", False), ("require_trusted_time", False), ("supersession_verifier", None)])
def test_strict_configuration_fails_during_construction(scene, field, value):
    kwargs = dict(registry=scene.registry, require_authoritative_governance=True,
        require_authenticated_approval=True, require_trusted_time=True,
        strict_activation_verifier=scene.strict, require_verified_supersession=True, supersession_verifier=scene.lifecycle)
    kwargs[field] = value
    with pytest.raises(ValueError):
        ShadowRuntimeReader(**kwargs)


def test_stale_event_and_checkpoint_rejected(scene):
    scene.clock.advance(301)
    scene.token = scene.attestation(generation=11)
    scene.view = scene.governance_view()
    rejected(scene, scene.make_bundle(sequence=3), "TRUSTED_ARTIFACT_STALE")


def test_restart_requires_fresh_independent_checkpoint(scene):
    scene.verified()
    restarted = scene.new_lifecycle_verifier()
    with pytest.raises(TrustedArtifactError) as exc:
        restarted.verify(scene.bundle, read_context=scene.read_context())
    assert "LIFECYCLE_CHECKPOINT_MISMATCH" in exc.value.rejection_codes
    checkpoint = sign_trusted_artifact(scene.checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload=checkpoint_payload(scene.bundle.state, session_challenge=restarted.session_challenge, issued_at=NOW))
    assert restarted.verify(replace(scene.bundle, checkpoint=checkpoint), read_context=scene.read_context()).sequence == 2


def test_continuity_loss_fails_closed(scene):
    scene.verified()
    object.__setattr__(scene.lifecycle, "_previous", None)
    rejected(scene, scene.bundle, "LIFECYCLE_CONTINUITY_LOST")


def test_role_substitution_configuration_rejected(scene):
    with pytest.raises(ValueError):
        ActivationSupersessionVerifier(scene.context.dataset_identity, "authority",
            scene.env.authorizer_public, scene.checkpoint_public, scene.env.authorizer_public, scene.strict)


def test_historical_forged_artifact_cannot_be_silently_ignored(scene):
    old = replace(scene.old, signature="A" * 88)
    evidence = ShadowAuthorizationEvidence(old, scene.decision, scene.env.proposal)
    event = scene.transition(old, scene.auth, "forged-A-to-B", 2)
    rejected(scene, scene.make_bundle(authorizations=(evidence, scene.evidence), transitions=(event,)), "SIGNATURE_INVALID")


def test_unknown_successor_and_predecessor_rejected(scene):
    for field in ("old_activation_id", "new_activation_id"):
        rejected(scene, scene.make_bundle(transitions=(signed_event(scene, **{field: "unknown"}),)),
            "LIFECYCLE_UNKNOWN_AUTHORIZATION")


def test_no_effective_binding_duplicates_or_obsolete_history_in_snapshot(scene):
    for auths in ((scene.old, scene.auth), (scene.old,)):
        snapshot = scene.snapshot_for(auths)
        scene.registry.register(snapshot)
        scene.registry.select(snapshot.snapshot_id)
        assert "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in scene.read().rejection_codes
    # The legacy builder still refuses to place expired history into a snapshot.
    with pytest.raises(ShadowActivationSnapshotError):
        from utils.runtime_activation_authorization import build_shadow_activation_snapshot
        build_shadow_activation_snapshot(authorizations=(scene.old,),
            verification_context=replace(scene.context, current_time="2030-01-15T00:00:06Z"),
            observations=(scene.env.observation,), generated_at=NOW)


def test_different_state_at_same_sequence_rejected(scene):
    scene.verified()
    rejected(scene, scene.make_bundle(issued_at="2030-01-15T00:00:00Z"), "LIFECYCLE_STATE_ROLLBACK")


def test_signed_state_cannot_omit_event_or_inventory(scene):
    rejected(scene, scene.make_bundle(transitions=[]), "LIFECYCLE_CURRENT_MISMATCH")
    rejected(scene, scene.make_bundle(authorizations=(scene.evidence,)), "LIFECYCLE_UNKNOWN_AUTHORIZATION")


def test_no_implicit_reenable_on_valid_lifecycle(scene):
    assert scene.read().is_resolved
    scene.reader.disable(at=NOW)
    assert scene.verified().current_for(scene.auth.scoped_source_identity_key)
    assert not scene.read().is_resolved and not scene.reader.enabled


@pytest.mark.parametrize("part", ["state", "checkpoint"])
def test_forged_state_or_checkpoint(scene, part):
    bad = replace(getattr(scene.bundle, part), signature="A" * 88)
    rejected(scene, replace(scene.bundle, **{part: bad}), "TRUSTED_ARTIFACT_SIGNATURE_INVALID")


def test_checkpoint_role_cannot_be_substituted(scene):
    checkpoint = sign_trusted_artifact(scene.lifecycle_private, artifact_type=CHECKPOINT_PURPOSE,
        payload=scene.bundle.checkpoint.payload())
    rejected(scene, replace(scene.bundle, checkpoint=checkpoint), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


def test_stale_checkpoint_rejects_even_fresh_current_state(scene):
    from datetime import timedelta
    checkpoint = sign_trusted_artifact(scene.checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload={**scene.bundle.checkpoint.payload(), "issued_at": (scene.clock() - timedelta(seconds=301)).isoformat()})
    rejected(scene, replace(scene.bundle, checkpoint=checkpoint), "TRUSTED_ARTIFACT_STALE")


def test_missing_authenticated_revocation_manifest_rejected(scene):
    from utils.authoritative_governance import build_authoritative_governance_view
    with pytest.raises(TrustedArtifactError):
        build_authoritative_governance_view(**{**scene.governance_inputs, "revocation_manifest": None})


def test_owner_signature_cannot_substitute_lifecycle_role(scene):
    verifier = ActivationSupersessionVerifier(scene.context.dataset_identity, "synthetic-lifecycle-authority",
        scene.env.owner_public, scene.checkpoint_public, scene.env.authorizer_public, scene.strict)
    with pytest.raises(TrustedArtifactError) as exc:
        verifier.verify(scene.bundle, read_context=scene.read_context())
    assert "LIFECYCLE_ROLE_SUBSTITUTION" in exc.value.rejection_codes


@pytest.mark.parametrize("sequence", [True, 0, -1, 1.5, "2"])
def test_sequence_type_and_bounds_rejected(scene, sequence):
    rejected(scene, scene.make_bundle(sequence=sequence), "LIFECYCLE_SEQUENCE_INVALID")


def test_transition_sequence_cannot_be_reused_for_new_event(scene):
    scene.verified()
    auth, evidence, _ = chain(scene)
    event = scene.transition(scene.auth, auth, "backdated-B-to-C", 1)
    rejected(scene, scene.make_bundle(authorizations=(*scene.bundle.authorizations, evidence),
        transitions=(scene.event, event), current=(auth.activation_id,), sequence=3), "LIFECYCLE_SEQUENCE_INVALID")


def test_transition_cannot_change_source_or_owner(scene):
    for field, value in (("scoped_source_identity_key", "other-source"), ("candidate_canonical_id", "other-owner"),
                          ("dataset_identity", "other-dataset")):
        old = sign_runtime_activation_authorization(scene.env.authorizer_private, {**scene.old.as_dict(), field: value})
        evidence = ShadowAuthorizationEvidence(old, scene.decision, scene.env.proposal)
        event = scene.transition(old, scene.auth, "wrong-scope", 2)
        rejected(scene, scene.make_bundle(authorizations=(evidence, scene.evidence), transitions=(event,)),
            "LIFECYCLE_SCOPE_MISMATCH")


def test_identical_effective_binding_duplication_is_rejected(scene):
    snapshot = replace(scene.snapshot, bindings=scene.snapshot.bindings * 2)
    snapshot = replace(snapshot, snapshot_id=snapshot.content_fingerprint(), snapshot_fingerprint=snapshot.content_fingerprint())
    scene.registry.register(snapshot)
    scene.registry.select(snapshot.snapshot_id)
    assert "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in scene.read().rejection_codes


def test_current_approval_fingerprint_cannot_be_replaced_by_caller(scene):
    auth = sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), "approval_fingerprint": "0" * 64})
    evidence = ShadowAuthorizationEvidence(auth, scene.decision, scene.env.proposal)
    event = scene.transition(scene.old, auth, "forged-approval", 2)
    rejected(scene, scene.make_bundle(authorizations=(scene.old_evidence, evidence), transitions=(event,)),
        "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH")


def test_valid_unknown_observation_returns_not_found_only_with_valid_lifecycle(scene):
    from utils.observation_authority import sign_observation_attestation
    observation = scene.env.other_observation
    attestation = sign_observation_attestation(scene.governance_private, observation,
        captured_at=NOW, dataset_identity=scene.context.dataset_identity, adapter="market_xml")
    kwargs = dict(observation=observation, snapshot=scene.snapshot, observation_attestation=attestation)
    result = scene.reader.read(**kwargs, read_context=scene.read_context())
    assert result.resolution_status == "NOT_FOUND"
    bad = scene.make_bundle(transitions=(), current=(scene.old.activation_id, scene.auth.activation_id))
    result = scene.reader.read(**kwargs, read_context=scene.read_context(lifecycle_evidence=bad))
    assert result.resolution_status == "REJECTED_SHADOW"


def test_reenable_preserves_current_recovery_binding(scene):
    recovery = scene.audited_recovery()
    assert scene.read().is_resolved
    scene.simulator().disable_shadow_runtime(at=NOW)
    scene.registry.select(scene.snapshot.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence,
            recovery_evidence=recovery)
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes
    assert not scene.reader.enabled


def test_unavailable_authenticated_successor_still_retires_predecessor(scene):
    predecessor = scene.evidence  # A remains valid for ten minutes.
    successor = scene.old_evidence  # B expires after five seconds.
    old = scene.make_bundle(authorizations=(predecessor,), transitions=(),
        current=(predecessor.authorization.activation_id,), sequence=1)
    assert scene.verified(old).sequence == 1
    event = scene.transition(predecessor.authorization, successor.authorization, "long-A-to-short-B", 2)
    scene.clock.advance(6)
    scene.token = scene.attestation(generation=11)
    newer = scene.make_bundle(authorizations=(predecessor, successor), transitions=(event,),
        current=(successor.authorization.activation_id,), sequence=2)
    rejected(scene, newer, "ACTIVATION_AUTHORIZATION_EXPIRED")
    rejected(scene, old, "LIFECYCLE_STATE_ROLLBACK")


def test_activation_trust_anchor_cannot_be_replaced_at_same_path(scene):
    # Replacing a path's bytes does not change the configured trusted identity.
    Path(scene.env.authorizer_public).write_bytes(Path(scene.checkpoint_public).read_bytes())
    changed = sign_runtime_activation_authorization(scene.checkpoint_private, scene.auth.as_dict())
    evidence = ShadowAuthorizationEvidence(changed, scene.decision, scene.env.proposal)
    rejected(scene, scene.make_bundle(authorizations=(evidence,), transitions=(), current=(changed.activation_id,)),
        "LIFECYCLE_ACTIVATION_SIGNER_CHANGED")


def test_mutable_history_input_is_rejected(scene):
    rejected(scene, replace(scene.bundle, authorizations=list(scene.bundle.authorizations)), "LIFECYCLE_MALFORMED")


def test_six_required_synthetic_signing_roles_are_distinct(scene):
    from utils.mapping_governance import load_trusted_public_key
    paths = (scene.env.owner_public, scene.env.authorizer_public, scene.lifecycle_public,
        scene.checkpoint_public, scene.governance_public, scene.public)
    assert len({load_trusted_public_key(path)["public_key_id"] for path in paths}) == 6
