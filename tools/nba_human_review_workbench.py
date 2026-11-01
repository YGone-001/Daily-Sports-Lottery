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
        if record.get("verification_status") not in EVIDENCE_STATUS_VALUES:
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "UNSUPPORTED_VERIFICATION_STATUS",
                    "detail": f"status must be one of {list(EVIDENCE_STATUS_VALUES)}",
                }
            )
            continue
        if not str(record.get("checked_by") or "").strip():
            rejected.append(
                {
                    "index": index,
                    "evidence_id": record.get("evidence_id"),
                    "reason_code": "MISSING_CHECKED_BY",
                    "detail": "an evidence item needs a human 'checked_by' identity",
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
        accepted.append(record)
    return {
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "evidence": accepted,
        "rejected": rejected,
        "rejected_count": len(rejected),
    }


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if not ch.isspace()).lower()


def _pair_matches(evidence: Mapping[str, Any], teams: Mapping[str, Any]) -> bool:
    pair = {_norm(evidence.get("home_team")), _norm(evidence.get("away_team"))}
    wanted = {_norm(value.get("home")) for value in teams.values()} | {
        _norm(value.get("away")) for value in teams.values()
    }
    return len(pair) == 2 and pair == wanted


def resolve_orientation(
    candidate: Mapping[str, Any], evidence_document: Mapping[str, Any]
) -> dict[str, Any]:
    """
    Resolve orientation evidence for one candidate.

    Only ``SOURCE_REFERENCE_REVIEWED`` items whose event date and team pair match the
    candidate can support or contradict orientation. A URL alone stays unverified.
    """
    items = [
        item
        for item in evidence_document.get("evidence", [])
        if item.get("candidate_id") == candidate["candidate_id"]
    ]
    supplied = [item for item in items if item["verification_status"] == STATUS_SOURCE_REFERENCE_SUPPLIED]
    reviewed = [item for item in items if item["verification_status"] == STATUS_SOURCE_REFERENCE_REVIEWED]

    matched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    for item in reviewed:
        if str(item.get("actual_event_date")) != str(candidate.get("event_date")):
            unmatched.append(f"{item['evidence_id']}: event date mismatch")
            continue
        if not _pair_matches(item, candidate.get("team_names", {})):
            unmatched.append(f"{item['evidence_id']}: team pair mismatch")
            continue
        matched.append(item)

    if not matched:
        return {
            "status": STATUS_UNVERIFIED,
            "reason": (
                "no reviewed external evidence with a matching event date and team pair"
                if not reviewed
                else "supplied evidence was not reviewed, or does not match the candidate"
            ),
            "supplied_count": len(supplied),
            "reviewed_count": len(reviewed),
            "matched_count": 0,
            "unmatched": unmatched,
            "home_team_claims": {},
            "contradictory_references": [],
            "supporting_evidence_ids": [],
            "authoritative": False,
        }

    claims: dict[str, list[str]] = defaultdict(list)
    for item in matched:
        claims[str(item["home_team"])].append(str(item["evidence_id"]))
    if len(claims) > 1:
        return {
            "status": STATUS_CONTRADICTED,
            "reason": "reviewed external evidence disagrees about the home team",
            "supplied_count": len(supplied),
            "reviewed_count": len(reviewed),
            "matched_count": len(matched),
            "unmatched": unmatched,
            "home_team_claims": {home: sorted(ids) for home, ids in sorted(claims.items())},
            "contradictory_references": sorted(
                {ref for item in matched for ref in item.get("contradictory_references", [])}
            ),
            "supporting_evidence_ids": sorted(item["evidence_id"] for item in matched),
            "authoritative": False,
        }

    home = next(iter(claims))
    return {
        "status": STATUS_INDEPENDENTLY_SUPPORTED,
        "reason": "reviewed external evidence matches the event date and team pair",
        "supplied_count": len(supplied),
        "reviewed_count": len(reviewed),
        "matched_count": len(matched),
        "unmatched": unmatched,
        "home_team_claims": {home: sorted(claims[home])},
        "contradictory_references": sorted(
            {ref for item in matched for ref in item.get("contradictory_references", [])}
        ),
        "supporting_evidence_ids": sorted(item["evidence_id"] for item in matched),
        "authoritative": False,
    }


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
    """Blank independent-orientation evidence template (operator fills, nobody fetches)."""
    return {
        "kind": EVIDENCE_KIND,
        "schema_version": WORKBENCH_SCHEMA_VERSION,
        "dataset_fingerprint": queue["dataset_fingerprint"],
        "instructions": [
            "Add one object per independently checked event record (box score, fixture page, document).",
            "verification_status must be SOURCE_REFERENCE_SUPPLIED or SOURCE_REFERENCE_REVIEWED.",
            "Only SOURCE_REFERENCE_REVIEWED items with matching date and team pair can support orientation.",
            "A URL alone is never proof; automation or approval fields are rejected.",
            "The workbench never fetches the network.",
        ],
        "required_keys": sorted(EVIDENCE_REQUIRED_KEYS),
        "allowed_statuses": list(EVIDENCE_STATUS_VALUES),
        "example": {
            "evidence_id": "EV-0001",
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
            "checked_by": "reviewer identity",
            "verification_status": STATUS_SOURCE_REFERENCE_REVIEWED,
            "contradictory_references": [],
        },
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


def command_validate(data_dir: str | None, reviews_path: str | None, *, evidence_path: str | None = None) -> dict[str, Any]:
    """Validate an imported review file against the current NBA queue."""
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

    summary = subparsers.add_parser("summary", help="coverage dashboard and register")
    summary.add_argument("--data-dir", default=None)
    summary.add_argument("--reviews", default=None)
    summary.add_argument("--evidence", default=None)
    summary.add_argument("--output", default=None)
    summary.add_argument("--markdown", default=None)
    return parser


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
                    "label_status": document.get("label_status"),
                    "executable": document.get("executable"),
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK if not document.get("rejected_count") else EXIT_REVIEW_REJECTED
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