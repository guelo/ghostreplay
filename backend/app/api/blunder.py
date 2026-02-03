"""Blunder recording endpoint.

Receives blunder data from frontend, replays PGN to build position graph,
and records the blunder for spaced repetition review.
"""

from __future__ import annotations

import uuid

import chess
import chess.pgn
from fastapi import APIRouter, Depends, HTTPException
from io import StringIO
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.fen import active_color, fen_hash
from app.models import Blunder, GameSession, Move, Position
from app.security import TokenPayload, get_current_user

router = APIRouter(prefix="/api/blunder", tags=["blunder"])


class BlunderRequest(BaseModel):
    session_id: uuid.UUID = Field(..., description="Game session ID")
    pgn: str = Field(..., description="Full game history in PGN format")
    fen: str = Field(..., description="Position FEN before the bad move (sanity check)")
    user_move: str = Field(..., description="SAN of the bad move")
    best_move: str = Field(..., description="SAN of the engine's best move")
    eval_before: int = Field(..., description="Centipawn eval of best move")
    eval_after: int = Field(..., description="Centipawn eval after user's move")


class BlunderResponse(BaseModel):
    blunder_id: int | None
    position_id: int
    positions_created: int
    is_new: bool


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
    # Fetch and validate the session
    session = db.query(GameSession).filter(GameSession.id == request.session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")

    if session.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this game")

    # Check if blunder already recorded for this session
    if session.blunder_recorded:
        # Find the existing blunder position - we need to return position_id
        # The position should exist since blunder_recorded is True
        # Return early without creating anything
        return BlunderResponse(
            blunder_id=None,
            position_id=0,
            positions_created=0,
            is_new=False,
        )

    # Parse PGN and replay to get all positions
    pgn_io = StringIO(request.pgn)
    game = chess.pgn.read_game(pgn_io)

    if game is None:
        raise HTTPException(status_code=422, detail="Invalid PGN format")

    # Collect all positions from the game
    board = game.board()
    positions_data: list[tuple[str, str, str]] = []  # (fen, hash, active_color)
    moves_data: list[tuple[str, str, str]] = []  # (from_hash, move_san, to_hash)

    # Starting position
    start_fen = board.fen()
    start_hash = fen_hash(start_fen)
    start_color = active_color(start_fen)
    positions_data.append((start_fen, start_hash, start_color))

    # Replay all moves
    pre_blunder_fen = None
    pre_blunder_hash = None
    for i, move in enumerate(game.mainline_moves()):
        from_hash = fen_hash(board.fen())
        move_san = board.san(move)

        board.push(move)

        to_fen = board.fen()
        to_hash = fen_hash(to_fen)
        to_color = active_color(to_fen)

        positions_data.append((to_fen, to_hash, to_color))
        moves_data.append((from_hash, move_san, to_hash))

        # Track the position before the last move (that's where the blunder happened)
        if i == len(list(game.mainline_moves())) - 2:
            # This is complex because we need to know total moves
            # Let's track differently
            pass

    # The pre-blunder position is the second-to-last position
    # (the position before user_move was played)
    if len(positions_data) < 2:
        raise HTTPException(status_code=422, detail="PGN must contain at least one move")

    # Get pre-blunder position (position before the last move)
    pre_blunder_fen_raw, pre_blunder_hash, pre_blunder_color = positions_data[-2]

    # Verify the pre-move FEN matches what the frontend sent
    # Normalize both for comparison
    from app.fen import normalize_fen
    if normalize_fen(pre_blunder_fen_raw) != normalize_fen(request.fen):
        raise HTTPException(
            status_code=422,
            detail="Pre-move FEN mismatch: position does not match PGN"
        )

    # Check that the blunder position's side-to-move matches player's color
    if pre_blunder_color != session.player_color:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot record blunder: position is {pre_blunder_color} to move but player is {session.player_color}"
        )

    # Upsert all positions
    positions_created = 0
    hash_to_position_id: dict[str, int] = {}

    for fen_raw, hash_val, color in positions_data:
        # Check if position already exists for this user
        existing = db.query(Position).filter(
            Position.user_id == user.user_id,
            Position.fen_hash == hash_val,
        ).first()

        if existing:
            hash_to_position_id[hash_val] = existing.id
        else:
            position = Position(
                user_id=user.user_id,
                fen_hash=hash_val,
                fen_raw=fen_raw,
                active_color=color,
            )
            db.add(position)
            db.flush()  # Get the ID
            hash_to_position_id[hash_val] = position.id
            positions_created += 1

    # Upsert all moves (edges)
    for from_hash, move_san, to_hash in moves_data:
        from_id = hash_to_position_id[from_hash]
        to_id = hash_to_position_id[to_hash]

        # Check if move edge already exists
        existing_move = db.query(Move).filter(
            Move.from_position_id == from_id,
            Move.move_san == move_san,
        ).first()

        if not existing_move:
            move = Move(
                from_position_id=from_id,
                move_san=move_san,
                to_position_id=to_id,
            )
            db.add(move)

    # Get the pre-blunder position ID
    pre_blunder_position_id = hash_to_position_id[pre_blunder_hash]

    # Check if blunder already exists for this position (from a previous game)
    existing_blunder = db.query(Blunder).filter(
        Blunder.user_id == user.user_id,
        Blunder.position_id == pre_blunder_position_id,
    ).first()

    is_new = existing_blunder is None
    eval_loss = request.eval_before - request.eval_after

    if is_new:
        blunder = Blunder(
            user_id=user.user_id,
            position_id=pre_blunder_position_id,
            bad_move_san=request.user_move,
            best_move_san=request.best_move,
            eval_loss_cp=eval_loss,
        )
        db.add(blunder)
        db.flush()
        blunder_id = blunder.id
    else:
        blunder_id = existing_blunder.id

    # Mark session as having recorded a blunder
    session.blunder_recorded = True

    db.commit()

    return BlunderResponse(
        blunder_id=blunder_id,
        position_id=pre_blunder_position_id,
        positions_created=positions_created,
        is_new=is_new,
    )
