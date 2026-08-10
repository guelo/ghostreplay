"""
Simple test for POST /api/game/start endpoint.

Run with: pytest test_game_api.py -v
"""
import json
import logging
import re
import time
import uuid
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from conftest import TestingSessionLocal

import app.opening_cache as oc
from app.models import (
    Blunder,
    BlunderReview,
    GameSession,
    OpeningScoreBatch,
    RatingHistory,
    UserOpeningScore,
)
from app.opening_baseline_scheduler import OpeningBaselineScheduler
from app.opening_cache import (
    capture_freshness_snapshot,
    opening_score_inputs_fingerprint,
)
from app.opening_graph import get_opening_graph
from app.opening_roots import get_opening_roots


def _seed_fresh_batch(db, *, user_id, player_color, computed_at, scores):
    """Seed a provably-fresh opening-score batch + rows for (user_id, player_color)."""
    snap = capture_freshness_snapshot(db, user_id, player_color)
    batch = OpeningScoreBatch(
        user_id=user_id, player_color=player_color, generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(
            get_opening_graph(), get_opening_roots()
        ),
        inputs_fingerprint=snap.inputs_fingerprint,
        evidence_seq=snap.evidence_seq,
        cache_epoch=snap.cache_epoch,
        scoped_shared_digest=snap.scoped_shared_digest,
        computed_at=computed_at,
    )
    db.add(batch)
    db.flush()
    for key, score in scores.items():
        db.add(UserOpeningScore(
            batch_id=batch.id, user_id=user_id, player_color=player_color,
            opening_key=key, opening_name="x", opening_family="x",
            opening_score=score, confidence=0.5, coverage=0.5, weighted_depth=1.0,
            sample_size=5, computed_at=computed_at,
        ))
    db.commit()
    return batch.id


def _inject_baseline_scheduler(module: str):
    """Return (scheduler, patch-context) binding a non-autostart baseline scheduler
    into ``<module>.enqueue_baseline_snapshot`` so a test can drain it with run_due()."""
    sched = OpeningBaselineScheduler(
        session_factory=TestingSessionLocal, auto_start=False
    )

    def _enqueue(session_id, user_id, player_color):
        sched.enqueue(session_id, user_id, player_color)

    return sched, patch(f"{module}.enqueue_baseline_snapshot", _enqueue)


def test_start_game_success(client, auth_headers):
    """Test successful game creation with standard ELO."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()

    # Verify response structure
    assert "session_id" in data
    assert "engine_elo" in data
    assert "player_color" in data

    # Verify values
    assert data["engine_elo"] == 1500
    assert data["player_color"] == "white"  # Default

    # Verify session_id is a valid UUID
    try:
        uuid.UUID(data["session_id"])
    except ValueError:
        assert False, "session_id is not a valid UUID"


def test_start_game_defaults_player_color_white(client, auth_headers, db_session):
    """Test that player_color defaults to white when omitted."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    session_uuid = uuid.UUID(data["session_id"])

    # Verify response includes player_color
    assert data["player_color"] == "white"

    # Verify database persistence
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).first()
    assert session is not None
    assert session.player_color == "white"


def test_start_game_with_player_color_black(client, auth_headers, db_session):
    """Test that player_color is persisted when provided."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": "black"},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    session_uuid = uuid.UUID(data["session_id"])

    # Verify response includes player_color
    assert data["player_color"] == "black"

    # Verify database persistence
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).first()
    assert session is not None
    assert session.player_color == "black"


def test_start_game_low_elo(client, auth_headers):
    """Test that low ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 400},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 400


def test_start_game_high_elo(client, auth_headers):
    """Test that high ELO values are accepted (no validation)."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 3000},
        headers=auth_headers()
    )

    assert response.status_code == 201
    data = response.json()
    assert data["engine_elo"] == 3000


def test_start_game_missing_auth(client):
    """Test that missing Authorization header is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500}
    )

    assert response.status_code == 401  # Missing auth token


def test_start_game_invalid_user_id(client):
    """Test that invalid bearer token is rejected."""
    response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers={"Authorization": "Bearer not-a-token"}
    )

    assert response.status_code == 401  # Invalid token


def test_start_game_missing_elo(client, auth_headers):
    """Test that missing engine_elo is rejected."""
    response = client.post(
        "/api/game/start",
        json={},
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


def test_end_game_success(client, auth_headers):
    """Test successfully ending a game with checkmate_win."""
    # Start a game first
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End the game
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#"
        },
        headers=auth_headers()
    )

    assert end_response.status_code == 200
    data = end_response.json()

    assert data["session_id"] == session_id
    assert data["result"] == "checkmate_win"
    assert "ended_at" in data
    assert data["rating"]["rating_after"] == data["scores"]["elo"]["rating"]
    assert data["scores"]["chesscom"]["rating"] > 1200
    assert data["scores"]["lichess"]["rating"] > 1500
    assert data["score_changes"]["chesscom"]["rating"] > 0


def test_end_game_keeps_glicko_null_for_unbackfilled_legacy_user(client, auth_headers, db_session):
    user_id = 123
    legacy_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers(user_id=user_id),
    )
    legacy_session_id = uuid.UUID(legacy_response.json()["session_id"])
    legacy_row = RatingHistory(
        user_id=user_id,
        game_session_id=legacy_session_id,
        rating=1400,
        is_provisional=False,
        games_played=30,
        recorded_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(legacy_row)
    db_session.commit()

    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers(user_id=user_id),
    )
    session_id = start_response.json()["session_id"]

    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5",
        },
        headers=auth_headers(user_id=user_id),
    )

    assert end_response.status_code == 200
    data = end_response.json()
    assert data["scores"]["elo"]["rating"] == data["rating"]["rating_after"]
    assert data["scores"]["chesscom"] is None
    assert data["scores"]["lichess"] is None
    assert data["score_changes"]["chesscom"] is None
    assert data["score_changes"]["lichess"] is None


def test_end_game_with_pgn(client, auth_headers):
    """Test ending a game with PGN."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End with PGN
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "draw",
            "pgn": pgn
        },
        headers=auth_headers()
    )

    assert end_response.status_code == 200
    assert end_response.json()["result"] == "draw"


def test_end_game_all_result_types(client, auth_headers):
    """Every supported normal terminal result submits delta work after commit."""
    cases = [
        ("checkmate_win", True),
        ("checkmate_loss", True),
        ("resign", True),
        ("draw", True),
        ("draw", False),
        ("abandon", True),
    ]
    observed = []

    def observe_delta(db, session):
        # A separate session can see the authoritative terminal transition: the
        # delta helper is called only after the endpoint commit.
        with TestingSessionLocal() as observer:
            durable = observer.get(GameSession, session.id)
            observed.append(
                (durable.result, durable.status, durable.is_rated)
            )
        return []

    with patch("app.api.game.compute_opening_score_delta", side_effect=observe_delta):
        for result, is_rated in cases:
            start_response = client.post(
                "/api/game/start",
                json={"engine_elo": 1500},
                headers=auth_headers()
            )
            session_id = start_response.json()["session_id"]

            end_response = client.post(
                "/api/game/end",
                json={
                    "session_id": session_id,
                    "result": result,
                    "pgn": "1. e4 e5",
                    "is_rated": is_rated,
                },
                headers=auth_headers()
            )

            assert end_response.status_code == 200
            assert end_response.json()["result"] == result

    assert observed == [
        ("checkmate_win", "ended", True),
        ("checkmate_loss", "ended", True),
        ("resign", "ended", True),
        ("draw", "ended", True),
        ("draw", "ended", False),
    ]


def test_end_game_not_found(client, auth_headers):
    """Test ending a non-existent game."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    response = client.post(
        "/api/game/end",
        json={
            "session_id": fake_uuid,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_end_game_wrong_user(client, auth_headers):
    """Test that users cannot end other users' games."""
    # User 123 starts a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers(user_id=123, username="ghost_123")
    )
    session_id = start_response.json()["session_id"]

    # User 456 tries to end it
    end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers(user_id=456, username="ghost_456")
    )

    assert end_response.status_code == 403
    assert "not authorized" in end_response.json()["detail"].lower()


def test_end_game_already_ended(client, auth_headers):
    """Test that ending an already-ended game fails."""
    # Start a game
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    # End it once
    client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "checkmate_win",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    # Try to end it again
    second_end_response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert second_end_response.status_code == 400
    assert "already ended" in second_end_response.json()["detail"].lower()


def test_end_game_invalid_result(client, auth_headers):
    """Test that invalid result values are rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "invalid_result",
            "pgn": "1. e4 e5"
        },
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


def test_end_game_missing_auth(client, auth_headers):
    """Test that missing Authorization header is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign",
            "pgn": "1. e4 e5"
        }
    )

    assert response.status_code == 401  # Missing auth token


def test_end_game_missing_pgn(client, auth_headers):
    """Test that missing PGN is rejected."""
    start_response = client.post(
        "/api/game/start",
        json={"engine_elo": 1500},
        headers=auth_headers()
    )
    session_id = start_response.json()["session_id"]

    response = client.post(
        "/api/game/end",
        json={
            "session_id": session_id,
            "result": "resign"
        },
        headers=auth_headers()
    )

    assert response.status_code == 422  # Validation error


# === Opponent Move Endpoint Steering Tests ===


def _post_next_opponent_move(client, auth_headers, session_id, fen, user_id: int = 123):
    return client.post(
        "/api/game/next-opponent-move",
        json={"session_id": str(session_id), "fen": fen},
        headers=auth_headers(user_id=user_id),
    )


def test_next_opponent_move_returns_opponent_move_to_blunder(client, auth_headers, create_game_session, db_session):
    """Test next-opponent-move returns opponent's move leading to a blunder position."""
    user_id = 123

    # First, record a blunder via /api/blunder
    # PGN: 1. e4 e5 2. Qh5 (white blunders with Qh5)
    # Blunder is at position after 1.e4 e5 (white to move)
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    blunder_response = client.post(
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
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Backdate blunder so it's due for SRS review (priority >= 1.0)
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    # Now start a NEW game and query next-opponent-move
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    # After 1.e4 (black to move), ghost should suggest e5 to reach blunder position
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_after_e4, user_id=user_id
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["move"]["san"] == "e5"
    assert data["move"]["uci"] == "e7e5"
    assert data["target_blunder_id"] is not None
    assert data["decision_source"] == "ghost_path"


def test_next_opponent_move_returns_move_to_manual_library_target(client, auth_headers, create_game_session, db_session):
    """Manual /api/blunder/manual targets should be reachable by next-opponent-move traversal."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Manual add for position after 1.e4 e5 (white to move), selected move is 2.Nf3
    manual_response = client.post(
        "/api/blunder/manual",
        json={
            "session_id": session_id,
            "pgn": "1. e4 e5 2. Nf3",
            "fen": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
            "user_move": "Nf3",
            "best_move": "Nf3",
            "eval_before": 30,
            "eval_after": 30,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert manual_response.status_code == 201
    assert manual_response.json()["is_new"] is True

    # Backdate blunder so it's due for SRS review (priority >= 1.0)
    blunder_id = manual_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    new_session_id = create_game_session(user_id=user_id, player_color="white")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_after_e4, user_id=user_id
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["move"]["san"] == "e5"
    assert data["move"]["uci"] == "e7e5"
    assert data["target_blunder_id"] is not None
    assert data["decision_source"] == "ghost_path"


def test_next_opponent_move_no_blunder_in_path(client, auth_headers, create_game_session):
    """Test next-opponent-move falls back to engine when no blunder exists in graph."""
    from unittest.mock import patch
    from app.opponent_move_controller import ControllerMove

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    fake_move = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    with patch("app.opponent_move_controller.choose_move", return_value=fake_move):
        response = _post_next_opponent_move(
            client, auth_headers, session_id, fen_after_e4, user_id=user_id
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "engine"
    assert data["move"]["san"]
    assert data["move"]["uci"]
    assert data["target_blunder_id"] is None
    assert data["decision_source"] == "backend_engine"


def test_next_opponent_move_users_turn_returns_error(client, auth_headers, create_game_session):
    """Test next-opponent-move returns 400 when it's the user's turn."""
    user_id = 123

    # Record a blunder
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    client.post(
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
        headers=auth_headers(user_id=user_id),
    )

    # Start new game and query at the blunder position (white to move)
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_before_blunder, user_id=user_id
    )

    assert response.status_code == 400
    assert "player's turn" in response.json()["detail"].lower()


def test_next_opponent_move_black_player(client, auth_headers, create_game_session, db_session):
    """Test next-opponent-move works for black player."""
    user_id = 123

    # Record a blunder as black
    # PGN: 1. e4 e5 2. Nf3 Qh4 (black blunders with Qh4)
    session_id = create_game_session(user_id=user_id, player_color="black")
    pgn = "1. e4 e5 2. Nf3 Qh4"
    # FEN after 1.e4 e5 2.Nf3 (black to move)
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"

    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh4",
            "best_move": "Nc6",
            "eval_before": -50,
            "eval_after": 100,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Backdate blunder so it's due for SRS review (priority >= 1.0)
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    # Start new game as black
    new_session_id = create_game_session(user_id=user_id, player_color="black")

    # After 1.e4 e5 (white to move), ghost should suggest Nf3 to reach blunder position
    fen_after_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_after_e5, user_id=user_id
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["move"]["san"] == "Nf3"
    assert data["move"]["uci"] == "g1f3"
    assert data["target_blunder_id"] is not None
    assert data["decision_source"] == "ghost_path"


def test_next_opponent_move_target_srs_pass_fail_counts(client, auth_headers, create_game_session, db_session):
    """Regression: target_blunder_srs pass/fail counts come from the shared loader."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="black")
    pgn = "1. e4 e5 2. Nf3 Qh4"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Qh4",
            "best_move": "Nc6",
            "eval_before": -50,
            "eval_after": 100,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    now = datetime.now(timezone.utc)
    db_session.add_all([
        BlunderReview(blunder_id=blunder_id, session_id=uuid.UUID(str(session_id)), reviewed_at=now - timedelta(hours=2), passed=True, move_played_san="Nc6", eval_delta_cp=0),
        BlunderReview(blunder_id=blunder_id, session_id=uuid.UUID(str(session_id)), reviewed_at=now - timedelta(hours=1), passed=False, move_played_san="Qh4", eval_delta_cp=150),
    ])
    db_session.commit()

    new_session_id = create_game_session(user_id=user_id, player_color="black")
    fen_after_e5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_after_e5, user_id=user_id
    )
    assert response.status_code == 200
    data = response.json()
    assert data["target_blunder_id"] == blunder_id
    srs = data["target_blunder_srs"]
    assert srs["pass_count"] == 1
    assert srs["fail_count"] == 1


def test_next_opponent_move_session_not_found(client, auth_headers):
    """Test next-opponent-move returns 404 for non-existent session."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = _post_next_opponent_move(
        client, auth_headers, fake_uuid, starting_fen
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_next_opponent_move_wrong_user(client, auth_headers, create_game_session):
    """Test next-opponent-move returns 403 when user doesn't own the session."""
    # User 123 starts a game
    session_id = create_game_session(user_id=123, player_color="white")
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    # User 456 tries to query next-opponent-move
    response = _post_next_opponent_move(
        client, auth_headers, session_id, starting_fen, user_id=456
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"].lower()


def test_next_opponent_move_missing_auth(client):
    """Test next-opponent-move returns 401 when auth is missing."""
    starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    response = client.post(
        "/api/game/next-opponent-move",
        json={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "fen": starting_fen,
        },
    )

    assert response.status_code == 401


def test_next_opponent_move_finds_blunder_multiple_moves_downstream(client, auth_headers, create_game_session, db_session):
    """Test next-opponent-move finds a blunder 3 moves downstream via recursive CTE."""
    user_id = 123

    # Record a blunder at move 4 (white's second move)
    # PGN: 1. e4 e5 2. Nf3 Nc6 3. Bc4 Nd4 (white blunders with Bc4, a dubious move)
    # Blunder is at position after 1.e4 e5 2.Nf3 Nc6 (white to move)
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Nf3 Nc6 3. Bc4"
    # FEN after 1.e4 e5 2.Nf3 Nc6 (white to move) - this is where blunder happens
    fen_before_blunder = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"

    blunder_response = client.post(
        "/api/blunder",
        json={
            "session_id": session_id,
            "pgn": pgn,
            "fen": fen_before_blunder,
            "user_move": "Bc4",
            "best_move": "Bb5",
            "eval_before": 30,
            "eval_after": -20,
        },
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Backdate blunder so it's due for SRS review (priority >= 1.0)
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    # Now start a NEW game and query next-opponent-move from earlier in the game
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    # After 1.e4 (black to move) - blunder is 3 half-moves away
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = _post_next_opponent_move(
        client, auth_headers, new_session_id, fen_after_e4, user_id=user_id
    )

    assert response.status_code == 200
    data = response.json()
    # Ghost should suggest "e5" - the first move in the path to the blunder position
    assert data["mode"] == "ghost"
    assert data["move"]["san"] == "e5"
    assert data["move"]["uci"] == "e7e5"
    assert data["target_blunder_id"] is not None
    assert data["decision_source"] == "ghost_path"


def _create_position_chain(db_session, user_id: int, length: int):
    """Helper to create a chain of positions for depth tests."""
    from app.models import Move, Position
    from app.fen import fen_hash

    positions = []
    for i in range(length):
        color_char = "w" if i % 2 == 0 else "b"
        # Place king at different squares to get unique positions
        file_idx = i % 8
        rank_idx = i // 8
        # Build FEN with king at unique square
        ranks = ["8"] * 8
        if file_idx == 0:
            ranks[rank_idx] = f"K{7 - file_idx}" if 7 - file_idx > 0 else "K"
        else:
            ranks[rank_idx] = f"{file_idx}K" + (f"{7 - file_idx}" if 7 - file_idx > 0 else "")
        fen = "/".join(reversed(ranks)) + f" {color_char} - - 0 {i}"

        pos = Position(
            user_id=user_id,
            fen_hash=fen_hash(fen),
            fen_raw=fen,
            active_color="white" if i % 2 == 0 else "black",
        )
        db_session.add(pos)
        db_session.flush()
        positions.append(pos)

    # Create moves connecting them: 0->1->2->...->n
    for i in range(length - 1):
        move = Move(
            from_position_id=positions[i].id,
            move_san=f"m{i}",
            to_position_id=positions[i + 1].id,
        )
        db_session.add(move)

    return positions


def test_find_ghost_move_finds_blunder_at_max_depth(db_session):
    """find_ghost_move finds a blunder exactly at depth 5 (the steering radius)."""
    from app.api.game import find_ghost_move
    from app.models import Blunder

    user_id = 123

    # Create chain of 7 positions (0 through 6)
    # Query from position 1, blunder at position 6 = depth 5
    positions = _create_position_chain(db_session, user_id, 7)

    # Create a blunder at position 6 (depth 5 from position 1)
    # Position 6 has active_color="white" (6 % 2 == 0)
    # Backdate so it's due for SRS review (priority >= 1.0)
    blunder = Blunder(
        user_id=user_id,
        position_id=positions[6].id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(blunder)
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=positions[1].fen_raw,
        player_color="white",
    )

    # Should find the blunder at exactly depth 5
    assert move_san == "m1"
    assert target_blunder_id is not None


def test_find_ghost_move_respects_depth_limit(db_session):
    """find_ghost_move does not find blunders beyond depth 5."""
    from app.api.game import find_ghost_move
    from app.models import Blunder

    user_id = 123

    # Create chain of 9 positions (0 through 8)
    # Query from position 1, blunder at position 8 = depth 7 (beyond limit)
    positions = _create_position_chain(db_session, user_id, 9)

    # Create a blunder at position 8 (depth 7 from position 1, beyond limit of 5)
    # Position 8 has active_color="white" (8 % 2 == 0)
    blunder = Blunder(
        user_id=user_id,
        position_id=positions[8].id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=positions[1].fen_raw,
        player_color="white",
    )

    # Should NOT find the blunder at depth 7 (positions 1->2->...->8)
    assert move_san is None
    assert target_blunder_id is None


def test_find_ghost_move_prefers_higher_severity_when_priority_equal(db_session):
    """With equal priority/distance, higher eval_loss_cp should win."""
    from datetime import datetime, timedelta, timezone

    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)

    # Opponent-to-move start position for white player.
    fen_start = "8/8/8/8/8/8/8/K6k b - - 0 1"
    fen_low = "8/8/8/8/8/8/8/1K5k w - - 0 2"
    fen_high = "8/8/8/8/8/8/8/2K4k w - - 0 2"

    pos_start = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_start),
        fen_raw=fen_start,
        active_color="black",
    )
    pos_low = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_low),
        fen_raw=fen_low,
        active_color="white",
    )
    pos_high = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_high),
        fen_raw=fen_high,
        active_color="white",
    )
    db_session.add_all([pos_start, pos_low, pos_high])
    db_session.flush()

    db_session.add_all([
        Move(from_position_id=pos_start.id, move_san="mLow", to_position_id=pos_low.id),
        Move(from_position_id=pos_start.id, move_san="mHigh", to_position_id=pos_high.id),
    ])
    db_session.add_all([
        Blunder(
            user_id=user_id,
            position_id=pos_low.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=50,
            last_reviewed_at=now - timedelta(hours=5),
        ),
        Blunder(
            user_id=user_id,
            position_id=pos_high.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
            last_reviewed_at=now - timedelta(hours=5),
        ),
    ])
    db_session.commit()

    # Use _rng_seed=1 for deterministic top-k sampling (seed 1 picks higher-weight candidate)
    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_start,
        player_color="white",
        _rng_seed=1,
    )

    assert move_san == "mHigh"
    assert target_blunder_id is not None


def test_find_ghost_move_mate_magnitude_does_not_outrank_decisive(db_session):
    """g-no51: eval_loss_cp is stored RAW (a mate pseudo-cp is ~10000). Ghost
    selection must normalize severity through the shared decisive-mistake ceiling,
    so a mate blunder cannot out-rank a normal >=1000cp decisive blunder. With
    severity equal, the pick is decided by the deterministic later sort keys (here
    the HIGHER blunder_id), NOT by mate magnitude.
    """
    from app.api.game import (
        GhostMoveCandidate,
        _candidate_sort_key,
        find_ghost_move,
    )
    from app.centipawn_loss import centipawn_loss
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)
    # pass_streak=0, interval 4h, reviewed 5h ago -> priority 1.25 (due) for both.
    reviewed_at = now - timedelta(hours=5)

    fen_start = "8/8/8/8/K6k/8/8/8 b - - 0 1"       # opponent (black) to move
    fen_mate = "8/8/8/8/1K5k/8/8/8 w - - 0 2"       # reached by mMate
    fen_decisive = "8/8/8/8/2K4k/8/8/8 w - - 0 2"   # reached by mDecisive

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_mate = Position(user_id=user_id, fen_hash=fen_hash(fen_mate), fen_raw=fen_mate, active_color="white")
    pos_decisive = Position(user_id=user_id, fen_hash=fen_hash(fen_decisive), fen_raw=fen_decisive, active_color="white")
    db_session.add_all([pos_start, pos_mate, pos_decisive])
    db_session.flush()

    db_session.add_all([
        Move(from_position_id=pos_start.id, move_san="mMate", to_position_id=pos_mate.id),
        Move(from_position_id=pos_start.id, move_san="mDecisive", to_position_id=pos_decisive.id),
    ])

    # Mate blunder added FIRST -> lower blunder_id; the raw-1000 decisive blunder
    # added SECOND -> HIGHER blunder_id, so it must win the deterministic
    # fall-through once severity is equal.
    mate_blunder = Blunder(
        user_id=user_id, position_id=pos_mate.id, bad_move_san="bad",
        best_move_san="good", eval_loss_cp=10000, pass_streak=0,
        last_reviewed_at=reviewed_at,
    )
    db_session.add(mate_blunder)
    db_session.flush()
    decisive_blunder = Blunder(
        user_id=user_id, position_id=pos_decisive.id, bad_move_san="bad",
        best_move_san="good", eval_loss_cp=1000, pass_streak=0,
        last_reviewed_at=reviewed_at,
    )
    db_session.add(decisive_blunder)
    db_session.commit()

    # eval_loss_cp is stored RAW (the ceiling is a decision-time normalizer, not a
    # write-time clamp), and the raw-1000 blunder carries the higher id.
    assert mate_blunder.eval_loss_cp == 10000
    assert decisive_blunder.eval_loss_cp == 1000
    assert decisive_blunder.id > mate_blunder.id

    # Deterministic decision path: build candidates exactly as find_ghost_move
    # scores them from the stored RAW eval_loss_cp. Mate magnitude neither raises
    # the score nor the eval sort element above the decisive blunder, so the
    # higher-id decisive blunder sorts first.
    def _cand(blunder, first_move):
        return GhostMoveCandidate(
            first_move=first_move, blunder_id=blunder.id, depth=1,
            eval_loss_cp=blunder.eval_loss_cp, pass_streak=0,
            last_reviewed_at=reviewed_at, created_at=None,
        )

    c_mate = _cand(mate_blunder, "mMate")
    c_decisive = _cand(decisive_blunder, "mDecisive")
    assert c_mate.score(now) == c_decisive.score(now)

    key_mate = _candidate_sort_key((c_mate, c_mate.score(now)))
    key_decisive = _candidate_sort_key((c_decisive, c_decisive.score(now)))
    assert key_mate[2] == key_decisive[2] == -centipawn_loss(1000)  # both -> -1000
    ordered = sorted(
        [(c_mate, c_mate.score(now)), (c_decisive, c_decisive.score(now))],
        key=_candidate_sort_key,
    )
    assert ordered[0][0].blunder_id == decisive_blunder.id

    # End-to-end smoke through the real selector: with equal severity the mate
    # blunder does NOT monopolize selection — the decisive (raw-1000) blunder is
    # reachable and gets picked across seeds.
    picks = set()
    for seed in range(20):
        move_san, target_blunder_id, _, _, _ = find_ghost_move(
            db=db_session, user_id=user_id, fen=fen_start,
            player_color="white", _rng_seed=seed,
        )
        assert move_san in {"mMate", "mDecisive"}
        assert target_blunder_id in {mate_blunder.id, decisive_blunder.id}
        picks.add(move_san)

    assert "mDecisive" in picks, "mate magnitude wrongly monopolized Ghost selection"


def test_find_ghost_move_prefers_more_overdue_when_severity_equal(db_session):
    """With equal severity/distance, higher SRS priority should win."""
    from datetime import datetime, timedelta, timezone

    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)

    fen_start = "8/8/8/8/8/8/8/K5k1 b - - 0 1"
    fen_recent = "8/8/8/8/8/8/8/1K4k1 w - - 0 2"
    fen_overdue = "8/8/8/8/8/8/8/2K3k1 w - - 0 2"

    pos_start = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_start),
        fen_raw=fen_start,
        active_color="black",
    )
    pos_recent = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_recent),
        fen_raw=fen_recent,
        active_color="white",
    )
    pos_overdue = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_overdue),
        fen_raw=fen_overdue,
        active_color="white",
    )
    db_session.add_all([pos_start, pos_recent, pos_overdue])
    db_session.flush()

    db_session.add_all([
        Move(from_position_id=pos_start.id, move_san="mRecent", to_position_id=pos_recent.id),
        Move(from_position_id=pos_start.id, move_san="mOverdue", to_position_id=pos_overdue.id),
    ])
    db_session.add_all([
        Blunder(
            user_id=user_id,
            position_id=pos_recent.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            pass_streak=0,
            last_reviewed_at=now - timedelta(hours=5),
        ),
        Blunder(
            user_id=user_id,
            position_id=pos_overdue.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            pass_streak=0,
            last_reviewed_at=now - timedelta(hours=12),
        ),
    ])
    db_session.commit()

    # Use _rng_seed=1 for deterministic top-k sampling (seed 1 picks higher-weight candidate)
    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_start,
        player_color="white",
        _rng_seed=1,
    )

    assert move_san == "mOverdue"
    assert target_blunder_id is not None


def test_find_ghost_move_handles_cycles(db_session):
    """find_ghost_move handles cycles in the position graph without infinite loops."""
    from app.api.game import find_ghost_move
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    # Create positions that form a cycle: A -> B -> C -> A
    # Plus a path from B to a blunder position D
    # Use valid FEN format with different piece placements to get unique hashes
    fen_a = "8/8/8/8/8/8/8/K7 b - - 0 1"  # black to move
    fen_b = "8/8/8/8/8/8/8/1K6 w - - 0 2"  # white to move
    fen_c = "8/8/8/8/8/8/8/2K5 b - - 0 3"  # black to move
    fen_d = "8/8/8/8/8/8/8/3K4 w - - 0 4"  # white to move - blunder position

    pos_a = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_a),
        fen_raw=fen_a,
        active_color="black",  # Opponent's turn
    )
    pos_b = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_b),
        fen_raw=fen_b,
        active_color="white",  # User's turn
    )
    pos_c = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_c),
        fen_raw=fen_c,
        active_color="black",  # Opponent's turn
    )
    pos_d = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen_d),
        fen_raw=fen_d,
        active_color="white",  # User's turn - blunder here
    )

    db_session.add_all([pos_a, pos_b, pos_c, pos_d])
    db_session.flush()

    # Create cycle: A -> B -> C -> A
    move_ab = Move(from_position_id=pos_a.id, move_san="a2b", to_position_id=pos_b.id)
    move_bc = Move(from_position_id=pos_b.id, move_san="b2c", to_position_id=pos_c.id)
    move_ca = Move(from_position_id=pos_c.id, move_san="c2a", to_position_id=pos_a.id)
    # Also B -> D (path to blunder)
    move_bd = Move(from_position_id=pos_b.id, move_san="b2d", to_position_id=pos_d.id)

    db_session.add_all([move_ab, move_bc, move_ca, move_bd])

    # Create blunder at position D, backdated so it's due for SRS review
    blunder = Blunder(
        user_id=user_id,
        position_id=pos_d.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        created_at=datetime.now(timezone.utc) - timedelta(hours=5),
    )
    db_session.add(blunder)
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_a,
        player_color="white",
    )

    # Should find the blunder despite the cycle, and not hang
    assert move_san == "a2b"
    assert target_blunder_id is not None


def test_find_ghost_move_skips_not_yet_due_blunder(db_session):
    """find_ghost_move ignores blunders whose SRS priority is below 1.0."""
    from app.api.game import find_ghost_move
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    fen_start = "8/8/8/8/8/8/K7/7k b - - 0 1"
    fen_blunder = "8/8/8/8/8/8/1K6/7k w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))

    # Blunder reviewed 1 hour ago with pass_streak=0 → interval=4h → priority=0.25 (not due)
    now = datetime.now(timezone.utc)
    db_session.add(Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        last_reviewed_at=now - timedelta(hours=1),
    ))
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
    )

    assert move_san is None
    assert target_blunder_id is None


def test_find_ghost_move_selects_just_due_blunder(db_session):
    """find_ghost_move selects a blunder whose SRS priority just crossed 1.0."""
    from app.api.game import find_ghost_move
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    fen_start = "8/8/8/8/8/8/K7/6k1 b - - 0 1"
    fen_blunder = "8/8/8/8/8/8/1K6/6k1 w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))

    # Blunder reviewed 5 hours ago with pass_streak=0 → interval=4h → priority=1.25 (due)
    now = datetime.now(timezone.utc)
    db_session.add(Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        last_reviewed_at=now - timedelta(hours=5),
    ))
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
    )

    assert move_san == "m1"
    assert target_blunder_id is not None


def test_find_ghost_move_slow_log_includes_opening_family_timer(db_session, monkeypatch, caplog):
    """Slow ghost-search logs report nonzero time spent resolving the opening family."""
    from app.api import game as game_api
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    fen_start = "8/8/8/8/8/8/K7/5k2 b - - 0 1"
    fen_blunder = "8/8/8/8/8/8/1K6/5k2 w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))
    db_session.add(Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        opening_family="Test Family",
        last_reviewed_at=datetime.now(timezone.utc) - timedelta(hours=5),
    ))
    db_session.commit()

    def fake_detect_opening_family(_fen):
        time.sleep(0.001)
        return "Test Family"

    monkeypatch.setattr(game_api, "SLOW_GHOST_SEARCH_LOG_MS", 0)
    monkeypatch.setattr(game_api, "detect_opening_family", fake_detect_opening_family)

    with caplog.at_level(logging.INFO, logger="app.api.game"):
        move_san, target_blunder_id, _, _, _ = game_api.find_ghost_move(
            db=db_session,
            user_id=user_id,
            fen=fen_start,
            player_color="white",
        )

    assert move_san == "m1"
    assert target_blunder_id is not None
    slow_messages = [
        record.getMessage()
        for record in caplog.records
        if "ghost_move_search slow outcome=selected" in record.getMessage()
    ]
    assert len(slow_messages) == 1
    match = re.search(r"opening_family_ms=(\d+\.\d+)", slow_messages[0])
    assert match is not None
    assert float(match.group(1)) >= 1.0


def test_find_ghost_move_ignores_current_session_opportunity(db_session):
    """The in-progress game must not count as a missed opportunity against the
    very blunder it is steering toward.

    Regression for g-tgub: once the active session touches an ancestor of a due
    blunder, _compute_blunder_opportunity_events records a BlunderOpportunityEvent
    for that session. That single event flips srs_priority from the time-based
    value to opportunities_since_review/expected = 1/1.0 = exactly 1.0, which
    fails the (intentional) ``> 1.0`` due gate and silently kills steering for
    the rest of the game. find_ghost_move must exclude the current session.
    """
    from app.api.game import find_ghost_move
    from app.models import Blunder, BlunderOpportunityEvent, Move, Position
    from app.fen import fen_hash

    user_id = 123

    fen_start = "8/8/8/8/8/8/K7/6k1 b - - 0 1"
    fen_blunder = "8/8/8/8/8/8/1K6/6k1 w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))

    now = datetime.now(timezone.utc)
    blunder = Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        last_reviewed_at=now - timedelta(hours=5),  # time-based priority 1.25 (due)
    )
    db_session.add(blunder)
    db_session.flush()

    # The in-progress game generates its own opportunity event for this blunder.
    current_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=now,
        status="active",
        engine_elo=1500,
        is_rated=True,
        player_color="white",
    )
    db_session.add(current_session)
    db_session.flush()
    db_session.add(BlunderOpportunityEvent(
        blunder_id=blunder.id,
        session_id=current_session.id,
        occurred_at=now,
        opportunity=True,
        reached=False,
    ))
    db_session.commit()

    # Without the current session excluded, its own event suppresses steering
    # (priority collapses to exactly 1.0) — this is the failure mode.
    suppressed_san, _, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
    )
    assert suppressed_san is None

    # Passing the in-progress session excludes its own event, so the blunder
    # stays time-based due (1.25) and steering survives.
    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=fen_start,
        player_color="white",
        session_id=current_session.id,
    )
    assert move_san == "m1"
    assert target_blunder_id == blunder.id


def test_find_ghost_move_skips_mastered_blunder_high_pass_streak(db_session):
    """find_ghost_move skips a blunder with high pass_streak whose interval hasn't elapsed."""
    from app.api.game import find_ghost_move
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    fen_start = "8/8/8/8/8/K7/8/7k b - - 0 1"
    fen_blunder = "8/8/8/8/8/1K6/8/7k w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))

    # pass_streak=5 → interval=4*2^5=128h. Reviewed 40h ago → priority=40/128≈0.31 (not due)
    now = datetime.now(timezone.utc)
    db_session.add(Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        pass_streak=5,
        last_reviewed_at=now - timedelta(hours=40),
    ))
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
    )

    # pass_streak=5 with only 40h elapsed should NOT be targeted
    assert move_san is None
    assert target_blunder_id is None


def test_find_ghost_move_not_due_excluded_despite_urgency(db_session):
    """A not-yet-due candidate is excluded by the linear priority gate even though
    the bounded urgency formula would give it a non-trivial urgency value."""
    from app.api.game import find_ghost_move
    from app.models import Blunder, Move, Position
    from app.fen import fen_hash

    user_id = 123

    fen_start = "8/8/8/8/8/8/K7/6k1 b - - 0 1"
    fen_blunder = "8/8/8/8/8/8/1K6/6k1 w - - 0 2"

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    pos_blunder = Position(user_id=user_id, fen_hash=fen_hash(fen_blunder), fen_raw=fen_blunder, active_color="white")
    db_session.add_all([pos_start, pos_blunder])
    db_session.flush()

    db_session.add(Move(from_position_id=pos_start.id, move_san="m1", to_position_id=pos_blunder.id))

    # Reviewed 2h ago with pass_streak=0 → interval=4h → linear priority=0.5 (not due)
    # But bounded urgency = 1 + log2(1+0.5) = 1.585 — non-trivial
    now = datetime.now(timezone.utc)
    db_session.add(Blunder(
        user_id=user_id,
        position_id=pos_blunder.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
        last_reviewed_at=now - timedelta(hours=2),
    ))
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
    )

    # Must be excluded by the linear priority <= 1.0 gate
    assert move_san is None
    assert target_blunder_id is None


def test_find_ghost_move_topk_samples_from_candidates(db_session):
    """With multiple due candidates, top-k sampling returns one from the valid set."""
    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)

    fen_start = "8/8/8/8/8/K7/8/7k b - - 0 1"
    fens = [
        "8/8/8/8/8/1K6/8/7k w - - 0 2",
        "8/8/8/8/8/2K5/8/7k w - - 0 2",
        "8/8/8/8/8/3K4/8/7k w - - 0 2",
    ]
    moves = ["mA", "mB", "mC"]

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    db_session.add(pos_start)
    db_session.flush()

    for fen, move_san in zip(fens, moves):
        pos = Position(user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="white")
        db_session.add(pos)
        db_session.flush()
        db_session.add(Move(from_position_id=pos_start.id, move_san=move_san, to_position_id=pos.id))
        db_session.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=150,
            last_reviewed_at=now - timedelta(hours=10),
        ))
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
        _rng_seed=42,
    )

    assert move_san in moves
    assert target_blunder_id is not None


def test_find_ghost_move_deterministic_with_same_seed(db_session):
    """Same _rng_seed produces the same result."""
    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)

    fen_start = "8/8/8/8/K7/8/8/7k b - - 0 1"
    fens = [
        "8/8/8/8/1K6/8/8/7k w - - 0 2",
        "8/8/8/8/2K5/8/8/7k w - - 0 2",
        "8/8/8/8/3K4/8/8/7k w - - 0 2",
    ]
    moves = ["mX", "mY", "mZ"]

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    db_session.add(pos_start)
    db_session.flush()

    for fen, move_san in zip(fens, moves):
        pos = Position(user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="white")
        db_session.add(pos)
        db_session.flush()
        db_session.add(Move(from_position_id=pos_start.id, move_san=move_san, to_position_id=pos.id))
        db_session.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            last_reviewed_at=now - timedelta(hours=8),
        ))
    db_session.commit()

    result1 = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
        _rng_seed=99,
    )
    result2 = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
        _rng_seed=99,
    )

    assert result1 == result2


def test_find_ghost_move_different_seed_can_differ(db_session):
    """Different seeds can produce different results with equal-score candidates."""
    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)

    fen_start = "8/8/8/K7/8/8/8/7k b - - 0 1"
    fens = [
        "8/8/8/1K6/8/8/8/7k w - - 0 2",
        "8/8/8/2K5/8/8/8/7k w - - 0 2",
        "8/8/8/3K4/8/8/8/7k w - - 0 2",
        "8/8/8/4K3/8/8/8/7k w - - 0 2",
        "8/8/8/5K2/8/8/8/7k w - - 0 2",
    ]
    moves = [f"m{i}" for i in range(5)]

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    db_session.add(pos_start)
    db_session.flush()

    for fen, move_san in zip(fens, moves):
        pos = Position(user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="white")
        db_session.add(pos)
        db_session.flush()
        db_session.add(Move(from_position_id=pos_start.id, move_san=move_san, to_position_id=pos.id))
        db_session.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            last_reviewed_at=now - timedelta(hours=8),
        ))
    db_session.commit()

    # Try many seeds; with 5 equal-score candidates, different seeds should
    # eventually pick different candidates.
    results = set()
    for seed in range(50):
        move_san, _, _, _, _ = find_ghost_move(
            db=db_session, user_id=user_id, fen=fen_start, player_color="white",
            _rng_seed=seed,
        )
        results.add(move_san)

    assert len(results) > 1, "50 different seeds all picked the same candidate"


def test_find_ghost_move_session_id_seed_is_stable(db_session):
    """Calling find_ghost_move with the same session_id produces the same result,
    exercising the default _stable_seed path (no _rng_seed override)."""
    import uuid
    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)
    sid = uuid.uuid4()

    fen_start = "8/8/8/8/K7/8/8/7k b - - 0 1"
    fens = [
        "8/8/8/8/1K6/8/8/7k w - - 0 2",
        "8/8/8/8/2K5/8/8/7k w - - 0 2",
        "8/8/8/8/3K4/8/8/7k w - - 0 2",
    ]
    moves = ["mP", "mQ", "mR"]

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_start), fen_raw=fen_start, active_color="black")
    db_session.add(pos_start)
    db_session.flush()

    for fen, move_san in zip(fens, moves):
        pos = Position(user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="white")
        db_session.add(pos)
        db_session.flush()
        db_session.add(Move(from_position_id=pos_start.id, move_san=move_san, to_position_id=pos.id))
        db_session.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            last_reviewed_at=now - timedelta(hours=8),
        ))
    db_session.commit()

    result1 = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
        session_id=sid,
    )
    result2 = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_start, player_color="white",
        session_id=sid,
    )

    assert result1[0] is not None
    assert result1 == result2


def test_find_ghost_move_session_id_seed_normalized_fen(db_session):
    """FENs that differ only in halfmove/fullmove produce the same seed
    because _stable_seed uses fen_hash (normalized position identity)."""
    import uuid
    from app.api.game import find_ghost_move
    from app.fen import fen_hash
    from app.models import Blunder, Move, Position

    user_id = 123
    now = datetime.now(timezone.utc)
    sid = uuid.uuid4()

    # Two FENs identical except halfmove/fullmove counters
    fen_a = "8/8/8/8/K7/8/8/7k b - - 0 1"
    fen_b = "8/8/8/8/K7/8/8/7k b - - 5 20"
    assert fen_hash(fen_a) == fen_hash(fen_b)

    fens_dest = [
        "8/8/8/8/1K6/8/8/7k w - - 0 2",
        "8/8/8/8/2K5/8/8/7k w - - 0 2",
    ]
    move_names = ["mAlpha", "mBeta"]

    pos_start = Position(user_id=user_id, fen_hash=fen_hash(fen_a), fen_raw=fen_a, active_color="black")
    db_session.add(pos_start)
    db_session.flush()

    for fen, move_san in zip(fens_dest, move_names):
        pos = Position(user_id=user_id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="white")
        db_session.add(pos)
        db_session.flush()
        db_session.add(Move(from_position_id=pos_start.id, move_san=move_san, to_position_id=pos.id))
        db_session.add(Blunder(
            user_id=user_id,
            position_id=pos.id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=100,
            last_reviewed_at=now - timedelta(hours=8),
        ))
    db_session.commit()

    result_a = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_a, player_color="white",
        session_id=sid,
    )
    result_b = find_ghost_move(
        db=db_session, user_id=user_id, fen=fen_b, player_color="white",
        session_id=sid,
    )

    assert result_a[0] is not None
    assert result_a == result_b


def test_ghost_selection_dedupes_and_groups_first_moves():
    """Duplicate path rows for one first_move/blunder_id do not inflate group score."""
    from app.api.game import (
        FIRST_MOVE_SECONDARY_WEIGHT,
        GhostMoveCandidate,
        _dedupe_path_candidates,
        _group_candidates_by_first_move,
    )

    duplicate_low = GhostMoveCandidate(
        first_move="e5",
        blunder_id=1,
        depth=3,
        eval_loss_cp=100,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=None,
    )
    duplicate_high = GhostMoveCandidate(
        first_move="e5",
        blunder_id=1,
        depth=2,
        eval_loss_cp=100,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=None,
    )
    distinct_same_first = GhostMoveCandidate(
        first_move="e5",
        blunder_id=2,
        depth=2,
        eval_loss_cp=100,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=None,
    )
    other_first = GhostMoveCandidate(
        first_move="c5",
        blunder_id=3,
        depth=2,
        eval_loss_cp=100,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=None,
    )

    deduped = _dedupe_path_candidates([
        (duplicate_low, 9.0),
        (duplicate_high, 10.0),
        (distinct_same_first, 4.0),
        (other_first, 8.0),
    ])
    groups = _group_candidates_by_first_move(deduped)

    assert len(deduped) == 3
    e5_group = next(group for group in groups if group.first_move == "e5")
    assert len(e5_group.candidates) == 2
    assert e5_group.aggregate_score == 10.0 + FIRST_MOVE_SECONDARY_WEIGHT * 4.0


def test_ghost_selection_weight_flattening_and_zero_fallback():
    from app.api.game import _flatten_selection_weight, _weighted_choice

    assert _flatten_selection_weight(9.0) == 3.0
    assert _flatten_selection_weight(-4.0) == 0.0
    assert _weighted_choice(["first", "second"], [0.0, 0.0], random.Random(1)) == "first"


def test_ghost_repeat_penalties_multiply_by_recency():
    from app.api.game import _repeat_penalties

    assert _repeat_penalties(["e5", "c5", "e5"]) == {
        "e5": 0.35 * 0.80,
        "c5": 0.60,
    }


def test_same_fen_recent_ghost_moves_bounded_and_defensive(db_session, monkeypatch):
    from app.api import game as game_api
    from app.models import GameSession, SessionMove

    user_id = 123
    current_fen = "8/8/8/8/K7/8/8/7k b - - 0 1"
    equivalent_fen = "8/8/8/8/K7/8/8/7k b - - 12 34"
    different_fen = "8/8/8/8/1K6/8/8/7k b - - 0 1"

    def add_history(started_at, fen_before, move_san):
        session = GameSession(
            id=uuid.uuid4(),
            user_id=user_id,
            started_at=started_at,
            status="ended",
            engine_elo=1500,
            player_color="white",
        )
        db_session.add(session)
        db_session.flush()
        db_session.add(SessionMove(
            session_id=session.id,
            move_number=1,
            color="black",
            move_san=move_san,
            fen_before=fen_before,
            fen_after=current_fen,
            decision_source="ghost_path",
        ))

    now = datetime.now(timezone.utc)
    add_history(now, "not-a-fen", "bad")
    add_history(now - timedelta(minutes=1), different_fen, "d5")
    add_history(now - timedelta(minutes=2), equivalent_fen, "e5")
    add_history(now - timedelta(minutes=3), current_fen, "c5")
    db_session.commit()

    monkeypatch.setattr(game_api, "REPEAT_HISTORY_SCAN_LIMIT", 3)
    monkeypatch.setattr(game_api, "REPEAT_PENALTY_LOOKBACK", 2)

    assert game_api._same_fen_recent_ghost_moves(db_session, user_id, current_fen) == ["e5"]


def test_same_fen_recent_ghost_moves_scopes_drill_to_srs_targets(db_session):
    # Amended drill policy (2026-06-01): drill ghost moves contribute to repeat
    # selection ONLY when they are actual SRS-target replays (target_blunder_id set).
    # Pre-root drill ROUTE steering moves (target_blunder_id NULL) must not pollute it.
    from app.api import game as game_api
    from app.fen import fen_hash
    from app.models import Blunder, GameSession, Position, SessionMove

    user_id = 321
    current_fen = "8/8/8/8/K7/8/8/7k b - - 0 1"

    pos = Position(user_id=user_id, fen_hash=fen_hash(current_fen), fen_raw=current_fen, active_color="black")
    db_session.add(pos)
    db_session.flush()
    blunder = Blunder(
        user_id=user_id,
        position_id=pos.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.flush()

    def add_drill_ghost(started_at, move_san, target_blunder_id):
        session = GameSession(
            id=uuid.uuid4(),
            user_id=user_id,
            started_at=started_at,
            status="ended",
            engine_elo=1500,
            player_color="white",
            session_mode="drill",
            drill_state="active",
            is_rated=False,
        )
        db_session.add(session)
        db_session.flush()
        db_session.add(SessionMove(
            session_id=session.id,
            move_number=1,
            color="black",
            move_san=move_san,
            fen_before=current_fen,
            fen_after=current_fen,
            decision_source="ghost_path",
            target_blunder_id=target_blunder_id,
        ))

    now = datetime.now(timezone.utc)
    add_drill_ghost(now, "route", None)          # route steering — excluded
    add_drill_ghost(now - timedelta(minutes=1), "target", blunder.id)  # SRS replay — included
    db_session.commit()

    assert game_api._same_fen_recent_ghost_moves(db_session, user_id, current_fen) == ["target"]


# === Next Opponent Move Endpoint Tests ===


def test_next_opponent_move_ghost_path(client, auth_headers, create_game_session, db_session):
    """Test next-opponent-move returns ghost move with UCI and SAN when path exists."""
    user_id = 123

    # Record a blunder via /api/blunder
    # PGN: 1. e4 e5 2. Qh5 (white blunders with Qh5)
    session_id = create_game_session(user_id=user_id, player_color="white")
    pgn = "1. e4 e5 2. Qh5"
    fen_before_blunder = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    blunder_response = client.post(
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
        headers=auth_headers(user_id=user_id),
    )
    assert blunder_response.status_code == 201

    # Backdate blunder so it's due for SRS review (priority >= 1.0)
    blunder_id = blunder_response.json()["blunder_id"]
    blunder = db_session.get(Blunder, blunder_id)
    blunder.created_at = datetime.now(timezone.utc) - timedelta(hours=5)
    db_session.commit()

    # Start a NEW game and query next-opponent-move
    new_session_id = create_game_session(user_id=user_id, player_color="white")

    # After 1.e4 (black to move), ghost should suggest e5 to reach blunder position
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    response = client.post(
        "/api/game/next-opponent-move",
        json={"session_id": str(new_session_id), "fen": fen_after_e4},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "ghost"
    assert data["move"]["san"] == "e5"
    assert data["move"]["uci"] == "e7e5"
    assert data["target_blunder_id"] is not None
    assert data["decision_source"] == "ghost_path"


def test_next_opponent_move_engine_fallback(client, auth_headers, create_game_session):
    """Test next-opponent-move returns engine move when no ghost path exists."""
    from unittest.mock import patch
    from app.opponent_move_controller import ControllerMove

    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    fake_move = ControllerMove(uci="e7e5", san="e5", method="maia3_api")
    fen_after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    with patch("app.opponent_move_controller.choose_move", return_value=fake_move):
        response = client.post(
            "/api/game/next-opponent-move",
            json={"session_id": str(session_id), "fen": fen_after_e4},
            headers=auth_headers(user_id=user_id),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "engine"
    assert "uci" in data["move"]
    assert "san" in data["move"]
    assert data["target_blunder_id"] is None
    assert data["decision_source"] == "backend_engine"


def test_next_opponent_move_players_turn_error(client, auth_headers, create_game_session):
    """Test next-opponent-move returns error when it's the player's turn."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    # Position where it's white's turn (player's turn)
    white_turn_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

    response = client.post(
        "/api/game/next-opponent-move",
        json={"session_id": str(session_id), "fen": white_turn_fen},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 400
    assert "player's turn" in response.json()["detail"].lower()


def test_next_opponent_move_invalid_fen(client, auth_headers, create_game_session):
    """Test next-opponent-move returns error for invalid FEN."""
    user_id = 123
    session_id = create_game_session(user_id=user_id, player_color="white")

    response = client.post(
        "/api/game/next-opponent-move",
        json={"session_id": str(session_id), "fen": "invalid-fen"},
        headers=auth_headers(user_id=user_id),
    )

    assert response.status_code == 400
    assert "invalid fen" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# g-mxeo: opening-baseline capture moved OFF the /start request path
# ---------------------------------------------------------------------------
def test_game_start_does_not_run_digest_on_request_path(client, auth_headers, db_session):
    # Load-bearing contract: NO O(evidence) digest runs before the /start response
    # (g-mxeo) — and since g-jact none runs on the drained worker either: the
    # freshness verdict for the seeded batch is the cheap partitioned signal, so
    # the digest count stays 0 across the entire capture while the baseline still
    # persists.
    #
    # A dedicated user_id isolates the counter from any background opening-score
    # recompute thread a sibling test's /game/end may have left running (those fire
    # the digest for OTHER users); we count only digests for THIS test's user.
    baseline_user = 990101
    _seed_fresh_batch(
        db_session, user_id=baseline_user, player_color="white",
        computed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        scores={"opening-x": 41.0},
    )
    calls = {"n": 0}
    real_digest = oc.raw_evidence_inputs_digest
    real_snapshot = oc.raw_evidence_inputs_snapshot

    def counting_digest(db, user_id, player_color, *args, **kwargs):
        if user_id == baseline_user:
            calls["n"] += 1
        return real_digest(db, user_id, player_color, *args, **kwargs)

    def counting_snapshot(db, user_id, player_color, *args, **kwargs):
        if user_id == baseline_user:
            calls["n"] += 1
        return real_snapshot(db, user_id, player_color, *args, **kwargs)

    sched, enqueue_patch = _inject_baseline_scheduler("app.api.game")
    with (
        patch("app.opening_cache.raw_evidence_inputs_digest", counting_digest),
        patch("app.opening_cache.raw_evidence_inputs_snapshot", counting_snapshot),
        enqueue_patch,
    ):
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=baseline_user),
        )
        assert resp.status_code == 201
        sid = uuid.UUID(resp.json()["session_id"])

        # Exactly one queued job, and NO digest ran on the request path.
        with sched._cond:
            assert len(sched._pending) == 1
        assert calls["n"] == 0

        # g-jact: the drained job proves freshness via the cheap signal — the
        # O(evidence) digest never runs, yet the baseline persists below.
        sched.run_due()
        assert calls["n"] == 0

    db_session.expire_all()
    session = db_session.get(GameSession, sid)
    assert session.baseline_watermark_seq is not None
    assert session.baseline_watermark_epoch is not None
    assert session.baseline_watermark_fingerprint is not None
    assert json.loads(session.opening_score_baseline) == {
        "schema_version": 1,
        "model_version": oc.SCORE_MODEL_VERSION,
        "root_calc_config_fingerprint": oc.root_calc_config_fingerprint(),
        "scores": {"opening-x": 41.0},
    }


def test_game_start_accepts_later_batch_when_watermark_still_matches(
    client, auth_headers, db_session
):
    sched, enqueue_patch = _inject_baseline_scheduler("app.api.game")
    with enqueue_patch:
        resp = client.post(
            "/api/game/start",
            json={"engine_elo": 1500, "player_color": "white"},
            headers=auth_headers(user_id=123),
        )
        assert resp.status_code == 201
        sid = uuid.UUID(resp.json()["session_id"])

        # A recompute lands after the session started, but no relevant input moved.
        _seed_fresh_batch(
            db_session, user_id=123, player_color="white",
            computed_at=datetime.now(timezone.utc) + timedelta(hours=1),
            scores={"opening-x": 41.0},
        )
        sched.run_due()

    db_session.expire_all()
    session = db_session.get(GameSession, sid)
    assert session.baseline_watermark_seq is not None
    assert session.baseline_watermark_epoch is not None
    assert session.baseline_watermark_fingerprint is not None
    assert json.loads(session.opening_score_baseline) == {
        "schema_version": 1,
        "model_version": oc.SCORE_MODEL_VERSION,
        "root_calc_config_fingerprint": oc.root_calc_config_fingerprint(),
        "scores": {"opening-x": 41.0},
    }


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
