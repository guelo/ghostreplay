"""Centipawn-loss normalization shared by analysis summary code."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case


def centipawn_loss(eval_delta: int | None) -> int | None:
    """Return display/contract CPL from a raw eval delta."""
    if eval_delta is None:
        return None
    return max(eval_delta, 0)


def centipawn_loss_expr(eval_delta_column: Any) -> Any:
    """SQL expression equivalent of :func:`centipawn_loss`."""
    return case((eval_delta_column < 0, 0), else_=eval_delta_column)
