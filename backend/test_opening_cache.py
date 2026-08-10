from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import event

from conftest import engine as test_engine
from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.fen import active_color, normalize_fen
from app.models import (
    AnalysisCache,
    Blunder,
    BlunderReview,
    GameSession,
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
    OpeningScoreCursor,
    Position,
    PositionAnalysisRow,
    SessionMove,
    UserOpeningScore,
)
import app.opening_cache as oc
from app.opening_cache import (
    OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL,
    ensure_opening_scores,
    ensure_tree_cache,
    get_latest_opening_score_batch,
    list_cached_opening_scores,
    list_position_scores,
    OBSERVED_EDGE_PARENT_CHUNK_SIZE,
    load_cached_rows,
    load_cached_rows_nonblocking,
    lookup_observed_edges_for_parent,
    lookup_observed_edges_for_parents,
    lookup_position_scores,
    observed_edge_parent_chunk_count,
    lookup_position_scores_for_batch,
    list_opening_score_candidate_pairs,
    opening_score_inputs_fingerprint,
    opening_score_raw_inputs_fingerprint,
    proven_fresh_opening_scores,
    prune_old_opening_score_batches,
    recompute_opening_scores,
    recompute_opening_scores_if_needed,
    OpeningScoreRecomputeResult,
    RecomputeDisposition,
    TREE_BOOTSTRAP_TIMEOUT,
)
from app.opening_score_scheduler import OpeningScoreTrigger
from app.game_phase import DIVIDER_VERSION
from app.opening_evidence import (
    FRESHNESS_CONTRACT_VERSION,
    OPENING_EVIDENCE_INPUTS_VERSION,
)
from app.opening_evidence import overlay_evidence as _real_overlay_evidence
from app.opening_graph import get_opening_graph
from app.opening_quality import QUALITY_VERSION, TAU_CP, TAU_WC
from app.opening_rootcalc import RootCalcConfig, root_calc_config_fingerprint
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


def _create_session_row(
    db_session, *, user_id: int, player_color: str, status: str = "ended"
) -> GameSession:
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        status=status,
        result="win" if status == "ended" else None,
        engine_elo=1500,
        player_color=player_color,
    )
    db_session.add(session)
    db_session.commit()
    return session


def _seed_black_opening_session(
    db_session, *, user_id: int = 123, status: str = "ended"
) -> GameSession:
    session = _create_session_row(
        db_session, user_id=user_id, player_color="black", status=status
    )
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


def _rebuilt_batch(db_session, user_id: int, player_color: str, *, reason: str):
    """Run the recompute gate, assert it REBUILT for ``reason``, return the batch.

    ``recompute_opening_scores_if_needed`` returns an explicit
    ``OpeningScoreRecomputeResult``. Asserting the disposition is what proves the
    decision — batch identity/generation only corroborates it — and taking ``.batch``
    keeps every later assertion on the SQLAlchemy model rather than the wrapper.
    """
    result = recompute_opening_scores_if_needed(db_session, user_id, player_color)
    assert result.disposition is RecomputeDisposition.REBUILT
    assert result.reason == reason
    assert result.batch is not None
    return result.batch


def _cached_batch(db_session, user_id: int, player_color: str):
    """Run the gate, assert it served the existing batch UNCHANGED, return it."""
    result = recompute_opening_scores_if_needed(db_session, user_id, player_color)
    assert result.disposition is RecomputeDisposition.CACHED
    assert result.reason is None
    assert result.batch is not None
    return result.batch


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


class _MonoClock:
    """A monotonic fake ``_utcnow`` that advances one second per call, so read
    wrappers and the ``computed_at`` sample land on strictly increasing ticks."""

    def __init__(self) -> None:
        self._t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._t = self._t + timedelta(seconds=1)
        return self._t


def _naive(dt: datetime) -> datetime:
    # SQLite round-trips DateTime(timezone=True) as a naive value; strip tz on both
    # sides of the comparison so an aware/naive TypeError can't mask the assertion.
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _assert_computed_at_after_reads(
    db_session, recompute_call, *, operational_capture: bool
):
    # g-mxeo invariant: computed_at is an UPPER BOUND on the evidence reads (sampled
    # AFTER the freshness-snapshot + overlay reads). Each read wrapper advances the
    # shared clock and records its post-read tick; the writer's _utcnow() sample must
    # land on a strictly later tick than both. (Old lower-bound behaviour — sampling
    # computed_at before the reads — would fail this: the reads would tick past it.)
    clock = _MonoClock()
    recorded: dict[str, datetime] = {}
    real_snapshot = oc.capture_freshness_snapshot
    real_overlay = oc.overlay_evidence
    real_shared_snapshot = oc.shared_scope_snapshot

    def snapshot_wrap(*args, **kwargs):
        result = real_snapshot(*args, **kwargs)
        recorded["fp"] = clock()
        return result

    def overlay_wrap(*args, **kwargs):
        result = real_overlay(*args, **kwargs)
        recorded["overlay"] = clock()
        return result

    def shared_snapshot_wrap(*args, **kwargs):
        result = real_shared_snapshot(*args, **kwargs)
        recorded["shared"] = clock()
        return result

    with (
        patch("app.opening_cache._utcnow", clock),
        patch("app.opening_cache.capture_freshness_snapshot", snapshot_wrap),
        patch("app.opening_cache.overlay_evidence", overlay_wrap),
        patch("app.opening_cache.shared_scope_snapshot", shared_snapshot_wrap),
    ):
        batch = recompute_call()

    assert batch is not None
    assert "overlay" in recorded
    if operational_capture:
        assert "shared" in recorded
        assert "fp" not in recorded
    else:
        assert "fp" in recorded
    latest_read = max(recorded.values())
    assert _naive(batch.computed_at) >= _naive(latest_read)


def test_computed_at_is_evidence_read_upper_bound_direct(db_session):
    _seed_black_opening_session(db_session)
    _assert_computed_at_after_reads(
        db_session,
        lambda: recompute_opening_scores(db_session, 123, "black"),
        operational_capture=False,
    )


def test_computed_at_is_evidence_read_upper_bound_if_needed(db_session):
    _seed_black_opening_session(db_session)
    _assert_computed_at_after_reads(
        db_session,
        lambda: _rebuilt_batch(db_session, 123, "black", reason="cache_miss"),
        operational_capture=True,
    )


def test_recompute_releases_db_transaction_before_scoring(db_session):
    _seed_black_opening_session(db_session)
    observed_transactions: list[bool] = []

    def fake_build_cached_scores(*args, **kwargs):
        observed_transactions.append(db_session.in_transaction())
        return [], []

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
    # End the session first: an in-progress session's moves are not evidence.
    session_stub.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.SESSION_EVIDENCE
    )
    game_session = db_session.get(GameSession, uuid.UUID(session_id))
    game_session.status = "ended"
    game_session.ended_at = datetime.now(timezone.utc)
    db_session.commit()
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
    srs_stub.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.SRS_REVIEW
    )
    # End the originating session: a blunder sourced from an in-progress
    # session is not evidence until that session terminates.
    game_session = db_session.get(GameSession, uuid.UUID(session_id))
    game_session.status = "ended"
    game_session.ended_at = datetime.now(timezone.utc)
    db_session.commit()
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

    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    second = _cached_batch(db_session, 123, "black")

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert second.generation == first.generation
    assert _count_batches(db_session, 123, "black") == 1


def test_if_needed_recomputes_when_evidence_mutated_in_place(db_session):
    session = _seed_black_opening_session(db_session)

    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    # In-place upsert of a move's eval_delta (no updated_at bump) flips a pass to a
    # fail, changing the consumed-evidence content. Production reaches this only
    # through upsert_session_moves, whose choke-point bumps the per-user evidence
    # counter for an eligible session (g-jact) — mirror that here; a direct ORM
    # write without the bump is the documented out-of-band-writer caveat.
    move = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session.id, SessionMove.color == "black")
        .first()
    )
    move.eval_delta = 500
    oc.bump_evidence_seq(db_session, 123, "black")
    db_session.commit()

    second = _rebuilt_batch(db_session, 123, "black", reason="evidence_change")

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

    first = _rebuilt_batch(db_session, 777, "white", reason="cache_miss")
    _, first_rows = list_cached_opening_scores(db_session, 777, "white")
    assert first is not None
    assert {row.opening_key for row in first_rows} == {SYNTHETIC_INITIAL_FEN}

    second = _cached_batch(db_session, 777, "white")

    assert second is not None
    assert second.id == first.id
    assert _count_batches(db_session, 777, "white") == 1


def test_if_needed_recomputes_when_batch_stale_for_decay(db_session):
    _seed_black_opening_session(db_session)

    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    # Age the batch past the decay interval; fingerprint is unchanged.
    stale_at = datetime.now(timezone.utc) - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL - timedelta(hours=1)
    first.computed_at = stale_at
    db_session.commit()

    second = _rebuilt_batch(db_session, 123, "black", reason="decay_staleness")

    assert second is not None
    assert second.id != first.id
    assert second.generation > first.generation
    # Freshly recomputed batch carries a current computed_at (not the stale one).
    computed_at = second.computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    assert computed_at > stale_at + OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL


# ---------------------------------------------------------------------------
# proven_fresh_opening_scores — non-blocking freshness verdict (g-fix-start-latency)
# ---------------------------------------------------------------------------

def test_proven_fresh_true_for_freshly_recomputed_batch(db_session):
    # A batch just written by the gate must be reported provably fresh.
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    batch, rows, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")

    assert batch is not None
    assert rows
    assert is_fresh is True


def test_proven_fresh_false_when_evidence_mutated(db_session):
    # In-place evidence change -> the cached batch is stale even with NO scheduler
    # work pending; the verdict must catch it. The bump mirrors the production
    # upsert_session_moves choke-point (g-jact); a direct ORM write without it is
    # the documented out-of-band-writer caveat.
    session = _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    move = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session.id, SessionMove.color == "black")
        .first()
    )
    move.eval_delta = 500
    oc.bump_evidence_seq(db_session, 123, "black")
    db_session.commit()

    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is False


def test_proven_fresh_false_on_registry_drift(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    batch = get_latest_opening_score_batch(db_session, 123, "black")
    batch.registry_fingerprint = "stale-registry"
    db_session.commit()

    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is False


def test_proven_fresh_allows_omitted_optional_raw_fingerprint(db_session):
    # The full raw fingerprint is an optional audit/release-capture field. The
    # partitioned signal is the serving proof and remains sufficient on its own.
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    batch = get_latest_opening_score_batch(db_session, 123, "black")
    batch.inputs_fingerprint = None
    db_session.commit()

    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is True

    # A genuinely pre-signal/partial batch is still fail-closed.
    batch.evidence_seq = None
    db_session.commit()
    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is False


def test_proven_fresh_false_for_stale_branch_keys(db_session):
    # Legacy rows carrying a branch NAME but no branch KEY are stale even when both
    # fingerprints match — mirrors the gate's _batch_has_stale_branch_keys guard.
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    _, rows = list_cached_opening_scores(db_session, 123, "black")
    rows[0].strongest_branch_name = "Some Branch"
    rows[0].strongest_branch_key = None
    db_session.commit()

    _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert is_fresh is False


def test_proven_fresh_none_when_no_batch(db_session):
    batch, rows, is_fresh = proven_fresh_opening_scores(db_session, 999, "white")
    assert batch is None
    assert rows == []
    assert is_fresh is False


def test_proven_fresh_never_touches_scheduler(db_session):
    # The verdict must be readable on the request hot path with zero scheduler
    # interaction (no refresh_now / no request_recompute, blocking or otherwise).
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    with (
        patch("app.opening_score_scheduler.refresh_now") as mock_refresh,
        patch("app.opening_score_scheduler.request_recompute") as mock_request,
    ):
        _, _, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")

    assert is_fresh is True
    mock_refresh.assert_not_called()
    mock_request.assert_not_called()


@pytest.mark.parametrize(
    "const",
    ["SCORE_MODEL_VERSION", "DIVIDER_VERSION", "QUALITY_VERSION", "TAU_WC", "TAU_CP",
     "OPENING_SCORE_CACHE_SCHEMA_VERSION"],
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
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None

    # A model-version bump drifts the registry fingerprint; the next if_needed
    # read recomputes a fresh generation, leaving generation/pruning atomic.
    monkeypatch.setattr("app.opening_cache.SCORE_MODEL_VERSION", "sm-v2-1-bumped")
    second = _rebuilt_batch(db_session, 123, "black", reason="registry_drift")

    assert second is not None
    assert second.id != first.id
    assert second.generation > first.generation
    assert _count_batches(db_session, 123, "black") <= 2


def test_sm_v2_3_config_and_model_version_recomputes_once(db_session):
    """A batch stamped under sm-v2-3's config/version drifts once, then
    the rebuilt batch serves the fast path."""
    _seed_black_opening_session(db_session)
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None

    old_config_fp = root_calc_config_fingerprint(
        RootCalcConfig(
            lcb_z=1.0,
            coverage_fold="gate",
            coverage_live_threshold=1,
            report_fold_p=0.0,
            report_fold_scope="all",
            report_self_term="keep",
        )
    )
    current_config_fp = root_calc_config_fingerprint()
    assert old_config_fp != current_config_fp
    assert oc.SCORE_MODEL_VERSION == "sm-v2-4"
    first.registry_fingerprint = first.registry_fingerprint.replace(
        current_config_fp, old_config_fp
    ).replace(oc.SCORE_MODEL_VERSION, "sm-v2-3")
    db_session.commit()

    second = _rebuilt_batch(db_session, 123, "black", reason="registry_drift")
    third = _cached_batch(db_session, 123, "black")

    assert second is not None and third is not None
    assert second.id != first.id
    assert third.id == second.id
    assert second.registry_fingerprint == opening_score_inputs_fingerprint(
        _make_graph(), _make_roots()
    )


def test_cache_schema_version_bump_invalidates_edgeless_batch(db_session, monkeypatch):
    """A read-model schema-version bump (folded into the registry fingerprint) drifts
    a pre-existing batch and forces exactly one recompute on the next gated read, so a
    batch built before the edge read model existed self-heals (materializing edges)."""
    _seed_black_opening_session(db_session)
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None
    assert (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == first.id)
        .count()
        > 0
    )

    monkeypatch.setattr(
        "app.opening_cache.OPENING_SCORE_CACHE_SCHEMA_VERSION", "edges-v2"
    )
    second = _rebuilt_batch(db_session, 123, "black", reason="registry_drift")

    assert second is not None
    assert second.id != first.id  # registry drift forced a fresh batch
    assert second.generation > first.generation
    # The fresh batch carries its own edge rows.
    assert (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == second.id)
        .count()
        > 0
    )


# ---------------------------------------------------------------------------
# Report-fold config compatibility at the cache boundary.
#
# The sm-v2-4 default embeds its active user-scope fold fingerprint. A batch
# stamped under any non-default report-stage shape is registry drift and
# recomputes once.
# ---------------------------------------------------------------------------

# root_calc_config_fingerprint(RootCalcConfig()) — the production default config fp,
# embedded verbatim in the registry fingerprint.
GOLDEN = "301c3130cad49253aa87df8f68f578ab7a320c5bc3401170ff5498d7986c1090"


def test_registry_fingerprint_composes_golden_config():
    # Pin the exact sm-v2-4 registry composition, with GOLDEN as the config-fp segment.
    graph = _make_graph()
    roots = _make_roots()
    assert root_calc_config_fingerprint() == GOLDEN
    expected = (
        f"{graph.fingerprint}:{roots.fingerprint}:{GOLDEN}"
        f":{oc.SCORE_MODEL_VERSION}:{DIVIDER_VERSION}"
        f":{QUALITY_VERSION}:{TAU_WC!r}:{TAU_CP!r}"
        f":{oc.OPENING_SCORE_CACHE_SCHEMA_VERSION}"
        f":{OPENING_EVIDENCE_INPUTS_VERSION}:{FRESHNESS_CONTRACT_VERSION}"
    )
    assert opening_score_inputs_fingerprint(graph, roots) == expected
    # The GOLDEN config fp is the third colon-delimited segment, not buried elsewhere.
    assert opening_score_inputs_fingerprint(graph, roots).split(":")[2] == GOLDEN


def test_golden_stamped_expired_batch_stays_on_fast_path(db_session):
    # Even aged well past the decay interval ("expired"), a current-config batch's
    # cheap freshness predicate — which ignores wall-clock decay — proves it fresh,
    # so the fast path serves it without a rebuild.
    _seed_black_opening_session(db_session)
    batch = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert batch is not None
    assert GOLDEN in batch.registry_fingerprint

    batch.computed_at = (
        datetime.now(timezone.utc)
        - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL
        - timedelta(days=3)
    )
    db_session.commit()

    _, rows, is_fresh = proven_fresh_opening_scores(db_session, 123, "black")
    assert rows
    assert is_fresh is True


@pytest.mark.parametrize(
    "active_config",
    [
        RootCalcConfig(report_fold_p=0.5, report_fold_scope="all"),
        RootCalcConfig(report_self_term="drop_user"),
    ],
    ids=["all_scope", "drop_user"],
)
def test_nondefault_report_axis_stamped_batch_recomputes_once(db_session, active_config):
    # A batch stamped under a non-default report-fold shape carries a non-GOLDEN config fp
    # in its registry fingerprint, which the default runtime (GOLDEN) cannot match →
    # registry drift → exactly ONE recompute, after which the rebuilt GOLDEN-stamped
    # batch serves the fast path. (The batch is also aged past decay; registry drift
    # is checked first, so it — not decay — drives the single recompute.)
    _seed_black_opening_session(db_session)
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None
    assert GOLDEN in first.registry_fingerprint

    active_fp = root_calc_config_fingerprint(active_config)
    assert active_fp != GOLDEN
    first.registry_fingerprint = first.registry_fingerprint.replace(GOLDEN, active_fp)
    first.computed_at = (
        datetime.now(timezone.utc)
        - OPENING_SCORE_DECAY_RECOMPUTE_INTERVAL
        - timedelta(days=3)
    )
    db_session.commit()

    second = _rebuilt_batch(db_session, 123, "black", reason="registry_drift")
    third = _cached_batch(db_session, 123, "black")

    assert second is not None and third is not None
    assert second.id != first.id  # active-axis stamp drifted → rebuilt
    assert third.id == second.id  # rebuilt batch is GOLDEN-stamped → reused, no 2nd
    assert second.registry_fingerprint == opening_score_inputs_fingerprint(
        _make_graph(), _make_roots()
    )
    assert GOLDEN in second.registry_fingerprint


# ---------------------------------------------------------------------------
# Cheap raw-input freshness digest (g-6zhp): the gate must flip on every change
# the overlay would see, stay stable otherwise, and let the read path skip the
# expensive overlay build entirely on the cache-hit fast path.
# ---------------------------------------------------------------------------


def _raw_fp(db_session, user_id: int = 123, player_color: str = "black") -> str:
    return opening_score_raw_inputs_fingerprint(db_session, user_id, player_color)


def _black_move(db_session, session):
    return (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == session.id, SessionMove.color == "black")
        .first()
    )


def test_raw_fp_deterministic_across_repeated_calls(db_session):
    _seed_black_opening_session(db_session)
    assert _raw_fp(db_session) == _raw_fp(db_session)


def test_raw_fp_flips_on_eval_delta_mutation(db_session):
    session = _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    move = _black_move(db_session, session)
    move.eval_delta = 500
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_primary_eval_mutation(db_session):
    session = _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    move = _black_move(db_session, session)
    move.eval_cp = 20
    move.best_move_eval_cp = 30
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_new_session_move(db_session):
    session = _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    db_session.add(
        SessionMove(
            session_id=session.id,
            move_number=3,
            color="white",
            move_san="Bb5",
            fen_before=TWO_KNIGHTS_FULL,
            fen_after="r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
            eval_delta=0,
        )
    )
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_new_ghost_target(db_session):
    session = _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    position = Position(
        user_id=123, fen_hash="gt-black", fen_raw=KNIGHT_OPENING_FULL, active_color="black"
    )
    db_session.add(position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=123,
            position_id=position.id,
            bad_move_san="Qh5",
            best_move_san="Nc6",
            eval_loss_cp=120,
            source_session_id=session.id,
        )
    )
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_new_blunder_review(db_session):
    session = _seed_black_opening_session(db_session)
    position = Position(
        user_id=123, fen_hash="rev-black", fen_raw=KNIGHT_OPENING_FULL, active_color="black"
    )
    db_session.add(position)
    db_session.flush()
    blunder = Blunder(
        user_id=123,
        position_id=position.id,
        bad_move_san="Qh5",
        best_move_san="Nc6",
        eval_loss_cp=120,
        source_session_id=session.id,
    )
    db_session.add(blunder)
    db_session.commit()
    before = _raw_fp(db_session)

    db_session.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=session.id,
            passed=True,
            move_played_san="Nc6",
            eval_delta_cp=0,
        )
    )
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_consulted_analysis_cache_row(db_session):
    # The seeded black moves carry eval_delta but no primary eval, so their
    # fen_before values are analysis_cache fallback candidates. A cache row on one
    # of those fens is consulted by the overlay → must flip the digest.
    _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    db_session.add(
        AnalysisCache(
            fen_before=KINGS_PAWN_FULL,
            move_uci="e7e5",
            move_san="e5",
            played_eval=10,
            best_eval=15,
        )
    )
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_unaffected_by_unrelated_analysis_cache_row(db_session):
    # A cache row for a fen that no null-eval session move references can never be
    # consulted, so it must not flip the digest (no needless rebuild).
    _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    db_session.add(
        AnalysisCache(
            fen_before="8/8/8/8/8/8/8/8 w - - 0 1",
            move_uci="a1a2",
            move_san="Ra2",
            played_eval=10,
            best_eval=15,
        )
    )
    db_session.commit()

    assert _raw_fp(db_session) == before


def _canonical_identity_cols() -> dict:
    profile = get_profile(CANONICAL_PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def test_raw_fp_flips_on_trusted_position_winner(db_session):
    # A trusted position_analysis storage winner at a candidate normalized FEN
    # (g-opening-score-trust): the fallback pairs ITS best_eval with the move row,
    # so flipping the winner's best_eval must flip the digest.
    _seed_black_opening_session(db_session)
    winner = PositionAnalysisRow(
        normalized_fen=normalize_fen(KNIGHT_OPENING_FULL),
        fen=KNIGHT_OPENING_FULL,
        best_move_uci="b8c6",
        best_move_san="Nc6",
        best_line_uci="b8c6 f1b5",
        best_eval=30,
        source="precomputed",
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="position-complete-v1",
        **_canonical_identity_cols(),
    )
    db_session.add(winner)
    db_session.commit()
    before = _raw_fp(db_session)

    winner.best_eval = 250
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_move_trust_column(db_session):
    # A move-trusted canonical analysis_cache row at a candidate fen: flipping a
    # column the move-trust gate reads (classification) must flip the digest, since
    # a move-trust change alters which played eval the fallback consumes.
    _seed_black_opening_session(db_session)
    row = AnalysisCache(
        fen_before=KNIGHT_OPENING_FULL,
        normalized_fen_before=normalize_fen(KNIGHT_OPENING_FULL),
        move_uci="b8c6",
        move_san="Nc6",
        best_move_uci="b8c6",
        best_move_san="Nc6",
        best_line_uci="b8c6 f1b5",
        played_eval=-30,
        best_eval=20,
        eval_delta=50,
        classification="inaccuracy",
        source="precomputed",
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2",
        **_canonical_identity_cols(),
    )
    db_session.add(row)
    db_session.commit()
    before = _raw_fp(db_session)

    row.classification = "blunder"
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_legacy_fallback_normalized_key(db_session):
    # resolve_trusted_positions GROUPS legacy analysis_cache rows by
    # normalized_fen_before. A row whose normalized_fen_before is repaired from one
    # candidate norm to ANOTHER stays inside the digest's IN-set (both norms are
    # candidates) but is reassigned to a different position at runtime, so the
    # digest must flip on that grouping column.
    _seed_black_opening_session(db_session)
    norm_a = normalize_fen(KINGS_PAWN_FULL)
    norm_b = normalize_fen(KNIGHT_OPENING_FULL)
    assert norm_a != norm_b
    # A clock variant of KINGS_PAWN so the row sits only in the legacy POSITION
    # subset (keyed by normalized_fen_before), not the exact-fen move subset.
    clock_variant = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 5 9"
    assert clock_variant not in (KINGS_PAWN_FULL, KNIGHT_OPENING_FULL)
    row = AnalysisCache(
        fen_before=clock_variant,
        normalized_fen_before=norm_a,
        move_uci="e7e5",
        move_san="e5",
        best_move_uci="e7e5",
        best_move_san="e5",
        best_line_uci="e7e5 g1f3",
        played_eval=20,
        best_eval=20,
        eval_delta=0,
        classification="best",
        source="precomputed",
        analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2",
        **_canonical_identity_cols(),
    )
    db_session.add(row)
    db_session.commit()
    before = _raw_fp(db_session)

    # Repair the normalized key to the OTHER candidate norm (still in the IN-set).
    row.normalized_fen_before = norm_b
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_raw_fp_flips_on_evidence_inputs_version_change(db_session, monkeypatch):
    _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    monkeypatch.setattr("app.opening_cache.OPENING_EVIDENCE_INPUTS_VERSION", "raw-v1-bumped")

    assert _raw_fp(db_session) != before


@pytest.mark.parametrize(
    "const",
    ["SCORE_MODEL_VERSION", "DIVIDER_VERSION", "QUALITY_VERSION", "TAU_WC", "TAU_CP"],
)
def test_raw_fp_flips_on_registry_version_change(db_session, monkeypatch, const):
    _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    current = getattr(__import__("app.opening_cache", fromlist=[const]), const)
    bumped = current + 1.0 if isinstance(current, float) else f"{current}-bumped"
    monkeypatch.setattr(f"app.opening_cache.{const}", bumped)

    assert _raw_fp(db_session) != before


def test_raw_fp_ignores_in_progress_session_moves(db_session):
    # g-dmd1 regression: live-play move upserts land in an ACTIVE session and
    # must NOT flip the fingerprint — otherwise every poll-triggered gate check
    # sees evidence_change and pays a full overlay rebuild, looping the worker.
    _seed_black_opening_session(db_session)
    before = _raw_fp(db_session)

    live = _seed_black_opening_session(db_session, status="active")
    assert _raw_fp(db_session) == before

    live.status = "ended"
    live.ended_at = datetime.now(timezone.utc)
    db_session.commit()

    assert _raw_fp(db_session) != before


def test_active_only_user_has_no_opening_evidence(db_session):
    # The existence gate must agree with the overlay/digest eligibility rule:
    # an in-progress session's moves are not evidence yet, so
    # has_opening_evidence must be False while the overlay builds empty.
    session = _seed_black_opening_session(db_session, status="active")

    assert oc.has_opening_evidence(db_session, 123, "black") is False
    overlay = _real_overlay_evidence(db_session, 123, "black", _make_graph())
    assert overlay.nodes == {}

    session.status = "ended"
    session.ended_at = datetime.now(timezone.utc)
    db_session.commit()

    assert oc.has_opening_evidence(db_session, 123, "black") is True
    overlay = _real_overlay_evidence(db_session, 123, "black", _make_graph())
    assert overlay.nodes != {}


def test_active_only_user_baseline_is_empty_no_evidence(db_session):
    # With the existence gate aligned, a user whose only session is still
    # active gets a valid EMPTY baseline (openings later read as new) — not
    # skipped_cold, which would drop the end-of-first-game delta.
    from app.opening_score_delta import _capture_baseline_json

    _seed_black_opening_session(db_session, status="active")

    json_str, source = _capture_baseline_json(
        db_session, 123, "black",
        skip_when_inflight=False,
    )
    assert source == "empty_no_evidence"
    assert json.loads(json_str) == {
        "schema_version": 1,
        "model_version": oc.SCORE_MODEL_VERSION,
        "root_calc_config_fingerprint": root_calc_config_fingerprint(),
        "scores": {},
    }


def test_manual_blunder_counts_as_evidence_regardless_of_session_status(db_session):
    # Manual blunders (no source session) mirror the gs.id IS NULL branch in
    # the overlay/digest: always eligible.
    position = Position(
        user_id=123, fen_hash="manual-gate", fen_raw=KNIGHT_OPENING_FULL,
        active_color="black",
    )
    db_session.add(position)
    db_session.flush()
    db_session.add(
        Blunder(
            user_id=123,
            position_id=position.id,
            bad_move_san="Qh5",
            best_move_san="Nc6",
            eval_loss_cp=120,
            source_session_id=None,
        )
    )
    db_session.commit()

    assert oc.has_opening_evidence(db_session, 123, "black") is True


def test_pre_bump_batch_recomputes_once_then_serves_fast_path(db_session):
    # A batch stamped under the PREVIOUS OPENING_EVIDENCE_INPUTS_VERSION (raw-v6,
    # the state g-v21l bumped away from) is stale WITHOUT any raw-row mutation, and
    # mismatches exactly once (self-healing recompute); the next unchanged trigger
    # serves the rebuilt batch without recomputing OR rebuilding the overlay. Since
    # g-jact the evidence version rides in the REGISTRY fingerprint, so an older
    # stamp shows up as registry drift.
    _seed_black_opening_session(db_session)

    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None
    assert "raw-v7" in first.registry_fingerprint
    # Seed the pre-bump stamp. No raw row is touched.
    first.registry_fingerprint = first.registry_fingerprint.replace("raw-v7", "raw-v6")
    db_session.commit()

    with patch("app.opening_cache.overlay_evidence", wraps=_real_overlay_evidence) as spy:
        second = _rebuilt_batch(db_session, 123, "black", reason="registry_drift")
    assert second is not None
    assert second.id != first.id  # exactly one recompute
    assert spy.call_count == 1
    assert "raw-v7" in second.registry_fingerprint  # stamped at the new version

    # A second unchanged read serves that same batch through the fast path — no
    # recompute, and no overlay build at all.
    with patch(
        "app.opening_cache.overlay_evidence",
        side_effect=AssertionError("overlay must not be built on the fast path"),
    ):
        third = _cached_batch(db_session, 123, "black")
    assert third is not None
    assert third.id == second.id


def test_if_needed_fast_path_does_not_build_overlay(db_session):
    _seed_black_opening_session(db_session)
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")
    assert first is not None

    # Nothing changed since the first recompute: the second call must serve the
    # cached batch WITHOUT ever building the (expensive) overlay.
    with patch(
        "app.opening_cache.overlay_evidence",
        side_effect=AssertionError("overlay must not be built on the fast path"),
    ):
        second = _cached_batch(db_session, 123, "black")

    assert second is not None
    assert second.id == first.id


def test_if_needed_builds_overlay_on_cache_miss(db_session):
    _seed_black_opening_session(db_session)

    with patch("app.opening_cache.overlay_evidence", wraps=_real_overlay_evidence) as spy:
        batch = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    assert batch is not None
    assert spy.called


def test_if_needed_rebuild_skips_full_raw_snapshot_and_reuses_signal(db_session):
    _seed_black_opening_session(db_session)

    with patch(
        "app.opening_cache.capture_freshness_snapshot",
        side_effect=AssertionError("operational rebuild ran the full raw snapshot"),
    ):
        first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    assert first is not None
    assert first.inputs_fingerprint is None
    assert first.evidence_seq is not None
    assert first.cache_epoch is not None
    assert first.scoped_shared_digest is not None

    second = _cached_batch(db_session, 123, "black")
    assert second is not None
    assert second.id == first.id


@pytest.mark.parametrize("drift", ["counter", "scope_identity"])
def test_if_needed_operational_capture_drift_falls_back_to_full_snapshot(
    db_session, drift, caplog
):
    _seed_black_opening_session(db_session)
    real_shared_snapshot = oc.shared_scope_snapshot
    shared_calls = 0

    def drifting_shared_snapshot(*args, **kwargs):
        nonlocal shared_calls
        shared_calls += 1
        snapshot = real_shared_snapshot(*args, **kwargs)
        if shared_calls != 1:
            return snapshot
        if drift == "counter":
            oc.bump_evidence_seq(db_session, 123, "black")
            db_session.commit()
            return snapshot
        return snapshot.__class__(
            digest=snapshot.digest,
            move_row_ids=(*snapshot.move_row_ids, 999_999),
        )

    with caplog.at_level("INFO", logger="app.opening_cache"):
        with (
            patch(
                "app.opening_cache.shared_scope_snapshot",
                side_effect=drifting_shared_snapshot,
            ),
            patch(
                "app.opening_cache.capture_freshness_snapshot",
                wraps=oc.capture_freshness_snapshot,
            ) as full_snapshot,
            patch(
                "app.opening_cache.overlay_evidence",
                wraps=_real_overlay_evidence,
            ) as overlay,
            patch("app.opening_cache.capture") as analytics,
        ):
            batch = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    assert batch is not None
    assert batch.inputs_fingerprint is not None
    assert full_snapshot.call_count == 1
    assert overlay.call_count == 2
    expected_capture = (
        "fallback_counter" if drift == "counter" else "fallback_identity"
    )
    assert analytics.call_args.args[2]["freshness_capture"] == expected_capture
    assert any(
        f"freshness_capture={expected_capture}" in record.getMessage()
        for record in caplog.records
    )


def test_if_needed_builds_overlay_on_real_change(db_session):
    session = _seed_black_opening_session(db_session)
    first = _rebuilt_batch(db_session, 123, "black", reason="cache_miss")

    move = _black_move(db_session, session)
    move.eval_delta = 500
    # Mirror the production upsert_session_moves choke-point bump (g-jact).
    oc.bump_evidence_seq(db_session, 123, "black")
    db_session.commit()

    with patch("app.opening_cache.overlay_evidence", wraps=_real_overlay_evidence) as spy:
        second = _rebuilt_batch(db_session, 123, "black", reason="evidence_change")

    assert spy.called
    assert second is not None
    assert second.id != first.id


def test_recompute_default_computes_fingerprint_before_overlay(db_session):
    # Race-safety: the stored freshness bundle must reflect inputs at-or-before
    # the scored overlay, never newer. Capturing the snapshot first guarantees
    # that, so a later fast-path can never serve scores older than their stamp.
    _seed_black_opening_session(db_session)
    order: list[str] = []
    real_snapshot = oc.capture_freshness_snapshot
    real_overlay = _real_overlay_evidence

    def spy_snapshot(*args, **kwargs):
        order.append("fingerprint")
        return real_snapshot(*args, **kwargs)

    def spy_overlay(*args, **kwargs):
        order.append("overlay")
        return real_overlay(*args, **kwargs)

    with (
        patch("app.opening_cache.capture_freshness_snapshot", side_effect=spy_snapshot),
        patch("app.opening_cache.overlay_evidence", side_effect=spy_overlay),
    ):
        recompute_opening_scores(db_session, 123, "black")

    assert order == ["fingerprint", "overlay"]


def test_recompute_rejects_overlay_without_fingerprint(db_session):
    # Passing a prebuilt overlay without its matching freshness snapshot is unsafe
    # (the function cannot derive a bundle that is guaranteed not-newer than the
    # overlay) and must be rejected before any generation is reserved.
    _seed_black_opening_session(db_session)
    overlay = _real_overlay_evidence(db_session, 123, "black", _make_graph())

    with pytest.raises(ValueError, match="freshness snapshot is required"):
        recompute_opening_scores(db_session, 123, "black", overlay=overlay)

    assert _count_batches(db_session, 123, "black") == 0


def test_if_needed_builds_overlay_on_registry_drift(db_session, monkeypatch):
    _seed_black_opening_session(db_session)
    recompute_opening_scores_if_needed(db_session, 123, "black")

    monkeypatch.setattr("app.opening_cache.SCORE_MODEL_VERSION", "sm-v2-1-bumped")

    with patch("app.opening_cache.overlay_evidence", wraps=_real_overlay_evidence) as spy:
        recompute_opening_scores_if_needed(db_session, 123, "black")

    assert spy.called


# ---------------------------------------------------------------------------
# load_cached_rows — stale-while-revalidate reader (Approach A)
#
# The scheduler funcs are lazy-imported INSIDE load_cached_rows, so they are
# patched at the source module (app.opening_score_scheduler). The row fetch is
# patched at app.opening_cache.list_cached_opening_scores.
# ---------------------------------------------------------------------------

def test_load_cached_rows_warm_serves_cache_and_schedules_background():
    """Warm (batch present): serve cached rows + background recompute, no block."""
    sentinel_batch = object()
    sentinel_rows = object()
    snapshotted = object()
    with patch(
        "app.opening_cache.list_cached_opening_scores",
        return_value=(sentinel_batch, sentinel_rows),
    ) as list_cached, patch(
        "app.opening_cache._snapshot_cached_rows", return_value=snapshotted
    ) as snapshot, patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows = load_cached_rows("db", 123, "black")

    assert batch is sentinel_batch
    assert rows is snapshotted
    snapshot.assert_called_once_with(sentinel_rows)
    request_recompute.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.CACHED_SCORE_READER_WARM
    )
    refresh_now.assert_not_called()
    list_cached.assert_called_once()


def test_load_cached_rows_cold_blocks_then_serves_computed_batch():
    """Cold (no batch): block on refresh_now once, then re-list and serve."""
    sentinel_batch = object()
    sentinel_rows = object()
    snapshotted = object()
    with patch(
        "app.opening_cache.list_cached_opening_scores",
        side_effect=[(None, []), (sentinel_batch, sentinel_rows)],
    ) as list_cached, patch(
        "app.opening_cache._snapshot_cached_rows", return_value=snapshotted
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows = load_cached_rows("db", 123, "black")

    assert batch is sentinel_batch
    assert rows is snapshotted
    refresh_now.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.CACHED_SCORE_READER_COLD
    )
    request_recompute.assert_not_called()
    assert list_cached.call_count == 2


def test_load_cached_rows_cold_no_evidence_serves_empty():
    """Cold with no evidence: refresh_now once, still no batch, serve empty."""
    with patch(
        "app.opening_cache.list_cached_opening_scores",
        side_effect=[(None, []), (None, [])],
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows = load_cached_rows("db", 123, "black")

    assert batch is None
    assert rows == []
    refresh_now.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.CACHED_SCORE_READER_COLD
    )
    request_recompute.assert_not_called()


def test_ensure_tree_cache_warm_fresh_serves_without_blocking(db_session):
    """Warm-fresh (batch present, registry matches): background revalidate, no block."""
    graph = _make_graph()
    roots = _make_roots()
    batch = OpeningScoreBatch(
        user_id=123, player_color="black", generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        computed_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.commit()

    with patch("app.opening_score_scheduler.refresh_now") as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch_id, computed_at, state = ensure_tree_cache(
            db_session, 123, "black", graph, roots
        )

    request_recompute.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.TREE_READER_WARM
    )
    refresh_now.assert_not_called()
    assert batch_id == batch.id
    # SQLite round-trips datetimes tz-naive; compare wall-clock components.
    assert computed_at.replace(tzinfo=None) == datetime(2026, 6, 15)
    assert state == "warm_fresh"


def test_ensure_tree_cache_legacy_edgeless_batch_blocks_and_bootstraps(db_session):
    """Finding #1: a batch predating edges-v1 (registry mismatch ⇒ no edge rows) must
    BLOCK on refresh_now (not background-revalidate), so the served tree carries its
    observed edges instead of silently rendering book-only. The resolved batch is the
    freshly bootstrapped one, not the stale edgeless one."""
    graph = _make_graph()
    roots = _make_roots()
    current_fp = opening_score_inputs_fingerprint(graph, roots)
    stale = OpeningScoreBatch(
        user_id=123, player_color="black", generation=1,
        registry_fingerprint="pre-edges-v1",
        computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    db_session.add(stale)
    db_session.commit()

    def _bootstrap(user_id, player_color, timeout=30.0, *, source=None):
        # Stand in for the scheduler landing a fresh edges-v1 batch with edge rows.
        fresh = OpeningScoreBatch(
            user_id=user_id, player_color=player_color, generation=2,
            registry_fingerprint=current_fp,
            computed_at=datetime(2026, 6, 15, tzinfo=timezone.utc),
        )
        db_session.add(fresh)
        db_session.commit()
        return True

    with patch(
        "app.opening_score_scheduler.refresh_now", side_effect=_bootstrap
    ) as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch_id, computed_at, state = ensure_tree_cache(
            db_session, 123, "black", graph, roots
        )

    # BLOCKED on the bootstrap, did not background.
    refresh_now.assert_called_once_with(
        123,
        "black",
        timeout=TREE_BOOTSTRAP_TIMEOUT,
        source=OpeningScoreTrigger.TREE_READER_BOOTSTRAP,
    )
    request_recompute.assert_not_called()
    assert state == "bootstrapped"
    assert computed_at.replace(tzinfo=None) == datetime(2026, 6, 15)
    resolved = get_latest_opening_score_batch(db_session, 123, "black")
    assert batch_id == resolved.id
    assert batch_id != stale.id


def test_ensure_tree_cache_no_evidence_returns_book_only(db_session):
    """Cold user with no evidence: short-circuit to (None, None, book_only) WITHOUT
    blocking on refresh_now — a book-only tree is correct and complete, and the
    blocking flush would needlessly queue behind the single scheduler worker (g-k4z2)."""
    graph = _make_graph()
    roots = _make_roots()
    with patch(
        "app.opening_score_scheduler.refresh_now", return_value=True
    ) as refresh_now, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        result = ensure_tree_cache(db_session, 999, "white", graph, roots)

    # No blocking bootstrap and no background trigger for a no-evidence user.
    refresh_now.assert_not_called()
    request_recompute.assert_not_called()
    assert result == (None, None, "book_only")


def test_ensure_tree_cache_bootstrap_timeout_no_batch_logs_warning(db_session, caplog):
    """A bootstrap that times out (refresh_now False) with no batch logs a WARNING and
    reports the degraded state distinctly (not a clean book-only). The user HAS
    evidence (else the no-evidence short-circuit returns book_only before refresh_now)."""
    graph = _make_graph()
    roots = _make_roots()
    with patch(
        "app.opening_cache.has_opening_evidence", return_value=True
    ), patch(
        "app.opening_score_scheduler.refresh_now", return_value=False
    ), patch("app.opening_score_scheduler.request_recompute"):
        with caplog.at_level("WARNING"):
            result = ensure_tree_cache(db_session, 999, "white", graph, roots)

    assert result == (None, None, "bootstrap_timeout")
    assert any("tree_cache_bootstrap_timeout" in r.message for r in caplog.records)


def test_ensure_tree_cache_bootstrap_timeout_serves_stale_batch_distinctly(db_session):
    """Finding: a timed-out bootstrap that leaves a registry-stale (edgeless) batch is
    served for that one request but reported as 'bootstrap_timeout', never as a clean
    'bootstrapped' — the timing log must not claim a successful bootstrap happened."""
    graph = _make_graph()
    roots = _make_roots()
    stale = OpeningScoreBatch(
        user_id=123, player_color="black", generation=1,
        registry_fingerprint="pre-edges-v1",
        computed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    db_session.add(stale)
    db_session.commit()

    # refresh_now times out without landing a fresh batch (recompute still running).
    with patch(
        "app.opening_score_scheduler.refresh_now", return_value=False
    ), patch("app.opening_score_scheduler.request_recompute"):
        batch_id, _computed_at, state = ensure_tree_cache(
            db_session, 123, "black", graph, roots
        )

    assert state == "bootstrap_timeout"
    # The stale batch is still served (degraded read), not dropped to book_only-None.
    assert batch_id == stale.id


# ---------------------------------------------------------------------------
# Direct position-score read model persistence + lookup (g-tree-score-model).
# ---------------------------------------------------------------------------


def test_large_read_models_use_core_executemany_without_returning(db_session):
    """The two large row sets use one id-free Core executemany apiece."""
    _seed_black_opening_session(db_session)
    statements: list[tuple[str, bool]] = []

    def capture_statement(_conn, _cursor, statement, _params, _context, executemany):
        normalized = statement.lower()
        if (
            "insert into opening_position_scores" in normalized
            or "insert into opening_position_edges" in normalized
        ):
            statements.append((normalized, executemany))

    event.listen(test_engine, "before_cursor_execute", capture_statement)
    try:
        batch = recompute_opening_scores(db_session, 123, "black")
    finally:
        event.remove(test_engine, "before_cursor_execute", capture_statement)

    position_statements = [
        item for item in statements if "opening_position_scores" in item[0]
    ]
    edge_statements = [
        item for item in statements if "opening_position_edges" in item[0]
    ]
    assert len(position_statements) == 1
    assert len(edge_statements) == 1
    assert position_statements[0][1] is True
    assert edge_statements[0][1] is True
    assert all(" returning " not in statement for statement, _ in statements)
    assert (
        db_session.query(OpeningPositionScore)
        .filter(OpeningPositionScore.batch_id == batch.id)
        .count()
        > 0
    )
    assert (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == batch.id)
        .count()
        > 0
    )


def test_large_read_model_insert_failure_rolls_back_whole_batch(db_session):
    """A failing second bulk insert leaves no partial batch or position rows."""
    _seed_black_opening_session(db_session)

    def fail_edge_insert(_conn, _cursor, statement, _params, _context, _many):
        if "insert into opening_position_edges" in statement.lower():
            raise RuntimeError("injected edge insert failure")

    event.listen(test_engine, "before_cursor_execute", fail_edge_insert)
    try:
        with pytest.raises(RuntimeError, match="injected edge insert failure"):
            recompute_opening_scores(db_session, 123, "black")
    finally:
        event.remove(test_engine, "before_cursor_execute", fail_edge_insert)

    assert db_session.query(OpeningScoreBatch).count() == 0
    assert db_session.query(OpeningPositionScore).count() == 0
    assert db_session.query(OpeningPositionEdge).count() == 0


def test_recompute_writes_direct_position_rows(db_session):
    _seed_black_opening_session(db_session)

    batch = recompute_opening_scores(db_session, 123, "black")
    found_batch, rows = list_position_scores(db_session, 123, "black")

    assert found_batch.id == batch.id
    fens = {row.normalized_fen for row in rows}
    # Scoreable in-book positions along the played line get direct rows...
    assert {START_FEN, KINGS_PAWN_FEN, OPEN_GAME_FEN, KNIGHT_OPENING_FEN} <= fens
    # ...but TWO_KNIGHTS is an in-book leaf with no evidence below: not materialized.
    assert TWO_KNIGHTS_FEN not in fens
    assert all(row.batch_id == batch.id for row in rows)
    assert all(row.user_id == 123 and row.player_color == "black" for row in rows)
    assert all(row.in_book and row.has_evidence for row in rows)
    assert all(row.computed_at == batch.computed_at for row in rows)
    kings_pawn = next(row for row in rows if row.normalized_fen == KINGS_PAWN_FEN)
    assert kings_pawn.opening_score is not None
    assert kings_pawn.confidence is not None
    assert kings_pawn.coverage is not None


def test_position_rows_match_named_root_metrics(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    _, named_rows = list_cached_opening_scores(db_session, 123, "black")
    named_by_key = {row.opening_key: row for row in named_rows}
    _, position_rows = list_position_scores(db_session, 123, "black")
    position_by_fen = {row.normalized_fen: row for row in position_rows}

    # The named-root row and the direct position row for the same FEN come from one
    # shared traversal, so their metrics agree exactly.
    for key in (KINGS_PAWN_FEN, KNIGHT_OPENING_FEN):
        named = named_by_key[key]
        position = position_by_fen[key]
        assert position.opening_score == pytest.approx(named.opening_score)
        assert position.confidence == pytest.approx(named.confidence)
        assert position.coverage == pytest.approx(named.coverage)
        assert position.sample_size == named.sample_size
        assert position.game_count == named.game_count


def test_position_rows_cascade_through_batch_retention(db_session):
    _seed_black_opening_session(db_session)

    for _ in range(5):
        recompute_opening_scores(db_session, 123, "black")

    remaining = (
        db_session.query(OpeningScoreBatch)
        .filter(OpeningScoreBatch.user_id == 123, OpeningScoreBatch.player_color == "black")
        .all()
    )
    kept_ids = {b.id for b in remaining}
    assert len(kept_ids) == 2

    # Every surviving batch still has position rows...
    assert (
        db_session.query(OpeningPositionScore)
        .filter(OpeningPositionScore.batch_id.in_(kept_ids))
        .count()
        > 0
    )
    # ...and pruned batches' position rows are gone via ON DELETE CASCADE.
    orphan_positions = (
        db_session.query(OpeningPositionScore)
        .filter(OpeningPositionScore.batch_id.notin_(kept_ids))
        .count()
    )
    assert orphan_positions == 0


def test_lookup_position_scores_normalizes_raw_fens(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    # Raw six-field FENs (with clocks) from a tree UI must normalize before lookup.
    batch, found = lookup_position_scores(
        db_session, 123, "black", [KINGS_PAWN_FULL, KNIGHT_OPENING_FULL]
    )
    assert batch is not None
    assert KINGS_PAWN_FEN in found
    assert KNIGHT_OPENING_FEN in found
    assert found[KINGS_PAWN_FEN].opening_score is not None


def test_lookup_position_scores_in_graph_no_evidence_is_no_data(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    # TWO_KNIGHTS is in the OpeningGraph but has no evidence below, so it was not
    # materialized: the lookup omits it and the API renders no-data.
    batch, found = lookup_position_scores(db_session, 123, "black", [TWO_KNIGHTS_FULL])
    assert batch is not None
    assert TWO_KNIGHTS_FEN not in found


def test_lookup_position_scores_without_batch_returns_empty(db_session):
    batch, found = lookup_position_scores(db_session, 123, "black", [KINGS_PAWN_FULL])
    assert batch is None
    assert found == {}


def test_lookup_position_scores_normalizes_four_field_noncanonical_ep(db_session):
    _seed_black_opening_session(db_session)
    recompute_opening_scores(db_session, 123, "black")

    # A four-field FEN with a stated-but-impossible en passant square must be
    # canonicalized (EP -> "-") before lookup, hitting the stored normalized key
    # instead of silently missing.
    noncanonical = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3"
    batch, found = lookup_position_scores(db_session, 123, "black", [noncanonical])
    assert batch is not None
    assert KINGS_PAWN_FEN in found


# ---------------------------------------------------------------------------
# Observed-edge read model persistence + lookup (g-tree-fast-cache).
# ---------------------------------------------------------------------------


def test_recompute_persists_observed_edges(db_session):
    """One OpeningPositionEdge per overlay.edges, with matching counters, so the tree
    read path never rebuilds the overlay."""
    _seed_black_opening_session(db_session)

    # The overlay the scorer builds during recompute is a pure function of the same
    # committed session_moves, so its edges are exactly what should be persisted.
    overlay = _real_overlay_evidence(db_session, 123, "black", _make_graph())
    expected = {
        (e.parent_fen, e.child_fen): e for e in overlay.edges.values()
    }
    assert expected, "fixture should yield observed edges"

    batch = recompute_opening_scores(db_session, 123, "black")

    rows = (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == batch.id)
        .all()
    )
    assert len(rows) == len(expected)
    by_key = {(r.parent_fen, r.child_fen): r for r in rows}
    assert set(by_key) == set(expected)
    for key, row in by_key.items():
        edge = expected[key]
        assert row.uci == edge.uci
        assert row.traversal_count == edge.traversal_count
        assert row.live_attempts == edge.live_attempts
        assert row.live_passes == edge.live_passes
        assert row.live_fails == edge.live_fails
        assert row.user_id == 123 and row.player_color == "black"
        assert row.computed_at == batch.computed_at


def test_edge_rows_cascade_through_batch_retention(db_session):
    """Edge rows ride the same keep=2 generation retention + ON DELETE CASCADE as
    position rows — pruned batches leave no orphan edge rows (exercises g-9zoe FK)."""
    _seed_black_opening_session(db_session)

    for _ in range(5):
        recompute_opening_scores(db_session, 123, "black")

    kept_ids = {
        b.id
        for b in db_session.query(OpeningScoreBatch)
        .filter(
            OpeningScoreBatch.user_id == 123,
            OpeningScoreBatch.player_color == "black",
        )
        .all()
    }
    assert len(kept_ids) == 2
    # Surviving batches still carry their edge rows...
    assert (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id.in_(kept_ids))
        .count()
        > 0
    )
    # ...and pruned batches' edge rows are gone (no orphans).
    assert (
        db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id.notin_(kept_ids))
        .count()
        == 0
    )


def test_lookup_observed_edges_for_parent_reconstructs_edge_evidence(db_session):
    """The read side returns EdgeEvidence with quality zeroed (not persisted), keyed
    to one parent via the bounded per-parent index."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")

    edges = lookup_observed_edges_for_parent(db_session, batch.id, KINGS_PAWN_FEN)
    assert edges, "1.e4 e5 is an observed edge out of KINGS_PAWN_FEN"
    edge = next(e for e in edges if e.uci == "e7e5")
    assert edge.parent_fen == KINGS_PAWN_FEN
    assert edge.child_fen == OPEN_GAME_FEN
    assert edge.traversal_count >= 1
    # Quality columns are not persisted; the tree never reads them.
    assert edge.quality_sum == 0.0
    assert edge.quality_count == 0
    # A parent with no observed edges resolves to an empty list (book-only).
    assert lookup_observed_edges_for_parent(db_session, batch.id, TWO_KNIGHTS_FEN) == []


def test_lookup_observed_edges_for_parents_returns_only_requested(db_session):
    """The bounded read loads observed edges for ONLY the requested parents (the tree's
    visible node set), indexed by normalized parent FEN — Option B's replacement for the
    whole-batch eager load (g-0qe6). Parents with no edges are absent; quality is
    zeroed."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")

    # Request only the 1.e4 position (which has the e7e5 edge) — NOT the whole batch.
    by_parent = lookup_observed_edges_for_parents(
        db_session, batch.id, [KINGS_PAWN_FEN, TWO_KNIGHTS_FEN]
    )
    assert isinstance(by_parent, dict)
    # The 1.e4 e5 edge is grouped under its parent FEN, matching the per-parent read.
    assert KINGS_PAWN_FEN in by_parent
    edge = next(e for e in by_parent[KINGS_PAWN_FEN] if e.uci == "e7e5")
    assert edge.child_fen == OPEN_GAME_FEN
    assert edge.traversal_count >= 1
    # Quality columns are not persisted; the tree never reads them.
    assert edge.quality_sum == 0.0
    assert edge.quality_count == 0
    # A requested parent with no observed edges is simply absent (callers use .get).
    assert TWO_KNIGHTS_FEN not in by_parent
    # A parent NOT requested is never loaded, even though it has edges in the batch —
    # the whole point of the bounded read.
    other_parents = {
        r.parent_fen
        for r in db_session.query(OpeningPositionEdge)
        .filter(OpeningPositionEdge.batch_id == batch.id)
        .all()
    } - {KINGS_PAWN_FEN}
    assert other_parents, "fixture should have parents beyond 1.e4"
    assert other_parents.isdisjoint(by_parent.keys())


def test_lookup_observed_edges_for_parents_empty_input_skips_query(db_session):
    """Empty parent set short-circuits to an empty map (no SELECT)."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")
    assert lookup_observed_edges_for_parents(db_session, batch.id, []) == {}


def test_lookup_observed_edges_for_parents_chunks_large_in_list(db_session):
    """A parent set larger than the chunk cap is split across multiple IN-queries and
    merged back into one map (SQLite ~999-param defence) — the real parent's edge still
    resolves alongside 1000+ unrelated FENs."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")

    padding = [f"synthetic-fen-{i} w - -" for i in range(1500)]
    by_parent = lookup_observed_edges_for_parents(
        db_session, batch.id, [KINGS_PAWN_FEN, *padding]
    )
    # Despite >900 requested FENs (forcing >1 chunk), the real edge resolves and the
    # synthetic FENs (no rows) are absent.
    assert KINGS_PAWN_FEN in by_parent
    assert any(e.uci == "e7e5" for e in by_parent[KINGS_PAWN_FEN])
    assert all(not key.startswith("synthetic-fen-") for key in by_parent)


def test_lookup_observed_edges_for_parents_unknown_batch_is_empty(db_session):
    """An unknown batch yields an empty map (book-only, zero edges)."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")
    assert (
        lookup_observed_edges_for_parents(
            db_session, batch.id + 9999, [KINGS_PAWN_FEN]
        )
        == {}
    )


def test_observed_edge_parent_chunk_count_matches_chunking():
    """The chunk-count helper (used by the tree builder to count actual SELECTs, not
    waves) reports one round-trip per IN-chunk: 0 for empty, 1 within the cap, and
    ceil(n / cap) when a wave splits."""
    cap = OBSERVED_EDGE_PARENT_CHUNK_SIZE
    assert observed_edge_parent_chunk_count(0) == 0
    assert observed_edge_parent_chunk_count(1) == 1
    assert observed_edge_parent_chunk_count(cap) == 1
    assert observed_edge_parent_chunk_count(cap + 1) == 2
    assert observed_edge_parent_chunk_count(2 * cap) == 2
    assert observed_edge_parent_chunk_count(2 * cap + 1) == 3


def test_lookup_position_scores_for_batch_resolves_by_batch(db_session):
    """The batch-scoped position lookup normalizes raw FENs and resolves rows for the
    given batch_id without re-querying the latest batch or touching the ORM row."""
    _seed_black_opening_session(db_session)
    batch = recompute_opening_scores(db_session, 123, "black")

    found = lookup_position_scores_for_batch(
        db_session, batch.id, [KINGS_PAWN_FULL, KNIGHT_OPENING_FULL]
    )
    assert KINGS_PAWN_FEN in found
    assert KNIGHT_OPENING_FEN in found
    assert found[KINGS_PAWN_FEN].opening_score is not None
    # Empty input short-circuits.
    assert lookup_position_scores_for_batch(db_session, batch.id, []) == {}


# ---------------------------------------------------------------------------
# load_cached_rows_nonblocking — never blocks on a cold cache (g-a5v3)
#
# Same patch targets as the load_cached_rows block above: the scheduler funcs
# are lazy-imported inside the function, so they are patched at the source
# module (app.opening_score_scheduler).
# ---------------------------------------------------------------------------

def test_load_cached_rows_nonblocking_warm_serves_cache_and_schedules_background():
    """Warm: identical to load_cached_rows — serve the batch and enqueue
    UNCONDITIONALLY (the only trigger catching evidence changes with no
    write-path enqueue)."""
    sentinel_batch = object()
    sentinel_rows = object()
    snapshotted = object()
    with patch(
        "app.opening_cache.list_cached_opening_scores",
        return_value=(sentinel_batch, sentinel_rows),
    ), patch(
        "app.opening_cache._snapshot_cached_rows", return_value=snapshotted
    ) as snapshot, patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.is_recompute_scheduled", return_value=True
    ) as is_scheduled, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows, pending = load_cached_rows_nonblocking("db", 123, "black")

    assert batch is sentinel_batch
    assert rows is snapshotted
    assert pending is False
    snapshot.assert_called_once_with(sentinel_rows)
    # Unconditional on the warm path — the scheduled-guard must NOT suppress it.
    request_recompute.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.SESSION_LINEAGE_WARM
    )
    is_scheduled.assert_not_called()
    refresh_now.assert_not_called()


def test_load_cached_rows_nonblocking_cold_returns_empty_without_blocking():
    """Cold WITH evidence, nothing scheduled: report pending immediately, never
    call refresh_now, and enqueue the recompute."""
    with patch(
        "app.opening_cache.list_cached_opening_scores", return_value=(None, [])
    ) as list_cached, patch(
        "app.opening_cache.has_opening_evidence", return_value=True
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.is_recompute_scheduled", return_value=False
    ), patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows, pending = load_cached_rows_nonblocking("db", 123, "black")

    assert batch is None
    assert rows == []
    assert pending is True
    refresh_now.assert_not_called()
    request_recompute.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.SESSION_LINEAGE_COLD
    )
    # Cold path must NOT re-list: there is nothing to wait for.
    list_cached.assert_called_once()


def test_load_cached_rows_nonblocking_cold_already_scheduled_does_not_reenqueue():
    """Cold, recompute ALREADY scheduled: skip request_recompute. It sets
    deadline = now + quiet_window, so an unguarded enqueue from a polling
    reader would repeatedly postpone the very compute it is waiting on."""
    with patch(
        "app.opening_cache.list_cached_opening_scores", return_value=(None, [])
    ), patch(
        "app.opening_cache.has_opening_evidence", return_value=True
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.is_recompute_scheduled", return_value=True
    ), patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows, pending = load_cached_rows_nonblocking("db", 123, "black")

    assert batch is None
    assert rows == []
    assert pending is True
    request_recompute.assert_not_called()
    refresh_now.assert_not_called()


def test_load_cached_rows_nonblocking_cold_reenqueues_after_work_lost():
    """Cold and nothing scheduled after a prior enqueue (worker fault/restart
    dropped the work): the next read re-enqueues rather than waiting forever."""
    with patch(
        "app.opening_cache.list_cached_opening_scores", return_value=(None, [])
    ), patch(
        "app.opening_cache.has_opening_evidence", return_value=True
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ), patch(
        "app.opening_score_scheduler.is_recompute_scheduled", side_effect=[True, False]
    ), patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        load_cached_rows_nonblocking("db", 123, "black")  # scheduled -> skip
        load_cached_rows_nonblocking("db", 123, "black")  # work lost -> retry

    request_recompute.assert_called_once_with(
        123, "black", source=OpeningScoreTrigger.SESSION_LINEAGE_COLD
    )


def test_load_cached_rows_nonblocking_cold_without_evidence_is_not_pending():
    """Cold with NO eligible evidence: NOT pending, and no enqueue.

    ``recompute_opening_scores_if_needed`` bails out without creating a batch
    when ``has_opening_evidence`` is false, so waiting can never produce one.
    Reporting pending here would pin a first-time user (whose only game is still
    in progress, and so is not yet eligible evidence) behind a permanent loading
    state while their client re-scheduled no-op recomputes.
    """
    with patch(
        "app.opening_cache.list_cached_opening_scores", return_value=(None, [])
    ), patch(
        "app.opening_cache.has_opening_evidence", return_value=False
    ), patch(
        "app.opening_score_scheduler.refresh_now"
    ) as refresh_now, patch(
        "app.opening_score_scheduler.is_recompute_scheduled"
    ) as is_scheduled, patch(
        "app.opening_score_scheduler.request_recompute"
    ) as request_recompute:
        batch, rows, pending = load_cached_rows_nonblocking("db", 123, "black")

    assert batch is None
    assert rows == []
    assert pending is False
    request_recompute.assert_not_called()
    is_scheduled.assert_not_called()
    refresh_now.assert_not_called()


# --- recompute result contract -------------------------------------------------
# The scheduler labels its run outcome, its rebuild reason, and the analytics
# `reason` property straight off these fields, so an impossible combination must be
# unconstructible rather than merely undocumented.


def test_unknown_disposition_is_rejected():
    """An unrecognised disposition must not be accepted as if it were no_evidence.

    Falling through the per-disposition checks is exactly the presence-inference
    failure the enum replaced: the scheduler would log run_outcome for a value it
    never agreed to, and PostHog would gain an unbounded outcome vocabulary.
    """
    for bad in ("unexpected", "REBUILT", None, 0, object()):
        with pytest.raises(ValueError, match="unknown recompute disposition"):
            OpeningScoreRecomputeResult(disposition=bad, batch=None)


def test_equivalent_disposition_string_normalizes_to_the_enum():
    # Every downstream reader compares with `is`, so a bare value must not survive
    # as a plain str and silently fail every one of those comparisons.
    result = OpeningScoreRecomputeResult(
        disposition="cached", batch=object(), reason=None
    )
    assert result.disposition is RecomputeDisposition.CACHED


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (
            {"disposition": RecomputeDisposition.REBUILT, "batch": None,
             "reason": "cache_miss"},
            "requires a batch",
        ),
        (
            {"disposition": RecomputeDisposition.REBUILT, "batch": object(),
             "reason": None},
            "requires a rebuild reason",
        ),
        (
            {"disposition": RecomputeDisposition.REBUILT, "batch": object(),
             "reason": "because_i_said_so"},
            "requires a rebuild reason",
        ),
        (
            {"disposition": RecomputeDisposition.CACHED, "batch": None},
            "requires the existing batch",
        ),
        (
            {"disposition": RecomputeDisposition.CACHED, "batch": object(),
             "reason": "cache_miss"},
            "must carry no rebuild reason",
        ),
        (
            {"disposition": RecomputeDisposition.NO_EVIDENCE, "batch": object()},
            "must carry no batch",
        ),
        (
            {"disposition": RecomputeDisposition.NO_EVIDENCE, "batch": None,
             "reason": "cache_miss"},
            "must carry no rebuild reason",
        ),
    ],
)
def test_impossible_result_combinations_are_rejected(kwargs, match):
    with pytest.raises(ValueError, match=match):
        OpeningScoreRecomputeResult(**kwargs)
