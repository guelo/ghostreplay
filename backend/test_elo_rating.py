import pytest

from app.rating import (
    DEFAULT_RATING,
    K_PROVISIONAL,
    K_STABLE,
    PROVISIONAL_THRESHOLD,
    RESULT_SCORES,
    compute_new_rating,
    expected_score,
)


# --- expected_score ---


def test_expected_score_equal_ratings():
    assert expected_score(1200, 1200) == pytest.approx(0.5)


def test_expected_score_higher_rated_player():
    e = expected_score(1600, 1200)
    assert e > 0.5
    assert e == pytest.approx(0.9091, abs=1e-3)


def test_expected_score_lower_rated_player():
    e = expected_score(1200, 1600)
    assert e < 0.5
    assert e == pytest.approx(0.0909, abs=1e-3)


def test_expected_score_symmetry():
    """E(a,b) + E(b,a) must equal 1.0 for any pair."""
    for a, b in [(1200, 1200), (800, 1600), (2000, 1000), (1200, 1201)]:
        assert expected_score(a, b) + expected_score(b, a) == pytest.approx(1.0)


# --- compute_new_rating: basic outcomes ---


def test_win_increases_rating():
    new, _ = compute_new_rating(1200, 1200, "checkmate_win", games_played=0)
    assert new > 1200


def test_loss_decreases_rating():
    new, _ = compute_new_rating(1200, 1200, "checkmate_loss", games_played=0)
    assert new < 1200


def test_draw_vs_equal_no_change():
    new, _ = compute_new_rating(1200, 1200, "draw", games_played=0)
    assert new == 1200


def test_draw_vs_higher_gains():
    new, _ = compute_new_rating(1200, 1600, "draw", games_played=0)
    assert new > 1200


def test_draw_vs_lower_loses():
    new, _ = compute_new_rating(1600, 1200, "draw", games_played=0)
    assert new < 1600


# --- provisional vs stable K-factor ---


def test_provisional_k_factor():
    """K=40 when games_played < PROVISIONAL_THRESHOLD."""
    new_prov, is_prov = compute_new_rating(1200, 1200, "checkmate_win", games_played=0)
    assert is_prov is True
    # With equal ratings, expected = 0.5, so delta = K * (1.0 - 0.5) = K/2
    assert new_prov == 1200 + round(K_PROVISIONAL * 0.5)


def test_stable_k_factor():
    """K=20 when games_played >= PROVISIONAL_THRESHOLD."""
    new_stable, is_prov = compute_new_rating(
        1200, 1200, "checkmate_win", games_played=PROVISIONAL_THRESHOLD
    )
    assert is_prov is False
    assert new_stable == 1200 + round(K_STABLE * 0.5)


def test_provisional_boundary():
    """Exactly at threshold → stable; one below → provisional."""
    _, prov_below = compute_new_rating(1200, 1200, "draw", games_played=PROVISIONAL_THRESHOLD - 1)
    _, prov_at = compute_new_rating(1200, 1200, "draw", games_played=PROVISIONAL_THRESHOLD)
    assert prov_below is True
    assert prov_at is False


# --- edge cases ---


def test_invalid_result_raises():
    with pytest.raises(ValueError, match="Unrated result"):
        compute_new_rating(1200, 1200, "abandon", games_played=0)


def test_resign_is_rated_as_loss():
    new, _ = compute_new_rating(1200, 1200, "resign", games_played=0)
    loss, _ = compute_new_rating(1200, 1200, "checkmate_loss", games_played=0)
    assert new == loss


def test_result_is_rounded_integer():
    new, _ = compute_new_rating(1200, 1350, "checkmate_win", games_played=5)
    assert isinstance(new, int)


def test_all_result_scores_covered():
    """Every entry in RESULT_SCORES produces a valid rating."""
    for result in RESULT_SCORES:
        new, _ = compute_new_rating(1200, 1200, result, games_played=0)
        assert isinstance(new, int)


def test_large_rating_gap_win():
    """Beating a much higher-rated opponent gives a large gain."""
    new, _ = compute_new_rating(800, 2000, "checkmate_win", games_played=0)
    assert new - 800 > K_PROVISIONAL * 0.9  # expected score ≈ 0, so gain ≈ K


def test_large_rating_gap_loss():
    """Losing to a much lower-rated opponent gives a large drop."""
    new, _ = compute_new_rating(2000, 800, "checkmate_loss", games_played=0)
    assert 2000 - new > K_PROVISIONAL * 0.9
