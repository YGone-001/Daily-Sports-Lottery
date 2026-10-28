"""隔离控制日志：严格 JSONL、追加哈希链和有界锁；不是独立信任锚。"""
from __future__ import annotations

import json
import os

from utils.persistent_shadow_snapshots import BoundedShadowLock, safe_path, workspace_path, reject, sync_directory
from utils.source_identity import canonical_json, fingerprint

JOURNAL_SCHEMA = "1.0.0"
GENESIS = "0" * 64
EVENT_TYPES = frozenset({"SNAPSHOT_PUBLICATION_REQUESTED", "SNAPSHOT_PUBLICATION_COMMITTED",
    "SNAPSHOT_PUBLICATION_REJECTED", "SNAPSHOT_SELECTION_REQUESTED", "SNAPSHOT_SELECTION_COMMITTED",
    "SNAPSHOT_SELECTION_REJECTED", "SNAPSHOT_SELECTION_UNCERTAIN", "SNAPSHOT_CONTROL_DISABLED",
    "SNAPSHOT_CONTROL_RECOVERY_VERIFIED"})
FIELDS = frozenset({"schema_version", "sequence", "previous_hash", "event_id", "entry_hash", "event_type",
    "request_id", "command_fingerprint", "command", "snapshot_id", "previous_selected", "effective_selected",
    "actor", "role", "at", "outcome", "audit_event_id", "audit_receipt", "generation", "reconciles"})


def event_id(record):
    return fingerprint({k: v for k, v in record.items() if k not in ("entry_hash", "event_id")})


class ShadowSnapshotControlJournal:
    def __init__(self, workspace, *, dataset_identity):
        self.workspace = workspace_path(workspace)
        self.dataset_identity = dataset_identity
        self.path = safe_path(self.workspace, self.workspace / "snapshot-control.jsonl")
        self.lock_path = safe_path(self.workspace, self.workspace / "snapshot-control-journal.lock")

    def identity(self):
        return fingerprint(dict(schema=JOURNAL_SCHEMA, path=str(self.path), dataset=self.dataset_identity))

    def records(self):
        safe_path(self.workspace, self.path)
        if not self.path.exists():
            return ()
        data = self.path.read_bytes()
        if len(data) > 64 * 1024 * 1024 or (data and not data.endswith(b"\n")):
            reject("CONTROL_JOURNAL_TORN")
        records, tip, ids = [], GENESIS, set()
        for line in data.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError):
                reject("CONTROL_JOURNAL_CORRUPT")
            if not isinstance(record, dict) or set(record) != FIELDS or line != (canonical_json(record) + "\n").encode("utf-8"):
                reject("CONTROL_JOURNAL_FORMAT_INVALID")
            if (record["schema_version"] != JOURNAL_SCHEMA or record["event_type"] not in EVENT_TYPES
                or type(record["sequence"]) is not int or record["sequence"] != len(records)
                or record["previous_hash"] != tip or record["event_id"] != event_id(record)
                or record["entry_hash"] != fingerprint({k: v for k, v in record.items() if k != "entry_hash"})
                or record["event_id"] in ids or type(record["generation"]) is not int or record["generation"] < 0):
                reject("CONTROL_JOURNAL_INTEGRITY_INVALID")
            for key in ("request_id", "command_fingerprint", "snapshot_id", "previous_selected", "effective_selected",
                        "actor", "role", "at", "outcome", "audit_event_id"):
                if not isinstance(record[key], str):
                    reject("CONTROL_JOURNAL_FORMAT_INVALID")
            if not isinstance(record["reconciles"], list) or any(not isinstance(x, str) for x in record["reconciles"]):
                reject("CONTROL_JOURNAL_FORMAT_INVALID")
            records.append(record)
            ids.add(record["event_id"])
            tip = record["entry_hash"]
        return tuple(records)

    def append(self, *, event_type, command, snapshot_id, previous_selected, effective_selected,
               actor, role, at, outcome, generation, audit_event_id="", audit_receipt=None, reconciles=()):
        if event_type not in EVENT_TYPES:
            reject("CONTROL_JOURNAL_EVENT_UNSUPPORTED")
        with BoundedShadowLock(self.lock_path):
            records = self.records()
            request_id = command.payload()["request_id"] if command is not None else ""
            record = dict(schema_version=JOURNAL_SCHEMA, sequence=len(records),
                previous_hash=records[-1]["entry_hash"] if records else GENESIS, event_type=event_type,
                request_id=request_id, command_fingerprint=command.fingerprint() if command else "",
                command=command.as_dict() if command else None, snapshot_id=snapshot_id,
                previous_selected=previous_selected, effective_selected=effective_selected,
                actor=actor, role=role, at=at, outcome=outcome, audit_event_id=audit_event_id,
                audit_receipt=audit_receipt, generation=generation, reconciles=list(reconciles))
            record["event_id"] = event_id(record)
            record["entry_hash"] = fingerprint(record)
            safe_path(self.workspace, self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "ab") as handle:
                handle.write((canonical_json(record) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            sync_directory(self.workspace)
            if self.records()[-1] != record:
                reject("CONTROL_JOURNAL_READBACK_MISMATCH")
            return record
