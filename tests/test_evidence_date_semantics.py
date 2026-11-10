"""
Evidence date-semantics regressions.

SYNTHETIC FIXTURES ONLY. These reproduce a comparison defect in
``resolve_orientation``: the candidate date is a Beijing calendar date while an
independent NBA source keeps its original US local game date. A raw string
comparison rejected a legitimately converted same-fixture record.

No fixture here is operator evidence and none is an acceptance claim.
"""
from __future__ import annotations

import sys

import pytest

ROOT = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import nba_human_review_workbench as wb  # noqa: E402

CANDIDATE = {
    "candidate_id": "NXD-DATE-SEMANTICS-FIXTURE",
    "event_date": "2026-10-28",  # Beijing date from daily_matches.date
    "team_names": {
        "500j-2026-10-28-2307": {"home": "纽约尼克斯", "away": "底特律活塞"},
        "500lq-2026-10-28-周二307": {"home": "底特律活塞", "away": "纽约尼克斯"},
    },
}

CONVERSION = {
    "source_utc_offset": "-04:00",  # EDT, independently attested
    "event_start_local": "2026-10-27T20:00:00",
    "event_start_utc": "2026-10-28T00:00:00Z",
    "beijing_event_date": "2026-10-28",
    "local_time_semantics": "UNAMBIGUOUS",
}


def _item(**overrides):
    base = {
        "evidence_id": "EV-1",
        "candidate_id": CANDIDATE["candidate_id"],
        "reference_identifier": "NBA_OFFICIAL_GAME_ID:0022600129",
        "publisher": "NBA.com",
        "source_reference": "https://www.nba.com/game/0022600129/box-score",
        "retrieved_at": "2026-11-10T15:00:00+08:00",
        "actual_event_date": "2026-10-27",  # original NBA local date
        "away_team": "底特律活塞",
        "home_team": "纽约尼克斯",
        "event_identifier": "0022600129",
        "excerpt": "DET 97 Final 111 NYK, Tue Oct 27 2026, Madison Square Garden",
        "checked_by": "OPERATOR-FIXTURE",
        "verification_status": wb.STATUS_SOURCE_REFERENCE_REVIEWED,
        "contradictory_references": [],
    }
    base.update(overrides)
    return base


def _resolve(item):
    return wb.resolve_orientation(
        CANDIDATE, {"schema_version": "1.0.0", "evidence": [item], "rejected": []}
    )


# 1. Same local and Beijing calendar date ------------------------------------

def test_ds_01_same_local_and_beijing_date():
    ok, reason = wb.event_date_compatible({"actual_event_date": "2026-10-28"}, CANDIDATE)
    assert ok and reason == "DATE_EQUAL"
    assert _resolve(_item(actual_event_date="2026-10-28"))["status"] == wb.STATUS_INDEPENDENTLY_SUPPORTED


# 2. Different local/Beijing dates, same confirmed fixture -------------------

def test_ds_02_different_dates_same_fixture_with_verified_conversion():
    item = _item(**CONVERSION)
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert ok and reason == "VERIFIED_TIMEZONE_CONVERSION"

    result = _resolve(item)
    assert result["status"] == wb.STATUS_INDEPENDENTLY_SUPPORTED
    assert result["matched_count"] == 1
    assert result["date_semantics"] == {"EV-1": "VERIFIED_TIMEZONE_CONVERSION"}
    assert result["unmatched"] == []


# 3. Genuine different-event date conflict -----------------------------------

def test_ds_03_genuine_different_event_date_is_rejected():
    item = _item(**{
        **CONVERSION,
        "actual_event_date": "2026-10-26",
        "event_start_local": "2026-10-26T20:00:00",
        "event_start_utc": "2026-10-27T00:00:00Z",
    })
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert not ok
    assert reason in ("BEIJING_DATE_MISMATCH", "DECLARED_BEIJING_DATE_MISMATCH",
                      "ORIGINAL_LOCAL_DATE_NOT_PRESERVED")
    assert _resolve(item)["status"] == wb.STATUS_UNVERIFIED


# 4. Missing source time zone / conversion basis ------------------------------

@pytest.mark.parametrize("missing", [
    "source_utc_offset", "event_start_local", "event_start_utc",
    "beijing_event_date", "local_time_semantics",
])
def test_ds_04_missing_source_timezone_fails_closed(missing):
    basis = {k: v for k, v in CONVERSION.items() if k != missing}
    item = _item(**basis)
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert not ok, missing
    assert _resolve(item)["status"] == wb.STATUS_UNVERIFIED


def test_ds_04b_no_conversion_basis_at_all_fails_closed():
    ok, reason = wb.event_date_compatible({"actual_event_date": "2026-10-27"}, CANDIDATE)
    assert not ok and reason.startswith(("LOCAL_TIME_", "CONVERSION_BASIS_"))


# 5. Ambiguous or nonexistent local time --------------------------------------

@pytest.mark.parametrize("semantics", ["AMBIGUOUS", "NONEXISTENT", "UNKNOWN", ""])
def test_ds_05_ambiguous_or_nonexistent_local_time_rejected(semantics):
    item = _item(**{**CONVERSION, "local_time_semantics": semantics})
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert not ok and reason == "LOCAL_TIME_AMBIGUOUS_OR_UNDECLARED"


def test_ds_05b_inconsistent_offset_at_dst_boundary_rejected():
    # A folded/duplicated local hour would produce a different UTC instant.
    item = _item(**{**CONVERSION, "source_utc_offset": "-05:00"})
    assert wb.event_date_compatible(item, CANDIDATE)[1] == "CONVERSION_INCONSISTENT"


# 6. Unsupported conversion ---------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"source_utc_offset": "EDT"},
    {"source_utc_offset": "-04:60"},
    {"source_utc_offset": "-15:00"},
    {"event_start_local": "2026-10-27T20:00:00-04:00"},  # not naive
    {"event_start_utc": "2026-10-28T00:00:00"},          # not aware UTC
    {"event_start_utc": "2026-10-28T00:00:00+02:00"},    # non-UTC offset
    {"event_start_local": "not-a-timestamp"},
])
def test_ds_06_unsupported_conversion_rejected(overrides):
    item = _item(**{**CONVERSION, **overrides})
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert not ok, overrides
    assert reason in (
        "UNSUPPORTED_UTC_OFFSET", "UNSUPPORTED_LOCAL_TIMESTAMP",
        "UNSUPPORTED_UTC_TIMESTAMP", "CONVERSION_INCONSISTENT",
        "LOCAL_TIME_AMBIGUOUS_OR_UNDECLARED",
    )


def test_ds_06b_declared_beijing_date_mismatch_rejected():
    item = _item(**{**CONVERSION, "beijing_event_date": "2026-10-27"})
    assert wb.event_date_compatible(item, CANDIDATE)[1] == "DECLARED_BEIJING_DATE_MISMATCH"


# 7. Original source date preserved ------------------------------------------

def test_ds_07_original_source_date_is_preserved():
    item = _item(**CONVERSION)
    before = dict(item)
    ok, _ = wb.event_date_compatible(item, CANDIDATE)
    assert ok
    assert item == before, "the conversion check must not mutate the evidence item"
    assert item["actual_event_date"] == "2026-10-27"
    assert item["source_utc_offset"] == "-04:00"
    assert item["event_start_local"] == "2026-10-27T20:00:00"


def test_ds_07b_original_date_original_mismatch_rejected():
    # Keep the original local date, but make the local timestamp disagree with it.
    item = _item(**{**CONVERSION, "event_start_local": "2026-10-26T20:00:00"})
    assert wb.event_date_compatible(item, CANDIDATE)[1] == "ORIGINAL_LOCAL_DATE_NOT_PRESERVED"


# 8. No automatic Canonical reconciliation ------------------------------------

def test_ds_08_no_automatic_canonical_reconciliation():
    result = _resolve(_item(**CONVERSION))
    assert result["authoritative"] is False
    assert result["cross_source_identity"] == wb.STATUS_INDEPENDENTLY_SUPPORTED
    # These are *candidate-level* orientation labels, never production approval.
    for key in ("approved", "signature", "apply", "merge", "delete",
                "surviving_canonical_id", "production_authorized"):
        assert key not in result


def test_ds_08b_supplied_evidence_never_becomes_reviewed():
    supplied = _item(verification_status=wb.STATUS_SOURCE_REFERENCE_SUPPLIED,
                     checked_by="", **CONVERSION)
    result = _resolve(supplied)
    assert result["reviewed_count"] == 0
    assert result["supplied_count"] == 1
    assert result["status"] == wb.STATUS_UNVERIFIED


# Backwards compatibility -----------------------------------------------------

def test_ds_09_equal_dates_do_not_require_conversion_fields():
    item = _item(actual_event_date="2026-10-28")
    for key in CONVERSION:
        assert key not in item
    ok, reason = wb.event_date_compatible(item, CANDIDATE)
    assert ok and reason == "DATE_EQUAL"


def test_ds_10_existing_load_evidence_schema_unchanged():
    assert "source_utc_offset" not in wb.EVIDENCE_REQUIRED_KEYS
    assert "event_start_local" not in wb.EVIDENCE_REQUIRED_KEYS
    assert "beijing_event_date" not in wb.EVIDENCE_REQUIRED_KEYS
    assert "local_time_semantics" not in wb.EVIDENCE_REQUIRED_KEYS
