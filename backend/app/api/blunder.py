"""Blunder recording endpoint.

Receives blunder data from frontend, replays PGN to build position graph,
and records the blunder for spaced repetition review.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from io import StringIO

import chess
import chess.pgn
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import active_color, fen_hash, normalize_fen
from app.models import Blunder, GameSession, Move, Position
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/blunder", tags=["blunder"])
AUTO_RECORDING_MAX_FULL_MOVES = 10


class BlunderRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    pgn: str = Field(..., description="Full game history in PGN format")
    fen: str = Field(..., description="Position FEN before the bad move (sanity check)")
    user_move: str = Field(..., description="SAN of the bad move")
    best_move: str = Field(..., description="SAN of the engine's best move")
    eval_before: int = Field(..., description="Centipawn eval of best move")
    eval_after: int = Field(..., description="Centipawn eval after user's move")


class ManualBlunderRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    pgn: str = Field(..., description="Game history in PGN format through selected move")
    fen: str = Field(..., description="Position FEN before the selected move (sanity check)")
    user_move: str = Field(..., description="SAN of the selected move")
    best_move: str | None = Field(
        None,
        description="SAN/notation of best move at capture time (optional metadata)",
    )
    eval_before: int | None = Field(
        None,
        description="Centipawn eval of best move (optional metadata)",
    )
    eval_after: int | None = Field(
        None,
        description="Centipawn eval after selected move (optional metadata)",
    )


class BlunderResponse(BaseModel):
    blunder_id: int | None
    position_id: int
    positions_created: int
    is_new: bool


@dataclass
class ReplayData:
    positions_data: list[tuple[str, str, str]]  # (fen, hash, active_color)
    moves_data: list[tuple[str, str, str]]  # (from_hash, move_san, to_hash)
    pre_move_fen_raw: str
    pre_move_hash: str
    pre_move_color: str


def _get_session_or_404(
    db: Session,
    session_id: uuid.UUID,
) -> GameSession:
    session = db.query(GameSession).filter(GameSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return session


def _ensure_session_owned_by_user(
    session: GameSession,
    user: TokenPayload,
) -> None:
    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")


def _full_moves_played(half_moves: int) -> int:
    """Convert ply count to full-move count (1.e4 = full move 1)."""
    return (half_moves + 1) // 2


def _replay_pgn(
    request_pgn: str,
    request_fen: str,
    *,
    max_full_moves: int | None = None,
) -> ReplayData:
    pgn_io = StringIO(request_pgn)
    game = chess.pgn.read_game(pgn_io)
    if game is None:
        raise HTTPException(status_code=422, detail="Invalid PGN format")

    board = game.board()
    positions_data: list[tuple[str, str, str]] = []
    moves_data: list[tuple[str, str, str]] = []

    start_fen = board.fen()
    positions_data.append((start_fen, fen_hash(start_fen), active_color(start_fen)))

    for move in game.mainline_moves():
        from_hash = fen_hash(board.fen())
        move_san = board.san(move)
        board.push(move)
        to_fen = board.fen()
        moves_data.append((from_hash, move_san, fen_hash(to_fen)))
        positions_data.append((to_fen, fen_hash(to_fen), active_color(to_fen)))

    if len(positions_data) < 2:
        raise HTTPException(status_code=422, detail="PGN must contain at least one move")

    if max_full_moves is not None:
        full_moves = _full_moves_played(len(moves_data))
        if full_moves > max_full_moves:
            raise HTTPException(
                status_code=422,
                detail=f"Automatic blunder recording is limited to the first {max_full_moves} full moves",
            )

    pre_move_fen_raw, pre_move_hash, pre_move_color = positions_data[-2]
    if normalize_fen(pre_move_fen_raw) != normalize_fen(request_fen):
        raise HTTPException(
            status_code=422,
            detail="Pre-move FEN mismatch: position does not match PGN",
        )

    return ReplayData(
        positions_data=positions_data,
        moves_data=moves_data,
        pre_move_fen_raw=pre_move_fen_raw,
        pre_move_hash=pre_move_hash,
        pre_move_color=pre_move_color,
    )


def _upsert_positions(
    db: Session,
    *,
    user_id: int,
    positions_data: list[tuple[str, str, str]],
) -> tuple[dict[str, int], int]:
    hash_to_position_id: dict[str, int] = {}
    positions_created = 0

    for fen_raw, hash_val, color in positions_data:
        existing = db.query(Position).filter(
            Position.user_id == user_id,
            Position.fen_hash == hash_val,
        ).first()

        if existing:
            hash_to_position_id[hash_val] = existing.id
            continue

        position = Position(
            user_id=user_id,
            fen_hash=hash_val,
            fen_raw=fen_raw,
            active_color=color,
        )
        db.add(position)
        db.flush()
        hash_to_position_id[hash_val] = position.id
        positions_created += 1

    return hash_to_position_id, positions_created


def _upsert_moves(
    db: Session,
    *,
    moves_data: list[tuple[str, str, str]],
    hash_to_position_id: dict[str, int],
) -> None:
    for from_hash, move_san, to_hash in moves_data:
        from_id = hash_to_position_id[from_hash]
        to_id = hash_to_position_id[to_hash]

        existing_move = db.query(Move).filter(
            Move.from_position_id == from_id,
            Move.move_san == move_san,
        ).first()
        if existing_move:
            continue

        db.add(
            Move(
                from_position_id=from_id,
                move_san=move_san,
                to_position_id=to_id,
            )
        )


def _upsert_blunder_target(
    db: Session,
    *,
    user_id: int,
    position_id: int,
    user_move: str,
    best_move: str,
    eval_loss: int,
) -> tuple[int, bool]:
    existing_blunder = db.query(Blunder).filter(
        Blunder.user_id == user_id,
        Blunder.position_id == position_id,
    ).first()

    if existing_blunder:
        return existing_blunder.id, False

    blunder = Blunder(
        user_id=user_id,
        position_id=position_id,
        bad_move_san=user_move,
        best_move_san=best_move,
        eval_loss_cp=eval_loss,
    )
    db.add(blunder)
    db.flush()
    return blunder.id, True


def _record_target(
    *,
    db: Session,
    session: GameSession,
    user: TokenPayload,
    pgn: str,
    fen: str,
    user_move: str,
    best_move: str,
    eval_before: int,
    eval_after: int,
    mark_first_blunder_recorded: bool,
    max_full_moves: int | None = None,
) -> BlunderResponse:
    replay_data = _replay_pgn(pgn, fen, max_full_moves=max_full_moves)

    if replay_data.pre_move_color != session.player_color:
        raise HTTPException(
            status_code=400,
            detail=(
                "Cannot record blunder: position is "
                f"{replay_data.pre_move_color} to move but player is {session.player_color}"
            ),
        )

    hash_to_position_id, positions_created = _upsert_positions(
        db,
        user_id=user.user_id,
        positions_data=replay_data.positions_data,
    )
    _upsert_moves(
        db,
        moves_data=replay_data.moves_data,
        hash_to_position_id=hash_to_position_id,
    )

    pre_move_position_id = hash_to_position_id[replay_data.pre_move_hash]
    blunder_id, is_new = _upsert_blunder_target(
        db,
        user_id=user.user_id,
        position_id=pre_move_position_id,
        user_move=user_move,
        best_move=best_move,
        eval_loss=eval_before - eval_after,
    )

    if mark_first_blunder_recorded:
        session.blunder_recorded = True

    db.commit()
    return BlunderResponse(
        blunder_id=blunder_id,
        position_id=pre_move_position_id,
        positions_created=positions_created,
        is_new=is_new,
    )


@router.post("", response_model=BlunderResponse, status_code=201)
def record_blunder(
    request: BlunderRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> BlunderResponse:
    """
    Record a blunder from a game session.

    Replays the PGN to build all intermediate positions and edges,
    then records the blunder at the pre-move position.

    Only the first blunder per game session is recorded. If a blunder
    has already been recorded for this session, returns 200 with the
    existing data.

    The blunder is only recorded if the position's side-to-move matches
    the player's color in the session.
    """
    session = _get_session_or_404(db, request.session_id)
    _ensure_session_owned_by_user(session, user)

    # Check if blunder already recorded for this session
    if session.blunder_recorded:
        return BlunderResponse(
            blunder_id=None,
            position_id=0,
            positions_created=0,
            is_new=False,
        )

    return _record_target(
        db=db,
        session=session,
        user=user,
        pgn=request.pgn,
        fen=request.fen,
        user_move=request.user_move,
        best_move=request.best_move,
        eval_before=request.eval_before,
        eval_after=request.eval_after,
        mark_first_blunder_recorded=True,
        max_full_moves=AUTO_RECORDING_MAX_FULL_MOVES,
    )


@router.post("/manual", response_model=BlunderResponse, status_code=201)
def record_manual_blunder(
    request: ManualBlunderRequest,
    db: Session = Depends(get_db),
    user: TokenPayload = Depends(get_current_user),
) -> BlunderResponse:
    """Add a selected MoveList decision to the ghost library."""
    session = _get_session_or_404(db, request.session_id)
    _ensure_session_owned_by_user(session, user)

    best_move = request.best_move or request.user_move
    eval_before = request.eval_before if request.eval_before is not None else 0
    eval_after = request.eval_after if request.eval_after is not None else eval_before

    return _record_target(
        db=db,
        session=session,
        user=user,
        pgn=request.pgn,
        fen=request.fen,
        user_move=request.user_move,
        best_move=best_move,
        eval_before=eval_before,
        eval_after=eval_after,
        mark_first_blunder_recorded=False,
    )
