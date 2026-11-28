# -*- coding: utf-8 -*-
"""跨源重复候选的状态暴露：候选级证据可见、只读、绝不合并。"""
import json

import app as server
from test_basketball_operational_acceptance import store_hashes, write_stores
from utils.basketball_identity_evidence import scan_cross_source_duplicate_candidates


def _xml_tracked(mid="500j-2026-11-29-6301", date="2026-11-29"):
    return {
        "id": mid, "sport": "basketball", "league": "美国职业篮球联盟",
        "round": "6301", "date": date, "time": "00:00",
        "kickoff_time_known": False, "status": "upcoming",
        "home": "华盛顿奇才", "away": "奥兰多魔术", "score": None,
        "odds": {"home_win": 1.7, "away_win": 1.7, "matchnum": "6301"},
        "jczq_no": "6301", "market_tracked": True,
    }


def _live_tracked(mid="500lq-2026-11-29-周六301", date="2026-11-29"):
    return {
        "id": mid, "sport": "basketball", "league": "NBA",
        "round": "", "date": date, "time": "06:00",
        "kickoff_time_known": True, "status": "upcoming",
        "home": "奥兰多魔术", "away": "华盛顿奇才", "score": None,
        "odds": {"home_win": 1.65, "away_win": 1.75},
        "jczq_no": "周六301", "market_tracked": True,
    }


def test_scanner_reports_pair_as_candidate_never_approval():
    result = scan_cross_source_duplicate_candidates([_xml_tracked(), _live_tracked()])
    assert result["count"] == 1 and result["truncated"] is False
    candidate = result["candidates"][0]
    assert candidate["authoritative"] is False
    assert candidate["is_approval"] is False
    assert candidate["note"] == "CANDIDATE_EVIDENCE_ONLY_NOT_MERGED"


def test_scanner_ignores_unrelated_and_untracked():
    unrelated_same_suffix = _xml_tracked(mid="500j-2026-10-06-1001", date="2026-10-06")
    other = _live_tracked(mid="500lq-2026-10-06-周一001", date="2026-10-06")
    other["jczq_no"] = "周一001"
    assert scan_cross_source_duplicate_candidates([unrelated_same_suffix, other])["count"] == 0
    untracked = _xml_tracked()
    untracked["market_tracked"] = False
    assert scan_cross_source_duplicate_candidates([untracked, _live_tracked()])["count"] == 0
    assert scan_cross_source_duplicate_candidates([_live_tracked()])["count"] == 0


def test_status_exposes_candidates_readonly(isolated_data_dir):
    write_stores(isolated_data_dir, [_xml_tracked(), _live_tracked()])
    before = store_hashes(isolated_data_dir)
    api = server.app.test_client()
    response = api.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    section = payload["source_health"]["cross_source_duplicate_candidates"]
    assert section["count"] == 1
    assert section["candidates"][0]["authoritative"] is False
    assert section["candidates"][0]["is_approval"] is False
    # 读取端点零写入：七库逐字节不变
    assert store_hashes(isolated_data_dir) == before
