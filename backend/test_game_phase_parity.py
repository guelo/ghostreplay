"""Shared-fixture parity checks for the browser Lichess phase-divider port."""

from __future__ import annotations

import json
from pathlib import Path

import chess
import pytest

from app.game_phase import (
    _is_middlegame_board,
    backrank_sparse,
    divide,
    majors_and_minors,
    mixedness,
)
from app.opening_boundary import OPENING_BOUNDARY_MAX_PROBE_PLY

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "utils"
    / "__fixtures__"
    / "gamePhaseParity.json"
)
FIXTURE = json.loads(FIXTURE_PATH.read_text())


def test_fixture_uses_server_probe_cap():
    assert FIXTURE["max_probe_ply"] == OPENING_BOUNDARY_MAX_PROBE_PLY


@pytest.mark.parametrize(
    "position", FIXTURE["positions"], ids=lambda position: position["fen"]
)
def test_phase_predicates_match_shared_fixture(position):
    board = chess.Board(position["fen"])
    assert majors_and_minors(board) == position["majors_and_minors"]
    assert backrank_sparse(board) is position["backrank_sparse"]
    assert mixedness(board) == position["mixedness"]
    assert _is_middlegame_board(board) is position["is_middlegame"]


@pytest.mark.parametrize("line", FIXTURE["lines"], ids=lambda line: line["name"])
def test_divide_matches_shared_line_fixture(line):
    boards = [chess.Board(), *(chess.Board(fen) for fen in line["fens"])]
    division = divide(boards)
    assert division.middle == line["middle"]
    assert division.end == line["end"]
    capped = divide(boards[: OPENING_BOUNDARY_MAX_PROBE_PLY + 1])
    assert capped.middle == line["opening_ply_count"]
