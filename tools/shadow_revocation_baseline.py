"""在未增加独立撤销路径的基线上执行 RV-01..RV-06；仅使用临时合成密钥。"""
from dataclasses import dataclass, fields, replace
import base64
import json
from pathlib import Path
import tempfile

from tools.shadow_activation_supersession_validation import SyntheticSupersessionScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.mapping_governance import generate_mapping_owner_keypair, load_trusted_public_key, _crypto_backend
from utils.runtime_activation_authorization import verify_runtime_activation_authorization
from utils.shadow_runtime_reader import ShadowReadContext, ShadowReenablementError
from utils.source_identity import canonical_json, fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, verify_trusted_artifact, TrustedArtifactError


@dataclass(frozen=True)
class ExternalRevocationContext(ShadowReadContext):
    external_revocation_state: object = None
    external_revocation_checkpoint: object = None


def external_signed(private, public, purpose, payload):
    """基线签发白名单尚无新用途；用相同工件载荷和 Ed25519 后端准备外部测试输入。"""
    artifact = SignedTrustedArtifact(purpose, canonical_json(payload), load_trusted_public_key(public)["public_key_id"], "")
    key = _crypto_backend()["serialization"].load_pem_private_key(Path(private).read_bytes(), password=None)
    artifact = replace(artifact, signature=base64.b64encode(key.sign(artifact.signing_payload())).decode("ascii"))
    verify_trusted_artifact(artifact, expected_artifact_type=purpose, trusted_public_key_path=public,
        current_time=NOW, max_age_seconds=60)
    return artifact


def reproduce(workspace):
    before = protected_store_hashes()
    scene = SyntheticSupersessionScenario(workspace / "baseline")
    private, public = str(workspace / "revocation-private.pem"), str(workspace / "revocation-public.pem")
    generate_mapping_owner_keypair(private, public)
    auth = scene.auth
    event = external_signed(private, public, "RUNTIME_ACTIVATION_REVOCATION", dict(
        issued_at=NOW, effective_at=NOW, activation_id=auth.activation_id,
        authorization_fingerprint=auth.fingerprint(), revocation_reference=auth.revocation_reference,
        dataset_identity=auth.dataset_identity, source_identity_key=auth.scoped_source_identity_key,
        canonical_id=auth.candidate_canonical_id, reason="synthetic external revocation", sequence=1))
    ordinary = verify_runtime_activation_authorization(auth, replace(scene.context, revocation_registry=()))
    v1 = scene.view
    v2 = scene.governance_view(((auth.activation_id, auth.revocation_reference),))
    # V2 and external event exist but V1 remains fresh and is still supplied.
    old_view_result = scene.read(authoritative_view=v1, revocation_registry=())
    s1 = external_signed(private, public, "RUNTIME_ACTIVATION_REVOCATION_STATE",
        dict(issued_at=NOW, sequence=1, events=[event.fingerprint()]))
    s2 = external_signed(private, public, "RUNTIME_ACTIVATION_REVOCATION_STATE",
        dict(issued_at=NOW, sequence=2, events=[]))
    def with_external_state(state, scenario=scene, checkpoint=None):
        ctx = scenario.read_context()
        ctx = ExternalRevocationContext(**{f.name: getattr(ctx, f.name) for f in fields(ShadowReadContext)},
            external_revocation_state=state, external_revocation_checkpoint=checkpoint)
        return scenario.reader.read(observation=scenario.env.observation, snapshot=scenario.snapshot,
            read_context=ctx, observation_attestation=scenario.observation_attestation)
    s1_read, s2_read = with_external_state(s1), with_external_state(s2)
    scene.view = v2
    scene.bundle = scene.make_bundle(sequence=3)
    revoked_read = scene.read()
    scene.reader.disable(at=NOW)
    try:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence)
    except ShadowReenablementError as exc:
        reenable_codes = exc.rejection_codes
    else:
        reenable_codes = ()
    rollback = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint(), reconcile_disabled_reader=True)
    # A previously observed signed governance revocation cannot be erased in
    # existing supersession mode. This protection already exists at baseline.
    scene.view = v1
    scene.bundle = scene.make_bundle(sequence=4)
    try:
        scene.verified()
    except TrustedArtifactError as exc:
        erased_codes = exc.rejection_codes
    else:
        erased_codes = ()
    historical = SyntheticSupersessionScenario(workspace / "historical")
    historical.view = historical.governance_view(((historical.old.activation_id, historical.old.revocation_reference),))
    historical.bundle = historical.make_bundle(sequence=3)
    predecessor = historical.read()
    empty = SyntheticSupersessionScenario(workspace / "empty")
    empty_state = external_signed(private, public, "RUNTIME_ACTIVATION_REVOCATION_STATE", dict(
        revocation_schema_version="1.0.0", purpose="RUNTIME_ACTIVATION_REVOCATION_STATE",
        signing_role="revocation-authority", freshness_policy="isolated-revocation-v1",
        dataset_identity=empty.context.dataset_identity, baseline_fingerprint=empty.context.current_baseline_fingerprint,
        authority_identity="synthetic-revocation-authority", authority_scope="SCOPED_SHADOW_ACTIVATION_AUTHORIZATIONS",
        authorization_inventory=sorted((e.authorization.activation_id, e.authorization.fingerprint()) for e in empty.bundle.authorizations),
        state_sequence=0, revocation_event_fingerprints=[], currently_revoked_authorizations=[], issued_at=NOW,
        lifecycle_state_fingerprint=empty.bundle.state.fingerprint(), lifecycle_sequence=2))
    cp_private, cp_public = str(workspace / "revocation-checkpoint-private.pem"), str(workspace / "revocation-checkpoint-public.pem")
    generate_mapping_owner_keypair(cp_private, cp_public)
    checkpoint = external_signed(cp_private, cp_public, "RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT", dict(
        revocation_schema_version="1.0.0", purpose="RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT",
        signing_role="revocation-checkpoint", freshness_policy="isolated-revocation-v1",
        dataset_identity=empty.context.dataset_identity, authority_identity="synthetic-revocation-authority",
        state_sequence=0, state_fingerprint=empty_state.fingerprint(), event_count=0,
        event_inventory_fingerprint=fingerprint([]), issued_at=NOW, expires_at="2030-01-15T00:01:00Z",
        session_challenge="independent-synthetic-baseline-checkpoint"))
    empty_result = with_external_state(empty_state, empty, checkpoint)
    return dict(baseline="fcfe1a27882add98f897a96b85f1d20f95bca0ce", results={
        "RV-01": dict(ordinary_consumable=ordinary.is_consumable, independent_signed_event_verified=bool(event.signature)),
        "RV-02": dict(old_view_status=old_view_result.resolution_status, codes=old_view_result.rejection_codes),
        "RV-03": dict(external_S1_status=s1_read.resolution_status, external_S2_status=s2_read.resolution_status,
            external_states_verified=bool(s1.signature and s2.signature), baseline_has_independent_state_input=False,
            observed_governance_removal_codes=erased_codes),
        "RV-04": dict(read_codes=revoked_read.rejection_codes, reenable_codes=reenable_codes,
            rollback_codes=rollback.rejection_codes, rollback_succeeded=rollback.succeeded),
        "RV-05": dict(status=predecessor.resolution_status,
            historical_revocation_retained=any(e.revoked for e in historical.verified().entries)),
        "RV-06": dict(status=empty_result.resolution_status, independent_proof_required_at_baseline=False,
            external_empty_state_and_checkpoint_verified=checkpoint.payload()["state_fingerprint"] == empty_state.fingerprint(),
            missing_proof_status=empty.read().resolution_status)},
        seven_store_sha256=before, seven_stores_unchanged=before == protected_store_hashes())


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="revocation-baseline-") as temp:
        print(json.dumps(reproduce(Path(temp)), sort_keys=True, indent=2))
