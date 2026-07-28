"""Live session-accuracy surface.

This module is the supported import point for whole-game accuracy. Application
code imports the public names from here (``from app.accuracy import ...``); the
actual algorithm lives in the frozen :mod:`app.accuracy_v1` snapshot, which this
module re-exports.

The indirection lets a future accuracy **v2** replace what live code computes by
swapping the re-export here, while every historical session that was scored by v1
stays reproducible: migrations and the frozen-golden tests import
:mod:`app.accuracy_v1` directly and never through this re-export. See
``docs/session-accuracy-versioning.md`` for the versioning contract.

Only the supported public names are re-exported. Private helpers and constants
(``_white_relative_cp``, ``_MATE_CP``, the Lichess coefficients, …) are *not*
part of this surface; tests that need them import from :mod:`app.accuracy_v1`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.accuracy_rows_v1 import ply_color, ply_coordinates_intact
from app.accuracy_v1 import (
    AccuracyMove,
    accuracy_from_win_percents,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    win_percent_from_cp,
)
from app.models import GameSession, SessionMove
from app.session_contracts import is_visible_game_session

logger = logging.getLogger(__name__)

# Algorithm version stamped onto session rows Release A writes. Bump ONLY when a
# new frozen module (accuracy_v2) becomes the live surface.
ACCURACY_ALGO_VERSION = 1

# The exact python-chess version accuracy v1 was captured and validated against
# (verified on 2026-07-11 against the deployed Railway artifact's /opt/venv). The
# runtime and requirements.txt must both enforce this pin; a drift means the
# frozen goldens are no longer guaranteed and requires an accuracy-v2 review.
CHESS_VERSION_PIN = "1.11.2"

__all__ = [
    "ACCURACY_ALGO_VERSION",
    "CHESS_VERSION_PIN",
    "AccuracyMove",
    "accuracy_for_sessions",
    "accuracy_from_win_percents",
    "compute_game_accuracy",
    "expected_total_moves_from_pgn",
    "game_accuracy_for_rows",
    "ply_color",
    "ply_coordinates_intact",
    "recompute_session_accuracy",
    "win_percent_from_cp",
]


def accuracy_for_sessions(
    db: Session, sessions: Iterable[GameSession]
) -> dict[uuid.UUID, int | None]:
    """Whole-game accuracy per session id, read from the cached column.

    Release B's aggregate read seam: ``/api/stats/summary`` and ``/api/history``
    reach accuracy ONLY through here, never through :func:`game_accuracy_for_rows`.

    **Issues no SQL.** Both callers already hold the ORM ``GameSession`` rows they
    are about to return, so this reads a loaded column attribute and must not
    trigger a lazy load. ``test_accuracy.py`` pins that with a statement counter.

    ``db`` is unused today and deliberately kept: it is what lets a future accuracy
    vN.0 swap this one body back to a bulk live computation without touching either
    consumer. See ``docs/session-accuracy-versioning.md``.

    **Version-agnostic by contract.** This does not read, filter on, or interpret
    ``player_accuracy_algo_version``, and neither consumer does. Version currency
    belongs to the migration: revision ``20260719_01``'s invalidation predicate
    (``player_accuracy_algo_version IS NULL OR < ACCURACY_ALGO_VERSION``) plus its
    coverage assertion guarantee every ended-visible row is stamped at the current
    version before any cache-only read serves. A reader therefore sees exactly two
    states — an integer 0..100, or None meaning "no accuracy was derivable", which
    includes the ply-coordinate guard's fail-closed verdict. Hidden sessions never
    reach here at all; ``visible_session_filter`` excludes them upstream.
    """
    return {session.id: session.player_accuracy for session in sessions}


def game_accuracy_for_rows(
    rows,
    player_color: str,
    expected_total_moves: int | None,
    *,
    session_id=None,
) -> int | None:
    """Guarded entry point. Every live caller uses THIS, never
    :func:`compute_game_accuracy`.

    ``rows`` are ``SessionMove`` rows (ORM instances or query tuples carrying
    ``move_number``, ``color``, ``eval_cp``, ``eval_mate``) already ordered by
    move_number ASC, white before black.

    Fails **closed** — returns None — on any non-mainline coordinate grid, which
    is the invariant that makes the algorithm's parity attribution and its
    ``move.color`` eval sign name the same side.

    It does NOT fail closed on ``n > expected_total_moves``, and that is now a
    MEASURED decision rather than a deferred one. g-i6st ran the check against a
    restore of the 2026-07-24 production dump: of 1,646 ended-visible sessions, 6
    had surplus rows, and the dominant shape is a TRUNCATED PGN rather than a
    surplus row set — the extra rows replay as a legal, ``fen_after``-chained
    continuation of the PGN's own final position, so the rows are the FULLER
    record and the PGN lags them. One of them (session 298ec83a) scores 50 over
    its 18 rows and 92 over the PGN's 16, because the two rows the PGN is missing
    are the queen blunder the player resigned to. Tightening to ``==`` would have
    nulled three CORRECT scores while missing 16 of the 19 sessions that really
    are serving a wrong number.

    Length is therefore not the discriminator, in either direction: it cannot
    tell "the PGN is short" from "the rows describe a different game". The defect
    worth catching is row-vs-PGN IDENTITY — a row at the right coordinate
    carrying a move the game never played — which needs a PGN replay and which
    this surface deliberately does not do. See g-i6st for the measurement and
    g-discard-branch-rows for the writer that produced the population.
    """
    if not ply_coordinates_intact(rows):
        logger.warning("accuracy: non-mainline ply coordinates, session=%s", session_id)
        return None
    return compute_game_accuracy(
        # ply_color, not a local re-implementation: the row set the guard
        # validated and the move list the algorithm scores must read colour by
        # the SAME rule.
        [
            AccuracyMove(color=ply_color(row), eval_cp=row.eval_cp, eval_mate=row.eval_mate)
            for row in rows
        ],
        player_color=player_color,
        expected_total_moves=expected_total_moves,
    )


def recompute_session_accuracy(db: Session, session: GameSession) -> None:
    """Recompute and stamp one session's cached whole-game accuracy in place.

    This is Release A's bounded write hook. The serving ``/api/game/end`` and
    post-end ``/api/session/{id}/moves`` writers call it while holding that
    session's ``FOR NO KEY UPDATE`` lock. It:

    * returns before touching either cache field unless the *in-memory* session
      is already ``ended`` and visible — a normal game or a converted drill —
      via the shared :func:`is_visible_game_session` predicate, so active
      sessions and ended failed/abandoned drills stay wholly unstamped;
    * issues exactly one ``SessionMove`` evaluation query scoped to this
      session, ordered by move number with white before black — the ply order
      :func:`compute_game_accuracy` requires;
    * parses the expected ply count from the session PGN and calls
      :func:`game_accuracy_for_rows`, so a non-mainline ply grid fails closed to
      None here rather than being scored wrong and then cached forever;
    * assigns ``player_accuracy`` (including a legitimate computed ``None``) and
      always stamps ``player_accuracy_algo_version`` once an eligible
      computation runs.

    It NEVER commits or flushes. The caller owns the transaction and flush
    order, so the dirty accuracy assignment drains in the caller's own
    pre-cursor flush. Because it reads the in-memory session, it sees a caller's
    dirty terminal status/PGN (e.g. game-end's not-yet-committed mutation) and
    performs no query or PGN parse at all when the session is ineligible.
    """
    # Population guard: only an ended, visible session is stamped. Uses the
    # shared visibility predicate rather than duplicating it, and reads the
    # in-memory instance so an ineligible session costs no move query / PGN parse.
    if session.status != "ended" or not is_visible_game_session(session):
        return

    color_order = case((SessionMove.color == "white", 0), else_=1)
    move_rows = (
        db.query(
            SessionMove.move_number,
            SessionMove.color,
            SessionMove.eval_cp,
            SessionMove.eval_mate,
        )
        .filter(SessionMove.session_id == session.id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    accuracy = game_accuracy_for_rows(
        move_rows,
        player_color=session.player_color,
        expected_total_moves=expected_total_moves_from_pgn(session.pgn),
        session_id=session.id,
    )
    # Assign even a legitimate None, and always stamp the algorithm version so an
    # eligible session is never left half-stamped.
    session.player_accuracy = accuracy
    session.player_accuracy_algo_version = ACCURACY_ALGO_VERSION
