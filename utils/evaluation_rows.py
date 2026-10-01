"""
评估样本
========
把两份**已存在的、不可变的**历史记录物化为一条确定性的评估样本：

    预测快照（模型开赛前实际预测了什么）
            +
    结算（最终实际发生了什么）
            ↓
    评估样本行

评估样本行将成为后续模型表现计算的输入数据集。本模块**只做数据装配**，
不计算任何模型表现指标（命中率 / Brier / LogLoss / ROI / CLV / 校准误差），
也不产生任何"模型结论"（如 predicted_outcome）。

为什么单独一层
--------------
避免未来的评估代码同时依赖三个可变/运行时子系统。评估行必须自带评估所需的
全部历史信息，无需读取当前球队实力、当前盘口、当前比赛状态，也无需重跑模型。

核心不变量
----------
- 只有「一条预测快照 + 该快照对应的结算」同时存在时，才允许生成评估样本。
- 缺失的一方一律不生成，且**绝不**重建缺失的历史数据。
- 一条 (快照, 结算) 组合至多一条 canonical 评估样本。

严格禁止
--------
不调用 `predict_match(...)`、不调用 `enrich_match(...)`、不读取当前 Elo、
不读当前盘口、不重新结算。所有字段一律复制自权威历史记录。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime

import config
from utils.atomic_json import atomic_write_json, load_json_file
from utils.daily_loader import get_beijing_now

# 评估身份前缀
EVALUATION_ID_PREFIX = "evaluation"

_LOCK = threading.RLock()


class EvaluationIntegrityError(Exception):
    """
    评估样本的完整性校验失败。

    覆盖两类情形：
    - 拼接/溯源不一致：结算与快照不指向同一历史预测（reason 见下）
    - 源内容冲突：同一 evaluation_id 已存在，但新推导出的 source_fingerprint 不同

    既有评估样本始终保持**不变**；本模块不判断哪一方才是正确的。
    """

    def __init__(
        self,
        reason: str,
        *,
        evaluation_id: str | None = None,
        snapshot_id: str | None = None,
        settlement_id: str | None = None,
        match_id: str | None = None,
        detail: str = "",
    ) -> None:
        self.reason = reason
        self.evaluation_id = evaluation_id
        self.snapshot_id = snapshot_id
        self.settlement_id = settlement_id
        self.match_id = match_id
        self.detail = detail
        message = (
            f"评估样本完整性错误({reason}) match={match_id} snapshot={snapshot_id} "
            f"settlement={settlement_id} evaluation={evaluation_id}"
        )
        if detail:
            message = f"{message} :: {detail}"
        super().__init__(message)


# ---------------------------------------------------------------------------
# 存储位置与读写
# ---------------------------------------------------------------------------

def store_path() -> str:
    """评估样本数据文件路径（运行时生成，不纳入版本控制）。"""
    return os.path.join(config.DATA_DIR, config.EVALUATION_ROW_FILE)


def _empty_store() -> dict:
    return {"version": 1, "rows": {}}


def _load_store() -> dict:
    """读取存储；文件缺失时返回空结构（不落盘，直到首次写入）。"""
    data = load_json_file(store_path(), None)
    if not isinstance(data, dict):
        return _empty_store()
    rows = data.get("rows")
    if not isinstance(rows, dict):
        rows = {}
    version = data.get("version", 1)
    return {"version": int(version) if isinstance(version, int) else 1, "rows": rows}


def _save_store(store: dict) -> None:
    atomic_write_json(store_path(), store)


# ---------------------------------------------------------------------------
# 身份与指纹
# ---------------------------------------------------------------------------

def evaluation_id_for(snapshot_id: str, settlement_id: str) -> str:
    """
    评估身份：`sha256("evaluation|" + snapshot_id + "|" + settlement_id)`。

    确定性、跨进程稳定，不使用随机 UUID，也不使用内置 `hash()`。
    同时包含两个 ID 使来源可追溯（虽然 settlement_id 已由 snapshot_id 派生）。
    """
    raw = f"{EVALUATION_ID_PREFIX}|{snapshot_id}|{settlement_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def source_fingerprint(row_body: dict) -> str:
    """
    评估样本**源内容**的稳定指纹（不含 source_fingerprint / materialized_at）。

    使用 `json.dumps(sort_keys=True)` + `sha256`，与字典键顺序无关；
    有意义的数值差异（例如 0.55 与 0.56）会产生不同指纹。
    """
    payload = json.dumps(
        row_body, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_valid_prematch(generated_at: object, kickoff_at: object) -> bool:
    """
    历史时间戳校验：`prediction_generated_at <= kickoff_at`。

    只看历史预测时间与开赛时间，**不**使用 settled_at / materialized_at。
    两者任一缺失、不可解析或时区一致性不同时不做额外约束。
    """
    generated = _parse_iso(generated_at)
    kickoff = _parse_iso(kickoff_at)
    if generated is None or kickoff is None:
        return True
    if (generated.tzinfo is None) != (kickoff.tzinfo is None):
        return True
    return generated <= kickoff


def _validate_join(snapshot: dict, settlement: dict) -> None:
    """
    校验「快照 × 结算」确实指向同一条历史预测。

    任一项不一致都视为完整性错误（不静默归一化、不静默物化）。
    """
    snapshot_id = snapshot.get("snapshot_id")
    settlement_id = settlement.get("settlement_id")
    evaluation_id = evaluation_id_for(str(snapshot_id or ""), str(settlement_id or ""))

    if settlement.get("snapshot_id") != snapshot_id:
        raise EvaluationIntegrityError(
            "snapshot_id_mismatch",
            evaluation_id=evaluation_id,
            snapshot_id=snapshot_id,
            settlement_id=settlement_id,
            match_id=snapshot.get("match_id"),
            detail=f"结算指向 {settlement.get('snapshot_id')}，快照为 {snapshot_id}",
        )

    if settlement.get("match_id") != snapshot.get("match_id"):
        raise EvaluationIntegrityError(
            "match_id_mismatch",
            evaluation_id=evaluation_id,
            snapshot_id=snapshot_id,
            settlement_id=settlement_id,
            match_id=snapshot.get("match_id"),
            detail=f"结算 match_id={settlement.get('match_id')}，快照 match_id={snapshot.get('match_id')}",
        )

    provenance = (
        ("model_name", snapshot.get("model_name"), settlement.get("model_name")),
        ("model_version", snapshot.get("model_version"), settlement.get("model_version")),
        ("prediction_generated_at", snapshot.get("generated_at"), settlement.get("prediction_generated_at")),
        ("kickoff_at", snapshot.get("kickoff_at"), settlement.get("kickoff_at")),
    )
    for field, expected, actual in provenance:
        if actual != expected:
            raise EvaluationIntegrityError(
                "provenance_mismatch",
                evaluation_id=evaluation_id,
                snapshot_id=snapshot_id,
                settlement_id=settlement_id,
                match_id=snapshot.get("match_id"),
                detail=f"{field}: 快照={expected!r} 结算={actual!r}",
            )

    if not _is_valid_prematch(snapshot.get("generated_at"), snapshot.get("kickoff_at")):
        raise EvaluationIntegrityError(
            "invalid_prematch_timestamp",
            evaluation_id=evaluation_id,
            snapshot_id=snapshot_id,
            settlement_id=settlement_id,
            match_id=snapshot.get("match_id"),
            detail=(
                f"prediction_generated_at={snapshot.get('generated_at')!r} "
                f"晚于 kickoff_at={snapshot.get('kickoff_at')!r}"
            ),
        )


# ---------------------------------------------------------------------------
# 构造（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def _as_dict(value: object) -> dict:
    """保持历史字段的字典形态：快照里合法的 `{}` 原样保留，不填充新数据。"""
    return value if isinstance(value, dict) else {}


def build_evaluation_row(
    snapshot: dict,
    settlement: dict,
    *,
    materialized_at: datetime | None = None,
) -> dict:
    """
    组装一条评估样本（不落盘）。

    所有预测类字段**逐字复制自快照**，结果类字段**逐字复制自结算**。
    不重算预测、不读当前状态、不派生任何评估结论。

    拼接或溯源不一致时抛出 `EvaluationIntegrityError`。
    """
    _validate_join(snapshot, settlement)

    snapshot_id = str(snapshot.get("snapshot_id") or "")
    settlement_id = str(settlement.get("settlement_id") or "")
    evaluation_id = evaluation_id_for(snapshot_id, settlement_id)

    body = {
        "evaluation_id": evaluation_id,
        "snapshot_id": snapshot_id,
        "settlement_id": settlement_id,
        "match_id": snapshot.get("match_id", ""),
        "sport": snapshot.get("sport", ""),
        "league": snapshot.get("league", ""),
        "home_team": snapshot.get("home_team", ""),
        "away_team": snapshot.get("away_team", ""),
        "model_name": snapshot.get("model_name", ""),
        "model_version": snapshot.get("model_version", ""),
        "prediction_generated_at": snapshot.get("generated_at"),
        "kickoff_at": snapshot.get("kickoff_at"),
        "settled_at": settlement.get("settled_at"),
        "home_elo": snapshot.get("home_elo"),
        "away_elo": snapshot.get("away_elo"),
        "model_probabilities": _as_dict(snapshot.get("model_probabilities")),
        "display_probabilities": _as_dict(snapshot.get("display_probabilities")),
        "market_odds": _as_dict(snapshot.get("market_odds")),
        "market_implied_probabilities": _as_dict(snapshot.get("market_implied_probabilities")),
        "expected_values": _as_dict(snapshot.get("expected_values")),
        "expected_score_data": _as_dict(snapshot.get("expected_score_data")),
        "final_score": _as_dict(settlement.get("final_score")),
        "actual_outcome": settlement.get("actual_outcome", ""),
        "result_fingerprint": settlement.get("result_fingerprint", ""),
    }
    body["source_fingerprint"] = source_fingerprint(body)
    body["materialized_at"] = (materialized_at or get_beijing_now()).isoformat()
    return body


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def get_evaluation_row(evaluation_id: str) -> dict | None:
    """按 ID 读取单条评估样本；不存在返回 None。"""
    with _LOCK:
        store = _load_store()
    row = store["rows"].get(evaluation_id)
    return row if isinstance(row, dict) else None


def get_evaluation_row_for_snapshot(snapshot_id: str) -> dict | None:
    """
    返回某条预测快照对应的评估样本。

    由于 settlement_id 由 snapshot_id 派生，一条快照至多对应一条结算，
    因此这里至多命中一条评估样本；命中多条时按确定性排序返回第一条。
    """
    matches = [
        row for row in _all_rows()
        if row.get("snapshot_id") == snapshot_id
    ]
    if not matches:
        return None
    matches.sort(key=_row_sort_key)
    return matches[0]


def get_evaluation_rows_for_match(match_id: str) -> list[dict]:
    """返回某场比赛的全部评估样本（确定性排序）；无记录返回空列表。"""
    rows = [row for row in _all_rows() if row.get("match_id") == match_id]
    rows.sort(key=_row_sort_key)
    return rows


def get_all_evaluation_rows() -> list[dict]:
    """返回全部评估样本，按预测生成时间 / 比赛 / 模型版本 / 评估 ID 确定性升序。"""
    rows = _all_rows()
    rows.sort(key=_row_sort_key)
    return rows


def _all_rows() -> list[dict]:
    with _LOCK:
        store = _load_store()
    return [row for row in store["rows"].values() if isinstance(row, dict)]


def _row_sort_key(row: dict) -> tuple:
    return (
        row.get("prediction_generated_at") or "",
        row.get("match_id") or "",
        row.get("model_version") or "",
        row.get("evaluation_id") or "",
    )


def capture_evaluation_row(
    snapshot: dict | None,
    settlement: dict | None,
    *,
    now: datetime | None = None,
) -> tuple[dict | None, bool]:
    """
    为「快照 × 结算」物化一条 canonical 评估样本。

    返回 `(row, created)`：
        created=True  -> 本次确实新增并落盘了一条评估样本
        created=False -> 未新增；row 为既有样本

    返回 `(None, False)`（无可物化内容）的情形：
        - 快照或结算缺失（例如只有快照、没有结算）

    拼接/溯源不一致，或同一 evaluation_id 的源内容指纹发生变化时，
    抛出 `EvaluationIntegrityError`；既有样本保持不变。
    """
    if not isinstance(snapshot, dict) or not isinstance(settlement, dict):
        return None, False
    if not snapshot.get("snapshot_id") or not settlement.get("settlement_id"):
        return None, False

    candidate = build_evaluation_row(snapshot, settlement, materialized_at=now)
    evaluation_id = candidate["evaluation_id"]

    with _LOCK:
        store = _load_store()
        existing = store["rows"].get(evaluation_id)
        if isinstance(existing, dict):
            if existing.get("source_fingerprint") != candidate["source_fingerprint"]:
                raise EvaluationIntegrityError(
                    "source_fingerprint_conflict",
                    evaluation_id=evaluation_id,
                    snapshot_id=candidate["snapshot_id"],
                    settlement_id=candidate["settlement_id"],
                    match_id=candidate["match_id"],
                    detail=(
                        f"既有指纹={existing.get('source_fingerprint')} "
                        f"新推导指纹={candidate['source_fingerprint']}"
                    ),
                )
            return existing, False

        store["rows"][evaluation_id] = candidate
        _save_store(store)

    return candidate, True
