"""Pure helpers for the canonical persisted session-move coordinate."""

from __future__ import annotations


def ply_after(move_number: int, color: str) -> int:
    """Return the one-based ply reached after ``move_number``/``color``."""
    return (move_number - 1) * 2 + (1 if color == "white" else 2)
