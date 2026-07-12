"""One-time data-repair backfill for historical checkmate final-ply evals (g-eh2w).

The g-hs78 forward fix deterministically fills the terminal final ply at game end,
so NEW checkmate games never persist a null final ply. But it cannot restore
historical data — those evals were never persisted. A checkmate-ended session whose
final :class:`~app.models.SessionMove` row has ``eval_cp IS NULL AND eval_mate IS
NULL`` keeps whole-game accuracy ``None`` forever (``compute_game_accuracy`` nulls
the whole game on a missing player transition; :mod:`app.accuracy`) and drops that
ply from the Avg-CPL denominator.

This module owns the selection / sizing / verify / fill / evidence-bump logic and
the read/write transaction orchestration; :mod:`scripts.backfill_checkmate_final_ply_evals`
is a thin argparse CLI over :func:`run_backfill`.

Design contracts (see the g-eh2w bead design field for the full plan):

* **The public entry point takes a session FACTORY, never a live Session.** The
  factory (a zero-arg callable / ``sessionmaker``) returns a FRESH session per call.
  The module owns every session's creation, commit, rollback, and close: one Phase A
  read session (ended/closed before any write), then one fresh Phase B write session
  per ``(user_id, player_color)`` group. A single caller-supplied Session could not
  satisfy that "fresh read session + fresh write session per group" structure.
* **Portable final-ply selection** via a SQLAlchemy ``row_number()`` window query
  (no PostgreSQL ``DISTINCT ON``), so the exact selection path is exercised by the
  SQLite tests.
* **One shared missing-eval predicate everywhere** — ``eval_cp IS NULL AND eval_mate
  IS NULL`` — matching ``compute_game_accuracy``'s own missing-post-move-eval rule
  (either field alone supplies the eval). Used for BOTH candidate selection and the
  sizing measurement, so an ordinary centipawn row is never mistaken for a gap.
* **Verify before fill** — replay ``fen_before + move_san`` (python-chess) and accept
  only when the replay is checkmate AND ``normalize_fen(replayed) ==
  normalize_fen(fen_after)`` (the same normalized comparison the session-replay
  validator uses). A row failing either check is skipped, logged, and counted in
  ``rows_rejected_verification`` — never written.
* **Fill** a verified row with ``eval_mate=0, eval_cp=+10000, eval_delta=0``
  (mover-relative; a mating move's loss is deterministically 0), then **recompute
  the session's cached ``player_accuracy``** in the same transaction. Release A
  persists ``game_sessions.player_accuracy`` in the serving move writers
  (:func:`app.accuracy.recompute_session_accuracy`); a repaired session already
  stamped ``player_accuracy_algo_version = current`` with ``player_accuracy = None``
  would otherwise be skipped by the Release B read switch and serve null forever.
  The fill therefore mirrors the serving writers' parent-session lock
  (:func:`app.row_locks.for_no_key_update`) and flush order (SPEC §7.4): flush the
  move, recompute accuracy, flush accuracy, THEN bump evidence.
* **Evidence bump** once per group that wrote >= 1 row, as the transaction's final
  blocking statement (the ``opening_score_cursors`` pure sink, SPEC §7.4) — after
  every move + accuracy write of the group has been flushed.
* **Concurrency-safe / idempotent** — Phase B holds the parent-session
  ``FOR NO KEY UPDATE`` lock and re-applies the both-fields-null predicate under
  ``SELECT ... FOR UPDATE`` (both no-ops on SQLite) so a candidate a concurrent
  ``/moves`` retry resolved with real worker values between Phase A and Phase B is
  DROPPED rather than overwritten with the synthetic ``+10000/0/0``.
* **Consistent Phase A forecast** — the multi-statement sizing read runs in a
  REPEATABLE READ read-only transaction on PostgreSQL, so every statement sees one
  snapshot (the default READ COMMITTED would let each statement see different
  committed data). A no-op on SQLite, whose single-connection read is already
  consistent within the transaction.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable
from uuid import UUID

import chess
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.accuracy import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    recompute_session_accuracy,
)
from app.fen import normalize_fen
from app.models import GameSession, SessionMove
from app.opening_cache import bump_evidence_seq
from app.row_locks import for_no_key_update

log = logging.getLogger("checkmate_final_ply_backfill")

# Centipawn magnitude a mating move is forced to before clamping — mirrors
# app.accuracy_v1._MATE_CP (private there; duplicated as a fill constant so we do
# not import a frozen-snapshot private). Written mover-relative as +10000.
MATE_EVAL_CP = 10000

# The checkmate-ended results whose final ply this backfill repairs.
_CHECKMATE_RESULTS = ("checkmate_win", "checkmate_loss")

SessionFactory = Callable[[], Session]


@dataclass
class SizingReport:
    """Phase A snapshot forecast — MEASURED via ``compute_game_accuracy`` before/after
    the verified in-memory fill, never inferred from raw null counts.

    The four candidate buckets (:attr:`moved_off_none`,
    :attr:`repaired_accuracy_already_non_null`, :attr:`residual_remains_none`,
    :attr:`rows_rejected_verification`) partition the candidate set as measured at the
    Phase A read and MUST reconcile (:meth:`reconciles`). This is a forecast pinned to
    that snapshot, NOT the exact run outcome: between phases a concurrent ``/moves``
    retry can resolve a candidate that Phase B then drops, so :attr:`moved_off_none` is
    an UPPER BOUND on games actually moved off ``None`` — the actual writes live in
    :class:`BackfillOutcome`.
    """

    total_checkmate_sessions: int = 0
    # Candidate set: final ply with eval_cp IS NULL AND eval_mate IS NULL.
    final_ply_missing_eval: int = 0
    # Candidate rows whose replay is not checkmate or whose FEN disagrees with
    # fen_after under normalize_fen — skipped, not written, not repaired.
    rows_rejected_verification: int = 0
    # Of the verified candidates, accuracy None BEFORE -> non-null AFTER the fill:
    # the games the fill moves off accuracy=None (only possible when the player
    # DELIVERED mate, so the null final ply is the player's own move).
    moved_off_none: int = 0
    # Of the verified candidates, accuracy non-null both before AND after: the
    # checkmate-LOSS shape (null final ply is the OPPONENT's mating move). The row is
    # still filled +10000/0/0 (data-integrity repair), but whole-game accuracy was
    # never None and the player's Avg CPL is unaffected (the filled ply is not the
    # player's color).
    repaired_accuracy_already_non_null: int = 0
    # Of the verified candidates, accuracy None both before AND after: an earlier eval
    # gap, stored rows short of the PGN ply count, or an unknown/unparseable PGN keeps
    # accuracy None even once the final ply is filled. Row still written; accuracy
    # stays None.
    residual_remains_none: int = 0
    # Final ply whose eval_mate is already 0 (e.g. a g-1l4p-era race that persisted a
    # mate-0 row). Reported for awareness; NOT a candidate (not both-fields-null), NOT
    # rewritten.
    mate0_persisted: int = 0

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
    """Phase A output that crosses the transaction boundary: only the sizing report
    plus stable ``(session_id, move_id)`` candidate pairs grouped by ``(user_id,
    player_color)`` — never live ORM rows bound to the (now closed) read session. The
    ``session_id`` rides along so Phase B can take the parent-session lock and recompute
    that session's cached accuracy."""

    report: SizingReport
    groups: dict[tuple[int, str], list[tuple[UUID, int]]] = field(default_factory=dict)


@dataclass
class BackfillOutcome:
    """Actual Phase B write outcome — reported separately from the forecast."""

    # Rows Phase B actually wrote (verified candidates that survived the Phase B
    # re-select, i.e. still both-fields-null under lock at write time).
    rows_filled_actual: int = 0
    # (user_id, player_color) groups that wrote >= 1 row and therefore bumped evidence.
    evidence_groups_bumped_actual: int = 0


@dataclass
class BackfillReport:
    sizing: SizingReport
    outcome: BackfillOutcome
    dry_run: bool


# ---------------------------------------------------------------------------
# Verification: python-chess replay of a single final ply.
# ---------------------------------------------------------------------------
def _verify_checkmate(fen_before: str | None, move_san: str, fen_after: str) -> bool:
    """True iff replaying ``fen_before`` + ``move_san`` yields checkmate AND the
    replayed position matches ``fen_after`` under :func:`normalize_fen`.

    ``normalize_fen`` strips the halfmove clock and fullmove number (fields 5-6), so a
    valid historical row is not rejected merely because its stored clocks differ from
    python-chess's serialization. A null ``fen_before`` (nullable column) cannot be
    replayed and is rejected.
    """
    if not fen_before:
        return False
    try:
        board = chess.Board(fen_before)
        board.push_san(move_san)
    except Exception:
        return False
    if not board.is_checkmate():
        return False
    try:
        return normalize_fen(board.fen()) == normalize_fen(fen_after)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Phase A — read: sizing measurement + candidate selection.
# ---------------------------------------------------------------------------
def _begin_readonly_snapshot(session: Session) -> None:
    """On PostgreSQL, start Phase A as a REPEATABLE READ read-only transaction so the
    multi-statement sizing forecast reflects ONE consistent snapshot.

    PostgreSQL's default READ COMMITTED would let the final-ply selection, the cohort
    count, and the per-session PGN / move reads each see different committed data — a
    concurrent ``/moves`` upload could then produce a forecast that corresponds to no
    single snapshot. REPEATABLE READ pins them to one snapshot; ``READ ONLY`` documents
    and enforces that Phase A never writes. Must run before the first statement (setting
    the isolation level mid-transaction raises). A no-op on SQLite, whose single
    StaticPool connection already reads consistently within the transaction and which
    does not support the REPEATABLE READ isolation level.
    """
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.connection(
            execution_options={
                "isolation_level": "REPEATABLE READ",
                "postgresql_readonly": True,
            }
        )


def _final_ply_rows(session: Session, *, session_id: UUID | None):
    """Portable ``row_number()`` selection of the final ply of every checkmate-ended
    session (no ``DISTINCT ON``). The final ply per session is the highest move_number,
    black after white within the same move_number.
    """
    ply_rank = func.row_number().over(
        partition_by=SessionMove.session_id,
        order_by=[
            SessionMove.move_number.desc(),
            # white=0, black=1; DESC -> black (the later ply) ranks first.
            case((SessionMove.color == "white", 0), else_=1).desc(),
        ],
    ).label("ply_rank")

    inner = (
        session.query(
            SessionMove.id.label("move_id"),
            SessionMove.session_id.label("session_id"),
            SessionMove.eval_cp.label("eval_cp"),
            SessionMove.eval_mate.label("eval_mate"),
            SessionMove.fen_before.label("fen_before"),
            SessionMove.move_san.label("move_san"),
            SessionMove.fen_after.label("fen_after"),
            GameSession.user_id.label("user_id"),
            GameSession.player_color.label("player_color"),
            ply_rank,
        )
        .join(GameSession, GameSession.id == SessionMove.session_id)
        .filter(
            GameSession.status == "ended",
            GameSession.result.in_(_CHECKMATE_RESULTS),
        )
    )
    if session_id is not None:
        inner = inner.filter(SessionMove.session_id == session_id)
    subq = inner.subquery()

    return (
        session.query(
            subq.c.move_id,
            subq.c.session_id,
            subq.c.eval_cp,
            subq.c.eval_mate,
            subq.c.fen_before,
            subq.c.move_san,
            subq.c.fen_after,
            subq.c.user_id,
            subq.c.player_color,
        )
        .filter(subq.c.ply_rank == 1)
        .all()
    )


def _measure_sizing(session: Session, candidates: list, report: SizingReport) -> None:
    """Bucket each candidate by ``compute_game_accuracy`` before/after the VERIFIED
    in-memory fill, using the same shared accuracy path the API uses.
    """
    session_ids = [c.session_id for c in candidates]
    if not session_ids:
        return

    pgn_by_session = dict(
        session.query(GameSession.id, GameSession.pgn)
        .filter(GameSession.id.in_(session_ids))
        .all()
    )

    color_order = case((SessionMove.color == "white", 0), else_=1)
    move_rows = (
        session.query(
            SessionMove.id,
            SessionMove.session_id,
            SessionMove.color,
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
    moves_by_session: dict = defaultdict(list)
    for m in move_rows:
        moves_by_session[m.session_id].append(m)

    for c in candidates:
        if not _verify_checkmate(c.fen_before, c.move_san, c.fen_after):
            report.rows_rejected_verification += 1
            continue

        session_moves = moves_by_session.get(c.session_id, [])
        expected = expected_total_moves_from_pgn(pgn_by_session.get(c.session_id))

        before = compute_game_accuracy(
            [AccuracyMove(color=m.color, eval_cp=m.eval_cp, eval_mate=m.eval_mate) for m in session_moves],
            player_color=c.player_color,
            expected_total_moves=expected,
        )
        after = compute_game_accuracy(
            [
                AccuracyMove(
                    color=m.color,
                    eval_cp=MATE_EVAL_CP if m.id == c.move_id else m.eval_cp,
                    eval_mate=0 if m.id == c.move_id else m.eval_mate,
                )
                for m in session_moves
            ],
            player_color=c.player_color,
            expected_total_moves=expected,
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
    """Phase A (read). Selects the checkmate final plies, measures the sizing forecast,
    and returns the report plus candidate ids grouped by ``(user_id, player_color)``.

    Does NOT mutate the DB. The caller owns this read session's lifecycle and MUST end
    the read transaction (rollback) and close it before Phase B — only ids and grouping
    cross the boundary, never live ORM rows.
    """
    _begin_readonly_snapshot(session)

    report = SizingReport()
    # Count the checkmate cohort DIRECTLY from game_sessions so a checkmate session with
    # zero stored move rows (which produces no final-ply row) is still reported.
    total_q = session.query(GameSession).filter(
        GameSession.status == "ended",
        GameSession.result.in_(_CHECKMATE_RESULTS),
    )
    if session_id is not None:
        total_q = total_q.filter(GameSession.id == session_id)
    report.total_checkmate_sessions = total_q.count()

    final_plies = _final_ply_rows(session, session_id=session_id)

    candidates = []
    for r in final_plies:
        if r.eval_mate == 0:
            report.mate0_persisted += 1
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
    """Re-select ONE candidate final ply by id, re-applying the both-fields-null
    predicate under ``FOR UPDATE`` (a no-op on SQLite); verify + fill it, then recompute
    the session's cached ``player_accuracy``. Returns True iff the row was written.

    Re-applying the null predicate here is REQUIRED for correctness, not an
    optimization: the ``/moves`` endpoint permits upserts on ended sessions and can
    overwrite eval fields, so a late worker retry may populate the REAL analysis for a
    candidate between Phase A's select and this write. Re-checking both-fields-null (with
    the parent session already locked ``FOR NO KEY UPDATE`` by the caller) drops any
    concurrently-resolved row, so the backfill never overwrites a real worker eval with
    the synthetic +10000/0/0.

    The flush order mirrors the serving move writers (SPEC §7.4): flush the filled move
    so ``recompute_session_accuracy``'s scoped SELECT sees it (autoflush is disabled),
    recompute + stamp the cached accuracy, then flush that so the group's evidence bump
    remains the transaction's final blocking statement. Without the recompute a repaired
    session already stamped at the current algo version with ``player_accuracy = None``
    would stay cache-null forever (the Release B read switch skips current-version rows).
    """
    row = (
        session.query(SessionMove)
        .filter(
            SessionMove.id == move_id,
            SessionMove.eval_cp.is_(None),
            SessionMove.eval_mate.is_(None),
        )
        .with_for_update()
        .first()
    )
    if row is None:
        return False
    if not _verify_checkmate(row.fen_before, row.move_san, row.fen_after):
        log.info(
            "skip session=%s move_id=%s: replay not a verified checkmate",
            row.session_id,
            row.id,
        )
        return False

    row.eval_mate = 0
    row.eval_cp = MATE_EVAL_CP
    row.eval_delta = 0
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
    """Phase B (write). For each ``(user_id, player_color)`` group in deterministic
    order, open a FRESH session and run a single explicit transaction: for each of the
    group's sessions (in deterministic session_id order) take the parent-session
    ``FOR NO KEY UPDATE`` lock, fill + recompute its verified final ply, then bump
    evidence ONCE as the transaction's final statement when the group wrote >= 1 row.
    Commit (or roll back under ``dry_run``).

    Per-group commit keeps the evidence bump atomic with that group's row + accuracy
    writes, bounds lock-hold time, and makes the run resumable — a mid-run failure leaves
    committed groups repaired and reruns the rest idempotently. The lock order (parent
    session -> child move -> cursor) matches the serving writers, so no cycle forms and
    the cursor stays a pure sink (SPEC §7.4).
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
    """Public entry point. Owns the whole session lifecycle via the injected factory:
    one fresh Phase A read session (ended + closed before any write) then Phase B's
    per-group write sessions.

    Pass ``session_id`` to scope Phase A to a single session's group. ``dry_run`` writes
    nothing (each group's transaction is explicitly rolled back).
    """
    read_session = session_factory()
    try:
        plan = plan_backfill(read_session, session_id=session_id)
    finally:
        # End the read transaction and drop the session so no read snapshot / lock is
        # held across Phase B; only ids + grouping (already captured in `plan`) survive.
        read_session.rollback()
        read_session.close()

    outcome = apply_backfill(session_factory, plan, dry_run=dry_run)
    return BackfillReport(sizing=plan.report, outcome=outcome, dry_run=dry_run)
