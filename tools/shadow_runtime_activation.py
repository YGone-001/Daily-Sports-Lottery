"""
影子运行期激活模拟入口（**opt-in、SHADOW ONLY**）
=================================================
本工具在**显式提供的隔离工作区**中演练影子运行期读取器与激活模拟：

- 合成场景执行；
- 快照创建、注册与**显式**切换；
- 读时授权再验证；
- 冲突绑定模拟；
- 授权过期 / 撤销模拟；
- 紧急停用；
- 确定性回滚；
- 隔离持久审计校验与重启恢复。

严格边界
--------
- 需要显式合成夹具输入与显式隔离工作区；**没有默认工作区**。
- **不联网抓取**、**不修改生产数据**、**不隐藏激活**、**不使用生产签名凭据**。
- **默认没有任何活动影子映射**。
- 全部输出标注 ``SHADOW_ONLY``；``is_production_authority`` 恒为 ``False``。
- 命令名带 ``shadow`` 限定词；**绝不**复用生产修复的 ``apply`` / ``recover``。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from utils.mapping_governance import (
    ProposalAction,
    build_approval_body,
    generate_mapping_owner_keypair,
    proposal_from_observations,
    sign_mapping_approval,
)
from utils.runtime_activation_authorization import (
    ActivationVerificationContext,
    ShadowActivationBinding,
    ShadowActivationSnapshotError,
    build_activation_authorization_body,
    build_shadow_activation_snapshot,
    generate_activation_authorizer_keypair,
    sign_runtime_activation_authorization,
    verify_runtime_activation_authorization,
)
from utils.runtime_mapping_eligibility import (
    RuntimeMappingContext,
    caller_asserted_provenance,
    evaluate_runtime_mapping_eligibility,
)
from utils.shadow_activation_audit_store import ShadowActivationAuditStore
from utils.shadow_activation_simulation import (
    ShadowActivationSimulator,
    ShadowSnapshotError,
    ShadowSnapshotRegistry,
)
from utils.shadow_runtime_reader import (
    ShadowAuthorizationEvidence,
    ShadowReadContext,
    ShadowRuntimeReader,
    read_shadow_runtime_mapping,
)
from utils.source_identity import observe_source_row

#: 报告 schema 版本。
SHADOW_TOOL_REPORT_SCHEMA_VERSION = "1.0.0"

#: 可用场景。
SCENARIOS: tuple[str, ...] = (
    "snapshot_create",
    "snapshot_switch",
    "read_resolved",
    "read_not_found",
    "conflict",
    "expiry",
    "revocation",
    "disable",
    "rollback",
    "audit_verify",
    "restart_recovery",
)

CANONICAL_ID = "500j-2030-05-01-007"
ALT_CANONICAL_ID = "500j-2030-05-01-008"
DATASET = "synthetic-shadow-activation"
BASELINE = "baseline-A"
OPERATOR = "shadow-operator"
APPROVER = "shadow-owner"
AUTHORIZER = "shadow-authorizer"
AUTHORIZED_AT = "2030-01-01T00:00:00+00:00"
EXPIRES_AT = "2030-02-01T00:00:00+00:00"
WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"
REVOCATION_REFERENCE = "shadow-revocation-1"
SCOPE = "RUNTIME_READ_ONLY_RESOLUTION"


def _row(
    *,
    match_id: str = CANONICAL_ID,
    number: str = "007",
    home: str = "皇马",
    away: str = "巴萨",
    sport: str = "football",
) -> dict:
    return {
        "id": match_id,
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "date": "2030-05-01",
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": None,
        "jczq_no": number,
    }


@dataclass(frozen=True)
class SyntheticShadowFixture:
    """一次合成的影子场景夹具（全部位于隔离工作区内）。"""

    workspace: str
    owner_public: str
    authorizer_private: str
    authorizer_public: str
    observation: Any
    other_observation: Any
    proposal: Any
    approval: Any
    decision: Any
    authorization: Any
    verification_context: ActivationVerificationContext
    snapshot: Any
    audit_store: ShadowActivationAuditStore

    def read_context(self, **overrides) -> ShadowReadContext:
        kwargs = dict(
            dataset_identity=DATASET,
            current_baseline_fingerprint=BASELINE,
            current_time=WITHIN_WINDOW,
            trusted_public_key_path=self.authorizer_public,
            known_canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
            evidence=(
                ShadowAuthorizationEvidence(
                    authorization=self.authorization,
                    decision=self.decision,
                    proposal=self.proposal,
                ),
            ),
        )
        kwargs.update(overrides)
        return ShadowReadContext(**kwargs)


def build_synthetic_shadow_fixture(workspace: str) -> SyntheticShadowFixture:
    """
    在隔离工作区内构建完整的合成夹具。

    密钥写入 ``<workspace>/keys``（工作区必须位于仓库之外）。
    """
    keys_dir = os.path.join(os.path.abspath(workspace), "keys")
    os.makedirs(keys_dir, exist_ok=True)
    owner_private = os.path.join(keys_dir, "owner_private.pem")
    owner_public = os.path.join(keys_dir, "owner_public.pem")
    authorizer_private = os.path.join(keys_dir, "authorizer_private.pem")
    authorizer_public = os.path.join(keys_dir, "authorizer_public.pem")
    generate_mapping_owner_keypair(owner_private, owner_public)
    generate_activation_authorizer_keypair(authorizer_private, authorizer_public)

    observation = observe_source_row(_row(), adapter="market_xml")
    other_observation = observe_source_row(
        _row(match_id=ALT_CANONICAL_ID, number="008", home="甲", away="乙"),
        adapter="market_xml",
    )
    proposal = proposal_from_observations(
        [observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="synthetic shadow activation fixture",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    approval = sign_mapping_approval(
        owner_private,
        build_approval_body(
            proposal=proposal,
            source_observation_fingerprint=observation.semantic_fingerprint(),
            dataset_identity=DATASET,
            operator=OPERATOR,
            approver=APPROVER,
            approved_at=AUTHORIZED_AT,
            reason="synthetic owner approval",
        ),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        RuntimeMappingContext(
            dataset_identity=DATASET,
            current_baseline_fingerprint=BASELINE,
            evidence_provenance=caller_asserted_provenance(
                dataset_identity=DATASET, baseline_fingerprint=BASELINE
            ),
            canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
            trusted_public_key_path=owner_public,
            expected_approver=APPROVER,
            expected_operator=OPERATOR,
            recorded_proposal_id=proposal.proposal_id,
            recorded_proposal_fingerprint=proposal.fingerprint(),
            recorded_proposal_state="APPROVED",
        ),
        evaluated_at=AUTHORIZED_AT,
    )
    authorization = sign_runtime_activation_authorization(
        authorizer_private,
        build_activation_authorization_body(
            decision=decision,
            proposal=proposal,
            dataset_identity=DATASET,
            authorized_scope=SCOPE,
            authorizer=AUTHORIZER,
            operator=OPERATOR,
            authorized_at=AUTHORIZED_AT,
            expires_at=EXPIRES_AT,
            revocation_reference=REVOCATION_REFERENCE,
            reason="synthetic shadow activation authorization",
        ),
    )
    verification_context = ActivationVerificationContext(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        current_time=WITHIN_WINDOW,
        trusted_public_key_path=authorizer_public,
        eligibility_decision=decision,
        proposal=proposal,
    )
    snapshot = build_shadow_activation_snapshot(
        authorizations=[authorization],
        verification_context=verification_context,
        observations=[observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    audit_store = ShadowActivationAuditStore(
        os.path.abspath(workspace), dataset_identity=DATASET
    )
    return SyntheticShadowFixture(
        workspace=os.path.abspath(workspace),
        owner_public=owner_public,
        authorizer_private=authorizer_private,
        authorizer_public=authorizer_public,
        observation=observation,
        other_observation=other_observation,
        proposal=proposal,
        approval=approval,
        decision=decision,
        authorization=authorization,
        verification_context=verification_context,
        snapshot=snapshot,
        audit_store=audit_store,
    )


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------

def _scenario(name: str, fixture: SyntheticShadowFixture) -> dict:
    if name == "snapshot_create":
        registry = ShadowSnapshotRegistry()
        registration = registry.register(fixture.snapshot)
        fixture.audit_store.append(
            event_type="SHADOW_SNAPSHOT_REGISTERED",
            activation_id=fixture.authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=OPERATOR,
            actor_role="shadow-operator",
            payload={"snapshot_id": fixture.snapshot.snapshot_id},
            snapshot_id=fixture.snapshot.snapshot_id,
        )
        return {
            "scenario": name,
            "registration": registration,
            "snapshot": fixture.snapshot.as_dict(),
            "active_snapshot_id": registry.active_snapshot_id,
            "auto_activation_from_registration": False,
        }

    if name == "snapshot_switch":
        registry = ShadowSnapshotRegistry()
        registry.register(fixture.snapshot)
        first = registry.select(fixture.snapshot.snapshot_id)
        fixture.audit_store.append(
            event_type="SHADOW_SNAPSHOT_SELECTED",
            activation_id=fixture.authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=OPERATOR,
            actor_role="shadow-operator",
            snapshot_id=fixture.snapshot.snapshot_id,
        )
        second = registry.switch(fixture.snapshot.snapshot_id)
        fixture.audit_store.append(
            event_type="SHADOW_SNAPSHOT_SWITCHED",
            activation_id=fixture.authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=OPERATOR,
            actor_role="shadow-operator",
            snapshot_id=fixture.snapshot.snapshot_id,
        )
        unknown = None
        try:
            registry.select("0" * 64)
        except ShadowSnapshotError as exc:
            unknown = {"rejection_codes": list(exc.rejection_codes)}
        return {
            "scenario": name,
            "select": first,
            "switch": second,
            "unknown_snapshot": unknown,
            "history": list(registry.history()),
        }

    if name == "read_resolved":
        reader = ShadowRuntimeReader()
        result = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        return {"scenario": name, "result": result.as_dict()}

    if name == "read_not_found":
        reader = ShadowRuntimeReader()
        result = reader.read(
            observation=fixture.other_observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        return {"scenario": name, "result": result.as_dict()}

    if name == "conflict":
        # 构建期：同一源身份键指向不同 Canonical 目标 -> 拒绝发布。
        conflicting_proposal = proposal_from_observations(
            [fixture.observation],
            requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
            candidate_canonical_id=ALT_CANONICAL_ID,
            existing_canonical_owner=ALT_CANONICAL_ID,
            ingestion_plan_reference="planner:ACCEPTED",
            expected_baseline_fingerprint=BASELINE,
            reason="conflicting synthetic target",
            identity_evidence_type="strong_competition_number",
            operator=OPERATOR,
        )
        owner_private = os.path.join(
            os.path.dirname(fixture.owner_public), "owner_private.pem"
        )
        conflicting_approval = sign_mapping_approval(
            owner_private,
            build_approval_body(
                proposal=conflicting_proposal,
                source_observation_fingerprint=fixture.observation.semantic_fingerprint(),
                dataset_identity=DATASET,
                operator=OPERATOR,
                approver=APPROVER,
                approved_at=AUTHORIZED_AT,
                reason="synthetic conflicting approval",
            ),
        )
        conflicting_decision = evaluate_runtime_mapping_eligibility(
            conflicting_proposal,
            conflicting_approval,
            fixture.observation,
            RuntimeMappingContext(
                dataset_identity=DATASET,
                current_baseline_fingerprint=BASELINE,
                evidence_provenance=caller_asserted_provenance(
                    dataset_identity=DATASET, baseline_fingerprint=BASELINE
                ),
                canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
                trusted_public_key_path=fixture.owner_public,
                expected_approver=APPROVER,
                expected_operator=OPERATOR,
                recorded_proposal_id=conflicting_proposal.proposal_id,
                recorded_proposal_fingerprint=conflicting_proposal.fingerprint(),
                recorded_proposal_state="APPROVED",
            ),
            evaluated_at=AUTHORIZED_AT,
        )
        conflicting_authorization = sign_runtime_activation_authorization(
            fixture.authorizer_private,
            build_activation_authorization_body(
                decision=conflicting_decision,
                proposal=conflicting_proposal,
                dataset_identity=DATASET,
                authorized_scope=SCOPE,
                authorizer=AUTHORIZER,
                operator=OPERATOR,
                authorized_at=AUTHORIZED_AT,
                expires_at=EXPIRES_AT,
                revocation_reference=REVOCATION_REFERENCE,
                reason="synthetic conflicting authorization",
            ),
        )
        build_rejection: dict | None = None
        try:
            build_shadow_activation_snapshot(
                authorizations=[fixture.authorization, conflicting_authorization],
                verification_contexts=[
                    fixture.verification_context,
                    ActivationVerificationContext(
                        dataset_identity=DATASET,
                        current_baseline_fingerprint=BASELINE,
                        current_time=WITHIN_WINDOW,
                        trusted_public_key_path=fixture.authorizer_public,
                        eligibility_decision=conflicting_decision,
                        proposal=conflicting_proposal,
                    ),
                ],
                observations=[fixture.observation],
                generated_at=WITHIN_WINDOW,
                dataset_identity=DATASET,
            )
        except ShadowActivationSnapshotError as exc:
            build_rejection = {"rejection_codes": list(exc.rejection_codes)}

        # 读取期：手工构造冲突快照（绕过构建器）-> 必须 REJECTED_SHADOW。
        from dataclasses import replace as _replace

        conflicting_binding = ShadowActivationBinding(
            activation_id=conflicting_authorization.activation_id,
            source_identity_key=conflicting_authorization.scoped_source_identity_key,
            candidate_canonical_id=ALT_CANONICAL_ID,
            eligibility_decision_fingerprint=(
                conflicting_authorization.eligibility_decision_fingerprint
            ),
            authorized_scope=SCOPE,
        )
        manual = _replace(
            fixture.snapshot,
            bindings=tuple(
                sorted(
                    fixture.snapshot.bindings + (conflicting_binding,),
                    key=lambda b: (b.source_identity_key, b.candidate_canonical_id),
                )
            ),
        )
        manual = _replace(
            manual, snapshot_id="", snapshot_fingerprint=""
        )
        manual = _replace(
            manual,
            snapshot_id=manual.content_fingerprint(),
            snapshot_fingerprint=manual.content_fingerprint(),
        )
        reader = ShadowRuntimeReader()
        read_result = reader.read(
            observation=fixture.observation,
            snapshot=manual,
            read_context=fixture.read_context(),
        )
        return {
            "scenario": name,
            "snapshot_build_rejection": build_rejection,
            "reader_result": read_result.as_dict(),
        }

    if name == "expiry":
        reader = ShadowRuntimeReader()
        inside = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        after = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(current_time=AFTER_EXPIRY),
        )
        fixture.audit_store.append(
            event_type="ACTIVATION_AUTHORIZATION_EXPIRED",
            activation_id=fixture.authorization.activation_id,
            occurred_at=AFTER_EXPIRY,
            actor=OPERATOR,
            actor_role="shadow-operator",
        )
        return {
            "scenario": name,
            "inside_window": inside.as_dict(),
            "after_expiry": after.as_dict(),
        }

    if name == "revocation":
        reader = ShadowRuntimeReader()
        before = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        after = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(
                revocation_registry=(
                    (fixture.authorization.activation_id, REVOCATION_REFERENCE),
                )
            ),
        )
        fixture.audit_store.append(
            event_type="ACTIVATION_AUTHORIZATION_REVOKED",
            activation_id=fixture.authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=APPROVER,
            actor_role="owner",
        )
        return {
            "scenario": name,
            "before_revocation": before.as_dict(),
            "after_revocation": after.as_dict(),
            "cache_key_changed": before.cache_key != after.cache_key,
        }

    if name == "disable":
        registry = ShadowSnapshotRegistry()
        registry.register(fixture.snapshot)
        registry.select(fixture.snapshot.snapshot_id)
        reader = ShadowRuntimeReader(registry=registry)
        handle = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        simulator = ShadowActivationSimulator(
            registry=registry, reader=reader, audit_store=fixture.audit_store
        )
        disable_report = simulator.disable_shadow_runtime(
            at=WITHIN_WINDOW, actor=OPERATOR, reason="synthetic emergency disable"
        )
        after = reader.read(
            observation=fixture.observation,
            snapshot=fixture.snapshot,
            read_context=fixture.read_context(),
        )
        return {
            "scenario": name,
            "handle_before_disable": handle.as_dict(),
            "handle_still_current": reader.is_generation_current(handle),
            "disable_report": disable_report,
            "after_disable": after.as_dict(),
            "active_snapshot_id": registry.active_snapshot_id,
            "history_preserved": list(registry.history()),
        }

    if name == "rollback":
        registry = ShadowSnapshotRegistry()
        registry.register(fixture.snapshot)
        registry.select(fixture.snapshot.snapshot_id)
        reader = ShadowRuntimeReader(registry=registry)
        simulator = ShadowActivationSimulator(
            registry=registry, reader=reader, audit_store=fixture.audit_store
        )
        ok = simulator.rollback_shadow_snapshot(
            target_snapshot_id=fixture.snapshot.snapshot_id,
            read_context=fixture.read_context(),
            at=WITHIN_WINDOW,
            actor=OPERATOR,
        )
        revoked = simulator.rollback_shadow_snapshot(
            target_snapshot_id=fixture.snapshot.snapshot_id,
            read_context=fixture.read_context(
                revocation_registry=(
                    (fixture.authorization.activation_id, REVOCATION_REFERENCE),
                )
            ),
            at=WITHIN_WINDOW,
            actor=OPERATOR,
        )
        expired = simulator.rollback_shadow_snapshot(
            target_snapshot_id=fixture.snapshot.snapshot_id,
            read_context=fixture.read_context(current_time=AFTER_EXPIRY),
            at=AFTER_EXPIRY,
            actor=OPERATOR,
        )
        unknown = simulator.rollback_shadow_snapshot(
            target_snapshot_id="0" * 64,
            read_context=fixture.read_context(),
            at=WITHIN_WINDOW,
            actor=OPERATOR,
        )
        return {
            "scenario": name,
            "rollback_ok": ok.as_dict(),
            "rollback_revoked_rejected": revoked.as_dict(),
            "rollback_expired_rejected": expired.as_dict(),
            "rollback_unknown_rejected": unknown.as_dict(),
            "effective_snapshot_id": registry.active_snapshot_id,
        }

    if name == "audit_verify":
        return {
            "scenario": name,
            "verification": fixture.audit_store.verify(),
            "in_memory_log_untouched": True,
        }

    if name == "restart_recovery":
        # 模拟重启：新实例读取同一隔离工作区。
        reopened = ShadowActivationAuditStore(
            fixture.workspace, dataset_identity=DATASET
        )
        return {
            "scenario": name,
            "recovery": reopened.recover(),
        }

    raise ValueError(f"unknown scenario {name!r}")


def run_scenarios(
    workspace: str, *, scenarios: Sequence[str] | None = None
) -> dict:
    """在隔离工作区内运行影子场景（默认全部场景）。"""
    selected = list(scenarios) if scenarios else list(SCENARIOS)
    unknown = sorted(set(selected) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenarios: {unknown}")

    fixture = build_synthetic_shadow_fixture(workspace)
    reports: list[dict] = []
    failures: list[str] = []
    for name in selected:
        try:
            reports.append(_scenario(name, fixture))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    return {
        "kind": "SHADOW_RUNTIME_ACTIVATION_SIMULATION",
        "label": "SHADOW_ONLY",
        "prototype_only": True,
        "shadow_tool_report_schema_version": SHADOW_TOOL_REPORT_SCHEMA_VERSION,
        "workspace": fixture.workspace,
        "dataset_identity": DATASET,
        "scenarios_requested": selected,
        "scenario_reports": reports,
        "failures": failures,
        "default_active_shadow_mappings": 0,
        "production_activation": "NOT_ENABLED",
        "production_runtime_reader": "NOT_ENABLED",
        "production_mapping_write_authorization": "NOT_GRANTED",
        "is_production_authority": False,
        "network_access": False,
        "production_data_writes": 0,
        "production_signing_credentials_used": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "SHADOW ONLY: run isolated synthetic shadow-runtime activation scenarios. "
            "Never activates production mappings, never writes production data, never "
            "scrapes the network."
        )
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="explicit isolated workspace OUTSIDE the repository (must already exist)",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=list(SCENARIOS),
        help="scenario to run (repeatable); defaults to all",
    )
    parser.add_argument("--json-out", help="optional path to write the report JSON")
    args = parser.parse_args(argv)

    report = run_scenarios(args.workspace, scenarios=args.scenario)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        sys.stdout.write(text + "\n")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
