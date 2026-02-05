import uuid
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import fen_hash, active_color
from app.models import Blunder, GameSession, Position
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/game", tags=["game"])


class GameResult(str, Enum):
    """Possible game results."""
    CHECKMATE_WIN = "checkmate_win"
    CHECKMATE_LOSS = "checkmate_loss"
    RESIGN = "resign"
    DRAW = "draw"
    ABANDON = "abandon"


class PlayerColor(str, Enum):
    """Player color selection."""
    WHITE = "white"
    BLACK = "black"


class GameStartRequest(BaseModel):
    engine_elo: int = Field(..., description="Engine ELO rating")
    player_color: PlayerColor = Field(
        PlayerColor.WHITE,
        description="Player color (white|black)",
    )


class GameStartResponse(BaseModel):
    session_id: uuid.UUID
    engine_elo: int
    player_color: PlayerColor


class GameEndRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    result: GameResult = Field(..., description="Game result")
    pgn: str = Field(..., description="PGN of the game")


class GameEndResponse(BaseModel):
    session_id: uuid.UUID
    result: str
    ended_at: datetime


class GhostMoveMode(str, Enum):
    """Ghost move response mode."""
    GHOST = "ghost"
    ENGINE = "engine"


class GhostMoveResponse(BaseModel):
    mode: GhostMoveMode = Field(
        ...,
        description="ghost = steering toward blunder, engine = use local Stockfish",
    )
    move: str | None = Field(
        None,
        description="Next move SAN when mode=ghost, null when mode=engine",
    )
    target_blunder_id: int | None = Field(
        None,
        description="ID of the blunder being targeted (for debugging/display)",
    )


@router.post("/start", response_model=GameStartResponse, status_code=201)
def start_game(
    request: GameStartRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> GameStartResponse:
    """
    Create a new game session with the specified engine ELO.

    Returns the session_id to be used for subsequent game operations.
    """
    session = GameSession(
        id=uuid.uuid4(),
        user_id=user.user_id,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=request.engine_elo,
        blunder_recorded=False,
        player_color=request.player_color.value,
    )

    db.add(session)
    db.commit()
    db.refresh(session)

    return GameStartResponse(
        session_id=session.id,
        engine_elo=session.engine_elo,
        player_color=request.player_color,
    )


@router.post("/end", response_model=GameEndResponse)
def end_game(
    request: GameEndRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> GameEndResponse:
    """
    End a game session by setting its status to 'ended', recording the result,
    and setting the ended_at timestamp.

    Validates that the session exists, belongs to the user, and is currently active.
    """
    # Fetch the session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    # Verify ownership
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to end this game")

    # Verify session is active
    if session.status != "active":
        raise HTTPException(
            status_code=400,
            detail=f"Game session is already {session.status}"
        )

    # Update session
    session.status = "ended"
    session.result = request.result.value
    session.ended_at = datetime.now(timezone.utc)
    session.pgn = request.pgn

    db.commit()
    db.refresh(session)

    return GameEndResponse(
        session_id=session.id,
        result=session.result,
        ended_at=session.ended_at,
    )


@router.get("/ghost-move", response_model=GhostMoveResponse)
def get_ghost_move(
    session_id: uuid.UUID = Query(..., description="Game session ID"),
    fen: str = Query(..., description="Current board position FEN"),
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> GhostMoveResponse:
    """
    Look up a move that leads toward a position where the user previously blundered.

    Scoped to the session's player color. Returns null if no such move exists.
    """
    # Fetch and validate session
    session = db.query(GameSession).filter(GameSession.id == session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")

    # Ghost-move only works when it's the opponent's turn
    # (ghost suggests the opponent's move to steer towards a blunder position)
    position_color = active_color(fen)
    if position_color == session.player_color:
        # It's the user's turn, ghost doesn't suggest user moves
        return GhostMoveResponse(mode=GhostMoveMode.ENGINE, move=None, target_blunder_id=None)

    # Look up current position by FEN hash
    current_position = (
        db.query(Position)
        .filter(
            Position.user_id == user.user_id,
            Position.fen_hash == fen_hash(fen),
        )
        .first()
    )

    if not current_position:
        return GhostMoveResponse(mode=GhostMoveMode.ENGINE, move=None, target_blunder_id=None)

    # Recursive CTE to find blunders up to 15 moves downstream
    # Returns the first move in the path and the target blunder ID
    # Uses string-based path for cycle detection
    cte_query = text("""
        WITH RECURSIVE reachable(position_id, depth, path, first_move) AS (
            -- Base case: current position (depth 0, no first_move yet)
            SELECT
                CAST(:start_position_id AS BIGINT),
                0,
                ',' || :start_position_id || ',',
                CAST(NULL AS TEXT)

            UNION ALL

            -- Recursive case: follow moves up to depth 15
            SELECT
                m.to_position_id,
                r.depth + 1,
                r.path || m.to_position_id || ',',
                COALESCE(r.first_move, m.move_san)
            FROM reachable r
            JOIN moves m ON m.from_position_id = r.position_id
            WHERE r.depth < 15
              AND r.path NOT LIKE '%,' || CAST(m.to_position_id AS TEXT) || ',%'
        )
        SELECT r.first_move, b.id AS blunder_id
        FROM reachable r
        JOIN positions p ON p.id = r.position_id
        JOIN blunders b ON b.position_id = r.position_id
        WHERE b.user_id = :user_id
          AND p.active_color = :player_color
          AND r.first_move IS NOT NULL
        LIMIT 1
    """)

    result = db.execute(
        cte_query,
        {
            "start_position_id": current_position.id,
            "user_id": user.user_id,
            "player_color": session.player_color,
        },
    ).fetchone()

    if not result:
        return GhostMoveResponse(mode=GhostMoveMode.ENGINE, move=None, target_blunder_id=None)

    return GhostMoveResponse(
        mode=GhostMoveMode.GHOST,
        move=result[0],
        target_blunder_id=result[1],
    )
