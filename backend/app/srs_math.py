from __future__ import annotations

from datetime import datetime, timezone

BASE_INTERVAL_HOURS = 1.0
BACKOFF_FACTOR = 2.0
MAX_INTERVAL_HOURS = 4320.0


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
