from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.drill_steering import (
    DrillRouteMove,
    get_drill_route_map,
    route_move_for_uci,
    route_preserving_moves,
    safe_san_for_uci,
)
from app.fen import active_color, normalize_fen
from app.models import AnalysisCache, GameSession
from app.opening_graph import get_opening_graph
from app.opening_roots import get_opening_roots
from app.security import TokenPayload, get_current_user
from app.session_contracts import (
    DRILL_SESSION_MODE,
    resegment_session_moves,
    resolve_drill_threshold,
    utcnow,
)

router = APIRouter(prefix="/api/drills", tags=["drills"])
_logger = logging.getLogger(__name__)

def _resolve_eval_delta(entry: AnalysisCache, is_white_to_move: bool) -> int | None:
    if entry.eval_delta is not None:
        return entry.eval_delta
    if entry.played_eval is not None and entry.best_eval is not None:
        if is_white_to_move:
            return max(entry.best_eval - entry.played_eval, 0)
        return max(entry.played_eval - entry.best_eval, 0)
    return None


def _filter_suggestions(
    db: Session,
    suggestions: list[DrillRouteMove],
    previous_fen: str,
    threshold: int,
) -> list[DrillRouteMove]:
    if not suggestions:
        return suggestions
    candidate_ucis = [m.uci for m in suggestions]
    cache_entries = (
        db.query(AnalysisCache)
        .filter(
            AnalysisCache.fen_before == previous_fen,
            AnalysisCache.move_uci.in_(candidate_ucis),
        )
        .all()
    )
    is_white = active_color(previous_fen) == "white"
    delta_by_uci: dict[str, int] = {}
    for entry in cache_entries:
        delta = _resolve_eval_delta(entry, is_white)
        if delta is not None:
            delta_by_uci[entry.move_uci] = delta
    # Cache-miss defaults to delta=0 (pass). Matches played-move policy: opening book
    # positions are expected to be pre-analyzed, so unanalyzed suggestions are rare and
    # silently passing is safer than hiding a valid green arrow.
    filtered = [m for m in suggestions if delta_by_uci.get(m.uci, 0) <= threshold]
    return filtered if filtered else suggestions


class PlayerColor(str, Enum):
    WHITE = "white"
    BLACK = "black"


class DrillStrictness(str, Enum):
    LENIENT = "lenient"
    STANDARD = "standard"
    STRICT = "strict"


class DrillStartRequest(BaseModel):
    opening_key: str = Field(..., min_length=1)
    player_color: PlayerColor
    engine_elo: int
    strictness: DrillStrictness
    strictness_cp: int | None = Field(None, ge=0, le=50)


class DrillContinueRequest(BaseModel):
    current_ply: int = Field(..., ge=0)


class DrillRouteCheckRequest(BaseModel):
    current_fen: str = Field(..., min_length=1)
    previous_fen: str | None = Field(None, min_length=1)
    played_uci: str | None = Field(None, min_length=4, max_length=5)


class DrillRouteSuggestion(BaseModel):
    uci: str
    san: str
    resulting_fen: str
    plies_to_target: int


class DrillRouteFailure(BaseModel):
    played_move_uci: str | None = None
    played_move_san: str | None = None
    correction_fen: str
    reason: str | None = None


class DrillRouteCheckResponse(BaseModel):
    status: str
    current_fen: str
    target_fen: str
    suggestions: list[DrillRouteSuggestion]
    failure: DrillRouteFailure | None = None


class DrillSessionContract(BaseModel):
    session_id: uuid.UUID
    mode: str
    drill_state: str
    opening_key: str
    opening_name: str
    opening_family: str
    eco: str | None
    depth: int
    player_color: str
    engine_elo: int
    strictness: str
    strictness_cp: int | None = None
    is_rated: bool
    rated_start_ply: int | None
    normal_started_at: datetime | None
    converted_at: datetime | None
    terminal_reason: str | None = None


def _get_drill_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if session is None:
        raise HTTPException(status_code=404, detail="Drill session not found")
    return session


def _ensure_owner(session: GameSession, user: TokenPayload) -> None:
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this drill")


def _root_or_404(opening_key: str):
    root = get_opening_roots().get_root(opening_key)
    if root is None:
        raise HTTPException(status_code=404, detail="Unknown opening root")
    return root


def _suggestion(move: DrillRouteMove) -> DrillRouteSuggestion:
    return DrillRouteSuggestion(
        uci=move.uci,
        san=move.san,
        resulting_fen=move.resulting_fen,
        plies_to_target=move.plies_to_target,
    )


def _contract(session: GameSession) -> DrillSessionContract:
    if session.session_mode != DRILL_SESSION_MODE or not session.drill_opening_key:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    root = _root_or_404(session.drill_opening_key)
    return DrillSessionContract(
        session_id=session.id,
        mode=session.session_mode,
        drill_state=session.drill_state or "active",
        opening_key=root.opening_key,
        opening_name=root.opening_name,
        opening_family=root.opening_family,
        eco=root.eco,
        depth=root.depth,
        player_color=session.player_color,
        engine_elo=session.engine_elo,
        strictness=session.drill_strictness or "standard",
        strictness_cp=session.drill_strictness_cp,
        is_rated=session.is_rated,
        rated_start_ply=session.rated_start_ply,
        normal_started_at=session.normal_started_at,
        converted_at=session.converted_at,
        terminal_reason=session.drill_terminal_reason,
    )


@router.post("/start", response_model=DrillSessionContract, status_code=201)
def start_drill(
    request: DrillStartRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    _root_or_404(request.opening_key)
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user.user_id,
        started_at=utcnow(),
        status="active",
        engine_elo=request.engine_elo,
        blunder_recorded=False,
        is_rated=False,
        player_color=request.player_color.value,
        session_mode=DRILL_SESSION_MODE,
        drill_state="active",
        drill_opening_key=request.opening_key,
        drill_strictness=request.strictness.value,
        drill_strictness_cp=request.strictness_cp,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return _contract(session)


@router.get("/{session_id}", response_model=DrillSessionContract)
def get_drill(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    return _contract(session)


@router.post("/{session_id}/fail", response_model=DrillSessionContract)
def fail_drill(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.status != "active" or session.drill_state != "active":
        raise HTTPException(status_code=400, detail="Drill cannot be failed from its current state")
    session.drill_state = "failed"
    db.commit()
    db.refresh(session)
    return _contract(session)


@router.post("/{session_id}/continue", response_model=DrillSessionContract)
def continue_drill(
    session_id: uuid.UUID,
    request: DrillContinueRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state == "converted":
        if session.rated_start_ply == request.current_ply:
            return _contract(session)
        raise HTTPException(status_code=409, detail="Drill already converted with a different rated_start_ply")
    if session.drill_state not in ("root_reached", "failed"):
        raise HTTPException(status_code=400, detail="Drill must be at root or stopped before continuing")

    now = utcnow()
    session.drill_state = "converted"
    session.is_rated = True
    session.normal_started_at = now
    session.converted_at = now
    session.rated_start_ply = request.current_ply
    resegment_session_moves(db, session)
    db.commit()
    db.refresh(session)
    return _contract(session)


@router.post("/{session_id}/route-check", response_model=DrillRouteCheckResponse)
def check_drill_route(
    session_id: uuid.UUID,
    request: DrillRouteCheckRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillRouteCheckResponse:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE or not session.drill_opening_key:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.status != "active":
        raise HTTPException(status_code=400, detail="Drill session is not active")
    if session.drill_state in {"failed", "abandoned", "converted"}:
        raise HTTPException(status_code=400, detail="Drill route cannot be checked from its current state")
    if (request.previous_fen is None) != (request.played_uci is None):
        raise HTTPException(status_code=400, detail="previous_fen and played_uci must be provided together")

    graph = get_opening_graph()
    route_map = get_drill_route_map(graph, session.drill_opening_key)
    if not route_map.plies_by_fen:
        raise HTTPException(status_code=400, detail="Drill opening root is not in the opening graph")

    try:
        current_fen = normalize_fen(request.current_fen)
        previous_fen = normalize_fen(request.previous_fen) if request.previous_fen else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid FEN: {exc}") from exc

    if session.drill_state == "root_reached":
        return DrillRouteCheckResponse(
            status="root_reached",
            current_fen=current_fen,
            target_fen=route_map.target_fen,
            suggestions=[],
        )

    threshold = resolve_drill_threshold(session)
    if threshold is None:
        _logger.warning(
            "Drill session %s has no resolvable threshold (drill_strictness=%r, drill_strictness_cp=%r); accuracy check skipped",
            session.id,
            session.drill_strictness,
            session.drill_strictness_cp,
        )

    # Accuracy check before route check
    if previous_fen is not None and request.played_uci is not None and threshold is not None:
        is_white = active_color(previous_fen) == "white"
        cache_entry = (
            db.query(AnalysisCache)
            .filter(
                AnalysisCache.fen_before == previous_fen,
                AnalysisCache.move_uci == request.played_uci,
            )
            .first()
        )
        if cache_entry is not None:
            eval_delta = _resolve_eval_delta(cache_entry, is_white)
            if eval_delta is not None and eval_delta > threshold:
                suggestions_raw = route_preserving_moves(graph, route_map, previous_fen)
                suggestions_filtered = _filter_suggestions(db, suggestions_raw, previous_fen, threshold)
                session.drill_state = "failed"
                session.drill_terminal_reason = "accuracy"
                db.commit()
                return DrillRouteCheckResponse(
                    status="failed",
                    current_fen=current_fen,
                    target_fen=route_map.target_fen,
                    suggestions=[_suggestion(m) for m in suggestions_filtered],
                    failure=DrillRouteFailure(
                        reason="accuracy",
                        played_move_uci=request.played_uci,
                        played_move_san=safe_san_for_uci(previous_fen, request.played_uci),
                        correction_fen=previous_fen,
                    ),
                )

    if route_map.is_target(current_fen):
        session.drill_state = "root_reached"
        db.commit()
        return DrillRouteCheckResponse(
            status="root_reached",
            current_fen=current_fen,
            target_fen=route_map.target_fen,
            suggestions=[],
        )

    if route_map.is_on_route(current_fen):
        return DrillRouteCheckResponse(
            status="on_route",
            current_fen=current_fen,
            target_fen=route_map.target_fen,
            suggestions=[
                _suggestion(move)
                for move in route_preserving_moves(graph, route_map, current_fen)
            ],
        )

    if previous_fen is None or request.played_uci is None:
        raise HTTPException(
            status_code=400,
            detail="previous_fen and played_uci are required when the current position is off route",
        )

    played_move = route_move_for_uci(graph, route_map, previous_fen, request.played_uci)
    suggestions_raw = route_preserving_moves(graph, route_map, previous_fen)
    suggestions_filtered = (
        _filter_suggestions(db, suggestions_raw, previous_fen, threshold)
        if threshold is not None
        else suggestions_raw
    )
    session.drill_state = "failed"
    session.drill_terminal_reason = "off_route"
    db.commit()
    # reason="off_route" even if the move also exceeds the centipawn threshold —
    # leaving the route is the primary signal; staying on route is the first correction.
    return DrillRouteCheckResponse(
        status="failed",
        current_fen=current_fen,
        target_fen=route_map.target_fen,
        suggestions=[_suggestion(move) for move in suggestions_filtered],
        failure=DrillRouteFailure(
            reason="off_route",
            played_move_uci=request.played_uci,
            played_move_san=played_move.san if played_move is not None else safe_san_for_uci(previous_fen, request.played_uci),
            correction_fen=previous_fen,
        ),
    )


class DrillNaturalEndRequest(BaseModel):
    result: str
    pgn: str | None = None


@router.post("/{session_id}/natural-end", response_model=DrillSessionContract)
def natural_end_drill(
    session_id: uuid.UUID,
    request: DrillNaturalEndRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.drill_state not in ("active", "root_reached"):
        raise HTTPException(status_code=400, detail="Drill cannot end naturally from its current state")
    if request.result not in ("checkmate_win", "checkmate_loss", "draw"):
        raise HTTPException(status_code=400, detail="Invalid natural-end result")
    session.drill_state = "failed"
    session.drill_terminal_reason = "natural_end"
    session.status = "ended"
    session.result = request.result
    session.ended_at = utcnow()
    if request.pgn:
        session.pgn = request.pgn
    db.commit()
    db.refresh(session)
    return _contract(session)


@router.post("/{session_id}/abandon", response_model=DrillSessionContract)
def abandon_drill(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> DrillSessionContract:
    session = _get_drill_or_404(db, session_id)
    _ensure_owner(session, user)
    if session.session_mode != DRILL_SESSION_MODE:
        raise HTTPException(status_code=400, detail="Session is not a drill")
    if session.drill_state == "converted":
        raise HTTPException(status_code=400, detail="Use /api/game/end to abandon a converted drill")
    if session.drill_state not in {"active", "root_reached", "failed", "abandoned"}:
        raise HTTPException(status_code=400, detail="Drill cannot be abandoned from its current state")
    # Already-ended drills (e.g. natural-end finished the session) are no-ops:
    # don't overwrite session.result or ended_at.
    if session.drill_state != "abandoned" and session.status != "ended":
        session.drill_state = "abandoned"
        session.status = "ended"
        session.result = "drill_abandon"
        session.ended_at = utcnow()
        session.is_rated = False
        db.commit()
        db.refresh(session)
    return _contract(session)
