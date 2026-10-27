"""Synthetic time security reproduction and strict-time acceptance (no live clocks)."""
import ast
import base64
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from tools.shadow_runtime_activation import build_synthetic_shadow_fixture
from tools.shadow_trusted_time_validation import (
    SyntheticTimeScenario, protected_store_hashes, run_acceptance,
)
from utils.runtime_activation_authorization import (
    build_activation_authorization_body, sign_runtime_activation_authorization,
    verify_runtime_activation_authorization, StrictActivationVerifier,
)
from utils.shadow_activation_simulation import ShadowActivationSimulator, ShadowSnapshotRegistry
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowReenablementError
from utils.trusted_time_authority import (
    TrustedTimeVerifier, TrustedTimeError, TIME_ATTESTATION_PURPOSE,
)
from utils.trusted_artifacts import sign_trusted_artifact, TrustedArtifactError

NOW = "2030-01-15T00:00:00+00:00"
LATER = "2030-01-15T00:10:00+00:00"


@pytest.fixture
def env(tmp_path):
    return build_synthetic_shadow_fixture(str(tmp_path))


def authorization(env, start=NOW, end=LATER):
    return sign_runtime_activation_authorization(
        env.authorizer_private,
        build_activation_authorization_body(
            decision=env.decision, proposal=env.proposal,
            dataset_identity=env.authorization.dataset_identity,
            authorized_scope=env.authorization.authorized_scope,
            authorizer="synthetic-authorizer", operator="shadow-operator",
            authorized_at=start, expires_at=end,
            revocation_reference=env.authorization.revocation_reference,
        ),
    )


def test_te01_legacy_depends_on_caller_time(env):
    auth = authorization(env)
    assert verify_runtime_activation_authorization(auth, env.verification_context).is_consumable
    expired = verify_runtime_activation_authorization(
        auth, replace(env.verification_context, current_time=LATER))
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in expired.rejection_codes


def test_te02_legacy_accepts_excessive_lifetime(env):
    assert verify_runtime_activation_authorization(
        env.authorization, env.verification_context).is_consumable


def test_te03_legacy_earlier_time_can_revive_expired_authorization(env):
    auth = authorization(env)
    assert not verify_runtime_activation_authorization(
        auth, replace(env.verification_context, current_time=LATER)).is_consumable
    assert verify_runtime_activation_authorization(auth, env.verification_context).is_consumable


def test_te04_legacy_has_no_independent_freshness_input(env):
    # Actual baseline: only a supplied string, no attestation required by this legacy API.
    assert verify_runtime_activation_authorization(
        authorization(env), env.verification_context).is_consumable


def test_te05_legacy_valid_signature_is_not_authenticated_current_time(env):
    result = verify_runtime_activation_authorization(authorization(env), env.verification_context)
    assert result.is_consumable and result.verified_at == NOW


def test_te_lifecycle_real_audited_disable_blocks_default_rollback(env):
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry, audit_store=env.audit_store,
                                 require_recovery_evidence_for_reenable=True)
    simulator = ShadowActivationSimulator(registry=registry, reader=reader,
                                          audit_store=env.audit_store)
    assert reader.read(observation=env.observation, snapshot=env.snapshot,
                       read_context=env.read_context()).is_resolved
    simulator.disable_shadow_runtime(at=NOW)
    assert "SHADOW_RUNTIME_DISABLED" in env.audit_store.event_types()
    result = simulator.rollback_shadow_snapshot(target_snapshot_id=env.snapshot.snapshot_id,
                                                read_context=env.read_context(), at=NOW)
    assert "ROLLBACK_READER_DISABLED" in result.rejection_codes
    assert not reader.enabled


# TT-01 .. TT-32: strict authenticated time and integration acceptance.


@pytest.fixture
def scene(tmp_path):
    return SyntheticTimeScenario(tmp_path / "strict")


def signed_payload(scene, **changes):
    return sign_trusted_artifact(scene.private, artifact_type=TIME_ATTESTATION_PURPOSE,
                                payload={**scene.token.payload(), **changes})


def rejected_time(scene, token, code):
    with pytest.raises(TrustedArtifactError) as exc:
        scene.time.verify(token, dataset_identity=scene.context.dataset_identity)
    assert code in exc.value.rejection_codes


def strict(scene, auth=None, token=None, **context):
    return scene.strict.verify(auth or scene.auth, replace(scene.context, **context),
                              time_evidence=scene.token if token is None else token)


def test_tt01_valid_signed_attestation(scene):
    observation = scene.time.verify(scene.token, dataset_identity=scene.context.dataset_identity)
    assert observation.effective_time == NOW
    assert strict(scene).signature_valid and strict(scene).trusted_time_verified
    assert strict(scene).as_dict()["is_consumable"]
    assert scene.token == replace(scene.token)


def test_attestation_serialized_roundtrip(scene):
    assert scene.time.verify(scene.token.as_dict(), dataset_identity=scene.context.dataset_identity).effective_time == NOW


def test_tt02_unknown_signer(scene):
    token = sign_trusted_artifact(scene.governance_private, artifact_type=TIME_ATTESTATION_PURPOSE,
                                 payload=scene.token.payload())
    rejected_time(scene, token, "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


def test_tt03_wrong_purpose(scene):
    token = sign_trusted_artifact(scene.private, artifact_type="SHADOW_RECOVERY_EVIDENCE",
                                 payload=scene.token.payload())
    rejected_time(scene, token, "TRUSTED_ARTIFACT_TYPE_MISMATCH")
    rejected_time(scene, signed_payload(scene, purpose="SHADOW_RECOVERY_EVIDENCE"),
                  "TRUSTED_TIME_PURPOSE_MISMATCH")


def test_tt04_wrong_dataset_and_authority(scene):
    rejected_time(scene, signed_payload(scene, dataset_identity="other"), "TRUSTED_TIME_DATASET_MISMATCH")
    rejected_time(scene, signed_payload(scene, authority_identity="other"), "TRUSTED_TIME_AUTHORITY_MISMATCH")


def test_tt05_tampered_timestamp(scene):
    changed = scene.token.payload()
    changed["expires_at"] = "2030-01-15T00:00:59Z"
    rejected_time(scene, replace(scene.token, payload_json=json.dumps(changed)),
                  "TRUSTED_ARTIFACT_SIGNATURE_INVALID")


@pytest.mark.parametrize("value", ["invalid", "2030-01-15T00:00:00", "2030-01-15T08:00:00+08:00",
                                  "2030-02-31T00:00:00Z", "2030-01-15", None, 10])
def test_tt06_malformed_utc_and_timezone(scene, value):
    rejected_time(scene, signed_payload(scene, attested_at=value), "TRUSTED_TIME_TIMESTAMP_INVALID")


def test_tt07_future_evidence(scene):
    rejected_time(scene, scene.attestation(at="2030-01-15T00:00:01Z"), "TRUSTED_ARTIFACT_FROM_FUTURE")


def test_tt08_stale_evidence(scene):
    scene.clock.advance(61)
    rejected_time(scene, scene.token, "TRUSTED_ARTIFACT_STALE")
    assert not scene.read().is_resolved


def test_tt09_expired_evidence_at_exact_boundary(scene):
    token = signed_payload(scene, expires_at="2030-01-15T00:00:30Z")
    scene.clock.advance(30)
    rejected_time(scene, token, "TRUSTED_TIME_EXPIRED")


def test_tt10_replayed_generation(scene):
    rejected_time(scene, scene.attestation(generation=9), "TRUSTED_TIME_GENERATION_ROLLBACK")


def test_tt11_earlier_signed_time_rejected(scene):
    scene.clock.advance(10)
    scene.time.verify(scene.attestation(generation=11), dataset_identity=scene.context.dataset_identity)
    rejected_time(scene, scene.attestation(generation=12, at="2030-01-15T00:00:05Z"),
                  "TRUSTED_TIME_CLOCK_ROLLBACK")


def test_tt12_contradictory_same_generation(scene):
    rejected_time(scene, signed_payload(scene, evidence_id="conflict"), "TRUSTED_TIME_SEQUENCE_CONFLICT")


def test_tt13_missing_time_and_independent_reference(scene):
    assert "TRUSTED_TIME_EVIDENCE_MISSING" in scene.strict.verify(scene.auth, scene.context).rejection_codes
    verifier = TrustedTimeVerifier(scene.context.dataset_identity, "synthetic-time-authority", scene.public, None)
    with pytest.raises(TrustedTimeError) as exc:
        verifier.verify(scene.token, dataset_identity=scene.context.dataset_identity)
    assert "TRUSTED_TIME_REFERENCE_MISSING" in exc.value.rejection_codes
    reader = ShadowRuntimeReader(require_trusted_time=True)
    assert "TRUSTED_TIME_VERIFIER_MISSING" in reader.read(observation=scene.env.observation,
        snapshot=scene.snapshot, read_context=scene.read_context()).rejection_codes


def test_tt14_caller_time_cannot_override_authenticated_time(scene):
    for value in ("invalid", "1900-01-01T00:00:00Z", "2100-01-01T00:00:00Z"):
        result = strict(scene, current_time=value)
        assert result.is_consumable and result.verified_at == NOW
        assert scene.read(current_time=value).checked_at == NOW


def test_tt15_maximum_fixed_for_established_verifier(scene):
    with pytest.raises(FrozenInstanceError):
        scene.strict.max_lifetime_seconds = 100000
    with pytest.raises(ValueError):
        StrictActivationVerifier(scene.time, 86401)
    with pytest.raises(TypeError):
        scene.strict.verify(scene.auth, scene.context, time_evidence=scene.token, max_lifetime_seconds=100000)


def test_tt16_exactly_maximum_lifetime(scene):
    assert strict(scene, scene.authorization(end="2030-01-16T00:00:00Z")).is_consumable


def test_tt17_greater_than_maximum_lifetime(scene):
    result = strict(scene, scene.authorization(end="2030-01-16T00:00:00.000001Z"))
    assert "ACTIVATION_LIFETIME_EXCEEDED" in result.rejection_codes
    assert result.signature_valid and not result.is_consumable


def resign_authorization(scene, auth):
    # Simulate a hostile but cryptographically signed artifact bypassing issuance validation.
    key = load_pem_private_key(Path(scene.env.authorizer_private).read_bytes(), password=None)
    return replace(auth, signature=base64.b64encode(key.sign(auth.signing_payload())).decode("ascii"))


@pytest.mark.parametrize("end", [NOW, "2030-01-14T23:59:59Z"])
def test_tt18_nonpositive_lifetime(scene, end):
    auth = resign_authorization(scene, replace(scene.auth, expires_at=end))
    result = strict(scene, auth)
    assert "ACTIVATION_LIFETIME_INVALID" in result.rejection_codes
    assert result.signature_valid and not result.is_consumable


def test_tt19_before_authorization_start(scene):
    result = strict(scene, scene.authorization(start="2030-01-15T00:00:01Z"))
    assert "NOT_YET_VALID" in result.rejection_codes


def test_tt20_at_authorization_start(scene):
    assert strict(scene).is_consumable


@pytest.mark.parametrize("elapsed", [600, 601])
def test_tt21_tt22_at_and_after_expiry(scene, elapsed):
    scene.clock.advance(elapsed)
    token = scene.attestation(generation=11)
    result = strict(scene, token=token, current_time=NOW)
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in result.rejection_codes
    assert result.signature_valid and result.trusted_time_verified and not result.is_consumable
    # A previously signed token cannot revive the authorization.
    assert not strict(scene, token=scene.token).is_consumable


def test_tt23_revocation_remains_authoritative(scene):
    view = scene.governance_view([(scene.auth.activation_id, scene.auth.revocation_reference)])
    result = scene.read(authoritative_view=view, revocation_registry=())
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in result.rejection_codes
    assert not result.is_resolved


def test_tt24_valid_strict_shadow_read(scene):
    result = scene.read()
    assert result.is_resolved and result.checked_at == NOW
    assert result.snapshot_authority == "REGISTRY_SELECTED"


def test_tt25_stale_cached_read_cannot_be_consumed(scene):
    assert scene.read().is_resolved
    scene.clock.advance(61)
    result = scene.read(current_time=NOW)
    assert not result.is_resolved and "TRUSTED_ARTIFACT_STALE" in result.rejection_codes
    assert result.checked_at == ""


def test_tt26_tt29_real_audited_recovery_lifecycle(scene):
    recovery = scene.audited_recovery()
    events = scene.env.audit_store.event_types()
    assert events == ("SHADOW_RUNTIME_DISABLED", "SHADOW_ROLLBACK_REQUESTED", "SHADOW_ROLLBACK_COMPLETED")
    assert recovery.payload()["selected_snapshot_id"] == scene.registry.active_snapshot_id


def test_tt27_reenable_with_rollback_generation_stays_disabled(scene):
    recovery = scene.audited_recovery()
    scene.reader.disable(at=NOW)
    generation = scene.reader.generation
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(
            trusted_time_evidence=scene.attestation(generation=9)), evidence=scene.evidence,
            recovery_evidence=recovery)
    assert "TRUSTED_TIME_GENERATION_ROLLBACK" in exc.value.rejection_codes
    assert not scene.reader.enabled and scene.reader.generation == generation


def test_tt28_restart_requires_fresh_session_bound_evidence(scene):
    restarted = TrustedTimeVerifier(scene.context.dataset_identity, "synthetic-time-authority",
                                     scene.public, scene.clock)
    with pytest.raises(TrustedTimeError) as exc:
        restarted.verify(scene.token, dataset_identity=scene.context.dataset_identity)
    assert "TRUSTED_TIME_SESSION_MISMATCH" in exc.value.rejection_codes
    fresh = scene.attestation(session_challenge=restarted.session_challenge)
    assert restarted.verify(fresh, dataset_identity=scene.context.dataset_identity).effective_time == NOW
    # Losing established state must never silently bootstrap an active session.
    object.__setattr__(scene.time, "_state", None)
    rejected_time(scene, scene.token, "TRUSTED_TIME_CONTINUITY_LOST")


def test_tt30_production_runtime_remains_isolated():
    root = Path(__file__).resolve().parents[1]
    protected = ("scraper", "fetcher_500", "ingestion_identity", "daily_loader")
    for name in protected:
        tree = ast.parse((root / "utils" / f"{name}.py").read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "trusted_time_authority" not in (node.module or "")
                assert "shadow_trusted_time_validation" not in (node.module or "")
    from utils.runtime_mapping_policy import RUNTIME_MAPPING_ACTIVATION
    from utils.shadow_runtime_reader import PRODUCTION_RUNTIME_READER
    assert RUNTIME_MAPPING_ACTIVATION == PRODUCTION_RUNTIME_READER == "NOT_ENABLED"


def test_tt31_seven_stores_byte_identical_and_demo_functional(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path)
    assert result["success"] and all(result["checks"].values())
    assert protected_store_hashes() == before


def test_tt32_cross_platform_ci_matrix_is_preserved():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert workflow.count("os: [ubuntu-latest, windows-latest]") == 2
    assert 'python-version: ["3.12"]' in workflow


@pytest.mark.parametrize("updates,code", [
    ({"time_schema_version": "unsupported"}, "TRUSTED_TIME_SCHEMA_UNSUPPORTED"),
    ({"expires_at": NOW}, "TRUSTED_TIME_FRESHNESS_INTERVAL_INVALID"),
    ({"expires_at": "2030-01-15T00:01:01Z"}, "TRUSTED_TIME_FRESHNESS_INTERVAL_INVALID"),
    ({"generation": True}, "TRUSTED_TIME_EVIDENCE_INVALID"),
    ({"evidence_id": ""}, "TRUSTED_TIME_EVIDENCE_INVALID"),
    ({"generation": 11}, "TRUSTED_TIME_REPLAY"),
])
def test_additional_malformed_policy_and_replay(scene, updates, code):
    rejected_time(scene, signed_payload(scene, **updates), code)


def test_additional_outer_schema_and_missing_anchor(scene):
    rejected_time(scene, replace(scene.token, schema_version="unsupported"), "TRUSTED_ARTIFACT_SCHEMA_UNSUPPORTED")
    verifier = TrustedTimeVerifier(scene.context.dataset_identity, "synthetic-time-authority", None, scene.clock)
    with pytest.raises(TrustedArtifactError) as exc:
        verifier.verify(scene.token, dataset_identity=scene.context.dataset_identity)
    assert "TRUSTED_ARTIFACT_TRUST_ANCHOR_MISSING" in exc.value.rejection_codes


def test_additional_independent_reference_rollback_poisoned_session(scene):
    scene.clock.advance(5)
    assert strict(scene).is_consumable
    scene.clock.advance(-1)
    rejected_time(scene, scene.token, "TRUSTED_TIME_REFERENCE_ROLLBACK")
    scene.clock.advance(1)
    rejected_time(scene, scene.token, "TRUSTED_TIME_CONTINUITY_LOST")


def test_additional_same_token_cannot_freeze_expiry(scene):
    auth = scene.authorization(end="2030-01-15T00:00:30Z")
    assert strict(scene, auth).is_consumable
    scene.clock.advance(30)
    result = strict(scene, auth)
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in result.rejection_codes


def test_additional_reconciliation_requires_checkpoint_and_leaves_disabled(scene):
    simulator = ShadowActivationSimulator(registry=scene.registry, reader=scene.reader,
                                          audit_store=scene.env.audit_store)
    simulator.disable_shadow_runtime(at=NOW)
    result = simulator.rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, reconcile_disabled_reader=True)
    assert "ROLLBACK_AUDIT_CHECKPOINT_MISSING" in result.rejection_codes
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None


@pytest.mark.parametrize("failure", ["missing", "expired", "excessive", "old_recovery", "old_snapshot"])
def test_additional_reenable_rejects_missing_or_stale_mandatory_evidence(scene, failure):
    recovery = scene.audited_recovery()
    scene.reader.disable(at=NOW)
    context = scene.read_context()
    evidence = scene.evidence
    expected = ""
    if failure == "missing":
        context = replace(context, trusted_time_evidence=None)
        expected = "TRUSTED_TIME_EVIDENCE_MISSING"
    elif failure == "expired":
        scene.clock.advance(600)
        context = replace(context, trusted_time_evidence=scene.attestation(generation=11))
        expected = "ACTIVATION_AUTHORIZATION_EXPIRED"
    elif failure == "excessive":
        evidence = replace(evidence, authorization=scene.authorization(end="2030-01-16T00:00:01Z"))
        expected = "ACTIVATION_LIFETIME_EXCEEDED"
    elif failure == "old_recovery":
        # The real simulator advances the current audit chain on disable.
        ShadowActivationSimulator(registry=scene.registry, reader=scene.reader,
            audit_store=scene.env.audit_store).disable_shadow_runtime(at=NOW)
        scene.registry.select(scene.snapshot.snapshot_id)
        expected = "RE_ENABLE_RECOVERY_STATE_MISMATCH"
    elif failure == "old_snapshot":
        other = replace(scene.snapshot, generated_at="2030-01-15T00:00:01Z")
        digest = other.content_fingerprint()
        other = replace(other, snapshot_id=digest, snapshot_fingerprint=digest)
        scene.registry.register(other)
        scene.registry.select(other.snapshot_id)
        expected = "RE_ENABLE_RECOVERY_STATE_MISMATCH"
    generation = scene.reader.generation
    with pytest.raises(ShadowReenablementError) as exc:
        scene.reader.re_enable(at=NOW, read_context=context, evidence=evidence, recovery_evidence=recovery)
    assert expected in exc.value.rejection_codes
    assert not scene.reader.enabled and generation == scene.reader.generation


def test_additional_reused_evidence_identity_rejected_across_generations(scene):
    scene.clock.advance(1)
    scene.time.verify(scene.attestation(generation=11), dataset_identity=scene.context.dataset_identity)
    scene.clock.advance(1)
    rejected_time(scene, scene.attestation(generation=12, evidence_id="synthetic-time-10"), "TRUSTED_TIME_REPLAY")


def test_additional_session_capacity_fails_closed(scene, monkeypatch):
    import utils.trusted_time_authority as module
    monkeypatch.setattr(module, "MAX_SESSION_EVIDENCE", 1)
    rejected_time(scene, scene.attestation(generation=11), "TRUSTED_TIME_SESSION_EXHAUSTED")


def test_additional_rollback_tolerance_never_rewinds_effective_time(scene):
    verifier = TrustedTimeVerifier(scene.context.dataset_identity, "synthetic-time-authority",
                                   scene.public, scene.clock, rollback_tolerance_seconds=2)
    first = scene.attestation(session_challenge=verifier.session_challenge)
    verifier.verify(first, dataset_identity=scene.context.dataset_identity)
    scene.clock.advance(10)
    newer = scene.attestation(generation=11, session_challenge=verifier.session_challenge)
    latest = verifier.verify(newer, dataset_identity=scene.context.dataset_identity)
    earlier = scene.attestation(generation=12, at="2030-01-15T00:00:09Z",
                                session_challenge=verifier.session_challenge)
    result = verifier.verify(earlier, dataset_identity=scene.context.dataset_identity)
    assert result.effective_time >= latest.effective_time


@pytest.mark.parametrize("payload", ["[]", "null", "{", '"text"'])
def test_additional_malformed_envelope(scene, payload):
    rejected_time(scene, replace(scene.token, payload_json=payload), "TRUSTED_TIME_EVIDENCE_INVALID")


def test_additional_lifetime_malformed_timezone_rejected(scene):
    auth = resign_authorization(scene, replace(scene.auth, authorized_at="2030-01-15T00:00:00"))
    result = strict(scene, auth)
    assert "MALFORMED_TIMESTAMP" in result.rejection_codes and result.signature_valid


def test_additional_stale_governance_cannot_use_caller_time(scene):
    scene.clock.advance(3601)
    token = scene.attestation(generation=11)
    result = scene.read(current_time=NOW, trusted_time_evidence=token)
    assert "AUTHORITATIVE_GOVERNANCE_VIEW_INVALID" in result.rejection_codes


def test_additional_demo_failure_exits_nonzero(monkeypatch, capsys):
    import tools.shadow_trusted_time_validation as module
    monkeypatch.setattr(module, "run_acceptance", lambda root: {"success": False})
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out)["success"] is False


def test_strict_time_reenable_always_requires_existing_recovery_contract(scene):
    reader = ShadowRuntimeReader(registry=scene.registry, audit_store=scene.env.audit_store,
        require_trusted_time=True, strict_activation_verifier=scene.strict)
    reader.disable(at=NOW)
    with pytest.raises(ShadowReenablementError) as exc:
        reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence)
    assert "RE_ENABLE_RECOVERY_EVIDENCE_MISSING" in exc.value.rejection_codes
    assert not reader.enabled


@pytest.mark.parametrize("field,override,code", [
    ("purpose", "expected_purpose", "PURPOSE_MISMATCH"),
    ("authorization_schema_version", "expected_schema_version", "SCHEMA_VERSION_UNSUPPORTED"),
])
def test_strict_activation_domain_cannot_be_redefined_by_read_caller(scene, field, override, code):
    auth = resign_authorization(scene, replace(scene.auth, **{field: "other-contract"}))
    result = strict(scene, auth, **{override: "other-contract"})
    assert code in result.rejection_codes and not result.is_consumable
