"""Frozen v1 input-shape contract for whole-game accuracy.

:func:`app.accuracy_v1.compute_game_accuracy` attributes each ply to a mover by
INDEX PARITY (``accuracy_v1.py:200``) and takes the eval's sign from
``move.color`` (``_white_relative_cp``, ``accuracy_v1.py:133``). Those are
independent axes: hand it a row list that is not the contiguous mainline
coordinate grid and they disagree, so the accuracy it returns is silently WRONG
rather than None.

This module is that grid's definition. It is frozen for the same reason
:mod:`app.accuracy_v1` is: a persisted ``player_accuracy`` depends on whether
this validation passed, so the Release B backfill/repair migration imports it
DIRECTLY and never through the mutable :mod:`app.accuracy` re-export — a guard
that only wrapped the live surface would be skipped by exactly the code that
writes most of the rows.

Never edited in place; superseded by an ``accuracy_rows_v2.py`` if the input
contract ever changes. See ``docs/session-accuracy-versioning.md``.

The live, guarded entry point is :func:`app.accuracy.game_accuracy_for_rows`;
application code calls that, never :func:`compute_game_accuracy` directly.
"""

from __future__ import annotations

__all__ = ["ply_color", "ply_coordinates_intact"]


def ply_color(row) -> str:
    """The ONE colour-normalization rule for accuracy inputs — public on purpose.

    An ORM ``SessionMove.color`` is an Enum (``session.py`` passes ORM rows); a
    query-tuple row carries a plain ``str``. Validation and ``AccuracyMove``
    construction MUST normalize identically, or the guard could validate a grid
    the algorithm then reads differently — so this is a public frozen seam that
    both go through, not a private helper each re-implements.
    """
    color = row.color
    return color.value if hasattr(color, "value") else str(color)


def ply_coordinates_intact(rows) -> bool:
    """True when ``rows`` ARE the contiguous mainline ply-COORDINATE grid.

    ``rows`` must already be ordered (move_number ASC, white before black). Row
    ``i`` must carry ``move_number == i // 2 + 1`` and colour white if ``i`` is
    even, black if odd.

    Scope, precisely: this validates COORDINATES, not identity. It catches a
    missing ply, a move_number gap, a colour/parity disagreement, and any row set
    whose coordinates are not the mainline grid — which is exactly the invariant
    the parity attribution and the ``move.color`` eval sign need in order to name
    the same side. It does NOT prove a row carries the PGN's move: a row sitting
    at the right coordinate with a different san/fen_before passes. Only a
    PGN-vs-rows replay could catch that.

    That gap is real and was measured, not merely anticipated. g-i6st replayed a
    restore of the 2026-07-24 production dump and found 20 ended-visible sessions
    that pass this function while carrying rows whose ``move_san`` disagrees with
    their own PGN mainline; 19 of them serve an accuracy computed over those rows
    (1.2% of all served values). The shape is a discarded two-ply variation left
    at coordinates the real game later reused — the record resyncs immediately
    after, so the grid stays perfect. The population is historical (2026-03-28 to
    2026-04-04, plus one 2026-05-15 outlier) and the accepted decision was to
    record it rather than supersede this contract; see g-i6st for the evidence
    and the priced alternatives, and g-discard-branch-rows for the writer.
    """
    for i, row in enumerate(rows):
        if row.move_number != i // 2 + 1:
            return False
        if ply_color(row) != ("white" if i % 2 == 0 else "black"):
            return False
    return True
