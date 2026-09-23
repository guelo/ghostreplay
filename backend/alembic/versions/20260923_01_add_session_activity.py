"""Add nullable coalesced activity hints; no history is fabricated on rollout."""
from alembic import op
import sqlalchemy as sa

revision = "20260923_01"
down_revision = "20260920_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_sessions", sa.Column("last_activity_at", sa.DateTime(timezone=True)))
    op.create_index("idx_game_sessions_user_activity", "game_sessions",
                    ["user_id", "last_activity_at"])


def downgrade():
    op.drop_index("idx_game_sessions_user_activity", table_name="game_sessions")
    op.drop_column("game_sessions", "last_activity_at")
