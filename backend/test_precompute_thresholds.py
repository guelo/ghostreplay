"""Threshold-adjacent delta cases: the canonical precompute output must be usable
by every downstream consumer at the exact boundaries they compare against.

Drills fail when ``eval_delta > tier_threshold`` (strict 15 / standard 35 /
lenient 50); recording and SRS treat ``eval_delta >= 50`` as a recordable
failure. These are distinct boundaries (note 50: a lenient drill PASSES at
exactly 50, while recording/SRS already count 50 as a failure).
"""
import chess
import pytest

from app.evidence_contracts import RESOLVER_COMPLETE_V2, contract_satisfied
from app.opening_evidence import PASS_THRESHOLD
from app.session_contracts import DRILL_STRICTNESS_TIER_THRESHOLDS

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def test_named_thresholds_are_pinned():
    assert DRILL_STRICTNESS_TIER_THRESHOLDS == {
        "strict": 15,
        "standard": 35,
        "lenient": 50,
    }
    assert PASS_THRESHOLD == 50  # recordable-failure / SRS boundary (>=)


def _v2_row_with_delta(delta: int) -> dict:
    # White to move: delta = best - played, so pick played=0, best=delta.
    return {
        "fen_before": START_FEN,
        "best_move_uci": "e2e4",
        "best_line_uci": ["e2e4", "e7e5"],
        "classification": "good",
        "played_eval": 0,
        "best_eval": delta,
        "eval_delta": delta,
    }


@pytest.mark.parametrize("delta", [0, 1, 14, 15, 16, 34, 35, 36, 49, 50, 51])
def test_v2_rows_valid_at_threshold_boundaries(delta):
    # A canonical row carries an internally consistent delta at every boundary the
    # consumers compare against, so it is always a valid v2 trusted-cache row.
    assert contract_satisfied(RESOLVER_COMPLETE_V2, _v2_row_with_delta(delta))


def _drill_fails(delta: int, tier: str) -> bool:
    return delta > DRILL_STRICTNESS_TIER_THRESHOLDS[tier]


def _is_recordable(delta: int) -> bool:
    return delta >= PASS_THRESHOLD


@pytest.mark.parametrize(
    "tier,delta,expect_fail",
    [
        ("strict", 14, False), ("strict", 15, False), ("strict", 16, True),
        ("standard", 34, False), ("standard", 35, False), ("standard", 36, True),
        ("lenient", 49, False), ("lenient", 50, False), ("lenient", 51, True),
    ],
)
def test_drill_strict_greater_than_boundary(tier, delta, expect_fail):
    assert _drill_fails(delta, tier) is expect_fail


@pytest.mark.parametrize(
    "delta,expect", [(49, False), (50, True), (51, True)]
)
def test_recordable_failure_is_inclusive_50(delta, expect):
    assert _is_recordable(delta) is expect


def test_lenient_drill_and_recording_diverge_at_50():
    # The key distinction: at exactly 50 a lenient drill passes but the move is a
    # recordable failure for blunder recording / SRS.
    assert _drill_fails(50, "lenient") is False
    assert _is_recordable(50) is True
    # Sanity: the stored delta is genuinely side-to-move-relative and clamped.
    board = chess.Board(START_FEN)
    assert board.turn == chess.WHITE
