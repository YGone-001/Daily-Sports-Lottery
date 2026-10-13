"""
运动作用域市场源 ID 契约 + 只读迁移影响清单
============================================
本模块只做两件事，且**两件都是纯设计 / 只读分析**：

1. **版本化的未来市场源 ID 契约**（设计文档 + 纯函数）。
   当前 `500j-{date}-{number}` 前缀被足球（jczq）与篮球（jclq）**共用**，同一日期下
   两侧编号序列相互独立，因此同一个字符串可能代表两个不同运动的不同事件。
   本模块给出可评估的三个候选格式与优选结论，但**绝不**把它接进生产解析器。

2. **只读的迁移影响清单（impact manifest）构建器**。
   给定「合成 Canonical 夹具 + 合成源观测 + 提议的旧→新源映射 + 可选的合成依赖存储」，
   产出确定性的影响分析工件。清单**不包含任何可执行的迁移操作**。

绝对边界
--------
- 不改写任何 Canonical ID，不改写任何源别名，不写任何文件。
- 不迁移历史源标识符，不重写快照 / 结算 / 评估历史。
- 不引入生产迁移执行器，不引入 force / 绕过校验的开关。
- 不消耗治理账本批准，不把清单当作所有者批准。

清单是一份**分析工件**，不是许可。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    ProviderIdUniquenessScope,
    canonical_json,
    detect_all_conflicts,
    detect_cross_sport_namespace_collision,
    fingerprint,
    identity_kind_is_sufficient,
)

# =============================================================================
# 1. 未来市场源 ID 契约（设计；未接入生产解析器）
# =============================================================================

#: 契约版本。任何影响格式**含义**的变化都必须提升该版本。
MARKET_SOURCE_ID_CONTRACT_VERSION = "1.0.0"

#: 当前（历史）共用前缀。
LEGACY_MARKET_SOURCE_ID_PREFIX = "500j"

#: 运动 -> 命名空间后缀标记（Option A）。
SPORT_NAMESPACE_TOKENS: dict[str, str] = {
    "football": "zq",
    "basketball": "lq",
}


class MarketSourceIdKind(str, Enum):
    """市场源 ID 的格式类别。"""

    LEGACY_SHARED = "LEGACY_SHARED"
    SPORT_SCOPED = "SPORT_SCOPED"
    HTML_FALLBACK_POSITION = "HTML_FALLBACK_POSITION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MarketSourceIdOption:
    """一个候选的未来市场源 ID 格式。"""

    option_id: str
    format_template: str
    example_football: str
    example_basketball: str
    sport_disambiguation: str
    legacy_collision_risk: str
    assessment: str
    selected: bool


#: 候选格式评估（A / B / C）。
MARKET_SOURCE_ID_OPTIONS: tuple[MarketSourceIdOption, ...] = (
    MarketSourceIdOption(
        option_id="A",
        format_template="{prefix}{sport_token}-{date}-{number}",
        example_football="500jzq-2030-05-01-001",
        example_basketball="500jlq-2030-05-01-001",
        sport_disambiguation="命名空间前缀本身区分运动（500jzq / 500jlq）。",
        legacy_collision_risk=(
            "低：新前缀不匹配既有 `startswith('500j-')` 的 market-fallback 兼容分支，"
            "因此不会误触发 legacy 语义。"
        ),
        assessment=(
            "首选。前缀级消歧，与既有按 `-` 首段切分命名空间的代码路径天然兼容；"
            "新增两个命名空间前缀需要在 SOURCE_NAMESPACE_TIERS 中显式登记。"
        ),
        selected=True,
    ),
    MarketSourceIdOption(
        option_id="B",
        format_template="{prefix}-{sport}-{date}-{number}",
        example_football="500j-football-2030-05-01-001",
        example_basketball="500j-basketball-2030-05-01-001",
        sport_disambiguation="第二个 `-` 段显式给出运动。",
        legacy_collision_risk=(
            "中：新字符串仍然匹配 `startswith('500j-')`，会误触发 "
            "`competition_number_for_match` 的历史 market-fallback 分支（读取 `round`）。"
        ),
        assessment=(
            "可行但需要同时收紧既有兼容分支；运动段与日期段靠字典序区分（年份是数字、"
            "运动标记是字母），解析可判定，但兼容风险高于 A。"
        ),
        selected=False,
    ),
    MarketSourceIdOption(
        option_id="C",
        format_template="{prefix}v2-{sport}-{date}-{number}",
        example_football="500jv2-football-2030-05-01-001",
        example_basketball="500jv2-basketball-2030-05-01-001",
        sport_disambiguation="命名空间内嵌显式契约版本标记，再给出运动段。",
        legacy_collision_risk=(
            "低：`500jv2-` 不匹配 `500j-`；但引入了第三个命名空间，且版本标记与"
            "`IDENTITY_KEY_SCHEMA_VERSION` 表达的是两件不同的事，容易被混淆。"
        ),
        assessment=(
            "可作为「需要显式契约版本标记」时的替代；相较 A 多一层冗余，"
            "在既有代码里没有额外收益。"
        ),
        selected=False,
    ),
)

#: 优选格式。
PREFERRED_MARKET_SOURCE_ID_OPTION_ID = "A"


def preferred_market_source_id_option() -> MarketSourceIdOption:
    """返回优选候选（只读）。"""
    for option in MARKET_SOURCE_ID_OPTIONS:
        if option.option_id == PREFERRED_MARKET_SOURCE_ID_OPTION_ID:
            return option
    raise AssertionError("preferred market source ID option is not declared")


#: 设计必须回答的十个问题（对应 §9 的要求）。
MARKET_SOURCE_ID_DESIGN_CONSIDERATIONS: tuple[tuple[str, str], ...] = (
    (
        "sport_disambiguation",
        "运动由命名空间前缀（500jzq / 500jlq）承载，因此即使日期或编号段缺失，"
        "运动归属依然确定。",
    ),
    (
        "event_date_scoping",
        "日期段保留 `YYYY-MM-DD`，与既有 `{prefix}-{date}-{number}` 段数一致，"
        "使竞彩编号的作用域仍是 (sport, date, number)。",
    ),
    (
        "competition_number_normalization",
        "编号段沿用既有 `normalize_competition_number` 口径（去前导零、仅保留数字）；"
        "新契约不改变归一化语义。",
    ),
    (
        "provider_id_vs_competition_number",
        "新格式只承载**竞彩编号**；provider 比赛号继续留在溯源侧车中，"
        "绝不被写入源 ID（其唯一性未经验证）。",
    ),
    (
        "unknown_identifiers",
        "缺少编号时不得伪造：沿用既有派生规则（无编号即退化为行位置派生 ID），"
        "并且该 ID 只能观测 / 复核，不得用于活动绑定。",
    ),
    (
        "legacy_500j_compatibility",
        "历史 `500j-…` 字符串保持**逐字节不变**，并被显式标记为歧义别名；"
        "只能经治理决策映射，绝不静默改写。",
    ),
    (
        "historical_canonical_ownership",
        "既有 Canonical ID 一律保持不变；源别名变更**不自动**构成 Canonical ID 变更的理由。",
    ),
    (
        "mapping_approval_requirements",
        "任何旧→新别名绑定都必须经 Ed25519 所有者批准，且绑定判别性身份键"
        "（契约版本 3.0.0），绝不自动批准。",
    ),
    (
        "snapshot_references",
        "prediction / odds 快照的 `snapshot_id` 由 `match_id` 派生，"
        "因此 Canonical ID 若被改写，快照 ID 将无法再由 `match_id` 复现 —— "
        "这是保持 Canonical ID 不变的最强论据。",
    ),
    (
        "migration_rollback_feasibility",
        "别名映射本身可逆（旧字符串保留、新字符串可重算）；但一旦改写 Canonical ID，"
        "快照 / 结算 / 评估链的 ID 复现性无法回滚。因此回滚可行性完全取决于"
        "「是否改写 Canonical ID」，而本阶段明确不改写。",
    ),
)


def compose_sport_scoped_market_source_id(
    *, sport: str, event_date: str, number: str
) -> str:
    """
    按优选契约（Option A）构造**未来**运动作用域市场源 ID。

    **绝不**被生产解析器调用：本函数只用于设计验证与影响分析。
    非法输入一律 fail closed（不猜测、不降级）。
    """
    token = SPORT_NAMESPACE_TOKENS.get(sport)
    if token is None:
        raise ValueError(f"unsupported sport for a sport-scoped market source ID: {sport!r}")
    if not event_date or not str(event_date).strip():
        raise ValueError("event_date is required for a sport-scoped market source ID")
    if number is None or not str(number).strip():
        raise ValueError("a competition number is required; it is never invented")
    return f"{LEGACY_MARKET_SOURCE_ID_PREFIX}{token}-{event_date}-{str(number).strip()}"


def parse_sport_scoped_market_source_id(source_id: str) -> dict[str, str] | None:
    """
    解析优选契约下的运动作用域市场源 ID（只读）。无法解析返回 ``None``。

    刻意**不**尝试把历史 `500j-{date}-{number}` 解释为运动作用域 ID。
    """
    if not isinstance(source_id, str):
        return None
    for sport, token in sorted(SPORT_NAMESPACE_TOKENS.items()):
        prefix = f"{LEGACY_MARKET_SOURCE_ID_PREFIX}{token}-"
        if not source_id.startswith(prefix):
            continue
        remainder = source_id[len(prefix):]
        parts = remainder.split("-")
        if len(parts) < 4:
            return None
        event_date = "-".join(parts[:3])
        number = "-".join(parts[3:])
        if not number:
            return None
        return {"sport": sport, "event_date": event_date, "number": number}
    return None


def classify_market_source_id(source_id: Any) -> str:
    """
    判定一个市场源 ID 的格式类别（只读、不猜测）。

    - ``SPORT_SCOPED``：优选契约下的运动作用域 ID。
    - ``HTML_FALLBACK_POSITION``：``500j-{date}-{row_index}`` 的行位置派生兜底 ID。
      与竞彩编号路径共享前缀却语义完全不同，必须由调用方用适配器上下文区分；
      本函数只能在**没有适配器上下文**时按保守口径归类为 ``LEGACY_SHARED``。
    - ``LEGACY_SHARED``：历史共用前缀的字符串。
    - ``UNKNOWN``：无法解析。
    """
    if not isinstance(source_id, str) or not source_id.strip():
        return MarketSourceIdKind.UNKNOWN.value
    if parse_sport_scoped_market_source_id(source_id) is not None:
        return MarketSourceIdKind.SPORT_SCOPED.value
    if source_id.startswith(f"{LEGACY_MARKET_SOURCE_ID_PREFIX}-"):
        return MarketSourceIdKind.LEGACY_SHARED.value
    return MarketSourceIdKind.UNKNOWN.value


# =============================================================================
# 2. 只读迁移影响清单
# =============================================================================

MIGRATION_MANIFEST_SCHEMA_VERSION = "1.0.0"

#: 清单类别：这是**分析工件**，不是许可，更不是可执行计划。
MANIFEST_KIND = "READ_ONLY_MIGRATION_IMPACT_ANALYSIS"

#: 明确标记：本模块**不存在**可执行的迁移操作。
MIGRATION_EXECUTION_AVAILABLE = False

#: 清单中禁止出现的键（任何「可执行」暗示都会让清单被误读为计划）。
FORBIDDEN_MANIFEST_KEYS: tuple[str, ...] = (
    "apply",
    "execute",
    "executor",
    "migration_plan",
    "operations",
    "rewrite",
    "mutations",
)

#: 六种影响分类。
CLASSIFICATION_UNCHANGED_CANONICAL_ID = "UNCHANGED_CANONICAL_ID"
CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY = "SOURCE_ALIAS_CHANGE_ONLY"
CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT = "HISTORICAL_REFERENCE_IMPACT"
CLASSIFICATION_AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
CLASSIFICATION_UNSUPPORTED_EVIDENCE = "UNSUPPORTED_EVIDENCE"
CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION = "BLOCKED_BY_MISSING_AUTHORIZATION"

ALL_IMPACT_CLASSIFICATIONS: tuple[str, ...] = (
    CLASSIFICATION_UNCHANGED_CANONICAL_ID,
    CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY,
    CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT,
    CLASSIFICATION_AMBIGUOUS_MAPPING,
    CLASSIFICATION_UNSUPPORTED_EVIDENCE,
    CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION,
)

#: 七个持久化存储的引用规格：``(文件名, 引用种类, 提取器名)``。
STORE_REFERENCE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("daily_matches.json", "CANONICAL_DEFINITION", "canonical"),
    ("team_strength.json", "NO_MATCH_ID_REFERENCE", "none"),
    ("calibrated.json", "MATCH_ID_LIST", "id_list"),
    ("prediction_snapshots.json", "MATCH_ID_FIELD", "snapshot_map"),
    ("odds_snapshots.json", "MATCH_ID_FIELD", "snapshot_map"),
    ("settlements.json", "MATCH_ID_FIELD", "settlement_map"),
    ("evaluation_rows.json", "MATCH_ID_FIELD", "row_map"),
)

#: 七存储的固定顺序（保证清单确定性）。
STORE_ORDER: tuple[str, ...] = tuple(spec[0] for spec in STORE_REFERENCE_SPECS)


@dataclass(frozen=True)
class ProposedSourceMapping:
    """
    一条**提议**的旧→新源映射（仅用于影响分析；不执行、不批准）。

    ``existing_canonical_id`` 给出该源别名当前归属的 Canonical ID。
    刻意**不提供**任何「新 Canonical ID」字段：本阶段不存在改写 Canonical ID 的操作。
    """

    legacy_source_id: str
    proposed_source_id: str
    existing_canonical_id: str | None
    identity_kind: str
    identity_value: str
    provider_id_uniqueness_scope: str = ProviderIdUniquenessScope.NOT_APPLICABLE.value
    binding_key_contract_version: str = IDENTITY_KEY_SCHEMA_VERSION
    observation_fingerprints: tuple[str, ...] = ()
    mapping_proposal_reference: str = ""

    def as_dict(self) -> dict:
        return {
            "legacy_source_id": self.legacy_source_id,
            "proposed_source_id": self.proposed_source_id,
            "existing_canonical_id": self.existing_canonical_id,
            "identity_kind": self.identity_kind,
            "identity_value": self.identity_value,
            "provider_id_uniqueness_scope": self.provider_id_uniqueness_scope,
            "binding_key_contract_version": self.binding_key_contract_version,
            "observation_fingerprints": list(self.observation_fingerprints),
            "mapping_proposal_reference": self.mapping_proposal_reference,
        }


# ---------------------------------------------------------------------------
# 基线指纹（与治理基线刻意区分）
# ---------------------------------------------------------------------------

def fixture_baseline_fingerprint(matches: Sequence[Mapping[str, Any]]) -> str:
    """
    迁移夹具的基线指纹（与输入顺序无关）。

    刻意使用与治理层**不同**的 ``kind`` 标记，避免把一个迁移夹具基线与治理基线
    指纹混为一谈。
    """
    per_match = sorted(
        fingerprint({"id": str(m.get("id") or ""), "match": dict(m)}) for m in matches
    )
    return fingerprint(
        {
            "kind": "MIGRATION_FIXTURE_BASELINE",
            "contract_version": MARKET_SOURCE_ID_CONTRACT_VERSION,
            "count": len(per_match),
            "matches": per_match,
        }
    )


# ---------------------------------------------------------------------------
# 七存储引用枚举
# ---------------------------------------------------------------------------

def _canonical_references(store: Any) -> tuple[dict[str, list[str]], list[str]]:
    references: dict[str, list[str]] = {}
    notes: list[str] = []
    matches = store.get("matches") if isinstance(store, Mapping) else None
    if not isinstance(matches, list):
        return references, ["daily_matches.json 缺少 matches 列表"]
    for index, record in enumerate(matches):
        if not isinstance(record, Mapping):
            notes.append(f"matches[{index}] 不是对象")
            continue
        mid = str(record.get("id") or "")
        if not mid:
            notes.append(f"matches[{index}] 缺少 id")
            continue
        references.setdefault(mid, []).append(f"/matches/{index}/id")
    return references, notes


def _id_list_references(store: Any) -> tuple[dict[str, list[str]], list[str]]:
    references: dict[str, list[str]] = {}
    ids = store.get("ids") if isinstance(store, Mapping) else None
    if not isinstance(ids, list):
        return references, ["calibrated.json 缺少 ids 列表"]
    for index, value in enumerate(ids):
        mid = str(value or "")
        if not mid:
            continue
        references.setdefault(mid, []).append(f"/ids/{index}")
    return references, []


def _map_references(
    store: Any, *, collection: str, id_field: str = "match_id"
) -> tuple[dict[str, list[str]], list[str]]:
    references: dict[str, list[str]] = {}
    collection_data = store.get(collection) if isinstance(store, Mapping) else None
    if not isinstance(collection_data, Mapping):
        return references, [f"{collection} 不是以 ID 为键的对象"]
    for key in sorted(collection_data):
        record = collection_data[key]
        if not isinstance(record, Mapping):
            continue
        mid = str(record.get(id_field) or "")
        if not mid:
            continue
        references.setdefault(mid, []).append(f"/{collection}/{key}/{id_field}")
    return references, []


def enumerate_store_references(stores: Mapping[str, Any]) -> dict[str, dict]:
    """
    只读枚举七个存储中的比赛 ID 引用（确定性，绝不写文件）。

    返回 ``{store_name: {...}}``；每个存储给出引用种类、``match_id -> [JSON 指针]``、
    以及该存储自身的说明。
    """
    out: dict[str, dict] = {}
    for name, kind, extractor in STORE_REFERENCE_SPECS:
        store = stores.get(name)
        present = store is not None
        if not present:
            out[name] = {
                "reference_kind": kind,
                "present": False,
                "references": {},
                "notes": ["存储未提供；影响无法评估。"],
            }
            continue
        if extractor == "canonical":
            references, notes = _canonical_references(store)
        elif extractor == "id_list":
            references, notes = _id_list_references(store)
        elif extractor == "snapshot_map":
            references, notes = _map_references(store, collection="snapshots")
        elif extractor == "settlement_map":
            references, notes = _map_references(store, collection="settlements")
        elif extractor == "row_map":
            references, notes = _map_references(store, collection="rows")
        else:
            references, notes = {}, ["该存储不含比赛 ID 引用（例如球队实力按队名索引）。"]
        out[name] = {
            "reference_kind": kind,
            "present": True,
            "references": {mid: sorted(pointers) for mid, pointers in sorted(references.items())},
            "notes": notes,
        }
    return out


def derived_identity_warnings(stores: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """
    只读列出**由 match_id 派生**的标识符（它们会因 Canonical ID 改写而失去复现性）。
    """
    warnings: list[dict[str, str]] = []
    for name in ("prediction_snapshots.json", "odds_snapshots.json"):
        store = stores.get(name)
        collection = store.get("snapshots") if isinstance(store, Mapping) else None
        if isinstance(collection, Mapping) and collection:
            warnings.append(
                {
                    "store": name,
                    "derived_field": "snapshot_id",
                    "detail": (
                        "snapshot_id 由 match_id 派生（sha256），改写 Canonical ID 后"
                        "无法再由 match_id 复现该 ID。"
                    ),
                }
            )
    store = stores.get("evaluation_rows.json")
    rows = store.get("rows") if isinstance(store, Mapping) else None
    if isinstance(rows, Mapping) and rows:
        warnings.append(
            {
                "store": "evaluation_rows.json",
                "derived_field": "evaluation_id",
                "detail": (
                    "evaluation_id 由 (snapshot_id, settlement_id) 派生，"
                    "因此间接依赖 match_id。"
                ),
            }
        )
    return tuple(warnings)


# ---------------------------------------------------------------------------
# 影响清单构建
# ---------------------------------------------------------------------------

def _chain_inconsistencies(stores: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """只读检查快照 / 结算 / 评估链的 match_id 一致性。"""
    findings: list[dict[str, str]] = []
    snapshots = stores.get("prediction_snapshots.json")
    snap_map = snapshots.get("snapshots") if isinstance(snapshots, Mapping) else None
    settlements = stores.get("settlements.json")
    settle_map = settlements.get("settlements") if isinstance(settlements, Mapping) else None
    rows_store = stores.get("evaluation_rows.json")
    row_map = rows_store.get("rows") if isinstance(rows_store, Mapping) else None

    if isinstance(settle_map, Mapping) and isinstance(snap_map, Mapping):
        for key in sorted(settle_map):
            settlement = settle_map[key]
            if not isinstance(settlement, Mapping):
                continue
            snapshot = snap_map.get(settlement.get("snapshot_id"))
            if not isinstance(snapshot, Mapping):
                findings.append(
                    {
                        "kind": "MISSING_SNAPSHOT",
                        "subject": f"settlements/{key}",
                        "detail": f"结算指向不存在的快照 {settlement.get('snapshot_id')!r}",
                    }
                )
                continue
            if settlement.get("match_id") != snapshot.get("match_id"):
                findings.append(
                    {
                        "kind": "MATCH_ID_MISMATCH",
                        "subject": f"settlements/{key}",
                        "detail": (
                            f"结算 match_id={settlement.get('match_id')!r} 与快照 "
                            f"match_id={snapshot.get('match_id')!r} 不一致"
                        ),
                    }
                )

    if isinstance(row_map, Mapping):
        for key in sorted(row_map):
            row = row_map[key]
            if not isinstance(row, Mapping):
                continue
            if isinstance(snap_map, Mapping):
                snapshot = snap_map.get(row.get("snapshot_id"))
                if isinstance(snapshot, Mapping) and snapshot.get("match_id") != row.get(
                    "match_id"
                ):
                    findings.append(
                        {
                            "kind": "MATCH_ID_MISMATCH",
                            "subject": f"evaluation_rows/{key}",
                            "detail": (
                                f"评估样本 match_id={row.get('match_id')!r} 与其快照 "
                                f"match_id={snapshot.get('match_id')!r} 不一致"
                            ),
                        }
                    )
            if isinstance(settle_map, Mapping):
                settlement = settle_map.get(row.get("settlement_id"))
                if isinstance(settlement, Mapping) and settlement.get(
                    "match_id"
                ) != row.get("match_id"):
                    findings.append(
                        {
                            "kind": "MATCH_ID_MISMATCH",
                            "subject": f"evaluation_rows/{key}",
                            "detail": (
                                f"评估样本 match_id={row.get('match_id')!r} 与其结算 "
                                f"match_id={settlement.get('match_id')!r} 不一致"
                            ),
                        }
                    )
    return tuple(sorted(findings, key=lambda f: (f["kind"], f["subject"])))


def build_migration_impact_manifest(
    *,
    fixture_identity: str,
    canonical_fixture: Sequence[Mapping[str, Any]],
    observations: Sequence[Any],
    proposed_mappings: Sequence[ProposedSourceMapping],
    dependent_stores: Mapping[str, Any] | None = None,
    dataset_identity: str = "",
) -> dict:
    """
    构建**只读、确定性**的迁移影响清单（绝不写文件、绝不执行任何操作）。

    清单显式区分六种影响分类，并带 ``executable: False`` 与
    ``kind = READ_ONLY_MIGRATION_IMPACT_ANALYSIS`` 标记。
    """
    stores = dict(dependent_stores or {})
    canonical_ids = sorted(
        {str(m.get("id") or "") for m in canonical_fixture if m.get("id")}
    )
    canonical_id_set = set(canonical_ids)
    baseline = fixture_baseline_fingerprint(canonical_fixture)

    store_references = enumerate_store_references(stores)
    chain_findings = _chain_inconsistencies(stores)
    derived_warnings = derived_identity_warnings(stores)

    # --- 冲突：同一旧别名 -> 多个新目标；多个旧别名 -> 同一新目标 ------------
    by_legacy: dict[str, set[str]] = {}
    by_proposed: dict[str, set[str]] = {}
    for mapping in proposed_mappings:
        by_legacy.setdefault(mapping.legacy_source_id, set()).add(
            mapping.proposed_source_id
        )
        by_proposed.setdefault(mapping.proposed_source_id, set()).add(
            mapping.legacy_source_id
        )
    conflicting_legacy = sorted(
        legacy for legacy, targets in by_legacy.items() if len(targets) > 1
    )
    conflicting_proposed = sorted(
        proposed for proposed, sources in by_proposed.items() if len(sources) > 1
    )

    # --- 每条映射的条目 -----------------------------------------------------
    entries: list[dict] = []
    for mapping in sorted(
        proposed_mappings, key=lambda m: (m.legacy_source_id, m.proposed_source_id)
    ):
        flags: list[str] = []
        affected_references: dict[str, list[str]] = {}

        for name in STORE_ORDER:
            store_entry = store_references[name]
            pointers = store_entry["references"].get(mapping.legacy_source_id)
            if pointers:
                affected_references[name] = list(pointers)

        # 未解析引用：旧别名在 Canonical 集合中不存在
        if (
            mapping.existing_canonical_id
            and mapping.existing_canonical_id not in canonical_id_set
        ):
            flags.append("EXISTING_CANONICAL_ID_NOT_IN_FIXTURE")
        if mapping.legacy_source_id not in canonical_id_set and not affected_references:
            flags.append("LEGACY_ALIAS_NOT_REFERENCED_BY_FIXTURE")

        # 冲突
        if mapping.legacy_source_id in conflicting_legacy:
            flags.append(CLASSIFICATION_AMBIGUOUS_MAPPING)
        if mapping.proposed_source_id in conflicting_proposed:
            flags.append(CLASSIFICATION_AMBIGUOUS_MAPPING)

        # 与既有 Canonical ID 碰撞：新别名不得占用一个已被别的赛事使用的字符串
        collision = mapping.proposed_source_id in canonical_id_set and (
            mapping.proposed_source_id != mapping.existing_canonical_id
        )
        if collision:
            flags.append("PROPOSED_SOURCE_ID_COLLIDES_WITH_EXISTING_CANONICAL_ID")

        # 证据充分性
        unsupported = (
            not identity_kind_is_sufficient(mapping.identity_kind)
            or (
                mapping.identity_kind == "RAW_PROVIDER_ID"
                and mapping.provider_id_uniqueness_scope
                != ProviderIdUniquenessScope.VERIFIED_SCOPED.value
            )
        )
        if unsupported:
            flags.append(CLASSIFICATION_UNSUPPORTED_EVIDENCE)

        # 分类：Canonical ID 一律保持不变 -> UNCHANGED_CANONICAL_ID 是基线分类
        classifications = [CLASSIFICATION_UNCHANGED_CANONICAL_ID]
        if affected_references:
            classifications.append(CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT)
        if not affected_references:
            classifications.append(CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY)
        classifications.extend(
            f for f in flags if f in ALL_IMPACT_CLASSIFICATIONS
        )
        classifications.append(CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION)

        entries.append(
            {
                "legacy_source_id": mapping.legacy_source_id,
                "existing_canonical_id": mapping.existing_canonical_id,
                "proposed_future_source_identity": mapping.proposed_source_id,
                "proposed_source_id_kind": classify_market_source_id(
                    mapping.proposed_source_id
                ),
                "legacy_source_id_kind": classify_market_source_id(
                    mapping.legacy_source_id
                ),
                "mapping_proposal_reference": mapping.mapping_proposal_reference,
                "binding_key_contract_version": mapping.binding_key_contract_version,
                "identity_kind": mapping.identity_kind,
                "identity_value": mapping.identity_value,
                "provider_id_uniqueness_scope": mapping.provider_id_uniqueness_scope,
                "observation_fingerprints": sorted(mapping.observation_fingerprints),
                "classifications": sorted(set(classifications)),
                "flags": sorted(set(flags)),
                "affected_evidence_references": {
                    name: affected_references[name] for name in sorted(affected_references)
                },
                "missing_references": sorted(
                    name for name in STORE_ORDER if not store_references[name]["present"]
                ),
                "legacy_compatibility_classification": (
                    "LEGACY_SHARED_NAMESPACE_PRESERVED"
                    if classify_market_source_id(mapping.legacy_source_id)
                    == MarketSourceIdKind.LEGACY_SHARED.value
                    else "NO_LEGACY_SHARED_NAMESPACE"
                ),
                "estimated_rewrite_scope": {
                    "canonical_id_rewrites": 0,
                    "source_alias_updates": 1,
                    "historical_reference_updates": sum(
                        len(v) for v in affected_references.values()
                    ),
                    "stores_with_references": sorted(affected_references),
                },
            }
        )

    # --- 未知 provider 证据 -------------------------------------------------
    unknown_provider_evidence: list[dict] = []
    for observation in observations:
        raw_id = getattr(observation, "raw_provider_id", None)
        scope = getattr(
            observation, "provider_id_uniqueness_scope", None
        ) or getattr(observation, "raw_provider_id_uniqueness_scope", None)
        if raw_id is None:
            continue
        if scope == ProviderIdUniquenessScope.VERIFIED_SCOPED.value:
            continue
        unknown_provider_evidence.append(
            {
                "observation_id": str(getattr(observation, "observation_id", "")),
                "adapter": str(getattr(observation, "adapter", "")),
                "raw_provider_id": str(raw_id),
                "raw_provider_id_type": str(
                    getattr(observation, "raw_provider_id_type", "")
                ),
                "provider_id_uniqueness_scope": str(scope or "UNKNOWN"),
            }
        )
    unknown_provider_evidence.sort(
        key=lambda item: (item["adapter"], item["observation_id"])
    )

    unresolved_references = sorted(
        {
            mid
            for name in STORE_ORDER
            for mid in store_references[name]["references"]
            if mid not in canonical_id_set
        }
    )

    cross_sport_collisions = [
        conflict.as_dict()
        for conflict in detect_cross_sport_namespace_collision(list(observations))
    ]
    all_conflicts = [conflict.as_dict() for conflict in detect_all_conflicts(list(observations))]

    manifest = {
        "kind": MANIFEST_KIND,
        "executable": False,
        "execution_api": None,
        "manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "market_source_id_contract_version": MARKET_SOURCE_ID_CONTRACT_VERSION,
        "preferred_market_source_id_option": PREFERRED_MARKET_SOURCE_ID_OPTION_ID,
        "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
        "fixture_identity": fixture_identity,
        "dataset_identity": dataset_identity,
        "baseline_fingerprint": baseline,
        "canonical_id_count": len(canonical_ids),
        "canonical_ids": canonical_ids,
        "source_observation_fingerprints": sorted(
            str(getattr(o, "semantic_fingerprint", lambda: "")())
            for o in observations
        ),
        "proposed_mapping_count": len(proposed_mappings),
        "entries": entries,
        "conflicting_mappings": {
            "legacy_alias_with_multiple_targets": conflicting_legacy,
            "proposed_source_id_with_multiple_sources": conflicting_proposed,
        },
        "missing_references": {
            "unresolved_store_references": unresolved_references,
            "absent_stores": sorted(
                name for name in STORE_ORDER if not store_references[name]["present"]
            ),
        },
        "unknown_provider_evidence": unknown_provider_evidence,
        "legacy_cross_sport_collisions": cross_sport_collisions,
        "source_identity_conflicts": all_conflicts,
        "store_reference_inventory": store_references,
        "chain_inconsistencies": list(chain_findings),
        "derived_identity_warnings": list(derived_warnings),
        "classification_histogram": _classification_histogram(entries),
        "authorization_status": {
            "production_mapping_write_authorization": (
                PRODUCTION_MAPPING_WRITE_AUTHORIZATION
            ),
            "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
            "historical_canonical_migration": "NOT_AUTHORIZED",
            "manifest_is_owner_approval": False,
            "manifest_is_production_authorization": False,
        },
        "forbidden_operations": list(FORBIDDEN_MANIFEST_KEYS),
        "non_executable_marker": (
            "This manifest is a read-only analysis artifact. It contains no migration "
            "operation and grants no authorization."
        ),
    }
    assert_not_executable(manifest)
    return manifest


def _classification_histogram(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    histogram: dict[str, int] = {name: 0 for name in ALL_IMPACT_CLASSIFICATIONS}
    for entry in entries:
        for name in entry.get("classifications", ()):
            histogram[name] = histogram.get(name, 0) + 1
    return histogram


def assert_not_executable(manifest: Mapping[str, Any]) -> None:
    """
    断言清单**不含任何可执行迁移路径**（fail closed）。

    检查：显式标记、禁止键、以及任何可调用对象。
    """
    if manifest.get("executable") is not False:
        raise ValueError("a migration impact manifest must never be executable")
    if manifest.get("kind") != MANIFEST_KIND:
        raise ValueError("a migration impact manifest must declare its analysis-only kind")
    if manifest.get("execution_api") is not None:
        raise ValueError("a migration impact manifest must not expose an execution API")
    lowered = {str(key).lower() for key in manifest}
    offenders = sorted(lowered & {k.lower() for k in FORBIDDEN_MANIFEST_KEYS})
    if offenders:
        raise ValueError(
            f"a migration impact manifest must not contain executable keys: {offenders}"
        )
    for key, value in manifest.items():
        if callable(value):
            raise ValueError(f"manifest key {key!r} exposes a callable execution path")


def manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    """清单的确定性指纹（用于与所有者批准绑定；本函数不签名、不批准）。"""
    return fingerprint(dict(manifest))


def manifest_json(manifest: Mapping[str, Any]) -> str:
    """清单的规范 JSON（便于审计比对；本函数不写任何文件）。"""
    return canonical_json(dict(manifest))
