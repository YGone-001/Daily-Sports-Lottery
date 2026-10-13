"""
影子激活模拟测试套件（SG-12 .. SG-29）
=====================================
覆盖：
- 多快照注册表：独立注册、显式选择 / 切换、幂等、同 ID 不同内容拒绝、未知 ID 拒绝；
- 输入顺序无关性；同键不同所有者冲突 fail closed；
- 切换不改动任何 Canonical 数据；
- 有效期边界与撤销对已发布快照的即时失效；
- 缓存键不得绕过当前撤销证据；
- 不可用 / 畸形时钟 fail closed；
- 紧急停用（无需授权、立即不可消费、历史保留、句柄失效）；
- 确定性回滚（成功 / 撤销 / 过期 / 陈旧基线 / 未知目标拒绝、绝不部分启用）。

全部使用隔离合成夹具，绝不触碰生产数据，绝不激活任何运行期映射。
"""
from __future__ import annotations

import dataclasses
import hashlib
import os

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
    ShadowActivationSnapshotError,
    build_activation_authorization_body,
    build_shadow_activation_snapshot,
    sign_runtime_activation_authorization,
)
from utils.runtime_mapping_eligibility import (
    RuntimeMappingContext,
    caller_asserted_provenance,
    evaluate_runtime_mapping_eligibility,
)
from utils.shadow_activation_simulation import (
    ROLLBACK_REJECTION_CODES,
    SHADOW_SIMULATION_VERSION,
    ShadowActivationSimulator,
    ShadowSnapshotError,
    ShadowSnapshotRegistry,
    shadow_simulation_status,
)
from utils.shadow_runtime_reader import ShadowReadContext, ShadowRuntimeReader
from utils.source_identity import ScopedSourceIdentityKey

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SEVEN_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)

AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"
WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
EXACT_EXPIRY = "2030-02-01T00:00:00+00:00"
EXACT_START = "2030-01-01T00:00:00+00:00"
BEFORE_START = "2029-12-31T23:59:59+00:00"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("shadow_simulation_env")
    return build_synthetic_shadow_fixture(str(workspace))


def _ctx(env, **overrides) -> ShadowReadContext:
    return env.read_context(**overrides)


def _hash_seven_stores(directory) -> dict:
    out = {}
    for name in SEVEN_STORES:
        path = os.path.join(str(directory), name)
        if not os.path.exists(path):
            out[name] = "<absent>"
            continue
        with open(path, "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def _seed_seven_stores(data_dir) -> None:
    from utils.daily_loader import save_json

    save_json("daily_matches.json", {"meta": {}, "matches": []})
    save_json("team_strength.json", {"teams": {}})
    save_json("calibrated.json", {"ids": []})
    save_json("prediction_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("odds_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("settlements.json", {"version": 1, "settlements": {}})
    save_json("evaluation_rows.json", {"version": 1, "rows": {}})


def _second_snapshot(env, tmp_path):
    """
    构建第二个**兼容**快照（不同源身份，同一数据集与基线）。

    刻意通过真实的合成流程（提案 -> 批准 -> 资格 -> 授权 -> 快照）产生，
    而不是手工拼装。
    """
    from utils.runtime_activation_authorization import (
        generate_activation_authorizer_keypair,
    )

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
    verification_context = ActivationVerificationContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
        current_time=WITHIN_WINDOW,
        trusted_public_key_path=env.authorizer_public,
        eligibility_decision=decision,
        proposal=proposal,
    )
    snapshot = build_shadow_activation_snapshot(
        authorizations=[authorization],
        verification_context=verification_context,
        observations=[other_observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    from utils.shadow_runtime_reader import ShadowAuthorizationEvidence

    return snapshot, authorization, ShadowAuthorizationEvidence(
        authorization=authorization, decision=decision, proposal=proposal
    )


# ---------------------------------------------------------------------------
# SG-12 .. SG-18 — 多快照注册表与切换
# ---------------------------------------------------------------------------

def test_sg_12_two_compatible_snapshots_register_independently(env, tmp_path):
    """SG-12: 两个兼容快照可以独立注册。"""
    second_snapshot, _authorization, _evidence = _second_snapshot(env, tmp_path)
    registry = ShadowSnapshotRegistry()

    first_report = registry.register(env.snapshot)
    second_report = registry.register(second_snapshot)

    assert first_report["registered_count"] == 1
    assert second_report["registered_count"] == 2
    assert first_report["activation_performed"] is False
    assert second_report["activation_performed"] is False
    assert registry.get(env.snapshot.snapshot_id) is env.snapshot
    assert registry.get(second_snapshot.snapshot_id) is second_snapshot
    assert registry.active_snapshot_id is None  # 注册绝不激活
    assert len(registry) == 2
    assert set(registry.history()) == {
        env.snapshot.snapshot_id,
        second_snapshot.snapshot_id,
    }


def test_sg_13_snapshot_selection_is_explicit(env, tmp_path):
    """SG-13: 快照选择必须显式（无最新即生效、无插入顺序激活）。"""
    second_snapshot, _a, _e = _second_snapshot(env, tmp_path)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(second_snapshot)

    # 注册之后仍然没有活动快照。
    assert registry.active_snapshot_id is None
    assert registry.active() is None

    report = registry.select(env.snapshot.snapshot_id)
    assert report["previous_snapshot_id"] is None
    assert report["snapshot_id"] == env.snapshot.snapshot_id
    assert report["latest_wins_applied"] is False
    assert report["production_flags_modified"] is False
    assert registry.active_snapshot_id == env.snapshot.snapshot_id

    # 切换到第二个快照必须显式调用。
    switch_report = registry.switch(second_snapshot.snapshot_id)
    assert switch_report["previous_snapshot_id"] == env.snapshot.snapshot_id
    assert switch_report["snapshot_id"] == second_snapshot.snapshot_id
    assert switch_report["switched"] is True
    assert registry.active_snapshot_id == second_snapshot.snapshot_id
    # 未知快照 ID 一律拒绝。
    with pytest.raises(ShadowSnapshotError) as exc:
        registry.select("0" * 64)
    assert "SNAPSHOT_NOT_REGISTERED" in exc.value.rejection_codes


def test_sg_14_input_ordering_does_not_change_resolution(env, tmp_path):
    """SG-14: 输入顺序不改变解析结果。"""
    second_snapshot, authorization, evidence = _second_snapshot(env, tmp_path)
    reader = ShadowRuntimeReader()

    forward = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(
            env,
            evidence=(
                evidence,
                # 夹具自带的证据
                *env.read_context().evidence,
            ),
        ),
    )
    reversed_evidence = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(
            env, evidence=tuple(reversed((evidence, *env.read_context().evidence)))
        ),
    )
    assert forward.resolution_status == "RESOLVED_SHADOW"
    assert reversed_evidence.resolution_status == "RESOLVED_SHADOW"
    assert forward.candidate_canonical_id == reversed_evidence.candidate_canonical_id
    assert forward.decision_fingerprint == reversed_evidence.decision_fingerprint

    # 快照构建也与输入顺序无关。
    context_a = ActivationVerificationContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
        current_time=WITHIN_WINDOW,
        trusted_public_key_path=env.authorizer_public,
        eligibility_decision=env.decision,
        proposal=env.proposal,
    )
    context_b = dataclasses.replace(
        context_a,
        eligibility_decision=evidence.decision,
        proposal=evidence.proposal,
    )
    forward_snapshot = build_shadow_activation_snapshot(
        authorizations=[env.authorization, authorization],
        verification_contexts=[context_a, context_b],
        observations=[env.observation, env.other_observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    reversed_snapshot = build_shadow_activation_snapshot(
        authorizations=[authorization, env.authorization],
        verification_contexts=[context_b, context_a],
        observations=[env.other_observation, env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    assert forward_snapshot.snapshot_fingerprint == reversed_snapshot.snapshot_fingerprint
    assert second_snapshot.snapshot_id  # 已构建


def test_sg_15_same_key_different_owner_conflict_fails_closed(env, tmp_path):
    """SG-15: 同源键不同所有者冲突 fail closed（构建期与读取期）。"""
    # 构建期：同一观测指向不同 Canonical 目标 -> 拒绝发布。
    conflicting_proposal = proposal_from_observations(
        [env.observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=ALT_CANONICAL_ID,
        existing_canonical_owner=ALT_CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=env.snapshot.baseline_fingerprint,
        reason="conflicting target for the same source identity",
        identity_evidence_type="strong_competition_number",
        operator="shadow-operator",
    )
    owner_private = os.path.join(os.path.dirname(env.owner_public), "owner_private.pem")
    conflicting_approval = sign_mapping_approval(
        owner_private,
        build_approval_body(
            proposal=conflicting_proposal,
            source_observation_fingerprint=env.observation.semantic_fingerprint(),
            dataset_identity=DATASET,
            operator="shadow-operator",
            approver="shadow-owner",
            approved_at="2030-01-01T00:00:00+00:00",
            reason="conflicting synthetic approval",
        ),
    )
    conflicting_decision = evaluate_runtime_mapping_eligibility(
        conflicting_proposal,
        conflicting_approval,
        env.observation,
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
            recorded_proposal_id=conflicting_proposal.proposal_id,
            recorded_proposal_fingerprint=conflicting_proposal.fingerprint(),
            recorded_proposal_state="APPROVED",
        ),
        evaluated_at="2030-01-01T00:00:00+00:00",
    )
    conflicting_authorization = sign_runtime_activation_authorization(
        env.authorizer_private,
        build_activation_authorization_body(
            decision=conflicting_decision,
            proposal=conflicting_proposal,
            dataset_identity=DATASET,
            authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
            authorizer="shadow-authorizer",
            operator="shadow-operator",
            authorized_at="2030-01-01T00:00:00+00:00",
            expires_at="2030-02-01T00:00:00+00:00",
            revocation_reference=REVOCATION_REFERENCE,
            reason="conflicting synthetic authorization",
        ),
    )
    with pytest.raises(ShadowActivationSnapshotError) as exc:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization, conflicting_authorization],
            verification_contexts=[
                ActivationVerificationContext(
                    dataset_identity=DATASET,
                    current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
                    current_time=WITHIN_WINDOW,
                    trusted_public_key_path=env.authorizer_public,
                    eligibility_decision=env.decision,
                    proposal=env.proposal,
                ),
                ActivationVerificationContext(
                    dataset_identity=DATASET,
                    current_baseline_fingerprint=env.snapshot.baseline_fingerprint,
                    current_time=WITHIN_WINDOW,
                    trusted_public_key_path=env.authorizer_public,
                    eligibility_decision=conflicting_decision,
                    proposal=conflicting_proposal,
                ),
            ],
            observations=[env.observation],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
        )
    assert "AMBIGUOUS_SHADOW_BINDING" in exc.value.rejection_codes

    # 读取期：手工构造冲突快照 -> REJECTED_SHADOW（绝不降级为 NOT_FOUND）。
    from utils.runtime_activation_authorization import ShadowActivationBinding

    conflicting_binding = ShadowActivationBinding(
        activation_id=conflicting_authorization.activation_id,
        source_identity_key=conflicting_authorization.scoped_source_identity_key,
        candidate_canonical_id=ALT_CANONICAL_ID,
        eligibility_decision_fingerprint=(
            conflicting_authorization.eligibility_decision_fingerprint
        ),
        authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
    )
    manual = dataclasses.replace(
        env.snapshot,
        bindings=tuple(
            sorted(
                env.snapshot.bindings + (conflicting_binding,),
                key=lambda b: (b.source_identity_key, b.candidate_canonical_id),
            )
        ),
    )
    value = manual.content_fingerprint()
    manual = dataclasses.replace(manual, snapshot_id=value, snapshot_fingerprint=value)
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.observation, snapshot=manual, read_context=_ctx(env)
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "OWNERSHIP_CONFLICT" in result.rejection_codes


def test_sg_16_duplicate_identical_snapshot_registration_is_idempotent(env):
    """SG-16: 重复登记相同快照是幂等的。"""
    registry = ShadowSnapshotRegistry()
    first = registry.register(env.snapshot)
    second = registry.register(env.snapshot)

    assert first["already_registered"] is False
    assert second["already_registered"] is True
    assert second["idempotent"] is True
    assert len(registry) == 1
    assert registry.history() == (env.snapshot.snapshot_id,)
    assert registry.active_snapshot_id is None


def test_sg_17_duplicate_snapshot_id_with_different_content_is_rejected(env):
    """SG-17: 同 ID 不同内容一律拒绝。"""
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)

    forged = dataclasses.replace(
        env.snapshot, baseline_fingerprint="baseline-B"
    )
    # 伪造者把 ID 保持为原值，但内容已经不同 -> 指纹不可复算。
    assert forged.content_fingerprint() != forged.snapshot_fingerprint
    with pytest.raises(ShadowSnapshotError) as exc:
        registry.register(forged)
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in exc.value.rejection_codes

    # 伪造者「修正」了指纹但与已登记 ID 冲突 -> 同 ID 不同内容。
    value = forged.content_fingerprint()
    forged = dataclasses.replace(forged, snapshot_fingerprint=value)
    with pytest.raises(ShadowSnapshotError) as exc2:
        registry.register(forged)
    assert set(exc2.value.rejection_codes) & {
        "SHADOW_SNAPSHOT_ID_CONFLICT",
        "SNAPSHOT_FINGERPRINT_MISMATCH",
    }

    # 非快照对象一律拒绝。
    with pytest.raises(ShadowSnapshotError):
        registry.register({"snapshot_id": "x"})  # type: ignore[arg-type]


def test_sg_18_switching_snapshots_does_not_change_canonical_data(
    isolated_data_dir, tmp_path, env
):
    """SG-18: 切换快照不改动任何 Canonical 数据。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_seven_stores(isolated_data_dir)

    second_snapshot, _a, _e = _second_snapshot(env, tmp_path)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.register(second_snapshot)
    registry.select(env.snapshot.snapshot_id)
    registry.switch(second_snapshot.snapshot_id)
    registry.switch(env.snapshot.snapshot_id)

    assert _hash_seven_stores(isolated_data_dir) == before
    assert registry.active_snapshot_id == env.snapshot.snapshot_id


# ---------------------------------------------------------------------------
# SG-19 .. SG-22 — 有效期与撤销
# ---------------------------------------------------------------------------

def test_sg_19_authorization_boundary_is_enforced_at_expiry(env):
    """SG-19: 有效期边界（生效前 / 生效瞬间 / 窗口内 / 到期瞬间 / 到期后）。"""
    reader = ShadowRuntimeReader()

    before = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=BEFORE_START),
    )
    assert before.resolution_status == "REJECTED_SHADOW"
    assert "NOT_YET_VALID" in before.rejection_codes

    at_start = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=EXACT_START),
    )
    assert at_start.resolution_status == "RESOLVED_SHADOW"

    inside = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=WITHIN_WINDOW),
    )
    assert inside.resolution_status == "RESOLVED_SHADOW"

    # 到期**瞬间**即视为过期（now >= expires_at）。
    at_expiry = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=EXACT_EXPIRY),
    )
    assert at_expiry.resolution_status == "REJECTED_SHADOW"
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in at_expiry.rejection_codes

    after = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=AFTER_EXPIRY),
    )
    assert after.resolution_status == "REJECTED_SHADOW"
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in after.rejection_codes

    # 时区归一化：等价 UTC 表示得到同一结论。
    normalized = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time="2030-01-15T08:00:00+08:00"),
    )
    assert normalized.resolution_status == inside.resolution_status

    # 畸形时间戳 fail closed。
    malformed = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time="2030-13-45T99:99:99"),
    )
    assert malformed.resolution_status == "REJECTED_SHADOW"
    assert "CLOCK_UNAVAILABLE" in malformed.rejection_codes


def test_sg_20_newly_revoked_authorization_invalidates_previous_snapshot(env):
    """SG-20: 新发生的撤销使此前可消费的快照立即失效。"""
    reader = ShadowRuntimeReader()
    before = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert before.resolution_status == "RESOLVED_SHADOW"

    after = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(
            env,
            revocation_registry=(
                (env.authorization.activation_id, REVOCATION_REFERENCE),
            ),
        ),
    )
    assert after.resolution_status == "REJECTED_SHADOW"
    assert "AUTHORIZATION_NOT_CONSUMABLE" in after.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in after.rejection_codes
    # 快照指纹没有变化：失效来自**读时**撤销证据。
    assert after.snapshot_fingerprint == before.snapshot_fingerprint


def test_sg_21_stale_cached_read_cannot_bypass_current_revocation(env):
    """SG-21: 陈旧的缓存读取结果不能绕过当前撤销证据。"""
    reader = ShadowRuntimeReader()
    cached = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert cached.resolution_status == "RESOLVED_SHADOW"

    # 撤销发生后，用**同一个**快照对象与同一读取器再次读取。
    revoked_context = _ctx(
        env,
        revocation_registry=((env.authorization.activation_id, REVOCATION_REFERENCE),),
    )
    fresh = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=revoked_context,
    )
    assert fresh.resolution_status == "REJECTED_SHADOW"
    # 缓存键把撤销上下文纳入身份，因此键必然改变。
    assert fresh.cache_key != cached.cache_key
    # 读取器不提供任何「命中缓存即返回」的路径。
    assert not hasattr(reader, "cached_result")
    assert not hasattr(reader, "read_cached")


def test_sg_22_invalid_or_unavailable_trusted_time_fails_closed(env):
    """SG-22: 无效 / 不可用的受控时钟一律 fail closed（绝不回退到系统时钟）。"""
    reader = ShadowRuntimeReader()
    for bad in ("", "   ", "not-a-time", None, 12345, "2030-13-45", "2030-02-30T00:00:00"):
        result = reader.read(
            observation=env.observation,
            snapshot=env.snapshot,
            read_context=_ctx(env, current_time=bad),
        )
        assert result.resolution_status == "REJECTED_SHADOW", bad
        assert "CLOCK_UNAVAILABLE" in result.rejection_codes, bad

    # 合法但仅含日期的 ISO 值按 UTC 零点解释（文档化行为，而非 fail closed）。
    date_only = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time="2030-01-15"),
    )
    assert date_only.resolution_status == "RESOLVED_SHADOW"
    assert date_only.checked_at == "2030-01-15"

    # 模块不导入任何不受控时钟源。
    with open(
        os.path.join(REPO_ROOT, "utils", "shadow_runtime_reader.py"), encoding="utf-8"
    ) as fh:
        source = fh.read()
    assert "datetime.now" not in source
    assert "time.time" not in source


# ---------------------------------------------------------------------------
# SG-23 .. SG-29 — 紧急停用与回滚
# ---------------------------------------------------------------------------

def _simulator(env):
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=env.audit_store
    )
    return registry, reader, simulator


def test_sg_23_emergency_disable_makes_mappings_non_consumable(env):
    """SG-23: 紧急停用立即使影子映射不可消费。"""
    registry, reader, simulator = _simulator(env)
    before = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert before.resolution_status == "RESOLVED_SHADOW"

    report = simulator.disable_shadow_runtime(
        at=WITHIN_WINDOW, actor="shadow-operator", reason="synthetic disable"
    )
    assert report["disabled"] is True
    assert report["handles_invalidated"] is True
    assert report["history_preserved"] is True
    assert report["canonical_writes"] == 0
    assert report["dependent_store_writes"] == 0
    assert report["partial_enablement"] is False

    after = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert after.resolution_status == "REJECTED_SHADOW"
    assert "READER_DISABLED" in after.rejection_codes
    # 此前取得的句柄按代际失效。
    assert reader.is_result_current(before) is False
    # 活动影子指针失效；历史快照保留。
    assert registry.active_snapshot_id is None
    assert env.snapshot.snapshot_id in registry.history()

    # 停用是确定性的。
    second_registry, second_reader, second_simulator = _simulator(env)
    second_report = second_simulator.disable_shadow_runtime(
        at=WITHIN_WINDOW, actor="shadow-operator", reason="synthetic disable"
    )
    assert second_report == report


def test_sg_24_disable_requires_no_activation_signature(env):
    """SG-24: 停用不需要任何激活授权或所有者签名。"""
    _registry, _reader, simulator = _simulator(env)
    report = simulator.disable_shadow_runtime(at=WITHIN_WINDOW)
    assert report["requires_activation_authorization"] is False
    assert report["requires_owner_signing_key"] is False
    assert report["actor_role"] == "shadow-operator"

    # 用一个**没有**任何授权证据的模拟器也能停用。
    registry = ShadowSnapshotRegistry()
    reader = ShadowRuntimeReader(registry=registry)
    bare = ShadowActivationSimulator(registry=registry, reader=reader)
    bare_report = bare.disable_shadow_runtime(at=WITHIN_WINDOW)
    assert bare_report["disabled"] is True
    assert reader.enabled is False

    # 生产激活与读取器标志不变。
    assert shadow_simulation_status()["production_activation"] == "NOT_ENABLED"
    assert shadow_simulation_status()["production_runtime_reader"] == "NOT_ENABLED"
    assert SHADOW_SIMULATION_VERSION == "1.0.0"


def test_sg_25_rollback_to_still_valid_snapshot_succeeds(env):
    """SG-25: 回滚到仍然有效的先前快照成功且确定。"""
    registry, reader, simulator = _simulator(env)
    first = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
        actor="shadow-operator",
    )
    second = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
        actor="shadow-operator",
    )
    assert first.status == "ROLLBACK_COMPLETED"
    assert first.succeeded is True
    assert first.recovery_fingerprint == second.recovery_fingerprint
    assert first.effective_snapshot_id == env.snapshot.snapshot_id
    assert first.audit_event_types == (
        "SHADOW_ROLLBACK_REQUESTED",
        "SHADOW_ROLLBACK_COMPLETED",
    )
    assert first.as_dict()["canonical_writes"] == 0
    assert first.as_dict()["is_production_authority"] is False
    assert reader.enabled is True


def test_sg_26_rollback_to_revoked_snapshot_is_rejected(env):
    """SG-26: 回滚到已撤销授权支撑的快照被拒绝。"""
    registry, _reader, simulator = _simulator(env)
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(
            env,
            revocation_registry=(
                (env.authorization.activation_id, REVOCATION_REFERENCE),
            ),
        ),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_REJECTED"
    assert "ROLLBACK_AUTHORIZATION_NOT_CONSUMABLE" in report.rejection_codes
    assert report.audit_event_types == (
        "SHADOW_ROLLBACK_REQUESTED",
        "SHADOW_ROLLBACK_REJECTED",
    )
    # 有效状态未被改变（仍然停留在原活动快照）。
    assert registry.active_snapshot_id == env.snapshot.snapshot_id


def test_sg_27_rollback_to_expired_snapshot_is_rejected(env):
    """SG-27: 回滚到已过期授权支撑的快照被拒绝。"""
    registry, _reader, simulator = _simulator(env)
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, current_time=AFTER_EXPIRY),
        at=AFTER_EXPIRY,
    )
    assert report.status == "ROLLBACK_REJECTED"
    assert "ROLLBACK_AUTHORIZATION_NOT_CONSUMABLE" in report.rejection_codes
    assert registry.active_snapshot_id == env.snapshot.snapshot_id


def test_sg_28_rollback_to_stale_baseline_snapshot_is_rejected(env):
    """SG-28: 回滚到基线陈旧的快照被拒绝。"""
    registry, _reader, simulator = _simulator(env)
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, current_baseline_fingerprint="baseline-B"),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_REJECTED"
    assert "ROLLBACK_BASELINE_STALE" in report.rejection_codes

    dataset_report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, dataset_identity="another-dataset"),
        at=WITHIN_WINDOW,
    )
    assert "ROLLBACK_DATASET_MISMATCH" in dataset_report.rejection_codes

    unknown_report = simulator.rollback_shadow_snapshot(
        target_snapshot_id="0" * 64, read_context=_ctx(env), at=WITHIN_WINDOW
    )
    assert "ROLLBACK_TARGET_NOT_REGISTERED" in unknown_report.rejection_codes
    assert registry.active_snapshot_id == env.snapshot.snapshot_id


def test_sg_29_failed_rollback_never_leaves_a_partially_enabled_reader(env):
    """SG-29: 失败的回滚绝不留下部分启用的读取器。"""
    registry, reader, simulator = _simulator(env)

    # 停用后回滚必须被拒绝（不得绕过紧急停用）。
    simulator.disable_shadow_runtime(at=WITHIN_WINDOW, reason="disable first")
    assert reader.enabled is False
    blocked = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert blocked.status == "ROLLBACK_REJECTED"
    assert "ROLLBACK_READER_DISABLED" in blocked.rejection_codes
    assert reader.enabled is False
    assert registry.active_snapshot_id is None
    assert reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).resolution_status == "REJECTED_SHADOW"

    # 失败的回滚不改变有效状态：重新启用后仍停留在原活动快照。
    reader.re_enable(at=WITHIN_WINDOW, reason="separately re-enabled")
    registry.select(env.snapshot.snapshot_id)
    stale = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env, current_baseline_fingerprint="baseline-B"),
        at=WITHIN_WINDOW,
    )
    assert stale.status == "ROLLBACK_REJECTED"
    assert registry.active_snapshot_id == env.snapshot.snapshot_id
    assert reader.enabled is True
    assert reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    ).resolution_status == "RESOLVED_SHADOW"

    # 回滚拒绝码注册表自洽。
    assert len({code for code, _ in ROLLBACK_REJECTION_CODES}) == len(
        ROLLBACK_REJECTION_CODES
    )
