from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import fields as dc_fields
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import OpeningScoreBatch, UserOpeningScore
from app.opening_cache import (
    ensure_opening_scores,
    list_cached_opening_scores,
    opening_score_inputs_fingerprint,
    recompute_opening_scores,
)
from app.opening_evidence import overlay_evidence
from app.opening_graph import get_opening_graph
from app.opening_rootcalc import RootScore, compute_root_score
from app.opening_roots import OpeningRoots, get_opening_roots
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/openings", tags=["openings"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RootScoreRequest(BaseModel):
    opening_key: str
    player_color: Literal["white", "black"]


class BranchSummaryResponse(BaseModel):
    opening_key: str
    opening_name: str
    value: float


class NodeDebugResponse(BaseModel):
    fen: str
    is_user_turn: bool
    in_book: bool
    is_extension_node: bool
    p_n: float
    c_n: float
    sample_conf: float
    freshness: float
    evidence_total: float
    days_since_last_touch: float
    last_touch_at: datetime | None
    live_attempts: int
    live_passes: int
    review_attempts: int
    prepared_children: list[str]
    weights: dict[str, float]
    subtree_live_attempts: int
    subtree_review_attempts: int
    covered_locally: bool
    raw_score: float
    raw_confidence: float
    raw_coverage: float
    raw_depth: float
    is_leaf: bool


class RootScoreResponse(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    player_color: str
    opening_score: float
    confidence: float
    coverage: float
    weighted_depth: float
    sample_size: int
    last_practiced_at: datetime | None
    strongest_branch: BranchSummaryResponse | None
    weakest_branch: BranchSummaryResponse | None
    underexposed_branch: BranchSummaryResponse | None
    computed_at: datetime
    debug_nodes: list[NodeDebugResponse]


class OpeningRootItem(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int


class OpeningFamilyItem(BaseModel):
    family_name: str
    roots: list[OpeningRootItem]


class OpeningRootsListResponse(BaseModel):
    families: list[OpeningFamilyItem]
    total_roots: int
    total_families: int


class FamilyScoreItem(BaseModel):
    family_name: str
    root_count: int
    family_score: float
    family_confidence: float
    family_coverage: float
    root_sample_size_sum: int
    last_practiced_at: datetime | None
    weakest_root_name: str
    weakest_root_score: float


class FamilyScoresResponse(BaseModel):
    player_color: str
    families: list[FamilyScoreItem]
    total_families: int
    computed_at: datetime | None


class DrillDownBranchSummary(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    value: float


class DrillDownRootItem(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    depth: int
    eco: str | None
    opening_score: float | None
    confidence: float | None
    coverage: float | None
    weighted_depth: float | None
    sample_size: int | None
    last_practiced_at: datetime | None
    strongest_branch: DrillDownBranchSummary | None
    weakest_branch: DrillDownBranchSummary | None
    underexposed_branch: DrillDownBranchSummary | None


class DrillDownResponse(BaseModel):
    player_color: str
    family_name: str
    roots: list[DrillDownRootItem]
    total_roots: int
    scored_roots: int
    computed_at: datetime | None


class OpeningChildItem(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int
    child_count: int
    subtree_score: float | None
    subtree_confidence: float | None
    subtree_coverage: float | None
    subtree_sample_size: int
    subtree_root_count: int
    last_practiced_at: datetime | None
    weakest_root_key: str | None
    weakest_root_name: str | None
    weakest_root_family: str | None
    weakest_root_score: float | None


class OpeningBreadcrumbItem(BaseModel):
    opening_key: str
    opening_name: str
    is_current: bool


class CurrentBranchStats(BaseModel):
    score: float | None
    confidence: float | None
    coverage: float | None
    sample_size: int | None
    root_count: int


class ChildrenResponse(BaseModel):
    player_color: str
    parent_key: str | None
    parent_name: str | None
    canonical_opening_key: str | None
    canonical_path: list[str]
    breadcrumbs: list[OpeningBreadcrumbItem]
    current_branch_stats: CurrentBranchStats
    children: list[OpeningChildItem]
    total_children: int
    computed_at: datetime | None


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


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

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


def _branch_aggregate_to_response(aggregate: OpeningBranchAggregate) -> CurrentBranchStats:
    return CurrentBranchStats(
        score=aggregate.score,
        confidence=aggregate.confidence,
        coverage=aggregate.coverage,
        sample_size=aggregate.sample_size if aggregate.root_count > 0 else None,
        root_count=aggregate.root_count,
    )


def build_family_scores(rows: list[CachedOpeningScoreRow]) -> list[FamilyScoreItem]:
    """Aggregate per-root cached scores into per-family items."""
    families_map: dict[str, list[CachedOpeningScoreRow]] = defaultdict(list)
    for row in rows:
        families_map[row.opening_family].append(row)

    items: list[FamilyScoreItem] = []
    for family_name, root_rows in families_map.items():
        total_conf = sum(r.confidence for r in root_rows)
        if total_conf > 0:
            family_score = sum(r.opening_score * r.confidence for r in root_rows) / total_conf
        else:
            family_score = sum(r.opening_score for r in root_rows) / len(root_rows)

        family_confidence = sum(r.confidence for r in root_rows) / len(root_rows)
        family_coverage = sum(r.coverage for r in root_rows) / len(root_rows)
        root_sample_size_sum = sum(r.sample_size for r in root_rows)

        practiced_dates = [r.last_practiced_at for r in root_rows if r.last_practiced_at is not None]
        last_practiced_at = max(practiced_dates) if practiced_dates else None

        weakest = _weakest_root(root_rows)

        items.append(FamilyScoreItem(
            family_name=family_name,
            root_count=len(root_rows),
            family_score=family_score,
            family_confidence=family_confidence,
            family_coverage=family_coverage,
            root_sample_size_sum=root_sample_size_sum,
            last_practiced_at=last_practiced_at,
            weakest_root_name=weakest.opening_name,
            weakest_root_score=weakest.opening_score,
        ))

    # Sort: weakest_root_score asc, family_score asc, family_name asc
    items.sort(key=lambda f: (f.weakest_root_score, f.family_score, f.family_name))
    return items


def _batch_has_stale_branch_keys(rows: list[UserOpeningScore | CachedOpeningScoreRow]) -> bool:
    """Detect cache batches written before branch key columns existed."""
    return any(
        (row.strongest_branch_name and not row.strongest_branch_key)
        or (row.weakest_branch_name and not row.weakest_branch_key)
        or (row.underexposed_branch_name and not row.underexposed_branch_key)
        for row in rows
    )


def _row_needs_branch_enrichment(row: UserOpeningScore | CachedOpeningScoreRow) -> bool:
    return (
        row.strongest_branch_key is None
        and row.weakest_branch_key is None
        and row.underexposed_branch_key is None
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


def _refresh_cached_scores_if_stale(
    db: Session,
    user_id: int,
    player_color: Literal["white", "black"],
    current_fingerprint: str,
    roots_registry: OpeningRoots,
    batch: OpeningScoreBatch | None,
    rows: list[CachedOpeningScoreRow],
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


def _compute_missing_drill_down_branches(
    db: Session,
    user_id: int,
    player_color: Literal["white", "black"],
    family_name: str,
    rows: list[CachedOpeningScoreRow],
    graph,
    roots_registry: OpeningRoots,
) -> dict[str, RootScore]:
    missing_rows = [
        row
        for row in rows
        if (
            row.opening_family == family_name
            and _row_needs_branch_enrichment(row)
            and graph.has_position(row.opening_key)
        )
    ]
    if not missing_rows:
        return {}

    overlay = overlay_evidence(db, user_id, player_color, graph)
    db.rollback()
    return {
        row.opening_key: compute_root_score(
            row.opening_key,
            player_color,
            graph,
            overlay,
            roots_registry,
            include_branch_summaries=True,
        )
        for row in missing_rows
    }


def _make_drill_branch(
    key: str | None,
    value: float | None,
    roots_registry: OpeningRoots,
) -> DrillDownBranchSummary | None:
    if key is None or value is None:
        return None
    root = roots_registry.get_root(key)
    if root is None:
        return None
    return DrillDownBranchSummary(
        opening_key=key,
        opening_name=root.opening_name,
        opening_family=root.opening_family,
        value=value,
    )


def build_drill_down_roots(
    rows: list[CachedOpeningScoreRow],
    family_name: str,
    roots_registry: OpeningRoots,
    branch_scores_by_key: dict[str, RootScore] | None = None,
) -> tuple[list[DrillDownRootItem], int]:
    rows_by_key = {row.opening_key: row for row in rows}
    items: list[DrillDownRootItem] = []
    scored_count = 0

    for root in roots_registry.get_family(family_name):
        row = rows_by_key.get(root.opening_key)
        if row is None:
            items.append(
                DrillDownRootItem(
                    opening_key=root.opening_key,
                    opening_name=root.opening_name,
                    opening_family=root.opening_family,
                    depth=root.depth,
                    eco=root.eco,
                    opening_score=None,
                    confidence=None,
                    coverage=None,
                    weighted_depth=None,
                    sample_size=None,
                    last_practiced_at=None,
                    strongest_branch=None,
                    weakest_branch=None,
                    underexposed_branch=None,
                )
            )
            continue

        scored_count += 1
        branch_score = branch_scores_by_key.get(root.opening_key) if branch_scores_by_key else None
        items.append(
            DrillDownRootItem(
                opening_key=root.opening_key,
                opening_name=root.opening_name,
                opening_family=root.opening_family,
                depth=root.depth,
                eco=root.eco,
                opening_score=row.opening_score,
                confidence=row.confidence,
                coverage=row.coverage,
                weighted_depth=row.weighted_depth,
                sample_size=row.sample_size,
                last_practiced_at=row.last_practiced_at,
                strongest_branch=_make_drill_branch(
                    (
                        branch_score.strongest_branch.opening_key
                        if branch_score and branch_score.strongest_branch
                        else row.strongest_branch_key
                    ),
                    (
                        branch_score.strongest_branch.value
                        if branch_score and branch_score.strongest_branch
                        else row.strongest_branch_score
                    ),
                    roots_registry,
                ),
                weakest_branch=_make_drill_branch(
                    (
                        branch_score.weakest_branch.opening_key
                        if branch_score and branch_score.weakest_branch
                        else row.weakest_branch_key
                    ),
                    (
                        branch_score.weakest_branch.value
                        if branch_score and branch_score.weakest_branch
                        else row.weakest_branch_score
                    ),
                    roots_registry,
                ),
                underexposed_branch=_make_drill_branch(
                    (
                        branch_score.underexposed_branch.opening_key
                        if branch_score and branch_score.underexposed_branch
                        else row.underexposed_branch_key
                    ),
                    (
                        branch_score.underexposed_branch.value
                        if branch_score and branch_score.underexposed_branch
                        else row.underexposed_branch_value
                    ),
                    roots_registry,
                ),
            )
        )

    items.sort(
        key=lambda item: (
            item.opening_score is None,
            item.opening_name if item.opening_score is None else item.opening_score,
            item.opening_name,
            item.opening_key,
        )
    )
    return items, scored_count


def build_opening_children(
    rows_by_key: dict[str, CachedOpeningScoreRow],
    parent_key: str | None,
    roots_registry: OpeningRoots,
) -> list[OpeningChildItem]:
    items: list[OpeningChildItem] = []

    for child in roots_registry.get_children(parent_key):
        subtree_rows = _collect_branch_rows(rows_by_key, child.opening_key, roots_registry)
        aggregate = _aggregate_branch_rows(subtree_rows)
        weakest = aggregate.weakest_root

        items.append(
            OpeningChildItem(
                opening_key=child.opening_key,
                opening_name=child.opening_name,
                opening_family=child.opening_family,
                eco=child.eco,
                depth=child.depth,
                child_count=len(roots_registry.get_children(child.opening_key)),
                subtree_score=aggregate.score,
                subtree_confidence=aggregate.confidence,
                subtree_coverage=aggregate.coverage,
                subtree_sample_size=aggregate.sample_size,
                subtree_root_count=aggregate.root_count,
                last_practiced_at=aggregate.last_practiced_at,
                weakest_root_key=weakest.opening_key if weakest is not None else None,
                weakest_root_name=weakest.opening_name if weakest is not None else None,
                weakest_root_family=weakest.opening_family if weakest is not None else None,
                weakest_root_score=weakest.opening_score if weakest is not None else None,
            )
        )

    items.sort(
        key=lambda item: (
            item.subtree_score is None,
            -item.subtree_score if item.subtree_score is not None else math.inf,
            -item.weakest_root_score if item.weakest_root_score is not None else math.inf,
            item.opening_name,
            item.opening_key,
        )
    )
    return items


def canonicalize_children_route(
    parent_key: str | None,
    path_keys: list[str],
    roots_registry: OpeningRoots,
) -> tuple[str | None, list[str], list[OpeningRoot]]:
    """Return the deepest valid route prefix for the requested DAG path.

    The requested route is interpreted as [*path_keys, parent_key] where the
    final item is the currently selected opening and path entries are its
    explicit ancestors from top-level down to the immediate parent.
    """
    if parent_key is None:
        return None, [], []

    route_keys = [*path_keys, parent_key]
    validated_roots: list[OpeningRoot] = []

    for index, opening_key in enumerate(route_keys):
        root = roots_registry.get_root(opening_key)
        if root is None:
            break

        if index == 0:
            if root.parent_keys:
                break
        else:
            previous_key = validated_roots[-1].opening_key
            if previous_key not in root.parent_keys:
                break

        validated_roots.append(root)

    if not validated_roots:
        return None, [], []

    canonical_opening_key = validated_roots[-1].opening_key
    canonical_path = [root.opening_key for root in validated_roots[:-1]]
    return canonical_opening_key, canonical_path, validated_roots


def build_opening_breadcrumbs(route_roots: list[OpeningRoot]) -> list[OpeningBreadcrumbItem]:
    if not route_roots:
        return []

    current_key = route_roots[-1].opening_key
    return [
        OpeningBreadcrumbItem(
            opening_key=root.opening_key,
            opening_name=root.opening_name,
            is_current=root.opening_key == current_key,
        )
        for root in route_roots
    ]


# ---------------------------------------------------------------------------
# Dataclass → Pydantic conversion
# ---------------------------------------------------------------------------

def _branch_to_response(b) -> BranchSummaryResponse | None:
    if b is None:
        return None
    return BranchSummaryResponse(
        opening_key=b.opening_key,
        opening_name=b.opening_name,
        value=b.value,
    )


def _root_score_to_response(rs: RootScore) -> RootScoreResponse:
    debug_nodes = [
        NodeDebugResponse(**{f.name: getattr(n, f.name) for f in dc_fields(n)})
        for n in rs.debug_nodes
    ]
    return RootScoreResponse(
        opening_key=rs.opening_key,
        opening_name=rs.opening_name,
        opening_family=rs.opening_family,
        player_color=rs.player_color,
        opening_score=rs.opening_score,
        confidence=rs.confidence,
        coverage=rs.coverage,
        weighted_depth=rs.weighted_depth,
        sample_size=rs.sample_size,
        last_practiced_at=rs.last_practiced_at,
        strongest_branch=_branch_to_response(rs.strongest_branch),
        weakest_branch=_branch_to_response(rs.weakest_branch),
        underexposed_branch=_branch_to_response(rs.underexposed_branch),
        computed_at=rs.computed_at,
        debug_nodes=debug_nodes,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/score", response_model=RootScoreResponse)
def compute_opening_score(
    body: RootScoreRequest,
    debug: bool = Query(False),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> RootScoreResponse:
    graph = get_opening_graph()
    roots = get_opening_roots()

    if roots.get_root(body.opening_key) is None:
        raise HTTPException(status_code=404, detail="Unknown opening root")

    overlay = overlay_evidence(db, user.user_id, body.player_color, graph)
    db.rollback()
    score = compute_root_score(
        body.opening_key,
        body.player_color,
        graph,
        overlay,
        roots,
        debug=debug,
    )
    return _root_score_to_response(score)


@router.get("/roots", response_model=OpeningRootsListResponse)
def list_opening_roots(
    family: str | None = Query(None),
    user: TokenPayload = Depends(get_current_user),
) -> OpeningRootsListResponse:
    roots = get_opening_roots()

    if family is not None:
        family_names = [family] if roots.get_family(family) else []
    else:
        family_names = roots.get_families()

    families: list[OpeningFamilyItem] = []
    total_roots = 0
    for name in family_names:
        items = [
            OpeningRootItem(
                opening_key=r.opening_key,
                opening_name=r.opening_name,
                opening_family=r.opening_family,
                eco=r.eco,
                depth=r.depth,
            )
            for r in roots.get_family(name)
        ]
        families.append(OpeningFamilyItem(family_name=name, roots=items))
        total_roots += len(items)

    return OpeningRootsListResponse(
        families=families,
        total_roots=total_roots,
        total_families=len(families),
    )


@router.get("/families/scores", response_model=FamilyScoresResponse)
def get_family_scores(
    player_color: Literal["white", "black"] = Query(...),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> FamilyScoresResponse:
    graph = get_opening_graph()
    roots_registry = get_opening_roots()
    batch, rows = ensure_opening_scores(db, user.user_id, player_color)
    current_fingerprint = opening_score_inputs_fingerprint(graph, roots_registry)
    batch, rows = _refresh_cached_scores_if_stale(
        db,
        user.user_id,
        player_color,
        current_fingerprint,
        roots_registry,
        batch,
        rows,
    )
    computed_at = batch.computed_at if batch is not None else None
    row_views = _snapshot_cached_rows(rows)
    families = build_family_scores(row_views)
    return FamilyScoresResponse(
        player_color=player_color,
        families=families,
        total_families=len(families),
        computed_at=computed_at,
    )


@router.get("/families/{family_name}/scores", response_model=DrillDownResponse)
def get_family_drill_down(
    family_name: str,
    player_color: Literal["white", "black"] = Query(...),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillDownResponse:
    graph = get_opening_graph()
    roots_registry = get_opening_roots()
    if not roots_registry.get_family(family_name):
        raise HTTPException(status_code=404, detail="Unknown opening family")

    batch, rows = ensure_opening_scores(db, user.user_id, player_color)
    current_fingerprint = opening_score_inputs_fingerprint(graph, roots_registry)
    batch, rows = _refresh_cached_scores_if_stale(
        db,
        user.user_id,
        player_color,
        current_fingerprint,
        roots_registry,
        batch,
        rows,
    )
    computed_at = batch.computed_at if batch is not None else None
    row_views = _snapshot_cached_rows(rows)

    branch_scores_by_key = _compute_missing_drill_down_branches(
        db,
        user.user_id,
        player_color,
        family_name,
        row_views,
        graph,
        roots_registry,
    )
    root_items, scored_count = build_drill_down_roots(
        row_views,
        family_name,
        roots_registry,
        branch_scores_by_key=branch_scores_by_key,
    )
    return DrillDownResponse(
        player_color=player_color,
        family_name=family_name,
        roots=root_items,
        total_roots=len(root_items),
        scored_roots=scored_count,
        computed_at=computed_at,
    )


@router.get("/children", response_model=ChildrenResponse)
def get_opening_children(
    player_color: Literal["white", "black"] = Query(...),
    parent_key: str | None = Query(None),
    path: list[str] = Query([]),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> ChildrenResponse:
    graph = get_opening_graph()
    roots_registry = get_opening_roots()
    if parent_key is not None and roots_registry.get_root(parent_key) is None:
        raise HTTPException(status_code=404, detail="Unknown opening root")

    batch, rows = ensure_opening_scores(db, user.user_id, player_color)
    current_fingerprint = opening_score_inputs_fingerprint(graph, roots_registry)
    batch, rows = _refresh_cached_scores_if_stale(
        db,
        user.user_id,
        player_color,
        current_fingerprint,
        roots_registry,
        batch,
        rows,
    )
    computed_at = batch.computed_at if batch is not None else None

    row_views = _snapshot_cached_rows(rows)
    rows_by_key = {row.opening_key: row for row in row_views}
    canonical_parent_key, canonical_path, route_roots = canonicalize_children_route(
        parent_key,
        path,
        roots_registry,
    )
    parent_root = (
        roots_registry.get_root(canonical_parent_key)
        if canonical_parent_key is not None
        else None
    )
    children = build_opening_children(
        rows_by_key,
        canonical_parent_key,
        roots_registry,
    )
    current_branch_rows = (
        row_views
        if canonical_parent_key is None
        else _collect_branch_rows(rows_by_key, canonical_parent_key, roots_registry)
    )
    return ChildrenResponse(
        player_color=player_color,
        parent_key=canonical_parent_key,
        parent_name=parent_root.opening_name if parent_root is not None else None,
        canonical_opening_key=canonical_parent_key,
        canonical_path=canonical_path,
        breadcrumbs=build_opening_breadcrumbs(route_roots),
        current_branch_stats=_branch_aggregate_to_response(
            _aggregate_branch_rows(current_branch_rows)
        ),
        children=children,
        total_children=len(children),
        computed_at=computed_at,
    )
