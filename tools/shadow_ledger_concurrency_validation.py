"""真实隔离并发验收；所有子进程均由父进程设截止时间并清理。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import multiprocessing as mp
import os
import tempfile
import threading
import time
import traceback

from tools.shadow_snapshot_control_validation import SyntheticControlScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes, require
from tools.shadow_ledger_concurrency_baseline import append_audit, append_governance, proposal, paused_writer_read
from tools.shadow_ledger_lock_baseline import legacy_lock_namespace
from utils.ledger_lock import BoundedLedgerLock, LedgerLockError
from utils.mapping_governance import GovernanceLedger, build_proposal, reconstruct_proposal_from_evidence
from utils.shadow_activation_audit_store import ShadowActivationAuditStore


def fresh_proposal(seed, index):
    return build_proposal(source_observation_ref=seed.source_observation_ref,
        source_identity_scope=seed.scope_dict, scoped_source_identity_key=seed.identity_key_dict,
        candidate_canonical_id=seed.candidate_canonical_id, existing_canonical_owner=seed.existing_canonical_owner,
        identity_evidence_type=seed.identity_evidence_type, observation_fingerprints=seed.observation_fingerprints,
        ingestion_plan_reference=seed.ingestion_plan_reference, reason=f"explicit concurrent proposal {index}",
        expected_baseline_fingerprint=seed.expected_baseline_fingerprint, requested_action=seed.requested_action,
        operator=seed.operator)


def assert_snapshot(snapshot):
    records, report = snapshot.records, snapshot.report
    require(report["entry_count"] == len(records), "hybrid entry count")
    require(report["chain_tip"] == (records[-1].entry_hash if records else "0" * 64), "hybrid chain tip")
    require([r.sequence for r in records] == list(range(len(records))), "sequence discontinuity")
    if "binding_key_contracts" in report:
        require(sum(report["binding_key_contracts"].values()) <= len(records), "hybrid binding statistics")
    return report["entry_count"]


def windows_replace_probe(workspace, seed):
    """有意持有 deny-delete 句柄；证明共享冲突，不推断间歇问题根因。"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
        wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    ledger = GovernanceLedger(str(Path(workspace) / "sharing.jsonl"))
    append_governance(ledger,fresh_proposal(seed,101))
    before = Path(ledger.path).read_bytes()
    handle = kernel.CreateFileW(ledger.path,0x80000000,3,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        try:
            append_governance(ledger,fresh_proposal(seed,102))
            raise RuntimeError("deny-delete handle did not prevent replacement")
        except OSError as exc:
            result = dict(errno=exc.errno,winerror=getattr(exc,"winerror",None))
            require(result["winerror"] in (5,32), "unexpected replacement failure classification")
            require(Path(ledger.path).read_bytes()==before,"sharing failure changed historical bytes")
    finally:
        require(bool(kernel.CloseHandle(handle)), "Windows probe handle close failed")
    require(append_governance(ledger,fresh_proposal(seed,102))[1],"append did not recover after handle close")
    return dict(**result,history_preserved=True,subsequent_append=True,original_root_cause="ROOT_CAUSE_NOT_REPRODUCED")


def _stop(process):
    if process.is_alive():
        process.terminate()
    process.join(timeout=5)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
    require(not process.is_alive(), "owned child could not be stopped")


def _lock_holder(path, connection, legacy=False, byte_offset=None):
    try:
        if byte_offset is not None:
            import msvcrt
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.lseek(fd, byte_offset, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                connection.send(dict(status="HELD", offset=byte_offset))
                connection.recv()
                os.lseek(fd, byte_offset, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)
        else:
            cls = legacy_lock_namespace()["_ledger_lock"] if legacy else BoundedLedgerLock
            with cls(path):
                connection.send(dict(status="HELD"))
                connection.recv()
    except BaseException:
        connection.send(dict(status="ERROR", error=traceback.format_exc()))
    finally:
        connection.close()


def _legacy_contender(path, connection):
    try:
        cls = legacy_lock_namespace()["_ledger_lock"]
        original_acquire = cls._acquire_os_lock
        def observed_acquire(self):
            connection.send(dict(status="OS_ATTEMPT"))
            return original_acquire(self)
        cls._acquire_os_lock = observed_acquire
        started = time.monotonic()
        try:
            with cls(path):
                result = dict(status="ACQUIRED")
        except OSError as exc:
            result = dict(status="REJECTED", errno=exc.errno, winerror=getattr(exc,"winerror",None))
        connection.send(dict(**result, elapsed_seconds=time.monotonic()-started))
    finally:
        connection.close()


def process_lock_probe(workspace, *, abrupt=False, legacy=False, byte_offset=None):
    ctx = mp.get_context("spawn")
    path = str(Path(workspace) / "process.lock")
    parent, child = ctx.Pipe()
    holder = ctx.Process(target=_lock_holder, args=(path, child, legacy, byte_offset))
    holder.start()
    child.close()
    try:
        require(parent.poll(10), "lock holder did not start")
        ready = parent.recv()
        require(ready["status"] == "HELD", str(ready))
        if legacy:
            receive, send = ctx.Pipe()
            contender = ctx.Process(target=_legacy_contender, args=(path, send))
            contender.start()
            send.close()
            try:
                require(receive.poll(10), "legacy contender did not reach OS acquisition")
                require(receive.recv()["status"] == "OS_ATTEMPT", "legacy probe failed before OS call")
                outcome = receive.recv() if receive.poll(0.6) else dict(status="WAIT_EXCEEDED_CONFIGURED_DEADLINE")
            finally:
                _stop(contender)
                receive.close()
        elif abrupt:
            outcome = dict(status="HOLDER_TERMINATED")
        else:
            try:
                with BoundedLedgerLock(path, timeout=0.12, poll_interval=0.01) as lock:
                    outcome = dict(status="ACQUIRED", diagnostics=lock.diagnostics)
            except LedgerLockError as exc:
                require(exc.rejection_codes == ("LEDGER_LOCK_TIMEOUT",),
                    "unexpected OS error is not a contention timeout")
                outcome = dict(status="TIMEOUT", codes=exc.rejection_codes, diagnostics=exc.diagnostics)
            if byte_offset == 1:
                require(outcome["status"] == "ACQUIRED", "unrelated byte blocked")
            else:
                require(outcome["status"] == "TIMEOUT" and outcome["diagnostics"]["phase"] == "os", "OS lock did not bound contention")
        if abrupt:
            _stop(holder)
        else:
            parent.send("release")
            holder.join(timeout=10)
            require(not holder.is_alive() and holder.exitcode == 0, "holder failed to release")
        with BoundedLedgerLock(path, timeout=0.5):
            reacquired = True
        return dict(outcome=outcome, reacquired=reacquired, holder_exitcode=holder.exitcode)
    finally:
        _stop(holder)
        parent.close()


def _stress_worker(kind, location, seed_evidence, writer_index, rounds, barrier, connection):
    try:
        store = GovernanceLedger(location) if kind == "governance" else ShadowActivationAuditStore(location)
        seed = reconstruct_proposal_from_evidence(seed_evidence)
        observations = []
        connection.send(dict(status="READY"))
        for step in range(rounds):
            barrier.wait(timeout=20)
            if writer_index is None:
                observations.append(assert_snapshot(store.verified_snapshot()))
            elif kind == "governance":
                append_governance(store, fresh_proposal(seed, writer_index*1000+step))
            else:
                append_audit(store, writer_index*1000+step)
            barrier.wait(timeout=20)
            if writer_index is None:
                observations.append(assert_snapshot(store.verified_snapshot()))
            barrier.wait(timeout=20)
        require(observations == sorted(observations), "observed prefix moved backward")
        connection.send(dict(status="OK", observed_counts=observations))
    except BaseException:
        connection.send(dict(status="ERROR", error=traceback.format_exc()))
    finally:
        connection.close()


def _partial_append_worker(workspace, connection):
    import builtins
    from unittest.mock import patch
    store=ShadowActivationAuditStore(workspace)
    opened=builtins.open
    class Handle:
        def __init__(self,handle): self.handle=handle
        def __getattr__(self,name): return getattr(self.handle,name)
        def __enter__(self): self.handle.__enter__(); return self
        def __exit__(self,*args): return self.handle.__exit__(*args)
        def write(self,text):
            if len(text)>2:
                self.handle.write(text[:len(text)//2])
                self.handle.flush()
                connection.send("PARTIAL_WRITE_WHILE_LOCKED")
                connection.recv()
                raise RuntimeError("abrupt probe unexpectedly resumed")
            return self.handle.write(text)
    def opened_audit(path,mode="r",*args,**kwargs):
        handle=opened(path,mode,*args,**kwargs)
        return Handle(handle) if str(path)==store.path and mode=="a" else handle
    with patch("builtins.open",opened_audit):
        append_audit(store,9)


def abrupt_append_probe(workspace):
    workspace=Path(workspace)
    workspace.mkdir(exist_ok=True)
    store=ShadowActivationAuditStore(str(workspace))
    append_audit(store)
    before=Path(store.path).read_bytes()
    ctx=mp.get_context("spawn")
    parent,child=ctx.Pipe()
    process=ctx.Process(target=_partial_append_worker,args=(str(workspace),child))
    process.start()
    child.close()
    try:
        require(parent.poll(10),"append process did not reach partial write")
        require(parent.recv()=="PARTIAL_WRITE_WHILE_LOCKED","append readiness invalid")
        _stop(process)
        preserved=Path(store.path).read_bytes()
        require(preserved.startswith(before) and not preserved.endswith(b"\n"),"crash probe did not leave torn evidence")
        with BoundedLedgerLock(store._lock_path,timeout=.5): pass
        restarted=ShadowActivationAuditStore(str(workspace))
        try:
            restarted.verify()
            raise RuntimeError("torn committed tail accepted on restart")
        except Exception as exc:
            codes=getattr(exc,"rejection_codes",())
            require("TORN_FINAL_RECORD" in codes,"restart did not reject actual torn tail")
        require(Path(store.path).read_bytes()==preserved,"verification repaired history")
        return dict(writer_exitcode=process.exitcode,lock_reacquired=True,restart_rejected=True,
            rejection_codes=codes,history_preserved=True)
    finally:
        _stop(process)
        parent.close()


def process_stress(workspace, seed, *, kind, writers=2, readers=5, rounds=4):
    ctx = mp.get_context("spawn")
    workspace = Path(workspace)
    workspace.mkdir(exist_ok=True)
    location = str(workspace / "governance.jsonl") if kind == "governance" else str(workspace)
    barrier = ctx.Barrier(writers+readers)
    children, connections = [], []
    deadline = time.monotonic()+70
    try:
        for index in range(writers+readers):
            parent, child = ctx.Pipe()
            process = ctx.Process(target=_stress_worker, args=(kind, location, seed.evidence_payload(),
                index if index < writers else None, rounds, barrier, child))
            process.start()
            child.close()
            children.append(process)
            connections.append(parent)
        for connection in connections:
            require(connection.poll(max(0,deadline-time.monotonic())), "worker readiness timeout")
            require(connection.recv()["status"] == "READY", "worker setup failed")
        results = []
        for connection in connections:
            require(connection.poll(max(0,deadline-time.monotonic())), "cross-process stress timeout")
            result = connection.recv()
            require(result["status"] == "OK", str(result))
            results.append(result)
        for process in children:
            process.join(timeout=max(0,deadline-time.monotonic()))
            require(not process.is_alive() and process.exitcode == 0, "stress process did not finish")
        store = GovernanceLedger(location) if kind == "governance" else ShadowActivationAuditStore(location)
        count = assert_snapshot(store.verified_snapshot())
        require(count == writers*rounds, "lost writer update")
        return dict(kind=kind, writers=writers, readers=readers, rounds=rounds,
            final_count=count, coherent_reads=sum(len(r["observed_counts"]) for r in results), success=True)
    finally:
        for process in children:
            _stop(process)
        for connection in connections:
            connection.close()


def run_validation(workspace):
    before = protected_store_hashes()
    scene = SyntheticControlScenario(Path(workspace) / "scene")
    governance = paused_writer_read(scene, governance=True)
    audit = paused_writer_read(scene, governance=False)
    require(governance["blocked_until_release"] and audit["blocked_until_release"], "reader bypassed writer lock")
    require(governance["status"] == audit["status"] == "VERIFIED", "cooperative writer misclassified as corruption")
    stress = [process_stress(Path(workspace) / (kind+str(writers)), scene.env.proposal,
        kind=kind, writers=writers) for kind in ("governance","audit") for writers in (1,2)]
    lock = process_lock_probe(workspace)
    termination = process_lock_probe(workspace, abrupt=True)
    baseline_os = process_lock_probe(workspace, legacy=True)
    replacement = windows_replace_probe(workspace, scene.env.proposal)
    append_crash = abrupt_append_probe(Path(workspace) / "append-crash")
    scene.control_checkpoint = scene.checkpoint()
    require(scene.publish()["control_committed"], "publication failed")
    barrier = threading.Barrier(2)
    def select():
        barrier.wait(timeout=5)
        return scene.select()
    def verify():
        barrier.wait(timeout=5)
        return assert_snapshot(scene.audit.verified_snapshot())
    with ThreadPoolExecutor(max_workers=2) as pool:
        selected, count = pool.submit(select), pool.submit(verify)
        require(selected.result(timeout=15)["control_committed"], "selection/verifier contention failed")
        observed_count = count.result(timeout=15)
    require(scene.read().is_resolved, "strict reader failed after coordinated selection")
    original = scene.audit.append
    def uncertain(**kwargs):
        result = original(**kwargs)
        raise OSError("synthetic post-write acknowledgment loss")
    scene.audit.append = uncertain
    outcome = scene.select()
    require(outcome["outcome"] == "UNCERTAIN" and not scene.reader.enabled and scene.registry.active_snapshot_id is None,
        "uncertain selection remained consumable")
    require(before == protected_store_hashes(), "production JSON changed")
    return dict(success=True, platform=os.name, synchronized_reads=dict(governance=governance,audit=audit),
        process_stress=stress, bounded_os_lock=lock, terminated_holder=termination, pinned_baseline_os=baseline_os,
        windows_replace_probe=replacement,
        abrupt_append=append_crash,
        selection_with_audit_verifier=dict(success=True, observed_audit_count=observed_count),
        uncertain_selection=dict(outcome=outcome["outcome"], reader_enabled=scene.reader.enabled),
        windows_original_root_cause="ROOT_CAUSE_NOT_REPRODUCED", seven_store_sha256=before,
        seven_stores_unchanged=True, production_activation_ready=False, decision="NO-GO")


def _validation_worker(root, connection):
    try:
        connection.send(run_validation(root))
    except BaseException:
        connection.send(dict(success=False, error=traceback.format_exc()))
    finally:
        connection.close()


def main():
    ctx = mp.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="shadow-ledger-concurrency-") as root:
        parent, child = ctx.Pipe()
        process = ctx.Process(target=_validation_worker, args=(root,child))
        process.start()
        child.close()
        try:
            result = parent.recv() if parent.poll(180) else dict(success=False,error="validation timed out")
        finally:
            process.join(timeout=5)
            _stop(process)
            parent.close()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
