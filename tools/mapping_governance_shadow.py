"""
Canonical 映射治理的**只读影子校验**入口
========================================
本工具用于在**隔离的合成工作区**中演练治理流程，并输出确定性报告。

严格边界
--------
允许：
- 检视一批合成源观测；
- **只读**地从原始源夹具抽取 provider 证据观测（`utils.provider_evidence`）；
- 生成提案工件（proposal artifacts）；
- 与隔离的 Canonical 夹具对比；
- **只读**地调用既有摄取规划器校验归属；
- **只读**地生成迁移影响清单（`utils.source_id_migration`）；
- **只读**地模拟未来的运行期资格闸门（`utils.runtime_mapping_eligibility`）；
- 校验治理账本；
- 产出确定性冲突报告。

禁止（本模块绝不实现，也绝不被运行期引用）：
- 把账本当作生产权威；
- 改写 ``daily_matches.json``；
- 改写任何历史快照 / 结算 / 评估样本；
- 改变源适配器的持久化 ID 格式；
- 让账本决策阻断实时刷新；
- 自动批准提案；
- 自动迁移历史映射；
- **执行任何迁移**（清单只是分析工件）；
- **激活任何运行期映射**（就绪清单只是分析工件）。

默认行为：无活动绑定、无生产权威、无迁移执行、无运行期映射激活。
运行期身份权威仍然是 ``utils.ingestion_identity.validate_ingestion_identity``；
本工具**不是**额外的生产写入路径，``utils/scraper.py`` 也从不引用它。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from typing import Any, Mapping, Sequence

from utils.ingestion_identity import CanonicalIdentityCollisionError, plan_ingestion_identity
from utils.mapping_governance import (
    ACTIVE_STATES,
    GOVERNANCE_SCHEMA_VERSION,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    PROPOSAL_SCHEMA_VERSION,
    RUNTIME_LEDGER_ENFORCEMENT,
    GovernanceLedger,
    MappingProposal,
    MappingState,
    ProposalAction,
    audit_historical_approvals,
    build_approval_body,
    build_proposal,
    governance_rule_for,
    governance_status,
    mapping_eligibility,
    proposal_for_conflict,
    proposal_from_observations,
    sign_mapping_approval,
    verify_mapping_approval,
)
from utils.provider_evidence import (
    PROVIDER_EVIDENCE_ADAPTERS,
    PROVIDER_EVIDENCE_SCHEMA_VERSION,
    extract_source_evidence,
)
from utils.runtime_mapping_eligibility import (
    RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
    RUNTIME_READINESS_MANIFEST_KIND,
    RuntimeMappingContext,
    build_runtime_readiness_manifest,
    evaluate_runtime_mapping_eligibility,
    runtime_eligibility_status,
)
from utils.runtime_mapping_policy import (
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)
from utils.source_id_migration import (
    MARKET_SOURCE_ID_CONTRACT_VERSION,
    MIGRATION_EXECUTION_AVAILABLE,
    MIGRATION_MANIFEST_SCHEMA_VERSION,
    ProposedSourceMapping,
    build_migration_impact_manifest,
    manifest_fingerprint,
)
from utils.source_identity import (
    IDENTITY_KEY_SCHEMA_VERSION,
    PROVENANCE_SCHEMA_VERSION,
    ScopedSourceIdentityKey,
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
# 影子入口：原始源证据检视（只读、离线）
# ---------------------------------------------------------------------------

def shadow_evidence_inspection(
    *,
    workspace: str,
    fixture: Any,
    adapter: str,
    sport: str | None = None,
    dataset_identity: str = "synthetic-evidence-fixture",
) -> dict:
    """
    从**原始源夹具**抽取 provider 证据观测（只读、离线、无副作用）。

    本入口绝不联网、绝不写盘、绝不产生提案或账本条目；返回确定性报告。
    """
    extraction = extract_source_evidence(fixture, adapter=adapter, sport=sport)
    observations = list(extraction.provenance_observations)
    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "analysis_kind": "SHADOW_PROVIDER_EVIDENCE_INSPECTION",
        "evidence_schema_version": PROVIDER_EVIDENCE_SCHEMA_VERSION,
        "provenance_schema_version": PROVENANCE_SCHEMA_VERSION,
        "dataset_identity": dataset_identity,
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "adapter": adapter,
        "supported_adapters": list(PROVIDER_EVIDENCE_ADAPTERS),
        "normalized_row_count": len(extraction.normalized_rows),
        "observation_count": len(observations),
        "sidecar": extraction.sidecar.as_dict(),
        "sidecar_fingerprint": extraction.sidecar.fingerprint(),
        "provenance": build_provenance_report(observations),
        "conflict_report": conflict_report(observations),
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "migration_execution_available": MIGRATION_EXECUTION_AVAILABLE,
        "active_bindings_created": 0,
        "is_production_authority": False,
    }


# ---------------------------------------------------------------------------
# 影子入口：只读迁移影响分析（只读、无执行）
# ---------------------------------------------------------------------------

def shadow_migration_analysis(
    *,
    workspace: str,
    canonical_fixture: Sequence[Mapping[str, Any]],
    observations: Sequence[SourceObservation],
    proposed_mappings: Sequence[ProposedSourceMapping],
    dependent_stores: Mapping[str, Any] | None = None,
    fixture_identity: str = "synthetic-migration-fixture",
    dataset_identity: str = "synthetic-migration-fixture",
) -> dict:
    """
    生成**只读**迁移影响清单（绝不执行迁移、绝不改写任何 ID）。

    返回的报告显式声明：无活动绑定、无生产权威、无迁移执行。
    """
    manifest = build_migration_impact_manifest(
        fixture_identity=fixture_identity,
        canonical_fixture=canonical_fixture,
        observations=observations,
        proposed_mappings=proposed_mappings,
        dependent_stores=dependent_stores,
        dataset_identity=dataset_identity,
    )
    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "analysis_kind": "SHADOW_MIGRATION_IMPACT_ANALYSIS",
        "manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "market_source_id_contract_version": MARKET_SOURCE_ID_CONTRACT_VERSION,
        "dataset_identity": dataset_identity,
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "migration_impact_manifest": manifest,
        "migration_manifest_fingerprint": manifest_fingerprint(manifest),
        "migration_execution_available": MIGRATION_EXECUTION_AVAILABLE,
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "active_bindings_created": 0,
        "canonical_ownership_changed": False,
        "identifiers_rewritten": 0,
        "files_written": 0,
        "is_production_authority": False,
    }


def proposed_mapping_from_payload(payload: Mapping[str, Any]) -> ProposedSourceMapping:
    """由 JSON 载荷构造一条**提议**映射（仅用于分析；不批准、不执行）。"""
    return ProposedSourceMapping(
        legacy_source_id=str(payload["legacy_source_id"]),
        proposed_source_id=str(payload["proposed_source_id"]),
        existing_canonical_id=(
            str(payload["existing_canonical_id"])
            if payload.get("existing_canonical_id")
            else None
        ),
        identity_kind=str(payload.get("identity_kind") or "COMPETITION_NUMBER"),
        identity_value=str(payload.get("identity_value") or ""),
        provider_id_uniqueness_scope=str(
            payload.get("provider_id_uniqueness_scope") or "NOT_APPLICABLE"
        ),
        observation_fingerprints=tuple(
            str(f) for f in (payload.get("observation_fingerprints") or ())
        ),
        mapping_proposal_reference=str(
            payload.get("mapping_proposal_reference") or ""
        ),
    )


# ---------------------------------------------------------------------------
# 影子入口：运行期资格模拟（只读、无激活、无账本写入）
# ---------------------------------------------------------------------------

#: 运行期资格模拟的必需场景：``(场景名, 期望结果, 说明)``。
RUNTIME_SCENARIO_MATRIX: tuple[tuple[str, str, str], ...] = (
    (
        "VALID_APPROVED_MAPPING",
        "ELIGIBLE",
        "已批准的作用域身份、无历史影响 -> 应判为 ELIGIBLE（且不做任何变更）。",
    ),
    ("MISSING_APPROVAL", "NOT_ELIGIBLE", "缺少所有者批准件。"),
    ("LEGACY_IDENTITY_KEY", "NOT_ELIGIBLE", "旧契约身份键（2.0.0）不得被消费。"),
    ("UNVERIFIED_PROVIDER_ID", "NOT_ELIGIBLE", "未验证的 provider 标识符一律阻断。"),
    ("REVOKED_APPROVAL", "NOT_ELIGIBLE", "已撤销的映射不得被消费。"),
    ("STALE_BASELINE", "NOT_ELIGIBLE", "批准绑定的基线已过期。"),
    ("HISTORICAL_SNAPSHOT_IMPACT", "NOT_ELIGIBLE", "任何历史（快照）影响一律阻断。"),
    ("CANONICAL_OWNER_CONFLICT", "NOT_ELIGIBLE", "Canonical 所有者冲突一律阻断。"),
)


def shadow_runtime_eligibility_check(
    *,
    workspace: str,
    proposal: MappingProposal | None,
    approval: Mapping[str, Any] | None,
    source_observation: SourceObservation | None,
    runtime_context: RuntimeMappingContext,
    evaluated_at: str | None = None,
    scenario: str = "",
) -> dict:
    """
    **只读**运行期资格模拟。

    绝不写账本、绝不写文件、绝不创建绑定、绝不修改 Canonical 记录、绝不激活映射。
    返回决策工件 + 显式隔离标记。
    """
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        source_observation,
        runtime_context,
        evaluated_at=evaluated_at,
    )
    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "analysis_kind": "SHADOW_RUNTIME_ELIGIBILITY_CHECK",
        "scenario": scenario,
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "runtime_mapping_policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "runtime_eligibility_evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "decision": decision.as_dict(),
        "eligibility_result": decision.eligibility_result,
        "rejection_codes": list(decision.rejection_codes),
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "production_mapping_write_authorization": PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
        "runtime_ledger_enforcement": RUNTIME_LEDGER_ENFORCEMENT,
        "ledger_written": False,
        "files_written": 0,
        "bindings_created": 0,
        "canonical_records_modified": 0,
        "is_production_authority": False,
    }


def _synthetic_runtime_proposal(
    *,
    key_dict: Mapping[str, str],
    scope_dict: Mapping[str, str],
    canonical_id: str,
    operator: str,
    action: str = ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
) -> MappingProposal:
    """
    构造一条**仅用于影子模拟**的提案（刻意绕过 ``validate_proposal``）。

    用途：演示「若有人提交了旧契约 / 未验证身份键，运行期闸门会如何判定」。
    这类对象**绝不**进入账本，也**绝不**进入任何生产路径。
    """
    key_pairs = tuple(sorted((str(k), str(v)) for k, v in key_dict.items()))
    scope_pairs = tuple(sorted((str(k), str(v)) for k, v in scope_dict.items()))
    return MappingProposal(
        proposal_schema_version=PROPOSAL_SCHEMA_VERSION,
        proposal_id=fingerprint(
            {
                "kind": "SHADOW_SYNTHETIC_RUNTIME_PROPOSAL",
                "scoped_source_identity_key": dict(key_pairs),
                "candidate_canonical_id": canonical_id,
                "operator": operator,
            }
        ),
        source_observation_ref="shadow-synthetic-observation",
        source_identity_scope=scope_pairs,
        scoped_source_identity_key=key_pairs,
        candidate_canonical_id=canonical_id,
        existing_canonical_owner=canonical_id,
        identity_evidence_type="shadow_synthetic",
        observation_fingerprints=("0" * 64,),
        ingestion_plan_reference="planner:SHADOW",
        reason="shadow runtime eligibility scenario",
        conflict_category=None,
        expected_baseline_fingerprint="baseline-A",
        requested_action=action,
        status=MappingState.PROPOSED.value,
        operator=operator,
    )


def shadow_runtime_scenario_matrix(
    *,
    workspace: str,
    canonical_id: str,
    source_observation: SourceObservation,
    owner_private_key_path: str | None,
    trusted_public_key_path: str | None,
    dataset_identity: str = "synthetic-runtime-fixture",
    baseline_fingerprint: str = "baseline-A",
    evaluated_at: str = "2030-01-01T00:00:00+00:00",
    operator: str = "shadow-operator",
    approver: str = "shadow-owner",
) -> dict:
    """
    运行 §9 要求的运行期资格场景矩阵（只读、无激活）。

    需要显式提供所有者私钥与可信公钥才能构造「有效批准」场景；缺失时该场景标记为
    ``SKIPPED``，绝不退化为信任内嵌公钥。
    """
    scope = dict(source_observation.stability_scope.as_dict())
    base_key = ScopedSourceIdentityKey.for_observation(source_observation)
    base_key_dict = base_key.as_dict()

    base_proposal = build_proposal(
        source_observation_ref=source_observation.observation_id,
        source_identity_scope=scope,
        identity_key=base_key,
        candidate_canonical_id=canonical_id,
        existing_canonical_owner=canonical_id,
        identity_evidence_type="strong_competition_number",
        observation_fingerprints=[source_observation.semantic_fingerprint()],
        ingestion_plan_reference="planner:ACCEPTED",
        reason="shadow runtime eligibility scenario",
        expected_baseline_fingerprint=baseline_fingerprint,
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        operator=operator,
    )

    valid_approval: dict | None = None
    approval_skip_reason = ""
    if not owner_private_key_path or not trusted_public_key_path:
        approval_skip_reason = (
            "owner private key / trusted public key not supplied; the valid-approval "
            "scenario is SKIPPED rather than trusting an embedded public key"
        )
    else:
        body = build_approval_body(
            proposal=base_proposal,
            source_observation_fingerprint=source_observation.semantic_fingerprint(),
            dataset_identity=dataset_identity,
            operator=operator,
            approver=approver,
            approved_at=evaluated_at,
            reason="shadow owner approves the proposed binding",
        )
        valid_approval = sign_mapping_approval(owner_private_key_path, body)

    base_context = RuntimeMappingContext(
        dataset_identity=dataset_identity,
        current_baseline_fingerprint=baseline_fingerprint,
        canonical_ids=(canonical_id,),
        trusted_public_key_path=trusted_public_key_path,
        expected_approver=approver,
        expected_operator=operator,
        recorded_proposal_state=MappingState.APPROVED.value,
    )

    legacy_proposal = _synthetic_runtime_proposal(
        key_dict={
            "identity_key_schema_version": "2.0.0",
            "identity_kind": "RAW_PROVIDER_ID",
            "identity_value": "market_xml|500j|PROVIDER_MATCH_ID|12345",
        },
        scope_dict=scope,
        canonical_id=canonical_id,
        operator=operator,
    )
    unverified_proposal = _synthetic_runtime_proposal(
        key_dict={
            "identity_key_schema_version": IDENTITY_KEY_SCHEMA_VERSION,
            "identity_kind": "UNVERIFIED_PROVIDER_ID",
            "identity_value": "market_xml|500j|football|2030-05-01|PROVIDER_MATCH_ID|UNKNOWN|12345",
            "provider_id_uniqueness_scope": "UNKNOWN",
        },
        scope_dict=scope,
        canonical_id=canonical_id,
        operator=operator,
    )

    scenarios: list[dict] = []

    def _run(name: str, expected: str, proposal, approval, context) -> None:
        if name == "VALID_APPROVED_MAPPING" and valid_approval is None:
            scenarios.append(
                {
                    "scenario": name,
                    "expected": expected,
                    "status": "SKIPPED",
                    "detail": approval_skip_reason,
                }
            )
            return

        report = shadow_runtime_eligibility_check(
            workspace=workspace,
            proposal=proposal,
            approval=approval,
            source_observation=source_observation,
            runtime_context=context,
            evaluated_at=evaluated_at,
            scenario=name,
        )
        scenarios.append(
            {
                "scenario": name,
                "expected": expected,
                "status": "EVALUATED",
                "eligibility_result": report["eligibility_result"],
                "rejection_codes": report["rejection_codes"],
                "matches_expected": report["eligibility_result"] == expected,
                "ledger_written": report["ledger_written"],
                "files_written": report["files_written"],
                "bindings_created": report["bindings_created"],
            }
        )

    for name, expected, _detail in RUNTIME_SCENARIO_MATRIX:
        if name == "VALID_APPROVED_MAPPING":
            _run(name, expected, base_proposal, valid_approval, base_context)
        elif name == "MISSING_APPROVAL":
            _run(name, expected, base_proposal, None, base_context)
        elif name == "LEGACY_IDENTITY_KEY":
            _run(name, expected, legacy_proposal, None, base_context)
        elif name == "UNVERIFIED_PROVIDER_ID":
            _run(name, expected, unverified_proposal, None, base_context)
        elif name == "REVOKED_APPROVAL":
            _run(
                name,
                expected,
                base_proposal,
                valid_approval,
                replace(base_context, recorded_proposal_state=MappingState.REVOKED.value),
            )
        elif name == "STALE_BASELINE":
            _run(
                name,
                expected,
                base_proposal,
                valid_approval,
                replace(base_context, current_baseline_fingerprint="baseline-B"),
            )
        elif name == "HISTORICAL_SNAPSHOT_IMPACT":
            _run(
                name,
                expected,
                base_proposal,
                valid_approval,
                replace(
                    base_context,
                    required_rewrite_targets=("prediction_snapshots.json",),
                ),
            )
        elif name == "CANONICAL_OWNER_CONFLICT":
            _run(
                name,
                expected,
                base_proposal,
                valid_approval,
                replace(
                    base_context,
                    known_source_bindings=((base_key.source_key(), "canon-OTHER"),),
                ),
            )

    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "analysis_kind": "SHADOW_RUNTIME_SCENARIO_MATRIX",
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "dataset_identity": dataset_identity,
        "canonical_id": canonical_id,
        "base_source_identity_key": base_key.source_key(),
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "runtime_mapping_policy_version": RUNTIME_MAPPING_POLICY_VERSION,
        "runtime_eligibility_evaluator_version": RUNTIME_ELIGIBILITY_EVALUATOR_VERSION,
        "scenarios": scenarios,
        "evaluated_count": sum(1 for s in scenarios if s["status"] == "EVALUATED"),
        "skipped_count": sum(1 for s in scenarios if s["status"] == "SKIPPED"),
        "all_scenarios_evaluated": all(
            s["status"] == "EVALUATED" for s in scenarios
        ),
        "all_matched": all(
            s["matches_expected"]
            for s in scenarios
            if s["status"] == "EVALUATED"
        )
        and all(s["status"] == "EVALUATED" for s in scenarios),
        "ledger_written": False,
        "files_written": 0,
        "bindings_created": 0,
        "is_production_authority": False,
    }


def shadow_runtime_readiness_report(
    *,
    workspace: str,
    proposals: Sequence[MappingProposal],
    observations: Sequence[SourceObservation],
    canonical_fixture: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any] | None] | None = None,
    dataset_identity: str = "synthetic-runtime-fixture",
    baseline_fingerprint: str = "",
    trusted_public_key_path: str | None = None,
    evaluated_at: str | None = None,
) -> dict:
    """
    汇总运行期就绪清单（只读）。默认**不提供任何批准件**，因此结论必然是
    NOT_ELIGIBLE —— 这正是本阶段的预期状态：闸门存在，但没有任何映射被激活。
    """
    canonical_ids = tuple(
        str(m.get("id") or "") for m in canonical_fixture if m.get("id")
    )
    approval_list = list(approvals) if approvals is not None else [None] * len(proposals)
    context = RuntimeMappingContext(
        dataset_identity=dataset_identity,
        current_baseline_fingerprint=baseline_fingerprint,
        canonical_ids=canonical_ids,
        trusted_public_key_path=trusted_public_key_path,
        recorded_proposal_state=None,
    )
    decisions = [
        evaluate_runtime_mapping_eligibility(
            proposal,
            approval_list[index] if index < len(approval_list) else None,
            observations[index] if index < len(observations) else None,
            context,
            evaluated_at=evaluated_at,
        )
        for index, proposal in enumerate(proposals)
    ]
    manifest = build_runtime_readiness_manifest(
        fixture_identity="shadow-runtime-readiness",
        decisions=decisions,
        dataset_identity=dataset_identity,
        current_baseline_fingerprint=baseline_fingerprint,
        approval_versions=sorted(
            {
                str(a.get("approval_schema_version"))
                for a in approval_list
                if isinstance(a, Mapping) and a.get("approval_schema_version")
            }
        ),
    )
    return {
        "shadow_report_schema_version": SHADOW_REPORT_SCHEMA_VERSION,
        "analysis_kind": "SHADOW_RUNTIME_READINESS",
        "workspace": os.path.realpath(os.path.abspath(workspace)),
        "manifest_kind": RUNTIME_READINESS_MANIFEST_KIND,
        "runtime_readiness_manifest": manifest,
        "runtime_eligibility_status": runtime_eligibility_status(),
        "ledger_written": False,
        "files_written": 0,
        "bindings_created": 0,
        "canonical_records_modified": 0,
        "runtime_mapping_activation": RUNTIME_MAPPING_ACTIVATION,
        "is_production_authority": False,
    }


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
            "Never writes production data, never executes a migration, and is never "
            "consulted by refresh()."
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
    parser.add_argument(
        "--evidence-fixture",
        help="raw source fixture (HTML/XML text, or JSON with 'document'/'documents')",
    )
    parser.add_argument(
        "--adapter",
        choices=list(PROVIDER_EVIDENCE_ADAPTERS),
        default="market_xml",
        help="adapter used to extract evidence from --evidence-fixture",
    )
    parser.add_argument("--evidence-sport", help="sport for market_xml evidence extraction")
    parser.add_argument(
        "--migration-manifest",
        action="store_true",
        help="also build the read-only migration impact manifest (never executes)",
    )
    parser.add_argument(
        "--proposed-mapping",
        help="JSON file with a list of proposed legacy -> future source mappings",
    )
    parser.add_argument(
        "--dependent-stores",
        help="JSON file with synthetic dependent evidence stores (7-store fixture)",
    )
    parser.add_argument(
        "--runtime-eligibility",
        action="store_true",
        help="also build the read-only runtime readiness manifest (never activates)",
    )
    parser.add_argument(
        "--runtime-scenarios",
        action="store_true",
        help="also run the runtime eligibility scenario matrix (read-only)",
    )
    args = parser.parse_args(argv)

    evidence_report: dict | None = None

    if args.evidence_fixture:
        with open(args.evidence_fixture, "rb") as handle:
            raw_fixture = handle.read().decode("utf-8", errors="replace")
        fixture: Any = raw_fixture
        if args.evidence_fixture.lower().endswith(".json"):
            try:
                parsed = json.loads(raw_fixture)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("documents"), dict):
                fixture = {str(k): str(v) for k, v in parsed["documents"].items()}
            elif isinstance(parsed, dict) and "document" in parsed:
                fixture = str(parsed["document"])
            elif parsed is not None:
                fixture = parsed
        evidence_report = shadow_evidence_inspection(
            workspace=args.workspace,
            fixture=fixture,
            adapter=args.adapter,
            sport=args.evidence_sport,
            dataset_identity=args.dataset_identity,
        )
        extraction = extract_source_evidence(
            fixture, adapter=args.adapter, sport=args.evidence_sport
        )
        observations = list(extraction.provenance_observations)
        incoming_rows = list(extraction.normalized_rows)
        canonical_fixture: list[dict] = []
        if args.canonical_fixture:
            with open(args.canonical_fixture, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            canonical_fixture = payload["matches"] if isinstance(payload, dict) else payload
    elif args.demo or not args.observations:
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
    if evidence_report is not None:
        report["provider_evidence_inspection"] = evidence_report

    if args.migration_manifest:
        mappings: list[ProposedSourceMapping] = []
        if args.proposed_mapping:
            with open(args.proposed_mapping, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            entries = payload["mappings"] if isinstance(payload, dict) else payload
            mappings = [proposed_mapping_from_payload(item) for item in entries]
        dependent_stores: dict = {}
        if args.dependent_stores:
            with open(args.dependent_stores, "r", encoding="utf-8") as handle:
                dependent_stores = json.load(handle)
        report["migration_impact_analysis"] = shadow_migration_analysis(
            workspace=args.workspace,
            canonical_fixture=canonical_fixture,
            observations=observations,
            proposed_mappings=mappings,
            dependent_stores=dependent_stores,
            dataset_identity=args.dataset_identity,
        )

    if args.runtime_eligibility or args.runtime_scenarios:
        baseline = report.get("canonical_baseline_fingerprint", "")
        target_canonical_id = (
            str(canonical_fixture[0].get("id") or "") if canonical_fixture else ""
        )
        runtime_proposals: list[MappingProposal] = []
        skipped: list[dict] = []
        for observation in observations:
            if not ScopedSourceIdentityKey.for_observation(observation).is_sufficient:
                skipped.append(
                    {
                        "observation_id": observation.observation_id,
                        "detail": "insufficient identity evidence; not a binding candidate",
                    }
                )
                continue
            runtime_proposals.append(
                proposal_from_observations(
                    [observation],
                    requested_action=(
                        ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value
                    ),
                    candidate_canonical_id=target_canonical_id or None,
                    existing_canonical_owner=target_canonical_id or None,
                    ingestion_plan_reference=(
                        f"planner:{report['planner_validation']['outcome']}"
                    ),
                    expected_baseline_fingerprint=baseline,
                    reason="shadow runtime readiness candidate",
                    identity_evidence_type="strong_competition_number",
                )
            )
        readiness = shadow_runtime_readiness_report(
            workspace=args.workspace,
            proposals=runtime_proposals,
            observations=observations,
            canonical_fixture=canonical_fixture,
            dataset_identity=args.dataset_identity,
            baseline_fingerprint=baseline,
            trusted_public_key_path=args.trusted_public_key,
        )
        readiness["skipped_observations"] = skipped
        report["runtime_readiness"] = readiness

        if args.runtime_scenarios and target_canonical_id and observations:
            report["runtime_scenario_matrix"] = shadow_runtime_scenario_matrix(
                workspace=args.workspace,
                canonical_id=target_canonical_id,
                source_observation=observations[0],
                owner_private_key_path=args.owner_private_key,
                trusted_public_key_path=args.trusted_public_key,
                dataset_identity=args.dataset_identity,
                baseline_fingerprint=baseline,
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
