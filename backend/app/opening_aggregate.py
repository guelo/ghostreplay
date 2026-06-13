"""Shared opening-score aggregation primitives.

Hoisted out of app.api.openings so both the openings router and the session
router can aggregate cached per-root scores into subtree totals without one API
module importing another (or duplicating the logic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import UserOpeningScore
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
class DirectBranchView:
    """Direct-row view of a branch.

    ``direct_row`` is the branch root's own cached score (None when unscored).
    ``scored_root_count`` is navigation metadata only — the number of scored
    named rows in the subtree (root + descendants) — and never feeds a score.
    ``weakest_root`` is the weakest scored root in the subtree.
    """

    direct_row: CachedOpeningScoreRow | None
    scored_root_count: int
    weakest_root: CachedOpeningScoreRow | None


def _weakest_root(rows: list[CachedOpeningScoreRow]) -> CachedOpeningScoreRow:
    """Pick the weakest root, tie-breaking on ``opening_key`` only.

    The design requires stable opening-key tie-breaking: equal-score roots must
    resolve to the same key regardless of confidence/name, so the surfaced
    weakest-root metadata does not flip when confidence changes despite unchanged
    mastery (score).
    """
    return min(rows, key=lambda r: (r.opening_score, r.opening_key))


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


def direct_branch_view(
    rows_by_key: dict[str, CachedOpeningScoreRow],
    branch_key: str,
    roots_registry: OpeningRoots,
) -> DirectBranchView:
    """Direct-row semantics for a branch (section 9).

    Score/sample/last-practiced come from the branch root's **own** cached row,
    not a descendant rollup. ``scored_root_count`` and ``weakest_root`` are
    navigation metadata computed over the scored subtree rows.
    """
    subtree_rows = _collect_branch_rows(rows_by_key, branch_key, roots_registry)
    return DirectBranchView(
        direct_row=rows_by_key.get(branch_key),
        scored_root_count=len(subtree_rows),
        weakest_root=_weakest_root(subtree_rows) if subtree_rows else None,
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
