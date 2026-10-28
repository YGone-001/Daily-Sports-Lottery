"""Cross-component security acceptance; remote platform evidence is recorded separately."""
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from tools.shadow_trust_chain_validation import (
    IntegratedScenario, SUBSTITUTIONS, capture, resign, run_acceptance, verify_substitution,
)
from tools.shadow_snapshot_control_validation import inspect_restart_public
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from tools.shadow_ledger_concurrency_baseline import paused_writer_read
from tools.shadow_ledger_concurrency_validation import process_lock_probe
from utils.authoritative_governance import build_authoritative_governance_view
from utils.mapping_governance import audit_historical_approvals
from utils.trusted_artifacts import sign_trusted_artifact


@pytest.fixture
def scene(tmp_path):
    before = protected_store_hashes()
    value = IntegratedScenario(tmp_path / "integrated")
    yield value
    assert protected_store_hashes() == before


def test_it01_complete_strict_chain(scene):
    result = scene.selected()
    record = scene.view.approved_records[0]
    assert record.approval_fingerprint == scene.decision.approval_fingerprint == scene.auth.approval_fingerprint
    assert record.approved_transition_identity == scene.ledger.entries()[-1].transition_id
    proof = scene.controller.verify_current(scene.read_context(), scene.snapshot)
    assert proof.checkpoint_fingerprint == scene.control_checkpoint.fingerprint()
    assert result.candidate_canonical_id == record.candidate_canonical_id


@pytest.mark.parametrize("name", SUBSTITUTIONS, ids=list(SUBSTITUTIONS))
def test_integrated_trust_substitution(scene, name):
    before = scene.registry.active_snapshot_id
    result = verify_substitution(scene, name)
    assert SUBSTITUTIONS[name] in result["rejection_codes"]
    # Without the authentic publication/selection link the chain cannot consume.
    if "signer" in name and ("publication" in name or "selection" in name) or name.startswith("unsigned"):
        assert before is None and scene.registry.active_snapshot_id is None
        assert not scene.read().is_resolved


def test_it16_publication_without_selection(scene):
    assert scene.publish()["control_committed"]
    assert scene.registry.active_snapshot_id is None
    assert "SNAPSHOT_NOT_SELECTED" in scene.read().rejection_codes


def test_expiry_during_same_reader_and_old_token_replay(scene):
    scene.replace_authorization(expires_at="2030-01-15T00:00:05Z")
    scene.snapshot = scene.snapshot_for((scene.auth,))
    result = scene.selected()
    token = scene.token
    scene.clock.advance(6)
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in scene.read(trusted_time_evidence=token).rejection_codes
    # Generation/caches are observability only; the old successful result is never consumed.
    assert result.is_resolved
    assert not scene.read().is_resolved


def test_current_governance_withdrawal_and_old_view_replay(scene):
    old = scene.selected()
    view = scene.view
    scene.supersede()
    assert not scene.read().is_resolved
    replay = scene.read(authoritative_view=view)
    assert "ACTIVATION_APPROVAL_NOT_CURRENT" in replay.rejection_codes
    # Equal cache keys can accompany a rejected replay: keys grant no authority.
    assert replay.cache_key == old.cache_key and not replay.is_resolved


def test_supersession_and_revocation_timeline(scene):
    first = scene.selected()
    original_bundle, original_revocations, original_snapshot = scene.bundle, scene.revocations, scene.snapshot
    scene.successor()
    assert scene.publish()["control_committed"] and scene.select()["control_committed"]
    successor = scene.read()
    assert successor.is_resolved and not scene.reader.is_generation_current(first)
    assert not scene.reader.read(observation=scene.env.observation, snapshot=original_snapshot,
        read_context=scene.read_context(), observation_attestation=scene.observation_attestation).is_resolved
    replay = scene.read(lifecycle_evidence=original_bundle, revocation_evidence=original_revocations,
        evidence=original_bundle.authorizations)
    assert "REVOCATION_STATE_ROLLBACK" in replay.rejection_codes
    empty = scene.revocations
    scene.revocations = scene.make_revocations((scene.revocation_event(sequence=3),), sequence=3)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read().rejection_codes
    assert not scene.reader.is_generation_current(successor)
    assert "REVOCATION_STATE_ROLLBACK" in scene.read(revocation_evidence=empty).rejection_codes


def append_audit(scene):
    return scene.audit.append(event_type="SHADOW_RUNTIME_DISABLED", occurred_at=NOW,
        actor="synthetic-independent-writer", actor_role="operator", dataset_identity=scene.context.dataset_identity,
        payload={"reason": "concurrent independent audit change"})


def test_it19_audit_advance_after_success_invalidates_control(scene):
    previous = scene.selected()
    append_audit(scene)
    result = scene.read()
    assert not result.is_resolved and "CONTROL_CHECKPOINT_MISMATCH" in result.rejection_codes
    assert previous.is_resolved


@pytest.mark.parametrize("mutation", ["selection", "disable", "revocation", "checkpoint"])
def test_it20_interleaved_mutation_at_final_read_boundary(scene, monkeypatch, mutation):
    scene.selected()
    ready, done = threading.Event(), threading.Event()
    original = scene.reader._verify_current_lifecycle
    def fence(context, snapshot):
        result = original(context, snapshot)
        ready.set()
        assert done.wait(10)
        return result
    monkeypatch.setattr(scene.reader, "_verify_current_lifecycle", fence)
    def writer():
        assert ready.wait(10)
        try:
            if mutation == "selection":
                assert scene.select()["control_committed"]
            elif mutation == "disable":
                scene.controller.disable(at=NOW)
            elif mutation == "revocation":
                scene.revocations = scene.make_revocations((scene.revocation_event(),), sequence=1)
                scene.verified_revocations()
            else:
                append_audit(scene)
                scene.control_checkpoint = scene.checkpoint()
        finally:
            done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        write = pool.submit(writer)
        read = pool.submit(scene.read)
        result = read.result(timeout=20)
        write.result(timeout=20)
    assert not result.is_resolved, (mutation, result.as_dict())


def test_it21_disable_survives_audit_failure(scene, monkeypatch):
    previous = scene.selected()
    def fail(**kwargs):
        raise OSError("synthetic audit service unavailable")
    monkeypatch.setattr(scene.audit, "append", fail)
    result = scene.controller.disable(at=NOW)
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None
    assert not result["audit_committed"] and not scene.reader.is_generation_current(previous)
    assert not scene.read().is_resolved


def test_it22_guarded_recovery_full_cycle(scene):
    historical = scene.selected()
    disabled = scene.controller.disable(at=NOW)
    assert disabled["audit_committed"] and scene.audit.records()[-1].event_type == "SHADOW_RUNTIME_DISABLED"
    evidence = scene.recovery_commitment()
    assert not scene.reader.enabled and not scene.read().is_resolved
    resumed = scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence,
        recovery_evidence=evidence)
    assert resumed["enabled"] and scene.read().is_resolved
    assert not scene.reader.is_generation_current(historical)


@pytest.mark.parametrize("controller_disable", [False, True])
def test_newer_emergency_disable_wins_over_inflight_reenable(scene, monkeypatch, controller_disable):
    scene.selected()
    scene.controller.disable(at=NOW)
    evidence = scene.recovery_commitment()
    checked, release = threading.Event(), threading.Event()
    original = scene.reader._reenable_rejections
    def verified_then_pause(**kwargs):
        codes = original(**kwargs)
        assert not codes
        checked.set()
        assert release.wait(10)
        return codes
    monkeypatch.setattr(scene.reader, "_reenable_rejections", verified_then_pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(lambda: capture(lambda: scene.reader.re_enable(at=NOW,
            read_context=scene.read_context(), evidence=scene.evidence, recovery_evidence=evidence)))
        assert checked.wait(10)
        (scene.controller if controller_disable else scene.reader).disable(at=NOW)
        disabled_generation = scene.reader.generation
        release.set()
        result = pending.result(timeout=15)
    assert result.get("rejection_codes") == ["RE_ENABLE_STATE_CHANGED"], result
    assert not scene.reader.enabled and scene.reader.generation == disabled_generation
    assert "READER_DISABLED" in scene.read().rejection_codes


@pytest.mark.parametrize("change,expected", [
    ("cross_store", "RE_ENABLE_RECOVERY_STATE_MISMATCH"),
    ("pointer", "RE_ENABLE_RECOVERY_STATE_MISMATCH"),
    ("audit_advance", "RE_ENABLE_RECOVERY_STATE_MISMATCH"),
    ("stale", "RE_ENABLE_RECOVERY_EVIDENCE_INVALID"),
    ("superseded", "LIFECYCLE_SNAPSHOT_NOT_CURRENT"),
    ("revoked", "ACTIVATION_AUTHORIZATION_REVOKED"),
    ("checkpoint", "CONTROL_CHECKPOINT_MISMATCH"),
])
def test_it23_it26_guarded_recovery_boundary(scene, change, expected):
    scene.selected()
    scene.controller.disable(at=NOW)
    evidence = scene.recovery_commitment()
    if change in ("cross_store", "pointer"):
        # Build internally self-consistent signed evidence for the wrong state.
        from utils.recovery_authority import recovery_state_fingerprint
        payload = evidence.payload()
        payload["store_identity" if change == "cross_store" else "effective_snapshot_id"] = "another-isolated-state"
        payload["recovery_generation"] = recovery_state_fingerprint(payload)
        evidence = sign_trusted_artifact(scene.recovery_private, artifact_type=evidence.artifact_type, payload=payload)
    elif change == "audit_advance":
        append_audit(scene)
    elif change == "stale":
        payload = evidence.payload()
        evidence = resign(scene, evidence, scene.recovery_private, issued_at="2030-01-14T22:59:59Z")
    elif change == "superseded":
        old = scene.snapshot
        scene.successor()
        scene.snapshot = old
    elif change == "revoked":
        scene.revocations = scene.make_revocations((scene.revocation_event(),), sequence=1)
    elif change == "checkpoint":
        scene.control_checkpoint = resign(scene, scene.control_checkpoint, scene.control_checkpoint_private,
            control_generation=999)
    result = capture(lambda: scene.reader.re_enable(at=NOW, read_context=scene.read_context(),
        evidence=scene.evidence, recovery_evidence=evidence))
    assert expected in result["rejection_codes"], result
    assert not scene.reader.enabled and not scene.read().is_resolved


@pytest.mark.parametrize("boundary", ["publication", "intent", "audit", "commit", "pointer", "checkpoint", "recovery"])
def test_it27_it28_commit_failure_preserves_history(scene, monkeypatch, boundary):
    scene.selected()
    audit_bytes = Path(scene.audit.path).read_bytes()
    journal_bytes = scene.controller.journal.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic injected commit interruption")
    operation = scene.select
    if boundary == "publication":
        scene.successor()
        monkeypatch.setattr(scene.controller.store, "publish", fail)
        operation = scene.publish
    elif boundary in ("intent", "commit"):
        original = scene.controller.journal.append
        def append(**kwargs):
            if kwargs["event_type"].endswith("REQUESTED" if boundary == "intent" else "COMMITTED"):
                fail()
            return original(**kwargs)
        monkeypatch.setattr(scene.controller.journal, "append", append)
    elif boundary == "audit":
        original = scene.audit.append
        def append(**kwargs):
            original(**kwargs)
            fail()
        monkeypatch.setattr(scene.audit, "append", append)
    elif boundary == "pointer":
        monkeypatch.setattr(scene.controller, "_apply_pointer", fail)
    elif boundary == "checkpoint":
        old = scene.control_checkpoint
        assert scene.select()["control_committed"]
        # A failed independent issuer cannot be replaced by the precommit proof.
        result = scene.read(control_checkpoint=old)
        assert not result.is_resolved and "CONTROL_CHECKPOINT_MISMATCH" in result.rejection_codes
        return
    else:
        scene.controller.disable(at=NOW)
        scene.control_checkpoint = scene.checkpoint()
        monkeypatch.setattr(scene.controller, "_apply_pointer", fail)
        operation = lambda: scene.select(operation="RECOVER")
    result = operation()
    assert result["status"] == "RECOVERY_REQUIRED"
    assert result["outcome"] in ("UNCERTAIN", "NOT_COMMITTED")
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None
    assert not scene.read().is_resolved
    assert Path(scene.audit.path).read_bytes().startswith(audit_bytes)
    assert scene.controller.journal.path.read_bytes().startswith(journal_bytes)


def test_governance_append_failure_cannot_derive_current_view(scene, monkeypatch):
    scene.selected()
    before = Path(scene.ledger.path).read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic append interruption")
    monkeypatch.setattr(scene.ledger, "append", fail)
    with pytest.raises(OSError):
        scene.supersede()
    assert Path(scene.ledger.path).read_bytes() == before
    # No withdrawal was acknowledged; original authenticated view is still valid.
    assert scene.read().is_resolved


def test_it29_public_key_restart_never_auto_enables(scene):
    scene.selected()
    result = inspect_restart_public(Path(scene.control_workspace).parent)
    assert not result["reader_enabled"] and result["memory_selected"] is None
    assert not result["consumption_enabled"]


@pytest.mark.parametrize("domain", ["time", "governance", "lifecycle", "revocation", "control"])
def test_it30_lost_continuity_closes_active_session(scene, domain):
    scene.selected()
    obj, field = {"time": (scene.time, "_state"), "governance": (scene.strict, "_approval_view_state"),
        "lifecycle": (scene.lifecycle, "_previous"), "revocation": (scene.revocation, "_state"),
        "control": (scene.controller, "_observed")}[domain]
    object.__setattr__(obj, field, None)
    assert not scene.read().is_resolved


@pytest.mark.parametrize("governance", [True, False], ids=["IT-31-governance", "IT-32-audit"])
def test_it31_it32_cooperating_process_snapshot(scene, governance):
    result = paused_writer_read(scene, governance=governance)
    assert result["blocked_until_release"] and result["status"] == "VERIFIED"


def test_it33_platform_os_lock_and_release(tmp_path):
    result = process_lock_probe(tmp_path)
    assert result["outcome"]["status"] == "TIMEOUT" and result["reacquired"]
    assert result["outcome"]["diagnostics"]["platform"] == os.name


def test_two_competing_signed_recoveries_have_one_commit(scene):
    scene.selected()
    scene.controller.disable(at=NOW)
    scene.control_checkpoint = scene.checkpoint()
    requests = [scene.selection_request(operation="RECOVER") for _ in range(2)]
    context, checkpoint = scene.read_context(), scene.control_checkpoint
    start = threading.Barrier(2)
    def recover(command):
        start.wait(timeout=5)
        return capture(lambda: scene.controller.select(scene.snapshot.snapshot_id, request=command,
            context=context, checkpoint=checkpoint))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recover, requests, timeout=20))
    assert sum(r.get("control_committed", False) for r in results) == 1
    rejected = next(r for r in results if not r.get("control_committed"))
    assert "CONTROL_CHECKPOINT_MISMATCH" in rejected["rejection_codes"]
    assert not scene.reader.enabled and not scene.read().is_resolved


def test_signed_cas_conflict_is_durable_and_requires_new_checkpoint(scene):
    scene.selected()
    command = scene.selection_request(expected_generation=999)
    result = capture(lambda: scene.controller.select(scene.snapshot.snapshot_id, request=command,
        context=scene.read_context(), checkpoint=scene.control_checkpoint))
    assert result["rejection_codes"] == ["CONTROL_COMPARE_AND_SWAP_FAILED"]
    assert scene.controller.journal.records()[-1]["event_type"] == "SNAPSHOT_SELECTION_REJECTED"
    assert "CONTROL_CHECKPOINT_MISMATCH" in scene.read().rejection_codes
    scene.control_checkpoint = scene.checkpoint()
    assert scene.read().is_resolved


@pytest.mark.parametrize("domain", ["observation", "governance", "time", "lifecycle", "revocation", "control", "recovery"])
def test_other_role_signature_cannot_satisfy_trust_domain(scene, domain):
    scene.selected()
    if domain == "observation":
        scene.observation_attestation = resign(scene, scene.observation_attestation, scene.selector_private)
    elif domain == "governance":
        scene.view = replace(scene.view, attestation=resign(scene, scene.view.attestation, scene.lifecycle_private))
    elif domain == "time":
        scene.token = resign(scene, scene.token, scene.governance_private)
    elif domain == "lifecycle":
        scene.bundle = replace(scene.bundle, checkpoint=resign(scene, scene.bundle.checkpoint, scene.revocation_checkpoint_private))
    elif domain == "revocation":
        scene.revocations = replace(scene.revocations, state=resign(scene, scene.revocations.state, scene.lifecycle_private))
    elif domain == "control":
        scene.control_checkpoint = resign(scene, scene.control_checkpoint, scene.publisher_private)
    else:
        scene.controller.disable(at=NOW)
        evidence = scene.recovery_commitment()
        evidence = resign(scene, evidence, scene.selector_private)
        result = capture(lambda: scene.reader.re_enable(at=NOW, read_context=scene.read_context(),
            evidence=scene.evidence, recovery_evidence=evidence))
        assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in result["rejection_codes"]
        assert not scene.reader.enabled
        return
    assert not scene.read().is_resolved


def test_it34_historical_signatures_stay_auditable_after_replacement(scene):
    scene.selected()
    original = Path(scene.ledger.path).read_bytes()
    scene.successor()
    assert scene.publish()["control_committed"] and scene.select()["control_committed"]
    assert scene.read().is_resolved
    audit = audit_historical_approvals(scene.ledger, trusted_public_key_path=scene.env.owner_public,
        dataset_identity=scene.context.dataset_identity)
    assert audit["approved_decisions"] == 1
    assert Path(scene.ledger.path).read_bytes() == original
    assert any(e.selection == "SUPERSEDED" and e.cryptographically_valid for e in scene.verified().entries)


def test_it35_it36_production_entrypoint_boundaries():
    from utils.shadow_runtime_reader import shadow_reader_status
    from utils.runtime_mapping_policy import PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    status = shadow_reader_status()
    assert status["production_runtime_reader"] == status["production_activation"] == "NOT_ENABLED"
    assert PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"
    root = Path(__file__).resolve().parents[1]
    forbidden = ("shadow_runtime_reader", "shadow_snapshot_control", "persistent_shadow_snapshots",
        "runtime_activation_authorization", "shadow_trust_chain_validation")
    for path in (root / "app.py", root / "utils/scraper.py", root / "utils/ingestion_identity.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(token in module for module in imports for token in forbidden)
        routes = [node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "route" and node.args
            and isinstance(node.args[0], ast.Constant)]
        assert not any("activation" in route or "selection" in route for route in routes)
    for path in (root / "frontend/src").rglob("*"):
        if path.suffix in (".ts", ".tsx"):
            text = path.read_text(encoding="utf-8-sig").lower()
            assert "/api/activation" not in text and "/api/selection" not in text


def test_it37_it39_functional_demo_and_seven_store_bytes(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path / "demo")
    assert result["success"] and result["seven_stores_unchanged"]
    assert before == result["seven_store_sha256_before"] == result["seven_store_sha256_after"] == protected_store_hashes()
    names = {s["scenario"] for s in result["steps"]}
    assert set(SUBSTITUTIONS) <= names
    assert {"strict_resolution", "explicit_replacement", "guarded_reenable", "control_commitment_failure"} <= names
    assert result["decision"] == "NO-GO" and not result["production_activation_ready"]


@pytest.mark.parametrize("boundary", ["intent", "audit", "commit", "pointer", "recovery"])
def test_real_process_crash_reconstructs_read_only(tmp_path, boundary):
    root = tmp_path / "process-scene"
    script = '''
import os, sys
from pathlib import Path
from tools.shadow_trust_chain_validation import IntegratedScenario
s = IntegratedScenario(Path(sys.argv[1]))
s.selected()
boundary = sys.argv[2]
if boundary == "recovery":
    s.controller.disable(at="2030-01-15T00:00:00+00:00")
    s.control_checkpoint = s.checkpoint()
obj, field = ((s.controller.journal, "append") if boundary in ("intent", "commit") else
              (s.audit, "append") if boundary == "audit" else (s.controller, "_apply_pointer"))
original = getattr(obj, field)
def crash(*args, **kwargs):
    result = original(*args, **kwargs)
    if boundary not in ("intent", "commit") or kwargs["event_type"].endswith("REQUESTED" if boundary == "intent" else "COMMITTED"):
        os._exit(73)
    return result
setattr(obj, field, crash)
s.select(operation="RECOVER" if boundary == "recovery" else "SELECT")
os._exit(74)
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(root), boundary], capture_output=True, text=True, timeout=40)
    assert crashed.returncode == 73, crashed.stderr
    report = inspect_restart_public(root)
    assert not report["reader_enabled"] and report["memory_selected"] is None
    assert not report["consumption_enabled"]
