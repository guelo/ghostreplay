"""Recompute blunder opportunity events from stored session moves.

Use after changes to opportunity semantics. The recompute is rerunnable because
events are upserted by (session_id, blunder_id) and stale events are deleted per
session by _compute_blunder_opportunity_events.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.session import (  # noqa: E402
    _compute_blunder_opportunity_events,
    _reverse_ancestor_position_ids,
    _upsert_opportunity_event,
)
from app.db import DATABASE_URL, SessionLocal  # noqa: E402
from app.fen import fen_hash  # noqa: E402
from app.models import Blunder, BlunderOpportunityEvent, GameSession, Position, SessionMove  # noqa: E402
from app.session_contracts import normal_play_started_at  # noqa: E402
from app.srs_opportunity import load_opportunity_counters  # noqa: E402
from app.srs_math import as_utc  # noqa: E402


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _session_position_ids(db, *, session_id, user_id: int) -> set[int]:
    session_moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .all()
    )
    session_hashes: set[str] = set()
    for move in session_moves:
        if move.fen_before:
            try:
                session_hashes.add(fen_hash(move.fen_before))
            except ValueError:
                pass
        if move.fen_after:
            try:
                session_hashes.add(fen_hash(move.fen_after))
            except ValueError:
                pass

    if not session_hashes:
        return set()

    return {
        row[0]
        for row in db.query(Position.id)
        .filter(Position.user_id == user_id, Position.fen_hash.in_(session_hashes))
        .all()
    }


def _cached_session_position_ids(
    db,
    *,
    session_id,
    user_id: int,
    cache: dict | None,
) -> set[int]:
    if cache is not None and session_id in cache:
        return cache[session_id]
    position_ids = _session_position_ids(db, session_id=session_id, user_id=user_id)
    if cache is not None:
        cache[session_id] = position_ids
    return position_ids


def recompute_one_blunder(
    db,
    *,
    blunder_id: int,
    progress_every: int = 100,
    session_position_cache: dict | None = None,
) -> tuple[int, int, int]:
    blunder = db.query(Blunder).filter(Blunder.id == blunder_id).first()
    if blunder is None:
        blunder_count = db.query(Blunder.id).count()
        raise RuntimeError(
            f"Blunder not found: {blunder_id}. "
            f"Connected database has {blunder_count} blunders. "
            f"DATABASE_URL={_safe_database_url(DATABASE_URL)}"
        )
    blunder_position = db.query(Position).filter(Position.id == blunder.position_id).first()
    if blunder_position is None:
        raise RuntimeError(f"Position not found for blunder: {blunder_id}")

    sessions = (
        db.query(GameSession)
        .join(SessionMove, SessionMove.session_id == GameSession.id)
        .filter(
            GameSession.user_id == blunder.user_id,
            GameSession.player_color == blunder_position.active_color,
        )
        .distinct()
        .order_by(GameSession.started_at.asc(), GameSession.id.asc())
        .all()
    )
    ancestor_ids = _reverse_ancestor_position_ids(
        db,
        start_position_id=blunder.position_id,
        player_color=blunder_position.active_color,
        user_id=blunder.user_id,
    )

    existing_events = {
        event.session_id: event
        for event in db.query(BlunderOpportunityEvent)
        .filter(BlunderOpportunityEvent.blunder_id == blunder.id)
        .all()
    }

    opportunities = 0
    reached_count = 0
    for index, session in enumerate(sessions, start=1):
        occurred_at = normal_play_started_at(session)
        if blunder.created_at and as_utc(occurred_at) < as_utc(blunder.created_at):
            existing = existing_events.get(session.id)
            if existing is not None:
                db.delete(existing)
            continue

        position_ids = _cached_session_position_ids(
            db,
            session_id=session.id,
            user_id=blunder.user_id,
            cache=session_position_cache,
        )
        reached = blunder.position_id in position_ids
        opportunity = reached or bool(position_ids.intersection(ancestor_ids))

        existing = existing_events.get(session.id)
        if opportunity:
            opportunities += 1
            reached_count += 1 if reached else 0
            _upsert_opportunity_event(
                db,
                session_id=session.id,
                blunder_id=blunder.id,
                occurred_at=occurred_at,
                opportunity=True,
                reached=reached,
            )
        elif existing is not None:
            db.delete(existing)

        if progress_every > 0 and index % progress_every == 0:
            db.commit()
            print(
                f"blunder={blunder.id} sessions={index}/{len(sessions)} "
                f"opportunities={opportunities} reached={reached_count}",
                flush=True,
            )

    db.commit()
    return len(sessions), opportunities, reached_count


def recompute_srs_opportunities(
    db,
    *,
    user_id: int | None = None,
    limit: int | None = None,
    progress_every: int = 100,
) -> int:
    query = (
        db.query(GameSession)
        .join(SessionMove, SessionMove.session_id == GameSession.id)
        .distinct()
    )
    if user_id is not None:
        query = query.filter(GameSession.user_id == user_id)

    sessions = (
        query
        .distinct()
        .order_by(GameSession.started_at.asc(), GameSession.id.asc())
        .limit(limit)
        .all()
    )

    for index, session in enumerate(sessions, start=1):
        _compute_blunder_opportunity_events(
            db,
            session_id=session.id,
            user_id=session.user_id,
            player_color=session.player_color,
        )
        # The function no longer self-commits (the live path times the commit as a
        # distinct stage); commit per session to preserve incremental durability.
        db.commit()
        if progress_every > 0 and index % progress_every == 0:
            print(f"sessions={index}/{len(sessions)}", flush=True)

    return len(sessions)


def recompute_all_blunders(
    db,
    *,
    user_id: int | None = None,
    limit: int | None = None,
    progress_every: int = 10,
) -> tuple[int, int, int, int]:
    query = db.query(Blunder.id)
    if user_id is not None:
        query = query.filter(Blunder.user_id == user_id)

    blunder_ids = [
        row[0]
        for row in query
        .order_by(Blunder.user_id.asc(), Blunder.id.asc())
        .limit(limit)
        .all()
    ]

    total_sessions = 0
    total_opportunities = 0
    total_reached = 0
    # Session position-id sets are independent of the blunder, so compute each
    # one lazily on first scan and reuse it across all blunders.
    session_position_cache: dict = {}
    for index, blunder_id in enumerate(blunder_ids, start=1):
        sessions, opportunities, reached = recompute_one_blunder(
            db,
            blunder_id=blunder_id,
            progress_every=0,
            session_position_cache=session_position_cache,
        )
        total_sessions += sessions
        total_opportunities += opportunities
        total_reached += reached
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"blunders={index}/{len(blunder_ids)} "
                f"session_scans={total_sessions} "
                f"opportunities={total_opportunities} reached={total_reached}",
                flush=True,
            )

    return len(blunder_ids), total_sessions, total_opportunities, total_reached


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, help="Only recompute sessions for one user.")
    parser.add_argument("--blunder-id", type=int, help="Only recompute one blunder.")
    parser.add_argument(
        "--all-blunders",
        action="store_true",
        help="Recompute all blunders, optionally filtered by --user-id.",
    )
    parser.add_argument("--limit", type=int, help="Maximum sessions or blunders to recompute.")
    parser.add_argument("--progress-every", type=int, default=100, help="Progress print interval.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        if args.blunder_id is not None:
            total, opportunities, reached = recompute_one_blunder(
                db,
                blunder_id=args.blunder_id,
                progress_every=args.progress_every,
            )
            counters = load_opportunity_counters(db, [args.blunder_id]).get(args.blunder_id)
            print(
                f"Recomputed blunder {args.blunder_id}: sessions={total} "
                f"matched_opportunities={opportunities} matched_reached={reached}"
            )
            if counters is not None:
                print(
                    "Counters: "
                    f"opportunities_since_review={counters.opportunities_since_review} "
                    f"opportunities_30d={counters.opportunities_30d} "
                    f"reached_30d={counters.reached_30d} "
                    f"p_reach={counters.p_reach:.4f}"
                )
        elif args.all_blunders:
            blunders, session_scans, opportunities, reached = recompute_all_blunders(
                db,
                user_id=args.user_id,
                limit=args.limit,
                progress_every=args.progress_every,
            )
            print(
                f"Recomputed {blunders} blunders: session_scans={session_scans} "
                f"matched_opportunities={opportunities} matched_reached={reached}"
            )
        else:
            raise RuntimeError(
                "Choose --blunder-id for targeted cleanup or --all-blunders for full cleanup. "
                "The old implicit full-session recompute is intentionally disabled."
            )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
