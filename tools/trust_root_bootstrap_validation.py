"""Executable offline bootstrap pilot. Every credential is temporary/synthetic.

python -m tools.trust_root_bootstrap_validation
The output schema and scenario ordering are deterministic; random keys and
temporary paths are deliberately not included. A failed invariant exits nonzero.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile

from tools.shadow_trust_chain_validation import IntegratedScenario, production_boundary_audit
from tools.shadow_trusted_time_validation import NOW, DATASET, protected_store_hashes, require
from utils.authoritative_governance import build_authoritative_governance_view, ledger_identity
from utils.activation_supersession import ActivationSupersessionVerifier, AuthorizationLifecycleEvidence, checkpoint_payload
from utils.mapping_governance import generate_mapping_owner_keypair, load_trusted_public_key
from utils.trusted_artifacts import sign_trusted_artifact, verify_trusted_artifact, TrustedArtifactError
from utils.trusted_time_authority import TrustedTimeVerifier, sign_time_attestation, parse_utc
from utils.trust_root_bootstrap import (
    APPROVAL, BUNDLE, TRANSITION, ROLE_PURPOSES, BootstrapError, BootstrapPolicy,
    ContinuityEvidence, FixedAuthorityContext, TrustRootVerifier,
    approval_body, bundle_body, transition_body,
)

ENVIRONMENT = "synthetic-offline-lab"
BEFORE = "2030-01-14T00:00:00+00:00"
AFTER = "2030-01-16T00:00:00+00:00"
AUTHORITIES = {"trusted-time": "synthetic-time-authority", "lifecycle": "synthetic-lifecycle-authority",
    "revocation": "synthetic-revocation-authority", "root-bundle": "synthetic-root-executor"}


def rejected(call, expected):
    try:
        call()
    except TrustedArtifactError as exc:
        require(expected in exc.rejection_codes, f"expected {expected}, observed {exc.rejection_codes}")
        return dict(status="REJECTED", rejection_codes=list(exc.rejection_codes))
    raise RuntimeError("security boundary accepted: " + expected)


class SyntheticRootFixture:
    """The harness provisions pins/expectations separately from verifier input."""

    def __init__(self, workspace, *, key_paths=None, dataset=DATASET, environment=ENVIRONMENT):
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True)
        self.dataset, self.environment = dataset, environment
        self.public = dict(key_paths or {})
        self.private = {}
        for role in ROLE_PURPOSES:
            if role not in self.public:
                private, public = self.keypair(role)
                self.private[role], self.public[role] = private, public
        self.approval_private, self.approval_public = self.keypair("independent-approval")
        # Provision from separately generated key files before building the bundle.
        self.pin = load_trusted_public_key(self.public["root-bundle"])["public_key_id"]
        self.approval_pin = load_trusted_public_key(self.approval_public)["public_key_id"]
        self.policy = BootstrapPolicy(environment, dataset, self.pin, self.public["root-bundle"],
            "synthetic-independent-approver", self.approval_pin, self.approval_public)
        self.initial_body = self.body(self.public, generation=1)
        self.initial = self.sign_bundle(self.initial_body, self.private["root-bundle"])
        self.initial_approval = self.approve(None, self.initial_body)
        self.initial_continuity = self.anchor(self.initial_body, "independently-provisioned-initial-expectation")
        self.verifier = TrustRootVerifier(self.policy)

    def keypair(self, name):
        private = str(self.workspace / (name + "-private.pem"))
        public = str(self.workspace / (name + "-public.pem"))
        generate_mapping_owner_keypair(private, public)
        return private, public

    def body(self, paths, *, generation, predecessor="", revoked=()):
        records = [dict(authority_identity=AUTHORITIES.get(role, "synthetic-" + role), signer_role=role,
            public_key_pem=Path(path).read_text(encoding="ascii"),
            public_key_fingerprint=load_trusted_public_key(path)["public_key_id"],
            authorized_purposes=sorted(ROLE_PURPOSES[role]), signing_not_before=BEFORE, signing_not_after=AFTER)
            for role, path in sorted(paths.items())]
        return bundle_body(bundle_identity="synthetic-bundle-" + str(generation), environment_identity=self.environment,
            dataset_identity=self.dataset, root_generation=generation, minimum_accepted_version=generation,
            authorities=records, revoked_key_identities=revoked, predecessor_bundle_fingerprint=predecessor,
            issuer_identity=AUTHORITIES["root-bundle"], approval_reference="independent-approval-" + str(generation), issued_at=NOW)

    @staticmethod
    def sign_bundle(body, private):
        return sign_trusted_artifact(private, artifact_type=BUNDLE, payload=body)

    def approve(self, old, new, **changes):
        payload = approval_body(old, new, approver_identity="synthetic-independent-approver",
            requester_identity="synthetic-requester", executor_identity=AUTHORITIES["root-bundle"], issued_at=NOW)
        return sign_trusted_artifact(self.approval_private, artifact_type=APPROVAL, payload={**payload, **changes})

    def anchor(self, body, reference="independently-provisioned-next-expectation"):
        # Called by the issuance/test harness, never by TrustRootVerifier on input.
        return ContinuityEvidence(self.environment, self.dataset, body["root_generation"],
            body["bundle_fingerprint"], body["minimum_accepted_version"], reference)

    def bootstrap(self, **changes):
        values = dict(approval=self.initial_approval, continuity=self.initial_continuity, current_time=NOW)
        self.handle = self.verifier.bootstrap(self.initial, **{**values, **changes})
        self.fixed = FixedAuthorityContext(self.handle, key_paths=self.public)
        return self.handle

    def evidence(self, role="governance-view", **changes):
        purpose = ROLE_PURPOSES[role][0]
        payload = dict(issued_at=NOW, dataset_identity=self.dataset, environment_identity=self.environment,
            authority_identity=AUTHORITIES.get(role, "synthetic-" + role), observation="synthetic-only")
        return sign_trusted_artifact(self.private[role], artifact_type=purpose, payload={**payload, **changes})

    def next_bundle(self, *, replaced_roles=("root-bundle", "governance-view"), revoked_roles=()):
        paths, private = dict(self.public), dict(self.private)
        retired = set(self.initial_body["revoked_key_identities"])
        for role in replaced_roles:
            retired.add(load_trusted_public_key(paths[role])["public_key_id"])
            private[role], paths[role] = self.keypair("replacement-" + role)
        retired.update(load_trusted_public_key(paths[role])["public_key_id"] for role in revoked_roles)
        body = self.body(paths, generation=2, predecessor=self.initial_body["bundle_fingerprint"], revoked=retired)
        approval = self.approve(self.initial_body, body)
        bundle = self.sign_bundle(body, private["root-bundle"])
        transition = sign_trusted_artifact(self.private["root-bundle"], artifact_type=TRANSITION,
            payload=transition_body(self.initial_body, body, approval))
        return dict(body=body, bundle=bundle, approval=approval, transition=transition,
            new_root_public_key_path=paths["root-bundle"], continuity=self.anchor(body), paths=paths, private=private)

    def rotate(self, value, **changes):
        values = {key: value[key] for key in ("approval", "transition", "new_root_public_key_path", "continuity")}
        return self.verifier.transition(value["bundle"], current_time=NOW, **{**values, **changes})


class RoleSeparatedShadowScenario(IntegratedScenario):
    """Existing engines with dedicated provenance and checkpoint keys.

    Only the synthetic fixture changes. No production/security module is rewired.
    Lifecycle and revocation each keep their existing related state/event purposes.
    """

    def extra_key(self, role):
        public_name = role.replace("-", "_") + "_public"
        if not hasattr(self, public_name):
            keydir = Path(self.env.owner_public).parent
            private, public = str(keydir / (role + "-private.pem")), str(keydir / (role + "-public.pem"))
            generate_mapping_owner_keypair(private, public)
            setattr(self, public_name, public)
            setattr(self, role.replace("-", "_") + "_private", private)
        return getattr(self, role.replace("-", "_") + "_private"), getattr(self, public_name)

    def governance_view(self, revocations=()):
        paths = {role: self.extra_key(role) for role in ("ledger-checkpoint", "canonical-publisher", "governance-revocation")}
        def sign(role, **payload):
            return sign_trusted_artifact(paths[role][0], artifact_type=ROLE_PURPOSES[role][0],
                payload={**payload, "dataset_identity": DATASET, "issued_at": self.clock().isoformat()})
        entries = self.ledger.entries()
        checkpoint = sign("ledger-checkpoint", ledger_identity=ledger_identity(self.ledger),
            chain_tip=entries[-1].entry_hash, entry_count=len(entries))
        canonical = sign("canonical-publisher", baseline_fingerprint=self.env.snapshot.baseline_fingerprint,
            canonical_ids=list(self.env.read_context().known_canonical_ids))
        revocation = sign("governance-revocation", revocations=list(revocations))
        self.governance_inputs = dict(ledger_path=self.ledger.path, dataset_identity=DATASET,
            current_baseline_fingerprint=self.env.snapshot.baseline_fingerprint, trusted_owner_public_key_path=self.env.owner_public,
            ledger_checkpoint=checkpoint, ledger_checkpoint_trusted_public_key_path=paths["ledger-checkpoint"][1],
            canonical_manifest=canonical, canonical_manifest_trusted_public_key_path=paths["canonical-publisher"][1],
            revocation_manifest=revocation, revocation_manifest_trusted_public_key_path=paths["governance-revocation"][1],
            view_signing_private_key_path=self.governance_private, view_trusted_public_key_path=self.governance_public,
            current_time=self.clock().isoformat())
        return build_authoritative_governance_view(**self.governance_inputs)

    def new_lifecycle_verifier(self):
        _, checkpoint = self.extra_key("lifecycle-checkpoint")
        return ActivationSupersessionVerifier(self.context.dataset_identity, AUTHORITIES["lifecycle"],
            self.lifecycle_public, checkpoint, self.env.authorizer_public, self.strict)

    def make_bundle(self, *args, **kwargs):
        original = super().make_bundle(*args, **kwargs)
        private, _ = self.extra_key("lifecycle-checkpoint")
        checkpoint = sign_trusted_artifact(private, artifact_type=ROLE_PURPOSES["lifecycle-checkpoint"][0],
            payload=checkpoint_payload(original.state, session_challenge=self.lifecycle.session_challenge,
                issued_at=self.clock().isoformat()))
        return AuthorizationLifecycleEvidence(original.authorizations, original.transitions, original.state, checkpoint)

    def role_paths(self):
        return {"owner-approval": self.env.owner_public, "activation": self.env.authorizer_public,
            "governance-view": self.governance_public, "trusted-time": self.public,
            "lifecycle": self.lifecycle_public, "revocation": self.revocation_public,
            "audit-checkpoint": self.checkpoint_public, "control-checkpoint": self.control_checkpoint_public,
            "recovery": self.recovery_public, "observation": self.observation_public,
            "ledger-checkpoint": self.ledger_checkpoint_public, "canonical-publisher": self.canonical_publisher_public,
            "governance-revocation": self.governance_revocation_public, "lifecycle-checkpoint": self.lifecycle_checkpoint_public,
            "revocation-checkpoint": self.revocation_checkpoint_public, "publisher": self.publisher_public,
            "selector": self.selector_public}


def baseline_reproduction(workspace):
    """RB-01..06 exercise the unchanged legacy primitive/session boundaries."""
    fixture = SyntheticRootFixture(workspace)
    purpose = ROLE_PURPOSES["governance-view"][0]
    a = fixture.evidence()
    b = fixture.evidence("recovery")
    replacement = sign_trusted_artifact(fixture.private["recovery"], artifact_type=purpose, payload=a.payload())
    def verify(artifact, role="governance-view", expected=purpose):
        return verify_trusted_artifact(artifact, expected_artifact_type=expected,
            trusted_public_key_path=fixture.public[role], current_time=NOW, max_age_seconds=60)
    require(verify(a) == a.payload(), "RB-01")
    rejected(lambda: verify(replacement), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")
    require(verify(replacement, "recovery") == a.payload(), "RB-02")
    require(set(load_trusted_public_key(fixture.public["recovery"])) == {"key", "raw", "public_key_id"}, "RB-03")
    old = sign_trusted_artifact(fixture.private["governance-view"], artifact_type=purpose, payload={**a.payload(), "root_generation": 1})
    new = sign_trusted_artifact(fixture.private["governance-view"], artifact_type=purpose, payload={**a.payload(), "root_generation": 2})
    verify(new); require(verify(old)["root_generation"] == 1, "RB-04")
    clock = lambda: parse_utc(NOW)
    def time(): return TrustedTimeVerifier(DATASET, AUTHORITIES["trusted-time"], fixture.public["trusted-time"], clock)
    def token(v, generation):
        return sign_time_attestation(fixture.private["trusted-time"], dataset_identity=DATASET,
            authority_identity=AUTHORITIES["trusted-time"], attested_at=NOW, expires_at="2030-01-15T00:01:00+00:00",
            generation=generation, evidence_id="baseline-" + str(generation), session_challenge=v.session_challenge)
    first = time(); initial = token(first, 1)
    first.verify(initial, dataset_identity=DATASET); first.verify(token(first, 2), dataset_identity=DATASET)
    rejected(lambda: first.verify(initial, dataset_identity=DATASET), "TRUSTED_TIME_GENERATION_ROLLBACK")
    restart = time()
    rejected(lambda: restart.verify(initial, dataset_identity=DATASET), "TRUSTED_TIME_SESSION_MISMATCH")
    restart.verify(token(restart, 1), dataset_identity=DATASET)
    rejected(lambda: verify(b), "TRUSTED_ARTIFACT_TYPE_MISMATCH")
    return {"RB-01": "CALLER_KEY_VERIFIES", "RB-02": "ARTIFACT_AND_KEY_REPLACEMENT_VERIFIES_FIXED_KEY_REJECTS",
        "RB-03": "KEY_FILE_DOES_NOT_ESTABLISH_REGISTRATION", "RB-04": "GENERIC_SIGNATURE_HAS_NO_ROOT_VERSION_RULE",
        "RB-05": "SESSION_ROLLBACK_AND_OLD_CHALLENGE_REJECT_NEW_SIGNED_SESSION_CAN_RESET_GENERATION",
        "RB-06": "EXISTING_PURPOSE_SEPARATION_REJECTS"}


def run_acceptance(workspace):
    before = protected_store_hashes()
    root = SyntheticRootFixture(Path(workspace) / "root")
    root.bootstrap()
    original = root.evidence()
    require(root.fixed.verify_artifact("governance-view", original, current_time=NOW) == original.payload(), "valid role evidence")
    steps = [dict(scenario="role_separated_keys", status="VERIFIED"),
        dict(scenario="independently_provisioned_pin", status="VERIFIED"),
        dict(scenario="initial_bundle", status=root.verifier.status),
        dict(scenario="fixed_authority_context", status="VERIFIED"),
        dict(scenario="role_evidence", status="VERIFIED")]
    attacker_private, attacker_public = root.keypair("caller-attacker")
    forged = sign_trusted_artifact(attacker_private, artifact_type=BUNDLE, payload=root.initial_body)
    steps.append(dict(scenario="forged_bundle", **rejected(lambda: TrustRootVerifier(root.policy).bootstrap(forged,
        approval=root.initial_approval, continuity=root.initial_continuity, current_time=NOW), "TRUSTED_ARTIFACT_SIGNER_UNTRUSTED")))
    paths = {**root.public, "governance-view": attacker_public}
    steps.append(dict(scenario="caller_key_override", **rejected(lambda: FixedAuthorityContext(root.handle, key_paths=paths), "ROOT_UNTRUSTED")))
    successor = root.next_bundle(revoked_roles=("audit-checkpoint",))
    steps.append(dict(scenario="unauthorized_rotation", **rejected(lambda: root.rotate(successor, approval=None), "ROOT_APPROVAL_MISSING")))
    handle = root.rotate(successor)
    current = FixedAuthorityContext(handle, key_paths=successor["paths"])
    steps.append(dict(scenario="independently_approved_transition", status=root.verifier.status, generation=2))
    steps.append(dict(scenario="old_root_version", **rejected(lambda: root.verifier.bootstrap(root.initial,
        approval=root.initial_approval, continuity=root.initial_continuity, current_time=NOW), "ROOT_VERSION_ROLLBACK")))
    steps.append(dict(scenario="revoked_key", **rejected(lambda: current.key_path("audit-checkpoint",
        ROLE_PURPOSES["audit-checkpoint"][0], current_time=NOW), "ROOT_REVOKED")))
    historical = root.verifier.inspect_historical(root.initial_body["bundle_fingerprint"], "governance-view", original,
        public_key_path=root.public["governance-view"], current_time=NOW)
    require(historical["cryptographically_valid"] and not historical["current_authorization_eligible"], "history is not authority")
    steps.append(dict(scenario="historical_signature_inspection", **historical))
    # Omit random fingerprints from the deterministic report.
    steps[-1].pop("bundle_fingerprint")
    root.verifier._watermark = None
    steps.append(dict(scenario="lost_continuity", **rejected(lambda: current.key_path("governance-view",
        ROLE_PURPOSES["governance-view"][0], current_time=NOW), "CONTINUITY_UNVERIFIED")))
    scene = RoleSeparatedShadowScenario(Path(workspace) / "strict-shadow")
    shadow = SyntheticRootFixture(Path(workspace) / "shadow-bootstrap", key_paths=scene.role_paths())
    shadow.bootstrap()
    require(scene.selected().is_resolved, "existing complete strict chain")
    context = scene.read_context()
    resolved = shadow.fixed.read_shadow(scene.reader, observation=scene.env.observation, snapshot=scene.snapshot,
        read_context=context, observation_attestation=scene.observation_attestation)
    require(resolved.is_resolved and resolved.is_shadow_only and not resolved.is_production_authorization, "fixed strict chain")
    override = replace(context, observation_trusted_public_key_path=attacker_public)
    steps.append(dict(scenario="read_context_override", **rejected(lambda: shadow.fixed.read_shadow(scene.reader,
        observation=scene.env.observation, snapshot=scene.snapshot, read_context=override,
        observation_attestation=scene.observation_attestation), "FIXED_AUTHORITY_OVERRIDE")))
    steps.append(dict(scenario="existing_fully_strict_shadow", status=resolved.resolution_status))
    flags = production_boundary_audit()
    require(protected_store_hashes() == before, "protected production stores changed")
    steps.append(dict(scenario="production_flags_disabled", status="VERIFIED"))
    steps.append(dict(scenario="seven_store_integrity", status="VERIFIED"))
    return dict(schema_version="1.0.0", success=True, synthetic_only=True, steps=steps,
        production_flags={k: v for k, v in flags.items() if k.startswith("production_")},
        seven_stores_unchanged=True, production_activation_ready=False, decision="NO-GO")


def main():
    with tempfile.TemporaryDirectory(prefix="offline-trust-bootstrap-") as workspace:
        result = run_acceptance(Path(workspace))
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
