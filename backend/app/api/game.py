import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import GameSession

router = APIRouter(prefix="/api/game", tags=["game"])


# TODO: Replace with proper JWT auth once auth system is implemented
def get_current_user_id(x_user_id: str = Header(...)) -> int:
    """Temporary auth placeholder. Extract user_id from X-User-Id header."""
    try:
        return int(x_user_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid user ID")


class GameStartRequest(BaseModel):
    engine_elo: int = Field(..., description="Engine ELO rating")


class GameStartResponse(BaseModel):
    session_id: uuid.UUID
    engine_elo: int


@router.post("/start", response_model=GameStartResponse, status_code=201)
def start_game(
    request: GameStartRequest,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id),
) -> GameStartResponse:
    """
    Create a new game session with the specified engine ELO.

    Returns the session_id to be used for subsequent game operations.
    """
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=request.engine_elo,
        blunder_recorded=False,
    )

    db.add(session)
    db.commit()
    db.refresh(session)

    return GameStartResponse(
        session_id=session.id,
        engine_elo=session.engine_elo,
    )
