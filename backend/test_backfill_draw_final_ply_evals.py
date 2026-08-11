"""Backfill of historical draw final-ply evals (g-c60b).

Drives :mod:`app.draw_final_ply_backfill` on the conftest in-memory SQLite engine via a
``sessionmaker(bind=engine)`` factory — the exact production path (Phase A read session,
per-group Phase B write session, commit/rollback/close all owned by the module).

Every fixture is a REAL game replayed through python-chess FROM THE STANDARD START, so
each ``fen_before`` / ``move_san`` / ``fen_after`` is genuine and the full-chain
verification replay is exercised for real. That start is not incidental: threefold
repetition and the fifty-move rule are history-dependent, so a fixture replayed from a
mid-game FEN could not exercise the verifier's actual contract.

The four draw kinds:

* **Stalemate** — Sam Loyd's 10-move stalemate (white's 10. Qe6 stalemates black).
* **Insufficient material** — a capture-greedy game searched down to bare kings.
* **Fifty-move** — a generated knight shuffle: 100 halfmoves with no pawn move and no
  capture, so the replayed clock (never a stored one) reaches 100.
* **Threefold** — a knight shuffle returning to the start position 3 times, the start
  counting as the first occurrence.

The shuffles are built by a small seeded python-chess search rather than hardcoded, and
each builder ASSERTS the property it claims, so a python-chess behavior drift fails loudly
here instead of silently weakening the fixture.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timezone

import chess
import chess.pgn
import pytest
from sqlalchemy import case

from app.accuracy import ACCURACY_ALGO_VERSION
from app.draw_final_ply_backfill import (
    _is_fifty_moves,
    _is_terminal_draw,
    apply_backfill,
    plan_backfill,
    run_backfill,
    verify_terminal_draw,
)
from app.models import GameSession, OpeningScoreCursor, SessionMove
from conftest import TestingSessionLocal, engine, pg_required

_factory = TestingSessionLocal

# White's 10. Qe6 stalemates black (Sam Loyd, 1889) — 19 plies from the standard start.
STALEMATE = ["e3", "a5", "Qh5", "Ra6", "Qxa5", "h5", "Qxc7", "Rah6", "h4", "f6",
             "Qxd7+", "Kf7", "Qxb7", "Qd3", "Qxb8", "Qh7", "Qxc8", "Kg6", "Qe6"]
# Black mates on the 4th ply (2... Qh4#) — a checkmate, never a draw.
FOOL = ["f3", "e5", "g4", "Qh4#"]


# ---------------------------------------------------------------------------
# Fixture builders: real games replayed through python-chess.
# ---------------------------------------------------------------------------
def _play(sans: list[str]):
    """Replay SAN moves from the STANDARD START, returning (per-ply dicts, movetext PGN).
    Each ply carries the true fen_before / canonical move_san / fen_after."""
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
    return plies, game.accept(exporter).strip()


def _build_shuffle(halfmoves: int, *, seed: int = 7) -> list[str]:
    """A knight-only shuffle from the standard start: no pawn move, no capture, no check,
    and never a third repetition — so the replayed halfmove clock reaches ``halfmoves``
    with no earlier terminality.

    Generated, not hardcoded: the start is pinned to STARTING_FEN, so there is no seeded
    halfmove clock to lean on and the clock must be earned by 100 real reversible plies.
    """
    rng = random.Random(seed)
    board = chess.Board()
    sans: list[str] = []
    while len(sans) < halfmoves:
        candidates = [
            m for m in board.legal_moves
            if board.piece_at(m.from_square).piece_type == chess.KNIGHT
            and not board.is_capture(m)
        ]
        rng.shuffle(candidates)
        for move in candidates:
            san = board.san(move)
            board.push(move)
            if not board.is_check() and not board.is_repetition(3):
                sans.append(san)
                break
            board.pop()
        else:
            raise AssertionError(f"shuffle stuck after {len(sans)} plies")
    assert board.halfmove_clock == halfmoves
    return sans


def _build_insufficient_material(*, seed: int = 0) -> list[str]:
    """A capture-greedy game from the standard start searched down to insufficient
    material (bare kings), rejecting any move that would make an INTERMEDIATE position
    terminal — so the only terminal position in the chain is the last one."""
    rng = random.Random(seed)
    board = chess.Board()
    sans: list[str] = []
    while not board.is_insufficient_material():
        legal = list(board.legal_moves)
        captures = [m for m in legal if board.is_capture(m)]
        pool = captures or legal
        rng.shuffle(pool)
        for move in pool:
            san = board.san(move)
            board.push(move)
            terminal = (
                board.is_checkmate() or board.is_stalemate()
                or board.is_fifty_moves() or board.is_repetition(3)
            )
            if not terminal or board.is_insufficient_material():
                sans.append(san)
                break
            board.pop()
        else:
            raise AssertionError(f"capture search stuck after {len(sans)} plies")
    assert board.is_insufficient_material()
    return sans


def _threefold_sans(pairs: int) -> list[str]:
    """``pairs`` repetitions of the Nf3/Nf6/Ng1/Ng8 shuffle, each pair returning the board
    to the standard start. The start position counts as the FIRST occurrence, so 2 pairs
    (8 plies) is the third occurrence — a terminal threefold — and 1 pair (4 plies) is only
    the second, a lookalike that is NOT terminal."""
    return ["Nf3", "Nf6", "Ng1", "Ng8"] * pairs


# The four verified draw kinds, each a full game from the standard start.
STALEMATE_SANS = STALEMATE
INSUFFICIENT_SANS = _build_insufficient_material()
FIFTY_MOVE_SANS = _build_shuffle(100)
# One ply short of the clock: the fifty-move boundary partner, still non-terminal.
FORTY_NINE_AND_A_HALF_SANS = FIFTY_MOVE_SANS[:99]
THREEFOLD_SANS = _threefold_sans(2)       # threefold lands on ply 8
THREEFOLD_LOOKALIKE_SANS = _threefold_sans(1)   # same placement, only 2 occurrences
THREEFOLD_EXTENDED_SANS = _threefold_sans(3)    # threefold at ply 8, chain runs to ply 12


def _insert_game(
    db,
    *,
    plies,
    pgn,
    user_id=1,
    player_color="white",
    result="draw",
    status="ended",
    session_mode="normal",
    drill_state=None,
    non_final_eval=20,
    final_overrides=None,
    row_slice=None,
):
    """Insert a GameSession + its SessionMove rows. Non-final plies get a present eval_cp
    so the only gap is the final ply (both eval fields NULL by default), which
    ``final_overrides`` can further customize. ``row_slice`` stores only a slice of the
    plies (keeping their ORIGINAL coordinates) to model a truncated row set."""
    session_id = uuid.uuid4()
    is_drill = session_mode == "drill"
    db.add(
        GameSession(
            id=session_id,
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
            status=status,
            result=result,
            engine_elo=1500,
            player_color=player_color,
            session_mode=session_mode,
            drill_state=drill_state,
            # A hidden (unconverted) drill is unrated with no rated_start_ply — the shape
            # the game_sessions mode/rating check constraint requires.
            is_rated=not is_drill,
            pgn=pgn,
        )
    )
    last = len(plies) - 1
    stored = plies if row_slice is None else plies[row_slice]
    for p in stored:
        is_final = p is plies[last]
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
            segment="drill" if is_drill else "normal",
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


def _last_ply_color(sans: list[str]) -> str:
    return "white" if (len(sans) - 1) % 2 == 0 else "black"


# ---------------------------------------------------------------------------
# The fixtures really are what they claim (guards against python-chess drift).
# ---------------------------------------------------------------------------
def test_fixtures_are_the_draws_they_claim():
    def final_board(sans):
        b = chess.Board()
        for s in sans:
            b.push_san(s)
        return b

    assert final_board(STALEMATE_SANS).is_stalemate()
    assert final_board(INSUFFICIENT_SANS).is_insufficient_material()
    assert final_board(FIFTY_MOVE_SANS).is_fifty_moves()
    assert final_board(THREEFOLD_SANS).is_repetition(3)
    # The boundary partner and the lookalike are NOT terminal — that is their whole point.
    partner = final_board(FORTY_NINE_AND_A_HALF_SANS)
    assert partner.halfmove_clock == 99 and not partner.is_fifty_moves()
    lookalike = final_board(THREEFOLD_LOOKALIKE_SANS)
    assert lookalike.board_fen() == chess.Board().board_fen()  # same placement...
    assert not lookalike.is_repetition(3)                      # ...only the 2nd occurrence


# ---------------------------------------------------------------------------
# Cohort: shared final-ply selection, result filter, visibility filter.
# ---------------------------------------------------------------------------
def test_final_ply_selection_picks_black_over_white_same_move_number(db_session):
    """The shared row_number() selection picks the LAST ply — the threefold's 4... Ng8
    (black) over 4. Ng1 (white), which share move_number 4."""
    plies, pgn = _play(THREEFOLD_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, player_color="black")

    from app.draw_final_ply_backfill import _DRAW_RESULTS
    from app.checkmate_final_ply_backfill import final_ply_rows
    from app.session_contracts import visible_session_filter

    reader = TestingSessionLocal()
    try:
        rows = final_ply_rows(reader, results=_DRAW_RESULTS, session_id=None,
                              visibility_filter=visible_session_filter())
    finally:
        reader.close()

    assert len(rows) == 1
    assert rows[0].session_id == sid
    assert rows[0].move_san == "Ng8"   # black's ply, not the same-move_number white Ng1


def test_checkmate_session_is_not_in_the_draw_cohort(db_session):
    """The result filter is 'draw' only: a checkmate-ended session (g-eh2w's cohort) is
    neither counted nor selected here."""
    plies, pgn = _play(FOOL)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, result="checkmate_win")

    report = run_backfill(_factory)

    assert report.sizing.total_draw_sessions == 0
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_hidden_natural_ended_drill_draw_excluded_and_counted(db_session):
    """A hidden (unconverted) natural-ended drill also carries result='draw', but
    recompute_session_accuracy intentionally no-ops for invisible sessions, so the symptom
    this backfill repairs does not exist for it. It is counted for awareness, never
    selected, never filled."""
    plies, pgn = _play(STALEMATE_SANS)
    hidden = _insert_game(db_session, plies=plies, pgn=pgn, user_id=3,
                          session_mode="drill", drill_state="abandoned")

    report = run_backfill(_factory)

    assert report.sizing.hidden_draw_sessions_excluded == 1
    assert report.sizing.total_draw_sessions == 0     # visible cohort only
    assert report.sizing.final_ply_missing_eval == 0  # never a candidate
    assert report.outcome.rows_filled_actual == 0

    check = TestingSessionLocal()
    try:
        move = _final_move(check, hidden)
        assert move.eval_cp is None and move.eval_mate is None
        assert _cursor(check, 3, "white") is None
    finally:
        check.close()


def test_converted_drill_draw_is_in_the_visible_cohort(db_session):
    """The visibility filter is visible_session_filter(), not session_mode == 'normal': a
    CONVERTED drill is a visible game and its draw is repaired."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = uuid.uuid4()
    db_session.add(
        GameSession(
            id=sid, user_id=4, started_at=datetime.now(timezone.utc), status="ended",
            result="draw", engine_elo=1500, player_color="white", session_mode="drill",
            drill_state="converted", is_rated=True,
            normal_started_at=datetime.now(timezone.utc),
            converted_at=datetime.now(timezone.utc), rated_start_ply=0, pgn=pgn,
        )
    )
    last = len(plies) - 1
    for idx, p in enumerate(plies):
        db_session.add(SessionMove(
            session_id=sid, move_number=p["move_number"], color=p["color"],
            move_san=p["move_san"], fen_before=p["fen_before"], fen_after=p["fen_after"],
            eval_cp=None if idx == last else 20, eval_mate=None, eval_delta=None,
        ))
    db_session.commit()

    report = run_backfill(_factory)

    assert report.sizing.total_draw_sessions == 1
    assert report.sizing.hidden_draw_sessions_excluded == 0
    assert report.outcome.rows_filled_actual == 1


def test_normal_centipawn_final_ply_is_not_a_candidate(db_session):
    """A final ply carrying a present eval_cp (eval_mate NULL) is NOT both-fields-null, so
    it is neither selected nor counted as a gap."""
    plies, pgn = _play(STALEMATE_SANS)
    _insert_game(db_session, plies=plies, pgn=pgn,
                 final_overrides={"eval_cp": 15, "eval_mate": None, "eval_delta": 5})

    report = run_backfill(_factory)

    assert report.sizing.total_draw_sessions == 1
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0


def test_final_ply_eval_cp_zero_reported_not_a_candidate(db_session):
    """A final ply already at eval_cp == 0 with eval_mate null — an indistinguishable mix
    of forward-fix fills and real 0.00 worker evals — is reported for awareness only, not
    treated as a gap and not rewritten."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn,
                       final_overrides={"eval_cp": 0, "eval_mate": None, "eval_delta": 7})

    report = run_backfill(_factory)

    assert report.sizing.final_ply_eval_cp_zero == 1
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_delta == 7   # untouched, not rewritten
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Verified fills: all four draw kinds -> eval_cp=0 / eval_mate=None / eval_delta=None.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sans,subtype",
    [
        (STALEMATE_SANS, "verified_stalemate"),
        (INSUFFICIENT_SANS, "verified_insufficient_material"),
        (FIFTY_MOVE_SANS, "verified_fifty_move"),
        (THREEFOLD_SANS, "verified_threefold"),
    ],
    ids=["stalemate", "insufficient_material", "fifty_move", "threefold"],
)
def test_verified_draw_fills_and_bumps(db_session, sans, subtype):
    plies, pgn = _play(sans)
    # Player owns the final ply, so the fill is what moves the game off accuracy=None.
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=7,
                       player_color=_last_ply_color(sans))

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 0
    assert report.sizing.moved_off_none == 1
    assert getattr(report.sizing, subtype) == 1
    assert report.outcome.rows_filled_actual == 1
    assert report.outcome.evidence_groups_bumped_actual == 1

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp == 0
        assert move.eval_mate is None
        assert move.eval_delta is None
        cursor = _cursor(check, 7, _last_ply_color(sans))
        assert cursor is not None and cursor.evidence_seq == 1
    finally:
        check.close()


def test_stale_eval_delta_is_cleared(db_session):
    """eval_delta is independently nullable, so a both-eval-fields-null candidate can carry
    a stale non-null delta. A draw does not prove the move was best, so the fill clears it
    explicitly — mirroring the forward fix."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn,
                       final_overrides={"eval_delta": 250})

    report = run_backfill(_factory)
    assert report.outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp == 0
        assert move.eval_delta is None   # stale delta gone, not left contributing to CPL
    finally:
        check.close()


# ---------------------------------------------------------------------------
# The fifty-move flag follows chess.js, not python-chess.
# ---------------------------------------------------------------------------
def test_fifty_move_flag_matches_chess_js_on_an_overlapping_stalemate():
    """A position that is BOTH stalemate and at halfmove clock 100 must report BOTH
    subtypes — the frontend-matching, no-forced-precedence contract.

    python-chess's ``Board.is_fifty_moves()`` is ``halfmove_clock >= 100 AND a legal move
    exists``: its docstring says the check holds only when "no other means of ending the
    game (like checkmate) take precedence". chess.js's ``isDraw()`` tests ``_halfMoves >=
    100`` outright. So python-chess reports this position as stalemate ONLY, silently
    dropping the fifty-move flag; :func:`_is_fifty_moves` does not.

    Verdict-neutral by construction: a clock-100 position with no legal moves is either
    stalemate (already terminal) or checkmate (rejected outright), so this changes what is
    REPORTED, never what is filled — which is why it is pinned here on the predicate rather
    than through a game fixture.
    """
    # Black Ka8; white Qc7 covers a7/b7/b8 but not a8 -> stalemate, clock parked at 100.
    board = chess.Board("k7/2Q5/8/3K4/8/8/8/8 b - - 100 60")
    assert board.is_stalemate() and board.halfmove_clock == 100

    assert not board.is_fifty_moves()   # python-chess applies precedence...
    assert _is_fifty_moves(board)       # ...chess.js, and this module, do not
    assert _is_terminal_draw(board)     # terminal either way — the verdict is unchanged


def test_fifty_move_flag_requires_the_full_clock():
    """The boundary is exact: 99 halfmoves is not a fifty-move draw, with or without legal
    moves available."""
    assert not _is_fifty_moves(chess.Board("k7/2Q5/8/3K4/8/8/8/8 b - - 99 60"))
    assert _is_fifty_moves(chess.Board("k7/2Q5/8/3K4/8/8/8/8 b - - 101 60"))


def test_subtype_flags_are_counted_independently(db_session):
    """A position can satisfy several draw predicates at once; each flag is counted on its
    own with no forced precedence. The 100-halfmove knight shuffle is a fifty-move draw
    with ample material on the board — fifty_move only."""
    plies, pgn = _play(FIFTY_MOVE_SANS)
    _insert_game(db_session, plies=plies, pgn=pgn)

    s = run_backfill(_factory).sizing

    assert s.verified_fifty_move == 1
    assert s.verified_stalemate == 0
    assert s.verified_insufficient_material == 0


# ---------------------------------------------------------------------------
# Fail-closed: history-dependent verification needs an ESTABLISHED start.
# ---------------------------------------------------------------------------
def test_truncated_prefix_extended_threefold_rejected(db_session):
    """The counterexample that makes the established-start rule load-bearing.

    A 12-ply repetition game whose threefold really landed at ply 8, stored with rows 1-4
    missing. The remaining chain replays CLEANLY from its own first FEN and 'reaches'
    threefold at ply 12 — a from-first-stored-FEN verifier would bless the wrong ply,
    because a truncated prefix silently resets the repetition table.

    This is also why the established start is a COORDINATE rule and not just a FEN one:
    the suffix begins at the start POSITION (the shuffle returned there), so its first
    fen_before normalizes to STARTING_FEN and the FEN comparison alone would wave it
    through. Only the intact-coordinate requirement sees that the first stored row is
    move 3, not move 1.
    """
    from app.fen import normalize_fen

    plies, pgn = _play(THREEFOLD_EXTENDED_SANS)
    suffix = plies[4:]

    # The trap is real: the suffix's own first FEN normalizes to the standard start...
    assert normalize_fen(suffix[0]["fen_before"]) == normalize_fen(chess.STARTING_FEN)
    assert suffix[0]["fen_before"] != chess.STARTING_FEN   # ...differing only in clocks
    # ...and replayed from there it is a clean chain that ends in a threefold.
    board = chess.Board()
    for p in suffix:
        board.push_san(p["move_san"])
    assert board.is_repetition(3)
    assert suffix[0]["move_number"] == 3   # what actually rejects it

    sid = _insert_game(db_session, plies=plies, pgn=pgn, row_slice=slice(4, None))

    report = run_backfill(_factory)

    assert report.sizing.final_ply_missing_eval == 1
    assert report.sizing.rows_rejected_verification == 1   # start not established
    assert report.sizing.verified_threefold == 0
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 0

    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None   # null left in place
    finally:
        check.close()


def test_shifted_ply_coordinates_rejected(db_session):
    """A legal, link-consistent FEN chain whose stored move_number coordinates are all
    shifted by one is rejected by the coordinate-grid rule — the same mainline-grid
    invariant the guarded accuracy path enforces.

    (This rejection happens BEFORE sizing, so it cannot prove which accuracy function
    sizing calls — that is test_sizing_uses_the_guarded_accuracy_path's job.)
    """
    plies, pgn = _play(THREEFOLD_SANS)
    shifted = [dict(p, move_number=p["move_number"] + 1) for p in plies]
    sid = _insert_game(db_session, plies=shifted, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_ply_coordinate_hole_rejected(db_session):
    """A missing middle ply leaves a hole in the coordinate grid -> rejected."""
    plies, pgn = _play(THREEFOLD_SANS)
    holed = plies[:2] + plies[3:]
    sid = _insert_game(db_session, plies=holed, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_non_standard_starting_fen_rejected(db_session):
    """A first row whose fen_before is not the standard start cannot establish the history
    the fifty-move clock and the repetition table depend on -> rejected."""
    plies, pgn = _play(THREEFOLD_SANS)
    tweaked = [dict(p) for p in plies]
    # A legal-but-not-standard start: black is missing a rook. The chain below it is no
    # longer replayable from it either — both reasons are fail-closed.
    tweaked[0]["fen_before"] = "rnbqkbn1/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQq - 0 1"
    _insert_game(db_session, plies=tweaked, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0


def test_null_first_fen_before_rejected(db_session):
    """fen_before is nullable; a null first one cannot be compared to the standard start."""
    plies, pgn = _play(THREEFOLD_SANS)
    nulled = [dict(p) for p in plies]
    nulled[0]["fen_before"] = None
    _insert_game(db_session, plies=nulled, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0


# ---------------------------------------------------------------------------
# Fail-closed: chain, terminality, and result/replay agreement.
# ---------------------------------------------------------------------------
def test_fen_chain_mismatch_rejected(db_session):
    """A genuine placement difference between the replayed board and a stored fen_after
    (not just clocks) breaks the chain -> rejected."""
    plies, pgn = _play(STALEMATE_SANS)
    broken = [dict(p) for p in plies]
    broken[-1]["fen_after"] = chess.Board().fen()   # a real placement mismatch
    sid = _insert_game(db_session, plies=broken, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_mid_chain_fen_before_mismatch_rejected(db_session):
    """The break need not be on the final ply: a mid-chain fen_before that disagrees with
    the replayed board rejects the whole session."""
    plies, pgn = _play(STALEMATE_SANS)
    broken = [dict(p) for p in plies]
    broken[5]["fen_before"] = chess.Board().fen()
    _insert_game(db_session, plies=broken, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0


def test_malformed_san_rejected(db_session):
    plies, pgn = _play(STALEMATE_SANS)
    _insert_game(db_session, plies=plies, pgn=pgn, final_overrides={"move_san": "zzz"})

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0


def test_clock_only_fen_difference_accepted(db_session):
    """Stored FENs differing from the replayed board ONLY in the halfmove-clock /
    fullmove-number fields (5-6) are accepted — normalize_fen strips them, so a stored
    clock never rejects a real row (nor is it ever trusted: the replay carries its own)."""
    plies, pgn = _play(THREEFOLD_SANS)
    clocked = [dict(p) for p in plies]
    for p in clocked:
        for key in ("fen_before", "fen_after"):
            fields = p[key].split(" ")
            fields[4], fields[5] = "37", "99"
            p[key] = " ".join(fields)
    sid = _insert_game(db_session, plies=clocked, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 0
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp == 0
    finally:
        check.close()


def test_fifty_move_boundary_partner_at_99_halfmoves_rejected(db_session):
    """The exact-clock fail-closed analog of the forward fix: 99 halfmoves is NOT a
    fifty-move draw, so the final position is non-terminal and the row stays null."""
    plies, pgn = _play(FORTY_NINE_AND_A_HALF_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.sizing.verified_fifty_move == 0
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_threefold_lookalike_rejected(db_session):
    """A final placement identical to an earlier position but only the SECOND occurrence is
    not a threefold — exactly what a bare-FEN check would get wrong."""
    plies, pgn = _play(THREEFOLD_LOOKALIKE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.sizing.verified_threefold == 0
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_early_terminal_chain_rejected(db_session):
    """The chain went terminal BEFORE its final row: the threefold landed on ply 8 but the
    stored game runs to ply 12. The final row is not the ply that ended the game, so the
    session fails closed rather than having a later ply blessed."""
    plies, pgn = _play(THREEFOLD_EXTENDED_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn)

    report = run_backfill(_factory)

    assert report.sizing.rows_rejected_verification == 1
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_result_says_draw_but_replay_is_checkmate_rejected(db_session):
    """result='draw' with a replayed final position that is CHECKMATE is inconsistent data.
    The verifier rejects rather than writing 0 over a mate (and it is the checkmate
    backfill's shape anyway)."""
    plies, pgn = _play(FOOL)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, result="draw")

    report = run_backfill(_factory)

    assert report.sizing.final_ply_missing_eval == 1
    assert report.sizing.rows_rejected_verification == 1
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


def test_verifier_rejects_empty_row_set():
    assert verify_terminal_draw([]) is None


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
    import app.draw_final_ply_backfill as mod

    real = mod.game_accuracy_for_rows
    calls: list[list] = []

    def spy(rows, player_color, expected_total_moves, *, session_id=None):
        calls.append(list(rows))
        return real(rows, player_color, expected_total_moves, session_id=session_id)

    monkeypatch.setattr(mod, "game_accuracy_for_rows", spy)
    # The raw entry point is not even reachable from this module's namespace, so sizing
    # cannot drift onto it later without this failing.
    assert not hasattr(mod, "compute_game_accuracy")

    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn,
                       player_color=_last_ply_color(STALEMATE_SANS))

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
    assert before_rows[-1].eval_cp is None   # the candidate, unfilled
    assert after_rows[-1].eval_cp == 0       # the candidate, filled in memory only
    assert after_rows[-1].eval_mate is None
    # The in-memory "after" fill never touched the DB.
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Sizing buckets + reconciliation.
# ---------------------------------------------------------------------------
def test_bucket_repaired_when_final_ply_is_the_opponents_move(db_session):
    """The stalemate as the BLACK player: the null final ply is white's move, so player
    accuracy is non-null both before and after. The row is still filled (data-integrity
    repair) but the game never moved off None."""
    plies, pgn = _play(STALEMATE_SANS)   # white plays the last ply
    _insert_game(db_session, plies=plies, pgn=pgn, player_color="black")

    s = run_backfill(_factory).sizing

    assert s.moved_off_none == 0
    assert s.repaired_accuracy_already_non_null == 1
    assert s.residual_remains_none == 0
    assert s.reconciles()


def test_bucket_residual_unparseable_pgn_still_filled(db_session):
    """With no PGN the expected ply count is unknown, so accuracy stays None both before
    and after — the row is still filled (data integrity) and the session stays residual."""
    plies, _ = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=None,
                       player_color=_last_ply_color(STALEMATE_SANS))

    report = run_backfill(_factory)

    assert report.sizing.residual_remains_none == 1
    assert report.sizing.moved_off_none == 0
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp == 0   # still filled
    finally:
        check.close()


def test_mixed_cohort_reconciles_and_reports_actuals(db_session):
    stale, stale_pgn = _play(STALEMATE_SANS)
    three, three_pgn = _play(THREEFOLD_SANS)
    look, look_pgn = _play(THREEFOLD_LOOKALIKE_SANS)

    def seed(db):
        # moved_off_none: white owns the stalemating final ply.
        _insert_game(db, plies=stale, pgn=stale_pgn, user_id=1, player_color="white")
        # repaired: same game as black — the null final ply is the opponent's.
        _insert_game(db, plies=stale, pgn=stale_pgn, user_id=1, player_color="black")
        # moved_off_none: black owns the threefold's final ply.
        _insert_game(db, plies=three, pgn=three_pgn, user_id=2, player_color="black")
        # residual: verified, but no PGN.
        _insert_game(db, plies=stale, pgn=None, user_id=3, player_color="white")
        # rejected: only the 2nd occurrence, not a threefold.
        _insert_game(db, plies=look, pgn=look_pgn, user_id=4, player_color="black")
        # hidden: excluded from the cohort entirely.
        _insert_game(db, plies=stale, pgn=stale_pgn, user_id=5, session_mode="drill",
                     drill_state="failed")

    _seed(seed)

    report = run_backfill(_factory)
    s, o = report.sizing, report.outcome

    assert s.total_draw_sessions == 5
    assert s.hidden_draw_sessions_excluded == 1
    assert s.final_ply_missing_eval == 5
    assert s.moved_off_none == 2
    assert s.repaired_accuracy_already_non_null == 1
    assert s.residual_remains_none == 1
    assert s.rows_rejected_verification == 1
    assert s.final_ply_eval_cp_zero == 0
    assert s.reconciles()
    assert s.verified_stalemate == 3 and s.verified_threefold == 1

    # Actuals: the 4 verified draws were written across 4 distinct groups; the rejected
    # lookalike (user 4) wrote nothing, so its group did not bump.
    assert o.rows_filled_actual == 4
    assert o.evidence_groups_bumped_actual == 4
    # Under no concurrency the actual equals the sum of the three written buckets.
    assert o.rows_filled_actual == (
        s.moved_off_none + s.repaired_accuracy_already_non_null + s.residual_remains_none
    )

    check = TestingSessionLocal()
    try:
        assert _cursor(check, 4, "black") is None    # rejected-only group never bumped
        assert _cursor(check, 5, "white") is None    # hidden drill never touched
        assert _cursor(check, 1, "white").evidence_seq == 1
        assert _cursor(check, 1, "black").evidence_seq == 1
    finally:
        check.close()


def test_zero_move_draw_session_counted_in_cohort(db_session):
    sid = uuid.uuid4()
    db_session.add(
        GameSession(
            id=sid, user_id=1, started_at=datetime.now(timezone.utc), status="ended",
            result="draw", engine_elo=1500, player_color="white", session_mode="normal",
            is_rated=True, pgn=None,
        )
    )
    db_session.commit()

    report = run_backfill(_factory)

    assert report.sizing.total_draw_sessions == 1   # counted despite zero moves
    assert report.sizing.final_ply_missing_eval == 0
    assert report.outcome.rows_filled_actual == 0


# ---------------------------------------------------------------------------
# Cached accuracy refresh.
# ---------------------------------------------------------------------------
def test_repair_updates_stale_cached_accuracy(db_session):
    """A draw that Release A already stamped (algo version current, player_accuracy=None
    because the final ply was still null) has its cached accuracy refreshed by the repair —
    not left null for the Release B read switch to serve forever."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=1,
                       player_color=_last_ply_color(STALEMATE_SANS))
    gs = db_session.query(GameSession).filter(GameSession.id == sid).one()
    gs.player_accuracy = None
    gs.player_accuracy_algo_version = ACCURACY_ALGO_VERSION
    db_session.commit()

    assert run_backfill(_factory).outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        gs = check.query(GameSession).filter(GameSession.id == sid).one()
        assert gs.player_accuracy is not None                    # no longer stale-null
        assert gs.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
    finally:
        check.close()


def test_residual_repair_stamps_none_accuracy(db_session):
    """A residual session (no PGN -> genuinely unscoreable) still gets its cached accuracy
    recomputed and stamped (None, correctly)."""
    plies, _ = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=None, user_id=1,
                       player_color=_last_ply_color(STALEMATE_SANS))

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
# --dry-run, --session-id, idempotency.
# ---------------------------------------------------------------------------
def test_dry_run_writes_nothing(db_session):
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=9,
                       final_overrides={"eval_delta": 40})

    report = run_backfill(_factory, dry_run=True)

    # Reports what it WOULD write, but the group transaction was rolled back.
    assert report.outcome.rows_filled_actual == 1
    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp is None and move.eval_mate is None
        assert move.eval_delta == 40                # the stale delta clear rolled back too
        assert _cursor(check, 9, "white") is None   # bump rolled back with the writes
    finally:
        check.close()


def test_session_id_scopes_to_one_session(db_session):
    plies, pgn = _play(STALEMATE_SANS)

    def seed(db):
        a = _insert_game(db, plies=plies, pgn=pgn, user_id=1)
        b = _insert_game(db, plies=plies, pgn=pgn, user_id=2)
        return a, b

    target, other = _seed(seed)

    report = run_backfill(_factory, session_id=target)

    assert report.sizing.total_draw_sessions == 1
    assert report.outcome.rows_filled_actual == 1

    check = TestingSessionLocal()
    try:
        assert _final_move(check, target).eval_cp == 0    # filled
        assert _final_move(check, other).eval_cp is None  # untouched
    finally:
        check.close()


def test_idempotent_second_run_is_noop(db_session):
    plies, pgn = _play(STALEMATE_SANS)
    _insert_game(db_session, plies=plies, pgn=pgn, user_id=5)

    first = run_backfill(_factory)
    assert first.outcome.rows_filled_actual == 1

    second = run_backfill(_factory)
    # The filled row is no longer both-fields-null, so it is not a candidate again.
    assert second.sizing.final_ply_missing_eval == 0
    assert second.sizing.final_ply_eval_cp_zero == 1   # now visible as an eval_cp==0 row
    assert second.outcome.rows_filled_actual == 0
    assert second.outcome.evidence_groups_bumped_actual == 0

    check = TestingSessionLocal()
    try:
        assert _cursor(check, 5, "white").evidence_seq == 1   # not bumped again
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Between-phase re-checks (SQLite).
#
# NAMING/SCOPE: these exercise the PREDICATE / final-ply / chain re-checks between phases
# on SQLite, NOT PostgreSQL SELECT ... FOR UPDATE row-lock behavior (SQLite renders FOR
# UPDATE as a no-op and the tests are single-threaded). True concurrent row-lock behavior
# would need a PostgreSQL integration test and is out of scope here.
# ---------------------------------------------------------------------------
def test_between_phase_resolution_drops_candidate(db_session):
    """A candidate a concurrent /moves retry resolved with REAL worker values after Phase A
    is dropped by Phase B, never overwritten with the synthetic 0."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=11)

    reader = TestingSessionLocal()
    try:
        plan = plan_backfill(reader)
    finally:
        reader.rollback()
        reader.close()
    assert plan.groups.get((11, "white"))   # the candidate was captured

    retry = TestingSessionLocal()
    try:
        move = _final_move(retry, sid)
        move.eval_cp = 250
        retry.commit()
    finally:
        retry.close()

    outcome = apply_backfill(_factory, plan, dry_run=False)

    assert outcome.rows_filled_actual == 0
    assert outcome.evidence_groups_bumped_actual == 0

    check = TestingSessionLocal()
    try:
        move = _final_move(check, sid)
        assert move.eval_cp == 250   # real worker value preserved, NOT overwritten
        assert _cursor(check, 11, "white") is None   # no write -> no evidence bump
    finally:
        check.close()


def test_between_phase_appended_terminal_row_drops_stale_candidate(db_session):
    """Ended sessions still accept /moves upserts, so a NEWER terminal row can be appended
    between phases. The session's CURRENT chain still verifies as a draw — but the Phase A
    candidate is no longer its final ply, so Phase B must skip it. Verifying "the chain ends
    in a draw" is not enough; the verdict is bound to the row being filled.

    The threefold's ply-8 candidate is demoted by appending plies 9-12 of the extended
    shuffle, whose own final ply is a (later) threefold.
    """
    extended, extended_pgn = _play(THREEFOLD_EXTENDED_SANS)
    plies, pgn = extended[:8], _play(THREEFOLD_SANS)[1]
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=12, player_color="black")

    reader = TestingSessionLocal()
    try:
        plan = plan_backfill(reader)
    finally:
        reader.rollback()
        reader.close()
    stale_move_id = plan.groups[(12, "black")][0][1]

    # Simulate a /moves upsert appending the game's real continuation after Phase A.
    upsert = TestingSessionLocal()
    try:
        for p in extended[8:]:
            upsert.add(SessionMove(
                session_id=sid, move_number=p["move_number"], color=p["color"],
                move_san=p["move_san"], fen_before=p["fen_before"],
                fen_after=p["fen_after"], eval_cp=None, eval_mate=None, eval_delta=None,
            ))
        gs = upsert.query(GameSession).filter(GameSession.id == sid).one()
        gs.pgn = extended_pgn
        upsert.commit()
    finally:
        upsert.close()

    outcome = apply_backfill(_factory, plan, dry_run=False)

    assert outcome.rows_filled_actual == 0
    assert outcome.evidence_groups_bumped_actual == 0   # otherwise-empty group, no bump

    check = TestingSessionLocal()
    try:
        stale = check.query(SessionMove).filter(SessionMove.id == stale_move_id).one()
        assert stale.eval_cp is None and stale.eval_delta is None   # untouched
        assert _cursor(check, 12, "black") is None
    finally:
        check.close()

    # A subsequent full rerun sees the session's NEW final row. Here the extended chain went
    # terminal at ply 8 before its final row, so it fails closed — the stale candidate stays
    # untouched throughout either way.
    rerun = run_backfill(_factory)
    assert rerun.sizing.rows_rejected_verification == 1
    assert rerun.outcome.rows_filled_actual == 0

    check = TestingSessionLocal()
    try:
        stale = check.query(SessionMove).filter(SessionMove.id == stale_move_id).one()
        assert stale.eval_cp is None
    finally:
        check.close()


def test_between_phase_session_leaving_the_cohort_is_skipped(db_session):
    """A candidate whose session is no longer an ended draw when Phase B locks it (e.g. it
    was resumed or reclassified) is skipped: the cohort predicate is re-checked under the
    lock, not trusted from the Phase A snapshot."""
    plies, pgn = _play(STALEMATE_SANS)
    sid = _insert_game(db_session, plies=plies, pgn=pgn, user_id=13)

    reader = TestingSessionLocal()
    try:
        plan = plan_backfill(reader)
    finally:
        reader.rollback()
        reader.close()

    mutate = TestingSessionLocal()
    try:
        gs = mutate.query(GameSession).filter(GameSession.id == sid).one()
        gs.status = "active"
        gs.result = None
        mutate.commit()
    finally:
        mutate.close()

    outcome = apply_backfill(_factory, plan, dry_run=False)

    assert outcome.rows_filled_actual == 0
    assert outcome.evidence_groups_bumped_actual == 0
    check = TestingSessionLocal()
    try:
        assert _final_move(check, sid).eval_cp is None
        assert _cursor(check, 13, "white") is None
    finally:
        check.close()


# ---------------------------------------------------------------------------
# Evidence bump ordering: the cursor upsert is the transaction's final write after the
# move and cached-accuracy writes have flushed.
# ---------------------------------------------------------------------------
def test_cursor_upsert_is_last_write(db_session):
    from sqlalchemy import event

    plies, pgn = _play(STALEMATE_SANS)
    _insert_game(db_session, plies=plies, pgn=pgn, user_id=1,
                 player_color=_last_ply_color(STALEMATE_SANS))

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
    assert max(move_updates) < cursor_writes[-1]
    assert max(acc_updates) < cursor_writes[-1]
    assert cursor_writes[-1] == len(writes) - 1


# ---------------------------------------------------------------------------
# Real PostgreSQL: exercises what SQLite cannot — the Phase A REPEATABLE READ read-only
# snapshot, the parent-session FOR NO KEY UPDATE lock, and the cached-accuracy recompute on
# the alembic-migrated schema. @pg_required skips cleanly without GHOSTREPLAY_TEST_PG_URL.
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

    stale, stale_pgn = _play(STALEMATE_SANS)
    seed = pg_session_factory()
    try:
        seed.add(User(id=1, username=None, is_anonymous=True))
        seed.commit()
        moved = _insert_game(seed, plies=stale, pgn=stale_pgn, user_id=1,
                             player_color="white")   # moved_off_none
        repaired = _insert_game(seed, plies=stale, pgn=stale_pgn, user_id=1,
                                player_color="black")  # repaired
    finally:
        seed.close()

    report = run_backfill(pg_session_factory)

    assert report.sizing.total_draw_sessions == 2
    assert report.sizing.moved_off_none == 1
    assert report.sizing.repaired_accuracy_already_non_null == 1
    assert report.sizing.reconciles()
    assert report.outcome.rows_filled_actual == 2
    assert report.outcome.evidence_groups_bumped_actual == 2

    check = pg_session_factory()
    try:
        w = check.query(GameSession).filter(GameSession.id == moved).one()
        assert w.player_accuracy is not None                     # recompute ran on PG
        assert w.player_accuracy_algo_version == ACCURACY_ALGO_VERSION
        assert _final_move(check, moved).eval_cp == 0

        b = check.query(GameSession).filter(GameSession.id == repaired).one()
        assert b.player_accuracy is not None
        assert _final_move(check, repaired).eval_cp == 0

        assert _cursor(check, 1, "white").evidence_seq == 1
        assert _cursor(check, 1, "black").evidence_seq == 1
    finally:
        check.close()
