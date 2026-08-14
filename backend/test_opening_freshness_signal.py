"""Differential tests for the cheap opening-score freshness signal (g-jact).

The acceptance property, checked scenario by scenario and in a randomized loop:

    _is_batch_fresh(batch) == True
        ⟹ evidence overlay unchanged
        ⟹ score outputs at the batch clock unchanged

False-negatives (stale verdict on unchanged evidence) are allowed — they only
cost an unnecessary, still-correct rebuild. False-positives (fresh verdict over
changed evidence) are forbidden: they would serve stale scores forever.

The full raw digest remains a mutation/non-vacuity oracle, but fresh-v2
operational batches intentionally store the overlay's narrower exact shared
dependency scope. A shared write at a broad-only candidate may therefore change
the raw digest while the batch correctly remains fresh; that divergence has an
explicit regression test below.

Per-user mutation scenarios drive the PRODUCTION choke-points (the /end, /fail,
/continue, /abandon, /moves, SRS-review endpoints and blunder recording),
so a missed or mis-gated bump site fails here. Shared-table scenarios use raw
SQL on purpose: the evidence_epoch DB triggers must fire for ANY writer,
including direct UPDATE/DELETE that bypass every app code path.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from conftest import TestingSessionLocal

import app.opening_cache as oc
from app.api.blunder import (
    _bump_evidence_for_new_blunder,
    _upsert_blunder_target,
)
from app.fen import active_color
from app.models import Blunder, GameSession, OpeningScoreBatch, Position, SessionMove
from app.opening_cache import (
    _is_batch_fresh,
    bump_evidence_seq,
    current_cache_epoch,
    current_evidence_seq,
    list_cached_opening_scores,
    recompute_opening_scores,
    recompute_opening_scores_if_needed,
    reserve_opening_score_generation,
)
from app.opening_evidence import EvidenceOverlay, raw_evidence_inputs_digest
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import PositionScore, RootScore
from app.opening_roots import OpeningRoot, OpeningRoots
from app.opening_transposition_artifact import EMPTY_DENSIFIED_EDGES

USER = 4242

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
KINGS_PAWN_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
OPEN_GAME_FEN = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"

START_FULL = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
KINGS_PAWN_FULL = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
OPEN_GAME_FULL = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"

# A shared-cache position that is NEVER in the test user's candidate scope.
NON_CANDIDATE_FULL = "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1"

# A standalone candidate position reachable only through the digest's BROAD
# candidate SQL (its session breaks board continuity, so the overlay excludes
# the whole session and its narrow fallback candidates never include it).
BROKEN_CHAIN_FULL = "rnbqkbnr/ppp1pppp/8/3p4/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2"

# batch_id -> (overlay, named-root scores, direct position scores), all scored
# under that batch's own fixed computed_at clock. Cleared by the autouse fixture.
SemanticOracle = tuple[
    EvidenceOverlay,
    tuple[RootScore, ...],
    tuple[PositionScore, ...],
]
_SEMANTIC_ORACLES: dict[int, SemanticOracle] = {}


def _make_graph() -> OpeningGraph:
    start = OpeningGraphNode(START_FEN, active_color(START_FEN))
    kp = OpeningGraphNode(KINGS_PAWN_FEN, active_color(KINGS_PAWN_FEN))
    og = OpeningGraphNode(OPEN_GAME_FEN, active_color(OPEN_GAME_FEN))
    start.children["e2e4"] = KINGS_PAWN_FEN
    kp.parents.add((START_FEN, "e2e4"))
    kp.children["e7e5"] = OPEN_GAME_FEN
    og.parents.add((KINGS_PAWN_FEN, "e7e5"))
    graph = OpeningGraph(
        {START_FEN: start, KINGS_PAWN_FEN: kp, OPEN_GAME_FEN: og}, START_FEN
    )
    graph.freeze()
    return graph


def _make_roots() -> OpeningRoots:
    kp = OpeningRoot(
        opening_key=KINGS_PAWN_FEN,
        opening_name="King's Pawn Game",
        opening_family="Open Games",
        eco="B00",
        depth=1,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )
    return OpeningRoots(
        {KINGS_PAWN_FEN: kp},
        {
            KINGS_PAWN_FEN: frozenset([KINGS_PAWN_FEN]),
            OPEN_GAME_FEN: frozenset([KINGS_PAWN_FEN]),
        },
    )


@pytest.fixture(autouse=True)
def _stub_registry():
    _SEMANTIC_ORACLES.clear()
    with (
        patch("app.opening_cache.get_opening_graph", return_value=_make_graph()),
        patch("app.opening_cache.get_opening_roots", return_value=_make_roots()),
    ):
        yield
    _SEMANTIC_ORACLES.clear()


# ---------------------------------------------------------------------------
# Raw-SQL seed helpers (mirroring test_opening_evidence's idiom)
# ---------------------------------------------------------------------------

def _insert_user(db, user_id: int = USER) -> None:
    db.execute(text(
        "INSERT OR IGNORE INTO users (id, username, is_anonymous) VALUES (:id, :u, 1)"
    ), {"id": user_id, "u": f"user{user_id}"})
    db.commit()


def _insert_session(
    db,
    *,
    user_id: int = USER,
    player_color: str = "white",
    status: str = "ended",
    session_mode: str = "normal",
    drill_state: str | None = None,
    drill_terminal_reason: str | None = None,
    is_rated: bool = True,
) -> uuid.UUID:
    # ORM insert on purpose: the UUID column's storage representation must match
    # what the endpoints (also ORM) read and reference, or FK joins silently miss.
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        ended_at=(
            datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
            if status == "ended" else None
        ),
        status=status,
        result="checkmate_win" if status == "ended" else None,
        engine_elo=1500,
        player_color=player_color,
        is_rated=is_rated,
        session_mode=session_mode,
        drill_state=drill_state,
        drill_terminal_reason=drill_terminal_reason,
    )
    db.add(session)
    db.commit()
    return session.id


def _insert_move(
    db,
    session_id: uuid.UUID,
    move_number: int,
    color: str,
    move_san: str,
    fen_before: str,
    fen_after: str,
    *,
    eval_delta: int | None = 10,
    eval_cp: int | None = None,
    best_move_eval_cp: int | None = None,
) -> None:
    db.add(SessionMove(
        session_id=session_id, move_number=move_number, color=color,
        move_san=move_san, fen_before=fen_before, fen_after=fen_after,
        eval_delta=eval_delta, eval_cp=eval_cp,
        best_move_eval_cp=best_move_eval_cp,
    ))
    db.commit()


def _insert_position(db, *, user_id: int = USER, fen_raw: str, color: str) -> int:
    position = Position(
        user_id=user_id, fen_hash=uuid.uuid4().hex, fen_raw=fen_raw,
        active_color=color,
    )
    db.add(position)
    db.commit()
    return position.id


def _insert_analysis_cache(db, fen_before: str, move_uci: str = "e2e4",
                           played_eval: int = 30) -> None:
    db.execute(text("""
        INSERT INTO analysis_cache (fen_before, move_uci, move_san, played_eval)
        VALUES (:fb, :mu, 'x', :pe)
    """), {"fb": fen_before, "mu": move_uci, "pe": played_eval})
    db.commit()


def _seed_base_evidence(db) -> str:
    """One eligible white session whose first move lacks primary evals, so
    START_FULL is a shared-scope candidate FEN."""
    _insert_user(db)
    sid = _insert_session(db)
    _insert_move(db, sid, 1, "white", "e4", START_FULL, KINGS_PAWN_FULL)
    _insert_move(db, sid, 1, "black", "e5", KINGS_PAWN_FULL, OPEN_GAME_FULL)
    return sid


def _semantic_snapshot(
    db,
    batch: OpeningScoreBatch,
    user_id: int,
    color: str,
) -> SemanticOracle:
    """The production freshness consequence, independent of the raw digest.

    Rebuild the overlay and deterministic score outputs at the persisted batch
    clock. Wall-clock decay is therefore held fixed while evidence semantics are
    compared.
    """
    graph = _make_graph()
    roots = _make_roots()
    overlay = oc.overlay_evidence(db, user_id, color, graph)
    computed_at = batch.computed_at
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    scores, position_scores = oc._build_cached_scores(
        color,
        graph,
        overlay,
        roots,
        computed_at,
        EMPTY_DENSIFIED_EDGES,
    )
    return overlay, tuple(scores), tuple(position_scores)


def _remember_semantics(
    db,
    batch: OpeningScoreBatch,
    user_id: int,
    color: str,
) -> OpeningScoreBatch:
    _SEMANTIC_ORACLES[batch.id] = _semantic_snapshot(db, batch, user_id, color)
    return batch


def _build_batch(db, user_id: int = USER, color: str = "white") -> OpeningScoreBatch:
    """Build through the production fresh-v2 scheduler decision path."""
    result = recompute_opening_scores_if_needed(db, user_id, color)
    assert result.batch is not None
    return _remember_semantics(db, result.batch, user_id, color)


def _build_direct_batch(
    db, user_id: int = USER, color: str = "white"
) -> OpeningScoreBatch:
    """Explicit full-snapshot builder for the one broad-scope contrast test."""
    return _remember_semantics(
        db, recompute_opening_scores(db, user_id, color), user_id, color
    )


def _check_sound(db, user_id: int, color: str) -> bool:
    """The acceptance property: fresh ⟹ overlay + score outputs unchanged."""
    batch, rows = list_cached_opening_scores(db, user_id, color)
    assert batch is not None
    fresh = _is_batch_fresh(db, batch, rows)
    if fresh:
        expected = _SEMANTIC_ORACLES[batch.id]
        actual = _semantic_snapshot(db, batch, user_id, color)
        assert actual == expected, (
            "FALSE POSITIVE: batch reported fresh but its overlay/scores changed"
        )
    return fresh


def _assert_stale(db, digest_before: str, *, user_id: int = USER,
                  color: str = "white") -> None:
    assert _check_sound(db, user_id, color) is False
    assert raw_evidence_inputs_digest(db, user_id, color) != digest_before, (
        "scenario did not actually change the digest — vacuous test"
    )


def _assert_fresh_unchanged(db, digest_before: str, *, user_id: int = USER,
                            color: str = "white") -> None:
    assert raw_evidence_inputs_digest(db, user_id, color) == digest_before
    assert _check_sound(db, user_id, color) is True


# ---------------------------------------------------------------------------
# Baseline + writer stamping
# ---------------------------------------------------------------------------

def test_noop_is_fresh(db_session):
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    _assert_fresh_unchanged(db_session, digest)


def test_writer_stamps_signal_and_scope(db_session):
    _seed_base_evidence(db_session)
    batch = _build_batch(db_session)
    assert batch.evidence_seq == current_evidence_seq(db_session, USER, "white")
    assert batch.cache_epoch == current_cache_epoch(db_session)
    assert batch.inputs_fingerprint is None
    assert batch.scoped_shared_digest is not None
    raw_fens, norm_fens = oc._load_batch_shared_scope(db_session, batch.id)
    assert START_FULL in raw_fens
    assert START_FEN in norm_fens


def test_verdict_path_never_calls_raw_digest(db_session):
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    batch, rows = list_cached_opening_scores(db_session, USER, "white")
    fail = Mock(side_effect=AssertionError("raw digest ran on the verdict path"))
    with (
        patch("app.opening_cache.raw_evidence_inputs_digest", fail),
        patch("app.opening_cache.raw_evidence_inputs_snapshot", fail),
    ):
        assert _is_batch_fresh(db_session, batch, rows) is True
    fail.assert_not_called()


# ---------------------------------------------------------------------------
# Per-user surfaces through the production choke-points
# ---------------------------------------------------------------------------

def _start_game(client, auth_headers, color="white"):
    resp = client.post(
        "/api/game/start",
        json={"engine_elo": 1500, "player_color": color},
        headers=auth_headers(user_id=USER),
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _upload_move(client, auth_headers, sid, *, move_number=1, color="white",
                 san="e4", fen_before=START_FULL, fen_after=KINGS_PAWN_FULL,
                 eval_delta=None):
    resp = client.post(
        f"/api/session/{sid}/moves",
        json={"moves": [{
            "move_number": move_number, "color": color, "move_san": san,
            "fen_before": fen_before, "fen_after": fen_after,
            **({"eval_delta": eval_delta} if eval_delta is not None else {}),
        }]},
        headers=auth_headers(user_id=USER),
    )
    assert resp.status_code == 200


def _end_game(client, auth_headers, sid):
    with patch("app.opening_score_scheduler.request_recompute"):
        resp = client.post(
            "/api/game/end",
            json={"session_id": sid, "result": "checkmate_win",
                  "pgn": "1. e4", "is_rated": False},
            headers=auth_headers(user_id=USER),
        )
    assert resp.status_code == 200


def test_live_upload_does_not_bump_then_end_flips(client, auth_headers, db_session):
    # Scenarios 16 (live upload -> no churn) and 3 (end -> eligibility flip).
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    seq_before = current_evidence_seq(db_session, USER, "white")

    sid = _start_game(client, auth_headers)
    _upload_move(client, auth_headers, sid)

    # Live/active session: no seq bump, digest unchanged, still fresh.
    assert current_evidence_seq(db_session, USER, "white") == seq_before
    _assert_fresh_unchanged(db_session, digest)

    # Ending the session flips eligibility (F->T): one bump, provably stale.
    _end_game(client, auth_headers, sid)
    assert current_evidence_seq(db_session, USER, "white") == seq_before + 1
    _assert_stale(db_session, digest)


def test_post_end_eval_backfill_upload_bumps(client, auth_headers, db_session):
    # Scenarios 1 + 2: an upload to an ENDED session (insert or in-place eval
    # backfill — the ON-CONFLICT upsert can't tell which) bumps -> stale.
    _seed_base_evidence(db_session)
    sid = _start_game(client, auth_headers)
    _upload_move(client, auth_headers, sid)
    _end_game(client, auth_headers, sid)

    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    seq_before = current_evidence_seq(db_session, USER, "white")

    _upload_move(client, auth_headers, sid, eval_delta=250)  # in-place backfill

    assert current_evidence_seq(db_session, USER, "white") == seq_before + 1
    _assert_stale(db_session, digest)


def _insert_drill(db, *, drill_state: str, reason: str | None = None) -> str:
    return _insert_session(
        db, status="active", session_mode="drill", drill_state=drill_state,
        drill_terminal_reason=reason, is_rated=False,
    )


def test_accuracy_fail_then_convert_flips_both_ways(client, auth_headers, db_session):
    # Scenarios 4 (accuracy fail: F->T with NO timestamp write) and 10 (convert:
    # T->F removes the drill's moves from the evidence set).
    _seed_base_evidence(db_session)
    drill_sid = _insert_drill(db_session, drill_state="root_reached")
    # Give the drill an uploaded move so eligibility actually moves evidence.
    _insert_move(db_session, drill_sid, 1, "white", "e4", START_FULL, KINGS_PAWN_FULL)
    drill = db_session.get(GameSession, drill_sid)
    drill.drill_opening_key = KINGS_PAWN_FEN
    drill.drill_line = "e2e4"
    db_session.commit()

    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    with patch("app.opening_score_scheduler.request_recompute"):
        resp = client.post(
            f"/api/drills/{drill_sid}/fail",
            json={"terminal_reason": "accuracy"},
            headers=auth_headers(user_id=USER),
        )
    assert resp.status_code == 200
    _assert_stale(db_session, digest)

    # Rebuild over the now-eligible drill, then convert it (T->F flip).
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    with patch("app.opening_score_scheduler.request_recompute"):
        resp = client.post(
            f"/api/drills/{drill_sid}/continue",
            json={"current_ply": 2},
            headers=auth_headers(user_id=USER),
        )
    assert resp.status_code == 200
    _assert_stale(db_session, digest)


def test_non_eligibility_transitions_do_not_bump(db_session):
    # Scenario 18: active -> root_reached and failed+off_route leave the
    # eligibility truth value False -> the digest ignores them; the production
    # sites carry no bump. (Direct writes mirror those no-bump sites.)
    _seed_base_evidence(db_session)
    drill_sid = _insert_drill(db_session, drill_state="active")
    _insert_move(db_session, drill_sid, 1, "white", "e4", START_FULL, KINGS_PAWN_FULL)

    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    drill = db_session.get(GameSession, drill_sid)
    drill.drill_state = "root_reached"
    db_session.commit()
    _assert_fresh_unchanged(db_session, digest)

    drill.drill_state = "failed"
    drill.drill_terminal_reason = "off_route"
    db_session.commit()
    _assert_fresh_unchanged(db_session, digest)


def test_ghost_target_blunder_bumps_when_source_eligible(db_session):
    # Scenario 11: a new blunder whose source session is already eligible is a
    # digest-visible ghost target -> bump -> stale.
    sid = _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    pos_id = _insert_position(db_session, fen_raw=KINGS_PAWN_FULL, color="black")
    blunder_id, _ = _upsert_blunder_target(
        db_session, user_id=USER, position_id=pos_id, user_move="Qh5",
        best_move="Nf3", eval_loss=200, source_session_id=sid,
    )
    _bump_evidence_for_new_blunder(
        db_session,
        db_session.get(Blunder, blunder_id),
        require_eligible_source=True,
    )
    db_session.commit()
    _assert_stale(db_session, digest)


def test_live_session_blunder_defers_to_eligibility_flip(client, auth_headers, db_session):
    # Scenario 17: a blunder recorded against a still-ACTIVE source session is
    # not yet a ghost target -> no bump, fresh; ending the session carries it.
    _seed_base_evidence(db_session)
    live_sid = _start_game(client, auth_headers)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    seq_before = current_evidence_seq(db_session, USER, "white")

    pos_id = _insert_position(db_session, fen_raw=KINGS_PAWN_FULL, color="black")
    _upsert_blunder_target(
        db_session, user_id=USER, position_id=pos_id, user_move="Qh5",
        best_move="Nf3", eval_loss=200, source_session_id=uuid.UUID(live_sid),
    )
    db_session.commit()

    assert current_evidence_seq(db_session, USER, "white") == seq_before
    _assert_fresh_unchanged(db_session, digest)

    _end_game(client, auth_headers, live_sid)
    _assert_stale(db_session, digest)


def test_srs_review_bumps(client, auth_headers, db_session):
    # Scenario 12: a new review row is digest-visible -> bump -> stale.
    sid = _seed_base_evidence(db_session)
    pos_id = _insert_position(db_session, fen_raw=KINGS_PAWN_FULL, color="black")
    _upsert_blunder_target(
        db_session, user_id=USER, position_id=pos_id, user_move="Qh5",
        best_move="Nf3", eval_loss=200, source_session_id=sid,
    )
    db_session.commit()
    blunder_id = db_session.execute(text(
        "SELECT id FROM blunders WHERE user_id = :u"), {"u": USER}
    ).fetchone()[0]

    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    resp = client.post(
        "/api/srs/review",
        json={"session_id": str(sid), "blunder_id": blunder_id, "passed": True,
              "user_move": "Nf3", "eval_delta": 5},
        headers=auth_headers(user_id=USER),
    )
    assert resp.status_code == 200
    _assert_stale(db_session, digest)


def test_bump_scopes_by_player_color(db_session):
    # A bump attributed to one color must not mark the other color's batch stale
    # — and the other color's digest must genuinely be unchanged.
    _seed_base_evidence(db_session)  # white evidence
    black_sid = _insert_session(db_session, player_color="black")
    _insert_move(db_session, black_sid, 1, "white", "e4", START_FULL, KINGS_PAWN_FULL)
    _insert_move(db_session, black_sid, 1, "black", "e5", KINGS_PAWN_FULL, OPEN_GAME_FULL)

    _build_batch(db_session, USER, "white")
    _build_batch(db_session, USER, "black")
    white_digest = raw_evidence_inputs_digest(db_session, USER, "white")
    black_digest = raw_evidence_inputs_digest(db_session, USER, "black")

    # Ghost-target blunder sourced from the BLACK session.
    pos_id = _insert_position(db_session, fen_raw=KINGS_PAWN_FULL, color="black")
    blunder_id, _ = _upsert_blunder_target(
        db_session, user_id=USER, position_id=pos_id, user_move="Qh5",
        best_move="Nf3", eval_loss=200, source_session_id=black_sid,
    )
    _bump_evidence_for_new_blunder(
        db_session,
        db_session.get(Blunder, blunder_id),
        require_eligible_source=True,
    )
    db_session.commit()

    _assert_stale(db_session, black_digest, color="black")
    _assert_fresh_unchanged(db_session, white_digest, color="white")


# ---------------------------------------------------------------------------
# Shared surfaces through the DB triggers (any writer, incl. raw SQL)
# ---------------------------------------------------------------------------

def _scope_version(db, kind: str, fen: str) -> int | None:
    return db.execute(
        text(
            "SELECT last_changed_epoch FROM shared_evidence_scope_versions "
            "WHERE kind = :kind AND fen = :fen"
        ),
        {"kind": kind, "fen": fen},
    ).scalar_one_or_none()


def test_analysis_cache_trigger_marks_old_new_keys_and_delete_tombstone(db_session):
    db_session.execute(
        text(
            "INSERT INTO analysis_cache "
            "(fen_before, normalized_fen_before, move_uci, move_san, played_eval) "
            "VALUES (:raw, :norm, 'e2e4', 'e4', 1)"
        ),
        {"raw": START_FULL, "norm": START_FEN},
    )
    db_session.commit()
    inserted_epoch = current_cache_epoch(db_session)
    assert _scope_version(db_session, "raw", START_FULL) == inserted_epoch
    assert _scope_version(db_session, "norm", START_FEN) == inserted_epoch

    db_session.execute(
        text(
            "UPDATE analysis_cache SET fen_before = :raw, "
            "normalized_fen_before = :norm WHERE fen_before = :old"
        ),
        {"raw": KINGS_PAWN_FULL, "norm": KINGS_PAWN_FEN, "old": START_FULL},
    )
    db_session.commit()
    updated_epoch = current_cache_epoch(db_session)
    assert updated_epoch > inserted_epoch
    assert _scope_version(db_session, "raw", START_FULL) == updated_epoch
    assert _scope_version(db_session, "norm", START_FEN) == updated_epoch
    assert _scope_version(db_session, "raw", KINGS_PAWN_FULL) == updated_epoch
    assert _scope_version(db_session, "norm", KINGS_PAWN_FEN) == updated_epoch

    db_session.execute(text("DELETE FROM analysis_cache"))
    db_session.commit()
    deleted_epoch = current_cache_epoch(db_session)
    assert deleted_epoch > updated_epoch
    assert _scope_version(db_session, "raw", KINGS_PAWN_FULL) == deleted_epoch
    assert _scope_version(db_session, "norm", KINGS_PAWN_FEN) == deleted_epoch


def test_analysis_cache_normalized_null_transitions_are_tracked(db_session):
    _insert_analysis_cache(db_session, START_FULL)
    first_epoch = current_cache_epoch(db_session)
    assert _scope_version(db_session, "norm", START_FEN) is None

    db_session.execute(
        text(
            "UPDATE analysis_cache SET normalized_fen_before = :norm "
            "WHERE fen_before = :raw"
        ),
        {"norm": START_FEN, "raw": START_FULL},
    )
    db_session.commit()
    value_epoch = current_cache_epoch(db_session)
    assert value_epoch > first_epoch
    assert _scope_version(db_session, "norm", START_FEN) == value_epoch

    db_session.execute(
        text(
            "UPDATE analysis_cache SET normalized_fen_before = NULL "
            "WHERE fen_before = :raw"
        ),
        {"raw": START_FULL},
    )
    db_session.commit()
    null_epoch = current_cache_epoch(db_session)
    assert _scope_version(db_session, "norm", START_FEN) == null_epoch


def test_position_analysis_trigger_marks_old_new_and_delete(db_session):
    db_session.execute(
        text(
            "INSERT INTO position_analysis (normalized_fen, fen, best_move_uci) "
            "VALUES (:norm, :raw, 'e2e4')"
        ),
        {"norm": START_FEN, "raw": START_FULL},
    )
    db_session.commit()
    inserted_epoch = current_cache_epoch(db_session)
    assert _scope_version(db_session, "norm", START_FEN) == inserted_epoch

    db_session.execute(
        text(
            "UPDATE position_analysis SET normalized_fen = :new "
            "WHERE normalized_fen = :old"
        ),
        {"new": KINGS_PAWN_FEN, "old": START_FEN},
    )
    db_session.commit()
    updated_epoch = current_cache_epoch(db_session)
    assert _scope_version(db_session, "norm", START_FEN) == updated_epoch
    assert _scope_version(db_session, "norm", KINGS_PAWN_FEN) == updated_epoch

    db_session.execute(text("DELETE FROM position_analysis"))
    db_session.commit()
    assert _scope_version(
        db_session, "norm", KINGS_PAWN_FEN
    ) == current_cache_epoch(db_session)


def test_shared_trigger_side_effects_roll_back_with_source_write(db_session):
    epoch = current_cache_epoch(db_session)
    db_session.execute(
        text(
            "INSERT INTO analysis_cache "
            "(fen_before, move_uci, move_san, played_eval) "
            "VALUES (:raw, 'e2e4', 'e4', 1)"
        ),
        {"raw": START_FULL},
    )
    db_session.rollback()

    assert current_cache_epoch(db_session) == epoch
    assert _scope_version(db_session, "raw", START_FULL) is None


def test_sqlite_unqualified_delete_keeps_every_exact_tombstone(db_session):
    _insert_analysis_cache(db_session, START_FULL)
    _insert_analysis_cache(db_session, KINGS_PAWN_FULL, move_uci="e7e5")
    before_invalidation = db_session.execute(
        text(
            "SELECT kind, last_changed_epoch "
            "FROM shared_evidence_scope_invalidations ORDER BY kind"
        )
    ).all()

    db_session.execute(text("DELETE FROM analysis_cache"))
    db_session.commit()
    epoch = current_cache_epoch(db_session)

    assert _scope_version(db_session, "raw", START_FULL) <= epoch
    assert _scope_version(db_session, "raw", KINGS_PAWN_FULL) == epoch
    assert db_session.execute(
        text(
            "SELECT kind, last_changed_epoch "
            "FROM shared_evidence_scope_invalidations ORDER BY kind"
        )
    ).all() == before_invalidation

def test_analysis_cache_insert_at_candidate_fen_stales(db_session):
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    epoch_before = current_cache_epoch(db_session)

    _insert_analysis_cache(db_session, START_FULL)

    assert current_cache_epoch(db_session) > epoch_before  # trigger fired
    _assert_stale(db_session, digest)


def test_analysis_cache_inplace_update_at_candidate_fen_stales(db_session):
    _seed_base_evidence(db_session)
    _insert_analysis_cache(db_session, START_FULL)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    db_session.execute(text(
        "UPDATE analysis_cache SET played_eval = 999 WHERE fen_before = :fb"
    ), {"fb": START_FULL})
    db_session.commit()
    _assert_stale(db_session, digest)


@pytest.mark.parametrize("column,value", [
    # Every one of these is read off the MOVE row by
    # ``resolve_coherent_evidence_tuple`` and can flip an opening-evidence pair
    # between accepted and refused on its own (g-v21l):
    ("eval_delta", 7),            # check 4 + the move-only recomputed-delta equality
    ("best_move_uci", "d2d4"),    # COMBINED-vs-move-only branch + facts-match
    ("best_line_uci", "d2d4 d7d5"),  # facts-match
    ("classification", "blunder"),   # the shared classification validator
])
def test_move_row_coherence_inputs_stale_the_digest(db_session, column, value):
    """A coherence-input-only update must stale the batch.

    The trigger bumps ``evidence_epoch`` for any of these writes, which drops the
    check to the scoped re-hash — so if the column is missing from the ``AC|``
    projection the re-hash MATCHES and ``_cheap_evidence_fresh`` re-arms the batch
    indefinitely, serving an overlay whose coherence verdict has since flipped.
    Freshness must therefore hash a superset of the facts the overlay consumes: the
    full set the pairing rule READS, including fields never taken as truth from
    this row.
    """
    _seed_base_evidence(db_session)
    _insert_analysis_cache(db_session, START_FULL)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    db_session.execute(
        text(f"UPDATE analysis_cache SET {column} = :v WHERE fen_before = :fb"),
        {"v": value, "fb": START_FULL},
    )
    db_session.commit()
    _assert_stale(db_session, digest)


def test_analysis_cache_delete_at_candidate_fen_stales(db_session):
    # Scenario 9 — the repair-invalidation direct-DELETE gap: only the trigger
    # sees this write.
    _seed_base_evidence(db_session)
    _insert_analysis_cache(db_session, START_FULL)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    db_session.execute(text(
        "DELETE FROM analysis_cache WHERE fen_before = :fb"), {"fb": START_FULL})
    db_session.commit()
    _assert_stale(db_session, digest)


def test_position_analysis_winner_at_candidate_norm_stales(db_session):
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")

    db_session.execute(text("""
        INSERT INTO position_analysis (normalized_fen, fen, best_move_uci)
        VALUES (:nf, :f, 'e2e4')
    """), {"nf": START_FEN, "f": START_FULL})
    db_session.commit()
    _assert_stale(db_session, digest)


def test_viewer_association_flip_stales_operational_batch_and_changes_scores(
    db_session,
):
    # Association membership is part of the exact shared dependency set. Granting
    # this viewer access changes no analysis_cache evidence column, but it changes
    # which coherent browser tuple the overlay may consume.
    from app.analysis_profiles import (
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        stamp_profile_full,
    )
    from app.models import AnalysisCache, AnalysisCacheSubmission

    _seed_base_evidence(db_session)
    row = AnalysisCache(
        fen_before=START_FULL,
        normalized_fen_before=START_FEN,
        move_uci="e2e4",
        move_san="e4",
        played_eval=-30,
        best_eval=20,
        best_move_uci="d2d4",
        best_move_san="d4",
        best_line_uci="d2d4 d7d5",
        eval_delta=50,
        classification="good",
        source="analysis",
        analysis_profile_id=BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2",
        **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
    )
    db_session.add(row)
    db_session.commit()
    batch = _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    before_semantics = _SEMANTIC_ORACLES[batch.id]

    db_session.add(
        AnalysisCacheSubmission(analysis_cache_id=row.id, user_id=USER)
    )
    db_session.commit()

    assert _semantic_snapshot(db_session, batch, USER, "white") != before_semantics
    _assert_stale(db_session, digest)


def test_unrelated_shared_churn_stays_fresh_and_rearms(db_session):
    # Scenario 7 + the re-arm test: a shared write at a NON-candidate FEN drifts
    # the epoch but not the scoped digest -> fresh (digest also unchanged), and
    # the batch re-arms so the NEXT check is O(1) (no scoped re-hash).
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    epoch_before = current_cache_epoch(db_session)

    _insert_analysis_cache(db_session, NON_CANDIDATE_FULL, move_uci="g1f3")
    assert current_cache_epoch(db_session) > epoch_before

    real_scoped = oc.shared_scope_digest
    scoped_calls = {"n": 0}

    def counting_scoped(*args, **kwargs):
        scoped_calls["n"] += 1
        return real_scoped(*args, **kwargs)

    with patch("app.opening_cache.shared_scope_digest", counting_scoped):
        _assert_fresh_unchanged(db_session, digest)
        assert scoped_calls["n"] == 1  # epoch drift resolved via scoped re-hash

        # Re-armed: the follow-up verdict is O(1), no scoped re-hash.
        db_session.expire_all()
        _assert_fresh_unchanged(db_session, digest)
        assert scoped_calls["n"] == 1


def test_operational_scope_ignores_broad_only_candidate_without_score_change(
    db_session,
):
    # Fresh-v2's intentional divergence: this candidate FEN comes from a
    # broken-continuity session, so it is in the full raw oracle digest but never
    # in the overlay's exact fallback dependency set. A shared write there
    # changes the raw digest but cannot change the overlay or score outputs.
    _seed_base_evidence(db_session)
    broken_sid = _insert_session(db_session)
    # fen_before does not chain from any prior fen_after -> ContinuityError ->
    # the overlay excludes this session; the digest still hashes its rows.
    _insert_move(
        db_session, broken_sid, 1, "white", "d4", BROKEN_CHAIN_FULL, START_FULL
    )
    _build_batch(db_session)

    raw_fens, _ = oc._load_batch_shared_scope(
        db_session, list_cached_opening_scores(db_session, USER, "white")[0].id
    )
    assert BROKEN_CHAIN_FULL not in raw_fens

    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    _insert_analysis_cache(db_session, BROKEN_CHAIN_FULL, move_uci="d4d5")
    assert raw_evidence_inputs_digest(db_session, USER, "white") != digest
    assert _check_sound(db_session, USER, "white") is True


def test_direct_snapshot_retains_broad_scope_for_same_candidate(db_session):
    # Explicit/direct snapshots keep the stronger broad audit scope. The same
    # broad-only write therefore conservatively stales that batch.
    _seed_base_evidence(db_session)
    broken_sid = _insert_session(db_session)
    _insert_move(
        db_session, broken_sid, 1, "white", "d4", BROKEN_CHAIN_FULL, START_FULL
    )
    batch = _build_direct_batch(db_session)

    raw_fens, _ = oc._load_batch_shared_scope(db_session, batch.id)
    assert BROKEN_CHAIN_FULL in raw_fens

    digest = raw_evidence_inputs_digest(db_session, USER, "white")
    _insert_analysis_cache(db_session, BROKEN_CHAIN_FULL, move_uci="d4d5")
    _assert_stale(db_session, digest)


# ---------------------------------------------------------------------------
# Version / registry surfaces (O(1) coverage, no digest on the verdict path)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version_attr", [
    "OPENING_EVIDENCE_INPUTS_VERSION",
    "FRESHNESS_CONTRACT_VERSION",
])
def test_version_bump_stales_without_digest(db_session, monkeypatch, version_attr):
    # Scenario 13: either version bump changes registry_fingerprint -> stale,
    # and the verdict path must not touch the raw digest while deciding.
    _seed_base_evidence(db_session)
    _build_batch(db_session)

    monkeypatch.setattr(f"app.opening_cache.{version_attr}", "bumped-vNEXT")
    batch, rows = list_cached_opening_scores(db_session, USER, "white")
    fail = Mock(side_effect=AssertionError("raw digest ran on the verdict path"))
    with (
        patch("app.opening_cache.raw_evidence_inputs_digest", fail),
        patch("app.opening_cache.raw_evidence_inputs_snapshot", fail),
    ):
        assert _is_batch_fresh(db_session, batch, rows) is False
    fail.assert_not_called()


def test_registry_change_stales(db_session, monkeypatch):
    # Scenario 14: any registry surface (here the score-model version) -> stale.
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    monkeypatch.setattr("app.opening_cache.SCORE_MODEL_VERSION", "sm-vNEXT")
    batch, rows = list_cached_opening_scores(db_session, USER, "white")
    assert _is_batch_fresh(db_session, batch, rows) is False


def test_singleton_cannot_be_deleted_or_moved_non_increasing(db_session):
    before = current_cache_epoch(db_session)
    assert before is not None

    with pytest.raises(IntegrityError):
        db_session.execute(text("DELETE FROM evidence_epoch WHERE id = 1"))
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("UPDATE evidence_epoch SET value = value WHERE id = 1")
        )
    db_session.rollback()
    with pytest.raises(IntegrityError):
        db_session.execute(
            text("UPDATE evidence_epoch SET value = value - 1 WHERE id = 1")
        )
    db_session.rollback()

    db_session.execute(
        text("UPDATE evidence_epoch SET value = value + 5 WHERE id = 1")
    )
    db_session.commit()
    assert current_cache_epoch(db_session) == before + 5


def test_unstamped_batch_is_stale_not_error(db_session):
    _seed_base_evidence(db_session)
    _build_batch(db_session)
    batch, rows = list_cached_opening_scores(db_session, USER, "white")

    # Pre-migration / corrupt batch: NULL signal -> not provable.
    batch.evidence_seq = None
    db_session.commit()
    assert _is_batch_fresh(db_session, batch, rows) is False



# ---------------------------------------------------------------------------
# Counter mechanics
# ---------------------------------------------------------------------------

def test_bump_and_reserve_do_not_clobber_each_other(db_session):
    # Both upserts hit the same (user,color) composite-PK row from independent
    # transactions; each DO-UPDATE must advance ONLY its own column.
    _insert_user(db_session)
    bump_evidence_seq(db_session, USER, "white")
    db_session.commit()
    assert reserve_opening_score_generation(db_session, USER, "white") == 1
    bump_evidence_seq(db_session, USER, "white")
    db_session.commit()
    assert reserve_opening_score_generation(db_session, USER, "white") == 2

    row = db_session.execute(text(
        "SELECT evidence_seq, latest_generation FROM opening_score_cursors"
        " WHERE user_id = :u AND player_color = 'white'"), {"u": USER}
    ).fetchone()
    assert (row.evidence_seq, row.latest_generation) == (2, 2)


def test_bumps_from_independent_sessions_compose(db_session):
    # Two bumps from independent sessions never collapse into one advance: the
    # increment is an in-DB column expression, so +1 applied twice is +2.
    _insert_user(db_session)
    s1, s2 = TestingSessionLocal(), TestingSessionLocal()
    try:
        bump_evidence_seq(s1, USER, "white")
        s1.commit()
        bump_evidence_seq(s2, USER, "white")
        s2.commit()
    finally:
        s1.close()
        s2.close()
    assert current_evidence_seq(db_session, USER, "white") == 2


# ---------------------------------------------------------------------------
# Randomized differential loop — the strongest no-false-positive guarantee
# ---------------------------------------------------------------------------

def test_randomized_differential_loop(db_session):
    """Apply a random mutation sequence; after each step assert
    fresh ⟹ overlay and score outputs unchanged. Per-user mutations pair the
    write with the exact production bump rule (this exercises the SIGNAL math;
    endpoint wiring is covered scenario-by-scenario above)."""
    _seed_base_evidence(db_session)
    _build_batch(db_session)

    rng = random.Random(1337)
    board_fens = [START_FULL, KINGS_PAWN_FULL, OPEN_GAME_FULL, NON_CANDIDATE_FULL]
    counters = {"move": 1, "ac": 0}

    def op_add_eligible_move():
        sid = _insert_session(db_session)
        counters["move"] += 1
        _insert_move(db_session, sid, 1, "white", "e4", START_FULL,
                     KINGS_PAWN_FULL, eval_delta=counters["move"])
        bump_evidence_seq(db_session, USER, "white")  # production S1 rule
        db_session.commit()

    def op_add_active_move():
        sid = _insert_session(db_session, status="active")
        _insert_move(db_session, sid, 1, "white", "e4", START_FULL,
                     KINGS_PAWN_FULL, eval_delta=7)
        # No bump: active sessions are digest-invisible (S1 gate).

    def op_shared_insert():
        counters["ac"] += 1
        # Upsert form: repeated (fen, uci) pairs replace in place instead of
        # violating the unique key — both INSERT and UPDATE fire the triggers.
        db_session.execute(text("""
            INSERT INTO analysis_cache (fen_before, move_uci, move_san, played_eval)
            VALUES (:fb, :mu, 'x', :pe)
            ON CONFLICT(fen_before, move_uci)
            DO UPDATE SET played_eval = excluded.played_eval
        """), {
            "fb": rng.choice(board_fens),
            "mu": f"a2a{(counters['ac'] % 6) + 3}",
            "pe": counters["ac"],
        })
        db_session.commit()

    def op_shared_update():
        db_session.execute(text(
            "UPDATE analysis_cache SET played_eval = :pe"
            " WHERE id = (SELECT id FROM analysis_cache ORDER BY id DESC LIMIT 1)"
        ), {"pe": rng.randrange(1000)})
        db_session.commit()

    def op_shared_delete():
        db_session.execute(text(
            "DELETE FROM analysis_cache"
            " WHERE id = (SELECT id FROM analysis_cache ORDER BY id LIMIT 1)"
        ))
        db_session.commit()

    def op_rebuild():
        _build_batch(db_session)

    ops = [op_add_eligible_move, op_add_active_move, op_shared_insert,
           op_shared_update, op_shared_delete, op_rebuild]

    for _ in range(40):
        rng.choice(ops)()
        db_session.expire_all()
        _check_sound(db_session, USER, "white")
