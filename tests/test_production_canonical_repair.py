"""
tests/test_production_canonical_repair.py
=========================================
Offline test suite for the auditable production Canonical de-duplication entry
point (``tools/production_canonical_repair.py``).

Coverage
--------
* Safety boundaries: evidence epochs, drive roots, workspace placement.
* Read-only guarantees for ``inspect`` / ``dry-run`` / ``verify``.
* The Ed25519 authorization trust boundary: preparation is unsigned, approval
  is signed, and only a signature that verifies against a separately
  provisioned trusted public key can authorize a mutation.
* Authorization binding: schema, scope, purpose, ``authorized`` flag, data-dir,
  source hash, source record count, candidate hash, operator, migration ID.
* Operator identity is mandatory for every mutation.
* CLI safety: no destructive fault-injection flag exists on the deployable
  command surface, and unsupported mutation switches are rejected without
  touching any persistent store.
* Happy path: dynamic ``N -> N - 1`` projection with every postcondition
  enforced (uniqueness, unrelated-record preservation and ordering, metadata
  preservation, unknown top-level key preservation, six immutable stores).
* Separation of dataset integrity inspection from full repair verification.
* Idempotency, lock contention, and refusal on unexpected collision shapes.
* Deterministic crash recovery (rollback / forward / fail closed on UNKNOWN),
  bound to an authenticated authorization.

Every fixture lives in a temporary directory: this suite never touches ``data/``
and never performs network access.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os

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
    ProtectedPathViolationError,
    RecoveryStateError,
    SignatureVerificationError,
    StagingLockError,
    acquire_repair_lock,
    authorization_fingerprint,
    build_preparation,
    canonicalize_path,
    default_migration_id,
    derive_file_hash,
    fault_injection_enabled,
    generate_signing_keypair,
    inspect_dataset,
    load_authorization,
    load_trusted_public_key,
    sign_authorization,
    synthesize_candidate,
    verify_authorization_signature,
    write_json_atomic,
)
from tools.staging_canonical_repair import StagingMigrationError, hash_record

TARGET_ID = "500l-2026-10-09-1362422"
OPERATOR = "ops-oncall"

# Record A: live-score alias (abbreviated club names, closing odds, lottery no.)
RECORD_A = {
    "id": TARGET_ID,
    "sport": "football",
    "league": "巴甲",
    "round": "第29轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "upcoming",
    "home": "巴竞技",
    "away": "米竞技",
    "home_rank": 3,
    "away_rank": 7,
    "score": None,
    "odds": {"home_win": 4.75, "away_win": 1.6, "draw": 3.4, "matchnum": "4004"},
    "jczq_no": "4004",
    "market_tracked": True,
    "odds_updated_at": "2026-10-08T21:19:44.432817+08:00",
}

# Record B: finished feed (standard club names, terminal score, no odds)
RECORD_B = {
    "id": TARGET_ID,
    "sport": "football",
    "league": "巴甲",
    "round": "第29轮",
    "date": "2026-10-09",
    "time": "07:00",
    "kickoff_time_known": True,
    "status": "finished",
    "home": "巴拉纳竞技",
    "away": "米内罗竞技",
    "home_rank": 3,
    "away_rank": 7,
    "score": {"ft": [2, 2], "ht": [1, 1]},
    "odds": None,
    "jczq_no": "",
    "market_tracked": True,
}


# ===========================================================================
# Owner key material (provisioned once, outside the repository)
# ===========================================================================

_KEYS: dict = {}


@pytest.fixture(scope="session", autouse=True)
def owner_keys(tmp_path_factory):
    """Provision an owner keypair plus an unrelated keypair, outside the repo."""
    base = tmp_path_factory.mktemp("owner_keys")
    private = base / "owner-private.pem"
    public = base / "owner-public.pem"
    generate_signing_keypair(str(private), str(public))

    other_private = base / "attacker-private.pem"
    other_public = base / "attacker-public.pem"
    generate_signing_keypair(str(other_private), str(other_public))

    _KEYS.update({
        "dir": str(base),
        "private": str(private),
        "public": str(public),
        "other_private": str(other_private),
        "other_public": str(other_public),
    })
    yield _KEYS


def _match(mid, home, away, *, status="upcoming", score=None, date="2026-10-09", time="01:00"):
    return {
        "id": mid,
        "sport": "football",
        "league": "测试联赛",
        "round": "第1轮",
        "date": date,
        "time": time,
        "status": status,
        "home": home,
        "away": away,
        "score": score,
        "odds": None,
        "jczq_no": "",
    }


def _stores_payload():
    return {
        "team_strength.json": {"巴拉纳竞技": {"rating": 1666.7}},
        "calibrated.json": {"ids": [TARGET_ID, "500l-2026-10-09-1000"]},
        "prediction_snapshots.json": {"version": 1, "snapshots": {"s1": {"match_id": TARGET_ID}}},
        "odds_snapshots.json": {"version": 1, "snapshots": {"o1": {"match_id": TARGET_ID}}},
        "settlements.json": {"version": 1, "settlements": {"t1": {"match_id": TARGET_ID}}},
        "evaluation_rows.json": {"version": 1, "rows": {"e1": {"match_id": TARGET_ID}}},
    }


def build_dataset(
    root: str,
    *,
    n_before: int = 6,
    n_after: int = 5,
    target_occurrences: int = 2,
    extra_dup_group: bool = False,
) -> str:
    """
    Construct a synthetic data directory with all seven stores.

    ``target_occurrences == 2`` is the well-formed collision; other values model
    malformed datasets the tool must refuse.
    """
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    matches = [
        _match(f"500l-2026-10-09-{1000 + i}", f"主队{i}", f"客队{i}", status="finished", score={"ft": [1, 0]})
        for i in range(n_before)
    ]

    if target_occurrences >= 1:
        matches.append(copy.deepcopy(RECORD_A))
    if target_occurrences >= 2:
        matches.append(copy.deepcopy(RECORD_B))
    for extra in range(max(0, target_occurrences - 2)):
        third = copy.deepcopy(RECORD_B)
        third["score"] = {"ft": [3, 3], "ht": [1, 1]}
        third["home"] = f"第三记录{extra}"
        matches.append(third)

    if extra_dup_group:
        matches.append(_match("dup-other", "X", "Y"))
        matches.append(_match("dup-other", "X2", "Y2"))

    matches.extend(
        _match(f"500l-2026-10-09-{2000 + i}", f"后续主队{i}", f"后续客队{i}")
        for i in range(n_after)
    )

    payload = {
        "schema_version": 3,  # unknown top-level key that must survive
        "matches": matches,
        "meta": {
            "total": len(matches),
            "last_refresh": "2026-10-10T18:45:23.927174+08:00",
            "sources": {"live_football": 462, "wanchang": 914},
        },
    }
    write_json_atomic(os.path.join(data_dir, CANONICAL_FILE), payload)
    for name, data in _stores_payload().items():
        write_json_atomic(os.path.join(data_dir, name), data)

    return data_dir


def snapshot_dir_hashes(directory: str) -> dict:
    """Hash every visible file in ``directory``.

    Dotfiles are skipped: ``.refresh.lock`` is created on demand by the shared
    cross-process lock and is not part of the evidence set.
    """
    hashes = {}
    for name in sorted(os.listdir(directory)):
        if name.startswith("."):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            hashes[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    return hashes


# ===========================================================================
# Authorization helpers
# ===========================================================================

def make_preparation(data_dir: str, **overrides):
    """Unsigned preparation artifact, exactly as the ``prepare`` command emits."""
    artifact = build_preparation(
        data_dir=data_dir,
        target_canonical_id=TARGET_ID,
        operator=OPERATOR,
        reason="repair legacy canonical duplicate",
    )
    artifact.update(overrides)
    return artifact


def make_authorization(data_dir: str, **overrides):
    """Signed authorization artifact (prepared, then owner-approved)."""
    return sign_authorization(make_preparation(data_dir, **overrides), _KEYS["private"])


def manual_preparation(data_dir: str, **overrides):
    """Hand-built preparation artifact used to exercise apply-side guards that
    ``build_preparation`` itself would refuse to produce."""
    report = inspect_dataset(data_dir)
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    records = [m for m in payload["matches"] if m.get("id") == TARGET_ID]
    candidate, field_diff = synthesize_candidate(records)
    artifact = {
        "authorization_schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "scope": REPAIR_SCOPE,
        "purpose": AUTHORIZATION_PURPOSE,
        "authorized": False,
        APPROVAL_FIELD: None,
        "migration_id": default_migration_id(TARGET_ID),
        "data_dir": canonicalize_path(data_dir),
        "target_canonical_id": TARGET_ID,
        "expected_source_sha256": report["store_hashes"][CANONICAL_FILE],
        "expected_source_total": report["total_matches"],
        "expected_candidate_sha256": hash_record(candidate),
        "operator": OPERATOR,
        "approved_at": "2026-10-12T00:00:00+00:00",
        "reason": "manual",
        "field_diff": field_diff,
    }
    artifact.update(overrides)
    return artifact


def manual_authorization(data_dir: str, **overrides):
    return sign_authorization(manual_preparation(data_dir, **overrides), _KEYS["private"])


def tampered(artifact: dict, **changes) -> dict:
    """Return a copy of a signed artifact with fields changed after signing."""
    out = copy.deepcopy(artifact)
    out.update(changes)
    return out


def make_repairer(data_dir: str, workspace: str, artifact: dict | None = None, **kwargs):
    return ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=workspace,
        migration_id=(artifact or {}).get("migration_id"),
        trusted_public_key=_KEYS["public"],
        **kwargs,
    )


def apply_artifact(tmp_path, data_dir, artifact, operator=OPERATOR, **kwargs):
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    return repairer, repairer.apply(artifact, confirm_apply=True, operator=operator, **kwargs)


# ===========================================================================
# 1. Inspection & read-only guarantees
# ===========================================================================

def test_inspect_reports_collision(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    report = inspect_dataset(data_dir)

    assert report["duplicate_ids"] == {TARGET_ID: 2}
    assert report["duplicate_groups"][TARGET_ID]["count"] == 2
    assert report["duplicate_groups"][TARGET_ID]["indices"][1] - report["duplicate_groups"][TARGET_ID]["indices"][0] == 1
    assert report["total_matches"] == report["unique_ids_count"] + 1
    assert report["store_hashes"][CANONICAL_FILE] != "MISSING"


def test_inspect_clean_dataset_has_no_collision(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=1)
    report = inspect_dataset(data_dir)
    assert report["duplicate_ids"] == {}
    assert report["total_matches"] == report["unique_ids_count"]


def test_inspect_rejects_missing_canonical_file(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(PreconditionViolationError):
        inspect_dataset(str(empty))


def test_inspect_rejects_evidence_epoch_path(tmp_path):
    data_dir = build_dataset(str(tmp_path / "_quiescent_evidence_backup_20261010"))
    with pytest.raises(ProtectedPathViolationError):
        inspect_dataset(data_dir)


def test_inspect_is_read_only(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    before = snapshot_dir_hashes(data_dir)
    inspect_dataset(data_dir)
    inspect_dataset(data_dir)
    assert snapshot_dir_hashes(data_dir) == before


def test_workspace_inside_data_dir_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(ProtectedPathViolationError):
        ProductionCanonicalRepairer(data_dir=data_dir, workspace=os.path.join(data_dir, "ws"))


def test_workspace_parent_of_data_dir_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(ProtectedPathViolationError):
        ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path))


# ===========================================================================
# 2. Authorization artifact: preparation vs. approval
# ===========================================================================

def test_build_preparation_is_unsigned_and_binds_source_and_candidate(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_preparation(data_dir)

    assert artifact["authorization_schema_version"] == AUTHORIZATION_SCHEMA_VERSION
    assert artifact["scope"] == REPAIR_SCOPE
    assert artifact["purpose"] == AUTHORIZATION_PURPOSE
    # Preparation grants nothing.
    assert artifact["authorized"] is False
    assert artifact[APPROVAL_FIELD] is None

    assert artifact["data_dir"] == canonicalize_path(data_dir)
    assert artifact["expected_source_sha256"] == inspect_dataset(data_dir)["store_hashes"][CANONICAL_FILE]
    assert artifact["expected_source_total"] == inspect_dataset(data_dir)["total_matches"]

    candidate, _ = synthesize_candidate([RECORD_A, RECORD_B])
    assert artifact["expected_candidate_sha256"] == hash_record(candidate)
    # The synthesized record keeps the terminal result and the closing odds.
    assert candidate["home"] == "巴拉纳竞技"
    assert candidate["status"] == "finished"
    assert candidate["score"] == {"ft": [2, 2], "ht": [1, 1]}
    assert candidate["jczq_no"] == "4004"
    assert candidate["odds"]["home_win"] == 4.75


def test_build_authorization_refuses_clean_dataset(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=1)
    with pytest.raises(PreconditionViolationError):
        make_preparation(data_dir)


def test_build_authorization_refuses_unexpected_duplicate_group(tmp_path):
    data_dir = build_dataset(str(tmp_path), extra_dup_group=True)
    with pytest.raises(PreconditionViolationError):
        make_preparation(data_dir)


def test_build_authorization_refuses_three_occurrences(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=3)
    with pytest.raises(PreconditionViolationError):
        make_preparation(data_dir)


def test_build_authorization_requires_operator_and_reason(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    with pytest.raises(AuthorizationError):
        build_preparation(data_dir, TARGET_ID, "", "reason")
    with pytest.raises(AuthorizationError):
        build_preparation(data_dir, TARGET_ID, "ops", "   ")


def test_signed_artifact_verifies_and_is_bound_to_every_declared_field(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    assert artifact["authorized"] is True
    approval = artifact[APPROVAL_FIELD]
    assert approval["algorithm"] == APPROVAL_ALGORITHM
    assert approval["key_id"] == load_trusted_public_key(_KEYS["public"])["key_id"]

    info = verify_authorization_signature(artifact, _KEYS["public"])
    assert info["verified"] is True

    # Every bound field is covered by the signature.
    for field, value in (
        ("scope", "SOMETHING_ELSE"),
        ("target_canonical_id", "other-id"),
        ("expected_source_sha256", "0" * 64),
        ("expected_candidate_sha256", "0" * 64),
        ("expected_source_total", 999),
        ("migration_id", "MIG-FORGED"),
        ("operator", "someone-else"),
        ("purpose", "SOMETHING_ELSE"),
        ("reason", "rewritten"),
    ):
        with pytest.raises(SignatureVerificationError):
            verify_authorization_signature(tampered(artifact, **{field: value}), _KEYS["public"])


def test_load_authorization_rejects_unauthorized_flag(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = tampered(make_authorization(data_dir), authorized=False)
    path = str(tmp_path / "auth.json")
    write_json_atomic(path, artifact)
    with pytest.raises(SignatureVerificationError):
        load_authorization(path, _KEYS["public"])


def test_load_authorization_rejects_bad_scope_and_missing_fields(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    bad_scope = make_authorization(data_dir, scope="SOMETHING_ELSE")
    path = str(tmp_path / "bad_scope.json")
    write_json_atomic(path, bad_scope)
    with pytest.raises(AuthorizationError):
        load_authorization(path, _KEYS["public"])

    missing = make_authorization(data_dir)
    missing.pop("expected_candidate_sha256")
    path2 = str(tmp_path / "missing.json")
    write_json_atomic(path2, missing)
    with pytest.raises(AuthorizationError):
        load_authorization(path2, _KEYS["public"])


def test_load_authorization_rejects_legacy_unsigned_schema(tmp_path):
    """A schema 1.0.0 artifact asserted `authorized: true` with no signature."""
    data_dir = build_dataset(str(tmp_path))
    legacy = make_authorization(data_dir)
    legacy["authorization_schema_version"] = "1.0.0"
    legacy.pop(APPROVAL_FIELD)
    path = str(tmp_path / "legacy.json")
    write_json_atomic(path, legacy)

    with pytest.raises(AuthorizationError, match="predates signed owner approvals"):
        load_authorization(path, _KEYS["public"])


def test_load_authorization_accepts_unsigned_preparation_only_for_preview(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    prepared = make_preparation(data_dir)
    path = str(tmp_path / "prepared.json")
    write_json_atomic(path, prepared)

    # Read-only preview may inspect an unsigned preparation artifact...
    loaded = load_authorization(path, require_signature=False)
    assert loaded["authorized"] is False

    # ...but it can never authorize a mutation.
    with pytest.raises(SignatureVerificationError):
        load_authorization(path, _KEYS["public"], require_signature=True)


# ===========================================================================
# 3. Negative: authorization trust boundary
# ===========================================================================

def test_unsigned_authorization_cannot_authorize_apply(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    prepared = make_preparation(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), prepared)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(prepared, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_forged_authorization_signature_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    forged = tampered(artifact)
    forged[APPROVAL_FIELD] = dict(artifact[APPROVAL_FIELD])
    forged[APPROVAL_FIELD]["signature"] = "A" * 88  # well-formed base64, wrong bytes

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(forged, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_authorization_signed_by_untrusted_key_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    prepared = make_preparation(data_dir)
    before = snapshot_dir_hashes(data_dir)

    # An attacker with their own keypair produces a perfectly valid signature...
    attacker_signed = sign_authorization(prepared, _KEYS["other_private"])
    with pytest.raises(SignatureVerificationError, match="untrusted key"):
        verify_authorization_signature(attacker_signed, _KEYS["public"])

    # ...and the production apply path refuses it.
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), attacker_signed)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(attacker_signed, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_missing_trusted_public_key_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    with pytest.raises(SignatureVerificationError):
        verify_authorization_signature(artifact, None)
    with pytest.raises(SignatureVerificationError):
        load_trusted_public_key("")
    with pytest.raises(SignatureVerificationError):
        load_trusted_public_key(str(tmp_path / "does-not-exist.pem"))

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir, workspace=str(tmp_path / "ws"), migration_id=artifact["migration_id"]
    )
    with pytest.raises(SignatureVerificationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_wrong_trusted_public_key_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    with pytest.raises(SignatureVerificationError):
        verify_authorization_signature(artifact, _KEYS["other_public"])

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id=artifact["migration_id"],
        trusted_public_key=_KEYS["other_public"],
    )
    with pytest.raises(SignatureVerificationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_tampered_source_hash_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(
            tampered(artifact, expected_source_sha256="f" * 64),
            confirm_apply=True,
            operator=OPERATOR,
        )
    assert snapshot_dir_hashes(data_dir) == before


def test_tampered_candidate_hash_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(
            tampered(artifact, expected_candidate_sha256="f" * 64),
            confirm_apply=True,
            operator=OPERATOR,
        )
    assert snapshot_dir_hashes(data_dir) == before


def test_tampered_source_record_count_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(SignatureVerificationError):
        repairer.apply(
            tampered(artifact, expected_source_total=42),
            confirm_apply=True,
            operator=OPERATOR,
        )
    assert snapshot_dir_hashes(data_dir) == before


def test_invalid_migration_id_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir,
        workspace=str(tmp_path / "ws"),
        migration_id="MIG-SOMEONE-ELSES",
        trusted_public_key=_KEYS["public"],
    )
    with pytest.raises(AuthorizationError, match="Migration ID mismatch"):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_owner_private_key_inside_repository_is_refused(tmp_path):
    """A production private signing key must never live in the repository."""
    from tools.production_canonical_repair import REPO_ROOT

    with pytest.raises(AuthorizationError, match="inside the repository"):
        generate_signing_keypair(
            os.path.join(REPO_ROOT, "leaked-private.pem"),
            os.path.join(str(tmp_path), "public.pem"),
        )


# ===========================================================================
# 4. Operator identity binding
# ===========================================================================

def test_missing_operator_is_rejected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    for missing in (None, "", "   "):
        with pytest.raises(AuthorizationError, match="explicit --operator"):
            repairer.apply(artifact, confirm_apply=True, operator=missing)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_operator_mismatch(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(AuthorizationError, match="Operator mismatch"):
        repairer.apply(artifact, confirm_apply=True, operator="someone-else")
    assert snapshot_dir_hashes(data_dir) == before


# ===========================================================================
# 5. Dry run
# ===========================================================================

def test_dry_run_is_read_only_and_predicts_n_minus_1(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir, workspace=str(tmp_path / "ws"), writable=False
    )
    preview = repairer.dry_run(artifact)

    assert preview["read_only_data_dir"] is True
    assert preview["writes_external_artifact"] is True
    assert preview["after_total"] == preview["before_total"] - 1
    assert preview["candidate_sha256"] == artifact["expected_candidate_sha256"]
    assert preview["authorization_matches_candidate"] is True
    assert snapshot_dir_hashes(data_dir) == before


def test_dry_run_accepts_an_unsigned_preparation_artifact(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    prepared = make_preparation(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir, workspace=str(tmp_path / "ws"), writable=False
    )
    preview = repairer.dry_run(prepared)

    assert preview["status"] == "DRY_RUN_COMPLETED"
    assert preview["authorization_signature_verified"] is False
    assert snapshot_dir_hashes(data_dir) == before


# ===========================================================================
# 6. Apply guards
# ===========================================================================

def test_apply_requires_confirm_flag(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(AuthorizationError):
        repairer.apply(artifact, confirm_apply=False, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_artifact_bound_to_other_data_dir(tmp_path):
    data_dir_a = build_dataset(str(tmp_path / "a"))
    data_dir_b = build_dataset(str(tmp_path / "b"))
    artifact = make_authorization(data_dir_a)

    repairer = make_repairer(data_dir_b, str(tmp_path / "ws"), artifact)
    with pytest.raises(AuthorizationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert inspect_dataset(data_dir_b)["duplicate_ids"] == {TARGET_ID: 2}


def test_apply_rejects_stale_source_hash(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    # Dataset drifts after authorization was approved.
    path = os.path.join(data_dir, CANONICAL_FILE)
    payload = json.load(open(path, encoding="utf-8"))
    payload["matches"][0]["home"] = "改名后的主队"
    write_json_atomic(path, payload)
    drifted = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == drifted


def test_apply_rejects_unexpected_duplicate_groups(tmp_path):
    data_dir = build_dataset(str(tmp_path), extra_dup_group=True)
    artifact = manual_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_three_occurrences(tmp_path):
    data_dir = build_dataset(str(tmp_path), target_occurrences=3)
    artifact = manual_authorization(data_dir)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(PreconditionViolationError):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_rejects_source_count_drift(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = manual_authorization(data_dir, expected_source_total=99)
    before = snapshot_dir_hashes(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with pytest.raises(PreconditionViolationError, match="record count"):
        repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert snapshot_dir_hashes(data_dir) == before


def test_apply_lock_contention_fails_closed(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)

    with acquire_repair_lock(data_dir):
        with pytest.raises(StagingLockError):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)


# ===========================================================================
# 7. Happy path
# ===========================================================================

def test_apply_happy_path_enforces_every_postcondition(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    before_stores = snapshot_dir_hashes(data_dir)
    original = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))

    repairer, result = apply_artifact(tmp_path, data_dir, artifact)

    assert result["status"] == "SUCCESS"
    assert result["operator"] == OPERATOR
    assert result["authorization_key_id"] == load_trusted_public_key(_KEYS["public"])["key_id"]
    assert result["before_total"] == original["meta"]["total"]
    assert result["after_total"] == original["meta"]["total"] - 1
    assert result["duplicate_ids_count"] == 0
    assert result["immutable_stores_changed"] == 0
    assert result["candidate_sha256"] == artifact["expected_candidate_sha256"]

    report = inspect_dataset(data_dir)
    assert report["duplicate_ids"] == {}
    assert report["total_matches"] == report["unique_ids_count"] == result["after_total"]

    current = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    assert current["meta"]["total"] == result["after_total"]
    assert current["meta"]["last_refresh"] == original["meta"]["last_refresh"]
    assert current["meta"]["sources"] == original["meta"]["sources"]
    assert current["schema_version"] == original["schema_version"]

    # Unrelated records preserved byte-for-byte, in the original relative order.
    before_unrelated = [m for m in original["matches"] if m.get("id") != TARGET_ID]
    after_unrelated = [m for m in current["matches"] if m.get("id") != TARGET_ID]
    assert before_unrelated == after_unrelated

    # Target appears once, at the position of the first duplicate.
    assert [m["id"] for m in current["matches"]].count(TARGET_ID) == 1
    assert hash_record([m for m in current["matches"] if m["id"] == TARGET_ID][0]) == result["candidate_sha256"]

    # Six non-Canonical stores untouched; only the Canonical file changed.
    after_stores = snapshot_dir_hashes(data_dir)
    for store in NON_CANONICAL_STORES:
        assert after_stores[store] == before_stores[store]
    assert after_stores[CANONICAL_FILE] != before_stores[CANONICAL_FILE]

    # Journal completed the full seven-phase sequence.
    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states == JOURNAL_STATES

    assert os.path.exists(result["result_file"])
    audit = repairer.verify(artifact, trusted_public_key=_KEYS["public"])
    assert audit["verified"] is True
    assert audit["complete"] is True
    assert audit["verification"] == FULL_REPAIR_VERIFICATION


def test_apply_is_idempotent(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer, _result = apply_artifact(tmp_path, data_dir, artifact)

    journal_before = repairer.journal.read_entries()
    hashes_before = snapshot_dir_hashes(data_dir)

    second = repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)

    assert second["status"] == "NO_OP_ALREADY_REPAIRED"
    assert snapshot_dir_hashes(data_dir) == hashes_before
    assert repairer.journal.read_entries() == journal_before

    # Repeated verification stays green and writes nothing.
    for _ in range(2):
        assert repairer.verify(artifact, trusted_public_key=_KEYS["public"])["verified"] is True
    assert snapshot_dir_hashes(data_dir) == hashes_before


def test_derived_hash_matches_on_disk_bytes(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    on_disk = hashlib.sha256(open(os.path.join(data_dir, CANONICAL_FILE), "rb").read()).hexdigest()
    assert derive_file_hash(payload) == on_disk


# ===========================================================================
# 8. Immutable store protection
# ===========================================================================

def test_immutable_store_mutation_is_detected(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)

    with fault_injection_enabled():
        with pytest.raises(ImmutableStoreMutationError):
            repairer.apply(
                artifact,
                confirm_apply=True,
                operator=OPERATOR,
                fault_inject="corrupt_store_pre_postconditions",
            )

    # The projection was committed but completion was correctly withheld.
    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states[-1] == "PROJECTION_COMMITTED"
    assert "COMPLETION_RECORDED" not in states

    # Recovery must also fail closed rather than paper over the mutation.
    with pytest.raises(ImmutableStoreMutationError):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])


# ===========================================================================
# 9. Verification contract: inspection vs. full repair verification
# ===========================================================================

def test_basic_inspection_is_never_reported_as_full_repair_verification(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    repairer = ProductionCanonicalRepairer(
        data_dir=data_dir, workspace=str(tmp_path / "ws"), writable=False
    )

    report = repairer.verify()
    assert report["verification"] == DATASET_INTEGRITY_INSPECTION
    assert report["verified"] is False
    assert report["complete"] is False
    assert report["repair_verified"] is False

    # inspect_dataset itself is labelled as the weaker contract.
    assert inspect_dataset(data_dir)["verification"] == DATASET_INTEGRITY_INSPECTION


def test_full_repair_verification_reports_incomplete_when_evidence_is_missing(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer, _result = apply_artifact(tmp_path, data_dir, artifact)

    assert repairer.verify(artifact, trusted_public_key=_KEYS["public"])["verified"] is True

    # Remove the forensic archive: the audit must report INCOMPLETE, not a pass.
    for name in os.listdir(repairer.archive_dir):
        os.unlink(os.path.join(repairer.archive_dir, name))

    audit = repairer.verify(artifact, trusted_public_key=_KEYS["public"])
    assert audit["verified"] is False
    assert audit["complete"] is False
    assert audit["verification"] == FULL_REPAIR_VERIFICATION
    assert "forensic_archive_raw" in audit["error"]


def test_full_repair_verification_requires_the_signed_authorization(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    # Prepared while the collision still exists, so it is a genuine artifact.
    unsigned = make_preparation(data_dir)
    artifact = sign_authorization(unsigned, _KEYS["private"])
    repairer, _result = apply_artifact(tmp_path, data_dir, artifact)

    audit = repairer.verify(unsigned, trusted_public_key=_KEYS["public"])
    assert audit["verified"] is False
    assert audit["complete"] is False
    assert audit["verification"] == FULL_REPAIR_VERIFICATION

    # An unsigned artifact is an explicit authorization failure, never a pass.
    with pytest.raises(SignatureVerificationError):
        repairer.verify_completed_dataset(unsigned, trusted_public_key=_KEYS["public"])


# ===========================================================================
# 10. Crash recovery
# ===========================================================================

def test_recover_requires_the_signed_authorization(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    with pytest.raises(AuthorizationError, match="signed authorization artifact"):
        repairer.recover()


def test_recover_rejects_a_foreign_authorization(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    foreign = make_authorization(data_dir, migration_id="MIG-SOMETHING-ELSE")

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    with pytest.raises(AuthorizationError):
        repairer.recover(foreign, operator=OPERATOR, trusted_public_key=_KEYS["public"])


def test_recover_rejects_a_forged_journal_fingerprint(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_replace_before_committed")

    # Rewrite the durable commit intent with a fingerprint the archive cannot
    # possibly produce.
    entries = repairer.journal.read_entries()
    for entry in entries:
        if entry["state"] == "COMMIT_INTENT_DURABLE":
            entry["payload"]["projected_file_sha256"] = "a" * 64
    with open(repairer.journal.journal_file, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    with pytest.raises(RecoveryStateError, match="independently derived"):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])


def test_recover_rejects_an_invalid_journal_chain(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    entries = repairer.journal.read_entries()
    # Drop the EVIDENCE_VERIFIED entry: the state machine skips a phase.
    entries = [e for e in entries if e["state"] != "EVIDENCE_VERIFIED"]
    with open(repairer.journal.journal_file, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    with pytest.raises(RecoveryStateError):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])


def test_recover_fails_closed_when_forensic_evidence_is_missing(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    baseline = open(os.path.join(data_dir, CANONICAL_FILE), "rb").read()

    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    import shutil
    shutil.rmtree(repairer.archive_dir)

    with pytest.raises(RecoveryStateError, match="Missing raw archive"):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])
    assert open(os.path.join(data_dir, CANONICAL_FILE), "rb").read() == baseline


def test_recover_rolls_back_from_commit_intent_then_apply_succeeds(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    baseline_bytes = open(os.path.join(data_dir, CANONICAL_FILE), "rb").read()

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    assert open(os.path.join(data_dir, CANONICAL_FILE), "rb").read() == baseline_bytes

    recovery = repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])
    assert recovery["status"] == "RECOVERED_ROLLED_BACK"
    assert open(os.path.join(data_dir, CANONICAL_FILE), "rb").read() == baseline_bytes

    result = repairer.apply(artifact, confirm_apply=True, operator=OPERATOR)
    assert result["status"] == "SUCCESS"
    assert inspect_dataset(data_dir)["duplicate_ids"] == {}
    assert repairer.verify(artifact, trusted_public_key=_KEYS["public"])["verified"] is True


def test_recover_completes_forward_when_projection_already_written(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_replace_before_committed")

    states = [e["state"] for e in repairer.journal.read_entries()]
    assert states[-1] == "COMMIT_INTENT_DURABLE"
    assert inspect_dataset(data_dir)["duplicate_ids"] == {}

    recovery = repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])
    assert recovery["status"] == "RECOVERED_FORWARD_COMPLETED"
    assert [e["state"] for e in repairer.journal.read_entries()][-1] == "COMPLETION_RECORDED"
    assert repairer.verify(artifact, trusted_public_key=_KEYS["public"])["verified"] is True


def test_recover_does_not_record_a_false_completion(tmp_path):
    """A projection that is not the authorized one must never be 'completed'."""
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_replace_before_committed")

    # Tamper with the committed projection so it no longer matches the derived one.
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    payload["matches"][0]["home"] = "被篡改的主队"
    write_json_atomic(os.path.join(data_dir, CANONICAL_FILE), payload)

    with pytest.raises(RecoveryStateError):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])

    states = [e["state"] for e in repairer.journal.read_entries()]
    assert "COMPLETION_RECORDED" not in states


def test_recover_fails_closed_on_unknown_state(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)

    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    with fault_injection_enabled():
        with pytest.raises(Exception):
            repairer.apply(artifact, confirm_apply=True, operator=OPERATOR,
                           fault_inject="after_commit_intent")

    # Corrupt the active file into a state matching neither baseline nor projection.
    payload = json.load(open(os.path.join(data_dir, CANONICAL_FILE), encoding="utf-8"))
    payload["matches"] = payload["matches"][:2]
    write_json_atomic(os.path.join(data_dir, CANONICAL_FILE), payload)

    with pytest.raises(RecoveryStateError):
        repairer.recover(artifact, operator=OPERATOR, trusted_public_key=_KEYS["public"])


def test_recover_without_journal_is_a_no_op(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    repairer = ProductionCanonicalRepairer(data_dir=data_dir, workspace=str(tmp_path / "ws"))
    assert repairer.recover()["status"] == "NO_JOURNAL_ENTRIES"


# ===========================================================================
# 11. CLI surface
# ===========================================================================

def test_cli_prepare_approve_apply_verify_roundtrip(tmp_path, capsys):
    from tools.production_canonical_repair import main

    data_dir = build_dataset(str(tmp_path))
    prepared_path = str(tmp_path / "prepared.json")
    auth_path = str(tmp_path / "authorization.json")
    workspace = str(tmp_path / "ws")
    public = _KEYS["public"]

    assert main(["inspect", "--data-dir", data_dir]) == 0
    capsys.readouterr()

    assert main([
        "prepare", "--data-dir", data_dir, "--target-id", TARGET_ID,
        "--operator", OPERATOR, "--reason", "cli roundtrip", "--out", prepared_path,
    ]) == 0
    capsys.readouterr()
    assert os.path.exists(prepared_path)
    assert json.load(open(prepared_path, encoding="utf-8"))["authorized"] is False

    # An unsigned preparation artifact cannot be applied.
    assert main([
        "apply", "--data-dir", data_dir, "--authorization", prepared_path,
        "--workspace", workspace, "--operator", OPERATOR,
        "--trusted-public-key", public, "--confirm-apply",
    ]) == 112
    capsys.readouterr()

    assert main([
        "approve", "--prepared", prepared_path, "--private-key", _KEYS["private"],
        "--out", auth_path,
    ]) == 0
    capsys.readouterr()
    assert json.load(open(auth_path, encoding="utf-8"))["authorized"] is True

    assert main([
        "verify-authorization", "--authorization", auth_path,
        "--trusted-public-key", public,
    ]) == 0
    capsys.readouterr()

    assert main([
        "dry-run", "--data-dir", data_dir, "--authorization", auth_path,
        "--workspace", workspace, "--trusted-public-key", public,
    ]) == 0
    capsys.readouterr()

    assert main([
        "apply", "--data-dir", data_dir, "--authorization", auth_path,
        "--workspace", workspace, "--operator", OPERATOR,
        "--trusted-public-key", public, "--confirm-apply",
    ]) == 0
    capsys.readouterr()

    assert main([
        "verify", "--data-dir", data_dir, "--authorization", auth_path,
        "--trusted-public-key", public, "--operator", OPERATOR, "--workspace", workspace,
    ]) == 0
    capsys.readouterr()

    # Applying without --confirm-apply is refused with the authorization exit code.
    assert main([
        "apply", "--data-dir", data_dir, "--authorization", auth_path, "--workspace", workspace,
        "--operator", OPERATOR, "--trusted-public-key", public,
    ]) == 110


def test_cli_has_no_fault_injection_option(tmp_path, capsys):
    from tools.production_canonical_repair import main

    data_dir = build_dataset(str(tmp_path))
    prepared_path = str(tmp_path / "prepared.json")
    auth_path = str(tmp_path / "authorization.json")
    main([
        "prepare", "--data-dir", data_dir, "--target-id", TARGET_ID,
        "--operator", OPERATOR, "--reason", "fault hook", "--out", prepared_path,
    ])
    main(["approve", "--prepared", prepared_path, "--private-key", _KEYS["private"],
          "--out", auth_path])
    capsys.readouterr()

    before = snapshot_dir_hashes(data_dir)
    with pytest.raises(SystemExit) as exc:
        main([
            "apply", "--data-dir", data_dir, "--authorization", auth_path,
            "--workspace", str(tmp_path / "ws"), "--operator", OPERATOR,
            "--trusted-public-key", _KEYS["public"], "--confirm-apply",
            "--fault-inject", "corrupt_store_pre_postconditions",
        ])
    assert exc.value.code == 2
    capsys.readouterr()
    assert snapshot_dir_hashes(data_dir) == before


@pytest.mark.parametrize(
    "flag",
    [
        "--force",
        "--skip-verification",
        "--ignore-hash",
        "--allow-unsigned",
        "--allow-production-without-approval",
        "--fault-inject",
    ],
)
def test_cli_rejects_unsupported_mutation_switches(tmp_path, capsys, flag):
    from tools.production_canonical_repair import main

    data_dir = build_dataset(str(tmp_path))
    prepared_path = str(tmp_path / "prepared.json")
    auth_path = str(tmp_path / "authorization.json")
    main([
        "prepare", "--data-dir", data_dir, "--target-id", TARGET_ID,
        "--operator", OPERATOR, "--reason", "bypass probe", "--out", prepared_path,
    ])
    main(["approve", "--prepared", prepared_path, "--private-key", _KEYS["private"],
          "--out", auth_path])
    capsys.readouterr()

    before = snapshot_dir_hashes(data_dir)
    with pytest.raises(SystemExit) as exc:
        main([
            "apply", "--data-dir", data_dir, "--authorization", auth_path,
            "--workspace", str(tmp_path / "ws"), "--operator", OPERATOR,
            "--trusted-public-key", _KEYS["public"], "--confirm-apply", flag,
        ])
    assert exc.value.code == 2
    capsys.readouterr()
    assert snapshot_dir_hashes(data_dir) == before
    assert inspect_dataset(data_dir)["duplicate_ids"] == {TARGET_ID: 2}


def test_fault_injection_gate_blocks_direct_api_use(tmp_path):
    """Even a Python caller cannot arm the destructive hooks by accident."""
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    repairer = make_repairer(data_dir, str(tmp_path / "ws"), artifact)
    before = snapshot_dir_hashes(data_dir)

    with pytest.raises(StagingMigrationError, match="test-only facility"):
        repairer.apply(
            artifact, confirm_apply=True, operator=OPERATOR,
            fault_inject="corrupt_store_pre_postconditions",
        )
    assert snapshot_dir_hashes(data_dir) == before


def test_cli_verify_requires_a_signed_authorization(tmp_path, capsys):
    from tools.production_canonical_repair import main

    data_dir = build_dataset(str(tmp_path))
    prepared_path = str(tmp_path / "prepared.json")
    main([
        "prepare", "--data-dir", data_dir, "--target-id", TARGET_ID,
        "--operator", OPERATOR, "--reason", "verify probe", "--out", prepared_path,
    ])
    capsys.readouterr()

    # Signature failure is reported on the signature exit code.
    assert main([
        "verify", "--data-dir", data_dir, "--authorization", prepared_path,
        "--trusted-public-key", _KEYS["public"], "--operator", OPERATOR,
    ]) == 112
    capsys.readouterr()


# ===========================================================================
# 12. Production isolation
# ===========================================================================

def test_repo_canonical_data_is_never_touched(tmp_path):
    """The suite operates on synthetic fixtures only."""
    repo_data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    live = os.path.join(repo_data, CANONICAL_FILE)
    if os.path.exists(live):
        before = hashlib.sha256(open(live, "rb").read()).hexdigest()
    else:
        before = None

    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    apply_artifact(tmp_path, data_dir, artifact)

    if before is None:
        assert not os.path.exists(live)
    else:
        assert hashlib.sha256(open(live, "rb").read()).hexdigest() == before


def test_authorization_fingerprint_covers_the_approval_block(tmp_path):
    data_dir = build_dataset(str(tmp_path))
    artifact = make_authorization(data_dir)
    assert authorization_fingerprint(artifact) != authorization_fingerprint(
        tampered(artifact, expected_candidate_sha256="0" * 64)
    )
