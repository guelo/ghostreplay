"""Regression coverage for model-driven database schema creation."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import (
    Boolean,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)

from app.models import Base, OpeningPositionEdge, OpeningPositionScore


_OPENING_IDENTITY_CONTRACTS = (
    (
        OpeningPositionScore,
        "opening_position_scores",
        "idx_opening_position_scores_batch_fen",
        "uq_opening_position_scores_batch_fen",
        ("batch_id", "normalized_fen"),
    ),
    (
        OpeningPositionEdge,
        "opening_position_edges",
        "idx_opening_position_edges_batch_parent",
        "uq_opening_position_edges_batch_parent_child",
        ("batch_id", "parent_fen", "child_fen"),
    ),
)


@pytest.mark.parametrize(
    ("model", "_table_name", "redundant_index", "unique_name", "unique_columns"),
    _OPENING_IDENTITY_CONTRACTS,
)
def test_opening_identity_metadata_uses_only_unique_constraint_index(
    model, _table_name, redundant_index, unique_name, unique_columns
):
    """Model metadata must not recreate indexes dropped by migration 20260718_01."""
    table = model.__table__

    assert redundant_index not in {index.name for index in table.indexes}
    unique_constraints = {
        constraint.name: tuple(constraint.columns.keys())
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert unique_constraints[unique_name] == unique_columns


def test_fresh_create_all_omits_redundant_opening_indexes():
    """A fresh model-driven schema must match the migrated opening index set."""
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        for (
            _model,
            table_name,
            redundant_index,
            unique_name,
            unique_columns,
        ) in _OPENING_IDENTITY_CONTRACTS:
            assert redundant_index not in {
                index["name"] for index in inspector.get_indexes(table_name)
            }
            unique_constraints = {
                constraint["name"]: tuple(constraint["column_names"])
                for constraint in inspector.get_unique_constraints(table_name)
            }
            assert unique_constraints[unique_name] == unique_columns
    finally:
        engine.dispose()


# A bare row per table that owns a boolean column with a server default: only the
# columns the table cannot supply itself, so every boolean is written by the DDL
# default rather than by the INSERT.
_BARE_ROWS = {
    "users": {"id": 1},
    "game_sessions": {
        "id": uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        "user_id": 1,
        "status": "active",
        "engine_elo": 1200,
    },
    "opportunity_retention_policy": {"id": 1},
    "opponent_decisions": {
        "decision_id": uuid.UUID("00000000-0000-0000-0000-0000000000b1"),
        "session_id": uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        "request_fingerprint": "fingerprint",
        "request_fen_hash": "fen-hash",
        "uci_history": "",
        "ply_before": 0,
        "response_payload": "{}",
    },
}

_DECLARED_BOOLEAN = {"true": True, "false": False}


def _boolean_defaults(table):
    """Declared boolean defaults for ``table``, as ``{column: python value}``."""
    defaults = {}
    for column in table.columns:
        if not isinstance(column.type, Boolean) or column.server_default is None:
            continue
        declared = str(getattr(column.server_default.arg, "text", column.server_default.arg))
        assert declared in _DECLARED_BOOLEAN, (
            f"{table.name}.{column.name} declares an unrecognized boolean default "
            f"{declared!r}; spell it text(\"true\") or text(\"false\")"
        )
        defaults[column.name] = _DECLARED_BOOLEAN[declared]
    return defaults


def test_bare_row_map_covers_every_boolean_default_table():
    """A new table with a defaulted boolean must also gain a bare-row case below."""
    owning_tables = {
        table.name for table in Base.metadata.tables.values() if _boolean_defaults(table)
    }

    assert owning_tables == set(_BARE_ROWS)


@pytest.mark.parametrize("table_name", sorted(_BARE_ROWS))
def test_create_all_boolean_defaults_store_booleans(table_name):
    """A string server default renders as a quoted literal, which SQLite stores as
    TEXT: the row then reads back as ``True`` whatever the default said, and any
    CHECK comparing the column to a real boolean rejects the INSERT outright."""
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        table = Base.metadata.tables[table_name]
        expected = _boolean_defaults(table)

        with engine.begin() as connection:
            connection.execute(table.insert().values(**_BARE_ROWS[table_name]))
            stored = connection.execute(
                select(*[table.c[name] for name in expected])
            ).one()
            storage_types = connection.execute(
                text(
                    "SELECT "
                    + ", ".join(f"typeof({name})" for name in expected)
                    + f" FROM {table_name}"
                )
            ).one()

        assert dict(zip(expected, stored)) == expected
        assert all(isinstance(value, bool) for value in stored)
        assert set(storage_types) == {"integer"}
    finally:
        engine.dispose()
