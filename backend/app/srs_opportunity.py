from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.centipawn_loss import centipawn_loss
from app.fen import normalize_fen
from app.models import Blunder, BlunderOpportunityEvent, BlunderReview, OpponentDecision
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
    """Two independent evidence streams for one blunder.

    The ``opportunities_*`` / ``reached_*`` counters are BROAD evidence from
    ``blunder_opportunity_events``: an 8-ply forward neighbourhood of everything
    the session touched. They drive SRS dueness, where "the position was in
    reach and you did not review it" is exactly the intended signal.

    ``targeted_*`` is the TARGETED-SESSION reach rate, read straight from the
    authoritative ``opponent_decisions`` log: sessions in which the ghost
    actually steered at this blunder, and how many of those reached it. Broad
    evidence is structurally ~1/N in a dense neighbourhood, so using it as the
    p_reach denominator put the AVERAGE blunder at the exclusion floor and
    collapsed steering onto whichever target was newest
    (g-ghost-preach-absorb). p_reach therefore derives from the targeted stream
    only.

    ``event_count`` is not a row count. It counts the rows the BROAD eligibility
    predicate accepts — the same rows ``opportunities_*`` sum — because it is the
    switch that routes ``srs_priority`` / ``practice_priority_score`` between the
    dueness branch and the time-based schedule (g-boundary-event-scope).
    """

    opportunities_since_review: int = 0
    opportunities_30d: int = 0
    reached_30d: int = 0
    reached_since_review: int = 0
    event_count: int = 0
    targeted_30d: int = 0
    targeted_reached_30d: int = 0

    @property
    def p_reach(self) -> float:
        # Zero targeted samples fall out of the Laplace prior as 0.5 with no
        # special-casing, which is what makes the ungated reach weight in
        # practice_priority_score / GhostMoveCandidate.score safe.
        return compute_p_reach(self.targeted_reached_30d, self.targeted_30d)


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
    opportunity_30d = and_(eligible_broad, event_time >= cutoff)
    reached_30d = and_(eligible_reached, event_time >= cutoff)
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
            opportunities_30d=int(row.opportunities_30d or 0),
            reached_30d=int(row.reached_30d or 0),
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
    targets steered at often enough to have a real sample, is reached often
    enough that steering is likely to feel relevant (p_reach >= P_REACH_FLOOR).
    This is position independent: actual in-game steerability still depends on
    the current FEN and is owned by find_ghost_move.

    The floor gates on TARGETED samples, not broad ones. Against the broad
    denominator it was absorbing: exclusion stopped the blunder being served,
    which froze the numerator while ordinary play kept growing the denominator,
    so nothing could ever climb back out. Against the targeted denominator
    exclusion freezes BOTH sides, so the 30-day window drains it and lockout is
    bounded at 30 days. ``event_count`` still routes srs_priority; it no longer
    guards the floor, because the targeted count is its own sufficient guard.
    """
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
        counters is not None
        and counters.targeted_30d >= P_REACH_MIN_SAMPLE
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
    # Severity saturates at the decisive-mistake ceiling (g-no51), mirroring the
    # Ghost candidate score: >=1000cp losses share one severity so mate pseudo-cp
    # cannot dominate practice priority. Floors legacy negatives to 0.
    severity = math.log1p(float(centipawn_loss(eval_loss_cp)) / SEVERITY_NORMALIZER_CP)
    # Ungated, unlike urgency above. Gating reach on has_events handed a
    # never-targeted blunder 1.0 — a better weight than any measured target can
    # earn — instead of the intended prior. With zero targeted samples p_reach
    # is the Laplace 0.5, so this is a uniform 0.354 across the no-data cohort
    # and leaves intra-cohort ordering untouched.
    p_reach = counters.p_reach if counters is not None else compute_p_reach(0, 0)
    reach_weight = p_reach**OPPORTUNITY_POWER
    return urgency * severity * reach_weight
