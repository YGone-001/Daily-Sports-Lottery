"""只读结算 API 的端到端测试。"""
from __future__ import annotations

from utils.daily_loader import enrich_match
from utils.prediction_snapshots import capture_snapshot
from utils.settlements import settle_snapshot


def _finished(match: dict, ft) -> dict:
    return dict(match, status="finished", date="2020-01-01", time="20:00", score={"ft": ft})


def test_settlement_api_returns_records(isolated_data_dir, make_match):
    m = enrich_match(make_match(id="500w-2030-09-01-1"))
    snapshot, created = capture_snapshot(m)
    assert created is True
    settlement, settled = settle_snapshot(snapshot, _finished(m, [3, 0]))
    assert settled is True

    import app as app_module
    client = app_module.app.test_client()

    resp = client.get(f"/api/match/{m['id']}/settlements")
    assert resp.status_code == 200
    data = resp.get_json()

    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["settlement_id"] == settlement["settlement_id"]
    assert data[0]["snapshot_id"] == snapshot["snapshot_id"]
    assert data[0]["final_score"] == {"home": 3, "away": 0}
    assert data[0]["actual_outcome"] == "home_win"

    # 只读、无副作用：重复调用结果完全一致
    assert client.get(f"/api/match/{m['id']}/settlements").get_json() == data


def test_settlement_api_empty_for_unknown_match(isolated_data_dir):
    import app as app_module
    client = app_module.app.test_client()

    resp = client.get("/api/match/does-not-exist/settlements")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_settlement_api_is_deterministic_for_multiple_versions(isolated_data_dir, make_match):
    m = enrich_match(make_match(id="500w-2030-09-02-2"))
    s1, _c1 = capture_snapshot(m, model_version="model-v1")
    s2, _c2 = capture_snapshot(m, model_version="model-v2")
    finished = _finished(m, [1, 1])
    settle_snapshot(s1, finished)
    settle_snapshot(s2, finished)

    import app as app_module
    client = app_module.app.test_client()

    data = client.get(f"/api/match/{m['id']}/settlements").get_json()
    assert len(data) == 2
    assert {d["model_version"] for d in data} == {"model-v1", "model-v2"}
    assert {d["final_score"]["home"] for d in data} == {1}
    assert client.get(f"/api/match/{m['id']}/settlements").get_json() == data
