"""Add provable session-start opening baselines (g-f3m4).

This deliberately revisits the watermark idea reconciled out by ``20260718_01``.
That revision removed three orphan columns from a lost, partial feature:
``game_sessions.opening_baseline_evidence_seq``,
``opening_score_batches.evidence_seq_end``, and
``opening_score_batches.scoped_shared_digest_end``. This attempt is complete in
one revision/application change: the three session watermark columns land with
both start writers, the two acceptance proofs, push-fill, bounded retries, and
tests. No batch-end columns are resurrected; existing sessions remain all-NULL.

The shared epoch becomes monotonic and fail-closed. Event-specific triggers track
the last changed epoch of exact raw/normalized FENs for ``analysis_cache``,
``position_analysis``, and ``analysis_cache_submission``. PostgreSQL TRUNCATE is
represented by per-kind invalidations; SQLite DELETE retains exact tombstones.

Revision ID: 20260809_01
Revises: 20260802_01
Create Date: 2026-08-09

"""

import sqlalchemy as sa
from alembic import op


revision = "20260809_01"
down_revision = "20260802_01"
branch_labels = None
depends_on = None

BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
SHARED_TABLES = (
    "analysis_cache",
    "position_analysis",
    "analysis_cache_submission",
)
EVENTS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def _postgres_scope_select(table: str, event: str) -> str:
    if table == "analysis_cache":
        aliases = {
            "INSERT": ("new_rows",),
            "UPDATE": ("old_rows", "new_rows"),
            "DELETE": ("old_rows",),
        }[event]
        parts: list[str] = []
        for alias in aliases:
            parts.extend(
                [
                    f"SELECT 'raw'::varchar(4) AS kind, fen_before AS fen FROM {alias}",
                    f"SELECT 'norm'::varchar(4) AS kind, normalized_fen_before AS fen "
                    f"FROM {alias} WHERE normalized_fen_before IS NOT NULL",
                ]
            )
        return "\nUNION\n".join(parts)
    if table == "position_analysis":
        aliases = {
            "INSERT": ("new_rows",),
            "UPDATE": ("old_rows", "new_rows"),
            "DELETE": ("old_rows",),
        }[event]
        return "\nUNION\n".join(
            f"SELECT 'norm'::varchar(4) AS kind, normalized_fen AS fen FROM {alias}"
            for alias in aliases
        )
    id_sources = {
        "INSERT": "SELECT analysis_cache_id FROM new_rows",
        "UPDATE": (
            "SELECT analysis_cache_id FROM old_rows "
            "UNION SELECT analysis_cache_id FROM new_rows"
        ),
        "DELETE": "SELECT analysis_cache_id FROM old_rows",
    }
    return f"""
        WITH affected_ids AS ({id_sources[event]})
        SELECT 'raw'::varchar(4) AS kind, cache.fen_before AS fen
        FROM analysis_cache AS cache
        JOIN affected_ids ON affected_ids.analysis_cache_id = cache.id
        UNION
        SELECT 'norm'::varchar(4) AS kind, cache.normalized_fen_before AS fen
        FROM analysis_cache AS cache
        JOIN affected_ids ON affected_ids.analysis_cache_id = cache.id
        WHERE cache.normalized_fen_before IS NOT NULL
    """


def _install_postgresql() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION guard_evidence_epoch_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'evidence_epoch singleton cannot be deleted';
            END IF;
            IF NEW.value <= OLD.value THEN
                RAISE EXCEPTION 'evidence_epoch value must increase';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_evidence_epoch_truncate()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'evidence_epoch singleton cannot be truncated';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evidence_epoch_monotonic ON evidence_epoch"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evidence_epoch_no_truncate ON evidence_epoch"
    )
    op.execute("""
        CREATE TRIGGER trg_evidence_epoch_monotonic
        BEFORE UPDATE OR DELETE ON evidence_epoch
        FOR EACH ROW EXECUTE FUNCTION guard_evidence_epoch_mutation()
    """)
    op.execute("""
        CREATE TRIGGER trg_evidence_epoch_no_truncate
        BEFORE TRUNCATE ON evidence_epoch
        FOR EACH STATEMENT EXECUTE FUNCTION reject_evidence_epoch_truncate()
    """)

    for table in SHARED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch ON {table}")
        for event in EVENTS:
            event_lower = event.lower()
            trigger = f"trg_{table}_evidence_epoch_{event_lower}"
            function = f"track_{table}_evidence_{event_lower}"
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            if event == "TRUNCATE":
                kinds = "('norm')" if table == "position_analysis" else "('raw','norm')"
                body = f"""
                    UPDATE shared_evidence_scope_invalidations
                    SET last_changed_epoch = new_epoch
                    WHERE kind IN {kinds};
                """
            else:
                scope_select = _postgres_scope_select(table, event)
                body = f"""
                    INSERT INTO shared_evidence_scope_versions
                        (kind, fen, last_changed_epoch)
                    SELECT affected.kind, affected.fen, new_epoch
                    FROM ({scope_select}) AS affected
                    WHERE affected.fen IS NOT NULL
                    GROUP BY affected.kind, affected.fen
                    ORDER BY affected.kind, affected.fen
                    ON CONFLICT (kind, fen) DO UPDATE
                    SET last_changed_epoch = EXCLUDED.last_changed_epoch;
                """
            op.execute(f"""
                CREATE OR REPLACE FUNCTION {function}() RETURNS trigger AS $$
                DECLARE
                    new_epoch bigint;
                BEGIN
                    IF (
                        SELECT count(*)
                        FROM shared_evidence_scope_invalidations
                        WHERE kind IN ('raw', 'norm')
                    ) <> 2 THEN
                        RAISE EXCEPTION 'shared evidence invalidation rows missing';
                    END IF;
                    UPDATE evidence_epoch
                    SET value = value + 1
                    WHERE id = 1
                    RETURNING value INTO new_epoch;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'evidence_epoch singleton missing';
                    END IF;
                    {body}
                    RETURN NULL;
                END;
                $$ LANGUAGE plpgsql
            """)
            if event == "INSERT":
                referencing = "REFERENCING NEW TABLE AS new_rows"
            elif event == "UPDATE":
                referencing = (
                    "REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows"
                )
            elif event == "DELETE":
                referencing = "REFERENCING OLD TABLE AS old_rows"
            else:
                referencing = ""
            op.execute(f"""
                CREATE TRIGGER {trigger}
                AFTER {event} ON {table}
                {referencing}
                FOR EACH STATEMENT EXECUTE FUNCTION {function}()
            """)


def _sqlite_version_statements(table: str, event: str) -> list[str]:
    aliases = {
        "INSERT": ("NEW",),
        "UPDATE": ("OLD", "NEW"),
        "DELETE": ("OLD",),
    }[event]
    statements: list[str] = []
    if table == "analysis_cache":
        for alias in aliases:
            statements.append(f"""
                INSERT INTO shared_evidence_scope_versions
                    (kind, fen, last_changed_epoch)
                VALUES ('raw', {alias}.fen_before,
                    (SELECT value FROM evidence_epoch WHERE id = 1))
                ON CONFLICT(kind, fen) DO UPDATE SET
                    last_changed_epoch = excluded.last_changed_epoch;
            """)
            statements.append(f"""
                INSERT INTO shared_evidence_scope_versions
                    (kind, fen, last_changed_epoch)
                SELECT 'norm', {alias}.normalized_fen_before,
                    (SELECT value FROM evidence_epoch WHERE id = 1)
                WHERE {alias}.normalized_fen_before IS NOT NULL
                ON CONFLICT(kind, fen) DO UPDATE SET
                    last_changed_epoch = excluded.last_changed_epoch;
            """)
    elif table == "position_analysis":
        for alias in aliases:
            statements.append(f"""
                INSERT INTO shared_evidence_scope_versions
                    (kind, fen, last_changed_epoch)
                VALUES ('norm', {alias}.normalized_fen,
                    (SELECT value FROM evidence_epoch WHERE id = 1))
                ON CONFLICT(kind, fen) DO UPDATE SET
                    last_changed_epoch = excluded.last_changed_epoch;
            """)
    else:
        for alias in aliases:
            statements.append(f"""
                INSERT INTO shared_evidence_scope_versions
                    (kind, fen, last_changed_epoch)
                SELECT 'raw', fen_before,
                    (SELECT value FROM evidence_epoch WHERE id = 1)
                FROM analysis_cache
                WHERE id = {alias}.analysis_cache_id
                ON CONFLICT(kind, fen) DO UPDATE SET
                    last_changed_epoch = excluded.last_changed_epoch;
            """)
            statements.append(f"""
                INSERT INTO shared_evidence_scope_versions
                    (kind, fen, last_changed_epoch)
                SELECT 'norm', normalized_fen_before,
                    (SELECT value FROM evidence_epoch WHERE id = 1)
                FROM analysis_cache
                WHERE id = {alias}.analysis_cache_id
                  AND normalized_fen_before IS NOT NULL
                ON CONFLICT(kind, fen) DO UPDATE SET
                    last_changed_epoch = excluded.last_changed_epoch;
            """)
    return statements


def _install_sqlite() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_evidence_epoch_monotonic")
    op.execute("DROP TRIGGER IF EXISTS trg_evidence_epoch_no_delete")
    op.execute("""
        CREATE TRIGGER trg_evidence_epoch_monotonic
        BEFORE UPDATE ON evidence_epoch
        WHEN NEW.value <= OLD.value
        BEGIN
            SELECT RAISE(ABORT, 'evidence_epoch value must increase');
        END
    """)
    op.execute("""
        CREATE TRIGGER trg_evidence_epoch_no_delete
        BEFORE DELETE ON evidence_epoch
        BEGIN
            SELECT RAISE(ABORT, 'evidence_epoch singleton cannot be deleted');
        END
    """)
    for table in SHARED_TABLES:
        for event in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"trg_{table}_evidence_epoch_{event.lower()}"
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            version_sql = "\n".join(_sqlite_version_statements(table, event))
            op.execute(f"""
                CREATE TRIGGER {trigger}
                AFTER {event} ON {table}
                BEGIN
                    SELECT CASE WHEN NOT EXISTS (
                        SELECT 1 FROM evidence_epoch WHERE id = 1
                    ) THEN RAISE(ABORT, 'evidence_epoch singleton missing') END;
                    SELECT CASE WHEN (
                        SELECT count(*)
                        FROM shared_evidence_scope_invalidations
                        WHERE kind IN ('raw', 'norm')
                    ) <> 2 THEN RAISE(
                        ABORT, 'shared evidence invalidation rows missing'
                    ) END;
                    UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                    {version_sql}
                END
            """)


def _restore_legacy_postgresql() -> None:
    for table in SHARED_TABLES:
        for event in EVENTS:
            event_lower = event.lower()
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch_{event_lower} "
                f"ON {table}"
            )
            op.execute(
                f"DROP FUNCTION IF EXISTS track_{table}_evidence_{event_lower}()"
            )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evidence_epoch_monotonic ON evidence_epoch"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_evidence_epoch_no_truncate ON evidence_epoch"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_evidence_epoch_mutation()")
    op.execute("DROP FUNCTION IF EXISTS reject_evidence_epoch_truncate()")
    op.execute("""
        CREATE OR REPLACE FUNCTION bump_evidence_epoch() RETURNS trigger AS $$
        BEGIN
            UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in SHARED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch ON {table}")
        op.execute(f"""
            CREATE TRIGGER trg_{table}_evidence_epoch
            AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION bump_evidence_epoch()
        """)


def _restore_legacy_sqlite() -> None:
    for table in SHARED_TABLES:
        for event in ("insert", "update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_evidence_epoch_{event}")
    op.execute("DROP TRIGGER IF EXISTS trg_evidence_epoch_monotonic")
    op.execute("DROP TRIGGER IF EXISTS trg_evidence_epoch_no_delete")
    for table in SHARED_TABLES:
        for event in ("INSERT", "UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER trg_{table}_evidence_epoch_{event.lower()}
                AFTER {event} ON {table}
                BEGIN
                    UPDATE evidence_epoch SET value = value + 1 WHERE id = 1;
                END
            """)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.add_column(sa.Column("baseline_watermark_seq", BIGINT, nullable=True))
        batch_op.add_column(sa.Column("baseline_watermark_epoch", BIGINT, nullable=True))
        batch_op.add_column(
            sa.Column("baseline_watermark_fingerprint", sa.Text(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_game_sessions_baseline_watermark_complete",
            "(baseline_watermark_seq is null and baseline_watermark_epoch is null "
            "and baseline_watermark_fingerprint is null) or "
            "(baseline_watermark_seq is not null and baseline_watermark_epoch is not null "
            "and baseline_watermark_fingerprint is not null)",
        )

    op.create_table(
        "shared_evidence_scope_versions",
        sa.Column("kind", sa.String(4), primary_key=True),
        sa.Column("fen", sa.Text(), primary_key=True),
        sa.Column("last_changed_epoch", BIGINT, nullable=False),
        sa.CheckConstraint(
            "kind in ('raw','norm')",
            name="ck_shared_evidence_scope_versions_kind",
        ),
    )
    op.create_table(
        "shared_evidence_scope_invalidations",
        sa.Column("kind", sa.String(4), primary_key=True),
        sa.Column("last_changed_epoch", BIGINT, nullable=False),
        sa.CheckConstraint(
            "kind in ('raw','norm')",
            name="ck_shared_evidence_scope_invalidations_kind",
        ),
    )

    epoch = bind.execute(
        sa.text("SELECT value FROM evidence_epoch WHERE id = 1")
    ).scalar_one_or_none()
    if epoch is None:
        raise RuntimeError("evidence_epoch singleton missing during established upgrade")
    bind.execute(
        sa.text(
            "INSERT INTO shared_evidence_scope_invalidations "
            "(kind, last_changed_epoch) VALUES ('raw', :epoch), ('norm', :epoch)"
        ),
        {"epoch": int(epoch)},
    )

    if dialect == "postgresql":
        _install_postgresql()
    else:
        _install_sqlite()


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        _restore_legacy_postgresql()
    else:
        _restore_legacy_sqlite()

    op.drop_table("shared_evidence_scope_versions")
    op.drop_table("shared_evidence_scope_invalidations")

    with op.batch_alter_table("game_sessions") as batch_op:
        batch_op.drop_constraint(
            "ck_game_sessions_baseline_watermark_complete",
            type_="check",
        )
        batch_op.drop_column("baseline_watermark_fingerprint")
        batch_op.drop_column("baseline_watermark_epoch")
        batch_op.drop_column("baseline_watermark_seq")
