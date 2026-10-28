"""显式激活替代与只读生命周期验证；仅供隔离 shadow 原型。

签名的完整 inventory 与 CURRENT 声明由生命周期角色提供；独立检查点角色
绑定状态指纹、单调序号及 verifier 会话 challenge。所有授权仍复用既有验证器。
历史集合永不直接进入可执行快照；不实现生产注册表或自动签名。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from uuid import uuid4

from utils.mapping_governance import load_trusted_public_key
from utils.runtime_activation_authorization import (
    ActivationVerificationContext, StrictActivationVerifier,
    verify_runtime_activation_authorization,
)
from utils.source_identity import fingerprint
from utils.trusted_artifacts import (
    SignedTrustedArtifact, TrustedArtifactError, verify_trusted_artifact,
)
from utils.trusted_time_authority import parse_utc

SCHEMA_VERSION = "1.0.0"
SUPERSESSION_PURPOSE = "RUNTIME_ACTIVATION_SUPERSESSION"
STATE_PURPOSE = "RUNTIME_ACTIVATION_LIFECYCLE_STATE"
CHECKPOINT_PURPOSE = "RUNTIME_ACTIVATION_LIFECYCLE_CHECKPOINT"


class SupersessionError(TrustedArtifactError):
    """稳定拒绝码；任何失败均不返回可消费状态。"""


def _reject(code):
    raise SupersessionError(code, rejection_codes=(code,))


def transition_payload(old, new, *, transition_id: str, sequence: int,
                       effective_at: str, authority_identity: str, reason: str) -> dict:
    """构造待签载荷；调用方必须显式使用独立角色签名。"""
    return dict(lifecycle_schema_version=SCHEMA_VERSION, purpose=SUPERSESSION_PURPOSE,
        dataset_identity=new.dataset_identity, source_identity_key=new.scoped_source_identity_key,
        canonical_id=new.candidate_canonical_id, baseline_fingerprint=new.expected_baseline_fingerprint,
        authorized_scope=new.authorized_scope, old_activation_id=old.activation_id,
        old_authorization_fingerprint=old.fingerprint(), new_activation_id=new.activation_id,
        new_authorization_fingerprint=new.fingerprint(), old_proposal_id=old.proposal_id,
        new_proposal_id=new.proposal_id, approval_fingerprint=new.approval_fingerprint,
        eligibility_decision_fingerprint=new.eligibility_decision_fingerprint,
        transition_id=transition_id, sequence=sequence, effective_at=effective_at,
        issued_at=effective_at, authority_identity=authority_identity, reason=reason)


def state_payload(evidence, transitions, *, current_activation_ids, dataset_identity,
                  baseline_fingerprint, governance_fingerprint, sequence,
                  issued_at, authority_identity) -> dict:
    """完整、顺序无关的签名 inventory；遗漏或额外事件都不能静默通过。"""
    return dict(lifecycle_schema_version=SCHEMA_VERSION, purpose=STATE_PURPOSE,
        dataset_identity=dataset_identity, baseline_fingerprint=baseline_fingerprint,
        governance_fingerprint=governance_fingerprint, sequence=sequence,
        issued_at=issued_at, authority_identity=authority_identity,
        authorizations=sorted({(e.authorization.activation_id, e.authorization.fingerprint())
                               for e in evidence}),
        transitions=sorted({event.fingerprint() for event in transitions}),
        current_activation_ids=sorted(current_activation_ids))


def checkpoint_payload(state, *, session_challenge, issued_at) -> dict:
    payload = state.payload()
    return dict(lifecycle_schema_version=SCHEMA_VERSION, purpose=CHECKPOINT_PURPOSE,
        dataset_identity=payload["dataset_identity"], baseline_fingerprint=payload["baseline_fingerprint"],
        authority_identity=payload["authority_identity"], sequence=payload["sequence"],
        state_fingerprint=state.fingerprint(), session_challenge=session_challenge, issued_at=issued_at)


@dataclass(frozen=True)
class AuthorizationLifecycleEvidence:
    """历史签名工件与有效快照分离；这里没有可执行绑定。"""
    authorizations: tuple[Any, ...]
    transitions: tuple[SignedTrustedArtifact, ...]
    state: SignedTrustedArtifact
    checkpoint: SignedTrustedArtifact


@dataclass(frozen=True)
class AuthorizationLifecycleEntry:
    activation_id: str
    authorization_fingerprint: str
    source_identity_key: str
    canonical_id: str
    selection: str
    expired: bool
    revoked: bool
    cryptographically_valid: bool = True


@dataclass(frozen=True)
class VerifiedAuthorizationState:
    """只读派生结果不是信任凭证；Reader 总是重新验证原始证据。"""
    sequence: int
    state_fingerprint: str
    entries: tuple[AuthorizationLifecycleEntry, ...]
    transition_fingerprints: tuple[str, ...]

    def current_for(self, source_key):
        matches = tuple(e for e in self.entries if e.source_identity_key == source_key and e.selection == "CURRENT")
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ActivationSupersessionVerifier:
    """构造时固定信任根；单进程高水位 + 新会话独立检查点。

    同一签名事件和状态在新鲜度内幂等；同 ID 异内容拒绝。新状态必须保留所有
    已认证历史。重启没有旧高水位，不接受旧 challenge；必须由独立检查点角色
    为新 challenge 签发当前状态，不能声称这是持久化生产回滚保护。
    """
    dataset_identity: str
    authority_identity: str
    lifecycle_public_key_path: str
    checkpoint_public_key_path: str
    activation_public_key_path: str
    strict_activation_verifier: StrictActivationVerifier
    max_age_seconds: float = 300.0
    _challenge: str = field(default_factory=lambda: uuid4().hex, init=False)
    _keys: tuple[str, ...] = field(default=(), init=False, repr=False)
    _accepted: bool = field(default=False, init=False, repr=False)
    _previous: Any = field(default=None, init=False, repr=False)
    _observed_revocations: tuple = field(default=(), init=False, repr=False)
    _lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        strict = self.strict_activation_verifier
        if (not self.dataset_identity or not self.authority_identity
            or not isinstance(strict, StrictActivationVerifier) or not strict.require_authenticated_approval
            or not math.isfinite(self.max_age_seconds) or not 0 < self.max_age_seconds <= 3600):
            raise ValueError("supersession requires authenticated approval/time and bounded freshness")
        keys = tuple(load_trusted_public_key(path)["public_key_id"] for path in (
            self.lifecycle_public_key_path, self.checkpoint_public_key_path, self.activation_public_key_path,
            strict.governance_view_trusted_public_key_path, strict.time_verifier.trusted_public_key_path))
        if len(set(keys)) != len(keys):
            raise ValueError("lifecycle, checkpoint, activation, governance and time roles must use distinct keys")
        object.__setattr__(self, "_keys", keys)

    @property
    def session_challenge(self):
        return self._challenge

    def _artifact(self, artifact, purpose, path, key_id, now):
        payload = verify_trusted_artifact(artifact, expected_artifact_type=purpose,
            trusted_public_key_path=path, current_time=now, max_age_seconds=self.max_age_seconds)
        if artifact.signer_public_key_id != key_id:
            _reject("LIFECYCLE_SIGNER_CHANGED")
        if (payload.get("lifecycle_schema_version") != SCHEMA_VERSION
            or payload.get("purpose") != purpose):
            _reject("LIFECYCLE_CONTRACT_INVALID")
        if payload.get("dataset_identity") != self.dataset_identity:
            _reject("LIFECYCLE_DATASET_MISMATCH")
        if payload.get("authority_identity") != self.authority_identity:
            _reject("LIFECYCLE_AUTHORITY_MISMATCH")
        return payload

    def verify(self, bundle, *, read_context) -> VerifiedAuthorizationState:
        with self._lock:
            if self._accepted and self._previous is None:
                _reject("LIFECYCLE_CONTINUITY_LOST")
            if not isinstance(bundle, AuthorizationLifecycleEvidence):
                _reject("LIFECYCLE_EVIDENCE_MISSING")
            if not isinstance(bundle.authorizations, tuple) or not isinstance(bundle.transitions, tuple):
                _reject("LIFECYCLE_MALFORMED")
            try:
                return self._verify(bundle, read_context)
            except TrustedArtifactError:
                raise
            except (TypeError, ValueError, KeyError, AttributeError, IndexError) as exc:
                raise SupersessionError("malformed lifecycle evidence",
                    rejection_codes=("LIFECYCLE_MALFORMED",)) from exc

    def _verify(self, bundle, context):
        strict = self.strict_activation_verifier
        now = strict.time_verifier.verify(context.trusted_time_evidence,
            dataset_identity=context.dataset_identity).effective_time
        verification_context = ActivationVerificationContext(context.dataset_identity,
            context.current_baseline_fingerprint, now, self.activation_public_key_path,
            require_verified_governance=True)
        # Uses exactly the existing authenticated governance/approval continuity engine.
        view = strict._authenticated_approval_view(context.authoritative_view,
            effective_time=now, context=verification_context)
        if not set(self._observed_revocations).issubset(set(view.revocations)):
            _reject("LIFECYCLE_REVOCATION_REMOVED")
        object.__setattr__(self, "_observed_revocations", tuple(sorted(view.revocations)))
        if self._keys[0] in {record.owner_public_key_id for record in view.approved_records}:
            _reject("LIFECYCLE_ROLE_SUBSTITUTION")
        state = self._artifact(bundle.state, STATE_PURPOSE, self.lifecycle_public_key_path, self._keys[0], now)
        checkpoint = self._artifact(bundle.checkpoint, CHECKPOINT_PURPOSE,
            self.checkpoint_public_key_path, self._keys[1], now)
        parse_utc(state["issued_at"])
        parse_utc(checkpoint["issued_at"])
        sequence = state["sequence"]
        if type(sequence) is not int or sequence < 1:
            _reject("LIFECYCLE_SEQUENCE_INVALID")
        if state["baseline_fingerprint"] != context.current_baseline_fingerprint:
            _reject("LIFECYCLE_BASELINE_MISMATCH")
        if state["governance_fingerprint"] != view.fingerprint():
            _reject("LIFECYCLE_GOVERNANCE_MISMATCH")
        if (checkpoint.get("state_fingerprint") != bundle.state.fingerprint()
            or checkpoint.get("session_challenge") != self._challenge
            or type(checkpoint.get("sequence")) is not int or checkpoint.get("sequence") != sequence
            or checkpoint.get("baseline_fingerprint") != state["baseline_fingerprint"]):
            _reject("LIFECYCLE_CHECKPOINT_MISMATCH")

        inventory = {}
        for evidence in sorted(bundle.authorizations, key=lambda e: (e.authorization.activation_id, e.authorization.fingerprint())):
            auth = evidence.authorization
            if auth.signer_public_key_id != self._keys[2]:
                _reject("LIFECYCLE_ACTIVATION_SIGNER_CHANGED")
            previous = inventory.get(auth.activation_id)
            if previous and previous.authorization.fingerprint() != auth.fingerprint():
                _reject("LIFECYCLE_ACTIVATION_ID_CONFLICT")
            inventory[auth.activation_id] = evidence
        actual_inventory = sorted((aid, e.authorization.fingerprint()) for aid, e in inventory.items())
        if fingerprint(state["authorizations"]) != fingerprint(actual_inventory):
            _reject("LIFECYCLE_INVENTORY_MISMATCH")
        # Caller evidence cannot substitute a different signature for an inventory ID.
        for evidence in context.evidence:
            aid = evidence.authorization.activation_id
            if aid not in inventory or evidence.authorization.fingerprint() != inventory[aid].authorization.fingerprint():
                _reject("LIFECYCLE_ACTIVATION_ID_CONFLICT")

        events, outgoing, incoming, event_sequences = {}, {}, {}, {}
        for artifact in sorted(bundle.transitions, key=lambda a: a.fingerprint()):
            p = self._artifact(artifact, SUPERSESSION_PURPOSE, self.lifecycle_public_key_path, self._keys[0], now)
            identity = p["transition_id"]
            if not isinstance(identity, str) or not identity.strip() or not p["reason"].strip():
                _reject("LIFECYCLE_TRANSITION_INVALID")
            digest = artifact.fingerprint()
            if identity in events:
                if events[identity] != digest:
                    _reject("LIFECYCLE_TRANSITION_ID_CONFLICT")
                continue  # Byte-identical signed events are explicitly idempotent.
            events[identity] = digest
            seq = p["sequence"]
            if type(seq) is not int or not 0 < seq <= sequence or seq in event_sequences:
                _reject("LIFECYCLE_SEQUENCE_INVALID")
            event_sequences[seq] = identity
            old_id, new_id = p["old_activation_id"], p["new_activation_id"]
            if old_id not in inventory or new_id not in inventory:
                _reject("LIFECYCLE_UNKNOWN_AUTHORIZATION")
            old, new = inventory[old_id].authorization, inventory[new_id].authorization
            expected = transition_payload(old, new, transition_id=identity, sequence=seq,
                effective_at=p["effective_at"], authority_identity=self.authority_identity, reason=p["reason"])
            if p != expected:
                _reject("LIFECYCLE_TRANSITION_BINDING_MISMATCH")
            if any(getattr(old, attr) != getattr(new, attr) for attr in (
                "dataset_identity", "expected_baseline_fingerprint", "scoped_source_identity_key",
                "candidate_canonical_id", "authorized_scope")):
                _reject("LIFECYCLE_SCOPE_MISMATCH")
            effective = parse_utc(p["effective_at"])
            if (effective > parse_utc(now) or effective > parse_utc(state["issued_at"])
                or effective < parse_utc(new.authorized_at)):
                _reject("LIFECYCLE_TRANSITION_TIME_INVALID")
            if old_id in outgoing or new_id in incoming:
                _reject("LIFECYCLE_SUCCESSOR_CONFLICT")
            outgoing[old_id] = (new_id, seq, effective)
            incoming[new_id] = old_id
        if sorted(state["transitions"]) != sorted(events.values()):
            _reject("LIFECYCLE_EVENTS_MISMATCH")

        # Detect cycles before checking sequence, so a cycle has one stable code
        # regardless of the point where traversal starts.
        for start in sorted(inventory):
            seen, node = set(), start
            while node in outgoing:
                if node in seen:
                    _reject("LIFECYCLE_CYCLE")
                seen.add(node)
                node = outgoing[node][0]
        for start in sorted(inventory):
            seen, node, last_sequence, last_time = set(), start, 0, None
            while node in outgoing:
                if node in seen:
                    _reject("LIFECYCLE_CYCLE")
                seen.add(node)
                node, seq, at = outgoing[node]
                if seq <= last_sequence or (last_time is not None and at < last_time):
                    _reject("LIFECYCLE_SEQUENCE_INVALID")
                last_sequence, last_time = seq, at
        current = sorted(set(inventory) - set(outgoing))
        if sorted(state["current_activation_ids"]) != current:
            _reject("LIFECYCLE_CURRENT_MISMATCH")
        by_source = {}
        for aid in current:
            source = inventory[aid].authorization.scoped_source_identity_key
            if source in by_source:
                _reject("LIFECYCLE_AMBIGUOUS_CURRENT")
            by_source[source] = aid

        previous = self._previous
        digest = bundle.state.fingerprint()
        if previous:
            if sequence < previous[0] or (sequence == previous[0] and digest != previous[1]):
                _reject("LIFECYCLE_STATE_ROLLBACK")
            if (not set(previous[2]).issubset(set(actual_inventory))
                or not set(previous[3]).issubset(set(events.values()))):
                _reject("LIFECYCLE_HISTORY_REMOVED")
            if parse_utc(state["issued_at"]) < previous[4]:
                _reject("LIFECYCLE_STATE_ROLLBACK")
            if not set(previous[5]).issubset(set(view.revocations)):
                _reject("LIFECYCLE_REVOCATION_REMOVED")
            if any(seq <= previous[0] and events[identity] not in previous[3]
                   for seq, identity in event_sequences.items()):
                _reject("LIFECYCLE_SEQUENCE_INVALID")

        # Authentication of the control state is independent of availability.
        # A signed A->B must retire A even when B's current consumption fails
        # (expiry, revocation, approval withdrawal, etc.). Never roll back this
        # watermark merely because the successor cannot currently be consumed.
        object.__setattr__(self, "_previous", (sequence, digest, tuple(actual_inventory), tuple(events.values()),
            parse_utc(state["issued_at"]), tuple(view.revocations)))
        object.__setattr__(self, "_accepted", True)
        entries = []
        for aid, evidence in sorted(inventory.items()):
            auth = evidence.authorization
            ctx = ActivationVerificationContext(context.dataset_identity,
                context.current_baseline_fingerprint, now, self.activation_public_key_path,
                evidence.decision, evidence.proposal, revocation_registry=view.revocations,
                require_verified_governance=True)
            if aid in outgoing:
                # Independently authenticate historical bytes and original bindings.
                # Only expiry/revocation affect availability, not signature integrity.
                verified = verify_runtime_activation_authorization(auth, ctx)
                allowed = {"ACTIVATION_AUTHORIZATION_EXPIRED", "ACTIVATION_AUTHORIZATION_REVOKED"}
                codes = set(verified.rejection_codes) - allowed
                if auth.approval_fingerprint != evidence.decision.approval_fingerprint or not auth.approval_fingerprint:
                    codes.add("ACTIVATION_APPROVAL_FINGERPRINT_MISMATCH")
                if (evidence.decision.decision_id != fingerprint(evidence.decision.identity_payload())
                    or evidence.decision.rejection_codes
                    or evidence.decision.evidence_fingerprint not in evidence.proposal.observation_fingerprints
                    or evidence.decision.source_identity_key != auth.scoped_source_identity_key
                    or evidence.decision.canonical_id != auth.candidate_canonical_id
                    or evidence.decision.baseline_fingerprint != auth.expected_baseline_fingerprint):
                    codes.add("LIFECYCLE_HISTORICAL_ELIGIBILITY_INVALID")
                lifetime = (parse_utc(auth.expires_at) - parse_utc(auth.authorized_at)).total_seconds()
                if not 0 < lifetime <= strict.max_lifetime_seconds:
                    codes.add("ACTIVATION_LIFETIME_EXCEEDED")
                if codes:
                    raise SupersessionError("historical authorization invalid", rejection_codes=tuple(sorted(codes)))
            else:
                verified = strict.verify(auth, ctx, time_evidence=context.trusted_time_evidence, governance_view=view)
                if not verified.is_consumable:
                    raise SupersessionError("current authorization invalid", rejection_codes=verified.rejection_codes)
                if (view.active_binding_owners_for(auth.scoped_source_identity_key) != (auth.candidate_canonical_id,)
                    or auth.candidate_canonical_id not in view.canonical_id_set()):
                    _reject("LIFECYCLE_OWNERSHIP_MISMATCH")
            entries.append(AuthorizationLifecycleEntry(aid, auth.fingerprint(), auth.scoped_source_identity_key,
                auth.candidate_canonical_id, "SUPERSEDED" if aid in outgoing else "CURRENT",
                parse_utc(now) >= parse_utc(auth.expires_at), aid in view.revocation_ids()))
        return VerifiedAuthorizationState(sequence, digest, tuple(entries), tuple(sorted(events.values())))

    def verify_snapshot(self, bundle, *, read_context, snapshot):
        """完整快照必须与显式 CURRENT 集合一一对应；旧快照永不重新启用。"""
        view = self.verify(bundle, read_context=read_context)
        expected = {entry.activation_id for entry in view.entries if entry.selection == "CURRENT"}
        actual = [binding.activation_id for binding in snapshot.bindings]
        if (set(actual) != expected or len(actual) != len(expected)
            or set(snapshot.activation_ids) != expected or len(snapshot.activation_ids) != len(expected)):
            _reject("LIFECYCLE_SNAPSHOT_NOT_CURRENT")
        by_id = {e.authorization.activation_id: e.authorization for e in bundle.authorizations}
        for binding in snapshot.bindings:
            auth = by_id[binding.activation_id]
            if (binding.source_identity_key != auth.scoped_source_identity_key
                or binding.candidate_canonical_id != auth.candidate_canonical_id
                or binding.authorized_scope != auth.authorized_scope
                or binding.eligibility_decision_fingerprint != auth.eligibility_decision_fingerprint):
                _reject("LIFECYCLE_SNAPSHOT_BINDING_MISMATCH")
        return view
