"""
Tests for POST /api/blunder endpoint.

Run with: pytest test_blunder_api.py -v
"""
import concurrent.futures
import uuid

import pytest
from sqlalchemy import text

from app.fen import fen_hash
from app.models import GameSession
from app.opening_cache import current_evidence_seq
from conftest import pg_required


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


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
