"""Isolated signed time, with explicitly trusted independent freshness reference.

No system-clock default, network, production keys or persistence. A new verifier
requires a fresh authority response bound to its unpredictable session challenge.
The reference provider is a deployment trust boundary, not a timestamp supplied
by the read caller. Tests install a separately controlled synthetic clock.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable, Mapping, Any
from uuid import uuid4

from utils.mapping_governance import load_trusted_public_key
from utils.trusted_artifacts import (
    SignedTrustedArtifact, TrustedArtifactError, sign_trusted_artifact,
    verify_trusted_artifact,
)

TIME_ATTESTATION_PURPOSE = "RUNTIME_ACTIVATION_TIME_ATTESTATION"
TIME_SCHEMA_VERSION = "1.0.0"
TRUSTED_TIME_POLICY_VERSION = "synthetic-time-v1"
MAX_SESSION_EVIDENCE = 1024


class TrustedTimeError(TrustedArtifactError):
    """Fail-closed time verification error with stable rejection codes."""


def _reject(code: str) -> None:
    raise TrustedTimeError(code, rejection_codes=(code,))


def parse_utc(value: str) -> datetime:
    """Strict UTC RFC3339 profile; never guess a missing timezone."""
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value
    ):
        _reject("TRUSTED_TIME_TIMESTAMP_INVALID")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _reject("TRUSTED_TIME_TIMESTAMP_INVALID")


def sign_time_attestation(private_key_path: str, *, dataset_identity: str,
                          authority_identity: str, attested_at: str,
                          expires_at: str, generation: int, evidence_id: str,
                          session_challenge: str) -> SignedTrustedArtifact:
    """Explicit synthetic issuance only; never called automatically by a verifier."""
    return sign_trusted_artifact(private_key_path, artifact_type=TIME_ATTESTATION_PURPOSE,
        payload={"time_schema_version": TIME_SCHEMA_VERSION,
                 "purpose": TIME_ATTESTATION_PURPOSE,
                 "dataset_identity": dataset_identity,
                 "authority_identity": authority_identity,
                 "attested_at": attested_at, "issued_at": attested_at,
                 "expires_at": expires_at, "generation": generation,
                 "evidence_id": evidence_id, "session_challenge": session_challenge})


@dataclass(frozen=True)
class TrustedTimeObservation:
    effective_time: str
    generation: int
    evidence_fingerprint: str
    session_challenge: str


@dataclass(frozen=True)
class _Watermark:
    attested: datetime
    reference: datetime
    effective: datetime
    generation: int
    evidence_id: str
    fingerprint: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TrustedTimeVerifier:
    """One bounded, serialized in-process session; configuration cannot be replaced.

    The callback must itself be trusted independently of the evidence and caller.
    No restart high-watermark is claimed. New sessions use a new nonce and require
    newly issued evidence; losing active state permanently closes that session.
    Reuse of identical evidence is allowed only inside its freshness interval.
    """
    dataset_identity: str
    authority_identity: str
    trusted_public_key_path: str | None
    freshness_reference: Callable[[], datetime] | None
    max_age_seconds: float = 60.0
    rollback_tolerance_seconds: float = 0.0
    _challenge: str = field(default_factory=lambda: uuid4().hex, init=False)
    _state: _Watermark | None = field(default=None, init=False, repr=False)
    _accepted: bool = field(default=False, init=False, repr=False)
    _broken: bool = field(default=False, init=False, repr=False)
    _lock: Any = field(default_factory=RLock, init=False, repr=False, compare=False)
    _key_id: str = field(default="", init=False, repr=False)
    _started_at: datetime | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        if (not math.isfinite(self.max_age_seconds) or not 0 < self.max_age_seconds <= 300
            or not math.isfinite(self.rollback_tolerance_seconds)
            or not 0 <= self.rollback_tolerance_seconds <= 5
            or not self.dataset_identity or not self.authority_identity):
            raise ValueError("bounded explicit time policy required")
        if self.trusted_public_key_path:
            object.__setattr__(self, "_key_id",
                load_trusted_public_key(self.trusted_public_key_path)["public_key_id"])
        if self.freshness_reference is not None:
            object.__setattr__(self, "_started_at", self._reference())

    @property
    def session_challenge(self) -> str:
        return self._challenge

    def _reference(self) -> datetime:
        if not callable(self.freshness_reference):
            _reject("TRUSTED_TIME_REFERENCE_MISSING")
        try:
            value = self.freshness_reference()
        except Exception as exc:
            raise TrustedTimeError("independent reference unavailable",
                rejection_codes=("TRUSTED_TIME_REFERENCE_UNAVAILABLE",)) from exc
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
            _reject("TRUSTED_TIME_REFERENCE_INVALID")
        return value.astimezone(timezone.utc)

    def verify(self, evidence: SignedTrustedArtifact | Mapping[str, Any] | None,
               *, dataset_identity: str) -> TrustedTimeObservation:
        with self._lock:
            if self._broken or (self._accepted and self._state is None):
                _reject("TRUSTED_TIME_CONTINUITY_LOST")
            if evidence is None:
                _reject("TRUSTED_TIME_EVIDENCE_MISSING")
            now = self._reference()
            previous = self._state
            if ((previous and now < previous.reference)
                or (self._started_at and now < self._started_at)):
                object.__setattr__(self, "_broken", True)
                _reject("TRUSTED_TIME_REFERENCE_ROLLBACK")
            if isinstance(evidence, Mapping):
                evidence = SignedTrustedArtifact.from_dict(evidence)
            if not isinstance(evidence, SignedTrustedArtifact):
                _reject("TRUSTED_TIME_EVIDENCE_INVALID")
            # Validate shape before delegating to the shared crypto primitive,
            # whose older timestamp parser intentionally accepts legacy naive UTC.
            try:
                payload = evidence.payload()
            except (TrustedArtifactError, TypeError, ValueError):
                _reject("TRUSTED_TIME_EVIDENCE_INVALID")
            if not isinstance(payload, dict):
                _reject("TRUSTED_TIME_EVIDENCE_INVALID")
            issued = parse_utc(payload.get("issued_at"))
            attested = parse_utc(payload.get("attested_at"))
            expires = parse_utc(payload.get("expires_at"))
            try:
                payload = verify_trusted_artifact(evidence,
                    expected_artifact_type=TIME_ATTESTATION_PURPOSE,
                    trusted_public_key_path=self.trusted_public_key_path,
                    current_time=now.isoformat(), max_age_seconds=self.max_age_seconds)
            except (TypeError, ValueError, AttributeError) as exc:
                raise TrustedTimeError("malformed time envelope",
                    rejection_codes=("TRUSTED_TIME_EVIDENCE_INVALID",)) from exc
            if evidence.signer_public_key_id != self._key_id:
                _reject("TRUSTED_TIME_SIGNER_CHANGED")
            if payload.get("purpose") != TIME_ATTESTATION_PURPOSE:
                _reject("TRUSTED_TIME_PURPOSE_MISMATCH")
            if payload.get("time_schema_version") != TIME_SCHEMA_VERSION:
                _reject("TRUSTED_TIME_SCHEMA_UNSUPPORTED")
            if dataset_identity != self.dataset_identity or payload.get("dataset_identity") != self.dataset_identity:
                _reject("TRUSTED_TIME_DATASET_MISMATCH")
            if payload.get("authority_identity") != self.authority_identity:
                _reject("TRUSTED_TIME_AUTHORITY_MISMATCH")
            if payload.get("session_challenge") != self._challenge:
                _reject("TRUSTED_TIME_SESSION_MISMATCH")
            if self._started_at is None or issued < self._started_at:
                _reject("TRUSTED_TIME_RESTART_EVIDENCE_REQUIRED")
            if attested != issued or expires <= issued or (expires-issued).total_seconds() > self.max_age_seconds:
                _reject("TRUSTED_TIME_FRESHNESS_INTERVAL_INVALID")
            if now >= expires:
                _reject("TRUSTED_TIME_EXPIRED")
            generation = payload.get("generation")
            evidence_id = payload.get("evidence_id")
            if (type(generation) is not int or not 0 <= generation < 2**63
                or not isinstance(evidence_id, str) or not evidence_id.strip() or len(evidence_id) > 128):
                _reject("TRUSTED_TIME_EVIDENCE_INVALID")
            digest = evidence.fingerprint()
            if previous:
                if generation < previous.generation:
                    _reject("TRUSTED_TIME_GENERATION_ROLLBACK")
                if generation == previous.generation and digest != previous.fingerprint:
                    _reject("TRUSTED_TIME_SEQUENCE_CONFLICT")
                if generation > previous.generation and evidence_id in previous.evidence_ids:
                    _reject("TRUSTED_TIME_REPLAY")
                if (previous.attested - attested).total_seconds() > self.rollback_tolerance_seconds:
                    _reject("TRUSTED_TIME_CLOCK_ROLLBACK")
            # Advance an attestation with the independently trusted elapsed time.
            # Replaying the same still-fresh token can never freeze authorization time.
            effective = attested + (now - issued)
            if previous:
                effective = max(effective, previous.effective)
            evidence_ids = previous.evidence_ids if previous else ()
            if not previous or generation > previous.generation:
                if len(evidence_ids) >= MAX_SESSION_EVIDENCE:
                    _reject("TRUSTED_TIME_SESSION_EXHAUSTED")
                evidence_ids += (evidence_id,)
            object.__setattr__(self, "_state", _Watermark(
                attested, now, effective, generation, evidence_id, digest, evidence_ids))
            object.__setattr__(self, "_accepted", True)
            return TrustedTimeObservation(effective.isoformat(), generation, digest, self._challenge)


def verify_trusted_time_evidence(evidence, *, verifier: TrustedTimeVerifier,
                                 dataset_identity: str) -> TrustedTimeObservation:
    return verifier.verify(evidence, dataset_identity=dataset_identity)
