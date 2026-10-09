"""赛事生命周期与终态保护单元测试。"""
from __future__ import annotations

import pytest

from utils.match_lifecycle import (
    MatchLifecycleConflict,
    is_finalized_match,
    resolve_match_update,
    valid_full_time_score,
)


# 1. 终态赛事的不可变保护与冲突检测 - 相同比分幂等
def test_lifecycle_finalized_match_idempotent():
    existing = {"id": "m1", "status": "finished", "score": {"ft": [2, 1]}}
    incoming = {"id": "m1", "status": "finished", "score": {"ft": [2, 1]}}
    result = resolve_match_update(existing, incoming)
    assert result == existing


# 2. 终态赛事的不可变保护与冲突检测 - 不同比分引发冲突
def test_lifecycle_finalized_match_conflict():
    existing = {"id": "m1", "status": "finished", "score": {"ft": [2, 1]}}
    incoming = {"id": "m1", "status": "finished", "score": {"ft": [1, 1]}}
    with pytest.raises(MatchLifecycleConflict):
        resolve_match_update(existing, incoming)


# 3. 终态赛事 - 无效状态不影响现有终态
def test_lifecycle_finalized_match_ignores_stale_update():
    existing = {"id": "m1", "status": "finished", "score": {"ft": [2, 1]}}
    incoming = {"id": "m1", "status": "live", "score": {"ft": [2, 0]}}
    result = resolve_match_update(existing, incoming)
    assert result == existing


# 4. 有效的全场比分校验 - 正常整数
def test_valid_full_time_score_ints():
    assert valid_full_time_score({"ft": [2, 1]}) is True
    assert valid_full_time_score({"ft": [0, 0]}) is True
    assert valid_full_time_score({"ft": [105, 99]}) is True


# 5. 有效的全场比分校验 - 严格拒绝 float, bool, 负数, 字符串
def test_valid_full_time_score_strict_types():
    assert valid_full_time_score({"ft": [2.0, 1.0]}) is False
    assert valid_full_time_score({"ft": [True, False]}) is False
    assert valid_full_time_score({"ft": ["2", "1"]}) is False
    assert valid_full_time_score({"ft": [-1, 1]}) is False
    assert valid_full_time_score({"ft": [1, None]}) is False


# 6. 未完赛赛事 - 严格有效的 incoming 比分无条件替换畸形/缺失的 existing 比分
def test_resolve_match_update_replaces_malformed_score():
    existing = {"id": "m1", "status": "live", "score": {"ft": [2.0, 1.0]}}
    incoming = {"id": "m1", "status": "live", "score": {"ft": [2, 1]}}
    result = resolve_match_update(existing, incoming)
    # Python 的 == 无法区分 2.0 和 2，但是我们必须确保它的 type 被修正
    assert type(result["score"]["ft"][0]) is int
    assert result["score"]["ft"] == [2, 1]


# 7. is_finalized_match - 有效比分且 status=finished
def test_is_finalized_match_true():
    assert is_finalized_match({"status": "finished", "score": {"ft": [2, 1]}}) is True
    assert is_finalized_match({"status": "finished", "score": {"ft": [0, 0]}}) is True


# 8. is_finalized_match - 状态不对或比分畸形
def test_is_finalized_match_false():
    assert is_finalized_match({"status": "live", "score": {"ft": [2, 1]}}) is False
    assert is_finalized_match({"status": "finished", "score": {"ft": [2.0, 1.0]}}) is False
    assert is_finalized_match({"status": "finished", "score": None}) is False


# 9. 状态更新 - 正常非终态转换
def test_resolve_match_update_status_transitions():
    existing = {"id": "m1", "status": "upcoming", "score": None}
    incoming = {"id": "m1", "status": "live", "score": {"ft": [0, 0]}}
    result = resolve_match_update(existing, incoming)
    assert result["status"] == "live"


# 10. 排期元数据升级
def test_resolve_match_update_metadata_upgrade():
    existing = {"id": "m1", "status": "upcoming", "jczq_no": None}
    incoming = {"id": "m1", "status": "upcoming", "jczq_no": "101"}
    result = resolve_match_update(existing, incoming)
    assert result["jczq_no"] == "101"
