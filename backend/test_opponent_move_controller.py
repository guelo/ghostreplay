"""Opponent move controller: the remote Maia path and the local retention floor.

``fallback_move`` is what answers when a retention-degraded request finds the
engine unavailable too. Its whole value is that it CANNOT fail the way
``choose_move`` can, so the tests here are about independence and determinism
rather than about move quality: no remote call, a legal move for the position it
was asked about, and the same answer every time the same request is recomputed.
"""

from __future__ import annotations

from unittest.mock import patch

import chess
import pytest

from app.opponent_move_controller import (
    LOCAL_FALLBACK_METHOD,
    ControllerMove,
    choose_move,
    fallback_move,
)

START_FEN = chess.STARTING_FEN
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
# Black is mated: no legal move exists.
FOOLS_MATE_FEN = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"


def test_fallback_move_is_legal_in_the_requested_position():
    move = fallback_move(AFTER_E4_FEN, seed=0)

    board = chess.Board(AFTER_E4_FEN)
    assert chess.Move.from_uci(move.uci) in board.legal_moves
    assert move.san == board.san(chess.Move.from_uci(move.uci))
    assert move.method == LOCAL_FALLBACK_METHOD


def test_fallback_move_never_calls_maia():
    """The point of the whole function: no remote dependency in a degradation."""
    with patch("app.opponent_move_controller.maia3_get_move") as remote:
        fallback_move(AFTER_E4_FEN, seed=3)

    remote.assert_not_called()


def test_fallback_move_is_stable_for_the_same_request():
    """A retry that misses the decision replay must recompute the SAME move.

    Otherwise two requests carrying one fingerprint would be offered two different
    moves, and only one of them could ever be the decision that got recorded.
    """
    first = fallback_move(AFTER_E4_FEN, seed=12345)
    second = fallback_move(AFTER_E4_FEN, seed=12345)

    assert first == second


def test_fallback_move_does_not_depend_on_legal_move_ordering():
    """Selection indexes SORTED UCI, which python-chess does not promise on its own."""
    board = chess.Board(AFTER_E4_FEN)
    expected = sorted(move.uci() for move in board.legal_moves)

    chosen = {fallback_move(AFTER_E4_FEN, seed=seed).uci for seed in range(len(expected))}

    assert chosen == set(expected)
    assert fallback_move(AFTER_E4_FEN, seed=len(expected)).uci == expected[0]


def test_fallback_move_varies_with_the_seed():
    """Different requests are not all answered with the same move."""
    moves = {fallback_move(AFTER_E4_FEN, seed=seed).uci for seed in range(20)}

    assert len(moves) > 1


def test_fallback_move_rejects_a_position_with_no_legal_move():
    """Terminal positions stay an ordinary input error, not a retention condition."""
    with pytest.raises(ValueError, match="No legal move"):
        fallback_move(FOOLS_MATE_FEN, seed=0)


def test_choose_move_still_calls_maia_and_converts_the_result():
    """The ordinary engine path is untouched by the fallback's existence."""
    with patch("app.opponent_move_controller.maia3_get_move") as remote:
        remote.return_value = type("Result", (), {"uci": "e7e5"})()
        move = choose_move(AFTER_E4_FEN, target_elo=1500, moves=["e2e4"])

    remote.assert_called_once_with(moves=["e2e4"], target_elo=1500)
    assert move == ControllerMove(uci="e7e5", san="e5", method="maia3_api")


def test_choose_move_rejects_an_illegal_remote_move():
    with patch("app.opponent_move_controller.maia3_get_move") as remote:
        remote.return_value = type("Result", (), {"uci": "e2e4"})()
        with pytest.raises(ValueError, match="illegal move"):
            choose_move(AFTER_E4_FEN, target_elo=1500, moves=["e2e4"])
