#!/usr/bin/env python3
"""Adaptive, synthetic-only PostgreSQL storage spike for g-score-store-spike.

Run as ``python -m scripts.bench_opening_score_storage --output PATH`` from
backend with its venv active. See BENCH_OPENING_SCORE_STORAGE.md.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import re
import resource
import subprocess
import sys
import struct
import time
import tracemalloc
import uuid

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from scripts.opening_score_storage_adapters import (
    CurrentAdapter,
    LegacyAdapter,
    PayloadCache,
    retained_size,
)
from scripts.opening_score_storage_workload import (
    FIELDS,
    KEYS,
    MODELS,
    Payload,
    build_timeline,
    summarize_timeline,
    scale,
)

DATABASE_ENV = "GHOSTREPLAY_STORAGE_BENCH_DATABASE_URL"
CLUSTER_NAME = "ghostreplay-score-storage-spike"
APPLICATION_NAME = "ghostreplay-storage-spike"


def guard_url(raw):
    if not raw:
        raise ValueError(
            f"{DATABASE_ENV} must explicitly name an isolated synthetic database"
        )
    url = make_url(raw)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("only PostgreSQL/psycopg is supported")
    if url.host not in {"127.0.0.1", "localhost", "::1"} or url.query:
        raise ValueError(
            "destination must be explicit loopback without URL query overrides"
        )
    if not re.fullmatch(r"gr_score_spike_[a-z0-9_]+", url.database or ""):
        raise ValueError(
            "destination must be a newly created gr_score_spike_* synthetic database"
        )
    return url.set(drivername="postgresql+psycopg")


def engine_for(url, schema="public"):
    if not re.fullmatch(r"(public|ss_[a-z0-9_]+)", schema):
        raise ValueError("invalid private schema")
    return create_engine(
        url,
        connect_args={
            "application_name": APPLICATION_NAME,
            "options": f"-csearch_path={schema},public",
        },
        pool_size=2,
        max_overflow=0,
    )


def guard_database(engine):
    with engine.connect() as conn:
        if conn.execute(text("SHOW cluster_name")).scalar_one() != CLUSTER_NAME:
            raise ValueError(
                "cluster must explicitly identify itself as the isolated storage spike"
            )
        unexpected = conn.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') "
                "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'ss\\_%' ESCAPE '\\'"
            )
        ).scalar_one()
        if unexpected:
            raise ValueError(
                "database contains non-spike tables; retained/restored databases are forbidden"
            )
        if conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' "
                "AND pid<>pg_backend_pid() AND application_name<>:app"
            ),
            {"app": APPLICATION_NAME},
        ).scalar_one():
            raise ValueError(
                "unrelated cluster clients invalidate isolated WAL measurement"
            )
        settings = dict(
            conn.execute(
                text(
                    "SELECT name, setting FROM pg_settings WHERE name IN "
                    "('server_version','shared_buffers','wal_compression','full_page_writes','synchronous_commit',"
                    "'autovacuum','checkpoint_timeout','max_wal_size','block_size','lc_collate','cluster_name')"
                )
            ).all()
        )
        if settings["autovacuum"] != "off":
            raise ValueError(
                "controlled equal VACUUM schedule requires autovacuum=off on this isolated cluster"
            )
        if (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_extension WHERE extname IN ('pg_walinspect','pgstattuple')"
                )
            ).scalar_one()
            != 2
        ):
            raise ValueError(
                "install pg_walinspect and pgstattuple in the disposable database first"
            )
        return settings


def p95(values):
    return sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]


def stable_digest_cost_probe(payload):
    """Unpersisted canonical-encoding cost probe, only after a material screen.

    This is not a selected/versioned storage format and adds no schema or hash
    read path. It measures the proposed exact encoding cost before those exist.
    """

    def encode(value):
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("nonfinite semantic value")
            return ["float64", struct.pack(">d", value).hex()]
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("canonical timestamps require timezone")
            return [
                "utc",
                value.astimezone(timezone.utc).isoformat(timespec="microseconds"),
            ]
        return value

    envelope = ["spike-cost-probe-not-a-storage-format", 1, "black"]
    for name in MODELS:
        indices = [i for i, f in enumerate(FIELDS[name]) if f != "confidence"]
        key_indices = [FIELDS[name].index(f) for f in KEYS[name]]
        ordered = sorted(
            getattr(payload, name),
            key=lambda row: tuple(row[i].encode("utf-8") for i in key_indices),
        )
        envelope.append([name, [[encode(row[i]) for i in indices] for row in ordered]])
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).digest()


def rss_bytes():
    # ru_maxrss is the process high-water, not current retained memory.
    maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return maximum if sys.platform == "darwin" else maximum * 1024


class Trace:
    def __init__(self, engine):
        self.engine = engine
        self.shapes = {}
        self.read_bytes = 0
        self.read_queries = 0
        event.listen(engine, "after_cursor_execute", self.after)

    def reset(self):
        self.read_bytes = self.read_queries = 0

    def after(self, conn, cursor, statement, parameters, context, executemany):
        # No parameters or SQL text in output: report lengths/shapes only.
        normalized = re.sub(r"__\d+", "__N", statement)
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        relation = re.search(
            r"\b(?:INTO|UPDATE|FROM)\s+([a-z_]+)", statement, re.IGNORECASE
        )
        shape = self.shapes.setdefault(
            digest,
            {
                "verb": statement.split()[0],
                "relation": relation.group(1) if relation else None,
                "length": len(statement.encode()),
                "executemany": bool(executemany),
                "returning": "RETURNING" in statement.upper(),
                "calls": 0,
            },
        )
        shape["calls"] += 1
        result = cursor.pgresult
        if result is not None and result.ntuples:
            self.read_queries += 1
            self.read_bytes += sum(
                7
                + sum(
                    4 + len(result.get_value(r, c) or b"")
                    for c in range(result.nfields)
                )
                for r in range(result.ntuples)
            )

    def close(self):
        event.remove(self.engine, "after_cursor_execute", self.after)


def lsn(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_current_wal_insert_lsn()::text")
        ).scalar_one()


def wal_between(engine, start, end):
    with engine.connect() as conn:
        total = int(
            conn.execute(
                text("SELECT pg_wal_lsn_diff(:end, :start)"),
                {"start": start, "end": end},
            ).scalar_one()
        )
        # Classify whole records only when all block refs belong to one table.
        # Mixed/catalog/commit records remain explicit, never double-counted.
        locators = dict(
            conn.execute(
                text(
                    "SELECT c.relfilenode::text, COALESCE(parent.relname, c.relname) "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "LEFT JOIN pg_index i ON i.indexrelid=c.oid LEFT JOIN pg_class parent ON parent.oid=i.indrelid "
                    "WHERE n.nspname=current_schema() AND c.relfilenode<>0"
                )
            ).all()
        )
        records = conn.execute(
            text(
                "SELECT record_length, fpi_length, block_ref FROM pg_get_wal_records_info(:start, :end)"
            ),
            {"start": start, "end": end},
        )
        tables, fpi, record_total = Counter(), Counter(), 0
        for length, image_length, refs in records:
            names = {
                locators.get(n, "unattributed")
                for n in re.findall(r"rel \d+/\d+/(\d+)", refs or "")
            }
            name = next(iter(names)) if len(names) == 1 else "mixed_or_metadata"
            tables[name] += length
            fpi[name] += image_length
            record_total += length
    return {
        "total_bytes": total,
        "record_bytes_by_table": dict(tables),
        "fpi_bytes_by_table": dict(fpi),
        "record_bytes": record_total,
        "alignment_and_page_headers": total - record_total,
    }


def snapshot(adapter, *, vacuum=False):
    with adapter.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as conn:
        conn.execute(text("SELECT pg_stat_force_next_flush()"))
        if vacuum:
            for name in adapter.tables:
                conn.execute(text(f"VACUUM (ANALYZE) {name}"))
        conn.execute(text("SELECT pg_stat_clear_snapshot()"))
        relations = {}
        for name in adapter.tables:
            row = conn.execute(
                text(
                    "SELECT pg_relation_size(:name), pg_indexes_size(:name), pg_total_relation_size(:name)"
                ),
                {"name": name},
            ).one()
            tuples = conn.execute(
                text(
                    f"SELECT tuple_count, dead_tuple_count, free_space FROM pgstattuple('{name}')"
                )
            ).one()
            stats = conn.execute(
                text(
                    "SELECT n_tup_ins,n_tup_upd,n_tup_hot_upd,n_tup_del FROM pg_stat_user_tables WHERE schemaname=current_schema() AND relname=:name"
                ),
                {"name": name},
            ).one()
            relations[name] = {
                "heap_bytes": row[0],
                "index_bytes": row[1],
                "total_bytes": row[2],
                "toast_fsm_vm_bytes": row[2] - row[0] - row[1],
                "live_tuples": tuples[0],
                "dead_tuples": tuples[1],
                "free_bytes": tuples[2],
                "inserted": stats[0],
                "updated": stats[1],
                "hot_updated": stats[2],
                "deleted": stats[3],
            }
        return {
            "total_bytes": sum(r["total_bytes"] for r in relations.values()),
            "relations": relations,
        }


def make_adapter(url, run_id, name, layout, fillfactor, cached=False):
    schema = f"ss_{run_id}_{name}"
    admin = engine_for(url)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    admin.dispose()
    engine = engine_for(url, schema)
    adapter = (
        LegacyAdapter(engine)
        if layout == "A"
        else CurrentAdapter(engine, layout, fillfactor, cached=cached)
    )
    adapter.create()
    return adapter


def run_cell(adapter, sequence, *, cycles=None, cache_controls=False):
    trace = Trace(adapter.engine)
    try:
        # Two untimed publications give A its actual retained pair; B/D reuse one.
        adapter.publish(sequence[0])
        adapter.publish(sequence[0])
        adapter.verify(sequence[0])
        before = snapshot(adapter, vacuum=True)
        records, windows = [], []
        maintenance_wal_bytes = 0
        workload = (
            sequence[1:]
            if cycles is None
            else [sequence[1 + (i % (len(sequence) - 1))] for i in range(cycles)]
        )
        if cache_controls and adapter.cache:
            adapter.cache.evict()  # Natural process-cold load is a recorded miss.
        for i, candidate in enumerate(workload):
            control = "natural_cold" if cache_controls and i == 0 else None
            if cache_controls and i == len(workload) // 2:
                adapter.cache.evict()
                control = "forced_restart"
            if cache_controls and i == len(workload) - 2:
                # External publisher changes marker without changing payload.
                with adapter.engine.begin() as conn:
                    conn.execute(text("UPDATE marker SET id=DEFAULT"))
                control = "external_marker"
            post_checkpoint = i % 10 == 0
            if post_checkpoint:
                with adapter.engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT"
                ) as conn:
                    conn.execute(text("CHECKPOINT"))
            start_lsn = lsn(adapter.engine)
            trace.reset()
            adapter.publish(candidate)
            publication_bytes, publication_queries = (
                trace.read_bytes,
                trace.read_queries,
            )
            end_lsn = lsn(adapter.engine)
            record = dict(adapter.last_metrics)
            record.update(
                {
                    "reason": candidate.reason,
                    "period": candidate.period,
                    "cause": candidate.cause,
                    "post_checkpoint": post_checkpoint,
                    "control": control,
                    "protocol_datarow_bytes": publication_bytes,
                    "result_queries": publication_queries,
                    "rss_highwater_bytes": rss_bytes(),
                    "wal": wal_between(adapter.engine, start_lsn, end_lsn),
                }
            )
            # Oracle and bounded query are excluded from publication timing/WAL.
            adapter.verify(candidate)
            reads = [adapter.read(candidate) for _ in range(5)]
            record["bounded_read_ms"] = p95([r["bounded_read_ms"] for r in reads])
            record["bounded_rows"] = reads[0]["bounded_rows"]
            records.append(record)
            if (i + 1) % 10 == 0 or i == len(workload) - 1:
                pre_vacuum = snapshot(adapter)
                vacuum_start = lsn(adapter.engine)
                after_vacuum = snapshot(adapter, vacuum=True)
                vacuum_end = lsn(adapter.engine)
                maintenance = wal_between(adapter.engine, vacuum_start, vacuum_end)
                maintenance_wal_bytes += maintenance["total_bytes"]
                windows.append(
                    {
                        "cycle": i + 1,
                        "before_vacuum": pre_vacuum,
                        "after_vacuum": after_vacuum,
                        "vacuum_wal": maintenance,
                    }
                )
                print(
                    f"{adapter.layout}/{adapter.fillfactor} cache={bool(adapter.cache)} {i + 1}/{len(workload)}",
                    flush=True,
                )
        warm = [r for r in records if not r["control"]]
        by_reason = {}
        for reason in sorted({r["reason"] for r in records}):
            selected = [r for r in records if r["reason"] == reason]
            by_reason[reason] = {
                "count": len(selected),
                "cache_hits": sum(r["cache_hit"] for r in selected),
                "mean_read_bytes": sum(r["protocol_datarow_bytes"] for r in selected)
                / len(selected),
                "read_p95_ms": p95([r["read_ms"] for r in selected]),
            }
        return {
            "layout": adapter.layout,
            "fillfactor": adapter.fillfactor,
            "cached": bool(adapter.cache),
            "exact_parity": True,
            "records": records,
            "before": before,
            "windows": windows,
            "total_wal_bytes": sum(r["wal"]["total_bytes"] for r in records)
            + maintenance_wal_bytes,
            "publication_wal_bytes": sum(r["wal"]["total_bytes"] for r in records),
            "maintenance_wal_bytes": maintenance_wal_bytes,
            "warm_wal_bytes": sum(
                r["wal"]["total_bytes"] for r in records if not r["post_checkpoint"]
            ),
            "checkpoint_wal_bytes": sum(
                r["wal"]["total_bytes"] for r in records if r["post_checkpoint"]
            ),
            "vacuumed_bytes": windows[-1]["after_vacuum"]["total_bytes"],
            "publish_p95_ms": p95([r["publish_ms"] for r in warm]),
            "bounded_read_p95_ms": p95([r["bounded_read_ms"] for r in warm]),
            "bounded_read_max_ms": max(r["bounded_read_ms"] for r in records),
            "bytes_per_logical_row": windows[-1]["after_vacuum"]["total_bytes"]
            / sum(len(getattr(workload[-1].payload, n)) for n in MODELS),
            "read_by_reason": by_reason,
            "statements": list(trace.shapes.values()),
            "cache_limits": {
                "entries": adapter.cache.max_entries,
                "bytes": adapter.cache.max_bytes,
                "peak_retained_bytes": adapter.cache.peak_bytes,
                "evictions": adapter.cache.evictions,
            }
            if adapter.cache
            else None,
        }
    finally:
        trace.close()


def compare(reference, candidate):
    ratios = {
        key: candidate[key] / reference[key]
        for key in (
            "total_wal_bytes",
            "vacuumed_bytes",
            "publish_p95_ms",
            "bounded_read_p95_ms",
        )
    }
    gates = {
        "wal": ratios["total_wal_bytes"] <= 0.5,
        "footprint": ratios["vacuumed_bytes"] <= 1,
        "publication": ratios["publish_p95_ms"] <= 1.1,
        "bounded_read": ratios["bounded_read_p95_ms"] <= 1.1,
        "exact_parity": candidate["exact_parity"],
    }
    return {"ratios": ratios, "gates": gates, "passes": all(gates.values())}


def select_candidate(cells):
    # Stop rule: simpler already-passing B wins; no extra losing-layout tuning.
    for layout in ("B", "D"):
        passing = [
            (name, cell)
            for name, cell in cells.items()
            if cell["layout"] == layout and cell["comparison"]["passes"]
        ]
        if passing:
            # Choose physical setting by WAL. A paired cache is retained only
            # for >=5% warm publication improvement with all budgets passing.
            name, base = min(passing, key=lambda pair: pair[1]["total_wal_bytes"])
            if name.endswith("_cache"):
                name, base = (
                    name.removesuffix("_cache"),
                    cells[name.removesuffix("_cache")],
                )
            cached = cells.get(name + "_cache")
            if (
                cached
                and cached["comparison"]["passes"]
                and (
                    not base["comparison"]["passes"]
                    or cached["publish_p95_ms"] <= base["publish_p95_ms"] * 0.95
                )
            ):
                return name + "_cache"
            return name
    return None


def slow_reader(adapter, sequence):
    connection = adapter.engine.connect().execution_options(
        isolation_level="REPEATABLE READ"
    )
    transaction = connection.begin()
    table = "positions" if adapter.layout != "A" else "opening_position_scores"
    connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
    try:
        for candidate in sequence[-3:]:
            adapter.publish(candidate)
        held = snapshot(adapter, vacuum=True)
    finally:
        transaction.rollback()
        connection.close()
    released = snapshot(adapter, vacuum=True)
    return {
        "held": held,
        "released": released,
        "dead_tuples_held": sum(r["dead_tuples"] for r in held["relations"].values()),
        "dead_tuples_released": sum(
            r["dead_tuples"] for r in released["relations"].values()
        ),
    }


def memory_control(adapter, sequence):
    """Separate untimed allocation control; don't distort latency with tracing."""
    adapter.publish(sequence[-2])
    retained = retained_size((sequence[-2].payload, sequence[-1].payload))
    tracemalloc.start()
    try:
        adapter.publish(sequence[-1])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    result = {
        "two_candidate_payload_bytes": retained,
        "publication_new_allocations_peak_bytes": peak,
        "rss_highwater_bytes": rss_bytes(),
        "caveat": "RSS includes the pre-generated replay library and scorer; not an isolated production worker budget",
    }
    if adapter.cache and adapter.cache.entries:
        entry = next(iter(adapter.cache.entries.values()))
        result["representative_entry_bytes"] = entry.size
        result["capacity_pairs_estimate"] = min(8, (64 * 1024**2) // entry.size)
        result["binding_limit"] = (
            "bytes" if (64 * 1024**2) // entry.size < 8 else "entries"
        )
        # One declared capacity-pressure trace, not a layout/occupancy matrix.
        lru = PayloadCache(adapter.engine)
        hits = misses = 0
        for _ in range(4):
            for owner in range(1, 9):
                if lru.get(adapter.engine, owner, owner=owner):
                    hits += 1
                else:
                    misses += 1
                    lru.put(owner, entry.payload, entry.ids, owner=owner)
        result["eight_pair_round_robin_lru_control"] = {
            "accesses": 32,
            "hits": hits,
            "misses": misses,
            "evictions": lru.evictions,
            "retained_bytes": lru.bytes,
            "provenance": "synthetic cache-pressure control, not observed production residency; single-pair timed workload reported separately",
        }
    return result


def _memory_worker(url, run_id, layout, fillfactor, cached, candidates, pipe):
    """Spawned process receives only two candidates, not the whole replay library."""
    adapter = None
    try:
        adapter = make_adapter(url, run_id, "memory_worker", layout, fillfactor, cached)
        adapter.publish(candidates[0])
        adapter.publish(candidates[1])
        untraced_peak = rss_bytes()
        result = memory_control(adapter, candidates)
        result["untraced_persistence_worker_rss_highwater_bytes"] = untraced_peak
        result["caveat"] = (
            "Fresh spawned persistence worker with two candidates and graph/roots; "
            "excludes evidence capture/scorer allocations. Traced RSS includes profiler overhead."
        )
        pipe.send(result)
    except BaseException as exc:
        pipe.send({"error": type(exc).__name__})
    finally:
        if adapter:
            adapter.engine.dispose()
        pipe.close()


def isolated_memory_control(url, run_id, winner, sequence):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_memory_worker,
        args=(
            url,
            run_id,
            winner["layout"],
            winner["fillfactor"],
            winner["cached"],
            sequence[-2:],
            sender,
        ),
    )
    process.start()
    sender.close()
    try:
        result = receiver.recv()
    finally:
        receiver.close()
        process.join()
    if process.exitcode or "error" in result:
        raise RuntimeError("isolated memory control failed")
    return result


def hash_screen(cells, selected, events, sequence):
    winner = cells[selected]
    baseline = cells[selected.removesuffix("_cache")]
    steady = [
        e
        for e in events
        if e["period"] in ("active", "idle") and e["disposition"] == "rebuilt"
    ]
    eligible = [
        e["changes"]["stable_equal"] and not r["cache_hit"]
        for e, r in zip(steady, winner["records"], strict=True)
    ]
    mean_full_read = sum(r["read_ms"] for r in baseline["records"]) / len(steady)
    savings_upper_bound = sum(
        r["read_ms"]
        for hit, r in zip(eligible, baseline["records"], strict=True)
        if hit
    ) / len(steady)
    threshold = 0.05 * winner["publish_p95_ms"]
    result = {
        "eligible_fraction": sum(eligible) / len(steady),
        "stable_equal_fraction": sum(e["changes"]["stable_equal"] for e in steady)
        / len(steady),
        "zero_cost_savings_upper_bound_ms": savings_upper_bound,
        "material_threshold_ms": threshold,
        "mean_full_read_ms": mean_full_read,
        "assumption": "optimistic zero narrow-read cost; synthetic mix/residency only",
    }
    costs = []
    if savings_upper_bound >= threshold:
        # The cache paired trial, when required, has already finished. Only now
        # is an encoder CPU probe justified. No hash schema/read path is added.
        for candidate in sequence[1:]:
            start = time.perf_counter()
            stable_digest_cost_probe(candidate.payload)
            costs.append((time.perf_counter() - start) * 1000)
    mean_cost = sum(costs) / len(costs) if costs else 0
    result.update(
        {
            "encoder_cpu_probe_ms": costs,
            "mean_encoder_cpu_ms": mean_cost,
            "net_savings_upper_bound_ms": savings_upper_bound - mean_cost,
            "optimistic_break_even_eligible_fraction": (mean_cost + threshold)
            / mean_full_read,
            "screened_out": savings_upper_bound - mean_cost < threshold,
            "persisted_hash_format": None,
        }
    )
    return result


def write_report(path, report):
    temp = path.with_suffix(".partial.json")
    temp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=64)
    parser.add_argument("--rebuilds", type=int, default=20)
    parser.add_argument("--target-positions", type=int, default=20000)
    parser.add_argument("--qualification-cycles", type=int, default=100)
    args = parser.parse_args(argv)
    if (
        args.sessions < 4
        or args.rebuilds < 10
        or args.qualification_cycles != 100
        or args.target_positions < 1
    ):
        parser.error(
            "use >=4 sessions, >=10 rebuilds, and exactly 100 qualification cycles"
        )
    url = guard_url(os.environ.get(DATABASE_ENV))
    admin = engine_for(url)
    settings = guard_database(admin)
    run_id = uuid.uuid4().hex[:10]
    schema = f"ss_{run_id}_evidence"
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    evidence_engine = engine_for(url, schema)
    report = {
        "status": "running",
        "synthetic_only": True,
        "run_id": run_id,
        "environment": {
            "postgresql": settings,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
        },
        "method": {
            "transport": "bounded executemany, chunk=500",
            "vacuum_every": 10,
            "checkpoint_every": 10,
            "warmup_publications": 2,
            "read_bytes": "libpq text/binary DataRow bytes including per-field lengths; excludes RowDescription/TLS",
            "memory": "process ru_maxrss high-water; cache CPython deep-size estimate with per-entry shared-object deduplication",
            "production_mix": "unavailable; all frequencies synthetic",
            "hash_materiality_threshold": "at least 5% of publication p95 and greater than paired timing noise",
        },
        "source_sha256": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                Path(__file__),
                Path(__file__).with_name("opening_score_storage_workload.py"),
                Path(__file__).with_name("opening_score_storage_adapters.py"),
                Path("app/opening_cache.py"),
                Path("app/opening_evidence.py"),
                Path("app/opening_rootcalc.py"),
            ]
        },
        "cells": {},
        "skipped": [],
    }
    try:
        candidates, buckets, events = build_timeline(
            evidence_engine, sessions=args.sessions, repetitions=args.rebuilds
        )
        report["timeline"] = summarize_timeline(candidates, buckets, events)
        report["timeline"]["scorer_process_rss_highwater_before_replication_bytes"] = (
            rss_bytes()
        )
        # A single multiplier preserves field-change distribution and membership.
        copies = max(
            1, round(args.target_positions / len(candidates[0].payload.positions))
        )
        continuous = [
            scale(c, copies)
            for c in candidates
            if c.period in ("startup_control", "active", "idle")
        ]
        bucketed = [
            scale(c, copies)
            for c in buckets
            if c.period in ("startup_control", "active", "idle")
        ]
        report["persistence_fixture"] = {
            "kind": "real scorer payload"
            if copies == 1
            else "persistence-only replicas of real scorer payload",
            "copies": copies,
            "sizes": [
                {n: len(getattr(c.payload, n)) for n in MODELS} for c in continuous
            ],
        }
        write_report(args.output, report)
        for name, layout, ff in [
            ("a", "A", 100),
            ("b100", "B", 100),
            ("b50", "B", 50),
            ("d100", "D", 100),
            ("d50", "D", 50),
        ]:
            adapter = make_adapter(url, run_id, name, layout, ff)
            try:
                cell = run_cell(adapter, continuous)
                if layout != "A":
                    cell["comparison"] = compare(report["cells"]["a"], cell)
                report["cells"][name] = cell
                write_report(args.output, report)
            finally:
                adapter.engine.dispose()
        evidence_fraction = report["timeline"]["periods"]["steady"]["reasons"][
            "evidence_change"
        ]["fraction"]
        if evidence_fraction > 0.5:
            # Most promising measured setting: minimize failed gates, then WAL.
            name, base = min(
                [(n, c) for n, c in report["cells"].items() if n != "a"],
                key=lambda pair: (
                    sum(not v for v in pair[1]["comparison"]["gates"].values()),
                    pair[1]["total_wal_bytes"],
                    pair[1]["layout"] != "D",
                ),
            )
            adapter = make_adapter(
                url,
                run_id,
                name + "_cache",
                base["layout"],
                base["fillfactor"],
                cached=True,
            )
            try:
                cell = run_cell(adapter, continuous, cache_controls=True)
                cell["comparison"] = compare(report["cells"]["a"], cell)
                report["cells"][name + "_cache"] = cell
                report["cache_memory_control"] = memory_control(adapter, continuous)
                full_read_ms = sum(r["read_ms"] for r in base["records"]) / len(
                    base["records"]
                )
                accounting_ms = sum(
                    r["cache_accounting_ms"] for r in cell["records"]
                ) / len(cell["records"])
                hits = [r for r in cell["records"] if r["cache_hit"]]
                hit_read_ms = sum(r["read_ms"] for r in hits) / len(hits) if hits else 0
                report["cache_evaluation"] = {
                    "paired_baseline": name,
                    "publication_p95_ratio": cell["publish_p95_ms"]
                    / base["publish_p95_ms"],
                    "observed_single_pair_hit_fraction": len(hits)
                    / len(cell["records"]),
                    "mean_accounting_ms": accounting_ms,
                    "optimistic_break_even_hit_fraction": accounting_ms
                    / max(full_read_ms - hit_read_ms, 0.001),
                    "production_residency": "unknown; see separately labeled eight-pair LRU pressure control",
                }
            finally:
                adapter.engine.dispose()
            write_report(args.output, report)
        else:
            report["skipped"].append("memory cache: evidence rebuilds do not dominate")
        selected = select_candidate(report["cells"])
        report["selected"] = selected
        report["skipped"] += [
            "C: excluded by design",
            "losing-layout tuning: stop rule",
            "production telemetry: not required or available in this synthetic run",
        ]
        if any(
            c["layout"] == "D" and c["comparison"]["gates"]["wal"]
            for c in report["cells"].values()
        ):
            report["skipped"].append(
                "E: D meets WAL target; chunked confidence not eligible"
            )
        report["skipped"].append(
            "COPY/extra fillfactor/vacuum/100k sweeps: no measured concern justifying them"
        )
        if selected is None:
            report["status"] = "no_qualifying_candidate"
            report["skipped"].append(
                "qualification: no candidate clears all comparison gates; revised design/budgets require review"
            )
        else:
            winner = report["cells"][selected]
            report["hash_screen"] = hash_screen(
                report["cells"], selected, events, continuous
            )
            hash_negligible = report["hash_screen"]["screened_out"]
            report["skipped"].append(
                "hash: optimistic savings after measured encoder cost below material threshold"
                if hash_negligible
                else "hash: screen requires a paired trial before selection is final"
            )
            # Fixed key set is the union. Repeat captured values (explicit replay,
            # not a claim of 100 fresh real-scorer rebuilds) to qualify storage reuse.
            union = {n: {} for n in MODELS}
            from scripts.opening_score_storage_workload import key, sorted_rows

            for c in continuous:
                for n in MODELS:
                    union[n].update({key(n, row): row for row in getattr(c.payload, n)})
            fixed = []
            for c in continuous:
                groups = {}
                for n in MODELS:
                    rows = dict(union[n])
                    rows.update({key(n, row): row for row in getattr(c.payload, n)})
                    groups[n] = sorted_rows(n, rows.values())
                p = Payload(**groups)
                fixed.append(
                    replace(
                        c,
                        payload=p,
                        freshness=replace(
                            c.freshness,
                            shared_raw_fens=tuple(
                                r["fen"] for r in p.rows("scope") if r["kind"] == "raw"
                            ),
                            shared_norm_fens=tuple(
                                r["fen"] for r in p.rows("scope") if r["kind"] == "norm"
                            ),
                        ),
                    )
                )
            adapter = make_adapter(
                url,
                run_id,
                "qualification",
                winner["layout"],
                winner["fillfactor"],
                cached=winner["cached"],
            )
            try:
                report["qualification"] = run_cell(adapter, fixed, cycles=100)
                report["slow_reader"] = slow_reader(adapter, fixed)
                report["memory_control"] = memory_control(adapter, fixed)
                report["quantized_winner_control"] = run_cell(adapter, bucketed)
                with adapter.engine.connect() as conn:
                    report["selected_ordered_read_plan"] = conn.execute(
                        text(
                            "EXPLAIN (FORMAT JSON) SELECT id FROM positions "
                            "WHERE user_id=1 AND player_color='black' ORDER BY normalized_fen"
                        )
                    ).scalar_one()
            finally:
                adapter.engine.dispose()
            reference = make_adapter(url, run_id, "reference100", "A", 100)
            try:
                report["qualification_reference"] = run_cell(
                    reference, fixed, cycles=100
                )
            finally:
                reference.engine.dispose()
            q, a = report["qualification"], report["qualification_reference"]
            report["qualification_comparison"] = compare(a, q)
            report["isolated_memory_control"] = isolated_memory_control(
                url, run_id, winner, fixed
            )
            windows = [w["after_vacuum"]["total_bytes"] for w in q["windows"]]
            report["plateau"] = {
                "last_five_window_bytes": windows[-5:],
                "growth_fraction": (max(windows[-5:]) - min(windows[-5:]))
                / min(windows[-5:]),
                "passes": max(windows[-5:]) <= min(windows[-5:]) * 1.05,
            }
            counts = [
                {n: r["live_tuples"] for n, r in w["after_vacuum"]["relations"].items()}
                for w in q["windows"]
            ]
            report["plateau"]["fixed_live_counts"] = all(c == counts[0] for c in counts)
            report["plateau"]["passes"] &= report["plateau"]["fixed_live_counts"]
            report["reader_fallback_screen"] = {
                "measured_max_ms": q["bounded_read_max_ms"],
                "scheduler_quiet_window_ms": 1500,
                "frequency_sweep_required": q["bounded_read_max_ms"] >= 1500,
            }
            if q["bounded_read_max_ms"] < 1500:
                report["skipped"].append(
                    "fallback-frequency sweep: bounded reads stay below actual 1500ms scheduler quiet window"
                )
            report["proposed_release_budgets"] = {
                "combined_wal_bytes_per_100": a["total_wal_bytes"] * 0.5,
                "vacuumed_total_bytes": a["vacuumed_bytes"],
                "warm_publish_p95_ms": a["publish_p95_ms"] * 1.1,
                "bounded_read_p95_ms": a["bounded_read_p95_ms"] * 1.1,
                "fixed_set_last_five_vacuum_growth_fraction": 0.05,
                "cache_bytes": 64 * 1024**2 if winner["cached"] else 0,
                "cache_entries": 8 if winner["cached"] else 0,
                "persistence_worker_rss_highwater_bytes": math.ceil(
                    report["isolated_memory_control"][
                        "untraced_persistence_worker_rss_highwater_bytes"
                    ]
                    * 1.1
                ),
                "publication_new_allocation_peak_bytes": math.ceil(
                    report["isolated_memory_control"][
                        "publication_new_allocations_peak_bytes"
                    ]
                    * 1.1
                ),
                "approved": False,
            }
            report["status"] = (
                "awaiting_user_review"
                if (
                    report["qualification_comparison"]["passes"]
                    and report["plateau"]["passes"]
                    and report["slow_reader"]["dead_tuples_released"] == 0
                    and hash_negligible
                )
                else "qualification_failed"
            )
        if args.target_positions < 18000:
            report["status"] = "smoke_only"
        write_report(args.output, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "selected": report.get("selected"),
                    "output": str(args.output),
                }
            ),
            flush=True,
        )
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        write_report(args.output, report)
        raise
    finally:
        evidence_engine.dispose()
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
