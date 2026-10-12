"""
Canonical 身份摄取预检（Ingestion Identity Preflight）
====================================================
本模块是**唯一权威、可复用**的摄取身份边界：它决定一批 incoming 源记录
是否允许被准入 Canonical 集合，并为每个被接受的事件确定**唯一、顺序无关**
的 Canonical 归属。

位置
----
在「源行采集 + 候选构造」之后、在**任何持久化下游写入**之前执行：

    Source Rows -> Parser / Adapters -> Candidate Construction
        -> plan_ingestion_identity() / validate_ingestion_identity()   # 本模块
            -> VALID        -> Market Admission -> Canonical Merge -> Downstream
            -> AMBIGUOUS    -> FAIL CLOSED（零持久化写入）

设计约束
--------
1. **复用既有比较语义。** 事件判等一律走 ``utils.match_identity.same_event``
   （强竞彩编号优先、双方均有强编号时绝不退化为队名、否则精确主客队对）。
   本模块不引入任何新的判等规则、模糊队名匹配、俱乐部别名词典或启发式合并。
2. **same_event 不可传递。** 因此身份分析基于**身份图连通分量 + 团校验**，
   而不是「链式 pairwise 命中即视为同一事件」。
3. **顺序无关（deterministic）。** 冲突判定与首个 Canonical ID 的选择都只依赖
   记录内容，不依赖行序；同一批记录的任意排列必须得到同一结果。
4. **只读。** 预检不修改任何输入记录。
5. **Fail closed。** 任何无法唯一确定归属的批次整体拒绝，不静默隔离、不做部分提交。

Canonical 所有权契约
--------------------
- 已持久化的 Canonical ID 一旦存在就不得被改写；incoming 源 ID 只能用于
  「识别 / 对账」事件，绝不能静默替换既有 Canonical ID。
- 每个被接受的 incoming 事件必须恰好对应**一个** Canonical 所有者，
  或者成为一个使用**未被占用** Canonical ID 的全新事件。

首个（first-seen）事件的 Canonical ID 选择契约
----------------------------------------------
身份确立之后，才允许选择标识符；选择规则完全由记录内容决定：

1. 若该事件的源记录中至少有一条携带源 ID，则按**已声明的源命名空间优先级**
   （见 ``SOURCE_NAMESPACE_TIERS``）取最优者；同一层级内以源 ID 字符串
   字典序最小者胜出。
2. 若该事件的源记录全部缺少源 ID，则沿用既有合成格式
   ``{sport}-{date}-{ordinal}``，其中 ``ordinal = len(existing) + rank``，
   ``rank`` 为该批次内所有「无源 ID 新事件」按内容键排序后的序号。

源命名空间优先级**只用于在身份确立后挑选标识符**，绝不作为身份证据；
它也绝不是「行序」，更不是「源受欢迎程度」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from utils.match_identity import (
    competition_event_key,
    competition_number_for_match,
    same_event,
    team_event_key,
)

#: 已声明的源命名空间优先级（层级越小越优先）。
#: 这是 Phase 2-A 之前**事实存在**的行为（live 行先于 market / finished 行进入批次）
#: 的显式化，仅用于在身份确立之后挑选首个 Canonical ID。
SOURCE_NAMESPACE_TIERS: dict[str, int] = {
    "500l": 0,   # live 即时比分（足球）
    "500lq": 0,  # live 即时比分（篮球）
    "500j": 1,   # 竞彩市场 XML（jczq / jclq）
    "500w": 2,   # 完场 feed
}

#: 未在表中声明的命名空间统一落入的最低层级（同层内按源 ID 字典序决胜）。
DEFAULT_SOURCE_NAMESPACE_TIER = 3


class CanonicalIdentityCollisionError(Exception):
    """
    Canonical 身份冲突的主要异常抽象。

    向后兼容：``category`` 与 ``match_id`` 保持可直接读取。
    其余字段为**附加**诊断上下文，仅用于日志排查与离线回归，
    不引入任何数据库 / 审核队列 / 持久化 schema。

    诊断字段
    --------
    - ``category``：冲突类别（既有类别保持原样）
    - ``match_id``：涉及的 Canonical ID 或源别名
    - ``source_namespace``：源命名空间（由 ID 前缀字面切分，不推断来源）
    - ``incoming_index``：该 incoming 记录在本批中的下标
    - ``competing_canonical_ids``：竞争/相关的 Canonical ID 列表
    - ``evidence_type``：判定所依据的证据类型
    - ``reason``：稳定的原因说明
    """

    def __init__(
        self,
        match_id: str,
        category: str,
        message: str,
        *,
        source_namespace: str | None = None,
        incoming_index: int | None = None,
        competing_canonical_ids: Sequence[str] | None = None,
        evidence_type: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(f"Collision [{category}] for ID {match_id}: {message}")
        self.match_id = match_id
        self.category = category
        self.source_namespace = source_namespace
        self.incoming_index = incoming_index
        self.competing_canonical_ids = list(competing_canonical_ids or [])
        self.evidence_type = evidence_type
        self.reason = reason if reason is not None else message


# ---------------------------------------------------------------------------
# 解析计划（plan）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IdentityComponent:
    """一个 incoming 身份连通分量（已通过团校验）。"""

    member_indices: tuple[int, ...]
    owner_index: int | None
    canonical_id: str

    @property
    def is_new_event(self) -> bool:
        return self.owner_index is None


@dataclass(frozen=True)
class IncomingIdentityPlan:
    """
    一批 incoming 记录的确定性归属解析结果。

    - ``components``：按内容键排序的连通分量（顺序无关）
    - ``canonical_id_by_index``：与 incoming 下标对齐的 Canonical ID
    - ``owner_index_by_index``：与 incoming 下标对齐的既有所有者下标（新事件为 None）
    """

    components: tuple[IdentityComponent, ...]
    canonical_id_by_index: tuple[str, ...]
    owner_index_by_index: tuple[int | None, ...]

    def canonical_id_for(self, index: int) -> str:
        return self.canonical_id_by_index[index]

    def owner_index_for(self, index: int) -> int | None:
        return self.owner_index_by_index[index]

    @property
    def new_components(self) -> tuple[IdentityComponent, ...]:
        return tuple(c for c in self.components if c.is_new_event)

    @property
    def new_canonical_ids(self) -> tuple[str, ...]:
        return tuple(c.canonical_id for c in self.new_components)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def source_namespace_of(match_id: Any) -> str | None:
    """
    从 ID 字面量切出源命名空间：``'500lq-2030-05-01-301'`` -> ``'500lq'``。

    这只是对已持久化标识符的**字面切分**，不推断源数据中不存在的来源信息。
    """
    if not isinstance(match_id, str):
        return None
    head = match_id.split("-", 1)[0].strip()
    return head or None


def source_namespace_tier(match_id: Any) -> int:
    """源命名空间优先级层级（越小越优先）。"""
    return SOURCE_NAMESPACE_TIERS.get(
        source_namespace_of(match_id) or "", DEFAULT_SOURCE_NAMESPACE_TIER
    )


def _content_key(match: dict) -> tuple[str, str, str, str, str]:
    """记录的内容键：只依赖记录字段，绝不依赖行序。"""
    return (
        str(match.get("sport") or ""),
        str(match.get("date") or ""),
        str(match.get("home") or ""),
        str(match.get("away") or ""),
        str(competition_number_for_match(match) or ""),
    )


def _bucket_key(match: dict) -> tuple[Any, Any]:
    """
    同事件判等的必要前置：sport 与 date 必须相等。

    因此按 ``(sport, date)`` 分桶是**等价**的优化，不改变任何判定结果，
    并把两两比较的代价从 O(n^2) 降到各桶内 O(b^2)。
    """
    return (match.get("sport"), match.get("date"))


def _incoming_id_or_raise(inc: dict, idx: int) -> str | None:
    """
    校验并归一化单条 incoming 记录的源 ID。

    - 无 ``id`` 键：要求具备充分的事件身份（强编号键或精确队名对键），否则拒绝。
    - ``id`` 存在但非字符串 / 去空白后为空：拒绝。

    返回归一化后的 ID，或 ``None``（记录无 ID 但身份充分）。
    """
    raw_id = inc.get("id")
    if raw_id is None:
        if not competition_event_key(inc) and not team_event_key(inc):
            raise CanonicalIdentityCollisionError(
                "NONE",
                "INADEQUATE_EVENT_IDENTITY",
                "Incoming match has missing ID and inadequate event identity fields",
                incoming_index=idx,
                evidence_type="none",
                reason="inadequate_event_identity",
            )
        return None

    if not isinstance(raw_id, str):
        raise CanonicalIdentityCollisionError(
            "NONE",
            "MALFORMED_INCOMING_ID",
            "Incoming ID has invalid type",
            incoming_index=idx,
            evidence_type="none",
            reason="malformed_incoming_id",
        )

    normalized = raw_id.strip()
    if not normalized:
        raise CanonicalIdentityCollisionError(
            "NONE",
            "MALFORMED_INCOMING_ID",
            "Incoming ID is empty",
            incoming_index=idx,
            evidence_type="none",
            reason="malformed_incoming_id",
        )
    return normalized


# ---------------------------------------------------------------------------
# 主入口：解析计划
# ---------------------------------------------------------------------------

def plan_ingestion_identity(
    existing: Sequence[dict], incoming: Sequence[dict]
) -> IncomingIdentityPlan:
    """
    解析一批 incoming 记录的 Canonical 归属，或在存在歧义时整体拒绝。

    检查项
    ------
    1. 既有 Canonical 集合自身的 ID 合法性（缺失 / 类型 / 唯一）。
    2. incoming 记录的结构与身份充分性。
    3. **批次内源别名一致性**：同一源 ID 不得在同一批中 claim 不同事件。
    4. **既有别名归属兼容性**：incoming 源 ID 若已被某 Canonical 记录占用，
       则二者必须判等；否则拒绝。
    5. **身份连通分量必须是团**：由 same_event 链连接起来的分量，若存在互不判等的
       成员（强编号互斥、却由缺号记录桥接），则该分量本质歧义 -> 拒绝。
    6. **分量内所有者一致性**：同一分量的所有成员必须匹配**同一组**既有 Canonical
       所有者（可为空集）；不一致 -> 拒绝。
    7. **所有者唯一性**：一条事件不得同时匹配多个既有 Canonical 所有者。
    8. **首个 Canonical ID 的确定性选择**（见模块 docstring）。

    本函数不修改任何输入。
    """
    # --- 1. 既有 Canonical 记录 ------------------------------------------
    existing_index_by_id: dict[str, int] = {}
    existing_buckets: dict[tuple[Any, Any], list[int]] = {}
    for idx, record in enumerate(existing):
        if not isinstance(record, dict):
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "MALFORMED_MATCH_ENTRY",
                "An existing canonical record is not an object",
                incoming_index=idx,
                evidence_type="canonical_record_shape",
                reason="malformed_existing_record",
            )
        raw_mid = record.get("id")
        if raw_mid is None:
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "AMBIGUOUS_SOURCE",
                "An existing canonical ID is missing",
                incoming_index=idx,
                evidence_type="canonical_id",
                reason="missing_canonical_id",
            )
        if not isinstance(raw_mid, str):
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "MALFORMED_ID_TYPE",
                "An existing canonical ID has an invalid type",
                incoming_index=idx,
                evidence_type="canonical_id",
                reason="malformed_canonical_id",
            )
        mid = raw_mid.strip()
        if not mid:
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "AMBIGUOUS_SOURCE",
                "An existing canonical ID is empty or whitespace-only",
                incoming_index=idx,
                evidence_type="canonical_id",
                reason="missing_canonical_id",
            )
        if mid in existing_index_by_id:
            raise CanonicalIdentityCollisionError(
                mid,
                "PREEXISTING_DUPLICATE",
                "Two canonical records share the same ID",
                source_namespace=source_namespace_of(mid),
                competing_canonical_ids=[mid],
                evidence_type="canonical_id_uniqueness",
                reason="preexisting_duplicate",
            )
        existing_index_by_id[mid] = idx
        existing_buckets.setdefault(_bucket_key(record), []).append(idx)

    # --- 2. incoming 结构与身份充分性 ------------------------------------
    parsed: list[tuple[int, dict, str | None]] = []
    for idx, inc in enumerate(incoming):
        if not isinstance(inc, dict):
            raise CanonicalIdentityCollisionError(
                f"inc_index_{idx}",
                "MALFORMED_INCOMING_RECORD",
                "Incoming record is not a dictionary",
                incoming_index=idx,
                evidence_type="incoming_record_shape",
                reason="malformed_incoming_record",
            )
        parsed.append((idx, inc, _incoming_id_or_raise(inc, idx)))

    # --- 3. 批次内源别名一致性（全对全，顺序无关） ------------------------
    alias_groups: dict[str, list[int]] = {}
    for pos, (_idx, _inc, norm) in enumerate(parsed):
        if norm is not None:
            alias_groups.setdefault(norm, []).append(pos)

    for alias in sorted(alias_groups):
        members = alias_groups[alias]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not same_event(parsed[members[i]][1], parsed[members[j]][1]):
                    raise CanonicalIdentityCollisionError(
                        alias,
                        "SAME_SOURCE_ALIAS_DIFFERENT_EVENT",
                        "Source alias reused for different events within one batch",
                        source_namespace=source_namespace_of(alias),
                        incoming_index=parsed[members[j]][0],
                        competing_canonical_ids=[alias],
                        evidence_type="source_alias_reuse",
                        reason="same_source_alias_different_event",
                    )

    # --- 4. 既有 Canonical 别名归属兼容性 --------------------------------
    for idx, inc, norm in parsed:
        if norm is None:
            continue
        owner_idx = existing_index_by_id.get(norm)
        if owner_idx is None:
            continue
        if not same_event(existing[owner_idx], inc):
            raise CanonicalIdentityCollisionError(
                norm,
                "SAME_SOURCE_ALIAS_DIFFERENT_EVENT",
                "Incoming alias is already owned by an incompatible canonical event",
                source_namespace=source_namespace_of(norm),
                incoming_index=idx,
                competing_canonical_ids=[norm],
                evidence_type="existing_alias_ownership",
                reason="same_source_alias_different_event",
            )

    # --- 5. 身份连通分量（union-find，仅在 (sport, date) 桶内建边） --------
    total = len(parsed)
    parent = list(range(total))

    def _find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    incoming_buckets: dict[tuple[Any, Any], list[int]] = {}
    for pos, (_idx, inc, _norm) in enumerate(parsed):
        incoming_buckets.setdefault(_bucket_key(inc), []).append(pos)

    for key in sorted(incoming_buckets, key=lambda k: (str(k[0]), str(k[1]))):
        members = incoming_buckets[key]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if same_event(parsed[members[i]][1], parsed[members[j]][1]):
                    _union(members[i], members[j])

    grouped: dict[int, list[int]] = {}
    for pos in range(total):
        grouped.setdefault(_find(pos), []).append(pos)

    def _component_sort_key(positions: list[int]) -> tuple:
        """分量排序键：只依赖记录内容，与行序无关。"""
        ids = sorted(n for n in (parsed[p][2] for p in positions) if n is not None)
        content = tuple(sorted(_content_key(parsed[p][1]) for p in positions))
        return (0 if ids else 1, ids[0] if ids else "", content)

    ordered_components = sorted(grouped.values(), key=_component_sort_key)

    # --- 6/7. 团校验、所有者一致性与唯一性 --------------------------------
    resolved: list[dict] = []
    for positions in ordered_components:
        for i in range(len(positions)):
            for j in range(i + 1, len(positions)):
                if not same_event(parsed[positions[i]][1], parsed[positions[j]][1]):
                    first_pos = positions[0]
                    raise CanonicalIdentityCollisionError(
                        parsed[first_pos][2] or "NONE",
                        "AMBIGUOUS_CANONICAL_OWNERSHIP",
                        "Identity component is not pairwise consistent: a missing strong "
                        "number bridges mutually incompatible strong-number events",
                        source_namespace=source_namespace_of(parsed[first_pos][2]),
                        incoming_index=parsed[positions[j]][0],
                        competing_canonical_ids=[
                            n for n in (parsed[p][2] for p in positions) if n
                        ],
                        evidence_type="non_transitive_identity",
                        reason="non_clique_component",
                    )

        owner_sets: list[tuple[int, ...]] = []
        for pos in positions:
            inc = parsed[pos][1]
            candidates = existing_buckets.get(_bucket_key(inc), ())
            owner_sets.append(
                tuple(i for i in candidates if same_event(existing[i], inc))
            )

        if len({s for s in owner_sets}) > 1:
            merged_owners = sorted({i for s in owner_sets for i in s})
            first_pos = positions[0]
            raise CanonicalIdentityCollisionError(
                parsed[first_pos][2] or "NONE",
                "AMBIGUOUS_CANONICAL_OWNERSHIP",
                "Members of one identity component resolve to different canonical owners",
                source_namespace=source_namespace_of(parsed[first_pos][2]),
                incoming_index=parsed[first_pos][0],
                competing_canonical_ids=[
                    str(existing[i].get("id") or "") for i in merged_owners
                ],
                evidence_type="non_transitive_identity",
                reason="component_owner_inconsistency",
            )

        owners = list(owner_sets[0])
        if len(owners) > 1:
            first_pos = positions[0]
            raise CanonicalIdentityCollisionError(
                parsed[first_pos][2] or "NONE",
                "AMBIGUOUS_CANONICAL_OWNERSHIP",
                "Incoming event matches multiple canonical owners",
                source_namespace=source_namespace_of(parsed[first_pos][2]),
                incoming_index=parsed[first_pos][0],
                competing_canonical_ids=[str(existing[i].get("id") or "") for i in owners],
                evidence_type="multi_owner",
                reason="ambiguous_canonical_ownership",
            )

        resolved.append(
            {
                "positions": positions,
                "sort_key": _component_sort_key(positions),
                "owner_index": owners[0] if owners else None,
                "canonical_id": None,
            }
        )

    # --- 8a. 有源 ID 的新事件：命名空间优先级 + 字典序（内容决定，与行序无关）--
    for comp in resolved:
        if comp["owner_index"] is not None:
            comp["canonical_id"] = str(existing[comp["owner_index"]].get("id") or "")
            continue
        source_ids = sorted(
            n for n in (parsed[p][2] for p in comp["positions"]) if n is not None
        )
        if source_ids:
            comp["canonical_id"] = min(
                source_ids, key=lambda mid: (source_namespace_tier(mid), mid)
            )

    # --- 8b. 无源 ID 的新事件：确定性合成序号 ------------------------------
    idless = [c for c in resolved if c["canonical_id"] is None]
    idless.sort(key=lambda c: c["sort_key"])
    for rank, comp in enumerate(idless):
        representative = min(
            (parsed[p][1] for p in comp["positions"]), key=_content_key
        )
        comp["canonical_id"] = (
            f"{representative.get('sport', 'f')}-{representative.get('date')}"
            f"-{len(existing) + rank}"
        )

    # --- 8c. 新 Canonical ID 不得与既有 ID 或彼此冲突 ----------------------
    new_ids = [c["canonical_id"] for c in resolved if c["owner_index"] is None]
    counts: dict[str, int] = {}
    for cid in new_ids:
        counts[cid] = counts.get(cid, 0) + 1
    duplicate = next(
        (cid for cid in new_ids if counts[cid] > 1 or cid in existing_index_by_id),
        None,
    )
    if duplicate is not None:
        raise CanonicalIdentityCollisionError(
            duplicate,
            "INCOMING_SHARED_ID",
            "New event would reuse an already-owned Canonical ID",
            source_namespace=source_namespace_of(duplicate),
            competing_canonical_ids=[duplicate],
            evidence_type="canonical_id_uniqueness",
            reason="incoming_shared_id",
        )

    # --- 组装计划 ---------------------------------------------------------
    canonical_by_index: list[str] = [""] * total
    owner_by_index: list[int | None] = [None] * total
    components: list[IdentityComponent] = []
    for comp in resolved:
        components.append(
            IdentityComponent(
                member_indices=tuple(parsed[p][0] for p in comp["positions"]),
                owner_index=comp["owner_index"],
                canonical_id=comp["canonical_id"],
            )
        )
        for pos in comp["positions"]:
            canonical_by_index[pos] = comp["canonical_id"]
            owner_by_index[pos] = comp["owner_index"]

    return IncomingIdentityPlan(
        components=tuple(components),
        canonical_id_by_index=tuple(canonical_by_index),
        owner_index_by_index=tuple(owner_by_index),
    )


def validate_ingestion_identity(existing: Sequence[dict], incoming: Sequence[dict]) -> None:
    """
    摄取身份预检：批次整体通过或整体拒绝。

    语义与 ``plan_ingestion_identity`` 完全一致；本函数是该边界对外暴露的
    「只校验、不消费计划」入口，供流水线在 merge 之前显式调用。
    """
    plan_ingestion_identity(existing, incoming)


def validate_canonical_uniqueness(
    merged: Sequence[dict],
    *,
    previous_ids: Sequence[str] | None = None,
    expected_total: int | None = None,
) -> None:
    """
    merge 之后的 Canonical ID 唯一性证明（附加防线，P1）。

    在**第一个下游持久化写入之前**执行，独立于既有启动守卫
    （启动守卫拒绝「既有重复 Canonical ID」，本函数拒绝「merge 产出重复 / 丢 ID / 计数不一致」）。

    校验项
    ------
    1. 每条 Canonical 记录都是 dict。
    2. 每个 Canonical ID 都是非空字符串。
    3. 每个 Canonical ID 唯一。
    4. 既有 Canonical ID 未被改写或丢失（``previous_ids`` 必须全部保留）。
    5. 记录条数与预期一致（``expected_total``）。

    本函数**不**自动修复任何历史重复；历史重复继续由启动守卫 fail closed。
    """
    seen: dict[str, int] = {}
    for idx, record in enumerate(merged):
        if not isinstance(record, dict):
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "MALFORMED_CANONICAL_MATCH",
                "A merged canonical record is not an object",
                incoming_index=idx,
                evidence_type="canonical_record_shape",
                reason="malformed_merged_record",
            )
        raw_mid = record.get("id")
        if not isinstance(raw_mid, str) or not raw_mid.strip():
            raise CanonicalIdentityCollisionError(
                f"index_{idx}",
                "MALFORMED_CANONICAL_ID",
                "A merged canonical ID is not a nonempty string",
                incoming_index=idx,
                evidence_type="canonical_id",
                reason="malformed_merged_id",
            )
        mid = raw_mid.strip()
        if mid in seen:
            raise CanonicalIdentityCollisionError(
                mid,
                "CANONICAL_UNIQUENESS_VIOLATION",
                "Two merged canonical records share the same ID",
                source_namespace=source_namespace_of(mid),
                competing_canonical_ids=[mid],
                evidence_type="canonical_id_uniqueness",
                reason="merged_id_not_unique",
            )
        seen[mid] = idx

    if previous_ids is not None:
        # 与 ``seen`` 的键保持同一归一化口径（strip），避免仅因空白差异误报 ID 丢失。
        missing = sorted(
            {
                str(x).strip()
                for x in previous_ids
                if str(x).strip() and str(x).strip() not in seen
            }
        )
        if missing:
            raise CanonicalIdentityCollisionError(
                missing[0],
                "CANONICAL_ID_LOST",
                "An existing canonical ID disappeared during the merge",
                source_namespace=source_namespace_of(missing[0]),
                competing_canonical_ids=missing,
                evidence_type="historical_preservation",
                reason="canonical_id_lost",
            )

    if expected_total is not None and len(merged) != expected_total:
        raise CanonicalIdentityCollisionError(
            "NONE",
            "CANONICAL_COUNT_MISMATCH",
            f"Merged canonical count {len(merged)} does not match expected {expected_total}",
            evidence_type="count_consistency",
            reason="canonical_count_mismatch",
        )
