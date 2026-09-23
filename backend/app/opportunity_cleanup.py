"""Activity-aware bounded visits; no sweep watermark stands in for real backlog."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import case, extract, func, select
from sqlalchemy.orm import Session

from app.models import (Blunder, BlunderOpportunityEvent, GameSession,
                        OpponentDecision, OpponentTargetFact)
from app.opponent_target_facts import target_source
from app.opportunity_fold import (FoldLimits, FoldOutcome, SweepReport,
                                  _eligible_rows_query, eligible_users, fold_user_batch)
from app.opportunity_retention import database_clock, load_policy
from app.session_activity import known_inflight
from app.srs_math import as_utc
from app.srs_opportunity import TARGETED_WINDOW_DAYS

logger = logging.getLogger(__name__)
FORCE_AFTER = 12 * 3600
IGNORE_ACTIVITY_AFTER = 18 * 3600
ALERT_AFTER = 24 * 3600
SCHEDULED_LIMITS = FoldLimits(max_pairs=25, max_blunders=4)


@dataclass(frozen=True)
class Backlog:
    pairs: int
    # Conservative upper bound if historical target metadata has already expired.
    lag_seconds: float = 0.0


def backlog(engine, user_id: int) -> Backlog:
    """Age since eligibility of the oldest CURRENTLY foldable pair.

    A target's inclusive 30d pin and a legacy event time can postpone eligibility
    beyond M+G. Activity, prefix advancement and earlier attempts never do.
    Expired targeting metadata cannot prove a pair was never pinned. With no
    surviving target, the session/event floor gives a conservative lag upper
    bound: this can accelerate a visit/alert, never hide backlog or defer work.
    Aggregate in SQL rather than materializing the user's entire event history.
    """
    with Session(bind=engine) as db:
        policy = load_policy(db)
        now = as_utc(db.scalar(select(database_clock(db))))
        if target_source() == "facts":
            sid, bid, served = (OpponentTargetFact.session_id,
                                OpponentTargetFact.blunder_id,
                                OpponentTargetFact.last_served_at)
        else:
            sid, bid, served = (OpponentDecision.session_id,
                                OpponentDecision.target_blunder_id,
                                OpponentDecision.served_at)
        last_target = select(func.max(served)).where(
            sid == BlunderOpportunityEvent.session_id,
            bid == BlunderOpportunityEvent.blunder_id,
            served >= Blunder.created_at,
        ).correlate(BlunderOpportunityEvent, Blunder).scalar_subquery()

        def epoch(value):
            if engine.dialect.name == "sqlite":
                return (func.julianday(value) - 2440587.5) * 86400.0
            return extract("epoch", value)

        age_floor = epoch(GameSession.started_at) + policy.foldable_age.total_seconds()
        event_floor = epoch(func.coalesce(BlunderOpportunityEvent.occurred_at,
                                         BlunderOpportunityEvent.created_at))
        pin_floor = func.coalesce(epoch(last_target) + TARGETED_WINDOW_DAYS * 86400,
                                  age_floor)
        later = case((event_floor > age_floor, event_floor), else_=age_floor)
        eligible_at = case((pin_floor > later, pin_floor), else_=later)
        query = _eligible_rows_query(db, user_id=user_id, policy=policy,
                                     blunder_ids=None).with_only_columns(
            func.count(BlunderOpportunityEvent.id), func.min(eligible_at),
        )
        count, oldest = db.execute(query).one()
        return Backlog(int(count), max(0.0, now.timestamp() - float(oldest)) if count else 0.0)


def recently_active(engine, user_id: int, *, quiet_seconds: float = 3660) -> bool:
    """A minute of tolerance covers the coalesced hint. Unknown prefers deferral."""
    if known_inflight(user_id):
        return True
    with Session(bind=engine) as db:
        last, now = db.execute(select(func.max(GameSession.last_activity_at),
                                      database_clock(db)).where(
            GameSession.user_id == user_id,
        )).one()
        return last is None or (as_utc(now) - as_utc(last)).total_seconds() < quiet_seconds


@dataclass
class CleanupReport:
    sweep: SweepReport = field(default_factory=SweepReport)
    remaining: dict[int, Backlog] = field(default_factory=dict)
    capped: list[int] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)
    alert_users: list[int] = field(default_factory=list)
    disabled: bool = False


@dataclass
class _Visit:
    ready_at: float = 0.0
    forced: bool = False
    quiet_rechecked: bool = False
    charged_seconds: float = 0.0
    attempts: int = 0
    size: int = 25


def scheduled_sweep(engine, *, limits=SCHEDULED_LIMITS, max_attempts=100,
                    max_seconds=120.0, clock=time.monotonic, sleep=time.sleep,
                    stopped=lambda: False) -> CleanupReport:
    """One fair visit per candidate; every attempt costs the same finite budget.

    Queue order rotates after each batch, including busy/stale results. Discovery
    has no fixed first-N cutoff that could permanently starve later users. A
    forced visit latches until lag is below 12h, regardless of returning activity.
    Each user pays only for their fold attempts and required cooldowns, not for
    other users' work or their preliminary quiet-time preference.
    All waits and reads are outside the fold's critical transaction.
    """
    if not 1 <= max_attempts <= 100 or not 0 < max_seconds <= 120:
        raise ValueError("visits require 1..100 attempts and 0..120 seconds")
    if not 1 <= limits.max_pairs <= 25 or not 1 <= limits.max_blunders <= 4:
        raise ValueError("scheduled batches may only shrink below 25 pairs / 4 blunders")
    report = CleanupReport()
    with Session(bind=engine) as db:
        report.disabled = not load_policy(db).cleanup_enabled
    if report.disabled or stopped():
        return report
    pending = eligible_users(engine, limit=None)
    report.sweep.candidates = len(pending)
    visits = {uid: _Visit(size=limits.max_pairs) for uid in pending}
    while pending and not stopped():
        moment = clock()
        uid = next((uid for uid in pending if visits[uid].ready_at <= moment), None)
        if uid is None:
            sleep(min(1.0, max(0.0, min(visits[u].ready_at for u in pending) - moment)))
            continue
        pending.remove(uid)
        visit = visits[uid]
        if visit.attempts >= max_attempts or visit.charged_seconds >= max_seconds:
            report.capped.append(uid)
            continue
        try:
            current = backlog(engine, uid)
            if not current.pairs or (visit.forced and current.lag_seconds < FORCE_AFTER):
                continue
            if current.lag_seconds >= FORCE_AFTER:
                visit.forced = True
            if visit.forced:
                if (not visit.attempts and not visit.quiet_rechecked
                        and current.lag_seconds < IGNORE_ACTIVITY_AFTER
                        and recently_active(engine, uid, quiet_seconds=120)):
                    visit.quiet_rechecked = True
                    visit.ready_at = clock() + 60
                    pending.append(uid)
                    continue
                check = None
            else:
                def check():
                    return recently_active(engine, uid)
            started = clock()
            visit.attempts += 1
            result = fold_user_batch(engine, user_id=uid, limits=limits,
                                     max_pairs=visit.size, activity_check=check,
                                     stop_check=lambda: stopped() or (
                                         visit.charged_seconds + clock() - started >= max_seconds))
            visit.charged_seconds += clock() - started
            report.sweep.record(result)
            if result.outcome == FoldOutcome.DEFERRED_ACTIVITY:
                report.deferred.append(uid)
                continue
            if result.outcome == FoldOutcome.DISABLED:
                report.disabled = True
                break
            if result.outcome == FoldOutcome.DEFERRED_BUDGET:
                report.capped.append(uid)
                continue
            if (result.outcome == FoldOutcome.DEADLINE_EXCEEDED
                    or result.lock_seconds > limits.hold_target):
                visit.size = max(min(limits.min_pairs, visit.size), visit.size // 2)
            cooldown = max(1.0, limits.user_cooldown)
            # Charge the required cooldown once, even when other users run
            # during it. Queue time beyond that never consumes this user's cap.
            visit.charged_seconds += cooldown
            visit.ready_at = clock() + cooldown
            pending.append(uid)
        except Exception:
            logger.exception("srs_cleanup_user_failed user_id=%s", uid)
            report.sweep.errors.append(f"user {uid}: cleanup failed")

    # Fresh measured rows, including new candidates, even after activity skips,
    # exceptions and caps. A batch result / prefix is NEVER a completion signal.
    if stopped():
        return report
    for uid in sorted(set(visits) | set(eligible_users(engine, limit=None))):
        if stopped():
            break
        try:
            remaining = backlog(engine, uid)
            report.remaining[uid] = remaining
            if remaining.pairs and remaining.lag_seconds >= ALERT_AFTER:
                report.alert_users.append(uid)
                logger.error("srs_cleanup_lag_alert user_id=%s pairs=%s lag_upper_bound_seconds=%.3f",
                             uid, remaining.pairs, remaining.lag_seconds)
        except Exception:
            logger.exception("srs_cleanup_backlog_unknown user_id=%s", uid)
            report.sweep.errors.append(f"user {uid}: backlog unknown")
    return report
