"""Workstream C：历史生命周期呈现回归。

证据链用**真实 refresh** 在隔离目录生成（快照→结算→评估），
「遗留 live 状态」通过直接改写隔离库文件模拟——模拟只发生在合成隔离目录，
绝不触碰真实 data/。全部分类只读：不重建历史、不改写存储。
"""
from __future__ import annotations

from copy import deepcopy
import json
import os

import pytest

from utils import daily_loader, scraper
from utils.daily_loader import override_beijing_now
from utils.history_evidence import build_history_view_data
from test_basketball_operational_acceptance import write_stores, store_hashes
from test_multisport_refresh_integration import (
    _finished_versions,
    _mock_sources,
    _upcoming_sources,
)
from test_nba_schedule_completeness import NOW

FOOTBALL_ID = "500j-2030-05-15-3001"


def _build_valid_chain(isolated_data_dir, monkeypatch) -> dict:
    """隔离目录：真实 refresh 两轮生成完整不可变证据链，返回链字段。"""
    write_stores(isolated_data_dir, [])
    live_rows, xml_rows = _upcoming_sources()
    _mock_sources(monkeypatch, live_rows, xml_rows)
    with override_beijing_now(NOW):
        scraper.refresh(verbose=False)
    finished = _finished_versions()
    finished.append(deepcopy(live_rows[2]))
    _mock_sources(monkeypatch, finished, xml_rows)
    with override_beijing_now(NOW):
        scraper.refresh(verbose=False)

    snapshots = json.load(open(os.path.join(str(isolated_data_dir), "prediction_snapshots.json"), encoding="utf-8"))
    settlements = json.load(open(os.path.join(str(isolated_data_dir), "settlements.json"), encoding="utf-8"))
    snap_id = next(s["snapshot_id"] for s in snapshots["snapshots"].values() if s["match_id"] == FOOTBALL_ID)
    set_id = next(s["settlement_id"] for s in settlements["settlements"].values() if s["match_id"] == FOOTBALL_ID)
    return {"snapshot_id": snap_id, "settlement_id": set_id}


def _rewrite_match(directory, match_id, mutate):
    """直接改写隔离库中的单场比赛记录（模拟遗留数据；只用于隔离测试）。"""
    path = os.path.join(str(directory), "daily_matches.json")
    data = json.load(open(path, encoding="utf-8"))
    for m in data["matches"]:
        if m["id"] == match_id:
            mutate(m)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, sort_keys=True)


def _view():
    return build_history_view_data(daily_loader.get_all_matches())


def test_live_with_valid_terminal_evidence_presented_with_warning(isolated_data_dir, monkeypatch):
    """LIVE_WITH_VALID_TERMINAL_EVIDENCE：合同允许时保持 verified，但必须带状态不一致警告。"""
    chain = _build_valid_chain(isolated_data_dir, monkeypatch)
    _rewrite_match(isolated_data_dir, FOOTBALL_ID, lambda m: m.update(status="live"))
    hashes = store_hashes(isolated_data_dir)

    data = _view()
    assert store_hashes(isolated_data_dir) == hashes  # STORED_STATUS_UNCHANGED（只读）

    row = next(m for m in data["matches"] if m["id"] == FOOTBALL_ID)
    assert row["lifecycle_presentation"] == "LIVE_WITH_TERMINAL_RESULT_EVIDENCE"
    assert row["state_inconsistency_warning"] is True
    # 评估合同完整通过 → 允许 verified 分类（不是静默降级，也不是伪造）
    assert row["comparison"]["evidence_status"] == "verified"
    assert row["comparison"]["is_verified"] is True
    assert row["comparison"]["model_version"] == "football-coldstart-1"


def test_live_without_final_result_is_excluded(isolated_data_dir, monkeypatch):
    """LIVE_WITHOUT_FINAL_RESULT：无终场比分不进入历史呈现。"""
    chain = _build_valid_chain(isolated_data_dir, monkeypatch)
    bb_upcoming_id = "500j-2030-05-16-4306"
    _rewrite_match(isolated_data_dir, bb_upcoming_id, lambda m: m.update(status="live", score=None))

    data = _view()
    assert all(m["id"] != bb_upcoming_id for m in data["matches"])


def test_live_with_settlement_but_missing_snapshot_reported_honestly(isolated_data_dir, monkeypatch):
    """LIVE_WITH_SETTLEMENT_BUT_MISSING_SNAPSHOT：结算存在但快照缺失 → 如实区分，不误报。"""
    _build_valid_chain(isolated_data_dir, monkeypatch)
    _rewrite_match(isolated_data_dir, FOOTBALL_ID, lambda m: m.update(status="live"))
    # 删除快照库与评估库中的对应记录（隔离目录内模拟孤儿结算）
    snap_path = os.path.join(str(isolated_data_dir), "prediction_snapshots.json")
    snaps = json.load(open(snap_path, encoding="utf-8"))
    snaps["snapshots"] = {
        sid: s for sid, s in snaps["snapshots"].items() if s["match_id"] != FOOTBALL_ID
    }
    with open(snap_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snaps, fh, ensure_ascii=False, sort_keys=True)
    eval_path = os.path.join(str(isolated_data_dir), "evaluation_rows.json")
    evals = json.load(open(eval_path, encoding="utf-8"))
    evals["rows"] = {
        eid: r for eid, r in evals["rows"].items() if r["match_id"] != FOOTBALL_ID
    }
    with open(eval_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(evals, fh, ensure_ascii=False, sort_keys=True)

    data = _view()
    row = next(m for m in data["matches"] if m["id"] == FOOTBALL_ID)
    assert row["comparison"]["evidence_status"] == "no_evidence"
    assert row["comparison"]["no_evidence_reason"] == "SETTLEMENT_WITHOUT_SNAPSHOT"


def test_live_with_conflicting_final_score_marked_as_conflict(isolated_data_dir, monkeypatch):
    """LIVE_WITH_CONFLICTING_FINAL_SCORE：结算比分与落盘比分冲突 → HISTORICAL_EVIDENCE_CONFLICT。"""
    _build_valid_chain(isolated_data_dir, monkeypatch)
    _rewrite_match(
        isolated_data_dir,
        FOOTBALL_ID,
        lambda m: m.update(status="live", score={"ft": [9, 9]}),
    )

    data = _view()
    row = next(m for m in data["matches"] if m["id"] == FOOTBALL_ID)
    assert row["lifecycle_presentation"] == "HISTORICAL_EVIDENCE_CONFLICT"
    assert row["state_inconsistency_warning"] is True
    assert row["comparison"]["is_verified"] is False  # 冲突绝不计入命中


def test_historical_evaluation_present_keeps_normal_path(isolated_data_dir, monkeypatch):
    """HISTORICAL_EVALUATION_PRESENT：正常完赛记录的既有行为不变。"""
    _build_valid_chain(isolated_data_dir, monkeypatch)
    data = _view()
    row = next(m for m in data["matches"] if m["id"] == FOOTBALL_ID)
    assert row["lifecycle_presentation"] == "SOURCE_CONFIRMED_FINISHED"
    assert row["state_inconsistency_warning"] is False
    assert row["comparison"]["evidence_status"] == "verified"


def test_no_historical_reconstruction_for_evidenceless_records(isolated_data_dir, monkeypatch):
    """NO_HISTORICAL_RECONSTRUCTION：无证据记录不得被当前模型重算。

    4307 是未知开赛时间的篮球赛：整条链路从未为它生成快照/结算/评估；
    给它一个「终场比分」并置为 live 后，只能以 FINAL_RESULT_UNVERIFIED 呈现。
    """
    _build_valid_chain(isolated_data_dir, monkeypatch)
    bb_id = "500j-2030-05-16-4307"
    _rewrite_match(
        isolated_data_dir,
        bb_id,
        lambda m: m.update(status="live", score={"ft": [80, 77]}),
    )
    data = _view()
    row = next(m for m in data["matches"] if m["id"] == bb_id)
    assert row["lifecycle_presentation"] == "FINAL_RESULT_UNVERIFIED"
    assert row["state_inconsistency_warning"] is True
    assert row["comparison"]["evidence_status"] == "no_evidence"
    assert row["comparison"]["no_evidence_reason"] == "NO_SNAPSHOT_EVIDENCE"
    # 无重建：不携带任何当前模型重算的预测字段
    assert row["comparison"]["model_name"] == ""
    assert row["comparison"]["predicted_outcome"] == ""
    assert row["comparison"]["predicted_label"] == ""


def test_protected_stores_byte_identical_through_view_build(isolated_data_dir, monkeypatch):
    """PROTECTED_STORES_BYTE_IDENTICAL：构建视图前后七库逐字节一致。"""
    _build_valid_chain(isolated_data_dir, monkeypatch)
    _rewrite_match(isolated_data_dir, FOOTBALL_ID, lambda m: m.update(status="live"))
    before = store_hashes(isolated_data_dir)
    _view()
    assert store_hashes(isolated_data_dir) == before
