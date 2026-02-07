from __future__ import annotations

import uuid
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import GameSession, SessionMove
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/session", tags=["session"])


class MoveColor(str, Enum):
    WHITE = "white"
    BLACK = "black"


class MoveClassification(str, Enum):
    BEST = "best"
    EXCELLENT = "excellent"
    GOOD = "good"
    INACCURACY = "inaccuracy"
    MISTAKE = "mistake"
    BLUNDER = "blunder"


class SessionMoveInput(BaseModel):
    move_number: int = Field(..., ge=1)
    color: MoveColor
    move_san: str = Field(..., min_length=1, max_length=10)
    fen_after: str = Field(..., min_length=1)
    eval_cp: int | None = None
    eval_mate: int | None = None
    best_move_san: str | None = Field(None, max_length=10)
    best_move_eval_cp: int | None = None
    eval_delta: int | None = None
    classification: MoveClassification | None = None


class SessionMovesRequest(BaseModel):
    moves: list[SessionMoveInput] = Field(default_factory=list)


class SessionMovesResponse(BaseModel):
    moves_inserted: int


class SessionAnalysisMove(BaseModel):
    move_number: int
    color: MoveColor
    move_san: str
    fen_after: str
    eval_cp: int | None = None
    eval_mate: int | None = None
    best_move_san: str | None = None
    best_move_eval_cp: int | None = None
    eval_delta: int | None = None
    classification: MoveClassification | None = None


class SessionAnalysisSummary(BaseModel):
    blunders: int
    mistakes: int
    inaccuracies: int
    average_centipawn_loss: int


class SessionAnalysisResponse(BaseModel):
    session_id: uuid.UUID
    pgn: str | None
    result: str | None
    moves: list[SessionAnalysisMove]
    summary: SessionAnalysisSummary


def _get_session_or_404(db: Session, session_id: uuid.UUID) -> GameSession:
    game_session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not game_session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return game_session


def _ensure_session_owned_by_user(game_session: GameSession, user: TokenPayload) -> None:
    if game_session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


def _validate_unique_move_keys(moves: list[SessionMoveInput]) -> None:
    seen: set[tuple[int, str]] = set()
    for move in moves:
        key = (move.move_number, move.color.value)
        if key in seen:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Duplicate move entry in payload for "
                    f"move_number={move.move_number}, color={move.color.value}"
                ),
            )
        seen.add(key)


@router.post("/{session_id}/moves", response_model=SessionMovesResponse)
def upsert_session_moves(
    session_id: uuid.UUID,
    request: SessionMovesRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionMovesResponse:
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)
    _validate_unique_move_keys(request.moves)

    if not request.moves:
        return SessionMovesResponse(moves_inserted=0)

    values = [
        {
            "session_id": session_id,
            "move_number": move.move_number,
            "color": move.color.value,
            "move_san": move.move_san,
            "fen_after": move.fen_after,
            "eval_cp": move.eval_cp,
            "eval_mate": move.eval_mate,
            "best_move_san": move.best_move_san,
            "best_move_eval_cp": move.best_move_eval_cp,
            "eval_delta": move.eval_delta,
            "classification": move.classification.value if move.classification else None,
        }
        for move in request.moves
    ]

    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        statement = sqlite_insert(SessionMove).values(values)
    elif dialect_name == "postgresql":
        statement = postgresql_insert(SessionMove).values(values)
    else:
        for value in values:
            existing_row = db.query(SessionMove).filter(
                SessionMove.session_id == value["session_id"],
                SessionMove.move_number == value["move_number"],
                SessionMove.color == value["color"],
            ).first()
            if existing_row:
                existing_row.move_san = value["move_san"]
                existing_row.fen_after = value["fen_after"]
                existing_row.eval_cp = value["eval_cp"]
                existing_row.eval_mate = value["eval_mate"]
                existing_row.best_move_san = value["best_move_san"]
                existing_row.best_move_eval_cp = value["best_move_eval_cp"]
                existing_row.eval_delta = value["eval_delta"]
                existing_row.classification = value["classification"]
            else:
                db.add(SessionMove(**value))

        db.commit()
        return SessionMovesResponse(moves_inserted=len(values))

    statement = statement.on_conflict_do_update(
        index_elements=[
            SessionMove.session_id,
            SessionMove.move_number,
            SessionMove.color,
        ],
        set_={
            "move_san": statement.excluded.move_san,
            "fen_after": statement.excluded.fen_after,
            "eval_cp": statement.excluded.eval_cp,
            "eval_mate": statement.excluded.eval_mate,
            "best_move_san": statement.excluded.best_move_san,
            "best_move_eval_cp": statement.excluded.best_move_eval_cp,
            "eval_delta": statement.excluded.eval_delta,
            "classification": statement.excluded.classification,
        },
    )
    db.execute(statement)
    db.commit()

    return SessionMovesResponse(moves_inserted=len(values))


@router.get("/{session_id}/analysis", response_model=SessionAnalysisResponse)
def get_session_analysis(
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> SessionAnalysisResponse:
    game_session = _get_session_or_404(db, session_id)
    _ensure_session_owned_by_user(game_session, user)

    color_order = case((SessionMove.color == MoveColor.WHITE.value, 0), else_=1)
    session_moves = (
        db.query(SessionMove)
        .filter(SessionMove.session_id == session_id)
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )

    summary_row = (
        db.query(
            func.sum(case((SessionMove.classification == MoveClassification.BLUNDER.value, 1), else_=0)).label(
                "blunders"
            ),
            func.sum(case((SessionMove.classification == MoveClassification.MISTAKE.value, 1), else_=0)).label(
                "mistakes"
            ),
            func.sum(
                case((SessionMove.classification == MoveClassification.INACCURACY.value, 1), else_=0)
            ).label("inaccuracies"),
            func.avg(SessionMove.eval_delta).label("average_centipawn_loss"),
        )
        .filter(SessionMove.session_id == session_id)
        .one()
    )

    average_centipawn_loss = (
        int(round(summary_row.average_centipawn_loss))
        if summary_row.average_centipawn_loss is not None
        else 0
    )

    return SessionAnalysisResponse(
        session_id=game_session.id,
        pgn=game_session.pgn,
        result=game_session.result,
        moves=[
            SessionAnalysisMove(
                move_number=move.move_number,
                color=move.color,
                move_san=move.move_san,
                fen_after=move.fen_after,
                eval_cp=move.eval_cp,
                eval_mate=move.eval_mate,
                best_move_san=move.best_move_san,
                best_move_eval_cp=move.best_move_eval_cp,
                eval_delta=move.eval_delta,
                classification=move.classification,
            )
            for move in session_moves
        ],
        summary=SessionAnalysisSummary(
            blunders=int(summary_row.blunders or 0),
            mistakes=int(summary_row.mistakes or 0),
            inaccuracies=int(summary_row.inaccuracies or 0),
            average_centipawn_loss=average_centipawn_loss,
        ),
    )
