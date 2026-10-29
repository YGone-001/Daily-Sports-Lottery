"""Isolated root bootstrap. No production registration, persistence or signing.

All signatures use trusted_artifacts. Pins and continuity are supplied by an
independent harness, never inferred from a request or the bundle under review.
The adapter adds fixed key provenance; existing engines retain authorization.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import re
from threading import RLock
from types import MappingProxyType
from typing import Mapping

from utils.mapping_governance import (
    _crypto_backend, load_trusted_public_key, public_key_id, verify_mapping_approval,
)
from utils.source_identity import canonical_json, fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError, verify_trusted_artifact
from utils.trusted_time_authority import parse_utc, TrustedTimeVerifier

SCHEMA = "1.0.0"
POLICY = "offline-root-policy-v1"
BUNDLE = "OFFLINE_TRUST_ROOT_BUNDLE"
TRANSITION = "OFFLINE_TRUST_ROOT_TRANSITION"
APPROVAL = "OFFLINE_TRUST_ROOT_CHANGE_APPROVAL"
BOOTSTRAP_STATES = (
    "BOOTSTRAP_NOT_ESTABLISHED", "BOOTSTRAP_VERIFIED", "ROOT_TRANSITION_REQUIRED",
    "ROOT_UNTRUSTED", "ROOT_VERSION_ROLLBACK", "ROOT_REVOKED", "CONTINUITY_UNVERIFIED",
)
# These grants name existing purposes; they do not introduce new role semantics.
ROLE_PURPOSES = MappingProxyType({
    "root-bundle": (BUNDLE, TRANSITION),
    "owner-approval": ("CANONICAL_MAPPING_GOVERNANCE_APPROVE",),
    "activation": ("RUNTIME_MAPPING_ACTIVATION_AUTHORIZE",),
    "governance-view": ("GOVERNANCE_VIEW_ATTESTATION",),
    "trusted-time": ("RUNTIME_ACTIVATION_TIME_ATTESTATION",),
    "lifecycle": ("RUNTIME_ACTIVATION_SUPERSESSION", "RUNTIME_ACTIVATION_LIFECYCLE_STATE"),
    "revocation": ("RUNTIME_ACTIVATION_REVOCATION", "RUNTIME_ACTIVATION_REVOCATION_STATE"),
    "audit-checkpoint": ("SHADOW_AUDIT_CHAIN_CHECKPOINT",),
    "control-checkpoint": ("SHADOW_SNAPSHOT_CONTROL_CHECKPOINT",),
    "recovery": ("SHADOW_RECOVERY_EVIDENCE",),
    "ledger-checkpoint": ("GOVERNANCE_LEDGER_CHECKPOINT",),
    "canonical-publisher": ("CANONICAL_BASELINE_MANIFEST",),
    "governance-revocation": ("ACTIVATION_REVOCATION_MANIFEST",),
    "observation": ("SOURCE_OBSERVATION_ATTESTATION",),
    "lifecycle-checkpoint": ("RUNTIME_ACTIVATION_LIFECYCLE_CHECKPOINT",),
    "revocation-checkpoint": ("RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT",),
    "publisher": ("SHADOW_SNAPSHOT_PUBLICATION_REQUEST",),
    "selector": ("SHADOW_SNAPSHOT_SELECTION_REQUEST",),
})
REQUIRED_ROLES = frozenset(("root-bundle", "owner-approval", "activation", "governance-view",
    "trusted-time", "lifecycle", "revocation", "audit-checkpoint", "control-checkpoint", "recovery"))
ROLE_FIELDS = frozenset(("authority_identity", "signer_role", "public_key_pem", "public_key_fingerprint",
    "authorized_purposes", "signing_not_before", "signing_not_after"))
BUNDLE_FIELDS = frozenset(("schema_version", "bundle_identity", "environment_identity", "dataset_identity",
    "root_policy_version", "root_generation", "minimum_accepted_version", "authorities",
    "revoked_key_identities", "predecessor_bundle_fingerprint", "issuer_identity",
    "independent_approval_evidence_reference", "bundle_fingerprint", "issued_at"))


class BootstrapError(TrustedArtifactError):
    """A stable fail-closed bootstrap or fixed-authority rejection."""


def deny(code):
    raise BootstrapError(code, rejection_codes=(code,))


def require(condition, code):
    if not condition:
        deny(code)


def key_fingerprint(pem: str) -> str:
    """Parse public material only; signature operations stay in the existing engine."""
    try:
        backend = _crypto_backend()
        serialization = backend["serialization"]
        key = serialization.load_pem_public_key(pem.encode("ascii"))
        require(isinstance(key, backend["Ed25519PublicKey"]), "ROOT_KEY_MALFORMED")
        canonical = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        require(canonical == pem.encode("ascii"), "ROOT_KEY_MALFORMED")
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return public_key_id(raw)
    except BootstrapError:
        raise
    except Exception as exc:
        raise BootstrapError("ROOT_KEY_MALFORMED", rejection_codes=("ROOT_KEY_MALFORMED",)) from exc


def bundle_body(*, bundle_identity, environment_identity, dataset_identity, root_generation,
                minimum_accepted_version, authorities, revoked_key_identities=(),
                predecessor_bundle_fingerprint="", issuer_identity, approval_reference, issued_at):
    """Unsigned deterministic body. No key generation, signing or implicit approval."""
    body = dict(schema_version=SCHEMA, bundle_identity=bundle_identity,
        environment_identity=environment_identity, dataset_identity=dataset_identity,
        root_policy_version=POLICY, root_generation=root_generation,
        minimum_accepted_version=minimum_accepted_version,
        authorities=sorted((dict(a) for a in authorities), key=lambda a: a["signer_role"]),
        revoked_key_identities=sorted(revoked_key_identities),
        predecessor_bundle_fingerprint=predecessor_bundle_fingerprint,
        issuer_identity=issuer_identity, independent_approval_evidence_reference=approval_reference,
        issued_at=issued_at)
    return {**body, "bundle_fingerprint": fingerprint(body)}


def role_changes(old, new):
    previous = {} if old is None else {r["signer_role"]: r for r in old["authorities"]}
    current = {r["signer_role"]: r for r in new["authorities"]}
    return [dict(signer_role=role, old_assignment_fingerprint=fingerprint(previous[role]) if role in previous else "",
                 new_assignment_fingerprint=fingerprint(current[role]) if role in current else "")
            for role in sorted(previous.keys() | current.keys()) if previous.get(role) != current.get(role)]


def approval_body(old, new, *, approver_identity, requester_identity, executor_identity, issued_at):
    return dict(schema_version=SCHEMA, environment_identity=new["environment_identity"],
        dataset_identity=new["dataset_identity"], root_policy_version=new["root_policy_version"],
        approval_id=new["independent_approval_evidence_reference"],
        old_bundle_fingerprint="" if old is None else old["bundle_fingerprint"],
        new_bundle_fingerprint=new["bundle_fingerprint"], old_generation=0 if old is None else old["root_generation"],
        new_generation=new["root_generation"], approved_role_transitions=role_changes(old, new),
        approver_identity=approver_identity, requester_identity=requester_identity,
        executor_identity=executor_identity, issued_at=issued_at)


def transition_body(old, new, approval):
    p = approval.payload()
    return dict(schema_version=SCHEMA, environment_identity=new["environment_identity"],
        dataset_identity=new["dataset_identity"], root_policy_version=new["root_policy_version"],
        old_bundle_identity=old["bundle_identity"], new_bundle_identity=new["bundle_identity"],
        old_generation=old["root_generation"], new_generation=new["root_generation"],
        predecessor_bundle_fingerprint=old["bundle_fingerprint"], new_bundle_fingerprint=new["bundle_fingerprint"],
        approved_role_transitions=role_changes(old, new), approval_fingerprint=approval.fingerprint(),
        issuer_identity=old["issuer_identity"], requester_identity=p["requester_identity"],
        executor_identity=p["executor_identity"], issued_at=p["issued_at"])


@dataclass(frozen=True)
class BootstrapPolicy:
    environment_identity: str
    dataset_identity: str
    initial_root_fingerprint: str | None
    initial_root_public_key_path: str | None
    approval_authority_identity: str
    approval_key_fingerprint: str
    approval_public_key_path: str
    minimum_accepted_version: int = 1
    root_policy_version: str = POLICY


@dataclass(frozen=True)
class ContinuityEvidence:
    """Independently provisioned harness evidence, NOT writable local metadata.

    The harness must obtain these values before accepting application input.
    The reference is an audit label, not a signature or production service.
    """
    environment_identity: str
    dataset_identity: str
    expected_generation: int
    expected_bundle_fingerprint: str
    minimum_accepted_version: int
    reference: str


@dataclass(frozen=True)
class VerifiedBootstrap:
    payload_json: str
    _verifier: object = field(repr=False, compare=False)
    bundle_artifact_json: str = field(repr=False)
    approval_artifact_json: str = field(repr=False)
    transition_artifact_json: str | None = field(repr=False)

    def body(self):
        import json
        return json.loads(self.payload_json)

    def require_current(self):
        require(type(self._verifier) is TrustRootVerifier, "BOOTSTRAP_NOT_ESTABLISHED")
        TrustRootVerifier.require_current(self._verifier, self)

    def evidence(self):
        """Copies of actual authenticated envelopes, retained for session audit."""
        import json
        return dict(bundle=json.loads(self.bundle_artifact_json), approval=json.loads(self.approval_artifact_json),
            transition=json.loads(self.transition_artifact_json) if self.transition_artifact_json else None,
            synthetic_only=True, production_authorized=False)


class TrustRootVerifier:
    """Serialized isolated session. Restart requires new independent continuity."""

    def __init__(self, policy: BootstrapPolicy):
        require(isinstance(policy, BootstrapPolicy), "ROOT_POLICY_INVALID")
        require(all(isinstance(value, str) and 0 < len(value.strip()) <= 256 for value in
                    (policy.environment_identity, policy.dataset_identity, policy.approval_authority_identity))
            and type(policy.minimum_accepted_version) is int and policy.minimum_accepted_version > 0
            and policy.root_policy_version == POLICY, "ROOT_POLICY_INVALID")
        self._policy = policy
        self._active = None
        self._watermark = None
        self._ever_verified = False
        self._history = {}
        self._root_path = None
        self._lock = RLock()

    @property
    def status(self):
        with self._lock:
            if self._ever_verified and (self._active is None or self._watermark is None):
                return "CONTINUITY_UNVERIFIED"
            return "BOOTSTRAP_VERIFIED" if self._active is not None else "BOOTSTRAP_NOT_ESTABLISHED"

    @staticmethod
    def _pin(path, expected):
        require(isinstance(expected, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", expected) is not None,
                "BOOTSTRAP_NOT_ESTABLISHED" if not expected else "ROOT_UNTRUSTED")
        try:
            require(path is not None and Path(path).is_absolute(), "ROOT_UNTRUSTED")
            require(load_trusted_public_key(path)["public_key_id"] == expected, "ROOT_UNTRUSTED")
        except BootstrapError:
            raise
        except Exception as exc:
            raise BootstrapError("ROOT_UNTRUSTED", rejection_codes=("ROOT_UNTRUSTED",)) from exc

    def _body(self, artifact):
        require(isinstance(artifact, SignedTrustedArtifact) and artifact.artifact_type == BUNDLE,
                "ROOT_BUNDLE_INVALID")
        body = artifact.payload()
        require(isinstance(body, dict) and set(body) == BUNDLE_FIELDS, "ROOT_BUNDLE_INVALID")
        require(body["schema_version"] == SCHEMA, "ROOT_SCHEMA_UNSUPPORTED")
        policy = self._policy
        require(body["environment_identity"] == policy.environment_identity, "ROOT_ENVIRONMENT_MISMATCH")
        require(body["dataset_identity"] == policy.dataset_identity, "ROOT_DATASET_MISMATCH")
        require(body["root_policy_version"] == policy.root_policy_version, "ROOT_POLICY_INVALID")
        for name in ("bundle_identity", "issuer_identity", "independent_approval_evidence_reference"):
            require(isinstance(body[name], str) and 0 < len(body[name].strip()) <= 256, "ROOT_BUNDLE_INVALID")
        require(type(body["root_generation"]) is int and type(body["minimum_accepted_version"]) is int
            and 0 < body["minimum_accepted_version"] <= body["root_generation"] < 2**63, "ROOT_BUNDLE_INVALID")
        require(body["root_generation"] >= policy.minimum_accepted_version, "ROOT_VERSION_ROLLBACK")
        require(body["minimum_accepted_version"] >= policy.minimum_accepted_version, "ROOT_VERSION_ROLLBACK")
        require(body["bundle_fingerprint"] == fingerprint({k: v for k, v in body.items() if k != "bundle_fingerprint"}),
                "ROOT_BUNDLE_TAMPERED")
        require(body["predecessor_bundle_fingerprint"] == "" or isinstance(body["predecessor_bundle_fingerprint"], str)
            and re.fullmatch(r"[0-9a-f]{64}", body["predecessor_bundle_fingerprint"]) is not None, "ROOT_BUNDLE_INVALID")
        records = body["authorities"]
        require(isinstance(records, list) and 0 < len(records) <= len(ROLE_PURPOSES), "ROOT_ROLE_INVALID")
        roles, identities, keys = set(), set(), set()
        for record in records:
            require(isinstance(record, dict) and set(record) == ROLE_FIELDS, "ROOT_ROLE_INVALID")
            role = record["signer_role"]
            require(isinstance(role, str) and role in ROLE_PURPOSES and role not in roles, "ROOT_ROLE_CONFLICT")
            identity = record["authority_identity"]
            require(isinstance(identity, str) and identity.strip() and identity not in identities, "ROOT_AUTHORITY_DUPLICATE")
            key = key_fingerprint(record["public_key_pem"])
            require(key == record["public_key_fingerprint"], "ROOT_KEY_FINGERPRINT_MISMATCH")
            require(key not in keys and key != policy.approval_key_fingerprint, "ROOT_ROLE_CONFLICT")
            require(record["authorized_purposes"] == sorted(ROLE_PURPOSES[role]), "ROOT_PURPOSE_INVALID")
            require(parse_utc(record["signing_not_before"]) < parse_utc(record["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            roles.add(role); identities.add(identity); keys.add(key)
        require(REQUIRED_ROLES <= roles, "ROOT_MANDATORY_ROLE_MISSING")
        require(records == sorted(records, key=lambda r: r["signer_role"]), "ROOT_BUNDLE_NONCANONICAL")
        revoked = body["revoked_key_identities"]
        require(isinstance(revoked, list) and len(revoked) <= 1024
            and all(isinstance(k, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", k) for k in revoked)
            and revoked == sorted(set(revoked)), "ROOT_REVOCATION_INVALID")
        root = next(r for r in records if r["signer_role"] == "root-bundle")
        require(root["authority_identity"] == body["issuer_identity"], "ROOT_ISSUER_MISMATCH")
        require(root["public_key_fingerprint"] not in revoked, "ROOT_REVOKED")
        return body

    @staticmethod
    def _verify(artifact, purpose, path, now, age, expected_key_id):
        require(isinstance(age, (int, float)) and math.isfinite(age) and age >= 0, "ROOT_FRESHNESS_INVALID")
        require(isinstance(artifact, SignedTrustedArtifact), "ROOT_ROLE_EVIDENCE_INVALID")
        require(isinstance(artifact.payload(), dict), "ROOT_ROLE_EVIDENCE_INVALID")
        parse_utc(now)
        payload = verify_trusted_artifact(artifact, expected_artifact_type=purpose,
            trusted_public_key_path=path, current_time=now, max_age_seconds=age)
        # The shared engine opens a path. Compare its authenticated signer with
        # the fixed fingerprint as well, even if that file changed after _pin.
        require(artifact.signer_public_key_id == expected_key_id, "ROOT_UNTRUSTED")
        return payload

    def _approval(self, old, new, approval, now, age):
        p = self._policy
        self._pin(p.approval_public_key_path, p.approval_key_fingerprint)
        require(approval is not None, "ROOT_APPROVAL_MISSING")
        a = self._verify(approval, APPROVAL, p.approval_public_key_path, now, age, p.approval_key_fingerprint)
        require(set(a) == set(approval_body(old, new, approver_identity=p.approval_authority_identity,
            requester_identity="requester", executor_identity="executor", issued_at=now)), "ROOT_APPROVAL_INVALID")
        actors = (a["approver_identity"], a["requester_identity"], a["executor_identity"])
        require(all(isinstance(x, str) and x.strip() for x in actors) and len(set(actors)) == 3,
                "ROOT_APPROVAL_SEPARATION_INVALID")
        require(a["approver_identity"] == p.approval_authority_identity, "ROOT_APPROVAL_INVALID")
        expected = approval_body(old, new, approver_identity=p.approval_authority_identity,
            requester_identity=a["requester_identity"], executor_identity=a["executor_identity"], issued_at=a["issued_at"])
        require(a == expected, "ROOT_APPROVAL_INVALID")
        expected_executor = new["issuer_identity"] if old is None else old["issuer_identity"]
        require(a["executor_identity"] == expected_executor, "ROOT_APPROVAL_SEPARATION_INVALID")
        return a

    def _continuity(self, body, evidence):
        require(isinstance(evidence, ContinuityEvidence), "CONTINUITY_UNVERIFIED")
        require(evidence.environment_identity == self._policy.environment_identity
            and evidence.dataset_identity == self._policy.dataset_identity
            and isinstance(evidence.reference, str) and bool(evidence.reference.strip()), "CONTINUITY_UNVERIFIED")
        require(type(evidence.expected_generation) is int and type(evidence.minimum_accepted_version) is int
            and 0 < evidence.minimum_accepted_version <= evidence.expected_generation, "CONTINUITY_UNVERIFIED")
        require(body["root_generation"] >= max(evidence.expected_generation, evidence.minimum_accepted_version), "ROOT_VERSION_ROLLBACK")
        require(body["root_generation"] == evidence.expected_generation
            and body["minimum_accepted_version"] >= evidence.minimum_accepted_version
            and body["bundle_fingerprint"] == evidence.expected_bundle_fingerprint, "CONTINUITY_UNVERIFIED")

    def _install(self, body, root_path, artifact, approval, transition=None):
        handle = VerifiedBootstrap(canonical_json(body), self, artifact.to_json(), approval.to_json(),
            transition.to_json() if transition is not None else None)
        self._history[body["bundle_fingerprint"]] = handle
        self._active = handle
        self._root_path = os.fspath(root_path)
        self._watermark = (body["root_generation"], body["bundle_fingerprint"])
        self._ever_verified = True
        return handle

    def bootstrap(self, artifact, *, approval, continuity, current_time, max_age_seconds=60):
        with self._lock:
            require(not self._ever_verified or self.status != "CONTINUITY_UNVERIFIED", "CONTINUITY_UNVERIFIED")
            body = self._body(artifact)
            if self._ever_verified:
                require(body["root_generation"] >= self._watermark[0], "ROOT_VERSION_ROLLBACK")
                deny("ROOT_TRANSITION_REQUIRED")
            p = self._policy
            self._pin(p.initial_root_public_key_path, p.initial_root_fingerprint)
            root = next(r for r in body["authorities"] if r["signer_role"] == "root-bundle")
            require(root["public_key_fingerprint"] == p.initial_root_fingerprint, "ROOT_UNTRUSTED")
            self._verify(artifact, BUNDLE, p.initial_root_public_key_path, current_time, max_age_seconds, p.initial_root_fingerprint)
            require(parse_utc(root["signing_not_before"]) <= parse_utc(current_time) < parse_utc(root["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            require(parse_utc(root["signing_not_before"]) <= parse_utc(body["issued_at"]) < parse_utc(root["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            # Initial registration is approved independently, not by the root itself.
            self._approval(None, body, approval, current_time, max_age_seconds)
            self._continuity(body, continuity)
            return self._install(body, p.initial_root_public_key_path, artifact, approval)

    def require_current(self, handle):
        with self._lock:
            require(self.status != "CONTINUITY_UNVERIFIED", "CONTINUITY_UNVERIFIED")
            require(self._active is not None, "BOOTSTRAP_NOT_ESTABLISHED")
            require(handle is self._active, "ROOT_VERSION_ROLLBACK")
            body = handle.body()
            require(self._watermark == (body["root_generation"], body["bundle_fingerprint"]), "CONTINUITY_UNVERIFIED")
            self._pin(self._root_path, next(r["public_key_fingerprint"] for r in body["authorities"] if r["signer_role"] == "root-bundle"))

    def transition(self, artifact, *, transition, approval, new_root_public_key_path,
                   continuity, current_time, max_age_seconds=60):
        with self._lock:
            self.require_current(self._active)
            old = self._active.body()
            new = self._body(artifact)
            require(new["root_generation"] > old["root_generation"], "ROOT_VERSION_ROLLBACK")
            require(new["root_generation"] == old["root_generation"] + 1
                and new["minimum_accepted_version"] >= old["minimum_accepted_version"]
                and new["predecessor_bundle_fingerprint"] == old["bundle_fingerprint"]
                and new["bundle_identity"] != old["bundle_identity"], "ROOT_TRANSITION_INVALID")
            self._approval(old, new, approval, current_time, max_age_seconds)
            require(transition is not None, "ROOT_TRANSITION_REQUIRED")
            old_root = next(r for r in old["authorities"] if r["signer_role"] == "root-bundle")
            actual = self._verify(transition, TRANSITION, self._root_path, current_time, max_age_seconds, old_root["public_key_fingerprint"])
            require(actual == transition_body(old, new, approval), "ROOT_TRANSITION_INVALID")
            require(parse_utc(old_root["signing_not_before"]) <= parse_utc(actual["issued_at"])
                <= parse_utc(current_time) < parse_utc(old_root["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            previous = {r["signer_role"]: r for r in old["authorities"]}
            current = {r["signer_role"]: r for r in new["authorities"]}
            retired = {r["public_key_fingerprint"] for role, r in previous.items()
                if role not in current or r["public_key_fingerprint"] != current[role]["public_key_fingerprint"]}
            require(set(old["revoked_key_identities"]) | retired <= set(new["revoked_key_identities"]), "ROOT_RETIREMENT_INVALID")
            root = current["root-bundle"]
            self._pin(new_root_public_key_path, root["public_key_fingerprint"])
            self._verify(artifact, BUNDLE, new_root_public_key_path, current_time, max_age_seconds, root["public_key_fingerprint"])
            require(parse_utc(root["signing_not_before"]) <= parse_utc(current_time) < parse_utc(root["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            require(parse_utc(root["signing_not_before"]) <= parse_utc(new["issued_at"]) < parse_utc(root["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            self._continuity(new, continuity)
            return self._install(new, new_root_public_key_path, artifact, approval, transition)

    def inspect_historical(self, bundle_fingerprint, role, artifact, *, public_key_path, current_time):
        """Math/period inspection only; never returns current authorization."""
        with self._lock:
            require(bundle_fingerprint in self._history, "ROOT_HISTORY_UNKNOWN")
            body = self._history[bundle_fingerprint].body()
            record = next((r for r in body["authorities"] if r["signer_role"] == role), None)
            require(record is not None and artifact.artifact_type in record["authorized_purposes"], "ROOT_PURPOSE_INVALID")
            self._pin(public_key_path, record["public_key_fingerprint"])
            issued = parse_utc(artifact.payload().get("issued_at"))
            require(parse_utc(record["signing_not_before"]) <= issued < parse_utc(record["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
            age = max(0, (parse_utc(current_time) - issued).total_seconds())
            self._verify(artifact, artifact.artifact_type, public_key_path, current_time, age, record["public_key_fingerprint"])
            return dict(cryptographically_valid=True, current_authorization_eligible=False,
                bundle_fingerprint=bundle_fingerprint, revoked_in_current_bundle=bool(self._active
                    and record["public_key_fingerprint"] in self._active.body()["revoked_key_identities"]))


@dataclass(frozen=True, init=False)
class FixedAuthorityContext:
    """Read-only adapter. All application-supplied key paths are checked, not trusted."""
    bootstrap: VerifiedBootstrap
    key_paths: Mapping[str, str]
    environment_identity: str
    dataset_identity: str
    _records: Mapping[str, str] = field(repr=False)

    def __init__(self, bootstrap, *, key_paths):
        require(type(bootstrap) is VerifiedBootstrap, "BOOTSTRAP_NOT_ESTABLISHED")
        bootstrap.require_current()
        body = bootstrap.body()
        records = {r["signer_role"]: canonical_json(r) for r in body["authorities"]}
        require(set(key_paths) == set(records), "FIXED_AUTHORITY_PATHS_INVALID")
        paths = {}
        for role, path in key_paths.items():
            require(Path(path).is_absolute() and not Path(path).is_symlink(), "FIXED_AUTHORITY_PATHS_INVALID")
            normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
            TrustRootVerifier._pin(normalized, self._record_from(records, role)["public_key_fingerprint"])
            paths[role] = normalized
        object.__setattr__(self, "bootstrap", bootstrap)
        object.__setattr__(self, "key_paths", MappingProxyType(paths))
        object.__setattr__(self, "_records", MappingProxyType(records))
        object.__setattr__(self, "environment_identity", body["environment_identity"])
        object.__setattr__(self, "dataset_identity", body["dataset_identity"])

    @staticmethod
    def _record_from(records, role):
        import json
        require(role in records, "ROOT_ROLE_UNAUTHORIZED")
        return json.loads(records[role])

    def key_path(self, role, purpose, *, current_time, dataset_identity=None, environment_identity=None):
        self.bootstrap.require_current()
        require(dataset_identity is None or dataset_identity == self.dataset_identity, "ROOT_DATASET_MISMATCH")
        require(environment_identity is None or environment_identity == self.environment_identity, "ROOT_ENVIRONMENT_MISMATCH")
        record = self._record_from(self._records, role)
        require(purpose in record["authorized_purposes"], "ROOT_PURPOSE_INVALID")
        require(record["public_key_fingerprint"] not in self.bootstrap.body()["revoked_key_identities"], "ROOT_REVOKED")
        require(parse_utc(record["signing_not_before"]) <= parse_utc(current_time) < parse_utc(record["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
        path = self.key_paths[role]
        TrustRootVerifier._pin(path, record["public_key_fingerprint"])
        return path

    def verify_artifact(self, role, artifact, *, current_time, max_age_seconds=60,
                        dataset_identity=None, environment_identity=None):
        require(isinstance(artifact, SignedTrustedArtifact), "ROOT_ROLE_EVIDENCE_INVALID")
        path = self.key_path(role, artifact.artifact_type, current_time=current_time,
            dataset_identity=dataset_identity, environment_identity=environment_identity)
        record = self._record_from(self._records, role)
        payload = TrustRootVerifier._verify(artifact, artifact.artifact_type, path, current_time, max_age_seconds, record["public_key_fingerprint"])
        require(payload.get("dataset_identity") == self.dataset_identity, "ROOT_DATASET_MISMATCH")
        require(payload.get("environment_identity", self.environment_identity) == self.environment_identity, "ROOT_ENVIRONMENT_MISMATCH")
        if role in ("trusted-time", "lifecycle", "revocation") and "authority_identity" in payload:
            require(payload["authority_identity"] == self._record_from(self._records, role)["authority_identity"], "FIXED_AUTHORITY_IDENTITY_MISMATCH")
        require(parse_utc(record["signing_not_before"]) <= parse_utc(payload["issued_at"]) < parse_utc(record["signing_not_after"]), "ROOT_SIGNING_PERIOD_INVALID")
        self.bootstrap.require_current()
        return payload

    def trusted_time_verifier(self, freshness_reference, *, current_time):
        record = self._record_from(self._records, "trusted-time")
        # The existing verifier handles nonce, generation, rollback and freshness.
        path = self.key_path("trusted-time", ROLE_PURPOSES["trusted-time"][0], current_time=current_time)
        return TrustedTimeVerifier(self.dataset_identity, record["authority_identity"],
            path, freshness_reference)

    def verify_owner_approval(self, approval, *, current_time, **facts):
        require("trusted_public_key_path" not in facts, "FIXED_AUTHORITY_OVERRIDE")
        require(facts.get("dataset_identity") == self.dataset_identity, "ROOT_DATASET_MISMATCH")
        path = self.key_path("owner-approval", ROLE_PURPOSES["owner-approval"][0], current_time=current_time)
        result = verify_mapping_approval(approval, trusted_public_key_path=path, **facts)
        require(approval.get("public_key_id") == self._record_from(self._records, "owner-approval")["public_key_fingerprint"], "ROOT_UNTRUSTED")
        self.bootstrap.require_current()
        return result

    def bind_read_context(self, context, *, current_time, environment_identity=None, authority_paths=None):
        from utils.shadow_runtime_reader import ShadowReadContext
        require(isinstance(context, ShadowReadContext), "FIXED_AUTHORITY_CONTEXT_INVALID")
        require(context.dataset_identity == self.dataset_identity, "ROOT_DATASET_MISMATCH")
        require(environment_identity is None or environment_identity == self.environment_identity, "ROOT_ENVIRONMENT_MISMATCH")
        require(authority_paths is None or dict(authority_paths) == dict(self.key_paths), "FIXED_AUTHORITY_OVERRIDE")
        fields = {"trusted_public_key_path": "activation", "authoritative_view_trusted_public_key_path": "governance-view",
            "observation_trusted_public_key_path": "observation", "recovery_trusted_public_key_path": "recovery"}
        values = {}
        for name, role in fields.items():
            path = self.key_path(role, ROLE_PURPOSES[role][0], current_time=current_time)
            supplied = getattr(context, name)
            require(supplied is None or Path(supplied).is_absolute()
                and ".." not in Path(supplied).parts and os.path.normcase(os.path.abspath(os.fspath(supplied))) == path,
                    "FIXED_AUTHORITY_OVERRIDE")
            values[name] = path
        return replace(context, **values)

    def read_shadow(self, reader, *, observation, snapshot, read_context, observation_attestation):
        """Guard one existing fully strict Reader call; no new authorization engine."""
        from utils.shadow_runtime_reader import ShadowRuntimeReader
        self.bootstrap.require_current()
        require(isinstance(reader, ShadowRuntimeReader), "FIXED_AUTHORITY_CONTEXT_INVALID")
        modes = ("controlled_snapshot_selection", "independent_revocation", "verified_supersession",
            "authenticated_approval", "trusted_time", "authoritative_governance", "observation_attestation",
            "recovery_evidence_for_reenable")
        require(all(getattr(reader, "_require_" + mode, False) for mode in modes), "FIXED_AUTHORITY_STRICT_MODE_REQUIRED")
        strict = reader._strict_activation_verifier
        require(strict.require_authenticated_approval, "FIXED_AUTHORITY_STRICT_MODE_REQUIRED")
        now = strict.time_verifier.verify(read_context.trusted_time_evidence,
            dataset_identity=self.dataset_identity).effective_time
        life, rev, control = reader._supersession_verifier, reader._revocation_verifier, reader._snapshot_controller
        bindings = [(strict.time_verifier, "trusted_public_key_path", "trusted-time"),
            (strict, "governance_view_trusted_public_key_path", "governance-view"),
            (life, "lifecycle_public_key_path", "lifecycle"), (life, "checkpoint_public_key_path", "lifecycle-checkpoint"),
            (life, "activation_public_key_path", "activation"), (rev, "revocation_public_key_path", "revocation"),
            (rev, "checkpoint_public_key_path", "revocation-checkpoint"), (rev, "owner_public_key_path", "owner-approval"),
            (rev, "activation_public_key_path", "activation"), (rev, "lifecycle_public_key_path", "lifecycle")]
        for obj, name, role in bindings:
            expected = self.key_path(role, ROLE_PURPOSES[role][0], current_time=now)
            require(os.path.normcase(os.path.abspath(getattr(obj, name))) == expected, "FIXED_AUTHORITY_OVERRIDE")
        for obj, role in ((strict.time_verifier, "trusted-time"), (life, "lifecycle"), (rev, "revocation")):
            require(obj.authority_identity == self._record_from(self._records, role)["authority_identity"], "FIXED_AUTHORITY_IDENTITY_MISMATCH")
        expected = tuple(self.key_path(role, ROLE_PURPOSES[role][0], current_time=now)
                         for role in ("publisher", "selector", "control-checkpoint"))
        require(tuple(os.path.normcase(os.path.abspath(p)) for p in control._paths) == expected, "FIXED_AUTHORITY_OVERRIDE")
        fixed = self.bind_read_context(read_context, current_time=now)
        self.verify_artifact("observation", observation_attestation, current_time=now,
            max_age_seconds=fixed.observation_max_age_seconds)
        result = reader.read(observation=observation, snapshot=snapshot, read_context=fixed,
            observation_attestation=observation_attestation)
        self.bootstrap.require_current()
        return result
