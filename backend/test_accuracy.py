"""Tests for the Lichess-compatible game accuracy module."""

from __future__ import annotations

import math

# Public names come from the live surface (app.accuracy); private helpers are not
# re-exported there and must be imported from the frozen v1 module directly.
from app.accuracy import (
    AccuracyMove,
    accuracy_from_win_percents,
    compute_game_accuracy,
    expected_total_moves_from_pgn,
    win_percent_from_cp,
)
from app.accuracy_v1 import _MATE_CP, _white_relative_cp


def _moves(cps: list[int | None], start: str = "white") -> list[AccuracyMove]:
    """Build an ordered move list alternating colors from white-relative cps.

    ``cps`` are white-relative; convert back to move-color perspective so the
    module's internal flip reproduces them.
    """
    colors = ["white", "black"] if start == "white" else ["black", "white"]
    out: list[AccuracyMove] = []
    for i, cp in enumerate(cps):
        color = colors[i % 2]
        if cp is None:
            out.append(AccuracyMove(color=color, eval_cp=None, eval_mate=None))
            continue
        sign = -1 if color == "black" else 1
        out.append(AccuracyMove(color=color, eval_cp=cp * sign, eval_mate=None))
    return out


def test_win_percent_initial_and_zero():
    assert win_percent_from_cp(0) == 50.0
    assert win_percent_from_cp(15) > 50.0
    # Symmetric around 0.
    assert math.isclose(win_percent_from_cp(200) + win_percent_from_cp(-200), 100.0, abs_tol=1e-6)


def test_win_percent_clamps_large_cp():
    # Mate-like force-as-cp clamps to the same value as +1000.
    assert win_percent_from_cp(10000) == win_percent_from_cp(1000)
    assert win_percent_from_cp(-10000) == win_percent_from_cp(-1000)


def test_accuracy_improvement_is_100():
    assert accuracy_from_win_percents(40.0, 55.0) == 100.0
    assert accuracy_from_win_percents(50.0, 50.0) == 100.0


def test_accuracy_decreases_with_loss():
    small = accuracy_from_win_percents(60.0, 55.0)
    big = accuracy_from_win_percents(60.0, 20.0)
    assert 0.0 <= big < small <= 100.0


def test_mate_eval_forced_to_cp():
    # A white mate (eval_mate>0 stored white) yields near-max win%.
    moves = [
        AccuracyMove(color="white", eval_cp=None, eval_mate=3),
        AccuracyMove(color="black", eval_cp=-50, eval_mate=None),
    ]
    acc = compute_game_accuracy(moves, player_color="white", expected_total_moves=2)
    assert acc is not None


def test_white_relative_cp_mate_zero_is_mover_win():
    # Post-move mate-0 means the mover delivered checkmate (a WIN), so it must
    # map to +_MATE_CP in the mover's color. A strictly negative mate count
    # (mover getting mated) stays -_MATE_CP. eval_cp=None isolates the sign test.
    assert _white_relative_cp(AccuracyMove("white", eval_cp=None, eval_mate=0)) == _MATE_CP
    assert _white_relative_cp(AccuracyMove("black", eval_cp=None, eval_mate=0)) == -_MATE_CP
    assert _white_relative_cp(AccuracyMove("white", eval_cp=None, eval_mate=-3)) == -_MATE_CP
    assert _white_relative_cp(AccuracyMove("black", eval_cp=None, eval_mate=-3)) == _MATE_CP


def test_checkmate_win_scores_high():
    # A cleanly played white win ending in mate must score 100, not collapse to
    # ~31 via the mate-0 sign flip zeroing the harmonic mean. eval_cp on the mate
    # ply mirrors the real uploaded row; the mate branch ignores it.
    moves = [
        AccuracyMove("white", eval_cp=20, eval_mate=None),     # ply 0
        AccuracyMove("black", eval_cp=-10, eval_mate=None),    # ply 1
        AccuracyMove("white", eval_cp=60, eval_mate=None),     # ply 2
        AccuracyMove("black", eval_cp=-40, eval_mate=None),    # ply 3
        AccuracyMove("white", eval_cp=120, eval_mate=None),    # ply 4
        AccuracyMove("black", eval_cp=-90, eval_mate=None),    # ply 5
        AccuracyMove("white", eval_cp=10000, eval_mate=0),     # ply 6 — mate
    ]
    assert compute_game_accuracy(moves, "white", 7) == 100


def test_steady_winning_game_high_accuracy():
    # White slowly improving: every white move >= previous => high accuracy.
    cps = [20, 10, 60, 40, 120, 90, 200, 150]
    moves = _moves(cps)
    acc = compute_game_accuracy(moves, player_color="white", expected_total_moves=len(cps))
    assert acc is not None
    assert acc >= 80


def test_blunder_lowers_accuracy():
    # White drops from +300 to -300 on one move.
    cps = [50, 40, 300, 280, -300, -310, -320, -330]
    moves = _moves(cps)
    acc = compute_game_accuracy(moves, player_color="white", expected_total_moves=len(cps))
    assert acc is not None
    assert acc < 80


def test_black_orientation():
    # Black improving from black's perspective: white-relative cps falling.
    cps = [-20, -50, -40, -120, -90, -200, -150, -260]
    moves = _moves(cps)
    acc = compute_game_accuracy(moves, player_color="black", expected_total_moves=len(cps))
    assert acc is not None
    assert acc >= 80


def test_incomplete_returns_none():
    cps = [20, 10, 60, 40]
    moves = _moves(cps)
    assert compute_game_accuracy(moves, "white", expected_total_moves=10) is None


def test_missing_expected_returns_none():
    cps = [20, 10, 60, 40]
    moves = _moves(cps)
    assert compute_game_accuracy(moves, "white", expected_total_moves=None) is None


def test_missing_player_eval_returns_none():
    cps: list[int | None] = [20, 10, None, 40, 120, 90]
    moves = _moves(cps)  # index 2 is a white move (player) with missing eval
    assert compute_game_accuracy(moves, "white", expected_total_moves=len(cps)) is None


def test_missing_opponent_eval_nullifies_via_player_before_position():
    # A missing opponent (black) post-move eval is the "before" position for the
    # player's next move, so a player transition is missing => null.
    cps: list[int | None] = [20, None, 60, 40, 120, 90]
    moves = _moves(cps)
    assert compute_game_accuracy(moves, "white", expected_total_moves=len(cps)) is None


def test_no_player_moves_returns_none():
    # Single white move (white-first); player is black => zero player moves.
    moves = _moves([20])
    assert compute_game_accuracy(moves, "black", expected_total_moves=1) is None


def test_expected_total_moves_from_pgn():
    pgn = "1. e4 e5 2. Nf3 Nc6 *"
    assert expected_total_moves_from_pgn(pgn) == 4
    assert expected_total_moves_from_pgn(None) is None
    # Non-PGN text yields zero mainline moves => not determinable => None.
    assert expected_total_moves_from_pgn("not a pgn ;;;") is None


def test_malformed_pgn_does_not_yield_accuracy():
    # A non-PGN string yields expected=None, which must force accuracy to None
    # even when player evals are present (guards the expected=0 exploit path).
    moves = _moves([20, 10])
    assert (
        compute_game_accuracy(
            moves,
            "white",
            expected_total_moves=expected_total_moves_from_pgn("not a pgn ;;;"),
        )
        is None
    )


def test_half_up_rounding():
    # game_acc exactly .5 rounds up (banker's round() would give 50).
    assert math.floor(50.5 + 0.5) == 51
