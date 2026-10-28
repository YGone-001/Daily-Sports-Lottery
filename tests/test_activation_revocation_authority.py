"""VR-01..VR-50：独立撤销状态、身份绑定、连续性及真实 shadow 消费路径。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import ast
import itertools
from pathlib import Path

import pytest

from tools.shadow_revocation_authority_validation import SyntheticRevocationScenario, run_acceptance
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.activation_revocation_authority import (
    IndependentRevocationVerifier, RevocationEvidence, REVOCATION_PURPOSE, STATE_PURPOSE, CHECKPOINT_PURPOSE,
    MAX_INVENTORY, revocation_checkpoint_payload,
)
from utils.runtime_activation_authorization import sign_runtime_activation_authorization
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowAuthorizationEvidence, ShadowReenablementError
from utils.source_identity import fingerprint
from utils.trusted_artifacts import TrustedArtifactError, sign_trusted_artifact


@pytest.fixture
def scene(tmp_path):
    return SyntheticRevocationScenario(tmp_path / "revocation")


def rejected(scene, evidence, code=None, **context):
    with pytest.raises(TrustedArtifactError) as exc:
        scene.verified_revocations(evidence, **context)
    if code:
        assert code in exc.value.rejection_codes, exc.value.rejection_codes
    return exc.value.rejection_codes


def revoked(scene, sequence=1):
    event = scene.revocation_event(sequence=sequence)
    return scene.make_revocations((event,), sequence=sequence)


def event_with(scene, **changes):
    event = scene.revocation_event()
    return sign_trusted_artifact(scene.revocation_private, artifact_type=REVOCATION_PURPOSE,
        payload={**event.payload(), **changes})


def cp_with(scene, evidence, **changes):
    checkpoint = sign_trusted_artifact(scene.revocation_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload={**evidence.checkpoint.payload(), **changes})
    return replace(evidence, checkpoint=checkpoint)


def test_vr01_correct_current_state_and_empty_state_verify(scene):
    assert scene.verified_revocations().revoked_authorizations == ()
    state = scene.verified_revocations(revoked(scene))
    assert state.is_revoked(scene.auth) and state.state_sequence == 1
    with pytest.raises(FrozenInstanceError):
        state.state_sequence = 0


def test_vr02_unknown_revocation_signer(scene):
    event = sign_trusted_artifact(scene.lifecycle_private, artifact_type=REVOCATION_PURPOSE,
        payload=scene.revocation_event().payload())
    rejected(scene, scene.make_revocations((event,), sequence=1), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


@pytest.mark.parametrize("outer", [False, True])
def test_vr03_wrong_purpose(scene, outer):
    event = (sign_trusted_artifact(scene.revocation_private, artifact_type=STATE_PURPOSE,
        payload=scene.revocation_event().payload()) if outer else event_with(scene, purpose=STATE_PURPOSE))
    rejected(scene, scene.make_revocations((event,), sequence=1),
        "TRUSTED_ARTIFACT_TYPE_MISMATCH" if outer else "REVOCATION_CONTRACT_INVALID")


def test_vr04_wrong_dataset(scene):
    rejected(scene, scene.make_revocations(dataset_identity="other"), "REVOCATION_DATASET_MISMATCH")
    event = event_with(scene, dataset_identity="other")
    rejected(scene, scene.make_revocations((event,), sequence=1), "REVOCATION_DATASET_MISMATCH")


def test_vr05_tampered_event(scene):
    event = replace(scene.revocation_event(), signature="A" * 88)
    rejected(scene, scene.make_revocations((event,), sequence=1), "TRUSTED_ARTIFACT_SIGNATURE_INVALID")


@pytest.mark.parametrize("field,value", [
    ("authorization_fingerprint", "0" * 64), ("revocation_reference", "wrong-reference"),
    ("source_identity_key", "other-source"), ("canonical_id", "other-owner"),
], ids=["VR-06", "VR-07", "VR-08", "VR-09"])
def test_vr06_to_vr09_identity_binding(scene, field, value):
    event = event_with(scene, **{field: value})
    rejected(scene, scene.make_revocations((event,), sequence=1), "REVOCATION_EVENT_BINDING_MISMATCH")


def test_vr10_identical_event_is_idempotent(scene):
    event = scene.revocation_event()
    once = scene.make_revocations((event,), sequence=1)
    twice = scene.make_revocations((event, event), sequence=1)
    assert once.state == twice.state
    assert scene.verified_revocations(once) == scene.verified_revocations(twice)


def test_vr11_duplicate_event_id_conflicting_content(scene):
    event = scene.revocation_event()
    other = event_with(scene, reason="different reason")
    for events in itertools.permutations((event, other)):
        rejected(scene, scene.make_revocations(events, sequence=1), "REVOCATION_EVENT_ID_CONFLICT")


def test_vr12_missing_historical_event(scene):
    scene.verified_revocations(revoked(scene))
    rejected(scene, scene.make_revocations(sequence=2), "REVOCATION_HISTORY_REMOVED")


def test_vr13_truncated_inventory(scene):
    evidence = revoked(scene)
    rejected(scene, replace(evidence, events=()), "REVOCATION_STATE_INCOMPLETE")
    rejected(scene, scene.make_revocations(evidence.events, sequence=1, revocation_event_fingerprints=[]),
        "REVOCATION_STATE_INCOMPLETE")


def test_vr14_old_state_replay(scene):
    old = scene.revocations
    scene.verified_revocations(revoked(scene))
    rejected(scene, old, "REVOCATION_STATE_ROLLBACK")


def test_vr15_same_sequence_changed_state(scene):
    scene.verified_revocations()
    rejected(scene, scene.make_revocations(issued_at="2030-01-15T00:00:00Z"), "REVOCATION_STATE_ROLLBACK")


def test_vr16_wrong_checkpoint_signer(scene):
    cp = sign_trusted_artifact(scene.revocation_private, artifact_type=CHECKPOINT_PURPOSE,
        payload=scene.revocations.checkpoint.payload())
    rejected(scene, replace(scene.revocations, checkpoint=cp), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


@pytest.mark.parametrize("field,value", [("state_fingerprint", "0" * 64), ("state_sequence", 99),
    ("event_count", 99), ("session_challenge", "other-session")], ids=["VR-17", "VR-18", "VR-19", "VR-20"])
def test_vr17_to_vr20_checkpoint_binding(scene, field, value):
    rejected(scene, cp_with(scene, scene.revocations, **{field: value}), "REVOCATION_CHECKPOINT_MISMATCH")


def test_vr21_stale_checkpoint(scene):
    at = (scene.clock() - timedelta(seconds=61)).isoformat()
    rejected(scene, cp_with(scene, scene.revocations, issued_at=at), "TRUSTED_ARTIFACT_STALE")


def test_vr22_future_state(scene):
    rejected(scene, scene.make_revocations(issued_at="2030-01-15T00:00:01Z"), "TRUSTED_ARTIFACT_FROM_FUTURE")


def test_vr23_missing_trusted_time(scene):
    rejected(scene, scene.revocations, "TRUSTED_TIME_EVIDENCE_MISSING", trusted_time_evidence=None)


def test_vr24_empty_caller_registry_cannot_override(scene):
    scene.revocations = revoked(scene)
    result = scene.read(revocation_registry=())
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in result.rejection_codes


def test_vr25_old_governance_view_cannot_hide_new_revocation(scene):
    old = scene.view
    assert scene.read().is_resolved
    scene.revocations = revoked(scene)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read(authoritative_view=old).rejection_codes


def test_vr26_revoked_current_activation(scene):
    scene.revocations = revoked(scene)
    assert not scene.read().is_resolved
    assert scene.verify().is_consumable  # Owner/activation signature alone cannot override revocation.


def test_vr27_revoked_predecessor_allows_current_successor(scene):
    event = scene.revocation_event()
    scene.revocations = scene.make_revocations((event,), sequence=1)
    assert not scene.read().is_resolved
    successor = scene.successor()
    assert scene.read().is_resolved
    state = scene.verified_revocations()
    assert state.is_revoked(scene.A) and not state.is_revoked(successor)


def test_vr28_revoked_successor_rejected(scene):
    scene.successor()
    scene.revocations = revoked(scene, sequence=3)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read().rejection_codes


def prepare_chain(scene):
    event_A = scene.revocation_event(scene.A, sequence=1)
    scene.revocations = scene.make_revocations((event_A,), sequence=1)
    scene.read()
    B = scene.successor()
    assert scene.read().is_resolved
    event_B = scene.revocation_event(B, sequence=3)
    scene.revocations = scene.make_revocations((event_A, event_B), sequence=3)
    scene.read()
    C = scene.successor(sequence=3, revocation_sequence=4)
    return B, C


def test_vr29_chain_revoked_A_B_valid_C(scene):
    B, C = prepare_chain(scene)
    result = scene.read()
    assert result.is_resolved and result.authorization_ids == (C.activation_id,)
    state = scene.verified_revocations()
    assert state.is_revoked(scene.A) and state.is_revoked(B) and not state.is_revoked(C)


def test_vr30_chain_revoked_C(scene):
    _, C = prepare_chain(scene)
    event = scene.revocation_event(C, sequence=5)
    scene.revocations = scene.make_revocations((*scene.revocations.events, event), sequence=5)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read().rejection_codes


def test_vr31_old_state_cannot_restore_revocation(scene):
    old = scene.revocations
    scene.revocations = revoked(scene)
    assert not scene.read().is_resolved
    assert "REVOCATION_STATE_ROLLBACK" in scene.read(revocation_evidence=old).rejection_codes


def test_vr32_strict_reader_missing_proof_rejects(scene):
    result = scene.read(revocation_evidence=None)
    assert result.resolution_status == "REJECTED_SHADOW" and "REVOCATION_EVIDENCE_MISSING" in result.rejection_codes
    assert not scene.read(revocation_evidence=scene.verified_revocations()).is_resolved


def test_vr33_authenticated_complete_empty_state_resolves(scene):
    assert scene.read().is_resolved
    assert scene.reader.status()["require_independent_revocation"]


def test_vr34_next_read_observes_new_revocation(scene):
    assert scene.read().is_resolved
    scene.revocations = revoked(scene)
    assert not scene.read().is_resolved


def test_vr35_cached_result_invalidated_by_authority_observation(scene):
    result = scene.read()
    assert scene.reader.is_generation_current(result)
    scene.verified_revocations(revoked(scene))
    assert not scene.reader.is_generation_current(result)
    assert not scene.read().is_resolved  # The old empty state is now a replay.


def test_vr36_reenable_rejects_revoked_with_valid_recovery(scene):
    recovery = scene.audited_recovery()
    scene.revocations = revoked(scene)
    scene.reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=recovery)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in exc.value.rejection_codes
    assert not scene.reader.enabled


@pytest.mark.parametrize("disabled", [False, True])
def test_vr37_rollback_and_disabled_reconciliation_reject_revoked(scene, disabled):
    assert scene.read().is_resolved
    scene.revocations = revoked(scene)
    if disabled:
        scene.reader.disable(at=NOW)
    report = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint(), reconcile_disabled_reader=disabled)
    assert not report.succeeded and "ACTIVATION_AUTHORIZATION_REVOKED" in report.rejection_codes
    if disabled:
        assert not scene.reader.enabled


def test_vr38_disable_without_authority(scene):
    scene.revocations = None
    scene.simulator().disable_shadow_runtime(at=NOW)
    assert not scene.reader.enabled
    assert not scene.read(revocation_evidence=None, trusted_time_evidence=None).is_resolved


def test_vr39_recovery_still_mandatory(scene):
    scene.reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence)
    assert "RE_ENABLE_RECOVERY_EVIDENCE_MISSING" in exc.value.rejection_codes


def test_vr40_owner_approval_still_mandatory(scene):
    assert not scene.read(authoritative_view=None).is_resolved
    scene.supersede()
    scene.bundle = scene.make_bundle(authorizations=(scene.evidence,), transitions=(), current=(scene.auth.activation_id,), sequence=2)
    scene.revocations = scene.make_revocations(sequence=1)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.read().rejection_codes


def test_vr41_time_and_lifetime_still_mandatory(scene):
    assert not scene.read(trusted_time_evidence=None).is_resolved
    long = scene.bound_authorization(expires_at="2030-01-16T00:00:01Z")
    evidence = ShadowAuthorizationEvidence(long, scene.decision, scene.env.proposal)
    scene.bundle = scene.make_bundle(authorizations=(evidence,), transitions=(), current=(long.activation_id,), sequence=2)
    scene.revocations = scene.make_revocations(sequence=1)
    snapshot = scene.snapshot_for((long,))
    scene.registry.register(snapshot)
    scene.registry.select(snapshot.snapshot_id)
    assert "ACTIVATION_LIFETIME_EXCEEDED" in scene.read().rejection_codes


def test_vr42_supersession_and_lifecycle_scope_remain_enforced(scene):
    old_revocations = scene.revocations
    scene.successor()
    assert "REVOCATION_LIFECYCLE_MISMATCH" in scene.read(revocation_evidence=old_revocations).rejection_codes
    assert scene.read().is_resolved
    scene.registry.select(scene.A_snapshot.snapshot_id)
    assert "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in scene.read().rejection_codes


def test_vr43_restart_requires_fresh_independent_checkpoint(scene):
    restarted = scene.new_revocation_verifier()
    with pytest.raises(TrustedArtifactError) as exc:
        restarted.verify(scene.revocations, read_context=scene.read_context())
    assert "REVOCATION_CHECKPOINT_MISMATCH" in exc.value.rejection_codes
    checkpoint = sign_trusted_artifact(scene.revocation_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload=revocation_checkpoint_payload(scene.revocations.state, session_challenge=restarted.session_challenge, issued_at=NOW))
    assert restarted.verify(replace(scene.revocations, checkpoint=checkpoint), read_context=scene.read_context()).state_sequence == 0


def test_vr44_loss_of_continuity_fails_closed(scene):
    result = scene.read()
    object.__setattr__(scene.revocation, "_state", None)
    rejected(scene, scene.revocations, "REVOCATION_CONTINUITY_LOST")
    assert not scene.reader.is_generation_current(result)


@pytest.mark.parametrize("role", ["owner", "activation", "lifecycle", "checkpoint"])
def test_vr45_signing_role_substitution_rejected(scene, role):
    paths = dict(owner=scene.env.owner_public, activation=scene.env.authorizer_public,
        lifecycle=scene.lifecycle_public, checkpoint=scene.revocation_checkpoint_public)
    with pytest.raises(ValueError):
        scene.new_revocation_verifier(revocation_public_key_path=paths[role])


def test_vr46_no_automatic_rescission(scene):
    evidence = revoked(scene)
    scene.verified_revocations(evidence)
    rejected(scene, scene.make_revocations(sequence=2), "REVOCATION_HISTORY_REMOVED")
    event = event_with(scene, transition="REINSTATE")
    rejected(scene, scene.make_revocations((event,), sequence=2), "REVOCATION_EVENT_BINDING_MISMATCH")


def test_vr47_demo_and_seven_store_integrity(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path / "demo")
    assert result["success"] and all(result["checks"].values())
    assert before == result["seven_store_sha256"] == protected_store_hashes()


def test_vr48_no_production_activation_import_or_api():
    root = Path(__file__).resolve().parents[1]
    for filename in ("app.py", "utils/scraper.py", "utils/ingestion_identity.py"):
        tree = ast.parse((root / filename).read_text(encoding="utf-8-sig"))
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any("activation_revocation_authority" in m or "shadow_runtime_reader" in m for m in modules)


def test_vr49_existing_activation_and_approval_verifiers_reused(scene):
    assert scene.verify().is_consumable
    bad = sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), "approval_fingerprint": "wrong"})
    assert not scene.verify(bad).is_consumable
    # The full pre-existing suites are run independently, not inferred from this test.


def test_vr50_ci_has_both_platforms_and_full_suite():
    ci = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert ci.count("os: [ubuntu-latest, windows-latest]") == 2
    assert 'python-version: ["3.12"]' in ci and "python -m pytest -q -rs" in ci
    # Configuration only; actual four-job success must be verified after push.


@pytest.mark.parametrize("field,value", [("registry", None), ("require_authoritative_governance", False),
    ("require_authenticated_approval", False), ("require_trusted_time", False), ("revocation_verifier", None)])
def test_invalid_strict_configuration_rejected_during_construction(scene, field, value):
    kwargs = dict(registry=scene.registry, require_authoritative_governance=True,
        require_authenticated_approval=True, require_trusted_time=True, strict_activation_verifier=scene.strict,
        require_independent_revocation=True, revocation_verifier=scene.revocation)
    kwargs[field] = value
    with pytest.raises(ValueError):
        ShadowRuntimeReader(**kwargs)


def test_governance_revocation_cannot_be_erased_or_overridden(scene):
    scene.view = scene.governance_view(((scene.auth.activation_id, scene.auth.revocation_reference),))
    scene.bundle = scene.make_bundle(authorizations=(scene.evidence,), transitions=(), current=(scene.auth.activation_id,), sequence=2)
    scene.revocations = scene.make_revocations(sequence=1)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read(revocation_registry=()).rejection_codes
    scene.view = scene.governance_view(())
    rejected(scene, scene.revocations, "REVOCATION_GOVERNANCE_REMOVED")


def test_historical_governance_revocation_does_not_block_successor(scene):
    scene.successor()
    scene.view = scene.governance_view(((scene.A.activation_id, scene.A.revocation_reference),))
    scene.bundle = scene.make_bundle(authorizations=scene.bundle.authorizations, transitions=scene.bundle.transitions,
        current=(scene.auth.activation_id,), sequence=3)
    scene.revocations = scene.make_revocations(sequence=3)
    assert scene.read().is_resolved
    assert scene.verified_revocations().is_revoked(scene.A)


def test_unknown_activation_rejected_explicitly(scene):
    event = event_with(scene, activation_id="unknown")
    rejected(scene, scene.make_revocations((event,), sequence=1), "REVOCATION_UNKNOWN_ACTIVATION")


def test_revoked_activation_id_cannot_sign_different_artifact(scene):
    scene.verified_revocations(revoked(scene))
    changed = sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), "reason": "different authorization with same activation ID"})
    evidence = ShadowAuthorizationEvidence(changed, scene.decision, scene.env.proposal)
    scene.bundle = scene.make_bundle(authorizations=(scene.evidence, evidence), transitions=(),
        current=(scene.auth.activation_id,), sequence=2)
    rejected(scene, scene.make_revocations(sequence=2), "REVOCATION_ACTIVATION_ID_CONFLICT")


def test_input_order_deterministic_and_duplicate_auth_revocation_rejected(scene):
    scene.successor()
    A, B = scene.revocation_event(scene.A, sequence=1), scene.revocation_event(sequence=2)
    results = [scene.verified_revocations(scene.make_revocations(events, sequence=2))
        for events in itertools.permutations((A, B))]
    assert results[0] == results[1]
    extra = scene.revocation_event(sequence=3, reason="second revocation for same authorization")
    rejected(scene, scene.make_revocations((A, B, extra), sequence=3), "REVOCATION_AUTHORIZATION_CONFLICT")


@pytest.mark.parametrize("sequence", [True, -1, 1.5, "1", 2**63])
def test_sequence_type_and_bound(scene, sequence):
    rejected(scene, scene.make_revocations(sequence=sequence), "REVOCATION_SEQUENCE_INVALID")


def test_evidence_inventory_is_bounded_and_immutable(scene):
    evidence = revoked(scene)
    rejected(scene, replace(evidence, events=evidence.events * (MAX_INVENTORY + 1)), "REVOCATION_INVENTORY_INVALID")
    rejected(scene, replace(evidence, events=list(evidence.events)), "REVOCATION_INVENTORY_INVALID")
    with pytest.raises(FrozenInstanceError):
        evidence.events = ()


def test_stale_state_and_earlier_caller_time_cannot_restore(scene):
    scene.revocations = revoked(scene)
    assert not scene.read().is_resolved
    scene.clock.advance(61)
    scene.token = scene.attestation(generation=11)
    rejected(scene, scene.revocations, "TRUSTED_ARTIFACT_STALE", current_time=NOW)


def test_historical_events_remain_immutable_after_freshness_interval(scene):
    event = scene.revocation_event()
    evidence = scene.make_revocations((event,), sequence=1)
    scene.verified_revocations(evidence)
    scene.clock.advance(61)
    scene.token = scene.attestation(generation=11)
    refreshed = scene.make_revocations((event,), sequence=2)
    assert scene.verified_revocations(refreshed).is_revoked(scene.auth)
    assert refreshed.events[0] == evidence.events[0]


def test_fixed_key_identity_survives_path_replacement(scene):
    Path(scene.revocation_public).write_bytes(Path(scene.lifecycle_public).read_bytes())
    state = sign_trusted_artifact(scene.lifecycle_private, artifact_type=STATE_PURPOSE, payload=scene.revocations.state.payload())
    rejected(scene, replace(scene.revocations, state=state), "REVOCATION_SIGNER_CHANGED")


def test_thread_safe_idempotent_verification_and_old_replay(scene):
    evidence = revoked(scene)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: scene.verified_revocations(evidence), range(12)))
    assert all(r == results[0] for r in results)
    assert scene.revocation.generation == 1
    rejected(scene, scene.revocations, "REVOCATION_STATE_ROLLBACK")


@pytest.mark.parametrize("field,value", [("authority_identity", "another-authority"),
    ("activation_authorizer", "another-authorizer"), ("revocation_event_id", "0" * 64)])
def test_event_authority_authorizer_and_deterministic_identity(scene, field, value):
    event = event_with(scene, **{field: value})
    rejected(scene, scene.make_revocations((event,), sequence=1),
        "REVOCATION_AUTHORITY_MISMATCH" if field == "authority_identity" else "REVOCATION_EVENT_BINDING_MISMATCH")


@pytest.mark.parametrize("part", ["state", "checkpoint"])
def test_forged_state_and_checkpoint(scene, part):
    artifact = replace(getattr(scene.revocations, part), signature="A" * 88)
    rejected(scene, replace(scene.revocations, **{part: artifact}), "TRUSTED_ARTIFACT_SIGNATURE_INVALID")


def test_missing_checkpoint_is_not_empty_authority(scene):
    rejected(scene, replace(scene.revocations, checkpoint=None), "REVOCATION_ARTIFACT_MALFORMED")


@pytest.mark.parametrize("age", [0, -1, 301, float("inf"), float("nan")])
def test_bounded_freshness_configuration(scene, age):
    with pytest.raises(ValueError):
        scene.new_revocation_verifier(max_age_seconds=age)


def test_continuity_generation_loss_rejects(scene):
    scene.verified_revocations()
    object.__setattr__(scene.revocation, "_generation", 0)
    rejected(scene, scene.revocations, "REVOCATION_CONTINUITY_LOST")


def test_standalone_independent_mode_without_supersession(tmp_path):
    scene = SyntheticRevocationScenario(tmp_path / "without-lifecycle", supersession=False)
    changes = dict(lifecycle_state_fingerprint="", lifecycle_sequence=0)
    scene.revocations = scene.make_revocations(**changes)
    assert scene.read(lifecycle_evidence=None).is_resolved
    scene.revocations = scene.make_revocations((scene.revocation_event(),), sequence=1, **changes)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read(lifecycle_evidence=None).rejection_codes


def test_future_event_and_invalid_checkpoint_expiry(scene):
    event = event_with(scene, issued_at="2030-01-15T00:00:01Z")
    rejected(scene, scene.make_revocations((event,), sequence=1), "TRUSTED_ARTIFACT_FROM_FUTURE")
    rejected(scene, cp_with(scene, scene.revocations, expires_at=NOW), "REVOCATION_CHECKPOINT_EXPIRED")


def test_current_complete_state_cannot_delete_revoked_identity(scene):
    evidence = revoked(scene)
    rejected(scene, scene.make_revocations(evidence.events, sequence=1, currently_revoked_authorizations=[]),
        "REVOCATION_STATE_INCOMPLETE")


def test_unknown_governance_revocation_is_not_ignored(scene):
    scene.view = scene.governance_view((("unknown-activation", "unknown-reference"),))
    rejected(scene, scene.revocations, "REVOCATION_GOVERNANCE_UNKNOWN_ACTIVATION")


def test_audited_recovery_chain_still_bound(scene):
    recovery = scene.audited_recovery()
    scene.simulator().disable_shadow_runtime(at=NOW)
    scene.registry.select(scene.snapshot.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=recovery)
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes
    assert not scene.reader.enabled


def test_serialized_checkpoint_inventory_and_cached_generation(scene):
    result = scene.read()
    assert result.cache_key == scene.reader.cache_key(snapshot=scene.snapshot, read_context=scene.read_context(),
        source_identity_key=result.source_identity_key)
    rejected(scene, cp_with(scene, scene.revocations, event_inventory_fingerprint="wrong"), "REVOCATION_CHECKPOINT_MISMATCH")


def test_revocation_and_lifecycle_trust_roots_must_match(scene):
    incompatible = scene.new_revocation_verifier(lifecycle_public_key_path=scene.checkpoint_public)
    with pytest.raises(ValueError):
        ShadowRuntimeReader(registry=scene.registry, require_authoritative_governance=True,
            require_authenticated_approval=True, require_trusted_time=True, strict_activation_verifier=scene.strict,
            require_verified_supersession=True, supersession_verifier=scene.lifecycle,
            require_independent_revocation=True, revocation_verifier=incompatible)


def test_fresh_checkpoint_cannot_refresh_stale_state(scene):
    scene.clock.advance(61)
    scene.token = scene.attestation(generation=11)
    cp = sign_trusted_artifact(scene.revocation_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload=revocation_checkpoint_payload(scene.revocations.state, session_challenge=scene.revocation.session_challenge,
            issued_at=scene.clock().isoformat()))
    rejected(scene, replace(scene.revocations, checkpoint=cp), "TRUSTED_ARTIFACT_STALE")


def test_checkpoint_cannot_create_or_remove_events(scene):
    event = scene.revocation_event()
    evidence = scene.make_revocations((event,), sequence=1)
    rejected(scene, cp_with(scene, evidence, event_count=0), "REVOCATION_CHECKPOINT_MISMATCH")
    rejected(scene, replace(evidence, events=()), "REVOCATION_STATE_INCOMPLETE")


def test_reenable_argument_must_match_full_authenticated_inventory(tmp_path):
    scene = SyntheticRevocationScenario(tmp_path / "reenable-identity", supersession=False)
    recovery = scene.audited_recovery()
    different = sign_runtime_activation_authorization(scene.env.authorizer_private,
        {**scene.auth.as_dict(), "reason": "different signed bytes, same activation ID"})
    evidence = ShadowAuthorizationEvidence(different, scene.decision, scene.env.proposal)
    scene.reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=evidence, recovery_evidence=recovery)
    assert "REVOCATION_ACTIVATION_ID_CONFLICT" in exc.value.rejection_codes
    assert not scene.reader.enabled
