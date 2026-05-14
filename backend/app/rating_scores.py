from __future__ import annotations

from app.glicko import (
    CHESSCOM_INITIAL_RATING,
    INITIAL_RD,
    INITIAL_VOLATILITY,
    LICHESS_INITIAL_RATING,
    GlickoState,
    compute_chesscom_glicko,
    compute_lichess_glicko2,
)
from app.models import RatingHistory
from app.rating import DEFAULT_RATING, compute_new_rating


def latest_rating_order(model=RatingHistory):
    return (model.recorded_at.desc(), model.games_played.desc(), model.id.desc())


def rating_score(rating: int, is_provisional: bool, rd: float | None = None, volatility: float | None = None) -> dict:
    data: dict[str, int | float | bool] = {
        "rating": rating,
        "is_provisional": is_provisional,
    }
    if rd is not None:
        data["rd"] = rd
    if volatility is not None:
        data["volatility"] = volatility
    return data


def scores_for_row(row: RatingHistory | None) -> dict:
    if row is None:
        return {
            "elo": rating_score(DEFAULT_RATING, True),
            "chesscom": None,
            "lichess": None,
        }
    return {
        "elo": rating_score(row.rating, row.is_provisional),
        "chesscom": None
        if row.chesscom_rating is None
        else rating_score(round(row.chesscom_rating), row.is_provisional, row.chesscom_rd),
        "lichess": None
        if row.lichess_rating is None
        else rating_score(round(row.lichess_rating), row.is_provisional, row.lichess_rd, row.lichess_volatility),
    }


def compute_rating_tracks(
    latest: RatingHistory | None,
    opponent_rating: int,
    result: str,
) -> tuple[int, bool, GlickoState | None, GlickoState | None]:
    current_elo = latest.rating if latest else DEFAULT_RATING
    games_played = latest.games_played if latest else 0
    new_elo, is_provisional = compute_new_rating(current_elo, opponent_rating, result, games_played)

    chesscom = None
    if latest is None or latest.chesscom_rating is not None:
        chesscom_current = GlickoState(
            rating=latest.chesscom_rating if latest and latest.chesscom_rating is not None else CHESSCOM_INITIAL_RATING,
            rd=latest.chesscom_rd if latest and latest.chesscom_rd is not None else INITIAL_RD,
        )
        chesscom = compute_chesscom_glicko(chesscom_current, opponent_rating, result)

    lichess = None
    if latest is None or latest.lichess_rating is not None:
        lichess_current = GlickoState(
            rating=latest.lichess_rating if latest and latest.lichess_rating is not None else LICHESS_INITIAL_RATING,
            rd=latest.lichess_rd if latest and latest.lichess_rd is not None else INITIAL_RD,
            volatility=latest.lichess_volatility if latest and latest.lichess_volatility is not None else INITIAL_VOLATILITY,
        )
        lichess = compute_lichess_glicko2(lichess_current, opponent_rating, result)

    return (
        new_elo,
        is_provisional,
        chesscom,
        lichess,
    )
