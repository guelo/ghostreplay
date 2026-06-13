"""Exact port of the Lichess scalachess game-phase divider.

This module reproduces the opening / middlegame / endgame boundary logic from
``Divider.scala`` in ``lichess-org/scalachess`` so that opening-score v2 can use
the same phase horizon as Lichess.

Upstream source (verified 2026-06-06):
    https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/Divider.scala

The thresholds, strict comparisons, the 49-region mixedness lookup, and the
middle/end control flow below are a faithful translation of that source. We do
not fetch or execute upstream code at runtime; this is a static port.

scalachess is MIT licensed. The required notice accompanies this derived work:

    Copyright (c) 2012-2014 Thibault Duplessis

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

Everything here is pure: functions take ``chess.Board`` objects (or FEN strings)
and return plain values, with no database or IO dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

import chess

from app.fen import normalize_fen

# Version tag for the phase-divider implementation. Bump when the divider logic
# (thresholds, mixedness, control flow) changes so opening-score fingerprints
# invalidate stale cached batches computed under the old divider.
DIVIDER_VERSION = "divider-1"

__all__ = [
    "DIVIDER_VERSION",
    "Division",
    "majors_and_minors",
    "backrank_sparse",
    "mixedness",
    "divide",
    "reconstruct_board_sequence",
    "ContinuityError",
    "is_opening_premove",
    "is_middlegame_position",
]


# ---------------------------------------------------------------------------
# Phase predicates (port of Divider.scala private helpers)
# ---------------------------------------------------------------------------

# scalachess uses an a1=0 little-endian-rank-file bitboard layout, which is the
# same convention python-chess uses for square indexes (chess.A1 == 0). The
# bitboard masks below therefore line up bit-for-bit with the upstream source.
_FIRST_RANK = chess.BB_RANK_1
_LAST_RANK = chess.BB_RANK_8


def majors_and_minors(board: chess.Board) -> int:
    """Count occupied squares excluding kings and pawns.

    Upstream: ``(board.occupied & ~(board.kings | board.pawns)).count``.
    """
    mask = board.occupied & ~(board.kings | board.pawns)
    return chess.popcount(mask)


def backrank_sparse(board: chess.Board) -> bool:
    """True when either back rank holds fewer than four pieces of its colour.

    Upstream::

        (Bitboard.firstRank & board.white).count < 4 ||
          (Bitboard.lastRank & board.black).count < 4
    """
    white_back = chess.popcount(_FIRST_RANK & board.occupied_co[chess.WHITE])
    black_back = chess.popcount(_LAST_RANK & board.occupied_co[chess.BLACK])
    return white_back < 4 or black_back < 4


def _score(y: int, white: int, black: int) -> int:
    """Per-region mixedness contribution.

    Direct translation of ``Divider.score(y)(white, black)``. ``y`` is the
    1-based rank of the region's lower-left corner. Any (white, black) pair not
    listed below contributes 0, matching the upstream ``case _ => 0``.
    """
    if white == 0 and black == 0:
        return 0
    if white == 1 and black == 0:
        return 1 + (8 - y)
    if white == 2 and black == 0:
        return 2 + (y - 2) if y > 2 else 0
    if white == 3 and black == 0:
        return 3 + (y - 1) if y > 1 else 0
    if white == 4 and black == 0:
        return 3 + (y - 1) if y > 1 else 0
    if white == 0 and black == 1:
        return 1 + y
    if white == 1 and black == 1:
        return 5 + abs(4 - y)
    if white == 2 and black == 1:
        return 4 + (y - 1)
    if white == 3 and black == 1:
        return 5 + (y - 1)
    if white == 0 and black == 2:
        return 2 + (6 - y) if y < 6 else 0
    if white == 1 and black == 2:
        return 4 + (7 - y)
    if white == 2 and black == 2:
        return 7
    if white == 0 and black == 3:
        return 3 + (7 - y) if y < 7 else 0
    if white == 1 and black == 3:
        return 5 + (7 - y)
    if white == 0 and black == 4:
        return 3 + (7 - y) if y < 7 else 0
    return 0


def _build_mixedness_regions() -> list[tuple[int, int]]:
    """Construct the 49 overlapping 2x2 regions used by mixedness.

    Upstream::

        val smallSquare = 0x0303L
        for y <- 0 to 6; x <- 0 to 6
          yield (smallSquare << (x + 8 * y), y + 1)

    ``0x0303`` is the 2x2 block a1/b1/a2/b2; shifting it by ``x + 8*y`` slides it
    ``x`` files right and ``y`` ranks up. The region label is ``y + 1``.
    """
    small_square = 0x0303
    regions: list[tuple[int, int]] = []
    for y in range(7):
        for x in range(7):
            regions.append((small_square << (x + 8 * y), y + 1))
    return regions


_MIXEDNESS_REGIONS: list[tuple[int, int]] = _build_mixedness_regions()


def mixedness(board: chess.Board) -> int:
    """Sum of per-region mixedness scores over all 49 regions.

    Upstream sums ``score(y)(whiteCount, blackCount)`` for each region, where the
    counts are the number of white/black pieces inside that 2x2 region.
    """
    white = board.occupied_co[chess.WHITE]
    black = board.occupied_co[chess.BLACK]
    total = 0
    for region, y in _MIXEDNESS_REGIONS:
        w = chess.popcount(white & region)
        b = chess.popcount(black & region)
        total += _score(y, w, b)
    return total


# ---------------------------------------------------------------------------
# Division (port of the apply control flow)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Division:
    """Phase markers for a board sequence.

    ``middle`` and ``end`` are indexes into the board sequence passed to
    :func:`divide` (0-based, matching upstream ``zipWithIndex``). ``plies`` is the
    number of boards. ``middle`` is retained only when it precedes ``end``.
    """

    middle: int | None
    end: int | None
    plies: int

    @property
    def opening_size(self) -> int:
        """Upstream ``Division.openingSize`` = ``middle | plies``."""
        return self.middle if self.middle is not None else self.plies


def _is_middlegame_board(board: chess.Board) -> bool:
    """First-middlegame predicate: any of the three triggers fires."""
    return (
        majors_and_minors(board) <= 10
        or backrank_sparse(board)
        or mixedness(board) > 150
    )


def is_middlegame_position(fen: str) -> bool:
    """Return whether a normalized four-field FEN is a middlegame position."""
    return _is_middlegame_board(chess.Board(f"{fen} 0 1"))


def divide(boards: list[chess.Board]) -> Division:
    """Compute the phase division for a board sequence.

    Faithful port of ``Divider.apply``. Following upstream
    (``replay.chronoMoves.map(_.before.board)``), ``boards`` is the ordered list
    of *pre-move* positions: one board per ply, starting with the initial
    position (index 0). ``plies`` therefore equals the number of moves, and the
    terminal post-move position is not examined.
    """
    middle: int | None = None
    for index, board in enumerate(boards):
        if _is_middlegame_board(board):
            middle = index
            break

    end: int | None = None
    if middle is not None:
        # The end scan restarts from board 0, *not* from the middle index. This
        # mirrors upstream exactly: a middle candidate only gates whether the end
        # scan runs; it does not bound where it starts.
        for index, board in enumerate(boards):
            if majors_and_minors(board) <= 6:
                end = index
                break

    # Retain the middle marker only when it precedes the end marker.
    if middle is not None and end is not None and not (middle < end):
        middle = None

    return Division(middle=middle, end=end, plies=len(boards))


# ---------------------------------------------------------------------------
# Session-line board reconstruction and opening-interval inclusion
# ---------------------------------------------------------------------------


class ContinuityError(ValueError):
    """Raised when a session's move list does not form a continuous board line."""


def _canonical_key(fen: str, move_index: int, field: str) -> str:
    """Canonical position identity (via :func:`app.fen.normalize_fen`).

    Strips move clocks and canonicalizes the en-passant square so that, e.g., a
    ``e3`` EP marker with no legal capture compares equal to ``-`` for the same
    position. Invalid FENs are surfaced as :class:`ContinuityError` to honour the
    documented error contract instead of leaking a raw ``ValueError``.
    """
    try:
        return normalize_fen(fen)
    except ValueError as exc:
        raise ContinuityError(
            f"move {move_index} has an invalid {field}: {fen!r}"
        ) from exc


def reconstruct_board_sequence(
    moves: list[tuple[str, str, str]],
) -> list[chess.Board]:
    """Rebuild the pre-move board sequence for one session line.

    ``moves`` is the deterministically ordered list of ``(fen_before, fen_after,
    move_san)`` triples for a single ``game_session`` (ordered by move number then
    colour). Returns one *pre-move* board per ply —
    ``[board(fen_before[0]), board(fen_before[1]), ...]`` with length
    ``len(moves)`` — matching upstream ``chronoMoves.map(_.before.board)``. The
    terminal post-move position is intentionally not included, so ``plies``
    equals the number of moves.

    Two continuity checks reject malformed lines with :class:`ContinuityError`,
    so the caller can exclude and count the session rather than guessing across:

    1. Each move's ``fen_after`` must equal the next move's ``fen_before`` (no
       jumps between rows).
    2. The stored ``move_san`` must be legal from ``fen_before`` and actually
       produce ``fen_after`` (the row's own before/after pair is a real move, not
       an arbitrary board substitution).
    """
    if not moves:
        return []

    boards: list[chess.Board] = []
    prev_after_key: str | None = None
    for i, move in enumerate(moves):
        fen_before, fen_after, move_san = move
        if fen_before is None or fen_after is None or move_san is None:
            raise ContinuityError(f"move {i} is missing a FEN or SAN")

        before_key = _canonical_key(fen_before, i, "fen_before")
        after_key = _canonical_key(fen_after, i, "fen_after")
        if prev_after_key is not None and prev_after_key != before_key:
            raise ContinuityError(
                f"discontinuity before move {i}: "
                f"{prev_after_key!r} != {before_key!r}"
            )

        # fen_before is known-parseable (normalize_fen above succeeded).
        pre_board = chess.Board(fen_before)
        probe = pre_board.copy(stack=False)
        try:
            parsed = probe.parse_san(move_san)
        except ValueError as exc:
            raise ContinuityError(
                f"move {i} SAN {move_san!r} is illegal from its fen_before"
            ) from exc
        probe.push(parsed)
        if normalize_fen(probe.fen()) != after_key:
            raise ContinuityError(
                f"move {i} SAN {move_san!r} does not produce its fen_after"
            )

        boards.append(pre_board)
        prev_after_key = after_key

    return boards


def is_opening_premove(division: Division, premove_index: int) -> bool:
    """Whether a move made from board ``premove_index`` is opening evidence.

    The opening interval is ply 0 up to but *not* including ``middle``
    (``Division.openingSize`` semantics). The move that transitions the board
    into the first middlegame position has ``premove_index == middle - 1`` and is
    therefore included as the final opening decision. When there is no middle
    marker the whole line is opening (matching ``openingSize == plies``).
    """
    if division.middle is None:
        return True
    return premove_index < division.middle
