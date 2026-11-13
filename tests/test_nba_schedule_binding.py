"""Phase 3-O: NBA 赛程时间补全、UTC/北京时转换、严格一对一绑定与赛前预测闭环测试。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

import app as flask_app
import config
from utils import daily_loader, fetcher_500, match_identity, nba_schedule, odds_snapshots, prediction_snapshots, scraper
from utils.daily_loader import BEIJING_TZ, enrich_match, get_match_datetime
from utils.match_identity import build_kickoff_time_provenance


def _store_hashes(data_dir: str) -> dict[str, str]:
    out = {}
    for fn in (
        "daily_matches.json",
        "team_strength.json",
        "calibrated.json",
        "prediction_snapshots.json",
        "odds_snapshots.json",
        "settlements.json",
        "evaluation_rows.json",
    ):
        fp = os.path.join(data_dir, fn)
        if os.path.exists(fp):
            with open(fp, "rb") as fh:
                out[fn] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _espn_payload(events: list[dict], league_uid: str = "s:40~l:46", league_abbr: str = "NBA") -> dict:
    return {
        "leagues": [
            {
                "uid": league_uid,
                "abbreviation": league_abbr,
                "season": {"year": 2027, "displayName": "2026-27"},
            }
        ],
        "events": events,
    }


def _espn_event(
    gid: str = "401909976",
    utc: str = "2026-11-13T00:00Z",
    status_name: str = "STATUS_SCHEDULED",
    home_id: str = "20",
    home_abbr: str = "PHI",
    home_name: str = "Philadelphia 76ers",
    away_id: str = "28",
    away_abbr: str = "TOR",
    away_name: str = "Toronto Raptors",
    uid: str = "s:40~l:46~e:401909976",
) -> dict:
    return {
        "id": gid,
        "uid": uid,
        "date": utc,
        "season": {"year": 2027, "type": 2},
        "status": {"type": {"name": status_name}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"id": home_id, "abbreviation": home_abbr, "displayName": home_name},
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": away_id, "abbreviation": away_abbr, "displayName": away_name},
                    },
                ]
            }
        ],
    }


def _official_schedule_payload(games: list[dict], league_id: str = "00") -> dict:
    return {
        "leagueSchedule": {
            "seasonYear": "2026-27",
            "leagueId": league_id,
            "gameDates": [{"gameDate": "11/12/2026", "games": games}],
        }
    }


def _canonical_500j(
    mid: str = "500j-2030-05-15-4306",
    date: str = "2030-05-15",
    time: str = "00:00",
    kickoff_time_known: bool = False,
    status: str = "upcoming",
    home: str = "费城76人",
    away: str = "多伦多猛龙",
    league: str = "美国职业篮球联盟",
    jczq_no: str = "4306",
    with_odds: bool = True,
) -> dict:
    m = {
        "id": mid,
        "sport": "basketball",
        "league": league,
        "round": jczq_no,
        "date": date,
        "time": time,
        "kickoff_time_known": kickoff_time_known,
        "status": status,
        "home": home,
        "away": away,
        "home_rank": None,
        "away_rank": None,
        "score": {"ft": [110, 102]} if status == "finished" else None,
        "odds": {"home_win": 1.68, "away_win": 2.12, "matchnum": jczq_no} if with_odds else None,
        "jczq_no": jczq_no,
        "market_tracked": True,
    }
    return m


# ---------------------------------------------------------------------------
# 1. UTC-to-Beijing conversion & parser contracts (Workstream A)
# ---------------------------------------------------------------------------


def test_utc_to_beijing_conversion_boundaries():
    # 1) UTC -> Beijing date transition
    assert nba_schedule.convert_utc_to_beijing("2026-11-12T23:30Z")[:2] == ("2026-11-13", "07:30")
    # 2) Explicit midnight UTC -> Beijing 08:00
    assert nba_schedule.convert_utc_to_beijing("2026-11-13T00:00Z")[:2] == ("2026-11-13", "08:00")
    # 3) Explicit Beijing midnight (16:00Z)
    assert nba_schedule.convert_utc_to_beijing("2026-11-12T16:00Z")[:2] == ("2026-11-13", "00:00")
    # 4) US DST spring-forward & fall-back: explicit UTC invariant
    assert nba_schedule.convert_utc_to_beijing("2026-03-08T00:30:00Z")[:2] == ("2026-03-08", "08:30")
    assert nba_schedule.convert_utc_to_beijing("2026-11-01T01:00:00+00:00")[:2] == ("2026-11-01", "09:00")
    # 5) Leap day valid vs non-leap invalid & year transition
    assert nba_schedule.convert_utc_to_beijing("2028-02-29T16:30Z")[:2] == ("2028-03-01", "00:30")
    assert nba_schedule.convert_utc_to_beijing("2026-02-29T00:00Z")[3] == "INVALID_UTC"
    assert nba_schedule.convert_utc_to_beijing("2026-12-31T20:00Z")[:2] == ("2027-01-01", "04:00")
    # 6) Missing timezone offset -> INVALID_UTC
    assert nba_schedule.convert_utc_to_beijing("2026-11-13T08:00:00")[3] == "INVALID_UTC"
    assert nba_schedule.convert_utc_to_beijing("2026-11-13 08:00")[3] == "INVALID_UTC"
    # 7) Missing / malformed values
    assert nba_schedule.convert_utc_to_beijing("")[3] == "MISSING_UTC"
    assert nba_schedule.convert_utc_to_beijing(None)[3] == "MISSING_UTC"
    assert nba_schedule.convert_utc_to_beijing("2026-11-13")[3] == "INVALID_UTC"
    assert nba_schedule.convert_utc_to_beijing("TBD")[3] == "INVALID_UTC"


def test_parse_espn_and_nba_official_schedule_schemas_and_wnba_isolation():
    obs_time = datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ)
    espn_data = _espn_payload([
        _espn_event(gid="401909976", utc="2026-11-13T00:00Z", status_name="STATUS_SCHEDULED"),
        _espn_event(gid="401999999", utc="2026-11-13T01:00Z", status_name="STATUS_SCHEDULED", uid="s:40~l:59~e:401999999"),
        _espn_event(gid="401909980", utc="2026-11-13T02:00Z", status_name="STATUS_POSTPONED"),
    ])
    rows, errs = nba_schedule.parse_espn_nba_scoreboard(espn_data, observed_at=obs_time)
    assert errs == []
    assert [r["provider_game_id"] for r in rows] == ["401909976", "401909980"]
    assert rows[0]["beijing_date"] == "2026-11-13"
    assert rows[0]["beijing_time"] == "08:00"
    assert rows[0]["scheduled_start_utc"] == "2026-11-13T00:00Z"
    assert rows[0]["observed_at"] != rows[0]["scheduled_start_utc"]
    assert rows[1]["game_status"] == "postponed"

    # Official scheduleLeagueV2 schema: leading zeros preserved, WNBA leagueId='10' rejected
    official = _official_schedule_payload([
        {
            "gameId": "0022600153",
            "gameStatus": 1,
            "gameStatusText": "7:00 pm ET",
            "postponedStatus": "A",
            "gameDateTimeUTC": "2026-11-13T00:00:00Z",
            "homeTeam": {"teamId": 1610612755, "teamCity": "Philadelphia", "teamName": "76ers", "teamTricode": "PHI"},
            "awayTeam": {"teamId": 1610612761, "teamCity": "Toronto", "teamName": "Raptors", "teamTricode": "TOR"},
        }
    ])
    off_rows, off_errs = nba_schedule.parse_nba_official_schedule(official, observed_at=obs_time)
    assert off_errs == []
    assert len(off_rows) == 1
    assert off_rows[0]["provider_game_id"] == "0022600153"  # leading zeros preserved
    assert off_rows[0]["home_team_identifier"] == "nba:1610612755"
    assert off_rows[0]["beijing_time"] == "08:00"
    assert off_rows[0]["game_status"] == "postponed"  # postponedStatus='A' overrides 1

    wnba_official = _official_schedule_payload(official["leagueSchedule"]["gameDates"][0]["games"], league_id="10")
    w_rows, w_errs = nba_schedule.parse_nba_official_schedule(wnba_official)
    assert w_rows == [] and "NON_NBA_LEAGUE_ID" in w_errs


def test_fetch_nba_schedule_unavailable_and_non_near_term(monkeypatch):
    nba_schedule.clear_schedule_cache()

    # Future year synthetic date avoids outbound HTTP completely
    far_rows, far_diag = nba_schedule.fetch_nba_schedule_observations(["2030-05-15"], now=datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ))
    assert far_rows == []
    assert far_diag["note"] == "NO_NEAR_TERM_DATES"

    class _FailResp:
        status_code = 403

    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **kw: _FailResp())
    rows, diag = nba_schedule.fetch_nba_schedule_observations(
        ["2026-11-13"],
        now=datetime(2026, 11, 12, 18, 0, tzinfo=BEIJING_TZ),
        allow_network=True,
        provider=nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD,
    )
    assert rows == []
    assert diag["status"] == "unavailable"
    nba_schedule.clear_schedule_cache()


# ---------------------------------------------------------------------------
# 2. Explicit 30-team mapping & 12 fail-closed binding checks (Workstream B)
# ---------------------------------------------------------------------------


def test_30_franchise_mapping_completeness_and_no_fuzzy_matching():
    codes = [e.canonical_code for e in nba_schedule.NBA_FRANCHISE_MAPPINGS]
    espn_ids = [e.espn_team_id for e in nba_schedule.NBA_FRANCHISE_MAPPINGS]
    nba_ids = [e.nba_team_id for e in nba_schedule.NBA_FRANCHISE_MAPPINGS]
    assert len(codes) == 30 and len(set(codes)) == 30
    assert len(set(espn_ids)) == 30
    assert len(set(nba_ids)) == 30
    # Strict exact names only — truncated / noisy / non-NBA rejected
    assert nba_schedule.resolve_canonical_team_code("费城76人") == "PHI"
    assert nba_schedule.resolve_canonical_team_code("俄克拉荷马城雷霆") == "OKC"
    assert nba_schedule.resolve_canonical_team_code("俄克拉荷马雷霆") == "OKC"
    assert nba_schedule.resolve_canonical_team_code("费城76") is None
    assert nba_schedule.resolve_canonical_team_code("76ers") is None
    assert nba_schedule.resolve_canonical_team_code("上海大鲨鱼") is None


def test_strict_binding_all_12_rejection_reasons():
    now = datetime(2030, 5, 14, 15, 0, tzinfo=BEIJING_TZ)
    base_can = _canonical_500j(date="2030-05-15")
    base_obs, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(gid="401909976", utc="2030-05-15T00:00Z")]),
        observed_at=now,
    )

    # 1. OFFICIAL_SCHEDULE_UNAVAILABLE
    r1 = nba_schedule.bind_schedule_observations_to_canonical([base_can], [], now=now, schedule_available=False)
    assert r1["decisions"][0]["reason"] == nba_schedule.REASON_OFFICIAL_SCHEDULE_UNAVAILABLE

    # 2. OFFICIAL_GAME_NOT_FOUND
    r2 = nba_schedule.bind_schedule_observations_to_canonical([base_can], [], now=now, schedule_available=True)
    assert r2["decisions"][0]["reason"] == nba_schedule.REASON_OFFICIAL_GAME_NOT_FOUND

    # 3. OFFICIAL_GAME_TIME_MISSING
    obs_missing, _ = nba_schedule.parse_espn_nba_scoreboard(_espn_payload([_espn_event(utc="")]), observed_at=now)
    r3 = nba_schedule.bind_schedule_observations_to_canonical([base_can], obs_missing, now=now)
    assert r3["decisions"][0]["reason"] == nba_schedule.REASON_OFFICIAL_GAME_TIME_MISSING

    # 4. OFFICIAL_GAME_TIME_INVALID
    obs_inv, _ = nba_schedule.parse_espn_nba_scoreboard(_espn_payload([_espn_event(utc="2030-05-15T08:00:00")]), observed_at=now)
    r4 = nba_schedule.bind_schedule_observations_to_canonical([base_can], obs_inv, now=now)
    assert r4["decisions"][0]["reason"] == nba_schedule.REASON_OFFICIAL_GAME_TIME_INVALID

    # 5. OFFICIAL_GAME_NOT_SCHEDULED (postponed / canceled / live / final)
    for st in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_IN_PROGRESS", "STATUS_FINAL"):
        obs_st, _ = nba_schedule.parse_espn_nba_scoreboard(_espn_payload([_espn_event(utc="2030-05-15T00:00Z", status_name=st)]), observed_at=now)
        r5 = nba_schedule.bind_schedule_observations_to_canonical([base_can], obs_st, now=now)
        assert r5["decisions"][0]["reason"] == nba_schedule.REASON_OFFICIAL_GAME_NOT_SCHEDULED

    # 6. TEAM_ID_MAPPING_UNVERIFIED
    bad_team_can = _canonical_500j(home="未知篮球队")
    r6 = nba_schedule.bind_schedule_observations_to_canonical([bad_team_can], base_obs, now=now)
    assert r6["decisions"][0]["reason"] == nba_schedule.REASON_TEAM_ID_MAPPING_UNVERIFIED

    # 7. HOME_AWAY_ORIENTATION_CONFLICT (official home/away reversed -> never swap!)
    rev_obs, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(home_id="28", home_abbr="TOR", away_id="20", away_abbr="PHI", utc="2030-05-15T00:00Z")]),
        observed_at=now,
    )
    r7 = nba_schedule.bind_schedule_observations_to_canonical([base_can], rev_obs, now=now)
    assert r7["decisions"][0]["reason"] == nba_schedule.REASON_HOME_AWAY_ORIENTATION_CONFLICT

    # 8. BEIJING_EVENT_DATE_MISMATCH
    mismatch_obs, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(utc="2030-05-14T15:30Z")]),  # 2030-05-14 23:30 Beijing
        observed_at=now,
    )
    r8 = nba_schedule.bind_schedule_observations_to_canonical([base_can], mismatch_obs, now=now)
    assert r8["decisions"][0]["reason"] == nba_schedule.REASON_BEIJING_EVENT_DATE_MISMATCH

    # 9. MULTIPLE_OFFICIAL_CANDIDATES (two official games same date & teams)
    dup_obs, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([
            _espn_event(gid="401909976", utc="2030-05-15T00:00Z"),
            _espn_event(gid="401909999", utc="2030-05-15T03:00Z"),
        ]),
        observed_at=now,
    )
    r9 = nba_schedule.bind_schedule_observations_to_canonical([base_can], dup_obs, now=now)
    assert r9["decisions"][0]["reason"] == nba_schedule.REASON_MULTIPLE_OFFICIAL_CANDIDATES

    # 10. MULTIPLE_CANONICAL_CANDIDATES
    dup_can = [_canonical_500j(mid="500j-2030-05-15-4306"), _canonical_500j(mid="500j-2030-05-15-4399", jczq_no="4399")]
    r10 = nba_schedule.bind_schedule_observations_to_canonical(dup_can, base_obs, now=now)
    assert all(d["reason"] == nba_schedule.REASON_MULTIPLE_CANONICAL_CANDIDATES for d in r10["decisions"])

    # 11. TIME_PROVENANCE_CONFLICT (Canonical already had another known time without matching official ID)
    conflict_can = _canonical_500j(time="09:30", kickoff_time_known=True)
    r11 = nba_schedule.bind_schedule_observations_to_canonical([conflict_can], base_obs, now=now)
    assert r11["decisions"][0]["reason"] == nba_schedule.REASON_TIME_PROVENANCE_CONFLICT

    # 12. GAME_ALREADY_STARTED
    late_now = datetime(2030, 5, 15, 8, 0, tzinfo=BEIJING_TZ)
    r12 = nba_schedule.bind_schedule_observations_to_canonical([base_can], base_obs, now=late_now)
    assert r12["decisions"][0]["reason"] == nba_schedule.REASON_GAME_ALREADY_STARTED


# ---------------------------------------------------------------------------
# 3. Provenance Attribution & Anti-Laundering (Workstream C)
# ---------------------------------------------------------------------------


def test_kickoff_provenance_distinction_and_anti_laundering():
    legacy = _canonical_500j()
    p_leg = build_kickoff_time_provenance(legacy)
    assert p_leg["canonical_origin_source"] == "500j"
    assert p_leg["actual_kickoff_time_source"] == "NOT_ESTABLISHED"
    assert p_leg["kickoff_verification_status"] == "NOT_ESTABLISHED"
    assert p_leg["kickoff_time_source"] == "NOT_ESTABLISHED"

    # Manually setting kickoff_time_known=True or forging OFFICIAL_NBA_VERIFIED without valid source fails closed
    forged = _canonical_500j(time="08:00", kickoff_time_known=True)
    forged["kickoff_provenance"] = {
        "actual_kickoff_time_source": "espn_nba_scoreboard",
        "kickoff_source_event_id": "401909976",
        "kickoff_source_observed_at": "2030-05-14T15:00:00+08:00",
        "kickoff_source_scheduled_start_utc": "2030-05-15T00:00Z",
        "kickoff_verification_status": "OFFICIAL_NBA_VERIFIED",
    }
    p_forged = build_kickoff_time_provenance(forged)
    assert p_forged["kickoff_verification_status"] == "UNVERIFIED_PROVENANCE_CONFLICT"

    # Authorized official NBA CDN attribution succeeds with OFFICIAL_NBA_VERIFIED
    now = datetime(2030, 5, 14, 15, 0, tzinfo=BEIJING_TZ)
    off_obs, _ = nba_schedule.parse_nba_official_schedule(
        _official_schedule_payload([
            {
                "gameId": "0022600153",
                "gameStatus": 1,
                "gameDateTimeUTC": "2030-05-15T00:00:00Z",
                "homeTeam": {"teamId": 1610612755, "teamCity": "Philadelphia", "teamName": "76ers", "teamTricode": "PHI"},
                "awayTeam": {"teamId": 1610612761, "teamCity": "Toronto", "teamName": "Raptors", "teamTricode": "TOR"},
            }
        ]),
        observed_at=now,
    )
    m = deepcopy(legacy)
    nba_schedule.apply_nba_schedule_time_enrichment([m], off_obs, now=now)
    p_off = build_kickoff_time_provenance(m)
    assert p_off["canonical_origin_source"] == "500j"
    assert p_off["actual_kickoff_time_source"] == "nba_official_schedule"
    assert p_off["kickoff_source_event_id"] == "0022600153"
    assert p_off["kickoff_verification_status"] == "OFFICIAL_NBA_VERIFIED"
    assert p_off["kickoff_time_source"] == "nba_official_schedule"


# ---------------------------------------------------------------------------
# 4. Time-Only Enrichment Invariants & Reschedule Preservation (Workstream D)
# ---------------------------------------------------------------------------


def test_time_only_enrichment_invariants_and_reschedule_lifecycle(isolated_data_dir):
    now = datetime(2030, 5, 14, 15, 0, tzinfo=BEIJING_TZ)
    m = _canonical_500j(date="2030-05-15")
    wnba = {
        "id": "500j-2030-05-15-9901",
        "sport": "basketball",
        "league": "WNBA",
        "date": "2030-05-15",
        "time": "00:00",
        "kickoff_time_known": False,
        "status": "upcoming",
        "home": "费城76人",
        "away": "多伦多猛龙",
        "odds": {"home_win": 1.80, "away_win": 2.00, "matchnum": "9901"},
        "jczq_no": "9901",
    }
    before_wnba = deepcopy(wnba)

    obs1, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(gid="401909976", utc="2030-05-15T00:00Z")]),
        observed_at=now,
    )
    r1 = nba_schedule.apply_nba_schedule_time_enrichment([m, wnba], obs1, now=now)
    assert r1["enriched"] == 1
    assert wnba == before_wnba  # WNBA untouched
    assert m["id"] == "500j-2030-05-15-4306"
    assert m["jczq_no"] == "4306"
    assert m["home"] == "费城76人" and m["away"] == "多伦多猛龙"
    assert m["odds"] == {"home_win": 1.68, "away_win": 2.12, "matchnum": "4306"}
    assert m["time"] == "08:00" and m["kickoff_time_known"] is True

    # Capture immutable pregame prediction snapshot at original 08:00 kickoff
    snap, created = prediction_snapshots.capture_snapshot(enrich_match(dict(m)), now=now)
    assert created is True and snap is not None

    # Official reschedule moves start time to 09:30 Beijing -> allowed before kickoff, snapshot preserved!
    now2 = datetime(2030, 5, 14, 18, 0, tzinfo=BEIJING_TZ)
    obs2, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(gid="401909976", utc="2030-05-15T01:30Z")]),
        observed_at=now2,
    )
    r2 = nba_schedule.apply_nba_schedule_time_enrichment([m], obs2, now=now2)
    assert r2["rescheduled"] == 1
    assert m["time"] == "09:30"
    assert r2["decisions"][0]["change_classification"] == nba_schedule.CHANGE_OFFICIAL_RESCHEDULE_CONFIRMED
    # Existing prediction snapshot remains byte-immutable!
    snaps_after = prediction_snapshots.get_snapshots_for_match("500j-2030-05-15-4306")
    assert len(snaps_after) == 1
    assert snaps_after[0]["snapshot_id"] == snap["snapshot_id"]
    assert snaps_after[0]["generated_at"] == snap["generated_at"]
    assert snaps_after[0]["match_time"] == "08:00"

    # Unchanged second observation -> unchanged == 1
    r3 = nba_schedule.apply_nba_schedule_time_enrichment([m], obs2, now=now2)
    assert r3["unchanged"] == 1


# ---------------------------------------------------------------------------
# 5. Isolated End-to-End Refresh Closure (Workstream E)
# ---------------------------------------------------------------------------


def test_isolated_end_to_end_refresh_enriches_and_captures_prediction_snapshot(isolated_data_dir, monkeypatch):
    now = datetime(2030, 5, 14, 15, 0, tzinfo=BEIJING_TZ)
    market_only_500j = _canonical_500j(date="2030-05-15", time="00:00", kickoff_time_known=False)
    # Also include finished match without snapshot -> never backfills historical snapshot
    finished_500j = _canonical_500j(
        mid="500j-2030-05-13-4301",
        date="2030-05-13",
        time="00:00",
        kickoff_time_known=False,
        status="finished",
        home="波士顿凯尔特人",
        away="纽约尼克斯",
        jczq_no="4301",
    )

    monkeypatch.setattr(scraper, "get_beijing_now", lambda: now)
    monkeypatch.setattr(fetcher_500, "fetch_live_matches", lambda sport: [])
    monkeypatch.setattr(fetcher_500, "fetch_live_basketball", lambda: [])
    monkeypatch.setattr(fetcher_500, "fetch_finished_matches", lambda: [])
    monkeypatch.setattr(
        fetcher_500,
        "fetch_jczq_xml",
        lambda sport: [deepcopy(market_only_500j), deepcopy(finished_500j)] if sport == "basketball" else [],
    )
    obs, _ = nba_schedule.parse_espn_nba_scoreboard(
        _espn_payload([_espn_event(gid="401909976", utc="2030-05-15T00:00Z")]),
        observed_at=now,
    )
    monkeypatch.setattr(
        nba_schedule,
        "fetch_nba_schedule_observations",
        lambda dates, **kw: (obs, {"provider": nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD, "status": "ok", "rows": len(obs)}),
    )

    res = scraper.refresh(verbose=False)
    assert res["nba_schedule_enriched"] == 1
    assert res["prediction_snapshots_added"] == 1

    with open(os.path.join(str(isolated_data_dir), "daily_matches.json"), "r", encoding="utf-8") as fh:
        stored_matches = {m["id"]: m for m in json.load(fh)["matches"]}

    enriched = stored_matches["500j-2030-05-15-4306"]
    assert enriched["id"] == "500j-2030-05-15-4306"
    assert enriched["jczq_no"] == "4306"
    assert enriched["time"] == "08:00"
    assert enriched["kickoff_time_known"] is True
    assert get_match_datetime(enriched) == datetime(2030, 5, 15, 8, 0, tzinfo=BEIJING_TZ)

    prov = build_kickoff_time_provenance(enriched)
    assert prov["canonical_origin_source"] == "500j"
    assert prov["actual_kickoff_time_source"] == nba_schedule.PROVIDER_ESPN_NBA_SCOREBOARD
    assert prov["kickoff_source_event_id"] == "401909976"
    assert prov["kickoff_verification_status"] == "SCHEDULE_PROVIDER_VERIFIED"

    snaps = prediction_snapshots.get_snapshots_for_match("500j-2030-05-15-4306")
    assert len(snaps) == 1
    assert datetime.fromisoformat(snaps[0]["generated_at"]) < get_match_datetime(enriched)
    assert prediction_snapshots.get_snapshots_for_match("500j-2030-05-13-4301") == []

    # Second refresh idempotent: zero new snapshots created
    res2 = scraper.refresh(verbose=False)
    assert res2["prediction_snapshots_added"] == 0


# ---------------------------------------------------------------------------
# 6. Stale Stored Lifecycle Diagnostics & Read-Only Contract (Workstream F)
# ---------------------------------------------------------------------------


def test_effective_lifecycle_diagnostics_stale_vs_verified_and_readonly(isolated_data_dir):
    now = datetime(2030, 5, 10, 14, 0, tzinfo=BEIJING_TZ)

    m_future_unmat = _canonical_500j(mid="500j-2030-05-11-4306", date="2030-05-11", time="08:00", kickoff_time_known=True)
    m_future_mat = _canonical_500j(mid="500j-2030-05-11-4307", date="2030-05-11", time="09:00", kickoff_time_known=True)
    m_unknown_today = _canonical_500j(mid="500j-2030-05-10-4308", date="2030-05-10", time="00:00", kickoff_time_known=False)
    m_recent_past = _canonical_500j(mid="500lq-2030-05-10-4309", date="2030-05-10", time="10:00", kickoff_time_known=True)
    m_stale_unknown = _canonical_500j(mid="500j-2030-05-08-4301", date="2030-05-08", time="00:00", kickoff_time_known=False)
    m_stale_known = _canonical_500j(mid="500lq-2030-05-08-4302", date="2030-05-08", time="08:00", kickoff_time_known=True)
    m_live = _canonical_500j(mid="500lq-2030-05-10-4310", date="2030-05-10", time="13:00", kickoff_time_known=True, status="live")
    m_fin = _canonical_500j(mid="500lq-2030-05-09-4311", date="2030-05-09", time="08:00", kickoff_time_known=True, status="finished")
    m_unknown_st = _canonical_500j(mid="500lq-2030-05-10-4312", date="2030-05-10", time="16:00", kickoff_time_known=True, status="cancelled")

    rows = [m_future_unmat, m_future_mat, m_unknown_today, m_recent_past, m_stale_unknown, m_stale_known, m_live, m_fin, m_unknown_st]
    with open(os.path.join(str(isolated_data_dir), "daily_matches.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": {}, "matches": rows}, fh, ensure_ascii=False)

    snap, created = prediction_snapshots.capture_snapshot(enrich_match(dict(m_future_mat)), now=now)
    assert created and snap is not None
    before = _store_hashes(str(isolated_data_dir))

    report = flask_app.build_nba_pregame_readiness(rows, now=now)
    assert _store_hashes(str(isolated_data_dir)) == before

    assert report["read_only"] is True
    assert report["historical_status_stale_count"] == 2
    assert report["prediction_capture_eligible_now_count"] == 1
    assert report["prediction_ready"] == 2

    by_id = {it["match_id"]: it for it in report["items"]}
    assert by_id["500j-2030-05-11-4306"]["effective_status"] == "FUTURE_UPCOMING_VERIFIED_KICKOFF"
    assert by_id["500j-2030-05-11-4306"]["prediction_capture_eligible_now"] is True
    assert by_id["500j-2030-05-11-4307"]["effective_status"] == "FUTURE_UPCOMING_VERIFIED_KICKOFF"
    assert by_id["500j-2030-05-11-4307"]["prediction_snapshot_present"] is True
    assert by_id["500j-2030-05-11-4307"]["prediction_capture_eligible_now"] is False
    assert by_id["500j-2030-05-10-4308"]["effective_status"] == "UPCOMING_UNKNOWN_KICKOFF"
    assert by_id["500lq-2030-05-10-4309"]["effective_status"] == "PAST_KICKOFF_STORED_UPCOMING"
    assert by_id["500j-2030-05-08-4301"]["effective_status"] == "HISTORICAL_STATUS_STALE"
    assert by_id["500lq-2030-05-08-4302"]["effective_status"] == "HISTORICAL_STATUS_STALE"
    # Never auto-labeled as FINISHED_SOURCE_CONFIRMED from time elapsed
    assert by_id["500lq-2030-05-08-4302"]["effective_status"] != "FINISHED_SOURCE_CONFIRMED"
    assert by_id["500lq-2030-05-10-4310"]["effective_status"] == "LIVE_SOURCE_CONFIRMED"
    assert by_id["500lq-2030-05-09-4311"]["effective_status"] == "FINISHED_SOURCE_CONFIRMED"
    assert by_id["500lq-2030-05-10-4312"]["effective_status"] == "STATUS_NOT_ESTABLISHED"
