"""只读快照 API 的端到端测试。"""
from __future__ import annotations

import json
import os

import config


def _write_daily(match: dict) -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)
    path = os.path.join(config.DATA_DIR, "daily_matches.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"meta": {}, "matches": [match]}, fh, ensure_ascii=False)


def test_snapshot_api_returns_persisted_record(isolated_data_dir, make_match):
    match = make_match(id="500w-2030-06-01-7")
    _write_daily(match)

    import app as app_module

    client = app_module.app.test_client()

    # 触发一次预测 -> 自动固化快照
    assert client.get(f"/api/match/{match['id']}").status_code == 200

    resp = client.get(f"/api/match/{match['id']}/snapshots")
    assert resp.status_code == 200
    data = resp.get_json()

    assert isinstance(data, list)
    assert len(data) == 1
    snap = data[0]
    assert snap["match_id"] == match["id"]
    assert snap["model_version"] == config.MODEL_VERSIONS["football"]
    assert snap["model_probabilities"]
    assert "expected_score_data" in snap


def test_snapshot_api_empty_list_for_unknown_match(isolated_data_dir):
    import app as app_module

    client = app_module.app.test_client()
    resp = client.get("/api/match/does-not-exist/snapshots")

    assert resp.status_code == 200
    assert resp.get_json() == []


def test_snapshot_api_does_not_duplicate(isolated_data_dir, make_match):
    match = make_match(id="500w-2030-06-02-3")
    _write_daily(match)

    import app as app_module

    client = app_module.app.test_client()
    for _ in range(3):
        client.get(f"/api/match/{match['id']}")

    data = client.get(f"/api/match/{match['id']}/snapshots").get_json()
    assert len(data) == 1
