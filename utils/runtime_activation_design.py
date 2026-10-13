"""
受控运行期映射激活架构 —— **纯声明式设计契约**
================================================
本模块**只描述设计**，不实现激活。它把「未来的受控运行期映射激活」需要遵守的
边界、契约与模型，以**纯数据 + 导入时自洽性断言**的形式固定下来，使设计本身
可被机器校验、无法被静默漂移。

本模块的硬性性质
----------------
- **零运行时行为**：不读写文件、不访问网络、不修改任何状态、不被运行期引用。
- **无可执行入口**：不导出任何 apply / activate / enable / execute / authorize /
  bind / migrate 形式的函数；激活授权只被**声明**，不被实现。
- **不改变既有判定**：资格评估（`utils.runtime_mapping_eligibility`）与策略注册表
  （`utils.runtime_mapping_policy`）的版本与语义**完全不变**。

设计分层（本阶段的关键结论）
----------------------------
``ELIGIBILITY``（已实现）
    「这份映射**是否会被允许**？」—— 只读判定，产出
    `RuntimeMappingEligibilityDecision`。它**不是**授权，也**不**产生任何效果。

``ACTIVATION_AUTHORIZATION``（**未实现**，仅设计）
    「这份映射**是否可以影响运行期**？」—— 一个**独立于所有者批准**的授权工件，
    把某个具体的 ELIGIBLE 判定绑定到受控范围、运行期快照与**有效期**。

``RUNTIME_READER``（**未实现**，仅设计）
    「运行期如何**只读地**消费一个已激活映射？」—— 严格只读的读取器契约。

资格（eligibility）与激活（activation）**必须保持分离**：
- 资格回答「正确吗 / 会被允许吗」；激活回答「可以生效吗」。
- 一条 ELIGIBLE 判定**绝不**自动等于激活授权；它只是激活授权的**前置条件**之一。
- 一条曾经 ELIGIBLE 的判定**不是**永久授权；基线或治理变化后必须重评。
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.mapping_governance import (
    MAPPING_APPROVAL_PURPOSE,
    MAPPING_APPROVAL_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
)
from utils.runtime_mapping_policy import (
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)

#: 激活架构设计契约版本。任何设计语义变化都必须提升该版本。
#:
#: ``1.1.0``：授权工件的字段名与原型实现对齐（``authorization_schema_version``），
#: 并补齐资格判定指纹与密码学三件套（``signature_algorithm`` /
#: ``signer_public_key_id`` / ``signature``）。
RUNTIME_ACTIVATION_DESIGN_VERSION = "1.1.0"

#: 激活实现状态：本阶段**未实现**。不要把该值改成 IMPLEMENTED。
ACTIVATION_IMPLEMENTATION_STATUS = "NOT_IMPLEMENTED"

#: 运行期映射激活状态：**未启用**（与策略注册表同值，导入时断言一致）。
RUNTIME_ACTIVATION = RUNTIME_MAPPING_ACTIVATION

#: 生产映射写入授权：**未授权**。
RUNTIME_ACTIVATION_WRITE_AUTHORIZATION = PRODUCTION_MAPPING_WRITE_AUTHORIZATION

#: 激活授权入口：刻意**不存在**。将来若引入，必须同时引入有效期、撤销与审计。
ACTIVATION_ENABLE_API = None

#: 运行期读取器入口：刻意**不存在**。
RUNTIME_READER_API = None

#: 紧急停用入口：刻意**不存在**；本阶段的「紧急停用」是**保持常量 NOT_ENABLED**。
EMERGENCY_DISABLE_API = None


# =============================================================================
# 1. 运行期激活边界（流水线）
# =============================================================================

#: 当前**生产运行期**流水线（唯一权威，未变）。
CURRENT_RUNTIME_PIPELINE: tuple[str, ...] = (
    "SOURCE_OBSERVATION",
    "IDENTITY_VALIDATION",
    "INGESTION_IDENTITY_PLAN",
    "CANONICAL_OWNERSHIP",
)

#: 未来受控流水线。
#:
#: 刻意**保留**当前流水线的全部阶段：摄取身份规划与 Canonical 归属**仍然权威**，
#: 受控映射层是**附加**的闸门，绝不是替代品。后两段只在本设计中定义、**未实现**。
FUTURE_RUNTIME_PIPELINE: tuple[str, ...] = (
    "SOURCE_OBSERVATION",
    "IDENTITY_VALIDATION",
    "INGESTION_IDENTITY_PLAN",
    "MAPPING_PROPOSAL",
    "OWNER_APPROVAL",
    "RUNTIME_ELIGIBILITY",
    "ACTIVATION_AUTHORIZATION",
    "RUNTIME_READER",
    "CANONICAL_OWNERSHIP",
)

#: 今天**已经存在**的流水线阶段。
IMPLEMENTED_PIPELINE_STAGES: tuple[str, ...] = (
    "SOURCE_OBSERVATION",
    "IDENTITY_VALIDATION",
    "MAPPING_PROPOSAL",
    "OWNER_APPROVAL",
    "RUNTIME_ELIGIBILITY",
)

#: **尚未实现**、只在本设计中定义的阶段。
NOT_IMPLEMENTED_PIPELINE_STAGES: tuple[str, ...] = (
    "ACTIVATION_AUTHORIZATION",
    "RUNTIME_READER",
)


@dataclass(frozen=True)
class ActivationLayer:
    """一个流水线层的声明式职责与禁令。"""

    layer_id: str
    question: str
    implemented: bool
    artifact: str
    authority: str
    may_do: tuple[str, ...]
    must_not_do: tuple[str, ...]


#: 三个层的职责边界。资格层已实现；激活授权层与读取器层只被声明。
ACTIVATION_LAYERS: tuple[ActivationLayer, ...] = (
    ActivationLayer(
        layer_id="ELIGIBILITY",
        question="这份映射是否会被允许？（would be allowed）",
        implemented=True,
        artifact="RuntimeMappingEligibilityDecision",
        authority="utils.runtime_mapping_eligibility.evaluate_runtime_mapping_eligibility",
        may_do=(
            "READ_OBSERVATION",
            "READ_PROPOSAL",
            "READ_APPROVAL",
            "VERIFY_ED25519_SIGNATURE",
            "PRODUCE_DECISION",
        ),
        must_not_do=(
            "ACTIVATE_RUNTIME_MAPPING",
            "CREATE_BINDING",
            "MUTATE_DATA",
            "WRITE_FILES",
            "CONSUME_LEDGER_FOR_WRITE",
            "GRANT_ACTIVATION_AUTHORIZATION",
        ),
    ),
    ActivationLayer(
        layer_id="ACTIVATION_AUTHORIZATION",
        question="这份映射是否可以影响运行期？（may it affect runtime）",
        implemented=False,
        artifact="RuntimeActivationAuthorization",
        authority="NOT_IMPLEMENTED",
        may_do=(
            "READ_ELIGIBILITY_DECISION",
            "READ_OWNER_APPROVAL",
            "BIND_ACTIVATION_SCOPE",
            "BIND_ACTIVATION_EXPIRY",
            "PRODUCE_ACTIVATION_AUTHORIZATION",
        ),
        must_not_do=(
            "SUBSTITUTE_FOR_OWNER_APPROVAL",
            "MODIFY_CANONICAL_IDS",
            "REWRITE_HISTORICAL_STORES",
            "MIGRATE_SOURCE_IDENTITIES",
            "AUTO_ACTIVATE_ON_ELIGIBLE",
            "WRITE_PRODUCTION_DATA",
        ),
    ),
    ActivationLayer(
        layer_id="RUNTIME_READER",
        question="运行期如何只读地消费一个已激活映射？",
        implemented=False,
        artifact="RuntimeMappingReadView",
        authority="NOT_IMPLEMENTED",
        may_do=(
            "READ_ACTIVATION_AUTHORIZATION",
            "READ_ELIGIBILITY_DECISION",
            "RESOLVE_ACTIVATED_MAPPING",
            "REPORT_INELIGIBLE",
        ),
        must_not_do=(
            "MUTATE_DATA",
            "REPAIR_IDENTITIES",
            "CREATE_MAPPINGS",
            "WRITE_FILES",
            "CONSUME_UNVERIFIED_CONTEXT",
            "FALL_BACK_TO_SOURCE_ID_MATCHING",
        ),
    ),
)

#: 层标识的顺序（资格 -> 激活授权 -> 读取器）。
ACTIVATION_LAYER_ORDER: tuple[str, ...] = tuple(
    layer.layer_id for layer in ACTIVATION_LAYERS
)


# =============================================================================
# 2. 运行期读取器契约（仅设计）
# =============================================================================

#: 读取器契约的六个必答维度（键 -> 设计结论）。
RUNTIME_READER_CONTRACT: tuple[tuple[str, str], ...] = (
    (
        "input_contract",
        "输入 = (当前 SourceObservation, 运行期快照标识, 当前 Canonical 基线指纹)。"
        "读取器**不**接受裸源字符串、**不**接受调用方自述的治理事实。",
    ),
    (
        "trust_requirements",
        "只消费：当前契约的作用域身份键、已验证的 Ed25519 所有者批准、"
        "未过期且未撤销的激活授权、以及可复核的资格判定。"
        "任何一项缺失或不可复核即拒绝服务（fail closed），绝不降级为源 ID 字符串匹配。",
    ),
    (
        "snapshot_model",
        "读取器基于一个**不可变的运行期快照**工作：快照 = (授权集合, 基线指纹, "
        "策略版本, 评估器版本, 数据集身份)。快照一经发布即只读；变更必须发布新快照，"
        "绝不原地修改旧快照。",
    ),
    (
        "cache_invalidation",
        "快照缓存键 = 快照指纹。任一触发条件变化（新授权、授权撤销/过期、"
        "Canonical 基线变化、策略或评估器版本变化、紧急停用）都必须使缓存失效并重新装载；"
        "禁止基于时间猜测有效性。",
    ),
    (
        "baseline_compatibility",
        "授权绑定的基线指纹必须等于当前运行期基线指纹。不相等即视为**不兼容**："
        "该授权不得被消费，必须重新走资格评估 + 重新授权。",
    ),
    (
        "failure_behavior",
        "任何失败（快照缺失、签名不可验、授权过期/撤销、基线不兼容、歧义未决）"
        "一律**拒绝服务并如实报告**，绝不回退到未授权的启发式匹配，"
        "也绝不静默使用上一次成功的快照。",
    ),
)

#: 读取器**绝不允许**做的事（设计级禁令）。
RUNTIME_READER_PROHIBITIONS: tuple[str, ...] = (
    "MUTATE_DATA",
    "REPAIR_IDENTITIES",
    "CREATE_MAPPINGS",
    "WRITE_FILES",
    "CONSUME_UNVERIFIED_CONTEXT",
    "FALL_BACK_TO_SOURCE_ID_MATCHING",
    "SILENTLY_REUSE_STALE_SNAPSHOT",
    "BLOCK_OR_ALTER_REFRESH",
)


# =============================================================================
# 3. 激活授权模型（仅设计）
# =============================================================================

#: 激活授权工件的名称（与所有者批准工件**不同**）。
ACTIVATION_AUTHORIZATION_ARTIFACT = "RuntimeActivationAuthorization"

#: 激活授权的域分离用途串。刻意与映射批准用途**不同**：
#: 生产修复授权件与映射批准件都无法被重放为激活授权，反之亦然。
ACTIVATION_AUTHORIZATION_PURPOSE = "RUNTIME_MAPPING_ACTIVATION_AUTHORIZE"

#: 激活授权 schema 版本（设计期声明；本阶段无任何工件被签发）。
ACTIVATION_AUTHORIZATION_SCHEMA_VERSION = "1.0.0"

#: 激活授权签名算法（沿用既有 Ed25519 原语，不自实现密码学）。
ACTIVATION_AUTHORIZATION_ALGORITHM = "ed25519"

#: 激活授权必须绑定的字段。
#:
#: 前半部分与所有者批准**同构**（保证「同一份证据」被绑定），
#: 后半部分是激活**特有**的（有效期、范围、运行期快照、所依据的资格判定）。
ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS: tuple[str, ...] = (
    # 与批准同构的绑定项
    "proposal_id",
    "proposal_content_fingerprint",
    "scoped_source_identity_key",
    "candidate_canonical_id",
    "dataset_identity",
    "expected_baseline_fingerprint",
    "approval_fingerprint",
    # 激活特有项
    "authorization_schema_version",
    "purpose",
    "activation_id",
    "eligibility_decision_id",
    "eligibility_decision_fingerprint",
    "eligibility_evaluator_version",
    "policy_version",
    "runtime_snapshot_id",
    "authorized_scope",
    "operator",
    "authorizer",
    "authorized_at",
    "expires_at",
    "revocation_reference",
    "reason",
    # 密码学三件套
    "signature_algorithm",
    "signer_public_key_id",
    "signature",
)

#: 允许的激活范围（最小权限：只允许「只读解析参与」，不允许任何写入语义）。
ALLOWED_ACTIVATION_SCOPES: tuple[str, ...] = ("RUNTIME_READ_ONLY_RESOLUTION",)

#: 激活授权与所有者批准的区别（设计对照）。
ACTIVATION_VS_APPROVAL: tuple[tuple[str, str, str], ...] = (
    ("question", "这份映射**是否正确**？", "这份映射**是否可以影响运行期**？"),
    ("artifact", "Ed25519 映射批准件", ACTIVATION_AUTHORIZATION_ARTIFACT),
    ("purpose", MAPPING_APPROVAL_PURPOSE, ACTIVATION_AUTHORIZATION_PURPOSE),
    ("schema", MAPPING_APPROVAL_SCHEMA_VERSION, ACTIVATION_AUTHORIZATION_SCHEMA_VERSION),
    ("issuer", "所有者（持有仓库外私钥）", "授权人（可与所有者相同，但必须独立签发）"),
    ("lifetime", "无固有有效期；基线变化即失效", "**有显式有效期**，到期即失效"),
    ("revocable", "通过撤销 / 取代决策", "通过撤销引用 + 有效期双重约束"),
    ("effect", "无（仅表示映射正确）", "允许读取器在受控范围内只读消费"),
    ("sufficient_alone", "否：不构成激活依据", "否：仍要求资格判定与基线兼容"),
)

#: 有效期模型（设计）。
ACTIVATION_AUTHORIZATION_EXPIRY_MODEL: tuple[tuple[str, str], ...] = (
    ("required", "有效期必填；无有效期的激活授权视为无效。"),
    ("max_lifetime", "由所有者策略给定上界；超过上界一律拒绝（不得由签发方自行放宽）。"),
    ("evaluation", "每次运行期读取都重新比较 `expires_at` 与运行期时钟；过期即拒绝。"),
    ("clock_authority", "以运行期受控时钟为准，绝不信任工件自述的「当前时间」。"),
    ("expired_state", "过期不等于撤销：过期授权保留在审计中，但**永不**再被消费。"),
    ("renewal", "续期 = 重新签发一份新授权（新的 activation_id），绝不原地延长旧件。"),
)

#: 撤销与回滚模型（设计）。
ACTIVATION_AUTHORIZATION_ROLLBACK_MODEL: tuple[tuple[str, str], ...] = (
    ("revocation", "撤销通过一份新的、可审计的撤销记录表达（引用 activation_id）。"),
    ("revocation_is_immediate", "撤销在下一个快照发布时立即生效；不等待有效期。"),
    ("supersession", "同一映射的新授权取代旧授权；旧件保留在审计中，不再活动。"),
    ("authorization_is_not_permanent", "激活授权**永不**表示永久授权：有效期 + 基线兼容是硬约束。"),
)


# =============================================================================
# 4. 审计模型（仅设计）
# =============================================================================

@dataclass(frozen=True)
class ActivationAuditEvent:
    """一类激活审计事件的声明式定义。"""

    event_type: str
    when: str
    required_fields: tuple[str, ...]
    append_only: bool = True


#: 所有审计事件共有的字段。
AUDIT_COMMON_FIELDS: tuple[str, ...] = (
    "event_id",
    "event_type",
    "schema_version",
    "occurred_at",
    "actor",
    "actor_role",
    "dataset_identity",
    "baseline_fingerprint",
)

#: 激活审计事件目录（**设计**；本阶段不产生任何事件）。
ACTIVATION_AUDIT_EVENTS: tuple[ActivationAuditEvent, ...] = (
    ActivationAuditEvent(
        event_type="ELIGIBILITY_EVALUATED",
        when="每次运行期资格判定之后（含 NOT_ELIGIBLE）。",
        required_fields=(
            "decision_id",
            "proposal_id",
            "eligibility_result",
            "rejection_codes",
            "evaluator_version",
            "policy_version",
            "evidence_provenance",
        ),
    ),
    ActivationAuditEvent(
        event_type="ACTIVATION_REQUESTED",
        when="有人请求把某个 ELIGIBLE 判定转为激活授权。",
        required_fields=("activation_id", "decision_id", "requested_scope", "operator"),
    ),
    ActivationAuditEvent(
        event_type="ACTIVATION_AUTHORIZED",
        when="授权人签发了激活授权（本阶段不可达）。",
        required_fields=(
            "activation_id",
            "decision_id",
            "authorization_fingerprint",
            "authorized_scope",
            "expires_at",
            "authorizer",
        ),
    ),
    ActivationAuditEvent(
        event_type="ACTIVATION_ENABLED",
        when="激活授权被装载进运行期快照（本阶段不可达）。",
        required_fields=("activation_id", "runtime_snapshot_id", "activated_at"),
    ),
    ActivationAuditEvent(
        event_type="ACTIVATION_DISABLED",
        when="某个激活授权被停用（含紧急停用）。",
        required_fields=("activation_id", "runtime_snapshot_id", "reason"),
    ),
    ActivationAuditEvent(
        event_type="ROLLBACK_EXECUTED",
        when="回滚到上一个运行期快照。",
        required_fields=(
            "from_runtime_snapshot_id",
            "to_runtime_snapshot_id",
            "trigger",
            "deterministic_recovery_fingerprint",
        ),
    ),
    ActivationAuditEvent(
        event_type="ACTIVATION_AUTHORIZATION_EXPIRED",
        when="授权在读取时被判定为已过期。",
        required_fields=("activation_id", "expires_at"),
    ),
    ActivationAuditEvent(
        event_type="RUNTIME_READ_REJECTED",
        when="读取器拒绝服务（授权缺失/过期/撤销、基线不兼容、歧义未决）。",
        required_fields=("activation_id", "rejection_reason"),
    ),
)

#: 审计存储模型（设计）。刻意与治理账本同构：隔离、追加、哈希链、仓库外。
AUDIT_STORE_MODEL: tuple[tuple[str, str], ...] = (
    ("location", "仓库**之外**的隔离工作区；绝不写入 data/。"),
    ("structure", "append-only JSONL；条目一旦写入永不删除、永不原地修改。"),
    ("integrity", "哈希链 + 前驱绑定；只证明内部一致性，**不是**外部信任锚。"),
    ("concurrency", "独立的跨进程 / 跨线程锁，与刷新锁无关。"),
    ("immutability", "审计不可变性由「追加 + 哈希链 + 保留原始签名工件」共同保证。"),
    ("trust_anchor", "哈希链本身不是信任锚；独立可信签名 / 检查点才是。"),
)


# =============================================================================
# 5. 回滚与紧急停用（仅设计）
# =============================================================================

#: 回滚模型（设计）。任何回滚都**不得**改写历史 Canonical 数据。
ROLLBACK_MODEL: tuple[tuple[str, str], ...] = (
    (
        "emergency_disable",
        "紧急停用 = 把激活状态常量置回 NOT_ENABLED 并发布一个空授权集合的新快照；"
        "该动作必须是**单步、立即、可审计**的，且不依赖任何外部服务。",
    ),
    (
        "previous_runtime_snapshot",
        "每个运行期快照都保留其完整指纹与内容；回滚 = 指向**上一个已验证快照**，"
        "绝不重建、绝不原地修改快照。",
    ),
    (
        "audit_preservation",
        "回滚只追加审计事件（ACTIVATION_DISABLED / ROLLBACK_EXECUTED），"
        "**绝不**删除或改写任何既有审计条目。",
    ),
    (
        "deterministic_recovery",
        "给定 (目标快照指纹, 数据集身份, 基线指纹)，回滚结果必须唯一确定；"
        "回滚后必须重算并比对确定性恢复指纹，不一致即判回滚失败并 fail closed。",
    ),
    (
        "historical_data",
        "回滚**绝不**改写 Canonical ID、快照、结算、评估样本或源别名 —— "
        "运行期激活只影响「解析时是否消费某个映射」，不影响任何持久化历史。",
    ),
    (
        "fail_closed_default",
        "回滚路径本身失败时，系统必须停在**未激活**状态，绝不部分应用。",
    ),
)

#: 紧急停用机制（设计）。
EMERGENCY_DISABLE_MECHANISM: tuple[tuple[str, str], ...] = (
    ("mechanism", "声明式激活状态常量（本阶段为 RUNTIME_MAPPING_ACTIVATION = NOT_ENABLED）。"),
    ("trigger", "任何人（运维 / 所有者 / 事件响应）均可触发；不需要预先授权即可**停用**。"),
    ("latency", "下一个快照发布即生效；缓存以快照指纹为键，因此停用必然使缓存失效。"),
    ("audit", "停用必须记录 ACTIVATION_DISABLED（含 reason），并保留被停用的授权工件。"),
    ("asymmetry", "停用**不需要**授权，启用**必须**授权 —— 刻意的不对称设计。"),
    ("no_data_effect", "停用不触碰任何持久化数据；它只改变运行期读取器的输入集合。"),
)

#: 回滚**绝不允许**做的事。
ROLLBACK_PROHIBITIONS: tuple[str, ...] = (
    "REWRITE_CANONICAL_IDS",
    "REWRITE_HISTORICAL_STORES",
    "DELETE_AUDIT_EVENTS",
    "REBUILD_SNAPSHOTS",
    "PARTIAL_APPLICATION",
    "SILENT_ROLLBACK",
)


# =============================================================================
# 6. 未来激活测试要求（仅要求，本阶段**不**实现）
# =============================================================================

@dataclass(frozen=True)
class FutureActivationTestRequirement:
    """一条**未来**必须实现的激活测试要求（本阶段全部为未实现）。"""

    test_id: str
    requirement: str
    expected_outcome: str
    implemented: bool = False


#: 未来激活测试要求清单。``implemented`` 在本阶段一律为 False。
FUTURE_ACTIVATION_TEST_REQUIREMENTS: tuple[FutureActivationTestRequirement, ...] = (
    FutureActivationTestRequirement(
        test_id="AT-01",
        requirement="激活必须要求授权：仅有 ELIGIBLE 判定不足以启用映射。",
        expected_outcome="无激活授权时启用被拒绝（fail closed）。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-02",
        requirement="过期授权被拒绝：expires_at 早于运行期时钟的授权不得被消费。",
        expected_outcome="拒绝服务并记录 ACTIVATION_AUTHORIZATION_EXPIRED。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-03",
        requirement="回滚确定性：相同 (目标快照, 数据集, 基线) 得到唯一结果。",
        expected_outcome="恢复指纹可复现；不一致即判回滚失败。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-04",
        requirement="未授权的运行期读取被拒绝：无授权/已撤销/基线不兼容一律拒绝。",
        expected_outcome="拒绝服务并记录 RUNTIME_READ_REJECTED，绝不降级匹配。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-05",
        requirement="审计轨迹不可变：既有审计条目不可删除、不可改写。",
        expected_outcome="任何删除/改写尝试被检出（哈希链断裂）并 fail closed。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-06",
        requirement="撤销立即生效：撤销后下一个快照即不得再消费该授权。",
        expected_outcome="撤销后的读取被拒绝。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-07",
        requirement="基线变化使授权失效：基线指纹变化后必须重新评估并重新授权。",
        expected_outcome="基线不兼容的授权被拒绝。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-08",
        requirement="紧急停用立即生效且不需授权。",
        expected_outcome="停用后所有读取被拒绝，且审计记录 ACTIVATION_DISABLED。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-09",
        requirement="读取器永不修改数据：只读契约必须可被结构性证明。",
        expected_outcome="读取路径无任何写入原语。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-10",
        requirement="用途域分离：映射批准件、生产修复授权件与激活授权件互不可重放。",
        expected_outcome="三者互相重放均被拒绝。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-11",
        requirement="刷新路径隔离：激活状态绝不阻断或改变 refresh() 行为。",
        expected_outcome="激活开/关都不改变 refresh() 的输出与持久化行为。",
    ),
    FutureActivationTestRequirement(
        test_id="AT-12",
        requirement="七存储完整性：激活、停用与回滚都不改动七个持久化存储。",
        expected_outcome="逐字节不变。",
    ),
)


# =============================================================================
# 7. 非目标
# =============================================================================

#: 本设计明确**不**包含的内容。
ACTIVATION_DESIGN_NON_GOALS: tuple[str, ...] = (
    "不实现激活授权签发、装载或消费。",
    "不实现运行期读取器。",
    "不实现紧急停用或回滚的执行路径。",
    "不修改 scraper.refresh() 行为。",
    "不在运行期消费治理账本。",
    "不创建绑定、不改写 Canonical ID、不改写任何历史存储。",
    "不迁移源身份、不改动源解析器 ID 格式。",
    "不引入运行期映射数据库、管理界面或新的生产写入路径。",
    "不改变预测模型、结算逻辑或 Elo 数学。",
)


# =============================================================================
# 只读状态视图
# =============================================================================

def activation_design_status() -> dict:
    """激活架构设计的只读状态视图（供审计与报告）。"""
    return {
        "runtime_activation_design_version": RUNTIME_ACTIVATION_DESIGN_VERSION,
        "activation_implementation_status": ACTIVATION_IMPLEMENTATION_STATUS,
        "runtime_mapping_activation": RUNTIME_ACTIVATION,
        "production_mapping_write_authorization": RUNTIME_ACTIVATION_WRITE_AUTHORIZATION,
        "activation_enable_api": ACTIVATION_ENABLE_API,
        "runtime_reader_api": RUNTIME_READER_API,
        "emergency_disable_api": EMERGENCY_DISABLE_API,
        "current_runtime_pipeline": list(CURRENT_RUNTIME_PIPELINE),
        "future_runtime_pipeline": list(FUTURE_RUNTIME_PIPELINE),
        "implemented_pipeline_stages": list(IMPLEMENTED_PIPELINE_STAGES),
        "not_implemented_pipeline_stages": list(NOT_IMPLEMENTED_PIPELINE_STAGES),
        "activation_layer_order": list(ACTIVATION_LAYER_ORDER),
        "eligibility_evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "eligibility_decision_schema_version": (
            RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION
        ),
        "policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "approval_purpose": MAPPING_APPROVAL_PURPOSE,
        "activation_authorization_purpose": ACTIVATION_AUTHORIZATION_PURPOSE,
        "activation_authorization_artifact": ACTIVATION_AUTHORIZATION_ARTIFACT,
        "allowed_activation_scopes": list(ALLOWED_ACTIVATION_SCOPES),
        "audit_event_types": [event.event_type for event in ACTIVATION_AUDIT_EVENTS],
        "future_test_requirement_count": len(FUTURE_ACTIVATION_TEST_REQUIREMENTS),
        "implemented_future_test_count": sum(
            1 for requirement in FUTURE_ACTIVATION_TEST_REQUIREMENTS if requirement.implemented
        ),
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "writes_files": False,
        "creates_bindings": False,
        "activates_mappings": False,
    }


# =============================================================================
# 导入时自洽性断言（fail closed）
# =============================================================================

#: 本模块**绝不**允许出现的公开可调用名（任何「可执行激活」暗示）。
_FORBIDDEN_PUBLIC_CALLABLES: tuple[str, ...] = (
    "apply",
    "activate",
    "enable",
    "execute",
    "authorize",
    "bind",
    "migrate",
    "write",
    "commit",
)


def _assert_design_is_coherent() -> None:
    """
    导入时断言设计契约自洽（fail closed）。

    任何一条不成立都说明设计被改坏了 —— 与其静默漂移，不如在导入期就失败。
    """
    if ACTIVATION_IMPLEMENTATION_STATUS != "NOT_IMPLEMENTED":
        raise AssertionError(
            "activation design: this module declares a design contract only; the "
            "implementation status must remain NOT_IMPLEMENTED"
        )
    if RUNTIME_ACTIVATION != RUNTIME_MAPPING_ACTIVATION:
        raise AssertionError(
            "activation design: the declared activation state must equal the policy "
            "registry's state"
        )
    if ACTIVATION_ENABLE_API is not None or RUNTIME_READER_API is not None:
        raise AssertionError(
            "activation design: no activation or runtime-reader API may exist in this phase"
        )

    # 资格与激活必须分层且不可合并。
    if ACTIVATION_LAYER_ORDER != (
        "ELIGIBILITY",
        "ACTIVATION_AUTHORIZATION",
        "RUNTIME_READER",
    ):
        raise AssertionError("activation design: layer order must remain explicit")
    for layer in ACTIVATION_LAYERS:
        if not layer.must_not_do:
            raise AssertionError(f"activation design: layer {layer.layer_id} has no prohibitions")
    eligibility = ACTIVATION_LAYERS[0]
    if "GRANT_ACTIVATION_AUTHORIZATION" not in eligibility.must_not_do:
        raise AssertionError(
            "activation design: the eligibility layer must never grant activation"
        )
    if ACTIVATION_LAYERS[1].implemented or ACTIVATION_LAYERS[2].implemented:
        raise AssertionError(
            "activation design: the authorization and reader layers are not implemented"
        )

    # 激活授权必须与所有者批准**分离**。
    if ACTIVATION_AUTHORIZATION_PURPOSE == MAPPING_APPROVAL_PURPOSE:
        raise AssertionError(
            "activation design: the activation purpose must be domain-separated from the "
            "mapping approval purpose"
        )
    if ACTIVATION_AUTHORIZATION_ARTIFACT == "approval":
        raise AssertionError("activation design: the activation artifact must be distinct")
    for field in (
        "eligibility_decision_id",
        "eligibility_decision_fingerprint",
        "expires_at",
        "authorized_scope",
        "runtime_snapshot_id",
        "authorizer",
        "signature_algorithm",
        "signer_public_key_id",
        "signature",
    ):
        if field not in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS:
            raise AssertionError(
                f"activation design: activation-specific field {field!r} is required"
            )
    if "APPROVED" in ALLOWED_ACTIVATION_SCOPES:
        raise AssertionError("activation design: activation scopes must be read-only")

    # 读取器契约必须完整且包含禁令。
    contract_keys = {key for key, _ in RUNTIME_READER_CONTRACT}
    if contract_keys != {
        "input_contract",
        "trust_requirements",
        "snapshot_model",
        "cache_invalidation",
        "baseline_compatibility",
        "failure_behavior",
    }:
        raise AssertionError("activation design: the runtime reader contract is incomplete")
    for prohibition in (
        "MUTATE_DATA",
        "REPAIR_IDENTITIES",
        "CREATE_MAPPINGS",
    ):
        if prohibition not in RUNTIME_READER_PROHIBITIONS:
            raise AssertionError(
                f"activation design: reader prohibition {prohibition!r} is required"
            )

    # 审计事件目录必须覆盖必需事件且全部追加式。
    event_types = [event.event_type for event in ACTIVATION_AUDIT_EVENTS]
    if len(set(event_types)) != len(event_types):
        raise AssertionError("activation design: duplicate audit event types")
    for required in (
        "ELIGIBILITY_EVALUATED",
        "ACTIVATION_REQUESTED",
        "ACTIVATION_AUTHORIZED",
        "ACTIVATION_ENABLED",
        "ACTIVATION_DISABLED",
        "ROLLBACK_EXECUTED",
    ):
        if required not in event_types:
            raise AssertionError(f"activation design: audit event {required!r} is required")
    for event in ACTIVATION_AUDIT_EVENTS:
        if not event.append_only or not event.required_fields:
            raise AssertionError(
                f"activation design: audit event {event.event_type!r} must be append-only "
                "with declared required fields"
            )

    # 回滚模型必须齐备且禁止改写历史。
    rollback_keys = {key for key, _ in ROLLBACK_MODEL}
    if rollback_keys != {
        "emergency_disable",
        "previous_runtime_snapshot",
        "audit_preservation",
        "deterministic_recovery",
        "historical_data",
        "fail_closed_default",
    }:
        raise AssertionError("activation design: the rollback model is incomplete")
    for prohibition in ("REWRITE_CANONICAL_IDS", "REWRITE_HISTORICAL_STORES", "DELETE_AUDIT_EVENTS"):
        if prohibition not in ROLLBACK_PROHIBITIONS:
            raise AssertionError(
                f"activation design: rollback prohibition {prohibition!r} is required"
            )

    # 未来测试要求必须全部**未实现**，且覆盖五项必需项。
    if any(r.implemented for r in FUTURE_ACTIVATION_TEST_REQUIREMENTS):
        raise AssertionError(
            "activation design: no future activation test may be implemented in this phase"
        )
    for required_id in ("AT-01", "AT-02", "AT-03", "AT-04", "AT-05"):
        if required_id not in {r.test_id for r in FUTURE_ACTIVATION_TEST_REQUIREMENTS}:
            raise AssertionError(
                f"activation design: future test requirement {required_id!r} is required"
            )

    # 尚未实现的阶段不得出现在已实现清单里。
    overlap = set(IMPLEMENTED_PIPELINE_STAGES) & set(NOT_IMPLEMENTED_PIPELINE_STAGES)
    if overlap:
        raise AssertionError(
            f"activation design: stages cannot be both implemented and planned: {sorted(overlap)}"
        )
    if not set(FUTURE_RUNTIME_PIPELINE) >= set(CURRENT_RUNTIME_PIPELINE):
        raise AssertionError(
            "activation design: the future pipeline must remain a superset of the current one"
        )

    # 本模块绝不暴露任何可执行激活入口。
    namespace = globals()
    for name in _FORBIDDEN_PUBLIC_CALLABLES:
        if name in namespace:
            raise AssertionError(
                f"activation design: this design module must not define {name!r}"
            )


_assert_design_is_coherent()
