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


# --------------------------------------------------------------------------
# The fold manifest and the finite recovery window (g-srs-fold-recovery)
# --------------------------------------------------------------------------

MANIFEST = "20260920_01"
RECOVERY_TABLES = ("opportunity_fold_batches",)
FOLD_BATCH_CHECKS = {
    "ck_opportunity_fold_batch_rows",
    "ck_opportunity_fold_batch_expiry",
}


def test_the_manifest_model_declares_its_invariants():
    from app.models import OpportunityFoldBatch

    checks = {
        constraint.name
        for constraint in OpportunityFoldBatch.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert FOLD_BATCH_CHECKS <= checks


def test_a_fresh_model_driven_schema_creates_the_manifest_and_the_anchor():
    from app.models import OpportunityRetentionPolicy as Policy

    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        inspector = inspect(engine)
        assert set(RECOVERY_TABLES) <= set(inspector.get_table_names())
        columns = {c["name"] for c in inspector.get_columns(Policy.__tablename__)}
        assert "first_fold_committed_at" in columns
    finally:
        engine.dispose()


@pg_gate
def test_the_manifest_migration_is_additive_and_leaves_the_anchor_null(
    pg_migration_db, monkeypatch
):
    """Nothing has been folded on a database that just migrated, and it says so.

    A non-NULL anchor would start the seven-day rollback clock on a deployment
    that has deleted nothing, which is the one way this column can do harm.
    """
    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    command.upgrade(config(), MANIFEST)
    engine = create_engine(pg_migration_db)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            assert set(RECOVERY_TABLES) <= set(inspector.get_table_names())
            assert conn.execute(text(
                "SELECT first_fold_committed_at FROM opportunity_retention_policy "
                "WHERE id = 1"
            )).scalar() is None
            assert conn.execute(text(
                "SELECT count(*) FROM opportunity_fold_batches"
            )).scalar_one() == 0
    finally:
        engine.dispose()


def _pg_history(engine, *, user_id: int, tmp_path):
    """A migrated PostgreSQL database with real foldable history on it.

    Two blunders, three old sessions each with one raw row, one of which carries
    a NULL ``occurred_at`` — the legacy shape a restore has to reproduce exactly
    rather than normalize to ``created_at``.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session as OrmSession

    from app.fen import fen_hash
    from app.models import (
        Blunder,
        BlunderOpportunityEvent,
        BlunderOpportunitySummary,
        GameSession,
        OpportunityRetentionPolicy as Policy,
        Position,
        User,
    )

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=120)
    fens = ("8/8/8/8/8/8/8/K6k w - - 0 1", "8/8/8/8/8/8/K6k/8 w - - 0 1")
    with OrmSession(bind=engine) as db:
        db.add(User(id=user_id, username=None, is_anonymous=True))
        db.flush()
        blunder_ids = []
        for index, fen in enumerate(fens):
            position = Position(user_id=user_id, fen_hash=fen_hash(fen),
                                fen_raw=fen, active_color="white")
            db.add(position)
            db.flush()
            blunder = Blunder(
                user_id=user_id, position_id=position.id, bad_move_san="bad",
                best_move_san="good", eval_loss_cp=200,
                created_at=now - timedelta(days=365),
            )
            db.add(blunder)
            db.flush()
            db.add(BlunderOpportunitySummary(blunder_id=blunder.id))
            blunder_ids.append(blunder.id)
            for step in range(3):
                game_session = GameSession(
                    id=uuid.uuid4(), user_id=user_id,
                    started_at=old - timedelta(days=index * 10 + step),
                    status="completed", engine_elo=1500, player_color="white",
                )
                db.add(game_session)
                db.flush()
                db.add(BlunderOpportunityEvent(
                    blunder_id=blunder.id, session_id=game_session.id,
                    # The last row of each blunder is a legacy NULL.
                    occurred_at=None if step == 2 else game_session.started_at,
                    opportunity=True, reached=step == 0,
                ))
        policy = db.get(Policy, 1)
        policy.readiness = True
        policy.freeze_enabled = True
        policy.cleanup_enabled = True
        db.commit()
    return blunder_ids, now


def _raw_rows(engine):
    """Every raw event row, as plain tuples, on the caller's engine.

    The caller's engine, not a fresh one: an engine built here would keep its own
    pool alive past the `with`, and the connection in it is only closed when the
    interpreter gets round to collecting the engine.
    """
    with engine.connect() as conn:
        return {
            (row.id, str(row.session_id), row.blunder_id, row.occurred_at,
             row.created_at, row.opportunity, row.reached)
            for row in conn.execute(text(
                "SELECT id, session_id, blunder_id, occurred_at, created_at, "
                "opportunity, reached FROM blunder_opportunity_events"
            ))
        }


@pg_gate
def test_a_full_rollback_restores_raw_history_alongside_later_writes(
    pg_migration_db, monkeypatch, tmp_path
):
    """The seven-day rehearsal, end to end, on the real schema and triggers.

    Fold; let the world move on (a later write, a review, a purged blunder);
    restore; and only then is the schema downgrade allowed to run. Each of those
    three intervening events is a different way a naive restore goes wrong — by
    overwriting, by double-subtracting, and by resurrecting.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session as OrmSession

    from app.models import (
        Blunder,
        BlunderOpportunityEvent,
        BlunderOpportunitySummary,
        BlunderReview,
        GameSession,
        OpportunityFoldBatch,
        OpportunityRetentionPolicy as Policy,
        UserOpportunityRetentionState,
    )
    from app.opportunity_fold import FoldLimits, sweep
    from app.opportunity_fold_recovery import restore_folded_evidence
    from app.opportunity_store import load_policy, record_review_basis
    from app.srs_opportunity import load_opportunity_counters

    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    monkeypatch.setenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR", str(tmp_path / "exports"))
    cfg = config()
    # This rehearsal runs today's ORM/writers before downgrading. Schema-only
    # migration tests above remain pinned to their historical revisions.
    command.upgrade(cfg, "head")
    engine = create_engine(pg_migration_db)
    user_id = 8841
    try:
        blunder_ids, now = _pg_history(engine, user_id=user_id, tmp_path=tmp_path)
        original = _raw_rows(engine)
        with OrmSession(bind=engine) as db:
            before = load_opportunity_counters(
                db, blunder_ids, user_id=user_id, now=now)

        report = sweep(
            engine, user_ids=[user_id],
            limits=FoldLimits(transaction_deadline=30.0, user_cooldown=0.0),
        )
        assert report.rows_deleted == 6
        assert _raw_rows(engine) == set()

        with OrmSession(bind=engine) as db:
            assert db.get(Policy, 1).first_fold_committed_at is not None
            assert db.get(UserOpportunityRetentionState, user_id).folded_through_started_at
            # Counters are unchanged by the storage move.
            assert load_opportunity_counters(
                db, blunder_ids, user_id=user_id, now=now) == before

            # 1. A later write, in a session young enough to still accept one.
            recent = GameSession(
                id=uuid.uuid4(), user_id=user_id,
                started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                status="completed", engine_elo=1500, player_color="white",
            )
            db.add(recent)
            db.flush()
            db.add(BlunderOpportunityEvent(
                blunder_id=blunder_ids[0], session_id=recent.id,
                occurred_at=recent.started_at, opportunity=True, reached=True,
            ))
            # 2. A review, which zeroes that summary's since-review counters and
            #    moves its basis out from under the manifest.
            review = BlunderReview(
                blunder_id=blunder_ids[0], session_id=recent.id,
                reviewed_at=datetime.now(timezone.utc), passed=True,
                move_played_san="good", eval_delta_cp=0,
            )
            db.add(review)
            db.flush()
            record_review_basis(
                db, blunder_id=blunder_ids[0], review_id=review.id,
                reviewed_at=review.reviewed_at, session_id=recent.id,
                policy=load_policy(db),
            )
            # 3. A purge. The event guard lets this cascade through because the
            #    parent blunder is already gone by the time it fires.
            db.query(Blunder).filter_by(id=blunder_ids[1]).delete()
            db.get(Policy, 1).cleanup_enabled = False
            db.commit()

        restored = restore_folded_evidence(engine)

        assert restored.batches >= 1
        assert restored.rows_restored == 3
        assert restored.rows_skipped_missing_parent == 3
        assert restored.since_review_left_alone == 1
        surviving = {row for row in original if row[2] == blunder_ids[0]}
        # Exact facts, NULL occurred_at included, plus the later write.
        assert surviving <= _raw_rows(engine)
        assert len(_raw_rows(engine)) == 4

        with OrmSession(bind=engine) as db:
            assert db.get(Blunder, blunder_ids[1]) is None
            summary = db.get(BlunderOpportunitySummary, blunder_ids[0])
            assert summary.folded_eligible_count == 0
            assert summary.folded_opportunities_since_review == 0
            assert db.get(UserOpportunityRetentionState, user_id).folded_through_started_at is None
            assert all(batch.restored_at is not None
                       for batch in db.query(OpportunityFoldBatch))
            # The id generator was moved past the restored rows: a fresh insert
            # must not collide with an id that came back from the export.
            another = GameSession(
                id=uuid.uuid4(), user_id=user_id,
                started_at=datetime.now(timezone.utc), status="completed",
                engine_elo=1500, player_color="white",
            )
            db.add(another)
            db.flush()
            db.add(BlunderOpportunityEvent(
                blunder_id=blunder_ids[0], session_id=another.id,
                occurred_at=another.started_at, opportunity=True, reached=False,
            ))
            db.commit()

        # Only now, with nothing unrestored and every total back at zero, may the
        # schema go back.
        command.downgrade(cfg, GUARDS)
        with engine.connect() as conn:
            assert set(RECOVERY_TABLES).isdisjoint(set(inspect(conn).get_table_names()))
    finally:
        engine.dispose()


@pg_gate
def test_the_downgrade_refuses_while_rows_are_deleted_and_after_the_window(
    pg_migration_db, monkeypatch, tmp_path
):
    """Two refusals, and only one of them is temporary.

    While a batch is unrestored the manifest is the only pointer to the export
    that could refill its rows, so dropping it would strand them. After the
    window the exports are gone regardless, and a raw-history downgrade stops
    being a possible rollback target at all.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.orm import Session as OrmSession

    from app.opportunity_fold import FoldLimits, fold_user_batch
    from app.opportunity_fold_recovery import RecoveryExpired, restore_folded_evidence
    from app.models import OpportunityRetentionPolicy as Policy

    monkeypatch.setenv("DATABASE_URL", pg_migration_db)
    monkeypatch.setenv("GHOSTREPLAY_SRS_FOLD_EXPORT_DIR", str(tmp_path / "exports"))
    cfg = config()
    # This rehearsal runs today's ORM/writers before downgrading. Schema-only
    # migration tests above remain pinned to their historical revisions.
    command.upgrade(cfg, "head")
    engine = create_engine(pg_migration_db)
    user_id = 8842
    try:
        _pg_history(engine, user_id=user_id, tmp_path=tmp_path)
        assert fold_user_batch(
            engine, user_id=user_id,
            limits=FoldLimits(transaction_deadline=30.0),
        ).rows_deleted > 0

        with pytest.raises(RuntimeError, match="unrestored SRS fold batches"):
            command.downgrade(cfg, GUARDS)

        with OrmSession(bind=engine) as db:
            db.get(Policy, 1).cleanup_enabled = False
            db.get(Policy, 1).first_fold_committed_at = (
                datetime.now(timezone.utc) - timedelta(days=8)
            )
            db.commit()

        with pytest.raises(RecoveryExpired, match="recovery window closed"):
            restore_folded_evidence(engine)
        with pytest.raises(RuntimeError, match="recovery window closed"):
            command.downgrade(cfg, GUARDS)
    finally:
        engine.dispose()
