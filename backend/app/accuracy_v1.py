"""Frozen v1 of the Lichess-compatible whole-game accuracy algorithm.

This module is the immutable snapshot of accuracy **algorithm version 1**, the
implementation Release A writes to session rows. It is byte-for-byte the pre-A
algorithm ported from lila modules/analyse/src/main/AccuracyPercent.scala
(gameAccuracy, fromWinPercents) and chess.eval.WinPercent, captured from
``app.accuracy`` at blob faed4614153c72b6a3170a9b37d5580c769f697c (commit
01f6afd) under python-chess 1.11.2.

DO NOT change the numeric behavior here. The frozen goldens in
``tests/fixtures/accuracy_v1_goldens.json`` pin these outputs forever, and stored
session accuracies were computed by this exact code. Any semantic change, or a
change forced by a new python-chess version, is **accuracy v2** — a new module
under the plan in ``docs/session-accuracy-versioning.md`` — not an edit to this
file.

The live import surface is ``app.accuracy``, which re-exports the supported
public names from here. Application code imports from ``app.accuracy``; only
historical migrations and the frozen-golden tests import ``app.accuracy_v1``
directly, so that a future v2 re-export can never silently rewrite history.

The public entry point is :func:`compute_game_accuracy`, a pure function used by
both the session-analysis and history code paths so they share one rule for
null/incomplete handling and produce identical results for the same game.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Lichess constants ---------------------------------------------------------

# chess.eval.WinPercent.fromCentiPawns
_WIN_PERCENT_MULTIPLIER = -0.00368208
_CP_CLAMP = 1000
# A mate is forced to a large centipawn magnitude before clamping.
_MATE_CP = 10000

# AccuracyPercent.fromWinPercents
_ACC_A = 103.1668100711649
_ACC_B = -0.04354415386753951
_ACC_C = -3.166924740191411
# The trailing +1 is lila's "uncertainty bonus".
_ACC_BONUS = 1.0

# chess.eval.Cp.initial — the eval before any move is played (white-relative).
_INITIAL_CP = 15


def expected_total_moves_from_pgn(pgn: str | None) -> int | None:
    """Mainline ply count from a stored PGN, or None if unparseable.

    Shared by the session-analysis and history completion rules so both code
    paths derive identical expected move counts.
    """
    if not pgn:
        return None
    try:
        import io

        import chess.pgn

        pgn_game = chess.pgn.read_game(io.StringIO(pgn))
        if pgn_game is None:
            return None
        # Reject malformed PGNs: python-chess reports parse problems in .errors
        # (e.g. illegal/unparseable moves) and yields 0 for non-PGN text. In
        # either case the expected ply count is not reliably determinable.
        if pgn_game.errors:
            return None
        ply_count = sum(1 for _ in pgn_game.mainline_moves())
        return ply_count if ply_count > 0 else None
    except Exception:
        return None


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def win_percent_from_cp(cp: int) -> float:
    """Win% (0-100) for a white-relative centipawn eval."""
    clamped = _clamp(cp, -_CP_CLAMP, _CP_CLAMP)
    return 50 + 50 * (2 / (1 + math.exp(_WIN_PERCENT_MULTIPLIER * clamped)) - 1)


def accuracy_from_win_percents(before: float, after: float) -> float:
    """Per-move accuracy from Win% before/after, from the mover's perspective."""
    if after >= before:
        return 100.0
    raw = _ACC_A * math.exp(_ACC_B * (before - after)) + _ACC_C + _ACC_BONUS
    return _clamp(raw, 0.0, 100.0)


def _stddev(values: list[float]) -> float:
    """Population standard deviation."""
    n = len(values)
    if n == 0:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


@dataclass(frozen=True)
class AccuracyMove:
    """Minimal per-move eval needed for accuracy.

    ``color`` is "white" or "black".  ``eval_cp`` / ``eval_mate`` are stored in
    the MOVE COLOR's perspective (as uploaded); this module flips them to
    white-relative.  Exactly one of ``eval_cp`` / ``eval_mate`` should drive the
    eval; if both are None the post-move eval is missing.
    """

    color: str
    eval_cp: int | None
    eval_mate: int | None


def _white_relative_cp(move: AccuracyMove) -> int | None:
    if move.eval_mate is not None:
        # Post-move mate-0 means the mover just delivered checkmate (the side to
        # move is the mated one), so eval_mate == 0 is a mover WIN. Only a
        # strictly negative mate count means the mover is getting mated. Mirrors
        # the frontend's moverMateToWhiteCp resolution (analysisUtils.ts:137-143).
        magnitude = _MATE_CP if move.eval_mate >= 0 else -_MATE_CP
        cp = magnitude
    elif move.eval_cp is not None:
        cp = move.eval_cp
    else:
        return None
    sign = -1 if move.color == "black" else 1
    return cp * sign


def compute_game_accuracy(
    moves: list[AccuracyMove],
    player_color: str,
    expected_total_moves: int | None,
) -> int | None:
    """Whole-game accuracy (0-100) for ``player_color``, or None.

    ``moves`` must be ordered by ply (move_number, then white before black).
    Returns None when the game is incomplete, the PGN ply count is unknown, the
    player has no analyzed moves, or any eval required for a player transition
    is missing.
    """
    if expected_total_moves is None:
        return None

    n = len(moves)
    if n < expected_total_moves:
        return None
    if n == 0:
        return None

    # White-relative centipawns per move (None where eval missing).
    cps: list[int | None] = [_white_relative_cp(m) for m in moves]

    # allWinPercents = [WinPercent(15)] + [WinPercent(cp) for cp in cps]
    all_win: list[float | None] = [win_percent_from_cp(_INITIAL_CP)] + [
        (win_percent_from_cp(cp) if cp is not None else None) for cp in cps
    ]

    # windowSize = clamp(N // 10, 2, 8); N is the move count.
    window_size = int(_clamp(n // 10, 2, 8))

    # windows = (min(windowSize, len(all)) - 2) copies of the first window,
    # then every sliding window of size windowSize over allWinPercents.
    first_window = all_win[:window_size]
    lead_copies = max(0, min(window_size, len(all_win)) - 2)
    windows: list[list[float | None]] = [first_window] * lead_copies
    for i in range(0, len(all_win) - window_size + 1):
        windows.append(all_win[i : i + window_size])

    # weights[k] = clamp(stddev(window values), 0.5, 12); None if window has a gap.
    weights: list[float | None] = []
    for window in windows:
        if any(v is None for v in window):
            weights.append(None)
        else:
            weights.append(_clamp(_stddev([v for v in window]), 0.5, 12))  # type: ignore[arg-type]

    # pairs = sliding(allWinPercents, 2): there are N consecutive (prev, next) pairs.
    # startColor is the color to move first (white in standard chess).
    start_is_white = True

    weighted_sum: dict[str, float] = {"white": 0.0, "black": 0.0}
    weight_total: dict[str, float] = {"white": 0.0, "black": 0.0}
    harmonic_terms: dict[str, list[float]] = {"white": [], "black": []}
    # Track whether the player had any missing transition.
    player_missing = False
    player_move_count = 0

    for i in range(n):
        prev = all_win[i]
        nxt = all_win[i + 1]
        weight = weights[i] if i < len(weights) else None
        color = "white" if ((i % 2 == 0) == start_is_white) else "black"

        if color == player_color:
            player_move_count += 1
            if prev is None or nxt is None or weight is None:
                player_missing = True
                continue

        if prev is None or nxt is None or weight is None:
            continue

        if color == "white":
            acc = accuracy_from_win_percents(prev, nxt)
        else:
            acc = accuracy_from_win_percents(nxt, prev)

        weighted_sum[color] += acc * weight
        weight_total[color] += weight
        harmonic_terms[color].append(acc)

    if player_missing or player_move_count == 0:
        return None

    accs = harmonic_terms[player_color]
    if not accs or weight_total[player_color] == 0:
        return None

    weighted_mean = weighted_sum[player_color] / weight_total[player_color]
    # Harmonic mean of per-move accuracies (guard against zeros).
    if any(a <= 0 for a in accs):
        harmonic_mean = 0.0
    else:
        harmonic_mean = len(accs) / sum(1.0 / a for a in accs)

    game_acc = (weighted_mean + harmonic_mean) / 2
    # Half-up rounding to match Lichess/Scala (Python round() is banker's).
    return math.floor(game_acc + 0.5)
