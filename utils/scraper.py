"""
抓取调度器
==========
负责：
1. 调用 fetcher_500 抓取赛事（广域即时比分 + 竞彩赔率）
2. 市场准入：只把**有可用盘口覆盖**的比赛纳入 canonical 跟踪集合
3. 合并进 daily_matches.json（去重 + 增量更新）
4. 用完赛结果滚动校准球队 Elo
5. 定时任务入口

准入依据是市场证据，不是联赛名气——本模块不含任何联赛白名单 / 黑名单。
"""
from __future__ import annotations

import copy
import json
from datetime import datetime

import config
from utils.concurrency import acquire_refresh_lock, RefreshBusyError
from utils import fetcher_500
from utils import refresh_state
from utils.daily_loader import (
    add_time_status,
    enrich_match,
    get_beijing_now,
    load_json,
    save_json,
)
from utils.evaluation_rows import EvaluationIntegrityError, capture_evaluation_row
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    plan_ingestion_identity,
    validate_canonical_uniqueness,
    validate_ingestion_identity,
)
from utils.basketball_identity_evidence import (
    classify_cross_source_candidate,
    team_relation,
    OUTCOME_AMBIGUOUS,
    OUTCOME_CANDIDATE_SAME_EVENT,
    RELATION_EXACT,
    RELATION_NORMALIZED_EQUAL,
)
from utils.market_coverage import has_usable_market_odds
from utils.match_identity import competition_number_for_match, is_kickoff_time_known, same_event
from utils.match_lifecycle import (
    MatchLifecycleConflict,
    is_finalized_match,
    resolve_match_update,
    valid_full_time_score,
)
from utils.odds_snapshots import record_odds_snapshot
from utils.prediction_snapshots import capture_snapshot, get_snapshot, get_snapshots_for_match
from utils.settlements import (
    SettlementConflictError,
    get_settlements_for_match,
    settle_snapshot,
)
from utils.team_strength import update_from_result

DAILY_FILE = "daily_matches.json"


def _norm_no(value: str) -> str:
    """归一化竞彩编号：'周三302' / '302' / 302 -> '302'"""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or digits


def _match_key(match: dict) -> str:
    """
    canonical 比赛匹配键：`sport|date|time|home|away`。

    合并与市场准入共用同一个键构造，避免在多处重复字符串拼接。
    """
    return (
        f"{match.get('sport')}|{match.get('date')}|{match.get('time')}|"
        f"{match.get('home')}|{match.get('away')}"
    )


#: 赔率价格键：必须是严格大于 1.0 的有限数值才算有效。
_PRICE_ODDS_KEYS = frozenset(
    {"home_win", "away_win", "draw", "over", "under", "over_2_5", "under_2_5"}
)

#: 盘口线键：只要求有限数值（不要求 > 1.0）。
_LINE_ODDS_KEYS = frozenset({"total_line"})


def _odds_value_acceptable(key: str, value: object) -> bool:
    """
    某个键的取值是否**已经可用**。

    - 价格键：`is_valid_price`（0 / 1.0 / NaN / inf / 字符串一律不算）；
    - 盘口线键：`is_finite_number`；
    - 其它元数据键（如 `matchnum`）：非 None 即可。

    这样「已有的无效值」不会阻止一个独立验证过的有效值入册，
    而「已有的有效值」仍然不会被覆盖。
    """
    from utils.market_coverage import is_finite_number, is_valid_price

    if key in _PRICE_ODDS_KEYS:
        return is_valid_price(value)
    if key in _LINE_ODDS_KEYS:
        return is_finite_number(value)
    return value is not None


def _fill_missing_odds(target: dict, source: dict) -> bool:
    """
    把 ``source`` 中**可用**的盘口键补进 ``target``。

    规则：
    - 新值本身无效（None / 空串 / 非法价格 / 非法盘口线）→ 不写入；
    - 目标已有**有效**值 → 绝不覆盖（不为了让价格"标准化"而改写）；
    - 目标缺失或只有**无效**值（0、1.0、NaN、inf、非数值字符串）→ 用有效新值替换。

    直播源与竞彩 XML 是两类互补市场（直播给胜负，XML 的 sf 给胜负、dxf 给大小分）。
    既有实现只要目标已有任意盘口就整行跳过，导致直播行永远拿不到 dxf 大小分。
    """
    changed = False
    for key, value in source.items():
        if value is None or value == "":
            continue
        if not _odds_value_acceptable(key, value):
            continue
        if _odds_value_acceptable(key, target.get(key)):
            continue
        target[key] = value
        changed = True
    return changed


def _attach_odds(
    matches: list[dict],
    odds_rows: list[dict],
    diagnostics: dict | None = None,
) -> tuple[int, list[dict]]:
    """
    把竞彩赔率挂到已有赛程上。
    优先级：
    1. 强赛事编号 (sport + date + 归一化编号)
    2. 主客队对 fallback (sport + date + home + away)
    不使用开赛时间，不单独使用 (date, home)。

    合并口径（见 AGENTS.md §4.7）：**已有值优先，只补缺失键**。
    直播行自带胜负盘，XML 的 dxf 大小分因此可以补入；已有完整覆盖时不会有任何改动。

    方向守卫（Phase 6-B）：盘口行自带主客队名；挂载前按 ``team_relation`` 复核
    行主客与目标记录主客的关系：
    - 两位一致（EXACT / NORMALIZED_EQUAL）→ 正常挂载；
    - 两位**恰好互换**（主行=目标客、客行=目标主）→ **跳过整行**并计数
      （``REVERSED_DIRECTIONAL_ODDS_NOT_ATTACHED``）：编号相等但主客互换的行
      不得向任何方向的盘口键写入价格，更不得交换价格；
    - 其余（拼写变体 / 无法判定）→ 保持既有挂载行为（编号是 provider 写下的
      身份链路，变体不得独自否决挂载），但计入
      ``variant_orientation_rows_attached`` 供只读审计。

    ``diagnostics`` 为可选的调用方字典；传入时函数会就地写入：
    ``reversed_orientation_rows_skipped``、``variant_orientation_rows_attached``、
    ``reversed_rows`` / ``variant_rows``（有界证据列表：行 id、目标 id、队名关系）。

    返回 (被补充盘口的场次数, 被消费的赔率源行列表)。
    被消费的行不再作为兜底候选，避免同一市场事件重复入册；
    被方向守卫跳过的行**不**标记为已消费，交由后续准入防线处置。
    """
    count = 0
    used: list[dict] = []
    used_ids: set[int] = set()

    if diagnostics is None:
        diagnostics = {}
    diagnostics.setdefault("reversed_orientation_rows_skipped", 0)
    diagnostics.setdefault("variant_orientation_rows_attached", 0)
    diagnostics.setdefault("reversed_rows", [])
    diagnostics.setdefault("variant_rows", [])

    def _orientation_relations(m: dict, r: dict) -> tuple[str, str, str, str]:
        home_direct = team_relation(m.get("home"), r.get("home"))
        away_direct = team_relation(m.get("away"), r.get("away"))
        home_rev = team_relation(m.get("home"), r.get("away"))
        away_rev = team_relation(m.get("away"), r.get("home"))
        return home_direct, away_direct, home_rev, away_rev

    for m in matches:
        # 同一场竞彩赛事会有**多个玩法行**（篮球 sf 胜负 + dxf 大小分；
        # 足球 spf + nspf）。必须全部消费，否则大小分永远补不上。
        matched_rows = [
            r for r in odds_rows
            if id(r) not in used_ids and same_event(m, r) and r.get("odds")
        ]
        if not matched_rows:
            continue

        target = m.get("odds") if isinstance(m.get("odds"), dict) else {}
        touched = False
        for r in matched_rows:
            home_direct, away_direct, home_rev, away_rev = _orientation_relations(m, r)
            same_labels = (RELATION_EXACT, RELATION_NORMALIZED_EQUAL)
            if home_rev in same_labels and away_rev in same_labels and not (
                home_direct in same_labels and away_direct in same_labels
            ):
                # 主客恰好互换：任何方向的盘口键都不得写入，绝不交换价格。
                diagnostics["reversed_orientation_rows_skipped"] += 1
                if len(diagnostics["reversed_rows"]) < 50:
                    diagnostics["reversed_rows"].append({
                        "odds_row_id": r.get("id"),
                        "target_id": m.get("id"),
                        "odds_row_home": r.get("home"),
                        "odds_row_away": r.get("away"),
                        "target_home": m.get("home"),
                        "target_away": m.get("away"),
                        "reason": "REVERSED_DIRECTIONAL_ODDS_NOT_ATTACHED",
                    })
                continue

            source = r["odds"]
            if not target:
                target.update(source)
                touched = True
            elif _fill_missing_odds(target, source):
                touched = True

            used.append(r)
            used_ids.add(id(r))
            if not (
                home_direct in same_labels and away_direct in same_labels
            ):
                # 拼写变体 / 无法判定的方向：挂载保持既有行为（编号身份链路），
                # 但显式留痕；不得据此断言任何别名等价。
                diagnostics["variant_orientation_rows_attached"] += 1
                if len(diagnostics["variant_rows"]) < 50:
                    diagnostics["variant_rows"].append({
                        "odds_row_id": r.get("id"),
                        "target_id": m.get("id"),
                        "home_relation": home_direct,
                        "away_relation": away_direct,
                        "reason": "VARIANT_ORIENTATION_ATTACHED_BY_NUMBER",
                    })
            # 用赔率源的联赛名补全（更规范）
            if r.get("league") and not m.get("league"):
                m["league"] = r["league"]
            if r.get("jczq_no") and not m.get("jczq_no"):
                m["jczq_no"] = r["jczq_no"]

        if touched:
            m["odds"] = target
            count += 1
    return count, used


def _append_unmatched_market_candidates(
    incoming: list[dict],
    market_rows: list[dict],
    used_market_rows: list[dict],
) -> int:
    """
    把「未被挂载」但自带可用盘口的赔率行作为兜底候选追加进 incoming，返回追加条数。

    必须防止重复：与既有 incoming 行（含 live 行）属于 same_event 的一律跳过，
    同一真实事件至多产生一条 incoming 候选。
    """
    used_ids = {id(row) for row in used_market_rows}
    added = 0
    for row in market_rows:
        if id(row) in used_ids:
            continue
        if not has_usable_market_odds(row):
            continue
        if any(same_event(row, inc) for inc in incoming):
            continue
        incoming.append(dict(row))
        added += 1
    return added


def _prepare_existing_market_universe(existing: list[dict]) -> tuple[list[dict], int]:
    """
    迁移既有 canonical 集合，使其只保留被跟踪的比赛。

    - `market_tracked` 为 true -> 保留（并保持标记）。
    - 无标记但自身带可用盘口 -> 打上 `market_tracked = true` 后保留。
    - 两者皆无（历史上被广泛抓取进来的无盘口赛事）-> 从 canonical 日常集合移除。

    返回 (保留集合, 移除条数)。
    只影响 `daily_matches.json`；历史不可变存储（预测快照 / 赔率历史 / 结算 /
    评估样本）一律不动。
    """
    kept: list[dict] = []
    removed = 0
    for m in existing:
        if m.get("market_tracked") is True or has_usable_market_odds(m):
            m["market_tracked"] = True
            kept.append(m)
        else:
            removed += 1
    return kept, removed


def _cross_source_duplicate_risk(candidate: dict, counterpart: dict) -> bool:
    """
    判定一条**首次出现**的候选是否与既有记录构成「跨源重复候选」。

    复用既有候选级验证器 ``classify_cross_source_candidate``（绝不另建第二套
    解释器）。触发条件从严（全部满足才触发）：

    - 同运动、同日期、两队名可比（分类器内部保证）；
    - 双方原始编号都能解释为可比较形态（``numbers_comparable``）；
    - 两种书写形态解析到**同一个编码式编号**（``numbers_agree is True``）——
      例如 ``6301``（编码式）与 ``周六301``（展示式）；
    - 候选结论是 ``CANDIDATE_SAME_EVENT`` 或 ``AMBIGUOUS``。
      ``CONTRADICTORY_EVIDENCE`` / ``CANDIDATE_DIFFERENT_EVENT`` 一律**不**触发
      （编号互斥或队名互斥 = 真实不同事件，继续走既有准入）。

    这里做的是**准入级 fail-closed**（拒绝制造第二条跟踪记录），不是归并：
    既不删除、不改写、不选择存活 ID，也不改写 ``same_event()`` 语义。
    ``1001`` vs ``2001`` 这类同日期同序号不同星期前缀的真实不同事件
    （``numbers_agree is False``）永不触发。
    """
    evidence = classify_cross_source_candidate(candidate, counterpart)
    if not evidence.numbers_comparable or evidence.numbers_agree is not True:
        return False
    return evidence.outcome in (OUTCOME_CANDIDATE_SAME_EVENT, OUTCOME_AMBIGUOUS)


def _admit_market_candidates(
    candidates: list[dict],
    existing: list[dict],
    diagnostics: dict | None = None,
) -> tuple[list[dict], int]:
    """
    市场准入：决定哪些候选可以进入 canonical 跟踪集合。

    - 已在跟踪集合中（same_event 命中已跟踪事件）-> 准入：接受状态 / 比分 / 开赛时间更新，
      即使本轮没有盘口（盘口源临时不可用时也必须保持生命周期连续性）。
    - 首次出现的全新候选 -> 只有具备可用盘口覆盖才准入，并打上 market_tracked = True；
      且必须先通过**跨源重复候选守卫**（Phase 6-B）：与既有跟踪记录或本批已准入
      候选构成「编号形态对应 + 候选级同事件/歧义」的候选一律拒绝准入
      （不产生第二条跟踪记录、不产生重复预测机会；历史记录不受影响）。
    - 其余 -> 拒绝（不进入 canonical 集合，也不写入任何下游历史）。

    ``diagnostics`` 为可选调用方字典；传入时就地写入
    ``cross_source_duplicate_candidates_rejected`` 与有界的
    ``cross_source_duplicate_evidence``（id、编号、结论、blockers）。

    返回 (准入列表, 拒绝条数)。
    """
    admitted: list[dict] = []
    rejected = 0
    if diagnostics is None:
        diagnostics = {}
    diagnostics.setdefault("cross_source_duplicate_candidates_rejected", 0)
    diagnostics.setdefault("cross_source_duplicate_evidence", [])

    # 跨源重复守卫的廉价预索引：只对 (sport, date) 相同的对做分类器检查，
    # 且仅当候选自带原始编号时才有触发可能。
    by_key: dict[tuple[str, str], list[dict]] = {}
    for ex in existing:
        key = (str(ex.get("sport") or ""), str(ex.get("date") or ""))
        by_key.setdefault(key, []).append(ex)

    for m in candidates:
        if any(same_event(m, ex) for ex in existing):
            admitted.append(m)
            continue
        if competition_number_for_match(dict(m)) is not None:
            counterpart_pool = by_key.get(
                (str(m.get("sport") or ""), str(m.get("date") or "")), []
            ) + [a for a in admitted if a.get("sport") == m.get("sport") and a.get("date") == m.get("date")]
            duplicate_hit = None
            for ex in counterpart_pool:
                if _cross_source_duplicate_risk(m, ex):
                    duplicate_hit = ex
                    break
            if duplicate_hit is not None:
                rejected += 1
                diagnostics["cross_source_duplicate_candidates_rejected"] += 1
                if len(diagnostics["cross_source_duplicate_evidence"]) < 50:
                    evidence = classify_cross_source_candidate(m, duplicate_hit)
                    diagnostics["cross_source_duplicate_evidence"].append({
                        "rejected_candidate_id": m.get("id"),
                        "counterpart_id": duplicate_hit.get("id"),
                        "sport": m.get("sport"),
                        "date": m.get("date"),
                        "candidate_number": competition_number_for_match(dict(m)),
                        "counterpart_number": competition_number_for_match(dict(duplicate_hit)),
                        "outcome": evidence.outcome,
                        "numbers_agree": evidence.numbers_agree,
                        "orientation": evidence.orientation,
                        "blockers": list(evidence.blockers),
                        "note": "CROSS_SOURCE_DUPLICATE_CANDIDATE_NOT_ADMITTED",
                    })
                continue
        if has_usable_market_odds(m):
            m["market_tracked"] = True
            admitted.append(m)
            continue
        rejected += 1
    return admitted, rejected


# Canonical 身份冲突异常由 utils.ingestion_identity 统一定义，这里通过上面的 import
# 重新导出，保持既有调用方 `from utils.scraper import CanonicalIdentityCollisionError` 可用。


def _record_changed(before: dict, after: dict) -> bool:
    """
    判定 canonical 记录是否真的发生变化（用于 updated 计数）。

    不能只用 Python 值相等：JSON 中 `2.0` / `true` / `false` 与 `2` / `1` / `0`
    是不同的持久化表示，但 Python 判定它们相等（`2.0 == 2`、`True == 1`、`False == 0`）。
    因此畸形浮点/布尔比分被严格有效的整数表示修复时（如 `[2.0, 1]` -> `[2, 1]`），
    `before != after` 会误判为「无变化」，导致 updated 少计。

    记录最终以 JSON 落盘，故以 JSON 规范化序列化结果作为判据。
    """
    if before is after:
        return False
    return json.dumps(before, sort_keys=True, default=str) != json.dumps(
        after, sort_keys=True, default=str
    )


def _merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """
    合并最新赛事。
    使用 same_event 做去重，严格保持旧 canonical id，通过 resolve_match_update 实施状态机。
    返回 (merged, added, updated)。

    入口先执行**摄取身份预检**（``utils.ingestion_identity.plan_ingestion_identity``）：
    批次内任何无法唯一确定 Canonical 归属的情况都整体拒绝，且该预检在**任何持久化写入之前**，
    因此直接调用本函数的调用方同样受到 fail-closed 保护。

    新事件的 Canonical ID 一律取自预检给出的**确定性选择结果**，而不是本批次的行序：
    同一组 unambiguous 的 incoming 记录，无论排列如何，都会得到同一个首个 Canonical ID。
    """
    plan = plan_ingestion_identity(existing, incoming)

    result: list[dict] = [copy.deepcopy(m) for m in existing]

    id_owner: dict[str, dict] = {}
    seen_ids = set()
    for m in result:
        mid = m.get("id")
        if not mid:
            raise CanonicalIdentityCollisionError("NONE", "AMBIGUOUS_SOURCE", "Existing record missing ID")
        if mid in seen_ids:
            raise CanonicalIdentityCollisionError(mid, "PREEXISTING_DUPLICATE", "Duplicate ID in existing")
        seen_ids.add(mid)
        id_owner[mid] = m

    added = updated = 0

    from utils.match_identity import competition_event_key, team_event_key

    position_by_id: dict[str, int] = {
        str(m.get("id")): i for i, m in enumerate(result)
    }

    # 预检单批次内部同一目标事件的权威开赛时间是否自相矛盾（顺序无关 fail-closed）
    known_times_by_target: dict[str, set[tuple[str, str]]] = {}
    for inc_idx, inc in enumerate(incoming):
        if not isinstance(inc, dict) or not is_kickoff_time_known(inc):
            continue
        owner_pos = plan.owner_index_by_index[inc_idx]
        target_key = (
            str(existing[owner_pos].get("id") or "")
            if owner_pos is not None
            else (plan.canonical_id_by_index[inc_idx] or "")
        )
        if not target_key:
            continue
        known_times_by_target.setdefault(target_key, set()).add(
            (str(inc.get("date") or "").strip(), str(inc.get("time") or "").strip())
        )
    conflicted_time_targets = {
        k for k, vals in known_times_by_target.items() if len(vals) > 1
    }

    for inc_idx, inc in enumerate(incoming):
        if not isinstance(inc, dict):
            raise CanonicalIdentityCollisionError(f"inc_index_{inc_idx}", "MALFORMED_INCOMING_RECORD", "Incoming record is not a dictionary")

        inc_mid = inc.get("id")
        if inc_mid is not None:
            if not isinstance(inc_mid, str):
                raise CanonicalIdentityCollisionError("NONE", "MALFORMED_INCOMING_ID", "Incoming ID has invalid type")
            inc_mid = inc_mid.strip()
            if not inc_mid:
                raise CanonicalIdentityCollisionError("NONE", "MALFORMED_INCOMING_ID", "Incoming ID is empty")
        else:
            if not competition_event_key(inc) and not team_event_key(inc):
                raise CanonicalIdentityCollisionError("NONE", "INADEQUATE_EVENT_IDENTITY", "Incoming match has missing ID and inadequate event identity fields")

        matching_indices = [i for i, old in enumerate(result) if same_event(old, inc)]
        match_idx = -1

        if len(matching_indices) > 1:
            raise CanonicalIdentityCollisionError(str(inc_mid), "AMBIGUOUS_CANONICAL_OWNERSHIP", "Multiple matching Canonical records found")

        # Canonical 归属以预检结果为准。预检基于**合并前**快照判定，而本循环会通过
        # resolve_match_update 改写排期元数据（队名、联赛、开赛时间）；队名同时是
        # same_event 的 fallback 身份键，若逐条重新计算，处理顺序就会影响判定：
        # 先以简称更新的记录，会让随后同事件的全名记录不再判等，从而被误当成
        # 「新事件又复用了既有 ID」而整体拒绝（真实批次曾因此必然 500）。
        owner_pos = plan.owner_index_by_index[inc_idx]
        anchored_id = (
            str(existing[owner_pos].get("id") or "")
            if owner_pos is not None
            else plan.canonical_id_by_index[inc_idx]
        )
        if anchored_id:
            match_idx = position_by_id.get(anchored_id, -1)

        if match_idx < 0 and inc_mid and inc_mid in id_owner:
            owner = id_owner[inc_mid]
            if not same_event(owner, inc):
                raise CanonicalIdentityCollisionError(inc_mid, "SAME_SOURCE_ALIAS_DIFFERENT_EVENT", "Source alias reused for different event")
            if matching_indices:
                match_idx = matching_indices[0]
        elif match_idx < 0 and len(matching_indices) == 1:
            match_idx = matching_indices[0]

        if match_idx >= 0 and inc_mid and inc_mid not in id_owner:
            id_owner[inc_mid] = result[match_idx]

        if match_idx >= 0:
            old = result[match_idx]
            before = copy.deepcopy(old)
            target_key = anchored_id or str(old.get("id") or "")
            inc_for_update = inc
            if target_key and target_key in conflicted_time_targets and is_kickoff_time_known(inc):
                inc_for_update = dict(inc)
                inc_for_update["time"] = "00:00"
                inc_for_update["kickoff_time_known"] = False
            try:
                resolve_match_update(old, inc_for_update)
            except MatchLifecycleConflict as exc:
                h_e, a_e = exc.existing_score[0], exc.existing_score[1]
                h_i, a_i = exc.incoming_score[0], exc.incoming_score[1]
                print(
                    f"[Auto-Sync] 完赛结果冲突 {exc.match_id}: "
                    f"existing={h_e}-{a_e} incoming={h_i}-{a_i}; "
                    f"canonical result preserved"
                )
                old.clear()
                old.update(before)
                continue

            if _record_changed(before, old):
                updated += 1
        else:
            m = dict(inc)
            # Canonical ID 由预检确定性给出（源命名空间优先级 + 字典序，
            # 或对无源 ID 记录的确定性合成序号），与本批次行序无关。
            mid = plan.canonical_id_by_index[inc_idx] or m.get("id")
            if not mid:
                mid = f"{m.get('sport','f')}-{m.get('date')}-{len(result) + added}"
            m["id"] = mid
            target_key = plan.canonical_id_by_index[inc_idx] or str(mid)
            if target_key in conflicted_time_targets:
                m["time"] = "00:00"
                m["kickoff_time_known"] = False

            if mid in seen_ids:
                raise CanonicalIdentityCollisionError(mid, "INCOMING_SHARED_ID", "New event shares existing ID")

            seen_ids.add(mid)
            id_owner[mid] = m
            position_by_id[mid] = len(result)

            m["market_tracked"] = True
            result.append(m)
            added += 1

    result.sort(key=lambda x: (x.get("date", ""), x.get("time", "00:00")))
    return result, added, updated


def _calibrate_from_finished(matches: list[dict]) -> int:
    """
    用新完赛的比赛校准 Elo（跳过已校准的）。

    最终比分有效性由 `utils.match_lifecycle.valid_full_time_score` 统一裁决，
    与该函数在 canonical 终态判定、结算、评估样本中使用的语义完全一致。

    - 只有 `status == "finished"` 且比分通过严格校验的比赛才可校准。
    - 畸形结果（字符串 / 浮点 / 布尔 / 负数）一律跳过：不写 calibrated.json、
      不改动任何球队状态。
    - 校验通过后**不**做数值强转，直接把已验证的整数传给 `update_from_result`。
    """
    from utils.daily_loader import load_json as _load

    calibrated_file = "calibrated.json"
    done = set((_load(calibrated_file) or {}).get("ids", []))

    count = 0
    for m in matches:
        if m.get("status") != "finished":
            continue
        if not valid_full_time_score(m.get("score")):
            continue
        mid = m.get("id")
        if not mid or mid in done:
            continue
        ft = m["score"]["ft"]
        try:
            update_from_result(
                m.get("home", ""),
                m.get("away", ""),
                ft[0],
                ft[1],
                m.get("league", ""),
                m.get("sport", "football"),
            )
            done.add(mid)
            count += 1
        except Exception:  # noqa: BLE001
            continue

    if count:
        save_json(calibrated_file, {"ids": sorted(done)})
    return count


def _capture_odds_history(
    matches: list[dict],
    now=None,
    refresh_started_at=None,
) -> int:
    """
    为仍可投注的比赛捕获赛前盘口历史，返回本次**实际新增**的历史观察条数。

    只有「未开赛 + 有盘口 + 与最新一条不同」的比赛才会产生新记录，
    因此盘口未变的一次刷新返回 0。

    失败策略：单场写入失败仅记录告警并继续，绝不阻断 daily_matches.json
    的正常落盘——赔率历史是附加层，其故障不应破坏主数据。
    """
    added = 0
    ref_start = refresh_started_at or now
    for m in matches:
        if not m.get("odds"):
            continue
        try:
            snapshot = record_odds_snapshot(
                m,
                now=None if refresh_started_at is not None else now,
                refresh_started_at=ref_start,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto-Sync] 赔率历史写入失败 {m.get('id')}: {exc}")
            continue
        if snapshot is not None:
            added += 1
    return added


def _capture_prediction_snapshots(
    matches: list[dict],
    now=None,
    refresh_started_at=None,
    blocked_match_ids=None,
) -> int:
    """
    为合并后仍「未开赛」的比赛自动固化 canonical 赛前预测快照，
    返回本次**实际新增**的快照条数。

    必须在 `_calibrate_from_finished(...)` **之后**调用：这样本轮已完赛的比赛
    结果会先写入 Elo，随后生成的预测快照使用的是「当前刷新时刻已知」的最新实力，
    而不是过期的旧 Elo。这不是未来泄漏——这些结果在目标比赛开赛前就已发生。

    只处理 upcoming：live / finished 一律跳过（复用既有时间状态判定，不自行实现）。

    失败策略：单场失败仅记录告警（比赛 id + 异常）并继续，
    绝不阻断 daily_matches.json 的落盘。
    """
    added = 0
    ref_start = refresh_started_at or now
    for m in matches:
        match_id = m.get("id")
        if not match_id:
            continue
        if match_id in (blocked_match_ids or set()):
            continue
        curr_time = get_beijing_now()
        if add_time_status(m, curr_time).get("status") != "upcoming":
            continue
        if ref_start and add_time_status(m, ref_start).get("status") != "upcoming":
            continue
        try:
            enriched = enrich_match(m, curr_time)
            _snapshot, created = capture_snapshot(
                enriched,
                now=None if refresh_started_at is not None else now,
                refresh_started_at=ref_start,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto-Sync] 预测快照写入失败 {match_id}: {exc}")
            continue
        if created:
            added += 1
    return added


def _settle_finished_matches(matches: list[dict], now=None) -> int:
    """
    为已完赛比赛关联既有赛前预测快照，返回本次**实际新增**的结算条数。

    规则
    ----
    - 只处理 `finished` 且有有效最终比分的比赛（复用既有时间状态判定）。
    - 只结算**已存在**的预测快照；完结比赛若没有预测快照则跳过，
      **绝不**回溯生成预测或伪造历史快照。
    - 一场比赛的全部快照各自独立结算（兼容未来多模型版本）。
    - 上游结果与既有结算冲突时，记录告警并保持既有记录不变，继续处理其他比赛。
    - 单场失败仅告警并继续，绝不阻断 daily_matches.json 的落盘。

    本函数不更新 Elo、不跑预测模型、不计算任何模型指标。
    """
    added = 0
    for m in matches:
        match_id = m.get("id")
        if not match_id:
            continue
        if not is_finalized_match(m):
            continue

        snapshots = get_snapshots_for_match(match_id)
        if not snapshots:
            continue

        for snapshot in snapshots:
            try:
                _settlement, created = settle_snapshot(snapshot, m, now=now)
            except SettlementConflictError as exc:
                print(f"[Auto-Sync] 结算冲突，既有记录保持不变: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"[Auto-Sync] 结算写入失败 {match_id}/{snapshot.get('snapshot_id')}: {exc}")
                continue
            if created:
                added += 1
    return added


def _materialize_evaluation_rows(matches: list[dict], now=None) -> int:
    """
    把「既有预测快照 × 既有结算」物化为评估样本，返回本次**实际新增**的样本条数。

    规则
    ----
    - 以 canonical 比赛 id 为线索，取该场全部结算，再取其精确引用的预测快照。
    - 只有快照与结算**同时存在**才物化；缺任一方一律跳过，绝不重建历史数据。
    - 不只是处理本轮新建的结算：既有结算若缺少评估样本，同样会被补齐（回填）。
    - 拼接/溯源不一致或源内容冲突 -> 记录告警并跳过，既有样本不变，继续处理其他样本。
    - 单条失败仅告警并继续，绝不阻断 daily_matches.json 的落盘。

    本函数不跑预测模型、不读当前球队实力、不读当前盘口、不重新结算、不计算任何指标。
    """
    from utils.settlements import extract_final_score, outcome_from_score, result_fingerprint

    added = 0
    visited: set[str] = set()
    for m in matches:
        match_id = m.get("id")
        if not match_id or match_id in visited:
            continue
        if not is_finalized_match(m):
            continue
        visited.add(match_id)

        canonical_score = extract_final_score(m)
        if not canonical_score:
            continue

        canonical_outcome = outcome_from_score(canonical_score)
        canonical_fingerprint = result_fingerprint(canonical_score)

        for settlement in get_settlements_for_match(match_id):
            snapshot_id = settlement.get("snapshot_id")
            snapshot = get_snapshot(snapshot_id) if snapshot_id else None
            if not snapshot:
                continue

            if snapshot.get("match_id") != match_id or settlement.get("match_id") != match_id:
                print(f"[Auto-Sync] 匹配身份冲突 {match_id}/{snapshot_id}，拒绝物化")
                continue

            if settlement.get("final_score") != canonical_score:
                print(f"[Auto-Sync] 遗留结算比分与 canonical 不一致 {match_id}/{snapshot_id}，拒绝物化")
                continue

            if settlement.get("actual_outcome") != canonical_outcome:
                print(f"[Auto-Sync] 遗留结算胜负与 canonical 不一致 {match_id}/{snapshot_id}，拒绝物化")
                continue

            if settlement.get("result_fingerprint") != canonical_fingerprint:
                print(f"[Auto-Sync] 遗留结算指纹与 canonical 不一致 {match_id}/{snapshot_id}，拒绝物化")
                continue

            try:
                _row, created = capture_evaluation_row(snapshot, settlement, now=now)
            except EvaluationIntegrityError as exc:
                print(f"[Auto-Sync] 评估样本完整性错误，已跳过: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"[Auto-Sync] 评估样本写入失败 {match_id}/{snapshot_id}: {exc}")
                continue
            if created:
                added += 1
    return added


def _refresh_impl(verbose: bool = True) -> dict:
    """
    执行一次完整抓取刷新。
    返回统计信息。
    """
    now = get_beijing_now()

    # 0. Validate existing canonical identities BEFORE fetching
    data = load_json(DAILY_FILE)
    if data is None:
        matches_list = []
    else:
        if not isinstance(data, dict):
            raise CanonicalIdentityCollisionError("NONE", "MALFORMED_CANONICAL_ROOT", "Canonical root is not an object")
        if "matches" in data and not isinstance(data["matches"], list):
            raise CanonicalIdentityCollisionError("NONE", "MALFORMED_MATCHES_ARRAY", "matches is not an array")
        matches_list = data.get("matches", [])

    seen_ids = set()
    for idx, m in enumerate(matches_list):
        if not isinstance(m, dict):
            raise CanonicalIdentityCollisionError(f"index_{idx}", "MALFORMED_MATCH_ENTRY", "A match entry is not an object")

        mid = m.get("id")
        if mid is None:
            raise CanonicalIdentityCollisionError(f"index_{idx}", "AMBIGUOUS_SOURCE", "A match ID is missing")
        if not isinstance(mid, str):
            raise CanonicalIdentityCollisionError(f"index_{idx}", "MALFORMED_ID_TYPE", "A match ID has an invalid type")
        mid = mid.strip()
        if not mid:
            raise CanonicalIdentityCollisionError(f"index_{idx}", "AMBIGUOUS_SOURCE", "A match ID is empty or whitespace-only")

        if mid in seen_ids:
            raise CanonicalIdentityCollisionError(mid, "PREEXISTING_DUPLICATE", "Two matches share the same ID")
        seen_ids.add(mid)

    result = {
        "started_at": now.isoformat(),
        "sources": {},
        "added": 0,
        "updated": 0,
        "calibrated": 0,
        "odds_snapshots_added": 0,
        "prediction_snapshots_added": 0,
        "settlements_added": 0,
        "evaluation_rows_added": 0,
        "total": 0,
    }

    incoming: list[dict] = []

    # 1. 即时比分（含开赛时间 + 竞彩编号）★权威赛程
    for sport in ("football", "basketball"):
        try:
            if sport == "basketball":
                rows = fetcher_500.fetch_live_basketball()
            else:
                rows = fetcher_500.fetch_live_matches(sport)
            incoming.extend(rows)
            result["sources"][f"live_{sport}"] = len(rows)
            if verbose:
                print(f"  [即时比分{sport}] {len(rows)} 场")
        except Exception as exc:  # noqa: BLE001
            result["sources"][f"live_{sport}"] = f"error: {exc}"

    # 2. 竞彩赔率（XML）—— 按「竞彩编号」挂载到赛程上；未挂载的留作兜底候选
    market_rows: list[dict] = []
    used_market_rows: list[dict] = []
    attach_diagnostics: dict = {}
    for sport in ("football", "basketball"):
        try:
            rows = fetcher_500.fetch_jczq_xml(sport)
            sport_diag: dict = {}
            attached, used = _attach_odds(incoming, rows, diagnostics=sport_diag)
            for key in ("reversed_orientation_rows_skipped", "variant_orientation_rows_attached"):
                attach_diagnostics[key] = attach_diagnostics.get(key, 0) + sport_diag.get(key, 0)
            attach_diagnostics.setdefault("reversed_rows", []).extend(sport_diag.get("reversed_rows", []))
            attach_diagnostics.setdefault("variant_rows", []).extend(sport_diag.get("variant_rows", []))
            market_rows.extend(rows)
            used_market_rows.extend(used)
            result["sources"][f"jczq_odds_{sport}"] = len(rows)
            if verbose:
                print(f"  [竞彩赔率{sport}] {attached}/{len(rows)} 场已挂载盘口")
        except Exception as exc:  # noqa: BLE001
            result["sources"][f"jczq_odds_{sport}"] = f"error: {exc}"
    if attach_diagnostics.get("reversed_orientation_rows_skipped"):
        result["sources"]["odds_rows_skipped_reversed_orientation"] = attach_diagnostics["reversed_orientation_rows_skipped"]
        result["reversed_orientation_odds_rows"] = attach_diagnostics["reversed_rows"]
    if attach_diagnostics.get("variant_orientation_rows_attached"):
        result["sources"]["odds_rows_attached_variant_orientation"] = attach_diagnostics["variant_orientation_rows_attached"]
        result["variant_orientation_odds_rows"] = attach_diagnostics["variant_rows"]

    # 3. 未被挂载但自带可用盘口的赔率行 -> 兜底候选（同一市场事件不重复入册）
    result["sources"]["market_candidates"] = _append_unmatched_market_candidates(
        incoming, market_rows, used_market_rows
    )

    # 4. 500 完场（SSR，含赛果与历史）
    try:
        finished = fetcher_500.fetch_finished_matches()
        incoming.extend(finished)
        result["sources"]["wanchang"] = len(finished)
        if verbose:
            print(f"  [完场] {len(finished)} 场")
    except Exception as exc:  # noqa: BLE001
        result["sources"]["wanchang"] = f"error: {exc}"

    if not incoming:
        if verbose:
            print("  [警告] 未获取到任何数据")
        result["total"] = len(load_json(DAILY_FILE).get("matches", []))
        return result

    # 5. 迁移既有 canonical 集合：剔除历史上被广泛抓取、且无盘口覆盖的赛事
    existing, legacy_removed = _prepare_existing_market_universe(matches_list)

    # 5b. ★ Canonical 身份摄取预检（fail closed）
    #     执行位置：候选构造完成之后，市场准入与**所有**下游持久化写入之前。
    #     该预检顺序无关，且不修改任何输入；一旦发现无法唯一确定归属的批次，
    #     直接抛出 CanonicalIdentityCollisionError -> 整批拒绝、零持久化写入。
    validate_ingestion_identity(existing, incoming)

    # 6. 市场准入：已跟踪比赛接受更新；新候选必须具备可用盘口覆盖，
    #    且不得是既有跟踪事件的跨源重复候选（Phase 6-B 守卫，准入级 fail-closed）
    admit_diagnostics: dict = {}
    admitted, rejected = _admit_market_candidates(incoming, existing, diagnostics=admit_diagnostics)
    result["sources"]["market_tracked_removed"] = legacy_removed
    result["sources"]["market_rejected"] = rejected
    if admit_diagnostics.get("cross_source_duplicate_candidates_rejected"):
        result["sources"]["cross_source_duplicate_candidates_rejected"] = admit_diagnostics[
            "cross_source_duplicate_candidates_rejected"
        ]
        result["cross_source_duplicate_evidence"] = admit_diagnostics["cross_source_duplicate_evidence"]
        if verbose:
            print(
                f"  [跨源重复守卫] 拒绝 {admit_diagnostics['cross_source_duplicate_candidates_rejected']}"
                " 条重复候选（候选级证据，不归并）"
            )

    # 7. 合并为 canonical 跟踪集合（_merge 内部再次执行同一预检，属幂等附加防线）
    merged, added, updated = _merge(existing, admitted)

    # 7b. Canonical ID 唯一性证明（附加防线）
    #     在第一个下游持久化写入之前执行。不替代既有启动守卫（既有重复 ID 继续
    #     fail closed），也不自动修复任何历史重复。
    validate_canonical_uniqueness(
        merged,
        previous_ids=[m.get("id") for m in existing],
        expected_total=len(existing) + added,
    )

    # 7c. NBA 外部权威赛程时间补全（严格 time-only：不新增赛事、不改身份与赔率）
    from utils.fetcher_500 import is_nba_league
    from utils.nba_schedule import (
        ENABLE_NBA_SCHEDULE_NETWORK,
        PROVIDER_BALLDONTLIE_NBA_GAMES,
        apply_nba_schedule_time_enrichment,
        fetch_nba_schedule_observations,
    )

    upcoming_nba_dates = sorted({
        str(m.get("date") or "").strip()
        for m in merged
        if isinstance(m, dict)
        and m.get("sport") == "basketball"
        and is_nba_league(m.get("league"))
        and (m.get("status") or "upcoming") == "upcoming"
        and m.get("date")
    })
    schedule_capture_blocked = set()
    if upcoming_nba_dates:
        # A BDL-derived time alone cannot establish the provider's current scheduled status.
        schedule_capture_blocked = {
            m.get("id") for m in merged
            if (m.get("kickoff_provenance") or {}).get("actual_kickoff_time_source") == PROVIDER_BALLDONTLIE_NBA_GAMES
        }
        try:
            sched_obs, sched_diag = fetch_nba_schedule_observations(
                upcoming_nba_dates,
                now=now,
                allow_network=getattr(config, "ENABLE_NBA_SCHEDULE_NETWORK", ENABLE_NBA_SCHEDULE_NETWORK),
                provider=PROVIDER_BALLDONTLIE_NBA_GAMES,
            )
            sched_available = (
                sched_diag.get("status") in ("ok", "empty")
                and sched_diag.get("pagination_complete") is True
            )
            result["nba_schedule_diagnostics"] = sched_diag
            sched_res = apply_nba_schedule_time_enrichment(
                merged,
                sched_obs,
                now=get_beijing_now(),
                schedule_available=sched_available,
            )
            result["sources"]["nba_schedule"] = sched_diag.get("status") if str(sched_diag.get("status", "")).startswith("disabled") else sched_diag.get("rows", 0)
            result["nba_schedule_enriched"] = sched_res.get("enriched", 0)
            result["nba_schedule_rescheduled"] = sched_res.get("rescheduled", 0)
            schedule_capture_blocked.difference_update(
                d["match_id"] for d in sched_res.get("decisions", []) if d.get("bound")
            )
            if sched_res.get("enriched", 0) or sched_res.get("rescheduled", 0):
                updated += sched_res.get("enriched", 0) + sched_res.get("rescheduled", 0)
        except Exception as exc:  # noqa: BLE001
            result["sources"]["nba_schedule"] = f"error: {exc}"

    # 8. 捕获赛前赔率历史（以合并后的 canonical 比赛 id 为准；传入刷新启动时间防时间回溯）
    odds_snapshots_added = _capture_odds_history(merged, refresh_started_at=now)

    # 9. 校准 Elo（用本轮新完赛结果更新实力）
    calibrated = _calibrate_from_finished(merged)

    # 10. 自动固化赛前预测快照（传入刷新启动时间，以当前物理时钟捕获，跨越开赛自动拒绝）
    #     必须在 Elo 校准之后：快照应使用「当前刷新时刻已知」的最新实力。
    prediction_snapshots_added = _capture_prediction_snapshots(
        merged, refresh_started_at=now, blocked_match_ids=schedule_capture_blocked,
    )

    # 11. 结算已完赛比赛（只关联既有预测快照，不回溯生成预测）
    settlements_added = _settle_finished_matches(merged, now=now)

    # 12. 物化评估样本（消费权威历史记录：预测快照 × 结算）
    evaluation_rows_added = _materialize_evaluation_rows(merged, now=now)

    # 13. 落盘
    save_json(
        DAILY_FILE,
        {
            "meta": {
                "last_refresh": now.isoformat(),
                "sources": result["sources"],
                "total": len(merged),
            },
            "matches": merged,
        },
    )

    result.update(
        {
            "added": added,
            "updated": updated,
            "calibrated": calibrated,
            "odds_snapshots_added": odds_snapshots_added,
            "prediction_snapshots_added": prediction_snapshots_added,
            "settlements_added": settlements_added,
            "evaluation_rows_added": evaluation_rows_added,
            "total": len(merged),
            "finished_at": get_beijing_now().isoformat(),
        }
    )

    if verbose:
        print(
            f"  ✅ 新增 {added} | 更新 {updated} | Elo校准 {calibrated} "
            f"| 赔率历史 +{odds_snapshots_added} | 预测快照 +{prediction_snapshots_added} "
            f"| 结算 +{settlements_added} | 评估样本 +{evaluation_rows_added} "
            f"| 无盘口剔除 {legacy_removed + rejected} | 库内共 {len(merged)} 场"
        )
    return result


def refresh(verbose: bool = True) -> dict:
    """
    执行一次完整刷新。

    全程持有跨进程写入者锁；并在持锁期间发布「代」标记，使只读方能够判断
    盘上七个库是否来自同一次成功刷新（见 ``utils.refresh_state``）。
    标记失败不影响刷新本身，也绝不回滚已经落盘的单个文件。
    """
    with acquire_refresh_lock() as lease:
        try:
            lease.begin()
        except OSError:
            # 标记只是可观测性产物，写不出来不应阻断刷新。
            pass
        try:
            result = _refresh_impl(verbose=verbose)
        except BaseException as exc:
            try:
                lease.complete(
                    refresh_state.STATUS_FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except OSError:
                pass
            raise
        try:
            lease.complete(refresh_state.STATUS_COMPLETED)
        except OSError:
            pass
        return result


if __name__ == "__main__":
    print("=" * 60)
    print("  每日体彩数据抓取")
    print("=" * 60)
    import sys
    try:
        stats = refresh()
        print("\n结果:", stats)
    except RefreshBusyError as e:
        print(f"\n[Busy] {e}")
        sys.exit(1)
