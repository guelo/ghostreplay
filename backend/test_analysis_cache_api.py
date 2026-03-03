"""Tests for analysis cache: lookup endpoint and auto-population from session moves."""

from app.models import AnalysisCache


STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def _seed_cache(db_session, entries: list[dict]) -> None:
    for entry in entries:
        db_session.add(AnalysisCache(**entry))
    db_session.commit()


def test_lookup_returns_cached_hit(client, auth_headers, db_session):
    _seed_cache(db_session, [
        {
            "fen_before": STARTING_FEN,
            "move_uci": "e2e4",
            "move_san": "e4",
            "best_move_uci": "e2e4",
            "best_move_san": "e4",
            "played_eval": 20,
            "best_eval": 20,
            "eval_delta": 0,
        },
    ])

    response = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    data = response.json()
    key = f"{STARTING_FEN}::e2e4"
    assert key in data["results"]
    result = data["results"][key]
    assert result["move_san"] == "e4"
    assert result["played_eval"] == 20
    assert result["best_eval"] == 20
    assert result["eval_delta"] == 0


def test_lookup_returns_empty_for_miss(client, auth_headers):
    response = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "d2d4"}]},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["results"] == {}


def test_lookup_batch_mixed_hits_and_misses(client, auth_headers, db_session):
    _seed_cache(db_session, [
        {
            "fen_before": STARTING_FEN,
            "move_uci": "e2e4",
            "move_san": "e4",
            "best_move_uci": "e2e4",
            "best_move_san": "e4",
            "played_eval": 20,
            "best_eval": 20,
            "eval_delta": 0,
        },
    ])

    response = client.post(
        "/api/analysis/lookup",
        json={
            "positions": [
                {"fen": STARTING_FEN, "move_uci": "e2e4"},
                {"fen": STARTING_FEN, "move_uci": "d2d4"},
                {"fen": AFTER_E4_FEN, "move_uci": "e7e5"},
            ]
        },
        headers=auth_headers(),
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert f"{STARTING_FEN}::e2e4" in results


def test_lookup_rejects_empty_positions(client, auth_headers):
    response = client.post(
        "/api/analysis/lookup",
        json={"positions": []},
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_lookup_rejects_too_many_positions(client, auth_headers):
    positions = [
        {"fen": f"fen_{i}", "move_uci": "e2e4"} for i in range(61)
    ]
    response = client.post(
        "/api/analysis/lookup",
        json={"positions": positions},
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_lookup_requires_auth(client):
    response = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
    )

    assert response.status_code == 401


# --- Cache auto-population from session move uploads ---


def test_session_moves_with_cache_fields_populates_cache(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_after": AFTER_E4_FEN,
                    "eval_cp": 20,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 20,
                    "eval_delta": 0,
                    "classification": "best",
                    "fen_before": STARTING_FEN,
                    "move_uci": "e2e4",
                    "best_move_uci": "e2e4",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200

    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN,
        AnalysisCache.move_uci == "e2e4",
    ).first()
    assert cached is not None
    assert cached.move_san == "e4"
    assert cached.played_eval == 20  # white move, no sign flip
    assert cached.best_eval == 20
    assert cached.eval_delta == 0


def test_session_moves_black_eval_flipped_for_cache(
    client, auth_headers, create_game_session, db_session
):
    """Black move evals are player-relative (positive = good for black).
    The cache stores white-relative, so they should be negated."""
    session_id = create_game_session(user_id=123, player_color="black")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_after": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                    "eval_cp": 15,
                    "best_move_san": "e5",
                    "best_move_eval_cp": 15,
                    "eval_delta": 0,
                    "classification": "best",
                    "fen_before": AFTER_E4_FEN,
                    "move_uci": "e7e5",
                    "best_move_uci": "e7e5",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200

    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == AFTER_E4_FEN,
        AnalysisCache.move_uci == "e7e5",
    ).first()
    assert cached is not None
    assert cached.played_eval == -15  # flipped for white-relative
    assert cached.best_eval == -15


def test_session_moves_without_cache_fields_skips_cache(
    client, auth_headers, create_game_session, db_session
):
    """Old clients that don't send fen_before/move_uci should not populate cache."""
    session_id = create_game_session(user_id=123, player_color="white")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "d4",
                    "fen_after": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1",
                    "eval_cp": 30,
                    "best_move_san": "e4",
                    "best_move_eval_cp": 35,
                    "eval_delta": 5,
                    "classification": "good",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    count = db_session.query(AnalysisCache).count()
    assert count == 0
