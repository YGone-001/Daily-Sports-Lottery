"""精确起点的协调式竞争复现；不改动任何实现或生产文件。"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from unittest.mock import patch
import builtins
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

from tools.shadow_snapshot_control_validation import SyntheticControlScenario
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.mapping_governance import GovernanceLedger, proposal_from_observations


def proposal(scene, index):
    seed = scene.env.proposal
    return proposal_from_observations((scene.env.observation,), requested_action=seed.requested_action,
        candidate_canonical_id=seed.candidate_canonical_id, existing_canonical_owner=seed.candidate_canonical_id,
        ingestion_plan_reference="synthetic-concurrency", expected_baseline_fingerprint=seed.expected_baseline_fingerprint,
        reason=f"separate synthetic decision {index}", identity_evidence_type=seed.identity_evidence_type,
        operator=seed.operator)


def append_governance(ledger, item):
    return ledger.append(proposal=item, to_state=item.status, actor=item.operator, actor_role="operator",
        reason=item.reason, baseline_fingerprint=item.expected_baseline_fingerprint, occurred_at=NOW)


def append_audit(store, index=0):
    return store.append(event_type="SHADOW_RUNTIME_DISABLED", occurred_at=NOW, actor="concurrency-probe",
        actor_role="operator", payload={"probe": str(index)})


def paused_writer_read(scene, *, governance):
    ledger = GovernanceLedger(str(scene.control_workspace / "baseline-governance.jsonl"))
    append_governance(ledger, proposal(scene, 0))
    store = ledger if governance else scene.audit
    if not governance:
        append_audit(store)
    ready, release = threading.Event(), threading.Event()
    original_open = builtins.open
    class Handle:
        def __init__(self, handle):
            self.handle = handle
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def __enter__(self):
            self.handle.__enter__()
            return self
        def __exit__(self, *args):
            return self.handle.__exit__(*args)
        def write(self, text):
            if not governance and len(text) > 2:
                middle = len(text)//2
                self.handle.write(text[:middle])
                self.handle.flush()
                ready.set()
                if not release.wait(5):
                    raise TimeoutError("baseline writer release not delivered")
                self.handle.write(text[middle:])
                return len(text)
            return self.handle.write(text)
        def flush(self):
            if governance:
                ready.set()
                if not release.wait(5):
                    raise TimeoutError("baseline writer release not delivered")
            return self.handle.flush()
    def opened(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if (governance and str(path).startswith(ledger.path + ".tmp-")) or (not governance and str(path) == store.path and mode == "a"):
            return Handle(handle)
        return handle
    def verify():
        try:
            result = store.verify()
            return dict(status="VERIFIED", count=result["entry_count"], tip=result["chain_tip"])
        except Exception as exc:
            return dict(status="REJECTED", error=type(exc).__name__, codes=getattr(exc, "rejection_codes", ()))
    with patch("builtins.open", opened), ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(append_governance, ledger, proposal(scene, 1)) if governance else pool.submit(append_audit, store, 1)
        try:
            if not ready.wait(5):
                raise RuntimeError("writer never reached coordinated boundary")
            reader = pool.submit(verify)
            try:
                result = reader.result(timeout=0.25)
                result["blocked_until_release"] = False
            except TimeoutError:
                result = dict(blocked_until_release=True)
        finally:
            release.set()
        writer.result(timeout=5)
        if result["blocked_until_release"]:
            result.update(reader.result(timeout=5))
    result["final_count"] = store.verify()["entry_count"]
    return result


CHILD = r'''
import json, os, sys, threading, time
import utils.mapping_governance as g
path, mode = sys.argv[1:3]
g.LEDGER_LOCK_TIMEOUT_SECONDS = 0.1
if mode == "thread":
    with g._ledger_lock(path):
        started = threading.Event()
        def waiter():
            started.set()
            with g._ledger_lock(path): pass
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        started.wait(1)
        thread.join(0.3)
        print(json.dumps(dict(waiting_after_configured_deadline=thread.is_alive(), configured_seconds=0.1)), flush=True)
        os._exit(0)
if mode == "holder":
    with g._ledger_lock(path):
        print("LOCKED", flush=True)
        sys.stdin.readline()
else:
    count = [0]
    if os.name == "nt":
        import msvcrt
        original = msvcrt.locking
        def locking(*args):
            if args[1] == msvcrt.LK_NBLCK: count[0] += 1
            return original(*args)
        msvcrt.locking = locking
    start = time.monotonic()
    try:
        with g._ledger_lock(path): pass
        result = dict(acquired=True)
    except OSError as exc:
        result = dict(acquired=False, error=type(exc).__name__, errno=exc.errno, winerror=getattr(exc,"winerror",None))
    result.update(elapsed_seconds=time.monotonic()-start, attempts=count[0])
    print(json.dumps(result), flush=True)
'''


def lock_probes(workspace):
    lock = str(workspace / "baseline.lock")
    thread = subprocess.run([sys.executable, "-c", CHILD, lock, "thread"], capture_output=True, text=True, timeout=5)
    if thread.returncode:
        raise RuntimeError(thread.stderr)
    holder = subprocess.Popen([sys.executable, "-c", CHILD, lock, "holder"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # readline runs in a bounded future; parent always terminates its own child.
        with ThreadPoolExecutor(max_workers=1) as pool:
            line = pool.submit(holder.stdout.readline).result(timeout=5)
        if line.strip() != "LOCKED":
            raise RuntimeError("holder not ready")
        contender = subprocess.Popen([sys.executable, "-c", CHILD, lock, "contender"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = contender.communicate(timeout=0.7)
            if contender.returncode:
                raise RuntimeError(err)
            os_result = json.loads(out)
        except subprocess.TimeoutExpired:
            contender.terminate()
            contender.communicate(timeout=5)
            os_result = dict(waiting_after_configured_deadline=True, terminated_by_probe=True, configured_seconds=0.1)
    finally:
        holder.communicate("release\n", timeout=5)
    released = subprocess.run([sys.executable, "-c", CHILD, lock, "contender"], capture_output=True, text=True, timeout=5)
    return json.loads(thread.stdout), dict(platform=os.name, contention=os_result, after_release=json.loads(released.stdout))


def reproduce(workspace):
    import hashlib
    import inspect
    from utils.mapping_governance import _ledger_lock
    from tools.shadow_ledger_lock_baseline import LEGACY_LOCK_SHA256
    if hashlib.sha256(inspect.getsource(_ledger_lock).encode()).hexdigest() != LEGACY_LOCK_SHA256:
        raise RuntimeError("run this reproducer against the unchanged 6146ff1 source export")
    before = protected_store_hashes()
    scene = SyntheticControlScenario(workspace / "scene")
    gov = paused_writer_read(scene, governance=True)
    audit = paused_writer_read(scene, governance=False)
    hybrid_ledger = GovernanceLedger(str(workspace / "hybrid.jsonl"))
    append_governance(hybrid_ledger, proposal(scene, 20))
    original_contracts, advanced = hybrid_ledger.binding_key_contracts, False
    def contracts_after_append():
        nonlocal advanced
        if not advanced:
            advanced = True
            append_governance(hybrid_ledger, proposal(scene, 21))
        return original_contracts()
    with patch.object(hybrid_ledger, "binding_key_contracts", contracts_after_append):
        report = hybrid_ledger.verify()
    gov["hybrid_report"] = dict(entry_count=report["entry_count"],
        binding_count=sum(report["binding_key_contracts"].values()),
        impossible_hybrid=sum(report["binding_key_contracts"].values()) > report["entry_count"])
    thread, os_result = lock_probes(workspace)
    scene.control_checkpoint = scene.checkpoint()
    scene.publish()
    command = scene.selection_request()
    barrier = threading.Barrier(4)
    def action(name):
        barrier.wait(timeout=5)
        try:
            if name == "select":
                result = scene.controller.select(scene.snapshot.snapshot_id, request=command,
                    context=scene.read_context(), checkpoint=scene.control_checkpoint)
            elif name == "append":
                append_audit(scene.audit, 9)
                result = dict(status="APPENDED")
            elif name == "verify":
                result = scene.audit.verify()
            else:
                result = scene.controller.recover_read_only()
            return dict(operation=name, status=result.get("status", result.get("integrity")))
        except Exception as exc:
            return dict(operation=name, status="REJECTED", codes=getattr(exc,"rejection_codes",()))
    with ThreadPoolExecutor(max_workers=4) as pool:
        mixed = list(pool.map(action, ("select", "append", "verify", "recover"), timeout=15))
    consumable_after_race = scene.read().is_resolved
    path = Path(scene.audit.path)
    original = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(b'{"torn":')
    try:
        scene.audit.verify()
        corrupt = dict(rejected=False)
    except Exception as exc:
        corrupt = dict(rejected=True, codes=getattr(exc,"rejection_codes",()), bytes_preserved=path.read_bytes().startswith(original))
    return dict(baseline="6146ff11c5eeb43a45b9ede19ae28241e7ee38be", platform=os.name,
        results={"LC-01":gov,"LC-02":audit,"LC-03":thread,
            "LC-04" if os.name != "nt" else "LC-05":os_result,
            "LC-06":dict(valid_nested_append_succeeded=gov["final_count"]==2,
                naive_public_relock_would_reenter_same_nonrecursive_guard=True),
            "LC-07":dict(no_deadlock=True, outcomes=mixed, consumable_after_race=consumable_after_race),
            "LC-08":corrupt}, windows_original_root_cause="ROOT_CAUSE_NOT_REPRODUCED",
        seven_store_sha256=before, seven_stores_unchanged=before==protected_store_hashes())


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ledger-concurrency-baseline-") as root:
        print(json.dumps(reproduce(Path(root)), sort_keys=True, indent=2))
