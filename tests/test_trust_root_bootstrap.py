"""Executable synthetic bootstrap evidence RB-07..35 and adversarial controls."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import importlib.util
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
SCOPE_VERSION = "offline-root-pilot-v1"
ALLOWED_PATHS = frozenset(("utils/trust_root_bootstrap.py", "utils/trusted_artifacts.py",
    "tools/trust_root_bootstrap_validation.py", "tests/test_trust_root_bootstrap.py",
    "tests/test_production_trust_contracts.py", ".github/workflows/ci.yml",
    "docs/security/runtime-activation-offline-trust-bootstrap.md",
    "docs/security/runtime-activation-production-readiness-matrix.md"))


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


def test_rb33_active_versioned_change_scope_and_production_import_closure():
    raw = subprocess.check_output(["git", "archive", "--format=tar", BASELINE], cwd=ROOT)
    with tarfile.open(fileobj=BytesIO(raw), mode="r:") as archive:
        baseline = {m.name: archive.extractfile(m).read() for m in archive.getmembers() if m.isfile()}
    listed = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT).decode().split("\0")
    assert {p for p in listed if p} - ALLOWED_PATHS == set(baseline) - ALLOWED_PATHS, SCOPE_VERSION
    for name in set(baseline) - ALLOWED_PATHS:
        assert (ROOT / name).read_bytes().replace(b"\r\n", b"\n") == baseline[name].replace(b"\r\n", b"\n"), name
    assert production_boundary_audit()["production_activation"] == "NOT_ENABLED"
    assert not any(Path(p).suffix in {".pem", ".key", ".pfx"} for p in listed if p)


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
