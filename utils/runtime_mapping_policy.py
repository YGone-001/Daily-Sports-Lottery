"""
运行期映射边界策略注册表（Runtime Mapping Policy Registry）
==========================================================
本模块是**唯一的声明式权威**：它描述「未来的运行期解析被允许消费什么」，以及
「什么一律被阻断」。运行期资格评估器（`utils.runtime_mapping_eligibility`）只是
**消费**这张表，不在这里、也不在任何别处散落判定分支。

生产授权状态（本阶段）
----------------------
``RUNTIME_MAPPING_ACTIVATION = "NOT_ENABLED"``
``PRODUCTION_MAPPING_WRITE_AUTHORIZATION = "NOT_GRANTED"``

本模块只做声明，**不含**任何 I/O、任何 Canonical 写入路径、任何执行入口。
运行期身份权威仍然是 ``utils.ingestion_identity.validate_ingestion_identity``。
"""
from __future__ import annotations

from dataclasses import dataclass

from utils.mapping_governance import (
    BINDING_ACTIONS,
    FORBIDDEN_ACTION_TOKENS,
    MAPPING_APPROVAL_PURPOSE,
    MAPPING_APPROVAL_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    MappingState,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    LEGACY_IDENTITY_KEY_SCHEMA_VERSION,
    SUPERSEDED_IDENTITY_KEY_SCHEMA_VERSION,
    IdentifierStability,
    IdentityKeyKind,
    ProviderIdUniquenessScope,
    RawIdentifierType,
    SUFFICIENT_IDENTITY_KEY_KINDS,
)

#: 策略注册表版本。任何影响**判定含义**的变化都必须提升该版本。
#:
#: ``1.1.0``：新增证据绑定规则、记录指纹 / 记录状态完整性规则与上下文证据来源规则。
RUNTIME_MAPPING_POLICY_VERSION = "1.1.0"

#: 运行期映射激活状态：本阶段**未启用**。
RUNTIME_MAPPING_ACTIVATION = "NOT_ENABLED"

#: 生产映射写入授权状态：本阶段**未授权**。
RUNTIME_MAPPING_WRITE_AUTHORIZATION = PRODUCTION_MAPPING_WRITE_AUTHORIZATION


# =============================================================================
# 声明式白名单
# =============================================================================

#: 运行期允许消费的**身份键种类**。
#:
#: 刻意与 ``SUFFICIENT_IDENTITY_KEY_KINDS`` 一致，并在导入时断言，防止两处漂移。
ALLOWED_RUNTIME_IDENTITY_KEYS: frozenset[str] = frozenset(SUFFICIENT_IDENTITY_KEY_KINDS)

#: 运行期允许的**身份键契约版本**。旧契约一律阻断。
ALLOWED_IDENTITY_KEY_VERSIONS: frozenset[str] = frozenset({IDENTITY_KEY_SCHEMA_VERSION})

#: 运行期允许的**批准契约版本**。
ALLOWED_APPROVAL_VERSIONS: frozenset[str] = frozenset({MAPPING_APPROVAL_SCHEMA_VERSION})

#: 运行期允许的**批准用途**（域分离串）。
ALLOWED_APPROVAL_PURPOSES: frozenset[str] = frozenset({MAPPING_APPROVAL_PURPOSE})

#: 运行期允许的**提案动作**（仅绑定到既有 Canonical 所有者；绝无改写动作）。
ALLOWED_ACTIONS: frozenset[str] = frozenset(BINDING_ACTIONS)

#: 运行期允许的**源作用域**：``(adapter, provider_namespace)``。
#:
#: 这与 ``utils.source_identity.SOURCE_ID_PATTERNS`` 声明的源 ID 构造路径一一对应。
#: 「在允许列表内」只表示这是一个**已声明的源路径**，绝不表示其证据充分 ——
#: 证据充分性由身份键规则单独判定。
ALLOWED_SOURCE_SCOPES: tuple[tuple[str, str], ...] = (
    ("live_football", "500l"),
    ("live_basketball", "500lq"),
    ("market_xml", "500j"),
    ("finished_feed", "500w"),
    ("html_fallback", "500j"),
)

#: 运行期允许的 **provider 标识符唯一性作用域**：只有独立文档化验证过的那一档。
ALLOWED_PROVIDER_ID_UNIQUENESS_SCOPES: frozenset[str] = frozenset(
    {ProviderIdUniquenessScope.VERIFIED_SCOPED.value}
)

#: 运行期允许的**提案状态**（只有活动终态可被消费）。
ALLOWED_PROPOSAL_STATES: frozenset[str] = frozenset({MappingState.APPROVED.value})

#: 全部**已知**提案状态。未知状态一律 fail closed（绝不退化为「非活动」而静默放过）。
KNOWN_PROPOSAL_STATES: frozenset[str] = frozenset(state.value for state in MappingState)


# ---------------------------------------------------------------------------
# 上下文证据来源（Context Evidence Provenance）
# ---------------------------------------------------------------------------

#: 调用方自行断言治理事实（提案指纹 / 状态 / 绑定）。**不是**独立验证过的证据。
CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED = "CALLER_ASSERTED_CONTEXT"

#: 治理事实来自一个**隔离的合成治理夹具快照**；结构校验与历史批准审计可能已执行，
#: 但账本哈希链本身**不是**外部信任锚（见账本信任锚声明）。
CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT = "ISOLATED_SNAPSHOT_CONTEXT"

#: 治理事实由**独立验证过的账本证据**支撑。
#:
#: 本阶段**没有任何代码路径**能独立建立这一档证据（不读取生产账本、不执行生产批准审计），
#: 因此任何声称该来源的上下文一律被阻断（``EVIDENCE_PROVENANCE_UNSUPPORTED``）。
#: 将来若接入真实账本验证，应在此处实现该判定，而不是放宽本声明。
CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER = "VERIFIED_GOVERNANCE_EVIDENCE"

#: 本阶段**允许**的上下文证据来源：调用方断言 与 隔离快照。
#: 两者产生的判定都是**条件式影子就绪判定**，绝不是生产授权。
ALLOWED_CONTEXT_EVIDENCE_PROVENANCE: frozenset[str] = frozenset(
    {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    }
)

#: 本阶段**阻断**的上下文证据来源（声称即拒绝，绝不据以升级信任）。
BLOCKED_CONTEXT_EVIDENCE_PROVENANCE: frozenset[str] = frozenset(
    {CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER}
)


# =============================================================================
# 声明式黑名单
# =============================================================================

#: 运行期**一律阻断**的身份键种类。
BLOCKED_IDENTITY_TYPES: frozenset[str] = frozenset(
    {
        IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value,
        IdentityKeyKind.AMBIGUOUS_SHARED_SOURCE_ID.value,
        IdentityKeyKind.INSUFFICIENT.value,
    }
)

#: 运行期**一律阻断**的身份键契约版本。
BLOCKED_IDENTITY_KEY_VERSIONS: frozenset[str] = frozenset(
    {LEGACY_IDENTITY_KEY_SCHEMA_VERSION, SUPERSEDED_IDENTITY_KEY_SCHEMA_VERSION}
)

#: 运行期**一律阻断**的 provider 唯一性作用域。
BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES: frozenset[str] = frozenset(
    {
        ProviderIdUniquenessScope.UNVERIFIED.value,
        ProviderIdUniquenessScope.UNKNOWN.value,
        ProviderIdUniquenessScope.NOT_APPLICABLE.value,
    }
)

#: 运行期**一律阻断**的标识符稳定性分类（行位置派生的身份永不持久）。
BLOCKED_IDENTITY_STABILITIES: frozenset[str] = frozenset(
    {
        IdentifierStability.ROW_POSITION_UNSTABLE.value,
        IdentifierStability.SYNTHETIC_LOCAL.value,
        IdentifierStability.UNKNOWN.value,
    }
)

#: 运行期**一律阻断**的原始标识符类型（HTML 元素 id 与行序号不是持久身份）。
BLOCKED_RAW_IDENTIFIER_TYPES: frozenset[str] = frozenset(
    {
        RawIdentifierType.ROW_INDEX.value,
        RawIdentifierType.HTML_ELEMENT_ID.value,
        RawIdentifierType.NONE.value,
        RawIdentifierType.UNKNOWN.value,
    }
)

#: 运行期**一律阻断**的动作（任何改写 Canonical 身份的动作都不存在）。
BLOCKED_ACTIONS: frozenset[str] = frozenset(FORBIDDEN_ACTION_TOKENS)

#: 运行期**一律阻断**的提案状态（撤销 / 取代 / 拒绝 / 待复核都不得被消费）。
BLOCKED_PROPOSAL_STATES: frozenset[str] = frozenset(
    {
        MappingState.REVOKED.value,
        MappingState.SUPERSEDED.value,
        MappingState.REJECTED.value,
        MappingState.REVIEW_REQUIRED.value,
        MappingState.OBSERVED.value,
        MappingState.PROPOSED.value,
    }
)

#: 运行期**明确禁止的效果**。任何要求这些效果之一的映射一律不合格。
BLOCKED_EFFECTS: frozenset[str] = frozenset(
    {
        "CANONICAL_ID_REWRITE",
        "HISTORICAL_MIGRATION",
        "SNAPSHOT_REGENERATION",
        "SETTLEMENT_REGENERATION",
        "EVALUATION_REGENERATION",
    }
)

#: 存储 -> 被阻断效果 的声明式映射（用于把「需要改写的目标存储」翻译成阻断效果）。
STORE_BLOCKED_EFFECTS: tuple[tuple[str, str], ...] = (
    ("prediction_snapshots.json", "SNAPSHOT_REGENERATION"),
    ("odds_snapshots.json", "SNAPSHOT_REGENERATION"),
    ("settlements.json", "SETTLEMENT_REGENERATION"),
    ("evaluation_rows.json", "EVALUATION_REGENERATION"),
    ("daily_matches.json", "CANONICAL_ID_REWRITE"),
    ("calibrated.json", "HISTORICAL_MIGRATION"),
    ("team_strength.json", "HISTORICAL_MIGRATION"),
)

#: 快照类存储（用于把改写需求翻译为具体效果）。
SNAPSHOT_STORES: frozenset[str] = frozenset(
    {"prediction_snapshots.json", "odds_snapshots.json"}
)
SETTLEMENT_STORES: frozenset[str] = frozenset({"settlements.json"})
EVALUATION_STORES: frozenset[str] = frozenset({"evaluation_rows.json"})

#: 运行期禁止写入的存储（本阶段完全只读）。
RUNTIME_READ_ONLY_STORES: tuple[str, ...] = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)


# =============================================================================
# 声明式规则表
# =============================================================================

#: 规则类别。
RULE_CATEGORY_EVIDENCE_BINDING = "EVIDENCE_BINDING"
RULE_CATEGORY_IDENTITY = "IDENTITY"
RULE_CATEGORY_GOVERNANCE = "GOVERNANCE"
RULE_CATEGORY_RUNTIME_SAFETY = "RUNTIME_SAFETY"


@dataclass(frozen=True)
class RuntimeEligibilityRule:
    """
    一条**声明式**运行期资格规则。

    ``code`` 是规则的唯一标识；评估器按 ``RUNTIME_ELIGIBILITY_RULES`` 的顺序逐条
    调用注册在 ``_RULE_CHECKS`` 中的纯函数，收集 ``rejection_code``。
    规则本身只是数据 —— 判定逻辑不散落在评估器里。
    """

    code: str
    category: str
    requirement: str
    rejection_code: str


#: **有序**规则表：先证据绑定、再身份、再治理、最后运行期安全。
RUNTIME_ELIGIBILITY_RULES: tuple[RuntimeEligibilityRule, ...] = (
    # -- 证据绑定（SourceObservation ↔ MappingProposal） ---------------------
    RuntimeEligibilityRule(
        code="SOURCE_OBSERVATION_IN_PROPOSAL",
        category=RULE_CATEGORY_EVIDENCE_BINDING,
        requirement=(
            "当前观测必须真正被提案的证据覆盖：观测指纹属于提案的观测指纹集合，"
            "且提案的观测引用与该证据一致。"
        ),
        rejection_code="SOURCE_OBSERVATION_NOT_IN_PROPOSAL",
    ),
    RuntimeEligibilityRule(
        code="SOURCE_IDENTITY_KEY_MATCHES",
        category=RULE_CATEGORY_EVIDENCE_BINDING,
        requirement=(
            "由 SourceObservation **独立推导**的作用域身份键必须与提案的身份键一致。"
        ),
        rejection_code="SOURCE_IDENTITY_KEY_MISMATCH",
    ),
    RuntimeEligibilityRule(
        code="SOURCE_SCOPE_MATCHES",
        category=RULE_CATEGORY_EVIDENCE_BINDING,
        requirement="观测的源作用域必须与提案声明的作用域一致。",
        rejection_code="SOURCE_SCOPE_MISMATCH",
    ),
    # -- 身份 ---------------------------------------------------------------
    RuntimeEligibilityRule(
        code="IDENTITY_KEY_VERSION_CURRENT",
        category=RULE_CATEGORY_IDENTITY,
        requirement="身份键契约版本必须是当前版本。",
        rejection_code="IDENTITY_KEY_VERSION_NOT_CURRENT",
    ),
    RuntimeEligibilityRule(
        code="IDENTITY_KEY_KIND_ALLOWED",
        category=RULE_CATEGORY_IDENTITY,
        requirement="身份键种类必须在允许列表内。",
        rejection_code="IDENTITY_KIND_NOT_ALLOWED",
    ),
    RuntimeEligibilityRule(
        code="IDENTITY_EVIDENCE_SUFFICIENT",
        category=RULE_CATEGORY_IDENTITY,
        requirement="身份证据必须充分（足以授权活动绑定）。",
        rejection_code="IDENTITY_EVIDENCE_INSUFFICIENT",
    ),
    RuntimeEligibilityRule(
        code="PROVIDER_ID_SCOPE_VERIFIED",
        category=RULE_CATEGORY_IDENTITY,
        requirement="provider 标识符的唯一性作用域必须已验证。",
        rejection_code="PROVIDER_ID_SCOPE_NOT_VERIFIED",
    ),
    RuntimeEligibilityRule(
        code="UNVERIFIED_PROVIDER_ID_BLOCKED",
        category=RULE_CATEGORY_IDENTITY,
        requirement="UNVERIFIED_PROVIDER_ID 一律阻断。",
        rejection_code="UNVERIFIED_PROVIDER_ID",
    ),
    RuntimeEligibilityRule(
        code="BLOCKED_IDENTITY_TYPE_REJECTED",
        category=RULE_CATEGORY_IDENTITY,
        requirement="其余被阻断的身份种类（歧义共享源 ID / 证据不足）一律拒绝。",
        rejection_code="IDENTITY_TYPE_BLOCKED",
    ),
    RuntimeEligibilityRule(
        code="ROW_POSITION_IDENTITY_BLOCKED",
        category=RULE_CATEGORY_IDENTITY,
        requirement="行位置派生的身份一律阻断。",
        rejection_code="ROW_POSITION_UNSTABLE",
    ),
    RuntimeEligibilityRule(
        code="RAW_IDENTIFIER_TYPE_DURABLE",
        category=RULE_CATEGORY_IDENTITY,
        requirement="原始标识符类型必须是持久类型（行序号 / HTML 元素 id 一律阻断）。",
        rejection_code="RAW_IDENTIFIER_TYPE_NOT_DURABLE",
    ),
    RuntimeEligibilityRule(
        code="SOURCE_FINGERPRINT_DETERMINISTIC",
        category=RULE_CATEGORY_IDENTITY,
        requirement="必须存在确定性的源证据指纹。",
        rejection_code="SOURCE_FINGERPRINT_MISSING",
    ),
    RuntimeEligibilityRule(
        code="IDENTITY_NOT_AMBIGUOUS",
        category=RULE_CATEGORY_IDENTITY,
        requirement="不得存在待处理的身份歧义。",
        rejection_code="IDENTITY_AMBIGUOUS",
    ),
    RuntimeEligibilityRule(
        code="NO_COMPETING_CANONICAL_OWNERS",
        category=RULE_CATEGORY_IDENTITY,
        requirement="同一源身份不得存在竞争的 Canonical 所有者。",
        rejection_code="CANONICAL_OWNER_CONFLICT",
    ),
    # -- 治理 ---------------------------------------------------------------
    RuntimeEligibilityRule(
        code="PROPOSAL_PRESENT",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="提案必须存在且结构合法。",
        rejection_code="PROPOSAL_MISSING",
    ),
    RuntimeEligibilityRule(
        code="GOVERNANCE_EVIDENCE_PROVENANCE_SUPPORTED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement=(
            "上下文必须显式声明治理证据来源，且该来源必须是本阶段可支持的来源"
            "（调用方断言 / 隔离快照）；声称独立验证过的账本证据一律阻断。"
        ),
        rejection_code="EVIDENCE_PROVENANCE_UNSUPPORTED",
    ),
    RuntimeEligibilityRule(
        code="PROPOSAL_FINGERPRINT_PRESENT",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement=(
            "必须存在**已记录**的提案指纹；缺失 / 空白即拒绝，"
            "绝不把「没有记录」当作「校验可选」。"
        ),
        rejection_code="PROPOSAL_FINGERPRINT_MISSING",
    ),
    RuntimeEligibilityRule(
        code="PROPOSAL_FINGERPRINT_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement=(
            "已记录的提案指纹必须与当前提案指纹逐字一致"
            "（格式不合法同样按不一致处理）。"
        ),
        rejection_code="PROPOSAL_FINGERPRINT_MISMATCH",
    ),
    RuntimeEligibilityRule(
        code="RECORDED_STATE_PRESENT",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="必须存在**已记录**的治理状态；缺失即拒绝（绝不回退到提案自述状态）。",
        rejection_code="RECORDED_STATE_MISSING",
    ),
    RuntimeEligibilityRule(
        code="RECORDED_STATE_KNOWN",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="已记录状态必须是已知状态之一。",
        rejection_code="RECORDED_STATE_UNKNOWN",
    ),
    RuntimeEligibilityRule(
        code="RECORDED_STATE_PROPOSAL_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="已记录状态必须对应**被评估的那个提案 ID**。",
        rejection_code="RECORDED_STATE_PROPOSAL_MISMATCH",
    ),
    RuntimeEligibilityRule(
        code="PROPOSAL_STATE_ACTIVE",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="已记录状态必须是活动终态（APPROVED）。",
        rejection_code="MAPPING_STATE_NOT_ACTIVE",
    ),
    RuntimeEligibilityRule(
        code="MAPPING_NOT_REVOKED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="映射不得处于已撤销状态。",
        rejection_code="MAPPING_REVOKED",
    ),
    RuntimeEligibilityRule(
        code="MAPPING_NOT_SUPERSEDED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="映射不得处于已取代状态。",
        rejection_code="MAPPING_SUPERSEDED",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_PRESENT",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="所有者批准件必须存在。",
        rejection_code="APPROVAL_MISSING",
    ),
    RuntimeEligibilityRule(
        code="TRUST_ANCHOR_PRESENT",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="必须显式提供可信公钥；内嵌公钥绝不作为信任锚。",
        rejection_code="TRUST_ANCHOR_MISSING",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_VERSION_ALLOWED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="批准契约版本必须在允许列表内。",
        rejection_code="APPROVAL_LEGACY_VERSION",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_PURPOSE_ALLOWED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="批准用途必须是映射治理用途。",
        rejection_code="APPROVAL_PURPOSE_MISMATCH",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_APPROVER_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="批准人身份必须与运行期期望一致。",
        rejection_code="APPROVAL_WRONG_APPROVER",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_OPERATOR_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="操作者身份必须与提案的操作者一致。",
        rejection_code="APPROVAL_WRONG_OPERATOR",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_SIGNATURE_VERIFIES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="Ed25519 签名必须对规范载荷验证通过。",
        rejection_code="APPROVAL_SIGNATURE_INVALID",
    ),
    RuntimeEligibilityRule(
        code="BASELINE_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="批准绑定的基线必须等于当前运行期基线。",
        rejection_code="BASELINE_STALE",
    ),
    RuntimeEligibilityRule(
        code="DATASET_IDENTITY_MATCHES",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="批准绑定的数据集身份必须与运行期一致。",
        rejection_code="DATASET_IDENTITY_MISMATCH",
    ),
    RuntimeEligibilityRule(
        code="APPROVAL_NOT_REPLAYED",
        category=RULE_CATEGORY_GOVERNANCE,
        requirement="同一批准件不得被复用于另一个源身份。",
        rejection_code="APPROVAL_REPLAYED",
    ),
    # -- 运行期安全 ---------------------------------------------------------
    RuntimeEligibilityRule(
        code="ACTION_ALLOWED",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="提案动作必须在允许列表内（且不得是任何改写动作）。",
        rejection_code="ACTION_NOT_ALLOWED",
    ),
    RuntimeEligibilityRule(
        code="SOURCE_SCOPE_ALLOWED",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="源作用域必须是一个已声明的源路径。",
        rejection_code="SOURCE_SCOPE_NOT_ALLOWED",
    ),
    RuntimeEligibilityRule(
        code="CANONICAL_OWNER_EXISTS_OR_NEW_EVENT",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="目标 Canonical ID 必须存在，或显式声明为新事件模式。",
        rejection_code="CANONICAL_OWNER_MISSING",
    ),
    RuntimeEligibilityRule(
        code="NO_CANONICAL_OWNERSHIP_CONFLICT",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得存在 Canonical 所有权冲突。",
        rejection_code="CANONICAL_OWNERSHIP_CONFLICT",
    ),
    RuntimeEligibilityRule(
        code="NO_CANONICAL_ID_CHANGE",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="绑定不得要求改写 Canonical ID。",
        rejection_code="CANONICAL_ID_CHANGE_REQUIRED",
    ),
    RuntimeEligibilityRule(
        code="NO_HISTORICAL_REWRITE",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得要求任何历史数据改写。",
        rejection_code="HISTORICAL_REFERENCE_CONFLICT",
    ),
    RuntimeEligibilityRule(
        code="NO_SNAPSHOT_IMPACT",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得产生任何快照影响。",
        rejection_code="SNAPSHOT_IMPACT",
    ),
    RuntimeEligibilityRule(
        code="NO_SETTLEMENT_IMPACT",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得产生任何结算影响。",
        rejection_code="SETTLEMENT_IMPACT",
    ),
    RuntimeEligibilityRule(
        code="NO_EVALUATION_IMPACT",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得产生任何评估样本影响。",
        rejection_code="EVALUATION_IMPACT",
    ),
    RuntimeEligibilityRule(
        code="NO_MIGRATION_REQUIRED",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得要求任何迁移。",
        rejection_code="MIGRATION_REQUIRED",
    ),
    RuntimeEligibilityRule(
        code="NO_PENDING_AMBIGUITY",
        category=RULE_CATEGORY_RUNTIME_SAFETY,
        requirement="不得存在待决歧义。",
        rejection_code="PENDING_AMBIGUITY",
    ),
)

#: 全部拒绝码（声明式集合，供清单与报告使用）。
REJECTION_CODES: frozenset[str] = frozenset(
    rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES
)


def rule_codes() -> tuple[str, ...]:
    """全部规则标识（只读，声明顺序）。"""
    return tuple(rule.code for rule in RUNTIME_ELIGIBILITY_RULES)


def rule_for(code: str) -> RuntimeEligibilityRule | None:
    """按标识取规则（只读）。"""
    for rule in RUNTIME_ELIGIBILITY_RULES:
        if rule.code == code:
            return rule
    return None


def rules_by_category() -> dict[str, tuple[str, ...]]:
    """按类别列出规则标识（只读）。"""
    out: dict[str, list[str]] = {}
    for rule in RUNTIME_ELIGIBILITY_RULES:
        out.setdefault(rule.category, []).append(rule.code)
    return {category: tuple(codes) for category, codes in out.items()}


def runtime_policy_status() -> dict:
    """策略注册表的只读状态视图（供审计与报告）。"""
    return {
        "runtime_mapping_policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "production_mapping_write_authorization": RUNTIME_MAPPING_WRITE_AUTHORIZATION,
        "runtime_identity_authority": (
            "utils.ingestion_identity.validate_ingestion_identity"
        ),
        "allowed_runtime_identity_keys": sorted(ALLOWED_RUNTIME_IDENTITY_KEYS),
        "allowed_identity_key_versions": sorted(ALLOWED_IDENTITY_KEY_VERSIONS),
        "allowed_approval_versions": sorted(ALLOWED_APPROVAL_VERSIONS),
        "allowed_actions": sorted(ALLOWED_ACTIONS),
        "allowed_proposal_states": sorted(ALLOWED_PROPOSAL_STATES),
        "known_proposal_states": sorted(KNOWN_PROPOSAL_STATES),
        "allowed_context_evidence_provenance": sorted(
            ALLOWED_CONTEXT_EVIDENCE_PROVENANCE
        ),
        "blocked_context_evidence_provenance": sorted(
            BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
        ),
        "allowed_source_scopes": [list(scope) for scope in ALLOWED_SOURCE_SCOPES],
        "blocked_identity_types": sorted(BLOCKED_IDENTITY_TYPES),
        "blocked_identity_key_versions": sorted(BLOCKED_IDENTITY_KEY_VERSIONS),
        "blocked_provider_id_uniqueness_scopes": sorted(
            BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES
        ),
        "blocked_identity_stabilities": sorted(BLOCKED_IDENTITY_STABILITIES),
        "blocked_raw_identifier_types": sorted(BLOCKED_RAW_IDENTIFIER_TYPES),
        "blocked_actions": sorted(BLOCKED_ACTIONS),
        "blocked_proposal_states": sorted(BLOCKED_PROPOSAL_STATES),
        "blocked_effects": sorted(BLOCKED_EFFECTS),
        "read_only_stores": list(RUNTIME_READ_ONLY_STORES),
        "rule_count": len(RUNTIME_ELIGIBILITY_RULES),
        "rules_by_category": {
            category: list(codes) for category, codes in rules_by_category().items()
        },
    }


# =============================================================================
# 注册表自洽性（导入时断言，fail closed）
# =============================================================================

def _assert_policy_registry_is_coherent() -> None:
    """
    导入时断言：白/黑名单互不重叠、规则码唯一、拒绝码唯一。

    任何不一致都在导入期 fail closed —— 一张自相矛盾的策略表绝不允许被消费。
    """
    if ALLOWED_RUNTIME_IDENTITY_KEYS & BLOCKED_IDENTITY_TYPES:
        raise AssertionError(
            "runtime policy: allowed identity kinds overlap blocked identity types"
        )
    if ALLOWED_IDENTITY_KEY_VERSIONS & BLOCKED_IDENTITY_KEY_VERSIONS:
        raise AssertionError(
            "runtime policy: allowed identity key versions overlap blocked versions"
        )
    if ALLOWED_ACTIONS & BLOCKED_ACTIONS:
        raise AssertionError("runtime policy: allowed actions overlap blocked actions")
    if ALLOWED_PROPOSAL_STATES & BLOCKED_PROPOSAL_STATES:
        raise AssertionError(
            "runtime policy: allowed proposal states overlap blocked states"
        )
    if ALLOWED_CONTEXT_EVIDENCE_PROVENANCE & BLOCKED_CONTEXT_EVIDENCE_PROVENANCE:
        raise AssertionError(
            "runtime policy: allowed context evidence provenance overlaps blocked provenance"
        )
    if not ALLOWED_CONTEXT_EVIDENCE_PROVENANCE:
        raise AssertionError(
            "runtime policy: at least one context evidence provenance must be allowed"
        )
    if not BLOCKED_PROPOSAL_STATES <= KNOWN_PROPOSAL_STATES:
        raise AssertionError(
            "runtime policy: blocked proposal states must be known states"
        )
    if not ALLOWED_PROPOSAL_STATES <= KNOWN_PROPOSAL_STATES:
        raise AssertionError(
            "runtime policy: allowed proposal states must be known states"
        )
    if not ALLOWED_APPROVAL_PURPOSES:
        raise AssertionError("runtime policy: at least one approval purpose is required")

    codes = rule_codes()
    if len(set(codes)) != len(codes):
        raise AssertionError("runtime policy: duplicate rule codes")
    rejection = [rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES]
    if len(set(rejection)) != len(rejection):
        raise AssertionError("runtime policy: duplicate rejection codes")
    if not BLOCKED_EFFECTS:
        raise AssertionError("runtime policy: blocked effects must not be empty")


_assert_policy_registry_is_coherent()
