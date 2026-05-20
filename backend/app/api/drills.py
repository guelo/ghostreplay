from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.drill_steering import (
    DrillRouteMove,
    get_drill_route_map,
    route_move_for_uci,
    route_preserving_moves,
)
from app.fen import normalize_fen
from app.models import GameSession
from app.opening_graph import get_opening_graph
from app.opening_roots import get_opening_roots
from app.security import TokenPayload, get_current_user
from app.session_contracts import (
    DRILL_SESSION_MODE,
    resegment_session_moves,
    utcnow,
)

router = APIRouter(prefix="/api/drills", tags=["drills"])


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
    is_rated: bool
    rated_start_ply: int | None
    normal_started_at: datetime | None
    converted_at: datetime | None


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
        is_rated=session.is_rated,
        rated_start_ply=session.rated_start_ply,
        normal_started_at=session.normal_started_at,
        converted_at=session.converted_at,
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
    if session.drill_state != "root_reached":
        raise HTTPException(status_code=400, detail="Drill root must be reached before continuing")

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
    suggestions = route_preserving_moves(graph, route_map, previous_fen)
    session.drill_state = "failed"
    db.commit()
    return DrillRouteCheckResponse(
        status="failed",
        current_fen=current_fen,
        target_fen=route_map.target_fen,
        suggestions=[_suggestion(move) for move in suggestions],
        failure=DrillRouteFailure(
            played_move_uci=request.played_uci,
            played_move_san=played_move.san if played_move is not None else None,
            correction_fen=previous_fen,
        ),
    )


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
    if session.drill_state != "abandoned":
        session.drill_state = "abandoned"
        session.status = "ended"
        session.result = "drill_abandon"
        session.ended_at = utcnow()
        session.is_rated = False
        db.commit()
        db.refresh(session)
    return _contract(session)
