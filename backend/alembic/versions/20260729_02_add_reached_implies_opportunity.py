"""Add ck_blunder_opportunity_reached_implies_opportunity (g-boundary-event-scope).

Reaching a blunder position IS the strongest possible opportunity at it, which is why
``_compute_blunder_opportunity_events`` has always written ``opportunity = reached OR
forward_reachable``. A row claiming ``reached`` without ``opportunity`` is therefore not
a stricter observation, it is an incoherent one — and it is exactly the shape that would
land in a reach NUMERATOR whose denominator excludes it, quietly lifting a p_reach the
exclusion floor then reads as evidence.

The aggregates in ``app.srs_opportunity`` now restate the implication in SQL as defence
in depth. This constraint is the other half: it stops such a row from being WRITTEN, so
the two can never disagree about a row that exists.

The UPDATE below repairs any pre-existing violator before the constraint is created.
Against every writer in the tree it must match zero rows; it exists because ADD
CONSTRAINT on a table with one violator fails the whole migration, and the repair —
promote the row to the opportunity its own ``reached`` already asserts — is the value
the writer would have produced. It is idempotent and safe to re-run.

PostgreSQL path (production). The constraint is created VALIDATED in one statement
rather than ``NOT VALID`` + a separate ``VALIDATE CONSTRAINT``. Alembic runs this
revision inside a transaction, so ``ADD CONSTRAINT`` holds ACCESS EXCLUSIVE until COMMIT
either way: splitting it would run the same scan under the same lock and leave a
permanently unproven constraint in the catalog if the second statement were ever
skipped. The scan itself is a single sequential pass over two NOT NULL booleans on a
table in the low hundreds of thousands of rows — sub-second, and the same order of work
as the UPDATE that precedes it. A transaction-local ``lock_timeout`` bounds how long the
DDL waits behind live session-upload writers, and a ``statement_timeout`` bounds the
scan itself; either abort (SQLSTATE 55P03 / 57014) fails the migration instead of
stalling traffic.

SQLite path (tests only). Skipped: SQLite cannot ALTER-ADD a CHECK, and
``batch_alter_table`` would rewrite the table from a reflection that drops constraints
it cannot see. The application's SQLite test schema is ``backend/conftest.py``'s
hand-written DDL, which declares this CHECK inline, so constraint behaviour stays
covered there.

Revision ID: 20260729_02
Revises: 20260729_01
Create Date: 2026-07-29

"""
from alembic import op


revision = "20260729_02"
down_revision = "20260729_01"
branch_labels = None
depends_on = None

CONSTRAINT_NAME = "ck_blunder_opportunity_reached_implies_opportunity"
CONDITION = "reached = false OR opportunity = true"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    # Transaction-local: bounds every wait and every scan below, and resets at COMMIT.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute(
        "UPDATE blunder_opportunity_events SET opportunity = true "
        "WHERE reached = true AND opportunity = false"
    )
    op.create_check_constraint(
        CONSTRAINT_NAME, "blunder_opportunity_events", CONDITION
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint(CONSTRAINT_NAME, "blunder_opportunity_events", type_="check")
