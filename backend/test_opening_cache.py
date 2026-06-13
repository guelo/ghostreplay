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
    OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL,
    ensure_opening_scores,
    get_latest_opening_score_batch,
    list_cached_opening_scores,
    list_opening_score_candidate_pairs,
    opening_score_inputs_fingerprint,
    prune_old_opening_score_batches,
    recompute_opening_scores,
    recompute_opening_scores_if_needed,
)
from app.opening_graph import get_opening_graph
from app.opening_roots import get_opening_roots
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
KINGS_PAWN_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
OPEN_GAME_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
KNIGHT_OPENING_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -"
TWO_KNIGHTS_FEN = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"

# Synthetic whole-repertoire hero row key (normalized initial position).
SYNTHETIC_INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"

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
    assert {row.opening_key for row in rows} == {
        KINGS_PAWN_FEN,
        KNIGHT_OPENING_FEN,
        SYNTHETIC_INITIAL_FEN,
    }
    assert all(row.batch_id == batch.id for row in rows)
    assert all(row.user_id == 123 for row in rows)
    assert all(row.player_color == "black" for row in rows)
    assert all(row.computed_at == batch.computed_at for row in rows)


def test_recompute_releases_db_transaction_before_scoring(db_session):
    _seed_black_opening_session(db_session)
    observed_transactions: list[bool] = []

    def fake_build_cached_scores(*args, **kwargs):
        observed_transactions.append(db_session.in_transaction())
        return []

    with patch("app.opening_cache._build_cached_scores", side_effect=fake_build_cached_scores):
        recompute_opening_scores(db_session, 123, "black")

    assert observed_transactions == [False]


def test_recompute_persists_branch_summaries(db_session):
    # Branch summaries are now persisted from the single shared calculation (2b),
    # so the drill-down endpoint reads them straight from cached rows.
    _seed_black_opening_session(db_session)

    recompute_opening_scores(db_session, 123, "black")
    _, rows = list_cached_opening_scores(db_session, 123, "black")

    assert rows
    by_key = {row.opening_key: row for row in rows}
    # The Kings Pawn root has a scored child branch, so its strongest/weakest
    # branch keys are persisted from the shared calc.
    kings_pawn = by_key[KINGS_PAWN_FEN]
    assert kings_pawn.strongest_branch_key is not None
    assert kings_pawn.weakest_branch_key is not None


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


def test_backfill_candidate_discovery_applies_optional_filters(db_session):
    _seed_black_opening_session(db_session, user_id=123)

    white_session = _create_session_row(db_session, user_id=123, player_color="white")
    db_session.add(
        SessionMove(
            session_id=white_session.id,
            move_number=1,
            color="white",
            move_san="e4",
            fen_before=START_FULL,
            fen_after=KINGS_PAWN_FULL,
            eval_delta=0,
        )
    )

    ghost_position = Position(
        user_id=234,
        fen_hash="ghost-white-filter",
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
    db_session.commit()

    assert list_opening_score_candidate_pairs(db_session, user_id=123) == [
        (123, "black"),
        (123, "white"),
    ]
    assert list_opening_score_candidate_pairs(db_session, player_color="white") == [
        (123, "white"),
        (234, "white"),
    ]
    assert list_opening_score_candidate_pairs(
        db_session,
        user_id=123,
        player_color="white",
        limit=1,
    ) == [(123, "white")]


def test_session_upload_refreshes_relevant_opening_snapshot(
    client,
    auth_headers,
    create_game_session,
    db_session,
    _no_op_recompute_scheduler,
):
    session_stub, _ = _no_op_recompute_scheduler
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
    # The endpoint enqueues a coalesced recompute rather than running it inline;
    # drive that recompute directly to assert the snapshot it would produce.
    session_stub.assert_called_once_with(123, "black")
    recompute_opening_scores_if_needed(db_session, 123, "black")
    db_session.commit()
    db_session.expire_all()
    batch, rows = list_cached_opening_scores(db_session, 123, "black")
    assert batch is not None
    assert {row.opening_key for row in rows} == {
        KINGS_PAWN_FEN,
        KNIGHT_OPENING_FEN,
        SYNTHETIC_INITIAL_FEN,
    }


def test_srs_review_refreshes_relevant_opening_snapshot(
    client,
    auth_headers,
    create_game_session,
    db_session,
    _no_op_recompute_scheduler,
):
    _, srs_stub = _no_op_recompute_scheduler
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
    srs_stub.assert_called_once_with(123, "black")
    recompute_opening_scores_if_needed(db_session, 123, "black")
    db_session.commit()
    db_session.expire_all()
    batch, rows = list_cached_opening_scores(db_session, 123, "black")
    assert batch is not None
    assert rows == []


def _count_batches(db_session, user_id: int, player_color: str) -> int:
    return (
        db_session.query(OpeningScoreBatch)
        .filter(
            OpeningScoreBatch.user_id == user_id,
            OpeningScoreBatch.player_color == player_color,
        )
        .count()
    )


def test_repeated_recompute_retains_only_latest_batches(db_session):
    _seed_black_opening_session(db_session)

    for _ in range(5):
        recompute_opening_scores(db_session, 123, "black")

    batch, rows = list_cached_opening_scores(db_session, 123, "black")
    remaining = (
        db_session.query(OpeningScoreBatch)
        .filter(OpeningScoreBatch.user_id == 123, OpeningScoreBatch.player_color == "black")
        .order_by(OpeningScoreBatch.generation.desc())
        .all()
    )

    # keep=2 retention: only the two newest generations survive.
    assert len(remaining) == 2
    assert batch.id == remaining[0].id
    assert {row.opening_key for row in rows} == {
        KINGS_PAWN_FEN,
        KNIGHT_OPENING_FEN,
        SYNTHETIC_INITIAL_FEN,
    }
    assert all(row.batch_id == batch.id for row in rows)

    # Pruned batches' snapshot rows are gone via ON DELETE CASCADE.
    kept_ids = {b.id for b in remaining}
    orphan_scores = (
        db_session.query(UserOpeningScore)
        .filter(UserOpeningScore.batch_id.notin_(kept_ids))
        .count()
    )
    assert orphan_scores == 0


def test_pruning_scoped_per_user_and_color(db_session):
    _seed_black_opening_session(db_session, user_id=123)
    _seed_black_opening_session(db_session, user_id=456)

    for _ in range(4):
        recompute_opening_scores(db_session, 123, "black")
    other_batch = recompute_opening_scores(db_session, 456, "black")

    assert _count_batches(db_session, 123, "black") == 2
    # The unrelated user is untouched by pruning of user 123.
    assert _count_batches(db_session, 456, "black") == 1
    assert get_latest_opening_score_batch(db_session, 456, "black").id == other_batch.id


def test_prune_helper_recovers_from_failure_without_poisoning_session(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    with patch.object(db_session, "commit", side_effect=RuntimeError("boom")):
        deleted = prune_old_opening_score_batches(db_session, 123, "black", keep=0)

    assert deleted == 0
    # Session is usable again (rolled back, not left in a failed transaction).
    assert _count_batches(db_session, 123, "black") == 1


def test_if_needed_reuses_batch_when_inputs_unchanged(db_session):
    _seed_black_opening_session(db_session)

    first = recompute_opening_scores_if_needed(db_session, 123, "black")
    second = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.generation == first.generation
    assert _count_batches(db_session, 123, "black") == 1


def test_if_needed_recomputes_when_evidence_mutated_in_place(db_session):
    session = _seed_black_opening_session(db_session)

    first = recompute_opening_scores_if_needed(db_session, 123, "black")

    # In-place upsert of a move's eval_delta (no updated_at bump) flips a pass to a
    # fail, changing the consumed-evidence content fingerprint.
    move = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session.id, SessionMove.color == "black")
        .first()
    )
    move.eval_delta = 500
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert second is not None
    assert second.id != first.id
    assert second.generation > first.generation
    # Old batch pruned (keep=2 leaves both here; assert latest read is the new one).
    assert get_latest_opening_score_batch(db_session, 123, "black").id == second.id


def test_if_needed_reuses_synthetic_only_batch(db_session):
    # Evidence exists but maps to no scored named roots → batch carries only the
    # synthetic whole-repertoire hero row. The reuse path must not re-append it.
    session = _create_session_row(db_session, user_id=777, player_color="white")
    db_session.add(
        SessionMove(
            session_id=session.id,
            move_number=1,
            color="white",
            move_san="e4",
            fen_before=START_FULL,
            fen_after=KINGS_PAWN_FULL,
            eval_delta=0,
        )
    )
    db_session.commit()

    first = recompute_opening_scores_if_needed(db_session, 777, "white")
    _, first_rows = list_cached_opening_scores(db_session, 777, "white")
    assert first is not None
    assert {row.opening_key for row in first_rows} == {SYNTHETIC_INITIAL_FEN}

    second = recompute_opening_scores_if_needed(db_session, 777, "white")

    assert second is not None
    assert second.id == first.id
    assert _count_batches(db_session, 777, "white") == 1


def test_if_needed_recomputes_when_batch_stale_for_decay(db_session):
    _seed_black_opening_session(db_session)

    first = recompute_opening_scores_if_needed(db_session, 123, "black")

    # Age the batch past the decay interval; fingerprint is unchanged.
    stale_at = datetime.now(timezone.utc) - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL - timedelta(hours=1)
    first.computed_at = stale_at
    db_session.commit()

    second = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert second is not None
    assert second.id != first.id
    assert second.generation > first.generation
    # Freshly recomputed batch carries a current computed_at (not the stale one).
    computed_at = second.computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    assert computed_at > stale_at + OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL


@pytest.mark.parametrize(
    "const",
    ["SCORE_MODEL_VERSION", "DIVIDER_VERSION", "QUALITY_VERSION", "TAU_WC", "TAU_CP"],
)
def test_inputs_fingerprint_changes_with_model_curve_versions(monkeypatch, const):
    graph = get_opening_graph()
    roots = get_opening_roots()
    baseline = opening_score_inputs_fingerprint(graph, roots)

    current = getattr(__import__("app.opening_cache", fromlist=[const]), const)
    bumped = current + 1.0 if isinstance(current, float) else f"{current}-bumped"
    monkeypatch.setattr(f"app.opening_cache.{const}", bumped)

    assert opening_score_inputs_fingerprint(graph, roots) != baseline


def test_model_version_bump_invalidates_existing_batch(db_session, monkeypatch):
    _seed_black_opening_session(db_session)
    first = recompute_opening_scores_if_needed(db_session, 123, "black")
    assert first is not None

    # A model-version bump drifts the registry fingerprint; the next if_needed
    # read recomputes a fresh generation, leaving generation/pruning atomic.
    monkeypatch.setattr("app.opening_cache.SCORE_MODEL_VERSION", "sm-v2-1-bumped")
    second = recompute_opening_scores_if_needed(db_session, 123, "black")

    assert second is not None
    assert second.id != first.id
    assert second.generation > first.generation
    assert _count_batches(db_session, 123, "black") <= 2
