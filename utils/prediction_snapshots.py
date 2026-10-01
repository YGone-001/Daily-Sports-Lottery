"""
赛前预测快照
============
把「开赛前模型此刻的预测」固化为一条不可变的历史记录，供后续实现
无泄漏（leakage-free）的结算与回测使用。

职责边界
--------
- 本模块只负责「编排 + 持久化」：调用 `models.predictor.predict_match` 取预测，
  决定是否应存在快照，再单独落盘。
- 预测数学始终留在 `models.predictor`，模型代码保持确定性与无副作用。

关键设计
--------
- 身份确定性：`snapshot_id = sha256(match_id | slot | model_version)`，
  不使用 Python 内置 `hash()`（其跨进程不稳定）。
- 唯一性：每场（match_id）× 每模型版本仅一条 canonical 赛前快照。
  存储以 snapshot_id 为键，天然去重。
- 不可变：一旦某 snapshot_id 已存在，后续调用只读返回，绝不覆盖。
- 时序：仅 `upcoming` 允许新建；`live` / `finished` 一律拒绝新建，
  但已有快照仍可读取。
- 并发：进程内 `threading.RLock` 保护「读-改-写」，配合原子落盘，
  避免重复快照与并发写坏 JSON。
"""
from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime

import config
from utils.atomic_json import atomic_write_json, load_json_file
from utils.daily_loader import add_time_status, get_beijing_now, get_match_datetime

# 赛前快照的「生成槽位」常量。
# 当前基线规则为「每场每模型版本一条 canonical 赛前快照」，故槽位固定为 prematch；
# 该值参与 snapshot_id 计算，保证身份稳定可复现。
SNAPSHOT_SLOT = "prematch"

# 各运动的模型名称（人类可读，随快照落盘）。
MODEL_NAMES = {
    "football": "elo-poisson-dixon-coles",
    "basketball": "elo-normal-points",
}

# 进程内锁：Flask 请求线程与后台抓取线程共享同一进程时，保护快照文件的读-改-写。
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 存储位置与读写
# ---------------------------------------------------------------------------

def store_path() -> str:
    """快照数据文件路径（运行时生成，不纳入版本控制）。"""
    return os.path.join(config.DATA_DIR, config.PREDICTION_SNAPSHOT_FILE)


def _empty_store() -> dict:
    return {"version": 1, "snapshots": {}}


def _load_store() -> dict:
    """
    读取快照存储。文件缺失时惰性初始化为空结构（不落盘，直到首次写入）。
    存储以 snapshot_id 为键的字典形式保存，保证去重与 O(1) 查询。
    """
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
# 身份与模型版本
# ---------------------------------------------------------------------------

def snapshot_id_for(match_id: str, model_version: str, slot: str = SNAPSHOT_SLOT) -> str:
    """
    由「match_id + 生成槽位 + 模型版本」构造确定性快照 ID。
    使用 sha256 而非内置 hash()，保证跨进程/跨运行稳定。
    """
    raw = f"{match_id}|{slot}|{model_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_model_version(model_version: str | None) -> str:
    return model_version or config.MODEL_VERSION


def model_name_for(sport: str) -> str:
    return MODEL_NAMES.get(sport, "elo-baseline")


# ---------------------------------------------------------------------------
# 快照构造（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _team_elo(team: dict | None) -> float | None:
    if not isinstance(team, dict):
        return None
    elo = team.get("elo_rating")
    return elo if isinstance(elo, (int, float)) else None


def _expected_score_data(sport: str, prediction: dict) -> dict:
    """
    按运动类型抽取模型实际产出的「比分/得分」数据。
    只搬运模型已生成的信息，不虚构额外状态。
    """
    if sport == "basketball":
        bb = prediction.get("basketball") or {}
        keys = (
            "expected_home_points",
            "expected_away_points",
            "expected_total",
            "spread",
            "over_line",
            "over_prob",
            "under_prob",
        )
        return {k: bb.get(k) for k in keys if k in bb}
    return {
        "expected_goals": dict(prediction.get("expected_goals") or {}),
        "top_scores": list(prediction.get("top_scores") or []),
        "goals_prediction": dict(prediction.get("goals_prediction") or {}),
    }


def build_snapshot(
    match: dict,
    prediction: dict,
    *,
    model_version: str | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """
    组装一条快照记录（不落盘）。

    `match` 应为已富化的比赛（含 `home_team` / `away_team` 实力档案），
    `prediction` 为 `predict_match(...)` 的返回值。
    """
    model_version = resolve_model_version(model_version)
    match_id = str(match.get("id") or "")
    if not match_id:
        raise ValueError("快照需要比赛 id（match_id）")

    sport = match.get("sport", "football")
    market = prediction.get("market") or {}
    kickoff = get_match_datetime(match)
    home_team = match.get("home_team") or {}
    away_team = match.get("away_team") or {}

    return {
        "snapshot_id": snapshot_id_for(match_id, model_version),
        "match_id": match_id,
        "sport": sport,
        "league": match.get("league", ""),
        "home_team": match.get("home") or home_team.get("name", ""),
        "away_team": match.get("away") or away_team.get("name", ""),
        "match_date": match.get("date", ""),
        "match_time": match.get("time", ""),
        "kickoff_at": _iso(kickoff),
        "generated_at": _iso(generated_at or get_beijing_now()),
        "model_name": model_name_for(sport),
        "model_version": model_version,
        "home_elo": _team_elo(home_team),
        "away_elo": _team_elo(away_team),
        "model_probabilities": dict(prediction.get("model_probabilities") or {}),
        "display_probabilities": dict(prediction.get("probabilities") or {}),
        "expected_values": prediction.get("ev_analysis") or {},
        "market_odds": dict(market.get("odds") or {}),
        "market_implied_probabilities": dict(market.get("implied_probabilities") or {}),
        "expected_score_data": _expected_score_data(sport, prediction),
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_snapshot(snapshot_id: str) -> dict | None:
    """按 ID 读取单条快照；不存在返回 None。"""
    with _LOCK:
        store = _load_store()
    snap = store["snapshots"].get(snapshot_id)
    return snap if isinstance(snap, dict) else None


def get_snapshots_for_match(match_id: str) -> list[dict]:
    """返回某场比赛的全部快照（按生成时间升序）；无记录时返回空列表。"""
    with _LOCK:
        store = _load_store()
    found = [
        snap for snap in store["snapshots"].values()
        if isinstance(snap, dict) and snap.get("match_id") == match_id
    ]
    found.sort(key=lambda s: (s.get("generated_at") or "", s.get("snapshot_id") or ""))
    return found


def snapshot_exists(match_id: str, model_version: str | None = None) -> bool:
    """某场某模型版本的 canonical 赛前快照是否已存在。"""
    sid = snapshot_id_for(str(match_id), resolve_model_version(model_version))
    return get_snapshot(sid) is not None


def create_snapshot(
    match: dict,
    prediction: dict | None = None,
    *,
    model_version: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """
    为一场比赛创建 canonical 赛前快照。

    行为
    ----
    - 仅当比赛状态为 `upcoming` 时才新建；`live` / `finished` 返回已有快照或 None。
    - `prediction` 为空时，内部调用 `models.predictor.predict_match` 计算。
    - 幂等：同 match_id + 同 model_version 若已有快照，直接返回既有记录，不覆盖。
    - 返回值：该场该模型版本的快照记录（新建或既有），或在被拒绝时返回 None。
    """
    model_version = resolve_model_version(model_version)
    match_id = str(match.get("id") or "")
    if not match_id:
        raise ValueError("快照需要比赛 id（match_id）")

    snapshot_id = snapshot_id_for(match_id, model_version)

    # 时序门禁：以比赛当前状态为准（upcoming 才允许新建）
    status = add_time_status(dict(match), now).get("status")
    if status != "upcoming":
        return get_snapshot(snapshot_id)

    if prediction is None:
        from models.predictor import predict_match

        prediction = predict_match(
            match.get("home_team") or {},
            match.get("away_team") or {},
            odds=match.get("odds"),
            sport=match.get("sport", "football"),
            league=match.get("league", ""),
        )

    snapshot = build_snapshot(
        match, prediction, model_version=model_version, generated_at=now
    )

    with _LOCK:
        store = _load_store()
        existing = store["snapshots"].get(snapshot_id)
        if isinstance(existing, dict):
            # 不可变：已有快照一律原样返回，绝不用新状态覆盖。
            return existing
        store["snapshots"][snapshot_id] = snapshot
        _save_store(store)

    return snapshot


def ensure_snapshot(
    match: dict,
    prediction: dict | None = None,
    *,
    model_version: str | None = None,
    now: datetime | None = None,
) -> dict | None:
    """
    面向调用方的便捷入口：传入原始比赛时自动补齐球队实力后再建快照。

    与 `create_snapshot` 的唯一区别是：当 `match` 尚未富化（缺少 home_team /
    away_team）时，先经 `utils.daily_loader.enrich_match` 补齐，保持调用点简洁。
    """
    if not match.get("home_team") or not match.get("away_team"):
        from utils.daily_loader import enrich_match

        match = enrich_match(match, now)
    return create_snapshot(match, prediction, model_version=model_version, now=now)
