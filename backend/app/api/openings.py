from __future__ import annotations

import logging
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, fields as dc_fields
from datetime import datetime
from typing import Literal

import chess
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import active_color, normalize_fen
from app.game_phase import is_middlegame_position
from app.opening_aggregate import (
    CachedOpeningScoreRow,
    CachedPositionScoreRow,
    _weakest_root,
    direct_branch_view,
)
from app.opening_cache import (
    SCORE_MODEL_VERSION,
    ensure_tree_cache,
    load_cached_rows,
    lookup_observed_edges_for_parent,
    lookup_position_scores_for_batch,
)
from app.opening_evidence import EdgeEvidence, overlay_evidence
from app.opening_graph import OpeningGraph, get_opening_graph
from app.opening_rootcalc import (
    SYNTHETIC_INITIAL_FEN,
    SYNTHETIC_ROOT_FAMILY,
    RootScore,
    compute_root_score,
)
from app.opening_roots import OpeningRoots, get_opening_roots
from app.security import TokenPayload, get_current_user
from app.tree_eval import lookup_move_evals, lookup_root_eval

# Hard ply ceiling for a single resolved move line. Bounds replay/BFS work and
# truncates a pathologically deep (or adversarial) deep-link URL.
MAX_TREE_PLY = 80
SLOW_OPENING_TREE_LOG_MS = 1000.0

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/openings", tags=["openings"])

TreeTiming = dict[str, bool | float | int | str | None]


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000


def _record_timing(timings: TreeTiming | None, key: str, start: float) -> None:
    if timings is not None:
        timings[key] = round(_elapsed_ms(start), 3)


def _timing_ms(timings: TreeTiming, key: str) -> float:
    value = timings.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _timing_count(timings: TreeTiming, key: str) -> int:
    value = timings.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def _timing_enabled() -> bool:
    return os.environ.get("OPENING_TREE_TIMING_LOG", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _slow_tree_threshold_ms() -> float:
    raw = os.environ.get("SLOW_OPENING_TREE_LOG_MS")
    if raw is None:
        return SLOW_OPENING_TREE_LOG_MS
    try:
        return float(raw)
    except ValueError:
        return SLOW_OPENING_TREE_LOG_MS


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
    game_count: int
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
    game_count: int | None
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
    subtree_game_count: int
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
    game_count: int | None
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


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _direct_branch_stats(row: CachedOpeningScoreRow | None) -> CurrentBranchStats:
    """Direct-row current-branch stats. ``root_count`` is direct-row presence."""
    if row is None:
        return CurrentBranchStats(
            score=None,
            confidence=None,
            coverage=None,
            sample_size=None,
            game_count=None,
            root_count=0,
        )
    return CurrentBranchStats(
        score=row.opening_score,
        confidence=row.confidence,
        coverage=row.coverage,
        sample_size=row.sample_size,
        game_count=row.game_count,
        root_count=1,
    )


def build_family_scores(rows: list[CachedOpeningScoreRow]) -> list[FamilyScoreItem]:
    """Aggregate per-root cached scores into per-family items.

    The synthetic whole-repertoire row is excluded so it never pollutes a family.
    """
    rows = [row for row in rows if row.opening_family != SYNTHETIC_ROOT_FAMILY]
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
                    game_count=None,
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
                game_count=row.game_count,
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
        view = direct_branch_view(rows_by_key, child.opening_key, roots_registry)
        direct = view.direct_row
        weakest = view.weakest_root

        items.append(
            OpeningChildItem(
                opening_key=child.opening_key,
                opening_name=child.opening_name,
                opening_family=child.opening_family,
                eco=child.eco,
                depth=child.depth,
                child_count=len(roots_registry.get_children(child.opening_key)),
                # Card score/sample/last-practiced are the child's DIRECT row.
                subtree_score=direct.opening_score if direct is not None else None,
                subtree_confidence=direct.confidence if direct is not None else None,
                subtree_coverage=direct.coverage if direct is not None else None,
                subtree_sample_size=direct.sample_size if direct is not None else 0,
                subtree_game_count=direct.game_count if direct is not None else 0,
                # Navigation metadata only — count of scored named rows in subtree.
                subtree_root_count=view.scored_root_count,
                last_practiced_at=direct.last_practiced_at if direct is not None else None,
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
        game_count=rs.game_count,
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
    batch, row_views = load_cached_rows(db, user.user_id, player_color)
    computed_at = batch.computed_at if batch is not None else None
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
    roots_registry = get_opening_roots()
    if not roots_registry.get_family(family_name):
        raise HTTPException(status_code=404, detail="Unknown opening family")

    batch, row_views = load_cached_rows(db, user.user_id, player_color)
    computed_at = batch.computed_at if batch is not None else None

    # Branch summaries are persisted from the shared calculation; read them
    # straight from the cached rows (no per-root recompute).
    root_items, scored_count = build_drill_down_roots(
        row_views,
        family_name,
        roots_registry,
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
    roots_registry = get_opening_roots()
    if parent_key is not None and roots_registry.get_root(parent_key) is None:
        raise HTTPException(status_code=404, detail="Unknown opening root")

    batch, row_views = load_cached_rows(db, user.user_id, player_color)
    computed_at = batch.computed_at if batch is not None else None

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
    # Top level: synthetic whole-repertoire row. Drilled in: the selected root's
    # own direct row. No descendant aggregation.
    current_branch_row = rows_by_key.get(
        SYNTHETIC_INITIAL_FEN
        if canonical_parent_key is None
        else canonical_parent_key
    )
    return ChildrenResponse(
        player_color=player_color,
        parent_key=canonical_parent_key,
        parent_name=parent_root.opening_name if parent_root is not None else None,
        canonical_opening_key=canonical_parent_key,
        canonical_path=canonical_path,
        breadcrumbs=build_opening_breadcrumbs(route_roots),
        current_branch_stats=_direct_branch_stats(current_branch_row),
        children=children,
        total_children=len(children),
        computed_at=computed_at,
    )


# ---------------------------------------------------------------------------
# GET /api/openings/tree — horizontal move-graph read model (epic g-d5cu)
#
# Returns one hydrated column per position along a canonical move line so a deep
# link / refresh renders in a single request. Does ZERO per-request scoring and
# NO per-request overlay rebuild: structural shape comes from the opening graph +
# the persisted observed-edge read model (opening_position_edges, read by bounded
# per-parent lookups), direct metrics from the persisted batch, evals from the
# analysis_cache.
# ---------------------------------------------------------------------------


class TreeNode(BaseModel):
    parent_fen: str            # normalized 4-field
    child_fen: str             # normalized 4-field
    uci: str
    san: str
    ply: int
    opening_name: str | None
    eco: str | None
    in_book: bool
    is_navigable: bool         # uci in _structural_children(parent); gates clicks/drops
    is_observed: bool
    is_prepared: bool
    user_choice_count: int
    encounter_count: int
    opening_score: float | None
    confidence: float | None
    coverage: float | None
    sample_size: int | None
    game_count: int | None
    last_practiced_at: datetime | None
    eval_cp: int | None        # white-relative
    eval_mate: int | None      # white-relative
    terminal_reason: str | None
    drill_opening_key: str | None
    is_selected: bool


class TreeColumn(BaseModel):
    position_fen: str          # normalized 4-field
    ply: int
    selected_uci: str | None
    nodes: list[TreeNode]


class TreeResponse(BaseModel):
    player_color: str
    canonical_line: list[str]          # UCI
    selected_fen: str                  # normalized pos[k]
    selected_ply: int
    selected_is_terminal: bool
    selected_terminal_reason: str | None
    drill_opening_key: str | None      # roots.get_root(selected_fen)
    root_eval_cp: int | None           # column-0 start position best_eval (white-rel)
    root_eval_mate: int | None
    root_opening_score: float | None   # start-position metrics from position_rows (None when no evidence)
    root_coverage: float | None
    root_game_count: int | None
    root_confidence: float | None
    columns: list[TreeColumn]
    batch_computed_at: datetime | None
    model_version: str


@dataclass(frozen=True)
class _ChildEdge:
    """One candidate move out of a position, merged by UCI across book + observed.

    ``in_book`` is true iff a reference (book) edge carries this UCI; ``is_observed``
    iff the user's persisted observed-edge read model (``opening_position_edges``)
    carries it. ``edge`` is the observed :class:`EdgeEvidence` reconstructed from that
    row (None for a book-only move).
    """

    uci: str
    child_fen: str             # normalized 4-field
    in_book: bool
    is_observed: bool
    edge: EdgeEvidence | None


@dataclass
class _RawNode:
    """Column node before per-request metric/eval/name hydration."""

    parent_fen: str
    child_fen: str
    uci: str
    san: str
    ply: int
    in_book: bool
    is_navigable: bool
    is_observed: bool
    is_prepared: bool
    user_choice_count: int
    encounter_count: int
    terminal_reason: str | None
    own_name: str | None
    own_eco: str | None
    drill_opening_key: str | None
    parent_full_fen: str       # six-field, for the (parent_fen, uci) eval key


class _OpeningTreeBuilder:
    """Builds the ``/tree`` read model for one (user, color) request.

    Structural shape (which moves exist, which are navigable, which are terminal)
    is a pure function of the opening graph + observed tree edges; this class derives
    it, then performs exactly the batched DB lookups the epic mandates — bounded
    per-parent observed-edge lookups, one position-row load, one move-eval batch, one
    root-eval — to hydrate metrics and evals. It never scores on the request path
    (the cold bootstrap inside ``ensure_tree_cache`` is the only exception, and it is
    bounded).

    Observed edges come from the persisted ``opening_position_edges`` read model via
    lazy, memoized per-parent point queries keyed by ``batch_id``: the builder holds
    only the scalar ``batch_id`` / ``batch_computed_at`` the route resolved BEFORE its
    ``db.rollback()``, never an ORM batch row, so no surprise refresh SELECT can fire
    after the rollback. ``batch_id is None`` (cold / no-evidence user) yields a
    book-only structural tree.
    """

    def __init__(
        self,
        db: Session,
        graph: OpeningGraph,
        roots: OpeningRoots,
        batch_id: int | None,
        batch_computed_at: datetime | None,
        player_color: str,
        user_id: int,
    ) -> None:
        self.db = db
        self.graph = graph
        self.roots = roots
        self.batch_id = batch_id
        self.batch_computed_at = batch_computed_at
        self.player_color = player_color
        self.user_id = user_id

        # Per-request memoization (game_phase.is_middlegame_position rebuilds a
        # board + mixedness on every call; the children sets are revisited during
        # replay, resolution, and terminal-reason derivation).
        self._mid_cache: dict[str, bool] = {}
        self._struct_cache: dict[str, dict[str, _ChildEdge]] = {}
        self._column_cache: dict[str, dict[str, _ChildEdge]] = {}

        # Lazy, memoized per-parent observed-edge cache. Loaded only for the parents
        # the builder actually visits (visible line ∪ rendered frontier), so a warm
        # read is bounded by visible nodes, not total session history.
        self._observed_cache: dict[str, list[EdgeEvidence]] = {}
        # Instrumentation for the route timing log (Finding: prove the read is
        # bounded by line+frontier, not by total observed edges).
        self._observed_edge_query_count = 0
        self._observed_edge_row_count = 0

    # -- structural shape ---------------------------------------------------

    def _observed_children(self, norm_fen: str) -> list[EdgeEvidence]:
        """Observed edges out of ``norm_fen``, loaded lazily from the cache batch.

        Stored edge keys are already normalized 4-field FENs (``_record_edge`` keys
        on ``norm_before`` / ``norm_after``), matching ``norm_fen``, so no
        renormalization. ``batch_id is None`` ⇒ no cache ⇒ book-only tree.
        """
        hit = self._observed_cache.get(norm_fen)
        if hit is None:
            if self.batch_id is None:
                hit = []
            else:
                hit = lookup_observed_edges_for_parent(self.db, self.batch_id, norm_fen)
                self._observed_edge_query_count += 1
                self._observed_edge_row_count += len(hit)
            self._observed_cache[norm_fen] = hit
        return hit

    def _is_middlegame(self, norm_fen: str) -> bool:
        cached = self._mid_cache.get(norm_fen)
        if cached is None:
            cached = is_middlegame_position(norm_fen)
            self._mid_cache[norm_fen] = cached
        return cached

    def _book_children(self, norm_fen: str) -> dict[str, str]:
        node = self.graph.get_node(norm_fen)
        if node is None:
            return {}
        return dict(node.children)

    def _structural_children(self, norm_fen: str) -> dict[str, _ChildEdge]:
        """Navigable moves out of ``norm_fen``, merged by UCI (mirrors the scorer).

        Observed edges are ALWAYS included (phase-authoritative); book edges are
        included only when their child is not a middlegame position. This is the
        navigable set — exactly the scorer's ``_structural_children`` domain — and
        the only set a move in ``canonical_line`` may come from.
        """
        cached = self._struct_cache.get(norm_fen)
        if cached is not None:
            return cached

        book = self._book_children(norm_fen)
        result: dict[str, _ChildEdge] = {}
        # Observed children: always navigable, never middlegame-filtered.
        for edge in self._observed_children(norm_fen):
            result[edge.uci] = _ChildEdge(
                uci=edge.uci,
                child_fen=edge.child_fen,
                in_book=edge.uci in book,
                is_observed=True,
                edge=edge,
            )
        # Reference (book) children whose child is not a middlegame position.
        for uci, child_fen in book.items():
            if self._is_middlegame(child_fen):
                continue
            if uci not in result:
                result[uci] = _ChildEdge(
                    uci=uci,
                    child_fen=child_fen,
                    in_book=True,
                    is_observed=False,
                    edge=None,
                )
        self._struct_cache[norm_fen] = result
        return result

    def _column_children(self, norm_fen: str) -> dict[str, _ChildEdge]:
        """Displayed moves: the navigable set plus the display-only boundary.

        When the parent is itself not yet a middlegame position, its middlegame
        book children are shown as terminal, non-navigable boundary nodes (the
        first move that crosses into the middlegame). A middlegame parent — only
        reachable via an observed continuation — adds no such boundary, so
        ``_column_children`` ⊇ ``_structural_children`` always holds.
        """
        cached = self._column_cache.get(norm_fen)
        if cached is not None:
            return cached

        result = dict(self._structural_children(norm_fen))
        if not self._is_middlegame(norm_fen):
            for uci, child_fen in self._book_children(norm_fen).items():
                if uci in result:
                    continue
                if self._is_middlegame(child_fen):
                    # Display-only middlegame book boundary: shown, terminal,
                    # non-navigable, outside the scorer domain (null metrics).
                    result[uci] = _ChildEdge(
                        uci=uci,
                        child_fen=child_fen,
                        in_book=True,
                        is_observed=False,
                        edge=None,
                    )
        self._column_cache[norm_fen] = result
        return result

    def _is_leaf(self, board: chess.Board, norm_fen: str) -> bool:
        return (
            board.is_checkmate()
            or board.is_stalemate()
            or not self._column_children(norm_fen)
        )

    def _terminal_reason_for_position(
        self, board: chess.Board, norm_fen: str
    ) -> str | None:
        """Terminal reason for a position itself (the selected pos[k], Bug B)."""
        if board.is_checkmate():
            return "checkmate"
        if board.is_stalemate():
            return "stalemate"
        if not self._column_children(norm_fen):
            return "opening_boundary" if self._is_middlegame(norm_fen) else "no_children"
        return None

    def _terminal_reason_for_child(
        self,
        child_is_mate: bool,
        child_is_stale: bool,
        is_navigable: bool,
        child_norm: str,
    ) -> str | None:
        """Terminal reason for a column node. Precedence matters (Gap I): a short
        mate is tested via the board before any boundary/navigability label."""
        if child_is_mate:
            return "checkmate"
        if child_is_stale:
            return "stalemate"
        if not is_navigable:
            # A display-only middlegame book boundary move.
            return "opening_boundary"
        if not self._column_children(child_norm):
            return "opening_boundary" if self._is_middlegame(child_norm) else "no_children"
        return None

    # -- canonical line resolution -----------------------------------------

    def _resolve_moves(self, moves: list[str]) -> list[str]:
        """Validate + truncate a UCI move line to its canonical navigable prefix.

        Malformed UCI is a client error (422); a well-formed-but-non-navigable,
        illegal, cyclic, or over-deep move truncates the line (canonical-URL
        behavior). The legality guard runs BEFORE ``push`` so a corrupt/synthetic
        observed edge that passed the structural check can never corrupt replay
        (finding #4).
        """
        board = chess.Board()
        visited = {normalize_fen(board.fen())}
        line: list[str] = []
        for token in moves:
            try:
                move = chess.Move.from_uci(token)
            except ValueError:
                raise HTTPException(status_code=422, detail="Malformed move in line")
            if len(line) >= MAX_TREE_PLY:
                break
            parent_norm = normalize_fen(board.fen())
            if self._is_leaf(board, parent_norm):
                break
            if token not in self._structural_children(parent_norm):
                break
            if move not in board.legal_moves:
                break
            board.push(move)
            child_norm = normalize_fen(board.fen())
            if child_norm in visited:
                board.pop()
                break
            visited.add(child_norm)
            line.append(token)
        return line

    def _bfs_book_path(self, target_norm: str) -> list[str] | None:
        """Shortest book UCI path from the graph root to ``target_norm``.

        Forward BFS over ``node.children`` with an explicit visited normalized-FEN
        set (finding #3): bounds work and gives a stable first-arrival path in the
        cyclic/transposed graph. Ties broken by UCI order. None when unreachable.
        """
        root = self.graph.root_fen
        if root == target_norm:
            return []
        visited = {root}
        queue: deque[tuple[str, list[str]]] = deque([(root, [])])
        while queue:
            fen, path = queue.popleft()
            if len(path) >= MAX_TREE_PLY:
                continue
            node = self.graph.get_node(fen)
            if node is None:
                continue
            for uci in sorted(node.children.keys()):
                child = node.children[uci]
                if child == target_norm:
                    return [*path, uci]
                if child in visited:
                    continue
                visited.add(child)
                queue.append((child, [*path, uci]))
        return None

    def resolve_line(self, moves: list[str], opening: str | None) -> list[str]:
        if moves:
            return self._resolve_moves(moves)
        if opening:
            try:
                target_norm = normalize_fen(opening)
            except ValueError:
                raise HTTPException(status_code=422, detail="Malformed opening FEN")
            path = self._bfs_book_path(target_norm)
            if not path:
                return []
            # Re-validate the book path through the same move validator so the
            # legacy and move-param entrypoints can never diverge (Bug C).
            return self._resolve_moves(path)
        return []

    # -- build --------------------------------------------------------------

    def build(
        self,
        moves: list[str],
        opening: str | None,
        *,
        timings: TreeTiming | None = None,
    ) -> TreeResponse:
        build_started = time.perf_counter()
        stage_started = time.perf_counter()
        line = self.resolve_line(moves, opening)
        _record_timing(timings, "resolve_line_ms", stage_started)
        if timings is not None:
            timings["canonical_ply"] = len(line)

        # Replay the canonical line, keeping full FENs (eval keys) and normalized
        # FENs (graph/score identity) plus a board per ply for SAN + mate tests.
        stage_started = time.perf_counter()
        board = chess.Board()
        pos_full = [board.fen()]
        pos_norm = [normalize_fen(pos_full[0])]
        boards = [board.copy()]
        for token in line:
            move = chess.Move.from_uci(token)
            if move not in board.legal_moves:  # defensive; line is pre-validated
                break
            board.push(move)
            pos_full.append(board.fen())
            pos_norm.append(normalize_fen(board.fen()))
            boards.append(board.copy())
        k = len(boards) - 1
        line = line[:k]
        _record_timing(timings, "replay_line_ms", stage_started)

        # Deepest opening name/eco at or above each position along the line, so a
        # child without its own graph name inherits the deepest named ancestor.
        stage_started = time.perf_counter()
        line_name: list[str | None] = [None] * (k + 1)
        line_eco: list[str | None] = [None] * (k + 1)
        cur_name = cur_eco = None
        for i in range(k + 1):
            node_i = self.graph.get_node(pos_norm[i])
            if node_i is not None and node_i.name is not None:
                cur_name, cur_eco = node_i.name, node_i.eco
            line_name[i] = cur_name
            line_eco[i] = cur_eco
        _record_timing(timings, "line_names_ms", stage_started)

        # Pass 1: structural column build (board-derived fields) + lookup keys.
        stage_started = time.perf_counter()
        raw_columns: list[tuple[int, str, str | None, list[_RawNode]]] = []
        eval_requests: list[tuple[str, str]] = []
        position_fens: set[str] = set()
        for i in range(k + 1):
            board_i = boards[i]
            norm_i = pos_norm[i]
            if self._is_leaf(board_i, norm_i):
                continue  # no reveal column (only reachable at i == k)
            structural = self._structural_children(norm_i)
            selected_uci = line[i] if i < k else None
            raw_nodes: list[_RawNode] = []
            for uci, child in self._column_children(norm_i).items():
                try:
                    move = chess.Move.from_uci(uci)
                except ValueError:
                    continue
                if move not in board_i.legal_moves:
                    # Structurally listed but board-illegal (synthetic/corrupt
                    # observed edge on a transposed full position): skip, never
                    # 500 (Gap H / finding #4). Book UCIs are always legal here.
                    continue
                san = board_i.san(move)
                board_i.push(move)
                child_is_mate = board_i.is_checkmate()
                child_is_stale = board_i.is_stalemate()
                board_i.pop()
                is_navigable = uci in structural
                node_obj = self.graph.get_node(child.child_fen)
                edge = child.edge
                drill_root = self.roots.get_root(child.child_fen)
                raw_nodes.append(
                    _RawNode(
                        parent_fen=norm_i,
                        child_fen=child.child_fen,
                        uci=uci,
                        san=san,
                        ply=i + 1,
                        in_book=child.in_book,
                        is_navigable=is_navigable,
                        is_observed=child.is_observed,
                        is_prepared=bool(
                            edge is not None
                            and (edge.live_attempts >= 2 or edge.live_passes >= 1)
                        ),
                        user_choice_count=edge.live_attempts if edge is not None else 0,
                        encounter_count=edge.traversal_count if edge is not None else 0,
                        terminal_reason=self._terminal_reason_for_child(
                            child_is_mate, child_is_stale, is_navigable, child.child_fen
                        ),
                        own_name=node_obj.name if node_obj is not None else None,
                        own_eco=node_obj.eco if node_obj is not None else None,
                        drill_opening_key=(
                            drill_root.opening_key if drill_root is not None else None
                        ),
                        parent_full_fen=pos_full[i],
                    )
                )
                eval_requests.append((pos_full[i], uci))
                position_fens.add(child.child_fen)
            raw_columns.append((i, norm_i, selected_uci, raw_nodes))
        # Always include the root position so we can expose its metrics on the
        # Starting position card (the batch may have a row for it).
        position_fens.add(pos_norm[0])
        _record_timing(timings, "structural_columns_ms", stage_started)
        if timings is not None:
            timings["raw_column_count"] = len(raw_columns)
            timings["raw_node_count"] = sum(len(nodes) for _, _, _, nodes in raw_columns)
            timings["eval_request_count"] = len(eval_requests)
            timings["position_fen_count"] = len(position_fens)

        # Batched DB lookups against the batch the route already resolved (no
        # scheduler re-trigger, no ORM batch row): one metric load, one eval batch.
        stage_started = time.perf_counter()
        position_rows = (
            lookup_position_scores_for_batch(self.db, self.batch_id, position_fens)
            if self.batch_id is not None
            else {}
        )
        _record_timing(timings, "position_rows_ms", stage_started)
        if timings is not None:
            timings["batch_present"] = self.batch_id is not None
            timings["position_row_count"] = len(position_rows)

        stage_started = time.perf_counter()
        move_evals = lookup_move_evals(self.db, eval_requests)
        _record_timing(timings, "move_evals_ms", stage_started)
        if timings is not None:
            timings["move_eval_hit_count"] = sum(
                1 for ev in move_evals.values() if ev is not None
            )

        stage_started = time.perf_counter()
        root_eval = lookup_root_eval(self.db, pos_full[0])
        _record_timing(timings, "root_eval_ms", stage_started)
        if timings is not None:
            timings["root_eval_hit"] = root_eval is not None

        # Pass 2: hydrate metrics/evals/names, mark selection, sort each column.
        stage_started = time.perf_counter()
        columns: list[TreeColumn] = []
        for i, norm_i, selected_uci, raw_nodes in raw_columns:
            user_turn = active_color(norm_i) == self.player_color
            nodes = [
                self._hydrate_node(
                    rn, position_rows, move_evals, line_name[i], line_eco[i], selected_uci
                )
                for rn in raw_nodes
            ]
            nodes.sort(key=lambda node: self._sort_key(node, user_turn))
            columns.append(
                TreeColumn(
                    position_fen=norm_i,
                    ply=i,
                    selected_uci=selected_uci,
                    nodes=nodes,
                )
            )
        _record_timing(timings, "hydrate_sort_ms", stage_started)

        stage_started = time.perf_counter()
        sel_norm = pos_norm[k]
        selected_terminal_reason = self._terminal_reason_for_position(boards[k], sel_norm)
        selected_root = self.roots.get_root(sel_norm)
        root_row = position_rows.get(pos_norm[0])
        _record_timing(timings, "selected_terminal_ms", stage_started)
        if timings is not None:
            timings["response_column_count"] = len(columns)
            timings["response_node_count"] = sum(len(column.nodes) for column in columns)
            timings["builder_total_ms"] = round(_elapsed_ms(build_started), 3)
        return TreeResponse(
            player_color=self.player_color,
            canonical_line=line,
            selected_fen=sel_norm,
            selected_ply=k,
            selected_is_terminal=selected_terminal_reason is not None,
            selected_terminal_reason=selected_terminal_reason,
            drill_opening_key=(
                selected_root.opening_key if selected_root is not None else None
            ),
            root_eval_cp=root_eval.cp if root_eval is not None else None,
            root_eval_mate=root_eval.mate if root_eval is not None else None,
            root_opening_score=root_row.opening_score if root_row is not None else None,
            root_coverage=root_row.coverage if root_row is not None else None,
            root_game_count=root_row.game_count if root_row is not None else None,
            root_confidence=root_row.confidence if root_row is not None else None,
            columns=columns,
            batch_computed_at=self.batch_computed_at,
            model_version=SCORE_MODEL_VERSION,
        )

    def _hydrate_node(
        self,
        rn: _RawNode,
        position_rows: dict[str, CachedPositionScoreRow],
        move_evals: dict,
        inherited_name: str | None,
        inherited_eco: str | None,
        selected_uci: str | None,
    ) -> TreeNode:
        row = position_rows.get(rn.child_fen)
        ev = move_evals.get((rn.parent_full_fen, rn.uci))
        has_own_name = rn.own_name is not None
        return TreeNode(
            parent_fen=rn.parent_fen,
            child_fen=rn.child_fen,
            uci=rn.uci,
            san=rn.san,
            ply=rn.ply,
            opening_name=rn.own_name if has_own_name else inherited_name,
            eco=rn.own_eco if has_own_name else inherited_eco,
            in_book=rn.in_book,
            is_navigable=rn.is_navigable,
            is_observed=rn.is_observed,
            is_prepared=rn.is_prepared,
            user_choice_count=rn.user_choice_count,
            encounter_count=rn.encounter_count,
            opening_score=row.opening_score if row is not None else None,
            confidence=row.confidence if row is not None else None,
            coverage=row.coverage if row is not None else None,
            sample_size=row.sample_size if row is not None else None,
            game_count=row.game_count if row is not None else None,
            last_practiced_at=row.last_practiced_at if row is not None else None,
            eval_cp=ev.cp if ev is not None else None,
            eval_mate=ev.mate if ev is not None else None,
            terminal_reason=rn.terminal_reason,
            drill_opening_key=rn.drill_opening_key,
            is_selected=selected_uci is not None and rn.uci == selected_uci,
        )

    @staticmethod
    def _sort_key(node: TreeNode, user_turn: bool) -> tuple:
        """Relevance order per the parent's side to move. Engine eval NEVER orders.

        User turn: observed first, then most-chosen, then weakest mastery (null
        last). Opponent turn: most-encountered first, then weakest mastery. The
        deterministic destination/source/promotion/UCI tail makes distinct UCIs a
        total order (they never tie)."""
        move = chess.Move.from_uci(node.uci)
        tiebreak = (
            chess.square_file(move.to_square),
            chess.square_rank(move.to_square),
            chess.square_file(move.from_square),
            chess.square_rank(move.from_square),
            move.promotion or 0,
            node.uci,
        )
        # Opening score ascending, null last.
        score_key = (
            node.opening_score is None,
            node.opening_score if node.opening_score is not None else 0.0,
        )
        if user_turn:
            return (not node.is_observed, -node.user_choice_count, score_key, tiebreak)
        return (node.encounter_count <= 0, -node.encounter_count, score_key, tiebreak)


@router.get("/tree", response_model=TreeResponse)
def get_opening_tree(
    player_color: Literal["white", "black"] = Query(...),
    move: list[str] = Query([]),
    opening: str | None = Query(None),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> TreeResponse:
    """Hydrated horizontal move-graph for a (color, move-line) deep link.

    ``move`` is a repeated UCI parameter giving the selected line; ``opening`` is
    the legacy normalized-FEN entry used only when ``move`` is empty. Stale or
    invalid lines canonicalize (truncate) to ``canonical_line``; a malformed UCI
    or a malformed ``opening`` FEN is a 422.
    """
    request_started = time.perf_counter()
    timings: TreeTiming = {}

    stage_started = time.perf_counter()
    graph = get_opening_graph()
    _record_timing(timings, "graph_ms", stage_started)

    stage_started = time.perf_counter()
    roots = get_opening_roots()
    _record_timing(timings, "roots_ms", stage_started)

    # Resolve the cache batch to serve from — the ONLY scheduler trigger on this
    # path. Warm-fresh batches serve immediately (background revalidate); a cold or
    # registry/schema-stale batch (e.g. predating edges-v1, so it has no observed
    # edge rows) blocks for a one-time bootstrap so observed moves are never hidden.
    # The observed edges themselves are then read lazily by the builder from the
    # persisted opening_position_edges read model — no overlay rebuild on this path.
    stage_started = time.perf_counter()
    batch_id, batch_computed_at, cache_state = ensure_tree_cache(
        db, user.user_id, player_color, graph, roots
    )
    _record_timing(timings, "ensure_cache_ms", stage_started)
    timings["cache_state"] = cache_state

    # Release the checked-out connection before the (read-only) structural pass
    # and batched lookups, mirroring /score (openings.py). Scalars above were
    # captured pre-rollback so no ORM batch field is read afterward.
    stage_started = time.perf_counter()
    db.rollback()
    _record_timing(timings, "rollback_ms", stage_started)

    builder = _OpeningTreeBuilder(
        db, graph, roots, batch_id, batch_computed_at, player_color, user.user_id
    )
    response = builder.build(move, opening, timings=timings)
    timings["observed_edge_query_count"] = builder._observed_edge_query_count
    timings["observed_edge_row_count"] = builder._observed_edge_row_count

    total_ms = round(_elapsed_ms(request_started), 3)
    timings["total_ms"] = total_ms
    if _timing_enabled() or total_ms >= _slow_tree_threshold_ms():
        logger.info(
            "opening_tree timing user_id=%s player_color=%s total_ms=%.3f "
            "move_count=%d has_opening_param=%s canonical_ply=%d graph_ms=%.3f "
            "roots_ms=%.3f ensure_cache_ms=%.3f cache_state=%s rollback_ms=%.3f "
            "resolve_line_ms=%.3f "
            "replay_line_ms=%.3f line_names_ms=%.3f structural_columns_ms=%.3f "
            "position_rows_ms=%.3f move_evals_ms=%.3f root_eval_ms=%.3f "
            "hydrate_sort_ms=%.3f selected_terminal_ms=%.3f builder_total_ms=%.3f "
            "observed_edge_queries=%d observed_edge_rows=%d raw_columns=%d "
            "raw_nodes=%d response_columns=%d response_nodes=%d position_fens=%d "
            "position_rows=%d eval_requests=%d move_eval_hits=%d batch_present=%s "
            "root_eval_hit=%s selected_terminal=%s",
            user.user_id,
            player_color,
            total_ms,
            len(move),
            opening is not None,
            _timing_count(timings, "canonical_ply"),
            _timing_ms(timings, "graph_ms"),
            _timing_ms(timings, "roots_ms"),
            _timing_ms(timings, "ensure_cache_ms"),
            timings.get("cache_state"),
            _timing_ms(timings, "rollback_ms"),
            _timing_ms(timings, "resolve_line_ms"),
            _timing_ms(timings, "replay_line_ms"),
            _timing_ms(timings, "line_names_ms"),
            _timing_ms(timings, "structural_columns_ms"),
            _timing_ms(timings, "position_rows_ms"),
            _timing_ms(timings, "move_evals_ms"),
            _timing_ms(timings, "root_eval_ms"),
            _timing_ms(timings, "hydrate_sort_ms"),
            _timing_ms(timings, "selected_terminal_ms"),
            _timing_ms(timings, "builder_total_ms"),
            _timing_count(timings, "observed_edge_query_count"),
            _timing_count(timings, "observed_edge_row_count"),
            _timing_count(timings, "raw_column_count"),
            _timing_count(timings, "raw_node_count"),
            _timing_count(timings, "response_column_count"),
            _timing_count(timings, "response_node_count"),
            _timing_count(timings, "position_fen_count"),
            _timing_count(timings, "position_row_count"),
            _timing_count(timings, "eval_request_count"),
            _timing_count(timings, "move_eval_hit_count"),
            timings.get("batch_present"),
            timings.get("root_eval_hit"),
            response.selected_is_terminal,
        )

    return response
