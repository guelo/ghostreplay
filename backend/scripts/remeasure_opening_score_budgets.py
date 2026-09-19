"""Review-driven A/B50 budget measurements; preserves the sealed selection run.

Uses the same synthetic guards and adapters. No production access or changes.
See BENCH_OPENING_SCORE_STORAGE.md for method and interpretation.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import time
import uuid

from sqlalchemy import text

from scripts.bench_opening_score_storage import (
    DATABASE_ENV,
    Trace,
    engine_for,
    guard_database,
    guard_url,
    isolated_memory_control,
    lsn,
    make_adapter,
    p95,
    snapshot,
    wal_between,
    write_report,
)
from scripts.opening_score_storage_workload import (
    MODELS,
    Payload,
    build_timeline,
    key,
    scale,
    sorted_rows,
    summarize_timeline,
)

MIB = 1024**2


def distribution(values):
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "p95": p95(values),
        "max": max(values),
        "mean": statistics.mean(values),
    }


def fixed_membership(sequence):
    union = {name: {} for name in MODELS}
    for candidate in sequence:
        for name in MODELS:
            union[name].update(
                {key(name, row): row for row in getattr(candidate.payload, name)}
            )
    result = []
    for candidate in sequence:
        groups = {}
        for name in MODELS:
            rows = dict(union[name])
            rows.update(
                {key(name, row): row for row in getattr(candidate.payload, name)}
            )
            groups[name] = sorted_rows(name, rows.values())
        payload = Payload(**groups)
        result.append(
            replace(
                candidate,
                payload=payload,
                freshness=replace(
                    candidate.freshness,
                    shared_raw_fens=tuple(
                        r["fen"] for r in payload.rows("scope") if r["kind"] == "raw"
                    ),
                    shared_norm_fens=tuple(
                        r["fen"] for r in payload.rows("scope") if r["kind"] == "norm"
                    ),
                ),
            )
        )
    return result


def cell_summary(cell):
    records = cell["records"]
    reads = [x for r in records for x in r["bounded_read_samples_ms"]]
    windows = cell["windows"]
    result = {
        "publication_ms": distribution([r["publish_ms"] for r in records]),
        "bounded_read_ms": distribution(reads),
        "publication_datarow_bytes": distribution(
            [r["protocol_datarow_bytes"] for r in records]
        ),
        "publication_cpu_ms": distribution([r["client_cpu_ms"] for r in records]),
        "vacuum_wal_bytes_per_ten": distribution(
            [w["vacuum_wal"]["total_bytes"] for w in windows]
        ),
        "combined_wal_bytes": sum(r["wal"]["total_bytes"] for r in records)
        + sum(w["vacuum_wal"]["total_bytes"] for w in windows),
        "vacuumed_bytes": windows[-1]["after_vacuum"]["total_bytes"],
        "block_publication_medians_ms": [
            statistics.median([r["publish_ms"] for r in records[i : i + 10]])
            for i in range(0, len(records), 10)
        ],
        "block_read_p95_ms": [
            p95([x for r in records[i : i + 10] for x in r["bounded_read_samples_ms"]])
            for i in range(0, len(records), 10)
        ],
    }
    for checkpoint, label in [(False, "warm"), (True, "post_checkpoint")]:
        selected = [r for r in records if r["post_checkpoint"] == checkpoint]
        if selected:
            result[label + "_publication_wal_bytes"] = distribution(
                [r["wal"]["total_bytes"] for r in selected]
            )
    last = [w["after_vacuum"]["total_bytes"] for w in windows[-5:]]
    counts = [
        {n: v["live_tuples"] for n, v in w["after_vacuum"]["relations"].items()}
        for w in windows
    ]
    result["last_five_footprint_growth"] = (max(last) - min(last)) / min(last)
    result["fixed_live_counts"] = all(c == counts[0] for c in counts)
    return result


def measure_block(adapter, trace, candidates, cell, *, every_checkpoint):
    for i, candidate in enumerate(candidates):
        checkpoint = every_checkpoint or i == 0
        if checkpoint:
            with adapter.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as conn:
                conn.execute(text("CHECKPOINT"))
        start_lsn = lsn(adapter.engine)
        trace.reset()
        cpu_start = time.process_time()
        wall_start = time.time()
        adapter.publish(candidate)
        cpu_ms = (time.process_time() - cpu_start) * 1000
        byte_count, queries = trace.read_bytes, trace.read_queries
        end_lsn = lsn(adapter.engine)
        record = dict(adapter.last_metrics)
        record.update(
            {
                "cycle": len(cell["records"]) + 1,
                "reason": candidate.reason,
                "post_checkpoint": checkpoint,
                "started_unix_seconds": wall_start,
                "client_cpu_ms": cpu_ms,
                "host_load_1_5_15": os.getloadavg(),
                "protocol_datarow_bytes": byte_count,
                "result_queries": queries,
                "wal": wal_between(adapter.engine, start_lsn, end_lsn),
            }
        )
        # Exact parity outside publication time/WAL. Keep every read, no max-of-five.
        adapter.verify(candidate)
        record["bounded_read_samples_ms"] = [
            adapter.read(candidate)["bounded_read_ms"] for _ in range(5)
        ]
        cell["records"].append(record)
    before = snapshot(adapter)
    start = lsn(adapter.engine)
    after = snapshot(adapter, vacuum=True)
    end = lsn(adapter.engine)
    cell["windows"].append(
        {
            "cycle": len(cell["records"]),
            "before_vacuum": before,
            "after_vacuum": after,
            "vacuum_wal": wal_between(adapter.engine, start, end),
        }
    )
    cell["summary"] = cell_summary(cell)
    cell["exact_parity"] = True
    print(
        json.dumps(
            {
                "layout": adapter.layout,
                "cycles": len(cell["records"]),
                "checkpoint_each": every_checkpoint,
                "last_block_publish_median_ms": cell["summary"][
                    "block_publication_medians_ms"
                ][-1],
            }
        ),
        flush=True,
    )


def paired_phase(url, run_id, sequence, cycles, every_checkpoint, output, report, name):
    adapters, traces = {}, {}
    cells = report[name] = {}
    try:
        for label, layout, ff in [("a", "A", 100), ("b50", "B", 50)]:
            adapter = adapters[label] = make_adapter(
                url, run_id, name + "_" + label, layout, ff
            )
            adapter.publish(sequence[0])
            adapter.publish(sequence[0])
            adapter.verify(sequence[0])
            cells[label] = {
                "records": [],
                "windows": [],
                "before": snapshot(adapter, vacuum=True),
            }
            traces[label] = Trace(adapter.engine)
        for block in range(cycles // 10):
            candidates = [
                sequence[1 + (i % (len(sequence) - 1))]
                for i in range(block * 10, (block + 1) * 10)
            ]
            order = ["a", "b50"] if block % 2 == 0 else ["b50", "a"]
            for label in order:
                measure_block(
                    adapters[label],
                    traces[label],
                    candidates,
                    cells[label],
                    every_checkpoint=every_checkpoint,
                )
                write_report(output, report)
        return cells
    finally:
        for trace in traces.values():
            trace.close()
        for adapter in adapters.values():
            adapter.engine.dispose()


def rounded_headroom(value, factor, unit=1):
    return math.ceil(value * factor / unit) * unit


def derive_budgets(report, original):
    """Keep independent A-relative floors and B50 ceilings; do not approve them."""
    fixed = report["fixed100"]
    checkpoint = report["checkpoint_each20"]
    a, b = fixed["a"]["summary"], fixed["b50"]["summary"]
    representative = original["cells"]["b50"]
    warm_values = [
        r["wal"]["total_bytes"]
        for r in representative["records"]
        if not r["post_checkpoint"]
    ]
    warm_anchor = max(p95(warm_values), b["warm_publication_wal_bytes"]["p95"])
    checkpoint_anchor = max(
        b["post_checkpoint_publication_wal_bytes"]["p95"],
        checkpoint["b50"]["summary"]["post_checkpoint_publication_wal_bytes"]["p95"],
    )
    vacuum_anchor = max(
        b["vacuum_wal_bytes_per_ten"]["max"],
        checkpoint["b50"]["summary"]["vacuum_wal_bytes_per_ten"]["max"],
        max(w["vacuum_wal"]["total_bytes"] for w in representative["windows"]),
    )
    footprint_anchor = max(
        b["vacuumed_bytes"],
        checkpoint["b50"]["summary"]["vacuumed_bytes"],
        representative["vacuumed_bytes"],
    )
    rss = max(
        x["untraced_persistence_worker_rss_highwater_bytes"]
        for x in report["memory_repeats"]
    )
    allocations = max(
        x["publication_new_allocations_peak_bytes"] for x in report["memory_repeats"]
    )
    return {
        "approved": False,
        "selection_approved": "b50",
        "scope": "same synthetic workload size; local latency provisional until real network qualification",
        "a_relative_minimum_improvement": {
            "combined_wal_ratio_max": 0.5,
            "vacuumed_footprint_ratio_max": 1.0,
            "publication_p95_ratio_max": 1.1,
            "bounded_read_p95_ratio_max": 1.1,
        },
        "selected_design_ceilings": {
            "warm_publication_wal_bytes": rounded_headroom(warm_anchor, 1.5, MIB / 4),
            "post_checkpoint_publication_wal_bytes": rounded_headroom(
                checkpoint_anchor, 1.5, MIB / 4
            ),
            "vacuum_wal_bytes_per_ten_publications": rounded_headroom(
                vacuum_anchor, 1.5, MIB / 4
            ),
            "vacuumed_total_bytes": rounded_headroom(footprint_anchor, 1.5, MIB),
            "local_publication_p95_ms": rounded_headroom(
                b["publication_ms"]["p95"], 1.5, 100
            ),
            "local_bounded_read_p95_ms": rounded_headroom(
                b["bounded_read_ms"]["p95"], 2.0
            ),
            "isolated_persistence_worker_rss_bytes": rounded_headroom(
                rss, 1.5, 10 * MIB
            ),
            "publication_allocation_peak_bytes": rounded_headroom(
                allocations, 1.5, 5 * MIB
            ),
            "last_five_vacuum_growth_fraction": 0.05,
            "cache_entries": 0,
            "cache_bytes": 0,
        },
        "selected_anchors": {
            "warm_publication_wal_p95_bytes": warm_anchor,
            "post_checkpoint_publication_wal_p95_bytes": checkpoint_anchor,
            "vacuum_wal_max_bytes_per_ten": vacuum_anchor,
            "vacuumed_total_bytes": footprint_anchor,
            "publication_p95_ms": b["publication_ms"]["p95"],
            "individual_bounded_read_p95_ms": b["bounded_read_ms"]["p95"],
            "memory_rss_max_bytes": rss,
            "allocation_max_bytes": allocations,
        },
        "clean_a_reference": {
            "publication_p95_ms": a["publication_ms"]["p95"],
            "individual_bounded_read_p95_ms": a["bounded_read_ms"]["p95"],
        },
        "cadence_combined_wal_ceiling": "Nwarm * warm ceiling + Npostcheckpoint * postcheckpoint ceiling + Nvacuum10 * vacuum ceiling; separately require <=0.5 of matched A",
        "wal_evaluation": "compare per-state p95 and count-weighted total plus measured maintenance; do not compare unlike checkpoint or vacuum schedules",
        "latency_finalization": "remeasure A and B50 over actual application-to-database network path; retain protocol bytes and RTT/throughput; review ceilings before activation",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    original = json.loads(args.original.read_text())
    root = Path(__file__).resolve().parents[2]
    # Preserve the previous measurement and prohibit silent writer/scorer drift.
    for path, expected in original["source_sha256"].items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"sealed source changed: {path}")
    url = guard_url(os.environ.get(DATABASE_ENV))
    admin = engine_for(url)
    run_id = uuid.uuid4().hex[:10]
    evidence = None
    sources = dict(original["source_sha256"])
    sources[str(Path(__file__).resolve().relative_to(root))] = hashlib.sha256(
        Path(__file__).read_bytes()
    ).hexdigest()
    # Models may be edited elsewhere in this workspace; pin all loaded authorities too.
    for path in [
        "backend/app/models.py",
        "backend/app/opening_aggregate.py",
        "backend/app/opening_graph.py",
    ]:
        if (root / path).exists():
            sources[path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    report = {
        "status": "running",
        "synthetic_only": True,
        "run_id": run_id,
        "original_sha256": hashlib.sha256(args.original.read_bytes()).hexdigest(),
        "source_sha256": sources,
        "environment": {
            "postgresql": guard_database(admin),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "method": {
            "fixed_cycles_per_layout": 100,
            "block_size": 10,
            "order": "AB then BA alternating",
            "bounded_reads": "all five individual reads per publication, pooled and per-block; no nested maxima",
            "checkpoint_control_cycles_per_layout": 20,
            "memory_fresh_process_repetitions": 5,
            "no_outlier_removal": True,
            "vacuum_every": 10,
            "CPU_timing": "whole adapter call, includes A fixture preparation; publish_ms excludes it as in sealed run",
        },
    }
    try:
        schema = f"ss_{run_id}_evidence"
        with admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        evidence = engine_for(url, schema)
        candidates, buckets, events = build_timeline(
            evidence, sessions=64, repetitions=20
        )
        timeline = summarize_timeline(candidates, buckets, events)
        assert (
            timeline["periods"]["steady"] == original["timeline"]["periods"]["steady"]
        )
        assert timeline["actual_sizes"] == original["timeline"]["actual_sizes"]
        report["timeline"] = timeline
        sequence = [
            scale(c, original["persistence_fixture"]["copies"])
            for c in candidates
            if c.period in ("startup_control", "active", "idle")
        ]
        fixed = fixed_membership(sequence)
        report["fixture_sizes"] = [
            {n: len(getattr(c.payload, n)) for n in MODELS} for c in sequence
        ]
        assert report["fixture_sizes"] == original["persistence_fixture"]["sizes"]
        write_report(args.output, report)
        paired_phase(url, run_id, fixed, 100, False, args.output, report, "fixed100")
        paired_phase(
            url, run_id, sequence, 20, True, args.output, report, "checkpoint_each20"
        )
        report["memory_repeats"] = []
        for i in range(5):
            report["memory_repeats"].append(
                isolated_memory_control(
                    url,
                    run_id + str(i),
                    {"layout": "B", "fillfactor": 50, "cached": False},
                    fixed,
                )
            )
            write_report(args.output, report)
            print(f"Fresh memory worker {i + 1}/5 complete", flush=True)
        report["proposed_release_budgets"] = derive_budgets(report, original)
        for path, expected in sources.items():
            assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, (
                f"source changed during run: {path}"
            )
        report["status"] = "awaiting_budget_review"
        write_report(args.output, report)
        print(
            json.dumps({"status": report["status"], "output": str(args.output)}),
            flush=True,
        )
    except BaseException as exc:
        report["status"] = "failed"
        report["failure_type"] = type(exc).__name__
        write_report(args.output, report)
        raise
    finally:
        if evidence is not None:
            evidence.dispose()
        admin.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
