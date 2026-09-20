"""Additive retention storage: migration shape, eager zero backfill, model parity.

Folding and recovery are NOT covered here — those belong to g-srs-fold-recovery.
What this file proves is that the storage arrives additively, that every existing
blunder and user gets its ZERO row (so a later missing row is unambiguous
evidence of loss rather than of a partial rollout), that the backfill is
re-runnable and never overwrites a concurrent writer's values, and that a
downgrade refuses rather than destroying folded totals that cannot be recomputed.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.models import (
    Base,
    BlunderOpportunitySummary,
    OpportunityRetentionPolicy,
)
from app.opportunity_retention import (
    DEFAULT_GRACE_SECONDS,
    DEFAULT_MUTATION_WINDOW_DAYS,
)
from pg_gate_plugin import pg_gate

PREVIOUS = "20260919_03"
STORAGE = "20260919_04"
GUARDS = "20260919_05"

TABLES = (
    "opportunity_retention_policy",
    "user_opportunity_retention_state",
    "blunder_opportunity_summaries",
)
SUMMARY_CHECKS = {
    "ck_blunder_opportunity_summary_nonnegative",
    "ck_blunder_opportunity_summary_reached_within_opportunities",
    "ck_blunder_opportunity_summary_since_review_within_lifetime",
}
POLICY_CHECKS = {
    "ck_opportunity_retention_policy_singleton",
    "ck_opportunity_retention_policy_window",
    "ck_opportunity_retention_policy_grace",
    "ck_opportunity_retention_policy_version",
    "ck_opportunity_retention_policy_ready_before_freeze",
    "ck_opportunity_retention_policy_freeze_before_cleanup",
}


def config():
    backend = Path(__file__).resolve().parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return cfg


# --------------------------------------------------------------------------
# Model / handwritten-schema parity
# --------------------------------------------------------------------------


def test_the_models_declare_every_retention_invariant():
    """The checks are the invariants; losing one from the model loses it for real."""
    summary_checks = {
        constraint.name
        for constraint in BlunderOpportunitySummary.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    policy_checks = {
        constraint.name
        for constraint in OpportunityRetentionPolicy.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert SUMMARY_CHECKS <= summary_checks
    assert POLICY_CHECKS <= policy_checks


def test_a_fresh_model_driven_schema_creates_all_three_tables():
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        assert set(TABLES) <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"folded_eligible_count": -1},
        {"folded_opportunities_since_review": 1, "folded_reached_since_review": 2,
         "folded_eligible_count": 5},
        {"folded_opportunities_since_review": 6, "folded_eligible_count": 5},
    ],
    ids=["negative", "reached-exceeds-opportunities", "since-review-exceeds-lifetime"],
)
def test_the_handwritten_test_schema_enforces_the_same_summary_invariants(
    db_session, values
):
    """The SQLite suite must run the real shape, not a permissive stand-in."""
    from app.api.blunder import _upsert_blunder_target
    from app.fen import fen_hash
    from app.models import Position, User

    user = User(id=8801, username=None, is_anonymous=True)
    db_session.add(user)
    db_session.flush()
    fen = "8/8/8/8/8/8/8/8 w - - 0 1"
    position = Position(
        user_id=user.id, fen_hash=fen_hash(fen), fen_raw=fen, active_color="w"
    )
    db_session.add(position)
    db_session.flush()
    blunder_id, _ = _upsert_blunder_target(
        db_session, user_id=user.id, position_id=position.id,
        user_move="a3", best_move="e4", eval_loss=200,
    )

    summary = db_session.get(BlunderOpportunitySummary, blunder_id)
    for key, value in values.items():
        setattr(summary, key, value)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# --------------------------------------------------------------------------
# Migration behaviour on a real PostgreSQL database
# --------------------------------------------------------------------------


@pg_gate
def test_retention_storage_migration_backfills_zero_rows(pg_migration_db, monkeypatch):
    """Every pre-existing blunder and user must arrive with a ZERO row.

    Eager creation is the whole loss-detection mechanism: a lazy COALESCE(...,0)
    reader cannot tell "never folded" from "summary lost after raw deletion".
    """
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = config()
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(pg_migration_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, is_anonymous) VALUES (8811, true), (8812, true)"
                )
            )
            position_id = conn.execute(
                text(
                    "INSERT INTO positions (user_id, fen_hash, fen_raw, active_color) "
                    "VALUES (8811, 'mig-fen', '8/8/8/8/8/8/8/8 w - - 0 1', 'white') "
                    "RETURNING id"
                )
            ).scalar_one()
            blunder_id = conn.execute(
                text(
                    "INSERT INTO blunders (user_id, position_id, bad_move_san, "
                    "best_move_san, eval_loss_cp) VALUES (8811, :p, 'Qh5', 'Nf3', 120) "
                    "RETURNING id"
                ),
                {"p": position_id},
            ).scalar_one()

        command.upgrade(cfg, STORAGE)

        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT folded_eligible_count, folded_opportunities_since_review, "
                    "folded_reached_since_review, latest_review_id, policy_version "
                    "FROM blunder_opportunity_summaries WHERE blunder_id = :b"
                ),
                {"b": blunder_id},
            ).one() == (0, 0, 0, None, 1)
            assert conn.execute(
                text(
                    "SELECT count(*) FROM user_opportunity_retention_state "
                    "WHERE user_id IN (8811, 8812)"
                )
            ).scalar_one() == 2
            # Nothing is frozen and nothing is enabled by arriving, and the
            # horizon that arrives is the DECIDED one. Asserted against the
            # constants rather than literals so the migration's server_default
            # and app.opportunity_retention cannot drift apart: an operator who
            # flips the switches without also setting M must get M = 60, never
            # the 30 days this project rejected.
            assert conn.execute(
                text(
                    "SELECT mutation_window_days, grace_seconds, version, "
                    "freeze_enabled, cleanup_enabled, readiness "
                    "FROM opportunity_retention_policy WHERE id = 1"
                )
            ).one() == (
                DEFAULT_MUTATION_WINDOW_DAYS,
                DEFAULT_GRACE_SECONDS,
                1,
                False,
                False,
                False,
            )
            assert conn.execute(
                text(
                    "SELECT count(*) FROM user_opportunity_retention_state "
                    "WHERE folded_through_started_at IS NOT NULL"
                )
            ).scalar_one() == 0
    finally:
        engine.dispose()


@pg_gate
def test_the_backfill_stamps_the_review_basis_of_already_reviewed_blunders(
    pg_migration_db, monkeypatch
):
    """A basis-less backfill makes flipping readiness a 500 on the ghost-move path.

    After readiness the reader compares the summary's ``latest_review_id`` with
    the blunder's live latest review and raises when they disagree. NULL
    disagrees with every real review, so a backfill that inserts only
    ``blunder_id`` breaks every blunder reviewed before this deploy — on the
    path that selects ghost moves.
    """
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = config()
    command.upgrade(cfg, PREVIOUS)
    engine = create_engine(pg_migration_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, is_anonymous) VALUES (8841, true)"))
            position_id = conn.execute(
                text(
                    "INSERT INTO positions (user_id, fen_hash, fen_raw, active_color) "
                    "VALUES (8841, 'mig-fen-4', '8/8/8/8/8/8/8/8 w - - 0 1', 'white') "
                    "RETURNING id"
                )
            ).scalar_one()
            blunder_id = conn.execute(
                text(
                    "INSERT INTO blunders (user_id, position_id, bad_move_san, "
                    "best_move_san, eval_loss_cp) VALUES (8841, :p, 'Qh5', 'Nf3', 120) "
                    "RETURNING id"
                ),
                {"p": position_id},
            ).scalar_one()
            session_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO game_sessions (id, user_id, started_at, status, "
                    "engine_elo, player_color) "
                    "VALUES (:s, 8841, now(), 'completed', 1500, 'white')"
                ),
                {"s": session_id},
            )
            # Two reviews an hour apart: the backfill must pick the LATER one,
            # ranked exactly as load_opportunity_counters ranks it.
            conn.execute(
                text(
                    "INSERT INTO blunder_reviews (blunder_id, session_id, reviewed_at, "
                    "passed, move_played_san, eval_delta_cp) VALUES "
                    "(:b, :s, now() - interval '1 hour', true, 'Nf3', 0)"
                ),
                {"b": blunder_id, "s": session_id},
            )
            newest_id = conn.execute(
                text(
                    "INSERT INTO blunder_reviews (blunder_id, session_id, reviewed_at, "
                    "passed, move_played_san, eval_delta_cp) VALUES "
                    "(:b, :s, now(), true, 'Nf3', 0) RETURNING id"
                ),
                {"b": blunder_id, "s": session_id},
            ).scalar_one()

        command.upgrade(cfg, STORAGE)

        with engine.connect() as conn:
            stored_review, stored_session = conn.execute(
                text(
                    "SELECT latest_review_id, latest_review_session_id "
                    "FROM blunder_opportunity_summaries WHERE blunder_id = :b"
                ),
                {"b": blunder_id},
            ).one()
            assert stored_review == newest_id
            assert stored_session == session_id
    finally:
        engine.dispose()


@pg_gate
def test_the_backfill_never_overwrites_a_concurrent_writers_values(
    pg_migration_db, monkeypatch
):
    """A review or blunder insert may legitimately create the row first.

    Re-running the whole migration chain is the strongest available stand-in for
    that race: the second pass finds rows already present and must leave their
    values alone rather than resetting them to zero.
    """
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = config()
    command.upgrade(cfg, STORAGE)
    engine = create_engine(pg_migration_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, is_anonymous) VALUES (8821, true)"))
            position_id = conn.execute(
                text(
                    "INSERT INTO positions (user_id, fen_hash, fen_raw, active_color) "
                    "VALUES (8821, 'mig-fen-2', '8/8/8/8/8/8/8/8 w - - 0 1', 'white') "
                    "RETURNING id"
                )
            ).scalar_one()
            blunder_id = conn.execute(
                text(
                    "INSERT INTO blunders (user_id, position_id, bad_move_san, "
                    "best_move_san, eval_loss_cp) VALUES (8821, :p, 'Qh5', 'Nf3', 120) "
                    "RETURNING id"
                ),
                {"p": position_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO blunder_opportunity_summaries "
                    "(blunder_id, folded_eligible_count, "
                    " folded_opportunities_since_review, folded_reached_since_review) "
                    "VALUES (:b, 9, 4, 2)"
                ),
                {"b": blunder_id},
            )
            conn.execute(
                text(
                    "INSERT INTO user_opportunity_retention_state "
                    "(user_id, folded_through_started_at) VALUES (8821, now())"
                )
            )

        command.upgrade(cfg, GUARDS)
        # Re-run the backfill statement itself. Re-running the whole migration
        # is a no-op once alembic_version is at head, so replaying the exact
        # INSERT ... NOT EXISTS ... ON CONFLICT DO NOTHING is what actually
        # exercises the idempotence and the concurrent-writer race.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO blunder_opportunity_summaries (blunder_id) "
                    "SELECT b.id FROM blunders b WHERE NOT EXISTS ("
                    "  SELECT 1 FROM blunder_opportunity_summaries s "
                    "  WHERE s.blunder_id = b.id) ON CONFLICT DO NOTHING"
                )
            )

        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT folded_eligible_count, folded_opportunities_since_review, "
                    "folded_reached_since_review FROM blunder_opportunity_summaries "
                    "WHERE blunder_id = :b"
                ),
                {"b": blunder_id},
            ).one() == (9, 4, 2)
            assert conn.execute(
                text(
                    "SELECT folded_through_started_at IS NOT NULL "
                    "FROM user_opportunity_retention_state WHERE user_id = 8821"
                )
            ).scalar_one() is True
    finally:
        engine.dispose()


@pg_gate
def test_downgrade_refuses_to_destroy_folded_evidence(pg_migration_db, monkeypatch):
    """Nonzero totals are the ONLY remaining record of physically deleted rows."""
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    cfg = config()
    command.upgrade(cfg, GUARDS)
    engine = create_engine(pg_migration_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, is_anonymous) VALUES (8831, true)"))
            position_id = conn.execute(
                text(
                    "INSERT INTO positions (user_id, fen_hash, fen_raw, active_color) "
                    "VALUES (8831, 'mig-fen-3', '8/8/8/8/8/8/8/8 w - - 0 1', 'white') "
                    "RETURNING id"
                )
            ).scalar_one()
            blunder_id = conn.execute(
                text(
                    "INSERT INTO blunders (user_id, position_id, bad_move_san, "
                    "best_move_san, eval_loss_cp) VALUES (8831, :p, 'Qh5', 'Nf3', 120) "
                    "RETURNING id"
                ),
                {"p": position_id},
            ).scalar_one()
            # Stands in for evidence that was physically deleted: these totals
            # are its only surviving record.
            conn.execute(
                text(
                    "INSERT INTO blunder_opportunity_summaries "
                    "(blunder_id, folded_eligible_count) VALUES (:b, 3)"
                ),
                {"b": blunder_id},
            )

        with pytest.raises(RuntimeError, match="recover before downgrade"):
            command.downgrade(cfg, PREVIOUS)

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE blunder_opportunity_summaries SET folded_eligible_count = 0")
            )
            conn.execute(
                text(
                    "INSERT INTO user_opportunity_retention_state "
                    "(user_id, folded_through_started_at) VALUES (8831, now())"
                )
            )
        with pytest.raises(RuntimeError, match="recover before downgrade"):
            command.downgrade(cfg, PREVIOUS)

        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE user_opportunity_retention_state "
                    "SET folded_through_started_at = NULL"
                )
            )
        command.downgrade(cfg, PREVIOUS)
        with engine.connect() as conn:
            remaining = set(inspect(conn).get_table_names())
        assert remaining.isdisjoint(TABLES)
    finally:
        engine.dispose()
