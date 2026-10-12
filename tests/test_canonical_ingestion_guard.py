"""
Canonical 摄取身份守卫测试套件
==============================
覆盖 Phase 2-A 的摄取安全与重复防护契约：

- 源别名冲突（同 ID 不同事件 / 批次内别名复用）
- 多 Canonical 所有者歧义
- 强竞彩编号与精确主客队回退语义保持不变
- 批次顺序无关（确定性）与合法重放幂等
- 失败关闭：冲突批次对七个持久化 JSON 库零写入
- 市场准入、终态保护、模型数学保持既有契约

全部使用隔离的合成夹具（tmp_path + monkeypatch 源抓取器），
绝不访问网络。
"""
from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

import config
from utils import fetcher_500, scraper
from utils.daily_loader import load_json, save_json
from utils.evaluation_rows import build_evaluation_row, get_all_evaluation_rows
from utils.ingestion_identity import (
    CanonicalIdentityCollisionError,
    validate_canonical_uniqueness,
    validate_ingestion_identity,
)
from utils.match_identity import same_event
from utils.odds_snapshots import build_snapshot as build_odds_snapshot
from utils.prediction_snapshots import build_snapshot as build_pred_snapshot
from utils.settlements import build_settlement, extract_final_score

BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

FOOTBALL_1X2 = {"home_win": 1.90, "draw": 3.40, "away_win": 3.80}
BASKETBALL_ML = {"home_win": 1.80, "away_win": 2.00}

# 七个持久化 JSON 库（daily_matches.json 为 Canonical 集合，其余六个为不可变历史证据）
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

def _match(
    match_id=None,
    *,
    sport="football",
    league="西甲",
    home="皇马",
    away="巴萨",
    date="2030-05-01",
    time="20:00",
    status="upcoming",
    score=None,
    odds=None,
    jczq_no=None,
    kickoff_time_known=True,
    **extra,
):
    match = {
        "id": match_id,
        "sport": sport,
        "league": league,
        "date": date,
        "time": time,
        "status": status,
        "home": home,
        "away": away,
        "score": score,
        "odds": dict(odds) if isinstance(odds, dict) else odds,
        "jczq_no": jczq_no,
        "kickoff_time_known": kickoff_time_known,
    }
    match.update(extra)
    return match


def _fake_sources(
    monkeypatch,
    *,
    live_football=(),
    live_basketball=(),
    market=(),
    finished=(),
):
    """把全部网络数据源替换为受控假数据（返回深拷贝，避免测试间串扰）。"""
    monkeypatch.setattr(
        fetcher_500, "fetch_live_matches", lambda sport: copy.deepcopy(list(live_football))
    )
    monkeypatch.setattr(
        fetcher_500, "fetch_live_basketball", lambda: copy.deepcopy(list(live_basketball))
    )
    monkeypatch.setattr(
        fetcher_500,
        "fetch_jczq_xml",
        lambda sport: [
            copy.deepcopy(m) for m in market if m.get("sport", "football") == sport
        ],
    )
    monkeypatch.setattr(
        fetcher_500, "fetch_finished_matches", lambda *a, **k: copy.deepcopy(list(finished))
    )


def _store_path(data_dir, name):
    return os.path.join(str(data_dir), name)


def _sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _hash_all(data_dir):
    return {name: _sha256(_store_path(data_dir, name)) for name in SEVEN_STORES}


def _seed_seven_stores(data_dir):
    """
    生成一套自洽的七库夹具：

    - daily_matches.json：一场已完赛且被跟踪的 Canonical 比赛
    - team_strength.json / calibrated.json：Elo 与校准状态
    - prediction_snapshots.json × odds_snapshots.json × settlements.json
      × evaluation_rows.json：一条完整的「快照 -> 结算 -> 评估样本」证据链
    """
    match = _match(
        "old-1",
        date="2030-05-01",
        time="12:00",
        status="finished",
        score={"ft": [1, 0]},
        odds=FOOTBALL_1X2,
        market_tracked=True,
        home_team={"elo": 1500},
        away_team={"elo": 1500},
    )

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
    save_json(
        "team_strength.json",
        {"teams": {"皇马": {"elo_rating": 1500}, "巴萨": {"elo_rating": 1500}}},
    )
    save_json("calibrated.json", {"ids": ["old-1"]})
    save_json(
        "prediction_snapshots.json",
        {"version": 1, "snapshots": {snapshot["snapshot_id"]: snapshot}},
    )
    save_json(
        "odds_snapshots.json",
        {"version": 1, "snapshots": {odds_snapshot["snapshot_id"]: odds_snapshot}},
    )
    save_json(
        "settlements.json",
        {"version": 1, "settlements": {settlement["settlement_id"]: settlement}},
    )
    save_json(
        "evaluation_rows.json",
        {"version": 1, "rows": {evaluation_row["evaluation_id"]: evaluation_row}},
    )
    return match


def _inject_same_id_different_event(monkeypatch):
    """注入一条与既有 `old-1` 同 ID 但不同事件的 live 记录。"""
    _fake_sources(
        monkeypatch,
        live_football=[
            _match("old-1", home="X队", away="Y队", status="live", score={"ft": [1, 0]})
        ],
    )


# ---------------------------------------------------------------------------
# IG-01 .. IG-07 — 冲突类别
# ---------------------------------------------------------------------------

def test_ig_01_same_source_id_different_events_rejected():
    """IG-01: 同一源 ID 指向不同事件 -> 拒绝（Case A）。"""
    existing = [_match("source-001", home="Alpha", away="Beta")]
    incoming = [_match("source-001", home="Gamma", away="Delta")]

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        validate_ingestion_identity(existing, incoming)
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"
    assert exc.value.match_id == "source-001"

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper._merge(existing, incoming)
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


def test_ig_02_different_source_ids_same_strong_identity_reconcile_without_id_change():
    """IG-02: 不同源 ID、同一强编号 -> 对账且不改变既有 Canonical ID（Case B）。"""
    existing = [_match("live-001", jczq_no="007", home="皇家马德里", away="巴塞罗那")]
    incoming = [
        _match(
            "market-001",
            jczq_no="7",
            home="皇马",
            away="巴萨",
            time="00:00",
            kickoff_time_known=False,
            odds=FOOTBALL_1X2,
        )
    ]

    merged, added, _updated = scraper._merge(existing, incoming)

    assert len(merged) == 1
    assert added == 0
    assert merged[0]["id"] == "live-001"
    assert merged[0]["jczq_no"] == "007"


def test_ig_03_conflicting_strong_numbers_never_fall_back_to_team_names():
    """IG-03: 强编号冲突时绝不退化为队名匹配（Case C）。"""
    a = _match("canon-007", jczq_no="007", home="皇马", away="巴萨")
    b = _match("market-008", jczq_no="008", home="皇马", away="巴萨")

    assert same_event(a, b) is False

    merged, added, _updated = scraper._merge([a], [b])

    assert len(merged) == 2
    assert added == 1
    by_id = {m["id"]: m for m in merged}
    assert by_id["canon-007"]["jczq_no"] == "007"
    assert by_id["market-008"]["jczq_no"] == "008"


def test_ig_04_exact_team_fallback_remains_supported():
    """IG-04: 无强编号时精确主客队回退仍然生效（Case D）。"""
    existing = [_match("canon-teams", home="曼联", away="利物浦", time="00:00",
                       kickoff_time_known=False)]
    incoming = [_match("live-teams", home="曼联", away="利物浦", time="21:30")]

    merged, added, _updated = scraper._merge(existing, incoming)

    assert len(merged) == 1
    assert added == 0
    assert merged[0]["id"] == "canon-teams"
    assert merged[0]["time"] == "21:30"
    assert merged[0]["kickoff_time_known"] is True


def test_ig_05_abbreviated_names_without_strong_identity_do_not_fuzzy_reconcile():
    """IG-05: 无强编号时缩写队名不得触发模糊对账。"""
    existing = [_match("canon-full", home="曼彻斯特联", away="利物浦")]
    incoming = [_match("live-abbr", home="曼联", away="利物浦")]

    assert same_event(existing[0], incoming[0]) is False

    merged, added, _updated = scraper._merge(existing, incoming)

    assert len(merged) == 2
    assert added == 1
    assert {m["id"] for m in merged} == {"canon-full", "live-abbr"}


def test_ig_06_reused_alias_within_one_batch_rejected():
    """IG-06: 批次内同一源别名被复用为不同事件 -> 拒绝（Case F）。"""
    incoming = [
        _match("alias-1", home="A", away="B"),
        _match("alias-1", home="C", away="D"),
    ]

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper._merge([], incoming)

    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"
    assert exc.value.match_id == "alias-1"
    assert exc.value.source_namespace == "alias"


def test_ig_07_multiple_canonical_owners_rejected():
    """IG-07: 一条 incoming 匹配多个 Canonical 所有者 -> 歧义拒绝（Case E）。"""
    existing = [
        _match("owner-a", home="X", away="Y"),
        _match("owner-b", home="X", away="Y", jczq_no="002"),
    ]
    incoming = [_match("incoming-1", home="X", away="Y", jczq_no="002")]

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper._merge(existing, incoming)

    assert exc.value.category == "AMBIGUOUS_CANONICAL_OWNERSHIP"
    assert set(exc.value.competing_canonical_ids) == {"owner-a", "owner-b"}
    assert exc.value.evidence_type == "multi_owner"


# ---------------------------------------------------------------------------
# IG-08 .. IG-11 — 失败关闭与持久化边界
# ---------------------------------------------------------------------------

def test_ig_08_existing_id_collision_rejected_before_network_fetch(isolated_data_dir, monkeypatch):
    """IG-08: 既有 Canonical ID 冲突在任何网络抓取之前即被拒绝。"""
    save_json(
        "daily_matches.json",
        {
            "meta": {},
            "matches": [
                _match("dup-1", home="A", away="B"),
                _match("dup-1", home="C", away="D"),
            ],
        },
    )

    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", spy)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", spy)
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", spy)
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", spy)

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper.refresh(verbose=False)

    assert exc.value.category == "PREEXISTING_DUPLICATE"
    assert calls == [], "冲突必须在任何源抓取之前被拒绝"


def test_ig_09_incoming_collision_rejected_before_downstream_writes(isolated_data_dir, monkeypatch):
    """IG-09: incoming 冲突在下游持久化写入之前被拒绝，六个下游步骤一次都没执行。"""
    _seed_seven_stores(isolated_data_dir)

    invoked = []

    def _spy(name):
        def _inner(*args, **kwargs):
            invoked.append(name)
            return 0

        return _inner

    for name in (
        "_capture_odds_history",
        "_calibrate_from_finished",
        "_capture_prediction_snapshots",
        "_settle_finished_matches",
        "_materialize_evaluation_rows",
    ):
        monkeypatch.setattr(scraper, name, _spy(name))

    _inject_same_id_different_event(monkeypatch)

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper.refresh(verbose=False)

    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"
    assert invoked == []


def test_ig_10_existing_snapshots_and_settlements_unchanged_after_conflict(
    isolated_data_dir, monkeypatch
):
    """IG-10: 冲突之后既有预测快照 / 赔率历史 / 结算 / 评估样本逐字节不变。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)

    _inject_same_id_different_event(monkeypatch)
    with pytest.raises(CanonicalIdentityCollisionError):
        scraper.refresh(verbose=False)

    after = _hash_all(isolated_data_dir)
    for name in (
        "prediction_snapshots.json",
        "odds_snapshots.json",
        "settlements.json",
        "evaluation_rows.json",
    ):
        assert after[name] == before[name], f"{name} 在冲突批次后被改动"


def test_ig_11_elo_and_calibration_stores_unchanged_after_conflict(
    isolated_data_dir, monkeypatch
):
    """IG-11: 冲突之后球队实力库与校准状态逐字节不变。"""
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)

    _inject_same_id_different_event(monkeypatch)
    with pytest.raises(CanonicalIdentityCollisionError):
        scraper.refresh(verbose=False)

    after = _hash_all(isolated_data_dir)
    assert after["team_strength.json"] == before["team_strength.json"]
    assert after["calibrated.json"] == before["calibrated.json"]


# ---------------------------------------------------------------------------
# IG-12 .. IG-14 — 身份维度隔离
# ---------------------------------------------------------------------------

def test_ig_12_same_competition_number_different_sport_does_not_establish_identity():
    """IG-12: 同编号、不同运动 -> 不构成事件身份。"""
    football = _match("fb-007", sport="football", jczq_no="007", home="A", away="B")
    basketball = _match("bb-007", sport="basketball", jczq_no="007", home="A", away="B")

    assert same_event(football, basketball) is False

    merged, added, _updated = scraper._merge([football], [basketball])
    assert len(merged) == 2
    assert added == 1


def test_ig_13_same_competition_number_different_date_does_not_establish_identity():
    """IG-13: 同编号、不同日期 -> 不构成事件身份。"""
    day1 = _match("d1-007", jczq_no="007", date="2030-05-01")
    day2 = _match("d2-007", jczq_no="007", date="2030-05-02")

    assert same_event(day1, day2) is False

    merged, added, _updated = scraper._merge([day1], [day2])
    assert len(merged) == 2
    assert added == 1


def test_ig_14_placeholder_kickoff_time_does_not_change_event_identity():
    """IG-14: 占位符开赛时间（00:00 / kickoff_time_known=False）不影响事件身份。"""
    live = _match("live-x", jczq_no="007", time="20:00", kickoff_time_known=True)
    market = _match("market-x", jczq_no="007", time="00:00", kickoff_time_known=False)

    assert same_event(live, market) is True

    merged, added, _updated = scraper._merge([live], [market])
    assert len(merged) == 1
    assert added == 0
    assert merged[0]["id"] == "live-x"
    # 权威开赛时间不得被占位符降级
    assert merged[0]["time"] == "20:00"
    assert merged[0]["kickoff_time_known"] is True


# ---------------------------------------------------------------------------
# IG-15 .. IG-19 — 市场准入、幂等与终态保护
# ---------------------------------------------------------------------------

def test_ig_15_legitimate_market_fallback_remains_valid(isolated_data_dir, monkeypatch):
    """IG-15: 合法的市场兜底候选仍可准入（有完整盘口 -> 新事件被跟踪）。"""
    market = _match(
        "500j-2030-05-01-007",
        jczq_no="007",
        time="00:00",
        kickoff_time_known=False,
        odds=FOOTBALL_1X2,
    )
    _fake_sources(monkeypatch, market=[market])

    result = scraper.refresh(verbose=False)

    assert result["added"] == 1
    assert result["sources"]["market_candidates"] == 1
    stored = load_json("daily_matches.json")["matches"]
    assert [m["id"] for m in stored] == ["500j-2030-05-01-007"]
    assert stored[0]["market_tracked"] is True


def test_ig_16_previously_tracked_event_survives_odds_loss(isolated_data_dir, monkeypatch):
    """IG-16: 已跟踪比赛在盘口临时缺失时继续存活并更新状态，保留最后已知盘口。"""
    _fake_sources(monkeypatch, live_football=[_match("m-1", odds=FOOTBALL_1X2, jczq_no="007")])
    assert scraper.refresh(verbose=False)["added"] == 1

    _fake_sources(
        monkeypatch,
        live_football=[_match("m-1", odds=None, status="live", jczq_no="007")],
    )
    result = scraper.refresh(verbose=False)

    assert result["added"] == 0
    stored = load_json("daily_matches.json")["matches"]
    assert len(stored) == 1
    assert stored[0]["id"] == "m-1"
    assert stored[0]["status"] == "live"
    assert stored[0]["odds"] == FOOTBALL_1X2


def test_ig_17_repeated_clean_refresh_remains_idempotent(isolated_data_dir, monkeypatch):
    """IG-17: 重复的干净刷新保持幂等（Case G）。"""
    _fake_sources(monkeypatch, live_football=[_match("m-1", odds=FOOTBALL_1X2, jczq_no="007")])

    first = scraper.refresh(verbose=False)
    assert first["added"] == 1

    second = scraper.refresh(verbose=False)
    assert second["added"] == 0
    assert second["updated"] == 0
    assert second["total"] == 1

    third = scraper.refresh(verbose=False)
    assert third["added"] == 0
    assert third["updated"] == 0


def test_ig_18_valid_final_score_propagation_preserved(isolated_data_dir, monkeypatch):
    """IG-18: 合法终场比分的下游传播（Elo / 结算 / 评估样本）保持不变。"""
    _fake_sources(monkeypatch, live_football=[_match("m-fin", odds=FOOTBALL_1X2, jczq_no="011")])
    assert scraper.refresh(verbose=False)["prediction_snapshots_added"] == 1

    _fake_sources(
        monkeypatch,
        live_football=[
            _match(
                "m-fin",
                odds=None,
                status="finished",
                score={"ft": [2, 1]},
                jczq_no="011",
            )
        ],
    )
    result = scraper.refresh(verbose=False)

    assert result["calibrated"] == 1
    assert result["settlements_added"] == 1
    assert result["evaluation_rows_added"] == 1
    assert len(get_all_evaluation_rows()) == 1
    assert "m-fin" in (load_json("calibrated.json") or {}).get("ids", [])


def test_ig_19_conflicting_final_results_remain_protected(isolated_data_dir, monkeypatch):
    """IG-19: 冲突终场比分不得覆盖既有权威比分，也不产生新的结算。"""
    _fake_sources(monkeypatch, live_football=[_match("m-cf", odds=FOOTBALL_1X2, jczq_no="012")])
    scraper.refresh(verbose=False)

    _fake_sources(
        monkeypatch,
        live_football=[
            _match("m-cf", odds=None, status="finished", score={"ft": [2, 1]}, jczq_no="012")
        ],
    )
    assert scraper.refresh(verbose=False)["settlements_added"] == 1

    _fake_sources(
        monkeypatch,
        live_football=[
            _match("m-cf", odds=None, status="finished", score={"ft": [2, 2]}, jczq_no="012")
        ],
    )
    result = scraper.refresh(verbose=False)

    assert result["updated"] == 0
    assert result["settlements_added"] == 0
    assert result["evaluation_rows_added"] == 0
    stored = load_json("daily_matches.json")["matches"]
    assert stored[0]["score"] == {"ft": [2, 1]}
    assert len(get_all_evaluation_rows()) == 1


# ---------------------------------------------------------------------------
# IG-20 .. IG-22 — 确定性
# ---------------------------------------------------------------------------

def test_ig_20_incoming_permutation_does_not_alter_unambiguous_ownership():
    """IG-20: 明确归属的 incoming 批次，任意排列都产生同一 Canonical 归属。"""
    existing = [_match("canon-a", jczq_no="001", home="A", away="B")]
    members = [
        _match("live-a", jczq_no="001", home="皇家马德里", away="巴塞罗那"),
        _match(
            "market-a",
            jczq_no="1",
            home="皇马",
            away="巴萨",
            time="00:00",
            kickoff_time_known=False,
        ),
        _match("finished-a", jczq_no="001", home="皇马", away="巴萨",
               status="finished", score={"ft": [2, 1]}),
    ]

    permutations = [
        members,
        list(reversed(members)),
        [members[1], members[2], members[0]],
        [members[2], members[0], members[1]],
    ]

    outcomes = []
    for perm in permutations:
        merged, added, _updated = scraper._merge(
            copy.deepcopy(existing), copy.deepcopy(perm)
        )
        outcomes.append((sorted(m["id"] for m in merged), added))

    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert outcomes[0] == (["canon-a"], 0)


def test_ig_21_batch_local_alias_reuse_is_row_order_independent():
    """IG-21: 批次内别名复用，两种行序必须得到同一冲突类别。"""
    existing = [_match("canon-1", home="A", away="B")]
    first = _match("alias-1", home="A", away="B")
    second = _match("alias-1", home="C", away="D")

    for order in ([first, second], [second, first]):
        with pytest.raises(CanonicalIdentityCollisionError) as exc:
            scraper._merge(copy.deepcopy(existing), copy.deepcopy(order))
        assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"


def test_ig_21b_non_transitive_identity_is_row_order_independent():
    """
    IG-21b（审计发现的归属转换）：强编号优先与队名回退**不可传递**。

    - ``x``（无强编号，队名 A/B）回退命中既有 Canonical ``canon-001``
    - ``y``（强编号 002，队名 A/B）与 ``canon-001`` 强编号冲突，属另一事件
    - 但 ``x`` 与 ``y`` 之间按队名回退又判等

    因此 ``x`` 同时被两个不同事件claim -> 本质歧义，必须在**任意行序**下都失败关闭。
    """
    existing = [_match("canon-001", jczq_no="001", home="A", away="B")]
    x = _match("live-x", home="A", away="B")
    y = _match("live-y", jczq_no="002", home="A", away="B")

    assert same_event(x, y) is True

    for order in ([x, y], [y, x]):
        with pytest.raises(CanonicalIdentityCollisionError) as exc:
            scraper._merge(copy.deepcopy(existing), copy.deepcopy(order))
        assert exc.value.category == "AMBIGUOUS_CANONICAL_OWNERSHIP"
        assert exc.value.evidence_type == "non_transitive_identity"


def test_ig_22_unstable_row_position_identity_is_never_sole_ownership_proof():
    """
    IG-22: ``500w-{date}-{row_index}`` 由行位置派生、跨刷新不稳定，
    绝不能单独作为「改写无关 Canonical 记录」的所有权证明。
    """
    existing = [_match("500w-2030-05-01-0", home="X队", away="Y队")]

    # 同一行位置 ID，但事件不同 -> 拒绝，不得静默覆盖
    incompatible = [_match("500w-2030-05-01-0", home="P队", away="Q队")]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper._merge(copy.deepcopy(existing), incompatible)
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    # 同一行位置 ID 且确为同一事件 -> 仍按既有契约对账，Canonical ID 不变
    compatible = [
        _match("500w-2030-05-01-0", home="X队", away="Y队",
               status="finished", score={"ft": [1, 0]})
    ]
    merged, added, _updated = scraper._merge(copy.deepcopy(existing), compatible)
    assert len(merged) == 1
    assert added == 0
    assert merged[0]["id"] == "500w-2030-05-01-0"


# ---------------------------------------------------------------------------
# IG-23 .. IG-24 — 失败关闭与源中断
# ---------------------------------------------------------------------------

def test_ig_23_missing_or_malformed_identity_fails_closed():
    """IG-23: 身份缺失或畸形 -> 失败关闭（保留既有类别）。"""
    cases = [
        ([None], "MALFORMED_INCOMING_RECORD"),
        (["not-a-dict"], "MALFORMED_INCOMING_RECORD"),
        ([{"id": 123, "sport": "football"}], "MALFORMED_INCOMING_ID"),
        ([{"id": "   ", "sport": "football"}], "MALFORMED_INCOMING_ID"),
        ([{"sport": "football", "date": "2030-05-01"}], "INADEQUATE_EVENT_IDENTITY"),
    ]

    for incoming, expected in cases:
        with pytest.raises(CanonicalIdentityCollisionError) as exc:
            validate_ingestion_identity([], incoming)
        assert exc.value.category == expected, incoming

        with pytest.raises(CanonicalIdentityCollisionError) as exc:
            scraper._merge([], incoming)
        assert exc.value.category == expected, incoming


def test_ig_24_source_outage_retains_existing_handling(isolated_data_dir, monkeypatch):
    """IG-24: 普通源中断保留既有错误处理，绝不误判为身份冲突。"""
    def _boom(*args, **kwargs):
        raise ConnectionError("source unavailable")

    monkeypatch.setattr(fetcher_500, "fetch_live_matches", _boom)
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_jczq_xml", _boom)
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda *a, **k: [])

    result = scraper.refresh(verbose=False)

    assert result["sources"]["live_football"].startswith("error:")
    assert result["sources"]["jczq_odds_football"].startswith("error:")
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# IG-25 .. IG-27 — 七库不变性、唯一性与历史拒绝
# ---------------------------------------------------------------------------

def test_ig_25_invalid_batch_causes_zero_changes_across_seven_stores(
    isolated_data_dir, monkeypatch
):
    """
    IG-25: 冲突批次对七个持久化 JSON 库零写入，刷新锁被释放，
    且随后一次合法刷新可以正常进行。
    """
    _seed_seven_stores(isolated_data_dir)
    before = _hash_all(isolated_data_dir)

    _inject_same_id_different_event(monkeypatch)

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper.refresh(verbose=False)
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    after = _hash_all(isolated_data_dir)
    assert after == before, "冲突批次必须让七个库逐字节不变"

    # 锁已释放 + 合法刷新可继续（成功即证明锁与状态机恢复正常）
    _fake_sources(
        monkeypatch,
        live_football=[
            _match("new-clean", home="新队", away="另一队",
                   date="2030-06-01", odds=FOOTBALL_1X2, jczq_no="021")
        ],
    )
    recovered = scraper.refresh(verbose=False)
    assert recovered["added"] == 1
    assert recovered["total"] == 2


def test_ig_26_successful_merge_retains_canonical_id_uniqueness(isolated_data_dir, monkeypatch):
    """IG-26: 成功合并后 Canonical ID 保持唯一且非空；唯一性证明可独立复用。"""
    _fake_sources(
        monkeypatch,
        live_football=[
            _match("m-1", home="A", away="B", odds=FOOTBALL_1X2),
            _match("m-2", home="C", away="D", odds=FOOTBALL_1X2),
        ],
        live_basketball=[
            _match("m-3", sport="basketball", league="NBA", home="E", away="F",
                   odds=BASKETBALL_ML)
        ],
    )
    scraper.refresh(verbose=False)

    matches = load_json("daily_matches.json")["matches"]
    ids = [m["id"] for m in matches]

    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert all(isinstance(i, str) and i.strip() for i in ids)

    # 附加防线可独立复用，并对重复 ID 失败关闭
    validate_canonical_uniqueness(matches, previous_ids=[], expected_total=3)
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        validate_canonical_uniqueness([{"id": "z"}, {"id": "z"}])
    assert exc.value.category == "CANONICAL_UNIQUENESS_VIOLATION"


def test_ig_27_historical_collision_remains_refused_without_auto_repair(
    isolated_data_dir, monkeypatch
):
    """IG-27: 既有历史冲突继续被拒绝，且不做任何自动修复。"""
    payload = {
        "meta": {"note": "historical fixture"},
        "matches": [
            _match("hist-dup", home="A", away="B"),
            _match("hist-dup", home="C", away="D"),
        ],
    }
    save_json("daily_matches.json", payload)
    path = _store_path(isolated_data_dir, "daily_matches.json")
    before = _sha256(path)

    _fake_sources(monkeypatch)

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        scraper.refresh(verbose=False)

    assert exc.value.category == "PREEXISTING_DUPLICATE"
    assert _sha256(path) == before
    assert load_json("daily_matches.json") == payload


# ---------------------------------------------------------------------------
# IG-28 — 模型与数学不变性
# ---------------------------------------------------------------------------

def test_ig_28_model_versions_and_prediction_mathematics_unchanged(isolated_data_dir):
    """IG-28: 模型版本与预测数学参数保持不变。"""
    assert config.MODEL_VERSIONS == {
        "football": "football-coldstart-1",
        "basketball": "basketball-margin-1",
    }
    assert config.MODEL_VERSION == "baseline-1"

    expected_math = {
        "base_goals_per_match": 1.35,
        "home_advantage_elo": 60.0,
        "elo_scale": 400.0,
        "dixon_coles_rho": -0.15,
        "basketball_base_total": 215.0,
        "basketball_elo_scale": 200.0,
        "basketball_home_advantage": 2.5,
    }
    for key, value in expected_math.items():
        assert config.MODEL_CONFIG[key] == value, key

    # 预测仍是输入的纯函数（同一输入两次调用结果完全一致）
    from models.predictor import predict_match
    from utils.team_strength import get_team_profile

    home = get_team_profile("皇马", "西甲", "football")
    away = get_team_profile("巴萨", "西甲", "football")
    first = predict_match(home, away, odds=FOOTBALL_1X2, sport="football", league="西甲")
    second = predict_match(home, away, odds=FOOTBALL_1X2, sport="football", league="西甲")
    assert first == second


# ---------------------------------------------------------------------------
# 附加：预检只读性与跨源组合覆盖（审计补充）
# ---------------------------------------------------------------------------

def test_preflight_does_not_mutate_its_inputs():
    """预检是只读的：不得修改 existing / incoming 的任何字段。"""
    existing = [_match("c1", home="A", away="B", jczq_no="001")]
    incoming = [_match("i1", home="A", away="B", jczq_no="001", odds=FOOTBALL_1X2)]
    existing_before = copy.deepcopy(existing)
    incoming_before = copy.deepcopy(incoming)

    validate_ingestion_identity(existing, incoming)

    assert existing == existing_before
    assert incoming == incoming_before


def test_cross_source_combinations_are_covered():
    """
    跨源组合覆盖（§6）：live 足球 / live 篮球 / 市场 XML / 完场 之间
    的别名冲突必须被检出，而合法对账必须继续成立。
    """
    # (a) live 足球 vs 市场 XML：同别名、不同事件 -> 拒绝
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        validate_ingestion_identity(
            [_match("shared-1", sport="football", home="A", away="B")],
            [_match("shared-1", sport="football", home="C", away="D")],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    # (b) live 篮球 vs 市场 XML：同别名、不同事件（运动不同）-> 拒绝
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        validate_ingestion_identity(
            [_match("shared-2", sport="basketball", home="A", away="B")],
            [_match("shared-2", sport="football", home="A", away="B")],
        )
    assert exc.value.category == "SAME_SOURCE_ALIAS_DIFFERENT_EVENT"

    # (c) live 足球 vs 完场足球：同强编号、不同别名 -> 合法对账，保留 Canonical ID
    merged, added, _updated = scraper._merge(
        [_match("live-9", jczq_no="009", home="皇马", away="巴萨")],
        [_match("500w-2030-05-01-3", jczq_no="009", home="皇马", away="巴萨",
                status="finished", score={"ft": [1, 1]})],
    )
    assert len(merged) == 1
    assert added == 0
    assert merged[0]["id"] == "live-9"

    # (d) 同一 live 响应内的多行：两条互不相关的行不得互相干扰
    validate_ingestion_identity(
        [],
        [
            _match("row-1", home="A", away="B"),
            _match("row-2", home="C", away="D"),
        ],
    )
