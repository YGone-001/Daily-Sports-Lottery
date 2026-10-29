"""Offline review-table integrity only: no production configuration or authority engine.

TC-01..16 validate linked contracts, not keyword presence. Mutation controls prove
that broken authority/root/separation/continuity/dependency/gate links are rejected.
The original frozen manifest is verified against its exact historical Git tree.
Current authorized implementation is assessed by the new pilot boundary tests.
"""
from copy import deepcopy
from functools import lru_cache
from io import BytesIO
import tarfile
import ast
import hashlib
import json
from pathlib import Path
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / "docs/security"
NAMES = {
    "ownership": "runtime-activation-production-authority-ownership.md",
    "bootstrap": "runtime-activation-production-trust-bootstrap.md",
    "deployment": "runtime-activation-production-deployment-contract.md",
    "acceptance": "runtime-activation-production-acceptance-gates.md",
}
UNKNOWN = "NOT_ESTABLISHED"
STATES = ("DESIGNED", "OWNER_ASSIGNED", "APPROVED", "IMPLEMENTED", "VERIFIED_IN_TEST",
          "VERIFIED_IN_DEPLOYMENT", "PRODUCTION_AUTHORIZED")
HISTORICAL_REVISION = "4f4da993a27dc0c5362f38bc228b463ba364999b"
CATEGORIES = set("ABCDEFGHIJKLMNOPQR")
STORES = {"daily_matches.json", "team_strength.json", "calibrated.json",
          "prediction_snapshots.json", "odds_snapshots.json", "settlements.json", "evaluation_rows.json"}
ACTUAL_FIELDS = ("operational_owner", "security_approver", "implementation_owner", "availability_owner",
                 "source_of_truth_system", "issuer_identity", "verifier_identity", "public_key_trust_anchor",
                 "key_rotation_authority", "key_revocation_authority", "checkpoint_publisher",
                 "storage_location", "recovery_ownership")


def need(condition, code):
    if not condition:
        raise ValueError(code)


def unique(records, field):
    values = [record[field] for record in records]
    need(len(values) == len(set(values)), "duplicate:" + field)
    return set(values)


@lru_cache(maxsize=1)
def historical_files():
    # Read the exact committed snapshot, including its original seven-path scope.
    # CI checks out history before offline pytest; tests never fetch or write it.
    archive = subprocess.check_output(["git", "archive", "--format=tar", HISTORICAL_REVISION], cwd=ROOT)
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as tree:
        return {member.name: tree.extractfile(member).read() for member in tree.getmembers() if member.isfile()}


def historical_text(path):
    return historical_files()[path].decode("utf-8-sig")


def load_contracts():
    result = {}
    for key, name in NAMES.items():
        blocks = re.findall(r"```json\n(.*?)\n```", historical_text("docs/security/" + name), re.S)
        need(len(blocks) == 1, "one-review-table:" + key)
        result[key] = json.loads(blocks[0])
    return result


def validate_ownership(c):
    for contract in c.values():
        progress = contract["production_progress"]
        need(set(progress) == set(STATES), "document-state-fields")
        need(progress["DESIGNED"] == "YES" and all(progress[s] == "NO" for s in STATES[1:]), "document-no-state-promotion")
    o = c["ownership"]
    need(tuple(o["states"]) == STATES, "state-vocabulary")
    need(unique(o["authorities"], "authority_id") == CATEGORIES, "eighteen-authorities")
    required = set(ACTUAL_FIELDS) | {"authority_id", "name", "authoritative_dataset_and_scope", "trust_boundary",
        "signing_key_role", "currentness_guarantee", "continuity_guarantee", "checkpoint_role",
        "audit_retention_policy", "status", "progress", "supporting_evidence", "production_evidence", "acceptance_test"}
    for a in o["authorities"]:
        need(required <= set(a), "authority-fields")
        need(all(a[f] == UNKNOWN for f in ACTUAL_FIELDS), "unknown-real-owner-or-source")
        need(a["status"] == "DESIGNED" and a["production_evidence"] == [], "design-not-deployment")
        need(set(a["progress"]) == set(STATES), "progress-fields")
        need(a["progress"]["DESIGNED"] == "YES" and all(a["progress"][s] == "NO" for s in STATES[1:]), "no-state-promotion")
        need(a["audit_retention_policy"] == "POLICY_NOT_APPROVED", "retention-unapproved")
        need(bool(a["trust_boundary"] and a["authoritative_dataset_and_scope"]), "authority-scope")
        for e in a["supporting_evidence"]:
            path = ROOT / e["path"]
            need(e["path"] in historical_files() and path.suffix == ".py", "source-evidence-path")
            tree = ast.parse(historical_text(e["path"]))
            need(any(isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == e["symbol"] for n in ast.walk(tree)), "source-evidence-symbol")


def validate_roles_and_roots(c):
    roles = c["ownership"]["signer_roles"]
    ids = unique(roles, "role_id")
    need(len(ids) >= 18, "signing-role-coverage")
    roots = c["bootstrap"]["roots"]
    root_ids = unique(roots, "root_id")
    need({r["trust_root_id"] for r in roles} == root_ids, "root-role-bijection")
    byroot = {r["root_id"]: r for r in roots}
    for role in roles:
        rid = role["role_id"]
        need(role["authority_id"] in CATEGORIES, "role-authority")
        need(role["issuer_identity"] == role["verifier_identity"] == UNKNOWN, "unknown-signer-identities")
        need(role["verifier_role"] and role["purpose"] and role["historical_verification"], "role-verification-contract")
        need(set(role["independent_from"]) == ids - {rid} and bool(role["separation"]), "role-separation")
        root = byroot[role["trust_root_id"]]
        need(root["role_id"] == rid, "root-role-binding")
        need(all(root[k] == UNKNOWN for k in ("production_source", "operational_owner", "approval_evidence", "key_id",
             "distribution_location", "environment", "dataset_identity", "version", "minimum_accepted_version")), "unknown-production-root")
        need(root["status"] == UNKNOWN and root["synthetic"] is False, "no-synthetic-root")
    for a in c["ownership"]["authorities"]:
        need(set(a["signing_key_role"]) == {r["role_id"] for r in roles if r["authority_id"] == a["authority_id"]}, "authority-role-membership")
        need(a["checkpoint_role"] in ids, "checkpoint-root-source")
        need(byroot["root:" + a["checkpoint_role"]]["production_source"] == UNKNOWN, "checkpoint-root-not-established")
    b = c["bootstrap"]
    need(unique(b["bootstrap_steps"], "id") == {"RT-" + x for x in "ABCDEFGHIJKLMN"}, "root-bootstrap-coverage")
    need(all(x["evidence"] and x["negative_test"] for x in b["bootstrap_steps"]), "root-bootstrap-oracle")
    need(b["unknown_root_action"] == "FAIL_CLOSED", "unknown-root-denial")
    for field in ("artifact_self_authentication", "caller_selected_root_allowed", "historical_signature_grants_current_authority", "synthetic_root_allowed"):
        need(b[field] is False, "root-no-self-authority")
    approval = b["root_change_approval"]
    need(set(approval["required_roles"]) == {"root-custodian", "independent-security-approver"}, "root-independent-approval")
    need(approval["actual_approvers"] == UNKNOWN and approval["self_approval_allowed"] is False, "root-no-self-approval")


def validate_continuity_and_recovery(c):
    d = c["deployment"]
    current = d["current_state_authorities"]
    need(unique(current, "authority_id") == set("ABFHILM"), "current-authority-coverage")
    roles = {r["role_id"] for r in c["ownership"]["signer_roles"]}
    for a in current:
        need(a["publisher_role"] in roles, "current-state-signer")
        need(all(a[k] for k in ("completeness", "replay_requirement", "continuity_requirement", "consistency")), "replay-continuity-required")
        need(a["persistent_implementation"] == UNKNOWN and a["production_verified"] is False, "no-persistent-claim")
    for a in c["ownership"]["authorities"]:
        need(a["currentness_guarantee"] and a["continuity_guarantee"], "authority-continuity")
    need(d["linearizability_claimed"] is False and d["uncertain_auto_promote"] is False, "no-consistency-overclaim")
    need(unique(d["policies"], "policy_id") >= {"max_clock_drift", "authorization_lifetime", "time_evidence_freshness",
        "revocation_visibility_budget", "checkpoint_freshness", "audit_retention", "backup_rpo_rto"}, "policy-coverage")
    for policy in d["policies"]:
        need(policy["status"] == "POLICY_NOT_APPROVED" and policy["value"] is None, "no-invented-policy")
        need(policy["policy_owner"] == policy["approval_evidence"] == UNKNOWN and bool(policy["security_rationale"]), "policy-owner-rationale")
    failures = d["failure_states"]
    need(unique(failures, "state") == {"NOT_COMMITTED", "COMMITTED", "UNCERTAIN", "CORRUPT", "RECOVERY_REQUIRED"}, "failure-category-coverage")
    allowed = {"READ_ONLY_INSPECT", "PRESERVE_EVIDENCE", "DISABLE", "APPROVED_RECOVERY"}
    for f in failures:
        need(all(f[k] for k in ("evidence", "audit_preservation", "checkpoint_requirement", "recovery_approval")), "failure-evidence")
        need(set(f["operator_actions"]) <= allowed and "APPROVED_RECOVERY" in f["operator_actions"], "no-unapproved-repair")
        need(f["reader_consumption"] == ("CONDITIONAL" if f["state"] == "COMMITTED" else "DENY"), "failure-consumption-denial")
    recovery = d["recovery"]
    need(recovery["operational_approval"] == UNKNOWN, "recovery-approval-unknown")
    need(set(recovery["required_approval_roles"]) == {"operational-recovery-owner", "independent-security-approver"}, "recovery-dual-approval")
    need(set(recovery["executor_roles"]) == {"selector", "recovery"}, "recovery-executor-separation")
    need(recovery["self_approval_allowed"] is False and recovery["auto_reenable_allowed"] is False, "recovery-no-self-approval")


def validate_gates_and_dependencies(c):
    d, a = c["deployment"], c["acceptance"]
    tests = a["acceptance_tests"]
    need(unique(tests, "id") == {"PA-" + x for x in CATEGORIES}, "acceptance-coverage")
    bytest = {t["id"]: t for t in tests}
    for t in tests:
        need(t["id"] == "PA-" + t["authority_id"], "acceptance-authority-link")
        need(t["positive_control"] and t["negative_control"] and t["expected_result"] and len(t["evidence"]) >= 4, "measurable-test-oracle")
        need(t["environment"] == "PRODUCTION_EQUIVALENT_REQUIRED" and t["result"] == "NOT_EXECUTED", "no-deployment-test-claim")
        need(t["actual_owner"] == UNKNOWN and t["grants_activation"] is False, "test-not-authorization")
        need(set(t["prerequisites"]) >= {"OWNER_ASSIGNED", "APPROVED", "IMPLEMENTED", "APPROVED_POLICY", "INDEPENDENT_ROOTS"}, "acceptance-prerequisites")
    blockers = a["blockers"]
    need(unique(blockers, "id") >= {"B-" + str(i).zfill(2) for i in range(1, 15)} | {"BT-ROOT", "BT-OWNER", "BT-CONTINUITY", "BT-DURABILITY", "BT-POLICY", "BT-DEPLOYMENT", "BT-REVIEW"}, "blocker-coverage")
    for b in blockers:
        need(b["status"] == "OPEN" and b["deployment_verified"] is False, "blocker-not-closed-by-docs")
        need(set(b["authority_ids"]) <= CATEGORIES and b["authority_ids"], "blocker-authority")
        need(set(b["future_acceptance_tests"]) == {"PA-" + cat for cat in b["authority_ids"]}, "blocker-measurable-coverage")
        need(all(t in bytest for t in b["future_acceptance_tests"]) and b["closure_evidence"], "blocker-closure-evidence")
    byblock = {b["id"]: b for b in blockers}
    need(byblock["BT-ROOT"]["severity"] == byblock["BT-CONTINUITY"]["severity"] == "Critical", "missing-root-continuity-blocks")
    downgrades = d["strict_downgrades"]
    need(unique(downgrades, "id") == {"SD-" + str(i).zfill(2) for i in range(1, 16)}, "strict-downgrade-coverage")
    need(len(unique(downgrades, "requirement")) == 15, "strict-requirement-coverage")
    for s in downgrades:
        need(s["attack"] and s["expected"] in {"STARTUP_DENIED", "CONSUMPTION_DENIED", "FLAGS_DISABLED"}, "strict-denial")
        need(set(s["entrypoint_coverage"]) == {"HTTP", "BACKGROUND", "CLI", "RESTART"}, "strict-entrypoint-coverage")
        need(s["status"] == "DESIGNED" and s["production_verified"] is False, "strict-design-only")
    gates = a["gates"]
    need(unique(gates, "id") == set("ABCDEFGHIJKLMNOPQRSTUVWX"), "gate-coverage")
    for gate in gates:
        need(gate["grants_activation"] is False and gate["operational_status"] == "NOT_VERIFIED", "gate-not-production-permission")
        need(gate["evidence_requirement"] and gate["contract_status"] == "DESIGNED", "gate-evidence")
    for doc in (d, a):
        need(doc["production_activation_ready"] is False and doc["decision"] == "NO-GO", "explicit-no-go")
    need(a["automatic_activation"] is False, "gates-never-authorize")
    packages = d["work_packages"]
    ids = unique(packages, "id")
    need(ids == {"W" + str(i).zfill(2) for i in range(1, 15)}, "work-package-coverage")
    byid = {w["id"]: w for w in packages}
    for w in packages:
        need(set(w["prerequisites"]) <= ids and w["id"] not in w["prerequisites"], "work-package-prerequisites")
        need(set(w["authority_ids"]) <= CATEGORIES and w["authority_ids"], "work-package-authorities")
        need(w["actual_owner"] == w["platform"] == UNKNOWN and w["status"] == "BLOCKED", "implementation-blocked")
        need(all(w[k] for k in ("implementation_boundary", "responsible_role", "security_risk", "independent_verification", "completion_evidence", "stop_condition")), "work-package-evidence")
        need(w["grants_production_activation"] is False, "work-package-no-permission")
    def ancestors(node, visiting=frozenset()):
        need(node not in visiting, "dependency-cycle")
        deps = set(byid[node]["prerequisites"])
        return deps | set().union(*(ancestors(n, visiting | {node}) for n in deps))
    for node in ids:
        ancestors(node)
    need(ancestors("W14") == ids - {"W14"}, "activation-depends-on-all-controls")
    need(set(byid["W11"]["prerequisites"]) >= {"W02", "W03", "W04", "W05", "W06", "W07", "W08", "W09", "W10"}, "strict-policy-all-authorities")
    threats = d["threats"]
    need(unique(threats, "id") == {"TH-" + str(i).zfill(2) for i in range(1, 21)}, "threat-coverage")
    need(len(unique(threats, "scenario")) == 20, "threat-scenario-coverage")
    for t in threats:
        need(t["authority_id"] in CATEGORIES and t["residual_risk_owner"] == UNKNOWN, "threat-risk-owner")
        need(all(t[k] for k in ("attacker_capabilities", "existing_protection", "missing_deployment_control", "detection", "required_acceptance_test")), "threat-evidence")
        need(t["fail_closed"] == "DENY_CONSUMPTION_AND_PRESERVE_EVIDENCE" and t["status"] == "OPEN" and t["deployment_mitigated"] is False, "threat-not-mitigated-by-unit-tests")


def validate(c):
    validate_ownership(c)
    validate_roles_and_roots(c)
    validate_continuity_and_recovery(c)
    validate_gates_and_dependencies(c)


@pytest.fixture(scope="module")
def contracts():
    return load_contracts()


def store_hashes():
    return {name: hashlib.sha256((ROOT / "data" / name).read_bytes()).hexdigest()
            if (ROOT / "data" / name).exists() else "<absent>" for name in sorted(STORES)}


@pytest.fixture(scope="session", autouse=True)
def seven_store_session_integrity():
    before = store_hashes()
    yield before
    assert store_hashes() == before, "contract or regression run wrote a protected production store"


@pytest.mark.parametrize("validator", [validate_ownership, validate_roles_and_roots, validate_continuity_and_recovery, validate_gates_and_dependencies])
def test_tc01_through_tc13_linked_contracts(contracts, validator):
    validator(contracts)


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_tc01_tc02_each_authority_has_explicit_unknown_owners(contracts, category):
    a, = (a for a in contracts["ownership"]["authorities"] if a["authority_id"] == category)
    assert a["status"] == "DESIGNED"
    assert all(a[k] == UNKNOWN for k in ACTUAL_FIELDS)
    assert a["acceptance_test"] in {t["id"] for t in contracts["acceptance"]["acceptance_tests"]}


# Actual wire constants: source evidence rather than a second signature engine.
WIRE_SOURCES = {
    "owner-approval": ("mapping_governance.py", "MAPPING_APPROVAL_PURPOSE"),
    "activation": ("runtime_activation_design.py", "ACTIVATION_AUTHORIZATION_PURPOSE"),
    "canonical-publisher": ("authoritative_governance.py", "CANONICAL_MANIFEST_TYPE"),
    "governance-view": ("authoritative_governance.py", "GOVERNANCE_VIEW_ATTESTATION_TYPE"),
    "ledger-checkpoint": ("authoritative_governance.py", "LEDGER_CHECKPOINT_TYPE"),
    "governance-revocation": ("authoritative_governance.py", "REVOCATION_MANIFEST_TYPE"),
    "observation": ("observation_authority.py", "SOURCE_OBSERVATION_ATTESTATION_TYPE"),
    "time": ("trusted_time_authority.py", "TIME_ATTESTATION_PURPOSE"),
    "lifecycle": ("activation_supersession.py", "SUPERSESSION_PURPOSE"),
    "lifecycle-current": ("activation_supersession.py", "STATE_PURPOSE"),
    "lifecycle-checkpoint": ("activation_supersession.py", "CHECKPOINT_PURPOSE"),
    "revocation": ("activation_revocation_authority.py", "REVOCATION_PURPOSE"),
    "revocation-current": ("activation_revocation_authority.py", "STATE_PURPOSE"),
    "revocation-checkpoint": ("activation_revocation_authority.py", "CHECKPOINT_PURPOSE"),
    "publisher": ("persistent_shadow_snapshots.py", "PUBLICATION_PURPOSE"),
    "selector": ("shadow_snapshot_control.py", "SELECTION_PURPOSE"),
    "control-checkpoint": ("shadow_snapshot_control.py", "CHECKPOINT_PURPOSE"),
    "audit-checkpoint": ("shadow_activation_audit_store.py", "SHADOW_AUDIT_CHECKPOINT_TYPE"),
    "recovery": ("recovery_authority.py", "RECOVERY_EVIDENCE_TYPE"),
}


@pytest.mark.parametrize("role", sorted(WIRE_SOURCES))
def test_tc04_signer_purpose_reconciles_with_actual_code(contracts, role):
    name, constant = WIRE_SOURCES[role]
    tree = ast.parse(historical_text("utils/" + name))
    values = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == constant for t in node.targets)
              and isinstance(node.value, ast.Constant)]
    assert len(values) == 1
    record, = (r for r in contracts["ownership"]["signer_roles"] if r["role_id"] == role)
    assert record["purpose"] == values[0]


@pytest.mark.parametrize("mutation,code", [
    ("missing_authority", "eighteen-authorities"),
    ("invented_owner", "unknown-real-owner-or-source"),
    ("invented_root", "unknown-production-root"),
    ("synthetic_root", "no-synthetic-root"),
    ("shared_signing_role", "role-separation"),
    ("checkpoint_wrong_root", "root-role-bijection"),
    ("removed_continuity", "replay-continuity-required"),
    ("self_approved_root", "root-no-self-approval"),
    ("self_approved_recovery", "recovery-no-self-approval"),
    ("invented_lifetime", "no-invented-policy"),
    ("uncertain_consumable", "failure-consumption-denial"),
    ("bypass_entrypoint", "strict-entrypoint-coverage"),
    ("blocker_no_test", "blocker-measurable-coverage"),
    ("test_no_negative", "measurable-test-oracle"),
    ("gate_auto_authorizes", "gate-not-production-permission"),
    ("dependency_cycle", "dependency-cycle"),
    ("implementation_claim", "implementation-blocked"),
    ("threat_claim", "threat-not-mitigated-by-unit-tests"),
    ("state_promotion", "no-state-promotion"),
    ("production_go", "explicit-no-go"),
])
def test_contract_mutations_fail_closed(contracts, mutation, code):
    c = deepcopy(contracts)
    o, b, d, a = (c[k] for k in ("ownership", "bootstrap", "deployment", "acceptance"))
    if mutation == "missing_authority": o["authorities"].pop()
    elif mutation == "invented_owner": o["authorities"][0]["operational_owner"] = "unaccepted-owner"
    elif mutation == "invented_root": b["roots"][0]["production_source"] = "caller-key.pem"
    elif mutation == "synthetic_root": b["roots"][0]["synthetic"] = True
    elif mutation == "shared_signing_role": o["signer_roles"][0]["independent_from"].pop()
    elif mutation == "checkpoint_wrong_root": o["signer_roles"][0]["trust_root_id"] = "root:caller"
    elif mutation == "removed_continuity": d["current_state_authorities"][0]["continuity_requirement"] = ""
    elif mutation == "self_approved_root": b["root_change_approval"]["self_approval_allowed"] = True
    elif mutation == "self_approved_recovery": d["recovery"]["self_approval_allowed"] = True
    elif mutation == "invented_lifetime": d["policies"][0]["value"] = 86400
    elif mutation == "uncertain_consumable": next(f for f in d["failure_states"] if f["state"] == "UNCERTAIN")["reader_consumption"] = "CONDITIONAL"
    elif mutation == "bypass_entrypoint": d["strict_downgrades"][0]["entrypoint_coverage"].remove("BACKGROUND")
    elif mutation == "blocker_no_test": a["blockers"][0]["future_acceptance_tests"] = []
    elif mutation == "test_no_negative": a["acceptance_tests"][0]["negative_control"] = ""
    elif mutation == "gate_auto_authorizes": a["gates"][0]["grants_activation"] = True
    elif mutation == "dependency_cycle": d["work_packages"][0]["prerequisites"] = ["W14"]
    elif mutation == "implementation_claim": d["work_packages"][0]["status"] = "IMPLEMENTED"
    elif mutation == "threat_claim": d["threats"][0]["deployment_mitigated"] = True
    elif mutation == "state_promotion": o["authorities"][0]["progress"]["PRODUCTION_AUTHORIZED"] = "YES"
    elif mutation == "production_go": d["production_activation_ready"] = True
    else: raise AssertionError(mutation)
    with pytest.raises(ValueError, match=code):
        validate(c)


def test_dependency_diagram_matches_structured_register(contracts):
    text = historical_text("docs/security/" + NAMES["deployment"])
    edges = set(re.findall(r"^ (W[0-9]{2})(?:\[[^\]]*\])? --> (W[0-9]{2})", text, re.M))
    expected = {(pre, w["id"]) for w in contracts["deployment"]["work_packages"] for pre in w["prerequisites"]}
    assert edges == expected


def test_tc14_production_flags_stay_disabled(contracts):
    from utils.runtime_mapping_policy import RUNTIME_MAPPING_ACTIVATION, PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    from utils.shadow_runtime_reader import shadow_reader_status
    from utils.runtime_activation_design import ACTIVATION_ENABLE_API, RUNTIME_READER_API
    flags = contracts["deployment"]["production_flags"]
    status = shadow_reader_status()
    assert flags["RUNTIME_MAPPING_ACTIVATION"] == RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED"
    assert flags["PRODUCTION_MAPPING_WRITE_AUTHORIZATION"] == PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"
    assert flags["PRODUCTION_RUNTIME_READER"] == status["production_runtime_reader"] == status["production_activation"] == "NOT_ENABLED"
    assert ACTIVATION_ENABLE_API is RUNTIME_READER_API is None


def test_tc15_seven_protected_stores(contracts, seven_store_session_integrity):
    expected = contracts["acceptance"]["protected_stores"]
    assert set(expected) == STORES
    # Original recorded hashes remain in the exact historical Git document.
    # Private JSON was never committed: do not present archived metadata as old bytes.
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in expected.values())
    assert len(expected) == 7
    # Active safety assertion: every currently present/absent store stays identical
    # during this run, independently of legitimate changes between authorized runs.
    assert store_hashes() == seven_store_session_integrity


def test_tc16_no_runtime_integration_new_endpoints_keys_or_weakened_existing_tests(contracts):
    frozen = contracts["acceptance"]["immutable_repository"]
    allowed = set(frozen["allowed_change_paths"])
    assert len(allowed) == 7 and "tests/test_production_trust_contracts.py" in allowed
    assert all(p.startswith("docs/security/runtime-activation-production-") and p.endswith(".md")
               for p in allowed - {"tests/test_production_trust_contracts.py"})
    archived = historical_files()
    assert set(archived) - allowed == set(frozen["paths"])
    h = hashlib.sha256()
    for name in frozen["paths"]:
        data = archived[name].replace(b"\r\n", b"\n")
        h.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
    assert h.hexdigest() == frozen["normalized_sha256"], "historical scope/content changed"
    # The current authorized change has its own allowlist and executable boundary
    # assertions in test_trust_root_bootstrap; historic scope is never overwritten.
