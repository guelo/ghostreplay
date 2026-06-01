from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import GameSession, SessionMove

SessionMode = Literal["normal", "drill"]
DrillState = Literal["active", "root_reached", "failed", "abandoned", "converted"]
MoveSegment = Literal["normal", "drill"]

NORMAL_SESSION_MODE = "normal"
DRILL_SESSION_MODE = "drill"
VISIBLE_DRILL_STATE = "converted"
NORMAL_MOVE_SEGMENT = "normal"
DRILL_MOVE_SEGMENT = "drill"
DRILL_STRICTNESS_TIER_THRESHOLDS: dict[str, int] = {
    "strict": 15,
    "standard": 35,
    "lenient": 50,
}


def visible_session_filter():
    return or_(
        GameSession.session_mode == NORMAL_SESSION_MODE,
        GameSession.drill_state == VISIBLE_DRILL_STATE,
    )


def is_visible_game_session(session: GameSession) -> bool:
    return session.session_mode == NORMAL_SESSION_MODE or session.drill_state == VISIBLE_DRILL_STATE


def resolve_drill_threshold(session: GameSession) -> int | None:
    if session.drill_strictness_cp is not None:
        return session.drill_strictness_cp
    return DRILL_STRICTNESS_TIER_THRESHOLDS.get(session.drill_strictness or "standard")


def normal_play_started_at(session: GameSession) -> datetime:
    # Amended drill policy (2026-06-01): a converted drill is one full normal
    # game whose timeline anchors to the drill's actual start, not conversion
    # time. Pre-continue moves count, so recency/window/duration use started_at.
    return session.started_at


def normal_play_started_at_expr():
    return GameSession.started_at


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
