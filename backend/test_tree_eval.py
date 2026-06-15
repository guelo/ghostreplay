"""Tests for the opening-tree analysis_cache eval lookup helper (app/tree_eval.py)."""

import pytest
from sqlalchemy import create_engine, tuple_
from sqlalchemy.orm import sessionmaker

from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import BROWSER_PROFILE_ID
from app.evidence_contracts import MINIMAL_PLAYED_EVAL
from app.fen import normalize_fen
from app.models import AnalysisCache, Base
from app.tree_eval import (
    MoveEval,
    eval_for_perspective,
    lookup_move_evals,
    lookup_root_eval,
)

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
# Same position reached via two move orders => identical normalized FEN, different clocks.
POS_A = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
POS_B = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 4 5"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tree_eval.db'}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    s = Factory()
    yield s
    s.close()
    engine.dispose()


def _seed(session, *, fen, uci, source="game", **fields):
    """Insert a cache row, computing normalized_fen_before like the real writer."""
    row = AnalysisCache(
        fen_before=fen,
        normalized_fen_before=normalize_fen(fen),
        move_uci=uci,
        move_san=fields.pop("move_san", uci),
        source=source,
        **fields,
    )
    session.add(row)
    session.commit()
    return row


# --- exact full-FEN hits -------------------------------------------------------

def test_exact_full_fen_hit_returns_cp(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=42, mate=None)


def test_exact_hit_prefers_mate_over_cp(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42, played_eval_mate=3)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=3)


# --- normalized transposition fallback ----------------------------------------

def test_normalized_fallback_on_clock_variant(session):
    # Cache row stored under POS_B; request the clock-variant POS_A (no exact row).
    _seed(session, fen=POS_B, uci="f1c4", played_eval=15)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=15, mate=None)


def test_fallback_prefers_mate_same_position(session):
    # Two rows, both normalize to POS_A's position, same move: cp vs mate.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10)
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=20,
        played_eval_mate=2,
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=None, mate=2)


def test_fallback_source_ordering_precomputed_over_game(session):
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10, source="game")
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=20,
        source="precomputed",
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=20, mate=None)


def test_fallback_stable_id_tiebreak(session):
    first = _seed(session, fen=POS_B, uci="f1c4", played_eval=11, source="precomputed")
    _seed(
        session,
        fen="r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 6 7",
        uci="f1c4",
        played_eval=22,
        source="precomputed",
    )
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    # Same source, no mate -> lowest id wins.
    assert out[(POS_A, "f1c4")] == MoveEval(cp=11, mate=None)
    assert first.id == min(r.id for r in session.query(AnalysisCache).all())


def test_fallback_does_not_cross_moves(session):
    # Same position, different move with a richer eval must not leak into f1c4.
    _seed(session, fen=POS_B, uci="f1c4", played_eval=10)
    _seed(session, fen=POS_B, uci="f1b5", played_eval=99)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=10, mate=None)


# --- root eval -----------------------------------------------------------------

def test_root_eval_from_non_best_move_row(session):
    # A played-move row whose move is NOT the engine best move still carries best_eval.
    _seed(
        session,
        fen=START,
        uci="e2e4",
        played_eval=18,
        best_move_uci="d2d4",
        best_eval=30,
    )
    assert lookup_root_eval(session, START) == MoveEval(cp=30, mate=None)


def test_root_eval_prefers_complete_best_move_row(session):
    _seed(session, fen=START, uci="e2e4", best_move_uci="d2d4", best_eval=25)
    _seed(session, fen=START, uci="d2d4", best_move_uci="d2d4", best_eval=31)
    # Both have usable best_eval; the row where move==best_move is preferred.
    assert lookup_root_eval(session, START) == MoveEval(cp=31, mate=None)


def test_root_eval_mate(session):
    _seed(session, fen=START, uci="e2e4", best_move_uci="e2e4", best_eval=20, best_eval_mate=5)
    assert lookup_root_eval(session, START) == MoveEval(cp=None, mate=5)


def test_root_eval_missing_returns_none(session):
    _seed(session, fen=START, uci="e2e4", played_eval=18)  # no best_eval
    assert lookup_root_eval(session, START) is None


# --- misses & no-usable-eval ---------------------------------------------------

def test_cache_miss_returns_none(session):
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] is None


def test_exact_without_usable_eval_falls_back(session):
    # Exact row exists but carries no played eval; a transposition does.
    _seed(session, fen=POS_A, uci="f1c4", best_eval=5)  # best_eval only, no played eval
    _seed(session, fen=POS_B, uci="f1c4", played_eval=12)
    out = lookup_move_evals(session, [(POS_A, "f1c4")])
    assert out[(POS_A, "f1c4")] == MoveEval(cp=12, mate=None)


def test_batch_mixes_exact_fallback_and_miss(session):
    _seed(session, fen=POS_A, uci="f1c4", played_eval=42)   # exact
    _seed(session, fen=POS_B, uci="f1b5", played_eval=7)    # fallback for POS_A/f1b5
    out = lookup_move_evals(session, [(POS_A, "f1c4"), (POS_A, "f1b5"), (POS_A, "g1f3")])
    assert out == {
        (POS_A, "f1c4"): MoveEval(cp=42, mate=None),
        (POS_A, "f1b5"): MoveEval(cp=7, mate=None),
        (POS_A, "g1f3"): None,
    }


def test_empty_requests(session):
    assert lookup_move_evals(session, []) == {}


# --- perspective conversion ----------------------------------------------------

def test_perspective_white_unchanged():
    ev = MoveEval(cp=30, mate=None)
    assert eval_for_perspective(ev, "white") is ev


def test_perspective_black_negates_cp():
    assert eval_for_perspective(MoveEval(cp=30, mate=None), "black") == MoveEval(cp=-30, mate=None)


def test_perspective_black_negates_mate():
    assert eval_for_perspective(MoveEval(cp=None, mate=4), "black") == MoveEval(cp=None, mate=-4)


def test_perspective_zero_and_none():
    assert eval_for_perspective(MoveEval(cp=0, mate=None), "black") == MoveEval(cp=0, mate=None)
    assert eval_for_perspective(None, "black") is None


def test_perspective_invalid_color():
    with pytest.raises(ValueError):
        eval_for_perspective(MoveEval(cp=1, mate=None), "sideways")


# --- writer populates the derived column ---------------------------------------

def test_writer_populates_normalized_fen_before(session):
    write_analysis_cache_rows(
        session,
        [
            {
                "fen_before": POS_A,
                "move_uci": "f1c4",
                "move_san": "Bc4",
                "played_eval": 25,
                "source": "game",
                "analysis_profile_id": BROWSER_PROFILE_ID,
                "evidence_contract_id": MINIMAL_PLAYED_EVAL,
            }
        ],
    )
    row = session.query(AnalysisCache).one()
    assert row.normalized_fen_before == normalize_fen(POS_A)
    # And the lookup resolves the clock-variant via that stored normalized value.
    out = lookup_move_evals(session, [(POS_B, "f1c4")])
    assert out[(POS_B, "f1c4")] == MoveEval(cp=25, mate=None)
