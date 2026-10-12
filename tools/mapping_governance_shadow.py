"""
Canonical 映射治理的**只读影子校验**入口
========================================
本工具用于在**隔离的合成工作区**中演练治理流程，并输出确定性报告。

严格边界
--------
允许：
- 检视一批合成源观测；
- 生成提案工件（proposal artifacts）；
- 与隔离的 Canonical 夹具对比；
- **只读**地调用既有摄取规划器校验归属；
- 校验治理账本；
- 产出确定性冲突报告。

禁止（本模块绝不实现，也绝不被运行期引用）：
- 把账本当作生产权威；
- 改写 ``daily_matches.json``；
- 改写任何历史快照 / 结算 / 评估样本；
- 改变源适配器的持久化 ID 格式；
- 让账本决策阻断实时刷新；
- 自动批准提案；
- 自动迁移历史映射。

运行期身份权威仍然是 ``utils.ingestion_identity.validate_ingestion_identity``；
本工具**不是**额外的生产写入路径，``utils/scraper.py`` 也从不引用它。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Mapping, Sequence

from utils.ingestion_identity import CanonicalIdentityCollisionError, plan_ingestion_identity
from utils.mapping_governance import (
    ACTIVE_STATES,
    GOVERNANCE_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    GovernanceLedger,
    MappingProposal,
    MappingState,
    ProposalAction,
    audit_historical_approvals,
    build_approval_body,
    governance_rule_for,
    governance_status,
    mapping_eligibility,
    proposal_for_conflict,
    proposal_from_observations,
    sign_mapping_approval,
    verify_mapping_approval,
)
from utils.source_identity import (
    PROVENANCE_SCHEMA_VERSION,
    SourceObservation,
    build_provenance_report,
    conflict_report,
    detect_all_conflicts,
    fingerprint,
    observe_source_row,
)

#: 影子校验的 schema 版本。
SHADOW_REPORT_SCHEMA_VERSION = "1.0.0"

#: 影子工作区内的账本文件名。
SHADOW_LEDGER_NAME = "mapping_governance_ledger.jsonl"


# ---------------------------------------------------------------------------
# 基线指纹（只读）
# ---------------------------------------------------------------------------

def canonical_baseline_fingerprint(matches: Sequence[Mapping[str, Any]]) -> str:
    """
    Canonical 夹具的基线指纹。

    与列表顺序**无关**：按 ``id`` 排序后逐条指纹再哈希。因此「基线是否变化」
    这一判定不受输入排列影响。
    """
    per_match = sorted(
        fingerprint({"id": str(m.get("id") or ""), "match": dict(m)}) for m in matches
    )
    return fingerprint(
        {
            "kind": "CANONICAL_BASELINE",
            "count": len(per_match),
            "matches": per_match,
        }
    )


def _canonical_index(matches: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(m.get("id") or ""): m for m in matches}


# ---------------------------------------------------------------------------
# 只读规划器校验
# ---------------------------------------------------------------------------

def validate_against_ingestion_planner(
    canonical_fixture: Sequence[Mapping[str, Any]],
    incoming_rows: Sequence[Mapping[str, Any]],
) -> dict:
    """
    **只读**地调用既有摄取规划器，检查归属结论。

    不复制、不替换规划器；只消费它的结论。任何身份冲突都按类别如实报告。
    """
    try:
        plan = plan_ingestion_identity(list(canonical_fixture), list(incoming_rows))
    except CanonicalIdentityCollisionError as exc:
        return {
            "outcome": "REJECTED",
            "category": exc.category,
            "reason": exc.reason,
            "evidence_type": exc.evidence_type,
            "match_id": exc.match_id,
            "competing_canonical_ids": list(exc.competing_canonical_ids),
        }
    return {
        "outcome": "ACCEPTED",
        "resolved_canonical_ids": sorted(set(plan.canonical_id_by_index)),
        "new_canonical_ids": sorted(plan.new_canonical_ids),
        "owner_index_by_index": list(plan.owner_index_by_index),
    }


# ---------------------------------------------------------------------------
# 影子校验主体
# ---------------------------------------------------------------------------

def shadow_validate(
    *,
    workspace: str,
    observations: Sequence[SourceObservation],
    incoming_rows: Sequence[Mapping[str, Any]],
    canonical_fixture: Sequence[Mapping[str, Any]],
    dataset_identity: str,
    operator: str = "shadow-operator",
    approver: str = "shadow-owner",
    owner_private_key_path: str | None = None,
    trusted_public_key_path: str | None = None,
    write_ledger: bool = True,
    approve: bool = False,
) -> dict:
    """
    执行一次只读影子校验，返回确定性报告。

    行为
    ----
    - 生成溯源报告与冲突报告（只读）。
    - **只读**调用摄取规划器校验归属。
    - 为每条冲突生成**非活动**提案（目标状态永不为 ``APPROVED``）。
    - 仅在 ``workspace`` 内写入隔离账本；账本从不成为生产权威。
    - 仅在显式提供所有者私钥 + 可信公钥时才会记录 ``APPROVED``。
    """
    ledger_path = os.path.join(workspace, SHADOW_LEDGER_NAME)
    ledger = GovernanceLedger(ledger_path, dataset_identity=dataset_identity)
    baseline = canonical_baseline_fingerprint(canonical_fixture)

    provenance = build_provenance_report(observations)
    conflicts = detect_all_conflicts(observations)
    planner_result = validate_against_ingestion_planner(canonical_fixture, incoming_rows)

    proposals: list[MappingProposal] = []
    recorded: list[dict] = []
    observations_by_id = {o.observation_id: o for o in observations}

    # 1) 冲突 -> 非活动提案（REVIEW_REQUIRED 或 REJECTED，永不为 APPROVED）
    for conflict in conflicts:
        members = [
            observations_by_id[oid]
            for oid in conflict.observation_ids
            if oid in observations_by_id
        ]
        try:
            proposal = proposal_for_conflict(
                conflict,
                members,
                ingestion_plan_reference=f"planner:{planner_result['outcome']}",
                expected_baseline_fingerprint=baseline,
                identity_evidence_type="source_alias",
                operator=operator,
            )
        except Exception as exc:  # noqa: BLE001
            recorded.append(
                {"kind": "conflict", "category": conflict.category, "error": str(exc)}
            )
            continue
        proposals.append(proposal)
        rule = governance_rule_for(conflict.category) or {}
        target = rule.get("target_state", MappingState.REVIEW_REQUIRED.value)
        detail = rule.get("detail", "shadow validation conflict")

        # OBSERVED 不等于已提案：先记录显式提案，再推进到治理目标状态。
        first_entry, first_appended = _record(
            ledger,
            proposal,
            MappingState.PROPOSED.value,
            operator,
            "operator",
            "operator proposes governance handling for a detected conflict",
            baseline,
            write_ledger=write_ledger,
        )
        second_entry, second_appended = (None, False)
        if target != MappingState.PROPOSED.value:
            second_entry, second_appended = _record(
                ledger,
                proposal,
                target,
                operator,
                "operator",
                detail,
                baseline,
                write_ledger=write_ledger,
            )
        recorded.append(
            {
                "kind": "conflict",
                "category": conflict.category,
                "subject": conflict.subject,
                "proposal_id": proposal.proposal_id,
                "states": [
                    MappingState.PROPOSED.value,
                    *([target] if target != MappingState.PROPOSED.value else []),
                ],
                "target_state": target,
                "appended": [first_appended, second_appended],
                "sequences": [
                    e.sequence for e in (first_entry, second_entry) if e is not None
                ],
            }
        )

    # 2) 明确归属的观测 -> 绑定提案（默认仅 PROPOSED，绝不自动 APPROVED）
    indexed_fixture = _canonical_index(canonical_fixture)
    for observation in observations:
        if observation.canonical_match_id is None:
            continue
        if observation.canonical_match_id not in indexed_fixture:
            continue
        proposal = proposal_from_observations(
            [observation],
            requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
            candidate_canonical_id=observation.canonical_match_id,
            existing_canonical_owner=observation.canonical_match_id,
            ingestion_plan_reference=f"planner:{planner_result['outcome']}",
            expected_baseline_fingerprint=baseline,
            reason="observed source row already reconciles with an existing Canonical owner",
            identity_evidence_type="strong_competition_number",
            operator=operator,
        )
        proposals.append(proposal)
        entry, appended = _record(
            ledger, proposal, MappingState.PROPOSED.value, operator, "operator",
            "proposed binding for owner review", baseline, write_ledger=write_ledger,
        )
        recorded.append(
            {
                "kind": "binding",
                "proposal_id": proposal.proposal_id,
                "target_state": MappingState.PROPOSED.value,
                "appended": appended,
                "sequence": entry.sequence if entry else None,
            }
        )

    # 3) 可选的、显式的所有者批准（只有提供了密钥才会记录 APPROVED）
    approvals: list[dict] = []
    if approve:
        if not owner_private_key_path or not trusted_public_key_path:
            approvals.append(
                {"status": "SKIPPED", "detail": "owner private key / trusted public key not supplied"}
            )
        else:
            for proposal in proposals:
                if not proposal.is_binding_action:
                    continue
                observation = next(
                    (
                        o
                        for o in observations
                        if o.canonical_match_id == proposal.candidate_canonical_id
                    ),
                    None,
                )
                if observation is None:
                    continue
                body = build_approval_body(
                    proposal=proposal,
                    source_observation_fingerprint=observation.semantic_fingerprint(),
                    dataset_identity=dataset_identity,
                    operator=operator,
                    approver=approver,
                    approved_at="2030-01-01T00:00:00+00:00",
                    reason="owner approves the proposed binding",
                )
                approval = sign_mapping_approval(owner_private_key_path, body)
                verify_mapping_approval(
                    approval,
                    proposal=proposal,
                    source_observation_fingerprint=observation.semantic_fingerprint(),
                    dataset_identity=dataset_identity,
                    current_baseline_fingerprint=baseline,
                    trusted_public_key_path=trusted_public_key_path,
                    # 主体归属：账本主体必须等于被签名的批准人；被签名的操作者必须
                    # 等于提案的操作者。
                    expected_actor=approver,
                    expected_operator=proposal.operator,
                )
                entry, appended = ledger.append(
                    proposal=proposal,
                    to_state=MappingState.APPROVED.value,
                    actor=approver,
                    actor_role="owner",
                    reason="owner approves the proposed binding",
                    baseline_fingerprint=baseline,
                    approval=approval,
                    trusted_public_key_path=trusted_public_key_path,
                    source_observation_fingerprint=observation.semantic_fingerprint(),
                )
                approvals.append(
                    {
                        "status": "APPROVED",
                        "proposal_id": proposal.proposal_id,
                        "appended": appended,
                        "sequence": entry.sequence,
                    }
                )

    ledger_state = ledger.proposal_states() if write_ledger else {}
    active = ledger.active_bindings() if write_ledger else {}
    legacy_active = ledger.legacy_active_bindings() if write_ledger else ()

    # 4) 独立的历史批准审计（仅在提供可信公钥时；缺失时绝不退化为信任内嵌公钥）
    audit: dict | None = None
    eligibility: dict | None = None
    if write_ledger and ledger.exists():
        if trusted_public_key_path:
            audit = audit_historical_approvals(
                ledger,
                trusted_public_key_path=trusted_public_key_path,
                dataset_identity=dataset_identity,
            )
        else:
            audit = {
                "verification_category": "AUTHENTICATED_MAPPING_APPROVAL_AUDIT",
                "status": "NOT_PERFORMED",
                "detail": (
                    "no explicitly trusted public key was supplied; an embedded public "
                    "key is never accepted as the trust anchor, so no authenticated "
                    "approval verification is claimed"
                ),
            }
        eligibility = mapping_eligibility(
            ledger, current_baseline_fingerprint=baseline
        )

    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "governance_schema_version": GOVERNANCE_SCHEMA_VERSION,
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "dataset_identity": dataset_identity,
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "ledger_path": ledger.path,
        "ledger_written": bool(write_ledger),
        "canonical_baseline_fingerprint": baseline,
        "provenance": provenance,
        "conflict_report": conflict_report(observations),
        "planner_validation": planner_result,
        "proposals": [p.as_dict() for p in proposals],
        "recorded": recorded,
        "approvals": approvals,
        "proposal_states": ledger_state,
        "active_bindings": active,
        "legacy_active_bindings": list(legacy_active),
        "authenticated_approval_audit": audit,
        "current_mapping_eligibility": eligibility,
        "ledger_verification": ledger.verify() if ledger.exists() else None,
        "governance_status": governance_status(),
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "is_production_authority": False,
    }


def _record(
    ledger: GovernanceLedger,
    proposal: MappingProposal,
    target_state: str,
    actor: str,
    actor_role: str,
    reason: str,
    baseline: str,
    *,
    write_ledger: bool,
):
    """把一条**非活动**提案状态写入隔离账本（``write_ledger=False`` 时不写）。"""
    if not write_ledger:
        return None, False
    if target_state in ACTIVE_STATES:
        raise ValueError("shadow validation must never record an active state")
    return ledger.append(
        proposal=proposal,
        to_state=target_state,
        actor=actor,
        actor_role=actor_role,
        reason=reason or "shadow validation",
        baseline_fingerprint=baseline,
    )


# ---------------------------------------------------------------------------
# 合成演示夹具（仅用于演示与测试）
# ---------------------------------------------------------------------------

def demo_observations() -> tuple[tuple[SourceObservation, ...], list[dict], list[dict]]:
    """
    构造一个合成场景，覆盖本阶段的全部已确认风险：

    - 跨运动共享 500j 前缀（足球 001 / 篮球 001，同日期）
    - 完场 feed 行位置标识符被复用于两场不同比赛
    - 一条可安全对账的 live 行（强编号一致）
    - 一条无关的新事件
    """
    football_market = {
        "id": "500j-2030-05-01-001", "sport": "football", "league": "西甲",
        "date": "2030-05-01", "time": "00:00", "kickoff_time_known": False,
        "status": "upcoming", "home": "皇马", "away": "巴萨", "score": None,
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8}, "jczq_no": "001",
    }
    basketball_market = {
        "id": "500j-2030-05-01-001", "sport": "basketball", "league": "NBA",
        "date": "2030-05-01", "time": "00:00", "kickoff_time_known": False,
        "status": "upcoming", "home": "湖人", "away": "勇士", "score": None,
        "odds": {"home_win": 1.8, "away_win": 2.0}, "jczq_no": "001",
    }
    finished_row_a = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "20:00", "kickoff_time_known": True,
        "status": "finished", "home": "甲队", "away": "乙队",
        "score": {"ft": [2, 1]}, "odds": None, "jczq_no": None,
    }
    finished_row_b = {
        "id": "500w-2030-05-01-0", "sport": "football", "league": "英超",
        "date": "2030-05-01", "time": "22:00", "kickoff_time_known": True,
        "status": "finished", "home": "丙队", "away": "丁队",
        "score": {"ft": [0, 3]}, "odds": None, "jczq_no": None,
    }
    live_row = {
        "id": "500l-2030-05-01-007", "sport": "football", "league": "西甲",
        "date": "2030-05-01", "time": "20:00", "kickoff_time_known": True,
        "status": "upcoming", "home": "皇马", "away": "巴萨", "score": None,
        "odds": None, "jczq_no": "007",
    }
    new_event_row = {
        "id": "500l-2030-06-01-021", "sport": "football", "league": "英超",
        "date": "2030-06-01", "time": "20:00", "kickoff_time_known": True,
        "status": "upcoming", "home": "戊队", "away": "己队", "score": None,
        "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.5}, "jczq_no": "021",
    }

    rows = [
        (football_market, "market_xml"),
        (basketball_market, "market_xml"),
        (finished_row_a, "finished_feed"),
        (finished_row_b, "finished_feed"),
        (live_row, "live_football"),
        (new_event_row, "live_football"),
    ]

    canonical_fixture = [
        {
            "id": "500j-2030-05-01-007", "sport": "football", "league": "西甲",
            "date": "2030-05-01", "time": "20:00", "kickoff_time_known": True,
            "status": "upcoming", "home": "皇马", "away": "巴萨", "score": None,
            "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
            "jczq_no": "007", "market_tracked": True,
        }
    ]

    observations = tuple(
        observe_source_row(
            row,
            adapter=adapter,
            canonical_match_id=(
                "500j-2030-05-01-007"
                if row is live_row
                else None
            ),
        )
        for row, adapter in rows
    )
    incoming_rows = [row for row, _ in rows]
    return observations, incoming_rows, canonical_fixture


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only shadow validation for Canonical mapping governance. "
            "Never writes production data and is never consulted by refresh()."
        )
    )
    parser.add_argument("--workspace", required=True, help="isolated workspace OUTSIDE the repository")
    parser.add_argument("--dataset-identity", default="synthetic-shadow-fixture")
    parser.add_argument("--observations", help="JSON file with synthetic source rows")
    parser.add_argument("--canonical-fixture", help="JSON file with Canonical match rows")
    parser.add_argument("--trusted-public-key", help="trusted owner Ed25519 public key (PEM)")
    parser.add_argument("--owner-private-key", help="owner Ed25519 private key (PEM, outside the repo)")
    parser.add_argument("--approve", action="store_true", help="record owner approvals (requires both keys)")
    parser.add_argument("--no-ledger", action="store_true", help="do not write the isolated ledger")
    parser.add_argument("--demo", action="store_true", help="use the built-in synthetic demo fixture")
    parser.add_argument("--json-out", help="optional path to write the report JSON")
    args = parser.parse_args(argv)

    if args.demo or not args.observations:
        observations, incoming_rows, canonical_fixture = demo_observations()
    else:
        with open(args.observations, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rows = payload["rows"] if isinstance(payload, dict) else payload
        adapters = payload.get("adapters", ["market_xml"] * len(rows)) if isinstance(payload, dict) else None
        observations = tuple(
            observe_source_row(row, adapter=(adapters[i] if adapters else "market_xml"))
            for i, row in enumerate(rows)
        )
        incoming_rows = list(rows)
        canonical_fixture = []
        if args.canonical_fixture:
            with open(args.canonical_fixture, "r", encoding="utf-8") as handle:
                fixture = json.load(handle)
            canonical_fixture = fixture["matches"] if isinstance(fixture, dict) else fixture

    report = shadow_validate(
        workspace=args.workspace,
        observations=observations,
        incoming_rows=incoming_rows,
        canonical_fixture=canonical_fixture,
        dataset_identity=args.dataset_identity,
        owner_private_key_path=args.owner_private_key,
        trusted_public_key_path=args.trusted_public_key,
        write_ledger=not args.no_ledger,
        approve=bool(args.approve),
    )

    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
