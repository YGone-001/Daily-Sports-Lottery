"""
赛事生命周期与终态保护模块
==========================
负责：
1. 完赛终场比分合法性校验
2. 赛事完赛终态判定
3. 终态赛事的不可变保护与冲突检测
4. 未完赛赛事的可变状态安全合并
"""
from __future__ import annotations

from typing import Any

from utils.daily_loader import get_beijing_now
from utils.match_identity import is_kickoff_time_known


class MatchLifecycleConflict(Exception):
    """完赛赛事结果或状态冲突异常。"""

    def __init__(
        self,
        match_id: str,
        existing_score: Any,
        incoming_score: Any,
        reason: str = "conflicting_final_score",
    ) -> None:
        super().__init__(
            f"Match {match_id} conflict ({reason}): existing={existing_score}, incoming={incoming_score}"
        )
        self.match_id = str(match_id or "")
        self.existing_score = existing_score
        self.incoming_score = incoming_score
        self.reason = reason


def valid_full_time_score(score: Any) -> bool:
    """
    校验是否为有效的全场终场比分。

    规则：
    - score 为 dict；
    - score["ft"] 为 list 或 tuple，长度 >= 2；
    - ft[0] 与 ft[1] 为整数（必须是精确的 int，排除 bool、float、str 等）；
    - 双方进球/得分均 >= 0。
    """
    if not isinstance(score, dict):
        return False
    ft = score.get("ft")
    if not isinstance(ft, (list, tuple)):
        return False
    if len(ft) < 2:
        return False
    h, a = ft[0], ft[1]
    # Python 中 bool 是 int 的子类，必须严格排除 bool 类型
    if type(h) is not int or type(a) is not int:
        return False
    if h < 0 or a < 0:
        return False
    return True


def is_finalized_match(match: dict | None) -> bool:
    """
    判定比赛是否已处于合法的完赛终态。

    当且仅当 match["status"] == "finished" 且具备合法的全场终场比分时为 True。
    缺失比分或比分畸形的完赛记录不属于终态，允许后续数据补全修复。
    """
    if not isinstance(match, dict):
        return False
    if match.get("status") != "finished":
        return False
    return valid_full_time_score(match.get("score"))


def resolve_match_update(existing: dict, incoming: dict) -> dict:
    """
    决策并将 incoming 数据合并至 existing 记录：

    1. 若 existing 已处于合法完赛终态：
       - 若 incoming 同为完赛且比分一致：幂等保留 existing 不变；
       - 若 incoming 同为完赛但比分不同：抛出 MatchLifecycleConflict；
       - 若 incoming 为未完赛或缺失比分（如 stale live/upcoming/placeholder）：直接忽略，保持 existing 终态。

    2. 若 existing 尚未处于完赛终态：
       - 允许正常更新状态、开赛时间、比分、盘口与排期元数据。
    """
    if is_finalized_match(existing):
        # 终态保护生效
        if incoming.get("status") == "finished" and valid_full_time_score(incoming.get("score")):
            ex_ft = tuple(existing["score"]["ft"][:2])
            inc_ft = tuple(incoming["score"]["ft"][:2])
            if ex_ft != inc_ft:
                raise MatchLifecycleConflict(
                    match_id=str(existing.get("id") or ""),
                    existing_score=list(ex_ft),
                    incoming_score=list(inc_ft),
                    reason="conflicting_final_score",
                )
            # 比分一致：幂等重报，保持 existing 原样（不自动添加辅助字段）
            return existing

        # incoming 为非完赛或无有效比分时，忽略回退，保持 existing
        return existing

    # existing 未处于终态：执行正常状态流转与合并
    # 1. 开赛时间更新
    if is_kickoff_time_known(incoming):
        inc_time = incoming.get("time") or "00:00"
        if inc_time != existing.get("time") or not existing.get("kickoff_time_known"):
            existing["time"] = inc_time
            existing["kickoff_time_known"] = True

    # 2. 比分更新
    if incoming.get("score") and incoming["score"] != existing.get("score"):
        existing["score"] = incoming["score"]

    # 3. 状态更新（未终态前允许权威源正常修正，如 live -> upcoming 延期；占位符 upcoming 不降级）
    inc_status = incoming.get("status")
    if inc_status and inc_status != existing.get("status"):
        if not (
            existing.get("status") in ("live", "finished")
            and not is_kickoff_time_known(incoming)
            and inc_status == "upcoming"
        ):
            existing["status"] = inc_status

    # 4. 盘口更新
    if incoming.get("odds"):
        if incoming["odds"] != existing.get("odds"):
            existing["odds"] = incoming["odds"]
            existing["odds_updated_at"] = (
                incoming.get("odds_updated_at") or get_beijing_now().isoformat()
            )

    # 5. 排期元数据升级
    if is_kickoff_time_known(incoming):
        if incoming.get("league") and incoming["league"] != "竞彩":
            existing["league"] = incoming["league"]
        if incoming.get("home") and incoming["home"] != existing.get("home"):
            existing["home"] = incoming["home"]
        if incoming.get("away") and incoming["away"] != existing.get("away"):
            existing["away"] = incoming["away"]

    if incoming.get("jczq_no") and not existing.get("jczq_no"):
        existing["jczq_no"] = incoming["jczq_no"]
    if incoming.get("round") and not existing.get("round"):
        existing["round"] = incoming["round"]
    if incoming.get("home_rank") is not None and existing.get("home_rank") is None:
        existing["home_rank"] = incoming["home_rank"]
    if incoming.get("away_rank") is not None and existing.get("away_rank") is None:
        existing["away_rank"] = incoming["away_rank"]

    existing["market_tracked"] = True
    return existing
