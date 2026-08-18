"""Reconcile a session's stored move rows against its terminal PGN.

``POST /api/game/end`` persists the client's PGN verbatim while the move rows
arrive through separate ``POST /moves`` transactions, so a final upload that
never commits leaves an ended session describing more plies than it stores —
the ``rows_short_of_pgn`` refusal shape (g-short-move-rows). This module closes
that gap at the terminal write: when the stored rows are a verified prefix of
the PGN mainline, the missing tail rows are derived from the PGN itself and
staged into the caller's transaction, so the same commit that flips
``status='ended'`` also persists the full canonical row grid. The serving path
also accepts a verified sparse subset, which is the historical
``broken_ply_grid`` mechanism: resolved-only incremental uploads could leave an
interior coordinate absent while later plies and the terminal PGN survived.

Derived rows carry only what the PGN proves — coordinates, SAN, and the FEN
chain. Evaluations stay NULL (accuracy then refuses under the eval-gap rule
rather than the short-row rule), and a later ``/moves`` upsert overwrites a
derived row with the client's richer record via ON CONFLICT DO UPDATE.

Fail-closed by design: an unparseable or size-over PGN, a stored row
disagreeing with the PGN mainline when missing rows would be derived (the
g-discard-branch-rows shape), a surplus row set (the g-i6st truncated-PGN
shape, a measured non-defect), or a PGN over ``MAX_DERIVABLE_PLIES`` whose
canonical coordinate set is incomplete all derive nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import chess
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.models import GameSession, SessionMove
from app.opening_boundary import clear_boundary_observation
from app.session_contracts import segment_for_move
from app.terminal_pgn import (
    MAX_DERIVABLE_PLIES,
    MAX_TERMINAL_PGN_BYTES,
    PlyRecord,
    pgn_size_over_ceiling,
    replay_pgn_mainline,
)

logger = logging.getLogger(__name__)

# Mutually exclusive reconcile outcomes, emitted on the post-commit
# ``game_ended`` analytics event so recurrence is measurable directly:
# ``complete``        surplus rows, or a complete allowed coordinate set — no-op.
# ``pgn_unknown``     PGN absent/unparseable/empty — expected count unknowable.
# ``prefix_mismatch`` a stored row disagrees with the PGN mainline, or the
#                     caller's policy does not allow a sparse grid — fail closed.
# ``over_ceiling``    PGN exceeds a derivation ceiling below — fail closed.
# ``derived``         verified short prefix/subset; missing rows were staged.
OUTCOME_COMPLETE = "complete"
OUTCOME_PGN_UNKNOWN = "pgn_unknown"
OUTCOME_PREFIX_MISMATCH = "prefix_mismatch"
OUTCOME_OVER_CEILING = "over_ceiling"
OUTCOME_DERIVED = "derived"
OUTCOME_LINE_UNACKNOWLEDGED = "line_unacknowledged"


@dataclass(frozen=True)
class TerminalLineFenceResult:
    acknowledged: bool
    deleted_rows: int


def suppress_unacknowledged_move_line(
    db: Session,
    session: GameSession,
    *,
    line_revision: int | None,
    discard_move_evidence: bool,
) -> TerminalLineFenceResult:
    """Discard an unacknowledged branch and fence every stale writer.

    Terminal actions must never wait for a browser-side takeback request.  A
    current client sends its last acknowledged generation; legacy clients omit
    it and remain compatible only while the server is still at generation zero.
    If the client explicitly reports an unknown transition, or its generation
    no longer matches under the session lock, remove every move row and advance
    the generation in this same transaction.  The terminal action can then
    commit normally while an already-sent upload or truncate is guaranteed to
    fail its generation/active-session check instead of restoring evidence.

    Returns whether the branch was acknowledged plus the number of deleted rows,
    so callers can skip reconciliation and advance an already-visible evidence
    cursor when needed.
    """
    revision_acknowledged = not discard_move_evidence and (
        line_revision == session.move_line_revision
        if line_revision is not None
        else session.move_line_revision == 0
    )
    if revision_acknowledged:
        return TerminalLineFenceResult(acknowledged=True, deleted_rows=0)

    deleted = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session.id)
        .delete(synchronize_session=False)
    )
    session.move_line_revision += 1
    clear_boundary_observation(session)
    session.terminal_line_reconciled = False
    session.derived_tail_rows = None
    return TerminalLineFenceResult(acknowledged=False, deleted_rows=deleted)


# Derivation ceilings. ``GameEndRequest.pgn`` is unbounded, and without a bound
# one terminal request could expand into thousands of staged INSERTs under the
# session lock (a legal ~6.4 KB repetition PGN encodes 1000 plies). Real games
# sit far below both bounds — the longest recorded serious game is 269 moves
# (538 plies) — so exceeding either with an incomplete coordinate set is
# fail-closed refusal, never derivation or identity verification: the session
# keeps its rows and strict-NULL accuracy, exactly the pre-reconcile behavior.
# The size gate (``_pgn_size_over_ceiling``) runs
# before the parse; ``end_game`` then reuses ``ReconcileResult.expected_plies``
# for the accuracy recompute and analytics, so the terminal path parses a PGN
# exactly once and a size-refused PGN exactly zero times.
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
    return pgn_size_over_ceiling(pgn, max_bytes=MAX_TERMINAL_PGN_BYTES)


@dataclass(frozen=True)
class ReconcileResult:
    outcome: str
    expected_plies: int | None
    stored_rows: int
    derived_rows: int


def _row_matches_ply(row, ply: PlyRecord) -> bool:
    """True when one stored row is the declared PGN ply, including identity."""
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
    return all(_row_matches_ply(row, ply) for row, ply in zip(rows, replay))


def stored_subset_matches(rows, replay: list[PlyRecord]) -> bool:
    """True when every stored row is its declared ply in the PGN mainline.

    Unlike :func:`stored_prefix_matches`, rows may omit interior coordinates.
    Each surviving row is looked up by its own ``(move_number, color)`` rather
    than zipped against a compacted ordinal, so its attached evaluation can
    never be shifted onto another ply.
    """
    if len(rows) > len(replay):
        return False
    replay_by_key = {(ply.move_number, ply.color): ply for ply in replay}
    for row in rows:
        color = row.color.value if hasattr(row.color, "value") else str(row.color)
        ply = replay_by_key.get((row.move_number, color))
        if ply is None or not _row_matches_ply(row, ply):
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
    db: Session,
    session: GameSession,
    *,
    stage: bool = True,
    log: bool = True,
    allow_sparse: bool = False,
) -> ReconcileResult:
    """Stage PGN-derived missing rows for a row-short session, fail-closed.

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

    ``allow_sparse=False`` preserves the historical repair boundary: only a
    verified prefix can be extended, so the ten historical interior-grid
    sessions remain untouched. The live ``/game/end`` caller opts into
    ``allow_sparse=True`` because it is reconciling a still-active session.
    When its coordinate comparison finds an absent PGN ply, every surviving
    row must bind to its declared PGN ply before the missing coordinates can
    be represented honestly with NULL-evaluation rows. An exact canonical key
    set returns complete without paying SAN/FEN identity replay because there
    is nothing for this function to derive.
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
    # Preserve the measured g-i6st policy in both modes: surplus rows can be the
    # fuller record over a truncated PGN, so length alone never deletes or
    # rejects them. Prefix mode also preserves its original exact-count no-op.
    # Sparse mode must inspect coordinates at exact count because a missing
    # canonical key plus a surplus key can otherwise masquerade as complete.
    if stored_count > expected or (stored_count == expected and not allow_sparse):
        return ReconcileResult(OUTCOME_COMPLETE, expected, stored_count, 0)

    rows = _ordered_rows(db, session.id)
    if allow_sparse:
        stored_keys = {
            (
                row.move_number,
                row.color.value if hasattr(row.color, "value") else str(row.color),
            )
            for row in rows
        }
        missing = [
            ply
            for ply in replay
            if (ply.move_number, ply.color) not in stored_keys
        ]
    else:
        missing = replay[len(rows) :]

    if not missing:
        return ReconcileResult(OUTCOME_COMPLETE, expected, len(rows), 0)

    # The ply ceiling bounds both expansion and the SAN/FEN replay needed to
    # authorize it. Complete coordinate sets return above without either cost;
    # any missing-key shape refuses here before identity verification.
    if expected > MAX_DERIVABLE_PLIES:
        if stage and log:
            logger.warning(
                "terminal reconcile: PGN ply count exceeds the derivation "
                "ceiling, session=%s stored=%d expected=%d",
                session.id,
                stored_count,
                expected,
            )
        return ReconcileResult(OUTCOME_OVER_CEILING, expected, len(rows), 0)

    rows_match = (
        stored_subset_matches(rows, replay)
        if allow_sparse
        else stored_prefix_matches(rows, replay)
    )
    if not rows_match:
        if stage and log:
            logger.warning(
                "terminal reconcile: stored rows disagree with the PGN mainline, "
                "session=%s stored=%d expected=%d",
                session.id,
                len(rows),
                expected,
            )
        return ReconcileResult(OUTCOME_PREFIX_MISMATCH, expected, len(rows), 0)

    derived = len(missing)
    if stage:
        for ply in missing:
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
                "terminal reconcile: derived %d missing row(s) from the terminal "
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
