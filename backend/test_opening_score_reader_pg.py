"""Deterministic publication-boundary barriers for the opening-score tree read.

Under B50 there is exactly ONE current row set per (owner, color): a publication
mutates those rows in place and deletes the prior marker in the same transaction.
A multi-statement reader under READ COMMITTED could therefore splice pre- and
post-publication rows into one response. These tests drive a second real
PostgreSQL connection into every boundary where that could happen and prove the
reader either serves one coherent generation or throws the whole attempt away.

SQLite cannot stand in for any of it: its single StaticPool connection gives a
test no second transaction to commit a publication from, and it has no REPEATABLE
READ isolation level for the snapshot fallback to fall back TO.

The route function is called directly rather than through an HTTP client so a
barrier can sit inside one request's builder, on that request's own Session.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from conftest import pg_gate
from app.api import openings as openings_api
from app.migration_guard import MIGRATION_LOCK_CLASSID, MIGRATION_LOCK_OBJID
from app.models import OpeningScoreBatch
from app.opening_score_delta import _shared_scope_change_statement
from app.opening_cache import reserve_opening_score_generation
from app.opening_score_storage import (
    RetiredScoreHandle,
    ScoreHandle,
    ScorePayload,
    StorageFormat,
    handle_is_live,
    latest_batch_view,
    latest_marker_query,
    publish_scores,
    read_group,
    score_snapshot,
)
from app.security import TokenPayload
from test_tree_api import E4, E4E5, _make_graph, _make_roots

USER = 4242
COLOR = "white"
REGISTRY = "reader-pg-registry"
NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
# Distinct per-generation stamps: a response that stitched two generations
# together could still carry a plausible timestamp if every publication shared
# one clock, so the coherence assertions need them to differ.
GEN_ONE_AT = datetime(2026, 9, 19, 1, tzinfo=timezone.utc)
GEN_TWO_AT = datetime(2026, 9, 19, 2, tzinfo=timezone.utc)
CALLER = TokenPayload(user_id=USER, username="reader-pg", is_anonymous=False)
PAYLOAD_TABLES = {"opening_current_positions", "opening_position_scores"}


def _payload(score: float, *, uci: str = "e7e5", traversal: int = 6) -> ScorePayload:
    """One root, one position row for 1.e4 e5, and one observed edge into it."""
    return ScorePayload(
        roots=(
            {
                "opening_key": E4,
                "opening_name": "Kings Pawn",
                "opening_family": "Kings Pawn",
                "opening_score": score,
                "confidence": 0.5,
                "coverage": 0.4,
                "weighted_depth": 2.0,
                "sample_size": 7,
                "game_count": 2,
                "last_practiced_at": None,
                "strongest_branch_name": None,
                "strongest_branch_key": None,
                "strongest_branch_score": None,
                "weakest_branch_name": None,
                "weakest_branch_key": None,
                "weakest_branch_score": None,
                "underexposed_branch_name": None,
                "underexposed_branch_key": None,
                "underexposed_branch_value": None,
            },
        ),
        positions=(
            {
                "normalized_fen": E4E5,
                "in_book": True,
                "has_evidence": True,
                "opening_score": score,
                "confidence": 0.5,
                "coverage": 0.4,
                "weighted_depth": 2.0,
                "sample_size": 7,
                "game_count": 2,
                "last_practiced_at": None,
            },
        ),
        edges=(
            {
                "parent_fen": E4,
                "child_fen": E4E5,
                "uci": uci,
                "traversal_count": traversal,
                "live_attempts": 2,
                "live_passes": 1,
                "live_fails": 1,
            },
        ),
        scope=({"kind": "raw", "fen": "scope-fen"},),
    )


def _publish(
    factory,
    score: float,
    *,
    uci: str = "e7e5",
    traversal: int = 6,
    fmt: StorageFormat = StorageFormat.CURRENT,
    computed_at: datetime = NOW,
    user_id: int = USER,
    player_color: str = COLOR,
) -> ScoreHandle:
    """Commit one generation from an INDEPENDENT connection."""
    with factory() as db:
        generation = reserve_opening_score_generation(db, user_id, player_color)
        batch = OpeningScoreBatch(
            user_id=user_id,
            player_color=player_color,
            generation=generation,
            computed_at=computed_at,
            registry_fingerprint=REGISTRY,
            evidence_seq=1,
            cache_epoch=1,
            scoped_shared_digest="scope",
        )
        published = publish_scores(
            db,
            batch,
            _payload(score, uci=uci, traversal=traversal),
            storage_format=fmt,
        )
        return ScoreHandle.from_batch(published)


def _tree(db, *, barriers=(), bootstrap_state="warm_fresh"):
    """Run the real /tree route on ``db`` with only the registry singletons stubbed.

    ``ensure_tree_cache`` is stubbed both to keep the scheduler out and to act as
    the enqueue counter: it is the route's ONE bootstrap/trigger call, and a retry
    must not produce a second one. ``bootstrap_state`` is the label it hands back,
    which the route may relabel against the marker it actually serves.
    """
    graph, roots = _make_graph(), _make_roots()
    with contextlib.ExitStack() as stack:
        ensure_spy = stack.enter_context(
            patch(
                "app.api.openings.ensure_tree_cache",
                return_value=(None, None, bootstrap_state, REGISTRY),
            )
        )
        stack.enter_context(
            patch("app.api.openings.get_opening_graph", return_value=graph)
        )
        stack.enter_context(
            patch("app.api.openings.get_opening_roots", return_value=roots)
        )
        stack.enter_context(
            patch("app.api.openings.routing_view", side_effect=lambda g: None)
        )
        stack.enter_context(
            patch("app.api.openings.lookup_move_evals", side_effect=lambda d, r: {})
        )
        for barrier in barriers:
            stack.enter_context(barrier)
        response = openings_api.get_opening_tree(
            player_color=COLOR, move=["e2e4"], opening=None, db=db, user=CALLER
        )
    return response, ensure_spy


def _barrier(target: str, action, *, on_calls=(1,)):
    """Patch ``target`` so ``action()`` runs right AFTER the chosen calls.

    The wrapped function keeps its real behavior, so the publication lands between
    this statement and whatever the builder issues next — exactly the window a
    multi-statement reader has to survive. Returns ``(patcher, state)``.
    """
    real = getattr(openings_api, target)
    state = {"calls": 0}

    def wrapper(*args, **kwargs):
        result = real(*args, **kwargs)
        state["calls"] += 1
        if on_calls is None or state["calls"] in on_calls:
            action()
        return result

    return patch(f"app.api.openings.{target}", side_effect=wrapper), state


def _once(action):
    """Wrap ``action`` so only the first barrier hit fires it."""
    fired = []

    def run():
        if not fired:
            fired.append(action())

    return run, fired


def _scores(response):
    """Every position metric the response carries, for coherence assertions."""
    return {
        node.child_fen: node.opening_score
        for column in response.columns
        for node in column.nodes
        if node.opening_score is not None
    }


def _ucis(response):
    return {node.uci for column in response.columns for node in column.nodes}


@contextlib.contextmanager
def _timing_fields(monkeypatch, caplog):
    """Capture the route's timing line as a ``{key: value}`` dict of strings.

    ``score_read_mode`` and ``score_read_attempts`` are emitted ONLY here, so a
    test that wants to assert the reader took the snapshot fallback — rather than
    infer it from how many times a seam was called — has to read what an operator
    would read. Parsed from the rendered message so it is insensitive to the
    argument order of a format string with thirty-odd slots.
    """
    monkeypatch.setenv("OPENING_TREE_TIMING_LOG", "1")
    fields: dict[str, str] = {}
    with caplog.at_level(logging.INFO, logger="app.api.openings"):
        yield fields
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("opening_tree timing ")
    ]
    assert len(lines) == 1, lines
    fields.update(
        part.split("=", 1) for part in lines[0].split(" ") if "=" in part
    )


# ---------------------------------------------------------------------------
# Optimistic attempts: every publication boundary in one tree read.
# ---------------------------------------------------------------------------


@pg_gate
@pytest.mark.parametrize(
    "seam",
    ["lookup_observed_edges_for_parents", "lookup_position_scores_for_batch"],
)
def test_pg_a_publication_between_bounded_reads_is_retried_not_spliced(
    pg_engine, pg_session_factory, seam
):
    """A publication landing between two of the builder's statements.

    The edge seam covers wave 1 -> wave 2; the position seam covers the last
    payload query -> the final fence. Either way the attempt is discarded whole
    and the retry serves ONE generation.
    """
    _publish(pg_session_factory, 10.0, computed_at=GEN_ONE_AT)
    action, fired = _once(
        lambda: _publish(pg_session_factory, 90.0, computed_at=GEN_TWO_AT)
    )
    barrier, _state = _barrier(seam, action)
    with pg_session_factory() as db:
        response, ensure_spy = _tree(db, barriers=[barrier])

    assert fired, "the barrier never fired"
    assert set(_scores(response).values()) == {90.0}
    # The response is stamped from the generation it actually served, not the one
    # the request first resolved: scores and timestamp come from the same marker.
    assert response.batch_computed_at == GEN_TWO_AT
    # One bootstrap/enqueue for the whole request, retries included.
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_a_retirement_before_the_first_query_is_retried(
    pg_engine, pg_session_factory
):
    """The marker is already gone when the builder issues its first payload read."""
    _publish(pg_session_factory, 10.0)
    action, fired = _once(lambda: _publish(pg_session_factory, 55.0))
    barrier, _state = _barrier("latest_batch_view", action)
    with pg_session_factory() as db:
        response, ensure_spy = _tree(db, barriers=[barrier])

    assert fired
    assert set(_scores(response).values()) == {55.0}
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_a_publication_after_the_fence_is_served_as_read(
    pg_engine, pg_session_factory
):
    """A publication committing AFTER the fence cannot retroactively invalidate it.

    The fence's claim is "no publication committed DURING this read", not "no
    publication ever", so the response stays the generation the reader proved live.
    """
    _publish(pg_session_factory, 10.0)
    with pg_session_factory() as db:
        response, _ = _tree(db)
        _publish(pg_session_factory, 90.0)

    assert set(_scores(response).values()) == {10.0}


@pg_gate
def test_pg_a_first_publication_invalidates_the_book_only_attempt(
    pg_engine, pg_session_factory
):
    """No marker at all is still a claim that has to be fenced.

    Without ``pair_has_no_batch``, a read that started with no batch and finished
    after the first publication would serve a book-only tree while the user's
    observed moves were already durable.
    """
    action, fired = _once(lambda: _publish(pg_session_factory, 42.0))
    barrier, _state = _barrier("lookup_root_eval", action)
    with pg_session_factory() as db:
        response, ensure_spy = _tree(db, barriers=[barrier])

    assert fired
    # The retry resolved the freshly published marker and served its observed edge.
    assert "e7e5" in _ucis(response)
    assert set(_scores(response).values()) == {42.0}
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_a_retry_resets_every_attempt_and_never_re_enqueues(
    pg_engine, pg_session_factory
):
    """Nothing crosses an attempt: no mixed generation and no second enqueue."""
    _publish(pg_session_factory, 10.0, uci="e7e5", traversal=1)
    action, fired = _once(
        lambda: _publish(pg_session_factory, 90.0, uci="c7c5", traversal=9)
    )
    barrier, _state = _barrier("lookup_observed_edges_for_parents", action)
    with pg_session_factory() as db:
        response, ensure_spy = _tree(db, barriers=[barrier])

    assert fired
    # The discarded attempt's observed edge must not survive into the response.
    assert "c7c5" in _ucis(response)
    assert set(_scores(response).values()) == {90.0}
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_a_neighbours_publication_never_invalidates_this_read(
    pg_engine, pg_session_factory, monkeypatch, caplog
):
    """Retirement is scoped to one (owner, colour) pair.

    A publication for another user and one for this user's OTHER colour both land
    mid-read. Neither touches this pair's marker, so the first optimistic attempt
    has to stand — a fence that keyed on "any publication" would make every tree
    read on a busy instance retry, and eventually fall back to a snapshot.
    """
    _publish(pg_session_factory, 10.0, computed_at=GEN_ONE_AT)

    def neighbours():
        _publish(
            pg_session_factory, 99.0, user_id=USER + 7, computed_at=GEN_TWO_AT
        )
        _publish(
            pg_session_factory, 98.0, player_color="black", computed_at=GEN_TWO_AT
        )

    action, fired = _once(neighbours)
    barrier, _state = _barrier("lookup_observed_edges_for_parents", action)
    with pg_session_factory() as db:
        with _timing_fields(monkeypatch, caplog) as timings:
            response, ensure_spy = _tree(db, barriers=[barrier])

    assert fired
    assert timings["score_read_attempts"] == "1"
    assert timings["score_read_mode"] == "optimistic"
    assert set(_scores(response).values()) == {10.0}
    assert response.batch_computed_at == GEN_ONE_AT
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_a_cold_bootstrap_timeout_with_no_batch_stays_book_only(
    pg_engine, pg_session_factory, monkeypatch, caplog
):
    """A cold pair whose bootstrap timed out and that still has no marker at all.

    The empty read is a claim like any other, so ``pair_has_no_batch`` has to
    CONFIRM the absence before it can be served. Nothing published, so there is
    nothing to relabel: the request reports ``bootstrap_timeout`` because it
    really did block on the bootstrap, and the client is right to retry.
    """
    with pg_session_factory() as db:
        with _timing_fields(monkeypatch, caplog) as timings:
            response, ensure_spy = _tree(db, bootstrap_state="bootstrap_timeout")

    assert timings["score_read_attempts"] == "1"
    assert timings["score_read_mode"] == "optimistic"
    assert timings["cache_state"] == "bootstrap_timeout"
    assert response.cache_state == "bootstrap_timeout"
    assert response.batch_computed_at is None
    assert _scores(response) == {}
    assert ensure_spy.call_count == 1


# ---------------------------------------------------------------------------
# Snapshot fallback.
# ---------------------------------------------------------------------------


@pg_gate
def test_pg_two_invalidated_attempts_fall_back_to_a_coherent_snapshot(
    pg_engine, pg_session_factory, monkeypatch, caplog
):
    """Both optimistic attempts lose the race, and the snapshot still answers.

    Publications keep landing DURING the fallback; a REPEATABLE READ snapshot
    cannot see them, so the response is one generation either way.
    """
    _publish(pg_session_factory, 10.0, computed_at=GEN_ONE_AT)
    scores = itertools.count(20, 10)
    stamps = (GEN_TWO_AT + timedelta(hours=n) for n in itertools.count())
    barrier, state = _barrier(
        "lookup_observed_edges_for_parents",
        lambda: _publish(
            pg_session_factory, float(next(scores)), computed_at=next(stamps)
        ),
        on_calls=None,
    )
    with pg_session_factory() as db:
        with _timing_fields(monkeypatch, caplog) as timings:
            response, ensure_spy = _tree(db, barriers=[barrier])

    # The route says so itself, rather than the test inferring it from call counts.
    assert timings["score_read_mode"] == "snapshot"
    assert timings["score_read_attempts"] == "2"
    # Exactly four completed wave queries, which is the fallback's signature:
    # each optimistic attempt publishes on wave 1 and then dies on wave 2 (the
    # retired marker raises before the counter advances), and the snapshot pass
    # completes both waves.
    assert state["calls"] == 4
    # 10 -> 20 (attempt 1) -> 30 (attempt 2) -> snapshot opens -> 40 (invisible).
    # A REPEATABLE READ snapshot cannot see a publication committed inside it, so
    # the response is generation 30 whole, with nothing to fence.
    assert set(_scores(response).values()) == {30.0}
    assert response.batch_computed_at == GEN_TWO_AT + timedelta(hours=1)
    assert ensure_spy.call_count == 1


@pg_gate
def test_pg_the_snapshot_fallback_returns_the_connection_clean(pg_engine):
    """First use of the read-only snapshot on a request-scoped POOLED session.

    A connection handed back REPEATABLE READ or READ ONLY would silently change
    (or break) whatever request checks it out next, so the reset is proven here
    rather than assumed from ``rollback()``. ``pool_size=1`` guarantees the second
    session gets the very connection the snapshot ran on.
    """
    single = create_engine(pg_engine.url, pool_size=1, max_overflow=0)
    try:
        factory = sessionmaker(autocommit=False, autoflush=False, bind=single)
        _publish(factory, 10.0)
        with factory() as db:
            db.rollback()
            with score_snapshot(db):
                assert latest_batch_view(db, USER, COLOR) is not None
        with factory() as db:
            assert (
                db.execute(text("SHOW transaction_isolation")).scalar()
                == "read committed"
            )
            assert db.execute(text("SHOW transaction_read_only")).scalar() == "off"
            db.rollback()
            # Still read-write: this commits a real row.
            assert reserve_opening_score_generation(db, USER + 1, COLOR) == 1
    finally:
        single.dispose()


# ---------------------------------------------------------------------------
# Exact-marker and bounded-read contracts.
# ---------------------------------------------------------------------------


@pg_gate
def test_pg_an_exact_retired_marker_never_resolves_current_rows(
    pg_engine, pg_session_factory
):
    """Retired is neither empty nor the CURRENT payload.

    The rows are still physically present under the new generation, so an
    unanchored query would happily hand them back under the old marker.
    """
    stale = _publish(pg_session_factory, 10.0)
    _publish(pg_session_factory, 90.0)
    with pg_session_factory() as db:
        assert not handle_is_live(db, stale)
        for group in ("roots", "positions", "edges", "scope"):
            with pytest.raises(RetiredScoreHandle):
                read_group(db, stale, group)
        # The live marker still serves its own payload.
        live = latest_batch_view(db, USER, COLOR).handle
        assert read_group(db, live, "positions")


@pg_gate
def test_pg_mixed_format_pairs_read_side_by_side(pg_engine, pg_session_factory):
    """Conversion is per pair, so both formats are live at once mid-cutover.

    Each marker's own format decides which table the reader joins, in one
    statement, with no cross-talk between the two pairs.
    """
    _publish(pg_session_factory, 10.0, fmt=StorageFormat.LEGACY)
    other = sessionmaker(autocommit=False, autoflush=False, bind=pg_engine)
    with other() as db:
        legacy = latest_batch_view(db, USER, COLOR)
        assert legacy.storage_format == StorageFormat.LEGACY.value
        legacy_rows = read_group(db, legacy.handle, "positions")

    _publish(pg_session_factory, 10.0, fmt=StorageFormat.CURRENT)
    with other() as db:
        current = latest_batch_view(db, USER, COLOR)
        assert current.storage_format == StorageFormat.CURRENT.value
        current_rows = read_group(db, current.handle, "positions")
        # The retired legacy marker does not resolve the current rows.
        with pytest.raises(RetiredScoreHandle):
            read_group(db, legacy.handle, "positions")

    assert [row.normalized_fen for row in legacy_rows] == [
        row.normalized_fen for row in current_rows
    ]
    assert [row.opening_score for row in legacy_rows] == [
        row.opening_score for row in current_rows
    ]


@pg_gate
@pytest.mark.parametrize(
    "fmt",
    [StorageFormat.LEGACY, StorageFormat.CURRENT],
    ids=["legacy", "current"],
)
def test_pg_bounded_reads_never_scan_the_unmatched_table(
    pg_engine, pg_session_factory, fmt
):
    """The format guard sits on the join's OUTER side, so PostgreSQL may still
    probe the table the marker's format rules out. That probe must stay an indexed
    lookup returning zero rows, never an unbounded scan of the other format's
    table (the epic's bounded-read check)."""
    _publish(pg_session_factory, 10.0, fmt=fmt)
    with pg_session_factory() as db:
        _seed_unrelated_position_rows(db)
        statement = latest_marker_query(
            USER,
            COLOR,
            "positions",
            on=lambda table: table.c.normalized_fen.in_([E4E5]),
            dialect_name="postgresql",
        )
        compiled = statement.compile(
            db.get_bind(), compile_kwargs={"literal_binds": True}
        )
        plan = db.execute(text("EXPLAIN (FORMAT JSON) " + str(compiled))).scalar_one()
        scanned = {
            node.get("Relation Name")
            for node in _plan_nodes(plan[0]["Plan"])
            if node["Node Type"] in {"Seq Scan", "Parallel Seq Scan"}
        }
        assert not scanned & PAYLOAD_TABLES, plan


@pg_gate
def test_pg_the_tree_read_takes_no_advisory_locks_and_waits_on_none(
    pg_engine, pg_session_factory
):
    """Non-contention with the migration and graph-write lock namespaces.

    Rather than time a read against a held lock and call the absence of a stall
    "non-contention", assert the stronger property the reader actually has: it
    takes NO advisory lock, so there is nothing for those namespaces to contend
    with. Both locks are held from another backend for the whole read, and the
    reader's own backend is inspected from that backend mid-read. ``lock_timeout``
    turns a regression that DID start taking a lock into a failure rather than a
    hung gate.
    """
    _publish(pg_session_factory, 10.0)
    guarded = create_engine(
        pg_engine.url,
        pool_size=1,
        max_overflow=0,
        connect_args={"options": "-c lock_timeout=3000"},
    )
    held: list[int] = []
    try:
        factory = sessionmaker(autocommit=False, autoflush=False, bind=guarded)
        with _advisory_lock_holder(pg_engine) as observer:
            with factory() as db:

                def probe():
                    pid = db.execute(text("SELECT pg_backend_pid()")).scalar()
                    held.append(
                        observer.execute(
                            text(
                                "SELECT count(*) FROM pg_locks "
                                "WHERE locktype = 'advisory' AND pid = :pid"
                            ),
                            {"pid": pid},
                        ).scalar()
                    )

                barrier, _state = _barrier("lookup_position_scores_for_batch", probe)
                response, _ = _tree(db, barriers=[barrier])
    finally:
        guarded.dispose()

    assert held == [0]
    assert set(_scores(response).values()) == {10.0}


@contextlib.contextmanager
def _advisory_lock_holder(engine):
    """Hold the migration lock and this user's graph-write lock on one backend."""
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:classid, :objid)"),
            {"classid": MIGRATION_LOCK_CLASSID, "objid": MIGRATION_LOCK_OBJID},
        )
        connection.execute(text("SELECT pg_advisory_lock(:uid)"), {"uid": USER})
        yield connection
    finally:
        connection.execute(text("SELECT pg_advisory_unlock_all()"))
        connection.close()


@pg_gate
@pytest.mark.parametrize(
    "fmt",
    [StorageFormat.LEGACY, StorageFormat.CURRENT],
    ids=["legacy", "current"],
)
def test_pg_the_session_start_proof_never_scans_global_shared_evidence(
    pg_engine, pg_session_factory, fmt
):
    """Proof 2's scope check stays bounded by the BATCH's own scope.

    ``shared_evidence_scope_versions`` is global: nothing this owner does bounds
    it. The current-format scope keys are ``C``-collated and that table's are not,
    so PostgreSQL resolves the join comparison to ``C`` — and it will only use an
    index whose own collation matches the clause's. Without the probe collation
    the primary key becomes unusable and one probe per scope row silently becomes
    a sequential scan of every shared FEN on the instance, on a path that runs
    once per baseline job and once per push-fill candidate.
    """
    handle = _publish(pg_session_factory, 10.0, fmt=fmt)
    with pg_session_factory() as db:
        _seed_shared_evidence_versions(db)
        statement = _shared_scope_change_statement(db, handle, 0)
        compiled = statement.compile(
            db.get_bind(), compile_kwargs={"literal_binds": True}
        )
        plan = db.execute(text("EXPLAIN (FORMAT JSON) " + str(compiled))).scalar_one()
        scanned = {
            node.get("Relation Name")
            for node in _plan_nodes(plan[0]["Plan"])
            if node["Node Type"] in {"Seq Scan", "Parallel Seq Scan"}
        }
        assert "shared_evidence_scope_versions" not in scanned, plan


def _seed_shared_evidence_versions(db, count: int = 50000) -> None:
    """Enough global shared evidence that a sequential scan is the cheaper plan
    whenever the primary key is not usable, and ``ANALYZE`` so the planner knows."""
    db.execute(
        text(
            """
            INSERT INTO shared_evidence_scope_versions (kind, fen, last_changed_epoch)
            SELECT 'raw', 'shared-fen-' || g, 9
            FROM generate_series(1, :count) AS g
            ON CONFLICT DO NOTHING
            """
        ),
        {"count": count},
    )
    db.execute(text("ANALYZE shared_evidence_scope_versions"))
    db.execute(text("ANALYZE opening_current_scope"))
    db.execute(text("ANALYZE opening_score_batch_shared_scope"))


def _seed_unrelated_position_rows(db, count: int = 5000) -> None:
    """Fill BOTH position tables for other owners, so a sequential scan is a plan
    the optimizer would visibly prefer if the join were unbounded."""
    db.execute(
        text(
            """
            INSERT INTO opening_current_positions
                (user_id, player_color, normalized_fen, in_book, has_evidence,
                 sample_size, game_count)
            SELECT g, 'black', 'fen-' || g, true, false, 0, 0
            FROM generate_series(100000, 100000 + :count) AS g
            """
        ),
        {"count": count},
    )
    batch_id = db.execute(
        text(
            """
            INSERT INTO opening_score_batches
                (user_id, player_color, generation, computed_at, storage_format)
            VALUES (999999, 'black', 1, now(), 'legacy')
            RETURNING id
            """
        )
    ).scalar_one()
    db.execute(
        text(
            """
            INSERT INTO opening_position_scores
                (batch_id, user_id, player_color, normalized_fen, in_book,
                 has_evidence, sample_size, game_count, computed_at)
            SELECT :batch, g, 'black', 'fen-' || g, true, false, 0, 0, now()
            FROM generate_series(100000, 100000 + :count) AS g
            """
        ),
        {"batch": batch_id, "count": count},
    )
    db.commit()
    db.execute(text("ANALYZE opening_current_positions"))
    db.execute(text("ANALYZE opening_position_scores"))
    db.commit()


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)
