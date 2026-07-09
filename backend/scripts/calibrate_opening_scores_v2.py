#!/usr/bin/env python3
"""One-off opening-score v2 calibration over historical evidence.

v2 is the only live scoring model (no v1 baseline exists), so this script
*calibrates v2 directly*: it scores every candidate ``(user_id, player_color)``
pair fully in memory and reports the distributions, source mix, phase-horizon
behaviour, and recursion accounting needed to choose grade thresholds and the
``tau`` parameters.

Safety:
  * The default run performs **zero database writes**. It only reads evidence via
    :func:`overlay_evidence` and scores in memory via
    :func:`compute_all_root_scores`; ``recompute_opening_scores`` (which reserves a
    generation and persists a batch) is never invoked unless ``--write-bench`` is
    passed.
  * ``--write-bench`` additionally requires ``--allow-writes`` and a
    ``--database-url`` that passes :func:`validate_write_bench_database_url`
    (SQLite under ``backend/.tmp/`` or an explicit ``calibrate`` database name,
    and never the configured production URL).

See ``CALIBRATE_OPENING_SCORES.md`` for cohort definition, flags, and the
write-bench safety gate.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import chess  # noqa: E402

from app.db import DATABASE_URL  # noqa: E402
from app.fen import active_color, normalize_fen  # noqa: E402
from app.opening_evidence import (  # noqa: E402
    EvidenceOverlay,
    NodeEvidence,
    PhaseSample,
    overlay_evidence,
)
from app.opening_graph import OpeningGraph, OpeningGraphNode, get_opening_graph  # noqa: E402
from app.opening_quality import TAU_CP, TAU_WC  # noqa: E402
from app.opening_rootcalc import (  # noqa: E402
    COVERAGE_FOLD_MODES,
    SYNTHETIC_INITIAL_FEN,
    CalcTelemetry,
    RootCalcConfig,
    compute_all_root_scores,
    compute_root_score,
)
from app.opening_roots import OpeningRoot, OpeningRoots, get_opening_roots  # noqa: E402

DEFAULT_MIN_OBSERVATIONS = 20
HISTOGRAM_EDGES = (0.0, 20.0, 40.0, 60.0, 80.0, 100.0)
DEFAULT_PERCENTILES = (5.0, 25.0, 50.0, 75.0, 95.0)

# Calibration-grid sweep defaults (the SET of values measured, NOT a tuning
# choice — g-5bcz ships no chosen value). Baseline (lcb_z=0.0, coverage_fold="off")
# is the current model and is always included by ``build_grid``.
DEFAULT_LCB_Z_GRID = (0.0, 1.0, 1.28)
DEFAULT_COVERAGE_GRID = ("off", "gate", "gate_x_cov")

# Current (pre-recalibration) grade cutoffs mirrored from src/openings/format.ts
# getPriorityLabel (A>=50 / B>=38 / C>=28 / D>=22 / F<22). Used ONLY to give the
# PASS/FAIL diagnostics a readable grade band; recalibrating format.ts to the new
# combined distribution belongs to the follow-up ship bead, not here.
GRADE_A, GRADE_B, GRADE_C, GRADE_D = 50.0, 38.0, 28.0, 22.0

# Documented numeric release gates (openingscore_final.md "Calibration Outcome").
SCORING_LATENCY_GATE_SECONDS = 5.0
CACHE_READ_GATE_MS = 50.0


# ---------------------------------------------------------------------------
# Pure statistics helpers (DB-free; unit-tested in test_calibrate_opening_scores)
# ---------------------------------------------------------------------------


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile ``q`` (0-100) of an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (q / 100.0) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[low]
    frac = rank - low
    return sorted_values[low] * (1.0 - frac) + sorted_values[high] * frac


def percentiles(
    values: list[float], qs: tuple[float, ...] = DEFAULT_PERCENTILES
) -> dict[str, float | None]:
    ordered = sorted(values)
    return {f"p{int(q)}": percentile(ordered, q) for q in qs}


def histogram(values: list[float], edges: tuple[float, ...] = HISTOGRAM_EDGES) -> list[int]:
    """Bucket counts for the half-open intervals ``[edges[i], edges[i+1])``.

    The final bucket is closed on the right so a perfect 100.0 lands in it rather
    than falling off the end.
    """
    counts = [0] * (len(edges) - 1)
    last = len(edges) - 2
    for value in values:
        for i in range(len(edges) - 1):
            upper = edges[i + 1]
            if value < upper or (i == last and value <= upper):
                counts[i] += 1
                break
    return counts


def source_mix(counts: Counter[str]) -> dict[str, object]:
    """Source counts as percentages, guarding the zero-denominator case."""
    total = sum(counts.values())
    if total == 0:
        return {"total": 0, "pct": {}}
    return {
        "total": total,
        "pct": {key: 100.0 * value / total for key, value in counts.items()},
    }


def summarize(values: list[float]) -> dict[str, object]:
    """Count + mean + percentiles + histogram for a list of scores."""
    return {
        "count": len(values),
        "mean": (sum(values) / len(values)) if values else None,
        "percentiles": percentiles(values),
        "histogram": histogram(values),
    }


# ---------------------------------------------------------------------------
# Write-bench database-URL safety gate (unit-tested)
# ---------------------------------------------------------------------------


def _database_name(url: str) -> str:
    """Last path segment of a SQLAlchemy URL, stripped of any query string."""
    without_query = url.split("?", 1)[0]
    return without_query.rsplit("/", 1)[-1]


def validate_write_bench_database_url(url: str, production_url: str) -> str:
    """Return ``url`` if it is a safe write-bench target, else raise ``ValueError``.

    A safe target is SQLite under ``backend/.tmp/`` *or* has a database name
    containing ``calibrate``, and is never the configured production URL.
    """
    if url == production_url:
        raise ValueError(
            "refusing to write-bench against the configured production database URL"
        )
    is_sqlite_tmp = url.startswith("sqlite") and ".tmp/" in url
    has_calibrate_name = "calibrate" in _database_name(url)
    if is_sqlite_tmp or has_calibrate_name:
        return url
    raise ValueError(
        "write-bench requires a SQLite URL under backend/.tmp/ or a database name "
        f"containing 'calibrate'; got: {url!r}"
    )


# ---------------------------------------------------------------------------
# In-memory scoring (DB read only via the injected overlay)
# ---------------------------------------------------------------------------


@dataclass
class PairScore:
    """Calibration result for one ``(user_id, player_color)`` pair.

    Computed purely in memory; constructing it performs no DB writes.
    """

    user_id: int
    player_color: str
    named_scores: list[float] = field(default_factory=list)
    # Same named-root scores keyed by opening_key, so per-pair deltas can be matched
    # opening-to-opening ACROSS grid cells (the plain list above keeps distributions
    # cheap; the map is what the delta reporting needs).
    named_score_map: dict[str, float] = field(default_factory=dict)
    synthetic_score: float | None = None
    observation_total: int = 0
    source_counts: Counter[str] = field(default_factory=Counter)
    excluded_sessions: int = 0
    phase_samples: list[PhaseSample] = field(default_factory=list)
    telemetry: CalcTelemetry = field(default_factory=CalcTelemetry)
    scoring_seconds: float = 0.0

    @property
    def emitted_row_count(self) -> int:
        return len(self.named_scores) + (1 if self.synthetic_score is not None else 0)


def score_overlay(
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
) -> PairScore:
    """Score one overlay in memory and separate the synthetic hero row.

    Passes ``include_synthetic_root=True`` so the ``__repertoire__`` hero row is
    computed in the same DAG pass, then reports it in its own field rather than
    mixing it into the named-root distribution.
    """
    telemetry = CalcTelemetry()
    started = time.perf_counter()
    scores, _eligible = compute_all_root_scores(
        player_color,
        graph,
        overlay,
        roots,
        config or RootCalcConfig(),
        include_branch_summaries=False,
        include_synthetic_root=True,
        telemetry=telemetry,
    )
    elapsed = time.perf_counter() - started

    synthetic = scores.get(SYNTHETIC_INITIAL_FEN)
    named_score_map = {
        key: score.opening_score
        for key, score in scores.items()
        if key != SYNTHETIC_INITIAL_FEN
    }
    named_scores = list(named_score_map.values())
    observation_total = sum(node.quality_count for node in overlay.nodes.values())

    return PairScore(
        user_id=user_id,
        player_color=player_color,
        named_scores=named_scores,
        named_score_map=named_score_map,
        synthetic_score=synthetic.opening_score if synthetic is not None else None,
        observation_total=observation_total,
        source_counts=Counter(overlay.source_counts),
        excluded_sessions=overlay.excluded_sessions,
        phase_samples=list(overlay.phase_samples),
        telemetry=telemetry,
        scoring_seconds=elapsed,
    )


def score_pair(
    db,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
) -> PairScore:
    """Build the overlay for a pair (DB read only) and score it in memory."""
    overlay = overlay_evidence(db, user_id, player_color, graph)
    return score_overlay(user_id, player_color, graph, overlay, roots, config)


# ---------------------------------------------------------------------------
# Calibration grid (g-zc3p / g-5bcz): sweep (lcb_z x coverage_fold) cells
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridCell:
    """One (lcb_z, coverage_fold) point of the calibration grid."""

    lcb_z: float
    coverage_fold: str

    @property
    def config(self) -> RootCalcConfig:
        return RootCalcConfig(lcb_z=self.lcb_z, coverage_fold=self.coverage_fold)

    @property
    def is_baseline(self) -> bool:
        """The current model: no LCB shrinkage, no coverage gate."""
        return self.lcb_z == 0.0 and self.coverage_fold == "off"

    @property
    def label(self) -> str:
        return f"lcb_z={self.lcb_z:g},cov={self.coverage_fold}"


# The current-model cell every grid is measured against.
BASELINE_CELL = GridCell(0.0, "off")


def parse_lcb_z_grid(raw: str | None) -> list[float]:
    if not raw:
        return list(DEFAULT_LCB_Z_GRID)
    return [float(token) for token in raw.split(",") if token.strip()]


def parse_coverage_grid(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_COVERAGE_GRID)
    modes = [token.strip() for token in raw.split(",") if token.strip()]
    unknown = [mode for mode in modes if mode not in COVERAGE_FOLD_MODES]
    if unknown:
        raise ValueError(
            f"unknown coverage_fold mode(s) {unknown}; "
            f"valid modes: {sorted(COVERAGE_FOLD_MODES)}"
        )
    return modes


def build_grid(lcb_zs: list[float], coverage_folds: list[str]) -> list[GridCell]:
    """Cartesian product of the two axes, with the baseline cell first & deduped."""
    cells: list[GridCell] = [BASELINE_CELL]
    seen: set[GridCell] = {BASELINE_CELL}
    for coverage_fold in coverage_folds:
        for lcb_z in lcb_zs:
            cell = GridCell(lcb_z, coverage_fold)
            if cell not in seen:
                seen.add(cell)
                cells.append(cell)
    return cells


def score_pair_grid(
    db,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    roots: OpeningRoots,
    grid: list[GridCell],
) -> dict[GridCell, PairScore]:
    """Build a pair's overlay ONCE (the ~2.6s cost), then score it per grid cell.

    Naively gridding would rebuild ``overlay_evidence`` O(pairs x cells) times; here
    the (DB-read) overlay is built once and only the cheap in-memory
    ``score_overlay`` runs per cell.
    """
    overlay = overlay_evidence(db, user_id, player_color, graph)
    return {
        cell: score_overlay(user_id, player_color, graph, overlay, roots, cell.config)
        for cell in grid
    }


def pair_key_deltas(
    cell_score: PairScore, baseline_score: PairScore
) -> list[dict[str, object]]:
    """Per-opening-key delta records vs baseline, for keys present in both.

    Each record keeps the opening key, its baseline and cell scores, and the delta,
    so the report can emit true per-pair per-opening-key deltas (not just a summary).
    """
    base = baseline_score.named_score_map
    return [
        {
            "opening_key": key,
            "baseline": base[key],
            "cell": value,
            "delta": value - base[key],
        }
        for key, value in cell_score.named_score_map.items()
        if key in base
    ]


def summarize_deltas(values: list[float]) -> dict[str, object]:
    """Count + mean + percentiles for signed deltas (no 0-100 histogram)."""
    return {
        "count": len(values),
        "mean": (sum(values) / len(values)) if values else None,
        "percentiles": percentiles(values),
    }


# ---------------------------------------------------------------------------
# Report assembly + rendering
# ---------------------------------------------------------------------------


def build_report(
    pair_scores: list[PairScore],
    *,
    min_observations: int,
    named_root_count: int,
    write_bench: dict[str, object] | None = None,
) -> dict[str, object]:
    """Aggregate per-pair results into a calibration report dict.

    Two aggregation scopes, deliberately separated:

    * **Score distributions** use only the ``included`` cohort (pairs at/above
      ``min_observations``), so sparse one-game accounts cannot skew percentiles.
    * **Telemetry** (source mix, excluded sessions, horizon, recursion,
      throughput) aggregates over **all candidate pairs**. Per §1 the
      early-return telemetry is well-formed for empty/low-evidence pairs, so the
      structural counts (raw-middlegame roots, recursion key classes) must still
      surface even when the included cohort is empty.
    """
    included = [p for p in pair_scores if p.observation_total >= min_observations]
    below = [p for p in pair_scores if p.observation_total < min_observations]

    pooled_named = [score for p in included for score in p.named_scores]
    synthetic_scores = [
        p.synthetic_score for p in included if p.synthetic_score is not None
    ]

    # Per-pair: each included pair's full score distribution (preserves p5..p95
    # shape, not just the median), plus a summary of the per-pair medians so a
    # broad-tree user with hundreds of rows cannot dominate the pooled view.
    per_pair = [
        {
            "user_id": p.user_id,
            "player_color": p.player_color,
            "observations": p.observation_total,
            "summary": summarize(p.named_scores),
        }
        for p in included
    ]
    per_user_medians = [
        m
        for p in included
        if (m := percentile(sorted(p.named_scores), 50.0)) is not None
    ]

    # Telemetry aggregates over ALL candidate pairs (see docstring).
    aggregated_sources: Counter[str] = Counter()
    excluded_sessions_total = 0
    opening_interval_lens: list[float] = []
    middle_reached = 0
    phase_sample_total = 0
    for p in pair_scores:
        aggregated_sources.update(p.source_counts)
        excluded_sessions_total += p.excluded_sessions
        for sample in p.phase_samples:
            phase_sample_total += 1
            opening_interval_lens.append(float(sample.opening_interval_len))
            if sample.middle_ply is not None:
                middle_reached += 1

    actual_keys = [float(p.telemetry.actual_key_count) for p in pair_scores]
    perfect_keys = [float(p.telemetry.perfect_key_count) for p in pair_scores]
    misses = [float(p.telemetry.calculation_misses) for p in pair_scores]
    unscored_roots = [float(p.telemetry.unscored_root_count) for p in pair_scores]
    raw_middlegame_root_count = max(
        (p.telemetry.raw_middlegame_root_count for p in pair_scores), default=0
    )

    # Per-pair scoring latency vs the documented < 5s/pair gate.
    scoring_seconds = sorted(p.scoring_seconds for p in pair_scores)
    scoring_max = max(scoring_seconds, default=0.0)
    emitted_rows = [float(p.emitted_row_count) for p in pair_scores]

    cache_read_ms = None
    if write_bench is not None:
        cache_read_ms = write_bench.get("cache_read_ms")

    return {
        "cohort": {
            "candidate_pairs": len(pair_scores),
            "min_observations": min_observations,
            "included_pairs": len(included),
            "excluded_low_evidence_pairs": [
                {
                    "user_id": p.user_id,
                    "player_color": p.player_color,
                    "observations": p.observation_total,
                }
                for p in below
            ],
        },
        "named_score_distribution": {
            # Pooled rows correlate via shared ancestor/descendant FENs.
            "pooled": summarize(pooled_named),
            "per_user_median_summary": summarize(per_user_medians),
            "per_pair": per_pair,
        },
        "synthetic_hero_distribution": summarize(synthetic_scores),
        "source_mix": source_mix(aggregated_sources),
        "excluded_sessions_total": excluded_sessions_total,
        "horizon": {
            "phase_samples": phase_sample_total,
            "opening_interval_len": {
                "mean": (
                    sum(opening_interval_lens) / len(opening_interval_lens)
                    if opening_interval_lens
                    else None
                ),
                "percentiles": percentiles(opening_interval_lens),
            },
            "sessions_reaching_middlegame": middle_reached,
            # Two distinct numbers — a raw-middlegame root may still be scored.
            "raw_middlegame_root_count": raw_middlegame_root_count,
            "unscored_root_count": {
                "mean": (sum(unscored_roots) / len(unscored_roots)) if unscored_roots else None,
                "max": max(unscored_roots, default=0.0),
            },
        },
        "recursion": {
            # Reported per key class: _metrics is keyed (fen, perfect), so the
            # natural and perfect passes are counted apart, not conflated.
            "named_root_count": named_root_count,
            "actual_key_count": {
                "mean": (sum(actual_keys) / len(actual_keys)) if actual_keys else None,
                "max": max(actual_keys, default=0.0),
            },
            "perfect_key_count": {
                "mean": (sum(perfect_keys) / len(perfect_keys)) if perfect_keys else None,
                "max": max(perfect_keys, default=0.0),
            },
            "calculation_misses": {
                "mean": (sum(misses) / len(misses)) if misses else None,
                "max": max(misses, default=0.0),
            },
        },
        "throughput": {
            "total_scoring_seconds": sum(scoring_seconds),
            "scoring_seconds_per_pair": {
                "median": percentile(scoring_seconds, 50.0),
                "p95": percentile(scoring_seconds, 95.0),
                "max": scoring_max,
            },
            "emitted_row_count": {
                "total": int(sum(emitted_rows)),
                "mean": (sum(emitted_rows) / len(emitted_rows)) if emitted_rows else None,
            },
        },
        "gates": {
            "scoring_latency_seconds": SCORING_LATENCY_GATE_SECONDS,
            "scoring_latency_pass": (
                scoring_max < SCORING_LATENCY_GATE_SECONDS if scoring_seconds else None
            ),
            "cache_read_ms": CACHE_READ_GATE_MS,
            "cache_read_pass": (
                cache_read_ms < CACHE_READ_GATE_MS if cache_read_ms is not None else None
            ),
        },
        "parameters": {"tau_wc": TAU_WC, "tau_cp": TAU_CP},
        "write_bench": write_bench,
    }


# ---------------------------------------------------------------------------
# Grid report: per-cell distributions + per-pair per-key deltas vs baseline
# ---------------------------------------------------------------------------


def build_cell_report(
    cell: GridCell,
    pair_scores: list[PairScore],
    baseline_scores: list[PairScore],
    *,
    min_observations: int,
) -> dict[str, object]:
    """One grid cell's distribution + (for non-baseline cells) deltas vs baseline.

    ``pair_scores`` and ``baseline_scores`` are aligned per pair (same order); a
    pair is included when its observation total clears ``min_observations`` (the
    total is config-independent, so the baseline row decides membership).
    """
    paired = [
        (cell_p, base_p)
        for cell_p, base_p in zip(pair_scores, baseline_scores)
        if base_p.observation_total >= min_observations
    ]
    pooled = [score for cell_p, _ in paired for score in cell_p.named_scores]
    synthetic = [
        cell_p.synthetic_score for cell_p, _ in paired if cell_p.synthetic_score is not None
    ]
    report: dict[str, object] = {
        "lcb_z": cell.lcb_z,
        "coverage_fold": cell.coverage_fold,
        "baseline": cell.is_baseline,
        "named_score_distribution": summarize(pooled),
        "synthetic_hero_distribution": summarize(synthetic),
    }
    if not cell.is_baseline:
        per_pair: list[dict[str, object]] = []
        pooled_deltas: list[float] = []
        for cell_p, base_p in paired:
            records = pair_key_deltas(cell_p, base_p)
            values = [record["delta"] for record in records]
            pooled_deltas.extend(values)
            per_pair.append(
                {
                    "user_id": cell_p.user_id,
                    "player_color": cell_p.player_color,
                    # Full per-opening-key deltas (opening_key/baseline/cell/delta).
                    "keys": records,
                    "summary": summarize_deltas(values),
                }
            )
        report["deltas_vs_baseline"] = {
            "pooled": summarize_deltas(pooled_deltas),
            "per_pair": per_pair,
        }
    return report


def build_grid_report(
    grid: list[GridCell],
    pair_grids: list[dict[GridCell, PairScore]],
    *,
    min_observations: int,
) -> dict[str, object]:
    """Per-cell distributions + deltas across the whole grid."""
    baseline_scores = [pg[BASELINE_CELL] for pg in pair_grids]
    cells = [
        build_cell_report(
            cell,
            [pg[cell] for pg in pair_grids],
            baseline_scores,
            min_observations=min_observations,
        )
        for cell in grid
    ]
    return {"cells": cells}


# ---------------------------------------------------------------------------
# PASS/FAIL calibration diagnostics (synthetic overlays scored across the grid)
# ---------------------------------------------------------------------------


def _diag_fen(board: chess.Board) -> str:
    return normalize_fen(board.fen())


def _diag_positions(moves: list[str]) -> list[str]:
    board = chess.Board()
    result = [_diag_fen(board)]
    for uci in moves:
        board.push_uci(uci)
        result.append(_diag_fen(board))
    return result


def _diag_graph(paths: list[list[str]]) -> OpeningGraph:
    nodes: dict[str, OpeningGraphNode] = {}
    root_fen = _diag_fen(chess.Board())
    for moves in paths:
        board = chess.Board()
        parent = _diag_fen(board)
        nodes.setdefault(parent, OpeningGraphNode(parent, active_color(parent)))
        for uci in moves:
            board.push_uci(uci)
            child = _diag_fen(board)
            nodes.setdefault(child, OpeningGraphNode(child, active_color(child)))
            nodes[parent].children[uci] = child
            nodes[child].parents.add((parent, uci))
            parent = child
    return OpeningGraph(nodes, root_fen)


def _diag_roots(*fens: str) -> OpeningRoots:
    roots = {
        fen: OpeningRoot(
            opening_key=fen,
            opening_name="Diagnostic",
            opening_family="__diag__",
            eco=None,
            depth=0,
            parent_keys=frozenset(),
            child_keys=frozenset(),
        )
        for fen in fens
    }
    return OpeningRoots(roots, {fen: frozenset([fen]) for fen in fens})


def _specialist_scenario() -> tuple[OpeningGraph, EvidenceOverlay, OpeningRoots, str]:
    """One-variation specialist: strong in ONE opponent reply, unprepared for the
    siblings. The scored root is the opponent node after 1.e4 (black to move)."""
    opp = _diag_positions(["e2e4"])[1]
    strong = _diag_positions(["e2e4", "e7e5"])[2]
    graph = _diag_graph(
        [["e2e4", "e7e5"], ["e2e4", "c7c5"], ["e2e4", "e7e6"]]
    )
    overlay = EvidenceOverlay(0, "white")
    overlay.nodes[strong] = NodeEvidence(
        fen=strong, quality_sum=4.0, quality_count=4, live_attempts=4
    )
    return graph, overlay, _diag_roots(opp), opp


def _broad_guard_scenario() -> tuple[OpeningGraph, EvidenceOverlay, OpeningRoots, str]:
    """Broadly-prepared player: real, covered prep across ALL opponent replies."""
    opp = _diag_positions(["e2e4"])[1]
    replies = [
        _diag_positions(["e2e4", "e7e5"])[2],
        _diag_positions(["e2e4", "c7c5"])[2],
        _diag_positions(["e2e4", "e7e6"])[2],
    ]
    graph = _diag_graph(
        [["e2e4", "e7e5"], ["e2e4", "c7c5"], ["e2e4", "e7e6"]]
    )
    overlay = EvidenceOverlay(0, "white")
    for reply in replies:
        overlay.nodes[reply] = NodeEvidence(
            fen=reply, quality_sum=4.0, quality_count=4, live_attempts=4
        )
    return graph, overlay, _diag_roots(opp), opp


def _cliff_scenario(
    *, reviewed: bool
) -> tuple[OpeningGraph, EvidenceOverlay, OpeningRoots, str]:
    """Thin-but-earned branch. Evidence is concentrated at ONE node (subtree
    live=1) so it respects subtree-SUM semantics: a single live attempt fails the
    gate at coverage_live_threshold>=2 until one review lands. Smearing it across
    two nodes would reach live>=2 and silently PASS the gate — no cliff."""
    opp = _diag_positions(["e2e4"])[1]
    reply = _diag_positions(["e2e4", "e7e5"])[2]
    graph = _diag_graph([["e2e4", "e7e5"]])
    overlay = EvidenceOverlay(0, "white")
    overlay.nodes[reply] = NodeEvidence(
        fen=reply,
        quality_sum=0.9,
        quality_count=1,
        live_attempts=1,
        review_attempts=1 if reviewed else 0,
    )
    return graph, overlay, _diag_roots(opp), opp


def _score_target(
    target: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig,
) -> float:
    return compute_root_score(
        target, "white", graph, overlay, roots, config
    ).opening_score


def run_specialist_diagnostic(grid: list[GridCell]) -> dict[str, object]:
    """TRUE-POSITIVE gate: a one-variation specialist must drop from ~B to ~D/F
    once the coverage fold is on."""
    graph, overlay, roots, target = _specialist_scenario()
    cells = [
        {
            "lcb_z": cell.lcb_z,
            "coverage_fold": cell.coverage_fold,
            "baseline": cell.is_baseline,
            "score": _score_target(target, graph, overlay, roots, cell.config),
        }
        for cell in grid
    ]
    baseline = next(c for c in cells if c["baseline"])
    fold_on = [c for c in cells if c["coverage_fold"] != "off"]
    passed: bool | None = None
    if fold_on:
        passed = baseline["score"] >= GRADE_B and all(
            c["score"] < GRADE_C for c in fold_on
        )
    return {
        "name": "one-variation specialist (true-positive)",
        "baseline_score": baseline["score"],
        "cells": cells,
        "passed": passed,
    }


def run_broad_guard_diagnostic(grid: list[GridCell]) -> dict[str, object]:
    """FALSE-POSITIVE guard: a genuinely broadly-prepared player must NOT crater
    (stays ~B+) once the fold is on. Run against every cell so both ``gate`` and
    ``gate_x_cov`` are covered; if this cannot pass under uniform opponent weights
    at any acceptable z/mode, that is the trip wire to bring g-idgs forward."""
    graph, overlay, roots, target = _broad_guard_scenario()
    cells = [
        {
            "lcb_z": cell.lcb_z,
            "coverage_fold": cell.coverage_fold,
            "baseline": cell.is_baseline,
            "score": _score_target(target, graph, overlay, roots, cell.config),
        }
        for cell in grid
    ]
    fold_on = [c for c in cells if c["coverage_fold"] != "off"]
    passed: bool | None = (
        all(c["score"] >= GRADE_B for c in fold_on) if fold_on else None
    )
    return {
        "name": "broadly-prepared mainlines (false-positive guard)",
        "cells": cells,
        "passed": passed,
    }


def run_cliff_diagnostic(
    grid: list[GridCell], thresholds: tuple[int, ...] = (1, 2)
) -> dict[str, object]:
    """Thin-but-earned cliff: score the same branch at live=1/review=0 vs
    live=1/review=1, sweeping ``coverage_live_threshold`` so the size of the
    0->full-credit jump (and whether crediting a single live pass softens it) is
    visible before grades are recalibrated."""
    rows: list[dict[str, object]] = []
    for cell in grid:
        for threshold in thresholds:
            config = RootCalcConfig(
                lcb_z=cell.lcb_z,
                coverage_fold=cell.coverage_fold,
                coverage_live_threshold=threshold,
            )
            g0, o0, r0, t0 = _cliff_scenario(reviewed=False)
            g1, o1, r1, t1 = _cliff_scenario(reviewed=True)
            thin = _score_target(t0, g0, o0, r0, config)
            after_review = _score_target(t1, g1, o1, r1, config)
            rows.append(
                {
                    "lcb_z": cell.lcb_z,
                    "coverage_fold": cell.coverage_fold,
                    "coverage_live_threshold": threshold,
                    "thin_score": thin,
                    "reviewed_score": after_review,
                    "jump": after_review - thin,
                }
            )

    # PASS confirms the synthetic actually reproduces the cliff (rather than
    # silently passing the gate): under the gate at the current threshold=2 the
    # thin state fails (~0) and jumps up after one review, while crediting a single
    # live pass (threshold=1) removes the jump. Evaluated at the lowest gate z so
    # the check is independent of which z values the grid sweeps.
    def _gate_row(threshold: int) -> dict[str, object] | None:
        candidates = [
            row
            for row in rows
            if row["coverage_fold"] == "gate"
            and row["coverage_live_threshold"] == threshold
        ]
        return min(candidates, key=lambda row: row["lcb_z"]) if candidates else None

    gate2 = _gate_row(2)
    gate1 = _gate_row(1)
    passed: bool | None = None
    if gate2 is not None and gate1 is not None:
        passed = (
            gate2["thin_score"] < 1e-6
            and gate2["reviewed_score"] > gate2["thin_score"]
            and abs(gate1["thin_score"] - gate1["reviewed_score"]) < 1e-6
        )
    return {
        "name": "thin-but-earned cliff (0->full-credit jump)",
        "rows": rows,
        "passed": passed,
    }


def run_diagnostics(grid: list[GridCell]) -> dict[str, object]:
    return {
        "specialist": run_specialist_diagnostic(grid),
        "broad_guard": run_broad_guard_diagnostic(grid),
        "cliff": run_cliff_diagnostic(grid),
    }


def render_text(report: dict[str, object]) -> str:
    """Render the report dict as a plain-text summary."""
    cohort = report["cohort"]
    named = report["named_score_distribution"]
    lines: list[str] = []
    lines.append("=== Opening score v2 calibration ===")
    lines.append(
        f"Candidate pairs: {cohort['candidate_pairs']} | "
        f"included (>= {cohort['min_observations']} obs): {cohort['included_pairs']} | "
        f"low-evidence: {len(cohort['excluded_low_evidence_pairs'])}"
    )
    params = report["parameters"]
    lines.append(f"tau_wc={params['tau_wc']} tau_cp={params['tau_cp']}")
    lines.append("")
    lines.append("-- Named-root scores (pooled; rows correlate via shared FENs) --")
    lines.append(_fmt_summary(named["pooled"]))
    lines.append("-- Named-root scores (per-user median, summarized) --")
    lines.append(_fmt_summary(named["per_user_median_summary"]))
    per_pair = named["per_pair"]
    lines.append(f"-- Per-pair named-root distributions ({len(per_pair)} pairs) --")
    for entry in per_pair:
        lines.append(
            f"  user {entry['user_id']}/{entry['player_color']} "
            f"({entry['observations']} obs): {_fmt_summary(entry['summary']).strip()}"
        )
    lines.append("-- Synthetic hero row (__repertoire__) --")
    lines.append(_fmt_summary(report["synthetic_hero_distribution"]))
    lines.append("")
    mix = report["source_mix"]
    if mix["total"] == 0:
        lines.append("Source mix: (no quality observations)")
    else:
        parts = ", ".join(
            f"{key} {pct:.1f}%" for key, pct in sorted(mix["pct"].items())
        )
        lines.append(f"Source mix ({mix['total']} obs): {parts}")
    lines.append(f"Excluded sessions (broken continuity): {report['excluded_sessions_total']}")
    lines.append("")
    horizon = report["horizon"]
    lines.append(
        f"Horizon: {horizon['phase_samples']} samples, "
        f"mean opening interval {_fmt_opt(horizon['opening_interval_len']['mean'])}, "
        f"{horizon['sessions_reaching_middlegame']} reached middlegame"
    )
    lines.append(
        f"Raw-middlegame roots: {horizon['raw_middlegame_root_count']} | "
        f"unscored roots mean {_fmt_opt(horizon['unscored_root_count']['mean'])} "
        f"(max {horizon['unscored_root_count']['max']:.0f})"
    )
    rec = report["recursion"]
    lines.append(
        f"Recursion: named roots {rec['named_root_count']} | "
        f"actual keys mean {_fmt_opt(rec['actual_key_count']['mean'])} | "
        f"perfect keys mean {_fmt_opt(rec['perfect_key_count']['mean'])} | "
        f"misses mean {_fmt_opt(rec['calculation_misses']['mean'])}"
    )
    tp = report["throughput"]
    per_pair_latency = tp["scoring_seconds_per_pair"]
    lines.append(
        f"Throughput: {tp['total_scoring_seconds']:.3f}s total, "
        f"{tp['emitted_row_count']['total']} rows emitted | "
        f"per-pair scoring median {_fmt_opt(per_pair_latency['median'])}s "
        f"max {_fmt_opt(per_pair_latency['max'])}s"
    )
    gates = report["gates"]
    lines.append(
        f"Gates: scoring < {gates['scoring_latency_seconds']}s/pair "
        f"[{_fmt_gate(gates['scoring_latency_pass'])}] | "
        f"cache read < {gates['cache_read_ms']}ms "
        f"[{_fmt_gate(gates['cache_read_pass'])}]"
    )
    if report.get("write_bench"):
        wb = report["write_bench"]
        lines.append("")
        lines.append(
            f"Write-bench: cache read {wb.get('cache_read_ms')} ms over "
            f"{wb.get('cached_rows')} cached rows (db={wb.get('database_url')})"
        )
    if report.get("grid"):
        lines.append("")
        _render_grid(report["grid"], lines)
    if report.get("diagnostics"):
        lines.append("")
        _render_diagnostics(report["diagnostics"], lines)
    return "\n".join(lines)


def _cell_label(entry: dict[str, object]) -> str:
    base = " [baseline]" if entry.get("baseline") else ""
    return f"lcb_z={entry['lcb_z']:g},cov={entry['coverage_fold']}{base}"


# Text output shows only the N largest-magnitude per-key movers per cell; the
# FULL per-pair per-key deltas are always in the --json report.
DELTA_TEXT_LIMIT = 8


def _render_grid(grid: dict[str, object], lines: list[str]) -> None:
    lines.append("=== Calibration grid (per cell vs baseline) ===")
    for cell in grid["cells"]:
        lines.append(f"-- {_cell_label(cell)} --")
        lines.append(
            "  named:" + _fmt_summary(cell["named_score_distribution"]).rstrip()
        )
        lines.append(
            "  synthetic:" + _fmt_summary(cell["synthetic_hero_distribution"]).rstrip()
        )
        deltas = cell.get("deltas_vs_baseline")
        if deltas is not None:
            pooled = deltas["pooled"]
            pcts = pooled["percentiles"]
            lines.append(
                f"  Δ vs baseline (per key): n={pooled['count']} "
                f"mean={_fmt_opt(pooled['mean'])} "
                f"p5={_fmt_opt(pcts['p5'])} p50={_fmt_opt(pcts['p50'])} "
                f"p95={_fmt_opt(pcts['p95'])}"
            )
            _render_top_movers(deltas["per_pair"], lines)


def _render_top_movers(per_pair: list[dict[str, object]], lines: list[str]) -> None:
    """Render the largest-|Δ| per-pair per-key movers (bounded; full set in --json)."""
    movers = [
        (pair["user_id"], pair["player_color"], record)
        for pair in per_pair
        for record in pair["keys"]
    ]
    if not movers:
        return
    movers.sort(key=lambda item: abs(item[2]["delta"]), reverse=True)
    for user_id, color, record in movers[:DELTA_TEXT_LIMIT]:
        lines.append(
            f"    {user_id}/{color} {record['opening_key']}: "
            f"{_fmt_opt(record['baseline'])}→{_fmt_opt(record['cell'])} "
            f"(Δ{record['delta']:+.1f})"
        )
    remaining = len(movers) - DELTA_TEXT_LIMIT
    if remaining > 0:
        lines.append(
            f"    … {remaining} more per-key deltas (full set in --json)"
        )


def _render_diagnostic_cells(cells: list[dict[str, object]], lines: list[str]) -> None:
    for cell in cells:
        lines.append(f"    {_cell_label(cell)}: score={_fmt_opt(cell['score'])}")


def _render_diagnostics(diagnostics: dict[str, object], lines: list[str]) -> None:
    lines.append("=== Calibration diagnostics (PASS/FAIL) ===")

    specialist = diagnostics["specialist"]
    lines.append(
        f"[{_fmt_gate(specialist['passed'])}] {specialist['name']} "
        f"(baseline {_fmt_opt(specialist['baseline_score'])} → B; "
        f"fold-on must drop < {GRADE_C:g} = D/F)"
    )
    _render_diagnostic_cells(specialist["cells"], lines)

    guard = diagnostics["broad_guard"]
    lines.append(
        f"[{_fmt_gate(guard['passed'])}] {guard['name']} "
        f"(fold-on must stay ≥ {GRADE_B:g} = B+)"
    )
    _render_diagnostic_cells(guard["cells"], lines)

    cliff = diagnostics["cliff"]
    lines.append(f"[{_fmt_gate(cliff['passed'])}] {cliff['name']}")
    for row in cliff["rows"]:
        base = " [baseline]" if row["lcb_z"] == 0.0 and row["coverage_fold"] == "off" else ""
        lines.append(
            f"    lcb_z={row['lcb_z']:g},cov={row['coverage_fold']},"
            f"live_thr={row['coverage_live_threshold']}{base}: "
            f"thin={_fmt_opt(row['thin_score'])} "
            f"reviewed={_fmt_opt(row['reviewed_score'])} "
            f"jump={_fmt_opt(row['jump'])}"
        )


def _fmt_gate(passed: bool | None) -> str:
    if passed is None:
        return "n/a"
    return "PASS" if passed else "FAIL"


def _fmt_summary(summary: dict[str, object]) -> str:
    pcts = summary["percentiles"]
    return (
        f"  n={summary['count']} mean={_fmt_opt(summary['mean'])} "
        f"p5={_fmt_opt(pcts['p5'])} p25={_fmt_opt(pcts['p25'])} "
        f"p50={_fmt_opt(pcts['p50'])} p75={_fmt_opt(pcts['p75'])} "
        f"p95={_fmt_opt(pcts['p95'])} hist={summary['histogram']}"
    )


def _fmt_opt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=DATABASE_URL)
    parser.add_argument(
        "--min-observations",
        type=int,
        default=DEFAULT_MIN_OBSERVATIONS,
        help="Quality observations required to include a pair in distribution stats.",
    )
    parser.add_argument(
        "--users",
        default=None,
        help="Comma-separated user_ids to restrict the run to.",
    )
    parser.add_argument(
        "--pairs",
        default=None,
        help="Comma-separated user_id:color pairs to restrict the run to.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Limit candidate pairs.")
    parser.add_argument(
        "--lcb-z-grid",
        default=None,
        help='Comma-separated lcb_z values to sweep (default "0,1.0,1.28"). The '
        "baseline lcb_z=0.0 is always included.",
    )
    parser.add_argument(
        "--coverage-grid",
        default=None,
        help='Comma-separated coverage_fold modes to sweep '
        '(default "off,gate,gate_x_cov"). Baseline "off" is always included.',
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    parser.add_argument(
        "--write-bench",
        action="store_true",
        help="Run one isolated recompute + time cache reads (requires --allow-writes "
        "and a guarded --database-url).",
    )
    parser.add_argument(
        "--allow-writes",
        action="store_true",
        help="Required alongside --write-bench to acknowledge DB writes.",
    )
    return parser.parse_args(argv)


def _parse_user_filter(users: str | None) -> set[int] | None:
    if not users:
        return None
    return {int(token) for token in users.split(",") if token.strip()}


def _parse_pair_filter(pairs: str | None) -> set[tuple[int, str]] | None:
    if not pairs:
        return None
    result: set[tuple[int, str]] = set()
    for token in pairs.split(","):
        token = token.strip()
        if not token:
            continue
        user_part, color = token.split(":", 1)
        result.add((int(user_part), color))
    return result


def select_pairs(
    candidate_pairs: list[tuple[int, str]],
    *,
    users: set[int] | None,
    pairs: set[tuple[int, str]] | None,
) -> list[tuple[int, str]]:
    """Apply the --users / --pairs filters to the candidate list."""
    selected = candidate_pairs
    if users is not None:
        selected = [p for p in selected if p[0] in users]
    if pairs is not None:
        selected = [p for p in selected if p in pairs]
    return selected


def run_write_bench(db, user_id: int, player_color: str, database_url: str) -> dict[str, object]:
    """Persist one batch on the (already guarded) isolated DB and time a cache read."""
    from app.opening_cache import list_cached_opening_scores, recompute_opening_scores

    recompute_opening_scores(db, user_id, player_color)
    started = time.perf_counter()
    batch, rows = list_cached_opening_scores(db, user_id, player_color)
    cache_read_ms = (time.perf_counter() - started) * 1000.0
    return {
        "database_url": database_url,
        "user_id": user_id,
        "player_color": player_color,
        "cache_read_ms": round(cache_read_ms, 3),
        "cached_rows": len(rows),
        "batch_id": batch.id if batch is not None else None,
    }


def main(argv: list[str] | None = None, *, session_factory=None) -> dict[str, object]:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.write_bench:
        if not args.allow_writes:
            raise SystemExit("--write-bench requires --allow-writes")
        validate_write_bench_database_url(args.database_url, DATABASE_URL)

    if session_factory is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(args.database_url, pool_pre_ping=True)
        session_factory = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

    from app.opening_cache import list_opening_score_candidate_pairs

    graph = get_opening_graph()
    roots = get_opening_roots()
    named_root_count = _named_root_count(roots)

    grid = build_grid(
        parse_lcb_z_grid(args.lcb_z_grid), parse_coverage_grid(args.coverage_grid)
    )

    users = _parse_user_filter(args.users)
    pairs = _parse_pair_filter(args.pairs)

    with session_factory() as db:
        candidate_pairs = list_opening_score_candidate_pairs(db, limit=args.limit)
        selected = select_pairs(candidate_pairs, users=users, pairs=pairs)

        # Build each pair's overlay ONCE and score it for every grid cell.
        pair_grids = [
            score_pair_grid(db, user_id, player_color, graph, roots, grid)
            for user_id, player_color in selected
        ]

        write_bench = None
        if args.write_bench and selected:
            bench_user, bench_color = selected[0]
            write_bench = run_write_bench(db, bench_user, bench_color, args.database_url)

    # The baseline cell (current model) drives the top-level distribution/telemetry;
    # the grid section adds every other cell plus per-key deltas vs that baseline.
    baseline_scores = [pg[BASELINE_CELL] for pg in pair_grids]
    report = build_report(
        baseline_scores,
        min_observations=args.min_observations,
        named_root_count=named_root_count,
        write_bench=write_bench,
    )
    report["grid"] = build_grid_report(
        grid, pair_grids, min_observations=args.min_observations
    )
    report["diagnostics"] = run_diagnostics(grid)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return report


def _named_root_count(roots: OpeningRoots) -> int:
    from app.opening_rootcalc import _iter_named_roots

    return len(_iter_named_roots(roots))


if __name__ == "__main__":
    main()
