"""HTTP/startup capability boundaries; all stores and clocks are isolated."""
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

import app as server
import config
from utils import daily_loader, nba_schedule, prediction_snapshots, scraper, team_strength
from utils.daily_loader import override_beijing_now
from test_nba_schedule_completeness import NOW, canonical, game, Response
from test_balldontlie_schedule_adapter import _bdl_payload
from test_basketball_operational_acceptance import write_stores, store_hashes


@pytest.fixture
def stores(isolated_data_dir, monkeypatch):
    m = canonical()
    m.update(time="08:30", kickoff_time_known=True)
    write_stores(isolated_data_dir, [m])
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITES_ENABLED", False)
    monkeypatch.setitem(server.app.config, "BACKGROUND_REFRESH_ENABLED", False)
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITE_TOKEN", "")
    monkeypatch.setitem(server.app.config, "LOCAL_UI_REFRESH_ENABLED", False)
    monkeypatch.delenv("SERVE_FRONTEND", raising=False)
    monkeypatch.setattr(nba_schedule.requests, "get", lambda *a, **k: pytest.fail("GET attempted network"))
    return isolated_data_dir, m


def test_all_get_routes_are_readonly(stores):
    directory, m = stores
    before = store_hashes(directory)
    with override_beijing_now(NOW):
        api = server.app.test_client()
        for path in ["/?date=2030-05-15", "/match/"+m['id'], "/api/today",
                     "/api/matches?date=2030-05-15", "/api/match/"+m['id'],
                     "/api/match/"+m['id']+"/snapshots", "/api/match/"+m['id']+"/odds-history",
                     "/api/match/"+m['id']+"/settlements", "/api/history", "/history", "/strategy",
                     "/api/strategy", "/api/dates", "/api/status"]:
            assert api.get(path).status_code == 200, path
            assert store_hashes(directory) == before, path
        assert api.get('/api/refresh').status_code == 405
        assert api.head('/api/refresh').status_code == 405
        assert store_hashes(directory) == before
    assert team_strength._CACHE == {}


def test_existing_snapshot_is_displayed_unchanged(stores):
    directory, m = stores
    with override_beijing_now(NOW):
        snap, created = prediction_snapshots.capture_snapshot(daily_loader.enrich_match(m))
    assert created
    before = store_hashes(directory)
    response = server.app.test_client().get('/api/match/'+m['id']+'/snapshots')
    assert response.json == [snap]
    assert store_hashes(directory) == before


@pytest.mark.parametrize('enabled,token,supplied', [(False,'test','test'),(True,'',''),(True,'test',''),(True,'test','wrong')])
def test_unauthorized_post_never_calls_refresh(stores, monkeypatch, enabled, token, supplied):
    directory, _ = stores
    monkeypatch.setitem(server.app.config,'RUNTIME_WRITES_ENABLED',enabled)
    monkeypatch.setitem(server.app.config,'RUNTIME_WRITE_TOKEN',token)
    monkeypatch.setattr(scraper,'refresh',lambda **kw: pytest.fail('unauthorized refresh'))
    before = store_hashes(directory)
    assert server.app.test_client().post('/api/refresh',headers={'X-Refresh-Token':supplied}).status_code == 403
    assert store_hashes(directory) == before


def test_authorized_post_runs_normal_isolated_refresh(stores, monkeypatch):
    directory, m = stores
    monkeypatch.setitem(server.app.config,'RUNTIME_WRITES_ENABLED',True)
    monkeypatch.setitem(server.app.config,'RUNTIME_WRITE_TOKEN','isolated-token')
    monkeypatch.setattr(scraper.fetcher_500,'fetch_live_matches',lambda *a: [])
    monkeypatch.setattr(scraper.fetcher_500,'fetch_live_basketball',lambda: [])
    monkeypatch.setattr(scraper.fetcher_500,'fetch_finished_matches',lambda: [])
    monkeypatch.setattr(scraper.fetcher_500,'fetch_jczq_xml',lambda sport:[deepcopy(m)] if sport=='basketball' else [])
    # Provider networking remains disabled. The existing market schedule is independently known.
    monkeypatch.setattr(config,'ENABLE_NBA_SCHEDULE_NETWORK',False,raising=False)
    with override_beijing_now(NOW):
        response = server.app.test_client().post('/api/refresh',headers={'X-Refresh-Token':'isolated-token'})
    assert response.status_code == 200
    assert response.json['prediction_snapshots_added'] == 1
    snapshot = prediction_snapshots.get_snapshots_for_match(m['id'])[0]
    assert snapshot['generated_at'] == NOW.isoformat()
    assert snapshot['model_version']


def test_local_ui_refresh_requires_runtime_writes(stores, monkeypatch):
    """默认拒绝基线：仅显式打开本地 UI 开关、主写开关关闭时，回环浏览器上下文同样被拒绝。"""
    directory, _ = stores
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITES_ENABLED", False)
    monkeypatch.setitem(server.app.config, "LOCAL_UI_REFRESH_ENABLED", True)
    refreshed = []
    monkeypatch.setattr(scraper, "refresh", lambda **kw: refreshed.append(True) or {"status": "ok"})
    before = store_hashes(directory)

    for headers in (
        {"X-Requested-With": "XMLHttpRequest"},
        {"Sec-Fetch-Site": "same-origin"},
        {"Referer": "http://127.0.0.1:5000/"},
        {"Origin": "http://localhost:5000"},
    ):
        resp = server.app.test_client().post("/api/refresh", headers=headers)
        assert resp.status_code == 403, headers
        assert resp.json["error"] == "RUNTIME_WRITES_DISABLED", headers
    assert len(refreshed) == 0
    assert store_hashes(directory) == before


def test_local_ui_refresh_contract(stores, monkeypatch):
    """本地 UI 同步契约：主写开关 + 本地 UI 开关**同时**显式开启才可用。"""
    directory, m = stores
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITES_ENABLED", True)
    monkeypatch.setitem(server.app.config, "RUNTIME_WRITE_TOKEN", "")
    monkeypatch.setitem(server.app.config, "LOCAL_UI_REFRESH_ENABLED", True)
    refreshed = []
    monkeypatch.setattr(scraper, "refresh", lambda **kw: refreshed.append(True) or {"status": "ok", "added": 1})
    before = store_hashes(directory)

    # 1. 回环但无 Web UI 头（普通 API/curl 调用）：即使开关全开也必须持令牌，拒绝 403
    resp_api = server.app.test_client().post("/api/refresh")
    assert resp_api.status_code == 403
    assert resp_api.json["error"] == "REFRESH_AUTHORIZATION_REQUIRED"
    assert len(refreshed) == 0

    # 2. 回环 + Web UI 专用头（X-Requested-With）：允许同步 200
    resp_ui = server.app.test_client().post("/api/refresh", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp_ui.status_code == 200
    assert resp_ui.json["status"] == "ok"
    assert len(refreshed) == 1

    # 3. 回环 + Web UI 浏览器 Referer：允许同步 200
    resp_ref = server.app.test_client().post("/api/refresh", headers={"Referer": "http://127.0.0.1:5000/"})
    assert resp_ref.status_code == 200
    assert len(refreshed) == 2

    # 4. 非回环（远程伪造 Web UI 头）：不进入本地 UI 通道，必须持令牌，拒绝 403
    resp_remote = server.app.test_client().post(
        "/api/refresh",
        environ_base={"REMOTE_ADDR": "192.168.1.100"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert resp_remote.status_code == 403
    assert resp_remote.json["error"] == "REFRESH_AUTHORIZATION_REQUIRED"
    assert len(refreshed) == 2

    # 5. 本地 UI 开关关闭：即使主写开关开启且带 UI 头，回环调用也退回令牌校验，拒绝 403
    monkeypatch.setitem(server.app.config, "LOCAL_UI_REFRESH_ENABLED", False)
    resp_disabled = server.app.test_client().post("/api/refresh", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp_disabled.status_code == 403
    assert resp_disabled.json["error"] == "REFRESH_AUTHORIZATION_REQUIRED"
    assert len(refreshed) == 2
    assert store_hashes(directory) == before  # 上述全部被拒，无刷新发生


def test_default_config_flags_are_default_deny():
    """全新环境（无任何 DSL_* 环境变量）必须默认拒绝：主写 / 后台 / 本地 UI / 提供商网络全部关闭。"""
    script = (
        "import config\n"
        "assert config.RUNTIME_WRITES_ENABLED is False\n"
        "assert config.BACKGROUND_REFRESH_ENABLED is False\n"
        "assert config.LOCAL_UI_REFRESH_ENABLED is False\n"
        "assert config.ENABLE_NBA_SCHEDULE_NETWORK is False\n"
        "print('DEFAULT_DENY_CONFIG')\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "DSL_RUNTIME_WRITES_ENABLED",
            "DSL_BACKGROUND_REFRESH_ENABLED",
            "DSL_LOCAL_UI_REFRESH_ENABLED",
            "DSL_ENABLE_NBA_SCHEDULE_NETWORK",
            "DSL_RUNTIME_WRITE_TOKEN",
        )
    }
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "DEFAULT_DENY_CONFIG" in result.stdout


@pytest.mark.parametrize('debug,reloader',[(False,''),(True,''),(True,'true')])
def test_default_startup_in_isolated_process(stores, debug, reloader):
    directory,_ = stores
    before = store_hashes(directory)
    script = '''
import config,runpy,flask,threading
from unittest.mock import patch
import sys
config.DATA_DIR=sys.argv[1]
with patch.object(flask.Flask,'run'),patch.object(threading.Thread,'start',side_effect=AssertionError('implicit background start')):
    runpy.run_module('app',run_name='__main__')
assert not config.RUNTIME_WRITES_ENABLED and not config.BACKGROUND_REFRESH_ENABLED
print('READ_ONLY_APPLICATION_START')
'''
    env = {k:v for k,v in os.environ.items() if k not in ('DSL_RUNTIME_WRITES_ENABLED','DSL_BACKGROUND_REFRESH_ENABLED','DSL_RUNTIME_WRITE_TOKEN')}
    env.update(DEBUG=str(debug).lower(),WERKZEUG_RUN_MAIN=reloader,PYTHONIOENCODING='utf-8')
    result = subprocess.run([sys.executable,'-c',script,str(directory)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'READ_ONLY_APPLICATION_START' in result.stdout
    assert store_hashes(directory) == before


def test_background_requires_both_explicit_flags(stores,monkeypatch):
    calls=[]
    monkeypatch.setattr(server.threading.Thread,'start',lambda self:calls.append(self))
    monkeypatch.setattr(config,'DEBUG',False)
    for writes,background,expected in [(False,True,False),(True,False,False),(True,True,True)]:
        monkeypatch.setitem(server.app.config,'RUNTIME_WRITES_ENABLED',writes)
        monkeypatch.setitem(server.app.config,'BACKGROUND_REFRESH_ENABLED',background)
        assert server.start_background_scraper() is expected
    assert len(calls)==1


def test_profile_readonly_context_does_not_leak(stores):
    directory,_=stores
    before=store_hashes(directory)
    with team_strength.read_only_profiles():
        team_strength.get_team_profile('unseen','league')
    assert store_hashes(directory)==before and team_strength._CACHE=={}
    team_strength.get_team_profile('unseen','league')
    assert store_hashes(directory)['team_strength.json'] != before['team_strength.json']
