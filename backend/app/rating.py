"""Elo rating calculation for Ghost Replay."""

from __future__ import annotations

DEFAULT_RATING = 1200
PROVISIONAL_THRESHOLD = 20
K_PROVISIONAL = 40
K_STABLE = 20

# Map game result strings to Elo score values
RESULT_SCORES: dict[str, float] = {
    "checkmate_win": 1.0,
    "checkmate_loss": 0.0,
    "resign": 0.0,
    "draw": 0.5,
}


def expected_score(player_rating: int, opponent_rating: int) -> float:
    """Probability the player wins, given both ratings."""
    return 1.0 / (1.0 + 10.0 ** ((opponent_rating - player_rating) / 400.0))


def compute_new_rating(
    current_rating: int,
    opponent_rating: int,
    result: str,
    games_played: int,
) -> tuple[int, bool]:
    """Compute new Elo rating after a game.

    Args:
        current_rating: Player's current rating.
        opponent_rating: Opponent's engine_elo.
        result: Game result string (checkmate_win, checkmate_loss, resign, draw).
        games_played: Number of rated games played so far (before this game).

    Returns:
        Tuple of (new_rating, is_provisional).

    Raises:
        ValueError: If result is not a rated outcome.
    """
    score = RESULT_SCORES.get(result)
    if score is None:
        raise ValueError(f"Unrated result: {result!r}")

    is_provisional = games_played < PROVISIONAL_THRESHOLD
    k = K_PROVISIONAL if is_provisional else K_STABLE

    e = expected_score(current_rating, opponent_rating)
    new_rating = round(current_rating + k * (score - e))

    return new_rating, is_provisional
