"""
Opponent move controller: delegates to the remote Maia3 API.

Converts the UCI move returned by Maia3 into both UCI and SAN formats
for the frontend.
"""
import logging
from dataclasses import dataclass

import chess

from app.maia3_client import get_move as maia3_get_move

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


# Distinguishes a retention-degraded move from a Maia one in the decision log and
# in internal telemetry. It is NOT a public decision_source: the response still
# carries engine/backend_engine, because to every client and to root confirmation
# this is an ordinary non-targeted engine move.
LOCAL_FALLBACK_METHOD = "local_legal"


def fallback_move(fen: str, *, seed: int) -> ControllerMove:
    """A legal move for this position chosen locally, with no remote inference.

    Exists for exactly one caller, in exactly one situation: SRS retention
    bookkeeping has suppressed a request's target AND :func:`choose_move` cannot
    answer — either the Maia3 API is down, or it returned a move that is illegal
    in the requested position. A suppressed request goes to the engine first, like
    any other untargeted move — losing a target is not a reason to play worse.
    This is the floor under that, because a guarantee whose whole content is
    "never fail a move" must not rest on a network dependency that is allowed to
    be down, nor on that dependency agreeing with the caller about the position.

    Selection is deterministic in ``seed`` over the SORTED legal UCI moves, so the
    same request for the same position answers identically every time. That is not
    cosmetic: a retry that misses the decision replay (a lost response, a rolled
    back winner) recomputes here, and an unstable choice would offer the client a
    different move under the same fingerprint. Sorting pins the order that
    ``board.legal_moves`` does not promise.

    Strength is deliberately not a goal. This is a rare, bounded degradation, and
    a weak legal reply is a better answer than a 503.

    Raises:
        ValueError: if the position has no legal move. Callers hand that to the
            existing terminal/invalid-position handling; it is not a retention
            condition and must not be reported as one.
    """
    board = chess.Board(fen)
    legal = sorted(move.uci() for move in board.legal_moves)
    if not legal:
        raise ValueError(f"No legal move is available for FEN {fen}")

    uci = legal[seed % len(legal)]
    move = chess.Move.from_uci(uci)
    return ControllerMove(uci=uci, san=board.san(move), method=LOCAL_FALLBACK_METHOD)
