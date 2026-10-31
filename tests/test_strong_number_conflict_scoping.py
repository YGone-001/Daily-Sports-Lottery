"""
tests/test_strong_number_conflict_scoping.py
============================================
Strong-number conflict detection must be scoped to **one source event identity**,
not to a source stability scope.

The defect: ``detect_strong_number_conflict`` grouped observations by
``stability_scope.as_key()`` (adapter × sport × date × namespace × raw_identifier_type),
which is a source *domain* shared by unrelated matches. Every independent match on the
same day therefore looked like "one identity claiming mutually exclusive numbers".
A second, opposite defect came from the ``raw_identifier_type`` dimension: two
observations of the *same* source ID could land in different groups, so a genuine
contradiction was **missed**.

Business metrics are unaffected by these tests: they verify code behaviour only, and
the real-dataset acceptance result lives separately (see ``docs/analysis/``).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from tools.real_sports_identity_audit import (
    compute_audit,
    deterministic_view,
    load_dataset,
    verify_protected_stores,
)
from utils.mapping_governance import (
    MappingState,
    ProposalAction,
    governance_rule_for,
    proposal_for_conflict,
)
from utils.source_identity import (
    CONFLICT_CANONICAL_OWNER_CONFLICT,
    CONFLICT_CROSS_SPORT_NAMESPACE,
    CONFLICT_ROW_POSITION_REUSE,
    CONFLICT_STRONG_NUMBER_CONFLICT,
    SEVERITY_REJECT,
    detect_all_conflicts,
    detect_strong_number_conflict,
    observe_source_row,
    strong_number_identity_key,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTECTED_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def row(
    match_id: str | None,
    *,
    sport: str = "football",
    date: str = "2030-05-01",
    number: str | None = None,
    home: str = "主队A",
    away: str = "客队B",
    adapter: str = "live_football",
    time: str = "20:00",
    canonical_match_id: str | None = None,
):
    payload = {
        "id": match_id,
        "sport": sport,
        "league": "测试联赛",
        "date": date,
        "time": time,
        "home": home,
        "away": away,
        "status": "upcoming",
        "jczq_no": number if number is not None else "",
        "score": None,
        "odds": None,
    }
    if match_id is None:
        payload.pop("id")
    return observe_source_row(
        payload, adapter=adapter, canonical_match_id=canonical_match_id
    )


def strong_conflicts(observations):
    return detect_strong_number_conflict(observations)


def categories(observations):
    return sorted({conflict.category for conflict in detect_all_conflicts(observations)})


def write_dataset(root, matches, **overrides):
    root = str(root)
    os.makedirs(root, exist_ok=True)
    payloads = {
        "daily_matches.json": {"matches": list(matches), "meta": {}},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": {"snapshots": dict(overrides.get("predictions") or {}), "version": 1},
        "odds_snapshots.json": {"snapshots": dict(overrides.get("odds") or {}), "version": 1},
        "settlements.json": {"settlements": dict(overrides.get("settlements") or {}), "version": 1},
        "evaluation_rows.json": {"rows": dict(overrides.get("rows") or {}), "version": 1},
    }
    for name, payload in payloads.items():
        with open(os.path.join(root, name), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return root


def store_hashes(root):
    out = {}
    for name in PROTECTED_STORES:
        path = os.path.join(str(root), name)
        with open(path, "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def canonical(match_id, **overrides):
    record = {
        "id": match_id,
        "sport": "football",
        "league": "测试联赛",
        "date": "2030-05-01",
        "time": "20:00",
        "home": "主队A",
        "away": "客队B",
        "status": "upcoming",
        "jczq_no": "",
        "score": None,
        "odds": None,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# SN-01 / SN-02: distinct identities inside one domain must not conflict
# ---------------------------------------------------------------------------

def test_sn_01_distinct_source_ids_in_the_same_scope_do_not_conflict():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-002", number="002", home="主队C", away="客队D"),
    ]
    # The two rows genuinely share one stability scope...
    assert observations[0].stability_scope.as_key() == observations[1].stability_scope.as_key()
    # ...but they are two distinct source identities.
    assert strong_number_identity_key(observations[0]) != strong_number_identity_key(observations[1])
    assert observations[0].derived_source_id != observations[1].derived_source_id

    assert strong_conflicts(observations) == ()
    assert CONFLICT_STRONG_NUMBER_CONFLICT not in categories(observations)


def test_sn_02_multiple_numbers_within_one_date_domain_do_not_produce_false_reject():
    observations = [
        row(f"500l-2030-05-01-{index:03d}", number=f"{index:03d}",
            home=f"主队{index}", away=f"客队{index}")
        for index in range(1, 9)
    ]
    assert {o.stability_scope.as_key() for o in observations} == {
        observations[0].stability_scope.as_key()
    }
    assert len({o.competition_number_normalized for o in observations}) == 8
    assert strong_conflicts(observations) == ()
    assert detect_all_conflicts(observations) == ()


def test_grouping_key_is_not_the_stability_scope():
    """Structural guard: the domain key may not be used as the identity key."""
    import ast

    source = open(
        os.path.join(ROOT, "utils", "source_identity.py"), encoding="utf-8"
    ).read()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "detect_strong_number_conflict"
    )
    called = {ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call)}
    # Prose in the docstring may name the rejected key; executable code may not use it.
    assert not any("stability_scope" in name and "as_key" in name for name in called), called
    assert any("strong_number_identity_key" in name for name in called), called


# ---------------------------------------------------------------------------
# SN-03 / SN-04: one identity that contradicts itself still fails closed
# ---------------------------------------------------------------------------

def test_sn_03_same_source_id_with_incompatible_numbers_still_rejects():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-001", number="002", home="主队C", away="客队D"),
    ]
    confirmed = strong_conflicts(observations)
    assert len(confirmed) == 1
    conflict = confirmed[0]
    assert conflict.category == CONFLICT_STRONG_NUMBER_CONFLICT
    assert conflict.severity == SEVERITY_REJECT
    assert conflict.subject == "500l-2030-05-01-001"
    assert dict(conflict.evidence)["competition_numbers"] == "1,2"
    assert dict(conflict.evidence)["derived_source_id"] == "500l-2030-05-01-001"
    assert len(conflict.observation_ids) == 2


def test_sn_03b_raw_identifier_type_must_not_split_one_identity():
    """
    The same ID whose suffix matches its number in one row and not in the other
    must still be treated as one identity (this was the missed-detection case).
    """
    matching = row("500l-2030-05-01-001", number="001")
    mismatching = row("500l-2030-05-01-001", number="002", home="主队C", away="客队D")
    assert matching.stability_scope.raw_identifier_type != mismatching.stability_scope.raw_identifier_type
    assert strong_number_identity_key(matching) == strong_number_identity_key(mismatching)
    assert len(strong_conflicts([matching, mismatching])) == 1


def test_sn_04_same_source_id_with_identical_number_does_not_conflict():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-001", number="001", home="主队C", away="客队D"),
    ]
    assert strong_conflicts(observations) == ()
    assert CONFLICT_STRONG_NUMBER_CONFLICT not in categories(observations)


# ---------------------------------------------------------------------------
# SN-05 / SN-06: no invented identity, no false certainty
# ---------------------------------------------------------------------------

def test_sn_05_missing_source_id_never_fabricates_a_strong_collision():
    observations = [
        row(None, number="001"),
        row(None, number="002", home="主队C", away="客队D"),
    ]
    assert [o.derived_source_id for o in observations] == [None, None]
    assert all(strong_number_identity_key(o) is None for o in observations)
    assert strong_conflicts(observations) == ()
    assert observations[0].identity_stability_reason


def test_sn_06_missing_competition_number_is_handled_without_false_certainty():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-001", number=None, home="主队C", away="客队D"),
    ]
    assert [o.competition_number_normalized for o in observations] == ["1", None]
    assert strong_conflicts(observations) == ()


# ---------------------------------------------------------------------------
# SN-07 / SN-08: sport separation and cross-sport governance stay independent
# ---------------------------------------------------------------------------

def test_sn_07_football_and_basketball_identity_separation_is_preserved():
    observations = [
        row("500j-2030-05-01-001", sport="football", number="001", adapter="market_xml"),
        row("500j-2030-05-01-001", sport="basketball", number="001",
            adapter="market_xml", home="主队C", away="客队D"),
    ]
    assert strong_number_identity_key(observations[0]) != strong_number_identity_key(observations[1])
    assert strong_conflicts(observations) == ()


def test_sn_08_cross_sport_reused_derived_id_stays_governed_separately():
    observations = [
        row("500j-2030-05-01-001", sport="football", number="001", adapter="market_xml"),
        row("500j-2030-05-01-001", sport="basketball", number="002",
            adapter="market_xml", home="主队C", away="客队D"),
    ]
    conflicts = detect_all_conflicts(observations)
    # The literal string is reused across sports: that is a namespace collision,
    # never a strong-number contradiction of one identity.
    assert [c.category for c in conflicts] == [CONFLICT_CROSS_SPORT_NAMESPACE]
    assert conflicts[0].severity == "AMBIGUOUS"
    assert conflicts[0].subject == "500j-2030-05-01-001"


# ---------------------------------------------------------------------------
# SN-09 / SN-10: unrelated detectors keep their semantics
# ---------------------------------------------------------------------------

def test_sn_09_row_position_reuse_detection_is_preserved():
    observations = [
        row("500w-2030-05-01-0", number=None, adapter="finished_feed", time="20:00"),
        row("500w-2030-05-01-0", number=None, adapter="finished_feed", time="22:00",
            home="主队C", away="客队D"),
    ]
    assert observations[0].is_row_position_unstable
    conflicts = detect_all_conflicts(observations)
    assert [c.category for c in conflicts] == [CONFLICT_ROW_POSITION_REUSE]
    assert conflicts[0].severity == "AMBIGUOUS"
    assert strong_conflicts(observations) == ()


def test_sn_10_canonical_owner_conflict_detection_is_preserved():
    observations = [
        row("500l-2030-05-01-001", number="001", canonical_match_id="canon-A"),
        row("500l-2030-05-01-001", number="001", home="主队C", away="客队D",
            canonical_match_id="canon-B"),
    ]
    conflicts = detect_all_conflicts(observations)
    assert [c.category for c in conflicts] == [CONFLICT_CANONICAL_OWNER_CONFLICT]
    assert conflicts[0].severity == "AMBIGUOUS"
    assert strong_conflicts(observations) == ()


# ---------------------------------------------------------------------------
# SN-11 / SN-12 / SN-13: determinism and attributable evidence
# ---------------------------------------------------------------------------

def test_sn_11_observation_order_cannot_change_the_conflict_result():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-002", number="002", home="主队C", away="客队D"),
        row("500l-2030-05-01-001", number="003", home="主队E", away="客队F"),
    ]

    def triplets(items):
        return [
            (c.category, c.subject, c.severity, c.observation_ids)
            for c in detect_all_conflicts(items)
        ]

    forward = triplets(observations)
    backward = triplets(list(reversed(observations)))
    shuffled = triplets([observations[1], observations[0], observations[2]])
    assert forward == backward == shuffled
    assert len(forward) == 1


def test_sn_12_repeated_input_does_not_duplicate_conflict_objects():
    first = row("500l-2030-05-01-001", number="001")
    second = row("500l-2030-05-01-001", number="002", home="主队C", away="客队D")
    observations = [first, second, first, second]
    conflicts = strong_conflicts(observations)
    assert len(conflicts) == 1
    assert len(conflicts[0].observation_ids) == len(set(conflicts[0].observation_ids))
    assert dict(conflicts[0].evidence)["observation_count"] == "2"


def test_sn_13_conflict_observation_ids_are_attributable_and_deterministic():
    first = row("500l-2030-05-01-001", number="001")
    second = row("500l-2030-05-01-001", number="002", home="主队C", away="客队D")
    conflict = strong_conflicts([first, second])[0]
    expected = tuple(sorted({first.observation_id, second.observation_id}))
    assert conflict.observation_ids == expected
    assert all(oid == oid.lower() and len(oid) == 64 for oid in conflict.observation_ids)
    payload = conflict.as_dict()
    assert payload["evidence"]["derived_source_id"] == "500l-2030-05-01-001"
    assert payload["evidence"]["sport"] == "football"
    assert json.dumps(payload, sort_keys=True) == json.dumps(conflict.as_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# SN-14: governance still rejects a genuine contradiction
# ---------------------------------------------------------------------------

def test_sn_14_governance_reject_policy_remains_intact():
    observations = [
        row("500l-2030-05-01-001", number="001"),
        row("500l-2030-05-01-001", number="002", home="主队C", away="客队D"),
    ]
    conflict = strong_conflicts(observations)[0]
    rule = governance_rule_for(conflict.category)
    assert rule is not None
    assert rule["target_state"] == MappingState.REJECTED.value
    assert rule["requested_action"] == ProposalAction.REJECT_IDENTITY_CLAIM.value

    proposal = proposal_for_conflict(
        conflict,
        observations,
        ingestion_plan_reference="planner:REJECTED",
        expected_baseline_fingerprint="baseline-A",
    )
    assert proposal.status == MappingState.PROPOSED.value
    assert proposal.is_binding_action is False
    assert proposal.requested_action != ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value


# ---------------------------------------------------------------------------
# SN-15 / SN-16: audit tool stays internally consistent; downstream linkage intact
# ---------------------------------------------------------------------------

def test_sn_15_real_data_audit_metrics_remain_internally_consistent(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            canonical("500l-2030-05-01-001", jczq_no="001",
                      odds={"home_win": 2.0, "draw": 3.3, "away_win": 3.4, "matchnum": "001"}),
            canonical("500l-2030-05-01-002", jczq_no="002", home="主队C", away="客队D"),
        ],
    )
    report = compute_audit(load_dataset(root))
    assert report["identity"]["conflict_count"] == 0
    for metric in report["metrics"]:
        if metric["status"].startswith("MEASURED"):
            assert metric["denominator"] and metric["denominator"] > 0
            assert metric["value"] is not None
    assert json.dumps(deterministic_view(report), sort_keys=True, ensure_ascii=False) == (
        json.dumps(deterministic_view(compute_audit(load_dataset(root))),
                   sort_keys=True, ensure_ascii=False)
    )


def test_sn_15b_audit_counts_zero_false_conflicts_on_a_clean_set(tmp_path):
    """
    Two independent matches on one day must yield ``conflict_count == 0``.

    Before the correction this exact shape produced one scope-wide ``REJECT``;
    the Canonical store itself cannot hold two rows with the same ID, so a
    per-identity contradiction can only arrive from a separate observation
    stream — that path is covered by SN-03 at the observation layer.
    """
    root = write_dataset(
        tmp_path,
        [
            canonical("500l-2030-05-01-001", jczq_no="001"),
            canonical("500l-2030-05-01-002", jczq_no="002", home="主队C", away="客队D"),
            canonical("500l-2030-05-01-003", jczq_no="003", home="主队E", away="客队F"),
        ],
    )
    report = compute_audit(load_dataset(root))
    assert report["identity"]["conflict_count"] == 0
    assert report["identity"]["conflict_category_distribution"] == {}

    # The same rows observed as two independent identities still agree.
    observations = [row(f"500l-2030-05-01-00{index}", number=f"00{index}") for index in (1, 2, 3)]
    assert detect_all_conflicts(observations) == ()


def test_sn_16_downstream_identity_and_snapshot_linkage_still_resolve(tmp_path):
    snapshot = {
        "snapshot_id": "ps-1",
        "match_id": "500l-2030-05-01-001",
        "sport": "football",
        "league": "测试联赛",
        "home_team": "主队A",
        "away_team": "客队B",
        "match_date": "2030-05-01",
        "match_time": "20:00",
        "kickoff_at": "2030-05-01T20:00:00+08:00",
        "generated_at": "2030-04-30T12:00:00+08:00",
        "model_version": "test-1",
    }
    settlement = {
        "settlement_id": "st-1",
        "snapshot_id": "ps-1",
        "match_id": "500l-2030-05-01-001",
        "sport": "football",
        "home_team": "主队A",
        "away_team": "客队B",
        "final_score": {"home": 2, "away": 1},
        "actual_outcome": "home_win",
        "result_fingerprint": "placeholder",
        "model_version": "test-1",
    }
    root = write_dataset(
        tmp_path,
        [canonical("500l-2030-05-01-001", jczq_no="001", status="finished", score={"ft": [2, 1]})],
        predictions={"ps-1": snapshot},
        settlements={"st-1": settlement},
    )
    dataset = load_dataset(root)
    report = compute_audit(dataset)
    downstream = report["downstream"]
    assert downstream["distinct_events_with_prediction"] == 1
    assert downstream["distinct_events_with_settlement"] == 1
    assert downstream["orphaned_prediction_snapshots"] == 0
    assert downstream["settlement_snapshot_missing"] == 0
    assert downstream["settlement_snapshot_match_mismatch"] == 0
    assert verify_protected_stores(dataset) == store_hashes(root)


# ---------------------------------------------------------------------------
# SN-17 / SN-18 / SN-19: governance documentation and local report handling
# ---------------------------------------------------------------------------

def test_sn_17_agents_md_records_the_updated_delivery_constraints():
    text = open(os.path.join(ROOT, "AGENTS.md"), encoding="utf-8").read()
    required = [
        "稳定性作用域",           # A: scope vs identity distinction
        "strong_number_identity_key",  # B: grouping contract
        "REJECT",                  # C: genuine contradictions stay fail-closed
        "字节不变",                 # D: protected stores
        "info/exclude",            # E: local reports handling
        "远程 CI 必须先于最终报告完成",  # F: CI-before-report ordering
        "gh run view",             # G: exact-SHA verification
        "git ls-files -- reports/", # H: no tracked report files
    ]
    for needle in required:
        assert needle in text, needle
    assert text.count("## 四、身份语义与冲突口径") == 1
    assert "git add -f reports/" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for the local-exclude checks")
def test_sn_18_reports_directory_is_ignored_locally():
    probe = os.path.join("reports", "_probe.md")
    result = subprocess.run(
        ["git", "check-ignore", "-v", probe],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "/reports/" in result.stdout
    exclude = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "info/exclude"], cwd=ROOT, text=True
    ).strip()
    content = open(os.path.join(ROOT, exclude), encoding="utf-8").read().splitlines()
    assert content.count("/reports/") == 1, "the reports rule must exist exactly once"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for the tracked-file check")
def test_sn_19_no_reports_content_is_tracked():
    tracked = subprocess.check_output(
        ["git", "ls-files", "--", "reports/"], cwd=ROOT, text=True
    ).strip()
    assert tracked == ""
    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only", "--", "reports/"], cwd=ROOT, text=True
    ).strip()
    assert staged == ""


# ---------------------------------------------------------------------------
# SN-20 / SN-21 / SN-22: integrity, no activation path, CI matrix
# ---------------------------------------------------------------------------

def test_sn_20_seven_stores_stay_byte_identical_through_the_corrected_path(tmp_path):
    root = write_dataset(
        tmp_path,
        [
            canonical("500l-2030-05-01-001", jczq_no="001"),
            canonical("500l-2030-05-01-002", jczq_no="002", home="主队C", away="客队D"),
        ],
    )
    before = store_hashes(root)
    dataset = load_dataset(root)
    dataset.matches  # observations are built from the protected records
    compute_audit(dataset)
    assert store_hashes(root) == before
    assert verify_protected_stores(dataset) == before


def test_sn_21_no_production_activation_path_is_introduced():
    from utils.runtime_mapping_policy import runtime_policy_status

    status = runtime_policy_status()
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"
    assert status["production_mapping_write_authorization"] == "NOT_GRANTED"

    source = open(
        os.path.join(ROOT, "utils", "source_identity.py"), encoding="utf-8"
    ).read()
    for forbidden in ("import requests", "import socket", "urllib", "scraper.refresh"):
        assert forbidden not in source


def test_sn_22_ci_matrix_still_requires_ubuntu_and_windows_jobs():
    workflow = open(
        os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8"
    ).read()
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert workflow.count("uses: actions/checkout@") == 2
    for job in ("offline-tests:", "staging-validation:"):
        assert job in workflow