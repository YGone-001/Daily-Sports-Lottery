"""
tests/test_canonical_repair_end_to_end.py
=========================================
Phase 1-B -- Isolated Canonical Repair End-to-End Staging Validation.

VALIDATION ONLY.

This suite proves that the hardened production Canonical repair framework can
execute its **complete lifecycle** under controlled conditions:

    prepare -> approve -> Ed25519 verification -> authorization binding
            -> dry-run projection -> controlled apply -> journal state machine
            -> recovery simulation -> postcondition verification

It never repairs production data.  Every artefact lives under a private
temporary directory created outside the repository; ``data/``, ``repair_runs/``
and ``_quiescent_evidence_backup*`` are never touched.

The existing security contract is exercised, not redesigned: Ed25519 owner
approval, external private key, trusted public key verification, signed payload
binding, operator identity binding, the seven-phase journal, the cross-process
lock, atomic replacement, forensic archive, immutable-store verification, crash
recovery and fail-closed behaviour are all validated as-is.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile

import pytest

from tools.production_canonical_repair import (
    APPROVAL_ALGORITHM,
    APPROVAL_FIELD,
    AUTHORIZATION_PURPOSE,
    AUTHORIZATION_SCHEMA_VERSION,
    CANONICAL_FILE,
    DATASET_INTEGRITY_INSPECTION,
    FULL_REPAIR_VERIFICATION,
    JOURNAL_STATES,
    NON_CANONICAL_STORES,
    REPAIR_SCOPE,
    AuthorizationError,
    ImmutableStoreMutationError,
    IncompleteVerificationError,
    PreconditionViolationError,
    ProductionCanonicalRepairer,
    RecoveryStateError,
    SignatureVerificationError,
    build_preparation,
    derive_file_hash,
    fault_injection_enabled,
    generate_signing_keypair,
    inspect_dataset,
    load_authorization,
    load_trusted_public_key,
    read_json_strict,
    sha256_file,
    sign_authorization,
    synthesize_candidate,
    verify_authorization_signature,
    write_json_atomic,
)
from tools.staging_canonical_repair import hash_record

# ===========================================================================
# Fixture identity
# ===========================================================================

FIXTURE_CANONICAL_ID = "500l-test-fixture-001"
FIXTURE_OPERATOR = "phase1b-validator"

#: Records before the collision pair (filler).
_FILLER_BEFORE = 4
#: Records after the collision pair (filler).
_FILLER_AFTER = 3
#: Total source records = 4 + 2 + 3 = 9; projected = 8.
FIXTURE_SOURCE_TOTAL = _FILLER_BEFORE + 2 + _FILLER_AFTER
FIXTURE_PROJECTED_TOTAL = FIXTURE_SOURCE_TOTAL - 1

#: Record A -- inferior / incomplete candidate: live-score alias, abbreviated
#: club names, no terminal result.
FIXTURE_RECORD_A = {
    "id": FIXTURE_CANONICAL_ID,
    "sport": "football",
    "league": "测试联赛",
    "round": "第1轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "upcoming",
    "home": "测试主队简称",
    "away": "测试客队简称",
    "home_rank": 5,
    "away_rank": 9,
    "score": None,
    "odds": {"home_win": 2.1, "draw": 3.3, "away_win": 3.2, "matchnum": "9901"},
    "jczq_no": "9901",
    "market_tracked": True,
    "odds_updated_at": "2026-10-08T20:00:00+08:00",
}

#: Record B -- authoritative finished record: standard club names, valid
#: full-time score, complete metadata.
FIXTURE_RECORD_B = {
    "id": FIXTURE_CANONICAL_ID,
    "sport": "football",
    "league": "测试联赛",
    "round": "第1轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "finished",
    "home": "测试主队完整名称",
    "away": "测试客队完整名称",
    "home_rank": 5,
    "away_rank": 9,
    "score": {"ft": [2, 1], "ht": [1, 0]},
    "odds": None,
    "jczq_no": "",
    "market_tracked": True,
}


def _filler(index: int, bucket: str) -> dict:
    """Deterministic filler record, distinct from the collision pair."""
    return {
        "id": f"500l-test-fixture-{bucket}-{index:03d}",
        "sport": "football",
        "league": "测试联赛",
        "round": "第1轮",
        "date": "2026-10-09",
        "time": "01:00",
        "kickoff_time_known": True,
        "status": "finished",
        "home": f"填充主队{bucket}{index}",
        "away": f"填充客队{bucket}{index}",
        "home_rank": 10 + index,
        "away_rank": 11 + index,
        "score": {"ft": [1, 0], "ht": [0, 0]},
        "odds": None,
        "jczq_no": "",
        "market_tracked": False,
    }


def _immutable_store_payload() -> dict:
    """The six non-Canonical stores; their bytes must never change."""
    return {
        "team_strength.json": {"测试主队完整名称": {"rating": 1600.0}},
        "calibrated.json": {"ids": [FIXTURE_CANONICAL_ID]},
        "prediction_snapshots.json": {"version": 1, "snapshots": {"s1": {"match_id": FIXTURE_CANONICAL_ID}}},
        "odds_snapshots.json": {"version": 1, "snapshots": {"o1": {"match_id": FIXTURE_CANONICAL_ID}}},
        "settlements.json": {"version": 1, "settlements": {"t1": {"match_id": FIXTURE_CANONICAL_ID}}},
        "evaluation_rows.json": {"version": 1, "rows": {"e1": {"match_id": FIXTURE_CANONICAL_ID}}},
    }


class CanonicalFixture:
    """
    Deterministic, isolated Canonical collision fixture.

    Everything is created under a private temporary directory outside the
    repository.  ``close()`` removes it and verifies it is gone, so the suite
    can assert that the fixture is genuinely destroyed.
    """

    def __init__(
        self,
        canonical_id: str = FIXTURE_CANONICAL_ID,
        *,
        target_occurrences: int = 2,
        extra_duplicate_group: bool = False,
        corrupt_final_score: bool = False,
    ):
        self.target_id = canonical_id
        self.target_occurrences = target_occurrences
        self.extra_duplicate_group = extra_duplicate_group
        self.corrupt_final_score = corrupt_final_score

        self.root = tempfile.mkdtemp(prefix="dsl-phase1b-fixture-")
        self.data_dir = os.path.join(self.root, "data")
        self.workspace = os.path.join(self.root, "workspace")
        self.canonical_file = os.path.join(self.data_dir, CANONICAL_FILE)
        self._destroyed = False

        os.makedirs(self.data_dir, exist_ok=True)
        self._write_dataset()
        self.source_sha256 = sha256_file(self.canonical_file)
        self.store_hashes = self.snapshot_stores()

    # -- construction -------------------------------------------------------

    def _write_dataset(self) -> None:
        matches = [_filler(i, "before") for i in range(_FILLER_BEFORE)]

        record_a = copy.deepcopy(FIXTURE_RECORD_A)
        record_a["id"] = self.target_id
        record_b = copy.deepcopy(FIXTURE_RECORD_B)
        record_b["id"] = self.target_id
        if self.corrupt_final_score:
            record_b["score"] = {"ft": [-1, 0]}

        if self.target_occurrences >= 1:
            matches.append(record_a)
        if self.target_occurrences >= 2:
            matches.append(record_b)
        for extra in range(max(0, self.target_occurrences - 2)):
            third = copy.deepcopy(record_b)
            third["score"] = {"ft": [3, 3], "ht": [1, 1]}
            third["home"] = f"第三条记录{extra}"
            matches.append(third)

        if self.extra_duplicate_group:
            matches.append(_filler(0, "dup"))
            matches.append(_filler(1, "dup"))
            matches[-1]["id"] = matches[-2]["id"]

        matches.extend(_filler(i, "after") for i in range(_FILLER_AFTER))

        payload = {
            "schema_version": 3,
            "matches": matches,
            "meta": {
                "total": len(matches),
                "last_refresh": "2026-10-10T18:45:23.927174+08:00",
                "sources": {"live_football": 462, "wanchang": 914},
            },
        }
        write_json_atomic(self.canonical_file, payload)
        for name, data in _immutable_store_payload().items():
            write_json_atomic(os.path.join(self.data_dir, name), data)

    # -- evidence -----------------------------------------------------------

    def snapshot_stores(self) -> dict:
        hashes = {}
        for name in sorted(os.listdir(self.data_dir)):
            path = os.path.join(self.data_dir, name)
            if os.path.isfile(path) and not name.startswith("."):
                hashes[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
        return hashes

    def immutable_store_hashes(self) -> dict:
        return {k: v for k, v in self.snapshot_stores().items() if k != CANONICAL_FILE}

    def canonical_bytes(self) -> bytes:
        return open(self.canonical_file, "rb").read()

    def payload(self) -> dict:
        return read_json_strict(self.canonical_file)

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if not self._destroyed:
            shutil.rmtree(self.root, ignore_errors=True)
            self._destroyed = True

    @property
    def destroyed(self) -> bool:
        return self._destroyed and not os.path.exists(self.root)

    def __enter__(self) -> "CanonicalFixture":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


# ===========================================================================
# Temporary owner key material (generated outside the repository)
# ===========================================================================

@pytest.fixture(scope="module")
def owner_keys(tmp_path_factory):
    base = tmp_path_factory.mktemp("phase1b_owner_keys")
    private = base / "owner-private.pem"
    public = base / "owner-public.pem"
    generate_signing_keypair(str(private), str(public))

    other_private = base / "other-private.pem"
    other_public = base / "other-public.pem"
    generate_signing_keypair(str(other_private), str(other_public))

    return {
        "dir": str(base),
        "private": str(private),
        "public": str(public),
        "other_private": str(other_private),
        "other_public": str(other_public),
    }


@pytest.fixture()
def fixture():
    fx = CanonicalFixture()
    try:
        yield fx
    finally:
        fx.close()


def _prepare(fx: CanonicalFixture, **overrides) -> dict:
    artifact = build_preparation(
        data_dir=fx.data_dir,
        target_canonical_id=fx.target_id,
        operator=FIXTURE_OPERATOR,
        reason="phase 1-b isolated staging validation",
    )
    artifact.update(overrides)
    return artifact


def _authorize(fx: CanonicalFixture, keys, **overrides) -> dict:
    return sign_authorization(_prepare(fx, **overrides), keys["private"])


def _manual_preparation(fx: CanonicalFixture, **overrides) -> dict:
    """
    Hand-built preparation artifact for malformed collision shapes that
    ``build_preparation`` itself refuses to describe.
    """
    report = inspect_dataset(fx.data_dir)
    records = [m for m in fx.payload()["matches"] if m.get("id") == fx.target_id]
    candidate, field_diff = synthesize_candidate(records)

    artifact = {
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "scope": REPAIR_SCOPE,
        "purpose": AUTHORIZATION_PURPOSE,
        "authorized": False,
        APPROVAL_FIELD: None,
        "migration_id": f"MIG-PHASE1B-{fx.target_id}",
        "data_dir": report["data_dir"],
        "target_canonical_id": fx.target_id,
        "expected_source_sha256": report["store_hashes"][CANONICAL_FILE],
        "expected_source_total": report["total_matches"],
        "expected_candidate_sha256": hash_record(candidate),
        "operator": FIXTURE_OPERATOR,
        "approved_at": "2026-10-12T00:00:00+00:00",
        "reason": "phase 1-b negative matrix",
        "field_diff": field_diff,
    }
    artifact.update(overrides)
    return artifact


def _manual_authorize(fx: CanonicalFixture, keys, **overrides) -> dict:
    return sign_authorization(_manual_preparation(fx, **overrides), keys["private"])


def _repairer(fx: CanonicalFixture, keys, artifact: dict, writable: bool = True):
    return ProductionCanonicalRepairer(
        data_dir=fx.data_dir,
        workspace=fx.workspace,
        migration_id=artifact.get("migration_id"),
        writable=writable,
        trusted_public_key=keys["public"],
    )


# ===========================================================================
# 1. Fixture design: isolation, determinism, shape
# ===========================================================================

def test_fixture_is_isolated_outside_the_repository(fixture):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fx_real = os.path.realpath(fixture.root)
    assert not fx_real.startswith(os.path.realpath(repo_root))
    assert "_quiescent_evidence_backup" not in fx_real
    assert os.path.isdir(fixture.data_dir)
    for store in NON_CANONICAL_STORES:
        assert os.path.exists(os.path.join(fixture.data_dir, store))


def test_fixture_has_exactly_one_duplicate_group(fixture):
    report = inspect_dataset(fixture.data_dir)
    assert report["duplicate_ids"] == {FIXTURE_CANONICAL_ID: 2}
    assert set(report["duplicate_groups"].keys()) == {FIXTURE_CANONICAL_ID}
    assert report["total_matches"] == FIXTURE_SOURCE_TOTAL
    assert report["unique_ids_count"] == FIXTURE_SOURCE_TOTAL - 1


def test_fixture_content_is_deterministic():
    with CanonicalFixture() as a, CanonicalFixture() as b:
        assert a.payload() == b.payload()
        # hash_record is platform-independent (no newline translation).
        assert hash_record(a.payload()) == hash_record(b.payload())
        # The derived file hash reproduces the on-disk bytes on this platform.
        assert derive_file_hash(a.payload()) == a.source_sha256


def test_fixture_candidate_preserves_authoritative_record(fixture):
    payload = fixture.payload()
    records = [m for m in payload["matches"] if m.get("id") == FIXTURE_CANONICAL_ID]
    candidate, field_diff = synthesize_candidate(records)

    assert candidate["status"] == "finished"
    assert candidate["score"] == {"ft": [2, 1], "ht": [1, 0]}
    assert candidate["home"] == "测试主队完整名称"
    assert candidate["away"] == "测试客队完整名称"
    # Empty fields are filled from the inferior sibling, never overwritten.
    assert candidate["jczq_no"] == "9901"
    assert candidate["odds"]["home_win"] == 2.1
    assert field_diff["base_record_sha256"] == hash_record(FIXTURE_RECORD_B)


def test_fixture_is_destroyed_on_close():
    fx = CanonicalFixture()
    root = fx.root
    assert os.path.isdir(root)
    fx.close()
    assert fx.destroyed
    assert not os.path.exists(root)
    fx.close()  # idempotent


# ===========================================================================
# 2. End-to-end happy path: the complete lifecycle
# ===========================================================================

def test_e2e_full_lifecycle(fixture, owner_keys, tmp_path):
    fx, keys = fixture, owner_keys
    baseline_bytes = fx.canonical_bytes()
    baseline_immutable = fx.immutable_store_hashes()

    # ---- Step 1: fixture evidence ---------------------------------------
    assert fx.source_sha256 == sha256_file(fx.canonical_file)
    assert len(baseline_immutable) == len(NON_CANONICAL_STORES)

    # ---- Step 2: prepare (must NOT authorize mutation) -------------------
    prepared = _prepare(fx)
    assert prepared["authorized"] is False
    assert prepared[APPROVAL_FIELD] is None
    assert prepared["authorization_schema_version"] == AUTHORIZATION_SCHEMA_VERSION
    assert prepared["scope"] == REPAIR_SCOPE
    assert prepared["purpose"] == AUTHORIZATION_PURPOSE
    assert prepared["target_canonical_id"] == FIXTURE_CANONICAL_ID
    assert prepared["expected_source_sha256"] == fx.source_sha256
    assert prepared["expected_source_total"] == FIXTURE_SOURCE_TOTAL
    assert prepared["operator"] == FIXTURE_OPERATOR
    assert prepared["migration_id"]

    records = [m for m in fx.payload()["matches"] if m.get("id") == FIXTURE_CANONICAL_ID]
    candidate, _diff = synthesize_candidate(records)
    assert prepared["expected_candidate_sha256"] == hash_record(candidate)

    # An unsigned preparation artifact can never authorize a mutation.
    unsigned_repairer = _repairer(fx, keys, prepared)
    with pytest.raises(SignatureVerificationError):
        unsigned_repairer.apply(prepared, confirm_apply=True, operator=FIXTURE_OPERATOR)
    assert fx.canonical_bytes() == baseline_bytes

    # ---- Step 3: owner approval ------------------------------------------
    authorization = sign_authorization(prepared, keys["private"])
    assert authorization["authorized"] is True
    approval = authorization[APPROVAL_FIELD]
    assert approval["algorithm"] == APPROVAL_ALGORITHM
    assert approval["signature"] and approval["payload_sha256"]
    assert approval["key_id"] == load_trusted_public_key(keys["public"])["key_id"]
    assert approval["key_id"] == "sha256:" + hashlib.sha256(
        __import__("base64").b64decode(approval["public_key"])
    ).hexdigest()
    assert authorization["operator"] == FIXTURE_OPERATOR

    # ---- Step 4: signature verification (PASS + rejects) -----------------
    info = verify_authorization_signature(authorization, keys["public"])
    assert info["verified"] is True
    assert info["payload_sha256"] == approval["payload_sha256"]

    auth_path = str(tmp_path / "authorization.json")
    write_json_atomic(auth_path, authorization)
    assert load_authorization(auth_path, keys["public"])["authorized"] is True

    for label, broken, key in (
        ("modified payload", {**authorization, "expected_source_total": 99}, keys["public"]),
        ("wrong public key", authorization, keys["other_public"]),
        ("missing public key", authorization, None),
        (
            "removed approval block",
            {k: v for k, v in authorization.items() if k != APPROVAL_FIELD},
            keys["public"],
        ),
    ):
        with pytest.raises(SignatureVerificationError):
            verify_authorization_signature(broken, key)
        assert fx.canonical_bytes() == baseline_bytes, label

    # ---- Step 5: dry-run (read-only repairer, exactly as the CLI uses) ----
    preview_repairer = _repairer(fx, keys, authorization, writable=False)
    preview = preview_repairer.dry_run(authorization)
    assert preview["status"] == "DRY_RUN_COMPLETED"
    assert preview["before_total"] == FIXTURE_SOURCE_TOTAL
    assert preview["after_total"] == FIXTURE_PROJECTED_TOTAL
    assert preview["candidate_sha256"] == authorization["expected_candidate_sha256"]
    assert preview["authorization_matches_candidate"] is True
    assert preview["read_only_data_dir"] is True
    assert preview["writes_external_artifact"] is True
    assert preview["canonical_source_sha256"] == fx.source_sha256
    assert preview["projected_file_sha256"]
    assert preview["immutable_store_hashes"] == baseline_immutable
    assert "field_diff" in preview
    # Dry-run touched nothing in the data directory, wrote no journal entry and
    # created no forensic archive; only the documented external preview exists.
    assert fx.canonical_bytes() == baseline_bytes
    assert fx.immutable_store_hashes() == baseline_immutable
    assert not os.path.exists(
        os.path.join(fx.workspace, "journal", "disposition_journal.jsonl")
    ), "dry-run must not write journal entries"
    assert not os.path.exists(os.path.join(fx.workspace, "archive")), \
        "dry-run must not create a forensic archive"
    assert os.path.exists(preview["preview_file"])

    # ---- Step 6: controlled apply ---------------------------------------
    repairer = _repairer(fx, keys, authorization)
    result = repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)
    assert result["status"] == "SUCCESS"
    assert result["before_total"] == FIXTURE_SOURCE_TOTAL
    assert result["after_total"] == FIXTURE_PROJECTED_TOTAL
    assert result["duplicate_ids_count"] == 0
    assert result["immutable_stores_changed"] == 0
    assert result["candidate_sha256"] == authorization["expected_candidate_sha256"]

    # ---- Step 7: postconditions -----------------------------------------
    report = inspect_dataset(fx.data_dir)
    assert report["duplicate_ids"] == {}
    assert report["total_matches"] == report["unique_ids_count"] == FIXTURE_PROJECTED_TOTAL
    assert report["verification"] == DATASET_INTEGRITY_INSPECTION

    current = fx.payload()
    assert current["meta"]["total"] == FIXTURE_PROJECTED_TOTAL
    targets = [m for m in current["matches"] if m.get("id") == FIXTURE_CANONICAL_ID]
    assert len(targets) == 1
    assert hash_record(targets[0]) == authorization["expected_candidate_sha256"]

    baseline_payload = json.loads(baseline_bytes.decode("utf-8"))
    assert [m for m in current["matches"] if m.get("id") != FIXTURE_CANONICAL_ID] == [
        m for m in baseline_payload["matches"] if m.get("id") != FIXTURE_CANONICAL_ID
    ]
    assert current["schema_version"] == baseline_payload["schema_version"]
    assert current["meta"]["last_refresh"] == baseline_payload["meta"]["last_refresh"]

    # ---- Step 8: six immutable stores byte identical ---------------------
    assert fx.immutable_store_hashes() == baseline_immutable

    # ---- Step 9: full repair verification --------------------------------
    audit = repairer.verify(authorization, trusted_public_key=keys["public"])
    assert audit["verified"] is True
    assert audit["complete"] is True
    assert audit["verification"] == FULL_REPAIR_VERIFICATION
    assert audit["evidence"]["immutable_store_hashes"] == "ALL_SIX_UNCHANGED"
    assert audit["evidence"]["journal_chain"] == "VALID_TERMINAL_COMPLETION"


def test_e2e_journal_state_machine(fixture, owner_keys):
    fx, keys = fixture, owner_keys
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)

    entries = repairer.journal.read_entries()
    assert [e["state"] for e in entries] == JOURNAL_STATES
    assert repairer.journal.get_latest_valid_state() == "COMPLETION_RECORDED"
    repairer.journal.validate_chain(entries, expected_migration_id=authorization["migration_id"])

    # Every phase carried the migration identity.
    assert {e["payload"]["migration_id"] for e in entries} == {authorization["migration_id"]}

    # Evidence archive was created and is bound to the source bytes.
    raw = os.path.join(repairer.archive_dir, "daily_matches_raw.json")
    assert os.path.exists(raw)
    assert sha256_file(raw) == authorization["expected_source_sha256"]
    assert os.path.exists(os.path.join(repairer.archive_dir, "forensic_manifest.json"))


def test_e2e_apply_is_idempotent_and_verification_repeats(fixture, owner_keys):
    fx, keys = fixture, owner_keys
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)
    journal_after_first = repairer.journal.read_entries()
    immutable_after_first = fx.immutable_store_hashes()

    second = repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)
    assert second["status"] == "NO_OP_ALREADY_REPAIRED"
    assert repairer.journal.read_entries() == journal_after_first
    assert fx.immutable_store_hashes() == immutable_after_first

    for _ in range(2):
        audit = repairer.verify(authorization, trusted_public_key=keys["public"])
        assert audit["verified"] is True and audit["complete"] is True


# ===========================================================================
# 3. Crash recovery simulation (test-only fault injection)
# ===========================================================================

#: Interruption point -> expected recovery classification.
CRASH_MATRIX = [
    ("before_archive", "RECOVERED_PRE_COMMIT_CLEAN"),
    ("before_commit_intent", "RECOVERED_PRE_COMMIT_CLEAN"),
    ("after_commit_intent", "RECOVERED_ROLLED_BACK"),
    ("after_file_replace", "RECOVERED_FORWARD_COMPLETED"),
    ("before_postconditions", "RECOVERED_FORWARD_COMPLETED"),
    ("before_completion", "RECOVERED_FORWARD_COMPLETED"),
]


@pytest.mark.parametrize("point,expected_recovery", CRASH_MATRIX)
def test_e2e_crash_interruption_fails_safely(fixture, owner_keys, point, expected_recovery):
    fx, keys = fixture, owner_keys
    baseline_bytes = fx.canonical_bytes()
    baseline_immutable = fx.immutable_store_hashes()

    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(
                authorization,
                confirm_apply=True,
                operator=FIXTURE_OPERATOR,
                fault_inject=point,
            )

    # --- no silent corruption ---------------------------------------------
    entries = repairer.journal.read_entries()
    repairer.journal.validate_chain(entries, expected_migration_id=authorization["migration_id"])
    states = [e["state"] for e in entries]

    # --- no false completion ----------------------------------------------
    assert "COMPLETION_RECORDED" not in states

    # --- no immutable store mutation --------------------------------------
    assert fx.immutable_store_hashes() == baseline_immutable

    # --- active file is either the baseline or the projection, never junk ---
    current = fx.canonical_bytes()
    if current != baseline_bytes:
        report = inspect_dataset(fx.data_dir)
        assert report["duplicate_ids"] == {}
        assert report["total_matches"] == FIXTURE_PROJECTED_TOTAL

    # --- recovery resolves deterministically -------------------------------
    recovery = repairer.recover(
        authorization, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
    )
    assert recovery["status"] == expected_recovery
    assert fx.immutable_store_hashes() == baseline_immutable

    if expected_recovery == "RECOVERED_ROLLED_BACK":
        assert fx.canonical_bytes() == baseline_bytes
    else:
        final_states = [e["state"] for e in repairer.journal.read_entries()]
        if expected_recovery == "RECOVERED_FORWARD_COMPLETED":
            assert final_states[-1] == "COMPLETION_RECORDED"
            audit = repairer.verify(authorization, trusted_public_key=keys["public"])
            assert audit["verified"] is True and audit["complete"] is True


def test_e2e_fault_injection_is_unreachable_from_the_cli(fixture, owner_keys, tmp_path, capsys):
    """The production CLI must not be able to arm the destructive hooks."""
    from tools.production_canonical_repair import main

    fx, keys = fixture, owner_keys
    prepared_path = str(tmp_path / "prepared.json")
    auth_path = str(tmp_path / "authorization.json")
    write_json_atomic(prepared_path, _prepare(fx))
    write_json_atomic(auth_path, sign_authorization(_prepare(fx), keys["private"]))

    baseline_immutable = fx.immutable_store_hashes()
    baseline_bytes = fx.canonical_bytes()

    with pytest.raises(SystemExit) as exc:
        main([
            "apply", "--data-dir", fx.data_dir, "--authorization", auth_path,
            "--workspace", fx.workspace, "--operator", FIXTURE_OPERATOR,
            "--trusted-public-key", keys["public"], "--confirm-apply",
            "--fault-inject", "corrupt_store_pre_postconditions",
        ])
    assert exc.value.code == 2
    capsys.readouterr()

    assert fx.canonical_bytes() == baseline_bytes
    assert fx.immutable_store_hashes() == baseline_immutable


# ===========================================================================
# 4. Recovery validation: rollback and forward
# ===========================================================================

def test_e2e_recovery_scenario_a_rollback(fixture, owner_keys):
    """Projection NOT committed -> safe rollback, baseline restored."""
    fx, keys = fixture, owner_keys
    baseline_bytes = fx.canonical_bytes()
    baseline_immutable = fx.immutable_store_hashes()
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(
                authorization, confirm_apply=True, operator=FIXTURE_OPERATOR,
                fault_inject="after_commit_intent",
            )

    # Durable intent exists, but the file is still the baseline.
    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states[-1] == "COMMIT_INTENT_DURABLE"
    assert fx.canonical_bytes() == baseline_bytes

    recovery = repairer.recover(
        authorization, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
    )
    assert recovery["status"] == "RECOVERED_ROLLED_BACK"
    assert fx.canonical_bytes() == baseline_bytes
    assert fx.immutable_store_hashes() == baseline_immutable

    # The dataset is genuinely back to its pre-repair shape.
    report = inspect_dataset(fx.data_dir)
    assert report["duplicate_ids"] == {FIXTURE_CANONICAL_ID: 2}

    # And the same authorization can still drive a successful repair.
    result = repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)
    assert result["status"] == "SUCCESS"
    assert inspect_dataset(fx.data_dir)["duplicate_ids"] == {}


def test_e2e_recovery_scenario_b_forward(fixture, owner_keys):
    """Projection committed -> forward recovery reaching COMPLETION_RECORDED."""
    fx, keys = fixture, owner_keys
    baseline_immutable = fx.immutable_store_hashes()
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(
                authorization, confirm_apply=True, operator=FIXTURE_OPERATOR,
                fault_inject="after_file_replace",
            )

    assert [e["state"] for e in repairer.journal.read_entries()][-1] == "PROJECTION_COMMITTED"
    assert inspect_dataset(fx.data_dir)["duplicate_ids"] == {}

    recovery = repairer.recover(
        authorization, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
    )
    assert recovery["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert [e["state"] for e in repairer.journal.read_entries()][-1] == "COMPLETION_RECORDED"
    assert fx.immutable_store_hashes() == baseline_immutable

    audit = repairer.verify(authorization, trusted_public_key=keys["public"])
    assert audit["verified"] is True
    assert audit["complete"] is True
    assert audit["evidence"]["journal_chain"] == "VALID_TERMINAL_COMPLETION"


# ===========================================================================
# 5. Negative security matrix
# ===========================================================================

def test_e2e_authorization_negative_matrix(fixture, owner_keys, tmp_path):
    fx, keys = fixture, owner_keys
    baseline_bytes = fx.canonical_bytes()
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)

    def _expect_reject(artifact, **kwargs):
        with pytest.raises(AuthorizationError):
            repairer.apply(artifact, confirm_apply=True, **kwargs)
        assert fx.canonical_bytes() == baseline_bytes

    unsigned = _prepare(fx)
    _expect_reject(unsigned, operator=FIXTURE_OPERATOR)                       # unsigned
    _expect_reject({**authorization, APPROVAL_FIELD: None}, operator=FIXTURE_OPERATOR)  # removed approval
    _expect_reject({**authorization, "authorized": False}, operator=FIXTURE_OPERATOR)   # flag downgrade

    forged = copy.deepcopy(authorization)
    forged[APPROVAL_FIELD] = dict(forged[APPROVAL_FIELD])
    forged[APPROVAL_FIELD]["signature"] = "A" * 88
    _expect_reject(forged, operator=FIXTURE_OPERATOR)                          # forged signature

    attacker = sign_authorization(_prepare(fx), keys["other_private"])
    _expect_reject(attacker, operator=FIXTURE_OPERATOR)                        # wrong key

    _expect_reject({**authorization, "target_canonical_id": "other-id"}, operator=FIXTURE_OPERATOR)
    _expect_reject({**authorization, "expected_source_sha256": "0" * 64}, operator=FIXTURE_OPERATOR)
    _expect_reject({**authorization, "expected_candidate_sha256": "0" * 64}, operator=FIXTURE_OPERATOR)
    _expect_reject({**authorization, "migration_id": "MIG-FORGED"}, operator=FIXTURE_OPERATOR)

    # Operator identity binding.
    with pytest.raises(AuthorizationError):
        repairer.apply(authorization, confirm_apply=True, operator=None)
    with pytest.raises(AuthorizationError):
        repairer.apply(authorization, confirm_apply=True, operator="someone-else")
    with pytest.raises(AuthorizationError):
        repairer.apply(authorization, confirm_apply=True)
    assert fx.canonical_bytes() == baseline_bytes

    # Confirmation is mandatory.
    with pytest.raises(AuthorizationError):
        repairer.apply(authorization, confirm_apply=False, operator=FIXTURE_OPERATOR)
    assert fx.canonical_bytes() == baseline_bytes

    # A legacy schema-1.0.0 artifact can never authorize a mutation.
    legacy_path = str(tmp_path / "legacy.json")
    legacy = {k: v for k, v in authorization.items() if k != APPROVAL_FIELD}
    legacy["authorization_schema_version"] = "1.0.0"
    legacy["authorized"] = True
    write_json_atomic(legacy_path, legacy)
    with pytest.raises(AuthorizationError):
        load_authorization(legacy_path, keys["public"])


def test_e2e_data_negative_matrix(owner_keys):
    # Three records sharing one Canonical ID.
    with CanonicalFixture(target_occurrences=3) as fx:
        baseline = fx.canonical_bytes()
        artifact = _manual_authorize(fx, owner_keys)
        repairer = _repairer(fx, owner_keys, artifact)
        with pytest.raises(PreconditionViolationError):
            repairer.apply(artifact, confirm_apply=True, operator=FIXTURE_OPERATOR)
        assert fx.canonical_bytes() == baseline

    # More than one duplicate group.
    with CanonicalFixture(extra_duplicate_group=True) as fx:
        baseline = fx.canonical_bytes()
        artifact = _manual_authorize(fx, owner_keys)
        repairer = _repairer(fx, owner_keys, artifact)
        with pytest.raises(PreconditionViolationError):
            repairer.apply(artifact, confirm_apply=True, operator=FIXTURE_OPERATOR)
        assert fx.canonical_bytes() == baseline

    # No terminal record with a strictly valid full-time score.
    with CanonicalFixture(corrupt_final_score=True) as fx:
        baseline = fx.canonical_bytes()
        artifact = _manual_authorize(fx, owner_keys)
        repairer = _repairer(fx, owner_keys, artifact)
        with pytest.raises(PreconditionViolationError):
            repairer.apply(artifact, confirm_apply=True, operator=FIXTURE_OPERATOR)
        assert fx.canonical_bytes() == baseline

    # A well-formed collision is still refused by prepare when another group exists.
    with CanonicalFixture(extra_duplicate_group=True) as fx:
        with pytest.raises(PreconditionViolationError):
            _prepare(fx)


def test_e2e_missing_forensic_archive_fails_closed(fixture, owner_keys):
    fx, keys = fixture, owner_keys
    baseline_bytes = fx.canonical_bytes()
    authorization = _authorize(fx, keys)

    # --- Part 1: archive removed mid-transaction -> recovery refuses --------
    repairer = _repairer(fx, keys, authorization)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(
                authorization, confirm_apply=True, operator=FIXTURE_OPERATOR,
                fault_inject="after_commit_intent",
            )

    shutil.rmtree(repairer.archive_dir)

    with pytest.raises(RecoveryStateError):
        repairer.recover(
            authorization, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
        )
    assert fx.canonical_bytes() == baseline_bytes

    # --- Part 2: completed repair with the archive removed -> INCOMPLETE ----
    fx2 = CanonicalFixture()
    try:
        auth2 = _authorize(fx2, keys)
        repairer2 = _repairer(fx2, keys, auth2)
        assert repairer2.apply(
            auth2, confirm_apply=True, operator=FIXTURE_OPERATOR
        )["status"] == "SUCCESS"
        assert repairer2.verify(auth2, trusted_public_key=keys["public"])["verified"] is True

        shutil.rmtree(repairer2.archive_dir)

        audit = repairer2.verify(auth2, trusted_public_key=keys["public"])
        assert audit["verified"] is False
        assert audit["complete"] is False
        assert "forensic_archive_raw" in audit["error"]

        with pytest.raises(IncompleteVerificationError, match="INCOMPLETE"):
            repairer2.verify_completed_dataset(auth2, trusted_public_key=keys["public"])
    finally:
        fx2.close()


def test_e2e_recovery_negative_matrix(fixture, owner_keys):
    fx, keys = fixture, owner_keys
    authorization = _authorize(fx, keys)

    # Forged journal: a projected fingerprint the archive cannot produce.
    repairer = _repairer(fx, keys, authorization)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(
                authorization, confirm_apply=True, operator=FIXTURE_OPERATOR,
                fault_inject="after_file_replace",
            )
    entries = repairer.journal.read_entries()
    for entry in entries:
        if entry["state"] == "COMMIT_INTENT_DURABLE":
            entry["payload"]["projected_file_sha256"] = "a" * 64
    with open(repairer.journal.journal_file, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    with pytest.raises(RecoveryStateError, match="independently derived"):
        repairer.recover(
            authorization, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
        )

    # Invalid chain: a skipped phase.
    fx2 = CanonicalFixture()
    try:
        auth2 = _authorize(fx2, keys)
        repairer2 = _repairer(fx2, keys, auth2)
        with fault_injection_enabled():
            with pytest.raises(Exception):
                repairer2.apply(
                    auth2, confirm_apply=True, operator=FIXTURE_OPERATOR,
                    fault_inject="after_commit_intent",
                )
        entries2 = [e for e in repairer2.journal.read_entries() if e["state"] != "EVIDENCE_VERIFIED"]
        with open(repairer2.journal.journal_file, "w", encoding="utf-8") as fh:
            for entry in entries2:
                fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        with pytest.raises(RecoveryStateError):
            repairer2.recover(
                auth2, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
            )

        # Unknown active-file state.
        payload = fx2.payload()
        payload["matches"] = payload["matches"][:2]
        write_json_atomic(fx2.canonical_file, payload)
        with pytest.raises(RecoveryStateError):
            repairer2.recover(
                auth2, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
            )

        # Mismatched authorization.
        with pytest.raises(AuthorizationError):
            repairer2.recover(operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"])
    finally:
        fx2.close()

    # Immutable store drift.
    fx3 = CanonicalFixture()
    try:
        auth3 = _authorize(fx3, keys)
        repairer3 = _repairer(fx3, keys, auth3)
        with fault_injection_enabled():
            with pytest.raises(ImmutableStoreMutationError):
                repairer3.apply(
                    auth3, confirm_apply=True, operator=FIXTURE_OPERATOR,
                    fault_inject="corrupt_store_pre_postconditions",
                )
        with pytest.raises(ImmutableStoreMutationError):
            repairer3.recover(
                auth3, operator=FIXTURE_OPERATOR, trusted_public_key=keys["public"]
            )
    finally:
        fx3.close()


# ===========================================================================
# 6. CLI contract
# ===========================================================================

def test_e2e_cli_contract(capsys):
    from tools.production_canonical_repair import build_parser, main

    parser = build_parser()
    subparsers = [
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    ][0]
    available = set(subparsers.choices)
    assert {
        "inspect", "prepare", "approve", "verify", "verify-authorization",
        "dry-run", "apply", "recover", "keygen",
    } <= available

    # The superseded unsigned-only entry point must be gone.
    assert "authorize" not in available

    # No production bypass switch may exist anywhere on the command surface.
    help_text = parser.format_help()
    for sub in available:
        help_text += subparsers.choices[sub].format_help()
    for forbidden in (
        "--force", "--skip-verification", "--ignore-hash",
        "--allow-unsigned", "--allow-production-without-approval",
        "--fault-inject",
    ):
        assert forbidden not in help_text, forbidden

    # Fault injection is unreachable from the CLI surface.
    with pytest.raises(SystemExit) as exc:
        main(["apply", "--data-dir", ".", "--authorization", "x",
              "--operator", "o", "--trusted-public-key", "k",
              "--confirm-apply", "--fault-inject", "before_archive"])
    assert exc.value.code == 2
    capsys.readouterr()


# ===========================================================================
# 7. Production isolation
# ===========================================================================

def test_e2e_no_production_path_is_touched(fixture, owner_keys):
    """A full lifecycle must not create repair_runs/ or touch data/."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    production_data = os.path.join(repo_root, "data")
    production_runs = os.path.join(repo_root, "repair_runs")

    def snapshot(directory):
        if not os.path.isdir(directory):
            return None
        return {
            name: hashlib.sha256(open(os.path.join(directory, name), "rb").read()).hexdigest()
            for name in sorted(os.listdir(directory))
            if os.path.isfile(os.path.join(directory, name))
        }

    before_data = snapshot(production_data)
    assert not os.path.exists(production_runs)

    fx, keys = fixture, owner_keys
    authorization = _authorize(fx, keys)
    repairer = _repairer(fx, keys, authorization)
    repairer.dry_run(authorization)
    repairer.apply(authorization, confirm_apply=True, operator=FIXTURE_OPERATOR)
    repairer.verify(authorization, trusted_public_key=keys["public"])

    assert snapshot(production_data) == before_data
    assert not os.path.exists(production_runs)
    # The workspace is the fixture's own, outside the repository.
    assert not os.path.realpath(fx.workspace).startswith(os.path.realpath(repo_root))
