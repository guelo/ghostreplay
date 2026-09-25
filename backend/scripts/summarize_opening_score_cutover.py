"""Evaluate sealed Railway cutover cells without pooling them with local runs.

Outputs stay private. Passing relative gates only produces proposed, size-specific
absolute ceilings for review; it never authorizes production activation.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path

from scripts.qualify_opening_score_storage import assert_private_store
from scripts import summarize_opening_score_qualification as qualification
from scripts.summarize_opening_score_qualification import evaluate_cell, settings_homogeneity


def shape_checks(cells, qualification_report):
    fits = qualification_report["ceilings"]["ceilings"]
    metric = "post_checkpoint_vacuum_wal_bytes_per_ten_publications"
    # Rebuild from the sealed evaluated cells using the qualification builder,
    # without editing or resealing the historical qualification report.
    vacuum_fit = qualification.build_ceilings(qualification_report["cells"], [])["ceilings"][metric]
    checks = []
    for name, cell in cells.items():
        metrics = {
            ("warm_publication_wal_bytes" if name == "C1" else "post_checkpoint_publication_wal_bytes"):
                cell["distributions"]["B50"]["publication_wal_bytes"]["p95"],
            "vacuum_wal_bytes_per_ten_publications": cell["vacuum_wal_max_bytes_by_layout"]["B50"],
            "vacuumed_footprint_bytes": cell["vacuumed_footprint_bytes"],
        }
        if name == "C2":
            metrics[metric] = cell["vacuum_wal_max_bytes_by_layout"]["B50"]
        for key, measured in metrics.items():
            fit = vacuum_fit if key == metric else fits[key]
            _, factor, unit = qualification.PRODUCTION_SHAPE_METRICS[key]
            ceiling = qualification.ceiling_at(fit, cell["logical_rows"], factor, unit)["ceiling"]
            wrong_schedule = name == "C2" and key == "vacuum_wal_bytes_per_ten_publications"
            checks.append({
                "cell": name, "metric": key, "logical_rows": cell["logical_rows"],
                "measured": measured, "ceiling": ceiling, "passes": measured <= ceiling,
                "verdict": "pending_review" if wrong_schedule else "pass" if measured <= ceiling else "fail",
                "applicability": "warm_fit_applied_to_post_checkpoint_requires_review" if wrong_schedule
                    else qualification.PRODUCTION_SHAPE_METRICS[key][0] or "production_shape",
            })
    return checks, {
        "metric": metric, "ceiling": vacuum_fit,
        "at_measured_sizes": [qualification.ceiling_at(vacuum_fit, n, 1.5, qualification.MIB / 4)
                              for n in vacuum_fit["fit_sizes"]],
        "warm_ceiling_changed": False,
    }


def evaluate(reports, *, allow_warm_wal_deviation=False, qualification_report=None):
    if {r["cell"] for r in reports} != {"C1", "C2", "C3"} or len(reports) != 3:
        raise ValueError("one complete C1, C2 and C3 report is required")
    if not all(r.get("complete") for r in reports):
        raise ValueError("incomplete measurement; retain the partial reports")
    identities = {
        (r["manifest_sha256"], r["capture_sha256"], r["profile"], r["copies"])
        for r in reports
    }
    if len(identities) != 1:
        raise ValueError("source/server manifest, capture or scale differs between cells")
    settings = {json.dumps(r["profile_identity"]["settings"], sort_keys=True) for r in reports}
    if len(settings) != 1 and not allow_warm_wal_deviation:
        raise ValueError("settings changed between cells; review the deviation separately")
    settings_review = settings_homogeneity(reports)
    cells = {}
    for report in reports:
        if report["cell"] == "C3":
            continue
        cell = copy.deepcopy(report)
        # The remote container has no .git directory. Its complete source-file
        # manifest supplies a stronger pool identity than an unknown Git revision.
        source_digest = hashlib.sha256(json.dumps(cell["source_manifest"], sort_keys=True).encode()).hexdigest()
        cell["profile_identity"]["tested_revision"] = "source-sha256:" + source_digest
        cell["cluster"] = {"cluster_name": cell["profile_identity"]["settings"]["cluster_name"]["setting"]}
        cells[cell["cell"]] = evaluate_cell(cell)
    memory_report = next(r for r in reports if r["cell"] == "C3")
    if (memory_report["profile_identity"]["host"]["platform"].startswith("Linux")
            and memory_report["result"].get("memory_launch_mode") != "clean_parent"):
        raise ValueError("Linux memory workers must be launched from the clean parent collector")
    memory = memory_report["result"]["children"]
    if any(len(memory.get(layout, [])) != 5 for layout in ("A", "B50")):
        raise ValueError("five fresh memory workers per layout are required")
    sizes = {r["logical_rows"] for rows in memory.values() for r in rows}
    if len(sizes) != 1:
        raise ValueError("memory workers used different workload sizes")
    memory_size = sizes.pop()
    relative_pass = all(
        cell["sufficiency"]["verdict"] == "sufficient"
        and all(gate.get("verdict") == "pass" for gate in cell["gates"].values())
        for cell in cells.values()
    )
    proposed = {}
    if relative_pass:
        # These are measured-size proposals, not a fit or an extrapolation. The
        # headroom factors match the reviewed qualification methodology.
        for cell_name, cell in cells.items():
            selected = cell["distributions"]["B50"]
            for metric, key, factor in (
                ("publication_p95_ms", "publication_ms", 1.5),
                ("composite_d_p95_ms", "composite_d_ms", 2.0),
                ("composite_t_format_stage_p95_ms", "composite_t_format_stage_ms", 2.0),
            ):
                if selected[key] is not None:
                    proposed[f"{cell_name}:{metric}"] = {
                        "measured_p95": selected[key]["p95"],
                        "ceiling": math.ceil(factor * selected[key]["p95"]),
                        "factor": factor, "logical_rows": cell["logical_rows"],
                        "samples": selected[key]["count"], "reviewed": False,
                        "measurement_regime": "warm_only" if cell_name == "C1" else "post_checkpoint_without_reads",
                        "max_wal_size_mb": int(next(r for r in reports if r["cell"] == cell_name)
                                               ["profile_identity"]["settings"]["max_wal_size"]["setting"]),
                    }
        for key in ("untraced_worker_rss_highwater_bytes", "publication_allocation_peak_bytes"):
            measured = max(r[key] for r in memory["B50"])
            proposed[key] = {"measured_max": measured, "ceiling": math.ceil(1.5 * measured),
                             "factor": 1.5, "logical_rows": memory_size, "reviewed": False}
    checks, vacuum = shape_checks(cells, qualification_report) if qualification_report else ([], None)
    return {"relative_gates_pass": relative_pass, "activation_authorized": False,
            "traffic_representative": False, "cells": cells, "memory": memory,
            "settings_review": settings_review,
            "allow_warm_wal_deviation": allow_warm_wal_deviation,
            "shape_checks": checks, "qualification_vacuum_budget": vacuum,
            "proposed_real_path_ceilings": proposed,
            "limitations": ["Measured sizes only; no extrapolation or cross-host pooling.",
                            "Production-shape WAL/footprint ceilings remain additional acceptance gates.",
                            "Budget review, deployment verification and production conversion remain pending."]}


def public_projection(result):
    """Only remove fields; never manufacture evidence outside the evaluator."""
    public = copy.deepcopy(result)
    allowed = {"cell", "size_profile", "logical_rows", "sufficiency", "gates", "distributions",
               "vacuum_wal_max_bytes_by_layout", "vacuum_windows_kept", "vacuum_windows_total"}
    public["cells"] = {name: {k: v for k, v in cell.items() if k in allowed}
                       for name, cell in public["cells"].items()}
    memory_keys = {"logical_rows", "untraced_worker_rss_highwater_bytes", "publication_allocation_peak_bytes"}
    public["memory"] = {layout: [{k: v for k, v in child.items() if k in memory_keys} for child in children]
                        for layout, children in public["memory"].items()}
    return public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs=3, type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification-report", type=Path, required=True)
    parser.add_argument("--qualification-sha256", required=True)
    parser.add_argument("--public-output", type=Path)
    parser.add_argument("--vacuum-output", type=Path)
    parser.add_argument("--allow-warm-wal-deviation", action="store_true",
                        help="Only after review: allow qualification's C1-only max_wal_size exception")
    args = parser.parse_args()
    os.umask(0o077)
    for path in [*args.reports, args.output]:
        assert_private_store(path)
    if args.output.exists():
        raise ValueError("refusing to overwrite an evaluation")
    qualification_bytes = args.qualification_report.read_bytes()
    qualification_digest = hashlib.sha256(qualification_bytes).hexdigest()
    if qualification_digest != args.qualification_sha256:
        raise ValueError("qualification report does not match the reviewed seal")
    result = evaluate([json.loads(p.read_text()) for p in args.reports],
                      allow_warm_wal_deviation=args.allow_warm_wal_deviation,
                      qualification_report=json.loads(qualification_bytes))
    result["inputs_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in args.reports}
    result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["qualification_evaluator_sha256"] = hashlib.sha256(Path(qualification.__file__).read_bytes()).hexdigest()
    result["qualification_report_sha256"] = qualification_digest
    result["qualification_vacuum_budget"]["source_sha256"] = qualification_digest
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.public_output:
        args.public_output.write_text(json.dumps(public_projection(result), indent=2) + "\n")
    if args.vacuum_output:
        args.vacuum_output.write_text(json.dumps(result["qualification_vacuum_budget"], indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("relative_gates_pass", "activation_authorized")}))


if __name__ == "__main__":
    main()
