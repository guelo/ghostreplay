#!/usr/bin/env python3
"""§2.5 — the integrated qualification evaluator for g-score-store-qualify.

Sibling to ``summarize_opening_score_budgets.py``, reusing its
``paired_p95_ratio`` and ``remeasure_opening_score_budgets``'s
``rounded_headroom`` so the uncertainty treatment and the headroom arithmetic
are the reviewed ones rather than new ones.

Two rules shape everything here. SAMPLE SUFFICIENCY RUNS FIRST AND GATES THE
VERDICT: insufficient evidence is not a failure and not a pass, and it can never
be reached by pooling unlike things to make a minimum. And A-RELATIVE RATIOS
TRANSFER BETWEEN HOSTS WHILE ABSOLUTE HOST-DEPENDENT CEILINGS DO NOT, so every
ceiling is tagged, and a ``local_host_only`` one is owed to
``g-score-store-cutover`` before activation rather than published as a
production limit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from scripts.bench_opening_score_storage import write_report
from scripts.remeasure_opening_score_budgets import MIB, rounded_headroom
from scripts.summarize_opening_score_budgets import paired_p95_ratio

MINIMUM_READS_PER_POOL = 500
MINIMUM_PAIRED_BLOCKS = 2
MINIMUM_FIT_SIZES = 3
MINIMUM_CELL_SAMPLES = 40
MINIMUM_VACUUM_WINDOWS = 3
MINIMUM_MEMORY_CHILDREN = 5
DEFERRED_TO = "g-score-store-cutover"
FIXTURE_REPLICATION = 17

# What a metric's ``samples`` number MEANS, and the minimum that applies to it.
# A single n >= 40 rule applied to everything refuses the footprint ceiling (one
# final measurement), the vacuum-WAL ceiling (ten windows) and both memory
# ceilings (five children) outright — and it invites the opposite error, passing
# the literal minimum as the sample count so the check clears. So each metric
# declares the KIND of sample it is fitted from and a minimum for that kind, and
# ``assert_fit_inputs`` checks both.
METRIC_SAMPLE_RULES = {
    "warm_publication_wal_bytes": ("publications", MINIMUM_CELL_SAMPLES),
    "post_checkpoint_publication_wal_bytes": ("publications", MINIMUM_CELL_SAMPLES),
    "publication_p95_ms": ("publications", MINIMUM_CELL_SAMPLES),
    "composite_d_p95_ms": ("reads", MINIMUM_READS_PER_POOL),
    "composite_t_format_stage_p95_ms": ("reads", MINIMUM_READS_PER_POOL),
    "vacuum_wal_bytes_per_ten_publications": ("vacuum_windows", MINIMUM_VACUUM_WINDOWS),
    "vacuumed_footprint_bytes": ("final_footprint", 1),
    "integrated_worker_rss_bytes": ("spawned_children", MINIMUM_MEMORY_CHILDREN),
    "publication_allocation_peak_bytes": ("spawned_children", MINIMUM_MEMORY_CHILDREN),
}

# §4.1/§4.2: the sealed fixture shape is a REGRESSION TIE-BACK and never a fit
# point — its row mix is not production's, and one slope through it and S1 would
# confound mix with size. It also runs on the OTHER cluster, so it cannot join
# the settings or cluster homogeneity check either. It therefore arrives through
# its own argument and is refused in ``--cell``.
FIXTURE_PROFILE = "SF"

# §0.2's two clusters, spelled here so the evaluator can require one of them
# WITHOUT being handed a fixture cell first. The cluster-sharing rule below only
# ever fired when a ``--fixture-cell`` was supplied, so a run measured entirely
# on the spike cluster was perfectly homogeneous and passed. These must equal
# the harness's own constants; the release tests assert that they do rather than
# importing SQLAlchemy into an evaluator that reads nothing but JSON.
QUALIFICATION_CLUSTER = "ghostreplay-score-storage-qual"
FIXTURE_CLUSTER = "ghostreplay-score-storage-spike"

# §9.5 permits exactly ONE settings deviation, and only these cells may carry
# it. Anything else — another setting, or another cell — is a refusal, not a
# note: cells measured under different durability or checkpoint settings are not
# one qualification.
PERMITTED_DEVIATING_SETTINGS = frozenset({"max_wal_size"})
PERMITTED_DEVIATING_CELLS = frozenset({"C1"})

PRODUCTION_SHAPE_METRICS = {
    "post_checkpoint_publication_wal_bytes": ("production_applicable", 1.5, MIB / 4),
    "warm_publication_wal_bytes": ("lower_bound", 1.5, MIB / 4),
    "vacuum_wal_bytes_per_ten_publications": (None, 1.5, MIB / 4),
    "vacuumed_footprint_bytes": (None, 1.5, MIB),
}
LOCAL_HOST_ONLY_METRICS = {
    "publication_p95_ms": 1.5,
    "composite_d_p95_ms": 2.0,
    "composite_t_format_stage_p95_ms": 2.0,
    "integrated_worker_rss_bytes": 1.5,
    "publication_allocation_peak_bytes": 1.5,
}

A_RELATIVE_GATES = {
    "combined_wal_ratio_max": 0.5,
    "vacuumed_footprint_ratio_max": 1.0,
    "publication_p95_ratio_max": 1.1,
    "composite_d_p95_ratio_max": 1.1,
    "composite_t_format_stage_p95_ratio_max": 1.1,
}

# §4.7's C4 control and §4.9's C7 delta lane. Both are named in §4.2's required
# matrix and NEITHER was an evaluator input: the evaluator could not tell a C4
# that ran and passed from one that was never run, and ``required_coverage``
# exists precisely so that nothing missing reads as a pass.
CONTROL_LABELS = ("A_old", "A_new")
# The same 10% band the A-relative publication gate uses, for the same reason:
# the two labels run sequentially, in different worktrees and different
# databases, so a bare "not slower" would fail on run-to-run variation. §5.5's
# "A_new worse than A_old" is read at that band and at the p95, not the median.
CONTROL_P95_RATIO_MAX = A_RELATIVE_GATES["publication_p95_ratio_max"]
MINIMUM_CONTROL_REPETITIONS = MINIMUM_CELL_SAMPLES

# §4.9: warm whole-graph-contention p95 under 3000 ms for normal AND drill, in
# BOTH formats. Process-cold figures are recorded and never mixed into the warm
# p95, so they are reported here and gate nothing.
DELTA_LANE_P95_LIMIT_MS = 3000.0
DELTA_LANE_MODES = ("normal", "drill")
DELTA_LANE_FORMATS = ("legacy", "current-b50-v1")
MINIMUM_DELTA_LANE_REPETITIONS = 5


class QualificationEvaluationError(ValueError):
    """The evaluator refused to emit a number it could not justify."""


# --------------------------------------------------------------------------
# Pool identity and sample sufficiency — these run BEFORE any verdict
# --------------------------------------------------------------------------


def pool_identity(sample: dict) -> tuple:
    """What makes two samples comparable, stated once and applied everywhere.

    Nothing may be pooled across a different size profile, composite,
    membership, checkpoint schedule, revision, settings digest, cluster or host.
    Pooling unlike workloads merely to reach a minimum is what the minimum
    exists to prevent.
    """
    return (
        sample["size_profile"],
        sample["composite"],
        sample["membership"],
        sample["checkpoint_schedule"],
        sample["revision"],
        sample["settings_digest"],
        sample["cluster_identity"],
        sample["host_identity"],
    )


def assert_pool_homogeneous(samples: list[dict]) -> tuple:
    identities = {pool_identity(sample) for sample in samples}
    if len(identities) != 1:
        raise QualificationEvaluationError(
            f"pool mixes {len(identities)} identities; unlike workloads, "
            "revisions or settings may not be pooled to reach a minimum"
        )
    return next(iter(identities))


def p95_with_rank(values: list[float]) -> dict:
    """Every p95 carries its sample count and nearest-rank index."""
    if not values:
        raise QualificationEvaluationError("p95 of an empty sample")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "p95": ordered[index],
        "count": len(ordered),
        "nearest_rank_index": index,
        "max": ordered[-1],
        "median": ordered[len(ordered) // 2],
    }


def sufficiency(
    reads_by_composite: dict[str, dict[str, list]],
    complete_paired_blocks: int,
    discarded_blocks: list[dict],
    *,
    declared_reads: dict[str, int],
) -> dict:
    """The first gate, and the one that cannot be argued around.

    Fewer than 500 individual reads per comparable window per layout, or fewer
    than two complete paired ten-publication blocks AFTER the checkpoint
    discards, is INSUFFICIENT EVIDENCE — never a pass and never a fail. Fifty-read
    block p95s are diagnostics, not separate acceptance gates.

    SCOPED TO THE COMPOSITES THE CELL DECLARES. C2 runs zero reads by design —
    it measures post-checkpoint publication WAL, which is the ceiling §0.3 calls
    the production-applicable one — so a flat 500-read minimum made C2
    structurally ``insufficient_evidence``, and with it the aggregate verdict,
    at every size and for every possible run. A composite the cell does not
    declare is NOT GATED and is recorded as ``not_measured``; a composite it
    DOES declare must reach the minimum on BOTH layouts, composite T included —
    the earlier version checked only D, so two discarded blocks could leave 480
    T reads with the T gate still running.
    """
    reasons = []
    per_composite = {}
    for composite, declared in sorted(declared_reads.items()):
        layouts = reads_by_composite.get(composite, {})
        counts = {layout: len(values) for layout, values in sorted(layouts.items())}
        if not declared:
            per_composite[composite] = {
                "status": "not_measured",
                "reads_per_layout": counts,
                "note": "the cell declares no reads of this composite; not gated",
            }
            continue
        thin = [
            layout
            for layout, count in counts.items()
            if count < MINIMUM_READS_PER_POOL
        ]
        if thin or not counts:
            reasons.append(
                f"{composite}: {counts or 'no layouts'} below "
                f"{MINIMUM_READS_PER_POOL} reads per layout"
            )
        per_composite[composite] = {
            "status": "insufficient" if thin or not counts else "sufficient",
            "reads_per_layout": counts,
            "declared_per_publication": declared,
        }
    if complete_paired_blocks < MINIMUM_PAIRED_BLOCKS:
        reasons.append(
            f"{complete_paired_blocks} complete paired blocks remain after "
            f"{len(discarded_blocks)} checkpoint discards, below "
            f"{MINIMUM_PAIRED_BLOCKS}"
        )
    return {
        "verdict": "insufficient_evidence" if reasons else "sufficient",
        "reasons": reasons,
        "composites": per_composite,
        "gated_composites": sorted(
            name
            for name, entry in per_composite.items()
            if entry["status"] == "sufficient"
        ),
        "complete_paired_blocks": complete_paired_blocks,
        "discarded_blocks": discarded_blocks,
    }


# --------------------------------------------------------------------------
# Absolute ceilings: fixed overhead + per-logical-row slope
# --------------------------------------------------------------------------


def least_squares_fit(points: list[tuple[float, float]]) -> dict:
    """Two parameters over at least three sizes, residuals retained.

    ``fixed overhead + per-logical-row slope`` is the only form allowed, because
    a single ratio through one point cannot separate the per-row cost from the
    per-pair cost, and a ceiling that cannot separate them is not transferable to
    a pair of a different size.
    """
    sizes = sorted({size for size, _ in points})
    if len(sizes) < MINIMUM_FIT_SIZES:
        raise QualificationEvaluationError(
            f"a ceiling needs at least {MINIMUM_FIT_SIZES} distinct sizes, not "
            f"{len(sizes)}"
        )
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        raise QualificationEvaluationError("fit sizes are degenerate")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in points]
    return {
        "fixed_overhead": intercept,
        "per_logical_row_slope": slope,
        "residuals": residuals,
        "max_positive_residual": max([r for r in residuals if r > 0], default=0.0),
        "applicable_size_range": [min(sizes), max(sizes)],
        "fit_sizes": sizes,
        "logical_row_denominator": (
            "positions + roots + edges + scope counted once; marker, indexes and "
            "MVCC versions excluded"
        ),
    }


def assert_fit_inputs(metric: str, points: list[dict]) -> None:
    """A metric whose cell was too small cannot anchor a ceiling at all.

    The minimum is the one declared for THAT metric's sample kind
    (``METRIC_SAMPLE_RULES``), and the kind is asserted too, so a point cannot
    satisfy a publication-count minimum with a window count or with the
    constant itself.
    """
    if metric not in METRIC_SAMPLE_RULES:
        raise QualificationEvaluationError(f"{metric} declares no sample rule")
    kind, minimum = METRIC_SAMPLE_RULES[metric]
    wrong_kind = [p for p in points if p.get("sample_kind") != kind]
    if wrong_kind:
        raise QualificationEvaluationError(
            f"{metric}: every fit point must count {kind!r}; "
            f"{[(p['size_profile'], p.get('sample_kind')) for p in wrong_kind]} do not"
        )
    thin = [p for p in points if p["samples"] < minimum]
    if thin:
        raise QualificationEvaluationError(
            f"{metric}: refusing to fit from cells with fewer than {minimum} "
            f"{kind} ({[(p['size_profile'], p['samples']) for p in thin]})"
        )
    derived = [p for p in points if p.get("derived_from") != "measured"]
    if derived:
        raise QualificationEvaluationError(
            f"{metric}: every fit point must be MEASURED; "
            f"{[p['size_profile'] for p in derived]} are not"
        )
    layouts = {p["layout"] for p in points}
    if layouts != {"B50"}:
        raise QualificationEvaluationError(
            f"{metric}: absolute ceilings are fitted from the SELECTED design "
            f"only, never from {sorted(layouts - {'B50'})} — a slower A must not "
            "be able to loosen an absolute ceiling"
        )


def ceiling_at(fit: dict, size: float, factor: float, unit: float = 1) -> dict:
    """Headroom applies to fitted + max positive residual, never fitted alone.

    Applying the factor to the fit alone lets the fit's own error consume the
    headroom, so a size where the model under-predicts starts out of budget.
    """
    low, high = fit["applicable_size_range"]
    if not low <= size <= high:
        raise QualificationEvaluationError(
            f"refusing to emit a ceiling at {size} logical rows, outside the "
            f"measured range [{low}, {high}]"
        )
    fitted = fit["fixed_overhead"] + fit["per_logical_row_slope"] * size
    anchor = fitted + fit["max_positive_residual"]
    return {
        "size_logical_rows": size,
        "fitted": fitted,
        "max_positive_residual": fit["max_positive_residual"],
        "anchor": anchor,
        "factor": factor,
        "ceiling": rounded_headroom(anchor, factor, unit),
        "derived_from": "measured_fit",
    }


def assert_not_fixture_derived(provenance: dict) -> None:
    """The synthetic constants are a regression tie-back, never a production limit.

    They were measured on a ~23k-position / ~71k-logical-row fixture replicated
    17×, whose row mix is not production's. Dividing one of them by 17 does not
    make it a production-shape ceiling, and neither does assuming linearity
    through a single point.
    """
    source = str(provenance.get("derived_from", ""))
    if source != "measured_fit":
        raise QualificationEvaluationError(
            f"ceiling provenance {source!r} is not a measured fit"
        )
    if provenance.get("divided_by") == FIXTURE_REPLICATION or provenance.get(
        "fixture_constant"
    ):
        raise QualificationEvaluationError(
            "refusing a ceiling derived by dividing a fixture constant by "
            f"{FIXTURE_REPLICATION}"
        )


# --------------------------------------------------------------------------
# Tagging: what transfers between hosts, and what is owed to the cutover gate
# --------------------------------------------------------------------------


def tag_ceiling(metric: str, ceiling: dict) -> dict:
    """``production_shape`` or ``local_host_only``, and the deferral is mandatory.

    WAL and footprint ceilings are functions of page writes, row widths and
    settings, all of which are matched here, so they transfer. Publication and
    read latency and worker RSS are not: CPU, storage and fsync semantics differ,
    RSS accounting is macOS rather than the deployed Linux container, and no
    matched application-to-database network path exists locally. Emitting one of
    those as a production limit would be the whole error, so a
    ``local_host_only`` ceiling without ``deferred_to`` FAILS here.
    """
    tagged = dict(ceiling)
    if metric in PRODUCTION_SHAPE_METRICS:
        applies_to, _factor, _unit = PRODUCTION_SHAPE_METRICS[metric]
        tagged["tag"] = "production_shape"
        if applies_to:
            tagged["applies_to"] = applies_to
            tagged["applies_to_note"] = (
                "active_users_30d is 1 and the gaps between publications are "
                "expected to exceed checkpoint_timeout, so nearly every "
                "production publication is post-checkpoint: C2's ceiling is the "
                "production-applicable one and C1's warm figure is a LOWER BOUND"
            )
    elif metric in LOCAL_HOST_ONLY_METRICS:
        tagged["tag"] = "local_host_only"
        tagged["deferred_to"] = DEFERRED_TO
        tagged["deferral_note"] = (
            "the EXPECTATION for the pre-activation gate and a re-measurable "
            "local regression baseline; not a production limit"
        )
    else:
        raise QualificationEvaluationError(f"{metric} has no host-transfer tag")
    return tagged


def assert_tagging_complete(ceilings: dict) -> None:
    for metric, ceiling in ceilings.items():
        if ceiling.get("tag") not in {"production_shape", "local_host_only"}:
            raise QualificationEvaluationError(f"{metric} is untagged")
        if ceiling["tag"] == "local_host_only" and ceiling.get("deferred_to") != DEFERRED_TO:
            raise QualificationEvaluationError(
                f"{metric} is local_host_only but carries no deferred_to: "
                f"{DEFERRED_TO}"
            )
        assert_not_fixture_derived(ceiling)


def assert_network_provenance(terms: dict) -> None:
    """A modelled term without its operands is a number with no meaning."""
    for name, term in terms.items():
        provenance = term.get("provenance", "")
        if "MODELLED" not in provenance:
            raise QualificationEvaluationError(
                f"network term {name} must be marked MODELLED"
            )
        missing = [
            operand
            for operand in ("bytes", "round_trips", "throughput_bytes_per_s", "rtt_ms_median")
            if term.get(operand) is None
        ]
        if missing:
            raise QualificationEvaluationError(
                f"network term {name} is missing operands {missing}"
            )


# --------------------------------------------------------------------------
# A-relative gates — full acceptance gates here, because ratios transfer
# --------------------------------------------------------------------------


def relative_gate(name: str, reference: list[dict], selected: list[dict], field: str) -> dict:
    """Paired blocks with the reviewed deterministic bootstrap interval.

    An interval straddling the limit is INCONCLUSIVE and demands more paired
    blocks; it is not rounded down into a pass.
    """
    limit = A_RELATIVE_GATES[f"{name}_ratio_max"]
    analysis = paired_p95_ratio(reference, selected, field)
    lower, upper = analysis["paired_block_percentile_95_interval"]
    analysis.update(
        {
            "limit": limit,
            "verdict": "pass" if upper <= limit else "fail" if lower > limit else "inconclusive",
            "gate_kind": "a_relative",
            "transfers_between_hosts": True,
        }
    )
    return analysis


def ratio_gate(name: str, reference: float, selected: float) -> dict:
    limit = A_RELATIVE_GATES[f"{name}_ratio_max"]
    ratio = selected / reference if reference else math.inf
    return {
        "reference": reference,
        "selected": selected,
        "ratio": ratio,
        "limit": limit,
        "verdict": "pass" if ratio <= limit else "fail",
        "gate_kind": "a_relative",
        "transfers_between_hosts": True,
    }


# --------------------------------------------------------------------------
# SF tie-back validity
# --------------------------------------------------------------------------

TIMELINE_FIELDS = (
    "actual_sizes",
    "start",
    "end",
    "events",
    "periods",
    "provenance",
    "quantized_control",
    "seed",
)


def tie_back_validity(regenerated: dict, approved: dict) -> dict:
    """Comparability comes from the REGENERATED TIMELINE, not the source digests.

    The recorded sha256 seal is already broken — six of twelve digests drifted
    when the writer and reader commits landed — so ``summarize`` and
    ``remeasure`` would now raise ``measured source changed`` against the
    approved report. A difference here is RECORDED, not hidden and not fatal: the
    tie-back becomes a DOCUMENTED-DIFFERENCE comparison and stays a regression
    signal only. It never was an acceptance gate.
    """
    differing = [
        field
        for field in TIMELINE_FIELDS
        if regenerated.get(field) != approved.get(field)
    ]
    return {
        "compared_fields": list(TIMELINE_FIELDS),
        "differing_fields": differing,
        "comparison_kind": "identity" if not differing else "documented_difference",
        "is_acceptance_gate": False,
        "note": (
            "regression signal only; the sha256 seal no longer seals, so "
            "comparability rests on the deterministic timeline fields"
        ),
    }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def _kept_records(cell: dict) -> tuple[dict[str, list], set]:
    """Records, and the vacuum windows, from the blocks that survived the discard."""
    paired = cell["result"]["paired_blocks"]
    kept = {block["block"] for block in paired["reference"]}
    out = {}
    for layout, records in cell["result"]["records"].items():
        selected = [r for r in records if r["block"] in kept]
        for record in selected:
            record["composite_d_samples_ms"] = [
                sample["composite_ms"] for sample in record.get("composite_d", [])
            ]
            record["composite_t_format_stage_samples_ms"] = [
                sample["format_stage_ms"] for sample in record.get("composite_t", [])
            ]
        out[layout] = selected
    return out, kept


def _kept_windows(cell: dict, kept_blocks: set) -> list[dict]:
    """Only windows that followed a KEPT block.

    A window after a discarded block vacuumed writes made under a different
    checkpoint schedule, so its WAL is not comparable to the rest.
    """
    return [
        window
        for window in cell["result"].get("vacuum_windows", [])
        if window.get("after_block") in kept_blocks
    ]


def _sample_descriptor(cell: dict, composite: str) -> dict:
    identity = cell["profile_identity"]
    return {
        "size_profile": cell["profile"],
        "composite": composite,
        "membership": "growing" if cell["cell"] == "C2" else "fixed",
        "checkpoint_schedule": "post_checkpoint" if cell["cell"] == "C2" else "warm",
        "revision": identity["tested_revision"],
        "settings_digest": settings_digest(identity),
        "cluster_identity": cell["cluster"]["cluster_name"],
        "host_identity": identity["host"]["platform"],
    }


def _settings_values(identity: dict) -> dict:
    return {
        name: value.get("setting") if isinstance(value, dict) else value
        for name, value in (identity.get("settings") or {}).items()
    }


def settings_homogeneity(cells: list[dict]) -> dict:
    """One qualification, one settings shape — with §9.5's single exception.

    The earlier version compared each cell's digest against WHICHEVER CELL WAS
    LISTED FIRST and then only reported the difference under a fixed note about
    ``max_wal_size``. A C2 cell measured with a raised ``max_wal_size`` — or
    with ``fsync`` off — therefore passed and stayed in the post-checkpoint fit,
    which is the ceiling §0.3 calls the production-applicable one.

    The baseline is the settings shape of the cells that are NOT permitted to
    deviate; if every cell is one that may, the largest group is the baseline
    and ties break on the digest, because in that case the permitted deviation
    is the only difference either way. Every deviating cell must then be a
    permitted cell AND differ only in permitted settings.
    """
    if not cells:
        return {"baseline_digest": None, "digest_by_cell": {}, "deviating_cells": [],
                "deviating_settings": {}}
    entries = [
        {
            "name": f"{cell['cell']}:{cell['profile']}",
            "cell": cell["cell"],
            "digest": settings_digest(cell["profile_identity"]),
            "settings": _settings_values(cell["profile_identity"]),
        }
        for cell in cells
    ]
    unmovable = [e for e in entries if e["cell"] not in PERMITTED_DEVIATING_CELLS]
    fixed = {e["digest"] for e in unmovable}
    if len(fixed) > 1:
        # NAME THE SETTINGS, not the digests. A digest tells a reader that two
        # cells disagree and nothing about what to fix, and the deviation that
        # matters here — fsync, checkpoint_completion_target — is one line of
        # output away.
        counts = Counter(e["digest"] for e in unmovable)
        majority = min(counts, key=lambda d: (-counts[d], d))
        reference = next(e["settings"] for e in unmovable if e["digest"] == majority)
        detail = []
        for entry in unmovable:
            if entry["digest"] == majority:
                continue
            changed = sorted(
                f"{name} {reference.get(name)!r} -> {entry['settings'].get(name)!r}"
                for name in set(reference) | set(entry["settings"])
                if reference.get(name) != entry["settings"].get(name)
            )
            detail.append(f"{entry['name']}: " + ", ".join(changed))
        raise QualificationEvaluationError(
            "cells that may not deviate disagree on settings — " + "; ".join(detail)
        )
    if fixed:
        baseline_digest = next(iter(fixed))
    else:
        counts = Counter(e["digest"] for e in entries)
        baseline_digest = min(counts, key=lambda d: (-counts[d], d))
    baseline = next(e["settings"] for e in entries if e["digest"] == baseline_digest)

    deviating, differing = [], {}
    for entry in entries:
        if entry["digest"] == baseline_digest:
            continue
        changed = sorted(
            name
            for name in set(baseline) | set(entry["settings"])
            if baseline.get(name) != entry["settings"].get(name)
        )
        if entry["cell"] not in PERMITTED_DEVIATING_CELLS:
            raise QualificationEvaluationError(
                f"{entry['name']} was measured under different settings "
                f"({changed}); §9.5 permits the deviation for "
                f"{sorted(PERMITTED_DEVIATING_CELLS)} only"
            )
        forbidden = [name for name in changed if name not in PERMITTED_DEVIATING_SETTINGS]
        if forbidden:
            raise QualificationEvaluationError(
                f"{entry['name']} deviates on {forbidden}; §9.5 permits only "
                f"{sorted(PERMITTED_DEVIATING_SETTINGS)}"
            )
        deviating.append(entry["name"])
        differing[entry["name"]] = {
            name: {
                "baseline": baseline.get(name),
                "measured": entry["settings"].get(name),
            }
            for name in changed
        }
    return {
        "baseline_digest": baseline_digest,
        "digest_by_cell": {e["name"]: e["digest"] for e in entries},
        "deviating_cells": sorted(deviating),
        "deviating_settings": differing,
    }


def settings_digest(identity: dict) -> str:
    payload = json.dumps(identity["settings"], sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


CONFIDENCE_BEARING_RELATIONS = ("opening_current_roots", "opening_current_positions")


def _confidence_wal_share(records: list[dict]) -> dict:
    """§4.5's "confidence share of publication WAL", reported for what it is.

    WAL records blocks, not columns, so no measurement can isolate the
    confidence COLUMN's bytes. What is measurable is the share of publication
    WAL landing in the two relations that carry confidence, and that is what is
    reported — labelled, so nobody reads it as a column attribution.
    """
    bearing = total = 0
    for record in records:
        by_relation = record["wal"].get("record_bytes_by_relation", {})
        total += sum(by_relation.values())
        bearing += sum(
            by_relation.get(name, 0) for name in CONFIDENCE_BEARING_RELATIONS
        )
    return {
        "confidence_bearing_relation_wal_bytes": bearing,
        "attributed_publication_wal_bytes": total,
        "share": (bearing / total) if total else None,
        "relations": list(CONFIDENCE_BEARING_RELATIONS),
        "note": (
            "the share of publication WAL landing in the relations that CARRY "
            "confidence, not an attribution of the confidence column: WAL "
            "records blocks, so no per-column split exists to measure"
        ),
    }


def _directional_confidence(records: list[dict]) -> dict:
    """§1.3: forward and backward changed-row fractions, reported SEPARATELY.

    Ping-pong traverses the same adjacent pairs in both directions. Averaging
    the two hides an asymmetry, and an asymmetry is exactly what would tell a
    reader that the backward steps are not comparable diffs.
    """
    out = {}
    for direction in ("forward", "backward"):
        fractions = [
            r["confidence"]["confidence_changed_fraction"]
            for r in records
            if r.get("direction") == direction and "confidence" in r
        ]
        stable = [
            r["confidence"]["stable_changed_fraction"]
            for r in records
            if r.get("direction") == direction and "confidence" in r
        ]
        out[direction] = {
            "samples": len(fractions),
            "mean_confidence_changed_fraction": (
                sum(fractions) / len(fractions) if fractions else None
            ),
            "mean_stable_changed_fraction": (
                sum(stable) / len(stable) if stable else None
            ),
        }
    forward = out["forward"]["mean_confidence_changed_fraction"]
    backward = out["backward"]["mean_confidence_changed_fraction"]
    out["asymmetry"] = (
        abs(forward - backward) if forward is not None and backward is not None else None
    )
    return out


def evaluate_cell(cell: dict) -> dict:
    """One cell's gates, with sufficiency decided before anything else."""
    result = {
        "cell": cell["cell"],
        "size_profile": cell.get("profile"),
        # §1.3's closure check: the harness refuses to replay a capture that
        # FAILED it, so what can reach here is "passed" or "skipped", and a skip
        # is a coverage gap rather than silence.
        "capture_closure": cell.get("capture_closure"),
    }
    if not cell.get("complete", True):
        # §4 keeps a partial cell's samples; the evaluator keeps its verdict
        # honest. A cell that refused mid-run is a recorded gap, never a pass.
        result["gates"] = {"verdict": "insufficient_evidence"}
        result["sufficiency"] = {
            "verdict": "insufficient_evidence",
            "reasons": [
                "the cell did not complete: "
                + str(cell.get("failure", {}).get("message", "no failure recorded"))
            ],
        }
        result["incomplete"] = cell.get("failure", {"message": "unknown"})
        return result

    records, kept_blocks = _kept_records(cell)
    windows = _kept_windows(cell, kept_blocks)
    paired = cell["result"]["paired_blocks"]
    declared = cell["result"].get("reads_per_publication", {})
    def _pool(field):
        return {
            layout: [value for record in layout_records for value in record[field]]
            for layout, layout_records in records.items()
        }

    reads = {
        "composite_d": _pool("composite_d_samples_ms"),
        "composite_t": _pool("composite_t_format_stage_samples_ms"),
    }
    descriptor = _sample_descriptor(cell, "composite_d")
    checks = sufficiency(
        reads,
        paired["complete_paired_blocks"],
        paired["discarded"],
        declared_reads={
            "composite_d": declared.get("composite_d", 0),
            "composite_t": declared.get("composite_t", 0),
        },
    )
    result.update(
        {
            "pool_identity": list(pool_identity(descriptor)),
            "sufficiency": checks,
            "distinct_adjacent_steps": cell.get("sequence", {}).get(
                "distinct_adjacent_steps"
            ),
            "vacuum_windows_kept": len(windows),
            "vacuum_windows_total": len(cell["result"].get("vacuum_windows", [])),
        }
    )
    if checks["verdict"] == "insufficient_evidence":
        result["gates"] = {"verdict": "insufficient_evidence"}
        return result

    gates = {
        "publication_p95": relative_gate(
            "publication_p95", records["A"], records["B50"], "publish_ms"
        ),
    }
    if "composite_d" in checks["gated_composites"]:
        gates["composite_d_p95"] = relative_gate(
            "composite_d_p95", records["A"], records["B50"], "composite_d_samples_ms"
        )
    if "composite_t" in checks["gated_composites"]:
        gates["composite_t_format_stage_p95"] = relative_gate(
            "composite_t_format_stage_p95",
            records["A"],
            records["B50"],
            "composite_t_format_stage_samples_ms",
        )

    # PER-LAYOUT vacuum WAL. One window vacuums every relation, so its total
    # covers both layouts; adding that total to each side adds a common constant
    # to numerator and denominator and drags the ratio toward 1.
    combined = {}
    for layout, layout_records in records.items():
        publication = sum(r["wal"]["total_bytes"] for r in layout_records)
        vacuum = sum(w["vacuum_wal_by_layout"][layout] for w in windows)
        combined[layout] = {
            "publication_wal_bytes": publication,
            "vacuum_wal_bytes": vacuum,
            "total": publication + vacuum,
        }
    gates["combined_wal"] = ratio_gate(
        "combined_wal", combined["A"]["total"], combined["B50"]["total"]
    )
    gates["combined_wal"]["components"] = combined
    gates["combined_wal"]["shared_vacuum_wal_bytes"] = sum(
        w["vacuum_wal_shared_and_unattributed_bytes"] for w in windows
    )
    final = cell["final_footprint"]
    gates["vacuumed_footprint"] = ratio_gate(
        "vacuumed_footprint", final["A_total_bytes"], final["B50_total_bytes"]
    )
    result["gates"] = gates
    result["distributions"] = {
        layout: {
            "publication_ms": p95_with_rank([r["publish_ms"] for r in layout_records]),
            "publication_wal_bytes": p95_with_rank(
                [r["wal"]["total_bytes"] for r in layout_records]
            ),
            "composite_d_ms": (
                p95_with_rank(reads["composite_d"][layout])
                if reads["composite_d"][layout]
                else None
            ),
            "composite_t_format_stage_ms": (
                p95_with_rank(reads["composite_t"][layout])
                if reads["composite_t"][layout]
                else None
            ),
        }
        for layout, layout_records in records.items()
    }
    result["confidence_expectation"] = _confidence_expectation(records["B50"])
    result["confidence_by_direction"] = _directional_confidence(records["B50"])
    result["confidence_wal_share"] = _confidence_wal_share(records["B50"])
    result["vacuum_wal_max_bytes_by_layout"] = {
        layout: max(
            (w["vacuum_wal_by_layout"][layout] for w in windows), default=0
        )
        for layout in records
    }
    result["vacuumed_footprint_bytes"] = final["B50_total_bytes"]
    result["logical_rows"] = (
        records["B50"][0]["logical_rows"] if records["B50"] else 0
    )
    result["own_catalog_overhead_bytes"] = sum(
        r["wal"]["own_catalog_overhead_bytes"]
        for layout_records in records.values()
        for r in layout_records
    )
    return result


def _confidence_expectation(records: list[dict]) -> dict:
    """Reported and COMPARED against the fixture share, never asserted."""
    fractions = [
        r["confidence"]["confidence_changed_fraction"]
        for r in records
        if "confidence" in r
    ]
    if not fractions:
        return {"observed": None, "note": "no adjacent pairs in this cell"}
    return {
        "mean_confidence_changed_fraction": sum(fractions) / len(fractions),
        "min": min(fractions),
        "max": max(fractions),
        "samples": len(fractions),
        "note": (
            "Density is fixed at CAPTURE time, not by the replay's computed_at: "
            "replay patches _build_cached_scores out, so the replay clock stamps "
            "the marker and changes no payload row. A materially lower fraction "
            "than the fixture's means the capture's clock spacing is "
            "unrepresentative and the WAL results are optimistic."
        ),
    }


def fixture_confidence_share(timeline: dict) -> dict:
    """§4.5's comparison target, taken from the approved run's own timeline."""
    fractions = [
        event["changes"]["groups"]["positions"]["confidence_changed"]
        / (event["changes"]["groups"]["positions"]["common"] or 1)
        for event in timeline.get("events", [])
        if "changes" in event
    ]
    if not fractions:
        return {"mean": None, "samples": 0}
    return {
        "mean": sum(fractions) / len(fractions),
        "min": min(fractions),
        "max": max(fractions),
        "samples": len(fractions),
    }


def compare_confidence_against_fixture(cell_results: list[dict], fixture: dict) -> dict:
    """RECORDED, not gated (§4.5). Material divergence goes to §5.5."""
    observed = {
        f"{r['cell']}:{r['size_profile']}": r["confidence_expectation"].get(
            "mean_confidence_changed_fraction"
        )
        for r in cell_results
        if "confidence_expectation" in r
    }
    reference = fixture.get("mean")
    ratios = {
        key: (value / reference)
        for key, value in observed.items()
        if value is not None and reference
    }
    return {
        "fixture": fixture,
        "observed": observed,
        "observed_over_fixture": ratios,
        "materially_lower": sorted(key for key, r in ratios.items() if r < 0.5),
        "note": (
            "a materially lower fraction than the fixture's means the capture's "
            "clock spacing is unrepresentative and the WAL results are "
            "optimistic — a finding against the profile (§5.5), never a silent "
            "pass and never an automatic failure"
        ),
    }


def evaluate_plateau(cell: dict) -> dict:
    """§4.8 — the C5 gates, which nothing read before.

    ``reclaimed_after_reader_finished`` alone is not the reclamation proof: with
    zero dead tuples held it says only that nothing was ever there. The proof
    needs the reader to have HELD something and the count to have returned to
    zero, which is why the harness records both halves.
    """
    if not cell.get("complete", True):
        return {"verdict": "insufficient_evidence", "reason": "cell did not complete"}
    plateau = cell["result"]["plateau"]
    orphans = cell["result"]["orphans"]
    gates: dict[str, dict] = {}
    for layout, entry in sorted(plateau.items()):
        growth = entry["last_five_growth_fraction"]
        gates[f"plateau_growth:{layout}"] = {
            "value": growth,
            "limit": 0.05,
            "verdict": "pass" if growth <= 0.05 else "fail",
        }
        gates[f"fixed_live_counts:{layout}"] = {
            "value": entry["fixed_live_counts"],
            "verdict": "pass" if entry["fixed_live_counts"] else "fail",
        }
        gates[f"reclamation:{layout}"] = {
            "dead_tuples_held": entry["dead_tuples_held"],
            "dead_tuples_released": entry["dead_tuples_released"],
            "verdict": (
                "pass"
                if entry.get("reclamation_proved")
                else "insufficient_evidence"
                if not entry.get("reader_held_dead_tuples")
                else "fail"
            ),
            "note": (
                "held == 0 is INSUFFICIENT, not a pass: the open snapshot "
                "blocked nothing, so 'returned to zero' proves nothing"
            ),
        }
    gates["orphans"] = {
        "current_rows_for_legacy_pair": orphans["current_rows_for_legacy_pair"],
        "legacy_rows_for_current_pair": orphans["legacy_rows_for_current_pair"],
        "verdict": "pass" if orphans["clean"] else "fail",
    }
    return {"cell": "C5", "size_profile": cell.get("profile"), "gates": gates}


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def build_ceilings(cell_results: list[dict], memory_reports: list[dict]) -> dict:
    """Every fitted metric, at every fit size, or no ceiling at all.

    §4.2's matrix and §5.2's ceiling list cannot contradict each other: a metric
    missing a size is a refusal here, not a ceiling quietly fitted through fewer
    points.
    """
    series: dict[str, list[dict]] = {}

    def add(metric, size_profile, logical_rows, value, samples, sample_kind,
            layout="B50"):
        series.setdefault(metric, []).append(
            {
                "size_profile": size_profile,
                "logical_rows": logical_rows,
                "value": value,
                "samples": samples,
                "sample_kind": sample_kind,
                "layout": layout,
                "derived_from": "measured",
            }
        )

    for result in cell_results:
        if result["gates"].get("verdict") == "insufficient_evidence":
            continue
        distributions = result["distributions"]["B50"]
        rows = result["logical_rows"]
        size = result["size_profile"]
        if result["cell"] == "C1":
            add("warm_publication_wal_bytes", size, rows,
                distributions["publication_wal_bytes"]["p95"],
                distributions["publication_wal_bytes"]["count"], "publications")
            # B50's OWN vacuum WAL. Fitting the whole window would fit a ceiling
            # for the selected design largely out of layout A's vacuum — about
            # 28 MB against B50's 0.24 MB in the approved report, a ceiling two
            # orders of magnitude too loose.
            add("vacuum_wal_bytes_per_ten_publications", size, rows,
                result["vacuum_wal_max_bytes_by_layout"]["B50"],
                result["vacuum_windows_kept"], "vacuum_windows")
            add("vacuumed_footprint_bytes", size, rows,
                result["vacuumed_footprint_bytes"], 1, "final_footprint")
            add("publication_p95_ms", size, rows,
                distributions["publication_ms"]["p95"],
                distributions["publication_ms"]["count"], "publications")
            if distributions["composite_d_ms"]:
                add("composite_d_p95_ms", size, rows,
                    distributions["composite_d_ms"]["p95"],
                    distributions["composite_d_ms"]["count"], "reads")
            if distributions["composite_t_format_stage_ms"]:
                add("composite_t_format_stage_p95_ms", size, rows,
                    distributions["composite_t_format_stage_ms"]["p95"],
                    distributions["composite_t_format_stage_ms"]["count"], "reads")
        elif result["cell"] == "C2":
            add("post_checkpoint_publication_wal_bytes", size, rows,
                distributions["publication_wal_bytes"]["p95"],
                distributions["publication_wal_bytes"]["count"], "publications")
    for report in memory_reports:
        result = report.get("result", {})
        if not report.get("complete", True) or "repeated_maximum" not in result:
            continue
        maximum = result["repeated_maximum"]["B50"]
        children = len(result["children"]["B50"])
        add("integrated_worker_rss_bytes", report["profile"], result["logical_rows"],
            maximum["untraced_worker_rss_highwater_bytes"], children,
            "spawned_children")
        add("publication_allocation_peak_bytes", report["profile"],
            result["logical_rows"], maximum["publication_allocation_peak_bytes"],
            children, "spawned_children")

    ceilings, gaps = {}, {}
    for metric, points in sorted(series.items()):
        try:
            assert_fit_inputs(metric, points)
            fit = least_squares_fit([(p["logical_rows"], p["value"]) for p in points])
        except QualificationEvaluationError as exc:
            gaps[metric] = {"verdict": "insufficient_evidence", "reason": str(exc)}
            continue
        if metric in PRODUCTION_SHAPE_METRICS:
            _applies, factor, unit = PRODUCTION_SHAPE_METRICS[metric]
        else:
            factor, unit = LOCAL_HOST_ONLY_METRICS[metric], 1
        reference = max(fit["fit_sizes"])
        ceilings[metric] = tag_ceiling(
            metric, {**fit, **ceiling_at(fit, reference, factor, unit)}
        )
        ceilings[metric]["points"] = points
    # A metric with NO measurement at all is a gap, not silence. Without this a
    # run of C1 cells alone emitted `pass` with no C2 ceiling, no memory
    # ceilings and no C5 result anywhere in the verdict.
    for metric in sorted(set(PRODUCTION_SHAPE_METRICS) | set(LOCAL_HOST_ONLY_METRICS)):
        if metric not in ceilings and metric not in gaps:
            gaps[metric] = {
                "verdict": "insufficient_evidence",
                "reason": f"{metric}: no cell supplied a measurement",
            }
    assert_tagging_complete(ceilings)
    return {"ceilings": ceilings, "gaps": gaps}


def evaluate_control(reports: list[dict]) -> dict:
    """§4.7's C4: is the SHIPPED legacy writer slower than its predecessor?

    Two labels at one size, from two worktrees. The comparison is the
    publication p95 — ``A_new / A_old`` — against the same 10% band the
    A-relative publication gate uses. The retirement-stage and lock-hold figures
    are ONE-SIDED: A_old's writer has no atomic retirement stage, so those are
    reported as a characterisation of A_new and gate nothing, exactly as the
    runner's own report says.
    """
    by_profile: dict[str, dict[str, dict]] = {}
    for report in reports:
        label = report.get("label")
        if label not in CONTROL_LABELS:
            raise QualificationEvaluationError(
                f"a control report carries label {label!r}, not one of "
                f"{list(CONTROL_LABELS)}"
            )
        profile = str(report.get("profile"))
        if label in by_profile.setdefault(profile, {}):
            raise QualificationEvaluationError(
                f"two {label} control reports were supplied for profile {profile}"
            )
        by_profile[profile][label] = report

    results = []
    for profile in sorted(by_profile):
        labels = by_profile[profile]
        entry: dict = {"size_profile": profile, "labels": sorted(labels)}
        # §1.3's closure check travels with the capture into the C4 runner's
        # report too. Only ``--cell`` results were read for it, so a control
        # pair replayed from a capture whose closure check was SKIPPED counted
        # as full coverage; ``required_coverage`` reads this and records a gap.
        entry["capture_closure"] = {
            label: side.get("capture_closure") for label, side in sorted(labels.items())
        }
        entry["capture_synthetic_only"] = {
            label: side.get("capture_synthetic_only")
            for label, side in sorted(labels.items())
        }
        missing = [name for name in CONTROL_LABELS if name not in labels]
        if missing:
            entry["verdict"] = "insufficient_evidence"
            entry["reason"] = f"missing control label(s) {missing}"
            results.append(entry)
            continue
        old, new_ = labels["A_old"], labels["A_new"]
        clusters = {
            side["cluster"]["cluster_name"]
            for side in (old, new_)
            if isinstance(side.get("cluster"), dict)
        }
        if len(clusters) > 1:
            raise QualificationEvaluationError(
                f"C4:{profile} compared labels measured on different clusters "
                f"{sorted(clusters)}"
            )
        entry["revisions"] = {"A_old": old.get("revision"), "A_new": new_.get("revision")}
        entry["repetitions"] = {
            "A_old": old.get("repetitions"),
            "A_new": new_.get("repetitions"),
        }
        thin = [
            label
            for label, side in (("A_old", old), ("A_new", new_))
            if (side.get("repetitions") or 0) < MINIMUM_CONTROL_REPETITIONS
        ]
        entry["publication_p95_ms"] = {
            "A_old": old.get("publication_p95_ms"),
            "A_new": new_.get("publication_p95_ms"),
        }
        entry["one_sided_stages"] = {
            "retirement_delete_p95_ms": new_.get("retirement_delete_p95_ms"),
            "publication_lock_hold_p95_ms": new_.get("publication_lock_hold_p95_ms"),
            "publication_lock_hold_samples": new_.get("publication_lock_hold_samples"),
            "note": (
                "A_old emits no atomic retirement stage, so these characterise "
                "A_new and are never a comparison or a gate"
            ),
        }
        base = old.get("publication_p95_ms")
        measured = new_.get("publication_p95_ms")
        if thin:
            entry["verdict"] = "insufficient_evidence"
            entry["reason"] = (
                f"{thin} ran fewer than {MINIMUM_CONTROL_REPETITIONS} repetitions"
            )
        elif not base or measured is None:
            entry["verdict"] = "insufficient_evidence"
            entry["reason"] = "a control report carries no publication p95"
        else:
            ratio = measured / base
            entry["publication_p95_ratio"] = round(ratio, 4)
            entry["limit"] = CONTROL_P95_RATIO_MAX
            entry["verdict"] = "pass" if ratio <= CONTROL_P95_RATIO_MAX else "fail"
        results.append(entry)
    return {
        "profiles": results,
        "gate": "A_new publication p95 / A_old publication p95",
        "limit": CONTROL_P95_RATIO_MAX,
    }


def evaluate_delta_lane(reports: list[dict]) -> dict:
    """§4.9's C7: warm whole-graph-contention p95 in BOTH storage formats.

    The release gate asserts this itself, inside the test. It is repeated here
    because a gate that only asserts inside a run nobody has to make cannot be
    distinguished from a run that never happened, and §4.2's matrix names C7 as
    a required result. Process-cold figures are carried and never gated.
    """
    results, seen = [], {}
    for report in reports:
        storage_format = str(report.get("storage_format"))
        if storage_format in seen:
            raise QualificationEvaluationError(
                f"two delta-lane reports were supplied for {storage_format}"
            )
        seen[storage_format] = report
        entry: dict = {"storage_format": storage_format, "gates": {}}
        # The limit is §4.9's, NOT the report's. Reading ``p95_limit_ms`` from
        # the file let a report loosen the gate it was about to be measured
        # against; it is still read, but only to refuse a report that ran
        # against a different limit from the one being applied here.
        declared = report.get("p95_limit_ms")
        if declared is not None and float(declared) != DELTA_LANE_P95_LIMIT_MS:
            raise QualificationEvaluationError(
                f"the {storage_format} delta-lane report declares a "
                f"{declared} ms limit, but §4.9's limit is "
                f"{DELTA_LANE_P95_LIMIT_MS} ms; a report does not set the gate "
                "it is judged by"
            )
        limit = DELTA_LANE_P95_LIMIT_MS
        entry["limit_ms"] = limit
        entry["declared_limit_ms"] = declared
        entry["identity"] = report.get("identity")
        entry["repetitions"] = report.get("repetitions")
        warm = report.get("whole_graph") or {}
        for mode in DELTA_LANE_MODES:
            measured = ((warm.get(mode) or {}).get("end_to_end") or {}).get("p95_ms")
            if measured is None:
                entry["gates"][mode] = {
                    "verdict": "insufficient_evidence",
                    "reason": f"no warm whole-graph {mode} p95 in the report",
                }
            elif (report.get("repetitions") or 0) < MINIMUM_DELTA_LANE_REPETITIONS:
                entry["gates"][mode] = {
                    "verdict": "insufficient_evidence",
                    "reason": (
                        f"{report.get('repetitions')} repetitions is below "
                        f"{MINIMUM_DELTA_LANE_REPETITIONS}"
                    ),
                    "p95_ms": measured,
                }
            else:
                entry["gates"][mode] = {
                    "verdict": "pass" if measured < limit else "fail",
                    "p95_ms": measured,
                    "limit_ms": limit,
                }
        entry["process_cold"] = report.get("process_cold")
        entry["process_cold_note"] = (
            "recorded, never mixed into the warm p95 and never gated (§4.9)"
        )
        results.append(entry)
    return {
        "formats": sorted(seen),
        "results": results,
        "gate": f"warm whole-graph-contention p95 < {DELTA_LANE_P95_LIMIT_MS} ms",
    }


def required_coverage(
    cell_results: list[dict],
    plateau: dict | None,
    network: dict | None,
    control: dict | None = None,
    delta_lane: dict | None = None,
) -> dict:
    """The inputs the verdict needs, absent ones recorded as gaps.

    §4.2's matrix names C5 and C6 as required results. An evaluator that simply
    does not look at an input it was not given cannot distinguish "measured and
    clean" from "never run", and only one of those authorises readiness.
    """
    gaps = {}
    if plateau is None:
        gaps["C5:plateau"] = "no C5 report supplied"
    if network is None:
        gaps["C6:network"] = "no C6 report supplied"
    if not any(result["cell"] == "C1" for result in cell_results):
        gaps["C1"] = "no C1 cell supplied"
    if not any(result["cell"] == "C2" for result in cell_results):
        gaps["C2"] = "no C2 cell supplied"
    # §4.2 names C4 and C7 as required results just as it names C5 and C6.
    if control is None or not control["profiles"]:
        gaps["C4:control"] = "no A_old/A_new control report supplied"
    else:
        for entry in control["profiles"]:
            if entry["verdict"] == "insufficient_evidence":
                gaps[f"C4:{entry['size_profile']}"] = entry["reason"]
    if control is not None:
        for entry in control["profiles"]:
            for label, closure in sorted((entry.get("capture_closure") or {}).items()):
                if (closure or {}).get("skipped"):
                    gaps[f"C4:{entry['size_profile']}:{label}:capture_closure"] = (
                        "the capture's §1.3 closure check was skipped, so "
                        "nothing proves the reveal mechanism left the final "
                        "payload unperturbed"
                    )
    if delta_lane is None or not delta_lane["results"]:
        gaps["C7:delta_lane"] = "no delta-lane report supplied"
    else:
        for storage_format in DELTA_LANE_FORMATS:
            if storage_format not in delta_lane["formats"]:
                gaps[f"C7:{storage_format}"] = (
                    "§4.9 requires the delta lane in BOTH formats; this one is "
                    "missing"
                )
    for result in cell_results:
        closure = result.get("capture_closure") or {}
        if closure.get("skipped"):
            gaps[f"{result['cell']}:{result['size_profile']}:capture_closure"] = (
                "the capture's §1.3 closure check was skipped, so nothing proves "
                "the reveal mechanism left the final payload unperturbed"
            )
    return gaps


def aggregate_verdict(
    cell_results: list[dict],
    ceilings: dict,
    *,
    plateau: dict | None = None,
    coverage: dict | None = None,
    control: dict | None = None,
    delta_lane: dict | None = None,
) -> dict:
    """Per gate, and no aggregate pass while any gate is insufficient.

    A ``local_host_only`` ceiling marked ``deferred`` is NOT an insufficient
    gate: its A-relative counterpart is measured and enforced here, and the
    absolute limit is owed by ``g-score-store-cutover`` before activation. What
    the evaluator may not do is emit it as a production limit, or emit it without
    the deferral reference.

    An absolute pass can never substitute for an A-relative gate: the two verdict
    sets are computed separately and the aggregate requires BOTH.
    """
    per_gate: dict[str, str] = {}
    for result in cell_results:
        prefix = f"{result['cell']}:{result['size_profile']}"
        if result["gates"].get("verdict") == "insufficient_evidence":
            per_gate[prefix] = "insufficient_evidence"
            continue
        for name, gate in result["gates"].items():
            per_gate[f"{prefix}:{name}"] = gate["verdict"]
    if plateau is not None:
        if plateau.get("verdict") == "insufficient_evidence":
            per_gate["C5"] = "insufficient_evidence"
        else:
            for name, gate in plateau["gates"].items():
                per_gate[f"C5:{name}"] = gate["verdict"]
    for entry in (control or {}).get("profiles", []):
        per_gate[f"C4:{entry['size_profile']}:publication_p95"] = entry["verdict"]
    for entry in (delta_lane or {}).get("results", []):
        for mode, gate in entry["gates"].items():
            per_gate[f"C7:{entry['storage_format']}:{mode}"] = gate["verdict"]
    for metric, gap in ceilings["gaps"].items():
        per_gate[f"ceiling:{metric}"] = gap["verdict"]
    for metric, ceiling in ceilings["ceilings"].items():
        per_gate[f"ceiling:{metric}"] = (
            "deferred" if ceiling["tag"] == "local_host_only" else "pass"
        )
    for name in sorted(coverage or {}):
        per_gate[f"coverage:{name}"] = "insufficient_evidence"
    verdicts = set(per_gate.values())
    # FAIL OUTRANKS A GAP. A measured gate that failed is a harder fact than a
    # metric nobody measured, and with `max_wal_size` at the census value gaps
    # are near-certain in the first long run — under the old order one missing
    # memory child would have reported a failing combined-WAL gate as
    # `insufficient_evidence`, which reads as "come back with more data" rather
    # than "this did not pass". Both are reported either way: the gaps stay in
    # `coverage_gaps` and `insufficient_gates`.
    if "fail" in verdicts:
        aggregate = "fail"
    elif "insufficient_evidence" in verdicts:
        aggregate = "insufficient_evidence"
    elif "inconclusive" in verdicts:
        aggregate = "inconclusive"
    elif not per_gate:
        aggregate = "insufficient_evidence"
    else:
        aggregate = "pass"
    return {
        "per_gate": per_gate,
        "aggregate": aggregate,
        "failing_gates": sorted(k for k, v in per_gate.items() if v == "fail"),
        "insufficient_gates": sorted(
            k for k, v in per_gate.items() if v == "insufficient_evidence"
        ),
        "precedence": (
            "fail > insufficient_evidence > inconclusive > pass; a measured "
            "failure is never reported as a gap, and a gap never as a pass"
        ),
        "coverage_gaps": dict(coverage or {}),
        "a_relative_gates_enforced_here": sorted(A_RELATIVE_GATES),
        "deferred_absolute_ceilings": sorted(
            metric
            for metric, ceiling in ceilings["ceilings"].items()
            if ceiling["tag"] == "local_host_only"
        ),
        "note": (
            "Passing authorises READINESS for the cutover workflow. It is not a "
            "claim that production is deployed or observed."
        ),
    }


SEALED_SOURCES = (
    "scripts/qualify_opening_score_storage.py",
    "scripts/qualify_opening_score_capture.py",
    "scripts/qualify_legacy_retirement_cost.py",
    "scripts/summarize_opening_score_qualification.py",
    "scripts/bench_opening_score_storage.py",
    "scripts/opening_score_storage_adapters.py",
    "scripts/opening_score_storage_workload.py",
    "app/opening_cache.py",
    "app/opening_score_storage.py",
    "app/opening_score_delta.py",
    "app/models.py",
)


def source_seal(inputs: list[Path]) -> dict:
    """§5.1's seal: the tooling that produced the numbers, and the inputs read.

    Recorded so a later reader can tell whether a re-run would be comparable.
    It is NOT an acceptance gate — the upstream budget report's own seal has
    already drifted, which is what proves a seal cannot be one.
    """
    root = Path(__file__).resolve().parents[1]

    def digest(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return "missing"

    return {
        "tooling_sha256": {name: digest(root / name) for name in SEALED_SOURCES},
        "input_sha256": {str(path): digest(Path(path)) for path in inputs},
        "note": (
            "a record of what produced these numbers, not a gate; the approved "
            "budget report's own seal has already drifted"
        ),
    }


def decision_record(report: dict) -> str:
    """The adjacent ``.md`` a human reads before authorising the cutover."""
    verdict = report["verdict"]
    lines = [
        "# Opening score storage — integrated qualification decision",
        "",
        f"- Run id: `{report['run_id']}`",
        f"- Tested revision: `{report['profile']['tested_revision']}`",
        f"- Settings digest: `{report['profile']['settings_digest']}`",
        f"- Cluster: `{report['profile']['cluster_identity']}`",
        f"- Host: {report['profile']['host_identity']}",
        "- Revision, host and cluster checked over: "
        + ", ".join(f"`{name}`" for name in report["profile"]["identity_checked_over"]),
        "- Exempt from the revision check (predecessor commit by construction): "
        + (
            ", ".join(
                f"`{name}`"
                for name in report["profile"].get("identity_revision_exempt", [])
            )
            or "none"
        ),
        f"- Aggregate verdict: **{verdict['aggregate']}**",
        "",
        "## What this verdict authorises",
        "",
        "Passing authorises READINESS for the cutover workflow. It is not a "
        "claim that production is deployed or observed.",
        "",
        "## Deferred absolute ceilings",
        "",
        f"These are `local_host_only` and owed to `{DEFERRED_TO}` before any "
        "B50 write is activated for a production pair. They are that gate's "
        "EXPECTATION and a local regression baseline, never production limits:",
        "",
    ]
    for metric in verdict["deferred_absolute_ceilings"]:
        ceiling = report["ceilings"]["ceilings"][metric]
        lines.append(f"- `{metric}` = {ceiling['ceiling']} at "
                     f"{ceiling['size_logical_rows']} logical rows")
    lines += ["", "## Per-gate verdicts", ""]
    for name, value in sorted(verdict["per_gate"].items()):
        lines.append(f"- `{name}`: {value}")
    if report["ceilings"]["gaps"]:
        lines += ["", "## Recorded gaps", ""]
        for metric, gap in sorted(report["ceilings"]["gaps"].items()):
            lines.append(f"- `{metric}`: {gap['reason']}")
    if verdict["coverage_gaps"]:
        lines += ["", "## Missing inputs", ""]
        lines.append(
            "These were never supplied. A missing input is a gap, not a pass:"
        )
        lines.append("")
        for name, reason in sorted(verdict["coverage_gaps"].items()):
            lines.append(f"- `{name}`: {reason}")
    if report["profile"]["incomplete_cells"]:
        lines += ["", "## Incomplete cells", ""]
        for name, failure in sorted(report["profile"]["incomplete_cells"].items()):
            lines.append(f"- `{name}`: {failure.get('message', 'unknown')}")
    if report["profile"]["settings_deviating_cells"]:
        lines += ["", "## Recorded settings deviation", ""]
        lines.append(report["profile"]["settings_deviation_note"])
        lines.append("")
        for name in report["profile"]["settings_deviating_cells"]:
            changed = report["profile"].get("settings_deviations", {}).get(name, {})
            detail = ", ".join(
                f"{setting} {values['baseline']} -> {values['measured']}"
                for setting, values in sorted(changed.items())
            )
            lines.append(f"- `{name}`: {detail}" if detail else f"- `{name}`")
    if "fixture_tie_back_cells" in report:
        lines += ["", "## Fixture tie-back cells (SF)", ""]
        lines.append(report["fixture_tie_back_cells"]["role"])
        lines.append("")
        for result in report["fixture_tie_back_cells"]["cells"]:
            lines.append(
                f"- `{result['cell']}:{result['size_profile']}`: "
                f"{result['gates'].get('verdict', 'per-gate, see the JSON')}"
            )
    if "control" in report:
        lines += ["", "## Legacy-retirement control (C4, A_new vs A_old)", ""]
        lines.append(
            f"Gate: {report['control']['gate']} <= {report['control']['limit']}."
        )
        lines.append("")
        for entry in report["control"]["profiles"]:
            ratio = entry.get("publication_p95_ratio")
            detail = f"{ratio:.3f}x" if ratio is not None else entry.get("reason", "")
            lines.append(f"- `{entry['size_profile']}`: {entry['verdict']} ({detail})")
        lines.append("")
        lines.append(
            "Retirement duration and publication-lock hold are ONE-SIDED: A_old "
            "has no atomic retirement stage to compare against, so those figures "
            "characterise A_new and gate nothing."
        )
    if "delta_lane" in report:
        lines += ["", "## Delta lane (C7)", ""]
        lines.append(report["delta_lane"]["gate"] + ".")
        lines.append("")
        for entry in report["delta_lane"]["results"]:
            for mode, gate in sorted(entry["gates"].items()):
                measured = gate.get("p95_ms")
                shown = f"{measured} ms" if measured is not None else gate.get("reason")
                lines.append(
                    f"- `{entry['storage_format']}:{mode}`: "
                    f"{gate['verdict']} ({shown})"
                )
        lines.append("")
        lines.append(
            "Process-cold visibility is recorded in the JSON and never mixed "
            "into the warm p95 (§4.9)."
        )
    if "confidence_vs_fixture" in report:
        comparison = report["confidence_vs_fixture"]
        lines += ["", "## Confidence-change fraction against the fixture (§4.5)", ""]
        lines.append(f"- Fixture mean: {comparison['fixture'].get('mean')}")
        for name, ratio in sorted(comparison["observed_over_fixture"].items()):
            lines.append(f"- `{name}`: {ratio:.2f}x the fixture's share")
        if comparison["materially_lower"]:
            lines.append("")
            lines.append(
                "MATERIALLY LOWER than the fixture at "
                + ", ".join(f"`{k}`" for k in comparison["materially_lower"])
                + " — the capture's clock spacing may be unrepresentative and "
                "the WAL results correspondingly optimistic (§5.5)."
            )
    lines += [
        "",
        "## Upstream seal drift",
        "",
        "Six of the twelve source digests recorded in the approved budget "
        "report have drifted, so `summarize_opening_score_budgets` and "
        "`remeasure_opening_score_budgets` would now raise `measured source "
        "changed` against it. Comparability for the SF tie-back therefore rests "
        "on the regenerated timeline's deterministic fields, and the tie-back is "
        "a regression signal only — it never was an acceptance gate.",
        "",
    ]
    return "\n".join(lines) + "\n"


def assert_no_fixture_in_fit(cells: list[dict], *, fixture_clusters=()) -> None:
    """``--cell`` is the FIT set, and SF may never be in it.

    ``build_ceilings`` fits every point it is handed, so an SF cell passed as
    ``--cell`` became a fifth fit point at ~71k rows, with a row mix that is not
    production's, on the other cluster. It has its own argument.

    The same rule covers ``--plateau``, ``--network`` and ``--memory``: each
    feeds a verdict or a fit directly, so an SF report there would be an
    acceptance gate on the fixture shape by another route.

    THE PROFILE LABEL IS NOT THE ONLY EVIDENCE. ``--profile`` is typed by the
    operator, so a mislabelled SF cell would pass a check that reads only the
    label. The CLUSTER is recorded by the harness from the cluster itself, and
    SF runs on QC-SPIKE, so any fit input sharing a cluster with a supplied
    fixture cell is refused here too.

    That second rule needs a fixture cell to have been supplied. The case where
    none was — a whole run measured on the spike cluster, perfectly homogeneous
    with itself — is closed by ``assert_identity_homogeneity``, which requires
    the qualification cluster BY NAME on every input rather than merely
    requiring them to agree.
    """
    offenders = sorted(
        f"{cell.get('cell')}:{cell.get('profile')}"
        for cell in cells
        if cell is not None and cell.get("profile") == FIXTURE_PROFILE
    )
    if offenders:
        raise QualificationEvaluationError(
            f"{offenders} are fixture-shape cells and cannot be fit points; "
            "pass them as --fixture-cell (§4.1: SF is a regression tie-back, "
            "never a fit point)"
        )
    spike = set(fixture_clusters)
    on_spike = sorted(
        f"{cell.get('cell')}:{cell.get('profile')}"
        for cell in cells
        if cell is not None
        and isinstance(cell.get("cluster"), dict)
        and cell["cluster"].get("cluster_name") in spike
    )
    if on_spike:
        raise QualificationEvaluationError(
            f"{on_spike} were measured on {sorted(spike)}, the cluster the "
            "fixture cells ran on; the --profile label says otherwise but the "
            "recorded cluster decides"
        )


def assert_fixture_cells_are_fixtures(cells: list[dict]) -> None:
    """``--fixture-cell`` is the tie-back set, and only SF belongs in it.

    The mirror of ``assert_no_fixture_in_fit``: without it, a production-shape
    cell could be routed out of the fit and out of the coverage count by
    passing it under the wrong argument, which is the same mistake in the
    opposite direction and would silently shrink the fit set.
    """
    offenders = sorted(
        f"{cell.get('cell')}:{cell.get('profile')}"
        for cell in cells
        if cell.get("profile") != FIXTURE_PROFILE
    )
    if offenders:
        raise QualificationEvaluationError(
            f"{offenders} are not {FIXTURE_PROFILE} cells and do not belong in "
            "--fixture-cell; a production-shape cell passed here would leave "
            "the fit set and the coverage count without being refused"
        )


def _identity_descriptor(name, revision, host, cluster, *, compare_revision=True):
    return {
        "name": name,
        "revision": revision,
        "host_identity": host,
        "cluster_identity": cluster,
        "compare_revision": compare_revision,
    }


def identity_descriptors(reports, *, controls=(), lanes=()):
    """One (name, revision, host, cluster) row per input, whatever wrote it.

    The three kinds of report record their identity differently — ``run_cell``
    writes a ``profile_identity``/``cluster`` pair, the C4 runner writes flat
    ``revision``/``host_platform``/``cluster`` fields, and the C7 lane gate
    writes an ``identity`` block — so they are normalised here rather than each
    growing its own comparison.

    Returns the rows and the names of the inputs that carry no identity at all,
    which is a refusal rather than an exemption.
    """
    descriptors, nameless = [], []
    for report in reports:
        name = f"{report.get('cell')}:{report.get('profile')}"
        if not report.get("profile_identity") or not report.get("cluster"):
            nameless.append(name)
            continue
        descriptors.append(
            _identity_descriptor(
                name,
                report["profile_identity"]["tested_revision"],
                report["profile_identity"]["host"]["platform"],
                report["cluster"]["cluster_name"],
            )
        )
    for report in controls:
        label = report.get("label")
        name = f"C4:{report.get('profile')}:{label}"
        cluster = report.get("cluster")
        if (
            not report.get("revision")
            or not report.get("host_platform")
            or not isinstance(cluster, dict)
            or not cluster.get("cluster_name")
        ):
            nameless.append(name)
            continue
        descriptors.append(
            _identity_descriptor(
                name,
                report["revision"],
                report["host_platform"],
                cluster["cluster_name"],
                # A_old IS the predecessor commit — that is the entire point of
                # the control — so its revision is EXPECTED to differ and only
                # its host and cluster are compared. A_new carries no such
                # exemption: it is the shipped writer, and it must have been
                # measured at the revision this qualification is about.
                compare_revision=label != "A_old",
            )
        )
    for report in lanes:
        name = f"C7:{report.get('storage_format')}"
        identity = report.get("identity")
        if not isinstance(identity, dict) or not all(
            identity.get(field)
            for field in ("tested_revision", "host_platform", "cluster_name")
        ):
            nameless.append(name)
            continue
        descriptors.append(
            _identity_descriptor(
                name,
                identity["tested_revision"],
                identity["host_platform"],
                identity["cluster_name"],
            )
        )
    return descriptors, sorted(nameless)


def assert_identity_homogeneity(reports: list[dict], *, controls=(), lanes=()) -> dict:
    """One qualification, one revision, one host, one cluster — over EVERY input.

    The check used to run over ``--cell`` reports alone, then over the fit set.
    ``--control`` and ``--delta-lane`` became verdict inputs without joining it:
    a C4 pair's cluster was compared only to its own other half, its A_new
    revision to nothing at all, and a C7 summary carried no revision, host or
    cluster in the first place, so any lane file from any machine passed.

    The CLUSTER is additionally required to be the qualification cluster BY
    NAME. Homogeneity alone cannot see a run measured end to end on the spike
    cluster — every input agrees — and the cluster-sharing rule in
    ``assert_no_fixture_in_fit`` only fires when a fixture cell was supplied to
    share a cluster with.
    """
    descriptors, nameless = identity_descriptors(
        reports, controls=controls, lanes=lanes
    )
    if nameless:
        raise QualificationEvaluationError(
            f"{nameless} carry no revision, host or cluster; every report the "
            "harness, the C4 runner and the C7 gate write carries all three, "
            "and one that does not cannot be shown to belong to this "
            "qualification"
        )
    if not descriptors:
        return {"reports": [], "revision": None, "host": None, "cluster": None}
    baseline = next(
        (d for d in descriptors if d["compare_revision"]), descriptors[0]
    )
    disagreeing = []
    for descriptor in descriptors:
        keys = ["host_identity", "cluster_identity"]
        if descriptor["compare_revision"] and baseline["compare_revision"]:
            keys.append("revision")
        if any(descriptor[key] != baseline[key] for key in keys):
            disagreeing.append(descriptor)
    if disagreeing:
        raise QualificationEvaluationError(
            "inputs were measured at different revisions, hosts or clusters: "
            + ", ".join(
                f"{d['name']}=({d['revision']}, {d['host_identity']}, "
                f"{d['cluster_identity']})"
                for d in [baseline, *disagreeing]
            )
        )
    elsewhere = sorted(
        f"{d['name']}@{d['cluster_identity']}"
        for d in descriptors
        if d["cluster_identity"] != QUALIFICATION_CLUSTER
    )
    if elsewhere:
        raise QualificationEvaluationError(
            "every fit and gate input must be measured on the qualification "
            f"cluster {QUALIFICATION_CLUSTER!r}; these were not: {elsewhere}. "
            f"{FIXTURE_CLUSTER!r} carries the §4.2 fixture capture, and its "
            "cells belong in --fixture-cell, which is reported and enters "
            "nothing"
        )
    return {
        "reports": [d["name"] for d in descriptors],
        "revision": baseline["revision"],
        "host": baseline["host_identity"],
        "cluster": baseline["cluster_identity"],
        "revision_exempt": [
            d["name"] for d in descriptors if not d["compare_revision"]
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", type=Path, action="append", default=[], required=True)
    parser.add_argument(
        "--fixture-cell",
        type=Path,
        action="append",
        default=[],
        help="SF tie-back cells; evaluated and reported, never fitted",
    )
    parser.add_argument("--memory", type=Path, action="append", default=[])
    parser.add_argument("--plateau", type=Path, default=None)
    parser.add_argument("--network", type=Path, default=None)
    parser.add_argument(
        "--control",
        type=Path,
        action="append",
        default=[],
        help="§4.7 C4 control reports (A_old and A_new, one file each)",
    )
    parser.add_argument(
        "--delta-lane",
        type=Path,
        action="append",
        default=[],
        help="§4.9 C7 delta-lane reports, one per storage format",
    )
    parser.add_argument("--regenerated-timeline", type=Path, default=None)
    parser.add_argument("--approved-budgets", type=Path, default=None)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    cells = [json.loads(path.read_text()) for path in args.cell]
    fixture_cells = [json.loads(path.read_text()) for path in args.fixture_cell]
    assert_fixture_cells_are_fixtures(fixture_cells)
    fixture_clusters = {
        cell["cluster"]["cluster_name"]
        for cell in fixture_cells
        if isinstance(cell.get("cluster"), dict)
    }
    memory = [json.loads(path.read_text()) for path in args.memory]
    plateau_report = (
        json.loads(args.plateau.read_text()) if args.plateau is not None else None
    )
    network = (
        json.loads(args.network.read_text()) if args.network is not None else None
    )
    controls = [json.loads(path.read_text()) for path in args.control]
    lanes = [json.loads(path.read_text()) for path in args.delta_lane]
    # EVERY report that feeds a fit or a gate, not just ``--cell``.
    fit_inputs = cells + memory + [r for r in (plateau_report, network) if r]
    assert_no_fixture_in_fit(fit_inputs, fixture_clusters=fixture_clusters)
    complete = [cell for cell in cells if cell.get("complete", True)]
    all_complete = [r for r in fit_inputs if r.get("complete", True)]
    # Pool identity differs by size profile and cell by design; what must never
    # differ inside one qualification is the revision, the host and the CLUSTER.
    # SETTINGS may differ for exactly one recorded reason — §9.5's max_wal_size
    # deviation for C1 — and that is checked, not merely noted, by
    # ``settings_homogeneity`` below.
    identity = assert_identity_homogeneity(
        all_complete, controls=controls, lanes=lanes
    )
    descriptors = [_sample_descriptor(cell, "composite_d") for cell in complete]
    homogeneity = settings_homogeneity(all_complete)
    settings_by_cell = homogeneity["digest_by_cell"]
    deviating = homogeneity["deviating_cells"]

    results = [evaluate_cell(cell) for cell in cells]
    fixture_results = [evaluate_cell(cell) for cell in fixture_cells]
    plateau = evaluate_plateau(plateau_report) if plateau_report else None
    control = evaluate_control(controls) if controls else None
    delta_lane = evaluate_delta_lane(lanes) if lanes else None
    ceilings = build_ceilings(results, memory)
    coverage = required_coverage(
        results, plateau_report, network, control=control, delta_lane=delta_lane
    )
    report = {
        "schema": 2,
        "run_id": args.run_id,
        "seal": source_seal(
            list(args.cell)
            + list(args.fixture_cell)
            + list(args.memory)
            + list(args.control)
            + list(args.delta_lane)
            + [p for p in (args.plateau, args.network) if p is not None]
        ),
        "profile": {
            "tested_revision": descriptors[0]["revision"] if descriptors else None,
            "settings_digest": homogeneity["baseline_digest"],
            "settings_digest_by_cell": settings_by_cell,
            "settings_deviating_cells": deviating,
            "settings_deviations": homogeneity["deviating_settings"],
            "settings_deviation_note": (
                "§9.5 permits ONE recorded settings deviation — max_wal_size "
                "raised for C1 only, if and only if observed discards leave "
                "fewer than two complete paired blocks. C1's warm ceiling is "
                "already a LOWER BOUND, so the deviation cannot loosen the "
                "production-applicable ceiling, which is C2's and keeps the "
                "census value. Pool identity includes the settings digest, so "
                "nothing is pooled across the deviation either way."
            ),
            "cluster_identity": identity["cluster"],
            "host_identity": identity["host"],
            "identity_checked_over": identity["reports"],
            "identity_revision_exempt": identity.get("revision_exempt", []),
            "stated_differences": (
                complete[0]["profile_identity"].get("stated_differences", [])
                if complete
                else []
            ),
            "postgres_binary_prefix": (
                complete[0]["profile_identity"]["postgres_binary_prefix"]
                if complete
                else None
            ),
            "sample_windows": {
                result["cell"] + ":" + str(result["size_profile"]): result[
                    "sufficiency"
                ]
                for result in results
            },
            "incomplete_cells": {
                result["cell"] + ":" + str(result["size_profile"]): result["incomplete"]
                for result in results
                if "incomplete" in result
            },
            "optional_optimizations": [],
            "cache": "not_applicable",
            "content_hash": "not_applicable",
            "optional_optimizations_note": (
                "the spike selected none — no payload cache, no content hash, no "
                "COPY, no chunked confidence — so the acceptance item is met by "
                "recorded non-applicability rather than by silence"
            ),
            "upstream_source_digest_drift": (
                "six of twelve digests recorded in "
                "opening-score-storage-budgets-2026-09-19.json have drifted"
            ),
            "traffic_representative": False,
        },
        "cells": results,
        "ceilings": ceilings,
    }
    if plateau is not None:
        report["plateau"] = plateau
    if network is not None:
        for layout, terms in network["result"]["network_terms"].items():
            assert_network_provenance(terms)
        report["network"] = network["result"]
    if args.approved_budgets:
        approved = json.loads(args.approved_budgets.read_text())
        report["confidence_vs_fixture"] = compare_confidence_against_fixture(
            results, fixture_confidence_share(approved["timeline"])
        )
        if args.regenerated_timeline:
            report["sf_tie_back"] = tie_back_validity(
                json.loads(args.regenerated_timeline.read_text()),
                approved["timeline"],
            )
    if fixture_results:
        # SF is REPORTED — a regression signal is worthless if nobody sees it —
        # and it enters nothing: not the fit, not the coverage, not the verdict.
        report["fixture_tie_back_cells"] = {
            "cells": fixture_results,
            "role": (
                "regression tie-back only (§4.1/§4.2): never a fit point, never "
                "a coverage input, never an acceptance gate, and measured on "
                "the other cluster"
            ),
        }
    if control is not None:
        report["control"] = control
    if delta_lane is not None:
        report["delta_lane"] = delta_lane
    report["verdict"] = aggregate_verdict(
        results,
        ceilings,
        plateau=plateau,
        coverage=coverage,
        control=control,
        delta_lane=delta_lane,
    )
    write_report(args.output, report)
    args.output.with_suffix(".md").write_text(decision_record(report))
    print(json.dumps(report["verdict"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
