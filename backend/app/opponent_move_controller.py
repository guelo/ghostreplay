"""
Opponent move controller: delegates to the remote Maia3 API.

Converts the UCI move returned by Maia3 into both UCI and SAN formats
for the frontend.
"""
import logging
from dataclasses import dataclass

import chess

from app.maia3_client import Maia3Error, get_move as maia3_get_move

logger = logging.getLogger(__name__)


@dataclass
class ControllerMove:
    """Result of the opponent move controller."""
    uci: str
    san: str
    method: str


def choose_move(fen: str, target_elo: int, moves: list[str] | None = None) -> ControllerMove:
    """
    Select an opponent move for the given position and target ELO
    by calling the remote Maia3 API.

    Args:
        fen: Current board position FEN.
        target_elo: Target ELO for move selection.
        moves: UCI move history from game start.

    Raises:
        Maia3Error: If the Maia3 API call fails.
        ValueError: If the returned UCI move is illegal in the position.
    """
    result = maia3_get_move(moves=moves or [], target_elo=target_elo)

    board = chess.Board(fen)
    uci_move = chess.Move.from_uci(result.uci)

    if uci_move not in board.legal_moves:
        raise ValueError(
            f"Maia3 returned illegal move {result.uci} for FEN {fen}"
        )

    san = board.san(uci_move)

    return ControllerMove(uci=result.uci, san=san, method="maia3_api")
