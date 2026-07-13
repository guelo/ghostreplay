"""
Tests for POST /api/blunder endpoint.

Run with: pytest test_blunder_api.py -v
"""
import concurrent.futures
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.fen import fen_hash
from app.models import GameSession
from app.opening_cache import current_evidence_seq
from conftest import pg_required
from sql_capture import capture_statements, cursor_last_before_commit, no_cursor_bump


def test_record_blunder_success(client, auth_headers, create_game_session):
    """Test successful blunder recording with simple PGN."""
    session_id = create_game_session(user_id=123, player_color="white")

    # PGN: 1. e4 e5 2. Nf3 Nc6 3. Bb5 a6
    # The blunder is 3...a6 but we're testing white's perspective
    # Let's use a PGN where white makes the last move (blunder)
    # 1. e4 e5 2. Qh5 (blunder)
    pgn = "1. e4 e5 2. Qh5"
    # FEN before Qh5: after 1. e4 e5
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    data = response.json()

    assert "blunder_id" in data
    assert "position_id" in data
    assert "positions_created" in data
    assert "is_new" in data
    assert data["is_new"] is True
    assert data["positions_created"] == 4  # Starting pos + after e4 + after e5 + after Qh5


def test_record_blunder_links_pre_move_position(client, auth_headers, create_game_session, db_session):
    """Test that blunder.position_id points to the pre-move position."""
    session_id = create_game_session(user_id=123, player_color="white")

    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    blunder_id = response.json()["blunder_id"]

    position_row = db_session.execute(
        text(
            "SELECT id FROM positions WHERE user_id = :user_id AND fen_hash = :fen_hash"
        ),
        {"user_id": 123, "fen_hash": fen_hash(fen_before_blunder)},
    ).fetchone()
    assert position_row is not None

    blunder_position_id = db_session.execute(
        text("SELECT position_id FROM blunders WHERE id = :id"),
        {"id": blunder_id},
    ).fetchone()[0]

    assert blunder_position_id == position_row[0]


def test_record_blunder_creates_positions_and_moves(client, auth_headers, create_game_session, db_session):
    """Test that all intermediate positions and moves are created."""
    session_id = create_game_session(user_id=123, player_color="white")

    pgn = "1. e4 e5 2. Nf3"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Nf3",
            "best_move": "Nc3",
            "eval_before": 50,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201

    # Check positions were created
    positions = db_session.execute(text("SELECT COUNT(*) FROM positions WHERE user_id = 123")).fetchone()[0]
    assert positions == 4  # Starting + after e4 + after e5 + after Nf3

    moves = db_session.execute(text("SELECT COUNT(*) FROM moves")).fetchone()[0]
    assert moves == 3  # e4, e5, Nf3


def test_record_blunder_session_not_found(client, auth_headers):
    """Test 404 when session doesn't exist."""
    fake_id = str(uuid.uuid4())

    response = client.post(
        "/api/blunder",
        json={
            "session_id": fake_id,
            "pgn": "1. e4 e5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 50,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_record_blunder_wrong_user(client, auth_headers, create_game_session):
    """Test 403 when session belongs to different user."""
    session_id = create_game_session(user_id=999, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_record_blunder_already_recorded_legacy_ambiguous(client, auth_headers, create_game_session):
    """A session recorded before idempotency bookkeeping (no key, no recorded id)
    cannot safely echo the blunder id, so a retry is LEGACY_AMBIGUOUS."""
    session_id = create_game_session(user_id=123, player_color="white", blunder_recorded=True)

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["error_code"] == "LEGACY_AMBIGUOUS"


def test_record_blunder_invalid_pgn(client, auth_headers, create_game_session):
    """Test 422 when PGN is malformed."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "not valid pgn at all!!!",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 422


def test_record_blunder_fen_mismatch(client, auth_headers, create_game_session):
    """Test 422 when pre-move FEN doesn't match PGN."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            # Wrong FEN - this is starting position, not after 1. e4 e5
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 422
    assert "mismatch" in response.json()["detail"].lower()


def test_record_blunder_rejects_after_first_10_full_moves(
    client, auth_headers, create_game_session
):
    """Auto-recording endpoint enforces first-10-full-moves cap."""
    session_id = create_game_session(user_id=123, player_color="white")

    # Repeat knight shuffles to reach white's 11th move.
    pgn = (
        "1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3 Nf6 4. Ng1 Ng8 5. Nf3 Nf6 "
        "6. Ng1 Ng8 7. Nf3 Nf6 8. Ng1 Ng8 9. Nf3 Nf6 10. Ng1 Ng8 11. Nf3"
    )
    fen_before_11th_move = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_11th_move,
            "user_move": "Nf3",
            "best_move": "d4",
            "eval_before": 40,
            "eval_after": -20,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 422
    assert "first 10 full moves" in response.json()["detail"].lower()


def test_record_blunder_wrong_color(client, auth_headers, create_game_session):
    """Test 400 when blunder position is opponent's move."""
    # Player is white, but PGN ends with black's move
    session_id = create_game_session(user_id=123, player_color="white")

    # 1. e4 e5 - e5 is black's move, so pre-blunder position has black to move
    pgn = "1. e4 e5"
    # FEN before e5 (after 1. e4): black to move
    fen_before_e5 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_e5,
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 30,
            "eval_after": 50,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 400
    assert "black to move" in response.json()["detail"].lower()


def test_record_blunder_black_player(client, auth_headers, create_game_session):
    """Test recording blunder when player is black."""
    session_id = create_game_session(user_id=123, player_color="black")

    # 1. e4 e5 - e5 is black's move
    pgn = "1. e4 e5"
    # FEN before e5: after 1. e4, black to move
    fen_before_e5 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_e5,
            "user_move": "e5",
            "best_move": "d5",
            "eval_before": 30,
            "eval_after": 50,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    data = response.json()
    assert data["is_new"] is True


def test_record_blunder_duplicate_position(client, auth_headers, create_game_session):
    """Test that same position in different games creates only one blunder."""
    # First game - record a blunder
    session1 = create_game_session(user_id=123, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response1 = client.post(
        "/api/blunder",
        json={
            "session_id": session1,
            "pgn": pgn,
            "fen": fen,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response1.status_code == 201
    data1 = response1.json()
    assert data1["is_new"] is True
    positions_first = data1["positions_created"]

    # Second game - same position blunder
    session2 = create_game_session(user_id=123, player_color="white")

    response2 = client.post(
        "/api/blunder",
        json={
            "session_id": session2,
            "pgn": pgn,
            "fen": fen,
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response2.status_code == 201
    data2 = response2.json()
    # Same position, so blunder is not new
    assert data2["is_new"] is False
    # Positions already exist
    assert data2["positions_created"] == 0
    # Same position_id
    assert data2["position_id"] == data1["position_id"]


def test_record_blunder_missing_auth(client, create_game_session):
    """Test 401 when no auth token provided."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
    )

    assert response.status_code == 401


def test_record_blunder_sets_blunder_recorded_flag(client, auth_headers, create_game_session, db_session):
    """Test that blunder_recorded flag is set on session."""
    session_id = create_game_session(user_id=123, player_color="white")

    # Verify flag is false initially
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False

    # Record blunder
    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201

    # Verify flag is now true
    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is True


def test_record_blunder_eval_loss_calculation(client, auth_headers, create_game_session, db_session):
    """Test that eval_loss_cp is calculated correctly."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            "eval_before": 50,
            "eval_after": -100,
        },
        headers=auth_headers(user_id=123)
    )

    assert response.status_code == 201
    blunder_id = response.json()["blunder_id"]

    # Check eval_loss_cp in database
    db_session.expire_all()
    result = db_session.execute(
        text("SELECT eval_loss_cp FROM blunders WHERE id = :id"),
        {"id": blunder_id}
    ).fetchone()
    # eval_loss = eval_before - eval_after = 50 - (-100) = 150
    assert result[0] == 150


def test_eval_loss_cp_stored_raw_uncapped_and_negative(
    client, auth_headers, create_game_session, db_session
):
    """eval_loss_cp is persisted RAW at rest (g-no51): neither capped to the
    decisive-mistake ceiling (1000) on the auto path nor floored to 0 on the
    manual path. Normalization is a read/decision concern (centipawn_loss), so
    we read the DB row directly rather than through any normalizing endpoint.
    """
    # --- Auto path: raw diff of 10000 must NOT be capped to 1000 -----------
    auto_session = create_game_session(user_id=123, player_color="white")
    auto_response = client.post(
        "/api/blunder",
        json={
            "session_id": auto_session,
            "pgn": "1. e4 e5 2. Qh5",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Qh5",
            "best_move": "Nf3",
            # eval_before - eval_after = 50 - (-9950) = 10000 (mate pseudo-cp scale)
            "eval_before": 50,
            "eval_after": -9950,
        },
        headers=auth_headers(user_id=123),
    )
    assert auto_response.status_code == 201
    auto_blunder_id = auto_response.json()["blunder_id"]

    # --- Manual path: eval_before < eval_after must stay a NEGATIVE raw diff -
    # Distinct opening so this lands on a different pre-move position (blunders
    # dedup by (user_id, position_id)) and does not collide with the auto row.
    manual_session = create_game_session(user_id=123, player_color="white")
    manual_response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": manual_session,
            "pgn": "1. d4 d5 2. c4",
            "fen": "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "c4",
            "best_move": None,
            # eval_before - eval_after = -100 - 50 = -150 (negative raw diff)
            "eval_before": -100,
            "eval_after": 50,
        },
        headers=auth_headers(user_id=123),
    )
    assert manual_response.status_code == 201
    manual_blunder_id = manual_response.json()["blunder_id"]

    # Read the at-rest values directly from the DB (bypassing any normalizer).
    db_session.expire_all()
    auto_stored = db_session.execute(
        text("SELECT eval_loss_cp FROM blunders WHERE id = :id"),
        {"id": auto_blunder_id},
    ).fetchone()[0]
    manual_stored = db_session.execute(
        text("SELECT eval_loss_cp FROM blunders WHERE id = :id"),
        {"id": manual_blunder_id},
    ).fetchone()[0]

    # RAW, uncapped: 10000 (not clipped to the 1000 decisive-mistake ceiling).
    assert auto_stored == 10000
    # RAW, not floored to 0: the negative diff is preserved at rest.
    assert manual_stored == -150


def test_record_manual_blunder_success(client, auth_headers, create_game_session):
    """Manual endpoint records a selected move into ghost library."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. d4",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "d4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 201
    data = response.json()
    assert data["is_new"] is True
    assert data["positions_created"] >= 1


def test_record_manual_blunder_duplicate_returns_not_new(client, auth_headers, create_game_session):
    """Manual duplicate add returns is_new=false for same pre-move position."""
    session_id = create_game_session(user_id=123, player_color="white")
    payload = {
        "session_id": session_id,
        "pgn": "1. e4 e5 2. d4",
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "user_move": "d4",
        "best_move": "Nf3",
        "eval_before": 20,
        "eval_after": 15,
    }

    first = client.post("/api/blunder/manual", json=payload, headers=auth_headers(user_id=123))
    assert first.status_code == 201
    assert first.json()["is_new"] is True

    second = client.post("/api/blunder/manual", json=payload, headers=auth_headers(user_id=123))
    assert second.status_code == 201
    assert second.json()["is_new"] is False


@pytest.mark.parametrize("ended", [False, True], ids=["active", "ended-visible"])
def test_manual_new_target_bumps_once_and_duplicate_does_not_bump(
    client, auth_headers, create_game_session, db_session, ended
):
    session_id = create_game_session(user_id=123, player_color="white")
    if ended:
        response = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "draw", "pgn": "1. e4 e5"},
            headers=auth_headers(user_id=123),
        )
        assert response.status_code == 200

    payload = {
        "session_id": session_id,
        "pgn": "1. e4 e5 2. d4",
        "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        "user_move": "d4",
        "best_move": "Nf3",
        "eval_before": 20,
        "eval_after": 15,
    }
    seq_before = current_evidence_seq(db_session, 123, "white")

    first = client.post(
        "/api/blunder/manual", json=payload, headers=auth_headers(user_id=123)
    )
    assert first.status_code == 201
    assert first.json()["is_new"] is True
    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1

    duplicate = client.post(
        "/api/blunder/manual", json=payload, headers=auth_headers(user_id=123)
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["is_new"] is False
    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


def test_record_manual_blunder_allows_ended_session(client, auth_headers, create_game_session):
    """Manual endpoint works for ended sessions as well as active sessions."""
    session_id = create_game_session(user_id=123, player_color="white")

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": "1. e4",
        },
        headers=auth_headers(user_id=123),
    )
    assert end_response.status_code == 200

    manual_response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "e4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert manual_response.status_code == 201


def test_record_manual_blunder_wrong_color(client, auth_headers, create_game_session):
    """Manual capture rejects opponent-side decision points."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5",
            "fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "user_move": "e5",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 400
    assert "black to move" in response.json()["detail"].lower()


def test_record_manual_blunder_does_not_set_session_flag(client, auth_headers, create_game_session, db_session):
    """Manual capture must not toggle first-auto-blunder session flag."""
    session_id = create_game_session(user_id=123, player_color="white")
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False

    response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4",
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "user_move": "e4",
            "best_move": None,
            "eval_before": None,
            "eval_after": None,
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 201

    db_session.expire_all()
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    assert session.blunder_recorded is False


_BLUNDER_PGN = "1. e4 e5 2. Qh5"
_BLUNDER_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _blunder_body(session_id: str, *, idempotency_key: str | None = None) -> dict:
    body = {
        "session_id": session_id,
        "pgn": _BLUNDER_PGN,
        "fen": _BLUNDER_FEN,
        "user_move": "Qh5",
        "best_move": "Nf3",
        "eval_before": 50,
        "eval_after": -100,
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


def test_record_blunder_idempotent_key_match_echoes_id(
    client, auth_headers, create_game_session
):
    """A retry with the same idempotency key echoes the recorded blunder id and
    does not record a second blunder."""
    session_id = create_game_session(user_id=123, player_color="white")

    first = client.post(
        "/api/blunder",
        json=_blunder_body(session_id, idempotency_key="rec-key-1"),
        headers=auth_headers(user_id=123),
    )
    assert first.status_code == 201
    assert first.json()["is_new"] is True
    recorded_id = first.json()["blunder_id"]
    assert recorded_id is not None

    second = client.post(
        "/api/blunder",
        json=_blunder_body(session_id, idempotency_key="rec-key-1"),
        headers=auth_headers(user_id=123),
    )
    assert second.status_code == 201
    assert second.json()["is_new"] is False
    assert second.json()["blunder_id"] == recorded_id


def test_record_blunder_idempotent_key_mismatch_conflicts(
    client, auth_headers, create_game_session
):
    """A request with a different idempotency key against an already-recorded
    session is an IDEMPOTENCY_CONFLICT."""
    session_id = create_game_session(user_id=123, player_color="white")

    first = client.post(
        "/api/blunder",
        json=_blunder_body(session_id, idempotency_key="rec-key-1"),
        headers=auth_headers(user_id=123),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/blunder",
        json=_blunder_body(session_id, idempotency_key="rec-key-2"),
        headers=auth_headers(user_id=123),
    )
    assert second.status_code == 409
    assert second.json()["error"]["details"]["error_code"] == "IDEMPOTENCY_CONFLICT"


@pg_required
def test_record_blunder_concurrent_same_key_records_once(pg_client, pg_session_factory, auth_headers):
    """Under real row locks, two concurrent first requests with the same key
    record exactly one blunder and agree on the id."""
    start = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=auth_headers(user_id=123),
    )
    assert start.status_code == 201
    session_id = start.json()["session_id"]

    def _post():
        return pg_client.post(
            "/api/blunder",
            json=_blunder_body(session_id, idempotency_key="concurrent-rec-key"),
            headers=auth_headers(user_id=123),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = [f.result() for f in [pool.submit(_post), pool.submit(_post)]]

    assert all(r.status_code == 201 for r in responses)
    ids = {r.json()["blunder_id"] for r in responses}
    assert len(ids) == 1
    assert None not in ids

    verify = pg_session_factory()
    try:
        count = verify.execute(
            text("SELECT COUNT(*) FROM blunders WHERE user_id = :u"),
            {"u": 123},
        ).scalar_one()
    finally:
        verify.close()
    assert count == 1


# ===========================================================================
# Evidence-cursor statement ordering (g-n6c2).
#
# ``bump_evidence_seq`` upserts the shared per-(user,color) opening_score_cursors
# row and holds its row lock until COMMIT, so the bump must be the transaction's
# LAST statement — a trailing read holds that lock exactly as long as a trailing
# write would — and must fire exactly ONCE.
#
# None of these setups uploads moves: the autouse _sync_session_evidence shim runs
# the /moves evidence side effects inline, which pre-creates the Position/Move
# rows. _upsert_positions reuses an existing row by (user_id, fen_hash) and only
# INSERTs when absent, so an upload would consume the very graph inserts these
# tests assert on. /api/game/end needs no uploaded moves.
# ===========================================================================
def test_first_blunder_bookkeeping_and_target_precede_cursor_which_is_last(
    client, auth_headers, create_game_session, db_session
):
    """First-blunder recording on an ENDED (evidence-eligible) session: the graph
    upserts, the target insert, and the first-blunder bookkeeping on game_sessions
    all flush before the evidence-cursor bump, which is the transaction's final
    statement — and it commits."""
    headers = auth_headers(user_id=123)  # seeds the users row OUTSIDE the capture
    session_id = create_game_session(user_id=123, player_color="white")
    # An ACTIVE source session is not evidence-eligible, and the auto path bumps only
    # for an eligible source — so end the session first. The end computes the
    # opening-score delta post-commit, which lazily imports request_recompute from its
    # source module, past the autouse fixture's bound-alias patches.
    with patch("app.opening_score_scheduler.request_recompute"):
        end = client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "draw", "pgn": "1. e4 e5"},
            headers=headers,
        )
    assert end.status_code == 200, end.text
    seq_before = current_evidence_seq(db_session, 123, "white")

    with capture_statements() as log:
        response = client.post("/api/blunder", json=_blunder_body(session_id), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["is_new"] is True  # no early-return path was taken

    pre, cursor_idx = cursor_last_before_commit(log)
    positions_idx = next(i for i, s in enumerate(pre) if s.startswith("insert into positions"))
    moves_idx = next(i for i, s in enumerate(pre) if s.startswith("insert into moves"))
    blunder_idx = next(i for i, s in enumerate(pre) if s.startswith("insert into blunders"))
    # blunder_recorded / recorded_blunder_id / blunder_idempotency_key — the write a
    # naive refactor would slip past the bump.
    bookkeeping_idx = next(i for i, s in enumerate(pre) if s.startswith("update game_sessions"))

    assert positions_idx < cursor_idx, pre
    assert moves_idx < cursor_idx, pre
    assert blunder_idx < cursor_idx, pre
    assert bookkeeping_idx < cursor_idx, pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


def test_manual_blunder_target_precedes_cursor_which_is_last(
    client, auth_headers, create_game_session, db_session
):
    """Manual recording bumps unconditionally (a sessionless-style ghost target is
    always digest-visible), so an ACTIVE session is fine. The target insert flushes
    before the evidence-cursor bump, which is the transaction's final statement, and
    the manual path writes NO first-blunder bookkeeping."""
    headers = auth_headers(user_id=123)
    session_id = create_game_session(user_id=123, player_color="white")
    seq_before = current_evidence_seq(db_session, 123, "white")

    # The body must replay >= 2 plies: _record_target returns EARLY with no writes and
    # no bump when the replay yields a single move (the first move of the game can
    # never be steered back to).
    with capture_statements() as log:
        response = client.post(
            "/api/blunder/manual", json=_blunder_body(session_id), headers=headers
        )
    assert response.status_code == 201, response.text
    assert response.json()["is_new"] is True

    pre, cursor_idx = cursor_last_before_commit(log)
    blunder_idx = next(i for i, s in enumerate(pre) if s.startswith("insert into blunders"))
    assert blunder_idx < cursor_idx, pre
    # mark_first_blunder_recorded=False on this path — no session bookkeeping at all.
    assert not [s for s in pre if s.startswith("update game_sessions")], pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before + 1


def test_first_blunder_on_active_session_does_not_bump_cursor(
    client, auth_headers, create_game_session, db_session
):
    """The auto path bumps only for an evidence-ELIGIBLE source session: a live-game
    blunder waits for the session's own eligibility transition, which folds it in with
    that bump (no per-move churn). The target and bookkeeping still commit."""
    headers = auth_headers(user_id=123)
    session_id = create_game_session(user_id=123, player_color="white")  # stays active
    seq_before = current_evidence_seq(db_session, 123, "white")

    with capture_statements() as log:
        response = client.post("/api/blunder", json=_blunder_body(session_id), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["is_new"] is True

    pre = no_cursor_bump(log)
    # Both writes DID run — otherwise "no cursor write" would hold vacuously.
    assert any(s.startswith("insert into blunders") for s in pre), pre
    assert any(s.startswith("update game_sessions") for s in pre), pre

    db_session.expire_all()
    assert current_evidence_seq(db_session, 123, "white") == seq_before
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    assert session.blunder_recorded is True


@pg_required
def test_pg_blunder_advisory_lock_precedes_writes_and_cursor_is_last(
    pg_client, pg_engine, pg_session_factory, auth_headers
):
    """The per-user graph-write advisory lock is taken BEFORE the first shared
    Position/Move write and held (uncommitted) through the graph upserts, with the
    evidence cursor still the transaction's final statement.

    Postgres-only by necessity: ``acquire_graph_write_lock`` returns immediately on
    any other dialect and emits nothing at all, so moving it to AFTER the cursor bump
    would leave every SQLite blunder test green. Its statements are also SELECTs, so
    a last-WRITE check could not see them either — they are the concrete reason the
    ordering window is "last STATEMENT".

    Everything is built through pg_client: create_game_session writes to the SQLite
    db_session, and that row would not exist in the PostgreSQL database this request
    reads.
    """
    user_id = 123
    headers = auth_headers(user_id=user_id)
    start = pg_client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "white"},
        headers=headers,
    )
    assert start.status_code == 201, start.text
    session_id = start.json()["session_id"]

    # The auto path requires an evidence-eligible source; an active session would
    # record the blunder but not bump, and this test would fail on "got 0 cursor
    # writes". The end itself bumps, so the baseline is read AFTER it.
    #
    # is_rated=False because auth_headers seeds the backing users row into the SQLite
    # engine, not this Postgres one: a rated scoring end takes the users FOR NO KEY
    # UPDATE lock and fails closed with 500 when the row is missing. Rating is not
    # what this test is about, and an UNRATED end still flips status->'ended', which
    # is all SESSION_EVIDENCE_ELIGIBLE_SQL asks for.
    with patch("app.opening_score_scheduler.request_recompute"):
        end = pg_client.post(
            "/api/game/end",
            json={"session_id": session_id, "result": "draw", "pgn": "1. e4 e5", "is_rated": False},
            headers=headers,
        )
    assert end.status_code == 200, end.text

    verify = pg_session_factory()
    try:
        seq_before = current_evidence_seq(verify, user_id, "white")
    finally:
        verify.close()

    with capture_statements(target_engine=pg_engine) as log:
        response = pg_client.post("/api/blunder", json=_blunder_body(session_id), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["is_new"] is True

    pre, cursor_idx = cursor_last_before_commit(log)
    lock_timeout_idx = next(i for i, s in enumerate(pre) if "set_config('lock_timeout'" in s)
    stmt_timeout_idx = next(i for i, s in enumerate(pre) if "set_config('statement_timeout'" in s)
    advisory_idx = next(i for i, s in enumerate(pre) if "pg_advisory_xact_lock" in s)
    assert lock_timeout_idx < stmt_timeout_idx < advisory_idx, pre

    first_entity_write = min(
        next(i for i, s in enumerate(pre) if s.startswith("insert into positions")),
        next(i for i, s in enumerate(pre) if s.startswith("insert into moves")),
        next(i for i, s in enumerate(pre) if s.startswith("insert into blunders")),
    )
    assert advisory_idx < first_entity_write, pre
    assert advisory_idx < cursor_idx, pre

    # Durability — which cursor_last_before_commit explicitly does not prove. A FRESH
    # session is what makes this real on Postgres: unlike the SQLite StaticPool, the
    # request and this verify session are different connections, so a rolled-back txn
    # cannot leak through.
    verify = pg_session_factory()
    try:
        assert current_evidence_seq(verify, user_id, "white") == seq_before + 1
        blunder_count = verify.execute(
            text("SELECT COUNT(*) FROM blunders WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
        assert blunder_count == 1
        recorded = verify.execute(
            text("SELECT blunder_recorded FROM game_sessions WHERE id = :s"),
            {"s": uuid.UUID(session_id)},
        ).scalar_one()
        assert recorded is True
    finally:
        verify.close()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
