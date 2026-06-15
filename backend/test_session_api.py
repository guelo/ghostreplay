import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.fen import fen_hash
from app.models import Blunder, BlunderOpportunityEvent, Position, SessionMove


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
    assert response.json() == {"moves_inserted": 2}

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

    move_san, target_blunder_id, _, _ = find_ghost_move(
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

    move_san, target_blunder_id, _, _ = find_ghost_move(
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
    from app.opening_score_scheduler import request_recompute as real_request_recompute

    with patch("app.api.session.request_recompute", real_request_recompute), patch(
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
    assert response.json() == {"moves_inserted": 1}
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid, SessionMove.move_number == 1, SessionMove.color == "white")
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
    assert data["summary"] == {
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 1,
        "average_centipawn_loss": 0,
        "accuracy": 100,
    }
    assert [move["move_san"] for move in data["moves"]] == ["e4", "e5", "Nf3", "Nc6"]


def test_session_analysis_empty_moves_returns_zero_summary(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200
    assert response.json()["moves"] == []
    assert response.json()["summary"] == {
        "blunders": 0,
        "mistakes": 0,
        "inaccuracies": 0,
        "average_centipawn_loss": 0,
        "accuracy": None,
    }


def test_session_analysis_average_cpl_uses_player_moves_and_clamps_negative_delta(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="black")

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
