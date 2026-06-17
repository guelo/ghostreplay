from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.fen import normalize_fen
from app.models import Blunder, BlunderOpportunityEvent, BlunderReview
from app.opening_roots import get_opening_roots
from app.srs_math import (
    OPPORTUNITY_POWER,
    as_utc,
    calculate_opportunity_overdue,
    calculate_priority,
    calculate_urgency,
    compute_p_reach,
)

# ---------------------------------------------------------------------------
# Shared ghost eligibility / practice priority policy
#
# These constants and helpers are the single source of truth for both the live
# gameplay ghost selector (app.api.game.find_ghost_move) and the Blunders
# library endpoint (app.api.blunder). Keeping them here prevents the two
# surfaces from drifting on what counts as "ghost eligible" or how durable
# practice priority is scored.
# ---------------------------------------------------------------------------
SEVERITY_NORMALIZER_CP = 50.0
P_REACH_FLOOR = 0.03
P_REACH_MIN_SAMPLE = 30


@dataclass(frozen=True)
class OpportunityCounters:
    opportunities_since_review: int = 0
    opportunities_30d: int = 0
    reached_30d: int = 0
    reached_since_review: int = 0
    event_count: int = 0

    @property
    def p_reach(self) -> float:
        return compute_p_reach(self.reached_30d, self.opportunities_30d)


@dataclass(frozen=True)
class ReviewCounters:
    review_count: int = 0
    pass_count: int = 0
    fail_count: int = 0
    last_result: bool | None = None


def load_review_counters(
    db: Session,
    blunder_ids: list[int],
) -> dict[int, ReviewCounters]:
    """Per-blunder pass/fail review summary counters.

    Time-independent: these are lifetime counts, so this loader intentionally
    takes no ``now``. Shared source for both the in-game ghost SRS response and
    the Blunders library list so the two surfaces cannot drift.
    """
    if not blunder_ids:
        return {}

    unique_blunder_ids = list(dict.fromkeys(blunder_ids))
    counters = {blunder_id: ReviewCounters() for blunder_id in unique_blunder_ids}

    # Single statement so counts and last_result share one consistent snapshot:
    # a concurrent insert cannot yield e.g. zero reviews with last_result set.
    # The per-row aggregates (count / pass sum) and the latest-review flag are
    # both window functions over the same partitioned scan; we then pick the
    # latest row (rn == 1) per blunder, which carries the partition-wide totals.
    ranked = (
        db.query(
            BlunderReview.blunder_id.label("blunder_id"),
            BlunderReview.passed.label("last_result"),
            func.count()
            .over(partition_by=BlunderReview.blunder_id)
            .label("review_count"),
            func.sum(case((BlunderReview.passed, 1), else_=0))
            .over(partition_by=BlunderReview.blunder_id)
            .label("pass_count"),
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

    rows = db.query(
        ranked.c.blunder_id,
        ranked.c.last_result,
        ranked.c.review_count,
        ranked.c.pass_count,
    ).filter(ranked.c.rn == 1)

    for row in rows:
        review_count = int(row.review_count or 0)
        pass_count = int(row.pass_count or 0)
        counters[row.blunder_id] = ReviewCounters(
            review_count=review_count,
            pass_count=pass_count,
            fail_count=review_count - pass_count,
            last_result=row.last_result,
        )
    return counters


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
    exclude_session_id: uuid.UUID | None = None,
) -> dict[int, OpportunityCounters]:
    """Per-blunder opportunity counters.

    ``exclude_session_id`` drops that session's own opportunity events from the
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
    reached_since_review = and_(
        BlunderOpportunityEvent.reached.is_(True),
        event_after_blunder_created,
        or_(
            latest_review.c.reviewed_at.is_(None),
            and_(
                BlunderOpportunityEvent.session_id != latest_review.c.session_id,
                event_time > latest_review.c.reviewed_at,
            ),
        ),
    )

    rows_query = (
        db.query(
            BlunderOpportunityEvent.blunder_id.label("blunder_id"),
            func.count(BlunderOpportunityEvent.id).label("event_count"),
            func.coalesce(func.sum(case((opportunity_since_review, 1), else_=0)), 0).label(
                "opportunities_since_review"
            ),
            func.coalesce(func.sum(case((opportunity_30d, 1), else_=0)), 0).label("opportunities_30d"),
            func.coalesce(func.sum(case((reached_30d, 1), else_=0)), 0).label("reached_30d"),
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

    for row in rows:
        counters[row.blunder_id] = OpportunityCounters(
            opportunities_since_review=int(row.opportunities_since_review or 0),
            opportunities_30d=int(row.opportunities_30d or 0),
            reached_30d=int(row.reached_30d or 0),
            reached_since_review=int(row.reached_since_review or 0),
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


def srs_priority(
    *,
    counters: OpportunityCounters | None,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> float:
    """Existing SRS due value (opportunity-aware), tolerant of missing counters.

    This is the value used for the ``due`` filter (``srs_priority > 1.0``). It
    keeps the original SRS semantics; it is NOT the practice ordering score.
    """
    if counters is not None:
        return opportunity_priority(
            counters=counters,
            pass_streak=pass_streak,
            last_reviewed_at=last_reviewed_at,
            created_at=created_at,
            now=now,
        )
    return calculate_priority(
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
        created_at=created_at,
        now=now,
    )


def ghost_eligible(
    *,
    counters: OpportunityCounters | None,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> bool:
    """Same persistent eligibility rule used by ``find_ghost_move``.

    A target is ghost eligible when it is SRS due (priority > 1.0) and, for
    opportunity-tracked targets with enough samples, is reached often enough
    that steering is likely to feel relevant (p_reach >= P_REACH_FLOOR). This
    is position independent: actual in-game steerability still depends on the
    current FEN and is owned by find_ghost_move.
    """
    has_events = counters is not None and counters.event_count > 0
    priority = srs_priority(
        counters=counters,
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
        created_at=created_at,
        now=now,
    )
    if priority <= 1.0:
        return False
    if (
        has_events
        and counters.opportunities_30d >= P_REACH_MIN_SAMPLE
        and counters.p_reach < P_REACH_FLOOR
    ):
        return False
    return True


def practice_priority_score(
    *,
    counters: OpportunityCounters | None,
    eval_loss_cp: int,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> float:
    """Position-independent approximation of Ghost selection priority.

    Mirrors the durable parts of GhostMoveCandidate.score (urgency, severity,
    reach_weight) and deliberately omits current-position/session factors:
    distance from the current FEN, first-move grouping, repeat penalties,
    current opening-family match, and the session selection seed.
    """
    has_events = counters is not None and counters.event_count > 0
    if has_events:
        urgency = calculate_opportunity_overdue(
            opportunities_since_review=counters.opportunities_since_review,
            pass_streak=pass_streak,
        )
    else:
        urgency = calculate_urgency(
            pass_streak=pass_streak,
            last_reviewed_at=last_reviewed_at,
            created_at=created_at,
            now=now,
        )
    severity = math.log1p(max(float(eval_loss_cp), 0.0) / SEVERITY_NORMALIZER_CP)
    reach_weight = 1.0
    if has_events:
        reach_weight = counters.p_reach ** OPPORTUNITY_POWER
    return urgency * severity * reach_weight
