"""
Provider 身份证据测试套件（PE-01 .. PE-18）
==========================================
覆盖：
- RAW_PROVIDER_ID 的作用域安全（跨日期 / 跨运动不得碰撞）
- 未验证的 provider 唯一性绝不自动获得活动绑定权限
- 竞彩编号作用域身份保持区分且确定
- 缺失 provider 标识符保持缺失（绝不合成）
- 原始 provider 字段与行序号的严格区分
- 五个适配器（live football / live basketball / market XML / finished feed / HTML 兜底）
  的原始证据抽取
- 既有解析器返回形状不变、不新增运行期 Canonical 元数据字段
- 证据指纹确定性、原始证据不被规范化改写
- 旧身份键契约的批准不得被静默复用
- 独立历史批准审计与冲突绑定拒绝

全部使用隔离的合成夹具，绝不访问网络，绝不触碰生产数据。
"""
from __future__ import annotations

import copy
import json
import os
import re

import pytest

from utils import fetcher_500
from utils.mapping_governance import (
    IDENTITY_KEY_SCHEMA_VERSION,
    ConflictingBindingError,
    GovernanceLedger,
    LegacyBindingKeyError,
    MappingState,
    ProposalAction,
    ProposalValidationError,
    SignatureVerificationError,
    audit_historical_approvals,
    build_approval_body,
    build_proposal,
    classify_binding_key,
    generate_mapping_owner_keypair,
    mapping_eligibility,
    proposal_from_observations,
    sign_mapping_approval,
    verify_mapping_approval,
)
from utils.provider_evidence import (
    PROVIDER_EVIDENCE_ADAPTERS,
    PROVIDER_EVIDENCE_SCHEMA_VERSION,
    extract_rows_and_observations,
    extract_source_evidence,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION as SOURCE_IDENTITY_KEY_VERSION,
    LEGACY_IDENTITY_KEY_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION,
    PROVIDER_EVIDENCE_INVENTORY,
    SUPERSEDED_IDENTITY_KEY_SCHEMA_VERSION,
    SUFFICIENT_IDENTITY_KEY_KINDS,
    VERIFIED_PROVIDER_ID_SCOPES,
    IdentifierStability,
    IdentityKeyKind,
    ProviderIdScopeDeclaration,
    ProviderIdUniquenessScope,
    RawIdentifierType,
    ScopedSourceIdentityKey,
    batch_semantic_fingerprint,
    inventory_for_adapter,
    observe_source_row,
    resolve_provider_id_uniqueness_scope,
)

DATASET = "synthetic-provider-evidence-fixture"


# ---------------------------------------------------------------------------
# 合成夹具
# ---------------------------------------------------------------------------

def _live_football_html(order: str = "001", fid: str = "1001") -> str:
    return (
        '<table><tr id="a%s" order="%s" status="0">'
        "<td>周三%s</td><td>西甲</td><td>第1轮</td><td>05-01 20:00</td><td>未</td>"
        '<td><span class="mainName">皇马</span></td><td>-</td>'
        '<td><span class="mainName">巴萨</span></td><td>析</td>'
        "</tr></table>" % (fid, order, order)
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


_MARKET_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<xml><m id="999" matchnum="007" date="2030-05-01" league="西甲" '
    'home="皇马" away="巴萨"><row win="1.90" lost="3.80" draw="3.40"/></m></xml>'
)


def _finished_html(rows, *, with_element_id: bool = True) -> str:
    body = ""
    for index, (time, home, away, score) in enumerate(rows):
        id_attr = ' id="w%d"' % index if with_element_id else ""
        body += (
            "<tr%s><td>英超</td><td>第1轮</td><td>05-01 %s</td><td>完</td>"
            '<td><span class="mainName">%s</span></td><td>%s</td>'
            '<td><span class="mainName">%s</span></td><td>1 - 0</td><td>析</td></tr>'
            % (id_attr, time, home, score, away)
        )
    return f"<table>{body}</table>"


def _html_fallback_html(data_mid: str | None = "555") -> str:
    attr = f' data-mid="{data_mid}"' if data_mid is not None else ""
    return f'<tr{attr} homesxname="甲队" awaysxname="乙队">19:30</tr>'


def _provider_row(
    *,
    sport: str = "football",
    date: str = "2030-05-01",
    number: str | None = "001",
    namespace: str = "500j",
    home: str = "皇马",
    away: str = "巴萨",
) -> dict:
    row = {
        "id": f"{namespace}-{date}-{number or '0'}",
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "date": date,
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": None,
    }
    if number is not None:
        row["jczq_no"] = number
    return row


def _observed(
    *,
    provider_id: str = "12345",
    provider_type: str = RawIdentifierType.PROVIDER_MATCH_ID.value,
    scope: str | None = None,
    **row_kwargs,
):
    return observe_source_row(
        _provider_row(**row_kwargs),
        adapter="market_xml",
        raw_provider_id=provider_id,
        raw_provider_id_type=provider_type,
        raw_provider_id_uniqueness_scope=scope,
    )


@pytest.fixture(scope="module")
def owner_keys(tmp_path_factory):
    base = tmp_path_factory.mktemp("pe_owner_keys")
    private = str(base / "owner_private.pem")
    public = str(base / "owner_public.pem")
    info = generate_mapping_owner_keypair(private, public)
    return {"private": private, "public": public, "public_key_id": info["public_key_id"]}


def _ledger(tmp_path, name="pe_gov"):
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    return GovernanceLedger(
        str(workspace / "mapping_governance_ledger.jsonl"), dataset_identity=DATASET
    )


def _binding_proposal(observation, canonical_id, baseline, operator="operator-1"):
    return proposal_from_observations(
        [observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=canonical_id,
        existing_canonical_owner=canonical_id,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=baseline,
        reason="observed source row reconciles with an existing Canonical owner",
        identity_evidence_type="strong_competition_number",
        operator=operator,
    )


def _approve(ledger, proposal, observation, keys, *, baseline, approver="owner-1",
             operator="operator-1"):
    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=DATASET,
        operator=operator,
        approver=approver,
        approved_at="2030-01-01T00:00:00+00:00",
        reason="owner approves the proposed binding",
    )
    approval = sign_mapping_approval(keys["private"], body)
    return ledger.append(
        proposal=proposal,
        to_state=MappingState.APPROVED.value,
        actor=approver,
        actor_role="owner",
        reason="owner approves the proposed binding",
        baseline_fingerprint=baseline,
        approval=approval,
        trusted_public_key_path=keys["public"],
        source_observation_fingerprint=observation.semantic_fingerprint(),
    )


# ---------------------------------------------------------------------------
# PE-01
# ---------------------------------------------------------------------------

def test_pe_01_provider_id_repetition_across_dates_uses_an_explicit_safe_scope():
    """PE-01: 同一 provider 标识符跨日期出现时，必须落到**不同的**作用域身份。"""
    # 复现条件：竞彩编号缺失，只有 provider 标识符可用。
    day_one = _observed(date="2030-05-01", number=None)
    day_two = _observed(date="2030-05-02", number=None)

    key_one = ScopedSourceIdentityKey.for_observation(day_one)
    key_two = ScopedSourceIdentityKey.for_observation(day_two)

    # 作用域显式包含日期：同一个原始数字不再碰撞成同一个绑定身份。
    assert key_one.identity_value != key_two.identity_value
    assert "2030-05-01" in key_one.identity_value
    assert "2030-05-02" in key_two.identity_value
    assert key_one.source_key() != key_two.source_key()

    # 未验证的唯一性被显式记录（UNKNOWN），而不是被假定为全局唯一。
    assert day_one.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )
    assert day_two.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )
    assert key_one.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )

    # 观测本身仍被记录（原始值与类型都在）。
    for observation in (day_one, day_two):
        assert observation.raw_provider_id == "12345"
        assert observation.raw_provider_id_type == RawIdentifierType.PROVIDER_MATCH_ID.value
        assert observation.raw_provider_id_present is True

    # 竞彩编号存在时优先构成作用域竞彩身份（provider 证据不参与）。
    with_number = _observed(date="2030-05-01")
    with_number_key = ScopedSourceIdentityKey.for_observation(with_number)
    assert with_number_key.identity_kind == IdentityKeyKind.COMPETITION_NUMBER.value
    assert with_number_key.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
    )


# ---------------------------------------------------------------------------
# PE-02
# ---------------------------------------------------------------------------

def test_pe_02_provider_id_repetition_across_sports_cannot_share_a_binding():
    """PE-02: 同一 provider 标识符跨运动出现时，绝不能产生不安全的共享绑定。"""
    # 复现条件：竞彩编号缺失，只有 provider 标识符可用。
    football = _observed(sport="football", number=None)
    basketball = _observed(sport="basketball", number=None, home="湖人", away="勇士")

    key_football = ScopedSourceIdentityKey.for_observation(football)
    key_basketball = ScopedSourceIdentityKey.for_observation(basketball)

    assert key_football.identity_value != key_basketball.identity_value
    assert key_football.source_key() != key_basketball.source_key()
    assert "football" in key_football.identity_value
    assert "basketball" in key_basketball.identity_value

    # 两者都不充分 -> 任何活动绑定都会被拒绝。
    for key in (key_football, key_basketball):
        assert key.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
        assert key.is_sufficient is False
        assert key.requires_review is True
    with pytest.raises(ProposalValidationError):
        _binding_proposal(football, "canon-x", "baseline-A")
    with pytest.raises(ProposalValidationError):
        _binding_proposal(basketball, "canon-x", "baseline-A")

    # 三个不同 (sport, date) 作用域共用一个原始数字时得到三个不同身份。
    keys = {
        ScopedSourceIdentityKey.for_observation(_observed(number=None)).source_key(),
        ScopedSourceIdentityKey.for_observation(
            _observed(date="2030-05-02", number=None)
        ).source_key(),
        ScopedSourceIdentityKey.for_observation(
            _observed(sport="basketball", number=None, home="湖人", away="勇士")
        ).source_key(),
    }
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# PE-03
# ---------------------------------------------------------------------------

def test_pe_03_unverified_provider_uniqueness_never_authorizes_an_active_binding(tmp_path):
    """PE-03: 未验证的 provider 唯一性绝不自动授权活动绑定（fail closed）。"""
    observation = _observed(number=None)

    assert IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value not in SUFFICIENT_IDENTITY_KEY_KINDS
    assert resolve_provider_id_uniqueness_scope(
        adapter="market_xml",
        provider_namespace="500j",
        sport="football",
        raw_identifier_type=RawIdentifierType.PROVIDER_MATCH_ID.value,
    ) == ProviderIdUniquenessScope.UNKNOWN.value
    # 唯一性注册表刻意保持为空：仓库没有独立文档化的 provider 唯一性保证。
    assert VERIFIED_PROVIDER_ID_SCOPES == {}

    # 提案层拒绝：绑定动作需要充分证据（构造时即被拒绝）。
    with pytest.raises(ProposalValidationError):
        _binding_proposal(observation, "canon-x", "baseline-A")

    # 账本层同样拒绝（即使绕过提案层构造一个旧式键）。
    ledger = _ledger(tmp_path)
    key = ScopedSourceIdentityKey.for_observation(observation)
    with pytest.raises(ProposalValidationError):
        build_proposal(
            source_observation_ref=observation.observation_id,
            source_identity_scope=key.scope.as_dict(),
            scoped_source_identity_key=key.as_dict(),
            candidate_canonical_id="canon-x",
            existing_canonical_owner="canon-x",
            identity_evidence_type="provider_match_id",
            observation_fingerprints=[observation.semantic_fingerprint()],
            ingestion_plan_reference="planner:ACCEPTED",
            reason="attempt to bind on unverified provider evidence",
            expected_baseline_fingerprint="baseline-A",
            requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        )

    # 观测本身仍然被完整记录（可观测、可复核）。
    assert observation.raw_provider_id == "12345"
    assert observation.raw_provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )
    assert observation.completeness_dict()["provider_id_uniqueness_verified"] is False
    assert ledger.exists() is False


def test_verified_scope_declaration_grants_raw_provider_id_kind(monkeypatch):
    """补充不变量：只有**独立文档化**的唯一性作用域才产生充分证据。"""
    observation = _observed(number=None)
    assert ScopedSourceIdentityKey.for_observation(observation).is_sufficient is False

    monkeypatch.setitem(
        VERIFIED_PROVIDER_ID_SCOPES,
        ("market_xml", "500j"),
        ProviderIdScopeDeclaration(
            adapter="market_xml",
            provider_namespace="500j",
            sport="*",
            raw_identifier_type=RawIdentifierType.PROVIDER_MATCH_ID.value,
            uniqueness_scope=ProviderIdUniquenessScope.VERIFIED_SCOPED.value,
            documentation_reference="tests/test_provider_identity_evidence.py (synthetic)",
        ),
    )

    verified_observation = _observed(number=None)
    key = ScopedSourceIdentityKey.for_observation(verified_observation)

    assert verified_observation.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.VERIFIED_SCOPED.value
    )
    assert key.identity_kind == IdentityKeyKind.RAW_PROVIDER_ID.value
    assert key.is_sufficient is True
    # 即使唯一性已验证，身份值仍然携带 sport/date 作用域。
    assert "2030-05-01" in key.identity_value
    assert "football" in key.identity_value


# ---------------------------------------------------------------------------
# PE-04
# ---------------------------------------------------------------------------

def test_pe_04_scoped_competition_numbers_remain_distinct_and_deterministic():
    """PE-04: 合法竞彩编号作用域身份保持区分且确定。"""
    first = observe_source_row(
        _provider_row(number="001"), adapter="market_xml"
    )
    second = observe_source_row(
        _provider_row(number="002", home="甲", away="乙"), adapter="market_xml"
    )
    repeat = observe_source_row(_provider_row(number="001"), adapter="market_xml")

    key_first = ScopedSourceIdentityKey.for_observation(first)
    key_second = ScopedSourceIdentityKey.for_observation(second)
    key_repeat = ScopedSourceIdentityKey.for_observation(repeat)

    assert key_first.identity_kind == IdentityKeyKind.COMPETITION_NUMBER.value
    assert key_first.is_sufficient is True
    assert key_first.identity_value == "football|2030-05-01|jczq|1"
    assert key_second.identity_value == "football|2030-05-01|jczq|2"
    assert key_first.source_key() != key_second.source_key()
    # 同一身份的重复观测 -> 同一身份键（确定性）。
    assert key_first.source_key() == key_repeat.source_key()
    assert key_first.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
    )


# ---------------------------------------------------------------------------
# PE-05
# ---------------------------------------------------------------------------

def test_pe_05_missing_provider_identifier_stays_missing():
    """PE-05: 缺失的 provider 标识符必须保持缺失，绝不被合成。"""
    extraction = extract_source_evidence(
        _finished_html([("15:30", "甲队", "乙队", "2 - 1")], with_element_id=False),
        adapter="finished_feed",
    )
    record = extraction.sidecar.records[0]
    observation = extraction.provenance_observations[0]

    assert record.raw_provider_id is None
    assert record.html_element_id is None
    assert record.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
    )
    assert observation.raw_provider_id is None
    assert observation.raw_provider_id_present is False
    assert "raw_provider_id" in observation.missing_evidence()
    assert observation.completeness_dict()["raw_provider_id"] is False
    assert "MISSING" not in (observation.raw_provider_id or "")

    # 行位置仍然被记录，但**绝不**被当作 provider 标识符。
    assert record.row_ordinal == 0
    assert observation.derived_source_id == record.derived_source_id
    assert ScopedSourceIdentityKey.for_observation(observation).is_sufficient is False


# ---------------------------------------------------------------------------
# PE-06
# ---------------------------------------------------------------------------

def test_pe_06_raw_provider_fields_are_preserved_without_row_ordinal_confusion():
    """PE-06: 真实原始 provider 字段被保留，且绝不与行序号混淆。"""
    extraction = extract_source_evidence(_live_football_html(), adapter="live_football")
    record = extraction.sidecar.records[0]

    assert record.raw_provider_id == "a1001"
    assert record.raw_provider_id_type == RawIdentifierType.HTML_ELEMENT_ID.value
    assert record.html_element_id == "a1001"
    assert record.competition_number_raw == "001"
    assert record.row_ordinal == 0

    # 三者互不相同、各自独立存放。
    assert record.raw_provider_id != str(record.row_ordinal)
    assert record.html_element_id != record.competition_number_raw
    assert dict(record.raw_attributes)["order"] == "001"
    assert dict(record.raw_attributes)["status"] == "0"

    # 竞彩编号仍然是竞彩编号（不是被 HTML 元素 id 冒名顶替）。
    observation = extraction.provenance_observations[0]
    assert observation.competition_number_normalized == "1"
    assert observation.raw_provider_id_type == RawIdentifierType.HTML_ELEMENT_ID.value


# ---------------------------------------------------------------------------
# PE-07 / PE-08 / PE-09 / PE-10 / PE-11
# ---------------------------------------------------------------------------

def test_pe_07_live_football_evidence_extraction():
    """PE-07: live football 原始证据抽取（含真实解析器一致性）。"""
    html = _live_football_html()
    extraction = extract_source_evidence(html, adapter="live_football")
    expected_rows, expected_errors = fetcher_500._parse_live(html, "a", "football")

    assert expected_errors == 0
    assert list(extraction.normalized_rows) == expected_rows
    assert extraction.sidecar.parser_identity == "utils.fetcher_500._parse_live@1"
    assert extraction.sidecar.adapter == "live_football"
    assert extraction.sidecar.evidence_schema_version == PROVIDER_EVIDENCE_SCHEMA_VERSION
    assert len(extraction.sidecar.records) == len(expected_rows)

    observation = extraction.provenance_observations[0]
    assert observation.adapter == "live_football"
    assert observation.source_namespace == "500l"
    assert observation.raw_provider_id == "a1001"
    assert observation.identifier_stability == (
        IdentifierStability.COMPETITION_NUMBER_SCOPED.value
    )


def test_pe_08_live_basketball_evidence_extraction():
    """PE-08: live basketball 原始证据抽取（provider 比赛号 + 竞彩编号分别保留）。"""
    html = _live_basketball_html()
    extraction = extract_source_evidence(html, adapter="live_basketball")
    expected_rows, expected_errors = fetcher_500._parse_lq(html)

    assert expected_errors == 0
    assert list(extraction.normalized_rows) == expected_rows
    assert extraction.sidecar.parser_identity == "utils.fetcher_500._parse_lq@1"

    record = extraction.sidecar.records[0]
    assert record.raw_provider_id == "2001"          # matchList[_LQ_ID]
    assert record.competition_number_raw == "301"    # matchList[_LQ_NO]
    assert record.raw_provider_id_type == RawIdentifierType.PROVIDER_MATCH_ID.value
    assert record.provider_id_uniqueness_scope == ProviderIdUniquenessScope.UNKNOWN.value

    observation = extraction.provenance_observations[0]
    assert observation.sport == "basketball"
    assert observation.source_namespace == "500lq"
    assert observation.competition_number_normalized == "301"
    assert observation.raw_provider_id == "2001"


def test_pe_09_market_xml_evidence_extraction(monkeypatch):
    """PE-09: market XML 原始证据抽取，且与既有 fetch_jczq_xml 输出逐字一致。"""
    fetcher_500._CACHE.clear()
    monkeypatch.setattr(fetcher_500, "_fetch_xml", lambda lot, play: _MARKET_XML)
    try:
        baseline_rows = fetcher_500.fetch_jczq_xml("football")
    finally:
        fetcher_500._CACHE.clear()

    extraction = extract_source_evidence(
        {"spf": _MARKET_XML, "nspf": _MARKET_XML}, adapter="market_xml", sport="football"
    )

    assert list(extraction.normalized_rows) == baseline_rows
    assert extraction.sidecar.parser_identity == "utils.fetcher_500.fetch_jczq_xml@1"

    record = extraction.sidecar.records[0]
    assert record.raw_provider_id == "999"           # XML <m id>
    assert record.competition_number_raw == "007"    # XML <m matchnum>
    assert record.raw_provider_id_type == RawIdentifierType.PROVIDER_MATCH_ID.value
    assert record.provider_id_uniqueness_scope == ProviderIdUniquenessScope.UNKNOWN.value

    observation = extraction.provenance_observations[0]
    assert observation.source_namespace == "500j"
    assert observation.competition_number_normalized == "7"
    assert observation.raw_provider_id == "999"

    # 单个 XML 字符串（非映射）同样被接受。
    single = extract_source_evidence(_MARKET_XML, adapter="market_xml", sport="football")
    assert list(single.normalized_rows) == baseline_rows


def test_pe_10_finished_feed_evidence_classification():
    """PE-10: finished feed 的原始证据分类（HTML 元素 id 只按未验证记录）。"""
    html = _finished_html([("15:30", "甲队", "乙队", "2 - 1")])
    extraction = extract_source_evidence(html, adapter="finished_feed")
    expected_rows = fetcher_500._parse_wanchang(html)

    assert list(extraction.normalized_rows) == expected_rows
    record = extraction.sidecar.records[0]

    # 行位置派生 -> 永远不稳定。
    observation = extraction.provenance_observations[0]
    assert observation.identifier_stability == (
        IdentifierStability.ROW_POSITION_UNSTABLE.value
    )
    assert observation.raw_provider_id_type == RawIdentifierType.HTML_ELEMENT_ID.value
    assert observation.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )
    # HTML 元素 id 被记录，但绝不假定为稳定比赛标识。
    assert record.html_element_id == "w0"
    assert record.competition_number_raw is None
    assert any("行位置" in note for note in record.notes)
    assert ScopedSourceIdentityKey.for_observation(observation).is_sufficient is False


def test_pe_11_html_fallback_position_derived_id_is_unstable():
    """PE-11: HTML 兜底路径的行位置派生 ID 被分类为不稳定。"""
    html = _html_fallback_html()
    extraction = extract_source_evidence(html, adapter="html_fallback")
    expected_rows = fetcher_500._parse_jczq_html(html)

    assert list(extraction.normalized_rows) == expected_rows
    observation = extraction.provenance_observations[0]
    record = extraction.sidecar.records[0]

    assert observation.identifier_stability == (
        IdentifierStability.ROW_POSITION_UNSTABLE.value
    )
    assert observation.is_row_position_unstable is True
    assert observation.source_namespace == "500j"   # 与 market_xml 共用前缀
    assert record.row_ordinal == 0

    # data-mid 若存在：按未验证的 provider 匹配号记录，绝不用于活动绑定。
    assert record.raw_provider_id == "555"
    assert record.raw_provider_id_type == RawIdentifierType.PROVIDER_MATCH_ID.value
    assert observation.raw_provider_id_type == RawIdentifierType.PROVIDER_MATCH_ID.value
    assert observation.provider_id_uniqueness_scope == (
        ProviderIdUniquenessScope.UNKNOWN.value
    )
    assert ScopedSourceIdentityKey.for_observation(observation).is_sufficient is False

    # 没有 data-mid 时保持缺失，原始类型回落到行位置分类。
    without = extract_source_evidence(
        _html_fallback_html(data_mid=None), adapter="html_fallback"
    )
    assert without.sidecar.records[0].raw_provider_id is None
    assert without.provenance_observations[0].raw_provider_id is None
    assert without.provenance_observations[0].raw_provider_id_type == (
        RawIdentifierType.ROW_INDEX.value
    )
    assert ScopedSourceIdentityKey.for_observation(
        without.provenance_observations[0]
    ).is_sufficient is False


# ---------------------------------------------------------------------------
# PE-12
# ---------------------------------------------------------------------------

def test_pe_12_existing_parser_return_shapes_are_unchanged():
    """PE-12: 既有解析器返回形状不变（抽取路径不修改任何生产解析行为）。"""
    live_html = _live_football_html()
    lq_html = _live_basketball_html()
    wan_html = _finished_html([("15:30", "甲队", "乙队", "2 - 1")])
    fb_html = _html_fallback_html()

    baseline = {
        "live_football": fetcher_500._parse_live(live_html, "a", "football")[0],
        "live_basketball": fetcher_500._parse_lq(lq_html)[0],
        "finished_feed": fetcher_500._parse_wanchang(wan_html),
        "html_fallback": fetcher_500._parse_jczq_html(fb_html),
    }

    for adapter, fixture in (
        ("live_football", live_html),
        ("live_basketball", lq_html),
        ("finished_feed", wan_html),
        ("html_fallback", fb_html),
    ):
        rows, _observations = extract_rows_and_observations(fixture, adapter=adapter)
        assert rows == baseline[adapter], adapter
        # 返回的仍然是普通 dict 列表（形状不变）。
        for row in rows:
            assert isinstance(row, dict)
            assert set(row) == set(baseline[adapter][0]), adapter

    # 抽取模块从不被生产解析器引用。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fetcher_source = open(
        os.path.join(root, "utils", "fetcher_500.py"), encoding="utf-8"
    ).read()
    assert "provider_evidence" not in fetcher_source
    scraper_source = open(
        os.path.join(root, "utils", "scraper.py"), encoding="utf-8"
    ).read()
    assert "provider_evidence" not in scraper_source
    assert "source_id_migration" not in scraper_source


# ---------------------------------------------------------------------------
# PE-13
# ---------------------------------------------------------------------------

def test_pe_13_no_new_runtime_canonical_metadata_fields():
    """PE-13: 抽取路径不向运行期比赛 schema 注入任何新字段。"""
    html = _live_football_html()
    rows, observations = extract_rows_and_observations(html, adapter="live_football")
    expected_rows, _ = fetcher_500._parse_live(html, "a", "football")

    assert set(rows[0]) == set(expected_rows[0])
    forbidden = {"provenance", "evidence", "raw_provider_id", "sidecar", "observation_id"}
    assert forbidden.isdisjoint(set(rows[0]))
    for key in rows[0]:
        assert not key.startswith("_evidence")

    # 证据只存在于独立的观测对象里。
    assert observations[0].raw_provider_id == "a1001"
    assert "raw_provider_id" not in rows[0]


# ---------------------------------------------------------------------------
# PE-14
# ---------------------------------------------------------------------------

def test_pe_14_evidence_fingerprints_are_deterministic():
    """PE-14: 证据指纹确定（同输入同指纹，异输入异指纹，批次指纹与顺序无关）。"""
    html = _live_football_html()
    first = extract_source_evidence(html, adapter="live_football")
    second = extract_source_evidence(html, adapter="live_football")

    assert first.sidecar.fingerprint() == second.sidecar.fingerprint()
    assert first.sidecar.as_dict() == second.sidecar.as_dict()
    assert [o.observation_id for o in first.provenance_observations] == [
        o.observation_id for o in second.provenance_observations
    ]
    assert first.as_dict()["sidecar_fingerprint"] == second.as_dict()["sidecar_fingerprint"]
    assert re.fullmatch(r"[0-9a-f]{64}", first.sidecar.fingerprint())

    other = extract_source_evidence(
        _live_football_html(order="002"), adapter="live_football"
    )
    assert other.sidecar.fingerprint() != first.sidecar.fingerprint()

    # 批次语义指纹与观测顺序无关。
    observations = list(first.provenance_observations)
    assert batch_semantic_fingerprint(observations) == batch_semantic_fingerprint(
        list(reversed(observations))
    )


# ---------------------------------------------------------------------------
# PE-15
# ---------------------------------------------------------------------------

def test_pe_15_raw_evidence_is_not_modified_by_normalization():
    """PE-15: 规范化绝不改写原始证据（夹具与解析出的比赛行都保持不变）。"""
    html = _live_football_html()
    html_before = str(html)

    rows, _ = extract_rows_and_observations(html, adapter="live_football")
    rows_before = copy.deepcopy(rows)

    extraction = extract_source_evidence(html, adapter="live_football")
    assert html == html_before
    assert rows == rows_before
    assert extraction.normalized_rows == tuple(rows_before)
    # 抽取出的规范化行与原始行是**独立**的副本。
    assert extraction.normalized_rows[0] is not rows_before[0]

    # 观测层同样是只读的。
    row = _provider_row()
    row_before = copy.deepcopy(row)
    observe_source_row(row, adapter="market_xml", raw_provider_id="12345")
    assert row == row_before

    # 原始值与规范化值并存（规范化不取代原始证据）。
    record = extraction.sidecar.records[0]
    assert record.competition_number_raw == "001"
    assert extraction.provenance_observations[0].competition_number_normalized == "1"


# ---------------------------------------------------------------------------
# PE-16
# ---------------------------------------------------------------------------

def test_pe_16_legacy_identity_key_approvals_cannot_be_silently_reused(tmp_path):
    """PE-16: 身份键契约变更后，旧契约批准不得被静默复用。"""
    assert SOURCE_IDENTITY_KEY_VERSION == IDENTITY_KEY_SCHEMA_VERSION == "3.0.0"
    assert SUPERSEDED_IDENTITY_KEY_SCHEMA_VERSION == "2.0.0"
    assert LEGACY_IDENTITY_KEY_SCHEMA_VERSION == "1.0.0"

    # 旧 2.0.0 键：其 provider 取值不含 sport/date/作用域 -> 含义已经改变。
    legacy_2_key = json.dumps(
        {
            "identity_key_schema_version": "2.0.0",
            "identity_kind": "RAW_PROVIDER_ID",
            "identity_value": "market_xml|500j|PROVIDER_MATCH_ID|12345",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert classify_binding_key(legacy_2_key) == "LEGACY_VERSION"

    current_key = ScopedSourceIdentityKey.for_observation(_observed()).source_key()
    assert classify_binding_key(current_key) == "CURRENT"

    # 旧契约提案无法被写入账本。
    observation = _observed()
    legacy_proposal = build_proposal(
        source_observation_ref=observation.observation_id,
        source_identity_scope=observation.stability_scope.as_dict(),
        scoped_source_identity_key={
            "identity_key_schema_version": "2.0.0",
            "identity_kind": "RAW_PROVIDER_ID",
            "identity_value": "market_xml|500j|PROVIDER_MATCH_ID|12345",
        },
        candidate_canonical_id="canon-x",
        existing_canonical_owner="canon-x",
        identity_evidence_type="provider_match_id",
        observation_fingerprints=[observation.semantic_fingerprint()],
        ingestion_plan_reference="planner:ACCEPTED",
        reason="legacy contract attempt",
        expected_baseline_fingerprint="baseline-A",
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
    )
    ledger = _ledger(tmp_path)
    with pytest.raises(LegacyBindingKeyError):
        ledger.append(
            proposal=legacy_proposal,
            to_state=MappingState.PROPOSED.value,
            actor="operator-1",
            actor_role="operator",
            reason="legacy contract must never be silently reused",
            baseline_fingerprint="baseline-A",
        )
    assert ledger.exists() is False

    # 旧批准契约（approval schema）同样被拒绝。
    fresh = _binding_proposal(_observed(), "canon-x", "baseline-A")
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            {"approval_schema_version": "1.0.0", "purpose": "CANONICAL_MAPPING_GOVERNANCE_APPROVE"},
            proposal=fresh,
            source_observation_fingerprint="x",
            dataset_identity=DATASET,
            current_baseline_fingerprint="baseline-A",
            trusted_public_key_path="does-not-matter.pem",
        )


# ---------------------------------------------------------------------------
# PE-17
# ---------------------------------------------------------------------------

def test_pe_17_independent_historical_approval_audit_continues_to_pass(tmp_path, owner_keys):
    """PE-17: 独立历史批准审计在身份键契约变更后依然通过。"""
    observation = observe_source_row(
        _provider_row(number="007"), adapter="live_football"
    )
    proposal = _binding_proposal(observation, "canon-keep", "baseline-A")
    assert proposal.identity_key_dict["identity_key_schema_version"] == "3.0.0"
    assert proposal.identity_key_dict["provider_id_uniqueness_scope"] == (
        ProviderIdUniquenessScope.NOT_APPLICABLE.value
    )

    ledger = _ledger(tmp_path)
    ledger.append(
        proposal=proposal,
        to_state=MappingState.PROPOSED.value,
        actor="operator-1",
        actor_role="operator",
        reason="propose binding",
        baseline_fingerprint="baseline-A",
    )
    entry, appended = _approve(
        ledger, proposal, observation, owner_keys, baseline="baseline-A"
    )
    assert appended is True and entry.to_state == MappingState.APPROVED.value

    audit = audit_historical_approvals(
        ledger, trusted_public_key_path=owner_keys["public"], dataset_identity=DATASET
    )
    assert audit["verification_category"] == "AUTHENTICATED_MAPPING_APPROVAL_AUDIT"
    assert audit["approved_decisions"] == 1
    assert audit["failures"] == []
    assert audit["identity_key_schema_version"] == "3.0.0"

    eligibility = mapping_eligibility(ledger, current_baseline_fingerprint="baseline-A")
    assert eligibility["eligible_count"] == 1
    assert eligibility["stale_count"] == 0

    # 账本结构与绑定键契约分布仍然健康。
    structural = ledger.verify()
    assert structural["integrity"] == "OK"
    assert structural["binding_key_contracts"] == {"CURRENT": 2}
    assert structural["incomplete_approved_entries"] == []


# ---------------------------------------------------------------------------
# PE-18
# ---------------------------------------------------------------------------

def test_pe_18_conflicting_scoped_source_bindings_are_rejected(tmp_path, owner_keys):
    """PE-18: 同一作用域源身份的两个冲突活动绑定被拒绝。"""
    observation_a = observe_source_row(
        _provider_row(number="011", home="皇马", away="巴萨"), adapter="live_football"
    )
    observation_b = observe_source_row(
        _provider_row(number="011", home="皇马", away="巴萨"), adapter="market_xml"
    )
    key_a = ScopedSourceIdentityKey.for_observation(observation_a)
    key_b = ScopedSourceIdentityKey.for_observation(observation_b)
    # 两者都是竞彩编号作用域身份：同 sport/date/编号 -> 同一绑定身份。
    assert key_a.source_key() == key_b.source_key()
    assert key_a.is_sufficient and key_b.is_sufficient

    proposal_a = _binding_proposal(observation_a, "canon-A", "baseline-A")
    proposal_b = _binding_proposal(observation_b, "canon-B", "baseline-A")
    assert proposal_a.source_key == proposal_b.source_key

    ledger = _ledger(tmp_path)
    ledger.append(
        proposal=proposal_a,
        to_state=MappingState.PROPOSED.value,
        actor="operator-1",
        actor_role="operator",
        reason="propose binding A",
        baseline_fingerprint="baseline-A",
    )
    _approve(ledger, proposal_a, observation_a, owner_keys, baseline="baseline-A")

    # 第二个提案先显式提案，再尝试活动绑定 -> 冲突绑定必须被拒绝。
    ledger.append(
        proposal=proposal_b,
        to_state=MappingState.PROPOSED.value,
        actor="operator-1",
        actor_role="operator",
        reason="propose binding B",
        baseline_fingerprint="baseline-A",
    )
    body_b = build_approval_body(
        proposal=proposal_b,
        source_observation_fingerprint=observation_b.semantic_fingerprint(),
        dataset_identity=DATASET,
        operator="operator-1",
        approver="owner-1",
        approved_at="2030-01-01T00:00:00+00:00",
        reason="owner approves the conflicting binding",
    )
    approval_b = sign_mapping_approval(owner_keys["private"], body_b)

    with pytest.raises(ConflictingBindingError):
        ledger.append(
            proposal=proposal_b,
            to_state=MappingState.APPROVED.value,
            actor="owner-1",
            actor_role="owner",
            reason="conflicting binding attempt",
            baseline_fingerprint="baseline-A",
            approval=approval_b,
            trusted_public_key_path=owner_keys["public"],
            source_observation_fingerprint=observation_b.semantic_fingerprint(),
        )

    # 活动绑定视图仍然只有第一条。
    active = ledger.active_bindings()
    assert len(active) == 1
    assert list(active.values())[0]["canonical_id"] == "canon-A"
    assert list(active.keys())[0] == proposal_a.source_key


# ---------------------------------------------------------------------------
# 补充不变量：清单完备性 / 溯源 schema 版本
# ---------------------------------------------------------------------------

def test_provider_evidence_inventory_covers_every_adapter():
    """清单必须覆盖全部五个适配器，且每个字段都被显式填写。"""
    adapters = {entry.adapter for entry in PROVIDER_EVIDENCE_INVENTORY}
    assert adapters == set(PROVIDER_EVIDENCE_ADAPTERS)
    for adapter in PROVIDER_EVIDENCE_ADAPTERS:
        entry = inventory_for_adapter(adapter)
        assert entry is not None
        for field in (
            "parser",
            "input_field_or_attribute",
            "observed_meaning",
            "evidence_availability",
            "stability_limitation",
            "current_data_loss",
            "safe_preservation_mechanism",
        ):
            assert getattr(entry, field), (adapter, field)

    assert PROVENANCE_SCHEMA_VERSION == "2.0.0"
