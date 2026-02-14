"""
Opponent move controller: Maia-2 candidates + Stockfish calibration.

For ELO >= MAIA_ELO_FLOOR: weighted sample from Maia-2's probability
distribution (the model already differentiates rating bins here).

For ELO < MAIA_ELO_FLOOR: Maia-2 candidates + Stockfish centipawn-loss
targeting to simulate weaker play than Maia-2 can represent alone.

Feature-gated behind VMU_ENABLED env var.
"""
import logging
import math
import os
import random
from dataclasses import dataclass

from app.loss_distribution import sample_target_loss
from app.maia_engine import (
    MAIA_ELO_FLOOR,
    MaiaCandidate,
    MaiaEngineService,
    MaiaEngineUnavailableError,
    MaiaMove,
)
from app.stockfish_service import CandidateEval, StockfishService, StockfishServiceError

logger = logging.getLogger(__name__)

VMU_ENABLED = os.environ.get("VMU_ENABLED", "false").lower() in ("true", "1", "yes")
HUMAN_PENALTY_WEIGHT = float(os.environ.get("VMU_HUMAN_PENALTY_WEIGHT", "15.0"))


@dataclass
class ControllerMove:
    """Result of the opponent move controller."""
    uci: str
    san: str
    method: str  # "maia_argmax", "maia_sample", or "calibrated"


def choose_move(fen: str, target_elo: int, moves: list[str] | None = None) -> ControllerMove:
    """
    Select an opponent move for the given position and target ELO.

    When VMU_ENABLED is false, falls back to Maia-2 argmax (the
    pre-existing behavior).

    Args:
        fen: Current board position FEN.
        target_elo: Target ELO for move selection.
        moves: UCI move history from game start (used by Maia3 API).

    Raises:
        MaiaEngineUnavailableError: If Maia-2 model is unavailable
    """
    if not VMU_ENABLED:
        return _maia_argmax(fen, target_elo)

    if target_elo >= MAIA_ELO_FLOOR:
        return _maia_sampling(fen, target_elo)
    else:
        return _calibrated_selection(fen, target_elo)


def _maia_argmax(fen: str, target_elo: int) -> ControllerMove:
    """Legacy path: always pick the highest-probability Maia move."""
    move = MaiaEngineService.get_best_move(fen, target_elo)
    return ControllerMove(uci=move.uci, san=move.san, method="maia_argmax")


def _maia_sampling(fen: str, target_elo: int) -> ControllerMove:
    """
    For ELO >= 1100: weighted random sample from Maia-2's distribution.

    Maia-2 bins already differentiate rating levels here, so sampling
    from the distribution (rather than always picking the argmax) adds
    natural human-like variation.
    """
    candidates = MaiaEngineService.get_move_candidates(fen, target_elo)
    picked = _weighted_sample(candidates)
    return ControllerMove(uci=picked.uci, san=picked.san, method="maia_sample")


def _calibrated_selection(fen: str, target_elo: int) -> ControllerMove:
    """
    For ELO < 1100: Maia candidates + Stockfish loss targeting.

    1. Get human-plausible candidates from Maia (at ELO 1100 floor)
    2. Evaluate each with Stockfish
    3. Sample a target loss from the ELO's loss distribution
    4. Pick the candidate whose loss is closest to the target,
       penalizing very low Maia probability (alien moves)
    """
    candidates = MaiaEngineService.get_move_candidates(fen, elo=MAIA_ELO_FLOOR)

    try:
        evals = StockfishService.evaluate_moves(fen, [c.uci for c in candidates])
    except StockfishServiceError:
        # If Stockfish is unavailable, fall back to Maia sampling
        logger.warning("Stockfish unavailable, falling back to Maia sampling")
        picked = _weighted_sample(candidates)
        return ControllerMove(uci=picked.uci, san=picked.san, method="maia_sample")

    target_loss = sample_target_loss(target_elo)

    # Build eval lookup
    eval_by_uci = {e.uci: e for e in evals}

    best_score = float("inf")
    best_candidate = candidates[0]
    best_eval = evals[0]

    for cand in candidates:
        ev = eval_by_uci.get(cand.uci)
        if ev is None:
            continue

        # Distance from target loss (lower = better fit)
        loss_fit = abs(ev.cp_loss_vs_best - target_loss)

        # Mild penalty for very low Maia probability
        # Avoids selecting moves that look alien even as blunders
        human_penalty = -math.log(max(cand.probability, 0.001))

        score = loss_fit + HUMAN_PENALTY_WEIGHT * human_penalty

        if score < best_score:
            best_score = score
            best_candidate = cand
            best_eval = ev

    logger.debug(
        f"VMU calibrated at ELO {target_elo}: picked {best_candidate.san} "
        f"(maia_prob={best_candidate.probability:.3f}, "
        f"cp_loss={best_eval.cp_loss_vs_best}, target_loss={target_loss:.0f})"
    )

    return ControllerMove(
        uci=best_candidate.uci,
        san=best_candidate.san,
        method="calibrated",
    )


def _weighted_sample(candidates: list[MaiaCandidate]) -> MaiaCandidate:
    """Weighted random choice from Maia candidates by probability."""
    weights = [c.probability for c in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]
