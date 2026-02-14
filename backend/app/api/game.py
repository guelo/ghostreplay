import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import fen_hash, active_color
from app.models import GameSession, Position
from app.security import TokenPayload, get_current_user
from app.srs_math import calculate_priority

router = APIRouter(prefix="/api/game", tags=["game"])

STEERING_RADIUS = 5
SEVERITY_NORMALIZER_CP = 50.0
DISTANCE_WEIGHT_SLOPE = 0.1


@dataclass(frozen=True)
class GhostMoveCandidate:
    first_move: str
    blunder_id: int
    depth: int
    eval_loss_cp: int
    pass_streak: int
    last_reviewed_at: datetime | None
    created_at: datetime | None

    def score(self, now: datetime) -> float:
        priority = calculate_priority(
            pass_streak=self.pass_streak,
            last_reviewed_at=self.last_reviewed_at,
            created_at=self.created_at,
            now=now,
        )
        severity_weight = max(float(self.eval_loss_cp), 0.0) / SEVERITY_NORMALIZER_CP
        distance_weight = 1.0 / (1.0 + DISTANCE_WEIGHT_SLOPE * self.depth)
        return priority * severity_weight * distance_weight


def find_ghost_move(
    db: Session,
    user_id: int,
    fen: str,
    player_color: str,
) -> tuple[str | None, int | None]:
    """
    Find a move that steers toward a position where the user previously blundered.

    Uses recursive path traversal to search up to 5 moves downstream for reachable
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

    # Recursive CTE to find candidate blunders up to the steering radius.
    # Returns the first move in each path and candidate metadata for scoring.
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

            -- Recursive case: follow moves up to configured steering radius
            SELECT
                m.to_position_id,
                r.depth + 1,
                r.path || m.to_position_id || ',',
                COALESCE(r.first_move, m.move_san)
            FROM reachable r
            JOIN moves m ON m.from_position_id = r.position_id
            WHERE r.depth < :steering_radius
              AND r.path NOT LIKE '%,' || CAST(m.to_position_id AS TEXT) || ',%'
        )
        SELECT
            r.first_move,
            b.id AS blunder_id,
            r.depth,
            b.eval_loss_cp,
            b.pass_streak,
            b.last_reviewed_at,
            b.created_at
        FROM reachable r
        JOIN positions p ON p.id = r.position_id
        JOIN blunders b ON b.position_id = r.position_id
        WHERE b.user_id = :user_id
          AND p.active_color = :player_color
          AND r.first_move IS NOT NULL
    """)

    candidate_rows = db.execute(
        cte_query,
        {
            "start_position_id": current_position.id,
            "user_id": user_id,
            "player_color": player_color,
            "steering_radius": STEERING_RADIUS,
        },
    ).fetchall()

    if not candidate_rows:
        return (None, None)

    now = datetime.now(timezone.utc)
    best_candidate: GhostMoveCandidate | None = None
    best_key: tuple[float, float, int, int, str] | None = None

    for row in candidate_rows:
        candidate = GhostMoveCandidate(
            first_move=row[0],
            blunder_id=row[1],
            depth=row[2],
            eval_loss_cp=row[3],
            pass_streak=row[4],
            last_reviewed_at=row[5],
            created_at=row[6],
        )
        score = candidate.score(now)

        # Tie-breakers keep behavior deterministic in tests and production.
        rank_key = (
            score,
            -float(candidate.depth),
            candidate.eval_loss_cp,
            -candidate.blunder_id,
            candidate.first_move,
        )
        if best_key is None or rank_key > best_key:
            best_key = rank_key
            best_candidate = candidate

    if best_candidate is None:
        return (None, None)

    return (best_candidate.first_move, best_candidate.blunder_id)


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
    moves: list[str] = Field(default_factory=list, description="UCI move history from game start")


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

    # Step 2: Backend engine fallback — remote Maia3 API
    try:
        from app.maia3_client import Maia3Error
        from app.opponent_move_controller import choose_move

        controller_move = choose_move(
            fen=request.fen,
            target_elo=session.engine_elo,
            moves=request.moves,
        )

        return NextOpponentMoveResponse(
            mode=OpponentMoveMode.ENGINE,
            move=MoveDetails(
                uci=controller_move.uci,
                san=controller_move.san,
            ),
            target_blunder_id=None,
            decision_source=DecisionSource.BACKEND_ENGINE,
        )

    except Maia3Error as e:
        raise HTTPException(
            status_code=503,
            detail=f"Maia3 API unavailable: {e}",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid input: {e}")
