"""Add canonical-precompute columns to analysis_cache.

Adds nullable columns required by ``g-canonical-precomp``:
  * ``best_eval_mate`` — white-relative mate count for the best move;
  * ``eval_file_id`` / ``eval_file_small_id`` — full identities of the two
    embedded SF18 NNUE networks (Text, since two SHA-256 hashes do not fit the
    single VARCHAR(128) ``network_id`` column);
  * ``analyzer_protocol_version`` — version of the analyzer output contract;
  * ``profile_manifest_digest`` — digest of the producing profile's immutable
    analysis-identity bits.

Existing rows stay NULL. No data backfill.

Revision ID: 20260611_02
Revises: 20260611_01
Create Date: 2026-06-11

"""
import sqlalchemy as sa
from alembic import op


revision = "20260611_02"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("best_eval_mate", sa.Integer()),
    ("eval_file_id", sa.Text()),
    ("eval_file_small_id", sa.Text()),
    ("analyzer_protocol_version", sa.String(length=64)),
    ("profile_manifest_digest", sa.String(length=64)),
)


def upgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        for name, col_type in _COLUMNS:
            batch_op.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        for name, _ in reversed(_COLUMNS):
            batch_op.drop_column(name)
