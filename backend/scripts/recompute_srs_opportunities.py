"""Recompute boundary-scoped broad opportunity events from stored session moves.

Use after changes to opportunity semantics. The recompute is rerunnable because
events are upserted by (session_id, blunder_id) and stale events are deleted per
session by _compute_blunder_opportunity_events.

All four explicit modes use ``app.evidence_boundary``. Session-grain modes call the
runtime writer itself and are the preferred historical cleanup because one committed
session pass can retire every stale pre-boundary row. Blunder-grain modes remain useful
for targeted repair and use the reverse-walk dual of the same seed/reach sets.

Creation-time treatment intentionally differs by grain. Blunder-grain repair drops
events whose session evidence predates ``blunder.created_at``; session-grain cleanup
preserves live-writer parity and can retain a broad row for a session that started
before a blunder was created but uploaded after it. Targeted counters exclude that
pre-creation evidence independently.

Retention freeze: every mode refuses to rewrite a session whose broad opportunity
evidence is past the mutation boundary or at/below this user's permanent fold
prefix. Skipped sessions are printed and counted, never silently dropped and
never retried: the raw rows behind a fold prefix no longer exist, so rewriting
that session would recreate rows a summary has already absorbed. Both freeze
switches ship disabled, so until the retention rollout enables them only an
already-folded prefix can cause a skip. Every mode takes the per-user graph
write lock before deciding, because the event guards cover DELETE only: without
it a fold committing between the freeze check and the upsert would let a repair
recreate rows a summary has already absorbed. See
``backend/app/opportunity_retention.py``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
import uuid
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
from app.graph_write_lock import acquire_graph_write_lock  # noqa: E402
from app.evidence_boundary import session_evidence_hashes  # noqa: E402
from app.opportunity_store import frozen_session_ids  # noqa: E402
from app.models import (  # noqa: E402
    Blunder,
    BlunderOpportunityEvent,
    GameSession,
    Position,
    SessionMove,
)
from app.session_contracts import normal_play_started_at  # noqa: E402
from app.srs_opportunity import load_opportunity_counters  # noqa: E402
from app.srs_math import as_utc  # noqa: E402
from app import srs_write_telemetry as srs_telemetry  # noqa: E402


def _lock_user(db, user_id: int) -> None:
    """Take the per-user graph write lock this repair is about to write behind.

    Same lock, same order and same transaction scope as the upload path and the
    deferred evidence worker, so a repair cannot interleave with either of them
    or with a fold. It releases at the caller's next commit, which is why every
    commit in this module is followed by retaking it.
    """
    acquire_graph_write_lock(
        db, user_id=user_id, dialect_name=db.get_bind().dialect.name
    )


def _safe_database_url(database_url: str) -> str:
    parts = urlsplit(database_url)
    netloc = parts.netloc
    if "@" in netloc:
        credentials, host = netloc.rsplit("@", 1)
        username = credentials.split(":", 1)[0]
        netloc = f"{username}:***@{host}" if username else f"***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@dataclass(frozen=True)
class SessionPositionIds:
    """Resolved database ids for the shared boundary helper's two roles."""

    seed: frozenset[int]
    reach: frozenset[int]


@dataclass(frozen=True)
class SessionRecomputeReport:
    processed_sessions: int
    last_session_id: uuid.UUID | None
    # Sessions the retention policy refused, reported rather than swallowed: an
    # operator repairing a range needs to know that part of it was immutable,
    # not read a clean-looking total that quietly covered fewer sessions.
    frozen_sessions: int = 0


def _session_evidence_position_ids(
    db,
    *,
    session: GameSession,
    user_id: int,
) -> SessionPositionIds:
    if session.user_id != user_id:
        raise ValueError("session and position owner must match")

    evidence = session_evidence_hashes(db, session)
    if not evidence.seed:
        return SessionPositionIds(seed=frozenset(), reach=frozenset())

    rows = (
        db.query(Position.id, Position.fen_hash)
        .filter(
            Position.user_id == user_id,
            Position.fen_hash.in_(evidence.seed),
        )
        .all()
    )
    return SessionPositionIds(
        seed=frozenset(row[0] for row in rows),
        reach=frozenset(row[0] for row in rows if row[1] in evidence.reach),
    )


def _cached_session_position_ids(
    db,
    *,
    session: GameSession,
    user_id: int,
    cache: dict[uuid.UUID, SessionPositionIds] | None,
) -> SessionPositionIds:
    if cache is not None and session.id in cache:
        return cache[session.id]
    position_ids = _session_evidence_position_ids(
        db,
        session=session,
        user_id=user_id,
    )
    if cache is not None:
        cache[session.id] = position_ids
    return position_ids


@srs_telemetry.repair_operation("repair_blunder")
def recompute_one_blunder(
    db,
    *,
    blunder_id: int,
    progress_every: int = 100,
    session_position_cache: dict[uuid.UUID, SessionPositionIds] | None = None,
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

    # The freeze check is only sound BEHIND this lock. It samples the database
    # clock, but the event write/delete guards cover DELETE alone, so without
    # serialization against the compactor a fold could commit between the check
    # and the upsert and this repair would recreate rows a summary has already
    # absorbed. Every other evidence writer takes the same per-user lock; the
    # repair paths were the one hole. No-op outside PostgreSQL.
    _lock_user(db, blunder.user_id)

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
    # One batched freeze decision for this blunder's session set. The
    # blunder-grain modes mutate event rows directly instead of going through
    # _compute_blunder_opportunity_events, so they need the guard explicitly —
    # and they need it BEFORE the delete and the upsert below, not after.
    frozen = frozen_session_ids(
        db,
        user_id=blunder.user_id,
        session_ids=[session.id for session in sessions],
    )
    frozen_count = 0
    for index, session in enumerate(sessions, start=1):
        if session.id in frozen:
            frozen_count += 1
            # Same seam the runtime choke point uses, so a repair skip and an
            # upload skip are one observation shape rather than two.
            srs_telemetry.frozen_attempt(db, session)
            continue
        occurred_at = normal_play_started_at(session)
        if blunder.created_at and as_utc(occurred_at) < as_utc(blunder.created_at):
            existing = existing_events.get(session.id)
            if existing is not None:
                srs_telemetry.observe_session(db, session)
                srs_telemetry.evidence_mutated(db, session.id)
                db.delete(existing)
            continue

        position_ids = _cached_session_position_ids(
            db,
            session=session,
            user_id=blunder.user_id,
            cache=session_position_cache,
        )
        reached = blunder.position_id in position_ids.reach
        opportunity = reached or bool(position_ids.seed.intersection(ancestor_ids))

        existing = existing_events.get(session.id)
        if opportunity:
            srs_telemetry.observe_session(db, session)
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
            srs_telemetry.observe_session(db, session)
            srs_telemetry.evidence_mutated(db, session.id)
            db.delete(existing)

        if progress_every > 0 and index % progress_every == 0:
            db.commit()
            # The advisory lock is transaction-scoped, so that commit dropped
            # it. Retake it before re-asking, or the next page would check the
            # freeze unserialized — exactly the hole the lock closes.
            _lock_user(db, blunder.user_id)
            # Re-ask after every commit. The freeze decision is a database-clock
            # sample, so a batch that spans the deadline must not reuse the
            # verdict it got before it: a session that froze while this page ran
            # would otherwise be rewritten by the next page of the same run.
            frozen = frozen_session_ids(
                db,
                user_id=blunder.user_id,
                session_ids=[remaining.id for remaining in sessions[index:]],
            )
            print(
                f"blunder={blunder.id} sessions={index}/{len(sessions)} "
                f"opportunities={opportunities} reached={reached_count}",
                flush=True,
            )

    db.commit()
    if frozen_count:
        print(
            f"blunder={blunder.id} frozen_sessions_skipped={frozen_count}",
            flush=True,
        )
    return len(sessions) - frozen_count, opportunities, reached_count


@srs_telemetry.repair_operation("repair_session", bulk_source="repair_all_sessions")
def recompute_srs_opportunities(
    db,
    *,
    session_id: uuid.UUID | None = None,
    after_session_id: uuid.UUID | None = None,
    started_before: datetime | None = None,
    user_id: int | None = None,
    limit: int | None = None,
    progress_every: int = 100,
) -> SessionRecomputeReport:
    """Recompute and commit explicit sessions in stable UUID keyset order.

    Sessions with no current move rows are included: recomputing one is what retires
    any stale event rows left after its observations disappeared.
    """
    if session_id is not None and (
        after_session_id is not None
        or started_before is not None
        or user_id is not None
        or limit is not None
    ):
        raise ValueError(
            "after_session_id, started_before, user_id, and limit require "
            "an all-session recompute"
        )
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")
    if progress_every < 0:
        raise ValueError("progress_every must be non-negative")

    query = db.query(GameSession)
    if session_id is not None:
        query = query.filter(GameSession.id == session_id)
    if user_id is not None:
        query = query.filter(GameSession.user_id == user_id)
    if after_session_id is not None:
        query = query.filter(GameSession.id > after_session_id)
    if started_before is not None:
        if started_before.tzinfo is None or started_before.utcoffset() is None:
            raise ValueError("started_before must include a UTC offset")
        query = query.filter(
            GameSession.started_at < started_before.astimezone(timezone.utc)
        )

    query = query.order_by(GameSession.id.asc())
    if limit is not None:
        query = query.limit(limit)
    sessions = query.all()
    if session_id is not None and not sessions:
        raise RuntimeError(f"Session not found: {session_id}")

    frozen_sessions = 0
    for index, session in enumerate(sessions, start=1):
        # The guard lives inside _compute_blunder_opportunity_events, so both
        # session-grain modes inherit it; a False return is an explicit policy
        # skip, not a failure, and must not be retried.
        # Per session, not once per run: every iteration below commits, which
        # releases the transaction-scoped lock. Sessions here can also belong to
        # different users, so there is no single owner to lock for the run.
        _lock_user(db, session.user_id)
        if not _compute_blunder_opportunity_events(
            db,
            session_id=session.id,
            user_id=session.user_id,
            player_color=session.player_color,
        ):
            frozen_sessions += 1
            print(f"frozen session skipped: {session.id}", flush=True)
        # The function no longer self-commits (the live path times the commit as a
        # distinct stage); commit per session to preserve incremental durability.
        db.commit()
        if progress_every > 0 and index % progress_every == 0:
            print(
                f"sessions={index}/{len(sessions)} "
                f"last_session_id={session.id}",
                flush=True,
            )

    return SessionRecomputeReport(
        processed_sessions=len(sessions) - frozen_sessions,
        last_session_id=sessions[-1].id if sessions else None,
        frozen_sessions=frozen_sessions,
    )


@srs_telemetry.repair_operation("repair_all_blunders")
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
    session_position_cache: dict[uuid.UUID, SessionPositionIds] = {}
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be an ISO-8601 timestamp with a UTC offset"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--blunder-id", type=_positive_int, help="Recompute one blunder; its frozen sessions are skipped.")
    mode.add_argument(
        "--all-blunders",
        action="store_true",
        help="Recompute all blunders (optionally one user); frozen sessions are skipped.",
    )
    mode.add_argument(
        "--session-id",
        type=uuid.UUID,
        help="Recompute one session and retire its stale broad rows, unless frozen.",
    )
    mode.add_argument(
        "--all-sessions",
        action="store_true",
        help="Recompute sessions in UUID order (optionally one user); frozen ones are skipped.",
    )
    parser.add_argument(
        "--user-id",
        type=_positive_int,
        help="Scope an all-blunders or all-sessions run to one user.",
    )
    parser.add_argument(
        "--after-session-id",
        type=uuid.UUID,
        help="Resume an all-sessions run after this last committed UUID.",
    )
    parser.add_argument(
        "--started-before",
        type=_utc_datetime,
        help="Exclusive creation cutoff for a stable all-sessions cleanup cohort.",
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        help="Maximum sessions or blunders in this committed page.",
    )
    parser.add_argument(
        "--progress-every",
        type=_non_negative_int,
        default=100,
        help="Progress print interval; 0 disables intermediate output.",
    )
    args = parser.parse_args(argv)
    if args.after_session_id is not None and not args.all_sessions:
        parser.error("--after-session-id requires --all-sessions")
    if args.started_before is not None and not args.all_sessions:
        parser.error("--started-before requires --all-sessions")
    if args.user_id is not None and not (args.all_sessions or args.all_blunders):
        parser.error("--user-id requires --all-sessions or --all-blunders")
    if args.limit is not None and not (args.all_sessions or args.all_blunders):
        parser.error("--limit requires --all-sessions or --all-blunders")
    return args


def main(
    argv: list[str] | None = None,
    *,
    session_factory=None,
) -> int:
    args = parse_args(argv)
    factory = session_factory or SessionLocal
    db = factory()
    try:
        if args.blunder_id is not None:
            total, opportunities, reached = recompute_one_blunder(
                db,
                blunder_id=args.blunder_id,
                progress_every=args.progress_every,
            )
            # recompute_one_blunder loads the row but returns only its counts, so
            # the owner has to be looked up here for the loader's required scope.
            owner_id = (
                db.query(Blunder.user_id).filter(Blunder.id == args.blunder_id).scalar()
            )
            counters = load_opportunity_counters(
                db, [args.blunder_id], user_id=owner_id
            ).get(args.blunder_id)
            print(
                f"Recomputed blunder {args.blunder_id}: sessions={total} "
                f"matched_opportunities={opportunities} matched_reached={reached}"
            )
            if counters is not None:
                print(
                    "Counters: "
                    f"opportunities_since_review={counters.opportunities_since_review} "
                    # Targeting reads the selected server decision/fact source;
                    # reached joins the broad reach row. Rollout fingerprints both.
                    f"targeted_30d={counters.targeted_30d} "
                    f"targeted_reached_30d={counters.targeted_reached_30d} "
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
        elif args.session_id is not None:
            report = recompute_srs_opportunities(
                db,
                session_id=args.session_id,
                progress_every=args.progress_every,
            )
            print(
                f"Recomputed session {args.session_id}: "
                f"processed_sessions={report.processed_sessions} "
                f"frozen_sessions={report.frozen_sessions} "
                f"last_session_id={report.last_session_id}"
            )
        elif args.all_sessions:
            report = recompute_srs_opportunities(
                db,
                after_session_id=args.after_session_id,
                started_before=args.started_before,
                user_id=args.user_id,
                limit=args.limit,
                progress_every=args.progress_every,
            )
            print(
                "Recomputed sessions: "
                f"processed_sessions={report.processed_sessions} "
                f"frozen_sessions={report.frozen_sessions} "
                f"last_session_id={report.last_session_id}"
            )
        else:
            raise AssertionError("argparse accepted no recompute mode")
        print(
            "Targeted invariant: opponent_decisions_written=false "
            "opponent_target_facts_written=false "
            "frozen_counter_verification_required=true"
        )
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
