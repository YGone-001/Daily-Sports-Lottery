"""Executable B-13 reproduction on the unmodified reader; synthetic keys only."""
from dataclasses import dataclass, fields, replace
import base64
import json
from pathlib import Path
import tempfile

from tools.shadow_approval_binding_validation import SyntheticApprovalScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.runtime_activation_authorization import build_shadow_activation_snapshot
from utils.shadow_runtime_reader import ShadowAuthorizationEvidence, ShadowReadContext
from utils.trusted_artifacts import SignedTrustedArtifact, verify_trusted_artifact
from utils.mapping_governance import generate_mapping_owner_keypair, _crypto_backend, load_trusted_public_key
from utils.source_identity import canonical_json


@dataclass(frozen=True)
class CallerClaimContext(ShadowReadContext):
    """Legacy Reader does not interpret or trust these additional caller fields."""
    claimed_superseded_ids: tuple = ()
    external_signed_statement: object = None


def reproduce(workspace: Path) -> dict:
    before = protected_store_hashes()
    results = {}
    for case in range(1, 6):
        scene = SyntheticApprovalScenario(workspace / str(case))
        old = scene.bound_authorization(expires_at="2030-01-15T00:00:05Z")
        new = scene.auth
        snapshot = build_shadow_activation_snapshot(authorizations=[old, new],
            verification_context=scene.context, observations=[scene.env.observation], generated_at=NOW)
        scene.registry.register(snapshot)
        scene.registry.select(snapshot.snapshot_id)
        evidence = (ShadowAuthorizationEvidence(old, scene.decision, scene.env.proposal), scene.evidence)
        if case in (1, 4, 5):
            scene.clock.advance(6)
            scene.token = scene.attestation(generation=11)
        if case == 2:
            scene.view = scene.governance_view(((old.activation_id, old.revocation_reference),))
        # The baseline has no lifecycle input. A caller assertion, even an external
        # signed statement, cannot affect its authorization selection algorithm.
        if case == 5:
            lifecycle_private = str(workspace / str(case) / "keys" / "supersession-private.pem")
            lifecycle_public = str(workspace / str(case) / "keys" / "supersession-public.pem")
            generate_mapping_owner_keypair(lifecycle_private, lifecycle_public)
            purpose = "RUNTIME_ACTIVATION_SUPERSESSION"
            payload = dict(lifecycle_schema_version="1.0.0", purpose=purpose,
                dataset_identity=new.dataset_identity, source_identity_key=new.scoped_source_identity_key,
                canonical_id=new.candidate_canonical_id, baseline_fingerprint=new.expected_baseline_fingerprint,
                authorized_scope=new.authorized_scope, old_activation_id=old.activation_id,
                old_authorization_fingerprint=old.fingerprint(), new_activation_id=new.activation_id,
                new_authorization_fingerprint=new.fingerprint(), old_proposal_id=old.proposal_id,
                new_proposal_id=new.proposal_id, approval_fingerprint=new.approval_fingerprint,
                eligibility_decision_fingerprint=new.eligibility_decision_fingerprint,
                transition_id="synthetic-A-to-B", sequence=2, effective_at=scene.clock().isoformat(),
                issued_at=scene.clock().isoformat(), authority_identity="synthetic-lifecycle-authority",
                reason="explicit baseline replacement")
            # The baseline signing helper does not yet allow this new purpose.
            # Use its exact SignedTrustedArtifact wire/signing payload and existing
            # Ed25519 backend solely to issue this external synthetic test input.
            statement = SignedTrustedArtifact(purpose, canonical_json(payload),
                load_trusted_public_key(lifecycle_public)["public_key_id"], "")
            key = _crypto_backend()["serialization"].load_pem_private_key(
                Path(lifecycle_private).read_bytes(), password=None)
            statement = replace(statement, signature=base64.b64encode(key.sign(statement.signing_payload())).decode("ascii"))
            verified = verify_trusted_artifact(statement, expected_artifact_type=purpose,
                trusted_public_key_path=lifecycle_public, current_time=scene.clock().isoformat(), max_age_seconds=300)
            signed_statement_present = verified == payload
        else:
            signed_statement_present = False
            statement = None
        base_context = scene.read_context(evidence=evidence)
        context = CallerClaimContext(**{f.name: getattr(base_context, f.name) for f in fields(ShadowReadContext)},
            claimed_superseded_ids=(old.activation_id,) if case in (4, 5) else (),
            external_signed_statement=statement)
        result = scene.reader.read(observation=scene.env.observation, snapshot=snapshot,
            read_context=context,
            observation_attestation=scene.observation_attestation)
        results[f"SP-{case:02}"] = {"status": result.resolution_status,
            "codes": list(result.rejection_codes), "authorization_count": len(result.authorization_ids),
            "caller_supersession_claim_present": bool(context.claimed_superseded_ids),
            "external_signed_statement_present": signed_statement_present}
    return {"baseline": "10ecca5fa57d275773042bb609bcf580020aac01", "results": results,
            "seven_stores_unchanged": before == protected_store_hashes()}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="supersession-baseline-") as temp:
        print(json.dumps(reproduce(Path(temp)), sort_keys=True, indent=2))
