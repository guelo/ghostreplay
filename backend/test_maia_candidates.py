"""
Unit tests for MaiaEngineService.get_move_candidates().

Tests the candidate generation logic (filtering, sorting, ELO clamping)
by mocking _run_inference — the internal seam between Maia-2 model
inference and candidate selection.
"""
from unittest.mock import patch

import pytest

from app.maia_engine import (
    MAIA_ELO_FLOOR,
    MaiaCandidate,
    MaiaEngineService,
)

# Sicilian Defense: 1. e4 c5 — black just played, white to move
SICILIAN_FEN = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"

# Realistic Maia-2 move_probs for SICILIAN_FEN
SICILIAN_PROBS = {
    "g1f3": 0.35,
    "b1c3": 0.22,
    "d2d4": 0.18,
    "f1c4": 0.08,
    "f2f4": 0.05,
    "d1h5": 0.03,
    "g2g3": 0.02,
    "c2c3": 0.015,
    "d2d3": 0.012,
    "b2b4": 0.008,  # below 1% threshold
    "a2a3": 0.005,
    "h2h3": 0.003,
}


def _mock_run_inference(move_probs, captured_args=None):
    """Patch _run_inference to return fixed move_probs and optionally capture args."""

    def fake(fen, elo):
        if captured_args is not None:
            captured_args.append((fen, elo))
        return dict(move_probs)

    return patch.object(MaiaEngineService, "_run_inference", side_effect=fake)


class TestGetMoveCandidates:
    def test_returns_top_k_candidates(self):
        """Returns at most top_k candidates sorted by probability."""
        with _mock_run_inference(SICILIAN_PROBS):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500, top_k=5
            )

        assert len(candidates) == 5
        assert candidates[0].uci == "g1f3"
        assert candidates[0].probability == 0.35
        assert candidates[1].uci == "b1c3"
        assert candidates[4].uci == "f2f4"

    def test_filters_below_min_prob(self):
        """Moves below min_prob are excluded."""
        with _mock_run_inference(SICILIAN_PROBS):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500, top_k=20, min_prob=0.01
            )

        ucis = [c.uci for c in candidates]
        # b2b4 (0.008), a2a3 (0.005), h2h3 (0.003) should be filtered
        assert "b2b4" not in ucis
        assert "a2a3" not in ucis
        assert "h2h3" not in ucis
        # d2d3 (0.012) and c2c3 (0.015) should be included
        assert "d2d3" in ucis
        assert "c2c3" in ucis

    def test_always_returns_at_least_one(self):
        """Even if all probs are below threshold, returns the best move."""
        tiny_probs = {"g1f3": 0.005, "b1c3": 0.003}
        with _mock_run_inference(tiny_probs):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500, top_k=8, min_prob=0.01
            )

        assert len(candidates) == 1
        assert candidates[0].uci == "g1f3"

    def test_returns_maia_candidate_dataclass(self):
        """Each result is a MaiaCandidate with uci, san, probability."""
        with _mock_run_inference(SICILIAN_PROBS):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500, top_k=3
            )

        for c in candidates:
            assert isinstance(c, MaiaCandidate)
            assert isinstance(c.uci, str)
            assert isinstance(c.san, str)
            assert isinstance(c.probability, float)

        # Verify SAN conversion
        assert candidates[0].san == "Nf3"
        assert candidates[1].san == "Nc3"

    def test_elo_passed_through_to_inference(self):
        """ELO is forwarded to _run_inference (which handles clamping)."""
        captured = []
        with _mock_run_inference(SICILIAN_PROBS, captured_args=captured):
            MaiaEngineService.get_move_candidates(SICILIAN_FEN, elo=600)

        assert captured[0] == (SICILIAN_FEN, 600)

    def test_sorted_by_probability_descending(self):
        """Candidates are sorted highest probability first."""
        with _mock_run_inference(SICILIAN_PROBS):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500, top_k=8
            )

        probs = [c.probability for c in candidates]
        assert probs == sorted(probs, reverse=True)

    def test_default_top_k_is_8(self):
        """Default top_k returns at most 8 candidates."""
        with _mock_run_inference(SICILIAN_PROBS):
            candidates = MaiaEngineService.get_move_candidates(
                SICILIAN_FEN, elo=1500
            )

        # SICILIAN_PROBS has 9 moves above 0.01 threshold, top_k=8 limits it
        assert len(candidates) == 8


class TestRunInference:
    """Test _run_inference ELO clamping and validation (requires mocking deeper)."""

    def test_elo_clamped_to_floor(self):
        """ELO values below MAIA_ELO_FLOOR are clamped to 1100."""
        captured = []

        def fake_inference_each(model, prepared, fen, elo_self, elo_oppo):
            captured.append((elo_self, elo_oppo))
            return SICILIAN_PROBS, 0.55

        with patch.object(MaiaEngineService, "_model", object()), \
             patch.object(MaiaEngineService, "_prepared", object()), \
             patch("maia2.inference.inference_each", fake_inference_each):
            MaiaEngineService._run_inference(SICILIAN_FEN, elo=600)

        assert captured[0] == (MAIA_ELO_FLOOR, MAIA_ELO_FLOOR)

    def test_elo_above_floor_not_clamped(self):
        """ELO values at or above the floor are passed through."""
        captured = []

        def fake_inference_each(model, prepared, fen, elo_self, elo_oppo):
            captured.append((elo_self, elo_oppo))
            return SICILIAN_PROBS, 0.55

        with patch.object(MaiaEngineService, "_model", object()), \
             patch.object(MaiaEngineService, "_prepared", object()), \
             patch("maia2.inference.inference_each", fake_inference_each):
            MaiaEngineService._run_inference(SICILIAN_FEN, elo=1500)

        assert captured[0] == (1500, 1500)

    def test_elo_validation_low(self):
        """ELO below 500 raises ValueError."""
        with patch.object(MaiaEngineService, "_model", object()), \
             patch.object(MaiaEngineService, "_prepared", object()):
            with pytest.raises(ValueError, match="Elo must be between"):
                MaiaEngineService._run_inference(SICILIAN_FEN, elo=300)

    def test_elo_validation_high(self):
        """ELO above 2200 raises ValueError."""
        with patch.object(MaiaEngineService, "_model", object()), \
             patch.object(MaiaEngineService, "_prepared", object()):
            with pytest.raises(ValueError, match="Elo must be between"):
                MaiaEngineService._run_inference(SICILIAN_FEN, elo=2500)


class TestGetBestMoveRefactored:
    """Verify get_best_move still works after refactoring to use get_move_candidates."""

    def test_returns_highest_prob_move(self):
        with _mock_run_inference(SICILIAN_PROBS):
            result = MaiaEngineService.get_best_move(SICILIAN_FEN, elo=1500)

        assert result.uci == "g1f3"
        assert result.san == "Nf3"
        assert result.confidence == 0.35

    def test_elo_validation_preserved(self):
        """get_best_move still validates ELO range via _run_inference."""
        with patch.object(MaiaEngineService, "_model", object()), \
             patch.object(MaiaEngineService, "_prepared", object()):
            with pytest.raises(ValueError):
                MaiaEngineService.get_best_move(SICILIAN_FEN, elo=300)
