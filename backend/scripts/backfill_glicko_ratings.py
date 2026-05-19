"""Backfill Glicko columns on rating_history.

Rerunnable by design: existing Elo columns are preserved, while Glicko columns
are recomputed from rated ended sessions in deterministic session order.
Pass --recompute-elo to rebuild Elo/games_played from that same order too.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import SessionLocal
from app.glicko import (  # noqa: E402
    CHESSCOM_INITIAL_RATING,
    INITIAL_RD,
    INITIAL_VOLATILITY,
    LICHESS_INITIAL_RATING,
    GlickoState,
    compute_chesscom_glicko,
    compute_lichess_glicko2,
)
from app.models import GameSession, RatingHistory  # noqa: E402
from app.rating import DEFAULT_RATING, RESULT_SCORES, compute_new_rating  # noqa: E402
from sqlalchemy import func  # noqa: E402


def backfill_glicko_ratings(db, *, recompute_elo: bool = False) -> tuple[int, int]:
    session_time = func.coalesce(GameSession.ended_at, GameSession.started_at)
    sessions = (
        db.query(GameSession)
        .filter(
            GameSession.status == "ended",
            GameSession.is_rated.is_(True),
            GameSession.result.in_(tuple(RESULT_SCORES.keys())),
            ((GameSession.session_mode == "normal") | (GameSession.drill_state == "converted")),
        )
        .order_by(
            GameSession.user_id.asc(),
            session_time.asc(),
            GameSession.id.asc(),
        )
        .all()
    )

    rows_by_session = {
        row.game_session_id: row
        for row in db.query(RatingHistory).all()
    }
    duplicate_counts = defaultdict(int)
    for row in db.query(RatingHistory.game_session_id).all():
        duplicate_counts[row.game_session_id] += 1
    duplicates = [session_id for session_id, count in duplicate_counts.items() if count > 1]
    if duplicates:
        sample = ", ".join(str(value) for value in duplicates[:10])
        raise RuntimeError(f"Duplicate rating_history game_session_id rows found: {sample}")

    user_state: dict[int, tuple[int, int, GlickoState, GlickoState]] = {}
    created = 0
    updated = 0

    for session in sessions:
        elo, games_played, chesscom_state, lichess_state = user_state.get(
            session.user_id,
            (
                DEFAULT_RATING,
                0,
                GlickoState(CHESSCOM_INITIAL_RATING, INITIAL_RD),
                GlickoState(LICHESS_INITIAL_RATING, INITIAL_RD, INITIAL_VOLATILITY),
            ),
        )

        computed_elo, computed_is_provisional = compute_new_rating(
            elo,
            session.engine_elo,
            session.result,
            games_played,
        )
        next_chesscom = compute_chesscom_glicko(chesscom_state, session.engine_elo, session.result)
        next_lichess = compute_lichess_glicko2(lichess_state, session.engine_elo, session.result)
        existing = rows_by_session.get(session.id)

        if existing is None:
            existing = RatingHistory(
                user_id=session.user_id,
                game_session_id=session.id,
                rating=computed_elo,
                is_provisional=computed_is_provisional,
                games_played=games_played + 1,
                recorded_at=session.ended_at or session.started_at,
            )
            db.add(existing)
            rows_by_session[session.id] = existing
            created += 1
        else:
            if recompute_elo:
                existing.rating = computed_elo
                existing.is_provisional = computed_is_provisional
                existing.games_played = games_played + 1
            updated += 1

        existing.chesscom_rating = next_chesscom.rating
        existing.chesscom_rd = next_chesscom.rd
        existing.lichess_rating = next_lichess.rating
        existing.lichess_rd = next_lichess.rd
        existing.lichess_volatility = next_lichess.volatility

        user_state[session.user_id] = (
            existing.rating,
            existing.games_played,
            next_chesscom,
            next_lichess,
        )

    db.commit()
    return created, updated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--recompute-elo",
        action="store_true",
        help="Also rebuild rating/is_provisional/games_played from ordered rated sessions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = SessionLocal()
    try:
        created, updated = backfill_glicko_ratings(db, recompute_elo=args.recompute_elo)
        print(f"Backfilled Glicko ratings: created={created} updated={updated}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
