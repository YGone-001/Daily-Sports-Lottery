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

from utils.concurrency import acquire_refresh_lock, RefreshBusyError
from utils import fetcher_500
from utils.daily_loader import (
    add_time_status,
    enrich_match,
    get_beijing_now,
    load_json,
    save_json,
)
from utils.evaluation_rows import EvaluationIntegrityError, capture_evaluation_row
from utils.market_coverage import has_usable_market_odds
from utils.match_identity import is_kickoff_time_known, same_event
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


def _attach_odds(matches: list[dict], odds_rows: list[dict]) -> tuple[int, list[dict]]:
    """
    把竞彩赔率挂到已有赛程上。
    优先级：
    1. 强赛事编号 (sport + date + 归一化编号)
    2. 主客队对 fallback (sport + date + home + away)
    不使用开赛时间，不单独使用 (date, home)。

    返回 (成功挂载的场次数, 被消费的赔率源行列表)。
    被消费的行不再作为兜底候选，避免同一市场事件重复入册。
    """
    count = 0
    used: list[dict] = []
    used_ids: set[int] = set()

    for m in matches:
        if m.get("odds"):
            continue
        matched_row = None
        for r in odds_rows:
            if id(r) in used_ids:
                continue
            if same_event(m, r):
                matched_row = r
                break

        if matched_row and matched_row.get("odds"):
            m["odds"] = dict(matched_row["odds"])
            count += 1
            used.append(matched_row)
            used_ids.add(id(matched_row))
            # 用赔率源的联赛名补全（更规范）
            if matched_row.get("league") and not m.get("league"):
                m["league"] = matched_row["league"]
            if matched_row.get("jczq_no") and not m.get("jczq_no"):
                m["jczq_no"] = matched_row["jczq_no"]
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


def _admit_market_candidates(
    candidates: list[dict],
    existing: list[dict],
) -> tuple[list[dict], int]:
    """
    市场准入：决定哪些候选可以进入 canonical 跟踪集合。

    - 已在跟踪集合中（same_event 命中已跟踪事件）-> 准入：接受状态 / 比分 / 开赛时间更新，
      即使本轮没有盘口（盘口源临时不可用时也必须保持生命周期连续性）。
    - 首次出现的全新候选 -> 只有具备可用盘口覆盖才准入，并打上 market_tracked = True。
    - 其余 -> 拒绝（不进入 canonical 集合，也不写入任何下游历史）。

    返回 (准入列表, 拒绝条数)。
    """
    admitted: list[dict] = []
    rejected = 0
    for m in candidates:
        if any(same_event(m, ex) for ex in existing):
            admitted.append(m)
            continue
        if has_usable_market_odds(m):
            m["market_tracked"] = True
            admitted.append(m)
            continue
        rejected += 1
    return admitted, rejected



class CanonicalIdentityCollisionError(Exception):
    """Raised when a canonical identity collision is detected before downstream writes."""
    def __init__(self, match_id: str, category: str, message: str):
        super().__init__(f"Collision [{category}] for ID {match_id}: {message}")
        self.match_id = match_id
        self.category = category

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
    """
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

        if inc_mid and inc_mid in id_owner:
            owner = id_owner[inc_mid]
            if not same_event(owner, inc):
                raise CanonicalIdentityCollisionError(inc_mid, "SAME_SOURCE_ALIAS_DIFFERENT_EVENT", "Source alias reused for different event")
            if matching_indices:
                match_idx = matching_indices[0]
        else:
            if len(matching_indices) == 1:
                match_idx = matching_indices[0]
                if inc_mid:
                    id_owner[inc_mid] = result[match_idx]

        if match_idx >= 0:
            old = result[match_idx]
            before = copy.deepcopy(old)
            try:
                resolve_match_update(old, inc)
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
            mid = m.get("id")
            if not mid:
                mid = f"{m.get('sport','f')}-{m.get('date')}-{len(result) + added}"
                m["id"] = mid

            if mid in seen_ids:
                raise CanonicalIdentityCollisionError(mid, "INCOMING_SHARED_ID", "New event shares existing ID")

            seen_ids.add(mid)
            id_owner[mid] = m

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


def _capture_odds_history(matches: list[dict], now=None) -> int:
    """
    为仍可投注的比赛捕获赛前盘口历史，返回本次**实际新增**的历史观察条数。

    只有「未开赛 + 有盘口 + 与最新一条不同」的比赛才会产生新记录，
    因此盘口未变的一次刷新返回 0。

    失败策略：单场写入失败仅记录告警并继续，绝不阻断 daily_matches.json
    的正常落盘——赔率历史是附加层，其故障不应破坏主数据。
    """
    added = 0
    for m in matches:
        if not m.get("odds"):
            continue
        try:
            snapshot = record_odds_snapshot(m, now=now)
        except Exception as exc:  # noqa: BLE001
            print(f"[Auto-Sync] 赔率历史写入失败 {m.get('id')}: {exc}")
            continue
        if snapshot is not None:
            added += 1
    return added


def _capture_prediction_snapshots(matches: list[dict], now=None) -> int:
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
    for m in matches:
        match_id = m.get("id")
        if not match_id:
            continue
        if add_time_status(m, now).get("status") != "upcoming":
            continue
        try:
            enriched = enrich_match(m, now)
            _snapshot, created = capture_snapshot(enriched, now=now)
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
    for sport in ("football", "basketball"):
        try:
            rows = fetcher_500.fetch_jczq_xml(sport)
            attached, used = _attach_odds(incoming, rows)
            market_rows.extend(rows)
            used_market_rows.extend(used)
            result["sources"][f"jczq_odds_{sport}"] = len(rows)
            if verbose:
                print(f"  [竞彩赔率{sport}] {attached}/{len(rows)} 场已挂载盘口")
        except Exception as exc:  # noqa: BLE001
            result["sources"][f"jczq_odds_{sport}"] = f"error: {exc}"

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

    # 6. 市场准入：已跟踪比赛接受更新；新候选必须具备可用盘口覆盖
    admitted, rejected = _admit_market_candidates(incoming, existing)
    result["sources"]["market_tracked_removed"] = legacy_removed
    result["sources"]["market_rejected"] = rejected

    # 7. 合并为 canonical 跟踪集合
    merged, added, updated = _merge(existing, admitted)

    # 8. 捕获赛前赔率历史（以合并后的 canonical 比赛 id 为准）
    odds_snapshots_added = _capture_odds_history(merged, now=now)

    # 9. 校准 Elo（用本轮新完赛结果更新实力）
    calibrated = _calibrate_from_finished(merged)

    # 10. 自动固化赛前预测快照
    #     必须在 Elo 校准之后：快照应使用「当前刷新时刻已知」的最新实力。
    prediction_snapshots_added = _capture_prediction_snapshots(merged, now=now)

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
    with acquire_refresh_lock():
        return _refresh_impl(verbose=verbose)


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
