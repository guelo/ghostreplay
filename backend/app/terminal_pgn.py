"""Pure, bounded replay of a terminal PGN mainline.

This is the single PGN replay/counting boundary shared by terminal row
reconciliation and fresh opening-evidence proof.  It deliberately has no ORM
or SQLAlchemy dependencies so scorer code can import it without widening its
source fence to session visibility and mutation contracts.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import chess
import chess.pgn


MAX_TERMINAL_PGN_BYTES = 32_768
MAX_DERIVABLE_PLIES = 600


def pgn_size_over_ceiling(
    pgn: str,
    *,
    max_bytes: int = MAX_TERMINAL_PGN_BYTES,
) -> bool:
    """Return whether ``pgn`` exceeds ``max_bytes`` of strict UTF-8."""
    if len(pgn) > max_bytes:
        return True
    try:
        return len(pgn.encode("utf-8")) > max_bytes
    except UnicodeEncodeError:
        return True


@dataclass(frozen=True)
class PlyRecord:
    move_number: int
    color: str
    san: str
    uci: str
    fen_before: str
    fen_after: str


def replay_pgn_mainline(pgn: str | None) -> list[PlyRecord] | None:
    """Replay a stored PGN mainline into per-ply records, or return ``None``."""
    if not pgn:
        return None
    try:
        pgn_game = chess.pgn.read_game(io.StringIO(pgn))
        if pgn_game is None or pgn_game.errors:
            return None
        board = pgn_game.board()
        records: list[PlyRecord] = []
        for move in pgn_game.mainline_moves():
            fen_before = board.fen()
            move_number = board.fullmove_number
            color = "white" if board.turn == chess.WHITE else "black"
            san = board.san(move)
            board.push(move)
            records.append(
                PlyRecord(
                    move_number=move_number,
                    color=color,
                    san=san,
                    uci=move.uci(),
                    fen_before=fen_before,
                    fen_after=board.fen(),
                )
            )
        return records or None
    except Exception:
        return None


def bounded_replay_pgn_mainline(pgn: str | None) -> list[PlyRecord] | None:
    """Replay within the terminal byte ceiling, or skip the PGN entirely."""
    if pgn is None or pgn_size_over_ceiling(pgn):
        return None
    return replay_pgn_mainline(pgn)
