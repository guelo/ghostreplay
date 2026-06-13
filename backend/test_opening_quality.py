"""Tests for continuous move-quality conversion (opening-score v2)."""

from __future__ import annotations

import math

import pytest

from app.opening_quality import (
    SOURCE_ANALYSIS_CACHE,
    SOURCE_EVAL_DELTA,
    SOURCE_SESSION_EVAL,
    TAU_WC,
    cache_row_to_mover_evals,
    mate_to_cp,
    move_quality,
    quality_from_eval_delta,
    quality_from_win_chance_loss,
    win_chance,
)


class TestWinChance:
    def test_zero_is_even(self):
        assert win_chance(0) == pytest.approx(0.0, abs=1e-9)

    def test_monotonic_and_bounded(self):
        assert win_chance(-2000) < win_chance(0) < win_chance(2000)
        # Clamp at +/-1000cp keeps the logistic inside (-1, 1).
        assert -1.0 < win_chance(-100000) < win_chance(100000) < 1.0

    def test_clamps_to_ceiling(self):
        # Beyond the +/-1000 ceiling the value saturates, so mate-converted
        # ~+/-10000 evals do not overflow the exponential.
        assert win_chance(5000) == win_chance(1000)
        assert win_chance(-5000) == win_chance(-1000)


class TestQualityFromWinChanceLoss:
    def test_best_move_is_one(self):
        assert quality_from_win_chance_loss(50, 50) == pytest.approx(1.0)

    def test_noise_clamped_to_zero_loss(self):
        # Played eval better than "best" → negative loss clamped to 0 → quality 1.
        assert quality_from_win_chance_loss(0, 100) == pytest.approx(1.0)

    def test_monotonic_in_loss(self):
        q_small = quality_from_win_chance_loss(100, 50)
        q_big = quality_from_win_chance_loss(100, -300)
        assert 0 < q_big < q_small < 1

    def test_no_49_50_discontinuity(self):
        # Same best eval, played 49cp vs 50cp worse: quality barely moves.
        q49 = quality_from_win_chance_loss(0, -49)
        q50 = quality_from_win_chance_loss(0, -50)
        assert abs(q49 - q50) < 0.01

    def test_context_sensitivity(self):
        # The same 100cp played gap matters more near equality than when already
        # winning by a lot (the logistic is flatter in the tails).
        loss_equal = win_chance(50) - win_chance(-50)
        loss_winning = win_chance(900) - win_chance(800)
        assert loss_equal > loss_winning
        q_equal = quality_from_win_chance_loss(50, -50)
        q_winning = quality_from_win_chance_loss(900, 800)
        assert q_equal < q_winning


class TestEvalDeltaFallback:
    def test_zero_delta_is_one(self):
        assert quality_from_eval_delta(0) == pytest.approx(1.0)

    def test_negative_delta_clamped(self):
        assert quality_from_eval_delta(-200) == pytest.approx(1.0)

    def test_decays(self):
        assert quality_from_eval_delta(100) == pytest.approx(math.exp(-1.0))
        assert quality_from_eval_delta(200) < quality_from_eval_delta(100)


class TestMateToCp:
    def test_mate_zero_is_mated(self):
        assert mate_to_cp(0) == -10000

    def test_sign_and_decay(self):
        assert mate_to_cp(1) == 10000 - 10
        assert mate_to_cp(-3) == -(10000 - 30)


class TestCacheRowToMoverEvals:
    def test_white_to_move_no_flip(self):
        # White to move: white-relative evals already mover-relative.
        out = cache_row_to_mover_evals(
            played_eval=-30, played_eval_mate=None,
            best_eval=40, best_eval_mate=None, side_to_move="w",
        )
        assert out == (40, -30)

    def test_black_to_move_flips(self):
        # Black to move: white-relative evals flip sign to mover perspective.
        out = cache_row_to_mover_evals(
            played_eval=-30, played_eval_mate=None,
            best_eval=40, best_eval_mate=None, side_to_move="b",
        )
        assert out == (-40, 30)

    def test_raw_mate_conversion(self):
        # White-relative best mate-in-2; played is a plain cp eval.
        out = cache_row_to_mover_evals(
            played_eval=120, played_eval_mate=None,
            best_eval=None, best_eval_mate=2, side_to_move="w",
        )
        assert out == (mate_to_cp(2), 120)

    def test_mate_conversion_then_flip(self):
        # Black to move, best is mate-in-1 for white (bad for the mover).
        out = cache_row_to_mover_evals(
            played_eval=-50, played_eval_mate=None,
            best_eval=None, best_eval_mate=1, side_to_move="b",
        )
        best_mover, played_mover = out
        assert best_mover == -mate_to_cp(1)
        assert played_mover == 50

    def test_prefers_cp_column_over_mate_count(self):
        # The cp column is already mate-converted at write time; trust it instead
        # of re-deriving from the (ambiguous) signed mate count.
        out = cache_row_to_mover_evals(
            played_eval=-9980, played_eval_mate=-2,
            best_eval=-9980, best_eval_mate=-2, side_to_move="w",
        )
        assert out == (-9980, -9980)

    def test_delivered_checkmate_not_reversed(self):
        # Regression: a White checkmate (played_eval=+10000, mate=0) must score as
        # the best outcome, not be flipped to a loss by mate_to_cp(0) == -10000.
        out = cache_row_to_mover_evals(
            played_eval=10000, played_eval_mate=0,
            best_eval=10000, best_eval_mate=0, side_to_move="w",
        )
        assert out == (10000, 10000)
        best, played = out
        assert quality_from_win_chance_loss(best, played) == pytest.approx(1.0)

    def test_mate_count_used_only_when_cp_missing(self):
        out = cache_row_to_mover_evals(
            played_eval=120, played_eval_mate=None,
            best_eval=None, best_eval_mate=2, side_to_move="w",
        )
        assert out == (mate_to_cp(2), 120)

    def test_cp_null_mate_zero_is_rejected(self):
        # cp absent and mate-0: winner is ambiguous from the count, so reject the
        # whole cache row (caller falls through to the eval_delta fallback) rather
        # than guess -10000 and reverse a delivered checkmate.
        assert cache_row_to_mover_evals(
            played_eval=None, played_eval_mate=0,
            best_eval=10000, best_eval_mate=None, side_to_move="w",
        ) is None

    def test_missing_eval_returns_none(self):
        assert cache_row_to_mover_evals(None, None, 40, None, "w") is None


class TestMoveQualityPrecedence:
    def test_primary_session_evals_win(self):
        q, source = move_quality(
            eval_cp=-50, best_move_eval_cp=0,
            eval_delta=200, cache_mover_evals=(999, -999),
        )
        assert source == SOURCE_SESSION_EVAL
        assert q == pytest.approx(quality_from_win_chance_loss(0, -50, TAU_WC))

    def test_cache_used_when_primary_absent(self):
        q, source = move_quality(
            eval_cp=None, best_move_eval_cp=None,
            eval_delta=200, cache_mover_evals=(40, -30),
        )
        assert source == SOURCE_ANALYSIS_CACHE
        assert q == pytest.approx(quality_from_win_chance_loss(40, -30, TAU_WC))

    def test_eval_delta_last_resort(self):
        q, source = move_quality(
            eval_cp=None, best_move_eval_cp=None, eval_delta=100,
        )
        assert source == SOURCE_EVAL_DELTA
        assert q == pytest.approx(quality_from_eval_delta(100))

    def test_no_signal(self):
        assert move_quality(eval_cp=None, best_move_eval_cp=None, eval_delta=None) == (None, None)

    def test_primary_requires_both_evals(self):
        # Missing best eval → not primary; falls back to eval_delta.
        q, source = move_quality(eval_cp=-50, best_move_eval_cp=None, eval_delta=80)
        assert source == SOURCE_EVAL_DELTA
