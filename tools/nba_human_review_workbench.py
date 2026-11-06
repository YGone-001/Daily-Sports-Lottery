"""
tools/nba_human_review_workbench.py
===================================
Offline, read-only **operator workbench** for the NBA cross-source duplicates.

What it does
------------
Turns the Phase 2-X discrepancy evidence into something a human can actually work with:

- ``prepare``  : prioritized NBA review queue, one Markdown dossier per candidate, a blank
                 human-review template, an orientation-evidence template and import notes
- ``show``     : one candidate's dossier plus its structured queue entry
- ``validate`` : strict import of a human review file (reusing the existing validator)
- ``summary``  : coverage dashboard, unique downstream impact, unresolved/contradictory register

What it is not
--------------
- Not an approval system and not a canonical merge tool. It never writes ``data/``, never
  chooses a surviving Canonical ID, never swaps home/away or any price.
- Not an identity engine. Runtime identity remains ``utils.match_identity.same_event()``;
  candidate evidence stays non-authoritative (``utils.basketball_identity_evidence``).
- A human review decision recorded here is **review evidence only**; it is not a signed
  organizational authorization and cannot authorize a production Canonical change.

Hard constraints
----------------
1. Read-only over the seven protected stores (SHA-256 captured, never written).
2. No network access: external orientation evidence is supplied by the operator as a file.
3. Deterministic output: stable ordering, no generated timestamps.
4. Output paths restricted to the Git-ignored ``reports/`` or outside the checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from tools.nba_identity_discrepancy_audit import build_candidates, command_scan
from tools.real_sports_duplicate_review import (
    DECIDED_VALUES,
    DECISION_DIFFERENT_EVENT,
    DECISION_FURTHER_REVIEW,
    DECISION_INSUFFICIENT,
    DECISION_SAME_EVENT,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_PROTECTED_PATH,
    EXIT_REVIEW_REJECTED,
    GOVERNANCE_EVIDENCE_REQUIRED,
    PROHIBITED_REVIEW_KEYS,
    PROVENANCE_HUMAN,
    REVIEW_RECORD_KEYS,
    REVIEW_SCHEMA_VERSION,
    UNREVIEWED,
    ProtectedOutputError,
    ReviewInputError,
    _bounded,
    _reference_index,
    dataset_fingerprint,
    load_reviews,
    validate_output_path,
    validate_reviews,
    write_json,
    write_text,
)
from tools.real_sports_identity_audit import AuditInputError, Dataset, load_dataset
from utils.basketball_identity_evidence import (
    CLASS_BOTH,
    CLASS_INSUFFICIENT,
    ORIENTATION_REVERSED,
    ORIENTATION_SAME,
    ORIENTATION_UNKNOWN,
    OUTCOME_CONTRADICTORY_EVIDENCE,
    RELATION_EXACT,
    RELATION_NORMALIZED_EQUAL,
)

# =============================================================================
# Contract
# =============================================================================

WORKBENCH_SCHEMA_VERSION = "1.0.0"
QUEUE_KIND = "READ_ONLY_NBA_HUMAN_REVIEW_QUEUE"
DOSSIER_KIND = "NBA_CANDIDATE_REVIEW_DOSSIER"
SUMMARY_KIND = "READ_ONLY_NBA_REVIEW_SUMMARY"
VALIDATION_KIND = "READ_ONLY_NBA_REVIEW_VALIDATION"
PERSIST_KIND = "OPERATOR_EVIDENCE_PERSISTENCE"
AUDIT_PERSISTED_KIND = "READ_ONLY_PERSISTED_EVIDENCE_RECONCILIATION"

#: 队列/候选侧的可持久证据状态。刻意**不**包含任何"已接受/已批准"语义。
PERSISTED_NO_STORED_EVIDENCE = "NO_STORED_EVIDENCE"
PERSISTED_STALE = "STORED_BUT_STALE"
PERSISTED_AMBIGUOUS = "STORED_BUT_AMBIGUOUS"
PERSISTED_CONFLICT = "STORED_WITH_CONFLICT"
PERSISTED_BOUND = "STORED_AND_CURRENTLY_BOUND"

#: 无论上面哪一种，人工复核动作始终未完成——持久化不等于批准。
ACTION_OPERATOR_REVIEW_REQUIRED = "OPERATOR_REVIEW_REQUIRED"
ACTION_REATTACH_REQUIRED = "REATTACH_BY_OPERATOR_REQUIRED"
ACTION_ADJUDICATE_REQUIRED = "ADJUDICATE_CONFLICT_REQUIRED"
ACTION_COLLECT_EVIDENCE = "COLLECT_OPERATOR_DECISION"

#: 绑定严重度排序：用于把"同一分片内多条记录"折叠成单一分片结论。
_BINDING_PRECEDENCE = (
    "BOUND_TO_CURRENT_CANDIDATE",
    "CANDIDATE_FINGERPRINT_CHANGED",
    "CANDIDATE_ID_CHANGED",
    "STALE_DATASET_FINGERPRINT",
    "CANONICAL_IDS_CHANGED",
    "CANDIDATE_NOT_IN_CURRENT_DATASET",
    "AMBIGUOUS_IDENTITY",
)
PREPARE_KIND = "READ_ONLY_NBA_REVIEW_PREPARE"
EVIDENCE_KIND = "NBA_INDEPENDENT_ORIENTATION_EVIDENCE"

PRIORITY_P0 = "P0"
PRIORITY_P1 = "P1"
PRIORITY_P2 = "P2"
PRIORITY_P3 = "P3"
PRIORITY_ORDER: dict[str, int] = {
    PRIORITY_P0: 0,
    PRIORITY_P1: 1,
    PRIORITY_P2: 2,
    PRIORITY_P3: 3,
}
PRIORITY_MEANING = {
    PRIORITY_P0: "Contradictory number/team evidence — resolve before anything else",
    PRIORITY_P1: "Exact reversed-name candidates with number correspondence",
    PRIORITY_P2: "Team-name variants / transliteration differences",
    PRIORITY_P3: "Other ambiguous or insufficient identity cases",
}

STATUS_SOURCE_REFERENCE_SUPPLIED = "SOURCE_REFERENCE_SUPPLIED"
STATUS_SOURCE_REFERENCE_REVIEWED = "SOURCE_REFERENCE_REVIEWED"
STATUS_INDEPENDENTLY_SUPPORTED = "ORIENTATION_INDEPENDENTLY_SUPPORTED"
STATUS_CONTRADICTED = "ORIENTATION_CONTRADICTED"
STATUS_UNVERIFIED = "ORIENTATION_UNVERIFIED"
#: 外部记录只与**某一侧** provider 队对一致：方向可参考，但跨源同一性未建立。
STATUS_SOURCE_SIDE_MATCHED = "SOURCE_SIDE_MATCHED"
#: 跨源同一性未建立的显式标记（用于 `cross_source_identity` 字段）。
STATUS_CROSS_SOURCE_UNVERIFIED = "CROSS_SOURCE_IDENTITY_UNVERIFIED"

#: 证据校验退出码（沿用既有 `EXIT_REVIEW_REJECTED = 4`，仅新增后两者）。
EXIT_EVIDENCE_REJECTED = 5
EXIT_MULTIPLE_REJECTIONS = 6
EVIDENCE_STATUS_VALUES = (
    STATUS_SOURCE_REFERENCE_SUPPLIED,
    STATUS_SOURCE_REFERENCE_REVIEWED,
)

EVIDENCE_REQUIRED_KEYS = frozenset(
    {
        "evidence_id",
        "candidate_id",
        "reference_identifier",
        "publisher",
        "source_reference",
        "retrieved_at",
        "actual_event_date",
        "away_team",
        "home_team",
        "event_identifier",
        "excerpt",
        "checked_by",
        "verification_status",
        "contradictory_references",
    }
)

#: Evidence payloads must never carry automation or mutation semantics.
EVIDENCE_PROHIBITED_KEYS = frozenset(
    {
        "approved",
        "approval",
        "authorization",
        "signature",
        "signed",
        "apply",
        "merge",
        "delete",
        "surviving_canonical_id",
        "model_generated",
        "generated_by_model",
        "auto",
        "automated",
    }
)

REVIEW_TEMPLATE_KIND = "NBA_HUMAN_REVIEW_TEMPLATE"

LIMITATIONS = (
    "Candidate priorities are review ordering only; they are never confidence-based approvals.",
    "A URL inside an evidence file is not proof that its content was verified.",
    "Only SOURCE_REFERENCE_REVIEWED evidence with matching date and teams can support orientation.",
    "No Canonical merge, deletion or repair is performed or proposed.",
    "Human decisions are review evidence, not signed production authorization.",
)


# =============================================================================
# Priority and queue entry
# =============================================================================

def review_priority(candidate: Mapping[str, Any]) -> str:
    """Deterministic review priority (ordering only, never an approval grade)."""
    if candidate.get("outcome") == OUTCOME_CONTRADICTORY_EVIDENCE:
        return PRIORITY_P0
    aligned = (candidate.get("aligned_home_relation"), candidate.get("aligned_away_relation"))
    exact = (RELATION_EXACT, RELATION_NORMALIZED_EQUAL)
    if (
        candidate.get("candidate_class") == CLASS_BOTH
        and candidate.get("orientation") == ORIENTATION_REVERSED
        and candidate.get("numbers_agree") is True
        and aligned[0] in exact
        and aligned[1] in exact
    ):
        return PRIORITY_P1
    if candidate.get("candidate_class") == CLASS_INSUFFICIENT:
        return PRIORITY_P2
    return PRIORITY_P3


def missing_required_evidence(candidate: Mapping[str, Any]) -> list[str]:
    """Explicitly list what a reviewer still lacks (missing stays missing)."""
    missing: list[str] = []
    if candidate.get("orientation") == ORIENTATION_REVERSED:
        missing.append("HOME_AWAY_ORIENTATION_UNRESOLVED")
    if "TEAM_NAME_EQUIVALENCE_UNVERIFIED" in candidate.get("blockers", []):
        missing.append("TEAM_NAME_EQUIVALENCE_UNVERIFIED")
    if candidate.get("numbers_comparable") is False:
        missing.append("NUMBER_CORRESPONDENCE_UNRESOLVED")
    for mid, kickoff in sorted(candidate.get("kickoff_times", {}).items()):
        if not kickoff.get("known"):
            missing.append(f"KICKOFF_TIME_UNKNOWN:{mid}")
    missing.append("INDEPENDENT_ORIENTATION_EVIDENCE_MISSING")
    return missing


def build_queue_entry(candidate: Mapping[str, Any], *, review_status: str, orientation: Mapping[str, Any]) -> dict[str, Any]:
    """Review-queue view of one candidate (existing ids and fingerprints preserved)."""
    ids = list(candidate["canonical_ids"])
    return {
        "workbench_schema_version": WORKBENCH_SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "candidate_fingerprint": candidate["candidate_fingerprint"],
        "dataset_fingerprint": candidate["dataset_fingerprint"],
        "priority": review_priority(candidate),
        "priority_meaning": PRIORITY_MEANING[review_priority(candidate)],
        "candidate_class": candidate["candidate_class"],
        "outcome": candidate["outcome"],
        "canonical_ids": ids,
        "sport": candidate["sport"],
        "event_date": candidate["event_date"],
        "competition": dict(candidate["competition"]),
        "league_labels": dict(candidate["competition"]),
        "original_number_strings": dict(candidate["competition_number_raw"]),
        "normalized_number_strings": dict(candidate["competition_number_normalized"]),
        "interpreted_number_evidence": list(candidate["number_evidence"]),
        "kickoff_metadata": dict(candidate["kickoff_times"]),
        "original_teams": dict(candidate["team_names"]),
        "orientation": candidate["orientation"],
        "numbers_comparable": candidate["numbers_comparable"],
        "numbers_agree": candidate["numbers_agree"],
        "home_relation": candidate["home_relation"],
        "away_relation": candidate["away_relation"],
        "aligned_home_relation": candidate["aligned_home_relation"],
        "aligned_away_relation": candidate["aligned_away_relation"],
        "possible_reversed_alignment": {
            "orientation": candidate["orientation"],
            "aligned_home_relation": candidate["aligned_home_relation"],
            "aligned_away_relation": candidate["aligned_away_relation"],
            "swap_applied": False,
            "note": "the workbench never swaps home/away or any price",
        },
        "supporting_evidence": list(candidate["supporting_evidence"]),
        "contradictory_evidence": list(candidate["contradictory_evidence"]),
        "blockers": list(candidate["blockers"]),
        "missing_required_evidence": missing_required_evidence(candidate),
        "historical_reference_counts": {
            stage: {
                mid: candidate[stage][mid]["count"] for mid in ids
            }
            for stage in (
                "prediction_references",
                "odds_references",
                "settlement_references",
                "evaluation_references",
            )
        },
        "lifecycle_status": dict(candidate["lifecycle_status"]),
        "final_scores": dict(candidate["final_scores"]),
        "human_review_status": review_status,
        "orientation_evidence_status": orientation.get("status", STATUS_UNVERIFIED),
        "cross_source_identity": orientation.get("cross_source_identity", STATUS_CROSS_SOURCE_UNVERIFIED),
        "orientation_matched_source_sides": list(orientation.get("matched_source_sides", [])),
        "orientation_unresolved_sides": list(orientation.get("unresolved_sides", [])),
        "orientation_supporting_evidence_ids": list(orientation.get("supporting_evidence_ids", [])),
        "orientation_contradictory_references": list(orientation.get("contradictory_references", [])),
        "orientation_fixture_identifiers": list(orientation.get("fixture_identifiers", [])),
        "orientation_evidence": dict(orientation),
        "authoritative": False,
        "is_approval": False,
        "authorizes_odds_merge": False,
        "authorizes_event_admission": False,
        "executable": False,
        "surviving_canonical_id": None,
    }


# =============================================================================
# Independent orientation evidence
# =============================================================================

def load_evidence(path: str | None) -> dict[str, Any]:
    """Strict reader for an operator-supplied orientation evidence file."""
    if not path:
        return {"schema_version": WORKBENCH_SCHEMA_VERSION, "evidence": [], "rejected": []}
    if not os.path.isfile(path):
        raise ReviewInputError(f"evidence file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"malformed evidence JSON ({exc})") from exc
    except OSError as exc:
        raise ReviewInputError(f"unreadable evidence file ({exc})") from exc
    if not isinstance(payload, Mapping):
        raise ReviewInputError("evidence file must contain a JSON object")
    raw_items = payload.get("evidence")
    if not isinstance(raw_items, list):
        raise ReviewInputError("evidence file must contain an 'evidence' list")

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            rejected.append({"index": index, "reason_code": "MALFORMED_EVIDENCE", "detail": "not an object"})
            continue
        record = dict(item)
        prohibited = sorted({key for key in record if key.lower() in EVIDENCE_PROHIBITED_KEYS})
        if prohibited:
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "PROHIBITED_EVIDENCE_FIELD",
                    "detail": ", ".join(prohibited),
                }
            )
            continue
        missing = sorted(EVIDENCE_REQUIRED_KEYS - set(record))
        if missing:
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "INCOMPLETE_EVIDENCE",
                    "detail": f"missing fields: {missing}",
                }
            )
            continue
        status = record.get("verification_status")
        if status not in EVIDENCE_STATUS_VALUES:
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "UNSUPPORTED_VERIFICATION_STATUS",
                    "detail": f"status must be one of {list(EVIDENCE_STATUS_VALUES)}",
                }
            )
            continue
        if status == STATUS_SOURCE_REFERENCE_REVIEWED:
            checked_by = str(record.get("checked_by") or "").strip()
            if not checked_by:
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "MISSING_CHECKED_BY",
                        "detail": "a reviewed evidence item requires a human 'checked_by' identity",
                    }
                )
                continue
            if checked_by.lower() in ("model", "bot", "auto", "automated", "model_generated", "ai", "proxy"):
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "AUTOMATED_REVIEWER_REJECTED",
                        "detail": "checked_by cannot be automated or model-assigned",
                    }
                )
                continue
            source_ref = str(record.get("source_reference") or "").strip()
            if not source_ref:
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "MISSING_SOURCE_REFERENCE",
                        "detail": "a reviewed evidence item requires an independent source reference",
                    }
                )
                continue
            retrieved_at = str(record.get("retrieved_at") or "").strip()
            if not retrieved_at:
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "MISSING_RETRIEVAL_TIMESTAMP",
                        "detail": "a reviewed evidence item requires a retrieval timestamp",
                    }
                )
                continue
            excerpt = str(record.get("excerpt") or "").strip()
            if not excerpt:
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "MISSING_INSPECTION_EXCERPT",
                        "detail": "a reviewed evidence item requires a factual inspection excerpt",
                    }
                )
                continue
        if not isinstance(record.get("contradictory_references"), list):
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "INVALID_CONTRADICTORY_REFERENCES",
                    "detail": "contradictory_references must be a list",
                }
            )
            continue
        correspondence = record.get("team_name_correspondence")
        if correspondence is not None:
            if not isinstance(correspondence, list) or not all(
                isinstance(entry, Mapping)
                and entry.get("provider_team")
                and entry.get("source_team")
                and entry.get("verification_status") in EVIDENCE_STATUS_VALUES
                for entry in correspondence
            ):
                rejected.append(
                    {
                        "index": index,
                        "evidence_id": record.get("evidence_id"),
                        "reason_code": "INVALID_TEAM_NAME_CORRESPONDENCE",
                        "detail": (
                            "team_name_correspondence must be a list of "
                            "{provider_team, source_team, verification_status} objects"
                        ),
                    }
                )
                continue
        accepted.append(record)
    return {
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "evidence": accepted,
        "rejected": rejected,
        "rejected_count": len(rejected),
    }


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if not ch.isspace()).lower()


def _side_name_sets(candidate: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Per-provider-side ``(home, away)`` name sets, kept separate on purpose."""
    sides: dict[str, tuple[str, str]] = {}
    for mid, teams in (candidate.get("team_names") or {}).items():
        home, away = _norm(teams.get("home")), _norm(teams.get("away"))
        if home and away and home != away:
            sides[str(mid)] = (home, away)
    return sides


def _evidence_pair(item: Mapping[str, Any]) -> tuple[str, str] | None:
    home, away = _norm(item.get("home_team")), _norm(item.get("away_team"))
    if not home or not away or home == away:
        return None
    return (home, away)


def _reviewed_correspondences(item: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """
    Reviewed name correspondences supplied **with** the evidence item.

    A transliteration alias becomes usable only when the operator files a
    ``SOURCE_REFERENCE_REVIEWED`` entry for it; nothing is inferred from spelling.
    """
    out: list[tuple[str, str, str]] = []
    for entry in item.get("team_name_correspondence") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("verification_status") != STATUS_SOURCE_REFERENCE_REVIEWED:
            continue
        provider = _norm(entry.get("provider_team"))
        source = _norm(entry.get("source_team"))
        if provider and source:
            out.append((provider, source, str(entry.get("evidence_reference") or "")))
    return out


def _maps_onto_side(
    side: tuple[str, str], pair: tuple[str, str], correspondences: Sequence[tuple[str, str, str]]
) -> bool:
    """Can the reviewed correspondences reconcile this provider side with the source pair?"""
    mapped = set(side)
    for provider, source, _reference in correspondences:
        if provider in mapped:
            mapped.discard(provider)
            mapped.add(source)
    return set(pair) == mapped


def resolve_orientation(
    candidate: Mapping[str, Any], evidence_document: Mapping[str, Any]
) -> dict[str, Any]:
    """
    Resolve orientation evidence for one candidate.

    Matching is **per provider side**: an external record that equals only one side's
    team pair is reported as ``SOURCE_SIDE_MATCHED`` and never promoted to a verified
    cross-source identity. Name variants need an operator-filed reviewed correspondence.

    A candidate is only ``ORIENTATION_INDEPENDENTLY_SUPPORTED`` when every provider side
    is covered, no reviewed item carries unresolved contradictions, the reviewed items
    agree on the home team, and their fixture identifiers do not clash.
    """
    items = [
        item
        for item in evidence_document.get("evidence", [])
        if item.get("candidate_id") == candidate["candidate_id"]
    ]
    supplied = [item for item in items if item["verification_status"] == STATUS_SOURCE_REFERENCE_SUPPLIED]
    reviewed = [item for item in items if item["verification_status"] == STATUS_SOURCE_REFERENCE_REVIEWED]
    sides = _side_name_sets(candidate)

    unmatched: list[str] = []
    matched: list[dict[str, Any]] = []
    covered_sides: dict[str, set[str]] = {}
    corroborated_ids: set[str] = set()

    def base(**kwargs: Any) -> dict[str, Any]:
        payload = {
            "supplied_count": len(supplied),
            "reviewed_count": len(reviewed),
            "unmatched": unmatched,
            "home_team_claims": {},
            "contradictory_references": [],
            "supporting_evidence_ids": [],
            "fixture_identifiers": [],
            "matched_source_sides": [],
            "unresolved_sides": sorted(sides),
            "corroborated_evidence_ids": [],
            "cross_source_identity": STATUS_CROSS_SOURCE_UNVERIFIED,
            "authoritative": False,
        }
        payload.update(kwargs)
        return payload

    for item in reviewed:
        if str(item.get("actual_event_date")) != str(candidate.get("event_date")):
            unmatched.append(f"{item['evidence_id']}: event date mismatch")
            continue
        pair = _evidence_pair(item)
        if pair is None:
            unmatched.append(f"{item['evidence_id']}: team names missing")
            continue
        correspondences = _reviewed_correspondences(item)
        exact = [mid for mid, side in sides.items() if set(side) == set(pair)]
        reconciled = [
            mid for mid, side in sides.items()
            if mid not in exact and _maps_onto_side(side, pair, correspondences)
        ]
        if not exact and not reconciled:
            unmatched.append(f"{item['evidence_id']}: team pair mismatch")
            continue
        matched.append(dict(item))
        covered_sides[str(item["evidence_id"])] = set(exact) | set(reconciled)
        if reconciled:
            corroborated_ids.add(str(item["evidence_id"]))

    if not matched:
        return base(
            status=STATUS_UNVERIFIED,
            reason=(
                "no reviewed external evidence for this candidate"
                if not reviewed
                else "supplied evidence was not reviewed, or does not match any provider side"
            ),
            matched_count=0,
        )

    claims: dict[str, list[str]] = defaultdict(list)
    for item in matched:
        claims[str(item["home_team"])].append(str(item["evidence_id"]))
    supporting_ids = sorted(item["evidence_id"] for item in matched)
    contradictions = sorted(
        {str(ref) for item in matched for ref in item.get("contradictory_references") or []}
    )
    fixture_ids = sorted({str(item.get("event_identifier")) for item in matched if item.get("event_identifier")})
    union_covered = set().union(*covered_sides.values()) if covered_sides else set()
    unresolved_sides = sorted(set(sides) - union_covered)
    all_sides_covered = bool(sides) and not unresolved_sides

    common = dict(
        matched_count=len(matched),
        home_team_claims={home: sorted(ids) for home, ids in sorted(claims.items())},
        contradictory_references=contradictions,
        supporting_evidence_ids=supporting_ids,
        fixture_identifiers=fixture_ids,
        matched_source_sides=sorted(union_covered),
        unresolved_sides=unresolved_sides,
        corroborated_evidence_ids=sorted(corroborated_ids),
    )

    if len(claims) > 1:
        return base(
            status=STATUS_CONTRADICTED,
            reason="reviewed external evidence disagrees about the home team",
            **common,
        )
    if contradictions:
        return base(
            status=STATUS_CONTRADICTED,
            reason="reviewed external evidence still carries unresolved contradictory references",
            **common,
        )
    if len(fixture_ids) > 1:
        return base(
            status=STATUS_CONTRADICTED,
            reason="reviewed external evidence reports competing fixture identifiers",
            **common,
        )
    if not all_sides_covered:
        return base(
            status=STATUS_SOURCE_SIDE_MATCHED,
            reason=(
                "external record matches only the listed provider side(s); cross-source "
                "identity is not established without reviewed name correspondence for the "
                "remaining side"
            ),
            **common,
        )

    home = next(iter(claims))
    return base(
        status=STATUS_INDEPENDENTLY_SUPPORTED,
        reason="reviewed external evidence covers every provider side with no contradiction",
        cross_source_identity=STATUS_INDEPENDENTLY_SUPPORTED,
        **{**common, "home_team_claims": {home: sorted(claims[home])}},
    )


# =============================================================================
# Queue construction
# =============================================================================

def _accepted_decisions(queue: Mapping[str, Any], reviews_document: Mapping[str, Any] | None) -> dict[str, str]:
    if not reviews_document:
        return {}
    validation = validate_reviews(queue, reviews_document)
    return {
        candidate_id: record["decision"]
        for candidate_id, record in validation.get("accepted", {}).items()
    }


def build_review_queue(
    dataset: Dataset,
    *,
    reviews_document: Mapping[str, Any] | None = None,
    evidence_document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Prioritized NBA review queue (deterministic).

    The queue document is review-compatible with the existing validator, so a decision
    file written against it can be validated without any translation layer.
    """
    candidates = [item for item in build_candidates(dataset) if item.get("nba_scope")]
    candidates.sort(
        key=lambda item: (
            PRIORITY_ORDER[review_priority(item)],
            str(item.get("event_date")),
            item["candidate_id"],
        )
    )
    probe = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": dataset_fingerprint(dataset),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    decisions = _accepted_decisions(probe, reviews_document)
    evidence = evidence_document or {"evidence": [], "rejected": []}

    entries = [
        build_queue_entry(
            candidate,
            review_status=decisions.get(candidate["candidate_id"], UNREVIEWED),
            orientation=resolve_orientation(candidate, evidence),
        )
        for candidate in candidates
    ]
    priority_counts = Counter(entry["priority"] for entry in entries)
    return {
        "kind": QUEUE_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "workbench_schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": probe["dataset_fingerprint"],
        "candidate_count": len(entries),
        "priority_counts": dict(sorted(priority_counts.items())),
        "priority_meaning": dict(PRIORITY_MEANING),
        "reviewed_count": sum(1 for entry in entries if entry["human_review_status"] != UNREVIEWED),
        "pending_count": sum(1 for entry in entries if entry["human_review_status"] == UNREVIEWED),
        "evidence_rejected_count": len(evidence.get("rejected", [])),
        "evidence_rejected": list(evidence.get("rejected", [])),
        "entries": entries,
        "candidates": candidates,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
        "limitations": list(LIMITATIONS),
    }


def queue_for_reviews(queue: Mapping[str, Any]) -> dict[str, Any]:
    """Project the workbench queue into the existing review-queue dialect."""
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "candidate_count": queue["candidate_count"],
        "candidates": list(queue["candidates"]),
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
    }


# =============================================================================
# Unique historical impact
# =============================================================================

def unique_impact(dataset: Dataset, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """
    Reference impact with **per-pair sums separated from unique record counts**.

    A Canonical record may appear in several candidate pairs, so the sum of per-candidate
    counts is not the number of affected historical records.
    """
    references = _reference_index(dataset)
    stages = ("prediction", "odds", "settlement", "evaluation")
    per_candidate: dict[str, dict[str, int]] = {}
    totals = Counter()
    provenance: dict[str, dict[str, set[str]]] = {stage: {} for stage in stages}
    canonical_ids: set[str] = set()

    for candidate in candidates:
        candidate_id = candidate["candidate_id"]
        counts: dict[str, int] = {}
        for stage in stages:
            count = 0
            for mid in candidate["canonical_ids"]:
                for reference_id in references.get(mid, {}).get(stage, []):
                    provenance[stage].setdefault(reference_id, set()).add(candidate_id)
                    count += 1
                canonical_ids.add(mid)
            counts[stage] = count
            totals[stage] += count
        per_candidate[candidate_id] = counts

    distinct = {stage: sorted(provenance[stage]) for stage in stages}
    repeated = {
        stage: {
            reference_id: sorted(candidate_ids_for_ref)
            for reference_id, candidate_ids_for_ref in sorted(provenance[stage].items())
            if len(candidate_ids_for_ref) > 1
        }
        for stage in stages
    }
    return {
        "per_candidate_reference_totals": per_candidate,
        "summed_reference_totals": dict(sorted(totals.items())),
        "distinct_affected_canonical_ids": sorted(canonical_ids),
        "distinct_record_counts": {stage: len(distinct[stage]) for stage in stages},
        "distinct_record_ids": {stage: _bounded(distinct[stage]) for stage in stages},
        "repeated_reference_counts": {
            stage: len(repeated[stage]) for stage in stages
        },
        "repeated_reference_provenance": {
            stage: {ref: ids for ref, ids in list(repeated[stage].items())[:10]}
            for stage in stages
        },
        "counts_are_summed_not_unique": True,
        "historical_ids_immutable": True,
        "blockers_to_reconciliation": list(GOVERNANCE_EVIDENCE_REQUIRED)
        + ["PRODUCTION_WRITE_NOT_AUTHORIZED"],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
    }


# =============================================================================
# Dossier rendering
# =============================================================================

def render_dossier(queue: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    """Human-readable dossier answering the fifteen review questions in order."""
    candidate = next(item for item in queue["candidates"] if item["candidate_id"] == entry["candidate_id"])
    ids = list(entry["canonical_ids"])
    first, second = ids[0], ids[-1]
    teams = entry["original_teams"]
    numbers = entry["original_number_strings"]
    interpretations = {item["raw"]: item for item in entry["interpreted_number_evidence"]}

    def teams_line(mid: str) -> str:
        return f'{teams[mid].get("home")} (home) vs {teams[mid].get("away")} (away)'

    lines = [
        f"# Review dossier — `{entry['candidate_id']}`",
        "",
        f"- priority: **{entry['priority']}** — {entry['priority_meaning']}",
        f"- diagnostic class: `{entry['candidate_class']}` / outcome `{entry['outcome']}`",
        f"- dataset fingerprint: `{entry['dataset_fingerprint']}`",
        f"- candidate fingerprint: `{entry['candidate_fingerprint']}`",
        f"- event date: {entry['event_date']}",
        f"- review status: **{entry['human_review_status']}**",
        f"- orientation evidence: `{entry['orientation_evidence_status']}`",
        "",
        "## 1. Which two records are suspected of the same event?",
        f"- `{first}`",
        f"- `{second}`",
        "",
        "## 2. Why were they selected?",
        f"- class `{entry['candidate_class']}`",
    ]
    for item in entry["supporting_evidence"]:
        lines.append(f"- supporting: {item}")
    for item in entry["contradictory_evidence"]:
        lines.append(f"- contradictory: {item}")
    if entry["blockers"]:
        lines.append(f"- blockers: {', '.join(entry['blockers'])}")

    lines += [
        "",
        "## 3. Original competition number on each side",
    ]
    for mid in ids:
        lines.append(f"- `{mid}`: raw `{numbers[mid]}` (never rewritten)")
    lines += [
        "",
        "## 4. How were the numbers interpreted?",
    ]
    for item in entry["interpreted_number_evidence"]:
        lines.append(
            f"- `{item.get('raw')}` → method `{item.get('method')}`, encoded "
            f"`{item.get('interpreted_encoded')}`, confidence `{item.get('confidence')}`, "
            f"weekday `{item.get('weekday_prefix') or item.get('interpreted_weekday_prefix')}`"
        )
    lines += [
        "",
        "## 5. Does source evidence independently support number correspondence?",
        f"- numbers comparable: `{entry.get('numbers_comparable')}` / agree: `{entry.get('numbers_agree')}`",
        "- correlation is observed, not a provider contract; confidence labels are on each item",
        "",
        "## 6. Are home and away reversed?",
        f"- orientation: `{entry['orientation']}`",
        f"- aligned home relation: `{entry['aligned_home_relation']}`, aligned away relation: `{entry['aligned_away_relation']}`",
        "",
        "## 7. Which side claims the actual home team?",
    ]
    for mid in ids:
        lines.append(f"- `{mid}`: {teams_line(mid)}")

    evidence = entry["orientation_evidence"]
    lines += [
        "",
        "## 8. Is that claim independently supported?",
        f"- status: `{evidence.get('status')}`",
        f"- {evidence.get('reason', '')}",
    ]
    for item in sorted(evidence.get("unmatched", [])):
        lines.append(f"- unmatched evidence: {item}")
    for home, evidence_ids in sorted(evidence.get("home_team_claims", {}).items()):
        lines.append(f"- reviewed claim: home = {home} (from {', '.join(evidence_ids)})")

    weekday_rows = [
        (item.get("raw"), item.get("weekday_matches_event_date"))
        for item in entry["interpreted_number_evidence"]
    ]
    lines += [
        "",
        "## 9. Is the date the event date or the lottery sales date?",
    ]
    for raw, matches in weekday_rows:
        lines.append(
            f"- `{raw}`: number weekday matches event date = `{matches}` "
            "(a mismatch is normal when sales day precedes the match day)"
        )
    lines += [
        "",
        "## 10. Are the participating teams independently confirmed?",
    ]
    for mid in ids:
        lines.append(f"- `{mid}`: {teams_line(mid)}")
    if "TEAM_NAME_EQUIVALENCE_UNVERIFIED" in entry["blockers"]:
        lines.append("- **not established**: at least one team name is a variant/abbreviation")

    lines += [
        "",
        "## 11. Could another event produce the same apparent identity?",
    ]
    if entry["outcome"] == OUTCOME_CONTRADICTORY_EVIDENCE:
        lines.append(
            "- yes: number forms agree while team names are mutually exclusive — "
            "a transliteration variant and a real number collision are indistinguishable here"
        )
    else:
        lines.append(
            "- yes in principle: same date + same number form can collide across venues; "
            "see the negative controls in the scan output"
        )

    lines += [
        "",
        "## 12. What contradicts the candidate equivalence?",
    ]
    contradictions = list(entry["contradictory_evidence"])
    if not contradictions:
        lines.append("- none recorded beyond the listed blockers")
    for item in contradictions:
        lines.append(f"- {item}")
    if evidence.get("status") == "ORIENTATION_CONTRADICTED":
        lines.append("- reviewed external evidence disagrees about the home team")

    lines += [
        "",
        "## 13. Which historical records may a future repair affect?",
        "| stage | " + " | ".join(f"`{mid}`" for mid in ids) + " |",
        "| --- | " + " | ".join("---" for _ in ids) + " |",
    ]
    for stage, counts in entry["historical_reference_counts"].items():
        lines.append(f"| {stage} | " + " | ".join(str(counts[mid]) for mid in ids) + " |")
    lines.append("- repeated references across candidates are counted per pair, not per record")

    lines += [
        "",
        "## 14. What is still missing?",
    ]
    for item in entry["missing_required_evidence"]:
        lines.append(f"- {item}")

    lines += [
        "",
        "## 15. What can the reviewer safely decide?",
        f"- one of: {', '.join(DECIDED_VALUES)}",
        "- no decision is a mandate to merge Canonical records; that requires signed governance",
        "- the workbench never swaps home/away, never swaps prices, never selects a survivor",
        "",
        "## Original records (verbatim)",
        "",
        f"```json\n{json.dumps([candidate['team_names'], candidate['competition'], candidate['competition_number_raw'], candidate['kickoff_times'], candidate['lifecycle_status']], ensure_ascii=False, indent=2, sort_keys=True)}\n```",
        "",
    ]
    return "\n".join(lines)


def review_template(queue: Mapping[str, Any]) -> dict[str, Any]:
    """Blank review template bound to this exact dataset (no prepopulated decision)."""
    return {
        "kind": REVIEW_TEMPLATE_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "instructions": [
            "Copy one skeleton per candidate you actually reviewed.",
            "Fill decision with one of: " + ", ".join(DECIDED_VALUES),
            "label_provenance MUST be 'HUMAN_REVIEWED'; automated or model labels are rejected.",
            "Never add approval, signature, merge, delete or surviving_canonical_id fields.",
            "A decision is review evidence; it does not authorize any production change.",
            "Keep candidate_fingerprint unchanged; a changed dataset invalidates it.",
        ],
        "allowed_decisions": list(DECIDED_VALUES),
        "forbidden_keys": sorted(PROHIBITED_REVIEW_KEYS),
        "required_keys": sorted(REVIEW_RECORD_KEYS),
        "candidates": [
            {
                "candidate_id": entry["candidate_id"],
                "priority": entry["priority"],
                "skeleton": {
                    "candidate_id": entry["candidate_id"],
                    "candidate_fingerprint": entry["candidate_fingerprint"],
                    "dataset_fingerprint": entry["dataset_fingerprint"],
                    "canonical_ids": list(entry["canonical_ids"]),
                    "decision": None,
                    "label_provenance": PROVENANCE_HUMAN,
                    "reviewer": "",
                    "decided_at": "",
                    "evidence_reference": "",
                    "notes": "",
                    "schema_version": REVIEW_SCHEMA_VERSION,
                },
            }
            for entry in queue["entries"]
        ],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
    }


def orientation_evidence_template(queue: Mapping[str, Any]) -> dict[str, Any]:
    """
    Blank independent-orientation evidence template (operator fills, nobody fetches).

    The example deliberately demonstrates an **unverified** record: ``checked_by`` is
    empty and ``verification_status`` is ``SOURCE_REFERENCE_SUPPLIED``. Promoting a
    record to ``SOURCE_REFERENCE_REVIEWED`` is an operator action taken **after** the
    source content was actually inspected by a human.
    """
    return {
        "kind": EVIDENCE_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "instructions": [
            "Add one object per independently checked event record (box score, fixture page, document).",
            "verification_status must be SOURCE_REFERENCE_SUPPLIED or SOURCE_REFERENCE_REVIEWED.",
            "Start at SOURCE_REFERENCE_SUPPLIED. Only after a human actually opened and checked the",
            "source content may that human change the status to SOURCE_REFERENCE_REVIEWED and fill checked_by.",
            "Only SOURCE_REFERENCE_REVIEWED items covering every provider side can support orientation.",
            "A team-spelling variant needs its own reviewed team_name_correspondence entry.",
            "A URL alone is never proof; automation or approval fields are rejected.",
            "The workbench never fetches the network.",
        ],
        "required_keys": sorted(EVIDENCE_REQUIRED_KEYS),
        "optional_keys": ["team_name_correspondence"],
        "allowed_statuses": list(EVIDENCE_STATUS_VALUES),
        "status_promotion": {
            "from": STATUS_SOURCE_REFERENCE_SUPPLIED,
            "to": STATUS_SOURCE_REFERENCE_REVIEWED,
            "who": "a human operator, after inspecting the source content",
            "not_allowed": [
                "automation or a model setting REVIEWED on a human's behalf",
                "promotion without filling checked_by",
                "promotion while contradictory_references is non-empty",
            ],
        },
        "team_name_correspondence_example": [
            {
                "provider_team": "provider spelling",
                "source_team": "source spelling",
                "verification_status": STATUS_SOURCE_REFERENCE_SUPPLIED,
                "evidence_reference": "where the equivalence was read",
            }
        ],
        "example_allows_illegal_status": False,
        "example": {
            "evidence_id": "EV-EXAMPLE-NOT-REVIEWED",
            "candidate_id": queue["entries"][0]["candidate_id"] if queue["entries"] else "NXD-…",
            "reference_identifier": "operator reference",
            "publisher": "publisher",
            "source_reference": "https://example.invalid/boxscore or document path",
            "retrieved_at": "2030-05-02T00:00:00Z",
            "actual_event_date": "2030-05-01",
            "away_team": "away team as printed by the source",
            "home_team": "home team as printed by the source",
            "event_identifier": "provider fixture id",
            "excerpt": "verbatim excerpt",
            "checked_by": "",
            "verification_status": STATUS_SOURCE_REFERENCE_SUPPLIED,
            "contradictory_references": [],
        },
        "example_note": (
            "The example above is a blank, unreviewed placeholder. It is not human-verified "
            "and must not be copied with REVIEWED status."
        ),
        "evidence": [],
    }


# =============================================================================
# Commands
# =============================================================================

def _load_dataset_or_error(data_dir: str | None) -> Dataset:
    return load_dataset(data_dir)


def command_prepare(data_dir: str | None, output_dir: str | None, *, reviews_path: str | None = None,
                    evidence_path: str | None = None) -> dict[str, Any]:
    """Write the review package (queue, dossiers, templates, instructions)."""
    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return {
            "kind": PREPARE_KIND,
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "real_data_available": False,
            "status": "NOT_AVAILABLE",
            "reason": str(exc),
            "dossiers_written": 0,
            "executable": False,
            "apply_available": False,
            "production_write_authorization": False,
            "surviving_canonical_id": None,
            "decision": "NO-GO",
        }

    reviews_document = load_reviews(reviews_path) if reviews_path else None
    evidence_document = load_evidence(evidence_path)
    queue = build_review_queue(dataset, reviews_document=reviews_document, evidence_document=evidence_document)
    impact = unique_impact(dataset, queue["candidates"])

    document = {
        "kind": PREPARE_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "real_data_available": True,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "candidate_count": queue["candidate_count"],
        "priority_counts": queue["priority_counts"],
        "reviewed_count": queue["reviewed_count"],
        "pending_count": queue["pending_count"],
        "unique_impact": impact,
        "dossiers_written": 0,
        "queue": queue,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
    }

    if output_dir:
        validate_output_path(os.path.join(output_dir, "queue.json"))
        os.makedirs(output_dir, exist_ok=True)
        write_json(os.path.join(output_dir, "queue.json"), queue)
        write_json(os.path.join(output_dir, "impact.json"), impact)
        write_json(os.path.join(output_dir, "review-template.json"), review_template(queue))
        write_json(
            os.path.join(output_dir, "orientation-evidence-template.json"),
            orientation_evidence_template(queue),
        )
        dossier_dir = os.path.join(output_dir, "dossiers")
        os.makedirs(dossier_dir, exist_ok=True)
        for entry in queue["entries"]:
            write_text(
                os.path.join(dossier_dir, f"{entry['candidate_id']}.md"),
                render_dossier(queue, entry),
            )
            document["dossiers_written"] += 1
        write_text(os.path.join(output_dir, "README.md"), render_readme(queue, impact))
    return document


def render_readme(queue: Mapping[str, Any], impact: Mapping[str, Any]) -> str:
    lines = [
        "# NBA 复核工作台 — 使用说明",
        "",
        f"- 数据集指纹：`{queue['dataset_fingerprint']}`",
        f"- NBA 候选：**{queue['candidate_count']}**（已复核 {queue['reviewed_count']}，待复核 {queue['pending_count']}）",
        "",
        "## 文件",
        "",
        "| 文件 | 用途 |",
        "| --- | --- |",
        "| `queue.json` | 优先队列（含证据、历史引用计数、复核状态） |",
        "| `impact.json` | 逐对计数与**去重后**历史记录计数 |",
        "| `dossiers/<id>.md` | 每个候选的 15 问复核卷宗 |",
        "| `review-template.json` | 人工决定模板（决定字段留空） |",
        "| `orientation-evidence-template.json` | 独立主客证据模板（需人工填写） |",
        "",
        "## 导入流程",
        "",
        "1. 打开 `review-template.json`，为**真正复核过**的候选人复制一份 skeleton。",
        "2. `decision` 填 `SAME_EVENT` / `DIFFERENT_EVENT` / `INSUFFICIENT_EVIDENCE` / `REQUIRES_FURTHER_REVIEW`。",
        "3. `label_provenance` 必须是 `HUMAN_REVIEWED`；自动化/模型标签会被拒绝。",
        "4. 不要添加 `approved` / `signature` / `merge` / `surviving_canonical_id` 等字段。",
        "5. 把记录汇总成 `{\"schema_version\": \"1.0.0\", \"dataset_fingerprint\": …, \"reviews\": [...]}`。",
        "6. 运行 `python -m tools.nba_human_review_workbench validate --reviews <file>`。",
        "",
        "## 去重后的历史影响",
        "",
        "| 阶段 | 逐对合计 | 去重计数 |",
        "| --- | --- | --- |",
    ]
    for stage in ("prediction", "odds", "settlement", "evaluation"):
        lines.append(
            f"| {stage} | {impact['summed_reference_totals'].get(stage, 0)} | "
            f"{impact['distinct_record_counts'].get(stage, 0)} |"
        )
    lines += [
        "",
        "> 逐对合计会重复统计同一历史记录；去重计数才是受影响记录数。",
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "surviving_canonical_id = null",
        "```",
        "",
    ]
    return "\n".join(lines)


def command_show(data_dir: str | None, candidate_id: str, *, evidence_path: str | None = None) -> dict[str, Any]:
    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return {
            "kind": DOSSIER_KIND,
            "real_data_available": False,
            "status": "NOT_AVAILABLE",
            "reason": str(exc),
            "found": False,
            "candidate": None,
            "dossier": None,
            "decision": "NO-GO",
        }
    evidence_document = load_evidence(evidence_path)
    queue = build_review_queue(dataset, evidence_document=evidence_document)
    entry = next((item for item in queue["entries"] if item["candidate_id"] == candidate_id), None)
    return {
        "kind": DOSSIER_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "candidate_id": candidate_id,
        "found": entry is not None,
        "candidate": entry,
        "dossier": render_dossier(queue, entry) if entry else None,
        "candidate_ids": [item["candidate_id"] for item in queue["entries"]],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "decision": "NO-GO",
    }


def _validation_status(review_rejected: int, evidence_rejected: int) -> str:
    """Validation outcome covering **both** rejection categories."""
    if review_rejected and evidence_rejected:
        return "MULTIPLE_REJECTIONS"
    if review_rejected:
        return "REVIEW_REJECTED"
    if evidence_rejected:
        return "EVIDENCE_REJECTED"
    return "SUCCESS"


def _validation_exit_code(review_rejected: int, evidence_rejected: int) -> int:
    if review_rejected and evidence_rejected:
        return EXIT_MULTIPLE_REJECTIONS
    if review_rejected:
        return EXIT_REVIEW_REJECTED
    if evidence_rejected:
        return EXIT_EVIDENCE_REJECTED
    return EXIT_OK


def command_validate(data_dir: str | None, reviews_path: str | None, *, evidence_path: str | None = None) -> dict[str, Any]:
    """
    Validate an imported review file and optional evidence against the current NBA queue.

    Rejection count contract:
    - ``rejected_count``: legacy alias for ``review_rejected_count``, returning the count of
      rejected review decisions from ``validate_reviews()`` for backward compatibility.
    - ``review_rejected_count``: explicit count of rejected review records.
    - ``evidence_rejected_count``: explicit count of rejected orientation evidence items.
    - ``total_rejected_count``: sum of ``review_rejected_count + evidence_rejected_count``.
    """
    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return {
            "kind": VALIDATION_KIND,
            "real_data_available": False,
            "status": "NOT_AVAILABLE",
            "reason": str(exc),
            "executable": False,
            "decision": "NO-GO",
        }
    evidence_document = load_evidence(evidence_path)
    queue = build_review_queue(dataset, evidence_document=evidence_document)
    reviews_document = load_reviews(reviews_path) if reviews_path else {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "reviews": [],
    }
    validation = validate_reviews(queue_for_reviews(queue), reviews_document)
    statuses = Counter(
        entry["orientation_evidence_status"] for entry in queue["entries"]
    )
    return {
        "kind": VALIDATION_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "real_data_available": True,
        "candidate_count": validation["candidate_count"],
        "reviewed_count": validation["reviewed_count"],
        "pending_count": validation["pending_count"],
        "rejected_count": validation["rejected_count"],
        "review_rejected_count": validation["rejected_count"],
        "evidence_rejected_count": len(evidence_document.get("rejected", [])),
        "total_rejected_count": validation["rejected_count"] + len(evidence_document.get("rejected", [])),
        "validation_status": _validation_status(
            validation["rejected_count"], len(evidence_document.get("rejected", []))
        ),
        "rejected": validation["rejected"],
        "accepted": validation["accepted"],
        "decisions_by_class": validation["decisions_by_class"],
        "label_status": validation["label_status"],
        "label_provenance": validation["label_provenance"],
        "precision": None,
        "recall": None,
        "accuracy_claim": validation["accuracy_claim"],
        "evidence_rejected": list(evidence_document.get("rejected", [])),
        "evidence_rejected_count": len(evidence_document.get("rejected", [])),
        "orientation_status_counts": dict(sorted(statuses.items())),
        "blockers_to_reconciliation": list(GOVERNANCE_EVIDENCE_REQUIRED)
        + ["PRODUCTION_WRITE_NOT_AUTHORIZED"],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
    }


def command_persist(
    data_dir: str | None,
    reviews_path: str | None,
    *,
    evidence_path: str | None = None,
    evidence_dir: str | None = None,
    confirm: bool = False,
    source_references: Sequence[str] = (),
) -> dict[str, Any]:
    """
    Persist **already operator-authored** review decisions into the durable store.

    This is the only write path. It never creates a reviewer, a decision, a
    timestamp or a source-verification claim; it only copies what the operator
    wrote, after the existing validator accepted it against the current queue.

    ``confirm`` must be supplied explicitly: nothing is written as a side effect
    of ``validate`` or ``summary``.
    """
    from utils import evidence_store

    base = {
        "kind": PERSIST_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "decision": "NO-GO",
    }

    if not confirm:
        return dict(base, status="EXPLICIT_CONFIRMATION_REQUIRED",
                    reason="pass --confirm to persist operator-authored decisions")
    if not reviews_path:
        return dict(base, status="REVIEW_FILE_REQUIRED")

    validation = command_validate(data_dir, reviews_path, evidence_path=evidence_path)
    if not validation.get("real_data_available"):
        return dict(base, status="DATASET_NOT_AVAILABLE",
                    reason=validation.get("reason"))
    if validation["validation_status"] != "SUCCESS":
        return dict(
            base,
            status="REJECTED",
            validation_status=validation["validation_status"],
            review_rejected_count=validation["review_rejected_count"],
            evidence_rejected_count=validation["evidence_rejected_count"],
            rejected=validation["rejected"],
            persisted_count=0,
        )

    accepted = list((validation.get("accepted") or {}).values())
    if not accepted:
        return dict(base, status="NOTHING_TO_PERSIST", persisted_count=0)

    persisted: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for record in accepted:
        try:
            result = evidence_store.append_record(record, configured_dir=evidence_dir)
            persisted.append(
                {
                    "candidate_id": record.get("candidate_id"),
                    "pair_key": result["pair_key"],
                    "outcome": result["status"],
                    "record_count": result["record_count"],
                    "conflict": result.get("conflict", False),
                }
            )
        except evidence_store.EvidenceStoreError as exc:
            errors.append(
                {
                    "candidate_id": record.get("candidate_id"),
                    "reason_code": exc.code,
                }
            )

    retained: list[dict[str, Any]] = []
    for path in source_references:
        try:
            manifest = evidence_store.retain_source_copy(path, configured_dir=evidence_dir)
            retained.append({"sha256": manifest["sha256"], "size": manifest["size"]})
        except evidence_store.EvidenceStoreError as exc:
            errors.append({"source_reference": path, "reason_code": exc.code})

    # 校验 → 追加之间的新鲜度边界：数据集可能在此期间被刷新。
    # 这里只做**检测**，不改写已追加的历史记录，也不声称批次原子性。
    validated_fingerprint = validation.get("dataset_fingerprint")
    freshness_state = "NOT_RECHECKED"
    freshness_verified = False
    post_fingerprint = None
    try:
        post_dataset = _load_dataset_or_error(data_dir)
        post_fingerprint = dataset_fingerprint(post_dataset)
        if not validated_fingerprint:
            # 校验结果没有携带指纹 -> 无法比较，**不得**声称新鲜，也不得谎报变更。
            freshness_state = "VALIDATION_FINGERPRINT_MISSING"
        elif post_fingerprint == validated_fingerprint:
            freshness_state = "UNCHANGED"
            freshness_verified = True
        else:
            freshness_state = "DATASET_CHANGED_DURING_PERSIST"
    except AuditInputError:
        freshness_state = "RECHECK_UNAVAILABLE"

    status = "PERSISTED" if persisted and not errors else ("PARTIAL" if persisted else "FAILED")
    if status == "PERSISTED" and freshness_state == "DATASET_CHANGED_DURING_PERSIST":
        # 记录已落盘但已绑定到过期数据集：不得声称"当前有效"。
        status = "PARTIAL"

    return dict(
        base,
        status=status,
        staging_dir_description="EXTERNAL_EVIDENCE_DIRECTORY",
        persisted_count=len(persisted),
        persisted=persisted,
        retained_source_count=len(retained),
        retained_sources=retained,
        error_count=len(errors),
        errors=errors,
        dataset_fingerprint_at_validation=validated_fingerprint,
        dataset_fingerprint_after_persist=post_fingerprint,
        freshness_state=freshness_state,
        freshness_verified=freshness_verified,
        batch_atomic=False,
        batch_atomic_note=(
            "records are appended one at a time under a per-store writer lock; "
            "a multi-record operation is not atomic and partial results are reported above"
        ),
        # 明确：持久化 ≠ 生产授权，也 ≠ 已核验来源。
        review_evidence_only=True,
        production_authorized=False,
    )


def _persisted_state_for(binding: str, *, conflict: bool) -> str:
    if conflict:
        return PERSISTED_CONFLICT
    if binding == "AMBIGUOUS_IDENTITY":
        return PERSISTED_AMBIGUOUS
    if binding == "BOUND_TO_CURRENT_CANDIDATE":
        return PERSISTED_BOUND
    return PERSISTED_STALE


def _action_for(state: str) -> str:
    if state == PERSISTED_CONFLICT:
        return ACTION_ADJUDICATE_REQUIRED
    if state in (PERSISTED_AMBIGUOUS, PERSISTED_STALE):
        return ACTION_REATTACH_REQUIRED
    if state == PERSISTED_NO_STORED_EVIDENCE:
        return ACTION_COLLECT_EVIDENCE
    return ACTION_OPERATOR_REVIEW_REQUIRED


def _worst_binding(bindings: Sequence[str]) -> str:
    """Collapse per-record bindings into one deterministic shard-level verdict."""
    if not bindings:
        return "CANDIDATE_NOT_IN_CURRENT_DATASET"
    ranked = sorted(bindings, key=lambda item: _BINDING_PRECEDENCE.index(item))
    return ranked[0]


def command_audit_persisted(
    data_dir: str | None,
    *,
    evidence_dir: str | None = None,
) -> dict[str, Any]:
    """
    Read-only reconciliation of **durably persisted** human-review evidence
    against the **current** NBA candidate dataset.

    Hard guarantees
    ---------------
    - Never writes the seven protected stores, never writes the evidence store.
    - Never reattaches, rewrites or deletes a stored decision.
    - Never treats a matching ``pair_key`` as a fresh approval: identity is a
      lookup address, not an authorization.
    - Corrupt shards are reported, never repaired.
    - A missing evidence directory is reported as absent, never created.
    """
    from utils import evidence_store

    base = {
        "kind": AUDIT_PERSISTED_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "production_authorized": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
        "limitations": list(LIMITATIONS)
        + [
            "A stored decision is history, not a current approval.",
            "bound_record_count is not a count of independently reverified human decisions.",
            "Evidence persistence is per-record; a multi-record operation is not atomic.",
        ],
    }

    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return dict(base, real_data_available=False, status="NOT_AVAILABLE", reason=str(exc))

    current_fingerprint = dataset_fingerprint(dataset)

    try:
        storage = evidence_store.describe_storage(evidence_dir)
    except evidence_store.EvidenceStoreError as exc:
        return dict(
            base,
            real_data_available=True,
            dataset_fingerprint=current_fingerprint,
            status="FAIL_CLOSED",
            evidence_store_status="PATH_REJECTED",
            reason_code=exc.code,
        )

    candidates = [item for item in build_candidates(dataset) if item.get("nba_scope")]
    by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_pairs: dict[str, str] = {}
    for candidate in candidates:
        key = evidence_store.pair_key(candidate["canonical_ids"])
        by_pair[key].append(candidate)
        candidate_pairs[candidate["candidate_id"]] = key

    pair_reports: list[dict[str, Any]] = []
    corrupt_shard_count = 0
    stored_record_count = 0

    if storage["exists"]:
        for summary in evidence_store.list_pairs(evidence_dir):
            key = summary["pair_key"]
            if summary["status"] != "OK":
                corrupt_shard_count += 1
                pair_reports.append(
                    {
                        "pair_key": key,
                        "status": "CORRUPT",
                        "reason_code": summary.get("code"),
                        "record_count": 0,
                        "current_binding": None,
                        "persisted_state": "CORRUPT",
                        "required_next_action": "REPORT_AND_INVESTIGATE",
                    }
                )
                continue

            payload = evidence_store.load_pair(storage["directory"], key)
            records = list(payload.get("records") or [])
            stored_record_count += len(records)
            decision = evidence_store.decision_summary(payload)

            sharing = by_pair.get(key, [])
            bindings: list[str] = []
            if sharing:
                target = sharing[0]
                for record in records:
                    result = evidence_store.inspect_record(
                        record,
                        current_canonical_ids=target["canonical_ids"],
                        current_candidate_id=target["candidate_id"],
                        current_dataset_fingerprint=current_fingerprint,
                        candidates_sharing_pair=len(sharing),
                        current_candidate_fingerprint=target.get("candidate_fingerprint"),
                    )
                    bindings.append(result["binding"])
            binding = _worst_binding(bindings)
            state = _persisted_state_for(binding, conflict=bool(decision["conflict"]))

            pair_reports.append(
                {
                    "pair_key": key,
                    "status": "OK",
                    "canonical_ids": list(payload.get("canonical_ids") or []),
                    "record_count": len(records),
                    "current_candidate_count": len(sharing),
                    "current_binding": binding,
                    "conflict": bool(decision["conflict"]),
                    "decision_count": len(decision["decisions"]),
                    "persisted_state": state,
                    "required_next_action": _action_for(state),
                    "current_candidate_id": sharing[0]["candidate_id"] if sharing else None,
                }
            )

    stored_pairs = [item for item in pair_reports if item["status"] == "OK"]
    stored_pair_count = len(stored_pairs)

    bound_record_count = 0
    stale_record_count = 0
    ambiguous_pair_count = 0
    conflicting_pair_count = 0
    missing_candidate_count = 0
    for item in stored_pairs:
        binding = item["current_binding"]
        if binding == "BOUND_TO_CURRENT_CANDIDATE":
            bound_record_count += item["record_count"]
        elif binding == "AMBIGUOUS_IDENTITY":
            ambiguous_pair_count += 1
        elif binding == "CANDIDATE_NOT_IN_CURRENT_DATASET":
            missing_candidate_count += 1
        else:
            stale_record_count += item["record_count"]
        if item["conflict"]:
            conflicting_pair_count += 1

    # 需要**额外**处置（重绑定/裁决/收集）的条目数；
    # 单单"仍待人工复核"不算额外动作——那本来就是这个工作台的常态。
    operator_action_required_count = sum(
        1
        for item in pair_reports
        if item["persisted_state"] in (PERSISTED_STALE, PERSISTED_AMBIGUOUS, PERSISTED_CONFLICT)
        or item["status"] == "CORRUPT"
    )

    candidate_views = []
    for candidate in candidates:
        key = candidate_pairs[candidate["candidate_id"]]
        item = next((row for row in pair_reports if row["pair_key"] == key), None)
        state = item["persisted_state"] if item else PERSISTED_NO_STORED_EVIDENCE
        candidate_views.append(
            {
                "candidate_id": candidate["candidate_id"],
                "pair_key": key,
                "persisted_evidence_state": state,
                "operator_review_required": True,
                "required_next_action": _action_for(state),
                "record_count": item["record_count"] if item else 0,
            }
        )

    if not storage["exists"]:
        store_status = "NOT_PROVISIONED"
    elif not pair_reports and not stored_record_count:
        store_status = "EMPTY"
    elif corrupt_shard_count and corrupt_shard_count == len(pair_reports):
        store_status = "UNREADABLE"
    else:
        store_status = "READABLE"

    return dict(
        base,
        real_data_available=True,
        dataset_fingerprint=current_fingerprint,
        status="SUCCESS",
        evidence_store_status=store_status,
        evidence_directory_configured_by=storage["configured_by"],
        evidence_directory_exists=bool(storage["exists"]),
        stored_pair_count=stored_pair_count,
        stored_record_count=stored_record_count,
        current_candidate_count=len(candidates),
        bound_record_count=bound_record_count,
        stale_record_count=stale_record_count,
        ambiguous_pair_count=ambiguous_pair_count,
        conflicting_pair_count=conflicting_pair_count,
        missing_candidate_count=missing_candidate_count,
        corrupt_shard_count=corrupt_shard_count,
        operator_action_required_count=operator_action_required_count,
        candidates_with_stored_evidence=sum(
            1 for view in candidate_views if view["persisted_evidence_state"] != PERSISTED_NO_STORED_EVIDENCE
        ),
        # 真实性口径：本命令看不到操作者身份，只能报告"记录自述为人工复核"。
        genuine_operator_decision_count=0,
        genuine_operator_decision_note=(
            "no operator-authored file was supplied to this read-only command; "
            "self-declared provenance cannot be independently verified here"
        ),
        stored_human_reviewed_record_count=stored_record_count,
        stored_provenance_classification="SELF_DECLARED_LOCAL_HUMAN_REVIEW",
        synthetic_record_count=0,
        synthetic_record_count_note=(
            "synthetic fixtures live in isolated temporary stores and are never counted here"
        ),
        production_authorized=False,
        pairs=pair_reports,
        candidates=candidate_views,
        decision="NO-GO",
    )


def command_summary(data_dir: str | None, *, reviews_path: str | None = None,
                    evidence_path: str | None = None) -> dict[str, Any]:
    """Coverage dashboard plus unresolved/contradictory register."""
    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return {
            "kind": SUMMARY_KIND,
            "real_data_available": False,
            "status": "NOT_AVAILABLE",
            "reason": str(exc),
            "executable": False,
            "decision": "NO-GO",
        }
    reviews_document = load_reviews(reviews_path) if reviews_path else None
    evidence_document = load_evidence(evidence_path)
    queue = build_review_queue(dataset, reviews_document=reviews_document, evidence_document=evidence_document)
    impact = unique_impact(dataset, queue["candidates"])

    by_priority: dict[str, Counter] = defaultdict(Counter)
    register: list[dict[str, Any]] = []
    for entry in queue["entries"]:
        by_priority[entry["priority"]][entry["human_review_status"]] += 1
        flagged = (
            entry["outcome"] == OUTCOME_CONTRADICTORY_EVIDENCE
            or entry["orientation_evidence_status"] == STATUS_CONTRADICTED
            or entry["orientation"] == ORIENTATION_REVERSED
        )
        if flagged and entry["human_review_status"] == UNREVIEWED:
            register.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "priority": entry["priority"],
                    "outcome": entry["outcome"],
                    "orientation": entry["orientation"],
                    "orientation_evidence_status": entry["orientation_evidence_status"],
                    "blockers": entry["blockers"],
                    "missing_required_evidence": entry["missing_required_evidence"],
                }
            )

    return {
        "kind": SUMMARY_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "real_data_available": True,
        "candidate_count": queue["candidate_count"],
        "priority_counts": queue["priority_counts"],
        "review_status_by_priority": {
            priority: dict(sorted(counter.items())) for priority, counter in sorted(by_priority.items())
        },
        "reviewed_count": queue["reviewed_count"],
        "pending_count": queue["pending_count"],
        "completion_ratio": round(queue["reviewed_count"] / queue["candidate_count"], 4)
        if queue["candidate_count"]
        else None,
        "label_status": "NOT_COMPUTABLE_NO_HUMAN_DECISIONS" if not queue["reviewed_count"] else "PARTIAL_SAMPLE",
        "precision": None,
        "recall": None,
        "orientation_status_counts": dict(
            sorted(Counter(entry["orientation_evidence_status"] for entry in queue["entries"]).items())
        ),
        "unresolved_register": register,
        "unresolved_count": len(register),
        "unique_impact": impact,
        "evidence_rejected_count": len(evidence_document.get("rejected", [])),
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "decision": "NO-GO",
    }


# =============================================================================
# First real evidence pilot packet
# =============================================================================

PILOT_KIND = "NBA_EVIDENCE_PILOT_PACKET"
RETRIEVAL_NOT_RETRIEVED = "EXTERNAL_EVIDENCE_NOT_RETRIEVED"
DEFAULT_PILOT_LIMIT = 5


def select_pilot_candidates(queue: Mapping[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """Deterministic P0-first selection for the first evidence session."""
    selected: list[dict[str, Any]] = []
    for priority in (PRIORITY_P0, PRIORITY_P1, PRIORITY_P2, PRIORITY_P3):
        for entry in queue["entries"]:
            if len(selected) >= limit:
                break
            if entry["priority"] == priority:
                selected.append(entry)
        if len(selected) >= limit:
            break
    return selected


def _pilot_proposal(entry: Mapping[str, Any]) -> dict[str, Any]:
    teams = entry["original_teams"]
    ids = entry["canonical_ids"]
    return {
        "candidate_id": entry["candidate_id"],
        "candidate_fingerprint": entry["candidate_fingerprint"],
        "dataset_fingerprint": entry["dataset_fingerprint"],
        "priority": entry["priority"],
        "canonical_ids": list(ids),
        "original_number_strings": dict(entry["original_number_strings"]),
        "event_date": entry["event_date"],
        "kickoff_metadata": dict(entry["kickoff_metadata"]),
        "original_team_positions": {
            mid: {"home": teams[mid].get("home"), "away": teams[mid].get("away")} for mid in ids
        },
        "orientation": entry["orientation"],
        "reference_source": None,
        "publisher": None,
        "fixture_identifier": None,
        "retrieval_status": RETRIEVAL_NOT_RETRIEVED,
        "retrieved_at": None,
        "search_channels": [
            "official league schedule / results page",
            "club or competition record",
            "archival box score with explicit home/away designation",
        ],
        "proposed_verification_actions": [
            "open a primary record that prints an explicit home/away designation",
            "record the publisher, location and retrieval timestamp",
            "record the provider fixture identifier if the source publishes one",
            "file team_name_correspondence for any spelling variant separately",
            "note any contradicting record verbatim in contradictory_references",
        ],
        "contradicting_information": list(entry["contradictory_evidence"]),
        "evidence_status": RETRIEVAL_NOT_RETRIEVED,
        "human_verified": False,
        "provenance": "NONE",
    }


def _pilot_supplement(queue: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    dossier = render_dossier(queue, entry)
    ids = entry["canonical_ids"]
    teams = entry["original_teams"]
    lines = [
        dossier,
        "",
        "---",
        "",
        "## Pilot supplement (Phase 2-Z)",
        "",
        f"- candidate: `{entry['candidate_id']}` (priority `{entry['priority']}`)",
        f"- candidate fingerprint: `{entry['candidate_fingerprint']}`",
        f"- dataset fingerprint: `{entry['dataset_fingerprint']}`",
        "",
        "### Original records (verbatim, never rewritten)",
        "",
        "| canonical id | home (as printed) | away (as printed) | number (as printed) | kickoff |",
        "| --- | --- | --- | --- | --- |",
    ]
    for mid in ids:
        kickoff = entry["kickoff_metadata"].get(mid, {})
        lines.append(
            f"| `{mid}` | {teams[mid].get('home')} | {teams[mid].get('away')} | "
            f"{entry['original_number_strings'].get(mid)} | "
            f"{kickoff.get('value') or 'unknown'} |"
        )
    lines += [
        "",
        "### Source-side status",
        "",
        f"- orientation evidence status: `{entry['orientation_evidence_status']}`",
        f"- cross-source identity: `{entry['cross_source_identity']}`",
        f"- matched provider sides: {entry['orientation_matched_source_sides'] or 'none'}",
        f"- unresolved provider sides: {entry['orientation_unresolved_sides'] or 'none'}",
        "",
        "### External evidence",
        "",
        f"- retrieval status: `{RETRIEVAL_NOT_RETRIEVED}`",
        "- no external record has been opened by a human in this pilot",
        "- a URL alone would not constitute verification",
        "",
        "### What the operator has to do",
        "",
        "1. Open a primary record with an explicit home/away designation.",
        "2. Record publisher, location and retrieval time.",
        "3. File the evidence as `SOURCE_REFERENCE_SUPPLIED` first.",
        "4. Promote to `SOURCE_REFERENCE_REVIEWED` only after actually reading it.",
        "5. Record contradicting records verbatim; do not resolve them by guessing.",
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "surviving_canonical_id = null",
        "```",
        "",
    ]
    return "\n".join(lines)


def command_pilot(data_dir: str | None, output_dir: str | None, *, limit: int = DEFAULT_PILOT_LIMIT) -> dict[str, Any]:
    """Build the first offline evidence packet (no network, no invented references)."""
    try:
        dataset = _load_dataset_or_error(data_dir)
    except AuditInputError as exc:
        return {
            "kind": PILOT_KIND,
            "real_data_available": False,
            "status": "NOT_AVAILABLE",
            "reason": str(exc),
            "candidate_count": 0,
            "evidence_status": RETRIEVAL_NOT_RETRIEVED,
            "human_reviewed_count": 0,
            "decision": "NO-GO",
        }
    queue = build_review_queue(dataset)
    selected = select_pilot_candidates(queue, limit=limit)
    proposals = [_pilot_proposal(entry) for entry in selected]
    impact = unique_impact(dataset, [c for c in queue["candidates"] if c["candidate_id"] in {e["candidate_id"] for e in selected}])

    validation = {
        "kind": "NBA_EVIDENCE_PILOT_VALIDATION",
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "evidence_records": 0,
        "review_records": 0,
        "review_rejected_count": 0,
        "evidence_rejected_count": 0,
        "total_rejected_count": 0,
        "validation_status": "SUCCESS",
        "note": "no evidence or review records exist yet; nothing was invented",
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
    }

    document = {
        "kind": PILOT_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "real_data_available": True,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "candidate_count": len(selected),
        "priority_counts": dict(sorted(Counter(entry["priority"] for entry in selected).items())),
        "candidate_ids": [entry["candidate_id"] for entry in selected],
        "proposals": proposals,
        "evidence_status": RETRIEVAL_NOT_RETRIEVED,
        "retrieved_reference_count": 0,
        "human_reviewed_count": queue["reviewed_count"],
        "unique_impact": impact,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
    }

    if output_dir:
        validate_output_path(os.path.join(output_dir, "prioritized-five.json"))
        os.makedirs(output_dir, exist_ok=True)
        write_json(os.path.join(output_dir, "prioritized-five.json"), {
            "kind": PILOT_KIND,
            "dataset_fingerprint": queue["dataset_fingerprint"],
            "candidate_count": len(selected),
            "candidates": [dict(entry) for entry in selected],
        })
        write_json(os.path.join(output_dir, "source-reference-inventory.json"), {
            "kind": "NBA_SOURCE_REFERENCE_INVENTORY",
            "dataset_fingerprint": queue["dataset_fingerprint"],
            "retrieval_status": RETRIEVAL_NOT_RETRIEVED,
            "retrieved_reference_count": 0,
            "note": "no external record was opened in this pilot; no URL is invented",
            "candidates": [
                {
                    "candidate_id": entry["candidate_id"],
                    "event_date": entry["event_date"],
                    "original_number_strings": dict(entry["original_number_strings"]),
                    "original_team_positions": {
                        mid: entry["original_teams"][mid] for mid in entry["canonical_ids"]
                    },
                    "kickoff_metadata": dict(entry["kickoff_metadata"]),
                    "orientation": entry["orientation"],
                    "references": [],
                }
                for entry in selected
            ],
        })
        write_json(os.path.join(output_dir, "evidence-proposals.json"), {
            "kind": "NBA_EVIDENCE_PROPOSALS",
            "dataset_fingerprint": queue["dataset_fingerprint"],
            "proposal_count": len(proposals),
            "reviewed_proposal_count": 0,
            "evidence": [],
            "proposals": proposals,
            "note": "proposals are empty of retrieved references; every item stays unreviewed",
        })
        write_json(os.path.join(output_dir, "evidence-validation.json"), validation)
        supplement_dir = os.path.join(output_dir, "candidate-dossier-supplements")
        os.makedirs(supplement_dir, exist_ok=True)
        for entry in selected:
            write_text(
                os.path.join(supplement_dir, f"{entry['candidate_id']}.md"),
                _pilot_supplement(queue, entry),
            )
        write_text(os.path.join(output_dir, "first-review-instructions.md"), render_first_review_instructions(selected))
        write_text(os.path.join(output_dir, "unresolved-evidence-register.md"), render_unresolved_register(queue, selected))
        write_text(os.path.join(output_dir, "pilot-summary.md"), render_pilot_summary(queue, document))
    return document


def render_first_review_instructions(selected: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# 首批 NBA 证据复核操作说明",
        "",
        f"本批次共 **{len(selected)}** 个候选，全部处于 `UNREVIEWED`。",
        "",
        "## 步骤",
        "",
        "1. 打开 `prioritized-five.json`，按 `priority` 顺序处理（P0 优先）。",
        "2. 对每个候选打开一份**主记录**（联赛/俱乐部/赛事官方页或存档技术统计），",
        "   确认它显式写出主队与客队。",
        "3. 填 `evidence-proposals.json` 中对应条目的 `reference_source` / `publisher` /",
        "   `retrieved_at` / `fixture_identifier`，`verification_status` 先填 `SOURCE_REFERENCE_SUPPLIED`。",
        "4. 真正打开并核对内容之后，**由人**把它改为 `SOURCE_REFERENCE_REVIEWED` 并填写 `checked_by`。",
        "5. 队名拼写不同（如 休斯敦/休斯顿）时，另填一条 `team_name_correspondence`，",
        "   同样先 `SUPPLIED`，核实后才可 `REVIEWED`。",
        "6. 发现相反记录时，把它的位置写进 `contradictory_references`，不要猜。",
        "7. 运行 `python -m tools.nba_human_review_workbench validate --evidence <file>`。",
        "   退出码：0 成功 / 4 评审被拒 / 5 证据被拒 / 6 两者都被拒。",
        "",
        "## 边界",
        "",
        "- 只有 `ORIENTATION_INDEPENDENTLY_SUPPORTED` 才表示两源同一性与方向都被核实。",
        "- `SOURCE_SIDE_MATCHED` 只说明外部记录匹配**某一侧**，不构成跨源同一性。",
        "- 人工决定仍是复核证据，不是签名治理授权；不得据此合并 Canonical 记录。",
        "",
    ]
    return "\n".join(lines)


def render_unresolved_register(queue: Mapping[str, Any], selected: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# 未决证据登记",
        "",
        f"- 数据集指纹：`{queue['dataset_fingerprint']}`",
        f"- 本批次候选：{len(selected)}",
        "",
        "| candidate | priority | orientation | evidence status | 未决原因 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in selected:
        lines.append(
            f"| `{entry['candidate_id']}` | `{entry['priority']}` | `{entry['orientation']}` | "
            f"`{entry['orientation_evidence_status']}` | "
            f"{', '.join(entry['missing_required_evidence'])} |"
        )
    lines += [
        "",
        "> 全部条目等待真实人工核对；`EXTERNAL_EVIDENCE_NOT_RETRIEVED` 表示本阶段"
        "未联网、未取得外部记录，不代表外部记录不存在。",
        "",
    ]
    return "\n".join(lines)


def render_pilot_summary(queue: Mapping[str, Any], document: Mapping[str, Any]) -> str:
    impact = document["unique_impact"]
    lines = [
        "# 首批 NBA 证据试点摘要",
        "",
        f"- 数据集指纹：`{document['dataset_fingerprint']}`",
        f"- 候选：{document['candidate_count']}（{document['priority_counts']}）",
        f"- 外部参考已获取：**0**（`{RETRIEVAL_NOT_RETRIEVED}`）",
        f"- 已人工复核：**{document['human_reviewed_count']}**",
        "- 已核验方向证据：**0**",
        "",
        "## 历史影响（去重）",
        "",
        "| 阶段 | 逐对合计 | 去重 |",
        "| --- | --- | --- |",
    ]
    for stage in ("prediction", "odds", "settlement", "evaluation"):
        lines.append(
            f"| {stage} | {impact['summed_reference_totals'].get(stage, 0)} | "
            f"{impact['distinct_record_counts'].get(stage, 0)} |"
        )
    lines += [
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "surviving_canonical_id = null",
        "```",
        "",
    ]
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nba_human_review_workbench",
        description="Offline NBA human-review workbench (read-only).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="write queue, dossiers and templates")
    prepare.add_argument("--data-dir", default=None)
    prepare.add_argument("--output-dir", default=None)
    prepare.add_argument("--reviews", default=None)
    prepare.add_argument("--evidence", default=None)
    prepare.add_argument("--output", default=None, help="optional JSON summary path")

    show = subparsers.add_parser("show", help="show one candidate dossier")
    show.add_argument("--data-dir", default=None)
    show.add_argument("--candidate-id", required=True)
    show.add_argument("--evidence", default=None)
    show.add_argument("--output", default=None)
    show.add_argument("--markdown", default=None)

    validate = subparsers.add_parser("validate", help="validate an imported review file")
    validate.add_argument("--data-dir", default=None)
    validate.add_argument("--reviews", default=None)
    validate.add_argument("--evidence", default=None)
    validate.add_argument("--output", default=None)

    audit_persisted = subparsers.add_parser(
        "audit-persisted",
        help="read-only reconciliation of persisted evidence against the current dataset",
    )
    audit_persisted.add_argument("--data-dir", default=None)
    audit_persisted.add_argument("--evidence-dir", default=None)
    audit_persisted.add_argument("--output", default=None)
    audit_persisted.add_argument("--markdown", default=None)

    persist = subparsers.add_parser(
        "persist", help="persist operator-authored decisions into the durable store"
    )
    persist.add_argument("--data-dir", default=None)
    persist.add_argument("--reviews", required=True)
    persist.add_argument("--evidence", default=None)
    persist.add_argument("--evidence-dir", default=None)
    persist.add_argument("--source-reference", action="append", default=[])
    persist.add_argument(
        "--confirm",
        action="store_true",
        help="explicit operator acknowledgement; without it nothing is written",
    )
    persist.add_argument("--output", default=None)

    pilot = subparsers.add_parser("pilot", help="first evidence packet (offline)")
    pilot.add_argument("--data-dir", default=None)
    pilot.add_argument("--output-dir", default=None)
    pilot.add_argument("--limit", type=int, default=DEFAULT_PILOT_LIMIT)
    pilot.add_argument("--output", default=None)

    summary = subparsers.add_parser("summary", help="coverage dashboard and register")
    summary.add_argument("--data-dir", default=None)
    summary.add_argument("--reviews", default=None)
    summary.add_argument("--evidence", default=None)
    summary.add_argument("--output", default=None)
    summary.add_argument("--markdown", default=None)
    return parser


def render_audit_persisted_markdown(document: Mapping[str, Any]) -> str:
    """Audit-facing summary only: no record bodies, no reviewer identities."""
    if document.get("status") != "SUCCESS":
        return (
            "# Persisted evidence reconciliation\n\n"
            f"**{document.get('status')}** — {document.get('reason_code') or document.get('reason') or ''}\n"
        )

    lines = [
        "# Persisted evidence reconciliation (read-only)",
        "",
        f"- dataset fingerprint: `{document['dataset_fingerprint']}`",
        f"- evidence store: `{document['evidence_store_status']}`"
        f" (exists={document['evidence_directory_exists']},"
        f" configured_by={document['evidence_directory_configured_by']})",
        "",
        "| metric | value |",
        "| --- | --- |",
    ]
    for field in (
        "stored_pair_count",
        "stored_record_count",
        "current_candidate_count",
        "bound_record_count",
        "stale_record_count",
        "ambiguous_pair_count",
        "conflicting_pair_count",
        "missing_candidate_count",
        "corrupt_shard_count",
        "operator_action_required_count",
        "genuine_operator_decision_count",
        "synthetic_record_count",
    ):
        lines.append(f"| `{field}` | {document.get(field)} |")

    lines += ["", "## Pairs", "", "| pair_key | records | binding | state | next action |",
              "| --- | --- | --- | --- | --- |"]
    for item in document.get("pairs", []):
        lines.append(
            f"| `{item['pair_key']}` | {item.get('record_count', 0)} | "
            f"`{item.get('current_binding')}` | `{item.get('persisted_state')}` | "
            f"`{item.get('required_next_action')}` |"
        )

    lines += [
        "",
        "> `bound_record_count` is **not** a count of independently reverified human decisions.",
        "> A stored decision is history; it is never a current approval.",
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "production_authorized = false",
        "surviving_canonical_id = null",
        "decision = NO-GO",
        "```",
        "",
    ]
    return "\n".join(lines)


def render_summary_markdown(document: Mapping[str, Any]) -> str:
    if not document.get("real_data_available"):
        return "# NBA review summary\n\n**NOT_AVAILABLE** — no coverage is claimed.\n"
    lines = [
        "# NBA cross-source review summary",
        "",
        f"- dataset fingerprint: `{document['dataset_fingerprint']}`",
        f"- candidates: **{document['candidate_count']}**",
        f"- reviewed / pending: {document['reviewed_count']} / {document['pending_count']}",
        f"- completion ratio: {document['completion_ratio']}",
        f"- label status: `{document['label_status']}`",
        "",
        "## Priorities",
        "",
        "| priority | review status |",
        "| --- | --- |",
    ]
    for priority, statuses in document["review_status_by_priority"].items():
        rendered = ", ".join(f"{name}: {count}" for name, count in statuses.items())
        lines.append(f"| `{priority}` | {rendered} |")
    lines += ["", "## Orientation evidence", "", "| status | count |", "| --- | --- |"]
    for status, count in document["orientation_status_counts"].items():
        lines.append(f"| `{status}` | {count} |")
    impact = document["unique_impact"]
    lines += [
        "",
        "## Historical impact (summed vs unique)",
        "",
        "| stage | summed | unique |",
        "| --- | --- | --- |",
    ]
    for stage in ("prediction", "odds", "settlement", "evaluation"):
        lines.append(
            f"| {stage} | {impact['summed_reference_totals'].get(stage, 0)} | "
            f"{impact['distinct_record_counts'].get(stage, 0)} |"
        )
    lines += ["", "## Unresolved register", "", "| candidate | priority | outcome | blockers |",
              "| --- | --- | --- | --- |"]
    for item in document["unresolved_register"]:
        lines.append(
            f"| `{item['candidate_id']}` | `{item['priority']}` | `{item['outcome']}` | "
            f"{', '.join(item['blockers']) or '—'} |"
        )
    lines += ["", "```text", "executable = false", "apply_available = false",
              "production_write_authorization = false", "surviving_canonical_id = null", "```", ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            document = command_prepare(
                args.data_dir, args.output_dir, reviews_path=args.reviews, evidence_path=args.evidence
            )
            if args.output:
                write_json(args.output, document)
            print(json.dumps(
                {
                    "real_data_available": document.get("real_data_available"),
                    "candidate_count": document.get("candidate_count"),
                    "priority_counts": document.get("priority_counts"),
                    "dossiers_written": document.get("dossiers_written"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "show":
            document = command_show(args.data_dir, args.candidate_id, evidence_path=args.evidence)
            if args.output:
                write_json(args.output, document)
            if args.markdown and document.get("dossier"):
                write_text(args.markdown, document["dossier"])
            print(json.dumps(document, ensure_ascii=False, sort_keys=True))
            return EXIT_OK if document.get("found") else EXIT_INPUT_ERROR
        if args.command == "validate":
            document = command_validate(args.data_dir, args.reviews, evidence_path=args.evidence)
            if args.output:
                write_json(args.output, document)
            print(json.dumps(
                {
                    "candidate_count": document.get("candidate_count"),
                    "reviewed_count": document.get("reviewed_count"),
                    "pending_count": document.get("pending_count"),
                    "rejected_count": document.get("rejected_count"),
                    "review_rejected_count": document.get("review_rejected_count"),
                    "evidence_rejected_count": document.get("evidence_rejected_count"),
                    "total_rejected_count": document.get("total_rejected_count"),
                    "validation_status": document.get("validation_status"),
                    "label_status": document.get("label_status"),
                    "executable": document.get("executable"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return _validation_exit_code(
                int(document.get("review_rejected_count") or 0),
                int(document.get("evidence_rejected_count") or 0),
            )
        if args.command == "audit-persisted":
            document = command_audit_persisted(args.data_dir, evidence_dir=args.evidence_dir)
            if args.output:
                write_json(args.output, document)
            if args.markdown:
                write_text(args.markdown, render_audit_persisted_markdown(document))
            print(json.dumps(
                {
                    "status": document.get("status"),
                    "evidence_store_status": document.get("evidence_store_status"),
                    "dataset_fingerprint": document.get("dataset_fingerprint"),
                    "stored_pair_count": document.get("stored_pair_count"),
                    "stored_record_count": document.get("stored_record_count"),
                    "current_candidate_count": document.get("current_candidate_count"),
                    "bound_record_count": document.get("bound_record_count"),
                    "stale_record_count": document.get("stale_record_count"),
                    "ambiguous_pair_count": document.get("ambiguous_pair_count"),
                    "conflicting_pair_count": document.get("conflicting_pair_count"),
                    "missing_candidate_count": document.get("missing_candidate_count"),
                    "corrupt_shard_count": document.get("corrupt_shard_count"),
                    "genuine_operator_decision_count": document.get("genuine_operator_decision_count"),
                    "production_authorized": document.get("production_authorized"),
                    "decision": document.get("decision"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK if document.get("status") == "SUCCESS" else EXIT_INPUT_ERROR
        if args.command == "persist":
            document = command_persist(
                args.data_dir,
                args.reviews,
                evidence_path=args.evidence,
                evidence_dir=args.evidence_dir,
                confirm=args.confirm,
                source_references=tuple(args.source_reference or ()),
            )
            if args.output:
                write_json(args.output, document)
            print(json.dumps(
                {
                    "status": document.get("status"),
                    "persisted_count": document.get("persisted_count"),
                    "retained_source_count": document.get("retained_source_count"),
                    "error_count": document.get("error_count"),
                    "review_evidence_only": document.get("review_evidence_only"),
                    "production_authorized": document.get("production_authorized"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            if document.get("status") == "PERSISTED":
                return EXIT_OK
            return EXIT_INPUT_ERROR
        if args.command == "pilot":
            document = command_pilot(args.data_dir, args.output_dir, limit=args.limit)
            if args.output:
                write_json(args.output, document)
            print(json.dumps(
                {
                    "candidate_count": document.get("candidate_count"),
                    "priority_counts": document.get("priority_counts"),
                    "evidence_status": document.get("evidence_status"),
                    "human_reviewed_count": document.get("human_reviewed_count"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "summary":
            document = command_summary(
                args.data_dir, reviews_path=args.reviews, evidence_path=args.evidence
            )
            if args.output:
                write_json(args.output, document)
            if args.markdown:
                write_text(args.markdown, render_summary_markdown(document))
            print(json.dumps(
                {
                    "candidate_count": document.get("candidate_count"),
                    "reviewed_count": document.get("reviewed_count"),
                    "pending_count": document.get("pending_count"),
                    "unresolved_count": document.get("unresolved_count"),
                    "executable": document.get("executable"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK
    except ProtectedOutputError as exc:
        print(f"protected output path rejected: {exc}", file=sys.stderr)
        return EXIT_PROTECTED_PATH
    except ReviewInputError as exc:
        print(f"review input rejected: {exc}", file=sys.stderr)
        return EXIT_REVIEW_REJECTED
    except AuditInputError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    parser.print_help()
    return EXIT_INPUT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())