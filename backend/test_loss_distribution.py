"""Tests for centipawn-loss target distribution by ELO.

Verifies statistical moments over 10k samples for each calibration band,
monotonicity across ELOs, clamping at boundaries, and interpolation.
"""

import math
import random
import statistics

import pytest

from app.loss_distribution import get_params, sample_target_loss

N = 10_000
SEED = 42


def _sample_n(elo: int, n: int = N) -> list[float]:
    rng = random.Random(SEED)
    return [sample_target_loss(elo, rng=rng) for _ in range(n)]


# ------------------------------------------------------------------
# Calibration-point tests (median / p90 / blunder rate)
# ------------------------------------------------------------------

@pytest.mark.parametrize(
    "elo, expected_median, median_tol, expected_p90, p90_tol, expected_blunder_rate, blunder_tol",
    [
        (600, 65, 12, 350, 80, 0.18, 0.04),
        (800, 45, 8, 250, 60, 0.12, 0.03),
        (1000, 30, 6, 180, 50, 0.08, 0.03),
    ],
)
def test_calibration_point_stats(
    elo,
    expected_median,
    median_tol,
    expected_p90,
    p90_tol,
    expected_blunder_rate,
    blunder_tol,
):
    samples = _sample_n(elo)
    median = statistics.median(samples)
    p90 = sorted(samples)[int(0.9 * len(samples))]
    blunder_rate = sum(1 for s in samples if s > 200) / len(samples)

    assert abs(median - expected_median) < median_tol, (
        f"ELO {elo}: median {median:.1f}, expected ~{expected_median}"
    )
    assert abs(p90 - expected_p90) < p90_tol, (
        f"ELO {elo}: p90 {p90:.1f}, expected ~{expected_p90}"
    )
    assert abs(blunder_rate - expected_blunder_rate) < blunder_tol, (
        f"ELO {elo}: blunder rate {blunder_rate:.3f}, expected ~{expected_blunder_rate}"
    )


# ------------------------------------------------------------------
# All samples are non-negative
# ------------------------------------------------------------------

@pytest.mark.parametrize("elo", [500, 600, 800, 1000, 1100])
def test_samples_non_negative(elo):
    samples = _sample_n(elo)
    assert all(s >= 0 for s in samples)


# ------------------------------------------------------------------
# Monotonicity: lower ELO → higher median loss
# ------------------------------------------------------------------

def test_monotonicity():
    medians = {}
    for elo in [600, 700, 800, 900, 1000]:
        medians[elo] = statistics.median(_sample_n(elo))

    for lo, hi in [(600, 700), (700, 800), (800, 900), (900, 1000)]:
        assert medians[lo] > medians[hi], (
            f"median at ELO {lo} ({medians[lo]:.1f}) should exceed "
            f"median at ELO {hi} ({medians[hi]:.1f})"
        )


# ------------------------------------------------------------------
# Clamping: below min and above max use boundary params
# ------------------------------------------------------------------

def test_clamp_below_min():
    mu_400, sigma_400 = get_params(400)
    mu_600, sigma_600 = get_params(600)
    assert mu_400 == mu_600
    assert sigma_400 == sigma_600


def test_clamp_above_max():
    mu_1100, sigma_1100 = get_params(1100)
    mu_1000, sigma_1000 = get_params(1000)
    assert mu_1100 == mu_1000
    assert sigma_1100 == sigma_1000


# ------------------------------------------------------------------
# Interpolation: midpoint ELO produces params between neighbours
# ------------------------------------------------------------------

def test_interpolation_midpoint():
    mu_600, sigma_600 = get_params(600)
    mu_800, sigma_800 = get_params(800)
    mu_700, sigma_700 = get_params(700)

    assert mu_800 < mu_700 < mu_600
    assert abs(mu_700 - (mu_600 + mu_800) / 2) < 1e-9


# ------------------------------------------------------------------
# Reproducibility via explicit RNG
# ------------------------------------------------------------------

def test_reproducibility():
    a = _sample_n(800, 100)
    b = _sample_n(800, 100)
    assert a == b


# ------------------------------------------------------------------
# Log-normal theoretical median matches exp(mu)
# ------------------------------------------------------------------

@pytest.mark.parametrize("elo", [600, 800, 1000])
def test_theoretical_median(elo):
    mu, _ = get_params(elo)
    theoretical = math.exp(mu)
    empirical = statistics.median(_sample_n(elo))
    # Allow 15% relative error from sampling noise
    assert abs(empirical - theoretical) / theoretical < 0.15
