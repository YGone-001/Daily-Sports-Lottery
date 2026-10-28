"""独立撤销权威的隔离只读组件；不提供生产签名、服务或持久存储。

事件是不可变历史；当前性只来自新鲜的完整状态与独立检查点。会话 challenge
不能证明生产全局当前性，检查点签发者必须独立维护真实高水位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import math
from threading import RLock
from typing import Any
from uuid import uuid4

from utils.mapping_governance import load_trusted_public_key
from utils.runtime_activation_authorization import (
    ActivationVerificationContext, StrictActivationVerifier, verify_runtime_activation_authorization,
)
from utils.source_identity import fingerprint
from utils.trusted_artifacts import SignedTrustedArtifact, TrustedArtifactError, verify_trusted_artifact
from utils.trusted_time_authority import parse_utc

SCHEMA_VERSION = "1.0.0"
FRESHNESS_POLICY = "isolated-revocation-v1"
MAX_FRESHNESS_SECONDS = 300
MAX_INVENTORY = 1024
REVOCATION_PURPOSE = "RUNTIME_ACTIVATION_REVOCATION"
STATE_PURPOSE = "RUNTIME_ACTIVATION_REVOCATION_STATE"
CHECKPOINT_PURPOSE = "RUNTIME_ACTIVATION_REVOCATION_CHECKPOINT"
AUTHORITY_SCOPE = "SCOPED_SHADOW_ACTIVATION_AUTHORIZATIONS"


class RevocationAuthorityError(TrustedArtifactError):
    """任何缺失、不完整、冲突或过旧证据均 fail closed。"""


def reject(code):
    raise RevocationAuthorityError(code, rejection_codes=(code,))


def revocation_event_payload(authorization, *, authority_identity, sequence, effective_at, issued_at, reason):
    body = dict(revocation_schema_version=SCHEMA_VERSION, purpose=REVOCATION_PURPOSE,
        signing_role="revocation-authority", dataset_identity=authorization.dataset_identity,
        activation_id=authorization.activation_id, authorization_fingerprint=authorization.fingerprint(),
        revocation_reference=authorization.revocation_reference,
        source_identity_key=authorization.scoped_source_identity_key, canonical_id=authorization.candidate_canonical_id,
        activation_authorizer=authorization.authorizer, authority_identity=authority_identity,
        effective_at=effective_at, issued_at=issued_at, sequence=sequence, reason=reason)
    return {**body, "revocation_event_id": fingerprint(body)}


def _inventory(authorizations):
    return sorted({(e.authorization.activation_id, e.authorization.fingerprint()) for e in authorizations})


def _lifecycle_scope(lifecycle_evidence):
    if lifecycle_evidence is None:
        return "", 0
    return lifecycle_evidence.state.fingerprint(), lifecycle_evidence.state.payload()["sequence"]


def revocation_state_payload(events, *, authorizations, dataset_identity, baseline_fingerprint,
                             authority_identity, state_sequence, issued_at, lifecycle_evidence=None):
    life_fp, life_sequence = _lifecycle_scope(lifecycle_evidence)
    revoked = sorted({(e.payload()["activation_id"], e.payload()["authorization_fingerprint"]) for e in events})
    return dict(revocation_schema_version=SCHEMA_VERSION, purpose=STATE_PURPOSE,
        signing_role="revocation-authority", freshness_policy=FRESHNESS_POLICY,
        dataset_identity=dataset_identity, baseline_fingerprint=baseline_fingerprint,
        authority_identity=authority_identity, authority_scope=AUTHORITY_SCOPE,
        authorization_inventory=_inventory(authorizations), state_sequence=state_sequence,
        revocation_event_fingerprints=sorted({e.fingerprint() for e in events}),
        currently_revoked_authorizations=revoked, issued_at=issued_at,
        lifecycle_state_fingerprint=life_fp, lifecycle_sequence=life_sequence)


def revocation_checkpoint_payload(state, *, session_challenge, issued_at, lifetime_seconds=60):
    p = state.payload()
    events = p["revocation_event_fingerprints"]
    return dict(revocation_schema_version=SCHEMA_VERSION, purpose=CHECKPOINT_PURPOSE,
        signing_role="revocation-checkpoint", freshness_policy=FRESHNESS_POLICY,
        dataset_identity=p["dataset_identity"], authority_identity=p["authority_identity"],
        state_sequence=p["state_sequence"], state_fingerprint=state.fingerprint(), event_count=len(events),
        event_inventory_fingerprint=fingerprint(events), issued_at=issued_at,
        expires_at=(parse_utc(issued_at) + timedelta(seconds=lifetime_seconds)).isoformat(),
        session_challenge=session_challenge)


@dataclass(frozen=True)
class RevocationEvidence:
    events: tuple[SignedTrustedArtifact, ...]
    state: SignedTrustedArtifact
    checkpoint: SignedTrustedArtifact


@dataclass(frozen=True)
class VerifiedRevocationState:
    state_sequence: int
    state_fingerprint: str
    event_identities: tuple[tuple[str, str], ...]
    revoked_authorizations: tuple[tuple[str, str], ...]
    effective_revocations: tuple[tuple[str, str], ...]
    authorization_inventory: tuple[tuple[str, str], ...]
    continuity_generation: int

    def is_revoked(self, authorization):
        return (authorization.activation_id, authorization.fingerprint()) in self.revoked_authorizations


@dataclass(frozen=True)
class _Continuity:
    sequence: int
    fingerprint: str
    events: tuple[tuple[str, str], ...]
    authorizations: tuple[tuple[str, str], ...]
    issued_at: str
    governance_revocations: tuple[tuple[str, str], ...]
    revoked_authorizations: tuple[tuple[str, str], ...]
    generation: int


@dataclass(frozen=True)
class IndependentRevocationVerifier:
    """固定七个角色的信任根；只保存有界、串行化的进程内高水位。"""
    dataset_identity: str
    authority_identity: str
    revocation_public_key_path: str
    checkpoint_public_key_path: str
    activation_public_key_path: str
    lifecycle_public_key_path: str
    owner_public_key_path: str
    strict_activation_verifier: StrictActivationVerifier
    max_age_seconds: float = 60.0
    _challenge: str = field(default_factory=lambda: uuid4().hex, init=False)
    _keys: tuple[str, ...] = field(default=(), init=False, repr=False)
    _state: _Continuity | None = field(default=None, init=False, repr=False)
    _accepted: bool = field(default=False, init=False, repr=False)
    _governance_seen: tuple = field(default=(), init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        strict = self.strict_activation_verifier
        if (not self.dataset_identity or not self.authority_identity
            or not isinstance(strict, StrictActivationVerifier) or not strict.require_authenticated_approval
            or not callable(strict.time_verifier.freshness_reference)
            or strict.time_verifier.dataset_identity != self.dataset_identity
            or not math.isfinite(self.max_age_seconds) or not 0 < self.max_age_seconds <= MAX_FRESHNESS_SECONDS):
            raise ValueError("independent revocation requires approval, trusted time and bounded isolated freshness")
        paths = (self.revocation_public_key_path, self.checkpoint_public_key_path, self.activation_public_key_path,
            self.lifecycle_public_key_path, self.owner_public_key_path,
            strict.governance_view_trusted_public_key_path, strict.time_verifier.trusted_public_key_path)
        keys = tuple(load_trusted_public_key(p)["public_key_id"] for p in paths)
        if len(set(keys)) != len(keys):
            raise ValueError("revocation, checkpoint, activation, lifecycle, owner, governance and time keys must differ")
        object.__setattr__(self, "_keys", keys)

    @property
    def session_challenge(self):
        return self._challenge

    @property
    def generation(self):
        with self._lock:
            return self._generation

    @property
    def continuity_healthy(self):
        with self._lock:
            return not self._accepted or (
                isinstance(self._state, _Continuity) and type(self._generation) is int
                and self._generation >= self._state.generation)

    def _artifact(self, artifact, purpose, key_index, now, *, historical=False):
        if not isinstance(artifact, SignedTrustedArtifact):
            reject("REVOCATION_ARTIFACT_MALFORMED")
        p = artifact.payload()
        issued = parse_utc(p.get("issued_at"))
        # Historical events never expire or get re-signed. Only complete state and
        # checkpoint assert currentness; their freshness is strictly bounded.
        age = max(0, (parse_utc(now) - issued).total_seconds()) if historical else self.max_age_seconds
        path = self.revocation_public_key_path if key_index == 0 else self.checkpoint_public_key_path
        p = verify_trusted_artifact(artifact, expected_artifact_type=purpose, trusted_public_key_path=path,
            current_time=now, max_age_seconds=age)
        if artifact.signer_public_key_id != self._keys[key_index]:
            reject("REVOCATION_SIGNER_CHANGED")
        if (p.get("purpose") != purpose or p.get("revocation_schema_version") != SCHEMA_VERSION
            or p.get("signing_role") != ("revocation-authority" if key_index == 0 else "revocation-checkpoint")):
            reject("REVOCATION_CONTRACT_INVALID")
        if p.get("dataset_identity") != self.dataset_identity:
            reject("REVOCATION_DATASET_MISMATCH")
        if p.get("authority_identity") != self.authority_identity:
            reject("REVOCATION_AUTHORITY_MISMATCH")
        return p

    def verify(self, evidence, *, read_context):
        with self._lock:
            if not self.continuity_healthy:
                reject("REVOCATION_CONTINUITY_LOST")
            if not isinstance(evidence, RevocationEvidence):
                reject("REVOCATION_EVIDENCE_MISSING")
            try:
                return self._verify(evidence, read_context)
            except TrustedArtifactError:
                raise
            except (AttributeError, TypeError, ValueError, KeyError, IndexError) as exc:
                raise RevocationAuthorityError("malformed revocation evidence",
                    rejection_codes=("REVOCATION_EVIDENCE_MALFORMED",)) from exc

    def _verify(self, evidence, ctx):
        strict = self.strict_activation_verifier
        now = strict.time_verifier.verify(ctx.trusted_time_evidence, dataset_identity=ctx.dataset_identity).effective_time
        vctx = ActivationVerificationContext(ctx.dataset_identity, ctx.current_baseline_fingerprint, now,
            self.activation_public_key_path, require_verified_governance=True)
        governance = strict._authenticated_approval_view(ctx.authoritative_view, effective_time=now, context=vctx)
        previous = self._state
        seen = set(self._governance_seen) | (set(previous.governance_revocations) if previous else set())
        if not seen.issubset(set(governance.revocations)):
            reject("REVOCATION_GOVERNANCE_REMOVED")
        if set(governance.revocations) != seen:
            object.__setattr__(self, "_generation", self._generation + 1)
        object.__setattr__(self, "_governance_seen", tuple(sorted(governance.revocations)))
        if any(record.owner_public_key_id != self._keys[4] for record in governance.approved_records):
            reject("REVOCATION_OWNER_ROLE_MISMATCH")
        state = self._artifact(evidence.state, STATE_PURPOSE, 0, now)
        cp = self._artifact(evidence.checkpoint, CHECKPOINT_PURPOSE, 1, now)
        if state.get("freshness_policy") != FRESHNESS_POLICY or cp.get("freshness_policy") != FRESHNESS_POLICY:
            reject("REVOCATION_FRESHNESS_POLICY_MISMATCH")
        sequence = state["state_sequence"]
        if type(sequence) is not int or not 0 <= sequence < 2**63:
            reject("REVOCATION_SEQUENCE_INVALID")
        if state["baseline_fingerprint"] != ctx.current_baseline_fingerprint or state["authority_scope"] != AUTHORITY_SCOPE:
            reject("REVOCATION_SCOPE_MISMATCH")
        life_fp, life_seq = _lifecycle_scope(ctx.lifecycle_evidence)
        if (state["lifecycle_state_fingerprint"] != life_fp
            or type(state["lifecycle_sequence"]) is not int or state["lifecycle_sequence"] != life_seq):
            reject("REVOCATION_LIFECYCLE_MISMATCH")
        issued, cp_issued, expires = parse_utc(state["issued_at"]), parse_utc(cp["issued_at"]), parse_utc(cp["expires_at"])
        if (cp_issued < issued or not 0 < (expires - cp_issued).total_seconds() <= self.max_age_seconds
            or parse_utc(now) >= expires):
            reject("REVOCATION_CHECKPOINT_EXPIRED")
        event_fps = state["revocation_event_fingerprints"]
        if any(not isinstance(state.get(name), list) or len(state[name]) > MAX_INVENTORY
               for name in ("revocation_event_fingerprints", "authorization_inventory", "currently_revoked_authorizations")):
            reject("REVOCATION_INVENTORY_INVALID")
        if (cp.get("session_challenge") != self._challenge or cp.get("state_fingerprint") != evidence.state.fingerprint()
            or type(cp.get("state_sequence")) is not int or cp["state_sequence"] != sequence
            or type(cp.get("event_count")) is not int or cp["event_count"] != len(event_fps)
            or cp.get("event_inventory_fingerprint") != fingerprint(event_fps)):
            reject("REVOCATION_CHECKPOINT_MISMATCH")
        raw_authorizations = ctx.lifecycle_evidence.authorizations if ctx.lifecycle_evidence is not None else ctx.evidence
        if (not isinstance(raw_authorizations, tuple) or not isinstance(evidence.events, tuple)
            or len(raw_authorizations) > MAX_INVENTORY or len(evidence.events) > MAX_INVENTORY):
            reject("REVOCATION_INVENTORY_INVALID")
        inventory = {}
        for item in sorted(raw_authorizations, key=lambda e: (e.authorization.activation_id, e.authorization.fingerprint())):
            auth = item.authorization
            if auth.signer_public_key_id != self._keys[2]:
                reject("REVOCATION_ACTIVATION_SIGNER_MISMATCH")
            if auth.activation_id in inventory and inventory[auth.activation_id].authorization.fingerprint() != auth.fingerprint():
                reject("REVOCATION_ACTIVATION_ID_CONFLICT")
            verified = verify_runtime_activation_authorization(auth, ActivationVerificationContext(
                ctx.dataset_identity, ctx.current_baseline_fingerprint, now, self.activation_public_key_path,
                item.decision, item.proposal, require_verified_governance=True))
            codes = set(verified.rejection_codes) - {"ACTIVATION_AUTHORIZATION_EXPIRED"}
            if codes:
                raise RevocationAuthorityError("authorization identity invalid", rejection_codes=tuple(sorted(codes)))
            inventory[auth.activation_id] = item
        actual_inventory = _inventory(raw_authorizations)
        if fingerprint(state["authorization_inventory"]) != fingerprint(actual_inventory):
            reject("REVOCATION_AUTHORIZATION_INVENTORY_MISMATCH")
        for item in ctx.evidence:
            auth = item.authorization
            if auth.activation_id not in inventory or inventory[auth.activation_id].authorization.fingerprint() != auth.fingerprint():
                reject("REVOCATION_ACTIVATION_ID_CONFLICT")

        # Stable conflicting-ID diagnostics independent of event input ordering.
        ids = {}
        for event in evidence.events:
            identity, digest = event.payload()["revocation_event_id"], event.fingerprint()
            if identity in ids and ids[identity] != digest:
                reject("REVOCATION_EVENT_ID_CONFLICT")
            ids[identity] = digest
        events, revoked, event_sequences = {}, {}, {}
        for event in sorted(evidence.events, key=lambda e: e.fingerprint()):
            p = self._artifact(event, REVOCATION_PURPOSE, 0, now, historical=True)
            identity, digest = p["revocation_event_id"], event.fingerprint()
            if identity in events:
                continue  # Exact signed event replay is idempotent.
            seq = p["sequence"]
            if type(seq) is not int or not 0 < seq <= sequence or seq in event_sequences:
                reject("REVOCATION_EVENT_SEQUENCE_INVALID")
            if p["activation_id"] not in inventory:
                reject("REVOCATION_UNKNOWN_ACTIVATION")
            auth = inventory[p["activation_id"]].authorization
            expected = revocation_event_payload(auth, authority_identity=self.authority_identity,
                sequence=seq, effective_at=p["effective_at"], issued_at=p["issued_at"], reason=p["reason"])
            if not isinstance(p["reason"], str) or not p["reason"].strip() or p != expected:
                reject("REVOCATION_EVENT_BINDING_MISMATCH")
            if not parse_utc(auth.authorized_at) <= parse_utc(p["effective_at"]) <= parse_utc(p["issued_at"]) <= issued:
                reject("REVOCATION_EVENT_TIME_INVALID")
            if auth.activation_id in revoked:
                reject("REVOCATION_AUTHORIZATION_CONFLICT")
            events[identity], event_sequences[seq] = digest, identity
            revoked[auth.activation_id] = auth.fingerprint()
        if event_fps != sorted(events.values()) or fingerprint(state["currently_revoked_authorizations"]) != fingerprint(sorted(revoked.items())):
            reject("REVOCATION_STATE_INCOMPLETE")
        if previous:
            if sequence < previous.sequence or (sequence == previous.sequence and evidence.state.fingerprint() != previous.fingerprint):
                reject("REVOCATION_STATE_ROLLBACK")
            if (not set(previous.events).issubset(set(events.items()))
                or not set(previous.authorizations).issubset(set(actual_inventory))):
                reject("REVOCATION_HISTORY_REMOVED")
            if issued < parse_utc(previous.issued_at):
                reject("REVOCATION_STATE_ROLLBACK")
            if any(seq <= previous.sequence and (identity, events[identity]) not in previous.events
                   for seq, identity in event_sequences.items()):
                reject("REVOCATION_EVENT_SEQUENCE_INVALID")
        effective = {aid: inventory[aid].authorization.revocation_reference for aid in revoked}
        for aid, reference in governance.revocations:
            if aid not in inventory:
                reject("REVOCATION_GOVERNANCE_UNKNOWN_ACTIVATION")
            auth = inventory[aid].authorization
            if reference != auth.revocation_reference:
                reject("REVOCATION_GOVERNANCE_REFERENCE_MISMATCH")
            effective[aid] = reference
            revoked[aid] = auth.fingerprint()
        if previous and not set(previous.revoked_authorizations).issubset(set(revoked.items())):
            reject("REVOCATION_HISTORY_REMOVED")
        if previous is None or previous.fingerprint != evidence.state.fingerprint():
            object.__setattr__(self, "_generation", self._generation + 1)
        object.__setattr__(self, "_state", _Continuity(sequence, evidence.state.fingerprint(),
            tuple(sorted(events.items())), tuple(actual_inventory), state["issued_at"], self._governance_seen,
            tuple(sorted(revoked.items())), self._generation))
        object.__setattr__(self, "_accepted", True)
        return VerifiedRevocationState(sequence, evidence.state.fingerprint(), tuple(sorted(events.items())),
            tuple(sorted(revoked.items())), tuple(sorted(effective.items())), tuple(actual_inventory), self._generation)
