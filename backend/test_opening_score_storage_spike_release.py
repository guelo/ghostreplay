"""Explicit release-seal checks for the disposable storage selection spike.

Pure checks also carry the marker so this experiment stays outside pre-push.
PostgreSQL checks require the same guarded synthetic cluster as the runbook.
"""

from dataclasses import replace
from datetime import timedelta
import os
import uuid

import pytest
from sqlalchemy import event, text

from scripts.bench_opening_score_storage import (
    Trace,
    compare,
    engine_for,
    guard_database,
    guard_url,
    hash_screen,
    make_adapter,
    select_candidate,
    stable_digest_cost_probe,
)
from scripts.opening_score_storage_adapters import PayloadCache, retained_size
from scripts.probe_opening_score_storage_sql import ActualStatementTrace
from scripts.opening_score_storage_workload import (
    Candidate,
    FIELDS,
    Payload,
    START,
    build_timeline,
    changed,
    scale,
    summarize_timeline,
)
from app.opening_cache import FreshnessSnapshot

pytestmark = pytest.mark.release_seal


def example():
    position = {
        "normalized_fen": "synthetic-a",
        "in_book": True,
        "has_evidence": True,
        "opening_score": 0.5,
        "confidence": 0.8,
        "coverage": 0.5,
        "weighted_depth": 4.0,
        "sample_size": 2,
        "game_count": 1,
        "last_practiced_at": START,
    }
    payload = Payload((), (tuple(position[f] for f in FIELDS["positions"]),), (), ())
    freshness = FreshnessSnapshot(None, 1, 0, (), (), "synthetic")
    return Candidate(
        payload, START, freshness, "evidence_change", "synthetic", "active"
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "sqlite:///:memory:",
        "postgresql://remote/gr_score_spike_safe",
        "postgresql://localhost/railway",
        "postgresql://localhost/gr_snap_base",
        "postgresql://localhost/postgres",
        "postgresql://localhost/gr_score_spike_x?host=remote",
        "postgresql://localhost/gr_score_spike_x?options=-csearch_path=public",
    ],
)
def test_destination_guard_refuses_unsafe_urls(url):
    with pytest.raises(ValueError):
        guard_url(url)


def test_explicit_local_synthetic_url_and_schema_guards():
    url = guard_url("postgresql://127.0.0.1:55439/gr_score_spike_unit")
    assert url.drivername == "postgresql+psycopg"
    with pytest.raises(ValueError):
        engine_for(url, "public; DROP SCHEMA public")


def test_exact_diff_covers_every_semantic_field_and_membership():
    c = example()
    assert changed(c.payload, c.payload)["stable_equal"]
    for i, field in enumerate(FIELDS["positions"]):
        row = list(c.payload.positions[0])
        row[i] = None if row[i] is not None else "changed"
        new = replace(c.payload, positions=(tuple(row),))
        result = changed(c.payload, new)
        assert result["stable_equal"] == (field == "confidence")
        if field != "normalized_fen":
            assert result["groups"]["positions"]["fields"][field] == 1
    assert not changed(c.payload, replace(c.payload, positions=()))["stable_equal"]
    assert not changed(c.payload, replace(c.payload, scope=(("synthetic-a", "raw"),)))[
        "stable_equal"
    ]


def test_replica_fixture_preserves_exact_values_and_labels_keys():
    c = example()
    enlarged = scale(c, 2)
    assert len(enlarged.payload.positions) == 2
    assert all("|replica:" in row[0] for row in enlarged.payload.positions)
    assert all(
        row[1:] == c.payload.positions[0][1:] for row in enlarged.payload.positions
    )
    assert enlarged.computed_at == c.computed_at


def test_unpersisted_hash_cost_probe_preserves_semantic_types_and_excludes_confidence():
    c = example()
    digest = stable_digest_cost_probe(c.payload)
    for i, field in enumerate(FIELDS["positions"]):
        row = list(c.payload.positions[0])
        if field == "normalized_fen":
            row[i] += "-changed"
        elif isinstance(row[i], bool):
            row[i] = not row[i]
        elif isinstance(row[i], (float, int)):
            row[i] += 0.00000000000001
        else:
            row[i] = None
        new = replace(c.payload, positions=(tuple(row),))
        assert (stable_digest_cost_probe(new) == digest) == (field == "confidence")
    row = list(c.payload.positions[0])
    row[FIELDS["positions"].index("opening_score")] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        stable_digest_cost_probe(replace(c.payload, positions=(tuple(row),)))


def test_cache_immutability_ids_engine_owner_format_bounds_and_delayed_completion():
    c, engine = example(), object()
    cache = PayloadCache(engine, max_entries=2, max_bytes=100000)
    ids = {"positions": {("synthetic-a",): 7}}
    cache.put(2, c.payload, ids)
    ids["positions"][("synthetic-a",)] = 99
    entry = cache.get(engine, 2)
    assert entry.ids["positions"][("synthetic-a",)] == 7
    with pytest.raises(TypeError):
        entry.ids["positions"][("synthetic-a",)] = 9
    cache.put(1, c.payload, {})
    assert cache.get(engine, 2) is entry
    assert cache.get(engine, 2, owner=99) is None
    assert cache.get(engine, 2, color="white") is None
    assert cache.get(engine, 2, format="future") is None
    with pytest.raises(ValueError):
        cache.get(object(), 2)
    cache.put(3, c.payload, {}, owner=2)
    cache.get(engine, 2)
    cache.put(4, c.payload, {}, owner=3)
    assert cache.get(engine, 3, owner=2) is None
    assert cache.evictions == 1
    assert cache.get(engine, 99) is None  # foreign marker invalidates
    assert cache.get(engine, 2) is None
    cache.max_bytes = 1
    cache.put(5, c.payload, {})
    assert cache.get(engine, 5) is None
    assert retained_size((c.payload, c.payload)) < 2 * retained_size(c.payload)


def cell(layout, wal=40, space=90, publish=100, read=100):
    return {
        "layout": layout,
        "total_wal_bytes": wal,
        "vacuumed_bytes": space,
        "publish_p95_ms": publish,
        "bounded_read_p95_ms": read,
        "exact_parity": True,
    }


def test_selection_requires_all_budgets_and_prefers_already_passing_simple_layout():
    a = cell("A", 100, 100, 100, 100)
    b, d = cell("B"), cell("D", 30)
    for c in (b, d):
        c["comparison"] = compare(a, c)
    assert select_candidate({"b": b, "d": d}) == "b"
    b["bounded_read_p95_ms"] = 111
    b["comparison"] = compare(a, b)
    assert select_candidate({"b": b, "d": d}) == "d"
    d["total_wal_bytes"] = 51
    d["comparison"] = compare(a, d)
    assert select_candidate({"b": b, "d": d}) is None
    for field, value in [
        ("vacuumed_bytes", 101),
        ("publish_p95_ms", 111),
        ("exact_parity", False),
    ]:
        broken = {**cell("B"), field: value}
        assert not compare(a, broken)["passes"]


def test_hash_screen_counts_only_exact_equal_residual_misses_and_charges_all_publications(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "scripts.bench_opening_score_storage.stable_digest_cost_probe",
        lambda payload: calls.append(payload),
    )
    events = [
        {
            "period": "active",
            "disposition": "rebuilt",
            "reason": "evidence_change",
            "changes": {"stable_equal": equal},
        }
        for equal in (True, True, False, False)
    ]
    records = [{"read_ms": 1000, "cache_hit": False} for _ in events]
    base = {"records": records, "publish_p95_ms": 1000}
    sequence = [example()] * 5
    result = hash_screen({"b": base}, "b", events, sequence)
    assert result["eligible_fraction"] == 0.5
    assert len(calls) == 4  # Encoder overhead is paid even on unequal output.
    assert not result["screened_out"]  # Caller must not declare this qualified.
    calls.clear()
    cached = {
        **base,
        "records": [{**r, "cache_hit": i < 2} for i, r in enumerate(records)],
    }
    result = hash_screen({"b": base, "b_cache": cached}, "b_cache", events, sequence)
    assert result["eligible_fraction"] == 0
    assert result["screened_out"]
    assert not calls  # Warm cache hits cannot justify a redundant hash.


@pytest.fixture
def pg_url():
    raw = os.environ.get("GHOSTREPLAY_STORAGE_BENCH_DATABASE_URL")
    if not raw:
        pytest.skip("explicit guarded storage benchmark URL required")
    url = guard_url(raw)
    engine = engine_for(url)
    try:
        guard_database(engine)
    finally:
        engine.dispose()
    return url


@pytest.mark.parametrize(
    "layout,ff", [("A", 100), ("B", 100), ("B", 50), ("D", 100), ("D", 50)]
)
def test_equal_replay_exact_confidence_membership_and_empty_scope(pg_url, layout, ff):
    adapter = make_adapter(
        pg_url, uuid.uuid4().hex[:10], "test", layout, ff, cached=layout != "A"
    )
    c = example()
    try:
        adapter.publish(c)
        adapter.verify(c)
        confidence_index = FIELDS["positions"].index("confidence")
        row = list(c.payload.positions[0])
        row[confidence_index] = 0.8000000000000002
        updated = replace(
            c,
            payload=replace(c.payload, positions=(tuple(row),)),
            computed_at=START + timedelta(minutes=1),
        )
        seen = []

        def record(conn, cursor, statement, params, context, many):
            seen.append(statement)

        event.listen(adapter.engine, "before_cursor_execute", record)
        adapter.publish(updated)
        event.remove(adapter.engine, "before_cursor_execute", record)
        if adapter.cache:
            assert adapter.last_metrics["cache_hit"]
            assert not any(
                s.lstrip().startswith("SELECT")
                and (
                    "FROM positions" in s
                    or "FROM edges" in s
                    or "FROM roots" in s
                    or "FROM scope" in s
                )
                for s in seen
            )
        adapter.verify(updated)
        empty = replace(c, payload=Payload((), (), (), ()))
        adapter.publish(empty)
        adapter.verify(empty)
        edge_values = {
            "parent_fen": "synthetic-a",
            "child_fen": "synthetic-b",
            "uci": "a2a3",
            "traversal_count": 1,
            "live_attempts": 1,
            "live_passes": 1,
            "live_fails": 0,
        }
        edge_only = replace(
            c,
            payload=Payload(
                (),
                (),
                (tuple(edge_values[f] for f in FIELDS["edges"]),),
                (("synthetic-a", "raw"),),
            ),
            freshness=replace(c.freshness, shared_raw_fens=("synthetic-a",)),
        )
        adapter.publish(edge_only)
        adapter.verify(
            edge_only
        )  # Valid score-empty publication still has edges/scope.
        adapter.publish(c)
        adapter.verify(c)
        if adapter.cache:
            with adapter.engine.begin() as conn:
                conn.execute(text("UPDATE marker SET id=DEFAULT"))
            adapter.publish(updated)
            assert not adapter.last_metrics["cache_hit"]
            adapter.verify(updated)
            with adapter.engine.connect() as conn:
                collations = (
                    conn.execute(
                        text(
                            "SELECT collation_name FROM information_schema.columns WHERE table_schema=current_schema() AND column_name IN ('normalized_fen','parent_fen','child_fen','opening_key','fen')"
                        )
                    )
                    .scalars()
                    .all()
                )
                assert collations and set(collations) == {"C"}
    finally:
        adapter.engine.dispose()


def test_real_gate_denominators_epoch_rearms_and_quantized_controls(pg_url):
    engine = engine_for(pg_url)
    schema = "ss_" + uuid.uuid4().hex[:10] + "_timeline"
    with engine.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine.dispose()
    fixture_engine = engine_for(pg_url, schema)
    try:
        real, bucket, events = build_timeline(
            fixture_engine, sessions=4, repetitions=10
        )
        report = summarize_timeline(real, bucket, events)
        steady = report["periods"]["steady"]
        assert steady["rebuilds"] == 10
        assert steady["reasons"]["evidence_change"]["count"] == 8
        assert steady["reasons"]["decay_staleness"]["count"] == 2
        assert steady["epoch_rearms"] == 10
        assert report["periods"]["startup_control"]["dispositions"]["no_evidence"] == 1
        assert report["periods"]["forced_control"]["rebuilds"] == 2
        assert report["quantized_control"]["shipping"] is False
        assert report["quantized_control"]["bucket_crossings"] > 0
        assert report["quantized_control"]["max_absolute_confidence_deviation"] > 0
        assert all(
            c.computed_at == q.computed_at and c.freshness == q.freshness
            for c, q in zip(real, bucket, strict=True)
        )
        idle = [e for e in events if e["reason"] == "decay_staleness"]
        assert all(e["changes"]["stable_equal"] for e in idle)
        assert all(
            e["changes"]["groups"]["positions"]["confidence_changed"] > 0 for e in idle
        )
    finally:
        fixture_engine.dispose()


def test_current_transaction_failure_preserves_payload_and_evicts_cache(pg_url):
    adapter = make_adapter(
        pg_url, uuid.uuid4().hex[:10], "rollback", "D", 50, cached=True
    )
    c = example()
    try:
        adapter.publish(c)

        def fail_marker(conn, cursor, statement, params, context, many):
            if statement.startswith("INSERT INTO marker"):
                raise RuntimeError("injected publication failure")

        event.listen(adapter.engine, "before_cursor_execute", fail_marker)
        with pytest.raises(RuntimeError, match="injected publication failure"):
            adapter.publish(replace(c, payload=Payload((), (), (), ())))
        event.remove(adapter.engine, "before_cursor_execute", fail_marker)
        assert not adapter.cache.entries
        adapter.verify(c)
        adapter.publish(c)
        assert not adapter.last_metrics["cache_hit"]
        adapter.verify(c)
    finally:
        adapter.engine.dispose()


def test_protocol_result_bytes_include_null_lengths_and_utf8(pg_url):
    engine = engine_for(pg_url)
    trace = Trace(engine)
    try:
        with engine.connect() as conn:
            trace.reset()
            conn.execute(text("SELECT 'é'::text, NULL::text, 123::int")).one()
            # type + message length + field count; three lengths; 2+0+3 bytes
            assert trace.read_bytes == 7 + 3 * 4 + 2 + 3
    finally:
        trace.close()
        engine.dispose()


def test_actual_statement_probe_observes_expanded_orm_pages(pg_url):
    adapter = make_adapter(pg_url, uuid.uuid4().hex[:10], "sql_text", "A", 100)
    candidate = example()
    fens = tuple(f"synthetic-{i:04d}" for i in range(1001))
    candidate = replace(
        candidate,
        payload=replace(candidate.payload, scope=tuple((fen, "raw") for fen in fens)),
        freshness=replace(candidate.freshness, shared_raw_fens=fens),
    )
    trace = ActualStatementTrace(adapter.engine)
    try:
        adapter.publish(candidate)
        adapter.verify(candidate)
        inserts = [
            s
            for s in trace.statements.values()
            if s["verb"] == "INSERT"
            and s["relation"] == "opening_score_batch_shared_scope"
        ]
        # An unexpanded after-event template is only hundreds of bytes; the
        # actual 1000-row page must be observed here without storing its text.
        assert max(s["sql_bytes"] for s in inserts) > 10000
        assert all(s["returning"] for s in inserts)
        assert all("parameters" not in s and "sql" not in s for s in inserts)
    finally:
        trace.close()
        adapter.engine.dispose()


def test_cache_byte_bound_evicts_before_entry_count_limit():
    engine, payload = object(), example().payload
    cache = PayloadCache(engine)
    cache.put(1, payload, {})
    one_size = cache.bytes
    cache.max_bytes = one_size * 2
    cache.put(2, payload, {}, owner=2)
    cache.put(3, payload, {}, owner=3)
    assert cache.bytes <= cache.max_bytes
    assert len(cache.entries) <= 2
    assert cache.evictions >= 1


def test_postcommit_cache_failure_is_only_a_cold_miss(pg_url, monkeypatch):
    adapter = make_adapter(
        pg_url, uuid.uuid4().hex[:10], "cache_failure", "B", 100, cached=True
    )

    def fail(self, *args, **kwargs):
        raise MemoryError("injected cache retention failure")

    monkeypatch.setattr(PayloadCache, "put", fail)
    try:
        candidate = example()
        adapter.publish(candidate)
        adapter.verify(candidate)
        assert adapter.last_metrics["cache_retention_failed"]
        assert not adapter.cache.entries
    finally:
        adapter.engine.dispose()


@pytest.mark.parametrize("layout", ["A", "B", "D"])
def test_bounded_read_timer_excludes_fixture_key_selection(pg_url, monkeypatch, layout):
    adapter = make_adapter(pg_url, uuid.uuid4().hex[:10], "read_boundary", layout, 100)
    clock = {"now": 0.0}

    class CostlyFixtureEdges(tuple):
        def __iter__(self):
            clock["now"] += 1.0
            return super().__iter__()

    candidate = example()
    try:
        adapter.publish(candidate)
        candidate = replace(
            candidate, payload=replace(candidate.payload, edges=CostlyFixtureEdges())
        )
        with monkeypatch.context() as patcher:
            patcher.setattr(
                "scripts.opening_score_storage_adapters.time.perf_counter",
                lambda: clock["now"],
            )
            measured = adapter.read(candidate)
        assert clock["now"] == 1.0
        assert measured["bounded_read_ms"] == 0.0
        assert measured["bounded_rows"] == 1
    finally:
        adapter.engine.dispose()
