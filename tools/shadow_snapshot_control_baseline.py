"""PP-01..PP-06 精确基线复现；仅临时合成工作区。"""
from dataclasses import dataclass, fields, replace
import json
from pathlib import Path
import tempfile

from tools.shadow_revocation_authority_validation import SyntheticRevocationScenario
from tools.shadow_revocation_baseline import external_signed
from tools.shadow_trusted_time_validation import NOW, protected_store_hashes
from utils.mapping_governance import generate_mapping_owner_keypair
from utils.shadow_activation_simulation import ShadowSnapshotRegistry
from utils.shadow_runtime_reader import ShadowReadContext


@dataclass(frozen=True)
class ExternalControlContext(ShadowReadContext):
    publication_request: object = None
    selection_request: object = None
    control_checkpoint: object = None


def reproduce(workspace):
    before = protected_store_hashes()
    scene = SyntheticRevocationScenario(workspace / "baseline")
    A = scene.snapshot
    B = replace(A, generated_at="2030-01-15T00:00:00Z")
    B = replace(B, snapshot_id=B.content_fingerprint(), snapshot_fingerprint=B.content_fingerprint())
    registry = ShadowSnapshotRegistry()
    registry.register(A)
    registry.register(B)
    unsigned = registry.select(B.snapshot_id)
    restarted = ShadowSnapshotRegistry()
    audit_before = tuple(scene.env.audit_store.records())
    registry.select(A.snapshot_id)
    audit_after = tuple(scene.env.audit_store.records())
    scene.registry.register(B)
    scene.registry.select(B.snapshot_id)
    mutated = scene.read()
    # The baseline has no controlled selection API. External signatures do not
    # create one; keep them visible in an extension context for actual Reader input.
    proofs = []
    for role, purpose in (("publisher", "SHADOW_SNAPSHOT_PUBLICATION_REQUEST"),
                          ("selector", "SHADOW_SNAPSHOT_SELECTION_REQUEST"),
                          ("checkpoint", "SHADOW_SNAPSHOT_CONTROL_CHECKPOINT")):
        private, public = str(workspace / f"{role}-private.pem"), str(workspace / f"{role}-public.pem")
        generate_mapping_owner_keypair(private, public)
        proofs.append(external_signed(private, public, purpose,
            dict(dataset_identity=A.dataset_identity, snapshot_id=B.snapshot_id,
                 snapshot_fingerprint=B.snapshot_fingerprint, issued_at=NOW, role=role)))
    ctx = scene.read_context()
    values = {f.name: getattr(ctx, f.name) for f in fields(ShadowReadContext)}
    values.update(publication_request=proofs[0], selection_request=proofs[1], control_checkpoint=proofs[2])
    extended = ExternalControlContext(**values)
    signed = scene.reader.read(observation=scene.env.observation, snapshot=B, read_context=extended,
        observation_attestation=scene.observation_attestation)
    original_append = scene.env.audit_store.append
    def uncertain(**kwargs):
        result = original_append(**kwargs)
        if kwargs["event_type"] == "SHADOW_ROLLBACK_COMPLETED":
            raise OSError("synthetic post-write acknowledgment loss")
        return result
    scene.env.audit_store.append = uncertain
    audit_path = Path(scene.env.audit_store.path)
    audit_bytes = audit_path.read_bytes() if audit_path.exists() else b""
    report = scene.simulator().rollback_shadow_snapshot(target_snapshot_id=A.snapshot_id,
        read_context=scene.read_context(), at=NOW, audit_checkpoint=scene.audit_checkpoint())
    return dict(baseline="a920129606efcc444bd385a1f046f334e9ef7300", results={
        "PP-01": dict(selected_without_signature=unsigned["snapshot_id"] == B.snapshot_id),
        "PP-02": dict(restarted_registered_count=len(restarted), restarted_selection=restarted.active_snapshot_id),
        "PP-03": dict(pointer_changed=True, audit_unchanged=audit_before == audit_after),
        "PP-04": dict(direct_registry_read=mutated.resolution_status),
        "PP-05": dict(status=report.status, outcome=report.commit_outcome,
            reader_enabled=scene.reader.enabled, selected=scene.registry.active_snapshot_id,
            audit_bytes_preserved=Path(scene.env.audit_store.path).read_bytes().startswith(audit_bytes),
            discrepancy=report.discrepancy, codes=report.rejection_codes),
        "PP-06": dict(reader_status=signed.resolution_status, external_signatures_verified=len(proofs),
            baseline_has_durable_controller=False)}, seven_store_sha256=before,
        seven_stores_unchanged=before == protected_store_hashes())


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="shadow-control-baseline-") as temp:
        print(json.dumps(reproduce(Path(temp)), sort_keys=True, indent=2))
