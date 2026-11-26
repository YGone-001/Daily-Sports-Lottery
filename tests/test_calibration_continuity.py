"""Calibration continuity: one application per Canonical ID and no history rewrite."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import config
from utils import scraper

UNTOUCHED = (
    "daily_matches.json",
    "prediction_snapshots.json",
    "odds_snapshots.json",
    "settlements.json",
    "evaluation_rows.json",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _match(mid, score, status="finished"):
    return {
        "id": mid, "sport": "football", "league": "测试联赛",
        "date": "2026-01-02", "time": "20:00", "status": status,
        "home": "主队", "away": "客队", "score": {"ft": score} if score else None,
    }


def _setup(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "calibrated.json").write_text('{"ids": []}', encoding="utf-8")
    (data_dir / "daily_matches.json").write_text(
        json.dumps({"matches": [_match("500l-2026-01-02-1001", [2, 1])]}),
        encoding="utf-8")
    for name in UNTOUCHED[1:]:
        (data_dir / name).write_text('{"version": 1}', encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", str(data_dir))
    return data_dir


def test_repeated_calibration_is_idempotent_per_canonical_id(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(scraper, "update_from_result",
                        lambda *args, **kwargs: calls.append(args))
    before = {name: _sha(data_dir / name) for name in UNTOUCHED}

    match = _match("500l-2026-01-02-1001", [2, 1])
    assert scraper._calibrate_from_finished([match]) == 1
    assert scraper._calibrate_from_finished([match]) == 0
    assert len(calls) == 1

    calibrated = json.loads((data_dir / "calibrated.json").read_text(encoding="utf-8"))
    assert calibrated["ids"] == ["500l-2026-01-02-1001"]
    assert len(calibrated["ids"]) == len(set(calibrated["ids"]))
    assert {name: _sha(data_dir / name) for name in UNTOUCHED} == before


def test_second_distinct_canonical_id_is_applied_once(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(scraper, "update_from_result",
                        lambda *args, **kwargs: calls.append(args))
    first = _match("500l-2026-01-02-1001", [2, 1])
    second = _match("500l-2026-01-02-1002", [0, 3])
    assert scraper._calibrate_from_finished([first]) == 1
    assert scraper._calibrate_from_finished([first, second]) == 1
    assert scraper._calibrate_from_finished([first, second]) == 0
    assert len(calls) == 2
    calibrated = json.loads((data_dir / "calibrated.json").read_text(encoding="utf-8"))
    assert calibrated["ids"] == ["500l-2026-01-02-1001", "500l-2026-01-02-1002"]


def test_invalid_or_non_final_scores_never_calibrate(tmp_path, monkeypatch):
    data_dir = _setup(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(scraper, "update_from_result",
                        lambda *args, **kwargs: calls.append(args))
    before = (data_dir / "calibrated.json").read_bytes()
    assert scraper._calibrate_from_finished([_match("m1", None)]) == 0
    assert scraper._calibrate_from_finished([_match("m2", [1, 0], status="live")]) == 0
    assert scraper._calibrate_from_finished([_match("m3", ["1", 0])]) == 0
    assert calls == []
    assert (data_dir / "calibrated.json").read_bytes() == before
