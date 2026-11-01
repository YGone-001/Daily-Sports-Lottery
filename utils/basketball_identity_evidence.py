"""
NBA 跨源身份差异的**候选级证据**模块
====================================

用途
----
真实数据中同一场 NBA 比赛同时出现在两个记录命名空间里（竞彩 XML ``500j-`` 与
实时源 ``500lq-``），两者的竞彩编号书写形态不同、主客队顺序不同、联赛名用词不同。
本模块把这些差异整理成**可复现的证据**，供人工复核使用。

本模块**不是**身份权威
----------------------
- 运行时权威判定依旧是 ``utils.match_identity.same_event()``；本模块不改变它、
  不被它调用，也不产生任何可执行的相等性结论。
- 任何返回值都携带 ``authoritative = False``；不存在"批准/合并/写入"字段，
  调用方也无法通过任何参数把候选结论升级为批准。
- 不写文件、不联网、不导入 ``scraper``，没有任何写 API。

两类产物
--------
1. :func:`interpret_lottery_number_evidence`
   解释单条竞彩编号的书写形态（展示式 ``周六308`` ↔ 编码式 ``6308``）。
   转换只是**候选解释**：保留原始字符串、给出方法/证据/矛盾/置信分类。
2. :func:`classify_cross_source_candidate`
   对一对跨源记录给出候选级结论（``CANDIDATE_SAME_EVENT`` 等）。

依赖缺失即 fail closed：缺少运动、日期、队名或编号矛盾时返回
``INSUFFICIENT_EVIDENCE`` / ``CONTRADICTORY_EVIDENCE``，绝不补全证据。
"""
from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass
from typing import Any, Mapping

from utils.source_identity import fingerprint

#: 证据载荷版本。任何影响结论**含义**的变化都必须提升该版本。
EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: 本模块的任何结论都不是权威判定，也不是 Canonical 归属决定。
AUTHORITATIVE = False

#: 中文星期标签 -> 星期序号（周一=1 … 周日=7）。
WEEKDAY_LABEL_TO_DIGIT: dict[str, int] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    # 「天」与「七」是部分地区/历史写法，按同一序号记录但会在证据里点名。
    "天": 7,
    "七": 7,
}

#: 星期序号 -> 规范中文标签。
WEEKDAY_DIGIT_TO_LABEL: dict[int, str] = {
    1: "一",
    2: "二",
    3: "三",
    4: "四",
    5: "五",
    6: "六",
    7: "日",
}

#: 展示式编号的星期前缀字符。
WEEKDAY_PREFIX_CHAR = "周"

METHOD_WEEKDAY_PREFIX = "WEEKDAY_PREFIX_DISPLAY_FORM"
METHOD_ENCODED_DIGIT = "WEEKDAY_DIGIT_ENCODED_FORM"
METHOD_PLAIN_DIGITS = "PLAIN_DIGITS"
METHOD_MALFORMED = "MALFORMED_INPUT"
METHODS: frozenset[str] = frozenset(
    {METHOD_WEEKDAY_PREFIX, METHOD_ENCODED_DIGIT, METHOD_PLAIN_DIGITS, METHOD_MALFORMED}
)

CONFIDENCE_SUPPORTED = "SUPPORTED_BY_OBSERVED_CORRESPONDENCE"
CONFIDENCE_UNVERIFIED = "UNVERIFIED_NO_CORRESPONDING_OBSERVATION"
CONFIDENCE_CONTRADICTED = "CONTRADICTED_BY_OBSERVED_PAIR"
CONFIDENCE_MALFORMED = "MALFORMED_INPUT"
CONFIDENCE_AMBIGUOUS = "AMBIGUOUS_INPUT"

OUTCOME_CANDIDATE_SAME_EVENT = "CANDIDATE_SAME_EVENT"
OUTCOME_CANDIDATE_DIFFERENT_EVENT = "CANDIDATE_DIFFERENT_EVENT"
OUTCOME_AMBIGUOUS = "AMBIGUOUS"
OUTCOME_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
OUTCOME_CONTRADICTORY_EVIDENCE = "CONTRADICTORY_EVIDENCE"
OUTCOMES: frozenset[str] = frozenset(
    {
        OUTCOME_CANDIDATE_SAME_EVENT,
        OUTCOME_CANDIDATE_DIFFERENT_EVENT,
        OUTCOME_AMBIGUOUS,
        OUTCOME_INSUFFICIENT_EVIDENCE,
        OUTCOME_CONTRADICTORY_EVIDENCE,
    }
)

ORIENTATION_SAME = "SAME_HOME_AWAY"
ORIENTATION_REVERSED = "REVERSED_HOME_AWAY"
ORIENTATION_UNKNOWN = "UNKNOWN"

#: 队名比较关系（复用既有审计语义：缩写**不**等于相等）。
RELATION_EXACT = "EXACT"
RELATION_NORMALIZED_EQUAL = "NORMALIZED_EQUAL"
RELATION_VARIANT = "NAME_VARIANT"
RELATION_DIFFERENT = "DIFFERENT"
RELATION_MISSING = "MISSING"


# ---------------------------------------------------------------------------
# 基础工具（纯函数）
# ---------------------------------------------------------------------------

def event_weekday_digit(event_date: Any) -> int | None:
    """``YYYY-MM-DD`` -> 1..7（周一=1）。无法解析时返回 ``None``。"""
    if not isinstance(event_date, str) or not event_date.strip():
        return None
    try:
        return _datetime.date.fromisoformat(event_date.strip()).weekday() + 1
    except ValueError:
        return None


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value) if ch.isdigit())


def _weekday_label(value: Any) -> str | None:
    """提取形如 ``周一`` 的星期标签字符（只认表格内的字符）。"""
    text = str(value)
    for index, ch in enumerate(text):
        if ch == WEEKDAY_PREFIX_CHAR and index + 1 < len(text):
            candidate = text[index + 1]
            if candidate in WEEKDAY_LABEL_TO_DIGIT:
                return candidate
    return None


def _normalise_team(value: Any) -> str:
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if not ch.isspace()).lower()


def team_relation(left: Any, right: Any) -> str:
    """
    比较两个队名，但**不**断言等价。

    - ``EXACT``：去空白后完全一致；
    - ``NORMALIZED_EQUAL``：仅大小写/符号差异；
    - ``NAME_VARIANT``：一方是另一方的前缀（截断/简称），**不构成**相等证据；
    - ``DIFFERENT``：两者无关；
    - ``MISSING``：任一方为空。
    """
    if left is None or right is None:
        return RELATION_MISSING
    raw_left, raw_right = str(left), str(right)
    norm_left, norm_right = _normalise_team(left), _normalise_team(right)
    if not norm_left or not norm_right:
        return RELATION_MISSING
    if norm_left == norm_right:
        return RELATION_EXACT if raw_left == raw_right else RELATION_NORMALIZED_EQUAL
    if len(norm_left) >= 2 and len(norm_right) >= 2 and (
        norm_left.startswith(norm_right) or norm_right.startswith(norm_left)
    ):
        return RELATION_VARIANT
    return RELATION_DIFFERENT


def _raw_number(record: Mapping[str, Any]) -> str | None:
    """
    取记录**原始**的竞彩编号字符串（不改写）。

    优先级与 ``utils.match_identity.competition_number_for_match`` 一致，但这里
    刻意返回原始形态，以便保留 provider 实际写下的字符串。
    """
    if not isinstance(record, Mapping):
        return None
    for key in ("jczq_no", "matchnum"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    odds = record.get("odds")
    if isinstance(odds, Mapping):
        value = odds.get("matchnum")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    match_id = str(record.get("id") or "")
    if match_id.startswith("500j-"):
        value = record.get("round")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# ---------------------------------------------------------------------------
# 编号书写形态解释
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LotteryNumberInterpretation:
    """
    单条竞彩编号的候选解释。

    ``converts`` 为真只表示"产生了一个候选映射"，不代表该映射可靠；
    可靠性由 ``confidence`` 表达，而 ``authoritative`` 永远为 ``False``。
    """

    raw: str | None
    source_namespace: str
    sport: str | None
    event_date: str | None
    digits: str
    weekday_label: str | None = None
    weekday_digit: int | None = None
    suffix: str | None = None
    interpreted_weekday_prefix: str | None = None
    interpreted_encoded: str | None = None
    method: str = METHOD_MALFORMED
    confidence: str = CONFIDENCE_MALFORMED
    converts: bool = False
    authoritative: bool = AUTHORITATIVE
    weekday_matches_event_date: bool | None = None
    supporting_evidence: tuple[str, ...] = ()
    contradictory_evidence: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "raw": self.raw,
            "source_namespace": self.source_namespace,
            "sport": self.sport,
            "event_date": self.event_date,
            "digits": self.digits,
            "weekday_label": self.weekday_label,
            "weekday_digit": self.weekday_digit,
            "suffix": self.suffix,
            "interpreted_weekday_prefix": self.interpreted_weekday_prefix,
            "interpreted_encoded": self.interpreted_encoded,
            "method": self.method,
            "confidence": self.confidence,
            "converts": self.converts,
            "authoritative": self.authoritative,
            "weekday_matches_event_date": self.weekday_matches_event_date,
            "supporting_evidence": list(self.supporting_evidence),
            "contradictory_evidence": list(self.contradictory_evidence),
            "notes": list(self.notes),
        }
        return payload

    def fingerprint(self) -> str:
        return fingerprint(self.as_dict())


def interpret_lottery_number_evidence(
    raw: Any,
    *,
    sport: str | None,
    event_date: str | None,
    source_namespace: str = "",
    counterpart_raw: str | None = None,
    counterpart_team_relation: str | None = None,
    provider_documents: bool = False,
) -> LotteryNumberInterpretation:
    """
    解释一条竞彩编号的书写形态（纯函数，永不改写原始值）。

    观测到的两种形态：
    - 展示式：``周六308`` —— 星期标签 + 三位序号；
    - 编码式：``6308``   —— 星期序号 + 三位序号。

    观测对应关系是**证据**，不是 provider 契约：即便 ``provider_documents`` 为真，
    本函数也只提升证据描述，不会把结论变成权威（``authoritative`` 恒为 ``False``）。
    """
    original = None if raw is None else str(raw)
    namespace = str(source_namespace or "")
    digits = _digits(original) if original is not None else ""
    label = _weekday_label(original) if original is not None else None
    supporting: list[str] = []
    contradictory: list[str] = []
    notes: list[str] = []

    date_digit = event_weekday_digit(event_date)
    weekday_matches: bool | None = None

    if provider_documents:
        notes.append(
            "调用方声明已取得 provider 编号文档；本模块仍不产出权威结论。"
        )

    def finish(**kwargs: Any) -> LotteryNumberInterpretation:
        return LotteryNumberInterpretation(
            raw=original,
            source_namespace=namespace,
            sport=sport,
            event_date=event_date,
            digits=digits,
            weekday_label=label,
            supporting_evidence=tuple(supporting),
            contradictory_evidence=tuple(contradictory),
            notes=tuple(notes),
            weekday_matches_event_date=weekday_matches,
            **kwargs,
        )

    if not original or not original.strip():
        notes.append("编号缺失：缺失即缺失，绝不合成。")
        return finish(method=METHOD_MALFORMED, confidence=CONFIDENCE_MALFORMED)

    if label is not None:
        # 展示式
        if len(digits) != 3:
            notes.append(f"展示式编号的数字位数不是 3（实际 {len(digits)}），不解释。")
            return finish(method=METHOD_MALFORMED, confidence=CONFIDENCE_MALFORMED)
        weekday_digit = WEEKDAY_LABEL_TO_DIGIT[label]
        suffix = digits
        encoded = f"{weekday_digit}{suffix}"
        weekday_matches = None if date_digit is None else weekday_digit == date_digit
        supporting.append(f"展示式编号：星期标签「{label}」= {weekday_digit}，序号 {suffix}")
        if weekday_matches is True:
            supporting.append(f"编号星期与事件日 {event_date} 的日历星期一致")
        elif weekday_matches is False:
            contradictory.append(
                f"编号星期（{label}={weekday_digit}）与事件日 {event_date} 的日历星期"
                f"（{WEEKDAY_DIGIT_TO_LABEL.get(date_digit or 0, '?')}={date_digit}）不一致"
            )
        method = METHOD_WEEKDAY_PREFIX
        interpreted_encoded = encoded
    elif original.strip().isdigit() and len(digits) == 4:
        first = int(digits[0])
        if first not in WEEKDAY_DIGIT_TO_LABEL:
            notes.append("四位编号首位不在 1..7 内，不推断星期码。")
            return finish(
                method=METHOD_PLAIN_DIGITS,
                confidence=CONFIDENCE_UNVERIFIED,
                converts=False,
                suffix=digits[1:] if len(digits) > 1 else None,
            )
        weekday_digit = first
        suffix = digits[1:]
        weekday_matches = None if date_digit is None else weekday_digit == date_digit
        supporting.append(
            f"编码式编号：首位 {first} 可解释为星期「{WEEKDAY_DIGIT_TO_LABEL[first]}」，序号 {suffix}"
        )
        if weekday_matches is False and date_digit is not None:
            supporting.append(
                f"编号星期与事件日 {event_date} 的日历星期不同（观测到系统性差异，"
                "可能为销售日与比赛日不同）"
            )
        method = METHOD_ENCODED_DIGIT
        interpreted_encoded = digits
        label = WEEKDAY_DIGIT_TO_LABEL[first]
    elif original.strip().isdigit() and len(digits) <= 3:
        return finish(
            method=METHOD_PLAIN_DIGITS,
            confidence=CONFIDENCE_UNVERIFIED,
            converts=False,
            suffix=digits,
        )
    else:
        notes.append(f"无法识别的编号形态（数字位数 {len(digits)}）。")
        return finish(method=METHOD_MALFORMED, confidence=CONFIDENCE_MALFORMED)

    interpreted_weekday_prefix = WEEKDAY_DIGIT_TO_LABEL[weekday_digit]
    interpretation = finish(
        weekday_digit=weekday_digit,
        suffix=suffix,
        interpreted_weekday_prefix=interpreted_weekday_prefix,
        interpreted_encoded=interpreted_encoded,
        method=method,
        confidence=CONFIDENCE_UNVERIFIED,
        converts=True,
    )

    # 若有对端观测，用其原始值核对；核对结果决定置信分类（仍不改写）。
    if counterpart_raw is not None:
        counterpart_digits = _digits(counterpart_raw)
        counterpart_label = _weekday_label(counterpart_raw)
        counterpart_encoded = None
        if counterpart_label is not None and len(counterpart_digits) == 3:
            counterpart_encoded = f"{WEEKDAY_LABEL_TO_DIGIT[counterpart_label]}{counterpart_digits}"
        elif counterpart_digits.isdigit() and len(counterpart_digits) == 4:
            counterpart_encoded = counterpart_digits
        if counterpart_encoded is not None:
            if counterpart_encoded == interpreted_encoded:
                supporting.append(
                    f"对端原始编号 {counterpart_raw} 经同样解释得到相同的编码式 {counterpart_encoded}"
                )
                confidence = CONFIDENCE_SUPPORTED
            else:
                contradictory.append(
                    f"对端原始编号 {counterpart_raw} 解释为 {counterpart_encoded}，"
                    f"与本条 {interpreted_encoded} 不同"
                )
                confidence = CONFIDENCE_CONTRADICTED
            interpretation = finish(
                weekday_digit=interpretation.weekday_digit,
                suffix=interpretation.suffix,
                interpreted_weekday_prefix=interpretation.interpreted_weekday_prefix,
                interpreted_encoded=interpretation.interpreted_encoded,
                method=interpretation.method,
                confidence=confidence,
                converts=confidence != CONFIDENCE_CONTRADICTED,
            )
        else:
            notes.append("对端编号无法解释为可比较的形态，不参与置信判定。")

    if (
        counterpart_team_relation is not None
        and counterpart_team_relation == RELATION_DIFFERENT
    ):
        contradictory.append(
            "同一编号解释对应的对端记录队名互斥（队名关系 DIFFERENT），"
            "编号相等不足以支撑同一事件"
        )
        interpretation = finish(
            weekday_digit=interpretation.weekday_digit,
            suffix=interpretation.suffix,
            interpreted_weekday_prefix=interpretation.interpreted_weekday_prefix,
            interpreted_encoded=interpretation.interpreted_encoded,
            method=interpretation.method,
            confidence=CONFIDENCE_CONTRADICTED,
            converts=False,
        )

    return interpretation


# ---------------------------------------------------------------------------
# 候选级跨源结论
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CrossSourceCandidateEvidence:
    """
    一对跨源记录的候选级证据。

    该对象**不能**授权任何操作：``authoritative`` / ``authorizes_odds_merge`` /
    ``authorizes_event_admission`` 恒为 ``False``，也没有任何"批准"字段。
    """

    outcome: str
    left_canonical_id: str | None
    right_canonical_id: str | None
    sport: str | None
    event_date: str | None
    orientation: str = ORIENTATION_UNKNOWN
    home_relation: str = RELATION_MISSING
    away_relation: str = RELATION_MISSING
    aligned_home_relation: str = RELATION_MISSING
    aligned_away_relation: str = RELATION_MISSING
    date_relation: str = RELATION_MISSING
    league_relation: str = RELATION_MISSING
    kickoff_relation: str = RELATION_MISSING
    numbers_comparable: bool = False
    numbers_agree: bool | None = None
    authorizes_odds_merge: bool = False
    authorizes_event_admission: bool = False
    authoritative: bool = AUTHORITATIVE
    supporting_evidence: tuple[str, ...] = ()
    contradictory_evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    number_evidence: tuple[dict[str, Any], ...] = ()
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    @property
    def is_approval(self) -> bool:
        """永远为 ``False``：候选证据不是批准。"""
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome,
            "left_canonical_id": self.left_canonical_id,
            "right_canonical_id": self.right_canonical_id,
            "sport": self.sport,
            "event_date": self.event_date,
            "orientation": self.orientation,
            "home_relation": self.home_relation,
            "away_relation": self.away_relation,
            "aligned_home_relation": self.aligned_home_relation,
            "aligned_away_relation": self.aligned_away_relation,
            "date_relation": self.date_relation,
            "league_relation": self.league_relation,
            "kickoff_relation": self.kickoff_relation,
            "numbers_comparable": self.numbers_comparable,
            "numbers_agree": self.numbers_agree,
            "authoritative": self.authoritative,
            "is_approval": self.is_approval,
            "authorizes_odds_merge": self.authorizes_odds_merge,
            "authorizes_event_admission": self.authorizes_event_admission,
            "supporting_evidence": list(self.supporting_evidence),
            "contradictory_evidence": list(self.contradictory_evidence),
            "blockers": list(self.blockers),
            "notes": list(self.notes),
            "number_evidence": [dict(item) for item in self.number_evidence],
        }

    def fingerprint(self) -> str:
        return fingerprint(self.as_dict())


def _relation_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str, str]:
    return (
        team_relation(left.get("home"), right.get("home")),
        team_relation(left.get("away"), right.get("away")),
    )


def _both_reversed(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str, str]:
    return (
        team_relation(left.get("home"), right.get("away")),
        team_relation(left.get("away"), right.get("home")),
    )


def _kickoff(record: Mapping[str, Any]) -> str | None:
    value = record.get("time")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and value != "00:00" else None


def classify_cross_source_candidate(
    live_record: Mapping[str, Any],
    lottery_record: Mapping[str, Any],
    evidence_context: Mapping[str, Any] | None = None,
) -> CrossSourceCandidateEvidence:
    """
    对一对跨源记录给出**候选级**结论（纯函数，只读）。

    ``evidence_context`` 只提供**背景证据**（例如是否已取得 provider 编号文档、
    是否已存在强编号冲突）；任何字段都无法把候选结论变成批准——
    返回对象上没有可设置的批准字段。

    判定顺序（任一步 fail closed）：
    1. 输入不是映射或缺运动/日期/队名 -> ``INSUFFICIENT_EVIDENCE``；
    2. 运动或日期不同 -> ``CANDIDATE_DIFFERENT_EVENT``；
    3. 双方队名均互斥 -> ``CANDIDATE_DIFFERENT_EVENT``；
    4. 编号可比较但不一致 -> ``CONTRADICTORY_EVIDENCE``；
    5. 队名仅截断/简称 -> ``AMBIGUOUS``；
    6. 主客队整体反向 -> ``AMBIGUOUS``（反向本身**不**构成同一事件证据）；
    7. 其余一致 -> ``CANDIDATE_SAME_EVENT``（仍然只是候选，``authoritative=False``）。
    """
    context = dict(evidence_context or {})
    provider_documents = bool(context.get("provider_documents"))
    strong_conflict = bool(context.get("strong_number_conflict"))

    supporting: list[str] = []
    contradictory: list[str] = []
    blockers: list[str] = []
    notes: list[str] = []

    if not isinstance(live_record, Mapping) or not isinstance(lottery_record, Mapping):
        return CrossSourceCandidateEvidence(
            outcome=OUTCOME_INSUFFICIENT_EVIDENCE,
            left_canonical_id=None,
            right_canonical_id=None,
            sport=None,
            event_date=None,
            blockers=("input records are not mappings",),
        )

    left_id = str(live_record.get("id") or "") or None
    right_id = str(lottery_record.get("id") or "") or None
    sport = live_record.get("sport") or lottery_record.get("sport")
    event_date = live_record.get("date") or lottery_record.get("date")

    def result(outcome: str, **kwargs: Any) -> CrossSourceCandidateEvidence:
        return CrossSourceCandidateEvidence(
            outcome=outcome,
            left_canonical_id=left_id,
            right_canonical_id=right_id,
            sport=sport,
            event_date=event_date,
            supporting_evidence=tuple(supporting),
            contradictory_evidence=tuple(contradictory),
            blockers=tuple(blockers),
            notes=tuple(notes),
            **kwargs,
        )

    left_sport, right_sport = live_record.get("sport"), lottery_record.get("sport")
    left_date, right_date = live_record.get("date"), lottery_record.get("date")

    if not left_sport or not right_sport:
        blockers.append("sport missing")
        return result(OUTCOME_INSUFFICIENT_EVIDENCE)
    if str(left_sport) != str(right_sport):
        contradictory.append(f"sport mismatch: {left_sport!r} vs {right_sport!r}")
        return result(OUTCOME_CANDIDATE_DIFFERENT_EVENT)
    if not left_date or not right_date:
        blockers.append("event date missing")
        return result(OUTCOME_INSUFFICIENT_EVIDENCE)
    if str(left_date) != str(right_date):
        contradictory.append(f"event date mismatch: {left_date!r} vs {right_date!r}")
        return result(
            OUTCOME_CANDIDATE_DIFFERENT_EVENT, date_relation=RELATION_DIFFERENT
        )

    date_relation = RELATION_EXACT
    supporting.append(f"event date agrees: {left_date}")

    if not live_record.get("home") or not live_record.get("away") or not lottery_record.get("home") or not lottery_record.get("away"):
        blockers.append("team names missing")
        return result(OUTCOME_INSUFFICIENT_EVIDENCE, date_relation=date_relation)

    home_direct, away_direct = _relation_pair(live_record, lottery_record)
    home_rev, away_rev = _both_reversed(live_record, lottery_record)

    direct_score = sum(1 for rel in (home_direct, away_direct) if rel in (RELATION_EXACT, RELATION_NORMALIZED_EQUAL))
    reverse_score = sum(1 for rel in (home_rev, away_rev) if rel in (RELATION_EXACT, RELATION_NORMALIZED_EQUAL))
    variant_any = RELATION_VARIANT in (home_direct, away_direct, home_rev, away_rev)

    raw_number_left = _raw_number(live_record)
    raw_number_right = _raw_number(lottery_record)
    left_number = interpret_lottery_number_evidence(
        raw_number_left,
        sport=str(left_sport),
        event_date=str(left_date),
        source_namespace=str(left_id or "").split("-")[0],
        counterpart_raw=raw_number_right,
        provider_documents=provider_documents,
    )
    right_number = interpret_lottery_number_evidence(
        raw_number_right,
        sport=str(right_sport),
        event_date=str(right_date),
        source_namespace=str(right_id or "").split("-")[0],
        counterpart_raw=raw_number_left,
        provider_documents=provider_documents,
    )
    number_evidence = (left_number.as_dict(), right_number.as_dict())

    numbers_comparable = bool(
        left_number.interpreted_encoded and right_number.interpreted_encoded
    )
    numbers_agree: bool | None = None
    if numbers_comparable:
        numbers_agree = left_number.interpreted_encoded == right_number.interpreted_encoded
        if numbers_agree:
            supporting.append(
                f"number forms correspond: {left_number.raw} <-> {right_number.raw} "
                f"(both {left_number.interpreted_encoded})"
            )
        else:
            contradictory.append(
                f"number forms disagree: {left_number.raw} -> "
                f"{left_number.interpreted_encoded}, {right_number.raw} -> "
                f"{right_number.interpreted_encoded}"
            )
    elif raw_number_left and raw_number_right:
        blockers.append("numbers are not comparable; no conversion is asserted")

    left_league, right_league = live_record.get("league"), lottery_record.get("league")
    league_relation = (
        team_relation(left_league, right_league)
        if left_league and right_league
        else RELATION_MISSING
    )
    if league_relation in (RELATION_EXACT, RELATION_NORMALIZED_EQUAL):
        supporting.append(f"league label agrees: {left_league}")
    elif league_relation == RELATION_DIFFERENT:
        contradictory.append(
            f"league label disagreement: {left_league!r} vs {right_league!r} "
            "(provider naming conventions differ)"
        )

    left_kickoff, right_kickoff = _kickoff(live_record), _kickoff(lottery_record)
    if left_kickoff and right_kickoff:
        kickoff_relation = (
            RELATION_EXACT if left_kickoff == right_kickoff else RELATION_DIFFERENT
        )
        if kickoff_relation == RELATION_EXACT:
            supporting.append(f"kickoff time agrees: {left_kickoff}")
        else:
            contradictory.append(
                f"kickoff time disagreement: {left_kickoff} vs {right_kickoff}"
            )
    else:
        kickoff_relation = RELATION_MISSING
        notes.append(
            "kickoff time unknown on at least one side; time is not treated as evidence"
        )

    shared: dict[str, Any] = {
        "date_relation": date_relation,
        "league_relation": league_relation,
        "kickoff_relation": kickoff_relation,
        "numbers_comparable": numbers_comparable,
        "numbers_agree": numbers_agree,
        "number_evidence": number_evidence,
    }
    unresolved = {
        "orientation": ORIENTATION_UNKNOWN,
        "home_relation": home_direct,
        "away_relation": away_direct,
        "aligned_home_relation": RELATION_MISSING,
        "aligned_away_relation": RELATION_MISSING,
    }

    if strong_conflict:
        contradictory.append(
            "an existing strong competition-number conflict is unresolved; fail closed"
        )
        return result(OUTCOME_CONTRADICTORY_EVIDENCE, **unresolved, **shared)

    same_labels = (RELATION_EXACT, RELATION_NORMALIZED_EQUAL)
    if home_direct in same_labels and away_direct in same_labels:
        orientation = ORIENTATION_SAME
        aligned_home, aligned_away = home_direct, away_direct
        supporting.append("home/away orientation agrees")
    elif home_rev in same_labels and away_rev in same_labels:
        orientation = ORIENTATION_REVERSED
        aligned_home, aligned_away = home_rev, away_rev
        contradictory.append(
            "home/away positions are reversed between the two sources; a reversal alone is "
            "not evidence of the same event and home_win/away_win must never be swapped"
        )
        blockers.append("ORIENTATION_UNRESOLVED_NO_AUTOMATIC_SWAP")
    elif (direct_score + reverse_score) == 0 and not variant_any:
        contradictory.append("team names are mutually exclusive on both sides")
        if numbers_comparable and numbers_agree:
            # 两条独立的结构性证据互相矛盾：编号说"同一场"，队名说"不是"。
            # 这类冲突既可能是音译差异，也可能是真实的编号复用，任何一方都不得独占结论。
            contradictory.append(
                "the number forms agree while the team names are mutually exclusive: the two "
                "independent structural links conflict (transliteration variance or a real "
                "number collision cannot be distinguished from field evidence)"
            )
            blockers.append("NUMBERS_AGREE_NAMES_CONFLICT")
            return result(OUTCOME_CONTRADICTORY_EVIDENCE, **unresolved, **shared)
        return result(OUTCOME_CANDIDATE_DIFFERENT_EVENT, **unresolved, **shared)
    else:
        orientation = ORIENTATION_UNKNOWN
        aligned_home, aligned_away = home_direct, away_direct
        contradictory.append(
            "at least one team name is truncated, abbreviated or only partially matches; "
            "name equivalence is not established"
        )
        blockers.append("TEAM_NAME_EQUIVALENCE_UNVERIFIED")

    aligned = {
        "orientation": orientation,
        "home_relation": home_direct,
        "away_relation": away_direct,
        "aligned_home_relation": aligned_home,
        "aligned_away_relation": aligned_away,
    }

    if numbers_comparable and numbers_agree is False:
        contradictory.append(
            "the two number forms resolve to different competition numbers"
        )
        if orientation in (ORIENTATION_SAME, ORIENTATION_REVERSED):
            # 队名一致而强编号互斥 -> 真实矛盾，fail closed。
            blockers.append("STRONG_NUMBER_CONFLICT_ACROSS_SOURCES")
            return result(OUTCOME_CONTRADICTORY_EVIDENCE, **aligned, **shared)
        blockers.append("NUMBER_DISAGREEMENT_WITH_UNVERIFIED_NAMES")

    if league_relation == RELATION_DIFFERENT:
        blockers.append("LEAGUE_LABEL_DISAGREEMENT")

    aligned = {
        "orientation": orientation,
        "home_relation": home_direct,
        "away_relation": away_direct,
        "aligned_home_relation": aligned_home,
        "aligned_away_relation": aligned_away,
    }
    if blockers:
        return result(OUTCOME_AMBIGUOUS, **aligned, **shared)
    return result(OUTCOME_CANDIDATE_SAME_EVENT, **aligned, **shared)


# ---------------------------------------------------------------------------
# 诊断分类（永不升级为人工裁决）
# ---------------------------------------------------------------------------

CLASS_NUMBER_FORMAT = "NUMBER_FORMAT_DISCREPANCY"
CLASS_ORIENTATION = "HOME_AWAY_ORIENTATION_DISCREPANCY"
CLASS_BOTH = "BOTH_DISCREPANCIES"
CLASS_DATE = "DATE_DISCREPANCY"
CLASS_INSUFFICIENT = "INSUFFICIENT_IDENTITY_EVIDENCE"
CLASS_DISTINCT = "POSSIBLE_DISTINCT_EVENTS"
CLASS_UNCLASSIFIED = "UNCLASSIFIED"
CLASS_ORDER: dict[str, int] = {
    CLASS_DISTINCT: 0,
    CLASS_BOTH: 1,
    CLASS_NUMBER_FORMAT: 2,
    CLASS_ORIENTATION: 3,
    CLASS_DATE: 4,
    CLASS_INSUFFICIENT: 5,
    CLASS_UNCLASSIFIED: 6,
}
ALLOWED_CLASSES: frozenset[str] = frozenset(CLASS_ORDER)


def diagnostic_class(evidence: CrossSourceCandidateEvidence) -> str:
    """
    把候选证据映射为**诊断标签**（不是裁决，也不是人工标签）。

    ``CANDIDATE_SAME_EVENT`` 只会落到 ``UNCLASSIFIED``：本阶段研究的是差异，
    "没有差异"不是一个类别，也不构成任何批准。

    优先级：互斥 > 证据不足 > 名称等价未建立 > 编号形态与方向差异 > 未分类。
    """
    if evidence.date_relation == RELATION_DIFFERENT:
        return CLASS_DATE
    if evidence.outcome == OUTCOME_CANDIDATE_DIFFERENT_EVENT:
        return CLASS_DISTINCT
    if evidence.outcome == OUTCOME_INSUFFICIENT_EVIDENCE:
        return CLASS_INSUFFICIENT
    if "TEAM_NAME_EQUIVALENCE_UNVERIFIED" in evidence.blockers:
        return CLASS_INSUFFICIENT
    if evidence.outcome == OUTCOME_CONTRADICTORY_EVIDENCE:
        return CLASS_UNCLASSIFIED

    raw_forms = [item.get("raw") for item in evidence.number_evidence]
    methods = {
        item.get("method")
        for item in evidence.number_evidence
        if item.get("method") in (METHOD_WEEKDAY_PREFIX, METHOD_ENCODED_DIGIT)
    }
    form_gap = bool(raw_forms) and all(raw_forms) and len(methods) > 1
    orientation_gap = evidence.orientation == ORIENTATION_REVERSED

    if form_gap and orientation_gap:
        return CLASS_BOTH
    if form_gap:
        return CLASS_NUMBER_FORMAT
    if orientation_gap:
        return CLASS_ORIENTATION
    return CLASS_UNCLASSIFIED