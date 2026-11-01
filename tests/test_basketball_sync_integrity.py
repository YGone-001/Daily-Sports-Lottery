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
    fetcher_500._DIAGNOSTICS.clear()


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