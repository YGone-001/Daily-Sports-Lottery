"""隔离快照文件、严格路径与有界跨平台锁；文件存在不建立选择权威。"""
from __future__ import annotations

from dataclasses import fields
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from uuid import uuid4

from utils.mapping_governance import load_trusted_public_key, reconstruct_proposal_from_evidence
from utils.runtime_activation_authorization import RuntimeActivationAuthorization, ShadowActivationBinding, ShadowActivationSnapshot
from utils.shadow_activation_audit_store import _validate_workspace, _validate_audit_file
from utils.source_identity import canonical_json, fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError, verify_trusted_artifact
from utils.trusted_time_authority import parse_utc

STORE_SCHEMA = "1.0.0"
PUBLICATION_PURPOSE = "SHADOW_SNAPSHOT_PUBLICATION_REQUEST"
PUBLICATION_FIELDS = frozenset({"control_schema_version", "purpose", "dataset_identity", "snapshot_id",
    "snapshot_fingerprint", "baseline_fingerprint", "policy_version", "evaluator_version", "activation_ids",
    "activation_fingerprints", "lifecycle_fingerprint", "revocation_fingerprint", "governance_fingerprint",
    "actor", "role", "request_id", "issued_at", "expires_at", "reason"})


class ShadowControlError(TrustedArtifactError):
    """稳定拒绝码；禁止默默修复或忽略不完整证据。"""


def reject(code):
    raise ShadowControlError(code, rejection_codes=(code,))


def strict_artifact(payload):
    artifact = SignedTrustedArtifact.from_dict(payload)
    if artifact.as_dict() != payload:
        reject("PUBLICATION_EVIDENCE_MISMATCH")
    return artifact


def workspace_path(workspace):
    raw = str(workspace)
    if not raw or not Path(raw).is_absolute() or ".." in Path(raw).parts or raw.startswith("\\\\"):
        reject("CONTROL_WORKSPACE_INVALID")
    _reject_links(Path(raw))
    return Path(_validate_workspace(raw))


def _reject_links(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            reject("CONTROL_PATH_ALIAS")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            reject("CONTROL_PATH_ALIAS")


def safe_path(root, path):
    root = workspace_path(root)
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or ":" in path.name:
        reject("CONTROL_PATH_INVALID")
    _reject_links(path)
    return Path(_validate_audit_file(str(path), str(root)))


class BoundedShadowLock:
    """固定顺序调用的合作进程锁；线程和 POSIX/Windows OS 等待都有限。"""
    _guards = {}
    _guard_lock = threading.Lock()

    def __init__(self, path, timeout=5.0):
        self.path = str(safe_path(Path(path).parent, Path(path)))
        self.timeout = timeout
        self.fd = None
        self.guard = None

    def __enter__(self):
        with self._guard_lock:
            self.guard = self._guards.setdefault(self.path, threading.Lock())
        if not self.guard.acquire(timeout=self.timeout):
            reject("CONTROL_LOCK_TIMEOUT")
        try:
            safe_path(Path(self.path).parent, Path(self.path))
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if os.fstat(self.fd).st_nlink != 1:
                reject("CONTROL_PATH_ALIAS")
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        reject("CONTROL_LOCK_TIMEOUT")
                    time.sleep(0.01)
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
            self.guard.release()
            raise

    def __exit__(self, *args):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.guard.release()


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_json(root, path, payload, *, immutable=False):
    path = safe_path(root, path)
    temporary = safe_path(root, path.parent / (".pending-" + uuid4().hex))
    data = (canonical_json(payload) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    safe_path(root, path)
    if immutable:
        os.link(temporary, path)  # Atomic no-overwrite; includes Windows NTFS.
        temporary.unlink()
    else:
        os.replace(temporary, path)
    sync_directory(path.parent)


def strict_json_file(root, path):
    path = safe_path(root, path)
    data = path.read_bytes()
    try:
        value = json.loads(data)
        if data != (canonical_json(value) + "\n").encode("utf-8"):
            reject("CONTROL_FILE_NONCANONICAL")
        return value
    except (ValueError, UnicodeError) as exc:
        raise ShadowControlError("corrupt or truncated JSON", rejection_codes=("CONTROL_FILE_CORRUPT",)) from exc


def decode_snapshot(payload):
    names = {f.name for f in fields(ShadowActivationSnapshot)} | {"prototype_only", "is_production_authority"}
    if not isinstance(payload, dict) or set(payload) != names or payload["prototype_only"] is not True or payload["is_production_authority"] is not False:
        reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
    kwargs = {f.name: payload[f.name] for f in fields(ShadowActivationSnapshot)}
    if any(not isinstance(value, str) for key, value in kwargs.items() if key not in ("activation_ids", "bindings")):
        reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
    if not isinstance(kwargs["activation_ids"], list) or any(not isinstance(x, str) for x in kwargs["activation_ids"]):
        reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
    if not isinstance(kwargs["bindings"], list):
        reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
    binding_names = {f.name for f in fields(ShadowActivationBinding)}
    for binding in kwargs["bindings"]:
        if not isinstance(binding, dict) or set(binding) != binding_names or any(not isinstance(v, str) for v in binding.values()):
            reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
    kwargs["bindings"] = tuple(ShadowActivationBinding(**b) for b in kwargs["bindings"])
    kwargs["activation_ids"] = tuple(kwargs["activation_ids"])
    snapshot = ShadowActivationSnapshot(**kwargs)
    if snapshot.snapshot_id != snapshot.snapshot_fingerprint or snapshot.content_fingerprint() != snapshot.snapshot_id:
        reject("PERSISTENT_SNAPSHOT_FINGERPRINT_MISMATCH")
    return snapshot


def publication_evidence(context):
    def raw(artifact):
        if not isinstance(artifact, SignedTrustedArtifact):
            reject("PUBLICATION_EVIDENCE_MISSING")
        return artifact.as_dict()
    life, rev = context.lifecycle_evidence, context.revocation_evidence
    return dict(authorizations=[dict(authorization=e.authorization.as_dict(), decision=e.decision.as_dict(),
        proposal=e.proposal.evidence_payload()) for e in sorted(life.authorizations, key=lambda e: e.authorization.activation_id)],
        governance=context.authoritative_view.as_dict(), lifecycle=dict(state=raw(life.state),
            checkpoint=raw(life.checkpoint), transitions=[raw(x) for x in life.transitions]),
        revocation=dict(state=raw(rev.state), checkpoint=raw(rev.checkpoint), events=[raw(x) for x in rev.events]),
        trusted_time=raw(context.trusted_time_evidence))


class PersistentShadowSnapshotStore:
    def __init__(self, workspace, *, dataset_identity, publisher_public_key_path):
        self.workspace = workspace_path(workspace)
        self.directory = safe_path(self.workspace, self.workspace / "snapshots")
        if not self.directory.exists():
            self.directory.mkdir()
        self.dataset_identity = dataset_identity
        self.publisher_public_key_path = publisher_public_key_path
        self.publisher_key_id = load_trusted_public_key(publisher_public_key_path)["public_key_id"]
        self.lock_path = safe_path(self.workspace, self.workspace / "snapshot-store.lock")

    def store_identity(self):
        return fingerprint(dict(schema=STORE_SCHEMA, workspace=str(self.workspace), dataset=self.dataset_identity))

    def _path(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not re.fullmatch("[0-9a-f]{64}", snapshot_id):
            reject("PERSISTENT_SNAPSHOT_ID_INVALID")
        return safe_path(self.workspace, self.directory / (snapshot_id + ".json"))

    def verify_snapshot(self, snapshot_id):
        envelope = strict_json_file(self.workspace, self._path(snapshot_id))
        return self._validate_envelope(envelope, snapshot_id)

    def _validate_envelope(self, envelope, snapshot_id):
        if set(envelope) != {"schema_version", "snapshot", "publication", "evidence", "published_at"} or envelope["schema_version"] != STORE_SCHEMA:
            reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
        snapshot = decode_snapshot(envelope["snapshot"])
        if snapshot.snapshot_id != snapshot_id or snapshot.dataset_identity != self.dataset_identity:
            reject("PERSISTENT_SNAPSHOT_SCOPE_MISMATCH")
        publication = strict_artifact(envelope["publication"])
        p = verify_trusted_artifact(publication, expected_artifact_type=PUBLICATION_PURPOSE,
            trusted_public_key_path=self.publisher_public_key_path, current_time=envelope["published_at"], max_age_seconds=300)
        if set(p) != PUBLICATION_FIELDS:
            reject("PERSISTENT_PUBLICATION_SCHEMA_INVALID")
        start, end, published = parse_utc(p["issued_at"]), parse_utc(p["expires_at"]), parse_utc(envelope["published_at"])
        expected = dict(control_schema_version=STORE_SCHEMA, purpose=PUBLICATION_PURPOSE,
            role="snapshot-publisher", dataset_identity=self.dataset_identity,
            baseline_fingerprint=snapshot.baseline_fingerprint, policy_version=snapshot.policy_version,
            evaluator_version=snapshot.evaluator_version, activation_ids=sorted(snapshot.activation_ids))
        if (any(fingerprint(p[k]) != fingerprint(v) for k, v in expected.items())
            or not start <= published < end or not 0 < (end-start).total_seconds() <= 300
            or any(not isinstance(p[k], str) or not p[k].strip() for k in ("actor", "request_id", "reason"))
            or len(p["request_id"]) > 128):
            reject("PERSISTENT_PUBLICATION_INVALID")
        if publication.signer_public_key_id != self.publisher_key_id or p["snapshot_id"] != snapshot_id or p["snapshot_fingerprint"] != snapshot_id:
            reject("PERSISTENT_PUBLICATION_INVALID")
        evidence = envelope["evidence"]
        try:
            if set(evidence) != {"authorizations", "governance", "lifecycle", "revocation", "trusted_time"}:
                reject("PUBLICATION_EVIDENCE_MISSING")
            auths = []
            for item in evidence["authorizations"]:
                if (set(item) != {"authorization", "decision", "proposal"}
                    or not isinstance(item["authorization"], dict)
                    or any(not isinstance(v, str) for v in item["authorization"].values())):
                    reject("PUBLICATION_EVIDENCE_MISMATCH")
                auth = RuntimeActivationAuthorization.from_dict(item["authorization"])
                if auth.as_dict() != item["authorization"]:
                    reject("PUBLICATION_EVIDENCE_MISMATCH")
                decision = item["decision"]
                proposal = reconstruct_proposal_from_evidence(item["proposal"])
                if (auth.eligibility_decision_fingerprint != fingerprint(decision)
                    or auth.proposal_content_fingerprint != proposal.content_fingerprint()
                    or proposal.evidence_payload() != item["proposal"]):
                    reject("PUBLICATION_EVIDENCE_MISMATCH")
                if auth.activation_id in snapshot.activation_ids:
                    auths.append((auth.activation_id, auth.fingerprint()))
            if fingerprint(p["activation_fingerprints"]) != fingerprint(sorted(auths)):
                reject("PUBLICATION_EVIDENCE_MISMATCH")
            for group, key in (("lifecycle", "lifecycle_fingerprint"), ("revocation", "revocation_fingerprint")):
                event_key = "transitions" if group == "lifecycle" else "events"
                if set(evidence[group]) != {"state", "checkpoint", event_key} or not isinstance(evidence[group][event_key], list):
                    reject("PUBLICATION_EVIDENCE_MISSING")
                state = strict_artifact(evidence[group]["state"])
                if state.fingerprint() != p[key]:
                    reject("PUBLICATION_EVIDENCE_MISMATCH")
                references = state.payload()["transitions" if group == "lifecycle" else "revocation_event_fingerprints"]
                if sorted({strict_artifact(e).fingerprint() for e in evidence[group][event_key]}) != references:
                    reject("PUBLICATION_EVIDENCE_MISSING")
                if strict_artifact(evidence[group]["checkpoint"]).payload()["state_fingerprint"] != state.fingerprint():
                    reject("PUBLICATION_EVIDENCE_MISMATCH")
            strict_artifact(evidence["trusted_time"])
            governance = evidence["governance"]
            attestation = strict_artifact(governance["attestation"])
            facts = {k: v for k, v in governance.items() if k not in ("attestation", "view_fingerprint")}
            expected_attestation = dict(facts, issued_at=facts["verified_at"], view_fingerprint=fingerprint(facts))
            if (governance["view_fingerprint"] != p["governance_fingerprint"]
                or fingerprint(facts) != p["governance_fingerprint"] or attestation.payload() != expected_attestation):
                reject("PUBLICATION_EVIDENCE_MISMATCH")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ShadowControlError("missing publication evidence", rejection_codes=("PUBLICATION_EVIDENCE_MISSING",)) from exc
        return envelope

    def get(self, snapshot_id):
        return decode_snapshot(self.verify_snapshot(snapshot_id)["snapshot"])

    def list_snapshot_ids(self):
        safe_path(self.workspace, self.directory)
        ids = []
        for path in self.directory.iterdir():
            safe_path(self.workspace, path)
            if not re.fullmatch("[0-9a-f]{64}\\.json", path.name):
                reject("PERSISTENT_STORE_UNCOMMITTED_FILE")
            ids.append(path.stem)
        return tuple(sorted(ids))

    def verify_store(self):
        return tuple((sid, fingerprint(self.verify_snapshot(sid))) for sid in self.list_snapshot_ids())

    def recover_read_only(self):
        return dict(status="RECOVERED_READ_ONLY", inventory=self.verify_store(), consumption_enabled=False)

    def publish(self, snapshot, *, publication, evidence, published_at):
        if not isinstance(snapshot, ShadowActivationSnapshot):
            reject("PERSISTENT_SNAPSHOT_SCHEMA_INVALID")
        decode_snapshot(snapshot.as_dict())
        target = self._path(snapshot.snapshot_id)
        with BoundedShadowLock(self.lock_path):
            if not isinstance(publication, SignedTrustedArtifact):
                reject("PUBLICATION_EVIDENCE_MISSING")
            envelope = dict(schema_version=STORE_SCHEMA, snapshot=snapshot.as_dict(), publication=publication.as_dict(),
                evidence=evidence, published_at=published_at)
            self._validate_envelope(envelope, snapshot.snapshot_id)
            if target.exists():
                if self.get(snapshot.snapshot_id) != snapshot:
                    reject("PERSISTENT_SNAPSHOT_ID_CONFLICT")
                return dict(idempotent=True, snapshot_id=snapshot.snapshot_id)
            atomic_json(self.workspace, target, envelope, immutable=True)
            self.verify_snapshot(snapshot.snapshot_id)
            return dict(idempotent=False, snapshot_id=snapshot.snapshot_id)
