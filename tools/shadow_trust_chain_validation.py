"""Offline integration acceptance using the existing strict security engines.

Every key is synthetic and lives in an OS temporary workspace. No authority is
inferred from a successful read, a cache key, or an inspection report.
"""
from __future__ import annotations

from dataclasses import replace
import ast
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from tools.shadow_snapshot_control_validation import SyntheticControlScenario, inspect_restart_public
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes, require
from utils.mapping_governance import generate_mapping_owner_keypair, audit_historical_approvals, load_trusted_public_key
from utils.observation_authority import sign_observation_attestation
from utils.recovery_authority import build_recovery_evidence
from utils.runtime_activation_authorization import sign_runtime_activation_authorization, build_activation_authorization_body
from utils.shadow_runtime_reader import ShadowAuthorizationEvidence
from utils.trusted_artifacts import sign_trusted_artifact


def production_boundary_audit():
    """Read-only import closure audit; never import/start the Flask application."""
    from utils.shadow_runtime_reader import shadow_reader_status
    from utils.runtime_mapping_policy import PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    root = Path(__file__).resolve().parents[1]
    pending = [root / "app.py", root / "utils/scraper.py", root / "config.py"]
    visited, routes = set(), []
    forbidden = {"shadow_runtime_reader", "shadow_snapshot_control", "persistent_shadow_snapshots",
        "runtime_activation_authorization", "shadow_trust_chain_validation"}
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.append(node.module or "")
                modules.extend((node.module + "." if node.module else "") + alias.name for alias in node.names)
            for module in modules:
                require(not forbidden.intersection(module.split(".")), "production import reaches " + module)
                candidate = root.joinpath(*module.split(".")).with_suffix(".py")
                if candidate.is_file():
                    pending.append(candidate)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "route" and node.args and isinstance(node.args[0], ast.Constant)):
                routes.append(str(node.args[0].value))
    require(not any("activation" in route or "selection" in route for route in routes), "production control route")
    for path in (root / "frontend/src").rglob("*"):
        if path.suffix in (".ts", ".tsx"):
            content = path.read_text(encoding="utf-8-sig").lower()
            require("/api/activation" not in content and "/api/selection" not in content, "frontend activation control")
    for name in ("start.sh", "start.bat"):
        content = (root / name).read_text(encoding="utf-8-sig")
        require(not any(token in content for token in forbidden), "startup shadow entrypoint")
    flags = shadow_reader_status()
    require(flags["production_activation"] == flags["production_runtime_reader"] == "NOT_ENABLED"
        and PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED", "production flags changed")
    return dict(import_closure=sorted(str(p.relative_to(root)).replace("\\", "/") for p in visited),
        routes=sorted(routes), production_activation=flags["production_activation"],
        production_runtime_reader=flags["production_runtime_reader"],
        production_mapping_write_authorization=PRODUCTION_MAPPING_WRITE_AUTHORIZATION)


class IntegratedScenario(SyntheticControlScenario):
    """Real predecessor fixtures, with an independently keyed observation role."""

    def __init__(self, workspace):
        super().__init__(workspace)
        self.observation_private = str(workspace / "keys" / "observation-private.pem")
        self.observation_public = str(workspace / "keys" / "observation-public.pem")
        generate_mapping_owner_keypair(self.observation_private, self.observation_public)
        self.observation_attestation = sign_observation_attestation(self.observation_private,
            self.env.observation, dataset_identity=self.context.dataset_identity,
            adapter="market_xml", captured_at=NOW)

    def read_context(self, **changes):
        if hasattr(self, "observation_public"):
            changes = {"observation_trusted_public_key_path": self.observation_public, **changes}
        return super().read_context(**changes)

    def selected(self):
        require(self.publish()["control_committed"], "publication failed")
        require(not self.read().is_resolved and self.registry.active_snapshot_id is None,
            "publication conferred consumption authority")
        require(self.select()["control_committed"], "selection failed")
        result = self.read()
        require(result.is_resolved and result.is_shadow_only and not result.is_production_authorization,
            "complete strict resolution failed")
        require(result.candidate_canonical_id == self.env.proposal.candidate_canonical_id, "wrong Canonical target")
        return result

    def recovery_commitment(self):
        """Explicit signed reconciliation and fresh evidence; does not enable."""
        self.control_checkpoint = self.checkpoint()
        result = self.select(operation="RECOVER")
        require(result["control_committed"] and not self.reader.enabled, "recovery enabled consumption")
        checkpoint = self.audit.export_checkpoint(self.checkpoint_private, issued_at=NOW)
        recovery = build_recovery_evidence(audit_store=self.audit, audit_checkpoint=checkpoint,
            checkpoint_trusted_public_key_path=self.checkpoint_public,
            signing_private_key_path=self.recovery_private, current_time=NOW,
            recovery_actor="synthetic-recovery", selected_snapshot_id=self.registry.active_snapshot_id,
            effective_snapshot_id=self.registry.active_snapshot_id, max_age_seconds=60)
        return recovery

    def replace_authorization(self, **changes):
        """Re-sign the sole altered input; preserve all other original facts."""
        old, old_evidence = self.auth, self.evidence
        options = dict(dataset_identity=old.dataset_identity, authorized_scope=old.authorized_scope,
            authorizer=old.authorizer, operator=old.operator, authorized_at=old.authorized_at,
            expires_at=old.expires_at, revocation_reference=old.revocation_reference,
            approval_fingerprint=old.approval_fingerprint, reason=old.reason)
        self.auth = sign_runtime_activation_authorization(self.env.authorizer_private,
            build_activation_authorization_body(decision=self.decision, proposal=self.env.proposal,
                **{**options, **changes}))
        self.evidence = ShadowAuthorizationEvidence(self.auth, self.decision, self.env.proposal)
        transition = self.transition(old, self.auth, "altered-successor", 2)
        self.bundle = self.make_bundle(authorizations=(old_evidence, self.evidence), transitions=(transition,),
            current=(self.auth.activation_id,), sequence=2)
        self.revocations = self.make_revocations(sequence=2)


def resign(scene, artifact, private, **changes):
    return sign_trusted_artifact(private, artifact_type=artifact.artifact_type,
        payload={**artifact.payload(), **changes})


def capture(call):
    """Preserve implementation diagnostics, including boundary-specific codes."""
    try:
        result = call()
    except Exception as exc:
        codes = tuple(getattr(exc, "rejection_codes", ()))
        if not codes:
            raise
        return dict(status="REJECTED", rejection_codes=sorted(codes))
    if hasattr(result, "as_dict"):
        result = result.as_dict()
    return result


# These are exact existing contract codes, not a generic authorization failure.
SUBSTITUTIONS = {
    "wrong_observation_signer": "OBSERVATION_PROVENANCE_INVALID",
    "wrong_dataset": "TRUSTED_TIME_DATASET_MISMATCH",
    "wrong_source_scope": "OBSERVATION_PROVENANCE_INVALID",
    "wrong_baseline": "BASELINE_STALE",
    "unauthenticated_owner": "ACTIVATION_APPROVAL_NOT_AUTHENTICATED",
    "missing_current_approval": "ACTIVATION_APPROVAL_NOT_AUTHENTICATED",
    "wrong_approval_fingerprint": "ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH",
    "untrusted_activation_signer": "REVOCATION_ACTIVATION_SIGNER_MISMATCH",
    "stale_governance": "AUTHORITATIVE_GOVERNANCE_VIEW_INVALID",
    "expired_time": "TRUSTED_ARTIFACT_STALE",
    "excessive_lifetime": "ACTIVATION_LIFETIME_EXCEEDED",
    "old_lifecycle_checkpoint": "LIFECYCLE_CHECKPOINT_MISMATCH",
    "superseded_activation": "LIFECYCLE_SNAPSHOT_NOT_CURRENT",
    "revoked_activation": "ACTIVATION_AUTHORIZATION_REVOKED",
    "old_nonrevocation": "REVOCATION_STATE_ROLLBACK",
    "wrong_publication_signer": "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED",
    "wrong_selection_signer": "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED",
    "unsigned_publication": "CONTROL_COMMAND_MISSING",
    "unsigned_selection": "CONTROL_COMMAND_MISSING",
    "old_control_checkpoint": "CONTROL_CHECKPOINT_MISMATCH",
    "direct_registry_mutation": "CONTROL_REGISTRY_BYPASS",
    "wrong_selected_snapshot": "SNAPSHOT_NOT_SELECTED",
    "missing_recovery": "RE_ENABLE_RECOVERY_EVIDENCE_MISSING",
}


def substitution(scene, name):
    publication_cases = ("wrong_publication_signer", "unsigned_publication")
    selection_cases = ("wrong_selection_signer", "unsigned_selection")
    if name in selection_cases:
        require(scene.publish()["control_committed"], "valid publication prerequisite failed")
    elif name not in publication_cases:
        scene.selected()
    context = {}
    if name == "wrong_observation_signer":
        scene.observation_attestation = resign(scene, scene.observation_attestation,
            str(Path(scene.env.owner_public).with_name("owner_private.pem")))
    elif name == "wrong_dataset":
        context["dataset_identity"] = "synthetic-other-dataset"
    elif name == "wrong_source_scope":
        scene.observation_attestation = resign(scene, scene.observation_attestation,
            scene.observation_private, provider_id_uniqueness_scope="global")
    elif name == "wrong_baseline":
        context["current_baseline_fingerprint"] = "0" * 64
    elif name in ("unauthenticated_owner", "missing_current_approval"):
        view = scene.view
        if name == "unauthenticated_owner":
            view = replace(view, approved_records=(replace(view.approved_records[0], owner_public_key_id=""),))
        else:
            view = replace(view, approved_records=())
        scene.view = replace(view, attestation=sign_trusted_artifact(scene.governance_private,
            artifact_type=view.attestation.artifact_type, payload=view.attestation_payload()))
    elif name == "wrong_approval_fingerprint":
        scene.replace_authorization(approval_fingerprint="0" * 64)
    elif name == "untrusted_activation_signer":
        scene.auth = sign_runtime_activation_authorization(
            str(Path(scene.env.owner_public).with_name("owner_private.pem")), scene.auth.as_dict())
        scene.evidence = ShadowAuthorizationEvidence(scene.auth, scene.decision, scene.env.proposal)
        scene.bundle = scene.make_bundle(authorizations=(scene.evidence,), transitions=(),
            current=(scene.auth.activation_id,), sequence=2)
        scene.revocations = scene.make_revocations(sequence=1)
    elif name == "stale_governance":
        context["authoritative_view_max_age_seconds"] = 0.5
        scene.clock.advance(1)
    elif name == "expired_time":
        scene.clock.advance(61)
    elif name == "excessive_lifetime":
        scene.replace_authorization(expires_at="2030-01-17T00:00:00Z")
    elif name in ("old_lifecycle_checkpoint", "superseded_activation"):
        old, checkpoint = scene.snapshot, scene.bundle.checkpoint
        scene.successor()
        if name == "old_lifecycle_checkpoint":
            scene.bundle = replace(scene.bundle, checkpoint=checkpoint)
        # The old published selection must now reject the changed lifecycle.
        scene.snapshot = old
    elif name in ("revoked_activation", "old_nonrevocation"):
        old = scene.revocations
        scene.revocations = scene.make_revocations((scene.revocation_event(),), sequence=1)
        require(not scene.read().is_resolved, "revocation did not invalidate selection")
        if name == "old_nonrevocation":
            scene.revocations = old
    elif name in ("wrong_publication_signer", "unsigned_publication"):
        command = scene.publication_request()
        command = None if name.startswith("unsigned") else resign(scene, command, scene.selector_private)
        result = capture(lambda: scene.controller.publish(scene.snapshot, request=command,
            context=scene.read_context(), checkpoint=scene.control_checkpoint))
        require(not scene.read().is_resolved, "invalid publication allowed consumption")
        return result
    elif name in ("wrong_selection_signer", "unsigned_selection"):
        command = scene.selection_request()
        command = None if name.startswith("unsigned") else resign(scene, command, scene.publisher_private)
        result = capture(lambda: scene.controller.select(scene.snapshot.snapshot_id, request=command,
            context=scene.read_context(), checkpoint=scene.control_checkpoint))
        require(not scene.read().is_resolved, "invalid selection allowed consumption")
        return result
    elif name == "old_control_checkpoint":
        old = scene.control_checkpoint
        require(scene.select()["control_committed"], "selection advance failed")
        context["control_checkpoint"] = old
    elif name == "direct_registry_mutation":
        old = scene.snapshot
        scene.successor()
        # Deliberate hostile registry-only mutation, without signed selection.
        scene.registry.select(scene.snapshot.snapshot_id)
        require(scene.snapshot != old, "replacement did not change snapshot")
    elif name == "wrong_selected_snapshot":
        old = scene.snapshot
        scene.successor()
        require(scene.publish()["control_committed"] and scene.select()["control_committed"], "replacement failed")
        scene.snapshot = old
    elif name == "missing_recovery":
        scene.controller.disable(at=NOW)
        scene.recovery_commitment()
        return capture(lambda: scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence))
    else:
        raise ValueError(name)
    return capture(lambda: scene.read(**context))


def verify_substitution(scene, name):
    result = substitution(scene, name)
    codes = result.get("rejection_codes", ())
    require(SUBSTITUTIONS[name] in codes, f"{name}: expected {SUBSTITUTIONS[name]}, observed {codes}")
    require(result.get("resolution_status") != "RESOLVED_SHADOW", f"{name}: forged consumption")
    return result


def state_record(scene, scenario, invariant, result):
    inspection = scene.controller.recover_read_only()
    audit = scene.audit.verified_snapshot()
    journal = scene.controller.journal.records()
    persistent = inspection.get("selected")
    memory = scene.registry.active_snapshot_id
    return dict(scenario=scenario, expected_security_invariant=invariant,
        observed_status=result.get("status", result.get("resolution_status", "VERIFIED")),
        rejection_codes=sorted(result.get("rejection_codes", ())),
        verified_trust_domains=(["observation", "governance", "owner", "eligibility", "activation",
            "time", "lifecycle", "revocation", "publication", "selection", "control_checkpoint"]
            if result.get("resolution_status") == "RESOLVED_SHADOW" else []),
        audit_commitment_status="VERIFIED_PREFIX" if audit.records else "EMPTY",
        control_commitment_status=result.get("outcome", inspection["status"]),
        reader_enabled=scene.reader.enabled,
        snapshot_authority="AUTHENTICATED_SELECTION" if result.get("resolution_status") == "RESOLVED_SHADOW" else "NONE_FOR_THIS_OPERATION",
        registry_pointer_matches_persistent=memory == (persistent or None),
        audit_count=len(audit.records), control_count=len(journal),
        audit_tip_matches_last_record=audit.report["chain_tip"] == (audit.records[-1].entry_hash if audit.records else "0" * 64),
        recovery_required=bool(inspection.get("discrepancies")) or not scene.reader.enabled,
        production_activation_state="NOT_ENABLED")


FAULT_BOUNDARIES = ("governance", "publication", "intent", "audit", "commit", "pointer", "checkpoint", "recovery")


def run_fault(scene, boundary):
    """Record persisted evidence separately from acknowledgments at each boundary."""
    scene.selected()
    audit_before = Path(scene.audit.path).read_bytes()
    control_before = scene.controller.journal.path.read_bytes()
    ledger_before = Path(scene.ledger.path).read_bytes()
    counts = (len(scene.audit.records()), len(scene.controller.journal.records()))
    def fail(*args, **kwargs):
        raise OSError("synthetic commit boundary failure")
    exception_type = ""
    if boundary == "governance":
        with patch.object(scene.ledger, "_write_all", fail):
            try:
                scene.supersede()
            except OSError as exc:
                exception_type = type(exc).__name__
            else:
                raise RuntimeError("governance write fault did not execute")
        require(Path(scene.ledger.path).read_bytes() == ledger_before, "failed governance append changed ledger")
        result = dict(status="APPEND_REJECTED", outcome="NOT_COMMITTED", rejection_codes=[])
    elif boundary == "checkpoint":
        # The selection commits, but no post-commit proof is available to consume it.
        old = scene.control_checkpoint
        with patch.object(scene, "checkpoint", fail):
            try:
                scene.select()
            except OSError as exc:
                exception_type = type(exc).__name__
            else:
                raise RuntimeError("checkpoint fault did not execute")
        result = scene.read(control_checkpoint=old).as_dict()
        result["outcome"] = "COMMITTED"
        require("CONTROL_CHECKPOINT_MISMATCH" in result["rejection_codes"], "missing checkpoint allowed consumption")
    else:
        operation = scene.select
        if boundary == "publication":
            scene.successor()
            obj, field, replacement = scene.controller.store, "publish", fail
            operation = scene.publish
        elif boundary in ("intent", "commit"):
            obj, field = scene.controller.journal, "append"
            original = obj.append
            def replacement(**kwargs):
                if kwargs["event_type"].endswith("REQUESTED" if boundary == "intent" else "COMMITTED"):
                    fail()
                return original(**kwargs)
        elif boundary == "audit":
            obj, field = scene.audit, "append"
            original = obj.append
            def replacement(**kwargs):
                original(**kwargs)
                fail()
        elif boundary in ("pointer", "recovery"):
            obj, field, replacement = scene.controller, "_apply_pointer", fail
            if boundary == "recovery":
                scene.controller.disable(at=NOW)
                scene.control_checkpoint = scene.checkpoint()
                operation = lambda: scene.select(operation="RECOVER")
        else:
            raise ValueError(boundary)
        with patch.object(obj, field, replacement):
            result = operation()
        require(result["status"] == "RECOVERY_REQUIRED" and not scene.reader.enabled
            and scene.registry.active_snapshot_id is None and not scene.read().is_resolved,
            "failed commitment remained consumable")
    require(Path(scene.audit.path).read_bytes().startswith(audit_before)
        and scene.controller.journal.path.read_bytes().startswith(control_before), "fault rewrote history")
    report = state_record(scene, "fault_" + boundary, "Persisted evidence never substitutes for current consumption proof", result)
    inspected = scene.controller.recover_read_only()
    report.update(operation_acknowledged=False, exception_type=exception_type,
        audit_append_delta=report["audit_count"] - counts[0],
        control_append_delta=report["control_count"] - counts[1],
        audit_commit_acknowledged=result.get("audit_committed", False),
        control_commit_acknowledged=result.get("control_committed", False),
        original_audit_prefix_preserved=True, original_control_prefix_preserved=True,
        registry_pointer="CURRENT" if scene.registry.active_snapshot_id else "NONE",
        persistent_selected_pointer="CURRENT" if inspected.get("selected") == scene.snapshot.snapshot_id else "NONE_OR_PREVIOUS",
        audit_tip_reference="verified_audit_record[" + str(report["audit_count"] - 1) + "]",
        control_tip_reference="verified_control_record[" + str(report["control_count"] - 1) + "]")
    return report


def run_acceptance(workspace):
    before = protected_store_hashes()
    boundaries = production_boundary_audit()
    scene = IntegratedScenario(workspace / "lifecycle")
    public_roots = dict(owner=scene.env.owner_public, activation=scene.env.authorizer_public,
        governance=scene.governance_public, ledger_checkpoint=scene.checkpoint_public,
        time=scene.public, recovery=scene.recovery_public, lifecycle=scene.lifecycle_public,
        revocation=scene.revocation_public, revocation_checkpoint=scene.revocation_checkpoint_public,
        publisher=scene.publisher_public, selector=scene.selector_public,
        control_checkpoint=scene.control_checkpoint_public, observation=scene.observation_public)
    require(len({load_trusted_public_key(path)["public_key_id"] for path in public_roots.values()}) == len(public_roots),
        "synthetic signing roles share a key")
    steps = []
    def record(name, invariant, result):
        steps.append(state_record(scene, name, invariant, result))
    publication = scene.publish()
    require(publication["control_committed"] and not scene.read().is_resolved, "publication selected snapshot")
    record("publication_only", "Publication alone never authorizes consumption", publication)
    require(scene.select()["control_committed"], "selection failed")
    previous = scene.read()
    require(previous.is_resolved and previous.is_shadow_only and not previous.is_production_authorization,
        "strict positive path failed")
    require(previous.candidate_canonical_id == scene.env.proposal.candidate_canonical_id, "wrong target")
    record("strict_resolution", "Expected Canonical target resolves shadow-only", previous.as_dict())
    history = audit_historical_approvals(scene.ledger, trusted_public_key_path=scene.env.owner_public,
        dataset_identity=scene.context.dataset_identity)
    require(history["approved_decisions"] == 1, "historical owner approval not authenticated")
    scene.successor()
    require(scene.publish()["control_committed"] and scene.select()["control_committed"], "replacement failed")
    current = scene.read()
    require(current.is_resolved and not scene.reader.is_generation_current(previous), "stale handle survived replacement")
    require(scene.verified_revocations().state_sequence == 2, "current revocation verification missing")
    require(any(e.selection == "SUPERSEDED" for e in scene.verified().entries), "history not retained")
    record("explicit_replacement", "Only separately authorized successor is consumed", current.as_dict())
    disabled = scene.controller.disable(at=NOW)
    require(not scene.reader.enabled and scene.audit.records()[-1].event_type == "SHADOW_RUNTIME_DISABLED", "disable not durable")
    record("emergency_disable", "Disable precedes I/O and is durably audited", disabled)
    recovery = scene.recovery_commitment()
    require(not scene.read().is_resolved, "reconciliation enabled reader")
    record("signed_reconciliation", "Recovery commitment alone does not enable", dict(status="RECOVERY_VERIFIED"))
    scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=recovery)
    require(scene.read().is_resolved and not scene.reader.is_generation_current(current), "guarded recovery failed")
    record("guarded_reenable", "Current evidence and explicit re-enable required", scene.read().as_dict())
    original = scene.controller.journal.append
    def fail_commit(**kwargs):
        if kwargs["event_type"].endswith("COMMITTED"):
            raise OSError("synthetic control commitment failure")
        return original(**kwargs)
    audit_bytes, control_bytes = Path(scene.audit.path).read_bytes(), scene.controller.journal.path.read_bytes()
    with patch.object(scene.controller.journal, "append", fail_commit):
        result = scene.select()
    require(result["outcome"] == "UNCERTAIN" and not scene.reader.enabled and not scene.read().is_resolved,
        "uncertain commitment permitted consumption")
    require(Path(scene.audit.path).read_bytes().startswith(audit_bytes)
        and scene.controller.journal.path.read_bytes().startswith(control_bytes), "history was rewritten")
    record("control_commitment_failure", "Audit without control commitment is non-consumable", result)
    restarted = inspect_restart_public(workspace / "lifecycle")
    require(not restarted["reader_enabled"] and restarted["memory_selected"] is None, "restart auto-enabled")
    record("public_key_only_restart", "Restart reconstructs evidence read-only", restarted)
    for name in SUBSTITUTIONS:
        candidate = IntegratedScenario(workspace / name)
        result = verify_substitution(candidate, name)
        steps.append(state_record(candidate, name, "Altered trust boundary rejects the operation or consumption", result))
    race = IntegratedScenario(workspace / "disable-race")
    race.selected()
    race.controller.disable(at=NOW)
    evidence = race.recovery_commitment()
    original_check = race.reader._reenable_rejections
    def check_then_disable(**kwargs):
        codes = original_check(**kwargs)
        require(not codes, "race scenario must have fully valid recovery evidence")
        race.reader.disable(at=NOW, reason="newer emergency during pending re-enable")
        return codes
    with patch.object(race.reader, "_reenable_rejections", check_then_disable):
        result = capture(lambda: race.reader.re_enable(at=NOW, read_context=race.read_context(),
            evidence=race.evidence, recovery_evidence=evidence))
    require(result.get("rejection_codes") == ["RE_ENABLE_STATE_CHANGED"] and not race.reader.enabled
        and not race.read().is_resolved, "pending re-enable overwrote a newer emergency disable")
    steps.append(state_record(race, "newer_disable_wins", "A pending re-enable cannot overwrite newer disable", result))
    faults = [run_fault(IntegratedScenario(workspace / ("fault-" + boundary)), boundary) for boundary in FAULT_BOUNDARIES]
    require(before == protected_store_hashes(), "protected production bytes changed")
    return dict(schema_version="1.0.0", success=True, synthetic_only=True, steps=steps, commit_faults=faults,
        production_boundary_audit=boundaries,
        synthetic_signer_public_files={role: Path(path).name for role, path in public_roots.items()},
        synthetic_signer_keys_distinct=True,
        seven_store_sha256_before=before, seven_store_sha256_after=protected_store_hashes(),
        seven_stores_unchanged=True, production_activation="NOT_ENABLED",
        production_runtime_reader="NOT_ENABLED", production_mapping_write_authorization="NOT_GRANTED",
        production_activation_ready=False, decision="NO-GO")


def main():
    try:
        with tempfile.TemporaryDirectory(prefix="shadow-trust-chain-") as temp:
            result = run_acceptance(Path(temp))
    except Exception as exc:
        result = dict(success=False, synthetic_only=True, error=str(exc),
            rejection_codes=list(getattr(exc, "rejection_codes", ())), decision="NO-GO")
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
