"""
影子运行期读取器测试套件（SG-01 .. SG-11 与 SG-40 .. SG-50）
==========================================================
覆盖：
- 读取器的 12 项校验、读时授权再验证、时钟边界、唯一绑定与所有权冲突；
- 快照指纹独立复算（frozen dataclass 不等于可信）；
- 生产运行期隔离（无导入、无激活标志、无 Canonical 改写、七存储不变）；
- 既有资格 / 批准 / 修复契约与跨平台 CI 声明保持不变。

全部使用隔离的合成夹具与临时目录中的一次性 Ed25519 密钥，绝不访问网络，
绝不触碰生产数据，绝不激活任何运行期映射。
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re

import pytest

from tools.shadow_runtime_activation import (
    ALT_CANONICAL_ID,
    CANONICAL_ID,
    DATASET,
    REVOCATION_REFERENCE,
    build_synthetic_shadow_fixture,
)
from utils import fetcher_500, scraper
from utils.mapping_governance import (
    MAPPING_APPROVAL_PURPOSE,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    SignatureVerificationError,
    governance_status,
    verify_mapping_approval,
)
from utils.runtime_activation_authorization import (
    AUTHORIZATION_REJECTION_CODE_SET,
    RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE,
    RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION,
    SHADOW_SNAPSHOT_BUILDER_VERSION,
    ShadowActivationBinding,
    generate_activation_authorizer_keypair,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
)
from utils.runtime_mapping_policy import (
    RUNTIME_ELIGIBILITY_RULES,
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)
from utils.shadow_runtime_reader import (
    PRODUCTION_ACTIVATION,
    PRODUCTION_RUNTIME_READER,
    READER_REJECTION_CODE_SET,
    SHADOW_READER_VERSION,
    ShadowReadContext,
    ShadowReadResult,
    ShadowRuntimeReader,
    read_shadow_runtime_mapping,
    shadow_reader_status,
)
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

BEFORE_AUTHORIZATION = "2029-12-31T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """隔离工作区中的完整合成夹具（密钥位于工作区内的 keys/ 目录）。"""
    workspace = tmp_path_factory.mktemp("shadow_reader_env")
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


# ---------------------------------------------------------------------------
# SG-01 .. SG-11 — 读取器
# ---------------------------------------------------------------------------

def test_sg_01_valid_shadow_mapping_resolves(env):
    """SG-01: 一个有效、已批准、已授权、活动的影子映射成功解析。"""
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert isinstance(result, ShadowReadResult)
    assert result.resolution_status == "RESOLVED_SHADOW"
    assert result.candidate_canonical_id == CANONICAL_ID
    assert result.snapshot_id == env.snapshot.snapshot_id
    assert result.decision_fingerprint == (
        env.authorization.eligibility_decision_fingerprint
    )
    assert result.rejection_codes == ()
    assert result.source_identity_key == ScopedSourceIdentityKey.for_observation(
        env.observation
    ).source_key()
    assert result.is_shadow_only is True
    assert result.is_production_authorization is False
    assert result.as_dict()["is_production_authority"] is False
    # 向后兼容：父类字段与语义保持。
    from utils.runtime_activation_authorization import (
        ShadowRuntimeResolutionResult,
    )

    assert isinstance(result, ShadowRuntimeResolutionResult)
    assert reader.is_generation_current(result) is True


def test_sg_02_unknown_identity_returns_not_found(env):
    """SG-02: 未知源身份 -> NOT_FOUND（与「不安全映射」严格区分）。"""
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.other_observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "NOT_FOUND"
    assert result.candidate_canonical_id == ""
    assert result.rejection_codes == ()
    assert result.snapshot_id == env.snapshot.snapshot_id


def test_sg_03_malformed_observation_is_rejected(env):
    """SG-03: 观测畸形 -> REJECTED_SHADOW。"""
    reader = ShadowRuntimeReader()
    for bad in (None, {"id": CANONICAL_ID}, "500j-2030-05-01-007", 42):
        result = reader.read(
            observation=bad, snapshot=env.snapshot, read_context=_ctx(env)
        )
        assert result.resolution_status == "REJECTED_SHADOW", bad
        assert "OBSERVATION_MALFORMED" in result.rejection_codes, bad

    # 裸源 ID 绝不构成解析证据：即使它是合法字符串也被拒绝。
    assert "OBSERVATION_MALFORMED" in reader.read(
        observation=CANONICAL_ID, snapshot=env.snapshot, read_context=_ctx(env)
    ).rejection_codes


def test_sg_04_invalid_snapshot_is_rejected(env):
    """SG-04: 无效快照 -> REJECTED_SHADOW。"""
    reader = ShadowRuntimeReader()
    missing = reader.read(
        observation=env.observation, snapshot=None, read_context=_ctx(env)
    )
    assert missing.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_NOT_PROVIDED" in missing.rejection_codes

    malformed = reader.read(
        observation=env.observation,
        snapshot={"snapshot_id": "x"},
        read_context=_ctx(env),
    )
    assert "SNAPSHOT_MALFORMED" in malformed.rejection_codes

    # 未登记的 snapshot_id -> SNAPSHOT_NOT_REGISTERED（附注册表时）。
    from utils.shadow_activation_simulation import ShadowSnapshotRegistry

    registry = ShadowSnapshotRegistry()
    registered_reader = ShadowRuntimeReader(registry=registry)
    unregistered = registered_reader.read(
        observation=env.observation, snapshot_id="0" * 64, read_context=_ctx(env)
    )
    assert "SNAPSHOT_NOT_REGISTERED" in unregistered.rejection_codes

    # 非可消费状态 -> SNAPSHOT_NOT_CONSUMABLE。
    disabled = dataclasses.replace(env.snapshot, status="DISABLED")
    disabled = dataclasses.replace(
        disabled,
        snapshot_id=disabled.content_fingerprint(),
        snapshot_fingerprint=disabled.content_fingerprint(),
    )
    assert "SNAPSHOT_NOT_CONSUMABLE" in reader.read(
        observation=env.observation, snapshot=disabled, read_context=_ctx(env)
    ).rejection_codes


def test_sg_05_tampered_snapshot_fingerprint_is_rejected(env):
    """SG-05: 被篡改的快照指纹被拒绝（frozen dataclass 不等于可信）。"""
    reader = ShadowRuntimeReader()

    forged_fingerprint = dataclasses.replace(env.snapshot, snapshot_fingerprint="0" * 64)
    result = reader.read(
        observation=env.observation,
        snapshot=forged_fingerprint,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in result.rejection_codes

    forged_id = dataclasses.replace(env.snapshot, snapshot_id="0" * 64)
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in reader.read(
        observation=env.observation, snapshot=forged_id, read_context=_ctx(env)
    ).rejection_codes

    # 内容被改但指纹未更新 -> 独立复算检出。
    extra_binding = ShadowActivationBinding(
        activation_id="forged-activation",
        source_identity_key=env.authorization.scoped_source_identity_key,
        candidate_canonical_id=ALT_CANONICAL_ID,
        eligibility_decision_fingerprint="forged",
        authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
    )
    tampered_content = dataclasses.replace(
        env.snapshot, bindings=env.snapshot.bindings + (extra_binding,)
    )
    assert "SNAPSHOT_FINGERPRINT_MISMATCH" in reader.read(
        observation=env.observation,
        snapshot=tampered_content,
        read_context=_ctx(env),
    ).rejection_codes

    # 2-F 的评估器不做指纹复算（这正是读取器必须独立校验的原因）。
    from utils.runtime_activation_authorization import (
        evaluate_shadow_runtime_mapping,
    )

    assert evaluate_shadow_runtime_mapping(
        snapshot=forged_fingerprint, observation=env.observation
    ).resolution_status == "RESOLVED_SHADOW"


def test_sg_06_stale_canonical_baseline_is_rejected(env):
    """SG-06: 陈旧 Canonical 基线 -> REJECTED_SHADOW。"""
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_baseline_fingerprint="baseline-B"),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "BASELINE_STALE" in result.rejection_codes


def test_sg_07_different_dataset_is_rejected(env):
    """SG-07: 不同数据集 -> REJECTED_SHADOW。"""
    reader = ShadowRuntimeReader()
    result = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, dataset_identity="another-dataset"),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "DATASET_IDENTITY_MISMATCH" in result.rejection_codes


def test_sg_08_expired_authorization_is_rejected_at_read_time(env):
    """SG-08: 读取时授权已过期 -> REJECTED_SHADOW。"""
    reader = ShadowRuntimeReader()
    inside = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )
    assert inside.resolution_status == "RESOLVED_SHADOW"

    after = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=AFTER_EXPIRY),
    )
    assert after.resolution_status == "REJECTED_SHADOW"
    assert "AUTHORIZATION_NOT_CONSUMABLE" in after.rejection_codes
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in after.rejection_codes
    # 快照对象本身没有变化：拒绝来自**读时**再验证。
    assert after.snapshot_id == inside.snapshot_id


def test_sg_09_revoked_authorization_is_rejected_at_read_time(env):
    """SG-09: 读取时授权已撤销 -> REJECTED_SHADOW。"""
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


def test_sg_10_valid_snapshot_with_invalid_current_authorization_never_resolves(
    env, tmp_path
):
    """SG-10: 快照有效但当前授权无效时绝不解析。"""
    reader = ShadowRuntimeReader()

    # (a) 不可信签名密钥。
    other_private = str(tmp_path / "other_private.pem")
    other_public = str(tmp_path / "other_public.pem")
    generate_activation_authorizer_keypair(other_private, other_public)
    untrusted = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, trusted_public_key_path=other_public),
    )
    assert untrusted.resolution_status == "REJECTED_SHADOW"
    assert "AUTHORIZATION_NOT_CONSUMABLE" in untrusted.rejection_codes
    assert "SIGNER_KEY_UNTRUSTED" in untrusted.rejection_codes

    # (b) 缺少信任锚。
    no_anchor = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, trusted_public_key_path=None),
    )
    assert "TRUST_ANCHOR_MISSING" in no_anchor.rejection_codes

    # (c) 缺少可复核证据。
    no_evidence = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, evidence=()),
    )
    assert "SNAPSHOT_AUTHORIZATION_MISSING" in no_evidence.rejection_codes

    # (d) 受控时钟缺失 / 畸形。
    for bad_clock in ("", "not-a-timestamp", None):
        result = reader.read(
            observation=env.observation,
            snapshot=env.snapshot,
            read_context=_ctx(env, current_time=bad_clock),
        )
        assert result.resolution_status == "REJECTED_SHADOW"
        assert "CLOCK_UNAVAILABLE" in result.rejection_codes, bad_clock

    # (e) 授权尚未生效。
    early = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, current_time=BEFORE_AUTHORIZATION),
    )
    assert "AUTHORIZATION_NOT_CONSUMABLE" in early.rejection_codes
    assert "NOT_YET_VALID" in early.rejection_codes


def test_sg_11_ownership_conflict_cannot_resolve_arbitrarily(env):
    """SG-11: 源身份所有权冲突绝不被任意解析（绝不降级为 NOT_FOUND）。"""
    reader = ShadowRuntimeReader()

    # (a) 既有 Canonical 所有权指向另一个目标。
    conflicting_owner = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(
            env,
            known_source_bindings=(
                (
                    env.authorization.scoped_source_identity_key,
                    ALT_CANONICAL_ID,
                ),
            ),
        ),
    )
    assert conflicting_owner.resolution_status == "REJECTED_SHADOW"
    assert "OWNERSHIP_CONFLICT" in conflicting_owner.rejection_codes
    assert conflicting_owner.candidate_canonical_id == ""

    # (b) 快照内同一键指向不同 Canonical 目标（手工构造以绕过构建器）。
    conflicting_binding = ShadowActivationBinding(
        activation_id="conflicting-activation",
        source_identity_key=env.authorization.scoped_source_identity_key,
        candidate_canonical_id=ALT_CANONICAL_ID,
        eligibility_decision_fingerprint="conflicting",
        authorized_scope="RUNTIME_READ_ONLY_RESOLUTION",
    )
    conflicting_snapshot = dataclasses.replace(
        env.snapshot,
        bindings=tuple(
            sorted(
                env.snapshot.bindings + (conflicting_binding,),
                key=lambda b: (b.source_identity_key, b.candidate_canonical_id),
            )
        ),
    )
    fingerprint_value = conflicting_snapshot.content_fingerprint()
    conflicting_snapshot = dataclasses.replace(
        conflicting_snapshot,
        snapshot_id=fingerprint_value,
        snapshot_fingerprint=fingerprint_value,
    )
    result = reader.read(
        observation=env.observation,
        snapshot=conflicting_snapshot,
        read_context=_ctx(env),
    )
    assert result.resolution_status == "REJECTED_SHADOW"
    assert "OWNERSHIP_CONFLICT" in result.rejection_codes

    # (c) 绑定目标不是已知 Canonical 所有者。
    unknown_owner = reader.read(
        observation=env.observation,
        snapshot=env.snapshot,
        read_context=_ctx(env, known_canonical_ids=("500j-2030-05-01-999",)),
    )
    assert "CANONICAL_OWNER_UNKNOWN" in unknown_owner.rejection_codes


# ---------------------------------------------------------------------------
# SG-40 .. SG-50 — 生产隔离与既有契约
# ---------------------------------------------------------------------------

def test_sg_40_scraper_refresh_remains_unchanged(isolated_data_dir, monkeypatch):
    """SG-40: scraper.refresh() 行为不变。"""
    with open(os.path.join(REPO_ROOT, "utils", "scraper.py"), encoding="utf-8") as fh:
        source = fh.read()
    for forbidden in (
        "shadow_runtime_reader",
        "shadow_activation_simulation",
        "shadow_activation_audit_store",
        "runtime_activation_authorization",
        "runtime_activation_design",
    ):
        assert forbidden not in source, forbidden
    assert "acquire_refresh_lock" in source
    assert "validate_ingestion_identity(existing, incoming)" in source
    assert "validate_canonical_uniqueness(" in source
    assert "GovernanceLedger" not in source

    _seed_seven_stores(isolated_data_dir)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda *a, **k: [])
    result = scraper.refresh(verbose=False)
    assert "sources" in result and "total" in result


def test_sg_41_no_production_module_imports_the_shadow_reader_or_simulator():
    """SG-41: 生产运行期模块绝不导入影子读取器 / 模拟器 / 审计存储。"""
    for name in (
        "scraper.py",
        "fetcher_500.py",
        "ingestion_identity.py",
        "match_identity.py",
        "mapping_governance.py",
        "runtime_mapping_policy.py",
        "runtime_mapping_eligibility.py",
        "runtime_activation_design.py",
        "runtime_activation_authorization.py",
        "provider_evidence.py",
        "source_id_migration.py",
        "daily_loader.py",
        "match_lifecycle.py",
        "source_identity.py",
    ):
        with open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in (
            "shadow_runtime_reader",
            "shadow_activation_simulation",
            "shadow_activation_audit_store",
        ):
            assert forbidden not in source, (name, forbidden)

    # 2-F 的激活原型也不得导入本阶段的影子模块。
    with open(
        os.path.join(REPO_ROOT, "utils", "runtime_activation_authorization.py"),
        encoding="utf-8",
    ) as fh:
        activation_source = fh.read()
    assert "shadow_runtime_reader" not in activation_source
    assert "shadow_activation_simulation" not in activation_source
    assert "shadow_activation_audit_store" not in activation_source


def test_sg_42_to_sg_44_production_flags_remain_disabled():
    """SG-42/43/44: 生产激活、生产读取器与映射写入授权保持关闭。"""
    assert RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED"
    assert PRODUCTION_ACTIVATION == "NOT_ENABLED"
    assert PRODUCTION_RUNTIME_READER == "NOT_ENABLED"
    assert PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"

    status = shadow_reader_status()
    assert status["production_runtime_reader"] == "NOT_ENABLED"
    assert status["production_activation"] == "NOT_ENABLED"
    assert status["writes_files"] is False
    assert status["creates_bindings"] is False
    assert status["modifies_canonical_records"] is False
    assert status["consumed_by_production_runtime"] is False
    assert status["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )

    # 读取器模块不得暴露任何生产启用标志。
    import utils.shadow_runtime_reader as reader_module

    for forbidden in (
        "ENABLE_RUNTIME_READER",
        "PRODUCTION_READER_ENABLED",
        "ACTIVATION_ENABLED",
    ):
        assert not hasattr(reader_module, forbidden), forbidden


def test_sg_45_no_canonical_id_is_rewritten(env, tmp_path):
    """SG-45: 影子读取绝不改写任何 Canonical ID。"""
    reader = ShadowRuntimeReader()
    before = {CANONICAL_ID, ALT_CANONICAL_ID}
    result = reader.read(
        observation=env.observation, snapshot=env.snapshot, read_context=_ctx(env)
    )
    assert result.candidate_canonical_id in before
    # 结果对象不提供任何写入入口。
    for forbidden in ("apply", "write", "commit", "rewrite", "persist"):
        assert not hasattr(result, forbidden)

    # 结构性证明：读取器与模拟层不含任何 Canonical 写入原语。
    for name in ("shadow_runtime_reader.py", "shadow_activation_simulation.py"):
        with open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8") as fh:
            source = fh.read()
        for forbidden in (
            "atomic_write_json",
            "validate_canonical_uniqueness",
            "daily_matches",
            "save_json",
        ):
            assert forbidden not in source, (name, forbidden)
    assert tmp_path is not None


def test_sg_46_all_seven_stores_remain_byte_identical(
    isolated_data_dir, monkeypatch, env
):
    """SG-46: 全部影子操作后七个体育数据存储逐字节不变。"""
    _seed_seven_stores(isolated_data_dir)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda *a, **k: [])
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    reader = ShadowRuntimeReader()
    for context in (
        _ctx(env),
        _ctx(env, current_time=AFTER_EXPIRY),
        _ctx(
            env,
            revocation_registry=(
                (env.authorization.activation_id, REVOCATION_REFERENCE),
            ),
        ),
        _ctx(env, current_baseline_fingerprint="baseline-B"),
    ):
        reader.read(
            observation=env.observation, snapshot=env.snapshot, read_context=context
        )
    read_shadow_runtime_mapping(
        observation=env.other_observation,
        snapshot=env.snapshot,
        read_context=_ctx(env),
    )

    assert _hash_seven_stores(isolated_data_dir) == before


def test_sg_47_existing_eligibility_and_approval_contracts_remain(env):
    """SG-47: 既有资格评估与 Ed25519 批准契约保持成功。"""
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.2.0"
    assert RUNTIME_ELIGIBILITY_EVALUATOR_VERSION == "1.2.0"
    assert RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION == "1.1.0"
    assert len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert env.decision.eligibility_result == "ELIGIBLE"
    assert env.decision.is_production_authorization is False

    # Ed25519 批准仍然拒绝旧契约 / 缺失签名的批准件。
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            {"approval_schema_version": "1.0.0"},
            proposal=env.proposal,
            source_observation_fingerprint="x",
            dataset_identity=DATASET,
            current_baseline_fingerprint="baseline-A",
            trusted_public_key_path=env.authorizer_public,
        )


def test_sg_48_existing_activation_authorization_contracts_remain():
    """SG-48: 既有激活授权契约保持成功（含本阶段对原型缺陷的修正）。"""
    assert RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION == "1.0.0"
    assert RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE != MAPPING_APPROVAL_PURPOSE
    assert len(AUTHORIZATION_REJECTION_CODE_SET) == 27
    assert SHADOW_SNAPSHOT_BUILDER_VERSION == "1.1.0"
    assert len(READER_REJECTION_CODE_SET) >= 15
    assert SHADOW_READER_VERSION == "1.2.0"
    assert governance_status()["production_mapping_write_authorization"] == (
        "NOT_GRANTED"
    )


def test_sg_49_production_repair_lifecycle_remains_independently_authorized():
    """SG-49: 生产修复生命周期仍独立授权，且未引用影子模块。"""
    import tools.production_canonical_repair as repair

    assert repair.AUTHORIZATION_SCHEMA_VERSION == "2.0.0"
    assert repair.LEGACY_AUTHORIZATION_SCHEMA_VERSIONS == ["1.0.0"]

    parser = repair.build_parser()
    help_text = parser.format_help()
    for sub in parser._subparsers._group_actions[0].choices.values():
        help_text += sub.format_help()
    for forbidden in (
        "--force",
        "--skip-verification",
        "--ignore-hash",
        "--allow-unsigned",
        "--allow-production-without-approval",
        "--fault-inject",
    ):
        assert forbidden not in help_text, forbidden

    with open(
        os.path.join(REPO_ROOT, "tools", "production_canonical_repair.py"),
        encoding="utf-8",
    ) as fh:
        source = fh.read()
    for forbidden in (
        "shadow_runtime_reader",
        "shadow_activation_simulation",
        "shadow_activation_audit_store",
        "shadow_runtime_activation",
    ):
        assert forbidden not in source, forbidden

    # 影子入口工具绝不复用生产修复的 apply / recover 子命令。
    with open(
        os.path.join(REPO_ROOT, "tools", "shadow_runtime_activation.py"),
        encoding="utf-8",
    ) as fh:
        tool_source = fh.read()
    assert "add_parser(\"apply\")" not in tool_source
    assert "add_parser(\"recover\")" not in tool_source
    assert "--activate" not in tool_source


def test_sg_50_ubuntu_and_windows_ci_are_declared():
    """SG-50: CI 声明双平台矩阵与四个必需 job。"""
    with open(
        os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8"
    ) as fh:
        workflow = fh.read()
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert "Offline pytest suite" in workflow
    assert "Isolated Canonical repair lifecycle" in workflow
    assert re.search(r"os:\s*\[ubuntu-latest,\s*windows-latest\]", workflow)
    assert re.search(r'python-version:\s*\["3\.12"\]', workflow)
