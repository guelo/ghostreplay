from sqlalchemy import literal, select

from app.centipawn_loss import centipawn_loss, centipawn_loss_expr


def test_centipawn_loss_clamps_negative_values():
    assert centipawn_loss(None) is None
    assert centipawn_loss(-5) == 0
    assert centipawn_loss(0) == 0
    assert centipawn_loss(10) == 10


def test_centipawn_loss_expr_clamps_negative_values(db_session):
    row = db_session.execute(
        select(
            centipawn_loss_expr(literal(None)),
            centipawn_loss_expr(literal(-5)),
            centipawn_loss_expr(literal(10)),
        )
    ).one()

    assert row == (None, 0, 10)
