"""
恢复证据状态绑定回归（RE-01 .. RE-19）
=======================================
本文件验证严格再启用必须把签名恢复证据绑定到**当前实际状态**：

- 审计存储身份、经验证链尖、条目数、完成事件 ID；
- 显式选中的快照与有效快照指针；
- 恢复状态 generation（状态转换身份）；
- 空审计历史不得证明已恢复选中激活；
- 任何失败都保持停用且不切换指针。

全部夹具位于 pytest 临时目录，绝不触碰生产数据或生产激活。
"""
from __future__ import annotations

import dataclasses
import os

import pytest

from tools.shadow_runtime_activation import (
    DATASET,
    REVOCATION_REFERENCE,
    build_synthetic_shadow_fixture,
)
from utils.mapping_governance import generate_mapping_owner_keypair
from utils.recovery_authority import (
    RECOVERY_EVIDENCE_TYPE,
    build_recovery_evidence,
    recovery_state_fingerprint,
)
from utils.shadow_activation_audit_store import ShadowActivationAuditStore
from utils.shadow_activation_simulation import (
    ShadowActivationSimulator,
    ShadowSnapshotRegistry,
)
from utils.shadow_runtime_reader import (
    ShadowAuthorizationEvidence,
    ShadowRuntimeReader,
    ShadowReenablementError,
)
from utils.trusted_artifacts import sign_trusted_artifact

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WITHIN = "2030-01-15T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("recovery_binding_env")
    return build_synthetic_shadow_fixture(str(workspace))


def _keys(tmp_path, name: str) -> tuple[str, str]:
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    private = str(base / "private.pem")
    public = str(base / "public.pem")
    generate_mapping_owner_keypair(private, public)
    return private, public


def _store(tmp_path, name: str, dataset: str = DATASET) -> ShadowActivationAuditStore:
    base = tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    return ShadowActivationAuditStore(str(base), dataset_identity=dataset)


def _second_snapshot(env):
    changed = dataclasses.replace(
        env.snapshot, generated_at="2030-01-15T00:00:01+00:00"
    )
    value = changed.content_fingerprint()
    return dataclasses.replace(
        changed, snapshot_id=value, snapshot_fingerprint=value
    )


def _context(env, **overrides):
    return env.read_context(**overrides)


def _activation_evidence(env) -> ShadowAuthorizationEvidence:
    return ShadowAuthorizationEvidence(
        authorization=env.authorization,
        decision=env.decision,
        proposal=env.proposal,
    )


def _strict_reader(registry, store=None) -> ShadowRuntimeReader:
    return ShadowRuntimeReader(
        registry=registry,
        audit_store=store,
        require_recovery_evidence_for_reenable=True,
    )


def _prepared(tmp_path, env, name: str, *, snapshot=None, keys_name=None):
    """创建含 COMPLETED 事件的存储 + 停用但已选择快照的严格读取器。"""
    keys_name = keys_name or f"{name}_keys"
    recovery_private, recovery_public = _keys(tmp_path, keys_name)
    store = _store(tmp_path, name)
    target = snapshot or env.snapshot
    registry = ShadowSnapshotRegistry()
    registry.register(target)
    registry.select(target.snapshot_id)
    reader = _strict_reader(registry, store)
    handle = reader.read(
        observation=env.observation, snapshot=target, read_context=_context(env)
    )
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    assert simulator.rollback_shadow_snapshot(
        target_snapshot_id=target.snapshot_id,
        read_context=_context(env),
        at=WITHIN,
    ).status == "ROLLBACK_COMPLETED"
    # 停用但不写入审计事件，使最后一条仍是 COMPLETED。
    ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=None
    ).disable_shadow_runtime(at=WITHIN, reason="recovery test disable")
    registry.select(target.snapshot_id)
    checkpoint = store.export_checkpoint(recovery_private, issued_at=WITHIN)
    return {
        "store": store,
        "registry": registry,
        "reader": reader,
        "handle": handle,
        "recovery_private": recovery_private,
        "recovery_public": recovery_public,
        "checkpoint": checkpoint,
        "target": target,
    }


def _build(env, prepared, *, selected=None, effective=None, current_time=WITHIN,
           checkpoint=None, store=None):
    target = prepared["target"]
    return build_recovery_evidence(
        audit_store=store or prepared["store"],
        audit_checkpoint=checkpoint or prepared["checkpoint"],
        checkpoint_trusted_public_key_path=prepared["recovery_public"],
        signing_private_key_path=prepared["recovery_private"],
        current_time=current_time,
        recovery_actor="recovery-operator",
        selected_snapshot_id=selected if selected is not None else target.snapshot_id,
        effective_snapshot_id=(
            effective if effective is not None else target.snapshot_id
        ),
        max_age_seconds=3600,
    )


def _reenable(reader, env, recovery, *, recovery_public, **overrides):
    return reader.re_enable(
        at=overrides.pop("at", WITHIN),
        read_context=_context(
            env, recovery_trusted_public_key_path=recovery_public, **overrides
        ),
        evidence=_activation_evidence(env),
        recovery_evidence=recovery,
    )


def test_re_01_cross_store_recovery_evidence_is_rejected(env, tmp_path):
    """RE-01: Store A 的证据不得恢复绑定 Store B 的读取器。"""
    prepared_a = _prepared(tmp_path, env, "re01_a")
    evidence_a = _build(env, prepared_a)
    prepared_b = _prepared(tmp_path, env, "re01_b")
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared_b["reader"],
            env,
            evidence_a,
            recovery_public=prepared_a["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes
    assert prepared_b["reader"].enabled is False


def test_re_02_wrong_selected_snapshot_is_rejected(env, tmp_path):
    """RE-02: 证据绑定的有效快照必须等于当前选中快照。"""
    prepared = _prepared(tmp_path, env, "re02")
    evidence = _build(env, prepared)
    other = _second_snapshot(env)
    registry = prepared["registry"]
    registry.register(other)
    registry.select(other.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes
    assert registry.active_snapshot_id == other.snapshot_id
    assert prepared["reader"].enabled is False


def test_re_03_empty_audit_history_cannot_prove_recovery(env, tmp_path):
    """RE-03: 空审计历史不得证明选中激活已恢复。"""
    empty_store = _store(tmp_path, "re03_empty")
    recovery_private, recovery_public = _keys(tmp_path, "re03_keys")
    checkpoint = empty_store.export_checkpoint(recovery_private, issued_at=WITHIN)
    evidence = build_recovery_evidence(
        audit_store=empty_store,
        audit_checkpoint=checkpoint,
        checkpoint_trusted_public_key_path=recovery_public,
        signing_private_key_path=recovery_private,
        current_time=WITHIN,
        recovery_actor="recovery-operator",
        selected_snapshot_id="",
        effective_snapshot_id="",
        max_age_seconds=3600,
    )
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = _strict_reader(registry, empty_store)
    reader.disable(at=WITHIN, reason="recovery test disable")
    registry.select(env.snapshot.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(reader, env, evidence, recovery_public=recovery_public)
    assert "RE_ENABLE_RECOVERY_EMPTY_HISTORY" in exc.value.rejection_codes
    assert reader.enabled is False


def test_re_04_stale_chain_tip_evidence_is_rejected(env, tmp_path):
    """RE-04 / RE-14: 审计链推进后旧证据（旧 generation）失效。"""
    prepared = _prepared(tmp_path, env, "re04")
    evidence = _build(env, prepared)
    prepared["store"].append(
        event_type="SHADOW_SNAPSHOT_REGISTERED",
        occurred_at=WITHIN,
        actor="recovery-operator",
        actor_role="shadow-operator",
        payload={"marker": "advance"},
    )
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes
    assert prepared["reader"].enabled is False


def test_re_05_missing_trusted_audit_state_fails_closed(env, tmp_path):
    """RE-05: 读取器没有受信任当前审计状态源 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re05")
    evidence = _build(env, prepared)
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = _strict_reader(registry, None)
    reader.disable(at=WITHIN, reason="recovery test disable")
    registry.select(env.snapshot.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            reader, env, evidence, recovery_public=prepared["recovery_public"]
        )
    assert "RE_ENABLE_RECOVERY_STATE_SOURCE_MISSING" in exc.value.rejection_codes
    assert reader.enabled is False


def test_re_06_current_valid_recovery_succeeds_and_old_handle_stays_invalid(
    env, tmp_path
):
    """RE-06 / RE-19: 当前状态完全匹配时严格再启用成功，旧句柄仍失效。"""
    prepared = _prepared(tmp_path, env, "re06")
    evidence = _build(env, prepared)
    resumed = _reenable(
        prepared["reader"],
        env,
        evidence,
        recovery_public=prepared["recovery_public"],
    )
    assert resumed["enabled"] is True
    assert resumed["shadow_only"] is True
    assert resumed["grants_production_authorization"] is False
    assert resumed["recovery_evidence_fingerprint"]
    assert prepared["reader"].enabled is True
    assert prepared["reader"].is_generation_current(prepared["handle"]) is False
    assert prepared["registry"].active_snapshot_id == (
        prepared["target"].snapshot_id
    )


def test_re_07_wrong_dataset_is_rejected(env, tmp_path):
    """RE-07: 恢复证据绑定的数据集不一致 -> 拒绝。"""
    other_store = _store(tmp_path, "re07_other", dataset="another-dataset")
    recovery_private, recovery_public = _keys(tmp_path, "re07_keys")
    checkpoint = other_store.export_checkpoint(recovery_private, issued_at=WITHIN)
    evidence = build_recovery_evidence(
        audit_store=other_store,
        audit_checkpoint=checkpoint,
        checkpoint_trusted_public_key_path=recovery_public,
        signing_private_key_path=recovery_private,
        current_time=WITHIN,
        recovery_actor="recovery-operator",
        selected_snapshot_id="",
        effective_snapshot_id="",
        max_age_seconds=3600,
    )
    store = _store(tmp_path, "re07_reader")
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = _strict_reader(registry, store)
    reader.disable(at=WITHIN, reason="recovery test disable")
    registry.select(env.snapshot.snapshot_id)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(reader, env, evidence, recovery_public=recovery_public)
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


def test_re_08_wrong_trusted_signer_is_rejected(env, tmp_path):
    """RE-08: 恢复签名者不在信任锚内 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re08")
    evidence = _build(env, prepared)
    _other_private, other_public = _keys(tmp_path, "re08_other")
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=other_public,
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


def test_re_09_tampered_recovery_evidence_is_rejected(env, tmp_path):
    """RE-09: 篡改签名的恢复证据 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re09")
    evidence = _build(env, prepared)
    tampered = dataclasses.replace(evidence, signature="")
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            tampered,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


def test_re_10_expired_recovery_evidence_is_rejected(env, tmp_path):
    """RE-10: 超出允许新鲜度窗口 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re10")
    evidence = _build(env, prepared)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
            current_time="2030-01-15T02:00:00+00:00",
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


def _resign(prepared, payload):
    payload = dict(payload)
    payload["recovery_generation"] = recovery_state_fingerprint(payload)
    return sign_trusted_artifact(
        prepared["recovery_private"],
        artifact_type=RECOVERY_EVIDENCE_TYPE,
        payload=payload,
    )


def test_re_11_wrong_chain_tip_is_rejected(env, tmp_path):
    """RE-11: 受信任签名但链尖错误的证据 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re11")
    payload = dict(_build(env, prepared).payload())
    payload["chain_tip"] = "f" * 64
    forged = _resign(prepared, payload)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            forged,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes


def test_re_12_wrong_entry_count_is_rejected(env, tmp_path):
    """RE-12: 受信任签名但条目数错误的证据 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re12")
    payload = dict(_build(env, prepared).payload())
    payload["entry_count"] = int(payload["entry_count"]) + 1
    forged = _resign(prepared, payload)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            forged,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_STATE_MISMATCH" in exc.value.rejection_codes


def test_re_13_divergence_is_rejected(env, tmp_path):
    """RE-13: 事件/指针差异未消解 -> 拒绝。"""
    prepared = _prepared(tmp_path, env, "re13")
    divergent = _build(env, prepared, effective="unexpected-snapshot")
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            divergent,
            recovery_public=prepared["recovery_public"],
        )
    assert "RE_ENABLE_RECOVERY_EVIDENCE_INVALID" in exc.value.rejection_codes


def test_re_15_revoked_authorization_cannot_be_reenabled(env, tmp_path):
    """RE-15: 恢复证据不能复活已撤销授权。"""
    prepared = _prepared(tmp_path, env, "re15")
    evidence = _build(env, prepared)
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
            revocation_registry=(
                (env.authorization.activation_id, REVOCATION_REFERENCE),
            ),
        )
    assert "RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE" in exc.value.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in exc.value.rejection_codes


def test_re_16_expired_authorization_cannot_be_reenabled(env, tmp_path):
    """RE-16: 恢复证据不能复活已过期授权。"""
    prepared = _prepared(tmp_path, env, "re16")
    checkpoint = prepared["store"].export_checkpoint(
        prepared["recovery_private"], issued_at=AFTER_EXPIRY
    )
    evidence = _build(
        env, prepared, current_time=AFTER_EXPIRY, checkpoint=checkpoint
    )
    with pytest.raises(ShadowReenablementError) as exc:
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
            at=AFTER_EXPIRY,
            current_time=AFTER_EXPIRY,
        )
    assert "RE_ENABLE_AUTHORIZATION_NOT_CONSUMABLE" in exc.value.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in exc.value.rejection_codes


def test_re_17_failure_leaves_reader_disabled_and_pointer_unchanged(env, tmp_path):
    """RE-17 / RE-18: 失败保持停用，且不切换有效指针。"""
    prepared = _prepared(tmp_path, env, "re17")
    evidence = _build(env, prepared)
    other = _second_snapshot(env)
    registry = prepared["registry"]
    registry.register(other)
    registry.select(other.snapshot_id)
    with pytest.raises(ShadowReenablementError):
        _reenable(
            prepared["reader"],
            env,
            evidence,
            recovery_public=prepared["recovery_public"],
        )
    assert prepared["reader"].enabled is False
    assert prepared["reader"].generation == 1  # 仅 disable 递增
    assert registry.active_snapshot_id == other.snapshot_id
