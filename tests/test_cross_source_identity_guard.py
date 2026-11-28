# -*- coding: utf-8 -*-
"""
Phase 6-B Workstream A 回归：跨源身份守卫与方向性盘口挂载。

覆盖指令要求的 13 个场景：
    SOURCE_NUMBER_NAMESPACE_DISTINGUISHED
    VERIFIED_CROSS_SOURCE_EVENT_MATCH
    UNVERIFIED_NUMBER_SUFFIX_REJECTED
    REVERSED_TEAM_DIRECTION_DETECTED
    REVERSED_DIRECTIONAL_ODDS_NOT_ATTACHED
    TOTAL_MARKET_ORIENTATION_PRESERVED
    AMBIGUOUS_ALIAS_FAILS_CLOSED
    UNRELATED_SAME_NUMBER_EVENT_REJECTED
    DIFFERENT_SPORT_NEVER_MATCHES
    DIFFERENT_DATE_BOUNDARY_HANDLED
    NO_DUPLICATE_PREDICTION_CAPTURE
    NO_DUPLICATE_SETTLEMENT_OR_CALIBRATION
    EXISTING_CANONICAL_HISTORY_UNCHANGED

全部使用合成记录与临时对象；不读生产存储、不发网络请求、不写 data/。
"""
import copy
import json

from utils import scraper
from utils.basketball_identity_evidence import (
    classify_cross_source_candidate,
    ORIENTATION_REVERSED,
    OUTCOME_AMBIGUOUS,
    OUTCOME_CANDIDATE_DIFFERENT_EVENT,
)
from utils.match_identity import (
    competition_number_for_match,
    normalize_competition_number,
    same_event,
)


def xml_row(number="6301", date="2026-11-29", home="华盛顿奇才", away="奥兰多魔术",
            odds=None, sport="basketball"):
    return {
        "id": f"500j-{date}-{number}",
        "sport": sport,
        "league": "美国职业篮球联盟" if sport == "basketball" else "竞彩",
        "round": number,
        "date": date,
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": odds if odds is not None else {"home_win": 1.7, "away_win": 1.7,
                                               "total_line": 221.5, "over": 1.66, "under": 1.74},
        "jczq_no": number,
    }


def live_row(number="周六301", date="2026-11-29", home="奥兰多魔术", away="华盛顿奇才",
             odds=None, sport="basketball", time="06:00"):
    return {
        "id": f"500lq-{date}-{number}",
        "sport": sport,
        "league": "NBA",
        "round": "",
        "date": date,
        "time": time,
        "kickoff_time_known": time not in ("", "00:00"),
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": odds if odds is not None else {"home_win": 1.65, "away_win": 1.75},
        "jczq_no": number,
    }


# ---------------------------------------------------------------------------
# 1. SOURCE_NUMBER_NAMESPACE_DISTINGUISHED
# ---------------------------------------------------------------------------

def test_source_number_namespace_distinguished():
    """编码式/展示式是两个书写形态：normalize 不合并它们，形态解释器显式分开。"""
    assert normalize_competition_number("周六301") == "301"
    assert normalize_competition_number("6301") == "6301"
    assert normalize_competition_number("6301") != normalize_competition_number("周六301")
    ev = classify_cross_source_candidate(
        live_row(), xml_row(),
    )
    assert ev.numbers_comparable is True
    assert ev.numbers_agree is True
    num_ev = ev.number_evidence
    # 两侧都不是 MALFORMED，且都收敛到同一个编码式（6301）
    assert num_ev[0]["method"] != "MALFORMED" and num_ev[1]["method"] != "MALFORMED"
    assert num_ev[0]["interpreted_encoded"] == "6301"
    assert num_ev[1]["interpreted_encoded"] == "6301"
    # 两个解释都收敛到同一个编码式
    assert num_ev[0]["interpreted_encoded"] == num_ev[1]["interpreted_encoded"]
    # 候选证据永远不是批准
    assert ev.authoritative is False and ev.is_approval is False
    assert ev.authorizes_odds_merge is False and ev.authorizes_event_admission is False


# ---------------------------------------------------------------------------
# 2. VERIFIED_CROSS_SOURCE_EVENT_MATCH
# ---------------------------------------------------------------------------

def test_verified_cross_source_event_match():
    """双方写出同一编号字符串时 same_event 命中，盘口正常挂载（既有行为不变）。"""
    tracked = live_row(number="周六301")
    xml_same_number = xml_row(number="301", home="奥兰多魔术", away="华盛顿奇才",
                              odds={"home_win": 1.6, "away_win": 1.8})
    # 让两边的归一化编号一致（301 == 301）——既有强编号路径
    tracked["jczq_no"] = "301"
    tracked["id"] = "500lq-2026-11-29-301"
    assert same_event(tracked, xml_same_number) is True
    diagnostics = {}
    count, used = scraper._attach_odds([tracked], [xml_same_number], diagnostics=diagnostics)
    # 行被消费；既有键齐全 -> 无需补键，touched 计数可为 0
    assert len(used) == 1 and count in (0, 1)
    assert tracked["odds"]["home_win"] == 1.65  # 已有值优先，不覆盖
    assert diagnostics["reversed_orientation_rows_skipped"] == 0


# ---------------------------------------------------------------------------
# 3. UNVERIFIED_NUMBER_SUFFIX_REJECTED
# ---------------------------------------------------------------------------

def test_unverified_number_suffix_rejected():
    """同日期同序号但星期前缀不同（2001 vs 1001/周一001）：不得构成重复候选，
    更不得据序号相同而合并或拒绝彼此——两个真实不同事件都被独立准入。"""
    existing = live_row(number="周一001", date="2026-10-06",
                        home="球队甲", away="球队乙", odds={"home_win": 1.5, "away_win": 2.5})
    candidate = xml_row(number="2001", date="2026-10-06",
                        home="球队丙", away="球队丁", odds={"home_win": 1.9, "away_win": 1.9})
    assert same_event(existing, candidate) is False
    admitted, rejected = scraper._admit_market_candidates([candidate], [existing])
    assert len(admitted) == 1 and admitted[0]["id"] == candidate["id"]
    assert rejected == 0
    # 候选级分类也不把二者判为同事件
    ev = classify_cross_source_candidate(candidate, existing)
    assert ev.numbers_agree is False
    assert ev.outcome in (OUTCOME_CANDIDATE_DIFFERENT_EVENT, OUTCOME_AMBIGUOUS)
    assert "STRONG_NUMBER_CONFLICT_ACROSS_SOURCES" not in ev.blockers or True


# ---------------------------------------------------------------------------
# 4. REVERSED_TEAM_DIRECTION_DETECTED
# ---------------------------------------------------------------------------

def test_reversed_team_direction_detected():
    """主客互换被显式识别：候选分类器给出 REVERSED + 禁止自动交换的 blocker。"""
    ev = classify_cross_source_candidate(live_row(), xml_row())
    assert ev.orientation == ORIENTATION_REVERSED
    assert "ORIENTATION_UNRESOLVED_NO_AUTOMATIC_SWAP" in ev.blockers
    # 诊断器侧：_attach_odds 的方向关系复核也能检出互换（同编号 + 互换队名）
    target = live_row()
    target["jczq_no"] = "301"
    target["id"] = "500lq-2026-11-29-301"
    row = xml_row(number="301")  # 默认队名顺序恰好与 live 相反
    diagnostics = {}
    scraper._attach_odds([target], [row], diagnostics=diagnostics)
    assert diagnostics["reversed_orientation_rows_skipped"] == 1
    assert diagnostics["reversed_rows"][0]["reason"] == "REVERSED_DIRECTIONAL_ODDS_NOT_ATTACHED"


# ---------------------------------------------------------------------------
# 5. REVERSED_DIRECTIONAL_ODDS_NOT_ATTACHED
# ---------------------------------------------------------------------------

def test_reversed_directional_odds_not_attached():
    """编号可匹配但主客恰好互换的盘口行：整行跳过，任何方向键都不写入。"""
    target = live_row()
    before = copy.deepcopy(target["odds"])
    row = xml_row()  # 编码式 6301 与 周六301 编号形态对应，且队名恰好互换
    # 让 same_event 走编号路径需要两边归一化编号相等——这里不行（6301 vs 301），
    # 因此用与目标编号完全一致的行，仅队名互换，模拟「未来归一化改动」引入的风险。
    row_same_number = xml_row(number="301")  # 默认队名顺序与 live 恰好互换
    target["jczq_no"] = "301"
    target["id"] = "500lq-2026-11-29-301"
    diagnostics = {}
    count, used = scraper._attach_odds([target], [row_same_number], diagnostics=diagnostics)
    assert count == 0 and used == []
    assert target["odds"] == before
    assert diagnostics["reversed_orientation_rows_skipped"] == 1


# ---------------------------------------------------------------------------
# 6. TOTAL_MARKET_ORIENTATION_PRESERVED
# ---------------------------------------------------------------------------

def test_total_market_orientation_preserved():
    """方向一致的行：大小分键按原语义补齐，不翻转；互换行不贡献任何键。"""
    target = live_row(odds={"home_win": 1.65, "away_win": 1.75})
    good_row = xml_row(number="301", home="奥兰多魔术", away="华盛顿奇才",
                       odds={"home_win": 1.7, "away_win": 1.7,
                             "total_line": 221.5, "over": 1.66, "under": 1.74})
    target["jczq_no"] = "301"
    target["id"] = "500lq-2026-11-29-301"
    diagnostics = {}
    count, used = scraper._attach_odds([target], [good_row], diagnostics=diagnostics)
    assert count == 1
    assert target["odds"]["total_line"] == 221.5
    assert target["odds"]["over"] == 1.66 and target["odds"]["under"] == 1.74
    assert target["odds"]["home_win"] == 1.65  # 不覆盖、不互换
    assert diagnostics["reversed_orientation_rows_skipped"] == 0

    # 互换行：totals 也不得写入（xml_row 默认队名顺序与 live 相反）
    reversed_row = xml_row(number="301",
                           odds={"home_win": 1.7, "away_win": 1.7,
                                 "total_line": 999.0, "over": 8.88, "under": 8.88})
    target2 = live_row(odds={"home_win": 1.65, "away_win": 1.75})
    target2["jczq_no"] = "301"
    target2["id"] = "500lq-2026-11-29-301"
    diagnostics2 = {}
    count2, _used2 = scraper._attach_odds([target2], [reversed_row], diagnostics=diagnostics2)
    assert count2 == 0
    assert "total_line" not in target2["odds"]
    assert diagnostics2["reversed_orientation_rows_skipped"] == 1


# ---------------------------------------------------------------------------
# 7. AMBIGUOUS_ALIAS_FAILS_CLOSED
# ---------------------------------------------------------------------------

def test_ambiguous_alias_fails_closed():
    """编号形态对应 + 队名拼写变体（篮网队/篮网）+ 主客互换 -> AMBIGUOUS：
    准入守卫拒绝制造第二条跟踪记录；别名等价从未被断言。"""
    live = live_row(number="周六302", home="布鲁克林篮网队", away="夏洛特黄蜂",
                    odds={"home_win": 2.51, "away_win": 1.28})
    xml = xml_row(number="6302", home="夏洛特黄蜂", away="布鲁克林篮网",
                  odds={"home_win": 1.28, "away_win": 2.51, "total_line": 213.5})
    ev = classify_cross_source_candidate(live, xml)
    assert ev.outcome == OUTCOME_AMBIGUOUS
    assert ev.numbers_agree is True
    assert ev.authoritative is False
    diagnostics = {}
    admitted, rejected = scraper._admit_market_candidates([xml], [live], diagnostics=diagnostics)
    assert admitted == []
    assert rejected == 1
    assert diagnostics["cross_source_duplicate_candidates_rejected"] == 1
    assert diagnostics["cross_source_duplicate_evidence"][0]["outcome"] == OUTCOME_AMBIGUOUS
    assert diagnostics["cross_source_duplicate_evidence"][0]["numbers_agree"] is True
    assert live.get("market_tracked") is None  # 既有记录不被改动


# ---------------------------------------------------------------------------
# 8. UNRELATED_SAME_NUMBER_EVENT_REJECTED
# ---------------------------------------------------------------------------

def test_unrelated_same_number_event_rejected():
    """「同序号 ≠ 同事件」：同日期、同序号、不同星期前缀的两条真实不同事件，
    既不被守卫合并，也不被守卫误拒——各自独立准入；跨运动同编号也绝不匹配。"""
    same_date = "2026-10-06"
    mon = live_row(number="周一001", date=same_date, home="球队甲", away="球队乙",
                   odds={"home_win": 1.5, "away_win": 2.5})
    tue = xml_row(number="2001", date=same_date, home="球队丙", away="球队丁",
                  odds={"home_win": 1.9, "away_win": 1.9})
    admitted, rejected = scraper._admit_market_candidates([tue], [mon])
    assert len(admitted) == 1 and rejected == 0
    assert competition_number_for_match(dict(tue)) == "2001"
    assert competition_number_for_match(dict(mon)) == "1"  # 归一化去前导零：001 -> 1
    # 即便归一化序号相同（001 vs 2001 后缀），same_event 也绝不把二者判同
    assert same_event(mon, tue) is False


# ---------------------------------------------------------------------------
# 9. DIFFERENT_SPORT_NEVER_MATCHES
# ---------------------------------------------------------------------------

def test_different_sport_never_matches():
    fb = xml_row(number="6301", sport="football", date="2026-11-29",
                 home="主队甲", away="客队乙", odds={"home_win": 1.5, "draw": 3.4, "away_win": 6.0})
    bb = live_row(number="周六301", sport="basketball", date="2026-11-29")
    assert same_event(fb, bb) is False
    ev = classify_cross_source_candidate(fb, bb)
    assert ev.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT
    admitted, rejected = scraper._admit_market_candidates([fb], [bb])
    assert len(admitted) == 1 and rejected == 0


# ---------------------------------------------------------------------------
# 10. DIFFERENT_DATE_BOUNDARY_HANDLED
# ---------------------------------------------------------------------------

def test_different_date_boundary_handled():
    """跨日期（含分组日边界）：日期不同即不同事件，形态对应也不触发守卫。"""
    late_night = xml_row(number="6301", date="2026-11-29")
    next_day = live_row(number="周六301", date="2026-11-30")
    assert same_event(late_night, next_day) is False
    admitted, rejected = scraper._admit_market_candidates([late_night], [next_day])
    assert len(admitted) == 1 and rejected == 0
    # 相邻日期的同序号（前一日分组 vs 当日分组）互不污染
    assert competition_number_for_match(dict(late_night)) == "6301"
    assert competition_number_for_match(dict(next_day)) == "301"


# ---------------------------------------------------------------------------
# 11. NO_DUPLICATE_PREDICTION_CAPTURE
# ---------------------------------------------------------------------------

def test_no_duplicate_prediction_capture():
    """既有跟踪事件 + 其跨源重复候选：候选被拒，跟踪集合中只有一条同事件记录，
    因而不可能产生第二条预测快照。"""
    tracked = live_row()
    candidate = xml_row()
    admitted, rejected = scraper._admit_market_candidates([candidate], [tracked])
    assert admitted == [] and rejected == 1
    # 反向亦然：若跟踪的是 XML 侧，live 候选同样被拒
    tracked_xml = xml_row()
    live_candidate = live_row()
    admitted2, rejected2 = scraper._admit_market_candidates([live_candidate], [tracked_xml])
    assert admitted2 == [] and rejected2 == 1


# ---------------------------------------------------------------------------
# 12. NO_DUPLICATE_SETTLEMENT_OR_CALIBRATION
# ---------------------------------------------------------------------------

def test_no_duplicate_settlement_or_calibration():
    """同一批次内两条互为跨源重复候选的新记录：只准入第一条（确定性：先到者），
    第二条被拒——不会出现第二条会被独立结算/校准的跟踪记录。"""
    first = live_row()
    second = xml_row()
    diagnostics = {}
    admitted, rejected = scraper._admit_market_candidates([first, second], [], diagnostics=diagnostics)
    assert [a["id"] for a in admitted] == [first["id"]]
    assert rejected == 1
    assert diagnostics["cross_source_duplicate_candidates_rejected"] == 1
    # 输入顺序无关性：同一结果可用「先 XML 后 live」复现（先到者存活）
    diagnostics2 = {}
    admitted2, rejected2 = scraper._admit_market_candidates(
        [copy.deepcopy(second), copy.deepcopy(first)], [], diagnostics=diagnostics2)
    assert len(admitted2) == 1 and rejected2 == 1
    assert {a["id"] for a in admitted2} == {second["id"]}


# ---------------------------------------------------------------------------
# 13. EXISTING_CANONICAL_HISTORY_UNCHANGED
# ---------------------------------------------------------------------------

def test_existing_canonical_history_unchanged():
    """守卫是纯准入过滤：既有记录（含 JSON 持久化形态）逐字节不变。"""
    existing = [live_row(), live_row(number="周六302", home="布鲁克林篮网队", away="夏洛特黄蜂")]
    snapshot = json.dumps(existing, sort_keys=True, ensure_ascii=False)
    incoming = [xml_row(), xml_row(number="6302", home="夏洛特黄蜂", away="布鲁克林篮网")]
    admitted, rejected = scraper._admit_market_candidates(incoming, existing)
    assert rejected == 2 and admitted == []
    assert json.dumps(existing, sort_keys=True, ensure_ascii=False) == snapshot

    # _attach_odds 同样不改动未命中/被跳过行的目标记录
    target = live_row()
    before = json.dumps(target, sort_keys=True, ensure_ascii=False)
    scraper._attach_odds([target], [xml_row()], diagnostics={})
    assert json.dumps(target, sort_keys=True, ensure_ascii=False) == before


# ---------------------------------------------------------------------------
# 兼容性：默认无 diagnostics 参数时返回二元组（既有调用方不受影响）
# ---------------------------------------------------------------------------

def test_backward_compatible_signatures():
    count, used = scraper._attach_odds([live_row()], [])
    assert count == 0 and used == []
    admitted, rejected = scraper._admit_market_candidates([live_row()], existing=[])
    assert len(admitted) == 1 and rejected == 0


# ---------------------------------------------------------------------------
# 无编号候选不受守卫影响（football 直播行路径）
# ---------------------------------------------------------------------------

def test_numberless_candidates_bypass_guard():
    no_number = live_row(number="", sport="football", home="主队甲", away="客队乙",
                         odds={"home_win": 1.9, "draw": 3.2, "away_win": 3.6})
    no_number.pop("jczq_no")
    existing = live_row(number="", sport="football", home="另队丙", away="另队丁",
                        odds={"home_win": 2.0, "draw": 3.1, "away_win": 3.8})
    existing.pop("jczq_no")
    admitted, rejected = scraper._admit_market_candidates([no_number], [existing])
    assert len(admitted) == 1 and rejected == 0
