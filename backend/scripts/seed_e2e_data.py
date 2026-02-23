#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chess
import chess.pgn
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.fen import active_color, fen_hash
from app.models import (
    Base,
    Blunder,
    BlunderReview,
    GameSession,
    Move,
    Position,
    SessionMove,
    User,
)
from app.security import hash_password

DEFAULT_DATABASE_URL = "sqlite:///./.tmp/e2e.sqlite3"


@dataclass(frozen=True)
class SeedUser:
    username: str
    password: str


@dataclass(frozen=True)
class MoveSnapshot:
    move_number: int
    color: str
    move_san: str
    fen_before: str
    fen_after: str


def _arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Seed deterministic E2E fixtures.")
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help=f"SQLAlchemy database URL (default: {DEFAULT_DATABASE_URL})",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate all schema tables before seeding.",
    )
    return parser


def _load_seed_users() -> dict[str, SeedUser]:
    return {
        "due": SeedUser(
            username=os.getenv("E2E_DUE_USERNAME", "e2e_due_user"),
            password=os.getenv("E2E_DUE_PASSWORD", "e2e-pass-123"),
        ),
        "stable": SeedUser(
            username=os.getenv("E2E_STABLE_USERNAME", "e2e_stable_user"),
            password=os.getenv("E2E_STABLE_PASSWORD", "e2e-pass-123"),
        ),
        "empty": SeedUser(
            username=os.getenv("E2E_EMPTY_USERNAME", "e2e_empty_user"),
            password=os.getenv("E2E_EMPTY_PASSWORD", "e2e-pass-123"),
        ),
    }


def _build_pgn_and_line(moves_san: list[str]) -> tuple[str, list[MoveSnapshot], list[str]]:
    game = chess.pgn.Game()
    game.headers.pop("Event", None)
    game.headers.pop("Site", None)
    game.headers.pop("Date", None)
    game.headers.pop("Round", None)
    game.headers.pop("White", None)
    game.headers.pop("Black", None)
    game.headers.pop("Result", None)

    board = chess.Board()
    node = game
    snapshots: list[MoveSnapshot] = []
    positions: list[str] = [board.fen()]

    for index, san in enumerate(moves_san):
        move = board.parse_san(san)
        fen_before = board.fen()
        color = "white" if board.turn == chess.WHITE else "black"
        board.push(move)
        fen_after = board.fen()
        node = node.add_variation(move)
        snapshots.append(
            MoveSnapshot(
                move_number=(index // 2) + 1,
                color=color,
                move_san=san,
                fen_before=fen_before,
                fen_after=fen_after,
            )
        )
        positions.append(fen_after)

    exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    pgn = game.accept(exporter).strip()
    return pgn, snapshots, positions


def _get_or_create_user(db: Session, seed_user: SeedUser) -> User:
    user = db.query(User).filter(User.username == seed_user.username).first()
    if user:
        user.password_hash = hash_password(seed_user.password)
        user.is_anonymous = False
        db.flush()
        return user

    user = User(
        id=_next_id(db, User),
        username=seed_user.username,
        password_hash=hash_password(seed_user.password),
        is_anonymous=False,
    )
    db.add(user)
    db.flush()
    return user


def _upsert_position(db: Session, *, user_id: int, fen: str) -> Position:
    hash_value = fen_hash(fen)
    existing = (
        db.query(Position)
        .filter(Position.user_id == user_id, Position.fen_hash == hash_value)
        .first()
    )
    if existing:
        return existing

    position = Position(
        id=_next_id(db, Position),
        user_id=user_id,
        fen_hash=hash_value,
        fen_raw=fen,
        active_color=active_color(fen),
    )
    db.add(position)
    db.flush()
    return position


def _upsert_move(
    db: Session,
    *,
    from_position_id: int,
    move_san: str,
    to_position_id: int,
) -> None:
    existing = (
        db.query(Move)
        .filter(Move.from_position_id == from_position_id, Move.move_san == move_san)
        .first()
    )
    if existing:
        return

    db.add(
        Move(
            from_position_id=from_position_id,
            move_san=move_san,
            to_position_id=to_position_id,
        )
    )


def _next_id(db: Session, model) -> int:
    primary_key = model.__mapper__.primary_key[0]
    current = db.query(func.max(primary_key)).scalar()
    return int(current or 0) + 1


def _seed_blunder_user(
    db: Session,
    *,
    user: User,
    moves_san: list[str],
    blunder_index: int,
    best_move_san: str,
    eval_loss_cp: int,
    pass_streak: int,
    review_days_ago: int,
    review_passed: bool,
    result: str,
) -> None:
    now = datetime.now(timezone.utc)
    reviewed_at = now - timedelta(days=review_days_ago)
    started_at = reviewed_at - timedelta(minutes=8)
    ended_at = reviewed_at - timedelta(minutes=1)

    pgn, snapshots, positions = _build_pgn_and_line(moves_san)
    session_id = uuid.uuid4()

    game_session = GameSession(
        id=session_id,
        user_id=user.id,
        started_at=started_at,
        ended_at=ended_at,
        status="completed",
        result=result,
        engine_elo=1500,
        blunder_recorded=True,
        is_rated=True,
        player_color="white",
        pgn=pgn,
    )
    db.add(game_session)
    db.flush()

    position_rows = [_upsert_position(db, user_id=user.id, fen=fen) for fen in positions]
    for idx, move in enumerate(snapshots):
        _upsert_move(
            db,
            from_position_id=position_rows[idx].id,
            move_san=move.move_san,
            to_position_id=position_rows[idx + 1].id,
        )

    blunder_snapshot = snapshots[blunder_index]
    blunder_position = position_rows[blunder_index]

    blunder = Blunder(
        id=_next_id(db, Blunder),
        user_id=user.id,
        position_id=blunder_position.id,
        bad_move_san=blunder_snapshot.move_san,
        best_move_san=best_move_san,
        eval_loss_cp=eval_loss_cp,
        pass_streak=pass_streak,
        last_reviewed_at=reviewed_at,
        source_session_id=session_id,
        created_at=started_at,
    )
    db.add(blunder)
    db.flush()

    db.add(
        BlunderReview(
            id=_next_id(db, BlunderReview),
            blunder_id=blunder.id,
            session_id=session_id,
            reviewed_at=reviewed_at,
            passed=review_passed,
            move_played_san=blunder_snapshot.move_san,
            eval_delta_cp=eval_loss_cp,
        )
    )

    for idx, snapshot in enumerate(snapshots):
        classification = "blunder" if idx == blunder_index else "good"
        eval_delta = eval_loss_cp if idx == blunder_index else 15
        db.add(
            SessionMove(
                id=_next_id(db, SessionMove),
                session_id=session_id,
                move_number=snapshot.move_number,
                color=snapshot.color,
                move_san=snapshot.move_san,
                fen_after=snapshot.fen_after,
                eval_cp=20 - (idx * 5),
                eval_mate=None,
                best_move_san=best_move_san if idx == blunder_index else snapshot.move_san,
                best_move_eval_cp=20,
                eval_delta=eval_delta,
                classification=classification,
            )
        )


def seed_database(database_url: str, *, reset: bool) -> dict[str, SeedUser]:
    engine = create_engine(database_url)
    if reset:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    users = _load_seed_users()
    with Session(engine) as db:
        due_user = _get_or_create_user(db, users["due"])
        stable_user = _get_or_create_user(db, users["stable"])
        _get_or_create_user(db, users["empty"])

        _seed_blunder_user(
            db,
            user=due_user,
            moves_san=["e4", "e5", "Nf3", "Nc6", "Bc4", "Bc5", "Nxe5", "Nxe5", "d4"],
            blunder_index=6,
            best_move_san="d4",
            eval_loss_cp=180,
            pass_streak=0,
            review_days_ago=14,
            review_passed=False,
            result="checkmate_loss",
        )
        _seed_blunder_user(
            db,
            user=stable_user,
            moves_san=["d4", "d5", "c4", "e6", "Nc3", "Nf6", "Bg5", "Be7", "e3"],
            blunder_index=6,
            best_move_san="Nf3",
            eval_loss_cp=70,
            pass_streak=3,
            review_days_ago=1,
            review_passed=True,
            result="draw",
        )

        db.commit()

    return users


def main() -> None:
    args = _arg_parser().parse_args()
    users = seed_database(args.database_url, reset=args.reset)

    print(f"Seeded E2E data at: {args.database_url}")
    for key, user in users.items():
        print(f"- {key}: {user.username} / {user.password}")


if __name__ == "__main__":
    main()
