from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import patch

import pytest

from app.fen import active_color
from app.models import (
    Blunder,
    GameSession,
    OpeningScoreBatch,
    OpeningScoreCursor,
    Position,
    SessionMove,
    UserOpeningScore,
)
from app.opening_cache import (
    ensure_opening_scores,
    get_latest_opening_score_batch,
    list_cached_opening_scores,
    list_opening_score_candidate_pairs,
    recompute_opening_scores,
)
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
KINGS_PAWN_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
OPEN_GAME_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
KNIGHT_OPENING_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -"
TWO_KNIGHTS_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"

START_FULL = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
KINGS_PAWN_FULL = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
OPEN_GAME_FULL = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"
KNIGHT_OPENING_FULL = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
TWO_KNIGHTS_FULL = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"


def _make_graph() -> OpeningGraph:
    start_node = OpeningGraphNode(START_FEN, active_color(START_FEN))
    kings_pawn_node = OpeningGraphNode(KINGS_PAWN_FEN, active_color(KINGS_PAWN_FEN))
    open_game_node = OpeningGraphNode(OPEN_GAME_FEN, active_color(OPEN_GAME_FEN))
    knight_opening_node = OpeningGraphNode(KNIGHT_OPENING_FEN, active_color(KNIGHT_OPENING_FEN))
    two_knights_node = OpeningGraphNode(TWO_KNIGHTS_FEN, active_color(TWO_KNIGHTS_FEN))

    start_node.children["e2e4"] = KINGS_PAWN_FEN
    kings_pawn_node.parents.add((START_FEN, "e2e4"))

    kings_pawn_node.children["e7e5"] = OPEN_GAME_FEN
    open_game_node.parents.add((KINGS_PAWN_FEN, "e7e5"))

    open_game_node.children["g1f3"] = KNIGHT_OPENING_FEN
    knight_opening_node.parents.add((OPEN_GAME_FEN, "g1f3"))

    knight_opening_node.children["b8c6"] = TWO_KNIGHTS_FEN
    two_knights_node.parents.add((KNIGHT_OPENING_FEN, "b8c6"))

    graph = OpeningGraph(
        {
            START_FEN: start_node,
            KINGS_PAWN_FEN: kings_pawn_node,
            OPEN_GAME_FEN: open_game_node,
            KNIGHT_OPENING_FEN: knight_opening_node,
            TWO_KNIGHTS_FEN: two_knights_node,
        },
        START_FEN,
    )
    graph.freeze()
    return graph


def _make_roots() -> OpeningRoots:
    kings_pawn = OpeningRoot(
        opening_key=KINGS_PAWN_FEN,
        opening_name="King's Pawn Game",
        opening_family="Open Games",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset([KNIGHT_OPENING_FEN]),
    )
    knight_opening = OpeningRoot(
        opening_key=KNIGHT_OPENING_FEN,
        opening_name="King's Knight Opening",
        opening_family="Open Games",
        eco="C40",
        depth=3,
        parent_keys=frozenset([KINGS_PAWN_FEN]),
        child_keys=frozenset(),
    )
    return OpeningRoots(
        {
            KINGS_PAWN_FEN: kings_pawn,
            KNIGHT_OPENING_FEN: knight_opening,
        },
        {
            KINGS_PAWN_FEN: frozenset([KINGS_PAWN_FEN]),
            OPEN_GAME_FEN: frozenset([KINGS_PAWN_FEN]),
            KNIGHT_OPENING_FEN: frozenset([KNIGHT_OPENING_FEN]),
            TWO_KNIGHTS_FEN: frozenset([KNIGHT_OPENING_FEN]),
        },
    )


@pytest.fixture(autouse=True)
def _mock_opening_cache_singletons():
    with (
        patch("app.opening_cache.get_opening_graph", return_value=_make_graph()),
        patch("app.opening_cache.get_opening_roots", return_value=_make_roots()),
    ):
        yield


def _create_session_row(db_session, *, user_id: int, player_color: str) -> GameSession:
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        status="completed",
        result="win",
        engine_elo=1500,
        player_color=player_color,
    )
    db_session.add(session)
    db_session.commit()
    return session


def _seed_black_opening_session(db_session, *, user_id: int = 123) -> GameSession:
    session = _create_session_row(db_session, user_id=user_id, player_color="black")
    db_session.add_all(
        [
            SessionMove(
                session_id=session.id,
                move_number=1,
                color="white",
                move_san="e4",
                fen_before=START_FULL,
                fen_after=KINGS_PAWN_FULL,
                eval_delta=0,
            ),
            SessionMove(
                session_id=session.id,
                move_number=1,
                color="black",
                move_san="e5",
                fen_before=KINGS_PAWN_FULL,
                fen_after=OPEN_GAME_FULL,
                eval_delta=0,
            ),
            SessionMove(
                session_id=session.id,
                move_number=2,
                color="white",
                move_san="Nf3",
                fen_before=OPEN_GAME_FULL,
                fen_after=KNIGHT_OPENING_FULL,
                eval_delta=0,
            ),
            SessionMove(
                session_id=session.id,
                move_number=2,
                color="black",
                move_san="Nc6",
                fen_before=KNIGHT_OPENING_FULL,
                fen_after=TWO_KNIGHTS_FULL,
                eval_delta=0,
            ),
        ]
    )
    db_session.commit()
    return session


def test_recompute_writes_one_coherent_batch(db_session):
    _seed_black_opening_session(db_session)

    batch = recompute_opening_scores(db_session, 123, "black")
    _, rows = list_cached_opening_scores(db_session, 123, "black")

    assert batch.user_id == 123
    assert batch.player_color == "black"
    assert {row.opening_key for row in rows} == {KINGS_PAWN_FEN, KNIGHT_OPENING_FEN}
    assert all(row.batch_id == batch.id for row in rows)
    assert all(row.user_id == 123 for row in rows)
    assert all(row.player_color == "black" for row in rows)
    assert all(row.computed_at == batch.computed_at for row in rows)


def test_latest_batch_read_selects_only_latest_batch(db_session):
    _seed_black_opening_session(db_session)

    first_batch = recompute_opening_scores(db_session, 123, "black")
    db_session.add(
        UserOpeningScore(
            batch_id=first_batch.id,
            user_id=123,
            player_color="black",
            opening_key="legacy/root",
            opening_name="Legacy Root",
            opening_family="Legacy Family",
            opening_score=1.0,
            confidence=1.0,
            coverage=1.0,
            weighted_depth=1.0,
            sample_size=1,
            computed_at=first_batch.computed_at,
        )
    )
    db_session.commit()

    second_batch = recompute_opening_scores(db_session, 123, "black")
    batch, rows = list_cached_opening_scores(db_session, 123, "black")

    assert (
        db_session.query(OpeningScoreBatch)
        .filter(OpeningScoreBatch.user_id == 123, OpeningScoreBatch.player_color == "black")
        .count()
        == 2
    )
    assert batch is not None
    assert batch.id == second_batch.id
    assert all(row.batch_id == second_batch.id for row in rows)
    assert "legacy/root" not in {row.opening_key for row in rows}


def test_latest_batch_prefers_newer_snapshot_time_over_higher_insert_id(db_session):
    newer_time = datetime.now(timezone.utc)
    older_time = newer_time - timedelta(minutes=5)

    newer_batch = OpeningScoreBatch(
        user_id=123,
        player_color="black",
        generation=2,
        computed_at=newer_time,
    )
    db_session.add(newer_batch)
    db_session.flush()
    db_session.add(
        UserOpeningScore(
            batch_id=newer_batch.id,
            user_id=123,
            player_color="black",
            opening_key=KINGS_PAWN_FEN,
            opening_name="Newer Snapshot",
            opening_family="Open Games",
            opening_score=80.0,
            confidence=50.0,
            coverage=60.0,
            weighted_depth=1.0,
            sample_size=3,
            computed_at=newer_time,
        )
    )

    older_batch = OpeningScoreBatch(
        user_id=123,
        player_color="black",
        generation=1,
        computed_at=older_time,
    )
    db_session.add(older_batch)
    db_session.flush()
    db_session.add(
        UserOpeningScore(
            batch_id=older_batch.id,
            user_id=123,
            player_color="black",
            opening_key=KNIGHT_OPENING_FEN,
            opening_name="Older Snapshot",
            opening_family="Open Games",
            opening_score=10.0,
            confidence=20.0,
            coverage=30.0,
            weighted_depth=1.0,
            sample_size=1,
            computed_at=older_time,
        )
    )
    db_session.commit()

    batch, rows = list_cached_opening_scores(db_session, 123, "black")

    assert batch is not None
    assert batch.id == newer_batch.id
    assert {row.opening_name for row in rows} == {"Newer Snapshot"}


def test_latest_batch_prefers_higher_generation_when_timestamps_match(db_session):
    same_time = datetime.now(timezone.utc)

    db_session.add_all(
        [
            OpeningScoreCursor(user_id=123, player_color="black", latest_generation=2),
        ]
    )
    db_session.commit()

    newer_batch = OpeningScoreBatch(
        user_id=123,
        player_color="black",
        generation=2,
        computed_at=same_time,
    )
    db_session.add(newer_batch)
    db_session.flush()
    db_session.add(
        UserOpeningScore(
            batch_id=newer_batch.id,
            user_id=123,
            player_color="black",
            opening_key=KINGS_PAWN_FEN,
            opening_name="Generation Two",
            opening_family="Open Games",
            opening_score=80.0,
            confidence=50.0,
            coverage=60.0,
            weighted_depth=1.0,
            sample_size=3,
            computed_at=same_time,
        )
    )

    older_batch = OpeningScoreBatch(
        user_id=123,
        player_color="black",
        generation=1,
        computed_at=same_time,
    )
    db_session.add(older_batch)
    db_session.flush()
    db_session.add(
        UserOpeningScore(
            batch_id=older_batch.id,
            user_id=123,
            player_color="black",
            opening_key=KNIGHT_OPENING_FEN,
            opening_name="Generation One",
            opening_family="Open Games",
            opening_score=10.0,
            confidence=20.0,
            coverage=30.0,
            weighted_depth=1.0,
            sample_size=1,
            computed_at=same_time,
        )
    )
    db_session.commit()

    batch, rows = list_cached_opening_scores(db_session, 123, "black")

    assert batch is not None
    assert batch.generation == 2
    assert {row.opening_name for row in rows} == {"Generation Two"}


def test_ensure_opening_scores_bootstraps_batch_for_historical_evidence(db_session):
    _seed_black_opening_session(db_session)

    batch, rows = ensure_opening_scores(db_session, 123, "black")

    assert batch is not None
    assert rows
    assert get_latest_opening_score_batch(db_session, 123, "black") is not None


def test_ensure_opening_scores_returns_none_for_true_no_evidence(db_session):
    batch, rows = ensure_opening_scores(db_session, 987, "white")

    assert batch is None
    assert rows == []


def test_recompute_opening_scores_creates_empty_batch_when_no_roots_score(db_session):
    batch = recompute_opening_scores(db_session, 555, "white")
    read_batch, rows = list_cached_opening_scores(db_session, 555, "white")

    assert batch is not None
    assert read_batch is not None
    assert read_batch.id == batch.id
    assert rows == []


def test_backfill_candidate_discovery_finds_historical_pairs(db_session):
    session = _seed_black_opening_session(db_session, user_id=123)

    ghost_position = Position(
        user_id=234,
        fen_hash="ghost-white",
        fen_raw=START_FULL,
        active_color="white",
    )
    db_session.add(ghost_position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=234,
            position_id=ghost_position.id,
            bad_move_san="Qh5",
            best_move_san="Nf3",
            eval_loss_cp=120,
        )
    )

    session_position = Position(
        user_id=123,
        fen_hash="session-black",
        fen_raw=KNIGHT_OPENING_FULL,
        active_color="black",
    )
    db_session.add(session_position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=123,
            position_id=session_position.id,
            bad_move_san="Qh5",
            best_move_san="Nc6",
            eval_loss_cp=80,
            source_session_id=session.id,
        )
    )
    db_session.commit()

    pairs = list_opening_score_candidate_pairs(db_session)

    assert pairs == [(123, "black"), (234, "white")]


def test_session_upload_refreshes_relevant_opening_snapshot(
    client,
    auth_headers,
    create_game_session,
    db_session,
):
    session_id = create_game_session(user_id=123, player_color="black")

    response = client.post(
        f"/api/session/{session_id}/moves",
        json={
            "moves": [
                {
                    "move_number": 1,
                    "color": "white",
                    "move_san": "e4",
                    "fen_before": START_FULL,
                    "fen_after": KINGS_PAWN_FULL,
                    "eval_delta": 0,
                },
                {
                    "move_number": 1,
                    "color": "black",
                    "move_san": "e5",
                    "fen_before": KINGS_PAWN_FULL,
                    "fen_after": OPEN_GAME_FULL,
                    "eval_delta": 0,
                },
                {
                    "move_number": 2,
                    "color": "white",
                    "move_san": "Nf3",
                    "fen_before": OPEN_GAME_FULL,
                    "fen_after": KNIGHT_OPENING_FULL,
                    "eval_delta": 0,
                },
                {
                    "move_number": 2,
                    "color": "black",
                    "move_san": "Nc6",
                    "fen_before": KNIGHT_OPENING_FULL,
                    "fen_after": TWO_KNIGHTS_FULL,
                    "eval_delta": 0,
                },
            ]
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    db_session.expire_all()
    batch, rows = list_cached_opening_scores(db_session, 123, "black")
    assert batch is not None
    assert {row.opening_key for row in rows} == {KINGS_PAWN_FEN, KNIGHT_OPENING_FEN}


def test_srs_review_refreshes_relevant_opening_snapshot(
    client,
    auth_headers,
    create_game_session,
    db_session,
):
    session_id = create_game_session(user_id=123, player_color="black")
    position = Position(
        user_id=123,
        fen_hash="review-black",
        fen_raw=KNIGHT_OPENING_FULL,
        active_color="black",
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=123,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nc6",
        eval_loss_cp=120,
        source_session_id=uuid.UUID(session_id),
    )
    db_session.add(blunder)
    db_session.commit()

    response = client.post(
        "/api/srs/review",
        json={
            "session_id": session_id,
            "blunder_id": blunder.id,
            "passed": True,
            "user_move": "Nc6",
            "eval_delta": 0,
        },
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    db_session.expire_all()
    batch, rows = list_cached_opening_scores(db_session, 123, "black")
    assert batch is not None
    assert {row.opening_key for row in rows} == {KNIGHT_OPENING_FEN}
