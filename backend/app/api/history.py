from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.accuracy import (
    AccuracyMove,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
)
from app.centipawn_loss import centipawn_loss_expr
from app.db import get_db
from app.models import GameSession, SessionMove
from app.opening_roots import deepest_opening_name, get_opening_roots
from app.security import TokenPayload, get_current_user
from app.session_contracts import visible_session_filter

router = APIRouter(prefix="/api/history", tags=["history"])


class GameSummary(BaseModel):
    total_moves: int
    blunders: int
    mistakes: int
    inaccuracies: int
    # None IFF no player move has an eval_delta. 0 means perfect play, not missing
    # data. A partially analyzed game reports the average over the plies that
    # resolved (no completeness gate — unlike accuracy).
    average_centipawn_loss: int | None
    accuracy: int | None = None


class HistoryGame(BaseModel):
    session_id: uuid.UUID
    started_at: datetime
    ended_at: datetime | None
    result: str | None
    engine_elo: int
    player_color: str
    opening_name: str | None = None
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
            visible_session_filter(),
        )
        .order_by(GameSession.ended_at.desc())
        .limit(limit)
        .all()
    )

    if not sessions:
        return HistoryResponse(games=[])

    session_ids = [s.id for s in sessions]

    player_loss_expr = case(
        (
            SessionMove.color == GameSession.player_color,
            centipawn_loss_expr(SessionMove.eval_delta),
        ),
        else_=None,
    )
    player_move_expr = SessionMove.color == GameSession.player_color
    stats_rows = (
        db.query(
            SessionMove.session_id,
            func.count().label("total_moves"),
            func.sum(
                case((player_move_expr & (SessionMove.classification == "blunder"), 1), else_=0)
            ).label("blunders"),
            func.sum(
                case((player_move_expr & (SessionMove.classification == "mistake"), 1), else_=0)
            ).label("mistakes"),
            func.sum(
                case((player_move_expr & (SessionMove.classification == "inaccuracy"), 1), else_=0)
            ).label("inaccuracies"),
            func.avg(player_loss_expr).label("avg_cpl"),
        )
        .join(GameSession, GameSession.id == SessionMove.session_id)
        .filter(SessionMove.session_id.in_(session_ids))
        .group_by(SessionMove.session_id)
        .all()
    )

    # Ordered per-move evals per session, for accuracy (counts come from the
    # GROUP BY above; accuracy needs the full ordered eval series).
    color_order = case((SessionMove.color == "white", 0), else_=1)
    move_rows = (
        db.query(
            SessionMove.session_id,
            SessionMove.color,
            SessionMove.eval_cp,
            SessionMove.eval_mate,
            SessionMove.fen_after,
        )
        .filter(SessionMove.session_id.in_(session_ids))
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    moves_by_session: dict[uuid.UUID, list[AccuracyMove]] = {}
    fens_by_session: dict[uuid.UUID, list[str | None]] = {}
    for row in move_rows:
        moves_by_session.setdefault(row.session_id, []).append(
            AccuracyMove(color=row.color, eval_cp=row.eval_cp, eval_mate=row.eval_mate)
        )
        fens_by_session.setdefault(row.session_id, []).append(row.fen_after)

    deepest_by_session: dict[uuid.UUID, str | None] = {}
    if fens_by_session:
        # Only cold-load the opening registry when there are moves to classify.
        roots = get_opening_roots()
        deepest_by_session = {
            sid: deepest_opening_name(fens, roots) for sid, fens in fens_by_session.items()
        }

    player_color_by_session = {s.id: s.player_color for s in sessions}
    expected_by_session = {s.id: expected_total_moves_from_pgn(s.pgn) for s in sessions}

    stats_by_session: dict[uuid.UUID, GameSummary] = {}
    for row in stats_rows:
        avg_cpl = int(round(row.avg_cpl)) if row.avg_cpl is not None else None
        accuracy = compute_game_accuracy(
            moves_by_session.get(row.session_id, []),
            player_color=player_color_by_session.get(row.session_id, "white"),
            expected_total_moves=expected_by_session.get(row.session_id),
        )
        stats_by_session[row.session_id] = GameSummary(
            total_moves=int(row.total_moves),
            blunders=int(row.blunders or 0),
            mistakes=int(row.mistakes or 0),
            inaccuracies=int(row.inaccuracies or 0),
            average_centipawn_loss=avg_cpl,
            accuracy=accuracy,
        )

    empty_summary = GameSummary(
        total_moves=0,
        blunders=0,
        mistakes=0,
        inaccuracies=0,
        average_centipawn_loss=None,
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
                opening_name=deepest_by_session.get(s.id),
                summary=stats_by_session.get(s.id, empty_summary),
            )
            for s in sessions
        ]
    )
