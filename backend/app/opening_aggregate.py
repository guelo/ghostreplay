"""Shared opening-score aggregation primitives.

Hoisted out of app.api.openings so both the openings router and the session
router can aggregate cached per-root scores into subtree totals without one API
module importing another (or duplicating the logic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import OpeningPositionScore, UserOpeningScore


@dataclass(frozen=True)
class CachedPositionScoreRow:
    """Detached snapshot of one ``OpeningPositionScore`` row.

    Mirrors the persisted direct position-score read model. ``has_evidence`` false
    is a no-data row: the four metric fields are ``None`` and the counts are zero.
    """

    normalized_fen: str
    player_color: str
    in_book: bool
    has_evidence: bool
    opening_score: float | None
    confidence: float | None
    coverage: float | None
    weighted_depth: float | None
    sample_size: int
    game_count: int
    last_practiced_at: datetime | None


def _snapshot_position_rows(
    rows: list[OpeningPositionScore],
) -> list[CachedPositionScoreRow]:
    return [
        CachedPositionScoreRow(
            normalized_fen=row.normalized_fen,
            player_color=row.player_color,
            in_book=row.in_book,
            has_evidence=row.has_evidence,
            opening_score=row.opening_score,
            confidence=row.confidence,
            coverage=row.coverage,
            weighted_depth=row.weighted_depth,
            sample_size=row.sample_size,
            game_count=row.game_count,
            last_practiced_at=row.last_practiced_at,
        )
        for row in rows
    ]


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
    game_count: int
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


def _weakest_root(rows: list[CachedOpeningScoreRow]) -> CachedOpeningScoreRow:
    """Pick the weakest root, tie-breaking on ``opening_key`` only.

    The design requires stable opening-key tie-breaking: equal-score roots must
    resolve to the same key regardless of confidence/name, so the surfaced
    weakest-root metadata does not flip when confidence changes despite unchanged
    mastery (score).
    """
    return min(rows, key=lambda r: (r.opening_score, r.opening_key))


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
            game_count=row.game_count,
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
