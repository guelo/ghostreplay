from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from app.evidence_boundary_backfill import run_boundary_backfill
from app.models import GameSession, SessionMove
from scripts.backfill_drill_evidence_boundaries import main, parse_args

BEFORE = "7k/8/8/8/8/8/8/K7 w - - 0 1"
TARGET = "7k/8/8/8/8/8/8/1K6 b - - 1 1"
AFTER = "6k1/8/8/8/8/8/8/1K6 w - - 2 2"
OTHER = "6k1/8/8/8/8/8/8/2K5 b - - 3 2"


def _drill(
    db_session,
    *,
    session_int: int,
    started_at: datetime,
    target: str | None = TARGET,
    root_ply: int | None = None,
    moves: list[tuple[int, str, str | None, str | None]] | None = None,
) -> GameSession:
    game_session = GameSession(
        id=uuid.UUID(int=session_int),
        user_id=77,
        started_at=started_at,
        status="ended",
        result="drill_abandon",
        engine_elo=1500,
        is_rated=False,
        player_color="white",
        session_mode="drill",
        drill_state="abandoned",
        drill_opening_key=target,
        drill_root_reached_ply=root_ply,
    )
    db_session.add(game_session)
    db_session.flush()
    for move_number, color, fen_before, fen_after in moves or []:
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
    return game_session


def test_reconstructs_fen_after_and_fen_before_and_keeps_earliest_arrival(db_session):
    now = datetime.now(timezone.utc)
    after_only = _drill(
        db_session,
        session_int=1,
        started_at=now,
        moves=[(2, "white", BEFORE, TARGET)],
    )
    before_only = _drill(
        db_session,
        session_int=2,
        started_at=now,
        moves=[(2, "white", TARGET, AFTER)],
    )
    repeated = _drill(
        db_session,
        session_int=3,
        started_at=now,
        moves=[
            (2, "white", BEFORE, TARGET),  # target at ply 3
            (4, "white", OTHER, TARGET),  # same normalized target at ply 7
        ],
    )
    db_session.commit()

    report = run_boundary_backfill(
        db_session,
        started_before=now + timedelta(seconds=1),
        progress_every=0,
    )

    assert report.stamped == 3
    assert report.remaining_null == 0
    assert after_only.drill_root_reached_ply == 3
    assert before_only.drill_root_reached_ply == 2
    assert repeated.drill_root_reached_ply == 3


def test_reports_null_residue_and_is_write_once(db_session):
    now = datetime.now(timezone.utc)
    stamped = _drill(
        db_session,
        session_int=10,
        started_at=now,
        root_ply=9,
        moves=[(2, "white", BEFORE, TARGET)],
    )
    missing = _drill(
        db_session,
        session_int=11,
        started_at=now,
        target=None,
        moves=[(2, "white", BEFORE, TARGET)],
    )
    invalid = _drill(
        db_session,
        session_int=12,
        started_at=now,
        target="not-a-fen",
        moves=[(2, "white", BEFORE, TARGET)],
    )
    not_observed = _drill(
        db_session,
        session_int=13,
        started_at=now,
        moves=[(2, "white", BEFORE, AFTER)],
    )
    db_session.commit()

    report = run_boundary_backfill(
        db_session,
        started_before=now + timedelta(seconds=1),
        progress_every=0,
    )
    second = run_boundary_backfill(
        db_session,
        started_before=now + timedelta(seconds=1),
        progress_every=0,
    )

    assert report.already_stamped == 1
    assert report.missing_target == 1
    assert report.invalid_target == 1
    assert report.target_not_observed == 1
    assert report.unreconstructable == 3
    assert report.remaining_null == 3
    assert second.stamped == 0
    assert second.already_stamped == 1
    assert second.remaining_null == 3
    assert stamped.drill_root_reached_ply == 9
    assert missing.drill_root_reached_ply is None
    assert invalid.drill_root_reached_ply is None
    assert not_observed.drill_root_reached_ply is None


def test_legacy_cutoff_limit_and_uuid_checkpoint_resume(db_session):
    cutoff = datetime.now(timezone.utc)
    first = _drill(
        db_session,
        session_int=20,
        started_at=cutoff - timedelta(days=1),
        moves=[(2, "white", BEFORE, TARGET)],
    )
    second = _drill(
        db_session,
        session_int=21,
        started_at=cutoff - timedelta(days=1),
        moves=[(2, "white", BEFORE, TARGET)],
    )
    new_session = _drill(
        db_session,
        session_int=22,
        started_at=cutoff + timedelta(seconds=1),
        moves=[(2, "white", BEFORE, TARGET)],
    )
    db_session.commit()

    page_one = run_boundary_backfill(
        db_session,
        started_before=cutoff,
        limit=1,
        progress_every=0,
    )
    page_two = run_boundary_backfill(
        db_session,
        started_before=cutoff,
        after_session_id=page_one.last_session_id,
        limit=1,
        progress_every=0,
    )

    assert page_one.last_session_id == first.id
    assert page_one.remaining_null == 1
    assert page_two.last_session_id == second.id
    assert page_two.remaining_null == 0
    assert first.drill_root_reached_ply == 3
    assert second.drill_root_reached_ply == 3
    assert new_session.drill_root_reached_ply is None


def test_boundary_cli_executes_all_and_single_session_modes(db_session, capsys):
    cutoff = datetime.now(timezone.utc)
    all_mode = _drill(
        db_session,
        session_int=30,
        started_at=cutoff - timedelta(days=1),
        moves=[(2, "white", BEFORE, TARGET)],
    )
    single_mode = _drill(
        db_session,
        session_int=31,
        started_at=cutoff + timedelta(days=1),
        moves=[(2, "white", BEFORE, TARGET)],
    )
    db_session.commit()
    all_mode_id = all_mode.id
    single_mode_id = single_mode.id

    assert (
        main(
            [
                "--all-sessions",
                "--started-before",
                cutoff.isoformat(),
                "--limit",
                "1",
                "--progress-every",
                "0",
            ],
            session_factory=lambda: db_session,
        )
        == 0
    )
    assert (
        main(
            [
                "--session-id",
                str(single_mode_id),
                "--progress-every",
                "0",
            ],
            session_factory=lambda: db_session,
        )
        == 0
    )

    all_mode = db_session.get(GameSession, all_mode_id)
    single_mode = db_session.get(GameSession, single_mode_id)
    assert all_mode is not None and all_mode.drill_root_reached_ply == 3
    assert single_mode is not None and single_mode.drill_root_reached_ply == 3
    output = capsys.readouterr().out
    assert output.count("Boundary backfill committed:") == 2
    assert "remaining_null=0" in output


def test_boundary_cli_requires_cutoff_and_scopes_resume_options():
    with pytest.raises(SystemExit):
        parse_args(["--all-sessions"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--session-id",
                str(uuid.uuid4()),
                "--after-session-id",
                str(uuid.uuid4()),
            ]
        )
