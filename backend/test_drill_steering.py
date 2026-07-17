from __future__ import annotations

import chess

from app.drill_steering import (
    build_line_route_map,
    route_move_for_uci,
    route_preserving_moves,
)
from app.fen import normalize_fen


def _positions(line: list[str]) -> list[str]:
    """Normalized FENs reached by replaying `line` from the start position."""
    board = chess.Board()
    out = [normalize_fen(board.fen())]
    for uci in line:
        board.push(chess.Move.from_uci(uci))
        out.append(normalize_fen(board.fen()))
    return out


def test_build_line_route_map_distances_and_target():
    line = ["e2e4", "e7e5", "g1f3"]
    positions = _positions(line)
    rmap = build_line_route_map(line)

    assert rmap.target_fen == positions[3]
    # Distance = plies remaining to the target, target itself at 0.
    assert rmap.plies_by_fen[positions[0]] == 3
    assert rmap.plies_by_fen[positions[1]] == 2
    assert rmap.plies_by_fen[positions[2]] == 1
    assert rmap.plies_by_fen[positions[3]] == 0
    assert rmap.is_target(positions[3])
    assert rmap.is_on_route(positions[1])


def test_build_line_route_map_one_forward_move_per_position():
    line = ["e2e4", "e7e5", "g1f3"]
    positions = _positions(line)
    rmap = build_line_route_map(line)

    assert rmap.forward_moves is not None
    # Each non-target position offers exactly the single on-route next move.
    for i, uci in enumerate(line):
        moves = rmap.forward_moves[positions[i]]
        assert len(moves) == 1
        assert moves[0].uci == uci
        assert moves[0].resulting_fen == positions[i + 1]
        assert moves[0].plies_to_target == len(line) - (i + 1)
    # The target carries no forward move.
    assert rmap.forward_moves[positions[3]] == []


def test_route_helpers_use_line_map_without_a_graph_node():
    line = ["e2e4", "e7e5", "g1f3"]
    positions = _positions(line)
    rmap = build_line_route_map(line)

    # routing=None proves the strict line map never touches the routing view
    # (off-book positions have no graph node, and no overlay can reach them).
    moves = route_preserving_moves(None, rmap, positions[1])
    assert [m.uci for m in moves] == ["e7e5"]

    match = route_move_for_uci(None, rmap, positions[1], "e7e5")
    assert match is not None
    assert match.uci == "e7e5"
    assert match.san == "e5"
    # A legal move that is not the line continuation is not route-preserving.
    assert route_move_for_uci(None, rmap, positions[1], "g8f6") is None
    # An on-route position with no graph node still yields its line move.
    assert route_preserving_moves(None, rmap, positions[0])[0].uci == "e2e4"


def test_build_line_route_map_duplicate_position_keeps_first_occurrence():
    # e4 e5, then knights out and back, returns to the e4/e5 position (white to
    # move) a second time before Bc4 — positions[2] == positions[6].
    line = ["e2e4", "e7e5", "g1f3", "g8f6", "f3g1", "f6g8", "f1c4"]
    positions = _positions(line)
    assert positions[2] == positions[6]

    rmap = build_line_route_map(line)
    # First-occurrence policy: the revisited position keeps its earliest move
    # and distance, so the route stays deterministic from the top.
    assert rmap.plies_by_fen[positions[2]] == len(line) - 2
    assert rmap.forward_moves[positions[2]][0].uci == "g1f3"
    assert route_preserving_moves(None, rmap, positions[2])[0].uci == "g1f3"
