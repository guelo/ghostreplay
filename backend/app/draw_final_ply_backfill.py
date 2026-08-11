"""One-time data-repair backfill for historical DRAW final-ply evals (g-c60b).

Sibling of :mod:`app.checkmate_final_ply_backfill` (g-eh2w) for terminal draws. The
g-hs78 forward fix (``fillUnresolvedTerminal``) deterministically fills the terminal
draw final ply for NEW games (``eval_cp=0, eval_mate=null, eval_delta=null``) but cannot
restore historical data. A draw-ended session whose final :class:`~app.models.SessionMove`
row has ``eval_cp IS NULL AND eval_mate IS NULL`` keeps whole-game accuracy ``None``
forever.

This module owns the selection / sizing / verify / fill / evidence-bump logic and the
read/write transaction orchestration; :mod:`scripts.backfill_draw_final_ply_evals` is a
thin argparse CLI over :func:`run_backfill`. It shares :func:`begin_readonly_snapshot`
and :func:`final_ply_rows` with the checkmate module and deliberately duplicates the
rest: the buckets, the verification, and the fill differ enough that parameterizing the
whole pipeline would obscure both scripts.

Design contracts (see the g-c60b bead design field for the full plan):

* **History-dependent verification, from an ESTABLISHED start.** Checkmate is verifiable
  from ONE ply (``fen_before + move_san``). Terminal draws are not: stalemate and
  insufficient material are readable from the final position, but threefold repetition
  and the fifty-move rule are HISTORY-dependent — a bare FEN encodes no repetition count,
  and a stored halfmove clock is untrusted (``normalize_fen`` strips fields 5-6 precisely
  because stored clocks aren't relied on). So :func:`verify_terminal_draw` replays the
  COMPLETE game from the standard start, mirroring the forward fix. Replaying "from the
  first stored row's fen_before" would be UNSOUND: a truncated prefix silently resets both
  the repetition table and the halfmove clock, so a threefold that really landed at ply 8
  is invisible when rows 1-4 are missing and a LATER ply gets blessed as the first terminal
  repetition. The pinned standard start also removes any need to trust a stored clock — the
  replay's clock starts at 0 and is carried entirely by the replayed moves.
* **Fail closed, always.** Any reject (start not established, non-mainline coordinates,
  broken FEN chain, early terminality, final position not a terminal draw) leaves the null
  in place, logs, and counts in ``rows_rejected_verification``. Never write a possibly-wrong 0.
* **Visible cohort only.** Hidden natural-ended drills also carry ``result='draw'``, but
  ``recompute_session_accuracy`` intentionally no-ops for them (they are not visible games),
  so the accuracy/CPL symptom this bead repairs does not exist for them. They are EXCLUDED
  from the repair and counted in ``hidden_draw_sessions_excluded`` for awareness.
* **Guarded sizing.** The Phase A forecast measures accuracy via the GUARDED
  :func:`app.accuracy.game_accuracy_for_rows`, never raw ``compute_game_accuracy``. The
  guarded path is what Phase B's ``recompute_session_accuracy`` persists, so the
  ``moved_off_none`` forecast is structurally unable to diverge from the persisted outcome
  (raw accuracy can score a malformed coordinate grid that the persisted path nulls).
* **Candidate binding in Phase B.** Ended sessions still accept ``/moves`` upserts, so a
  newer terminal row can be appended between phases. Phase B therefore requires the verified
  terminal ply to BE the candidate row — verifying "the chain ends in a draw" is not enough
  when the chain may have changed since Phase A.

No g-1l4p-style ordering gate applies here (unlike g-eh2w): the draw fill writes
``eval_cp=0``, which is sign-neutral.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable
from uuid import UUID

import chess
from sqlalchemy import case, not_
from sqlalchemy.orm import Session

from app.accuracy import (
    expected_total_moves_from_pgn,
    game_accuracy_for_rows,
    ply_coordinates_intact,
    recompute_session_accuracy,
)
from app.checkmate_final_ply_backfill import begin_readonly_snapshot, final_ply_rows
from app.fen import normalize_fen
from app.models import GameSession, SessionMove
from app.opening_cache import bump_evidence_seq
from app.row_locks import for_no_key_update
from app.session_contracts import is_visible_game_session, visible_session_filter

log = logging.getLogger("draw_final_ply_backfill")

# The DB stores no draw subtype — GameSession.result is a bare 'draw'.
_DRAW_RESULTS = ("draw",)

SessionFactory = Callable[[], Session]


@dataclass
class DrawVerdict:
    """A VERIFIED terminal draw: the full-chain replay reached this row and the final
    position is a terminal draw.

    :attr:`final_move_id` binds the verdict to a specific row so callers can require the
    verified terminal ply to be the row they are about to fill. The four subtype flags are
    independent — a position can satisfy several at once (e.g. a fifty-move draw that is
    also a threefold), so no precedence is forced; they exist for the ops report only and
    have no behavioral effect.
    """

    final_move_id: int
    stalemate: bool = False
    insufficient_material: bool = False
    fifty_move: bool = False
    threefold: bool = False


@dataclass
class SizingReport:
    """Phase A snapshot forecast — MEASURED via the guarded
    :func:`~app.accuracy.game_accuracy_for_rows` before/after the verified in-memory fill.

    The four candidate buckets (:attr:`moved_off_none`,
    :attr:`repaired_accuracy_already_non_null`, :attr:`residual_remains_none`,
    :attr:`rows_rejected_verification`) partition the candidate set as measured at the
    Phase A read and MUST reconcile (:meth:`reconciles`). This is a forecast pinned to that
    snapshot, NOT the exact run outcome: between phases a concurrent ``/moves`` retry can
    resolve a candidate — or append a newer final row that demotes it — and Phase B then
    drops it, so :attr:`moved_off_none` is an UPPER BOUND. Actual writes live in
    :class:`BackfillOutcome`.
    """

    # The visible draw cohort, counted from game_sessions directly so a draw session with
    # zero stored move rows (which produces no final-ply row) is still reported.
    total_draw_sessions: int = 0
    # Awareness: draw-ended sessions OUTSIDE visible_session_filter() (hidden, unconverted
    # natural-ended drills). Never selected, never filled — see the module docstring.
    hidden_draw_sessions_excluded: int = 0
    # Candidate set: final ply with eval_cp IS NULL AND eval_mate IS NULL.
    final_ply_missing_eval: int = 0
    # Candidates the full-chain verifier rejected (start not established / coordinate grid /
    # chain break / terminality) — skipped, not written, not repaired.
    rows_rejected_verification: int = 0
    # Of the verified candidates, guarded accuracy None BEFORE -> non-null AFTER the fill:
    # the games the fill moves off accuracy=None. Requires the null final ply to be the
    # player's OWN move; for draws either color can move last, so unlike checkmate this is
    # not tied to a win/loss shape.
    moved_off_none: int = 0
    # Of the verified candidates, guarded accuracy non-null both before AND after: the null
    # final ply is the OPPONENT's move. Still filled — data-integrity repair only.
    repaired_accuracy_already_non_null: int = 0
    # Of the verified candidates, guarded accuracy None both before AND after: an earlier
    # eval gap, stored rows short of the PGN ply count, or an unknown/unparseable PGN keeps
    # accuracy None even once the final ply is filled. Row still written.
    residual_remains_none: int = 0
    # Final ply already carrying eval_cp == 0 with eval_mate null — an indistinguishable
    # mix of forward-fix fills and real 0.00 worker evals (the draw analog of the checkmate
    # module's mate0_persisted). Reported for awareness; NOT a candidate (not
    # both-fields-null), NOT rewritten.
    final_ply_eval_cp_zero: int = 0
    # Verified-draw subtype counts, for the ops report only. Independent flags: a position
    # satisfying several is counted in each, so these do NOT sum to the verified total.
    verified_stalemate: int = 0
    verified_insufficient_material: int = 0
    verified_fifty_move: int = 0
    verified_threefold: int = 0

    def reconciles(self) -> bool:
        """True when the four candidate buckets sum to the candidate set — the Phase A
        snapshot forecast identity."""
        return self.final_ply_missing_eval == (
            self.moved_off_none
            + self.repaired_accuracy_already_non_null
            + self.residual_remains_none
            + self.rows_rejected_verification
        )


@dataclass
class BackfillPlan:
    """Phase A output that crosses the transaction boundary: only the sizing report plus
    stable ``(session_id, move_id)`` candidate pairs grouped by ``(user_id, player_color)``
    — never live ORM rows bound to the (now closed) read session. The ``session_id`` rides
    along so Phase B can take the parent-session lock and recompute that session's cached
    accuracy."""

    report: SizingReport
    groups: dict[tuple[int, str], list[tuple[UUID, int]]] = field(default_factory=dict)


@dataclass
class BackfillOutcome:
    """Actual Phase B write outcome — reported separately from the forecast."""

    # Rows Phase B actually wrote (verified candidates that survived every Phase B re-check).
    rows_filled_actual: int = 0
    # (user_id, player_color) groups that wrote >= 1 row and therefore bumped evidence.
    evidence_groups_bumped_actual: int = 0


@dataclass
class BackfillReport:
    sizing: SizingReport
    outcome: BackfillOutcome
    dry_run: bool


# ---------------------------------------------------------------------------
# Verification: full-chain python-chess replay from the standard start.
# ---------------------------------------------------------------------------
def _is_fifty_moves(board: chess.Board) -> bool:
    """chess.js's fifty-move rule: the halfmove clock alone, at 100 or more.

    Deliberately NOT ``board.is_fifty_moves()``. python-chess defines that as
    ``halfmove_clock >= 100 AND any legal move exists`` — its docstring says the check
    holds only when "no other means of ending the game (like checkmate) take precedence".
    chess.js's ``isDraw()`` applies no such precedence: it tests ``_halfMoves >= 100``
    outright, so a position that is BOTH stalemate and at clock 100 is both a stalemate and
    a fifty-move draw there, while python-chess would report only the stalemate.

    That precedence changes no verdict — a clock-100 position with no legal moves is either
    stalemate (already terminal) or checkmate (rejected outright) — but it would silently
    break the independent, no-forced-precedence subtype contract the ops report promises.
    """
    return board.halfmove_clock >= 100


def _is_terminal_draw(board: chess.Board) -> bool:
    """The EXPLICIT terminal-draw predicate, mirroring chess.js ``isDraw()`` — which is
    what the frontend declared terminal when it ended these games.

    Deliberately NOT ``board.is_game_over(claim_draw=True)``: python-chess's
    ``can_claim_*`` variants include claims made available by the NEXT move, which is
    broader than the frontend's rule and would bless a position the game did not actually
    end on. :func:`_is_fifty_moves` is halfmoves >= 100 and ``is_repetition(3)`` counts 3
    occurrences including the current position — chess.js's exact semantics.
    """
    return (
        board.is_stalemate()
        or board.is_insufficient_material()
        or _is_fifty_moves(board)
        or board.is_repetition(3)
    )


def _is_terminal(board: chess.Board) -> bool:
    return board.is_checkmate() or _is_terminal_draw(board)


def verify_terminal_draw(rows) -> DrawVerdict | None:
    """Replay the COMPLETE stored game from the standard start; return a
    :class:`DrawVerdict` iff it verifies as a terminal draw, else ``None``.

    ``rows`` must be ALL of one session's :class:`~app.models.SessionMove` rows, ordered
    (move_number ASC, white before black), each carrying ``id``, ``move_number``,
    ``color``, ``move_san``, ``fen_before`` and ``fen_after``.

    Rejects (fail closed) unless every one of these holds:

    1. **Established start** — the rows are the contiguous mainline coordinate grid
       (:func:`~app.accuracy_rows_v1.ply_coordinates_intact`, the same invariant the guarded
       accuracy path enforces) and the first row's ``fen_before`` is the standard start.
       A truncated prefix, a shifted/duplicated coordinate, or a non-standard start is
       exactly what makes a history-dependent draw unverifiable, so it fails here rather
       than being replayed from an arbitrary position.
    2. **Chain replay** — each row's ``fen_before`` matches the replayed board, its
       ``move_san`` is legal and unambiguous, and its ``fen_after`` matches the result.
       Links compare under :func:`~app.fen.normalize_fen` (fields 5-6 stripped) so stored
       clock-serialization differences don't spuriously reject.
    3. **Early-terminal guard** — the board is not terminal before the final row. (The
       standard start is never terminal, so the pre-loop check is subsumed by rule 1; it
       stays explicit so the invariant survives refactors.)
    4. **Terminal-draw verdict** — the final position satisfies :func:`_is_terminal_draw`.
       A final board that is CHECKMATE is rejected: ``result`` says 'draw' but the replay
       says mate, which is inconsistent data (and is the checkmate backfill's shape anyway).

    Needs no ``GameSession.pgn`` — it verifies the stored rows themselves and works when
    pgn is null.
    """
    if not rows:
        return None
    if not ply_coordinates_intact(rows):
        return None

    board = chess.Board()
    try:
        if normalize_fen(rows[0].fen_before) != normalize_fen(chess.STARTING_FEN):
            return None
    except Exception:
        # Null or malformed first fen_before: the start cannot be established.
        return None
    if _is_terminal(board):  # unreachable from the standard start; see rule 3.
        return None

    last = len(rows) - 1
    for i, row in enumerate(rows):
        try:
            if normalize_fen(board.fen()) != normalize_fen(row.fen_before):
                return None
            board.push_san(row.move_san)
            if normalize_fen(board.fen()) != normalize_fen(row.fen_after):
                return None
        except Exception:
            # Malformed FEN, or illegal/ambiguous/unparseable SAN.
            return None
        if i < last and _is_terminal(board):
            return None

    if board.is_checkmate():
        return None
    verdict = DrawVerdict(
        final_move_id=rows[last].id,
        stalemate=board.is_stalemate(),
        insufficient_material=board.is_insufficient_material(),
        fifty_move=_is_fifty_moves(board),
        threefold=board.is_repetition(3),
    )
    if not (
        verdict.stalemate
        or verdict.insufficient_material
        or verdict.fifty_move
        or verdict.threefold
    ):
        return None
    return verdict


# ---------------------------------------------------------------------------
# Phase A — read: sizing measurement + candidate selection.
# ---------------------------------------------------------------------------
@dataclass
class _SizingRow:
    """Lightweight stand-in carrying exactly the four fields
    :func:`~app.accuracy.game_accuracy_for_rows` reads, so the "after" sizing variant can
    be built with the candidate's ``eval_cp=0`` without touching a live ORM row."""

    move_number: int
    color: str
    eval_cp: int | None
    eval_mate: int | None


def _chain_rows(session: Session, session_ids: list[UUID]) -> dict[UUID, list]:
    """Load ALL SessionMove rows of the given sessions, ordered (move_number ASC, white
    before black) and grouped by session — the full chain the verifier replays and the row
    set the guarded accuracy sizing scores."""
    color_order = case((SessionMove.color == "white", 0), else_=1)
    rows = (
        session.query(
            SessionMove.id,
            SessionMove.session_id,
            SessionMove.move_number,
            SessionMove.color,
            SessionMove.move_san,
            SessionMove.fen_before,
            SessionMove.fen_after,
            SessionMove.eval_cp,
            SessionMove.eval_mate,
        )
        .filter(SessionMove.session_id.in_(session_ids))
        .order_by(
            SessionMove.session_id,
            SessionMove.move_number.asc(),
            color_order.asc(),
        )
        .all()
    )
    by_session: dict[UUID, list] = defaultdict(list)
    for r in rows:
        by_session[r.session_id].append(r)
    return by_session


def _measure_sizing(session: Session, candidates: list, report: SizingReport) -> None:
    """Verify each candidate's full chain, then bucket it by GUARDED accuracy before/after
    the verified in-memory fill.

    The guarded :func:`~app.accuracy.game_accuracy_for_rows` — never raw
    ``compute_game_accuracy`` — is what Phase B's ``recompute_session_accuracy`` persists,
    so measuring through it makes this forecast structurally unable to disagree with the
    outcome it forecasts.
    """
    session_ids = [c.session_id for c in candidates]
    if not session_ids:
        return

    pgn_by_session = dict(
        session.query(GameSession.id, GameSession.pgn)
        .filter(GameSession.id.in_(session_ids))
        .all()
    )
    moves_by_session = _chain_rows(session, session_ids)

    for c in candidates:
        session_moves = moves_by_session.get(c.session_id, [])
        verdict = verify_terminal_draw(session_moves)
        if verdict is None or verdict.final_move_id != c.move_id:
            report.rows_rejected_verification += 1
            continue

        # Independent flags, no forced precedence (a draw can be several at once).
        report.verified_stalemate += int(verdict.stalemate)
        report.verified_insufficient_material += int(verdict.insufficient_material)
        report.verified_fifty_move += int(verdict.fifty_move)
        report.verified_threefold += int(verdict.threefold)

        expected = expected_total_moves_from_pgn(pgn_by_session.get(c.session_id))
        before = game_accuracy_for_rows(
            session_moves,
            player_color=c.player_color,
            expected_total_moves=expected,
            session_id=c.session_id,
        )
        after = game_accuracy_for_rows(
            [
                _SizingRow(
                    move_number=m.move_number,
                    color=m.color,
                    eval_cp=0 if m.id == c.move_id else m.eval_cp,
                    eval_mate=m.eval_mate,
                )
                for m in session_moves
            ],
            player_color=c.player_color,
            expected_total_moves=expected,
            session_id=c.session_id,
        )

        # Filling the final ply only ADDS an eval, so a game computable before stays
        # computable after; (before non-null -> after None) is impossible and folds
        # harmlessly into residual_remains_none.
        if after is not None and before is None:
            report.moved_off_none += 1
        elif after is not None:
            report.repaired_accuracy_already_non_null += 1
        else:
            report.residual_remains_none += 1


def plan_backfill(session: Session, *, session_id: UUID | None = None) -> BackfillPlan:
    """Phase A (read). Selects the visible draw cohort's final plies, measures the sizing
    forecast, and returns the report plus candidate ids grouped by ``(user_id,
    player_color)``.

    Does NOT mutate the DB. The caller owns this read session's lifecycle and MUST end the
    read transaction (rollback) and close it before Phase B — only ids and grouping cross
    the boundary, never live ORM rows.
    """
    begin_readonly_snapshot(session)

    report = SizingReport()
    # Count the cohort DIRECTLY from game_sessions so a draw session with zero stored move
    # rows (which produces no final-ply row) is still reported.
    total_q = session.query(GameSession).filter(
        GameSession.status == "ended",
        GameSession.result.in_(_DRAW_RESULTS),
    )
    hidden_q = total_q.filter(not_(visible_session_filter()))
    total_q = total_q.filter(visible_session_filter())
    if session_id is not None:
        total_q = total_q.filter(GameSession.id == session_id)
        hidden_q = hidden_q.filter(GameSession.id == session_id)
    report.total_draw_sessions = total_q.count()
    report.hidden_draw_sessions_excluded = hidden_q.count()

    final_plies = final_ply_rows(
        session,
        results=_DRAW_RESULTS,
        session_id=session_id,
        visibility_filter=visible_session_filter(),
    )

    candidates = []
    for r in final_plies:
        if r.eval_cp == 0 and r.eval_mate is None:
            report.final_ply_eval_cp_zero += 1
        if r.eval_cp is None and r.eval_mate is None:
            candidates.append(r)
    report.final_ply_missing_eval = len(candidates)

    _measure_sizing(session, candidates, report)

    groups: dict[tuple[int, str], list[tuple[UUID, int]]] = defaultdict(list)
    for c in candidates:
        groups[(c.user_id, c.player_color)].append((c.session_id, c.move_id))

    return BackfillPlan(report=report, groups=dict(groups))


# ---------------------------------------------------------------------------
# Phase B — write: one fresh session + one transaction per group.
# ---------------------------------------------------------------------------
def _fill_session(session: Session, game_session: GameSession, move_id: int) -> bool:
    """Re-check ONE candidate under the caller's parent-session lock and fill it. Returns
    True iff the row was written.

    Every check is re-applied against the state INSIDE this transaction, because the
    ``/moves`` endpoint permits upserts on ended sessions (``api/session.py``) and both the
    candidate's evals and the session's chain may have moved since Phase A:

    1. the row still exists, still belongs to this session, and is still both-fields-null
       (under ``FOR UPDATE``, a no-op on SQLite) — a late worker retry that populated the
       REAL analysis wins and is never overwritten with a synthetic 0;
    2. the candidate is still the session's CURRENT final ply — an appended row demotes it;
    3. the full chain as read here verifies as a terminal draw AND its final row IS the
       candidate. Binding the verdict to the row is what check 3 alone cannot give: a
       session whose chain grew a new terminal ply still "ends in a draw", but the row we
       hold is no longer that ply.

    The flush order mirrors the serving move writers and is pinned by
    ``test_backfill_draw_final_ply_evals.py::test_cursor_upsert_is_last_write``:
    flush the filled move so ``recompute_session_accuracy``'s scoped SELECT sees it
    (autoflush is disabled), recompute + stamp the cached accuracy, then flush that so
    the group's evidence bump remains the transaction's final blocking statement.
    Without the recompute a repaired session already stamped at the current algo version
    with ``player_accuracy = None`` would stay cache-null forever (the Release B read
    switch skips current-version rows).
    """
    row = (
        session.query(SessionMove)
        .filter(
            SessionMove.id == move_id,
            SessionMove.session_id == game_session.id,
            SessionMove.eval_cp.is_(None),
            SessionMove.eval_mate.is_(None),
        )
        .with_for_update()
        .first()
    )
    if row is None:
        return False

    current_final = final_ply_rows(
        session,
        results=_DRAW_RESULTS,
        session_id=game_session.id,
        visibility_filter=visible_session_filter(),
    )
    if len(current_final) != 1 or current_final[0].move_id != move_id:
        log.info(
            "skip session=%s move_id=%s: no longer the session's final ply",
            game_session.id,
            move_id,
        )
        return False

    chain = _chain_rows(session, [game_session.id]).get(game_session.id, [])
    verdict = verify_terminal_draw(chain)
    if verdict is None or verdict.final_move_id != move_id:
        log.info(
            "skip session=%s move_id=%s: chain does not verify as a terminal draw ending "
            "on this row",
            game_session.id,
            move_id,
        )
        return False

    row.eval_cp = 0
    row.eval_mate = None
    # Explicit: eval_delta is an independently nullable column, so a both-eval-fields-null
    # candidate can still carry a stale non-null delta. A draw fixes the played eval at 0
    # but does NOT prove the move was best (a repetition/stalemate can squander a win), so
    # the delta stays unknown — mirroring the forward fix, which clears it for the same
    # reason. On a player-color ply this also drops an invalid Avg-CPL contribution.
    row.eval_delta = None
    session.flush()  # make the fill visible to recompute's scoped SELECT
    recompute_session_accuracy(session, game_session)
    session.flush()  # drain the cached-accuracy write ahead of the cursor bump
    return True


def apply_backfill(
    session_factory: SessionFactory,
    plan: BackfillPlan,
    *,
    dry_run: bool = False,
) -> BackfillOutcome:
    """Phase B (write). For each ``(user_id, player_color)`` group in deterministic order,
    open a FRESH session and run a single explicit transaction: for each of the group's
    sessions (in deterministic session_id order) take the parent-session ``FOR NO KEY
    UPDATE`` lock, re-check + fill + recompute its verified final ply, then bump evidence
    ONCE as the transaction's final statement when the group wrote >= 1 row. Commit (or
    roll back under ``dry_run``).

    The parent-session lock is the same one the ``/moves`` writer takes, so the sibling-row
    reads behind the re-checks are stable. Per-group commit keeps the evidence bump atomic
    with that group's row + accuracy writes, bounds lock-hold time, and makes the run
    resumable — a mid-run failure leaves committed groups repaired and reruns the rest
    idempotently. The lock order (parent session -> child move -> cursor) matches the
    serving writers, so no cycle forms and the cursor stays a pure sink after every move
    and cached-accuracy write has flushed.

    A candidate skipped by a re-check is NOT filled by this run and NOT counted; a later
    rerun's Phase A picks up the session's new final row naturally.
    """
    outcome = BackfillOutcome()

    for user_id, player_color in sorted(plan.groups.keys()):
        entries = sorted(plan.groups[(user_id, player_color)])  # deterministic lock order
        session = session_factory()
        try:
            filled = 0
            for session_id, move_id in entries:
                game_session = for_no_key_update(
                    session.query(GameSession).filter(GameSession.id == session_id)
                ).first()
                if game_session is None:
                    continue
                # Re-check the cohort predicate under the lock: a session that left the
                # ended/draw/visible cohort since Phase A has no verified terminal draw to
                # fill (e.g. a resumed or reclassified session).
                if (
                    game_session.status != "ended"
                    or game_session.result != "draw"
                    or not is_visible_game_session(game_session)
                ):
                    log.info(
                        "skip session=%s move_id=%s: no longer an ended visible draw",
                        session_id,
                        move_id,
                    )
                    continue
                if _fill_session(session, game_session, move_id):
                    filled += 1
            if filled > 0:
                # The transaction's final blocking statement (cursor pure sink): a raw
                # write that skipped the bump would leave cached opening scores falsely
                # fresh. Every move + accuracy write above is already flushed.
                bump_evidence_seq(session, user_id, player_color)
            if dry_run:
                session.rollback()
            else:
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        outcome.rows_filled_actual += filled
        if filled > 0:
            outcome.evidence_groups_bumped_actual += 1
        log.info(
            "group user_id=%s color=%s: filled=%d bumped=%s%s",
            user_id,
            player_color,
            filled,
            filled > 0,
            " (dry-run, rolled back)" if dry_run else "",
        )

    return outcome


def run_backfill(
    session_factory: SessionFactory,
    *,
    dry_run: bool = False,
    session_id: UUID | None = None,
) -> BackfillReport:
    """Public entry point. Owns the whole session lifecycle via the injected factory: one
    fresh Phase A read session (ended + closed before any write) then Phase B's per-group
    write sessions.

    Pass ``session_id`` to scope Phase A to a single session's group. ``dry_run`` writes
    nothing (each group's transaction is explicitly rolled back).
    """
    read_session = session_factory()
    try:
        plan = plan_backfill(read_session, session_id=session_id)
    finally:
        # End the read transaction and drop the session so no read snapshot / lock is held
        # across Phase B; only ids + grouping (already captured in `plan`) survive.
        read_session.rollback()
        read_session.close()

    outcome = apply_backfill(session_factory, plan, dry_run=dry_run)
    return BackfillReport(sizing=plan.report, outcome=outcome, dry_run=dry_run)
