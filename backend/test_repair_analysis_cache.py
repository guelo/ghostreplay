"""End-to-end tests for scripts/repair_analysis_cache.py against SQLite.

Asserts the repair tool's contract: the audit tallies every row by category
(memory-bounded, no id retention), an ``--apply`` run deletes only contaminated
rows (and, under the opt-in, guard-rejected legacy rows) while preserving
canonical / legacy-valid rows, the operation is idempotent, and a row repaired in
place between scan and delete is RE-CLASSIFIED under lock and spared.
"""
import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.analysis_cache_audit import Category
from app.analysis_profiles import IDENTITY_FIELDS, get_profile
from app.evidence_contracts import MINIMAL_PLAYED_EVAL, RESOLVER_COMPLETE_V2
from app.models import AnalysisCache, Base

PROFILE_ID = "canonical-sf18-depth24-linux-v1"
FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _load_script():
    scripts_dir = Path(__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    return importlib.import_module("repair_analysis_cache")


def _identity_columns() -> dict:
    profile = get_profile(PROFILE_ID)
    return {f: getattr(profile, f) for f in IDENTITY_FIELDS}


def _canonical(move_uci: str, **overrides) -> AnalysisCache:
    data = {
        "fen_before": FEN,
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
        "eval_delta": 20,
        "classification": "good",
    }
    data.update(_identity_columns())
    data.update(overrides)
    return AnalysisCache(**data)


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _seed(engine):
    with Session(engine) as db:
        db.add_all([
            _canonical("e2e4"),                              # canonical_trusted
            _canonical("d2d4", engine_build="0" * 64),       # contaminated (identity)
            _canonical("g1f3", eval_delta=999),              # contaminated (contract)
            AnalysisCache(                                    # legacy_valid
                fen_before=FEN, move_uci="c2c4", move_san="c4", source="game",
                evidence_contract_id=MINIMAL_PLAYED_EVAL, played_eval=12,
            ),
            AnalysisCache(                                    # legacy_invalid (empty)
                fen_before=FEN, move_uci="b1c3", move_san="Nc3", source="game",
            ),
        ])
        db.commit()


def test_audit_counts_every_category(engine):
    script = _load_script()
    _seed(engine)
    with Session(engine) as db:
        report = script.audit(db, include_legacy_null=False)
    assert report.total == 5
    assert report.counts[Category.CANONICAL_TRUSTED.value] == 1
    assert report.counts[Category.CONTAMINATED_PROFILE_CLAIM.value] == 2
    assert report.counts[Category.LEGACY_VALID.value] == 1
    assert report.counts[Category.LEGACY_INVALID.value] == 1
    assert report.invalidate_count(include_legacy_null=False) == 2
    assert report.invalidate_count(include_legacy_null=True) == 3


def test_apply_deletes_only_contaminated(engine):
    script = _load_script()
    _seed(engine)
    deleted = script.apply_invalidation(engine, include_legacy_null=False)
    assert deleted == 2
    with Session(engine) as db:
        survivors = {r.move_uci for r in db.query(AnalysisCache).all()}
    assert survivors == {"e2e4", "c2c4", "b1c3"}


def test_apply_is_idempotent(engine):
    script = _load_script()
    _seed(engine)
    script.apply_invalidation(engine, include_legacy_null=False)
    with Session(engine) as db:
        report = script.audit(db, include_legacy_null=False)
    assert report.invalidate_count(include_legacy_null=False) == 0


def test_legacy_opt_in_also_removes_guard_rejected_rows(engine):
    script = _load_script()
    _seed(engine)
    deleted = script.apply_invalidation(engine, include_legacy_null=True)
    assert deleted == 3  # 2 contaminated + 1 legacy_invalid
    with Session(engine) as db:
        survivors = {r.move_uci for r in db.query(AnalysisCache).all()}
    assert survivors == {"e2e4", "c2c4"}  # legacy_valid kept


def test_row_repaired_after_scan_is_spared(engine):
    """Finding 2: apply re-classifies at delete time, not from a stale id list."""
    script = _load_script()
    _seed(engine)
    # Simulate an overlapping precompute repairing the contaminated d2d4 row in
    # place (same id) to a canonical row between audit and apply.
    with Session(engine) as db:
        row = db.query(AnalysisCache).filter_by(move_uci="d2d4").one()
        for k, v in _identity_columns().items():
            setattr(row, k, v)
        row.evidence_contract_id = RESOLVER_COMPLETE_V2
        db.commit()
    deleted = script.apply_invalidation(engine, include_legacy_null=False)
    # Only g1f3 (still contaminated) is deleted; the repaired d2d4 survives.
    assert deleted == 1
    with Session(engine) as db:
        survivors = {r.move_uci for r in db.query(AnalysisCache).all()}
    assert "d2d4" in survivors
    assert "g1f3" not in survivors
