"""可执行的合成 supersession 验收；python -m tools.shadow_activation_supersession_validation。"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile

from tools.shadow_approval_binding_validation import SyntheticApprovalScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes, require
from utils.activation_supersession import (
    ActivationSupersessionVerifier, AuthorizationLifecycleEvidence,
    SUPERSESSION_PURPOSE, STATE_PURPOSE, CHECKPOINT_PURPOSE,
    transition_payload, state_payload, checkpoint_payload,
)
from utils.mapping_governance import generate_mapping_owner_keypair
from utils.runtime_activation_authorization import build_shadow_activation_snapshot
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowAuthorizationEvidence, ShadowReenablementError
from utils.trusted_artifacts import sign_trusted_artifact, TrustedArtifactError


class SyntheticSupersessionScenario(SyntheticApprovalScenario):
    """复用 owner / activation / ledger checkpoint / view / time 各自独立密钥。"""

    def __init__(self, workspace: Path):
        super().__init__(workspace)
        self.lifecycle_private = str(workspace / "keys" / "lifecycle-private.pem")
        self.lifecycle_public = str(workspace / "keys" / "lifecycle-public.pem")
        generate_mapping_owner_keypair(self.lifecycle_private, self.lifecycle_public)
        self.lifecycle = self.new_lifecycle_verifier()
        self.old = self.bound_authorization(expires_at="2030-01-15T00:00:05Z", reason="historical A")
        self.old_evidence = ShadowAuthorizationEvidence(self.old, self.decision, self.env.proposal)
        self.old_snapshot = self.snapshot_for((self.old,))
        self.registry.register(self.old_snapshot)
        self.event = self.transition(self.old, self.auth, "A-to-B", 2)
        self.bundle = self.make_bundle()
        self.reader = ShadowRuntimeReader(registry=self.registry, audit_store=self.env.audit_store,
            require_trusted_time=True, strict_activation_verifier=self.strict,
            require_authoritative_governance=True, require_authenticated_approval=True,
            require_observation_attestation=True, require_recovery_evidence_for_reenable=True,
            require_verified_supersession=True, supersession_verifier=self.lifecycle)

    def new_lifecycle_verifier(self):
        return ActivationSupersessionVerifier(self.context.dataset_identity, "synthetic-lifecycle-authority",
            self.lifecycle_public, self.checkpoint_public, self.env.authorizer_public, self.strict)

    def transition(self, old, new, identity, sequence, **changes):
        payload = transition_payload(old, new, transition_id=identity, sequence=sequence,
            effective_at=self.clock().isoformat(), authority_identity="synthetic-lifecycle-authority",
            reason="explicit synthetic replacement")
        return sign_trusted_artifact(self.lifecycle_private, artifact_type=SUPERSESSION_PURPOSE,
            payload={**payload, **changes})

    def make_bundle(self, authorizations=None, transitions=None, current=None, sequence=2, **changes):
        authorizations = (self.old_evidence, self.evidence) if authorizations is None else tuple(authorizations)
        transitions = (self.event,) if transitions is None else tuple(transitions)
        current = (self.auth.activation_id,) if current is None else tuple(current)
        payload = state_payload(authorizations, transitions, current_activation_ids=current,
            dataset_identity=self.context.dataset_identity, baseline_fingerprint=self.context.current_baseline_fingerprint,
            governance_fingerprint=self.view.fingerprint(), sequence=sequence,
            issued_at=self.clock().isoformat(), authority_identity="synthetic-lifecycle-authority")
        state = sign_trusted_artifact(self.lifecycle_private, artifact_type=STATE_PURPOSE,
            payload={**payload, **changes})
        checkpoint = sign_trusted_artifact(self.checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
            payload=checkpoint_payload(state, session_challenge=self.lifecycle.session_challenge,
                issued_at=self.clock().isoformat()))
        return AuthorizationLifecycleEvidence(authorizations, transitions, state, checkpoint)

    def snapshot_for(self, auths):
        return build_shadow_activation_snapshot(authorizations=auths, verification_context=self.context,
            observations=(self.env.observation,), generated_at=NOW)

    def read_context(self, **overrides):
        # During the parent constructor the lifecycle evidence does not exist yet.
        if getattr(self, "bundle", None) is not None:
            overrides = {"evidence": self.bundle.authorizations, "lifecycle_evidence": self.bundle, **overrides}
        return super().read_context(**overrides)

    def read(self, **overrides):
        return self.reader.read(observation=self.env.observation, snapshot=self.registry.active(),
            read_context=self.read_context(**overrides), observation_attestation=self.observation_attestation)

    def verified(self, bundle=None, **context):
        bundle = self.bundle if bundle is None else bundle
        return self.lifecycle.verify(bundle, read_context=self.read_context(
            **{"evidence": bundle.authorizations, **context}))


def run_acceptance(workspace: Path) -> dict:
    before = protected_store_hashes()
    scene = SyntheticSupersessionScenario(workspace / "supersession")
    initial = scene.read()
    checks = {"independently_signed_owner_approval": scene.verify().is_consumable,
        "signed_governance_and_eligibility": scene.decision.is_eligible,
        "signed_A_and_B": len(scene.verified().entries) == 2,
        "explicit_A_to_B_only_B_consumable": initial.is_resolved and initial.authorization_ids == (scene.auth.activation_id,),
        "A_retained_as_history": any(e.activation_id == scene.old.activation_id and e.selection == "SUPERSEDED"
                                    for e in scene.verified().entries)}
    scene.clock.advance(6)
    scene.token = scene.attestation(generation=11)
    checks["expired_A_does_not_block_B"] = scene.read().is_resolved
    scene.view = scene.governance_view(((scene.old.activation_id, scene.old.revocation_reference),))
    scene.bundle = scene.make_bundle(sequence=3)
    checks["revoked_A_does_not_block_B"] = scene.read().is_resolved
    checks["A_expiry_and_revocation_both_retained"] = any(
        e.activation_id == scene.old.activation_id and e.expired and e.revoked for e in scene.verified().entries)
    checks["missing_supersession_rejected"] = not scene.read(lifecycle_evidence=None).is_resolved
    ambiguous = scene.make_bundle(transitions=(), current=(scene.old.activation_id, scene.auth.activation_id), sequence=4)
    try:
        scene.verified(ambiguous)
    except TrustedArtifactError:
        checks["two_current_rejected"] = True
    else:
        checks["two_current_rejected"] = False
    rollback = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.old_snapshot.snapshot_id,
        read_context=scene.read_context(), at=scene.clock().isoformat(), audit_checkpoint=scene.audit_checkpoint())
    checks["rollback_A_rejected"] = not rollback.succeeded and "LIFECYCLE_SNAPSHOT_NOT_CURRENT" in rollback.rejection_codes
    checks["trusted_time_required"] = not scene.read(trusted_time_evidence=None).is_resolved
    checks["owner_governance_required"] = not scene.read(authoritative_view=None).is_resolved
    # Exercise real audited disable/reconciliation/recovery and strict re-enable.
    scene.audited_recovery()
    checks["current_B_recovery_reenable"] = scene.read().is_resolved
    scene.reader.disable(at=NOW)
    try:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.old_evidence)
    except ShadowReenablementError:
        checks["superseded_A_reenable_rejected"] = not scene.reader.enabled
    else:
        checks["superseded_A_reenable_rejected"] = False
    checks["emergency_disable_available"] = not scene.read().is_resolved
    checks["seven_stores_byte_identical"] = before == protected_store_hashes()
    require(all(checks.values()), f"acceptance failed: {checks}")
    return dict(schema_version="1.0.0", synthetic_only=True, checks=checks,
        seven_store_sha256=before, success=all(checks.values()),
        production_activation_ready=False, decision="NO-GO")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-supersession-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = dict(synthetic_only=True, success=False, error=str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
