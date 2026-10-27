"""Synthetic authenticated owner-approval acceptance. Run with python -m tools.shadow_approval_binding_validation."""
from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from tools.shadow_trusted_time_validation import (
    SyntheticTimeScenario, NOW, DATASET, protected_store_hashes, require,
)
from utils.mapping_governance import (
    generate_mapping_owner_keypair, GovernanceError, verify_mapping_approval,
)
from utils.runtime_activation_authorization import (
    StrictActivationVerifier, build_approval_bound_authorization_body,
    sign_runtime_activation_authorization, build_shadow_activation_snapshot,
)
from utils.runtime_mapping_eligibility import evaluate_runtime_mapping_eligibility
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowAuthorizationEvidence
from utils.shadow_activation_simulation import ShadowActivationSimulator, ShadowSnapshotRegistry
from utils.recovery_authority import build_recovery_evidence


class SyntheticApprovalScenario(SyntheticTimeScenario):
    """Five independent required signers plus a separate recovery signer, all temporary."""

    def __init__(self, workspace: Path):
        super().__init__(workspace, separate_governance_roles=True)
        self.strict = StrictActivationVerifier(self.time, require_authenticated_approval=True,
            governance_view_trusted_public_key_path=self.governance_public)
        self.eligibility_context = replace(self.eligibility_context, require_authenticated_approval=True)
        self.decision = evaluate_runtime_mapping_eligibility(self.env.proposal, self.env.approval,
            self.env.observation, self.eligibility_context, evaluated_at=NOW)
        require(self.decision.is_eligible, "authenticated eligibility failed")
        self.context = replace(self.context, eligibility_decision=self.decision)
        self.auth = self.bound_authorization()
        self.evidence = ShadowAuthorizationEvidence(self.auth, self.decision, self.env.proposal)
        self.snapshot = build_shadow_activation_snapshot(authorizations=[self.auth],
            verification_context=self.context, observations=[self.env.observation], generated_at=NOW,
            dataset_identity=DATASET)
        self.registry = ShadowSnapshotRegistry()
        self.registry.register(self.snapshot)
        self.registry.select(self.snapshot.snapshot_id)
        self.reader = ShadowRuntimeReader(registry=self.registry, audit_store=self.env.audit_store,
            require_trusted_time=True, strict_activation_verifier=self.strict,
            require_authoritative_governance=True, require_authenticated_approval=True,
            require_observation_attestation=True, require_recovery_evidence_for_reenable=True)
        self.recovery_private = str(workspace / "keys" / "recovery-private.pem")
        self.recovery_public = str(workspace / "keys" / "recovery-public.pem")
        generate_mapping_owner_keypair(self.recovery_private, self.recovery_public)

    def bound_authorization(self, **overrides):
        kwargs = dict(dataset_identity=DATASET, authorized_scope=self.env.authorization.authorized_scope,
            authorizer="synthetic-authorizer", operator="shadow-operator", authorized_at=NOW,
            expires_at="2030-01-15T00:10:00Z", revocation_reference=self.env.authorization.revocation_reference)
        kwargs.update(overrides)
        body = build_approval_bound_authorization_body(verifier=self.strict, governance_view=self.view,
            time_evidence=self.token, decision=self.decision, proposal=self.env.proposal, **kwargs)
        return sign_runtime_activation_authorization(self.env.authorizer_private, body)

    def read_context(self, **overrides):
        values = dict(recovery_trusted_public_key_path=self.recovery_public)
        values.update(overrides)
        return super().read_context(**values)

    def verify(self, auth=None, view=None, **context):
        return self.strict.verify(auth or self.auth, replace(self.context, **context),
            time_evidence=self.token, governance_view=self.view if view is None else view)

    def simulator(self):
        return ShadowActivationSimulator(registry=self.registry, reader=self.reader,
            audit_store=self.env.audit_store, require_trusted_audit_checkpoint=True,
            audit_checkpoint_trusted_public_key_path=self.checkpoint_public)

    def audit_checkpoint(self):
        return self.env.audit_store.export_checkpoint(self.checkpoint_private, issued_at=NOW)

    def audited_recovery(self):
        previous = self.read()
        require(previous.is_resolved, "initial approval-bound read failed")
        simulator = self.simulator()
        simulator.disable_shadow_runtime(at=NOW)
        require(self.env.audit_store.records()[-1].event_type == "SHADOW_RUNTIME_DISABLED", "disable not audited")
        rollback = simulator.rollback_shadow_snapshot(target_snapshot_id=self.snapshot.snapshot_id,
            read_context=self.read_context(), at=NOW, audit_checkpoint=self.audit_checkpoint(),
            reconcile_disabled_reader=True)
        require(rollback.succeeded and not self.reader.enabled, "disabled approval reconciliation failed")
        recovery = build_recovery_evidence(audit_store=self.env.audit_store, audit_checkpoint=self.audit_checkpoint(),
            checkpoint_trusted_public_key_path=self.checkpoint_public,
            signing_private_key_path=self.recovery_private, current_time=NOW,
            recovery_actor="synthetic-recovery", selected_snapshot_id=self.registry.active_snapshot_id,
            effective_snapshot_id=self.registry.active_snapshot_id, max_age_seconds=60)
        self.reader.re_enable(at=NOW, read_context=self.read_context(), evidence=self.evidence, recovery_evidence=recovery)
        require(self.read().is_resolved and not self.reader.is_generation_current(previous), "guarded resumption failed")
        return recovery

    def supersede(self):
        self.ledger.append(proposal=self.env.proposal, to_state="SUPERSEDED", actor="shadow-owner",
            actor_role="owner", reason="separately governed synthetic replacement",
            baseline_fingerprint=self.context.current_baseline_fingerprint)
        self.view = self.governance_view()
        return self.view


def run_acceptance(workspace: Path) -> dict:
    before = protected_store_hashes()
    scene = SyntheticApprovalScenario(workspace / "approval")
    checks = {"signed_owner_approval_authenticated": scene.view.approved_records[0].approval_fingerprint
              == scene.decision.approval_fingerprint == scene.auth.approval_fingerprint,
              "authenticated_ledger_transition": scene.ledger.entries()[-1].transition_id
              == scene.view.approved_records[0].approved_transition_identity,
              "signed_current_governance_view": scene.verify().is_consumable,
              "eligible_decision": scene.decision.is_eligible,
              "correct_activation_authorization": scene.verify().signature_valid,
              "strict_shadow_resolution": scene.read().is_resolved}
    wrong_body = build_approval_bound_authorization_body(verifier=scene.strict, governance_view=scene.view,
        time_evidence=scene.token, decision=scene.decision, proposal=scene.env.proposal,
        dataset_identity=DATASET, authorized_scope=scene.auth.authorized_scope,
        authorizer="synthetic-authorizer", operator="shadow-operator", authorized_at=NOW,
        expires_at="2030-01-15T00:10:00Z")
    wrong_body["approval_fingerprint"] = "0" * 64
    wrong = sign_runtime_activation_authorization(scene.env.authorizer_private, wrong_body)
    checks["wrong_approval_fingerprint_rejected"] = "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH" in scene.verify(wrong).rejection_codes
    # Real cryptographic owner verification against retained proposal and trusted key.
    forged = copy.deepcopy(scene.env.approval)
    forged["approval"]["signature"] = "A" * 88
    try:
        verify_mapping_approval(forged, proposal=scene.env.proposal,
            source_observation_fingerprint=scene.env.observation.semantic_fingerprint(), dataset_identity=DATASET,
            current_baseline_fingerprint=scene.context.current_baseline_fingerprint,
            trusted_public_key_path=scene.env.owner_public)
    except GovernanceError:
        checks["forged_owner_approval_rejected"] = True
    else:
        checks["forged_owner_approval_rejected"] = False
    scene.audited_recovery()
    checks["guarded_reenable_verified"] = True
    scene.supersede()
    checks["obsolete_approval_rejected"] = "ACTIVATION_APPROVAL_NOT_CURRENT" in scene.verify().rejection_codes
    missing_time = scene.strict.verify(scene.auth, scene.context, governance_view=scene.view)
    checks["trusted_time_enforced"] = "TRUSTED_TIME_EVIDENCE_MISSING" in missing_time.rejection_codes
    checks["seven_store_byte_integrity"] = before == protected_store_hashes()
    return {"schema_version": "1.0.0", "synthetic_only": True, "checks": checks,
            "success": all(checks.values()), "production_activation_ready": False, "decision": "NO-GO"}


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-approval-binding-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = {"synthetic_only": True, "success": False, "error": str(exc)}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
