"""Resume-filter tests for precompute_openings.filter_unstored_positions.

Uses an in-memory SQLite DB and a real registered canonical profile to assert
that only TRUSTWORTHY stored rows (full identity + v2-contract valid) are
skipped, while malformed/foreign rows are kept for re-analysis.
"""
import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import RESOLVER_COMPLETE_V2
from app.models import AnalysisCache, Base

PROFILE_ID = "canonical-sf18-depth24-linux-v1"
START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


def _load_script():
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("precompute_openings")


def _identity_columns() -> dict:
    """The IDENTITY_FIELDS values that make a row pass ``_identity_verified``."""
    profile = get_profile(PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _valid_row(fen: str, move_uci: str) -> dict:
    """A row that passes both the identity gate and v2 contract validation.

    White to move at ``fen``: delta = best_eval - played_eval (white-relative).
    """
    row = {
        "fen_before": fen,
        "move_uci": move_uci,
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 10,
        "played_eval_mate": None,
        "best_eval": 30,
        "best_eval_mate": None,
        "eval_delta": 20,  # white to move: 30 - 10
        "classification": "good",
    }
    row.update(_identity_columns())
    return row


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _insert(db, row: dict):
    db.add(AnalysisCache(**row))
    db.commit()


def _positions(script, pairs):
    return [
        script.PositionToAnalyze(fen_before=f, move_uci=m, move_san="x")
        for f, m in pairs
    ]


def test_valid_row_is_skipped(db):
    script = _load_script()
    _insert(db, _valid_row(START_FEN, "e2e4"))
    positions = _positions(script, [(START_FEN, "e2e4"), (E4_FEN, "e7e5")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 1
    assert [(p.fen_before, p.move_uci) for p in remaining] == [(E4_FEN, "e7e5")]


def test_all_stored_yields_empty_remaining(db):
    script = _load_script()
    _insert(db, _valid_row(START_FEN, "e2e4"))
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert remaining == []
    assert already == 1


def test_bad_identity_row_is_not_skipped(db):
    script = _load_script()
    row = _valid_row(START_FEN, "e2e4")
    row["engine_build"] = "deadbeef"  # identity no longer matches the profile
    _insert(db, row)
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 0
    assert len(remaining) == 1


def test_contract_violating_row_is_not_skipped(db):
    script = _load_script()
    row = _valid_row(START_FEN, "e2e4")
    row["eval_delta"] = 999  # inconsistent with best_eval - played_eval
    _insert(db, row)
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 0
    assert len(remaining) == 1


def test_single_move_pv_row_is_not_skipped(db):
    script = _load_script()
    row = _valid_row(START_FEN, "e2e4")
    row["best_line_uci"] = "e2e4"  # PV length 1 fails the v2 contract
    _insert(db, row)
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 0
    assert len(remaining) == 1


def test_foreign_profile_row_is_not_skipped(db):
    script = _load_script()
    row = _valid_row(START_FEN, "e2e4")
    row["analysis_profile_id"] = "canonical-sf18-depth24-v1"  # different profile
    _insert(db, row)
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 0
    assert len(remaining) == 1


def test_v1_contract_row_is_not_skipped(db):
    script = _load_script()
    row = _valid_row(START_FEN, "e2e4")
    row["evidence_contract_id"] = "resolver-complete-v1"  # not the v2 target
    _insert(db, row)
    positions = _positions(script, [(START_FEN, "e2e4")])

    remaining, already = script.filter_unstored_positions(db, positions, PROFILE_ID)

    assert already == 0
    assert len(remaining) == 1
