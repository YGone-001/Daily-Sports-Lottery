"""
影子运行期信任边界修正测试（AC-01 .. AC-08 与 GC-01 .. GC-26）
==============================================================
本文件先以**修正后的期望**复现基线缺陷，再作为修正后的回归防线：

- 已登记但未显式选择的快照不得被注册表驱动的读取器解析；
- 已选择快照之外的快照不得被消费（对象与 ID 两种入口）；
- 同一源身份键的全部 Canonical 所有者都必须参与冲突判定；
- 紧急停用后裸 ``re_enable`` 不得恢复消费；
- 回滚的提交点必须先于有效指针切换（审计失败时 fail closed）；
- 审计追加前必须完整校验既有历史（含空行 / 未终止尾记录）；
- 句柄代际有效性不得被表述为当前授权有效性。

全部使用隔离合成夹具与一次性 Ed25519 密钥，绝不触碰生产数据。
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass

import pytest

from tools.shadow_runtime_activation import (
    ALT_CANONICAL_ID,
    CANONICAL_ID,
    DATASET,
    REVOCATION_REFERENCE,
    build_synthetic_shadow_fixture,
)
from utils.mapping_governance import (
    ProposalAction,
    build_approval_body,
    proposal_from_observations,
    sign_mapping_approval,
)
from utils.runtime_activation_authorization import (
    ActivationVerificationContext,
    build_activation_authorization_body,
    build_shadow_activation_snapshot,
    sign_runtime_activation_authorization,
)
from utils.runtime_mapping_eligibility import (
    RuntimeMappingContext,
    caller_asserted_provenance,
    evaluate_runtime_mapping_eligibility,
)
from utils.shadow_activation_audit_store import (
    ShadowActivationAuditStore,
    ShadowAuditCommitReceipt,
    ShadowAuditIntegrityError,
)
from utils.shadow_activation_simulation import (
    ShadowActivationSimulator,
    ShadowSnapshotRegistry,
)
from utils.shadow_runtime_reader import (
    REENABLE_REJECTION_CODES,
    REENABLE_REJECTION_CODE_SET,
    SHADOW_READER_VERSION,
    ShadowAuthorizationEvidence,
    ShadowReadContext,
    ShadowReenablementError,
    ShadowRuntimeReader,
)

WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("shadow_authority_env")
    return build_synthetic_shadow_fixture(str(workspace))


def _ctx(env, **overrides) -> ShadowReadContext:
    return env.read_context(**overrides)


def _second_snapshot(env):
    """构建第二个**兼容**快照（不同源身份，同一数据集与基线）。"""
    other_observation = env.other_observation
    proposal = proposal_from_observations(
        [other_observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=ALT_CANONICAL_ID,
        existing_canonical_owner=ALT_CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=env.snapshot.baseline_fingerprint,
        reason="second compatible shadow binding",
        identity_evidence_type="strong_competition_number",
        operator="shadow-operator",
    )
    owner_private = os.path.join(
        os.path.dirname(env.owner_public), "owner_private.pem"
    )
    approval = sign_mapping_approval(
        owner_private,
        build_approval_body(
            proposal=proposal,
            source_observation_fingerprint=other_observation.semantic_fingerprint(),
            dataset_identity=DATASET,
            operator="shadow-operator",
            approver="shadow-owner",
            approved_at="2030-01-01T00:00:00+00:00",
            reason="synthetic owner approval",
        ),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        other_observation,
        RuntimeMappingContext(
            dataset_identity=DATASET,
            current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
            evidence_provenance=caller_asserted_provenance(
                dataset_identity=DATASET,
                baseline_fingerprint=env.snapshot.baseline_fingerprint,
            ),
            canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
            trusted_public_key_path=env.owner_public,
            expected_approver="shadow-owner",
            expected_operator="shadow-operator",
            recorded_proposal_id=proposal.proposal_id,
            recorded_proposal_fingerprint=proposal.fingerprint(),
            recorded_proposal_state="APPROVED",
        ),
        evaluated_at="2030-01-01T00:00:00+00:00",
    )
    assert decision.eligibility_result == "ELIGIBLE"
    authorization = sign_runtime_activation_authorization(
        env.authorizer_private,
        build_activation_authorization_body(
            decision=decision,
            proposal=proposal,
            dataset_identity=DATASET,
            authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
            authorizer="shadow-authorizer",
            operator="shadow-operator",
            authorized_at="2030-01-01T00:00:00+00:00",
            expires_at="2030-02-01T00:00:00+00:00",
            revocation_reference=REVOCATION_REFERENCE,
            reason="second synthetic authorization",
        ),
    )
    snapshot = build_shadow_activation_snapshot(
        authorizations=[authorization],
        verification_context=ActivationVerificationContext(
            dataset_identity=DATASET,
            current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
            current_time=WITHIN_WINDOW,
            trusted_public_key_path=env.authorizer_public,
            eligibility_decision=decision,
            proposal=proposal,
        ),
        observations=[other_observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    return snapshot, authorization, ShadowAuthorizationEvidence(
        authorization=authorization, decision=decision, proposal=proposal
    )


def _evidence(env) -> ShadowAuthorizationEvidence:
    return ShadowAuthorizationEvidence(
        authorization=env.authorization,
        decision=env.decision,
        proposal=env.proposal,
    )


@dataclass
class _FailingAuditStore:
    """注入审计追加失败的包装器（其余事件与回读委托给真实存储）。"""

    real: ShadowActivationAuditStore
    fail_event_types: tuple[str, ...] = ()
    partial_write: bool = False

    def append(self, **kwargs):
        if kwargs.get("event_type") in self.fail_event_types:
            if self.partial_write:
                with open(self.real.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write('{"sequence": 999, "event_type": "SHADOW_ROLL')
                    handle.flush()
                    os.fsync(handle.fileno())
            raise OSError("injected shadow audit append failure")
        return self.real.append(**kwargs)

    def records(self):
        return self.real.records()

    def verify(self) -> dict:
        return self.real.verify()

    def chain_tip(self) -> str:
        return self.real.chain_tip()

    def store_identity(self) -> str:
        return self.real.store_identity()

    def receipt_for(self, record):
        return self.real.receipt_for(record)


# ---------------------------------------------------------------------------
# AC-01 .. AC-03 — 选择权威与所有权冲突（基线复现）
# ---------------------------------------------------------------------------

def test_ac_01_registered_but_unselected_snapshot_cannot_be_consumed(env):
    """AC-01: 仅登记（未选择）的快照不得经注册表驱动读取器解析。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    assert registry.active_snapshot_id is None

    reader = ShadowRuntimeReader(registry=registry)
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_SELECTED" in result.rejection_codes
    assert result.candidate_canonical_id == ""


def test_ac_02_non_selected_snapshot_cannot_be_consumed_by_object_or_id(env):
    """AC-02: 已选择 A 后，B 不得通过对象或 ID 入口被消费。"""
    snapshot_b, _authorization, _evidence = _second_snapshot(env)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(snapshot_b)
    registry.select(env.snapshot.snapshot_id)

    reader = ShadowRuntimeReader(registry=registry)
    by_object = reader.read(
        observation=env.other_observation,
        snapshot=snapshot_b,
        read_context=_ctx(env),
    )
    assert by_object.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_SELECTED" in by_object.rejection_codes

    by_id = reader.read(
        observation=env.other_observation,
        snapshot_id=snapshot_b.snapshot_id,
        read_context=_ctx(env),
    )
    assert by_id.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_SELECTED" in by_id.rejection_codes

    # 显式选择 B 之后才可消费。
    registry.select(snapshot_b.snapshot_id)
    selected = reader.read(
        observation=env.other_observation,
        snapshot=snapshot_b,
        read_context=_ctx(env),
    )
    # 读取 B 需要 B 的授权证据；夹具上下文只含 A 的证据，因此这里必须是
    # 「授权缺失」而不是「未选择」。
    assert "SNAPSHOT_AUTHORIZATION_MISSING" in selected.rejection_codes


def test_ac_03_all_competing_owners_reject_in_both_orders(env):
    """AC-03: 同一源键两个所有者在两种顺序下都必须 OWNERSHIP_CONFLICT。"""
    key = env.authorization.scoped_source_identity_key
    forward = ((key, CANONICAL_ID), (key, ALT_CANONICAL_ID))
    reverse = ((key, ALT_CANONICAL_ID), (key, CANONICAL_ID))

    reader = ShadowRuntimeReader()
    for bindings in (forward, reverse):
        result = reader.read(
            observation=env.observation,
            snapshot=env.snapshot,
            read_context=_ctx(env, known_source_bindings=bindings),
        )
        assert result.resolution_status == "REJECTED_SHADOW", bindings
        assert "OWNERSHIP_CONFLICT" in result.rejection_codes, bindings
        assert result.candidate_canonical_id == ""


# ---------------------------------------------------------------------------
# AC-04 / AC-05 — 停用反转与回滚提交边界（基线复现）
# ---------------------------------------------------------------------------

def test_ac_04_bare_reenable_cannot_restore_consumption(env):
    """AC-04: 停用后裸 ``re_enable`` 不得恢复消费。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=env.audit_store
    )
    simulator.disable_shadow_runtime(at=WITHIN_WINDOW, reason="emergency")

    with pytest.raises(Exception):
        reader.re_enable(at=WITHIN_WINDOW, reason="bare re-enable")  # type: ignore[call-arg]

    after = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert after.resolution_status != "RESOLVED_SHADOW"
    assert "READER_DISABLED" in after.rejection_codes

    # 重新选择也无法绕过：停用边界只能由**已校验证据**的再启用解除。
    registry.select(env.snapshot.snapshot_id)
    still = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert still.resolution_status == "REJECTED_SHADOW"
    assert "READER_DISABLED" in still.rejection_codes


def test_ac_05_completed_audit_failure_cannot_switch_effective_snapshot(env):
    """AC-05: 完成事件审计失败不得留下未记录的有效切换。"""
    snapshot_b, _authorization, _evidence = _second_snapshot(env)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(snapshot_b)
    registry.select(snapshot_b.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    failing = _FailingAuditStore(
        real=env.audit_store, fail_event_types=("SHADOW_ROLLBACK_COMPLETED",)
    )
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=failing
    )

    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status != "ROLLBACK_COMPLETED"
    assert report.audit_committed is False
    assert "ROLLBACK_AUDIT_PERSISTENCE_FAILED" in report.rejection_codes
    # 有效指针绝不切换到一个未持久提交的目标。
    assert registry.active_snapshot_id is None
    # fail closed：不得残留部分启用的运行期。
    assert reader.enabled is False
    assert "READER_DISABLED" in reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    ).rejection_codes


# ---------------------------------------------------------------------------
# AC-06 / AC-07 — 追加前完整性（基线复现）
# ---------------------------------------------------------------------------

def _seed(store: ShadowActivationAuditStore, count: int = 3) -> None:
    for index in range(count):
        store.append(
            event_type="SHADOW_SNAPSHOT_REGISTERED",
            occurred_at=f"2030-01-{index + 1:02d}T00:00:00+00:00",
            actor="shadow-operator",
            actor_role="shadow-operator",
            activation_id=f"activation-{index}",
            payload={"index": str(index)},
        )


def test_ac_06_append_refuses_structurally_valid_tampering(tmp_path):
    """AC-06: 既有条目被改（JSON 仍合法）后追加必须拒绝。"""
    workspace = tmp_path / "ac06"
    workspace.mkdir()
    store = ShadowActivationAuditStore(str(workspace))
    _seed(store, 3)

    with open(store.path, "r", encoding="utf-8") as handle:
        lines = [line for line in handle.read().split("\n") if line.strip()]
    payload = json.loads(lines[1])
    payload["actor"] = "attacker"
    lines[1] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")
    with open(store.path, "rb") as handle:
        before_bytes = handle.read()

    with pytest.raises(ShadowAuditIntegrityError) as exc:
        store.append(
            event_type="SHADOW_SNAPSHOT_REGISTERED",
            occurred_at="2030-02-01T00:00:00+00:00",
            actor="shadow-operator",
            actor_role="shadow-operator",
        )
    assert "MODIFIED_ENTRY" in exc.value.rejection_codes
    with open(store.path, "rb") as handle:
        assert handle.read() == before_bytes  # 绝不改写损坏历史


def test_ac_07_blank_physical_lines_are_not_silently_ignored(tmp_path):
    """AC-07: 空白物理行不得被静默规范化或忽略。"""
    workspace = tmp_path / "ac07"
    workspace.mkdir()
    store = ShadowActivationAuditStore(str(workspace))
    _seed(store, 3)

    with open(store.path, "r", encoding="utf-8") as handle:
        lines = [line for line in handle.read().split("\n") if line.strip()]
    lines.insert(1, "")
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")
    with open(store.path, "rb") as handle:
        before_bytes = handle.read()

    with pytest.raises(ShadowAuditIntegrityError) as verify_exc:
        store.verify()
    assert "UNEXPECTED_BLANK_LINE" in verify_exc.value.rejection_codes

    with pytest.raises(ShadowAuditIntegrityError) as append_exc:
        store.append(
            event_type="SHADOW_SNAPSHOT_REGISTERED",
            occurred_at="2030-02-01T00:00:00+00:00",
            actor="shadow-operator",
            actor_role="shadow-operator",
        )
    assert "UNEXPECTED_BLANK_LINE" in append_exc.value.rejection_codes
    with open(store.path, "rb") as handle:
        assert handle.read() == before_bytes


# ---------------------------------------------------------------------------
# AC-08 — 句柄有效性与授权有效性（语义限制）
# ---------------------------------------------------------------------------

def test_ac_08_generation_validity_is_not_authorization_validity(env):
    """AC-08: 代际有效不等于当前授权有效（必须显式区分）。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "RESOLVED_SHADOW"
    assert reader.is_generation_current(result) is True
    # 方法名与文档都必须说明：只校验代际，不重新校验授权。
    assert not hasattr(reader, "is_result_current")
    for forbidden in ("is_authorized", "is_currently_authorized", "revalidate"):
        assert not hasattr(reader, forbidden), forbidden

    # 撤销发生后，旧结果仍在**代际**意义上有效（这是已文档化的限制），
    # 但新的读取必须拒绝：旧结果绝不能被当作当前授权。
    revoked_ctx = _ctx(
        env,
        revocation_registry=((env.authorization.activation_id, REVOCATION_REFERENCE),),
    )
    fresh = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=revoked_ctx,
    )
    assert fresh.resolution_status == "REJECTED_SHADOW"
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in fresh.rejection_codes
    assert reader.is_generation_current(result) is True
    assert reader.generation == 0  # 读取本身绝不改变代际


# ---------------------------------------------------------------------------
# GC-01 .. GC-14 — 选择权威、所有权与再启用
# ---------------------------------------------------------------------------

def test_gc_01_registered_but_unselected_cannot_be_consumed(env):
    """GC-01: 仅登记的快照不得被注册表驱动读取器消费。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    result = ShadowRuntimeReader(registry=registry).read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_SELECTED" in result.rejection_codes


def test_gc_02_inactive_snapshot_rejected_while_another_is_selected(env):
    """GC-02: 已登记但非活动的快照在另一快照被选中时不可消费。"""
    snapshot_b, _a, _e = _second_snapshot(env)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(snapshot_b)
    registry.select(snapshot_b.snapshot_id)
    result = ShadowRuntimeReader(registry=registry).read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_SELECTED" in result.rejection_codes


def test_gc_03_valid_selection_enables_conditional_consumption(env):
    """GC-03: 显式选择有效快照后，注册表驱动读取才可条件性消费。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    result = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.resolution_status == "RESOLVED_SHADOW"
    assert result.snapshot_authority == "REGISTRY_SELECTED"
    assert result.is_standalone_inspection is False
    assert result.candidate_canonical_id == CANONICAL_ID


def test_gc_04_standalone_inspection_remains_explicitly_non_production(env):
    """GC-04: 独立（未附加注册表）检视明确标注为非生产 / 非活动权威。"""
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.resolution_status == "RESOLVED_SHADOW"
    assert result.snapshot_authority == "STANDALONE_INSPECTION"
    assert result.is_standalone_inspection is True
    assert result.as_dict()["is_production_authority"] is False
    status = reader.status()
    assert status["snapshot_authority"] == "STANDALONE_INSPECTION"
    assert status["requires_explicit_selection"] is False
    assert status["production_runtime_reader"] == "NOT_ENABLED"


def test_gc_05_incompatible_owners_rejected_in_both_orders(env):
    """GC-05: 两个不兼容所有者按集合判定，与输入顺序无关。"""
    key = env.authorization.scoped_source_identity_key
    reader = ShadowRuntimeReader()
    for bindings in (
        ((key, CANONICAL_ID), (key, ALT_CANONICAL_ID)),
        ((key, ALT_CANONICAL_ID), (key, CANONICAL_ID)),
    ):
        result = reader.read(
            observation=env.observation,
            snapshot=env.snapshot,
            read_context=_ctx(env, known_source_bindings=bindings),
        )
        assert result.resolution_status == "REJECTED_SHADOW"
        assert "OWNERSHIP_CONFLICT" in result.rejection_codes


def test_gc_06_duplicate_identical_owner_entries_are_not_conflicts(env):
    """GC-06: 完全相同的重复所有权条目不算冲突。"""
    key = env.authorization.scoped_source_identity_key
    result = ShadowRuntimeReader().read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(
            env,
            known_source_bindings=(
                (key, CANONICAL_ID),
                (key, CANONICAL_ID),
                (key, CANONICAL_ID),
            ),
        ),
    )
    assert result.resolution_status == "RESOLVED_SHADOW"
    assert "OWNERSHIP_CONFLICT" not in result.rejection_codes


def test_gc_07_rollback_uses_complete_ownership_rules(env, tmp_path):
    """GC-07: 回滚与读取器使用相同的完整所有权冲突语义。"""
    key = env.authorization.scoped_source_identity_key
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(
        registry=registry,
        reader=reader,
        audit_store=_fresh_audit_store(tmp_path, "gc07"),
    )

    for bindings in (
        ((key, CANONICAL_ID), (key, ALT_CANONICAL_ID)),
        ((key, ALT_CANONICAL_ID), (key, CANONICAL_ID)),
    ):
        report = simulator.rollback_shadow_snapshot(
            target_snapshot_id=env.snapshot.snapshot_id,
            read_context=_ctx(env, known_source_bindings=bindings),
            at=WITHIN_WINDOW,
        )
        assert report.status == "ROLLBACK_REJECTED"
        assert "ROLLBACK_OWNERSHIP_CONFLICT" in report.rejection_codes
        assert registry.active_snapshot_id == env.snapshot.snapshot_id


def test_gc_08_emergency_disable_requires_no_approval(env):
    """GC-08: 紧急停用无需任何授权 / 所有者签名。"""
    registry = ShadowSnapshotRegistry()
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(registry=registry, reader=reader)
    report = simulator.disable_shadow_runtime(at=WITHIN_WINDOW)
    assert report["disabled"] is True
    assert report["requires_activation_authorization"] is False
    assert report["requires_owner_signing_key"] is False
    assert reader.enabled is False


def test_gc_09_bare_reenable_cannot_restore_consumption(env):
    """GC-09: 无已验证证据的再启用保持非消费状态。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    reader.disable(at=WITHIN_WINDOW, reason="emergency")
    with pytest.raises(TypeError):
        reader.re_enable(at=WITHIN_WINDOW, reason="bare")  # type: ignore[call-arg]
    assert reader.enabled is False
    assert "READER_DISABLED" in reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).rejection_codes


def test_gc_10_authorized_reenable_restores_consumption(env):
    """GC-10: 显式选择 + 已验证证据的再启用可以恢复条件性消费。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    before = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert before.resolution_status == "RESOLVED_SHADOW"

    reader.disable(at=WITHIN_WINDOW, reason="emergency")
    assert reader.generation == 1
    registry.select(env.snapshot.snapshot_id)
    report = reader.re_enable(
        at=WITHIN_WINDOW,
        read_context=_ctx(env),
        evidence=_evidence(env),
        reason="authorized shadow re-enablement",
    )
    assert report["enabled"] is True
    assert report["reader_generation"] == 2
    assert report["requires_activation_authorization"] is True
    assert report["grants_production_authorization"] is False
    assert reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).resolution_status == "RESOLVED_SHADOW"


def _disabled_reader_with_selection(env):
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    reader.disable(at=WITHIN_WINDOW, reason="emergency")
    registry.select(env.snapshot.snapshot_id)
    return registry, reader


def test_gc_11_expired_activation_cannot_reenable(env):
    """GC-11: 已过期授权不能用于再启用。"""
    _registry, reader = _disabled_reader_with_selection(env)
    with pytest.raises(ShadowReenablementError) as exc:
        reader.re_enable(
            at=AFTER_EXPIRY,
            read_context=_ctx(env, current_time=AFTER_EXPIRY),
            evidence=_evidence(env),
        )
    assert "RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE" in exc.value.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in exc.value.rejection_codes
    assert reader.enabled is False
    assert reader.generation == 1


def test_gc_12_revoked_activation_cannot_reenable(env):
    """GC-12: 已撤销授权不能用于再启用。"""
    _registry, reader = _disabled_reader_with_selection(env)
    with pytest.raises(ShadowReenablementError) as exc:
        reader.re_enable(
            at=WITHIN_WINDOW,
            read_context=_ctx(
                env,
                revocation_registry=(
                    (env.authorization.activation_id, REVOCATION_REFERENCE),
                ),
            ),
            evidence=_evidence(env),
        )
    assert "RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE" in exc.value.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in exc.value.rejection_codes
    assert reader.enabled is False


def test_gc_13_stale_baseline_cannot_reenable(env):
    """GC-13: 基线陈旧的授权不能用于再启用。"""
    _registry, reader = _disabled_reader_with_selection(env)
    with pytest.raises(ShadowReenablementError) as exc:
        reader.re_enable(
            at=WITHIN_WINDOW,
            read_context=_ctx(env, current_baseline_fingerprint="baseline-B"),
            evidence=_evidence(env),
        )
    assert "RE_ENABLE_BASELINE_STALE" in exc.value.rejection_codes
    assert reader.enabled is False


def test_gc_14_old_generation_handles_stay_invalid_after_reenable(env):
    """GC-14: 再启用后旧代际句柄仍然无效。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    old_handle = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    reader.disable(at=WITHIN_WINDOW, reason="emergency")
    registry.select(env.snapshot.snapshot_id)
    reader.re_enable(
        at=WITHIN_WINDOW, read_context=_ctx(env), evidence=_evidence(env)
    )
    assert reader.is_generation_current(old_handle) is False
    new_handle = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert new_handle.reader_generation == 2
    assert reader.is_generation_current(new_handle) is True


# ---------------------------------------------------------------------------
# GC-15 .. GC-16 — 回滚事务安全
# ---------------------------------------------------------------------------

def _fresh_audit_store(tmp_path, name: str) -> ShadowActivationAuditStore:
    workspace = tmp_path / name
    workspace.mkdir()
    return ShadowActivationAuditStore(str(workspace), dataset_identity=DATASET)


def test_gc_15_completed_event_audit_failure_leaves_no_effective_switch(
    env, tmp_path
):
    """GC-15: 完成事件写入失败（含部分写入）不得留下有效切换。"""
    snapshot_b, _a, _e = _second_snapshot(env)
    for mode in ("raise", "partial"):
        registry = ShadowSnapshotRegistry()
        registry.register(env.snapshot)
        registry.register(snapshot_b)
        registry.select(snapshot_b.snapshot_id)
        reader = ShadowRuntimeReader(registry=registry)
        store = _fresh_audit_store(tmp_path, f"gc15_{mode}")
        failing = _FailingAuditStore(
            real=store,
            fail_event_types=("SHADOW_ROLLBACK_COMPLETED",),
            partial_write=(mode == "partial"),
        )
        simulator = ShadowActivationSimulator(
            registry=registry, reader=reader, audit_store=failing
        )
        report = simulator.rollback_shadow_snapshot(
            target_snapshot_id=env.snapshot.snapshot_id,
            read_context=_ctx(env),
            at=WITHIN_WINDOW,
        )
        assert report.status == "ROLLBACK_AUDIT_FAILED", mode
        assert report.audit_committed is False, mode
        assert report.reader_disabled is True, mode
        assert registry.active_snapshot_id is None, mode
        assert reader.enabled is False, mode
        if mode == "partial":
            # 部分写入的尾部必须让后续追加 fail closed，绝不静默修复。
            with pytest.raises(ShadowAuditIntegrityError):
                store.append(
                    event_type="SHADOW_SNAPSHOT_REGISTERED",
                    occurred_at=WITHIN_WINDOW,
                    actor="shadow-operator",
                    actor_role="shadow-operator",
                )


def test_gc_16_requested_event_audit_failure_cannot_modify_state(env, tmp_path):
    """GC-16: 请求事件写入失败绝不改变有效状态。"""
    snapshot_b, _a, _e = _second_snapshot(env)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(snapshot_b)
    registry.select(snapshot_b.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    store = _fresh_audit_store(tmp_path, "gc16")
    failing = _FailingAuditStore(
        real=store, fail_event_types=("SHADOW_ROLLBACK_REQUESTED",)
    )
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=failing
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.audit_committed is False
    assert report.audit_event_types == ()
    assert "ROLLBACK_AUDIT_PERSISTENCE_FAILED" in report.rejection_codes
    # 有效状态与读取器都保持不变。
    assert registry.active_snapshot_id == snapshot_b.snapshot_id
    assert reader.enabled is True
    assert reader.read(
        observation=env.other_observation,
        snapshot=snapshot_b,
        read_context=_ctx(env),
    ).resolution_status == "REJECTED_SHADOW"  # 仅有 A 的证据


def test_gc_16b_validation_failure_after_request_logging_changes_nothing(env, tmp_path):
    """GC-16b: 请求已记录后目标校验失败，有效状态不得改变。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    store = _fresh_audit_store(tmp_path, "gc16b")
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, current_baseline_fingerprint="baseline-B"),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_REJECTED"
    assert "ROLLBACK_BASELINE_STALE" in report.rejection_codes
    assert registry.active_snapshot_id == env.snapshot.snapshot_id
    assert reader.enabled is True
    assert report.audit_committed is True
    assert store.event_types() == (
        "SHADOW_ROLLBACK_REQUESTED",
        "SHADOW_ROLLBACK_REJECTED",
    )


# ---------------------------------------------------------------------------
# GC-17 .. GC-23 — 审计追加完整性
# ---------------------------------------------------------------------------

def _lines_of(store: ShadowActivationAuditStore) -> list[str]:
    with open(store.path, "r", encoding="utf-8") as handle:
        return [line for line in handle.read().split("\n") if line]


def _rewrite(store: ShadowActivationAuditStore, lines) -> None:
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(line + "\n")


def _append_new(store: ShadowActivationAuditStore):
    return store.append(
        event_type="SHADOW_SNAPSHOT_REGISTERED",
        occurred_at="2030-02-01T00:00:00+00:00",
        actor="shadow-operator",
        actor_role="shadow-operator",
    )


def test_gc_17_modified_entry_with_valid_json_is_refused(tmp_path):
    """GC-17: 结构合法但被修改的既有条目使追加拒绝。"""
    store = _fresh_audit_store(tmp_path, "gc17")
    _seed(store, 3)
    lines = _lines_of(store)
    payload = json.loads(lines[2])
    payload["sequence"] = 0
    lines[2] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _rewrite(store, lines)
    with pytest.raises(ShadowAuditIntegrityError):
        _append_new(store)


def test_gc_18_broken_previous_hash_is_refused(tmp_path):
    """GC-18: 前驱哈希断裂使追加拒绝。"""
    store = _fresh_audit_store(tmp_path, "gc18")
    _seed(store, 3)
    lines = _lines_of(store)
    payload = json.loads(lines[-1])
    payload["previous_entry_hash"] = "f" * 64
    payload["entry_hash"] = _entry_hash_of(payload)
    lines[-1] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _rewrite(store, lines)
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        _append_new(store)
    assert "BROKEN_PREVIOUS_HASH" in exc.value.rejection_codes


def _entry_hash_of(payload):  # noqa: N802 (test helper)
    from utils.shadow_activation_audit_store import _entry_hash

    return _entry_hash(payload)


def test_gc_19_sequence_gap_is_refused(tmp_path):
    """GC-19: 序号缺口使追加拒绝。"""
    store = _fresh_audit_store(tmp_path, "gc19")
    _seed(store, 3)
    lines = _lines_of(store)
    payload = json.loads(lines[2])
    payload["sequence"] = 9
    payload["entry_hash"] = _entry_hash_of(payload)
    lines[2] = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _rewrite(store, lines)
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        _append_new(store)
    assert "SEQUENCE_GAP" in exc.value.rejection_codes


def test_gc_20_duplicate_event_id_is_refused(tmp_path):
    """GC-20: 重复事件 ID 使追加拒绝。"""
    store = _fresh_audit_store(tmp_path, "gc20")
    _seed(store, 2)
    records = store.records()
    replayed = records[0].as_dict()
    replayed["sequence"] = 2
    replayed["previous_entry_hash"] = records[1].entry_hash
    replayed["entry_hash"] = _entry_hash_of(replayed)
    lines = _lines_of(store)
    lines.append(json.dumps(replayed, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    _rewrite(store, lines)
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        _append_new(store)
    assert "DUPLICATE_EVENT_ID" in exc.value.rejection_codes


def test_gc_21_blank_line_is_refused(tmp_path):
    """GC-21: 空白物理行使追加拒绝且不修改字节。"""
    store = _fresh_audit_store(tmp_path, "gc21")
    _seed(store, 3)
    lines = _lines_of(store)
    lines.insert(2, "")
    _rewrite(store, lines)
    with open(store.path, "rb") as handle:
        before = handle.read()
    with pytest.raises(ShadowAuditIntegrityError) as exc:
        _append_new(store)
    assert "UNEXPECTED_BLANK_LINE" in exc.value.rejection_codes
    with open(store.path, "rb") as handle:
        assert handle.read() == before


def test_gc_22_torn_or_unterminated_records_are_refused_without_writes(tmp_path):
    """GC-22: 撕裂 / 未终止尾记录使追加拒绝且绝不改写文件。"""
    for mode in ("torn", "unterminated"):
        store = _fresh_audit_store(tmp_path, f"gc22_{mode}")
        _seed(store, 2)
        if mode == "torn":
            with open(store.path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write('{"sequence": 2, "event_type": "SHADOW_SNAPSHOT_REG')
        else:
            with open(store.path, "r", encoding="utf-8") as handle:
                content = handle.read()
            with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content.rstrip("\n"))
        with open(store.path, "rb") as handle:
            before = handle.read()
        with pytest.raises(ShadowAuditIntegrityError) as exc:
            _append_new(store)
        assert set(exc.value.rejection_codes) & {
            "TORN_FINAL_RECORD",
            "UNTERMINATED_FINAL_RECORD",
        }, mode
        with open(store.path, "rb") as handle:
            assert handle.read() == before, mode


def test_gc_22b_fsync_error_is_surfaced_not_silently_accepted(tmp_path, monkeypatch):
    """GC-22b: fsync 失败必须向调用方暴露，绝不静默声称追加成功。"""
    store = _fresh_audit_store(tmp_path, "gc22b")
    _seed(store, 2)

    def _failing_fsync(fd):  # noqa: ANN001
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", _failing_fsync)
    with pytest.raises(OSError):
        _append_new(store)
    monkeypatch.undo()

    # 记录可能已完整写入，但历史必须保持结构自洽；绝不静默截断。
    report = store.verify()
    assert report["integrity"] == "OK"
    assert [record.sequence for record in store.records()] == list(
        range(len(store.records()))
    )


def test_gc_23_read_only_verification_and_recovery_still_work(tmp_path):
    """GC-23: 只读校验与恢复仍可用，且绝不自动恢复活动状态。"""
    store = _fresh_audit_store(tmp_path, "gc23")
    _seed(store, 4)
    report = store.verify()
    assert report["integrity"] == "OK"
    assert report["externally_authenticated"] is False
    recovery = store.recover()
    assert recovery["history_reconstructed"] is True
    assert recovery["active_shadow_state"] == "NOT_RESTORED"
    assert recovery["consumable"] is False
    assert recovery["auto_activation_performed"] is False


# ---------------------------------------------------------------------------
# GC-25 .. GC-26 — 句柄语义与确定性
# ---------------------------------------------------------------------------

def test_gc_25_generation_validity_is_documented_as_generation_only(env):
    """GC-25: 代际有效性绝不冒充当前授权有效性。"""
    assert SHADOW_READER_VERSION == "1.1.0"
    assert len(REENABLE_REJECTION_CODE_SET) == len(REENABLE_REJECTION_CODES)
    doc = ShadowRuntimeReader.is_generation_current.__doc__ or ""
    assert "仅代际" in doc
    assert "不是" in doc and "授权" in doc
    result = ShadowRuntimeReader().read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.as_dict()["snapshot_authority"] == "STANDALONE_INSPECTION"
    assert result.as_dict()["is_production_authority"] is False


def test_gc_26_reader_results_are_deterministic(env):
    """GC-26: 相同输入产生完全相同的读取结果。"""
    reader = ShadowRuntimeReader()
    first = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    second = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert first == second
    assert first.to_json() == second.to_json()


# ---------------------------------------------------------------------------
# RC-01 .. RC-03 — 审计提交边界（基线复现）
# ---------------------------------------------------------------------------

@dataclass
class _SilentAuditStore:
    """``append()`` 静默返回 ``None`` 且不记录事件的审计对象。"""

    real: ShadowActivationAuditStore

    @property
    def path(self) -> str:
        return self.real.path

    def append(self, **kwargs):
        return None

    def records(self):
        return self.real.records()

    def verify(self) -> dict:
        return self.real.verify()

    def chain_tip(self) -> str:
        return self.real.chain_tip()

    def store_identity(self) -> str:
        return self.real.store_identity()

    def receipt_for(self, record):
        return self.real.receipt_for(record)


@dataclass
class _AckLostAuditStore:
    """完成事件已写入，但调用方收不到可靠成功确认。"""

    real: ShadowActivationAuditStore
    fail_event_type: str = "SHADOW_ROLLBACK_COMPLETED"

    @property
    def path(self) -> str:
        return self.real.path

    def append(self, **kwargs):
        record = self.real.append(**kwargs)
        if kwargs.get("event_type") == self.fail_event_type:
            raise OSError("injected acknowledgment loss after persistence")
        return record

    def records(self):
        return self.real.records()

    def verify(self) -> dict:
        return self.real.verify()

    def chain_tip(self) -> str:
        return self.real.chain_tip()

    def store_identity(self) -> str:
        return self.real.store_identity()

    def receipt_for(self, record):
        return self.real.receipt_for(record)


def _two_snapshot_rollback_environment(env, store):
    snapshot_b, _authorization, _evidence = _second_snapshot(env)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(snapshot_b)
    registry.select(snapshot_b.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    return registry, reader, simulator, snapshot_b


def test_rc_01_missing_audit_store_never_reports_committed_rollback(env):
    """RC-01: 没有持久审计存储时绝不报告已提交的回滚。"""
    registry, reader, simulator, snapshot_b = _two_snapshot_rollback_environment(
        env, None
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status != "ROLLBACK_COMPLETED"
    assert report.audit_committed is False
    assert report.audit_store_attached is False
    assert report.commit_outcome == "NOT_ATTEMPTED"
    assert "ROLLBACK_AUDIT_STORE_MISSING" in report.rejection_codes
    # 有效状态与读取器均未被改动。
    assert registry.active_snapshot_id == snapshot_b.snapshot_id
    assert reader.enabled is True


def test_rc_02_silent_append_is_not_durable_commitment(env, tmp_path):
    """RC-02: ``append()`` 返回 ``None`` 不得被当作持久提交。"""
    store = _fresh_audit_store(tmp_path, "rc02")
    registry, reader, simulator, snapshot_b = _two_snapshot_rollback_environment(
        env, _SilentAuditStore(real=store)
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.audit_committed is False
    assert report.commit_outcome in {"NOT_COMMITTED", "UNCERTAIN"}
    assert report.audit_store_attached is True
    assert "ROLLBACK_AUDIT_COMMIT_UNVERIFIED" in report.rejection_codes or (
        "ROLLBACK_AUDIT_PERSISTENCE_FAILED" in report.rejection_codes
    )
    # 请求事件即未能持久提交：有效状态必须保持原样（不得出现新快照）。
    assert registry.active_snapshot_id == snapshot_b.snapshot_id
    assert reader.enabled is True
    assert store.records() == ()


def test_rc_03_lost_acknowledgment_is_reported_as_uncertain(env, tmp_path):
    """RC-03: 事件已写入但确认丢失时必须报告不确定提交，而非确定失败。"""
    store = _fresh_audit_store(tmp_path, "rc03")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, _AckLostAuditStore(real=store)
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.audit_committed is False
    assert report.commit_outcome == "UNCERTAIN"
    assert report.completion_event_observed is True
    assert report.pointer_applied is False
    assert report.discrepancy
    assert "ROLLBACK_AUDIT_COMMIT_UNVERIFIED" in report.rejection_codes
    assert registry.active_snapshot_id is None
    assert reader.enabled is False
    # 审计字节保留且链仍然有效；最后一条正是完成事件。
    assert store.verify()["integrity"] == "OK"
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_COMPLETED"


# ---------------------------------------------------------------------------
# RC-04 .. RC-15 — 收据契约、失败注入与既有修正保持
# ---------------------------------------------------------------------------

@dataclass
class _WrongReceiptStore:
    """``append()`` 正常持久化，但返回无关 / 无效的提交收据。"""

    real: ShadowActivationAuditStore
    mode: str = "wrong_receipt"

    @property
    def path(self) -> str:
        return self.real.path

    def append(self, **kwargs):
        return self.real.append(**kwargs)

    def records(self):
        return self.real.records()

    def verify(self) -> dict:
        return self.real.verify()

    def chain_tip(self) -> str:
        return self.real.chain_tip()

    def store_identity(self) -> str:
        return self.real.store_identity()

    def receipt_for(self, record):
        if record.event_type != "SHADOW_ROLLBACK_COMPLETED":
            return self.real.receipt_for(record)
        if self.mode == "none":
            return None
        return ShadowAuditCommitReceipt(
            store_identity=self.real.store_identity(),
            event_type="SHADOW_ROLLBACK_REQUESTED",
            event_id="unrelated-event",
            sequence=max(record.sequence - 1, 0),
            entry_hash="0" * 64,
            previous_entry_hash="0" * 64,
            persisted=True,
        )


def test_rc_04_invalid_receipt_is_rejected(env, tmp_path):
    """RC-04: 无效或无关收据绝不被接受为持久提交。"""
    for mode in ("wrong_receipt", "none"):
        store = _fresh_audit_store(tmp_path, f"rc04_{mode}")
        registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
            env, _WrongReceiptStore(real=store, mode=mode)
        )
        report = simulator.rollback_shadow_snapshot(
            target_snapshot_id=env.snapshot.snapshot_id,
            read_context=_ctx(env),
            at=WITHIN_WINDOW,
        )
        assert report.status == "ROLLBACK_AUDIT_FAILED", mode
        assert report.audit_committed is False, mode
        assert report.commit_outcome == "UNCERTAIN", mode
        assert "ROLLBACK_AUDIT_COMMIT_UNVERIFIED" in report.rejection_codes, mode
        assert registry.active_snapshot_id is None, mode
        assert reader.enabled is False, mode


def test_rc_05_valid_receipt_allows_compatible_rollback(env, tmp_path):
    """RC-05: 已验证持久收据允许兼容回滚。"""
    store = _fresh_audit_store(tmp_path, "rc05")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, store
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_COMPLETED"
    assert report.audit_committed is True
    assert report.audit_store_attached is True
    assert report.commit_outcome == "COMMITTED"
    assert report.completion_event_observed is True
    assert report.pointer_applied is True
    assert report.receipt_fingerprint
    assert report.audit_event_types == (
        "SHADOW_ROLLBACK_REQUESTED",
        "SHADOW_ROLLBACK_COMPLETED",
    )
    assert registry.active_snapshot_id == env.snapshot.snapshot_id
    assert reader.enabled is True
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_COMPLETED"
    assert store.verify()["integrity"] == "OK"


def test_rc_06_requested_write_failure_keeps_effective_state(env, tmp_path):
    """RC-06: 请求事件写入失败不改变有效状态。"""
    store = _fresh_audit_store(tmp_path, "rc06")
    registry, reader, simulator, snapshot_b = _two_snapshot_rollback_environment(
        env,
        _FailingAuditStore(
            real=store, fail_event_types=("SHADOW_ROLLBACK_REQUESTED",)
        ),
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.commit_outcome == "NOT_COMMITTED"
    assert "ROLLBACK_AUDIT_PERSISTENCE_FAILED" in report.rejection_codes
    assert registry.active_snapshot_id == snapshot_b.snapshot_id
    assert reader.enabled is True


def test_rc_07_completed_write_failure_fails_closed(env, tmp_path):
    """RC-07: 完成事件写入失败必须 fail closed 且不切换指针。"""
    store = _fresh_audit_store(tmp_path, "rc07")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env,
        _FailingAuditStore(
            real=store, fail_event_types=("SHADOW_ROLLBACK_COMPLETED",)
        ),
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.audit_committed is False
    assert report.commit_outcome == "NOT_COMMITTED"
    assert report.completion_event_observed is False
    assert report.pointer_applied is False
    assert report.reader_disabled is True
    assert registry.active_snapshot_id is None
    assert reader.enabled is False
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_REQUESTED"


def test_rc_08_partially_written_final_record_is_not_repaired(env, tmp_path):
    """RC-08: 部分写入的尾记录被拒绝，且绝不修复。"""
    store = _fresh_audit_store(tmp_path, "rc08")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env,
        _FailingAuditStore(
            real=store,
            fail_event_types=("SHADOW_ROLLBACK_COMPLETED",),
            partial_write=True,
        ),
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.commit_outcome == "UNCERTAIN"
    assert report.reader_disabled is True
    assert registry.active_snapshot_id is None
    with open(store.path, "rb") as handle:
        damaged_bytes = handle.read()
    with pytest.raises(ShadowAuditIntegrityError):
        store.verify()
    with pytest.raises(ShadowAuditIntegrityError):
        store.append(
            event_type="SHADOW_SNAPSHOT_REGISTERED",
            occurred_at=WITHIN_WINDOW,
            actor="shadow-operator",
            actor_role="shadow-operator",
        )
    with open(store.path, "rb") as handle:
        assert handle.read() == damaged_bytes  # 绝不截断或重写


def test_rc_09_uncertain_fsync_outcome_is_explicit_and_non_consumable(
    env, tmp_path, monkeypatch
):
    """RC-09: fsync 失败后的不确定结果必须显式标注且不可消费。"""
    store = _fresh_audit_store(tmp_path, "rc09")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, store
    )
    real_fsync = os.fsync
    calls = {"count": 0}

    def _flaky_fsync(fd):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] >= 2:
            raise OSError("injected fsync failure after bytes reached storage")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _flaky_fsync)
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    monkeypatch.undo()

    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert report.audit_committed is False
    assert report.commit_outcome == "UNCERTAIN"
    assert report.completion_event_observed is True
    assert report.pointer_applied is False
    assert report.discrepancy
    assert "ROLLBACK_AUDIT_COMMIT_UNVERIFIED" in report.rejection_codes
    assert registry.active_snapshot_id is None
    assert reader.enabled is False
    # 字节保留、链自洽；事件存在但不构成确定持久提交。
    assert store.verify()["integrity"] == "OK"
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_COMPLETED"


def test_rc_10_historical_audit_records_remain_intact(env, tmp_path):
    """RC-10: 失败的提交绝不改写既有历史字节。"""
    store = _fresh_audit_store(tmp_path, "rc10")
    store.append(
        event_type="SHADOW_SNAPSHOT_REGISTERED",
        occurred_at=WITHIN_WINDOW,
        actor="shadow-operator",
        actor_role="shadow-operator",
        payload={"marker": "history"},
    )
    with open(store.path, "rb") as handle:
        before_bytes = handle.read()

    registry, _reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, _FailingAuditStore(real=store, fail_event_types=("SHADOW_ROLLBACK_REQUESTED",))
    )
    simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    with open(store.path, "rb") as handle:
        after_bytes = handle.read()
    assert after_bytes == before_bytes
    assert store.records()[0].payload == (("marker", "history"),)


def test_rc_11_restart_recovery_cannot_activate_a_snapshot(env, tmp_path):
    """RC-11: 重启恢复绝不自动激活任何影子快照。"""
    store = _fresh_audit_store(tmp_path, "rc11")
    registry, _reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, _AckLostAuditStore(real=store)
    )
    simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    recovery = store.recover()
    assert recovery["history_reconstructed"] is True
    assert recovery["consumable"] is False
    assert recovery["active_shadow_state"] == "NOT_RESTORED"
    assert recovery["auto_activation_performed"] is False

    reopened_registry = ShadowSnapshotRegistry()
    reopened_registry.register(env.snapshot)
    assert reopened_registry.active_snapshot_id is None
    assert ShadowRuntimeReader(registry=reopened_registry).read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).resolution_status == "REJECTED_SHADOW"


def test_rc_12_selection_and_ownership_corrections_remain_valid(env):
    """RC-12: 既有选择权威与完整所有权冲突修正保持有效。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    assert "SNAPSHOT_NOT_SELECTED" in ShadowRuntimeReader(registry=registry).read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).rejection_codes

    key = env.authorization.scoped_source_identity_key
    for bindings in (
        ((key, CANONICAL_ID), (key, ALT_CANONICAL_ID)),
        ((key, ALT_CANONICAL_ID), (key, CANONICAL_ID)),
    ):
        result = ShadowRuntimeReader().read(
            observation=env.observation,
            snapshot=env.snapshot,
            read_context=_ctx(env, known_source_bindings=bindings),
        )
        assert "OWNERSHIP_CONFLICT" in result.rejection_codes


def test_rc_13_guarded_reenablement_remains_valid(env, tmp_path):
    """RC-13: 失败回滚后的再启用仍必须经过显式选择 + 已验证证据。"""
    store = _fresh_audit_store(tmp_path, "rc13")
    registry, reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env,
        _FailingAuditStore(
            real=store, fail_event_types=("SHADOW_ROLLBACK_COMPLETED",)
        ),
    )
    simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert reader.enabled is False
    with pytest.raises(TypeError):
        reader.re_enable(at=WITHIN_WINDOW, reason="bare")  # type: ignore[call-arg]

    registry.select(env.snapshot.snapshot_id)
    report = reader.re_enable(
        at=WITHIN_WINDOW, read_context=_ctx(env), evidence=_evidence(env)
    )
    assert report["enabled"] is True
    assert reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).resolution_status == "RESOLVED_SHADOW"


def test_rc_14_production_modules_do_not_import_the_shadow_prototype():
    """RC-14: 生产运行期模块绝不导入影子激活原型。"""
    for name in ("scraper.py", "daily_loader.py", "ingestion_identity.py"):
        with open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in (
            "shadow_activation_simulation",
            "shadow_runtime_reader",
            "shadow_activation_audit_store",
        ):
            assert forbidden not in source, (name, forbidden)
    with open(os.path.join(REPO_ROOT, "app.py"), encoding="utf-8") as handle:
        app_source = handle.read()
    for forbidden in (
        "shadow_activation_simulation",
        "shadow_runtime_reader",
        "shadow_activation_audit_store",
    ):
        assert forbidden not in app_source, forbidden


def test_rc_15_seven_stores_remain_byte_identical(isolated_data_dir, env, tmp_path):
    """RC-15: 全部回滚路径之后七个体育数据存储逐字节不变。"""
    from utils.daily_loader import save_json

    save_json("daily_matches.json", {"meta": {}, "matches": []})
    save_json("team_strength.json", {"teams": {}})
    save_json("calibrated.json", {"ids": []})
    save_json("prediction_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("odds_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("settlements.json", {"version": 1, "settlements": {}})
    save_json("evaluation_rows.json", {"version": 1, "rows": {}})

    names = (
        "daily_matches.json",
        "team_strength.json",
        "calibrated.json",
        "prediction_snapshots.json",
        "odds_snapshots.json",
        "settlements.json",
        "evaluation_rows.json",
    )

    def _hashes() -> dict:
        import hashlib

        out = {}
        for name in names:
            with open(os.path.join(str(isolated_data_dir), name), "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
        return out

    before = _hashes()

    # 成功回滚。
    store = _fresh_audit_store(tmp_path, "rc15_ok")
    registry, _reader, simulator, _snapshot_b = _two_snapshot_rollback_environment(
        env, store
    )
    assert simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    ).status == "ROLLBACK_COMPLETED"

    # 缺少审计存储。
    registry2, _reader2, simulator2, _snapshot_b2 = _two_snapshot_rollback_environment(
        env, None
    )
    simulator2.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )

    # 不确定提交。
    store3 = _fresh_audit_store(tmp_path, "rc15_uncertain")
    registry3, _reader3, simulator3, _snapshot_b3 = _two_snapshot_rollback_environment(
        env, _AckLostAuditStore(real=store3)
    )
    simulator3.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )

    assert _hashes() == before


