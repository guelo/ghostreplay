from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

import pytest

from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    GameSession,
    Move,
    Position,
    SessionMove,
)
from scripts.recompute_srs_opportunities import (
    parse_args,
    recompute_one_blunder,
)


def _position(db, *, user_id, fen, active_color):
    position = Position(
        user_id=user_id,
        fen_hash=fen_hash(fen),
        fen_raw=fen,
        active_color=active_color,
    )
    db.add(position)
    db.flush()
    return position


def test_help_smoke_imports_and_parses(monkeypatch):
    # Regression guard: the module top-level import (previously broken by a stale
    # reference to a deleted helper) and argparse must both succeed before any DB
    # work. --help short-circuits with SystemExit(0).
    monkeypatch.setattr(sys, "argv", ["recompute_srs_opportunities.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        parse_args()
    assert exc.value.code == 0


def test_recompute_one_blunder_matches_forward_semantics(db_session):
    # Mirrors test_srs_opportunity's ancestor -> opponent -> blunder scenario to
    # prove the offline per-blunder reverse walk yields the same (opportunity,
    # reached) classification as the live forward BFS.
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
    blunder = Blunder(
        user_id=user_id,
        position_id=blunder_position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.flush()

    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
    )
    db_session.add(game_session)
    db_session.flush()
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

    sessions, opportunities, reached = recompute_one_blunder(db_session, blunder_id=blunder.id)

    assert sessions == 1
    assert opportunities == 1
    assert reached == 0

    event = db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).one()
    assert event.opportunity is True
    assert event.reached is False


def test_recompute_one_blunder_rejects_cross_user_ancestor_path(db_session):
    # The reverse walk must be user-scoped like the live forward BFS: a steering
    # path that only connects through ANOTHER user's position must not create an
    # opportunity event. Here user A's ancestor reaches the blunder only via a
    # user-B intermediate; the scoped reverse expansion stops at the foreign edge.
    user_a = 123
    user_b = 456
    # player_color is white, so opponent (steering) positions are black to move.
    ancestor_fen = "8/8/8/8/8/8/8/K6k b - - 0 1"  # user A, opponent (black) to move
    mid_fen = "8/8/8/8/8/8/8/1K5k w - - 0 2"  # user B, cross-user intermediate
    blunder_fen = "8/8/8/8/8/8/8/2K4k w - - 0 2"  # user A, blunder (white to move)

    ancestor = _position(db_session, user_id=user_a, fen=ancestor_fen, active_color="black")
    mid = _position(db_session, user_id=user_b, fen=mid_fen, active_color="white")
    blunder_position = _position(db_session, user_id=user_a, fen=blunder_fen, active_color="white")
    db_session.add_all([
        Move(from_position_id=ancestor.id, move_san="a", to_position_id=mid.id),
        Move(from_position_id=mid.id, move_san="b", to_position_id=blunder_position.id),
    ])
    blunder = Blunder(
        user_id=user_a,
        position_id=blunder_position.id,
        bad_move_san="bad",
        best_move_san="good",
        eval_loss_cp=200,
    )
    db_session.add(blunder)
    db_session.flush()

    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_a,
        started_at=datetime.now(timezone.utc),
        status="active",
        engine_elo=1500,
        player_color="white",
    )
    db_session.add(game_session)
    db_session.flush()
    db_session.add(
        SessionMove(
            session_id=game_session.id,
            move_number=1,
            color="white",
            move_san="a",
            fen_after=ancestor_fen,
        )
    )
    db_session.commit()

    sessions, opportunities, reached = recompute_one_blunder(db_session, blunder_id=blunder.id)

    assert opportunities == 0
    assert reached == 0
    assert db_session.query(BlunderOpportunityEvent).filter_by(blunder_id=blunder.id).count() == 0
