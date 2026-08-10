"""Recover failed drill outcomes overwritten by the abandon endpoint.

Before g-drill-failed-overwrite, leaving a stopped drill called ``/abandon`` and
unconditionally changed ``drill_state`` from ``failed`` to ``abandoned``.  The
endpoint still preserved ``drill_terminal_reason``, so the historical defect has
an exact, self-limiting predicate.  ``result = 'drill_abandon'`` distinguishes
these rows from natural drill endings, while the two failure reasons exclude
genuine mid-drill abandons (whose terminal reason is NULL).

Deployment boundary
-------------------
This revision intentionally follows the application-only fix in a later deploy.
Railway was verified on 2026-08-10 to be running commit 4353088, which contains
fix commit 967a8f4, with the former replica drained.  Running this backfill in
the fix's own deploy would have allowed that former replica to corrupt rows
after Alembic had already visited them.

Sizing and transaction shape
----------------------------
The 2026-07-24 production restore contains 1,739 drill sessions.  Exactly 1,509
match the upgrade predicate: 1,458 accuracy failures and 51 off-route failures.
A single bounded UPDATE is therefore preferable to a separate batch runner.
Every target is already terminal (``status = 'ended'``), so serving code cannot
write it again; the ordinary Alembic transaction holds no contended live-session
locks, and 1,509 row versions impose negligible WAL/bloat cost at this scale.

The downgrade uses the identical predicate with only ``drill_state`` swapped.
It also relabels matching rows written after the application fix, which is the
correct restoration of the former semantics.  The operation is digest-neutral:
opening eligibility does inspect drill_state for an active accuracy failure, but
every target already qualifies through ``status = 'ended'``.  The evidence input
digests do not project drill_state, and this revision changes only that column,
so no evidence-sequence bump or opening-score recomputation is required.

Revision ID: 20260810_01
Revises: 20260809_01
Create Date: 2026-08-10

"""

from alembic import op


revision = "20260810_01"
down_revision = "20260809_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE game_sessions
        SET drill_state = 'failed'
        WHERE session_mode = 'drill'
          AND drill_state = 'abandoned'
          AND status = 'ended'
          AND result = 'drill_abandon'
          AND drill_terminal_reason IN ('accuracy', 'off_route')
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE game_sessions
        SET drill_state = 'abandoned'
        WHERE session_mode = 'drill'
          AND drill_state = 'failed'
          AND status = 'ended'
          AND result = 'drill_abandon'
          AND drill_terminal_reason IN ('accuracy', 'off_route')
    """)
