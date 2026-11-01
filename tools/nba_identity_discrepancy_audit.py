"""
tools/nba_identity_discrepancy_audit.py
=======================================
Offline, read-only NBA cross-source identity **discrepancy** audit.

What this tool does
-------------------
It reproduces, classifies and explains the cross-namespace pairs that appear twice in
the real dataset (``500j-`` lottery XML vs ``500lq-`` live feed) and emits the exact
evidence a human needs to review them:

- ``scan``    : inventory + deterministic candidate list + negative controls
- ``explain`` : candidate-level explanation for one candidate id
- ``preview`` : non-executable reconciliation impact, reusing the existing review
                queue / validation / preview machinery

What this tool is not
---------------------
- Not an approval system. Every candidate is ``UNREVIEWED``; no decision is written.
- Not a merge tool. ``preview`` never selects a surviving Canonical ID, never deletes a
  record, never emits a repair manifest, and always reports ``executable = false``.
- Not a source-identity engine. Runtime identity remains
  ``utils.match_identity.same_event()``; candidate classification comes from
  ``utils.basketball_identity_evidence`` and is explicitly non-authoritative.

Hard constraints
----------------
1. Read-only: the seven protected stores are opened for reading and their SHA-256 is
   captured; the tool never writes into ``data/``.
2. No network, no scraping, no ``refresh()``.
3. Output may only go to the Git-ignored ``reports/`` directory or outside the checkout.
4. Deterministic: stable ordering, no timestamps, identical input bytes produce
   byte-identical JSON.
5. Missing real data is reported as ``NOT_AVAILABLE`` rather than fabricated.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from tools.real_sports_duplicate_review import (
    DEFAULT_MAX_CONTROLS,
    EXIT_INPUT_ERROR,
    EXIT_OK,
    EXIT_PROTECTED_PATH,
    EXIT_REVIEW_REJECTED,
    ProtectedOutputError,
    ReviewInputError,
    _bounded,
    _final_score,
    _kickoff_time,
    _reference_index,
    dataset_fingerprint,
    load_reviews,
    preview_reconciliation,
    validate_output_path,
    validate_reviews,
    write_json,
    write_text,
)
from tools.real_sports_identity_audit import AuditInputError, Dataset, load_dataset
from utils.basketball_identity_evidence import (
    ALLOWED_CLASSES,
    CLASS_ORDER,
    ORIENTATION_REVERSED,
    ORIENTATION_SAME,
    OUTCOME_CANDIDATE_DIFFERENT_EVENT,
    OUTCOME_CANDIDATE_SAME_EVENT,
    OUTCOME_INSUFFICIENT_EVIDENCE,
    classify_cross_source_candidate,
    diagnostic_class,
)
from utils.match_identity import (
    competition_number_for_match,
    normalize_competition_number,
)
from utils.source_identity import fingerprint

# =============================================================================
# Contract
# =============================================================================

SCAN_KIND = "READ_ONLY_NBA_CROSS_SOURCE_DISCREPANCY_SCAN"
EXPLAIN_KIND = "READ_ONLY_NBA_CROSS_SOURCE_DISCREPANCY_EXPLAIN"
QUEUE_KIND = "READ_ONLY_NBA_DISCREPANCY_REVIEW_CANDIDATES"
PREVIEW_KIND = "READ_ONLY_NBA_DISCREPANCY_RECONCILIATION_PREVIEW"
CANDIDATE_SCHEMA_VERSION = "1.0.0"
DOCUMENT_SCHEMA_VERSION = "1.0.0"

CANDIDATE_ID_PREFIX = "NXD-"
UNREVIEWED = "UNREVIEWED"
NO_PROVENANCE = "NONE"

#: League labels observed for the NBA in the two namespaces. This is *presentation*
#: vocabulary, not an equivalence claim: the classifier still reports the label
#: disagreement as a blocker.
NBA_LEAGUE_LABELS = frozenset({"NBA", "美国职业篮球联盟"})

#: Reused from the existing review workflow so a decision file keeps one dialect.
REVIEW_SCHEMA_VERSION = "1.0.0"

LIMITATIONS = (
    "Candidate classes are diagnostics only; they are never human SAME_EVENT verdicts.",
    "Number-form interpretation is an observation-driven hypothesis, not a provider contract.",
    "Home/away orientation is reported, never corrected: no odds and no outcome are swapped.",
    "No Canonical reconciliation, merge, deletion or repair is performed or proposed.",
    "Downstream reference counts are OBSERVED references, not a modification plan.",
)


# =============================================================================
# Small helpers
# =============================================================================

def _namespace(match_id: Any) -> str:
    text = str(match_id or "")
    return text.split("-", 1)[0] if text else ""


def _ordered_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[dict, dict]:
    """
    Deterministic ``(live, lottery)`` ordering of a cross-namespace pair.

    ``500j-`` (lottery XML) always plays the ``lottery`` role; the other namespace plays
    ``live``. Pairs inside one namespace are never candidates, so the remaining pair
    keeps the higher namespace as ``live`` only for stable serialisation.
    """
    a, b = dict(left), dict(right)
    if _namespace(a["id"]) == "500j" and _namespace(b["id"]) != "500j":
        return b, a
    if _namespace(b["id"]) == "500j" and _namespace(a["id"]) != "500j":
        return a, b
    first, second = sorted((a, b), key=lambda item: str(item["id"]))
    return first, second


def _is_nba(record: Mapping[str, Any]) -> bool:
    return str(record.get("league") or "").strip().upper() in {
        label.upper() for label in NBA_LEAGUE_LABELS
    }


def _expected_kickoff(record: Mapping[str, Any]) -> dict[str, Any]:
    value = _kickoff_time(record)
    return {"value": value, "known": value is not None}


# =============================================================================
# Inventory and candidate reproduction
# =============================================================================

def build_inventory(dataset: Dataset) -> dict[str, Any]:
    """Deterministic observed inventory of the loaded dataset."""
    matches = list(dataset.matches)
    basketball = [m for m in matches if m.get("sport") == "basketball"]
    nba = [m for m in basketball if _is_nba(m)]
    namespaces = Counter(_namespace(m["id"]) for m in basketball)
    dates = sorted({str(m.get("date")) for m in basketball if m.get("date")})
    nba_by_date = Counter(str(m.get("date")) for m in nba)
    by_league = Counter(str(m.get("league")) for m in basketball)
    return {
        "total_records": len(matches),
        "basketball_records": len(basketball),
        "nba_records": len(nba),
        "basketball_namespaces": dict(sorted(namespaces.items())),
        "nba_namespaces": dict(
            sorted(Counter(_namespace(m["id"]) for m in nba).items())
        ),
        "basketball_league_labels": dict(sorted(by_league.items())),
        "basketball_dates": dates,
        "nba_records_by_date": dict(sorted(nba_by_date.items())),
        "football_records": sum(1 for m in matches if m.get("sport") == "football"),
    }


def candidate_pairs(dataset: Dataset) -> list[tuple[dict, dict]]:
    """
    Reproduce cross-namespace candidate pairs deterministically.

    A pair is a candidate only when one of two independent structural links holds on the
    **same event date** between two different namespaces:

    1. the two numbering forms interpret to the same encoded competition number; or
    2. one side's team pair equals the other side's team pair (directly or reversed).

    Anything else is not a candidate: pairs with mutually exclusive names become negative
    controls instead of diluting the candidate list.
    """
    basketball = [m for m in {str(m["id"]): m for m in dataset.matches}.values()
                  if m.get("sport") == "basketball"]
    by_date: dict[str, list[dict]] = defaultdict(list)
    for record in basketball:
        by_date[str(record.get("date"))].append(record)

    found: dict[tuple[str, str], tuple[dict, dict]] = {}
    for date in sorted(by_date):
        records = sorted(by_date[date], key=lambda item: str(item["id"]))
        for index, left in enumerate(records):
            for right in records[index + 1:]:
                if _namespace(left["id"]) == _namespace(right["id"]):
                    continue
                evidence = classify_cross_source_candidate(left, right)
                linked = (
                    evidence.numbers_agree is True
                    or evidence.orientation in (ORIENTATION_SAME, ORIENTATION_REVERSED)
                )
                if not linked:
                    continue
                live, lottery = _ordered_pair(left, right)
                key = tuple(sorted((str(live["id"]), str(lottery["id"]))))
                found[key] = (live, lottery)
    return [found[key] for key in sorted(found)]


def _candidate_id(dataset_fp: str, candidate_class: str, ids: Sequence[str]) -> str:
    return CANDIDATE_ID_PREFIX + fingerprint(
        {
            "dataset_fingerprint": dataset_fp,
            "candidate_class": candidate_class,
            "canonical_ids": sorted(str(item) for item in ids),
        }
    )[:24]


def _build_candidate(
    *,
    dataset_fp: str,
    canonical_sha: str,
    references: Mapping[str, Mapping[str, Sequence[str]]],
    live: Mapping[str, Any],
    lottery: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = classify_cross_source_candidate(live, lottery)
    payload = evidence.as_dict()
    candidate_class = diagnostic_class(evidence)
    ids = sorted([str(live["id"]), str(lottery["id"])])

    record: dict[str, Any] = {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "canonical_ids": ids,
        "candidate_class": candidate_class,
        "pair_shape": "PAIR",
        "source_data_sha256": canonical_sha,
        "dataset_fingerprint": dataset_fp,
        "sport": evidence.sport,
        "event_date": evidence.event_date,
        "nba_scope": _is_nba(live) or _is_nba(lottery),
        "namespaces": {mid: _namespace(mid) for mid in ids},
        "team_names": {
            str(live["id"]): {"home": live.get("home"), "away": live.get("away")},
            str(lottery["id"]): {"home": lottery.get("home"), "away": lottery.get("away")},
        },
        "competition": {
            str(live["id"]): live.get("league"),
            str(lottery["id"]): lottery.get("league"),
        },
        "kickoff_times": {
            str(live["id"]): _expected_kickoff(live),
            str(lottery["id"]): _expected_kickoff(lottery),
        },
        "competition_number_raw": {
            str(live["id"]): live.get("jczq_no"),
            str(lottery["id"]): lottery.get("jczq_no"),
        },
        "competition_number_normalized": {
            str(live["id"]): competition_number_for_match(dict(live)),
            str(lottery["id"]): competition_number_for_match(dict(lottery)),
        },
        "lifecycle_status": {
            str(live["id"]): live.get("status"),
            str(lottery["id"]): lottery.get("status"),
        },
        "final_scores": {
            str(live["id"]): _final_score(live),
            str(lottery["id"]): _final_score(lottery),
        },
        "prediction_references": {
            mid: _bounded(references.get(mid, {}).get("prediction", [])) for mid in ids
        },
        "odds_references": {
            mid: _bounded(references.get(mid, {}).get("odds", [])) for mid in ids
        },
        "settlement_references": {
            mid: _bounded(references.get(mid, {}).get("settlement", [])) for mid in ids
        },
        "evaluation_references": {
            mid: _bounded(references.get(mid, {}).get("evaluation", [])) for mid in ids
        },
        "outcome": payload["outcome"],
        "orientation": payload["orientation"],
        "home_relation": payload["home_relation"],
        "away_relation": payload["away_relation"],
        "aligned_home_relation": payload["aligned_home_relation"],
        "aligned_away_relation": payload["aligned_away_relation"],
        "league_relation": payload["league_relation"],
        "kickoff_relation": payload["kickoff_relation"],
        "numbers_comparable": payload["numbers_comparable"],
        "numbers_agree": payload["numbers_agree"],
        "number_evidence": payload["number_evidence"],
        "supporting_evidence": payload["supporting_evidence"],
        "contradictory_evidence": payload["contradictory_evidence"],
        "blockers": payload["blockers"],
        "evidence_notes": payload["notes"],
        "authoritative": False,
        "is_approval": False,
        "authorizes_odds_merge": False,
        "authorizes_event_admission": False,
        "reviewer_status": UNREVIEWED,
        "review_provenance": NO_PROVENANCE,
        "requires_human_review": True,
    }
    record["candidate_id"] = _candidate_id(dataset_fp, candidate_class, ids)
    record["candidate_fingerprint"] = _candidate_fingerprint(record)
    return record


def _candidate_fingerprint(payload: Mapping[str, Any]) -> str:
    evidence = {
        key: value
        for key, value in payload.items()
        if key not in {"candidate_id", "candidate_fingerprint", "reviewer_status", "review_provenance"}
    }
    return fingerprint(evidence)


# =============================================================================
# Candidate queue (review-compatible)
# =============================================================================

def build_candidates(dataset: Dataset) -> list[dict[str, Any]]:
    """Deterministically build the discrepancy candidate list (read-only)."""
    dataset_fp = dataset_fingerprint(dataset)
    canonical_sha = dataset.hashes().get("daily_matches.json", "")
    references = _reference_index(dataset)
    return [
        _build_candidate(
            dataset_fp=dataset_fp,
            canonical_sha=canonical_sha,
            references=references,
            live=live,
            lottery=lottery,
        )
        for live, lottery in candidate_pairs(dataset)
    ]


# =============================================================================
# Negative controls
# =============================================================================

def build_negative_controls(
    dataset: Dataset, candidates: Sequence[Mapping[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    """
    Deterministic negative controls drawn from the real dataset (no invention).

    Shapes:
    - ``SAME_TEAMS_DIFFERENT_DATE``  : identical unordered team pair on two dates;
    - ``CROSS_SPORT_SHARED_NUMBER``  : the same competition number in two sports;
    - ``DIFFERENT_TEAMS_SAME_SLOT``  : same date and number shape, mutually exclusive teams.
    """
    basketball = [m for m in dataset.matches if m.get("sport") == "basketball"]
    controls: list[dict[str, Any]] = []

    def add(shape: str, left: dict, right: dict) -> None:
        if len(controls) >= limit:
            return
        live, lottery = _ordered_pair(left, right)
        evidence = classify_cross_source_candidate(live, lottery)
        controls.append(
            {
                "control_shape": shape,
                "canonical_ids": sorted([str(live["id"]), str(lottery["id"])]),
                "expected_outcome": "CANDIDATE_DIFFERENT_EVENT",
                "observed_outcome": evidence.outcome,
                "matches_expectation": evidence.outcome
                == OUTCOME_CANDIDATE_DIFFERENT_EVENT,
                "evidence": evidence.as_dict(),
            }
        )

    # Shape 1: identical team pair, different dates.
    by_pair: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for record in basketball:
        home = str(record.get("home") or "").strip()
        away = str(record.get("away") or "").strip()
        if home and away:
            by_pair[tuple(sorted((home, away)))].append(record)
    for pair_key in sorted(by_pair):
        records = sorted(by_pair[pair_key], key=lambda item: (str(item.get("date")), str(item["id"])))
        for index, first in enumerate(records):
            for second in records[index + 1:]:
                if str(first.get("date")) == str(second.get("date")):
                    continue
                add("SAME_TEAMS_DIFFERENT_DATE", first, second)
                break
            if len(controls) >= limit:
                break
        if len(controls) >= limit:
            break

    # Shape 2: same competition number shared across sports.
    football = [m for m in dataset.matches if m.get("sport") == "football"]
    football_numbers = defaultdict(list)
    for record in football:
        number = competition_number_for_match(record)
        if number:
            football_numbers[number].append(record)
    for record in sorted(basketball, key=lambda item: str(item["id"])):
        number = competition_number_for_match(record)
        if not number or len(controls) >= limit:
            continue
        for other in football_numbers.get(number, [])[:1]:
            add("CROSS_SPORT_SHARED_NUMBER", other, record)
            break

    # Shape 3: same date, both numbered, mutually exclusive teams.
    by_date: dict[str, list[dict]] = defaultdict(list)
    for record in basketball:
        by_date[str(record.get("date"))].append(record)
    for date in sorted(by_date):
        records = sorted(by_date[date], key=lambda item: str(item["id"]))
        for index, left in enumerate(records):
            for right in records[index + 1:]:
                if _namespace(left["id"]) == _namespace(right["id"]):
                    continue
                evidence = classify_cross_source_candidate(left, right)
                if evidence.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT:
                    add("DIFFERENT_TEAMS_SAME_SLOT", left, right)
                if len(controls) >= limit:
                    break
            if len(controls) >= limit:
                break
        if len(controls) >= limit:
            break

    return controls


# =============================================================================
# Commands
# =============================================================================

def build_scan(dataset: Dataset, *, max_controls: int = DEFAULT_MAX_CONTROLS) -> dict[str, Any]:
    """Assemble the deterministic read-only scan document."""
    candidates = build_candidates(dataset)
    controls = build_negative_controls(dataset, candidates, limit=max_controls)
    class_counts = Counter(item["candidate_class"] for item in candidates)
    outcome_counts = Counter(item["outcome"] for item in candidates)
    orientation_counts = Counter(item["orientation"] for item in candidates)
    nba_candidates = [item for item in candidates if item["nba_scope"]]
    reference_totals = Counter()
    for item in candidates:
        for stage in ("prediction_references", "odds_references", "settlement_references", "evaluation_references"):
            for mid in item["canonical_ids"]:
                reference_totals[stage] += int(item[stage][mid]["count"])

    return {
        "kind": SCAN_KIND,
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "dataset_fingerprint": dataset_fingerprint(dataset),
        "protected_store_hashes": dataset.hashes(),
        "data_gaps": list(dataset.gaps),
        "schema_errors": list(dataset.schema_errors),
        "real_data_available": True,
        "inventory": build_inventory(dataset),
        "candidate_count": len(candidates),
        "nba_candidate_count": len(nba_candidates),
        "candidate_class_counts": dict(sorted(class_counts.items())),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "orientation_counts": dict(sorted(orientation_counts.items())),
        "downstream_reference_totals": dict(sorted(reference_totals.items())),
        "candidates": candidates,
        "negative_controls": controls,
        "negative_control_count": len(controls),
        "negative_controls_all_distinct": all(
            item["matches_expectation"] for item in controls
        ) if controls else None,
        "approval_count": 0,
        "human_reviewed_count": 0,
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
        "limitations": list(LIMITATIONS),
    }


def not_available_document(reason: str) -> dict[str, Any]:
    """Honest ``NOT_AVAILABLE`` result when the real dataset cannot be read."""
    return {
        "kind": SCAN_KIND,
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "real_data_available": False,
        "status": "NOT_AVAILABLE",
        "reason": reason,
        "candidate_count": None,
        "candidate_class_counts": {},
        "candidates": [],
        "negative_controls": [],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "no_mutation_performed": True,
        "decision": "NO-GO",
        "limitations": list(LIMITATIONS) + ["real dataset absent: no candidate count is claimed"],
    }


def command_scan(data_dir: str | None, *, max_controls: int) -> dict[str, Any]:
    try:
        dataset = load_dataset(data_dir)
    except AuditInputError as exc:
        return not_available_document(str(exc))
    return build_scan(dataset, max_controls=max_controls)


def command_explain(data_dir: str | None, candidate_id: str) -> dict[str, Any]:
    """Explain one candidate (read-only). Unknown ids fail closed."""
    try:
        dataset = load_dataset(data_dir)
    except AuditInputError as exc:
        return not_available_document(str(exc))
    candidates = build_candidates(dataset)
    index = {item["candidate_id"]: item for item in candidates}
    candidate = index.get(str(candidate_id))
    related = [
        item["candidate_id"]
        for item in candidates
        if item["candidate_id"] != candidate_id
        and set(item["canonical_ids"]) & set(candidate["canonical_ids"])
    ] if candidate else []
    return {
        "kind": EXPLAIN_KIND,
        "schema_version": DOCUMENT_SCHEMA_VERSION,
        "dataset_fingerprint": dataset_fingerprint(dataset),
        "candidate_id": str(candidate_id),
        "found": candidate is not None,
        "candidate": candidate,
        "related_candidate_ids": sorted(related),
        "candidate_ids": sorted(index),
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
        "surviving_canonical_id": None,
        "decision": "NO-GO",
    }


def build_queue(dataset: Dataset, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Review-compatible queue document (same dialect as the existing workflow)."""
    return {
        "kind": QUEUE_KIND,
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_fingerprint": dataset_fingerprint(dataset),
        "candidate_count": len(candidates),
        "candidates": [dict(item) for item in candidates],
        "executable": False,
        "apply_available": False,
        "production_write_authorization": False,
    }


def command_preview(data_dir: str | None, reviews_path: str | None) -> dict[str, Any]:
    """
    Non-executable reconciliation impact preview.

    Reuses ``validate_reviews`` and ``preview_reconciliation`` unchanged so the impact
    statement keeps the existing contract (no survivor, no manifest, no mutation).
    """
    try:
        dataset = load_dataset(data_dir)
    except AuditInputError as exc:
        return not_available_document(str(exc))
    candidates = build_candidates(dataset)
    queue = build_queue(dataset, candidates)
    if reviews_path:
        reviews = load_reviews(reviews_path)
    else:
        reviews = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "dataset_fingerprint": queue["dataset_fingerprint"],
            "reviews": [],
        }
    validation = validate_reviews(queue, reviews)
    impact = preview_reconciliation(dataset, queue, validation)
    impact["kind"] = PREVIEW_KIND
    impact["review_validation"] = {
        "reviewed_count": validation.get("reviewed_count", 0),
        "pending_count": validation.get("pending_count", 0),
        "rejected_count": validation.get("rejected_count", 0),
        "rejected": [
            {
                "candidate_id": item.get("candidate_id"),
                "reason_code": item.get("reason_code"),
            }
            for item in validation.get("rejected", [])
        ][:20],
        "label_status": validation.get("label_status"),
        "label_provenance": validation.get("label_provenance"),
        "precision": None,
        "recall": None,
    }
    impact["discrepancy_evidence"] = {
        "candidate_classes": dict(
            sorted(Counter(item["candidate_class"] for item in candidates).items())
        ),
        "orientations": dict(
            sorted(Counter(item["orientation"] for item in candidates).items())
        ),
        "number_form_pairs": sorted(
            {
                (item["number_evidence"][0].get("method"), item["number_evidence"][1].get("method"))
                for item in candidates
            }
        ),
        "human_reviewed_count": validation.get("reviewed_count", 0),
        "label_status": validation.get("label_status"),
    }
    impact["approval_count"] = 0
    impact["surviving_canonical_id"] = None
    impact["executable"] = False
    impact["apply_available"] = False
    impact["production_write_authorization"] = False
    return impact


# =============================================================================
# Markdown rendering
# =============================================================================

def render_scan_markdown(document: Mapping[str, Any]) -> str:
    lines = ["# NBA cross-source identity discrepancy scan", ""]
    if not document.get("real_data_available"):
        lines += [
            "**Real dataset NOT_AVAILABLE** — no candidate count is claimed.",
            "",
            f"- reason: `{document.get('reason')}`",
            "",
        ]
        return "\n".join(lines)
    inventory = document["inventory"]
    lines += [
        f"- dataset fingerprint: `{document['dataset_fingerprint']}`",
        f"- basketball records: **{inventory['basketball_records']}** "
        f"(NBA {inventory['nba_records']})",
        f"- basketball namespaces: `{inventory['basketball_namespaces']}`",
        f"- candidate pairs: **{document['candidate_count']}** "
        f"(NBA {document['nba_candidate_count']})",
        f"- negative controls: {document['negative_control_count']}",
        "",
        "## Candidate classes",
        "",
        "| class | count |",
        "| --- | --- |",
    ]
    for name, count in document["candidate_class_counts"].items():
        lines.append(f"| `{name}` | {count} |")
    lines += ["", "## Outcomes", "", "| outcome | count |", "| --- | --- |"]
    for name, count in document["outcome_counts"].items():
        lines.append(f"| `{name}` | {count} |")
    lines += [
        "",
        "## Candidates",
        "",
        "| candidate | date | ids | class | orientation | numbers |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in document["candidates"]:
        numbers = " / ".join(
            str(evidence.get("raw")) for evidence in item["number_evidence"]
        )
        lines.append(
            f"| `{item['candidate_id']}` | {item['event_date']} | "
            f"`{', '.join(item['canonical_ids'])}` | `{item['candidate_class']}` | "
            f"`{item['orientation']}` | {numbers} |"
        )
    lines += [
        "",
        "## Negative controls",
        "",
        "| shape | ids | observed outcome | distinct |",
        "| --- | --- | --- | --- |",
    ]
    for control in document["negative_controls"]:
        lines.append(
            f"| `{control['control_shape']}` | `{', '.join(control['canonical_ids'])}` | "
            f"`{control['observed_outcome']}` | {control['matches_expectation']} |"
        )
    lines += [
        "",
        "## Guarantees",
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "surviving_canonical_id = null",
        "approval_count = 0",
        "```",
        "",
    ]
    return "\n".join(lines)


def render_preview_markdown(document: Mapping[str, Any]) -> str:
    if not document.get("real_data_available"):
        return "# NBA discrepancy preview\n\n**NOT_AVAILABLE** — no preview is claimed.\n"
    summary = document.get("summary", {})
    lines = [
        "# NBA cross-source discrepancy reconciliation preview",
        "",
        f"- dataset fingerprint: `{document.get('dataset_fingerprint')}`",
        f"- candidates: **{document.get('candidate_count')}**",
        f"- reviewed: {document.get('reviewed_count')} (pending {document.get('pending_count')})",
        f"- unreviewed: {summary.get('unreviewed')}",
        "",
        "```text",
        "executable = false",
        "apply_available = false",
        "production_write_authorization = false",
        "surviving_canonical_id = null",
        "```",
        "",
        "## Entries",
        "",
        "| candidate | class | relationship | blocked reasons |",
        "| --- | --- | --- | --- |",
    ]
    for entry in document.get("entries", []):
        lines.append(
            f"| `{entry['candidate_id']}` | `{entry['candidate_class']}` | "
            f"`{entry['identity_relationship']}` | {', '.join(entry['blocked_reasons'])} |"
        )
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nba_identity_discrepancy_audit",
        description="Read-only NBA cross-source identity discrepancy audit.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="inventory, candidates and negative controls")
    scan.add_argument("--data-dir", default=None)
    scan.add_argument("--output", default=None, help="JSON output path")
    scan.add_argument("--markdown", default=None, help="Markdown output path")
    scan.add_argument("--max-controls", type=int, default=DEFAULT_MAX_CONTROLS)

    explain = subparsers.add_parser("explain", help="explain one candidate")
    explain.add_argument("--data-dir", default=None)
    explain.add_argument("--candidate-id", required=True)
    explain.add_argument("--output", default=None)

    preview = subparsers.add_parser("preview", help="non-executable impact preview")
    preview.add_argument("--data-dir", default=None)
    preview.add_argument("--reviews", default=None)
    preview.add_argument("--output", default=None)
    preview.add_argument("--markdown", default=None)
    return parser


def _emit(document: Mapping[str, Any], path: str | None) -> None:
    if path:
        write_json(path, document)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            document = command_scan(args.data_dir, max_controls=args.max_controls)
            _emit(document, args.output)
            if args.markdown:
                write_text(args.markdown, render_scan_markdown(document))
            print(json.dumps(
                {
                    "real_data_available": document["real_data_available"],
                    "candidate_count": document["candidate_count"],
                    "candidate_class_counts": document["candidate_class_counts"],
                    "negative_control_count": document["negative_control_count"],
                },
                ensure_ascii=False, sort_keys=True,
            ))
            return EXIT_OK
        if args.command == "explain":
            document = command_explain(args.data_dir, args.candidate_id)
            _emit(document, args.output)
            print(json.dumps(document, ensure_ascii=False, sort_keys=True))
            return EXIT_OK if document.get("found") else EXIT_INPUT_ERROR
        if args.command == "preview":
            document = command_preview(args.data_dir, args.reviews)
            _emit(document, args.output)
            if args.markdown:
                write_text(args.markdown, render_preview_markdown(document))
            print(json.dumps(
                {
                    "candidate_count": document.get("candidate_count"),
                    "reviewed_count": document.get("reviewed_count"),
                    "pending_count": document.get("pending_count"),
                    "executable": document.get("executable"),
                    "apply_available": document.get("apply_available"),
                    "surviving_canonical_id": document.get("surviving_canonical_id"),
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