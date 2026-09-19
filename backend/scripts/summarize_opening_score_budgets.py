"""Attach reproducible paired-block uncertainty and review gates to a rerun."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random

from scripts.bench_opening_score_storage import p95, write_report
from scripts.remeasure_opening_score_budgets import MIB, rounded_headroom


def add_fixed_wal_profile(report):
    """Do not grant growing-membership maintenance allowance to fixed-set runs."""
    budgets = report["proposed_release_budgets"]
    b = report["fixed100"]["b50"]["summary"]
    anchors = {
        "warm_publication_wal_bytes": b["warm_publication_wal_bytes"]["p95"],
        "post_checkpoint_publication_wal_bytes": b[
            "post_checkpoint_publication_wal_bytes"
        ]["p95"],
        "vacuum_wal_bytes_per_ten_publications": b["vacuum_wal_bytes_per_ten"]["max"],
    }
    budgets["selected_anchors"]["fixed_working_set_wal"] = anchors
    budgets["selected_design_ceilings"]["fixed_working_set_wal"] = {
        name: rounded_headroom(value, 1.5, MIB / 4) for name, value in anchors.items()
    }
    budgets["wal_profile_rule"] = (
        "Apply fixed_working_set_wal limits to fixed membership; top-level WAL ceilings cover growing membership. "
        "Use the matching profile in both per-state checks and count-weighted total. Do not apply growing allowances to fixed-set qualification."
    )


def paired_p95_ratio(reference, selected, field, *, seed=20260919, repetitions=4000):
    """Resample matched ten-publication blocks, retaining correlation within them.

    These are descriptive percentile intervals for this local synthetic run,
    not confidence bounds on production or a proof of host isolation.
    """
    if len(reference) != len(selected) or len(reference) % 10 or len(reference) < 20:
        raise ValueError("requires at least two complete paired ten-publication blocks")

    def samples(records):
        return [
            x
            for record in records
            for x in (
                record[field] if isinstance(record[field], list) else [record[field]]
            )
        ]

    ablocks = [samples(reference[i : i + 10]) for i in range(0, len(reference), 10)]
    bblocks = [samples(selected[i : i + 10]) for i in range(0, len(selected), 10)]
    rng = random.Random(seed)
    ratios = []
    for _ in range(repetitions):
        indices = [rng.randrange(len(ablocks)) for _ in ablocks]
        av = [x for i in indices for x in ablocks[i]]
        bv = [x for i in indices for x in bblocks[i]]
        ratios.append(p95(bv) / p95(av))
    ratios.sort()
    lower, upper = (
        ratios[int(repetitions * 0.025)],
        ratios[int(repetitions * 0.975) - 1],
    )
    return {
        "ratio": p95(samples(selected)) / p95(samples(reference)),
        "paired_block_percentile_95_interval": [lower, upper],
        "paired_blocks": len(ablocks),
        "bootstrap_repetitions": repetitions,
        "seed": seed,
        "relative_1_1_gate": "pass"
        if upper <= 1.1
        else "fail"
        if lower > 1.1
        else "inconclusive",
    }


def review_analysis(report):
    fixed = report["fixed100"]
    a, b = fixed["a"]["summary"], fixed["b50"]["summary"]
    limits = report["proposed_release_budgets"]["selected_design_ceilings"]
    result = {
        "publication_comparison": paired_p95_ratio(
            fixed["a"]["records"], fixed["b50"]["records"], "publish_ms"
        ),
        "read_comparison": paired_p95_ratio(
            fixed["a"]["records"], fixed["b50"]["records"], "bounded_read_samples_ms"
        ),
        "a_block_median_max_over_min": max(a["block_publication_medians_ms"])
        / min(a["block_publication_medians_ms"]),
        "a_publication_max_over_median": a["publication_ms"]["max"]
        / a["publication_ms"]["median"],
        "no_samples_removed": True,
        "relative_comparisons": {},
        "absolute_wal_envelopes": {},
        "absolute_checks": {
            "publication": b["publication_ms"]["p95"]
            <= limits["local_publication_p95_ms"],
            "bounded_read": b["bounded_read_ms"]["p95"]
            <= limits["local_bounded_read_p95_ms"],
            "plateau": b["fixed_live_counts"]
            and b["last_five_footprint_growth"]
            <= limits["last_five_vacuum_growth_fraction"],
        },
    }
    for name in ["fixed100", "checkpoint_each20"]:
        acell, bcell = report[name]["a"], report[name]["b50"]
        sa, sb = acell["summary"], bcell["summary"]
        wal_limits = (
            limits.get("fixed_working_set_wal", limits)
            if name == "fixed100"
            else limits
        )
        warm_count = sum(not r["post_checkpoint"] for r in bcell["records"])
        post_count = len(bcell["records"]) - warm_count
        vacuum_count = len(bcell["windows"])
        combined_ceiling = (
            warm_count * wal_limits["warm_publication_wal_bytes"]
            + post_count * wal_limits["post_checkpoint_publication_wal_bytes"]
            + vacuum_count * wal_limits["vacuum_wal_bytes_per_ten_publications"]
        )
        result["absolute_wal_envelopes"][name] = {
            "warm_publications": warm_count,
            "post_checkpoint_publications": post_count,
            "ten_publication_vacuum_windows": vacuum_count,
            "ceiling_bytes": combined_ceiling,
            "measured_bytes": sb["combined_wal_bytes"],
        }
        result["absolute_checks"][name + "_combined_wal"] = (
            sb["combined_wal_bytes"] <= combined_ceiling
        )
        ratios = {
            "combined_wal": sb["combined_wal_bytes"] / sa["combined_wal_bytes"],
            "footprint": sb["vacuumed_bytes"] / sa["vacuumed_bytes"],
        }
        result["relative_comparisons"][name] = {
            **ratios,
            "passes": ratios["combined_wal"] <= 0.5 and ratios["footprint"] <= 1,
            "parity": acell["exact_parity"] and bcell["exact_parity"],
        }
        for state in ["warm", "post_checkpoint"]:
            key = state + "_publication_wal_bytes"
            if key in sb:
                result["absolute_checks"][name + "_" + key] = (
                    sb[key]["p95"] <= wal_limits[key]
                )
        result["absolute_checks"][name + "_footprint"] = (
            sb["vacuumed_bytes"] <= limits["vacuumed_total_bytes"]
        )
        result["absolute_checks"][name + "_vacuum_wal"] = (
            sb["vacuum_wal_bytes_per_ten"]["max"]
            <= wal_limits["vacuum_wal_bytes_per_ten_publications"]
        )
    for metric, ceiling in [
        (
            "untraced_persistence_worker_rss_highwater_bytes",
            "isolated_persistence_worker_rss_bytes",
        ),
        ("publication_new_allocations_peak_bytes", "publication_allocation_peak_bytes"),
    ]:
        result["absolute_checks"][ceiling] = all(
            x[metric] <= limits[ceiling] for x in report["memory_repeats"]
        )
    result["local_gates_pass"] = (
        all(result["absolute_checks"].values())
        and all(
            v["passes"] and v["parity"] for v in result["relative_comparisons"].values()
        )
        and result["publication_comparison"]["relative_1_1_gate"] == "pass"
        and result["read_comparison"]["relative_1_1_gate"] == "pass"
    )
    result["network_qualification"] = (
        "pending; actual deployment path required before final latency ceilings/activation"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--original", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = json.loads(args.input.read_text())
    if (
        report["status"] != "awaiting_budget_review"
        or report["synthetic_only"] is not True
    ):
        raise ValueError("requires a completed synthetic budget rerun")
    if (
        hashlib.sha256(args.original.read_bytes()).hexdigest()
        != report["original_sha256"]
    ):
        raise ValueError("original artifact does not match rerun provenance")
    original = json.loads(args.original.read_text())
    timeline_fields = [
        "actual_sizes",
        "start",
        "end",
        "events",
        "periods",
        "provenance",
        "quantized_control",
        "seed",
    ]
    for field in timeline_fields:
        if report["timeline"][field] != original["timeline"][field]:
            raise ValueError(f"deterministic timeline changed: {field}")
    report["deterministic_timeline_fields_match"] = timeline_fields
    root = Path(__file__).resolve().parents[2]
    for path, digest in report["source_sha256"].items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
            raise ValueError(f"measured source changed: {path}")
    report["raw_rerun_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
    report["source_sha256"][str(Path(__file__).resolve().relative_to(root))] = (
        hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )
    add_fixed_wal_profile(report)
    report["review_analysis"] = review_analysis(report)
    write_report(args.output, report)
    print(json.dumps(report["review_analysis"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
