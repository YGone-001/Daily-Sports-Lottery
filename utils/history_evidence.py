"""Historical Review Evidence Presentation Layer.
=============================================
Read-only helpers that derive historical model performance exclusively
from authentic immutable prematch evidence (evaluation rows).

Key Invariants:
- Never calls `predict_match(...)` or recomputes probabilities after kickoff.
- Never mutates Elo, team profiles, snapshots, settlements, or evaluation rows.
- Derives predicted outcomes and confidence solely from stored `model_probabilities`.
- Distinguishes qualified current-version evaluations, historical-version evaluations,
  unverified matches, and ambiguous records.
- Preserves visibility of finished matches even when no prematch snapshot exists,
  while strictly excluding unverified matches from accuracy denominators.
"""
from __future__ import annotations

import config
from utils.evaluation_rows import get_all_evaluation_rows

FOOTBALL_CLASSES = ("home_win", "draw", "away_win")
BASKETBALL_CLASSES = ("home_win", "away_win")

OUTCOME_LABELS = {
    "home_win": "主胜",
    "draw": "平局",
    "away_win": "客胜",
}


def _derive_actual_outcome(ft: list | tuple, sport: str) -> str:
    """Derive outcome from score ft integers strictly."""
    if not (isinstance(ft, (list, tuple)) and len(ft) >= 2):
        return ""
    hg, ag = int(ft[0]), int(ft[1])
    if sport == "football":
        return "home_win" if hg > ag else ("draw" if hg == ag else "away_win")
    return "home_win" if hg > ag else "away_win"


def _format_score_str(score_obj: dict | None) -> str:
    if not isinstance(score_obj, dict):
        return ""
    ft = score_obj.get("ft")
    if isinstance(ft, (list, tuple)) and len(ft) >= 2:
        return f"{ft[0]}-{ft[1]}"
    return ""


def build_history_view_data(
    matches: list[dict],
    evaluation_rows: list[dict] | None = None,
    sport: str | None = None,
    max_display: int = 200,
) -> dict:
    """Build immutable evidence view model for /history route.

    Args:
        matches: Raw matches (typically from get_matches_by_date("all", sport)).
        evaluation_rows: Optional evaluation rows (defaults to get_all_evaluation_rows()).
        sport: Active sport filter ('football', 'basketball', or None/'all').
        max_display: Maximum rows to display (default 200).

    Returns:
        Structured dict containing visible matches and reconciled statistics.
    """
    if evaluation_rows is None:
        evaluation_rows = get_all_evaluation_rows()

    # 1. Index evaluation rows by match_id
    evals_by_match: dict[str, list[dict]] = {}
    for r in evaluation_rows:
        mid = r.get("match_id")
        if mid:
            evals_by_match.setdefault(mid, []).append(r)

    # 2. Filter finished matches
    finished = [
        m for m in matches
        if m.get("status") == "finished" and m.get("score")
    ]
    finished.sort(key=lambda x: (x.get("date", ""), x.get("time", "")), reverse=True)

    visible_matches = finished[:max_display]

    processed_matches = []
    for m in visible_matches:
        m_id = m.get("id", "")
        m_sport = m.get("sport", "football")
        score_obj = m.get("score") or {}
        ft = score_obj.get("ft")
        actual_score_str = _format_score_str(score_obj)
        derived_actual = _derive_actual_outcome(ft, m_sport)

        m_evals = evals_by_match.get(m_id, [])

        # Check for ambiguity
        evidence_status = "no_evidence"
        selected_eval = None

        if len(m_evals) > 1:
            first = m_evals[0]
            conflict = False
            for other in m_evals[1:]:
                if (
                    other.get("model_version") != first.get("model_version")
                    or other.get("actual_outcome") != first.get("actual_outcome")
                    or other.get("model_probabilities") != first.get("model_probabilities")
                ):
                    conflict = True
                    break
            if conflict:
                evidence_status = "ambiguous"
            else:
                selected_eval = first
        elif len(m_evals) == 1:
            selected_eval = m_evals[0]

        comparison = {
            "actual_score": actual_score_str,
            "actual_outcome": derived_actual,
            "actual_label": OUTCOME_LABELS.get(derived_actual, derived_actual),
            "evidence_status": evidence_status,
            "is_verified": False,
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

        if selected_eval:
            version = selected_eval.get("model_version", "")
            curr_version = config.MODEL_VERSIONS.get(m_sport, "")

            is_curr = (version == curr_version)
            comparison["evidence_status"] = "verified" if is_curr else "historical_version"
            comparison["is_verified"] = is_curr
            comparison["model_name"] = selected_eval.get("model_name", "")
            comparison["model_version"] = version
            comparison["prediction_generated_at"] = selected_eval.get("prediction_generated_at", "")
            comparison["kickoff_at"] = selected_eval.get("kickoff_at", "")

            # Prematch predicted outcome: deterministic argmax from model_probabilities
            probs = selected_eval.get("model_probabilities") or {}
            classes = FOOTBALL_CLASSES if m_sport == "football" else BASKETBALL_CLASSES
            if probs:
                pred_out = max(classes, key=lambda k: probs.get(k, 0))
                comparison["predicted_outcome"] = pred_out
                comparison["predicted_label"] = OUTCOME_LABELS.get(pred_out, pred_out)
                comparison["confidence"] = probs.get(pred_out, 0)

            # Stored actual outcome
            eval_actual = selected_eval.get("actual_outcome") or derived_actual
            comparison["actual_outcome"] = eval_actual
            comparison["actual_label"] = OUTCOME_LABELS.get(eval_actual, eval_actual)

            if comparison["predicted_outcome"]:
                comparison["correct"] = (comparison["predicted_outcome"] == eval_actual)

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
    unverified = [m for m in visible_matches if m["comparison"]["evidence_status"] == "no_evidence"]
    ambiguous = [m for m in visible_matches if m["comparison"]["evidence_status"] == "ambiguous"]

    def _calc_sport_stats(sport_name: str) -> dict:
        sp_matches = [m for m in visible_matches if m.get("sport") == sport_name]
        sp_verified = [m for m in sp_matches if m["comparison"]["evidence_status"] == "verified"]
        sp_unverified = [m for m in sp_matches if m["comparison"]["evidence_status"] != "verified"]

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
            "unverified_matches": len(sp_unverified),
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
            "unverified_matches": len(unverified),
            "historical_evaluations": len(historical_v),
            "ambiguous_evaluations": len(ambiguous),
        }

    return {
        "total_finished": total_finished,
        "verified_evaluations": len(verified),
        "historical_version_evaluations": len(historical_v),
        "unverified_matches": len(unverified),
        "ambiguous_evaluations": len(ambiguous),
        "football": football_stats,
        "basketball": basketball_stats,
        "scope": scope_stats,
    }
