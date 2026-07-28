from __future__ import annotations

import contextlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import chess
import pytest
from sqlalchemy import event

from app.api.session import (
    _compute_blunder_opportunity_events,
    _forward_reachable_position_ids,
    _upsert_session_position_graph,
)
from scripts.backfill_ghost_graph import _MoveRow
from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    BlunderReview,
    GameSession,
    Move,
    OpponentDecision,
    Position,
    SessionMove,
)
from app.srs_math import (
    OPPORTUNITY_ANCESTOR_RADIUS_PLY,
    calculate_opportunity_overdue,
    compute_p_reach,
    expected_opportunities,
)
from app.srs_opportunity import (
    P_REACH_FLOOR,
    P_REACH_MIN_SAMPLE,
    load_opportunity_counters,
    load_review_counters,
    practice_priority_score,
)


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


def _decision(
    db_session,
    *,
    session: GameSession,
    blunder: Blunder | None,
    served_at: datetime,
    ply_before: int = 0,
) -> OpponentDecision:
    """One row of the authoritative served-decision log.

    ``target_blunder_id=None`` is a real, common shape: engine moves and
    pre-root drill route moves are served without a target and must produce no
    targeted attribution.
    """
    decision = OpponentDecision(
        decision_id=uuid.uuid4(),
        session_id=session.id,
        # Unique per row: the real fingerprint is a hash of the request FEN and
        # UCI history, and (session_id, request_fingerprint) is the replay key.
        request_fingerprint=uuid.uuid4().hex,
        request_fen_hash=fen_hash("8/8/8/8/8/8/8/K6k w - - 0 1"),
        uci_history="[]",
        ply_before=ply_before,
        served_at=served_at,
        response_payload="{}",
        target_blunder_id=blunder.id if blunder is not None else None,
    )
    db_session.add(decision)
    db_session.flush()
    return decision


def _opportunity_event(
    db_session,
    *,
    user_id: int,
    blunder: Blunder,
    opportunity: bool,
    reached: bool,
    occurred_at: datetime,
    targeted: bool = False,
) -> GameSession:
    """One session's broad evidence, optionally with a served target.

    ``targeted`` adds an ``opponent_decisions`` row in the SAME session, which
    is what the targeted-session reach rate counts. The two streams are
    deliberately separate: broad evidence comes from the client upload, the
    decision log does not.
    """
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
    if targeted:
        _decision(db_session, session=game_session, blunder=blunder, served_at=occurred_at)
    return game_session


def test_opportunity_math_smoothed_and_bounded():
    assert expected_opportunities(0) == 1.0
    assert expected_opportunities(3) == 8.0
    assert calculate_opportunity_overdue(opportunities_since_review=4, pass_streak=2) == pytest.approx(1.0)
    assert compute_p_reach(1, 100) == pytest.approx(3 / 104)
    assert compute_p_reach(10, 0) == 1.0


def test_load_review_counters_empty_short_circuits(db_session):
    assert load_review_counters(db_session, []) == {}


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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)

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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)
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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)

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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)

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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)

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
    db_session.commit()  # caller owns the commit (see _run_session_move_evidence_side_effects)

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

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id, now=now)[blunder.id]
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

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id, now=now)[blunder.id]
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

    # Steered at 100 times, reached once, versus steered at 10 times and reached
    # every time. Reach weight reads the TARGETED sessions, so the rare branch
    # loses despite its much larger broad opportunity count.
    for idx in range(100):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=rare,
            opportunity=True,
            reached=idx == 0,
            occurred_at=now - timedelta(days=1, minutes=idx),
            targeted=True,
        )
    for idx in range(10):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=frequent,
            opportunity=True,
            reached=True,
            occurred_at=now - timedelta(days=1, minutes=idx),
            targeted=True,
        )
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
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

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id, now=now)[blunder.id]
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

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id, now=now)[blunder.id]
    assert counters.reached_since_review == 1


def _single_step_blunder(db_session, *, user_id: int, now: datetime, blunder_fen: str):
    """A start position with exactly one move leading to a blundered position."""
    start_fen = "8/8/8/8/8/8/8/K6k b - - 0 1"
    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")
    blunder_pos = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    db_session.add(Move(from_position_id=start.id, move_san="step", to_position_id=blunder_pos.id))
    blunder = _blunder(db_session, user_id=user_id, position=blunder_pos, eval_loss_cp=200)
    blunder.created_at = now - timedelta(days=5)
    return start_fen, blunder


def test_find_ghost_move_suppresses_high_targeted_zero_reach_blunder(db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen, blunder = _single_step_blunder(
        db_session, user_id=user_id, now=now, blunder_fen="8/8/8/8/8/8/8/1K5k w - - 0 2"
    )

    # 183 sessions steered at this blunder, none of which reached it.
    for idx in range(183):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=blunder,
            opportunity=True,
            reached=False,
            occurred_at=now - timedelta(minutes=idx + 1),
            targeted=True,
        )
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )
    assert move_san is None
    assert target_blunder_id is None


def test_find_ghost_move_keeps_blunder_with_broad_only_zero_reach_evidence(db_session):
    """The g-ghost-preach-absorb defect: broad evidence alone must not exclude.

    Identical to the test above except that no session ever STEERED at this
    blunder — the 183 rows are ancestor-only neighbourhood evidence from
    ordinary play. That denominator is structurally ~1/N, so gating on it put
    the average blunder at the exclusion floor and collapsed steering onto one
    target.
    """
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen, blunder = _single_step_blunder(
        db_session, user_id=user_id, now=now, blunder_fen="8/8/8/8/8/8/8/1K5k w - - 0 2"
    )

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

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )
    assert move_san == "step"
    assert target_blunder_id == blunder.id


def test_find_ghost_move_uses_due_opportunities_with_supported_reach_rate(db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen = "8/8/8/8/8/8/K7/6k1 b - - 0 1"
    blunder_fen = "8/8/8/8/8/8/1K6/6k1 w - - 0 2"
    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")
    blunder_pos = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="white")
    db_session.add(Move(from_position_id=start.id, move_san="step", to_position_id=blunder_pos.id))
    blunder = _blunder(db_session, user_id=user_id, position=blunder_pos, eval_loss_cp=200)
    blunder.created_at = now - timedelta(days=5)
    blunder.pass_streak = 5

    # 40 opportunities are due for pass_streak=5 (expected=32), while only
    # 3 exact reaches would not pass the old reached_since_review gate.
    for idx in range(40):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=blunder,
            opportunity=True,
            reached=idx < 3,
            occurred_at=now - timedelta(minutes=idx + 1),
        )
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )

    assert move_san == "step"
    assert target_blunder_id == blunder.id


# ---------------------------------------------------------------------------
# Forward-BFS unit tests (g-ejum rewrite) + traversal perf guard (g-8y63)
# ---------------------------------------------------------------------------


def _distinct_king_fens(count: int, *, turn: str = "w") -> list[str]:
    """Yield ``count`` distinct, python-chess-legal two-king FENs.

    ``fen_hash`` strips move clocks (and canonicalizes en passant), so distinct
    hashes require distinct *placements* — varying only the move counter collapses
    to one hash. Lone-king boards are the cheapest legal positions; we walk king
    square pairs (kings never adjacent) until we have enough.
    """
    fens: list[str] = []
    for wk in range(64):
        for bk in range(64):
            if wk == bk or chess.square_distance(wk, bk) <= 1:
                continue
            board = chess.Board(None)
            board.set_piece_at(wk, chess.Piece(chess.KING, chess.WHITE))
            board.set_piece_at(bk, chess.Piece(chess.KING, chess.BLACK))
            board.turn = chess.WHITE if turn == "w" else chess.BLACK
            fens.append(board.fen())
            if len(fens) >= count:
                return fens
    raise AssertionError(f"could only build {len(fens)} distinct fens, needed {count}")


class _MovesTableQueryCounter:
    r"""Count SQL statements that touch the global ``moves`` edge table.

    The perf guard must isolate the forward-BFS *traversal* cost from the rest of
    ``_compute_blunder_opportunity_events`` — the session_moves load, the per-user
    Blunder load, the existing-events delete, and one upsert per matched blunder.
    Only the BFS queries ``moves``, so counting moves-table statements measures
    exactly the cost the g-ejum rewrite was meant to bound.

    ``\bmoves\b`` deliberately matches the standalone ``moves`` table but not
    ``session_moves``: ``_`` is a word character, so no word boundary precedes the
    "moves" in "session_moves".
    """

    _MOVES_TABLE = re.compile(r"\bmoves\b")

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        if self._MOVES_TABLE.search(statement.lower()):
            self.count += 1


@contextlib.contextmanager
def _count_moves_queries(db_session):
    counter = _MovesTableQueryCounter()
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", counter)


class _OpportunityEventWriteCounter:
    r"""Count INSERT vs DELETE statements that touch ``blunder_opportunity_events``.

    The g-b809 perf guard asserts the per-blunder upsert loop collapsed to one
    INSERT and the per-event ``db.delete()`` loop collapsed to one DELETE. We
    classify by the statement's leading verb so the ``existing_events`` SELECT (a
    JOIN that also names the table) is ignored — only writes are tallied.
    """

    _TABLE = "blunder_opportunity_events"

    def __init__(self) -> None:
        self.insert = 0
        self.delete = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        lowered = statement.lower()
        if self._TABLE not in lowered:
            return
        verb = lowered.lstrip()
        if verb.startswith("insert"):
            self.insert += 1
        elif verb.startswith("delete"):
            self.delete += 1


@contextlib.contextmanager
def _count_opportunity_event_writes(db_session):
    counter = _OpportunityEventWriteCounter()
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", counter)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", counter)


def test_forward_reachable_multi_ply_and_radius_cutoff(db_session):
    user_id = 123
    fens = _distinct_king_fens(5)
    nodes = [
        _position(db_session, user_id=user_id, fen=fen, active_color="white")
        for fen in fens
    ]
    # Chain: n0 -> n1 -> n2 -> n3 -> n4
    db_session.add_all(
        [
            Move(from_position_id=nodes[i].id, move_san=f"m{i}", to_position_id=nodes[i + 1].id)
            for i in range(len(nodes) - 1)
        ]
    )
    db_session.commit()

    full = _forward_reachable_position_ids(
        db_session, user_id=user_id, start_ids={nodes[0].id}, radius_ply=8
    )
    assert full == {n.id for n in nodes[1:]}

    # Radius cutoff stops at distance 2 (n1, n2); deeper nodes excluded.
    capped = _forward_reachable_position_ids(
        db_session, user_id=user_id, start_ids={nodes[0].id}, radius_ply=2
    )
    assert capped == {nodes[1].id, nodes[2].id}


def test_fresh_upload_edge_visible_to_opportunity_bfs(db_session):
    """A fresh opponent->player edge taught THIS upload reaches a downstream blunder.

    Pins the g-wlzj Phase 6 flush: the upsert flushes its own edges so the
    immediately-following forward BFS (same txn, autoflush off) sees them. Here O
    and P pre-exist, so the OLD per-position incidental flush never fired for the
    fresh O->P edge — without Phase 6 the BFS could not reach B and no opportunity
    event would be created. Guards against silently reverting to defer-all.
    """
    user_id = 123
    opp_fen = "7k/8/8/8/8/8/8/K7 b - - 0 1"        # black to move (opponent)
    player_fen = "6k1/8/8/8/8/8/8/K7 w - - 1 2"    # white to move (player), after ...Kg8
    blunder_fen = "6k1/8/8/8/8/8/8/1K6 b - - 2 2"  # downstream of player_fen, after Kb1

    opp = _position(db_session, user_id=user_id, fen=opp_fen, active_color="black")
    player = _position(db_session, user_id=user_id, fen=player_fen, active_color="white")
    blunder_pos = _position(db_session, user_id=user_id, fen=blunder_fen, active_color="black")
    # Pre-existing player->blunder edge; the opponent->player edge is taught below.
    db_session.add(Move(from_position_id=player.id, move_san="Kb1", to_position_id=blunder_pos.id))
    blunder = _blunder(db_session, user_id=user_id, position=blunder_pos)
    game_session = _session(db_session, user_id=user_id, player_color="white")
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="black",
            move_san="Kg8",
            fen_before=opp_fen,
            fen_after=player_fen,
        )
    )
    db_session.commit()

    # Same transaction, no commit between: the upsert stages + flushes the fresh
    # O->P edge (Phase 6), then the BFS inside compute reads it.
    stats = _upsert_session_position_graph(
        db_session,
        user_id=user_id,
        moves=[_MoveRow(fen_before=opp_fen, move_san="Kg8", fen_after=player_fen)],
    )
    assert stats.edges_created == 1
    _compute_blunder_opportunity_events(
        db_session,
        session_id=game_session.id,
        user_id=user_id,
        player_color="white",
    )
    db_session.commit()

    event_row = (
        db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    )
    assert event_row.opportunity is True
    assert event_row.reached is False


def test_forward_reachable_handles_cycles_and_transpositions(db_session):
    user_id = 123
    fens = _distinct_king_fens(3)
    a, b, c = (
        _position(db_session, user_id=user_id, fen=fen, active_color="white")
        for fen in fens
    )
    # Cycle a -> b -> a, plus a transposition a -> c -> b (b reached two ways).
    db_session.add_all(
        [
            Move(from_position_id=a.id, move_san="ab", to_position_id=b.id),
            Move(from_position_id=b.id, move_san="ba", to_position_id=a.id),
            Move(from_position_id=a.id, move_san="ac", to_position_id=c.id),
            Move(from_position_id=c.id, move_san="cb", to_position_id=b.id),
        ]
    )
    db_session.commit()

    # The visited set must terminate the cycle and never re-yield the start.
    reachable = _forward_reachable_position_ids(
        db_session, user_id=user_id, start_ids={a.id}, radius_ply=8
    )
    assert reachable == {b.id, c.id}
    assert a.id not in reachable


def test_forward_reachable_is_user_scoped(db_session):
    # moves carries no user_id; expansion must constrain reached positions to the
    # querying user so a shared/foreign edge target cannot leak into the BFS.
    owner_id = 123
    other_id = 999
    own_fens = _distinct_king_fens(2)
    other_fen = _distinct_king_fens(1, turn="b")[0]
    start = _position(db_session, user_id=owner_id, fen=own_fens[0], active_color="white")
    own_child = _position(db_session, user_id=owner_id, fen=own_fens[1], active_color="white")
    foreign_child = _position(db_session, user_id=other_id, fen=other_fen, active_color="black")
    db_session.add_all(
        [
            Move(from_position_id=start.id, move_san="own", to_position_id=own_child.id),
            Move(from_position_id=start.id, move_san="foreign", to_position_id=foreign_child.id),
        ]
    )
    db_session.commit()

    reachable = _forward_reachable_position_ids(
        db_session, user_id=owner_id, start_ids={start.id}, radius_ply=8
    )
    assert reachable == {own_child.id}
    assert foreign_child.id not in reachable


def _build_chain_session_with_blunders(db_session, *, user_id: int, blunder_count: int):
    """A session that seeds the forward BFS from one opponent-color position and
    chains forward edges deeper than the radius, plus ``blunder_count`` blunders
    that sit on *no* session/reachable position (so none of them match).

    Because no blunder matches, ``_compute_blunder_opportunity_events`` issues
    zero upserts and the only ``moves``-table queries come from the BFS — its cost
    depends solely on the graph depth, never on ``blunder_count``.
    """
    chain_len = OPPORTUNITY_ANCESTOR_RADIUS_PLY + 2  # deeper than radius
    # White player ⇒ opponent is black: the seed must be black-to-move to be an
    # opportunity source. Chain + blunder FENs share one white-turn pool so they
    # stay mutually distinct; the black-turn seed cannot collide with any of them.
    pool = _distinct_king_fens(chain_len + blunder_count)
    seed_fen = "8/8/8/8/8/8/8/K6k b - - 0 1"
    seed = _position(db_session, user_id=user_id, fen=seed_fen, active_color="black")
    chain = [seed]
    chain_fens = [seed_fen]
    for fen in pool[:chain_len]:
        node = _position(db_session, user_id=user_id, fen=fen, active_color="white")
        db_session.add(
            Move(from_position_id=chain[-1].id, move_san=f"c{len(chain)}", to_position_id=node.id)
        )
        chain.append(node)
        chain_fens.append(fen)

    game_session = _session(db_session, user_id=user_id)
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="black",
            move_san="seed",
            fen_before=seed_fen,
            fen_after=chain_fens[1],
        )
    )

    # Blunders parked on positions that are neither session positions nor forward
    # reachable from the seed: a separate batch of distinct FENs with no in-edges.
    for fen in pool[chain_len:]:
        pos = _position(db_session, user_id=user_id, fen=fen, active_color="white")
        _blunder(db_session, user_id=user_id, position=pos)

    db_session.commit()
    return game_session


def test_opportunity_traversal_cost_is_bounded_and_independent_of_blunder_count(db_session):
    """Perf guard for the g-ejum forward-BFS rewrite.

    The old per-blunder reverse walk ran ≈ ``blunders × radius`` unindexed scans of
    ``moves``. The forward rewrite runs a single BFS, so the number of
    ``moves``-table queries is bounded by the radius and is identical whether the
    user has 5 blunders or 80. Counting only ``moves`` statements isolates traversal
    cost from the per-matched-blunder upserts (framing #1 in the g-8y63 plan).
    """
    with _count_moves_queries(db_session) as few_counter:
        _build_chain_session_with_blunders(db_session, user_id=201, blunder_count=5)
        game_few = db_session.query(GameSession).filter_by(user_id=201).first()
        # Reset the counter after fixture setup so we only measure the compute call.
        few_counter.count = 0
        _compute_blunder_opportunity_events(
            db_session, session_id=game_few.id, user_id=201, player_color="white"
        )
        db_session.commit()
    few_queries = few_counter.count

    with _count_moves_queries(db_session) as many_counter:
        _build_chain_session_with_blunders(db_session, user_id=202, blunder_count=80)
        game_many = db_session.query(GameSession).filter_by(user_id=202).first()
        many_counter.count = 0
        _compute_blunder_opportunity_events(
            db_session, session_id=game_many.id, user_id=202, player_color="white"
        )
        db_session.commit()
    many_queries = many_counter.count

    # Sanity: the BFS actually ran (guards against the counter silently matching
    # nothing, which would make the bounds below pass at zero).
    assert few_queries >= 1
    # Traversal cost is bounded by the radius (one query per BFS level), not by the
    # ~blunders×radius scans the old reverse walk produced.
    assert few_queries <= OPPORTUNITY_ANCESTOR_RADIUS_PLY
    # And it does not scale with blunder count: 16× more blunders, same query count.
    assert many_queries == few_queries


def _build_reached_session_with_stale_events(
    db_session, *, user_id: int, reached_count: int, stale_count: int
):
    """A session with ``reached_count`` reached blunders and ``stale_count``
    pre-existing opportunity events that the recompute must delete.

    Each reached blunder sits on a position the session actually reaches via a
    ``SessionMove.fen_after`` (so ``opportunity``/``reached`` are both true) — these
    drive the bulk INSERT. Each stale event points at a blunder on an unrelated
    position with no in-edges, so it is neither reached nor forward-reachable and
    the recompute drops it via the bulk DELETE. All blunders belong to ``user_id``
    so the user-scoped ``existing_events`` query sees the stale rows.

    Distinct king-only FENs satisfy ``uq_blunders_user_position`` (one blunder per
    position).
    """
    fens = _distinct_king_fens(reached_count + stale_count)
    game_session = _session(db_session, user_id=user_id)

    reached_blunders = []
    for i, fen in enumerate(fens[:reached_count]):
        pos = _position(db_session, user_id=user_id, fen=fen, active_color="white")
        reached_blunders.append(_blunder(db_session, user_id=user_id, position=pos))
        db_session.add(
            SessionMove(
                session_id=game_session.id,
                move_number=i + 1,
                color="black",
                move_san=f"m{i}",
                fen_after=fen,
            )
        )

    stale_blunders = []
    for fen in fens[reached_count:]:
        pos = _position(db_session, user_id=user_id, fen=fen, active_color="white")
        blunder = _blunder(db_session, user_id=user_id, position=pos)
        stale_blunders.append(blunder)
        db_session.add(
            BlunderOpportunityEvent(
                blunder_id=blunder.id,
                session_id=game_session.id,
                occurred_at=datetime.now(timezone.utc),
                opportunity=True,
                reached=True,
            )
        )

    db_session.commit()
    return game_session, reached_blunders, stale_blunders


@pytest.mark.parametrize("reached_count,stale_count", [(5, 3), (40, 12)])
def test_opportunity_event_writes_are_batched(db_session, reached_count, stale_count):
    """g-b809 perf guard: the per-blunder upsert loop and the per-event delete loop
    each collapse to a single statement, independent of blunder/event count.

    Stale rows are essential: without them a leftover per-row ``db.delete()`` loop
    would emit 0 deletes and pass silently, so the fixture seeds rows that must be
    removed and asserts both that exactly one DELETE ran and that they are gone.
    """
    user_id = 300 + reached_count
    game_session, reached_blunders, stale_blunders = _build_reached_session_with_stale_events(
        db_session, user_id=user_id, reached_count=reached_count, stale_count=stale_count
    )

    # Count only the recompute: the listener is registered after fixture setup, so
    # fixture INSERTs are excluded without needing a counter reset.
    with _count_opportunity_event_writes(db_session) as counter:
        _compute_blunder_opportunity_events(
            db_session, session_id=game_session.id, user_id=user_id, player_color="white"
        )
        # Snapshot before commit: both writes must emit inside the compute stage,
        # not be deferred to the caller's commit. (synchronize_session=False is what
        # moves the DELETE here; a leftover per-row ORM delete loop would instead
        # flush at commit and break the before/after split below.)
        insert_in_compute = counter.insert
        delete_in_compute = counter.delete
        db_session.commit()

    # One INSERT over all matched blunders (independent of reached_count) and one
    # DELETE over all stale rows (independent of stale_count) — no per-row trips —
    # and both landed in the compute stage with the commit adding nothing.
    assert insert_in_compute == 1
    assert delete_in_compute == 1
    assert counter.insert == 1
    assert counter.delete == 1

    # The stale events are actually gone (proves the bulk DELETE ran, not a no-op).
    stale_ids = [b.id for b in stale_blunders]
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter(
            BlunderOpportunityEvent.session_id == game_session.id,
            BlunderOpportunityEvent.blunder_id.in_(stale_ids),
        )
        .count()
        == 0
    )

    # Every reached blunder got its opportunity event written by the single INSERT.
    reached_ids = [b.id for b in reached_blunders]
    written = (
        db_session.query(BlunderOpportunityEvent)
        .filter(
            BlunderOpportunityEvent.session_id == game_session.id,
            BlunderOpportunityEvent.blunder_id.in_(reached_ids),
        )
        .all()
    )
    assert len(written) == reached_count
    assert all(e.opportunity and e.reached for e in written)


def test_find_ghost_move_prefers_immediate_review_over_deeper_route(db_session):
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen = "8/8/8/8/8/8/K7/7k b - - 0 1"
    immediate_fen = "8/8/8/8/8/8/1K6/7k w - - 0 2"
    route_1_fen = "8/8/8/8/8/8/2K5/7k w - - 0 2"
    route_2_fen = "8/8/8/8/8/3K4/8/7k b - - 0 2"
    route_3_fen = "8/8/8/8/4K3/8/8/7k w - - 0 3"
    route_4_fen = "8/8/8/5K2/8/8/8/7k b - - 0 3"
    deep_fen = "8/8/6K1/8/8/8/8/7k w - - 0 4"

    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")
    immediate_pos = _position(db_session, user_id=user_id, fen=immediate_fen, active_color="white")
    route_1 = _position(db_session, user_id=user_id, fen=route_1_fen, active_color="white")
    route_2 = _position(db_session, user_id=user_id, fen=route_2_fen, active_color="black")
    route_3 = _position(db_session, user_id=user_id, fen=route_3_fen, active_color="white")
    route_4 = _position(db_session, user_id=user_id, fen=route_4_fen, active_color="black")
    deep_pos = _position(db_session, user_id=user_id, fen=deep_fen, active_color="white")
    db_session.add_all([
        Move(from_position_id=start.id, move_san="review", to_position_id=immediate_pos.id),
        Move(from_position_id=start.id, move_san="route", to_position_id=route_1.id),
        Move(from_position_id=route_1.id, move_san="u1", to_position_id=route_2.id),
        Move(from_position_id=route_2.id, move_san="o2", to_position_id=route_3.id),
        Move(from_position_id=route_3.id, move_san="u2", to_position_id=route_4.id),
        Move(from_position_id=route_4.id, move_san="o3", to_position_id=deep_pos.id),
    ])
    immediate = _blunder(db_session, user_id=user_id, position=immediate_pos, eval_loss_cp=50)
    deep = _blunder(db_session, user_id=user_id, position=deep_pos, eval_loss_cp=1000)
    immediate.created_at = now - timedelta(days=5)
    deep.created_at = now - timedelta(days=5)

    for idx in range(2):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=immediate,
            opportunity=True,
            reached=True,
            occurred_at=now - timedelta(minutes=idx + 1),
        )
    for idx in range(100):
        _opportunity_event(
            db_session,
            user_id=user_id,
            blunder=deep,
            opportunity=True,
            reached=idx < 20,
            occurred_at=now - timedelta(minutes=idx + 2),
        )
    db_session.commit()

    move_san, target_blunder_id, _, _, _ = find_ghost_move(
        db=db_session,
        user_id=user_id,
        fen=start_fen,
        player_color="white",
        _rng_seed=1,
    )

    assert move_san == "review"
    assert target_blunder_id == immediate.id


def test_practice_priority_severity_saturates_and_floors_negatives():
    """g-no51: severity flows through the shared centipawn_loss cap.

    ``practice_priority_score`` computes severity as
    ``log1p(centipawn_loss(eval_loss_cp) / SEVERITY_NORMALIZER_CP)`` where
    ``centipawn_loss`` floors negatives to 0 and caps at 1000cp. So a 10000cp
    (mate pseudo-cp) loss and a 1000cp loss share one severity — mate magnitude
    cannot push practice priority above the decisive-mistake ceiling — while a
    negative legacy eval_loss floors to severity 0 (same as 0cp), strictly below
    the 1000cp case. All non-severity factors are held equal (counters=None ⇒
    urgency from created_at, reach_weight=1.0), so priority tracks severity alone.
    """
    now = datetime.now(timezone.utc)
    common = dict(
        counters=None,
        pass_streak=0,
        last_reviewed_at=None,
        created_at=now - timedelta(days=10),
        now=now,
    )

    score_mate = practice_priority_score(eval_loss_cp=10000, **common)
    score_decisive = practice_priority_score(eval_loss_cp=1000, **common)
    score_negative = practice_priority_score(eval_loss_cp=-30, **common)
    score_zero = practice_priority_score(eval_loss_cp=0, **common)

    # >=1000cp losses saturate at one severity: mate magnitude does not raise priority.
    assert score_mate == score_decisive
    # A positive-severity case must actually score above the floor (guards against
    # urgency==0 making every branch trivially equal).
    assert score_decisive > 0.0
    # Negative legacy eval_loss floors to severity 0, identical to a 0cp loss...
    assert score_negative == score_zero
    # ...and strictly below the capped-severity 1000cp case.
    assert score_negative < score_decisive


# ---------------------------------------------------------------------------
# g-targeted-reach-rate: the targeted-session reach rate comes from the
# authoritative opponent_decisions log, NOT from blunder_opportunity_events.
#
# The distinction is the whole point of the design: opportunity events are
# written by the client upload path, so a session that is served a target and
# then never uploads would silently drop a FAILED steer and bias p_reach
# upward. The decision log is written server-side at serve time and is
# permanent, so every one of these tests is a claim about evidence surviving
# something the upload path cannot guarantee.
# ---------------------------------------------------------------------------


def _targeted_only_setup(db_session, *, user_id: int = 123, now: datetime | None = None):
    """A blunder plus one session that was steered at it and uploaded nothing."""
    now = now or datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=user_id, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=user_id, position=pos)
    game_session = _session(db_session, user_id=user_id, started_at=now - timedelta(hours=2))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(hours=1)
    )
    return now, blunder, game_session


def test_targeted_counts_a_session_that_never_uploaded(db_session):
    # ZERO SessionMove rows and ZERO opportunity events: the failed steer that
    # must not vanish. Under the rejected materialize-at-upload design this
    # session produces no row at all and p_reach reads high.
    now, blunder, _ = _targeted_only_setup(db_session)
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1
    assert counters.targeted_reached_30d == 0
    # Broad evidence is genuinely absent — the two streams degrade independently.
    assert counters.event_count == 0
    assert counters.opportunities_30d == 0


def test_later_reached_event_moves_numerator_not_denominator(db_session):
    now, blunder, game_session = _targeted_only_setup(db_session)
    db_session.commit()
    before = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]

    # The upload finally lands, proving the position was reached.
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=game_session.id,
            occurred_at=now - timedelta(minutes=30),
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()
    after = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]

    assert (before.targeted_30d, before.targeted_reached_30d) == (1, 0)
    assert (after.targeted_30d, after.targeted_reached_30d) == (1, 1)
    assert after.p_reach > before.p_reach


def test_multiple_decisions_in_one_session_count_once(db_session):
    now, blunder, game_session = _targeted_only_setup(db_session)
    # Re-hooking the same blunder later in the same game is one targeted session.
    for minutes in (50, 40, 30):
        _decision(
            db_session,
            session=game_session,
            blunder=blunder,
            served_at=now - timedelta(minutes=minutes),
        )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1


def test_cross_cutoff_session_keeps_its_in_window_attempt(db_session):
    """Filter-order regression: rows are filtered BEFORE grouping.

    One session targets the blunder at day -31 and again at day -1. Grouping
    first and then testing ``MIN(served_at)`` against the cutoff would see day
    -31, drop the whole session, and return 0 — discarding an attempt that is
    squarely inside the window.
    """
    now, blunder, game_session = _targeted_only_setup(db_session)
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(days=31)
    )
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(days=1)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1


def test_out_of_window_decisions_alone_count_nothing(db_session):
    # The other half of the cutoff: with no in-window row the session drops out
    # entirely, which is what makes the exclusion floor self-healing.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    blunder.created_at = now - timedelta(days=60)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(days=40))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(days=31)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 0
    assert counters.p_reach == pytest.approx(compute_p_reach(0, 0))


def test_cross_creation_time_session_keeps_its_later_attempt(db_session):
    # Same filter-order trap on the after-created predicate: a decision served
    # before the blunder existed must not forfeit the session's later ones.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    blunder.created_at = now - timedelta(hours=6)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(hours=12))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(hours=10)
    )
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(hours=2)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1


def test_pre_creation_decisions_alone_count_nothing(db_session):
    # Independent guard on the after-created predicate. The cross-boundary test
    # above cannot be one: its two decisions collapse into a single session group
    # whether or not `served_at >= Blunder.created_at` is applied, so it still
    # passes with the predicate deleted. Only a session whose decisions are ALL
    # older than the blunder separates the two.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    blunder.created_at = now - timedelta(hours=6)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(hours=12))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(hours=10)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 0
    assert counters.p_reach == pytest.approx(compute_p_reach(0, 0))


def test_blunder_created_during_the_session_is_still_credited(db_session):
    # The timeline is per-decision served_at, never session.started_at: dating a
    # decision to the session's opening would drop this attempt entirely.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    blunder.created_at = now - timedelta(hours=3)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(hours=5))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(hours=1)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1


def test_long_running_session_counts_by_decision_time_not_start_time(db_session):
    # Session STARTED 40 days ago, decision served yesterday: in window.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    blunder.created_at = now - timedelta(days=60)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(days=40))
    _decision(
        db_session, session=game_session, blunder=blunder, served_at=now - timedelta(days=1)
    )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1


def test_replayed_broad_recompute_leaves_targeted_counts_unchanged(db_session):
    # The broad backfill deletes and rewrites opportunity events. It writes no
    # decisions, so it must be structurally incapable of moving the denominator.
    now, blunder, game_session = _targeted_only_setup(db_session)
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=game_session.id,
            occurred_at=now - timedelta(minutes=30),
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()

    def _targeted():
        counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)
        return counters[blunder.id].targeted_30d

    assert _targeted() == 1
    db_session.query(BlunderOpportunityEvent).filter(
        BlunderOpportunityEvent.session_id == game_session.id
    ).delete(synchronize_session=False)
    db_session.commit()
    # Mid-recompute, with the events gone, the attempt still counts.
    assert _targeted() == 1
    db_session.add(
        BlunderOpportunityEvent(
            blunder_id=blunder.id,
            session_id=game_session.id,
            occurred_at=now - timedelta(minutes=30),
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()
    assert _targeted() == 1


def test_exclude_session_id_applies_to_the_decision_aggregate(db_session):
    now, blunder, game_session = _targeted_only_setup(db_session)
    db_session.commit()

    counters = load_opportunity_counters(
        db_session, [blunder.id], user_id=123, now=now, exclude_session_id=game_session.id
    )[blunder.id]
    assert counters.targeted_30d == 0


def test_decision_targeting_another_users_blunder_is_not_attributed(db_session):
    # target_blunder_id is a bare FK with no user scoping, so the aggregate
    # joins through Blunder.user_id. This holds even when the caller's own
    # query was unscoped and handed us a foreign blunder id.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=999, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    other_blunder = _blunder(db_session, user_id=999, position=pos)
    game_session = _session(db_session, user_id=999, started_at=now - timedelta(hours=2))
    _decision(
        db_session, session=game_session, blunder=other_blunder, served_at=now - timedelta(hours=1)
    )
    db_session.commit()

    counters = load_opportunity_counters(
        db_session, [other_blunder.id], user_id=123, now=now
    )[other_blunder.id]
    assert counters.targeted_30d == 0


def test_untargeted_decisions_create_no_attribution(db_session):
    # Engine moves and pre-root drill route moves carry target_blunder_id=None.
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    game_session = _session(db_session, user_id=123, started_at=now - timedelta(hours=2))
    for minutes in (90, 80, 70):
        _decision(
            db_session,
            session=game_session,
            blunder=None,
            served_at=now - timedelta(minutes=minutes),
        )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 0


def test_legacy_drill_session_with_no_broad_evidence_still_counts_targeted(db_session):
    """A drill that produces no broad evidence still contributes an attempt.

    Legacy 'root_reached' drills carry no reconstructable evidence boundary, so
    boundary-scoped broad accounting excludes them. targeted +1 / reached +0 is
    the correct answer there, not zero and not an error: the session proves a
    steer was attempted and provides no proof it landed.
    """
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    drill = GameSession(
        id=uuid.uuid4(),
        user_id=123,
        started_at=now - timedelta(hours=3),
        status="ended",
        engine_elo=1500,
        player_color="white",
        session_mode="drill",
        drill_state="root_reached",
        is_rated=False,
    )
    db_session.add(drill)
    db_session.flush()
    assert drill.rated_start_ply is None
    _decision(db_session, session=drill, blunder=blunder, served_at=now - timedelta(hours=1))
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.targeted_30d == 1
    assert counters.targeted_reached_30d == 0


def test_p_reach_uses_targeted_counters_not_broad_ones(db_session):
    now = datetime.now(timezone.utc)
    pos = _position(
        db_session, user_id=123, fen="8/8/8/8/8/8/K7/4k3 w - - 0 1", active_color="white"
    )
    blunder = _blunder(db_session, user_id=123, position=pos)
    # 40 broad opportunities with no reaches would read p_reach ≈ 0.045; the
    # four targeted sessions, three of which reached, read ≈ 0.625.
    for idx in range(40):
        _opportunity_event(
            db_session,
            user_id=123,
            blunder=blunder,
            opportunity=True,
            reached=False,
            occurred_at=now - timedelta(minutes=idx + 1),
        )
    for idx in range(4):
        _opportunity_event(
            db_session,
            user_id=123,
            blunder=blunder,
            opportunity=True,
            reached=idx < 3,
            occurred_at=now - timedelta(hours=idx + 1),
            targeted=True,
        )
    db_session.commit()

    counters = load_opportunity_counters(db_session, [blunder.id], user_id=123, now=now)[blunder.id]
    assert counters.opportunities_30d == 44
    assert counters.targeted_30d == 4
    assert counters.targeted_reached_30d == 3
    assert counters.p_reach == pytest.approx(compute_p_reach(3, 4))


def test_load_opportunity_counters_requires_user_id(db_session):
    # A required keyword makes every call site a compile-time sweep instead of
    # a silent cross-user leak.
    with pytest.raises(TypeError):
        load_opportunity_counters(db_session, [1], now=datetime.now(timezone.utc))


def test_find_ghost_move_does_not_collapse_onto_one_target_under_broad_evidence(db_session):
    """End-to-end shape of the reported symptom (g-ghost-preach-absorb).

    A dense neighbourhood: one position with five candidate first moves, each
    reaching a different blunder, and every blunder carrying heavy broad
    opportunity evidence with zero broad reaches. That is ordinary play in a
    repeated opening — the user played 1.b4 every game — not evidence that any
    of these targets is unreachable.

    Against the broad denominator all five sat under the exclusion floor, the
    selector was left with a single eligible group, and the weighted choice
    became degenerate: the ghost replied with the same move every game
    regardless of seed. Nothing steers at these blunders, so the targeted
    denominator is empty and every one of them stays eligible.
    """
    from app.api.game import find_ghost_move

    user_id = 123
    now = datetime.now(timezone.utc)
    start_fen = "8/8/8/8/8/8/8/K6k b - - 0 1"
    start = _position(db_session, user_id=user_id, fen=start_fen, active_color="black")

    first_moves = ["f5", "e5", "d5", "g6", "Nf6"]
    blunder_fens = _distinct_king_fens(len(first_moves), turn="w")
    blunder_ids: list[int] = []
    for first_move, blunder_fen in zip(first_moves, blunder_fens):
        blunder_pos = _position(
            db_session,
            user_id=user_id,
            fen=blunder_fen,
            active_color="white",
        )
        db_session.add(
            Move(from_position_id=start.id, move_san=first_move, to_position_id=blunder_pos.id)
        )
        blunder = _blunder(db_session, user_id=user_id, position=blunder_pos, eval_loss_cp=200)
        blunder.created_at = now - timedelta(days=5)
        blunder_ids.append(blunder.id)
        # 2/104 ≈ 0.019 — under the old broad floor of 0.03, with well over
        # P_REACH_MIN_SAMPLE samples, so every one of the five was excluded.
        for event_idx in range(100):
            _opportunity_event(
                db_session,
                user_id=user_id,
                blunder=blunder,
                opportunity=True,
                reached=False,
                occurred_at=now - timedelta(minutes=event_idx + 1),
            )
    db_session.commit()

    # Pin the setup as a real regression guard: every one of these blunders
    # must still be over the broad sample floor and under the reach floor on
    # BROAD numbers, so this test cannot quietly stop reproducing the defect.
    counters = load_opportunity_counters(
        db_session, blunder_ids, user_id=user_id, now=now
    )
    for blunder_id in blunder_ids:
        broad = counters[blunder_id]
        assert broad.opportunities_30d >= P_REACH_MIN_SAMPLE
        assert compute_p_reach(broad.reached_30d, broad.opportunities_30d) < P_REACH_FLOOR
        assert broad.targeted_30d == 0

    served = {
        find_ghost_move(
            db=db_session,
            user_id=user_id,
            fen=start_fen,
            player_color="white",
            _rng_seed=seed,
        )[0]
        for seed in range(30)
    }
    assert None not in served
    assert len(served) > 1, f"ghost collapsed onto a single reply: {served}"
