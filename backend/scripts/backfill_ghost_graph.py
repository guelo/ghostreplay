"""Backfill the ghost steering graph from historical session moves.

Production session_moves hold fen_before / move_san / fen_after edges that were
recorded before the graph was taught on upload. This script replays the same
validated insertion logic used live (_upsert_session_position_graph) against
existing session_moves so historical play improves ghost steering immediately.

Rerunnable / idempotent: positions dedupe by (user_id, fen_hash) and moves by
(from_position_id, move_san), so a second run inserts nothing new.

After this backfill, recompute opportunity events so newly-reachable blunders
become due:
    python scripts/recompute_srs_opportunities.py --all-blunders

NOT lock-coordinated (g-q0aw): this calls ``_upsert_session_position_graph``
DIRECTLY, bypassing the live orchestrator, so it does NOT take the per-user
``pg_advisory_xact_lock(user_id)`` and is NOT subject to lock_timeout /
statement_timeout. That is intentional — this is a single-threaded admin
migration over large graphs. It MUST NOT be run concurrently with live uploads:
without the advisory lock it would race the live path's graph writes on the
(user_id, fen_hash) unique index. Run it during a quiet window / maintenance.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataclasses import dataclass  # noqa: E402

from app.api.session import (  # noqa: E402
    GhostGraphUpsertStats,
    _upsert_session_position_graph,
)
from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.models import GameSession, SessionMove  # noqa: E402


@dataclass
class _MoveRow:
    """Duck-typed stand-in for SessionMoveInput; only graph-relevant fields loaded."""

    fen_before: str | None
    move_san: str
    fen_after: str | None


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _color_order(color: str | None) -> int:
    """White ply before black ply so an edge's fen_before follows the prior fen_after."""
    return 0 if color == "white" else 1


def backfill_ghost_graph(
    db,
    *,
    user_id: int | None = None,
    limit: int | None = None,
    batch_size: int = 200,
    progress_every: int = 100,
    dry_run: bool = False,
) -> GhostGraphUpsertStats:
    """Replay validated graph insertion over historical sessions.

    Processed per session (each session belongs to one user, so positions stay
    user-scoped). When dry_run, never commits — a single rollback runs at the end.
    """
    query = db.query(GameSession)
    if user_id is not None:
        query = query.filter(GameSession.user_id == user_id)

    sessions = (
        query.join(SessionMove, SessionMove.session_id == GameSession.id)
        .distinct()
        .order_by(GameSession.started_at.asc(), GameSession.id.asc())
        .limit(limit)
        .all()
    )

    totals = GhostGraphUpsertStats()
    for index, session in enumerate(sessions, start=1):
        rows = (
            db.query(
                SessionMove.move_number,
                SessionMove.color,
                SessionMove.fen_before,
                SessionMove.move_san,
                SessionMove.fen_after,
            )
            .filter(SessionMove.session_id == session.id)
            .all()
        )
        # Sort so each ply's fen_before is seen after the prior ply's fen_after.
        rows = sorted(rows, key=lambda r: (r.move_number, _color_order(r.color)))
        moves = [
            _MoveRow(fen_before=r.fen_before, move_san=r.move_san, fen_after=r.fen_after)
            for r in rows
            if r.fen_before and r.fen_after
        ]
        if not moves:
            continue

        # Direct call (not via the orchestrator): no advisory lock / timeouts.
        # See the module docstring — never run concurrently with live uploads.
        stats = _upsert_session_position_graph(
            db,
            user_id=session.user_id,
            moves=moves,
        )
        totals.valid_moves += stats.valid_moves
        totals.invalid_moves += stats.invalid_moves
        totals.positions_created += stats.positions_created
        totals.edges_created += stats.edges_created
        totals.edges_existing += stats.edges_existing

        # Session autoflush is off; flush so the next session's dedup query sees
        # edges this batch already added (avoids duplicate-key inserts on commit).
        db.flush()

        if not dry_run and batch_size > 0 and index % batch_size == 0:
            db.commit()

        if progress_every > 0 and index % progress_every == 0:
            print(
                f"sessions={index}/{len(sessions)} "
                f"valid={totals.valid_moves} invalid={totals.invalid_moves} "
                f"positions={totals.positions_created} edges={totals.edges_created} "
                f"existing={totals.edges_existing}",
                flush=True,
            )

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return totals


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, help="Only backfill sessions for one user.")
    parser.add_argument("--limit", type=int, help="Maximum sessions to process (smoke runs).")
    parser.add_argument(
        "--batch-size", type=int, default=200, help="Sessions per commit (real runs only)."
    )
    parser.add_argument("--progress-every", type=int, default=100, help="Progress print interval.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Never commit; roll back at the end."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"DATABASE_URL={_safe_database_url(DATABASE_URL)}", flush=True)
    db = SessionLocal()
    try:
        totals = backfill_ghost_graph(
            db,
            user_id=args.user_id,
            limit=args.limit,
            batch_size=args.batch_size,
            progress_every=args.progress_every,
            dry_run=args.dry_run,
        )
        mode = "DRY RUN (rolled back)" if args.dry_run else "committed"
        print(
            f"Backfill {mode}: valid_moves={totals.valid_moves} "
            f"invalid_moves={totals.invalid_moves} "
            f"positions_created={totals.positions_created} "
            f"edges_created={totals.edges_created} "
            f"edges_existing={totals.edges_existing}"
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
