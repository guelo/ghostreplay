from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.fen import fen_hash
from app.models import (
    Blunder,
    BlunderOpportunityEvent,
    GameSession,
    Move,
    OpponentDecision,
    Position,
    SessionMove,
)
from app.srs_opportunity import load_opportunity_counters
from scripts.recompute_srs_opportunities import (
    main,
    parse_args,
    recompute_all_blunders,
    recompute_one_blunder,
    recompute_srs_opportunities,
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


def test_backfill_cannot_move_the_targeted_denominator(db_session):
    """The blunder-grain backfill writes BROAD evidence only.

    p_reach's denominator comes from ``opponent_decisions``, which this script
    never touches — that is what makes an interrupted or replayed recompute
    incapable of erasing a failed steer. Recording the counters before and after
    a full ``--blunder-id`` pass pins that separation structurally rather than
    trusting the query list.
    """
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

    now = datetime.now(timezone.utc)
    game_session = GameSession(
        id=uuid.uuid4(),
        user_id=user_id,
        started_at=now,
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
    db_session.add(
        OpponentDecision(
            decision_id=uuid.uuid4(),
            session_id=game_session.id,
            request_fingerprint=uuid.uuid4().hex,
            request_fen_hash=fen_hash(ancestor_fen),
            uci_history="[]",
            ply_before=0,
            served_at=now,
            response_payload="{}",
            target_blunder_id=blunder.id,
        )
    )
    db_session.commit()

    def _targeted():
        counters = load_opportunity_counters(db_session, [blunder.id], user_id=user_id)[blunder.id]
        return (counters.targeted_30d, counters.targeted_reached_30d)

    before = _targeted()
    assert before == (1, 0)

    recompute_one_blunder(db_session, blunder_id=blunder.id)
    db_session.commit()
    assert _targeted() == before

    # Rerunnable: a second pass is a no-op on both streams.
    recompute_one_blunder(db_session, blunder_id=blunder.id)
    db_session.commit()
    assert _targeted() == before


def _boundary_case(db_session, *, session_int: int = 100):
    """A drill whose root is a seed, with one pre-root-only and one root-only edge."""
    user_id = 991
    started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    start_fen = "7k/8/8/8/8/8/8/K7 w - - 0 1"
    pre_seed_fen = "7k/8/8/8/8/8/8/1K6 b - - 1 1"
    mid_fen = "6k1/8/8/8/8/8/8/1K6 w - - 2 2"
    root_fen = "6k1/8/8/8/8/8/8/2K5 b - - 3 2"
    post_fen = "5k2/8/8/8/8/8/8/2K5 w - - 4 3"
    pre_child_fen = "8/7k/8/8/8/8/8/1K6 w - - 2 2"
    downstream_fen = "7k/8/8/8/8/8/8/2K5 w - - 4 3"

    positions = {
        "pre_seed": _position(
            db_session,
            user_id=user_id,
            fen=pre_seed_fen,
            active_color="black",
        ),
        "root": _position(
            db_session,
            user_id=user_id,
            fen=root_fen,
            active_color="black",
        ),
        "post": _position(
            db_session,
            user_id=user_id,
            fen=post_fen,
            active_color="white",
        ),
        "pre_child": _position(
            db_session,
            user_id=user_id,
            fen=pre_child_fen,
            active_color="white",
        ),
        "downstream": _position(
            db_session,
            user_id=user_id,
            fen=downstream_fen,
            active_color="white",
        ),
    }
    db_session.add_all(
        [
            Move(
                from_position_id=positions["pre_seed"].id,
                move_san="route",
                to_position_id=positions["pre_child"].id,
            ),
            Move(
                from_position_id=positions["root"].id,
                move_san="real",
                to_position_id=positions["downstream"].id,
            ),
        ]
    )
    blunders = {}
    for name in ("root", "post", "pre_child", "downstream"):
        blunder = Blunder(
            user_id=user_id,
            position_id=positions[name].id,
            bad_move_san="bad",
            best_move_san="good",
            eval_loss_cp=200,
            created_at=started_at - timedelta(days=1),
        )
        db_session.add(blunder)
        blunders[name] = blunder
    db_session.flush()

    game_session = GameSession(
        id=uuid.UUID(int=session_int),
        user_id=user_id,
        started_at=started_at,
        status="ended",
        result="drill_abandon",
        engine_elo=1500,
        is_rated=False,
        player_color="white",
        session_mode="drill",
        drill_state="abandoned",
        drill_opening_key=root_fen,
        drill_root_reached_ply=3,
    )
    db_session.add(game_session)
    db_session.flush()
    for move_number, color, fen_before, fen_after in [
        (1, "white", start_fen, pre_seed_fen),
        (1, "black", pre_seed_fen, mid_fen),
        (2, "white", mid_fen, root_fen),
        (2, "black", root_fen, post_fen),
    ]:
        db_session.add(
            SessionMove(
                session_id=game_session.id,
                move_number=move_number,
                color=color,
                move_san="K",
                fen_before=fen_before,
                fen_after=fen_after,
                segment="drill",
            )
        )
    db_session.add(
        OpponentDecision(
            decision_id=uuid.uuid4(),
            session_id=game_session.id,
            request_fingerprint=uuid.uuid4().hex,
            request_fen_hash=fen_hash(root_fen),
            uci_history="[]",
            ply_before=3,
            served_at=started_at,
            response_payload="{}",
            target_blunder_id=blunders["post"].id,
        )
    )
    # This is the correct post-boundary reach written by the old whole-session
    # implementation too; the repair must preserve it and therefore preserve the
    # targeted numerator, while retiring the separately seeded pre-root rows.
    db_session.add(
        BlunderOpportunityEvent(
            session_id=game_session.id,
            blunder_id=blunders["post"].id,
            occurred_at=started_at,
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()
    return user_id, game_session, blunders


def _copy_boundary_session(db_session, source: GameSession, *, session_int: int):
    copied = GameSession(
        id=uuid.UUID(int=session_int),
        user_id=source.user_id,
        started_at=source.started_at + timedelta(minutes=1),
        status="ended",
        result="drill_abandon",
        engine_elo=source.engine_elo,
        is_rated=False,
        player_color=source.player_color,
        session_mode="drill",
        drill_state="abandoned",
        drill_opening_key=source.drill_opening_key,
        drill_root_reached_ply=source.drill_root_reached_ply,
    )
    db_session.add(copied)
    db_session.flush()
    rows = (
        db_session.query(SessionMove)
        .filter(SessionMove.session_id == source.id)
        .order_by(SessionMove.move_number, SessionMove.color)
        .all()
    )
    for row in rows:
        db_session.add(
            SessionMove(
                session_id=copied.id,
                move_number=row.move_number,
                color=row.color,
                move_san=row.move_san,
                fen_before=row.fen_before,
                fen_after=row.fen_after,
                segment=row.segment,
            )
        )
    db_session.commit()
    return copied


def _event_for(db_session, *, session_id, blunder_id):
    return (
        db_session.query(BlunderOpportunityEvent)
        .filter_by(session_id=session_id, blunder_id=blunder_id)
        .first()
    )


def _add_stale_event(db_session, *, game_session, blunder):
    db_session.add(
        BlunderOpportunityEvent(
            session_id=game_session.id,
            blunder_id=blunder.id,
            occurred_at=game_session.started_at,
            opportunity=True,
            reached=True,
        )
    )
    db_session.commit()


def test_blunder_grain_modes_use_shared_seed_and_reach_roles(db_session):
    user_id, game_session, blunders = _boundary_case(db_session)
    _add_stale_event(
        db_session,
        game_session=game_session,
        blunder=blunders["pre_child"],
    )

    before = load_opportunity_counters(
        db_session,
        [blunders["post"].id],
        user_id=user_id,
    )[blunders["post"].id]
    targeted_before = (before.targeted_30d, before.targeted_reached_30d)

    sessions, opportunities, reached = recompute_one_blunder(
        db_session,
        blunder_id=blunders["pre_child"].id,
    )
    assert (sessions, opportunities, reached) == (1, 0, 0)
    assert (
        _event_for(
            db_session,
            session_id=game_session.id,
            blunder_id=blunders["pre_child"].id,
        )
        is None
    )

    recompute_all_blunders(db_session, user_id=user_id, progress_every=0)

    # The pre-root route position seeds nothing, while the root seeds its downstream
    # neighbour.
    assert (
        _event_for(
            db_session,
            session_id=game_session.id,
            blunder_id=blunders["pre_child"].id,
        )
        is None
    )
    downstream = _event_for(
        db_session,
        session_id=game_session.id,
        blunder_id=blunders["downstream"].id,
    )
    assert downstream is not None
    assert (downstream.opportunity, downstream.reached) == (True, False)

    after = load_opportunity_counters(
        db_session,
        [blunders["post"].id],
        user_id=user_id,
    )[blunders["post"].id]
    assert (after.targeted_30d, after.targeted_reached_30d) == targeted_before

    # A legacy drill whose boundary remains NULL contributes no broad evidence. The
    # blunder-grain path must retire the row it just wrote, never recreate it from the
    # whole route prefix.
    game_session.drill_root_reached_ply = None
    db_session.commit()
    recompute_all_blunders(db_session, user_id=user_id, progress_every=0)
    assert (
        db_session.query(BlunderOpportunityEvent)
        .filter_by(session_id=game_session.id)
        .count()
        == 0
    )


def test_session_recompute_retires_stale_rows_and_resumes_by_uuid(db_session):
    user_id, first, first_blunders = _boundary_case(db_session, session_int=200)
    second = _copy_boundary_session(db_session, first, session_int=201)
    second_blunders = first_blunders
    _add_stale_event(
        db_session,
        game_session=first,
        blunder=first_blunders["pre_child"],
    )
    _add_stale_event(
        db_session,
        game_session=second,
        blunder=second_blunders["pre_child"],
    )

    page_one = recompute_srs_opportunities(
        db_session,
        user_id=user_id,
        limit=1,
        progress_every=0,
    )
    assert page_one == type(page_one)(
        processed_sessions=1,
        last_session_id=first.id,
    )
    assert (
        _event_for(
            db_session,
            session_id=first.id,
            blunder_id=first_blunders["pre_child"].id,
        )
        is None
    )
    assert (
        _event_for(
            db_session,
            session_id=second.id,
            blunder_id=second_blunders["pre_child"].id,
        )
        is not None
    )

    page_two = recompute_srs_opportunities(
        db_session,
        user_id=user_id,
        after_session_id=page_one.last_session_id,
        limit=1,
        progress_every=0,
    )
    assert page_two.last_session_id == second.id
    assert (
        _event_for(
            db_session,
            session_id=second.id,
            blunder_id=second_blunders["pre_child"].id,
        )
        is None
    )

    # Starting over after an interruption is also a safe no-op.
    rerun = recompute_srs_opportunities(
        db_session,
        user_id=user_id,
        progress_every=0,
    )
    assert rerun.processed_sessions == 2


def test_each_recompute_cli_mode_executes_and_preserves_targeted_counters(
    db_session, capsys
):
    user_id, game_session, blunders = _boundary_case(db_session, session_int=300)
    downstream_id = blunders["downstream"].id
    targeted_id = blunders["post"].id
    game_session_id = game_session.id

    def targeted():
        counters = load_opportunity_counters(
            db_session,
            [targeted_id],
            user_id=user_id,
        )[targeted_id]
        return counters.targeted_30d, counters.targeted_reached_30d

    before = targeted()
    assert before == (1, 1)

    invocations = [
        ["--blunder-id", str(downstream_id), "--progress-every", "0"],
        [
            "--all-blunders",
            "--user-id",
            str(user_id),
            "--progress-every",
            "0",
        ],
        ["--session-id", str(game_session_id), "--progress-every", "0"],
        [
            "--all-sessions",
            "--user-id",
            str(user_id),
            "--started-before",
            datetime.now(timezone.utc).isoformat(),
            "--limit",
            "1",
            "--progress-every",
            "0",
        ],
    ]
    for argv in invocations:
        assert main(argv, session_factory=lambda: db_session) == 0
        assert targeted() == before

    output = capsys.readouterr().out
    assert output.count("opponent_decisions_written=false") == 4
    # Compare full field names: targeted_reached_30d remains part of the output.
    output_fields = {token.split("=", 1)[0] for token in output.split() if "=" in token}
    assert "opportunities_since_review" in output_fields
    assert "targeted_30d" in output_fields
    assert "targeted_reached_30d" in output_fields
    assert "opportunities_30d" not in output_fields
    assert "reached_30d" not in output_fields


def _freeze_user_evidence(db_session, *, user_id: int, through) -> None:
    """Put this user's whole history behind a permanent fold prefix.

    The prefix arm needs no policy switch, which is exactly the point: a fold
    prefix freezes evidence even with freezing and cleanup turned off, because
    the raw rows it covers no longer exist.
    """
    from app.models import User, UserOpportunityRetentionState
    from app.opportunity_store import ensure_retention_state

    if db_session.get(User, user_id) is None:
        db_session.add(User(id=user_id, username=None, is_anonymous=True))
        db_session.flush()
    ensure_retention_state(db_session, user_id)
    state = db_session.get(UserOpportunityRetentionState, user_id)
    state.folded_through_started_at = through
    db_session.flush()


@pytest.mark.parametrize(
    "mode",
    ["blunder-id", "all-blunders", "session-id", "all-sessions"],
)
def test_every_repair_mode_refuses_to_rewrite_frozen_evidence(
    db_session, capsys, mode
):
    """All four modes, guarded before any delete or upsert.

    Recompute is full replacement. Letting a frozen session through would DELETE
    rows a per-blunder summary has already absorbed and then recreate them, so a
    refusal has to happen before the write, not be detected after it.
    """
    user_id, game_session, blunders = _boundary_case(db_session, session_int=400)
    blunder_id = blunders["downstream"].id
    session_id = game_session.id

    def stored_events():
        return {
            (event.session_id, event.blunder_id, event.opportunity, event.reached)
            for event in db_session.query(BlunderOpportunityEvent).all()
        }

    # Establish the unfrozen result first, so the frozen run is compared against
    # a real rewrite rather than against an empty table.
    assert main(
        ["--all-sessions", "--user-id", str(user_id), "--started-before",
         datetime.now(timezone.utc).isoformat(), "--progress-every", "0"],
        session_factory=lambda: db_session,
    ) == 0
    before = stored_events()
    assert before, "expected the unfrozen repair to write evidence"

    _freeze_user_evidence(
        db_session, user_id=user_id, through=datetime.now(timezone.utc)
    )
    # Remove one row so an unguarded rerun would visibly recreate it.
    db_session.query(BlunderOpportunityEvent).filter(
        BlunderOpportunityEvent.blunder_id == blunder_id
    ).delete(synchronize_session=False)
    db_session.flush()
    after_removal = stored_events()
    assert after_removal != before

    argv = {
        "blunder-id": ["--blunder-id", str(blunder_id)],
        "all-blunders": ["--all-blunders", "--user-id", str(user_id)],
        "session-id": ["--session-id", str(session_id)],
        "all-sessions": [
            "--all-sessions", "--user-id", str(user_id),
            "--started-before", datetime.now(timezone.utc).isoformat(),
        ],
    }[mode]
    assert main([*argv, "--progress-every", "0"], session_factory=lambda: db_session) == 0

    assert stored_events() == after_removal
    if mode in ("session-id", "all-sessions"):
        assert "frozen_sessions=1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "mode",
    ["blunder-id", "all-blunders", "session-id", "all-sessions"],
)
def test_every_repair_mode_checks_the_freeze_behind_the_user_lock(
    db_session, monkeypatch, mode
):
    """The freeze check is only sound while the per-user graph write lock is held.

    The event guards cover DELETE alone, so an unserialized repair can pass the
    freeze check and then have a fold commit underneath it before its upsert —
    recreating rows a summary has already absorbed. Upload and the deferred
    worker already hold this lock; the repair modes were the hole.
    """
    import scripts.recompute_srs_opportunities as script

    user_id, game_session, blunders = _boundary_case(db_session, session_int=401)
    trace: list[tuple[str, int | None]] = []

    def record_lock(db, *, user_id, dialect_name):
        trace.append(("lock", user_id))

    real_frozen = script.frozen_session_ids
    real_compute = script._compute_blunder_opportunity_events

    def record_frozen(db, *, user_id, session_ids):
        trace.append(("freeze_check", user_id))
        return real_frozen(db, user_id=user_id, session_ids=session_ids)

    def record_compute(db, *, session_id, user_id, player_color):
        trace.append(("freeze_check", user_id))
        return real_compute(
            db, session_id=session_id, user_id=user_id, player_color=player_color
        )

    monkeypatch.setattr(script, "acquire_graph_write_lock", record_lock)
    monkeypatch.setattr(script, "frozen_session_ids", record_frozen)
    monkeypatch.setattr(script, "_compute_blunder_opportunity_events", record_compute)

    argv = {
        "blunder-id": ["--blunder-id", str(blunders["downstream"].id)],
        "all-blunders": ["--all-blunders", "--user-id", str(user_id)],
        "session-id": ["--session-id", str(game_session.id)],
        "all-sessions": [
            "--all-sessions", "--user-id", str(user_id),
            "--started-before", datetime.now(timezone.utc).isoformat(),
        ],
    }[mode]
    assert script.main(
        [*argv, "--progress-every", "0"], session_factory=lambda: db_session
    ) == 0

    assert ("freeze_check", user_id) in trace
    # Ordering, not merely presence: a lock taken after the check protects
    # nothing. Every freeze check must be preceded by a lock for its own owner.
    for index, (kind, owner) in enumerate(trace):
        if kind != "freeze_check":
            continue
        assert ("lock", owner) in trace[:index]
