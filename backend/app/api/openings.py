from __future__ import annotations

from collections import defaultdict
from dataclasses import fields as dc_fields
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import UserOpeningScore
from app.opening_cache import ensure_opening_scores, recompute_opening_scores
from app.opening_evidence import overlay_evidence
from app.opening_graph import get_opening_graph
from app.opening_rootcalc import RootScore, compute_root_score
from app.opening_roots import get_opening_roots
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


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

def _weakest_root(rows: list[UserOpeningScore]) -> UserOpeningScore:
    """Pick the weakest root with deterministic tie-breaking."""
    return min(
        rows,
        key=lambda r: (r.opening_score, r.confidence, r.opening_name, r.opening_key),
    )


def build_family_scores(rows: list[UserOpeningScore]) -> list[FamilyScoreItem]:
    """Aggregate per-root cached scores into per-family items."""
    families_map: dict[str, list[UserOpeningScore]] = defaultdict(list)
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
    batch, rows = ensure_opening_scores(db, user.user_id, player_color)
    families = build_family_scores(rows)
    return FamilyScoresResponse(
        player_color=player_color,
        families=families,
        total_families=len(families),
        computed_at=batch.computed_at if batch is not None else None,
    )
