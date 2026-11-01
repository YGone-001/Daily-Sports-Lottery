"""
tests/test_basketball_operational_acceptance.py
===============================================
Post-repair operational acceptance for basketball synchronization.

These tests exercise the **reader endpoints** through the Flask WSGI test client and
the merge/admission helpers, using deterministic synthetic stores. They prove code and
endpoint behaviour only; they never call ``refresh()``, never touch ``data/`` and never
depend on a live provider.

Separately-reported states (must not be conflated):

    CODE_TESTED            -> what this module proves
    DEPLOYMENT_VERIFIED    -> proved by the operational probe, not here
    REAL_SOURCE_OBSERVED   -> proved by the read-only source probe, not here
    REAL_REFRESH_EXECUTED  -> NOT executed (authorization required)
    POST_REFRESH_VERIFIED  -> NOT verified (no authorized refresh)
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from utils import daily_loader, fetcher_500, scraper
from utils.market_coverage import (
    basketball_has_moneyline,
    basketball_has_total,
    has_usable_market_odds,
)
from utils.match_identity import same_event
from utils.match_lifecycle import is_finalized_match

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BEIJING = timezone(timedelta(hours=8))
STORES = (
    "daily_matches.json", "team_strength.json", "calibrated.json",
    "prediction_snapshots.json", "odds_snapshots.json", "settlements.json",
    "evaluation_rows.json",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def write_stores(directory, matches, *, extras=None):
    os.makedirs(str(directory), exist_ok=True)
    payloads = {
        "daily_matches.json": {"matches": list(matches), "meta": {"last_refresh": "2030-01-01T00:00:00+08:00"}},
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": {"snapshots": {}, "version": 1},
        "odds_snapshots.json": {"snapshots": {}, "version": 1},
        "settlements.json": {"settlements": {}, "version": 1},
        "evaluation_rows.json": {"rows": {}, "version": 1},
    }
    payloads.update(extras or {})
    for name, payload in payloads.items():
        with open(os.path.join(str(directory), name), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return str(directory)


def store_hashes(directory):
    out = {}
    for name in STORES:
        path = os.path.join(str(directory), name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


FOOTBALL_ONLY_DATE = "2030-05-03"
BASKETBALL_DATE = "2030-05-01"


def basketball_match(match_id, **overrides):
    record = {
        "id": match_id,
        "sport": "basketball",
        "league": "美国职业篮球联盟",
        "round": "6308",
        "date": BASKETBALL_DATE,
        "time": "08:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "犹他爵士",
        "away": "波特兰开拓者",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": {"home_win": 1.60, "away_win": 2.40, "total_line": 224.5,
                 "over": 1.88, "under": 1.92, "matchnum": "6308"},
        "jczq_no": "6308",
    }
    record.update(overrides)
    return record


def football_match(match_id, date=FOOTBALL_ONLY_DATE):
    return {
        "id": match_id,
        "sport": "football",
        "league": "英超",
        "date": date,
        "time": "20:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "甲队",
        "away": "乙队",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6, "matchnum": "3001"},
        "jczq_no": "3001",
    }


def client():
    import app as flask_app

    return flask_app.app.test_client()


@pytest.fixture()
def api_dataset(isolated_data_dir):
    write_stores(
        isolated_data_dir,
        [
            football_match("500l-2030-05-03-3001"),
            basketball_match("500j-2030-05-01-6308"),
            basketball_match("500j-2030-05-01-6310", home="夏洛特黄蜂", away="费城76人",
                             jczq_no="6310", round="6310",
                             odds={"home_win": 1.75, "away_win": 2.10, "total_line": 221.5,
                                   "over": 1.90, "under": 1.90, "matchnum": "6310"}),
        ],
    )
    return isolated_data_dir


# ---------------------------------------------------------------------------
# OA-01 … OA-04: date visibility through the running API
# ---------------------------------------------------------------------------

def test_oa_01_valid_basketball_dates_in_running_api(api_dataset):
    api = client()
    all_dates = api.get("/api/dates").get_json()
    basketball = api.get("/api/dates?sport=basketball").get_json()

    assert all_dates["dates"][0] == FOOTBALL_ONLY_DATE  # 全局最新日期只有足球
    assert all_dates["dates_by_sport"]["basketball"] == [BASKETBALL_DATE]
    assert all_dates["dates_by_sport"]["football"] == [FOOTBALL_ONLY_DATE]
    assert basketball["dates"] == [BASKETBALL_DATE]


def test_oa_02_stored_nba_visible_for_basketball_date(api_dataset):
    api = client()
    response = api.get(f"/api/matches?date={BASKETBALL_DATE}&sport=basketball")
    matches = response.get_json()
    assert response.status_code == 200
    ids = {m["id"] for m in matches}
    assert ids == {"500j-2030-05-01-6308", "500j-2030-05-01-6310"}
    assert all(m["sport"] == "basketball" for m in matches)
    assert all(m.get("odds") for m in matches)


def test_oa_03_stale_explicit_football_date_handled(api_dataset):
    api = client()
    stale = api.get(f"/api/matches?date={FOOTBALL_ONLY_DATE}&sport=basketball").get_json()
    assert stale == [], "stale date must not return football rows under a basketball filter"
    # 前端因此可以判定该日期不属于篮球（见 dateSelection.ts 的 resolveDisplayDate）
    assert FOOTBALL_ONLY_DATE not in daily_loader.get_available_dates("basketball")


def test_oa_04_no_basketball_dates_handled_honestly(isolated_data_dir):
    write_stores(isolated_data_dir, [football_match("500l-2030-05-03-3001")])
    api = client()
    payload = api.get("/api/dates").get_json()
    assert payload["dates_by_sport"]["basketball"] == []
    assert api.get("/api/dates?sport=basketball").get_json()["dates"] == []

    status = api.get("/api/status").get_json()
    health = status["source_health"]
    assert health["stored"]["basketball"] == 0
    assert health["displayed"]["basketball_on_default_date"] == 0
    assert health["basketball_dates"] == []


# ---------------------------------------------------------------------------
# OA-05 / OA-06: diagnostics must not fake a recent success
# ---------------------------------------------------------------------------

def test_oa_05_current_diagnostics_are_not_confused_with_cached_success(api_dataset):
    fetcher_500._CACHE.clear()
    fetcher_500._DIAGNOSTICS.clear()

    health = client().get("/api/status").get_json()["source_health"]
    for name in ("live_basketball", "jclq_sf", "jclq_dxf"):
        entry = health["sources"][name]
        # 本轮进程没有发起任何请求：既不能报 ok，也不能报旧的成功时间
        assert entry["status"] == "not_probed"
        assert entry.get("last_success_at") is None
    assert health["stored"]["basketball"] == 2


def test_oa_06_real_and_synthetic_coverage_metrics_are_separable(api_dataset):
    health = client().get("/api/status").get_json()["source_health"]
    # 存储计数来自数据本身；源计数只可能来自真实探测，未探测时显式标 not_probed
    assert set(health["stored"]) == {"basketball", "nba", "basketball_with_valid_market"}
    assert health["stored"]["nba"] == 2
    assert health["stored"]["basketball_with_valid_market"] == 2
    for entry in health["sources"].values():
        assert entry["status"] in {"not_probed", "ok", "empty", "unavailable"}
        assert "rows" in entry or entry["status"] == "not_probed"


# ---------------------------------------------------------------------------
# OA-07 … OA-11: merge and admission integrity
# ---------------------------------------------------------------------------

def _live_row(odds, **overrides):
    row = {
        "id": "500lq-2030-05-01-周六308",
        "sport": "basketball",
        "league": "NBA",
        "round": "",
        "date": BASKETBALL_DATE,
        "time": "08:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "犹他爵士",
        "away": "波特兰开拓者",
        "score": None,
        "odds": odds,
        "jczq_no": "6308",
    }
    row.update(overrides)
    return row


def _market_row(odds, match_id="500j-2030-05-01-6308", jczq_no="6308"):
    return {
        "id": match_id,
        "sport": "basketball",
        "league": "美国职业篮球联盟",
        "round": jczq_no,
        "date": BASKETBALL_DATE,
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "犹他爵士",
        "away": "波特兰开拓者",
        "score": None,
        "odds": odds,
        "jczq_no": jczq_no,
    }


def test_oa_07_matching_sf_and_dxf_markets_merge():
    live = _live_row({"home_win": 1.62, "away_win": 2.35})
    rows = [
        _market_row({"home_win": 1.60, "away_win": 2.40, "matchnum": "6308"}),
        _market_row({"total_line": 224.5, "over": 1.88, "under": 1.92}),
    ]
    attached, used = scraper._attach_odds([live], rows)
    assert attached == 1
    assert len(used) == 2
    assert live["odds"]["home_win"] == 1.62          # 已有有效值优先
    assert live["odds"]["total_line"] == 224.5       # 缺失的大小分补入
    assert basketball_has_moneyline(live["odds"]) and basketball_has_total(live["odds"])


def test_oa_08_existing_valid_prices_are_preserved():
    target = {"home_win": 1.60, "away_win": 2.40, "total_line": 224.5, "over": 1.88, "under": 1.92}
    changed = scraper._fill_missing_odds(
        target, {"home_win": 1.75, "away_win": 2.05, "total_line": 219.5, "over": 1.95, "under": 1.85}
    )
    assert changed is False
    assert target == {"home_win": 1.60, "away_win": 2.40, "total_line": 224.5,
                      "over": 1.88, "under": 1.92}


@pytest.mark.parametrize(
    "invalid_value",
    [0.0, 1.0, float("nan"), float("inf"), "1.60", None, True],
)
def test_oa_09_invalid_existing_prices_cannot_block_valid_market_coverage(invalid_value):
    target = {"home_win": invalid_value, "away_win": 2.40}
    assert basketball_has_moneyline(target) is False, "invalid price is not usable coverage"

    changed = scraper._fill_missing_odds(target, {"home_win": 1.60})
    assert changed is True
    assert target["home_win"] == 1.60
    assert basketball_has_moneyline(target) is True


def test_oa_09b_invalid_incoming_values_are_never_written():
    target = {"home_win": 1.60}
    assert scraper._fill_missing_odds(target, {"home_win": 0.0}) is False
    assert scraper._fill_missing_odds(target, {"over": 1.0}) is False
    assert scraper._fill_missing_odds(target, {"total_line": float("nan")}) is False
    assert target == {"home_win": 1.60}


def test_oa_09c_invalid_total_line_is_replaced_by_a_valid_one():
    target = {"total_line": float("nan"), "over": 1.9, "under": 1.9}
    assert basketball_has_total(target) is False
    assert scraper._fill_missing_odds(target, {"total_line": 220.5}) is True
    assert target["total_line"] == 220.5


def test_oa_10_incomplete_odds_remain_non_authoritative():
    partial = _live_row({"home_win": 1.6}, jczq_no="")
    admitted, rejected = scraper._admit_market_candidates([partial], existing=[])
    assert admitted == [] and rejected == 1
    assert has_usable_market_odds(partial) is False


def test_oa_11_strong_competition_number_conflicts_still_reject_unsafe_matching():
    live = _live_row({"home_win": 1.62, "away_win": 2.35})
    wrong = _market_row({"home_win": 1.10, "away_win": 6.00}, match_id="500j-2030-05-01-9999",
                        jczq_no="9999")
    assert same_event(live, wrong) is False
    attached, used = scraper._attach_odds([live], [wrong])
    assert attached == 0 and used == []
    assert live["odds"] == {"home_win": 1.62, "away_win": 2.35}


# ---------------------------------------------------------------------------
# OA-12 / OA-13: historical status must not create facts
# ---------------------------------------------------------------------------

def test_oa_12_historical_status_does_not_create_fictitious_final_results():
    stale = basketball_match("500lq-2030-05-01-周六308", status="live", score=None)
    # 显示层会按时间推导「已过开赛窗口」
    derived = daily_loader.add_time_status(stale, now=datetime(2030, 5, 2, 12, 0, tzinfo=BEIJING))
    assert derived["status"] == "finished"
    # 但「可结算」必须要求真实赛果，绝不能只凭时间
    assert is_finalized_match(stale) is False
    assert is_finalized_match({**stale, "status": "finished"}) is False
    assert is_finalized_match({**stale, "status": "finished", "score": {"ft": [98, 91]}}) is True


def test_oa_13_derived_display_status_does_not_rewrite_stored_or_settlement_history(
    isolated_data_dir,
):
    stored = basketball_match("500lq-2020-05-01-周六308", status="live", score=None,
                              date="2020-05-01")
    write_stores(isolated_data_dir, [stored])
    before = store_hashes(isolated_data_dir)

    enriched = daily_loader.enrich_match(dict(stored))
    assert enriched["status"] == "finished"      # 派生显示状态（时间已过）
    assert stored["status"] == "live"            # 存储事实未被改写
    after = store_hashes(isolated_data_dir)
    for name in NEVER_WRITTEN_BY_READER:
        assert after[name] == before[name], f"enrich_match rewrote {name}"
    assert {n for n in after if after[n] != before[n]} <= MATERIALIZED_BY_DESIGN


# ---------------------------------------------------------------------------
# OA-14 … OA-16: read-only safety and no activation
# ---------------------------------------------------------------------------

#: 读取路径**按设计**会物化的两个存储（赛前快照 + 球队库），不属于本阶段可改范围。
MATERIALIZED_BY_DESIGN = {"team_strength.json", "prediction_snapshots.json"}
#: 读取路径绝不允许改写的存储（Canonical / 赔率历史 / 结算 / 评估）。
NEVER_WRITTEN_BY_READER = (
    "daily_matches.json", "calibrated.json", "odds_snapshots.json",
    "settlements.json", "evaluation_rows.json",
)


def test_oa_14_reader_endpoints_do_not_write_canonical_or_historical_stores(api_dataset):
    before = store_hashes(api_dataset)
    api = client()
    for path in (
        "/api/dates",
        "/api/dates?sport=basketball",
        f"/api/matches?date={BASKETBALL_DATE}&sport=basketball",
        f"/api/matches?date={FOOTBALL_ONLY_DATE}&sport=basketball",
        "/api/status",
    ):
        assert api.get(path).status_code == 200, path

    after = store_hashes(api_dataset)
    for name in NEVER_WRITTEN_BY_READER:
        assert after[name] == before[name], f"reader endpoint rewrote {name}"
    changed = {name for name in after if after[name] != before[name]}
    assert changed <= MATERIALIZED_BY_DESIGN, changed


def test_oa_14b_view_snapshot_materialization_is_idempotent(api_dataset):
    api = client()
    path = f"/api/matches?date={BASKETBALL_DATE}&sport=basketball"
    api.get(path)
    first = store_hashes(api_dataset)["prediction_snapshots.json"]
    api.get(path)
    api.get(path)
    assert store_hashes(api_dataset)["prediction_snapshots.json"] == first


def test_oa_15_no_canonical_runtime_activation():
    from utils.runtime_mapping_policy import runtime_policy_status

    status = runtime_policy_status()
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"
    assert status["production_mapping_write_authorization"] == "NOT_GRANTED"


def test_oa_16_seven_protected_stores_unchanged_during_read_only_validation(tmp_path):
    directory = write_stores(tmp_path / "stores", [
        basketball_match("500j-2030-05-01-6308"),
        football_match("500l-2030-05-03-3001"),
    ])
    before = store_hashes(directory)
    # 只读路径：日期查询、诊断构建、合并、准入
    daily_loader.get_available_dates("basketball")
    daily_loader.get_available_dates_by_sport()
    daily_loader.get_default_date()
    scraper._attach_odds([_live_row({"home_win": 1.6})], [_market_row({"total_line": 220.5})])
    scraper._admit_market_candidates([_market_row({"home_win": 1.6, "away_win": 2.4})], existing=[])
    assert store_hashes(directory) == before


# ---------------------------------------------------------------------------
# OA-17: real instance probe (local only; never in CI)
# ---------------------------------------------------------------------------

REAL_CANONICAL = os.path.join(ROOT, "data", "daily_matches.json")


@pytest.mark.skipif(
    not os.path.isfile(REAL_CANONICAL),
    reason="no local real dataset (git-ignored); synthetic tests only in CI",
)
def test_oa_17_real_instance_exposes_dates_by_sport_and_visible_nba():
    from tools.real_sports_identity_audit import default_data_dir

    data_dir = default_data_dir()
    before = store_hashes(data_dir)
    import app as flask_app

    api = flask_app.app.test_client()
    payload = api.get("/api/dates").get_json()
    assert "dates_by_sport" in payload
    basketball_dates = payload["dates_by_sport"]["basketball"]
    assert basketball_dates, "real data must expose at least one basketball date"
    assert basketball_dates[0] in payload["dates"]

    status = api.get("/api/status").get_json()["source_health"]
    assert status["stored"]["basketball"] > 0

    visible = api.get(f"/api/matches?date={basketball_dates[0]}&sport=basketball").get_json()
    assert all(m["sport"] == "basketball" for m in visible)
    assert store_hashes(data_dir) == before