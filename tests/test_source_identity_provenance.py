"""
源身份溯源测试套件（PG-01 .. PG-08 及证据完整性）
================================================
覆盖：
- 原始 provider ID / 推导源 ID / Canonical ID 三者的严格区分
- 缺失 provider 证据必须保持缺失（绝不合成）
- 观测与指纹的确定性、批次指纹的顺序无关性
- sport/date 作用域阻止数字编号键碰撞
- 足球 / 篮球共用 500j 前缀的碰撞复现与分类
- 源标识符类型与稳定性的保守分类（不声称未文档化的稳定性）
- 完场 feed 行位置标识符复用的复现与分类
- 只读性、无网络、原始值与规范化值并存

全部使用隔离的合成夹具，绝不访问网络。
"""
from __future__ import annotations

import copy
import json
import os
import re

import pytest

from utils import fetcher_500
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    plan_ingestion_identity,
)
from utils.source_identity import (
    MISSING,
    PROVENANCE_SCHEMA_VERSION,
    SOURCE_ID_PATTERNS,
    IdentifierStability,
    RawIdentifierType,
    batch_raw_evidence_fingerprint,
    batch_semantic_fingerprint,
    build_provenance_report,
    classify_identifier_stability,
    competition_identity_key,
    conflict_report,
    detect_cross_sport_namespace_collision,
    detect_row_position_reuse,
    observe_batch,
    observe_source_row,
    pattern_for_adapter,
)


# ---------------------------------------------------------------------------
# 合成夹具
# ---------------------------------------------------------------------------

def _live_football_html(order: str = "001") -> str:
    return (
        '<table><tr id="a1001" order="%s" status="0">'
        "<td>周三%s</td><td>西甲</td><td>第1轮</td><td>05-01 20:00</td><td>未</td>"
        '<td><span class="mainName">皇马</span></td><td>-</td>'
        '<td><span class="mainName">巴萨</span></td><td>析</td>'
        "</tr></table>" % (order, order)
    )


def _live_basketball_html() -> str:
    return (
        "<script>"
        'var matchList = [["1", "2001", "", "2030-05-01", "19:30", "", "NBA", "", "", '
        '"常规赛", "", "", "", "湖人", "", "", "", "", "勇士", "", "", "", "-", "-", '
        '"", "", "301"]];'
        "var oddsList = {};"
        "</script>"
    )


def _finished_html(rows) -> str:
    body = "".join(
        '<tr id="w%d"><td>英超</td><td>第1轮</td><td>05-01 %s</td><td>完</td>'
        '<td><span class="mainName">%s</span></td><td>%s</td>'
        '<td><span class="mainName">%s</span></td><td>1 - 0</td><td>析</td></tr>'
        % (index, time, home, score, away)
        for index, (time, home, away, score) in enumerate(rows)
    )
    return f"<table>{body}</table>"


def _html_fallback_html() -> str:
    return '<tr homesxname="甲队" awaysxname="乙队">19:30</tr>'


_MARKET_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<xml><m id="999" matchnum="007" date="2030-05-01" league="西甲" '
    'home="皇马" away="巴萨"><row win="1.90" lost="3.80" draw="3.40"/></m></xml>'
)


def _market_row(sport: str = "football", number: str = "001", home: str = "皇马",
                away: str = "巴萨", date: str = "2030-05-01") -> dict:
    return {
        "id": f"500j-{date}-{number}",
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "round": number,
        "date": date,
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "jczq_no": number,
    }


# ---------------------------------------------------------------------------
# PG-01
# ---------------------------------------------------------------------------

def test_pg_01_raw_and_derived_ids_remain_distinct():
    """PG-01: 原始 provider ID / 推导源 ID / Canonical ID 永远是三个不同字段。"""
    row = _market_row()
    observation = observe_source_row(row, adapter="market_xml")
    payload = observation.as_dict()

    for field in ("raw_provider_id", "derived_source_id", "canonical_match_id"):
        assert field in payload, field

    assert observation.derived_source_id == row["id"]
    assert payload["derived_source_id"] == row["id"]
    assert payload["raw_provider_id"] is None
    assert payload["canonical_match_id"] is None

    # 显式提供 provider 标识符时，它与推导源 ID 仍是不同字段。
    with_raw = observe_source_row(
        row, adapter="market_xml", raw_provider_id="999", canonical_match_id="canon-1"
    )
    assert with_raw.raw_provider_id == "999"
    assert with_raw.derived_source_id == "500j-2030-05-01-001"
    assert with_raw.canonical_match_id == "canon-1"
    assert with_raw.raw_provider_id != with_raw.derived_source_id

    # 三个字段没有在任何地方被折叠成一个。
    assert len({"raw_provider_id", "derived_source_id", "canonical_match_id"}) == 3


# ---------------------------------------------------------------------------
# PG-02
# ---------------------------------------------------------------------------

def test_pg_02_missing_provider_id_is_never_synthesized():
    """PG-02: 缺失的 provider ID 必须保持缺失，绝不由其他字段合成。"""
    row = _market_row()
    observation = observe_source_row(row, adapter="market_xml")

    assert observation.raw_provider_id is None
    assert observation.raw_provider_id_present is False
    assert observation.semantic_payload()["raw_provider_id"] is None
    assert "raw_provider_id" in observation.missing_evidence()

    # 原始字段仍然保留（供审计），但没有被冒充为 provider 证据。
    raw = observation.raw_values_dict()
    assert raw["id"] == row["id"]
    assert raw["jczq_no"] == "001"
    assert observation.notes and "缺失" in observation.notes[0]

    # 每一种适配器都不会凭空合成 provider 标识符。
    for adapter in (
        "live_football",
        "live_basketball",
        "market_xml",
        "finished_feed",
        "html_fallback",
    ):
        obs = observe_source_row(_market_row(), adapter=adapter)
        assert obs.raw_provider_id is None, adapter


# ---------------------------------------------------------------------------
# PG-03
# ---------------------------------------------------------------------------

def test_pg_03_source_observations_are_deterministic():
    """PG-03: 相同输入产生完全相同的观测、ID 与指纹。"""
    row = _market_row()
    first = observe_source_row(dict(row), adapter="market_xml")
    second = observe_source_row(dict(row), adapter="market_xml")

    assert first == second
    assert first.observation_id == second.observation_id
    assert first.semantic_fingerprint() == second.semantic_fingerprint()
    assert first.raw_evidence_sha256 == second.raw_evidence_sha256
    assert first.as_dict() == second.as_dict()

    # 语义指纹是可复现的纯函数值。
    assert first.semantic_fingerprint() == first.semantic_fingerprint()
    assert re.fullmatch(r"[0-9a-f]{64}", first.semantic_fingerprint())

    # 内容不同的记录得到不同的指纹。
    other = observe_source_row(_market_row(number="002"), adapter="market_xml")
    assert other.semantic_fingerprint() != first.semantic_fingerprint()


# ---------------------------------------------------------------------------
# PG-04
# ---------------------------------------------------------------------------

def test_pg_04_fingerprints_are_unaffected_by_unrelated_input_ordering():
    """PG-04: 批次语义/原始证据指纹与输入行序无关。"""
    rows = [
        (_market_row(number="001"), "market_xml"),
        (_market_row(sport="basketball", number="001", home="湖人", away="勇士"),
         "market_xml"),
        (_market_row(number="002", home="甲", away="乙"), "market_xml"),
        (_market_row(number="003", home="丙", away="丁"), "market_xml"),
    ]
    observations = [observe_source_row(r, adapter=a) for r, a in rows]

    baseline_semantic = batch_semantic_fingerprint(observations)
    baseline_raw = batch_raw_evidence_fingerprint(observations)

    import itertools

    for permutation in itertools.permutations(range(len(observations))):
        shuffled = [observations[i] for i in permutation]
        assert batch_semantic_fingerprint(shuffled) == baseline_semantic
        assert batch_raw_evidence_fingerprint(shuffled) == baseline_raw

    # 加入一条无关观测会改变指纹（指纹确实反映内容）。
    extra = observe_source_row(_market_row(number="004", home="戊", away="己"),
                               adapter="market_xml")
    assert batch_semantic_fingerprint(observations + [extra]) != baseline_semantic


# ---------------------------------------------------------------------------
# PG-05
# ---------------------------------------------------------------------------

def test_pg_05_sport_date_scoping_prevents_numeric_competition_key_collisions():
    """PG-05: 竞彩身份必须带 sport + date 作用域，裸数字编号不是全局事件键。"""
    football = observe_source_row(_market_row(sport="football"), adapter="market_xml")
    basketball = observe_source_row(
        _market_row(sport="basketball", home="湖人", away="勇士"), adapter="market_xml"
    )
    next_day = observe_source_row(
        _market_row(date="2030-05-02"), adapter="market_xml"
    )

    assert football.competition_number_normalized == "1"
    assert basketball.competition_number_normalized == "1"
    assert next_day.competition_number_normalized == "1"

    assert football.competition_identity == "football|2030-05-01|jczq|1"
    assert basketball.competition_identity == "basketball|2030-05-01|jczq|1"
    assert next_day.competition_identity == "football|2030-05-02|jczq|1"
    assert len(
        {
            football.competition_identity,
            basketball.competition_identity,
            next_day.competition_identity,
        }
    ) == 3

    # 语义指纹同样区分运动与日期。
    assert len(
        {
            football.semantic_fingerprint(),
            basketball.semantic_fingerprint(),
            next_day.semantic_fingerprint(),
        }
    ) == 3

    # 缺少任一段都不构成有效竞彩身份。
    assert competition_identity_key(None, "2030-05-01", "1") is None
    assert competition_identity_key("football", None, "1") is None
    assert competition_identity_key("football", "2030-05-01", None) is None


# ---------------------------------------------------------------------------
# PG-06
# ---------------------------------------------------------------------------

def test_pg_06_cross_sport_500j_collision_is_reproduced_and_classified():
    """PG-06: 足球/篮球共用 500j 前缀的碰撞被复现、分类，且既有预检 fail closed。"""
    football_row = _market_row(sport="football", number="001")
    basketball_row = _market_row(
        sport="basketball", number="001", home="湖人", away="勇士"
    )

    # 碰撞被真实复现：两个不同运动的比赛共享同一个 500j 字符串。
    assert football_row["id"] == basketball_row["id"] == "500j-2030-05-01-001"

    football = observe_source_row(football_row, adapter="market_xml")
    basketball = observe_source_row(basketball_row, adapter="market_xml")

    conflicts = detect_cross_sport_namespace_collision([football, basketball])
    assert len(conflicts) == 1
    assert conflicts[0].category == "CROSS_SPORT_SHARED_NAMESPACE"
    assert conflicts[0].severity == "AMBIGUOUS"
    assert conflicts[0].subject == "500j-2030-05-01-001"

    # 共享的旧字符串不足以确立唯一事件身份：二者仍是不同作用域身份。
    assert football.semantic_fingerprint() != basketball.semantic_fingerprint()
    assert football.competition_identity != basketball.competition_identity

    # 既有摄取预检对该批次 fail closed（只读消费其结论）。
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity([], [football_row, basketball_row])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    # 冲突报告是确定性的。
    report = conflict_report([football, basketball])
    assert report["conflict_count"] == 1
    assert report["conflicts"][0]["category"] == "CROSS_SPORT_SHARED_NAMESPACE"


# ---------------------------------------------------------------------------
# PG-07
# ---------------------------------------------------------------------------

def test_pg_07_source_id_inventory_matches_actual_parsers(monkeypatch):
    """PG-07: 清单与真实解析器一致；稳定性分类保守，不声称未文档化的稳定性。"""
    # --- live football：order 存在 -> 竞彩编号作用域 ---
    live_rows, errors = fetcher_500._parse_live(_live_football_html(), "a", "football")
    assert errors == 0 and len(live_rows) == 1
    live_football = observe_source_row(live_rows[0], adapter="live_football")
    assert live_football.source_namespace == "500l"
    assert live_football.derived_source_id == live_rows[0]["id"]
    assert live_football.competition_number_normalized == "1"
    assert live_football.identifier_stability == (
        IdentifierStability.COMPETITION_NUMBER_SCOPED.value
    )
    assert live_football.raw_provider_id_type == RawIdentifierType.COMPETITION_NUMBER.value

    # --- live basketball ---
    lq_rows, lq_errors = fetcher_500._parse_lq(_live_basketball_html())
    assert lq_errors == 0 and len(lq_rows) == 1
    live_basketball = observe_source_row(lq_rows[0], adapter="live_basketball")
    assert live_basketball.source_namespace == "500lq"
    assert live_basketball.sport == "basketball"
    assert live_basketball.competition_number_normalized == "301"

    # --- market XML（只读消费真实解析器） ---
    fetcher_500._CACHE.clear()
    monkeypatch.setattr(fetcher_500, "_fetch_xml", lambda lot, play: _MARKET_XML)
    try:
        xml_rows = fetcher_500.fetch_jczq_xml("football")
        assert len(xml_rows) == 1
        market = observe_source_row(xml_rows[0], adapter="market_xml")
        assert market.source_namespace == "500j"
        assert market.competition_number_normalized == "7"
        assert market.identifier_stability == (
            IdentifierStability.COMPETITION_NUMBER_SCOPED.value
        )
    finally:
        fetcher_500._CACHE.clear()

    # --- finished feed：行位置派生 -> ROW_POSITION_UNSTABLE ---
    finished_rows = fetcher_500._parse_wanchang(
        _finished_html([("15:30", "甲队", "乙队", "2 - 1")])
    )
    assert len(finished_rows) == 1
    finished = observe_source_row(finished_rows[0], adapter="finished_feed")
    assert finished.source_namespace == "500w"
    assert finished.identifier_stability == (
        IdentifierStability.ROW_POSITION_UNSTABLE.value
    )
    assert finished.raw_provider_id_type == RawIdentifierType.ROW_INDEX.value
    assert finished.competition_number_normalized is None
    assert finished.is_row_position_unstable is True

    # --- HTML 兜底：同样是行位置派生，却共享 500j 前缀 ---
    fallback_rows = fetcher_500._parse_jczq_html(_html_fallback_html())
    assert len(fallback_rows) == 1
    fallback = observe_source_row(fallback_rows[0], adapter="html_fallback")
    assert fallback.source_namespace == "500j"
    assert fallback.identifier_stability == (
        IdentifierStability.ROW_POSITION_UNSTABLE.value
    )
    assert fallback.raw_provider_id_type == RawIdentifierType.ROW_INDEX.value

    # --- 清单完备性：每条清单条目都对应一个真实适配器，且不含未登记的路径 ---
    adapters = {p.adapter for p in SOURCE_ID_PATTERNS}
    assert adapters == {
        "live_football",
        "live_basketball",
        "market_xml",
        "finished_feed",
        "html_fallback",
    }
    # 未提供 provider 标识符时，绝不声称 PROVIDER_SUPPLIED_UNVERIFIED。
    for adapter in adapters:
        assert pattern_for_adapter(adapter) is not None
    assert classify_identifier_stability(
        adapter="market_xml",
        derived_source_id="500j-2030-05-01-999",
        competition_number_normalized="1",
        raw_provider_id_present=False,
    )[0] == IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value
    assert classify_identifier_stability(
        adapter="market_xml",
        derived_source_id="500j-2030-05-01-999",
        competition_number_normalized="1",
        raw_provider_id_present=True,
    )[0] == IdentifierStability.PROVIDER_SUPPLIED_UNVERIFIED.value
    assert classify_identifier_stability(
        adapter="unknown_source",
        derived_source_id="weird-1",
        competition_number_normalized=None,
        raw_provider_id_present=False,
    )[0] == IdentifierStability.UNKNOWN.value


# ---------------------------------------------------------------------------
# PG-08
# ---------------------------------------------------------------------------

def test_pg_08_finished_feed_position_reuse_is_detected():
    """PG-08: 不同行序的两次刷新会把同一 500w ID 分配给不同比赛。"""
    matches = [("15:30", "甲队", "乙队", "2 - 1"), ("18:00", "丙队", "丁队", "0 - 3")]

    first_pass = fetcher_500._parse_wanchang(_finished_html(matches))
    second_pass = fetcher_500._parse_wanchang(_finished_html(list(reversed(matches))))

    assert len(first_pass) == 2 and len(second_pass) == 2

    # 第一次刷新：位置 0 -> 甲队/乙队
    first_row = first_pass[0]
    assert first_row["home"] == "甲队"
    assert first_row["id"].endswith("-0")

    # 第二次刷新（行序颠倒）：位置 0 -> 丙队/丁队，**同一个 ID 落到了另一场比赛**。
    reused = second_pass[0]
    assert reused["home"] == "丙队"
    assert reused["id"] == first_row["id"]

    # 而甲队/乙队在第二次刷新中拿到了另一个 ID。
    moved = second_pass[1]
    assert moved["home"] == "甲队"
    assert moved["id"] != first_row["id"]

    observation_a = observe_source_row(first_row, adapter="finished_feed")
    observation_b = observe_source_row(reused, adapter="finished_feed")

    assert observation_a.derived_source_id == observation_b.derived_source_id
    assert observation_a.is_row_position_unstable

    conflicts = detect_row_position_reuse([observation_a, observation_b])
    assert len(conflicts) == 1
    assert conflicts[0].category == "ROW_POSITION_REUSE"
    assert conflicts[0].severity == "AMBIGUOUS"

    # 行位置标识符绝不构成持久身份：既有预检拒绝据此覆盖无关 Canonical 记录。
    canonical = [dict(first_row, market_tracked=True)]
    assert canonical[0]["id"] == reused["id"]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(canonical, [reused])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


# ---------------------------------------------------------------------------
# 证据完整性补充
# ---------------------------------------------------------------------------

def test_observations_do_not_mutate_source_rows():
    """溯源层是只读的：绝不修改传入的源字典。"""
    row = _market_row()
    before = copy.deepcopy(row)
    observe_source_row(row, adapter="market_xml")
    assert row == before

    rows = [_market_row(number="001"), _market_row(number="002", home="甲", away="乙")]
    rows_before = copy.deepcopy(rows)
    observe_batch(rows, adapter="market_xml")
    assert rows == rows_before


def test_observation_layer_performs_no_network_access(monkeypatch):
    """溯源层不访问网络：任何网络调用都会让本测试失败。"""
    import requests

    def _boom(*args, **kwargs):
        raise AssertionError("the provenance layer must never touch the network")

    monkeypatch.setattr(requests, "get", _boom)
    monkeypatch.setattr(requests, "post", _boom)

    observation = observe_source_row(_market_row(), adapter="market_xml")
    assert observation.semantic_fingerprint()

    module_source = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "utils", "source_identity.py"),
        encoding="utf-8",
    ).read()
    assert "import requests" not in module_source
    assert "urllib" not in module_source


def test_missing_evidence_is_explicit_and_raw_values_are_preserved():
    """缺失证据必须显式缺失；规范化值绝不取代原始源证据。"""
    row = _market_row(number="001")
    row.pop("jczq_no", None)
    row["id"] = "500l-2026-05-01-1495377"   # 末段不是竞彩编号
    row["round"] = ""

    observation = observe_source_row(row, adapter="live_football")
    completeness = observation.completeness_dict()

    assert completeness["raw_provider_id"] is False
    assert completeness["derived_source_id"] is True
    assert completeness["home_team"] is True

    missing = observation.missing_evidence()
    assert "raw_provider_id" in missing

    # 原始值保留；规范化值单独存放。
    raw = observation.raw_values_dict()
    assert raw["id"] == "500l-2026-05-01-1495377"
    assert observation.derived_source_id == "500l-2026-05-01-1495377"
    assert observation.competition_number_normalized is None
    assert observation.identifier_stability in {
        IdentifierStability.DERIVED_FROM_PROVIDER_FIELDS.value,
        IdentifierStability.UNKNOWN.value,
    }

    # 未读到的字段显式记为 MISSING，而不是空串或被省略。
    row_without_time = _market_row()
    row_without_time.pop("time", None)
    obs2 = observe_source_row(row_without_time, adapter="market_xml")
    assert obs2.raw_values_dict()["time"] == MISSING


def test_provenance_report_is_deterministic_and_versioned():
    """溯源报告带显式 schema 版本，且内容确定。"""
    observations = [
        observe_source_row(_market_row(number="001"), adapter="market_xml"),
        observe_source_row(_market_row(number="002", home="甲", away="乙"),
                           adapter="market_xml"),
    ]
    first = build_provenance_report(observations)
    second = build_provenance_report(list(reversed(observations)))

    assert first["provenance_schema_version"] == PROVENANCE_SCHEMA_VERSION
    assert first["observation_count"] == 2
    assert first["batch_semantic_fingerprint"] == second["batch_semantic_fingerprint"]
    assert first["observations"] == second["observations"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ---------------------------------------------------------------------------
# MC-01 / MC-04 / MC-05 / MC-06 / MC-08 — 作用域与身份键的正确区分
# ---------------------------------------------------------------------------

def test_mc_01_distinct_competition_numbers_produce_distinct_identity_keys():
    """MC-01: 同日不同竞彩编号 -> 不同作用域身份键（不再是同一绑定键）。"""
    from utils.source_identity import ScopedSourceIdentityKey

    observation_a = observe_source_row(_market_row(number="001"), adapter="market_xml")
    observation_b = observe_source_row(_market_row(number="002", home="甲", away="乙"),
                                       adapter="market_xml")

    key_a = ScopedSourceIdentityKey.for_observation(observation_a)
    key_b = ScopedSourceIdentityKey.for_observation(observation_b)

    assert observation_a.stability_scope.as_dict() == observation_b.stability_scope.as_dict()
    assert key_a.identity_kind == key_b.identity_kind == "COMPETITION_NUMBER"
    assert key_a.identity_value == "football|2030-05-01|jczq|1"
    assert key_b.identity_value == "football|2030-05-01|jczq|2"
    assert key_a.source_key() != key_b.source_key()

    # 治理层的绑定键同样区分二者。
    from utils.mapping_governance import ProposalAction, proposal_from_observations

    proposal_a = proposal_from_observations(
        [observation_a],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id="canon-A",
        existing_canonical_owner="canon-A",
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint="baseline-A",
        reason="bind first event",
        identity_evidence_type="strong_competition_number",
    )
    proposal_b = proposal_from_observations(
        [observation_b],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id="canon-B",
        existing_canonical_owner="canon-B",
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint="baseline-A",
        reason="bind second event",
        identity_evidence_type="strong_competition_number",
    )
    assert proposal_a.source_key != proposal_b.source_key
    assert proposal_a.identity_key_sufficient and proposal_b.identity_key_sufficient


def test_mc_04_same_number_football_and_basketball_remain_distinct():
    """MC-04: 同编号足球/篮球身份保持区分且作用域正确。"""
    from utils.source_identity import ScopedSourceIdentityKey

    football = observe_source_row(_market_row(sport="football"), adapter="market_xml")
    basketball = observe_source_row(
        _market_row(sport="basketball", home="湖人", away="勇士"), adapter="market_xml"
    )

    key_football = ScopedSourceIdentityKey.for_observation(football)
    key_basketball = ScopedSourceIdentityKey.for_observation(basketball)

    assert key_football.identity_value == "football|2030-05-01|jczq|1"
    assert key_basketball.identity_value == "basketball|2030-05-01|jczq|1"
    assert key_football.source_key() != key_basketball.source_key()
    # 键本身已包含 sport 作用域，不依赖裸数字编号。
    assert "football" in key_football.identity_value
    assert "basketball" in key_basketball.identity_value


def test_mc_05_same_number_different_dates_remain_distinct():
    """MC-05: 同编号不同日期的身份保持区分。"""
    from utils.source_identity import ScopedSourceIdentityKey

    day_one = observe_source_row(_market_row(number="001"), adapter="market_xml")
    day_two = observe_source_row(
        _market_row(number="001", date="2030-05-02"), adapter="market_xml"
    )

    key_one = ScopedSourceIdentityKey.for_observation(day_one)
    key_two = ScopedSourceIdentityKey.for_observation(day_two)

    assert key_one.identity_value == "football|2030-05-01|jczq|1"
    assert key_two.identity_value == "football|2030-05-02|jczq|1"
    assert key_one.source_key() != key_two.source_key()


def test_mc_06_repeated_observations_resolve_to_one_identity_key():
    """MC-06: 同一源身份的重复观测解析到同一身份键（与观测顺序无关）。"""
    from utils.source_identity import ScopedSourceIdentityKey, batch_semantic_fingerprint

    rows = [
        (_market_row(number="001"), "market_xml"),
        (_market_row(number="002", home="甲", away="乙"), "market_xml"),
        (_market_row(number="003", home="丙", away="丁"), "market_xml"),
    ]

    first_pass = [observe_source_row(r, adapter=a) for r, a in rows]
    second_pass = [observe_source_row(dict(r), adapter=a) for r, a in reversed(rows)]

    keys_first = [ScopedSourceIdentityKey.for_observation(o) for o in first_pass]
    keys_second = [ScopedSourceIdentityKey.for_observation(o) for o in second_pass]

    assert {k.source_key() for k in keys_first} == {k.source_key() for k in keys_second}
    assert len({k.source_key() for k in keys_first}) == 3

    # 批次证据指纹同样与顺序无关。
    assert batch_semantic_fingerprint(first_pass) == batch_semantic_fingerprint(second_pass)


def test_mc_08_missing_provider_evidence_is_never_fabricated_into_an_identity_key():
    """MC-08: 缺失的 provider 证据不得被合成进身份键。"""
    from utils.source_identity import IdentityKeyKind, ScopedSourceIdentityKey

    finished = fetcher_500._parse_wanchang(
        _finished_html([("15:30", "甲队", "乙队", "2 - 1")])
    )[0]
    observation = observe_source_row(finished, adapter="finished_feed")

    key = ScopedSourceIdentityKey.for_observation(observation)

    # 行位置派生 -> 证据不足，不得用于活动绑定。
    assert key.identity_kind == IdentityKeyKind.INSUFFICIENT.value
    assert key.is_sufficient is False
    # 身份键取值是**已观测**的推导标识符，绝不是凭空合成的 provider 标识符。
    assert key.identity_value == finished["id"]
    assert key.audit_payload()["detail"]["identifier_stability"] == (
        IdentifierStability.ROW_POSITION_UNSTABLE.value
    )
    assert observation.raw_provider_id is None

    # 原始 provider 证据仍然缺失（不会因为要构造身份键而出现）。
    assert "raw_provider_id" in observation.missing_evidence()
