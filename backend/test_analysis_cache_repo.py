"""Tests for the shared analysis_cache writer: policy wiring + concurrency."""

import logging
import os
import threading
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.analysis_cache_repo import (
    UnsupportedDialectError,
    write_analysis_cache_rows,
)
from app.database_url import _normalize_postgres_scheme
from app.analysis_profiles import (
    CANONICAL_PROFILE_ID,
    IDENTITY_FIELDS,
    JEFFML_PROFILE_ID,
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


def _passive_row(played_eval=20):
    """A writable non-authoritative row. The profile is a carrier for the writer
    mechanics under test (batching, chunking, locking, dedupe), not the subject —
    it only has to be ACTIVE, non-authoritative, not replacement-eligible, and
    all-``None`` identity. browser-game-v1 filled that role until g-bgv1-cutover
    retired it; jeffml-scores-v1 carries the same flags and is a real registry
    entry, so it also resolves inside CROSS_PROC_SCRIPT's subprocess."""
    return {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "played_eval": played_eval,
        "source": "game",
        "analysis_profile_id": JEFFML_PROFILE_ID,
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
    write_analysis_cache_rows(s, [_passive_row()])
    s.close()
    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.played_eval == 20
    assert row.analysis_profile_id == JEFFML_PROFILE_ID
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
    write_analysis_cache_rows(s, [_passive_row(played_eval=999)])
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
    write_analysis_cache_rows(s, [_passive_row(played_eval=999)])
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
    a = _passive_row(played_eval=20)
    b = _passive_row(played_eval=50)  # same key, conflicting played_eval
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
            write_analysis_cache_rows(s, [_passive_row(played_eval=val)])
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
    # Exactly one row; first writer wins (a passive row is insert-missing-only).
    assert s2.query(AnalysisCache).count() == 1
    s2.close()


def test_shared_engine_not_put_into_immediate_mode(file_db):
    """A cache write must not turn the caller's engine into BEGIN-IMMEDIATE mode,
    which would make ordinary read-only sessions take write reservations."""
    engine, Factory = file_db
    s = Factory()
    write_analysis_cache_rows(s, [_passive_row()])
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
    minimal = {**_passive_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    complete = {
        **_passive_row(played_eval=20),
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
    valid_minimal = {**_passive_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    # resolver-complete claim but missing the multi-move PV -> contract invalid.
    invalid_richer = {
        **_passive_row(played_eval=20),
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
    """A valid passive row + an identical row carrying contradictory identity
    metadata stores the valid one (not DUPLICATE_CONFLICT) in both orderings."""
    valid = {**_passive_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    # Same key/producer, but claims an all-None-identity profile while carrying
    # bogus engine metadata -> identity does not verify -> invalid.
    identity_bad = {
        **_passive_row(played_eval=20),
        "evidence_contract_id": MINIMAL_PLAYED_EVAL,
        "engine_build": "bogus-build-for-all-none-identity-profile",
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


def _full_evidence_profile_row(profile_id):
    """A valid, fully-stamped, contract-satisfying row under ``profile_id``.

    Satisfies resolver-complete-v2 (all required evidence fields present) and
    identity-verifies against the registry, so the ONLY reason the writer could
    refuse it is the profile's active/retired state.
    """
    from app.analysis_profiles import stamp_profile_full
    from app.evidence_contracts import RESOLVER_COMPLETE_V2

    row = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "analysis",
        "analysis_profile_id": profile_id,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 30,
        "best_eval": 30,
        "eval_delta": 0,
        "classification": "best",
    }
    row.update(stamp_profile_full(profile_id))
    return row


def test_canonical_move_grain_write_relocates_a_browser_v2_row(file_db):
    """g-6xc3 against the REAL writer, not just the pure policy.

    The point of the cross-grain rule is that the position half is RELOCATED to
    ``position_analysis``, so this asserts the stored row afterwards is a move-grain
    row and nothing else: the replace path must NULL the position columns rather than
    leave a v2 row's stale best-move facts sitting under a ``move-complete-v1``
    contract id, which would read as canonical position truth it never wrote.
    """
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import BROWSER_ANALYSIS_MULTIPV_PROFILE_ID, stamp_profile_full
    from app.evidence_contracts import MOVE_COMPLETE

    _, Factory = file_db
    _seed(Factory, _full_evidence_profile_row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID))

    canonical_move = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": MOVE_COMPLETE,
        "played_eval": 12,
        "classification": "good",
        **stamp_profile_full(CANONICAL_PROFILE_ID),
    }
    s = Factory()
    results = write_analysis_cache_rows(s, [canonical_move])
    s.close()
    assert all(r is Reason.CROSS_GRAIN_AUTHORITY_REPLACE for _, r in results)

    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.analysis_profile_id == CANONICAL_PROFILE_ID
    assert row.evidence_contract_id == MOVE_COMPLETE
    assert (row.played_eval, row.classification) == (12, "good")
    # The relocated grain, gone from this table.
    assert row.best_move_uci is None
    assert row.best_line_uci is None
    assert row.best_eval is None
    s2.close()


def test_same_profile_canonical_v2_transitions_after_position_winner_is_durable(
    file_db,
):
    """The real writer performs the narrow Rule 2 grain transition in place.

    The canonical producer's required order is represented explicitly: commit the
    native position winner first, then replace the legacy combined cache row with
    its agreeing move-grain row.  The cache replacement must not touch position
    truth, change the cache key/id, or strand duplicated position columns.
    """
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import stamp_profile_full
    from app.evidence_contracts import MOVE_COMPLETE, POSITION_COMPLETE
    from app.fen import normalize_fen
    from app.models import PositionAnalysisRow
    from app.position_analysis_repo import write_position_analysis_row

    _, Factory = file_db
    profile_stamp = stamp_profile_full(CANONICAL_PROFILE_ID)
    normalized_fen = normalize_fen(FEN)
    position = {
        "normalized_fen": normalized_fen,
        "fen": FEN,
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": POSITION_COMPLETE,
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "best_eval": 30,
        "best_eval_mate": None,
        **profile_stamp,
    }
    canonical_v2 = _full_evidence_profile_row(CANONICAL_PROFILE_ID)

    seed = Factory()
    write_position_analysis_row(seed, position)
    seed.add(AnalysisCache(**canonical_v2))
    seed.commit()  # position-first durability is the transition's precondition
    stored_before = seed.query(AnalysisCache).one()
    cache_id = stored_before.id
    position_before = seed.query(PositionAnalysisRow).one()
    position_id = position_before.id
    position_truth = (
        position_before.best_move_uci,
        position_before.best_move_san,
        position_before.best_line_uci,
        position_before.best_eval,
        position_before.best_eval_mate,
        position_before.analysis_profile_id,
        position_before.evidence_contract_id,
        position_before.source_cache_id,
    )
    seed.close()

    canonical_move = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": MOVE_COMPLETE,
        "played_eval": 30,
        "classification": "best",
        # A move-row snapshot need not equal the legacy combined row's delta.
        "eval_delta": 7,
        **profile_stamp,
    }
    writer = Factory()
    results = write_analysis_cache_rows(writer, [canonical_move])
    writer.close()
    assert len(results) == 1
    assert all(
        reason is Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE
        for _, reason in results
    )

    verify = Factory()
    assert verify.query(AnalysisCache).count() == 1
    stored = verify.query(AnalysisCache).one()
    assert stored.id == cache_id
    assert stored.evidence_contract_id == MOVE_COMPLETE
    assert (stored.played_eval, stored.classification, stored.eval_delta) == (
        30,
        "best",
        7,
    )
    assert (
        stored.best_move_uci,
        stored.best_move_san,
        stored.best_line_uci,
        stored.best_eval,
        stored.best_eval_mate,
    ) == (None, None, None, None, None)

    assert verify.query(PositionAnalysisRow).count() == 1
    position_after = verify.query(PositionAnalysisRow).one()
    assert position_after.id == position_id
    assert (
        position_after.best_move_uci,
        position_after.best_move_san,
        position_after.best_line_uci,
        position_after.best_eval,
        position_after.best_eval_mate,
        position_after.analysis_profile_id,
        position_after.evidence_contract_id,
        position_after.source_cache_id,
    ) == position_truth
    verify.close()


def test_same_profile_move_contract_with_position_fields_cannot_replace_v2(file_db):
    """The real writer keeps v2 when an incoming move row is not grain-clean."""
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import stamp_profile_full
    from app.evidence_contracts import MOVE_COMPLETE, RESOLVER_COMPLETE_V2

    _, Factory = file_db
    _seed(Factory, _full_evidence_profile_row(CANONICAL_PROFILE_ID))

    incoming = {
        "fen_before": FEN,
        "move_uci": "e2e4",
        "move_san": "e4",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": MOVE_COMPLETE,
        "played_eval": 30,
        "classification": "best",
        # Contract satisfaction does not reject this out-of-grain extra field.
        "best_move_uci": "e2e4",
        **stamp_profile_full(CANONICAL_PROFILE_ID),
    }
    writer = Factory()
    results = write_analysis_cache_rows(writer, [incoming])
    writer.close()

    assert len(results) == 1
    assert results[0][1] is Reason.SAME_PROFILE_IDEMPOTENT

    verify = Factory()
    stored = verify.query(AnalysisCache).one()
    assert stored.evidence_contract_id == RESOLVER_COMPLETE_V2
    assert stored.best_move_uci == "e2e4"
    assert stored.best_line_uci == "e2e4 e7e5"
    verify.close()


def test_replace_does_not_inherit_the_replaced_rows_mate_count(file_db):
    """A REPLACE stores the incoming row's evidence, never a union with the old row's.

    The Rule 5 mate strip lets a CP-only canonical row replace a weaker row that
    merely stored a raw mate count. If the writer left absent columns alone, that
    mate count would survive under the canonical stamp — a mate claim canonical never
    made, on a row every consumer reads as canonical truth.
    """
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import BROWSER_ANALYSIS_MULTIPV_PROFILE_ID

    _, Factory = file_db
    browser = _full_evidence_profile_row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    browser.update(played_eval_mate=3, best_eval_mate=2)
    _seed(Factory, browser)

    canonical_cp_only = _full_evidence_profile_row(CANONICAL_PROFILE_ID)
    assert "played_eval_mate" not in canonical_cp_only  # precondition: absent, not None
    s = Factory()
    results = write_analysis_cache_rows(s, [canonical_cp_only])
    s.close()
    assert all(r is Reason.DOMINATES_REPLACE for _, r in results)

    s2 = Factory()
    row = s2.query(AnalysisCache).one()
    assert row.analysis_profile_id == CANONICAL_PROFILE_ID
    assert row.played_eval_mate is None
    assert row.best_eval_mate is None
    s2.close()


def test_retired_profile_row_never_inserted_as_new_key(file_db):
    """A correctly-stamped RETIRED-profile row is refused at the insert path.

    Regression for g-reuse-d21-search P1: the batch writer partitioned validity
    with ``incoming_is_valid`` (which a retired row PASSES) and bulk-inserted
    missing keys before the replacement decision ever ran, so a retired
    browser-analysis-v1 row landed as a phantom NEW_KEY. The inactive gate must
    run on the insert path against the real writer, not only in the pure policy.
    """
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import BROWSER_ANALYSIS_PROFILE_ID, get_profile

    assert not get_profile(BROWSER_ANALYSIS_PROFILE_ID).active  # precondition
    _, Factory = file_db
    row = _full_evidence_profile_row(BROWSER_ANALYSIS_PROFILE_ID)
    s = Factory()
    results = write_analysis_cache_rows(s, [row])
    s.close()
    assert all(r is Reason.INACTIVE_PROFILE_KEEP for _, r in results)
    s2 = Factory()
    assert s2.query(AnalysisCache).count() == 0  # nothing stored
    s2.close()


def test_active_successor_profile_row_inserts(file_db):
    """Control: the ACTIVE successor is not caught by the retirement gate."""
    from app.analysis_cache_policy import Reason
    from app.analysis_profiles import (
        BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        get_profile,
    )

    assert get_profile(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID).active  # precondition
    _, Factory = file_db
    row = _full_evidence_profile_row(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID)
    s = Factory()
    results = write_analysis_cache_rows(s, [row])
    s.close()
    assert all(r is Reason.NEW_KEY for _, r in results)
    s2 = Factory()
    stored = s2.query(AnalysisCache).one()
    assert stored.analysis_profile_id == BROWSER_ANALYSIS_MULTIPV_PROFILE_ID
    s2.close()


def test_dedupe_rejects_differing_source(file_db):
    """Equal evidence but differing provenance is ambiguous -> reject the key."""
    from app.analysis_cache_policy import Reason
    a = {**_passive_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL}
    b = {**_passive_row(played_eval=20), "evidence_contract_id": MINIMAL_PLAYED_EVAL,
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
        a = {**_passive_row(played_eval=20), "best_eval": 5,
             "evidence_contract_id": MINIMAL_PLAYED_EVAL}
        b = {**_passive_row(played_eval=20), "eval_delta": 0,
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
from app.analysis_profiles import JEFFML_PROFILE_ID
from app.evidence_contracts import MINIMAL_PLAYED_EVAL

engine = create_engine("sqlite:///{db}")
F = sessionmaker(bind=engine)
row = {{
    "fen_before": "{fen}", "move_uci": "e2e4", "move_san": "e4",
    "played_eval": int(sys.argv[1]), "source": "game",
    "analysis_profile_id": JEFFML_PROFILE_ID,
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
        write_analysis_cache_rows(s, [_passive_row()])
    s.close()


def _pg_url():
    return os.environ.get("GHOSTREPLAY_TEST_PG_URL") or os.environ.get("TEST_DATABASE_URL_PG")


pg_required = pytest.mark.skipif(
    not _pg_url(), reason="set GHOSTREPLAY_TEST_PG_URL to run PostgreSQL locking tests"
)


@pytest.fixture
def pg_db():
    url = _normalize_postgres_scheme(_pg_url())
    # Pin READ COMMITTED explicitly (it is the Postgres default, so this is a
    # no-op for the existing tests) so the coordinated multi-connection TOCTOU
    # tests below have a load-bearing, non-drifting isolation contract: the
    # writer's post-barrier FOR UPDATE must observe the helper's *committed*
    # delete/recreate, which REPEATABLE READ / SERIALIZABLE would not permit.
    # The engine binds both the writer's internal sessions and the helper
    # connections, so both are pinned; the seams additionally assert
    # ``SHOW transaction_isolation`` so a pool/env default change fails loudly.
    engine = create_engine(url, isolation_level="READ COMMITTED")

    # Neutralize the analysis_cache -> evidence_epoch bump trigger for this
    # dedicated test engine. On the alembic-migrated schema (what CI runs, and
    # what a shared migrated DB leaves behind for this fixture's checkfirst
    # create) every INSERT/DELETE/UPDATE on analysis_cache fires an AFTER
    # STATEMENT trigger that does `UPDATE evidence_epoch SET value=value+1 WHERE
    # id=1`, taking a row lock on the singleton epoch row held until commit. That
    # turns the epoch row into a global serializer for ALL analysis_cache writes:
    # the writer parked at the TOCTOU lock seam (mid-transaction) holds it, so the
    # helper's *committed* concurrent delete/recreate deadlocks against it and the
    # coordinated seams time out. `session_replication_role = replica` disables
    # user triggers per-connection (the epoch bump is orthogonal to the
    # analysis_cache ON CONFLICT / FOR UPDATE row semantics these tests assert),
    # so the writer and helper no longer contend on the epoch row. It is scoped to
    # this engine and vanishes on dispose(); it is a no-op on the model-`create()`
    # schema, which has no such trigger.
    @event.listens_for(engine, "connect")
    def _disable_user_triggers(dbapi_conn, _record):  # pragma: no cover - thin
        cur = dbapi_conn.cursor()
        cur.execute("SET session_replication_role = replica")
        cur.close()

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
def test_pg_insert_then_passive_keeps_canonical(pg_db):
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

    # A passive row for the same key must be kept out (non-authoritative).
    s = Factory()
    write_analysis_cache_rows(s, [_passive_row(played_eval=999)])
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
            write_analysis_cache_rows(s, [_passive_row(played_eval=val)])
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
@pytest.mark.parametrize("n", [10, 40])
def test_pg_statement_count_overwrite_no_per_row_roundtrip(pg_db, n):
    """PostgreSQL overwrite maintenance stays set-based across batch sizes."""
    engine, Factory = pg_db
    _assert_overwrite_set_based(engine, Factory, n)  # engine is fixture-owned


def _run_concurrent_batches(Factory, batches, monkeypatch, *, timeout=30.0):
    """Run one worker per batch, all forced INSIDE the INSERT ... ON CONFLICT at the
    same instant via a barrier seamed immediately before the insert execute.

    A barrier at the worker *entry* (before ``write_analysis_cache_rows``) let a
    fast local PG fully serialize one worker's whole write before another thread
    reached the insert path, so real overlap was never guaranteed. Seaming the
    barrier into ``_insert_missing`` instead makes every worker block just before
    its INSERT and release together, so they are genuinely concurrent inside the
    ``INSERT ... ON CONFLICT``. This asserts the correct FINAL STATE under forced
    overlap; it is NOT a proof of deadlock-freedom — key ordering only lowers
    deadlock *probability*, and any transient 40P01/40001 that still occurs is
    retried by ``_run_batch_with_retry`` (which is left ENABLED here). Joins are
    bounded so a genuine hang surfaces as a failed assert rather than blocking CI.
    """
    import app.analysis_cache_repo as repo

    errors = []
    barrier = threading.Barrier(len(batches))
    real_insert = repo._insert_missing
    _local = threading.local()

    def barriered_insert(session, rows, *, insert):
        # Gate only the FIRST insert per worker (the deleter-free batches here run
        # exactly one _insert_missing, but a recovery insert must never re-wait a
        # barrier already sized to one wait per worker).
        if not getattr(_local, "passed", False):
            _local.passed = True
            barrier.wait(timeout=timeout)  # all workers enter the INSERT together
        return real_insert(session, rows, insert=insert)

    monkeypatch.setattr(repo, "_insert_missing", barriered_insert)

    def worker(batch):
        try:
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
def test_pg_concurrent_overlapping_batches_correct_final_state(pg_db, monkeypatch):
    """Contention/correctness smoke test: several concurrent overlapping multi-key
    batches, forced together inside the INSERT ... ON CONFLICT, converge to the
    correct FINAL STATE — one row per key, no *unretried* exception.

    The retry is left ENABLED (default ``_PG_MAX_RETRIES``): under g-dckw's single
    contract a deadlock is a retryable transient failure absorbed by
    ``_run_batch_with_retry``, and key ordering is only a deadlock-*probability*
    heuristic, not a proof of deadlock-freedom. So this asserts the observable
    final state under forced overlap, NOT that zero deadlocks occurred."""
    _, Factory = pg_db
    keys = [f"cpos-{i}" for i in range(20)]
    batches = [
        [{**_passive_row(played_eval=val), "fen_before": k} for k in keys]
        for val in (10, 20, 30, 40)
    ]

    errors = _run_concurrent_batches(Factory, batches, monkeypatch)

    assert not errors  # any 40P01/40001 is retried; only an UNretried error lands here
    s2 = Factory()
    stored = s2.query(AnalysisCache).all()
    assert {r.fen_before for r in stored} == set(keys)  # every key present, exactly one
    assert len(stored) == len(keys)
    s2.close()


@pg_required
def test_pg_concurrent_mixed_signature_batches_correct_final_state(pg_db, monkeypatch):
    """Contention/correctness smoke test with DIFFERENT per-key present-column
    signatures per thread (so the insert pass splits into multiple statements laid
    out differently per thread), forced together inside the INSERT ... ON CONFLICT.

    Insertion still proceeds in global key order, which lowers deadlock probability
    but does not guarantee deadlock-freedom; the retry is left ENABLED so any
    transient 40P01/40001 is absorbed by ``_run_batch_with_retry``. Asserts the
    correct final state (one row per key), NOT that no deadlock occurred."""
    _, Factory = pg_db
    keys = [f"mpos-{i}" for i in range(20)]

    def batch(swap):
        out = []
        for i, k in enumerate(keys):
            row = {**_passive_row(played_eval=20), "fen_before": k}
            if (i % 2 == 0) ^ swap:
                row["best_eval"] = 5  # extra column -> different insert signature
            out.append(row)
        return out

    batches = [batch(sw) for sw in (False, True, False, True)]

    errors = _run_concurrent_batches(Factory, batches, monkeypatch)

    assert not errors  # any 40P01/40001 is retried; only an UNretried error lands here
    s2 = Factory()
    stored = s2.query(AnalysisCache).all()
    assert {r.fen_before for r in stored} == set(keys)  # every key present, exactly one
    assert len(stored) == len(keys)
    s2.close()


# ---------------------------------------------------------------------------
# g-dckw.1: coordinated two-connection TOCTOU / rollback tests on real Postgres.
#
# These drive the vanish/recreate races in _run_batch against GENUINE PG
# transaction visibility — a real FOR UPDATE, a genuinely separate committed
# deleter/recreator, the real ON CONFLICT DO NOTHING, and the real transient-error
# rollback/retry path — coverage the SQLite control-flow tests above (which
# monkeypatch _lock_existing to FAKE the vanish inline and return a fabricated {})
# cannot give. Every seam is TIMING ONLY: it controls WHEN the writer's real DB
# call runs, while the delete/recreate is a real committed transaction on a
# separate connection observed under READ COMMITTED. All waits are bounded so a
# genuine hang fails the test instead of blocking CI. Isolation is pinned to READ
# COMMITTED (see the pg_db fixture) and each seam asserts it, because the writer's
# post-barrier FOR UPDATE must observe the helper's committed writes.
# ---------------------------------------------------------------------------

_TOCTOU_WAIT = 15.0  # bounded wait so a genuine lock/hang fails loudly, never hangs CI


class _Writer:
    """Run write_analysis_cache_rows on its own daemon thread; capture result/error.

    The helper opens its OWN engine-bound session internally, so this only needs a
    clean caller session. The bounded join turns a hang into a failed assert.
    """

    def __init__(self, Factory, batch):
        self._Factory = Factory
        self._batch = batch
        self.result = None
        self.error = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        s = self._Factory()
        try:
            self.result = write_analysis_cache_rows(s, self._batch)
        except Exception as e:  # noqa: BLE001
            self.error = e
        finally:
            s.close()

    def start(self):
        self._thread.start()
        return self

    def join(self):
        self._thread.join(timeout=_TOCTOU_WAIT)
        assert not self._thread.is_alive(), "writer thread hung (possible lock/deadlock)"


def _assert_read_committed(session):
    """The writer's post-barrier FOR UPDATE only observes the helper's committed
    delete/recreate under READ COMMITTED; fail loudly if a pool/env default drifts."""
    iso = session.execute(text("SHOW transaction_isolation")).scalar()
    assert iso == "read committed", f"writer isolation drifted to {iso!r}"


def _delete_committed(Factory, fen, uci="e2e4"):
    """Delete one key on a genuinely separate committed connection (helper H)."""
    h = Factory()
    try:
        h.query(AnalysisCache).filter(
            AnalysisCache.fen_before == fen, AnalysisCache.move_uci == uci
        ).delete()
        h.commit()
    finally:
        h.close()


@pg_required
def test_pg_toctou_delete_then_recovery_insert_new_key(pg_db, monkeypatch):
    """g-dckw.1 scenario (a): conflict insert -> a concurrent COMMITTED delete on a
    separate connection -> the writer's real FOR UPDATE sees the row gone -> the
    recovery ON CONFLICT DO NOTHING insert (no competitor) lands the incoming row
    -> NEW_KEY, in a single lock pass, on genuine PG visibility."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    _, Factory = pg_db
    K = "toctou-a"
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": K})  # prior committed row

    real_lock = repo._lock_existing
    writer_at_lock = threading.Event()
    helper_committed = threading.Event()
    calls = {"lock": 0, "barrier": 0}

    def seamed_lock(session, conflicted, *, for_update):
        calls["lock"] += 1
        if calls["lock"] == 1:  # timing-only barrier on the FIRST lock pass
            calls["barrier"] += 1
            _assert_read_committed(session)
            writer_at_lock.set()
            assert helper_committed.wait(timeout=_TOCTOU_WAIT), "helper did not commit in time"
        return real_lock(session, conflicted, for_update=for_update)

    monkeypatch.setattr(repo, "_lock_existing", seamed_lock)

    # W's incoming value (777) differs from the seed (20), so a stored 777 proves
    # the recovery insert wrote W's incoming row rather than a leftover of the seed.
    writer = _Writer(Factory, [{**_passive_row(played_eval=777), "fen_before": K}]).start()

    assert writer_at_lock.wait(timeout=_TOCTOU_WAIT), "writer never reached the lock seam"
    _delete_committed(Factory, K)   # H: real committed delete, now visible to W
    helper_committed.set()
    writer.join()

    assert writer.error is None, writer.error
    assert calls["lock"] == 1      # recovery inserted with no competitor: one lock pass
    assert calls["barrier"] == 1   # the timing barrier fired exactly once
    assert writer.result == [((K, "e2e4"), Reason.NEW_KEY)]
    s2 = Factory()
    row = s2.query(AnalysisCache).filter(AnalysisCache.fen_before == K).one()  # exactly one
    assert row.played_eval == 777  # W's incoming values, not the deleted seed's
    s2.close()


@pg_required
def test_pg_toctou_recreate_before_recovery_keeps_competitor(pg_db, monkeypatch):
    """g-dckw.1 scenario (b): the writer observes the key vanish (committed delete on
    H), THEN a competitor RE-CREATES it as the canonical row (a second committed
    phase on H) before the recovery insert. The recovery ON CONFLICT DO NOTHING
    loses, so the key is re-locked and re-decided against H's live canonical row ->
    a deterministic NON_AUTHORITATIVE_KEEP, with H's row never clobbered. This is the
    real-PG analogue of test_vanished_row_reresolved_against_concurrent_recreate and
    the only schedule that actually drives the recovery _insert_missing at :509 on
    genuine PG visibility (a bare session.add would instead raise IntegrityError and
    abort the whole batch)."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    _, Factory = pg_db
    K = "toctou-b"
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": K})  # prior committed row

    real_lock = repo._lock_existing
    real_insert = repo._insert_missing
    writer_at_lock = threading.Event()
    helper_deleted = threading.Event()
    writer_at_recovery = threading.Event()
    helper_recreated = threading.Event()
    calls = {"lock": 0, "insert": 0, "barrier_lock": 0, "barrier_recovery": 0}

    def seamed_lock(session, conflicted, *, for_update):
        calls["lock"] += 1
        if calls["lock"] == 1:  # seam 1: timing barrier on the FIRST lock pass
            calls["barrier_lock"] += 1
            _assert_read_committed(session)
            writer_at_lock.set()
            assert helper_deleted.wait(timeout=_TOCTOU_WAIT), "helper did not delete in time"
        return real_lock(session, conflicted, for_update=for_update)

    def seamed_insert(session, rows, *, insert):
        calls["insert"] += 1
        if calls["insert"] == 2:  # seam 2: timing barrier on the recovery insert (2nd call)
            calls["barrier_recovery"] += 1
            writer_at_recovery.set()
            assert helper_recreated.wait(timeout=_TOCTOU_WAIT), "helper did not recreate in time"
        return real_insert(session, rows, insert=insert)

    monkeypatch.setattr(repo, "_lock_existing", seamed_lock)
    monkeypatch.setattr(repo, "_insert_missing", seamed_insert)

    # W's incoming is NON-authoritative (passive); re-decided against H's
    # authoritative recreated row this is a deterministic NON_AUTHORITATIVE_KEEP.
    writer = _Writer(Factory, [{**_passive_row(played_eval=999), "fen_before": K}]).start()

    # Phase 1: writer must observe absence -> H commits the DELETE alone.
    assert writer_at_lock.wait(timeout=_TOCTOU_WAIT), "writer never reached the lock seam"
    _delete_committed(Factory, K)
    helper_deleted.set()

    # Phase 2: competitor RE-CREATES K as canonical immediately before the recovery
    # insert, so the recovery ON CONFLICT DO NOTHING loses.
    assert writer_at_recovery.wait(timeout=_TOCTOU_WAIT), "writer never reached the recovery seam"
    _seed(Factory, _canonical_full(K))
    helper_recreated.set()

    writer.join()

    assert writer.error is None, writer.error
    assert calls["barrier_lock"] == 1      # seam 1 fired exactly once
    assert calls["barrier_recovery"] == 1  # seam 2 fired exactly once
    assert calls["lock"] == 2              # first lock (vanished) + re-lock (decide)
    assert calls["insert"] == 2            # initial insert + recovery insert
    assert writer.result == [((K, "e2e4"), Reason.NON_AUTHORITATIVE_KEEP)]
    s2 = Factory()
    row = s2.query(AnalysisCache).filter(AnalysisCache.fen_before == K).one()  # exactly one
    assert row.analysis_profile_id == CANONICAL_PROFILE_ID  # H's canonical row kept
    assert row.played_eval == 10                            # never clobbered by W's 999
    s2.close()


@pg_required
@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
def test_pg_transient_error_after_partial_write_rolls_back_then_retries(
    pg_db, monkeypatch, sqlstate
):
    """g-dckw.1 scenario (c): attempt 1 does REAL work (a genuinely new key is
    inserted, uncommitted) and THEN a transient 40P01/40001 is injected before the
    commit. _run_batch_with_retry must roll the partial row back and re-run the
    whole batch on a fresh PG transaction, landing correctly. Proves rollback-
    AFTER-partial-work, not merely error classification: exactly one row for the new
    key survives (attempt 2's, no leaked attempt-1 duplicate). Parametrized over
    both classified SQLSTATEs (deadlock 40P01, serialization 40001)."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(repo.time, "sleep", lambda *a, **k: None)  # no backoff delay

    _, Factory = pg_db
    K = "txn-c-existing"  # pre-existing -> conflicts on insert
    N = "txn-c-new"       # genuinely new -> attempt 1 really INSERTs it (uncommitted)
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": K})

    real_lock = repo._lock_existing
    calls = {"lock": 0}
    own = {"count": None}
    probe = {"count": None}

    def seamed_lock(session, conflicted, *, for_update):
        calls["lock"] += 1
        if calls["lock"] == 1:
            # The pipeline already ran the REAL _insert_missing([K, N]) before this
            # lock, so N is inserted but UNCOMMITTED inside attempt 1's txn. The
            # writer's OWN session must see its uncommitted N (genuine partial work
            # to roll back), while a SELECT on a SEPARATE committed connection must
            # NOT (rules out an unexpected autocommit). Only THEN inject the
            # transient error, so the rollback has real partial work to undo.
            own["count"] = (
                session.query(AnalysisCache).filter(AnalysisCache.fen_before == N).count()
            )
            p = Factory()
            try:
                probe["count"] = (
                    p.query(AnalysisCache).filter(AnalysisCache.fen_before == N).count()
                )
            finally:
                p.close()
            orig = Exception("deadlock detected")
            orig.pgcode = sqlstate
            orig.sqlstate = sqlstate
            raise OperationalError("deadlock detected", {}, orig)
        return real_lock(session, conflicted, for_update=for_update)

    monkeypatch.setattr(repo, "_lock_existing", seamed_lock)

    s = Factory()
    results = write_analysis_cache_rows(
        s,
        [
            {**_passive_row(played_eval=20), "fen_before": K},
            {**_passive_row(played_eval=20), "fen_before": N},
        ],
    )
    s.close()

    # (a) exactly one retry: the seam fired on attempt 1 (raise) + attempt 2 (delegate).
    assert calls["lock"] == 2
    # attempt 1 GENUINELY inserted N (real partial work): its own txn saw the row,
    # but a separate committed connection did not -> uncommitted, not autocommitted.
    assert own["count"] == 1
    assert probe["count"] == 0
    # (b) the rollback wiped attempt 1's partial write: exactly one N row (attempt 2's,
    # no leaked attempt-1 duplicate) and the expected K row survive.
    s2 = Factory()
    assert s2.query(AnalysisCache).filter(AnalysisCache.fen_before == N).count() == 1
    assert s2.query(AnalysisCache).filter(AnalysisCache.fen_before == K).count() == 1
    s2.close()
    # (c) the final verdicts equal the no-error result exactly (key-sorted order).
    assert results == [
        ((K, "e2e4"), Reason.SAME_PROFILE_IDEMPOTENT),
        ((N, "e2e4"), Reason.NEW_KEY),
    ]


def test_clean_session_precondition(file_db):
    _, Factory = file_db
    s = Factory()
    s.execute(__import__("sqlalchemy").text("SELECT 1"))  # opens a transaction
    with pytest.raises(RuntimeError):
        write_analysis_cache_rows(s, [_passive_row()])
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
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": "k2"})   # idempotent
    _seed(Factory, _canonical_full("k3"))                                  # non-auth keep
    _seed(Factory, {"fen_before": "k4", "move_uci": "e2e4", "move_san": "e4",
                    "played_eval": 5, "source": "jeffml-scores"})          # legacy -> replace
    _seed(Factory, _canonical_full("k5"))                                  # superset merge
    _seed(Factory, _canonical_full("k6", played_eval=10))                  # merge conflict keep

    batch = [
        {**_passive_row(played_eval=20), "fen_before": "k1"},                       # NEW_KEY
        {**_passive_row(played_eval=20), "fen_before": "k2"},                       # SAME_PROFILE_IDEMPOTENT
        {**_passive_row(played_eval=999), "fen_before": "k3"},                      # NON_AUTHORITATIVE_KEEP
        _canonical_full("k4"),                                                      # LEGACY_REPLACED_BY_AUTH
        _canonical_full("k5", best_eval_mate=3),                                    # SAME_PROFILE_SUPERSET_MERGE
        _canonical_full("k6", played_eval=20),                                      # MERGE_CONFLICT_KEEP
        _canonical_full("k7", engine_build="WRONG-not-canonical"),                  # INVALID_INCOMING_KEEP
        {**_passive_row(played_eval=20), "fen_before": "k8"},                       # DUPLICATE_CONFLICT (a)
        {**_passive_row(played_eval=50), "fen_before": "k8"},                       # DUPLICATE_CONFLICT (b)
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
    assert by_fen["k1"].analysis_profile_id == JEFFML_PROFILE_ID
    assert by_fen["k2"].played_eval == 20                        # unchanged
    assert by_fen["k2"].analysis_profile_id == JEFFML_PROFILE_ID
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
    rows = _distinct_passive_rows(37)  # forces multiple insert chunks

    with _statement_counter(engine) as counts:
        s = F()
        results = write_analysis_cache_rows(s, rows)
        s.close()  # all fresh
    assert all(r is Reason.NEW_KEY for _, r in results)
    assert counts["INSERT"] > 1, counts  # chunked, not one oversized INSERT
    s2 = F()
    assert s2.query(AnalysisCache).count() == 37
    s2.close()

    # Re-upload: every key conflicts -> the conflict SELECT is chunked too.
    with _statement_counter(engine) as counts2:
        s = F()
        res2 = write_analysis_cache_rows(s, [dict(r) for r in rows])
        s.close()
    assert all(r is Reason.SAME_PROFILE_IDEMPOTENT for _, r in res2)
    assert counts2["SELECT"] > 1, counts2  # chunked conflict select
    assert counts2["UPDATE"] == 0, counts2
    s3 = F()
    assert s3.query(AnalysisCache).count() == 37
    s3.close()
    engine.dispose()


def test_vanished_row_recovered_when_no_competitor(file_db, monkeypatch):
    """g-dckw/#2,#3: a key that conflicts on insert but is gone by the lock (the
    concurrent-deleter TOCTOU window) is recovered via ON CONFLICT DO NOTHING and
    — with no competing row present — reported NEW_KEY in a single pass."""
    import app.analysis_cache_repo as repo
    from app.analysis_cache_policy import Reason

    _, Factory = file_db
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": "vanish"})

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
        s, [{**_passive_row(played_eval=20), "fen_before": "vanish"}]
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
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": "vanish"})

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
        s, [{**_passive_row(played_eval=999), "fen_before": "vanish"}]
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
    _seed(Factory, {**_passive_row(played_eval=20), "fen_before": "vanish"})

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
            s, [{**_passive_row(played_eval=999), "fen_before": "vanish"}]
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

    def fake_run_batch(
        session,
        surviving,
        *,
        insert,
        for_update,
        submitter_user_id=None,
        visible_d21_live_by_key=None,
    ):
        calls["n"] += 1
        assert for_update is True
        assert visible_d21_live_by_key is None
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

    def fake_run_batch(
        session,
        surviving,
        *,
        insert,
        for_update,
        submitter_user_id=None,
        visible_d21_live_by_key=None,
    ):
        calls["n"] += 1
        assert visible_d21_live_by_key is None
        orig = Exception()
        orig.pgcode = "23505"
        raise OperationalError("duplicate key value", {}, orig)

    monkeypatch.setattr(repo, "_run_batch", fake_run_batch)

    with pytest.raises(OperationalError):
        repo._run_postgresql(FakeSession, [{"fen_before": "k", "move_uci": "e2e4"}])
    assert calls["n"] == 1  # raised on the first attempt, no retry


@pytest.mark.parametrize(
    "dialect,err_text,pgcode,is_retryable_name,label",
    [
        ("sqlite", "database is locked", None, "_is_busy_error", "locked"),
        ("postgresql", "deadlock detected", "40P01", "_is_retryable_pg_error", "40P01"),
    ],
)
def test_run_batch_with_retry_warns_per_retry_and_on_exhaustion(
    monkeypatch, caplog, dialect, err_text, pgcode, is_retryable_name, label
):
    """g-dckw retry observability: every retry AND the final exhaustion emit a
    structured WARNING carrying the dialect, the classified error (PG SQLSTATE or
    SQLite BUSY/locked), the attempt number + retry bound, and the batch size —
    turning the silent attempt counter into a greppable concurrent-churn signal.
    Runs in CI on the SQLite path; the PG SQLSTATE label is synthetic (classified
    via ``_is_retryable_pg_error``), so no live PG is needed."""
    import app.analysis_cache_repo as repo
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(repo.time, "sleep", lambda *a, **k: None)

    class FakeSession:
        def rollback(self):
            pass

        def close(self):
            pass

    def always_fail(
        session,
        surviving,
        *,
        insert,
        for_update,
        submitter_user_id=None,
        visible_d21_live_by_key=None,
    ):
        assert visible_d21_live_by_key is None
        orig = Exception(err_text)
        if pgcode is not None:
            orig.pgcode = pgcode
        raise OperationalError(err_text, {}, orig)

    monkeypatch.setattr(repo, "_run_batch", always_fail)
    surviving = [{"fen_before": "k", "move_uci": "e2e4"}]

    with caplog.at_level(logging.WARNING, logger="analysis_cache_repo"):
        with pytest.raises(OperationalError):
            repo._run_batch_with_retry(
                FakeSession,
                surviving,
                insert=None,
                for_update=(dialect == "postgresql"),
                is_retryable=getattr(repo, is_retryable_name),
                max_retries=1,  # one scheduled retry, then exhaustion
                dialect=dialect,
            )

    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    scheduled = [m for m in warns if "retry scheduled" in m]
    exhausted = [m for m in warns if "retry exhausted" in m]
    assert len(scheduled) == 1, warns  # fires once per retry
    assert len(exhausted) == 1, warns  # and once on final exhaustion
    for m in (scheduled[0], exhausted[0]):
        assert f"dialect={dialect}" in m
        assert f"error={label}" in m
        assert "batch_size=1" in m
    assert "attempt=1 max_retries=1" in scheduled[0]
    assert "attempt=2 max_retries=1" in exhausted[0]


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


@pg_required
def test_source_default_preserved_on_absent_column_pg(pg_db):
    """PG-gated mirror: a row omitting ``source`` stores the server default on
    PostgreSQL too. Each dialect applies the column default via its own DDL path,
    so the SQLite run does not prove PG — the insert-column omission must reach PG
    intact (never source=None, which would violate NOT NULL / skip the default)."""
    _, Factory = pg_db
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


def _distinct_passive_rows(n):
    # Distinct keys; single (fixed) present-column signature -> one INSERT run.
    return [{**_passive_row(played_eval=20), "fen_before": f"pos-{i}"} for i in range(n)]


def _assert_idempotent_constant(engine, Factory, n):
    """Seed N distinct single-signature rows, then re-upload the identical batch and
    assert the re-upload is a constant 1 INSERT + 2 SELECT + 0 UPDATE (every row
    idempotent-KEEP), independent of N. Shared by the PG and SQLite contracts so
    the two can't silently diverge.

    The second SELECT is the paired association load (g-v21l): these rows are
    NON-authoritative, so the MERGE precondition needs their association sets. It is
    one set-based query over the locked ids, so the property under test — a CONSTANT
    statement count independent of N, with no per-row round-trip — is unchanged."""
    rows = _distinct_passive_rows(n)
    s = Factory()
    write_analysis_cache_rows(s, rows)
    s.close()  # seed fresh

    with _statement_counter(engine) as counts:
        s = Factory()
        write_analysis_cache_rows(s, [dict(r) for r in rows])
        s.close()

    assert counts["INSERT"] == 1, counts
    # one (FOR UPDATE) select over all conflicts + one association load
    assert counts["SELECT"] == 2, counts
    assert counts["UPDATE"] == 0, counts   # every row idempotent-KEEP


def _assert_overwrite_set_based(engine, Factory, n):
    """Seed N legacy rows, then reclaim them in one canonical batch.

    At these batch sizes every operation fits one bind-parameter chunk, so the
    overwrite is one conflict INSERT, one lock SELECT, one association SELECT,
    and one unconditional-on-REPLACE association DELETE. Larger batches scale by
    bind-parameter chunks, not by row. Shared by the PG and SQLite contracts so
    the default suite catches statement-shape drift.
    """
    keys = [f"leg-{i}" for i in range(n)]
    for key in keys:
        _seed(
            Factory,
            {
                "fen_before": key,
                "move_uci": "e2e4",
                "move_san": "e4",
                "played_eval": 5,
                "source": "jeffml-scores",
            },
        )

    with _statement_counter(engine) as counts:
        s = Factory()
        results = write_analysis_cache_rows(
            s, [_canonical_full(key) for key in keys]
        )
        s.close()

    from app.analysis_cache_policy import Reason

    assert all(reason is Reason.LEGACY_REPLACED_BY_AUTH for _, reason in results)
    assert counts["INSERT"] == 1, counts          # one multi-row conflict INSERT
    assert counts["SELECT"] == 2, counts          # lock + association load
    assert counts["DELETE"] == 1, counts          # unconditional on REPLACE
    assert counts["UPDATE"] <= len(keys), counts  # bounded by changed rows


def test_canonical_conflicts_issue_no_association_query():
    """A batch whose conflicts are all CANONICAL keeps its historical statement
    count: canonical merges skip the ownership precondition and canonical rows
    never carry associations, so no association query is issued at all (g-v21l).

    In-memory engine so the counter sees the writer's statements (for a file DB the
    helper writes through its own dedicated BEGIN IMMEDIATE engine)."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    rows = [{**_canonical_full(FEN), "fen_before": f"canon-{i}"} for i in range(5)]
    s = Factory()
    write_analysis_cache_rows(s, rows)
    s.close()  # seed fresh

    with _statement_counter(engine) as counts:
        s = Factory()
        write_analysis_cache_rows(s, [dict(r) for r in rows])
        s.close()

    assert counts["INSERT"] == 1, counts
    assert counts["SELECT"] == 1, counts  # conflict select only — no association load
    engine.dispose()


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


@pytest.mark.parametrize("n", [10, 40])
def test_statement_count_overwrite_set_based_sqlite(n):
    """SQLite mirrors the PostgreSQL overwrite statement-shape contract."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    _assert_overwrite_set_based(engine, Factory, n)
    engine.dispose()  # test-owned engine (not a fixture)
