"""Centipawn-loss normalization shared by analysis summary code."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import case

# The DECISIVE-MISTAKE CEILING: the maximum a single move contributes to Avg CPL
# and the maximum severity a single blunder contributes to practice scheduling.
# Chosen so a mate pseudo-cp (~10000) move cannot dominate a game's average, squat
# the top-costly list, or dominate Ghost scheduling. It numerically equals the
# win-chance CP_CEILING and adopts the same ±1000 evaluation clip Lichess uses, but
# it is a DISTINCT product control governing per-move CPL / severity (not win-chance
# math), so it is a standalone constant named for its own purpose. It mirrors the
# frontend EVAL_LOSS_CAP_CP (src/workers/analysisUtils.ts). Kept standalone (no
# import coupling to move_classification's CP_CEILING) to avoid a circular-import
# risk; it is the sole backend CPL-cap constant, so there is no backend pair to drift.
CENTIPAWN_LOSS_CAP_CP = 1000


def centipawn_loss(eval_delta: int | None) -> int | None:
    """Return the NORMALIZED display/decision CPL from a raw eval delta.

    Floors negatives to 0 and caps at :data:`CENTIPAWN_LOSS_CAP_CP` (0..1000). This
    is the display/decision CPL / severity normalizer applied at read / projection /
    decision time — NOT the raw-evidence value (which is retained uncapped at rest;
    see :func:`clamp_delta_nonneg`). ``None`` passes through unchanged.
    """
    if eval_delta is None:
        return None
    return min(max(eval_delta, 0), CENTIPAWN_LOSS_CAP_CP)


def centipawn_loss_expr(eval_delta_column: Any) -> Any:
    """SQL expression equivalent of :func:`centipawn_loss`.

    Uses a single multi-branch CASE (SQLite-compatible; avoids LEAST/GREATEST which
    the conftest SQLite tests cannot run). NULL passes through via ``else_``.
    """
    return case(
        (eval_delta_column < 0, 0),
        (eval_delta_column > CENTIPAWN_LOSS_CAP_CP, CENTIPAWN_LOSS_CAP_CP),
        else_=eval_delta_column,
    )


def round_half_up_cpl(value: float | Decimal) -> int:
    """Round a NONNEGATIVE centipawn-loss average to an int, half-up.

    Matches the frontend's ``Math.round`` (src/utils/gameStats.ts) and the convention
    ``accuracy_v1`` already set for accuracy: an exact ``.5`` rounds UP (2.5 -> 3), not
    to even. Nonnegative-only by contract — CPL is floored at 0 by
    :func:`centipawn_loss_expr`, and for negatives ROUND_HALF_UP means away-from-zero,
    which is NOT ``Math.round``'s toward-+inf.

    Decimal-exact on purpose. PostgreSQL ``AVG(NUMERIC)`` returns ``Decimal`` while
    SQLite returns ``float``, and a float round-trip corrupts near-halves
    (``float(Decimal("2.4999999999999999"))`` is exactly ``2.5``, which would round up
    to 3 instead of down to 2). Callers MUST pass the database aggregate straight
    through — never ``float(raw)``, and never via a local that was itself float()-cast.
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(d.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def clamp_delta_nonneg(eval_delta: int | None) -> int | None:
    """Return the RAW-evidence clamp: floor negatives to 0 with NO upper cap.

    For the analysis_cache write, whose stored ``eval_delta`` must remain the exact
    contract value (may legitimately be a mate pseudo-cp ~10000). Unlike
    :func:`centipawn_loss`, it never applies the decisive-mistake ceiling. ``None``
    passes through unchanged.
    """
    if eval_delta is None:
        return None
    return max(eval_delta, 0)
