from __future__ import annotations

from types import SimpleNamespace

import chess
import pytest
from sqlalchemy import literal, select

from app.accuracy_v1 import expected_total_moves_from_pgn
from app.game_phase import (
    CompleteLineProofVerdict,
    prove_complete_standard_line,
)
from app import opening_evidence
from app.session_contracts import ply_after, ply_after_expr
from app.terminal_pgn import (
    MAX_TERMINAL_PGN_BYTES,
    bounded_replay_pgn_mainline,
    replay_pgn_mainline,
)


def _rows(*sans: str):
    board = chess.Board()
    rows = []
    for index, san in enumerate(sans):
        before = board.fen()
        board.push_san(san)
        rows.append(
            SimpleNamespace(
                move_number=index // 2 + 1,
                color="white" if index % 2 == 0 else "black",
                move_san=san,
                fen_before=before,
                fen_after=board.fen(),
                session_id="session-1",
                eval_delta=0,
                eval_cp=0,
                best_move_eval_cp=0,
                session_ts="2026-08-14 12:00:00",
                session_pgn="1. e4 e5 *",
                terminal_line_reconciled=True,
            )
        )
    return rows


@pytest.mark.parametrize(
    ("move_number", "color"),
    [(1, "white"), (1, "black"), (2, "white"), (2, "black"), (500, "black")],
)
def test_ply_after_python_and_sql_renderings_stay_in_parity(
    db_session, move_number, color
):
    rendered = db_session.execute(
        select(ply_after_expr(literal(move_number), literal(color)))
    ).scalar_one()
    assert rendered == ply_after(move_number, color)


def test_complete_standard_line_proof_returns_boards_and_terminal_position():
    rows = _rows("e4", "e5", "Nf3")
    proof = prove_complete_standard_line(rows, 3)
    assert proof.verdict is CompleteLineProofVerdict.PASSED
    assert len(proof.premove_boards) == 3
    assert proof.premove_boards[0].fen() == chess.Board().fen()
    assert proof.final_board is not None
    assert proof.final_board.fen() == rows[-1].fen_after


def test_empty_complete_line_is_the_zero_ply_standard_position():
    proof = prove_complete_standard_line([], 0)
    assert proof.verdict is CompleteLineProofVerdict.PASSED
    assert proof.premove_boards == ()
    assert proof.final_board is not None
    assert proof.final_board.fen() == chess.Board().fen()


def test_fresh_opening_replay_excludes_a_partial_terminal_line():
    rows = _rows("e4", "e5")
    rows.pop(0)
    derived = opening_evidence._derive_session(rows)
    assert derived.excluded
    assert derived.moves == ()
    assert "wrong_row_count" in derived.exclusion_msg


def test_fresh_opening_replay_preserves_a_legacy_unreconciled_prefix():
    rows = _rows("e4", "e5")
    rows.pop(0)
    rows[0].terminal_line_reconciled = False

    derived = opening_evidence._derive_session(rows)

    assert not derived.excluded
    assert len(derived.moves) == 1


def test_fresh_opening_replay_accepts_an_exact_terminal_line():
    derived = opening_evidence._derive_session(_rows("e4", "e5"))
    assert not derived.excluded
    assert len(derived.moves) == 2


def test_fresh_opening_replay_tolerates_rows_surplus_to_a_truncated_pgn():
    rows = _rows("e4", "e5", "Nf3")
    derived = opening_evidence._derive_session(rows)
    assert not derived.excluded
    assert len(derived.moves) == 3


@pytest.mark.parametrize(
    "session_pgn",
    [None, "not a PGN", "1. e4 *" + " " * (MAX_TERMINAL_PGN_BYTES + 1)],
)
def test_fresh_opening_replay_skips_proof_when_bounded_pgn_is_unknown(session_pgn):
    rows = _rows("e4", "e5")
    for row in rows:
        row.session_pgn = session_pgn
    derived = opening_evidence._derive_session(rows)
    assert not derived.excluded


@pytest.mark.parametrize(
    "pgn",
    [
        "1. e4 e5 2. Nf3 *",
        '[SetUp "1"]\n[FEN "8/8/8/8/8/8/K6k/8 w - - 0 1"]\n\n1. Kb3 *',
    ],
)
def test_terminal_replay_and_accuracy_ply_counters_stay_in_parity(pgn):
    replay = replay_pgn_mainline(pgn)
    assert replay is not None
    assert len(replay) == expected_total_moves_from_pgn(pgn)
    assert bounded_replay_pgn_mainline(pgn) == replay


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("wrong_count", CompleteLineProofVerdict.WRONG_ROW_COUNT),
        ("coordinate", CompleteLineProofVerdict.COORDINATE_MISMATCH),
        ("nonstandard", CompleteLineProofVerdict.NONSTANDARD_START),
        ("illegal", CompleteLineProofVerdict.ILLEGAL_OR_DISCONTINUOUS_LINE),
        ("discontinuous", CompleteLineProofVerdict.ILLEGAL_OR_DISCONTINUOUS_LINE),
    ],
)
def test_complete_standard_line_failure_verdicts_are_stable(mutation, expected):
    rows = _rows("e4", "e5")
    expected_ply = 2
    if mutation == "wrong_count":
        expected_ply = 3
    elif mutation == "coordinate":
        rows[0].color = "black"
    elif mutation == "nonstandard":
        board = chess.Board()
        board.push_san("d4")
        rows[0].fen_before = board.fen()
    elif mutation == "illegal":
        rows[1].move_san = "Qh5"
    elif mutation == "discontinuous":
        rows[1].fen_before = chess.Board().fen()

    proof = prove_complete_standard_line(rows, expected_ply)
    assert proof.verdict is expected
    assert proof.premove_boards == ()
    assert proof.final_board is None
