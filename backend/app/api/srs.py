from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Blunder, BlunderReview, GameSession
from app.security import TokenPayload, get_current_user
from app.srs_math import calculate_priority, expected_interval_hours

router = APIRouter(prefix="/api/srs", tags=["srs"])


class SrsReviewRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    blunder_id: int = Field(..., ge=1, description="Blunder target ID")
    passed: bool = Field(..., description="Whether the user passed the review")
    user_move: str = Field(..., min_length=1, max_length=10, description="Move the user played")
    eval_delta: int = Field(..., description="Centipawn loss from best move")


class SrsReviewResponse(BaseModel):
    blunder_id: int
    pass_streak: int
    priority: float
    next_expected_review: datetime


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not game_session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return game_session


def _ensure_session_owned_by_user(game_session: GameSession, user: TokenPayload) -> None:
    if game_session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


def _get_blunder_or_404(db: Session, *, blunder_id: int, user_id: int) -> Blunder:
    blunder = db.query(Blunder).filter(Blunder.id == blunder_id, Blunder.user_id == user_id).first()
    if not blunder:
        raise HTTPException(status_code=404, detail="Blunder not found")
    return blunder


@router.post("/review", response_model=SrsReviewResponse, status_code=200)
def review_blunder(
    request: SrsReviewRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SrsReviewResponse:
    game_session = _get_session_or_404(db, request.session_id)
    _ensure_session_owned_by_user(game_session, user)

    blunder = _get_blunder_or_404(
        db,
        blunder_id=request.blunder_id,
        user_id=user.user_id,
    )

    reviewed_at = datetime.now(timezone.utc)
    blunder.pass_streak = blunder.pass_streak + 1 if request.passed else 0
    blunder.last_reviewed_at = reviewed_at

    db.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=request.session_id,
            reviewed_at=reviewed_at,
            passed=request.passed,
            move_played_san=request.user_move,
            eval_delta_cp=request.eval_delta,
        )
    )
    db.commit()

    interval_hours = expected_interval_hours(blunder.pass_streak)
    next_expected_review = reviewed_at + timedelta(hours=interval_hours)

    return SrsReviewResponse(
        blunder_id=blunder.id,
        pass_streak=blunder.pass_streak,
        priority=calculate_priority(
            pass_streak=blunder.pass_streak,
            last_reviewed_at=blunder.last_reviewed_at,
            created_at=blunder.created_at,
            now=reviewed_at,
        ),
        next_expected_review=next_expected_review,
    )
