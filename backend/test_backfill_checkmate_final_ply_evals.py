"""Backfill of historical checkmate final-ply evals (g-eh2w).

Drives :mod:`app.checkmate_final_ply_backfill` on the conftest in-memory SQLite engine
via a ``sessionmaker(bind=engine)`` factory — the exact production path (Phase A read
session, per-group Phase B write session, commit/rollback/close all owned by the
module). Fixtures are REAL checkmate games replayed through python-chess, so every
``fen_before`` / ``move_san`` / ``fen_after`` is genuine and the verification replay is
exercised for real.

Two mates cover both delivering colors:

* Scholar's mate — WHITE delivers mate on the final ply (4. Qxf7#). As the white
  player it is a checkmate WIN (the null final ply is the player's own move ->
  moved_off_none); as the black player it is a checkmate LOSS (the null final ply is
  the opponent's move -> repaired_accuracy_already_non_null).
* Fool's mate — BLACK delivers mate on the final ply (2... Qh4#), and its final move
  shares move_number 2 with white's 2. g4, so it also pins the final-ply selection
  (black is chosen over white within the same move_number).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from unittest.mock import MagicMock

import chess
import chess.pgn
from sqlalchemy import case, event

from app.accuracy import ACCURACY_ALGO_VERSION
from app.checkmate_final_ply_backfill import (
    MATE_EVAL_CP,
    _begin_readonly_snapshot,
    apply_backfill,
    plan_backfill,
    run_backfill,
)
from app.models import GameSession, OpeningScoreCursor, SessionMove
from conftest import TestingSessionLocal, engine, pg_required

_factory = TestingSessionLocal

# White delivers mate on the 7th ply (index 6): 4. Qxf7#.
SCHOLAR = ["e4", "e5", "Bc4", "Nc6", "Qh5", "Nf6", "Qxf7#"]
# Black delivers mate on the 4th ply (index 3): 2... Qh4#.
FOOL = ["f3", "e5", "g4", "Qh4#"]
# A non-mating game whose final ply (2... e5) is NOT checkmate.
NON_MATE = ["e4", "e5"]


# ---------------------------------------------------------------------------
# Fixtures: real games replayed through python-chess.
# ---------------------------------------------------------------------------
def _play(sans: list[str]):
    """Replay SAN moves, returning (per-ply dicts, movetext PGN). Each ply carries the
    true fen_before / canonical move_san / fen_after."""
    board = chess.Board()
    plies = []
    for i, san in enumerate(sans):
        fen_before = board.fen()
        move = board.parse_san(san)
        canonical = board.san(move)
        board.push(move)
        plies.append(
            {
                "move_number": i // 2 + 1,
                "color": "white" if i % 2 == 0 else "black",
                "move_san": canonical,
                "fen_before": fen_before,
                "fen_after": board.fen(),
            }
        )
    game = chess.pgn.Game.from_board(board)
    exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    pgn = game.accept(exporter).strip()
    return plies, pgn


def _insert_game(
    db,
    *,
    plies,
    pgn,
    user_id=1,
    player_color="white",
    result="checkmate_win",
    status="ended",
    non_final_eval=20,
    final_overrides=None,
):
    """Insert a GameSession + its SessionMove rows. Non-final plies get a present
    eval_cp so the only gap is the final ply (both eval fields NULL by default), which
    ``final_overrides`` can further customize."""
    session_id = uuid.uuid4()
    db.add(
        GameSession(
            id=session_id,
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status=status,
            result=result,
            engine_elo=1500,
            player_color=player_color,
            session_mode="normal",
            is_rated=True,
            pgn=pgn,
        )
    )
    last = len(plies) - 1
    for idx, p in enumerate(plies):
        is_final = idx == last
        move = SessionMove(
            session_id=session_id,
            move_number=p["move_number"],
            color=p["color"],
            move_san=p["move_san"],
            fen_after=p["fen_after"],
            fen_before=p["fen_before"],
            eval_cp=None if is_final else non_final_eval,
            eval_mate=None,
            eval_delta=None,
        )
        if is_final and final_overrides:
            for k, v in final_overrides.items():
                setattr(move, k, v)
        db.add(move)
    db.commit()
    return session_id


def _seed(fn):
    """Run ``fn(db)`` on a seed session bound to the shared test engine."""
    db = TestingSessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def _final_move(db, session_id) -> SessionMove:
    color_order = case((SessionMove.color == "white", 0), else_=1)
    return (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.desc(), color_order.desc())
        .first()
    )


def _cursor(db, user_id, color):
    return (
        db.query(OpeningScoreCursor)
        .filter(
            OpeningScoreCursor.user_id == user_id,
            OpeningScoreCursor.player_color == color,
        )
        .one_or_none()
    )


# ---------------------------------------------------------------------------
# Portable final-ply selection.
# ---------------------------------------------------------------------------
def test_final_ply_selection_picks_black_over_white_same_move_number(db_session):
    """The row_number() selection picks the LAST ply — Fool's mate's 2... Qh4# (black)
    over 2. g4 (white), which share move_number 2."""
    plies, pgn = _play(FOOL)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, player_color="black",
                       result="checkmate_win")

    from app.checkmate_final_ply_backfill import _final_ply_rows

    reader = TestingSessionLocal()
    try:
        rows = _final_ply_rows(reader, session_id=None)
    finally:
        reader.close()

    assert len(rows) == 1
    assert rows[0].session_id == sid
    # Qh4# is black's mating move; the same-move-number white alternative was g4. The
    # selection picking Qh4# proves black (the later ply) ranks over white.
    assert rows[0].move_san == "Qh4#"


# ---------------------------------------------------------------------------
# Shared both-fields-null missing-eval predicate.
# ---------------------------------------------------------------------------
def test_normal_centipawn_final_ply_is_not_a_candidate(db_session):
    """A final ply carrying a present eval_cp (eval_mate NULL) is NOT both-fields-null,
    so it is neither selected nor counted as a gap."""
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn,
                 final_overrides={"eval_cp": 10000, "eval_mate": None, "eval_delta": 0})

    report = run_backfill(_factory)

    assert report.sizing.total_checkmate_sessions == 1
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0
    assert report.outcome.evidence_groups_bumped_actual == 0


# ---------------------------------------------------------------------------
# Verified checkmate -> fill +10000 / 0 / 0 and bump evidence.
# ---------------------------------------------------------------------------
def test_verified_checkmate_fills_and_bumps(db_session):
    plies, pgn = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=7,
                       player_color="white", result="checkmate_win")

    report = run_backfill(_factory)

    assert report.sizing.moved_off_none == 1
    assert report.outcome.rows_filled_actual == 1
    assert report.outcome.evidence_groups_bumped_actual == 1

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_mate == 0
        assert move.eval_cp == MATE_EVAL_CP
        assert move.eval_delta == 0
        cursor = _cursor(check, 7, "white")
        assert cursor is not None and cursor.evidence_seq == 1
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Rejection paths (rows_rejected_verification): never written.
# ---------------------------------------------------------------------------
def test_non_checkmate_final_ply_rejected(db_session):
    plies, pgn = _play(NON_MATE)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, result="checkmate_win")

    report = run_backfill(_factory)

    assert report.sizing.final_ply_missing_eval == 1
    assert report.sizing.rows_rejected_verification == 1
    assert report.sizing.moved_off_none == 0
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 0
    assert report.outcome.evidence_groups_bumped_actual == 0

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp is None and move.eval_mate is None
    finally:
        check.close()


def test_fen_mismatch_rejected(db_session):
    """A genuine placement difference between the replayed board and the stored
    fen_after (not just clocks) is rejected."""
    plies, pgn = _play(SCHOLAR)
    wrong_fen = chess.Board().fen()  # start position — a real placement mismatch
    sid = _insert_game(db_session, plies=plies, pgn=pgn,
                       final_overrides={"fen_after": wrong_fen})

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0

    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_malformed_san_rejected(db_session):
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn,
                 final_overrides={"move_san": "zzz"})

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0


# ---------------------------------------------------------------------------
# Normalized-FEN acceptance: clock-only differences do not reject.
# ---------------------------------------------------------------------------
def test_clock_only_fen_difference_accepted(db_session):
    """A stored fen_after differing from the replayed board ONLY in the halfmove-clock
    / fullmove-number fields (5-6) is accepted (normalize_fen strips them)."""
    plies, pgn = _play(SCHOLAR)
    real = plies[-1]["fen_after"]
    fields = real.split(" ")
    fields[4] = "50"   # halfmove clock
    fields[5] = "99"   # fullmove number
    clock_variant = " ".join(fields)
    assert clock_variant != real
    sid = _insert_game(db_session, plies=plies, pgn=pgn,
                       final_overrides={"fen_after": clock_variant})

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 0
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_mate == 0
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Sizing goes through the GUARDED accuracy path, both variants.
# ---------------------------------------------------------------------------
def test_sizing_uses_the_guarded_accuracy_path(db_session, monkeypatch):
    """The Phase A forecast must measure through app.accuracy.game_accuracy_for_rows — the
    same guarded path Phase B's recompute_session_accuracy persists — for BOTH the before
    and after variants. Raw compute_game_accuracy can score a malformed coordinate grid 95
    where the persisted path returns None, which would let the forecast diverge from the
    outcome it forecasts.
    """
    import app.checkmate_final_ply_backfill as mod

    real = mod.game_accuracy_for_rows
    calls: list[list] = []

    def spy(rows, player_color, expected_total_moves, *, session_id=None):
        calls.append(list(rows))
        return real(rows, player_color, expected_total_moves, session_id=session_id)

    monkeypatch.setattr(mod, "game_accuracy_for_rows", spy)
    # The raw entry point is not even reachable from this module's namespace, so sizing
    # cannot drift onto it later without this failing.
    assert not hasattr(mod, "compute_game_accuracy")

    plies, pgn = _play(SCHOLAR)   # white delivers mate; scored as the white player
    sid = _insert_game(db_session, plies=plies, pgn=pgn, player_color="white")

    reader = TestingSessionLocal()
    try:
        plan = plan_backfill(reader)
    finally:
        reader.rollback()
        reader.close()

    assert plan.report.moved_off_none == 1
    # Exactly two guarded measurements for the one candidate: before, then after.
    assert len(calls) == 2
    before_rows, after_rows = calls
    assert before_rows[-1].eval_cp is None          # the candidate, unfilled
    assert before_rows[-1].eval_mate is None
    assert after_rows[-1].eval_cp == MATE_EVAL_CP   # the candidate, filled in memory only
    assert after_rows[-1].eval_mate == 0
    # The in-memory "after" fill never touched the DB.
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_shifted_ply_coordinates_forecast_no_repair(db_session):
    """A shifted move_number grid fails the guarded coordinate check, so BOTH sizing
    variants return None and the candidate lands in residual_remains_none — never in
    moved_off_none, which raw compute_game_accuracy would have produced."""
    plies, pgn = _play(SCHOLAR)
    shifted = [dict(p, move_number=p["move_number"] + 1) for p in plies]
    _insert_game(db_session, plies=shifted, pgn=pgn, player_color="white")

    s = run_backfill(_factory).sizing

    assert s.moved_off_none == 0
    assert s.residual_remains_none == 1
    assert s.reconciles()


# ---------------------------------------------------------------------------
# Sizing buckets, measured via the guarded accuracy path before/after.
# ---------------------------------------------------------------------------
def test_bucket_repaired_white_delivers_mate_loss(db_session):
    """Scholar's mate as the BLACK player: a checkmate LOSS whose null final ply is
    white's mating move. Player accuracy is non-null both before and after -> the row
    is filled but the game never moved off None."""
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn, player_color="black",
                 result="checkmate_loss")

    report = run_backfill(_factory)

    assert report.sizing.moved_off_none == 0
    assert report.sizing.repaired_accuracy_already_non_null == 1
    assert report.sizing.residual_remains_none == 0
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 1


def test_bucket_repaired_black_delivers_mate_loss(db_session):
    """Fool's mate as the WHITE player: a checkmate LOSS whose null final ply is
    black's mating move — the mirror direction of the loss shape."""
    plies, pgn = _play(FOOL)
    _insert_game(db_session, plies=plies, pgn=pgn, player_color="white",
                 result="checkmate_loss")

    report = run_backfill(_factory)

    assert report.sizing.repaired_accuracy_already_non_null == 1
    assert report.sizing.moved_off_none == 0
    assert report.outcome.rows_filled_actual == 1


def test_bucket_residual_unparseable_pgn(db_session):
    """With no PGN the expected ply count is unknown, so accuracy stays None both before
    and after — the row is still filled (data integrity) but the game stays residual."""
    plies, _ = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=None, player_color="white",
                       result="checkmate_win")

    report = run_backfill(_factory)

    assert report.sizing.residual_remains_none == 1
    assert report.sizing.moved_off_none == 0
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_mate == 0  # still filled
    finally:
        check.close()


def test_mate0_persisted_reported_not_a_candidate(db_session):
    """A final ply whose eval_mate is already 0 is reported for awareness but is not a
    both-fields-null candidate and is not rewritten."""
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn,
                 final_overrides={"eval_mate": 0, "eval_cp": MATE_EVAL_CP, "eval_delta": 0})

    report = run_backfill(_factory)

    assert report.sizing.mate0_persisted == 1
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0


# ---------------------------------------------------------------------------
# Reconciliation + actual-vs-forecast over a mixed cohort.
# ---------------------------------------------------------------------------
def test_mixed_cohort_reconciles_and_reports_actuals(db_session):
    scholar, scholar_pgn = _play(SCHOLAR)
    fool, fool_pgn = _play(FOOL)
    non_mate, non_mate_pgn = _play(NON_MATE)

    def seed(db):
        _insert_game(db, plies=scholar, pgn=scholar_pgn, user_id=1,
                     player_color="white", result="checkmate_win")   # moved_off_none
        _insert_game(db, plies=scholar, pgn=scholar_pgn, user_id=1,
                     player_color="black", result="checkmate_loss")  # repaired
        _insert_game(db, plies=fool, pgn=fool_pgn, user_id=2,
                     player_color="white", result="checkmate_loss")  # repaired
        _insert_game(db, plies=scholar, pgn=None, user_id=3,
                     player_color="white", result="checkmate_win")   # residual
        _insert_game(db, plies=non_mate, pgn=non_mate_pgn, user_id=4,
                     player_color="white", result="checkmate_win")   # rejected

    _seed(seed)

    report = run_backfill(_factory)
    s, o = report.sizing, report.outcome

    assert s.total_checkmate_sessions == 5
    assert s.final_ply_missing_eval == 5
    assert s.moved_off_none == 1
    assert s.repaired_accuracy_already_non_null == 2
    assert s.residual_remains_none == 1
    assert s.rows_rejected_verification == 1
    assert s.mate0_persisted == 0
    assert s.reconciles()

    # Actuals: the 4 verified checkmates were written across 4 distinct groups; the
    # rejected NON_MATE session (user 4) wrote nothing, so its group did not bump.
    assert o.rows_filled_actual == 4
    assert o.evidence_groups_bumped_actual == 4
    # Under no concurrency the actual equals the sum of the three written buckets.
    assert o.rows_filled_actual == (
        s.moved_off_none + s.repaired_accuracy_already_non_null + s.residual_remains_none
    )

    check = TestingSessionLocal()
    try:
        assert _cursor(check, 4, "white") is None  # rejected-only group never bumped
        assert _cursor(check, 1, "white").evidence_seq == 1
        assert _cursor(check, 1, "black").evidence_seq == 1
    finally:
        check.close()


# ---------------------------------------------------------------------------
# --dry-run, --session-id, idempotency.
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(db_session):
    plies, pgn = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=9,
                       player_color="white", result="checkmate_win")

    report = run_backfill(_factory, dry_run=True)

    # Reports what it WOULD write, but the group transaction was rolled back.
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp is None and move.eval_mate is None and move.eval_delta is None
        assert _cursor(check, 9, "white") is None  # bump rolled back with the writes
    finally:
        check.close()


def test_session_id_scopes_to_one_session(db_session):
    plies, pgn = _play(SCHOLAR)

    def seed(db):
        a = _insert_game(db, plies=plies, pgn=pgn, user_id=1, player_color="white",
                         result="checkmate_win")
        b = _insert_game(db, plies=plies, pgn=pgn, user_id=2, player_color="white",
                         result="checkmate_win")
        return a, b

    target, other = _seed(seed)

    report = run_backfill(_factory, session_id=target)

    assert report.sizing.total_checkmate_sessions == 1
    assert report.outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        assert _final_move(check, target).eval_mate == 0        # filled
        assert _final_move(check, other).eval_cp is None        # untouched
    finally:
        check.close()


def test_idempotent_second_run_is_noop(db_session):
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn, user_id=5, player_color="white",
                 result="checkmate_win")

    first = run_backfill(_factory)
    assert first.outcome.rows_filled_actual == 1

    second = run_backfill(_factory)
    assert second.sizing.final_ply_missing_eval == 0
    assert second.outcome.rows_filled_actual == 0
    assert second.outcome.evidence_groups_bumped_actual == 0

    check = TestingSessionLocal()
    try:
        assert _cursor(check, 5, "white").evidence_seq == 1  # not bumped again
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Between-phase predicate re-check (SQLite): a candidate resolved by a concurrent
# /moves retry AFTER Phase A and BEFORE Phase B is dropped, never overwritten.
#
# NAMING/SCOPE: this exercises the both-fields-null PREDICATE re-check between phases
# on SQLite, NOT PostgreSQL SELECT ... FOR UPDATE row-lock behavior (SQLite renders FOR
# UPDATE as a no-op and the tests are single-threaded). True concurrent row-lock
# behavior would need a PostgreSQL integration test and is out of scope here.
# ---------------------------------------------------------------------------
def test_between_phase_resolution_drops_candidate(db_session):
    plies, pgn = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=11,
                       player_color="white", result="checkmate_win")

    # Phase A: select + close the read session (ends the read transaction).
    reader = TestingSessionLocal()
    try:
        plan = plan_backfill(reader)
    finally:
        reader.rollback()
        reader.close()
    assert plan.groups.get((11, "white"))  # the candidate was captured

    # Simulate a concurrent /moves retry populating the REAL worker eval on the
    # candidate between Phase A and Phase B.
    retry = TestingSessionLocal()
    try:
        move = _final_move(retry, sid)
        move.eval_cp = 250
        move.eval_mate = None
        retry.commit()
    finally:
        retry.close()

    # Phase B re-applies the both-fields-null predicate: the resolved row is dropped.
    outcome = apply_backfill(_factory, plan, dry_run=False)

    assert outcome.rows_filled_actual == 0
    assert outcome.evidence_groups_bumped_actual == 0

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp == 250        # real worker value preserved, NOT overwritten
        assert move.eval_mate is None
        assert move.eval_delta is None
        assert _cursor(check, 11, "white") is None  # no write -> no evidence bump
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Review fix #1: repair also refreshes the cached game_sessions.player_accuracy so a
# Release-A-stamped session is never left cache-stale.
# ---------------------------------------------------------------------------
def test_repair_updates_stale_cached_accuracy(db_session):
    """A checkmate WIN that Release A already stamped (algo version current,
    player_accuracy=None because the final ply was still null) has its cached accuracy
    refreshed to a real value by the repair — not left null for the Release B read
    switch to serve forever."""
    plies, pgn = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=1, player_color="white",
                       result="checkmate_win")
    gs = db_session.query(GameSession).filter(GameSession.id == sid).one()
    gs.player_accuracy = None
    gs.player_accuracy_algo_version = ACCURACY_ALGO_VERSION
    db_session.commit()

    report = run_backfill(_factory)
    assert report.outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        gs = check.query(GameSession).filter(GameSession.id == sid).one()
        assert gs.player_accuracy is not None                      # no longer stale-null
        assert gs.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        check.close()


def test_residual_repair_stamps_none_accuracy(db_session):
    """A residual session (no PGN -> genuinely unscoreable) still gets its cached
    accuracy recomputed and stamped (None, correctly) — the repaired row's session is
    never left unstamped/stale."""
    plies, _ = _play(SCHOLAR)
    sid = _insert_game(db_session, plies=plies, pgn=None, user_id=1, player_color="white",
                       result="checkmate_win")

    report = run_backfill(_factory)
    assert report.sizing.residual_remains_none == 1
    assert report.outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        gs = check.query(GameSession).filter(GameSession.id == sid).one()
        assert gs.player_accuracy is None
        assert gs.player_accuracy_algo_version == ACCURACY_ALGO_VERSION  # recompute ran
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Review fix #2: the evidence cursor upsert is the transaction's final write, after the
# move + accuracy writes have flushed (cursor pure sink, SPEC 7.4).
# ---------------------------------------------------------------------------
def test_cursor_upsert_is_last_write(db_session):
    plies, pgn = _play(SCHOLAR)
    _insert_game(db_session, plies=plies, pgn=pgn, user_id=1, player_color="white",
                 result="checkmate_win")

    captured: list[str] = []

    def _on_cursor(conn, cursor, statement, params, context, executemany):
        captured.append(statement.lower())

    event.listen(engine, "before_cursor_execute", _on_cursor)
    try:
        run_backfill(_factory)   # seed already happened before the listener attached
    finally:
        event.remove(engine, "before_cursor_execute", _on_cursor)

    writes = [s for s in captured if s.lstrip().startswith(("insert", "update", "delete"))]
    move_updates = [i for i, s in enumerate(writes)
                    if s.lstrip().startswith("update") and "session_moves" in s]
    acc_updates = [i for i, s in enumerate(writes)
                   if s.lstrip().startswith("update") and "game_sessions" in s]
    cursor_writes = [i for i, s in enumerate(writes) if "opening_score_cursors" in s]

    assert move_updates, "expected a session_moves fill UPDATE"
    assert acc_updates, "expected a game_sessions accuracy UPDATE"
    assert cursor_writes, "expected an opening_score_cursors upsert"
    # Move + accuracy writes both precede the cursor bump, which is the LAST write.
    assert max(move_updates) < cursor_writes[-1]
    assert max(acc_updates) < cursor_writes[-1]
    assert cursor_writes[-1] == len(writes) - 1


# ---------------------------------------------------------------------------
# Review fix #3: Phase A runs a REPEATABLE READ read-only snapshot on PostgreSQL,
# no-op on SQLite.
# ---------------------------------------------------------------------------
def test_phase_a_begins_repeatable_read_on_postgres():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"

    _begin_readonly_snapshot(session)

    session.connection.assert_called_once_with(
        execution_options={
            "isolation_level": "REPEATABLE READ",
            "postgresql_readonly": True,
        }
    )


def test_phase_a_snapshot_is_noop_on_sqlite():
    session = MagicMock()
    session.get_bind.return_value.dialect.name = "sqlite"

    _begin_readonly_snapshot(session)

    session.connection.assert_not_called()


# ---------------------------------------------------------------------------
# Review fix #4: the cohort total counts checkmate sessions with zero stored moves.
# ---------------------------------------------------------------------------
def test_zero_move_checkmate_session_counted_in_cohort(db_session):
    sid = uuid.uuid4()
    db_session.add(
        GameSession(
            id=sid, user_id=1, started_at=datetime.now(timezone.utc), status="ended",
            result="checkmate_win", engine_elo=1500, player_color="white",
            session_mode="normal", is_rated=True, pgn=None,
        )
    )
    db_session.commit()

    report = run_backfill(_factory)

    assert report.sizing.total_checkmate_sessions == 1   # counted despite zero moves
    assert report.sizing.final_ply_missing_eval == 0     # no final ply -> no candidate
    assert report.outcome.rows_filled_actual == 0


# ---------------------------------------------------------------------------
# Real PostgreSQL: exercises what SQLite cannot — the Phase A REPEATABLE READ read-only
# snapshot, the parent-session FOR NO KEY UPDATE lock, and the cached-accuracy recompute
# on the alembic-migrated schema. @pg_required skips cleanly without GHOSTREPLAY_TEST_PG_URL.
# ---------------------------------------------------------------------------
@pg_required
def test_pg_run_recomputes_accuracy_and_bumps_under_real_locks(pg_engine, pg_session_factory):
    from sqlalchemy import text

    from app.models import Base, User

    preserved = {"evidence_epoch", "shared_evidence_scope_invalidations"}
    table_names = ", ".join(
        table.name
        for table in reversed(Base.metadata.sorted_tables)
        if table.name not in preserved
    )
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

    scholar, s_pgn = _play(SCHOLAR)
    seed = pg_session_factory()
    try:
        seed.add(User(id=1, username=None, is_anonymous=True))
        seed.commit()
        win = _insert_game(seed, plies=scholar, pgn=s_pgn, user_id=1,
                           player_color="white", result="checkmate_win")   # moved_off_none
        loss = _insert_game(seed, plies=scholar, pgn=s_pgn, user_id=1,
                            player_color="black", result="checkmate_loss")  # repaired
    finally:
        seed.close()

    report = run_backfill(pg_session_factory)

    assert report.sizing.total_checkmate_sessions == 2
    assert report.sizing.moved_off_none == 1
    assert report.sizing.repaired_accuracy_already_non_null == 1
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 2
    assert report.outcome.evidence_groups_bumped_actual == 2

    check = pg_session_factory()
    try:
        w = check.query(GameSession).filter(GameSession.id == win).one()
        assert w.player_accuracy is not None                       # recompute ran on PG
        assert w.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
        assert _final_move(check, win).eval_mate == 0

        lo = check.query(GameSession).filter(GameSession.id == loss).one()
        assert lo.player_accuracy is not None
        assert _final_move(check, loss).eval_mate == 0

        assert _cursor(check, 1, "white").evidence_seq == 1
        assert _cursor(check, 1, "black").evidence_seq == 1
    finally:
        check.close()
