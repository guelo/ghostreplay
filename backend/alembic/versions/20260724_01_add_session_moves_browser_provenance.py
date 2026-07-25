"""Add session_moves.browser_provenance (g-mk1d).

One nullable JSON ``Text`` column carrying the browser-game-v2 DYNAMIC provenance
(the seven self-reported engine/search values) for a move's OWN local search.

It exists to serve exactly one read: at ``GET /{session_id}/analysis`` a stored
``browser-game-v2`` cache row is REQUIRES_COMPARISON — it may only re-label a
played move when it is provably STRONGER than what this session itself computed.
That live operand is unavailable at GET time from anywhere else (request-side
provenance exists only at upload time; a freshly reloaded saved game holds no
client-side analysis — precisely when a stronger cross-user row matters most), so
it is persisted per move here.

Only the DYNAMIC subset is stored. The FIXED identity half and the manifest
digest are reconstructed from the server registry at read time, so a hand-edited
session-move row can never claim a fixed identity it did not earn.

Nullable with no server_default ⇒ instant on both SQLite and Postgres (no table
rewrite, no backfill). Every existing row reads NULL, which resolves to "no
comparable live evidence" and simply withholds the overlay — the safe direction.

Revision ID: 20260724_01
Revises: 20260720_01
Create Date: 2026-07-24

"""
import sqlalchemy as sa
from alembic import op


revision = "20260724_01"
down_revision = "20260720_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "session_moves",
        sa.Column("browser_provenance", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("session_moves", "browser_provenance")
