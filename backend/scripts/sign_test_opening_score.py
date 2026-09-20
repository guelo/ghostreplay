"""Sign test and normalisation probes for opening-score drill volatility.

Evidence tool for g-drill-score-swings / g-branch-ratio-norm / g-ratio-frontier-drag.
Reproduces every table recorded in those beads.

SYNTHETIC FIXTURES ONLY. This script builds its own book graph and evidence
overlay from hard-coded opening lines. It never reads the database, and no
production-derived score operand is queried or recorded.

The sign contract under test:

    a strong drill never LOWERS the pre-fold ratio of any ancestor row.

Usage (from backend/, venv active):

    python scripts/sign_test_opening_score.py sweep   # ramp sweep, depths 1-8
    python scripts/sign_test_opening_score.py mass    # perfect-mass weighting
    python scripts/sign_test_opening_score.py flip    # fail-then-strong admission
    python scripts/sign_test_opening_score.py mix     # TWO prepared children
    python scripts/sign_test_opening_score.py admit   # the admission step alone
    python scripts/sign_test_opening_score.py phase   # per-step + CUMULATIVE drag

Every mode accepts the scorer axes ``--branch-norm``, ``--branch-share`` and
``--coverage-fold``. ``mass``/``flip``/``mix``/``admit``/``phase`` always print the
``sums`` reference beside the selected configuration, so the bead's side-by-side
tables come out of a single run (the two columns coincide when the selected norm is
itself ``sums``).

Shape coverage matters as much as the axes. ``sweep``/``flip``/``mass`` branch only
at OPPONENT nodes, so no user node in them has two prepared children and the
``live_attempts + rho`` user weight mix is never exercised. ``mix``/``admit``/``phase``
are the shapes that do exercise it, and they are the only instruments for the
frontier-expansion drag owned by g-ratio-frontier-drag.

``sweep`` requires the ``depth_ramp_mode`` / ``depth_ramp_k`` fields on
RootCalcConfig. Those were implemented only to drive the sweep and then REVERTED, so
the sweep mode reports the ramp columns as unavailable until they are reinstated. The
``off`` row and every other mode run against the unmodified scorer.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import chess  # noqa: E402

from app.fen import active_color, normalize_fen  # noqa: E402
from app.opening_evidence import (  # noqa: E402
    EdgeEvidence,
    EvidenceOverlay,
    NodeEvidence,
)
from app.opening_graph import OpeningGraph, OpeningGraphNode  # noqa: E402
from app.opening_rootcalc import (  # noqa: E402
    BRANCH_NORM_MODES,
    COVERAGE_FOLD_MODES,
    OpeningCoverageValidationError,
    RootCalcConfig,
    _normalized,
    _SharedCalculator,
)
from app.opening_roots import OpeningRoot, OpeningRoots  # noqa: E402

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)
STRONG_QUALITY = 0.9
MATURE_SESSIONS = 40
FRESH_SESSIONS = 8

# Both lines open 1.e4 and diverge at Black's first move, so both branches hang
# off ONE opponent node -- the shape where a deep drill flips several user nodes
# at once, which is where the largest negative swings come from.
MATURE_SANS = [
    "e4", "c5", "Nf3", "d6", "d4", "cxd4", "Nxd4", "Nf6",
    "Nc3", "a6", "Be2", "e5", "Nb3", "Be7", "O-O", "O-O",
]
FRESH_SANS = [
    "e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6",
    "O-O", "Be7", "Re1", "b5", "Bb3", "d6", "c3", "O-O",
]


# --------------------------------------------------------------------------- axes


@dataclass(frozen=True)
class Axes:
    """The scorer axes this run probes, plus the ``sums`` reference it prints beside."""

    branch_norm: str = "sums"
    branch_share: float | None = None
    coverage_fold: str = "gate"

    @property
    def reference(self) -> "Axes":
        """The same run with the historical normalisation -- the comparison column."""
        return Axes(branch_norm="sums", coverage_fold=self.coverage_fold)

    @property
    def label(self) -> str:
        if self.branch_norm == "sums":
            return "sums"
        if self.branch_share is None:
            return "ratio"
        return f"ratio l={self.branch_share:.4g}"


AXES = Axes()


def _fen(board: chess.Board) -> str:
    return normalize_fen(board.fen())


def _path(sans: list[str]) -> tuple[list[str], list[str]]:
    board = chess.Board()
    fens = [_fen(board)]
    ucis: list[str] = []
    for san in sans:
        move = board.parse_san(san)
        ucis.append(move.uci())
        board.push(move)
        fens.append(_fen(board))
    return fens, ucis


def _build_graph(*lines: list[str]) -> OpeningGraph:
    nodes: dict[str, OpeningGraphNode] = {}
    root = _fen(chess.Board())
    for sans in lines:
        board = chess.Board()
        parent = _fen(board)
        nodes.setdefault(parent, OpeningGraphNode(parent, active_color(parent)))
        for san in sans:
            move = board.parse_san(san)
            uci = move.uci()
            board.push(move)
            child = _fen(board)
            nodes.setdefault(child, OpeningGraphNode(child, active_color(child)))
            nodes[parent].children[uci] = child
            nodes[child].parents.add((parent, uci))
            parent = child
    return OpeningGraph(nodes, root)


def _lay(
    overlay: EvidenceOverlay,
    sans: list[str],
    sessions: int,
    tag: str,
    quality: float = STRONG_QUALITY,
) -> None:
    """Drill one line ``sessions`` times at ``quality``, user plies only.

    Seeds BOTH edge directions' traversal counts, and ``live_attempts``/
    ``live_passes`` on the user->opponent edges -- the ones ``_prepared_children``
    keys on, and therefore the only ones that admit a child into the weight set.
    """
    if sessions <= 0:
        return
    fens, ucis = _path(sans)
    for ply, uci in enumerate(ucis):
        parent, child = fens[ply], fens[ply + 1]
        edge = overlay.edges.setdefault(
            (parent, child), EdgeEvidence(parent, child, uci)
        )
        edge.traversal_count += sessions
        if active_color(parent) != "white":
            continue
        edge.live_attempts += sessions
        edge.live_passes += sessions
        node = overlay.nodes.setdefault(parent, NodeEvidence(parent))
        node.live_attempts += sessions
        node.live_passes += sessions
        node.quality_sum += quality * sessions
        node.quality_count += sessions
        node.session_ids.update(f"{tag}-{i}" for i in range(sessions))
        node.last_live_at = NOW - timedelta(days=1)


def _build_overlay(specs: list[tuple[list[str], int, str]]) -> EvidenceOverlay:
    overlay = EvidenceOverlay(1, "white")
    for sans, sessions, tag in specs:
        _lay(overlay, sans, sessions, tag)
    return overlay


def _overlay(
    mature_sans: list[str],
    fresh_sans: list[str],
    fresh_sessions: int,
    mature_sessions: int = MATURE_SESSIONS,
) -> EvidenceOverlay:
    return _build_overlay(
        [
            (mature_sans, mature_sessions, "mature"),
            (fresh_sans, fresh_sessions, "fresh"),
        ]
    )


def _config(mode: str = "off", k: float = 0.0, *, axes: Axes | None = None) -> RootCalcConfig:
    """Served sm-v2-6 axes with the report fold held OFF (pre-fold ratio)."""
    axes = AXES if axes is None else axes
    values: dict[str, object] = dict(
        lcb_z=1.0,
        coverage_fold=axes.coverage_fold,
        coverage_live_threshold=1,
        report_fold_p=0.0,
        report_fold_scope="user",
        branch_norm=axes.branch_norm,
    )
    # branch_share is INERT under "sums" and RootCalcConfig rejects it there, so it
    # is spelled only on the ratio arm. None means the inherited gamma/(1+gamma).
    if axes.branch_norm == "ratio" and axes.branch_share is not None:
        values["branch_share"] = axes.branch_share
    if mode != "off":
        values["depth_ramp_mode"] = mode
        values["depth_ramp_k"] = k
    return RootCalcConfig(**values)


def _prefold_rows(
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    ancestors: list[str],
    config: RootCalcConfig,
) -> dict[str, float]:
    calc = _SharedCalculator("white", graph, overlay, roots, config, NOW)
    out: dict[str, float] = {}
    for key in ancestors:
        norm = _normalized(key)
        natural = calc._calc(norm, False)[0]
        perfect = calc._calc(norm, True)[0]
        out[key] = (100.0 * natural / perfect) if perfect > 0 else 0.0
    return out


def _perfect_mass(
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    key: str,
    config: RootCalcConfig,
) -> float:
    calc = _SharedCalculator("white", graph, overlay, roots, config, NOW)
    return calc._calc(_normalized(key), True)[0]


def _roots_for(ancestors: list[str]) -> OpeningRoots:
    return OpeningRoots(
        {
            key: OpeningRoot(key, f"row{i}", "fam", None, 0, frozenset(), frozenset())
            for i, key in enumerate(ancestors)
        },
        {key: frozenset([key]) for key in ancestors},
    )


# ---------------------------------------------------------------------- sweep/mass


def run_sweep(max_depth: int = 8, max_drills: int = 16) -> None:
    candidates: list[tuple[str, float]] = [("off", 0.0)]
    candidates += [("exp", k) for k in (1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 50.0)]
    candidates += [("linear", k) for k in (4.0, 8.0, 12.0)]

    print(f"\nnormalisation: {AXES.label}   coverage_fold: {AXES.coverage_fold}")
    for label, mature in (
        ("mature sibling DEEP", MATURE_SANS),
        ("mature sibling SHALLOW (2 plies)", MATURE_SANS[:4]),
    ):
        print(f"\n{label} -- depths 1-{max_depth}, drills 1-{max_drills}")
        print(f"{'ramp':<14} {'violations':>10} {'worst':>9}  where")
        print("-" * 52)
        for mode, k in candidates:
            try:
                config = _config(mode, k)
            except TypeError:
                print(f"{mode + f' k={k:g}':<14} {'unavailable (ramp reverted)':>10}")
                continue
            violations = 0
            worst = 0.0
            worst_at = ""
            for depth in range(1, max_depth + 1):
                fresh = FRESH_SANS[: 2 * depth]
                graph = _build_graph(mature, fresh)
                mfens, _ = _path(mature)
                ffens, _ = _path(fresh)
                ancestors = [mfens[0], mfens[1], ffens[2]]
                roots = _roots_for(ancestors)
                try:
                    prev = _prefold_rows(
                        graph, _overlay(mature, fresh, 0), roots, ancestors, config
                    )
                    for n in range(1, max_drills + 1):
                        cur = _prefold_rows(
                            graph, _overlay(mature, fresh, n), roots, ancestors, config
                        )
                        for key in ancestors:
                            delta = cur[key] - prev[key]
                            if delta < -1e-9:
                                violations += 1
                                if delta < worst:
                                    worst = delta
                                    worst_at = f"depth={depth} drill={n}"
                        prev = cur
                except OpeningCoverageValidationError:
                    continue
            name = mode + (f" k={k:g}" if mode != "off" else "")
            print(f"{name:<14} {violations:>10} {worst:>9.3f}  {worst_at}")


def run_mass(max_depth: int = 8) -> None:
    """Play quality held fixed; only the fresh branch's DEPTH varies.

    The mature branch is drilled ``MATURE_SESSIONS`` (40) times, the fresh branch
    ``FRESH_SESSIONS`` (8). Under equal reply importance the root would not fall as
    the fresh branch deepens. Under "sums" it does, because the root score is a ratio
    of SUMS, so a reply's influence tracks its perfect MASS rather than its weight.
    """
    reference = _config(axes=AXES.reference)
    config = _config()
    print(
        f"\nmature branch drilled {MATURE_SESSIONS}x, fresh branch {FRESH_SESSIONS}x, "
        "both strongly; only fresh-branch DEPTH varies"
    )
    print(f"normalisation: {AXES.label}   coverage_fold: {AXES.coverage_fold}")
    print(
        f"\n{'fresh depth':>12} {'sums: root':>12} {'sums: mass':>12} "
        f"{AXES.label + ': root':>18} {AXES.label + ': mass':>18}"
    )
    print("-" * 76)
    for depth in range(1, max_depth + 1):
        fresh = FRESH_SANS[: 2 * depth]
        graph = _build_graph(MATURE_SANS, fresh)
        mfens, _ = _path(MATURE_SANS)
        ffens, _ = _path(fresh)
        root = mfens[1]  # after 1.e4 -- the opponent node holding both replies
        head = ffens[2]
        roots = _roots_for([root, head])
        overlay = _overlay(MATURE_SANS, fresh, FRESH_SESSIONS)
        cells: list[str] = []
        for cfg in (reference, config):
            try:
                rows = _prefold_rows(graph, overlay, roots, [root], cfg)
                mass = _perfect_mass(graph, overlay, roots, head, cfg)
                cells.append(f"{rows[root]:>12.3f}")
                cells.append(f"{mass:>12.4f}")
            except OpeningCoverageValidationError:
                cells.extend([f"{'coverage-invalid':>12}", f"{'-':>12}"])
        print(f"{depth:>12} {cells[0]} {cells[1]} {cells[2]:>18} {cells[3]:>18}")


# ----------------------------------------------------------------------------- flip

# Alternative BLACK replies at the opponent node DIRECTLY BELOW the fresh branch
# head (after 1.e4 e5 Nf3). These are what the head's subtree score averages over,
# so they -- not replies higher up -- set how much one admitted child moves it.
SIBLING_SANS = [
    ["e4", "e5", "Nf3", "Nf6", "Nxe5", "d6"],
    ["e4", "e5", "Nf3", "d6", "d4", "exd4"],
    ["e4", "e5", "Nf3", "f5", "Nxe5", "Qf6"],
    ["e4", "e5", "Nf3", "Qe7", "Bc4", "Nf6"],
]

POOR_QUALITY = 0.2
FAIL_QUALITIES = (0.2, 0.5, 0.7)


def _flip_overlay(
    fresh: list[str],
    siblings: list[list[str]],
    sibling_sessions: int,
    continuation_sessions: int,
    fail_quality: float,
    second: bool,
) -> EvidenceOverlay:
    """The fail-then-strong overlay at the fresh branch head.

    A failed first visit leaves ``live_attempts = 1, live_passes = 0``, which does
    NOT admit the child. A strong second attempt takes ``live_attempts`` to 2 and
    admits it through the ``>= 2`` arm.

    ``continuation_sessions`` drills the fresh line BELOW the head. It is the axis
    that makes the admitted subtree's perfect MASS exceed 1: with the continuation
    unprepared, every node under the head is a user leaf, so P(admitted) == 1 at
    EVERY depth, sums' denominator is already (1 + gamma) and the two normalisations
    coincide algebraically. Depth alone therefore separates nothing.

    The head's own evidence is ASSIGNED (not accumulated) afterwards, session ids
    included, so the flip stays a clean 1 -> 2 attempt however the line was drilled.
    """
    ffens, fucis = _path(fresh)
    head = ffens[2]
    ov = _overlay(MATURE_SANS, fresh, continuation_sessions)
    for i, sib in enumerate(siblings):
        _lay(ov, sib, sibling_sessions, f"sib{i}")
    node = ov.nodes.setdefault(head, NodeEvidence(head))
    node.live_attempts = 2 if second else 1
    node.live_passes = 1 if second else 0
    node.live_fails = 1
    node.quality_sum = fail_quality + (STRONG_QUALITY if second else 0.0)
    node.quality_count = 2 if second else 1
    node.session_ids.clear()
    node.session_ids.update({"f-0"} | ({"f-1"} if second else set()))
    node.last_live_at = NOW - timedelta(days=1)
    child = ffens[3]
    edge = ov.edges.setdefault((head, child), EdgeEvidence(head, child, fucis[2]))
    edge.traversal_count = 2 if second else 1
    edge.live_attempts = 2 if second else 1
    edge.live_passes = 1 if second else 0
    return ov


CONTINUATION_SESSIONS = 8


def _flip_points(
    fail_quality: float,
    continuation_sessions: int,
    max_depth: int,
    axes: Axes | None = None,
) -> list[dict]:
    """One grid point per (sibling arm, replies, depth), both normalisations measured.

    ``replies`` is how many book replies sit at the opponent node below the head:
    the fresh line's own continuation plus ``extra`` siblings. At ``replies == 1``
    there are no siblings, so the sibling-drilling arm is vacuous there and the point
    is labelled ``"none"`` rather than double-counted into both arms -- the
    distinction the "drilled siblings keep every ancestor delta positive" claim
    depends on.
    """
    axes = AXES if axes is None else axes
    reference = _config(axes=axes.reference)
    config = _config(axes=axes)
    points: list[dict] = []
    for extra in range(0, len(SIBLING_SANS) + 1):
        arms = (("none", 0),) if extra == 0 else (
            ("drilled 40x", MATURE_SESSIONS),
            ("undrilled", 0),
        )
        for arm, sibling_sessions in arms:
            for depth in range(2, max_depth + 1):
                fresh = FRESH_SANS[: 2 * depth]
                siblings = SIBLING_SANS[:extra]
                graph = _build_graph(MATURE_SANS, fresh, *siblings)
                mfens, _ = _path(MATURE_SANS)
                ffens, _ = _path(fresh)
                head = ffens[2]
                admitted = ffens[3]
                ancestors = [mfens[0], mfens[1], head]
                roots = _roots_for(ancestors + [admitted])
                before_ov = _flip_overlay(
                    fresh, siblings, sibling_sessions, continuation_sessions,
                    fail_quality, False,
                )
                after_ov = _flip_overlay(
                    fresh, siblings, sibling_sessions, continuation_sessions,
                    fail_quality, True,
                )
                try:
                    deltas = {}
                    for tag, cfg in (("sums", reference), ("sel", config)):
                        before = _prefold_rows(graph, before_ov, roots, ancestors, cfg)
                        after = _prefold_rows(graph, after_ov, roots, ancestors, cfg)
                        deltas[tag] = [after[k] - before[k] for k in ancestors]
                    # The PERFECT MASS of the admitted subtree, read under "sums" --
                    # under "ratio" it is 1.0 by construction and identifies nothing.
                    mass = _perfect_mass(graph, after_ov, roots, admitted, reference)
                except OpeningCoverageValidationError:
                    continue
                points.append(
                    {
                        "arm": arm,
                        "replies": extra + 1,
                        "depth": depth,
                        "mass": mass,
                        "sums": deltas["sums"],
                        "sel": deltas["sel"],
                    }
                )
    return points


ROW_NAMES = ("initial", "after 1.e4", "branch head")


def run_flip(max_depth: int = 5) -> None:
    """The one flip where the node ALREADY has a positive score, swept on THREE axes.

    Swept on fail QUALITY (0.2 ~ a 160cp blunder; 0.5/0.7 ~ a marginal 50-70cp fail
    under PASS_THRESHOLD=50, TAU_CP=100), on SIBLING DRILLING, and on whether the
    admitted CONTINUATION is prepared. All three matter:

    - with drilled siblings the ratio arm's ancestor deltas are positive at every
      fail quality, so the undrilled arm is the only shape that exercises the floor;
    - with the continuation unprepared the admitted subtree's perfect mass is 1 at
      every depth, where the two normalisations coincide ALGEBRAICALLY.

    The flipping (head) row is reported apart from the ancestor rows -- they are not
    one story -- and every point carries the admitted subtree's perfect mass, so the
    equality case is identifiable rather than inferred from a depth literal.
    """
    print(f"\nnormalisation: {AXES.label}   coverage_fold: {AXES.coverage_fold}")
    print("delta = change in a row's pre-fold ratio at the flip")
    print(
        "P(admit) = the admitted subtree's PERFECT MASS under sums; "
        "P == 1 is the algebraic-equality case.\n"
    )
    sel = AXES.label
    for cont_label, cont_sessions in (
        ("continuation unprepared", 0),
        (f"continuation prepared ({CONTINUATION_SESSIONS}x)", CONTINUATION_SESSIONS),
    ):
        for fail_quality in FAIL_QUALITIES:
            points = _flip_points(fail_quality, cont_sessions, max_depth)
            head = f"{cont_label}, fail quality {fail_quality:g}"
            print(f"\n=== {head}")
            print("--- ANCESTOR rows")
            print(
                f"{'siblings':<12} {'replies':>8} {'depth':>6} {'row':>12} "
                f"{'P(admit)':>9} {'sums d':>9} {sel + ' d':>14}"
            )
            for point in points:
                for idx in (0, 1):
                    print(
                        f"{point['arm']:<12} {point['replies']:>8} {point['depth']:>6} "
                        f"{ROW_NAMES[idx]:>12} {point['mass']:>9.4f} "
                        f"{point['sums'][idx]:>9.3f} {point['sel'][idx]:>14.3f}"
                    )
            print("--- FLIPPING (head) row")
            print(
                f"{'siblings':<12} {'replies':>8} {'depth':>6} {'P(admit)':>9} "
                f"{'sums d':>9} {sel + ' d':>14}"
            )
            for point in points:
                print(
                    f"{point['arm']:<12} {point['replies']:>8} {point['depth']:>6} "
                    f"{point['mass']:>9.4f} {point['sums'][2]:>9.3f} "
                    f"{point['sel'][2]:>14.3f}"
                )
            _flip_summary(points, sel)


def _flip_summary(points: list[dict], sel: str) -> None:
    def worst(subset: list[dict], tag: str, idx: slice | int) -> float | None:
        values = [
            value
            for point in subset
            for value in (
                point[tag][idx] if isinstance(idx, slice) else [point[tag][idx]]
            )
        ]
        return min(values) if values else None

    for arm_label, subset in (
        ("drilled siblings", [p for p in points if p["arm"] == "drilled 40x"]),
        ("undrilled siblings", [p for p in points if p["arm"] == "undrilled"]),
        ("no siblings (replies=1)", [p for p in points if p["arm"] == "none"]),
    ):
        if not subset:
            continue
        print(
            f"  {arm_label:<24} worst ancestor: sums {worst(subset, 'sums', slice(0, 2)):+7.3f}"
            f"   {sel} {worst(subset, 'sel', slice(0, 2)):+7.3f}"
            f"   | worst head: sums {worst(subset, 'sums', 2):+7.3f}"
            f"   {sel} {worst(subset, 'sel', 2):+7.3f}"
        )
    equal = [p for p in points if abs(p["sel"][2] - p["sums"][2]) < 1e-9]
    unequal = [p for p in points if abs(p["sel"][2] - p["sums"][2]) >= 1e-9]
    print(
        f"  head equality: {len(equal)}/{len(points)} points; "
        f"all have P == 1: {all(abs(p['mass'] - 1.0) < 1e-9 for p in equal)}; "
        f"every unequal point has P > 1: "
        f"{all(p['mass'] > 1.0 + 1e-9 for p in unequal)}; "
        f"{sel} >= sums on every head row: "
        f"{all(p['sel'][2] >= p['sums'][2] - 1e-9 for p in points)}"
    )
    print(
        f"  {sel} >= sums on every ancestor row: "
        f"{all(p['sel'][i] >= p['sums'][i] - 1e-9 for p in points for i in (0, 1))}"
    )


# --------------------------------------------------------------- mix / admit / phase
#
# The shapes sweep/flip/mass structurally CANNOT produce: a user node with TWO
# prepared children. _build_graph lays paths and SIBLING_SANS adds opponent replies,
# so in every other mode each user node has at most one prepared child and the
# live_attempts + rho user weight mix is never exercised. These modes are also the
# only instrument for the frontier-expansion drag: the cohort capture is one frozen
# snapshot and measures EXPOSURE, never the decline along a drilling path.

# The established continuation off 1.e4 e5, drilled MATURE_SESSIONS times. MATURE_SANS
# (1.e4 c5) rides alongside so the root rows have a sibling branch carrying real mass.
MIX_BASE_SANS = ["e4", "e5", "Nf3", "Nc6", "Bb5", "a6", "Ba4", "Nf6"]

# The SECOND move admitted at the 1.e4 e5 user node. The GRAPH always carries this
# line in full -- that is what makes the admitted subtree UNPREPARED rather than
# absent. A book line that simply stops is an opponent LEAF, which the scorer credits
# at 1.0, so a fixture whose new move ends the book measures the opposite of the
# frontier drag.
MIX_NEW_SANS = [
    "e4", "e5", "Bc4", "Nf6", "d3", "Bc5", "Nf3", "d6", "O-O", "Be6",
]

# How many PLIES of MIX_NEW_SANS the overlay drills; everything deeper stays in the
# book and unprepared. "new move only" prepares the single move Bc4; the 4-user-ply
# shape prepares Bc4/d3/Nf3/O-O and so pushes the prepared frontier four plies down.
# Each shape is (plies of MIX_NEW_SANS the overlay drills, plies of MIX_NEW_SANS the
# GRAPH carries). "book ends" is the negative control: it truncates the BOOK to the
# drilled move, so the admitted child is an opponent LEAF credited at 1.0 and the
# shape measures the opposite of the drag. It exists so a fixture that accidentally
# ends the book is recognisable rather than reassuring.
MIX_SHAPES: dict[str, tuple[int, int]] = {
    "new move only": (3, len(MIX_NEW_SANS)),
    "new line (4 user plies)": (9, len(MIX_NEW_SANS)),
    "new move, book ends": (3, 3),
}

# Row index into the 1.e4 e5 path: 0 = initial, 1 = the opponent node after 1.e4,
# 2 = the MIX node itself (white to move, two prepared children).
MIX_ROWS: tuple[tuple[str, int], ...] = (
    ("initial", 0),
    ("after 1.e4", 1),
    ("mix node (1.e4 e5)", 2),
)

MAX_MIX_DRILLS = 16


def _mix_series(
    shape: tuple[int, int], config: RootCalcConfig, max_drills: int
) -> dict[str, list[float]] | None:
    """Row series over drills 0..max_drills of the newly admitted line.

    Index 0 is the pre-admission state: the new line's user->opponent edge has no
    live evidence, so _prepared_children leaves it OUT of the weight set entirely and
    the mix node is a single-child user node. Index 1 is the ADMISSION step; every
    later index is the user drilling the admitted line CORRECTLY, at STRONG_QUALITY.
    """
    drilled_plies, book_plies = shape
    graph = _build_graph(MATURE_SANS, MIX_BASE_SANS, MIX_NEW_SANS[:book_plies])
    bfens, _ = _path(MIX_BASE_SANS)
    rows = [bfens[idx] for _, idx in MIX_ROWS]
    roots = _roots_for(rows)
    series: dict[str, list[float]] = {key: [] for key in rows}
    for n in range(0, max_drills + 1):
        overlay = _build_overlay(
            [
                (MATURE_SANS, MATURE_SESSIONS, "mature"),
                (MIX_BASE_SANS, MATURE_SESSIONS, "base"),
                (MIX_NEW_SANS[:drilled_plies], n, "new"),
            ]
        )
        try:
            values = _prefold_rows(graph, overlay, roots, rows, config)
        except OpeningCoverageValidationError:
            return None
        for key in rows:
            series[key].append(values[key])
    return series


def _mix_table(max_drills: int, admission_only: bool) -> None:
    reference = _config(axes=AXES.reference)
    config = _config()
    print(f"\nnormalisation: {AXES.label}   coverage_fold: {AXES.coverage_fold}")
    header = (
        f"\n{'shape':<24} {'row':<20} {'admit step':>11} {'cumulative':>11} "
        f"{'negative':>9}   norm"
    )
    if admission_only:
        header = f"\n{'shape':<24} {'row':<20} {'admit step':>11}   norm"
    print(header)
    print("-" * (52 if admission_only else 82))
    bfens, _ = _path(MIX_BASE_SANS)
    for shape, spec in MIX_SHAPES.items():
        for tag, cfg in (("sums", reference), (AXES.label, config)):
            series = _mix_series(spec, cfg, max_drills)
            if series is None:
                print(f"{shape:<24} {'-':<20} coverage-invalid   {tag}")
                continue
            for name, idx in MIX_ROWS:
                values = series[bfens[idx]]
                admit = values[1] - values[0]
                if admission_only:
                    print(f"{shape:<24} {name:<20} {admit:>11.3f}   {tag}")
                    continue
                cumulative = values[max_drills] - values[1]
                negative = sum(
                    1
                    for n in range(2, max_drills + 1)
                    if values[n] - values[n - 1] < -1e-9
                )
                print(
                    f"{shape:<24} {name:<20} {admit:>11.3f} {cumulative:>11.3f} "
                    f"{negative:>4}/{max_drills - 1:<4}   {tag}"
                )
        print()


def run_mix(max_drills: int = MAX_MIX_DRILLS) -> None:
    """A user node with TWO prepared children, drilled independently.

    ``admit step`` is the 0 -> 1 admission of the second child. ``cumulative`` is the
    row's total movement over drills 1..max_drills -- the user drilling the newly
    admitted line CORRECTLY the whole way -- and ``negative`` counts how many of those
    steps LOWERED the row. Under "ratio" the decline is not a one-step admission
    charge: every prepared ply lengthens the leaf-credit yardstick again while the
    perfect pass stays pinned at 1.0, so it continues for as long as the prepared
    frontier expands. That residual is owned by g-ratio-frontier-drag.
    """
    _mix_table(max_drills, admission_only=False)


def run_admit(max_drills: int = MAX_MIX_DRILLS) -> None:
    """The admission step ALONE: a well-drilled node admits a second child.

    A test pinning only this step would let the sustained drift through -- see
    :func:`run_phase` for the instrument that measures it.
    """
    _mix_table(max_drills, admission_only=True)


def run_phase(max_drills: int = MAX_MIX_DRILLS) -> None:
    """Per-step AND cumulative decline while the newly admitted line is drilled.

    This is the only instrument for the frontier-expansion drag, and the source of
    the known-limit band pinned by test_opening_rootcalc's cumulative-drag test. The
    band is written FROM this mode's output, never guessed: the per-step contribution
    is bounded by lambda * w_new, but the cumulative path is not.
    """
    reference = _config(axes=AXES.reference)
    config = _config()
    print(f"\nnormalisation: {AXES.label}   coverage_fold: {AXES.coverage_fold}")
    bfens, _ = _path(MIX_BASE_SANS)
    for shape, spec in MIX_SHAPES.items():
        sums_series = _mix_series(spec, reference, max_drills)
        sel_series = _mix_series(spec, config, max_drills)
        if sums_series is None or sel_series is None:
            print(f"\n{shape}: coverage-invalid")
            continue
        for name, idx in MIX_ROWS:
            sums = sums_series[bfens[idx]]
            sel = sel_series[bfens[idx]]
            print(f"\n{shape} -- row {name}")
            print(
                f"{'drills':>7} {'sums':>9} {'sums d':>9} "
                f"{AXES.label:>12} {AXES.label + ' d':>14}"
            )
            print("-" * 56)
            for n in range(0, max_drills + 1):
                ds = "" if n == 0 else f"{sums[n] - sums[n - 1]:>9.3f}"
                dr = "" if n == 0 else f"{sel[n] - sel[n - 1]:>14.3f}"
                print(f"{n:>7} {sums[n]:>9.3f} {ds:>9} {sel[n]:>12.3f} {dr:>14}")
            neg = sum(
                1
                for n in range(2, max_drills + 1)
                if sel[n] - sel[n - 1] < -1e-9
            )
            print(
                f"  admission (0->1):   sums {sums[1] - sums[0]:+.3f}   "
                f"{AXES.label} {sel[1] - sel[0]:+.3f}"
            )
            print(
                f"  CUMULATIVE (1->{max_drills}): sums {sums[max_drills] - sums[1]:+.3f} "
                f"({sums[1]:.3f} -> {sums[max_drills]:.3f})   "
                f"{AXES.label} {sel[max_drills] - sel[1]:+.3f} "
                f"({sel[1]:.3f} -> {sel[max_drills]:.3f})"
            )
            print(f"  negative steps under {AXES.label}: {neg}/{max_drills - 1}")


MODES = {
    "sweep": run_sweep,
    "mass": run_mass,
    "flip": run_flip,
    "mix": run_mix,
    "admit": run_admit,
    "phase": run_phase,
}


def main(argv: list[str] | None = None) -> None:
    global AXES
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=sorted(MODES), nargs="?", default="sweep")
    parser.add_argument(
        "--branch-norm",
        choices=sorted(BRANCH_NORM_MODES),
        default="sums",
        help="score-channel normalisation (default: the served 'sums')",
    )
    parser.add_argument(
        "--branch-share",
        type=float,
        default=None,
        help=(
            "lambda, the continuation share under --branch-norm ratio; omit for the "
            "inherited gamma/(1+gamma)"
        ),
    )
    parser.add_argument(
        "--coverage-fold",
        choices=sorted(COVERAGE_FOLD_MODES),
        default="gate",
        help="readiness gate at opponent nodes ('off' is the ceiling of any C3 design)",
    )
    args = parser.parse_args(argv)
    if args.branch_share is not None and args.branch_norm != "ratio":
        parser.error("--branch-share is inert under --branch-norm sums")
    AXES = Axes(
        branch_norm=args.branch_norm,
        branch_share=args.branch_share,
        coverage_fold=args.coverage_fold,
    )
    MODES[args.mode]()


if __name__ == "__main__":
    main()
