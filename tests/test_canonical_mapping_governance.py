"""
Canonical 映射治理测试套件（PG-09 .. PG-30）
==========================================
覆盖：
- 提案不修改 Canonical 数据；歧义提案永不活动
- 冲突归属拒绝、重复提交幂等、非法迁移拒绝
- 账本哈希链断裂 / 重放检测、陈旧基线、撤销与取代的历史保留
- Ed25519 所有者批准信任边界（未签名 / 不可信 / 被篡改一律拒绝）
- 治理工件仅允许写入隔离工作区；生产数据逐字节不变
- 既有摄取预检、市场准入、终态保护、修复授权保持不变
- 跨进程账本并发安全；refresh 从不写账本

全部使用隔离的合成夹具，绝不访问网络，绝不执行生产修复。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import config
from tools.mapping_governance_shadow import (
    SHADOW_LEDGER_NAME,
    canonical_baseline_fingerprint,
    demo_observations,
    shadow_validate,
)
from utils import fetcher_500, scraper
from utils.daily_loader import load_json, save_json
from utils.evaluation_rows import build_evaluation_row, get_all_evaluation_rows
from utils.ingestion_identity import CanonicalIdentityCollisionError
from utils.mapping_governance import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    APPROVAL_ALGORITHM,
    GENESIS_PREVIOUS_HASH,
    GOVERNANCE_SCHEMA_VERSION,
    MAPPING_APPROVAL_PURPOSE,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    ConflictingBindingError,
    GovernanceLedger,
    InvalidTransitionError,
    LedgerIntegrityError,
    LedgerPathError,
    LedgerReplayError,
    MappingState,
    ProposalAction,
    ProposalValidationError,
    SignatureVerificationError,
    StaleBaselineError,
    build_approval_body,
    generate_mapping_owner_keypair,
    governance_rule_for,
    governance_status,
    proposal_for_conflict,
    proposal_from_observations,
    reachable_states,
    sign_mapping_approval,
    validate_proposal,
    validate_transition,
    verify_mapping_approval,
)
from utils.odds_snapshots import build_snapshot as build_odds_snapshot
from utils.prediction_snapshots import build_snapshot as build_pred_snapshot
from utils.settlements import build_settlement, extract_final_score
from utils.source_identity import (
    detect_all_conflicts,
    observe_source_row,
)

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
FOOTBALL_1X2 = {"home_win": 1.90, "draw": 3.40, "away_win": 3.80}
DATASET = "synthetic-governance-fixture"

SEVEN_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def owner_keys(tmp_path_factory):
    """在仓库之外生成一对所有者 Ed25519 密钥。"""
    base = tmp_path_factory.mktemp("mapping_owner_keys")
    private = str(base / "owner_private.pem")
    public = str(base / "owner_public.pem")
    info = generate_mapping_owner_keypair(private, public)
    return {"private": private, "public": public, "public_key_id": info["public_key_id"]}


@pytest.fixture(scope="module")
def other_keys(tmp_path_factory):
    """另一对不可信密钥（用于拒绝测试）。"""
    base = tmp_path_factory.mktemp("untrusted_owner_keys")
    private = str(base / "other_private.pem")
    public = str(base / "other_public.pem")
    generate_mapping_owner_keypair(private, public)
    return {"private": private, "public": public}


def _ledger(tmp_path, name="gov", dataset=DATASET):
    workspace = tmp_path / name
    workspace.mkdir(parents=True, exist_ok=True)
    return GovernanceLedger(str(workspace / SHADOW_LEDGER_NAME), dataset_identity=dataset)


def _row(match_id="500l-2030-05-01-007", *, sport="football", number="007",
         home="皇马", away="巴萨", date="2030-05-01", time="20:00", odds=None,
         status="upcoming", score=None):
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


def _observation(canonical_id="canon-keep", **kwargs):
    return observe_source_row(
        _row(**kwargs), adapter="live_football", canonical_match_id=canonical_id
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


def _approve(
    ledger,
    proposal,
    observation,
    keys,
    *,
    baseline,
    approver="owner-1",
    operator="operator-1",
    private_key=None,
    trusted_key=None,
    dataset=DATASET,
):
    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=dataset,
        operator=operator,
        approver=approver,
        approved_at="2030-01-01T00:00:00+00:00",
        reason="owner approves the proposed binding",
    )
    approval = sign_mapping_approval(private_key or keys["private"], body)
    return ledger.append(
        proposal=proposal,
        to_state=MappingState.APPROVED.value,
        actor=approver,
        actor_role="owner",
        reason="owner approves the proposed binding",
        baseline_fingerprint=baseline,
        approval=approval,
        trusted_public_key_path=trusted_key or keys["public"],
        source_observation_fingerprint=observation.semantic_fingerprint(),
    )


def _seed_seven_stores(data_dir):
    match = {
        "id": "old-1", "sport": "football", "league": "西甲", "date": "2030-05-01",
        "time": "12:00", "status": "finished", "score": {"ft": [1, 0]},
        "home": "皇马", "away": "巴萨", "odds": dict(FOOTBALL_1X2),
        "market_tracked": True, "home_team": {"elo": 1500}, "away_team": {"elo": 1500},
    }
    prediction = {
        "probabilities": {"home_win": 0.5, "draw": 0.3, "away_win": 0.2},
        "expected_goals": {"home": 1.0, "away": 1.0},
        "top_scores": [],
    }
    generated = datetime(2030, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
    snapshot = build_pred_snapshot(match, prediction, generated_at=generated)
    odds_snapshot = build_odds_snapshot(match, captured_at=generated)
    settlement = build_settlement(snapshot, match, extract_final_score(match))
    evaluation_row = build_evaluation_row(snapshot, settlement)

    save_json("daily_matches.json", {"meta": {}, "matches": [match]})
    save_json("team_strength.json",
              {"teams": {"皇马": {"elo_rating": 1500}, "巴萨": {"elo_rating": 1500}}})
    save_json("calibrated.json", {"ids": ["old-1"]})
    save_json("prediction_snapshots.json",
              {"version": 1, "snapshots": {snapshot["snapshot_id"]: snapshot}})
    save_json("odds_snapshots.json",
              {"version": 1, "snapshots": {odds_snapshot["snapshot_id"]: odds_snapshot}})
    save_json("settlements.json",
              {"version": 1, "settlements": {settlement["settlement_id"]: settlement}})
    save_json("evaluation_rows.json",
              {"version": 1, "rows": {evaluation_row["evaluation_id"]: evaluation_row}})
    return match


def _hash_all(data_dir):
    out = {}
    for name in SEVEN_STORES:
        with open(os.path.join(str(data_dir), name), "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# PG-09
# ---------------------------------------------------------------------------

def test_pg_09_valid_proposal_does_not_modify_canonical_data(isolated_data_dir, tmp_path):
    """PG-09: 合法提案与账本记录绝不修改任何 Canonical / 历史数据。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)

    canonical_fixture = [
        {"id": "canon-keep", "sport": "football", "date": "2030-05-01",
         "home": "皇马", "away": "巴萨", "jczq_no": "007", "market_tracked": True}
    ]
    fixture_before = json.dumps(canonical_fixture, sort_keys=True)
    baseline = canonical_baseline_fingerprint(canonical_fixture)

    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)

    entry, appended = ledger.append(
        proposal=proposal, to_state=MappingState.PROPOSED.value,
        actor="operator-1", actor_role="operator", reason="proposed",
        baseline_fingerprint=baseline,
    )

    assert appended is True
    assert entry.to_state == MappingState.PROPOSED.value
    assert ledger.state_of(proposal.proposal_id) == MappingState.PROPOSED.value

    # 提案与账本都没有触碰任何持久化存储或 Canonical 夹具。
    assert _hash_all(isolated_data_dir) == before
    assert json.dumps(canonical_fixture, sort_keys=True) == fixture_before
    assert ledger.active_bindings() == {}


# ---------------------------------------------------------------------------
# PG-10
# ---------------------------------------------------------------------------

def test_pg_10_ambiguous_proposal_cannot_become_active(tmp_path):
    """PG-10: 歧义提案绝不会自动变成活动状态；进入 APPROVED 必须经可验证的所有者决策。"""
    observations, _rows, _fixture = demo_observations()
    conflicts = detect_all_conflicts(observations)
    assert conflicts, "demo fixture must produce conflicts"

    conflict_proposals = []
    for conflict in conflicts:
        rule = governance_rule_for(conflict.category)
        assert rule is not None
        # 治理规则绝不把任何冲突直接指向 APPROVED。
        assert rule["target_state"] != MappingState.APPROVED.value
        members = [
            o for o in observations if o.observation_id in set(conflict.observation_ids)
        ]
        proposal = proposal_for_conflict(
            conflict, members,
            ingestion_plan_reference="planner:REJECTED",
            expected_baseline_fingerprint="baseline-A",
        )
        conflict_proposals.append(proposal)
        assert proposal.status == MappingState.PROPOSED.value
        assert not proposal.is_binding_action
        assert proposal.requested_action != (
            ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value
        )

    # OBSERVED / REJECTED 都不能直接到达 APPROVED。
    for state in ("OBSERVED", "REJECTED", "REVOKED", "SUPERSEDED"):
        assert MappingState.APPROVED.value not in ALLOWED_TRANSITIONS[state]
        with pytest.raises(InvalidTransitionError):
            validate_transition(state, MappingState.APPROVED.value)

    # REVIEW_REQUIRED -> APPROVED 虽在迁移表中，但**必须**有可验证的所有者决策，
    # 否则一律拒绝：歧义提案不会因为「处于 REVIEW_REQUIRED」而自动变为活动。
    ledger = _ledger(tmp_path)
    proposal = conflict_proposals[0]
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint="baseline-A")
    ledger.append(proposal=proposal, to_state=MappingState.REVIEW_REQUIRED.value,
                  actor="operator-1", actor_role="operator", reason="needs review",
                  baseline_fingerprint="baseline-A")
    assert ledger.state_of(proposal.proposal_id) == MappingState.REVIEW_REQUIRED.value
    assert ledger.active_bindings() == {}

    # (a) 非所有者角色
    with pytest.raises(SignatureVerificationError):
        ledger.append(proposal=proposal, to_state=MappingState.APPROVED.value,
                      actor="operator-1", actor_role="operator", reason="self approve",
                      baseline_fingerprint="baseline-A")
    # (b) 所有者角色但没有批准件
    with pytest.raises(SignatureVerificationError):
        ledger.append(proposal=proposal, to_state=MappingState.APPROVED.value,
                      actor="owner-1", actor_role="owner", reason="no artifact",
                      baseline_fingerprint="baseline-A", approval=None,
                      trusted_public_key_path="unused")
    assert ledger.state_of(proposal.proposal_id) == MappingState.REVIEW_REQUIRED.value
    assert ledger.active_bindings() == {}

    # 也无法直接构造一个 APPROVED 提案。
    observation = _observation()
    with pytest.raises(ProposalValidationError):
        proposal_from_observations(
            [observation],
            requested_action=ProposalAction.FLAG_AMBIGUOUS_IDENTITY.value,
            candidate_canonical_id=None,
            existing_canonical_owner=None,
            ingestion_plan_reference="planner:REJECTED",
            expected_baseline_fingerprint="baseline-A",
            reason="ambiguous",
            identity_evidence_type="source_alias",
            status=MappingState.APPROVED.value,
        )


# ---------------------------------------------------------------------------
# PG-11
# ---------------------------------------------------------------------------

def test_pg_11_conflicting_ownership_is_rejected(tmp_path, owner_keys):
    """PG-11: 同一源身份不得同时绑定两个不同 Canonical 所有者。"""
    baseline = "baseline-A"
    observation = _observation()
    ledger = _ledger(tmp_path)

    first = _binding_proposal(observation, "canon-A", baseline)
    ledger.append(proposal=first, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    _approve(ledger, first, observation, owner_keys, baseline=baseline)
    assert ledger.active_bindings()[first.source_key]["canonical_id"] == "canon-A"

    # 同一源身份、不同 Canonical ID 的第二个提案：批准时必须被拒绝。
    second = _binding_proposal(observation, "canon-B", baseline, operator="operator-2")
    ledger.append(proposal=second, to_state=MappingState.PROPOSED.value,
                  actor="operator-2", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    with pytest.raises(ConflictingBindingError):
        _approve(ledger, second, observation, owner_keys, baseline=baseline,
                 operator="operator-2", approver="owner-2")

    # 冲突未被静默接受：仍然只有 canon-A 是活动绑定。
    assert ledger.active_bindings()[first.source_key]["canonical_id"] == "canon-A"


# ---------------------------------------------------------------------------
# PG-12
# ---------------------------------------------------------------------------

def test_pg_12_duplicate_submission_is_idempotent_or_explicitly_rejected(tmp_path):
    """PG-12: 重复提交要么幂等 no-op，要么被显式拒绝；绝不追加第二条冲突决策。"""
    baseline = "baseline-A"
    proposal = _binding_proposal(_observation(), "canon-keep", baseline)
    ledger = _ledger(tmp_path)

    kwargs = dict(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    first, appended_first = ledger.append(**kwargs)
    second, appended_second = ledger.append(**kwargs)

    assert appended_first is True
    assert appended_second is False
    assert first == second
    assert len(ledger.entries()) == 1

    # 同一条决策被以不同载荷重复提交 -> 显式拒绝。
    with pytest.raises(LedgerReplayError):
        ledger.append(
            proposal=proposal, to_state=MappingState.PROPOSED.value,
            actor="operator-1", actor_role="operator",
            reason="a materially different justification",
            baseline_fingerprint=baseline,
        )
    assert len(ledger.entries()) == 1


# ---------------------------------------------------------------------------
# PG-13
# ---------------------------------------------------------------------------

def test_pg_13_invalid_governance_state_transition_is_rejected(tmp_path):
    """PG-13: 非法迁移一律拒绝，且没有通用 force 入口。"""
    forbidden = [
        ("OBSERVED", "APPROVED"),
        ("OBSERVED", "REVOKED"),
        ("PROPOSED", "SUPERSEDED"),
        ("REVIEW_REQUIRED", "SUPERSEDED"),
        ("APPROVED", "PROPOSED"),
        ("REJECTED", "APPROVED"),
        ("REJECTED", "PROPOSED"),
        ("REVOKED", "APPROVED"),
        ("SUPERSEDED", "APPROVED"),
        ("SUPERSEDED", "PROPOSED"),
        ("UNKNOWN", "PROPOSED"),
    ]
    for from_state, to_state in forbidden:
        with pytest.raises(InvalidTransitionError):
            validate_transition(from_state, to_state)

    # 权威迁移表：REJECTED 不可能到达 APPROVED；SUPERSEDED 是终态。
    assert MappingState.APPROVED.value not in ALLOWED_TRANSITIONS["REJECTED"]
    assert reachable_states("SUPERSEDED") == ()
    assert reachable_states("APPROVED") == ("REVOKED", "SUPERSEDED")

    # 账本层：首条迁移必须与提案声明的状态一致。
    ledger = _ledger(tmp_path)
    proposal = _binding_proposal(_observation(), "canon-keep", "baseline-A")
    with pytest.raises(InvalidTransitionError):
        ledger.append(proposal=proposal, to_state=MappingState.REVIEW_REQUIRED.value,
                      actor="operator-1", actor_role="operator", reason="wrong first state",
                      baseline_fingerprint="baseline-A")

    # 不存在 force 参数。
    import inspect

    signature = inspect.signature(GovernanceLedger.append)
    assert "force" not in signature.parameters
    assert not any("force" in name for name in signature.parameters)


# ---------------------------------------------------------------------------
# PG-14
# ---------------------------------------------------------------------------

def test_pg_14_broken_ledger_hash_chain_is_rejected(tmp_path, owner_keys):
    """PG-14: 账本内容被篡改或中间条目缺失一律拒绝。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    _approve(ledger, proposal, observation, owner_keys, baseline=baseline)
    ledger.append(proposal=proposal, to_state=MappingState.REVOKED.value,
                  actor="owner-1", actor_role="owner", reason="revoked",
                  baseline_fingerprint=baseline)
    assert len(ledger.entries()) == 3
    assert ledger.verify()["integrity"] == "OK"

    original = open(ledger.path, encoding="utf-8").read()
    lines = original.splitlines()

    # (a) 内容篡改 -> 条目哈希不匹配
    record = json.loads(lines[0])
    record["reason"] = "tampered"
    tampered = [json.dumps(record, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")), *lines[1:]]
    open(ledger.path, "w", encoding="utf-8", newline="\n").write(
        "\n".join(tampered) + "\n"
    )
    with pytest.raises(LedgerIntegrityError):
        ledger.verify()

    # (b) 中间条目缺失 -> 前驱哈希断链
    middle_removed = [lines[0], lines[2]]
    open(ledger.path, "w", encoding="utf-8", newline="\n").write(
        "\n".join(middle_removed) + "\n"
    )
    with pytest.raises(LedgerIntegrityError):
        ledger.verify()

    # (c) 损坏的 JSON
    open(ledger.path, "w", encoding="utf-8", newline="\n").write("{not json}\n")
    with pytest.raises(LedgerIntegrityError):
        ledger.verify()

    # (d) 恢复原文件后校验通过（只读校验不修改文件）。
    open(ledger.path, "w", encoding="utf-8", newline="\n").write(original)
    assert ledger.verify()["integrity"] == "OK"

    # 诚实的信任边界：哈希链只证明**内部一致性**。单纯截断尾部是一条自洽的
    # 更短链，链本身无法发现 —— 这正是需要独立签名/检查点作为外部信任锚的原因。
    truncated = [lines[0]]
    open(ledger.path, "w", encoding="utf-8", newline="\n").write(
        "\n".join(truncated) + "\n"
    )
    assert ledger.verify()["integrity"] == "OK"
    assert "NONE" in ledger.verify()["trust_anchor"]
    open(ledger.path, "w", encoding="utf-8", newline="\n").write(original)


# ---------------------------------------------------------------------------
# PG-15
# ---------------------------------------------------------------------------

def test_pg_15_ledger_replay_is_rejected(tmp_path):
    """PG-15: 针对陈旧账本末端的重放被拒绝。"""
    baseline = "baseline-A"
    ledger = _ledger(tmp_path)
    first = _binding_proposal(_observation(), "canon-A", baseline)
    ledger.append(proposal=first, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    tip = ledger.entries()[-1].entry_hash
    assert tip != GENESIS_PREVIOUS_HASH

    second = _binding_proposal(
        _observation(match_id="500l-2030-05-01-021", number="021", home="甲", away="乙"),
        "canon-B", baseline,
    )
    # 用陈旧的末端（创世哈希）重放 -> 拒绝
    with pytest.raises(LedgerReplayError):
        ledger.append(proposal=second, to_state=MappingState.PROPOSED.value,
                      actor="operator-1", actor_role="operator", reason="proposed",
                      baseline_fingerprint=baseline,
                      expected_previous_hash=GENESIS_PREVIOUS_HASH)
    # 用当前末端 -> 正常追加
    entry, appended = ledger.append(
        proposal=second, to_state=MappingState.PROPOSED.value,
        actor="operator-1", actor_role="operator", reason="proposed",
        baseline_fingerprint=baseline, expected_previous_hash=tip,
    )
    assert appended is True
    assert entry.previous_entry_hash == tip
    assert ledger.verify()["integrity"] == "OK"


# ---------------------------------------------------------------------------
# PG-16
# ---------------------------------------------------------------------------

def test_pg_16_stale_baseline_invalidates_proposal_use(tmp_path, owner_keys):
    """PG-16: 基线指纹变化后，批准不可复用于新基线。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)

    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=DATASET, operator="operator-1", approver="owner-1",
        approved_at="2030-01-01T00:00:00+00:00", reason="owner approves",
    )
    approval = sign_mapping_approval(owner_keys["private"], body)

    # 基线未变 -> 验证通过
    verify_mapping_approval(
        approval, proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=DATASET, current_baseline_fingerprint=baseline,
        trusted_public_key_path=owner_keys["public"],
    )

    # 基线变化 -> 陈旧批准被拒绝
    with pytest.raises(StaleBaselineError):
        verify_mapping_approval(
            approval, proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint="baseline-B",
            trusted_public_key_path=owner_keys["public"],
        )

    # 账本层同样拒绝
    with pytest.raises(StaleBaselineError):
        _approve(ledger, proposal, observation, owner_keys, baseline="baseline-B")

    # 基线指纹与列表顺序无关（因此「是否变化」的判定是确定的）
    fixture = [{"id": "a"}, {"id": "b"}]
    assert canonical_baseline_fingerprint(fixture) == canonical_baseline_fingerprint(
        list(reversed(fixture))
    )
    assert canonical_baseline_fingerprint(fixture) != canonical_baseline_fingerprint(
        [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    )


# ---------------------------------------------------------------------------
# PG-17
# ---------------------------------------------------------------------------

def test_pg_17_revoked_mapping_cannot_remain_active(tmp_path, owner_keys):
    """PG-17: 已撤销的映射不再是活动绑定。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    _approve(ledger, proposal, observation, owner_keys, baseline=baseline)
    assert ledger.active_bindings()

    ledger.append(proposal=proposal, to_state=MappingState.REVOKED.value,
                  actor="owner-1", actor_role="owner", reason="revoked by owner",
                  baseline_fingerprint=baseline)

    assert ledger.state_of(proposal.proposal_id) == MappingState.REVOKED.value
    assert ledger.active_bindings() == {}
    assert MappingState.REVOKED.value not in ACTIVE_STATES
    # 撤销后不能再回到 APPROVED。
    with pytest.raises(InvalidTransitionError):
        ledger.append(proposal=proposal, to_state=MappingState.APPROVED.value,
                      actor="owner-1", actor_role="owner", reason="reactivate",
                      baseline_fingerprint=baseline)


# ---------------------------------------------------------------------------
# PG-18
# ---------------------------------------------------------------------------

def test_pg_18_superseded_decision_retains_history(tmp_path, owner_keys):
    """PG-18: 被取代的决策保留完整历史证据。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)
    _approve(ledger, proposal, observation, owner_keys, baseline=baseline)
    ledger.append(proposal=proposal, to_state=MappingState.REVOKED.value,
                  actor="owner-1", actor_role="owner", reason="revoked",
                  baseline_fingerprint=baseline)
    ledger.append(proposal=proposal, to_state=MappingState.SUPERSEDED.value,
                  actor="owner-1", actor_role="owner", reason="superseded by a later decision",
                  baseline_fingerprint=baseline)

    history = ledger.history_of(proposal.proposal_id)
    assert [e.to_state for e in history] == [
        "PROPOSED", "APPROVED", "REVOKED", "SUPERSEDED",
    ]
    assert [e.from_state for e in history] == [
        "OBSERVED", "PROPOSED", "APPROVED", "REVOKED",
    ]
    # append-only：全部条目仍在，证据未被抹除，链仍完整。
    assert len(ledger.entries()) == 4
    assert ledger.verify()["integrity"] == "OK"
    assert ledger.active_bindings() == {}


# ---------------------------------------------------------------------------
# PG-19
# ---------------------------------------------------------------------------

def test_pg_19_unsigned_owner_approval_is_rejected(tmp_path, owner_keys):
    """PG-19: 未签名 / 空签名 / 非所有者角色的批准尝试一律拒绝。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)

    for bad in (None, {}, {"approval_schema_version": "1.0.0"}):
        with pytest.raises(SignatureVerificationError):
            ledger.append(
                proposal=proposal, to_state=MappingState.APPROVED.value,
                actor="owner-1", actor_role="owner", reason="approve",
                baseline_fingerprint=baseline, approval=bad,
                trusted_public_key_path=owner_keys["public"],
                source_observation_fingerprint=observation.semantic_fingerprint(),
            )

    # 非所有者角色不得触发 APPROVED。
    with pytest.raises(SignatureVerificationError):
        ledger.append(
            proposal=proposal, to_state=MappingState.APPROVED.value,
            actor="operator-1", actor_role="operator", reason="self approve",
            baseline_fingerprint=baseline,
            trusted_public_key_path=owner_keys["public"],
            source_observation_fingerprint=observation.semantic_fingerprint(),
        )

    # 缺少可信公钥路径 -> 拒绝。
    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=DATASET, operator="operator-1", approver="owner-1",
        approved_at="2030-01-01T00:00:00+00:00", reason="approve",
    )
    approval = sign_mapping_approval(owner_keys["private"], body)
    with pytest.raises(SignatureVerificationError):
        ledger.append(
            proposal=proposal, to_state=MappingState.APPROVED.value,
            actor="owner-1", actor_role="owner", reason="approve",
            baseline_fingerprint=baseline, approval=approval,
            source_observation_fingerprint=observation.semantic_fingerprint(),
        )

    assert ledger.state_of(proposal.proposal_id) == MappingState.PROPOSED.value


# ---------------------------------------------------------------------------
# PG-20
# ---------------------------------------------------------------------------

def test_pg_20_untrusted_or_tampered_approval_is_rejected(tmp_path, owner_keys, other_keys):
    """PG-20: 不可信密钥、篡改字段、错误用途与同一主体的批准一律拒绝。"""
    baseline = "baseline-A"
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", baseline)
    ledger = _ledger(tmp_path)
    ledger.append(proposal=proposal, to_state=MappingState.PROPOSED.value,
                  actor="operator-1", actor_role="operator", reason="proposed",
                  baseline_fingerprint=baseline)

    body = build_approval_body(
        proposal=proposal,
        source_observation_fingerprint=observation.semantic_fingerprint(),
        dataset_identity=DATASET, operator="operator-1", approver="owner-1",
        approved_at="2030-01-01T00:00:00+00:00", reason="approve",
    )
    approval = sign_mapping_approval(owner_keys["private"], body)

    # (a) 不可信签名密钥
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            approval, proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint=baseline,
            trusted_public_key_path=other_keys["public"],
        )

    # (b) 由不可信密钥签名
    rogue = sign_mapping_approval(other_keys["private"], dict(body))
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            rogue, proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint=baseline,
            trusted_public_key_path=owner_keys["public"],
        )

    # (c) 签名后被篡改
    tampered = dict(approval)
    tampered["candidate_canonical_id"] = "canon-attacker"
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            tampered, proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint=baseline,
            trusted_public_key_path=owner_keys["public"],
        )

    # (d) 生产修复用途的授权件不能当映射批准件使用
    wrong_purpose = dict(approval)
    wrong_purpose["purpose"] = "PRODUCTION_CANONICAL_DEDUP_APPLY"
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            wrong_purpose, proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint=baseline,
            trusted_public_key_path=owner_keys["public"],
        )
    assert MAPPING_APPROVAL_PURPOSE != "PRODUCTION_CANONICAL_DEDUP_APPLY"

    # (e) 操作者与批准者必须是不同主体
    with pytest.raises(SignatureVerificationError):
        build_approval_body(
            proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, operator="same-person", approver="same-person",
            approved_at="2030-01-01T00:00:00+00:00", reason="self approve",
        )

    # (f) 绑定不匹配（观测指纹不同）
    other_observation = _observation(match_id="500l-2030-05-01-021", number="021")
    with pytest.raises(SignatureVerificationError):
        verify_mapping_approval(
            approval, proposal=proposal,
            source_observation_fingerprint=other_observation.semantic_fingerprint(),
            dataset_identity=DATASET, current_baseline_fingerprint=baseline,
            trusted_public_key_path=owner_keys["public"],
        )

    assert ledger.state_of(proposal.proposal_id) == MappingState.PROPOSED.value
    assert ledger.active_bindings() == {}


# ---------------------------------------------------------------------------
# PG-21
# ---------------------------------------------------------------------------

def test_pg_21_governance_artifacts_only_in_isolated_workspace(tmp_path):
    """PG-21: 治理工件只能写入仓库之外的隔离工作区。"""
    from utils.mapping_governance import REPO_ROOT

    for bad in (
        os.path.join(REPO_ROOT, "data", "ledger.jsonl"),
        os.path.join(REPO_ROOT, "ledger.jsonl"),
        os.path.join(REPO_ROOT, "repair_runs", "ledger.jsonl"),
    ):
        with pytest.raises(LedgerPathError):
            GovernanceLedger(bad)
    with pytest.raises(LedgerPathError):
        GovernanceLedger("")

    # 所有者私钥绝不可以在仓库内。
    with pytest.raises(SignatureVerificationError):
        generate_mapping_owner_keypair(
            os.path.join(REPO_ROOT, "owner_private.pem"),
            str(tmp_path / "owner_public.pem"),
        )

    # 影子校验的工作区同样受此约束。
    observations, rows, fixture = demo_observations()
    with pytest.raises(LedgerPathError):
        shadow_validate(
            workspace=os.path.join(REPO_ROOT, "data", "shadow_ws"),
            observations=observations, incoming_rows=rows,
            canonical_fixture=fixture, dataset_identity=DATASET,
        )

    # 合法的隔离工作区可以正常写入。
    ledger = _ledger(tmp_path)
    assert ledger.path.startswith(os.path.realpath(str(tmp_path)))


# ---------------------------------------------------------------------------
# PG-22 / PG-28
# ---------------------------------------------------------------------------

def test_pg_22_and_pg_28_shadow_validation_preserves_all_seven_stores(
    isolated_data_dir, tmp_path
):
    """PG-22/PG-28: 影子校验之后七个持久化库逐字节不变。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)

    observations, rows, fixture = demo_observations()
    report = shadow_validate(
        workspace=str(tmp_path / "shadow_ws"),
        observations=observations,
        incoming_rows=rows,
        canonical_fixture=fixture,
        dataset_identity=DATASET,
    )

    after = _hash_all(isolated_data_dir)
    assert after == before, "影子校验不得改动任何持久化存储"
    assert set(after) == set(SEVEN_STORES)

    # 影子校验确实产出了治理工件，但它们全部位于隔离工作区。
    assert report["ledger_written"] is True
    assert report["ledger_verification"]["integrity"] == "OK"
    assert report["is_production_authority"] is False
    assert os.path.exists(report["ledger_path"])
    assert not os.path.exists(
        os.path.join(str(isolated_data_dir), SHADOW_LEDGER_NAME)
    )

    # 报告明确标注生产授权状态。
    assert report["production_mapping_write_authorization"] == "NOT_GRANTED"
    assert report["runtime_ledger_enforcement"] == "NOT_ENABLED"
    assert governance_status()["production_mapping_write_authorization"] == (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    )
    assert governance_status()["runtime_ledger_enforcement"] == (
        RUNTIME_LEDGER_ENFORCEMENT
    )


# ---------------------------------------------------------------------------
# PG-23 / PG-24
# ---------------------------------------------------------------------------

def test_pg_23_existing_planner_still_rejects_non_transitive_triangles():
    """PG-23: 既有摄取预检仍拒绝 incoming-only 非传递三角。"""
    from utils.ingestion_identity import plan_ingestion_identity

    x = _row("live-x", number=None, home="A", away="B")
    y = _row("market-y", number="001", home="A", away="B")
    z = _row("market-z", number="002", home="A", away="B")

    import itertools

    for order in itertools.permutations((x, y, z)):
        with pytest.raises(CanonicalIdentityCollisionError) as exc:
            plan_ingestion_identity([], list(order))
        assert exc.value.category == "AMBIGUOUS_CANONICAL_OWNERSHIP"
        assert exc.value.reason == "non_clique_component"


def test_pg_24_first_seen_canonical_id_selection_remains_deterministic():
    """PG-24: first-seen Canonical ID 选择仍然与行序无关。"""
    from utils.scraper import _merge

    live = _row("500l-2030-05-01-001", number="001")
    market = _row("500j-2030-05-01-001", number="001", time="00:00")
    market["kickoff_time_known"] = False

    forward = _merge([], [dict(live), dict(market)])
    backward = _merge([], [dict(market), dict(live)])

    assert forward[0][0]["id"] == backward[0][0]["id"] == "500l-2030-05-01-001"
    assert forward[1] == backward[1] == 1


# ---------------------------------------------------------------------------
# PG-25 / PG-26
# ---------------------------------------------------------------------------

def _fake_sources(monkeypatch, *, live_football=(), market=(), finished=()):
    monkeypatch.setattr(fetcher_500, "fetch_live_matches",
                        lambda sport: [dict(m) for m in live_football])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml",
                        lambda sport: [dict(m) for m in market if m.get("sport", "football") == sport])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches",
                        lambda *a, **k: [dict(m) for m in finished])


def test_pg_25_existing_market_admission_remains_valid(isolated_data_dir, monkeypatch):
    """PG-25: 正常市场准入（带完整盘口的新事件）仍然有效；无盘口新事件仍被拒绝。"""
    _fake_sources(monkeypatch, live_football=[
        _row("m-priced", number="007", odds=FOOTBALL_1X2),
        _row("m-unpriced", number="008", home="甲", away="乙", odds=None),
    ])
    result = scraper.refresh(verbose=False)

    stored = load_json("daily_matches.json")["matches"]
    assert [m["id"] for m in stored] == ["m-priced"]
    assert stored[0]["market_tracked"] is True
    assert result["sources"]["market_rejected"] == 1
    assert result["added"] == 1


def test_pg_26_existing_final_score_protection_remains_valid(isolated_data_dir, monkeypatch):
    """PG-26: 终态比分保护与冲突拒绝保持有效。"""
    _fake_sources(monkeypatch, live_football=[
        _row("m-fin", number="011", odds=FOOTBALL_1X2),
    ])
    assert scraper.refresh(verbose=False)["prediction_snapshots_added"] == 1

    _fake_sources(monkeypatch, live_football=[
        _row("m-fin", number="011", odds=None, status="finished", score={"ft": [2, 1]}),
    ])
    assert scraper.refresh(verbose=False)["settlements_added"] == 1

    _fake_sources(monkeypatch, live_football=[
        _row("m-fin", number="011", odds=None, status="finished", score={"ft": [2, 2]}),
    ])
    result = scraper.refresh(verbose=False)

    assert result["updated"] == 0
    assert result["settlements_added"] == 0
    stored = load_json("daily_matches.json")["matches"]
    assert stored[0]["score"] == {"ft": [2, 1]}
    assert len(get_all_evaluation_rows()) == 1


# ---------------------------------------------------------------------------
# PG-27
# ---------------------------------------------------------------------------

def test_pg_27_production_repair_authorization_is_unchanged(tmp_path, owner_keys):
    """PG-27: 生产修复授权契约未变，且映射批准件与修复授权件互不通用。"""
    from tools import production_canonical_repair as repair

    assert repair.AUTHORIZATION_SCHEMA_VERSION == "2.0.0"
    assert repair.AUTHORIZATION_PURPOSE == "PRODUCTION_CANONICAL_DEDUP_APPLY"
    assert repair.APPROVAL_ALGORITHM == APPROVAL_ALGORITHM == "ed25519"
    assert repair.AUTHORIZATION_PURPOSE != MAPPING_APPROVAL_PURPOSE

    # 映射批准件无法用于生产修复授权（缺少修复专用绑定字段与公钥块）。
    observation = _observation()
    proposal = _binding_proposal(observation, "canon-keep", "baseline-A")
    approval = sign_mapping_approval(
        owner_keys["private"],
        build_approval_body(
            proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET, operator="operator-1", approver="owner-1",
            approved_at="2030-01-01T00:00:00+00:00", reason="approve",
        ),
    )
    with pytest.raises(repair.SignatureVerificationError):
        repair.verify_authorization_signature(approval, owner_keys["public"])

    # 未签名的修复授权件同样被拒绝（授权边界未被削弱）。
    with pytest.raises(repair.SignatureVerificationError):
        repair.verify_authorization_signature(
            {"authorization_schema_version": "2.0.0"}, owner_keys["public"]
        )


# ---------------------------------------------------------------------------
# PG-29
# ---------------------------------------------------------------------------

_CHILD_SCRIPT = r'''
import sys
sys.path.insert(0, r"{root}")
from utils.source_identity import observe_source_row
from utils.mapping_governance import (
    GovernanceLedger, MappingState, ProposalAction, proposal_from_observations,
)
num = "{num}"
row = {{"id": "500l-2030-05-01-" + num, "sport": "football", "league": "x",
       "date": "2030-05-01", "time": "20:00", "kickoff_time_known": True,
       "home": "T" + num, "away": "U" + num, "jczq_no": num}}
obs = observe_source_row(row, adapter="live_football", canonical_match_id="canon-" + num)
proposal = proposal_from_observations(
    [obs],
    requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
    candidate_canonical_id="canon-" + num,
    existing_canonical_owner="canon-" + num,
    ingestion_plan_reference="planner:ACCEPTED",
    expected_baseline_fingerprint="baseline-A",
    reason="child append",
    identity_evidence_type="strong_competition_number",
    operator="child-" + num,
)
ledger = GovernanceLedger(r"{ledger}", dataset_identity="{dataset}")
entry, appended = ledger.append(
    proposal=proposal, to_state=MappingState.PROPOSED.value,
    actor="child-" + num, actor_role="operator", reason="child append",
    baseline_fingerprint="baseline-A",
)
print("CHILD_OK", appended, entry.sequence)
'''


def test_pg_29_cross_process_ledger_updates_are_safe(tmp_path):
    """PG-29: 跨进程并发追加是串行化的，哈希链保持完整。"""
    workspace = tmp_path / "concurrent"
    workspace.mkdir(parents=True, exist_ok=True)
    ledger_path = str(workspace / SHADOW_LEDGER_NAME)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    script = _CHILD_SCRIPT.format(root=root, ledger=ledger_path, dataset=DATASET, num="001")
    script_b = _CHILD_SCRIPT.format(root=root, ledger=ledger_path, dataset=DATASET, num="002")
    script_path = workspace / "child.py"
    script_b_path = workspace / "child_b.py"
    script_path.write_text(script, encoding="utf-8")
    script_b_path.write_text(script_b, encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = root
    processes = [
        subprocess.Popen([sys.executable, str(path)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for path in (script_path, script_b_path)
    ]
    outputs = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=120)
            outputs.append((process.returncode, out, err))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

    for returncode, out, err in outputs:
        assert returncode == 0, err
        assert "CHILD_OK True" in out, out

    ledger = GovernanceLedger(ledger_path, dataset_identity=DATASET)
    entries = ledger.entries()
    assert len(entries) == 2
    assert [e.sequence for e in entries] == [0, 1]
    assert entries[1].previous_entry_hash == entries[0].entry_hash
    assert ledger.verify()["integrity"] == "OK"


# ---------------------------------------------------------------------------
# PG-30
# ---------------------------------------------------------------------------

def test_pg_30_refresh_never_writes_a_governance_ledger(isolated_data_dir, monkeypatch):
    """PG-30: 正常运行期刷新绝不写治理账本，也不引用治理层。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)
    listing_before = sorted(os.listdir(str(isolated_data_dir)))

    _fake_sources(monkeypatch, live_football=[_row("m-1", number="021", odds=FOOTBALL_1X2)])
    scraper.refresh(verbose=False)

    listing_after = sorted(os.listdir(str(isolated_data_dir)))
    # 刷新只会新增运行时锁文件，绝不新增任何治理工件。
    assert set(listing_after) - set(listing_before) <= {".refresh.lock"}
    assert not any("ledger" in name for name in listing_after)
    assert not any("governance" in name for name in listing_after)

    # daily_matches.json 因刷新而更新，其余六个历史库保持不变。
    after = _hash_all(isolated_data_dir)
    assert after["daily_matches.json"] != before["daily_matches.json"]
    for name in SEVEN_STORES:
        if name == "daily_matches.json":
            continue
        assert after[name] == before[name], name

    # 运行期刷新路径不引用治理层。
    scraper_source = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "utils", "scraper.py"),
        encoding="utf-8",
    ).read()
    for token in ("mapping_governance", "source_identity", "GovernanceLedger",
                  "mapping_governance_shadow"):
        assert token not in scraper_source, token

    assert governance_status()["runtime_ledger_enforcement"] == "NOT_ENABLED"
    assert GOVERNANCE_SCHEMA_VERSION


def test_governance_rules_never_target_approved():
    """治理规则表不得把任何冲突直接指向 APPROVED。"""
    from utils.mapping_governance import CONFLICT_GOVERNANCE_RULES

    for category, rule in CONFLICT_GOVERNANCE_RULES.items():
        assert rule["target_state"] != MappingState.APPROVED.value, category
        assert rule["requested_action"] in {a.value for a in ProposalAction}


def test_proposal_contract_rejects_canonical_rewrite_actions():
    """提案契约拒绝任何「改写既有 Canonical ID」的动作。"""
    observation = _observation()
    for action in ("REWRITE_CANONICAL_ID", "RENAME_CANONICAL", "MIGRATE_CANONICAL",
                   "MERGE_CANONICAL_OWNERS", "DELETE_CANONICAL"):
        with pytest.raises(ProposalValidationError):
            proposal_from_observations(
                [observation],
                requested_action=action,
                candidate_canonical_id="canon-keep",
                existing_canonical_owner="canon-keep",
                ingestion_plan_reference="planner:ACCEPTED",
                expected_baseline_fingerprint="baseline-A",
                reason="attempt to rewrite Canonical identity",
                identity_evidence_type="strong_competition_number",
            )

    # BIND 动作必须带齐目标 Canonical ID 与既有所有者。
    with pytest.raises(ProposalValidationError):
        proposal_from_observations(
            [observation],
            requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
            candidate_canonical_id=None,
            existing_canonical_owner=None,
            ingestion_plan_reference="planner:ACCEPTED",
            expected_baseline_fingerprint="baseline-A",
            reason="missing binding target",
            identity_evidence_type="strong_competition_number",
        )

    # 合法提案通过校验。
    proposal = _binding_proposal(observation, "canon-keep", "baseline-A")
    validate_proposal(proposal)
    assert proposal.proposal_schema_version == "1.0.0"
    assert proposal.status == MappingState.PROPOSED.value
