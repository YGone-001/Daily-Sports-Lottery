"""认证、持久、仅 shadow 的发布/选择控制；没有生产入口或自动签名。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import os
from pathlib import Path
from uuid import uuid4

from utils.mapping_governance import load_trusted_public_key
from utils.activation_supersession import ActivationSupersessionVerifier
from utils.activation_revocation_authority import IndependentRevocationVerifier
from utils.persistent_shadow_snapshots import (
    PersistentShadowSnapshotStore, BoundedShadowLock, ShadowControlError, PUBLICATION_PURPOSE, PUBLICATION_FIELDS,
    workspace_path, safe_path, atomic_json, strict_json_file, decode_snapshot, publication_evidence, reject,
)
from utils.shadow_snapshot_control_journal import ShadowSnapshotControlJournal, GENESIS
from utils.shadow_activation_audit_store import ShadowActivationAuditStore, ShadowAuditRecord, ShadowAuditCommitReceipt
from utils.source_identity import fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError, verify_trusted_artifact
from utils.trusted_time_authority import parse_utc

CONTROL_SCHEMA = "1.0.0"
SELECTION_PURPOSE = "SHADOW_SNAPSHOT_SELECTION_REQUEST"
CHECKPOINT_PURPOSE = "SHADOW_SNAPSHOT_CONTROL_CHECKPOINT"
MAX_AGE_SECONDS = 300
SELECTION_FIELDS = PUBLICATION_FIELDS | {"operation", "expected_previous", "expected_generation",
    "expected_audit_tip", "expected_audit_count", "expected_selection_sequence"}


def current_bindings(snapshot, context):
    evidence = {e.authorization.activation_id: e.authorization for e in context.lifecycle_evidence.authorizations}
    return dict(dataset_identity=context.dataset_identity, snapshot_id=snapshot.snapshot_id,
        snapshot_fingerprint=snapshot.snapshot_fingerprint, baseline_fingerprint=context.current_baseline_fingerprint,
        policy_version=snapshot.policy_version, evaluator_version=snapshot.evaluator_version,
        activation_ids=sorted(snapshot.activation_ids),
        activation_fingerprints=sorted((aid, evidence[aid].fingerprint()) for aid in snapshot.activation_ids),
        lifecycle_fingerprint=context.lifecycle_evidence.state.fingerprint(),
        revocation_fingerprint=context.revocation_evidence.state.fingerprint(),
        governance_fingerprint=context.authoritative_view.fingerprint())


def publication_request_payload(snapshot, context, *, publisher, request_id, issued_at, reason, lifetime_seconds=60):
    return dict(control_schema_version=CONTROL_SCHEMA, purpose=PUBLICATION_PURPOSE,
        **current_bindings(snapshot, context), actor=publisher, role="snapshot-publisher", request_id=request_id,
        issued_at=issued_at, expires_at=(parse_utc(issued_at) + timedelta(seconds=lifetime_seconds)).isoformat(), reason=reason)


def selection_request_payload(snapshot, context, state, *, selector, request_id, issued_at, reason,
                              operation="SELECT", lifetime_seconds=60):
    return dict(control_schema_version=CONTROL_SCHEMA, purpose=SELECTION_PURPOSE,
        **current_bindings(snapshot, context), actor=selector, role="snapshot-selector", request_id=request_id,
        issued_at=issued_at, expires_at=(parse_utc(issued_at) + timedelta(seconds=lifetime_seconds)).isoformat(), reason=reason,
        operation=operation, expected_previous=state["selected"], expected_generation=state["generation"],
        expected_audit_tip=state["audit_tip"], expected_audit_count=state["audit_count"],
        expected_selection_sequence=state["selection_sequence"])


@dataclass(frozen=True)
class VerifiedControlSelection:
    snapshot_id: str
    generation: int
    journal_tip: str
    audit_tip: str
    checkpoint_fingerprint: str
    selection_request_fingerprint: str


class PersistentShadowSnapshotController:
    def __init__(self, workspace, *, dataset_identity, registry, audit_store,
                 publisher_public_key_path, selector_public_key_path, checkpoint_public_key_path,
                 lifecycle_verifier, revocation_verifier, max_age_seconds=60):
        from utils.shadow_activation_simulation import ShadowSnapshotRegistry
        if (not isinstance(registry, ShadowSnapshotRegistry) or not isinstance(audit_store, ShadowActivationAuditStore)
            or not isinstance(lifecycle_verifier, ActivationSupersessionVerifier)
            or not isinstance(revocation_verifier, IndependentRevocationVerifier)
            or audit_store._lock_factory is not BoundedShadowLock
            or not 0 < max_age_seconds <= MAX_AGE_SECONDS
            or lifecycle_verifier.strict_activation_verifier is not revocation_verifier.strict_activation_verifier
            or dataset_identity != lifecycle_verifier.dataset_identity or dataset_identity != revocation_verifier.dataset_identity):
            raise ValueError("persistent control requires registry, bounded durable audit and coherent strict authorities")
        self.workspace = workspace_path(workspace)
        self.dataset_identity, self.registry, self.audit = dataset_identity, registry, audit_store
        if audit_store.dataset_identity != dataset_identity:
            raise ValueError("control and audit datasets must match")
        self.lifecycle, self.revocation = lifecycle_verifier, revocation_verifier
        self.strict = lifecycle_verifier.strict_activation_verifier
        self.max_age_seconds = max_age_seconds
        self._paths = (publisher_public_key_path, selector_public_key_path, checkpoint_public_key_path)
        self._keys = tuple(load_trusted_public_key(p)["public_key_id"] for p in self._paths)
        if len(set(self._keys)) != 3 or set(self._keys) & (set(revocation_verifier._keys) | set(lifecycle_verifier._keys)):
            raise ValueError("publisher, selector and checkpoint roles must be distinct from all authorization roles")
        self.store = PersistentShadowSnapshotStore(str(self.workspace), dataset_identity=dataset_identity,
            publisher_public_key_path=publisher_public_key_path)
        self.journal = ShadowSnapshotControlJournal(str(self.workspace), dataset_identity=dataset_identity)
        self.lock_path = safe_path(self.workspace, self.workspace / "snapshot-control.lock")
        self.pointer_path = safe_path(self.workspace, self.workspace / "selected-snapshot.json")
        self.session_challenge = uuid4().hex
        self._reader = None
        self._uncertain = False
        self._observed = None
        self._accepted = False
        self._checkpoint_time = None
        self._read_only = bool(self.journal.records() or self.store.list_snapshot_ids())
        self._audit_identity = self.audit.store_identity()

    def attach_reader(self, reader):
        if self._reader is not None and self._reader is not reader:
            raise ValueError("one controlled reader per controller session")
        self._reader = reader
        with BoundedShadowLock(self.lock_path):
            state = self._inspect()
            for sid in state["published"]:
                self.registry.register(self.store.get(sid))
        if self._read_only:
            reader.disable(at="", reason="persistent restart is read-only until authenticated reconciliation")

    def _now(self, context):
        return self.strict.time_verifier.verify(context.trusted_time_evidence,
            dataset_identity=context.dataset_identity).effective_time

    def _security(self, snapshot, context):
        decode_snapshot(snapshot.as_dict())
        if (snapshot.dataset_identity != self.dataset_identity or context.dataset_identity != self.dataset_identity
            or snapshot.baseline_fingerprint != context.current_baseline_fingerprint
            or snapshot.policy_version != context.policy_version or snapshot.evaluator_version != context.evaluator_version):
            reject("CONTROL_SNAPSHOT_SCOPE_MISMATCH")
        revoked = self.revocation.verify(context.revocation_evidence, read_context=context)
        if any(aid in {x for x, _ in revoked.revoked_authorizations} for aid in snapshot.activation_ids):
            reject("ACTIVATION_AUTHORIZATION_REVOKED")
        self.lifecycle.verify_snapshot(context.lifecycle_evidence, read_context=context, snapshot=snapshot)

    def _command(self, command, purpose, now, *, historical=False):
        if not isinstance(command, SignedTrustedArtifact):
            reject("CONTROL_COMMAND_MISSING")
        index = 0 if purpose == PUBLICATION_PURPOSE else 1
        p = verify_trusted_artifact(command, expected_artifact_type=purpose, trusted_public_key_path=self._paths[index],
            current_time=now, max_age_seconds=self.max_age_seconds if not historical else MAX_AGE_SECONDS)
        if command.signer_public_key_id != self._keys[index]:
            reject("CONTROL_SIGNER_CHANGED")
        if set(p) != (PUBLICATION_FIELDS if index == 0 else SELECTION_FIELDS):
            reject("CONTROL_COMMAND_SCHEMA_INVALID")
        start, end, checked = parse_utc(p["issued_at"]), parse_utc(p["expires_at"]), parse_utc(now)
        if (not start <= checked < end or not 0 < (end-start).total_seconds() <= self.max_age_seconds
            or p.get("control_schema_version") != CONTROL_SCHEMA or p.get("purpose") != purpose
            or p.get("role") != ("snapshot-publisher" if index == 0 else "snapshot-selector")
            or p.get("dataset_identity") != self.dataset_identity
            or any(not isinstance(p.get(k), str) or not p[k].strip() for k in ("actor", "request_id", "reason"))
            or len(p["request_id"]) > 128):
            reject("CONTROL_COMMAND_INVALID")
        return p

    def _binding(self, command, snapshot, context, now):
        p = self._command(command, command.artifact_type, now)
        expected = current_bindings(snapshot, context)
        if any(fingerprint(p.get(key)) != fingerprint(value) for key, value in expected.items()):
            reject("CONTROL_COMMAND_BINDING_MISMATCH")
        return p

    def _audit_record(self, record, receipt, command, sid, previous, *, event_type=None):
        if not isinstance(record, ShadowAuditRecord) or not isinstance(receipt, ShadowAuditCommitReceipt):
            reject("CONTROL_AUDIT_RECEIPT_INVALID")
        p = command.payload()
        payload = dict(command_fingerprint=command.fingerprint(), request_id=p["request_id"],
            previous_snapshot_id=previous,
            effective_snapshot_id=previous if record.event_type == "SHADOW_SNAPSHOT_REGISTERED" else sid,
            snapshot_fingerprint=sid)
        if (receipt.store_identity != self._audit_identity or not receipt.matches(record)
            or type(receipt.persisted) is not bool or receipt.persisted is not True
            or type(receipt.sequence) is not int
            or record.actor != p["actor"] or record.actor_role != p["role"] or record.dataset_identity != self.dataset_identity
            or record.snapshot_id != sid or dict(record.payload) != payload
            or (event_type is not None and record.event_type != event_type)):
            reject("CONTROL_AUDIT_RECEIPT_INVALID")

    def _inspect(self):
        inventory = self.store.verify_store()
        records = self.journal.records()
        audit_records = tuple(self.audit.records())
        self.audit.verify()
        if tuple(self.audit.records()) != audit_records:
            reject("CONTROL_AUDIT_CONCURRENT_CHANGE")
        requests, pending, published = {}, set(), {}
        selected, generation, selection_sequence, last_commit, selection_command = "", 0, 0, None, ""
        bound_audit_ids, reconciled = set(), set()
        for rec in records:
            event = rec["event_type"]
            if event == "SNAPSHOT_CONTROL_DISABLED":
                if (rec["command"] is not None or rec["generation"] != generation + 1
                    or rec["previous_selected"] != selected or rec["effective_selected"] != ""
                    or rec["role"] != "emergency-disable" or rec["outcome"] != "DISABLED"):
                    reject("CONTROL_HISTORY_INVALID")
                selected, generation, last_commit = "", rec["generation"], rec
                continue
            if rec["command"] is None:
                reject("CONTROL_HISTORY_INVALID")
            command = SignedTrustedArtifact.from_dict(rec["command"])
            purpose = PUBLICATION_PURPOSE if event.startswith("SNAPSHOT_PUBLICATION") else SELECTION_PURPOSE
            p = self._command(command, purpose, rec["at"], historical=True)
            rid = p["request_id"]
            if (command.as_dict() != rec["command"] or command.fingerprint() != rec["command_fingerprint"]
                or rid != rec["request_id"] or rec["actor"] != p["actor"] or rec["role"] != p["role"]
                or rec["snapshot_id"] != p["snapshot_id"]):
                reject("CONTROL_HISTORY_INVALID")
            if rid in requests and requests[rid] != command.fingerprint():
                reject("CONTROL_REQUEST_ID_CONFLICT")
            requests[rid] = command.fingerprint()
            if event.endswith("REQUESTED"):
                if (rid in pending or rec["generation"] != generation
                    or rec["previous_selected"] != selected or rec["effective_selected"] != selected):
                    reject("CONTROL_HISTORY_INVALID")
                pending.add(rid)
            elif event.endswith("COMMITTED") or event == "SNAPSHOT_CONTROL_RECOVERY_VERIFIED":
                if rid not in pending or rec["generation"] != generation + 1 or rec["previous_selected"] != selected:
                    reject("CONTROL_HISTORY_INVALID")
                receipt = ShadowAuditCommitReceipt(**{k: rec["audit_receipt"][k]
                    for k in ("store_identity", "event_type", "event_id", "sequence", "entry_hash", "previous_entry_hash", "persisted")})
                if receipt.as_dict() != rec["audit_receipt"]:
                    reject("CONTROL_AUDIT_RECEIPT_INVALID")
                if not 0 <= receipt.sequence < len(audit_records):
                    reject("CONTROL_AUDIT_MISSING")
                audit = audit_records[receipt.sequence]
                expected_type = ("SHADOW_SNAPSHOT_REGISTERED" if event.startswith("SNAPSHOT_PUBLICATION") else
                    "SHADOW_ROLLBACK_COMPLETED" if event == "SNAPSHOT_CONTROL_RECOVERY_VERIFIED" else
                    "SHADOW_SNAPSHOT_SELECTED" if not selected else "SHADOW_SNAPSHOT_SWITCHED")
                self._audit_record(audit, receipt, command, rec["snapshot_id"], selected, event_type=expected_type)
                if rec["audit_event_id"] != audit.event_id or rec["at"] != audit.occurred_at:
                    reject("CONTROL_AUDIT_MISMATCH")
                bound_audit_ids.add(audit.event_id)
                if event.startswith("SNAPSHOT_PUBLICATION"):
                    self.store.get(rec["snapshot_id"])
                    published[rec["snapshot_id"]] = command.fingerprint()
                    if rec["effective_selected"] != selected:
                        reject("CONTROL_HISTORY_INVALID")
                else:
                    if rec["snapshot_id"] not in published or rec["effective_selected"] != rec["snapshot_id"]:
                        reject("CONTROL_HISTORY_INVALID")
                    # Recheck historical CAS from signed evidence, not just the
                    # caller-reported generation/previous pointer in journal rows.
                    first_audit = audit_records[audit.sequence - 1] if event == "SNAPSHOT_CONTROL_RECOVERY_VERIFIED" and audit.sequence > 0 else audit
                    expected_cas = dict(expected_previous=selected, expected_generation=generation,
                        expected_selection_sequence=selection_sequence, expected_audit_tip=first_audit.previous_entry_hash,
                        expected_audit_count=first_audit.sequence)
                    if any(type(p.get(k)) is not type(v) or p[k] != v for k, v in expected_cas.items()):
                        reject("CONTROL_HISTORY_COMPARE_AND_SWAP_INVALID")
                    selected, selection_command = rec["snapshot_id"], command.fingerprint()
                    selection_sequence += 1
                    if event == "SNAPSHOT_CONTROL_RECOVERY_VERIFIED":
                        pending.difference_update(rec["reconciles"])
                        reconciled.update(rec["reconciles"])
                        # Recovery has a selection audit plus the existing recovery completion audit.
                        selection_audits = [ar for ar in audit_records if dict(ar.payload).get("request_id") == rid
                            and ar.event_type in ("SHADOW_SNAPSHOT_SELECTED", "SHADOW_SNAPSHOT_SWITCHED")]
                        if (len(selection_audits) != 1 or selection_audits[0].sequence != audit.sequence - 1
                            or audit.previous_entry_hash != selection_audits[0].entry_hash):
                            reject("CONTROL_AUDIT_MISSING")
                        for ar in selection_audits:
                            self._audit_record(ar, self.audit.receipt_for(ar), command, selected, rec["previous_selected"])
                            bound_audit_ids.add(ar.event_id)
                generation, last_commit = rec["generation"], rec
                pending.discard(rid)
            elif event.endswith("REJECTED"):
                pending.discard(rid)
            elif event == "SNAPSHOT_SELECTION_UNCERTAIN":
                pending.add(rid)
            else:
                reject("CONTROL_HISTORY_INVALID")
        discrepancies = []
        for ar in audit_records:
            if (ar.event_type in ("SHADOW_SNAPSHOT_REGISTERED", "SHADOW_SNAPSHOT_SELECTED", "SHADOW_SNAPSHOT_SWITCHED")
                and ar.event_id not in bound_audit_ids and dict(ar.payload).get("request_id") not in reconciled):
                discrepancies.append("AUDIT_WITHOUT_CONTROL_COMMIT")
                pending.add(dict(ar.payload).get("request_id", ""))
        if pending:
            discrepancies.append("CONTROL_INTENT_WITHOUT_COMMIT")
        pointer_hash = ""
        if self.pointer_path.exists():
            safe_path(self.workspace, self.pointer_path)
            pointer_hash = hashlib.sha256(self.pointer_path.read_bytes()).hexdigest()
            try:
                pointer = strict_json_file(self.workspace, self.pointer_path)
            except ShadowControlError:
                pointer = None
            expected = self._pointer(selected, generation, last_commit)
            if pointer != expected:
                discrepancies.append("POINTER_WITHOUT_MATCHING_COMMIT")
        elif last_commit is not None:
            discrepancies.append("COMMIT_WITHOUT_POINTER")
        return dict(records=records, pending=tuple(sorted(pending)), published=published, selected=selected,
            generation=generation, selection_sequence=selection_sequence, last_commit=last_commit,
            selection_command=selection_command, journal_tip=records[-1]["entry_hash"] if records else GENESIS,
            sequence=len(records), inventory=inventory, audit_tip=audit_records[-1].entry_hash if audit_records else GENESIS,
            audit_count=len(audit_records), pointer_hash=pointer_hash, discrepancies=tuple(sorted(set(discrepancies))))

    def _pointer(self, selected, generation, commit):
        return dict(schema_version=CONTROL_SCHEMA, selected=selected, selected_fingerprint=selected,
            generation=generation, commit_id=commit["event_id"] if commit else "", commit_hash=commit["entry_hash"] if commit else "")

    def _apply_pointer(self, state, *, select=False):
        if self.pointer_path.exists():
            old = self.pointer_path.read_bytes()
            archive = safe_path(self.workspace, self.workspace / ("pointer-" + hashlib.sha256(old).hexdigest() + ".evidence"))
            if not archive.exists():
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(old)
                    handle.flush()
                    os.fsync(handle.fileno())
        atomic_json(self.workspace, self.pointer_path, self._pointer(state["selected"], state["generation"], state["last_commit"]))
        if select:
            self.registry.select(state["selected"])

    def recover_read_only(self):
        with BoundedShadowLock(self.lock_path):
            try:
                state = self._inspect()
                return dict(status="RECOVERY_REQUIRED" if state["discrepancies"] else "RECOVERED_READ_ONLY",
                    selected=state["selected"], generation=state["generation"], published=tuple(sorted(state["published"])),
                    discrepancies=state["discrepancies"], consumption_enabled=False)
            except Exception as exc:
                return dict(status="CORRUPT", consumption_enabled=False,
                    rejection_codes=getattr(exc, "rejection_codes", ("CONTROL_STORAGE_CORRUPT",)))

    def planning_state(self):
        """仅供准备签名 CAS 请求；此视图本身不授权选择或消费。"""
        with BoundedShadowLock(self.lock_path):
            state = self._inspect()
            return {key: state[key] for key in ("selected", "generation", "audit_tip", "audit_count", "selection_sequence")}

    def _checkpoint_body(self, state, issued_at):
        return dict(control_schema_version=CONTROL_SCHEMA, purpose=CHECKPOINT_PURPOSE, role="control-checkpoint",
            dataset_identity=self.dataset_identity, store_identity=self.store.store_identity(), journal_identity=self.journal.identity(),
            control_sequence=state["sequence"], control_chain_tip=state["journal_tip"],
            selected_snapshot_id=state["selected"], selected_snapshot_fingerprint=state["selected"],
            publication_inventory_fingerprint=fingerprint(dict(files=state["inventory"], published=state["published"])),
            audit_store_identity=self._audit_identity, audit_chain_tip=state["audit_tip"], audit_record_count=state["audit_count"],
            control_generation=state["generation"], pointer_fingerprint=state["pointer_hash"],
            discrepancies=list(state["discrepancies"]), session_challenge=self.session_challenge,
            issued_at=issued_at, expires_at=(parse_utc(issued_at)+timedelta(seconds=self.max_age_seconds)).isoformat())

    def checkpoint_payload(self, *, issued_at):
        """只读载荷；必须由外部配置的独立合成角色显式签名。"""
        with BoundedShadowLock(self.lock_path):
            return self._checkpoint_body(self._inspect(), issued_at)

    def _checkpoint(self, checkpoint, state, context):
        if self._accepted and self._observed is None:
            reject("CONTROL_CONTINUITY_LOST")
        now = self._now(context)
        p = verify_trusted_artifact(checkpoint, expected_artifact_type=CHECKPOINT_PURPOSE,
            trusted_public_key_path=self._paths[2], current_time=now, max_age_seconds=self.max_age_seconds)
        if not isinstance(checkpoint, SignedTrustedArtifact) or checkpoint.signer_public_key_id != self._keys[2]:
            reject("CONTROL_CHECKPOINT_SIGNER_CHANGED")
        expected = self._checkpoint_body(state, p["issued_at"])
        if p != expected or parse_utc(now) >= parse_utc(p["expires_at"]):
            reject("CONTROL_CHECKPOINT_MISMATCH")
        previous = self._observed
        if previous and (state["sequence"] < previous[0] or state["generation"] < previous[1]
            or (state["sequence"] == previous[0] and state["journal_tip"] != previous[2])
            or state["audit_count"] < previous[3]
            or (state["audit_count"] == previous[3] and state["audit_tip"] != previous[4])):
            reject("CONTROL_CHECKPOINT_REPLAY")
        if self._checkpoint_time and parse_utc(p["issued_at"]) < self._checkpoint_time:
            reject("CONTROL_CHECKPOINT_REPLAY")
        self._remember(state)
        self._checkpoint_time, self._accepted = parse_utc(p["issued_at"]), True
        return now

    def _remember(self, state):
        # Local successful writes advance the rejection watermark even before an
        # independent post-commit checkpoint arrives. This grants no read authority.
        self._observed = (state["sequence"], state["generation"], state["journal_tip"], state["audit_count"], state["audit_tip"])

    def _audit_append(self, event_type, command, sid, previous, now):
        p = command.payload()
        payload = dict(command_fingerprint=command.fingerprint(), request_id=p["request_id"],
            previous_snapshot_id=previous,
            effective_snapshot_id=previous if event_type == "SHADOW_SNAPSHOT_REGISTERED" else sid,
            snapshot_fingerprint=sid)
        record = self.audit.append(event_type=event_type, occurred_at=now, actor=p["actor"], actor_role=p["role"],
            dataset_identity=self.dataset_identity, snapshot_id=sid, payload=payload)
        if not isinstance(record, ShadowAuditRecord):
            reject("CONTROL_AUDIT_RECEIPT_INVALID")
        receipt = self.audit.receipt_for(record)
        self.audit.verify()
        readback = tuple(self.audit.records())
        self._audit_record(record, receipt, command, sid, previous, event_type=event_type)
        if event_type in ("SHADOW_SNAPSHOT_SELECTED", "SHADOW_SNAPSHOT_SWITCHED") and (
            record.previous_entry_hash != p["expected_audit_tip"] or record.sequence != p["expected_audit_count"]
        ):
            reject("CONTROL_AUDIT_COMPARE_AND_SWAP_FAILED")
        if not readback or readback[-1] != record or record.occurred_at != now:
            reject("CONTROL_AUDIT_READBACK_MISMATCH")
        return record, receipt

    def _fail_closed(self, now, reason):
        self._uncertain, self._read_only = True, True
        if self._reader is not None:
            self._reader.disable(at=now, reason=reason)
        self.registry._active_id = None

    def _operation(self, kind, snapshot, command, context, checkpoint):
        with BoundedShadowLock(self.lock_path):
            now = self._now(context)
            purpose = PUBLICATION_PURPOSE if kind == "PUBLICATION" else SELECTION_PURPOSE
            p = self._command(command, purpose, now)
            state = self._inspect()
            self._checkpoint(checkpoint, state, context)
            recovery = kind == "SELECTION" and p.get("operation") == "RECOVER"
            if (state["discrepancies"] or self._read_only or self._uncertain) and not recovery:
                reject("CONTROL_RECOVERY_REQUIRED")
            if not recovery and self.registry.active_snapshot_id != (state["selected"] or None):
                reject("CONTROL_REGISTRY_BYPASS")
            self._binding(command, snapshot, context, now)
            self._security(snapshot, context)
            rid = p["request_id"]
            matches = [r for r in state["records"] if r["request_id"] == rid]
            if matches:
                if any(r["command_fingerprint"] != command.fingerprint() for r in matches):
                    reject("CONTROL_REQUEST_ID_CONFLICT")
                done = next((r for r in matches if r["event_type"].endswith("COMMITTED") or r["event_type"] == "SNAPSHOT_CONTROL_RECOVERY_VERIFIED"), None)
                if done and (kind == "PUBLICATION" or state["selected"] == snapshot.snapshot_id):
                    return self._result("IDEMPOTENT", state, audit=True, control=True, checkpoint=True)
                reject("CONTROL_REQUEST_REPLAY")
            if kind == "SELECTION":
                if snapshot.snapshot_id not in state["published"]:
                    reject("CONTROL_SNAPSHOT_NOT_PUBLISHED")
                expected = dict(expected_previous=state["selected"], expected_generation=state["generation"],
                    expected_audit_tip=state["audit_tip"], expected_audit_count=state["audit_count"],
                    expected_selection_sequence=state["selection_sequence"])
                if any(type(p.get(k)) is not type(v) or p[k] != v for k, v in expected.items()):
                    self.journal.append(event_type="SNAPSHOT_SELECTION_REJECTED", command=command,
                        snapshot_id=snapshot.snapshot_id, previous_selected=state["selected"], effective_selected=state["selected"],
                        actor=p["actor"], role=p["role"], at=now, outcome="COMPARE_AND_SWAP_FAILED", generation=state["generation"])
                    reject("CONTROL_COMPARE_AND_SWAP_FAILED")
                if p.get("operation") not in ("SELECT", "ROLLBACK", "RECOVER"):
                    reject("CONTROL_OPERATION_UNSUPPORTED")
                if self._reader is not None and not self._reader.enabled and not recovery:
                    reject("CONTROL_RECOVERY_REQUIRED")
                self.registry.register(snapshot)
            before_audit = (state["audit_tip"], state["audit_count"])
            control_committed, audit_committed, started = False, False, False
            try:
                started = True
                self.journal.append(event_type=f"SNAPSHOT_{kind}_REQUESTED", command=command,
                    snapshot_id=snapshot.snapshot_id, previous_selected=state["selected"], effective_selected=state["selected"],
                    actor=p["actor"], role=p["role"], at=now, outcome="REQUESTED", generation=state["generation"])
                if kind == "PUBLICATION":
                    self.store.publish(snapshot, publication=command, evidence=publication_evidence(context), published_at=now)
                    self.registry.register(self.store.get(snapshot.snapshot_id))
                audit_type = "SHADOW_SNAPSHOT_REGISTERED" if kind == "PUBLICATION" else "SHADOW_SNAPSHOT_SWITCHED" if state["selected"] else "SHADOW_SNAPSHOT_SELECTED"
                audit, receipt = self._audit_append(audit_type, command, snapshot.snapshot_id, state["selected"], now)
                audit_committed = True
                if recovery:
                    selection_audit = audit
                    audit, receipt = self._audit_append("SHADOW_ROLLBACK_COMPLETED", command, snapshot.snapshot_id, state["selected"], now)
                    if audit.previous_entry_hash != selection_audit.entry_hash or audit.sequence != selection_audit.sequence + 1:
                        reject("CONTROL_AUDIT_COMPARE_AND_SWAP_FAILED")
                commit = self.journal.append(event_type="SNAPSHOT_CONTROL_RECOVERY_VERIFIED" if recovery else f"SNAPSHOT_{kind}_COMMITTED",
                    command=command, snapshot_id=snapshot.snapshot_id, previous_selected=state["selected"],
                    effective_selected=state["selected"] if kind == "PUBLICATION" else snapshot.snapshot_id,
                    actor=p["actor"], role=p["role"], at=now, outcome="COMMITTED", generation=state["generation"]+1,
                    audit_event_id=audit.event_id, audit_receipt=receipt.as_dict(), reconciles=state["pending"] if recovery else ())
                control_committed = True
                new_state = self._inspect()
                self._apply_pointer(new_state, select=kind == "SELECTION")
                checked = self._inspect()
                if checked["discrepancies"] or self.registry.active_snapshot_id != (checked["selected"] or None):
                    reject("CONTROL_POINTER_APPLICATION_FAILED")
                self._remember(checked)
                self._read_only, self._uncertain = False, False
                return self._result("RECOVERY_VERIFIED" if recovery else f"{kind}_COMMITTED", checked,
                    audit=True, control=True, checkpoint=True)
            except Exception as exc:
                self._fail_closed(now, "control commitment not independently verified")
                outcome = "UNCERTAIN"
                try:
                    if (self.audit.chain_tip(), len(self.audit.records())) == before_audit and not control_committed:
                        outcome = "NOT_COMMITTED"
                except Exception:
                    pass
                try:
                    self.journal.append(event_type="SNAPSHOT_SELECTION_UNCERTAIN" if kind == "SELECTION" else "SNAPSHOT_PUBLICATION_REJECTED",
                        command=command, snapshot_id=snapshot.snapshot_id, previous_selected=state["selected"], effective_selected="",
                        actor=p["actor"], role=p["role"], at=now, outcome=outcome, generation=state["generation"])
                except Exception:
                    pass  # A torn journal is preserved and remains CORRUPT.
                return dict(status="RECOVERY_REQUIRED", outcome=outcome, audit_committed=audit_committed,
                    control_committed=control_committed, selected_snapshot="", reader_enabled=False,
                    control_generation=state["generation"], checkpoint_verified=True,
                    rejection_codes=getattr(exc, "rejection_codes", ("CONTROL_STORAGE_FAILURE",)), production_activation="NOT_ENABLED")

    def publish(self, snapshot, *, request, context, checkpoint):
        return self._operation("PUBLICATION", snapshot, request, context, checkpoint)

    def select(self, snapshot_id, *, request, context, checkpoint):
        try:
            snapshot = self.store.get(snapshot_id)
        except FileNotFoundError:
            reject("CONTROL_SNAPSHOT_NOT_PUBLISHED")
        return self._operation("SELECTION", snapshot, request, context, checkpoint)

    def _result(self, status, state, *, audit, control, checkpoint):
        return dict(status=status, outcome="COMMITTED", audit_committed=audit, control_committed=control,
            selected_snapshot=state["selected"], reader_enabled=self._reader.enabled if self._reader else False,
            control_generation=state["generation"], checkpoint_verified=checkpoint,
            rejection_codes=(), production_activation="NOT_ENABLED")

    def verify_current(self, context, snapshot):
        try:
            return self._verify_current(context, snapshot)
        except TrustedArtifactError:
            raise
        except Exception as exc:
            raise ShadowControlError("control state cannot be verified",
                rejection_codes=("CONTROL_STORAGE_UNVERIFIABLE",)) from exc

    def _verify_current(self, context, snapshot):
        with BoundedShadowLock(self.lock_path):
            state = self._inspect()
            self._checkpoint(context.control_checkpoint, state, context)
            if self._read_only or self._uncertain or state["discrepancies"] or not state["selected"]:
                reject("CONTROL_SELECTION_UNVERIFIED")
            if (snapshot.snapshot_id != state["selected"] or self.registry.active_snapshot_id != state["selected"]
                or self.registry.get(state["selected"]) != self.store.get(state["selected"])):
                reject("CONTROL_REGISTRY_BYPASS")
            self._security(snapshot, context)
            return VerifiedControlSelection(state["selected"], state["generation"], state["journal_tip"], state["audit_tip"],
                context.control_checkpoint.fingerprint(), state["selection_command"])

    def generation_current(self, generation):
        try:
            with BoundedShadowLock(self.lock_path):
                state = self._inspect()
                return not self._read_only and not self._uncertain and not state["discrepancies"] and state["generation"] == generation and self.registry.active_snapshot_id == state["selected"]
        except Exception:
            return False

    def disable(self, *, at, actor="shadow-operator", reason="emergency disable"):
        # Consumption is disabled before any I/O, trusted-time or service access.
        self._fail_closed(at, reason)
        try:
            with BoundedShadowLock(self.lock_path):
                state = self._inspect()
                self.journal.append(event_type="SNAPSHOT_CONTROL_DISABLED", command=None, snapshot_id="",
                    previous_selected=state["selected"], effective_selected="", actor=actor, role="emergency-disable",
                    at=at, outcome="DISABLED", generation=state["generation"]+1)
                self.audit.append(event_type="SHADOW_RUNTIME_DISABLED", occurred_at=at, actor=actor, actor_role="emergency-disable",
                    dataset_identity=self.dataset_identity, payload=dict(reason=reason))
                checked = self._inspect()
                self._apply_pointer(checked)
                return self._result("DISABLED", checked, audit=True, control=True, checkpoint=False)
        except Exception as exc:
            return dict(status="DISABLED", reader_enabled=False, audit_committed=False, control_committed=False,
                rejection_codes=getattr(exc, "rejection_codes", ("CONTROL_DISABLE_AUDIT_FAILED",)))
