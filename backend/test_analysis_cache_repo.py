"""Tests for the shared analysis_cache writer: policy wiring + concurrency."""

import os
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis_cache_repo import (
    UnsupportedDialectError,
    write_analysis_cache_rows,
)
from app.analysis_profiles import (
    BROWSER_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    get_profile,
)
from app.evidence_contracts import MINIMAL_PLAYED_EVAL, RESOLVER_COMPLETE
from app.models import AnalysisCache, Base

FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def file_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'cache.db'}")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    yield engine, Factory
    engine.dispose()


def _browser_row(played_eval=20):
    return {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "played_eval": played_eval,
        "source": "game",
        "analysis_profile_id": BROWSER_PROFILE_ID,
        "evidence_contract_id": MINIMAL_PLAYED_EVAL,
    }


def _canonical_values():
    p = get_profile(CANONICAL_PROFILE_ID)
    return {
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE,
        **{f: getattr(p, f) for f in IDENTITY_FIELDS},
    }


def _seed(Factory, row):
    s = Factory()
    s.add(AnalysisCache(**row))
    s.commit()
    s.close()


def test_insert_new_key(file_db):
    _, Factory = file_db
    s = Factory()
    write_analysis_cache_rows(s, [_browser_row()])
    s.close()
    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.played_eval == 20
    assert row.analysis_profile_id == BROWSER_PROFILE_ID
    s2.close()


def test_game_does_not_downgrade_canonical(file_db):
    _, Factory = file_db
    canonical = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "eval_delta": 0,
        "classification": "best",
        "played_eval": 20,
        "source": "precomputed",
        **_canonical_values(),
    }
    _seed(Factory, canonical)

    s = Factory()
    write_analysis_cache_rows(s, [_browser_row(played_eval=999)])
    s.close()

    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.source == "precomputed"
    assert row.played_eval == 20  # not clobbered
    s2.close()


def test_game_does_not_downgrade_legacy(file_db):
    _, Factory = file_db
    _seed(Factory, {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "played_eval": 11,
        "best_eval": 12,
        "source": "precomputed",  # legacy: no profile metadata
    })
    s = Factory()
    write_analysis_cache_rows(s, [_browser_row(played_eval=999)])
    s.close()
    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.played_eval == 11
    s2.close()


def test_authoritative_reclaims_legacy(file_db):
    _, Factory = file_db
    _seed(Factory, {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "played_eval": 11,
        "source": "jeffml-scores",  # legacy, no metadata
    })
    s = Factory()
    canonical = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "eval_delta": 0,
        "classification": "best",
        "played_eval": 11,
        "source": "precomputed",
        **_canonical_values(),
    }
    write_analysis_cache_rows(s, [canonical])
    s.close()
    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.analysis_profile_id == CANONICAL_PROFILE_ID
    assert row.best_line_uci == "e2e4 e7e5"
    s2.close()


def test_duplicate_conflict_in_batch_rejected(file_db):
    _, Factory = file_db
    a = _browser_row(played_eval=20)
    b = _browser_row(played_eval=50)  # same key, conflicting played_eval
    s = Factory()
    results = write_analysis_cache_rows(s, [a, b])
    s.close()
    from app.analysis_cache_policy import Reason
    assert any(r is Reason.DUPLICATE_CONFLICT for _, r in results)
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == 0
    s2.close()


def test_concurrent_writes_same_key_no_error(file_db):
    engine, Factory = file_db
    errors = []

    def worker(val):
        try:
            s = Factory()
            write_analysis_cache_rows(s, [_browser_row(played_eval=val)])
            s.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(v,)) for v in (10, 20, 30, 40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    s2 = Factory()
    # Exactly one row; first writer wins (browser is insert-missing-only).
    assert s2.query(AnalysisCache).count() == 1
    s2.close()


def test_shared_engine_not_put_into_immediate_mode(file_db):
    """A cache write must not turn the caller's engine into BEGIN-IMMEDIATE mode,
    which would make ordinary read-only sessions take write reservations."""
    engine, Factory = file_db
    s = Factory()
    write_analysis_cache_rows(s, [_browser_row()])
    s.close()

    # pysqlite's isolation_level on the shared engine must remain its default
    # (deferred) rather than the manual-mode None the dedicated write engine uses.
    raw = engine.raw_connection()
    try:
        assert raw.driver_connection.isolation_level is not None
    finally:
        raw.close()


def test_comparable_contract_rows_collapse_to_most_complete(file_db):
    """Same producer, comparable contracts (minimal vs resolver-complete) collapse
    to the most-complete row instead of being rejected as a conflict."""
    _, Factory = file_db
    minimal = {**_browser_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    complete = {
        **_browser_row(played_eval=20),
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "classification": "best", "eval_delta": 0,
        "evidence_contract_id": RESOLVER_COMPLETE,
    }
    from app.analysis_cache_policy import Reason
    for batch in ([minimal, complete], [complete, minimal]):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        results = write_analysis_cache_rows(s, batch)
        s.close()
        assert not any(r is Reason.DUPLICATE_CONFLICT for _, r in results)
        s2 = F()
        row = s2.query(AnalysisCache).one()
        assert row.evidence_contract_id == RESOLVER_COMPLETE
        s2.close()
        engine.dispose()


def test_invalid_richer_duplicate_does_not_suppress_valid(file_db):
    """A valid minimal row + an invalid (incomplete) resolver-complete row in the
    same batch stores the valid evidence rather than nothing."""
    valid_minimal = {**_browser_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    # resolver-complete claim but missing the multi-move PV -> contract invalid.
    invalid_richer = {
        **_browser_row(played_eval=20),
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "classification": "best", "eval_delta": 0,
        "evidence_contract_id": RESOLVER_COMPLETE,  # no best_line_uci => invalid
    }
    for batch in ([valid_minimal, invalid_richer], [invalid_richer, valid_minimal]):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        write_analysis_cache_rows(s, batch)
        s.close()
        s2 = F()
        row = s2.query(AnalysisCache).one()
        assert row.played_eval == 20
        assert row.evidence_contract_id == MINIMAL_PLAYED_EVAL
        s2.close()
        engine.dispose()


def test_identity_invalid_duplicate_does_not_suppress_valid(file_db):
    """A valid browser row + an identical row carrying contradictory identity
    metadata stores the valid one (not DUPLICATE_CONFLICT) in both orderings."""
    valid = {**_browser_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    # Same key/producer-ish, but claims browser-game-v1 with bogus engine metadata
    # -> identity does not verify -> invalid.
    identity_bad = {
        **_browser_row(played_eval=20),
        "evidence_contract_id": MINIMAL_PLAYED_EVAL,
        "engine_build": "bogus-build-for-browser-profile",
    }
    for batch in ([valid, identity_bad], [identity_bad, valid]):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        write_analysis_cache_rows(s, batch)
        s.close()
        s2 = F()
        row = s2.query(AnalysisCache).one()
        assert row.played_eval == 20
        assert row.engine_build is None  # the valid row, not the bogus claim
        s2.close()
        engine.dispose()


def test_contradictory_profile_claim_rejected(file_db):
    """A row claiming the canonical profile with mismatched engine metadata is not
    inserted (validate-before-insert)."""
    from app.analysis_cache_policy import Reason

    _, Factory = file_db
    bogus = {
        "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "eval_delta": 0, "classification": "best",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "engine_build": "WRONG-not-the-canonical-binary",  # mismatched identity
        "evidence_contract_id": RESOLVER_COMPLETE,
    }
    s = Factory()
    results = write_analysis_cache_rows(s, [bogus])
    s.close()
    assert any(r is Reason.INVALID_INCOMING_KEEP for _, r in results)
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == 0
    s2.close()


def test_profileless_legacy_row_still_inserts(file_db):
    """Profile-less (legacy) rows remain allowed even without identity metadata."""
    _, Factory = file_db
    legacy = {
        "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
        "played_eval": 11, "source": "precomputed",
        "evidence_contract_id": MINIMAL_PLAYED_EVAL,
    }
    s = Factory()
    write_analysis_cache_rows(s, [legacy])
    s.close()
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == 1
    s2.close()


def test_dedupe_rejects_differing_source(file_db):
    """Equal evidence but differing provenance is ambiguous -> reject the key."""
    from app.analysis_cache_policy import Reason
    a = {**_browser_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    b = {**_browser_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL,
         "source": "jeffml-scores"}
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    F = sessionmaker(bind=engine)
    s = F()
    results = write_analysis_cache_rows(s, [a, b])
    s.close()
    assert any(r is Reason.DUPLICATE_CONFLICT for _, r in results)
    s2 = F()
    assert s2.query(AnalysisCache).count() == 0
    s2.close()
    engine.dispose()


def test_dedupe_order_independent_incomparable_fields(file_db):
    """Two same-key rows with incomparable extra fields reject regardless of order."""
    from app.analysis_cache_policy import Reason

    def rows(order):
        a = {**_browser_row(played_eval=20), "best_eval": 5,
             "evidence_contract_id": MINIMAL_PLAYED_EVAL}
        b = {**_browser_row(played_eval=20), "eval_delta": 0,
             "evidence_contract_id": MINIMAL_PLAYED_EVAL}
        return [a, b] if order else [b, a]

    for order in (True, False):
        engine = create_engine("sqlite://")  # fresh in-memory per order
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        results = write_analysis_cache_rows(s, rows(order))
        s.close()
        assert any(r is Reason.DUPLICATE_CONFLICT for _, r in results)
        s2 = F()
        assert s2.query(AnalysisCache).count() == 0
        s2.close()
        engine.dispose()


CROSS_PROC_SCRIPT = """
import sys, time
sys.path.insert(0, {backend!r})
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import BROWSER_PROFILE_ID
from app.evidence_contracts import MINIMAL_PLAYED_EVAL

engine = create_engine("sqlite:///{db}")
F = sessionmaker(bind=engine)
row = {{
    "fen_before": "{fen}", "move_uci": "e2e4", "move_san": "e4",
    "played_eval": int(sys.argv[1]), "source": "game",
    "analysis_profile_id": BROWSER_PROFILE_ID,
    "evidence_contract_id": MINIMAL_PLAYED_EVAL,
}}
barrier = float(sys.argv[2])
time.sleep(max(0.0, barrier - time.time()))  # synchronize the two processes
s = F()
write_analysis_cache_rows(s, [row])
s.close()
"""


def test_cross_process_sqlite_writes_serialize(tmp_path):
    """Two synchronized OS processes writing the same key must not raise UNIQUE."""
    import subprocess
    import sys
    from pathlib import Path

    db = tmp_path / "xproc.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()

    backend = str(Path(__file__).resolve().parent)
    script = tmp_path / "writer.py"
    script.write_text(CROSS_PROC_SCRIPT.format(backend=backend, db=db, fen=FEN))

    start = f"{__import__('time').time() + 1.0:.6f}"
    procs = [
        subprocess.Popen([sys.executable, str(script), str(v), start],
                         stderr=subprocess.PIPE, text=True)
        for v in (10, 20)
    ]
    errs = [p.communicate()[1] for p in procs]
    codes = [p.returncode for p in procs]

    assert codes == [0, 0], f"a writer failed: {errs}"
    s = sessionmaker(bind=create_engine(f"sqlite:///{db}"))()
    assert s.query(AnalysisCache).count() == 1
    s.close()


def test_unsupported_dialect_rejected(monkeypatch, file_db):
    _, Factory = file_db
    s = Factory()

    class FakeDialect:
        name = "oracle"

    class FakeBind:
        dialect = FakeDialect()

    monkeypatch.setattr(s, "get_bind", lambda: FakeBind())
    monkeypatch.setattr(s, "in_transaction", lambda: False)
    with pytest.raises(UnsupportedDialectError):
        write_analysis_cache_rows(s, [_browser_row()])
    s.close()


def _pg_url():
    return os.environ.get("GHOSTREPLAY_TEST_PG_URL") or os.environ.get("TEST_DATABASE_URL_PG")


pg_required = pytest.mark.skipif(
    not _pg_url(), reason="set GHOSTREPLAY_TEST_PG_URL to run PostgreSQL locking tests"
)


@pytest.fixture
def pg_db():
    url = _pg_url()
    engine = create_engine(url)
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    conn.close()
    AnalysisCache.__table__.drop(engine, checkfirst=True)
    AnalysisCache.__table__.create(engine)
    Factory = sessionmaker(bind=engine)
    yield engine, Factory
    AnalysisCache.__table__.drop(engine, checkfirst=True)
    engine.dispose()


@pg_required
def test_pg_insert_then_browser_keeps_canonical(pg_db):
    """Exercises _process_pg_row: insert-first + FOR UPDATE comparator path."""
    _, Factory = pg_db
    s = Factory()
    canonical = {
        "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5", "eval_delta": 0, "classification": "best",
        "played_eval": 20, "source": "precomputed", **_canonical_values(),
    }
    write_analysis_cache_rows(s, [canonical])
    s.close()

    # Browser row for the same key must be kept out (non-authoritative).
    s = Factory()
    write_analysis_cache_rows(s, [_browser_row(played_eval=999)])
    s.close()

    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.source == "precomputed"
    assert row.played_eval == 20
    s2.close()


@pg_required
def test_pg_concurrent_inserts_single_row(pg_db):
    _, Factory = pg_db
    errors = []

    def worker(val):
        try:
            s = Factory()
            write_analysis_cache_rows(s, [_browser_row(played_eval=val)])
            s.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(v,)) for v in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == 1
    s2.close()


def test_clean_session_precondition(file_db):
    _, Factory = file_db
    s = Factory()
    s.execute(__import__("sqlalchemy").text("SELECT 1"))  # opens a transaction
    with pytest.raises(RuntimeError):
        write_analysis_cache_rows(s, [_browser_row()])
    s.rollback()
    s.close()


def test_round_trip_best_eval_mate_and_nnue_columns(file_db):
    """best_eval_mate + the two NNUE-identity columns persist and read back."""
    _, Factory = file_db
    p = get_profile(CANONICAL_PROFILE_ID)
    row = {
        "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 10, "played_eval_mate": None,
        "best_eval": 40, "best_eval_mate": 3,
        "eval_delta": 30, "classification": "good",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE,
        **{f: getattr(p, f) for f in IDENTITY_FIELDS},
    }
    s = Factory()
    write_analysis_cache_rows(s, [row])
    s.close()
    s2 = Factory()
    stored = s2.query(AnalysisCache).one()
    assert stored.best_eval_mate == 3
    assert stored.eval_file_id == p.eval_file_id
    assert stored.eval_file_small_id == p.eval_file_small_id
    assert stored.analyzer_protocol_version == p.analyzer_protocol_version
    assert stored.profile_manifest_digest == p.profile_manifest_digest
    s2.close()


def test_dedupe_contract_upgrade_order_independent(file_db):
    """Identical v1/v2 canonical rows for one key store v2 regardless of order."""
    from app.evidence_contracts import RESOLVER_COMPLETE_V2

    def _row(contract):
        return {
            "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
            "best_move_uci": "e2e4", "best_move_san": "e4",
            "best_line_uci": "e2e4 e7e5",
            "played_eval": 0, "best_eval": 0, "eval_delta": 0,
            "classification": "best", "source": "precomputed",
            "analysis_profile_id": CANONICAL_PROFILE_ID,
            "evidence_contract_id": contract,
            **{f: get_profile(CANONICAL_PROFILE_ID).__getattribute__(f) for f in IDENTITY_FIELDS},
        }

    for order in ([RESOLVER_COMPLETE, RESOLVER_COMPLETE_V2],
                  [RESOLVER_COMPLETE_V2, RESOLVER_COMPLETE]):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        write_analysis_cache_rows(s, [_row(order[0]), _row(order[1])])
        s.close()
        s2 = F()
        row = s2.query(AnalysisCache).one()
        assert row.evidence_contract_id == RESOLVER_COMPLETE_V2, f"order={order}"
        s2.close()
        engine.dispose()


def test_dedupe_merges_richer_evidence_under_successor_contract(file_db):
    """v1 (with best_eval_mate) + v2 (without) collapse to best_eval_mate kept AND
    the v2 contract, regardless of input order."""
    from app.evidence_contracts import RESOLVER_COMPLETE_V2

    p = get_profile(CANONICAL_PROFILE_ID)

    def _row(contract, best_eval_mate):
        return {
            "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
            "best_move_uci": "e2e4", "best_move_san": "e4",
            "best_line_uci": "e2e4 e7e5",
            "played_eval": 0, "best_eval": 0, "eval_delta": 0,
            "best_eval_mate": best_eval_mate,
            "classification": "best", "source": "precomputed",
            "analysis_profile_id": CANONICAL_PROFILE_ID,
            "evidence_contract_id": contract,
            **{f: getattr(p, f) for f in IDENTITY_FIELDS},
        }

    v1 = _row(RESOLVER_COMPLETE, 3)
    v2 = _row(RESOLVER_COMPLETE_V2, None)
    for order in ([v1, v2], [v2, v1]):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        F = sessionmaker(bind=engine)
        s = F()
        write_analysis_cache_rows(s, [dict(order[0]), dict(order[1])])
        s.close()
        s2 = F()
        row = s2.query(AnalysisCache).one()
        assert row.evidence_contract_id == RESOLVER_COMPLETE_V2, f"order contract {order}"
        assert row.best_eval_mate == 3, f"richer evidence kept {order}"
        s2.close()
        engine.dispose()
