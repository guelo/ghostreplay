"""Shared opening-score aggregation primitives.

Hoisted out of app.api.openings so both the openings router and the session
router can aggregate cached per-root scores into subtree totals without one API
module importing another (or duplicating the logic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.models import OpeningScoreBatch, UserOpeningScore
from app.opening_cache import list_cached_opening_scores, recompute_opening_scores
from app.opening_roots import OpeningRoots


@dataclass(frozen=True)
class CachedOpeningScoreRow:
    opening_key: str
    opening_name: str
    opening_family: str
    opening_score: float
    confidence: float
    coverage: float
    weighted_depth: float
    sample_size: int
    last_practiced_at: datetime | None
    strongest_branch_name: str | None
    strongest_branch_key: str | None
    strongest_branch_score: float | None
    weakest_branch_name: str | None
    weakest_branch_key: str | None
    weakest_branch_score: float | None
    underexposed_branch_name: str | None
    underexposed_branch_key: str | None
    underexposed_branch_value: float | None


@dataclass(frozen=True)
class OpeningBranchAggregate:
    score: float | None
    confidence: float | None
    coverage: float | None
    sample_size: int
    root_count: int
    last_practiced_at: datetime | None
    weakest_root: CachedOpeningScoreRow | None


def _weakest_root(rows: list[CachedOpeningScoreRow]) -> CachedOpeningScoreRow:
    """Pick the weakest root with deterministic tie-breaking."""
    return min(
        rows,
        key=lambda r: (r.opening_score, r.confidence, r.opening_name, r.opening_key),
    )


def _collect_branch_rows(
    rows_by_key: dict[str, CachedOpeningScoreRow],
    branch_key: str,
    roots_registry: OpeningRoots,
) -> list[CachedOpeningScoreRow]:
    return [
        row
        for row in (
            rows_by_key.get(branch_key),
            *(
                rows_by_key.get(descendant.opening_key)
                for descendant in roots_registry.get_descendants(branch_key)
            ),
        )
        if row is not None
    ]


def _aggregate_branch_rows(rows: list[CachedOpeningScoreRow]) -> OpeningBranchAggregate:
    if not rows:
        return OpeningBranchAggregate(
            score=None,
            confidence=None,
            coverage=None,
            sample_size=0,
            root_count=0,
            last_practiced_at=None,
            weakest_root=None,
        )

    total_conf = sum(row.confidence for row in rows)
    if total_conf > 0:
        score = sum(row.opening_score * row.confidence for row in rows) / total_conf
    else:
        score = sum(row.opening_score for row in rows) / len(rows)

    practiced_dates = [
        row.last_practiced_at for row in rows if row.last_practiced_at is not None
    ]

    return OpeningBranchAggregate(
        score=score,
        confidence=sum(row.confidence for row in rows) / len(rows),
        coverage=sum(row.coverage for row in rows) / len(rows),
        sample_size=sum(row.sample_size for row in rows),
        root_count=len(rows),
        last_practiced_at=max(practiced_dates) if practiced_dates else None,
        weakest_root=_weakest_root(rows),
    )


def _batch_has_stale_branch_keys(
    rows: list[UserOpeningScore | CachedOpeningScoreRow],
) -> bool:
    """Detect cache batches written before branch key columns existed."""
    return any(
        (row.strongest_branch_name and not row.strongest_branch_key)
        or (row.weakest_branch_name and not row.weakest_branch_key)
        or (row.underexposed_branch_name and not row.underexposed_branch_key)
        for row in rows
    )


def _refresh_cached_scores_if_stale(
    db: Session,
    user_id: int,
    player_color: Literal["white", "black"],
    current_fingerprint: str,
    roots_registry: OpeningRoots,
    batch: OpeningScoreBatch | None,
    rows: list[UserOpeningScore | CachedOpeningScoreRow],
) -> tuple[OpeningScoreBatch | None, list[UserOpeningScore]]:
    should_refresh = (
        batch is not None
        and (
            batch.registry_fingerprint != current_fingerprint
            or _batch_has_stale_branch_keys(rows)
        )
    )
    if not should_refresh:
        return batch, rows
    recompute_opening_scores(db, user_id, player_color)
    return list_cached_opening_scores(db, user_id, player_color)


def _snapshot_cached_rows(rows: list[UserOpeningScore]) -> list[CachedOpeningScoreRow]:
    return [
        CachedOpeningScoreRow(
            opening_key=row.opening_key,
            opening_name=row.opening_name,
            opening_family=row.opening_family,
            opening_score=row.opening_score,
            confidence=row.confidence,
            coverage=row.coverage,
            weighted_depth=row.weighted_depth,
            sample_size=row.sample_size,
            last_practiced_at=row.last_practiced_at,
            strongest_branch_name=row.strongest_branch_name,
            strongest_branch_key=row.strongest_branch_key,
            strongest_branch_score=row.strongest_branch_score,
            weakest_branch_name=row.weakest_branch_name,
            weakest_branch_key=row.weakest_branch_key,
            weakest_branch_score=row.weakest_branch_score,
            underexposed_branch_name=row.underexposed_branch_name,
            underexposed_branch_key=row.underexposed_branch_key,
            underexposed_branch_value=row.underexposed_branch_value,
        )
        for row in rows
    ]
