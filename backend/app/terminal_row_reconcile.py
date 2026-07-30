"""Reconcile a session's stored move rows against its terminal PGN.

``POST /api/game/end`` persists the client's PGN verbatim while the move rows
arrive through separate ``POST /moves`` transactions, so a final upload that
never commits leaves an ended session describing more plies than it stores —
the ``rows_short_of_pgn`` refusal shape (g-short-move-rows). This module closes
that gap at the terminal write: when the stored rows are a verified prefix of
the PGN mainline, the missing tail rows are derived from the PGN itself and
staged into the caller's transaction, so the same commit that flips
``status='ended'`` also persists the full canonical row grid.

Derived rows carry only what the PGN proves — coordinates, SAN, and the FEN
chain. Evaluations stay NULL (accuracy then refuses under the eval-gap rule
rather than the short-row rule), and a later ``/moves`` upsert overwrites a
derived row with the client's richer record via ON CONFLICT DO UPDATE.

Fail-closed by design: an unparseable PGN, a stored row disagreeing with the
PGN mainline (the g-discard-branch-rows shape), a surplus row set (the g-i6st
truncated-PGN shape, a measured non-defect), or a PGN over the derivation
ceilings (``MAX_TERMINAL_PGN_CHARS`` / ``MAX_DERIVABLE_PLIES``) all derive
nothing.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import chess
import chess.pgn
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.models import GameSession, SessionMove
from app.session_contracts import segment_for_move

logger = logging.getLogger(__name__)

# Mutually exclusive reconcile outcomes, emitted on the post-commit
# ``game_ended`` analytics event so recurrence is measurable directly:
# ``complete``        stored rows >= PGN mainline plies (or both empty) — no-op.
# ``pgn_unknown``     PGN absent/unparseable/empty — expected count unknowable.
# ``prefix_mismatch`` a stored row disagrees with the PGN mainline — fail closed.
# ``over_ceiling``    PGN exceeds a derivation ceiling below — fail closed.
# ``derived``         verified short prefix; missing tail rows were staged.
OUTCOME_COMPLETE = "complete"
OUTCOME_PGN_UNKNOWN = "pgn_unknown"
OUTCOME_PREFIX_MISMATCH = "prefix_mismatch"
OUTCOME_OVER_CEILING = "over_ceiling"
OUTCOME_DERIVED = "derived"

# Derivation ceilings. ``GameEndRequest.pgn`` is unbounded, and without a bound
# one terminal request could expand into thousands of staged INSERTs under the
# session lock (a legal ~6.4 KB repetition PGN encodes 1000 plies). Real games
# sit far below both bounds — the longest recorded serious game is 269 moves
# (538 plies) — so exceeding either is fail-closed refusal, never derivation:
# the session keeps its short rows and strict-NULL accuracy, exactly the
# pre-reconcile behavior. The size gate (``_pgn_size_over_ceiling``) runs
# before the parse; ``end_game`` then reuses ``ReconcileResult.expected_plies``
# for the accuracy recompute and analytics, so the terminal path parses a PGN
# exactly once and a size-refused PGN exactly zero times.
MAX_TERMINAL_PGN_BYTES = 32_768
MAX_DERIVABLE_PLIES = 600


def _pgn_size_over_ceiling(pgn: str) -> bool:
    """True when the PGN exceeds ``MAX_TERMINAL_PGN_BYTES`` of strict UTF-8.

    Two steps so the check is both bounded and exact. Code points first: UTF-8
    bytes >= code points, so anything over the ceiling in characters is over
    it in bytes, and this O(1) reject bounds the encode below without touching
    an arbitrarily large string. Then a strict encode of the bounded
    remainder, which catches multi-byte inflation (20k two-byte characters is
    ~40 KiB — a character count alone would pass it) and refuses unpaired
    surrogates outright (JSON escapes can smuggle them into a ``str``;
    ``errors="ignore"`` would silently drop them from the count, and they can
    never be stored anyway).
    """
    if len(pgn) > MAX_TERMINAL_PGN_BYTES:
        return True
    try:
        return len(pgn.encode("utf-8")) > MAX_TERMINAL_PGN_BYTES
    except UnicodeEncodeError:
        return True


@dataclass(frozen=True)
class PlyRecord:
    move_number: int
    color: str
    san: str
    uci: str
    fen_before: str
    fen_after: str


@dataclass(frozen=True)
class ReconcileResult:
    outcome: str
    expected_plies: int | None
    stored_rows: int
    derived_rows: int


def replay_pgn_mainline(pgn: str | None) -> list[PlyRecord] | None:
    """Replay a stored PGN's mainline into per-ply records, or None.

    The terminal path's single parse: the reconcile takes both the expected
    ply count (its ``len``) and the verification/derivation records from one
    call. Its reject conditions — parse errors, empty mainlines, non-PGN
    text — mirror the frozen :func:`accuracy_v1.expected_total_moves_from_pgn`
    that post-end recomputes still parse with: both refuse the same PGNs and
    count the same ``mainline_moves()``, and the per-ply replay here can only
    refuse MORE (any replay failure returns None), never disagree on a count.
    Starts from ``pgn_game.board()`` so a FEN/SetUp header is honored, and
    takes each ply's coordinates from the board state rather than assuming a
    move-1 start.
    """
    if not pgn:
        return None
    try:
        pgn_game = chess.pgn.read_game(io.StringIO(pgn))
        if pgn_game is None or pgn_game.errors:
            return None
        board = pgn_game.board()
        records: list[PlyRecord] = []
        for move in pgn_game.mainline_moves():
            fen_before = board.fen()
            move_number = board.fullmove_number
            color = "white" if board.turn == chess.WHITE else "black"
            san = board.san(move)
            board.push(move)
            records.append(
                PlyRecord(
                    move_number=move_number,
                    color=color,
                    san=san,
                    uci=move.uci(),
                    fen_before=fen_before,
                    fen_after=board.fen(),
                )
            )
        return records or None
    except Exception:
        return None


def stored_prefix_matches(rows, replay: list[PlyRecord]) -> bool:
    """True when every stored row is the PGN mainline ply at its position.

    ``rows`` must be ordered like the accuracy readers order them: move_number
    ascending, white before black, and must not be longer than ``replay``.
    Identity is checked move-by-move — coordinates, the SAN parsed on the
    replay board (tolerating spelling variants but not different moves), and
    the normalized FEN chain — so a coordinate-intact row carrying a move the
    game never played fails closed here.
    """
    if len(rows) > len(replay):
        return False
    for row, ply in zip(rows, replay):
        color = row.color.value if hasattr(row.color, "value") else str(row.color)
        if row.move_number != ply.move_number or color != ply.color:
            return False
        try:
            # A malformed stored SAN or FEN (legacy rows carry junk) is an
            # identity failure, never an exception on the terminal path.
            if chess.Board(ply.fen_before).parse_san(row.move_san).uci() != ply.uci:
                return False
            if normalize_fen(row.fen_after) != normalize_fen(ply.fen_after):
                return False
            if row.fen_before is not None and normalize_fen(
                row.fen_before
            ) != normalize_fen(ply.fen_before):
                return False
        except (ValueError, TypeError):
            return False
    return True


def _ordered_rows(db: Session, session_id) -> list[SessionMove]:
    color_order = case((SessionMove.color == "white", 0), else_=1)
    return (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )


def reconcile_terminal_move_rows(
    db: Session, session: GameSession, *, stage: bool = True, log: bool = True
) -> ReconcileResult:
    """Stage the PGN-derived tail rows for a row-short session, fail-closed.

    Reads the in-memory ``session.pgn`` (the caller's possibly-dirty terminal
    assignment) and stages ``db.add`` INSERTs only. It never flushes or
    commits: the caller owns the transaction and must flush before any scoped
    SELECT (``recompute_session_accuracy``) needs to see the derived rows.
    Callers hold the session's FOR NO KEY UPDATE lock, which serializes this
    against concurrent ``/moves`` upserts of the same coordinates.

    ``stage=False`` classifies only — no INSERTs are staged and nothing is
    logged — so a read-only planner (``short_move_row_backfill``) can reuse
    this exact decision without mutating its snapshot transaction.
    ``log=False`` stages without the warnings, whose messages carry the
    session UUID: right for the serving process's logs, forbidden on the
    repair script's aggregate-only stdout/stderr.
    """
    if session.pgn is not None and _pgn_size_over_ceiling(session.pgn):
        return ReconcileResult(
            OUTCOME_OVER_CEILING, None, _stored_count(db, session.id), 0
        )

    # The terminal path's ONE parse: the expected count and the verification/
    # derivation records come from the same game object, and the caller
    # propagates the count onward, so /end never parses this PGN again. The
    # transient materialization is bounded by the byte gate above.
    replay = replay_pgn_mainline(session.pgn)
    if replay is None:
        return ReconcileResult(
            OUTCOME_PGN_UNKNOWN, None, _stored_count(db, session.id), 0
        )
    expected = len(replay)

    stored_count = _stored_count(db, session.id)
    if stored_count >= expected:
        return ReconcileResult(OUTCOME_COMPLETE, expected, stored_count, 0)

    if expected > MAX_DERIVABLE_PLIES:
        if stage and log:
            logger.warning(
                "terminal reconcile: PGN ply count exceeds the derivation "
                "ceiling, session=%s stored=%d expected=%d",
                session.id,
                stored_count,
                expected,
            )
        return ReconcileResult(OUTCOME_OVER_CEILING, expected, stored_count, 0)

    rows = _ordered_rows(db, session.id)
    if not stored_prefix_matches(rows, replay):
        if stage and log:
            logger.warning(
                "terminal reconcile: stored rows disagree with the PGN mainline, "
                "session=%s stored=%d expected=%d",
                session.id,
                len(rows),
                expected,
            )
        return ReconcileResult(OUTCOME_PREFIX_MISMATCH, expected, len(rows), 0)

    derived = expected - len(rows)
    if stage:
        for ply in replay[len(rows):]:
            db.add(
                SessionMove(
                    session_id=session.id,
                    move_number=ply.move_number,
                    color=ply.color,
                    move_san=ply.san,
                    fen_before=ply.fen_before,
                    fen_after=ply.fen_after,
                    segment=segment_for_move(session, ply.move_number, ply.color),
                )
            )
        if log:
            logger.warning(
                "terminal reconcile: derived %d tail row(s) from the terminal "
                "PGN, session=%s stored=%d expected=%d",
                derived,
                session.id,
                len(rows),
                expected,
            )
    return ReconcileResult(OUTCOME_DERIVED, expected, len(rows), derived)


def _stored_count(db: Session, session_id) -> int:
    return (
        db.query(func.count(SessionMove.id))
        .filter(SessionMove.session_id == session_id)
        .scalar()
        or 0
    )
