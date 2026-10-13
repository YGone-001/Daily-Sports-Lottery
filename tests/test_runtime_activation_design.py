"""
运行期激活架构设计守卫测试（AD-01 .. AD-15）
==========================================
本套件**只验证设计契约本身**，不实现、不激活任何运行期映射。

它断言的是「设计没有被改坏」：
- 激活实现状态仍为 NOT_IMPLEMENTED、激活仍为 NOT_ENABLED；
- 不存在任何激活 / 读取器 / 紧急停用的可执行入口；
- 资格（eligibility）与激活（activation）严格分层、工件与用途域分离；
- 运行期读取器契约与禁令齐备；
- 审计事件目录、回滚模型与紧急停用机制齐备；
- 未来测试要求已登记且**全部未实现**；
- 既有运行期模块、生产数据与七个持久化存储均未被本阶段触碰。

本套件不修改任何既有测试，也不引入任何运行时行为。
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re

import pytest

from utils import fetcher_500, scraper
from utils.mapping_governance import (
    MAPPING_APPROVAL_PURPOSE,
    MAPPING_APPROVAL_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
)
from utils.runtime_activation_design import (
    ACTIVATION_AUDIT_EVENTS,
    ACTIVATION_AUTHORIZATION_ALGORITHM,
    ACTIVATION_AUTHORIZATION_ARTIFACT,
    ACTIVATION_AUTHORIZATION_EXPIRY_MODEL,
    ACTIVATION_AUTHORIZATION_PURPOSE,
    ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS,
    ACTIVATION_AUTHORIZATION_ROLLBACK_MODEL,
    ACTIVATION_AUTHORIZATION_SCHEMA_VERSION,
    ACTIVATION_DESIGN_NON_GOALS,
    ACTIVATION_ENABLE_API,
    ACTIVATION_IMPLEMENTATION_STATUS,
    ACTIVATION_LAYERS,
    ACTIVATION_LAYER_ORDER,
    ACTIVATION_VS_APPROVAL,
    ALLOWED_ACTIVATION_SCOPES,
    AUDIT_COMMON_FIELDS,
    AUDIT_STORE_MODEL,
    CURRENT_RUNTIME_PIPELINE,
    EMERGENCY_DISABLE_API,
    EMERGENCY_DISABLE_MECHANISM,
    FUTURE_ACTIVATION_TEST_REQUIREMENTS,
    FUTURE_RUNTIME_PIPELINE,
    IMPLEMENTED_PIPELINE_STAGES,
    NOT_IMPLEMENTED_PIPELINE_STAGES,
    ROLLBACK_MODEL,
    ROLLBACK_PROHIBITIONS,
    RUNTIME_ACTIVATION,
    RUNTIME_ACTIVATION_DESIGN_VERSION,
    RUNTIME_ACTIVATION_WRITE_AUTHORIZATION,
    RUNTIME_READER_API,
    RUNTIME_READER_CONTRACT,
    RUNTIME_READER_PROHIBITIONS,
    activation_design_status,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
    RUNTIME_READINESS_MANIFEST_KIND,
)
from utils.runtime_mapping_policy import (
    RUNTIME_ELIGIBILITY_RULES,
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)

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

DESIGN_MODULE = os.path.join(REPO_ROOT, "utils", "runtime_activation_design.py")
DESIGN_DOC = os.path.join(REPO_ROOT, "docs", "runtime-activation-architecture.md")


def _design_source() -> str:
    with open(DESIGN_MODULE, encoding="utf-8") as handle:
        return handle.read()


def _design_doc() -> str:
    with open(DESIGN_DOC, encoding="utf-8") as handle:
        return handle.read()


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


# ---------------------------------------------------------------------------
# AD-01
# ---------------------------------------------------------------------------

def test_ad_01_design_contract_declares_no_activation():
    """AD-01: 设计契约自洽，且明确声明「未实现 / 未启用 / 未授权」。"""
    status = activation_design_status()

    assert RUNTIME_ACTIVATION_DESIGN_VERSION == "1.1.0"
    assert ACTIVATION_IMPLEMENTATION_STATUS == "NOT_IMPLEMENTED"
    assert status["activation_implementation_status"] == "NOT_IMPLEMENTED"
    assert RUNTIME_ACTIVATION == "NOT_ENABLED"
    assert RUNTIME_ACTIVATION == RUNTIME_MAPPING_ACTIVATION
    assert RUNTIME_ACTIVATION_WRITE_AUTHORIZATION == "NOT_GRANTED"
    assert RUNTIME_ACTIVATION_WRITE_AUTHORIZATION == (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    )

    assert status["writes_files"] is False
    assert status["creates_bindings"] is False
    assert status["activates_mappings"] is False
    assert status["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


# ---------------------------------------------------------------------------
# AD-02
# ---------------------------------------------------------------------------

def test_ad_02_no_activation_api_exists_anywhere():
    """AD-02: 不存在任何激活 / 读取器 / 紧急停用入口。"""
    assert ACTIVATION_ENABLE_API is None
    assert RUNTIME_READER_API is None
    assert EMERGENCY_DISABLE_API is None

    status = activation_design_status()
    assert status["activation_enable_api"] is None
    assert status["runtime_reader_api"] is None
    assert status["emergency_disable_api"] is None

    # 设计模块**绝不**定义任何「可执行激活」形式的公开可调用名。
    import utils.runtime_activation_design as design

    for name in (
        "apply",
        "activate",
        "enable",
        "execute",
        "authorize",
        "bind",
        "migrate",
        "write",
        "commit",
    ):
        assert not hasattr(design, name), name

    # 模块内只有一个公开**函数**（状态视图）；其余公开名都是数据声明或不可变 dataclass 类型。
    import inspect

    public_functions = sorted(
        name
        for name, value in vars(design).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and getattr(value, "__module__", "") == design.__name__
    )
    assert public_functions == ["activation_design_status"]

    # 公开的 dataclass 类型必须都是 frozen（不可变声明）。
    public_dataclasses = [
        value
        for name, value in vars(design).items()
        if not name.startswith("_")
        and inspect.isclass(value)
        and getattr(value, "__module__", "") == design.__name__
        and dataclasses.is_dataclass(value)
    ]
    assert public_dataclasses
    for cls in public_dataclasses:
        assert cls.__dataclass_params__.frozen is True, cls.__name__


# ---------------------------------------------------------------------------
# AD-03
# ---------------------------------------------------------------------------

def test_ad_03_eligibility_and_activation_are_separate_layers():
    """AD-03: 资格层与激活层严格分层，且资格层绝不下发激活授权。"""
    assert ACTIVATION_LAYER_ORDER == (
        "ELIGIBILITY",
        "ACTIVATION_AUTHORIZATION",
        "RUNTIME_READER",
    )
    layers = {layer.layer_id: layer for layer in ACTIVATION_LAYERS}
    assert set(layers) == set(ACTIVATION_LAYER_ORDER)

    eligibility = layers["ELIGIBILITY"]
    authorization = layers["ACTIVATION_AUTHORIZATION"]
    reader = layers["RUNTIME_READER"]

    # 只有资格层已实现；授权层与读取器层明确未实现。
    assert eligibility.implemented is True
    assert authorization.implemented is False
    assert reader.implemented is False

    # 三者的产物互不相同。
    assert len({eligibility.artifact, authorization.artifact, reader.artifact}) == 3
    assert eligibility.artifact == "RuntimeMappingEligibilityDecision"

    # 资格层的禁令必须显式包含「不得下发激活授权 / 不得激活 / 不得建绑定 / 不得写盘」。
    for prohibition in (
        "GRANT_ACTIVATION_AUTHORIZATION",
        "ACTIVATE_RUNTIME_MAPPING",
        "CREATE_BINDING",
        "MUTATE_DATA",
        "WRITE_FILES",
        "CONSUME_LEDGER_FOR_WRITE",
    ):
        assert prohibition in eligibility.must_not_do, prohibition

    # 授权层不得替代所有者批准，也不得自动激活。
    for prohibition in (
        "SUBSTITUTE_FOR_OWNER_APPROVAL",
        "AUTO_ACTIVATE_ON_ELIGIBLE",
        "MODIFY_CANONICAL_IDS",
        "REWRITE_HISTORICAL_STORES",
    ):
        assert prohibition in authorization.must_not_do, prohibition

    # 每一层都声明了「能做什么」，且不允许为空。
    for layer in ACTIVATION_LAYERS:
        assert layer.may_do and layer.must_not_do and layer.question


# ---------------------------------------------------------------------------
# AD-04
# ---------------------------------------------------------------------------

def test_ad_04_activation_authorization_is_not_an_approval():
    """AD-04: 激活授权工件与所有者批准件分离（用途域分离 + 激活特有绑定项）。"""
    assert ACTIVATION_AUTHORIZATION_ARTIFACT == "RuntimeActivationAuthorization"
    assert ACTIVATION_AUTHORIZATION_ARTIFACT != "approval"
    assert ACTIVATION_AUTHORIZATION_PURPOSE == "RUNTIME_MAPPING_ACTIVATION_AUTHORIZE"
    assert ACTIVATION_AUTHORIZATION_PURPOSE != MAPPING_APPROVAL_PURPOSE
    assert ACTIVATION_AUTHORIZATION_SCHEMA_VERSION == "1.0.0"
    assert ACTIVATION_AUTHORIZATION_SCHEMA_VERSION != MAPPING_APPROVAL_SCHEMA_VERSION
    assert ACTIVATION_AUTHORIZATION_ALGORITHM == "ed25519"

    # 与批准同构的绑定项。
    for field in (
        "proposal_id",
        "proposal_content_fingerprint",
        "scoped_source_identity_key",
        "candidate_canonical_id",
        "dataset_identity",
        "expected_baseline_fingerprint",
        "approval_fingerprint",
    ):
        assert field in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS, field

    # 激活特有的绑定项。
    for field in (
        "activation_id",
        "eligibility_decision_id",
        "eligibility_evaluator_version",
        "policy_version",
        "runtime_snapshot_id",
        "authorized_scope",
        "authorizer",
        "expires_at",
        "revocation_reference",
    ):
        assert field in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS, field

    # 最小权限：激活范围只允许「只读解析参与」。
    assert ALLOWED_ACTIVATION_SCOPES == ("RUNTIME_READ_ONLY_RESOLUTION",)

    # 对照表必须覆盖「问题 / 产物 / 用途 / 有效期 / 效力 / 是否单独充分」等关键差异。
    aspects = {row[0] for row in ACTIVATION_VS_APPROVAL}
    assert {
        "question",
        "artifact",
        "purpose",
        "schema",
        "issuer",
        "lifetime",
        "revocable",
        "effect",
        "sufficient_alone",
    } <= aspects
    for row in ACTIVATION_VS_APPROVAL:
        assert len(row) == 3 and all(str(cell).strip() for cell in row)


# ---------------------------------------------------------------------------
# AD-05
# ---------------------------------------------------------------------------

def test_ad_05_expiration_model_is_complete():
    """AD-05: 有效期模型齐备（必填、上界、时钟权威、过期≠撤销、续期=重签）。"""
    keys = {key for key, _ in ACTIVATION_AUTHORIZATION_EXPIRY_MODEL}
    assert keys == {
        "required",
        "max_lifetime",
        "evaluation",
        "clock_authority",
        "expired_state",
        "renewal",
    }
    for _, value in ACTIVATION_AUTHORIZATION_EXPIRY_MODEL:
        assert value.strip()

    rollback_keys = {key for key, _ in ACTIVATION_AUTHORIZATION_ROLLBACK_MODEL}
    assert rollback_keys == {
        "revocation",
        "revocation_is_immediate",
        "supersession",
        "authorization_is_not_permanent",
    }


# ---------------------------------------------------------------------------
# AD-06
# ---------------------------------------------------------------------------

def test_ad_06_runtime_reader_contract_is_read_only():
    """AD-06: 运行期读取器契约齐备、只读，且结构性不可写。"""
    contract = dict(RUNTIME_READER_CONTRACT)
    assert set(contract) == {
        "input_contract",
        "trust_requirements",
        "snapshot_model",
        "cache_invalidation",
        "baseline_compatibility",
        "failure_behavior",
    }
    for value in contract.values():
        assert value.strip()

    for prohibition in (
        "MUTATE_DATA",
        "REPAIR_IDENTITIES",
        "CREATE_MAPPINGS",
        "WRITE_FILES",
        "CONSUME_UNVERIFIED_CONTEXT",
        "FALL_BACK_TO_SOURCE_ID_MATCHING",
        "SILENTLY_REUSE_STALE_SNAPSHOT",
        "BLOCK_OR_ALTER_REFRESH",
    ):
        assert prohibition in RUNTIME_READER_PROHIBITIONS, prohibition

    # 设计模块源码不含任何写入原语。
    source = _design_source()
    for forbidden in (
        "open(",
        "os.replace",
        "os.remove",
        "os.makedirs",
        "os.unlink",
        "shutil",
        "json.dump",
        "save_json",
        "atomic_write_json",
        "requests",
        "urllib",
        "import os",
    ):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# AD-07
# ---------------------------------------------------------------------------

def test_ad_07_audit_model_is_complete_and_append_only():
    """AD-07: 审计事件目录覆盖必需事件，全部追加式且字段齐备。"""
    event_types = [event.event_type for event in ACTIVATION_AUDIT_EVENTS]
    assert len(set(event_types)) == len(event_types)
    for required in (
        "ELIGIBILITY_EVALUATED",
        "ACTIVATION_REQUESTED",
        "ACTIVATION_AUTHORIZED",
        "ACTIVATION_ENABLED",
        "ACTIVATION_DISABLED",
        "ROLLBACK_EXECUTED",
    ):
        assert required in event_types, required

    for event in ACTIVATION_AUDIT_EVENTS:
        assert event.append_only is True
        assert event.required_fields
        assert event.when.strip()
        assert event.event_type.isupper()

    assert AUDIT_COMMON_FIELDS == (
        "event_id",
        "event_type",
        "schema_version",
        "occurred_at",
        "actor",
        "actor_role",
        "dataset_identity",
        "baseline_fingerprint",
    )

    store = dict(AUDIT_STORE_MODEL)
    assert set(store) == {
        "location",
        "structure",
        "integrity",
        "concurrency",
        "immutability",
        "trust_anchor",
    }
    assert "append-only" in store["structure"]
    # 信任锚诚实性：哈希链不是外部信任锚。
    assert "不是**外部信任锚" in store["integrity"] or "不是外部信任锚" in store["integrity"]
    assert "哈希链本身不是信任锚" in store["trust_anchor"]


# ---------------------------------------------------------------------------
# AD-08
# ---------------------------------------------------------------------------

def test_ad_08_rollback_and_emergency_disable_are_designed():
    """AD-08: 回滚模型与紧急停用机制齐备；停用无需授权、启用必须授权。"""
    rollback = dict(ROLLBACK_MODEL)
    assert set(rollback) == {
        "emergency_disable",
        "previous_runtime_snapshot",
        "audit_preservation",
        "deterministic_recovery",
        "historical_data",
        "fail_closed_default",
    }
    for prohibition in (
        "REWRITE_CANONICAL_IDS",
        "REWRITE_HISTORICAL_STORES",
        "DELETE_AUDIT_EVENTS",
        "REBUILD_SNAPSHOTS",
        "PARTIAL_APPLICATION",
        "SILENT_ROLLBACK",
    ):
        assert prohibition in ROLLBACK_PROHIBITIONS, prohibition

    # 回滚绝不改写历史 Canonical 数据。
    assert "绝不**改写 Canonical ID" in rollback["historical_data"] or (
        "绝不" in rollback["historical_data"]
        and "Canonical ID" in rollback["historical_data"]
    )

    disable = dict(EMERGENCY_DISABLE_MECHANISM)
    assert set(disable) == {
        "mechanism",
        "trigger",
        "latency",
        "audit",
        "asymmetry",
        "no_data_effect",
    }
    # 不对称性：停用不需授权，启用必须授权。
    assert "停用**不需要**授权" in disable["asymmetry"]
    assert "启用**必须**授权" in disable["asymmetry"]


# ---------------------------------------------------------------------------
# AD-09
# ---------------------------------------------------------------------------

def test_ad_09_future_activation_test_requirements_are_declared_but_unimplemented():
    """AD-09: 未来激活测试要求已登记，且**全部**未实现。"""
    ids = [requirement.test_id for requirement in FUTURE_ACTIVATION_TEST_REQUIREMENTS]
    assert len(set(ids)) == len(ids)
    for required in ("AT-01", "AT-02", "AT-03", "AT-04", "AT-05"):
        assert required in ids, required
    assert len(FUTURE_ACTIVATION_TEST_REQUIREMENTS) >= 12

    for requirement in FUTURE_ACTIVATION_TEST_REQUIREMENTS:
        assert requirement.implemented is False
        assert requirement.requirement.strip()
        assert requirement.expected_outcome.strip()

    status = activation_design_status()
    assert status["future_test_requirement_count"] == len(
        FUTURE_ACTIVATION_TEST_REQUIREMENTS
    )
    assert status["implemented_future_test_count"] == 0

    # 这五项必需要求在设计文档中逐条出现。
    doc = _design_doc()
    for required in ("AT-01", "AT-02", "AT-03", "AT-04", "AT-05"):
        assert required in doc, required


# ---------------------------------------------------------------------------
# AD-10
# ---------------------------------------------------------------------------

def test_ad_10_non_goals_are_explicit():
    """AD-10: 非目标显式声明，且覆盖全部关键禁令。"""
    joined = " ".join(ACTIVATION_DESIGN_NON_GOALS)
    for forbidden in (
        "不实现激活授权",
        "不实现运行期读取器",
        "不修改 scraper.refresh()",
        "不在运行期消费治理账本",
        "不创建绑定",
        "不改写 Canonical ID",
        "不迁移源身份",
        "不引入运行期映射数据库",
    ):
        assert forbidden in joined, forbidden


# ---------------------------------------------------------------------------
# AD-11
# ---------------------------------------------------------------------------

def test_ad_11_future_pipeline_keeps_current_authority():
    """AD-11: 未来流水线保留当前全部阶段；未实现阶段与已实现阶段互斥。"""
    assert set(FUTURE_RUNTIME_PIPELINE) >= set(CURRENT_RUNTIME_PIPELINE)
    assert not (set(IMPLEMENTED_PIPELINE_STAGES) & set(NOT_IMPLEMENTED_PIPELINE_STAGES))
    assert NOT_IMPLEMENTED_PIPELINE_STAGES == (
        "ACTIVATION_AUTHORIZATION",
        "RUNTIME_READER",
    )
    # 摄取身份规划与 Canonical 归属在两条流水线中都在（权威未被替换）。
    for stage in ("INGESTION_IDENTITY_PLAN", "CANONICAL_OWNERSHIP"):
        assert stage in CURRENT_RUNTIME_PIPELINE
        assert stage in FUTURE_RUNTIME_PIPELINE
    # 未来流水线是当前流水线的超集，且顺序上新阶段位于归属之前。
    assert FUTURE_RUNTIME_PIPELINE.index("ACTIVATION_AUTHORIZATION") < (
        FUTURE_RUNTIME_PIPELINE.index("CANONICAL_OWNERSHIP")
    )


# ---------------------------------------------------------------------------
# AD-12
# ---------------------------------------------------------------------------

def test_ad_12_no_runtime_module_references_the_activation_design():
    """AD-12: 运行期 / 解析器 / 治理模块都不引用激活设计，refresh 行为未变。"""
    for name in (
        "scraper.py",
        "fetcher_500.py",
        "ingestion_identity.py",
        "match_identity.py",
        "mapping_governance.py",
        "runtime_mapping_policy.py",
        "runtime_mapping_eligibility.py",
        "provider_evidence.py",
        "source_id_migration.py",
    ):
        with open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("runtime_activation_design", "ACTIVATION_AUTHORIZATION"):
            assert forbidden not in source, (name, forbidden)

    with open(os.path.join(REPO_ROOT, "utils", "scraper.py"), encoding="utf-8") as handle:
        scraper_source = handle.read()
    # 刷新路径仍然经由刷新锁 + 摄取身份预检 + Canonical 唯一性证明。
    assert "acquire_refresh_lock" in scraper_source
    assert "validate_ingestion_identity(existing, incoming)" in scraper_source
    assert "validate_canonical_uniqueness(" in scraper_source
    # 刷新路径不消费治理账本、不引用激活层。
    assert "GovernanceLedger" not in scraper_source
    assert "runtime_mapping_eligibility" not in scraper_source


# ---------------------------------------------------------------------------
# AD-13
# ---------------------------------------------------------------------------

def test_ad_13_existing_semantics_are_unchanged():
    """AD-13: 本设计阶段未改动既有资格 / 策略语义与版本。"""
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_EVALUATOR_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION == "1.1.0"
    assert len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert RUNTIME_READINESS_MANIFEST_KIND == "READ_ONLY_RUNTIME_READINESS"
    assert RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED"

    status = activation_design_status()
    assert status["policy_version"] == RUNTIME_MAPPING_POLICY_VERSION
    assert status["eligibility_evaluator_version"] == RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
    assert status["eligibility_decision_schema_version"] == (
        RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION
    )


# ---------------------------------------------------------------------------
# AD-14
# ---------------------------------------------------------------------------

def test_ad_14_design_phase_touches_no_data(tmp_path, isolated_data_dir):
    """AD-14: 设计阶段不写任何文件，也不改动七个持久化存储。"""
    workspace = tmp_path / "ad14"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before_listing = sorted(os.listdir(workspace))

    # 反复读取设计状态不产生任何副作用。
    for _ in range(3):
        activation_design_status()
        dataclasses.asdict(ACTIVATION_LAYERS[0])

    assert sorted(os.listdir(workspace)) == before_listing
    assert (workspace / "sentinel.txt").read_text(encoding="utf-8") == "unchanged"

    # 七个持久化存储（隔离夹具）在设计阶段前后逐字节一致。
    stores = {
        "daily_matches.json": {"meta": {}, "matches": []},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": {"version": 1, "snapshots": {}},
        "odds_snapshots.json": {"version": 1, "snapshots": {}},
        "settlements.json": {"version": 1, "settlements": {}},
        "evaluation_rows.json": {"version": 1, "rows": {}},
    }
    from utils.daily_loader import save_json

    for name, payload in stores.items():
        save_json(name, payload)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    activation_design_status()

    assert _hash_seven_stores(isolated_data_dir) == before
    # 设计模块从未被运行期导入。
    assert "runtime_activation_design" not in open(
        os.path.join(REPO_ROOT, "utils", "scraper.py"), encoding="utf-8"
    ).read()
    assert scraper is not None and fetcher_500 is not None


# ---------------------------------------------------------------------------
# AD-15
# ---------------------------------------------------------------------------

def test_ad_15_design_document_covers_all_required_areas():
    """AD-15: 设计文档覆盖全部必需设计域，并明确声明未实现。"""
    doc = _design_doc()
    for heading in (
        "Runtime activation boundary",
        "Runtime reader contract",
        "Activation authorization model",
        "Audit model",
        "Rollback design",
        "Testing design",
        "Non-goals",
    ):
        assert heading in doc, heading

    # 明确声明未实现 / 未启用 / 未授权。
    assert "NOT_IMPLEMENTED" in doc
    assert "NOT_ENABLED" in doc
    assert "NOT_GRANTED" in doc
    # 资格与激活分离必须被显式写出。
    assert "Eligibility and activation are separate layers" in doc
    # 停用/启用不对称性必须被显式写出。
    assert "Disabling requires no authorization; enabling always does" in doc
    # 回滚不得改写历史。
    assert re.search(r"never\*\* rewrites Canonical IDs", doc)
    # 引用设计契约模块与守卫测试。
    assert "utils/runtime_activation_design.py" in doc
    assert "tests/test_runtime_activation_design.py" in doc
