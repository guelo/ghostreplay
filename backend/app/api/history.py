from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.accuracy import accuracy_for_sessions
from app.centipawn_loss import centipawn_loss_expr, round_half_up_cpl
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

    # Per-move FENs per session, for opening-name derivation only (counts come from
    # the GROUP BY above; accuracy comes from the cache; see
    # docs/session-accuracy-versioning.md).
    #
    # move_number and color have LEFT the select list but MUST stay in the ORDER BY:
    # deepest_opening_name walks the fens in play order, so dropping the ordering
    # would silently derive a different opening rather than fail. Ordering on
    # non-selected columns is legal in SQLite and Postgres for this non-DISTINCT
    # query.
    color_order = case((SessionMove.color == "white", 0), else_=1)
    move_rows = (
        db.query(SessionMove.session_id, SessionMove.fen_after)
        .filter(SessionMove.session_id.in_(session_ids))
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    fens_by_session: dict[uuid.UUID, list[str | None]] = {}
    for row in move_rows:
        fens_by_session.setdefault(row.session_id, []).append(row.fen_after)

    deepest_by_session: dict[uuid.UUID, str | None] = {}
    if fens_by_session:
        # Only cold-load the opening registry when there are moves to classify.
        roots = get_opening_roots()
        deepest_by_session = {
            sid: deepest_opening_name(fens, roots) for sid, fens in fens_by_session.items()
        }

    # Keyed on ``sessions`` — the rows this response RETURNS — not on the grouped
    # move rows, so an ended-visible session with zero session_moves rows (absent
    # from the GROUP BY above) still gets its own cached accuracy.
    accuracy_by_session = accuracy_for_sessions(db, sessions)

    stats_by_session: dict[uuid.UUID, GameSummary] = {}
    for row in stats_rows:
        avg_cpl = round_half_up_cpl(row.avg_cpl) if row.avg_cpl is not None else None
        stats_by_session[row.session_id] = GameSummary(
            total_moves=int(row.total_moves),
            blunders=int(row.blunders or 0),
            mistakes=int(row.mistakes or 0),
            inaccuracies=int(row.inaccuracies or 0),
            average_centipawn_loss=avg_cpl,
            accuracy=accuracy_by_session.get(row.session_id),
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
                # The zero-move summary is built PER SESSION rather than shared: a
                # single hoisted instance cannot carry a per-session accuracy, and a
                # map wrongly scoped to the grouped move rows would go unnoticed.
                # Release A's hook stamps such a session (an eligible session with an
                # empty row set stamps a computed None), so this reads NULL today —
                # but it must READ, not hard-code None.
                summary=stats_by_session.get(
                    s.id,
                    GameSummary(
                        total_moves=0,
                        blunders=0,
                        mistakes=0,
                        inaccuracies=0,
                        average_centipawn_loss=None,
                        accuracy=accuracy_by_session.get(s.id),
                    ),
                ),
            )
            for s in sessions
        ]
    )
