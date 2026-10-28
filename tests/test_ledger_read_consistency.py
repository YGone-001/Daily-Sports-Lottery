"""LC-01..LC-50：每例有独立进程截止时间，不允许挂住整个测试运行。"""
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
import errno
import json
import multiprocessing as mp
import os
from pathlib import Path
import threading
import traceback
from unittest.mock import patch

import pytest

from tools.shadow_snapshot_control_validation import SyntheticControlScenario
from tools.shadow_ledger_concurrency_baseline import append_audit, append_governance, proposal, paused_writer_read
from tools.shadow_ledger_concurrency_validation import assert_snapshot, process_lock_probe, process_stress, _stop
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.ledger_lock import BoundedLedgerLock, LedgerLockError
from utils.mapping_governance import GovernanceLedger, _ledger_lock, audit_historical_approvals
from utils.shadow_activation_audit_store import ShadowActivationAuditStore
from utils.source_identity import canonical_json

_CASES = {}


def _worker(name, root, kwargs, connection):
    try:
        _CASES[name](Path(root), **kwargs)
        connection.send(None)
    except BaseException:
        connection.send(traceback.format_exc())
    finally:
        connection.close()


def bounded_case(function):
    _CASES[function.__name__] = function
    @wraps(function)
    def supervised(tmp_path, **kwargs):
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_worker, args=(function.__name__,str(tmp_path),kwargs,child))
        process.start()
        child.close()
        try:
            assert parent.poll(95), "case exceeded its independent 95-second deadline"
            error = parent.recv()
            assert error is None, error
            process.join(timeout=5)
            assert not process.is_alive() and process.exitcode == 0
        finally:
            _stop(process)
            parent.close()
    return supervised


def scene(root):
    return SyntheticControlScenario(root / "scene")


def selected(s):
    assert s.publish()["control_committed"] and s.select()["control_committed"]
    assert s.read().is_resolved


def thread_timeout(path):
    entered, release = threading.Event(), threading.Event()
    def holder():
        with BoundedLedgerLock(path, timeout=1):
            entered.set()
            assert release.wait(3)
    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert entered.wait(2)
    try:
        with pytest.raises(LedgerLockError) as exc:
            with BoundedLedgerLock(path, timeout=0.08):
                pytest.fail("competing thread acquired lock")
        assert exc.value.rejection_codes == ("LEDGER_LOCK_TIMEOUT",)
        assert exc.value.diagnostics["phase"] == "thread"
        assert 0.05 <= exc.value.diagnostics["elapsed_seconds"] < 1.5
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    with BoundedLedgerLock(path, timeout=0.5):
        pass


@pytest.mark.parametrize("lc", ["LC-01","LC-02"])
@bounded_case
def test_lc01_lc02_cooperating_partial_append(tmp_path, lc):
    result = paused_writer_read(scene(tmp_path), governance=lc=="LC-01")
    assert result["blocked_until_release"] and result["status"] == "VERIFIED" and result["count"] == 2


@bounded_case
def test_lc03_bounded_thread_guard(tmp_path):
    thread_timeout(tmp_path / "thread.lock")


@pytest.mark.parametrize("lc", [
    pytest.param("LC-04", marks=pytest.mark.skipif(os.name=="nt", reason="POSIX path executes on Ubuntu CI")),
    pytest.param("LC-05", marks=pytest.mark.skipif(os.name!="nt", reason="Windows msvcrt path executes on Windows CI"))])
@bounded_case
def test_lc04_lc05_bounded_os_contention(tmp_path, lc):
    result = process_lock_probe(tmp_path)
    assert result["reacquired"] and result["outcome"]["status"] == "TIMEOUT"
    assert result["outcome"]["diagnostics"]["attempts"] >= 2
    baseline = process_lock_probe(tmp_path, legacy=True)
    assert baseline["outcome"]["status"] == ("REJECTED" if os.name=="nt" else "WAIT_EXCEEDED_CONFIGURED_DEADLINE")


@bounded_case
def test_lc06_append_has_no_recursive_public_verification(tmp_path):
    s = scene(tmp_path)
    ledger = GovernanceLedger(str(tmp_path / "nested.jsonl"))
    append_governance(ledger, proposal(s, 1))
    with patch.object(ledger,"entries",side_effect=AssertionError("recursive public entries")), patch.object(ledger,"verify",side_effect=AssertionError("recursive public verify")):
        append_governance(ledger,proposal(s,2))
    assert ledger.verify()["entry_count"] == 2
    with _ledger_lock(ledger._lock_path):
        with pytest.raises(LedgerLockError) as exc:
            ledger.verify()
        assert "LEDGER_LOCK_RECURSIVE" in exc.value.rejection_codes


@pytest.mark.parametrize("lc", ["LC-07","LC-40"])
@bounded_case
def test_lc07_lc40_controller_store_journal_audit_order(tmp_path, lc):
    s = scene(tmp_path)
    s.publish()
    barrier = threading.Barrier(4)
    def action(kind):
        barrier.wait(timeout=5)
        if kind == "select": return s.select()
        if kind == "audit": return s.audit.verified_snapshot()
        if kind == "store": return s.controller.store.verify_store()
        return s.controller.recover_read_only()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(action,("select","audit","store","recover"),timeout=20))
    assert results[0]["control_committed"] and s.read().is_resolved


@pytest.mark.parametrize("lc", ["LC-08","LC-09"])
@bounded_case
def test_lc08_lc09_real_torn_committed_history(tmp_path, lc):
    s = scene(tmp_path)
    store = s.audit if lc=="LC-08" else s.ledger
    if lc=="LC-08": append_audit(store)
    path = Path(store.path)
    with path.open("ab") as handle: handle.write(b'{"torn":')
    before = path.read_bytes()
    with pytest.raises(Exception): store.verify()
    assert path.read_bytes() == before


@pytest.mark.parametrize("lc", ["LC-10","LC-11"])
@bounded_case
def test_lc10_lc11_no_hybrid_multifield_report(tmp_path, lc):
    s = scene(tmp_path)
    store = s.ledger if lc=="LC-10" else s.audit
    original = store.entries if lc=="LC-10" else store.records
    attribute = "entries" if lc=="LC-10" else "records"
    captures = []
    def copy_then_advance():
        records = original()
        captures.append(records)
        if lc=="LC-10": append_governance(store,proposal(s,93))
        else: append_audit(store,93)
        return records
    with patch.object(store,attribute,copy_then_advance):
        snapshot = store.verified_snapshot()
    assert len(captures) == 1 and snapshot.records == captures[0]
    assert_snapshot(snapshot)
    assert store.verify()["entry_count"] == len(snapshot.records)+1


@bounded_case
def test_lc12_read_only_operations_preserve_history_bytes(tmp_path):
    s=scene(tmp_path)
    selected(s)
    paths=[Path(s.ledger.path),Path(s.audit.path),s.controller.journal.path,s.controller.pointer_path]
    before={p:p.read_bytes() for p in paths}
    s.ledger.verify(); s.audit.verify(); s.audit.checkpoint_payload(issued_at=NOW)
    s.controller.journal.records(); s.controller.verify_current(s.read_context(),s.snapshot)
    assert {p:p.read_bytes() for p in paths} == before


@bounded_case
def test_lc13_two_thread_writers_five_readers(tmp_path):
    s=scene(tmp_path)
    ledger=GovernanceLedger(str(tmp_path / "threads.jsonl"))
    barrier=threading.Barrier(7)
    def action(index):
        barrier.wait(timeout=5)
        if index < 2:
            for step in range(4): append_governance(ledger,proposal(s,index*10+step))
        else:
            for _ in range(5): assert_snapshot(ledger.verified_snapshot())
    with ThreadPoolExecutor(max_workers=7) as pool: list(pool.map(action,range(7),timeout=20))
    assert ledger.verify()["entry_count"] == 8


@bounded_case
def test_lc14_concurrent_owner_approvals_remain_attributable(tmp_path):
    from utils.mapping_governance import build_approval_body, sign_mapping_approval
    s=scene(tmp_path)
    ledger=GovernanceLedger(str(tmp_path / "approvals.jsonl"),dataset_identity=s.context.dataset_identity)
    items=[proposal(s,i+70) for i in range(2)]
    approvals=[]
    for item in items:
        append_governance(ledger,item)
        body=build_approval_body(proposal=item,source_observation_fingerprint=s.env.observation.semantic_fingerprint(),
            dataset_identity=s.context.dataset_identity,operator=item.operator,approver="concurrent-owner",
            approved_at=NOW,reason="explicit concurrent approval")
        approvals.append(sign_mapping_approval(str(Path(s.env.owner_public).with_name("owner_private.pem")),body))
    barrier=threading.Barrier(3)
    def action(index):
        barrier.wait(timeout=5)
        if index==2:
            return audit_historical_approvals(ledger,trusted_public_key_path=s.env.owner_public)
        return ledger.append(proposal=items[index],to_state="APPROVED",actor="concurrent-owner",actor_role="owner",
            reason="explicit concurrent approval",baseline_fingerprint=items[index].expected_baseline_fingerprint,
            approval=approvals[index],trusted_public_key_path=s.env.owner_public,
            source_observation_fingerprint=s.env.observation.semantic_fingerprint(),occurred_at=NOW)
    with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(action,range(3),timeout=20))
    audit=audit_historical_approvals(ledger,trusted_public_key_path=s.env.owner_public)
    assert audit["approved_decisions"]==2 and all(v["actor"]=="concurrent-owner" for v in audit["verified"])


@pytest.mark.parametrize("lc", ["LC-15","LC-16"])
@bounded_case
def test_lc15_lc16_idempotency_and_conflict(tmp_path,lc):
    s=scene(tmp_path)
    ledger=GovernanceLedger(str(tmp_path / "replay.jsonl"))
    item=proposal(s,1)
    append_governance(ledger,item)
    before=Path(ledger.path).read_bytes()
    if lc=="LC-15": assert append_governance(ledger,item)[1] is False
    else:
        with pytest.raises(Exception):
            ledger.append(proposal=item,to_state=item.status,actor=item.operator,actor_role="operator",
                reason="conflicting metadata",baseline_fingerprint=item.expected_baseline_fingerprint,occurred_at=NOW)
    assert Path(ledger.path).read_bytes()==before


@bounded_case
def test_lc17_historical_signature_is_still_verified(tmp_path):
    import utils.mapping_governance as g
    s=scene(tmp_path)
    path=Path(s.ledger.path)
    entries=[e.as_dict() for e in s.ledger.entries()]
    entry=entries[-1]
    entry["approval_artifact"]["signature"]="A"*86+"=="
    entry["approval_fingerprint"]=g.approval_fingerprint(entry["approval_artifact"])
    entry["decision_fingerprint"]=g._decision_fingerprint(entry)
    entry["replay_key"]=g._replay_key_of(entry)
    entry["transition_id"]=g._transition_identity(entry)
    entry["entry_hash"]=g._entry_hash(entry)
    path.write_text("".join(canonical_json(e)+"\n" for e in entries),encoding="utf-8")
    assert s.ledger.verify()["integrity"]=="OK"
    with pytest.raises(Exception): audit_historical_approvals(s.ledger,trusted_public_key_path=s.env.owner_public)


@bounded_case
def test_lc18_invalid_hash_chain_rejects(tmp_path):
    s=scene(tmp_path)
    path=Path(s.ledger.path)
    path.write_bytes(path.read_bytes().replace(b'"reason":',b'"reason_broken":',1))
    with pytest.raises(Exception): s.ledger.verify()


@bounded_case
def test_lc19_missing_and_corrupt_are_distinct(tmp_path):
    ledger=GovernanceLedger(str(tmp_path / "absent.jsonl"))
    assert not ledger.exists() and ledger.verify()["entry_count"]==0 and not ledger.exists()
    Path(ledger.path).write_bytes(b"not JSON")
    with pytest.raises(Exception): ledger.verify()


@pytest.mark.parametrize("lc", ["LC-20","LC-21","LC-22"])
@bounded_case
def test_lc20_lc22_release_on_read_validation_and_os_failure(tmp_path,lc):
    s=scene(tmp_path)
    append_audit(s.audit)
    if lc=="LC-20":
        with patch("utils.shadow_activation_audit_store.json.loads",side_effect=ValueError("parse fault")):
            with pytest.raises(Exception): s.audit.verify()
    elif lc=="LC-21":
        with patch.object(s.audit,"_validate_records",side_effect=ValueError("validation fault")):
            with pytest.raises(Exception): s.audit.verify()
    else:
        with patch.object(BoundedLedgerLock,"_try_os_lock",side_effect=OSError(errno.EIO,"unexpected OS error")):
            with pytest.raises(LedgerLockError) as exc:
                with _ledger_lock(str(tmp_path / "failure.lock"),timeout=.2): pass
            assert exc.value.rejection_codes==("LEDGER_LOCK_OS_ERROR",)
            assert exc.value.diagnostics["attempts"]==1
        with _ledger_lock(str(tmp_path / "failure.lock"),timeout=.2): pass
    with BoundedLedgerLock(s.audit._lock_path,timeout=.3): pass
    assert s.audit.verify()["entry_count"]==1


@bounded_case
def test_lc23_process_exit_releases_owned_os_lock(tmp_path):
    assert process_lock_probe(tmp_path,abrupt=True)["reacquired"]


@pytest.mark.skipif(os.name!="nt",reason="actual Windows byte-range locking executes on Windows CI")
@bounded_case
def test_lc24_windows_byte_zero_and_beyond_eof(tmp_path):
    assert process_lock_probe(tmp_path,byte_offset=0)["outcome"]["status"]=="TIMEOUT"
    assert process_lock_probe(tmp_path,byte_offset=1)["outcome"]["status"]=="ACQUIRED"


@pytest.mark.parametrize("lc", [
    pytest.param("LC-25",marks=pytest.mark.skipif(os.name!="nt",reason="Windows contention executes on Windows CI")),
    pytest.param("LC-26",marks=pytest.mark.skipif(os.name=="nt",reason="POSIX contention executes on Ubuntu CI"))])
@bounded_case
def test_lc25_lc26_bounded_os_wait(tmp_path,lc):
    result=process_lock_probe(tmp_path)
    assert .08 <= result["outcome"]["diagnostics"]["elapsed_seconds"] < 2


@bounded_case
def test_lc27_reader_and_writer_thread_timeouts(tmp_path):
    thread_timeout(tmp_path / "reader-writer.lock")


@pytest.mark.parametrize("lc", ["LC-28","LC-29"])
@bounded_case
def test_lc28_lc29_checkpoint_binds_one_snapshot(tmp_path,lc):
    s=scene(tmp_path)
    append_audit(s.audit)
    checkpoint=s.audit.export_checkpoint(s.checkpoint_private,issued_at=NOW)
    if lc=="LC-29": append_audit(s.audit,2)
    if lc=="LC-28":
        _,snap=s.audit.verify_external_checkpoint_snapshot(checkpoint,trusted_public_key_path=s.checkpoint_public,
            current_time=NOW,max_age_seconds=60)
        assert_snapshot(snap)
        assert snap.report["entry_count"]==checkpoint.payload()["entry_count"]
    else:
        with pytest.raises(Exception): s.audit.verify_external_checkpoint(checkpoint,
            trusted_public_key_path=s.checkpoint_public,current_time=NOW,max_age_seconds=60)


@bounded_case
def test_lc30_current_control_checkpoint_cannot_be_bypassed(tmp_path):
    s=scene(tmp_path)
    selected(s)
    assert not s.read(control_checkpoint=None).is_resolved


@bounded_case
def test_lc31_selection_audit_race_never_partially_consumes(tmp_path):
    s=scene(tmp_path)
    s.publish()
    original=s.audit.append
    inserted=False
    def append(**kwargs):
        nonlocal inserted
        if not inserted:
            inserted=True
            original(event_type="SHADOW_RUNTIME_DISABLED",occurred_at=NOW,actor="other",actor_role="operator",payload={})
        return original(**kwargs)
    with patch.object(s.audit,"append",append): result=s.select()
    assert result["outcome"]=="UNCERTAIN" and not s.reader.enabled and s.registry.active_snapshot_id is None


@pytest.mark.parametrize("lc", ["LC-32","LC-33","LC-34","LC-35"])
@bounded_case
def test_lc32_lc35_current_security_requirements(tmp_path,lc):
    s=scene(tmp_path)
    selected(s)
    if lc=="LC-32":
        s.revocations=s.make_revocations((s.revocation_event(),),sequence=1)
        assert "ACTIVATION_AUTHORIZATION_REVOKED" in s.read().rejection_codes
    elif lc=="LC-33":
        old=s.snapshot
        s.successor()
        assert not s.reader.read(observation=s.env.observation,snapshot=old,read_context=s.read_context(),
            observation_attestation=s.observation_attestation).is_resolved
    else:
        field="authoritative_view" if lc=="LC-34" else "trusted_time_evidence"
        assert not s.read(**{field:None}).is_resolved


@bounded_case
def test_lc36_disable_precedes_unavailable_audit_io(tmp_path):
    s=scene(tmp_path)
    selected(s)
    with patch.object(s.audit,"records",side_effect=OSError("unavailable")):
        s.controller.disable(at=NOW)
    assert not s.reader.enabled and s.registry.active_snapshot_id is None


@bounded_case
def test_lc37_audit_ack_loss_stays_uncertain(tmp_path):
    s=scene(tmp_path)
    s.publish()
    original=s.audit.append
    before=Path(s.audit.path).read_bytes()
    def append(**kwargs):
        original(**kwargs)
        raise OSError("acknowledgment lost")
    with patch.object(s.audit,"append",append): result=s.select()
    assert result["outcome"]=="UNCERTAIN" and not s.reader.enabled
    assert Path(s.audit.path).read_bytes().startswith(before)


@pytest.mark.parametrize("lc", ["LC-38","LC-39"])
@bounded_case
def test_lc38_lc39_restart_history_and_explicit_recovery(tmp_path,lc):
    s=scene(tmp_path)
    selected(s)
    s.restart()
    assert not s.reader.enabled and s.registry.active_snapshot_id is None
    assert s.controller.recover_read_only()["published"]
    if lc=="LC-39":
        with pytest.raises(Exception): s.reader.re_enable(at=NOW,read_context=s.read_context(),evidence=s.evidence)
        assert s.recover()["control_committed"] and s.read().is_resolved


@pytest.mark.parametrize("lc", ["LC-41","LC-42"])
@bounded_case
def test_lc41_lc42_cross_process_stress(tmp_path,lc):
    s=scene(tmp_path)
    result=process_stress(tmp_path / "stress",s.env.proposal,kind="governance" if lc=="LC-41" else "audit")
    assert result["final_count"]==8 and result["coherent_reads"]==40


@bounded_case
def test_lc43_os_failure_preserves_history(tmp_path):
    s=scene(tmp_path)
    append_audit(s.audit)
    before=Path(s.audit.path).read_bytes()
    with patch.object(BoundedLedgerLock,"_try_os_lock",side_effect=PermissionError(errno.EPERM,"denied")):
        with pytest.raises(Exception): append_audit(s.audit,1)
    assert Path(s.audit.path).read_bytes()==before


@bounded_case
def test_lc44_no_refresh_lock_interference(tmp_path):
    import config
    from utils.concurrency import acquire_refresh_lock
    with patch.object(config,"DATA_DIR",str(tmp_path / "refresh")):
        with acquire_refresh_lock():
            with _ledger_lock(str(tmp_path / "independent.lock"),timeout=.2): pass


@bounded_case
def test_lc45_protected_paths_and_aliases_reject(tmp_path):
    from utils.persistent_shadow_snapshots import BoundedShadowLock
    root=Path(__file__).resolve().parents[1]
    with pytest.raises(Exception): GovernanceLedger(str(root / "data" / "forbidden.jsonl"))
    with pytest.raises(Exception): BoundedShadowLock(root / "data" / "forbidden.lock")
    target=tmp_path / "target.lock"
    target.write_bytes(b"0")
    alias=tmp_path / "alias.lock"
    os.link(target,alias)
    try:
        with pytest.raises(LedgerLockError): BoundedLedgerLock(alias)
    finally: alias.unlink()


@bounded_case
def test_lc46_existing_junction_guard_is_unchanged(tmp_path):
    from utils.persistent_shadow_snapshots import BoundedShadowLock
    outside=tmp_path / "outside"
    outside.mkdir()
    alias=tmp_path / "alias"
    if os.name=="nt":
        import _winapi
        _winapi.CreateJunction(str(outside),str(alias))
    else: alias.symlink_to(outside,target_is_directory=True)
    try:
        with pytest.raises(Exception): BoundedShadowLock(alias / "escape.lock")
        assert not tuple(outside.iterdir())
    finally:
        if os.name=="nt": os.rmdir(alias)
        else: alias.unlink()


@bounded_case
def test_lc47_seven_stores_unchanged(tmp_path):
    before=protected_store_hashes()
    s=scene(tmp_path)
    selected(s)
    s.restart()
    s.recover()
    assert before==protected_store_hashes()


@bounded_case
def test_lc48_no_production_integration(tmp_path):
    from utils.mapping_governance import governance_status
    from utils.shadow_runtime_reader import shadow_reader_status
    assert governance_status()["production_mapping_write_authorization"]=="NOT_GRANTED"
    assert shadow_reader_status()["production_runtime_reader"]=="NOT_ENABLED"
    for filename in ("app.py","utils/scraper.py","utils/ingestion_identity.py"):
        source=(Path(__file__).resolve().parents[1] / filename).read_text(encoding="utf-8-sig")
        assert "from utils.ledger_lock" not in source and "from utils.shadow_snapshot_control" not in source


@bounded_case
def test_lc49_existing_crypto_domains_are_unchanged(tmp_path):
    s=scene(tmp_path)
    assert s.verify().is_consumable
    assert not s.strict.verify(s.auth,s.context,time_evidence=None,governance_view=s.view).is_consumable
    assert audit_historical_approvals(s.ledger,trusted_public_key_path=s.env.owner_public)["approved_decisions"]==1


@bounded_case
def test_lc50_ci_requires_both_platforms(tmp_path):
    ci=(Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
    assert ci.count("os: [ubuntu-latest, windows-latest]")==2
    assert "python -m pytest -q -rs" in ci and "junction regression was skipped on Windows" in ci
    # Actual final-SHA workflow/job conclusions and platform totals are checked after push.


@pytest.mark.parametrize("stage", ["registry", "open", "after_os", "initialize", "release"])
@bounded_case
def test_lock_cleanup_at_each_ownership_boundary(tmp_path,stage):
    from tools.shadow_ledger_lock_baseline import legacy_lock_namespace
    path=str(tmp_path / "ownership.lock")
    lock=BoundedLedgerLock(path,timeout=.15,poll_interval=.01)
    if stage=="registry":
        with patch.object(BoundedLedgerLock,"_thread_guards_lock") as registry:
            registry.acquire.return_value=False
            with pytest.raises(LedgerLockError): lock.__enter__()
            registry.release.assert_not_called()
    elif stage=="open":
        # Exact archived baseline leaves its thread guard owned on os.open error.
        legacy=legacy_lock_namespace()["_ledger_lock"](path)
        with patch("os.open",side_effect=OSError(errno.EIO,"open failed")):
            with pytest.raises(OSError): legacy.__enter__()
            assert legacy._guard.locked()
            with pytest.raises(LedgerLockError): lock.__enter__()
        assert not lock._thread_owned and lock._fd is None
    elif stage=="after_os":
        original=lock._acquire_os_lock
        def interrupted():
            original()
            raise KeyboardInterrupt("after OS success")
        with patch.object(lock,"_acquire_os_lock",interrupted), patch.object(lock,"_unlock_os") as unlock:
            with pytest.raises(KeyboardInterrupt): lock.__enter__()
            unlock.assert_not_called()  # ownership was not acknowledged; close releases the fd lock.
    elif stage=="initialize":
        original=lock._unlock_os
        with patch("os.write",side_effect=OSError(errno.EIO,"initialize failed")), patch.object(lock,"_unlock_os",wraps=original) as unlock:
            with pytest.raises(LedgerLockError): lock.__enter__()
            assert unlock.call_count==1
    else:
        with patch.object(lock,"_unlock_os",side_effect=OSError(errno.EIO,"unlock failed")):
            with pytest.raises(LedgerLockError) as exc:
                with lock: pass
            assert exc.value.rejection_codes==("LEDGER_LOCK_RELEASE_FAILED",)
    assert lock._fd is None and not lock._thread_owned and not lock._os_owned
    with BoundedLedgerLock(path,timeout=.3): pass


@pytest.mark.parametrize("stage", ["write", "flush", "fsync"])
@bounded_case
def test_audit_io_faults_preserve_evidence_and_release_locks(tmp_path,stage):
    import builtins
    s=scene(tmp_path)
    selected(s)
    before=Path(s.audit.path).read_bytes()
    opened=builtins.open
    fsync=os.fsync
    class Handle:
        def __init__(self,handle): self.handle=handle
        def __getattr__(self,name): return getattr(self.handle,name)
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self,*args): return self.handle.__exit__(*args)
        def write(self,text):
            if stage=="write" and len(text)>2:
                self.handle.write(text[:10]); self.handle.flush()
                raise OSError("partial audit append")
            return self.handle.write(text)
        def flush(self):
            self.handle.flush()
            if stage=="flush": raise OSError("flush acknowledgment lost")
    def open_fault(path,mode="r",*args,**kwargs):
        handle=opened(path,mode,*args,**kwargs)
        return Handle(handle) if str(path)==s.audit.path and mode=="a" else handle
    def fsync_fault(fd):
        if stage=="fsync" and os.fstat(fd).st_ino==os.stat(s.audit.path).st_ino:
            raise OSError("fsync failed")
        return fsync(fd)
    with patch("builtins.open",open_fault), patch("os.fsync",fsync_fault): result=s.select()
    assert result["outcome"]=="UNCERTAIN" and not s.reader.enabled and s.registry.active_snapshot_id is None
    assert Path(s.audit.path).read_bytes().startswith(before)
    with BoundedLedgerLock(s.audit._lock_path,timeout=.3): pass


@pytest.mark.skipif(os.name!="nt",reason="Windows deny-delete replacement probe executes on Windows CI")
@bounded_case
def test_windows_replacement_sharing_is_not_os_lock_contention(tmp_path):
    from tools.shadow_ledger_concurrency_validation import windows_replace_probe
    s=scene(tmp_path)
    result=windows_replace_probe(tmp_path,s.env.proposal)
    assert result["winerror"] in (5,32) and result["history_preserved"] and result["subsequent_append"]


@bounded_case
def test_real_process_termination_during_partial_append_rejects_on_restart(tmp_path):
    from tools.shadow_ledger_concurrency_validation import abrupt_append_probe
    result=abrupt_append_probe(tmp_path / "append-crash")
    assert result["lock_reacquired"] and result["restart_rejected"] and result["history_preserved"]
