"""
tests/test_basketball_sync_integrity.py
=======================================
Basketball lottery synchronization: parsing, odds merge, market admission,
sport-aware date selection and source diagnostics.

All fixtures are synthetic but follow the **observed** source shapes
(``var matchList=[...]`` / ``var oddsList={...}`` for the live basketball page,
``<m ...><row .../></m>`` for the jclq XML). No test performs a network call, so
CI never depends on an external service being available.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from utils import daily_loader, fetcher_500, scraper
from utils.market_coverage import (
    basketball_has_moneyline,
    basketball_has_total,
    has_usable_market_odds,
)
from utils.match_identity import same_event

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Fixtures (shapes copied from the live sources, values synthetic)
# ---------------------------------------------------------------------------

NBA_MATCHLIST_HTML = """
<script>
var matchList=[
 ["1","243049","x","2026-11-02","08:00","","NBA","","","","","","","洛杉矶湖人","","","","","金州勇士","","","","","","","","6308"],
 ["1","243050","x","2026-11-02","09:30","","NBA","","","","","","","波士顿凯尔特人","","","","","迈阿密热火","","","","","","","","6309"]
];
var oddsList={"243049":[["1","官方","1.62","2.35"]]};
</script>
"""

BASKETBALL_EMPTY_SHELL_HTML = """
<script>
var matchList=[];
var oddsList={};
</script>
"""

BASKETBALL_MALFORMED_ROWS_HTML = """
<script>
var matchList=[
 ["1","243051","x","not-a-date","08:00","","NBA","","","","","","","","","","","","","","","","","","","","6310"],
 "this-is-not-a-row"
];
var oddsList={};
</script>
"""

JCLQ_SF_XML = """<?xml version="1.0" encoding="utf-8"?>
<xml>
<m id="900001" date="2026-11-02" dayofweek="星期日" matchnum="6308" league="美国职业篮球联盟"
   home="洛杉矶湖人" away="金州勇士">
  <row win="1.60" lost="2.40" />
</m>
</xml>
"""

JCLQ_DXF_XML = """<?xml version="1.0" encoding="utf-8"?>
<xml>
<m id="900001" date="2026-11-02" dayofweek="星期日" matchnum="6308" league="美国职业篮球联盟"
   home="洛杉矶湖人" away="金州勇士">
  <row yszf="224.5" big="1.88" small="1.92" />
</m>
</xml>
"""

JCLQ_SF_EMPTY_XML = '<?xml version="1.0" encoding="utf-8"?><xml></xml>'


class _Resp:
    def __init__(self, text: str, status_code: int = 200):
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": "text/html"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _clear_caches():
    fetcher_500._CACHE.clear()
    # ``reset_source_observation`` clears both the diagnostics and the retained
    # observation rows, so a previous test can never masquerade as "just observed".
    fetcher_500.reset_source_observation()


def _patch_live(monkeypatch, text=None, *, error=None):
    def fake_get(url, **kwargs):
        if error is not None:
            raise error
        return _Resp(text if text is not None else NBA_MATCHLIST_HTML)

    monkeypatch.setattr(fetcher_500.requests, "get", fake_get)


def _patch_xml(monkeypatch, mapping):
    def fake_fetch_xml(lot, play):
        return mapping.get(play, JCLQ_SF_EMPTY_XML)

    monkeypatch.setattr(fetcher_500, "_fetch_xml", fake_fetch_xml)


_DEFAULT_ODDS = object()


def live_nba_row(odds=_DEFAULT_ODDS, **overrides):
    row = {
        "id": "500lq-2026-11-02-6308",
        "sport": "basketball",
        "league": "NBA",
        "round": "",
        "date": "2026-11-02",
        "time": "08:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": (
            {"home_win": 1.62, "away_win": 2.35} if odds is _DEFAULT_ODDS else odds
        ),
        "jczq_no": "6308",
    }
    row.update(overrides)
    return row


def market_row(odds, *, jczq_no="6308", match_id="500j-2026-11-02-6308"):
    return {
        "id": match_id,
        "sport": "basketball",
        "league": "美国职业篮球联盟",
        "round": jczq_no,
        "date": "2026-11-02",
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": odds,
        "jczq_no": jczq_no,
    }


PROTECTED = (
    "daily_matches.json", "team_strength.json", "calibrated.json",
    "prediction_snapshots.json", "odds_snapshots.json", "settlements.json",
    "evaluation_rows.json",
)


def store_hashes(directory):
    out = {}
    for name in PROTECTED:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                out[name] = hashlib.sha256(handle.read()).hexdigest()
    return out


def write_store(directory, matches):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "daily_matches.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"matches": matches, "meta": {}}, handle, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# BS-01 … BS-04: source rows, empty shell, unavailable, malformed
# ---------------------------------------------------------------------------

def test_bs_01_valid_nba_basketball_source_rows_parse():
    rows, errors = fetcher_500._parse_lq(NBA_MATCHLIST_HTML)
    assert errors == 0
    assert len(rows) == 2
    first = rows[0]
    assert first["sport"] == "basketball"
    assert first["league"] == "NBA"
    assert first["home"] == "洛杉矶湖人" and first["away"] == "金州勇士"
    assert first["date"] == "2026-11-02" and first["time"] == "08:00"
    assert first["jczq_no"] == "6308"
    # 只有官方(provider=1)赔率被采用，且只有一条有赔率
    assert first["odds"] == {"home_win": 1.62, "away_win": 2.35}
    assert rows[1]["odds"] is None
    assert fetcher_500.is_nba_league(first["league"]) is True


def test_bs_02_empty_javascript_shell_is_diagnosed_as_empty(monkeypatch):
    _clear_caches()
    _patch_live(monkeypatch, BASKETBALL_EMPTY_SHELL_HTML)
    rows = fetcher_500.fetch_live_basketball()
    assert rows == []
    diag = fetcher_500.source_diagnostics()["live_basketball"]
    # 空的 JS 外壳必须是「空」，绝不能被当成健康的比赛源
    assert diag["status"] == "empty"
    assert diag["rows"] == 0
    assert diag["last_success_at"] is None


def test_bs_03_unavailable_source_is_distinct_from_empty_source(monkeypatch):
    _clear_caches()
    _patch_live(monkeypatch, error=RuntimeError("connection refused"))
    assert fetcher_500.fetch_live_basketball() == []
    unavailable = fetcher_500.source_diagnostics()["live_basketball"]
    assert unavailable["status"] == "unavailable"
    assert unavailable["last_success_at"] is None

    _clear_caches()
    _patch_live(monkeypatch, BASKETBALL_EMPTY_SHELL_HTML)
    assert fetcher_500.fetch_live_basketball() == []
    empty = fetcher_500.source_diagnostics()["live_basketball"]
    assert empty["status"] == "empty"
    assert empty["status"] != unavailable["status"]


def test_bs_04_malformed_source_response_does_not_produce_rows():
    rows, errors = fetcher_500._parse_lq(BASKETBALL_MALFORMED_ROWS_HTML)
    assert rows == []
    assert errors >= 2  # 非日期行 + 非数组行都计入解析错误
    # 非 JS 响应同样不产出比赛
    rows2, errors2 = fetcher_500._parse_lq("<html><body>no script</body></html>")
    assert rows2 == [] and errors2 == 0


# ---------------------------------------------------------------------------
# BS-05 … BS-08: lottery market parsing, partial odds completion, fallback
# ---------------------------------------------------------------------------

def test_bs_05_valid_sf_moneyline_parses(monkeypatch):
    _clear_caches()
    _patch_xml(monkeypatch, {"sf": JCLQ_SF_XML, "dxf": JCLQ_SF_EMPTY_XML})
    rows = fetcher_500.fetch_jczq_xml("basketball")
    assert len(rows) == 1
    entry = rows[0]
    assert entry["sport"] == "basketball"
    assert entry["jczq_no"] == "6308"
    assert entry["odds"]["home_win"] == 1.60 and entry["odds"]["away_win"] == 2.40
    assert basketball_has_moneyline(entry["odds"]) is True
    assert basketball_has_total(entry["odds"]) is False


def test_bs_06_valid_dxf_totals_parse(monkeypatch):
    _clear_caches()
    _patch_xml(monkeypatch, {"sf": JCLQ_SF_EMPTY_XML, "dxf": JCLQ_DXF_XML})
    rows = fetcher_500.fetch_jczq_xml("basketball")
    assert len(rows) == 1
    odds = rows[0]["odds"]
    assert odds["total_line"] == 224.5
    assert odds["over"] == 1.88 and odds["under"] == 1.92
    assert basketball_has_total(odds) is True
    assert basketball_has_moneyline(odds) is False


def test_bs_07_partial_live_odds_are_completed_by_matching_market_odds():
    live = live_nba_row(odds={"home_win": 1.62, "away_win": 2.35})
    rows = [
        market_row({"home_win": 1.60, "away_win": 2.40, "matchnum": "6308"}),
        market_row({"total_line": 224.5, "over": 1.88, "under": 1.92}, match_id="500j-2026-11-02-6308"),
    ]
    attached, used = scraper._attach_odds([live], rows)

    assert attached == 1
    assert len(used) == 2, "两个玩法行都必须被消费"
    odds = live["odds"]
    # 已有值优先（直播胜负盘不被覆盖），缺失的大小分被补入
    assert odds["home_win"] == 1.62 and odds["away_win"] == 2.35
    assert odds["total_line"] == 224.5
    assert has_usable_market_odds(live) is True


def test_bs_08_unmatched_valid_market_row_is_admitted_through_the_fallback():
    alone = market_row({"home_win": 1.70, "away_win": 2.20}, jczq_no="7001",
                       match_id="500j-2026-11-02-7001")
    alone.update({"home": "丹佛掘金", "away": "萨克拉门托国王"})
    incoming = [live_nba_row()]
    added = scraper._append_unmatched_market_candidates(incoming, [alone], [])
    assert added == 1
    admitted, rejected = scraper._admit_market_candidates(incoming, existing=[])
    ids = {m["id"] for m in admitted}
    assert "500j-2026-11-02-7001" in ids
    assert rejected == 0
    assert all(m.get("market_tracked") is True for m in admitted if m["id"].startswith("500j-"))


# ---------------------------------------------------------------------------
# BS-09 … BS-11: admission rules and identity safety
# ---------------------------------------------------------------------------

def test_bs_09_incomplete_odds_do_not_authorize_market_admission():
    partial = live_nba_row(odds={"home_win": 1.62}, jczq_no="")
    assert has_usable_market_odds(partial) is False
    admitted, rejected = scraper._admit_market_candidates([partial], existing=[])
    assert admitted == []
    assert rejected == 1


def test_bs_10_missing_lottery_odds_remain_non_authoritative():
    no_odds = live_nba_row(odds=None)
    admitted, rejected = scraper._admit_market_candidates([no_odds], existing=[])
    assert admitted == []
    assert rejected == 1
    # 磁盘上只允许一条「无变化」的写入路径：不生成任何赔率
    assert no_odds["odds"] is None


def test_bs_11_basketball_matching_respects_strong_competition_number_conflicts():
    live = live_nba_row()
    wrong_market = market_row({"home_win": 1.10, "away_win": 6.00}, jczq_no="9999",
                              match_id="500j-2026-11-02-9999")
    # 双方都有强编号且不一致 -> same_event 必须为 False，绝不能靠队名合并
    assert same_event(live, wrong_market) is False
    attached, used = scraper._attach_odds([live], [wrong_market])
    assert attached == 0
    assert used == []
    assert live["odds"] == {"home_win": 1.62, "away_win": 2.35}


# ---------------------------------------------------------------------------
# BS-12 … BS-15: Beijing matchday, sport-aware dates, stored-but-hidden
# ---------------------------------------------------------------------------

def test_bs_12_nba_event_dates_respect_beijing_time():
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=8))
    assert daily_loader.get_logical_matchday(datetime(2026, 11, 1, 23, 59, tzinfo=tz)) == "2026-11-01"
    assert daily_loader.get_logical_matchday(datetime(2026, 11, 2, 0, 1, tzinfo=tz)) == "2026-11-01"
    assert daily_loader.get_logical_matchday(datetime(2026, 11, 2, 5, 59, tzinfo=tz)) == "2026-11-01"
    assert daily_loader.get_logical_matchday(datetime(2026, 11, 2, 6, 0, tzinfo=tz)) == "2026-11-02"

    # 存储日期按北京时间原样保留，刷新路径不做任何跨时区改写。
    rows, _errors = fetcher_500._parse_lq(NBA_MATCHLIST_HTML)
    assert rows[0]["date"] == "2026-11-02"
    kickoff = daily_loader.get_match_datetime(rows[0])
    assert kickoff is not None and kickoff.utcoffset() == timedelta(hours=8)


def test_bs_13_basketball_view_selects_a_date_with_basketball_matches(isolated_data_dir):
    write_store(str(isolated_data_dir), [
        {"id": "500l-2026-11-03-3001", "sport": "football", "league": "英超", "date": "2026-11-03",
         "time": "20:00", "home": "甲队", "away": "乙队", "status": "upcoming", "jczq_no": "3001",
         "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6}, "score": None},
        {"id": "500j-2026-11-02-6308", "sport": "basketball", "league": "美国职业篮球联盟",
         "date": "2026-11-02", "time": "08:00", "home": "湖人", "away": "勇士", "status": "upcoming",
         "jczq_no": "6308", "odds": {"home_win": 1.6, "away_win": 2.4}, "score": None},
    ])
    assert daily_loader.get_available_dates()[0] == "2026-11-03"
    basketball_dates = daily_loader.get_available_dates("basketball")
    assert basketball_dates == ["2026-11-02"]
    assert daily_loader.get_matches_by_date(basketball_dates[0], "basketball")


def test_bs_14_changing_sport_never_selects_an_empty_date(isolated_data_dir):
    write_store(str(isolated_data_dir), [
        {"id": "500l-2026-11-03-3001", "sport": "football", "league": "英超", "date": "2026-11-03",
         "time": "20:00", "home": "甲队", "away": "乙队", "status": "upcoming", "jczq_no": "3001",
         "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6}, "score": None},
        {"id": "500j-2026-11-02-6308", "sport": "basketball", "league": "美国职业篮球联盟",
         "date": "2026-11-02", "time": "08:00", "home": "湖人", "away": "勇士", "status": "upcoming",
         "jczq_no": "6308", "odds": {"home_win": 1.6, "away_win": 2.4}, "score": None},
    ])
    by_sport = daily_loader.get_available_dates_by_sport()
    assert by_sport["basketball"] == ["2026-11-02"]
    assert by_sport["football"] == ["2026-11-03"]
    # 每个运动的首个日期都必须真的含有该运动的比赛
    for sport, dates in by_sport.items():
        assert daily_loader.get_matches_by_date(dates[0], sport), sport


def test_bs_15_stored_but_hidden_matches_are_distinguishable_from_missing_data(isolated_data_dir):
    write_store(str(isolated_data_dir), [
        {"id": "500l-2026-11-03-3001", "sport": "football", "league": "英超", "date": "2026-11-03",
         "time": "20:00", "home": "甲队", "away": "乙队", "status": "upcoming", "jczq_no": "3001",
         "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6}, "score": None},
        {"id": "500j-2026-11-02-6308", "sport": "basketball", "league": "美国职业篮球联盟",
         "date": "2026-11-02", "time": "08:00", "home": "湖人", "away": "勇士", "status": "upcoming",
         "jczq_no": "6308", "odds": {"home_win": 1.6, "away_win": 2.4}, "score": None},
    ])
    stored = [m for m in daily_loader.get_all_matches() if m.get("sport") == "basketball"]
    newest_overall = daily_loader.get_available_dates()[0]
    assert stored, "数据确实已存储"
    assert daily_loader.get_matches_by_date(newest_overall, "basketball") == []
    assert daily_loader.get_matches_by_date(daily_loader.get_available_dates("basketball")[0], "basketball")


# ---------------------------------------------------------------------------
# BS-16: diagnostics report the failing stage
# ---------------------------------------------------------------------------

def test_bs_16_source_diagnostics_report_the_correct_stage(monkeypatch):
    _clear_caches()
    _patch_live(monkeypatch, NBA_MATCHLIST_HTML)
    _patch_xml(monkeypatch, {"sf": JCLQ_SF_XML, "dxf": JCLQ_DXF_XML})
    fetcher_500.fetch_live_basketball()
    fetcher_500.fetch_jczq_xml("basketball")

    diag = fetcher_500.source_diagnostics()
    assert diag["live_basketball"]["status"] == "ok"
    assert diag["live_basketball"]["rows"] == 2
    assert diag["live_basketball"]["nba_rows"] == 2
    assert diag["live_basketball"]["valid_market_rows"] == 1
    assert diag["live_basketball"]["last_nba_success_rows"] == 2

    for play_key, valid in (("jclq_sf", 1), ("jclq_dxf", 1)):
        assert diag[play_key]["status"] == "ok"
        assert diag[play_key]["rows"] == 1
        assert diag[play_key]["valid_market_rows"] == valid
        assert diag[play_key]["nba_rows"] == 1
    assert diag["jczq_odds_basketball"]["status"] == "ok"

    # 一个玩法不可用时，其它玩法的阶段不受影响
    _clear_caches()

    def flaky(lot, play):
        if play == "dxf":
            raise RuntimeError("dxf unavailable")
        return JCLQ_SF_XML

    monkeypatch.setattr(fetcher_500, "_fetch_xml", flaky)
    fetcher_500.fetch_jczq_xml("basketball")
    diag = fetcher_500.source_diagnostics()
    assert diag["jclq_sf"]["status"] == "ok"
    assert diag["jclq_dxf"]["status"] == "unavailable"


def test_bs_16b_cached_results_do_not_refresh_the_check_timestamp(monkeypatch):
    _clear_caches()
    _patch_live(monkeypatch, NBA_MATCHLIST_HTML)
    fetcher_500.fetch_live_basketball()
    first = fetcher_500.source_diagnostics()["live_basketball"]["checked_at"]

    # 第二次调用命中缓存：不应伪装成「刚刚又成功抓取了一次」
    assert fetcher_500.fetch_live_basketball()
    second = fetcher_500.source_diagnostics()["live_basketball"]["checked_at"]
    assert first == second


# ---------------------------------------------------------------------------
# BS-17 / BS-18: no duplicate admission, no downstream rewrite
# ---------------------------------------------------------------------------

def test_bs_17_no_duplicate_canonical_admission():
    live = live_nba_row()
    duplicate_market = market_row({"home_win": 1.60, "away_win": 2.40})
    incoming = [live]
    added = scraper._append_unmatched_market_candidates(incoming, [duplicate_market], [])
    assert added == 0
    assert len(incoming) == 1

    ids = [m["id"] for m in incoming]
    assert len(ids) == len(set(ids))


def test_bs_18_no_prediction_or_settlement_rewrite(tmp_path):
    before = store_hashes(str(tmp_path))
    live = live_nba_row()
    rows = [market_row({"home_win": 1.60, "away_win": 2.40}),
            market_row({"total_line": 224.5, "over": 1.88, "under": 1.92})]
    scraper._attach_odds([live], rows)
    scraper._admit_market_candidates([live], existing=[])
    after = store_hashes(str(tmp_path))
    assert before == after == {}


# ---------------------------------------------------------------------------
# BS-19 … BS-22: coverage semantics, store integrity, no activation, CI matrix
# ---------------------------------------------------------------------------

def test_bs_19_existing_market_coverage_semantics_are_unchanged():
    assert basketball_has_moneyline({"home_win": 1.5, "away_win": 2.4}) is True
    assert basketball_has_moneyline({"home_win": 1.5}) is False
    assert basketball_has_moneyline({"home_win": 1.0, "away_win": 2.4}) is False
    assert basketball_has_total({"total_line": 220.5, "over": 1.9, "under": 1.9}) is True
    assert basketball_has_total({"total_line": 220.5}) is False
    assert has_usable_market_odds({"sport": "basketball", "odds": None}) is False
    assert has_usable_market_odds({"sport": "football",
                                    "odds": {"home_win": 2.0, "draw": 3.3, "away_win": 3.6}}) is True


def test_bs_20_seven_production_stores_remain_byte_identical(isolated_data_dir):
    matches = [
        {"id": "500j-2026-11-02-6308", "sport": "basketball", "league": "美国职业篮球联盟",
         "date": "2026-11-02", "time": "08:00", "home": "湖人", "away": "勇士", "status": "upcoming",
         "jczq_no": "6308", "odds": {"home_win": 1.6, "away_win": 2.4}, "score": None},
    ]
    write_store(str(isolated_data_dir), matches)
    before = store_hashes(str(isolated_data_dir))
    # 只读路径：解析、日期查询、覆盖率判定、合并
    fetcher_500._parse_lq(NBA_MATCHLIST_HTML)
    daily_loader.get_available_dates("basketball")
    daily_loader.get_matches_by_date("2026-11-02", "basketball")
    scraper._attach_odds([dict(m) for m in matches], [])
    assert store_hashes(str(isolated_data_dir)) == before


def test_bs_21_no_production_runtime_mapping_activation():
    from utils.runtime_mapping_policy import runtime_policy_status

    status = runtime_policy_status()
    assert status["runtime_mapping_activation"] == "NOT_ENABLED"
    assert status["production_mapping_write_authorization"] == "NOT_GRANTED"

    source = open(os.path.join(ROOT, "utils", "fetcher_500.py"), encoding="utf-8").read()
    assert "activate_runtime_mapping" not in source
    assert "production_mapping" not in source


def test_bs_22_ci_matrix_still_covers_ubuntu_and_windows():
    workflow = open(os.path.join(ROOT, ".github", "workflows", "ci.yml"), encoding="utf-8").read()
    assert "ubuntu-latest" in workflow and "windows-latest" in workflow
    assert "offline-tests:" in workflow and "staging-validation:" in workflow


# ---------------------------------------------------------------------------
# BS-23: source health payload is read-only and free of secrets
# ---------------------------------------------------------------------------

def test_bs_23_source_health_payload_is_safe(isolated_data_dir):
    from app import build_source_health

    write_store(str(isolated_data_dir), [
        {"id": "500j-2026-11-02-6308", "sport": "basketball", "league": "美国职业篮球联盟",
         "date": "2026-11-02", "time": "08:00", "home": "湖人", "away": "勇士", "status": "upcoming",
         "jczq_no": "6308", "odds": {"home_win": 1.6, "away_win": 2.4}, "score": None},
    ])
    health = build_source_health()
    assert set(health) >= {"sources", "stored", "displayed"}
    assert set(health["sources"]) == {"live_basketball", "jclq_sf", "jclq_dxf"}
    assert health["stored"]["basketball"] == 1
    assert health["stored"]["nba"] == 1
    assert health["stored"]["basketball_with_valid_market"] == 1
    assert health["displayed"]["default_date"]
    assert health["basketball_dates"] == ["2026-11-02"]

    serialized = json.dumps(health, ensure_ascii=False)
    for forbidden in ("http://", "https://", "token", "password", "cookie"):
        assert forbidden not in serialized.lower()


# ---------------------------------------------------------------------------
# BS-24: date selection survives a dataset with no actionable match
# ---------------------------------------------------------------------------

def test_bs_24_default_date_without_actionable_days(monkeypatch):
    """
    A dataset whose matches have all kicked off (or finished) must not crash
    date selection: the distance helper is required by the fallback branch too.
    """
    monkeypatch.setattr(daily_loader, "get_available_dates", lambda *a, **k: ["2026-11-02"])
    monkeypatch.setattr(daily_loader, "get_logical_matchday", lambda *a, **k: "2026-11-05")
    monkeypatch.setattr(daily_loader, "get_all_matches", lambda *a, **k: [])
    monkeypatch.setattr(
        daily_loader, "get_available_dates_with_upcoming", lambda *a, **k: ["2026-11-02"]
    )

    assert daily_loader.get_default_date() == "2026-11-02"


# ---------------------------------------------------------------------------
# BS-25 … BS-31: isolated end-to-end refresh rehearsal
#
# Every test in this section calls the **real** ``scraper.refresh()`` against a
# temporary DATA_DIR with mocked source adapters. They prove code behaviour only:
#
#     ISOLATED_REFRESH_REHEARSAL   -> what this section proves
#     REAL_REFRESH_EXECUTED        -> NOT executed (separate operator authorization)
#     POST_REFRESH_VERIFIED        -> NOT verified (no authorized real refresh)
#
# The seven-store write set is deliberately exercised: an isolated rehearsal is
# allowed to mutate its own temporary stores, never ``data/``.
# ---------------------------------------------------------------------------

from utils import refresh_state  # noqa: E402
from utils.concurrency import RefreshBusyError  # noqa: E402
from utils.ingestion_identity import CanonicalIdentityCollisionError  # noqa: E402
from utils.match_identity import competition_number_for_match  # noqa: E402

REHEARSAL_DATE = "2030-05-02"
TRACKED_DATE = "2030-05-01"
TRACKED_ID = "500j-2030-05-01-6301"


def write_refresh_stores(directory, matches, *, prediction_snapshots=None,
                         odds_snapshots=None, settlements=None, evaluation_rows=None):
    """Write all seven protected stores so a refresh has a complete universe."""
    os.makedirs(directory, exist_ok=True)
    payloads = {
        "daily_matches.json": {
            "matches": list(matches),
            "meta": {"last_refresh": "2030-01-01T00:00:00+08:00"},
        },
        "team_strength.json": {"teams": {}},
        "calibrated.json": {"ids": []},
        "prediction_snapshots.json": prediction_snapshots or {"version": 1, "snapshots": {}},
        "odds_snapshots.json": odds_snapshots or {"version": 1, "snapshots": {}},
        "settlements.json": settlements or {"version": 1, "settlements": {}},
        "evaluation_rows.json": evaluation_rows or {"version": 1, "rows": {}},
    }
    for name, payload in payloads.items():
        with open(os.path.join(directory, name), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return directory


def patch_refresh_sources(monkeypatch, *, live_basketball=(), live_football=(),
                          xml=None, finished=()):
    """Replace the four source adapters; no test ever reaches the network."""
    rows = {"basketball": list(live_basketball), "football": list(live_football)}
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: list(rows["basketball"]))
    monkeypatch.setattr(
        fetcher_500, "fetch_live_matches", lambda sport="football": list(rows["football"])
    )
    monkeypatch.setattr(
        fetcher_500, "fetch_jczq_xml", lambda sport="football": list((xml or {}).get(sport, []))
    )
    monkeypatch.setattr(
        fetcher_500, "fetch_finished_matches", lambda date=None: list(finished)
    )


def tracked_nba_row():
    return {
        "id": TRACKED_ID,
        "sport": "basketball",
        "league": "美国职业篮球联盟",
        "round": "6301",
        "date": TRACKED_DATE,
        "time": "08:00",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": {"home_win": 1.60, "away_win": 2.40, "matchnum": "6301"},
        "jczq_no": "6301",
        "market_tracked": True,
    }


def rehearsal_live(number, *, home, away, odds=True, league="NBA", date=REHEARSAL_DATE):
    row = {
        "id": f"500lq-{date}-{number}",
        "sport": "basketball",
        "league": league,
        "round": "",
        "date": date,
        "time": "08:30",
        "kickoff_time_known": True,
        "status": "upcoming",
        "home": home,
        "away": away,
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": (
            {"home_win": 1.62, "away_win": 2.35, "total_line": 224.5, "over": 1.88, "under": 1.92}
            if odds
            else None
        ),
        "jczq_no": str(number),
    }
    return row


def rehearsal_market(number, *, home, away, date=REHEARSAL_DATE, league="美国职业篮球联盟"):
    return {
        "id": f"500j-{date}-{number}",
        "sport": "basketball",
        "league": league,
        "round": str(number),
        "date": date,
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": home,
        "away": away,
        "home_rank": None,
        "away_rank": None,
        "score": None,
        "odds": {
            "home_win": 1.58,
            "away_win": 2.45,
            "total_line": 226.5,
            "over": 1.90,
            "under": 1.90,
            "matchnum": str(number),
        },
        "jczq_no": str(number),
    }


TEAMS = [
    ("印第安那步行者", "华盛顿奇才"),
    ("多伦多猛龙", "波士顿凯尔特人"),
    ("纽约尼克斯", "克利夫兰骑士"),
    ("孟菲斯灰熊", "丹佛掘金"),
    ("新奥尔良鹈鹕", "布鲁克林篮网"),
    ("芝加哥公牛", "圣安东尼奥马刺"),
    ("密尔沃基雄鹿", "洛杉矶湖人"),
    ("犹他爵士", "奥兰多魔术"),
    ("休斯敦火箭", "金州勇士"),
]


def rehearsal_batch():
    """Nine eligible NBA fixtures, three market-ineligible ones and a WNBA row."""
    live = []
    xml = {"basketball": [], "football": []}
    for offset, (home, away) in enumerate(TEAMS):
        number = 6401 + offset
        live.append(rehearsal_live(number, home=home, away=away))
        xml["basketball"].append(rehearsal_market(number, home=home, away=away))
    for offset in range(3):
        number = 6410 + offset
        home, away = f"无盘口主队{offset}", f"无盘口客队{offset}"
        live.append(rehearsal_live(number, home=home, away=away, odds=False))
    live.append(
        rehearsal_live(6501, home="拉斯维加斯王牌", away="纽约自由人", league="WNBA")
    )
    # The tracked event also arrives from the lottery market: it must not duplicate.
    xml["basketball"].append(
        rehearsal_market(6301, home="洛杉矶湖人", away="金州勇士", date=TRACKED_DATE)
    )
    return live, xml


def test_bs_25_isolated_refresh_ingests_eligible_nba_and_preserves_identity(
    isolated_data_dir, monkeypatch
):
    data_dir = write_refresh_stores(str(isolated_data_dir), [tracked_nba_row()])
    live, xml = rehearsal_batch()
    patch_refresh_sources(monkeypatch, live_basketball=live, xml=xml)

    stats = scraper.refresh(verbose=False)

    payload = json.loads(open(os.path.join(data_dir, "daily_matches.json"), encoding="utf-8").read())
    matches = payload["matches"]
    ids = [m["id"] for m in matches]

    # 1) new NBA with usable odds -> admitted; 2) without usable odds -> filtered
    assert stats["sources"]["market_rejected"] == 3
    assert stats["added"] == 10, stats
    assert len(matches) == 11
    assert len(ids) == len(set(ids)), "canonical identity must stay unique"

    # 3) already tracked event keeps its Canonical ID, even though the live alias differs
    assert TRACKED_ID in ids
    tracked = next(m for m in matches if m["id"] == TRACKED_ID)
    assert tracked["home"] == "洛杉矶湖人" and tracked["away"] == "金州勇士"

    # 4) the same established event from both sources stays a single record
    lakers = [m for m in matches if m.get("date") == TRACKED_DATE]
    assert len(lakers) == 1

    nba_today = [
        m for m in matches
        if m.get("date") == REHEARSAL_DATE and fetcher_500.is_nba_league(m.get("league"))
    ]
    numbers = sorted(competition_number_for_match(m) for m in nba_today)
    assert numbers == [str(n) for n in range(6401, 6410)]
    assert all(m.get("market_tracked") is True for m in nba_today)
    assert all(has_usable_market_odds(m) for m in nba_today), "no synthetic odds may appear"

    # 5) WNBA is stored as basketball but is never counted as NBA
    wnba = [m for m in matches if m.get("league") == "WNBA"]
    assert len(wnba) == 1
    assert fetcher_500.is_nba_league(wnba[0]["league"]) is False

    # market orientation must be untouched: the live row's home/away survive
    assert nba_today[0]["home"] in {home for home, _ in TEAMS}

    # refresh generation transitions and the completed digest set
    state = refresh_state.inspect_state(data_dir)
    assert state["error"] is None
    assert state["state"]["generation"] == 1
    assert state["state"]["status"] == refresh_state.STATUS_COMPLETED
    assert set(state["state"]["stores"]) == set(refresh_state.PROTECTED_STORES)
    assert refresh_state.consistency(data_dir)["consistent"] is True

    # prediction snapshots exist for the upcoming admitted matches and are not duplicated
    snapshots = json.loads(
        open(os.path.join(data_dir, "prediction_snapshots.json"), encoding="utf-8").read()
    )["snapshots"]
    assert len({s["match_id"] for s in snapshots.values()}) == len(snapshots)
    assert stats["prediction_snapshots_added"] == len(snapshots)


def test_bs_25b_repeated_refresh_creates_no_duplicates(isolated_data_dir, monkeypatch):
    data_dir = write_refresh_stores(str(isolated_data_dir), [tracked_nba_row()])
    live, xml = rehearsal_batch()
    patch_refresh_sources(monkeypatch, live_basketball=live, xml=xml)
    first = scraper.refresh(verbose=False)
    ids_after_first = [
        m["id"] for m in json.loads(
            open(os.path.join(data_dir, "daily_matches.json"), encoding="utf-8").read()
        )["matches"]
    ]
    snapshots_first = json.loads(
        open(os.path.join(data_dir, "prediction_snapshots.json"), encoding="utf-8").read()
    )

    second = scraper.refresh(verbose=False)

    ids_after_second = [
        m["id"] for m in json.loads(
            open(os.path.join(data_dir, "daily_matches.json"), encoding="utf-8").read()
        )["matches"]
    ]
    snapshots_second = json.loads(
        open(os.path.join(data_dir, "prediction_snapshots.json"), encoding="utf-8").read()
    )

    assert second["added"] == 0, second
    assert ids_after_first == ids_after_second, "existing Canonical IDs must be stable"
    assert second["prediction_snapshots_added"] == 0
    assert snapshots_first == snapshots_second, "immutable snapshots must not be rewritten"
    assert second["total"] == first["total"]
    assert refresh_state.inspect_state(data_dir)["state"]["generation"] == 2


def test_bs_26_ambiguous_identity_fails_closed_before_any_write(isolated_data_dir, monkeypatch):
    data_dir = write_refresh_stores(str(isolated_data_dir), [])
    before = store_hashes(data_dir)
    bridge = rehearsal_live(7003, home="甲队", away="乙队", odds=False)
    bridge.pop("jczq_no")
    ambiguous = [
        rehearsal_live(7001, home="甲队", away="乙队", odds=False),
        rehearsal_live(7002, home="甲队", away="乙队", odds=False),
        bridge,
    ]
    patch_refresh_sources(monkeypatch, live_basketball=ambiguous)

    with pytest.raises(CanonicalIdentityCollisionError) as raised:
        scraper.refresh(verbose=False)

    assert raised.value.category == "AMBIGUOUS_CANONICAL_OWNERSHIP"
    assert raised.value.reason == "non_clique_component"
    # fail closed before the first protected-store write
    assert store_hashes(data_dir) == before
    state = refresh_state.inspect_state(data_dir)["state"]
    assert state["status"] == refresh_state.STATUS_FAILED
    assert "AMBIGUOUS_CANONICAL_OWNERSHIP" in (state.get("error") or "")
    assert refresh_state.consistency(data_dir)["consistent"] is False


def test_bs_26b_strict_batch_preflight_is_documented_behavior(isolated_data_dir, monkeypatch):
    """
    A market-ineligible row is *filtered* by admission, yet the ingestion identity
    preflight runs on the whole observed batch first (AGENTS.md §4.3/§4.26).

    This test pins that contract: it is intentional, not a silent NBA-ingestion
    defect. Narrowing the guard to the admitted subset would drop the
    ``SAME_SOURCE_ALIAS_DIFFERENT_EVENT`` signal for unfiltered rows, so the
    strict batch semantics are preserved.
    """
    from utils.ingestion_identity import validate_ingestion_identity

    orphan = {"sport": "basketball", "league": "NBA", "date": "2030-05-03", "home": "孤儿主队"}
    # The row would never be admitted: no usable odds, no identity.
    admitted, rejected = scraper._admit_market_candidates([dict(orphan)], existing=[])
    assert admitted == [] and rejected == 1

    with pytest.raises(CanonicalIdentityCollisionError) as raised:
        validate_ingestion_identity([], [orphan])
    assert raised.value.category == "INADEQUATE_EVENT_IDENTITY"

    data_dir = write_refresh_stores(str(isolated_data_dir), [])
    before = store_hashes(data_dir)
    patch_refresh_sources(monkeypatch, live_basketball=[orphan])
    with pytest.raises(CanonicalIdentityCollisionError):
        scraper.refresh(verbose=False)
    assert store_hashes(data_dir) == before


def test_bs_27_source_fetch_failure_is_reported_and_does_not_block_other_source(
    isolated_data_dir, monkeypatch
):
    data_dir = write_refresh_stores(str(isolated_data_dir), [])
    live, xml = rehearsal_batch()

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", boom)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport="football": [])
    monkeypatch.setattr(
        fetcher_500, "fetch_jczq_xml",
        lambda sport="football": [
            r for r in xml.get(sport, [])
            if competition_number_for_match(r) != "6301"
        ],
    )
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda date=None: [])

    stats = scraper.refresh(verbose=False)

    assert str(stats["sources"]["live_basketball"]).startswith("error:")
    assert stats["added"] == 9, stats
    matches = json.loads(
        open(os.path.join(data_dir, "daily_matches.json"), encoding="utf-8").read()
    )["matches"]
    assert len(matches) == 9


def test_bs_28_concurrent_refresh_is_rejected_by_the_existing_lock(
    isolated_data_dir, monkeypatch
):
    from utils.concurrency import acquire_refresh_lock

    data_dir = write_refresh_stores(str(isolated_data_dir), [tracked_nba_row()])
    before = store_hashes(data_dir)
    live, xml = rehearsal_batch()
    patch_refresh_sources(monkeypatch, live_basketball=live, xml=xml)

    with acquire_refresh_lock(action="rehearsal_holder"):
        with pytest.raises(RefreshBusyError):
            scraper.refresh(verbose=False)

    assert store_hashes(data_dir) == before, "a rejected writer must not touch any store"


def test_bs_29_interrupted_refresh_is_not_reported_as_atomic(isolated_data_dir, monkeypatch):
    data_dir = write_refresh_stores(str(isolated_data_dir), [tracked_nba_row()])
    before = store_hashes(data_dir)
    live, xml = rehearsal_batch()
    patch_refresh_sources(monkeypatch, live_basketball=live, xml=xml)

    def explode(*args, **kwargs):
        raise RuntimeError("interrupted during odds history capture")

    monkeypatch.setattr(scraper, "_capture_odds_history", explode)

    with pytest.raises(RuntimeError):
        scraper.refresh(verbose=False)

    after = store_hashes(data_dir)
    # The canonical store is written last, so it must not have been replaced.
    assert after["daily_matches.json"] == before["daily_matches.json"]
    state = refresh_state.inspect_state(data_dir)["state"]
    assert state["status"] == refresh_state.STATUS_FAILED
    assert "interrupted during odds history capture" in (state.get("error") or "")
    consistency = refresh_state.consistency(data_dir)
    assert consistency["consistent"] is False
    assert consistency["reason"] == refresh_state.REASON_FAILED
    assert consistency["seven_file_atomicity"] is False


def test_bs_30_settlement_lifecycle_is_append_only(isolated_data_dir, monkeypatch):
    data_dir = write_refresh_stores(str(isolated_data_dir), [])
    upcoming = rehearsal_live(6601, home="波士顿凯尔特人", away="迈阿密热火")
    patch_refresh_sources(monkeypatch, live_basketball=[upcoming])
    first = scraper.refresh(verbose=False)
    assert first["prediction_snapshots_added"] == 1

    finished = dict(upcoming, status="finished", score={"ft": [101, 98]})
    patch_refresh_sources(monkeypatch, live_basketball=[finished])
    second = scraper.refresh(verbose=False)
    assert second["settlements_added"] == 1
    assert second["evaluation_rows_added"] == 1
    settlements_after_second = store_hashes(data_dir)["settlements.json"]
    rows_after_second = store_hashes(data_dir)["evaluation_rows.json"]
    snapshots_after_second = store_hashes(data_dir)["prediction_snapshots.json"]

    third = scraper.refresh(verbose=False)
    assert third["settlements_added"] == 0, "an already settled match must not settle twice"
    assert third["evaluation_rows_added"] == 0
    assert store_hashes(data_dir)["settlements.json"] == settlements_after_second
    assert store_hashes(data_dir)["evaluation_rows.json"] == rows_after_second
    assert store_hashes(data_dir)["prediction_snapshots.json"] == snapshots_after_second

    settled = json.loads(
        open(os.path.join(data_dir, "settlements.json"), encoding="utf-8").read()
    )["settlements"]
    assert len(settled) == 1
    settlement = next(iter(settled.values()))
    assert settlement["final_score"] == {"home": 101, "away": 98}
    assert settlement["actual_outcome"] == "home_win"

    calibrated = json.loads(
        open(os.path.join(data_dir, "calibrated.json"), encoding="utf-8").read()
    )["ids"]
    assert calibrated == [upcoming["id"]]


def test_bs_31_nba_coverage_is_unknown_without_a_current_observation(isolated_data_dir):
    from app import build_nba_coverage

    _clear_caches()
    coverage = build_nba_coverage([], "2026-11-11T09:51:47+08:00")
    assert coverage["coverage_status"] == "UNKNOWN"
    assert coverage["coverage_reason"] == "SOURCE_NOT_OBSERVED"
    # "no observation" must never be reported as "zero missing fixtures"
    assert coverage["source_fixtures_not_yet_ingested"] is None
    assert coverage["market_ineligible_fixtures"] is None
    assert coverage["post_refresh_ingestion_verified"] is False
    assert coverage["current_source_observation"]["state"] == "NOT_OBSERVED"


def test_bs_32_nba_coverage_separates_ingested_pending_and_ineligible(isolated_data_dir):
    from app import build_nba_coverage

    _clear_caches()
    stored = [tracked_nba_row()]
    live_rows = [
        rehearsal_live(6301, home="洛杉矶湖人", away="金州勇士", date=TRACKED_DATE),
        rehearsal_live(6401, home="印第安那步行者", away="华盛顿奇才"),
        rehearsal_live(6410, home="无盘口主队", away="无盘口客队", odds=False),
        rehearsal_live(6501, home="拉斯维加斯王牌", away="纽约自由人", league="WNBA"),
    ]
    xml_rows = [rehearsal_market(6401, home="印第安那步行者", away="华盛顿奇才")]

    # Inject the exact same observation shape a real fetch produces.
    fetcher_500._record_diag("live_basketball", "ok", rows=len(live_rows), nba_rows=3, valid_market_rows=2)
    fetcher_500._record_observation("live_basketball", live_rows)
    fetcher_500._record_diag("jczq_odds_basketball", "ok", rows=len(xml_rows), nba_rows=1, valid_market_rows=1)
    fetcher_500._record_observation("jczq_odds_basketball", xml_rows)

    coverage = build_nba_coverage(stored, "2020-01-01T00:00:00+08:00")

    assert coverage["coverage_status"] == "KNOWN"
    assert coverage["coverage_reason"] is None
    # The tracked event is already ingested and is not counted as a gap.
    assert coverage["already_ingested_fixtures"] == 1
    # 6401 appears in both sources but is one unresolved event; 6410 has no market.
    assert coverage["source_fixtures_not_yet_ingested"] == 1
    assert coverage["market_ineligible_fixtures"] == 1
    # WNBA is excluded from the NBA figure but stays visible as non-NBA basketball.
    assert coverage["source_nba_fixtures_observed"] == 4
    assert coverage["non_nba_basketball_fixtures"] == 1
    assert coverage["post_refresh_ingestion_verified"] is False

    # Once every eligible fixture is stored, ingestion is verified.
    everything = stored + [
        rehearsal_live(6401, home="印第安那步行者", away="华盛顿奇才"),
    ]
    complete = build_nba_coverage(everything, "2020-01-01T00:00:00+08:00")
    assert complete["coverage_status"] == "KNOWN"
    assert complete["source_fixtures_not_yet_ingested"] == 0
    assert complete["post_refresh_ingestion_verified"] is True


def test_bs_33_real_fetch_wiring_and_stale_observation(isolated_data_dir, monkeypatch):
    from app import build_nba_coverage

    _clear_caches()
    _patch_live(monkeypatch, NBA_MATCHLIST_HTML)
    _patch_xml(monkeypatch, {"sf": JCLQ_SF_XML, "dxf": JCLQ_DXF_XML})
    assert fetcher_500.fetch_live_basketball()
    assert fetcher_500.fetch_jczq_xml("basketball")

    # The real adapters retained the parsed rows (no cache hit involved).
    observed = fetcher_500.last_source_observation()
    assert set(observed) == {"live_basketball", "jczq_odds_basketball"}
    assert observed["live_basketball"]["status"] == "ok"
    assert observed["live_basketball"]["checked_at"]

    coverage = build_nba_coverage([], "2020-01-01T00:00:00+08:00")
    assert coverage["coverage_status"] == "KNOWN"
    # 6308 (both sources) is one pending event; 6309 has no usable market.
    assert coverage["source_fixtures_not_yet_ingested"] == 1
    assert coverage["market_ineligible_fixtures"] == 1
    assert coverage["post_refresh_ingestion_verified"] is False

    # A dataset refreshed after the observation makes coverage undecidable.
    stale = build_nba_coverage([], "2030-01-01T00:00:00+08:00")
    assert stale["coverage_status"] == "UNKNOWN"
    assert stale["coverage_reason"] == "OBSERVATION_PREDATES_LAST_REFRESH"
    assert stale["source_fixtures_not_yet_ingested"] is None

    # A cache hit must not move the observation forward either.
    first = fetcher_500.last_source_observation()["live_basketball"]["checked_at"]
    fetcher_500.fetch_live_basketball()
    assert fetcher_500.last_source_observation()["live_basketball"]["checked_at"] == first


def test_bs_34_empty_source_is_not_reported_as_healthy_coverage(isolated_data_dir, monkeypatch):
    from app import build_nba_coverage

    _clear_caches()
    _patch_live(monkeypatch, BASKETBALL_EMPTY_SHELL_HTML)
    _patch_xml(monkeypatch, {"sf": JCLQ_SF_EMPTY_XML, "dxf": JCLQ_SF_EMPTY_XML})
    assert fetcher_500.fetch_live_basketball() == []
    assert fetcher_500.fetch_jczq_xml("basketball") == []

    coverage = build_nba_coverage([], "2020-01-01T00:00:00+08:00")
    assert coverage["coverage_status"] == "UNKNOWN"
    assert coverage["coverage_reason"] == "SOURCE_NOT_HEALTHY"
    assert coverage["source_nba_fixtures_observed"] is None
    assert coverage["post_refresh_ingestion_verified"] is False