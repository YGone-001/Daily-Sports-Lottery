"""
源 ID 迁移影响清单测试套件（MI-01 .. MI-16）
==========================================
覆盖：
- 跨运动共用 500j 前缀的碰撞出现在影响清单中
- 运动作用域提议身份保持区分（解析可往返）
- Canonical ID 一律保持不变（canonical_id_rewrites == 0）
- 快照 / 结算 / 评估引用被正确枚举
- 缺失引用被报告
- 一个旧别名不得静默绑定到多个不兼容的 Canonical ID
- 不稳定的 500w 身份被阻止自动迁移
- 清单在输入排列下确定
- 清单绑定夹具基线哈希
- 清单区分「仅别名变更」与「Canonical ID 迁移」
- 清单不含任何可执行 apply 路径
- 夹具变化使陈旧清单假设失效
- 影子校验保持隔离
- 七个夹具存储逐字节不变
- 生产映射授权保持 NOT_GRANTED、运行期账本强制保持 NOT_ENABLED

全部使用隔离的合成夹具，绝不访问网络，绝不触碰生产数据。
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os

import pytest

from tools.mapping_governance_shadow import (
    shadow_migration_analysis,
)
from utils.mapping_governance import (
    PRODUCTION_MAPPING_WRITE_AUTHORIZATION,
    RUNTIME_LEDGER_ENFORCEMENT,
    MappingProposal,
    ProposalAction,
    detect_owner_conflicts,
    governance_status,
    proposal_from_observations,
)
from utils.source_id_migration import (
    ALL_IMPACT_CLASSIFICATIONS,
    CLASSIFICATION_AMBIGUOUS_MAPPING,
    CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION,
    CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT,
    CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY,
    CLASSIFICATION_UNCHANGED_CANONICAL_ID,
    CLASSIFICATION_UNSUPPORTED_EVIDENCE,
    FORBIDDEN_MANIFEST_KEYS,
    MANIFEST_KIND,
    MIGRATION_EXECUTION_AVAILABLE,
    MIGRATION_MANIFEST_SCHEMA_VERSION,
    MARKET_SOURCE_ID_CONTRACT_VERSION,
    PREFERRED_MARKET_SOURCE_ID_OPTION_ID,
    STORE_ORDER,
    MarketSourceIdKind,
    ProposedSourceMapping,
    assert_not_executable,
    build_migration_impact_manifest,
    classify_market_source_id,
    compose_sport_scoped_market_source_id,
    fixture_baseline_fingerprint,
    manifest_fingerprint,
    parse_sport_scoped_market_source_id,
    preferred_market_source_id_option,
)
from utils.source_identity import (
    IdentityKeyKind,
    ScopedSourceIdentityKey,
    observe_source_row,
)

LEGACY_SHARED = "500j-2030-05-01-001"
SPORT_SCOPED_FOOTBALL = "500jzq-2030-05-01-001"
SPORT_SCOPED_BASKETBALL = "500jlq-2030-05-01-001"
SEVEN_STORES = (
    "daily_matches.json",
    "team_strength.json",
    "calibrated.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)
DATASET = "synthetic-migration-fixture"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _market_row(
    match_id: str,
    *,
    sport: str = "football",
    date: str = "2030-05-01",
    number: str = "001",
    home: str = "皇马",
    away: str = "巴萨",
) -> dict:
    return {
        "id": match_id,
        "sport": sport,
        "league": "西甲" if sport == "football" else "NBA",
        "date": date,
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": home,
        "away": away,
        "score": None,
        "odds": {"home_win": 1.9, "draw": 3.4, "away_win": 3.8},
        "jczq_no": number,
    }


def _canonical_fixture() -> list[dict]:
    return [dict(_market_row(LEGACY_SHARED), market_tracked=True)]


def _observations() -> list:
    football = _market_row(LEGACY_SHARED)
    basketball = _market_row(
        LEGACY_SHARED, sport="basketball", home="湖人", away="勇士"
    )
    return [
        observe_source_row(football, adapter="market_xml"),
        observe_source_row(basketball, adapter="market_xml"),
    ]


def _stores() -> dict:
    return {
        "daily_matches.json": {"matches": _canonical_fixture()},
        "team_strength.json": {"teams": {"皇马": {"elo_rating": 1500}}},
        "calibrated.json": {"ids": [LEGACY_SHARED, "ghost-1"]},
        "prediction_snapshots.json": {
            "snapshots": {"s1": {"snapshot_id": "s1", "match_id": LEGACY_SHARED}}
        },
        "odds_snapshots.json": {
            "snapshots": {"o1": {"snapshot_id": "o1", "match_id": LEGACY_SHARED}}
        },
        "settlements.json": {
            "settlements": {
                "t1": {
                    "settlement_id": "t1",
                    "snapshot_id": "s1",
                    "match_id": LEGACY_SHARED,
                }
            }
        },
        "evaluation_rows.json": {
            "rows": {
                "e1": {
                    "evaluation_id": "e1",
                    "snapshot_id": "s1",
                    "settlement_id": "t1",
                    "match_id": LEGACY_SHARED,
                }
            }
        },
    }


def _mappings() -> list[ProposedSourceMapping]:
    return [
        ProposedSourceMapping(
            legacy_source_id=LEGACY_SHARED,
            proposed_source_id=SPORT_SCOPED_FOOTBALL,
            existing_canonical_id=LEGACY_SHARED,
            identity_kind=IdentityKeyKind.COMPETITION_NUMBER.value,
            identity_value="football|2030-05-01|jczq|1",
            mapping_proposal_reference="prop-football",
        ),
        ProposedSourceMapping(
            legacy_source_id=LEGACY_SHARED,
            proposed_source_id=SPORT_SCOPED_BASKETBALL,
            existing_canonical_id=LEGACY_SHARED,
            identity_kind=IdentityKeyKind.COMPETITION_NUMBER.value,
            identity_value="basketball|2030-05-01|jczq|1",
            mapping_proposal_reference="prop-basketball",
        ),
    ]


def _manifest(**overrides) -> dict:
    kwargs = dict(
        fixture_identity="synthetic-migration-fixture",
        canonical_fixture=_canonical_fixture(),
        observations=_observations(),
        proposed_mappings=_mappings(),
        dependent_stores=_stores(),
        dataset_identity=DATASET,
    )
    kwargs.update(overrides)
    return build_migration_impact_manifest(**kwargs)


def _entry(manifest: dict, proposed_id: str) -> dict:
    for entry in manifest["entries"]:
        if entry["proposed_future_source_identity"] == proposed_id:
            return entry
    raise AssertionError(f"no manifest entry for {proposed_id!r}")


def _hash_seven_stores(directory) -> dict:
    out = {}
    for name in SEVEN_STORES:
        with open(os.path.join(str(directory), name), "rb") as handle:
            out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# MI-01
# ---------------------------------------------------------------------------

def test_mi_01_cross_sport_legacy_500j_collision_appears_in_the_manifest():
    """MI-01: 跨运动共用 500j 前缀的碰撞必须出现在影响清单中。"""
    manifest = _manifest()

    collisions = manifest["legacy_cross_sport_collisions"]
    assert len(collisions) == 1
    assert collisions[0]["category"] == "CROSS_SPORT_SHARED_NAMESPACE"
    assert collisions[0]["subject"] == LEGACY_SHARED
    assert collisions[0]["severity"] == "AMBIGUOUS"

    # 同一字符串被两个不同 (sport, date) 作用域使用。
    assert collisions[0]["evidence"] == {
        "basketball": "2030-05-01",
        "football": "2030-05-01",
    }
    assert len(collisions[0]["observation_ids"]) == 2

    # 冲突也出现在通用冲突报告里。
    assert any(
        c["category"] == "CROSS_SPORT_SHARED_NAMESPACE"
        for c in manifest["source_identity_conflicts"]
    )
    # 旧字符串被归类为共用命名空间。
    for entry in manifest["entries"]:
        assert entry["legacy_source_id_kind"] == MarketSourceIdKind.LEGACY_SHARED.value
        assert entry["legacy_compatibility_classification"] == (
            "LEGACY_SHARED_NAMESPACE_PRESERVED"
        )


# ---------------------------------------------------------------------------
# MI-02
# ---------------------------------------------------------------------------

def test_mi_02_sport_scoped_proposed_identities_remain_distinct():
    """MI-02: 运动作用域提议身份保持区分，且可解析往返。"""
    football = compose_sport_scoped_market_source_id(
        sport="football", event_date="2030-05-01", number="001"
    )
    basketball = compose_sport_scoped_market_source_id(
        sport="basketball", event_date="2030-05-01", number="001"
    )

    assert football == SPORT_SCOPED_FOOTBALL
    assert basketball == SPORT_SCOPED_BASKETBALL
    assert football != basketball

    assert parse_sport_scoped_market_source_id(football) == {
        "sport": "football",
        "event_date": "2030-05-01",
        "number": "001",
    }
    assert parse_sport_scoped_market_source_id(basketball)["sport"] == "basketball"
    assert parse_sport_scoped_market_source_id(LEGACY_SHARED) is None

    assert classify_market_source_id(football) == MarketSourceIdKind.SPORT_SCOPED.value
    assert classify_market_source_id(basketball) == MarketSourceIdKind.SPORT_SCOPED.value
    assert classify_market_source_id(LEGACY_SHARED) == MarketSourceIdKind.LEGACY_SHARED.value

    manifest = _manifest()
    for entry in manifest["entries"]:
        assert entry["proposed_source_id_kind"] == MarketSourceIdKind.SPORT_SCOPED.value
        assert entry["proposed_future_source_identity"] != LEGACY_SHARED

    # 未支持的运动 / 缺编号一律 fail closed，绝不猜测。
    with pytest.raises(ValueError):
        compose_sport_scoped_market_source_id(
            sport="tennis", event_date="2030-05-01", number="001"
        )
    with pytest.raises(ValueError):
        compose_sport_scoped_market_source_id(
            sport="football", event_date="", number="001"
        )
    with pytest.raises(ValueError):
        compose_sport_scoped_market_source_id(
            sport="football", event_date="2030-05-01", number=""
        )


# ---------------------------------------------------------------------------
# MI-03
# ---------------------------------------------------------------------------

def test_mi_03_canonical_ids_remain_unchanged():
    """MI-03: 清单不提议、不执行任何 Canonical ID 改写。"""
    canonical = _canonical_fixture()
    canonical_before = copy.deepcopy(canonical)
    stores = _stores()
    stores_before = copy.deepcopy(stores)

    manifest = _manifest(canonical_fixture=canonical, dependent_stores=stores)

    assert manifest["canonical_ids"] == [LEGACY_SHARED]
    assert manifest["canonical_id_count"] == 1
    for entry in manifest["entries"]:
        assert entry["existing_canonical_id"] == LEGACY_SHARED
        assert entry["estimated_rewrite_scope"]["canonical_id_rewrites"] == 0
        assert CLASSIFICATION_UNCHANGED_CANONICAL_ID in entry["classifications"]
        # 清单中不存在任何「新 Canonical ID」字段。
        assert "proposed_canonical_id" not in entry
        assert "new_canonical_id" not in entry

    # 输入夹具绝不被修改。
    assert canonical == canonical_before
    assert stores == stores_before


# ---------------------------------------------------------------------------
# MI-04
# ---------------------------------------------------------------------------

def test_mi_04_snapshot_settlement_evaluation_references_are_enumerated():
    """MI-04: 快照 / 结算 / 评估引用被正确枚举。"""
    manifest = _manifest()
    inventory = manifest["store_reference_inventory"]

    assert tuple(inventory) == STORE_ORDER
    assert inventory["daily_matches.json"]["references"] == {
        LEGACY_SHARED: ["/matches/0/id"]
    }
    assert inventory["team_strength.json"]["reference_kind"] == (
        "NO_MATCH_ID_REFERENCE"
    )
    assert inventory["team_strength.json"]["references"] == {}
    assert inventory["calibrated.json"]["references"][LEGACY_SHARED] == ["/ids/0"]
    assert inventory["prediction_snapshots.json"]["references"][LEGACY_SHARED] == [
        "/snapshots/s1/match_id"
    ]
    assert inventory["odds_snapshots.json"]["references"][LEGACY_SHARED] == [
        "/snapshots/o1/match_id"
    ]
    assert inventory["settlements.json"]["references"][LEGACY_SHARED] == [
        "/settlements/t1/match_id"
    ]
    assert inventory["evaluation_rows.json"]["references"][LEGACY_SHARED] == [
        "/rows/e1/match_id"
    ]

    entry = _entry(manifest, SPORT_SCOPED_FOOTBALL)
    affected = entry["affected_evidence_references"]
    assert set(affected) == {
        "daily_matches.json",
        "calibrated.json",
        "prediction_snapshots.json",
        "odds_snapshots.json",
        "settlements.json",
        "evaluation_rows.json",
    }
    assert entry["estimated_rewrite_scope"]["historical_reference_updates"] == 6
    assert CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT in entry["classifications"]

    # 链一致性：结算 / 评估与快照一致，因此没有不一致项。
    assert manifest["chain_inconsistencies"] == []
    # 由 match_id 派生的标识符被显式警示。
    derived = {w["derived_field"] for w in manifest["derived_identity_warnings"]}
    assert derived == {"snapshot_id", "evaluation_id"}


# ---------------------------------------------------------------------------
# MI-05
# ---------------------------------------------------------------------------

def test_mi_05_missing_historical_references_are_reported():
    """MI-05: 指向不存在 Canonical ID 的引用必须被报告。"""
    manifest = _manifest()
    unresolved = manifest["missing_references"]["unresolved_store_references"]
    assert unresolved == ["ghost-1"]
    assert manifest["missing_references"]["absent_stores"] == []

    # 七个存储齐备时没有缺失存储。
    assert all(
        manifest["store_reference_inventory"][name]["present"] for name in STORE_ORDER
    )

    # 未提供某个存储时，缺失被显式报告（影响无法评估）。
    partial = _manifest(dependent_stores={"daily_matches.json": _stores()["daily_matches.json"]})
    assert "calibrated.json" in partial["missing_references"]["absent_stores"]
    assert partial["store_reference_inventory"]["calibrated.json"]["present"] is False
    assert _entry(partial, SPORT_SCOPED_FOOTBALL)["missing_references"]
    assert partial["missing_references"]["unresolved_store_references"] == []


# ---------------------------------------------------------------------------
# MI-06
# ---------------------------------------------------------------------------

def test_mi_06_one_legacy_alias_cannot_silently_bind_to_multiple_canonical_ids():
    """MI-06: 一个旧别名不得静默绑定到多个不兼容的 Canonical ID。"""
    manifest = _manifest()
    conflicting = manifest["conflicting_mappings"]

    # 一个旧别名被提议到多个未来目标 -> 显式歧义，绝不自动选择。
    assert conflicting["legacy_alias_with_multiple_targets"] == [LEGACY_SHARED]
    for entry in manifest["entries"]:
        assert CLASSIFICATION_AMBIGUOUS_MAPPING in entry["classifications"]

    # 治理层同样拒绝「一个源身份 -> 多个 Canonical 所有者」的静默选择。
    observation = _observations()[0]
    key = ScopedSourceIdentityKey.for_observation(observation)
    proposals = [
        proposal_from_observations(
            [observation],
            requested_action=ProposalAction.BIND_SOURCE_TO_EXISTING_CANONICAL.value,
            candidate_canonical_id=canonical_id,
            existing_canonical_owner=canonical_id,
            ingestion_plan_reference="planner:ACCEPTED",
            expected_baseline_fingerprint="baseline-A",
            reason=f"claim {canonical_id}",
            identity_evidence_type="strong_competition_number",
        )
        for canonical_id in ("canon-A", "canon-B")
    ]
    conflicts = detect_owner_conflicts(proposals)
    assert len(conflicts) == 1
    assert conflicts[0]["source_key"] == key.source_key()
    assert conflicts[0]["competing_canonical_ids"] == "canon-A,canon-B"
    assert "latest-claim" in conflicts[0]["detail"]


# ---------------------------------------------------------------------------
# MI-07
# ---------------------------------------------------------------------------

def test_mi_07_unstable_500w_identity_is_blocked_from_automatic_migration():
    """MI-07: 不稳定的 500w 行位置身份被阻止自动迁移。"""
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
    key = ScopedSourceIdentityKey.for_observation(observation)
    assert key.identity_kind == IdentityKeyKind.INSUFFICIENT.value
    assert key.is_sufficient is False

    manifest = _manifest(
        canonical_fixture=_canonical_fixture() + [dict(finished)],
        observations=[observation],
        proposed_mappings=[
            ProposedSourceMapping(
                legacy_source_id="500w-2030-05-01-0",
                proposed_source_id="500w-2030-05-01-0",
                existing_canonical_id="500w-2030-05-01-0",
                identity_kind=IdentityKeyKind.INSUFFICIENT.value,
                identity_value="500w-2030-05-01-0",
                mapping_proposal_reference="prop-unstable",
            )
        ],
        dependent_stores={},
    )

    entry = _entry(manifest, "500w-2030-05-01-0")
    assert CLASSIFICATION_UNSUPPORTED_EVIDENCE in entry["classifications"]
    assert CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION in entry["classifications"]
    assert entry["estimated_rewrite_scope"]["canonical_id_rewrites"] == 0
    # 行位置身份绝不构成可自动迁移的稳定身份。
    assert classify_market_source_id("500w-2030-05-01-0") == (
        MarketSourceIdKind.UNKNOWN.value
    )


# ---------------------------------------------------------------------------
# MI-08
# ---------------------------------------------------------------------------

def test_mi_08_manifest_is_deterministic_across_input_permutations():
    """MI-08: 清单在输入排列下确定。"""
    canonical = _canonical_fixture()
    observations = _observations()
    mappings = _mappings()

    baseline = _manifest(
        canonical_fixture=canonical,
        observations=observations,
        proposed_mappings=mappings,
    )
    baseline_fingerprint = manifest_fingerprint(baseline)

    permutations = 0
    for canonical_order in itertools.permutations(range(len(canonical))):
        for observation_order in itertools.permutations(range(len(observations))):
            for mapping_order in itertools.permutations(range(len(mappings))):
                variant = _manifest(
                    canonical_fixture=[canonical[i] for i in canonical_order],
                    observations=[observations[i] for i in observation_order],
                    proposed_mappings=[mappings[i] for i in mapping_order],
                )
                assert manifest_fingerprint(variant) == baseline_fingerprint
                permutations += 1
    assert permutations >= 4

    # 同一清单重复构建 -> 同一指纹与同一规范 JSON。
    assert manifest_fingerprint(_manifest()) == baseline_fingerprint


# ---------------------------------------------------------------------------
# MI-09
# ---------------------------------------------------------------------------

def test_mi_09_manifest_binds_the_fixture_baseline_hash():
    """MI-09: 清单绑定夹具基线哈希。"""
    canonical = _canonical_fixture()
    manifest = _manifest(canonical_fixture=canonical)

    assert manifest["baseline_fingerprint"] == fixture_baseline_fingerprint(canonical)
    assert manifest["fixture_identity"] == "synthetic-migration-fixture"
    assert manifest["dataset_identity"] == DATASET
    assert manifest["identity_key_schema_version"] == "3.0.0"
    assert manifest["manifest_schema_version"] == MIGRATION_MANIFEST_SCHEMA_VERSION
    assert manifest["market_source_id_contract_version"] == (
        MARKET_SOURCE_ID_CONTRACT_VERSION
    )
    assert manifest["preferred_market_source_id_option"] == (
        PREFERRED_MARKET_SOURCE_ID_OPTION_ID
    )
    assert preferred_market_source_id_option().selected is True

    # 基线指纹与顺序无关，但随内容变化。
    reordered = [dict(canonical[0])]
    assert fixture_baseline_fingerprint(reordered) == manifest["baseline_fingerprint"]
    changed = [dict(canonical[0], market_tracked=False)]
    assert fixture_baseline_fingerprint(changed) != manifest["baseline_fingerprint"]

    # 观测指纹同样被绑定。
    assert len(manifest["source_observation_fingerprints"]) == len(_observations())
    assert all(
        len(fp) == 64 for fp in manifest["source_observation_fingerprints"]
    )


# ---------------------------------------------------------------------------
# MI-10
# ---------------------------------------------------------------------------

def test_mi_10_manifest_distinguishes_alias_only_changes_from_canonical_migration():
    """MI-10: 清单区分「仅源别名变更」与「Canonical ID 迁移」。"""
    # 一条被依赖存储引用的别名，和一条完全未被引用的别名。
    referenced = ProposedSourceMapping(
        legacy_source_id=LEGACY_SHARED,
        proposed_source_id=SPORT_SCOPED_FOOTBALL,
        existing_canonical_id=LEGACY_SHARED,
        identity_kind=IdentityKeyKind.COMPETITION_NUMBER.value,
        identity_value="football|2030-05-01|jczq|1",
    )
    unreferenced = ProposedSourceMapping(
        legacy_source_id="500l-2030-05-01-777",
        proposed_source_id="500l-2030-05-01-777",
        existing_canonical_id=None,
        identity_kind=IdentityKeyKind.INSUFFICIENT.value,
        identity_value="500l-2030-05-01-777",
    )
    manifest = _manifest(proposed_mappings=[referenced, unreferenced])

    with_references = _entry(manifest, SPORT_SCOPED_FOOTBALL)
    without_references = _entry(manifest, "500l-2030-05-01-777")

    assert CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT in with_references["classifications"]
    assert CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY not in with_references["classifications"]
    assert CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY in without_references["classifications"]
    assert CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT not in without_references["classifications"]

    # 两者都保持 Canonical ID 不变，并且都不含任何 Canonical 迁移分类。
    for entry in manifest["entries"]:
        assert CLASSIFICATION_UNCHANGED_CANONICAL_ID in entry["classifications"]
        assert entry["estimated_rewrite_scope"]["canonical_id_rewrites"] == 0
        for classification in entry["classifications"]:
            assert "MIGRATION" not in classification.replace(
                CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION, ""
            )

    assert ALL_IMPACT_CLASSIFICATIONS == (
        CLASSIFICATION_UNCHANGED_CANONICAL_ID,
        CLASSIFICATION_SOURCE_ALIAS_CHANGE_ONLY,
        CLASSIFICATION_HISTORICAL_REFERENCE_IMPACT,
        CLASSIFICATION_AMBIGUOUS_MAPPING,
        CLASSIFICATION_UNSUPPORTED_EVIDENCE,
        CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION,
    )
    histogram = manifest["classification_histogram"]
    assert set(histogram) == set(ALL_IMPACT_CLASSIFICATIONS)
    assert histogram[CLASSIFICATION_BLOCKED_BY_MISSING_AUTHORIZATION] == len(
        manifest["entries"]
    )


# ---------------------------------------------------------------------------
# MI-11
# ---------------------------------------------------------------------------

def test_mi_11_manifest_contains_no_executable_apply_path():
    """MI-11: 清单不含任何可执行的 apply 路径。"""
    manifest = _manifest()

    assert manifest["kind"] == MANIFEST_KIND
    assert manifest["executable"] is False
    assert manifest["execution_api"] is None
    assert MIGRATION_EXECUTION_AVAILABLE is False
    assert set(manifest["forbidden_operations"]) == set(FORBIDDEN_MANIFEST_KEYS)
    assert "no migration operation" in manifest["non_executable_marker"]

    lowered = {key.lower() for key in manifest}
    assert lowered.isdisjoint({k.lower() for k in FORBIDDEN_MANIFEST_KEYS})
    for value in manifest.values():
        assert not callable(value)

    # 断言函数本身 fail closed。
    assert_not_executable(manifest)

    tampered = dict(manifest, executable=True)
    with pytest.raises(ValueError):
        assert_not_executable(tampered)

    with pytest.raises(ValueError):
        assert_not_executable(dict(manifest, execution_api="apply_migration"))

    with pytest.raises(ValueError):
        assert_not_executable(dict(manifest, kind="MIGRATION_PLAN"))

    with pytest.raises(ValueError):
        assert_not_executable(dict(manifest, apply=lambda: None))

    with pytest.raises(ValueError):
        assert_not_executable(dict(manifest, operations=[]))


# ---------------------------------------------------------------------------
# MI-12
# ---------------------------------------------------------------------------

def test_mi_12_a_changed_fixture_invalidates_stale_manifest_assumptions():
    """MI-12: 夹具变化使陈旧清单假设失效。"""
    original = _manifest()

    changed_canonical = [dict(_market_row("500j-2030-05-01-999"), market_tracked=True)]
    changed = _manifest(canonical_fixture=changed_canonical)

    assert changed["baseline_fingerprint"] != original["baseline_fingerprint"]
    assert changed["canonical_ids"] == ["500j-2030-05-01-999"]

    # 旧别名在变化后的夹具中已不是 Canonical 所有者 -> 显式标记。
    for entry in changed["entries"]:
        assert "EXISTING_CANONICAL_ID_NOT_IN_FIXTURE" in entry["flags"]
        assert entry["existing_canonical_id"] == LEGACY_SHARED
        assert entry["estimated_rewrite_scope"]["canonical_id_rewrites"] == 0

    # 夹具内容一致时基线一致（确定性）。
    assert _manifest()["baseline_fingerprint"] == original["baseline_fingerprint"]
    assert manifest_fingerprint(_manifest()) == manifest_fingerprint(original)


# ---------------------------------------------------------------------------
# MI-13
# ---------------------------------------------------------------------------

def test_mi_13_shadow_validation_remains_isolated(tmp_path):
    """MI-13: 影子迁移分析保持隔离（无活动绑定、无生产权威、无文件写入）。"""
    workspace = tmp_path / "shadow_migration"
    workspace.mkdir()
    before = sorted(os.listdir(workspace))

    report = shadow_migration_analysis(
        workspace=str(workspace),
        canonical_fixture=_canonical_fixture(),
        observations=_observations(),
        proposed_mappings=_mappings(),
        dependent_stores=_stores(),
        dataset_identity=DATASET,
    )

    assert report["analysis_kind"] == "SHADOW_MIGRATION_IMPACT_ANALYSIS"
    assert report["is_production_authority"] is False
    assert report["migration_execution_available"] is False
    assert report["active_bindings_created"] == 0
    assert report["canonical_ownership_changed"] is False
    assert report["identifiers_rewritten"] == 0
    assert report["files_written"] == 0
    assert report["production_mapping_write_authorization"] == (
        PRODUCTION_MAPPING_WRITE_AUTHORIZATION
    )
    assert report["runtime_ledger_enforcement"] == RUNTIME_LEDGER_ENFORCEMENT

    manifest = report["migration_impact_manifest"]
    assert manifest["executable"] is False
    assert report["migration_manifest_fingerprint"] == manifest_fingerprint(manifest)

    # 工作区没有任何新增文件（含账本）。
    assert sorted(os.listdir(workspace)) == before
    assert not (workspace / "mapping_governance_ledger.jsonl").exists()


# ---------------------------------------------------------------------------
# MI-14
# ---------------------------------------------------------------------------

def test_mi_14_all_seven_fixture_stores_remain_byte_identical(tmp_path):
    """MI-14: 分析之后七个夹具存储逐字节不变。"""
    data_dir = tmp_path / "seven_stores"
    data_dir.mkdir()
    stores = _stores()
    for name in SEVEN_STORES:
        with open(str(data_dir / name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(stores[name], ensure_ascii=False, sort_keys=True, indent=2))
            handle.write("\n")

    before = _hash_seven_stores(data_dir)
    before_listing = sorted(os.listdir(data_dir))

    manifest = _manifest(dependent_stores=stores)
    shadow_migration_analysis(
        workspace=str(tmp_path / "shadow"),
        canonical_fixture=_canonical_fixture(),
        observations=_observations(),
        proposed_mappings=_mappings(),
        dependent_stores=stores,
        dataset_identity=DATASET,
    )

    assert _hash_seven_stores(data_dir) == before
    assert sorted(os.listdir(data_dir)) == before_listing
    # 清单只做分析，未产生任何「重写后的历史数据集」。
    assert all(
        entry["estimated_rewrite_scope"]["canonical_id_rewrites"] == 0
        for entry in manifest["entries"]
    )
    assert "rewritten_dataset" not in manifest


# ---------------------------------------------------------------------------
# MI-15 / MI-16
# ---------------------------------------------------------------------------

def test_mi_15_production_mapping_authorization_remains_not_granted():
    """MI-15: 生产映射写入授权保持 NOT_GRANTED。"""
    manifest = _manifest()
    status = manifest["authorization_status"]

    assert status["production_mapping_write_authorization"] == "NOT_GRANTED"
    assert status["manifest_is_owner_approval"] is False
    assert status["manifest_is_production_authorization"] is False
    assert status["historical_canonical_migration"] == "NOT_AUTHORIZED"

    governance = governance_status()
    assert governance["production_mapping_write_authorization"] == "NOT_GRANTED"
    assert governance["ledger_is_production_authority"] is False
    assert governance["identity_key_schema_version"] == "3.0.0"


def test_mi_16_runtime_ledger_enforcement_remains_not_enabled(tmp_path):
    """MI-16: 运行期账本强制保持 NOT_ENABLED。"""
    manifest = _manifest()
    assert manifest["authorization_status"]["runtime_ledger_enforcement"] == "NOT_ENABLED"

    report = shadow_migration_analysis(
        workspace=str(tmp_path / "shadow_enforcement"),
        canonical_fixture=_canonical_fixture(),
        observations=_observations(),
        proposed_mappings=_mappings(),
        dependent_stores=_stores(),
        dataset_identity=DATASET,
    )
    assert report["runtime_ledger_enforcement"] == "NOT_ENABLED"
    assert governance_status()["runtime_ledger_enforcement"] == "NOT_ENABLED"

    # 运行期身份权威没有改变，且本模块不被刷新路径引用。
    assert governance_status()["runtime_identity_authority"] == (
        "utils.ingestion_identity.validate_ingestion_identity"
    )
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in ("scraper.py",):
        source = open(os.path.join(root, "utils", module), encoding="utf-8").read()
        assert "source_id_migration" not in source
        assert "provider_evidence" not in source


# ---------------------------------------------------------------------------
# 补充不变量
# ---------------------------------------------------------------------------

def test_migration_contract_declares_three_evaluated_options():
    """补充：契约必须显式评估三个候选并只选一个。"""
    from utils.source_id_migration import (
        MARKET_SOURCE_ID_DESIGN_CONSIDERATIONS,
        MARKET_SOURCE_ID_OPTIONS,
    )

    option_ids = {option.option_id for option in MARKET_SOURCE_ID_OPTIONS}
    assert option_ids == {"A", "B", "C"}
    selected = [option for option in MARKET_SOURCE_ID_OPTIONS if option.selected]
    assert len(selected) == 1
    assert selected[0].option_id == PREFERRED_MARKET_SOURCE_ID_OPTION_ID
    for option in MARKET_SOURCE_ID_OPTIONS:
        for field in (
            "format_template",
            "example_football",
            "example_basketball",
            "sport_disambiguation",
            "legacy_collision_risk",
            "assessment",
        ):
            assert getattr(option, field), (option.option_id, field)

    assert len(MARKET_SOURCE_ID_DESIGN_CONSIDERATIONS) == 10
    assert len({name for name, _ in MARKET_SOURCE_ID_DESIGN_CONSIDERATIONS}) == 10


def test_mapping_proposal_objects_are_never_consumed_by_the_manifest():
    """补充：清单只接受提议映射，绝不接受或产生已批准决策。"""
    manifest = _manifest()
    # 清单里没有任何账本条目、批准件或活动绑定字段。
    for forbidden in ("ledger", "approval_artifact", "active_bindings", "approval"):
        assert forbidden not in manifest
    assert MappingProposal is not None  # 治理层类型未被清单消费
