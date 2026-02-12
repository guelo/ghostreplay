"""Centipawn-loss target distribution by ELO.

Models per-move centipawn loss as a log-normal distribution with
(mu, sigma) linearly interpolated by target ELO.  Used by the
OpponentMoveController to decide *how wrong* a sub-1100 bot
should play on each move.

Calibration points derived from Lichess aggregate stats:

    ELO 600  → median ~65 cp, p90 ~350 cp, blunder(>200cp) ~18%
    ELO 800  → median ~45 cp, p90 ~250 cp, blunder(>200cp) ~12%
    ELO 1000 → median ~30 cp, p90 ~180 cp, blunder(>200cp) ~8%
"""

from __future__ import annotations

import math
import random
from typing import NamedTuple

# ---------------------------------------------------------------------------
# Calibration table
# ---------------------------------------------------------------------------
# Each row: (elo, mu, sigma)  where mu/sigma parameterise a log-normal.
#   median = exp(mu)
#   p90    = exp(mu + 1.2816 * sigma)
#   P(X>200) ≈ 1 - Φ((ln200 - mu) / sigma)
#
# Values derived by solving for (mu, sigma) that simultaneously match
# the Lichess median and 90th-percentile targets above.

_CalPoint = NamedTuple("_CalPoint", [("elo", int), ("mu", float), ("sigma", float)])

_CALIBRATION: list[_CalPoint] = [
    _CalPoint(600, 4.174, 1.31),   # median ≈ 65 cp
    _CalPoint(800, 3.807, 1.34),   # median ≈ 45 cp
    _CalPoint(1000, 3.401, 1.40),  # median ≈ 30 cp
]

_ELO_MIN = _CALIBRATION[0].elo
_ELO_MAX = _CALIBRATION[-1].elo


def _lerp_params(target_elo: int) -> tuple[float, float]:
    """Linearly interpolate (mu, sigma) for *target_elo*.

    Clamps to the nearest calibration point outside the table range.
    """
    if target_elo <= _ELO_MIN:
        return _CALIBRATION[0].mu, _CALIBRATION[0].sigma
    if target_elo >= _ELO_MAX:
        return _CALIBRATION[-1].mu, _CALIBRATION[-1].sigma

    # Find the surrounding pair
    for i in range(len(_CALIBRATION) - 1):
        lo, hi = _CALIBRATION[i], _CALIBRATION[i + 1]
        if lo.elo <= target_elo <= hi.elo:
            t = (target_elo - lo.elo) / (hi.elo - lo.elo)
            mu = lo.mu + t * (hi.mu - lo.mu)
            sigma = lo.sigma + t * (hi.sigma - lo.sigma)
            return mu, sigma

    # Shouldn't reach here, but satisfy the type checker
    return _CALIBRATION[-1].mu, _CALIBRATION[-1].sigma  # pragma: no cover


def sample_target_loss(target_elo: int, *, rng: random.Random | None = None) -> float:
    """Sample a centipawn-loss target for one move.

    Returns a non-negative float (centipawns).  Most values will be
    small (good moves); occasionally large values appear (blunders).
    The distribution shifts toward higher losses at lower ELOs.

    Args:
        target_elo: Desired playing strength (typically 500-1100).
        rng: Optional random.Random instance for reproducibility.
    """
    mu, sigma = _lerp_params(target_elo)
    r = rng if rng is not None else random
    return r.lognormvariate(mu, sigma)


def get_params(target_elo: int) -> tuple[float, float]:
    """Return the (mu, sigma) pair for *target_elo* (exposed for tests)."""
    return _lerp_params(target_elo)
