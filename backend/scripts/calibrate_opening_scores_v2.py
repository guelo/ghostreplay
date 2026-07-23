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
import ast
import contextlib
import errno
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Protocol, Sequence

try:  # POSIX-only; capture's mutual-exclusion locks require it (governance-load-bearing).
    import fcntl
except ImportError:  # pragma: no cover - capture refuses on non-POSIX before using it
    fcntl = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Source binding — ALL OF IT CAPTURED HERE, ABOVE EVERY SCORER IMPORT.
# ---------------------------------------------------------------------------
# Each value below is a statement about code this interpreter has NOT COMPILED YET. Read
# any of them after the `from app...` imports and they describe a tree that may already
# have diverged from what is running.

# Repo root, from __file__ (backend/scripts/<this>.py -> parents[2]), so it is stable
# regardless of the process CWD.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The COMPLETE, sorted manifest of repo-relative POSIX source paths whose EXACT on-disk
# bytes the scores are bound to. This is the CODE surface the content fingerprints
# (graph/roots/evidence-derivation) cannot see: a graph-walk or normalization change moves
# scores under an unchanged content fingerprint. requirements.txt is the DECLARED
# dependency set (the chess pin lives inside the digest through it).
#
# CLOSURE INVARIANT (g-p4ih-producer-bind): the manifest must be CLOSED under app.*
# imports of every .py it contains — both the scoring entry point (opening_rootcalc.py)
# AND the derivation entry point (opening_evidence.py) pull in modules whose bytes shape
# the captured overlay, so an import outside the manifest would let a dirty edit move
# evidence under an unchanged digest. check_scorer_source_manifest() enforces this as a
# fixpoint over static app.* imports; add whatever it names.
SCORER_SOURCE_FILES: tuple[str, ...] = (
    "backend/app/analysis_profiles.py",
    "backend/app/analysis_trust.py",
    "backend/app/centipawn_loss.py",
    "backend/app/database_url.py",
    "backend/app/db.py",
    "backend/app/evidence_contracts.py",
    "backend/app/fen.py",
    "backend/app/game_phase.py",
    "backend/app/models.py",
    "backend/app/move_classification.py",
    "backend/app/opening_aggregate.py",
    "backend/app/opening_cache.py",
    "backend/app/opening_evidence.py",
    "backend/app/opening_graph.py",
    "backend/app/opening_quality.py",
    "backend/app/opening_rootcalc.py",
    "backend/app/opening_roots.py",
    "backend/app/opening_score_scheduler.py",
    "backend/app/position_analysis_policy.py",
    "backend/app/position_analysis_repo.py",
    "backend/app/posthog_client.py",
    "backend/requirements.txt",
    "backend/scripts/calibrate_opening_scores_v2.py",
)

# The env var through which a launcher hands this process the manifest digest it computed
# BEFORE exec'ing the interpreter. See _SCORER_SOURCE_DIGEST_AT_IMPORT for what it buys.
SCORER_SOURCE_DIGEST_ENV = "GHOSTREPLAY_SCORER_SOURCE_DIGEST"

# Read at the TOP of the module — above `import chess` and every `from app...` below —
# and never re-read. The value's ONLY meaning is "a digest my launcher computed before I
# existed", so it has to be an INHERITED value. Reading os.environ later (at scoring time)
# would let code inside this process set the matching digest AFTER the scorer was compiled
# and so promote scorer_source_verified_preexec to True — manufacturing precisely the
# compile-window proof the flag is supposed to represent. Captured once, immutable
# thereafter: a late os.environ write cannot upgrade the flag.
#
# Residual: if something in the SAME process imported app.* before this module and wrote the
# var, the capture still follows that compile. Only launching from an exclusive checkout
# closes that — release_calibration_launcher.py does, by exec'ing a fresh interpreter on a
# private worktree. This is the strongest an in-process read can be.
_LAUNCHER_SCORER_DIGEST: str | None = (
    os.environ.get(SCORER_SOURCE_DIGEST_ENV, "").strip().lower() or None
)


def _bytecode_cache_state() -> dict[str, str]:
    """For each manifest .py, classify the bytecode cache CPython is about to consult —
    specifically, whether it could hand the interpreter code that does NOT come from the
    source bytes the digest hashes.

    A source digest binds .py bytes; the interpreter runs .pyc. Under CPython's DEFAULT
    timestamp invalidation a cached .pyc is accepted whenever the source's (mtime, size)
    match the pair recorded in its header — so an edit of the SAME SIZE with the mtime
    preserved executes the OLD bytecode while every source digest hashes the NEW file.
    Verified, on this interpreter, in TestBytecodeFreshness.

    Verdicts:
      "absent"       — no cached .pyc: CPython must compile the source we hashed.
      "unusable"     — magic mismatch, or a checked-hash .pyc whose hash does not match the
                       source: CPython rejects it and recompiles from the source we hashed.
      "checked-hash" — PEP 552 hash-based WITH check_source: CPython verifies the .pyc
                       against the source CONTENT, which is exactly our guarantee.
      "timestamp"    — forgeable (mtime, size) validation. UNSAFE.
      "unchecked-hash" — hash-based without check_source: trusted blindly. UNSAFE.
    """
    state: dict[str, str] = {}
    for rel in SCORER_SOURCE_FILES:
        if not rel.endswith(".py"):
            continue  # requirements.txt is never compiled
        source = _REPO_ROOT / rel
        try:
            head = Path(importlib.util.cache_from_source(str(source))).read_bytes()[:16]
        except OSError:
            state[rel] = "absent"
            continue
        if len(head) < 16 or head[:4] != importlib.util.MAGIC_NUMBER:
            state[rel] = "unusable"
            continue
        flags = int.from_bytes(head[4:8], "little")
        if not flags & 0b1:
            state[rel] = "timestamp"
        elif not flags & 0b10:
            state[rel] = "unchecked-hash"
        else:
            try:
                expected = importlib.util.source_hash(source.read_bytes())
            except OSError:
                state[rel] = "unusable"
                continue
            state[rel] = "checked-hash" if head[8:16] == expected else "unusable"
    return state


# Snapshot BEFORE the imports below. Importing app.* compiles those modules and — unless
# bytecode writing is off — CPython then WRITES the .pyc, destroying the evidence of what
# the cache looked like when the decision to use it was taken.
_BYTECODE_CACHE_AT_IMPORT: dict[str, str] = _bytecode_cache_state()

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
from app.opening_cache import (  # noqa: E402
    SCORE_MODEL_VERSION,
    capture_freshness_snapshot,
    current_cache_epoch,
    evidence_derivation_fingerprint,
    list_opening_score_candidate_pairs,
)
from app.opening_graph import OpeningGraph, OpeningGraphNode, get_opening_graph  # noqa: E402
from app.opening_quality import (  # noqa: E402
    SOURCE_ANALYSIS_CACHE,
    SOURCE_EVAL_DELTA,
    SOURCE_SESSION_EVAL,
    TAU_CP,
    TAU_WC,
)
from app.opening_rootcalc import (  # noqa: E402
    REPORT_SCORER_CONTRACT_ID,
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
    # RootScore.confidence per named root. Carried, not discarded: confidence is
    # sample_conf * freshness, and freshness reads the clock — so it is the surface where a
    # clock leak shows up EVEN IF the opening_score happens to be unmoved (a non-user-turn
    # node folds c_n=1.0). Determinism tests compare it; see CellScore.
    named_confidence_map: dict[str, float] = field(default_factory=dict)
    synthetic_score: float | None = None
    synthetic_confidence: float | None = None
    observation_total: int = 0
    source_counts: Counter[str] = field(default_factory=Counter)
    excluded_sessions: int = 0
    phase_samples: list[PhaseSample] = field(default_factory=list)
    telemetry: CalcTelemetry = field(default_factory=CalcTelemetry)
    scoring_seconds: float = 0.0
    # Frozen-cohort pseudonym metadata (g-p4ih-artifact). All default None so the
    # live scoring path — which omits them — leaves every existing field unchanged.
    # ``surrogate_user_id`` rides the EXISTING integer ``user_id`` field; the string
    # pseudonyms ride here ALONGSIDE, never through the int contract.
    pair_id: str | None = None
    subject_id: str | None = None
    cohort_role: str | None = None

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
    *,
    as_of: datetime,
    pair_id: str | None = None,
    subject_id: str | None = None,
    cohort_role: str | None = None,
) -> PairScore:
    """Score one overlay in memory and separate the synthetic hero row.

    Passes ``include_synthetic_root=True`` so the ``__repertoire__`` hero row is
    computed in the same DAG pass, then reports it in its own field rather than
    mixing it into the named-root distribution.

    ``as_of`` is REQUIRED keyword-only with NO default (g-p4ih-replay-bind): it is
    threaded straight into ``compute_all_root_scores(..., now=as_of)`` so no caller can
    fall through to a silent ``datetime.now``. Every caller is exactly one of the three
    clock paths — the frozen-artifact header (release), ``SYNTHETIC_AS_OF`` (standalone
    synthetics), or a once-sampled ``run_as_of`` (the live report). Cutoff
    reproducibility is thereby a CONTRACT, not an undocumented invariant.

    ``pair_id`` / ``subject_id`` / ``cohort_role`` are the OPTIONAL frozen-cohort
    pseudonym fields (g-p4ih-artifact); they are forwarded VERBATIM into ``PairScore``
    and default None, so the live path (which omits them) is unperturbed. The frozen
    path passes ``user_id=surrogate_user_id`` so no production user_id is reconstructed.
    """
    telemetry = CalcTelemetry()
    started = time.perf_counter()
    scores, _eligible = compute_all_root_scores(
        player_color,
        graph,
        overlay,
        roots,
        config or RootCalcConfig(),
        now=as_of,
        include_branch_summaries=False,
        include_synthetic_root=True,
        telemetry=telemetry,
    )
    elapsed = time.perf_counter() - started

    synthetic = scores.get(SYNTHETIC_INITIAL_FEN)
    named = {key: score for key, score in scores.items() if key != SYNTHETIC_INITIAL_FEN}
    named_score_map = {key: score.opening_score for key, score in named.items()}
    named_confidence_map = {key: score.confidence for key, score in named.items()}
    named_scores = list(named_score_map.values())
    observation_total = sum(node.quality_count for node in overlay.nodes.values())

    return PairScore(
        user_id=user_id,
        player_color=player_color,
        named_scores=named_scores,
        named_score_map=named_score_map,
        named_confidence_map=named_confidence_map,
        synthetic_score=synthetic.opening_score if synthetic is not None else None,
        synthetic_confidence=synthetic.confidence if synthetic is not None else None,
        observation_total=observation_total,
        source_counts=Counter(overlay.source_counts),
        excluded_sessions=overlay.excluded_sessions,
        phase_samples=list(overlay.phase_samples),
        telemetry=telemetry,
        scoring_seconds=elapsed,
        pair_id=pair_id,
        subject_id=subject_id,
        cohort_role=cohort_role,
    )


def score_pair(
    db,
    user_id: int,
    player_color: str,
    graph: OpeningGraph,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
    *,
    as_of: datetime,
) -> PairScore:
    """Build the overlay for a pair (DB read only) and score it in memory.

    ``as_of`` is REQUIRED keyword-only (g-p4ih-replay-bind) and forwarded to
    ``score_overlay`` so the scoring clock can never fall through to ``datetime.now``.
    """
    overlay = overlay_evidence(db, user_id, player_color, graph)
    return score_overlay(user_id, player_color, graph, overlay, roots, config, as_of=as_of)


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
    *,
    as_of: datetime,
) -> dict[GridCell, PairScore]:
    """Build a pair's overlay ONCE (the ~2.6s cost), then score it per grid cell.

    Naively gridding would rebuild ``overlay_evidence`` O(pairs x cells) times; here
    the (DB-read) overlay is built once and only the cheap in-memory
    ``score_overlay`` runs per cell. ``cells`` is ``ArmGrid.cells`` — a bare cell tuple,
    never the ArmGrid wrapper (cohort scoring never reads roles).

    ``as_of`` is REQUIRED keyword-only (g-p4ih-replay-bind): the SAME clock scores every
    cell of every pair, so a two-pair run scored minutes apart cannot see two clocks.
    """
    overlay = overlay_evidence(db, user_id, player_color, graph)
    return {
        cell: score_overlay(
            user_id, player_color, graph, overlay, roots, cell.config, as_of=as_of
        )
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
# Frozen-cohort artifact (g-p4ih-artifact): schema, canonical encoding, semantic
# validation, split load guard, release-guard binding.
#
# The artifact is the pseudonymized, byte-stable serialization of raw opening
# EvidenceOverlays. The freeze transform (capture-side) turns overlays + as_of into
# canonical bytes; the split load guard (release-side) refuses a tampered, stale,
# drifted, or wrong-target artifact rather than mis-scoring or falling back to live
# selection. Production-derived artifacts stay PRIVATE (governance stance); only the
# schema/validation/load code lives here, exercised in CI against SYNTHETIC data.
#
# BYTE-STABILITY CONTRACT: the SHA-256 reproducibility guarantee is pinned to a
# CPython runtime (>= 3.1), where the shortest-round-trip float repr is guaranteed
# (float_repr_style == 'short' / David-Gay). ``quality_sum`` is the only float field;
# integer microsecond offsets, ints, and ASCII-escaped strings carry no such
# dependency. A non-CPython interpreter is OUT of the byte-stability contract.
# ---------------------------------------------------------------------------

# v2 (g-p4ih-producer-bind): the header + provenance record carry the four
# capture-attestation fields (capture_scorer_source_digest / capture_source_revision /
# capture_python_version / capture_chess_version). The key sets are closed, so widening
# them is a schema bump: a v1 artifact/record is refused with
# UnsupportedArtifactSchemaError, never a missing-keys error.
ARTIFACT_SCHEMA_VERSION: int = 2
# The pinned lower instant used only to bound the offset range and keep offset
# reconstruction (as_of - timedelta(us)) total. A fixed aware-UTC constant, never a
# per-run value.
TIMESTAMP_FLOOR: datetime = datetime(2000, 1, 1, tzinfo=timezone.utc)
# The FIXED, VERSIONED identifier for the membership rule. A membership-rule change is
# a conscious version bump of this constant, never a free-form edit.
COHORT_RULES_ID: str = "opening-cohort-rules-v1"

# The single exact canonical as_of spelling — 6 fractional digits + literal Z.
_AS_OF_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# Git privacy-boundary format controls: committed values carry a version number and
# nothing else (a free-text channel would let identifying detail into Git history).
_COHORT_RULES_RE = re.compile(r"^opening-cohort-rules-v\d+$")
_CAPTURED_MODEL_VERSION_RE = re.compile(r"^sm-v\d+-\d+$")
_PAIR_ID_RE = re.compile(r"^pair-\d+$")
_SUBJECT_ID_RE = re.compile(r"^subject-\d+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Capture attestation (g-p4ih-producer-bind): format-pinned, never free-form — the
# provenance record is committed to Git, so an open string would be an identifying-detail
# channel. capture_scorer_source_digest reuses _SHA256_RE.
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
# The interpreter IMPLEMENTATION and version, pinned to CPython: the byte-stability
# contract above rests on CPython's shortest-round-trip float repr, so a non-CPython
# capture runtime is out of contract and its artifact is rejected on format. This is a
# byte-stability FORMAT constraint, NOT a capture-vs-release environment-equality gate.
_CAPTURE_PYTHON_VERSION_RE = re.compile(r"^CPython \d+\.\d+\.\d+$")
# python-chess's three-segment release scheme (the version of the IMPORTED chess module,
# chess.__version__). If python-chess ever ships a two-segment or suffixed version, this
# regex is the single place to widen.
_CAPTURE_CHESS_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# White-before-black source ordering for deterministic pseudonym assignment (mirrors
# opening_evidence._COLOR_RANK; defined locally to avoid importing a private symbol).
_ARTIFACT_COLOR_RANK = {"white": 0, "black": 1}
_VALID_COLORS = ("white", "black")
_VALID_COHORT_ROLES = ("quantile", "release_guard")
_SOURCE_LABELS = frozenset({SOURCE_SESSION_EVAL, SOURCE_ANALYSIS_CACHE, SOURCE_EVAL_DELTA})


def _derive_opening_key(*ucis: str) -> str:
    """Normalized 4-field opening_key reached by playing ``ucis`` from the start.

    Derived from ``chess.Board`` + ``normalize_fen`` so the release-guard keys are
    NEVER a hand-typed FEN; the derivation-pin tests assert the constants equal this.
    """
    board = chess.Board()
    for uci in ucis:
        board.push_uci(uci)
    return normalize_fen(board.fen())


# The pinned gate-(ii) root keys. RELEASE_GUARD_OPENING_KEY is the King's Pawn Game
# root (1.e4); RELEASE_GUARD_CHILD_OPENING_KEY the Caro-Kann Defense node (1.e4 c6).
RELEASE_GUARD_OPENING_KEY: str = _derive_opening_key("e2e4")
RELEASE_GUARD_CHILD_OPENING_KEY: str = _derive_opening_key("e2e4", "c7c6")


# ---- Fail-closed exception hierarchy (each raises a DISTINCT message string) ----


class FrozenArtifactError(ValueError):
    """Base for every frozen-cohort artifact rejection. ALWAYS fails closed — the
    caller never falls back to live pair selection (governance stance)."""


class UnsupportedArtifactSchemaError(FrozenArtifactError):
    """schema_version selects the decoder; an unsupported version is refused in the
    DECODE/SCHEMA phase BEFORE any integrity comparison."""


class ArtifactSemanticError(FrozenArtifactError):
    """Payload-level well-formedness failure (shape, exact types, ranges, cross-field
    equations, FEN/color/edge legality, duplicate detection, canonical structure) —
    provenance-independent (Phase A)."""


class ArtifactCanonicalBytesError(FrozenArtifactError):
    """The validated payload does not re-encode byte-for-byte to the raw artifact
    bytes — a non-canonical serialization that survived a valid semantic pass."""


class ProvenanceRecordError(FrozenArtifactError):
    """The committed provenance record violates its own closed schema (a Git
    privacy-boundary control), decoded from raw bytes inside the guard."""


class ArtifactIntegrityError(FrozenArtifactError):
    """Phase B: the recomputed digest or a mirrored header field disagrees with the
    injected provenance record — a tampered, stale, or wrong artifact."""


class ArtifactScoringValidityError(FrozenArtifactError):
    """Phase C: the header drifted from the current runtime (graph/roots/derivation
    fingerprints or release keys) or violates a release-policy pin (cohort_rules /
    min_observations) — a matching provenance record cannot vouch for these."""


class ReleaseGuardShapeError(FrozenArtifactError):
    """The release-guard artifact shape, the scored release-guard shape, or the
    minimum pooled-quantile-score precondition is not met."""


# ---- Runtime binding + producer/loader handoff types ----


@dataclass(frozen=True)
class RuntimeBinding:
    """The current-runtime surfaces the SCORING-VALIDITY (Phase C) checks compare
    against, PLUS the supported schema version the decoder resolves against.

    Passed IN (not read from module globals inside the guard) so the load guard is
    testable with SYNTHETIC bindings, with NO dependency on a committed file.
    """

    graph_fingerprint: str
    roots_fingerprint: str
    evidence_derivation_fingerprint: str
    min_observations: int
    cohort_rules: str
    release_guard_opening_key: str
    release_guard_child_opening_key: str
    schema_version: int = ARTIFACT_SCHEMA_VERSION


@dataclass(frozen=True)
class CapturedPairInput:
    """One raw production overlay plus the freeze-side metadata that does NOT ride on
    the bare ``EvidenceOverlay``. The pseudonyms are assigned INSIDE the freeze."""

    overlay: EvidenceOverlay
    cohort_role: str  # "quantile" | "release_guard"
    evidence_seq: int  # per-pair freshness sequence (>= 0)
    inputs_fingerprint: str  # per-pair freshness fingerprint (non-empty)


@dataclass(frozen=True)
class ArtifactHeaderInput:
    """The run-specific provenance the freeze stamps into the header. The release-policy
    pins (min_observations / cohort_rules / release keys / pair_count / schema_version)
    are NOT here — the freeze stamps those from the pinned module constants."""

    as_of: datetime  # the pinned scoring clock (aware-UTC)
    graph_fingerprint: str
    roots_fingerprint: str
    cache_epoch: int | None
    captured_model_version: str  # SCORE_MODEL_VERSION at capture, ^sm-v\d+-\d+$
    evidence_derivation_fingerprint: str
    # Capture attestation (g-p4ih-producer-bind): WHO manufactured the overlay — recorded
    # and reviewed, never equality-gated at load. g-p4ih-capture computes the values.
    capture_scorer_source_digest: str  # scorer_source_digest() over the closed manifest
    capture_source_revision: str  # the resolved HEAD commit, ^[0-9a-f]{40}$
    capture_python_version: str  # f"{python_implementation()} {python_version()}", CPython-pinned
    capture_chess_version: str  # the IMPORTED chess.__version__ (never importlib.metadata)


@dataclass(frozen=True)
class LoadedHeader:
    """The validated, fully-typed header (``as_of`` reconstructed as an aware datetime)."""

    schema_version: int
    as_of: datetime
    captured_model_version: str
    graph_fingerprint: str
    roots_fingerprint: str
    evidence_derivation_fingerprint: str
    min_observations: int
    pair_count: int
    cohort_rules: str
    release_guard_opening_key: str
    release_guard_child_opening_key: str
    cache_epoch: int | None
    capture_scorer_source_digest: str
    capture_source_revision: str
    capture_python_version: str
    capture_chess_version: str


@dataclass(frozen=True)
class LoadedPair:
    """The three coordinate systems a bare overlay cannot carry, reunited: pseudonym
    metadata, freshness metadata, and the reconstructed ``EvidenceOverlay``."""

    pair_id: str
    subject_id: str
    cohort_role: str
    surrogate_user_id: int
    player_color: str
    evidence_seq: int
    inputs_fingerprint: str
    overlay: EvidenceOverlay


@dataclass(frozen=True)
class LoadedCohort:
    """``load_frozen_artifact``'s return: the validated header, the recomputed artifact
    digest (the integrity anchor), and the ordered loaded pairs (pair-00 … order)."""

    header: LoadedHeader
    artifact_sha256: str
    pairs: tuple[LoadedPair, ...]


@dataclass(frozen=True)
class _ValidatedProvenance:
    """Internal: the provenance record after closed-schema validation, with ``as_of``
    reconstructed as an aware datetime so integrity compares datetimes to datetimes."""

    sha256: str
    schema_version: int
    as_of: datetime
    captured_model_version: str
    graph_fingerprint: str
    roots_fingerprint: str
    evidence_derivation_fingerprint: str
    pair_count: int
    min_observations: int
    cohort_rules: str
    release_guard_opening_key: str
    release_guard_child_opening_key: str
    capture_scorer_source_digest: str
    capture_source_revision: str
    capture_python_version: str
    capture_chess_version: str


# ---- Canonical encoding primitives ----


def _canonical_dumps(payload: object) -> bytes:
    """The EXACT canonical serializer. Sorted keys, no insignificant whitespace,
    ASCII-escaped (locale/terminal cannot change bytes), NaN/Infinity forbidden. The
    ``.encode("ascii")`` is the PINNED total codec — any non-ASCII code point is an
    ``ensure_ascii`` violation that raises rather than silently widening the stream."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _canonical_float(value: float) -> float:
    """Pin a float field's canonical form: finite, ``-0.0`` normalized to ``0.0``.

    No rounding — the shortest-round-trip repr CPython emits is already exact and
    lossless (rounding ``quality_sum`` would move the score the scorer divides)."""
    f = float(value)
    if not math.isfinite(f):
        raise ArtifactSemanticError(f"non-finite float not allowed: {f!r}")
    if f == 0.0:  # True for both 0.0 and -0.0; normalize the sign
        return 0.0
    return f


def _canonical_as_of(dt: datetime) -> str:
    """The single canonical as_of spelling (6 fractional digits + literal Z, UTC)."""
    return dt.astimezone(timezone.utc).strftime(_AS_OF_FORMAT)


def _require_aware(dt: datetime, label: str) -> None:
    """A naive datetime silently breaks byte-stability (astimezone interprets it in the
    HOST's local zone), so reject it BEFORE any encoding with a DISTINCT message."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ArtifactSemanticError(f"{label} must be timezone-aware; got naive {dt!r}")


def _us_before(as_of: datetime, ts: datetime | None) -> int | None:
    """Integer-microsecond as_of-relative offset (null when absent). Exact: Python
    datetimes are microsecond-resolution, so the round trip reconstructs bit-for-bit."""
    if ts is None:
        return None
    _require_aware(ts, "evidence timestamp")
    return (as_of - ts) // timedelta(microseconds=1)


def _token_suffix(token: str) -> int:
    """Integer suffix ``k`` of ``{pair_id}-g{k}`` (numeric, so g2 precedes g10)."""
    return int(token.rsplit("-g", 1)[1])


def _phase_sample_sort_key(oil: int, mp: int | None, ep: int | None) -> tuple:
    """Nondecreasing order over (opening_interval_len, middle_ply, end_ply), null last."""
    return (oil, (1, 0) if mp is None else (0, mp), (1, 0) if ep is None else (0, ep))


# ---- Freeze (capture-side transform; g-p4ih-capture RUNS it) ----


def freeze_frozen_artifact(
    pairs: Sequence[CapturedPairInput], header: ArtifactHeaderInput
) -> bytes:
    """Serialize raw overlays + ``as_of`` into canonical, byte-stable artifact bytes.

    Assigns every pseudonym (pair_id / subject_id / surrogate_user_id / session tokens)
    by the DETERMINISTIC sorted-source procedure, so the emitted bytes are a pure
    function of the raw overlays, independent of dict/set iteration order. RETURNS the
    canonical bytes — NOT a provenance record: the caller (g-p4ih-capture) computes
    ``hashlib.sha256(<those bytes>)`` for the record. Fails on a duplicate source
    ``(user_id, player_color)`` or any naive datetime BEFORE encoding.
    """
    _require_aware(header.as_of, "header.as_of")

    seen: set[tuple[int, str]] = set()
    for cp in pairs:
        key = (cp.overlay.user_id, cp.overlay.player_color)
        if key in seen:
            raise ArtifactSemanticError(
                f"freeze: duplicate source pair (user_id, player_color)={key}; the "
                "pseudonym bijection assumes one overlay per (user_id, color)"
            )
        seen.add(key)

    ordered = sorted(
        pairs,
        key=lambda cp: (cp.overlay.user_id, _ARTIFACT_COLOR_RANK[cp.overlay.player_color]),
    )

    # Subjects: per distinct source user_id in that same sorted order.
    subject_by_user: dict[int, str] = {}
    for cp in ordered:
        uid = cp.overlay.user_id
        if uid not in subject_by_user:
            subject_by_user[uid] = f"subject-{len(subject_by_user):02d}"

    pair_objs = [
        _freeze_pair(cp, f"pair-{i:02d}", subject_by_user[cp.overlay.user_id], i + 1, header.as_of)
        for i, cp in enumerate(ordered)
    ]

    payload = {
        "header": {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "as_of": _canonical_as_of(header.as_of),
            "captured_model_version": header.captured_model_version,
            "graph_fingerprint": header.graph_fingerprint,
            "roots_fingerprint": header.roots_fingerprint,
            "evidence_derivation_fingerprint": header.evidence_derivation_fingerprint,
            "min_observations": DEFAULT_MIN_OBSERVATIONS,
            "pair_count": len(pair_objs),
            "cohort_rules": COHORT_RULES_ID,
            "release_guard_opening_key": RELEASE_GUARD_OPENING_KEY,
            "release_guard_child_opening_key": RELEASE_GUARD_CHILD_OPENING_KEY,
            "cache_epoch": header.cache_epoch,
            "capture_scorer_source_digest": header.capture_scorer_source_digest,
            "capture_source_revision": header.capture_source_revision,
            "capture_python_version": header.capture_python_version,
            "capture_chess_version": header.capture_chess_version,
        },
        "pairs": pair_objs,
    }
    return _canonical_dumps(payload)


def _freeze_pair(
    cp: CapturedPairInput, pair_id: str, subject_id: str, surrogate: int, as_of: datetime
) -> dict:
    overlay = cp.overlay
    # Session tokens: g{k} assigned CONTIGUOUSLY from the SORTED distinct raw session_ids
    # across ALL the pair's nodes (sorting the RAW ids before enumeration fixes which
    # real session becomes g0). The same real session maps to the same token at every
    # node, so the cross-node union reconstructs production's exactly.
    all_sessions = sorted(
        {sid for node in overlay.nodes.values() for sid in node.session_ids}
    )
    token_by_session = {sid: f"{pair_id}-g{k}" for k, sid in enumerate(all_sessions)}

    node_objs = [
        _freeze_node(node, token_by_session, as_of)
        for node in sorted(overlay.nodes.values(), key=lambda n: n.fen)
    ]
    edge_objs = [
        _freeze_edge(overlay.edges[key])
        for key in sorted(overlay.edges)
    ]
    phase_samples = [
        {"opening_interval_len": s.opening_interval_len, "middle_ply": s.middle_ply, "end_ply": s.end_ply}
        for s in sorted(
            overlay.phase_samples,
            key=lambda s: _phase_sample_sort_key(s.opening_interval_len, s.middle_ply, s.end_ply),
        )
    ]
    return {
        "pair_id": pair_id,
        "player_color": overlay.player_color,
        "subject_id": subject_id,
        "cohort_role": cp.cohort_role,
        "surrogate_user_id": surrogate,
        "evidence_seq": cp.evidence_seq,
        "inputs_fingerprint": cp.inputs_fingerprint,
        # Zero-valued labels are OMITTED (unary-plus drops non-positive counts) so the
        # bytes are a pure function of which SOURCE_* labels actually fired.
        "source_counts": dict(+Counter(overlay.source_counts)),
        "excluded_sessions": overlay.excluded_sessions,
        "phase_samples": phase_samples,
        "nodes": node_objs,
        "edges": edge_objs,
    }


def _freeze_node(node: NodeEvidence, token_by_session: dict[str, str], as_of: datetime) -> dict:
    tokens = sorted(
        (token_by_session[sid] for sid in node.session_ids), key=_token_suffix
    )
    return {
        "fen": node.fen,
        "quality_sum": _canonical_float(node.quality_sum),
        "quality_count": node.quality_count,
        "live_attempts": node.live_attempts,
        "live_passes": node.live_passes,
        "live_fails": node.live_fails,
        "review_attempts": node.review_attempts,
        "review_passes": node.review_passes,
        "review_fails": node.review_fails,
        "is_ghost_target": node.is_ghost_target,
        "session_tokens": tokens,
        "last_live_us_before": _us_before(as_of, node.last_live_at),
        "last_review_us_before": _us_before(as_of, node.last_review_at),
    }


def _freeze_edge(edge: EdgeEvidence) -> dict:
    return {
        "parent_fen": edge.parent_fen,
        "child_fen": edge.child_fen,
        "uci": edge.uci,
        "traversal_count": edge.traversal_count,
        "live_attempts": edge.live_attempts,
        "live_passes": edge.live_passes,
        "live_fails": edge.live_fails,
        "quality_sum": _canonical_float(edge.quality_sum),
        "quality_count": edge.quality_count,
    }


# ---- Decode hardening + type/shape primitives (bool-is-not-int) ----


def _hardened_loads(raw: bytes, err: type[FrozenArtifactError]):
    """``json.loads`` with a parse_constant raiser (Infinity/NaN refused, symmetric with
    the encoder's ``allow_nan=False``) and a duplicate-object-key hook (``json.loads``
    otherwise silently last-write-wins). ``json.loads`` accepts bytes directly."""

    def _raise_constant(value):
        raise err(f"non-finite JSON literal not allowed: {value!r}")

    def _no_dupes(items):
        result: dict = {}
        for key, value in items:
            if key in result:
                raise err(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(raw, parse_constant=_raise_constant, object_pairs_hook=_no_dupes)
    except json.JSONDecodeError as exc:
        raise err(f"not valid JSON: {exc}") from exc


def _require_keys(obj: object, allowed: frozenset[str], label: str, *, err=ArtifactSemanticError) -> dict:
    if not isinstance(obj, dict):
        raise err(f"{label} must be a JSON object; got {type(obj).__name__}")
    keys = set(obj)
    missing = allowed - keys
    if missing:
        raise err(f"{label} missing required key(s): {sorted(missing)}")
    unknown = keys - allowed
    if unknown:
        raise err(f"{label} has unknown key(s): {sorted(unknown)}")
    return obj


def _check_int(value: object, label: str, *, minimum: int | None = None, err=ArtifactSemanticError) -> int:
    # bool subclasses int, so isinstance(True, int) passes — reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise err(f"{label} must be an int (bool rejected); got {value!r}")
    if minimum is not None and value < minimum:
        raise err(f"{label} must be >= {minimum}; got {value}")
    return value


def _check_int_or_null(value: object, label: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return _check_int(value, label, minimum=minimum)


def _check_float(value: object, label: str) -> float:
    # A bare int is rejected — the canonical encoding pins floats-stay-floats.
    if isinstance(value, bool) or not isinstance(value, float):
        raise ArtifactSemanticError(f"{label} must be a float; got {value!r}")
    if not math.isfinite(value):
        raise ArtifactSemanticError(f"{label} must be finite; got {value!r}")
    return value


def _check_str(value: object, label: str, *, err=ArtifactSemanticError) -> str:
    if not isinstance(value, str):
        raise err(f"{label} must be a string; got {type(value).__name__}")
    return value


def _check_nonempty_str(value: object, label: str, *, err=ArtifactSemanticError) -> str:
    s = _check_str(value, label, err=err)
    if not s:
        raise err(f"{label} must be non-empty")
    return s


def _check_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ArtifactSemanticError(f"{label} must be a bool; got {value!r}")
    return value


def _validate_canonical_as_of(value: object, label: str, *, err=ArtifactSemanticError) -> datetime:
    """Reconstruct the pinned as_of and REJECT any string not byte-identical to its
    canonical re-emission (a +00:00, missing-Z, or non-6-fractional-digit variant)."""
    s = _check_str(value, label, err=err)
    try:
        dt = datetime.strptime(s, _AS_OF_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise err(f"{label} not in canonical %Y-%m-%dT%H:%M:%S.%fZ form: {s!r}") from exc
    if _canonical_as_of(dt) != s:
        raise err(f"{label} not byte-identical to canonical re-emission: {s!r}")
    return dt


def _validate_fen(fen: str, label: str) -> None:
    try:
        chess.Board(fen)
    except ValueError as exc:
        raise ArtifactSemanticError(f"{label} is not a parseable FEN: {fen!r}") from exc
    if normalize_fen(fen) != fen:
        raise ArtifactSemanticError(f"{label} is not in normalized 4-field form: {fen!r}")


# ---- Closed key sets ----

_TOP_KEYS = frozenset({"header", "pairs"})
_HEADER_KEYS = frozenset({
    "schema_version", "as_of", "captured_model_version", "graph_fingerprint",
    "roots_fingerprint", "evidence_derivation_fingerprint", "min_observations",
    "pair_count", "cohort_rules", "release_guard_opening_key",
    "release_guard_child_opening_key", "cache_epoch",
    "capture_scorer_source_digest", "capture_source_revision",
    "capture_python_version", "capture_chess_version",
})
_PAIR_KEYS = frozenset({
    "pair_id", "player_color", "subject_id", "cohort_role", "surrogate_user_id",
    "evidence_seq", "inputs_fingerprint", "source_counts", "excluded_sessions",
    "phase_samples", "nodes", "edges",
})
_NODE_KEYS = frozenset({
    "fen", "quality_sum", "quality_count", "live_attempts", "live_passes",
    "live_fails", "review_attempts", "review_passes", "review_fails",
    "is_ghost_target", "session_tokens", "last_live_us_before", "last_review_us_before",
})
_EDGE_KEYS = frozenset({
    "parent_fen", "child_fen", "uci", "traversal_count", "live_attempts",
    "live_passes", "live_fails", "quality_sum", "quality_count",
})
_PHASE_SAMPLE_KEYS = frozenset({"opening_interval_len", "middle_ply", "end_ply"})
_PROVENANCE_KEYS = frozenset({
    "sha256", "schema_version", "as_of", "captured_model_version", "graph_fingerprint",
    "roots_fingerprint", "evidence_derivation_fingerprint", "pair_count",
    "min_observations", "cohort_rules", "release_guard_opening_key",
    "release_guard_child_opening_key",
    "capture_scorer_source_digest", "capture_source_revision",
    "capture_python_version", "capture_chess_version",
})


# ---- Semantic validation (Phase A; provenance-independent well-formedness) ----


def _dispatch_schema_version(
    obj: object, obj_label: str, field_label: str, supported: int, *, err: type[FrozenArtifactError]
) -> int:
    """Schema-version DISPATCH, run BEFORE any closed key set (g-p4ih-producer-bind).

    The v2 closed key sets include the four capture-attestation fields, so a GENUINE
    prior-schema object — one that lacks them — would fail inside ``_require_keys`` as
    "missing required key(s)" and the promised version diagnostic would never fire.
    Requires the object be a mapping and ``schema_version`` a present int, then refuses
    any unsupported version; only after that may a closed key set apply."""
    if not isinstance(obj, dict):
        raise err(f"{obj_label} must be a JSON object; got {type(obj).__name__}")
    if "schema_version" not in obj:
        raise err(f"{obj_label} missing required key(s): ['schema_version']")
    version = _check_int(obj["schema_version"], field_label, err=err)
    if version != supported:
        raise UnsupportedArtifactSchemaError(
            f"unsupported schema: {field_label}={version}, only {supported} is supported"
        )
    return version


def _validate_capture_attestation(
    obj: Mapping[str, object], prefix: str, *, err: type[FrozenArtifactError]
) -> dict[str, str]:
    """Presence + FORMAT validation of the four capture-attestation fields
    (g-p4ih-producer-bind), shared by the header and the provenance record. Recorded and
    reviewed, never equality-gated at load — nothing here compares against the current
    environment."""
    digest = _check_str(obj["capture_scorer_source_digest"], f"{prefix}.capture_scorer_source_digest", err=err)
    if not _SHA256_RE.match(digest):
        raise err(
            f"{prefix}.capture_scorer_source_digest must be 64-char lowercase hex; got {digest!r}"
        )
    revision = _check_str(obj["capture_source_revision"], f"{prefix}.capture_source_revision", err=err)
    if not _GIT_REVISION_RE.match(revision):
        raise err(
            f"{prefix}.capture_source_revision must be a 40-char lowercase hex commit; got {revision!r}"
        )
    python_version = _check_str(obj["capture_python_version"], f"{prefix}.capture_python_version", err=err)
    if not _CAPTURE_PYTHON_VERSION_RE.match(python_version):
        raise err(
            f"{prefix}.capture_python_version must match ^CPython \\d+.\\d+.\\d+$ (the "
            f"byte-stability contract is CPython-only); got {python_version!r}"
        )
    chess_version = _check_str(obj["capture_chess_version"], f"{prefix}.capture_chess_version", err=err)
    if not _CAPTURE_CHESS_VERSION_RE.match(chess_version):
        raise err(
            f"{prefix}.capture_chess_version must match ^\\d+.\\d+.\\d+$; got {chess_version!r}"
        )
    return {
        "capture_scorer_source_digest": digest,
        "capture_source_revision": revision,
        "capture_python_version": python_version,
        "capture_chess_version": chess_version,
    }


def _validate_header(header: object, *, supported_schema_version: int = ARTIFACT_SCHEMA_VERSION) -> LoadedHeader:
    schema_version = _dispatch_schema_version(
        header, "header", "header.schema_version", supported_schema_version,
        err=ArtifactSemanticError,
    )
    obj = _require_keys(header, _HEADER_KEYS, "header")
    as_of = _validate_canonical_as_of(obj["as_of"], "header.as_of")
    if as_of < TIMESTAMP_FLOOR:
        raise ArtifactSemanticError(
            f"header.as_of {as_of.isoformat()} precedes TIMESTAMP_FLOOR "
            f"{TIMESTAMP_FLOOR.isoformat()}"
        )
    captured_model_version = _check_str(obj["captured_model_version"], "header.captured_model_version")
    if not _CAPTURED_MODEL_VERSION_RE.match(captured_model_version):
        raise ArtifactSemanticError(
            f"header.captured_model_version must match ^sm-v\\d+-\\d+$; got "
            f"{captured_model_version!r}"
        )
    cohort_rules = _check_str(obj["cohort_rules"], "header.cohort_rules")
    if not _COHORT_RULES_RE.match(cohort_rules):
        raise ArtifactSemanticError(
            f"header.cohort_rules must match ^opening-cohort-rules-v\\d+$; got "
            f"{cohort_rules!r}"
        )
    cache_epoch = obj["cache_epoch"]
    if cache_epoch is not None:
        cache_epoch = _check_int(cache_epoch, "header.cache_epoch", minimum=0)
    attestation = _validate_capture_attestation(obj, "header", err=ArtifactSemanticError)
    return LoadedHeader(
        schema_version=schema_version,
        as_of=as_of,
        captured_model_version=captured_model_version,
        graph_fingerprint=_check_nonempty_str(obj["graph_fingerprint"], "header.graph_fingerprint"),
        roots_fingerprint=_check_nonempty_str(obj["roots_fingerprint"], "header.roots_fingerprint"),
        evidence_derivation_fingerprint=_check_nonempty_str(
            obj["evidence_derivation_fingerprint"], "header.evidence_derivation_fingerprint"
        ),
        min_observations=_check_int(obj["min_observations"], "header.min_observations", minimum=0),
        pair_count=_check_int(obj["pair_count"], "header.pair_count", minimum=0),
        cohort_rules=cohort_rules,
        release_guard_opening_key=_check_nonempty_str(
            obj["release_guard_opening_key"], "header.release_guard_opening_key"
        ),
        release_guard_child_opening_key=_check_nonempty_str(
            obj["release_guard_child_opening_key"], "header.release_guard_child_opening_key"
        ),
        cache_epoch=cache_epoch,
        **attestation,
    )


def _validate_source_counts(obj: object, label: str) -> dict[str, int]:
    if not isinstance(obj, dict):
        raise ArtifactSemanticError(f"{label} must be a JSON object; got {type(obj).__name__}")
    result: dict[str, int] = {}
    for key, value in obj.items():
        if key not in _SOURCE_LABELS:
            raise ArtifactSemanticError(
                f"{label} has unknown quality-source label: {key!r} "
                f"(allowed: {sorted(_SOURCE_LABELS)})"
            )
        # >= 1: a 0-valued entry is OMITTED in the canonical form, so an explicit 0
        # (or negative) is non-canonical and REJECTS here (its own diagnostic).
        result[key] = _check_int(value, f"{label}[{key!r}]", minimum=1)
    return result


def _validate_phase_samples(arr: object, label: str) -> list[PhaseSample]:
    if not isinstance(arr, list):
        raise ArtifactSemanticError(f"{label} must be an array; got {type(arr).__name__}")
    result: list[PhaseSample] = []
    prev_key = None
    for j, sample in enumerate(arr):
        obj = _require_keys(sample, _PHASE_SAMPLE_KEYS, f"{label}[{j}]")
        oil = _check_int(obj["opening_interval_len"], f"{label}[{j}].opening_interval_len", minimum=0)
        mp = _check_int_or_null(obj["middle_ply"], f"{label}[{j}].middle_ply", minimum=0)
        ep = _check_int_or_null(obj["end_ply"], f"{label}[{j}].end_ply", minimum=0)
        key = _phase_sample_sort_key(oil, mp, ep)
        # Nondecreasing (duplicates PRESERVED — one sample per non-excluded session);
        # a strictly-decreasing / out-of-order sample REJECTS.
        if prev_key is not None and key < prev_key:
            raise ArtifactSemanticError(
                f"{label} is not in nondecreasing (opening_interval_len, middle_ply, "
                f"end_ply) order at index {j}"
            )
        prev_key = key
        result.append(PhaseSample(opening_interval_len=oil, middle_ply=mp, end_ply=ep))
    return result


def _validate_session_tokens(tokens: object, pair_id: str, label: str) -> list[str]:
    if not isinstance(tokens, list):
        raise ArtifactSemanticError(f"{label} must be an array; got {type(tokens).__name__}")
    token_re = re.compile(rf"^{re.escape(pair_id)}-g(\d+)$")
    result: list[str] = []
    prev_k = None
    for t in tokens:
        if not isinstance(t, str):
            raise ArtifactSemanticError(f"{label} entries must be strings; got {t!r}")
        m = token_re.match(t)
        if m is None:
            raise ArtifactSemanticError(
                f"{label} entry {t!r} is not a ^{pair_id}-g\\d+$ token for this pair "
                "(a cross-pair or leaky label)"
            )
        k = int(m.group(1))
        # EXACT canonical ASCII spelling — `token == f"{pair_id}-g{k}"`. `\d+` alone
        # accepts a leading-zero alias (`g015`) that `int(...)` collapses to the same
        # `k` as `g15`, so the string↔k map would NOT be a bijection: two distinct
        # strings on different nodes would share one numeric suffix, passing the
        # (numeric) contiguity check while reconstruction keeps BOTH strings as distinct
        # session_ids and inflates the cross-node game_count union. Pinning the exact
        # spelling makes each k have one and only one token string.
        if t != f"{pair_id}-g{k}":
            raise ArtifactSemanticError(
                f"{label} entry {t!r} is not the canonical spelling {pair_id}-g{k} "
                "(leading-zero or non-minimal token index)"
            )
        # Strictly ascending by NUMERIC k (g2 before g10) — also enforces uniqueness.
        if prev_k is not None and not (k > prev_k):
            raise ArtifactSemanticError(
                f"{label} is not strictly ascending by numeric token index at {t!r}"
            )
        prev_k = k
        result.append(t)
    return result


def _validate_node(node: object, index: int, pair_id: str, player_color: str, upper_us: int, as_of: datetime, seen_fens: set[str], prev_fen: str | None, label: str):
    obj = _require_keys(node, _NODE_KEYS, f"{label}.nodes[{index}]")
    lbl = f"{label}.nodes[{index}]"
    fen = _check_str(obj["fen"], f"{lbl}.fen")
    _validate_fen(fen, f"{lbl}.fen")
    if active_color(fen) != player_color:
        raise ArtifactSemanticError(
            f"{lbl}.fen active-color {active_color(fen)!r} != pair player_color "
            f"{player_color!r} (every overlay node is a position the player moves at)"
        )
    if fen in seen_fens:
        raise ArtifactSemanticError(f"{label} has a duplicate node key (fen): {fen!r}")
    if prev_fen is not None and not (fen > prev_fen):
        raise ArtifactSemanticError(f"{label}.nodes is not strictly ascending by fen at {fen!r}")

    quality_count = _check_int(obj["quality_count"], f"{lbl}.quality_count", minimum=0)
    quality_sum = _check_float(obj["quality_sum"], f"{lbl}.quality_sum")
    if not (0.0 <= quality_sum <= quality_count):
        raise ArtifactSemanticError(
            f"{lbl}.quality_sum {quality_sum} out of [0, quality_count={quality_count}]"
        )
    live_attempts = _check_int(obj["live_attempts"], f"{lbl}.live_attempts", minimum=0)
    live_passes = _check_int(obj["live_passes"], f"{lbl}.live_passes", minimum=0)
    live_fails = _check_int(obj["live_fails"], f"{lbl}.live_fails", minimum=0)
    if live_attempts != live_passes + live_fails:
        raise ArtifactSemanticError(
            f"{lbl}: live_attempts {live_attempts} != live_passes + live_fails "
            f"({live_passes} + {live_fails})"
        )
    review_attempts = _check_int(obj["review_attempts"], f"{lbl}.review_attempts", minimum=0)
    review_passes = _check_int(obj["review_passes"], f"{lbl}.review_passes", minimum=0)
    review_fails = _check_int(obj["review_fails"], f"{lbl}.review_fails", minimum=0)
    if review_attempts != review_passes + review_fails:
        raise ArtifactSemanticError(
            f"{lbl}: review_attempts {review_attempts} != review_passes + review_fails "
            f"({review_passes} + {review_fails})"
        )
    is_ghost_target = _check_bool(obj["is_ghost_target"], f"{lbl}.is_ghost_target")
    tokens = _validate_session_tokens(obj["session_tokens"], pair_id, f"{lbl}.session_tokens")
    last_live_us = _validate_offset(obj["last_live_us_before"], upper_us, f"{lbl}.last_live_us_before")
    last_review_us = _validate_offset(obj["last_review_us_before"], upper_us, f"{lbl}.last_review_us_before")

    node_ev = NodeEvidence(
        fen=fen,
        live_attempts=live_attempts,
        live_passes=live_passes,
        live_fails=live_fails,
        quality_sum=quality_sum,
        quality_count=quality_count,
        session_ids=set(tokens),
        last_live_at=None if last_live_us is None else as_of - timedelta(microseconds=last_live_us),
        review_attempts=review_attempts,
        review_passes=review_passes,
        review_fails=review_fails,
        last_review_at=None if last_review_us is None else as_of - timedelta(microseconds=last_review_us),
        is_ghost_target=is_ghost_target,
    )
    return fen, node_ev, tokens


def _validate_offset(value: object, upper_us: int, label: str) -> int | None:
    if value is None:
        return None
    us = _check_int(value, label, minimum=0)
    if us > upper_us:
        raise ArtifactSemanticError(
            f"{label} offset {us} exceeds (as_of - TIMESTAMP_FLOOR)={upper_us}µs"
        )
    return us


def _validate_edge(edge: object, index: int, prev_key: tuple[str, str] | None, seen: set[tuple[str, str]], label: str):
    obj = _require_keys(edge, _EDGE_KEYS, f"{label}.edges[{index}]")
    lbl = f"{label}.edges[{index}]"
    parent = _check_str(obj["parent_fen"], f"{lbl}.parent_fen")
    child = _check_str(obj["child_fen"], f"{lbl}.child_fen")
    _validate_fen(parent, f"{lbl}.parent_fen")
    _validate_fen(child, f"{lbl}.child_fen")
    uci = _check_str(obj["uci"], f"{lbl}.uci")
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise ArtifactSemanticError(f"{lbl}.uci is not a parseable UCI move: {uci!r}") from exc
    board = chess.Board(parent)
    if move not in board.legal_moves:
        raise ArtifactSemanticError(f"{lbl}.uci {uci!r} is not legal from parent_fen")
    board.push(move)
    if normalize_fen(board.fen()) != child:
        raise ArtifactSemanticError(
            f"{lbl}.uci {uci!r} pushed onto parent_fen does not reach child_fen"
        )
    traversal_count = _check_int(obj["traversal_count"], f"{lbl}.traversal_count", minimum=0)
    live_attempts = _check_int(obj["live_attempts"], f"{lbl}.live_attempts", minimum=0)
    live_passes = _check_int(obj["live_passes"], f"{lbl}.live_passes", minimum=0)
    live_fails = _check_int(obj["live_fails"], f"{lbl}.live_fails", minimum=0)
    if live_attempts != live_passes + live_fails:
        raise ArtifactSemanticError(
            f"{lbl}: live_attempts {live_attempts} != live_passes + live_fails "
            f"({live_passes} + {live_fails})"
        )
    if live_attempts > traversal_count:
        raise ArtifactSemanticError(
            f"{lbl}: live_attempts {live_attempts} > traversal_count {traversal_count}"
        )
    quality_count = _check_int(obj["quality_count"], f"{lbl}.quality_count", minimum=0)
    quality_sum = _check_float(obj["quality_sum"], f"{lbl}.quality_sum")
    if quality_count > traversal_count:
        raise ArtifactSemanticError(
            f"{lbl}: quality_count {quality_count} > traversal_count {traversal_count}"
        )
    if not (0.0 <= quality_sum <= quality_count):
        raise ArtifactSemanticError(
            f"{lbl}.quality_sum {quality_sum} out of [0, quality_count={quality_count}]"
        )
    key = (parent, child)
    if key in seen:
        raise ArtifactSemanticError(f"{label} has a duplicate edge key (parent_fen, child_fen): {key}")
    if prev_key is not None and not (key > prev_key):
        raise ArtifactSemanticError(f"{label}.edges is not strictly ascending by (parent_fen, child_fen) at {key}")
    edge_ev = EdgeEvidence(
        parent_fen=parent,
        child_fen=child,
        uci=uci,
        traversal_count=traversal_count,
        live_attempts=live_attempts,
        live_passes=live_passes,
        live_fails=live_fails,
        quality_sum=quality_sum,
        quality_count=quality_count,
    )
    return key, edge_ev


def _validate_pair(pair: object, index: int, header: LoadedHeader) -> LoadedPair:
    label = f"pairs[{index}]"
    obj = _require_keys(pair, _PAIR_KEYS, label)
    pair_id = _check_str(obj["pair_id"], f"{label}.pair_id")
    if not _PAIR_ID_RE.match(pair_id):
        raise ArtifactSemanticError(f"{label}.pair_id must match ^pair-\\d+$; got {pair_id!r}")
    if pair_id != f"pair-{index:02d}":
        raise ArtifactSemanticError(
            f"{label}.pair_id {pair_id!r} is not array-order-aligned (expected "
            f"'pair-{index:02d}')"
        )

    player_color = _check_str(obj["player_color"], f"{label}.player_color")
    if player_color not in _VALID_COLORS:
        raise ArtifactSemanticError(f"{label}.player_color must be white/black; got {player_color!r}")
    subject_id = _check_str(obj["subject_id"], f"{label}.subject_id")
    if not _SUBJECT_ID_RE.match(subject_id):
        raise ArtifactSemanticError(f"{label}.subject_id must match ^subject-\\d+$; got {subject_id!r}")
    cohort_role = _check_str(obj["cohort_role"], f"{label}.cohort_role")
    if cohort_role not in _VALID_COHORT_ROLES:
        raise ArtifactSemanticError(f"{label}.cohort_role must be quantile/release_guard; got {cohort_role!r}")
    surrogate = _check_int(obj["surrogate_user_id"], f"{label}.surrogate_user_id", minimum=1)
    if surrogate != index + 1:
        raise ArtifactSemanticError(
            f"{label}.surrogate_user_id {surrogate} is not array-order-aligned (expected {index + 1})"
        )
    evidence_seq = _check_int(obj["evidence_seq"], f"{label}.evidence_seq", minimum=0)
    inputs_fingerprint = _check_nonempty_str(obj["inputs_fingerprint"], f"{label}.inputs_fingerprint")
    source_counts = _validate_source_counts(obj["source_counts"], f"{label}.source_counts")
    excluded_sessions = _check_int(obj["excluded_sessions"], f"{label}.excluded_sessions", minimum=0)
    phase_samples = _validate_phase_samples(obj["phase_samples"], f"{label}.phase_samples")

    upper_us = (header.as_of - TIMESTAMP_FLOOR) // timedelta(microseconds=1)
    nodes_arr = obj["nodes"]
    if not isinstance(nodes_arr, list):
        raise ArtifactSemanticError(f"{label}.nodes must be an array; got {type(nodes_arr).__name__}")
    node_items = []
    seen_fens: set[str] = set()
    prev_fen: str | None = None
    for j, node in enumerate(nodes_arr):
        fen, node_ev, tokens = _validate_node(node, j, pair_id, player_color, upper_us, header.as_of, seen_fens, prev_fen, label)
        seen_fens.add(fen)
        prev_fen = fen
        node_items.append((fen, node_ev, tokens))

    edges_arr = obj["edges"]
    if not isinstance(edges_arr, list):
        raise ArtifactSemanticError(f"{label}.edges must be an array; got {type(edges_arr).__name__}")
    edge_items = []
    seen_edges: set[tuple[str, str]] = set()
    prev_edge: tuple[str, str] | None = None
    for j, edge in enumerate(edges_arr):
        key, edge_ev = _validate_edge(edge, j, prev_edge, seen_edges, label)
        seen_edges.add(key)
        prev_edge = key
        edge_items.append((key, edge_ev))

    # Cross-field telemetry invariant: source_counts sum == node quality_count sum ==
    # edge quality_count sum (all three increment together at each quality observation),
    # so an inequality means the captured telemetry does NOT describe the frozen overlay.
    node_qc = sum(ev.quality_count for _, ev, _ in node_items)
    edge_qc = sum(ev.quality_count for _, ev in edge_items)
    sc_sum = sum(source_counts.values())
    if not (sc_sum == node_qc == edge_qc):
        raise ArtifactSemanticError(
            f"{label}: telemetry does not describe overlay — source_counts sum "
            f"{sc_sum}, node quality_count sum {node_qc}, edge quality_count sum {edge_qc} disagree"
        )

    observation_total = node_qc
    if cohort_role == "quantile" and observation_total < header.min_observations:
        raise ArtifactSemanticError(
            f"{label}: quantile pair observation_total {observation_total} is below "
            f"header.min_observations {header.min_observations}"
        )

    # Per-pair session-token union must be EXACTLY the contiguous zero-based set of
    # STRING tokens {pair_id-g0 … pair_id-g(N-1)} (every assigned token touches >= 1
    # node). Compared as exact strings, not numeric suffixes: reconstruction keeps the
    # raw token strings as session_ids and _aggregate_metadata unions those strings, so
    # the guard must count exactly what game_count counts. (`_validate_session_tokens`
    # already pins each token to its canonical spelling, so no two strings share a k.)
    token_union = {t for _, _, tokens in node_items for t in tokens}
    expected_union = {f"{pair_id}-g{k}" for k in range(len(token_union))}
    if token_union != expected_union:
        raise ArtifactSemanticError(
            f"{label}: session-token union is not the contiguous zero-based set "
            f"{{{pair_id}-g0 … {pair_id}-g{len(token_union) - 1}}}"
        )

    overlay = EvidenceOverlay(user_id=surrogate, player_color=player_color)
    overlay.nodes = {fen: ev for fen, ev, _ in node_items}
    overlay.edges = {key: ev for key, ev in edge_items}
    overlay.source_counts = Counter(source_counts)
    overlay.excluded_sessions = excluded_sessions
    overlay.phase_samples = phase_samples

    return LoadedPair(
        pair_id=pair_id,
        subject_id=subject_id,
        cohort_role=cohort_role,
        surrogate_user_id=surrogate,
        player_color=player_color,
        evidence_seq=evidence_seq,
        inputs_fingerprint=inputs_fingerprint,
        overlay=overlay,
    )


def _validate_subject_structure(pairs: Sequence[LoadedPair]) -> None:
    """Subjects are the contiguous set subject-00 … subject-(M-1) assigned by first
    appearance; each subject's pairs are CONTIGUOUS in the array; no (subject_id,
    player_color) combination appears twice (<= one white + one black per subject)."""
    order: list[str] = []
    seen: set[str] = set()
    for lp in pairs:
        if lp.subject_id not in seen:
            seen.add(lp.subject_id)
            order.append(lp.subject_id)
    for j, sid in enumerate(order):
        if sid != f"subject-{j:02d}":
            raise ArtifactSemanticError(
                f"subject_ids are not the contiguous first-appearance set subject-00 … "
                f"(expected 'subject-{j:02d}' at first-appearance position {j}, got {sid!r})"
            )
    prev: str | None = None
    finished: set[str] = set()
    for lp in pairs:
        if lp.subject_id != prev:
            if lp.subject_id in finished:
                raise ArtifactSemanticError(
                    f"subject block for {lp.subject_id!r} is non-contiguous (interleaved) "
                    "in the pairs array"
                )
            if prev is not None:
                finished.add(prev)
            prev = lp.subject_id
    seen_sc: set[tuple[str, str]] = set()
    for lp in pairs:
        sc = (lp.subject_id, lp.player_color)
        if sc in seen_sc:
            raise ArtifactSemanticError(
                f"duplicate (subject_id, player_color) {sc} — a subject holds at most "
                "one white and one black record"
            )
        seen_sc.add(sc)


def _validate_and_reconstruct(
    payload: object, *, supported_schema_version: int = ARTIFACT_SCHEMA_VERSION
) -> tuple[LoadedHeader, tuple[LoadedPair, ...]]:
    obj = _require_keys(payload, _TOP_KEYS, "artifact")
    header = _validate_header(obj["header"], supported_schema_version=supported_schema_version)
    pairs_arr = obj["pairs"]
    if not isinstance(pairs_arr, list) or not pairs_arr:
        raise ArtifactSemanticError("artifact.pairs must be a non-empty array")
    loaded = tuple(_validate_pair(pair, i, header) for i, pair in enumerate(pairs_arr))
    if header.pair_count != len(loaded):
        raise ArtifactSemanticError(
            f"header.pair_count {header.pair_count} != len(pairs) {len(loaded)}"
        )
    _validate_subject_structure(loaded)
    return header, loaded


def validate_artifact_bytes(
    artifact_bytes: bytes, *, supported_schema_version: int = ARTIFACT_SCHEMA_VERSION
) -> tuple[LoadedHeader, tuple[LoadedPair, ...]]:
    """Full provenance-INDEPENDENT semantic validation of raw artifact bytes: hardened
    decode → closed-schema + type/range/cross-field/canonical-structure checks →
    canonical-byte re-encode invariant. The reusable pure validator the capture
    self-check re-runs AT SOURCE; ``load_frozen_artifact`` calls it inside Phase A."""
    if not isinstance(artifact_bytes, bytes):
        raise TypeError(
            f"artifact_bytes must be bytes (SHA-256 + canonical re-encode anchor on the "
            f"exact original bytes); got {type(artifact_bytes).__name__}"
        )
    payload = _hardened_loads(artifact_bytes, ArtifactSemanticError)
    header, pairs = _validate_and_reconstruct(payload, supported_schema_version=supported_schema_version)
    _canonical_reencode_check(payload, artifact_bytes)
    return header, pairs


def _canonicalize_floats(obj):
    """Re-normalize every float in a decoded payload the way the freeze would (-0.0 →
    0.0, non-finite refused). The ONLY float fields are node/edge quality_sum, so this
    is what makes the re-encode check reject a -0.0 that survived semantic validation
    (json.dumps alone re-emits -0.0 verbatim)."""
    if isinstance(obj, dict):
        return {k: _canonicalize_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonicalize_floats(v) for v in obj]
    if isinstance(obj, float):
        return _canonical_float(obj)
    return obj


def _canonical_reencode_check(payload: object, artifact_bytes: bytes) -> None:
    """Make byte-stability a LOAD invariant: the validated payload re-encoded with the
    pinned canonical flags must equal artifact_bytes. Owns ONLY the byte-level
    noncanonicality that survives a full semantic pass (insignificant whitespace,
    unsorted object KEYS, exponent-form floats, a -0.0) — every structural/value
    malformation is owned by semantic validation upstream and rejects THERE."""
    if _canonical_dumps(_canonicalize_floats(payload)) != artifact_bytes:
        raise ArtifactCanonicalBytesError(
            "non-canonical bytes: the validated payload does not re-encode byte-for-byte "
            "to the raw artifact bytes (whitespace, unsorted keys, exponent float, or -0.0)"
        )


# ---- Provenance record (committed to Git — a closed-schema privacy boundary) ----


def _validate_provenance_record(provenance_bytes: bytes) -> _ValidatedProvenance:
    record = _hardened_loads(provenance_bytes, ProvenanceRecordError)
    schema_version = _dispatch_schema_version(
        record, "provenance record", "provenance.schema_version", ARTIFACT_SCHEMA_VERSION,
        err=ProvenanceRecordError,
    )
    obj = _require_keys(record, _PROVENANCE_KEYS, "provenance record", err=ProvenanceRecordError)
    sha256 = _check_str(obj["sha256"], "provenance.sha256", err=ProvenanceRecordError)
    if not _SHA256_RE.match(sha256):
        raise ProvenanceRecordError(
            f"provenance.sha256 must be 64-char lowercase hex; got {sha256!r}"
        )
    captured_model_version = _check_str(obj["captured_model_version"], "provenance.captured_model_version", err=ProvenanceRecordError)
    if not _CAPTURED_MODEL_VERSION_RE.match(captured_model_version):
        raise ProvenanceRecordError(
            f"provenance.captured_model_version must match ^sm-v\\d+-\\d+$; got "
            f"{captured_model_version!r}"
        )
    cohort_rules = _check_str(obj["cohort_rules"], "provenance.cohort_rules", err=ProvenanceRecordError)
    if not _COHORT_RULES_RE.match(cohort_rules):
        raise ProvenanceRecordError(
            f"provenance.cohort_rules must match ^opening-cohort-rules-v\\d+$; got "
            f"{cohort_rules!r}"
        )
    return _ValidatedProvenance(
        sha256=sha256,
        schema_version=schema_version,
        as_of=_validate_canonical_as_of(obj["as_of"], "provenance.as_of", err=ProvenanceRecordError),
        captured_model_version=captured_model_version,
        graph_fingerprint=_check_nonempty_str(obj["graph_fingerprint"], "provenance.graph_fingerprint", err=ProvenanceRecordError),
        roots_fingerprint=_check_nonempty_str(obj["roots_fingerprint"], "provenance.roots_fingerprint", err=ProvenanceRecordError),
        evidence_derivation_fingerprint=_check_nonempty_str(
            obj["evidence_derivation_fingerprint"], "provenance.evidence_derivation_fingerprint", err=ProvenanceRecordError
        ),
        pair_count=_check_int(obj["pair_count"], "provenance.pair_count", minimum=0, err=ProvenanceRecordError),
        min_observations=_check_int(obj["min_observations"], "provenance.min_observations", minimum=0, err=ProvenanceRecordError),
        cohort_rules=cohort_rules,
        release_guard_opening_key=_check_nonempty_str(
            obj["release_guard_opening_key"], "provenance.release_guard_opening_key", err=ProvenanceRecordError
        ),
        release_guard_child_opening_key=_check_nonempty_str(
            obj["release_guard_child_opening_key"], "provenance.release_guard_child_opening_key", err=ProvenanceRecordError
        ),
        **_validate_capture_attestation(obj, "provenance", err=ProvenanceRecordError),
    )


# ---- Split load guard (one authoritative classification, strict phase order) ----


def _check_integrity(header: LoadedHeader, artifact_sha256: str, record: _ValidatedProvenance) -> None:
    if artifact_sha256 != record.sha256:
        raise ArtifactIntegrityError(
            f"integrity: recomputed artifact digest {artifact_sha256} != "
            f"record.sha256 {record.sha256}"
        )
    mismatches = [
        name
        for name in (
            "schema_version", "as_of", "captured_model_version", "graph_fingerprint",
            "roots_fingerprint", "evidence_derivation_fingerprint", "pair_count",
            "min_observations", "cohort_rules", "release_guard_opening_key",
            "release_guard_child_opening_key",
            "capture_scorer_source_digest", "capture_source_revision",
            "capture_python_version", "capture_chess_version",
        )
        if getattr(header, name) != getattr(record, name)
    ]
    if mismatches:
        raise ArtifactIntegrityError(
            f"integrity: header fields disagree with the provenance record: {mismatches}"
        )


def _check_scoring_validity(header: LoadedHeader, runtime_binding: RuntimeBinding) -> None:
    """Phase C: header vs the CURRENT runtime — derivation compatibility and release-policy
    pins only.

    The four capture-attestation fields (capture_scorer_source_digest /
    capture_source_revision / capture_python_version / capture_chess_version) are
    DELIBERATELY not compared here and not on RuntimeBinding (g-p4ih-producer-bind).
    They are AUDITABILITY, not compatibility: an equality gate on the producer's digest,
    revision, or runtime would silently reimpose "re-capture on every dependency bump" —
    discarding the property that evidence_derivation_fingerprint deliberately excludes
    model-side versions so a frozen artifact survives model bumps — and would create
    standing pressure to re-run authorized PRODUCTION captures of private data for
    routine reasons. The producer-side hazard is closed at capture time instead
    (clean-tree refusal over the closed manifest, g-p4ih-capture); an artifact whose
    attestation differs from the current environment loads GREEN."""
    drift = [
        name
        for name in (
            "graph_fingerprint", "roots_fingerprint", "evidence_derivation_fingerprint",
            "release_guard_opening_key", "release_guard_child_opening_key",
        )
        if getattr(header, name) != getattr(runtime_binding, name)
    ]
    if drift:
        raise ArtifactScoringValidityError(
            f"scoring-validity: header drifted from the current runtime: {drift} "
            "(remedy is an authorized re-capture, never a silent re-score)"
        )
    policy = []
    if header.cohort_rules != runtime_binding.cohort_rules:
        policy.append(f"cohort_rules {header.cohort_rules!r} != {runtime_binding.cohort_rules!r}")
    if header.min_observations != runtime_binding.min_observations:
        policy.append(f"min_observations {header.min_observations} != {runtime_binding.min_observations}")
    if policy:
        raise ArtifactScoringValidityError(
            f"scoring-validity: release-policy pin failed: {policy}"
        )


def assert_artifact_shape(pairs: Sequence[LoadedPair]) -> None:
    """The release-guard artifact shape (load guard + capture self-check): EXACTLY two
    release_guard records, colors EXACTLY {white, black}, one shared subject_id, distinct
    pair_ids — AND at least two quantile pairs (structural hygiene for derive_cutoffs)."""
    guards = [p for p in pairs if p.cohort_role == "release_guard"]
    quantiles = [p for p in pairs if p.cohort_role == "quantile"]
    if len(guards) != 2:
        raise ReleaseGuardShapeError(
            f"artifact-shape: expected exactly two release_guard records, got {len(guards)}"
        )
    colors = sorted(g.player_color for g in guards)
    if colors != ["black", "white"]:
        raise ReleaseGuardShapeError(
            f"artifact-shape: release_guard colors must be exactly {{white, black}}, got {colors}"
        )
    subjects = {g.subject_id for g in guards}
    if len(subjects) != 1:
        raise ReleaseGuardShapeError(
            f"artifact-shape: the two release_guard records must share one subject_id, "
            f"got {sorted(subjects)}"
        )
    if guards[0].pair_id == guards[1].pair_id:
        raise ReleaseGuardShapeError(
            "artifact-shape: the two release_guard records must have distinct pair_ids"
        )
    if len(quantiles) < 2:
        raise ReleaseGuardShapeError(
            f"too-few-quantile-pairs: need >= 2 quantile pairs for derive_cutoffs, got "
            f"{len(quantiles)}"
        )


def load_frozen_artifact(
    artifact_bytes: bytes, provenance_bytes: bytes, runtime_binding: RuntimeBinding
) -> LoadedCohort:
    """The split load guard. Decodes BOTH byte strings, runs semantic validation +
    the canonical-byte re-encode invariant (Phase A), integrity vs the injected
    provenance record (Phase B), scoring-validity vs the runtime binding (Phase C), then
    the artifact-shape guard. Reads NO committed file — the caller supplies both byte
    strings and the runtime binding. Any failure fails CLOSED; never live-selects."""
    if not isinstance(artifact_bytes, bytes):
        raise TypeError(
            f"artifact_bytes must be bytes, not {type(artifact_bytes).__name__} — SHA-256 "
            "integrity and the canonical-byte re-encode both anchor on the exact original bytes"
        )
    if not isinstance(provenance_bytes, bytes):
        raise TypeError(
            f"provenance_bytes must be bytes, not {type(provenance_bytes).__name__} — a "
            "pre-decoded record has already lost duplicate object keys"
        )

    # Phase A — DECODE/SCHEMA (intrinsic, provenance-independent).
    record = _validate_provenance_record(provenance_bytes)
    payload = _hardened_loads(artifact_bytes, ArtifactSemanticError)
    header, pairs = _validate_and_reconstruct(
        payload, supported_schema_version=runtime_binding.schema_version
    )
    _canonical_reencode_check(payload, artifact_bytes)

    # Phase B — INTEGRITY class (1) (vs the decoded provenance record).
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    _check_integrity(header, artifact_sha256, record)

    # Phase C — SCORING VALIDITY class (2) (header vs the current runtime).
    _check_scoring_validity(header, runtime_binding)

    assert_artifact_shape(pairs)
    return LoadedCohort(header=header, artifact_sha256=artifact_sha256, pairs=pairs)


# ---- Release-guard score-shape guards (Protocol-typed; no downstream import) ----


class CellScored(Protocol):
    """The only members the score guards read off ONE cell's result: satisfied by the
    mutable PairScore (capture side) and by the immutable CellScore (release side)."""

    named_scores: Sequence[float]
    named_score_map: Mapping[str, float]


class ReleaseGuardScored(Protocol):
    """Artifact-owned STRUCTURAL view (typing.Protocol) exposing EXACTLY the two members
    the score guards read. A downstream ScoredPair (carrying .player_color + .grid)
    satisfies it with no import and no inheritance, so this bead never depends on the
    type it blocks."""

    player_color: str
    grid: Mapping["GridCell", CellScored]


def _require_finite_named_score(named_score_map: Mapping[str, float], key: str, color: str, cell: "GridCell") -> None:
    if key not in named_score_map:
        raise ReleaseGuardShapeError(
            f"score-shape: {color} release-guard pair missing named_score_map[{key!r}] "
            f"for cell {cell.label!r}"
        )
    value = named_score_map[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ReleaseGuardShapeError(
            f"score-shape: {color} release-guard named_score_map[{key!r}] is not finite "
            f"for cell {cell.label!r}: {value!r}"
        )
    if not (0.0 <= float(value) <= 100.0):
        raise ReleaseGuardShapeError(
            f"score-shape: {color} release-guard named_score_map[{key!r}] out of [0, 100] "
            f"for cell {cell.label!r}: {value}"
        )


def assert_release_guard_score_shape(
    guard_pairs: Sequence[ReleaseGuardScored],
    required_cells: Sequence["GridCell"],
    runtime_binding: RuntimeBinding,
) -> None:
    """For EVERY required cell, each release-guard scored pair's named_score_map contains
    RELEASE_GUARD_OPENING_KEY with a finite float in [0, 100]; the BLACK pair's map
    additionally contains RELEASE_GUARD_CHILD_OPENING_KEY (the Caro is Black's defense).
    Raises before any gate runs. Consumed by select_candidate binding check 3 and the
    capture self-check (both pass their scored pairs directly)."""
    opening_key = runtime_binding.release_guard_opening_key
    child_key = runtime_binding.release_guard_child_opening_key
    for gp in guard_pairs:
        for cell in required_cells:
            named_score_map = gp.grid[cell].named_score_map
            _require_finite_named_score(named_score_map, opening_key, gp.player_color, cell)
            if gp.player_color == "black":
                _require_finite_named_score(named_score_map, child_key, gp.player_color, cell)


def assert_min_quantile_scores_per_cell(
    quantile_pairs: Sequence[ReleaseGuardScored], required_cells: Sequence["GridCell"]
) -> None:
    """The SUFFICIENT half of the derive_cutoffs precondition (post-scoring): for EVERY
    required cell the pooled quantile distribution — the CONCATENATION of each quantile
    pair's named_scores — has len >= 2, raising a distinct per-cell diagnostic BEFORE
    derive_cutoffs (which raises ValueError on < 2). The load-time pair count is
    necessary but NOT sufficient: a quantile pair the observation threshold admits can
    still pool ZERO named scores for a cell (off-book-only evidence)."""
    for cell in required_cells:
        pooled = [s for qp in quantile_pairs for s in qp.grid[cell].named_scores]
        if len(pooled) < 2:
            raise ReleaseGuardShapeError(
                f"too-few-pooled-quantile-scores for cell {cell.label!r}: pooled "
                f"quantile named_scores number {len(pooled)} (< 2 required by derive_cutoffs)"
            )


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


# The frozen User-14 scenario tuple: (graph, black_overlay, white_overlay, roots,
# root_fen, child_fen). Built ONCE per diagnostic run and passed to every cell.
User14Scenario = tuple[
    OpeningGraph, EvidenceOverlay, EvidenceOverlay, OpeningRoots, str, str
]


def _user14_cell_operands(
    scenario: User14Scenario, cell: GridCell, *, as_of: datetime
) -> dict[str, object]:
    """The nine User-14 synthetic operands for ONE grid cell, at now=as_of, debug=True.

    Extracted so the User-14 diagnostic rows and the release-path DiagnosticCellResult
    (g-p4ih-replay-bind) compute the SAME operands from ONE code path — a divergence
    between the reported diagnostic and the binding-time operand set is impossible.
    """
    graph, black_overlay, white_overlay, roots, root_fen, child_fen = scenario
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
    scenario = _user14_scenario()

    def operands(cell: GridCell) -> dict[str, object]:
        return _user14_cell_operands(scenario, cell, as_of=as_of)

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
# Replay inputs + approval/source binding (g-p4ih-replay-bind)
#
# The ONE impure step (build_selection_inputs) turns a validated frozen-cohort artifact
# into typed, provenance-bound scoring inputs a pure selector (g-p4ih-selection) can
# trust. It closes two contract gaps: the gates cannot be evaluated from cohort scores
# alone (they also need the synthetic diagnostics), and free-floating scores are not
# provably bound to the artifact, its clock, the scoring code, or the runtime.
# ---------------------------------------------------------------------------

# _REPO_ROOT and SCORER_SOURCE_FILES are defined at the TOP of this module, above the
# scorer imports — see the source-binding block there for why they cannot live here.

# The committed provenance record the release-side load guard validates against. It is
# NOT checked in by this bead (an authorized --capture-cohort run publishes it, like the
# User-14 fixture); build_selection_inputs fails CLOSED if it is absent.
COHORT_PROVENANCE_PATH = Path(__file__).resolve().parent / "fixtures" / "cohort_provenance.json"

class ScorerSourceManifestError(Exception):
    """A scoring import of opening_rootcalc.py is not covered by SCORER_SOURCE_FILES —
    a future import would otherwise silently escape the source binding."""


class ScorerSourceUnstableError(Exception):
    """The scorer bytes on disk moved between this interpreter importing them and the end
    of the run, so no digest can honestly describe the code that produced the scores."""


class StaleBytecodeError(ScorerSourceUnstableError):
    """A scorer module could have been imported from a bytecode cache CPython never proved
    against the source the digest hashes — so the digest would certify code that did not
    run. Same family as ScorerSourceUnstableError: the running code is not the named code."""


class ScorerImportOriginError(ScorerSourceUnstableError):
    """A scorer module was imported from somewhere other than the tree the digest hashes —
    so the digest describes files that module object never came from. Same family as
    ScorerSourceUnstableError: the running code is not the named code."""


def check_scorer_import_origins(modules: Mapping[str, object] | None = None) -> None:
    """Fail closed unless every bound manifest app.* module came from _REPO_ROOT.

    THE GAP THIS CLOSES. An import binds a module OBJECT, not a file: if anything put app.*
    into sys.modules before this module ran — an interpreter wrapper, an outer harness, a
    stray sys.path entry — the `from app...` imports above silently reuse those objects. The
    scorer then executes code from wherever they came from while every source read here
    describes the tree we were pointed at, and the flag gets minted for code that never ran.

    WHAT IS AND IS NOT CHECKED. The rule is ORIGIN, not order. A preload from the hashed tree
    is harmless — same bytes, and the launcher's digest predates the whole process, including
    the preload's own compile — so preloading is not itself an error, and the test suite
    legitimately imports app.* before the scorer. What must never happen is a manifest module
    resolving to a file outside the tree the digest binds, however it got there: a preload
    from another checkout, a shadowed sys.path entry, an egg-link.

    WHAT THIS CANNOT SEE, stated plainly because the flag this feeds is a release gate:

    * REBINDING. `__file__` describes where a module was LOADED FROM, not what its attributes
      hold now. A startup hook that imports app.fen from the correct tree and then rebinds
      app.fen.normalize_fen passes this check untouched, with the digest green — the source
      bytes on disk were never edited. This is demonstrated, not hypothetical.
    * A LYING LOADER. `__file__` is set by whatever imported the module. A meta_path finder
      can serve arbitrary code under the origin we expect, and this check reads that origin.

    Both need the startup hook to run at all, which is what the launcher's `-S` denies (see
    child_command); neither is defended by anything in THIS process, because in-process code
    cannot audit the runtime it is already running inside. So read this as a check against
    MISCONFIGURATION — the wrong checkout, a shadowed path — and not as a defence against a
    hostile runtime. That boundary is g-release-os-boundary's, not this function's.

    Only meaningful on the verified path, and only called there: an unverified dev run claims
    nothing about where its code came from.

    The third leg, alongside the pre-exec digest (what the bytes WERE) and the bytecode check
    (that source, not a stale .pyc, was compiled): the modules we bound declare an origin
    inside the tree we hashed.
    """
    modules = sys.modules if modules is None else modules
    for rel in SCORER_SOURCE_FILES:
        if not rel.startswith("backend/app/") or not rel.endswith(".py"):
            continue  # requirements.txt, and the scorer itself (run as __main__, not app.*)
        name = rel[len("backend/"):-len(".py")].replace("/", ".")
        module = modules.get(name)
        if module is None:
            continue  # not every manifest module is imported on every path
        origin = getattr(module, "__file__", None)
        expected = (_REPO_ROOT / rel).resolve()
        if origin is None or Path(origin).resolve() != expected:
            raise ScorerImportOriginError(
                f"module {name!r} was imported from {origin!r}, not from the tree the digest "
                f"binds ({expected}): the scorer is running code the digest never hashed"
            )


class UnverifiedScorerSourceError(Exception):
    """A release path was handed scores whose source digest is not proven to name the code
    that RAN. Deliberately NOT a ScorerSourceUnstableError: nothing is known to be wrong
    here — the run simply never established the guarantee, which is the normal, correct
    outcome for every dev and test run."""


def require_preexec_verified_source(bound: object) -> None:
    """THE RELEASE GATE (g-p4ih-srcfence). Refuse anything carrying
    ``scorer_source_verified_preexec=False``.

    Callers: ``--select-release`` (g-p4ih-release-cli) and the Phase-3 preflight
    (g-p4ih-preflight), before applying an approved winner. Accepts anything stamped with
    the flag — ScoredCalibrationCohort or WinnerBinding — and reads it by attribute, so an
    unstamped object raises rather than sliding through as falsy.

    WHY THE GATE LIVES HERE AND NOT IN build_selection_inputs. A dev run legitimately has no
    launcher, so build_selection_inputs cannot refuse False without making the script
    unusable outside a release. That is exactly why the flag is CARRIED on the cohort rather
    than assumed: the run records what it can honestly claim, and the release boundary —
    where the digest actually gets spent — demands the stronger guarantee.

    False means the digest is fenced over source bytes but not proven to describe the
    compiled code: no pre-exec hash, or bytecode CPython could have served stale. Applying a
    winner on that basis would revalidate a tree against a digest that may name code which
    never scored. Re-run under backend/scripts/release_calibration.sh.

    WHAT True DOES AND DOES NOT ATTEST — read this before treating the flag as an assurance
    of anything broader, and do not let its scope creep. True means exactly:

        the bytes named by SCORER_SOURCE_FILES were hashed before this interpreter existed,
        they still hash the same from inside the run, and the modules bound from them declare
        an origin inside that tree.

    That is MANIFEST-SOURCE PROVENANCE. It is NOT an attestation of the complete code that
    produced the result, and it never has been. Outside its scope, all unhashed: the
    interpreter, the standard library, and every installed dependency — a `pip install` into
    a shared venv changes what runs without touching the tree or the digest. Also outside it:
    anything that rebinds an attribute after import (the launcher's -I -S denies the routine
    way in, it does not make the runtime honest), and a same-uid write to the checkout, which
    only an OS boundary can prevent.

    Those residuals are g-release-os-boundary's. If that bead ever makes the boundary
    mandatory for release gating, this docstring is where the widened claim gets earned —
    by pointing at the mechanism that earns it, not by quietly dropping this paragraph.
    """
    verified = bound.scorer_source_verified_preexec  # AttributeError = fail closed
    if verified is not True:
        raise UnverifiedScorerSourceError(
            f"{type(bound).__name__}.scorer_source_verified_preexec is {verified!r}: these "
            "scores were produced without a digest computed before the interpreter started "
            "(or without verified bytecode), so scorer_source_digest is not proven to name "
            "the code that ran and must not be spent on a release. Re-run under "
            "backend/scripts/release_calibration_launcher.py, which hashes the manifest "
            "pre-exec and runs from an exclusive worktree"
        )


# Cache verdicts under which CPython was FORCED to compile the source we hashed.
_SAFE_BYTECODE_VERDICTS = frozenset({"absent", "unusable", "checked-hash"})


def check_scorer_bytecode(state: Mapping[str, str] | None = None) -> None:
    """Fail closed unless every scorer module was compiled from the verified SOURCE.

    Only meaningful on the verified path (an inherited launcher digest), and only called
    there — an unverified dev/test run claims nothing about its bytecode and is left alone.

    Two conditions, both necessary:

    1. Bytecode writing must be OFF. If CPython may write .pyc files, it writes THIS
       module's before executing its body, so the cache state observed at import can no
       longer distinguish "just compiled from the source I hashed" from "loaded from a
       stale .pyc" — both look like a timestamp .pyc whose (mtime, size) match.
    2. Every manifest module's pre-import cache verdict must be one CPython could not have
       served stale bytecode from (see _bytecode_cache_state).

    Both are satisfied by release_calibration_launcher.py, which sets
    PYTHONDONTWRITEBYTECODE=1 and points PYTHONPYCACHEPREFIX at a fresh empty directory
    inside the throwaway worktree's temp parent.
    """
    if not sys.dont_write_bytecode:
        raise StaleBytecodeError(
            "a verified run must disable bytecode writing (PYTHONDONTWRITEBYTECODE=1 / -B): "
            "with writing enabled CPython caches this module before its body runs, so a "
            "stale .pyc and a freshly compiled one are indistinguishable afterwards"
        )
    state = _BYTECODE_CACHE_AT_IMPORT if state is None else state
    unsafe = {rel: v for rel, v in state.items() if v not in _SAFE_BYTECODE_VERDICTS}
    if unsafe:
        detail = ", ".join(f"{rel} ({verdict})" for rel, verdict in sorted(unsafe.items()))
        raise StaleBytecodeError(
            f"scorer modules were importable from unverified bytecode: {detail}. CPython "
            "accepts a timestamp .pyc whenever the source's (mtime, size) match, so a "
            "same-size edit with a preserved mtime runs the OLD bytecode while the source "
            "digest hashes the NEW file — the digest would certify code that never ran. "
            "Run the release with an empty/fresh bytecode cache (purge __pycache__ or set "
            "PYTHONPYCACHEPREFIX to a fresh directory)"
        )


def scorer_source_digest() -> str:
    """SHA-256 over, for each path in SCORER_SOURCE_FILES order, ``path`` + NUL +
    on-disk bytes + NUL. Deterministic, independent of git state, and HONEST under dirt:
    a locally-edited scorer binds to the EDITED bytes, so Phase 3 (running from the
    committed tree) fails the digest match unless exactly those bytes were committed."""
    h = hashlib.sha256()
    for rel in SCORER_SOURCE_FILES:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update((_REPO_ROOT / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


# The manifest digest read at import: after this module's top-level `from app...` imports,
# and before any scoring can run.
#
# It is NOT proof of what this interpreter compiled, and must not be described as such.
# Python compiles this module — and everything imported ahead of it — BEFORE this line
# executes, and top-level dependency init is not instantaneous. An edit landing in that
# window leaves old compiled code running while this reads the new bytes: matching open and
# close reads, a stamped digest, and code that never ran. Only a hash taken before the
# process existed can rule that out, which is what SCORER_SOURCE_DIGEST_ENV is for.
#
# What the snapshot DOES buy, paired with the re-read after the last score, is a fence: the
# scorer bytes did not move from here through the end of the run, so an edit landing
# mid-run cannot be stamped. Neither read detects a change-and-revert; nothing short of an
# exclusive checkout does — release_calibration_launcher.py runs from a throwaway worktree
# at a private path for exactly that reason.
_SCORER_SOURCE_DIGEST_AT_IMPORT = scorer_source_digest()

# The digest a LAUNCHER computed BEFORE exec'ing this interpreter, handed in through the
# environment. That hash necessarily precedes the compilation of every manifest file, so a
# match closes the compile window an in-process snapshot cannot reach: it is the only way
# this process can know the bytes it read at import are the bytes it ran.
# release_calibration_launcher.py sets it. It is ABSENT on dev and test runs, and that
# absence is recorded on the cohort as scorer_source_verified_preexec=False rather than
# quietly assumed away — require_preexec_verified_source() then demands the stronger
# guarantee at the release boundary without this module pretending to provide it.
#
# The value itself is _LAUNCHER_SCORER_DIGEST, captured at the TOP of this module before
# any scorer import. There is deliberately NO accessor that re-reads os.environ: a live
# read would let this process promote its own flag after the scorer was compiled.


def _app_imports_of(path: Path) -> set[str]:
    """The ``app.*`` modules ``path`` imports, parsed statically — never executed."""
    src = path.read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == "app":
                modules.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "app":
                    modules.add(alias.name)
    return modules


def _app_module_rel(module: str) -> str:
    return "backend/" + module.replace(".", "/") + ".py"


def scorer_imported_app_modules(manifest: tuple[str, ...] = SCORER_SOURCE_FILES) -> set[str]:
    """The ``app.*`` import CLOSURE of every ``.py`` in ``manifest`` (parsed statically).

    A fixpoint walk (g-p4ih-producer-bind): start from each manifest ``.py`` — the
    scoring entry point (opening_rootcalc.py) AND the derivation entry point
    (opening_evidence.py) alike — follow each module's static app.* Import/ImportFrom
    edges, and iterate until no new module appears. The walk follows a discovered module
    even when it is ABSENT from ``manifest``, so a missing module's own transitive
    imports are still reported rather than hidden behind the first gap."""
    modules: set[str] = set()
    pending = [_REPO_ROOT / rel for rel in manifest if rel.endswith(".py")]
    while pending:
        path = pending.pop()
        try:
            imported = _app_imports_of(path)
        except OSError:
            continue  # a missing file is the manifest-membership check's diagnostic, not ours
        for module in imported:
            if module not in modules:
                modules.add(module)
                pending.append(_REPO_ROOT / _app_module_rel(module))
    return modules


def check_scorer_source_manifest(manifest: tuple[str, ...] = SCORER_SOURCE_FILES) -> None:
    """Fail closed unless ``manifest`` is CLOSED under app.* imports: every app.* module
    in the import closure of the manifest's ``.py`` files must itself be in ``manifest``,
    so neither a future scoring import nor a derivation import (the code that
    MANUFACTURES a frozen overlay — the surface capture_scorer_source_digest attests)
    can silently escape scorer_source_digest. Parametrized on ``manifest`` so a test can
    prove a reduced manifest is rejected."""
    covered = set(manifest)
    for module in sorted(scorer_imported_app_modules(manifest)):
        rel = _app_module_rel(module)
        if rel not in covered:
            raise ScorerSourceManifestError(
                f"import-closure member {module!r} ({rel}) is not in SCORER_SOURCE_FILES — "
                "every app.* module the manifest's files reach must be added to the source "
                "binding manifest"
            )


def _git_head_revision() -> str | None:
    """``git rev-parse HEAD`` (AUDIT only, never a gate). None if git is unavailable —
    the release path never blocks on git, so a missing revision is recorded, not fatal."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:
        return None


def _scorer_dirty_paths() -> tuple[str, ...]:
    """The SCORER_SOURCE_FILES that differ from HEAD (sorted). AUDIT companion to
    scorer_source_digest, never a gate: the digest binds the actual bytes, so a dirty
    worktree (the uncommitted provenance diff + unrelated multi-agent files) is expected
    and NEVER blocks selection. Empty on a committed-scorer run; ``()`` if git fails."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "diff", "--name-only", "HEAD", "--",
             *SCORER_SOURCE_FILES],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return ()
        changed = {line.strip() for line in out.stdout.splitlines() if line.strip()}
        return tuple(sorted(p for p in SCORER_SOURCE_FILES if p in changed))
    except Exception:
        return ()


# ---- Typed wrappers (all frozen dataclasses) ----
#
# DEEPLY immutable, not merely frozen. A frozen wrapper around a mutable PairScore is
# security theatre: `frozen=True` only blocks rebinding the attribute, so
# `cohort.pairs[0].grid[cell].named_score_map[key] = 99.0` would still rewrite a
# release-gate score after every binding was stamped — changing cutoffs, gates or the
# winner while the artifact hash, source digest and runtime stamps all still verify. So
# every decision-bearing value below is a scalar, a tuple, or a mappingproxy over a dict
# this module owns and never hands out.


def _freeze_map(mapping: Mapping) -> Mapping:
    """A read-only view over a PRIVATE copy: copying defends against the caller mutating
    the dict it passed in, the proxy against mutation through our own reference."""
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class CellScore:
    """The decision-bearing scoring result for one (pair, cell), snapshotted OUT of the
    mutable PairScore. Everything the selector reads — cutoff pools, gate operands, the
    winner comparison — comes from here, so it holds no list, dict, Counter or other
    mutable container.

    ``named_confidence_map`` is carried alongside the scores because confidence folds
    freshness, which reads the clock: it is the surface that exposes a scoring-clock leak
    even when opening_score is unmoved."""

    named_scores: tuple[float, ...]
    named_score_map: Mapping[str, float]
    named_confidence_map: Mapping[str, float]
    synthetic_score: float | None
    synthetic_confidence: float | None
    observation_total: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "named_scores", tuple(self.named_scores))
        object.__setattr__(self, "named_score_map", _freeze_map(self.named_score_map))
        object.__setattr__(
            self, "named_confidence_map", _freeze_map(self.named_confidence_map)
        )

    @classmethod
    def from_pair_score(cls, ps: PairScore) -> CellScore:
        return cls(
            named_scores=tuple(ps.named_scores),
            named_score_map=ps.named_score_map,
            named_confidence_map=ps.named_confidence_map,
            synthetic_score=ps.synthetic_score,
            synthetic_confidence=ps.synthetic_confidence,
            observation_total=ps.observation_total,
        )


@dataclass(frozen=True)
class ScoredPair:
    """One cohort pair scored over EXACTLY required_cells at now=as_of. pair_id /
    subject_id / cohort_role are HOISTED here (pair-invariant across cells).

    The grid holds CellScore, never PairScore: a mutable PairScore reachable from the
    cohort would leave every score rewritable after the bindings were stamped."""

    pair_id: str
    subject_id: str
    # The artifact's array-order-aligned pseudonymous user id — the one the overlay was
    # ACTUALLY scored under (it rides PairScore.user_id, which is otherwise dropped by the
    # CellScore snapshot). Hoisted so the cohort still carries it: g-p4ih-selection's
    # uniqueness binding check reads {p.surrogate_user_id for p in cohort.pairs}, and it
    # cannot be reconstructed from anything else on the wrapper. NEVER a production user_id.
    surrogate_user_id: int
    cohort_role: str  # "quantile" | "release_guard"
    player_color: str
    grid: Mapping[GridCell, CellScore]

    def __post_init__(self) -> None:
        for cell, cs in self.grid.items():
            if not isinstance(cs, CellScore):
                raise TypeError(
                    f"ScoredPair grid at cell {cell.label!r} holds {type(cs).__name__}, "
                    "not CellScore — build it with ScoredPair.from_pair_scores so the "
                    "scores are snapshotted immutably"
                )
        object.__setattr__(self, "grid", _freeze_map(self.grid))

    @classmethod
    def from_pair_scores(
        cls,
        pair_id: str,
        subject_id: str,
        surrogate_user_id: int,
        cohort_role: str,
        player_color: str,
        grid: Mapping[GridCell, PairScore],
    ) -> ScoredPair:
        """Check the identity the PairScores were scored under against the hoisted wrapper
        values, THEN snapshot each into an immutable CellScore. The checks live here because
        this is the last point at which the PairScore identity fields exist — after the
        snapshot, ``user_id`` in particular is gone."""
        for cell, ps in grid.items():
            if (ps.pair_id, ps.subject_id, ps.cohort_role) != (
                pair_id, subject_id, cohort_role
            ):
                raise ValueError(
                    f"ScoredPair pseudonym mismatch at cell {cell.label!r}: wrapper "
                    f"{(pair_id, subject_id, cohort_role)!r} vs PairScore "
                    f"{(ps.pair_id, ps.subject_id, ps.cohort_role)!r}"
                )
            if ps.player_color != player_color:
                raise ValueError(
                    f"ScoredPair player_color mismatch at cell {cell.label!r}: "
                    f"{player_color!r} vs PairScore {ps.player_color!r}"
                )
            if ps.user_id != surrogate_user_id:
                raise ValueError(
                    f"ScoredPair surrogate_user_id mismatch at cell {cell.label!r}: wrapper "
                    f"{surrogate_user_id!r} vs the id the overlay was scored under "
                    f"{ps.user_id!r}"
                )
        return cls(
            pair_id=pair_id,
            subject_id=subject_id,
            surrogate_user_id=surrogate_user_id,
            cohort_role=cohort_role,
            player_color=player_color,
            grid={cell: CellScore.from_pair_score(ps) for cell, ps in grid.items()},
        )


@dataclass(frozen=True)
class ArtifactProvenance:
    """The IMMUTABLE input-side provenance, populated VERBATIM from the validated header
    + the load-time SHA-256. This lives on the INPUT and is the structure the selector's
    binding checks compare AGAINST; the SelectionResult echo is produced only AFTER the
    checks pass. ``captured_model_version`` is the CAPTURE model — provenance only, never
    compared to the scoring-time model.

    The four capture-attestation fields (g-p4ih-producer-bind) DELIBERATELY do not
    propagate here: this is the gating-relevant subset the selector compares against, and
    the attestation is non-gating and inert at scoring time. The scored cohort is still
    content-bound to it through ``provenance_record_sha256``, which hashes the on-disk
    provenance-record bytes that now carry the four fields."""

    artifact_sha256: str
    artifact_as_of: datetime
    graph_fingerprint: str
    roots_fingerprint: str
    captured_model_version: str
    schema_version: int
    pair_count: int
    min_observations: int
    cohort_rules: str
    evidence_derivation_fingerprint: str
    release_guard_opening_key: str
    release_guard_child_opening_key: str


@dataclass(frozen=True)
class ScoredCalibrationCohort:
    """Produced ONLY by the scoring pass — the ONLY code touching the private artifact
    and the wall clock. Every binding fact is stamped here from the header + the runtime,
    never caller-provided."""

    provenance: ArtifactProvenance
    as_of: datetime  # the pinned clock ACTUALLY threaded as now=, stamped from the header
    model_version: str  # SCORE_MODEL_VERSION at scoring time (NOT captured_model_version)
    scorer_contract_id: str  # REPORT_SCORER_CONTRACT_ID — fold-scorer SEMANTICS identity
    source_revision: str | None  # git rev-parse HEAD — AUDIT only, never a gate
    source_dirty_paths: tuple[str, ...]  # SCORER_SOURCE_FILES differing from HEAD — audit
    scorer_source_digest: str  # the deterministic source binding
    # True iff BOTH: a launcher handed in a pre-exec digest that matched
    # (SCORER_SOURCE_DIGEST_ENV), AND every scorer module was compiled from that verified
    # source rather than served from a bytecode cache CPython never checked against it
    # (check_scorer_bytecode). False means the digest above was only fenced from import
    # onward over SOURCE bytes, so it is not proven to name the code that RAN. Carried, not
    # hidden: a release gate requires True.
    scorer_source_verified_preexec: bool
    provenance_record_sha256: str  # SHA-256 over the on-disk provenance-record bytes
    runtime_python: str  # platform.python_version()
    runtime_chess_version: str  # chess.__version__
    config_fingerprints: Mapping[GridCell, str]  # _cfg_fp(cell) per required cell
    required_cells: frozenset[GridCell]
    manifest_pair_ids: frozenset[str]
    pairs: tuple[ScoredPair, ...]  # one per manifest pair, no dups

    def __post_init__(self) -> None:
        # Normalize every container to an immutable one, so a caller's dict/list cannot
        # stay a live handle into a stamped cohort.
        object.__setattr__(self, "config_fingerprints", _freeze_map(self.config_fingerprints))
        object.__setattr__(self, "required_cells", frozenset(self.required_cells))
        object.__setattr__(self, "manifest_pair_ids", frozenset(self.manifest_pair_ids))
        object.__setattr__(self, "pairs", tuple(self.pairs))
        object.__setattr__(self, "source_dirty_paths", tuple(self.source_dirty_paths))


@dataclass(frozen=True)
class DiagnosticCellResult:
    """Per-cell synthetic diagnostic operands the raw gates read — scored under the SAME
    required_cells + as_of + model/config as the cohort."""

    synth_black_root_score: float
    synth_caro_child_score: float
    synth_root_coverage_fraction: float
    synth_user_turn_multiplier: float
    synth_opp_turn_multiplier: float
    synth_user_turn_pre_fold_quality: float
    synth_opp_turn_score: float
    synth_opp_turn_pre_fold_quality: float
    broad_guard_opp_score: float
    specialist_pre_fold_quality: float
    user_tp_score: float


@dataclass(frozen=True)
class DiagnosticSuite:
    """The synthetic diagnostics scored alongside the cohort. as_of / model_version /
    scorer_contract_id equal the cohort's (binding check 4). ``cells`` is keyed over
    EXACTLY required_cells ∪ DEMO_CELLS — demo cells appear ONLY here, never in
    ScoredPair.grid."""

    as_of: datetime
    model_version: str
    scorer_contract_id: str
    config_fingerprints: Mapping[GridCell, str]  # every cell in cells (demo included)
    cells: Mapping[GridCell, DiagnosticCellResult]

    def __post_init__(self) -> None:
        # The gates read these operands; a mutable dict here is as exploitable as a mutable
        # cohort score (DiagnosticCellResult itself is all scalars).
        object.__setattr__(self, "config_fingerprints", _freeze_map(self.config_fingerprints))
        object.__setattr__(self, "cells", _freeze_map(self.cells))


@dataclass(frozen=True)
class SelectionInputs:
    """What build_selection_inputs returns and select_candidate consumes."""

    cohort: ScoredCalibrationCohort
    diagnostics: DiagnosticSuite


def _diagnostic_cell_result(
    scenario: User14Scenario, cell: GridCell, *, as_of: datetime
) -> DiagnosticCellResult:
    """The full raw-gate operand set for ONE cell: the nine User-14 operands plus the
    broad-guard NOT-crater operand and the specialist leak operand, all under now=as_of."""
    ops = _user14_cell_operands(scenario, cell, as_of=as_of)
    return DiagnosticCellResult(
        synth_black_root_score=float(ops["synth_black_root_score"]),
        synth_caro_child_score=float(ops["synth_caro_child_score"]),
        synth_root_coverage_fraction=float(ops["synth_root_coverage_fraction"]),
        synth_user_turn_multiplier=float(ops["synth_user_turn_multiplier"]),
        synth_opp_turn_multiplier=float(ops["synth_opp_turn_multiplier"]),
        synth_user_turn_pre_fold_quality=float(ops["synth_user_turn_pre_fold_quality"]),
        synth_opp_turn_score=float(ops["synth_opp_turn_score"]),
        synth_opp_turn_pre_fold_quality=float(ops["synth_opp_turn_pre_fold_quality"]),
        broad_guard_opp_score=float(run_broad_guard_diagnostic(cell, as_of=as_of)),
        specialist_pre_fold_quality=float(run_specialist_diagnostic(cell, as_of=as_of)),
        user_tp_score=float(ops["user_tp_score"]),
    )


def _current_runtime_binding(
    graph: OpeningGraph, roots: OpeningRoots
) -> RuntimeBinding:
    """The current-runtime surfaces the split load guard's scoring-validity phase (Phase
    C) compares the header against. Built from the LIVE graph/roots the scores are
    computed with, so a header fingerprint mismatch means the scores would have been
    computed under a different graph/roots than the artifact was frozen against."""
    return RuntimeBinding(
        graph_fingerprint=graph.fingerprint,
        roots_fingerprint=roots.fingerprint,
        evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
        min_observations=DEFAULT_MIN_OBSERVATIONS,
        cohort_rules=COHORT_RULES_ID,
        release_guard_opening_key=RELEASE_GUARD_OPENING_KEY,
        release_guard_child_opening_key=RELEASE_GUARD_CHILD_OPENING_KEY,
    )


def build_selection_inputs(artifact_path: str | Path) -> SelectionInputs:
    """The ONE impure step, and the ONLY supported entry point: load + validate a
    frozen-cohort artifact, reconstruct its overlays, score the cohort and the synthetic
    diagnostics under the artifact header clock, and STAMP every binding fact onto typed
    wrappers.

    SINGLE-ARGUMENT by contract. Every other input is a TRUST BOUNDARY the caller must not
    be able to move:
      * ``as_of`` (blocking): a caller-provided clock would let the caller score under a
        clock other than the header's while the wrappers store only the one as_of they
        were handed. The clock comes ONLY from the validated header.
      * the provenance record: it is the COMMITTED, reviewed approval of an artifact. A
        caller-supplied path lets a caller pass an unapproved artifact plus a freshly
        generated record that matches it — the split guard would accept the pair and stamp
        its digest, so Phase 2 would select against an artifact nobody approved.
      * graph / roots: the guard can only compare the fingerprints these registries
        REPORT, and OpeningGraph.fingerprint is cached while its nodes stay mutable, so an
        injected registry can keep a matching fingerprint while scoring altered topology.
        Production always takes them from the live registry.

    Fails CLOSED if the artifact or the committed provenance record is unavailable, if the
    split load guard rejects (integrity or scoring-validity), or if the scorer source moves
    during the run. Governance stance: never fall back to live cohort selection.
    """
    return _build_selection_inputs(
        artifact_path,
        provenance_path=COHORT_PROVENANCE_PATH,
        graph=get_opening_graph(),
        roots=get_opening_roots(),
    )


def _build_selection_inputs(
    artifact_path: str | Path,
    *,
    provenance_path: str | Path,
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> SelectionInputs:
    """Implementation behind build_selection_inputs, with the three trust-boundary inputs
    injectable. TEST-ONLY: never call this from the release path or from any caller that
    takes them from user input — build_selection_inputs is the entry point, and it is the
    thing that pins them to the committed record and the live registry. Kwargs are
    REQUIRED (no defaults) so this cannot be mistaken for the public API.

    The clock is NOT injectable even here: it still comes only from the validated header.
    """
    # --- source-stability fence (open) ---------------------------------------------
    # The digest only binds what the manifest covers, so prove the manifest is complete
    # before trusting it, then prove the bytes on disk have not moved since import.
    check_scorer_source_manifest()
    fenced_digest = scorer_source_digest()
    if fenced_digest != _SCORER_SOURCE_DIGEST_AT_IMPORT:
        raise ScorerSourceUnstableError(
            "scorer source changed after this process imported it "
            f"({_SCORER_SOURCE_DIGEST_AT_IMPORT[:12]} at import, {fenced_digest[:12]} on "
            "disk): the running code is no longer the code on disk, so no digest can "
            "honestly describe it — restart from a stable tree"
        )
    # A launcher digest predates this interpreter, so agreeing with it is what rules out an
    # edit during compilation. Its ABSENCE is not an error (dev/test runs have no launcher)
    # — it is stamped, so a release gate can demand the guarantee this module cannot fake.
    # Read from the import-time capture, NOT os.environ: see _LAUNCHER_SCORER_DIGEST.
    launcher_digest = _LAUNCHER_SCORER_DIGEST
    if launcher_digest is not None and launcher_digest != fenced_digest:
        raise ScorerSourceUnstableError(
            f"{SCORER_SOURCE_DIGEST_ENV} was computed before this interpreter started "
            f"({launcher_digest[:12]}) but the tree now hashes {fenced_digest[:12]}: the "
            "scorer moved while Python was compiling it — the code that would score is not "
            "the code that was approved"
        )
    if launcher_digest is not None:
        # The digest binds SOURCE bytes, but the interpreter runs BYTECODE. Matching source
        # is not enough: a stale .pyc CPython still considers valid would execute other code
        # entirely. Only claim the flag once that gap is closed too.
        check_scorer_bytecode()
        # ...and bytecode freshness only speaks for the files we IMPORTED. A module preloaded
        # from another checkout is fresh, matches its own source, and is still not the code
        # this digest names.
        check_scorer_import_origins()
    preexec_verified = launcher_digest is not None

    # Load BOTH byte strings from disk (unavailable -> fail closed, never live-select) and
    # run the split load guard. The load guard reads as_of FROM the validated header.
    artifact_bytes = Path(artifact_path).read_bytes()
    provenance_bytes = Path(provenance_path).read_bytes()
    runtime_binding = _current_runtime_binding(graph, roots)
    try:
        loaded = load_frozen_artifact(artifact_bytes, provenance_bytes, runtime_binding)
    except ArtifactIntegrityError as exc:
        # _check_integrity is a pure function over values and cannot name the files; the
        # caller that knows the paths re-raises with path context + recovery guidance.
        # BASENAMES only — never the full private artifact path (it would leak the private
        # store's layout into scrollback / CI logs, the one channel nobody redacts).
        raise ArtifactIntegrityError(
            f"{exc}: the artifact and provenance record do not describe each other "
            f"(record {Path(provenance_path).name}, artifact {Path(artifact_path).name}); "
            "rerun capture-cohort to republish a matched pair, or restore the artifact "
            "matching this record."
        ) from exc

    header = loaded.header
    as_of = header.as_of  # the ONLY clock source on this path

    # Score cohort pairs over EXACTLY required_cells at now=as_of. required_cells is the
    # default arm grid (both anchors, ARM-1×P_GRID, ARM-2×P_GRID, B1); NO demos.
    required_cells = build_arm_grid().cells
    scored_pairs: list[ScoredPair] = []
    for lp in loaded.pairs:
        cell_grid = {
            cell: score_overlay(
                lp.surrogate_user_id,
                lp.player_color,
                graph,
                lp.overlay,
                roots,
                cell.config,
                as_of=as_of,
                pair_id=lp.pair_id,
                subject_id=lp.subject_id,
                cohort_role=lp.cohort_role,
            )
            for cell in required_cells
        }
        scored_pairs.append(
            # Snapshots each PairScore into an immutable CellScore; the mutable PairScores
            # are dropped here and never reachable from the returned SelectionInputs.
            ScoredPair.from_pair_scores(
                pair_id=lp.pair_id,
                subject_id=lp.subject_id,
                surrogate_user_id=lp.surrogate_user_id,
                cohort_role=lp.cohort_role,
                player_color=lp.player_color,
                grid=cell_grid,
            )
        )
    manifest_pair_ids = frozenset(lp.pair_id for lp in loaded.pairs)
    if len(manifest_pair_ids) != len(loaded.pairs):
        raise ArtifactIntegrityError(
            f"duplicate pair_id in the loaded manifest: {len(loaded.pairs)} pairs, "
            f"{len(manifest_pair_ids)} distinct ids"
        )

    # A quantile pair the load-time observation threshold ADMITS can still pool zero named
    # scores for a cell (its evidence sits off the roots' subtrees), so a valid artifact
    # with the required pair count does not imply a derivable cutoff distribution. Assert
    # the sufficient post-scoring precondition HERE, per cell, or Phase 2 gets a cohort
    # that only blows up later inside derive_cutoffs as a bare ValueError.
    assert_min_quantile_scores_per_cell(
        [p for p in scored_pairs if p.cohort_role == "quantile"], required_cells
    )

    provenance = ArtifactProvenance(
        artifact_sha256=loaded.artifact_sha256,
        artifact_as_of=as_of,
        graph_fingerprint=header.graph_fingerprint,
        roots_fingerprint=header.roots_fingerprint,
        captured_model_version=header.captured_model_version,
        schema_version=header.schema_version,
        pair_count=header.pair_count,
        min_observations=header.min_observations,
        cohort_rules=header.cohort_rules,
        evidence_derivation_fingerprint=header.evidence_derivation_fingerprint,
        release_guard_opening_key=header.release_guard_opening_key,
        release_guard_child_opening_key=header.release_guard_child_opening_key,
    )
    cohort = ScoredCalibrationCohort(
        provenance=provenance,
        as_of=as_of,
        model_version=SCORE_MODEL_VERSION,
        scorer_contract_id=REPORT_SCORER_CONTRACT_ID,
        source_revision=_git_head_revision(),
        source_dirty_paths=_scorer_dirty_paths(),
        scorer_source_digest=fenced_digest,  # the FENCED digest, not a fresh read
        scorer_source_verified_preexec=preexec_verified,
        provenance_record_sha256=hashlib.sha256(provenance_bytes).hexdigest(),
        runtime_python=platform.python_version(),
        runtime_chess_version=chess.__version__,
        config_fingerprints={cell: _cfg_fp(cell) for cell in required_cells},
        required_cells=frozenset(required_cells),
        manifest_pair_ids=manifest_pair_ids,
        pairs=tuple(scored_pairs),
    )

    # Synthetic diagnostics over required_cells ∪ DEMO_CELLS, scored under the HEADER
    # clock (binding check 4 requires cohort.as_of == diagnostics.as_of). The scenario is
    # timestamp-free, so the header clock and SYNTHETIC_AS_OF score it identically.
    diag_cells = tuple(required_cells) + DEMO_CELLS
    scenario = _user14_scenario()
    diagnostics = DiagnosticSuite(
        as_of=as_of,
        model_version=SCORE_MODEL_VERSION,
        scorer_contract_id=REPORT_SCORER_CONTRACT_ID,
        config_fingerprints={cell: _cfg_fp(cell) for cell in diag_cells},
        cells={
            cell: _diagnostic_cell_result(scenario, cell, as_of=as_of)
            for cell in diag_cells
        },
    )

    # --- source-stability fence (close) --------------------------------------------
    # Re-read AFTER the last score (cohort AND diagnostics). Identical at import, at the
    # open fence, and now => the scorer bytes did not move across the scoring window, so an
    # edit landing mid-run fails CLOSED: nothing is returned, and no half-stamped
    # SelectionInputs can reach Phase 2/3. This does NOT by itself prove the digest names
    # the compiled code (see _SCORER_SOURCE_DIGEST_AT_IMPORT); that needs the launcher
    # digest, whose presence is stamped as scorer_source_verified_preexec.
    final_digest = scorer_source_digest()
    if final_digest != fenced_digest:
        raise ScorerSourceUnstableError(
            "scorer source changed DURING the run "
            f"({fenced_digest[:12]} -> {final_digest[:12]}): these scores were produced by "
            "code that no longer matches the tree — nothing is stamped, re-run"
        )

    return SelectionInputs(cohort=cohort, diagnostics=diagnostics)


# ===========================================================================
# Capture: snapshot freeze + consistency fence + atomic publication
# (g-p4ih-capture). The PRODUCER of the frozen-cohort artifact. Reads all
# evidence inside ONE read-only REPEATABLE READ PostgreSQL snapshot, verifies
# the window was quiescent for the cohort's membership function via a
# post-snapshot movement check from a fresh transaction, stamps as_of INSIDE
# the fence, pseudonymizes at capture (the freeze transform runs it), self-
# checks the candidate bytes through validate_capture_candidate, and publishes
# with an artifact-before-record rename order whose failure modes are all
# inert-and-recoverable.
# ===========================================================================


@dataclass(frozen=True)
class CaptureValidationResult:
    """The NARROW result of ``validate_capture_candidate`` — scalars only, nothing
    scoreable. Deliberately NOT ``SelectionInputs``: returning that type would hand a
    caller a ready-to-select bundle assembled from caller-supplied bytes and caller-
    supplied registries, which is exactly the trust boundary ``build_selection_inputs``
    exists to hold. The scored structures the self-check builds are constructed, asserted
    against, and DISCARDED inside the function; only these four scalars escape, and
    ``select_candidate`` cannot be fed from them."""

    artifact_sha256: str
    pair_count: int
    quantile_count: int
    as_of: datetime


def validate_capture_candidate(
    artifact_bytes: bytes,
    provenance_bytes: bytes,
    *,
    graph: OpeningGraph,
    roots: OpeningRoots,
) -> CaptureValidationResult:
    """Re-run the FULL load guard AND the release path's score-shape asserts against
    CANDIDATE bytes, before capture publishes them. A NEW producer-safe entry point beside
    ``build_selection_inputs``, because neither existing entry point can do the job:
    ``build_selection_inputs`` is single-argument by contract and always reads the COMMITTED
    provenance path (the very file capture has not published yet), and
    ``_build_selection_inputs`` is TEST-ONLY. This takes the candidate bytes DIRECTLY and
    the registries EXPLICITLY.

    The CLOCK IS NOT AN ARGUMENT: it comes only from the validated header, exactly like the
    public entry point. graph/roots are explicit so the producer threads the SAME instances
    it froze against.

    Runs: the split load guard (semantic validation, canonical-byte re-encode invariant,
    SHA-256 recompute + header-vs-record equality, graph/roots +
    evidence_derivation_fingerprint, release-guard keys / schema_version / cohort_rules /
    min_observations policy pins); surrogate-uniqueness; ONE scoring pass under the header
    ``as_of`` whose result is checked with the SAME two shape assertions the release path
    uses — ``assert_min_quantile_scores_per_cell`` and ``assert_release_guard_score_shape``.
    NOTHING stricter: a quantile pair that pools ZERO named scores for a cell is fine as
    long as the POOLED distribution reaches len >= 2, exactly as the release path accepts.
    """
    runtime_binding = _current_runtime_binding(graph, roots)
    loaded = load_frozen_artifact(artifact_bytes, provenance_bytes, runtime_binding)
    header = loaded.header
    as_of = header.as_of  # the ONLY clock source on this path

    required_cells = build_arm_grid().cells
    scored_pairs: list[ScoredPair] = []
    for lp in loaded.pairs:
        cell_grid = {
            cell: score_overlay(
                lp.surrogate_user_id,
                lp.player_color,
                graph,
                lp.overlay,
                roots,
                cell.config,
                as_of=as_of,
                pair_id=lp.pair_id,
                subject_id=lp.subject_id,
                cohort_role=lp.cohort_role,
            )
            for cell in required_cells
        }
        scored_pairs.append(
            ScoredPair.from_pair_scores(
                pair_id=lp.pair_id,
                subject_id=lp.subject_id,
                surrogate_user_id=lp.surrogate_user_id,
                cohort_role=lp.cohort_role,
                player_color=lp.player_color,
                grid=cell_grid,
            )
        )

    # surrogate_user_id uniqueness (the load guard already pins surrogate == index + 1 per
    # pair; assert the SET is distinct so a self-check failure names this specifically).
    surrogates = [lp.surrogate_user_id for lp in loaded.pairs]
    if len(set(surrogates)) != len(surrogates):
        raise ArtifactIntegrityError(
            f"duplicate surrogate_user_id in the loaded manifest: {surrogates}"
        )

    quantile_pairs = [p for p in scored_pairs if p.cohort_role == "quantile"]
    guard_pairs = [p for p in scored_pairs if p.cohort_role == "release_guard"]
    assert_min_quantile_scores_per_cell(quantile_pairs, required_cells)
    assert_release_guard_score_shape(guard_pairs, required_cells, runtime_binding)

    # Everything above is DISCARDED — only these four scalars escape.
    return CaptureValidationResult(
        artifact_sha256=loaded.artifact_sha256,
        pair_count=header.pair_count,
        quantile_count=len(quantile_pairs),
        as_of=as_of,
    )


# ---- Typed capture errors (the subcommand maps these to exit codes) --------


class CaptureError(RuntimeError):
    """Base for all typed capture failures. Every one fails closed with NO shippable
    half-state; turning them into exit codes and messages is the subcommand's job
    (g-p4ih-release-cli). Private artifact paths appear as BASENAMES only."""


class CaptureIsolationError(CaptureError):
    """Not launched through capture_cohort.sh (missing -S / bytecode-off / launcher digest)."""


class CaptureWorktreeError(CaptureError):
    """A linked worktree or a release-launcher-hosted run (git-common-dir != git-dir)."""


class CaptureGovernanceError(CaptureError):
    """A repo-interior output path, or a missing release-guard user."""


class CaptureDialectError(CaptureError):
    """The bound engine is not PostgreSQL — there is no degraded fence."""


class CaptureLockError(CaptureError):
    """A concurrent capture already holds one of the two mutual-exclusion locks."""


class CaptureSourceError(CaptureError):
    """A dirty or unverifiable derivation source (manifest gap, moved digest, stale
    bytecode, foreign import origin, shadow chess module, or non-CPython runtime)."""


class CaptureFenceExhaustedError(CaptureError):
    """Scoped movement observed on every bounded attempt — the window never quiesced."""


class CaptureEpochUnavailableError(CaptureError):
    """require_quiescent_epoch=True but the evidence_epoch singleton is missing, so no live
    value can vouch for quiescence — a hard fail, never treated as quiescence."""


class CaptureReleaseGuardShapeError(CaptureError):
    """The release-guard query returned other than exactly two {white, black} pairs."""


class CaptureSelfCheckError(CaptureError):
    """The pre-publication self-check rejected the candidate bytes."""


class CapturePublicationError(CaptureError):
    """A filesystem operation on a private destination failed (create/write/fsync/rename).

    Every OSError raised while touching a private path is funnelled through here for one
    reason: ``str(OSError)`` renders as ``[Errno 2] No such file or directory:
    '/private/store/cohort.json'``, so letting one escape prints the full private-store path
    into a traceback and out of the typed error boundary the child's ``except CaptureError``
    depends on. The message carries ``exc.strerror`` (the errno text ALONE) plus a BASENAME."""


class CaptureInterRenameError(CaptureError):
    """The second rename failed after the first succeeded — a recoverable mismatched pair."""


@dataclass(frozen=True)
class CaptureResult:
    """What ``capture_cohort`` returns on success. Scalars + the final artifact path; the
    subcommand redacts the path to a basename in any human-facing summary."""

    artifact_path: Path
    artifact_sha256: str
    pair_count: int
    quantile_count: int
    release_guard_count: int
    attempts: int
    orphan_replaced: bool
    snapshot_cache_epoch: int | None
    current_view_cache_epoch: int | None


@dataclass(frozen=True)
class _CaptureAttestation:
    scorer_source_digest: str
    source_revision: str
    python_version: str
    chess_version: str


@dataclass(frozen=True)
class _PairSample:
    evidence_seq: int
    inputs_fingerprint: str
    cache_epoch: int | None


# ---- Precondition refusals (each a separate function so the fence/publication
#      tests can drive the inner logic without a real two-process launch) -----


def _require_capture_isolation() -> None:
    """Precedence 1. Refuse a bare or wrongly-flagged invocation before any DB work: a
    verified capture is launched by capture_cohort.sh (``python -I -S`` launcher -> ``-S``
    child) with a pre-exec source digest. The in-process bytecode check fails closed
    without the pre-exec environment, so an absent digest means capture was invoked bare."""
    problems = []
    if not getattr(sys.flags, "no_site", False):
        problems.append("interpreter is not under -S (site.py / .pth / sitecustomize ran)")
    if not sys.dont_write_bytecode:
        problems.append("bytecode writing is enabled (PYTHONDONTWRITEBYTECODE unset / -B absent)")
    if _LAUNCHER_SCORER_DIGEST is None:
        problems.append(
            f"{SCORER_SOURCE_DIGEST_ENV} was not inherited (capture was not launched through "
            "capture_cohort.sh)"
        )
    if problems:
        raise CaptureIsolationError(
            "capture must be launched through capture_cohort.sh, which runs it under the "
            "same two-process isolation the release path proves: " + "; ".join(problems)
        )


def _git_capture(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(_REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _require_main_worktree() -> str:
    """Precedence 2. Refuse a linked worktree (and thereby a release-launcher-hosted run,
    which executes in a throwaway worktree): compare --git-common-dir against --git-dir,
    which are equal ONLY in the main worktree. A launcher-hosted capture would write its
    provenance record into a directory destroyed on exit, so the reviewable working-tree
    diff would silently never appear. Returns the resolved git COMMON dir (the provenance
    lock target)."""
    result = _git_capture("rev-parse", "--path-format=absolute", "--git-common-dir", "--git-dir")
    if result.returncode != 0:
        raise CaptureWorktreeError(
            "capture must run inside a git working tree, but `git rev-parse` failed; run it "
            "from the origin checkout via capture_cohort.sh."
        )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise CaptureWorktreeError(
            "could not resolve both --git-common-dir and --git-dir; refusing before any DB work"
        )
    common_dir, git_dir = lines
    if Path(common_dir).resolve() != Path(git_dir).resolve():
        raise CaptureWorktreeError(
            "capture must run from the MAIN worktree, but this is a LINKED worktree "
            f"(git-common-dir != git-dir). A capture hosted by the release launcher runs in "
            "an exclusive throwaway worktree whose deletion would destroy the reviewable "
            "cohort_provenance.json diff — capture uses its OWN isolated entrypoint "
            "(capture_cohort.sh) that stays in the origin checkout."
        )
    return common_dir


def _refuse_repo_interior_output(output: Path) -> Path:
    """Precedence 3. Refuse an output path resolving inside the repository working tree: the
    frozen-cohort artifact holds production-derived private data and must not become
    commit-able by accident. Returns the RESOLVED output path.

    A RELATIVE path is refused outright rather than resolved, because there is no single
    honest directory to resolve it against: the operator types it in their own cwd, but the
    launcher execs the child with ``cwd=<tree>/backend`` (release_calibration_launcher.launch),
    so ``Path.resolve()`` in this process would silently mean a DIFFERENT directory than the
    one the operator meant — publishing private evidence somewhere they did not name, or
    refusing a destination that was in fact valid. An absolute path means the same thing in
    both processes."""
    if not Path(output).is_absolute():
        raise CaptureGovernanceError(
            "--output must be an ABSOLUTE path. The launcher runs the child with a different "
            "working directory than the operator's shell, so a relative path would resolve "
            "against a directory nobody chose — a private artifact published to the wrong "
            "place, or a valid destination refused. Pass the full path to the private store."
        )
    resolved = Path(output).resolve()
    try:
        resolved.relative_to(_REPO_ROOT)
        inside = True
    except ValueError:
        inside = False
    if inside:
        raise CaptureGovernanceError(
            f"--output {resolved.name!r} resolves INSIDE the repository working tree; the "
            "frozen-cohort artifact holds production-derived private data and must land at "
            "an access-controlled path OUTSIDE the repo (governance invariant)."
        )
    return resolved


def _engine_of(session_factory: Callable[[], "object"]):
    """The engine a session_factory binds — without a DB round-trip (Session.get_bind()
    returns the Engine without connecting)."""
    probe = session_factory()
    try:
        return probe.get_bind()
    finally:
        probe.close()


def _require_postgres_dialect(engine) -> None:
    """Precedence 4. Refuse any non-PostgreSQL dialect before the first capture read: the
    fence's ONLY consistency mechanism is PostgreSQL REPEATABLE READ snapshot isolation and
    there is NO multi-transaction fallback (which would stamp overlays assembled across
    transactions — the torn-artifact hazard the fence exists to exclude)."""
    name = getattr(engine.dialect, "name", "<unknown>")
    if name != "postgresql":
        raise CaptureDialectError(
            f"capture requires a PostgreSQL evidence DB (the consistency fence IS REPEATABLE "
            f"READ snapshot isolation); the bound dialect is {name!r}. A dialect that cannot "
            "provide the snapshot cannot produce a publishable artifact — there is no "
            "degraded mode."
        )


# ---- Producer source/runtime binding (before any evidence read) ------------


def _assert_chess_from_installed_distribution() -> None:
    """CHESS-MODULE ORIGIN: check_scorer_import_origins covers app.* but NOT ``chess``, and
    capture_chess_version alone cannot prove identity (this script inserts BACKEND_ROOT onto
    sys.path before ``import chess``, so a shadow backend/chess.py declaring a matching
    __version__ would pass both the grammar and the app.*-only origin check). Assert
    ``chess.__file__`` resolves under the INSTALLED distribution, NOT the repo tree, cross-
    checking the imported module's file against the distribution RECORD."""
    chess_file = getattr(chess, "__file__", None)
    if not chess_file:
        raise CaptureSourceError("the imported chess module has no __file__; its origin is unverifiable")
    chess_path = Path(chess_file).resolve()
    try:
        dist = importlib.metadata.distribution("chess")
    except importlib.metadata.PackageNotFoundError as exc:
        raise CaptureSourceError(
            "no installed 'chess' distribution metadata found; cannot verify the module origin"
        ) from exc
    # The imported module's file must be part of the INSTALLED distribution — either listed in
    # its RECORD (authoritative) or, if the RECORD is unavailable, resolving under the install
    # location. A repo-interior shadow (backend/chess.py, imported ahead of the package because
    # this script puts BACKEND_ROOT on sys.path) satisfies NEITHER, even with a matching
    # __version__. Note the repo-local venv (backend/.venv/.../site-packages) IS the install
    # location, so a bare "under the repo tree" test would false-positive on it.
    recorded: set[Path] = set()
    for f in (dist.files or []):
        try:
            recorded.add(Path(dist.locate_file(f)).resolve())
        except Exception:  # pragma: no cover - defensive on odd RECORD entries
            continue
    if recorded:
        if chess_path not in recorded:
            raise CaptureSourceError(
                f"the imported chess module ({chess_path}) is not listed in the installed chess "
                "distribution's RECORD — a repo-interior shadow (e.g. backend/chess.py) can "
                "declare any __version__ and would be imported ahead of the package. Remove it."
            )
    else:
        install_base = Path(dist.locate_file("")).resolve()
        try:
            chess_path.relative_to(install_base)
        except ValueError as exc:
            raise CaptureSourceError(
                f"the imported chess module ({chess_path}) does not resolve under the installed "
                f"distribution location ({install_base}); refusing (possible shadow module)."
            ) from exc


def _resolve_clean_head_revision() -> str:
    """A CLEAN-TREE requirement: ``git status --porcelain`` over SCORER_SOURCE_FILES must be
    empty and HEAD must resolve to a 40-hex commit. A capture from a dirty derivation tree
    REFUSES — the one place a hard refusal is right rather than a recorded warning, because
    'it was captured from someone's uncommitted edit' is not a condition anyone can evaluate
    six months later from a digest."""
    status = _git_capture("status", "--porcelain", "--", *SCORER_SOURCE_FILES)
    if status.returncode != 0:
        raise CaptureSourceError(
            "`git status --porcelain` over the scorer manifest failed; cannot verify a clean "
            "derivation tree, so capture refuses."
        )
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        raise CaptureSourceError(
            "capture refuses to run from a DIRTY derivation tree; uncommitted changes over "
            f"SCORER_SOURCE_FILES: {sorted(line[3:].strip() for line in dirty)}. An artifact "
            "is a long-lived, authorized production of private data."
        )
    head = _git_capture("rev-parse", "HEAD")
    revision = head.stdout.strip()
    if head.returncode != 0 or not _GIT_REVISION_RE.match(revision):
        raise CaptureSourceError(
            "could not resolve a 40-hex HEAD commit for the capture attestation; refusing."
        )
    return revision


def _capture_source_fence() -> _CaptureAttestation:
    """Run the SAME in-process source fence _build_selection_inputs runs, but REQUIRING the
    inherited launcher digest (unlike the dev/selection path, which tolerates its absence),
    then the chess-origin, non-CPython, and clean-tree checks capture adds. Returns the four
    reviewable attestation values. Any source instability refuses BEFORE any evidence read."""
    impl = platform.python_implementation()
    if impl != "CPython":
        raise CaptureSourceError(
            f"capture refuses to run under {impl}; the byte-stability contract puts non-CPython "
            "runtimes out of scope, so a frozen artifact must always name CPython."
        )
    try:
        check_scorer_source_manifest()
        fenced_digest = scorer_source_digest()
        if fenced_digest != _SCORER_SOURCE_DIGEST_AT_IMPORT:
            raise ScorerSourceUnstableError(
                "scorer source changed after this process imported it "
                f"({_SCORER_SOURCE_DIGEST_AT_IMPORT[:12]} at import, {fenced_digest[:12]} on "
                "disk): the running code is no longer the code on disk"
            )
        launcher_digest = _LAUNCHER_SCORER_DIGEST
        if launcher_digest is None:
            # The isolation precondition already refuses a missing digest; re-guard so this
            # function is safe to call directly (capture REQUIRES the pre-exec digest).
            raise ScorerSourceUnstableError(
                f"{SCORER_SOURCE_DIGEST_ENV} was not inherited; capture requires the pre-exec "
                "digest its launcher computes before this interpreter existed."
            )
        if launcher_digest != fenced_digest:
            raise ScorerSourceUnstableError(
                f"{SCORER_SOURCE_DIGEST_ENV} was computed before this interpreter started "
                f"({launcher_digest[:12]}) but the tree now hashes {fenced_digest[:12]}: the "
                "scorer moved while Python was compiling it — the code that would score is not "
                "the code that was approved"
            )
        # Capture ALWAYS runs these (step 2 REQUIRED the launcher digest), unlike the
        # selection path which runs them only when a launcher digest is present.
        check_scorer_bytecode()
        check_scorer_import_origins()
    except (ScorerSourceManifestError, ScorerSourceUnstableError) as exc:
        raise CaptureSourceError(str(exc)) from exc

    _assert_chess_from_installed_distribution()
    revision = _resolve_clean_head_revision()
    return _CaptureAttestation(
        scorer_source_digest=fenced_digest,
        source_revision=revision,
        python_version=f"{impl} {platform.python_version()}",
        chess_version=chess.__version__,
    )


# ---- Mutual exclusion, private temps, orphan detection, publication --------


@contextlib.contextmanager
def _capture_locks(common_dir: str, resolved_output: Path):
    """TWO flock(LOCK_EX | LOCK_NB) locks in FIXED order (provenance, then output), because
    there are two shared resources and a checkout-relative lock protects neither reliably:

      * provenance lock at <git-common-dir>/cohort-capture.lock — the COMMON dir is shared
        by the main worktree and every linked worktree, so one lock file, one inode, no
        matter how many checkouts exist; outside the tree, so untracked by construction.
      * output lock at <output-parent>/.cohort-capture-<sha256(resolved output)>.lock, 0600
        — the private destination may be written by captures from unrelated checkouts (or a
        clone) that share no git common dir; keying on the resolved path follows the resource.

    Nonblocking, because a second concurrent capture is an OPERATOR ERROR, not a queue. An
    flock dies with the holding process (including kill -9), so there is no stale-lock reap."""
    if fcntl is None:
        raise CaptureLockError(
            "capture's mutual-exclusion locks require POSIX fcntl.flock, unavailable on this "
            "platform; refusing rather than publishing unserialized."
        )
    prov_lock = Path(common_dir) / "cohort-capture.lock"
    out_hash = hashlib.sha256(str(resolved_output).encode("utf-8")).hexdigest()
    out_lock = resolved_output.parent / f".cohort-capture-{out_hash}.lock"
    fds: list[int] = []
    try:
        for lock_path, label in ((prov_lock, "provenance"), (out_lock, "output")):
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                # Creating the lock file is the FIRST thing that touches the private output
                # directory, so it is where a missing/unwritable destination surfaces. Raise
                # inside the typed hierarchy with the errno text alone — a bare OSError here
                # prints the full private-store path in a traceback.
                raise CapturePublicationError(
                    f"cannot create the {label} lock {lock_path.name!r}: {exc.strerror}. "
                    "Check that the destination directory exists and is writable by this uid."
                ) from None
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                raise CaptureLockError(
                    f"another capture already holds the {label} lock ({lock_path.name}); a "
                    "second concurrent capture against the same evidence DB is an operator "
                    "error, not a queue to wait in — refusing before reading any evidence."
                ) from exc
            fds.append(fd)
        yield
    finally:
        for fd in reversed(fds):
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _log_stale_temps(final_path: Path) -> None:
    """LOG any leftover <final>.tmp-* beside a destination (a killed run's inert temp),
    name them as operator cleanup, and do NOT delete them — they may be another capture's
    live temp, and deleting private production-derived data on a guess is worse than
    reporting it. BASENAMES only."""
    try:
        stale = sorted(p.name for p in final_path.parent.glob(f"{final_path.name}.tmp-*"))
    except OSError:
        return
    if stale:
        print(
            f"[capture] found stale temp file(s) beside {final_path.name!r}: {stale}. These "
            "are inert (never read, adopted, or renamed by a later run) — operator cleanup.",
            file=sys.stderr,
        )


def _write_private_temp(final_path: Path, data: bytes) -> Path:
    """Write ``data`` to a private, exclusive temp in the SAME directory as ``final_path``
    (so os.replace is same-filesystem and atomic), mode 0600 AT CREATION (never briefly
    group/world-readable under a normal umask), name unguessable from the pid alone
    (<final>.tmp-<pid>-<token>), flushed + fsynced before the rename."""
    token = secrets.token_hex(8)
    temp_path = final_path.with_name(f"{final_path.name}.tmp-{os.getpid()}-{token}")
    try:
        fd = os.open(str(temp_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise CapturePublicationError(
            f"cannot create the private temp beside {final_path.name!r}: {exc.strerror}."
        ) from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException as exc:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        # ENOSPC/EIO/EDQUOT land here. Nothing was published (the final path is untouched),
        # so this is a clean typed refusal — but only if the OSError does not escape with
        # its filename attached.
        if isinstance(exc, OSError):
            raise CapturePublicationError(
                f"writing the private temp beside {final_path.name!r} failed: {exc.strerror}. "
                "The temp was removed; no final file was written."
            ) from None
        raise
    return temp_path


def _detect_and_log_orphan(resolved_output: Path, provenance_path: Path) -> bool:
    """Startup orphan detection (under the locks, before evidence reads): if a file already
    exists at the final artifact path whose SHA-256 MISMATCHES the current provenance
    record, log that it is being REPLACED — never 'adopted' (its fence provenance cannot be
    verified after the fact). The rerun's renames overwrite both files. BASENAME only."""
    try:
        if not resolved_output.exists():
            return False
        record = json.loads(provenance_path.read_bytes())
        record_sha = record.get("sha256")
        if not record_sha:
            return False
        existing_sha = hashlib.sha256(resolved_output.read_bytes()).hexdigest()
    except (OSError, ValueError):
        return False
    if existing_sha != record_sha:
        print(
            f"[capture] REPLACING a mismatched artifact at {resolved_output.name!r}: its "
            "SHA-256 does not match the committed provenance record, so its fence provenance "
            "cannot be verified — it is replaced, never adopted.",
            file=sys.stderr,
        )
        return True
    return False


@contextlib.contextmanager
def _repeatable_read_snapshot(session_factory: Callable[[], "object"]):
    """ONE read-only REPEATABLE READ snapshot on ONE connection: under PostgreSQL snapshot
    isolation every statement sees the SAME snapshot, so all stamped overlays/counters/
    digests describe one moment even if the world moves while capturing. Rolled back (never
    committed — capture writes nothing to the evidence DB) on exit."""
    db = session_factory()
    try:
        conn = db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        yield db
    finally:
        db.rollback()
        db.close()


def _collect_pair_samples(db, pairs) -> dict[tuple[int, str], _PairSample]:
    """One ``capture_freshness_snapshot`` bundle per pair. inputs_fingerprint is the
    COMPOSITE registry-plus-raw-input fingerprint (NOT a bare row digest), so a registry
    change is as visible to the fence as an evidence change; evidence_seq is the per-pair
    counter; cache_epoch is the global epoch as observed in THIS transaction."""
    out: dict[tuple[int, str], _PairSample] = {}
    for (uid, color) in pairs:
        snap = capture_freshness_snapshot(db, uid, color)
        out[(uid, color)] = _PairSample(
            evidence_seq=snap.evidence_seq,
            inputs_fingerprint=snap.inputs_fingerprint,
            cache_epoch=snap.cache_epoch,
        )
    return out


def capture_cohort(
    *,
    session_factory: Callable[[], "object"],
    output: Path,
    release_guard_user: int,
    require_quiescent_epoch: bool = False,
    max_attempts: int = 3,
) -> CaptureResult:
    """Freeze a frozen-cohort artifact from live PostgreSQL evidence under a strict
    consistency fence, self-check it in full, and publish it atomically alongside a
    reviewable provenance record. Keyword-only, fully typed, no argparse/sys.argv/sys.exit —
    it returns a CaptureResult or raises a typed CaptureError; the subcommand maps those to
    exit codes (g-p4ih-release-cli). See the g-p4ih-capture design for the full contract."""
    # Argument validity first: it costs nothing, touches nothing, and a non-positive
    # max_attempts would otherwise run an EMPTY retry loop and fall through to the
    # `assert accepted is not None` below — an AssertionError outside the typed hierarchy,
    # which the subcommand's `except CaptureError` does not catch.
    if max_attempts < 1:
        raise CaptureGovernanceError(
            f"max_attempts must be >= 1 (got {max_attempts}); a capture that is allowed zero "
            "attempts cannot establish a fence, and 'no attempts' is an operator error, not a "
            "quiescence verdict."
        )
    # --- precondition refusals, in documented precedence, before any DB work ---
    _require_capture_isolation()             # 1: launched through capture_cohort.sh
    common_dir = _require_main_worktree()    # 2: main worktree (not the release launcher's)
    resolved_output = _refuse_repo_interior_output(output)  # 3: not commit-able
    engine = _engine_of(session_factory)
    _require_postgres_dialect(engine)        # 4: PostgreSQL or refuse
    provenance_path = COHORT_PROVENANCE_PATH

    with _capture_locks(common_dir, resolved_output):
        # Producer source/runtime binding + the four attestation values (before any read).
        attestation = _capture_source_fence()
        # Startup orphan detection, under the locks (reads both destinations).
        orphan_replaced = _detect_and_log_orphan(resolved_output, provenance_path)

        # Resolve ONE graph/roots pair and thread the SAME instances through header
        # construction, derivation, and the self-check (OpeningGraph caches its fingerprint
        # while nodes stay mutable, so re-resolving could stamp one graph's fingerprint over
        # another graph's overlay).
        graph = get_opening_graph()
        roots = get_opening_roots()

        accepted = None
        for attempt in range(1, max_attempts + 1):
            # --- ONE read-only REPEATABLE READ snapshot: all evidence reads live here ---
            with _repeatable_read_snapshot(session_factory) as db:
                raw_pairs = list_opening_score_candidate_pairs(db)
                guard_pairs = list_opening_score_candidate_pairs(db, user_id=release_guard_user)
                fence_pairs = list(dict.fromkeys([*raw_pairs, *guard_pairs]))
                snap_samples = _collect_pair_samples(db, fence_pairs)
                overlays = {p: overlay_evidence(db, p[0], p[1], graph) for p in fence_pairs}
                obs_totals = {
                    p: sum(node.quality_count for node in overlays[p].nodes.values())
                    for p in fence_pairs
                }
                # as_of stamped ONCE, INSIDE the fence, AFTER the snapshot is established —
                # so it is >= every timestamp the snapshot can contain.
                as_of = datetime.now(timezone.utc)
                # The GLOBAL epoch as observed inside the accepted snapshot: THIS is stamped.
                snapshot_epoch = current_cache_epoch(db)
                bundle_epochs = {s.cache_epoch for s in snap_samples.values()}
                if bundle_epochs and bundle_epochs != {snapshot_epoch}:  # pragma: no cover
                    raise AssertionError(
                        f"snapshot epoch inconsistency: bundles {bundle_epochs} vs "
                        f"current_cache_epoch {snapshot_epoch} (REPEATABLE READ violated)"
                    )

            # Strict mode fails CLOSED on an UNAVAILABLE snapshot epoch (missing singleton):
            # shared writes fire triggers that silently no-op, so quiescence is unprovable.
            if require_quiescent_epoch and snapshot_epoch is None:
                raise CaptureEpochUnavailableError(
                    "require_quiescent_epoch=True but the evidence_epoch singleton is missing "
                    "(snapshot current_cache_epoch is None): a None is proof that quiescence "
                    "is unprovable, never a match — a hard fail, not a retry."
                )

            # --- POST-SNAPSHOT movement check from a NEW (READ COMMITTED) transaction ---
            with session_factory() as re_db:
                re_raw = list_opening_score_candidate_pairs(re_db)
                re_guard = list_opening_score_candidate_pairs(re_db, user_id=release_guard_user)
                re_present = set(re_raw) | set(re_guard)
                common = [p for p in fence_pairs if p in re_present]
                re_samples = _collect_pair_samples(re_db, common)
                post_epoch = current_cache_epoch(re_db)

            reasons: list[str] = []
            if set(re_raw) != set(raw_pairs):
                reasons.append("raw candidate-set drift (a pair appeared in or vanished from the pre-filter list)")
            if set(re_guard) != set(guard_pairs):
                reasons.append("release-guard pair-set drift")
            for p in common:
                s0, s1 = snap_samples[p], re_samples[p]
                if (s0.evidence_seq != s1.evidence_seq
                        or s0.inputs_fingerprint != s1.inputs_fingerprint):
                    reasons.append(
                        f"pair at surrogate position {fence_pairs.index(p)} moved "
                        "(evidence_seq / inputs_fingerprint)"
                    )
            scoped_moved = bool(reasons)

            if require_quiescent_epoch and post_epoch is None:
                raise CaptureEpochUnavailableError(
                    "require_quiescent_epoch=True but the evidence_epoch singleton is missing "
                    "in the post-snapshot re-read (current_cache_epoch is None) — a hard fail."
                )
            epoch_moved = snapshot_epoch != post_epoch

            # Scoped movement -> discard + retry (the window was not quiescent for the cohort's
            # MEMBERSHIP FUNCTION).
            if scoped_moved:
                if attempt >= max_attempts:
                    raise CaptureFenceExhaustedError(
                        f"the capture window was not quiescent on any of {max_attempts} "
                        f"attempts (scoped movement): {reasons}. No final artifact, no "
                        "provenance-record change."
                    )
                print(
                    f"[capture] attempt {attempt}/{max_attempts} discarded — scoped movement: "
                    f"{reasons}; retrying with a fresh snapshot.",
                    file=sys.stderr,
                )
                continue

            # Global-epoch movement: retry under strict mode; LOG + stamp the snapshot epoch
            # under the default scoped mode (unrelated engine traffic must not STARVE capture).
            if require_quiescent_epoch and epoch_moved:
                if attempt >= max_attempts:
                    raise CaptureFenceExhaustedError(
                        "require_quiescent_epoch=True and the global cache_epoch advanced on "
                        f"every one of {max_attempts} attempts; no quiescent window."
                    )
                print(
                    f"[capture] attempt {attempt}/{max_attempts} discarded — strict global "
                    f"epoch movement {snapshot_epoch} -> {post_epoch}; retrying.",
                    file=sys.stderr,
                )
                continue
            if epoch_moved:
                print(
                    f"[capture] global cache_epoch advanced {snapshot_epoch} -> {post_epoch} "
                    "during the window (unrelated engine traffic); no captured pair moved, so "
                    f"stamping the SNAPSHOT epoch {snapshot_epoch} and continuing.",
                    file=sys.stderr,
                )

            accepted = attempt
            break

        assert accepted is not None  # the loop either breaks with a value or raises
        attempts = accepted

        # --- capture-time cohort membership ---
        guard_colors = {color for (_uid, color) in guard_pairs}
        if len(guard_pairs) != 2 or guard_colors != {"white", "black"}:
            raise CaptureReleaseGuardShapeError(
                f"the release-guard query returned {len(guard_pairs)} pair(s) with colors "
                f"{sorted(guard_colors)}; capture requires EXACTLY two, one white and one "
                "black. No final artifact, no provenance change."
            )
        guard_set = set(guard_pairs)
        # Subtract the guard pairs FIRST, THEN threshold the remainder -> quantile cohort, so
        # a threshold-clearing release-guard pair is never double-counted as quantile.
        quantile_pairs = [
            p for p in raw_pairs
            if p not in guard_set and obs_totals[p] >= DEFAULT_MIN_OBSERVATIONS
        ]
        captured_inputs = [
            CapturedPairInput(
                overlays[p], "release_guard",
                snap_samples[p].evidence_seq, snap_samples[p].inputs_fingerprint,
            )
            for p in guard_pairs
        ] + [
            CapturedPairInput(
                overlays[p], "quantile",
                snap_samples[p].evidence_seq, snap_samples[p].inputs_fingerprint,
            )
            for p in quantile_pairs
        ]

        # --- freeze (pseudonymization runs INSIDE the transform) + provenance record ---
        header = ArtifactHeaderInput(
            as_of=as_of,
            graph_fingerprint=graph.fingerprint,
            roots_fingerprint=roots.fingerprint,
            cache_epoch=snapshot_epoch,
            captured_model_version=SCORE_MODEL_VERSION,
            evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
            capture_scorer_source_digest=attestation.scorer_source_digest,
            capture_source_revision=attestation.source_revision,
            capture_python_version=attestation.python_version,
            capture_chess_version=attestation.chess_version,
        )
        artifact_bytes = freeze_frozen_artifact(captured_inputs, header)
        artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        # The record mirrors the header's non-cache fields (constants for the release-policy
        # pins, header input for the rest) so _check_integrity's field-by-field compare
        # passes; the one deliberate deviation is the trailing newline (this file is COMMITTED
        # to Git, where a missing final newline shows as a diff marker; the load guard parses
        # with json.loads, which ignores trailing whitespace). The ARTIFACT gets no newline.
        record = {
            "sha256": artifact_sha256,
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "as_of": _canonical_as_of(as_of),
            "captured_model_version": SCORE_MODEL_VERSION,
            "graph_fingerprint": graph.fingerprint,
            "roots_fingerprint": roots.fingerprint,
            "evidence_derivation_fingerprint": evidence_derivation_fingerprint(),
            "pair_count": len(captured_inputs),
            "min_observations": DEFAULT_MIN_OBSERVATIONS,
            "cohort_rules": COHORT_RULES_ID,
            "release_guard_opening_key": RELEASE_GUARD_OPENING_KEY,
            "release_guard_child_opening_key": RELEASE_GUARD_CHILD_OPENING_KEY,
            "capture_scorer_source_digest": attestation.scorer_source_digest,
            "capture_source_revision": attestation.source_revision,
            "capture_python_version": attestation.python_version,
            "capture_chess_version": attestation.chess_version,
        }
        record_bytes = _canonical_dumps(record) + b"\n"

        # --- private temps + self-check against the temp bytes + atomic publication ---
        # Capture is the PRODUCER of the committed provenance record, so it owns creating the
        # fixtures directory the record and its exclusive temp live in.
        try:
            provenance_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CapturePublicationError(
                f"cannot create the provenance directory {provenance_path.parent.name!r}: "
                f"{exc.strerror}."
            ) from None
        _log_stale_temps(resolved_output)
        _log_stale_temps(provenance_path)
        artifact_temp = _write_private_temp(resolved_output, artifact_bytes)
        record_temp = None
        try:
            record_temp = _write_private_temp(provenance_path, record_bytes)
            try:
                validate_capture_candidate(
                    artifact_bytes, record_bytes, graph=graph, roots=roots
                )
            except FrozenArtifactError as exc:
                raise CaptureSelfCheckError(
                    "the pre-publication self-check rejected the candidate bytes for "
                    f"{resolved_output.name!r}: {exc}"
                ) from exc
            except Exception as exc:
                # The self-check runs a FULL scoring pass, and score_overlay is a large
                # surface that can fail in ways the artifact vocabulary does not name (a
                # ValueError out of the grid config, a RuntimeError from a registry, a
                # KeyError on an unexpected overlay shape). Those are still SELF-CHECK
                # failures: nothing has been published, both temps are about to be unlinked,
                # and the operator's contract is a typed diagnostic + exit 1 rather than a
                # traceback out of a command that promised neither. Deliberately NOT
                # BaseException: KeyboardInterrupt/SystemExit must still unwind.
                raise CaptureSelfCheckError(
                    "the pre-publication self-check FAILED for "
                    f"{resolved_output.name!r} — {type(exc).__name__}: {exc}. This is a "
                    "scoring/runtime failure rather than a rejection of the candidate "
                    "bytes; nothing was published."
                ) from exc
            # Artifact-rename-before-record-rename: the reviewable, committable half never
            # lands before the artifact it describes exists.
            try:
                os.replace(artifact_temp, resolved_output)
            except OSError as exc:
                # BEFORE the first rename nothing has been published: the prior artifact and
                # the committed record are both untouched, and the finally-block unlinks both
                # temps. A clean typed refusal, with the errno text only.
                raise CapturePublicationError(
                    f"publishing the artifact to {resolved_output.name!r} failed: "
                    f"{exc.strerror}. Nothing was published — the previous artifact and the "
                    "committed provenance record are both untouched."
                ) from None
            artifact_temp = None
            try:
                os.replace(record_temp, provenance_path)
                record_temp = None
            except OSError as exc:
                raise CaptureInterRenameError(
                    f"the artifact was published to {resolved_output.name!r} but renaming the "
                    f"provenance record into {provenance_path.name} failed ({exc.strerror}); the pre-"
                    "existing artifact bytes are already gone and rolling back would be a "
                    "second unverified write. Rerun capture-cohort to republish a matched pair "
                    "— the load guard rejects the current mismatched pair on its SHA-256 check."
                ) from exc
        finally:
            # Best-effort unlink of any temp that did not become a final file.
            for tmp in (artifact_temp, record_temp):
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)

    return CaptureResult(
        artifact_path=resolved_output,
        artifact_sha256=artifact_sha256,
        pair_count=len(captured_inputs),
        quantile_count=len(quantile_pairs),
        release_guard_count=len(guard_pairs),
        attempts=attempts,
        orphan_replaced=orphan_replaced,
        snapshot_cache_epoch=snapshot_epoch,
        current_view_cache_epoch=post_epoch,
    )


@dataclass(frozen=True)
class WinnerBinding:
    """The DURABLE Phase-2 -> Phase-3 binding, assembled onto SelectionResult AFTER a
    winner is chosen (None iff no_ship). EVERY scoring surface Phase 3 must revalidate
    against the tree before any edit — binding to the config fingerprint alone would
    leave every other surface unproven at apply time. These are exactly the surfaces
    opening_score_inputs_fingerprint folds, plus the runtime + release-guard keys."""

    config_fingerprint: str
    scorer_contract_id: str
    scorer_source_digest: str
    # Travels WITH the digest so Phase 3 can see what the digest is worth: False means the
    # winner was scored without a pre-exec source check or without verified bytecode, i.e.
    # the digest is fenced over source bytes but not proven to name the code that RAN.
    # Phase 3 must not apply a winner carrying False: call require_preexec_verified_source.
    scorer_source_verified_preexec: bool
    provenance_record_sha256: str
    runtime_python: str
    runtime_chess_version: str
    source_revision: str | None
    source_dirty_paths: tuple[str, ...]
    model_version: str
    artifact_sha256: str
    graph_fingerprint: str
    roots_fingerprint: str
    evidence_derivation_fingerprint: str
    release_guard_opening_key: str
    release_guard_child_opening_key: str


def build_winner_binding(
    cohort: ScoredCalibrationCohort, winner_cell: GridCell
) -> WinnerBinding:
    """Assemble the winner_binding for the chosen ``winner_cell`` from the stamped cohort.
    Consumed by g-p4ih-selection AFTER a winner is chosen (it passes None on no_ship).
    ``config_fingerprint`` is the winner cell's fingerprint (KeyError — fail closed — if
    the winner is not a scored required cell)."""
    prov = cohort.provenance
    return WinnerBinding(
        config_fingerprint=cohort.config_fingerprints[winner_cell],
        scorer_contract_id=cohort.scorer_contract_id,
        scorer_source_digest=cohort.scorer_source_digest,
        scorer_source_verified_preexec=cohort.scorer_source_verified_preexec,
        provenance_record_sha256=cohort.provenance_record_sha256,
        runtime_python=cohort.runtime_python,
        runtime_chess_version=cohort.runtime_chess_version,
        source_revision=cohort.source_revision,
        source_dirty_paths=cohort.source_dirty_paths,
        model_version=cohort.model_version,
        artifact_sha256=prov.artifact_sha256,
        graph_fingerprint=prov.graph_fingerprint,
        roots_fingerprint=prov.roots_fingerprint,
        evidence_derivation_fingerprint=prov.evidence_derivation_fingerprint,
        release_guard_opening_key=prov.release_guard_opening_key,
        release_guard_child_opening_key=prov.release_guard_child_opening_key,
    )


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
    run_as_of = report.get("run_as_of")
    if run_as_of is not None:
        lines.append(f"Run clock (as_of): {run_as_of}")
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


def _utcnow() -> datetime:
    """The ONE live wall-clock sample point for the report path (clock path 3). Isolated
    so ``main`` samples it exactly once and a test can pin/advance it to prove
    single-sampling (one ``run_as_of`` across a multi-pair run)."""
    return datetime.now(timezone.utc)


def _positive_int(raw: str) -> int:
    """argparse type for a >= 1 count. argparse turns the ValueError into its own usage
    error (exit 2), which is the right shell contract for a malformed argument — the
    operation raises CaptureGovernanceError for the same value (exit 1) when called
    directly, so neither boundary depends on the other to be safe."""
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be >= 1, got {value}")
    return value


def _run_capture_cohort_child(argv: list[str], *, session_factory=None) -> int:
    """The MINIMAL capture-cohort child dispatch — g-p4ih-capture's ONLY CLI surface. The
    -S child (reached through capture_cohort.sh -> -I -S launcher -> -S child) recognizes
    the ``capture-cohort`` invocation, sources ``--output`` and the release-guard user from
    GHOSTREPLAY_RELEASE_GUARD_USER (NEVER a CLI argument: a production user id must not enter
    shell history or the process listing), calls ``capture_cohort(...)``, and maps its typed
    errors to exit codes + a REDACTED summary. It RETURNS before the legacy flat ``_parse_args``
    ever runs, so no live-report option can attach to a capture run. The polished subcommand
    surface (flat-parser -> subcommands, per-mode option sets, mutual exclusion) is
    g-p4ih-release-cli's, which SUBSUMES this branch."""
    parser = argparse.ArgumentParser(
        prog="calibrate_opening_scores_v2.py capture-cohort",
        description="Freeze + fence + publish a frozen-cohort artifact (g-p4ih-capture).",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Final artifact path; MUST resolve OUTSIDE the repository working tree.",
    )
    parser.add_argument(
        "--require-quiescent-epoch", action="store_true",
        help="Promote global-epoch movement to a retry trigger (explicit maintenance window).",
    )
    parser.add_argument(
        "--max-attempts", type=_positive_int, default=3,
        help="Fence attempts before giving up (>= 1). Rejected at the CLI boundary as well as "
             "in the operation, so the bad value never reaches the retry loop.",
    )
    args = parser.parse_args(argv)

    raw_user = os.environ.get("GHOSTREPLAY_RELEASE_GUARD_USER", "").strip()
    if not raw_user:
        print(
            "[capture] GHOSTREPLAY_RELEASE_GUARD_USER is required and supplied only via the "
            "environment (never a CLI argument: a production user id must not enter shell "
            "history or every process listing on the capture host).",
            file=sys.stderr,
        )
        return 2
    try:
        release_guard_user = int(raw_user)
    except ValueError:
        print("[capture] GHOSTREPLAY_RELEASE_GUARD_USER must be an integer user id.", file=sys.stderr)
        return 2

    if session_factory is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        session_factory = sessionmaker(
            bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
        )

    try:
        result = capture_cohort(
            session_factory=session_factory,
            output=args.output,
            release_guard_user=release_guard_user,
            require_quiescent_epoch=args.require_quiescent_epoch,
            max_attempts=args.max_attempts,
        )
    except CaptureError as exc:
        print(f"[capture] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    # Redacted summary: the artifact BASENAME only, never the full private path.
    print(
        f"[capture] published {result.artifact_path.name!r} sha256={result.artifact_sha256[:12]} "
        f"pairs={result.pair_count} quantile={result.quantile_count} "
        f"release_guard={result.release_guard_count} attempts={result.attempts} "
        f"orphan_replaced={result.orphan_replaced}",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None, *, session_factory=None):
    raw_argv = sys.argv[1:] if argv is None else argv
    # The minimal capture-cohort dispatch RETURNS before the flat live-report parser, so no
    # legacy option (--limit / --users / --pairs / ...) can attach to a capture run.
    if raw_argv and raw_argv[0] == "capture-cohort":
        return _run_capture_cohort_child(raw_argv[1:], session_factory=session_factory)
    args = _parse_args(raw_argv)

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

    # Clock path 3 (g-p4ih-replay-bind): sample the live report clock EXACTLY ONCE at
    # command start and thread it through every pair's grid, so pairs scored minutes
    # apart cannot see different wall clocks. (Paths 1/2 — release header / synthetic —
    # never reach here.) Printed in the report header below.
    run_as_of = _utcnow()

    with session_factory() as db:
        candidate_pairs = list_opening_score_candidate_pairs(db, limit=args.limit)
        selected = select_pairs(candidate_pairs, users=users, pairs=pairs)

        # Build each pair's overlay ONCE and score it for every required cell.
        pair_grids = [
            score_pair_grid(
                db, user_id, player_color, graph, roots, required_cells, as_of=run_as_of
            )
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
    report["run_as_of"] = run_as_of.astimezone(timezone.utc).isoformat()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return report


def _named_root_count(roots: OpeningRoots) -> int:
    from app.opening_rootcalc import _iter_named_roots

    return len(_iter_named_roots(roots))


if __name__ == "__main__":
    _result = main()
    # The capture-cohort child returns an int exit code the launcher must propagate; the
    # legacy report path returns a dict (exit 0).
    if isinstance(_result, int):
        sys.exit(_result)
