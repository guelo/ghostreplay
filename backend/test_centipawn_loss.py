import json
from pathlib import Path

import pytest
from sqlalchemy import literal, select

from app.centipawn_loss import (
    CENTIPAWN_LOSS_CAP_CP,
    centipawn_loss,
    centipawn_loss_expr,
    clamp_delta_nonneg,
)

_CPL_CAP_VECTORS = json.loads(
    (Path(__file__).parent / "tests" / "fixtures" / "cpl_cap_vectors.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CPL_CAP_VECTORS, ids=lambda c: str(c["input"]))
def test_centipawn_loss_matches_shared_vectors(case):
    # Same fixture drives the TS evalLoss suite, so the Python cap and the TS cap are
    # pinned to one vector set (null -> None on the Python side).
    assert centipawn_loss(case["input"]) == case["expected"]


def test_centipawn_loss_none_passes_through():
    assert centipawn_loss(None) is None


def test_centipawn_loss_cap_constant_is_1000():
    assert CENTIPAWN_LOSS_CAP_CP == 1000


def test_centipawn_loss_expr_floors_and_caps(db_session):
    row = db_session.execute(
        select(
            centipawn_loss_expr(literal(None)),
            centipawn_loss_expr(literal(-5)),
            centipawn_loss_expr(literal(10)),
            centipawn_loss_expr(literal(999)),
            centipawn_loss_expr(literal(1000)),
            centipawn_loss_expr(literal(1001)),
            centipawn_loss_expr(literal(10000)),
        )
    ).one()

    assert row == (None, 0, 10, 999, 1000, 1000, 1000)


def test_clamp_delta_nonneg_floors_but_never_caps():
    # The RAW-evidence clamp for analysis_cache: floor at 0, NO upper cap.
    assert clamp_delta_nonneg(None) is None
    assert clamp_delta_nonneg(-5) == 0
    assert clamp_delta_nonneg(10) == 10
    assert clamp_delta_nonneg(1000) == 1000
    assert clamp_delta_nonneg(1001) == 1001
    assert clamp_delta_nonneg(10000) == 10000
