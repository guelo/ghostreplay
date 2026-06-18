"""Tests for analysis cache: lookup endpoint and auto-population from session moves."""

from unittest.mock import patch

import pytest

import uuid

from app.fen import normalize_fen
from app.models import AnalysisCache, GameSession, PositionAnalysisRow, SessionMove


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
    # Move-grain evidence stays from the exact (fen, move) row.
    assert result["played_eval"] == 20
    assert result["eval_delta"] == 0
    assert result["classification"] is None  # legacy row without classification
    # Position-grain best_eval is now resolved separately and is null for this
    # untrusted legacy row (no identity), never copied off the move row.
    assert result["best_eval"] is None
    assert result["position_trusted"] is False
    assert result["move_trusted"] is False


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
    assert cached.classification == "best"


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
    assert cached.classification == "best"


def test_session_moves_mate_round_trips_through_cache_and_lookup(
    client, auth_headers, create_game_session, db_session
):
    """eval_mate is player-relative on upload, stored white-relative, and
    returned white-relative from the lookup endpoint."""
    session_id = create_game_session(user_id=123, player_color="black")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "Qh4#",
                    "fen_after": "rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3",
                    "eval_cp": 10000,
                    "eval_mate": 1,
                    "best_move_san": "Qh4#",
                    "best_move_eval_cp": 10000,
                    "eval_delta": 0,
                    "classification": "best",
                    "fen_before": AFTER_E4_FEN,
                    "move_uci": "d8h4",
                    "best_move_uci": "d8h4",
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200

    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == AFTER_E4_FEN,
        AnalysisCache.move_uci == "d8h4",
    ).first()
    assert cached is not None
    # Black player mate in 1 → white-relative -1.
    assert cached.played_eval_mate == -1

    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": AFTER_E4_FEN, "move_uci": "d8h4"}]},
        headers=auth_headers(user_id=123),
    )
    assert lookup.status_code == 200
    result = lookup.json()["results"][f"{AFTER_E4_FEN}::d8h4"]
    assert result["played_eval_mate"] == -1


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


def test_session_moves_active_drill_populates_cache(
    client, auth_headers, create_game_session, db_session
):
    """Amended drill policy (2026-06-01): pre-continue uploads from an unconverted
    (active) drill feed the regular analysis-cache side effect like a normal game."""
    session_id = create_game_session(user_id=123, player_color="white")
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.session_mode = "drill"
    session.drill_state = "active"
    session.is_rated = False
    db_session.commit()

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
    assert cached.classification == "best"


def test_session_moves_active_drill_refreshes_opening_scores(
    client, auth_headers, create_game_session, db_session
):
    """The opening-score refresh side effect fires for active-drill uploads too."""
    session_id = create_game_session(user_id=123, player_color="white")
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).one()
    session.session_mode = "drill"
    session.drill_state = "active"
    session.is_rated = False
    db_session.commit()

    with patch("app.api.session.request_recompute", return_value=None) as refresh:
        response = client.post(
            f"/api/session/{session_id}/moves",
            json={
                "moves": [
                    {
                        "move_number": 1, "color": "white", "move_san": "e4",
                        "fen_after": AFTER_E4_FEN, "eval_delta": 0, "classification": "best",
                        "fen_before": STARTING_FEN, "move_uci": "e2e4",
                    },
                ]
            },
            headers=auth_headers(user_id=123),
        )

    assert response.status_code == 200
    refresh.assert_called_once()
    _, args, _kwargs = refresh.mock_calls[0]
    assert args[0] == 123
    assert args[1] == "white"


def test_session_moves_abandoned_drill_skips_expensive_evidence_side_effects(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).one()
    session.session_mode = "drill"
    session.drill_state = "abandoned"
    session.is_rated = False
    db_session.commit()

    with patch("app.api.session.request_recompute", return_value=None) as refresh:
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
    assert response.json() == {
        "moves_inserted": 1,
        "drill_state": "abandoned",
    }
    assert (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session_uuid)
        .count()
        == 1
    )
    assert db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN,
        AnalysisCache.move_uci == "e2e4",
    ).first() is None
    refresh.assert_not_called()


def test_session_moves_natural_ended_drill_skips_expensive_evidence_side_effects(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    session_uuid = uuid.UUID(session_id)
    session = db_session.query(GameSession).filter(GameSession.id == session_uuid).one()
    session.session_mode = "drill"
    session.drill_state = "failed"
    session.drill_terminal_reason = "natural_end"
    session.status = "ended"
    session.result = "draw"
    session.is_rated = False
    db_session.commit()

    with patch("app.api.session.request_recompute", return_value=None) as refresh:
        response = client.post(
            f"/api/session/{session_id}/moves",
            json={
                "moves": [
                    {
                        "move_number": 1,
                        "color": "white",
                        "move_san": "e4",
                        "fen_after": AFTER_E4_FEN,
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
    assert response.json() == {
        "moves_inserted": 1,
        "drill_state": "failed",
        "drill_terminal_reason": "natural_end",
    }
    assert db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN,
        AnalysisCache.move_uci == "e2e4",
    ).first() is None
    refresh.assert_not_called()


def test_lookup_returns_classification_when_present(client, auth_headers, db_session):
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
            "classification": "best",
        },
    ])

    response = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(),
    )

    assert response.status_code == 200
    key = f"{STARTING_FEN}::e2e4"
    result = response.json()["results"][key]
    assert result["classification"] == "best"


def test_best_line_uci_round_trips_through_cache_and_lookup(
    client, auth_headers, create_game_session, db_session
):
    """best_line_uci is stored on both session_moves and analysis_cache from a
    browser upload. Post-Phase-4 it is a POSITION-grain fact, so /lookup only serves
    it from a TRUSTED position — a non-authoritative browser upload does not surface
    it (null), even though it is persisted."""
    from app.models import SessionMove

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
                    "best_line_uci": ["e2e4", "e7e5", "g1f3"],
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )
    assert response.status_code == 200

    move_row = db_session.query(SessionMove).filter(
        SessionMove.session_id == uuid.UUID(str(session_id)),
        SessionMove.move_number == 1,
    ).first()
    assert move_row is not None
    assert move_row.best_line_uci == "e2e4 e7e5 g1f3"

    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN,
        AnalysisCache.move_uci == "e2e4",
    ).first()
    assert cached is not None
    assert cached.best_line_uci == "e2e4 e7e5 g1f3"

    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(user_id=123),
    )
    assert lookup.status_code == 200
    result = lookup.json()["results"][f"{STARTING_FEN}::e2e4"]
    # Browser upload is untrusted at the position grain -> best_line_uci is suppressed.
    assert result["best_line_uci"] is None
    assert result["position_trusted"] is False


def test_session_analysis_position_analysis_includes_best_line_uci(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")

    client.post(
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
                    "best_line_uci": ["e2e4", "e7e5", "g1f3"],
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    analysis = client.get(
        f"/api/session/{session_id}/analysis",
        headers=auth_headers(user_id=123),
    )
    assert analysis.status_code == 200
    position_analysis = analysis.json()["position_analysis"]
    assert position_analysis[STARTING_FEN]["best_line_uci"] == ["e2e4", "e7e5", "g1f3"]


def test_lookup_best_line_uci_null_for_legacy_rows(client, auth_headers, db_session):
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
    result = response.json()["results"][f"{STARTING_FEN}::e2e4"]
    assert result["best_line_uci"] is None


# --- Quality metadata: provenance, trust flag, and non-downgrade ---


def _canonical_seed_values():
    from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile

    p = get_profile(CANONICAL_PROFILE_ID)
    values = {"analysis_profile_id": CANONICAL_PROFILE_ID, "evidence_contract_id": "resolver-complete-v1"}
    for f in IDENTITY_FIELDS:
        values[f] = getattr(p, f)
    return values


def test_browser_upload_stamps_non_authoritative_metadata(
    client, auth_headers, create_game_session, db_session
):
    session_id = create_game_session(user_id=123, player_color="white")
    client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": [{
            "move_number": 1, "color": "white", "move_san": "e4",
            "fen_after": AFTER_E4_FEN, "eval_cp": 20, "best_move_san": "e4",
            "best_move_eval_cp": 20, "eval_delta": 0, "classification": "best",
            "fen_before": STARTING_FEN, "move_uci": "e2e4", "best_move_uci": "e2e4",
            "best_line_uci": ["e2e4", "e7e5"],
        }]},
        headers=auth_headers(user_id=123),
    )
    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN, AnalysisCache.move_uci == "e2e4",
    ).first()
    assert cached.analysis_profile_id == "browser-game-v1"
    assert cached.evidence_contract_id == "resolver-complete-v1"

    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(user_id=123),
    )
    result = lookup.json()["results"][f"{STARTING_FEN}::e2e4"]
    assert result["analysis_profile_id"] == "browser-game-v1"
    assert result["source"] == "game"
    assert result["authoritative"] is False


def test_game_upload_does_not_downgrade_canonical_row(
    client, auth_headers, create_game_session, db_session
):
    _seed_cache(db_session, [{
        "fen_before": STARTING_FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "eval_delta": 0, "classification": "best",
        "played_eval": 20, "best_eval": 20, "source": "precomputed",
        **_canonical_seed_values(),
    }])

    client.post(
        f"/api/session/{create_game_session(user_id=123, player_color='white')}/moves",
        json={"moves": [{
            "move_number": 1, "color": "white", "move_san": "e4",
            "fen_after": AFTER_E4_FEN, "eval_cp": 999, "best_move_san": "e4",
            "best_move_eval_cp": 999, "eval_delta": 0, "classification": "blunder",
            "fen_before": STARTING_FEN, "move_uci": "e2e4", "best_move_uci": "e2e4",
            "best_line_uci": ["e2e4", "e7e5"],
        }]},
        headers=auth_headers(user_id=123),
    )

    cached = db_session.query(AnalysisCache).filter(
        AnalysisCache.fen_before == STARTING_FEN, AnalysisCache.move_uci == "e2e4",
    ).first()
    assert cached.source == "precomputed"
    assert cached.played_eval == 20  # canonical evidence preserved

    lookup = client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(user_id=123),
    )
    result = lookup.json()["results"][f"{STARTING_FEN}::e2e4"]
    assert result["authoritative"] is True


# --- Backend-computed trust: contract_satisfied + trusted_for_resolution ---


def _canonical_v2_seed_values():
    """Canonical identity, but stamped with the resolver-complete-v2 contract."""
    values = _canonical_seed_values()
    values["evidence_contract_id"] = "resolver-complete-v2"
    return values


def _seed_v2_canonical(db_session, **overrides):
    row = {
        "fen_before": STARTING_FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "eval_delta": 0, "classification": "best",
        "played_eval": 20, "best_eval": 20, "source": "precomputed",
        **_canonical_v2_seed_values(),
    }
    row.update(overrides)
    _seed_cache(db_session, [row])


def _lookup_e4(client, auth_headers):
    return client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": STARTING_FEN, "move_uci": "e2e4"}]},
        headers=auth_headers(),
    ).json()["results"][f"{STARTING_FEN}::e2e4"]


def test_trusted_for_resolution_true_for_authoritative_v2_row(
    client, auth_headers, db_session
):
    _seed_v2_canonical(db_session)
    result = _lookup_e4(client, auth_headers)
    assert result["authoritative"] is True
    assert result["contract_satisfied"] is True
    assert result["trusted_for_resolution"] is True


def test_trust_false_when_contract_is_v1_even_if_authoritative(
    client, auth_headers, db_session
):
    # v1 contract: identity is authoritative and v1's own validation passes, but
    # trust requires resolver-complete-v2 specifically.
    _seed_cache(db_session, [{
        "fen_before": STARTING_FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "eval_delta": 0, "classification": "best",
        "played_eval": 20, "best_eval": 20, "source": "precomputed",
        **_canonical_seed_values(),
    }])
    result = _lookup_e4(client, auth_headers)
    assert result["authoritative"] is True
    assert result["contract_satisfied"] is True  # v1 validator passes
    assert result["trusted_for_resolution"] is False


def test_trust_false_when_not_authoritative(
    client, auth_headers, create_game_session, db_session
):
    # A browser upload satisfies a contract but is not from an authoritative
    # profile, so it is never trusted for resolution.
    client.post(
        f"/api/session/{create_game_session(user_id=123, player_color='white')}/moves",
        json={"moves": [{
            "move_number": 1, "color": "white", "move_san": "e4",
            "fen_after": AFTER_E4_FEN, "eval_cp": 20, "best_move_san": "e4",
            "best_move_eval_cp": 20, "eval_delta": 0, "classification": "best",
            "fen_before": STARTING_FEN, "move_uci": "e2e4", "best_move_uci": "e2e4",
            "best_line_uci": ["e2e4", "e7e5"],
        }]},
        headers=auth_headers(user_id=123),
    )
    result = _lookup_e4(client, lambda: auth_headers(user_id=123))
    assert result["authoritative"] is False
    assert result["trusted_for_resolution"] is False


def test_trust_false_when_v2_validation_fails(client, auth_headers, db_session):
    # Authoritative + v2 contract id, but the stored delta is inconsistent with
    # the eval triple (white to move: expected best-played = 0, stored 40), so
    # the v2 validator fails closed.
    _seed_v2_canonical(db_session, eval_delta=40)
    result = _lookup_e4(client, auth_headers)
    assert result["authoritative"] is True
    assert result["contract_satisfied"] is False
    assert result["trusted_for_resolution"] is False


# --- Phase 4: grain-split lookup (separate position + move evidence) -----------

# Clock variants of one position (same normalized FEN) so the move row and the
# position storage row live at different full FENs -> proves transposition.
_REQ_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
_STORE_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7"


def _seed_position_storage(db_session, *, fen, best_move_uci, best_line_uci,
                           best_eval=None, best_eval_mate=None):
    from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile

    profile = get_profile(CANONICAL_PROFILE_ID)
    identity = {f: getattr(profile, f) for f in IDENTITY_FIELDS}
    db_session.add(PositionAnalysisRow(
        normalized_fen=normalize_fen(fen), fen=fen,
        best_move_uci=best_move_uci, best_move_san=best_move_uci,
        best_line_uci=best_line_uci, best_eval=best_eval, best_eval_mate=best_eval_mate,
        source="precomputed", analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="position-complete-v1", **identity,
    ))
    db_session.commit()


def _lookup(client, auth_headers, fen, move_uci):
    return client.post(
        "/api/analysis/lookup",
        json={"positions": [{"fen": fen, "move_uci": move_uci}]},
        headers=auth_headers(),
    ).json()["results"]


def test_lookup_both_grains_position_by_norm_move_by_exact(client, auth_headers, db_session):
    # Move evidence comes from the exact (fen, move_uci) row; position evidence is
    # resolved separately by NORMALIZED FEN from a clock-variant storage winner whose
    # best move DIFFERS from the move row -> flattened best is derived from the
    # position payload, never copied off the move row.
    _seed_cache(db_session, [{
        "fen_before": _REQ_FEN, "normalized_fen_before": normalize_fen(_REQ_FEN),
        "move_uci": "f1c4", "move_san": "Bc4",
        "best_move_uci": "f1c4", "best_move_san": "Bc4", "best_line_uci": "f1c4 g8f6",
        "played_eval": 18, "best_eval": 18, "eval_delta": 0, "classification": "best",
        "source": "precomputed", **_canonical_v2_seed_values(),
    }])
    _seed_position_storage(db_session, fen=_STORE_FEN, best_move_uci="f1b5",
                           best_line_uci="f1b5 a7a6", best_eval=40)

    result = _lookup(client, auth_headers, _REQ_FEN, "f1c4")[f"{_REQ_FEN}::f1c4"]
    # Move grain (exact row).
    assert result["played_eval"] == 18
    assert result["classification"] == "best"
    assert result["move_trusted"] is True
    # Position grain (transposed storage winner), independent of the move grain.
    assert result["position_trusted"] is True
    assert result["best_move_uci"] == "f1b5"        # storage, not the f1c4 move row
    assert result["best_line_uci"] == ["f1b5", "a7a6"]
    assert result["best_eval"] == 40                # white-relative, no sign convert


def test_lookup_move_only_untrusted_position_null(client, auth_headers, db_session):
    # An untrusted/legacy move row with no trusted position: a result is still emitted
    # (gated on the exact move row), but position_trusted is False and every flattened
    # best-move field is null.
    _seed_cache(db_session, [{
        "fen_before": STARTING_FEN, "normalized_fen_before": normalize_fen(STARTING_FEN),
        "move_uci": "e2e4", "move_san": "e4", "best_move_uci": "e2e4",
        "best_line_uci": "e2e4 e7e5", "played_eval": 20, "best_eval": 20,
        "eval_delta": 0, "classification": "best",
    }])

    results = _lookup(client, auth_headers, STARTING_FEN, "e2e4")
    result = results[f"{STARTING_FEN}::e2e4"]
    assert result["move_san"] == "e4"
    assert result["played_eval"] == 20            # move grain retained
    assert result["move_trusted"] is False
    assert result["position_trusted"] is False
    assert result["best_move_uci"] is None
    assert result["best_line_uci"] is None
    assert result["best_eval"] is None
    assert result["best_eval_mate"] is None


def test_lookup_position_only_no_move_row_suppressed(client, auth_headers, db_session):
    # A trusted storage row exists but NO exact move row: no result is emitted
    # (position-only hits are intentionally suppressed until Phase 5).
    _seed_position_storage(db_session, fen=STARTING_FEN, best_move_uci="e2e4",
                           best_line_uci="e2e4 e7e5", best_eval=25)

    results = _lookup(client, auth_headers, STARTING_FEN, "e2e4")
    assert results == {}


def test_lookup_move_complete_mate_only_contract_satisfied(client, auth_headers, db_session):
    # A native move-complete-v1 row whose played evidence is mate-only must read
    # contract_satisfied=True (move-complete-v1 accepts played_eval_mate) and
    # move_trusted=True -- the two diagnostics must agree.
    from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile

    profile = get_profile(CANONICAL_PROFILE_ID)
    identity = {f: getattr(profile, f) for f in IDENTITY_FIELDS}
    _seed_cache(db_session, [{
        "fen_before": STARTING_FEN, "normalized_fen_before": normalize_fen(STARTING_FEN),
        "move_uci": "e2e4", "move_san": "e4",
        "played_eval": None, "played_eval_mate": 3, "classification": "best",
        "source": "precomputed", "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": "move-complete-v1", **identity,
    }])

    result = _lookup(client, auth_headers, STARTING_FEN, "e2e4")[f"{STARTING_FEN}::e2e4"]
    assert result["played_eval_mate"] == 3
    assert result["contract_satisfied"] is True
    assert result["move_trusted"] is True


def test_lookup_batch_resolves_transposition_for_multiple_positions(
    client, auth_headers, db_session
):
    # Two emitted positions that are clock variants of one another (same normalized
    # FEN) both resolve their position evidence from a single storage winner via the
    # batched resolver path.
    _seed_cache(db_session, [
        {
            "fen_before": _REQ_FEN, "normalized_fen_before": normalize_fen(_REQ_FEN),
            "move_uci": "f1c4", "move_san": "Bc4", "best_move_uci": "f1c4",
            "best_line_uci": "f1c4 g8f6", "played_eval": 18, "best_eval": 18,
            "eval_delta": 0, "classification": "best", "source": "precomputed",
            **_canonical_v2_seed_values(),
        },
        {
            "fen_before": _STORE_FEN, "normalized_fen_before": normalize_fen(_STORE_FEN),
            "move_uci": "f1c4", "move_san": "Bc4", "best_move_uci": "f1c4",
            "best_line_uci": "f1c4 g8f6", "played_eval": 12, "best_eval": 12,
            "eval_delta": 0, "classification": "best", "source": "precomputed",
            **_canonical_v2_seed_values(),
        },
    ])
    _seed_position_storage(db_session, fen=_STORE_FEN, best_move_uci="f1b5",
                           best_line_uci="f1b5 a7a6", best_eval=40)

    results = client.post(
        "/api/analysis/lookup",
        json={"positions": [
            {"fen": _REQ_FEN, "move_uci": "f1c4"},
            {"fen": _STORE_FEN, "move_uci": "f1c4"},
        ]},
        headers=auth_headers(),
    ).json()["results"]
    # Both clock variants resolve the same trusted storage winner.
    for fen in (_REQ_FEN, _STORE_FEN):
        entry = results[f"{fen}::f1c4"]
        assert entry["position_trusted"] is True
        assert entry["best_move_uci"] == "f1b5"
        assert entry["best_eval"] == 40
