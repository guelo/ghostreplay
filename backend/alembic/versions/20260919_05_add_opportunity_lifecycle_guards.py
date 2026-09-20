"""Protect folded SRS opportunity evidence from deletion (PostgreSQL only).

Three database-side guards, none of which takes an advisory lock:

* ``game_sessions`` BEFORE DELETE — refuses to delete a session whose
  opportunity evidence is frozen, which also stops the cascade that would take
  its event rows with it.
* ``blunder_opportunity_events`` BEFORE DELETE — refuses to delete an individual
  frozen event row, while allowing the two legitimate deletions: a bounded fold
  transfer, and the cascade from deleting the parent blunder.
* ``user_opportunity_retention_state`` AFTER DELETE, DEFERRABLE INITIALLY
  DEFERRED — asserts at COMMIT that a whole-user purge actually removed every
  piece of that owner's opportunity state, so a partial purge rolls back instead
  of leaving orphaned summaries behind a deleted prefix.

The purge and fold escapes are TRANSACTION-LOCAL settings
(``set_config(..., true)``), read with ``current_setting(name, true)`` so an
unset marker yields NULL instead of erroring. They are an accidental-misuse
guard, not authorization: a privileged administrator running arbitrary SQL can
set them too. They bypass ONLY these new freeze guards, never any existing
foreign key or check constraint, and ``session_replication_role`` is not used.

SQLite gets no triggers. It is a test dialect with no equivalent semantics, and
the shared-policy layer in ``app.opportunity_retention`` is what it covers.

Revision ID: 20260919_05
Revises: 20260919_04
"""

from alembic import op

revision = "20260919_05"
down_revision = "20260919_04"
branch_labels = None
depends_on = None

PURGE_MODE = "ghostreplay.srs_purge_mode"
PURGE_USER = "ghostreplay.srs_purge_user_id"
FOLD_MODE = "ghostreplay.srs_fold_mode"

SESSION_GUARD = f"""
CREATE OR REPLACE FUNCTION srs_guard_session_delete() RETURNS trigger AS $$
DECLARE
    policy opportunity_retention_policy%ROWTYPE;
    prefix timestamptz;
BEGIN
    -- Whole-user training/account purge. BOTH markers must match exactly: the
    -- mode alone would let any transaction that ever set it delete any user's
    -- sessions, and the id alone would let an unrelated operation inherit the
    -- escape. current_setting(..., true) returns NULL when unset.
    IF current_setting('{PURGE_MODE}', true) = 'user_training'
       AND current_setting('{PURGE_USER}', true) = OLD.user_id::text THEN
        RETURN OLD;
    END IF;

    SELECT folded_through_started_at INTO prefix
      FROM user_opportunity_retention_state
     WHERE user_id = OLD.user_id;
    -- Unconditional: the raw rows behind this prefix no longer exist, so
    -- deleting the session cannot be made safe by turning the policy off.
    IF prefix IS NOT NULL AND OLD.started_at <= prefix THEN
        RAISE EXCEPTION
            'session % is at or below the permanent SRS fold prefix', OLD.id
            USING ERRCODE = 'raise_exception';
    END IF;

    SELECT * INTO policy FROM opportunity_retention_policy WHERE id = 1;
    -- clock_timestamp(), not now(): a transaction that started before the
    -- deadline and blocked across it must be judged on when it actually got
    -- here, not on when it began.
    -- M alone, never M + G. G is the compactor's drain gap on top of the freeze
    -- boundary; adding it here would keep rows writable right up to the instant
    -- they become foldable and hand the compactor a live writer.
    IF FOUND AND policy.freeze_enabled
       AND OLD.started_at <= clock_timestamp()
             - make_interval(days => policy.mutation_window_days) THEN
        RAISE EXCEPTION
            'session % is past the SRS evidence mutation boundary', OLD.id
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

EVENT_GUARD = f"""
CREATE OR REPLACE FUNCTION srs_guard_opportunity_event_delete() RETURNS trigger AS $$
DECLARE
    policy opportunity_retention_policy%ROWTYPE;
    prefix timestamptz;
    owner bigint;
    started timestamptz;
BEGIN
    -- The fold transfer's own allowance, private to that controlled operation.
    -- A repair CLI cannot reach it: nothing outside the fold SQL sets it, and
    -- it dies with the transaction.
    IF current_setting('{FOLD_MODE}', true) = 'transfer' THEN
        RETURN OLD;
    END IF;

    -- Cascade from deleting the parent blunder. By the time this BEFORE DELETE
    -- fires for a cascaded row the blunder is already gone, which makes its
    -- absence a reliable discriminator between "the target was deleted" and "a
    -- repair is rewriting a frozen session's evidence".
    SELECT user_id INTO owner FROM blunders WHERE id = OLD.blunder_id;
    IF NOT FOUND THEN
        RETURN OLD;
    END IF;

    IF current_setting('{PURGE_MODE}', true) = 'user_training'
       AND current_setting('{PURGE_USER}', true) = owner::text THEN
        RETURN OLD;
    END IF;

    SELECT started_at INTO started FROM game_sessions WHERE id = OLD.session_id;
    -- The session is already gone: this is the game_sessions cascade, which the
    -- session guard above has already vetted.
    IF NOT FOUND THEN
        RETURN OLD;
    END IF;

    SELECT folded_through_started_at INTO prefix
      FROM user_opportunity_retention_state WHERE user_id = owner;
    IF prefix IS NOT NULL AND started <= prefix THEN
        RAISE EXCEPTION
            'opportunity event for session % is at or below the fold prefix',
            OLD.session_id USING ERRCODE = 'raise_exception';
    END IF;

    SELECT * INTO policy FROM opportunity_retention_policy WHERE id = 1;
    IF FOUND AND policy.freeze_enabled
       AND started <= clock_timestamp()
             - make_interval(days => policy.mutation_window_days) THEN
        RAISE EXCEPTION
            'opportunity event for session % is past the mutation boundary',
            OLD.session_id USING ERRCODE = 'raise_exception';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

PURGE_ASSERTION = """
CREATE OR REPLACE FUNCTION srs_assert_purge_complete() RETURNS trigger AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM blunder_opportunity_summaries s
          JOIN blunders b ON b.id = s.blunder_id
         WHERE b.user_id = OLD.user_id
    ) OR EXISTS (
        SELECT 1 FROM blunder_opportunity_events e
          JOIN blunders b ON b.id = e.blunder_id
         WHERE b.user_id = OLD.user_id
    ) THEN
        RAISE EXCEPTION
            'SRS opportunity state remains for purged owner %', OLD.user_id
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(SESSION_GUARD)
    op.execute(EVENT_GUARD)
    op.execute(PURGE_ASSERTION)
    op.execute(
        "CREATE TRIGGER trg_srs_guard_session_delete BEFORE DELETE ON game_sessions "
        "FOR EACH ROW EXECUTE FUNCTION srs_guard_session_delete()"
    )
    op.execute(
        "CREATE TRIGGER trg_srs_guard_opportunity_event_delete "
        "BEFORE DELETE ON blunder_opportunity_events "
        "FOR EACH ROW EXECUTE FUNCTION srs_guard_opportunity_event_delete()"
    )
    # Deferred to COMMIT so the purge may delete this row first (it locks the
    # user and its retention state BEFORE the parent rows, to keep a single lock
    # order) and still be checked against the final state of the transaction.
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_srs_assert_purge_complete "
        "AFTER DELETE ON user_opportunity_retention_state "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION srs_assert_purge_complete()"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "DROP TRIGGER IF EXISTS trg_srs_assert_purge_complete "
        "ON user_opportunity_retention_state"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_srs_guard_opportunity_event_delete "
        "ON blunder_opportunity_events"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_srs_guard_session_delete ON game_sessions")
    op.execute("DROP FUNCTION IF EXISTS srs_assert_purge_complete()")
    op.execute("DROP FUNCTION IF EXISTS srs_guard_opportunity_event_delete()")
    op.execute("DROP FUNCTION IF EXISTS srs_guard_session_delete()")
