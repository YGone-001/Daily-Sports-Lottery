"""PC-01..PC-60：持久控制、角色分离、精确审计、故障与恢复。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools.shadow_snapshot_control_validation import SyntheticControlScenario, run_acceptance
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.persistent_shadow_snapshots import (
    BoundedShadowLock, PersistentShadowSnapshotStore, ShadowControlError, decode_snapshot, strict_json_file,
)
from utils.shadow_snapshot_control import PUBLICATION_PURPOSE, SELECTION_PURPOSE, CHECKPOINT_PURPOSE, PersistentShadowSnapshotController
from utils.shadow_activation_audit_store import ShadowAuditCommitReceipt
from utils.shadow_runtime_reader import ShadowRuntimeReader, ShadowReenablementError
from utils.source_identity import canonical_json, fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError, sign_trusted_artifact


@pytest.fixture
def scene(tmp_path):
    return SyntheticControlScenario(tmp_path / "scene")


def rejected(call, code=None):
    with pytest.raises(Exception) as exc:
        call()
    if code:
        assert code in getattr(exc.value, "rejection_codes", ()), str(exc.value)
    return exc.value


def selected(scene):
    assert scene.publish()["control_committed"]
    assert scene.select()["control_committed"]
    assert scene.read().is_resolved


def alternate(scene):
    snapshot = replace(scene.snapshot, generated_at="2030-01-15T00:00:00Z")
    return replace(snapshot, snapshot_id=snapshot.content_fingerprint(), snapshot_fingerprint=snapshot.content_fingerprint())


def sign_pub(scene, payload, key=None, purpose=PUBLICATION_PURPOSE):
    return sign_trusted_artifact(key or scene.publisher_private, artifact_type=purpose, payload=payload)


def sign_sel(scene, payload, key=None, purpose=SELECTION_PURPOSE):
    return sign_trusted_artifact(key or scene.selector_private, artifact_type=purpose, payload=payload)


def publish_call(scene, request, snapshot=None):
    return scene.controller.publish(snapshot or scene.snapshot, request=request, context=scene.read_context(), checkpoint=scene.control_checkpoint)


def select_call(scene, request, snapshot=None, checkpoint=None):
    return scene.controller.select((snapshot or scene.snapshot).snapshot_id, request=request,
        context=scene.read_context(), checkpoint=checkpoint or scene.control_checkpoint)


def cp_with(scene, **changes):
    return sign_trusted_artifact(scene.control_checkpoint_private, artifact_type=CHECKPOINT_PURPOSE,
        payload={**scene.control_checkpoint.payload(), **changes})


def test_pc01_persistent_immutable_roundtrip(scene):
    result = scene.publish()
    assert result["control_committed"] and scene.controller.store.get(scene.snapshot.snapshot_id) == scene.snapshot
    original = scene.controller.store._path(scene.snapshot.snapshot_id).read_bytes()
    assert scene.controller.store.verify_store()
    assert scene.controller.store._path(scene.snapshot.snapshot_id).read_bytes() == original


def test_pc02_publication_does_not_select(scene):
    scene.publish()
    assert scene.registry.active_snapshot_id is None
    assert not scene.read().is_resolved


def test_pc03_unsigned_publication_rejects(scene):
    rejected(lambda: publish_call(scene, None), "CONTROL_COMMAND_MISSING")
    assert scene.controller.store.list_snapshot_ids() == ()


def test_pc04_wrong_publisher(scene):
    request = sign_pub(scene, scene.publication_request().payload(), key=scene.selector_private)
    rejected(lambda: publish_call(scene, request), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")


def test_pc05_wrong_publisher_purpose(scene):
    request = sign_pub(scene, scene.publication_request().payload(), purpose=SELECTION_PURPOSE)
    rejected(lambda: publish_call(scene, request), "TRUSTED_ARTIFACT_TYPE_MISMATCH")


@pytest.mark.parametrize("field,value,code", [("dataset_identity", "other", "CONTROL_COMMAND_INVALID"),
    ("baseline_fingerprint", "other", "CONTROL_COMMAND_BINDING_MISMATCH")], ids=["PC-06", "PC-07"])
def test_pc06_pc07_scope_binding(scene, field, value, code):
    rejected(lambda: publish_call(scene, scene.publication_request(**{field: value})), code)


def test_pc08_snapshot_fingerprint_mismatch(scene):
    malformed = replace(scene.snapshot, snapshot_fingerprint="0" * 64)
    rejected(lambda: scene.publish(malformed))
    assert not scene.controller.store.list_snapshot_ids()


def test_pc09_same_id_changed_content(scene):
    scene.publish()
    changed = replace(scene.snapshot, generated_at="2030-01-15T00:00:01Z")
    rejected(lambda: scene.publish(changed))
    assert scene.controller.store.get(scene.snapshot.snapshot_id) == scene.snapshot


def test_pc10_identical_publication_request_is_idempotent(scene):
    command = scene.publication_request()
    scene.publish(request=command)
    before = scene.controller.journal.path.read_bytes(), Path(scene.audit.path).read_bytes()
    result = scene.publish(request=command)
    assert result["status"] == "IDEMPOTENT"
    assert before == (scene.controller.journal.path.read_bytes(), Path(scene.audit.path).read_bytes())


def test_pc11_unsigned_selection_rejects(scene):
    scene.publish()
    rejected(lambda: select_call(scene, None), "CONTROL_COMMAND_MISSING")


@pytest.mark.parametrize("role", ["time", "publisher", "owner", "activation"], ids=["PC-12", "PC-13", "PC-14", "PC-15"])
def test_pc12_to_pc15_selection_role_substitution(scene, role):
    scene.publish()
    keys = dict(time=scene.private, publisher=scene.publisher_private,
        owner=str(Path(scene.env.owner_public).with_name("owner_private.pem")), activation=scene.env.authorizer_private)
    command = sign_sel(scene, scene.selection_request().payload(), key=keys[role])
    rejected(lambda: select_call(scene, command), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")
    assert scene.registry.active_snapshot_id is None


def test_pc16_selection_before_publication(scene):
    rejected(lambda: select_call(scene, scene.selection_request()), "CONTROL_SNAPSHOT_NOT_PUBLISHED")


@pytest.mark.parametrize("field,value", [("expected_previous", "other"), ("expected_generation", 99)], ids=["PC-17", "PC-18"])
def test_pc17_pc18_compare_and_swap(scene, field, value):
    scene.publish()
    rejected(lambda: select_call(scene, scene.selection_request(**{field: value})), "CONTROL_COMPARE_AND_SWAP_FAILED")
    assert scene.registry.active_snapshot_id is None


def test_pc19_conflicting_request_id(scene):
    command = scene.publication_request()
    scene.publish(request=command)
    other = sign_pub(scene, {**command.payload(), "reason": "different signed content"})
    rejected(lambda: publish_call(scene, other), "CONTROL_REQUEST_ID_CONFLICT")


def test_pc20_valid_controlled_selection(scene):
    selected(scene)
    assert scene.reader.status()["require_controlled_snapshot_selection"]


def test_pc21_direct_registry_bypass_rejected(scene):
    selected(scene)
    B = alternate(scene)
    scene.publish(B)
    scene.registry.select(B.snapshot_id)
    result = scene.reader.read(observation=scene.env.observation, snapshot=B, read_context=scene.read_context(),
        observation_attestation=scene.observation_attestation)
    assert "CONTROL_REGISTRY_BYPASS" in result.rejection_codes


def test_pc22_only_authenticated_selected_snapshot_consumed(scene):
    selected(scene)
    B = alternate(scene)
    scene.publish(B)
    assert scene.registry.active_snapshot_id == scene.snapshot.snapshot_id
    result = scene.reader.read(observation=scene.env.observation, snapshot=B, read_context=scene.read_context(),
        observation_attestation=scene.observation_attestation)
    assert "SNAPSHOT_NOT_SELECTED" in result.rejection_codes


def test_pc23_exact_selection_audit_persisted(scene):
    scene.publish()
    command = scene.selection_request()
    scene.select(request=command)
    audit = scene.audit.records()[-1]
    assert audit.event_type == "SHADOW_SNAPSHOT_SELECTED" and audit.snapshot_id == scene.snapshot.snapshot_id
    assert dict(audit.payload)["command_fingerprint"] == command.fingerprint()
    assert scene.controller.journal.records()[-1]["audit_event_id"] == audit.event_id


def test_pc24_missing_audit_store_rejects(scene):
    rejected(lambda: PersistentShadowSnapshotController(str(scene.control_workspace), dataset_identity=scene.context.dataset_identity,
        registry=scene.registry, audit_store=None, publisher_public_key_path=scene.publisher_public,
        selector_public_key_path=scene.selector_public, checkpoint_public_key_path=scene.control_checkpoint_public,
        lifecycle_verifier=scene.lifecycle, revocation_verifier=scene.revocation))


def test_pc25_invalid_durable_receipt(scene, monkeypatch):
    scene.publish()
    monkeypatch.setattr(scene.audit, "receipt_for", lambda record: None)
    result = scene.select()
    assert result["outcome"] == "UNCERTAIN" and not scene.reader.enabled and scene.registry.active_snapshot_id is None


def test_pc26_post_write_acknowledgment_loss(scene, monkeypatch):
    scene.publish()
    original = scene.audit.append
    def append(**kwargs):
        original(**kwargs)
        raise OSError("synthetic post-write failure")
    monkeypatch.setattr(scene.audit, "append", append)
    result = scene.select()
    assert result["outcome"] == "UNCERTAIN" and not result["audit_committed"]
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None
    assert scene.audit.records()[-1].event_type == "SHADOW_SNAPSHOT_SELECTED"


def test_pc27_control_journal_corruption(scene):
    selected(scene)
    path = scene.controller.journal.path
    path.write_bytes(path.read_bytes() + b'{"torn":')
    assert scene.controller.recover_read_only()["status"] == "CORRUPT"
    assert not scene.read().is_resolved


def test_pc28_valid_chain_tail_truncation_against_checkpoint(scene):
    selected(scene)
    old = scene.control_checkpoint
    lines = scene.controller.journal.path.read_bytes().splitlines(keepends=True)
    scene.controller.journal.path.write_bytes(b"".join(lines[:-1]))
    assert not scene.read(control_checkpoint=old).is_resolved


def test_pc29_history_replacement_against_checkpoint(scene):
    selected(scene)
    old = scene.control_checkpoint
    scene.controller.journal.path.write_bytes(b"")
    assert not scene.read(control_checkpoint=old).is_resolved


def test_pc30_wrong_checkpoint_signer(scene):
    selected(scene)
    cp = sign_trusted_artifact(scene.selector_private, artifact_type=CHECKPOINT_PURPOSE, payload=scene.control_checkpoint.payload())
    assert not scene.read(control_checkpoint=cp).is_resolved


@pytest.mark.parametrize("field,value", [("control_chain_tip", "0" * 64), ("control_generation", 99)], ids=["PC-31", "PC-32"])
def test_pc31_pc32_checkpoint_state_binding(scene, field, value):
    selected(scene)
    result = scene.read(control_checkpoint=cp_with(scene, **{field: value}))
    assert "CONTROL_CHECKPOINT_MISMATCH" in result.rejection_codes


def test_pc33_stale_checkpoint(scene):
    selected(scene)
    scene.clock.advance(61)
    scene.token = scene.attestation(generation=11)
    assert "TRUSTED_ARTIFACT_STALE" in scene.read().rejection_codes


def test_pc34_checkpoint_replay_after_selection(scene):
    scene.publish()
    old = scene.control_checkpoint
    scene.select()
    assert not scene.read(control_checkpoint=old).is_resolved


def test_pc35_restart_history_read_only(scene):
    selected(scene)
    scene.restart()
    report = scene.controller.recover_read_only()
    assert report["selected"] == scene.snapshot.snapshot_id and report["published"] == (scene.snapshot.snapshot_id,)
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None


def test_pc36_restart_requires_fresh_checkpoint(scene):
    selected(scene)
    scene.restart()
    rejected(lambda: select_call(scene, scene.selection_request(operation="RECOVER")), "CONTROL_CHECKPOINT_MISMATCH")
    assert not scene.reader.enabled


def test_pc37_restart_missing_audit_fails_closed(scene):
    selected(scene)
    Path(scene.audit.path).write_bytes(b"")
    rejected(scene.restart)
    assert not scene.controller.generation_current(2)


def test_pc38_commit_without_pointer_is_reported(scene):
    selected(scene)
    scene.controller.pointer_path.unlink()
    report = scene.controller.recover_read_only()
    assert "COMMIT_WITHOUT_POINTER" in report["discrepancies"] and not scene.read().is_resolved


def test_pc39_pointer_without_event_is_reported(scene):
    selected(scene)
    payload = strict_json_file(scene.control_workspace, scene.controller.pointer_path)
    payload["selected"] = "f" * 64
    scene.controller.pointer_path.write_text(canonical_json(payload)+"\n", encoding="utf-8")
    assert "POINTER_WITHOUT_MATCHING_COMMIT" in scene.controller.recover_read_only()["discrepancies"]
    assert not scene.read().is_resolved


def test_pc40_recovery_explicit_authorization_and_guarded_reenable(scene):
    selected(scene)
    scene.restart()
    rejected(lambda: scene.reader.re_enable(at=NOW, read_context=scene.read_context(), evidence=scene.evidence))
    result = scene.recover()
    assert result["status"] == "RECOVERY_VERIFIED" and scene.read().is_resolved


def test_pc41_revoked_activation_rejects(scene):
    selected(scene)
    event = scene.revocation_event()
    scene.revocations = scene.make_revocations((event,), sequence=1)
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in scene.read().rejection_codes


def test_pc42_superseded_activation_rejects(scene):
    selected(scene)
    old = scene.snapshot
    scene.successor()
    result = scene.reader.read(observation=scene.env.observation, snapshot=old,
        read_context=scene.read_context(), observation_attestation=scene.observation_attestation)
    assert not result.is_resolved


def test_pc43_expired_activation_rejects(scene):
    selected(scene)
    scene.clock.advance(600)
    scene.token = scene.attestation(generation=11)
    scene.view = scene.governance_view()
    scene.bundle = scene.make_bundle(authorizations=scene.bundle.authorizations, transitions=scene.bundle.transitions,
        current=(scene.auth.activation_id,), sequence=3)
    scene.revocations = scene.make_revocations(sequence=3)
    scene.control_checkpoint = scene.checkpoint()
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in scene.read().rejection_codes


@pytest.mark.parametrize("field", ["authoritative_view", "trusted_time_evidence", "revocation_evidence", "lifecycle_evidence"], ids=["PC-44", "PC-45", "PC-46", "PC-47"])
def test_pc44_to_pc47_current_security_evidence_mandatory(scene, field):
    selected(scene)
    assert not scene.read(**{field: None}).is_resolved


def test_pc48_legacy_rollback_cannot_bypass_control(scene):
    selected(scene)
    report = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=scene.snapshot.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint())
    assert "CONTROL_SELECTION_AUTHORIZATION_REQUIRED" in report.rejection_codes


def test_pc49_emergency_disable_survives_audit_failure(scene, monkeypatch):
    selected(scene)
    monkeypatch.setattr(scene.audit, "append", lambda **kwargs: (_ for _ in ()).throw(OSError("unavailable")))
    result = scene.controller.disable(at=NOW)
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None
    assert result["status"] == "DISABLED" and not scene.read().is_resolved


def test_pc50_concurrent_selection_single_cas_winner(scene):
    scene.publish()
    commands = [scene.selection_request(), scene.selection_request()]
    checkpoint = scene.control_checkpoint
    def perform(command):
        try:
            return select_call(scene, command, checkpoint=checkpoint)
        except Exception as exc:
            return dict(status="REJECTED", codes=getattr(exc, "rejection_codes", ()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(perform, commands))
    assert sum(r["status"] == "SELECTION_COMMITTED" for r in results) == 1
    scene.control_checkpoint = scene.checkpoint()
    assert scene.read().is_resolved


def test_pc51_concurrent_read_selection_is_fail_closed(scene):
    selected(scene)
    old_cp = scene.control_checkpoint
    command = scene.selection_request()
    with ThreadPoolExecutor(max_workers=2) as pool:
        read_future = pool.submit(scene.read)
        select_future = pool.submit(select_call, scene, command)
        result, selection = read_future.result(), select_future.result()
    assert result.resolution_status in ("RESOLVED_SHADOW", "REJECTED_SHADOW")
    assert selection["control_committed"]
    assert not scene.read(control_checkpoint=old_cp).is_resolved


def test_pc52_symlink_escape_rejected(scene, tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = scene.control_workspace / "alias"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        # Windows unprivileged symlink creation may be unavailable; exercise the
        # equivalent reparse-point guard with a real junction, never skip Windows.
        if os.name != "nt":
            raise
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    try:
        rejected(lambda: PersistentShadowSnapshotStore(str(link), dataset_identity=scene.context.dataset_identity,
            publisher_public_key_path=scene.publisher_public))
    finally:
        if os.name == "nt" and not link.is_symlink():
            os.rmdir(link)
        else:
            link.unlink()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction requires Windows; mandatory on Windows CI")
def test_pc53_windows_junction_escape(scene, tmp_path):
    import _winapi
    link = scene.control_workspace / "junction"
    _winapi.CreateJunction(str(tmp_path), str(link))
    try:
        rejected(lambda: PersistentShadowSnapshotStore(str(link), dataset_identity=scene.context.dataset_identity,
            publisher_public_key_path=scene.publisher_public))
    finally:
        os.rmdir(link)


def test_pc54_traversal_and_repository_alias_rejected(scene):
    rejected(lambda: scene.controller.store.get("../outside"), "PERSISTENT_SNAPSHOT_ID_INVALID")
    rejected(lambda: PersistentShadowSnapshotStore(str(scene.control_workspace / ".."),
        dataset_identity=scene.context.dataset_identity, publisher_public_key_path=scene.publisher_public))
    rejected(lambda: PersistentShadowSnapshotStore(str(Path(__file__).resolve().parents[1]),
        dataset_identity=scene.context.dataset_identity, publisher_public_key_path=scene.publisher_public))


def test_pc55_partial_snapshot_file_rejected(scene):
    scene.publish()
    path = scene.controller.store._path(scene.snapshot.snapshot_id)
    path.write_bytes(path.read_bytes()[:30])
    rejected(lambda: scene.controller.store.get(scene.snapshot.snapshot_id))
    assert not scene.read().is_resolved


def test_pc56_bounded_thread_and_os_lock(scene):
    lock = scene.control_workspace / "bounded-test.lock"
    with BoundedShadowLock(lock):
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(lambda: rejected(lambda: BoundedShadowLock(lock, timeout=0.05).__enter__(), "CONTROL_LOCK_TIMEOUT"))
            assert result.result()


def test_pc57_legacy_inspection_not_control_authority(scene):
    from utils.shadow_runtime_reader import shadow_reader_status
    assert shadow_reader_status()["production_runtime_reader"] == "NOT_ENABLED"
    assert scene.controller.recover_read_only()["consumption_enabled"] is False


def test_pc58_functional_demo_and_seven_store_integrity(tmp_path):
    before = protected_store_hashes()
    result = run_acceptance(tmp_path / "demo")
    assert result["success"] and len(result["steps"]) >= 9
    assert before == result["seven_store_sha256"] == protected_store_hashes()


def test_pc59_no_production_import_or_api():
    root = Path(__file__).resolve().parents[1]
    for file in ("app.py", "utils/scraper.py", "utils/ingestion_identity.py"):
        tree = ast.parse((root / file).read_text(encoding="utf-8-sig"))
        modules = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any("shadow_snapshot_control" in m or "persistent_shadow_snapshots" in m for m in modules)


def test_pc60_cross_platform_ci_contract():
    ci = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert ci.count("os: [ubuntu-latest, windows-latest]") == 2 and "python -m pytest -q -rs" in ci
    # Final local suite and actual remote jobs are verified independently.


@pytest.mark.parametrize("kind", ["publication", "selection"])
@pytest.mark.parametrize("change", ["extra", "missing", "expired", "purpose"])
def test_command_schema_expiry_and_purpose(scene, kind, change):
    if kind == "selection":
        scene.publish()
    request = getattr(scene, kind + "_request")()
    payload = request.payload()
    purpose = request.artifact_type
    if change == "extra":
        payload["unexpected"] = "not ignored"
    elif change == "missing":
        payload.pop("reason")
    elif change == "expired":
        payload["expires_at"] = NOW
    else:
        purpose = PUBLICATION_PURPOSE if kind == "selection" else SELECTION_PURPOSE
    key = scene.selector_private if kind == "selection" else scene.publisher_private
    command = sign_trusted_artifact(key, artifact_type=purpose, payload=payload)
    rejected(lambda: (select_call if kind == "selection" else publish_call)(scene, command))
    assert scene.registry.active_snapshot_id is None


@pytest.mark.parametrize("change", ["schema", "extra", "evidence", "binding", "authorization"])
def test_persistent_envelope_strict_schema_and_references(scene, change):
    scene.publish()
    path = scene.controller.store._path(scene.snapshot.snapshot_id)
    envelope = json.loads(path.read_bytes())
    if change == "schema":
        envelope["schema_version"] = "999"
    elif change == "extra":
        envelope["extra"] = True
    elif change == "evidence":
        envelope["evidence"]["lifecycle"].pop("checkpoint")
    elif change == "binding":
        envelope["snapshot"]["bindings"][0]["extra"] = 1
    else:
        envelope["evidence"]["authorizations"][0]["authorization"]["operator"] = 123
    path.write_text(canonical_json(envelope)+"\n", encoding="utf-8")
    rejected(lambda: scene.controller.store.verify_store())
    assert not scene.read().is_resolved


@pytest.mark.parametrize("field", ["publisher", "selector", "control_checkpoint"])
@pytest.mark.parametrize("substitute", ["owner", "activation", "time", "governance", "revocation"])
def test_control_roots_cannot_substitute_existing_roles(scene, field, substitute):
    keys = dict(owner=scene.env.owner_public, activation=scene.env.authorizer_public,
        time=scene.public, governance=scene.governance_public, revocation=scene.revocation_public)
    setattr(scene, field + "_public", keys[substitute])
    with pytest.raises(ValueError):
        scene.new_controller()


def test_concurrent_publication_is_serial_and_idempotent(scene):
    command, checkpoint = scene.publication_request(), scene.control_checkpoint
    def publish():
        try:
            return publish_call(scene, command)
        except TrustedArtifactError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(publish), pool.submit(publish)]]
    assert sum(bool(r and r["control_committed"]) for r in results) == 1
    scene.control_checkpoint = scene.checkpoint()
    assert publish_call(scene, command)["status"] == "IDEMPOTENT"
    assert len(scene.controller.store.list_snapshot_ids()) == 1
    assert scene.controller.planning_state()["generation"] == 1
    assert checkpoint != scene.control_checkpoint and scene.registry.active_snapshot_id is None


@pytest.mark.parametrize("stage", ["publication", "intent", "commit", "pointer", "fsync", "readback"])
def test_storage_faults_preserve_bytes_and_disable(scene, monkeypatch, stage):
    import utils.persistent_shadow_snapshots as persistence
    scene.publish()
    audit_before = Path(scene.audit.path).read_bytes()
    journal_before = scene.controller.journal.path.read_bytes()
    def fail(*args, **kwargs):
        raise PermissionError("synthetic read-only filesystem")
    if stage == "publication":
        monkeypatch.setattr(scene.controller.store, "publish", fail)
        command = scene.publication_request()
        result = publish_call(scene, command)
    else:
        command = scene.selection_request()
        if stage in ("intent", "commit"):
            original = scene.controller.journal.append
            def append(**kwargs):
                if kwargs["event_type"].endswith("REQUESTED" if stage == "intent" else "COMMITTED"):
                    fail()
                return original(**kwargs)
            monkeypatch.setattr(scene.controller.journal, "append", append)
        elif stage == "pointer":
            monkeypatch.setattr(scene.controller, "_apply_pointer", fail)
        elif stage == "fsync":
            monkeypatch.setattr(persistence.os, "fsync", fail)
        else:
            original = scene.audit.records
            def records():
                values = original()
                if len(values) > 1:
                    fail()
                return values
            monkeypatch.setattr(scene.audit, "records", records)
        result = select_call(scene, command)
    assert result["status"] == "RECOVERY_REQUIRED"
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None
    assert Path(scene.audit.path).read_bytes().startswith(audit_before)
    assert scene.controller.journal.path.read_bytes().startswith(journal_before)


def test_partial_atomic_write_preserved_and_not_published(scene, monkeypatch):
    import utils.persistent_shadow_snapshots as persistence
    original = persistence.os.fsync
    def fsync(fd):
        # Only the immutable snapshot's pending file is interrupted.
        pending = tuple(scene.controller.store.directory.glob(".pending-*"))
        if pending:
            raise OSError("snapshot fsync failed")
        return original(fd)
    monkeypatch.setattr(persistence.os, "fsync", fsync)
    result = scene.publish()
    assert not result["control_committed"] and not scene.reader.enabled
    assert tuple(scene.controller.store.directory.glob(".pending-*"))
    rejected(scene.controller.store.verify_store, "PERSISTENT_STORE_UNCOMMITTED_FILE")


def test_audit_append_race_rejects_signed_old_tip(scene, monkeypatch):
    scene.publish()
    command = scene.selection_request()
    original = scene.audit.append
    raced = False
    def append(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            original(event_type="SHADOW_RUNTIME_DISABLED", occurred_at=NOW, actor="other-writer", actor_role="operator",
                dataset_identity=scene.context.dataset_identity, payload={"reason": "concurrent writer"})
        return original(**kwargs)
    monkeypatch.setattr(scene.audit, "append", append)
    result = select_call(scene, command)
    assert result["outcome"] == "UNCERTAIN"
    assert "CONTROL_AUDIT_COMPARE_AND_SWAP_FAILED" in result["rejection_codes"]
    assert not scene.reader.enabled and scene.registry.active_snapshot_id is None


def test_missing_workspace_and_file_hardlink_alias(scene, tmp_path):
    rejected(lambda: PersistentShadowSnapshotStore(str(tmp_path / "missing"),
        dataset_identity=scene.context.dataset_identity, publisher_public_key_path=scene.publisher_public))
    scene.publish()
    path = scene.controller.store._path(scene.snapshot.snapshot_id)
    alias = tmp_path / "hardlink.json"
    os.link(path, alias)
    try:
        rejected(lambda: scene.controller.store.get(scene.snapshot.snapshot_id), "CONTROL_PATH_ALIAS")
    finally:
        alias.unlink()


def test_cross_process_lock_is_bounded(scene):
    path = scene.control_workspace / "cross-process.lock"
    script = '''import sys
from utils.persistent_shadow_snapshots import BoundedShadowLock, ShadowControlError
try:
    with BoundedShadowLock(sys.argv[1], timeout=0.1):
        sys.exit(9)
except ShadowControlError as exc:
    assert "CONTROL_LOCK_TIMEOUT" in exc.rejection_codes
'''
    with BoundedShadowLock(path):
        result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("stage", ["intent_before", "intent_after", "audit_before", "audit_after",
    "commit_before", "commit_after", "pointer_before", "pointer_after"])
def test_real_process_crash_each_commit_boundary(tmp_path, stage):
    workspace = tmp_path / "crash-scene"
    script = '''import os, sys
from pathlib import Path
from tools.shadow_snapshot_control_validation import SyntheticControlScenario
s = SyntheticControlScenario(Path(sys.argv[1]))
assert s.publish()["control_committed"]
stage = sys.argv[2]
if stage.startswith(("intent", "commit")):
    obj, name = s.controller.journal, "append"
elif stage.startswith("audit"):
    obj, name = s.audit, "append"
else:
    obj, name = s.controller, "_apply_pointer"
original = getattr(obj, name)
def crash(*args, **kwargs):
    applicable = not stage.startswith(("intent", "commit")) or kwargs["event_type"].endswith("REQUESTED" if stage.startswith("intent") else "COMMITTED")
    if applicable and stage.endswith("before"):
        os._exit(73)
    result = original(*args, **kwargs)
    if applicable:
        os._exit(73)
    return result
setattr(obj, name, crash)
s.select()
os._exit(74)
'''
    result = subprocess.run([sys.executable, "-c", script, str(workspace), stage], capture_output=True, text=True, timeout=40)
    assert result.returncode == 73, result.stderr
    # A second actual process reconstructs all verifiers, loading public keys only.
    inspect = '''import json, sys
from tools.shadow_snapshot_control_validation import inspect_restart_public
print(json.dumps(inspect_restart_public(sys.argv[1])))
'''
    result = subprocess.run([sys.executable, "-c", inspect, str(workspace)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["registered"] == 1 and not report["reader_enabled"] and report["memory_selected"] is None
    if stage not in ("intent_before", "pointer_after"):
        assert report["status"] == "RECOVERY_REQUIRED" and report["discrepancies"]


def test_uncertain_commit_requires_explicit_reconciliation(scene, monkeypatch):
    scene.publish()
    original = scene.audit.append
    def append(**kwargs):
        record = original(**kwargs)
        raise OSError("post-write acknowledgment lost")
    with monkeypatch.context() as patch:
        patch.setattr(scene.audit, "append", append)
        result = scene.select()
    assert result["outcome"] == "UNCERTAIN" and not scene.reader.enabled
    before = scene.controller.journal.path.read_bytes()
    assert scene.recover()["status"] == "RECOVERY_VERIFIED"
    assert scene.controller.journal.path.read_bytes().startswith(before)
    assert scene.read().is_resolved and not scene.controller.recover_read_only()["discrepancies"]


def test_signed_rollback_and_old_handle_invalidation(scene):
    selected(scene)
    old = scene.read()
    first, second = scene.snapshot, alternate(scene)
    scene.publish(second)
    scene.select(second)
    assert not scene.reader.is_generation_current(old)
    result = scene.select(first, operation="ROLLBACK")
    assert result["control_committed"] and scene.read().is_resolved


def test_authenticated_rejected_cas_is_journaled(scene):
    scene.publish()
    command = scene.selection_request(expected_generation=999)
    rejected(lambda: select_call(scene, command), "CONTROL_COMPARE_AND_SWAP_FAILED")
    assert scene.controller.journal.records()[-1]["event_type"] == "SNAPSHOT_SELECTION_REJECTED"
    assert scene.registry.active_snapshot_id is None


def test_publication_audit_never_claims_new_effective_selection(scene):
    selected(scene)
    first, second = scene.snapshot, alternate(scene)
    scene.publish(second)
    assert dict(scene.audit.records()[-1].payload)["effective_snapshot_id"] == first.snapshot_id
    assert scene.registry.active_snapshot_id == first.snapshot_id


@pytest.mark.parametrize("field", ["require_observation_attestation", "require_recovery_evidence_for_reenable",
    "require_independent_revocation", "require_verified_supersession"])
def test_strict_control_rejects_missing_security_options(scene, field):
    kwargs = dict(registry=scene.registry, audit_store=scene.audit, strict_activation_verifier=scene.strict,
        require_trusted_time=True, require_authoritative_governance=True, require_authenticated_approval=True,
        require_verified_supersession=True, supersession_verifier=scene.lifecycle, require_independent_revocation=True,
        revocation_verifier=scene.revocation, require_observation_attestation=True, require_recovery_evidence_for_reenable=True,
        require_controlled_snapshot_selection=True, snapshot_controller=scene.controller)
    kwargs[field] = False
    with pytest.raises(ValueError):
        ShadowRuntimeReader(**kwargs)


def test_selection_interleaved_during_read_cannot_resolve(scene, monkeypatch):
    selected(scene)
    original = scene.reader._verify_current_lifecycle
    def verify(context, snapshot):
        result = original(context, snapshot)
        assert scene.select()["control_committed"]
        return result
    monkeypatch.setattr(scene.reader, "_verify_current_lifecycle", verify)
    result = scene.read()
    assert not result.is_resolved and "CONTROL_CHECKPOINT_MISMATCH" in result.rejection_codes


def test_partial_journal_write_is_never_truncated(scene, monkeypatch):
    scene.publish()
    before = scene.controller.journal.path.read_bytes()
    original = scene.controller.journal.append
    def append(**kwargs):
        if kwargs["event_type"].endswith("COMMITTED"):
            with scene.controller.journal.path.open("ab") as handle:
                handle.write(b'{"partial":')
                handle.flush()
                os.fsync(handle.fileno())
            raise OSError("torn append")
        return original(**kwargs)
    monkeypatch.setattr(scene.controller.journal, "append", append)
    result = scene.select()
    assert result["outcome"] == "UNCERTAIN" and not scene.reader.enabled
    assert scene.controller.journal.path.read_bytes().startswith(before)
    assert scene.controller.journal.path.read_bytes().endswith(b'{"partial":')
    assert scene.controller.recover_read_only()["status"] == "CORRUPT"


@pytest.mark.parametrize("change", ["governance", "checkpoint", "time_extra", "proposal_extra"])
def test_serialized_publication_evidence_cannot_be_substituted(scene, change):
    scene.publish()
    path = scene.controller.store._path(scene.snapshot.snapshot_id)
    envelope = json.loads(path.read_bytes())
    evidence = envelope["evidence"]
    if change == "governance":
        evidence["governance"]["canonical_ids"] = ["substitute"]
    elif change == "checkpoint":
        evidence["lifecycle"]["checkpoint"] = evidence["revocation"]["checkpoint"]
    elif change == "time_extra":
        evidence["trusted_time"]["extra"] = 1
    else:
        evidence["authorizations"][0]["proposal"]["extra"] = True
    path.write_text(canonical_json(envelope)+"\n", encoding="utf-8")
    rejected(scene.controller.store.verify_store)


def test_read_only_lock_and_permission_failure_are_closed(scene, monkeypatch):
    import stat
    lock = scene.control_workspace / "readonly.lock"
    lock.write_bytes(b"0")
    lock.chmod(stat.S_IREAD)
    try:
        if os.name == "nt" or not os.access(lock, os.W_OK):
            with pytest.raises(PermissionError):
                with BoundedShadowLock(lock):
                    pytest.fail("read-only lock accepted")
        else:
            # A root POSIX runner bypasses mode bits; inject the equivalent OS
            # denial rather than skip the required failure-path assertion.
            original = os.open
            def denied(path, *args, **kwargs):
                if str(path) == str(lock):
                    raise PermissionError("read-only workspace")
                return original(path, *args, **kwargs)
            with monkeypatch.context() as patch:
                patch.setattr(os, "open", denied)
                with pytest.raises(PermissionError):
                    with BoundedShadowLock(lock):
                        pytest.fail("permission denial ignored")
    finally:
        lock.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_own_success_advances_watermark_before_post_commit_checkpoint(scene):
    selected(scene)
    paths = [scene.controller.journal.path, Path(scene.audit.path), scene.controller.pointer_path]
    previous = {path: path.read_bytes() for path in paths}
    old_checkpoint = scene.control_checkpoint
    command = scene.selection_request()
    result = select_call(scene, command)
    assert result["control_committed"]
    for path, content in previous.items():
        path.write_bytes(content)
    assert "CONTROL_CHECKPOINT_REPLAY" in scene.read(control_checkpoint=old_checkpoint).rejection_codes


def test_existing_file_does_not_waive_store_publication_authentication(scene):
    scene.publish()
    before = scene.controller.store._path(scene.snapshot.snapshot_id).read_bytes()
    rejected(lambda: scene.controller.store.publish(scene.snapshot, publication=None, evidence={}, published_at=NOW),
        "PUBLICATION_EVIDENCE_MISSING")
    assert scene.controller.store._path(scene.snapshot.snapshot_id).read_bytes() == before
