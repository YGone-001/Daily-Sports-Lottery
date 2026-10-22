"""
独立运行期激活安全评审探针（SR-02 / SR-14 / SR-17 ~ SR-21）
==========================================================
本文件是 Phase 2-H **只读安全评审**的可执行证据，不修改任何生产代码：

- SR-02：伪造 ``VERIFIED_GOVERNANCE_EVIDENCE`` 来源必须被阻断（复述既有覆盖）；
- SR-14：审计事件已提交但有效指针未应用时，必须报告差异并 fail closed；
- SR-17：**有效链尾截断不会被检出**（无检查点）—— 记录为评审发现；
- SR-18：审计存储没有外部可信检查点 / 独立签名（如实声明）；
- SR-19：``re_enable()`` 不强制先执行审计恢复 —— 记录为评审发现；
- SR-20/21：生产模块不导入影子原型；仓库内账本 / 审计路径被拒绝。

所有夹具都在 pytest 临时目录中，绝不触碰生产数据或生产签名密钥。
"""
from __future__ import annotations

import os

import pytest

from tools.shadow_runtime_activation import (
    CANONICAL_ID,
    DATASET,
    build_synthetic_shadow_fixture,
)
from utils.mapping_governance import GovernanceLedger, LedgerPathError
from utils.shadow_activation_audit_store import (
    ShadowActivationAuditStore,
    ShadowAuditPathError,
    shadow_audit_store_status,
)
from utils.shadow_activation_simulation import (
    ShadowActivationSimulator,
    ShadowSnapshotRegistry,
)
from utils.shadow_runtime_reader import (
    ShadowAuthorizationEvidence,
    ShadowRuntimeReader,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("security_review_env")
    return build_synthetic_shadow_fixture(str(workspace))


def _ctx(env, **overrides):
    return env.read_context(**overrides)


def _evidence(env) -> ShadowAuthorizationEvidence:
    return ShadowAuthorizationEvidence(
        authorization=env.authorization,
        decision=env.decision,
        proposal=env.proposal,
    )


def _fresh_store(tmp_path, name: str) -> ShadowActivationAuditStore:
    workspace = tmp_path / name
    workspace.mkdir()
    return ShadowActivationAuditStore(str(workspace), dataset_identity=DATASET)


def _single_snapshot_registry(env, audit_store):
    registry = ShadowSnapshotRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    return registry, reader, ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=audit_store
    )


class _SelectFailingRegistry(ShadowSnapshotRegistry):
    """指针应用阶段注入失败（审计事件已提交之后）。"""

    def __init__(self) -> None:
        super().__init__()
        self.fail_select = False

    def select(self, snapshot_id: str) -> dict:
        if self.fail_select:
            raise RuntimeError("injected effective pointer application failure")
        return super().select(snapshot_id)


def test_sr_14_committed_event_with_unapplied_pointer_is_reported(env, tmp_path):
    """SR-14: 完成事件已持久提交但指针未应用时必须报告差异并 fail closed。"""
    store = _fresh_store(tmp_path, "sr14")
    registry = _SelectFailingRegistry()
    registry.register(env.snapshot)
    registry.select(env.snapshot.snapshot_id)
    reader = ShadowRuntimeReader(registry=registry)
    simulator = ShadowActivationSimulator(
        registry=registry, reader=reader, audit_store=store
    )
    registry.fail_select = True

    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.status == "ROLLBACK_AUDIT_FAILED"
    assert "ROLLBACK_COMMIT_APPLY_FAILED" in report.rejection_codes
    assert report.audit_committed is True
    assert report.commit_outcome == "COMMITTED"
    assert report.completion_event_observed is True
    assert report.pointer_applied is False
    assert report.discrepancy
    assert report.reader_disabled is True
    assert registry.active_snapshot_id is None
    assert reader.enabled is False
    # 审计事件确实存在，且链自洽 —— 差异被如实暴露，而不是被静默忽略。
    assert store.verify()["integrity"] == "OK"
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_COMPLETED"


def test_sr_17_valid_chain_tail_truncation_is_not_detected_review_finding(env, tmp_path):
    """SR-17: 有效链尾截断不可检测（无检查点）—— 评审发现，而非 PASS。"""
    store = _fresh_store(tmp_path, "sr17")
    _, _, simulator = _single_snapshot_registry(env, store)
    first = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert first.status == "ROLLBACK_COMPLETED"
    before = store.records()
    assert before[-1].event_type == "SHADOW_ROLLBACK_COMPLETED"

    # 干净地删除最后一条完整记录（保留合法 LF 终止与哈希链）。
    with open(store.path, "r", encoding="utf-8") as handle:
        lines = [line for line in handle.read().split("\n") if line]
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines[:-1]:
            handle.write(line + "\n")

    # 结构校验仍然通过：哈希链只能证明内部一致性，无法发现尾部截断。
    report = store.verify()
    assert report["integrity"] == "OK"
    assert report["entry_count"] == len(before) - 1
    assert store.records()[-1].event_type == "SHADOW_ROLLBACK_REQUESTED"
    assert report["trusted_checkpoint_implemented"] is False


def test_sr_18_no_external_trust_anchor_or_checkpoint(tmp_path):
    """SR-18: 审计存储如实声明缺少外部信任锚 / 检查点 / 独立签名。"""
    store = _fresh_store(tmp_path, "sr18")
    status = shadow_audit_store_status()
    assert status["trusted_checkpoint_implemented"] is False
    assert status["externally_authenticated"] is False
    assert "NO EXTERNAL TRUST ANCHOR" in status["trust_anchor"]
    assert status["production_event_emission"] is False
    for forbidden in ("checkpoint", "sign_checkpoint", "external_anchor"):
        assert not hasattr(store, forbidden), forbidden
    with open(store.path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("")
    assert store.verify()["entry_count"] == 0
    assert store.store_identity()  # 身份是确定性指纹，不是签名


def test_sr_19_re_enable_does_not_require_audit_recovery_review_finding(
    env, tmp_path
):
    """SR-19: ``re_enable()`` 不强制先执行审计恢复 —— 评审发现。"""
    store = _fresh_store(tmp_path, "sr19")

    class _AckLostStore:
        def __init__(self, real):
            self.real = real

        def append(self, **kwargs):
            record = self.real.append(**kwargs)
            if kwargs.get("event_type") == "SHADOW_ROLLBACK_COMPLETED":
                raise OSError("injected acknowledgment loss")
            return record

        def records(self):
            return self.real.records()

        def verify(self):
            return self.real.verify()

        def chain_tip(self):
            return self.real.chain_tip()

        def store_identity(self):
            return self.real.store_identity()

        def receipt_for(self, record):
            return self.real.receipt_for(record)

    registry, reader, simulator = _single_snapshot_registry(
        env, _AckLostStore(store)
    )
    report = simulator.rollback_shadow_snapshot(
        target_snapshot_id=env.snapshot.snapshot_id,
        read_context=_ctx(env),
        at=WITHIN_WINDOW,
    )
    assert report.commit_outcome == "UNCERTAIN"
    assert reader.enabled is False

    # 未执行 store.recover()，仅显式选择 + 已验证证据即可再启用。
    registry.select(env.snapshot.snapshot_id)
    resumed = reader.re_enable(
        at=WITHIN_WINDOW, read_context=_ctx(env), evidence=_evidence(env)
    )
    assert resumed["enabled"] is True
    # 恢复仍是可用的只读操作，但当前实现没有把它绑定为再启用的前提。
    assert store.recover()["active_shadow_state"] == "NOT_RESTORED"


def test_sr_20_production_entry_points_do_not_import_shadow_or_governance_prototypes():
    """SR-20: 生产入口（应用 / 抓取 / 前端）不导入影子或治理原型。"""
    shadow_modules = (
        "shadow_runtime_reader",
        "shadow_activation_simulation",
        "shadow_activation_audit_store",
        "runtime_activation_authorization",
        "mapping_governance",
    )
    targets = (
        os.path.join(REPO_ROOT, "app.py"),
        os.path.join(REPO_ROOT, "utils", "scraper.py"),
        os.path.join(REPO_ROOT, "utils", "daily_loader.py"),
        os.path.join(REPO_ROOT, "start.bat"),
    )
    for path in targets:
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for module in shadow_modules:
            assert module not in source, (path, module)

    api_dir = os.path.join(REPO_ROOT, "frontend", "src", "api")
    for name in sorted(os.listdir(api_dir)):
        if not name.endswith(".ts"):
            continue
        with open(os.path.join(api_dir, name), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("activation", "shadow", "mapping_governance"):
            assert forbidden not in source, (name, forbidden)


def test_sr_21_repository_internal_ledger_and_audit_paths_are_rejected(tmp_path):
    """SR-21: 仓库内治理账本 / 审计存储与工作区逃逸一律拒绝。"""
    with pytest.raises(LedgerPathError):
        GovernanceLedger(os.path.join(REPO_ROOT, "data", "ledger.jsonl"))
    with pytest.raises(LedgerPathError):
        GovernanceLedger("")

    workspace = tmp_path / "sr21"
    workspace.mkdir()
    with pytest.raises(ShadowAuditPathError):
        ShadowActivationAuditStore(str(workspace), filename="../escape.jsonl")
    with pytest.raises(ShadowAuditPathError):
        ShadowActivationAuditStore(os.path.join(REPO_ROOT, "data"))
    with pytest.raises(ShadowAuditPathError):
        ShadowActivationAuditStore("")


def test_sr_02_forged_verified_governance_provenance_is_blocked():
    """SR-02: 自称“独立验证过的账本证据”的来源被策略阻断（静态+既有测试）。"""
    from utils.runtime_mapping_policy import (
        ALLOWED_CONTEXT_EVIDENCE_PROVENANCE,
        BLOCKED_CONTEXT_EVIDENCE_PROVENANCE,
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
        CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
    )

    assert CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER in (
        BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    )
    assert ALLOWED_CONTEXT_EVIDENCE_PROVENANCE == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    }
    # 可执行阻断证据由既有测试提供（全量回归中执行）：
    # tests/test_runtime_mapping_readiness.py::test_ec_12_...
    assert WITHIN_WINDOW and AFTER_EXPIRY and CANONICAL_ID
