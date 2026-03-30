from __future__ import annotations

import logging
import uuid
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.db import get_db
from app.opening_cache import recompute_opening_scores_if_needed
from app.models import AnalysisCache, GameSession, SessionMove
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/session", tags=["session"])
logger = logging.getLogger(__name__)


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
    fen_before: str | None = Field(None, min_length=1)
    move_uci: str | None = Field(None, min_length=2, max_length=5)
    best_move_uci: str | None = Field(None, max_length=5)


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


class PositionAnalysis(BaseModel):
    best_move_uci: str
    best_move_san: str | None = None
    best_move_eval_cp: int | None = None  # side-to-move-relative


class SessionAnalysisResponse(BaseModel):
    session_id: uuid.UUID
    pgn: str | None
    result: str | None
    moves: list[SessionAnalysisMove]
    summary: SessionAnalysisSummary
    position_analysis: dict[str, PositionAnalysis] = {}
    expected_total_moves: int | None = None
    analyzed_moves: int = 0
    is_complete: bool = False


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


def _refresh_opening_scores_best_effort(db: Session, user_id: int, player_color: str) -> None:
    try:
        recompute_opening_scores_if_needed(db, user_id, player_color)
    except Exception:
        logger.exception(
            "opening score cache refresh failed after session upload",
            extra={"user_id": user_id, "player_color": player_color},
        )


def _upsert_analysis_cache(
    db: Session,
    moves: list[SessionMoveInput],
) -> None:
    """Upsert analysis results into the global cache for moves that include
    the new fen_before/move_uci fields.  Evals are converted from
    player-relative (as uploaded) to white-relative for storage."""
    cache_values = []
    for move in moves:
        if not move.fen_before or not move.move_uci:
            continue
        if move.eval_cp is None and move.best_move_eval_cp is None:
            continue

        is_black = move.color == MoveColor.BLACK
        sign = -1 if is_black else 1
        played_eval = move.eval_cp * sign if move.eval_cp is not None else None
        best_eval = move.best_move_eval_cp * sign if move.best_move_eval_cp is not None else None
        eval_delta = move.eval_delta  # already unsigned (best - played >= 0)

        cache_values.append({
            "fen_before": move.fen_before,
            "move_uci": move.move_uci,
            "move_san": move.move_san,
            "best_move_uci": move.best_move_uci,
            "best_move_san": move.best_move_san,
            "played_eval": played_eval,
            "best_eval": best_eval,
            "eval_delta": eval_delta,
            "classification": move.classification.value if move.classification else None,
            "source": "game",
        })

    if not cache_values:
        return

    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        stmt = sqlite_insert(AnalysisCache).values(cache_values)
    elif dialect_name == "postgresql":
        stmt = postgresql_insert(AnalysisCache).values(cache_values)
    else:
        for val in cache_values:
            existing = db.query(AnalysisCache).filter(
                AnalysisCache.fen_before == val["fen_before"],
                AnalysisCache.move_uci == val["move_uci"],
            ).first()
            if existing:
                for k, v in val.items():
                    if k not in ("fen_before", "move_uci"):
                        setattr(existing, k, v)
            else:
                db.add(AnalysisCache(**val))
        db.commit()
        return

    stmt = stmt.on_conflict_do_update(
        index_elements=[AnalysisCache.fen_before, AnalysisCache.move_uci],
        set_={
            "move_san": stmt.excluded.move_san,
            "best_move_uci": stmt.excluded.best_move_uci,
            "best_move_san": stmt.excluded.best_move_san,
            "played_eval": stmt.excluded.played_eval,
            "best_eval": stmt.excluded.best_eval,
            "eval_delta": stmt.excluded.eval_delta,
            "classification": stmt.excluded.classification,
            "source": stmt.excluded.source,
        },
    )
    db.execute(stmt)
    db.commit()


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
            "fen_before": move.fen_before,
            "best_move_uci": move.best_move_uci,
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
                existing_row.fen_before = value["fen_before"]
                existing_row.best_move_uci = value["best_move_uci"]
            else:
                db.add(SessionMove(**value))

        db.commit()
        _upsert_analysis_cache(db, request.moves)
        _refresh_opening_scores_best_effort(db, user.user_id, game_session.player_color)
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
            "fen_before": statement.excluded.fen_before,
            "best_move_uci": statement.excluded.best_move_uci,
        },
    )
    db.execute(statement)
    db.commit()

    _upsert_analysis_cache(db, request.moves)
    _refresh_opening_scores_best_effort(db, user.user_id, game_session.player_color)

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

    position_analysis: dict[str, PositionAnalysis] = {}
    for move in session_moves:
        if move.fen_before and move.best_move_uci and move.fen_before not in position_analysis:
            position_analysis[move.fen_before] = PositionAnalysis(
                best_move_uci=move.best_move_uci,
                best_move_san=move.best_move_san,
                best_move_eval_cp=move.best_move_eval_cp,
            )

    # Completion metadata: derive expected_total_moves from stored PGN
    expected_total_moves: int | None = None
    if game_session.pgn:
        try:
            import chess.pgn
            import io
            pgn_game = chess.pgn.read_game(io.StringIO(game_session.pgn))
            if pgn_game is not None:
                expected_total_moves = sum(1 for _ in pgn_game.mainline_moves())
        except Exception:
            pass

    analyzed_moves = len(session_moves)
    is_complete = (
        expected_total_moves is not None
        and analyzed_moves >= expected_total_moves
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
        position_analysis=position_analysis,
        expected_total_moves=expected_total_moves,
        analyzed_moves=analyzed_moves,
        is_complete=is_complete,
    )
