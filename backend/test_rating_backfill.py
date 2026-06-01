from datetime import datetime, timedelta, timezone
import uuid

from app.models import GameSession, RatingHistory
from app.rating import DEFAULT_RATING, compute_new_rating
from scripts.backfill_glicko_ratings import backfill_glicko_ratings


def _end_session(db_session, session_id: str, *, result: str, ended_at: datetime | None) -> None:
    session = db_session.get(GameSession, uuid.UUID(session_id))
    session.status = "ended"
    session.result = result
    session.ended_at = ended_at
    session.is_rated = True
    db_session.commit()


def test_backfill_preserves_existing_elo_and_recomputes_glicko(create_game_session, db_session):
    now = datetime.now(timezone.utc)
    session_id = create_game_session(user_id=123)
    _end_session(db_session, session_id, result="checkmate_win", ended_at=now)
    row = RatingHistory(
        user_id=123,
        game_session_id=uuid.UUID(session_id),
        rating=1321,
        is_provisional=False,
        games_played=27,
        chesscom_rating=900,
        chesscom_rd=350,
        recorded_at=now,
    )
    db_session.add(row)
    db_session.commit()

    created, updated = backfill_glicko_ratings(db_session)
    db_session.refresh(row)
    first_chesscom = row.chesscom_rating

    assert (created, updated) == (0, 1)
    assert row.rating == 1321
    assert row.games_played == 27
    assert row.is_provisional is False
    assert row.chesscom_rating != 900
    assert row.chesscom_rd is not None
    assert row.lichess_rating is not None

    created_again, updated_again = backfill_glicko_ratings(db_session)
    db_session.refresh(row)

    assert (created_again, updated_again) == (0, 1)
    assert row.chesscom_rating == first_chesscom


def test_backfill_can_recompute_reset_existing_elo_sequence(create_game_session, db_session):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = create_game_session(user_id=123)
    second = create_game_session(user_id=123)
    second_session = db_session.get(GameSession, uuid.UUID(second))
    second_session.engine_elo = 800
    db_session.commit()
    _end_session(db_session, first, result="resign", ended_at=base)
    _end_session(db_session, second, result="checkmate_win", ended_at=base + timedelta(days=1))

    first_elo, first_provisional = compute_new_rating(DEFAULT_RATING, 1500, "resign", 0)
    reset_elo, _ = compute_new_rating(DEFAULT_RATING, 800, "checkmate_win", 0)
    second_elo, second_provisional = compute_new_rating(first_elo, 800, "checkmate_win", 1)

    db_session.add_all(
        [
            RatingHistory(
                user_id=123,
                game_session_id=uuid.UUID(first),
                rating=first_elo,
                is_provisional=first_provisional,
                games_played=1,
                recorded_at=base,
            ),
            RatingHistory(
                user_id=123,
                game_session_id=uuid.UUID(second),
                rating=reset_elo,
                is_provisional=True,
                games_played=1,
                recorded_at=base + timedelta(days=1),
            ),
        ]
    )
    db_session.commit()

    created, updated = backfill_glicko_ratings(db_session, recompute_elo=True)

    rows = {
        str(row.game_session_id): row
        for row in db_session.query(RatingHistory).order_by(RatingHistory.recorded_at).all()
    }
    assert (created, updated) == (0, 2)
    assert rows[first].rating == first_elo
    assert rows[first].games_played == 1
    assert rows[second].rating == second_elo
    assert rows[second].rating != reset_elo
    assert rows[second].games_played == 2
    assert rows[second].is_provisional is second_provisional
    assert rows[second].lichess_rating is not None


def test_backfill_creates_missing_rating_history_rows(create_game_session, db_session):
    now = datetime.now(timezone.utc)
    session_id = create_game_session(user_id=123)
    _end_session(
        db_session,
        session_id,
        result="draw",
        ended_at=now + timedelta(minutes=1),
    )

    created, updated = backfill_glicko_ratings(db_session)

    row = db_session.query(RatingHistory).filter(RatingHistory.game_session_id == uuid.UUID(session_id)).one()
    assert (created, updated) == (1, 0)
    assert row.rating is not None
    assert row.games_played == 1
    assert row.chesscom_rating is not None
    assert row.lichess_volatility is not None


def test_backfill_orders_null_ended_at_by_started_at(create_game_session, db_session):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = create_game_session(user_id=123)
    middle_null_ended = create_game_session(user_id=123)
    last = create_game_session(user_id=123)

    first_session = db_session.get(GameSession, uuid.UUID(first))
    middle_session = db_session.get(GameSession, uuid.UUID(middle_null_ended))
    last_session = db_session.get(GameSession, uuid.UUID(last))
    first_session.started_at = base
    middle_session.started_at = base + timedelta(days=1)
    last_session.started_at = base + timedelta(days=2)
    db_session.commit()

    _end_session(db_session, first, result="checkmate_win", ended_at=base)
    _end_session(db_session, middle_null_ended, result="checkmate_loss", ended_at=None)
    _end_session(db_session, last, result="draw", ended_at=base + timedelta(days=2))

    backfill_glicko_ratings(db_session)

    rows = {
        str(row.game_session_id): row
        for row in db_session.query(RatingHistory).all()
    }
    assert rows[first].games_played == 1
    assert rows[middle_null_ended].games_played == 2
    assert rows[last].games_played == 3


def _make_drill(db_session, session_id, *, drill_state, is_rated, result, ended_at):
    session = db_session.get(GameSession, uuid.UUID(session_id))
    session.session_mode = "drill"
    session.drill_state = drill_state
    session.status = "ended"
    session.result = result
    session.ended_at = ended_at
    session.is_rated = is_rated
    if drill_state == "converted":
        session.normal_started_at = session.started_at
        session.converted_at = session.started_at
        session.rated_start_ply = 0
    db_session.commit()


def test_backfill_ignores_uncontinued_drills_and_includes_converted(
    create_game_session, db_session
):
    # Amended drill policy (2026-06-01): an uncontinued drill stays unrated and must
    # never receive a RatingHistory row, while a converted drill is one full normal
    # game and is backfilled like any rated session.
    now = datetime.now(timezone.utc)
    converted = create_game_session(user_id=123)
    uncontinued = create_game_session(user_id=123)
    _make_drill(
        db_session, converted,
        drill_state="converted", is_rated=True, result="checkmate_win", ended_at=now,
    )
    _make_drill(
        db_session, uncontinued,
        drill_state="active", is_rated=False, result="checkmate_loss",
        ended_at=now + timedelta(minutes=1),
    )

    backfill_glicko_ratings(db_session)

    rows = {
        str(row.game_session_id): row
        for row in db_session.query(RatingHistory).all()
    }
    assert converted in rows
    assert uncontinued not in rows
