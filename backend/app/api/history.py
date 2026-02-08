from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import GameSession, SessionMove
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/history", tags=["history"])


class GameSummary(BaseModel):
    total_moves: int
    blunders: int
    mistakes: int
    inaccuracies: int
    average_centipawn_loss: int


class HistoryGame(BaseModel):
    session_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    result: str | None
    engine_elo: int
    player_color: str
    summary: GameSummary


class HistoryResponse(BaseModel):
    games: list[HistoryGame]


@router.get("", response_model=HistoryResponse)
def get_history(
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> HistoryResponse:
    sessions = (
        db.query(GameSession)
        .filter(
            GameSession.user_id == user.user_id,
            GameSession.status == "ended",
        )
        .order_by(GameSession.ended_at.desc())
        .limit(limit)
        .all()
    )

    if not sessions:
        return HistoryResponse(games=[])

    session_ids = [s.id for s in sessions]

    stats_rows = (
        db.query(
            SessionMove.session_id,
            func.count().label("total_moves"),
            func.sum(case((SessionMove.classification == "blunder", 1), else_=0)).label("blunders"),
            func.sum(case((SessionMove.classification == "mistake", 1), else_=0)).label("mistakes"),
            func.sum(case((SessionMove.classification == "inaccuracy", 1), else_=0)).label("inaccuracies"),
            func.avg(SessionMove.eval_delta).label("avg_cpl"),
        )
        .filter(SessionMove.session_id.in_(session_ids))
        .group_by(SessionMove.session_id)
        .all()
    )

    stats_by_session: dict[uuid.UUID, GameSummary] = {}
    for row in stats_rows:
        avg_cpl = int(round(row.avg_cpl)) if row.avg_cpl is not None else 0
        stats_by_session[row.session_id] = GameSummary(
            total_moves=int(row.total_moves),
            blunders=int(row.blunders or 0),
            mistakes=int(row.mistakes or 0),
            inaccuracies=int(row.inaccuracies or 0),
            average_centipawn_loss=avg_cpl,
        )

    empty_summary = GameSummary(
        total_moves=0,
        blunders=0,
        mistakes=0,
        inaccuracies=0,
        average_centipawn_loss=0,
    )

    return HistoryResponse(
        games=[
            HistoryGame(
                session_id=s.id,
                started_at=s.started_at,
                ended_at=s.ended_at,
                result=s.result,
                engine_elo=s.engine_elo,
                player_color=s.player_color,
                summary=stats_by_session.get(s.id, empty_summary),
            )
            for s in sessions
        ]
    )
