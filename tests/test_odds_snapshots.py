"""赛前赔率历史的行为测试。"""
from __future__ import annotations

import json
import os
from datetime import timedelta

import config
from utils.daily_loader import get_match_datetime
from utils.odds_snapshots import (
    ODDS_SOURCE,
    get_odds_history_for_match,
    get_odds_snapshot,
    latest_odds_snapshot,
    normalize_odds,
    odds_fingerprint,
    record_odds_snapshot,
    snapshot_id_for,
    store_path,
)

ODDS_A = {"home_win": 2.10, "draw": 3.20, "away_win": 3.40, "matchnum": "3001"}
ODDS_B = {"home_win": 1.95, "draw": 3.30, "away_win": 3.80, "matchnum": "3001"}


def _before_kickoff(match, hours=2):
    return get_match_datetime(match) - timedelta(hours=hours)


def test_first_observation_stored(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    snap = record_odds_snapshot(m, now=_before_kickoff(m))

    assert snap is not None
    assert snap["match_id"] == m["id"]
    assert snap["odds"] == ODDS_A
    assert snap["source"] == ODDS_SOURCE
    assert snap["jczq_no"] == "3001"
    assert os.path.exists(store_path())
    assert len(get_odds_history_for_match(m["id"])) == 1


def test_schema_fields(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    snap = record_odds_snapshot(m, now=_before_kickoff(m))

    for key in (
        "snapshot_id", "match_id", "sport", "league", "home_team", "away_team",
        "match_date", "match_time", "kickoff_at", "captured_at",
        "source", "jczq_no", "odds", "odds_fingerprint",
    ):
        assert key in snap, f"缺少字段: {key}"
    assert snap["kickoff_at"] is not None


def test_unchanged_odds_suppressed(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    base = _before_kickoff(m)

    assert record_odds_snapshot(m, now=base) is not None
    assert record_odds_snapshot(m, now=base + timedelta(minutes=30)) is None
    assert record_odds_snapshot(m, now=base + timedelta(minutes=60)) is None

    assert len(get_odds_history_for_match(m["id"])) == 1


def test_changed_odds_appended(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    base = _before_kickoff(m)

    record_odds_snapshot(m, now=base)
    record_odds_snapshot(make_match(odds=dict(ODDS_B)), now=base + timedelta(minutes=30))

    history = get_odds_history_for_match(m["id"])
    assert len(history) == 2
    assert history[0]["odds"] == ODDS_A
    assert history[1]["odds"] == ODDS_B
    assert history[0]["captured_at"] < history[1]["captured_at"]


def test_return_to_previous_odds_is_preserved(isolated_data_dir, make_match):
    """A -> B -> A 必须保留三条（证明是「连续重复抑制」而非「全局指纹去重」）。"""
    m = make_match(odds=dict(ODDS_A))
    base = _before_kickoff(m)

    record_odds_snapshot(m, now=base)
    record_odds_snapshot(make_match(odds=dict(ODDS_B)), now=base + timedelta(minutes=30))
    record_odds_snapshot(make_match(odds=dict(ODDS_A)), now=base + timedelta(minutes=60))

    history = get_odds_history_for_match(m["id"])
    assert len(history) == 3
    assert [h["odds"] for h in history] == [ODDS_A, ODDS_B, ODDS_A]
    assert history[0]["odds_fingerprint"] == history[2]["odds_fingerprint"]


def test_dictionary_ordering_is_irrelevant(isolated_data_dir):
    a = {"home_win": 2.1, "draw": 3.2, "away_win": 3.4}
    b = {"away_win": 3.4, "home_win": 2.1, "draw": 3.2}
    assert odds_fingerprint(a) == odds_fingerprint(b)
    assert normalize_odds(a) == normalize_odds(b)
    assert list(normalize_odds(b)) == ["away_win", "draw", "home_win"]  # 稳定键序


def test_none_values_do_not_create_fake_movement(isolated_data_dir, make_match):
    plain = {"home_win": 2.1, "draw": 3.2, "away_win": 3.4}
    padded = {"home_win": 2.1, "draw": 3.2, "away_win": 3.4, "total_line": None, "over": None}

    assert odds_fingerprint(plain) == odds_fingerprint(padded)

    base = _before_kickoff(make_match())
    m1 = make_match(odds=dict(plain))
    m2 = make_match(odds=dict(padded))

    assert record_odds_snapshot(m1, now=base) is not None
    assert record_odds_snapshot(m2, now=base + timedelta(minutes=30)) is None
    assert len(get_odds_history_for_match(m1["id"])) == 1


def test_no_odds_produces_nothing(isolated_data_dir, make_match):
    m = make_match(odds=None)
    assert record_odds_snapshot(m, now=_before_kickoff(m)) is None
    assert get_odds_history_for_match(m["id"]) == []


def test_live_match_rejected(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    live_now = get_match_datetime(m) + timedelta(minutes=10)

    assert record_odds_snapshot(m, now=live_now) is None
    assert get_odds_history_for_match(m["id"]) == []


def test_finished_match_rejected(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    finished_now = get_match_datetime(m) + timedelta(minutes=200)

    assert record_odds_snapshot(m, now=finished_now) is None
    assert get_odds_history_for_match(m["id"]) == []


def test_existing_history_readable_after_kickoff_and_finish(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    kickoff = get_match_datetime(m)
    snap = record_odds_snapshot(m, now=kickoff - timedelta(hours=2))
    assert snap is not None

    changed = make_match(odds=dict(ODDS_B))
    assert record_odds_snapshot(changed, now=kickoff + timedelta(minutes=10)) is None   # live
    assert record_odds_snapshot(changed, now=kickoff + timedelta(minutes=200)) is None  # finished

    history = get_odds_history_for_match(m["id"])
    assert len(history) == 1
    assert history[0]["snapshot_id"] == snap["snapshot_id"]
    assert get_odds_snapshot(snap["snapshot_id"]) is not None
    assert latest_odds_snapshot(m["id"])["odds"] == ODDS_A


def test_fingerprint_is_stable(isolated_data_dir):
    odds = {"draw": 3.2, "away_win": 3.4, "home_win": 2.1}
    first = odds_fingerprint(odds)
    assert first == odds_fingerprint(odds)
    assert first == odds_fingerprint({"home_win": 2.1, "away_win": 3.4, "draw": 3.2})
    assert len(first) == 64
    assert first != odds_fingerprint({**odds, "home_win": 2.11})


def test_snapshot_identity_represents_match_time_and_fingerprint(isolated_data_dir):
    fp = odds_fingerprint(ODDS_A)
    base = snapshot_id_for("m1", "2030-01-01T10:00:00+08:00", fp)
    assert base == snapshot_id_for("m1", "2030-01-01T10:00:00+08:00", fp)
    assert base != snapshot_id_for("m2", "2030-01-01T10:00:00+08:00", fp)
    assert base != snapshot_id_for("m1", "2030-01-01T11:00:00+08:00", fp)
    assert base != snapshot_id_for("m1", "2030-01-01T10:00:00+08:00", odds_fingerprint(ODDS_B))


def test_atomic_serialization_is_reloadable(isolated_data_dir, make_match):
    m = make_match(odds=dict(ODDS_A))
    base = _before_kickoff(m)
    record_odds_snapshot(m, now=base)
    record_odds_snapshot(make_match(odds=dict(ODDS_B)), now=base + timedelta(minutes=30))

    path = store_path()
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    parsed = json.loads(raw)

    assert parsed["version"] == 1
    assert len(parsed["snapshots"]) == 2
    # sort_keys 保证重新落盘字节稳定
    from utils.atomic_json import atomic_write_json
    atomic_write_json(path, parsed)
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == raw


def test_store_file_is_lazy(isolated_data_dir):
    assert not os.path.exists(store_path())
    assert get_odds_history_for_match("nope") == []  # 纯读取不落盘
    assert not os.path.exists(store_path())


# ---------------------------------------------------------------------------
# 只读 API
# ---------------------------------------------------------------------------

def _write_daily(match: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, "daily_matches.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"meta": {}, "matches": [match]}, fh, ensure_ascii=False)


def test_api_returns_chronological_history(isolated_data_dir, make_match):
    match = make_match(id="500w-2030-07-01-9", odds=dict(ODDS_A))
    _write_daily(match)

    import app as app_module
    client = app_module.app.test_client()

    # 无历史时返回 []，且不产生任何副作用（不创建文件）
    resp = client.get(f"/api/match/{match['id']}/odds-history")
    assert resp.status_code == 200
    assert resp.get_json() == []
    assert not os.path.exists(store_path())

    base = _before_kickoff(match)
    record_odds_snapshot(match, now=base)
    record_odds_snapshot(make_match(id=match["id"], odds=dict(ODDS_B)), now=base + timedelta(minutes=30))
    record_odds_snapshot(make_match(id=match["id"], odds=dict(ODDS_A)), now=base + timedelta(minutes=60))

    data = client.get(f"/api/match/{match['id']}/odds-history").get_json()
    assert isinstance(data, list) and len(data) == 3
    assert [d["odds"]["home_win"] for d in data] == [2.10, 1.95, 2.10]
    assert [d["captured_at"] for d in data] == sorted(d["captured_at"] for d in data)
    # 再次调用仍然不改变历史
    assert len(client.get(f"/api/match/{match['id']}/odds-history").get_json()) == 3


def test_api_empty_for_unknown_match(isolated_data_dir):
    import app as app_module
    client = app_module.app.test_client()

    resp = client.get("/api/match/does-not-exist/odds-history")
    assert resp.status_code == 200
    assert resp.get_json() == []
