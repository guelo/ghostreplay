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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import chess  # noqa: E402

from app.db import DATABASE_URL  # noqa: E402
from app.fen import active_color, normalize_fen  # noqa: E402
from app.opening_evidence import (  # noqa: E402
    EdgeEvidence,
    EvidenceOverlay,
    NodeEvidence,
    PhaseSample,
    overlay_evidence,
)
from app.opening_graph import OpeningGraph, OpeningGraphNode, get_opening_graph  # noqa: E402
from app.opening_quality import TAU_CP, TAU_WC  # noqa: E402
from app.opening_rootcalc import (  # noqa: E402
    SYNTHETIC_INITIAL_FEN,
    CalcTelemetry,
    NodeDebug,
    RootCalcConfig,
    RootScore,
    compute_all_root_scores,
    compute_root_score,
    root_calc_config_fingerprint,
)
from app.opening_roots import OpeningRoot, OpeningRoots, get_opening_roots  # noqa: E402

DEFAULT_MIN_OBSERVATIONS = 20
HISTOGRAM_EDGES = (0.0, 20.0, 40.0, 60.0, 80.0, 100.0)
DEFAULT_PERCENTILES = (5.0, 25.0, 50.0, 75.0, 95.0)

# Fixed release-diagnostic gates from the pre-readiness display bands. These are
# intentionally NOT the current display grades in src/openings/format.ts: they
# keep the specialist/broad-prep PASS/FAIL tests anchored to the human calibration
# question that chose the fold values.
GRADE_A, GRADE_B, GRADE_C, GRADE_D = 50.0, 38.0, 28.0, 22.0

# Documented numeric release gates (openingscore_final.md "Calibration Outcome").
SCORING_LATENCY_GATE_SECONDS = 5.0
CACHE_READ_GATE_MS = 50.0


# ---------------------------------------------------------------------------
# Grade-decoupling pure primitives (g-p4ih.1.2): ordinal grade rank, fixed diagnostic
# bands, derived-cutoff grading, and the opponent-guard / leak tolerance constants.
# All grade-free where noted; consumed by the paired diagnostics here and by
# g-p4ih-selection downstream.
# ---------------------------------------------------------------------------

# A > B > C > D > F is NOT the lexicographic order of the letters, so grade
# comparisons MUST go through this single ordinal rank (best -> worst = increasing
# distance from A) rather than a bare string <=. rank(A)=0 .. rank(F)=4.
GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}


def grade_rank(letter: str) -> int:
    """Ordinal rank of an A-F grade (0=A best .. 4=F worst); fail-closed otherwise.

    The "No Data" / null grade never enters the tolerance gates (every gated score is
    a finite 0..100 number), so this covers A-F only and fails closed on anything
    else rather than silently ranking it. (The frontend gradeRank mirror returns null
    + reject-before-compare instead; that is g-p4ih-cutoff-fixture's.)
    """
    assert letter in GRADE_RANK, f"grade_rank expects an A-F grade; got {letter!r}"
    return GRADE_RANK[letter]


def fixed_band(score: float) -> str:
    """Letter grade from the FIXED diagnostic bands (total over every finite score)."""
    if score >= GRADE_A:
        return "A"
    if score >= GRADE_B:
        return "B"
    if score >= GRADE_C:
        return "C"
    if score >= GRADE_D:
        return "D"
    return "F"


def provisional_grade(score: float, cutoffs: Cutoffs) -> str:
    """Letter grade from the DERIVED per-candidate ``Cutoffs`` (A .. F).

    Requires a ``Cutoffs`` — there is no bare ``provisional_grade(score)`` form.
    Consumed by g-p4ih-selection's User-14 derived-grade gate.
    """
    if score >= cutoffs.a:
        return "A"
    if score >= cutoffs.b:
        return "B"
    if score >= cutoffs.c:
        return "C"
    if score >= cutoffs.d:
        return "D"
    return "F"


# Opponent-drop and leak tolerances (band-anchored rank + raw caps; see g-p4ih
# "Grade decoupling"). The A band [50, inf) and F band [0, 22) are open-ended and the
# real reference scores land in exactly those, so "one band width" is undefined —
# these are pinned as an explicit rank cap plus a raw-point cap, each band-anchored:
#   - OPP_GUARD: one B-band width (50-38), the widest bounded interior band; lenient.
#   - LEAK: one D-band width (28-22), the tightest bounded interior band; strict.
OPP_GUARD_MAX_RANK_DROP = 1
OPP_GUARD_MAX_RAW_DROP_PTS = 12.0
LEAK_MAX_RANK_INCREASE = 1
LEAK_MAX_RAW_INCREASE_PTS = 6.0


def _opp_guard_fires(candidate_opp_score: float, reference_opp_score: float) -> bool:
    """The opponent regression guard FIRES (candidate inadmissible) iff the candidate
    craters more than one rank below the reference OR drops more than the raw cap."""
    return (
        grade_rank(fixed_band(candidate_opp_score))
        > grade_rank(fixed_band(reference_opp_score)) + OPP_GUARD_MAX_RANK_DROP
        or (reference_opp_score - candidate_opp_score) > OPP_GUARD_MAX_RAW_DROP_PTS
    )


def _leak_fires(candidate_pre_fold_quality: float, reference_gated_quality: float) -> bool:
    """The unprepared-branch leak guard FIRES iff the candidate's ungated pre-fold
    quality is more than one rank BETTER than the reference gated quality OR exceeds
    it by more than the raw cap (quality leaking up through a dropped coverage gate)."""
    return (
        grade_rank(fixed_band(candidate_pre_fold_quality))
        < grade_rank(fixed_band(reference_gated_quality)) - LEAK_MAX_RANK_INCREASE
        or (candidate_pre_fold_quality - reference_gated_quality) > LEAK_MAX_RAW_INCREASE_PTS
    )


# ---------------------------------------------------------------------------
# Pure statistics helpers (DB-free; unit-tested in test_calibrate_opening_scores)
# ---------------------------------------------------------------------------


def _interp_percentile(sorted_scores: list[float], q: float) -> float:
    """Type-7 (numpy default) linear-interpolated percentile ``q`` (0-100).

    ASSUMES ``len(sorted_scores) >= 2`` and an already-sorted input. The shared
    interpolation core behind BOTH the public ``percentile`` (which keeps its own
    empty/singleton guards) and the precondition-checked ``_percentiles`` primitive,
    so ordinary reporting and the cutoff/distribution path can never disagree on
    interpolation. Equivalent to ``numpy.percentile(sorted, q, method="linear")``.
    """
    rank = (q / 100.0) * (len(sorted_scores) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_scores[low]
    frac = rank - low
    return sorted_scores[low] * (1.0 - frac) + sorted_scores[high] * frac


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile ``q`` (0-100) of an already-sorted list.

    INTENTIONALLY returns ``None`` for an empty cohort and the lone value for a
    singleton (ordinary cohort/telemetry reporting depends on this); it delegates to
    the shared ``_interp_percentile`` core only on the ``len >= 2`` path, so the
    refactor cannot regress empty/low-evidence reporting.
    """
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    return _interp_percentile(sorted_values, q)


def _percentiles(scores: list[float], qs: tuple[float, ...]) -> tuple[float, ...]:
    """Sorted type-7 percentiles at ``qs`` for a cohort of ``len >= 2`` (raises below).

    The single primitive shared by ``derive_cutoffs`` (qs = 12/25/40/82/95) and
    ``distribution_stats`` (qs = 5/25/50/75/95), so the two can never disagree on
    interpolation. Deliberately does NOT model the empty/singleton None case (that
    stays at the ``percentile`` boundary) and never special-cases an all-equal cohort
    — all-equal rejection belongs ONLY to ``derive_cutoffs`` (via its strict-ordering
    ``CutoffCollision``).
    """
    if len(scores) < 2:
        raise ValueError(f"_percentiles requires at least 2 scores; got {len(scores)}")
    ordered = sorted(scores)
    return tuple(_interp_percentile(ordered, q) for q in qs)


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
# Distribution stats + derived cutoffs (pure; consumed by g-p4ih-selection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistributionStats:
    """Five exact-float quantiles of a candidate's pooled named-root score cohort."""

    p05: float
    p25: float
    p50: float
    p75: float
    p95: float


def distribution_stats(scores: list[float]) -> DistributionStats:
    """Five quantiles via the shared ``_percentiles`` primitive (requires len >= 2).

    UNLIKE ``derive_cutoffs`` this does NOT reject an all-equal cohort: five equal
    quantiles (spread 0) are a valid statistic that simply loses the spread ordering.
    Assumes finite in-range scores; does not re-validate the range (the scorer's
    contract, asserted upstream at load). g-p4ih.1.2 is the SOLE definer of this type
    and function; g-p4ih-selection imports them and must not redefine them.
    """
    p05, p25, p50, p75, p95 = _percentiles(scores, (5.0, 25.0, 50.0, 75.0, 95.0))
    return DistributionStats(p05=p05, p25=p25, p50=p50, p75=p75, p95=p95)


class CutoffCollision(ValueError):
    """A compressed/all-equal cohort collapsed a rounded cutoff band to width 0.

    ALWAYS raised (never a sentinel return) so g-p4ih-selection can catch it and
    record ``rejection_reason="cutoff_collision"``; the candidate is rejected, its
    boundaries are never nudged apart.
    """


@dataclass(frozen=True)
class Cutoffs:
    """Derived grade + tone boundaries for one candidate cell.

    Grade boundaries (F is below ``d``), strictly ``a > b > c > d``; tone boundaries,
    strictly ``watch > alert``. The ONE shape both the selection gates and the emitted
    format.ts numbers read; ``provisional_grade`` consumes exactly these six fields.
    """

    a: int
    b: int
    c: int
    d: int
    alert: int
    watch: int


def _round_half_up(value: float) -> int:
    """Round to the nearest integer, halves UP — NOT Python's banker's round."""
    return int(math.floor(value + 0.5))


def derive_cutoffs(scores: list[float]) -> Cutoffs:
    """Pure, deterministic, validity-checked grade/tone cutoffs for one cell's scores.

    Percentiles by type-7 linear interpolation (the shared ``_percentiles`` primitive),
    each boundary ROUND-HALF-UP. Grades A/B/C/D := p95/p82/p40/p12 (F below d); tones
    alert/watch := p25/p82 (watch and grade-b share p82 by intent — one reused
    boundary, NOT a collision). Requires len(scores) >= 2 (raises ``ValueError`` — a
    precondition the frozen cohort guarantees). Assumes finite in-range scores; does
    not re-validate the range. On any non-strict rounded ordering (a band collapsed to
    width 0, INCLUDING an all-equal cohort, which surfaces through the ordinary
    strict-ordering check) raises ``CutoffCollision`` — the candidate is rejected,
    never nudged.
    """
    if len(scores) < 2:
        raise ValueError(f"derive_cutoffs requires at least 2 scores; got {len(scores)}")
    q12, q25, q40, q82, q95 = _percentiles(scores, (12.0, 25.0, 40.0, 82.0, 95.0))
    cutoffs = Cutoffs(
        a=_round_half_up(q95),
        b=_round_half_up(q82),
        c=_round_half_up(q40),
        d=_round_half_up(q12),
        alert=_round_half_up(q25),
        watch=_round_half_up(q82),
    )
    if not (cutoffs.d < cutoffs.c < cutoffs.b < cutoffs.a):
        raise CutoffCollision(
            f"non-strict grade ordering d<c<b<a: {cutoffs.d} < {cutoffs.c} < "
            f"{cutoffs.b} < {cutoffs.a}"
        )
    if not cutoffs.alert < cutoffs.watch:
        raise CutoffCollision(
            f"non-strict tone ordering alert<watch: {cutoffs.alert} < {cutoffs.watch}"
        )
    return cutoffs


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
# Calibration grid (g-zc3p / g-p4ih): anchor-first arm structure over the fold axes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridCell:
    """One point of the readiness-fold calibration grid.

    Six behavioral axes. The two existing positional fields (lcb_z, coverage_fold)
    stay first so every current 2-arg ``GridCell(lcb_z, coverage_fold)`` call keeps
    compiling; the four additions default to their identity values, so a bare 2-arg
    cell is a no-fold cell (threshold=1, p=0, keep). ``__post_init__`` canonicalizes
    the INERT report_fold_scope axis (inert when report_fold_p == 0) so GridCell's
    native ``eq``/``hash`` IS the behavioral key — ``set[GridCell]`` dedupe and
    ``dict[GridCell, PairScore]`` scoring maps collapse behaviorally-equal cells with
    zero call-site discipline. report_self_term IS a genuine behavioral axis
    ("drop_user" changes the number) and is NEVER normalized; lcb_z / coverage_fold /
    coverage_live_threshold are always behavioral and NEVER normalized.
    """

    lcb_z: float
    coverage_fold: str
    coverage_live_threshold: int = 1
    report_fold_p: float = 0.0
    report_fold_scope: str = "all"
    report_self_term: str = "keep"

    def __post_init__(self) -> None:
        # Inert-axis canonicalization: with no report fold (p == 0) the scope selects
        # nothing, so force it to the single canonical sentinel "all". This makes the
        # native eq/hash the behavioral key AND makes .config reflect the canonical
        # scope, so no scoring map can key on a raw (behaviorally-inert) scope literal.
        if self.report_fold_p == 0.0 and self.report_fold_scope != "all":
            object.__setattr__(self, "report_fold_scope", "all")

    @property
    def config(self) -> RootCalcConfig:
        return RootCalcConfig(
            lcb_z=self.lcb_z,
            coverage_fold=self.coverage_fold,
            coverage_live_threshold=self.coverage_live_threshold,
            report_fold_p=self.report_fold_p,
            report_fold_scope=self.report_fold_scope,
            report_self_term=self.report_self_term,
        )

    @property
    def is_original(self) -> bool:
        """This cell behaviorally equals ORIGINAL_CELL (the pre-g-zc3p comparator)."""
        return self == ORIGINAL_CELL

    @property
    def is_reference(self) -> bool:
        """This cell behaviorally equals CURRENT_SM_V2_3_CELL (the delta reference)."""
        return self == CURRENT_SM_V2_3_CELL

    @property
    def label(self) -> str:
        return f"lcb_z={self.lcb_z:g},cov={self.coverage_fold}"


# The two pinned LITERAL anchors (fixed comparators every historical grid report and
# behavior-diff diagnostic is keyed off). ORIGINAL_CELL is the pre-g-zc3p original
# comparator (value unchanged from the former BASELINE_CELL). CURRENT_SM_V2_3_CELL is
# today's deployed sm-v2-3 model (== RootCalcConfig() today) — pinned by literal field
# values so it keeps meaning "the historical current model" after the phase-3 default
# flip (g-p4ih-model-flip). Because p == 0 the scope is canonicalized, giving the
# anti-drift property: each anchor compares equal to any other p=0 cell of the same
# (lcb_z, coverage_fold, threshold, self_term) regardless of the scope literal passed.
CURRENT_SM_V2_3_CELL = GridCell(
    lcb_z=1.0,
    coverage_fold="gate",
    coverage_live_threshold=1,
    report_fold_p=0.0,
    report_fold_scope="all",
    report_self_term="keep",
)
ORIGINAL_CELL = GridCell(
    lcb_z=0.0,
    coverage_fold="off",
    coverage_live_threshold=1,
    report_fold_p=0.0,
    report_fold_scope="all",
    report_self_term="keep",
)


def _cell_axes(cell: GridCell) -> dict[str, object]:
    """The SIX behavioral axes as JSON primitives (never a raw GridCell).

    The single module-level helper both the cohort grid report rows and the diagnostic
    CellRows use, so every serialized row identifies its cell by all six axes and no
    two swept cells collide. Reports are emitted via ``json.dumps(..., default=str)``,
    under which a raw GridCell would serialize as its dataclass repr STRING and hide
    the axes; ``_cell_axes`` makes the axes survive that (``default=str`` becomes only
    a safety net, never load-bearing).
    """
    return {
        "lcb_z": cell.lcb_z,
        "coverage_fold": cell.coverage_fold,
        "coverage_live_threshold": cell.coverage_live_threshold,
        "report_fold_p": cell.report_fold_p,
        "report_fold_scope": cell.report_fold_scope,
        "report_self_term": cell.report_self_term,
    }


def _cfg_fp(cell_or_config: "GridCell | RootCalcConfig") -> str:
    """Sole sanctioned fingerprint router for the calibration grid.

    ``root_calc_config_fingerprint`` rejects a raw GridCell with TypeError, so a
    grid cell can never be fingerprinted by accident. This router is the ONE place
    allowed to bridge the two: a GridCell is routed through its ``.config``, and a
    RootCalcConfig passes straight through. Every grid/cache fingerprint the
    calibration path computes must go through here rather than calling
    ``root_calc_config_fingerprint`` on a cell directly.
    """
    config = (
        cell_or_config.config
        if isinstance(cell_or_config, GridCell)
        else cell_or_config
    )
    return root_calc_config_fingerprint(config)


# Merged-role tuple canonicalization (DETERMINISTIC — pin the order, never
# tuple(set(...))). Once role moves OUTSIDE the cell, the merged tuple's element order
# becomes a serialization surface: a set-derived tuple would reorder run-to-run even
# though the underlying cells are byte-stable, a real flake against the report
# round-trip stability. Pin ONE canonical role rank (anchors, then arms, then B1, then
# demo — matching the anchor-first cells order) and sort + dedupe every merged tuple by
# it at the ONE point roles_by_cell is built, so every downstream read is already
# stable. An unknown label raises KeyError (fail-closed, like grade_rank).
ROLE_ORDER = ("original", "current", "arm1", "arm2", "b1", "demo")
_ROLE_RANK = {role: i for i, role in enumerate(ROLE_ORDER)}


def _canonical_roles(roles: Iterable[str]) -> tuple[str, ...]:
    """Sort roles by the pinned ROLE_ORDER and dedupe, so the same SET of roles ALWAYS
    serializes identically (("current", "arm2"), never ("arm2", "current"))."""
    return tuple(sorted(set(roles), key=lambda role: _ROLE_RANK[role]))


# The report-fold p-grid the arms are swept over (Option A's swept range, subset of
# (0, 1]). The single value the whole grid is parameterized by.
REPORT_FOLD_P_GRID = (0.25, 0.5, 0.75, 1.0)


def parse_report_fold_grid(raw: str | None) -> tuple[float, ...]:
    """Parse the ``--report-fold-grid`` argument (a pure ``str | None -> tuple``).

    The ``str | None`` type is load-bearing: ``None`` is the DISTINCT "argument
    omitted" representation (the CLI flag defaults to None) and is the ONLY path to the
    default REPORT_FOLD_P_GRID; a present-but-empty argument is never silently
    defaulted. RAISES ``ValueError`` on any present-but-invalid input — an explicit
    "" / whitespace-only / "," (a present-but-empty list), a non-numeric token, a
    NaN/±inf token, ``p <= 0`` (the fold-OFF identity, never a swept candidate), or
    ``p > 1`` (over-attenuation, outside Option A's range). The domain is 0 < p <= 1,
    STRICTER than RootCalcConfig (which accepts any finite non-negative p). Order-
    preserving dedupe (first-seen), returning a nonempty finite tuple — enumeration
    order is the tie-break substrate downstream. _parse_args converts the ValueError
    into ``parser.error`` (SystemExit(2)); this function itself never touches argparse.
    """
    if raw is None:
        return REPORT_FOLD_P_GRID
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ValueError(
            "--report-fold-grid may not be empty; expected 0 < p <= 1 values"
        )
    values: list[float] = []
    for token in tokens:
        try:
            p = float(token)
        except ValueError:
            raise ValueError(
                f"invalid report-fold p value {token!r}; domain 0 < p <= 1"
            ) from None
        if not math.isfinite(p):
            raise ValueError(
                f"report-fold p must be finite, got {token!r}; domain 0 < p <= 1"
            )
        if p <= 0.0 or p > 1.0:
            raise ValueError(f"report-fold p {p!r} out of domain 0 < p <= 1")
        values.append(p)
    return tuple(dict.fromkeys(values))


# ARM-1 — "fold instead of gate": drop the coverage gate, apply the report fold on BOTH
# sides. Its ungated pre_fold_quality is the leak channel the opponent-guard reads.
def arm1_cells(p_grid: tuple[float, ...] = REPORT_FOLD_P_GRID) -> tuple[GridCell, ...]:
    return tuple(
        GridCell(
            lcb_z=1.0,
            coverage_fold="off",
            coverage_live_threshold=1,
            report_fold_p=p,
            report_fold_scope="all",
            report_self_term="keep",
        )
        for p in p_grid
    )


# ARM-2 — "gate + user-side fold": keep the gate, apply the report fold to USER reports
# only. Built with p > 0 directly (NOT a p=0.0 base + dataclasses.replace): __post_init__
# canonicalizes scope to "all" whenever p == 0, so a p=0.0 ARM-2 base would SILENTLY
# LOSE its scope="user".
def arm2_cells(p_grid: tuple[float, ...] = REPORT_FOLD_P_GRID) -> tuple[GridCell, ...]:
    return tuple(
        GridCell(
            lcb_z=1.0,
            coverage_fold="gate",
            coverage_live_threshold=1,
            report_fold_p=p,
            report_fold_scope="user",
            report_self_term="keep",
        )
        for p in p_grid
    )


# B1 — "drop_user self-term": keep the gate, NO fold (fold+drop_user is disqualified),
# drop the user self-mastery term. A single literal — never swept over the p-grid.
B1_CELL = GridCell(
    lcb_z=1.0,
    coverage_fold="gate",
    coverage_live_threshold=1,
    report_fold_p=0.0,
    report_fold_scope="all",
    report_self_term="drop_user",
)

# DEMO — gate + uniform (all-scope) report fold at maximal p=1.0: the opponent-side
# double-count DEMONSTRATION. Diagnostics-only, NEVER a candidate, NEVER cohort-scored,
# never swept (a qualitative demonstration). NEVER enters build_arm_grid().cells /
# required_cells; reaches the STANDALONE diagnostics only via --include-demo-diagnostics.
DEMO_GATE_UNIFORM_FOLD_CELL = GridCell(
    lcb_z=1.0,
    coverage_fold="gate",
    coverage_live_threshold=1,
    report_fold_p=1.0,
    report_fold_scope="all",
    report_self_term="keep",
)
DEMO_CELLS = (DEMO_GATE_UNIFORM_FOLD_CELL,)


@dataclass(frozen=True)
class ArmGrid:
    """The settled grid return: an anchor-first deduped cell tuple + roles OUTSIDE the
    cell (a merged, _canonical_roles-sorted role-label tuple per deduped cell)."""

    cells: tuple[GridCell, ...]  # anchor-first, deduped — the required grid
    roles_by_cell: dict[GridCell, tuple[str, ...]]  # merged role labels per cell


def build_arm_grid(p_grid: tuple[float, ...] = REPORT_FOLD_P_GRID) -> ArmGrid:
    """Build the fixed anchor-first grid, parameterized ONLY by the report-fold p-grid.

    Emits exactly ``{ORIGINAL_CELL, CURRENT_SM_V2_3_CELL, ARM-1×p_grid, ARM-2×p_grid,
    B1}``, deduped by native identity (inert-axis canonicalization collapses any
    accidental p=0 / scope overlap onto an anchor), ordered anchors-first. role is
    carried OUTSIDE the cell in ``roles_by_cell``: when one deduped cell earns more than
    one role (an anchor value that also lands in an arm's p-sweep — only reachable via a
    DIRECT builder call with p=0; the CLI parser rejects 0), its label tuple MERGES both
    via ``_canonical_roles``, and the cell is still scored ONCE. NEVER emits a demo cell.
    """
    role_specs: list[tuple[GridCell, str]] = [
        (ORIGINAL_CELL, "original"),
        (CURRENT_SM_V2_3_CELL, "current"),
        *[(cell, "arm1") for cell in arm1_cells(p_grid)],
        *[(cell, "arm2") for cell in arm2_cells(p_grid)],
        (B1_CELL, "b1"),
    ]
    cells: list[GridCell] = []
    roles_accum: dict[GridCell, list[str]] = {}
    for cell, role in role_specs:
        if cell not in roles_accum:
            roles_accum[cell] = []
            cells.append(cell)
        roles_accum[cell].append(role)
    roles_by_cell = {
        cell: _canonical_roles(roles) for cell, roles in roles_accum.items()
    }
    return ArmGrid(cells=tuple(cells), roles_by_cell=roles_by_cell)


def score_pair_grid(
    db,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    roots: OpeningRoots,
    cells: tuple[GridCell, ...],
) -> dict[GridCell, PairScore]:
    """Build a pair's overlay ONCE (the ~2.6s cost), then score it per grid cell.

    Naively gridding would rebuild ``overlay_evidence`` O(pairs x cells) times; here
    the (DB-read) overlay is built once and only the cheap in-memory
    ``score_overlay`` runs per cell. ``cells`` is ``ArmGrid.cells`` — a bare cell tuple,
    never the ArmGrid wrapper (cohort scoring never reads roles).
    """
    overlay = overlay_evidence(db, user_id, player_color, graph)
    return {
        cell: score_overlay(user_id, player_color, graph, overlay, roots, cell.config)
        for cell in cells
    }


def pair_key_deltas(
    cell_score: PairScore, reference_score: PairScore
) -> list[dict[str, object]]:
    """Per-opening-key delta records vs the current-model reference, for keys in both.

    Each record keeps the opening key, its current-model and cell scores, and the
    delta, so the report can emit true per-pair per-opening-key deltas (not a summary).
    """
    reference = reference_score.named_score_map
    return [
        {
            "opening_key": key,
            "current_score": reference[key],
            "cell": value,
            "delta": value - reference[key],
        }
        for key, value in cell_score.named_score_map.items()
        if key in reference
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
# Grid report: per-cell distributions + per-pair per-key deltas vs current model
# ---------------------------------------------------------------------------


def build_cell_report(
    cell: GridCell,
    pair_scores: list[PairScore],
    reference_scores: list[PairScore],
    *,
    min_observations: int,
) -> dict[str, object]:
    """One grid cell's distribution + (for non-reference cells) deltas vs current.

    ``pair_scores`` and ``reference_scores`` are aligned per pair (same order); a
    pair is included when its observation total clears ``min_observations`` (the total
    is config-independent, so any cell's row decides membership identically). The
    REFERENCE cell (CURRENT_SM_V2_3_CELL) omits deltas — deltas against itself are all
    zero and carry no signal; ORIGINAL_CELL now EMITS deltas like any non-reference
    cell, so its continuity column carries a real delta-vs-current. Each row's cell
    identity is the full SIX-axis ``_cell_axes(cell)`` dict, not a flat two-field pair
    (which would collide across the arm p-cells and B1), plus explicit ``is_original``
    / ``is_reference`` booleans so a consumer can identify each anchor unambiguously.
    """
    paired = [
        (cell_p, ref_p)
        for cell_p, ref_p in zip(pair_scores, reference_scores)
        if ref_p.observation_total >= min_observations
    ]
    pooled = [score for cell_p, _ in paired for score in cell_p.named_scores]
    synthetic = [
        cell_p.synthetic_score for cell_p, _ in paired if cell_p.synthetic_score is not None
    ]
    report: dict[str, object] = {
        "cell": _cell_axes(cell),
        "is_original": cell.is_original,
        "is_reference": cell.is_reference,
        "named_score_distribution": summarize(pooled),
        "synthetic_hero_distribution": summarize(synthetic),
    }
    if not cell.is_reference:
        per_pair: list[dict[str, object]] = []
        pooled_deltas: list[float] = []
        for cell_p, ref_p in paired:
            records = pair_key_deltas(cell_p, ref_p)
            values = [record["delta"] for record in records]
            pooled_deltas.extend(values)
            per_pair.append(
                {
                    "user_id": cell_p.user_id,
                    "player_color": cell_p.player_color,
                    # Full per-opening-key deltas (opening_key/current_score/cell/delta).
                    "keys": records,
                    "summary": summarize_deltas(values),
                }
            )
        report["deltas_vs_current"] = {
            "pooled": summarize_deltas(pooled_deltas),
            "per_pair": per_pair,
        }
    return report


def build_grid_report(
    cells: tuple[GridCell, ...],
    pair_grids: list[dict[GridCell, PairScore]],
    *,
    min_observations: int,
) -> dict[str, object]:
    """Per-cell distributions + deltas across the whole grid (delta ref = current)."""
    reference_scores = [pg[CURRENT_SM_V2_3_CELL] for pg in pair_grids]
    cell_reports = [
        build_cell_report(
            cell,
            [pg[cell] for pg in pair_grids],
            reference_scores,
            min_observations=min_observations,
        )
        for cell in cells
    ]
    return {"cells": cell_reports}


# ---------------------------------------------------------------------------
# PASS/FAIL calibration diagnostics (synthetic overlays scored across the grid)
# ---------------------------------------------------------------------------

# Deterministic clock for the synthetic scenarios (g-p4ih.1.2 "Deterministic clock").
# compute_root_score defaults to datetime.now(timezone.utc), so every diagnostic /
# builder this bead owns threads a keyword-only as_of (defaulting to this) into every
# compute_root_score(..., now=as_of) so no scorer it calls reaches the wall clock. The
# STANDALONE path uses this default; g-p4ih-replay-bind threads the artifact-header
# as_of on the release path. The User-14 scenario carries no last-touch timestamps, so
# its score/coverage are in fact clock-INVARIANT — but the threading is the
# architectural guarantee that closes the datetime.now path for EVERY diagnostic
# (including the broad-guard/specialist producers, whose confidence channel DOES read
# the clock) and keeps the release run reproducible.
SYNTHETIC_AS_OF = datetime(2025, 1, 1, tzinfo=timezone.utc)


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


def _user14_scenario() -> tuple[
    OpeningGraph, EvidenceOverlay, EvidenceOverlay, OpeningRoots, str, str
]:
    """The frozen synthetic User-14 scenario (g-p4ih.1.2 "Synthetic User-14 scenario").

    A TWO-LEVEL opponent fan (Caro breadth 2 x deep breadth 6) with PINNED node + edge
    evidence, exposing the SAME FEN as a user-turn node under black and an opponent-turn
    node under white on IDENTICAL evidence. Returns (graph, black_overlay, white_overlay,
    roots, root_fen, child_fen). The pinned values reproduce (scored under
    CURRENT_SM_V2_3_CELL at SYNTHETIC_AS_OF) root pre-fold quality 54.32, Caro child
    34.38, coverage fraction 0.0833 (each inside its self-check tolerance).
    """
    root_fen = _diag_positions(["e2e4"])[1]  # 1.e4, black to move (USER / OPP mirror)
    child_fen = _diag_positions(["e2e4", "c7c6"])[2]  # Caro-Kann, white to move (OPP)
    mid_fen = _diag_positions(["e2e4", "c7c6", "d2d4"])[3]  # 2.d4, black to move (USER)
    deep_fen = _diag_positions(["e2e4", "c7c6", "d2d4", "d7d5"])[4]  # 3.*, white to move
    leaf_fen = _diag_positions(["e2e4", "c7c6", "d2d4", "d7d5", "e4d5"])[5]  # after exd5

    # Exactly SEVEN paths: the prepared mainline plus the unprepared siblings at each of
    # the two opponent levels. The Caro node gets 2 White replies (d4 + Nc3); the deep
    # node gets 6 (exd5 + 5 siblings). Their PRODUCT is the coverage denominator
    # (1 / (2*6) = 0.0833).
    paths = [
        ["e2e4", "c7c6", "d2d4", "d7d5", "e4d5"],  # prepared mainline: Caro Exchange
        ["e2e4", "c7c6", "b1c3"],                    # 1 unprepared Caro reply (2.Nc3)
        ["e2e4", "c7c6", "d2d4", "d7d5", "b1c3"],    # 5 unprepared deep replies
        ["e2e4", "c7c6", "d2d4", "d7d5", "g1f3"],
        ["e2e4", "c7c6", "d2d4", "d7d5", "f1d3"],
        ["e2e4", "c7c6", "d2d4", "d7d5", "b1d2"],
        ["e2e4", "c7c6", "d2d4", "d7d5", "g1e2"],
    ]
    graph = _diag_graph(paths)

    def _overlay(color: str) -> EvidenceOverlay:
        overlay = EvidenceOverlay(14, color)
        # Node quality (mastery) sits on the USER-turn nodes ONLY — root, mid, leaf —
        # because _mastery is defined for user-turn nodes; quality on the OPPONENT Caro
        # node would be INERT. Exactly THREE non-zero quality_sum nodes.
        overlay.nodes[root_fen] = NodeEvidence(
            fen=root_fen, quality_sum=54.0, quality_count=60, live_attempts=60
        )
        overlay.nodes[mid_fen] = NodeEvidence(
            fen=mid_fen, quality_sum=38.8, quality_count=40, live_attempts=40
        )
        overlay.nodes[leaf_fen] = NodeEvidence(
            fen=leaf_fen, quality_sum=16.0, quality_count=20, live_attempts=20
        )
        # Prepared-child status is set by EDGE evidence (_prepared_children reads edges,
        # not node evidence): the two USER moves c7c6 (root->caro) and d7d5 (mid->deep)
        # carry edges with live_passes >= 1. NEVER add an edge for a non-move pair — an
        # overlay edge also registers as an OBSERVED structural child, injecting a
        # phantom child that would corrupt the topology.
        overlay.edges[(root_fen, child_fen)] = EdgeEvidence(
            root_fen, child_fen, "c7c6", live_attempts=60, live_passes=60
        )
        overlay.edges[(mid_fen, deep_fen)] = EdgeEvidence(
            mid_fen, deep_fen, "d7d5", live_attempts=40, live_passes=40
        )
        return overlay

    # Both roots are named so compute_root_score can score EACH as its own root (root_fen
    # for the black/white root operands, child_fen for the "strongest child" operand).
    return (
        graph,
        _overlay("black"),
        _overlay("white"),
        _diag_roots(root_fen, child_fen),
        root_fen,
        child_fen,
    )


def _score_target(
    target: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig,
    *,
    now: datetime = SYNTHETIC_AS_OF,
    debug: bool = False,
) -> float:
    return compute_root_score(
        target, "white", graph, overlay, roots, config, now=now, debug=debug
    ).opening_score


# --- report-stage FEN lookup (leak gate reads pre_fold_quality, not opening_score) ---


def _report_node_for(
    root_score: RootScore,
    fen: str,
    *,
    require: tuple[str, ...] = (
        "pre_fold_quality",
        "reported_score",
        "report_fold_multiplier",
    ),
) -> NodeDebug:
    """The NodeDebug for a REPORTED FEN, scanning debug_nodes (a list, not a FEN map).

    Fails closed (RAISES ValueError) in TWO distinct cases, so a never-reported node can
    never masquerade as its own reported row: (1) NO node matches the FEN (debug_nodes
    never visited it); and (2) a node matches but ANY report-stage operand named in
    ``require`` is None — the FEN was visited only as a DESCENDANT during _calc, never
    reported as its own row (the report-stage fields stay None until a FEN is reported in
    _direct_metrics). The None check is the necessary guard the FEN scan alone cannot
    provide.
    """
    target = normalize_fen(fen)
    for node in root_score.debug_nodes:
        if normalize_fen(node.fen) == target:
            missing = [name for name in require if getattr(node, name) is None]
            if missing:
                raise ValueError(
                    f"fen present but not reported: report-stage operand(s) "
                    f"{missing} are null for {fen!r}"
                )
            return node
    raise ValueError(f"fen not in debug_nodes: {fen!r}")


def pre_fold_quality_for(root_score: RootScore, fen: str) -> float:
    """Ungated pre-fold quality of a reported FEN (fails closed on an unreported FEN)."""
    return _report_node_for(
        root_score, fen, require=("pre_fold_quality",)
    ).pre_fold_quality


# --- behavior keys + cell-role taxonomy (grading routed by NATIVE identity, not label) ---


def _opp_behavior_key(cell: GridCell) -> tuple[object, ...]:
    # Axes that determine an OPPONENT node's reported score: LCB + gate (+ threshold)
    # + the report fold ONLY when it reaches opponent reports (scope="all"). A
    # scope="user" fold and drop_user never touch opponent reports -> excluded here.
    opp_fold_p = cell.report_fold_p if cell.report_fold_scope == "all" else 0.0
    return (cell.lcb_z, cell.coverage_fold, cell.coverage_live_threshold, opp_fold_p)


def _user_behavior_key(cell: GridCell) -> tuple[object, ...]:
    # Axes that determine a USER node's reported score: LCB + gate (+ threshold)
    # + the report fold (BOTH scopes fold user reports -> scope omitted) + self-term.
    return (
        cell.lcb_z,
        cell.coverage_fold,
        cell.coverage_live_threshold,
        cell.report_fold_p,
        cell.report_self_term,
    )


_ANCHOR_CELLS = (ORIGINAL_CELL, CURRENT_SM_V2_3_CELL)


def _is_eligible(cell: GridCell, demo_cells: tuple[GridCell, ...]) -> bool:
    """Filter 1: NOT behaviorally an anchor and NOT a demo (a native-identity test on
    the pinned constants, never a mutable role label)."""
    return cell not in _ANCHOR_CELLS and cell not in demo_cells


def _graded_for(cell: GridCell, demo_cells: tuple[GridCell, ...]) -> str:
    """The cell's grading role: GRADED-FOR-SELECTION (arms — outcome flips the aggregate
    and gates release), GRADED-FOR-REFERENCE (B1 — scored + reported, never flips the
    aggregate), or "none" (continuity anchors, demos)."""
    if not _is_eligible(cell, demo_cells):
        return "none"
    return "reference" if cell == B1_CELL else "selection"


def _iter_grid_and_demos(
    grid: ArmGrid, demo_cells: tuple[GridCell, ...]
):
    """Yield (cell, roles) for every grid cell (roles from roles_by_cell, already
    _canonical_roles-sorted) then each demo cell with a synthesized ("demo",)."""
    for cell in grid.cells:
        yield cell, grid.roles_by_cell[cell]
    for cell in demo_cells:
        yield cell, ("demo",)


def _diagnostic_rows(
    grid: ArmGrid,
    demo_cells: tuple[GridCell, ...],
    operands,
    applicable,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Build one CellRow per grid cell (then demos) plus the CURRENT reference row.

    ``operands(cell)`` returns the per-cell operand fields (mapped 1:1 into
    DiagnosticCellResult downstream); ``applicable(cell)`` is Filter 2 on the
    diagnostic's RELEVANT turn. CellRow["cell"] is the six-axis primitive dict (never a
    raw GridCell), so every operand survives ``json.dumps`` WITHOUT relying on
    ``default=str``.
    """

    def _row(cell: GridCell, roles: tuple[str, ...]) -> dict[str, object]:
        row: dict[str, object] = {
            "cell": _cell_axes(cell),
            "cell_label": cell.label,
            "roles": roles,
            "eligible": _is_eligible(cell, demo_cells),
            "graded_for": _graded_for(cell, demo_cells),
            "applicable": applicable(cell),
        }
        row.update(operands(cell))
        return row

    rows = [_row(cell, roles) for cell, roles in _iter_grid_and_demos(grid, demo_cells)]
    reference = _row(
        CURRENT_SM_V2_3_CELL,
        grid.roles_by_cell.get(CURRENT_SM_V2_3_CELL, ("current",)),
    )
    return rows, reference


def _selection_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The GRADED-FOR-SELECTION arm rows applicable on the diagnostic's relevant turn —
    the ONLY rows that contribute to a diagnostic's AGGREGATE passed."""
    return [r for r in rows if r["graded_for"] == "selection" and r["applicable"]]


def run_user14_diagnostic(
    grid: ArmGrid, *, demo_cells: tuple[GridCell, ...] = (), as_of: datetime = SYNTHETIC_AS_OF
) -> dict[str, object]:
    """User-turn true-positive + fold operands (the ~54 A -> <= C drop under the fold).

    Scores the frozen _user14_scenario under each grid cell on BOTH colors from the
    identical synthetic evidence, at now=as_of, debug=True. AGGREGATE passed: True iff
    for EVERY graded-for-selection arm row with user_moves_vs_current the user_tp_score
    grades <= C, while the reference (CURRENT_SM_V2_3_CELL) row is A. B1 is GRADED-FOR-
    REFERENCE (its ~34 user-turn drop is reported but NEVER enters passed).
    """
    graph, black_overlay, white_overlay, roots, root_fen, child_fen = _user14_scenario()

    def operands(cell: GridCell) -> dict[str, object]:
        config = cell.config
        black_root = compute_root_score(
            root_fen, "black", graph, black_overlay, roots, config, now=as_of, debug=True
        )
        caro_child = compute_root_score(
            child_fen, "black", graph, black_overlay, roots, config, now=as_of, debug=True
        )
        white_root = compute_root_score(
            root_fen, "white", graph, white_overlay, roots, config, now=as_of, debug=True
        )
        user_node = _report_node_for(black_root, root_fen)
        opp_node = _report_node_for(white_root, root_fen)
        return {
            "synth_black_root_score": black_root.opening_score,
            "synth_caro_child_score": caro_child.opening_score,
            "synth_root_coverage_fraction": black_root.coverage / 100.0,
            "synth_user_turn_pre_fold_quality": user_node.pre_fold_quality,
            "synth_user_turn_multiplier": user_node.report_fold_multiplier,
            "synth_opp_turn_score": white_root.opening_score,
            "synth_opp_turn_pre_fold_quality": opp_node.pre_fold_quality,
            "synth_opp_turn_multiplier": opp_node.report_fold_multiplier,
            "user_tp_score": black_root.opening_score,
        }

    def applicable(cell: GridCell) -> bool:
        return _user_behavior_key(cell) != _user_behavior_key(CURRENT_SM_V2_3_CELL)

    rows, reference = _diagnostic_rows(grid, demo_cells, operands, applicable)
    selection = _selection_rows(rows)
    passed: bool | None
    if not selection:
        passed = None
    else:
        passed = fixed_band(reference["user_tp_score"]) == "A" and all(
            grade_rank(fixed_band(r["user_tp_score"])) >= grade_rank("C")
            for r in selection
        )
    return {
        "name": "User-14 user-turn true-positive (A -> <= C fold drop)",
        "reference": reference,
        "rows": rows,
        "passed": passed,
    }


def run_broad_guard_diagnostic(
    cell: GridCell, *, as_of: datetime = SYNTHETIC_AS_OF
) -> float:
    """Broad-guard OPERAND producer (opponent-turn score of a broadly-prepared player).

    Demoted from a top-level diagnostic to the opponent-guard's NOT-crater operand
    producer — the opponent-turn opening_score of a player with real covered prep across
    ALL opponent replies (must stay ~A+ under the fold)."""
    graph, overlay, roots, target = _broad_guard_scenario()
    return _score_target(target, graph, overlay, roots, cell.config, now=as_of)


def run_specialist_diagnostic(
    cell: GridCell, *, as_of: datetime = SYNTHETIC_AS_OF
) -> float:
    """Specialist OPERAND producer (opponent-turn UNGATED pre_fold_quality — the leak).

    Demoted from a top-level diagnostic to the opponent-guard's leak operand producer.
    Reads the UNGATED quality CHANNEL (pre_fold_quality via debug=True), NOT the reported
    opening_score: the coverage**p multiplier would MASK a leaked quality channel."""
    graph, overlay, roots, target = _specialist_scenario()
    root_score = compute_root_score(
        target, "white", graph, overlay, roots, cell.config, now=as_of, debug=True
    )
    return pre_fold_quality_for(root_score, target)


def run_opponent_guard_diagnostic(
    grid: ArmGrid, *, demo_cells: tuple[GridCell, ...] = (), as_of: datetime = SYNTHETIC_AS_OF
) -> dict[str, object]:
    """Opponent regression guard + unprepared-branch leak.

    Calls the re-wired broad-guard (NOT-crater operand) and specialist (leak operand)
    producers under debug=True. AGGREGATE passed: True iff for every graded-for-selection
    arm row with opponent_moves_vs_current BOTH the opponent-drop guard and the leak guard
    hold. ARM-2 and B1 share CURRENT's _opp_behavior_key, so only the ARM-1 p-cells are
    applicable here; without this pair the grid could select a p that fixes Black roots
    while silently making White/opponent cards unusably low.
    """

    def operands(cell: GridCell) -> dict[str, object]:
        return {
            "broad_guard_opp_score": run_broad_guard_diagnostic(cell, as_of=as_of),
            "specialist_pre_fold_quality": run_specialist_diagnostic(cell, as_of=as_of),
        }

    def applicable(cell: GridCell) -> bool:
        return _opp_behavior_key(cell) != _opp_behavior_key(CURRENT_SM_V2_3_CELL)

    rows, reference = _diagnostic_rows(grid, demo_cells, operands, applicable)
    reference_opp_score = reference["broad_guard_opp_score"]
    reference_gated_quality = reference["specialist_pre_fold_quality"]
    selection = _selection_rows(rows)
    passed: bool | None
    if not selection:
        passed = None
    else:
        passed = all(
            not _opp_guard_fires(r["broad_guard_opp_score"], reference_opp_score)
            and not _leak_fires(
                r["specialist_pre_fold_quality"], reference_gated_quality
            )
            for r in selection
        )
    return {
        "name": "opponent regression guard + unprepared-branch leak",
        "reference": reference,
        "rows": rows,
        "passed": passed,
    }


def run_cliff_diagnostic(
    grid: ArmGrid, thresholds: tuple[int, ...] = (1, 2), *, as_of: datetime = SYNTHETIC_AS_OF
) -> dict[str, object]:
    """STRUCTURAL thin-but-earned cliff self-check (EXEMPT from the arm-only grading).

    Verifies the SCORER's coverage-gate + threshold MECHANISM: score the same branch at
    live=1/review=0 vs live=1/review=1, sweeping coverage_live_threshold. Not a candidate
    grade — it neither runs Filter 1 / Filter 2 nor aggregates over arm rows, and its
    passed reads the CURRENT_SM_V2_3_CELL (is_reference) probe row (the deployed gate
    config), NOT the degenerate "lowest-lcb_z gate row" (every gate cell now shares
    lcb_z=1.0). Uses dataclasses.replace(cell.config, ...) so the new fold/self-term axes
    are carried into every row, never dropped by a hand-built RootCalcConfig.
    """
    rows: list[dict[str, object]] = []
    for cell in grid.cells:
        for threshold in thresholds:
            # Serialize the PROBE cell (the base cell at the swept threshold) so the
            # six-axis identity matches the config actually scored — otherwise a
            # threshold-2 row would carry cell.coverage_live_threshold == 1, an identity
            # that contradicts its own coverage_live_threshold field. is_reference stays
            # keyed off the BASE cell (a threshold-independent probe tag): the probe is
            # "CURRENT swept to threshold T", and CURRENT itself is pinned at threshold 1.
            probe = replace(cell, coverage_live_threshold=threshold)
            g0, o0, r0, t0 = _cliff_scenario(reviewed=False)
            g1, o1, r1, t1 = _cliff_scenario(reviewed=True)
            thin = _score_target(t0, g0, o0, r0, probe.config, now=as_of)
            after_review = _score_target(t1, g1, o1, r1, probe.config, now=as_of)
            rows.append(
                {
                    "cell": _cell_axes(probe),
                    "cell_label": probe.label,
                    "is_reference": cell.is_reference,
                    "coverage_live_threshold": threshold,
                    "thin_score": thin,
                    "reviewed_score": after_review,
                    "jump": after_review - thin,
                }
            )

    # Deterministic structural probe: the CURRENT model's gate cell at each threshold.
    def _probe_row(threshold: int) -> dict[str, object] | None:
        for row in rows:
            if row["is_reference"] and row["coverage_live_threshold"] == threshold:
                return row
        return None

    gate2 = _probe_row(2)
    gate1 = _probe_row(1)
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


def run_diagnostics(
    grid: ArmGrid, *, demo_cells: tuple[GridCell, ...] = (), as_of: datetime = SYNTHETIC_AS_OF
) -> dict[str, object]:
    """Exactly three keys: {"user14", "opponent_guard", "cliff"}. The old specialist /
    broad_guard top-level keys are GONE — they survive only as operand producers."""
    return {
        "user14": run_user14_diagnostic(grid, demo_cells=demo_cells, as_of=as_of),
        "opponent_guard": run_opponent_guard_diagnostic(
            grid, demo_cells=demo_cells, as_of=as_of
        ),
        "cliff": run_cliff_diagnostic(grid, as_of=as_of),
    }


# ---------------------------------------------------------------------------
# User-14 fixture builder + writer (settled path consumed by g-p4ih-cutoff-fixture)
# ---------------------------------------------------------------------------

# The settled repo-relative fixture path, computed from __file__ (NOT the process CWD,
# so it is stable regardless of where the CLI is invoked). g-p4ih-cutoff-fixture adds
# the --emit-user14-fixture CLI mode that binds the builder+writer to SM_V2_4_DEFAULT_CELL
# and performs the one real emission here; this bead never touches the checked-in file.
DEFAULT_USER14_FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "openings"
    / "__fixtures__"
    / "user14_synthetic.json"
)


def build_user14_fixture(
    cell: GridCell, model_version: str, *, as_of: datetime = SYNTHETIC_AS_OF
) -> dict[str, object]:
    """Compute the User-14 fixture dict from the synthetic scenario under ``cell``.

    Every field is scored from _user14_scenario under the GIVEN cell at now=as_of.
    ``coverage_implied_score`` is DERIVED from root_coverage_fraction and report_fold_p
    (never an independently-typed number). ``config_fingerprint`` routes through the
    cell's .config via _cfg_fp (g-report-cfg-fp owns the router; this only CALLS it).
    """
    graph, black_overlay, white_overlay, roots, root_fen, child_fen = _user14_scenario()
    config = cell.config
    black_root = compute_root_score(
        root_fen, "black", graph, black_overlay, roots, config, now=as_of, debug=True
    )
    caro_child = compute_root_score(
        child_fen, "black", graph, black_overlay, roots, config, now=as_of, debug=True
    )
    white_root = compute_root_score(
        root_fen, "white", graph, white_overlay, roots, config, now=as_of, debug=True
    )
    root_coverage_fraction = black_root.coverage / 100.0  # coverage is 0-100 PERCENT
    coverage_implied_score = 100.0 * root_coverage_fraction ** cell.report_fold_p
    return {
        "schema_version": 1,
        "model_version": model_version,
        "config_fingerprint": _cfg_fp(cell),
        "report_fold_p": cell.report_fold_p,
        "black_root_score": black_root.opening_score,
        "caro_child_score": caro_child.opening_score,
        "white_root_score": white_root.opening_score,
        "root_coverage_fraction": root_coverage_fraction,
        "coverage_implied_score": coverage_implied_score,
    }


def write_user14_fixture(
    payload: dict[str, object], path: Path = DEFAULT_USER14_FIXTURE_PATH
) -> Path:
    """Write ``payload`` deterministically (sorted keys, 2-space indent, trailing \\n).

    A PURE writer: takes a payload + path, does no scoring, and never hard-codes the
    default in the body (so a test can redirect it). Creates parent directories; returns
    the Path actually written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


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
    """Compact six-axis label from a report/diagnostic row's nested ``cell`` dict.

    Reads ``entry["cell"]`` (the ``_cell_axes`` primitive dict) so the four arm p-cells
    that share lcb_z/coverage_fold are distinguishable in text, not only in --json.
    """
    cell = entry["cell"]
    label = f"lcb_z={cell['lcb_z']:g},cov={cell['coverage_fold']}"
    if cell["coverage_live_threshold"] != 1:
        label += f",thr={cell['coverage_live_threshold']}"
    if cell["report_fold_p"]:
        label += f",p={cell['report_fold_p']:g}/{cell['report_fold_scope']}"
    if cell["report_self_term"] != "keep":
        label += f",self={cell['report_self_term']}"
    return label


def _cell_tag(entry: dict[str, object]) -> str:
    if entry.get("is_reference"):
        return " [reference]"
    if entry.get("is_original"):
        return " [original]"
    return ""


# Text output shows only the N largest-magnitude per-key movers per cell; the
# FULL per-pair per-key deltas are always in the --json report.
DELTA_TEXT_LIMIT = 8


def _render_grid(grid: dict[str, object], lines: list[str]) -> None:
    lines.append("=== Calibration grid (per cell vs current model) ===")
    for cell in grid["cells"]:
        lines.append(f"-- {_cell_label(cell)}{_cell_tag(cell)} --")
        lines.append(
            "  named:" + _fmt_summary(cell["named_score_distribution"]).rstrip()
        )
        lines.append(
            "  synthetic:" + _fmt_summary(cell["synthetic_hero_distribution"]).rstrip()
        )
        deltas = cell.get("deltas_vs_current")
        if deltas is not None:
            pooled = deltas["pooled"]
            pcts = pooled["percentiles"]
            lines.append(
                f"  Δ vs current (per key): n={pooled['count']} "
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
            f"{_fmt_opt(record['current_score'])}→{_fmt_opt(record['cell'])} "
            f"(Δ{record['delta']:+.1f})"
        )
    remaining = len(movers) - DELTA_TEXT_LIMIT
    if remaining > 0:
        lines.append(
            f"    … {remaining} more per-key deltas (full set in --json)"
        )


def _fmt_operands(row: dict[str, object], operand_keys: tuple[str, ...]) -> str:
    return " ".join(f"{key}={_fmt_opt(row.get(key))}" for key in operand_keys)


def _render_paired_diagnostic(
    diag: dict[str, object], lines: list[str], operand_keys: tuple[str, ...]
) -> None:
    """Render a paired diagnostic: name + PASS/FAIL, the reference row, then one row per
    cell reading the operand keys that EXIST on the row it renders (never a generic
    pre_fold_quality — the NAMED per-turn operands are surfaced), so the leak channel is
    visible in text. A graded_for="none" anchor/continuity/demo row is a plain column."""
    lines.append(f"[{_fmt_gate(diag['passed'])}] {diag['name']}")
    reference = diag["reference"]
    lines.append(
        f"    reference {_cell_label(reference)}: "
        + _fmt_operands(reference, operand_keys)
    )
    for row in diag["rows"]:
        roles = ",".join(row["roles"])
        na = "" if row["applicable"] else " (no effect)"
        lines.append(
            f"    {_cell_label(row)} [{row['graded_for']}|{roles}]{na}: "
            + _fmt_operands(row, operand_keys)
        )


def _render_diagnostics(diagnostics: dict[str, object], lines: list[str]) -> None:
    lines.append("=== Calibration diagnostics (PASS/FAIL) ===")

    _render_paired_diagnostic(
        diagnostics["user14"],
        lines,
        (
            "user_tp_score",
            "synth_user_turn_pre_fold_quality",
            "synth_opp_turn_pre_fold_quality",
        ),
    )
    _render_paired_diagnostic(
        diagnostics["opponent_guard"],
        lines,
        ("broad_guard_opp_score", "specialist_pre_fold_quality"),
    )

    cliff = diagnostics["cliff"]
    lines.append(f"[{_fmt_gate(cliff['passed'])}] {cliff['name']}")
    for row in cliff["rows"]:
        lines.append(
            f"    {_cell_label(row)},live_thr={row['coverage_live_threshold']}"
            f"{_cell_tag(row)}: "
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
        "--report-fold-grid",
        default=None,
        help='Comma-separated report-fold p values to sweep the arms over '
        '(default "0.25,0.5,0.75,1.0"; domain 0 < p <= 1).',
    )
    parser.add_argument(
        "--include-demo-diagnostics",
        action="store_true",
        help="Add the diagnostics-only demo rows (gate + uniform fold) to a STANDALONE "
        "run's diagnostics (default off; never enters cohort scoring).",
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
    args = parser.parse_args(argv)
    # parse_report_fold_grid is a pure str|None -> tuple that RAISES ValueError on any
    # invalid input; convert that into a clean fail-fast exit here (the ONLY place the
    # parser object is in scope). The --report-fold-grid flag keeps default=None (a
    # str-or-None, NOT a pre-parsed tuple), so argparse never applies a type callable to
    # a non-string default and the "raw is None -> default" branch stays the sole
    # default path.
    try:
        args.report_fold_p_grid = parse_report_fold_grid(args.report_fold_grid)
    except ValueError as exc:
        parser.error(str(exc))  # prints usage + message, raises SystemExit(2)
    return args


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

    arm_grid = build_arm_grid(args.report_fold_p_grid)
    required_cells = arm_grid.cells  # every ScoredPair.grid / cohort row's cells; NO demos

    users = _parse_user_filter(args.users)
    pairs = _parse_pair_filter(args.pairs)

    with session_factory() as db:
        candidate_pairs = list_opening_score_candidate_pairs(db, limit=args.limit)
        selected = select_pairs(candidate_pairs, users=users, pairs=pairs)

        # Build each pair's overlay ONCE and score it for every required cell.
        pair_grids = [
            score_pair_grid(db, user_id, player_color, graph, roots, required_cells)
            for user_id, player_color in selected
        ]

        write_bench = None
        if args.write_bench and selected:
            bench_user, bench_color = selected[0]
            write_bench = run_write_bench(db, bench_user, bench_color, args.database_url)

    # The CURRENT model cell (today's deployed model) drives the top-level
    # distribution/telemetry; the grid section adds every other cell plus per-key deltas
    # vs that current-model reference.
    reference_scores = [pg[CURRENT_SM_V2_3_CELL] for pg in pair_grids]
    report = build_report(
        reference_scores,
        min_observations=args.min_observations,
        named_root_count=named_root_count,
        write_bench=write_bench,
    )
    report["grid"] = build_grid_report(
        required_cells, pair_grids, min_observations=args.min_observations
    )
    report["diagnostics"] = run_diagnostics(
        arm_grid,
        demo_cells=DEMO_CELLS if args.include_demo_diagnostics else (),
        as_of=SYNTHETIC_AS_OF,
    )

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
