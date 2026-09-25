"""Unmodified raw SQL reader from 15854a1 (before facts/retention).

Only the two reader functions are copied; imports bind their original models
and the unchanged five-counter value type. Never update this oracle to follow
production compaction arithmetic. Tests supply a separate, never-folded DB.
"""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session
from app.models import Blunder, BlunderOpportunityEvent, BlunderReview, OpponentDecision
from app.srs_math import as_utc
from app.srs_opportunity import OpportunityCounters

def load_opportunity_counters(
    db: Session,
    blunder_ids: list[int],
    *,
    user_id: int,
    now: datetime | None = None,
    exclude_session_id: uuid.UUID | None = None,
) -> dict[int, OpportunityCounters]:
    """Per-blunder opportunity counters, broad and targeted.

    ``user_id`` is REQUIRED, not defaulted. ``opponent_decisions.target_blunder_id``
    is a bare FK to ``blunders.id`` with no user scoping of its own, so the
    targeted aggregate below joins through ``Blunder.user_id`` to scope it. A
    default would let a caller silently skip that scoping; making it required
    turns every call site into a compile-time sweep instead.

    ``exclude_session_id`` drops that session's own evidence from BOTH
    aggregates. Ghost steering passes the in-progress game session here: the
    game we are steering *toward* the blunder in must not count as a missed
    opportunity against that blunder's dueness, or a single ancestor touch
    early in the game would flip the target to "exactly due, not overdue" and
    silently kill steering for the rest of that game.
    """
    if not blunder_ids:
        return {}

    unique_blunder_ids = list(dict.fromkeys(blunder_ids))
    now_utc = as_utc(now or datetime.now(timezone.utc))
    cutoff = now_utc - timedelta(days=30)
    counters = {blunder_id: OpportunityCounters() for blunder_id in unique_blunder_ids}

    ranked_reviews = (
        db.query(
            BlunderReview.blunder_id.label("blunder_id"),
            BlunderReview.session_id.label("session_id"),
            BlunderReview.reviewed_at.label("reviewed_at"),
            func.row_number()
            .over(
                partition_by=BlunderReview.blunder_id,
                order_by=(BlunderReview.reviewed_at.desc(), BlunderReview.id.desc()),
            )
            .label("rn"),
        )
        .filter(BlunderReview.blunder_id.in_(unique_blunder_ids))
        .subquery()
    )
    latest_review = (
        db.query(
            ranked_reviews.c.blunder_id,
            ranked_reviews.c.session_id,
            ranked_reviews.c.reviewed_at,
        )
        .filter(ranked_reviews.c.rn == 1)
        .subquery()
    )

    event_time = func.coalesce(BlunderOpportunityEvent.occurred_at, BlunderOpportunityEvent.created_at)
    blunder_created_at = func.coalesce(Blunder.created_at, BlunderOpportunityEvent.created_at)
    event_after_blunder_created = event_time >= blunder_created_at
    # The single eligibility predicate for BROAD evidence. Every aggregate below is one
    # of these two AND its own window; ``event_count`` is ``eligible_broad`` with no
    # window at all.
    eligible_broad = and_(
        BlunderOpportunityEvent.opportunity.is_(True),
        event_after_blunder_created,
    )
    # ``reached`` is only ever meaningful ALONGSIDE ``opportunity``: reaching a position
    # is the strongest possible opportunity, which is why the writer sets both and
    # ck_blunder_opportunity_reached_implies_opportunity enforces it. Restating it here
    # is defence in depth — a hand-edited or legacy row that claims a reach without an
    # opportunity must not inflate a numerator whose denominator excludes it.
    eligible_reached = and_(eligible_broad, BlunderOpportunityEvent.reached.is_(True))
    since_review = or_(
        latest_review.c.reviewed_at.is_(None),
        and_(
            BlunderOpportunityEvent.session_id != latest_review.c.session_id,
            event_time > latest_review.c.reviewed_at,
        ),
    )
    opportunity_since_review = and_(eligible_broad, since_review)
    reached_since_review = and_(eligible_reached, since_review)

    rows_query = (
        db.query(
            BlunderOpportunityEvent.blunder_id.label("blunder_id"),
            # ALIGNED with ``eligible_broad``, not a raw row count. ``event_count`` is
            # the routing switch in ``opportunity_priority`` /
            # ``practice_priority_score``: >0 means "opportunity evidence exists, score
            # by dueness", 0 means "fall back to the time-based schedule". Counting
            # rows the eligibility predicate rejects — an opportunity=false row, or one
            # dated before the blunder existed — routed a blunder into the dueness
            # branch with an ``opportunities_since_review`` of 0, i.e. a priority of 0,
            # permanently not-due. The counter and the gate now read the same rows.
            func.coalesce(func.sum(case((eligible_broad, 1), else_=0)), 0).label("event_count"),
            func.coalesce(func.sum(case((opportunity_since_review, 1), else_=0)), 0).label(
                "opportunities_since_review"
            ),
            func.coalesce(func.sum(case((reached_since_review, 1), else_=0)), 0).label(
                "reached_since_review"
            ),
        )
        .outerjoin(latest_review, BlunderOpportunityEvent.blunder_id == latest_review.c.blunder_id)
        .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
        .filter(BlunderOpportunityEvent.blunder_id.in_(unique_blunder_ids))
    )
    if exclude_session_id is not None:
        rows_query = rows_query.filter(BlunderOpportunityEvent.session_id != exclude_session_id)
    rows = rows_query.group_by(BlunderOpportunityEvent.blunder_id).all()

    targeted = _load_targeted_counters(
        db,
        unique_blunder_ids,
        user_id=user_id,
        cutoff=cutoff,
        exclude_session_id=exclude_session_id,
    )

    for row in rows:
        targeted_30d, targeted_reached_30d = targeted.pop(row.blunder_id, (0, 0))
        counters[row.blunder_id] = OpportunityCounters(
            opportunities_since_review=int(row.opportunities_since_review or 0),
            reached_since_review=int(row.reached_since_review or 0),
            event_count=int(row.event_count or 0),
            targeted_30d=targeted_30d,
            targeted_reached_30d=targeted_reached_30d,
        )
    # A blunder can be targeted with no broad event at all — that is the failed
    # steer whose whole point is to survive the session never uploading — so the
    # targeted-only remainder still has to land in the result.
    for blunder_id, (targeted_30d, targeted_reached_30d) in targeted.items():
        counters[blunder_id] = OpportunityCounters(
            targeted_30d=targeted_30d,
            targeted_reached_30d=targeted_reached_30d,
        )
    return counters


def _load_targeted_counters(
    db: Session,
    unique_blunder_ids: list[int],
    *,
    user_id: int,
    cutoff: datetime,
    exclude_session_id: uuid.UUID | None,
) -> dict[int, tuple[int, int]]:
    """Targeted-session denominator/numerator from ``opponent_decisions``.

    A SECOND aggregate rather than more columns on the broad query: the grains
    differ (one is over events, the other over decisions), so they cannot fold
    into one GROUP BY. It reads the decision log directly instead of
    materializing targeting into ``blunder_opportunity_events``, which is
    written by the client upload path — a session served a target and then
    never uploading would drop the FAILED steer and bias p_reach upward, the
    exact client-controlled-denominator hole the decision log exists to close.

    FILTER BEFORE GROUPING. Eligibility is a property of an individual decision
    ROW, not of a group's ``MIN(served_at)``. Grouping first and testing the
    minimum would drop a whole session whose EARLIEST targeting of a blunder
    falls outside the window, even when a later attempt sits squarely inside
    it; the same error hits ``served_at >= created_at`` for a blunder created
    mid-session.
    """
    filters = [
        OpponentDecision.target_blunder_id.in_(unique_blunder_ids),
        Blunder.user_id == user_id,
        OpponentDecision.served_at >= cutoff,
        # Per-decision served_at IS the targeted timeline. Not session.started_at:
        # that would date a late-session decision to the session's opening and
        # silently drop targeting of a blunder created during that same session.
        OpponentDecision.served_at >= Blunder.created_at,
    ]
    if exclude_session_id is not None:
        filters.append(OpponentDecision.session_id != exclude_session_id)

    groups = (
        db.query(
            OpponentDecision.session_id.label("session_id"),
            OpponentDecision.target_blunder_id.label("blunder_id"),
        )
        .join(Blunder, Blunder.id == OpponentDecision.target_blunder_id)
        .filter(*filters)
        # Grouping by session is what makes the denominator count targeted
        # SESSIONS: re-hooking the same blunder later in one session counts once.
        .group_by(OpponentDecision.session_id, OpponentDecision.target_blunder_id)
        .subquery()
    )

    # reached stays whole-session position-set membership from the broad stream,
    # joined in for the NUMERATOR only. The unique (session_id, blunder_id) on
    # blunder_opportunity_events means this outer join cannot fan out a group.
    # No event row means not reached.
    rows = (
        db.query(
            groups.c.blunder_id,
            func.count().label("targeted_30d"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                BlunderOpportunityEvent.reached.is_(True),
                                # Same defence in depth as the broad aggregates: a
                                # reach without an opportunity is not evidence.
                                BlunderOpportunityEvent.opportunity.is_(True),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("targeted_reached_30d"),
        )
        .select_from(groups)
        .outerjoin(
            BlunderOpportunityEvent,
            and_(
                BlunderOpportunityEvent.session_id == groups.c.session_id,
                BlunderOpportunityEvent.blunder_id == groups.c.blunder_id,
            ),
        )
        .group_by(groups.c.blunder_id)
        .all()
    )
    return {
        row.blunder_id: (int(row.targeted_30d or 0), int(row.targeted_reached_30d or 0))
        for row in rows
    }
