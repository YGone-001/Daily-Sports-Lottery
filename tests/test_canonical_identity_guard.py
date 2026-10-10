import pytest
import os
import json
from unittest.mock import patch

from utils.scraper import (
    _merge,
    _refresh_impl,
    CanonicalIdentityCollisionError,
    RefreshBusyError
)

# Test R01
def test_r01_same_id_different_events_rejected():
    existing = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'}]
    incoming = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_SHARED_ID'

# Test R02
def test_r02_different_ids_same_real_event_retains_existing():
    existing = [{'id': 'orig-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'jczq_no': '001'}]
    incoming = [{'id': 'new-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'jczq_no': '001'}]
    res, added, updated = _merge(existing, incoming)
    assert len(res) == 1
    assert res[0]['id'] == 'orig-1'

# Test R03
def test_r03_missing_identity_fails_closed():
    existing = [{'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'}]
    incoming = []
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'AMBIGUOUS_SOURCE'
    
    existing2 = [{'id': 'football-2026-10-09-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}]
    incoming2 = [{'sport': 'football', 'date': '2026-10-09', 'home': 'E', 'away': 'F'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing2, incoming2)
    assert exc.value.category == 'INCOMING_SHARED_ID'

# Test R04
def test_r04_finished_result_conflicts_with_identity():
    existing = [{'id': '500l-dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'TeamA', 'away': 'TeamB', 'jczq_no': '4004', 'status': 'upcoming'}]
    incoming = [{'id': '500l-dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'TeamC', 'away': 'TeamD', 'status': 'finished'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_SHARED_ID'

# Test R05
def test_r05_pre_existing_duplicate_rejected():
    existing = [
        {'id': 'dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'},
        {'id': 'dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [])
    assert exc.value.category == 'PREEXISTING_DUPLICATE'

# Test R06
def test_r06_two_incoming_events_with_same_id_rejected():
    existing = []
    incoming = [
        {'id': 'dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'},
        {'id': 'dup', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_SHARED_ID'

@pytest.fixture
def isolated_data_dir(tmp_path):
    import config
    old_dir = config.DATA_DIR
    config.DATA_DIR = str(tmp_path)
    os.makedirs(config.DATA_DIR, exist_ok=True)
    yield config.DATA_DIR
    config.DATA_DIR = old_dir

@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_r07_to_r12_integration(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    from utils.daily_loader import save_json, load_json
    
    odds = {'home_win': 1.5, 'draw': 3.0, 'away_win': 4.0}
    
    # R11 Valid repeated refresh -> accepted existing idempotency
    base_match = {'id': 'valid-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'odds': odds}
    mock_fball.return_value = [base_match]
    mock_bball.return_value = []
    mock_xml.return_value = []
    mock_finished.return_value = []
    
    res = _refresh_impl(verbose=False)
    assert res['added'] == 1
    
    res2 = _refresh_impl(verbose=False)
    assert res2['added'] == 0
    
    hashes = {}
    files = ['daily_matches.json', 'team_strength.json', 'calibrated.json', 'prediction_snapshots.json', 'odds_snapshots.json', 'settlements.json', 'evaluation_rows.json']
    import hashlib
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        hashes[f] = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None
        
    colliding_match = {'id': 'valid-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D', 'odds': odds}
    mock_fball.return_value = [colliding_match]
    
    with pytest.raises(CanonicalIdentityCollisionError):
        _refresh_impl(verbose=False)
        
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        new_hash = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None
        assert hashes[f] == new_hash

    from utils.scraper import refresh
    with pytest.raises(CanonicalIdentityCollisionError):
        refresh(verbose=False)
        
    mock_fball.return_value = []
    res3 = refresh(verbose=False)
    assert res3['total'] == 1
