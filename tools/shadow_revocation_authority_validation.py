"""独立撤销权威的真实合成演示；python -m tools.shadow_revocation_authority_validation。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from tools.shadow_activation_supersession_validation import SyntheticSupersessionScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes, require
from utils.activation_revocation_authority import (
    IndependentRevocationVerifier, RevocationEvidence, REVOCATION_PURPOSE, STATE_PURPOSE, CHECKPOINT_PURPOSE,
    revocation_event_payload, revocation_state_payload, revocation_checkpoint_payload,
)
from utils.mapping_governance import generate_mapping_owner_keypair
from utils.shadow_runtime_reader import ShadowAuthorizationEvidence, ShadowRuntimeReader, ShadowReenablementError
from utils.trusted_artifacts import sign_trusted_artifact


class SyntheticRevocationScenario(SyntheticSupersessionScenario):
    """所有角色及历史均在显式临时目录；初始 A CURRENT，随后显式 A→B。"""

    def __init__(self, workspace: Path, *, supersession=True):
        super().__init__(workspace)
        self.revocation_private = str(workspace / "keys" / "revocation-private.pem")
        self.revocation_public = str(workspace / "keys" / "revocation-public.pem")
        self.revocation_checkpoint_private = str(workspace / "keys" / "revocation-checkpoint-private.pem")
        self.revocation_checkpoint_public = str(workspace / "keys" / "revocation-checkpoint-public.pem")
        generate_mapping_owner_keypair(self.revocation_private, self.revocation_public)
        generate_mapping_owner_keypair(self.revocation_checkpoint_private, self.revocation_checkpoint_public)
        self.revocation = self.new_revocation_verifier()
        self.A, self.A_evidence, self.A_snapshot = self.auth, self.evidence, self.snapshot
        self.bundle = self.make_bundle(authorizations=(self.evidence,), transitions=(), current=(self.auth.activation_id,), sequence=1)
        self.revocations = self.make_revocations()
        self.reader = ShadowRuntimeReader(registry=self.registry, audit_store=self.env.audit_store,
            require_trusted_time=True, strict_activation_verifier=self.strict,
            require_authoritative_governance=True, require_authenticated_approval=True,
            require_observation_attestation=True, require_recovery_evidence_for_reenable=True,
            require_verified_supersession=supersession, supersession_verifier=self.lifecycle if supersession else None,
            require_independent_revocation=True, revocation_verifier=self.revocation)

    def new_revocation_verifier(self, **changes):
        kwargs = dict(dataset_identity=self.context.dataset_identity, authority_identity="synthetic-revocation-authority",
            revocation_public_key_path=self.revocation_public, checkpoint_public_key_path=self.revocation_checkpoint_public,
            activation_public_key_path=self.env.authorizer_public, lifecycle_public_key_path=self.lifecycle_public,
            owner_public_key_path=self.env.owner_public, strict_activation_verifier=self.strict)
        return IndependentRevocationVerifier(**{**kwargs, **changes})

    def revocation_event(self, auth=None, sequence=1, **changes):
        reason = changes.pop("reason", "explicit synthetic revocation")
        payload = revocation_event_payload(auth or self.auth, authority_identity="synthetic-revocation-authority",
            sequence=sequence, effective_at=self.clock().isoformat(), issued_at=self.clock().isoformat(),
            reason=reason)
        return sign_trusted_artifact(self.revocation_private, artifact_type=REVOCATION_PURPOSE, payload={**payload, **changes})

    def make_revocations(self, events=(), sequence=0, **changes):
        payload = revocation_state_payload(events, authorizations=self.bundle.authorizations,
            dataset_identity=self.context.dataset_identity, baseline_fingerprint=self.context.current_baseline_fingerprint,
            authority_identity="synthetic-revocation-authority", state_sequence=sequence,
            issued_at=self.clock().isoformat(), lifecycle_evidence=self.bundle)
        state = sign_trusted_artifact(self.revocation_private, artifact_type=STATE_PURPOSE, payload={**payload, **changes})
        checkpoint = sign_trusted_artifact(self.revocation_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
            payload=revocation_checkpoint_payload(state, session_challenge=self.revocation.session_challenge,
                issued_at=self.clock().isoformat()))
        return RevocationEvidence(tuple(events), state, checkpoint)

    def read_context(self, **overrides):
        if hasattr(self, "revocations"):
            overrides = {"revocation_evidence": self.revocations, **overrides}
        return super().read_context(**overrides)

    def verified_revocations(self, evidence=None, **context):
        return self.revocation.verify(self.revocations if evidence is None else evidence, read_context=self.read_context(**context))

    def successor(self, *, sequence=2, events=None, revocation_sequence=2):
        old = self.auth
        new = self.bound_authorization(reason=f"separately authorized successor {sequence}")
        evidence = ShadowAuthorizationEvidence(new, self.decision, self.env.proposal)
        transition = self.transition(old, new, f"successor-{sequence}", sequence)
        self.bundle = self.make_bundle(authorizations=(*self.bundle.authorizations, evidence),
            transitions=(*self.bundle.transitions, transition), current=(new.activation_id,), sequence=sequence)
        self.auth, self.evidence = new, evidence
        self.snapshot = self.snapshot_for((new,))
        self.registry.register(self.snapshot)
        self.registry.select(self.snapshot.snapshot_id)
        self.revocations = self.make_revocations(self.revocations.events if events is None else events, sequence=revocation_sequence)
        return new


def run_acceptance(workspace: Path):
    before = protected_store_hashes()
    scene = SyntheticRevocationScenario(workspace / "revocation")
    empty = scene.revocations
    initial = scene.read()
    checks = dict(current_authenticated_empty_state_resolves=initial.is_resolved,
        current_state_and_checkpoint_verified=scene.verified_revocations().state_sequence == 0,
        signed_owner_approval_and_activation=scene.verify().is_consumable)
    event_A = scene.revocation_event(scene.A, sequence=1)
    scene.revocations = scene.make_revocations((event_A,), sequence=1)
    rejected_A = scene.read(revocation_registry=())
    checks["A_revocation_enforced"] = "ACTIVATION_AUTHORIZATION_REVOKED" in rejected_A.rejection_codes
    checks["cached_resolution_invalidated"] = not scene.reader.is_generation_current(initial)
    checks["old_empty_state_replay_rejected"] = "REVOCATION_STATE_ROLLBACK" in scene.read(revocation_evidence=empty).rejection_codes
    scene.successor()
    checks["independent_B_after_explicit_A_to_B_resolves"] = scene.read().is_resolved
    checks["A_revocation_retained"] = scene.verified_revocations().is_revoked(scene.A)
    checks["superseded_A_rollback_rejected"] = not scene.simulator().rollback_shadow_snapshot(
        target_snapshot_id=scene.A_snapshot.snapshot_id, read_context=scene.read_context(), at=NOW,
        audit_checkpoint=scene.audit_checkpoint()).succeeded
    recovery = scene.audited_recovery()
    checks["current_B_guarded_recovery_succeeds"] = scene.read().is_resolved
    event_B = scene.revocation_event(sequence=3)
    scene.revocations = scene.make_revocations((event_A, event_B), sequence=3)
    rejected_B = scene.read()
    checks["B_current_revocation_enforced"] = "ACTIVATION_AUTHORIZATION_REVOKED" in rejected_B.rejection_codes
    scene.reader.disable(at=NOW)
    try:
        scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=recovery)
    except ShadowReenablementError as exc:
        checks["revoked_B_reenable_rejected"] = "ACTIVATION_AUTHORIZATION_REVOKED" in exc.rejection_codes and not scene.reader.enabled
    else:
        checks["revoked_B_reenable_rejected"] = False
    report = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint(), reconcile_disabled_reader=True)
    checks["revoked_B_disabled_reconciliation_rejected"] = not report.succeeded and "ACTIVATION_AUTHORIZATION_REVOKED" in report.rejection_codes
    checks["missing_authority_evidence_rejected"] = not scene.read(revocation_evidence=None).is_resolved
    checks["missing_trusted_time_rejected"] = not scene.read(trusted_time_evidence=None).is_resolved
    scene.simulator().disable_shadow_runtime(at=NOW)
    checks["emergency_disable_without_revocation_service"] = not scene.reader.enabled
    checks["seven_production_stores_unchanged"] = before == protected_store_hashes()
    require(all(checks.values()), f"synthetic revocation acceptance failed: {checks}")
    return dict(schema_version="1.0.0", synthetic_only=True, checks=checks, success=all(checks.values()),
        seven_store_sha256=before, production_activation_ready=False, decision="NO-GO")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-revocation-authority-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = dict(synthetic_only=True, success=False, error=str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
