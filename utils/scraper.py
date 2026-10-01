"""
抓取调度器
==========
负责：
1. 调用 fetcher_500 抓取赛事
2. 合并进 daily_matches.json（去重 + 增量更新）
3. 用完赛结果滚动校准球队 Elo
4. 定时任务入口
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
from utils.odds_snapshots import record_odds_snapshot
from utils.prediction_snapshots import capture_snapshot
from utils.team_strength import update_from_result

DAILY_FILE = "daily_matches.json"


def _norm_no(value: str) -> str:
    """归一化竞彩编号：'周三302' / '302' / 302 -> '302'"""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or digits


def _attach_odds(matches: list[dict], odds_rows: list[dict]) -> int:
    """
    把竞彩赔率按「竞彩编号」挂到已有赛程上。
    编号缺失时退化为按 (日期, 主队) 匹配。
    返回成功挂载的场次数。
    """
    by_no: dict[str, dict] = {}
    by_home: dict[tuple, dict] = {}
    for r in odds_rows:
        no = _norm_no(r.get("jczq_no") or r.get("round") or "")
        if no:
            by_no.setdefault(no, r)
        by_home.setdefault((r.get("date", ""), r.get("home", "")), r)

    count = 0
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
            # 用赔率源的联赛名补全（更规范）
            if src.get("league") and not m.get("league"):
                m["league"] = src["league"]
    return count


def _merge(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    """
    合并新旧赛事。
    匹配键: sport|date|time|home|away
    返回 (merged, added, updated)
    """
    index = {}
    for m in existing:
        key = f"{m.get('sport')}|{m.get('date')}|{m.get('time')}|{m.get('home')}|{m.get('away')}"
        index[key] = m

    added = updated = 0
    for m in incoming:
        key = f"{m.get('sport')}|{m.get('date')}|{m.get('time')}|{m.get('home')}|{m.get('away')}"
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

    # 2. 竞彩赔率（XML）—— 按「竞彩编号」合并到赛程上
    for sport in ("football", "basketball"):
        try:
            rows = fetcher_500.fetch_jczq_xml(sport)
            merged = _attach_odds(incoming, rows)
            result["sources"][f"jczq_odds_{sport}"] = len(rows)
            if verbose:
                print(f"  [竞彩赔率{sport}] {merged}/{len(rows)} 场已挂载盘口")
        except Exception as exc:  # noqa: BLE001
            result["sources"][f"jczq_odds_{sport}"] = f"error: {exc}"

    # 2b. 兜底：若赛程源无数据，直接用赔率源自成赛程
    if not any(k.startswith("live_") and isinstance(v, int) and v for k, v in result["sources"].items()):
        for sport in ("football", "basketball"):
            try:
                rows = fetcher_500.fetch_jczq_xml(sport)
                incoming.extend(rows)
                result["sources"][f"jczq_only_{sport}"] = len(rows)
                if verbose and rows:
                    print(f"  [竞彩独立{sport}] {len(rows)} 场")
            except Exception as exc:  # noqa: BLE001
                result["sources"][f"jczq_only_{sport}"] = f"error: {exc}"

    # 2. 500 完场（SSR，含赛果与历史）
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

    # 3. 合并
    data = load_json(DAILY_FILE) or {}
    existing = data.get("matches", [])
    merged, added, updated = _merge(existing, incoming)

    # 3b. 捕获赛前赔率历史（以合并后的 canonical 比赛 id 为准）
    odds_snapshots_added = _capture_odds_history(merged, now=now)

    # 4. 校准 Elo（用本轮新完赛结果更新实力）
    calibrated = _calibrate_from_finished(merged)

    # 5. 自动固化赛前预测快照
    #    必须在 Elo 校准之后：快照应使用「当前刷新时刻已知」的最新实力。
    prediction_snapshots_added = _capture_prediction_snapshots(merged, now=now)

    # 6. 落盘
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
            "total": len(merged),
            "finished_at": get_beijing_now().isoformat(),
        }
    )

    if verbose:
        print(
            f"  ✅ 新增 {added} | 更新 {updated} | Elo校准 {calibrated} "
            f"| 赔率历史 +{odds_snapshots_added} | 预测快照 +{prediction_snapshots_added} "
            f"| 库内共 {len(merged)} 场"
        )
    return result


if __name__ == "__main__":
    print("=" * 60)
    print("  每日体彩数据抓取")
    print("=" * 60)
    stats = refresh()
    print()
    print("结果:", stats)
