"""Historical Review Evidence Presentation Layer.
=============================================
Read-only helpers that derive historical model performance exclusively
from authentic immutable prematch evidence (evaluation rows, settlements,
and prediction snapshots).

Key Invariants:
- Never calls `predict_match(...)` or recomputes probabilities after kickoff.
- Never mutates Elo, team profiles, snapshots, settlements, or evaluation rows.
- Derives predicted outcomes and confidence solely from stored `model_probabilities`.
- Validates the strict unbroken evidence chain:
    canonical match score -> settlement -> prediction snapshot -> evaluation row
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


def _validate_score_and_outcomes(
    match: dict, eval_row: dict, settlement: dict, sport: str
) -> bool:
    """Validate score validity and outcome alignment across match, eval, and settlement."""
    score = match.get("score")
    if not valid_full_time_score(score):
        return False

    canonical_final = extract_final_score(match)
    if canonical_final is None:
        return False

    canonical_outcome = outcome_from_score(canonical_final)

    # Basketball two-class evaluator does not support a draw
    if sport == "basketball" and canonical_outcome == "draw":
        return False

    # Stored final score and outcome agreement
    if eval_row.get("final_score") != canonical_final:
        return False
    if eval_row.get("actual_outcome") != canonical_outcome:
        return False

    if settlement.get("final_score") != canonical_final:
        return False
    if settlement.get("actual_outcome") != canonical_outcome:
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

    Checks:
    1. match_id matches canonical finished match ID
    2. sport matches canonical match sport
    3. model_name and model_version match authoritative current model identity
    4. snapshot_id, settlement_id, evaluation_id match deterministic identity contracts
    5. Referenced snapshot and settlement exist in authoritative stores
    6. Settlement and snapshot reference the same match and snapshot_id
    7. Timestamps parseable, timezone-compatible, ordered as prematch
    8. is_valid_prematch returns True
    9. Stored evaluation probabilities equal original snapshot model_probabilities
    10. Canonical score valid, outcomes agree, basketball tied score excluded
    11. Passes authoritative classification validation
    """
    m_id = str(match.get("id") or "")
    m_sport = str(match.get("sport") or "football")

    # 1. Match ID equals canonical finished match ID
    if eval_row.get("match_id") != m_id:
        return False, "match_id_mismatch"

    # 2. Sport equals canonical match sport
    if eval_row.get("sport") != m_sport:
        return False, "sport_mismatch"

    # 3. Model name and model version match authoritative current model identity
    expected_version = config.MODEL_VERSIONS.get(m_sport, "")
    expected_name = MODEL_NAMES.get(m_sport, "")
    if eval_row.get("model_version") != expected_version:
        return False, "model_version_mismatch"
    if eval_row.get("model_name") != expected_name:
        return False, "model_name_mismatch"

    # 4. Identity contracts
    snap_id = eval_row.get("snapshot_id")
    sett_id = eval_row.get("settlement_id")
    eval_id = eval_row.get("evaluation_id")
    if not (snap_id and sett_id and eval_id):
        return False, "missing_identities"

    if snap_id != snapshot_id_for(m_id, expected_version):
        return False, "snapshot_id_contract_violation"
    if sett_id != settlement_id_for(snap_id):
        return False, "settlement_id_contract_violation"
    if eval_id != evaluation_id_for(snap_id, sett_id):
        return False, "evaluation_id_contract_violation"

    # 5. Referenced immutable snapshot and settlement exist in stores
    snapshot = snapshots.get(snap_id)
    if not snapshot:
        return False, "referenced_snapshot_missing"

    settlement = settlements.get(sett_id)
    if not settlement:
        return False, "referenced_settlement_missing"

    # 6. Cross references
    if settlement.get("snapshot_id") != snap_id or settlement.get("match_id") != m_id:
        return False, "settlement_reference_mismatch"
    if snapshot.get("match_id") != m_id:
        return False, "snapshot_reference_mismatch"

    # 7 & 8. Timestamps
    if not _validate_prematch_timestamps(eval_row, snapshot):
        return False, "invalid_prematch_timestamps"

    # 9. Probabilities equality with original snapshot
    if eval_row.get("model_probabilities") != snapshot.get("model_probabilities"):
        return False, "probabilities_snapshot_mismatch"

    # 10. Canonical score and outcome agreement
    if not _validate_score_and_outcomes(match, eval_row, settlement, m_sport):
        return False, "score_or_outcome_invalid"

    # 11. Authoritative classification validation
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

        # Distinct evaluation identities for this match
        distinct_evals: dict[str, dict] = {}
        for r in m_evals:
            if isinstance(r, dict):
                eid = r.get("evaluation_id")
                if eid and eid not in distinct_evals:
                    distinct_evals[eid] = r

        evidence_status = "no_evidence"
        selected_eval = None

        if len(distinct_evals) > 1:
            # Multiple distinct evaluation identities for one match -> ambiguous (fail closed)
            evidence_status = "ambiguous"
        elif len(distinct_evals) == 1:
            eval_row = list(distinct_evals.values())[0]
            curr_version = config.MODEL_VERSIONS.get(m_sport, "")
            row_version = eval_row.get("model_version", "")

            if row_version == curr_version:
                is_qual, _ = qualify_current_version_evaluation(
                    m, eval_row, snapshots_dict, settlements_dict
                )
                if is_qual:
                    evidence_status = "verified"
                    selected_eval = eval_row
                else:
                    evidence_status = "invalid"
                    selected_eval = eval_row
            else:
                evidence_status = "historical_version"
                selected_eval = eval_row
        else:
            evidence_status = "no_evidence"

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
