"""Successful request variants hint activity; hints never own request durability."""
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, update

from conftest import engine
from app import session_activity as activity
from app.models import GameSession
from app.opportunity_retention import database_clock
from app.srs_math import as_utc
from test_srs_api import _create_blunder
from test_drill_api import _start_drill, _roots, _steering_graph, ROOT_FEN, START_FEN


@pytest.mark.parametrize('broken_hint', [False, True])
@pytest.mark.parametrize('action', ['start', 'upload', 'revert', 'opponent', 'replay',
                                    'grade', 'grade_retry', 'end', 'drill_start',
                                    'drill_route', 'drill_fail', 'drill_end', 'drill_abandon'])
def test_success_paths_survive_failed_hints_and_stamp_successful_hints(
    client, auth_headers, create_game_session, db_session, monkeypatch, action, broken_hint,
):
    headers = auth_headers(user_id=123)
    sid = create_game_session(user_id=123)
    session = db_session.get(GameSession, uuid.UUID(sid))
    session.is_rated = False
    session.last_activity_at = None
    db_session.commit()
    if action.startswith('drill_') and action != 'drill_start':
        start = _start_drill(client, auth_headers)
        assert start.status_code == 201
        sid = start.json()['session_id']
        db_session.execute(update(GameSession).where(GameSession.id == uuid.UUID(sid)).values(
            last_activity_at=None))
        if action == 'drill_fail':
            drill = db_session.get(GameSession, uuid.UUID(sid))
            drill.drill_state = 'root_reached'
        db_session.commit()
    blunder_id = None
    if action.startswith('grade'):
        blunder_id = _create_blunder(db_session, user_id=123).id
    calls = []
    real = activity.stamp_activity
    def stamp(e, **kw):
        calls.append(kw)
        assert activity.known_inflight(123)
        if broken_hint:
            raise RuntimeError('hint setup failed')
        return real(e, **kw)
    monkeypatch.setattr(activity, 'stamp_activity', stamp)
    with (patch('app.api.drills.get_opening_roots', return_value=_roots()),
          patch('app.api.drills.get_opening_graph', return_value=_steering_graph())):
        if action == 'start':
            response = client.post('/api/game/start', headers=headers, json={'engine_elo': 1500})
        elif action == 'upload':
            response = client.post(f'/api/session/{sid}/moves', headers=headers, json={'moves': []})
        elif action == 'revert':
            response = client.post(f'/api/session/{sid}/moves/truncate', headers=headers,
                                   json={'line_revision': 0, 'after_ply': 0})
        elif action in ('opponent', 'replay'):
            from app.opponent_move_controller import ControllerMove
            with patch('app.opponent_move_controller.choose_move', return_value=ControllerMove(
                uci='e7e5', san='e5', method='maia3_api',
            )):
                payload = {'session_id': sid, 'fen': ROOT_FEN + ' 0 1'}
                response = client.post('/api/game/next-opponent-move', headers=headers, json=payload)
                if action == 'replay':
                    assert response.status_code == 200, response.text
                    calls.clear()
                    db_session.execute(update(GameSession).where(GameSession.id == uuid.UUID(sid))
                                       .values(last_activity_at=None))
                    db_session.commit()
                    response = client.post('/api/game/next-opponent-move', headers=headers, json=payload)
        elif action.startswith('grade'):
            payload = {'session_id': sid, 'blunder_id': blunder_id, 'passed': True,
                       'user_move': 'Nf3', 'eval_delta': 20, 'idempotency_key': 'activity-grade'}
            response = client.post('/api/srs/review', headers=headers, json=payload)
            if action == 'grade_retry':
                assert response.status_code == 200, response.text
                calls.clear()
                db_session.execute(update(GameSession).where(GameSession.id == uuid.UUID(sid))
                                   .values(last_activity_at=None))
                db_session.commit()
                response = client.post('/api/srs/review', headers=headers, json=payload)
        elif action == 'end':
            response = client.post('/api/game/end', headers=headers,
                                   json={'session_id': sid, 'result': 'draw', 'pgn': '', 'is_rated': False})
        elif action == 'drill_start':
            response = _start_drill(client, auth_headers)
        elif action == 'drill_route':
            response = client.post(f'/api/drills/{sid}/route-check', headers=headers,
                json={'current_fen': START_FEN, 'current_ply': 0})
        elif action == 'drill_fail':
            response = client.post(f'/api/drills/{sid}/fail', headers=headers,
                                   json={'terminal_reason': 'accuracy'})
        elif action == 'drill_end':
            response = client.post(f'/api/drills/{sid}/natural-end', headers=headers,
                                   json={'result': 'draw', 'pgn': ''})
        else:
            response = client.post(f'/api/drills/{sid}/abandon', headers=headers)
    assert response.status_code in (200, 201), response.text
    assert len(calls) == 1
    sid = response.json().get('session_id', sid)
    assert calls[0]['session_id'] == uuid.UUID(sid)
    db_session.expire_all()
    assert (db_session.get(GameSession, uuid.UUID(sid)).last_activity_at is None) == broken_hint
    assert not activity.known_inflight(123)


def test_failed_request_does_not_emit_hint(client, auth_headers, create_game_session, monkeypatch):
    sid = create_game_session(user_id=123)
    monkeypatch.setattr(activity, 'stamp_activity', lambda *a, **k: pytest.fail('failed request hinted'))
    response = client.post('/api/game/next-opponent-move', headers=auth_headers(),
        json={'session_id': sid, 'fen': START_FEN + ' 0 1'})
    assert response.status_code == 400
    assert not activity.known_inflight(123)


def test_database_stamped_coalesced_and_owner_scoped(db_session, create_game_session):
    sid = uuid.UUID(create_game_session(user_id=123))
    db_session.execute(update(GameSession).where(GameSession.id == sid).values(last_activity_at=None))
    db_session.commit()
    before = as_utc(db_session.scalar(select(database_clock(db_session))))
    assert not activity.stamp_activity(engine, session_id=sid, user_id=456)
    assert activity.stamp_activity(engine, session_id=sid, user_id=123)
    db_session.expire_all()
    stamp = as_utc(db_session.get(GameSession, sid).last_activity_at)
    after = as_utc(db_session.scalar(select(database_clock(db_session))))
    assert before <= stamp <= after
    assert not activity.stamp_activity(engine, session_id=sid, user_id=123)
    db_session.execute(update(GameSession).where(GameSession.id == sid).values(
        last_activity_at=stamp - timedelta(minutes=2)))
    db_session.commit()
    assert activity.stamp_activity(engine, session_id=sid, user_id=123)
