"""
Canonical 身份摄取预检（Ingestion Identity Preflight）
====================================================
本模块是**唯一权威、可复用**的摄取身份边界：它决定一批incoming源记录
是否允许被准入 Canonical 集合。

位置
----
在「源行采集 + 候选构造」之后、在**任何持久化下游写入**之前执行：

    Source Rows -> Parser / Adapters -> Candidate Construction
        -> validate_ingestion_identity()        # 本模块
            -> VALID        -> Market Admission -> Canonical Merge -> Downstream
            -> AMBIGUOUS    -> FAIL CLOSED（零持久化写入）

设计约束
--------
1. **复用既有比较语义。** 事件判等一律走 ``utils.match_identity.same_event``
   （强竞彩编号优先、双方均有强编号时绝不退化为队名、否则精确主客队对）。
   本模块不引入任何新的判等规则、模糊队名匹配、俱乐部别名词典或启发式合并。
2. **顺序无关（deterministic）。** 冲突判定基于集合/分组，而非「谁先被处理」。
   同一批记录的任意排列必须得到同一冲突类别。
3. **只读。** 预检不修改任何输入记录。
4. **Fail closed。** 任何无法唯一确定归属的批次整体拒绝，不静默隔离、不做部分提交。

Canonical 所有权契约
--------------------
- 已持久化的 Canonical ID 一旦存在就不得被改写；incoming 源 ID 只能用于
  「识别 / 对账」事件，绝不能静默替换既有 Canonical ID。
- 每个被接受的 incoming 事件必须恰好对应**一个** Canonical 所有者，
  或者成为一个使用**未被占用** Canonical ID 的全新事件。
"""
from __future__ import annotations

from typing import Any, Sequence

from utils.match_identity import (
    competition_event_key,
    same_event,
    team_event_key,
)


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


def source_namespace_of(match_id: Any) -> str | None:
    """
    从 ID 字面量切出源命名空间：``'500lq-2030-05-01-301'`` -> ``'500lq'``。

    这只是对已持久化标识符的**字面切分**，不推断源数据中不存在的来源信息。
    """
    if not isinstance(match_id, str):
        return None
    head = match_id.split("-", 1)[0].strip()
    return head or None


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


def validate_ingestion_identity(existing: Sequence[dict], incoming: Sequence[dict]) -> None:
    """
    摄取身份预检：批次整体通过或整体拒绝。

    检查项
    ------
    1. 既有 Canonical 集合自身的 ID 合法性（缺失 / 类型 / 唯一）。
    2. incoming 记录的结构与身份充分性。
    3. **批次内源别名一致性**：同一源 ID 不得在同一批中claim不同事件。
    4. **既有别名归属兼容性**：incoming 源 ID 若已被某 Canonical 记录占用，
       则二者必须判等；否则拒绝。
    5. **Canonical 所有者唯一性**：一条 incoming 事件不得同时匹配多个 Canonical 所有者。
    6. **非传递身份一致性**：两条被判等的 incoming 记录必须映射到同一组所有者；
       否则该批次存在本质歧义（强编号与队名回退不可传递），整体拒绝。

    本函数不修改任何输入。
    """
    # --- 1. 既有 Canonical 记录 ------------------------------------------
    existing_index_by_id: dict[str, int] = {}
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
    alias_groups: dict[str, list[tuple[int, dict]]] = {}
    for idx, inc, norm in parsed:
        if norm is not None:
            alias_groups.setdefault(norm, []).append((idx, inc))

    for alias in sorted(alias_groups):
        members = alias_groups[alias]
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if not same_event(members[i][1], members[j][1]):
                    raise CanonicalIdentityCollisionError(
                        alias,
                        "SAME_SOURCE_ALIAS_DIFFERENT_EVENT",
                        "Source alias reused for different events within one batch",
                        source_namespace=source_namespace_of(alias),
                        incoming_index=members[j][0],
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

    # --- 5. Canonical 所有者唯一性 ----------------------------------------
    owners: list[list[int]] = []
    for _idx, inc, _norm in parsed:
        owners.append([i for i, ex in enumerate(existing) if same_event(ex, inc)])

    for pos, matched in enumerate(owners):
        if len(matched) > 1:
            idx, _inc, norm = parsed[pos]
            raise CanonicalIdentityCollisionError(
                norm or "NONE",
                "AMBIGUOUS_CANONICAL_OWNERSHIP",
                "Incoming event matches multiple canonical owners",
                source_namespace=source_namespace_of(norm),
                incoming_index=idx,
                competing_canonical_ids=[
                    str(existing[i].get("id") or "") for i in matched
                ],
                evidence_type="multi_owner",
                reason="ambiguous_canonical_ownership",
            )

    # --- 6. 非传递身份一致性 ----------------------------------------------
    for i in range(len(parsed)):
        for j in range(i + 1, len(parsed)):
            if not same_event(parsed[i][1], parsed[j][1]):
                continue
            if set(owners[i]) == set(owners[j]):
                continue
            idx_i, _inc_i, norm_i = parsed[i]
            idx_j, _inc_j, norm_j = parsed[j]
            competing = sorted(
                {str(existing[k].get("id") or "") for k in (owners[i] + owners[j])}
            )
            raise CanonicalIdentityCollisionError(
                norm_j or norm_i or "NONE",
                "AMBIGUOUS_CANONICAL_OWNERSHIP",
                "Two incoming records claim the same event but resolve to different canonical owners",
                source_namespace=source_namespace_of(norm_j or norm_i),
                incoming_index=idx_j,
                competing_canonical_ids=competing,
                evidence_type="non_transitive_identity",
                reason="ambiguous_canonical_ownership",
            )


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
