"""The write-path claim rule and the single-affected-submitter MERGE precondition
(g-v21l).

An ``analysis_cache_submission`` row means exactly: *this user independently
submitted a tuple consistent with this stored row*. The claim rule decides when the
writer may create one; the MERGE precondition decides when a same-profile merge may
fold one submitter's evidence into a row another submitter already reads.

Both live INSIDE the writer's locked transaction. An endpoint-side association
write after ``write_analysis_cache_rows`` would race a concurrent canonical REPLACE
and could leave a user associated with facts they never submitted.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

import app.analysis_cache_repo as repo
from app.analysis_cache_policy import (
    Decision,
    Reason,
    decide_analysis_cache_replacement,
    project_cache_row,
)
from app.analysis_cache_repo import write_analysis_cache_rows
from app.analysis_profiles import (
    BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
    BROWSER_ANALYSIS_PROFILE_ID,
    CANONICAL_PROFILE_ID,
    stamp_profile_full,
)
from app.analysis_submissions import viewer_associated_ids
from app.evidence_contracts import RESOLVER_COMPLETE_V2, MINIMAL_PLAYED_EVAL
from app.evidence_policy import Capability
from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    AnalysisCacheSubmission,
    Base,
    User,
    ensure_evidence_epoch_infrastructure,
)
from app.position_analysis_repo import resolve_trusted_positions

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
NORM = normalize_fen(START)

ALICE = 11
BOB = 22


def test_epoch_helper_never_heals_established_missing_state():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with pytest.raises(RuntimeError, match="incomplete"):
        ensure_evidence_epoch_infrastructure(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM evidence_epoch")
        ).scalar_one() == 0

    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM shared_evidence_scope_invalidations "
                "WHERE kind = 'norm'"
            )
        )

    with pytest.raises(RuntimeError, match="incomplete"):
        ensure_evidence_epoch_infrastructure(engine)
    with engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT count(*) FROM shared_evidence_scope_invalidations "
                "WHERE kind = 'norm'"
            )
        ).scalar_one() == 0
    engine.dispose()


def test_known_new_helper_preserves_epoch_and_replaces_stale_trigger_body():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE evidence_epoch SET value = value + 7 WHERE id = 1")
        )
        connection.execute(
            text("DROP TRIGGER trg_analysis_cache_evidence_epoch_insert")
        )
        connection.execute(
            text(
                "CREATE TRIGGER trg_analysis_cache_evidence_epoch_insert "
                "AFTER INSERT ON analysis_cache BEGIN SELECT 1; END"
            )
        )

    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT value FROM evidence_epoch WHERE id = 1")
        ).scalar_one() == 7
        trigger_sql = connection.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_analysis_cache_evidence_epoch_insert'"
            )
        ).scalar_one()
        assert "shared_evidence_scope_versions" in trigger_sql
    engine.dispose()


def test_known_new_helper_refuses_to_seed_after_shared_rows_exist():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO analysis_cache "
                "(fen_before, move_uci, move_san, played_eval) "
                "VALUES (:fen, 'e2e4', 'e4', 1)"
            ),
            {"fen": START},
        )

    with pytest.raises(RuntimeError, match="after shared evidence exists"):
        ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    engine.dispose()


@pytest.fixture
def db():
    """An isolated in-memory database. The writer reuses the caller bind for
    ``:memory:``, so seeds, writes and assertions all see the same data."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    Factory = sessionmaker(bind=engine)
    s = Factory()
    s.add_all([User(id=ALICE, username="alice"), User(id=BOB, username="bob")])
    s.commit()
    s.close()
    yield engine, Factory
    engine.dispose()


@pytest.fixture
def file_db(tmp_path):
    """A FILE-BACKED database, so the writer takes its DEDICATED engine.

    The ``db`` fixture above is ``:memory:``, where the writer reuses the caller's
    bind and therefore inherits whatever pragmas the caller's engine set. Only a
    file URL exercises ``_sqlite_write_engine``'s own connection — which is the
    connection the association INSERT actually executes on, and the one that has to
    enforce the foreign keys itself.
    """
    url = f"sqlite:///{tmp_path/'claims.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    ensure_evidence_epoch_infrastructure(engine, assume_new_schema=True)
    Factory = sessionmaker(bind=engine)
    s = Factory()
    s.add_all([User(id=ALICE, username="alice"), User(id=BOB, username="bob")])
    s.commit()
    s.close()
    yield engine, Factory
    written = repo._sqlite_write_engines.pop(url, None)
    if written is not None:
        written.dispose()
    engine.dispose()


def _browser_row(**over) -> dict:
    row = {
        "fen_before": START,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 20,
        "best_eval": 20,
        "eval_delta": 0,
        "classification": "best",
        "source": "analysis",
        "analysis_profile_id": BROWSER_ANALYSIS_MULTIPV_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **stamp_profile_full(BROWSER_ANALYSIS_MULTIPV_PROFILE_ID),
    }
    row.update(over)
    return row


def _canonical_row(**over) -> dict:
    row = {
        "fen_before": START,
        "move_uci": "e2e4",
        "move_san": "e4",
        "best_move_uci": "e2e4",
        "best_move_san": "e4",
        "best_line_uci": "e2e4 e7e5",
        "played_eval": 20,
        "best_eval": 20,
        "eval_delta": 0,
        "classification": "best",
        "source": "precomputed",
        "analysis_profile_id": CANONICAL_PROFILE_ID,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **stamp_profile_full(CANONICAL_PROFILE_ID),
    }
    row.update(over)
    return row


def _write(Factory, rows, submitter=None):
    s = Factory()
    try:
        return write_analysis_cache_rows(s, rows, submitter_user_id=submitter)
    finally:
        s.close()


def _associations(Factory) -> set[tuple[int, int]]:
    s = Factory()
    try:
        return {
            (r.analysis_cache_id, r.user_id)
            for r in s.query(AnalysisCacheSubmission).all()
        }
    finally:
        s.close()


def _stored(Factory) -> AnalysisCache:
    s = Factory()
    try:
        return s.query(AnalysisCache).one()
    finally:
        s.close()


def _reasons(results) -> list[Reason]:
    return [r for _, r in results]


# --------------------------------------------------------------------------- #
# the pre-migration path: an existing row with no associations becomes readable
# --------------------------------------------------------------------------- #
def test_pre_existing_browser_row_becomes_readable_via_the_idempotent_branch(db):
    """browser-analysis-multipv-v2 is a FIXED profile with no same-profile REPLACE
    path, so a row stored before g-v21l could never acquire an owner COLUMN. The
    claim rule unblocks it with no migration: the original submitter resubmits the
    identical tuple, the decision is SAME_PROFILE_IDEMPOTENT, NO evidence column
    changes, and an association appears."""
    engine, Factory = db
    # Pre-bead state: stored by a writer that passed no submitter.
    assert _reasons(_write(Factory, [_browser_row()])) == [Reason.NEW_KEY]
    assert _associations(Factory) == set()

    before = _stored(Factory)
    snapshot = {f: getattr(before, f) for f in repo._EVIDENCE_FIELDS}
    row_id = before.id

    s = Factory()
    assert resolve_trusted_positions(
        s, [NORM], Capability.POSITION_READ, ALICE
    )[NORM] is None
    s.close()

    # The original submitter resubmits the identical tuple.
    assert _reasons(_write(Factory, [_browser_row()], submitter=ALICE)) == [
        Reason.SAME_PROFILE_IDEMPOTENT
    ]

    after = _stored(Factory)
    assert {f: getattr(after, f) for f in repo._EVIDENCE_FIELDS} == snapshot
    assert _associations(Factory) == {(row_id, ALICE)}

    s = Factory()
    assert resolve_trusted_positions(
        s, [NORM], Capability.POSITION_READ, ALICE
    )[NORM] is not None
    s.close()


def test_two_independent_submitters_both_associate(db):
    """A shared opening position: A and B independently produce the same tuple.
    Neither denies the other — which a single owner column could not express."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id
    assert _associations(Factory) == {(row_id, ALICE)}

    assert _reasons(_write(Factory, [_browser_row()], submitter=BOB)) == [
        Reason.SAME_PROFILE_IDEMPOTENT
    ]
    assert _associations(Factory) == {(row_id, ALICE), (row_id, BOB)}

    s = Factory()
    for user in (ALICE, BOB):
        assert resolve_trusted_positions(
            s, [NORM], Capability.POSITION_READ, user
        )[NORM] is not None
    s.close()


# --------------------------------------------------------------------------- #
# the coverage condition
# --------------------------------------------------------------------------- #
def test_a_strict_subset_submitter_does_not_associate(db):
    """Condition 2 (``existing.populated_fields <= incoming.populated_fields``) is
    what makes an association SAFE rather than merely plausible: a user may only
    read fields they produced themselves. Its deliberate cost is that a strict
    SUBSET falls back to the worker."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id

    # B agrees on every overlapping field but produced no best_move_san.
    subset = _browser_row()
    subset.pop("best_move_san")
    assert _reasons(_write(Factory, [subset], submitter=BOB)) == [
        Reason.SAME_PROFILE_IDEMPOTENT
    ]
    assert _associations(Factory) == {(row_id, ALICE)}


def test_a_strict_superset_submitter_associates(db):
    """A superset reads only fields it produced, so it associates — and merges,
    because it is also the row's only associate after the precondition."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id

    superset = _browser_row(best_eval_mate=5, played_eval_mate=5)
    assert _reasons(_write(Factory, [superset], submitter=ALICE)) == [
        Reason.SAME_PROFILE_SUPERSET_MERGE
    ]
    assert _associations(Factory) == {(row_id, ALICE)}


def test_conflicting_fields_associate_nobody(db):
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id

    conflicting = _browser_row(played_eval=-100, eval_delta=120)
    assert _reasons(_write(Factory, [conflicting], submitter=BOB)) == [
        Reason.MERGE_CONFLICT_KEEP
    ]
    assert _associations(Factory) == {(row_id, ALICE)}


def test_a_batch_with_no_submitter_creates_no_association(db):
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=None)
    assert _associations(Factory) == set()


# --------------------------------------------------------------------------- #
# the single-affected-submitter MERGE precondition
# --------------------------------------------------------------------------- #
def test_cross_user_superset_merge_is_refused(db):
    """``_build_merged`` keeps the EXISTING row's provenance and only fills its null
    evidence columns, so merging B's superset into A's row would let A read fields
    only B produced. Refused with MERGE_OWNER_MISMATCH_KEEP — and NOT a denial of
    access: B still associates with the UNMERGED row through the claim rule."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id

    superset = _browser_row(best_eval_mate=5, played_eval_mate=5)
    assert _reasons(_write(Factory, [superset], submitter=BOB)) == [
        Reason.MERGE_OWNER_MISMATCH_KEEP
    ]

    stored = _stored(Factory)
    # A's row is untouched: it never gains B's mate fields.
    assert stored.best_eval_mate is None
    assert stored.played_eval_mate is None
    assert stored.evidence_contract_id == RESOLVER_COMPLETE_V2
    # B associates with the unmerged row, which contains only fields B produced.
    assert _associations(Factory) == {(row_id, ALICE), (row_id, BOB)}


def test_contract_upgrade_merge_carries_the_same_guard(db):
    """The precondition guards BOTH merge decisions, not just the superset one."""
    engine, Factory = db
    existing = project_cache_row(_browser_row(evidence_contract_id=MINIMAL_PLAYED_EVAL))
    incoming = project_cache_row(_browser_row())
    decision, reason = decide_analysis_cache_replacement(
        existing,
        incoming,
        existing_submitters=frozenset({ALICE}),
        incoming_submitter=BOB,
    )
    assert (decision, reason) == (Decision.KEEP, Reason.MERGE_OWNER_MISMATCH_KEEP)


def test_a_merge_whose_associates_are_exactly_the_submitter_proceeds(db):
    existing = project_cache_row(_browser_row())
    incoming = project_cache_row(_browser_row(best_eval_mate=5))
    decision, reason = decide_analysis_cache_replacement(
        existing,
        incoming,
        existing_submitters=frozenset({ALICE}),
        incoming_submitter=ALICE,
    )
    assert (decision, reason) == (Decision.MERGE, Reason.SAME_PROFILE_SUPERSET_MERGE)


def test_canonical_merges_skip_the_precondition_entirely(db):
    """Canonical parity is independent of the claim rule being correct: even a
    (impossible) canonical row carrying associations merges exactly as before."""
    existing = project_cache_row(_canonical_row())
    incoming = project_cache_row(_canonical_row(best_eval_mate=5))
    decision, reason = decide_analysis_cache_replacement(
        existing,
        incoming,
        existing_submitters=frozenset({ALICE, BOB}),
        incoming_submitter=None,
    )
    assert (decision, reason) == (Decision.MERGE, Reason.SAME_PROFILE_SUPERSET_MERGE)


# --------------------------------------------------------------------------- #
# canonical parity: browser claims never touch a canonical row
# --------------------------------------------------------------------------- #
def test_a_browser_submission_over_a_canonical_row_creates_no_association(db):
    """Condition 3 of the claim rule is load-bearing. A browser submission can
    AGREE with and COVER a canonical tuple; Rule 5 returns INCOMPATIBLE_KEEP, and a
    profile-agnostic claim rule would attach a browser user to the canonical row.
    That association would then block a later canonical merge."""
    engine, Factory = db
    _write(Factory, [_canonical_row()], submitter=None)
    row_id = _stored(Factory).id

    covering = _browser_row(best_eval_mate=5, played_eval_mate=5)
    assert _reasons(_write(Factory, [covering], submitter=ALICE)) == [
        Reason.INCOMPATIBLE_KEEP
    ]
    assert _associations(Factory) == set()

    # The submitter still reads it — through canonical authority, not an association.
    s = Factory()
    assert resolve_trusted_positions(
        s, [NORM], Capability.POSITION_READ, ALICE
    )[NORM] is not None
    s.close()

    # And a later canonical same-profile MERGE proceeds exactly as it does today.
    assert _reasons(_write(Factory, [_canonical_row(best_eval_mate=5)])) == [
        Reason.SAME_PROFILE_SUPERSET_MERGE
    ]
    assert _stored(Factory).best_eval_mate == 5
    assert _associations(Factory) == set()
    assert row_id == _stored(Factory).id


# --------------------------------------------------------------------------- #
# REPLACE clears associations unconditionally
# --------------------------------------------------------------------------- #
def test_a_canonical_replace_clears_every_prior_association(db):
    """The clearing half is unconditional — a stale association surviving a full
    overwrite would let its holder read facts it never submitted. A canonical
    REPLACE clears and adds none, leaving the row association-free."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id
    assert _associations(Factory) == {(row_id, ALICE)}

    assert _reasons(_write(Factory, [_canonical_row(best_eval=99, played_eval=99)])) == [
        Reason.DOMINATES_REPLACE
    ]
    assert _associations(Factory) == set()

    # The displaced associate falls back rather than reading the replacement...
    s = Factory()
    resolved = resolve_trusted_positions(s, [NORM], Capability.POSITION_READ, ALICE)
    s.close()
    # ...though canonical authority serves them the new row on its own merit.
    assert resolved[NORM] is not None
    assert resolved[NORM].evidence.is_effectively_authoritative() is True


def test_a_browser_replace_clears_then_associates_its_own_writer(db):
    """A cross-profile dominance REPLACE by a browser submission: the prior
    associates go, and only the writer is added.

    The loser is a RETIRED browser-analysis-v1 row, seeded directly — the writer
    refuses to insert a retired-profile row, but rows stored before retirement
    exist and the truthful visible-MultiPV protocol correctively replaces them."""
    engine, Factory = db
    s = Factory()
    stale = AnalysisCache(
        **{
            "fen_before": START,
            "normalized_fen_before": NORM,
            "move_uci": "e2e4",
            "move_san": "e4",
            "best_move_uci": "e2e4",
            "best_move_san": "e4",
            "best_line_uci": "e2e4 e7e5",
            "played_eval": 25,
            "best_eval": 25,
            "eval_delta": 0,
            "classification": "best",
            "source": "analysis",
            "analysis_profile_id": BROWSER_ANALYSIS_PROFILE_ID,
            "evidence_contract_id": RESOLVER_COMPLETE_V2,
            **stamp_profile_full(BROWSER_ANALYSIS_PROFILE_ID),
        }
    )
    s.add(stale)
    s.commit()
    row_id = stale.id
    # A stale association on the pre-replacement row.
    s.add(AnalysisCacheSubmission(analysis_cache_id=row_id, user_id=BOB))
    s.commit()
    s.close()

    assert _reasons(_write(Factory, [_browser_row()], submitter=ALICE)) == [
        Reason.PROTOCOL_CORRECTED_REPLACE
    ]
    assert _associations(Factory) == {(row_id, ALICE)}


# --------------------------------------------------------------------------- #
# atomicity
# --------------------------------------------------------------------------- #
def test_a_rolled_back_batch_leaves_no_association(db, monkeypatch):
    engine, Factory = db
    real = repo._apply_claims

    def boom(session, claims, submitter_user_id, *, insert):
        real(session, claims, submitter_user_id, insert=insert)
        raise RuntimeError("commit failed")

    monkeypatch.setattr(repo, "_apply_claims", boom)
    with pytest.raises(RuntimeError):
        _write(Factory, [_browser_row()], submitter=ALICE)
    assert _associations(Factory) == set()


def test_a_retried_batch_produces_exactly_one_association(db, monkeypatch):
    """Insertions use ON CONFLICT DO NOTHING, so the bounded whole-transaction
    retry replays the claim pass idempotently."""
    engine, Factory = db
    real = repo._apply_claims
    calls = {"n": 0}

    def flaky(session, claims, submitter_user_id, *, insert):
        real(session, claims, submitter_user_id, insert=insert)
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("database is locked", {}, Exception())

    monkeypatch.setattr(repo, "_apply_claims", flaky)
    _write(Factory, [_browser_row()], submitter=ALICE)
    assert calls["n"] >= 2
    assert len(_associations(Factory)) == 1


@pytest.mark.parametrize("claim_first", [True, False])
def test_claim_and_canonical_replace_serialize_consistently(db, claim_first):
    """Whichever order a claiming browser submission and a canonical REPLACE of the
    same key serialize in, the end state is consistent: the claim either PRECEDES
    the REPLACE and is cleared by it, or FOLLOWS it, sees a canonical post-decision
    row, and is refused. No interleaving leaves a user associated with a row whose
    stored facts they did not submit.

    (The writer holds one transaction per batch — SQLite BEGIN IMMEDIATE, Postgres
    FOR UPDATE — so concurrency reduces to these two serial orders.)"""
    engine, Factory = db
    _write(Factory, [_browser_row(best_eval=15, played_eval=15, eval_delta=0)])

    canonical = _canonical_row(best_eval=99, played_eval=99)
    browser = _browser_row(best_eval=15, played_eval=15, eval_delta=0)
    if claim_first:
        _write(Factory, [browser], submitter=ALICE)
        _write(Factory, [canonical])
    else:
        _write(Factory, [canonical])
        _write(Factory, [browser], submitter=ALICE)

    stored = _stored(Factory)
    assert stored.analysis_profile_id == CANONICAL_PROFILE_ID
    assert stored.best_eval == 99
    # Either order: no association survives against the canonical row.
    assert _associations(Factory) == set()


# --------------------------------------------------------------------------- #
# terminal recovery still sees the stored row's associations
# --------------------------------------------------------------------------- #
def test_terminal_recovery_lock_loads_associations_before_deciding(db, monkeypatch):
    """A key that oscillates past the bounded pass budget is decided by the FINAL
    lock-and-decide. That path must load associations too — a terminal decision
    against an empty set would wave a cross-submitter merge through, which is
    exactly what the precondition exists to refuse."""
    engine, Factory = db
    _write(Factory, [_browser_row()], submitter=ALICE)
    row_id = _stored(Factory).id

    # Force every bounded pass to see the key as vanished so the batch falls
    # through to the terminal lock-and-decide.
    real_lock = repo._lock_existing_with_submissions
    calls = {"n": 0}

    def flaky_lock(session, conflicted, *, for_update):
        calls["n"] += 1
        locked = real_lock(session, conflicted, for_update=for_update)
        if calls["n"] <= repo._MAX_TOCTOU_PASSES:
            return repo._LockedRows({}, locked.projections, locked.submissions)
        return locked

    monkeypatch.setattr(repo, "_lock_existing_with_submissions", flaky_lock)

    superset = _browser_row(best_eval_mate=5, played_eval_mate=5)
    results = _write(Factory, [superset], submitter=BOB)
    assert _reasons(results) == [Reason.MERGE_OWNER_MISMATCH_KEEP]
    assert calls["n"] == repo._MAX_TOCTOU_PASSES + 1  # terminal pass ran
    assert _stored(Factory).best_eval_mate is None
    assert _associations(Factory) == {(row_id, ALICE), (row_id, BOB)}


# --------------------------------------------------------------------------- #
# _dedupe_batch is deliberately unguarded
# --------------------------------------------------------------------------- #
def test_dedupe_batch_carries_no_ownership_precondition(db):
    """It collapses ONE batch's in-memory rows before any reach the database, so
    there is no persisted association set to test — and the batch-level submitter
    means every row it could collapse necessarily shares one submitter. The
    survivor then faces the ordinary claim rule against whatever is persisted."""
    engine, Factory = db
    rows = [_browser_row(), _browser_row(best_eval_mate=5, played_eval_mate=5)]
    surviving, rejected = repo._dedupe_batch(rows)
    assert rejected == []
    assert len(surviving) == 1
    assert surviving[0]["best_eval_mate"] == 5  # the union survives

    results = _write(Factory, rows, submitter=ALICE)
    assert _reasons(results) == [Reason.NEW_KEY]
    row_id = _stored(Factory).id
    assert _associations(Factory) == {(row_id, ALICE)}


# --------------------------------------------------------------------------- #
# the association write bumps the shared evidence epoch
# --------------------------------------------------------------------------- #
def test_an_association_write_bumps_evidence_epoch(db):
    engine, Factory = db
    _write(Factory, [_browser_row()])
    s = Factory()
    before = s.execute(text("SELECT value FROM evidence_epoch WHERE id = 1")).scalar()
    s.close()

    _write(Factory, [_browser_row()], submitter=ALICE)

    s = Factory()
    after = s.execute(text("SELECT value FROM evidence_epoch WHERE id = 1")).scalar()
    # No evidence column changed (SAME_PROFILE_IDEMPOTENT); only the association
    # row was inserted — and that alone must advance the shared epoch.
    assert after > before
    assert s.execute(
        text(
            "SELECT kind, fen, last_changed_epoch "
            "FROM shared_evidence_scope_versions "
            "WHERE (kind = 'raw' AND fen = :raw) "
            "OR (kind = 'norm' AND fen = :norm) ORDER BY kind"
        ),
        {"raw": START, "norm": NORM},
    ).all() == [("norm", NORM, after), ("raw", START, after)]
    assert viewer_associated_ids(
        s, ALICE, [r.id for r in s.query(AnalysisCache).all()]
    )
    s.close()


# --------------------------------------------------------------------------- #
# the DEDICATED file-backed writer enforces the association foreign keys
# --------------------------------------------------------------------------- #
def test_the_dedicated_writer_path_is_actually_taken(file_db):
    """Guard for the two tests below: prove the fixture reaches the other engine.

    If ``_sqlite_write_engine`` ever starts returning the caller's bind for file
    URLs too, the FK regressions below would pass by testing the caller's pragmas
    instead of the writer's, and the real gap would reopen unnoticed.
    """
    engine, _ = file_db
    assert repo._sqlite_write_engine(engine) is not engine


def test_the_dedicated_writer_refuses_an_association_for_a_missing_user(file_db):
    """SQLite enforces foreign keys PER CONNECTION, defaulting to OFF.

    The writer's dedicated engine does not inherit ``app.db``'s pragma, so without
    setting its own it is the one path in the system that can insert a grant naming
    a user who does not exist — and, symmetrically, the one path whose rows survive
    the ON DELETE CASCADE that is supposed to retire a deleted user's grants. On a
    recycled user id that silently hands a stranger another user's read access.
    """
    _, Factory = file_db
    ghost = 999  # never inserted into users

    with pytest.raises(IntegrityError):
        _write(Factory, [_browser_row()], submitter=ghost)

    assert _associations(Factory) == set()


def test_the_dedicated_writer_still_associates_a_real_submitter(file_db):
    """Positive control: enabling the pragma did not break the normal file path."""
    _, Factory = file_db
    results = _write(Factory, [_browser_row()], submitter=ALICE)

    assert _reasons(results) == [Reason.NEW_KEY]
    assert _associations(Factory) == {(_stored(Factory).id, ALICE)}
