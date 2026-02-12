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


def find_ghost_move(
    db: Session,
    user_id: int,
    fen: str,
    player_color: str,
) -> tuple[str | None, int | None]:
    """
    Find a move that steers toward a position where the user previously blundered.

    Uses recursive path traversal to search up to 15 moves downstream for reachable
    blunders that match the player's color.

    Args:
        db: Database session
        user_id: User ID to scope blunder lookup
        fen: Current board position FEN
        player_color: Player color from game session ('white' or 'black')

    Returns:
        Tuple of (move_san, target_blunder_id) if ghost path exists, else (None, None)
    """
    # Look up current position by FEN hash
    current_position = (
        db.query(Position)
        .filter(
            Position.user_id == user_id,
            Position.fen_hash == fen_hash(fen),
        )
        .first()
    )

    if not current_position:
        return (None, None)

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
            "user_id": user_id,
            "player_color": player_color,
        },
    ).fetchone()

    if not result:
        return (None, None)

    return (result[0], result[1])


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


class MoveDetails(BaseModel):
    """Move representation with both UCI and SAN formats."""
    uci: str = Field(..., description="Move in UCI notation (e.g., 'e2e4')")
    san: str = Field(..., description="Move in SAN notation (e.g., 'e4')")


class DecisionSource(str, Enum):
    """Source of the opponent move decision."""
    GHOST_PATH = "ghost_path"
    BACKEND_ENGINE = "backend_engine"


class OpponentMoveMode(str, Enum):
    """Opponent move response mode."""
    GHOST = "ghost"
    ENGINE = "engine"


class NextOpponentMoveRequest(BaseModel):
    """Request for next opponent move."""
    session_id: uuid.UUID = Field(..., description="Game session ID")
    fen: str = Field(..., description="Current board position FEN")


class NextOpponentMoveResponse(BaseModel):
    """Response for next opponent move (unified ghost + engine endpoint)."""
    mode: OpponentMoveMode = Field(
        ...,
        description="ghost = steering toward blunder, engine = backend inference",
    )
    move: MoveDetails = Field(
        ...,
        description="Next opponent move in both UCI and SAN formats",
    )
    target_blunder_id: int | None = Field(
        None,
        description="ID of the blunder being targeted (ghost mode only)",
    )
    decision_source: DecisionSource = Field(
        ...,
        description="Backend decision branch used to produce the move",
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

    # Use shared ghost path traversal logic
    move_san, target_blunder_id = find_ghost_move(
        db=db,
        user_id=user.user_id,
        fen=fen,
        player_color=session.player_color,
    )

    if move_san is None:
        return GhostMoveResponse(mode=GhostMoveMode.ENGINE, move=None, target_blunder_id=None)

    return GhostMoveResponse(
        mode=GhostMoveMode.GHOST,
        move=move_san,
        target_blunder_id=target_blunder_id,
    )


@router.post("/next-opponent-move", response_model=NextOpponentMoveResponse)
def get_next_opponent_move(
    request: NextOpponentMoveRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> NextOpponentMoveResponse:
    """
    Get the next opponent move using unified ghost-first pipeline with backend engine fallback.

    This endpoint replaces the split orchestration (ghost endpoint + local engine fallback)
    with a single decision-maker that returns exactly one opponent move.

    Flow:
    1. Validate session ownership and FEN input
    2. Attempt ghost-path traversal (look for due blunders within steering radius)
    3. If ghost path exists, return ghost move
    4. Otherwise, fall back to backend engine inference (Maia)
    """
    # Fetch and validate session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")

    # Validate FEN and check it's the opponent's turn
    try:
        position_color = active_color(request.fen)
    except (IndexError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid FEN: {e}")

    if position_color == session.player_color:
        raise HTTPException(
            status_code=400,
            detail="Cannot get opponent move when it's the player's turn",
        )

    # Step 1: Ghost-first path traversal
    # Use shared ghost path traversal logic to find moves toward due blunders
    move_san, target_blunder_id = find_ghost_move(
        db=db,
        user_id=user.user_id,
        fen=request.fen,
        player_color=session.player_color,
    )

    # If ghost path exists, convert SAN to both UCI and SAN formats
    if move_san is not None:
        import chess
        try:
            board = chess.Board(request.fen)
            # Parse SAN to get the move object
            move = board.parse_san(move_san)

            return NextOpponentMoveResponse(
                mode=OpponentMoveMode.GHOST,
                move=MoveDetails(
                    uci=move.uci(),
                    san=move_san,
                ),
                target_blunder_id=target_blunder_id,
                decision_source=DecisionSource.GHOST_PATH,
            )
        except (ValueError, chess.IllegalMoveError, chess.InvalidMoveError) as e:
            # If SAN parsing fails, log and fall through to engine fallback
            # This should not happen in normal operation but provides resilience
            import logging
            logging.warning(
                f"Failed to parse ghost SAN move '{move_san}' for FEN '{request.fen}': {e}"
            )

    # Step 2: Backend engine fallback
    # Use Maia-2 inference to generate opponent move at session's configured Elo
    try:
        from app.maia_engine import MaiaEngineService, MaiaEngineUnavailableError

        # Get best move from Maia at session's configured Elo
        maia_move = MaiaEngineService.get_best_move(
            fen=request.fen,
            elo=session.engine_elo,
        )

        return NextOpponentMoveResponse(
            mode=OpponentMoveMode.ENGINE,
            move=MoveDetails(
                uci=maia_move.uci,
                san=maia_move.san,
            ),
            target_blunder_id=None,
            decision_source=DecisionSource.BACKEND_ENGINE,
        )

    except MaiaEngineUnavailableError as e:
        # Model cannot be loaded or initialized - return 503 Service Unavailable
        raise HTTPException(
            status_code=503,
            detail=f"Maia engine unavailable: {e}",
        )
    except ValueError as e:
        # Invalid FEN or Elo range
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}")
