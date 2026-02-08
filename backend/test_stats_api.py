import uuid
from datetime import datetime, timedelta, timezone

from app.models import Blunder, GameSession, Move, Position


def _end_game(client, auth_headers, session_id, user_id=123, result="checkmate_win"):
    response = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": result, "pgn": "1. e4 e5"},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _upload_moves(client, auth_headers, session_id, moves, user_id=123):
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _set_session_times(
    db_session,
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime | None,
):
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    session.started_at = started_at
    session.ended_at = ended_at
    db_session.commit()


def _insert_library_data(db_session, *, user_id: int, now: datetime):
    p1 = Position(
        user_id=user_id,
        fen_hash="hash-a",
        fen_raw="fen-a",
        active_color="white",
    )
    p2 = Position(
        user_id=user_id,
        fen_hash="hash-b",
        fen_raw="fen-b",
        active_color="black",
    )
    p3 = Position(
        user_id=user_id,
        fen_hash="hash-c",
        fen_raw="fen-c",
        active_color="white",
    )
    db_session.add_all([p1, p2, p3])
    db_session.flush()

    db_session.add(
        Move(
            from_position_id=p1.id,
            move_san="e4",
            to_position_id=p2.id,
        )
    )
    db_session.add(
        Move(
            from_position_id=p2.id,
            move_san="e5",
            to_position_id=p3.id,
        )
    )

    db_session.add_all(
        [
            Blunder(
                user_id=user_id,
                position_id=p1.id,
                bad_move_san="Qxh7+",
                best_move_san="Re1",
                eval_loss_cp=300,
                created_at=now - timedelta(days=2),
            ),
            Blunder(
                user_id=user_id,
                position_id=p2.id,
                bad_move_san="Bxf7+",
                best_move_san="O-O",
                eval_loss_cp=150,
                created_at=now - timedelta(days=10),
            ),
            Blunder(
                user_id=999,
                position_id=p3.id,
                bad_move_san="Qh5",
                best_move_san="Nc3",
                eval_loss_cp=999,
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    db_session.commit()


def test_stats_summary_empty_dataset(client, auth_headers):
    response = client.get("/api/stats/summary", headers=auth_headers(user_id=123))
    assert response.status_code == 200

    data = response.json()
    assert data["window_days"] == 30
    assert data["games"] == {
        "played": 0,
        "completed": 0,
        "active": 0,
        "record": {
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "resigns": 0,
            "abandons": 0,
        },
        "avg_duration_seconds": 0,
        "avg_moves": 0.0,
    }
    assert data["moves"] == {
        "player_moves": 0,
        "avg_cpl": 0.0,
        "mistakes_per_100_moves": 0.0,
        "blunders_per_100_moves": 0.0,
        "quality_distribution": {
            "best": 0.0,
            "excellent": 0.0,
            "good": 0.0,
            "inaccuracy": 0.0,
            "mistake": 0.0,
            "blunder": 0.0,
        },
    }
    assert data["colors"]["white"]["games"] == 0
    assert data["colors"]["black"]["games"] == 0
    assert data["library"]["blunders_total"] == 0
    assert data["library"]["positions_total"] == 0
    assert data["library"]["edges_total"] == 0
    assert data["library"]["new_blunders_in_window"] == 0
    assert data["library"]["top_costly_blunders"] == []
    assert data["data_completeness"]["sessions_with_uploaded_moves_pct"] == 0.0
    assert len(data["data_completeness"]["notes"]) == 2


def test_stats_summary_mixed_data(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)

    white_session = create_game_session(user_id=123, player_color="white")
    black_session = create_game_session(user_id=123, player_color="black")
    create_game_session(user_id=123, player_color="white")  # active session

    _end_game(client, auth_headers, white_session, user_id=123, result="checkmate_win")
    _end_game(client, auth_headers, black_session, user_id=123, result="checkmate_loss")

    _upload_moves(
        client,
        auth_headers,
        white_session,
        [
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
                "eval_delta": 150,
                "classification": "blunder",
            },
        ],
    )
    _upload_moves(
        client,
        auth_headers,
        black_session,
        [
            {
                "move_number": 1,
                "color": "white",
                "move_san": "d4",
                "fen_after": "fen-2w",
                "eval_delta": 0,
                "classification": "best",
            },
            {
                "move_number": 1,
                "color": "black",
                "move_san": "Nf6",
                "fen_after": "fen-2b",
                "eval_delta": 60,
                "classification": "mistake",
            },
            {
                "move_number": 2,
                "color": "white",
                "move_san": "c4",
                "fen_after": "fen-3w",
                "eval_delta": 100,
                "classification": "inaccuracy",
            },
        ],
    )

    _insert_library_data(db_session, user_id=123, now=now)

    response = client.get("/api/stats/summary", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    data = response.json()

    assert data["games"]["played"] == 3
    assert data["games"]["completed"] == 2
    assert data["games"]["active"] == 1
    assert data["games"]["record"] == {
        "wins": 1,
        "losses": 1,
        "draws": 0,
        "resigns": 0,
        "abandons": 0,
    }
    assert data["games"]["avg_moves"] == 1.7

    assert data["moves"]["player_moves"] == 2
    assert data["moves"]["avg_cpl"] == 130.0
    assert data["moves"]["mistakes_per_100_moves"] == 50.0
    assert data["moves"]["blunders_per_100_moves"] == 50.0
    assert data["moves"]["quality_distribution"] == {
        "best": 0.0,
        "excellent": 0.0,
        "good": 0.0,
        "inaccuracy": 0.0,
        "mistake": 50.0,
        "blunder": 50.0,
    }

    assert data["colors"]["white"] == {
        "games": 2,
        "completed": 1,
        "wins": 1,
        "losses": 0,
        "draws": 0,
        "avg_cpl": 200.0,
        "blunders_per_100_moves": 100.0,
    }
    assert data["colors"]["black"] == {
        "games": 1,
        "completed": 1,
        "wins": 0,
        "losses": 1,
        "draws": 0,
        "avg_cpl": 60.0,
        "blunders_per_100_moves": 0.0,
    }

    assert data["library"]["blunders_total"] == 2
    assert data["library"]["positions_total"] == 3
    assert data["library"]["edges_total"] == 2
    assert data["library"]["new_blunders_in_window"] == 2
    assert data["library"]["avg_blunder_eval_loss_cp"] == 225
    assert len(data["library"]["top_costly_blunders"]) == 2
    assert data["library"]["top_costly_blunders"][0]["eval_loss_cp"] == 300
    assert data["library"]["top_costly_blunders"][1]["eval_loss_cp"] == 150

    assert data["data_completeness"]["sessions_with_uploaded_moves_pct"] == 66.7


def test_stats_summary_window_filtering(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)

    old_session = create_game_session(user_id=123, player_color="white")
    recent_session = create_game_session(user_id=123, player_color="black")
    recent_active = create_game_session(user_id=123, player_color="white")

    _end_game(client, auth_headers, old_session, user_id=123, result="draw")
    _end_game(client, auth_headers, recent_session, user_id=123, result="resign")

    _set_session_times(
        db_session,
        old_session,
        started_at=now - timedelta(days=40),
        ended_at=now - timedelta(days=39, hours=23),
    )
    _set_session_times(
        db_session,
        recent_session,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2, minutes=-30),
    )
    _set_session_times(
        db_session,
        recent_active,
        started_at=now - timedelta(days=1),
        ended_at=None,
    )

    _upload_moves(
        client,
        auth_headers,
        old_session,
        [
            {
                "move_number": 1,
                "color": "white",
                "move_san": "e4",
                "fen_after": "fen-old",
                "eval_delta": 20,
                "classification": "good",
            }
        ],
    )
    _upload_moves(
        client,
        auth_headers,
        recent_session,
        [
            {
                "move_number": 1,
                "color": "black",
                "move_san": "Nf6",
                "fen_after": "fen-recent",
                "eval_delta": 80,
                "classification": "inaccuracy",
            }
        ],
    )

    p1 = Position(
        user_id=123,
        fen_hash="window-h1",
        fen_raw="window-fen-1",
        active_color="white",
    )
    p2 = Position(
        user_id=123,
        fen_hash="window-h2",
        fen_raw="window-fen-2",
        active_color="black",
    )
    db_session.add_all([p1, p2])
    db_session.flush()
    db_session.add_all(
        [
            Blunder(
                user_id=123,
                position_id=p1.id,
                bad_move_san="a4",
                best_move_san="Nf3",
                eval_loss_cp=120,
                created_at=now - timedelta(days=40),
            ),
            Blunder(
                user_id=123,
                position_id=p2.id,
                bad_move_san="h4",
                best_move_san="e4",
                eval_loss_cp=220,
                created_at=now - timedelta(days=2),
            ),
        ]
    )
    db_session.commit()

    response_30 = client.get(
        "/api/stats/summary?window_days=30",
        headers=auth_headers(user_id=123),
    )
    assert response_30.status_code == 200
    data_30 = response_30.json()
    assert data_30["games"]["played"] == 2
    assert data_30["games"]["completed"] == 1
    assert data_30["games"]["active"] == 1
    assert data_30["games"]["record"]["resigns"] == 1
    assert data_30["games"]["record"]["draws"] == 0
    assert data_30["library"]["blunders_total"] == 2
    assert data_30["library"]["new_blunders_in_window"] == 1

    response_all = client.get(
        "/api/stats/summary?window_days=0",
        headers=auth_headers(user_id=123),
    )
    assert response_all.status_code == 200
    data_all = response_all.json()
    assert data_all["games"]["played"] == 3
    assert data_all["games"]["completed"] == 2
    assert data_all["games"]["record"]["draws"] == 1
    assert data_all["library"]["new_blunders_in_window"] == 2


def test_stats_summary_zero_denominator_with_sessions(client, auth_headers, create_game_session):
    session_id = create_game_session(user_id=123, player_color="white")
    _end_game(client, auth_headers, session_id, user_id=123, result="draw")

    response = client.get("/api/stats/summary", headers=auth_headers(user_id=123))
    assert response.status_code == 200

    data = response.json()
    assert data["moves"]["player_moves"] == 0
    assert data["moves"]["avg_cpl"] == 0.0
    assert data["moves"]["mistakes_per_100_moves"] == 0.0
    assert data["moves"]["blunders_per_100_moves"] == 0.0
    assert data["colors"]["white"]["avg_cpl"] == 0.0
    assert data["colors"]["white"]["blunders_per_100_moves"] == 0.0
    assert data["data_completeness"]["sessions_with_uploaded_moves_pct"] == 0.0


def test_stats_summary_window_days_validation(client, auth_headers):
    response = client.get("/api/stats/summary?window_days=31", headers=auth_headers(user_id=123))
    assert response.status_code == 422
