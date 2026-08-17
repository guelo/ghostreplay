import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.fen import fen_hash
from app.models import (
    AnalysisCache,
    Blunder,
    BlunderOpportunityEvent,
    Position,
    SessionMove,
)
from app.opening_score_scheduler import (
    request_recompute as real_request_recompute_facade,
)


STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Valid per-row browser-game-v2 DYNAMIC provenance (g-mk1d §2.2). Uploads whose
# subject is the analysis_cache row itself must carry it: an upload without
# provenance declares the retired browser-game-v1 (g-bgv1-cutover) and the batch
# writer refuses the row with INACTIVE_PROFILE_KEEP, so a cache assertion would
# read the profile gate instead of the cohort/filtering contract under test.
BROWSER_V2_PROVENANCE = {
    "engine_version": "18",
    "engine_build": "a8fbc05ec6920b56d7485826dcb02c5ffd2826bcbf751cf973046f237a9096f1",
    "eval_file_id": (
        "nn-9067e33176e8.nnue:"
        "9067e33176e8c5edb7aa8db6a3aedd012f84a1f39872e86357c6c2d0993f314d"
    ),
    "search_limit_type": "depth",
    "search_limit_value": 17,
    "threads": 1,
    "hash_mb": 128,
}


def test_session_moves_bulk_insert_success(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                    "eval_cp": 20,
                    "eval_mate": None,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "best",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                    "eval_cp": 12,
                    "eval_mate": None,
                    "best_move_san": "e5",
                    "best_move_eval_cp": 12,
                    "eval_delta": 0,
                    "classification": "excellent",
                    "decision_source": "backend_engine",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    assert response.json() == {"moves_inserted": 2, "line_revision": 0}

    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .order_by(SessionMove.move_number, SessionMove.color)
        .all()
    )
    assert len(rows) == 2
    assert rows[0].move_number == 1
    assert rows[0].color == "black"
    assert rows[0].move_san == "e5"
    assert rows[0].classification == "excellent"
    assert rows[0].decision_source == "backend_engine"
    assert rows[0].target_blunder_id is None
    assert rows[1].move_number == 1
    assert rows[1].color == "white"
    assert rows[1].move_san == "e4"
    assert rows[1].classification == "best"


def test_session_moves_populate_ghost_graph(client, auth_headers, create_game_session, db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    fen_start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    fen_after_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    target_position = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_after_e5),
        fen_raw=fen_after_e5,
        active_color="white",
    )
    db_session.add(target_position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=target_position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(blunder)
    db_session.commit()

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_before": fen_start,
                    "fen_after": fen_after_e4,
                    "eval_cp": 20,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "best",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_before": fen_after_e4,
                    "fen_after": fen_after_e5,
                    "eval_cp": 12,
                    "best_move_san": "e5",
                    "best_move_eval_cp": 12,
                    "eval_delta": 0,
                    "classification": "excellent",
                    "decision_source": "backend_engine",
                },
            ]
        },
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    extra_session_id = create_game_session(user_id=user_id, player_color="white")
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=uuid.UUID(extra_session_id),
            occurred_at=datetime.now(timezone.utc),
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_after_e4,
        player_color="white",
    )

    assert move_san == "e5"
    assert target_blunder_id is not None


def test_session_moves_skip_invalid_ghost_graph_edge(client, auth_headers, create_game_session, db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    forged_target_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    target_position = Position(
        user_id=user_id,
        fen_hash=fen_hash(forged_target_fen),
        fen_raw=forged_target_fen,
        active_color="white",
    )
    db_session.add(target_position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=user_id,
            position_id=target_position.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
    )
    db_session.commit()

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "c5",
                    "fen_before": fen_after_e4,
                    "fen_after": forged_target_fen,
                    "eval_cp": 12,
                    "best_move_san": "e5",
                    "best_move_eval_cp": 12,
                    "eval_delta": 0,
                    "classification": "excellent",
                    "decision_source": "backend_engine",
                },
            ]
        },
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == uuid.UUID(session_id))
        .count()
        == 1
    )

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_after_e4,
        player_color="white",
    )

    assert move_san is None
    assert target_blunder_id is None


def test_session_moves_upsert_idempotent(client, auth_headers, create_game_session, db_session):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)

    db_session.execute(
        text("""
            INSERT INTO positions (user_id, fen_hash, fen_raw, active_color)
            VALUES (123, 'obs-hash', 'obs-fen', 'white')
        """)
    )
    position_id = db_session.execute(text("SELECT id FROM positions WHERE fen_hash = 'obs-hash'")).scalar_one()
    db_session.execute(
        text("""
            INSERT INTO blunders (user_id, position_id, bad_move_san, best_move_san, eval_loss_cp)
            VALUES (123, :position_id, 'e4', 'Nf3', 120)
        """),
        {"position_id": position_id},
    )
    blunder_id = db_session.execute(text("SELECT id FROM blunders WHERE position_id = :position_id"), {"position_id": position_id}).scalar_one()
    db_session.commit()

    first = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1",
                    "eval_cp": 30,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 30,
                    "eval_delta": 0,
                    "classification": "best",
                    "decision_source": "ghost_path",
                    "target_blunder_id": blunder_id,
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert first.status_code == 200
    assert first.json()["moves_inserted"] == 1

    second = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "d4",
                    "fen_after": "fen-2",
                    "eval_cp": -10,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 30,
                    "classification": "good",
                    "decision_source": "local_fallback",
                    "target_blunder_id": None,
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert second.status_code == 200
    assert second.json()["moves_inserted"] == 1

    count = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .count()
    )
    assert count == 1

    row = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == session_uuid,
            SessionMove.move_number == 1,
            SessionMove.color == "white",
        )
        .first()
    )
    assert row is not None
    assert row.move_san == "d4"
    assert row.fen_after == "fen-2"
    assert row.eval_cp == -10
    assert row.best_move_san == "e4"
    assert row.best_move_eval_cp == 20
    assert row.eval_delta == 30
    assert row.classification == "good"
    assert row.decision_source == "local_fallback"
    assert row.target_blunder_id is None


def test_session_moves_session_not_found(client, auth_headers):
    response = client.post(
        "/api/session/00000000-0000-0000-0000-000000000000/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_session_moves_wrong_user_forbidden(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen",
                }
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_session_moves_duplicate_payload_rejected(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-a",
                },
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "d4",
                    "fen_after": "fen-b",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 422
    assert "duplicate move entry" in response.json()["detail"].lower()


def test_session_moves_succeeds_when_opening_cache_refresh_fails(
    client,
    auth_headers,
    create_game_session,
    db_session,
):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)

    # Exercise the real best-effort facade (not a stub) and force the underlying
    # scheduler enqueue to raise. The facade must swallow it so /moves stays 200.
    with patch(
        "app.api.session.request_recompute", real_request_recompute_facade
    ), patch(
        "app.opening_score_scheduler.OpeningScoreScheduler.request_recompute",
        side_effect=RuntimeError("boom"),
    ):
        response = client.post(
            f"/api/session/{session_id}/moves",
            json={
                "moves": [
                    {
                        "move_number": 1,
                        "color": "white",
                        "move_san": "e4",
                        "fen_after": "fen-1",
                    }
                ]
            },
            headers=auth_headers(user_id=123),
        )

    assert response.status_code == 200
    assert response.json() == {"moves_inserted": 1, "line_revision": 0}
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid, SessionMove.move_number == 1, SessionMove.color == "white")
        .count()
        == 1
    )


def test_session_moves_defers_opportunity_events_off_request_path(
    client, auth_headers, create_game_session, db_session
):
    """Acceptance (dialect-independent): the opportunity-event stage is NOT on the
    /moves request-latency path.

    Seed a reachable blunder so a *successful* evidence run WOULD write a
    BlunderOpportunityEvent. Patch ``enqueue_session_evidence`` with a MagicMock
    (this nested patch overrides the autouse synchronous shim for this test only)
    so the deferred pipeline never runs inline. The response must still be 200 with
    the session_moves committed, the enqueue must be called exactly once carrying
    the evidence payload, and NO opportunity event may exist immediately after the
    response — proving the work is deferred. Re-POSTing without the mock (the
    autouse sync shim) then runs the deferred work and produces the event,
    confirming the seed is genuinely opportunity-producing.
    """
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    session_uuid = uuid.UUID(session_id)
    fen_start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    # A reachable blunder at the player position the upload lands on: a successful
    # evidence run would record an opportunity event (reached=True).
    target_position = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_after_e4),
        fen_raw=fen_after_e4,
        active_color="black",
    )
    db_session.add(target_position)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=target_position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(blunder)
    db_session.commit()

    payload = {
        "moves": [
            {
                "move_number": 1,
                "color": "white",
                "move_san": "e4",
                "fen_before": fen_start,
                "fen_after": fen_after_e4,
                "eval_cp": 20,
                "best_move_san": "e4",
                "best_move_eval_cp": 20,
                "eval_delta": 0,
                "classification": "best",
            }
        ]
    }

    enqueue_mock = MagicMock()
    with patch("app.api.session.enqueue_session_evidence", enqueue_mock):
        response = client.post(
            f"/api/session/{session_id}/moves",
            json=payload,
            headers=auth_headers(user_id=user_id),
        )

    # (1) 200 + session_moves committed durably.
    assert response.status_code == 200
    assert response.json()["moves_inserted"] == 1
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .count()
        == 1
    )

    # (2) The deferred work was enqueued exactly once with the expected identity
    # and the full evidence payload — not run inline.
    enqueue_mock.assert_called_once()
    kwargs = enqueue_mock.call_args.kwargs
    assert kwargs["session_id"] == session_uuid
    assert kwargs["user_id"] == user_id
    assert kwargs["player_color"] == "white"
    assert len(kwargs["evidence_moves"]) == 1

    # (3) The opportunity-event stage did NOT run in the request path.
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter(BlunderOpportunityEvent.session_id == session_uuid)
        .count()
        == 0
    )

    # Control: re-POST without the mock (autouse sync shim runs the deferred work)
    # — the same seed now produces the opportunity event, so the count==0 above is
    # a genuine deferral, not a setup that could never have produced an event.
    response = client.post(
        f"/api/session/{session_id}/moves",
        json=payload,
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200
    db_session.expire_all()
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter(BlunderOpportunityEvent.session_id == session_uuid)
        .count()
        == 1
    )


def test_session_moves_recompute_opportunity_false_skips_event(
    client, auth_headers, create_game_session, db_session, _no_op_recompute_scheduler
):
    """g-y90g: ``recompute_opportunity=false`` skips ONLY the opportunity recompute.

    With a reachable blunder seeded, a default/true upload writes a
    BlunderOpportunityEvent (the existing tests cover that). A ``false`` upload
    must write NO opportunity event, yet still run the non-gated stages — proved
    here by the opening-score ``request_recompute`` (stubbed) still firing. A
    follow-up ``true`` upload then produces the event, confirming the seed is
    genuinely opportunity-producing and the skip was the flag, not the setup.
    """
    session_stub, _srs_stub = _no_op_recompute_scheduler
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    session_uuid = uuid.UUID(session_id)
    fen_start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    target_position = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_after_e4),
        fen_raw=fen_after_e4,
        active_color="black",
    )
    db_session.add(target_position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=user_id,
            position_id=target_position.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
            created_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
    )
    db_session.commit()

    move_payload = {
        "move_number": 1,
        "color": "white",
        "move_san": "e4",
        "fen_before": fen_start,
        "fen_after": fen_after_e4,
        "eval_cp": 20,
        "best_move_san": "e4",
        "best_move_eval_cp": 20,
        "eval_delta": 0,
        "classification": "best",
    }

    # (1) recompute_opportunity=false — moves persist, opening recompute still
    # fires (non-gated stage), but NO opportunity event is written.
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [move_payload], "recompute_opportunity": False},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .count()
        == 1
    )
    assert session_stub.called  # opening-score recompute is NOT gated
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter(BlunderOpportunityEvent.session_id == session_uuid)
        .count()
        == 0
    )

    # (2) recompute_opportunity=true — the same seed now produces the event.
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [move_payload], "recompute_opportunity": True},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200
    db_session.expire_all()
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter(BlunderOpportunityEvent.session_id == session_uuid)
        .count()
        == 1
    )


def test_session_analysis_success(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5 2. Nf3",
        },
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200

    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 2,
                    "color": "white",
                    "move_san": "Nf3",
                    "fen_after": "fen-2w",
                    "eval_cp": 30,
                    "eval_mate": None,
                    "best_move_san": "Nf3",
                    "best_move_eval_cp": 30,
                    "eval_delta": 0,
                    "classification": "excellent",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "fen-1b",
                    "eval_cp": 10,
                    "eval_mate": None,
                    "best_move_san": "c5",
                    "best_move_eval_cp": 25,
                    "eval_delta": 15,
                    "classification": "mistake",
                },
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1w",
                    "eval_cp": 20,
                    "eval_mate": None,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "inaccuracy",
                },
                {
                    "move_number": 2,
                    "color": "black",
                    "move_san": "Nc6",
                    "fen_after": "fen-2b",
                    "eval_cp": 5,
                    "eval_mate": None,
                    "best_move_san": "d6",
                    "best_move_eval_cp": 50,
                    "eval_delta": 45,
                    "classification": "blunder",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200

    data = response.json()
    assert data["session_id"] == session_id
    assert data["player_color"] == "white"
    assert data["result"] == "checkmate_win"
    assert data["pgn"] == "1. e4 e5 2. Nf3"
    # Both white (player) moves were evaluated with eval_delta 0: a genuine
    # perfect-play average of 0, NOT the "no data" null. Keep these apart.
    assert data["summary"] == {
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 1,
        "average_centipawn_loss": 0,
        "accuracy": 100,
    }
    assert [move["move_san"] for move in data["moves"]] == ["e4", "e5", "Nf3", "Nc6"]


def test_synthetic_checkmate_eval_persists_repairs_accuracy_and_skips_cache(
    client, auth_headers, create_game_session, db_session
):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="black")
    final_fen_before = (
        "rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2"
    )
    final_fen_after = (
        "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    )
    moves = [
        {
            "move_number": 1,
            "color": "white",
            "move_san": "f3",
            "fen_after": "fen-1w",
            "eval_cp": 10,
        },
        {
            "move_number": 1,
            "color": "black",
            "move_san": "e5",
            "fen_after": "fen-1b",
            "eval_cp": 20,
        },
        {
            "move_number": 2,
            "color": "white",
            "move_san": "g4",
            "fen_after": "fen-2w",
            "eval_cp": -500,
        },
        {
            "move_number": 2,
            "color": "black",
            "move_san": "Qh4#",
            "fen_before": final_fen_before,
            "fen_after": final_fen_after,
            "move_uci": "d8h4",
            "eval_cp": 10000,
            "eval_mate": 0,
            "eval_delta": 0,
            "synthetic_terminal_eval": True,
        },
    ]

    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )
    assert upload.status_code == 200
    end = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. f3 e5 2. g4 Qh4#",
        },
        headers=auth_headers(user_id=user_id),
    )
    assert end.status_code == 200

    analysis = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert analysis.status_code == 200
    assert analysis.json()["summary"]["accuracy"] is not None
    stored = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == uuid.UUID(session_id),
            SessionMove.move_number == 2,
            SessionMove.color == "black",
        )
        .one()
    )
    assert (stored.eval_cp, stored.eval_mate, stored.eval_delta) == (10000, 0, 0)
    assert (
        db_session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == final_fen_before,
            AnalysisCache.move_uci == "d8h4",
        )
        .count()
        == 0
    )


def test_synthetic_threefold_draw_skips_cache_but_unflagged_sparse_eval_caches(
    client, auth_headers, create_game_session, db_session
):
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    final_fen_before = (
        "rnbqkb1r/pppppppp/5n2/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 7 4"
    )
    moves = []
    sans = ["Nf3", "Nf6", "Ng1", "Ng8", "Nf3", "Nf6", "Ng1", "Ng8"]
    for ply, san in enumerate(sans):
        moves.append(
            {
                "move_number": ply // 2 + 1,
                "color": "white" if ply % 2 == 0 else "black",
                "move_san": san,
                "fen_after": f"fen-{ply}",
                "eval_cp": 0,
            }
        )
    moves[-1].update(
        {
            "fen_before": final_fen_before,
            "move_uci": "f6g8",
            "synthetic_terminal_eval": True,
            # Valid provenance so the row is refused for being SYNTHETIC, not for
            # declaring a retired profile.
            "provenance": BROWSER_V2_PROVENANCE,
        }
    )
    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )
    assert upload.status_code == 200
    end = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": "1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8",
        },
        headers=auth_headers(user_id=user_id),
    )
    assert end.status_code == 200
    analysis = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert analysis.json()["summary"]["accuracy"] is not None
    assert (
        db_session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == final_fen_before,
            AnalysisCache.move_uci == "f6g8",
        )
        .count()
        == 0
    )

    contrast_session = create_game_session(user_id=user_id, player_color="white")
    contrast = client.post(
        f"/api/session/{contrast_session}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_before": STARTING_FEN,
                    "fen_after": "fen-e4",
                    "move_uci": "e2e4",
                    "eval_cp": 20,
                    "provenance": BROWSER_V2_PROVENANCE,
                }
            ]
        },
        headers=auth_headers(user_id=user_id),
    )
    assert contrast.status_code == 200
    cached = (
        db_session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == STARTING_FEN,
            AnalysisCache.move_uci == "e2e4",
        )
        .one()
    )
    assert cached.evidence_contract_id == "minimal-played-eval-v1"


def test_session_analysis_empty_moves_returns_null_avg_cpl(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    assert response.json()["moves"] == []
    # No move rows -> no player CPL: null, not 0.
    assert response.json()["summary"] == {
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 0,
        "average_centipawn_loss": None,
        "accuracy": None,
    }


def test_session_analysis_avg_cpl_null_when_no_player_move_evaluated(
    client, auth_headers, create_game_session
):
    """Move rows exist, but no PLAYER move has an eval_delta -> null.

    The opponent's moves ARE evaluated, so a non-null result would prove the average
    leaked past the player-only restriction in player_loss_expr.
    """
    session_id = create_game_session(user_id=123, player_color="white")

    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                # Player (white): unanalyzed — no eval_delta.
                {
                    "move_number": 1, "color": "white", "move_san": "e4",
                    "fen_after": "fen-1w",
                },
                {
                    "move_number": 2, "color": "white", "move_san": "Nf3",
                    "fen_after": "fen-2w",
                },
                # Opponent (black): evaluated.
                {
                    "move_number": 1, "color": "black", "move_san": "e5",
                    "fen_after": "fen-1b", "eval_delta": 60, "classification": "blunder",
                },
                {
                    "move_number": 2, "color": "black", "move_san": "Nc6",
                    "fen_after": "fen-2b", "eval_delta": 40, "classification": "mistake",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    assert response.json()["summary"]["average_centipawn_loss"] is None


def test_session_analysis_avg_cpl_averages_evaluated_subset_when_partially_analyzed(
    client, auth_headers, create_game_session
):
    """Partial analysis is NOT gated: average over the plies that resolved.

    Only one of the two player moves is evaluated, so the result is that move's loss
    (40) — not None, and not 20 (which would average in the unevaluated ply).
    """
    session_id = create_game_session(user_id=123, player_color="white")

    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1, "color": "white", "move_san": "e4",
                    "fen_after": "fen-1w", "eval_delta": 40, "classification": "mistake",
                },
                # Player's second move never got analyzed.
                {
                    "move_number": 2, "color": "white", "move_san": "Nf3",
                    "fen_after": "fen-2w",
                },
                {
                    "move_number": 1, "color": "black", "move_san": "e5",
                    "fen_after": "fen-1b", "eval_delta": 200, "classification": "blunder",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    assert response.json()["summary"]["average_centipawn_loss"] == 40


def test_session_analysis_average_cpl_uses_player_moves_and_clamps_negative_delta(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="black")

    # Upload BEFORE ending: the terminal reconcile (g-short-move-rows) would
    # otherwise derive the PGN's four plies into an empty session, and this
    # fixture deliberately stores only three rows (no 2. white).
    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1w",
                    "eval_delta": 200,
                    "classification": "blunder",
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "fen-1b",
                    "eval_delta": -30,
                    "classification": "best",
                },
                {
                    "move_number": 2,
                    "color": "black",
                    "move_san": "Nc6",
                    "fen_after": "fen-2b",
                    "eval_delta": 40,
                    "classification": "mistake",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": "1. e4 e5 2. Nf3 Nc6",
        },
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200

    stored_negative = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == uuid.UUID(session_id),
            SessionMove.move_number == 1,
            SessionMove.color == "black",
        )
        .one()
    )
    assert stored_negative.eval_delta == 0

    # Simulate historical rows that predate write-side normalization.
    db_session.execute(
        text("""
            UPDATE session_moves
            SET eval_delta = -30
            WHERE session_id = :session_id AND move_number = 1 AND color = 'black'
        """),
        {"session_id": session_id},
    )
    db_session.commit()

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200

    data = response.json()
    assert data["summary"]["blunders"] == 0
    assert data["summary"]["mistakes"] == 1
    assert data["summary"]["average_centipawn_loss"] == 20  # black moves: (0+40)/2
    assert [move["eval_delta"] for move in data["moves"]] == [200, 0, 40]


def test_session_analysis_session_not_found(client, auth_headers):
    response = client.get(
        "/api/session/00000000-0000-0000-0000-000000000000/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_session_analysis_wrong_user_forbidden(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def _convert_to_drill(db_session, session_id: str, rated_start_ply: int) -> None:
    """Mark an existing session as a converted drill (one full normal game).

    rated_start_ply marks the drill-prefix boundary: plies <= rated_start_ply
    are segment='drill', the rest 'normal'. Satisfies the converted-drill CHECK
    constraint (rated + normal_started_at + converted_at + rated_start_ply set).
    """
    db_session.execute(
        text("""
            UPDATE game_sessions
            SET session_mode = 'drill',
                drill_state = 'converted',
                is_rated = true,
                normal_started_at = started_at,
                converted_at = started_at,
                rated_start_ply = :rsp
            WHERE id = :sid
        """),
        {"sid": session_id, "rsp": rated_start_ply},
    )
    db_session.commit()


def test_session_analysis_converted_drill_includes_drill_prefix_summary(
    client, auth_headers, create_game_session, db_session
):
    # Amended drill policy (2026-06-01): a converted drill is one full normal game.
    # Player prefix classifications still count; CPL averages the player's moves only.
    session_id = create_game_session(user_id=123, player_color="white")
    end_response = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": "checkmate_win", "pgn": "1. e4 e5 2. Nf3"},
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200
    # ply boundary 2: move 1 (white+black) is drill-prefix, move 2 onward is normal.
    _convert_to_drill(db_session, session_id, rated_start_ply=2)

    upload_response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1, "color": "white", "move_san": "e4",
                    "fen_after": "fen-1w", "eval_delta": 200, "classification": "blunder",
                },
                {
                    "move_number": 1, "color": "black", "move_san": "e5",
                    "fen_after": "fen-1b", "eval_delta": 10, "classification": "good",
                },
                {
                    # Normal-segment mistake (ply 3).
                    "move_number": 2, "color": "white", "move_san": "Nf3",
                    "fen_after": "fen-2w", "eval_delta": 40, "classification": "mistake",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert upload_response.status_code == 200

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    summary = response.json()["summary"]
    # Drill-prefix blunder is counted; CPL averages only the player's moves.
    assert summary["blunders"] == 1
    assert summary["mistakes"] == 1
    assert summary["average_centipawn_loss"] == round((200 + 40) / 2)


# --- g-dckw analysis-cache-write instrumentation (cohort key + finality) --------


def test_timed_side_effect_renders_extra_and_body_stamped_fields(caplog):
    """g-dckw: _timed_side_effect appends **extra fields and a body-stamped field
    (cache_row_count, known only after the upload is filtered) between move_count
    and elapsed_ms, so the analysis_cache_write line is cohortable on the
    submitted-row count + upload finality rather than the overcounting move_count.
    That count is what was SUBMITTED to the writer, not what it wrote — see
    cache_rows_written (g-bgv1-cutover) for the written count."""
    from app.api.session import _timed_side_effect

    sid = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="app.api.session"):
        with _timed_side_effect(
            "analysis_cache_write",
            session_id=sid,
            user_id=7,
            move_count=8,
            cache_row_count=0,  # seed; overwritten by the body below
            final=True,
            kind="final",
        ) as fields:
            fields["cache_row_count"] = 5

    line = next(
        r.getMessage()
        for r in caplog.records
        if "side_effect=analysis_cache_write" in r.getMessage()
    )
    assert "move_count=8" in line
    assert "cache_row_count=5" in line  # body-stamped value, not the seed 0
    assert "final=True" in line
    assert "kind=final" in line
    assert "elapsed_ms=" in line
    # The extra fields sit between move_count and elapsed_ms (the source format).
    assert (
        line.index("move_count=8")
        < line.index("cache_row_count=5")
        < line.index("elapsed_ms=")
    )


def test_upsert_analysis_cache_returns_submitted_row_count():
    """g-dckw cohort key: _upsert_analysis_cache returns len(cache_values) — the
    rows SUBMITTED to the writer — NOT the uploaded move_count, and NOT the count
    the writer went on to store (g-bgv1-cutover; see cache_rows_written). Moves with
    no fen_before/move_uci or no eval are filtered before the writer, so the
    return undercounts move_count for those (why move_count can't bucket the
    latency cohort)."""
    from app.api.session import MoveColor, SessionMoveInput, _upsert_analysis_cache
    from app.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    moves = [
        # Valid: fen_before + move_uci + a played eval -> minimal-played-eval.
        SessionMoveInput(
            move_number=1, color=MoveColor.WHITE, move_san="e4",
            fen_after="after-1", fen_before=STARTING_FEN, move_uci="e2e4", eval_cp=20,
        ),
        SessionMoveInput(
            move_number=1, color=MoveColor.BLACK, move_san="e5",
            fen_after="after-2", fen_before="pos-2", move_uci="e7e5", eval_cp=-10,
        ),
        # Filtered: no eval at all -> dropped before the writer.
        SessionMoveInput(
            move_number=2, color=MoveColor.WHITE, move_san="Nf3",
            fen_after="after-3", fen_before="pos-3", move_uci="g1f3",
        ),
        # Filtered: no fen_before/move_uci key -> dropped before the writer.
        SessionMoveInput(
            move_number=2, color=MoveColor.BLACK, move_san="Nc6",
            fen_after="after-4", eval_cp=5,
        ),
    ]
    written = _upsert_analysis_cache(db, moves)
    db.close()
    engine.dispose()

    assert written == 2  # 2 of 4 uploaded moves reached the writer

    # And an all-filtered batch reports zero (never a phantom move_count).
    engine2 = create_engine("sqlite://")
    Base.metadata.create_all(engine2)
    db2 = sessionmaker(bind=engine2)()
    only_filtered = [
        SessionMoveInput(
            move_number=3, color=MoveColor.WHITE, move_san="Bb5",
            fen_after="after-5", fen_before="pos-5", move_uci="f1b5",
        ),
    ]
    assert _upsert_analysis_cache(db2, only_filtered) == 0
    db2.close()
    engine2.dispose()


def test_analysis_cache_write_logs_true_count_and_error_status_on_writer_failure(
    monkeypatch, caplog
):
    """g-dckw: a writer that raises AFTER receiving a non-empty batch must not be
    logged as cache_row_count=0 (which would drop a slow failed write into the
    zero-row latency cohort). The count is stamped before the write, and status
    stays 'error' (never flipped to ok), so the scrape can exclude the failure."""
    import app.api.session as session_mod
    from app.api.session import (
        MoveColor,
        SessionMoveInput,
        _timed_side_effect,
        _upsert_analysis_cache,
    )
    from app.models import Base

    def boom(db, rows):
        raise RuntimeError("simulated writer failure")

    monkeypatch.setattr(session_mod, "write_analysis_cache_rows", boom)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    moves = [
        SessionMoveInput(
            move_number=1, color=MoveColor.WHITE, move_san="e4",
            fen_after="after-1", fen_before=STARTING_FEN, move_uci="e2e4", eval_cp=20,
        ),
    ]
    sid = uuid.uuid4()

    with caplog.at_level(logging.INFO, logger="app.api.session"):
        with pytest.raises(RuntimeError):
            with _timed_side_effect(
                "analysis_cache_write",
                session_id=sid, user_id=7, move_count=1,
                cache_row_count=0, final=True, kind="final", status="error",
            ) as cache_fields:
                _upsert_analysis_cache(db, moves, timing_fields=cache_fields)
                cache_fields["status"] = "ok"  # never reached — the writer raised

    db.close()
    engine.dispose()

    line = next(
        r.getMessage()
        for r in caplog.records
        if "side_effect=analysis_cache_write" in r.getMessage()
    )
    assert "cache_row_count=1" in line  # true filtered count, NOT the seed 0
    assert "status=error" in line  # never flipped to ok -> excluded from the scrape


# --- g-no51 RAW evidence vs NORMALIZED display/decision CPL --------------------


def test_browser_cache_upload_persists_raw_delta_uncapped_but_capped_everywhere_else(
    client, auth_headers, create_game_session, db_session
):
    """g-no51: a browser-game upload persists RAW evidence (uncapped) into
    analysis_cache while every display/decision read is NORMALIZED (floored at 0,
    capped at CENTIPAWN_LOSS_CAP_CP=1000).

    This direct-upload assertion is the only guard on the analysis_cache write path.
    A browser row classifies as the minimal ``minimal-played-eval-v1`` contract,
    which validates only that ``played_eval`` is a finite number and does NOT check
    ``eval_delta`` equality — so an accidental cap on the raw write would satisfy
    every contract check and slip through unnoticed, surfacing only as a silently
    capped raw delta on ``/api/analysis/lookup``.

    One white move (best +10000, played -20) yields raw ``eval_delta`` 10020. The
    cache row keeps 10020 (``clamp_delta_nonneg``: floor 0, no upper cap); the
    session_moves row and the session-analysis per-move echo both show 1000
    (``centipawn_loss``: floor 0, cap 1000); and ``/lookup`` returns the RAW 10020
    unchanged.
    """
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_before": STARTING_FEN,
                    "fen_after": fen_after_e4,
                    "move_uci": "e2e4",
                    # White move: eval_cp is already white-relative -> played_eval -20.
                    "eval_cp": -20,
                    "best_move_eval_cp": 10000,  # -> best_eval 10000
                    # RAW delta best - played = 10000 - (-20) = 10020 (> the 1000 cap).
                    "eval_delta": 10020,
                    "classification": "blunder",
                    "provenance": BROWSER_V2_PROVENANCE,
                }
            ]
        },
        headers=auth_headers(user_id=user_id),
    )
    assert upload.status_code == 200
    assert upload.json()["moves_inserted"] == 1

    # session_moves: NORMALIZED write via centipawn_loss (capped at 1000).
    stored = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == uuid.UUID(session_id),
            SessionMove.move_number == 1,
            SessionMove.color == "white",
        )
        .one()
    )
    assert stored.eval_delta == 1000

    # analysis_cache: RAW evidence via clamp_delta_nonneg (floor 0, NO upper cap).
    cached = (
        db_session.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == STARTING_FEN,
            AnalysisCache.move_uci == "e2e4",
        )
        .one()
    )
    assert cached.eval_delta == 10020
    # The row really is the minimal contract that skips delta-equality validation,
    # which is exactly why the direct-upload assertion above is load-bearing.
    assert cached.evidence_contract_id == "minimal-played-eval-v1"

    # Session-analysis per-move echo: NORMALIZED via centipawn_loss (capped 1000).
    analysis = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert analysis.status_code == 200
    echoed = next(m for m in analysis.json()["moves"] if m["move_san"] == "e4")
    assert echoed["eval_delta"] == 1000

    # /api/analysis/lookup: returns the RAW analysis_cache value unchanged.
    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(user_id=user_id),
    )
    assert lookup.status_code == 200
    result = lookup.json()["results"][f"{STARTING_FEN}::e2e4"]
    assert result["eval_delta"] == 10020


def test_session_analysis_caps_historical_uncapped_session_move_delta_at_read(
    client, auth_headers, create_game_session, db_session
):
    """g-no51: a genuinely-historical session_moves row (written before write-side
    normalization existed) can hold a raw uncapped ``eval_delta`` at rest. The read
    paths must normalize it on the way out: the summary Avg CPL
    (``centipawn_loss_expr``) and the per-move echo (``centipawn_loss``) both report
    1000 for a 10000 delta, even though the value stored in the row exceeds the cap.
    """
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    upload = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": "fen-1w",
                    "eval_delta": 40,
                    "classification": "mistake",
                }
            ]
        },
        headers=auth_headers(user_id=user_id),
    )
    assert upload.status_code == 200
    # In-range value is written through unchanged by write-side normalization.
    stored = (
        db_session.query(SessionMove)
        .filter(
            SessionMove.session_id == uuid.UUID(session_id),
            SessionMove.move_number == 1,
            SessionMove.color == "white",
        )
        .one()
    )
    assert stored.eval_delta == 40

    # Simulate a genuinely-historical row that predates write-side normalization:
    # a raw uncapped delta lands directly in session_moves. NB: the UUID(as_uuid)
    # session_id is stored as dashless hex under SQLite, so bind that form (a plain
    # dashed string matches zero rows).
    result = db_session.execute(
        text("""
            UPDATE session_moves
            SET eval_delta = 10000
            WHERE session_id = :session_id AND move_number = 1 AND color = 'white'
        """),
        {"session_id": uuid.UUID(session_id).hex},
    )
    assert result.rowcount == 1  # the historical row was actually rewritten
    db_session.commit()

    analysis = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=user_id),
    )
    assert analysis.status_code == 200
    data = analysis.json()
    # Summary Avg CPL (centipawn_loss_expr) caps the lone player move at 1000.
    assert data["summary"]["average_centipawn_loss"] == 1000
    # Per-move echo (centipawn_loss) also caps at 1000.
    echoed = next(m for m in data["moves"] if m["move_san"] == "e4")
    assert echoed["eval_delta"] == 1000
