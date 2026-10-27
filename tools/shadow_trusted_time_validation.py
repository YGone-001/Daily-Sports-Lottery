"""Executable synthetic trusted-time acceptance; no production credentials or writes.

Run: python -m tools.shadow_trusted_time_validation
"""
from __future__ import annotations

import json
import hashlib
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from tools.shadow_runtime_activation import build_synthetic_shadow_fixture, DATASET
from utils.authoritative_governance import build_authoritative_governance_view, ledger_identity
from utils.mapping_governance import GovernanceLedger, generate_mapping_owner_keypair
from utils.observation_authority import sign_observation_attestation
from utils.recovery_authority import build_recovery_evidence
from utils.runtime_activation_authorization import (
    ActivationVerificationContext, StrictActivationVerifier,
    build_activation_authorization_body, sign_runtime_activation_authorization,
    build_shadow_activation_snapshot,
)
from utils.runtime_mapping_eligibility import (
    RuntimeMappingContext, GovernanceEvidenceProvenance,
    CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER, evaluate_runtime_mapping_eligibility,
)
from utils.shadow_activation_simulation import ShadowActivationSimulator, ShadowSnapshotRegistry
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowAuthorizationEvidence
from utils.trusted_artifacts import sign_trusted_artifact
from utils.trusted_time_authority import TrustedTimeVerifier, sign_time_attestation, parse_utc

NOW = "2030-01-15T00:00:00+00:00"
STORES = ("daily_matches.json", "team_strength.json", "calibrated.json",
          "prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json")


def protected_store_hashes() -> dict:
    root = Path(__file__).resolve().parents[1] / "data"
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            if (root / name).exists() else "<absent>" for name in STORES}


def require(condition, description):
    """Acceptance invariants remain enforced even with python -O."""
    if not condition:
        raise RuntimeError(description)


@dataclass
class SyntheticReferenceClock:
    """Independently controlled test clock, never populated from an artifact."""
    now: datetime

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class SyntheticTimeScenario:
    """All signers, governance, audit bytes and authority are temporary fixtures."""

    def __init__(self, workspace: Path, *, separate_governance_roles: bool = False):
        workspace.mkdir(parents=True, exist_ok=True)
        self.env = build_synthetic_shadow_fixture(str(workspace))
        self.clock = SyntheticReferenceClock(parse_utc(NOW))
        self.private = str(workspace / "keys" / "time-private.pem")
        self.public = str(workspace / "keys" / "time-public.pem")
        generate_mapping_owner_keypair(self.private, self.public)
        self.governance_private = str(workspace / "keys" / "governance-private.pem")
        self.governance_public = str(workspace / "keys" / "governance-public.pem")
        generate_mapping_owner_keypair(self.governance_private, self.governance_public)
        self.checkpoint_private, self.checkpoint_public = self.governance_private, self.governance_public
        if separate_governance_roles:
            self.checkpoint_private = str(workspace / "keys" / "checkpoint-private.pem")
            self.checkpoint_public = str(workspace / "keys" / "checkpoint-public.pem")
            generate_mapping_owner_keypair(self.checkpoint_private, self.checkpoint_public)
        self.time = TrustedTimeVerifier(DATASET, "synthetic-time-authority", self.public, self.clock)
        self.strict = StrictActivationVerifier(self.time)
        self.ledger = GovernanceLedger(str(workspace / "governance.jsonl"), dataset_identity=DATASET)
        for state, actor, role in (("PROPOSED", "shadow-operator", "operator"),
                                   ("APPROVED", "shadow-owner", "owner")):
            self.ledger.append(proposal=self.env.proposal, to_state=state, actor=actor,
                actor_role=role, reason="synthetic trusted-time acceptance",
                baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
                **({"approval": self.env.approval,
                    "trusted_public_key_path": self.env.owner_public,
                    "source_observation_fingerprint": self.env.observation.semantic_fingerprint()}
                   if state == "APPROVED" else {}))
        self.view = self.governance_view()
        eligibility_context = RuntimeMappingContext(dataset_identity=DATASET,
            current_baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
            evidence_provenance=GovernanceEvidenceProvenance(
                source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
                dataset_identity=DATASET, baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
                structural_ledger_validation_performed=True, authenticated_approval_audit_performed=True,
                ledger_trust_anchor="synthetic signed checkpoint"),
            canonical_ids=self.view.canonical_ids, trusted_public_key_path=self.env.owner_public,
            expected_approver="shadow-owner", expected_operator="shadow-operator",
            recorded_proposal_id=self.env.proposal.proposal_id,
            recorded_proposal_fingerprint=self.env.proposal.fingerprint(), recorded_proposal_state="APPROVED",
            known_source_bindings=tuple((key, cid) for key, cid, _ in self.view.active_bindings),
            authoritative_view=self.view,
            authoritative_view_trusted_public_key_path=self.governance_public)
        self.decision = evaluate_runtime_mapping_eligibility(self.env.proposal, self.env.approval,
            self.env.observation, eligibility_context, evaluated_at=NOW)
        require(self.decision.eligibility_result == "ELIGIBLE", "synthetic eligibility failed")
        self.eligibility_context = eligibility_context
        self.auth = self.authorization()
        self.context = ActivationVerificationContext(DATASET,
            self.env.snapshot.baseline_fingerprint, NOW, self.env.authorizer_public,
            self.decision, self.env.proposal, require_verified_governance=True)
        self.evidence = ShadowAuthorizationEvidence(self.auth, self.decision, self.env.proposal)
        self.token = self.attestation()
        verified = self.strict.verify(self.auth, self.context, time_evidence=self.token)
        require(verified.is_consumable, f"strict authorization failed: {verified.rejection_codes}")
        self.snapshot = build_shadow_activation_snapshot(authorizations=[self.auth],
            verification_context=self.context, observations=[self.env.observation],
            generated_at=NOW, dataset_identity=DATASET)
        self.registry = ShadowSnapshotRegistry()
        self.registry.register(self.snapshot)
        self.registry.select(self.snapshot.snapshot_id)
        self.reader = ShadowRuntimeReader(registry=self.registry, audit_store=self.env.audit_store,
            require_trusted_time=True, strict_activation_verifier=self.strict,
            require_authoritative_governance=True, require_observation_attestation=True,
            require_recovery_evidence_for_reenable=True)
        self.observation_attestation = sign_observation_attestation(self.governance_private,
            self.env.observation, captured_at=NOW, dataset_identity=DATASET, adapter="market_xml")

    def governance_view(self, revocations=()):
        def sign(kind, **payload):
            return sign_trusted_artifact(self.checkpoint_private, artifact_type=kind,
                payload={**payload, "dataset_identity": DATASET, "issued_at": self.clock().isoformat()})
        entries = self.ledger.entries()
        checkpoint = sign("GOVERNANCE_LEDGER_CHECKPOINT", ledger_identity=ledger_identity(self.ledger),
                          chain_tip=entries[-1].entry_hash, entry_count=len(entries))
        canonical = sign("CANONICAL_BASELINE_MANIFEST", baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
                         canonical_ids=list(self.env.read_context().known_canonical_ids))
        revocation = sign("ACTIVATION_REVOCATION_MANIFEST", revocations=list(revocations))
        self.governance_inputs = dict(ledger_path=self.ledger.path, dataset_identity=DATASET,
            current_baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
            trusted_owner_public_key_path=self.env.owner_public, ledger_checkpoint=checkpoint,
            ledger_checkpoint_trusted_public_key_path=self.checkpoint_public,
            canonical_manifest=canonical, canonical_manifest_trusted_public_key_path=self.checkpoint_public,
            revocation_manifest=revocation, revocation_manifest_trusted_public_key_path=self.checkpoint_public,
            view_signing_private_key_path=self.governance_private,
            view_trusted_public_key_path=self.governance_public, current_time=self.clock().isoformat())
        return build_authoritative_governance_view(**self.governance_inputs)

    def authorization(self, start=NOW, end="2030-01-15T00:10:00+00:00"):
        return sign_runtime_activation_authorization(self.env.authorizer_private,
            build_activation_authorization_body(decision=self.decision, proposal=self.env.proposal,
                dataset_identity=DATASET, authorized_scope=self.env.authorization.authorized_scope,
                authorizer="synthetic-authorizer", operator="shadow-operator", authorized_at=start,
                expires_at=end, revocation_reference=self.env.authorization.revocation_reference))

    def attestation(self, generation=10, at=None, **overrides):
        issued = parse_utc(at) if at else self.clock()
        kwargs = dict(dataset_identity=DATASET, authority_identity="synthetic-time-authority",
            attested_at=issued.isoformat(), expires_at=(issued + timedelta(seconds=60)).isoformat(),
            generation=generation, evidence_id=f"synthetic-time-{generation}",
            session_challenge=self.time.session_challenge)
        kwargs.update(overrides)
        return sign_time_attestation(self.private, **kwargs)

    def read_context(self, **overrides):
        values = dict(current_time="1900-01-01T00:00:00+00:00", evidence=(self.evidence,),
            authoritative_view=self.view, authoritative_view_trusted_public_key_path=self.governance_public,
            observation_trusted_public_key_path=self.governance_public,
            recovery_trusted_public_key_path=self.governance_public, trusted_time_evidence=self.token)
        values.update(overrides)
        return self.env.read_context(**values)

    def read(self, **overrides):
        return self.reader.read(observation=self.env.observation, snapshot=self.snapshot,
            read_context=self.read_context(**overrides), observation_attestation=self.observation_attestation)

    def audited_recovery(self):
        """Real disable, explicit checkpoint-guarded reconciliation, guarded re-enable."""
        historical = self.read()
        require(historical.is_resolved, "initial strict shadow read failed")
        simulator = ShadowActivationSimulator(registry=self.registry, reader=self.reader,
            audit_store=self.env.audit_store, require_trusted_audit_checkpoint=True,
            audit_checkpoint_trusted_public_key_path=self.governance_public)
        simulator.disable_shadow_runtime(at=NOW, reason="synthetic emergency disable")
        require(self.env.audit_store.records()[-1].event_type == "SHADOW_RUNTIME_DISABLED",
                "real disable audit event missing")
        checkpoint = self.env.audit_store.export_checkpoint(self.governance_private, issued_at=NOW)
        report = simulator.rollback_shadow_snapshot(target_snapshot_id=self.snapshot.snapshot_id,
            read_context=self.read_context(), at=NOW, audit_checkpoint=checkpoint,
            reconcile_disabled_reader=True)
        require(report.succeeded, f"reconciliation failed: {report.rejection_codes}")
        require(not self.reader.enabled and not self.reader.is_generation_current(historical),
                "reconciliation bypassed disabled consumption")
        checkpoint = self.env.audit_store.export_checkpoint(self.governance_private, issued_at=NOW)
        self.env.audit_store.verify_external_checkpoint(checkpoint,
            trusted_public_key_path=self.governance_public, current_time=NOW, max_age_seconds=60)
        recovery = build_recovery_evidence(audit_store=self.env.audit_store, audit_checkpoint=checkpoint,
            checkpoint_trusted_public_key_path=self.governance_public,
            signing_private_key_path=self.governance_private, current_time=NOW,
            recovery_actor="synthetic-recovery-operator", selected_snapshot_id=self.registry.active_snapshot_id,
            effective_snapshot_id=self.registry.active_snapshot_id, max_age_seconds=60)
        resumed = self.reader.re_enable(at=NOW, read_context=self.read_context(),
            evidence=self.evidence, recovery_evidence=recovery)
        require(resumed["enabled"] and self.read().is_resolved, "guarded resumption failed")
        require(not self.reader.is_generation_current(historical), "historical handle revived")
        return recovery


def run_acceptance(workspace: Path) -> dict:
    before = protected_store_hashes()
    scenario = SyntheticTimeScenario(workspace / "time")
    def verify(auth=None, token=None):
        return scenario.strict.verify(auth or scenario.auth, scenario.context,
                                      time_evidence=scenario.token if token is None else token)
    checks = {}
    checks["valid_authorization_accepted"] = verify().is_consumable
    checks["valid_shadow_read_accepted"] = scenario.read().is_resolved
    checks["excessive_lifetime_rejected"] = "ACTIVATION_LIFETIME_EXCEEDED" in verify(
        scenario.authorization(end="2030-01-16T00:00:01+00:00")).rejection_codes
    checks["forged_time_signature_rejected"] = "TRUSTED_ARTIFACT_SIGNATURE_INVALID" in verify(
        token=replace(scenario.token, signature="A" * 88)).rejection_codes
    checks["wrong_dataset_rejected"] = "TRUSTED_TIME_DATASET_MISMATCH" in verify(
        token=scenario.attestation(dataset_identity="another-dataset")).rejection_codes
    checks["missing_trusted_time_rejected"] = "TRUSTED_TIME_EVIDENCE_MISSING" in scenario.strict.verify(
        scenario.auth, scenario.context).rejection_codes
    checks["backward_time_generation_rejected"] = "TRUSTED_TIME_GENERATION_ROLLBACK" in verify(
        token=scenario.attestation(generation=9)).rejection_codes
    scenario.clock.advance(61)
    checks["stale_time_evidence_rejected"] = "TRUSTED_ARTIFACT_STALE" in verify().rejection_codes
    scenario.clock.advance(539)
    checks["expired_authorization_rejected"] = "ACTIVATION_AUTHORIZATION_EXPIRED" in verify(
        token=scenario.attestation(generation=11)).rejection_codes
    recovery = SyntheticTimeScenario(workspace / "recovery")
    recovery.audited_recovery()
    checks["guarded_recovery_path_verified"] = True
    checks["seven_stores_byte_identical"] = before == protected_store_hashes()
    return {"schema_version": "1.0.0", "synthetic_only": True,
            "production_activation": "NOT_ENABLED", "production_activation_ready": False,
            "decision": "NO-GO", "checks": checks, "success": all(checks.values())}


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-trusted-time-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = {"synthetic_only": True, "success": False, "error": str(exc)}
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
