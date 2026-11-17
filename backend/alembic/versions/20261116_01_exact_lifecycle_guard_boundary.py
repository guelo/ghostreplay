"""Make the SRS delete guards' freeze boundary exact elapsed time (PostgreSQL only).

``20260919_05`` computed the boundary as ``clock_timestamp() -
make_interval(days => M)``. Interval DAYS are subtracted as calendar days in the
session TimeZone, so in a zone that observes daylight saving a window spanning a
clock change is an hour longer or shorter than M days. The application's age
tests (``app.opportunity_retention``) use exact elapsed seconds; on a fall-back
change the trigger's freeze instant would land on the application's fold cutoff
``now - (M + G)`` and remove G for out-of-band deletes. ``make_interval(secs =>
M * 86400)`` has no days part and is the same instant in every session zone.

Only the boundary expression changes. Both function bodies are otherwise
byte-for-byte those of ``20260919_05`` (test_opportunity_lifecycle_migration.py
holds the downgrade rendering to that), and the triggers that call them are
untouched: ``CREATE OR REPLACE FUNCTION`` swaps the body under the existing
trigger. SQLite has no triggers, as before.

Revision ID: 20261116_01
Revises: 20260923_01
"""

from alembic import op

revision = "20261116_01"
down_revision = "20260923_01"
branch_labels = None
depends_on = None

PURGE_MODE = "ghostreplay.srs_purge_mode"
PURGE_USER = "ghostreplay.srs_purge_user_id"
FOLD_MODE = "ghostreplay.srs_fold_mode"

EXACT_BOUNDARY = "make_interval(secs => policy.mutation_window_days * 86400)"
CALENDAR_BOUNDARY = "make_interval(days => policy.mutation_window_days)"

_SESSION_GUARD = """
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
             - {BOUNDARY} THEN
        RAISE EXCEPTION
            'session % is past the SRS evidence mutation boundary', OLD.id
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""

_EVENT_GUARD = """
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
             - {BOUNDARY} THEN
        RAISE EXCEPTION
            'opportunity event for session % is past the mutation boundary',
            OLD.session_id USING ERRCODE = 'raise_exception';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;
"""


def render(template: str, boundary: str) -> str:
    return template.format(
        PURGE_MODE=PURGE_MODE, PURGE_USER=PURGE_USER, FOLD_MODE=FOLD_MODE,
        BOUNDARY=boundary,
    )


def _install(boundary: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(render(_SESSION_GUARD, boundary))
    op.execute(render(_EVENT_GUARD, boundary))


def upgrade():
    _install(EXACT_BOUNDARY)


def downgrade():
    _install(CALENDAR_BOUNDARY)
