"""
Unit tests for StockfishService.

Tests eval output format, cp_loss computation, and mate score handling
using the real Stockfish binary (integration-style, not mocked).
"""
import chess
import pytest

from app.stockfish_service import (
    MATE_CP_BASE,
    CandidateEval,
    StockfishService,
    _eval_to_cp,
    _mate_to_cp,
)

# Sicilian Defense: 1. e4 c5 — white to move
SICILIAN_FEN = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"


@pytest.fixture(autouse=True)
def _reset_service():
    """Reset StockfishService between tests."""
    StockfishService.reset()
    yield
    StockfishService.reset()


class TestMateToCp:
    def test_mate_in_1(self):
        assert _mate_to_cp(1) == MATE_CP_BASE

    def test_mate_in_3(self):
        assert _mate_to_cp(3) == MATE_CP_BASE - 2

    def test_getting_mated_in_1(self):
        assert _mate_to_cp(-1) == -MATE_CP_BASE

    def test_getting_mated_in_3(self):
        assert _mate_to_cp(-3) == -(MATE_CP_BASE - 2)

    def test_zero(self):
        assert _mate_to_cp(0) == 0


class TestEvalToCp:
    def test_cp_negated_for_mover(self):
        """After pushing a move, eval is from opponent's view — we negate."""
        # Opponent sees +50 → mover has -50 (we negate to get mover's view)
        assert _eval_to_cp({"type": "cp", "value": 50}, chess.BLACK) == -50

    def test_cp_negative_negated(self):
        """Opponent sees -30 → mover has +30."""
        assert _eval_to_cp({"type": "cp", "value": -30}, chess.BLACK) == 30

    def test_mate_for_opponent_is_bad_for_mover(self):
        """Opponent can mate in 2 → very bad for mover."""
        cp = _eval_to_cp({"type": "mate", "value": 2}, chess.BLACK)
        assert cp < -9000

    def test_opponent_getting_mated_is_good_for_mover(self):
        """Opponent getting mated in 1 → very good for mover."""
        cp = _eval_to_cp({"type": "mate", "value": -1}, chess.BLACK)
        assert cp > 9000


class TestEvaluateMoves:
    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_returns_correct_count(self):
        candidates = ["g1f3", "d2d4", "a2a3"]
        results = StockfishService.evaluate_moves(SICILIAN_FEN, candidates)
        assert len(results) == 3

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_returns_candidate_eval_dataclass(self):
        results = StockfishService.evaluate_moves(SICILIAN_FEN, ["g1f3"])
        assert isinstance(results[0], CandidateEval)
        assert results[0].uci == "g1f3"
        assert isinstance(results[0].cp_score, int)
        assert isinstance(results[0].cp_loss_vs_best, int)

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_preserves_input_order(self):
        candidates = ["a2a3", "g1f3", "d2d4"]
        results = StockfishService.evaluate_moves(SICILIAN_FEN, candidates)
        assert [r.uci for r in results] == candidates

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_best_candidate_has_zero_loss(self):
        """The best-scoring candidate should have cp_loss_vs_best == 0."""
        candidates = ["g1f3", "d2d4", "a2a3", "h2h4"]
        results = StockfishService.evaluate_moves(SICILIAN_FEN, candidates)
        losses = [r.cp_loss_vs_best for r in results]
        assert min(losses) == 0

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_all_losses_non_negative(self):
        """cp_loss_vs_best should never be negative."""
        candidates = ["g1f3", "d2d4", "a2a3", "h2h4"]
        results = StockfishService.evaluate_moves(SICILIAN_FEN, candidates)
        for r in results:
            assert r.cp_loss_vs_best >= 0, f"{r.uci} has negative loss"

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_good_move_scores_higher_than_bad(self):
        """Nf3 (a strong move) should score higher than h4 (dubious)."""
        results = StockfishService.evaluate_moves(SICILIAN_FEN, ["g1f3", "h2h4"])
        nf3 = next(r for r in results if r.uci == "g1f3")
        h4 = next(r for r in results if r.uci == "h2h4")
        assert nf3.cp_score > h4.cp_score

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_empty_candidates(self):
        assert StockfishService.evaluate_moves(SICILIAN_FEN, []) == []

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_mate_position(self):
        """Evaluating moves in a position where one candidate gives mate."""
        # White can play Qh7# (mate in 1)
        fen = "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4"
        results = StockfishService.evaluate_moves(fen, ["h5f7"])
        assert results[0].cp_score > 9000  # mate score

    @pytest.mark.skipif(
        not StockfishService.is_available(),
        reason="Stockfish binary not found",
    )
    def test_single_candidate(self):
        results = StockfishService.evaluate_moves(SICILIAN_FEN, ["g1f3"])
        assert len(results) == 1
        assert results[0].cp_loss_vs_best == 0  # only candidate is "best"
