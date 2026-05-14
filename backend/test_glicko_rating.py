from app.glicko import (
    CHESSCOM_INITIAL_RATING,
    INITIAL_RD,
    INITIAL_VOLATILITY,
    LICHESS_INITIAL_RATING,
    GlickoState,
    compute_chesscom_glicko,
    compute_lichess_glicko2,
)


def test_chesscom_glicko_win_increases_rating_and_lowers_rd():
    updated = compute_chesscom_glicko(
        GlickoState(CHESSCOM_INITIAL_RATING, INITIAL_RD),
        1200,
        "checkmate_win",
    )

    assert round(updated.rating) > CHESSCOM_INITIAL_RATING
    assert 30 <= updated.rd < INITIAL_RD
    assert updated.volatility is None


def test_lichess_glicko2_draw_against_equal_keeps_rating_near_initial():
    updated = compute_lichess_glicko2(
        GlickoState(LICHESS_INITIAL_RATING, INITIAL_RD, INITIAL_VOLATILITY),
        1500,
        "draw",
    )

    assert round(updated.rating) == LICHESS_INITIAL_RATING
    assert 30 <= updated.rd < INITIAL_RD
    assert updated.volatility is not None


def test_glicko_rejects_unrated_result():
    try:
        compute_chesscom_glicko(GlickoState(1200, 350), 1200, "abandon")
    except ValueError as exc:
        assert "Unrated result" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
