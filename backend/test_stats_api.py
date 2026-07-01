import uuid
from datetime import datetime, timedelta, timezone

from app.models import (
    Blunder,
    BlunderReview,
    GameSession,
    OpeningScoreBatch,
    Position,
    UserOpeningScore,
)


def _end_game(client, auth_headers, session_id, user_id=123, result="checkmate_win", pgn="1. e4 e5"):
    response = client.post(
        "/api/game/end",
        json={"session_id": session_id, "result": result, "pgn": pgn},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _upload_moves(client, auth_headers, session_id, moves, user_id=123):
    response = client.post(
        f"/api/session/{session_id}/moves",
        json={"moves": moves},
        headers=auth_headers(user_id=user_id),
    )
    assert response.status_code == 200


def _set_session_times(
    db_session,
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime | None,
    normal_started_at: datetime | None = None,
):
    session = db_session.query(GameSession).filter(GameSession.id == uuid.UUID(session_id)).first()
    assert session is not None
    session.started_at = started_at
    session.ended_at = ended_at
    if normal_started_at is not None:
        session.normal_started_at = normal_started_at
        session.converted_at = normal_started_at
        session.rated_start_ply = 0
        session.session_mode = "drill"
        session.drill_state = "converted"
        session.is_rated = True
    db_session.commit()


def _add_position(db_session, *, user_id: int, tag: str, active_color: str = "white") -> Position:
    position = Position(
        user_id=user_id,
        fen_hash=f"hash-{tag}",
        fen_raw=f"fen-{tag}",
        active_color=active_color,
    )
    db_session.add(position)
    db_session.flush()
    return position


def _add_blunder(
    db_session,
    *,
    user_id: int,
    position: Position,
    eval_loss_cp: int = 200,
    pass_streak: int = 0,
    last_reviewed_at: datetime | None = None,
    created_at: datetime | None = None,
    bad_move_san: str = "Qxh7+",
    best_move_san: str = "Re1",
) -> Blunder:
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san=bad_move_san,
        best_move_san=best_move_san,
        eval_loss_cp=eval_loss_cp,
        pass_streak=pass_streak,
        last_reviewed_at=last_reviewed_at,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db_session.add(blunder)
    db_session.flush()
    return blunder


def _add_review(
    db_session,
    *,
    blunder: Blunder,
    session_id: str,
    reviewed_at: datetime,
    passed: bool,
    pass_streak_after: int,
):
    db_session.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=uuid.UUID(session_id),
            reviewed_at=reviewed_at,
            passed=passed,
            move_played_san="Re1" if passed else "Qxh7+",
            eval_delta_cp=0 if passed else 200,
            pass_streak_after=pass_streak_after,
        )
    )


def _seed_opening(
    db_session,
    *,
    batch: OpeningScoreBatch,
    user_id: int,
    player_color: str,
    opening_name: str,
    opening_score: float,
    sample_size: int,
    game_count: int = 3,
):
    db_session.add(
        UserOpeningScore(
            batch_id=batch.id,
            user_id=user_id,
            player_color=player_color,
            opening_key=f"{player_color}:{opening_name}",
            opening_name=opening_name,
            opening_family=opening_name,
            opening_score=opening_score,
            confidence=0.8,
            coverage=0.5,
            weighted_depth=6.0,
            sample_size=sample_size,
            game_count=game_count,
        )
    )


def test_stats_summary_empty_dataset(client, auth_headers):
    response = client.get("/api/stats/summary", headers=auth_headers(user_id=123))
    assert response.status_code == 200

    data = response.json()
    assert data["window_days"] == 30
    assert data["games"] == {
        "played": 0,
        "score_pct": None,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "avg_moves": 0.0,
    }
    assert data["moves"] == {
        "accuracy_pct": None,
        "mistake_free_game_rate": None,
        "quality_distribution": None,
    }
    assert data["colors"]["white"] == {"games": 0, "score_pct": None, "accuracy_pct": None}
    assert data["colors"]["black"] == {"games": 0, "score_pct": None, "accuracy_pct": None}
    assert data["training"] == {
        "retention_pct": None,
        "reviewed_blunders": 0,
        "retained_blunders": 0,
        "review_pass_rate": None,
        "reviews_total": 0,
        "reviews_passed": 0,
        "conversions_in_window": 0,
        "mastery_threshold": 3,
    }
    assert data["library"] == {
        "blunders_total": 0,
        "new_blunders_in_window": 0,
        "avg_blunder_eval_loss_cp": 0,
        "top_costly_blunders": [],
    }
    assert data["openings"] == {"strongest": [], "weakest": []}
    # Removed fields must not reappear.
    assert "achievements" not in data
    assert "data_completeness" not in data
    assert "positions_total" not in data["library"]
    assert "edges_total" not in data["library"]


def test_stats_summary_score_pct_folds_resign_and_abandon(
    client, auth_headers, create_game_session
):
    # White: win + checkmate_loss + resign; Black: draw + abandon.
    white_win = create_game_session(user_id=123, player_color="white")
    white_loss = create_game_session(user_id=123, player_color="white")
    white_resign = create_game_session(user_id=123, player_color="white")
    black_draw = create_game_session(user_id=123, player_color="black")
    black_abandon = create_game_session(user_id=123, player_color="black")

    _end_game(client, auth_headers, white_win, result="checkmate_win")
    _end_game(client, auth_headers, white_loss, result="checkmate_loss")
    _end_game(client, auth_headers, white_resign, result="resign")
    _end_game(client, auth_headers, black_draw, result="draw")
    _end_game(client, auth_headers, black_abandon, result="abandon")

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    # Resigns and abandons fold into losses; W-L-D sums to decided games.
    assert data["games"]["played"] == 5
    assert data["games"]["wins"] == 1
    assert data["games"]["losses"] == 3
    assert data["games"]["draws"] == 1
    # (1 + 0.5*1) / 5 * 100 = 30.0
    assert data["games"]["score_pct"] == 30.0

    # White: 1 win, 2 losses (checkmate_loss + resign) -> (1)/3*100 = 33.3
    assert data["colors"]["white"]["games"] == 3
    assert data["colors"]["white"]["score_pct"] == 33.3
    # Black: abandon folds into losses -> (0 + 0.5) / 2 * 100 = 25.0
    assert data["colors"]["black"]["games"] == 2
    assert data["colors"]["black"]["score_pct"] == 25.0


def test_stats_summary_quality_and_mistake_free_excludes_active(
    client, auth_headers, create_game_session
):
    clean_ended = create_game_session(user_id=123, player_color="white")
    blunder_ended = create_game_session(user_id=123, player_color="white")
    active_clean = create_game_session(user_id=123, player_color="white")

    _end_game(client, auth_headers, clean_ended, result="checkmate_win")
    _end_game(client, auth_headers, blunder_ended, result="checkmate_loss")

    # Clean ended game: player (white) best move; opponent (black) blunder must
    # NOT dirty the game.
    _upload_moves(client, auth_headers, clean_ended, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "c1", "classification": "best"},
        {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "c2", "classification": "blunder"},
    ])
    # Ended game with a player blunder.
    _upload_moves(client, auth_headers, blunder_ended, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "b1", "classification": "blunder"},
    ])
    # Active game (clean) — must not inflate the mistake-free denominator, but its
    # player move still counts toward the quality distribution.
    _upload_moves(client, auth_headers, active_clean, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "a1", "classification": "best"},
    ])

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    # 3 classified player moves (2 best, 1 blunder); only ended games count for
    # mistake-free: 2 ended, 1 clean -> 50.0 (NOT 66.7, which would include the
    # active clean game).
    assert data["moves"]["mistake_free_game_rate"] == 50.0
    assert data["moves"]["quality_distribution"] == {
        "inaccuracy": 0.0,
        "mistake": 0.0,
        "blunder": 33.3,
    }


def test_stats_summary_accuracy_over_ended_games(
    client, auth_headers, create_game_session
):
    # White game with a flat white-relative eval (15 cp throughout) -> accuracy 100.
    white_game = create_game_session(user_id=123, player_color="white")
    # Ended game with no evals -> accuracy None -> excluded from the average.
    empty_ended = create_game_session(user_id=123, player_color="white")
    # Active game with evals -> excluded (accuracy is over ended games only).
    active_game = create_game_session(user_id=123, player_color="white")

    _end_game(client, auth_headers, white_game, result="checkmate_win")
    _end_game(client, auth_headers, empty_ended, result="draw")

    _upload_moves(client, auth_headers, white_game, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "w1", "eval_cp": 15},
        {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "w2", "eval_cp": -15},
    ])
    _upload_moves(client, auth_headers, active_game, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "x1", "eval_cp": 15},
        {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "x2", "eval_cp": -15},
    ])

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    assert data["moves"]["accuracy_pct"] == 100.0
    assert data["colors"]["white"]["accuracy_pct"] == 100.0
    assert data["colors"]["black"]["accuracy_pct"] is None


def test_stats_summary_training_retention_pass_rate_and_conversions(
    client, auth_headers, create_game_session, db_session
):
    now = datetime.now(timezone.utc)
    session_id = create_game_session(user_id=123, player_color="white")

    p1 = _add_position(db_session, user_id=123, tag="t1")
    p2 = _add_position(db_session, user_id=123, tag="t2", active_color="black")
    p3 = _add_position(db_session, user_id=123, tag="t3")
    p4 = _add_position(db_session, user_id=123, tag="t4", active_color="black")

    # Retention (all-time) is driven by Blunder.pass_streak / last_reviewed_at.
    b1 = _add_blunder(db_session, user_id=123, position=p1, pass_streak=3, last_reviewed_at=now)
    b2 = _add_blunder(db_session, user_id=123, position=p2, pass_streak=1, last_reviewed_at=now)
    b3 = _add_blunder(db_session, user_id=123, position=p3, pass_streak=0, last_reviewed_at=now)
    _add_blunder(db_session, user_id=123, position=p4, pass_streak=0, last_reviewed_at=None)

    recent = now - timedelta(days=2)
    old = now - timedelta(days=40)

    # b1: three passing reviews within the window, crossing to mastery (=3).
    _add_review(db_session, blunder=b1, session_id=session_id, reviewed_at=recent, passed=True, pass_streak_after=1)
    _add_review(db_session, blunder=b1, session_id=session_id, reviewed_at=recent, passed=True, pass_streak_after=2)
    _add_review(db_session, blunder=b1, session_id=session_id, reviewed_at=recent, passed=True, pass_streak_after=3)
    # b2: single passing review within the window.
    _add_review(db_session, blunder=b2, session_id=session_id, reviewed_at=recent, passed=True, pass_streak_after=1)
    # b3: mastered long ago (outside the window), then failed recently.
    _add_review(db_session, blunder=b3, session_id=session_id, reviewed_at=old, passed=True, pass_streak_after=1)
    _add_review(db_session, blunder=b3, session_id=session_id, reviewed_at=old, passed=True, pass_streak_after=2)
    _add_review(db_session, blunder=b3, session_id=session_id, reviewed_at=old, passed=True, pass_streak_after=3)
    _add_review(db_session, blunder=b3, session_id=session_id, reviewed_at=recent, passed=False, pass_streak_after=0)
    db_session.commit()

    data_30 = client.get(
        "/api/stats/summary?window_days=30", headers=auth_headers(user_id=123)
    ).json()["training"]

    # Retention is all-time: 3 reviewed (last_reviewed_at set), 2 retained (streak>=1).
    assert data_30["reviewed_blunders"] == 3
    assert data_30["retained_blunders"] == 2
    assert data_30["retention_pct"] == 66.7
    # In-window reviews: b1 (3 pass) + b2 (1 pass) + b3 (1 fail) = 5 total, 4 passed.
    assert data_30["reviews_total"] == 5
    assert data_30["reviews_passed"] == 4
    assert data_30["review_pass_rate"] == 80.0
    # Only b1 crossed to mastery inside the window; b3's crossing is old.
    assert data_30["conversions_in_window"] == 1
    assert data_30["mastery_threshold"] == 3

    data_all = client.get(
        "/api/stats/summary?window_days=0", headers=auth_headers(user_id=123)
    ).json()["training"]

    # All-time widens the review window: b3's old crossing now counts too.
    assert data_all["reviews_total"] == 8
    assert data_all["reviews_passed"] == 7
    assert data_all["review_pass_rate"] == 87.5
    assert data_all["conversions_in_window"] == 2
    # Retention is unaffected by the window.
    assert data_all["reviewed_blunders"] == 3
    assert data_all["retained_blunders"] == 2


def test_stats_summary_training_is_user_scoped(
    client, auth_headers, create_game_session, db_session
):
    now = datetime.now(timezone.utc)
    my_session = create_game_session(user_id=123, player_color="white")
    other_session = create_game_session(user_id=999, player_color="white")

    mine_pos = _add_position(db_session, user_id=123, tag="mine")
    other_pos = _add_position(db_session, user_id=999, tag="other")

    mine = _add_blunder(db_session, user_id=123, position=mine_pos, pass_streak=1, last_reviewed_at=now)
    theirs = _add_blunder(db_session, user_id=999, position=other_pos, pass_streak=3, last_reviewed_at=now)

    recent = now - timedelta(days=1)
    _add_review(db_session, blunder=mine, session_id=my_session, reviewed_at=recent, passed=True, pass_streak_after=1)
    # Another user's mastery crossing — must never leak into user 123's stats.
    _add_review(db_session, blunder=theirs, session_id=other_session, reviewed_at=recent, passed=True, pass_streak_after=3)
    db_session.commit()

    training = client.get(
        "/api/stats/summary", headers=auth_headers(user_id=123)
    ).json()["training"]

    assert training["reviewed_blunders"] == 1
    assert training["retained_blunders"] == 1
    assert training["reviews_total"] == 1
    assert training["reviews_passed"] == 1
    assert training["conversions_in_window"] == 0


def test_stats_summary_openings_strongest_and_weakest(
    client, auth_headers, db_session
):
    now = datetime.now(timezone.utc)
    white_batch = OpeningScoreBatch(
        user_id=123, player_color="white", generation=1, computed_at=now
    )
    black_batch = OpeningScoreBatch(
        user_id=123, player_color="black", generation=1, computed_at=now
    )
    db_session.add_all([white_batch, black_batch])
    db_session.flush()

    _seed_opening(db_session, batch=white_batch, user_id=123, player_color="white",
                  opening_name="Ruy Lopez", opening_score=0.9, sample_size=10)
    _seed_opening(db_session, batch=white_batch, user_id=123, player_color="white",
                  opening_name="Italian", opening_score=0.5, sample_size=5)
    _seed_opening(db_session, batch=white_batch, user_id=123, player_color="white",
                  opening_name="Scotch", opening_score=0.2, sample_size=4)
    # Below the sample_size >= 3 noise floor — excluded.
    _seed_opening(db_session, batch=white_batch, user_id=123, player_color="white",
                  opening_name="Noise", opening_score=0.99, sample_size=2)
    _seed_opening(db_session, batch=black_batch, user_id=123, player_color="black",
                  opening_name="Sicilian", opening_score=0.1, sample_size=8)
    _seed_opening(db_session, batch=black_batch, user_id=123, player_color="black",
                  opening_name="Caro-Kann", opening_score=0.7, sample_size=6)
    db_session.commit()

    openings = client.get(
        "/api/stats/summary", headers=auth_headers(user_id=123)
    ).json()["openings"]

    strongest_names = [o["opening_name"] for o in openings["strongest"]]
    weakest_names = [o["opening_name"] for o in openings["weakest"]]

    assert strongest_names == ["Ruy Lopez", "Caro-Kann", "Italian"]
    assert weakest_names == ["Sicilian", "Scotch", "Italian"]
    assert "Noise" not in strongest_names
    assert "Noise" not in weakest_names
    assert openings["strongest"][0]["player_color"] == "white"
    assert openings["strongest"][0]["sample_size"] == 10


def test_stats_summary_null_pcts_when_no_decided_games(
    client, auth_headers, create_game_session
):
    # A single active (never-ended) game: it counts as played, but nothing is
    # decided and there are no moves, so every rate is null (never 0.0).
    create_game_session(user_id=123, player_color="white")

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    assert data["games"]["played"] == 1
    assert data["games"]["score_pct"] is None
    assert data["moves"]["accuracy_pct"] is None
    assert data["moves"]["mistake_free_game_rate"] is None
    assert data["moves"]["quality_distribution"] is None
    assert data["colors"]["white"]["score_pct"] is None
    assert data["colors"]["white"]["accuracy_pct"] is None


def test_stats_summary_quality_zero_bucket_is_not_null(
    client, auth_headers, create_game_session
):
    # A game with classified player moves but no blunders: the blunder bucket is a
    # legitimate 0.0, and the distribution object is present (not null).
    session_id = create_game_session(user_id=123, player_color="white")
    _end_game(client, auth_headers, session_id, result="checkmate_win")
    _upload_moves(client, auth_headers, session_id, [
        {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "q1", "classification": "best"},
        {"move_number": 2, "color": "white", "move_san": "Nf3", "fen_after": "q2", "classification": "good"},
    ])

    data = client.get("/api/stats/summary", headers=auth_headers(user_id=123)).json()

    assert data["moves"]["quality_distribution"] == {
        "inaccuracy": 0.0,
        "mistake": 0.0,
        "blunder": 0.0,
    }
    # Clean game -> mistake-free rate is a real 100.0.
    assert data["moves"]["mistake_free_game_rate"] == 100.0


def test_stats_summary_library_all_time_and_window(
    client, auth_headers, db_session
):
    now = datetime.now(timezone.utc)
    p1 = _add_position(db_session, user_id=123, tag="lib1")
    p2 = _add_position(db_session, user_id=123, tag="lib2", active_color="black")
    p3 = _add_position(db_session, user_id=999, tag="lib3")

    _add_blunder(db_session, user_id=123, position=p1, eval_loss_cp=300,
                 created_at=now - timedelta(days=2), bad_move_san="Qxh7+", best_move_san="Re1")
    _add_blunder(db_session, user_id=123, position=p2, eval_loss_cp=150,
                 created_at=now - timedelta(days=40), bad_move_san="Bxf7+", best_move_san="O-O")
    # Another user's blunder must not leak.
    _add_blunder(db_session, user_id=999, position=p3, eval_loss_cp=999,
                 created_at=now - timedelta(days=1), bad_move_san="Qh5", best_move_san="Nc3")
    db_session.commit()

    data = client.get(
        "/api/stats/summary?window_days=30", headers=auth_headers(user_id=123)
    ).json()["library"]

    # blunders_total / avg / top are all-time (window-independent).
    assert data["blunders_total"] == 2
    assert data["avg_blunder_eval_loss_cp"] == 225
    assert [b["eval_loss_cp"] for b in data["top_costly_blunders"]] == [300, 150]
    # new_blunders_in_window respects the 30d window.
    assert data["new_blunders_in_window"] == 1

    data_all = client.get(
        "/api/stats/summary?window_days=0", headers=auth_headers(user_id=123)
    ).json()["library"]
    assert data_all["new_blunders_in_window"] == 2


def test_stats_summary_window_filtering(client, auth_headers, create_game_session, db_session):
    now = datetime.now(timezone.utc)

    old_session = create_game_session(user_id=123, player_color="white")
    recent_session = create_game_session(user_id=123, player_color="black")
    recent_active = create_game_session(user_id=123, player_color="white")

    _end_game(client, auth_headers, old_session, user_id=123, result="draw")
    _end_game(client, auth_headers, recent_session, user_id=123, result="resign")

    _set_session_times(
        db_session,
        old_session,
        started_at=now - timedelta(days=40),
        ended_at=now - timedelta(days=39, hours=23),
    )
    _set_session_times(
        db_session,
        recent_session,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2, minutes=-30),
    )
    _set_session_times(
        db_session,
        recent_active,
        started_at=now - timedelta(days=1),
        ended_at=None,
    )

    response_30 = client.get(
        "/api/stats/summary?window_days=30",
        headers=auth_headers(user_id=123),
    )
    assert response_30.status_code == 200
    data_30 = response_30.json()
    # In-window: recent_session (resign -> loss) + recent_active (no result).
    assert data_30["games"]["played"] == 2
    assert data_30["games"]["wins"] == 0
    assert data_30["games"]["losses"] == 1
    assert data_30["games"]["draws"] == 0
    assert data_30["games"]["score_pct"] == 0.0

    response_all = client.get(
        "/api/stats/summary?window_days=0",
        headers=auth_headers(user_id=123),
    )
    assert response_all.status_code == 200
    data_all = response_all.json()
    assert data_all["games"]["played"] == 3
    assert data_all["games"]["draws"] == 1
    # win=0, loss=1 (resign), draw=1 -> (0 + 0.5)/2 * 100 = 25.0
    assert data_all["games"]["score_pct"] == 25.0


def test_stats_summary_converted_drill_anchors_window_to_started_at(
    client, auth_headers, create_game_session, db_session
):
    # Amended drill policy (2026-06-01): a converted drill is one full normal game
    # anchored to its actual started_at, not conversion time. A drill STARTED
    # outside the window but CONVERTED inside it must NOT count.
    now = datetime.now(timezone.utc)
    session_id = create_game_session(user_id=123, player_color="white")
    _end_game(client, auth_headers, session_id, user_id=123, result="draw")
    normal_started_at = now - timedelta(days=2)
    _set_session_times(
        db_session,
        session_id,
        started_at=now - timedelta(days=40),
        normal_started_at=normal_started_at,
        ended_at=normal_started_at + timedelta(minutes=12),
    )

    response = client.get(
        "/api/stats/summary?window_days=30",
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    assert response.json()["games"]["played"] == 0


def _convert_to_drill(db_session, session_id, rated_start_ply):
    from sqlalchemy import text

    db_session.execute(
        text("""
            UPDATE game_sessions
            SET session_mode = 'drill',
                drill_state = 'converted',
                is_rated = true,
                normal_started_at = started_at,
                converted_at = started_at,
                rated_start_ply = :rsp
            WHERE id = :sid
        """),
        {"sid": session_id, "rsp": rated_start_ply},
    )
    db_session.commit()


def test_stats_summary_converted_drill_move_metrics_include_drill_prefix(
    client, auth_headers, create_game_session, db_session
):
    # Amended drill policy (2026-06-01): a converted drill is one full normal game,
    # so move-quality metrics span the full line — including the pre-continue
    # drill-prefix moves — not only the moves after continue.
    session_id = create_game_session(user_id=123, player_color="white")
    _end_game(client, auth_headers, session_id, user_id=123, result="draw")
    # ply boundary 2: move 1 white is drill-prefix, move 2 white is normal.
    _convert_to_drill(db_session, session_id, rated_start_ply=2)

    _upload_moves(client, auth_headers, session_id, [
        {
            # Drill-prefix blunder — must still count toward move metrics.
            "move_number": 1, "color": "white", "move_san": "e4",
            "fen_after": "fen-1w", "eval_delta": 200, "classification": "blunder",
        },
        {
            # Normal-segment mistake.
            "move_number": 2, "color": "white", "move_san": "Nf3",
            "fen_after": "fen-2w", "eval_delta": 40, "classification": "mistake",
        },
    ])

    response = client.get(
        "/api/stats/summary?window_days=0",
        headers=auth_headers(user_id=123),
    )

    assert response.status_code == 200
    moves = response.json()["moves"]
    # Both player moves are classified (drill-prefix blunder + normal mistake).
    assert moves["quality_distribution"] == {
        "inaccuracy": 0.0,
        "mistake": 50.0,
        "blunder": 50.0,
    }
    # The single ended game contains a blunder -> not clean.
    assert moves["mistake_free_game_rate"] == 0.0


def test_stats_achievements_endpoint_all_time_and_player_only(
    client, auth_headers, create_game_session, db_session
):
    now = datetime.now(timezone.utc)
    old_session = create_game_session(user_id=123, player_color="white")
    recent_session = create_game_session(user_id=123, player_color="black")
    other_user_session = create_game_session(user_id=999, player_color="white")

    _set_session_times(
        db_session,
        old_session,
        started_at=now - timedelta(days=60),
        ended_at=now - timedelta(days=60, minutes=-20),
    )
    _set_session_times(
        db_session,
        recent_session,
        started_at=now - timedelta(days=2),
        ended_at=now - timedelta(days=2, minutes=-20),
    )

    _upload_moves(
        client,
        auth_headers,
        old_session,
        [
            {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "a", "classification": "best"},
            {"move_number": 1, "color": "black", "move_san": "e5", "fen_after": "b", "classification": "best"},
            {"move_number": 2, "color": "white", "move_san": "Nf3", "fen_after": "c", "classification": None},
            {"move_number": 2, "color": "black", "move_san": "Nc6", "fen_after": "d", "classification": "blunder"},
            {"move_number": 3, "color": "white", "move_san": "Bb5", "fen_after": "e", "classification": "best"},
            {"move_number": 4, "color": "white", "move_san": "O-O", "fen_after": "f", "classification": "best"},
            {"move_number": 5, "color": "white", "move_san": "Re1", "fen_after": "g", "classification": "good"},
            {"move_number": 6, "color": "white", "move_san": "c3", "fen_after": "h", "classification": "best"},
        ],
    )
    _upload_moves(
        client,
        auth_headers,
        other_user_session,
        [
            {"move_number": 1, "color": "white", "move_san": "e4", "fen_after": "m", "classification": "best"},
            {"move_number": 2, "color": "white", "move_san": "Nf3", "fen_after": "n", "classification": "best"},
            {"move_number": 3, "color": "white", "move_san": "Bc4", "fen_after": "o", "classification": "best"},
        ],
        user_id=999,
    )

    response = client.get("/api/stats/achievements", headers=auth_headers(user_id=123))
    assert response.status_code == 200
    # Player-only, all-time best streak (3 consecutive best on white in old_session).
    assert response.json()["perfect_streak"]["personal_best"] == 3


def test_stats_summary_window_days_validation(client, auth_headers):
    response = client.get("/api/stats/summary?window_days=31", headers=auth_headers(user_id=123))
    assert response.status_code == 422
