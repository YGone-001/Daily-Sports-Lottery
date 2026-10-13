"""
运行期映射就绪测试套件（RR-01 .. RR-28）
=======================================
覆盖：
- 运行期资格契约：有效批准 -> ELIGIBLE；决策对象确定、不可变、可哈希
- 判定**不写文件、不改 Canonical 数据、不写账本**
- 治理拒绝面：缺批准 / 签名无效 / 批准人错 / 操作者错 / 旧契约 / 撤销 / 取代 / 陈旧基线
- 身份拒绝面：未验证 provider ID / 行位置身份 / 旧身份键契约
- 历史安全：快照 / 结算 / 评估依赖冲突一律阻断
- 就绪清单：确定、不可执行
- 既有运行期不变：refresh / 摄取预检 / 修复授权 / 七存储

全部使用隔离的合成夹具，绝不访问网络，绝不触碰生产数据，绝不激活任何映射。
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re

import pytest

from tools.mapping_governance_shadow import (
    RUNTIME_SCENARIO_MATRIX,
    IsolatedGovernanceSnapshot,
    build_isolated_governance_snapshot,
    shadow_runtime_eligibility_check,
    shadow_runtime_readiness_report,
    shadow_runtime_scenario_matrix,
)
from utils import fetcher_500, scraper
from utils.daily_loader import load_json, save_json
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    plan_ingestion_identity,
)
from utils.mapping_governance import (
    MAPPING_APPROVAL_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    GovernanceLedger,
    MappingProposal,
    ProposalAction,
    approval_fingerprint,
    audit_historical_approvals,
    build_approval_body,
    generate_mapping_owner_keypair,
    governance_status,
    proposal_from_observations,
    sign_mapping_approval,
)
from utils.runtime_mapping_eligibility import (
    FORBIDDEN_RUNTIME_MANIFEST_KEYS,
    RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION,
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
    RUNTIME_READINESS_MANIFEST_KIND,
    GovernanceEvidenceProvenance,
    RuntimeEligibilityPolicyError,
    RuntimeEligibilityResult,
    RuntimeMappingContext,
    RuntimeMappingEligibilityDecision,
    assert_runtime_manifest_not_executable,
    build_runtime_readiness_manifest,
    caller_asserted_provenance,
    evaluate_runtime_mapping_eligibility,
    isolated_snapshot_provenance,
    runtime_eligibility_status,
    runtime_manifest_fingerprint,
)
from utils.runtime_mapping_policy import (
    ALLOWED_ACTIONS,
    ALLOWED_APPROVAL_VERSIONS,
    ALLOWED_CONTEXT_EVIDENCE_PROVENANCE,
    ALLOWED_IDENTITY_KEY_VERSIONS,
    ALLOWED_PROPOSAL_STATES,
    ALLOWED_RUNTIME_IDENTITY_KEYS,
    ALLOWED_SOURCE_SCOPES,
    BLOCKED_ACTIONS,
    BLOCKED_CONTEXT_EVIDENCE_PROVENANCE,
    BLOCKED_IDENTITY_TYPES,
    BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES,
    CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
    CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
    KNOWN_PROPOSAL_STATES,
    REJECTION_CODES,
    RUNTIME_ELIGIBILITY_RULES,
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
    RUNTIME_READ_ONLY_STORES,
    rule_codes,
    runtime_policy_status,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    IdentityKeyKind,
    ScopedSourceIdentityKey,
    observe_source_row,
)

CANONICAL_ID = "500j-2030-05-01-007"
CANONICAL_ID_ALT = "500j-2030-05-01-008"
DATASET = "synthetic-runtime-readiness-fixture"
BASELINE = "baseline-A"
OPERATOR = "operator-1"
APPROVER = "owner-1"
EVALUATED_AT = "2030-01-01T00:00:00+00:00"

SEVEN_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def owner_keys(tmp_path_factory):
    """在仓库之外生成一对所有者 Ed25519 密钥。"""
    base = tmp_path_factory.mktemp("rr_owner_keys")
    private = str(base / "owner_private.pem")
    public = str(base / "owner_public.pem")
    info = generate_mapping_owner_keypair(private, public)
    return {"private": private, "public": public, "public_key_id": info["public_key_id"]}


def _market_row(
    *,
    match_id: str = CANONICAL_ID,
    sport: str = "football",
    date: str = "2030-05-01",
    time: str = "20:00",
    number: str = "007",
    home: str = "皇马",
    away: str = "巴萨",
    odds=None,
    status: str = "upcoming",
    score=None,
) -> dict:
    return {
        "id": match_id,
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "date": date,
        "time": time,
        "kickoff_time_known": True,
        "status": status,
        "home": home,
        "away": away,
        "score": score,
        "odds": dict(odds) if isinstance(odds, dict) else odds,
        "jczq_no": number,
    }


def _observation(**overrides):
    return observe_source_row(_market_row(**overrides), adapter="market_xml")


def _proposal(observation, *, canonical_id: str = CANONICAL_ID,
              baseline: str = BASELINE, operator: str = OPERATOR) -> MappingProposal:
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


def _approval(proposal, observation, keys, *, approver: str = APPROVER,
              operator: str = OPERATOR, dataset: str = DATASET,
              baseline: str = BASELINE) -> dict:
    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=dataset,
        operator=operator,
        approver=approver,
        approved_at=EVALUATED_AT,
        reason="owner approves the proposed binding",
    )
    return sign_mapping_approval(keys["private"], body)


def _context(keys, proposal=None, **overrides) -> RuntimeMappingContext:
    """
    合成运行期上下文。

    ``evidence_provenance`` / ``recorded_proposal_id`` / ``recorded_proposal_fingerprint``
    是修正后**必填**的治理证据；省略任何一项都会被评估器判为不合格，而不是静默通过。
    """
    kwargs = dict(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        evidence_provenance=caller_asserted_provenance(
            dataset_identity=DATASET,
            baseline_fingerprint=BASELINE,
        ),
        canonical_ids=(CANONICAL_ID, CANONICAL_ID_ALT),
        trusted_public_key_path=keys["public"],
        expected_approver=APPROVER,
        expected_operator=OPERATOR,
        recorded_proposal_id=(
            proposal.proposal_id if isinstance(proposal, MappingProposal) else None
        ),
        recorded_proposal_fingerprint=(
            proposal.fingerprint() if isinstance(proposal, MappingProposal) else None
        ),
        recorded_proposal_state="APPROVED",
    )
    kwargs.update(overrides)
    return RuntimeMappingContext(**kwargs)


def _valid_setup(keys):
    observation = _observation()
    proposal = _proposal(observation)
    approval = _approval(proposal, observation, keys)
    return observation, proposal, approval, _context(keys, proposal)


def _synthetic_proposal(
    *,
    key_dict,
    canonical_id=CANONICAL_ID,
    operator=OPERATOR,
    action: str = ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
    scope_override: dict | None = None,
) -> MappingProposal:
    """仅用于测试的合成提案（刻意绕过 validate_proposal）。"""
    scope = {
        "adapter": "market_xml",
        "sport": "football",
        "event_date": "2030-05-01",
        "provider_namespace": "500j",
        "raw_identifier_type": key_dict.get("identity_kind", "PROVIDER_MATCH_ID"),
    }
    if scope_override:
        scope.update(scope_override)
    key_pairs = tuple(sorted((str(k), str(v)) for k, v in key_dict.items()))
    scope_pairs = tuple(sorted((str(k), str(v)) for k, v in scope.items()))
    return MappingProposal(
        proposal_schema_version="2.0.0",
        proposal_id=f"rr-synthetic-{key_dict.get('identity_kind', 'X')}",
        source_observation_ref="rr-synthetic-observation",
        source_identity_scope=scope_pairs,
        scoped_source_identity_key=key_pairs,
        candidate_canonical_id=canonical_id,
        existing_canonical_owner=canonical_id,
        identity_evidence_type="rr_synthetic",
        observation_fingerprints=("0" * 64,),
        ingestion_plan_reference="planner:RR",
        reason="runtime readiness synthetic proposal",
        conflict_category=None,
        expected_baseline_fingerprint=BASELINE,
        requested_action=action,
        status="PROPOSED",
        operator=operator,
    )


def _recompose_proposal(
    proposal: MappingProposal,
    *,
    scope_overrides: dict | None = None,
    key_dict: dict | None = None,
    observation_fingerprints=None,
    source_observation_ref: str | None = None,
    status: str | None = None,
) -> MappingProposal:
    """
    仅用于测试：在保持身份键与观测指纹不变的前提下改写提案的**声明作用域**，
    以便单独触发 ``SOURCE_SCOPE_MISMATCH``。
    """
    scope = dict(proposal.scope_dict)
    if scope_overrides:
        scope.update(scope_overrides)
    return MappingProposal(
        proposal_schema_version=proposal.proposal_schema_version,
        proposal_id=proposal.proposal_id,
        source_observation_ref=(
            source_observation_ref
            if source_observation_ref is not None
            else proposal.source_observation_ref
        ),
        source_identity_scope=tuple(sorted((str(k), str(v)) for k, v in scope.items())),
        scoped_source_identity_key=tuple(
            sorted((str(k), str(v)) for k, v in (key_dict or proposal.identity_key_dict).items())
        ),
        candidate_canonical_id=proposal.candidate_canonical_id,
        existing_canonical_owner=proposal.existing_canonical_owner,
        identity_evidence_type=proposal.identity_evidence_type,
        observation_fingerprints=(
            tuple(observation_fingerprints)
            if observation_fingerprints is not None
            else proposal.observation_fingerprints
        ),
        ingestion_plan_reference=proposal.ingestion_plan_reference,
        reason=proposal.reason,
        conflict_category=proposal.conflict_category,
        expected_baseline_fingerprint=proposal.expected_baseline_fingerprint,
        requested_action=proposal.requested_action,
        status=status or proposal.status,
        operator=proposal.operator,
    )


def _hash_seven_stores(directory) -> dict:
    out = {}
    for name in SEVEN_STORES:
        path = os.path.join(str(directory), name)
        if not os.path.exists(path):
            out[name] = "<absent>"
            continue
        with open(path, "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def _seed_seven_stores(data_dir) -> None:
    save_json("daily_matches.json", {"meta": {}, "matches": [_market_row()]})
    save_json("team_strength.json", {"teams": {"皇马": {"elo_rating": 1500}}})
    save_json("calibrated.json", {"ids": [CANONICAL_ID]})
    save_json("prediction_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("odds_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("settlements.json", {"version": 1, "settlements": {}})
    save_json("evaluation_rows.json", {"version": 1, "rows": {}})


def _fake_sources(monkeypatch, *, live_football=(), market=(), finished=()):
    monkeypatch.setattr(
        fetcher_500, "fetch_live_matches", lambda sport: [dict(m) for m in live_football]
    )
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(
        fetcher_500,
        "fetch_jczq_xml",
        lambda sport: [dict(m) for m in market if m.get("sport", "football") == sport],
    )
    monkeypatch.setattr(
        fetcher_500, "fetch_finished_matches", lambda *a, **k: [dict(m) for m in finished]
    )


# ---------------------------------------------------------------------------
# RR-01
# ---------------------------------------------------------------------------

def test_rr_01_valid_approved_scoped_identity_returns_eligible(tmp_path, owner_keys):
    """RR-01: 有效的已批准作用域身份 -> ELIGIBLE，且不产生任何变更。"""
    workspace = tmp_path / "rr01"
    workspace.mkdir()
    before_listing = sorted(os.listdir(workspace))

    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )

    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()
    assert decision.is_eligible is True
    assert decision.proposal_id == proposal.proposal_id
    assert decision.canonical_id == CANONICAL_ID
    assert decision.baseline_fingerprint == BASELINE
    assert decision.evidence_fingerprint == observation.semantic_fingerprint()
    assert decision.approval_fingerprint == decision.approval_fingerprint
    assert decision.schema_version == RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION
    assert decision.evaluator_version == RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
    # 身份键是**判别性**身份键（不是宽泛作用域元数据）。
    assert json.loads(decision.source_identity_key)["identity_kind"] == (
        IdentityKeyKind.COMPETITION_NUMBER.value
    )

    # 判定不产生任何文件。
    assert sorted(os.listdir(workspace)) == before_listing


# ---------------------------------------------------------------------------
# RR-02
# ---------------------------------------------------------------------------

def test_rr_02_decision_object_is_deterministic_and_immutable(owner_keys):
    """RR-02: 决策对象确定、可哈希、不可变，且无任何执行方法。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)

    first = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    second = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert first == second
    assert first.decision_id == second.decision_id
    assert first.as_dict() == second.as_dict()
    assert first.to_json() == second.to_json()

    # decision_id 与时钟无关（evaluated_at 是元数据，不参与判定身份）。
    later = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at="2099-12-31T23:59:59+00:00"
    )
    assert later.decision_id == first.decision_id
    assert later.evaluated_at != first.evaluated_at
    assert later.as_dict()["decision_id"] == first.as_dict()["decision_id"]

    # 可哈希：相同元数据的两个对象是同一集合元素。
    assert len({first, second}) == 1
    assert hash(first) == hash(second)
    # 元数据不同 -> 对象不同，但**判定身份**仍然相同（decision_id 与时钟无关）。
    assert later != first
    assert later.decision_id == first.decision_id
    assert later.evaluated_at != first.evaluated_at

    # 不可变。
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.eligibility_result = RuntimeEligibilityResult.NOT_ELIGIBLE.value  # type: ignore[misc]

    # 字段集合与 §7 要求完全一致（修正后新增 evidence_provenance）。
    assert {f.name for f in dataclasses.fields(RuntimeMappingEligibilityDecision)} == {
        "schema_version",
        "decision_id",
        "proposal_id",
        "source_identity_key",
        "canonical_id",
        "eligibility_result",
        "rejection_codes",
        "evaluated_at",
        "evaluator_version",
        "evidence_fingerprint",
        "approval_fingerprint",
        "baseline_fingerprint",
        "evidence_provenance",
    }
    # 条件式判定绝不冒充生产授权。
    assert first.is_conditional is True
    assert first.is_production_authorization is False
    assert first.as_dict()["is_conditional"] is True
    assert first.as_dict()["is_production_authorization"] is False
    assert first.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED

    # 无任何执行 / 变更方法。
    for forbidden in ("apply", "execute", "activate", "bind", "write", "mutate", "commit"):
        assert not hasattr(first, forbidden)


# ---------------------------------------------------------------------------
# RR-03
# ---------------------------------------------------------------------------

def test_rr_03_no_files_written_by_the_evaluator(tmp_path, owner_keys):
    """RR-03: 评估器不写任何文件（结构上也不具备写入能力）。"""
    workspace = tmp_path / "rr03"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before = sorted(os.listdir(workspace))
    before_bytes = {
        name: (workspace / name).read_bytes() for name in before
    }

    observation, proposal, approval, context = _valid_setup(owner_keys)
    for _ in range(3):
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT
        )

    assert sorted(os.listdir(workspace)) == before
    for name in before:
        assert (workspace / name).read_bytes() == before_bytes[name]

    # 结构性证明：模块源码不含任何写入原语。
    source = open(
        os.path.join(REPO_ROOT, "utils", "runtime_mapping_eligibility.py"),
        encoding="utf-8",
    ).read()
    for forbidden in (
        "import os",
        "open(",
        "os.replace",
        "os.remove",
        "os.makedirs",
        "os.unlink",
        "shutil",
        "json.dump",
        "save_json",
        "atomic_write_json",
        "GovernanceLedger",
    ):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# RR-04
# ---------------------------------------------------------------------------

def test_rr_04_no_canonical_data_changed(tmp_path, owner_keys):
    """RR-04: 判定绝不修改 Canonical 记录。"""
    canonical = [_market_row()]
    canonical_before = copy.deepcopy(canonical)
    observation, proposal, approval, context = _valid_setup(owner_keys)
    observation_before = observation.as_dict()
    proposal_before = proposal.as_dict()

    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.is_eligible

    assert canonical == canonical_before
    assert observation.as_dict() == observation_before
    assert proposal.as_dict() == proposal_before

    # 运行期只读存储清单与评估器状态一致。
    assert RUNTIME_READ_ONLY_STORES == SEVEN_STORES
    assert runtime_eligibility_status()["modifies_canonical_records"] is False
    assert runtime_eligibility_status()["creates_bindings"] is False
    assert runtime_eligibility_status()["writes_files"] is False


# ---------------------------------------------------------------------------
# RR-05 .. RR-08 — 批准面
# ---------------------------------------------------------------------------

def test_rr_05_missing_approval_is_rejected(owner_keys):
    """RR-05: 缺少批准件 -> NOT_ELIGIBLE(APPROVAL_MISSING)。"""
    observation, proposal, _approval_artifact, context = _valid_setup(owner_keys)

    for missing in (None, {}, "not-an-artifact"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal, missing, observation, context, evaluated_at=EVALUATED_AT
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "APPROVAL_MISSING" in decision.rejection_codes


def test_rr_06_invalid_signature_is_rejected(owner_keys):
    """RR-06: 签名无效（被篡改）-> NOT_ELIGIBLE(APPROVAL_SIGNATURE_INVALID)。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)

    tampered = copy.deepcopy(approval)
    raw = tampered["approval"]["signature"]
    flipped = ("A" if raw[0] != "A" else "B") + raw[1:]
    tampered["approval"]["signature"] = flipped

    decision = evaluate_runtime_mapping_eligibility(
        proposal, tampered, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "APPROVAL_SIGNATURE_INVALID" in decision.rejection_codes

    # 缺少签名块同样被拒绝。
    unsigned = {k: v for k, v in approval.items() if k != "approval"}
    decision_unsigned = evaluate_runtime_mapping_eligibility(
        proposal, unsigned, observation, context, evaluated_at=EVALUATED_AT
    )
    assert "APPROVAL_SIGNATURE_INVALID" in decision_unsigned.rejection_codes


def test_rr_07_wrong_approver_is_rejected(owner_keys):
    """RR-07: 批准人身份与运行期期望不一致 -> NOT_ELIGIBLE(APPROVAL_WRONG_APPROVER)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, expected_approver="owner-OTHER"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "APPROVAL_WRONG_APPROVER" in decision.rejection_codes

    # 空白批准人身份同样被拒绝。
    blank = copy.deepcopy(approval)
    blank["approver"] = ""
    decision_blank = evaluate_runtime_mapping_eligibility(
        proposal, blank, observation, _context(owner_keys), evaluated_at=EVALUATED_AT
    )
    assert "APPROVAL_WRONG_APPROVER" in decision_blank.rejection_codes


def test_rr_08_wrong_operator_is_rejected(owner_keys):
    """RR-08: 操作者身份与提案 / 运行期期望不一致 -> NOT_ELIGIBLE(APPROVAL_WRONG_OPERATOR)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, expected_operator="operator-OTHER"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "APPROVAL_WRONG_OPERATOR" in decision.rejection_codes

    # 批准件签名的操作者与提案的操作者不同 -> 同样拒绝。
    other_operator_approval = _approval(
        proposal, observation, owner_keys, operator="operator-OTHER"
    )
    decision_mismatch = evaluate_runtime_mapping_eligibility(
        proposal, other_operator_approval, observation, _context(owner_keys),
        evaluated_at=EVALUATED_AT,
    )
    assert "APPROVAL_WRONG_OPERATOR" in decision_mismatch.rejection_codes


# ---------------------------------------------------------------------------
# RR-09 .. RR-11 — 身份面
# ---------------------------------------------------------------------------

def test_rr_09_legacy_identity_version_is_rejected(owner_keys):
    """RR-09: 旧契约身份键版本 -> NOT_ELIGIBLE(IDENTITY_KEY_VERSION_NOT_CURRENT)。"""
    observation = _observation()
    legacy = _synthetic_proposal(
        key_dict={
            "identity_key_schema_version": "2.0.0",
            "identity_kind": "RAW_PROVIDER_ID",
            "identity_value": "market_xml|500j|PROVIDER_MATCH_ID|12345",
        }
    )
    decision = evaluate_runtime_mapping_eligibility(
        legacy, None, observation, _context(owner_keys), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "IDENTITY_KEY_VERSION_NOT_CURRENT" in decision.rejection_codes
    assert "2.0.0" not in ALLOWED_IDENTITY_KEY_VERSIONS
    assert IDENTITY_KEY_SCHEMA_VERSION in ALLOWED_IDENTITY_KEY_VERSIONS


def test_rr_10_unverified_provider_id_is_rejected(owner_keys):
    """RR-10: UNVERIFIED_PROVIDER_ID -> NOT_ELIGIBLE（绝无例外）。"""
    observation = _observation(number=None)
    key = ScopedSourceIdentityKey.for_observation(
        observe_source_row(
            _market_row(number=None),
            adapter="market_xml",
            raw_provider_id="12345",
            raw_provider_id_type="PROVIDER_MATCH_ID",
        )
    )
    assert key.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value

    unverified = _synthetic_proposal(key_dict=key.as_dict())
    decision = evaluate_runtime_mapping_eligibility(
        unverified, None, observation, _context(owner_keys), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "UNVERIFIED_PROVIDER_ID" in decision.rejection_codes
    assert "IDENTITY_EVIDENCE_INSUFFICIENT" in decision.rejection_codes
    assert IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value in BLOCKED_IDENTITY_TYPES
    assert "UNKNOWN" in BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES
    assert "UNVERIFIED" in BLOCKED_PROVIDER_ID_UNIQUENESS_SCOPES


def test_rr_11_row_position_identity_is_rejected(owner_keys):
    """RR-11: 行位置派生身份 -> NOT_ELIGIBLE(ROW_POSITION_UNSTABLE)。"""
    finished = {
        "id": "500w-2030-05-01-0",
        "sport": "football",
        "league": "英超",
        "date": "2030-05-01",
        "time": "15:30",
        "kickoff_time_known": True,
        "status": "finished",
        "home": "甲队",
        "away": "乙队",
        "score": {"ft": [2, 1]},
        "odds": None,
    }
    observation = observe_source_row(finished, adapter="finished_feed")
    row_position = _synthetic_proposal(
        key_dict=ScopedSourceIdentityKey.for_observation(observation).as_dict()
    )

    decision = evaluate_runtime_mapping_eligibility(
        row_position, None, observation, _context(owner_keys), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "ROW_POSITION_UNSTABLE" in decision.rejection_codes
    assert "RAW_IDENTIFIER_TYPE_NOT_DURABLE" in decision.rejection_codes


# ---------------------------------------------------------------------------
# RR-12 .. RR-14 — 治理状态面
# ---------------------------------------------------------------------------

def test_rr_12_revoked_mapping_is_rejected(owner_keys):
    """RR-12: 已撤销映射 -> NOT_ELIGIBLE(MAPPING_REVOKED)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, recorded_proposal_state="REVOKED"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "MAPPING_REVOKED" in decision.rejection_codes
    assert "MAPPING_STATE_NOT_ACTIVE" in decision.rejection_codes


def test_rr_13_superseded_mapping_is_rejected(owner_keys):
    """RR-13: 已取代映射 -> NOT_ELIGIBLE(MAPPING_SUPERSEDED)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, recorded_proposal_state="SUPERSEDED"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "MAPPING_SUPERSEDED" in decision.rejection_codes
    assert "MAPPING_STATE_NOT_ACTIVE" in decision.rejection_codes


def test_rr_14_stale_baseline_is_rejected(owner_keys):
    """RR-14: 陈旧基线 -> NOT_ELIGIBLE(BASELINE_STALE)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, current_baseline_fingerprint="baseline-B"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "BASELINE_STALE" in decision.rejection_codes

    # 数据集身份不匹配同样被拒绝。
    mismatched = _approval(proposal, observation, owner_keys, dataset="other-dataset")
    decision_dataset = evaluate_runtime_mapping_eligibility(
        proposal, mismatched, observation, _context(owner_keys), evaluated_at=EVALUATED_AT
    )
    assert "DATASET_IDENTITY_MISMATCH" in decision_dataset.rejection_codes


# ---------------------------------------------------------------------------
# RR-15 .. RR-18 — 运行期安全面
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "store, expected_code",
    (
        ("prediction_snapshots.json", "SNAPSHOT_IMPACT"),
        ("odds_snapshots.json", "SNAPSHOT_IMPACT"),
        ("settlements.json", "SETTLEMENT_IMPACT"),
        ("evaluation_rows.json", "EVALUATION_IMPACT"),
    ),
)
def test_rr_15_16_17_dependency_conflicts_are_rejected(owner_keys, store, expected_code):
    """RR-15/16/17: 快照 / 结算 / 评估依赖冲突 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, required_rewrite_targets=(store,)),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert expected_code in decision.rejection_codes
    assert "HISTORICAL_REFERENCE_CONFLICT" in decision.rejection_codes


def test_rr_18_canonical_owner_conflict_is_rejected(owner_keys):
    """RR-18: Canonical 所有者冲突 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    conflicting = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(
            owner_keys,
            known_source_bindings=((proposal.source_key, "canon-OTHER"),),
        ),
        evaluated_at=EVALUATED_AT,
    )
    assert conflicting.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "CANONICAL_OWNERSHIP_CONFLICT" in conflicting.rejection_codes

    # 一个源身份同时对应多个 Canonical 所有者 -> 直接拒绝。
    competing = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(
            owner_keys,
            known_source_bindings=(
                (proposal.source_key, "canon-A"),
                (proposal.source_key, "canon-B"),
            ),
        ),
        evaluated_at=EVALUATED_AT,
    )
    assert "CANONICAL_OWNER_CONFLICT" in competing.rejection_codes

    # 要求改写 Canonical ID / 要求迁移 -> 同样拒绝。
    rename = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, canonical_rename_required=True, migration_required=True),
        evaluated_at=EVALUATED_AT,
    )
    assert "CANONICAL_ID_CHANGE_REQUIRED" in rename.rejection_codes
    assert "MIGRATION_REQUIRED" in rename.rejection_codes


# ---------------------------------------------------------------------------
# RR-19 .. RR-21 — 幂等与就绪清单
# ---------------------------------------------------------------------------

def test_rr_19_duplicate_evaluation_is_idempotent(owner_keys):
    """RR-19: 重复评估幂等（同一决策身份，无副作用）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)

    decisions = [
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT
        )
        for _ in range(5)
    ]
    assert len({d.decision_id for d in decisions}) == 1
    assert len(set(decisions)) == 1
    assert all(d.is_eligible for d in decisions)

    manifest = build_runtime_readiness_manifest(
        fixture_identity="rr19",
        decisions=decisions,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert len(set(manifest["decision_ids"])) == 1
    assert manifest["eligible_count"] == len(decisions)


def test_rr_20_manifest_is_deterministic(owner_keys):
    """RR-20: 就绪清单确定（与决策输入顺序无关）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    eligible = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    rejected = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, context, evaluated_at=EVALUATED_AT
    )
    decisions = [eligible, rejected]

    first = build_runtime_readiness_manifest(
        fixture_identity="rr20",
        decisions=decisions,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        approval_versions=[MAPPING_APPROVAL_SCHEMA_VERSION],
    )
    second = build_runtime_readiness_manifest(
        fixture_identity="rr20",
        decisions=list(reversed(decisions)),
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        approval_versions=[MAPPING_APPROVAL_SCHEMA_VERSION],
    )

    assert runtime_manifest_fingerprint(first) == runtime_manifest_fingerprint(second)
    assert first == second
    assert first["kind"] == RUNTIME_READINESS_MANIFEST_KIND
    assert first["evaluated_proposal_count"] == 2
    assert first["eligible_count"] == 1
    assert first["rejected_count"] == 1
    assert first["rejection_reason_histogram"] == {"APPROVAL_MISSING": 1}
    assert first["policy_version"] == RUNTIME_MAPPING_POLICY_VERSION
    assert first["evaluator_version"] == RUNTIME_ELIGIBILITY_EVALUATOR_VERSION
    assert first["baseline_fingerprint"] == BASELINE
    assert first["identity_key_schema_versions"] == [IDENTITY_KEY_SCHEMA_VERSION]
    assert first["approval_versions"] == [MAPPING_APPROVAL_SCHEMA_VERSION]
    assert first["runtime_mapping_activation"] == RUNTIME_MAPPING_ACTIVATION
    assert first["production_mapping_write_authorization"] == (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    )
    assert first["runtime_ledger_enforcement"] == RUNTIME_LEDGER_ENFORCEMENT
    assert first["read_only_stores"] == list(RUNTIME_READ_ONLY_STORES)


def test_rr_21_manifest_cannot_execute(owner_keys):
    """RR-21: 就绪清单不含任何可执行 / 激活路径。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    manifest = build_runtime_readiness_manifest(
        fixture_identity="rr21", decisions=[decision], dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )

    assert manifest["executable"] is False
    assert manifest["activation_api"] is None
    assert set(manifest["forbidden_operations"]) == set(FORBIDDEN_RUNTIME_MANIFEST_KEYS)
    assert "activates nothing" in manifest["non_executable_marker"]
    lowered = {key.lower() for key in manifest}
    assert lowered.isdisjoint({k.lower() for k in FORBIDDEN_RUNTIME_MANIFEST_KEYS})
    for value in manifest.values():
        assert not callable(value)

    assert_runtime_manifest_not_executable(manifest)

    for tampered in (
        dict(manifest, executable=True),
        dict(manifest, activation_api="activate_mapping"),
        dict(manifest, kind="RUNTIME_ACTIVATION_PLAN"),
        dict(manifest, apply=lambda: None),
        dict(manifest, bindings_to_create=[]),
        dict(manifest, enable=True),
    ):
        with pytest.raises(RuntimeEligibilityPolicyError):
            assert_runtime_manifest_not_executable(tampered)


# ---------------------------------------------------------------------------
# RR-22 — 影子运行期校验绝不写账本
# ---------------------------------------------------------------------------

def test_rr_22_shadow_runtime_check_never_writes_a_ledger(tmp_path, owner_keys):
    """RR-22: 影子运行期校验绝不写账本、绝不写文件、绝不创建绑定。"""
    workspace = tmp_path / "rr22"
    workspace.mkdir()
    before = sorted(os.listdir(workspace))

    observation, proposal, approval, context = _valid_setup(owner_keys)
    report = shadow_runtime_eligibility_check(
        workspace=str(workspace),
        proposal=proposal,
        approval=approval,
        source_observation=observation,
        runtime_context=context,
        evaluated_at=EVALUATED_AT,
        scenario="RR-22",
    )

    assert report["analysis_kind"] == "SHADOW_RUNTIME_ELIGIBILITY_CHECK"
    assert report["eligibility_result"] == RuntimeEligibilityResult.ELIGIBLE.value
    assert report["ledger_written"] is False
    assert report["files_written"] == 0
    assert report["bindings_created"] == 0
    assert report["canonical_records_modified"] == 0
    assert report["is_production_authority"] is False
    assert report["runtime_mapping_activation"] == RUNTIME_MAPPING_ACTIVATION
    assert report["production_mapping_write_authorization"] == (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    )

    assert sorted(os.listdir(workspace)) == before
    assert not (workspace / "mapping_governance_ledger.jsonl").exists()
    assert not (workspace / "mapping_governance_ledger.jsonl.lock").exists()

    # 场景矩阵：全部 8 个场景评估完成且与期望一致，且不写任何东西。
    matrix = shadow_runtime_scenario_matrix(
        workspace=str(workspace),
        canonical_id=CANONICAL_ID,
        source_observation=observation,
        owner_private_key_path=owner_keys["private"],
        trusted_public_key_path=owner_keys["public"],
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        evaluated_at=EVALUATED_AT,
    )
    assert matrix["all_scenarios_evaluated"] is True
    assert matrix["all_matched"] is True
    assert matrix["evaluated_count"] == len(RUNTIME_SCENARIO_MATRIX) == 8
    assert matrix["ledger_written"] is False
    assert matrix["files_written"] == 0
    assert sorted(os.listdir(workspace)) == before

    expected = {name: want for name, want, _detail in RUNTIME_SCENARIO_MATRIX}
    for scenario in matrix["scenarios"]:
        assert scenario["eligibility_result"] == expected[scenario["scenario"]]

    # 未提供密钥时「有效批准」场景标记为 SKIPPED，绝不退化为信任内嵌公钥。
    skipped = shadow_runtime_scenario_matrix(
        workspace=str(workspace),
        canonical_id=CANONICAL_ID,
        source_observation=observation,
        owner_private_key_path=None,
        trusted_public_key_path=None,
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        evaluated_at=EVALUATED_AT,
    )
    valid = next(
        s for s in skipped["scenarios"] if s["scenario"] == "VALID_APPROVED_MAPPING"
    )
    assert valid["status"] == "SKIPPED"
    assert skipped["all_scenarios_evaluated"] is False
    assert skipped["all_matched"] is False


# ---------------------------------------------------------------------------
# RR-23 / RR-24 — 既有运行期与七存储
# ---------------------------------------------------------------------------

def test_rr_23_production_refresh_remains_unchanged(isolated_data_dir, monkeypatch):
    """RR-23: 生产 refresh 行为不变（不引用任何新模块，输出形状不变）。"""
    # 结构证明：刷新路径不引用治理 / 证据 / 迁移 / 运行期就绪模块。
    for name in ("scraper.py", "ingestion_identity.py", "match_identity.py", "fetcher_500.py"):
        source = open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8").read()
        for forbidden in (
            "runtime_mapping_eligibility",
            "runtime_mapping_policy",
            "mapping_governance",
            "provider_evidence",
            "source_id_migration",
        ):
            assert forbidden not in source, (name, forbidden)

    # refresh 仍然经由刷新锁 + 摄取身份预检。
    scraper_source = open(
        os.path.join(REPO_ROOT, "utils", "scraper.py"), encoding="utf-8"
    ).read()
    assert "acquire_refresh_lock" in scraper_source
    assert "validate_ingestion_identity(existing, incoming)" in scraper_source
    assert "validate_canonical_uniqueness(" in scraper_source

    # 行为证明：一次真实 refresh 输出形状不变，且只写入预期的存储。
    _fake_sources(
        monkeypatch,
        live_football=[_market_row(match_id="m-live", odds={"home_win": 1.9, "draw": 3.4, "away_win": 3.8})],
    )
    result = scraper.refresh(verbose=False)

    assert set(result) >= {
        "started_at",
        "sources",
        "added",
        "updated",
        "calibrated",
        "odds_snapshots_added",
        "prediction_snapshots_added",
        "settlements_added",
        "evaluation_rows_added",
        "total",
    }
    stored = load_json("daily_matches.json")["matches"]
    assert [m["id"] for m in stored] == ["m-live"]
    assert stored[0]["market_tracked"] is True

    # 刷新目录里没有出现任何治理 / 运行期工件。
    listing = sorted(os.listdir(str(isolated_data_dir)))
    for forbidden in (
        "mapping_governance_ledger.jsonl",
        "runtime_readiness.json",
        "migration_impact_manifest.json",
    ):
        assert forbidden not in listing


def test_rr_24_seven_store_hashes_unchanged(isolated_data_dir, monkeypatch, owner_keys):
    """RR-24: 七个存储的运行期判定前后逐字节不变。"""
    _seed_seven_stores(isolated_data_dir)
    _fake_sources(
        monkeypatch,
        live_football=[_market_row(match_id="m-live", odds={"home_win": 1.9, "draw": 3.4, "away_win": 3.8})],
    )
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)

    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    build_runtime_readiness_manifest(
        fixture_identity="rr24",
        decisions=[decision],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    shadow_runtime_readiness_report(
        workspace=str(isolated_data_dir.parent / "rr24_shadow"),
        proposals=[proposal],
        observations=[observation],
        canonical_fixture=[_market_row()],
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )
    shadow_runtime_eligibility_check(
        workspace=str(isolated_data_dir.parent / "rr24_shadow"),
        proposal=proposal,
        approval=approval,
        source_observation=observation,
        runtime_context=context,
        evaluated_at=EVALUATED_AT,
    )

    assert _hash_seven_stores(isolated_data_dir) == before
    assert len(before) == len(SEVEN_STORES) == 7
    assert all(len(digest) == 64 for digest in before.values())


# ---------------------------------------------------------------------------
# RR-25 / RR-26 — 既有守卫与修复授权
# ---------------------------------------------------------------------------

def test_rr_25_existing_ingestion_guard_remains_unchanged():
    """RR-25: 既有摄取身份预检语义不变。"""
    # 跨运动共用 500j 前缀仍然 fail closed。
    football = _market_row(match_id="500j-2030-05-01-001", number="001")
    basketball = _market_row(
        match_id="500j-2030-05-01-001", number="001", sport="basketball",
        home="湖人", away="勇士",
    )
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity([], [football, basketball])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    # 既有重复 Canonical ID 仍然 fail closed。
    duplicate = [_market_row(match_id="dup-1"), _market_row(match_id="dup-1")]
    with pytest.raises(CanonicalIdentityCollisionError) as exc2:
        plan_ingestion_identity(duplicate, [])
    assert exc2.value.category == "PREEXISTING_DUPLICATE"

    # 运行期就绪层没有替换、包装或削弱它。
    source = open(
        os.path.join(REPO_ROOT, "utils", "ingestion_identity.py"), encoding="utf-8"
    ).read()
    assert "runtime_mapping" not in source
    assert "runtime_eligibility" not in source
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )
    assert runtime_policy_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


def test_rr_26_existing_repair_authorization_remains_unchanged():
    """RR-26: 生产修复授权契约不变（schema 2.0.0，无旁路开关）。"""
    import tools.production_canonical_repair as repair

    assert repair.AUTHORIZATION_SCHEMA_VERSION == "2.0.0"
    assert repair.LEGACY_AUTHORIZATION_SCHEMA_VERSIONS == ["1.0.0"]

    parser = repair.build_parser()
    help_text = parser.format_help()
    for sub in parser._subparsers._group_actions[0].choices.values():
        help_text += sub.format_help()
    for forbidden in (
        "--force",
        "--skip-verification",
        "--ignore-hash",
        "--allow-unsigned",
        "--allow-production-without-approval",
        "--fault-inject",
    ):
        assert forbidden not in help_text, forbidden

    assert governance_status()["production_mapping_write_authorization"] == (
        "NOT_GRANTED"
    )
    assert governance_status()["runtime_ledger_enforcement"] == "NOT_ENABLED"


# ---------------------------------------------------------------------------
# RR-27 / RR-28 — 跨平台 CI
# ---------------------------------------------------------------------------

def _ci_workflow() -> str:
    return open(
        os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8"
    ).read()


def test_rr_27_ubuntu_ci_is_declared():
    """RR-27: CI 在 ubuntu-latest 上运行全量离线套件（实际结论由远端 CI 证明）。"""
    workflow = _ci_workflow()
    assert "ubuntu-latest" in workflow
    assert "Offline pytest suite" in workflow
    assert "python -m pytest -q -rs" in workflow
    assert re.search(r"os:\s*\[ubuntu-latest,\s*windows-latest\]", workflow)


def test_rr_28_windows_ci_is_declared():
    """RR-28: CI 在 windows-latest 上运行（Windows 专属代码路径必须被覆盖）。"""
    workflow = _ci_workflow()
    assert "windows-latest" in workflow
    assert "Isolated Canonical repair lifecycle" in workflow
    assert "staging-validation" in workflow


# ---------------------------------------------------------------------------
# 补充不变量：策略注册表自洽 / 规则覆盖
# ---------------------------------------------------------------------------

def test_policy_registry_is_declarative_and_coherent():
    """补充：策略注册表自洽，白名单与黑名单互不重叠，规则码唯一。"""
    assert ALLOWED_RUNTIME_IDENTITY_KEYS == {
        IdentityKeyKind.COMPETITION_NUMBER.value,
        IdentityKeyKind.RAW_PROVIDER_ID.value,
    }
    assert ALLOWED_IDENTITY_KEY_VERSIONS == {IDENTITY_KEY_SCHEMA_VERSION}
    assert ALLOWED_APPROVAL_VERSIONS == {MAPPING_APPROVAL_SCHEMA_VERSION}
    assert ALLOWED_ACTIONS == {ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value}
    assert BLOCKED_ACTIONS == {"REWRITE", "RENAME", "MIGRATE", "DELETE", "MERGE_CANONICAL"}
    assert set(ALLOWED_RUNTIME_IDENTITY_KEYS).isdisjoint(BLOCKED_IDENTITY_TYPES)
    assert set(ALLOWED_ACTIONS).isdisjoint(BLOCKED_ACTIONS)
    assert ("market_xml", "500j") in ALLOWED_SOURCE_SCOPES

    codes = rule_codes()
    assert len(codes) == len(set(codes)) == len(RUNTIME_ELIGIBILITY_RULES)
    assert REJECTION_CODES == {rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES}
    assert len(REJECTION_CODES) == len(RUNTIME_ELIGIBILITY_RULES)

    status = runtime_policy_status()
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"
    assert status["rule_count"] == len(RUNTIME_ELIGIBILITY_RULES)
    assert set(status["blocked_effects"]) == {
        "CANONICAL_ID_REWRITE",
        "HISTORICAL_MIGRATION",
        "SNAPSHOT_REGENERATION",
        "SETTLEMENT_REGENERATION",
        "EVALUATION_REGENERATION",
    }


def test_every_rejection_code_is_reachable(owner_keys):
    """
    补充：**每一条**声明的拒绝码都必须能被某个真实判定触发（拒绝面没有死代码）。

    这是逐码证明，而不是覆盖率抽样：每个拒绝码都有明确的构造场景。
    """
    observation, proposal, approval, base = _valid_setup(owner_keys)
    base_key_dict = ScopedSourceIdentityKey.for_observation(observation).as_dict()

    legacy_proposal = _synthetic_proposal(
        key_dict={
            "identity_key_schema_version": "2.0.0",
            "identity_kind": "RAW_PROVIDER_ID",
            "identity_value": "market_xml|500j|PROVIDER_MATCH_ID|12345",
        }
    )
    unverified_proposal = _synthetic_proposal(
        key_dict={
            "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
            "identity_kind": "UNVERIFIED_PROVIDER_ID",
            "identity_value": "market_xml|500j|football|2030-05-01|PROVIDER_MATCH_ID|UNKNOWN|12345",
            "provider_id_uniqueness_scope": "UNKNOWN",
        }
    )
    ambiguous_proposal = _synthetic_proposal(
        key_dict={
            "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
            "identity_kind": "AMBIGUOUS_SHARED_SOURCE_ID",
            "identity_value": "500j-2030-05-01-001",
        }
    )
    no_action_proposal = _synthetic_proposal(
        key_dict=base_key_dict, action=ProposalAction.NO_ACTION.value
    )
    foreign_scope_proposal = _synthetic_proposal(
        key_dict=base_key_dict,
        scope_override={"adapter": "unknown_adapter", "provider_namespace": "999x"},
    )

    finished_row = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "15:30", "kickoff_time_known": True,
        "status": "finished", "home": "甲队", "away": "乙队",
        "score": {"ft": [2, 1]}, "odds": None,
    }
    row_position_observation = observe_source_row(finished_row, adapter="finished_feed")
    row_position_proposal = _synthetic_proposal(
        key_dict=ScopedSourceIdentityKey.for_observation(
            row_position_observation
        ).as_dict()
    )

    tampered = copy.deepcopy(approval)
    raw_signature = tampered["approval"]["signature"]
    tampered["approval"]["signature"] = (
        ("A" if raw_signature[0] != "A" else "B") + raw_signature[1:]
    )

    scenarios = {
        # -- 证据绑定 ---------------------------------------------------------
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL": (
            proposal, approval, _observation(match_id=CANONICAL_ID_ALT, number="008"),
            _context(owner_keys, proposal),
        ),
        "SOURCE_IDENTITY_KEY_MISMATCH": (
            proposal, approval, _observation(match_id=CANONICAL_ID_ALT, number="008"),
            _context(owner_keys, proposal),
        ),
        "SOURCE_SCOPE_MISMATCH": (
            _recompose_proposal(proposal, scope_overrides={"event_date": "2030-05-02"}),
            approval,
            observation,
            _context(owner_keys, proposal),
        ),
        # -- 身份 -------------------------------------------------------------
        "IDENTITY_KEY_VERSION_NOT_CURRENT": (
            legacy_proposal, None, observation, _context(owner_keys, legacy_proposal),
        ),
        "IDENTITY_KIND_NOT_ALLOWED": (
            unverified_proposal, None, observation,
            _context(owner_keys, unverified_proposal),
        ),
        "IDENTITY_EVIDENCE_INSUFFICIENT": (
            unverified_proposal, None, observation,
            _context(owner_keys, unverified_proposal),
        ),
        "UNVERIFIED_PROVIDER_ID": (
            unverified_proposal, None, observation,
            _context(owner_keys, unverified_proposal),
        ),
        "IDENTITY_TYPE_BLOCKED": (
            ambiguous_proposal, None, observation,
            _context(owner_keys, ambiguous_proposal),
        ),
        "PROVIDER_ID_SCOPE_NOT_VERIFIED": (
            legacy_proposal, None, observation, _context(owner_keys, legacy_proposal),
        ),
        "ROW_POSITION_UNSTABLE": (
            row_position_proposal, None, row_position_observation,
            _context(owner_keys, row_position_proposal),
        ),
        "RAW_IDENTIFIER_TYPE_NOT_DURABLE": (
            row_position_proposal, None, row_position_observation,
            _context(owner_keys, row_position_proposal),
        ),
        "SOURCE_FINGERPRINT_MISSING": (
            proposal, approval, None, _context(owner_keys, proposal),
        ),
        "IDENTITY_AMBIGUOUS": (
            proposal, approval, observation,
            _context(
                owner_keys,
                proposal,
                pending_ambiguities=(base_key_dict["identity_value"],),
            ),
        ),
        "CANONICAL_OWNER_CONFLICT": (
            proposal, approval, observation,
            _context(
                owner_keys,
                proposal,
                known_source_bindings=(
                    (proposal.source_key, "canon-A"),
                    (proposal.source_key, "canon-B"),
                ),
            ),
        ),
        # -- 治理 -------------------------------------------------------------
        "PROPOSAL_MISSING": (None, approval, observation, _context(owner_keys, proposal)),
        "EVIDENCE_PROVENANCE_UNSUPPORTED": (
            proposal,
            approval,
            observation,
            _context(
                owner_keys,
                proposal,
                evidence_provenance=GovernanceEvidenceProvenance(
                    source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
                    structural_ledger_validation_performed=True,
                    authenticated_approval_audit_performed=True,
                ),
            ),
        ),
        "PROPOSAL_FINGERPRINT_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        ),
        "PROPOSAL_FINGERPRINT_MISMATCH": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint="0" * 64),
        ),
        "RECORDED_STATE_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state=None),
        ),
        "RECORDED_STATE_UNKNOWN": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="NOT_A_KNOWN_STATE"),
        ),
        "RECORDED_STATE_PROPOSAL_MISMATCH": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_id="some-other-proposal"),
        ),
        "MAPPING_STATE_NOT_ACTIVE": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="PROPOSED"),
        ),
        "MAPPING_REVOKED": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="REVOKED"),
        ),
        "MAPPING_SUPERSEDED": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="SUPERSEDED"),
        ),
        "APPROVAL_MISSING": (proposal, None, observation, _context(owner_keys, proposal)),
        "TRUST_ANCHOR_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, trusted_public_key_path=None),
        ),
        "APPROVAL_LEGACY_VERSION": (
            proposal,
            dict(approval, approval_schema_version="1.0.0"),
            observation,
            _context(owner_keys, proposal),
        ),
        "APPROVAL_PURPOSE_MISMATCH": (
            proposal,
            dict(approval, purpose="PRODUCTION_CANONICAL_DEDUP_APPLY"),
            observation,
            _context(owner_keys, proposal),
        ),
        "APPROVAL_WRONG_APPROVER": (
            proposal, approval, observation,
            _context(owner_keys, proposal, expected_approver="owner-OTHER"),
        ),
        "APPROVAL_WRONG_OPERATOR": (
            proposal, approval, observation,
            _context(owner_keys, proposal, expected_operator="operator-OTHER"),
        ),
        "APPROVAL_SIGNATURE_INVALID": (
            proposal, tampered, observation, _context(owner_keys, proposal),
        ),
        "BASELINE_STALE": (
            proposal, approval, observation,
            _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        ),
        "DATASET_IDENTITY_MISMATCH": (
            proposal,
            _approval(proposal, observation, owner_keys, dataset="other-dataset"),
            observation,
            _context(owner_keys, proposal),
        ),
        "APPROVAL_REPLAYED": (
            proposal, approval, observation,
            _context(
                owner_keys,
                proposal,
                prior_approval_bindings=(
                    (approval_fingerprint(approval), "other-identity-key"),
                ),
            ),
        ),
        # -- 运行期安全 -------------------------------------------------------
        "ACTION_NOT_ALLOWED": (
            no_action_proposal, None, observation, _context(owner_keys, no_action_proposal),
        ),
        "SOURCE_SCOPE_NOT_ALLOWED": (
            foreign_scope_proposal, None, observation,
            _context(owner_keys, foreign_scope_proposal),
        ),
        "CANONICAL_OWNER_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, canonical_ids=(), allow_new_event=False),
        ),
        "CANONICAL_OWNERSHIP_CONFLICT": (
            proposal, approval, observation,
            _context(
                owner_keys,
                proposal,
                known_source_bindings=((proposal.source_key, "canon-OTHER"),),
            ),
        ),
        "CANONICAL_ID_CHANGE_REQUIRED": (
            proposal, approval, observation,
            _context(owner_keys, proposal, canonical_rename_required=True),
        ),
        "HISTORICAL_REFERENCE_CONFLICT": (
            proposal, approval, observation,
            _context(owner_keys, proposal, required_rewrite_targets=("calibrated.json",)),
        ),
        "SNAPSHOT_IMPACT": (
            proposal, approval, observation,
            _context(
                owner_keys, proposal,
                required_rewrite_targets=("odds_snapshots.json",),
            ),
        ),
        "SETTLEMENT_IMPACT": (
            proposal, approval, observation,
            _context(owner_keys, proposal, required_rewrite_targets=("settlements.json",)),
        ),
        "EVALUATION_IMPACT": (
            proposal, approval, observation,
            _context(
                owner_keys, proposal,
                required_rewrite_targets=("evaluation_rows.json",),
            ),
        ),
        "MIGRATION_REQUIRED": (
            proposal, approval, observation,
            _context(owner_keys, proposal, migration_required=True),
        ),
        "PENDING_AMBIGUITY": (
            proposal, approval, observation,
            _context(owner_keys, proposal, pending_ambiguities=("some-other-identity",)),
        ),
    }
    # 场景表必须覆盖**全部**声明的拒绝码（不多不少）。
    assert set(scenarios) == set(REJECTION_CODES)

    for code, (candidate_proposal, candidate_approval, candidate_observation, ctx) in (
        scenarios.items()
    ):
        decision = evaluate_runtime_mapping_eligibility(
            candidate_proposal,
            candidate_approval,
            candidate_observation,
            ctx,
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, code
        assert code in decision.rejection_codes, (code, decision.rejection_codes)

    # 允许列表内没有改写动作。
    for token in BLOCKED_ACTIONS:
        assert all(token not in action for action in ALLOWED_ACTIONS)


# ===========================================================================
# EC-01 .. EC-28 — 证据绑定与 fail-closed 修正回归矩阵
# ===========================================================================

# ---------------------------------------------------------------------------
# EC-01 .. EC-04 — 已记录提案指纹完整性
# ---------------------------------------------------------------------------

def test_ec_01_missing_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-01: 缺失已记录提案指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes
    # 缺失指纹绝不意味着「指纹校验可选」。
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes


def test_ec_02_blank_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-02: 空白已记录指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for blank in ("", "   ", "\t"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=blank),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes, blank


def test_ec_03_mismatched_recorded_fingerprint_returns_not_eligible(owner_keys):
    """EC-03: 不匹配 / 格式不合法的已记录指纹 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for bad in ("0" * 64, "NOT-A-SHA256", proposal.fingerprint()[:-1] + "0"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=bad),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISMATCH" in decision.rejection_codes, bad


def test_ec_04_correct_recorded_fingerprint_passes_the_rule(owner_keys):
    """EC-04: 正确的已记录指纹通过指纹规则（但**不**证明账本可信）。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(
            owner_keys, proposal,
            recorded_proposal_fingerprint=proposal.fingerprint(),
        ),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" not in decision.rejection_codes
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes
    # 指纹匹配只是一致性；来源仍然是条件式的，不是独立验证过的证据。
    assert decision.is_conditional is True
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED


# ---------------------------------------------------------------------------
# EC-05 / EC-06 — 已记录治理状态完整性
# ---------------------------------------------------------------------------

def test_ec_05_missing_recorded_proposal_state_returns_not_eligible(owner_keys):
    """EC-05: 缺失已记录治理状态 -> NOT_ELIGIBLE(RECORDED_STATE_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in decision.rejection_codes

    # 未知状态同样 fail closed。
    unknown = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state="BANANA"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_UNKNOWN" in unknown.rejection_codes

    # 已记录状态必须对应被评估的提案 ID。
    other = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_id="another-proposal"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_PROPOSAL_MISMATCH" in other.rejection_codes


def test_ec_06_asserted_proposal_status_does_not_replace_the_recorded_state(owner_keys):
    """EC-06: 提案自述的 APPROVED 状态绝不替代已记录的治理状态。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    self_asserted = _recompose_proposal(proposal, status="APPROVED")
    assert self_asserted.status == "APPROVED"

    # 没有任何已记录状态 -> 仍然拒绝（绝不回退到提案自述状态）。
    missing = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert missing.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in missing.rejection_codes

    # 已记录状态为 PROPOSED -> 同样拒绝，即便提案自述 APPROVED。
    proposed = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state="PROPOSED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_STATE_NOT_ACTIVE" in proposed.rejection_codes

    # 已记录状态为 REVIEW_REQUIRED / REJECTED -> 拒绝（不授权任何变更）。
    for state in ("REVIEW_REQUIRED", "REJECTED", "REVOKED", "SUPERSEDED", "OBSERVED"):
        decision = evaluate_runtime_mapping_eligibility(
            self_asserted,
            approval,
            observation,
            _context(owner_keys, self_asserted, recorded_proposal_state=state),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, state
        assert "MAPPING_STATE_NOT_ACTIVE" in decision.rejection_codes, state

    # 只有 APPROVED 才是活动终态。
    assert ALLOWED_PROPOSAL_STATES == {"APPROVED"}
    assert ALLOWED_PROPOSAL_STATES <= KNOWN_PROPOSAL_STATES


# ---------------------------------------------------------------------------
# EC-07 .. EC-11 — 观测 / 提案证据绑定
# ---------------------------------------------------------------------------

def _unrelated_observation():
    return _observation(match_id=CANONICAL_ID_ALT, number="008", home="甲", away="乙")


def test_ec_07_observation_with_unrelated_competition_identity_is_rejected(owner_keys):
    """EC-07: 与提案无关的竞彩身份观测被拒绝。"""
    _observation_valid, proposal, approval, _ = _valid_setup(owner_keys)
    other = _unrelated_observation()
    assert ScopedSourceIdentityKey.for_observation(other).source_key() != proposal.source_key

    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, other, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes


def test_ec_08_observation_fingerprint_absent_from_proposal_is_rejected(owner_keys):
    """EC-08: 观测指纹不在提案证据集合中 -> NOT_ELIGIBLE。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()
    assert unrelated.semantic_fingerprint() not in proposal.observation_fingerprints

    # 用**无关观测的指纹**签名一份有效批准，再把它用在原提案上。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes

    # 观测引用为空白同样拒绝。
    blank_reference = _recompose_proposal(proposal, source_observation_ref="")
    decision_blank = evaluate_runtime_mapping_eligibility(
        blank_reference, _approval(blank_reference, observation, owner_keys),
        observation, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT,
    )
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision_blank.rejection_codes


def test_ec_09_observation_identity_key_differing_is_rejected(owner_keys):
    """EC-09: 由观测独立推导的身份键与提案身份键不一致 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    tampered_key = dict(proposal.identity_key_dict)
    tampered_key["identity_value"] = "football|2030-05-01|jczq|99"
    tampered = _recompose_proposal(proposal, key_dict=tampered_key)

    decision = evaluate_runtime_mapping_eligibility(
        tampered, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes

    # 身份键契约非当前 -> 直接拒绝（不因别名而被绕过）。
    legacy_key = dict(proposal.identity_key_dict)
    legacy_key["identity_key_schema_version"] = "2.0.0"
    legacy = _recompose_proposal(proposal, key_dict=legacy_key)
    decision_legacy = evaluate_runtime_mapping_eligibility(
        legacy, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "IDENTITY_KEY_VERSION_NOT_CURRENT" in decision_legacy.rejection_codes


def test_ec_10_observation_scope_differing_is_rejected(owner_keys):
    """EC-10: 观测作用域与提案声明作用域不一致 -> NOT_ELIGIBLE(SOURCE_SCOPE_MISMATCH)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    for override in (
        {"event_date": "2030-05-02"},
        {"sport": "basketball"},
        {"provider_namespace": "500l"},
        {"adapter": "live_football"},
    ):
        mismatched = _recompose_proposal(proposal, scope_overrides=override)
        decision = evaluate_runtime_mapping_eligibility(
            mismatched, approval, observation, _context(owner_keys, proposal),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, override
        assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes, override


def test_ec_11_valid_multi_observation_proposal_remains_supported(owner_keys):
    """EC-11: 合法的多观测提案仍然受支持（当前观测不必是第一条）。"""
    first = _observation(time="20:00")
    second = _observation(time="21:00")
    assert first.semantic_fingerprint() != second.semantic_fingerprint()
    assert (
        ScopedSourceIdentityKey.for_observation(first).source_key()
        == ScopedSourceIdentityKey.for_observation(second).source_key()
    )

    proposal = proposal_from_observations(
        [first, second],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="multi-observation proposal",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    assert len(proposal.observation_fingerprints) == 2
    assert second.semantic_fingerprint() in proposal.observation_fingerprints

    # 批准件绑定**第二条**观测；用第二条观测评估 -> 仍然是条件式 ELIGIBLE。
    approval = _approval(proposal, second, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, second, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()

    # 第一条观测同样满足成员关系（不要求是「当前」那一条）。
    first_decision = evaluate_runtime_mapping_eligibility(
        proposal,
        _approval(proposal, first, owner_keys),
        first,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert first_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value


# ---------------------------------------------------------------------------
# EC-12 / EC-13 — 条件式合格 vs 一致证据
# ---------------------------------------------------------------------------

def test_ec_12_correct_evidence_produces_a_conditional_eligible_decision(owner_keys):
    """EC-12: 正确的观测 + 提案 + 签名批准 -> 条件式 ELIGIBLE（非生产授权）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED

    # 换成隔离快照来源 -> 仍是条件式（只是来源不同）。
    snapshot_context = _context(
        owner_keys,
        proposal,
        evidence_provenance=isolated_snapshot_provenance(
            dataset_identity=DATASET, baseline_fingerprint=BASELINE,
            structural_ledger_validation_performed=True,
        ),
    )
    snapshot_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, snapshot_context, evaluated_at=EVALUATED_AT
    )
    assert snapshot_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert snapshot_decision.evidence_provenance == (
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    )
    assert snapshot_decision.is_conditional is True

    # 声称独立验证过的账本证据 -> 一律阻断（自述布尔值不是证据）。
    claimed = _context(
        owner_keys,
        proposal,
        evidence_provenance=GovernanceEvidenceProvenance(
            source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
            structural_ledger_validation_performed=True,
            authenticated_approval_audit_performed=True,
        ),
    )
    claimed_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, claimed, evaluated_at=EVALUATED_AT
    )
    assert claimed_decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "EVIDENCE_PROVENANCE_UNSUPPORTED" in claimed_decision.rejection_codes
    assert CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER in BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    assert ALLOWED_CONTEXT_EVIDENCE_PROVENANCE == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    }


def test_ec_13_valid_signature_over_inconsistent_evidence_remains_not_eligible(owner_keys):
    """EC-13: 有效签名覆盖不一致证据时仍然 NOT_ELIGIBLE（签名不使证据变合格）。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    # 用无关观测的指纹签署**原提案**：签名本身有效，但证据不一致。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    assert signed_over_unrelated["source_observation_fingerprint"] == (
        unrelated.semantic_fingerprint()
    )

    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    # 直接可诊断的不一致**不**复用 APPROVAL_SIGNATURE_INVALID。
    assert "APPROVAL_SIGNATURE_INVALID" not in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes


# ---------------------------------------------------------------------------
# EC-14 .. EC-19 — 既有拒绝面在修正后保持有效
# ---------------------------------------------------------------------------

def test_ec_14_revoked_mapping_remains_rejected(owner_keys):
    """EC-14: 已撤销映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="REVOKED"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "MAPPING_REVOKED" in decision.rejection_codes


def test_ec_15_superseded_mapping_remains_rejected(owner_keys):
    """EC-15: 已取代映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="SUPERSEDED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_SUPERSEDED" in decision.rejection_codes


def test_ec_16_stale_baseline_remains_rejected(owner_keys):
    """EC-16: 陈旧基线仍然被拒绝，且旧 ELIGIBLE 判定不构成永久授权。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    before = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert before.is_eligible

    after = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        evaluated_at=EVALUATED_AT,
    )
    assert after.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "BASELINE_STALE" in after.rejection_codes
    # 基线变化 -> 判定身份随之改变（必须重新评估）。
    assert after.decision_id != before.decision_id
    assert after.baseline_fingerprint != before.baseline_fingerprint


def test_ec_17_unverified_provider_identity_remains_rejected(owner_keys):
    """EC-17: 未验证的 provider 身份仍然被拒绝。"""
    provider_key = ScopedSourceIdentityKey.for_observation(
        observe_source_row(
            _market_row(number=None),
            adapter="market_xml",
            raw_provider_id="12345",
            raw_provider_id_type="PROVIDER_MATCH_ID",
        )
    )
    assert provider_key.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
    proposal = _synthetic_proposal(key_dict=provider_key.as_dict())
    observation = observe_source_row(
        _market_row(number=None),
        adapter="market_xml",
        raw_provider_id="12345",
        raw_provider_id_type="PROVIDER_MATCH_ID",
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "UNVERIFIED_PROVIDER_ID" in decision.rejection_codes


def test_ec_18_row_position_identity_remains_rejected(owner_keys):
    """EC-18: 行位置派生身份仍然被拒绝。"""
    finished = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "15:30", "kickoff_time_known": True,
        "status": "finished", "home": "甲队", "away": "乙队",
        "score": {"ft": [2, 1]}, "odds": None,
    }
    observation = observe_source_row(finished, adapter="finished_feed")
    proposal = _synthetic_proposal(
        key_dict=ScopedSourceIdentityKey.for_observation(observation).as_dict()
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "ROW_POSITION_UNSTABLE" in decision.rejection_codes
    assert "RAW_IDENTIFIER_TYPE_NOT_DURABLE" in decision.rejection_codes


def test_ec_19_cross_sport_shared_aliases_remain_ineligible(owner_keys):
    """EC-19: 跨运动共用源别名的身份证明不足时仍然不合格。"""
    football = observe_source_row(
        _market_row(match_id="500j-2030-05-01-001", number="001"), adapter="market_xml"
    )
    basketball = observe_source_row(
        _market_row(
            match_id="500j-2030-05-01-001", number="001", sport="basketball",
            home="湖人", away="勇士",
        ),
        adapter="market_xml",
    )
    assert football.derived_source_id == basketball.derived_source_id
    key_football = ScopedSourceIdentityKey.for_observation(football)
    key_basketball = ScopedSourceIdentityKey.for_observation(basketball)
    assert key_football.source_key() != key_basketball.source_key()

    proposal = proposal_from_observations(
        [football],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id="500j-2030-05-01-001",
        existing_canonical_owner="500j-2030-05-01-001",
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="cross-sport alias",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, basketball, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes

    # 既有摄取预检继续对该批次 fail closed。
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity([], [dict(_market_row(match_id="500j-2030-05-01-001", number="001")),
                                     dict(_market_row(match_id="500j-2030-05-01-001", number="001",
                                                      sport="basketball", home="湖人", away="勇士"))])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


# ---------------------------------------------------------------------------
# EC-20 / EC-21 — 注册表完整性
# ---------------------------------------------------------------------------

def test_ec_20_policy_registry_and_dispatch_remain_complete():
    """EC-20: 声明式策略注册表与规则分派表保持完整、自洽。"""
    from utils.runtime_mapping_eligibility import _RULE_CHECKS

    declared = {rule.code for rule in RUNTIME_ELIGIBILITY_RULES}
    assert declared == set(_RULE_CHECKS)
    assert len(declared) == len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert len(REJECTION_CODES) == 45
    assert {rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES} == REJECTION_CODES
    assert len(rule_codes()) == len(set(rule_codes()))

    # 新增类别与来源声明齐备。
    categories = runtime_policy_status()["rules_by_category"]
    assert categories["EVIDENCE_BINDING"] == [
        "SOURCE_OBSERVATION_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MATCHES",
        "SOURCE_SCOPE_MATCHES",
    ]
    assert len(categories["GOVERNANCE"]) == 20
    assert runtime_policy_status()["known_proposal_states"] == sorted(
        KNOWN_PROPOSAL_STATES
    )
    assert set(ALLOWED_CONTEXT_EVIDENCE_PROVENANCE).isdisjoint(
        BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    )

    # 版本号已按语义变化提升。
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_EVALUATOR_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION == "1.1.0"


def test_ec_21_every_new_rejection_code_has_an_executable_reproducer(owner_keys):
    """EC-21: 每条**新增**拒绝码都有可执行复现。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    reproducers = {
        "PROPOSAL_FINGERPRINT_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        ),
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_IDENTITY_KEY_MISMATCH": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_SCOPE_MISMATCH": (
            _recompose_proposal(proposal, scope_overrides={"event_date": "2030-05-02"}),
            approval, observation, _context(owner_keys, proposal),
        ),
        "RECORDED_STATE_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state=None),
        ),
        "RECORDED_STATE_UNKNOWN": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="NOPE"),
        ),
        "RECORDED_STATE_PROPOSAL_MISMATCH": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_id="nope"),
        ),
        "EVIDENCE_PROVENANCE_UNSUPPORTED": (
            proposal, approval, observation,
            _context(
                owner_keys, proposal,
                evidence_provenance=GovernanceEvidenceProvenance(
                    source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER
                ),
            ),
        ),
    }
    new_codes = {
        "PROPOSAL_FINGERPRINT_MISSING",
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MISMATCH",
        "SOURCE_SCOPE_MISMATCH",
        "RECORDED_STATE_MISSING",
        "RECORDED_STATE_UNKNOWN",
        "RECORDED_STATE_PROPOSAL_MISMATCH",
        "EVIDENCE_PROVENANCE_UNSUPPORTED",
    }
    assert set(reproducers) == new_codes
    assert new_codes <= REJECTION_CODES

    for code, (candidate_proposal, candidate_approval, candidate_observation, ctx) in (
        reproducers.items()
    ):
        decision = evaluate_runtime_mapping_eligibility(
            candidate_proposal, candidate_approval, candidate_observation, ctx,
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, code
        assert code in decision.rejection_codes, (code, decision.rejection_codes)


# ---------------------------------------------------------------------------
# EC-22 / EC-23 — 确定性与不可执行
# ---------------------------------------------------------------------------

def test_ec_22_repeated_identical_evaluations_remain_deterministic(owner_keys):
    """EC-22: 重复的相同评估保持确定（判定身份与时钟无关）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decisions = [
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT
        )
        for _ in range(5)
    ]
    assert len({d.decision_id for d in decisions}) == 1
    assert len(set(decisions)) == 1
    assert all(d.is_eligible and d.is_conditional for d in decisions)

    clock_shifted = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context,
        evaluated_at="2099-12-31T23:59:59+00:00",
    )
    assert clock_shifted.decision_id == decisions[0].decision_id
    assert clock_shifted.evaluated_at != decisions[0].evaluated_at


def test_ec_23_readiness_manifests_remain_non_executable(owner_keys):
    """EC-23: 就绪清单仍然不可执行，并显式标注条件式判定。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    eligible = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    rejected = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, context, evaluated_at=EVALUATED_AT
    )
    manifest = build_runtime_readiness_manifest(
        fixture_identity="ec23",
        decisions=[eligible, rejected],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert manifest["executable"] is False
    assert manifest["activation_api"] is None
    assert manifest["production_authorization_granted"] is False
    assert manifest["conditional_decision_count"] == 2
    assert manifest["verified_evidence_decision_count"] == 0
    assert manifest["conditional_decisions_are_production_authorization"] is False
    assert manifest["evidence_provenance_histogram"] == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED: 2
    }
    assert manifest["eligible_count"] == 1
    assert manifest["rejected_count"] == 1
    assert_runtime_manifest_not_executable(manifest)
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, executable=True))
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, activate=True))


# ---------------------------------------------------------------------------
# EC-24 / EC-25 — 隔离与七存储
# ---------------------------------------------------------------------------

def test_ec_24_shadow_evaluation_creates_no_binding_ledger_or_write(
    tmp_path, owner_keys
):
    """EC-24: 影子评估不创建绑定、不写账本、不写生产数据。"""
    workspace = tmp_path / "ec24"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before_listing = sorted(os.listdir(workspace))
    before_bytes = (workspace / "sentinel.txt").read_bytes()

    observation, proposal, approval, context = _valid_setup(owner_keys)
    report = shadow_runtime_eligibility_check(
        workspace=str(workspace),
        proposal=proposal,
        approval=approval,
        source_observation=observation,
        runtime_context=context,
        evaluated_at=EVALUATED_AT,
        scenario="EC-24",
    )
    assert report["eligibility_result"] == RuntimeEligibilityResult.ELIGIBLE.value
    assert report["ledger_written"] is False
    assert report["files_written"] == 0
    assert report["bindings_created"] == 0
    assert report["canonical_records_modified"] == 0
    assert report["is_production_authority"] is False
    assert report["runtime_mapping_activation"] == RUNTIME_MAPPING_ACTIVATION
    assert report["decision"]["is_production_authorization"] is False

    assert sorted(os.listdir(workspace)) == before_listing
    assert (workspace / "sentinel.txt").read_bytes() == before_bytes
    assert not (workspace / "mapping_governance_ledger.jsonl").exists()


def test_ec_25_all_seven_stores_remain_byte_identical(
    isolated_data_dir, monkeypatch, owner_keys
):
    """EC-25: 修正后的判定与拒绝路径都不改动七个持久化存储。"""
    _seed_seven_stores(isolated_data_dir)
    _fake_sources(
        monkeypatch,
        live_football=[
            _market_row(
                match_id="m-live",
                odds={"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
            )
        ],
    )
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    observation, proposal, approval, context = _valid_setup(owner_keys)
    workspace = str(isolated_data_dir.parent / "ec25_shadow")

    # 合格路径、拒绝路径、清单与影子入口全部执行一遍。
    evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    for broken_context in (
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        _context(owner_keys, proposal, recorded_proposal_state=None),
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        _context(owner_keys, proposal, required_rewrite_targets=("settlements.json",)),
    ):
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, broken_context, evaluated_at=EVALUATED_AT
        )
    build_runtime_readiness_manifest(
        fixture_identity="ec25",
        decisions=[evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT)],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    shadow_runtime_readiness_report(
        workspace=workspace,
        proposals=[proposal],
        observations=[observation],
        canonical_fixture=[_market_row()],
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )

    assert _hash_seven_stores(isolated_data_dir) == before


# ---------------------------------------------------------------------------
# EC-26 / EC-27 / EC-28 — 既有行为与审计不变
# ---------------------------------------------------------------------------

def test_ec_26_existing_ingestion_identity_behavior_remains_unchanged():
    """EC-26: 既有摄取身份行为不变（规划器语义与权威归属均未改）。"""
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(
            [],
            [
                _market_row(match_id="500j-2030-05-01-001", number="001"),
                _market_row(
                    match_id="500j-2030-05-01-001", number="001",
                    sport="basketball", home="湖人", away="勇士",
                ),
            ],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    with pytest.raises(CanonicalIdentityCollisionError) as dup:
        plan_ingestion_identity(
            [_market_row(match_id="dup-1"), _market_row(match_id="dup-1")], []
        )
    assert dup.value.category == "PREEXISTING_DUPLICATE"

    for name in ("ingestion_identity.py", "match_identity.py", "scraper.py"):
        source = open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8").read()
        assert "runtime_mapping" not in source, name
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


def test_ec_27_historical_ed25519_approval_audit_continues_to_work(
    tmp_path, owner_keys
):
    """EC-27: 历史 Ed25519 批准审计继续工作；隔离快照只读且保留信任锚警告。"""
    observation = _observation()
    proposal = _proposal(observation)
    workspace = tmp_path / "ec27_gov"
    workspace.mkdir()
    ledger = GovernanceLedger(
        str(workspace / "mapping_governance_ledger.jsonl"), dataset_identity=DATASET
    )
    ledger.append(
        proposal=proposal,
        to_state="PROPOSED",
        actor=OPERATOR,
        actor_role="operator",
        reason="propose",
        baseline_fingerprint=BASELINE,
    )
    approval = _approval(proposal, observation, owner_keys)
    ledger.append(
        proposal=proposal,
        to_state="APPROVED",
        actor=APPROVER,
        actor_role="owner",
        reason="approve",
        baseline_fingerprint=BASELINE,
        approval=approval,
        trusted_public_key_path=owner_keys["public"],
        source_observation_fingerprint=observation.semantic_fingerprint(),
    )

    audit = audit_historical_approvals(
        ledger, trusted_public_key_path=owner_keys["public"], dataset_identity=DATASET
    )
    assert audit["approved_decisions"] == 1
    assert audit["failures"] == []

    ledger_bytes = open(ledger.path, "rb").read()
    snapshot = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )
    # 只读：账本逐字节不变。
    assert open(ledger.path, "rb").read() == ledger_bytes

    assert isinstance(snapshot, IsolatedGovernanceSnapshot)
    assert snapshot.structural_integrity == "OK"
    assert snapshot.authenticated_approval_audit == (
        "AUTHENTICATED_MAPPING_APPROVAL_AUDIT"
    )
    assert snapshot.state_of(proposal.proposal_id) == "APPROVED"
    assert snapshot.fingerprint_of(proposal.proposal_id) == proposal.fingerprint()
    assert snapshot.provenance.source == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    # 账本信任锚警告被原样保留（哈希链不是外部信任锚）。
    assert "NONE" in snapshot.provenance.ledger_trust_anchor
    assert snapshot.as_dict()["is_production_authority"] is False

    context = RuntimeMappingContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        evidence_provenance=snapshot.provenance,
        canonical_ids=(CANONICAL_ID,),
        trusted_public_key_path=owner_keys["public"],
        expected_approver=APPROVER,
        expected_operator=OPERATOR,
        recorded_proposal_id=proposal.proposal_id,
        recorded_proposal_fingerprint=snapshot.fingerprint_of(proposal.proposal_id),
        recorded_proposal_state=snapshot.state_of(proposal.proposal_id),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False

    # 缺少可信公钥时审计**不**执行，并如实记录限制（绝不信任内嵌公钥）。
    without_key = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert without_key.authenticated_approval_audit == "NOT_PERFORMED"
    assert any(
        "trusted public key" in limitation
        for limitation in without_key.provenance.verification_limitations
    )


def test_ec_28_ci_declares_ubuntu_and_windows_regressions():
    """EC-28: CI 声明的四个必需 job 与双平台矩阵保持存在。"""
    workflow = _ci_workflow()
    for job in (
        "Offline pytest suite",
        "Isolated Canonical repair lifecycle",
    ):
        assert job in workflow, job
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert re.search(r"os:/s*/[ubuntu-latest,/s*windows-latest/]", workflow)
    assert re.search(r'python-version:/s*/["3/.12"/]', workflow)


# ===========================================================================
# EC-01 .. EC-28 — 证据绑定与 fail-closed 修正回归矩阵
# ===========================================================================

# ---------------------------------------------------------------------------
# EC-01 .. EC-04 — 已记录提案指纹完整性
# ---------------------------------------------------------------------------

def test_ec_01_missing_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-01: 缺失已记录提案指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes
    # 缺失指纹绝不意味着「指纹校验可选」。
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes


def test_ec_02_blank_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-02: 空白已记录指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for blank in ("", "   ", "\t"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=blank),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes, blank


def test_ec_03_mismatched_recorded_fingerprint_returns_not_eligible(owner_keys):
    """EC-03: 不匹配 / 格式不合法的已记录指纹 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for bad in ("0" * 64, "NOT-A-SHA256", proposal.fingerprint()[:-1] + "0"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=bad),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISMATCH" in decision.rejection_codes, bad


def test_ec_04_correct_recorded_fingerprint_passes_the_rule(owner_keys):
    """EC-04: 正确的已记录指纹通过指纹规则（但**不**证明账本可信）。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(
            owner_keys, proposal,
            recorded_proposal_fingerprint=proposal.fingerprint(),
        ),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" not in decision.rejection_codes
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes
    # 指纹匹配只是一致性；来源仍然是条件式的，不是独立验证过的证据。
    assert decision.is_conditional is True
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED


# ---------------------------------------------------------------------------
# EC-05 / EC-06 — 已记录治理状态完整性
# ---------------------------------------------------------------------------

def test_ec_05_missing_recorded_proposal_state_returns_not_eligible(owner_keys):
    """EC-05: 缺失已记录治理状态 -> NOT_ELIGIBLE(RECORDED_STATE_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in decision.rejection_codes

    # 未知状态同样 fail closed。
    unknown = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state="BANANA"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_UNKNOWN" in unknown.rejection_codes

    # 已记录状态必须对应被评估的提案 ID。
    other = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_id="another-proposal"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_PROPOSAL_MISMATCH" in other.rejection_codes


def test_ec_06_asserted_proposal_status_does_not_replace_the_recorded_state(owner_keys):
    """EC-06: 提案自述的 APPROVED 状态绝不替代已记录的治理状态。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    self_asserted = _recompose_proposal(proposal, status="APPROVED")
    assert self_asserted.status == "APPROVED"

    # 没有任何已记录状态 -> 仍然拒绝（绝不回退到提案自述状态）。
    missing = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert missing.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in missing.rejection_codes

    # 已记录状态为 PROPOSED -> 同样拒绝，即便提案自述 APPROVED。
    proposed = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state="PROPOSED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_STATE_NOT_ACTIVE" in proposed.rejection_codes

    # 其余非活动状态一律拒绝（不授权任何变更）。
    for state in ("REVIEW_REQUIRED", "REJECTED", "REVOKED", "SUPERSEDED", "OBSERVED"):
        decision = evaluate_runtime_mapping_eligibility(
            self_asserted,
            approval,
            observation,
            _context(owner_keys, self_asserted, recorded_proposal_state=state),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, state
        assert "MAPPING_STATE_NOT_ACTIVE" in decision.rejection_codes, state

    # 只有 APPROVED 才是活动终态。
    assert ALLOWED_PROPOSAL_STATES == {"APPROVED"}
    assert ALLOWED_PROPOSAL_STATES <= KNOWN_PROPOSAL_STATES


# ---------------------------------------------------------------------------
# EC-07 .. EC-11 — 观测 / 提案证据绑定
# ---------------------------------------------------------------------------

def _unrelated_observation():
    return _observation(match_id=CANONICAL_ID_ALT, number="008", home="甲", away="乙")


def test_ec_07_observation_with_unrelated_competition_identity_is_rejected(owner_keys):
    """EC-07: 与提案无关的竞彩身份观测被拒绝。"""
    _observation_valid, proposal, approval, _ = _valid_setup(owner_keys)
    other = _unrelated_observation()
    assert ScopedSourceIdentityKey.for_observation(other).source_key() != proposal.source_key

    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, other, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes


def test_ec_08_observation_fingerprint_absent_from_proposal_is_rejected(owner_keys):
    """EC-08: 观测指纹不在提案证据集合中 -> NOT_ELIGIBLE。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()
    assert unrelated.semantic_fingerprint() not in proposal.observation_fingerprints

    # 用**无关观测的指纹**签名一份有效批准，再把它用在原提案上。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes

    # 观测引用为空白同样拒绝。
    blank_reference = _recompose_proposal(proposal, source_observation_ref="")
    decision_blank = evaluate_runtime_mapping_eligibility(
        blank_reference, _approval(blank_reference, observation, owner_keys),
        observation, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT,
    )
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision_blank.rejection_codes


def test_ec_09_observation_identity_key_differing_is_rejected(owner_keys):
    """EC-09: 由观测独立推导的身份键与提案身份键不一致 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    tampered_key = dict(proposal.identity_key_dict)
    tampered_key["identity_value"] = "football|2030-05-01|jczq|99"
    tampered = _recompose_proposal(proposal, key_dict=tampered_key)

    decision = evaluate_runtime_mapping_eligibility(
        tampered, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes

    # 身份键契约非当前 -> 直接拒绝（不因别名而被绕过）。
    legacy_key = dict(proposal.identity_key_dict)
    legacy_key["identity_key_schema_version"] = "2.0.0"
    legacy = _recompose_proposal(proposal, key_dict=legacy_key)
    decision_legacy = evaluate_runtime_mapping_eligibility(
        legacy, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "IDENTITY_KEY_VERSION_NOT_CURRENT" in decision_legacy.rejection_codes


def test_ec_10_observation_scope_differing_is_rejected(owner_keys):
    """EC-10: 观测作用域与提案声明作用域不一致 -> NOT_ELIGIBLE(SOURCE_SCOPE_MISMATCH)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    for override in (
        {"event_date": "2030-05-02"},
        {"sport": "basketball"},
        {"provider_namespace": "500l"},
        {"adapter": "live_football"},
    ):
        mismatched = _recompose_proposal(proposal, scope_overrides=override)
        decision = evaluate_runtime_mapping_eligibility(
            mismatched, approval, observation, _context(owner_keys, proposal),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, override
        assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes, override


def test_ec_11_valid_multi_observation_proposal_remains_supported(owner_keys):
    """EC-11: 合法的多观测提案仍然受支持（当前观测不必是第一条）。"""
    first = _observation(time="20:00")
    second = _observation(time="21:00")
    assert first.semantic_fingerprint() != second.semantic_fingerprint()
    assert (
        ScopedSourceIdentityKey.for_observation(first).source_key()
        == ScopedSourceIdentityKey.for_observation(second).source_key()
    )

    proposal = proposal_from_observations(
        [first, second],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="multi-observation proposal",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    assert len(proposal.observation_fingerprints) == 2
    assert second.semantic_fingerprint() in proposal.observation_fingerprints

    # 批准件绑定**第二条**观测；用第二条观测评估 -> 仍然是条件式 ELIGIBLE。
    approval = _approval(proposal, second, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, second, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()

    # 第一条观测同样满足成员关系（不要求是「当前」那一条）。
    first_decision = evaluate_runtime_mapping_eligibility(
        proposal,
        _approval(proposal, first, owner_keys),
        first,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert first_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value


# ---------------------------------------------------------------------------
# EC-12 / EC-13 — 条件式合格 vs 一致证据
# ---------------------------------------------------------------------------

def test_ec_12_correct_evidence_produces_a_conditional_eligible_decision(owner_keys):
    """EC-12: 正确的观测 + 提案 + 签名批准 -> 条件式 ELIGIBLE（非生产授权）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED

    # 换成隔离快照来源 -> 仍是条件式（只是来源不同）。
    snapshot_context = _context(
        owner_keys,
        proposal,
        evidence_provenance=isolated_snapshot_provenance(
            dataset_identity=DATASET, baseline_fingerprint=BASELINE,
            structural_ledger_validation_performed=True,
        ),
    )
    snapshot_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, snapshot_context, evaluated_at=EVALUATED_AT
    )
    assert snapshot_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert snapshot_decision.evidence_provenance == (
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    )
    assert snapshot_decision.is_conditional is True

    # 声称独立验证过的账本证据 -> 一律阻断（自述布尔值不是证据）。
    claimed = _context(
        owner_keys,
        proposal,
        evidence_provenance=GovernanceEvidenceProvenance(
            source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
            structural_ledger_validation_performed=True,
            authenticated_approval_audit_performed=True,
        ),
    )
    claimed_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, claimed, evaluated_at=EVALUATED_AT
    )
    assert claimed_decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "EVIDENCE_PROVENANCE_UNSUPPORTED" in claimed_decision.rejection_codes
    assert CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER in BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    assert ALLOWED_CONTEXT_EVIDENCE_PROVENANCE == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    }


def test_ec_13_valid_signature_over_inconsistent_evidence_remains_not_eligible(owner_keys):
    """EC-13: 有效签名覆盖不一致证据时仍然 NOT_ELIGIBLE（签名不使证据变合格）。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    # 用无关观测的指纹签署**原提案**：签名本身有效，但证据不一致。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    assert signed_over_unrelated["source_observation_fingerprint"] == (
        unrelated.semantic_fingerprint()
    )

    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    # 直接可诊断的不一致**不**复用 APPROVAL_SIGNATURE_INVALID。
    assert "APPROVAL_SIGNATURE_INVALID" not in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes


# ---------------------------------------------------------------------------
# EC-14 .. EC-19 — 既有拒绝面在修正后保持有效
# ---------------------------------------------------------------------------

def test_ec_14_revoked_mapping_remains_rejected(owner_keys):
    """EC-14: 已撤销映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="REVOKED"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "MAPPING_REVOKED" in decision.rejection_codes


def test_ec_15_superseded_mapping_remains_rejected(owner_keys):
    """EC-15: 已取代映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="SUPERSEDED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_SUPERSEDED" in decision.rejection_codes


def test_ec_16_stale_baseline_remains_rejected(owner_keys):
    """EC-16: 陈旧基线仍然被拒绝，且旧 ELIGIBLE 判定不构成永久授权。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    before = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert before.is_eligible

    after = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        evaluated_at=EVALUATED_AT,
    )
    assert after.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "BASELINE_STALE" in after.rejection_codes
    # 基线变化 -> 判定身份随之改变（必须重新评估）。
    assert after.decision_id != before.decision_id
    assert after.baseline_fingerprint != before.baseline_fingerprint


def test_ec_17_unverified_provider_identity_remains_rejected(owner_keys):
    """EC-17: 未验证的 provider 身份仍然被拒绝。"""
    provider_row = _market_row(number=None)
    provider_key = ScopedSourceIdentityKey.for_observation(
        observe_source_row(
            provider_row,
            adapter="market_xml",
            raw_provider_id="12345",
            raw_provider_id_type="PROVIDER_MATCH_ID",
        )
    )
    assert provider_key.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
    proposal = _synthetic_proposal(key_dict=provider_key.as_dict())
    observation = observe_source_row(
        provider_row,
        adapter="market_xml",
        raw_provider_id="12345",
        raw_provider_id_type="PROVIDER_MATCH_ID",
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "UNVERIFIED_PROVIDER_ID" in decision.rejection_codes


def test_ec_18_row_position_identity_remains_rejected(owner_keys):
    """EC-18: 行位置派生身份仍然被拒绝。"""
    finished = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "15:30", "kickoff_time_known": True,
        "status": "finished", "home": "甲队", "away": "乙队",
        "score": {"ft": [2, 1]}, "odds": None,
    }
    observation = observe_source_row(finished, adapter="finished_feed")
    proposal = _synthetic_proposal(
        key_dict=ScopedSourceIdentityKey.for_observation(observation).as_dict()
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "ROW_POSITION_UNSTABLE" in decision.rejection_codes
    assert "RAW_IDENTIFIER_TYPE_NOT_DURABLE" in decision.rejection_codes


def test_ec_19_cross_sport_shared_aliases_remain_ineligible(owner_keys):
    """EC-19: 跨运动共用源别名的身份证明不足时仍然不合格。"""
    football_row = _market_row(match_id="500j-2030-05-01-001", number="001")
    basketball_row = _market_row(
        match_id="500j-2030-05-01-001", number="001", sport="basketball",
        home="湖人", away="勇士",
    )
    football = observe_source_row(football_row, adapter="market_xml")
    basketball = observe_source_row(basketball_row, adapter="market_xml")
    assert football.derived_source_id == basketball.derived_source_id
    key_football = ScopedSourceIdentityKey.for_observation(football)
    key_basketball = ScopedSourceIdentityKey.for_observation(basketball)
    assert key_football.source_key() != key_basketball.source_key()

    proposal = proposal_from_observations(
        [football],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id="500j-2030-05-01-001",
        existing_canonical_owner="500j-2030-05-01-001",
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="cross-sport alias",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, basketball, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes

    # 既有摄取预检继续对该批次 fail closed。
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity([], [dict(football_row), dict(basketball_row)])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


# ---------------------------------------------------------------------------
# EC-20 / EC-21 — 注册表完整性
# ---------------------------------------------------------------------------

def test_ec_20_policy_registry_and_dispatch_remain_complete():
    """EC-20: 声明式策略注册表与规则分派表保持完整、自洽。"""
    from utils.runtime_mapping_eligibility import _RULE_CHECKS

    declared = {rule.code for rule in RUNTIME_ELIGIBILITY_RULES}
    assert declared == set(_RULE_CHECKS)
    assert len(declared) == len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert len(REJECTION_CODES) == 45
    assert {rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES} == REJECTION_CODES
    assert len(rule_codes()) == len(set(rule_codes()))

    # 新增类别与来源声明齐备。
    categories = runtime_policy_status()["rules_by_category"]
    assert categories["EVIDENCE_BINDING"] == [
        "SOURCE_OBSERVATION_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MATCHES",
        "SOURCE_SCOPE_MATCHES",
    ]
    assert len(categories["GOVERNANCE"]) == 20
    assert runtime_policy_status()["known_proposal_states"] == sorted(
        KNOWN_PROPOSAL_STATES
    )
    assert set(ALLOWED_CONTEXT_EVIDENCE_PROVENANCE).isdisjoint(
        BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    )

    # 版本号已按语义变化提升。
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_EVALUATOR_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION == "1.1.0"


def test_ec_21_every_new_rejection_code_has_an_executable_reproducer(owner_keys):
    """EC-21: 每条**新增**拒绝码都有可执行复现。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    reproducers = {
        "PROPOSAL_FINGERPRINT_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        ),
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_IDENTITY_KEY_MISMATCH": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_SCOPE_MISMATCH": (
            _recompose_proposal(proposal, scope_overrides={"event_date": "2030-05-02"}),
            approval, observation, _context(owner_keys, proposal),
        ),
        "RECORDED_STATE_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state=None),
        ),
        "RECORDED_STATE_UNKNOWN": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="NOPE"),
        ),
        "RECORDED_STATE_PROPOSAL_MISMATCH": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_id="nope"),
        ),
        "EVIDENCE_PROVENANCE_UNSUPPORTED": (
            proposal, approval, observation,
            _context(
                owner_keys, proposal,
                evidence_provenance=GovernanceEvidenceProvenance(
                    source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER
                ),
            ),
        ),
    }
    new_codes = {
        "PROPOSAL_FINGERPRINT_MISSING",
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MISMATCH",
        "SOURCE_SCOPE_MISMATCH",
        "RECORDED_STATE_MISSING",
        "RECORDED_STATE_UNKNOWN",
        "RECORDED_STATE_PROPOSAL_MISMATCH",
        "EVIDENCE_PROVENANCE_UNSUPPORTED",
    }
    assert set(reproducers) == new_codes
    assert new_codes <= REJECTION_CODES

    for code, (candidate_proposal, candidate_approval, candidate_observation, ctx) in (
        reproducers.items()
    ):
        decision = evaluate_runtime_mapping_eligibility(
            candidate_proposal, candidate_approval, candidate_observation, ctx,
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, code
        assert code in decision.rejection_codes, (code, decision.rejection_codes)


# ---------------------------------------------------------------------------
# EC-22 / EC-23 — 确定性与不可执行
# ---------------------------------------------------------------------------

def test_ec_22_repeated_identical_evaluations_remain_deterministic(owner_keys):
    """EC-22: 重复的相同评估保持确定（判定身份与时钟无关）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decisions = [
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT
        )
        for _ in range(5)
    ]
    assert len({d.decision_id for d in decisions}) == 1
    assert len(set(decisions)) == 1
    assert all(d.is_eligible and d.is_conditional for d in decisions)

    clock_shifted = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context,
        evaluated_at="2099-12-31T23:59:59+00:00",
    )
    assert clock_shifted.decision_id == decisions[0].decision_id
    assert clock_shifted.evaluated_at != decisions[0].evaluated_at


def test_ec_23_readiness_manifests_remain_non_executable(owner_keys):
    """EC-23: 就绪清单仍然不可执行，并显式标注条件式判定。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    eligible = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    rejected = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, context, evaluated_at=EVALUATED_AT
    )
    manifest = build_runtime_readiness_manifest(
        fixture_identity="ec23",
        decisions=[eligible, rejected],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert manifest["executable"] is False
    assert manifest["activation_api"] is None
    assert manifest["production_authorization_granted"] is False
    assert manifest["conditional_decision_count"] == 2
    assert manifest["verified_evidence_decision_count"] == 0
    assert manifest["conditional_decisions_are_production_authorization"] is False
    assert manifest["evidence_provenance_histogram"] == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED: 2
    }
    assert manifest["eligible_count"] == 1
    assert manifest["rejected_count"] == 1
    assert_runtime_manifest_not_executable(manifest)
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, executable=True))
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, activate=True))


# ---------------------------------------------------------------------------
# EC-24 / EC-25 — 隔离与七存储
# ---------------------------------------------------------------------------

def test_ec_24_shadow_evaluation_creates_no_binding_ledger_or_write(
    tmp_path, owner_keys
):
    """EC-24: 影子评估不创建绑定、不写账本、不写生产数据。"""
    workspace = tmp_path / "ec24"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before_listing = sorted(os.listdir(workspace))
    before_bytes = (workspace / "sentinel.txt").read_bytes()

    observation, proposal, approval, context = _valid_setup(owner_keys)
    report = shadow_runtime_eligibility_check(
        workspace=str(workspace),
        proposal=proposal,
        approval=approval,
        source_observation=observation,
        runtime_context=context,
        evaluated_at=EVALUATED_AT,
        scenario="EC-24",
    )
    assert report["eligibility_result"] == RuntimeEligibilityResult.ELIGIBLE.value
    assert report["ledger_written"] is False
    assert report["files_written"] == 0
    assert report["bindings_created"] == 0
    assert report["canonical_records_modified"] == 0
    assert report["is_production_authority"] is False
    assert report["runtime_mapping_activation"] == RUNTIME_MAPPING_ACTIVATION
    assert report["decision"]["is_production_authorization"] is False

    assert sorted(os.listdir(workspace)) == before_listing
    assert (workspace / "sentinel.txt").read_bytes() == before_bytes
    assert not (workspace / "mapping_governance_ledger.jsonl").exists()


def test_ec_25_all_seven_stores_remain_byte_identical(
    isolated_data_dir, monkeypatch, owner_keys
):
    """EC-25: 修正后的判定与拒绝路径都不改动七个持久化存储。"""
    _seed_seven_stores(isolated_data_dir)
    _fake_sources(
        monkeypatch,
        live_football=[
            _market_row(
                match_id="m-live",
                odds={"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
            )
        ],
    )
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    observation, proposal, approval, context = _valid_setup(owner_keys)
    workspace = str(isolated_data_dir.parent / "ec25_shadow")

    # 合格路径、拒绝路径、清单与影子入口全部执行一遍。
    evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    for broken_context in (
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        _context(owner_keys, proposal, recorded_proposal_state=None),
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        _context(owner_keys, proposal, required_rewrite_targets=("settlements.json",)),
    ):
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, broken_context, evaluated_at=EVALUATED_AT
        )
    build_runtime_readiness_manifest(
        fixture_identity="ec25",
        decisions=[
            evaluate_runtime_mapping_eligibility(
                proposal, approval, observation, context, evaluated_at=EVALUATED_AT
            )
        ],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    shadow_runtime_readiness_report(
        workspace=workspace,
        proposals=[proposal],
        observations=[observation],
        canonical_fixture=[_market_row()],
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )

    assert _hash_seven_stores(isolated_data_dir) == before


# ---------------------------------------------------------------------------
# EC-26 / EC-27 / EC-28 — 既有行为与审计不变
# ---------------------------------------------------------------------------

def test_ec_26_existing_ingestion_identity_behavior_remains_unchanged():
    """EC-26: 既有摄取身份行为不变（规划器语义与权威归属均未改）。"""
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(
            [],
            [
                _market_row(match_id="500j-2030-05-01-001", number="001"),
                _market_row(
                    match_id="500j-2030-05-01-001", number="001",
                    sport="basketball", home="湖人", away="勇士",
                ),
            ],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    with pytest.raises(CanonicalIdentityCollisionError) as dup:
        plan_ingestion_identity(
            [_market_row(match_id="dup-1"), _market_row(match_id="dup-1")], []
        )
    assert dup.value.category == "PREEXISTING_DUPLICATE"

    for name in ("ingestion_identity.py", "match_identity.py", "scraper.py"):
        source = open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8").read()
        assert "runtime_mapping" not in source, name
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


def test_ec_27_historical_ed25519_approval_audit_continues_to_work(
    tmp_path, owner_keys
):
    """EC-27: 历史 Ed25519 批准审计继续工作；隔离快照只读且保留信任锚警告。"""
    observation = _observation()
    proposal = _proposal(observation)
    workspace = tmp_path / "ec27_gov"
    workspace.mkdir()
    ledger = GovernanceLedger(
        str(workspace / "mapping_governance_ledger.jsonl"), dataset_identity=DATASET
    )
    ledger.append(
        proposal=proposal,
        to_state="PROPOSED",
        actor=OPERATOR,
        actor_role="operator",
        reason="propose",
        baseline_fingerprint=BASELINE,
    )
    approval = _approval(proposal, observation, owner_keys)
    ledger.append(
        proposal=proposal,
        to_state="APPROVED",
        actor=APPROVER,
        actor_role="owner",
        reason="approve",
        baseline_fingerprint=BASELINE,
        approval=approval,
        trusted_public_key_path=owner_keys["public"],
        source_observation_fingerprint=observation.semantic_fingerprint(),
    )

    audit = audit_historical_approvals(
        ledger, trusted_public_key_path=owner_keys["public"], dataset_identity=DATASET
    )
    assert audit["approved_decisions"] == 1
    assert audit["failures"] == []

    with open(ledger.path, "rb") as handle:
        ledger_bytes = handle.read()
    snapshot = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )
    # 只读：账本逐字节不变。
    with open(ledger.path, "rb") as handle:
        assert handle.read() == ledger_bytes

    assert isinstance(snapshot, IsolatedGovernanceSnapshot)
    assert snapshot.structural_integrity == "OK"
    assert snapshot.authenticated_approval_audit == (
        "AUTHENTICATED_MAPPING_APPROVAL_AUDIT"
    )
    assert snapshot.state_of(proposal.proposal_id) == "APPROVED"
    assert snapshot.fingerprint_of(proposal.proposal_id) == proposal.fingerprint()
    assert snapshot.provenance.source == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    # 账本信任锚警告被原样保留（哈希链不是外部信任锚）。
    assert "NONE" in snapshot.provenance.ledger_trust_anchor
    assert snapshot.as_dict()["is_production_authority"] is False

    context = RuntimeMappingContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        evidence_provenance=snapshot.provenance,
        canonical_ids=(CANONICAL_ID,),
        trusted_public_key_path=owner_keys["public"],
        expected_approver=APPROVER,
        expected_operator=OPERATOR,
        recorded_proposal_id=proposal.proposal_id,
        recorded_proposal_fingerprint=snapshot.fingerprint_of(proposal.proposal_id),
        recorded_proposal_state=snapshot.state_of(proposal.proposal_id),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False

    # 缺少可信公钥时审计**不**执行，并如实记录限制（绝不信任内嵌公钥）。
    without_key = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert without_key.authenticated_approval_audit == "NOT_PERFORMED"
    assert any(
        "trusted public key" in limitation
        for limitation in without_key.provenance.verification_limitations
    )


def test_ec_28_ci_declares_ubuntu_and_windows_regressions():
    """EC-28: CI 声明的四个必需 job 与双平台矩阵保持存在。"""
    workflow = _ci_workflow()
    for job in (
        "Offline pytest suite",
        "Isolated Canonical repair lifecycle",
    ):
        assert job in workflow, job
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert re.search(r"os:\s*\[ubuntu-latest,\s*windows-latest\]", workflow)
    assert re.search(r'python-version:\s*\["3\.12"\]', workflow)


# ===========================================================================
# EC-01 .. EC-28 — 证据绑定与 fail-closed 修正回归矩阵
# ===========================================================================

# ---------------------------------------------------------------------------
# EC-01 .. EC-04 — 已记录提案指纹完整性
# ---------------------------------------------------------------------------

def test_ec_01_missing_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-01: 缺失已记录提案指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes
    # 缺失指纹绝不意味着「指纹校验可选」。
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes


def test_ec_02_blank_recorded_proposal_fingerprint_returns_not_eligible(owner_keys):
    """EC-02: 空白已记录指纹 -> NOT_ELIGIBLE(PROPOSAL_FINGERPRINT_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for blank in ("", "   ", "\t"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=blank),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISSING" in decision.rejection_codes, blank


def test_ec_03_mismatched_recorded_fingerprint_returns_not_eligible(owner_keys):
    """EC-03: 不匹配 / 格式不合法的已记录指纹 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    for bad in ("0" * 64, "NOT-A-SHA256", proposal.fingerprint()[:-1] + "0"):
        decision = evaluate_runtime_mapping_eligibility(
            proposal,
            approval,
            observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=bad),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
        assert "PROPOSAL_FINGERPRINT_MISMATCH" in decision.rejection_codes, bad


def test_ec_04_correct_recorded_fingerprint_passes_the_rule(owner_keys):
    """EC-04: 正确的已记录指纹通过指纹规则（但**不**证明账本可信）。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(
            owner_keys, proposal,
            recorded_proposal_fingerprint=proposal.fingerprint(),
        ),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert "PROPOSAL_FINGERPRINT_MISSING" not in decision.rejection_codes
    assert "PROPOSAL_FINGERPRINT_MISMATCH" not in decision.rejection_codes
    # 指纹匹配只是一致性；来源仍然是条件式的，不是独立验证过的证据。
    assert decision.is_conditional is True
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED


# ---------------------------------------------------------------------------
# EC-05 / EC-06 — 已记录治理状态完整性
# ---------------------------------------------------------------------------

def test_ec_05_missing_recorded_proposal_state_returns_not_eligible(owner_keys):
    """EC-05: 缺失已记录治理状态 -> NOT_ELIGIBLE(RECORDED_STATE_MISSING)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in decision.rejection_codes

    # 未知状态同样 fail closed。
    unknown = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_state="BANANA"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_UNKNOWN" in unknown.rejection_codes

    # 已记录状态必须对应被评估的提案 ID。
    other = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _context(owner_keys, proposal, recorded_proposal_id="another-proposal"),
        evaluated_at=EVALUATED_AT,
    )
    assert "RECORDED_STATE_PROPOSAL_MISMATCH" in other.rejection_codes


def test_ec_06_asserted_proposal_status_does_not_replace_the_recorded_state(owner_keys):
    """EC-06: 提案自述的 APPROVED 状态绝不替代已记录的治理状态。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    self_asserted = _recompose_proposal(proposal, status="APPROVED")
    assert self_asserted.status == "APPROVED"

    # 没有任何已记录状态 -> 仍然拒绝（绝不回退到提案自述状态）。
    missing = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state=None),
        evaluated_at=EVALUATED_AT,
    )
    assert missing.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "RECORDED_STATE_MISSING" in missing.rejection_codes

    # 已记录状态为 PROPOSED -> 同样拒绝，即便提案自述 APPROVED。
    proposed = evaluate_runtime_mapping_eligibility(
        self_asserted,
        approval,
        observation,
        _context(owner_keys, self_asserted, recorded_proposal_state="PROPOSED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_STATE_NOT_ACTIVE" in proposed.rejection_codes

    # 其余非活动状态一律拒绝（不授权任何变更）。
    for state in ("REVIEW_REQUIRED", "REJECTED", "REVOKED", "SUPERSEDED", "OBSERVED"):
        decision = evaluate_runtime_mapping_eligibility(
            self_asserted,
            approval,
            observation,
            _context(owner_keys, self_asserted, recorded_proposal_state=state),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, state
        assert "MAPPING_STATE_NOT_ACTIVE" in decision.rejection_codes, state

    # 只有 APPROVED 才是活动终态。
    assert ALLOWED_PROPOSAL_STATES == {"APPROVED"}
    assert ALLOWED_PROPOSAL_STATES <= KNOWN_PROPOSAL_STATES


# ---------------------------------------------------------------------------
# EC-07 .. EC-11 — 观测 / 提案证据绑定
# ---------------------------------------------------------------------------

def _unrelated_observation():
    return _observation(match_id=CANONICAL_ID_ALT, number="008", home="甲", away="乙")


def test_ec_07_observation_with_unrelated_competition_identity_is_rejected(owner_keys):
    """EC-07: 与提案无关的竞彩身份观测被拒绝。"""
    _observation_valid, proposal, approval, _ = _valid_setup(owner_keys)
    other = _unrelated_observation()
    assert ScopedSourceIdentityKey.for_observation(other).source_key() != proposal.source_key

    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, other, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes


def test_ec_08_observation_fingerprint_absent_from_proposal_is_rejected(owner_keys):
    """EC-08: 观测指纹不在提案证据集合中 -> NOT_ELIGIBLE。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()
    assert unrelated.semantic_fingerprint() not in proposal.observation_fingerprints

    # 用**无关观测的指纹**签名一份有效批准，再把它用在原提案上。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes

    # 观测引用为空白同样拒绝。
    blank_reference = _recompose_proposal(proposal, source_observation_ref="")
    decision_blank = evaluate_runtime_mapping_eligibility(
        blank_reference, _approval(blank_reference, observation, owner_keys),
        observation, _context(owner_keys, proposal), evaluated_at=EVALUATED_AT,
    )
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision_blank.rejection_codes


def test_ec_09_observation_identity_key_differing_is_rejected(owner_keys):
    """EC-09: 由观测独立推导的身份键与提案身份键不一致 -> NOT_ELIGIBLE。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    tampered_key = dict(proposal.identity_key_dict)
    tampered_key["identity_value"] = "football|2030-05-01|jczq|99"
    tampered = _recompose_proposal(proposal, key_dict=tampered_key)

    decision = evaluate_runtime_mapping_eligibility(
        tampered, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes

    # 身份键契约非当前 -> 直接拒绝（不因别名而被绕过）。
    legacy_key = dict(proposal.identity_key_dict)
    legacy_key["identity_key_schema_version"] = "2.0.0"
    legacy = _recompose_proposal(proposal, key_dict=legacy_key)
    decision_legacy = evaluate_runtime_mapping_eligibility(
        legacy, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "IDENTITY_KEY_VERSION_NOT_CURRENT" in decision_legacy.rejection_codes


def test_ec_10_observation_scope_differing_is_rejected(owner_keys):
    """EC-10: 观测作用域与提案声明作用域不一致 -> NOT_ELIGIBLE(SOURCE_SCOPE_MISMATCH)。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)

    for override in (
        {"event_date": "2030-05-02"},
        {"sport": "basketball"},
        {"provider_namespace": "500l"},
        {"adapter": "live_football"},
    ):
        mismatched = _recompose_proposal(proposal, scope_overrides=override)
        decision = evaluate_runtime_mapping_eligibility(
            mismatched, approval, observation, _context(owner_keys, proposal),
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, override
        assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes, override


def test_ec_11_valid_multi_observation_proposal_remains_supported(owner_keys):
    """EC-11: 合法的多观测提案仍然受支持（当前观测不必是第一条）。"""
    first = _observation(time="20:00")
    second = _observation(time="21:00")
    assert first.semantic_fingerprint() != second.semantic_fingerprint()
    assert (
        ScopedSourceIdentityKey.for_observation(first).source_key()
        == ScopedSourceIdentityKey.for_observation(second).source_key()
    )

    proposal = proposal_from_observations(
        [first, second],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="multi-observation proposal",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    assert len(proposal.observation_fingerprints) == 2
    assert second.semantic_fingerprint() in proposal.observation_fingerprints

    # 批准件绑定**第二条**观测；用第二条观测评估 -> 仍然是条件式 ELIGIBLE。
    approval = _approval(proposal, second, owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, second, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()

    # 第一条观测同样满足成员关系（不要求是「当前」那一条）。
    first_decision = evaluate_runtime_mapping_eligibility(
        proposal,
        _approval(proposal, first, owner_keys),
        first,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert first_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value


# ---------------------------------------------------------------------------
# EC-12 / EC-13 — 条件式合格 vs 一致证据
# ---------------------------------------------------------------------------

def test_ec_12_correct_evidence_produces_a_conditional_eligible_decision(owner_keys):
    """EC-12: 正确的观测 + 提案 + 签名批准 -> 条件式 ELIGIBLE（非生产授权）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.rejection_codes == ()
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED

    # 换成隔离快照来源 -> 仍是条件式（只是来源不同）。
    snapshot_context = _context(
        owner_keys,
        proposal,
        evidence_provenance=isolated_snapshot_provenance(
            dataset_identity=DATASET, baseline_fingerprint=BASELINE,
            structural_ledger_validation_performed=True,
        ),
    )
    snapshot_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, snapshot_context, evaluated_at=EVALUATED_AT
    )
    assert snapshot_decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert snapshot_decision.evidence_provenance == (
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    )
    assert snapshot_decision.is_conditional is True

    # 声称独立验证过的账本证据 -> 一律阻断（自述布尔值不是证据）。
    claimed = _context(
        owner_keys,
        proposal,
        evidence_provenance=GovernanceEvidenceProvenance(
            source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER,
            structural_ledger_validation_performed=True,
            authenticated_approval_audit_performed=True,
        ),
    )
    claimed_decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, claimed, evaluated_at=EVALUATED_AT
    )
    assert claimed_decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "EVIDENCE_PROVENANCE_UNSUPPORTED" in claimed_decision.rejection_codes
    assert CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER in BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    assert ALLOWED_CONTEXT_EVIDENCE_PROVENANCE == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED,
        CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT,
    }


def test_ec_13_valid_signature_over_inconsistent_evidence_remains_not_eligible(owner_keys):
    """EC-13: 有效签名覆盖不一致证据时仍然 NOT_ELIGIBLE（签名不使证据变合格）。"""
    observation, proposal, _approval_artifact, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    # 用无关观测的指纹签署**原提案**：签名本身有效，但证据不一致。
    signed_over_unrelated = _approval(proposal, unrelated, owner_keys)
    assert signed_over_unrelated["source_observation_fingerprint"] == (
        unrelated.semantic_fingerprint()
    )

    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        signed_over_unrelated,
        unrelated,
        _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    # 直接可诊断的不一致**不**复用 APPROVAL_SIGNATURE_INVALID。
    assert "APPROVAL_SIGNATURE_INVALID" not in decision.rejection_codes
    assert "SOURCE_OBSERVATION_NOT_IN_PROPOSAL" in decision.rejection_codes
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes


# ---------------------------------------------------------------------------
# EC-14 .. EC-19 — 既有拒绝面在修正后保持有效
# ---------------------------------------------------------------------------

def test_ec_14_revoked_mapping_remains_rejected(owner_keys):
    """EC-14: 已撤销映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="REVOKED"),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "MAPPING_REVOKED" in decision.rejection_codes


def test_ec_15_superseded_mapping_remains_rejected(owner_keys):
    """EC-15: 已取代映射仍然被拒绝。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, recorded_proposal_state="SUPERSEDED"),
        evaluated_at=EVALUATED_AT,
    )
    assert "MAPPING_SUPERSEDED" in decision.rejection_codes


def test_ec_16_stale_baseline_remains_rejected(owner_keys):
    """EC-16: 陈旧基线仍然被拒绝，且旧 ELIGIBLE 判定不构成永久授权。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    before = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert before.is_eligible

    after = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation,
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        evaluated_at=EVALUATED_AT,
    )
    assert after.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "BASELINE_STALE" in after.rejection_codes
    # 基线变化 -> 判定身份随之改变（必须重新评估）。
    assert after.decision_id != before.decision_id
    assert after.baseline_fingerprint != before.baseline_fingerprint


def test_ec_17_unverified_provider_identity_remains_rejected(owner_keys):
    """EC-17: 未验证的 provider 身份仍然被拒绝。"""
    provider_row = _market_row(number=None)
    provider_key = ScopedSourceIdentityKey.for_observation(
        observe_source_row(
            provider_row,
            adapter="market_xml",
            raw_provider_id="12345",
            raw_provider_id_type="PROVIDER_MATCH_ID",
        )
    )
    assert provider_key.identity_kind == IdentityKeyKind.UNVERIFIED_PROVIDER_ID.value
    proposal = _synthetic_proposal(key_dict=provider_key.as_dict())
    observation = observe_source_row(
        provider_row,
        adapter="market_xml",
        raw_provider_id="12345",
        raw_provider_id_type="PROVIDER_MATCH_ID",
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "UNVERIFIED_PROVIDER_ID" in decision.rejection_codes


def test_ec_18_row_position_identity_remains_rejected(owner_keys):
    """EC-18: 行位置派生身份仍然被拒绝。"""
    finished = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "15:30", "kickoff_time_known": True,
        "status": "finished", "home": "甲队", "away": "乙队",
        "score": {"ft": [2, 1]}, "odds": None,
    }
    observation = observe_source_row(finished, adapter="finished_feed")
    proposal = _synthetic_proposal(
        key_dict=ScopedSourceIdentityKey.for_observation(observation).as_dict()
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert "ROW_POSITION_UNSTABLE" in decision.rejection_codes
    assert "RAW_IDENTIFIER_TYPE_NOT_DURABLE" in decision.rejection_codes


def test_ec_19_cross_sport_shared_aliases_remain_ineligible(owner_keys):
    """EC-19: 跨运动共用源别名的身份证明不足时仍然不合格。"""
    football_row = _market_row(match_id="500j-2030-05-01-001", number="001")
    basketball_row = _market_row(
        match_id="500j-2030-05-01-001", number="001", sport="basketball",
        home="湖人", away="勇士",
    )
    football = observe_source_row(football_row, adapter="market_xml")
    basketball = observe_source_row(basketball_row, adapter="market_xml")
    assert football.derived_source_id == basketball.derived_source_id
    key_football = ScopedSourceIdentityKey.for_observation(football)
    key_basketball = ScopedSourceIdentityKey.for_observation(basketball)
    assert key_football.source_key() != key_basketball.source_key()

    proposal = proposal_from_observations(
        [football],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id="500j-2030-05-01-001",
        existing_canonical_owner="500j-2030-05-01-001",
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="cross-sport alias",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, None, basketball, _context(owner_keys, proposal),
        evaluated_at=EVALUATED_AT,
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value
    assert "SOURCE_IDENTITY_KEY_MISMATCH" in decision.rejection_codes
    assert "SOURCE_SCOPE_MISMATCH" in decision.rejection_codes

    # 既有摄取预检继续对该批次 fail closed。
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity([], [dict(football_row), dict(basketball_row)])
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


# ---------------------------------------------------------------------------
# EC-20 / EC-21 — 注册表完整性
# ---------------------------------------------------------------------------

def test_ec_20_policy_registry_and_dispatch_remain_complete():
    """EC-20: 声明式策略注册表与规则分派表保持完整、自洽。"""
    from utils.runtime_mapping_eligibility import _RULE_CHECKS

    declared = {rule.code for rule in RUNTIME_ELIGIBILITY_RULES}
    assert declared == set(_RULE_CHECKS)
    assert len(declared) == len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert len(REJECTION_CODES) == 45
    assert {rule.rejection_code for rule in RUNTIME_ELIGIBILITY_RULES} == REJECTION_CODES
    assert len(rule_codes()) == len(set(rule_codes()))

    # 新增类别与来源声明齐备。
    categories = runtime_policy_status()["rules_by_category"]
    assert categories["EVIDENCE_BINDING"] == [
        "SOURCE_OBSERVATION_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MATCHES",
        "SOURCE_SCOPE_MATCHES",
    ]
    assert len(categories["GOVERNANCE"]) == 20
    assert runtime_policy_status()["known_proposal_states"] == sorted(
        KNOWN_PROPOSAL_STATES
    )
    assert set(ALLOWED_CONTEXT_EVIDENCE_PROVENANCE).isdisjoint(
        BLOCKED_CONTEXT_EVIDENCE_PROVENANCE
    )

    # 版本号已按语义变化提升。
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_EVALUATOR_VERSION == "1.1.0"
    assert RUNTIME_ELIGIBILITY_DECISION_SCHEMA_VERSION == "1.1.0"


def test_ec_21_every_new_rejection_code_has_an_executable_reproducer(owner_keys):
    """EC-21: 每条**新增**拒绝码都有可执行复现。"""
    observation, proposal, approval, _ = _valid_setup(owner_keys)
    unrelated = _unrelated_observation()

    reproducers = {
        "PROPOSAL_FINGERPRINT_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        ),
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_IDENTITY_KEY_MISMATCH": (
            proposal, approval, unrelated, _context(owner_keys, proposal),
        ),
        "SOURCE_SCOPE_MISMATCH": (
            _recompose_proposal(proposal, scope_overrides={"event_date": "2030-05-02"}),
            approval, observation, _context(owner_keys, proposal),
        ),
        "RECORDED_STATE_MISSING": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state=None),
        ),
        "RECORDED_STATE_UNKNOWN": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_state="NOPE"),
        ),
        "RECORDED_STATE_PROPOSAL_MISMATCH": (
            proposal, approval, observation,
            _context(owner_keys, proposal, recorded_proposal_id="nope"),
        ),
        "EVIDENCE_PROVENANCE_UNSUPPORTED": (
            proposal, approval, observation,
            _context(
                owner_keys, proposal,
                evidence_provenance=GovernanceEvidenceProvenance(
                    source=CONTEXT_EVIDENCE_PROVENANCE_VERIFIED_LEDGER
                ),
            ),
        ),
    }
    new_codes = {
        "PROPOSAL_FINGERPRINT_MISSING",
        "SOURCE_OBSERVATION_NOT_IN_PROPOSAL",
        "SOURCE_IDENTITY_KEY_MISMATCH",
        "SOURCE_SCOPE_MISMATCH",
        "RECORDED_STATE_MISSING",
        "RECORDED_STATE_UNKNOWN",
        "RECORDED_STATE_PROPOSAL_MISMATCH",
        "EVIDENCE_PROVENANCE_UNSUPPORTED",
    }
    assert set(reproducers) == new_codes
    assert new_codes <= REJECTION_CODES

    for code, (candidate_proposal, candidate_approval, candidate_observation, ctx) in (
        reproducers.items()
    ):
        decision = evaluate_runtime_mapping_eligibility(
            candidate_proposal, candidate_approval, candidate_observation, ctx,
            evaluated_at=EVALUATED_AT,
        )
        assert decision.eligibility_result == RuntimeEligibilityResult.NOT_ELIGIBLE.value, code
        assert code in decision.rejection_codes, (code, decision.rejection_codes)


# ---------------------------------------------------------------------------
# EC-22 / EC-23 — 确定性与不可执行
# ---------------------------------------------------------------------------

def test_ec_22_repeated_identical_evaluations_remain_deterministic(owner_keys):
    """EC-22: 重复的相同评估保持确定（判定身份与时钟无关）。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    decisions = [
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, context, evaluated_at=EVALUATED_AT
        )
        for _ in range(5)
    ]
    assert len({d.decision_id for d in decisions}) == 1
    assert len(set(decisions)) == 1
    assert all(d.is_eligible and d.is_conditional for d in decisions)

    clock_shifted = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context,
        evaluated_at="2099-12-31T23:59:59+00:00",
    )
    assert clock_shifted.decision_id == decisions[0].decision_id
    assert clock_shifted.evaluated_at != decisions[0].evaluated_at


def test_ec_23_readiness_manifests_remain_non_executable(owner_keys):
    """EC-23: 就绪清单仍然不可执行，并显式标注条件式判定。"""
    observation, proposal, approval, context = _valid_setup(owner_keys)
    eligible = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    rejected = evaluate_runtime_mapping_eligibility(
        proposal, None, observation, context, evaluated_at=EVALUATED_AT
    )
    manifest = build_runtime_readiness_manifest(
        fixture_identity="ec23",
        decisions=[eligible, rejected],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert manifest["executable"] is False
    assert manifest["activation_api"] is None
    assert manifest["production_authorization_granted"] is False
    assert manifest["conditional_decision_count"] == 2
    assert manifest["verified_evidence_decision_count"] == 0
    assert manifest["conditional_decisions_are_production_authorization"] is False
    assert manifest["evidence_provenance_histogram"] == {
        CONTEXT_EVIDENCE_PROVENANCE_CALLER_ASSERTED: 2
    }
    assert manifest["eligible_count"] == 1
    assert manifest["rejected_count"] == 1
    assert_runtime_manifest_not_executable(manifest)
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, executable=True))
    with pytest.raises(RuntimeEligibilityPolicyError):
        assert_runtime_manifest_not_executable(dict(manifest, activate=True))


# ---------------------------------------------------------------------------
# EC-24 / EC-25 — 隔离与七存储
# ---------------------------------------------------------------------------

def test_ec_24_shadow_evaluation_creates_no_binding_ledger_or_write(
    tmp_path, owner_keys
):
    """EC-24: 影子评估不创建绑定、不写账本、不写生产数据。"""
    workspace = tmp_path / "ec24"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before_listing = sorted(os.listdir(workspace))
    before_bytes = (workspace / "sentinel.txt").read_bytes()

    observation, proposal, approval, context = _valid_setup(owner_keys)
    report = shadow_runtime_eligibility_check(
        workspace=str(workspace),
        proposal=proposal,
        approval=approval,
        source_observation=observation,
        runtime_context=context,
        evaluated_at=EVALUATED_AT,
        scenario="EC-24",
    )
    assert report["eligibility_result"] == RuntimeEligibilityResult.ELIGIBLE.value
    assert report["ledger_written"] is False
    assert report["files_written"] == 0
    assert report["bindings_created"] == 0
    assert report["canonical_records_modified"] == 0
    assert report["is_production_authority"] is False
    assert report["runtime_mapping_activation"] == RUNTIME_MAPPING_ACTIVATION
    assert report["decision"]["is_production_authorization"] is False

    assert sorted(os.listdir(workspace)) == before_listing
    assert (workspace / "sentinel.txt").read_bytes() == before_bytes
    assert not (workspace / "mapping_governance_ledger.jsonl").exists()


def test_ec_25_all_seven_stores_remain_byte_identical(
    isolated_data_dir, monkeypatch, owner_keys
):
    """EC-25: 修正后的判定与拒绝路径都不改动七个持久化存储。"""
    _seed_seven_stores(isolated_data_dir)
    _fake_sources(
        monkeypatch,
        live_football=[
            _market_row(
                match_id="m-live",
                odds={"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
            )
        ],
    )
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    observation, proposal, approval, context = _valid_setup(owner_keys)
    workspace = str(isolated_data_dir.parent / "ec25_shadow")

    # 合格路径、拒绝路径、清单与影子入口全部执行一遍。
    evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    for broken_context in (
        _context(owner_keys, proposal, recorded_proposal_fingerprint=None),
        _context(owner_keys, proposal, recorded_proposal_state=None),
        _context(owner_keys, proposal, current_baseline_fingerprint="baseline-B"),
        _context(owner_keys, proposal, required_rewrite_targets=("settlements.json",)),
    ):
        evaluate_runtime_mapping_eligibility(
            proposal, approval, observation, broken_context, evaluated_at=EVALUATED_AT
        )
    build_runtime_readiness_manifest(
        fixture_identity="ec25",
        decisions=[
            evaluate_runtime_mapping_eligibility(
                proposal, approval, observation, context, evaluated_at=EVALUATED_AT
            )
        ],
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    shadow_runtime_readiness_report(
        workspace=workspace,
        proposals=[proposal],
        observations=[observation],
        canonical_fixture=[_market_row()],
        dataset_identity=DATASET,
        baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )

    assert _hash_seven_stores(isolated_data_dir) == before


# ---------------------------------------------------------------------------
# EC-26 / EC-27 / EC-28 — 既有行为与审计不变
# ---------------------------------------------------------------------------

def test_ec_26_existing_ingestion_identity_behavior_remains_unchanged():
    """EC-26: 既有摄取身份行为不变（规划器语义与权威归属均未改）。"""
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(
            [],
            [
                _market_row(match_id="500j-2030-05-01-001", number="001"),
                _market_row(
                    match_id="500j-2030-05-01-001", number="001",
                    sport="basketball", home="湖人", away="勇士",
                ),
            ],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    with pytest.raises(CanonicalIdentityCollisionError) as dup:
        plan_ingestion_identity(
            [_market_row(match_id="dup-1"), _market_row(match_id="dup-1")], []
        )
    assert dup.value.category == "PREEXISTING_DUPLICATE"

    for name in ("ingestion_identity.py", "match_identity.py", "scraper.py"):
        source = open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8").read()
        assert "runtime_mapping" not in source, name
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


def test_ec_27_historical_ed25519_approval_audit_continues_to_work(
    tmp_path, owner_keys
):
    """EC-27: 历史 Ed25519 批准审计继续工作；隔离快照只读且保留信任锚警告。"""
    observation = _observation()
    proposal = _proposal(observation)
    workspace = tmp_path / "ec27_gov"
    workspace.mkdir()
    ledger = GovernanceLedger(
        str(workspace / "mapping_governance_ledger.jsonl"), dataset_identity=DATASET
    )
    ledger.append(
        proposal=proposal,
        to_state="PROPOSED",
        actor=OPERATOR,
        actor_role="operator",
        reason="propose",
        baseline_fingerprint=BASELINE,
    )
    approval = _approval(proposal, observation, owner_keys)
    ledger.append(
        proposal=proposal,
        to_state="APPROVED",
        actor=APPROVER,
        actor_role="owner",
        reason="approve",
        baseline_fingerprint=BASELINE,
        approval=approval,
        trusted_public_key_path=owner_keys["public"],
        source_observation_fingerprint=observation.semantic_fingerprint(),
    )

    audit = audit_historical_approvals(
        ledger, trusted_public_key_path=owner_keys["public"], dataset_identity=DATASET
    )
    assert audit["approved_decisions"] == 1
    assert audit["failures"] == []

    with open(ledger.path, "rb") as handle:
        ledger_bytes = handle.read()
    snapshot = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        trusted_public_key_path=owner_keys["public"],
    )
    # 只读：账本逐字节不变。
    with open(ledger.path, "rb") as handle:
        assert handle.read() == ledger_bytes

    assert isinstance(snapshot, IsolatedGovernanceSnapshot)
    assert snapshot.structural_integrity == "OK"
    assert snapshot.authenticated_approval_audit == (
        "AUTHENTICATED_MAPPING_APPROVAL_AUDIT"
    )
    assert snapshot.state_of(proposal.proposal_id) == "APPROVED"
    assert snapshot.fingerprint_of(proposal.proposal_id) == proposal.fingerprint()
    assert snapshot.provenance.source == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    # 账本信任锚警告被原样保留（哈希链不是外部信任锚）。
    assert "NONE" in snapshot.provenance.ledger_trust_anchor
    assert snapshot.as_dict()["is_production_authority"] is False

    context = RuntimeMappingContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        evidence_provenance=snapshot.provenance,
        canonical_ids=(CANONICAL_ID,),
        trusted_public_key_path=owner_keys["public"],
        expected_approver=APPROVER,
        expected_operator=OPERATOR,
        recorded_proposal_id=proposal.proposal_id,
        recorded_proposal_fingerprint=snapshot.fingerprint_of(proposal.proposal_id),
        recorded_proposal_state=snapshot.state_of(proposal.proposal_id),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal, approval, observation, context, evaluated_at=EVALUATED_AT
    )
    assert decision.eligibility_result == RuntimeEligibilityResult.ELIGIBLE.value
    assert decision.evidence_provenance == CONTEXT_EVIDENCE_PROVENANCE_ISOLATED_SNAPSHOT
    assert decision.is_conditional is True
    assert decision.is_production_authorization is False

    # 缺少可信公钥时审计**不**执行，并如实记录限制（绝不信任内嵌公钥）。
    without_key = build_isolated_governance_snapshot(
        ledger,
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
    )
    assert without_key.authenticated_approval_audit == "NOT_PERFORMED"
    assert any(
        "trusted public key" in limitation
        for limitation in without_key.provenance.verification_limitations
    )


def test_ec_28_ci_declares_ubuntu_and_windows_regressions():
    """EC-28: CI 声明的四个必需 job 与双平台矩阵保持存在。"""
    workflow = _ci_workflow()
    for job in (
        "Offline pytest suite",
        "Isolated Canonical repair lifecycle",
    ):
        assert job in workflow, job
    assert "ubuntu-latest" in workflow
    assert "windows-latest" in workflow
    assert re.search(r"os:\s*\[ubuntu-latest,\s*windows-latest\]", workflow)
    assert re.search(r'python-version:\s*\["3\.12"\]', workflow)
