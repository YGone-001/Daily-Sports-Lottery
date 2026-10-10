import pytest
import os
import json
import hashlib
from unittest.mock import patch

from utils.scraper import (
    _merge,
    _refresh_impl,
    refresh,
    CanonicalIdentityCollisionError,
    RefreshBusyError
)
from utils.daily_loader import save_json, load_json

# R01
def test_r01_same_id_different_events_rejected():
    existing = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'}]
    incoming = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_ID_OWNER_MISMATCH'

# R02
def test_r02_different_ids_same_event_retains_existing_id():
    existing = [{'id': 'orig-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'jczq_no': '001'}]
    incoming = [{'id': 'new-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'jczq_no': '001'}]
    res, added, updated = _merge(existing, incoming)
    assert len(res) == 1
    assert res[0]['id'] == 'orig-1'

# R03
def test_r03_missing_or_malformed_identity_fails_closed():
    existing = [{'id': 'football-2026-10-09-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}]
    incoming = [{'sport': 'football', 'date': '2026-10-09', 'home': 'E', 'away': 'F'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_SHARED_ID'

# R04
def test_r04_finalized_match_conflict_preserves_finality(capsys):
    existing = [{'id': 'orig-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'status': 'finished', 'score': {'ft': [1, 0]}}]
    incoming = [{'id': 'new-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'status': 'finished', 'score': {'ft': [2, 0]}}]
    res, added, updated = _merge(existing, incoming)
    assert res[0]['score']['ft'] == [1, 0]
    out, _ = capsys.readouterr()
    assert "[Auto-Sync] 完赛结果冲突 orig-1:" in out

# R05 and BC-04
@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_r05_pre_existing_duplicate_rejected_before_fetch(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    data = {
        "matches": [
            {'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'market_tracked': True},
            {'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D', 'market_tracked': True}
        ]
    }
    save_json('daily_matches.json', data)

    hashes = {}
    files = ['daily_matches.json', 'team_strength.json', 'calibrated.json', 'prediction_snapshots.json', 'odds_snapshots.json', 'settlements.json', 'evaluation_rows.json']
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        hashes[f] = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)

    assert exc.value.category == 'PREEXISTING_DUPLICATE'
    assert mock_fball.call_count == 0
    assert mock_bball.call_count == 0
    assert mock_xml.call_count == 0
    assert mock_finished.call_count == 0

    for f in files:
        p = os.path.join(isolated_data_dir, f)
        new_hash = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None
        assert hashes[f] == new_hash

    mock_fball.return_value = []
    mock_bball.return_value = []
    mock_xml.return_value = []
    mock_finished.return_value = []

    save_json('daily_matches.json', {"matches": [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'}]})
    res = refresh(verbose=False)
    assert res['total'] == 1

# R06
def test_r06_two_incoming_events_share_id_rejected():
    existing = []
    incoming = [
        {'id': 'dup-2', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'},
        {'id': 'dup-2', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_ID_OWNER_MISMATCH'

# R07, R08, R09
@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_r07_r08_r09_collision_prevents_downstream_writes(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    save_json('daily_matches.json', {"matches": [{'id': 'valid-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'status': 'upcoming', 'market_tracked': True}]})

    hashes = {}
    files = ['daily_matches.json', 'team_strength.json', 'calibrated.json', 'prediction_snapshots.json', 'odds_snapshots.json', 'settlements.json', 'evaluation_rows.json']
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        hashes[f] = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None

    odds = {'home_win': 1.5, 'draw': 3.0, 'away_win': 4.0}
    mock_fball.return_value = [{'id': 'valid-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D', 'status': 'finished', 'score': {'ft': [1,1]}, 'odds': odds}]
    mock_bball.return_value = []
    mock_xml.return_value = []
    mock_finished.return_value = []

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)

    assert exc.value.category == 'INCOMING_ID_OWNER_MISMATCH'
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        new_hash = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else None
        assert hashes[f] == new_hash

# R11
@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_r11_valid_repeated_refresh_preserves_idempotency(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    odds = {'home_win': 1.5, 'draw': 3.0, 'away_win': 4.0}
    base_match = {'id': 'valid-2', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'odds': odds}
    mock_fball.return_value = [base_match]
    mock_bball.return_value = []
    mock_xml.return_value = []
    mock_finished.return_value = []

    res = refresh(verbose=False)
    assert res['added'] == 1

    res2 = refresh(verbose=False)
    assert res2['added'] == 0

# BC-R13
def test_bc_r13_incoming_id_belongs_to_different_matched_event():
    existing = [
        {"id": "match-A", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "status": "upcoming"},
        {"id": "match-B", "sport": "football", "date": "2026-10-09", "home": "C", "away": "D", "status": "upcoming"},
    ]
    incoming = [
        {"id": "match-B", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "status": "finished", "score": {"ft": [2, 1]}}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_ID_OWNER_MISMATCH'

# BC-R14
def test_bc_r14_existing_canonical_entries_have_malformed_types(isolated_data_dir):
    save_json('daily_matches.json', ["array_instead_of_dict"])
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'MALFORMED_CANONICAL_ROOT'

    save_json('daily_matches.json', {"matches": "not_an_array"})
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'MALFORMED_MATCHES_ARRAY'

    save_json('daily_matches.json', {"matches": [{'sport': 'football'}]})
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'AMBIGUOUS_SOURCE'

    save_json('daily_matches.json', {"matches": [{'id': 123, 'sport': 'football'}]})
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'MALFORMED_ID_TYPE'

# BC-R15
def test_bc_r15_same_id_duplicate_appears_in_incoming_batch_after_valid():
    existing = []
    incoming = [
        {"id": "match-1", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B"},
        {"id": "match-1", "sport": "football", "date": "2026-10-09", "home": "C", "away": "D"},
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'INCOMING_ID_OWNER_MISMATCH'

# BC-R16
def test_bc_r16_supplied_id_is_unclaimed_and_legitimately_reconciles():
    existing = [{"id": "orig-id", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "jczq_no": "001"}]
    incoming = [{"id": "diff-id", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "jczq_no": "001"}]
    res, added, updated = _merge(existing, incoming)
    assert len(res) == 1
    assert res[0]['id'] == 'orig-id'
