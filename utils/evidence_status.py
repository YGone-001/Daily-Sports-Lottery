"""
Production Evidence Status Core
===============================
**只读**运行状态汇总：从权威运行时存储推导「证据计数 / 完整性计数 / 证据门禁」。

目的
----
让未来的证据积累**可度量**，无需反复编写一次性巡检脚本。

设计约束
--------
- **只读**：绝不写入 / 改写任何运行时存储，也不直接重写 JSON。
- **确定性**：输出与调用顺序无关，模型分组按 `(sport, model_name, model_version)` 字典序。
- **不重算指标**：分类 / 校准 / 时序诊断一律复用既有评估器，不在此模块重新实现
  Accuracy / Brier / LogLoss / ECE / 时序窗口。
- **不合并模型版本**：当前版本与历史版本分开报告。
- **零数据安全**：存储为空时返回事实性零计数，不抛异常、不伪造指标。
- **不推断就绪**：`collection_ready` 只依据真实快照 / 盘口管道证据，绝不由评估样本数量推断。

门禁语义（每个当前模型版本）
----------------------------
```text
evaluation_rows 0..29   -> below A  (gate = "R" 当且仅当存在同场「当前版本预测快照 × 盘口快照」证据)
evaluation_rows 30..99  -> Gate A
evaluation_rows 100..299-> Gate B
evaluation_rows 300+    -> Gate C
```
Gate R 表示**真实的当前版本采集路径已被证明**（同一场比赛同时有当前版本预测快照与盘口快照），
而不是「存在任意预测快照对象」。仅有预测快照、或仅有盘口快照，都不构成 R。
门禁是**运营性证据复核阈值**，不是统计显著性判断，也不得转成质量评分 / 置信度标签。
"""
from __future__ import annotations

import json
from typing import Iterable

import config

# 评估样本数 -> 门禁标签（降序匹配）
_GATE_BOUNDS: tuple[tuple[int, str], ...] = ((300, "C"), (100, "B"), (30, "A"))

# 门禁标签：达到采集就绪但样本不足 Gate A 时为 "R"；无任何证据为 "none"。
GATE_NONE = "none"
GATE_COLLECTION = "R"

# 视为「当前版本」的运动集合来自 config.MODEL_VERSIONS（权威来源）。


# ---------------------------------------------------------------------------
# 基础工具（纯函数，无 I/O）
# ---------------------------------------------------------------------------

def _as_list(value: object) -> list[dict]:
    """把「字典值集合 / 列表 / None」统一成 dict 列表，非 dict 项被丢弃。"""
    if value is None:
        return []
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, dict)]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, dict)]
    return []


def _duplicates(records: Iterable[dict], key_field: str) -> int:
    """
    按既有身份字段统计重复数：每个键出现 n 次计 `n - 1` 条重复。

    复用既有身份语义（snapshot_id / settlement_id / evaluation_id），
    不发明竞争性主键。键缺失（None）的行不参与重复判定。
    """
    counts: dict[object, int] = {}
    for record in records:
        key = record.get(key_field)
        if key is None:
            continue
        counts[key] = counts.get(key, 0) + 1
    return sum(c - 1 for c in counts.values() if c > 1)


def gate_for(evaluation_rows: int, matched_snapshot_odds_matches: int) -> str:
    """
    返回该模型版本当前达到的证据门禁标签（"none" / "R" / "A" / "B" / "C"）。

    Gate R 要求**同场**证据：至少一场比赛同时拥有该当前版本的预测快照与盘口快照。
    仅有预测快照对象、或仅有盘口快照，都**不**构成 R（返回 "none"）。
    Gate A/B/C 仅由真实评估样本数决定，阈值不变。
    """
    for threshold, label in _GATE_BOUNDS:
        if evaluation_rows >= threshold:
            return label
    if matched_snapshot_odds_matches >= 1:
        return GATE_COLLECTION
    return GATE_NONE


def _is_finished(match: dict) -> bool:
    """canonical 终态判定：finished 且含严格有效的全场比分。"""
    from utils.daily_loader import add_time_status
    from utils.match_lifecycle import valid_full_time_score

    if add_time_status(dict(match)).get("status") != "finished":
        return False
    return valid_full_time_score(match.get("score"))


def _is_valid_prematch(snapshot: dict) -> bool:
    """复用既有结算资格判定（prediction_generated_at <= kickoff_at）。"""
    from utils.settlements import is_valid_prematch

    return bool(is_valid_prematch(snapshot))


# ---------------------------------------------------------------------------
# 计数与完整性（纯函数）
# ---------------------------------------------------------------------------

def _count_model(records: Iterable[dict], sport: str, model_name: str, model_version: str) -> int:
    return sum(
        1 for r in records
        if r.get("sport") == sport
        and r.get("model_name") == model_name
        and r.get("model_version") == model_version
    )


def _model_breakdowns(
    snapshots: list[dict],
    odds_snapshots: list[dict],
    settlements: list[dict],
    evaluation_rows: list[dict],
    versions: dict,
    names: dict,
) -> tuple[list[dict], list[dict], set]:
    """
    返回 (当前版本分组, 历史版本分组, 全局同场匹配的 match_id 集合)。

    当前版本分组的 `matched_snapshot_odds_matches` = 该模型的当前版本预测快照 match_id
    与盘口快照 match_id 的**交集**大小。仅当前版本参与，历史版本不参与采集就绪判定。
    """
    odds_match_ids = {
        o.get("match_id") for o in odds_snapshots if o.get("match_id") is not None
    }

    current: list[dict] = []
    current_keys: set[tuple] = set()
    matched_all: set = set()
    for sport in sorted(versions):
        version = versions[sport]
        name = names.get(sport, "")
        current_keys.add((sport, name, version))

        model_snaps = [
            s for s in snapshots
            if s.get("sport") == sport
            and s.get("model_name") == name
            and s.get("model_version") == version
        ]
        current_match_ids = {
            s.get("match_id") for s in model_snaps if s.get("match_id") is not None
        }
        matched = current_match_ids & odds_match_ids
        matched_all |= matched

        rows = _count_model(evaluation_rows, sport, name, version)
        current.append({
            "sport": sport,
            "model_name": name,
            "model_version": version,
            "prediction_snapshots": len(model_snaps),
            "settlements": _count_model(settlements, sport, name, version),
            "evaluation_rows": rows,
            "matched_snapshot_odds_matches": len(matched),
            "gate": gate_for(rows, len(matched)),
        })

    buckets: dict[tuple, dict] = {}

    def _add(records: list[dict], field: str) -> None:
        for r in records:
            key = (r.get("sport", ""), r.get("model_name", ""), r.get("model_version", ""))
            if key in current_keys:
                continue
            bucket = buckets.setdefault(key, {
                "sport": key[0], "model_name": key[1], "model_version": key[2],
                "prediction_snapshots": 0, "settlements": 0, "evaluation_rows": 0,
            })
            bucket[field] += 1

    _add(snapshots, "prediction_snapshots")
    _add(settlements, "settlements")
    _add(evaluation_rows, "evaluation_rows")

    historical = [buckets[k] for k in sorted(buckets)]
    return current, historical, matched_all


def _integrity(
    matches: list[dict],
    snapshots: list[dict],
    settlements: list[dict],
    evaluation_rows: list[dict],
) -> dict:
    """计算完整性计数。全部为「问题计数」，0 表示健康。"""
    settled_snapshot_ids = {s.get("snapshot_id") for s in settlements}
    eval_settlement_ids = {r.get("settlement_id") for r in evaluation_rows}
    eval_match_ids = {r.get("match_id") for r in evaluation_rows}
    match_by_id = {m.get("id"): m for m in matches}
    snaps_by_match: dict[object, list[dict]] = {}
    for s in snapshots:
        snaps_by_match.setdefault(s.get("match_id"), []).append(s)

    # 已完赛且结算资格成立、但缺少结算的快照（未来 / 未完赛快照不计入失败）。
    unsettled_eligible = 0
    for s in snapshots:
        match = match_by_id.get(s.get("match_id"))
        if not match or not _is_finished(match):
            continue
        if not _is_valid_prematch(s):
            continue
        if s.get("snapshot_id") not in settled_snapshot_ids:
            unsettled_eligible += 1

    # 已完赛但没有评估样本的 canonical 比赛：按「是否存在合格赛前快照」二分。
    # 分类依据是**资格**（既有 is_valid_prematch），而不是字面上的快照存在与否。
    #   finished_without_evaluation
    #     = finished_without_evaluation_no_eligible_snapshot
    #     + finished_without_evaluation_had_eligible_snapshot
    finished_without_eval = [m for m in matches if _is_finished(m) and m.get("id") not in eval_match_ids]
    never_snapshot = 0   # 信息性子计数：字面上零快照
    no_eligible = 0      # 权威分类：零快照，或快照存在但无任一合格
    had_eligible = 0     # 权威分类：至少一条合格赛前快照
    for m in finished_without_eval:
        match_snaps = snaps_by_match.get(m.get("id"), [])
        if not match_snaps:
            never_snapshot += 1
            no_eligible += 1
        elif any(_is_valid_prematch(s) for s in match_snaps):
            had_eligible += 1
        else:
            no_eligible += 1

    return {
        "duplicate_snapshots": _duplicates(snapshots, "snapshot_id"),
        "duplicate_settlements": _duplicates(settlements, "settlement_id"),
        "duplicate_evaluation_rows": _duplicates(evaluation_rows, "evaluation_id"),
        "unsettled_eligible_snapshots": unsettled_eligible,
        "finished_without_evaluation": len(finished_without_eval),
        "finished_without_evaluation_no_eligible_snapshot": no_eligible,
        "finished_without_evaluation_had_eligible_snapshot": had_eligible,
        "finished_without_evaluation_never_snapshot": never_snapshot,
        "settlements_without_evaluation": sum(
            1 for s in settlements if s.get("settlement_id") not in eval_settlement_ids
        ),
    }


def _diagnostics(evaluation_rows: list[dict]) -> dict:
    """
    复用既有评估器（不重算指标）。仅在存在评估样本时调用。

    某个评估器若因畸形 / 重复样本抛错，则该项以 `{"error": ..., "detail": ...}` 形式
    如实标记，**不**中断整体状态输出（相应问题已由 integrity 计数单独暴露）。
    """
    from utils.calibration_evaluation import build_calibration_summaries
    from utils.classification_evaluation import build_classification_summaries
    from utils.temporal_evaluation import build_temporal_summaries

    diagnostics: dict = {}
    for key, evaluator in (
        ("classification", build_classification_summaries),
        ("calibration", build_calibration_summaries),
        ("temporal", build_temporal_summaries),
    ):
        try:
            diagnostics[key] = evaluator(evaluation_rows)
        except Exception as exc:  # noqa: BLE001 - 状态模块不应因单项诊断失败而中断
            diagnostics[key] = {"error": type(exc).__name__, "detail": str(exc)}
    return diagnostics


def _default_model_names(versions: dict) -> dict:
    from utils.prediction_snapshots import model_name_for

    return {sport: model_name_for(sport) for sport in versions}


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def build_evidence_status(
    *,
    matches: Iterable[dict] = (),
    snapshots: Iterable[dict] = (),
    odds_snapshots: Iterable[dict] = (),
    settlements: Iterable[dict] = (),
    evaluation_rows: Iterable[dict] = (),
    model_versions: dict | None = None,
    model_names: dict | None = None,
    with_diagnostics: bool = True,
) -> dict:
    """
    由**给定**的运行数据构建证据状态（纯计算，不读文件、不写文件、不修改输入）。

    参数均为内存中的记录列表，便于独立测试；`get_evidence_status()` 负责从运行时存储读取。
    """
    matches = _as_list(matches)
    snapshots = _as_list(snapshots)
    odds_snapshots = _as_list(odds_snapshots)
    settlements = _as_list(settlements)
    evaluation_rows = _as_list(evaluation_rows)

    versions = dict(model_versions if model_versions is not None else config.MODEL_VERSIONS)
    names = dict(model_names) if model_names else _default_model_names(versions)

    models, historical, matched_collection_match_ids = _model_breakdowns(
        snapshots, odds_snapshots, settlements, evaluation_rows, versions, names
    )
    integrity = _integrity(matches, snapshots, settlements, evaluation_rows)

    total_current_snapshots = sum(m["prediction_snapshots"] for m in models)

    # 采集就绪：必须存在**同一场比赛**同时拥有「当前版本预测快照」与「盘口快照」。
    # 跨场匹配（快照属比赛 A、盘口属比赛 B）无效；历史版本快照不计入；
    # 仅有预测快照或仅有盘口快照都不算。绝不由评估样本数量推断。
    collection_ready = bool(matched_collection_match_ids)

    # 管道完整性：重复身份 / 卡住的合格快照 / 无评估样本的结算 均为 0。
    # 注意：已完赛但**没有合格赛前快照**的历史比赛属预期情形，不计为失败。
    integrity_failures = (
        integrity["duplicate_snapshots"]
        + integrity["duplicate_settlements"]
        + integrity["duplicate_evaluation_rows"]
        + integrity["unsettled_eligible_snapshots"]
        + integrity["finished_without_evaluation_had_eligible_snapshot"]
        + integrity["settlements_without_evaluation"]
    )
    pipeline_integrity = integrity_failures == 0

    evidence_review_ready = any(m["gate"] in ("B", "C") for m in models)

    diagnostics = _diagnostics(evaluation_rows) if (with_diagnostics and evaluation_rows) else None

    return {
        "models": models,
        "historical_models": historical,
        "integrity": integrity,
        "collection_ready": collection_ready,
        "collection_evidence": {
            "prediction_snapshots": total_current_snapshots,
            "odds_snapshots": len(odds_snapshots),
            "matched_snapshot_odds_matches": len(matched_collection_match_ids),
        },
        "pipeline_integrity": pipeline_integrity,
        "evidence_review_ready": evidence_review_ready,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# 运行时读取（只读）
# ---------------------------------------------------------------------------

def _load_matches() -> list[dict]:
    from utils.daily_loader import get_all_matches

    return get_all_matches()


def _load_prediction_snapshots() -> list[dict]:
    from utils import prediction_snapshots

    return _as_list(prediction_snapshots._load_store().get("snapshots"))


def _load_odds_snapshots() -> list[dict]:
    from utils import odds_snapshots

    return _as_list(odds_snapshots._load_store().get("snapshots"))


def _load_settlements() -> list[dict]:
    from utils import settlements

    return _as_list(settlements._load_store().get("settlements"))


def _load_evaluation_rows() -> list[dict]:
    from utils.evaluation_rows import get_all_evaluation_rows

    return _as_list(get_all_evaluation_rows())


def get_evidence_status(*, with_diagnostics: bool = True) -> dict:
    """
    读取权威运行时存储并返回证据状态（只读，绝不写入任何存储）。
    """
    return build_evidence_status(
        matches=_load_matches(),
        snapshots=_load_prediction_snapshots(),
        odds_snapshots=_load_odds_snapshots(),
        settlements=_load_settlements(),
        evaluation_rows=_load_evaluation_rows(),
        with_diagnostics=with_diagnostics,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    """`python -m utils.evidence_status`：打印事实性运行状态 JSON。"""
    status = get_evidence_status()
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
