"""
tools/real_sports_identity_audit.py
===================================
READ-ONLY real sports identity and Canonical mapping evidence audit.

Purpose
-------
Measure the *observed* effectiveness of the existing identity / Canonical
mapping implementation against the real local dataset.  This tool produces
**evidence**, not decisions, and it never enables runtime activation.

Hard constraints (enforced, not documented promises)
----------------------------------------------------
1. Read-only: the seven protected JSON stores are opened only for reading and
   their SHA-256 is captured before and after the audit.  Any change aborts
   the run with a non-zero exit code.
2. No network access: only ``json``/``hashlib`` semantics on local files.
3. No production writes: an optional report path is validated through
   ``tools.staging_canonical_repair.assert_safe_path``, which rejects the
   production checkout, the evidence epochs and this source checkout.
4. Deterministic: every collection is ordered by canonical ID (or by the
   identity key string), so two runs on identical bytes produce identical
   reports, except for the explicitly-labelled ``latency`` block.
5. Fail-closed: malformed or structurally impossible input raises
   ``AuditInputError``; nothing is repaired, normalised away or guessed.

Ground-truth posture
--------------------
There are **no independently human-reviewed equivalence labels** in this
repository.  Pair comparisons therefore report a *field-evidence proxy* label
computed by ``classify_pair_evidence`` — a rule set deliberately different from
``utils.match_identity.same_event`` (it never uses competition-number equality
as the equivalence rule, and it downgrades name abbreviations to AMBIGUOUS).
The proxy is used **only** to describe disagreement between the existing
candidate generator and independent field evidence.  It is *not* precision or
recall, and the corresponding metrics are reported as ``N/A``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from tools.staging_canonical_repair import (
    ProtectedPathViolationError,
    assert_safe_path,
    sha256_file,
)
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    plan_ingestion_identity,
)
from utils.match_identity import (
    competition_number_for_match,
    is_kickoff_time_known,
    same_event,
)
from utils.settlements import outcome_from_score, result_fingerprint
from utils.source_id_migration import (
    STORE_ORDER,
    classify_market_source_id,
    enumerate_store_references,
)
from utils.source_identity import (
    ScopedSourceIdentityKey,
    detect_all_conflicts,
    observe_source_row,
)

# =============================================================================
# Contract constants
# =============================================================================

AUDIT_SCHEMA_VERSION = "1.0.0"
AUDIT_KIND = "READ_ONLY_REAL_SPORTS_IDENTITY_AUDIT"
REPORT_KIND = "REAL_SPORTS_IDENTITY_MAPPING_EVIDENCE"

#: The seven protected stores, in the contract order of the migration module.
PROTECTED_STORES: tuple[str, ...] = tuple(STORE_ORDER)
CANONICAL_STORE = "daily_matches.json"
OPTIONAL_STORES: tuple[str, ...] = tuple(
    name for name in PROTECTED_STORES if name != CANONICAL_STORE
)

#: This audit never authorises activation; the flags are part of the artifact.
PRODUCTION_ACTIVATION_READY = False
DECISION = "NO-GO"

#: Provenance of every equivalence label produced by this tool.
LABEL_PROVENANCE = "FIELD_EVIDENCE_PROXY_NOT_HUMAN_REVIEWED"
HUMAN_LABEL_AVAILABILITY = "NO_INDEPENDENT_HUMAN_REVIEWED_LABELS_AVAILABLE"

SAME_EVENT = "SAME_EVENT"
DIFFERENT_EVENT = "DIFFERENT_EVENT"
AMBIGUOUS = "AMBIGUOUS"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

#: Evidence classes used to qualify every finding in the report.
OBSERVED = "OBSERVED"
DERIVED = "DERIVED"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"

#: Namespace -> adapter mapping, mirroring ``utils.runtime_mapping_policy``.
#: The namespace is read from the persisted ID literal, therefore the adapter
#: itself is INFERRED (the original parser is no longer attached to the row).
NAMESPACE_ADAPTERS: dict[str, str] = {
    "500l": "live_football",
    "500lq": "live_basketball",
    "500j": "market_xml",
    "500w": "finished_feed",
}
MISSING = "<MISSING>"

#: Exit codes.
EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_PROTECTED_CHANGE = 3


class AuditInputError(Exception):
    """Raised when the supplied dataset cannot be audited safely."""


class AuditProtectedMutationError(Exception):
    """Raised when a protected store changed during the read-only audit."""


# =============================================================================
# Loading (read-only, strict)
# =============================================================================

@dataclass(frozen=True)
class StoreFile:
    """A read-only view of one protected store."""

    name: str
    path: str
    sha256: str
    payload: Any


@dataclass(frozen=True)
class Dataset:
    """The loaded real dataset plus explicit evidence gaps."""

    stores: Mapping[str, StoreFile]
    matches: tuple[dict, ...]
    prediction_snapshots: Mapping[str, dict]
    odds_snapshots: Mapping[str, dict]
    settlements: Mapping[str, dict]
    evaluation_rows: Mapping[str, dict]
    gaps: tuple[str, ...]
    schema_errors: tuple[str, ...]

    def hashes(self) -> dict[str, str]:
        return {name: self.stores[name].sha256 for name in PROTECTED_STORES if name in self.stores}


def default_data_dir() -> str:
    """The repository ``data/`` directory hosting this tool."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
    )


def _read_json_strict(path: str, name: str) -> Any:
    """Strict, side-effect free JSON reader (no ``.corrupt`` files are written)."""
    if not os.path.isfile(path):
        raise AuditInputError(f"{name}: file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise AuditInputError(f"{name}: malformed JSON ({exc})") from exc
    except OSError as exc:
        raise AuditInputError(f"{name}: unreadable file ({exc})") from exc


def _as_record_map(payload: Any, name: str, collection: str) -> dict[str, dict]:
    """
    Validate ``{"<collection>": {id: {...}}}`` and return a deterministically
    ordered copy.  Structurally impossible payloads fail closed.
    """
    if not isinstance(payload, Mapping):
        raise AuditInputError(f"{name}: top-level payload is not an object")
    block = payload.get(collection)
    if not isinstance(block, Mapping):
        raise AuditInputError(f"{name}: '{collection}' is not an ID-keyed object")
    out: dict[str, dict] = {}
    for key in sorted(block, key=str):
        record = block[key]
        if not isinstance(record, Mapping):
            raise AuditInputError(f"{name}: record {key!r} is not an object")
        out[str(key)] = dict(record)
    return out


def _as_id_list(payload: Any, name: str) -> list[str]:
    if not isinstance(payload, Mapping):
        raise AuditInputError(f"{name}: top-level payload is not an object")
    values = payload.get("ids")
    if not isinstance(values, list):
        raise AuditInputError(f"{name}: 'ids' is not a list")
    return [str(value) for value in values if str(value or "")]


def load_dataset(
    data_dir: str | None = None,
    overrides: Mapping[str, str] | None = None,
) -> Dataset:
    """
    Load the seven protected stores without mutating them.

    ``overrides`` maps a store file name to an explicit path, which is how a
    caller pins an isolated copy for a repeatable audit.
    """
    root = os.path.abspath(data_dir or default_data_dir())
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(PROTECTED_STORES))
    if unknown:
        raise AuditInputError(f"unknown store overrides: {unknown}")

    stores: dict[str, StoreFile] = {}
    gaps: list[str] = []
    schema_errors: list[str] = []

    for name in PROTECTED_STORES:
        path = overrides.get(name, os.path.join(root, name))
        if not os.path.isfile(path):
            gaps.append(f"{name}: not present at {path}")
            continue
        payload = _read_json_strict(path, name)
        stores[name] = StoreFile(
            name=name, path=path, sha256=sha256_file(path), payload=payload
        )

    if CANONICAL_STORE not in stores:
        raise AuditInputError(
            f"the Canonical store {CANONICAL_STORE} is required; audit cannot proceed"
        )

    canonical_payload = stores[CANONICAL_STORE].payload
    if not isinstance(canonical_payload, Mapping):
        raise AuditInputError(f"{CANONICAL_STORE}: top-level payload is not an object")
    raw_matches = canonical_payload.get("matches")
    if not isinstance(raw_matches, list):
        raise AuditInputError(f"{CANONICAL_STORE}: 'matches' is not a list")

    seen: set[str] = set()
    matches: list[dict] = []
    for index, record in enumerate(raw_matches):
        if not isinstance(record, Mapping):
            raise AuditInputError(
                f"{CANONICAL_STORE}: matches[{index}] is not an object"
            )
        match_id = record.get("id")
        if not isinstance(match_id, str) or not match_id.strip():
            raise AuditInputError(
                f"{CANONICAL_STORE}: matches[{index}] has no usable string id"
            )
        if match_id in seen:
            raise AuditInputError(
                f"{CANONICAL_STORE}: duplicate Canonical id {match_id!r} "
                "(the fail-closed uniqueness guard is violated)"
            )
        seen.add(match_id)
        matches.append(dict(record))
    matches.sort(key=lambda item: str(item["id"]))

    prediction_snapshots: dict[str, dict] = {}
    odds_snapshots: dict[str, dict] = {}
    settlements: dict[str, dict] = {}
    evaluation_rows: dict[str, dict] = {}

    if "prediction_snapshots.json" in stores:
        prediction_snapshots = _as_record_map(
            stores["prediction_snapshots.json"].payload,
            "prediction_snapshots.json",
            "snapshots",
        )
    if "odds_snapshots.json" in stores:
        odds_snapshots = _as_record_map(
            stores["odds_snapshots.json"].payload, "odds_snapshots.json", "snapshots"
        )
    if "settlements.json" in stores:
        settlements = _as_record_map(
            stores["settlements.json"].payload, "settlements.json", "settlements"
        )
    if "evaluation_rows.json" in stores:
        evaluation_rows = _as_record_map(
            stores["evaluation_rows.json"].payload, "evaluation_rows.json", "rows"
        )
    if "calibrated.json" in stores:
        calibrated_ids = _as_id_list(stores["calibrated.json"].payload, "calibrated.json")
        orphan_calibrated = sorted({mid for mid in calibrated_ids if mid not in seen})
        if orphan_calibrated:
            schema_errors.append(
                "calibrated.json: %d id(s) are not Canonical: %s"
                % (len(orphan_calibrated), orphan_calibrated[:5])
            )
    if "team_strength.json" in stores:
        payload = stores["team_strength.json"].payload
        if not isinstance(payload, Mapping) or not isinstance(payload.get("teams"), Mapping):
            schema_errors.append("team_strength.json: 'teams' is not an ID-keyed object")

    for name, collection in (
        ("prediction_snapshots.json", prediction_snapshots),
        ("odds_snapshots.json", odds_snapshots),
        ("settlements.json", settlements),
        ("evaluation_rows.json", evaluation_rows),
    ):
        for key in collection:
            record = collection[key]
            if not record.get("match_id"):
                schema_errors.append(f"{name}: record {key!r} has no match_id")

    return Dataset(
        stores=stores,
        matches=tuple(matches),
        prediction_snapshots=prediction_snapshots,
        odds_snapshots=odds_snapshots,
        settlements=settlements,
        evaluation_rows=evaluation_rows,
        gaps=tuple(gaps),
        schema_errors=tuple(schema_errors),
    )


# =============================================================================
# Field helpers
# =============================================================================

def _namespace(match_id: Any) -> str:
    if not isinstance(match_id, str) or not match_id:
        return MISSING
    head = match_id.split("-", 1)[0].strip()
    return head or MISSING


def adapter_for_match_id(match_id: Any) -> tuple[str, bool]:
    """
    Derive the parser adapter from the persisted namespace prefix.

    Returns ``(adapter, known)``.  ``known=False`` means the prefix is not in the
    declared table, so the adapter is an inference and must be reported as such.
    """
    namespace = _namespace(match_id)
    adapter = NAMESPACE_ADAPTERS.get(namespace)
    if adapter is None:
        return "unknown", False
    return adapter, True


def _final_score(match: Mapping[str, Any]) -> dict | None:
    score = match.get("score")
    if not isinstance(score, Mapping):
        return None
    full_time = score.get("ft")
    if not isinstance(full_time, (list, tuple)) or len(full_time) != 2:
        return None
    home, away = full_time
    if not isinstance(home, int) or not isinstance(away, int):
        return None
    if home < 0 or away < 0:
        return None
    return {"home": home, "away": away}


def _kickoff_time(match: Mapping[str, Any]) -> str | None:
    value = match.get("time")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value != "00:00" else None


def _norm_team(value: Any) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if not ch.isspace()).lower()


def team_relation(left: Any, right: Any) -> str:
    """
    Compare two team names without asserting equivalence prematurely.

    ``ABBREVIATION`` deliberately does **not** count as equivalence: the real
    dataset contains truncated names (``斯洛文尼`` vs ``斯洛文尼亚``) that cannot be
    resolved from field evidence alone.
    """
    if not left or not right:
        return "MISSING"
    raw_left, raw_right = str(left), str(right)
    norm_left, norm_right = _norm_team(left), _norm_team(right)
    if not norm_left or not norm_right:
        return "MISSING"
    if norm_left == norm_right:
        return "EXACT" if raw_left == raw_right else "NORMALIZED_EQUAL"
    if len(norm_left) >= 2 and len(norm_right) >= 2 and (
        norm_left.startswith(norm_right) or norm_right.startswith(norm_left)
    ):
        return "ABBREVIATION"
    return "DIFFERENT"


def classify_pair_evidence(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """
    An **independent field-evidence proxy** for "same real event".

    This is intentionally *not* ``same_event``:

    - it never treats ``sport|date|competition_number`` equality as sufficient
      when the team names differ;
    - it requires exact or punctuation-only-equivalent team names;
    - it downgrades truncated names, kickoff disagreement, league disagreement,
      competition-number disagreement and result disagreement to ``AMBIGUOUS``;
    - it returns ``DIFFERENT_EVENT`` only on decisive contradiction.

    The proxy is a diagnostic, never human-reviewed ground truth.
    """
    evidence: dict[str, Any] = {
        "left_id": str(left.get("id") or ""),
        "right_id": str(right.get("id") or ""),
        "left_sport": left.get("sport"),
        "right_sport": right.get("sport"),
        "left_date": left.get("date"),
        "right_date": right.get("date"),
    }
    if not left.get("sport") or not right.get("sport"):
        evidence["reason"] = "missing sport"
        return INSUFFICIENT_EVIDENCE, evidence
    if not left.get("date") or not right.get("date"):
        evidence["reason"] = "missing event date"
        return INSUFFICIENT_EVIDENCE, evidence
    if not left.get("home") or not left.get("away") or not right.get("home") or not right.get("away"):
        evidence["reason"] = "missing team names"
        return INSUFFICIENT_EVIDENCE, evidence

    if str(left.get("sport")) != str(right.get("sport")):
        evidence["reason"] = "sport mismatch"
        return DIFFERENT_EVENT, evidence
    if str(left.get("date")) != str(right.get("date")):
        evidence["reason"] = "event date mismatch"
        return DIFFERENT_EVENT, evidence

    home_relation = team_relation(left.get("home"), right.get("home"))
    away_relation = team_relation(left.get("away"), right.get("away"))
    evidence["home_relation"] = home_relation
    evidence["away_relation"] = away_relation

    left_kickoff = _kickoff_time(left) if is_kickoff_time_known(dict(left)) else None
    right_kickoff = _kickoff_time(right) if is_kickoff_time_known(dict(right)) else None
    evidence["left_kickoff"] = left_kickoff
    evidence["right_kickoff"] = right_kickoff
    left_league, right_league = left.get("league"), right.get("league")
    evidence["left_league"], evidence["right_league"] = left_league, right_league
    left_number = competition_number_for_match(dict(left))
    right_number = competition_number_for_match(dict(right))
    evidence["left_number"] = left_number
    evidence["right_number"] = right_number

    if "DIFFERENT" in (home_relation, away_relation):
        evidence["reason"] = "team names contradict"
        return DIFFERENT_EVENT, evidence
    if "MISSING" in (home_relation, away_relation):
        evidence["reason"] = "team name missing"
        return INSUFFICIENT_EVIDENCE, evidence
    if "ABBREVIATION" in (home_relation, away_relation):
        evidence["reason"] = "team name truncated or abbreviated"
        return AMBIGUOUS, evidence

    if left_kickoff and right_kickoff and left_kickoff != right_kickoff:
        evidence["reason"] = "kickoff time disagreement"
        return AMBIGUOUS, evidence

    if left_league and right_league and _norm_team(left_league) != _norm_team(right_league):
        evidence["reason"] = "league disagreement"
        return AMBIGUOUS, evidence

    if left_number is not None and right_number is not None and left_number != right_number:
        evidence["reason"] = "competition number disagreement"
        return AMBIGUOUS, evidence

    left_score, right_score = _final_score(left), _final_score(right)
    if left_score is not None and right_score is not None and left_score != right_score:
        evidence["reason"] = "final score disagreement"
        return AMBIGUOUS, evidence

    evidence["reason"] = "teams, date, kickoff, league, number and result agree"
    return SAME_EVENT, evidence


def observe_canonical(
    matches: Sequence[Mapping[str, Any]]
) -> tuple[tuple[Mapping[str, Any], Any, str, bool], ...]:
    """
    Build one :class:`SourceObservation` per Canonical record (pure, read-only).

    Raw provider identifiers are **not** synthesised: the normalised rows no
    longer carry them, so ``raw_provider_id`` stays ``None`` (an explicit gap).
    """
    out: list[tuple[Mapping[str, Any], Any, str, bool]] = []
    for match in matches:
        adapter, known = adapter_for_match_id(match.get("id"))
        observation = observe_source_row(
            match, adapter=adapter, canonical_match_id=str(match.get("id"))
        )
        out.append((match, observation, adapter, known))
    return tuple(out)


# =============================================================================
# Inventory
# =============================================================================

def build_inventory(dataset: Dataset, observed) -> dict:
    """OBSERVED dataset inventory, ordered deterministically."""
    matches = dataset.matches
    sports = Counter(str(match.get("sport") or MISSING) for match in matches)
    leagues = Counter(str(match.get("league") or MISSING) for match in matches)
    namespaces = Counter(_namespace(match.get("id")) for match in matches)
    dates = sorted({str(match.get("date")) for match in matches if match.get("date")})
    stability = Counter(observation.identifier_stability for _m, observation, _a, _k in observed)
    raw_types = Counter(observation.raw_provider_id_type for _m, observation, _a, _k in observed)
    identity_kinds = Counter(
        ScopedSourceIdentityKey.for_observation(observation).identity_kind
        for _m, observation, _a, _k in observed
    )
    adapters = Counter(adapter for _m, _o, adapter, _k in observed)
    unknown_adapters = sorted(
        {_namespace(match.get("id")) for match, _o, _a, known in observed if not known}
    )
    market_classes = Counter(
        classify_market_source_id(match.get("id"))
        for match in matches
        if _namespace(match.get("id")) == "500j"
    )
    missing_fields = Counter()
    for match in matches:
        for field in ("id", "sport", "date", "home", "away", "league", "status", "time"):
            if not match.get(field):
                missing_fields[field] += 1
        if match.get("jczq_no") in (None, ""):
            missing_fields["jczq_no"] += 1
        if not isinstance(match.get("odds"), Mapping):
            missing_fields["odds"] += 1
        if _final_score(match) is None:
            missing_fields["final_score"] += 1

    kickoff_known_by_guard = sum(1 for match in matches if is_kickoff_time_known(dict(match)))
    kickoff_flag_in_observation = sum(
        1 for _m, observation, _a, _k in observed if observation.kickoff_time_known
    )

    return {
        "events": len(matches),
        "sport_distribution": dict(sorted(sports.items())),
        "competition_distribution": dict(sorted(leagues.items())),
        "competition_count": len(leagues),
        "date_range": {"min": dates[0], "max": dates[-1], "distinct_dates": len(dates)}
        if dates
        else {"min": None, "max": None, "distinct_dates": 0},
        "namespace_distribution": dict(sorted(namespaces.items())),
        "adapter_distribution": dict(sorted(adapters.items())),
        "adapter_inferred_for_namespaces": unknown_adapters,
        "market_source_id_class_distribution": dict(sorted(market_classes.items())),
        "identifier_stability_distribution": dict(sorted(stability.items())),
        "raw_identifier_type_distribution": dict(sorted(raw_types.items())),
        "identity_key_kind_distribution": dict(sorted(identity_kinds.items())),
        "raw_provider_ids_preserved": sum(
            1 for _m, observation, _a, _k in observed if observation.raw_provider_id is not None
        ),
        "kickoff_time_known_by_guard": kickoff_known_by_guard,
        "kickoff_time_known_flag_in_observations": kickoff_flag_in_observation,
        "kickoff_time_known_field_present": sum(
            1 for match in matches if "kickoff_time_known" in match
        ),
        "missing_field_counts": dict(sorted(missing_fields.items())),
        "store_presence": {name: name in dataset.stores for name in PROTECTED_STORES},
        "prediction_snapshots": len(dataset.prediction_snapshots),
        "odds_snapshots": len(dataset.odds_snapshots),
        "settlements": len(dataset.settlements),
        "evaluation_rows": len(dataset.evaluation_rows),
        "evidence_gaps": list(dataset.gaps),
        "schema_errors": list(dataset.schema_errors),
    }


# =============================================================================
# Identity analysis
# =============================================================================

def analyze_identity(observed) -> dict:
    """OBSERVED + DERIVED identity key analysis over the Canonical set."""
    keys: list[tuple[str, Any]] = []
    for match, observation, _adapter, _known in observed:
        key = ScopedSourceIdentityKey.for_observation(observation)
        keys.append((str(match.get("id")), key))

    sufficient = [(mid, key) for mid, key in keys if key.is_sufficient]
    insufficient = [(mid, key) for mid, key in keys if not key.is_sufficient]

    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for mid, key in keys:
        owners[(key.identity_kind, key.identity_value)].add(mid)
    duplicate_keys = sorted(
        (
            {"identity_kind": kind, "identity_value": value, "canonical_ids": sorted(ids)}
            for (kind, value), ids in owners.items()
            if len(ids) > 1
        ),
        key=lambda item: (item["identity_kind"], item["identity_value"]),
    )

    conflicts = detect_all_conflicts([observation for _m, observation, _a, _k in observed])
    by_category = Counter(conflict.category for conflict in conflicts)

    unstable = [
        mid
        for mid, observation, _adapter, _known in observed
        if observation.identifier_stability
        in ("ROW_POSITION_UNSTABLE", "DERIVED_FROM_PROVIDER_FIELDS", "UNKNOWN")
    ]

    insufficient_ids = sorted(mid for mid, key in keys if not key.is_sufficient)

    return {
        "observations": len(keys),
        "sufficient_identity_keys": len(sufficient),
        "insufficient_identity_keys": len(insufficient),
        "insufficient_identity_canonical_ids": insufficient_ids,
        "identity_kind_distribution": dict(
            sorted(Counter(key.identity_kind for _mid, key in keys).items())
        ),
        "duplicate_identity_keys": duplicate_keys,
        "duplicate_identity_key_count": len(duplicate_keys),
        "conflict_count": len(conflicts),
        "conflict_category_distribution": dict(sorted(by_category.items())),
        "conflicts": [conflict.as_dict() for conflict in conflicts],
        "unstable_identifier_canonical_ids": unstable,
        "unstable_identifier_count": len(unstable),
        "provider_id_uniqueness_scopes": dict(
            sorted(
                Counter(
                    observation.provider_id_uniqueness_scope
                    for _m, observation, _a, _k in observed
                ).items()
            )
        ),
        "insufficient_examples": [
            {
                "canonical_id": mid,
                "derived_source_id": key.identity_value,
                "identifier_stability": observed[index][1].identifier_stability,
                "missing_evidence": list(observed[index][1].missing_evidence()),
            }
            for index, (mid, key) in enumerate(keys)
            if not key.is_sufficient
        ][:25],
    }


# =============================================================================
# Pairwise candidate analysis (same_event vs independent field evidence)
# =============================================================================

def analyze_pairs(matches: Sequence[Mapping[str, Any]], max_listed: int = 25) -> dict:
    """
    Enumerate candidate pairs inside each ``(sport, date)`` bucket.

    ``same_event`` is the implementation under audit; ``classify_pair_evidence``
    is the proxy reference.  Disagreements are reported as **proxy**
    false-positive / false-negative candidates — never as precision/recall.
    """
    buckets: dict[tuple[Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for match in matches:
        buckets[(match.get("sport"), match.get("date"))].append(match)

    label_counts = Counter()
    agreements = Counter()
    false_positive: list[dict] = []
    false_negative: list[dict] = []
    ambiguous_pairs: list[dict] = []
    total_pairs = 0
    algorithm_positive = 0
    evidence_positive = 0

    for bucket_key in sorted(buckets, key=lambda item: (str(item[0]), str(item[1]))):
        rows = sorted(buckets[bucket_key], key=lambda row: str(row.get("id")))
        for left_index in range(len(rows)):
            for right_index in range(left_index + 1, len(rows)):
                left, right = rows[left_index], rows[right_index]
                total_pairs += 1
                label, evidence = classify_pair_evidence(left, right)
                label_counts[label] += 1
                algorithm_says_same = same_event(dict(left), dict(right))
                algorithm_positive += int(algorithm_says_same)
                evidence_positive += int(label == SAME_EVENT)
                agreements[(algorithm_says_same, label)] += 1
                record = {
                    "left_id": evidence["left_id"],
                    "right_id": evidence["right_id"],
                    "sport": left.get("sport"),
                    "date": left.get("date"),
                    "competition": left.get("league"),
                    "left_number": evidence.get("left_number"),
                    "right_number": evidence.get("right_number"),
                    "left_kickoff": evidence.get("left_kickoff"),
                    "right_kickoff": evidence.get("right_kickoff"),
                    "home_relation": evidence.get("home_relation"),
                    "away_relation": evidence.get("away_relation"),
                    "reason": evidence.get("reason"),
                    "label": label,
                }
                if algorithm_says_same and label in (DIFFERENT_EVENT, AMBIGUOUS):
                    false_positive.append(record)
                if label == SAME_EVENT and not algorithm_says_same:
                    false_negative.append(record)
                if label == AMBIGUOUS:
                    ambiguous_pairs.append(record)

    profile = Counter()
    for item in ambiguous_pairs:
        profile["pairs"] += 1
        if item.get("left_kickoff") and item.get("left_kickoff") == item.get("right_kickoff"):
            profile["same_kickoff"] += 1
        if (item.get("left_number") is None) != (item.get("right_number") is None):
            profile["exactly_one_competition_number"] += 1
        if item.get("home_relation") == "ABBREVIATION" or item.get("away_relation") == "ABBREVIATION":
            profile["team_name_abbreviated"] += 1

    return {
        "scoped_pairs_compared": total_pairs,
        "label_distribution": dict(sorted(label_counts.items())),
        "ambiguous_pair_profile": dict(sorted(profile.items())),
        "agreement_matrix": {
            f"same_event={algo},evidence={label}": count
            for (algo, label), count in sorted(
                agreements.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        },
        "algorithm_positive_pairs": algorithm_positive,
        "evidence_positive_pairs": evidence_positive,
        "proxy_false_positive_count": len(false_positive),
        "proxy_false_negative_count": len(false_negative),
        "proxy_false_positive_examples": false_positive[:max_listed],
        "proxy_false_negative_examples": false_negative[:max_listed],
        "ambiguous_pair_count": len(ambiguous_pairs),
        "ambiguous_pair_examples": ambiguous_pairs[:max_listed],
        "label_provenance": LABEL_PROVENANCE,
    }


def _clusters(edges: Sequence[tuple[str, str]]) -> list[list[str]]:
    """Deterministic connected components from undirected edges."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            if left_root < right_root:
                parent[right_root] = left_root
            else:
                parent[left_root] = right_root

    for left, right in edges:
        union(left, right)
    groups: dict[str, list[str]] = defaultdict(list)
    for node in sorted(parent):
        groups[find(node)].append(node)
    return [sorted(members) for _root, members in sorted(groups.items())]


def analyze_canonical_ownership(
    matches: Sequence[Mapping[str, Any]], max_listed: int = 25
) -> dict:
    """
    Detect Canonical IDs that represent the same real event (DERIVED).

    The clustering uses the field-evidence proxy, so it is a *derived*
    association: it identifies candidates for human adjudication, it does not
    assert that one of the two records is wrong.
    """
    by_id = {str(match.get("id")): match for match in matches}
    buckets: dict[tuple[Any, Any], list[str]] = defaultdict(list)
    for match in matches:
        buckets[(match.get("sport"), match.get("date"))].append(str(match.get("id")))

    same_edges: list[tuple[str, str]] = []
    ambiguous_edges: list[tuple[str, str]] = []
    for bucket_key in sorted(buckets, key=lambda item: (str(item[0]), str(item[1]))):
        ids = sorted(buckets[bucket_key])
        for left_index in range(len(ids)):
            for right_index in range(left_index + 1, len(ids)):
                label, _evidence = classify_pair_evidence(
                    by_id[ids[left_index]], by_id[ids[right_index]]
                )
                if label == SAME_EVENT:
                    same_edges.append((ids[left_index], ids[right_index]))
                elif label == AMBIGUOUS:
                    ambiguous_edges.append((ids[left_index], ids[right_index]))

    duplicate_clusters = [group for group in _clusters(same_edges) if len(group) > 1]
    ambiguous_clusters = [
        group for group in _clusters(ambiguous_edges) if len(group) > 1
    ]

    involved = {mid for group in duplicate_clusters for mid in group}
    details: list[dict] = []
    for group in duplicate_clusters[:max_listed]:
        members = []
        for mid in group:
            match = by_id[mid]
            members.append(
                {
                    "canonical_id": mid,
                    "namespace": _namespace(mid),
                    "sport": match.get("sport"),
                    "date": match.get("date"),
                    "time": match.get("time"),
                    "league": match.get("league"),
                    "home": match.get("home"),
                    "away": match.get("away"),
                    "competition_number": competition_number_for_match(dict(match)),
                    "status": match.get("status"),
                    "final_score": _final_score(match),
                }
            )
        label, evidence = classify_pair_evidence(
            by_id[group[0]], by_id[group[1]]
        )
        details.append(
            {
                "canonical_ids": list(group),
                "members": members,
                "evidence_label": label,
                "evidence": evidence,
                "evidence_class": DERIVED,
                "requires": "HUMAN_ADJUDICATION_BEFORE_CANONICAL_CHANGE",
            }
        )

    return {
        "duplicate_cluster_count": len(duplicate_clusters),
        "canonical_ids_in_duplicate_clusters": len(involved),
        "duplicate_clusters": details,
        "ambiguous_cluster_count": len(ambiguous_clusters),
        "ambiguous_clusters": [
            {"canonical_ids": group, "evidence_class": DERIVED}
            for group in ambiguous_clusters[:max_listed]
        ],
    }


def _drop_candidate(group: Sequence[str], matches_by_id: Mapping[str, Mapping[str, Any]]) -> str:
    """
    Deterministically pick the weaker record of a duplicate cluster.

    Preference: the member without a competition number (weaker identity), then
    the lexicographically largest ID (the later-derived alias).  This is a
    *recommendation for review*, never an executed correction.
    """
    def sort_key(match_id: str) -> tuple[int, str]:
        number = competition_number_for_match(dict(matches_by_id[match_id]))
        return (1 if number is not None else 0, match_id)

    return sorted(group, key=sort_key)[0]


def replay_planner(
    matches: Sequence[Mapping[str, Any]],
    duplicate_clusters: Sequence[Sequence[str]],
    max_steps: int = 8,
) -> dict:
    """
    Replay the ingestion planner over the real Canonical set, read-only.

    An idempotent replay must map every row to its own Canonical ID.  Because
    the planner is fail-closed, a real duplicate Canonical ownership blocks the
    whole batch — which is a business-blocking finding, not a crash.
    """
    rows = list(matches)
    by_id = {str(match.get("id")): match for match in matches}
    dropped: list[str] = []
    attempts: list[dict] = []

    def run(batch: Sequence[Mapping[str, Any]]) -> dict:
        try:
            plan = plan_ingestion_identity(list(batch), list(batch))
        except CanonicalIdentityCollisionError as exc:
            return {
                "status": "BLOCKED",
                "category": exc.category,
                "match_id": exc.match_id,
                "competing_canonical_ids": list(exc.competing_canonical_ids),
                "reason": exc.reason,
            }
        mismatches = [
            {"canonical_id": str(batch[index].get("id")), "planned_id": plan.canonical_id_for(index)}
            for index in range(len(batch))
            if plan.canonical_id_for(index) != str(batch[index].get("id"))
        ]
        return {
            "status": "OK",
            "components": len(plan.components),
            "new_components": len(plan.new_components),
            "idempotent_mismatch_count": len(mismatches),
            "idempotent_mismatch_examples": mismatches[:10],
        }

    initial = run(rows)
    attempts.append({"dropped": [], "result": initial})

    cluster_by_member: dict[str, Sequence[str]] = {}
    for group in duplicate_clusters:
        for member in group:
            cluster_by_member[member] = group

    current = initial
    for _step in range(max_steps):
        if current["status"] == "OK":
            break
        blocking = str(current.get("match_id") or "")
        group = cluster_by_member.get(blocking)
        if group is None:
            attempts.append(
                {
                    "stopped": True,
                    "reason": "conflict is not attributable to a duplicate cluster",
                    "match_id": blocking,
                }
            )
            break
        remaining = [member for member in group if member not in dropped]
        if not remaining:
            attempts.append({"stopped": True, "reason": "cluster exhausted", "match_id": blocking})
            break
        candidate = _drop_candidate(remaining, by_id)
        dropped.append(candidate)
        rows = [row for row in rows if str(row.get("id")) != candidate]
        current = run(rows)
        attempts.append({"dropped": list(dropped), "result": current})

    return {
        "raw_replay": initial,
        "final_replay": current,
        "review_only_recommended_drops": dropped,
        "steps": attempts,
        "rows_after_recommended_removal": len(rows),
        "note": (
            "Removals are computed in memory for measurement only; no Canonical "
            "record was modified."
        ),
    }


# =============================================================================
# Downstream linkage (read-only cross-store trace)
# =============================================================================

def _iso_date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    head = value[:10]
    parts = head.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return head


def analyze_downstream(dataset: Dataset) -> dict:
    """
    Trace each real event across the seven-store chain without touching it.

    A missing downstream record is reported as *absent*, not as an error: the
    lifecycle may not have reached that stage.  Only contradictory references
    (wrong match, unresolvable ID, disagreeing result) are counted as defects.
    """
    matches = dataset.matches
    canonical = {str(match.get("id")): match for match in matches}
    predictions: dict[str, list[str]] = defaultdict(list)
    odds: dict[str, list[str]] = defaultdict(list)
    settlements: dict[str, list[str]] = defaultdict(list)
    evaluations: dict[str, list[str]] = defaultdict(list)

    for snapshot_id in sorted(dataset.prediction_snapshots):
        record = dataset.prediction_snapshots[snapshot_id]
        predictions[str(record.get("match_id") or "")].append(snapshot_id)
    for snapshot_id in sorted(dataset.odds_snapshots):
        record = dataset.odds_snapshots[snapshot_id]
        odds[str(record.get("match_id") or "")].append(snapshot_id)
    for settlement_id in sorted(dataset.settlements):
        record = dataset.settlements[settlement_id]
        settlements[str(record.get("match_id") or "")].append(settlement_id)
    for evaluation_id in sorted(dataset.evaluation_rows):
        record = dataset.evaluation_rows[evaluation_id]
        evaluations[str(record.get("match_id") or "")].append(evaluation_id)

    def orphans(store: Mapping[str, list[str]]) -> list[str]:
        return sorted(key for key in store if key and key not in canonical)

    def duplicates(store: Mapping[str, list[str]]) -> list[str]:
        return sorted(key for key, values in store.items() if key and len(values) > 1)

    fingerprint_checks: dict[str, Counter] = {
        "prediction_snapshots.json": Counter(),
        "odds_snapshots.json": Counter(),
        "settlements.json": Counter(),
        "evaluation_rows.json": Counter(),
    }
    mismatch_examples: dict[str, list[dict]] = defaultdict(list)

    def note(store: str, kind: str, example: dict | None = None) -> None:
        fingerprint_checks[store][kind] += 1
        if example is not None and len(mismatch_examples[store]) < 15:
            mismatch_examples[store].append(example)

    for snapshot_id in sorted(dataset.prediction_snapshots):
        record = dataset.prediction_snapshots[snapshot_id]
        match = canonical.get(str(record.get("match_id") or ""))
        if match is None:
            note("prediction_snapshots.json", "ORPHANED_MATCH_REFERENCE")
            continue
        if snapshot_id != record.get("snapshot_id"):
            note("prediction_snapshots.json", "SNAPSHOT_KEY_MISMATCH",
                 {"snapshot_id": snapshot_id})
        for field, left, right in (
            ("sport", match.get("sport"), record.get("sport")),
            ("league", match.get("league"), record.get("league")),
        ):
            if left and right and _norm_team(left) != _norm_team(right):
                note("prediction_snapshots.json", f"{field.upper()}_MISMATCH",
                     {"snapshot_id": snapshot_id, "canonical": left, "record": right})
        home_relation = team_relation(match.get("home"), record.get("home_team"))
        away_relation = team_relation(match.get("away"), record.get("away_team"))
        for relation in (home_relation, away_relation):
            if relation != "EXACT":
                note("prediction_snapshots.json", f"TEAM_NAME_{relation}",
                     {
                         "snapshot_id": snapshot_id,
                         "canonical_home": match.get("home"),
                         "record_home": record.get("home_team"),
                         "canonical_away": match.get("away"),
                         "record_away": record.get("away_team"),
                     })
        if match.get("date") and record.get("match_date") and match["date"] != record["match_date"]:
            note("prediction_snapshots.json", "DATE_MISMATCH",
                 {"snapshot_id": snapshot_id, "canonical": match["date"], "record": record["match_date"]})
        kickoff_date = _iso_date(record.get("kickoff_at"))
        if match.get("date") and kickoff_date and match["date"] != kickoff_date:
            note("prediction_snapshots.json", "KICKOFF_DATE_MISMATCH",
                 {"snapshot_id": snapshot_id, "canonical": match["date"], "kickoff_at": record["kickoff_at"]})
        if match.get("time") and record.get("match_time") and match["time"] != record["match_time"]:
            note("prediction_snapshots.json", "KICKOFF_TIME_MISMATCH",
                 {"snapshot_id": snapshot_id, "canonical": match["time"], "record": record["match_time"]})

    for snapshot_id in sorted(dataset.odds_snapshots):
        record = dataset.odds_snapshots[snapshot_id]
        match = canonical.get(str(record.get("match_id") or ""))
        if match is None:
            note("odds_snapshots.json", "ORPHANED_MATCH_REFERENCE")
            continue
        if snapshot_id != record.get("snapshot_id"):
            note("odds_snapshots.json", "SNAPSHOT_KEY_MISMATCH", {"snapshot_id": snapshot_id})
        if match.get("sport") and record.get("sport") and match["sport"] != record["sport"]:
            note("odds_snapshots.json", "SPORT_MISMATCH",
                 {"snapshot_id": snapshot_id, "canonical": match["sport"], "record": record["sport"]})
        for relation in (
            team_relation(match.get("home"), record.get("home_team")),
            team_relation(match.get("away"), record.get("away_team")),
        ):
            if relation != "EXACT":
                note("odds_snapshots.json", f"TEAM_NAME_{relation}",
                     {"snapshot_id": snapshot_id, "record": {
                         "home": record.get("home_team"), "away": record.get("away_team")}})
        if match.get("date") and record.get("match_date") and match["date"] != record["match_date"]:
            note("odds_snapshots.json", "DATE_MISMATCH",
                 {"snapshot_id": snapshot_id, "canonical": match["date"], "record": record["match_date"]})

    settlement_outcome_mismatch = 0
    settlement_result_mismatch = 0
    settlement_snapshot_missing = 0
    settlement_match_mismatch = 0
    for settlement_id in sorted(dataset.settlements):
        record = dataset.settlements[settlement_id]
        match_id = str(record.get("match_id") or "")
        match = canonical.get(match_id)
        snapshot = dataset.prediction_snapshots.get(str(record.get("snapshot_id") or ""))
        if snapshot is None:
            settlement_snapshot_missing += 1
            note("settlements.json", "MISSING_SNAPSHOT_REFERENCE", {"settlement_id": settlement_id})
        elif str(snapshot.get("match_id") or "") != match_id:
            settlement_match_mismatch += 1
            note("settlements.json", "SNAPSHOT_MATCH_MISMATCH",
                 {"settlement_id": settlement_id, "settlement_match": match_id,
                  "snapshot_match": snapshot.get("match_id")})
        if match is None:
            note("settlements.json", "ORPHANED_MATCH_REFERENCE", {"settlement_id": settlement_id})
        final_score = record.get("final_score")
        if isinstance(final_score, Mapping) and final_score.get("home") is not None:
            score = {"home": final_score.get("home"), "away": final_score.get("away")}
            if record.get("actual_outcome") != outcome_from_score(score):
                settlement_outcome_mismatch += 1
                note("settlements.json", "OUTCOME_INCONSISTENT_WITH_SCORE", {"settlement_id": settlement_id})
            if record.get("result_fingerprint") != result_fingerprint(score):
                settlement_result_mismatch += 1
                note("settlements.json", "RESULT_FINGERPRINT_MISMATCH", {"settlement_id": settlement_id})
            if match is not None:
                canonical_score = _final_score(match)
                if canonical_score is not None and canonical_score != score:
                    note("settlements.json", "CANONICAL_SCORE_DISAGREES",
                         {"settlement_id": settlement_id, "settlement": score,
                          "canonical": canonical_score})
        teams = (record.get("home_team"), record.get("away_team"))
        if match is not None and teams[0] and teams[1]:
            for relation in (team_relation(match.get("home"), teams[0]),
                             team_relation(match.get("away"), teams[1])):
                if relation != "EXACT":
                    note("settlements.json", f"TEAM_NAME_{relation}", {"settlement_id": settlement_id})

    evaluation_snapshot_missing = 0
    evaluation_settlement_missing = 0
    evaluation_match_mismatch = 0
    for evaluation_id in sorted(dataset.evaluation_rows):
        record = dataset.evaluation_rows[evaluation_id]
        match_id = str(record.get("match_id") or "")
        snapshot = dataset.prediction_snapshots.get(str(record.get("snapshot_id") or ""))
        settlement = dataset.settlements.get(str(record.get("settlement_id") or ""))
        if snapshot is None:
            evaluation_snapshot_missing += 1
            note("evaluation_rows.json", "MISSING_SNAPSHOT_REFERENCE", {"evaluation_id": evaluation_id})
        elif str(snapshot.get("match_id") or "") != match_id:
            evaluation_match_mismatch += 1
            note("evaluation_rows.json", "SNAPSHOT_MATCH_MISMATCH",
                 {"evaluation_id": evaluation_id, "row_match": match_id,
                  "snapshot_match": snapshot.get("match_id")})
        if settlement is None:
            evaluation_settlement_missing += 1
            note("evaluation_rows.json", "MISSING_SETTLEMENT_REFERENCE", {"evaluation_id": evaluation_id})
        else:
            if str(settlement.get("match_id") or "") != match_id:
                evaluation_match_mismatch += 1
                note("evaluation_rows.json", "SETTLEMENT_MATCH_MISMATCH",
                     {"evaluation_id": evaluation_id, "row_match": match_id,
                      "settlement_match": settlement.get("match_id")})
            if settlement.get("final_score") is not None and record.get("final_score") is not None:
                if settlement.get("final_score") != record.get("final_score"):
                    note("evaluation_rows.json", "FINAL_SCORE_MISMATCH", {"evaluation_id": evaluation_id})
            if settlement.get("actual_outcome") != record.get("actual_outcome"):
                note("evaluation_rows.json", "OUTCOME_MISMATCH", {"evaluation_id": evaluation_id})
            if settlement.get("model_version") != record.get("model_version"):
                note("evaluation_rows.json", "MODEL_VERSION_MISMATCH", {"evaluation_id": evaluation_id})
        if match_id not in canonical:
            note("evaluation_rows.json", "ORPHANED_MATCH_REFERENCE", {"evaluation_id": evaluation_id})

    finished = [match for match in matches if str(match.get("status")) == "finished"]
    finished_with_score = [match for match in finished if _final_score(match) is not None]

    chain_histogram = Counter()
    events_without_prediction: list[str] = []
    events_without_odds: list[str] = []
    for match in matches:
        match_id = str(match.get("id"))
        present = (
            bool(predictions.get(match_id)),
            bool(odds.get(match_id)),
            bool(settlements.get(match_id)),
            bool(evaluations.get(match_id)),
        )
        chain_histogram[
            "prediction=%d,odds=%d,settlement=%d,evaluation=%d" % tuple(int(flag) for flag in present)
        ] += 1
        if not present[0]:
            events_without_prediction.append(match_id)
        if not present[1]:
            events_without_odds.append(match_id)

    return {
        "canonical_events": len(matches),
        "prediction_snapshots": len(dataset.prediction_snapshots),
        "odds_snapshots": len(dataset.odds_snapshots),
        "settlements": len(dataset.settlements),
        "evaluation_rows": len(dataset.evaluation_rows),
        "distinct_events_with_prediction": len([key for key in predictions if key in canonical]),
        "distinct_events_with_odds": len([key for key in odds if key in canonical]),
        "distinct_events_with_settlement": len([key for key in settlements if key in canonical]),
        "distinct_events_with_evaluation": len([key for key in evaluations if key in canonical]),
        "orphaned_prediction_snapshots": len(orphans(predictions)),
        "orphaned_odds_snapshots": len(orphans(odds)),
        "orphaned_settlements": len(orphans(settlements)),
        "orphaned_evaluation_rows": len(orphans(evaluations)),
        "orphan_examples": {
            "prediction_snapshots": orphans(predictions)[:5],
            "odds_snapshots": orphans(odds)[:5],
            "settlements": orphans(settlements)[:5],
            "evaluation_rows": orphans(evaluations)[:5],
        },
        "events_with_multiple_prediction_snapshots": len(duplicates(predictions)),
        "events_with_multiple_odds_snapshots": len(duplicates(odds)),
        "events_with_multiple_settlements": len(duplicates(settlements)),
        "events_with_multiple_evaluation_rows": len(duplicates(evaluations)),
        "finished_events": len(finished),
        "finished_events_with_final_score": len(finished_with_score),
        "finished_events_without_final_score": sorted(
            str(match.get("id")) for match in finished if _final_score(match) is None
        ),
        "settlement_snapshot_missing": settlement_snapshot_missing,
        "settlement_snapshot_match_mismatch": settlement_match_mismatch,
        "settlement_outcome_inconsistent_with_score": settlement_outcome_mismatch,
        "settlement_result_fingerprint_mismatch": settlement_result_mismatch,
        "evaluation_snapshot_missing": evaluation_snapshot_missing,
        "evaluation_settlement_missing": evaluation_settlement_missing,
        "evaluation_match_mismatch": evaluation_match_mismatch,
        "chain_linkage_histogram": dict(sorted(chain_histogram.items())),
        "events_without_prediction_sample": events_without_prediction[:15],
        "events_without_odds_sample": events_without_odds[:15],
        "reference_checks": {
            store: dict(sorted(counter.items())) for store, counter in sorted(fingerprint_checks.items())
        },
        "mismatch_examples": {store: examples for store, examples in sorted(mismatch_examples.items())},
        "store_reference_enumeration": enumerate_store_references(
            {
                name: dataset.stores[name].payload
                for name in PROTECTED_STORES
                if name in dataset.stores
            }
        ),
    }


# =============================================================================
# Latency
# =============================================================================

def measure_latency(matches: Sequence[Mapping[str, Any]]) -> dict:
    """Local, machine-dependent timing of the existing identity path (no I/O)."""
    rows = list(matches)
    started = time.perf_counter()
    observations = [observe_source_row(row, adapter=adapter_for_match_id(row.get("id"))[0])
                    for row in rows]
    observe_seconds = time.perf_counter() - started

    buckets: dict[tuple[Any, Any], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[(row.get("sport"), row.get("date"))].append(row)
    started = time.perf_counter()
    pairs = 0
    for bucket_key in sorted(buckets, key=lambda item: (str(item[0]), str(item[1]))):
        members = sorted(buckets[bucket_key], key=lambda row: str(row.get("id")))
        for left_index in range(len(members)):
            for right_index in range(left_index + 1, len(members)):
                pairs += 1
                same_event(dict(members[left_index]), dict(members[right_index]))
    pair_seconds = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(3):
        try:
            plan_ingestion_identity(rows, rows)
        except CanonicalIdentityCollisionError:
            pass
    plan_seconds = (time.perf_counter() - started) / 3

    count = len(rows) or 1
    return {
        "events": len(rows),
        "observations": len(observations),
        "pairs_compared": pairs,
        "observe_source_row_ms": round(observe_seconds * 1000.0, 3),
        "observe_source_row_us_per_event": round(observe_seconds * 1e6 / count, 2),
        "same_event_pairwise_ms": round(pair_seconds * 1000.0, 3),
        "same_event_us_per_pair": round(pair_seconds * 1e6 / (pairs or 1), 2),
        "plan_ingestion_identity_ms": round(plan_seconds * 1000.0, 3),
        "plan_ingestion_identity_us_per_event": round(plan_seconds * 1e6 / count, 2),
        "extrapolated_ms_per_1000_events": {
            "observe_source_row": round(observe_seconds * 1000.0 / count * 1000.0, 2),
            "plan_ingestion_identity": round(plan_seconds * 1000.0 / count * 1000.0, 2),
        },
        "evidence_class": OBSERVED,
        "limitations": (
            "Single local run on the audit host; timings are wall-clock, "
            "exclude file I/O and are not reproducible across machines. The "
            "1000-event figures are linear extrapolations, hence INFERRED."
        ),
    }


# =============================================================================
# Metric assembly
# =============================================================================

def _metric(
    metric_id: str,
    name: str,
    definition: str,
    *,
    numerator: int | None,
    denominator: int | None,
    data_scope: str,
    evidence_quality: str,
    limitations: str,
    unit: str = "events",
    status: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict:
    """One uniformly-shaped metric record with explicit denominators."""
    if status is None:
        if numerator is None or denominator is None:
            status = "N/A"
        elif denominator == 0:
            status = "N/A_ZERO_DENOMINATOR"
        else:
            status = "MEASURED"
    value = None
    if numerator is not None and denominator:
        value = round(numerator / denominator, 4)
    payload = {
        "id": metric_id,
        "name": name,
        "definition": definition,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": unit,
        "status": status,
        "data_scope": data_scope,
        "evidence_quality": evidence_quality,
        "limitations": limitations,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def build_metrics(
    inventory: Mapping[str, Any],
    identity: Mapping[str, Any],
    pairs: Mapping[str, Any],
    ownership: Mapping[str, Any],
    planner: Mapping[str, Any],
    downstream: Mapping[str, Any],
    latency: Mapping[str, Any],
) -> list[dict]:
    """The required metric set, each with an explicit numerator/denominator."""
    events = int(inventory["events"])
    observations = int(identity["observations"])
    scope_all = f"all {events} Canonical events in daily_matches.json"
    proxy_note = (
        "Reference label is a field-evidence proxy, not an independently "
        "human-reviewed label; the value describes agreement, not accuracy."
    )
    metrics: list[dict] = []

    metrics.append(
        _metric(
            "identity_completeness_rate",
            "Identity completeness rate",
            "Events whose scoped identity key is sufficient for an active binding "
            "(COMPETITION_NUMBER or VERIFIED RAW_PROVIDER_ID).",
            numerator=int(identity["sufficient_identity_keys"]),
            denominator=observations,
            data_scope=scope_all,
            evidence_quality=OBSERVED,
            limitations=(
                "The normalised rows carry no preserved provider identifier, so "
                "RAW_PROVIDER_ID evidence is structurally unavailable."
            ),
        )
    )
    metrics.append(
        _metric(
            "duplicate_source_key_rate",
            "Duplicate source-key rate",
            "Sufficient scoped identity keys claimed by more than one Canonical ID.",
            numerator=int(identity["duplicate_identity_key_count"]),
            denominator=int(identity["sufficient_identity_keys"]),
            data_scope=scope_all,
            evidence_quality=DERIVED,
            limitations=(
                "Derived from the persisted identity fields only; a shared key is "
                "a conflict signal, not proof of a wrong mapping."
            ),
        )
    )
    metrics.append(
        _metric(
            "canonical_conflict_rate",
            "Canonical conflict rate",
            "Canonical IDs that share a single real event according to the "
            "field-evidence proxy.",
            numerator=int(ownership["canonical_ids_in_duplicate_clusters"]),
            denominator=events,
            data_scope=scope_all,
            evidence_quality=DERIVED,
            limitations=(
                "Duplicate ownership is derived from field evidence and requires "
                "human adjudication; no Canonical record was changed."
            ),
        )
    )
    final_replay = planner["final_replay"]
    metrics.append(
        _metric(
            "automatic_mapping_coverage",
            "Automatic mapping coverage",
            "Events the ingestion planner assigns to their own Canonical ID in a "
            "conflict-free idempotent replay.",
            numerator=int(planner["rows_after_recommended_removal"]),
            denominator=events,
            data_scope=scope_all,
            evidence_quality=OBSERVED,
            limitations=(
                "Raw replay is BLOCKED (%s); the numerator is measured after "
                "review-only removal of the conflicting records, so it is an upper "
                "bound, not current production coverage."
                % planner["raw_replay"].get("category", planner["raw_replay"].get("status"))
            ),
            extra={"raw_replay_status": planner["raw_replay"].get("status")},
        )
    )
    metrics.append(
        _metric(
            "automatic_mapping_precision",
            "Automatic mapping precision (independently reviewed)",
            "Correct automatic mappings divided by all automatic mappings, using "
            "independent human-reviewed labels.",
            numerator=None,
            denominator=None,
            data_scope="no reviewed dataset exists in this repository",
            evidence_quality=UNKNOWN,
            limitations=HUMAN_LABEL_AVAILABILITY + "; not computable without review.",
            status="N/A_NO_INDEPENDENT_LABELS",
        )
    )
    metrics.append(
        _metric(
            "mapping_recall",
            "Mapping recall (independently reviewed)",
            "Correctly associated real events divided by all real events, using "
            "independent human-reviewed labels.",
            numerator=None,
            denominator=None,
            data_scope="no reviewed dataset exists in this repository",
            evidence_quality=UNKNOWN,
            limitations=HUMAN_LABEL_AVAILABILITY + "; not computable without review.",
            status="N/A_NO_INDEPENDENT_LABELS",
        )
    )
    metrics.append(
        _metric(
            "proxy_false_positive_rate",
            "Proxy false-positive mapping rate",
            "Pairs reported SAME_EVENT by same_event() but contradicted or "
            "downgraded by field evidence, over all same_event() positives.",
            numerator=int(pairs["proxy_false_positive_count"]),
            denominator=int(pairs["algorithm_positive_pairs"]),
            data_scope=f"{pairs['scoped_pairs_compared']} pairs sharing (sport, date)",
            evidence_quality=DERIVED,
            limitations=proxy_note,
        )
    )
    metrics.append(
        _metric(
            "proxy_false_negative_rate",
            "Proxy false-negative mapping rate",
            "Pairs supported SAME_EVENT by field evidence but not joined by "
            "same_event(), over all evidence-supported pairs.",
            numerator=int(pairs["proxy_false_negative_count"]),
            denominator=int(pairs["evidence_positive_pairs"]),
            data_scope=f"{pairs['scoped_pairs_compared']} pairs sharing (sport, date)",
            evidence_quality=DERIVED,
            limitations=proxy_note,
        )
    )
    metrics.append(
        _metric(
            "proxy_downgraded_candidate_pairs",
            "Proxy-downgraded candidate pairs",
            "Same-date pairs whose teams differ only by provider-side truncation, "
            "same kickoff and one competition number, downgraded to AMBIGUOUS.",
            numerator=int(pairs["ambiguous_pair_profile"].get("same_kickoff", 0)),
            denominator=int(pairs["ambiguous_pair_count"]),
            data_scope=f"{pairs['scoped_pairs_compared']} pairs sharing (sport, date)",
            evidence_quality=DERIVED,
            limitations=(
                "Name truncation cannot be resolved from field evidence; these are "
                "candidate duplicates for human review, not confirmed misses."
            ),
        )
    )
    metrics.append(
        _metric(
            "ambiguous_event_review_rate",
            "Ambiguous event review rate",
            "Canonical events involved in an evidence-ambiguous pair that needs "
            "human confirmation.",
            numerator=sum(len(group) for group in
                          [item["canonical_ids"] for item in ownership["ambiguous_clusters"]]),
            denominator=events,
            data_scope=scope_all,
            evidence_quality=DERIVED,
            limitations=(
                "Ambiguity is derived from truncated team names / conflicting "
                "auxiliary fields; the set is a review queue, not an error list."
            ),
        )
    )
    metrics.append(
        _metric(
            "insufficient_evidence_rate",
            "Insufficient-evidence rate",
            "Events whose scoped identity key is not sufficient for an active binding.",
            numerator=int(identity["insufficient_identity_keys"]),
            denominator=observations,
            data_scope=scope_all,
            evidence_quality=OBSERVED,
            limitations="Fail-closed by design; these rows can only be reviewed.",
        )
    )
    metrics.append(
        _metric(
            "prediction_snapshot_linkage_rate",
            "Prediction snapshot linkage rate",
            "Events with at least one prediction snapshot whose match_id resolves.",
            numerator=int(downstream["distinct_events_with_prediction"]),
            denominator=events,
            data_scope=scope_all,
            evidence_quality=OBSERVED,
            limitations=(
                "All events are market-tracked in this dataset; a missing snapshot "
                "may also reflect a lifecycle stage that has not been reached."
            ),
        )
    )
    events_with_odds = events - int(inventory["missing_field_counts"].get("odds", 0))
    metrics.append(
        _metric(
            "odds_snapshot_linkage_rate",
            "Odds snapshot linkage rate",
            "Events with at least one odds snapshot whose match_id resolves.",
            numerator=int(downstream["distinct_events_with_odds"]),
            denominator=events_with_odds,
            data_scope="events that carry an odds block",
            evidence_quality=OBSERVED,
            limitations=(
                "Denominator excludes events with no odds block, because no odds "
                "snapshot can exist for them."
            ),
        )
    )
    metrics.append(
        _metric(
            "final_score_linkage_rate",
            "Final-score linkage rate",
            "Finished events carrying a valid full-time score.",
            numerator=int(downstream["finished_events_with_final_score"]),
            denominator=int(downstream["finished_events"]),
            data_scope="events with status=finished",
            evidence_quality=OBSERVED,
            limitations=(
                "A finished event without a score is an absent attachment; no "
                "result was recalculated or repaired."
            ),
        )
    )
    settlement_total = int(downstream["settlements"])
    settlement_defects = (
        int(downstream["settlement_snapshot_missing"])
        + int(downstream["settlement_snapshot_match_mismatch"])
        + int(downstream["settlement_outcome_inconsistent_with_score"])
        + int(downstream["settlement_result_fingerprint_mismatch"])
        + int(downstream["orphaned_settlements"])
    )
    metrics.append(
        _metric(
            "settlement_linkage_consistency_rate",
            "Settlement linkage consistency rate",
            "Settlements whose snapshot resolves, whose match agrees and whose "
            "outcome/fingerprint match the stored final score.",
            numerator=max(0, settlement_total - settlement_defects),
            denominator=settlement_total,
            data_scope="all stored settlements",
            evidence_quality=OBSERVED,
            limitations=(
                "Consistency only; the settlement formula was not re-derived and "
                "no historical settlement was recalculated."
            ),
        )
    )
    evaluation_total = int(downstream["evaluation_rows"])
    evaluation_defects = (
        int(downstream["evaluation_snapshot_missing"])
        + int(downstream["evaluation_settlement_missing"])
        + int(downstream["evaluation_match_mismatch"])
        + int(downstream["orphaned_evaluation_rows"])
        + int(downstream["reference_checks"].get("evaluation_rows.json", {}).get("FINAL_SCORE_MISMATCH", 0))
        + int(downstream["reference_checks"].get("evaluation_rows.json", {}).get("OUTCOME_MISMATCH", 0))
        + int(downstream["reference_checks"].get("evaluation_rows.json", {}).get("MODEL_VERSION_MISMATCH", 0))
    )
    metrics.append(
        _metric(
            "evaluation_row_linkage_consistency_rate",
            "Evaluation-row linkage consistency rate",
            "Evaluation rows whose snapshot and settlement resolve and whose "
            "match, result and model version agree.",
            numerator=max(0, evaluation_total - evaluation_defects),
            denominator=evaluation_total,
            data_scope="all stored evaluation rows",
            evidence_quality=OBSERVED,
            limitations=(
                "Consistency only; evaluation formulas were not recomputed."
            ),
        )
    )
    elapsed_us = int(
        round(
            (latency["observe_source_row_ms"] + latency["same_event_pairwise_ms"]
             + latency["plan_ingestion_identity_ms"]) * 1000.0
        )
    )
    metrics.append(
        _metric(
            "processing_latency",
            "Processing latency",
            "Cumulative wall-clock microseconds of observe_source_row, pairwise "
            "same_event and plan_ingestion_identity, per real event.",
            numerator=elapsed_us,
            denominator=int(latency["events"]),
            data_scope=f"{latency['events']} events, {latency['pairs_compared']} pairs",
            evidence_quality=OBSERVED,
            limitations=str(latency["limitations"]),
            unit="microseconds_per_event",
            status="MEASURED_LOCAL_ONLY" if latency["events"] else None,
            extra={
                "observe_source_row_ms": latency["observe_source_row_ms"],
                "same_event_pairwise_ms": latency["same_event_pairwise_ms"],
                "plan_ingestion_identity_ms": latency["plan_ingestion_identity_ms"],
                "total_ms": round(elapsed_us / 1000.0, 3),
            },
        )
    )
    review_ids: set[str] = set()
    for cluster in ownership["duplicate_clusters"]:
        review_ids.update(cluster["canonical_ids"])
    for cluster in ownership["ambiguous_clusters"]:
        review_ids.update(cluster["canonical_ids"])
    review_ids.update(identity["insufficient_identity_canonical_ids"])
    review_ids.update(downstream["finished_events_without_final_score"])
    metrics.append(
        _metric(
            "estimated_manual_review_workload",
            "Estimated manual-review workload",
            "Distinct Canonical records that need human confirmation: duplicate "
            "owners, ambiguous equivalences, insufficient identities and "
            "finished events without a result.",
            numerator=len(review_ids),
            denominator=events,
            data_scope=scope_all,
            evidence_quality=INFERRED,
            limitations=(
                "Counts review items, not minutes or cost; no per-item effort "
                "assumption is asserted."
            ),
            unit="review_items",
            status="ESTIMATE" if events else None,
        )
    )
    metrics.append(
        _metric(
            "identifier_instability_rate",
            "Identifier instability rate",
            "Events whose persisted identifier is row-position derived, "
            "provider-derived or unknown rather than competition-number scoped.",
            numerator=int(identity["unstable_identifier_count"]),
            denominator=observations,
            data_scope=scope_all,
            evidence_quality=OBSERVED,
            limitations=(
                "Instability is classified from the identifier literal and the "
                "adapter; no provider documentation exists in this repository."
            ),
        )
    )
    return metrics


# =============================================================================
# Review queue
# =============================================================================

def build_review_queue(
    identity: Mapping[str, Any],
    ownership: Mapping[str, Any],
    downstream: Mapping[str, Any],
    limit: int = 25,
) -> dict:
    """Explicit queue of items that need independent human confirmation."""
    items: list[dict] = []
    for cluster in ownership["duplicate_clusters"]:
        items.append(
            {
                "kind": "DUPLICATE_CANONICAL_OWNERSHIP",
                "canonical_ids": list(cluster["canonical_ids"]),
                "evidence_label": cluster["evidence_label"],
                "evidence_class": DERIVED,
                "proposed_label": SAME_EVENT,
                "reviewer_label": None,
                "confidence": "FIELD_EVIDENCE_STRONG",
                "action": "HUMAN_ADJUDICATION_REQUIRED",
                "detail": cluster["members"],
            }
        )
    for cluster in ownership["ambiguous_clusters"]:
        items.append(
            {
                "kind": "AMBIGUOUS_EQUIVALENCE",
                "canonical_ids": list(cluster["canonical_ids"]),
                "evidence_class": DERIVED,
                "proposed_label": AMBIGUOUS,
                "reviewer_label": None,
                "confidence": "AMBIGUOUS",
                "action": "HUMAN_ADJUDICATION_REQUIRED",
            }
        )
    for mid in identity["insufficient_identity_canonical_ids"][:limit]:
        items.append(
            {
                "kind": "INSUFFICIENT_SOURCE_IDENTITY",
                "canonical_ids": [mid],
                "evidence_class": OBSERVED,
                "proposed_label": INSUFFICIENT_EVIDENCE,
                "reviewer_label": None,
                "confidence": "FAIL_CLOSED",
                "action": "PROVIDER_IDENTITY_EVIDENCE_REQUIRED",
            }
        )
    for mid in downstream["finished_events_without_final_score"][:limit]:
        items.append(
            {
                "kind": "FINISHED_WITHOUT_FINAL_SCORE",
                "canonical_ids": [mid],
                "evidence_class": OBSERVED,
                "proposed_label": INSUFFICIENT_EVIDENCE,
                "reviewer_label": None,
                "confidence": "OBSERVED_GAP",
                "action": "RESULT_ATTACHMENT_REVIEW",
            }
        )
    total = (
        len(ownership["duplicate_clusters"])
        + len(ownership["ambiguous_clusters"])
        + len(identity["insufficient_identity_canonical_ids"])
        + len(downstream["finished_events_without_final_score"])
    )
    return {
        "total": total,
        "items": items[:limit],
        "truncated": max(0, total - len(items[:limit])),
        "label_provenance": LABEL_PROVENANCE,
        "human_label_availability": HUMAN_LABEL_AVAILABILITY,
    }


# =============================================================================
# Report assembly
# =============================================================================

def compute_audit(dataset: Dataset, *, review_limit: int = 25) -> dict:
    """Compute the full read-only audit report from a loaded dataset."""
    observed = observe_canonical(dataset.matches)
    inventory = build_inventory(dataset, observed)
    identity = analyze_identity(observed)
    pairs = analyze_pairs(dataset.matches)
    ownership = analyze_canonical_ownership(dataset.matches)
    planner = replay_planner(dataset.matches, ownership["duplicate_clusters"] and
                             [tuple(item["canonical_ids"]) for item in ownership["duplicate_clusters"]])
    downstream = analyze_downstream(dataset)
    latency = measure_latency(dataset.matches)

    metrics = build_metrics(
        inventory, identity, pairs, ownership, planner, downstream, latency
    )

    review = build_review_queue(identity, ownership, downstream, limit=review_limit)

    return {
        "audit_kind": AUDIT_KIND,
        "report_kind": REPORT_KIND,
        "audit_schema_version": AUDIT_SCHEMA_VERSION,
        "determinism": "all collections ordered by Canonical ID or identity key",
        "read_only": True,
        "network_access": "none",
        "production_writes": "none",
        "dataset": {
            "data_dir": os.path.dirname(
                dataset.stores[CANONICAL_STORE].path
            ),
            "dataset_identity": dataset.stores[CANONICAL_STORE].sha256,
            "store_sha256": dataset.hashes(),
            "stores_present": [name for name in PROTECTED_STORES if name in dataset.stores],
            "stores_absent": [name for name in PROTECTED_STORES if name not in dataset.stores],
        },
        "inventory": inventory,
        "identity": identity,
        "pairs": pairs,
        "canonical_ownership": ownership,
        "planner_replay": planner,
        "downstream": downstream,
        "latency": latency,
        "metrics": metrics,
        "review_queue": review,
        "evidence_classes": {
            "OBSERVED": "read directly from the protected stores",
            "DERIVED": "computed by an existing implementation or an explicit rule",
            "INFERRED": "extrapolated, adapter-attribute or workload estimate",
            "UNKNOWN": "not measurable with the available evidence",
        },
        "ground_truth": {
            "human_reviewed_labels_available": False,
            "proxy_label_rule": "classify_pair_evidence (field evidence, not same_event)",
            "proxy_label_provenance": LABEL_PROVENANCE,
        },
        "production_activation_ready": PRODUCTION_ACTIVATION_READY,
        "decision": DECISION,
    }


def deterministic_view(report: Mapping[str, Any]) -> dict:
    """Drop the machine-dependent latency data for byte-comparison of runs."""
    view = {key: value for key, value in report.items() if key != "latency"}
    view["metrics"] = [
        {
            key: value
            for key, value in metric.items()
            if key
            not in (
                "numerator",
                "value",
                "observe_source_row_ms",
                "same_event_pairwise_ms",
                "plan_ingestion_identity_ms",
                "total_ms",
            )
            or metric.get("id") != "processing_latency"
        }
        for metric in report.get("metrics", [])
    ]
    return view


def verify_protected_stores(dataset: Dataset) -> dict[str, str]:
    """Re-hash every loaded protected store and compare with the baseline."""
    after: dict[str, str] = {}
    for name, store in dataset.stores.items():
        after[name] = sha256_file(store.path)
    changed = sorted(
        name for name in after if after[name] != dataset.hashes().get(name)
    )
    if changed:
        raise AuditProtectedMutationError(
            f"protected store(s) changed during the read-only audit: {changed}"
        )
    return after


def render_summary(report: Mapping[str, Any]) -> str:
    """Compact human-readable summary of a computed report."""
    inventory = report["inventory"]
    identity = report["identity"]
    downstream = report["downstream"]
    planner = report["planner_replay"]
    lines = [
        "REAL SPORTS IDENTITY AUDIT (read-only)",
        "events=%d sports=%s dates=%s..%s"
        % (
            inventory["events"],
            inventory["sport_distribution"],
            inventory["date_range"]["min"],
            inventory["date_range"]["max"],
        ),
        "identity: sufficient=%d insufficient=%d conflicts=%d duplicate_keys=%d"
        % (
            identity["sufficient_identity_keys"],
            identity["insufficient_identity_keys"],
            identity["conflict_count"],
            identity["duplicate_identity_key_count"],
        ),
        "canonical duplicate clusters=%d ambiguous clusters=%d"
        % (
            report["canonical_ownership"]["duplicate_cluster_count"],
            report["canonical_ownership"]["ambiguous_cluster_count"],
        ),
        "planner raw replay=%s final=%s"
        % (planner["raw_replay"].get("status"), planner["final_replay"].get("status")),
        "linkage: prediction=%d odds=%d settlements=%d evaluations=%d"
        % (
            downstream["distinct_events_with_prediction"],
            downstream["distinct_events_with_odds"],
            downstream["distinct_events_with_settlement"],
            downstream["distinct_events_with_evaluation"],
        ),
        "orphans: pred=%d odds=%d settle=%d eval=%d"
        % (
            downstream["orphaned_prediction_snapshots"],
            downstream["orphaned_odds_snapshots"],
            downstream["orphaned_settlements"],
            downstream["orphaned_evaluation_rows"],
        ),
        "activation_ready=%s decision=%s"
        % (report["production_activation_ready"], report["decision"]),
    ]
    for metric in report["metrics"]:
        lines.append(
            "  %-42s %-8s %s/%s"
            % (
                metric["id"],
                metric["status"],
                metric["numerator"],
                metric["denominator"],
            )
        )
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def _parse_store_overrides(values: Sequence[str] | None) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise AuditInputError(f"--store expects NAME=PATH, got {value!r}")
        name, path = value.split("=", 1)
        if name not in PROTECTED_STORES:
            raise AuditInputError(f"unknown store name in --store: {name!r}")
        overrides[name] = path
    return overrides


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only real sports identity and Canonical mapping evidence audit. "
            "Never writes to the protected stores and never enables activation."
        )
    )
    parser.add_argument("--data-dir", default=None, help="directory holding the seven stores")
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        metavar="NAME=PATH",
        help="explicit path override for one protected store (repeatable)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="write the JSON report here; rejected inside the production checkout",
    )
    parser.add_argument(
        "--review-limit", type=int, default=25, help="maximum review-queue items to keep"
    )
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    parser.add_argument(
        "--summary", action="store_true", help="print the text summary instead of JSON"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(list(argv) if argv is not None else None)
    try:
        dataset = load_dataset(
            args.data_dir, _parse_store_overrides(args.store)
        )
        report = compute_audit(dataset, review_limit=max(1, args.review_limit))
        after = verify_protected_stores(dataset)
        report["dataset"]["store_sha256_after"] = after
        report["dataset"]["stores_unchanged"] = True
    except AuditInputError as exc:
        print(f"AUDIT INPUT ERROR: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except AuditProtectedMutationError as exc:
        print(f"AUDIT ABORTED: {exc}", file=sys.stderr)
        return EXIT_PROTECTED_CHANGE

    text = render_summary(report)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)

    if args.output:
        try:
            assert_safe_path(args.output, context="audit report output")
        except ProtectedPathViolationError as exc:
            print(f"AUDIT ABORTED: {exc}", file=sys.stderr)
            return EXIT_PROTECTED_CHANGE
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.write("\n")

    if args.json:
        print(payload)
    elif args.summary or not args.output:
        print(text)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())