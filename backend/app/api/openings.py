from __future__ import annotations

import logging
import os
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, fields as dc_fields, replace
from datetime import datetime
from typing import Literal

import chess
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import active_color, normalize_fen
from app.game_phase import is_middlegame_position
from app.models import GameSession
from app.opening_aggregate import (
    CachedOpeningScoreRow,
    CachedPositionScoreRow,
    _weakest_root,
)
from app.opening_cache import (
    SCORE_MODEL_VERSION,
    ensure_tree_cache,
    load_cached_rows,
    lookup_observed_edges_for_parent,
    lookup_observed_edges_for_parents,
    lookup_position_scores_for_batch,
    observed_edge_parent_chunk_count,
    resolve_tree_cache_state,
)
from app.opening_densify import RoutingView, routing_view
from app.opening_transposition_artifact import (
    coverage_structural_edge_is_eligible,
    load_strict_densified_edges,
)
from app.opening_evidence import EdgeEvidence, overlay_evidence
from app.opening_graph import OpeningGraph, get_opening_graph
from app.opening_quality import mate_to_cp
from app.opening_rootcalc import (
    SYNTHETIC_ROOT_FAMILY,
    ReportSelfTermEffective,
    RootScore,
    compute_root_score,
)
from app.opening_roots import OpeningRoots, get_opening_roots
from app.opening_score_delta import OpeningScoreDeltaItem, read_opening_score_delta
from app.security import TokenPayload, get_current_user
from app.tree_eval import lookup_move_evals, lookup_root_eval

# Hard ply ceiling for a single resolved move line. Bounds replay/BFS work and
# truncates a pathologically deep (or adversarial) deep-link URL.
MAX_TREE_PLY = 80
SLOW_OPENING_TREE_LOG_MS = 1000.0

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/openings", tags=["openings"])

TreeTiming = dict[str, bool | float | int | str | None]

COVERAGE_DESCRIPTION = (
    "Depth-weighted percentage (0-100) of visited opening positions below the card. "
    "User turns follow chosen structural routes after a live choice; an off-book-only "
    "choice retains known breadth. Opponent turns retain all eligible reference and "
    "transposition breadth. FEN visits share across move orders, and observed off-book "
    "continuations share one terminal branch bucket."
)


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
    # Historical wire name: score-readiness gate, not visited-node Coverage.
    covered_locally: bool
    raw_score: float
    raw_confidence: float
    raw_coverage: float  # New visited-node fraction before conversion to 0-100.
    raw_depth: float
    is_leaf: bool
    # Report-stage observability (g-report-debug-api). Null for a FEN that was
    # visited during the DAG traversal but never reported as its own row; non-null
    # (even at identity defaults) once reported. The shared ReportSelfTermEffective
    # Literal makes Pydantic reject any out-of-vocabulary self-term spelling.
    pre_fold_quality: float | None = None
    reported_score: float | None = None
    report_fold_multiplier: float | None = None
    report_self_term_effective: ReportSelfTermEffective | None = None


class RootScoreResponse(BaseModel):
    opening_key: str
    opening_name: str
    opening_family: str
    player_color: str
    opening_score: float
    confidence: float
    coverage: float = Field(description=COVERAGE_DESCRIPTION)
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
    family_coverage: float = Field(
        description=f"Equal-root mean. {COVERAGE_DESCRIPTION}"
    )
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
    coverage: float | None = Field(description=COVERAGE_DESCRIPTION)
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


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

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
    routing_snapshot = load_strict_densified_edges(graph)

    overlay = overlay_evidence(db, user.user_id, body.player_color, graph)
    db.rollback()
    score = compute_root_score(
        body.opening_key,
        body.player_color,
        graph,
        overlay,
        roots,
        debug=debug,
        routing_snapshot=routing_snapshot,
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


# ---------------------------------------------------------------------------
# GET /api/openings/tree — horizontal move-graph read model (epic g-d5cu)
#
# Returns one hydrated column per position along a canonical move line so a deep
# link / refresh renders in a single request. Does ZERO per-request scoring and
# NO per-request overlay rebuild: structural shape comes from the opening graph +
# the persisted observed-edge read model (opening_position_edges, read in one batch
# load), direct metrics from the persisted batch, evals from the analysis_cache.
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
    is_navigable: bool         # uci in _navigable_children(parent) OR this column's user-selected move; gates node clicks only — board drops accept any legal move (g-obh5)
    is_observed: bool
    is_user_selected: bool     # legal move chosen on the board, outside the navigable set (g-obh5)
    is_transposition: bool = False  # edge supplied by the routing overlay: in the book through a different move order (g-openings-transpose)
    is_prepared: bool
    user_choice_count: int
    encounter_count: int
    opening_score: float | None
    confidence: float | None
    coverage: float | None = Field(description=COVERAGE_DESCRIPTION)
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
    root_coverage: float | None = Field(description=COVERAGE_DESCRIPTION)
    root_game_count: int | None
    root_confidence: float | None
    columns: list[TreeColumn]
    batch_computed_at: datetime | None
    model_version: str
    # Diagnostic cache signal from ensure_tree_cache (warm_fresh / bootstrapped /
    # book_only / bootstrap_timeout), set by the route after build. Lets a DIRECT
    # /tree caller (one that did not gate on /tree/status) detect a degraded
    # book-only/timeout tree and retry. The default covers builder-only construction
    # in tests; the route always overwrites it.
    cache_state: str = "warm_fresh"


class TreeStatusResponse(BaseModel):
    """Cheap, non-blocking cache-state probe for the ``/tree`` poll (g-k4z2).

    ``state`` is ``"warm"`` (load ``/tree`` now — it serves the cached batch),
    ``"building"`` (a one-time bootstrap is running — show the setup UI and keep
    polling), or ``"cold"`` (this poll just kicked the bootstrap off).
    """

    player_color: str
    state: str


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
    # True iff this exact (parent, uci) edge comes from the validated routing
    # overlay (g-openings-transpose). Independent of the other flags: an edge can
    # be observed AND a transposition; it is never in_book (the artifact only
    # carries edges absent from the base graph).
    is_transposition: bool = False


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
    is_user_selected: bool
    is_transposition: bool
    # Display-only first-boundary card (shown, terminal, not navigable). It sits
    # outside the scorer domain, so hydration must suppress its scorer metrics even
    # when a cached row exists for the destination — a middlegame position CAN own
    # a row via an observed move order, and showing it would contradict the
    # documented null-metrics contract for boundary cards.
    is_display_boundary: bool
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
    it, then performs exactly the batched DB lookups the epic mandates — a bounded
    observed-edge prefetch (2 waves), one position-row load, one move-eval batch, one
    root-eval — to hydrate metrics and evals. It never scores on the request path
    (the cold bootstrap inside ``ensure_tree_cache`` is the only exception, and it is
    bounded).

    Observed edges come from the persisted ``opening_position_edges`` read model via a
    BOUNDED 2-wave prefetch (``_prefetch_observed_edges``) issued inside ``build`` as
    the timed ``observed_prefetch_ms`` stage, indexed by parent FEN in memory: the
    builder holds only the scalar ``batch_id`` / ``batch_computed_at`` the route
    resolved BEFORE its ``db.rollback()``, never an ORM batch row, so no surprise
    refresh SELECT can fire after the rollback. The prefetch loads edges for ONLY the
    parents the build will visit — line positions (wave 1) then their column
    children/frontier (wave 2) — in 2 ``parent_fen IN (...)`` queries independent of
    node count, fetching ~tens of rows instead of a high-history user's whole edge
    history (g-0qe6 Option B, superseding the g-a6k2 whole-batch eager load). It covers
    the terminal-probe frontier; a parent the prefetch missed falls back to a single
    point query and is counted (``_observed_straggler_count``, expected 0). ``batch_id
    is None`` (cold / no-evidence user) yields a book-only structural tree with zero
    edge queries.
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
        routing: RoutingView | None = None,
    ) -> None:
        self.db = db
        self.graph = graph
        # Same immutable snapshot drill routing uses. None ⇒ an empty overlay, so
        # a missing/stale artifact (or a caller that does not supply one) renders
        # exactly today's base/observed tree.
        self.routing = routing if routing is not None else RoutingView(graph)
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
        self._nav_cache: dict[str, dict[str, _ChildEdge]] = {}
        self._column_cache: dict[str, dict[str, _ChildEdge]] = {}

        # Observed-edge read model, indexed by normalized parent FEN. Populated by
        # _prefetch_observed_edges() during build() (timed as observed_prefetch_ms)
        # rather than here, so the DB round-trip is never an unaccounted gap in the
        # route timing log. Empty until then; an absent batch (cold/no-evidence user)
        # leaves it empty ⇒ book-only tree.
        # Instrumentation for the route timing log: query count is the number of actual
        # chunked SELECTs issued (normally 2 on the warm path — one per wave, each a
        # single chunk — and 0 cold; a pathologically large wave that splits into
        # multiple IN-chunks bumps it per chunk, so it stays an honest round-trip
        # count), independent of node count; straggler count is per-parent fallbacks the
        # prefetch should never need (asserted 0 in tests — a non-zero value means the
        # prefetch under-collected).
        self._observed_cache: dict[str, list[EdgeEvidence]] = {}
        self._observed_edge_query_count = 0
        self._observed_edge_row_count = 0
        self._observed_straggler_count = 0

    # -- structural shape ---------------------------------------------------

    def _prefetch_observed_edges(self, pos_norm: list[str], k: int) -> None:
        """Bounded 2-wave prefetch of observed edges for exactly the parents the build
        will visit — line positions (wave 1) then their column children/frontier
        (wave 2) — in 2 ``parent_fen IN (...)`` queries, independent of node count.

        The complete set of parents for which ``_observed_children`` is ever read in
        one request is line positions ∪ their column children (the structural pass and
        the terminal-reason probe go exactly two levels deep, no further), so these two
        waves fully cover it. ``batch_id is None`` (cold / no-evidence user) is a no-op
        ⇒ empty cache ⇒ book-only tree. Called from ``build`` inside the timed
        ``observed_prefetch_ms`` stage so the DB round-trip is always accounted for in
        the route timing log (never an unattributed gap in ``total_ms``).
        """
        if self.batch_id is None:
            return
        # Wave 1: line positions pos_norm[0..k].
        line_fens = set(pos_norm[: k + 1])
        self._load_observed(line_fens)
        # Wave 2: frontier = all column children of every line position — including
        # transposition destinations, since _column_children now covers the overlay,
        # so those parents join this wave instead of falling through to the
        # per-parent straggler query (still two waves, not three). After wave 1
        # each line position's _column_children is a pure in-memory derivation (its own
        # observed edges are cached), so this issues no extra queries beyond the wave-2
        # IN-query. Frontier FENs that are themselves line positions (the on-line child
        # pos_norm[i+1]) are already cached, so _load_observed skips them.
        frontier: set[str] = set()
        for norm_i in line_fens:
            for child in self._column_children(norm_i).values():
                frontier.add(child.child_fen)
        self._load_observed(frontier)

    def _load_observed(self, fens: set[str]) -> None:
        """Load observed edges for the not-yet-cached subset of ``fens`` in one bounded
        ``IN`` read; populate the cache so every requested fen has an entry (absent
        parents → ``[]``).

        The visible set is small, so this is normally a single SELECT; a pathologically
        large subset is split into chunks (SQLite param cap), each one DB round-trip, so
        the query counter is bumped by the actual chunk count — not by 1 per wave — to
        stay an honest round-trip count rather than a wave count.
        """
        missing = {f for f in fens if f not in self._observed_cache}
        if not missing:
            return
        loaded = lookup_observed_edges_for_parents(self.db, self.batch_id, missing)
        for f in missing:
            edges = loaded.get(f, [])
            self._observed_cache[f] = edges
            self._observed_edge_row_count += len(edges)
        self._observed_edge_query_count += observed_edge_parent_chunk_count(len(missing))

    def _observed_children(self, norm_fen: str) -> list[EdgeEvidence]:
        """Observed edges out of ``norm_fen``, read from the prefetched cache.

        Stored edge keys are already normalized 4-field FENs (``_record_edge`` keys
        on ``norm_before`` / ``norm_after``), matching ``norm_fen``, so no
        renormalization. The 2-wave prefetch should cover every parent the build
        visits; a cache miss means it under-collected, so fall back to a single point
        query (keeping the tree correct) and count the straggler so the regression is
        observable (and asserted zero in tests). ``batch_id is None`` ⇒ ``[]``.
        """
        hit = self._observed_cache.get(norm_fen)
        if hit is None:
            if self.batch_id is None:
                hit = []
            else:
                hit = lookup_observed_edges_for_parent(self.db, self.batch_id, norm_fen)
                self._observed_straggler_count += 1
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
        included only when both endpoints are not middlegame positions. This is
        the navigable set — exactly the scorer's ``_structural_children`` domain —
        and the only set a move in ``canonical_line`` may come from.
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
        # Reference (book) children whose endpoints stay inside the opening
        # boundary. This is the same predicate the score-only coverage topology
        # uses for reference and routing-transposition edges.
        for uci, child_fen in book.items():
            if not coverage_structural_edge_is_eligible(
                norm_fen, child_fen, is_middlegame=self._is_middlegame
            ):
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

    def _overlay_children(self, norm_fen: str) -> Mapping[str, str]:
        """Routing-overlay edges out of ``norm_fen``, by UCI → child FEN.

        Read straight off the overlay rather than through
        ``RoutingView.routing_children``, which unions base and overlay edges and
        so cannot tell them apart — using the merged accessor would classify
        ordinary base boundary edges as transpositions.
        """
        return self.routing.overlay.children_of(norm_fen)

    def _navigable_children(self, norm_fen: str) -> dict[str, _ChildEdge]:
        """Clickable moves: the structural (scorer-domain) set plus browsable
        transpositions (g-openings-transpose).

        An overlay edge is navigable when it lands on a non-middlegame position
        and the parent is not itself a middlegame position — the overlay must
        never reopen a line past the opening boundary. Overlay edges never enter
        ``_structural_children``, so the scorer's domain, the graph fingerprint,
        and the opening roots are untouched; only browsing widens.

        An overlay edge that duplicates an observed one merges into a single card
        keeping the evidence, while still being identified as a transposition.
        """
        cached = self._nav_cache.get(norm_fen)
        if cached is not None:
            return cached

        overlay = self._overlay_children(norm_fen)
        # Provenance is tagged INDEPENDENTLY of navigation eligibility: an edge
        # that is in the overlay is a transposition even when it is also observed,
        # even when it crosses into the middlegame, and even when its parent is
        # already a middlegame position. Folding the tag into the eligibility
        # filter below would strip provenance from exactly those cases and render
        # them as "Off book" — a move from the player's own games, which they are
        # not.
        result = {
            uci: (replace(edge, is_transposition=True) if uci in overlay else edge)
            for uci, edge in self._structural_children(norm_fen).items()
        }
        # Eligibility, by contrast, is filtered: the overlay only volunteers NEW
        # forward-progress edges, and never past the opening boundary.
        for uci, child_fen in overlay.items():
            if uci in result:
                continue
            if not coverage_structural_edge_is_eligible(
                norm_fen, child_fen, is_middlegame=self._is_middlegame
            ):
                continue  # boundary-crossing: display-only, added by the column
            result[uci] = _ChildEdge(
                uci=uci,
                child_fen=child_fen,
                in_book=False,
                is_observed=False,
                edge=None,
                is_transposition=True,
            )
        self._nav_cache[norm_fen] = result
        return result

    def _column_children(self, norm_fen: str) -> dict[str, _ChildEdge]:
        """Displayed moves: the navigable set plus the display-only boundary.

        When the parent is itself not yet a middlegame position, its middlegame
        children — from the book AND from the routing overlay — are shown as
        terminal, non-navigable boundary nodes (the first move that crosses into
        the middlegame). A middlegame parent — only reachable via an observed
        continuation — adds no such boundary, so ``_column_children`` ⊇
        ``_navigable_children`` ⊇ ``_structural_children`` always holds.
        """
        cached = self._column_cache.get(norm_fen)
        if cached is not None:
            return cached

        result = dict(self._navigable_children(norm_fen))
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
            for uci, child_fen in self._overlay_children(norm_fen).items():
                if uci in result:
                    continue
                if self._is_middlegame(child_fen):
                    # Same display-only boundary treatment, overlay provenance.
                    result[uci] = _ChildEdge(
                        uci=uci,
                        child_fen=child_fen,
                        in_book=False,
                        is_observed=False,
                        edge=None,
                        is_transposition=True,
                    )
        self._column_cache[norm_fen] = result
        return result

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
        """Validate + truncate a UCI move line to its canonical legal prefix.

        Malformed UCI is a client error (422); a true game-over, illegal, cyclic,
        or over-deep move truncates the line (canonical-URL behavior). Any legal
        move is kept — a legal move past the book/observed frontier becomes a
        user-selected (third type) node (g-obh5) rather than being dropped, so the
        board can explore lines that are not yet in the tree. Cycles, illegality,
        and max ply (``MAX_TREE_PLY``) still bound abuse. The legality guard runs
        BEFORE ``push`` so a corrupt/synthetic edge can never corrupt replay
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
            if board.is_checkmate() or board.is_stalemate():
                break  # no legal continuations exist
            if move not in board.legal_moves:
                break  # keep illegal-move truncation
            board.push(move)
            child_norm = normalize_fen(board.fen())
            if child_norm in visited:
                board.pop()
                break  # keep cycle/repetition guard
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

        # Prefetch observed edges for only the visible parents (line ∪ frontier) in 2
        # bounded IN-queries before the structural pass reads them from memory. Timed
        # separately so this DB round-trip is never an unaccounted gap between rollback
        # and the (now in-memory) structural stage.
        stage_started = time.perf_counter()
        self._prefetch_observed_edges(pos_norm, k)
        _record_timing(timings, "observed_prefetch_ms", stage_started)

        # Pass 1: structural column build (board-derived fields) + lookup keys.
        stage_started = time.perf_counter()
        raw_columns: list[tuple[int, str, str | None, list[_RawNode]]] = []
        eval_requests: list[tuple[str, str]] = []
        position_fens: set[str] = set()
        for i in range(k + 1):
            board_i = boards[i]
            norm_i = pos_norm[i]
            if board_i.is_checkmate() or board_i.is_stalemate():
                continue  # genuinely no children
            navigable = self._navigable_children(norm_i)
            # Local copy (NOT the cache): the selected off-tree move is injected
            # per-line and must never leak into _column_cache. Transpositions are
            # persistent column members and DO go through the cache, so they stay
            # visible in a cached shorter-prefix view.
            persistent = self._column_children(norm_i)
            column = dict(persistent)
            selected_uci = line[i] if i < k else None
            if selected_uci is not None and selected_uci not in column:
                # A legal move past the book/observed frontier (g-obh5): the
                # resolver kept it in the line, so inject it as a navigable
                # user-selected node. Pre-validated legal by _resolve_moves.
                move = chess.Move.from_uci(selected_uci)
                board_i.push(move)
                child_full = board_i.fen()
                board_i.pop()
                column[selected_uci] = _ChildEdge(
                    uci=selected_uci,
                    child_fen=normalize_fen(child_full),
                    in_book=False,
                    is_observed=False,
                    edge=None,
                    # The overlay never *volunteers* a move out of a middlegame
                    # parent, but a manually selected one is still a transposition
                    # and is labelled as such on the wire.
                    is_transposition=selected_uci in self._overlay_children(norm_i),
                )
            if not column:
                continue  # leaf with nothing selected (only at i == k) — no reveal column
            raw_nodes: list[_RawNode] = []
            for uci, child in column.items():
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
                # A move selected on the board that is not in the navigable set is
                # the third move type: forced navigable for THIS line only (the
                # same move as an unselected sibling stays non-navigable). Tested
                # against `navigable`, not `structural`, so an ordinary selected
                # transposition is NOT relabelled "Your move" while a selected
                # middlegame boundary (base or overlay) still is, as today.
                is_user_selected = uci == selected_uci and uci not in navigable
                is_navigable = uci in navigable or is_user_selected
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
                        is_user_selected=is_user_selected,
                        is_transposition=child.is_transposition,
                        # Boundary membership is a property of the POSITION, not of
                        # this line's selection: a persistent column member outside
                        # the navigable set sits outside the scorer domain whether or
                        # not the user happens to have it selected. Derived from
                        # `persistent` (pre-injection) rather than `not is_navigable`,
                        # since selecting such a card promotes is_navigable and would
                        # otherwise restore the destination's cached row.
                        is_display_boundary=uci in persistent and uci not in navigable,
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
            side_to_move = active_color(norm_i)
            user_turn = side_to_move == self.player_color
            nodes = [
                self._hydrate_node(
                    rn, position_rows, move_evals, line_name[i], line_eco[i], selected_uci
                )
                for rn in raw_nodes
            ]
            nodes.sort(
                key=lambda node: self._sort_key(node, user_turn, side_to_move)
            )
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
        # Metrics follow the destination, EXCEPT for display-only boundary cards:
        # those sit outside the scorer domain, so a row the destination happens to
        # own (reached through some other, observed move order) must not leak onto
        # them. Evals are a position property, not a scorer metric, so they stay.
        row = None if rn.is_display_boundary else position_rows.get(rn.child_fen)
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
            is_user_selected=rn.is_user_selected,
            is_transposition=rn.is_transposition,
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
    def _eval_favorability(node: TreeNode, side_to_move: str) -> float | None:
        """Engine favorability used as the secondary sort key, keyed to the
        column's *side to move* (not the repertoire color): higher means better
        for the side whose move this column ranks. Mirrors
        ``opening_quality._white_cp``: prefer the centipawn value, fall back to a
        non-zero (sign-recoverable) mate via :func:`mate_to_cp` (a closer mate is
        more decisive and dominates any cp), and treat a mate-0 as unknown
        (``None``) so it sorts last rather than being mistaken for a
        White-favorable mate. A white-relative mate-0 has no recoverable winner —
        the perspective sign-flip (``mate * sign``) zeroes it — and
        ``tree_eval._played_eval`` collapses every mate row to mate-only, so the
        disambiguating cp never reaches here; ranking it would require a
        ``_played_eval`` change that drops the card's ``#`` checkmate display.
        The stored eval is white-relative, so it is flipped when Black is to
        move (a White column ranks highest white-relative first; a Black column
        ranks lowest/most-negative first)."""
        if node.eval_cp is not None:
            white_cp = float(node.eval_cp)
        elif node.eval_mate is not None and node.eval_mate != 0:
            white_cp = float(mate_to_cp(node.eval_mate))
        else:
            return None
        return white_cp if side_to_move == "white" else -white_cp

    @staticmethod
    def _sort_key(node: TreeNode, user_turn: bool, side_to_move: str) -> tuple:
        """Relevance order per the parent's side to move. Play frequency is the
        primary key; engine eval breaks play-frequency ties in favor of the
        side to move in this column (the best move for that column floats up).

        User turn: observed first, then most-chosen. Opponent turn:
        most-encountered first. Then engine eval (most favorable to the column's
        side to move first — White columns highest-first, Black columns
        lowest-first — unknown last) as the secondary key, then weakest mastery
        (null last), then a deterministic destination/source/promotion/UCI tail
        that makes distinct UCIs a total order (they never tie)."""
        move = chess.Move.from_uci(node.uci)
        tiebreak = (
            chess.square_file(move.to_square),
            chess.square_rank(move.to_square),
            chess.square_file(move.from_square),
            chess.square_rank(move.from_square),
            move.promotion or 0,
            node.uci,
        )
        # Engine eval, most favorable to the column's side to move first, unknown
        # last.
        favorability = _OpeningTreeBuilder._eval_favorability(node, side_to_move)
        eval_key = (
            favorability is None,
            -favorability if favorability is not None else 0.0,
        )
        # Opening score ascending, null last.
        score_key = (
            node.opening_score is None,
            node.opening_score if node.opening_score is not None else 0.0,
        )
        if user_turn:
            return (
                not node.is_observed,
                -node.user_choice_count,
                eval_key,
                score_key,
                tiebreak,
            )
        return (
            node.encounter_count <= 0,
            -node.encounter_count,
            eval_key,
            score_key,
            tiebreak,
        )


@router.get("/tree/status", response_model=TreeStatusResponse)
def get_opening_tree_status(
    player_color: Literal["white", "black"] = Query(...),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> TreeStatusResponse:
    """Cheap cache-state probe so the UI can show an explicit one-time
    "Setting up your opening tree…" state instead of a silent ~22s spinner.

    Does ONE indexed batch lookup (+ a ``limit=1`` evidence check only when there
    is no batch) — it never builds the overlay and never blocks on the bootstrap.
    On a cold/registry-stale (user, color) with evidence it fires the BACKGROUND
    recompute (non-blocking) and returns ``building``/``cold``; the UI polls until
    ``warm`` and then loads ``/tree`` (now fast). See ``resolve_tree_cache_state``.
    """
    graph = get_opening_graph()
    roots = get_opening_roots()
    state = resolve_tree_cache_state(db, user.user_id, player_color, graph, roots)
    return TreeStatusResponse(player_color=player_color, state=state)


class OpeningScoreDeltaPollResponse(BaseModel):
    """Non-blocking poll payload for the end-of-session opening-score banner.

    ``is_fresh`` is the poll-stop signal: True once the cached delta is provably
    current (or no opening was crossed), False while a cold/stale cache is still
    converging in the background. ``opening_score_changes`` is None when the played
    chain crossed no opening (or the cache cannot be read yet).
    """

    opening_score_changes: list[OpeningScoreDeltaItem] | None = None
    is_fresh: bool


@router.get("/score-delta/{session_id}", response_model=OpeningScoreDeltaPollResponse)
def get_opening_score_delta(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> OpeningScoreDeltaPollResponse:
    """Reconcile-poll for the end-of-session opening-score delta (g-fix-end-latency).

    The terminal endpoints (game end, drill fail/natural-end) now serve a warm,
    possibly-stale delta immediately and enqueue a background recompute instead of
    blocking up to ~10s on the scheduler. The frontend polls this GET until
    ``is_fresh`` and overwrites the banner in place with the provably-fresh value.
    One endpoint serves both game and drill sessions (both are ``GameSession`` with
    the same ``player_color`` / ``opening_score_baseline``). Non-blocking: never
    touches the scheduler (see ``read_opening_score_delta``).
    """
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    items, is_fresh = read_opening_score_delta(db, session)
    return OpeningScoreDeltaPollResponse(
        opening_score_changes=items or None, is_fresh=is_fresh
    )


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
    # The observed edges themselves are then read by the builder via a bounded 2-wave
    # prefetch over only the visible parents from the persisted opening_position_edges
    # read model — no overlay rebuild on this path.
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

    # Same load-once, provenance-validated snapshot drill routing uses. It already
    # degrades to an empty overlay (and logs) when the artifact is missing or
    # stale; the guard here covers anything it does NOT catch, so a browsing
    # nicety can never take /openings down — worst case the tree is the base +
    # observed one, exactly as before transposition cards existed.
    try:
        routing = routing_view(graph)
    except Exception:
        logger.exception(
            "opening_tree: routing view unavailable; serving the base tree without "
            "transposition cards"
        )
        routing = None
    builder = _OpeningTreeBuilder(
        db,
        graph,
        roots,
        batch_id,
        batch_computed_at,
        player_color,
        user.user_id,
        routing=routing,
    )
    response = builder.build(move, opening, timings=timings)
    response.cache_state = cache_state
    timings["observed_edge_query_count"] = builder._observed_edge_query_count
    timings["observed_edge_row_count"] = builder._observed_edge_row_count
    timings["observed_straggler_count"] = builder._observed_straggler_count

    total_ms = round(_elapsed_ms(request_started), 3)
    timings["total_ms"] = total_ms
    if _timing_enabled() or total_ms >= _slow_tree_threshold_ms():
        logger.info(
            "opening_tree timing user_id=%s player_color=%s total_ms=%.3f "
            "move_count=%d has_opening_param=%s canonical_ply=%d graph_ms=%.3f "
            "roots_ms=%.3f ensure_cache_ms=%.3f cache_state=%s rollback_ms=%.3f "
            "resolve_line_ms=%.3f "
            "replay_line_ms=%.3f line_names_ms=%.3f observed_prefetch_ms=%.3f "
            "structural_columns_ms=%.3f "
            "position_rows_ms=%.3f move_evals_ms=%.3f root_eval_ms=%.3f "
            "hydrate_sort_ms=%.3f selected_terminal_ms=%.3f builder_total_ms=%.3f "
            "observed_edge_queries=%d observed_edge_rows=%d observed_stragglers=%d "
            "raw_columns=%d "
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
            _timing_ms(timings, "observed_prefetch_ms"),
            _timing_ms(timings, "structural_columns_ms"),
            _timing_ms(timings, "position_rows_ms"),
            _timing_ms(timings, "move_evals_ms"),
            _timing_ms(timings, "root_eval_ms"),
            _timing_ms(timings, "hydrate_sort_ms"),
            _timing_ms(timings, "selected_terminal_ms"),
            _timing_ms(timings, "builder_total_ms"),
            _timing_count(timings, "observed_edge_query_count"),
            _timing_count(timings, "observed_edge_row_count"),
            _timing_count(timings, "observed_straggler_count"),
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
