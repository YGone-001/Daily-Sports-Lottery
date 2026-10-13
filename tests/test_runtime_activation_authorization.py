"""
运行期激活授权原型与影子执行测试套件（AA-01 .. AA-26）
====================================================
覆盖：
- 授权工件结构、签名验证、资格 / 提案 / 数据集 / 范围绑定；
- 生命周期状态机、有效期与撤销；
- 影子快照的确定性与不可变性、影子运行期解析；
- 「批准 ≠ 激活」「资格 ≠ 激活」「激活不得授权写入」；
- 影子执行绝不写盘、绝不改 Canonical、绝不触碰七个持久化存储；
- 生产运行期隔离：无导入、无激活标志、无生产签名密钥依赖。

全部使用隔离的合成夹具与临时目录中的一次性 Ed25519 密钥，绝不访问网络，
绝不触碰生产数据，绝不激活任何运行期映射。
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import os
import re
from dataclasses import dataclass

import pytest

from utils import fetcher_500, scraper
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    plan_ingestion_identity,
)
from utils.mapping_governance import (
    MAPPING_APPROVAL_PURPOSE,
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    ProposalAction,
    build_approval_body,
    generate_mapping_owner_keypair,
    governance_status,
    proposal_from_observations,
    sign_mapping_approval,
)
from utils.runtime_activation_authorization import (
    ACTIVATION_LIFECYCLE_TRANSITIONS,
    ARTIFACT_FIELD_NAMES,
    AUTHORIZATION_REJECTION_CODE_SET,
    CONSUMABLE_LIFECYCLE_STATES,
    FORBIDDEN_SCOPE_TOKENS,
    MAY_BE_EMPTY_FIELDS,
    NON_ACTIVATABLE_LIFECYCLE_STATES,
    NON_CONSUMABLE_REJECTION_CODES,
    PROTOTYPE_ALLOWED_ACTIVATION_SCOPES,
    PRODUCTION_ACTIVATION,
    PRODUCTION_RUNTIME_READER,
    PROTOTYPE_ONLY,
    RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE,
    RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION,
    SHADOW_AUDIT_EVENT_TYPES,
    ActivationAuthorizationError,
    ActivationLifecycleRecord,
    ActivationLifecycleState,
    ActivationPrototypeError,
    ActivationVerificationContext,
    AuthorizationVerificationResult,
    InvalidActivationTransitionError,
    RuntimeActivationAuthorization,
    ShadowActivationAuditLog,
    ShadowActivationSnapshot,
    ShadowActivationSnapshotError,
    ShadowResolutionStatus,
    advance_activation_lifecycle,
    build_activation_authorization_body,
    build_shadow_activation_snapshot,
    derive_lifecycle_state,
    evaluate_shadow_runtime_mapping,
    generate_activation_authorizer_keypair,
    shadow_activation_prototype_status,
    sign_runtime_activation_authorization,
    validate_activation_transition,
    verify_runtime_activation_authorization,
)
from utils.runtime_activation_design import (
    ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS,
    ALLOWED_ACTIVATION_SCOPES,
)
from utils.runtime_mapping_eligibility import (
    RuntimeMappingContext,
    caller_asserted_provenance,
    evaluate_runtime_mapping_eligibility,
)
from utils.runtime_mapping_policy import (
    RUNTIME_ELIGIBILITY_RULES,
    RUNTIME_MAPPING_ACTIVATION,
    RUNTIME_MAPPING_POLICY_VERSION,
)
from utils.source_identity import (
    ScopedSourceIdentityKey,
    observe_source_row,
)

CANONICAL_ID = "500j-2030-05-01-007"
ALT_CANONICAL_ID = "500j-2030-05-01-008"
DATASET = "synthetic-activation-prototype"
BASELINE = "baseline-A"
OPERATOR = "operator-1"
APPROVER = "owner-1"
AUTHORIZER = "authorizer-1"
AUTHORIZED_AT = "2030-01-01T00:00:00+00:00"
EXPIRES_AT = "2030-02-01T00:00:00+00:00"
EVAL_AT = "2030-01-01T00:00:00+00:00"
WITHIN_WINDOW = "2030-01-15T00:00:00+00:00"
AFTER_EXPIRY = "2030-03-01T00:00:00+00:00"
REVOCATION_REFERENCE = "revocation-ref-1"
SCOPE = "RUNTIME_READ_ONLY_RESOLUTION"

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
PROTOTYPE_MODULE = os.path.join(
    REPO_ROOT, "utils", "runtime_activation_authorization.py"
)


# ---------------------------------------------------------------------------
# 合成夹具
# ---------------------------------------------------------------------------

def _market_row(
    *,
    match_id: str = CANONICAL_ID,
    number: str = "007",
    home: str = "皇马",
    away: str = "巴萨",
    time: str = "20:00",
    sport: str = "football",
) -> dict:
    return {
        "id": match_id,
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "date": "2030-05-01",
        "time": time,
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": None,
        "jczq_no": number,
    }


@dataclass(frozen=True)
class _Env:
    owner_private: str
    owner_public: str
    authorizer_private: str
    authorizer_public: str
    other_public: str
    observation: object
    proposal: object
    approval: dict
    decision: object
    authorization: RuntimeActivationAuthorization


@pytest.fixture(scope="module")
def env(tmp_path_factory) -> _Env:
    """构建完整的合成栈：观测 -> 提案 -> 批准 -> 资格判定 -> 激活授权。"""
    base = tmp_path_factory.mktemp("activation_prototype_keys")
    owner_private = str(base / "owner_private.pem")
    owner_public = str(base / "owner_public.pem")
    authorizer_private = str(base / "authorizer_private.pem")
    authorizer_public = str(base / "authorizer_public.pem")
    other_private = str(base / "other_private.pem")
    other_public = str(base / "other_public.pem")

    generate_mapping_owner_keypair(owner_private, owner_public)
    generate_activation_authorizer_keypair(authorizer_private, authorizer_public)
    generate_activation_authorizer_keypair(other_private, other_public)

    observation = observe_source_row(_market_row(), adapter="market_xml")
    proposal = proposal_from_observations(
        [observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="activation prototype fixture",
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
            approved_at=EVAL_AT,
            reason="owner approves the proposed binding",
        ),
    )
    decision = evaluate_runtime_mapping_eligibility(
        proposal,
        approval,
        observation,
        _eligibility_context(owner_public, proposal),
        evaluated_at=EVAL_AT,
    )
    authorization = _sign(
        authorizer_private, _body(decision=decision, proposal=proposal)
    )
    return _Env(
        owner_private=owner_private,
        owner_public=owner_public,
        authorizer_private=authorizer_private,
        authorizer_public=authorizer_public,
        other_public=other_public,
        observation=observation,
        proposal=proposal,
        approval=approval,
        decision=decision,
        authorization=authorization,
    )


def _eligibility_context(trusted_public_key: str, proposal, **overrides):
    kwargs = dict(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        evidence_provenance=caller_asserted_provenance(
            dataset_identity=DATASET, baseline_fingerprint=BASELINE
        ),
        canonical_ids=(CANONICAL_ID, ALT_CANONICAL_ID),
        trusted_public_key_path=trusted_public_key,
        expected_approver=APPROVER,
        expected_operator=OPERATOR,
        recorded_proposal_id=proposal.proposal_id,
        recorded_proposal_fingerprint=proposal.fingerprint(),
        recorded_proposal_state="APPROVED",
    )
    kwargs.update(overrides)
    return RuntimeMappingContext(**kwargs)


def _body(
    *,
    decision,
    proposal,
    authorized_scope: str = SCOPE,
    expires_at: str = EXPIRES_AT,
    authorized_at: str = AUTHORIZED_AT,
    dataset_identity: str = DATASET,
    revocation_reference: str = REVOCATION_REFERENCE,
    **overrides,
):
    kwargs = dict(
        decision=decision,
        proposal=proposal,
        dataset_identity=dataset_identity,
        authorized_scope=authorized_scope,
        authorizer=AUTHORIZER,
        operator=OPERATOR,
        authorized_at=authorized_at,
        expires_at=expires_at,
        revocation_reference=revocation_reference,
        reason="activation prototype",
    )
    kwargs.update(overrides)
    return build_activation_authorization_body(**kwargs)


def _sign(private_key_path: str, body) -> RuntimeActivationAuthorization:
    return sign_runtime_activation_authorization(private_key_path, body)


def _vctx(env: _Env, **overrides) -> ActivationVerificationContext:
    kwargs = dict(
        dataset_identity=DATASET,
        current_baseline_fingerprint=BASELINE,
        current_time=WITHIN_WINDOW,
        trusted_public_key_path=env.authorizer_public,
        eligibility_decision=env.decision,
        proposal=env.proposal,
    )
    kwargs.update(overrides)
    return ActivationVerificationContext(**kwargs)


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
    from utils.daily_loader import save_json

    save_json("daily_matches.json", {"meta": {}, "matches": [_market_row()]})
    save_json("team_strength.json", {"teams": {"皇马": {"elo_rating": 1500}}})
    save_json("calibrated.json", {"ids": [CANONICAL_ID]})
    save_json("prediction_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("odds_snapshots.json", {"version": 1, "snapshots": {}})
    save_json("settlements.json", {"version": 1, "settlements": {}})
    save_json("evaluation_rows.json", {"version": 1, "rows": {}})


# ---------------------------------------------------------------------------
# AA-01
# ---------------------------------------------------------------------------

def test_aa_01_valid_authorization_verifies(env):
    """AA-01: 合法的激活授权通过验证，且工件覆盖全部设计必填字段。"""
    assert PROTOTYPE_ONLY is True
    assert isinstance(env.authorization, RuntimeActivationAuthorization)

    artifact = env.authorization.as_dict()
    assert set(artifact) >= set(ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS)
    assert set(ARTIFACT_FIELD_NAMES) >= set(ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS)
    assert artifact["authorization_schema_version"] == (
        RUNTIME_ACTIVATION_AUTHORIZATION_SCHEMA_VERSION
    )
    assert artifact["purpose"] == RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE
    assert artifact["signature_algorithm"] == "ed25519"
    assert artifact["signer_public_key_id"].startswith("sha256:")
    assert artifact["signature"]

    result = verify_runtime_activation_authorization(env.authorization, _vctx(env))
    assert isinstance(result, AuthorizationVerificationResult)
    assert result.verification_status == "VERIFIED"
    assert result.rejection_codes == ()
    assert result.is_verified is True
    assert result.is_consumable is True
    assert result.verified_scope == SCOPE

    # 工件不可变、可哈希、确定性。
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.authorization.signature = "x"  # type: ignore[misc]
    assert hash(env.authorization) == hash(
        RuntimeActivationAuthorization.from_dict(artifact)
    )
    assert env.authorization.to_json() == (
        RuntimeActivationAuthorization.from_dict(artifact).to_json()
    )


# ---------------------------------------------------------------------------
# AA-02 .. AA-04 — 密码学与契约面
# ---------------------------------------------------------------------------

def test_aa_02_invalid_signature_is_rejected(env):
    """AA-02: 签名无效（被篡改）-> SIGNATURE_INVALID。"""
    raw = env.authorization.signature
    flipped = ("A" if raw[0] != "A" else "B") + raw[1:]
    tampered = dataclasses.replace(env.authorization, signature=flipped)

    result = verify_runtime_activation_authorization(tampered, _vctx(env))
    assert result.verification_status == "REJECTED"
    assert "SIGNATURE_INVALID" in result.rejection_codes
    assert result.is_consumable is False

    # 空签名同样被拒绝。
    empty = dataclasses.replace(env.authorization, signature="")
    assert "SIGNATURE_INVALID" in verify_runtime_activation_authorization(
        empty, _vctx(env)
    ).rejection_codes


def test_aa_03_unknown_signer_is_rejected(env):
    """AA-03: 不可信签名密钥 -> SIGNER_KEY_UNTRUSTED。"""
    result = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, trusted_public_key_path=env.other_public)
    )
    assert result.verification_status == "REJECTED"
    assert "SIGNER_KEY_UNTRUSTED" in result.rejection_codes

    # 缺少信任锚时单独报告 TRUST_ANCHOR_MISSING。
    missing = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, trusted_public_key_path=None)
    )
    assert "TRUST_ANCHOR_MISSING" in missing.rejection_codes


def test_aa_04_unsupported_schema_is_rejected(env):
    """AA-04: 不支持的 schema 版本 -> SCHEMA_VERSION_UNSUPPORTED。"""
    body = _body(decision=env.decision, proposal=env.proposal)
    body["authorization_schema_version"] = "0.9.0"
    legacy = _sign(env.authorizer_private, body)

    result = verify_runtime_activation_authorization(legacy, _vctx(env))
    assert result.verification_status == "REJECTED"
    assert "SCHEMA_VERSION_UNSUPPORTED" in result.rejection_codes
    # 签名本身仍然有效（只改 schema，不改签名）-> 说明拒绝来自契约而非密码学。
    assert "SIGNATURE_INVALID" not in result.rejection_codes

    # 用途不匹配同样被拒绝（域分离）。
    wrong_purpose = _body(decision=env.decision, proposal=env.proposal)
    wrong_purpose["purpose"] = MAPPING_APPROVAL_PURPOSE
    mismatched = _sign(env.authorizer_private, wrong_purpose)
    purpose_result = verify_runtime_activation_authorization(mismatched, _vctx(env))
    assert "PURPOSE_MISMATCH" in purpose_result.rejection_codes

    # 非 ed25519 算法被拒绝（手工构造以绕过签发期的算法归一化）。
    bad_alg = dataclasses.replace(env.authorization, signature_algorithm="rsa")
    alg_result = verify_runtime_activation_authorization(bad_alg, _vctx(env))
    assert "SIGNATURE_ALGORITHM_UNSUPPORTED" in alg_result.rejection_codes


# ---------------------------------------------------------------------------
# AA-05 .. AA-08 — 绑定面
# ---------------------------------------------------------------------------

def test_aa_05_eligibility_fingerprint_mismatch_is_rejected(env):
    """AA-05: 资格判定指纹不一致（判定被改动 / 过期）-> 拒绝。"""
    # 同一判定身份（decision_id 与时钟无关），但内容指纹不同 -> 精确命中指纹检查。
    later = evaluate_runtime_mapping_eligibility(
        env.proposal,
        env.approval,
        env.observation,
        _eligibility_context(env.owner_public, env.proposal),
        evaluated_at="2030-01-01T00:00:01+00:00",
    )
    assert later.decision_id == env.decision.decision_id

    signed_over_later = _sign(
        env.authorizer_private, _body(decision=later, proposal=env.proposal)
    )
    result = verify_runtime_activation_authorization(
        signed_over_later, _vctx(env, eligibility_decision=env.decision)
    )
    assert result.verification_status == "REJECTED"
    assert "ELIGIBILITY_DECISION_FINGERPRINT_MISMATCH" in result.rejection_codes
    assert "ELIGIBILITY_DECISION_ID_MISMATCH" not in result.rejection_codes

    # 缺少判定 -> ELIGIBILITY_DECISION_MISSING。
    missing = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, eligibility_decision=None)
    )
    assert "ELIGIBILITY_DECISION_MISSING" in missing.rejection_codes

    # 被改动的判定（不同结论）-> ID 与指纹双双不一致。
    altered = evaluate_runtime_mapping_eligibility(
        env.proposal,
        None,
        env.observation,
        _eligibility_context(env.owner_public, env.proposal),
        evaluated_at=EVAL_AT,
    )
    altered_result = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, eligibility_decision=altered)
    )
    assert "ELIGIBILITY_DECISION_ID_MISMATCH" in altered_result.rejection_codes
    assert (
        "ELIGIBILITY_DECISION_FINGERPRINT_MISMATCH" in altered_result.rejection_codes
    )


def test_aa_06_proposal_fingerprint_mismatch_is_rejected(env):
    """AA-06: 提案 ID / 内容指纹不一致 -> 拒绝。"""
    other_proposal = proposal_from_observations(
        [env.observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=CANONICAL_ID,
        existing_canonical_owner=CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="a different reason changes the content fingerprint",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    assert other_proposal.proposal_id != env.proposal.proposal_id

    signed_over_other = _sign(
        env.authorizer_private, _body(decision=env.decision, proposal=other_proposal)
    )
    result = verify_runtime_activation_authorization(
        signed_over_other, _vctx(env, proposal=env.proposal)
    )
    assert result.verification_status == "REJECTED"
    assert "PROPOSAL_ID_MISMATCH" in result.rejection_codes
    assert "PROPOSAL_FINGERPRINT_MISMATCH" in result.rejection_codes

    # 缺少提案 -> PROPOSAL_MISSING。
    missing = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, proposal=None)
    )
    assert "PROPOSAL_MISSING" in missing.rejection_codes

    # 作用域身份键与提案不一致 -> SCOPED_SOURCE_IDENTITY_KEY_MISMATCH。
    wrong_key = _body(decision=env.decision, proposal=env.proposal)
    wrong_key["scoped_source_identity_key"] = '{"identity_kind":"COMPETITION_NUMBER","identity_value":"x"}'
    key_result = verify_runtime_activation_authorization(
        _sign(env.authorizer_private, wrong_key), _vctx(env)
    )
    assert "SCOPED_SOURCE_IDENTITY_KEY_MISMATCH" in key_result.rejection_codes


def test_aa_07_dataset_baseline_mismatch_is_rejected(env):
    """AA-07: 数据集身份 / 基线不一致 -> 拒绝。"""
    stale = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, current_baseline_fingerprint="baseline-B")
    )
    assert stale.verification_status == "REJECTED"
    assert "BASELINE_STALE" in stale.rejection_codes
    assert stale.is_consumable is False

    wrong_dataset = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, dataset_identity="another-dataset")
    )
    assert "DATASET_IDENTITY_MISMATCH" in wrong_dataset.rejection_codes


def test_aa_08_unsupported_activation_scope_is_rejected(env):
    """AA-08: 未知 / 写入 / 变更范围一律拒绝。"""
    # 签发时即拒绝（fail closed）。
    for bad_scope in (
        "RUNTIME_WRITE_RESOLUTION",
        "CANONICAL_MUTATION",
        "UNKNOWN_SCOPE",
        "RUNTIME_READ_ONLY_RESOLUTION_AND_WRITE",
    ):
        with pytest.raises(ActivationAuthorizationError) as exc:
            _sign(
                env.authorizer_private,
                _body(
                    decision=env.decision, proposal=env.proposal,
                    authorized_scope=bad_scope,
                ),
            )
        assert set(exc.value.rejection_codes) & {
            "ACTIVATION_SCOPE_UNSUPPORTED",
            "ACTIVATION_SCOPE_WRITE_FORBIDDEN",
        }, bad_scope

    # 验证时同样拒绝（手工构造工件以绕过签发期守卫）。
    handcrafted = dataclasses.replace(
        env.authorization, authorized_scope="RUNTIME_WRITE_RESOLUTION"
    )
    result = verify_runtime_activation_authorization(handcrafted, _vctx(env))
    assert "ACTIVATION_SCOPE_UNSUPPORTED" in result.rejection_codes
    assert "ACTIVATION_SCOPE_WRITE_FORBIDDEN" in result.rejection_codes

    # 允许列表本身只含只读范围。
    assert PROTOTYPE_ALLOWED_ACTIVATION_SCOPES == ALLOWED_ACTIVATION_SCOPES
    assert all(
        token not in scope.upper()
        for scope in PROTOTYPE_ALLOWED_ACTIVATION_SCOPES
        for token in FORBIDDEN_SCOPE_TOKENS
    )


# ---------------------------------------------------------------------------
# AA-09 / AA-10 — 有效期与撤销
# ---------------------------------------------------------------------------

def test_aa_09_expired_authorization_is_rejected(env):
    """AA-09: 过期授权被拒绝，但仍保留为历史证据。"""
    result = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, current_time=AFTER_EXPIRY)
    )
    assert result.verification_status == "REJECTED"
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in result.rejection_codes
    assert result.is_consumable is False
    assert derive_lifecycle_state(result) is ActivationLifecycleState.EXPIRED

    # 窗口内有效。
    inside = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, current_time=WITHIN_WINDOW)
    )
    assert inside.is_consumable is True
    assert derive_lifecycle_state(inside) is ActivationLifecycleState.AUTHORIZED

    # 早于生效时间 -> NOT_YET_VALID。
    early = verify_runtime_activation_authorization(
        env.authorization,
        _vctx(env, current_time="2029-12-31T00:00:00+00:00"),
    )
    assert "NOT_YET_VALID" in early.rejection_codes

    # 时间戳不可解析 -> MALFORMED_TIMESTAMP。
    malformed = verify_runtime_activation_authorization(
        env.authorization, _vctx(env, current_time="not-a-timestamp")
    )
    assert "MALFORMED_TIMESTAMP" in malformed.rejection_codes

    # 过期授权**不得**被影子快照消费。
    with pytest.raises(ShadowActivationSnapshotError) as exc:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization],
            verification_context=_vctx(env, current_time=AFTER_EXPIRY),
            observations=[env.observation],
            generated_at=AFTER_EXPIRY,
            dataset_identity=DATASET,
        )
    assert "ACTIVATION_AUTHORIZATION_EXPIRED" in exc.value.rejection_codes

    # 历史证据保留：工件与验证结论本身仍可复核。
    assert env.authorization.activation_id
    assert result.authorization_fingerprint == env.authorization.fingerprint()


def test_aa_10_revoked_authorization_is_rejected(env):
    """AA-10: 撤销授权被拒绝（撤销登记 + 引用一致），但仍可审计。"""
    revoked = verify_runtime_activation_authorization(
        env.authorization,
        _vctx(
            env,
            revocation_registry=(
                (env.authorization.activation_id, REVOCATION_REFERENCE),
            ),
        ),
    )
    assert revoked.verification_status == "REJECTED"
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in revoked.rejection_codes
    assert revoked.is_consumable is False
    assert derive_lifecycle_state(revoked) is ActivationLifecycleState.REVOKED

    # 撤销登记与工件引用不一致 -> REVOCATION_REFERENCE_MISMATCH。
    mismatch = verify_runtime_activation_authorization(
        env.authorization,
        _vctx(
            env,
            revocation_registry=((env.authorization.activation_id, "other-ref"),),
        ),
    )
    assert "REVOCATION_REFERENCE_MISMATCH" in mismatch.rejection_codes

    # 其他 activation_id 的撤销登记不影响本工件。
    unrelated = verify_runtime_activation_authorization(
        env.authorization,
        _vctx(env, revocation_registry=(("some-other-activation", REVOCATION_REFERENCE),)),
    )
    assert unrelated.is_consumable is True

    # 撤销授权**不得**被影子快照消费。
    with pytest.raises(ShadowActivationSnapshotError) as exc:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization],
            verification_context=_vctx(
                env,
                revocation_registry=(
                    (env.authorization.activation_id, REVOCATION_REFERENCE),
                ),
            ),
            observations=[env.observation],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
        )
    assert "ACTIVATION_AUTHORIZATION_REVOKED" in exc.value.rejection_codes

    # 可审计：工件与拒绝码仍完整保留。
    assert revoked.rejection_codes == ("ACTIVATION_AUTHORIZATION_REVOKED",)


# ---------------------------------------------------------------------------
# AA-11 / AA-12 — 快照
# ---------------------------------------------------------------------------

def test_aa_11_shadow_snapshot_is_deterministic(env):
    """AA-11: 影子快照确定（同输入同指纹；与授权列表顺序无关）。"""
    first = build_shadow_activation_snapshot(
        authorizations=[env.authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    second = build_shadow_activation_snapshot(
        authorizations=[env.authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    assert first == second
    assert first.snapshot_id == second.snapshot_id
    assert first.to_json() == second.to_json()
    assert first.snapshot_id == first.snapshot_fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.snapshot_id)

    # 多条授权时与顺序无关。
    other_observation = observe_source_row(
        _market_row(match_id=ALT_CANONICAL_ID, number="008", home="甲", away="乙"),
        adapter="market_xml",
    )
    other_proposal = proposal_from_observations(
        [other_observation],
        requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
        candidate_canonical_id=ALT_CANONICAL_ID,
        existing_canonical_owner=ALT_CANONICAL_ID,
        ingestion_plan_reference="planner:ACCEPTED",
        expected_baseline_fingerprint=BASELINE,
        reason="second binding",
        identity_evidence_type="strong_competition_number",
        operator=OPERATOR,
    )
    other_approval = sign_mapping_approval(
        env.owner_private,
        build_approval_body(
            proposal=other_proposal,
            source_observation_fingerprint=other_observation.semantic_fingerprint(),
            dataset_identity=DATASET,
            operator=OPERATOR,
            approver=APPROVER,
            approved_at=EVAL_AT,
            reason="owner approves",
        ),
    )
    other_decision = evaluate_runtime_mapping_eligibility(
        other_proposal,
        other_approval,
        other_observation,
        _eligibility_context(env.owner_public, other_proposal),
        evaluated_at=EVAL_AT,
    )
    other_authorization = _sign(
        env.authorizer_private,
        _body(decision=other_decision, proposal=other_proposal),
    )

    forward = build_shadow_activation_snapshot(
        authorizations=[env.authorization, other_authorization],
        verification_contexts=[
            _vctx(env),
            _vctx(env, eligibility_decision=other_decision, proposal=other_proposal),
        ],
        observations=[env.observation, other_observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    reversed_order = build_shadow_activation_snapshot(
        authorizations=[other_authorization, env.authorization],
        verification_contexts=[
            _vctx(env, eligibility_decision=other_decision, proposal=other_proposal),
            _vctx(env),
        ],
        observations=[other_observation, env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    assert forward.snapshot_fingerprint == reversed_order.snapshot_fingerprint
    assert len(forward.bindings) == 2
    assert [b.source_identity_key for b in forward.bindings] == sorted(
        b.source_identity_key for b in forward.bindings
    )

    # 基线变化 -> 不同快照指纹。
    changed = build_shadow_activation_snapshot(
        authorizations=[env.authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at="2030-01-16T00:00:00+00:00",
        dataset_identity=DATASET,
    )
    assert changed.snapshot_fingerprint != first.snapshot_fingerprint


def test_aa_12_shadow_snapshot_is_immutable(env):
    """AA-12: 影子快照不可变、只读，且拒绝非法输入。"""
    snapshot = build_shadow_activation_snapshot(
        authorizations=[env.authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    assert isinstance(snapshot, ShadowActivationSnapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.status = "DISABLED"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.bindings = ()  # type: ignore[misc]
    assert isinstance(snapshot.bindings, tuple)
    assert hash(snapshot) == hash(snapshot)
    assert snapshot.as_dict()["prototype_only"] is True
    assert snapshot.as_dict()["is_production_authority"] is False

    # 非可消费状态 -> 拒绝构建。
    with pytest.raises(ShadowActivationSnapshotError) as exc:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization],
            verification_context=_vctx(env),
            observations=[env.observation],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
            status=ActivationLifecycleState.DISABLED.value,
        )
    assert "SNAPSHOT_STATUS_NOT_CONSUMABLE" in exc.value.rejection_codes

    # 绑定缺少对应观测 -> 拒绝构建。
    with pytest.raises(ShadowActivationSnapshotError) as exc2:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization],
            verification_context=_vctx(env),
            observations=[
                observe_source_row(
                    _market_row(match_id=ALT_CANONICAL_ID, number="008", home="甲", away="乙"),
                    adapter="market_xml",
                )
            ],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
        )
    assert "SHADOW_BINDING_OBSERVATION_MISSING" in exc2.value.rejection_codes

    # 签名无效的授权不得进入快照。
    with pytest.raises(ShadowActivationSnapshotError) as exc3:
        build_shadow_activation_snapshot(
            authorizations=[
                dataclasses.replace(env.authorization, signature="A" + env.authorization.signature[1:])
            ],
            verification_context=_vctx(env),
            observations=[env.observation],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
        )
    assert "SIGNATURE_INVALID" in exc3.value.rejection_codes

    # 陈旧基线不得进入快照。
    with pytest.raises(ShadowActivationSnapshotError) as exc4:
        build_shadow_activation_snapshot(
            authorizations=[env.authorization],
            verification_context=_vctx(env, current_baseline_fingerprint="baseline-B"),
            observations=[env.observation],
            generated_at=WITHIN_WINDOW,
            dataset_identity=DATASET,
        )
    assert "BASELINE_STALE" in exc4.value.rejection_codes


# ---------------------------------------------------------------------------
# AA-13 / AA-14 / AA-15 — 影子运行期解析
# ---------------------------------------------------------------------------

def _snapshot(env) -> ShadowActivationSnapshot:
    return build_shadow_activation_snapshot(
        authorizations=[env.authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )


def test_aa_13_shadow_evaluation_produces_expected_result(env):
    """AA-13: 影子解析给出期望结果（RESOLVED_SHADOW / NOT_FOUND / REJECTED_SHADOW）。"""
    snapshot = _snapshot(env)

    resolved = evaluate_shadow_runtime_mapping(
        snapshot=snapshot, observation=env.observation
    )
    assert resolved.resolution_status == ShadowResolutionStatus.RESOLVED_SHADOW.value
    assert resolved.is_resolved is True
    assert resolved.candidate_canonical_id == CANONICAL_ID
    assert resolved.snapshot_id == snapshot.snapshot_id
    assert resolved.decision_fingerprint == (
        env.authorization.eligibility_decision_fingerprint
    )
    assert resolved.source_identity_key == ScopedSourceIdentityKey.for_observation(
        env.observation
    ).source_key()
    assert resolved.as_dict()["is_production_authority"] is False

    # 快照中不存在的源身份 -> NOT_FOUND。
    unrelated = observe_source_row(
        _market_row(match_id=ALT_CANONICAL_ID, number="008", home="甲", away="乙"),
        adapter="market_xml",
    )
    not_found = evaluate_shadow_runtime_mapping(
        snapshot=snapshot, observation=unrelated
    )
    assert not_found.resolution_status == ShadowResolutionStatus.NOT_FOUND.value
    assert not_found.candidate_canonical_id == ""
    assert not_found.snapshot_id == snapshot.snapshot_id

    # 不可消费的快照 -> REJECTED_SHADOW。
    disabled = dataclasses.replace(
        snapshot, status=ActivationLifecycleState.DISABLED.value
    )
    rejected = evaluate_shadow_runtime_mapping(
        snapshot=disabled, observation=env.observation
    )
    assert rejected.resolution_status == ShadowResolutionStatus.REJECTED_SHADOW.value

    # 缺少观测 -> REJECTED_SHADOW。
    assert evaluate_shadow_runtime_mapping(
        snapshot=snapshot, observation=None
    ).resolution_status == ShadowResolutionStatus.REJECTED_SHADOW.value
    # 快照类型不对 -> REJECTED_SHADOW。
    assert evaluate_shadow_runtime_mapping(
        snapshot=None, observation=env.observation  # type: ignore[arg-type]
    ).resolution_status == ShadowResolutionStatus.REJECTED_SHADOW.value


def test_aa_14_shadow_evaluation_never_writes(tmp_path, env):
    """AA-14: 影子执行绝不写任何文件。"""
    workspace = tmp_path / "aa14"
    workspace.mkdir()
    (workspace / "sentinel.txt").write_text("unchanged", encoding="utf-8")
    before_listing = sorted(os.listdir(workspace))
    before_bytes = (workspace / "sentinel.txt").read_bytes()

    snapshot = _snapshot(env)
    for _ in range(3):
        evaluate_shadow_runtime_mapping(snapshot=snapshot, observation=env.observation)

    assert sorted(os.listdir(workspace)) == before_listing
    assert (workspace / "sentinel.txt").read_bytes() == before_bytes

    # 结构性证明：原型模块不含任何写入原语。
    with open(PROTOTYPE_MODULE, encoding="utf-8") as handle:
        source = handle.read()
    assert not re.search(r"open\([^)]*[\"'](?:w|a|w\+|a\+|wb|ab)[\"']", source)
    for forbidden in (
        "os.replace",
        "os.remove",
        "os.makedirs",
        "os.unlink",
        "shutil",
        "json.dump",
        "save_json",
        "atomic_write_json",
        "requests",
        "urllib",
    ):
        assert forbidden not in source, forbidden
    # 唯一的文件访问是**只读**载入签名私钥。
    assert source.count("open(") == 1
    assert 'open(private_norm, "rb")' in source

    # 审计日志是内存结构，绝不持久化。
    log = ShadowActivationAuditLog()
    log.append(
        event_type="ACTIVATION_AUTHORIZATION_CREATED",
        activation_id=env.authorization.activation_id,
        occurred_at=WITHIN_WINDOW,
        actor=AUTHORIZER,
        actor_role="authorizer",
        payload={"scope": SCOPE},
    )
    report = log.verify()
    assert report["persisted"] is False
    assert report["prototype_only"] is True
    assert sorted(os.listdir(workspace)) == before_listing


def test_aa_15_shadow_evaluation_never_changes_canonical_ids(tmp_path, env):
    """AA-15: 影子执行绝不修改 Canonical ID 或任何 Canonical 记录。"""
    canonical = [_market_row()]
    before = copy.deepcopy(canonical)

    snapshot = _snapshot(env)
    resolved = evaluate_shadow_runtime_mapping(
        snapshot=snapshot, observation=env.observation
    )

    assert canonical == before
    # 影子结果只是**观测**：它指向既有 Canonical ID，绝不生成新 ID。
    assert resolved.candidate_canonical_id == CANONICAL_ID
    assert resolved.candidate_canonical_id in {m["id"] for m in canonical}
    # 结果对象不提供任何改写入口。
    for forbidden in ("apply", "write", "commit", "persist", "rewrite"):
        assert not hasattr(resolved, forbidden)
    # 原型模块不含任何 Canonical 写入路径。
    with open(PROTOTYPE_MODULE, encoding="utf-8") as handle:
        source = handle.read()
    assert "validate_canonical_uniqueness" not in source
    assert "atomic_write_json" not in source
    assert "daily_matches" not in source


# ---------------------------------------------------------------------------
# AA-16 / AA-17 / AA-18 — 分离性
# ---------------------------------------------------------------------------

def test_aa_16_approval_cannot_replace_activation_authorization(env):
    """AA-16: 所有者批准绝不能被当作激活授权。"""
    # 用途域分离。
    assert RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE != MAPPING_APPROVAL_PURPOSE
    assert env.approval["purpose"] == MAPPING_APPROVAL_PURPOSE
    assert env.authorization.purpose == RUNTIME_ACTIVATION_AUTHORIZATION_PURPOSE

    # 直接把批准件当授权工件验证 -> 结构不符，拒绝。
    result = verify_runtime_activation_authorization(env.approval, _vctx(env))
    assert result.verification_status == "REJECTED"
    assert "MALFORMED_ARTIFACT" in result.rejection_codes

    # 把批准件的签名搬进授权工件 -> 签名不成立。
    forged = dataclasses.replace(
        env.authorization, signature=env.approval["approval"]["signature"]
    )
    forged_result = verify_runtime_activation_authorization(forged, _vctx(env))
    assert "SIGNATURE_INVALID" in forged_result.rejection_codes

    # 即使把用途改成批准用途并重新签名，验证仍然因用途不匹配而拒绝。
    wrong_purpose = _body(decision=env.decision, proposal=env.proposal)
    wrong_purpose["purpose"] = MAPPING_APPROVAL_PURPOSE
    assert "PURPOSE_MISMATCH" in verify_runtime_activation_authorization(
        _sign(env.authorizer_private, wrong_purpose), _vctx(env)
    ).rejection_codes

    # 批准件本身不足以构造可消费授权：缺少授权特有字段。
    assert "expires_at" not in env.approval
    assert "authorized_scope" not in env.approval
    assert "authorized_scope" in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS
    assert "expires_at" in ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS


def test_aa_17_eligibility_cannot_replace_activation_authorization(env):
    """AA-17: 资格判定绝不能被当作激活授权。"""
    # 判定对象本身不是授权工件。
    result = verify_runtime_activation_authorization(env.decision, _vctx(env))
    assert result.verification_status == "REJECTED"
    assert "MALFORMED_ARTIFACT" in result.rejection_codes

    # 缺少授权工件 -> 拒绝。
    assert "MALFORMED_ARTIFACT" in verify_runtime_activation_authorization(
        None, _vctx(env)
    ).rejection_codes

    # 一条 ELIGIBLE 判定**不**自动产生可消费授权。
    assert env.decision.eligibility_result == "ELIGIBLE"
    assert env.decision.is_production_authorization is False

    # 没有授权工件时，快照虽然可以构建（用于紧急停用），但**没有任何绑定**，
    # 因此任何解析都只能得到 NOT_FOUND —— 资格判定本身不产生解析能力。
    empty = build_shadow_activation_snapshot(
        authorizations=[],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    assert empty.bindings == ()
    assert empty.activation_ids == ()
    assert evaluate_shadow_runtime_mapping(
        snapshot=empty, observation=env.observation
    ).resolution_status == ShadowResolutionStatus.NOT_FOUND.value

    # 非 ELIGIBLE 判定即使被签发也一律拒绝。
    not_eligible = evaluate_runtime_mapping_eligibility(
        env.proposal,
        None,
        env.observation,
        _eligibility_context(env.owner_public, env.proposal),
        evaluated_at=EVAL_AT,
    )
    assert not_eligible.eligibility_result == "NOT_ELIGIBLE"
    signed = _sign(
        env.authorizer_private, _body(decision=not_eligible, proposal=env.proposal)
    )
    assert "ELIGIBILITY_NOT_ELIGIBLE" in verify_runtime_activation_authorization(
        signed, _vctx(env, eligibility_decision=not_eligible)
    ).rejection_codes


def test_aa_18_activation_artifact_cannot_authorize_writes(env):
    """AA-18: 激活工件绝不授权任何写入。"""
    assert PROTOTYPE_ALLOWED_ACTIVATION_SCOPES == ("RUNTIME_READ_ONLY_RESOLUTION",)
    for token in ("WRITE", "MUTATE", "REPAIR", "MIGRATE", "REWRITE", "DELETE"):
        assert token in FORBIDDEN_SCOPE_TOKENS
        assert all(
            token not in scope.upper() for scope in PROTOTYPE_ALLOWED_ACTIVATION_SCOPES
        )

    # 工件与结果对象都不提供任何写入 / 执行入口。
    for obj in (env.authorization, _snapshot(env)):
        for forbidden in ("apply", "execute", "enable", "activate", "write", "commit"):
            assert not hasattr(obj, forbidden), (type(obj).__name__, forbidden)

    status = shadow_activation_prototype_status()
    assert status["writes_files"] is False
    assert status["creates_bindings"] is False
    assert status["modifies_canonical_records"] is False
    assert status["consumed_by_production_runtime"] is False


# ---------------------------------------------------------------------------
# AA-19 — 七存储
# ---------------------------------------------------------------------------

def test_aa_19_all_seven_stores_remain_byte_identical(
    isolated_data_dir, monkeypatch, env
):
    """AA-19: 原型全流程不改动七个持久化存储。"""
    _seed_seven_stores(isolated_data_dir)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda *a, **k: [])
    scraper.refresh(verbose=False)
    before = _hash_seven_stores(isolated_data_dir)
    assert len(before) == 7 and all(len(v) == 64 for v in before.values())

    authorization = _sign(
        env.authorizer_private, _body(decision=env.decision, proposal=env.proposal)
    )
    snapshot = build_shadow_activation_snapshot(
        authorizations=[authorization],
        verification_context=_vctx(env),
        observations=[env.observation],
        generated_at=WITHIN_WINDOW,
        dataset_identity=DATASET,
    )
    evaluate_shadow_runtime_mapping(snapshot=snapshot, observation=env.observation)
    log = ShadowActivationAuditLog()
    for event_type in SHADOW_AUDIT_EVENT_TYPES:
        log.append(
            event_type=event_type,
            activation_id=authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=AUTHORIZER,
            actor_role="authorizer",
        )
    assert log.verify()["integrity"] == "OK"

    assert _hash_seven_stores(isolated_data_dir) == before


# ---------------------------------------------------------------------------
# AA-20 / AA-21 — 既有行为不变
# ---------------------------------------------------------------------------

def test_aa_20_existing_ingestion_tests_remain_unchanged():
    """AA-20: 既有摄取身份行为与测试语义不变。"""
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        plan_ingestion_identity(
            [],
            [
                _market_row(match_id="500j-2030-05-01-001", number="001"),
                _market_row(
                    match_id="500j-2030-05-01-001", number="001",
                    home="湖人", away="勇士", sport="basketball",
                ),
            ],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    with pytest.raises(CanonicalIdentityCollisionError) as dup:
        plan_ingestion_identity(
            [_market_row(match_id="dup-1"), _market_row(match_id="dup-1")], []
        )
    assert dup.value.category == "PREEXISTING_DUPLICATE"

    with open(
        os.path.join(REPO_ROOT, "utils", "ingestion_identity.py"), encoding="utf-8"
    ) as handle:
        source = handle.read()
    assert "runtime_activation" not in source
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )


def test_aa_21_existing_repair_tests_remain_unchanged():
    """AA-21: 既有生产修复授权契约不变，且未引用激活原型。"""
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

    with open(
        os.path.join(REPO_ROOT, "tools", "production_canonical_repair.py"),
        encoding="utf-8",
    ) as handle:
        source = handle.read()
    assert "runtime_activation" not in source


# ---------------------------------------------------------------------------
# AA-22 / AA-23 / AA-24 — 生产隔离
# ---------------------------------------------------------------------------

def test_aa_22_activation_module_is_not_imported_by_production_runtime():
    """AA-22: 生产运行期模块绝不导入激活原型。"""
    runtime_modules = (
        "scraper.py",
        "fetcher_500.py",
        "ingestion_identity.py",
        "match_identity.py",
        "mapping_governance.py",
        "runtime_mapping_policy.py",
        "runtime_mapping_eligibility.py",
        "provider_evidence.py",
        "source_id_migration.py",
        "daily_loader.py",
        "match_lifecycle.py",
    )
    for name in runtime_modules:
        with open(os.path.join(REPO_ROOT, "utils", name), encoding="utf-8") as handle:
            source = handle.read()
        # 绝不导入激活原型模块。
        assert "runtime_activation_authorization" not in source, name
        # 绝不引用影子快照或授权工件类型。
        assert "ShadowActivationSnapshot" not in source, name
        assert "RuntimeActivationAuthorization" not in source, name

    # 设计契约模块可以**声明**工件名称（它只是设计数据），但绝不导入原型实现。
    with open(
        os.path.join(REPO_ROOT, "utils", "runtime_activation_design.py"),
        encoding="utf-8",
    ) as handle:
        design_source = handle.read()
    assert "runtime_activation_authorization" not in design_source
    assert "ShadowActivationSnapshot" not in design_source

    with open(os.path.join(REPO_ROOT, "utils", "scraper.py"), encoding="utf-8") as handle:
        scraper_source = handle.read()
    assert "acquire_refresh_lock" in scraper_source
    assert "validate_ingestion_identity(existing, incoming)" in scraper_source
    assert "GovernanceLedger" not in scraper_source


def test_aa_23_no_production_activation_flags_exist():
    """AA-23: 不存在任何生产激活标志。"""
    assert PROTOTYPE_ONLY is True
    assert PRODUCTION_ACTIVATION == "NOT_ENABLED"
    assert PRODUCTION_RUNTIME_READER == "NOT_ENABLED"
    assert RUNTIME_MAPPING_ACTIVATION == "NOT_ENABLED"
    assert PRODUCTION_MAPPING_WRITE_AUTHORIZATION == "NOT_GRANTED"

    status = shadow_activation_prototype_status()
    assert status["production_activation"] == "NOT_ENABLED"
    assert status["production_runtime_reader"] == "NOT_ENABLED"
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"

    # 原型模块不得定义任何「启用激活」的开关名。
    import utils.runtime_activation_authorization as prototype

    for forbidden in (
        "ACTIVATION_ENABLED",
        "RUNTIME_READER_ENABLED",
        "ENABLE_ACTIVATION",
        "ACTIVATION_FLAG",
        "PRODUCTION_ACTIVATION_ENABLED",
    ):
        assert not hasattr(prototype, forbidden), forbidden


def test_aa_24_no_production_signing_keys_are_required(tmp_path, env):
    """AA-24: 不需要任何生产签名密钥；仓库内私钥一律被拒绝。"""
    # 仓库内不存在任何 PEM 私钥。
    for root, _dirs, files in os.walk(REPO_ROOT):
        if ".git" in root or "__pycache__" in root:
            continue
        for name in files:
            assert not name.lower().endswith(".pem"), os.path.join(root, name)

    # 把私钥放在仓库内 -> 拒绝（沿用既有「私钥必须在仓库外」守卫）。
    inside = os.path.join(REPO_ROOT, "synthetic_private.pem")
    with pytest.raises(Exception) as exc:
        _sign(inside, _body(decision=env.decision, proposal=env.proposal))
    assert "repository" in str(exc.value).lower() or "outside" in str(exc.value).lower()

    # 测试用的合成密钥位于临时目录（仓库之外）。
    assert not env.authorizer_private.startswith(REPO_ROOT)
    assert not env.owner_private.startswith(REPO_ROOT)
    assert tmp_path is not None


# ---------------------------------------------------------------------------
# AA-25 / AA-26 — 跨平台 CI
# ---------------------------------------------------------------------------

def _ci_workflow() -> str:
    with open(
        os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8"
    ) as handle:
        return handle.read()


def test_aa_25_ubuntu_ci_is_declared():
    """AA-25: CI 在 ubuntu-latest 上运行全量离线套件（实际结论由远端 CI 证明）。"""
    workflow = _ci_workflow()
    assert "ubuntu-latest" in workflow
    assert "Offline pytest suite" in workflow
    assert "python -m pytest -q -rs" in workflow
    assert re.search(r"os:\s*\[ubuntu-latest,\s*windows-latest\]", workflow)


def test_aa_26_windows_ci_is_declared():
    """AA-26: CI 在 windows-latest 上运行（Windows 专属代码路径必须被覆盖）。"""
    workflow = _ci_workflow()
    assert "windows-latest" in workflow
    assert "Isolated Canonical repair lifecycle" in workflow
    assert "staging-validation" in workflow


# ---------------------------------------------------------------------------
# 补充不变量
# ---------------------------------------------------------------------------

def test_lifecycle_state_machine_is_coherent(env):
    """补充：生命周期状态机自洽，且过期 / 撤销 / 停用绝不可消费。"""
    assert set(ACTIVATION_LIFECYCLE_TRANSITIONS) == {
        state.value for state in ActivationLifecycleState
    }
    assert CONSUMABLE_LIFECYCLE_STATES == {ActivationLifecycleState.ACTIVE_SHADOW.value}
    assert not (CONSUMABLE_LIFECYCLE_STATES & NON_ACTIVATABLE_LIFECYCLE_STATES)

    record = ActivationLifecycleRecord(
        activation_id=env.authorization.activation_id,
        state=ActivationLifecycleState.CREATED.value,
        updated_at=WITHIN_WINDOW,
    )
    authorized = advance_activation_lifecycle(
        record, ActivationLifecycleState.AUTHORIZED.value, at=WITHIN_WINDOW
    )
    active = advance_activation_lifecycle(
        authorized, ActivationLifecycleState.ACTIVE_SHADOW.value, at=WITHIN_WINDOW
    )
    disabled = advance_activation_lifecycle(
        active, ActivationLifecycleState.DISABLED.value, at=WITHIN_WINDOW,
        reason="emergency disable",
    )
    assert disabled.state == ActivationLifecycleState.DISABLED.value
    # 终态不可再迁移。
    with pytest.raises(InvalidActivationTransitionError):
        advance_activation_lifecycle(
            disabled, ActivationLifecycleState.ACTIVE_SHADOW.value, at=WITHIN_WINDOW
        )
    # 不得从 CREATED 直接跳到 ACTIVE_SHADOW。
    with pytest.raises(InvalidActivationTransitionError):
        validate_activation_transition(
            ActivationLifecycleState.CREATED.value,
            ActivationLifecycleState.ACTIVE_SHADOW.value,
        )
    # 未知状态一律拒绝。
    with pytest.raises(InvalidActivationTransitionError):
        validate_activation_transition("NOT_A_STATE", "DISABLED")

    # 不可激活状态确实不可消费。
    assert NON_ACTIVATABLE_LIFECYCLE_STATES == {"EXPIRED", "REVOKED", "DISABLED"}


def test_shadow_audit_log_is_append_only_and_hash_chained(env):
    """补充：影子审计日志追加式、哈希链、拒绝未知事件、且能检出篡改。"""
    log = ShadowActivationAuditLog()
    assert log.verify()["entry_count"] == 0
    assert log.chain_tip() == "0" * 64

    for index, event_type in enumerate(SHADOW_AUDIT_EVENT_TYPES):
        log.append(
            event_type=event_type,
            activation_id=env.authorization.activation_id,
            occurred_at=WITHIN_WINDOW,
            actor=AUTHORIZER,
            actor_role="authorizer",
            payload={"index": str(index)},
        )
    assert log.event_types() == SHADOW_AUDIT_EVENT_TYPES
    report = log.verify()
    assert report["integrity"] == "OK"
    assert report["entry_count"] == len(SHADOW_AUDIT_EVENT_TYPES)
    assert report["persisted"] is False
    assert "NONE" in report["trust_anchor"]

    # 前驱哈希绑定：第二条的前驱等于第一条的哈希。
    events = log.events()
    assert events[1].previous_entry_hash == events[0].entry_hash
    assert events[0].previous_entry_hash == "0" * 64

    # 未知事件类型被拒绝。
    with pytest.raises(ActivationPrototypeError):
        log.append(
            event_type="NOT_A_REAL_EVENT",
            activation_id="x",
            occurred_at=WITHIN_WINDOW,
            actor="x",
            actor_role="x",
        )

    # 篡改任一条目 -> verify() 检出。
    tampered = ShadowActivationAuditLog()
    tampered._events = list(events)  # noqa: SLF001  (测试专用：模拟内部篡改)
    tampered._events[1] = dataclasses.replace(  # noqa: SLF001
        events[1], actor="attacker"
    )
    with pytest.raises(ActivationPrototypeError):
        tampered.verify()


def test_rejection_code_registry_is_coherent():
    """补充：拒绝码注册表自洽，且非可消费码是拒绝码的子集。"""
    assert len(AUTHORIZATION_REJECTION_CODE_SET) == 26
    assert NON_CONSUMABLE_REJECTION_CODES <= AUTHORIZATION_REJECTION_CODE_SET
    assert MAY_BE_EMPTY_FIELDS
    # 允许为空的字段必须是设计必填字段的子集。
    assert set(MAY_BE_EMPTY_FIELDS) <= set(ACTIVATION_AUTHORIZATION_REQUIRED_FIELDS)
    # 策略注册表与资格规则未被本阶段改动。
    assert len(RUNTIME_ELIGIBILITY_RULES) == 45
    assert RUNTIME_MAPPING_POLICY_VERSION == "1.1.0"
