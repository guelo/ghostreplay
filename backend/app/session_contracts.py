from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.models import GameSession, SessionMove

SessionMode = Literal["normal", "drill"]
DrillState = Literal["active", "failed", "abandoned", "converted"]
MoveSegment = Literal["normal", "drill"]

NORMAL_SESSION_MODE = "normal"
DRILL_SESSION_MODE = "drill"
VISIBLE_DRILL_STATE = "converted"
NORMAL_MOVE_SEGMENT = "normal"
DRILL_MOVE_SEGMENT = "drill"


def visible_session_filter():
    return or_(
        GameSession.session_mode == NORMAL_SESSION_MODE,
        GameSession.drill_state == VISIBLE_DRILL_STATE,
    )


def normal_segment_filter():
    return SessionMove.segment == NORMAL_MOVE_SEGMENT


def is_visible_game_session(session: GameSession) -> bool:
    return session.session_mode == NORMAL_SESSION_MODE or session.drill_state == VISIBLE_DRILL_STATE


def normal_play_started_at(session: GameSession) -> datetime:
    if session.session_mode == DRILL_SESSION_MODE and session.drill_state == VISIBLE_DRILL_STATE:
        return session.normal_started_at or session.started_at
    return session.started_at


def normal_play_started_at_expr():
    return case(
        (
            GameSession.drill_state == VISIBLE_DRILL_STATE,
            func.coalesce(GameSession.normal_started_at, GameSession.started_at),
        ),
        else_=GameSession.started_at,
    )


def ply_after(move_number: int, color: str) -> int:
    return (move_number - 1) * 2 + (1 if color == "white" else 2)


def segment_for_move(session: GameSession, move_number: int, color: str) -> MoveSegment:
    if session.session_mode == NORMAL_SESSION_MODE:
        return NORMAL_MOVE_SEGMENT
    if session.drill_state != VISIBLE_DRILL_STATE:
        return DRILL_MOVE_SEGMENT
    rated_start_ply = session.rated_start_ply
    if rated_start_ply is None:
        return DRILL_MOVE_SEGMENT
    return DRILL_MOVE_SEGMENT if ply_after(move_number, color) <= rated_start_ply else NORMAL_MOVE_SEGMENT


def resegment_session_moves(db: Session, session: GameSession) -> None:
    rows = db.query(SessionMove).filter(SessionMove.session_id == session.id).all()
    for row in rows:
        row.segment = segment_for_move(session, row.move_number, row.color)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
