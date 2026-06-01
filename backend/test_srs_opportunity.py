from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.session import _compute_blunder_opportunity_events
from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderReview,
    GameSession,
    Move,
    Position,
    SessionMove,
)
from app.srs_math import (
    calculate_opportunity_overdue,
    compute_p_reach,
    expected_opportunities,
)
from app.srs_opportunity import load_opportunity_counters


def _session(
    db_session,
    *,
    user_id: int,
    player_color: str = "white",
    started_at: datetime | None = None,
    normal_started_at: datetime | None = None,
    converted: bool = False,
) -> GameSession:
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=started_at or datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color=player_color,
    )
    if converted:
        game_session.session_mode = "drill"
        game_session.drill_state = "converted"
        game_session.is_rated = True
        game_session.normal_started_at = normal_started_at or game_session.started_at
        game_session.converted_at = game_session.normal_started_at
        game_session.rated_start_ply = 0
    db_session.add(game_session)
    db_session.flush()
    return game_session


def _position(db_session, *, user_id: int, fen: str, active_color: str) -> Position:
    position = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen),
        fen_raw=fen,
        active_color=active_color,
    )
    db_session.add(position)
    db_session.flush()
    return position


def _blunder(db_session, *, user_id: int, position: Position, eval_loss_cp: int = 200) -> Blunder:
    blunder = Blunder(
        user_id=user_id,
        position_id=position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=eval_loss_cp,
        created_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(blunder)
    db_session.flush()
    return blunder


def _opportunity_event(
    db_session,
    *,
    user_id: int,
    blunder: Blunder,
    opportunity: bool,
    reached: bool,
    occurred_at: datetime,
) -> None:
    game_session = _session(db_session, user_id=user_id, started_at=occurred_at)
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=game_session.id,
            occurred_at=occurred_at,
            opportunity=opportunity,
            reached=reached,
        )
    )


def test_opportunity_math_smoothed_and_bounded():
    assert expected_opportunities(0) == 1.0
    assert expected_opportunities(3) == 8.0
    assert calculate_opportunity_overdue(opportunities_since_review=4, pass_streak=2) == pytest.approx(1.0)
    assert compute_p_reach(1, 100) == pytest.approx(3 / 104)
    assert compute_p_reach(10, 0) == 1.0


def test_session_upload_records_reached_as_opportunity_and_replay_deletes_stale_event(db_session):
    user_id = 123
    ancestor_fen = "8/8/8/8/8/8/8/K6k w - - 0 1"
    opponent_fen = "8/8/8/8/8/8/8/1K5k b - - 0 1"
    blunder_fen = "8/8/8/8/8/8/8/2K4k w - - 0 2"

    ancestor = _position(db_session, user_id=user_id, fen=ancestor_fen, active_color="white")
    opponent = _position(db_session, user_id=user_id, fen=opponent_fen, active_color="black")
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    db_session.add_all([
        Move(from_position_id=ancestor.id, move_san="a", to_position_id=opponent.id),
        Move(from_position_id=opponent.id, move_san="b", to_position_id=blunder_position.id),
    ])
    blunder = _blunder(db_session, user_id=user_id, position=blunder_position)
    game_session = _session(db_session, user_id=user_id)
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="white",
            move_san="a",
            fen_before=ancestor_fen,
            fen_after=opponent_fen,
        )
    )
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )

    event = db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    assert event.opportunity is True
    assert event.reached is False

    move = db_session.query(SessionMove).filter_by(session_id=game_session.id).one()
    move.fen_before = "not-a-fen"
    move.fen_after = "also-not-a-fen"
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )
    assert db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).count() == 0


def test_player_turn_ancestor_without_steer_point_is_not_opportunity(db_session):
    user_id = 123
    ancestor_fen = "8/8/8/8/8/8/8/K6k w - - 0 1"
    steer_fen = "8/8/8/8/8/8/8/1K5k b - - 0 1"
    diverged_fen = "8/8/8/8/8/8/8/K5k1 b - - 0 1"
    blunder_fen = "8/8/8/8/8/8/8/2K4k w - - 0 2"

    ancestor = _position(db_session, user_id=user_id, fen=ancestor_fen, active_color="white")
    steer = _position(db_session, user_id=user_id, fen=steer_fen, active_color="black")
    diverged = _position(db_session, user_id=user_id, fen=diverged_fen, active_color="black")
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    db_session.add_all([
        Move(from_position_id=ancestor.id, move_san="a", to_position_id=steer.id),
        Move(from_position_id=ancestor.id, move_san="h", to_position_id=diverged.id),
        Move(from_position_id=steer.id, move_san="b", to_position_id=blunder_position.id),
    ])
    blunder = _blunder(db_session, user_id=user_id, position=blunder_position)
    game_session = _session(db_session, user_id=user_id)
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="white",
            move_san="h",
            fen_before=ancestor_fen,
            fen_after=diverged_fen,
        )
    )
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )

    assert db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).count() == 0


def test_reached_position_counts_as_denominator_opportunity(db_session):
    user_id = 123
    blunder_fen = "8/8/8/8/8/8/K7/7k w - - 0 1"
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=blunder_position)
    game_session = _session(db_session, user_id=user_id)
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="black",
            move_san="x",
            fen_after=blunder_fen,
        )
    )
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )

    event = db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    assert event.opportunity is True
    assert event.reached is True


def test_converted_drill_opportunity_event_uses_started_at(db_session):
    # Amended drill policy (2026-06-01): a converted drill is one full normal game
    # whose opportunity timeline anchors to started_at, not conversion time.
    user_id = 123
    old_started_at = datetime.now(timezone.utc) - timedelta(days=40)
    normal_started_at = datetime.now(timezone.utc) - timedelta(days=2)
    blunder_fen = "8/8/8/8/8/8/K7/7k w - - 0 1"
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=blunder_position)
    game_session = _session(
        db_session,
        user_id=user_id,
        started_at=old_started_at,
        normal_started_at=normal_started_at,
        converted=True,
    )
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="black",
            move_san="x",
            fen_after=blunder_fen,
            segment="normal",
        )
    )
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )

    event = db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    assert event.occurred_at == old_started_at.replace(tzinfo=None)


def test_unconverted_drill_segment_move_creates_opportunity_event(db_session):
    # Amended drill policy (2026-06-01): pre-continue drill uploads (segment='drill',
    # unconverted session) feed regular SRS opportunity creation. This guards against
    # reintroducing the old normal-segment filter.
    user_id = 123
    started_at = datetime.now(timezone.utc) - timedelta(days=1)
    blunder_fen = "8/8/8/8/8/8/K7/7k w - - 0 1"
    blunder_position = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=blunder_position)

    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=started_at,
        status="active",
        engine_elo=1500,
        player_color="white",
        session_mode="drill",
        drill_state="active",
        is_rated=False,
    )
    db_session.add(game_session)
    db_session.flush()
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="black",
            move_san="x",
            fen_after=blunder_fen,
            segment="drill",
        )
    )
    db_session.commit()

    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )

    event = db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    assert event.reached is True
    assert event.occurred_at == started_at.replace(tzinfo=None)


def test_same_session_review_event_is_excluded_from_since_review(db_session):
    user_id = 123
    now = datetime.now(timezone.utc)
    pos = _position(db_session, user_id=user_id, fen="8/8/8/8/8/8/K7/6k1 w - - 0 1", active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=pos)
    game_session = _session(db_session, user_id=user_id, started_at=now - timedelta(minutes=5))
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=game_session.id,
            occurred_at=game_session.started_at,
            opportunity=True,
            reached=True,
        )
    )
    db_session.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=game_session.id,
            reviewed_at=now,
            passed=True,
            move_played_san="good",
            eval_delta_cp=0,
        )
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], now=now)[blunder.id]
    assert counters.opportunities_since_review == 0
    assert counters.opportunities_30d == 1
    assert counters.reached_30d == 1


def test_opportunity_counters_ignore_events_before_blunder_creation(db_session):
    user_id = 123
    now = datetime.now(timezone.utc)
    pos = _position(db_session, user_id=user_id, fen="8/8/8/8/8/8/K7/6k1 w - - 0 1", active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=pos)
    blunder.created_at = now - timedelta(days=5)
    old_session = _session(db_session, user_id=user_id, started_at=now - timedelta(days=10))
    recent_session = _session(db_session, user_id=user_id, started_at=now - timedelta(days=2))
    db_session.add_all([
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=old_session.id,
            occurred_at=old_session.started_at,
            opportunity=True,
            reached=True,
        ),
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=recent_session.id,
            occurred_at=recent_session.started_at,
            opportunity=True,
            reached=True,
        ),
    ])
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], now=now)[blunder.id]
    assert counters.opportunities_since_review == 1
    assert counters.opportunities_30d == 1
    assert counters.reached_30d == 1


def test_ghost_move_downweights_rare_branch_vs_frequently_reached_branch(db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen = "8/8/8/8/8/8/8/K5k1 b - - 0 1"
    rare_fen = "8/8/8/8/8/8/8/1K4k1 w - - 0 2"
    frequent_fen = "8/8/8/8/8/8/8/2K3k1 w - - 0 2"
    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")
    rare_pos = _position(db_session, user_id=user_id, fen=rare_fen, active_color="white")
    frequent_pos = _position(db_session, user_id=user_id, fen=frequent_fen, active_color="white")
    db_session.add_all([
        Move(from_position_id=start.id, move_san="rare", to_position_id=rare_pos.id),
        Move(from_position_id=start.id, move_san="freq", to_position_id=frequent_pos.id),
    ])
    rare = _blunder(db_session, user_id=user_id, position=rare_pos, eval_loss_cp=200)
    frequent = _blunder(db_session, user_id=user_id, position=frequent_pos, eval_loss_cp=200)
    rare.created_at = now - timedelta(days=2)
    frequent.created_at = now - timedelta(days=2)

    for idx in range(100):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=rare,
            opportunity=True,
            reached=idx == 0,
            occurred_at=now - timedelta(days=1, minutes=idx),
        )
    for idx in range(10):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=frequent,
            opportunity=True,
            reached=True,
            occurred_at=now - timedelta(days=1, minutes=idx),
        )
    db_session.commit()

    move_san, target_blunder_id, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )

    assert move_san == "freq"
    assert target_blunder_id == frequent.id


def test_reached_since_review_counts_only_post_review_reaches(db_session):
    user_id = 123
    now = datetime.now(timezone.utc)
    pos = _position(db_session, user_id=user_id, fen="8/8/8/8/8/8/K7/5k2 w - - 0 1", active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=pos)

    # 183 ancestor-only opportunities (not reached) — should not count toward reached_since_review
    for idx in range(183):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=blunder,
            opportunity=True,
            reached=False,
            occurred_at=now - timedelta(minutes=idx + 1),
        )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], now=now)[blunder.id]
    assert counters.opportunities_since_review == 183
    assert counters.reached_since_review == 0
    assert counters.reached_30d == 0


def test_reached_since_review_excludes_pre_review_and_same_session_reaches(db_session):
    user_id = 123
    now = datetime.now(timezone.utc)
    pos = _position(db_session, user_id=user_id, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white")
    blunder = _blunder(db_session, user_id=user_id, position=pos)

    # Pre-review reach
    pre_session = _session(db_session, user_id=user_id, started_at=now - timedelta(hours=5))
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=pre_session.id,
            occurred_at=now - timedelta(hours=5),
            opportunity=True,
            reached=True,
        )
    )

    # Review session — its reach is excluded by same-session filter
    review_session = _session(db_session, user_id=user_id, started_at=now - timedelta(hours=2))
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=review_session.id,
            occurred_at=now - timedelta(hours=2),
            opportunity=True,
            reached=True,
        )
    )
    db_session.add(
        BlunderReview(
            blunder_id=blunder.id,
            session_id=review_session.id,
            reviewed_at=now - timedelta(hours=2),
            passed=True,
            move_played_san="good",
            eval_delta_cp=0,
        )
    )

    # Post-review reach in new session — counts
    post_session = _session(db_session, user_id=user_id, started_at=now - timedelta(minutes=10))
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=post_session.id,
            occurred_at=now - timedelta(minutes=10),
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], now=now)[blunder.id]
    assert counters.reached_since_review == 1


def test_find_ghost_move_suppresses_high_opportunity_zero_reach_blunder(db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen = "8/8/8/8/8/8/8/K6k b - - 0 1"
    blunder_fen = "8/8/8/8/8/8/8/1K5k w - - 0 2"
    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")
    blunder_pos = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    db_session.add(Move(from_position_id=start.id, move_san="step", to_position_id=blunder_pos.id))
    blunder = _blunder(db_session, user_id=user_id, position=blunder_pos, eval_loss_cp=200)
    blunder.created_at = now - timedelta(days=5)

    # 183 ancestor-only opportunities, zero reaches — matches blunder 578 scenario
    for idx in range(183):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=blunder,
            opportunity=True,
            reached=False,
            occurred_at=now - timedelta(minutes=idx + 1),
        )
    db_session.commit()

    move_san, target_blunder_id, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )
    assert move_san is None
    assert target_blunder_id is None
