from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.models import Blunder, BlunderOpportunityEvent, BlunderReview
from app.opening_roots import get_opening_roots
from app.srs_math import (
    as_utc,
    calculate_opportunity_overdue,
    calculate_priority,
    compute_p_reach,
)


@dataclass(frozen=True)
class OpportunityCounters:
    opportunities_since_review: int = 0
    opportunities_30d: int = 0
    reached_30d: int = 0
    event_count: int = 0

    @property
    def p_reach(self) -> float:
        return compute_p_reach(self.reached_30d, self.opportunities_30d)


def detect_opening_family(fen_raw: str) -> str | None:
    roots = get_opening_roots()
    owning_keys = roots.owning_root_keys(normalize_fen(fen_raw))
    if not owning_keys:
        return None
    root = roots.get_root(sorted(owning_keys)[0])
    return root.opening_family if root is not None else None


def opening_weight(blunder_family: str | None, current_family: str | None) -> float:
    if blunder_family is None or current_family is None:
        return 1.0
    return 1.0 if blunder_family == current_family else 0.1


def load_opportunity_counters(
    db: Session,
    blunder_ids: list[int],
    *,
    now: datetime | None = None,
) -> dict[int, OpportunityCounters]:
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
    opportunity_30d = and_(
        BlunderOpportunityEvent.opportunity.is_(True),
        event_time >= cutoff,
        event_after_blunder_created,
    )
    reached_30d = and_(
        BlunderOpportunityEvent.reached.is_(True),
        event_time >= cutoff,
        event_after_blunder_created,
    )
    opportunity_since_review = and_(
        BlunderOpportunityEvent.opportunity.is_(True),
        event_after_blunder_created,
        or_(
            latest_review.c.reviewed_at.is_(None),
            and_(
                BlunderOpportunityEvent.session_id != latest_review.c.session_id,
                event_time > latest_review.c.reviewed_at,
            ),
        ),
    )

    rows = (
        db.query(
            BlunderOpportunityEvent.blunder_id.label("blunder_id"),
            func.count(BlunderOpportunityEvent.id).label("event_count"),
            func.coalesce(func.sum(case((opportunity_since_review, 1), else_=0)), 0).label(
                "opportunities_since_review"
            ),
            func.coalesce(func.sum(case((opportunity_30d, 1), else_=0)), 0).label("opportunities_30d"),
            func.coalesce(func.sum(case((reached_30d, 1), else_=0)), 0).label("reached_30d"),
        )
        .outerjoin(latest_review, BlunderOpportunityEvent.blunder_id == latest_review.c.blunder_id)
        .join(Blunder, Blunder.id == BlunderOpportunityEvent.blunder_id)
        .filter(BlunderOpportunityEvent.blunder_id.in_(unique_blunder_ids))
        .group_by(BlunderOpportunityEvent.blunder_id)
        .all()
    )

    for row in rows:
        counters[row.blunder_id] = OpportunityCounters(
            opportunities_since_review=int(row.opportunities_since_review or 0),
            opportunities_30d=int(row.opportunities_30d or 0),
            reached_30d=int(row.reached_30d or 0),
            event_count=int(row.event_count or 0),
        )
    return counters


def opportunity_priority(
    *,
    counters: OpportunityCounters,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> float:
    if counters.event_count > 0:
        return calculate_opportunity_overdue(
            opportunities_since_review=counters.opportunities_since_review,
            pass_streak=pass_streak,
        )
    return calculate_priority(
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
        created_at=created_at,
        now=now,
    )
