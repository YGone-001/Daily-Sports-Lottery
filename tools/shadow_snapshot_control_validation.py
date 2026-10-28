"""持久控制真实验收；python -m tools.shadow_snapshot_control_validation。"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from tools.shadow_revocation_authority_validation import SyntheticRevocationScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes, require
from tools.shadow_trusted_time_validation import DATASET, SyntheticReferenceClock
from utils.trusted_time_authority import TrustedTimeVerifier, parse_utc
from utils.runtime_activation_authorization import StrictActivationVerifier
from utils.activation_supersession import ActivationSupersessionVerifier
from utils.activation_revocation_authority import IndependentRevocationVerifier
from utils.mapping_governance import generate_mapping_owner_keypair
from utils.persistent_shadow_snapshots import BoundedShadowLock
from utils.recovery_authority import build_recovery_evidence
from utils.shadow_activation_audit_store import ShadowActivationAuditStore
from utils.shadow_activation_simulation import ShadowSnapshotRegistry
from utils.shadow_runtime_reader import ShadowRuntimeReader
from utils.shadow_snapshot_control import (PersistentShadowSnapshotController, PUBLICATION_PURPOSE,
    SELECTION_PURPOSE, CHECKPOINT_PURPOSE, publication_request_payload, selection_request_payload)
from utils.trusted_artifacts import sign_trusted_artifact


def inspect_restart_public(workspace):
    """新的验证器会话只读重建；仅加载公钥，不读取或生成任何私钥。"""
    workspace = Path(workspace)
    def key(name):
        return str(workspace / "keys" / name)
    time = TrustedTimeVerifier(DATASET, "synthetic-time-authority", key("time-public.pem"),
        SyntheticReferenceClock(parse_utc(NOW)))
    strict = StrictActivationVerifier(time, require_authenticated_approval=True,
        governance_view_trusted_public_key_path=key("governance-public.pem"))
    lifecycle = ActivationSupersessionVerifier(DATASET, "synthetic-lifecycle-authority", key("lifecycle-public.pem"),
        key("checkpoint-public.pem"), key("authorizer_public.pem"), strict)
    revocation = IndependentRevocationVerifier(DATASET, "synthetic-revocation-authority", key("revocation-public.pem"),
        key("revocation-checkpoint-public.pem"), key("authorizer_public.pem"), key("lifecycle-public.pem"),
        key("owner_public.pem"), strict)
    root = workspace / "persistent-control"
    audit = ShadowActivationAuditStore(str(root), dataset_identity=DATASET,
        filename="control-audit.jsonl", lock_factory=BoundedShadowLock)
    registry = ShadowSnapshotRegistry()
    controller = PersistentShadowSnapshotController(str(root), dataset_identity=DATASET, registry=registry,
        audit_store=audit, publisher_public_key_path=key("publisher-public.pem"), selector_public_key_path=key("selector-public.pem"),
        checkpoint_public_key_path=key("control_checkpoint-public.pem"), lifecycle_verifier=lifecycle, revocation_verifier=revocation)
    reader = ShadowRuntimeReader(registry=registry, audit_store=audit, strict_activation_verifier=strict,
        require_trusted_time=True, require_authoritative_governance=True, require_authenticated_approval=True,
        require_verified_supersession=True, supersession_verifier=lifecycle, require_independent_revocation=True,
        revocation_verifier=revocation, require_observation_attestation=True, require_recovery_evidence_for_reenable=True,
        require_controlled_snapshot_selection=True, snapshot_controller=controller)
    return dict(**controller.recover_read_only(), registered=len(registry),
        reader_enabled=reader.enabled, memory_selected=registry.active_snapshot_id)


class SyntheticControlScenario(SyntheticRevocationScenario):
    def __init__(self, workspace: Path):
        super().__init__(workspace)
        self.control_workspace = workspace / "persistent-control"
        self.control_workspace.mkdir()
        for role in ("publisher", "selector", "control_checkpoint"):
            private, public = str(workspace / "keys" / f"{role}-private.pem"), str(workspace / "keys" / f"{role}-public.pem")
            generate_mapping_owner_keypair(private, public)
            setattr(self, role + "_private", private)
            setattr(self, role + "_public", public)
        self.audit = ShadowActivationAuditStore(str(self.control_workspace), dataset_identity=self.context.dataset_identity,
            filename="control-audit.jsonl", lock_factory=BoundedShadowLock)
        self._request_count = 0
        self.registry = ShadowSnapshotRegistry()
        self.controller = self.new_controller()
        self.reader = self.new_reader()
        self.control_checkpoint = self.checkpoint()

    def new_controller(self):
        return PersistentShadowSnapshotController(str(self.control_workspace), dataset_identity=self.context.dataset_identity,
            registry=self.registry, audit_store=self.audit, publisher_public_key_path=self.publisher_public,
            selector_public_key_path=self.selector_public, checkpoint_public_key_path=self.control_checkpoint_public,
            lifecycle_verifier=self.lifecycle, revocation_verifier=self.revocation)

    def new_reader(self):
        return ShadowRuntimeReader(registry=self.registry, audit_store=self.audit,
            require_trusted_time=True, strict_activation_verifier=self.strict,
            require_authoritative_governance=True, require_authenticated_approval=True,
            require_verified_supersession=True, supersession_verifier=self.lifecycle,
            require_independent_revocation=True, revocation_verifier=self.revocation,
            require_observation_attestation=True, require_recovery_evidence_for_reenable=True,
            require_controlled_snapshot_selection=True, snapshot_controller=self.controller)

    def read_context(self, **overrides):
        if hasattr(self, "control_checkpoint"):
            overrides = {"control_checkpoint": self.control_checkpoint, **overrides}
        return super().read_context(**overrides)

    def checkpoint(self):
        return sign_trusted_artifact(self.control_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
            payload=self.controller.checkpoint_payload(issued_at=self.clock().isoformat()))

    def publication_request(self, snapshot=None, **changes):
        self._request_count += 1
        payload = publication_request_payload(snapshot or self.snapshot, self.read_context(), publisher="synthetic-publisher",
            request_id=f"publication-{self._request_count}", issued_at=self.clock().isoformat(), reason="explicit synthetic publication")
        return sign_trusted_artifact(self.publisher_private, artifact_type=PUBLICATION_PURPOSE, payload={**payload, **changes})

    def selection_request(self, snapshot=None, operation="SELECT", **changes):
        self._request_count += 1
        payload = selection_request_payload(snapshot or self.snapshot, self.read_context(), self.controller.planning_state(),
            selector="synthetic-selector", request_id=f"selection-{self._request_count}", issued_at=self.clock().isoformat(),
            reason="explicit synthetic selection", operation=operation)
        return sign_trusted_artifact(self.selector_private, artifact_type=SELECTION_PURPOSE, payload={**payload, **changes})

    def publish(self, snapshot=None, request=None):
        snapshot = snapshot or self.snapshot
        result = self.controller.publish(snapshot, request=request or self.publication_request(snapshot),
            context=self.read_context(), checkpoint=self.control_checkpoint)
        if result["control_committed"]:
            self.control_checkpoint = self.checkpoint()
        return result

    def select(self, snapshot=None, request=None, operation="SELECT"):
        snapshot = snapshot or self.snapshot
        result = self.controller.select(snapshot.snapshot_id, request=request or self.selection_request(snapshot, operation),
            context=self.read_context(), checkpoint=self.control_checkpoint)
        if result["control_committed"]:
            self.control_checkpoint = self.checkpoint()
        return result

    def read(self, **overrides):
        return self.reader.read(observation=self.env.observation, snapshot=self.snapshot,
            read_context=self.read_context(**overrides), observation_attestation=self.observation_attestation)

    def successor(self, **kwargs):
        selected = self.registry.active_snapshot_id
        result = super().successor(**kwargs)
        self.registry._active_id = selected  # Preparation never constitutes controlled selection.
        return result

    def restart(self):
        self.registry = ShadowSnapshotRegistry()
        self.controller = self.new_controller()
        self.reader = self.new_reader()

    def recover(self):
        self.control_checkpoint = self.checkpoint()  # Explicit synthetic independent issuer.
        result = self.select(operation="RECOVER")
        require(result["control_committed"] and not self.reader.enabled, "reconciliation must preserve disable")
        audit_checkpoint = self.audit.export_checkpoint(self.checkpoint_private, issued_at=self.clock().isoformat())
        recovery = build_recovery_evidence(audit_store=self.audit, audit_checkpoint=audit_checkpoint,
            checkpoint_trusted_public_key_path=self.checkpoint_public, signing_private_key_path=self.recovery_private,
            current_time=self.clock().isoformat(), recovery_actor="synthetic-recovery",
            selected_snapshot_id=self.registry.active_snapshot_id, effective_snapshot_id=self.registry.active_snapshot_id,
            max_age_seconds=60)
        self.reader.re_enable(at=NOW, read_context=self.read_context(), evidence=self.evidence, recovery_evidence=recovery)
        return result


def run_acceptance(workspace):
    before = protected_store_hashes()
    scene = SyntheticControlScenario(workspace / "control")
    labels = {scene.snapshot.snapshot_id: "A", "": "NONE", None: "NONE"}
    steps = []
    def record(step, result, condition):
        require(condition, f"control invariant failed: {step}: {result}")
        state = scene.controller.planning_state()
        memory_selected = scene.registry.active_snapshot_id
        effective = memory_selected if memory_selected == state["selected"] else None
        checkpoint_verified = result.get("checkpoint_verified", False)
        if result.get("resolution_status") == "RESOLVED_SHADOW":
            proof = scene.controller.verify_current(scene.read_context(), scene.snapshot)
            checkpoint_verified = proof.checkpoint_fingerprint == scene.control_checkpoint.fingerprint()
        steps.append(dict(step=step, status=result.get("status", result.get("resolution_status", "VERIFIED")),
            rejection_codes=list(result.get("rejection_codes", ())), audit_committed=result.get("audit_committed", False),
            control_committed=result.get("control_committed", False),
            selected_snapshot=labels.get(effective, "UNKNOWN"),
            durably_selected_snapshot=labels.get(state["selected"], "UNKNOWN"),
            registry_snapshot=labels.get(memory_selected, "UNKNOWN"), reader_enabled=scene.reader.enabled,
            control_generation=state["generation"],
            checkpoint_verified=checkpoint_verified, production_activation="NOT_ENABLED"))
    result = scene.publish()
    record("publish_A_without_selection", result, result["control_committed"] and scene.registry.active_snapshot_id is None)
    result = scene.select()
    record("controlled_select_A", result, result["control_committed"] and result["audit_committed"])
    original = scene.read()
    record("strict_read_A", original.as_dict(), original.is_resolved)
    old_snapshot = scene.snapshot
    scene.successor()
    labels[scene.snapshot.snapshot_id] = "B"
    result = scene.publish()
    record("publish_B_preserves_A_pointer", result, result["control_committed"] and scene.registry.active_snapshot_id == old_snapshot.snapshot_id)
    result = scene.select()
    record("controlled_select_B", result, result["control_committed"] and scene.read().is_resolved and not scene.reader.is_generation_current(original))
    scene.registry.select(old_snapshot.snapshot_id)
    rejected = scene.reader.read(observation=scene.env.observation, snapshot=old_snapshot,
        read_context=scene.read_context(), observation_attestation=scene.observation_attestation)
    record("direct_registry_bypass_rejected", rejected.as_dict(), not rejected.is_resolved)
    scene.registry.select(scene.snapshot.snapshot_id)
    scene.restart()
    recovered = scene.controller.recover_read_only()
    record("restart_is_read_only", recovered, len(recovered["published"]) == 2 and not scene.reader.enabled and not scene.read().is_resolved)
    result = scene.recover()
    record("explicit_reconciliation_and_guarded_reenable", result, scene.read().is_resolved)
    original_append = scene.audit.append
    def uncertain(**kwargs):
        result = original_append(**kwargs)
        if kwargs["event_type"] == "SHADOW_SNAPSHOT_SWITCHED":
            raise OSError("synthetic durability acknowledgment loss")
        return result
    scene.audit.append = uncertain
    audit_bytes = Path(scene.audit.path).read_bytes()
    result = scene.select()
    record("uncertain_audit_commit_fails_closed", result, result["outcome"] == "UNCERTAIN" and not scene.reader.enabled
        and scene.registry.active_snapshot_id is None and Path(scene.audit.path).read_bytes().startswith(audit_bytes))
    require(before == protected_store_hashes(), "production stores changed")
    return dict(schema_version="1.0.0", success=True, synthetic_only=True, steps=steps,
        seven_store_sha256=before, seven_stores_unchanged=True, production_activation_ready=False, decision="NO-GO")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-snapshot-control-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = dict(success=False, error=str(exc), rejection_codes=getattr(exc, "rejection_codes", ()))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
