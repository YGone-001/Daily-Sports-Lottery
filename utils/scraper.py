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

from datetime import datetime

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
    把竞彩赔率按「竞彩编号」挂到已有赛程上。
    编号缺失时退化为按 (日期, 主队) 匹配。

    返回 (成功挂载的场次数, 被消费的赔率源行列表)。
    被消费的行不再作为兜底候选，避免同一市场事件重复入册。
    """
    by_no: dict[str, dict] = {}
    by_home: dict[tuple, dict] = {}
    for r in odds_rows:
        no = _norm_no(r.get("jczq_no") or r.get("round") or "")
        if no:
            by_no.setdefault(no, r)
        by_home.setdefault((r.get("date", ""), r.get("home", "")), r)

    count = 0
    used: list[dict] = []
    for m in matches:
        if m.get("odds"):
            continue
        no = _norm_no(m.get("jczq_no") or "")
        src = by_no.get(no) if no else None
        if src is None:
            src = by_home.get((m.get("date", ""), m.get("home", "")))
        if src and src.get("odds"):
            m["odds"] = dict(src["odds"])
            count += 1
            used.append(src)
            # 用赔率源的联赛名补全（更规范）
            if src.get("league") and not m.get("league"):
                m["league"] = src["league"]
    return count, used


def _append_unmatched_market_candidates(
    incoming: list[dict],
    market_rows: list[dict],
    used_market_rows: list[dict],
) -> int:
    """
    把「未被挂载」但自带**可用盘口**的赔率行作为兜底候选追加进 `incoming`，返回追加条数。

    赔率源本身是独立于即时比分源的市场事件流：某行只是没挂上 live 行
    （编号不一致 / live 源暂时缺漏 / 队名写法不同）时，不应直接丢弃这个有效市场事件。

    同时必须防止重复：与既有 live 行同竞彩编号、或同 (日期, 主队) 的一律跳过，
    避免同一市场事件同时产生「live 行 + 00:00 兜底行」两条 canonical 记录。
    """
    used_ids = {id(row) for row in used_market_rows}
    live_nos = {_norm_no(m.get("jczq_no") or "") for m in incoming}
    live_nos.discard("")
    live_home = {(m.get("date", ""), m.get("home", "")) for m in incoming}

    appended_keys: set[str] = set()
    added = 0
    for row in market_rows:
        if id(row) in used_ids:
            continue
        if not has_usable_market_odds(row):
            continue
        no = _norm_no(row.get("jczq_no") or row.get("round") or "")
        if no and no in live_nos:
            continue
        if (row.get("date", ""), row.get("home", "")) in live_home:
            continue
        key = _match_key(row)
        if key in appended_keys:
            continue
        appended_keys.add(key)
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
    tracked_keys: set[str],
) -> tuple[list[dict], int]:
    """
    市场准入：决定哪些候选可以进入 canonical 跟踪集合。

    - 已在跟踪集合中（键命中）-> 准入：接受状态 / 比分更新，
      即使本轮没有盘口（盘口源临时不可用时也必须保持连续性）。
    - 首次出现的候选 -> 只有具备可用盘口覆盖才准入，并打上 `market_tracked = true`。
    - 其余 -> 拒绝（不进入 canonical 集合，也不写入任何下游历史）。

    返回 (准入列表, 拒绝条数)。不会写入 `market_tracked = false`。
    """
    admitted: list[dict] = []
    rejected = 0
    for m in candidates:
        if _match_key(m) in tracked_keys:
            admitted.append(m)
            continue
        if has_usable_market_odds(m):
            m["market_tracked"] = True
            admitted.append(m)
            continue
        rejected += 1
    return admitted, rejected


def _merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """
    合并新旧赛事。
    匹配键: sport|date|time|home|away
    返回 (merged, added, updated)
    """
    index = {}
    for m in existing:
        index[_match_key(m)] = m

    added = updated = 0
    for m in incoming:
        key = _match_key(m)
        if key in index:
            old = index[key]
            # 更新比分/赔率/状态
            changed = False
            if m.get("score") and m["score"] != old.get("score"):
                old["score"] = m["score"]
                changed = True
            if m.get("odds") and m["odds"] != old.get("odds"):
                old["odds"] = m["odds"]
                old["odds_updated_at"] = get_beijing_now().isoformat()
                changed = True
            if m.get("status") != old.get("status"):
                old["status"] = m["status"]
                changed = True
            if m.get("home_rank") is not None:
                old["home_rank"] = m["home_rank"]
            if m.get("away_rank") is not None:
                old["away_rank"] = m["away_rank"]
            if changed:
                updated += 1
        else:
            # 保留原 id
            if not m.get("id"):
                m["id"] = f"{m.get('sport','f')}-{m.get('date')}-{added}"
            index[key] = m
            added += 1

    merged = list(index.values())
    merged.sort(key=lambda x: (x.get("date", ""), x.get("time", "00:00")))
    return merged, added, updated


def _calibrate_from_finished(matches: list[dict]) -> int:
    """用新完赛的比赛校准 Elo（跳过已校准的）"""
    from utils.daily_loader import load_json as _load

    calibrated_file = "calibrated.json"
    done = set((_load(calibrated_file) or {}).get("ids", []))

    count = 0
    for m in matches:
        if m.get("status") != "finished" or not m.get("score"):
            continue
        mid = m.get("id")
        if not mid or mid in done:
            continue
        ft = m["score"].get("ft")
        if not (isinstance(ft, list) and len(ft) >= 2):
            continue
        try:
            update_from_result(
                m.get("home", ""),
                m.get("away", ""),
                int(ft[0]),
                int(ft[1]),
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
        if add_time_status(m, now).get("status") != "finished":
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
    added = 0
    visited: set[str] = set()
    for m in matches:
        match_id = m.get("id")
        if not match_id or match_id in visited:
            continue
        visited.add(match_id)

        for settlement in get_settlements_for_match(match_id):
            snapshot_id = settlement.get("snapshot_id")
            snapshot = get_snapshot(snapshot_id) if snapshot_id else None
            if not snapshot:
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


def refresh(verbose: bool = True) -> dict:
    """
    执行一次完整抓取刷新。
    返回统计信息。
    """
    now = get_beijing_now()
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
    data = load_json(DAILY_FILE) or {}
    existing, legacy_removed = _prepare_existing_market_universe(data.get("matches", []))

    # 6. 市场准入：已跟踪比赛接受更新；新候选必须具备可用盘口覆盖
    tracked_keys = {_match_key(m) for m in existing}
    admitted, rejected = _admit_market_candidates(incoming, tracked_keys)
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


if __name__ == "__main__":
    print("=" * 60)
    print("  每日体彩数据抓取")
    print("=" * 60)
    stats = refresh()
    print()
    print("结果:", stats)
