"""Add provenance/quality metadata to analysis_cache.

Adds nullable profile/engine/search/contract metadata columns used by the
quality-aware cache replacement policy. Existing rows stay NULL = legacy /
untrusted; the repair child reclaims them using this metadata state.

Revision ID: 20260611_01
Revises: 20260604_02
Create Date: 2026-06-11

"""
import sqlalchemy as sa
from alembic import op


revision = "20260611_01"
down_revision = "20260604_02"
branch_labels = None
depends_on = None


_COLUMNS = (
    ("analysis_profile_id", sa.String(length=64)),
    ("engine_name", sa.String(length=64)),
    ("engine_version", sa.String(length=64)),
    ("engine_build", sa.String(length=128)),
    ("network_id", sa.String(length=128)),
    ("search_limit_type", sa.String(length=16)),
    ("search_limit_value", sa.Integer()),
    ("threads", sa.Integer()),
    ("hash_mb", sa.Integer()),
    ("multipv", sa.Integer()),
    ("evidence_contract_id", sa.String(length=64)),
)


def upgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        for name, col_type in _COLUMNS:
            batch_op.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("analysis_cache") as batch_op:
        for name, _ in reversed(_COLUMNS):
            batch_op.drop_column(name)
