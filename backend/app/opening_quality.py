"""Continuous move-quality conversion for opening-score v2.

This module replaces the binary ``eval_delta < 50`` pass/fail signal with a
continuous ``[0, 1]`` move quality derived from mover win-chance loss. It owns
only the quality math and the data-source precedence; phase tagging, evidence
aggregation, and DAG scoring live elsewhere (see ``opening_evidence.py`` and the
``g-shared-score-dag`` work).

Quality source precedence (see ``g-opening-score-v2`` design §2-§3):

1. ``SESSION_EVAL`` — mover-relative ``session_moves.best_move_eval_cp`` and
   ``eval_cp``. These columns are already mate-converted at upload time by
   ``scoreForPlayer`` / ``mateToCp`` in ``src/workers/analysisUtils.ts``; we
   consume them directly and must not convert mates a second time.
2. ``ANALYSIS_CACHE`` — reconstructed from a matching ``analysis_cache`` row.
   Those evals are white-relative with raw mate counts; the caller converts the
   mate counts with :func:`mate_to_cp` and flips to mover perspective via
   :func:`cache_row_to_mover_evals` before scoring.
3. ``EVAL_DELTA`` — a deterministic exponential fallback over the unsigned
   centipawn loss, for historical rows that have neither evaluation pair.

``TAU_WC`` / ``TAU_CP`` and :data:`QUALITY_VERSION` are configuration and must
participate in the score-input fingerprint owned by ``g-score-cache-api``.
"""

from __future__ import annotations

import math

# The win-chance logistic constants are shared with the move classifier so the
# two scoring surfaces cannot drift apart.
from app.move_classification import CP_CEILING, WIN_CHANCE_MULTIPLIER

__all__ = [
    "QUALITY_VERSION",
    "TAU_WC",
    "TAU_CP",
    "SOURCE_SESSION_EVAL",
    "SOURCE_ANALYSIS_CACHE",
    "SOURCE_EVAL_DELTA",
    "mate_to_cp",
    "win_chance",
    "quality_from_win_chance_loss",
    "quality_from_eval_delta",
    "cache_row_to_mover_evals",
    "move_quality",
]

# Bump whenever any constant below changes; this string is part of the score
# model fingerprint so a curve change invalidates cached snapshots.
QUALITY_VERSION = "qv2-wc-1"

# Quality curve parameters (design §2-§3, §12 default).
TAU_WC = 0.20
TAU_CP = 100.0

# mateToCp constants, mirrored from src/workers/analysisUtils.ts.
_MATE_BASE = 10000
_MATE_DECAY = 10

# Quality source labels for telemetry / source accounting.
SOURCE_SESSION_EVAL = "session_eval"
SOURCE_ANALYSIS_CACHE = "analysis_cache"
SOURCE_EVAL_DELTA = "eval_delta"


def mate_to_cp(moves_to_mate: int) -> int:
    """Canonical mate-count → centipawn conversion (port of ``mateToCp``).

    The returned value is in the same perspective as ``moves_to_mate``: positive
    when that perspective delivers mate. ``0`` means the side whose count this is
    has been mated.
    """
    if moves_to_mate == 0:
        return -_MATE_BASE
    sign = 1 if moves_to_mate >= 0 else -1
    return sign * (_MATE_BASE - abs(moves_to_mate) * _MATE_DECAY)


def win_chance(cp: float) -> float:
    """Lichess win-chance logistic, clamped to ``[-1, 1]`` (design §2).

    ``W(cp) = 2 / (1 + exp(-0.00368208 * cp)) - 1`` with ``cp`` clamped to
    ``[-1000, 1000]``. The clamp makes the ~±10000 mate-converted values
    saturate correctly instead of overflowing the exponential.
    """
    clamped = max(-CP_CEILING, min(CP_CEILING, cp))
    return 2 / (1 + math.exp(WIN_CHANCE_MULTIPLIER * clamped)) - 1


def quality_from_win_chance_loss(
    best_cp: float, played_cp: float, tau_wc: float = TAU_WC
) -> float:
    """Bounded move quality from mover-relative best/played centipawns.

    ``loss = clamp(W(best) - W(played), 0, 2)`` and ``quality = exp(-loss/tau)``.
    Both evals must already be in the *mover's* perspective. The best move scores
    exactly ``1``; noise that makes the played eval look better than the best is
    clamped to zero loss.
    """
    loss = win_chance(best_cp) - win_chance(played_cp)
    loss = max(0.0, min(2.0, loss))
    return math.exp(-loss / tau_wc)


def quality_from_eval_delta(eval_delta: float, tau_cp: float = TAU_CP) -> float:
    """Deterministic centipawn-loss fallback ``exp(-max(delta, 0) / tau_cp)``."""
    return math.exp(-max(eval_delta, 0.0) / tau_cp)


def _white_cp(eval_cp: int | None, eval_mate: int | None) -> int | None:
    """Resolve a white-relative centipawn value from an ``analysis_cache`` pair.

    Prefers the already-converted centipawn column. Falls back to the raw mate
    count only for a non-zero (sign-recoverable) mate; a mate-0 with no cp is
    rejected as ``None`` because its winner is ambiguous from the count alone.
    """
    if eval_cp is not None:
        return eval_cp
    if eval_mate is None or eval_mate == 0:
        return None
    return mate_to_cp(eval_mate)


def cache_row_to_mover_evals(
    played_eval: int | None,
    played_eval_mate: int | None,
    best_eval: int | None,
    best_eval_mate: int | None,
    side_to_move: str,
) -> tuple[float, float] | None:
    """Convert white-relative ``analysis_cache`` evals to ``(best, played)`` cp.

    ``analysis_cache`` stores ``played_eval`` / ``best_eval`` white-relative in
    centipawns, already mate-converted at write time (a mate-in-2 for white is
    stored as ``mate_to_cp(2)``), with the raw white-relative mate distance kept
    separately in the ``*_mate`` columns. We therefore trust the centipawn column
    and only fall back to :func:`mate_to_cp` when it is missing. This matters for
    a mate-0 (delivered checkmate): its winner cannot be recovered from the
    signed count alone, but the centipawn column already encodes it (e.g. a White
    checkmate is stored as ``+10000``, not derived from ``mate == 0``).

    The white-relative values are then flipped to the mover's perspective; the
    mover is the side to move in ``fen_before`` (``side_to_move`` is ``'w'`` or
    ``'b'``). Returns ``None`` when an evaluation cannot be recovered so the
    caller falls through to the ``eval_delta`` fallback.

    A mate-0 (a position already checkmated) is rejected when it is the only
    signal: its winner cannot be derived from the signed count alone, and the cp
    column that would disambiguate it is absent here. Guessing a sign would
    reverse delivered checkmates, so we defer to the deterministic eval_delta
    fallback instead.
    """
    played_white = _white_cp(played_eval, played_eval_mate)
    best_white = _white_cp(best_eval, best_eval_mate)
    if played_white is None or best_white is None:
        return None
    flip = 1 if side_to_move == "w" else -1
    return best_white * flip, played_white * flip


def move_quality(
    *,
    eval_cp: int | None,
    best_move_eval_cp: int | None,
    eval_delta: int | None,
    cache_mover_evals: tuple[float, float] | None = None,
    tau_wc: float = TAU_WC,
    tau_cp: float = TAU_CP,
) -> tuple[float | None, str | None]:
    """Resolve a move's continuous quality and its source, honouring precedence.

    Returns ``(quality, source)``; ``(None, None)`` when no usable signal exists.
    ``cache_mover_evals`` is the already-flipped ``(best, played)`` pair from
    :func:`cache_row_to_mover_evals`, or ``None`` when no cache row matched.
    """
    if eval_cp is not None and best_move_eval_cp is not None:
        return (
            quality_from_win_chance_loss(best_move_eval_cp, eval_cp, tau_wc),
            SOURCE_SESSION_EVAL,
        )
    if cache_mover_evals is not None:
        best_mover, played_mover = cache_mover_evals
        return (
            quality_from_win_chance_loss(best_mover, played_mover, tau_wc),
            SOURCE_ANALYSIS_CACHE,
        )
    if eval_delta is not None:
        return quality_from_eval_delta(eval_delta, tau_cp), SOURCE_EVAL_DELTA
    return None, None
