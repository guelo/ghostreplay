"""Deterministic Glicko rating calculations for Ghost Replay."""

from __future__ import annotations

from dataclasses import dataclass
import math

from app.rating import RESULT_SCORES

CHESSCOM_INITIAL_RATING = 1200.0
LICHESS_INITIAL_RATING = 1500.0
INITIAL_RD = 350.0
INITIAL_VOLATILITY = 0.06
ENGINE_RD = 75.0
MIN_RD = 30.0
MAX_RD = 350.0
GLICKO2_TAU = 0.5
GLICKO2_SCALE = 173.7178


@dataclass(frozen=True)
class GlickoState:
    rating: float
    rd: float
    volatility: float | None = None


def _score(result: str) -> float:
    score = RESULT_SCORES.get(result)
    if score is None:
        raise ValueError(f"Unrated result: {result!r}")
    return score


def _clamp_rd(rd: float) -> float:
    return min(MAX_RD, max(MIN_RD, rd))


def compute_chesscom_glicko(
    current: GlickoState,
    opponent_rating: int,
    result: str,
    *,
    opponent_rd: float = ENGINE_RD,
) -> GlickoState:
    """Compute one-game Glicko-1 update using a Chess.com-style starting state."""
    q = math.log(10.0) / 400.0
    score = _score(result)
    rd = _clamp_rd(current.rd)
    opponent_rd = _clamp_rd(opponent_rd)

    g = 1.0 / math.sqrt(1.0 + (3.0 * q * q * opponent_rd * opponent_rd) / (math.pi * math.pi))
    expected = 1.0 / (1.0 + 10.0 ** (-g * (current.rating - opponent_rating) / 400.0))
    d_squared = 1.0 / (q * q * g * g * expected * (1.0 - expected))
    new_rating = current.rating + (q / ((1.0 / (rd * rd)) + (1.0 / d_squared))) * g * (score - expected)
    new_rd = math.sqrt(1.0 / ((1.0 / (rd * rd)) + (1.0 / d_squared)))

    return GlickoState(rating=new_rating, rd=_clamp_rd(new_rd))


def _glicko2_g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _glicko2_e(mu: float, opponent_mu: float, opponent_phi: float) -> float:
    return 1.0 / (1.0 + math.exp(-_glicko2_g(opponent_phi) * (mu - opponent_mu)))


def compute_lichess_glicko2(
    current: GlickoState,
    opponent_rating: int,
    result: str,
    *,
    opponent_rd: float = ENGINE_RD,
    tau: float = GLICKO2_TAU,
) -> GlickoState:
    """Compute one-game Glicko-2 update using a Lichess-style starting state."""
    score = _score(result)
    rating = current.rating
    rd = _clamp_rd(current.rd)
    volatility = current.volatility if current.volatility is not None else INITIAL_VOLATILITY

    mu = (rating - LICHESS_INITIAL_RATING) / GLICKO2_SCALE
    phi = rd / GLICKO2_SCALE
    opponent_mu = (float(opponent_rating) - LICHESS_INITIAL_RATING) / GLICKO2_SCALE
    opponent_phi = _clamp_rd(opponent_rd) / GLICKO2_SCALE

    g = _glicko2_g(opponent_phi)
    expected = _glicko2_e(mu, opponent_mu, opponent_phi)
    v = 1.0 / (g * g * expected * (1.0 - expected))
    delta = v * g * (score - expected)

    a = math.log(volatility * volatility)
    epsilon = 0.000001

    def f(x: float) -> float:
        exp_x = math.exp(x)
        numerator = exp_x * (delta * delta - phi * phi - v - exp_x)
        denominator = 2.0 * (phi * phi + v + exp_x) ** 2
        return (numerator / denominator) - ((x - a) / (tau * tau))

    lower = a
    if delta * delta > phi * phi + v:
        upper = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        upper = a - k * tau
        while f(upper) < 0:
            k += 1
            upper = a - k * tau

    f_lower = f(lower)
    f_upper = f(upper)
    while abs(upper - lower) > epsilon:
        candidate = lower + (lower - upper) * f_lower / (f_upper - f_lower)
        f_candidate = f(candidate)
        if f_candidate * f_upper <= 0:
            lower = upper
            f_lower = f_upper
        else:
            f_lower /= 2.0
        upper = candidate
        f_upper = f_candidate

    new_volatility = math.exp(lower / 2.0)
    phi_star = math.sqrt(phi * phi + new_volatility * new_volatility)
    new_phi = 1.0 / math.sqrt((1.0 / (phi_star * phi_star)) + (1.0 / v))
    new_mu = mu + new_phi * new_phi * g * (score - expected)

    return GlickoState(
        rating=LICHESS_INITIAL_RATING + GLICKO2_SCALE * new_mu,
        rd=_clamp_rd(GLICKO2_SCALE * new_phi),
        volatility=new_volatility,
    )
