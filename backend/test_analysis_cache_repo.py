"""Tests for the shared analysis_cache writer: policy wiring + concurrency."""

import logging
import os
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.analysis_cache_repo import (
    UnsupportedDialectError,
    write_analysis_cache_rows,
)
from app.database_url import _normalize_postgres_scheme
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
    url = _normalize_postgres_scheme(_pg_url())
    engine = create_engine(url)
    try:
        conn = engine.connect()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL not reachable: {exc}")
    conn.close()
    AnalysisCache.__table__.create(engine, checkfirst=True)
    with engine.begin() as cleanup:
        cleanup.execute(AnalysisCache.__table__.delete())
    Factory = sessionmaker(bind=engine)
    yield engine, Factory
    # lock_timeout so a genuinely hung/deadlocked worker holding row locks makes
    # the teardown DELETE fail fast instead of blocking pytest shutdown forever.
    with engine.begin() as cleanup:
        cleanup.execute(text("SET LOCAL lock_timeout = '10s'"))
        cleanup.execute(AnalysisCache.__table__.delete())
    engine.dispose()


@pg_required
def test_pg_insert_then_browser_keeps_canonical(pg_db):
    """Exercises the PG batch path: ON CONFLICT insert + FOR UPDATE comparator."""
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


@pg_required
def test_verdict_matrix_pg(pg_db):
    """The differential verdict matrix on PostgreSQL (insert-first + FOR UPDATE)."""
    _, Factory = pg_db
    _run_verdict_matrix(Factory)


@pg_required
@pytest.mark.parametrize("n", [10, 40])
def test_statement_count_idempotent_constant_pg(pg_db, n):
    """PRIMARY guarantee: on PostgreSQL a single-signature idempotent N-row
    re-upload issues a constant number of statements (≈1 INSERT + 1 SELECT), not
    O(N), and does not grow with N."""
    engine, Factory = pg_db
    _assert_idempotent_constant(engine, Factory, n)  # engine is fixture-owned


@pg_required
def test_pg_statement_count_overwrite_no_per_row_roundtrip(pg_db):
    """Overwrite path: UPDATEs are bounded by changed rows with no per-row
    SELECT/INSERT round-trips (one INSERT + one SELECT for the whole batch)."""
    engine, Factory = pg_db
    keys = [f"leg-{i}" for i in range(10)]
    for k in keys:  # seed legacy rows the canonical batch will reclaim
        _seed(Factory, {"fen_before": k, "move_uci": "e2e4", "move_san": "e4",
                        "played_eval": 5, "source": "jeffml-scores"})

    batch = [_canonical_full(k) for k in keys]  # all LEGACY_REPLACED_BY_AUTH
    with _statement_counter(engine) as counts:
        s = Factory(); results = write_analysis_cache_rows(s, batch); s.close()

    from app.analysis_cache_policy import Reason
    assert all(r is Reason.LEGACY_REPLACED_BY_AUTH for _, r in results)
    assert counts["INSERT"] == 1, counts            # one multi-row conflict INSERT
    assert counts["SELECT"] == 1, counts            # one FOR UPDATE over conflicts
    assert counts["UPDATE"] <= len(keys), counts    # bounded by changed rows


def _run_concurrent_batches(Factory, batches, *, timeout=30.0):
    """Run one worker per batch, all released into write_analysis_cache_rows at the
    same instant via a Barrier.

    Without the barrier a fast local PG can fully serialize one worker's whole
    write before another thread reaches the insert path, so the deadlock-ordering
    property under real overlap would never be exercised. The barrier forces every
    worker to hit the INSERT ... ON CONFLICT concurrently. Joins are bounded so a
    genuine deadlock/hang surfaces as a failure (a hung thread, or a PG
    deadlock-detector abort captured in ``errors``) rather than blocking forever.
    """
    errors = []
    barrier = threading.Barrier(len(batches))

    def worker(batch):
        try:
            barrier.wait(timeout=timeout)  # release all workers together
            s = Factory()
            try:
                write_analysis_cache_rows(s, batch)
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    # daemon=True so a genuinely hung worker cannot keep the interpreter alive at
    # exit: the bounded join below turns a hang into a failed assert, and CPython
    # would otherwise block shutdown joining a live non-daemon thread (CI hang).
    threads = [threading.Thread(target=worker, args=(b,), daemon=True) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    assert not any(t.is_alive() for t in threads), "a worker hung (possible deadlock)"
    return errors


@pg_required
def test_pg_concurrent_overlapping_batches_single_row(pg_db, monkeypatch):
    """Two+ concurrent overlapping multi-key batches, released together: no
    deadlock, one row per key. Validates the ORDER BY / key-sorted insert lock
    ordering under genuine contention at batch scale."""
    import app.analysis_cache_repo as repo
    # These batches are deleter-free, so the invariant must give ZERO deadlocks.
    # Disable the retry so a regression that breaks it surfaces as an error here
    # instead of being silently absorbed by _run_postgresql.
    monkeypatch.setattr(repo, "_PG_MAX_RETRIES", 0)
    _, Factory = pg_db
    keys = [f"cpos-{i}" for i in range(20)]
    batches = [
        [{**_browser_row(played_eval=val), "fen_before": k} for k in keys]
        for val in (10, 20, 30, 40)
    ]

    errors = _run_concurrent_batches(Factory, batches)

    assert not errors
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == len(keys)
    s2.close()


@pg_required
def test_pg_concurrent_mixed_signature_batches_no_deadlock(pg_db, monkeypatch):
    """Concurrent overlapping batches, released together, carrying DIFFERENT per-key
    present-column signatures (so the insert pass splits into multiple statements
    laid out differently per thread). Because insertion still proceeds in global
    key order, no speculative-lock deadlock: assert no error and one row per key."""
    import app.analysis_cache_repo as repo
    # Deleter-free: the invariant must yield zero deadlocks. Disable the retry so
    # a broken invariant surfaces as an error rather than being retried away.
    monkeypatch.setattr(repo, "_PG_MAX_RETRIES", 0)
    _, Factory = pg_db
    keys = [f"mpos-{i}" for i in range(20)]

    def batch(swap):
        out = []
        for i, k in enumerate(keys):
            row = {**_browser_row(played_eval=20), "fen_before": k}
            if (i % 2 == 0) ^ swap:
                row["best_eval"] = 5  # extra column -> different insert signature
            out.append(row)
        return out

    batches = [batch(sw) for sw in (False, True, False, True)]

    errors = _run_concurrent_batches(Factory, batches)

    assert not errors
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == len(keys)
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


# ---------------------------------------------------------------------------
# Batch pipeline (g-dckw): verdict-matrix differential + statement-count tests.
# ---------------------------------------------------------------------------


def _canonical_full(fen, **over):
    """A complete, valid canonical (authoritative) row for ``fen``."""
    row = {
        "fen_before": fen, "move_uci": "e2e4", "move_san": "e4",
        "best_move_uci": "e2e4", "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 10, "best_eval": 10, "eval_delta": 0,
        "classification": "best", "source": "precomputed",
        **_canonical_values(),
    }
    row.update(over)
    return row


def _run_verdict_matrix(Factory):
    """One batch spanning every verdict class; assert the returned (key, Reason)
    list EXACTLY (cardinality + order) and the final table state row-by-row.

    This is the differential guard: the batch pipeline must reproduce the per-row
    loop's semantics and return contract byte-for-byte.
    """
    from app.analysis_cache_policy import Reason

    # Seed pre-existing rows for the keys whose verdict depends on an incumbent.
    _seed(Factory, {**_browser_row(played_eval=20), "fen_before": "k2"})   # idempotent
    _seed(Factory, _canonical_full("k3"))                                  # non-auth keep
    _seed(Factory, {"fen_before": "k4", "move_uci": "e2e4", "move_san": "e4",
                    "played_eval": 5, "source": "jeffml-scores"})          # legacy -> replace
    _seed(Factory, _canonical_full("k5"))                                  # superset merge
    _seed(Factory, _canonical_full("k6", played_eval=10))                  # merge conflict keep

    batch = [
        {**_browser_row(played_eval=20), "fen_before": "k1"},                       # NEW_KEY
        {**_browser_row(played_eval=20), "fen_before": "k2"},                       # SAME_PROFILE_IDEMPOTENT
        {**_browser_row(played_eval=999), "fen_before": "k3"},                      # NON_AUTHORITATIVE_KEEP
        _canonical_full("k4"),                                                      # LEGACY_REPLACED_BY_AUTH
        _canonical_full("k5", best_eval_mate=3),                                    # SAME_PROFILE_SUPERSET_MERGE
        _canonical_full("k6", played_eval=20),                                      # MERGE_CONFLICT_KEEP
        _canonical_full("k7", engine_build="WRONG-not-canonical"),                  # INVALID_INCOMING_KEEP
        {**_browser_row(played_eval=20), "fen_before": "k8"},                       # DUPLICATE_CONFLICT (a)
        {**_browser_row(played_eval=50), "fen_before": "k8"},                       # DUPLICATE_CONFLICT (b)
    ]

    s = Factory()
    results = write_analysis_cache_rows(s, batch)
    s.close()

    expected = [
        (("k8", "e2e4"), Reason.DUPLICATE_CONFLICT),      # dedupe rejects first
        (("k1", "e2e4"), Reason.NEW_KEY),                 # then survivors, key-sorted
        (("k2", "e2e4"), Reason.SAME_PROFILE_IDEMPOTENT),
        (("k3", "e2e4"), Reason.NON_AUTHORITATIVE_KEEP),
        (("k4", "e2e4"), Reason.LEGACY_REPLACED_BY_AUTH),
        (("k5", "e2e4"), Reason.SAME_PROFILE_SUPERSET_MERGE),
        (("k6", "e2e4"), Reason.MERGE_CONFLICT_KEEP),
        (("k7", "e2e4"), Reason.INVALID_INCOMING_KEEP),   # invalid interleaved, not trailing
    ]
    assert results == expected

    # Final table state matches the verdicts exactly.
    s2 = Factory()
    by_fen = {r.fen_before: r for r in s2.query(AnalysisCache).all()}
    assert set(by_fen) == {"k1", "k2", "k3", "k4", "k5", "k6"}  # k7/k8 never stored
    assert by_fen["k1"].played_eval == 20
    assert by_fen["k1"].analysis_profile_id == BROWSER_PROFILE_ID
    assert by_fen["k2"].played_eval == 20                        # unchanged
    assert by_fen["k2"].analysis_profile_id == BROWSER_PROFILE_ID
    assert by_fen["k3"].analysis_profile_id == CANONICAL_PROFILE_ID  # not downgraded
    assert by_fen["k3"].played_eval == 10
    assert by_fen["k4"].analysis_profile_id == CANONICAL_PROFILE_ID  # replaced
    assert by_fen["k4"].best_line_uci == "e2e4 e7e5"
    assert by_fen["k5"].best_eval_mate == 3                      # merged in
    assert by_fen["k5"].analysis_profile_id == CANONICAL_PROFILE_ID
    assert by_fen["k6"].played_eval == 10                        # merge conflict -> kept
    s2.close()


def test_verdict_matrix_sqlite(file_db):
    _, Factory = file_db
    _run_verdict_matrix(Factory)


def test_large_batch_chunks_insert_and_select(monkeypatch):
    """g-dckw/#1: a batch exceeding the per-statement bind-param budget is split
    into several ordered INSERT/SELECT statements (never one oversized statement
    the driver would reject and roll the whole batch back), and every row lands."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    monkeypatch.setattr(repo, "_MAX_BIND_PARAMS", 40)  # ~5 rows/insert, 20 keys/select

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    F = sessionmaker(bind=engine)
    rows = _distinct_browser_rows(37)  # forces multiple insert chunks

    with _statement_counter(engine) as counts:
        s = F(); results = write_analysis_cache_rows(s, rows); s.close()  # all fresh
    assert all(r is Reason.NEW_KEY for _, r in results)
    assert counts["INSERT"] > 1, counts  # chunked, not one oversized INSERT
    s2 = F(); assert s2.query(AnalysisCache).count() == 37; s2.close()

    # Re-upload: every key conflicts -> the conflict SELECT is chunked too.
    with _statement_counter(engine) as counts2:
        s = F(); res2 = write_analysis_cache_rows(s, [dict(r) for r in rows]); s.close()
    assert all(r is Reason.SAME_PROFILE_IDEMPOTENT for _, r in res2)
    assert counts2["SELECT"] > 1, counts2  # chunked conflict select
    assert counts2["UPDATE"] == 0, counts2
    s3 = F(); assert s3.query(AnalysisCache).count() == 37; s3.close()
    engine.dispose()


def test_vanished_row_recovered_when_no_competitor(file_db, monkeypatch):
    """g-dckw/#2,#3: a key that conflicts on insert but is gone by the lock (the
    concurrent-deleter TOCTOU window) is recovered via ON CONFLICT DO NOTHING and
    — with no competing row present — reported NEW_KEY in a single pass."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    _, Factory = file_db
    _seed(Factory, {**_browser_row(played_eval=20), "fen_before": "vanish"})

    real_lock = repo._lock_existing
    calls = {"n": 0}

    def flaky_lock(session, conflicted, *, for_update):
        calls["n"] += 1
        if calls["n"] == 1:  # deleter wins the insert/lock race: the row is gone
            session.query(AnalysisCache).filter(
                AnalysisCache.fen_before == "vanish"
            ).delete()
            return {}
        return real_lock(session, conflicted, for_update=for_update)

    monkeypatch.setattr(repo, "_lock_existing", flaky_lock)

    s = Factory()
    results = write_analysis_cache_rows(
        s, [{**_browser_row(played_eval=20), "fen_before": "vanish"}]
    )
    s.close()

    assert calls["n"] == 1  # recovery insert succeeded; no re-resolve needed
    assert results == [(("vanish", "e2e4"), Reason.NEW_KEY)]
    s2 = Factory()
    row = s2.query(AnalysisCache).filter(AnalysisCache.fen_before == "vanish").one()
    assert row.played_eval == 20  # the recovered row is present
    s2.close()


def test_vanished_row_reresolved_against_concurrent_recreate(file_db, monkeypatch):
    """g-dckw/#3 (discriminating): when a concurrent writer RE-CREATES the vanished
    key, the recovery insert must lose the ON CONFLICT DO NOTHING — a bare
    session.add would raise IntegrityError and abort the whole batch — and the key
    is re-locked and decided against that live row on the next pass."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    _, Factory = file_db
    _seed(Factory, {**_browser_row(played_eval=20), "fen_before": "vanish"})

    real_lock = repo._lock_existing
    calls = {"n": 0}

    def flaky_lock(session, conflicted, *, for_update):
        calls["n"] += 1
        if calls["n"] == 1:
            # Deleter removes our row AND a writer re-creates it as canonical, so
            # our recovery insert will conflict (DO NOTHING) instead of inserting.
            session.query(AnalysisCache).filter(
                AnalysisCache.fen_before == "vanish"
            ).delete()
            session.add(AnalysisCache(**_canonical_full("vanish")))
            session.flush()
            return {}  # appears vanished to us this pass
        return real_lock(session, conflicted, for_update=for_update)

    monkeypatch.setattr(repo, "_lock_existing", flaky_lock)

    s = Factory()
    results = write_analysis_cache_rows(
        s, [{**_browser_row(played_eval=999), "fen_before": "vanish"}]
    )
    s.close()

    assert calls["n"] == 2  # recovery conflicted -> re-locked + decided on pass 2
    assert results == [(("vanish", "e2e4"), Reason.NON_AUTHORITATIVE_KEEP)]
    s2 = Factory()
    row = s2.query(AnalysisCache).filter(AnalysisCache.fen_before == "vanish").one()
    assert row.analysis_profile_id == CANONICAL_PROFILE_ID  # competitor kept, not clobbered
    s2.close()


def test_toctou_exhaustion_reports_recovery_aborted_not_new_key(
    file_db, monkeypatch, caplog
):
    """g-dckw fix-round/#1: a key a persistent deleter keeps vanishing past the
    pass budget was neither written nor resolved. It must be reported as
    RECOVERY_ABORTED_KEEP (a NON-accepted verdict) and warned — NOT a phantom
    NEW_KEY that would inflate 'accepted' writes and hide the dropped write."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    monkeypatch.setattr(repo, "_MAX_TOCTOU_PASSES", 1)
    _, Factory = file_db
    _seed(Factory, {**_browser_row(played_eval=20), "fen_before": "vanish"})

    calls = {"n": 0}

    def always_vanished(session, conflicted, *, for_update):
        calls["n"] += 1
        if calls["n"] == 1:
            # Install a persistent competitor so every recovery insert conflicts,
            # and never surface the row to our code so it can neither resolve nor
            # insert it — the pathological oscillation the terminal branch guards.
            session.query(AnalysisCache).filter(
                AnalysisCache.fen_before == "vanish"
            ).delete()
            session.add(AnalysisCache(**_canonical_full("vanish")))
            session.flush()
        return {}

    monkeypatch.setattr(repo, "_lock_existing", always_vanished)

    with caplog.at_level(logging.WARNING, logger="analysis_cache_repo"):
        s = Factory()
        results = write_analysis_cache_rows(
            s, [{**_browser_row(played_eval=999), "fen_before": "vanish"}]
        )
        s.close()

    assert calls["n"] == 2  # 1 recovery pass + 1 terminal lock, both vanished
    assert results == [(("vanish", "e2e4"), Reason.RECOVERY_ABORTED_KEEP)]
    assert "recovery aborted" in caplog.text
    # The competitor row is kept (never clobbered); our incoming 999 is not stored.
    s2 = Factory()
    assert s2.query(AnalysisCache).filter(
        AnalysisCache.fen_before == "vanish"
    ).one().analysis_profile_id == CANONICAL_PROFILE_ID
    s2.close()


def test_pg_retry_error_classification():
    """g-dckw fix-round/#4: _is_retryable_pg_error keys on SQLSTATE (psycopg2
    pgcode / psycopg3 sqlstate) for deadlock (40P01) and serialization (40001),
    with a PG-wording text fallback, and rejects everything else."""
    import app.analysis_cache_repo as repo
    from sqlalchemy.exc import OperationalError

    def op_err(*, pgcode=None, sqlstate=None, text="boom"):
        orig = Exception()
        if pgcode is not None:
            orig.pgcode = pgcode
        if sqlstate is not None:
            orig.sqlstate = sqlstate
        return OperationalError(text, {}, orig)

    assert repo._is_retryable_pg_error(op_err(pgcode="40P01"))     # deadlock (psycopg2)
    assert repo._is_retryable_pg_error(op_err(sqlstate="40001"))   # serialization (psycopg3)
    assert not repo._is_retryable_pg_error(op_err(pgcode="23505"))  # unique violation
    # Text fallback when no SQLSTATE is surfaced (real PG wording).
    assert repo._is_retryable_pg_error(op_err(text="deadlock detected"))
    assert repo._is_retryable_pg_error(
        op_err(text="could not serialize access due to concurrent update")
    )
    assert not repo._is_retryable_pg_error(op_err(text="syntax error at or near"))


def test_run_postgresql_retries_then_succeeds(monkeypatch):
    """g-dckw fix-round/#4: _run_postgresql re-runs the whole batch on a fresh
    session after a retryable deadlock, rolling back and closing each failed
    attempt, and returns the eventual result."""
    import app.analysis_cache_repo as repo
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(repo.time, "sleep", lambda *a, **k: None)

    class FakeSession:
        def __init__(self):
            self.rolled_back = False
            self.closed = False

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    sessions: list[FakeSession] = []

    def factory():
        s = FakeSession()
        sessions.append(s)
        return s

    sentinel = [(("k", "e2e4"), repo.Reason.NEW_KEY)]
    calls = {"n": 0}

    def fake_run_batch(session, surviving, *, insert, for_update):
        calls["n"] += 1
        assert for_update is True
        if calls["n"] <= 2:  # two deadlocks, then success
            orig = Exception()
            orig.pgcode = "40P01"
            raise OperationalError("deadlock detected", {}, orig)
        return sentinel

    monkeypatch.setattr(repo, "_run_batch", fake_run_batch)

    result = repo._run_postgresql(factory, [{"fen_before": "k", "move_uci": "e2e4"}])

    assert result is sentinel
    assert calls["n"] == 3  # retried twice, succeeded on the third attempt
    assert len(sessions) == 3
    assert sessions[0].rolled_back and sessions[1].rolled_back
    assert all(s.closed for s in sessions)  # every attempt's session was closed


def test_run_postgresql_reraises_non_retryable(monkeypatch):
    """A non-deadlock OperationalError (e.g. unique violation) is not retried."""
    import app.analysis_cache_repo as repo
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(repo.time, "sleep", lambda *a, **k: None)

    class FakeSession:
        def rollback(self):
            pass

        def close(self):
            pass

    calls = {"n": 0}

    def fake_run_batch(session, surviving, *, insert, for_update):
        calls["n"] += 1
        orig = Exception()
        orig.pgcode = "23505"
        raise OperationalError("duplicate key value", {}, orig)

    monkeypatch.setattr(repo, "_run_batch", fake_run_batch)

    with pytest.raises(OperationalError):
        repo._run_postgresql(FakeSession, [{"fen_before": "k", "move_uci": "e2e4"}])
    assert calls["n"] == 1  # raised on the first attempt, no retry


def test_source_default_preserved_on_absent_column(file_db):
    """A row omitting ``source`` stores the server default, never NULL."""
    _, Factory = file_db
    row = {
        "fen_before": FEN, "move_uci": "e2e4", "move_san": "e4",
        "played_eval": 11,
        "evidence_contract_id": MINIMAL_PLAYED_EVAL,  # legacy (no profile) -> valid
    }
    assert "source" not in row
    s = Factory()
    write_analysis_cache_rows(s, [row])
    s.close()
    s2 = Factory()
    assert s2.query(AnalysisCache).one().source == "game"
    s2.close()


@contextmanager
def _statement_counter(engine):
    """Count write/read statements by verb on ``engine`` for the ``with`` body.

    Detaches the listener on exit (matches the ``event.remove`` pattern in
    test_srs_opportunity.py) so a reused engine can't accumulate stale counters.
    """
    from collections import Counter

    from sqlalchemy import event

    counts: Counter = Counter()

    @event.listens_for(engine, "before_cursor_execute")
    def _rec(conn, cursor, statement, params, context, executemany):  # pragma: no cover - thin
        head = statement.lstrip().split(None, 1)
        if head and head[0].upper() in ("INSERT", "SELECT", "UPDATE", "DELETE"):
            counts[head[0].upper()] += 1

    try:
        yield counts
    finally:
        event.remove(engine, "before_cursor_execute", _rec)


def _distinct_browser_rows(n):
    # Distinct keys; single (fixed) present-column signature -> one INSERT run.
    return [{**_browser_row(played_eval=20), "fen_before": f"pos-{i}"} for i in range(n)]


def _assert_idempotent_constant(engine, Factory, n):
    """Seed N distinct single-signature rows, then re-upload the identical batch and
    assert the re-upload is a constant 1 INSERT + 1 SELECT + 0 UPDATE (every row
    idempotent-KEEP), independent of N. Shared by the PG and SQLite contracts so
    the two can't silently diverge."""
    rows = _distinct_browser_rows(n)
    s = Factory(); write_analysis_cache_rows(s, rows); s.close()  # seed fresh

    with _statement_counter(engine) as counts:
        s = Factory(); write_analysis_cache_rows(s, [dict(r) for r in rows]); s.close()

    assert counts["INSERT"] == 1, counts
    assert counts["SELECT"] == 1, counts   # single (FOR UPDATE) select over all conflicts
    assert counts["UPDATE"] == 0, counts   # every row idempotent-KEEP


@pytest.mark.parametrize("n", [10, 40])
def test_statement_count_idempotent_constant_sqlite(n):
    """Idempotent single-signature re-upload is a constant number of statements
    (1 INSERT + 1 SELECT, 0 UPDATE), independent of N. Counter is on the in-memory
    engine (the helper reuses the caller bind there, so it sees the writes)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    F = sessionmaker(bind=engine)
    _assert_idempotent_constant(engine, F, n)
    engine.dispose()  # test-owned engine (not a fixture)
