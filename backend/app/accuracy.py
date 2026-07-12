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

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.accuracy_v1 import (
    AccuracyMove,
    accuracy_from_win_percents,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    win_percent_from_cp,
)
from app.models import GameSession, SessionMove
from app.session_contracts import is_visible_game_session

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
    "accuracy_from_win_percents",
    "compute_game_accuracy",
    "expected_total_moves_from_pgn",
    "recompute_session_accuracy",
    "win_percent_from_cp",
]


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
    * builds :class:`AccuracyMove` values, parses the expected ply count from the
      session PGN, and calls frozen v1;
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
        db.query(SessionMove.color, SessionMove.eval_cp, SessionMove.eval_mate)
        .filter(SessionMove.session_id == session.id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    accuracy = compute_game_accuracy(
        [
            AccuracyMove(color=row.color, eval_cp=row.eval_cp, eval_mate=row.eval_mate)
            for row in move_rows
        ],
        player_color=session.player_color,
        expected_total_moves=expected_total_moves_from_pgn(session.pgn),
    )
    # Assign even a legitimate None, and always stamp the algorithm version so an
    # eligible session is never left half-stamped.
    session.player_accuracy = accuracy
    session.player_accuracy_algo_version = ACCURACY_ALGO_VERSION
