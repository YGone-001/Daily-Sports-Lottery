"""
赛前预测结算
============
把一条**已存在的、不可变的赛前预测快照**与其最终观测到的比赛结果关联起来。

数据生命周期：

    upcoming -> 预测快照 -> 开赛 -> 完赛 -> 观测到最终比分 -> 结算记录

结算只回答一个问题：

    这条历史预测快照对应的最终结果是什么？

本模块**不**计算任何模型表现指标（命中率 / Brier / LogLoss / ROI / CLV / 校准误差），
也**不**做任何货币派彩结算。它只记录事实性赛果。

核心不变量
----------
- 只结算「开赛前确实存在」的预测快照。
- 完结比赛若没有预测快照 -> 不结算，且**绝不**回溯生成预测。
- 一条预测快照至多一条 canonical 结算记录（身份只由 snapshot_id 决定）。
- 上游结果若与既有结算不一致 -> 抛冲突，既有记录保持不变。

职责边界
--------
不做网络抓取、不跑预测模型、不更新 Elo、不计算指标。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime

import config
from utils.atomic_json import atomic_write_json, load_json_file
from utils.daily_loader import add_time_status, get_beijing_now
from utils.match_lifecycle import valid_full_time_score

# 结算身份前缀：与「结果指纹」解耦，保证同一快照的结算 ID 与最终比分无关。
SETTLEMENT_ID_PREFIX = "settlement"

_LOCK = threading.RLock()

# 有效结果标识（与模型概率使用的命名保持一致）
OUTCOME_HOME_WIN = "home_win"
OUTCOME_DRAW = "draw"
OUTCOME_AWAY_WIN = "away_win"


class SettlementConflictError(Exception):
    """
    同一条预测快照被上游报告了不同的最终结果。

    既有结算记录保持**不变**，本模块不判断哪个比分才是正确的；
    数据订正属于独立策略，后续再实现。
    """

    def __init__(
        self,
        match_id: str | None,
        snapshot_id: str,
        existing_score: dict | None,
        incoming_score: dict | None,
    ) -> None:
        self.match_id = match_id
        self.snapshot_id = snapshot_id
        self.existing_score = existing_score
        self.incoming_score = incoming_score
        super().__init__(
            f"结算冲突 match={match_id} snapshot={snapshot_id} "
            f"既有={_format_score(existing_score)} 新到={_format_score(incoming_score)}"
        )


def _format_score(score: dict | None) -> str:
    if not isinstance(score, dict):
        return "?"
    return f"{score.get('home')}-{score.get('away')}"


# ---------------------------------------------------------------------------
# 存储位置与读写
# ---------------------------------------------------------------------------

def store_path() -> str:
    """结算数据文件路径（运行时生成，不纳入版本控制）。"""
    return os.path.join(config.DATA_DIR, config.SETTLEMENT_FILE)


def _empty_store() -> dict:
    return {"version": 1, "settlements": {}}


def _load_store() -> dict:
    """读取存储；文件缺失时返回空结构（不落盘，直到首次写入）。"""
    data = load_json_file(store_path(), None)
    if not isinstance(data, dict):
        return _empty_store()
    settlements = data.get("settlements")
    if not isinstance(settlements, dict):
        settlements = {}
    version = data.get("version", 1)
    return {"version": int(version) if isinstance(version, int) else 1, "settlements": settlements}


def _save_store(store: dict) -> None:
    atomic_write_json(store_path(), store)


# ---------------------------------------------------------------------------
# 比分提取与结果指纹
# ---------------------------------------------------------------------------

def extract_final_score(match: dict) -> dict | None:
    """
    从比赛记录中提取规范化的最终比分。

    最终比分的有效性由 `utils.match_lifecycle.valid_full_time_score` **统一裁决**——
    本模块不再独立定义一套「数值型（int 或 float）」的竞争性规则。

    语义：
    - `score["ft"]` 必须为 list / tuple 且长度 >= 2；
    - 前两个元素必须为**非负的精确整数**（严格排除 bool / float / str 等可强转类型）；
    - 半场比分、缺失字段、字符串、浮点、布尔、负数一律返回 None。不虚构比分，
      也不做数值归一化（如把 `2.0` 归一为 `2`）。
    """
    if not valid_full_time_score(match.get("score")):
        return None
    ft = match["score"]["ft"]
    return {"home": ft[0], "away": ft[1]}


def result_fingerprint(final_score: dict) -> str:
    """
    最终结果的稳定指纹（与字典顺序无关）。

    使用 `json.dumps(sort_keys=True)` + `sha256`；数值差异会被保留
    （2-1 与 2-2 指纹不同），不相等比较使用原始数值。
    """
    payload = json.dumps(
        {"home": final_score.get("home"), "away": final_score.get("away")},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def outcome_from_score(final_score: dict) -> str:
    """
    由最终比分推导事实性结果标识。

    平局按事实记录（篮球正常不会出现，但若上游确实给出相等比分，不虚构胜者）。
    """
    home, away = final_score.get("home"), final_score.get("away")
    if home > away:
        return OUTCOME_HOME_WIN
    if home == away:
        return OUTCOME_DRAW
    return OUTCOME_AWAY_WIN


# ---------------------------------------------------------------------------
# 身份与时间戳校验
# ---------------------------------------------------------------------------

def settlement_id_for(snapshot_id: str) -> str:
    """
    结算身份：`sha256("settlement|" + snapshot_id)`。

    只依赖 snapshot_id，**不包含最终比分**——这样上游结果变化时会被识别为冲突，
    而不是悄悄为同一条预测生成第二条 canonical 结算。
    """
    raw = f"{SETTLEMENT_ID_PREFIX}|{snapshot_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_valid_prematch(snapshot: dict) -> bool:
    """
    校验快照确实代表一条赛前预测：`generated_at <= kickoff_at`。

    两者任一缺失或不可解析时不做额外约束（现有系统生成的快照两者始终可用；
    缺失只可能来自外部手工数据）。两者时区一致性不同则跳过比较，避免误判。
    """
    generated = _parse_iso(snapshot.get("generated_at"))
    kickoff = _parse_iso(snapshot.get("kickoff_at"))
    if generated is None or kickoff is None:
        return True
    if (generated.tzinfo is None) != (kickoff.tzinfo is None):
        return True
    return generated <= kickoff


# ---------------------------------------------------------------------------
# 构造（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def build_settlement(
    snapshot: dict,
    match: dict,
    final_score: dict,
    *,
    settled_at: datetime | None = None,
) -> dict:
    """
    组装一条结算记录（不落盘）。

    描述性溯源信息（模型名称/版本、生成时间、开赛时间、球队、联赛）
    一律取自**快照**——它是权威的历史记录；最终比分取自比赛记录。
    不重新计算预测。
    """
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    if not snapshot_id:
        raise ValueError("结算需要预测快照的 snapshot_id")

    settled = settled_at or get_beijing_now()
    return {
        "settlement_id": settlement_id_for(snapshot_id),
        "snapshot_id": snapshot_id,
        "match_id": snapshot.get("match_id") or match.get("id") or "",
        "sport": snapshot.get("sport", ""),
        "league": snapshot.get("league", ""),
        "home_team": snapshot.get("home_team", ""),
        "away_team": snapshot.get("away_team", ""),
        "model_name": snapshot.get("model_name", ""),
        "model_version": snapshot.get("model_version", ""),
        "prediction_generated_at": snapshot.get("generated_at"),
        "kickoff_at": snapshot.get("kickoff_at"),
        "settled_at": settled.isoformat(),
        "final_score": {"home": final_score.get("home"), "away": final_score.get("away")},
        "actual_outcome": outcome_from_score(final_score),
        "result_fingerprint": result_fingerprint(final_score),
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_settlement(settlement_id: str) -> dict | None:
    """按 ID 读取单条结算；不存在返回 None。"""
    with _LOCK:
        store = _load_store()
    record = store["settlements"].get(settlement_id)
    return record if isinstance(record, dict) else None


def get_settlements_for_match(match_id: str) -> list[dict]:
    """
    返回某场比赛的全部结算记录，按 `prediction_generated_at` -> `model_version`
    -> `settlement_id` 确定性升序；无记录时返回空列表。
    """
    with _LOCK:
        store = _load_store()
    found = [
        record for record in store["settlements"].values()
        if isinstance(record, dict) and record.get("match_id") == match_id
    ]
    found.sort(
        key=lambda r: (
            r.get("prediction_generated_at") or "",
            r.get("model_version") or "",
            r.get("settlement_id") or "",
        )
    )
    return found


def get_settlement_for_snapshot(snapshot_id: str) -> dict | None:
    """返回某条预测快照的 canonical 结算；不存在返回 None。"""
    return get_settlement(settlement_id_for(str(snapshot_id)))


def settle_snapshot(
    snapshot: dict,
    match: dict,
    *,
    now: datetime | None = None,
) -> tuple[dict | None, bool]:
    """
    为一条预测快照创建 canonical 结算。

    返回 `(settlement, created)`：
        created=True  -> 本次确实新增并落盘了一条结算
        created=False -> 未新增；settlement 为既有记录

    返回 `(None, False)`（未产生结算）的情形：
        - 比赛状态不是 `finished`（upcoming / live 一律不结算）
        - `score.ft` 不是有效的双方最终比分
        - 快照缺少 snapshot_id
        - 快照时间戳不满足 `prediction_generated_at <= kickoff_at`

    若上游对同一快照给出**不同的**最终结果，抛出 `SettlementConflictError`，
    既有结算记录保持不变。
    """
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    if not snapshot_id:
        return None, False

    # 仅完结比赛可结算（复用既有时间状态判定）
    if add_time_status(dict(match), now).get("status") != "finished":
        return None, False

    final_score = extract_final_score(match)
    if final_score is None:
        return None, False

    if not is_valid_prematch(snapshot):
        return None, False

    settlement_id = settlement_id_for(snapshot_id)
    fingerprint = result_fingerprint(final_score)

    with _LOCK:
        store = _load_store()
        existing = store["settlements"].get(settlement_id)
        if isinstance(existing, dict):
            if existing.get("result_fingerprint") != fingerprint:
                raise SettlementConflictError(
                    existing.get("match_id") or match.get("id"),
                    snapshot_id,
                    existing.get("final_score"),
                    final_score,
                )
            return existing, False

        settlement = build_settlement(snapshot, match, final_score, settled_at=now)
        store["settlements"][settlement_id] = settlement
        _save_store(store)

    return settlement, True
