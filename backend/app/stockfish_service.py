"""
Server-side Stockfish evaluation service for scoring candidate moves.

Used by the VMU (Variable-strength Maia-2 Upgrade) controller to compute
centipawn loss for each Maia candidate, enabling strength calibration
below Maia-2's ELO floor.
"""
import logging
import os
from dataclasses import dataclass
from typing import ClassVar, Optional

import chess
from stockfish import Stockfish, StockfishException

logger = logging.getLogger(__name__)

# Large centipawn value used when Stockfish reports mate.
# Mate-in-1 = 10000, mate-in-2 = 9999, etc.
MATE_CP_BASE = 10000


@dataclass
class CandidateEval:
    """Stockfish evaluation of a single candidate move."""
    uci: str
    cp_score: int        # centipawn score from the moving side's perspective
    cp_loss_vs_best: int  # how many centipawns worse than the best candidate


class StockfishServiceError(Exception):
    """Raised when Stockfish cannot be initialized or used."""
    pass


def _eval_to_cp(ev: dict, side_to_move_after: chess.Color) -> int:
    """
    Convert a Stockfish evaluation dict to centipawns from the
    *pre-move* side's perspective.

    After pushing a candidate move, Stockfish evaluates from the
    opponent's view. We negate to get the mover's perspective.

    Args:
        ev: Stockfish evaluation dict with 'type' and 'value'
        side_to_move_after: The color to move in the position Stockfish
                           evaluated (i.e., the opponent of the mover)
    """
    if ev["type"] == "mate":
        mate_in = ev["value"]
        # Positive mate_in means the side to move (opponent) can mate.
        # That's bad for our mover, so negate.
        # Negative mate_in means the side to move is getting mated —
        # good for our mover.
        cp = -_mate_to_cp(mate_in)
    else:
        # Stockfish reports cp from the side to move's perspective.
        # Negate to get the mover's (pre-move) perspective.
        cp = -ev["value"]
    return cp


def _mate_to_cp(mate_in: int) -> int:
    """Convert mate-in-N to a large centipawn value preserving sign."""
    if mate_in > 0:
        return MATE_CP_BASE - (mate_in - 1)
    elif mate_in < 0:
        return -(MATE_CP_BASE - (abs(mate_in) - 1))
    return 0


class StockfishService:
    """
    Thin wrapper for server-side Stockfish evaluation.

    Maintains a singleton Stockfish process that's reused across requests.
    Evaluates candidate moves by making each move and reading the resulting
    position's eval, which is faster than MultiPV for small candidate sets.
    """

    _instance: ClassVar[Optional[Stockfish]] = None
    _initialization_error: ClassVar[Optional[str]] = None

    # Configurable via environment
    STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/local/bin/stockfish")
    EVAL_DEPTH = int(os.environ.get("STOCKFISH_EVAL_DEPTH", "8"))
    HASH_MB = 64
    THREADS = 1

    @classmethod
    def _ensure_initialized(cls) -> None:
        if cls._instance is not None:
            return

        if cls._initialization_error:
            raise StockfishServiceError(cls._initialization_error)

        try:
            cls._instance = Stockfish(
                path=cls.STOCKFISH_PATH,
                depth=cls.EVAL_DEPTH,
                parameters={"Hash": cls.HASH_MB, "Threads": cls.THREADS},
            )
            logger.info(
                f"Stockfish initialized: path={cls.STOCKFISH_PATH}, "
                f"depth={cls.EVAL_DEPTH}"
            )
        except Exception as e:
            cls._initialization_error = str(e)
            logger.error(f"Failed to initialize Stockfish: {e}")
            raise StockfishServiceError(str(e))

    @classmethod
    def evaluate_moves(
        cls,
        fen: str,
        candidate_ucis: list[str],
    ) -> list[CandidateEval]:
        """
        Evaluate a list of candidate moves and return centipawn scores.

        For each candidate, pushes the move on a python-chess board,
        evaluates the resulting position with Stockfish, and negates
        the score to get the mover's perspective.

        Args:
            fen: Position in FEN notation (must be the position *before* the move)
            candidate_ucis: List of UCI move strings to evaluate

        Returns:
            List of CandidateEval in the same order as candidate_ucis.
            cp_loss_vs_best is computed relative to the best candidate
            in the list (not the global best move).
        """
        cls._ensure_initialized()

        if not candidate_ucis:
            return []

        board = chess.Board(fen)
        sf = cls._instance
        evals: list[tuple[str, int]] = []

        for uci in candidate_ucis:
            try:
                move = chess.Move.from_uci(uci)
                board_after = board.copy()
                board_after.push(move)

                # Terminal positions: don't ask Stockfish, score directly
                if board_after.is_checkmate():
                    evals.append((uci, MATE_CP_BASE))
                    continue
                if board_after.is_stalemate() or board_after.is_insufficient_material():
                    evals.append((uci, 0))
                    continue

                sf.set_fen_position(board_after.fen())
                ev = sf.get_evaluation()
                cp = _eval_to_cp(ev, board_after.turn)
                evals.append((uci, cp))
            except (StockfishException, Exception) as e:
                # If Stockfish crashes or hangs on a move, reset and skip
                logger.warning(f"Stockfish eval failed for {uci}: {e}")
                cls._instance = None
                cls._initialization_error = None
                cls._ensure_initialized()
                sf = cls._instance
                # Assign a very bad score so this candidate is deprioritized
                evals.append((uci, -MATE_CP_BASE))

        best_cp = max(cp for _, cp in evals)
        return [
            CandidateEval(
                uci=uci,
                cp_score=cp,
                cp_loss_vs_best=best_cp - cp,
            )
            for uci, cp in evals
        ]

    @classmethod
    def is_available(cls) -> bool:
        """Check if Stockfish binary exists at the configured path."""
        return os.path.isfile(cls.STOCKFISH_PATH)

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (for testing)."""
        if cls._instance is not None:
            try:
                del cls._instance
            except Exception:
                pass
        cls._instance = None
        cls._initialization_error = None
