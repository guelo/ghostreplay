"""Regression coverage for model-driven database schema creation."""

from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint, create_engine, inspect

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
