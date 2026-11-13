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
