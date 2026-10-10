import pytest
import os
import hashlib
from datetime import datetime
from unittest.mock import patch

from utils.scraper import (
    _merge,
    refresh,
    CanonicalIdentityCollisionError
)
from utils.daily_loader import save_json
from utils.prediction_snapshots import build_snapshot as build_pred_snap
from utils.odds_snapshots import build_snapshot as build_odds_snap
from utils.settlements import build_settlement, extract_final_score
from utils.evaluation_rows import build_evaluation_row

# R01
def test_r01_same_id_different_events_rejected():
    existing = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'}]
    incoming = [{'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

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

# R05, R10
@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_r05_r10_pre_existing_duplicate_rejected_before_fetch(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    data = {
        "matches": [
            {'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B', 'market_tracked': True},
            {'id': 'dup-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D', 'market_tracked': True}
        ]
    }
    save_json('daily_matches.json', data)
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'PREEXISTING_DUPLICATE'
    assert mock_fball.call_count == 0

# R06
def test_r06_two_incoming_events_share_id_rejected():
    existing = []
    incoming = [
        {'id': 'dup-2', 'sport': 'football', 'date': '2026-10-09', 'home': 'A', 'away': 'B'},
        {'id': 'dup-2', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D'}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

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
    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

# BC-R14, BD-R18
def test_bc_r14_bd_r18_malformed_canonical_rejected(isolated_data_dir):
    save_json('daily_matches.json', ["array_instead_of_dict"])
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'MALFORMED_CANONICAL_ROOT'

    save_json('daily_matches.json', {"matches": "not_an_array"})
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)
    assert exc.value.category == 'MALFORMED_MATCHES_ARRAY'

    save_json('daily_matches.json', {"matches": None})
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
    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

# BC-R16
def test_bc_r16_supplied_id_is_unclaimed_and_legitimately_reconciles():
    existing = [{"id": "orig-id", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "jczq_no": "001"}]
    incoming = [{"id": "diff-id", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B", "jczq_no": "001"}]
    res, added, updated = _merge(existing, incoming)
    assert len(res) == 1
    assert res[0]['id'] == 'orig-id'

# BD-R17
def test_bd_r17_multiple_same_event_matches_with_different_canonical_owners():
    existing = [
        {"id": "match-A", "sport": "football", "date": "2026-10-09", "home": "X", "away": "Y", "status": "upcoming"},
        {"id": "match-B", "sport": "football", "date": "2026-10-09", "home": "X", "away": "Y", "jczq_no": "002", "status": "upcoming"},
    ]
    incoming = [
        {"id": "match-B", "sport": "football", "date": "2026-10-09", "home": "X", "away": "Y", "jczq_no": "002", "status": "finished", "score": {"ft": [2, 1]}}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'AMBIGUOUS_CANONICAL_OWNERSHIP'

# BD-R19
def test_bd_r19_incoming_source_alias_reused_for_another_event():
    existing = [
        {"id": "canon-1", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B"}
    ]
    incoming = [
        {"id": "alias-1", "sport": "football", "date": "2026-10-09", "home": "A", "away": "B"},
        {"id": "alias-1", "sport": "football", "date": "2026-10-09", "home": "C", "away": "D"}
    ]
    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, incoming)
    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'
    assert exc.value.match_id == 'alias-1'

# BD-R20, BE-R25
def test_bd_r20_be_r25_malformed_incoming_id_and_record_types():
    existing = []

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [{"id": 123, "sport": "football"}])
    assert exc.value.category == 'MALFORMED_INCOMING_ID'

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [{"id": "   ", "sport": "football"}])
    assert exc.value.category == 'MALFORMED_INCOMING_ID'

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [None])
    assert exc.value.category == 'MALFORMED_INCOMING_RECORD'

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, ["string"])
    assert exc.value.category == 'MALFORMED_INCOMING_RECORD'

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [{"sport": "football"}]) # Missing ID + inadequate identity
    assert exc.value.category == 'INADEQUATE_EVENT_IDENTITY'

# BE-R22, BE-R23, BE-R24, BE-R27
def test_be_r22_to_r24_stale_owner_reproducer():
    existing = [
        {'id': 'match-A', 'sport': 'football', 'date': '2026-10-09', 'home': 'X', 'away': 'Y', 'jczq_no': '001'},
        {'id': 'match-B', 'sport': 'football', 'date': '2026-10-09', 'home': 'X', 'away': 'Y', 'jczq_no': '002'}
    ]

    # Update match-A
    inc1 = {'id': 'match-A', 'sport': 'football', 'date': '2026-10-09', 'jczq_no': '001', 'home': 'C', 'away': 'D'}
    # Reuses id match-A, but looks like original match-B
    inc2 = {'id': 'match-A', 'sport': 'football', 'date': '2026-10-09', 'home': 'X', 'away': 'Y'}

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        _merge(existing, [inc1, inc2])

    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

def _seed_seven_stores(isolated_dir):
    match = {
        'id': 'old-1', 'sport': 'football', 'date': '2026-10-09', 'time': '12:00',
        'home': 'A', 'away': 'B', 'status': 'finished',
        'score': {'ft': [1,0]},
        'home_team': {'elo': 1500}, 'away_team': {'elo': 1500},
        'odds': {'home_win': 2.0, 'draw': 3.0, 'away_win': 4.0},
        'market_tracked': True
    }

    pred = {'probabilities': {'home_win': 0.5, 'draw': 0.3, 'away_win': 0.2}, 'expected_score': {'home': 1, 'away': 1}, 'expected_goals': {'home': 1, 'away': 1}}
    dt = datetime(2026, 10, 9, 10, 0, 0)
    snap = build_pred_snap(match, pred, generated_at=dt)

    odds_snap = build_odds_snap(match, captured_at=dt)

    final_score = extract_final_score(match)
    settle = build_settlement(snap, match, final_score)
    eval_row = build_evaluation_row(snap, settle)

    save_json('daily_matches.json', {"meta": {}, "matches": [match]})
    save_json('team_strength.json', {"teams": {"A": {"elo": 1500}, "B": {"elo": 1500}}})
    save_json('calibrated.json', {"ids": ["old-1"]})

    save_json('prediction_snapshots.json', {"version": 1, "snapshots": {snap['snapshot_id']: snap}})
    save_json('odds_snapshots.json', {"version": 1, "snapshots": {odds_snap['snapshot_id']: odds_snap}})
    save_json('settlements.json', {"version": 1, "settlements": {settle['settlement_id']: settle}})
    save_json('evaluation_rows.json', {"version": 1, "rows": {eval_row['evaluation_id']: eval_row}})

    return ['daily_matches.json', 'team_strength.json', 'calibrated.json', 'prediction_snapshots.json', 'odds_snapshots.json', 'settlements.json', 'evaluation_rows.json']

# BE-R26, R07, R08, R09, R12 (R19 lock release)
@patch('utils.scraper.fetcher_500.fetch_live_matches')
@patch('utils.scraper.fetcher_500.fetch_live_basketball')
@patch('utils.scraper.fetcher_500.fetch_jczq_xml')
@patch('utils.scraper.fetcher_500.fetch_finished_matches')
def test_be_r26_complete_schema_compatible_seven_store_hash_preservation(mock_finished, mock_xml, mock_bball, mock_fball, isolated_data_dir):
    files = _seed_seven_stores(isolated_data_dir)

    hashes = {}
    for f in files:
        p = os.path.join(isolated_data_dir, f)
        assert os.path.exists(p), f"File {f} must exist before"
        hashes[f] = hashlib.sha256(open(p, 'rb').read()).hexdigest()

    odds = {'home_win': 1.5, 'draw': 3.0, 'away_win': 4.0}
    mock_fball.return_value = [{'id': 'old-1', 'sport': 'football', 'date': '2026-10-09', 'home': 'C', 'away': 'D', 'status': 'finished', 'score': {'ft': [1,1]}, 'odds': odds}]
    mock_bball.return_value = []
    mock_xml.return_value = []
    mock_finished.return_value = []

    with pytest.raises(CanonicalIdentityCollisionError) as exc:
        refresh(verbose=False)

    assert exc.value.category == 'SAME_SOURCE_ALIAS_DIFFERENT_EVENT'

    for f in files:
        p = os.path.join(isolated_data_dir, f)
        assert os.path.exists(p), f"File {f} must exist after"
        assert hashes[f] == hashlib.sha256(open(p, 'rb').read()).hexdigest(), f"Hash changed for {f}"

    mock_fball.return_value = []
    res = refresh(verbose=False)
    assert res['total'] == 1
