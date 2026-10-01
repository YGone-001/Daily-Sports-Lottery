"""
赛前赔率历史
============
把「抓取器每次观察到的赛前盘口」固化为一条不可变的时序记录。

现状问题
--------
`daily_matches.json` 只保留每场比赛**最新**的赔率：抓取合并时直接
`old["odds"] = new_odds`，历史盘口变动信息被覆盖丢失。

本模块提供一份**附加的**历史层：

    daily_matches.json      -> 最新状态（保持不变）
    odds_snapshots.json     -> 历史状态（本模块维护）

职责边界
--------
- 只负责「规范化 + 历史比对 + 持久化」，不做任何网络抓取。
- 抓取由 `utils.fetcher_500` 负责；编排由 `utils.scraper` 负责。

关键设计
--------
- 规范化：剔除空值（None / 空字符串），键稳定排序。
- 指纹：`sha256(json.dumps(normalized, sort_keys=True))`，与字典顺序无关。
- 身份：`snapshot_id = sha256(match_id | captured_at | odds_fingerprint)`。
- 去重：**仅抑制连续重复**——只有与「该场最新一条」规范化赔率相同才跳过；
  因此 A -> B -> A 会保留 3 条，市场来回波动可被完整重建。
- 时序门禁：仅 `upcoming` 允许写入；`live` / `finished` 拒绝新写入，
  但既有历史始终可读。
- 并发：进程内 `threading.RLock` 保护读-改-写，配合原子落盘。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime

import config
from utils.atomic_json import atomic_write_json, load_json_file
from utils.daily_loader import add_time_status, get_beijing_now, get_match_datetime

# 数据来源标识。当前系统全部盘口来自 500.com 派生数据；
# 不声称更具体的博彩公司来源（解析器并不知情）。
ODDS_SOURCE = "500.com"

# 进程内锁：保护赔率历史文件的读-改-写。
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 存储位置与读写
# ---------------------------------------------------------------------------

def store_path() -> str:
    """赔率历史数据文件路径（运行时生成，不纳入版本控制）。"""
    return os.path.join(config.DATA_DIR, config.ODDS_SNAPSHOT_FILE)


def _empty_store() -> dict:
    return {"version": 1, "snapshots": {}}


def _load_store() -> dict:
    """读取存储；文件缺失时返回空结构（不落盘，直到首次写入）。"""
    data = load_json_file(store_path(), None)
    if not isinstance(data, dict):
        return _empty_store()
    snapshots = data.get("snapshots")
    if not isinstance(snapshots, dict):
        snapshots = {}
    version = data.get("version", 1)
    return {"version": int(version) if isinstance(version, int) else 1, "snapshots": snapshots}


def _save_store(store: dict) -> None:
    atomic_write_json(store_path(), store)


# ---------------------------------------------------------------------------
# 规范化与指纹
# ---------------------------------------------------------------------------

def normalize_odds(odds: dict | None) -> dict:
    """
    规范化赔率字典，用于指纹计算与比对。

    - 剔除「空值」：`None` 与空字符串。空值不构成市场信息，
      若保留会因字段出现/消失而产生虚假的盘口变动。
    - 键按字典序排列，保证与输入顺序无关。
    - 不做数值变换：有意义的数值差异一律保留。
    """
    if not isinstance(odds, dict):
        return {}
    cleaned: dict = {}
    for key, value in odds.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[str(key)] = value
    return {key: cleaned[key] for key in sorted(cleaned)}


def odds_fingerprint(odds: dict | None) -> str:
    """
    规范化赔率的稳定指纹。

    使用 `json.dumps(sort_keys=True)` + `sha256`，与字典顺序无关，
    也不使用跨进程不稳定的内置 `hash()`。
    """
    payload = json.dumps(
        normalize_odds(odds),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_id_for(match_id: str, captured_at: str, fingerprint: str) -> str:
    """由「match_id + captured_at + 赔率指纹」构造确定性快照 ID。"""
    raw = f"{match_id}|{captured_at}|{fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 构造（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def _jczq_no(match: dict, odds: dict) -> str:
    """竞彩编号：优先比赛记录自带，其次赔率里的 matchnum。"""
    value = match.get("jczq_no") or odds.get("matchnum") or ""
    return str(value)


def build_snapshot(
    match: dict,
    *,
    captured_at: datetime | None = None,
    source: str = ODDS_SOURCE,
) -> dict:
    """
    组装一条赔率历史记录（不落盘）。

    `match` 应为合并后的 canonical 比赛记录（含 `id` 与最新 `odds`）。
    `odds` 仅保留源实际提供的字段，不虚构缺失值。
    """
    match_id = str(match.get("id") or "")
    if not match_id:
        raise ValueError("赔率快照需要比赛 id（match_id）")

    normalized = normalize_odds(match.get("odds"))
    fingerprint = odds_fingerprint(normalized)
    captured = captured_at or get_beijing_now()
    captured_iso = captured.isoformat()
    kickoff = get_match_datetime(match)

    return {
        "snapshot_id": snapshot_id_for(match_id, captured_iso, fingerprint),
        "match_id": match_id,
        "sport": match.get("sport", "football"),
        "league": match.get("league", ""),
        "home_team": match.get("home", ""),
        "away_team": match.get("away", ""),
        "match_date": match.get("date", ""),
        "match_time": match.get("time", ""),
        "kickoff_at": kickoff.isoformat() if kickoff else None,
        "captured_at": captured_iso,
        "source": source,
        "jczq_no": _jczq_no(match, normalized),
        "odds": normalized,
        "odds_fingerprint": fingerprint,
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_odds_snapshot(snapshot_id: str) -> dict | None:
    """按 ID 读取单条赔率快照；不存在返回 None。"""
    with _LOCK:
        store = _load_store()
    snap = store["snapshots"].get(snapshot_id)
    return snap if isinstance(snap, dict) else None


def get_odds_history_for_match(match_id: str) -> list[dict]:
    """
    返回某场比赛的全部赔率历史，按 `captured_at` 升序（最早在前）；
    无记录时返回空列表。次级排序使用 `snapshot_id` 保证确定性。
    """
    with _LOCK:
        store = _load_store()
    found = [
        snap for snap in store["snapshots"].values()
        if isinstance(snap, dict) and snap.get("match_id") == match_id
    ]
    found.sort(key=lambda s: (s.get("captured_at") or "", s.get("snapshot_id") or ""))
    return found


def latest_odds_snapshot(match_id: str) -> dict | None:
    """返回某场最新一条赔率历史（用于连续重复比对）；无记录返回 None。"""
    history = get_odds_history_for_match(match_id)
    return history[-1] if history else None


def record_odds_snapshot(
    match: dict,
    *,
    now: datetime | None = None,
    source: str = ODDS_SOURCE,
) -> dict | None:
    """
    记录一次赛前盘口观察。

    行为
    ----
    - 无有效赔率（规范化后为空）-> 返回 None（无可记录内容）。
    - 比赛状态非 `upcoming`（即 live / finished）-> 返回 None，不写入。
    - 与「该场最新一条」规范化赔率相同（连续重复）-> 返回 None，不写入。
    - 否则追加一条不可变记录并原子落盘，返回该记录。

    返回值即「本次是否产生了新的历史观察」，便于调用方统计。
    """
    match_id = str(match.get("id") or "")
    if not match_id:
        raise ValueError("赔率快照需要比赛 id（match_id）")

    normalized = normalize_odds(match.get("odds"))
    if not normalized:
        return None

    status = add_time_status(dict(match), now).get("status")
    if status != "upcoming":
        return None

    snapshot = build_snapshot(match, captured_at=now, source=source)

    with _LOCK:
        store = _load_store()
        existing = store["snapshots"].get(snapshot["snapshot_id"])
        if isinstance(existing, dict):
            # 同一观察（同 captured_at + 同指纹）已存在，幂等返回。
            return existing

        latest = _latest_in_store(store, match_id)
        if latest is not None and latest.get("odds_fingerprint") == snapshot["odds_fingerprint"]:
            # 连续重复：市场未变，不产生新记录。
            return None

        store["snapshots"][snapshot["snapshot_id"]] = snapshot
        _save_store(store)

    return snapshot


def _latest_in_store(store: dict, match_id: str) -> dict | None:
    """在已加载的 store 内取某场最新一条记录（按 captured_at / snapshot_id）。"""
    found = [
        snap for snap in store["snapshots"].values()
        if isinstance(snap, dict) and snap.get("match_id") == match_id
    ]
    if not found:
        return None
    found.sort(key=lambda s: (s.get("captured_at") or "", s.get("snapshot_id") or ""))
    return found[-1]
