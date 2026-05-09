from __future__ import annotations

import math
from datetime import datetime, timezone

BASE_INTERVAL_HOURS = 4.0
BACKOFF_FACTOR = 2.0
MAX_INTERVAL_HOURS = 4320.0
OPPORTUNITY_POWER = 1.5
OPPORTUNITY_ANCESTOR_RADIUS_PLY = 8


def _coerce_datetime(timestamp: datetime | str) -> datetime:
    if isinstance(timestamp, datetime):
        return timestamp
    if isinstance(timestamp, str):
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    raise TypeError(f"Unsupported timestamp type: {type(timestamp)}")


def as_utc(timestamp: datetime | str) -> datetime:
    dt = _coerce_datetime(timestamp)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expected_interval_hours(pass_streak: int) -> float:
    interval = BASE_INTERVAL_HOURS * (BACKOFF_FACTOR ** max(pass_streak, 0))
    return min(interval, MAX_INTERVAL_HOURS)


def expected_opportunities(pass_streak: int) -> float:
    return expected_interval_hours(pass_streak) / BASE_INTERVAL_HOURS


def compute_p_reach(reached_30d: int, opportunities_30d: int) -> float:
    p_reach = (max(reached_30d, 0) + 2.0) / (max(opportunities_30d, 0) + 4.0)
    return min(max(p_reach, 0.0), 1.0)


def calculate_opportunity_overdue(*, opportunities_since_review: int, pass_streak: int) -> float:
    expected = expected_opportunities(pass_streak)
    if expected <= 0:
        return 0.0
    return max(opportunities_since_review, 0) / expected


def calculate_priority(
    *,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> float:
    reference_time = last_reviewed_at or created_at
    if not reference_time:
        return 0.0

    hours_since_review = max(
        (as_utc(now) - as_utc(reference_time)).total_seconds() / 3600.0,
        0.0,
    )
    return hours_since_review / expected_interval_hours(pass_streak)


def calculate_urgency(
    *,
    pass_streak: int,
    last_reviewed_at: datetime | None,
    created_at: datetime | None,
    now: datetime,
) -> float:
    """Bounded/saturating urgency for ghost move scoring.

    urgency = 1 + log2(1 + overdue)
    where overdue = hours_since / expected_interval

    Returns 0.0 when no reference timestamp exists.
    """
    reference_time = last_reviewed_at or created_at
    if not reference_time:
        return 0.0

    hours_since = max(
        (as_utc(now) - as_utc(reference_time)).total_seconds() / 3600.0,
        0.0,
    )
    overdue = hours_since / expected_interval_hours(pass_streak)
    return 1.0 + math.log2(1.0 + overdue)
