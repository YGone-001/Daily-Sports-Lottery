"""Historical Review Evidence Presentation Layer.
=============================================
Read-only helpers that derive historical model performance exclusively
from authentic immutable prematch evidence (evaluation rows, settlements,
and prediction snapshots).

Key Invariants:
- Never calls `predict_match(...)` or recomputes probabilities after kickoff.
- Never mutates Elo, team profiles, snapshots, settlements, or evaluation rows.
- Derives predicted outcomes and confidence solely from stored `model_probabilities`.
- Validates the strict unbroken evidence chain and full cross-record provenance equality:
    canonical match score <-> settlement <-> prediction snapshot <-> evaluation row
- Duplicate evaluation identities carrying conflicting payloads fail closed as ambiguous.
- Distinguishes qualified current-version evaluations, historical-version evaluations,
  no-evidence matches, and ambiguous/invalid records.
- Preserves visibility of finished matches even when no prematch snapshot exists,
  while strictly excluding unverified matches from accuracy denominators.
- Complete, non-overlapping partition across all visible finished matches:
    visible finalized matches
    = qualified current-version matches
    + historical-only matches
    + no-evidence matches
    + ambiguous/invalid matches
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Sequence

import config
from utils.classification_evaluation import (
    ClassificationEvaluationError,
    classes_for_sport,
    normalize_class_probabilities,
    _actual_class,
)
from utils.evaluation_rows import (
    evaluation_id_for,
    get_all_evaluation_rows,
    _as_dict,
)
from utils.match_lifecycle import valid_full_time_score
from utils.prediction_snapshots import (
    MODEL_NAMES,
    snapshot_id_for,
    _load_store as load_snapshots_store,
)
from utils.settlements import (
    extract_final_score,
    is_valid_prematch,
    outcome_from_score,
    result_fingerprint,
    settlement_id_for,
    _load_store as load_settlements_store,
)

FOOTBALL_CLASSES: tuple[str, ...] = ("home_win", "draw", "away_win")
BASKETBALL_CLASSES: tuple[str, ...] = ("home_win", "away_win")

OUTCOME_LABELS: dict[str, str] = {
    "home_win": "主胜",
    "draw": "平局",
    "away_win": "客胜",
}


def _format_score_str(score_obj: dict | None) -> str:
    """Safe score string formatter without arbitrary type casting."""
    if not isinstance(score_obj, dict):
        return ""
    ft = score_obj.get("ft")
    if isinstance(ft, (list, tuple)) and len(ft) >= 2:
        return f"{ft[0]}-{ft[1]}"
    return ""


def _derive_actual_outcome(score_obj: dict | None, sport: str) -> str:
    """Derive factual outcome from full-time score strictly.

    Reuses utils.match_lifecycle.valid_full_time_score without casting
    arbitrary score types. Equal score produces factual 'draw' for both sports.
    """
    if not valid_full_time_score(score_obj):
        return ""
    ft = score_obj["ft"]  # type: ignore[index]
    hg, ag = ft[0], ft[1]
    if hg > ag:
        return "home_win"
    if hg == ag:
        return "draw"
    return "away_win"


def _parse_iso(value: object) -> datetime | None:
    """Parse ISO datetime safely."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _validate_prematch_timestamps(eval_row: dict, snapshot: dict) -> bool:
    """Validate prediction generation and kickoff timestamps.

    Both must be present, parseable, timezone-compatible, and ordered
    as prematch evidence using absolute instants.
    """
    gen_str = eval_row.get("prediction_generated_at")
    kick_str = eval_row.get("kickoff_at")
    gen_dt = _parse_iso(gen_str)
    kick_dt = _parse_iso(kick_str)
    if gen_dt is None or kick_dt is None:
        return False

    # Timezone compatibility
    if (gen_dt.tzinfo is None) != (kick_dt.tzinfo is None):
        return False

    # Absolute instant order: generated <= kickoff
    if gen_dt.tzinfo is not None:
        if gen_dt.timestamp() > kick_dt.timestamp():
            return False
    else:
        if gen_dt > kick_dt:
            return False

    # Snapshot timestamps must also be valid prematch
    snap_gen_str = snapshot.get("generated_at")
    snap_kick_str = snapshot.get("kickoff_at")
    snap_gen_dt = _parse_iso(snap_gen_str)
    snap_kick_dt = _parse_iso(snap_kick_str)
    if snap_gen_dt is None or snap_kick_dt is None:
        return False

    if (snap_gen_dt.tzinfo is None) != (snap_kick_dt.tzinfo is None):
        return False

    if snap_gen_dt.tzinfo is not None:
        if snap_gen_dt.timestamp() > snap_kick_dt.timestamp():
            return False
    else:
        if snap_gen_dt > snap_kick_dt:
            return False

    if not is_valid_prematch(snapshot):
        return False

    return True


def _validate_probabilities_and_classes(eval_row: dict, sport: str) -> bool:
    """Verify probability vector passes authoritative classification validation."""
    try:
        classes = classes_for_sport(sport)
        normalize_class_probabilities(eval_row)
        _actual_class(eval_row, classes)
        return True
    except (ClassificationEvaluationError, ValueError, TypeError):
        return False


def qualify_current_version_evaluation(
    match: dict,
    eval_row: dict,
    snapshots: dict[str, dict],
    settlements: dict[str, dict],
) -> tuple[bool, str]:
    """Strictly qualify whether an evaluation row represents valid current-version evidence.

    Enforces full provenance equality across snapshot, settlement, and evaluation row:
    1. Deterministic identities: snapshot_id, settlement_id, evaluation_id.
    2. Stored record identities in snapshot and settlement match expected IDs.
    3. Match ID, sport, model_name, and model_version agree across all three records.
    4. Exact temporal provenance: generated_at and kickoff_at agree across all records,
       plus absolute-instant prematch ordering.
    5. Frozen prediction payload equality between snapshot and evaluation row
       (home_elo, away_elo, league, team names, model_probabilities, display_probabilities,
        market_odds, market_implied_probabilities, expected_values, expected_score_data).
    6. Result provenance equality (final_score, actual_outcome, result_fingerprint)
       matching authoritative canonical score.
    7. Basketball draw safely excluded from two-class classification.
    8. Passes authoritative classification validation.
    """
    m_id = str(match.get("id") or "")
    m_sport = str(match.get("sport") or "football")

    expected_version = config.MODEL_VERSIONS.get(m_sport, "")
    expected_model_name = MODEL_NAMES.get(m_sport, "")
    if not expected_version or not expected_model_name:
        return False, "unsupported_sport_model"

    # Deterministic IDs
    expected_snapshot_id = snapshot_id_for(m_id, expected_version)
    expected_settlement_id = settlement_id_for(expected_snapshot_id)
    expected_evaluation_id = evaluation_id_for(expected_snapshot_id, expected_settlement_id)

    # 1. Identity validation on evaluation row
    if eval_row.get("match_id") != m_id:
        return False, "match_id_mismatch"
    if eval_row.get("sport") != m_sport:
        return False, "sport_mismatch"
    if eval_row.get("model_version") != expected_version:
        return False, "model_version_mismatch"
    if eval_row.get("model_name") != expected_model_name:
        return False, "model_name_mismatch"
    if eval_row.get("snapshot_id") != expected_snapshot_id:
        return False, "snapshot_id_contract_violation"
    if eval_row.get("settlement_id") != expected_settlement_id:
        return False, "settlement_id_contract_violation"
    if eval_row.get("evaluation_id") != expected_evaluation_id:
        return False, "evaluation_id_contract_violation"

    # 2. Referenced records existence & stored identity
    snapshot = snapshots.get(expected_snapshot_id)
    if not snapshot or not isinstance(snapshot, dict):
        return False, "referenced_snapshot_missing"

    settlement = settlements.get(expected_settlement_id)
    if not settlement or not isinstance(settlement, dict):
        return False, "referenced_settlement_missing"

    if snapshot.get("snapshot_id") != expected_snapshot_id:
        return False, "snapshot_internal_id_mismatch"
    if settlement.get("settlement_id") != expected_settlement_id:
        return False, "settlement_internal_id_mismatch"
    if settlement.get("snapshot_id") != expected_snapshot_id:
        return False, "settlement_snapshot_id_mismatch"

    # 3. Sport and Model Identity consistency across all three records
    if snapshot.get("match_id") != m_id or settlement.get("match_id") != m_id:
        return False, "match_id_provenance_mismatch"
    if snapshot.get("sport") != m_sport or settlement.get("sport") != m_sport:
        return False, "sport_provenance_mismatch"
    if snapshot.get("model_name") != expected_model_name or settlement.get("model_name") != expected_model_name:
        return False, "model_name_provenance_mismatch"
    if snapshot.get("model_version") != expected_version or settlement.get("model_version") != expected_version:
        return False, "model_version_provenance_mismatch"

    # 4. Temporal Provenance exact equality
    snap_gen = snapshot.get("generated_at")
    snap_kick = snapshot.get("kickoff_at")
    if eval_row.get("prediction_generated_at") != snap_gen:
        return False, "prediction_generated_at_provenance_mismatch"
    if settlement.get("prediction_generated_at") != snap_gen:
        return False, "settlement_prediction_generated_at_provenance_mismatch"
    if eval_row.get("kickoff_at") != snap_kick:
        return False, "kickoff_at_provenance_mismatch"
    if settlement.get("kickoff_at") != snap_kick:
        return False, "settlement_kickoff_at_provenance_mismatch"

    if not _validate_prematch_timestamps(eval_row, snapshot):
        return False, "invalid_prematch_timestamps"

    # 5. Frozen Prediction Payload equality
    for field in ("league", "home_team", "away_team", "home_elo", "away_elo"):
        if eval_row.get(field) != snapshot.get(field):
            return False, f"{field}_payload_mismatch"

    for field in (
        "model_probabilities",
        "display_probabilities",
        "market_odds",
        "market_implied_probabilities",
        "expected_values",
        "expected_score_data",
    ):
        if eval_row.get(field) != _as_dict(snapshot.get(field)):
            return False, f"{field}_payload_mismatch"

    # 6. Result Provenance
    score = match.get("score")
    if not valid_full_time_score(score):
        return False, "canonical_score_invalid"

    canonical_final = extract_final_score(match)
    if canonical_final is None:
        return False, "canonical_final_score_missing"

    canonical_outcome = outcome_from_score(canonical_final)
    if m_sport == "basketball" and canonical_outcome == "draw":
        return False, "basketball_tied_score"

    if eval_row.get("final_score") != canonical_final:
        return False, "evaluation_final_score_mismatch"
    if settlement.get("final_score") != canonical_final:
        return False, "settlement_final_score_mismatch"

    if eval_row.get("actual_outcome") != canonical_outcome:
        return False, "evaluation_actual_outcome_mismatch"
    if settlement.get("actual_outcome") != canonical_outcome:
        return False, "settlement_actual_outcome_mismatch"

    # Result Fingerprint
    expected_fingerprint = result_fingerprint(canonical_final)
    if settlement.get("result_fingerprint") != expected_fingerprint:
        return False, "settlement_result_fingerprint_mismatch"
    if eval_row.get("result_fingerprint") != expected_fingerprint:
        return False, "evaluation_result_fingerprint_mismatch"
    if eval_row.get("result_fingerprint") != settlement.get("result_fingerprint"):
        return False, "result_fingerprint_mismatch"

    # 7. Authoritative Classification Validation
    if not _validate_probabilities_and_classes(eval_row, m_sport):
        return False, "classification_validation_failed"

    return True, ""


def build_history_view_data(
    matches: list[dict],
    evaluation_rows: list[dict] | None = None,
    sport: str | None = None,
    max_display: int = 200,
    *,
    snapshots: dict[str, dict] | list[dict] | None = None,
    settlements: dict[str, dict] | list[dict] | None = None,
) -> dict:
    """Build immutable evidence view model for /history route.

    Args:
        matches: Raw matches (typically from get_matches_by_date("all", sport)).
        evaluation_rows: Optional evaluation rows (defaults to get_all_evaluation_rows()).
        sport: Active sport filter ('football', 'basketball', or None/'all').
        max_display: Maximum rows to display (default 200).
        snapshots: Optional snapshots mapping/list (defaults to loading store once).
        settlements: Optional settlements mapping/list (defaults to loading store once).

    Returns:
        Structured dict containing visible matches and reconciled statistics.
    """
    if evaluation_rows is None:
        evaluation_rows = get_all_evaluation_rows()

    # Load stores once (batched read-only lookups)
    if snapshots is None:
        snapshots_dict = load_snapshots_store().get("snapshots", {})
    elif isinstance(snapshots, list):
        snapshots_dict = {
            s.get("snapshot_id"): s for s in snapshots if isinstance(s, dict) and s.get("snapshot_id")
        }
    else:
        snapshots_dict = snapshots

    if settlements is None:
        settlements_dict = load_settlements_store().get("settlements", {})
    elif isinstance(settlements, list):
        settlements_dict = {
            s.get("settlement_id"): s for s in settlements if isinstance(s, dict) and s.get("settlement_id")
        }
    else:
        settlements_dict = settlements

    # 1. Index evaluation rows by match_id
    evals_by_match: dict[str, list[dict]] = {}
    for r in evaluation_rows:
        if isinstance(r, dict):
            mid = r.get("match_id")
            if mid:
                evals_by_match.setdefault(mid, []).append(r)

    # 2. Filter finished matches
    finished = [
        m for m in matches
        if isinstance(m, dict) and m.get("status") == "finished" and m.get("score")
    ]
    # Fully deterministic reverse sort by (date, time, id)
    finished.sort(
        key=lambda x: (
            x.get("date", ""),
            x.get("time", ""),
            x.get("id", ""),
        ),
        reverse=True,
    )

    visible_matches = finished[:max_display]

    processed_matches = []
    for m in visible_matches:
        m_id = m.get("id", "")
        m_sport = m.get("sport", "football")
        score_obj = m.get("score")
        actual_score_str = _format_score_str(score_obj)
        derived_actual = _derive_actual_outcome(score_obj, m_sport)

        m_evals = evals_by_match.get(m_id, [])

        # Check for duplicate evaluation identities and content conflicts
        evidence_status = "no_evidence"
        selected_eval = None

        if len(m_evals) > 1:
            distinct_eids = {
                r.get("evaluation_id")
                for r in m_evals
                if isinstance(r, dict) and r.get("evaluation_id")
            }
            if len(distinct_eids) > 1:
                # Multiple distinct evaluation identities for one match -> ambiguous
                evidence_status = "ambiguous"
            else:
                first = m_evals[0]
                first_repr = json.dumps(first, sort_keys=True, ensure_ascii=False) if isinstance(first, dict) else ""
                has_payload_conflict = any(
                    json.dumps(r, sort_keys=True, ensure_ascii=False) != first_repr
                    for r in m_evals[1:]
                )
                if has_payload_conflict:
                    # Same evaluation ID but conflicting content -> ambiguous (fail closed)
                    evidence_status = "ambiguous"
                else:
                    # Strict complete record equivalence: identical copies
                    selected_eval = first
        elif len(m_evals) == 1:
            selected_eval = m_evals[0]
        else:
            evidence_status = "no_evidence"

        if selected_eval is not None:
            curr_version = config.MODEL_VERSIONS.get(m_sport, "")
            row_version = selected_eval.get("model_version", "")

            if row_version == curr_version:
                is_qual, _ = qualify_current_version_evaluation(
                    m, selected_eval, snapshots_dict, settlements_dict
                )
                if is_qual:
                    evidence_status = "verified"
                else:
                    evidence_status = "invalid"
            else:
                evidence_status = "historical_version"

        comparison = {
            "actual_score": actual_score_str,
            "actual_outcome": derived_actual,
            "actual_label": OUTCOME_LABELS.get(derived_actual, derived_actual),
            "evidence_status": evidence_status,
            "is_verified": (evidence_status == "verified"),
            "model_name": "",
            "model_version": "",
            "prediction_generated_at": "",
            "kickoff_at": m.get("time", ""),
            "predicted_outcome": "",
            "predicted_label": "",
            "confidence": None,
            "correct": None,
            "score_eligible": False,
            "score_hit": None,
            "top_candidates": [],
        }

        if evidence_status == "verified" and selected_eval:
            version = selected_eval.get("model_version", "")
            comparison["model_name"] = selected_eval.get("model_name", "")
            comparison["model_version"] = version
            comparison["prediction_generated_at"] = selected_eval.get("prediction_generated_at", "")
            comparison["kickoff_at"] = selected_eval.get("kickoff_at", "")

            # Prematch predicted outcome: deterministic argmax from model_probabilities
            probs = selected_eval.get("model_probabilities") or {}
            classes = classes_for_sport(m_sport)
            pred_out = max(classes, key=lambda k: probs.get(k, 0))
            comparison["predicted_outcome"] = pred_out
            comparison["predicted_label"] = OUTCOME_LABELS.get(pred_out, pred_out)
            comparison["confidence"] = probs.get(pred_out, 0)

            eval_actual = selected_eval.get("actual_outcome") or derived_actual
            comparison["actual_outcome"] = eval_actual
            comparison["actual_label"] = OUTCOME_LABELS.get(eval_actual, eval_actual)
            comparison["correct"] = (pred_out == eval_actual)

            # Football Top-3 score candidate coverage
            if m_sport == "football":
                esd = selected_eval.get("expected_score_data") or {}
                top_scores = esd.get("top_scores")
                fs = selected_eval.get("final_score") or {}
                if "home" in fs and "away" in fs:
                    eval_score_str = f"{fs['home']}-{fs['away']}"
                else:
                    eval_score_str = actual_score_str

                if isinstance(top_scores, list) and len(top_scores) > 0 and eval_score_str:
                    cands = [
                        s.get("score") for s in top_scores
                        if isinstance(s, dict) and s.get("score")
                    ]
                    if cands:
                        comparison["score_eligible"] = True
                        comparison["top_candidates"] = cands
                        comparison["score_hit"] = (eval_score_str in cands)

        elif evidence_status == "historical_version" and selected_eval:
            version = selected_eval.get("model_version", "")
            comparison["model_name"] = selected_eval.get("model_name", "")
            comparison["model_version"] = version
            comparison["prediction_generated_at"] = selected_eval.get("prediction_generated_at", "")
            comparison["kickoff_at"] = selected_eval.get("kickoff_at", "")

            probs = selected_eval.get("model_probabilities") or {}
            classes = FOOTBALL_CLASSES if m_sport == "football" else BASKETBALL_CLASSES
            if probs:
                pred_out = max(classes, key=lambda k: probs.get(k, 0))
                comparison["predicted_outcome"] = pred_out
                comparison["predicted_label"] = OUTCOME_LABELS.get(pred_out, pred_out)
                comparison["confidence"] = probs.get(pred_out, 0)
                eval_actual = selected_eval.get("actual_outcome") or derived_actual
                if eval_actual:
                    comparison["correct"] = (pred_out == eval_actual)

        m_copy = dict(m)
        m_copy["comparison"] = comparison
        m_copy["evidence_status"] = comparison["evidence_status"]
        m_copy["is_verified"] = comparison["is_verified"]
        processed_matches.append(m_copy)

    # 3. Calculate Reconciled Summary Metrics over visible matches
    stats = _compute_stats(processed_matches, sport)

    return {
        "matches": processed_matches,
        "total_finished": len(finished),
        "visible_count": len(visible_matches),
        "current_sport": sport or "all",
        "stats": stats,
    }


def _compute_stats(visible_matches: list[dict], current_sport: str | None) -> dict:
    total_finished = len(visible_matches)
    verified = [m for m in visible_matches if m["comparison"]["evidence_status"] == "verified"]
    historical_v = [m for m in visible_matches if m["comparison"]["evidence_status"] == "historical_version"]
    no_evidence = [m for m in visible_matches if m["comparison"]["evidence_status"] == "no_evidence"]
    ambiguous_invalid = [m for m in visible_matches if m["comparison"]["evidence_status"] in ("ambiguous", "invalid")]

    def _calc_sport_stats(sport_name: str) -> dict:
        sp_matches = [m for m in visible_matches if m.get("sport") == sport_name]
        sp_verified = [m for m in sp_matches if m["comparison"]["evidence_status"] == "verified"]
        sp_historical = [m for m in sp_matches if m["comparison"]["evidence_status"] == "historical_version"]
        sp_no_evidence = [m for m in sp_matches if m["comparison"]["evidence_status"] == "no_evidence"]
        sp_ambiguous_invalid = [m for m in sp_matches if m["comparison"]["evidence_status"] in ("ambiguous", "invalid")]

        total_sp = len(sp_matches)
        v_count = len(sp_verified)
        hits = sum(1 for m in sp_verified if m["comparison"]["correct"])

        acc_pct = (hits / v_count * 100.0) if v_count > 0 else None
        acc_str = f"{acc_pct:.1f}%" if acc_pct is not None else "暂无"
        ratio_str = f"{hits}/{v_count}" if v_count > 0 else "0/0"

        # Football Top-3 score coverage
        score_eligible_matches = [m for m in sp_verified if m["comparison"]["score_eligible"]]
        sc_eligible = len(score_eligible_matches)
        sc_hits = sum(1 for m in score_eligible_matches if m["comparison"]["score_hit"])
        sc_cov_pct = (sc_hits / sc_eligible * 100.0) if sc_eligible > 0 else None
        sc_cov_str = f"{sc_cov_pct:.1f}%" if sc_cov_pct is not None else "暂无"
        sc_ratio_str = f"{sc_hits}/{sc_eligible}" if sc_eligible > 0 else "0/0"

        return {
            "sport": sport_name,
            "total_finished": total_sp,
            "verified_evaluations": v_count,
            "historical_evaluations": len(sp_historical),
            "no_evidence_matches": len(sp_no_evidence),
            "ambiguous_evaluations": len(sp_ambiguous_invalid),
            "unverified_matches": total_sp - v_count,
            "verified_hits": hits,
            "accuracy_pct": acc_pct,
            "accuracy_str": acc_str,
            "ratio_str": ratio_str,
            "score_eligible": sc_eligible,
            "score_hits": sc_hits,
            "score_coverage_pct": sc_cov_pct,
            "score_coverage_str": sc_cov_str,
            "score_ratio_str": sc_ratio_str,
            "model_version": config.MODEL_VERSIONS.get(sport_name, ""),
        }

    football_stats = _calc_sport_stats("football")
    basketball_stats = _calc_sport_stats("basketball")

    # Scope-specific active stats
    if current_sport == "football":
        scope_stats = football_stats
    elif current_sport == "basketball":
        scope_stats = basketball_stats
    else:
        scope_stats = {
            "sport": "all",
            "total_finished": total_finished,
            "verified_evaluations": len(verified),
            "historical_evaluations": len(historical_v),
            "no_evidence_matches": len(no_evidence),
            "ambiguous_evaluations": len(ambiguous_invalid),
            "unverified_matches": total_finished - len(verified),
        }

    return {
        "total_finished": total_finished,
        "verified_evaluations": len(verified),
        "historical_version_evaluations": len(historical_v),
        "no_evidence_matches": len(no_evidence),
        "ambiguous_evaluations": len(ambiguous_invalid),
        "unverified_matches": total_finished - len(verified),
        "football": football_stats,
        "basketball": basketball_stats,
        "scope": scope_stats,
    }
