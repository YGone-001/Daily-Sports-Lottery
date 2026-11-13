"""Executable synthetic bootstrap evidence RB-07..35 and adversarial controls."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import importlib.util
import os
from pathlib import Path
import subprocess
from io import BytesIO
import tarfile

import pytest

from tools.trust_root_bootstrap_validation import (
    SyntheticRootFixture, RoleSeparatedShadowScenario, baseline_reproduction, run_acceptance,
    NOW, ENVIRONMENT, DATASET, ROLE_PURPOSES,
)
from tools.shadow_trust_chain_validation import production_boundary_audit
from tools.shadow_trusted_time_validation import protected_store_hashes
from utils.source_identity import fingerprint
from utils.trusted_artifacts import TrustedArtifactError, sign_trusted_artifact
from utils.trusted_time_authority import sign_time_attestation, parse_utc
from utils.trust_root_bootstrap import (
    APPROVAL, BUNDLE, TRANSITION, FixedAuthorityContext, TrustRootVerifier,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "4f4da993a27dc0c5362f38bc228b463ba364999b"
SCOPE_VERSION = "offline-root-pilot-v9-review-evidence-hardening"
ALLOWED_PATHS = frozenset((
    # trust-root pilot scope (unchanged)
    "utils/trust_root_bootstrap.py",
    "utils/trusted_artifacts.py",
    "tools/trust_root_bootstrap_validation.py",
    "tests/test_trust_root_bootstrap.py",
    "tests/test_production_trust_contracts.py",
    ".github/workflows/ci.yml",
    "docs/security/runtime-activation-offline-trust-bootstrap.md",
    "docs/security/runtime-activation-production-readiness-matrix.md",
    # read-only real sports identity audit scope
    "tools/real_sports_identity_audit.py",
    "tests/test_real_sports_identity_audit.py",
    "docs/analysis/real-sports-identity-mapping-evidence.md",
    "docs/analysis/real-sports-mapping-business-value.md",
    # strong-number conflict identity scoping
    "AGENTS.md",
    "utils/source_identity.py",
    "tests/test_strong_number_conflict_scoping.py",
    "docs/source-identity-provenance.md",
    # offline duplicate review and reconciliation preview
    "tools/real_sports_duplicate_review.py",
    "tests/test_real_sports_duplicate_review.py",
    "docs/duplicate-review-workflow.md",
    # basketball lottery synchronization
    "utils/fetcher_500.py",
    "utils/scraper.py",
    "utils/daily_loader.py",
    "utils/match_identity.py",
    "utils/match_lifecycle.py",
    "utils/nba_schedule.py",
    "app.py",
    "tests/test_basketball_sync_integrity.py",
    "tests/test_nba_schedule_binding.py",
    "tests/test_capture_time_integrity.py",
    "tests/test_snapshot_contract_corrections.py",
    "tests/test_balldontlie_schedule_adapter.py",
    "frontend/src/features/DailyView/DailyView.tsx",
    "frontend/src/features/DailyView/dateSelection.ts",
    "frontend/src/features/DailyView/dateSelection.test.ts",
    "frontend/src/types/match.ts",
    # basketball operational acceptance
    "tests/test_basketball_operational_acceptance.py",
    # canonical ingestion ownership regression
    "tests/test_canonical_ingestion_guard.py",
    # nba cross-source identity discrepancy evidence
    "utils/basketball_identity_evidence.py",
    "tools/nba_identity_discrepancy_audit.py",
    "tests/test_nba_cross_source_identity.py",
    # nba human review workbench
    "tools/nba_human_review_workbench.py",
    "tests/test_nba_human_review_workbench.py",
    # refresh writer ownership and cross-store consistency
    "utils/refresh_state.py",
    "utils/concurrency.py",
    "tests/test_concurrency.py",
    "tests/test_canonical_mapping_governance.py",
    # durable local human-review evidence
    "utils/evidence_store.py",
    "tests/test_refresh_ownership_and_evidence.py",
    # cross-process evidence and snapshot write ownership corrections
    "utils/prediction_snapshots.py",
    "utils/odds_snapshots.py",
    "tests/test_prediction_snapshots.py",
    "tests/test_odds_snapshots.py",
    "tests/test_evidence_concurrency_and_consistency.py",
    # persisted evidence reconciliation against the current candidate dataset
    "tests/test_persisted_evidence_reconciliation.py",
    # reconciliation and persistence result-integrity corrections
    "tests/test_persisted_evidence_result_integrity.py",
    # evidence local-date vs Beijing-date conversion regression
    "tests/test_evidence_date_semantics.py",
    # refresh-readiness and durable nba supply diagnostics
    "docs/ingestion-identity-preflight.md"))


@pytest.fixture(scope="session", autouse=True)
def protected_bytes():
    before = protected_store_hashes()
    yield before
    assert protected_store_hashes() == before


@pytest.fixture
def root(tmp_path):
    return SyntheticRootFixture(tmp_path / "root")


def expect(code, call):
    with pytest.raises(TrustedArtifactError) as raised:
        call()
    assert code in raised.value.rejection_codes


def bootstrap(root, artifact=None, approval=None, policy=None, continuity=None):
    verifier = TrustRootVerifier(policy or root.policy)
    return verifier, verifier.bootstrap(artifact or root.initial,
        approval=approval or root.initial_approval, current_time=NOW,
        continuity=continuity or root.initial_continuity)


def altered(root, change):
    body = deepcopy(root.initial_body)
    change(body)
    body["bundle_fingerprint"] = fingerprint({k: v for k, v in body.items() if k != "bundle_fingerprint"})
    return root.sign_bundle(body, root.private["root-bundle"]), root.approve(None, body)


def test_rb01_through_rb06_original_boundaries(tmp_path):
    result = baseline_reproduction(tmp_path / "baseline")
    assert set(result) == {"RB-" + str(i).zfill(2) for i in range(1, 7)}


def test_rb07_valid_independently_pinned_root(root):
    assert root.verifier.status == "BOOTSTRAP_NOT_ESTABLISHED"
    handle = root.bootstrap()
    assert root.verifier.status == "BOOTSTRAP_VERIFIED"
    assert handle.body()["bundle_fingerprint"] == root.initial_continuity.expected_bundle_fingerprint
    assert root.fixed.verify_artifact("governance-view", root.evidence(), current_time=NOW)["dataset_identity"] == DATASET
    handle.body()["root_generation"] = 999
    assert handle.body()["root_generation"] == 1
    with pytest.raises(TypeError): root.fixed.key_paths["activation"] = "caller.pem"
    with pytest.raises(FrozenInstanceError): root.fixed.dataset_identity = "caller"


def test_rb08_missing_independent_pin(root):
    expect("BOOTSTRAP_NOT_ESTABLISHED", lambda: bootstrap(root, policy=replace(root.policy, initial_root_fingerprint=None)))


def test_rb09_wrong_pin(root):
    expect("ROOT_UNTRUSTED", lambda: bootstrap(root, policy=replace(root.policy, initial_root_fingerprint=root.approval_pin)))


def test_rb10_embedded_root_cannot_authenticate_itself(root):
    other = root.next_bundle()
    approval = root.approve(None, other["body"])
    expect("ROOT_UNTRUSTED", lambda: bootstrap(root, other["bundle"], approval=approval, continuity=other["continuity"]))


@pytest.mark.parametrize("field,code", [("environment_identity", "ROOT_ENVIRONMENT_MISMATCH"),
    ("dataset_identity", "ROOT_DATASET_MISMATCH")])
def test_rb11_rb12_scope_bound_in_signed_bundle(root, field, code):
    artifact, approval = altered(root, lambda b: b.update({field: "different-scope"}))
    expect(code, lambda: bootstrap(root, artifact, approval))


def test_rb13_wrong_purpose(root):
    root.bootstrap()
    expect("ROOT_PURPOSE_INVALID", lambda: root.fixed.verify_artifact("governance-view", root.evidence("recovery"), current_time=NOW))


def test_rb14_wrong_role_signer(root):
    root.bootstrap()
    evidence = root.evidence()
    wrong = sign_trusted_artifact(root.private["trusted-time"], artifact_type=evidence.artifact_type, payload=evidence.payload())
    expect("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED", lambda: root.fixed.verify_artifact("governance-view", wrong, current_time=NOW))


def test_rb15_new_root_requires_transition(root):
    root.bootstrap(); new = root.next_bundle()
    expect("ROOT_TRANSITION_REQUIRED", lambda: root.verifier.bootstrap(new["bundle"], approval=new["approval"],
        continuity=new["continuity"], current_time=NOW))
    expect("ROOT_TRANSITION_REQUIRED", lambda: root.rotate(new, transition=None))


def test_rb16_signed_but_wrong_transition_rejects(root):
    root.bootstrap(); new = root.next_bundle()
    wrong = sign_trusted_artifact(root.private["root-bundle"], artifact_type=TRANSITION,
        payload={**new["transition"].payload(), "new_bundle_identity": "wrong"})
    expect("ROOT_TRANSITION_INVALID", lambda: root.rotate(new, transition=wrong))
    assert root.verifier.status == "BOOTSTRAP_VERIFIED" and root.handle.body()["root_generation"] == 1


def test_rb17_old_signer_cannot_approve_its_own_rotation(root):
    root.bootstrap(); new = root.next_bundle()
    expect("ROOT_APPROVAL_MISSING", lambda: root.rotate(new, approval=None))
    forged = sign_trusted_artifact(root.private["root-bundle"], artifact_type=APPROVAL, payload=new["approval"].payload())
    expect("TRUSTED_ARTIFACT_SIGNER_UNTRUSTED", lambda: root.rotate(new, approval=forged))


def test_rb18_approved_rotation_and_old_handle_invalidation(root):
    root.bootstrap(); new = root.next_bundle()
    current = FixedAuthorityContext(root.rotate(new), key_paths=new["paths"])
    assert current.bootstrap.body()["root_generation"] == 2
    artifact = sign_trusted_artifact(new["private"]["governance-view"],
        artifact_type=ROLE_PURPOSES["governance-view"][0], payload=root.evidence().payload())
    assert current.verify_artifact("governance-view", artifact, current_time=NOW)
    expect("ROOT_VERSION_ROLLBACK", lambda: root.fixed.verify_artifact("governance-view", root.evidence(), current_time=NOW))


def test_rb19_previous_root_key_cannot_return(root):
    root.bootstrap(); new = root.next_bundle(); root.rotate(new)
    paths = {**new["paths"], "root-bundle": root.public["root-bundle"]}
    body = root.body(paths, generation=3, predecessor=new["body"]["bundle_fingerprint"], revoked=new["body"]["revoked_key_identities"])
    artifact = root.sign_bundle(body, root.private["root-bundle"])
    expect("ROOT_REVOKED", lambda: root.verifier.transition(artifact, transition=new["transition"], approval=new["approval"],
        new_root_public_key_path=paths["root-bundle"], continuity=root.anchor(body), current_time=NOW))


def test_rb20_old_bundle_replay(root):
    root.bootstrap(); root.rotate(root.next_bundle())
    expect("ROOT_VERSION_ROLLBACK", lambda: root.verifier.bootstrap(root.initial, approval=root.initial_approval,
        continuity=root.initial_continuity, current_time=NOW))


@pytest.mark.parametrize("attack", ["truncate", "tamper", "invalid-signature"])
def test_rb21_bundle_tampering(root, attack):
    body = deepcopy(root.initial_body)
    if attack == "truncate":
        body.pop("issuer_identity"); expected = "ROOT_BUNDLE_INVALID"
    elif attack == "tamper":
        body["bundle_identity"] = "tampered"; expected = "ROOT_BUNDLE_TAMPERED"
    else:
        artifact = replace(root.initial, signature="AA==")
        expect("TRUSTED_ARTIFACT_SIGNATURE_INVALID", lambda: bootstrap(root, artifact)); return
    artifact = root.sign_bundle(body, root.private["root-bundle"])
    expect(expected, lambda: bootstrap(root, artifact))


@pytest.mark.parametrize("attack,code", [("duplicate-role", "ROOT_ROLE_CONFLICT"),
    ("duplicate-authority", "ROOT_AUTHORITY_DUPLICATE"), ("shared-key", "ROOT_ROLE_CONFLICT"),
    ("missing-role", "ROOT_MANDATORY_ROLE_MISSING"), ("malformed-key", "ROOT_KEY_MALFORMED"),
    ("wrong-fingerprint", "ROOT_KEY_FINGERPRINT_MISMATCH"), ("unknown-schema", "ROOT_SCHEMA_UNSUPPORTED"),
    ("unexpected-purpose", "ROOT_PURPOSE_INVALID"), ("unknown-role", "ROOT_ROLE_CONFLICT")])
def test_rb22_bundle_structural_security(root, attack, code):
    def change(body):
        records = body["authorities"]
        if attack == "duplicate-role":
            records[0]["signer_role"] = records[1]["signer_role"]
            records[0]["authorized_purposes"] = list(records[1]["authorized_purposes"])
        elif attack == "duplicate-authority": records[0]["authority_identity"] = records[1]["authority_identity"]
        elif attack == "shared-key":
            for f in ("public_key_pem", "public_key_fingerprint"): records[0][f] = records[1][f]
        elif attack == "missing-role": records[:] = [r for r in records if r["signer_role"] != "owner-approval"]
        elif attack == "malformed-key": records[0]["public_key_pem"] = "not a key"
        elif attack == "wrong-fingerprint": records[0]["public_key_fingerprint"] = "sha256:" + "0" * 64
        elif attack == "unknown-schema": body["schema_version"] = "999"
        elif attack == "unexpected-purpose": records[0]["authorized_purposes"].append("arbitrary")
        elif attack == "unknown-role": records[0]["signer_role"] = "unregistered"
    artifact, approval = altered(root, change)
    expect(code, lambda: bootstrap(root, artifact, approval))


def test_rb23_revoked_role_key_and_root(root):
    root.bootstrap(); new = root.next_bundle(revoked_roles=("audit-checkpoint",))
    current = FixedAuthorityContext(root.rotate(new), key_paths=new["paths"])
    expect("ROOT_REVOKED", lambda: current.verify_artifact("audit-checkpoint", root.evidence("audit-checkpoint"), current_time=NOW))
    revoked, approval = altered(root, lambda b: b["revoked_key_identities"].append(root.pin))
    expect("ROOT_REVOKED", lambda: bootstrap(root, revoked, approval))


def test_rb24_historical_signature_does_not_authorize(root):
    root.bootstrap(); artifact = root.evidence(); root.rotate(root.next_bundle())
    result = root.verifier.inspect_historical(root.initial_body["bundle_fingerprint"], "governance-view", artifact,
        public_key_path=root.public["governance-view"], current_time=NOW)
    assert result["cryptographically_valid"] and result["revoked_in_current_bundle"]
    assert result["current_authorization_eligible"] is False


def test_rb25_missing_or_lost_continuity(root):
    expect("CONTINUITY_UNVERIFIED", lambda: root.verifier.bootstrap(root.initial, approval=root.initial_approval,
        continuity=None, current_time=NOW))
    root.bootstrap(); root.verifier._watermark = None
    assert root.verifier.status == "CONTINUITY_UNVERIFIED"
    expect("CONTINUITY_UNVERIFIED", lambda: root.fixed.key_path("activation", ROLE_PURPOSES["activation"][0], current_time=NOW))
    expect("CONTINUITY_UNVERIFIED", lambda: root.verifier.bootstrap(root.initial, approval=root.initial_approval,
        continuity=root.initial_continuity, current_time=NOW))


def test_rb26_restart_requires_independent_current_expectation(root):
    root.bootstrap(); new = root.next_bundle(); root.rotate(new)
    restarted = TrustRootVerifier(root.policy)
    expect("ROOT_VERSION_ROLLBACK", lambda: restarted.bootstrap(root.initial, approval=root.initial_approval,
        continuity=new["continuity"], current_time=NOW))
    expect("ROOT_UNTRUSTED", lambda: restarted.bootstrap(new["bundle"], approval=new["approval"],
        continuity=new["continuity"], current_time=NOW))
    policy = replace(root.policy, initial_root_public_key_path=new["paths"]["root-bundle"],
        initial_root_fingerprint=new["bundle"].signer_public_key_id, minimum_accepted_version=2)
    current = TrustRootVerifier(policy).bootstrap(new["bundle"], approval=root.approve(None, new["body"]),
        continuity=new["continuity"], current_time=NOW)
    assert current.body()["root_generation"] == 2


def test_rb27_unverified_bootstrap_and_supplied_root_override(root):
    expect("BOOTSTRAP_NOT_ESTABLISHED", lambda: FixedAuthorityContext(None, key_paths=root.public))
    root.bootstrap()
    expect("ROOT_UNTRUSTED", lambda: FixedAuthorityContext(root.handle,
        key_paths={**root.public, "activation": root.public["governance-view"]}))


@pytest.fixture(scope="module")
def strict_scene(tmp_path_factory):
    before = protected_store_hashes()
    path = tmp_path_factory.mktemp("fixed-root-strict")
    scene = RoleSeparatedShadowScenario(path / "scene")
    roots = SyntheticRootFixture(path / "bootstrap", key_paths=scene.role_paths())
    roots.bootstrap()
    yield scene, roots
    assert protected_store_hashes() == before


@pytest.mark.parametrize("field", ["trusted_public_key_path", "authoritative_view_trusted_public_key_path",
    "observation_trusted_public_key_path", "recovery_trusted_public_key_path"])
def test_rb28_request_trusted_key_substitution(strict_scene, field):
    scene, roots = strict_scene
    override = replace(scene.read_context(), **{field: roots.public["root-bundle"]})
    expect("FIXED_AUTHORITY_OVERRIDE", lambda: roots.fixed.bind_read_context(override, current_time=NOW))
    expect("FIXED_AUTHORITY_OVERRIDE", lambda: roots.fixed.bind_read_context(scene.read_context(),
        current_time=NOW, authority_paths={"ledger": "caller-chosen.jsonl"}))


def test_rb29_existing_time_verification(root):
    root.bootstrap()
    time = root.fixed.trusted_time_verifier(lambda: parse_utc(NOW), current_time=NOW)
    evidence = sign_time_attestation(root.private["trusted-time"], dataset_identity=DATASET,
        authority_identity="synthetic-time-authority", attested_at=NOW, expires_at="2030-01-15T00:01:00+00:00",
        generation=1, evidence_id="root-compatible-time", session_challenge=time.session_challenge)
    assert time.verify(evidence, dataset_identity=DATASET).generation == 1


def test_rb30_existing_owner_approval(strict_scene):
    scene, roots = strict_scene
    roots.fixed.verify_owner_approval(scene.env.approval, current_time=NOW, proposal=scene.env.proposal,
        source_observation_fingerprint=scene.env.observation.semantic_fingerprint(), dataset_identity=DATASET,
        current_baseline_fingerprint=scene.context.current_baseline_fingerprint,
        expected_actor="shadow-owner", expected_operator="shadow-operator")


def test_rb31_existing_revocation(strict_scene):
    scene, roots = strict_scene
    assert scene.verified_revocations().state_sequence == 0
    for role, evidence in (("revocation", scene.revocations.state), ("revocation-checkpoint", scene.revocations.checkpoint)):
        assert roots.fixed.verify_artifact(role, evidence, current_time=NOW)


def test_rb32_existing_full_strict_shadow_and_override(strict_scene):
    scene, roots = strict_scene
    assert scene.selected().is_resolved
    context = scene.read_context()
    result = roots.fixed.read_shadow(scene.reader, observation=scene.env.observation, snapshot=scene.snapshot,
        read_context=context, observation_attestation=scene.observation_attestation)
    assert result.is_resolved and result.is_shadow_only and not result.is_production_authorization
    override = replace(context, observation_trusted_public_key_path=roots.public["root-bundle"])
    expect("FIXED_AUTHORITY_OVERRIDE", lambda: roots.fixed.read_shadow(scene.reader, observation=scene.env.observation,
        snapshot=scene.snapshot, read_context=override, observation_attestation=scene.observation_attestation))


# ---------------------------------------------------------------------------
# RB-33 — versioned-source scope is a different question from the untracked
# runtime inventory.
#
# The previous predicate merged `git ls-files --cached` with
# `git ls-files --others --exclude-standard` and compared the union against the
# historical versioned baseline. That made every legitimate untracked runtime
# artifact (e.g. `data/.snapshots.lock`) look like an unauthorized versioned
# change. Versioned scope and runtime hygiene are separated below; the union is
# never compared to the baseline again.
# ---------------------------------------------------------------------------

#: Runtime sidecars the current implementation creates inside the data
#: directory, taken from the code rather than assumed:
#: `utils.concurrency.acquire_refresh_lock` -> `<DATA_DIR>/.refresh.lock`,
#: `utils.refresh_state` -> `<DATA_DIR>/.refresh_state.json`,
#: `utils.prediction_snapshots` -> `<DATA_DIR>/.snapshots.lock`.
#: Exact paths only. They are runtime artifacts: never registered in
#: ALLOWED_PATHS, never committed, never deleted.
EXPECTED_RUNTIME_SIDECARS = frozenset((
    "data/.refresh.lock",
    "data/.refresh_state.json",
    "data/.snapshots.lock",
))

#: `reports/` is a checkout-local artifact root (AGENTS.md §5.1): excluded via
#: local Git metadata, never committed. Operator notes, audit dumps and repro
#: scripts live there by policy.
LOCAL_ARTIFACT_ROOTS = ("reports/",)

#: Ignored-path inspection is bounded to roots that hold repository / operator
#: artifacts. Dependency and build trees (`.venv/`, `frontend/node_modules/`,
#: `frontend/dist/`) are third-party output, not repository artifacts: they ship
#: public CA bundles such as `.venv/Lib/site-packages/certifi/cacert.pem`, so an
#: unbounded ignored scan would reject a healthy checkout.
IGNORED_ARTIFACT_ROOTS = (
    "reports/",
    "data/",
    "artifacts/",
    ".runtime/",
    "instance/",
    "docs/analysis/",
)

#: Private key material is rejected wherever it appears, including inside a
#: tolerated local artifact root.
SENSITIVE_SUFFIXES = frozenset((
    ".pem", ".key", ".pfx", ".p12", ".der", ".jks", ".keystore", ".kdbx", ".gpg", ".asc",
))

#: Executable / importable source is rejected unless it sits under a local
#: artifact root: a stray module inside the code tree would be importable.
EXECUTABLE_SUFFIXES = frozenset((
    ".py", ".pyw", ".pyi", ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".com",
    ".ps1", ".psm1", ".sh", ".bash", ".bat", ".cmd", ".vbs",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
))

RUNTIME_SIDECAR = "RUNTIME_SIDECAR"
LOCAL_ARTIFACT = "LOCAL_ARTIFACT"
LOCAL_UNRELATED_ARTIFACT = "LOCAL_UNRELATED_ARTIFACT"
IGNORED_LOCAL_ARTIFACT = "IGNORED_LOCAL_ARTIFACT"
REJECTED = "REJECTED"


def _under(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix)


def _sidecar_substitution_reason(root: Path, path: str):
    """
    Rejection reason when a known sidecar path is substituted, else ``None``.

    A symlink/junction pointing at the same *name* elsewhere would let an
    attacker place a lock path that resolves outside `data/`.
    """
    target = root / path
    try:
        real_parent = Path(os.path.realpath(target.parent))
        real_target = Path(os.path.realpath(target))
    except OSError:
        return "RUNTIME_SIDECAR_UNRESOLVABLE"
    if target.is_symlink() or real_target.parent != real_parent:
        return "RUNTIME_SIDECAR_PATH_SUBSTITUTED"
    if real_parent != Path(os.path.realpath(root / "data")):
        return "RUNTIME_SIDECAR_OUTSIDE_DATA_DIR"
    return None


def _classify_untracked_path(root: Path, path: str) -> dict:
    """
    Classify one untracked path. Exact-path decisions only — no wildcards.

    Order matters: key material is rejected before any tolerance applies, so a
    sidecar allowance or a local artifact root can never conceal it.
    """
    entry = {"path": path, "classification": None, "reason": None}

    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        entry.update(classification=REJECTED, reason="UNTRACKED_PATH_NOT_NORMALIZED")
        return entry

    if Path(path).suffix.lower() in SENSITIVE_SUFFIXES:
        entry.update(classification=REJECTED, reason="SENSITIVE_KEY_ARTIFACT")
        return entry

    if path in EXPECTED_RUNTIME_SIDECARS:
        reason = _sidecar_substitution_reason(root, path)
        if reason:
            entry.update(classification=REJECTED, reason=reason)
            return entry
        entry.update(classification=RUNTIME_SIDECAR, reason="KNOWN_RUNTIME_ARTIFACT")
        return entry

    if any(_under(path, prefix) for prefix in LOCAL_ARTIFACT_ROOTS):
        entry.update(classification=LOCAL_ARTIFACT, reason="LOCAL_ONLY_ARTIFACT_ROOT")
        return entry

    # `data/` holds the protected stores: only the known sidecars above may
    # appear there untracked, so no blanket tolerance is granted.
    if _under(path, "data/"):
        entry.update(classification=REJECTED, reason="UNEXPECTED_DATA_DIRECTORY_ARTIFACT")
        return entry

    if Path(path).suffix.lower() in EXECUTABLE_SUFFIXES:
        entry.update(classification=REJECTED, reason="UNEXPECTED_EXECUTABLE_SOURCE")
        return entry

    entry.update(classification=LOCAL_UNRELATED_ARTIFACT, reason="UNRELATED_UNTRACKED_ARTIFACT")
    return entry


def _expand_ignored_entries(root: Path, entries) -> list:
    """
    Turn ignored Git entries into concrete repository-relative file paths.

    Git reports an ignored directory in either shape: collapsed as `reports/`,
    or as its individual children. Both must yield the same file list, otherwise
    key material inside an ignored directory stays invisible to the inventory.
    Contents are never read; only names are enumerated.
    """
    expanded, seen = [], set()
    for raw in entries:
        path = str(raw).rstrip("/")
        if not path:
            continue
        if not any(_under(path, prefix) for prefix in IGNORED_ARTIFACT_ROOTS):
            continue
        base = root / path
        collapsed = str(raw).endswith("/") or base.is_dir()
        if not collapsed:
            if path not in seen:
                seen.add(path)
                expanded.append(path)
            continue
        for current, directories, files in os.walk(base):
            # Never follow a substituted directory: report it, do not walk in.
            directories[:] = sorted(
                name for name in directories
                if not os.path.islink(os.path.join(current, name))
            )
            for name in sorted(files):
                candidate = os.path.relpath(os.path.join(current, name), str(root))
                candidate = candidate.replace(os.sep, "/")
                if candidate not in seen:
                    seen.add(candidate)
                    expanded.append(candidate)
    return sorted(expanded)


def _classify_ignored_path(root: Path, path: str) -> dict:
    """
    Classify one ignored repository artifact.

    An ignored path is never versioned code, so it can never authorize a scope
    change. Only key material is refused here; ordinary reports and local
    diagnostics remain permitted local artifacts.
    """
    entry = {"path": path, "classification": None, "reason": None}

    if path.startswith("/") or "\\" in path or ".." in path.split("/"):
        entry.update(classification=REJECTED, reason="UNTRACKED_PATH_NOT_NORMALIZED")
        return entry

    if Path(path).suffix.lower() in SENSITIVE_SUFFIXES:
        entry.update(classification=REJECTED, reason="SENSITIVE_KEY_ARTIFACT_IGNORED")
        return entry

    if path in EXPECTED_RUNTIME_SIDECARS:
        reason = _sidecar_substitution_reason(root, path)
        if reason:
            entry.update(classification=REJECTED, reason=reason)
            return entry
        entry.update(classification=RUNTIME_SIDECAR, reason="KNOWN_RUNTIME_ARTIFACT_IGNORED")
        return entry

    entry.update(classification=IGNORED_LOCAL_ARTIFACT, reason="LOCAL_ONLY_IGNORED_ARTIFACT")
    return entry


def _versioned_scope_report(root: Path, tracked, untracked, baseline: dict, ignored=()) -> dict:
    """
    Two independent verdicts:

    - ``versioned_scope_ok``: the tracked set outside ALLOWED_PATHS still equals
      the historical baseline set and those files are byte-identical.
    - ``untracked_ok``: no untracked **or ignored** path is key material, no
      visible untracked path is unexpected source, and nothing unexpected
      appears inside the protected data directory.

    Untracked and ignored paths are reported individually, so nothing is
    silently invisible even when it is tolerated.
    """
    tracked_set = set(tracked)
    unscoped = set(baseline) - ALLOWED_PATHS

    unexpected_additions = sorted(tracked_set - ALLOWED_PATHS - unscoped)
    unexpected_removals = sorted(unscoped - tracked_set)
    modified = []
    for name in sorted(unscoped & tracked_set):
        current = (root / name).read_bytes().replace(b"\r\n", b"\n")
        if current != baseline[name].replace(b"\r\n", b"\n"):
            modified.append(name)

    inventory = [_classify_untracked_path(root, path) for path in untracked]
    rejected = [entry for entry in inventory if entry["classification"] == REJECTED]

    ignored_files = _expand_ignored_entries(root, ignored)
    ignored_inventory = [_classify_ignored_path(root, path) for path in ignored_files]
    rejected_ignored = [
        entry for entry in ignored_inventory if entry["classification"] == REJECTED
    ]

    sensitive = sorted(
        path
        for path in list(tracked) + list(untracked) + ignored_files
        if Path(path).suffix.lower() in SENSITIVE_SUFFIXES
    )

    return {
        "scope_version": SCOPE_VERSION,
        "unexpected_tracked_additions": unexpected_additions,
        "unexpected_tracked_removals": unexpected_removals,
        "modified_outside_scope": modified,
        "untracked_inventory": inventory,
        "ignored_inventory": ignored_inventory,
        "runtime_sidecars": sorted(
            entry["path"]
            for entry in inventory + ignored_inventory
            if entry["classification"] == RUNTIME_SIDECAR
        ),
        "rejected_untracked": rejected,
        "rejected_ignored": rejected_ignored,
        "sensitive_paths": sensitive,
        "versioned_scope_ok": not (unexpected_additions or unexpected_removals or modified),
        "untracked_ok": not rejected and not rejected_ignored and not sensitive,
    }


def _git_paths(root, *args: str):
    output = subprocess.check_output(["git", *args], cwd=str(root)).decode("utf-8")
    return [path for path in output.split("\0") if path]


def _baseline_archive() -> dict:
    raw = subprocess.check_output(["git", "archive", "--format=tar", BASELINE], cwd=ROOT)
    with tarfile.open(fileobj=BytesIO(raw), mode="r:") as archive:
        return {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}


def test_rb33_active_versioned_change_scope_and_production_import_closure():
    baseline = _baseline_archive()
    tracked = _git_paths(ROOT, "ls-files", "--cached", "-z")
    untracked = _git_paths(ROOT, "ls-files", "--others", "--exclude-standard", "-z")
    ignored = _git_paths(ROOT, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    report = _versioned_scope_report(ROOT, tracked, untracked, baseline, ignored)

    assert report["versioned_scope_ok"], report
    assert report["rejected_untracked"] == [], report
    assert report["rejected_ignored"] == [], report
    assert report["sensitive_paths"] == [], report
    assert production_boundary_audit()["production_activation"] == "NOT_ENABLED"


# ---------------------------------------------------------------------------
# RB-33 adversarial controls — temporary repositories / directories only.
#
# The active project's Git history is never mutated: each control builds its
# own repository under `tmp_path` and drives the same predicates.
# ---------------------------------------------------------------------------

_TEMPORARY_REPOSITORY_FILES = {
    "utils/trust_root_bootstrap.py": b"# synthetic trust root module\n",
    "utils/concurrency.py": b"# synthetic lock module\n",
    "data/.gitkeep": b"",
    "docs/notes.md": b"# synthetic notes\n",
}


def _git_command(root: Path, *args: str) -> str:
    return subprocess.check_output(
        [
            "git",
            "-c", "user.email=ci@example.invalid",
            "-c", "user.name=CI",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=str(root),
        stderr=subprocess.STDOUT,
    ).decode("utf-8")


def _temporary_repository(tmp_path: Path, files=None, exclude_lines=()) -> Path:
    files = dict(files or _TEMPORARY_REPOSITORY_FILES)
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    for name, payload in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _git_command(root, "init", "--quiet")
    if exclude_lines:
        (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
        (root / ".git" / "info" / "exclude").write_bytes(
            "".join(line + "\n" for line in exclude_lines).encode("utf-8")
        )
    _git_command(root, "add", "--all")
    _git_command(root, "commit", "--quiet", "-m", "synthetic baseline")
    return root


def _temporary_report(root: Path, files=None) -> dict:
    baseline = dict(files or _TEMPORARY_REPOSITORY_FILES)
    tracked = _git_paths(root, "ls-files", "--cached", "-z")
    untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z")
    ignored = _git_paths(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    return _versioned_scope_report(root, tracked, untracked, baseline, ignored)


def test_rb33b_untracked_snapshot_lock_does_not_fail_versioned_scope(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "data" / ".snapshots.lock").write_bytes(b"0")

    report = _temporary_report(root)
    assert report["runtime_sidecars"] == ["data/.snapshots.lock"], report
    assert report["versioned_scope_ok"] is True, report
    assert report["untracked_ok"] is True, report
    assert report["rejected_untracked"] == [], report
    # The lock is a runtime artifact, never an authorized versioned path.
    assert "data/.snapshots.lock" not in ALLOWED_PATHS


def test_rb33c_tracked_source_change_outside_scope_still_fails(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "docs" / "notes.md").write_bytes(b"# tampered outside scope\n")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is False
    assert report["modified_outside_scope"] == ["docs/notes.md"], report


def test_rb33d_new_unexpected_tracked_path_still_fails(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "utils" / "runtime_override.py").write_bytes(b"# unauthorized module\n")
    _git_command(root, "add", "--all")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is False
    assert report["unexpected_tracked_additions"] == ["utils/runtime_override.py"], report


def test_rb33d2_removed_baseline_path_still_fails(tmp_path):
    root = _temporary_repository(tmp_path)
    _git_command(root, "rm", "--quiet", "docs/notes.md")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is False
    assert report["unexpected_tracked_removals"] == ["docs/notes.md"], report


def test_rb33e_untracked_executable_source_in_code_directory_is_rejected(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "utils" / "runtime_override.py").write_bytes(b"# unauthorized module\n")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is True
    assert report["untracked_ok"] is False, report
    assert report["rejected_untracked"] == [
        {
            "path": "utils/runtime_override.py",
            "classification": REJECTED,
            "reason": "UNEXPECTED_EXECUTABLE_SOURCE",
        }
    ], report


def test_rb33f_private_key_artifact_is_still_rejected(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "data" / "owner.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    (root / "reports" / "keys").mkdir(parents=True)
    (root / "reports" / "keys" / "owner.p12").write_bytes(b"\x00")

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert report["sensitive_paths"] == ["data/owner.pem", "reports/keys/owner.p12"], report
    reasons = {entry["path"]: entry["reason"] for entry in report["rejected_untracked"]}
    assert reasons == {
        "data/owner.pem": "SENSITIVE_KEY_ARTIFACT",
        "reports/keys/owner.p12": "SENSITIVE_KEY_ARTIFACT",
    }, report


def test_rb33f2_unexpected_artifact_in_the_data_directory_is_rejected(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "data" / "production_activation.json").write_bytes(b"{}")

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert report["rejected_untracked"] == [
        {
            "path": "data/production_activation.json",
            "classification": REJECTED,
            "reason": "UNEXPECTED_DATA_DIRECTORY_ARTIFACT",
        }
    ], report


def test_rb33g_runtime_sidecar_symlink_substitution_fails_closed(tmp_path):
    root = _temporary_repository(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "copy-of-lock"
    victim.write_bytes(b"0")
    try:
        os.symlink(str(victim), str(root / "data" / ".snapshots.lock"))
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privileges vary by host
        pytest.skip(f"cannot create a file symlink here: {exc}")

    report = _temporary_report(root)
    assert report["runtime_sidecars"] == [], report
    assert report["untracked_ok"] is False, report
    assert report["rejected_untracked"] == [
        {
            "path": "data/.snapshots.lock",
            "classification": REJECTED,
            "reason": "RUNTIME_SIDECAR_PATH_SUBSTITUTED",
        }
    ], report


def test_rb33h_local_report_artifacts_stay_out_of_the_versioned_verdict(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "reports").mkdir()
    (root / "reports" / "PHASE-REPORT.md").write_bytes(b"# local report\n")
    (root / "reports" / "_probe.py").write_bytes(b"print('repro')\n")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is True, report
    assert report["untracked_ok"] is True, report
    classifications = {
        entry["path"]: entry["classification"] for entry in report["untracked_inventory"]
    }
    assert classifications == {
        "reports/PHASE-REPORT.md": LOCAL_ARTIFACT,
        "reports/_probe.py": LOCAL_ARTIFACT,
    }, report


def test_rb33i_unrelated_untracked_note_is_inventory_only(tmp_path):
    root = _temporary_repository(tmp_path)
    (root / "operator-note.md").write_bytes(b"# local work memory\n")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is True, report
    assert report["untracked_ok"] is True, report
    # Visible in the inventory, never reclassified as an authorized code change.
    assert report["untracked_inventory"] == [
        {
            "path": "operator-note.md",
            "classification": LOCAL_UNRELATED_ARTIFACT,
            "reason": "UNRELATED_UNTRACKED_ARTIFACT",
        }
    ], report
    assert "operator-note.md" not in ALLOWED_PATHS


def test_rb33j_production_activation_remains_disabled(tmp_path):
    root = _temporary_repository(tmp_path)
    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is True and report["untracked_ok"] is True

    audit = production_boundary_audit()
    assert audit["production_activation"] == "NOT_ENABLED"
    assert audit["production_runtime_reader"] == "NOT_ENABLED"
    assert audit["production_mapping_write_authorization"] == "NOT_GRANTED"


def test_rb33k_trust_root_baseline_checks_still_operate(tmp_path):
    root = _temporary_repository(tmp_path)
    # An unauthorized module next to the trust root still fails, and the
    # baseline byte comparison still detects a change to an unscoped file.
    (root / "utils" / "trust_root_shadow.py").write_bytes(b"# unregistered trust root\n")
    (root / "docs" / "notes.md").write_bytes(b"# rewritten\n")
    _git_command(root, "add", "--all")

    report = _temporary_report(root)
    assert report["unexpected_tracked_additions"] == ["utils/trust_root_shadow.py"], report
    assert report["modified_outside_scope"] == ["docs/notes.md"], report
    assert report["versioned_scope_ok"] is False


# ---------------------------------------------------------------------------
# RB-33 ignored-artifact visibility — the `reports/` blind spot.
#
# `git ls-files --others --exclude-standard` never returns children of an
# ignored directory, so key material placed under `reports/` used to be
# invisible to the inventory that claims to reject it everywhere. The controls
# below drive real Git ignore behaviour in temporary repositories.
# ---------------------------------------------------------------------------

_IGNORED_REPORTS_EXCLUDE = ("/reports/", "data/*.json")


def test_rb33l_ignored_reports_stay_permitted_local_artifacts(tmp_path):
    root = _temporary_repository(tmp_path, exclude_lines=_IGNORED_REPORTS_EXCLUDE)
    (root / "reports").mkdir()
    (root / "reports" / "ordinary-report.md").write_bytes(b"# local report\n")
    (root / "reports" / "diagnostic.py").write_bytes(b"print('local repro')\n")

    # The ordinary inventory genuinely does not see the ignored children.
    assert _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z") == []

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is True, report
    assert report["untracked_ok"] is True, report
    assert report["untracked_inventory"] == [], report
    assert {
        entry["path"]: entry["classification"] for entry in report["ignored_inventory"]
    } == {
        "reports/diagnostic.py": IGNORED_LOCAL_ARTIFACT,
        "reports/ordinary-report.md": IGNORED_LOCAL_ARTIFACT,
    }, report
    # Ignored artifacts never become authorized versioned paths.
    assert "reports/diagnostic.py" not in ALLOWED_PATHS


def test_rb33m_ignored_sensitive_key_inside_reports_is_rejected(tmp_path):
    root = _temporary_repository(tmp_path, exclude_lines=_IGNORED_REPORTS_EXCLUDE)
    (root / "reports").mkdir()
    (root / "reports" / "owner.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")

    assert _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z") == []

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert report["sensitive_paths"] == ["reports/owner.pem"], report
    assert report["rejected_ignored"] == [
        {
            "path": "reports/owner.pem",
            "classification": REJECTED,
            "reason": "SENSITIVE_KEY_ARTIFACT_IGNORED",
        }
    ], report
    # Rejection is a security verdict, never a versioned-scope change.
    assert report["versioned_scope_ok"] is True


def test_rb33n_ignored_nested_sensitive_key_is_rejected(tmp_path):
    root = _temporary_repository(tmp_path, exclude_lines=_IGNORED_REPORTS_EXCLUDE)
    (root / "reports" / "nested" / "deeper").mkdir(parents=True)
    (root / "reports" / "nested" / "deeper" / "service.key").write_bytes(b"secret\n")
    (root / "reports" / "nested" / "ordinary.md").write_bytes(b"# note\n")

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert [entry["path"] for entry in report["rejected_ignored"]] == [
        "reports/nested/deeper/service.key"
    ], report
    assert report["rejected_ignored"][0]["reason"] == "SENSITIVE_KEY_ARTIFACT_IGNORED"
    assert {
        entry["path"] for entry in report["ignored_inventory"]
    } == {"reports/nested/deeper/service.key", "reports/nested/ordinary.md"}


def test_rb33o_collapsed_ignored_directory_entry_is_expanded(tmp_path):
    """Git may report the ignored directory itself; children must still be seen."""
    root = _temporary_repository(tmp_path, exclude_lines=_IGNORED_REPORTS_EXCLUDE)
    (root / "reports" / "nested").mkdir(parents=True)
    (root / "reports" / "owner.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    (root / "reports" / "nested" / "leak.key").write_bytes(b"secret\n")

    collapsed = _expand_ignored_entries(root, ["reports/"])
    assert collapsed == ["reports/nested/leak.key", "reports/owner.pem"], collapsed

    report = _versioned_scope_report(
        root,
        _git_paths(root, "ls-files", "--cached", "-z"),
        _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z"),
        dict(_TEMPORARY_REPOSITORY_FILES),
        ["reports/"],
    )
    assert report["untracked_ok"] is False, report
    assert sorted(entry["path"] for entry in report["rejected_ignored"]) == [
        "reports/nested/leak.key",
        "reports/owner.pem",
    ], report


def test_rb33p_ignored_paths_outside_artifact_roots_are_not_scanned(tmp_path):
    """Dependency trees ship public CA bundles; scanning them would be wrong."""
    root = _temporary_repository(tmp_path, exclude_lines=("/venv/", "/reports/"))
    (root / "venv" / "lib").mkdir(parents=True)
    (root / "venv" / "lib" / "cacert.pem").write_bytes(b"public ca bundle\n")

    report = _temporary_report(root)
    assert _git_paths(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z") == [
        "venv/lib/cacert.pem"
    ]
    assert report["ignored_inventory"] == [], report
    assert report["untracked_ok"] is True, report
    assert report["versioned_scope_ok"] is True, report


def test_rb33q_ignored_runtime_sidecar_stays_permitted(tmp_path):
    root = _temporary_repository(
        tmp_path, exclude_lines=("data/*.json", "data/.snapshots.lock")
    )
    (root / "data" / ".snapshots.lock").write_bytes(b"0")

    assert _git_paths(root, "ls-files", "--others", "--exclude-standard", "-z") == []

    report = _temporary_report(root)
    assert report["runtime_sidecars"] == ["data/.snapshots.lock"], report
    assert report["untracked_ok"] is True, report
    assert report["rejected_ignored"] == [], report


def test_rb33r_ignored_sidecar_symlink_still_fails_closed(tmp_path):
    root = _temporary_repository(
        tmp_path, exclude_lines=("data/*.json", "data/.snapshots.lock")
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    victim = elsewhere / "copy-of-lock"
    victim.write_bytes(b"0")
    try:
        os.symlink(str(victim), str(root / "data" / ".snapshots.lock"))
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - privileges vary
        pytest.skip(f"cannot create a file symlink here: {exc}")

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert [entry["reason"] for entry in report["rejected_ignored"]] == [
        "RUNTIME_SIDECAR_PATH_SUBSTITUTED"
    ], report


def test_rb33s_visible_sensitive_key_is_still_rejected_without_ignore(tmp_path):
    """The original visible-path rule must not be replaced by the ignored scan."""
    root = _temporary_repository(tmp_path)
    (root / "reports").mkdir()
    (root / "reports" / "owner.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")

    report = _temporary_report(root)
    assert report["untracked_ok"] is False, report
    assert report["rejected_untracked"] == [
        {
            "path": "reports/owner.pem",
            "classification": REJECTED,
            "reason": "SENSITIVE_KEY_ARTIFACT",
        }
    ], report


def test_rb33t_ignored_artifacts_do_not_mask_tracked_changes(tmp_path):
    """An ignored artifact must never hide an unauthorized tracked change."""
    root = _temporary_repository(tmp_path, exclude_lines=_IGNORED_REPORTS_EXCLUDE)
    (root / "reports").mkdir()
    (root / "reports" / "ordinary-report.md").write_bytes(b"# local report\n")
    (root / "docs" / "notes.md").write_bytes(b"# tampered\n")

    report = _temporary_report(root)
    assert report["versioned_scope_ok"] is False, report
    assert report["modified_outside_scope"] == ["docs/notes.md"], report


def test_rb34_seven_store_integrity(protected_bytes):
    assert len(protected_bytes) == 7
    assert protected_store_hashes() == protected_bytes


def test_rb35_ci_configuration_requires_both_platforms_and_history():
    # Actual remote outcomes are checked after push, never inferred from this test.
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ci.count("fetch-depth: 0") == 2
    assert ci.count("os: [ubuntu-latest, windows-latest]") == 2
    assert "junction regression was skipped on Windows" in ci


@pytest.mark.parametrize("field,code", [("dataset_identity", "ROOT_DATASET_MISMATCH"),
    ("environment_identity", "ROOT_ENVIRONMENT_MISMATCH")])
def test_role_evidence_and_key_selection_scope(root, field, code):
    root.bootstrap()
    artifact = root.evidence(**{field: "wrong-scope"})
    expect(code, lambda: root.fixed.verify_artifact("governance-view", artifact, current_time=NOW))
    expect(code, lambda: root.fixed.key_path("governance-view", ROLE_PURPOSES["governance-view"][0],
        current_time=NOW, **{field: "wrong-scope"}))


@pytest.mark.parametrize("attack", ["self-approval", "wrong-scope", "wrong-target", "wrong-changes"])
def test_independent_approval_binding(root, attack):
    root.bootstrap(); new = root.next_bundle()
    p = new["approval"].payload()
    if attack == "self-approval": p["requester_identity"] = p["approver_identity"]; code = "ROOT_APPROVAL_SEPARATION_INVALID"
    elif attack == "wrong-scope": p["environment_identity"] = "other"; code = "ROOT_APPROVAL_INVALID"
    elif attack == "wrong-target": p["new_bundle_fingerprint"] = "0" * 64; code = "ROOT_APPROVAL_INVALID"
    else: p["approved_role_transitions"] = []; code = "ROOT_APPROVAL_INVALID"
    approval = sign_trusted_artifact(root.approval_private, artifact_type=APPROVAL, payload=p)
    expect(code, lambda: root.rotate(new, approval=approval))


def test_key_file_replacement_between_pin_and_crypto_cannot_substitute_signer(root, monkeypatch):
    root.bootstrap()
    attacker, attacker_public = root.keypair("race-attacker")
    artifact = sign_trusted_artifact(attacker, artifact_type=ROLE_PURPOSES["governance-view"][0], payload=root.evidence().payload())
    import utils.trust_root_bootstrap as module
    original = module.verify_trusted_artifact
    def replace_path(artifact, **options):
        return original(artifact, **{**options, "trusted_public_key_path": attacker_public})
    monkeypatch.setattr(module, "verify_trusted_artifact", replace_path)
    expect("ROOT_UNTRUSTED", lambda: root.fixed.verify_artifact("governance-view", artifact, current_time=NOW))


def historical_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/test_production_trust_contracts.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_history_archive_preserves_original_scope_and_accepts_current_authorized_changes():
    module = historical_module("historical_contract_pilot_test")
    contracts = module.load_contracts()
    assert module.HISTORICAL_REVISION == BASELINE
    assert len(contracts["acceptance"]["immutable_repository"]["allowed_change_paths"]) == 7
    module.test_tc16_no_runtime_integration_new_endpoints_keys_or_weakened_existing_tests(contracts)
    assert "listed = subprocess.check_output" in module.historical_text("tests/test_production_trust_contracts.py")
    assert "utils/trust_root_bootstrap.py" not in module.historical_files()


@pytest.mark.parametrize("store", sorted(("daily_matches.json", "team_strength.json", "calibrated.json",
    "prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json")))
def test_replacement_active_store_guard_detects_each_mutated_synthetic_copy(tmp_path, store):
    module = historical_module("synthetic_store_guard")
    module.ROOT = tmp_path
    (tmp_path / "data").mkdir()
    for name in module.STORES: (tmp_path / "data" / name).write_text("{}", encoding="utf-8")
    guard = module.seven_store_session_integrity.__wrapped__()
    next(guard)
    (tmp_path / "data" / store).write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(AssertionError, match="protected production store"):
        next(guard)


def test_functional_demo_uses_real_crypto_and_strict_reader(tmp_path):
    result = run_acceptance(tmp_path / "functional")
    assert result["success"] and result["seven_stores_unchanged"] and result["synthetic_only"]
    assert result["decision"] == "NO-GO" and result["production_activation_ready"] is False
    outcomes = {s["scenario"]: s for s in result["steps"]}
    assert outcomes["existing_fully_strict_shadow"]["status"] == "RESOLVED_SHADOW"
    assert outcomes["read_context_override"]["rejection_codes"] == ["FIXED_AUTHORITY_OVERRIDE"]

@pytest.mark.parametrize("attack,code", [("generation-jump", "ROOT_TRANSITION_INVALID"),
    ("wrong-predecessor", "ROOT_TRANSITION_INVALID"), ("removed-retirement", "ROOT_RETIREMENT_INVALID"),
    ("same-identity", "ROOT_TRANSITION_INVALID")])
def test_transition_changes_remain_bound_after_real_resigning(root, attack, code):
    root.bootstrap(); new = root.next_bundle(); body = deepcopy(new['body'])
    if attack == 'generation-jump': body['root_generation'] = 3
    elif attack == 'wrong-predecessor': body['predecessor_bundle_fingerprint'] = '0' * 64
    elif attack == 'removed-retirement': body['revoked_key_identities'] = []
    else: body['bundle_identity'] = root.initial_body['bundle_identity']
    body['bundle_fingerprint'] = fingerprint({k:v for k,v in body.items() if k != 'bundle_fingerprint'})
    from utils.trust_root_bootstrap import transition_body
    approval = root.approve(root.initial_body, body)
    transition = sign_trusted_artifact(root.private['root-bundle'], artifact_type=TRANSITION,
        payload=transition_body(root.initial_body, body, approval))
    value = {**new, 'body':body, 'bundle':root.sign_bundle(body,new['private']['root-bundle']),
        'approval':approval,'transition':transition,'continuity':root.anchor(body)}
    expect(code, lambda: root.rotate(value))


@pytest.mark.parametrize('attack', ['wrong-fingerprint','wrong-environment','wrong-minimum'])
def test_independent_continuity_expectation_cannot_be_replaced(root, attack):
    evidence = root.initial_continuity
    if attack == 'wrong-fingerprint': evidence = replace(evidence, expected_bundle_fingerprint='0'*64)
    elif attack == 'wrong-environment': evidence = replace(evidence, environment_identity='caller')
    else: evidence = replace(evidence, minimum_accepted_version=0)
    expect('CONTINUITY_UNVERIFIED', lambda: root.verifier.bootstrap(root.initial,
        approval=root.initial_approval, continuity=evidence, current_time=NOW))


def test_signing_period_and_minimum_version(root):
    root.bootstrap()
    expect('ROOT_SIGNING_PERIOD_INVALID', lambda: root.fixed.verify_artifact('governance-view',root.evidence(),
        current_time='2030-01-16T00:00:00+00:00',max_age_seconds=86400))
    expect('ROOT_VERSION_ROLLBACK', lambda: bootstrap(root,policy=replace(root.policy,minimum_accepted_version=2)))
    artifact, approval = altered(root,lambda b: next(r for r in b['authorities'] if r['signer_role']=='root-bundle').update(signing_not_after=NOW))
    expect('ROOT_SIGNING_PERIOD_INVALID',lambda: bootstrap(root,artifact,approval))


def test_unknown_role_and_fixed_path_alias_reject(root,strict_scene,tmp_path):
    root.bootstrap()
    expect('ROOT_ROLE_UNAUTHORIZED',lambda: root.fixed.key_path('unregistered','anything',current_time=NOW))
    scene, roots = strict_scene
    alias = tmp_path/'same-key-different-path.pem'
    alias.write_bytes(Path(scene.observation_public).read_bytes())
    expect('FIXED_AUTHORITY_OVERRIDE',lambda: roots.fixed.bind_read_context(replace(scene.read_context(),
        observation_trusted_public_key_path=str(alias)),current_time=NOW))


def test_untrusted_initial_bundle_and_role_crypto_are_not_self_verified(root,monkeypatch):
    import utils.trust_root_bootstrap as module
    attacker, attacker_public = root.keypair('bootstrap-race')
    forged = root.sign_bundle(root.initial_body,attacker)
    original = module.verify_trusted_artifact
    def replaced(artifact,**options):
        if options['expected_artifact_type']==BUNDLE:
            options={**options,'trusted_public_key_path':attacker_public}
        return original(artifact,**options)
    monkeypatch.setattr(module,'verify_trusted_artifact',replaced)
    expect('ROOT_UNTRUSTED',lambda: bootstrap(root,forged))


def test_fully_strict_consumer_checks_modes_and_post_read_root_fence(tmp_path,monkeypatch):
    scene = RoleSeparatedShadowScenario(tmp_path/'scene')
    roots = SyntheticRootFixture(tmp_path/'bootstrap',key_paths=scene.role_paths());roots.bootstrap()
    assert scene.selected().is_resolved
    values=dict(observation=scene.env.observation,snapshot=scene.snapshot,read_context=scene.read_context(),
        observation_attestation=scene.observation_attestation)
    with monkeypatch.context() as patch:
        patch.setattr(scene.reader,'_require_observation_attestation',False)
        expect('FIXED_AUTHORITY_STRICT_MODE_REQUIRED',lambda: roots.fixed.read_shadow(scene.reader,**values))
    successor=roots.next_bundle()
    original=scene.reader.read
    def changed_during_read(**kwargs):
        result=original(**kwargs)
        assert result.is_resolved
        roots.rotate(successor)
        return result
    monkeypatch.setattr(scene.reader,'read',changed_during_read)
    expect('ROOT_VERSION_ROLLBACK',lambda: roots.fixed.read_shadow(scene.reader,**values))

def test_authenticated_envelopes_retained_without_mutating_old_history(root):
    root.bootstrap()
    before=root.handle.evidence()
    assert before['bundle']==root.initial.as_dict() and before['approval']==root.initial_approval.as_dict()
    assert before['transition'] is None and not before['production_authorized']
    new=root.next_bundle();current=root.rotate(new)
    assert current.evidence()['transition']==new['transition'].as_dict()
    assert root.handle.evidence()==before
    current.evidence()['bundle']['payload']['root_generation']=999
    assert current.body()['root_generation']==2

def test_forged_verified_handle_or_boolean_cannot_establish_bootstrap(root):
    from utils.trust_root_bootstrap import VerifiedBootstrap
    from utils.source_identity import canonical_json
    class FakeVerifier:
        def require_current(self,handle): return None
    fake=VerifiedBootstrap(canonical_json(root.initial_body),FakeVerifier(),root.initial.to_json(),
        root.initial_approval.to_json(),None)
    expect('BOOTSTRAP_NOT_ESTABLISHED',lambda: FixedAuthorityContext(fake,key_paths=root.public))
    expect('BOOTSTRAP_NOT_ESTABLISHED',lambda: FixedAuthorityContext({'verified':True},key_paths=root.public))
