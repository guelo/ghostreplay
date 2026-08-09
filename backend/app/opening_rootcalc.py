from __future__ import annotations

import hashlib
import math
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from typing import Literal

from app.fen import active_color, normalize_fen
from app.game_phase import is_middlegame_position
from app.opening_evidence import EvidenceOverlay, observed_off_book_fens
from app.opening_graph import OpeningGraph
from app.opening_roots import OpeningRoot, OpeningRoots


# Standard chess initial position (normalized 4-field FEN). This is the graph
# root_fen and the key under which the synthetic whole-repertoire hero row is
# persisted/served.
SYNTHETIC_INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
SYNTHETIC_ROOT_NAME = "Repertoire"
SYNTHETIC_ROOT_FAMILY = "__repertoire__"

# Recognised RootCalcConfig.coverage_fold modes (g-zc3p Part 2). "off" is the
# ungated comparison mode; "gate" is the calibrated 0/1 local-coverage gate
# alone at opponent nodes; "gate_x_cov" additionally multiplies by the child
# coverage channel (the double-counting arm kept only for the calibration grid).
COVERAGE_FOLD_MODES = frozenset({"off", "gate", "gate_x_cov"})

# Report-fold configuration surface (g-report-cfg-fp, Phase 1a.1). The identity
# values (report_fold_p=0.0, report_fold_scope="all", report_self_term="keep")
# remain available for historical comparison and keep their pre-Phase-1
# fingerprint (see root_calc_config_fingerprint). The served sm-v2-4 default
# activates the user-turn-only square-root fold while retaining the self-term.
#
#   - report_fold_scope: which turns the report-time coverage fold applies to.
#     "all" folds both turns; "user" folds only user-turn rows (g-report-fold-score).
#     Inert while report_fold_p == 0 (the fold is off, so scope selects nothing).
#   - report_self_term: the orthogonal pre-fold quality choice. "keep" is today's
#     behaviour; "drop_user" is the B1 self-term arm (g-drop-user-score), which
#     applies even at report_fold_p == 0.
REPORT_FOLD_SCOPES = frozenset({"all", "user"})
REPORT_SELF_TERM_MODES = frozenset({"keep", "drop_user"})

# Weighted coverage is mathematically bounded to [0, 1], but repeated float
# normalization/accumulation can land a fully covered row a few ULP outside that
# interval. Accept only representational drift at the report-fold boundary; values
# beyond this tolerance still fail closed before a fractional power is evaluated.
_REPORT_FOLD_COVERAGE_EPSILON = 1e-9

# Effective report-time self-term actually applied to a REPORTED row, distinct
# from the RootCalcConfig.report_self_term *request* vocabulary (the two-value
# REPORT_SELF_TERM_MODES above). It is the truthful debug/API observation of which
# arm scored the row, not a config knob:
#   - "keep":          the ordinary aggregate node ratio scored the row — either
#                      report_self_term="keep", or a drop_user OPPONENT-turn row
#                      (whose self-term is the opponent's, not the user's).
#   - "drop_user":     the child-only ratio actually fired (a qualifying user-turn
#                      row under drop_user: nonempty prepared children, positive
#                      child perfect denominator).
#   - "keep_fallback": a drop_user USER-turn row that could not take the child-only
#                      ratio — a user leaf, an empty prepared-child set, or a
#                      non-positive child perfect denominator — so it fell back to
#                      the ordinary node ratio. Numerically identical to "keep", but
#                      reported distinctly so the fallback is visible.
ReportSelfTermEffective = Literal["keep", "drop_user", "keep_fallback"]

# Report-scorer contract id. A report-scorer semantic change that is NOT already
# captured by a RootCalcConfig field (config changes move
# root_calc_config_fingerprint on their own) MUST bump BOTH this id and
# SCORE_MODEL_VERSION (app.opening_cache) to force a full recompute. Adding the
# report-stage behavior above is config-captured, so this stays at v1.
REPORT_SCORER_CONTRACT_ID = "report-fold-v1"


@dataclass
class CalcTelemetry:
    """Optional calibration out-param for :func:`compute_all_root_scores`.

    Mutated in place; never read by scoring, so it has no behavioural effect. The
    recursion-bound proof relies on reporting key classes separately: ``_metrics``
    is keyed ``(fen, perfect)`` (see :class:`_SharedCalculator`), so the natural and
    perfect passes are counted apart rather than conflated into one ~2x number.

    - ``named_root_count``: named roots in the registry snapshot.
    - ``actual_key_count`` / ``perfect_key_count``: distinct memoized ``_calc``
      keys for the natural (``perfect=False``) and idealized (``perfect=True``)
      passes; each scales with unique reachable normalized FENs, not named roots.
    - ``calculation_misses``: total memo misses across both passes.
    - ``raw_middlegame_root_count``: named roots whose own board satisfies
      ``is_middlegame_position``. This does **not** imply the root is unscored: a
      raw-middlegame board can still have a scored subtree via observed off-book
      moves (see ``_structural_children``).
    - ``unscored_root_count``: named roots absent from the scored result set
      (no reachable quality observation). Reported separately from the raw
      middlegame count and never conflated with it.
    """

    named_root_count: int = 0
    actual_key_count: int = 0
    perfect_key_count: int = 0
    calculation_misses: int = 0
    raw_middlegame_root_count: int = 0
    unscored_root_count: int = 0


@dataclass(frozen=True)
class RootCalcConfig:
    alpha: float = 1.0
    beta: float = 2.0
    rho: float = 1.0
    gamma: float = 0.8
    lambda_review: float = 0.5
    k_evidence: float = 5.0
    half_life_days: float = 45.0
    coverage_live_threshold: int = 1
    # Readiness folds (g-zc3p/g-xnv7). The calibrated defaults make the displayed
    # score read as an earned readiness number.
    #   - lcb_z: strictness of the lower-confidence bound on mastery. 1.0 is the
    #     calibrated mild LCB; 0.0 reproduces the plain Beta-posterior mean.
    #   - coverage_fold: how the opponent-branch score credit is gated by local
    #     coverage. "off" == ungated comparison mode; "gate" == the 0/1 gate alone;
    #     "gate_x_cov" == the double-counting gate*child_cov arm kept only so the
    #     grid can confirm the double-count empirically (see _calc).
    lcb_z: float = 1.0
    coverage_fold: str = "gate"
    # Report-stage axes (see REPORT_FOLD_SCOPES / REPORT_SELF_TERM_MODES).
    # sm-v2-4 folds only user-turn reported rows by sqrt(coverage), preserving
    # opponent-turn rows already governed by the recursive coverage gate. The
    # historical p=0 identity remains fingerprint-compatible with sm-v2-3.
    report_fold_p: float = 0.5
    report_fold_scope: str = "user"
    report_self_term: str = "keep"

    def __post_init__(self) -> None:
        # Fail fast on a bad mode rather than letting the _calc opponent branch
        # silently treat an unknown value as "gate": the CLI validates its grid, but
        # any direct caller (e.g. a later bead flipping the default) must too.
        if self.coverage_fold not in COVERAGE_FOLD_MODES:
            raise ValueError(
                f"coverage_fold must be one of {sorted(COVERAGE_FOLD_MODES)}; "
                f"got {self.coverage_fold!r}"
            )
        # report_fold_p is a non-negative, finite real exponent. bool is a real int
        # in Python, but never a valid exponent here, so reject it BEFORE the
        # int→float canonicalization would silently accept True as 1.0.
        p = self.report_fold_p
        if isinstance(p, bool):
            raise TypeError(f"report_fold_p must be a real number, not bool; got {p!r}")
        if not isinstance(p, (int, float)):
            raise TypeError(
                f"report_fold_p must be a real number; got {type(p).__name__}"
            )
        if isinstance(p, int):
            # Canonicalize accepted ints to float so 0 and 0.0 (or 1 and 1.0) share
            # one fingerprint and one behavioural identity. An int too large to
            # represent (e.g. ±10**400) overflows the float conversion — surface it
            # as the promised finiteness ValueError (either sign), not a raw
            # OverflowError.
            try:
                p = float(p)
            except OverflowError:
                raise ValueError(
                    f"report_fold_p must be finite; got out-of-range int {p!r}"
                ) from None
            object.__setattr__(self, "report_fold_p", p)
        if not math.isfinite(p):
            raise ValueError(f"report_fold_p must be finite; got {p!r}")
        if p < 0.0:
            raise ValueError(f"report_fold_p must be >= 0; got {p!r}")
        if self.report_fold_scope not in REPORT_FOLD_SCOPES:
            raise ValueError(
                f"report_fold_scope must be one of {sorted(REPORT_FOLD_SCOPES)}; "
                f"got {self.report_fold_scope!r}"
            )
        if self.report_self_term not in REPORT_SELF_TERM_MODES:
            raise ValueError(
                f"report_self_term must be one of {sorted(REPORT_SELF_TERM_MODES)}; "
                f"got {self.report_self_term!r}"
            )


# The report-fold axes are appended to the fingerprint payload ONLY when active, so
# a config at their identity defaults hashes byte-identically to the pre-Phase-1
# payload (the legacy fields alone). See _report_fold_fingerprint_tokens.
_REPORT_FOLD_FIELD_NAMES = ("report_fold_p", "report_fold_scope", "report_self_term")


def _report_fold_fingerprint_tokens(config: RootCalcConfig) -> list[str]:
    """Behaviourally-canonical fingerprint tokens for the report-fold axes.

    Returns an empty list at the identity (so the payload is byte-unchanged) and
    only the tokens that actually alter scoring otherwise:

    - ``report_fold_p``: signed zero is canonicalized to +0.0 (a -0.0 exponent folds
      nothing, exactly like +0.0), so it is omitted; a positive p is emitted.
    - ``report_fold_scope``: emitted ONLY when p is active. With p == 0 the fold is
      off, so the scope selects nothing and is inert — two configs differing only in
      scope at p == 0 must share a fingerprint.
    - ``report_self_term``: an orthogonal pre-fold quality choice that applies even
      at p == 0, so it is emitted whenever it is not the ``"keep"`` identity.
    """
    tokens: list[str] = []
    p = config.report_fold_p
    # p == 0 (including -0.0, since -0.0 == 0.0) leaves the fold off, so BOTH p and
    # the now-inert scope are omitted — signed/plain zero and any scope collapse to
    # one fingerprint. A positive p is active: emit it and its scope.
    if p != 0.0:
        tokens.append(f"report_fold_p={p!r}")
        tokens.append(f"report_fold_scope={config.report_fold_scope!r}")
    if config.report_self_term != "keep":
        tokens.append(f"report_self_term={config.report_self_term!r}")
    return tokens


def root_calc_config_fingerprint(config: RootCalcConfig | None = None) -> str:
    """Return a stable fingerprint for the active root scoring configuration.

    Rejects every non-RootCalcConfig argument (including falsy values and a raw
    GridCell) with ``TypeError`` via an explicit ``None`` branch — a GridCell must be
    routed through its ``.config`` (the calibration script's ``_cfg_fp`` is the sole
    sanctioned router), never fingerprinted directly. The report-fold axes are
    canonicalized so the historical identity keeps its pre-Phase-1 fingerprint (see
    :func:`_report_fold_fingerprint_tokens`).
    """
    if config is None:
        config = RootCalcConfig()
    elif not isinstance(config, RootCalcConfig):
        raise TypeError(
            "root_calc_config_fingerprint requires a RootCalcConfig or None; got "
            f"{type(config).__name__}"
        )
    legacy = [
        f"{config_field.name}={getattr(config, config_field.name)!r}"
        for config_field in fields(config)
        if config_field.name not in _REPORT_FOLD_FIELD_NAMES
    ]
    payload = "|".join(legacy + _report_fold_fingerprint_tokens(config))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class NodeDebug:
    fen: str
    is_user_turn: bool
    in_book: bool
    is_extension_node: bool
    p_n: float
    c_n: float
    sample_conf: float
    freshness: float
    evidence_total: float
    days_since_last_touch: float
    last_touch_at: datetime | None
    live_attempts: int
    live_passes: int
    review_attempts: int
    prepared_children: list[str]
    weights: dict[str, float]
    subtree_live_attempts: int
    subtree_review_attempts: int
    covered_locally: bool
    raw_score: float
    raw_confidence: float
    raw_coverage: float
    raw_depth: float
    is_leaf: bool
    # Report-stage observability (g-report-debug-api). NULL until this FEN is
    # REPORTED as its own row in :meth:`_SharedCalculator._direct_metrics`; a FEN
    # only ever visited as a descendant during ``_calc`` (never reported) keeps all
    # four null. Back-filled idempotently on the shared, mutable per-FEN object, so a
    # descendant later reported as its own row becomes non-null through the earlier
    # RootScore snapshots that hold the same object (the shared-object contract).
    #   - pre_fold_quality: the 0..100 base actually selected by
    #     keep/drop_user/keep_fallback, BEFORE the coverage fold.
    #   - reported_score: pre_fold_quality * report_fold_multiplier — the final
    #     opening_score returned for the row.
    #   - report_fold_multiplier: bounded_coverage_fraction ** report_fold_p for an
    #     active, in-scope row, after an epsilon-tolerant bounds check and [0, 1]
    #     clamp; 1.0 for a dormant fold or an out-of-scope row.
    #   - report_self_term_effective: which self-term arm scored the row (see
    #     :data:`ReportSelfTermEffective`).
    pre_fold_quality: float | None = None
    reported_score: float | None = None
    report_fold_multiplier: float | None = None
    report_self_term_effective: ReportSelfTermEffective | None = None


@dataclass(frozen=True)
class BranchSummary:
    opening_key: str
    opening_name: str
    value: float


@dataclass(frozen=True)
class RootScore:
    opening_key: str
    opening_name: str
    opening_family: str
    player_color: str
    opening_score: float
    confidence: float
    coverage: float
    weighted_depth: float
    sample_size: int
    game_count: int
    last_practiced_at: datetime | None
    strongest_branch: BranchSummary | None
    weakest_branch: BranchSummary | None
    underexposed_branch: BranchSummary | None
    computed_at: datetime
    debug_nodes: list[NodeDebug]


@dataclass(frozen=True)
class PositionScore:
    """One direct position-score read-model row (see ``OpeningPositionScore``).

    ``normalized_fen`` is the normalized 4-field FEN identity. ``in_book`` marks a
    reference ``OpeningGraph`` position; a row with ``in_book`` false is a connected
    observed off-book node. ``has_evidence`` is the no-data gate: when false the four
    metric values are ``None`` and the counts are zero, because no mastery evidence
    exists at or below the FEN. The metrics, when present, come from the same
    formulas as :meth:`_SharedCalculator._base_root_score`.
    """

    normalized_fen: str
    player_color: str
    in_book: bool
    has_evidence: bool
    opening_score: float | None
    confidence: float | None
    coverage: float | None
    weighted_depth: float | None
    sample_size: int
    game_count: int
    last_practiced_at: datetime | None
    computed_at: datetime


@dataclass
class PositionCalcTelemetry:
    """Optional out-param for :meth:`_SharedCalculator.compute_position_scores`.

    Mutated in place, never read by scoring. Lets the batch builder log row-volume
    drift if user evidence grows beyond the spike's measured range (~15.5k book
    nodes + observed edges). ``persisted_row_count == scoreable_position_count +
    observed_off_book_row_count`` minus any overlap (an off-book FEN that is itself
    scoreable is counted under both classes but persisted once).
    """

    domain_count: int = 0
    scoreable_position_count: int = 0
    observed_off_book_row_count: int = 0
    persisted_row_count: int = 0
    metric_key_count: int = 0


def _normalized(fen: str) -> str:
    if len(fen.split()) == 4:
        return fen
    return normalize_fen(fen)


def _synthetic_initial_root() -> OpeningRoot:
    """Build the synthetic whole-repertoire root anchored at the initial position."""
    return OpeningRoot(
        opening_key=SYNTHETIC_INITIAL_FEN,
        opening_name=SYNTHETIC_ROOT_NAME,
        opening_family=SYNTHETIC_ROOT_FAMILY,
        eco=None,
        depth=0,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )


def _raw_middlegame_root_count(named_roots: list[OpeningRoot]) -> int:
    """Count named roots whose own board satisfies the middlegame predicate.

    Structural (depends only on the registry, not on evidence), so it is safe to
    compute on the early-return path. Distinct from the unscored-root count: a
    raw-middlegame root may still be scored via observed off-book children.
    """
    return sum(
        1 for root in named_roots if is_middlegame_position(_normalized(root.opening_key))
    )


def _iter_named_roots(roots: OpeningRoots) -> list[OpeningRoot]:
    result: list[OpeningRoot] = []
    seen: set[str] = set()
    for family in roots.get_families():
        for root in roots.get_family(family):
            if root.opening_key not in seen:
                seen.add(root.opening_key)
                result.append(root)
    return result


class _SharedCalculator:
    def __init__(
        self,
        player_color: str,
        graph: OpeningGraph,
        overlay: EvidenceOverlay,
        roots: OpeningRoots,
        config: RootCalcConfig,
        now: datetime,
        debug: bool = False,
        seeds: list[str] | None = None,
    ) -> None:
        self.player_color = player_color
        self.graph = graph
        self.overlay = overlay
        self.roots = roots
        self.config = config
        self.now = now
        self.debug = debug
        self._graph_nodes = {
            _normalized(fen): node for fen, node in graph._nodes.items()
        }

        self._overlay_nodes = {
            _normalized(fen): evidence for fen, evidence in overlay.nodes.items()
        }
        self._overlay_edges = {
            (_normalized(parent), _normalized(child)): evidence
            for (parent, child), evidence in overlay.edges.items()
        }
        self._observed_children: dict[str, set[str]] = {}
        for parent, child in self._overlay_edges:
            self._observed_children.setdefault(parent, set()).add(child)

        self._reference_cache: dict[str, tuple[str, ...]] = {}
        self._structural_cache: dict[str, tuple[str, ...]] = {}
        self._reachable_cache: dict[str, set[str]] = {}
        self._coverage_totals_cache: dict[str, tuple[int, int]] = {}
        self._middlegame_cache: dict[str, bool] = {}
        self._weights_cache: dict[str, dict[str, float]] = {}
        self._score_reachable_cache: dict[str, set[str]] = {}
        self._metrics: dict[tuple[str, bool], tuple[float, float, float, float]] = {}
        self.debug_nodes: dict[str, NodeDebug] = {}
        self.calculation_misses = 0

        named_keys = [_normalized(root.opening_key) for root in _iter_named_roots(roots)]
        start_seeds = seeds or [graph.root_fen, *named_keys]
        self._domain = self._enumerate_domain(start_seeds)
        self._structural_parents = self._build_structural_parents()
        self._mastery_ancestors = self._ancestors_of(
            {
                fen
                for fen, evidence in self._overlay_nodes.items()
                if evidence.quality_count > 0
            }
        )
        self._ghost_ancestors = self._ancestors_of(
            {
                fen
                for fen, evidence in self._overlay_nodes.items()
                if evidence.is_ghost_target
            }
        )
        self._precut_weights = {
            fen: self._base_weights(fen, self._structural_children(fen))
            for fen in self._domain
        }
        self._scc_index, self._scc_order = self._build_scc_cut()

    def _is_middlegame(self, fen: str) -> bool:
        if fen not in self._middlegame_cache:
            self._middlegame_cache[fen] = is_middlegame_position(fen)
        return self._middlegame_cache[fen]

    def _structural_children(self, fen: str) -> tuple[str, ...]:
        fen = _normalized(fen)
        cached = self._structural_cache.get(fen)
        if cached is not None:
            return cached

        children = set(self._observed_children.get(fen, ()))
        children.update(self._reference_children(fen))
        result = tuple(sorted(children))
        self._structural_cache[fen] = result
        return result

    def _reference_children(self, fen: str) -> tuple[str, ...]:
        fen = _normalized(fen)
        cached = self._reference_cache.get(fen)
        if cached is not None:
            return cached

        children: set[str] = set()
        node = self._graph_nodes.get(fen)
        if node is not None:
            for raw_child in node.children.values():
                child = _normalized(raw_child)
                if not self._is_middlegame(child):
                    children.add(child)

        result = tuple(sorted(children))
        self._reference_cache[fen] = result
        return result

    def _enumerate_domain(self, seeds: list[str]) -> set[str]:
        visited: set[str] = set()
        stack = [_normalized(seed) for seed in seeds]
        while stack:
            fen = stack.pop()
            if fen in visited:
                continue
            visited.add(fen)
            stack.extend(self._structural_children(fen))
        return visited

    def _get_reachable(self, fen: str) -> set[str]:
        fen = _normalized(fen)
        cached = self._reachable_cache.get(fen)
        if cached is not None:
            return cached
        reachable = self._enumerate_domain([fen])
        self._reachable_cache[fen] = reachable
        return reachable

    def _build_structural_parents(self) -> dict[str, set[str]]:
        parents: dict[str, set[str]] = {}
        for parent in self._domain:
            for child in self._structural_children(parent):
                parents.setdefault(child, set()).add(parent)
        return parents

    def _ancestors_of(self, targets: set[str]) -> set[str]:
        ancestors: set[str] = set()
        stack = [fen for fen in targets if fen in self._domain]
        while stack:
            fen = stack.pop()
            if fen in ancestors:
                continue
            ancestors.add(fen)
            stack.extend(self._structural_parents.get(fen, ()))
        return ancestors

    def has_mastery_below(self, fen: str) -> bool:
        return _normalized(fen) in self._mastery_ancestors

    def _subtree_has_ghost_target(self, fen: str) -> bool:
        return _normalized(fen) in self._ghost_ancestors

    def _is_user_turn(self, fen: str) -> bool:
        return active_color(fen) == self.player_color

    def _prepared_children(
        self, fen: str, children: tuple[str, ...] | list[str]
    ) -> list[str]:
        prepared: list[str] = []
        for child in children:
            edge = self._overlay_edges.get((fen, child))
            if (
                (edge is not None and (edge.live_attempts >= 2 or edge.live_passes >= 1))
                or self._subtree_has_ghost_target(child)
            ):
                prepared.append(child)
        return prepared

    def _base_weights(
        self, fen: str, children: tuple[str, ...] | list[str]
    ) -> dict[str, float]:
        if self._is_user_turn(fen):
            weighted_children = self._prepared_children(fen, children)
            if not weighted_children:
                return {}
            bases = {
                child: (
                    self._overlay_edges.get((fen, child)).live_attempts
                    if self._overlay_edges.get((fen, child)) is not None
                    else 0
                )
                + self.config.rho
                for child in weighted_children
            }
            total = sum(bases.values())
            return {child: basis / total for child, basis in bases.items()}
        if not children:
            return {}
        reference_children = set(self._reference_children(fen))
        weighted_children = [
            child for child in children if child in reference_children
        ] or list(children)
        weight = 1.0 / len(weighted_children)
        return {child: weight for child in weighted_children}

    def _build_scc_cut(self) -> tuple[dict[str, int], dict[str, int]]:
        index = 0
        indexes: dict[str, int] = {}
        lowlinks: dict[str, int] = {}
        stack: list[str] = []
        on_stack: set[str] = set()
        components: list[list[str]] = []

        def strongconnect(fen: str) -> None:
            nonlocal index
            indexes[fen] = index
            lowlinks[fen] = index
            index += 1
            stack.append(fen)
            on_stack.add(fen)
            for child in self._precut_weights.get(fen, ()):
                if child not in indexes:
                    strongconnect(child)
                    lowlinks[fen] = min(lowlinks[fen], lowlinks[child])
                elif child in on_stack:
                    lowlinks[fen] = min(lowlinks[fen], indexes[child])
            if lowlinks[fen] == indexes[fen]:
                component: list[str] = []
                while True:
                    child = stack.pop()
                    on_stack.remove(child)
                    component.append(child)
                    if child == fen:
                        break
                components.append(component)

        for fen in sorted(self._domain):
            if fen not in indexes:
                strongconnect(fen)

        scc_index: dict[str, int] = {}
        scc_order: dict[str, int] = {}
        for component_index, component in enumerate(components):
            for order, fen in enumerate(sorted(component)):
                scc_index[fen] = component_index
                scc_order[fen] = order
        return scc_index, scc_order

    def _score_children(self, fen: str) -> tuple[str, ...]:
        if not self._is_user_turn(fen):
            children = self._precut_weights.get(fen, {})
        else:
            children = self._structural_children(fen)
        result: list[str] = []
        for child in children:
            same_scc = self._scc_index.get(fen) == self._scc_index.get(child)
            backward = same_scc and self._scc_order.get(child, 0) <= self._scc_order.get(
                fen, 0
            )
            if not backward:
                result.append(child)
        return tuple(result)

    def _get_weights(self, fen: str) -> dict[str, float]:
        if fen not in self._weights_cache:
            score_children = set(self._score_children(fen))
            survivors = {
                child: weight
                for child, weight in self._precut_weights.get(fen, {}).items()
                if child in score_children
            }
            total = sum(survivors.values())
            self._weights_cache[fen] = (
                {
                    child: weight / total
                    for child, weight in survivors.items()
                }
                if total > 0
                else {}
            )
        return self._weights_cache[fen]

    def _mastery(self, fen: str) -> float:
        """Local mastery: the lower-confidence bound on the Beta posterior mean.

        Quality is treated as fractional Beta successes (``a = quality_sum + alpha``
        "successes", ``b = (quality_count - quality_sum) + beta`` "failures"); the
        posterior mean ``a / (a + b)`` is exactly today's mastery. ``lcb_z`` shifts
        the report down by ``z`` normal-approx standard deviations so thin evidence
        reads lower and rises toward the mean as reps are earned (g-zc3p Part 1).

        - ``lcb_z == 0.0`` returns the mean UNCHANGED (the pre-readiness value).
        - The bound is a clamped normal approximation ``clamp(mean - z*std, 0, 1)``,
          NOT ``scipy.stats.beta.ppf`` — scipy is not a backend dependency and numpy
          has no ``beta.ppf``.
        - The swap is GLOBAL (every caller of ``_mastery``), and applies to
          zero-quality nodes too (no ``quality_count > 0`` guard): the LCB shrinks
          the unearned prior (~0.33 → ~0.10 at z=1), the deliberate backstop for the
          gate-alone line-606 leak in ``_calc``. Reviews add no quality
          pseudo-observations — the Beta uses ``quality_count`` only.
        """
        node = self._overlay_nodes.get(fen)
        quality_sum = node.quality_sum if node is not None else 0.0
        quality_count = node.quality_count if node is not None else 0
        mean = (quality_sum + self.config.alpha) / (
            quality_count + self.config.alpha + self.config.beta
        )
        z = self.config.lcb_z
        if z == 0.0:
            return mean
        a = quality_sum + self.config.alpha
        b = (quality_count - quality_sum) + self.config.beta
        total = a + b
        variance = a * b / (total * total * (total + 1.0))
        lcb = mean - z * math.sqrt(variance)
        return 0.0 if lcb < 0.0 else (1.0 if lcb > 1.0 else lcb)

    def _confidence_components(
        self, fen: str
    ) -> tuple[float, float, float, float, datetime | None]:
        node = self._overlay_nodes.get(fen)
        if node is None:
            return 0.0, 0.0, 0.0, 0.0, None
        touches = [
            touch for touch in (node.last_live_at, node.last_review_at) if touch is not None
        ]
        if not touches:
            return 0.0, 0.0, 0.0, 0.0, None
        last_touch = max(touches)
        evidence = node.live_attempts + self.config.lambda_review * node.review_attempts
        sample_conf = 1.0 - math.exp(-evidence / self.config.k_evidence)
        days = max(0.0, (self.now - last_touch).total_seconds() / 86400.0)
        freshness = math.exp(-days / self.config.half_life_days)
        return sample_conf * freshness, sample_conf, freshness, evidence, last_touch

    def _subtree_coverage_totals(self, fen: str) -> tuple[int, int]:
        if fen not in self._coverage_totals_cache:
            live = 0
            review = 0
            for reachable in self._get_reachable(fen):
                node = self._overlay_nodes.get(reachable)
                if node is not None:
                    live += node.live_attempts
                    review += node.review_attempts
            self._coverage_totals_cache[fen] = live, review
        return self._coverage_totals_cache[fen]

    def _subtree_is_locally_covered(self, fen: str) -> bool:
        live, review = self._subtree_coverage_totals(fen)
        return live >= self.config.coverage_live_threshold or (
            live >= 1 and review >= 1
        )

    def _record_debug(
        self,
        fen: str,
        weights: dict[str, float],
        prepared: list[str],
        is_leaf: bool,
    ) -> None:
        if not self.debug or fen in self.debug_nodes:
            return
        node = self._overlay_nodes.get(fen)
        confidence, sample_conf, freshness, evidence, last_touch = (
            self._confidence_components(fen)
        )
        days = (
            max(0.0, (self.now - last_touch).total_seconds() / 86400.0)
            if last_touch is not None
            else 0.0
        )
        live, review = self._subtree_coverage_totals(fen)
        graph_node = self._graph_nodes.get(fen)
        observed_parent = fen in self._observed_children
        self.debug_nodes[fen] = NodeDebug(
            fen=fen,
            is_user_turn=self._is_user_turn(fen),
            in_book=graph_node is not None,
            is_extension_node=graph_node is None or observed_parent,
            p_n=self._mastery(fen) if self._is_user_turn(fen) else 1.0,
            c_n=confidence if self._is_user_turn(fen) else 1.0,
            sample_conf=sample_conf,
            freshness=freshness,
            evidence_total=evidence,
            days_since_last_touch=days,
            last_touch_at=last_touch,
            live_attempts=node.live_attempts if node is not None else 0,
            live_passes=node.live_passes if node is not None else 0,
            review_attempts=node.review_attempts if node is not None else 0,
            prepared_children=prepared,
            weights=dict(weights),
            subtree_live_attempts=live,
            subtree_review_attempts=review,
            covered_locally=self._subtree_is_locally_covered(fen),
            raw_score=0.0,
            raw_confidence=0.0,
            raw_coverage=0.0,
            raw_depth=0.0,
            is_leaf=is_leaf,
        )

    def _calc(
        self, fen: str, perfect: bool = False
    ) -> tuple[float, float, float, float]:
        fen = _normalized(fen)
        key = (fen, perfect)
        cached = self._metrics.get(key)
        if cached is not None:
            return cached
        self.calculation_misses += 1

        is_user = self._is_user_turn(fen)
        score_children = self._score_children(fen)
        weights = self._get_weights(fen)
        prepared = list(weights) if is_user else []
        is_leaf = not score_children
        self._record_debug(fen, weights, prepared, is_leaf)

        if is_leaf:
            if is_user:
                mastery = 1.0 if perfect else self._mastery(fen)
                confidence = 1.0 if perfect else self._confidence_components(fen)[0]
                result = mastery, confidence, 1.0, mastery
            else:
                result = 1.0, 1.0, 1.0, 0.0
        elif is_user:
            mastery = 1.0 if perfect else self._mastery(fen)
            confidence = 1.0 if perfect else self._confidence_components(fen)[0]
            if not weights:
                result = mastery, confidence, 0.0, mastery
            else:
                score_sum = confidence_sum = coverage_sum = depth_sum = 0.0
                for child, weight in weights.items():
                    child_score, child_conf, child_cov, child_depth = self._calc(
                        child, perfect
                    )
                    score_sum += weight * child_score
                    confidence_sum += weight * child_conf
                    coverage_sum += weight * child_cov
                    depth_sum += weight * child_depth
                result = (
                    mastery * (1.0 + self.config.gamma * score_sum),
                    confidence * confidence_sum,
                    coverage_sum,
                    mastery * (1.0 + self.config.gamma * depth_sum),
                )
        else:
            # Opponent node: gate each reply's SCORE credit by local coverage so an
            # unprepared book reply no longer falls back to the ~0.33 mastery prior
            # (g-zc3p Part 2). Applies at opponent nodes ONLY (user-turn breadth is
            # not penalized). The COVERAGE accumulator is unchanged from today.
            #
            # branch_cov factors:
            #   - perfect pass OR coverage_fold "off" → 1.0. The perfect pass MUST
            #     assume full coverage, else the gate would cancel in real/perfect
            #     and do nothing.
            #   - "gate" → the 0/1 gate ALONE (recommended). Deeper gaps are already
            #     folded in once by the recursive gate at deeper opponent nodes.
            #   - "gate_x_cov" → gate * child_cov, the double-counting arm kept only
            #     so the calibration grid can confirm the over-penalty empirically.
            score_sum = confidence_sum = coverage_sum = depth_sum = 0.0
            fold = self.config.coverage_fold
            for child, weight in weights.items():
                child_score, child_conf, child_cov, child_depth = self._calc(
                    child, perfect
                )
                covered = float(self._subtree_is_locally_covered(child))
                if perfect or fold == "off":
                    branch_cov = 1.0
                elif fold == "gate_x_cov":
                    branch_cov = covered * child_cov
                else:  # "gate"
                    branch_cov = covered
                score_sum += weight * branch_cov * child_score
                confidence_sum += weight * child_conf
                coverage_sum += weight * covered * child_cov
                depth_sum += weight * child_depth
            result = score_sum, confidence_sum, coverage_sum, depth_sum

        self._metrics[key] = result
        if not perfect and self.debug:
            debug = self.debug_nodes[fen]
            (
                debug.raw_score,
                debug.raw_confidence,
                debug.raw_coverage,
                debug.raw_depth,
            ) = result
        return result

    def _score_reachable(self, fen: str) -> set[str]:
        if fen not in self._score_reachable_cache:
            reachable: set[str] = set()
            queue = deque([fen])
            while queue:
                current = queue.popleft()
                if current in reachable:
                    continue
                reachable.add(current)
                queue.extend(self._get_weights(current))
            self._score_reachable_cache[fen] = reachable
        return self._score_reachable_cache[fen]

    def _aggregate_metadata(
        self, fen: str
    ) -> tuple[int, int, datetime | None]:
        """Transposition-safe sample/game/last-practiced metadata for a position.

        Shared by named-root rows and arbitrary position rows so both report
        identical evidence semantics over the structural reachable set:

        - ``sample_size``: summed ``quality_count`` (move-observations);
        - ``game_count``: distinct ``session_ids`` (a game played through nine
          subtree positions counts once, unlike ``sample_size``);
        - ``last_practiced_at``: max live/review touch.
        """
        reachable = self._get_reachable(_normalized(fen))
        sample_size = sum(
            node.quality_count
            for reached in reachable
            if (node := self._overlay_nodes.get(reached)) is not None
        )
        game_count = len(
            {
                session_id
                for reached in reachable
                if (node := self._overlay_nodes.get(reached)) is not None
                for session_id in node.session_ids
            }
        )
        touches = [
            touch
            for reached in reachable
            if (node := self._overlay_nodes.get(reached)) is not None
            for touch in (node.last_live_at, node.last_review_at)
            if touch is not None
        ]
        return sample_size, game_count, (max(touches) if touches else None)

    def _prefold_quality(
        self, key: str, node_score: float, node_perfect_score: float
    ) -> tuple[float, ReportSelfTermEffective]:
        """Pre-fold 0..100 quality for a reported row, plus the effective self-term.

        Returns ``(pre_fold_quality, report_self_term_effective)``: the base the
        coverage fold will then multiply by ``coverage ** report_fold_p`` exactly once,
        and the truthful record of which arm produced it (see
        :data:`ReportSelfTermEffective`). Orthogonal to the fold; never touches
        confidence, coverage, weighted_depth, or ``_calc``'s recursion.

        ``"keep"`` (default): the ordinary aggregate node ratio
        ``100 * node_score / node_perfect_score`` (0 when the perfect denominator is
        not positive) — byte-identical to the pre-B1 scorer — reported ``"keep"``.

        ``"drop_user"`` (B1, g-drop-user-score): for a user-turn row whose prepared-
        child weight set is nonempty AND whose child perfect denominator is positive,
        the CHILD-ONLY ratio ``100 * sum(w * child_natural) / sum(w * child_perfect)``,
        reported ``"drop_user"``. The node's own mastery self-term is dropped, scoring
        the row purely by the quality its prepared replies lead to. The child sums are
        recomputed directly from ``_get_weights`` and the memoized ``_calc`` passes —
        never algebraically recovered from the aggregate node score, since that
        recovery divides by the node's mastery and ``gamma`` and collapses at their
        zeros. Every ``_calc`` call here is already memoized by the two node-level
        passes in :meth:`_direct_metrics`, so this adds no recursion or misses and is
        idempotent.

        Every other shape keeps the ordinary ratio. An OPPONENT-turn row reports
        ``"keep"`` (its self-term is the opponent's, not the user's). A drop_user
        USER-turn row that cannot take the child ratio — a leaf or empty prepared-
        child set (no child ratio exists), or a non-positive child perfect denominator
        (guarded exactly like the node ratio's own zero denominator) — reports
        ``"keep_fallback"``: numerically the ordinary ratio, but flagged so the
        fallback is visible in the debug/API surface.
        """
        ordinary = (
            100.0 * node_score / node_perfect_score
            if node_perfect_score > 0
            else 0.0
        )
        if self.config.report_self_term != "drop_user" or not self._is_user_turn(key):
            # report_self_term="keep", or a drop_user opponent-turn row: the ordinary
            # node ratio scored the row, so the effective self-term is a plain "keep".
            return ordinary, "keep"
        weights = self._get_weights(key)
        if not weights:
            # User leaf or a user row whose children are all unprepared/cut: no
            # child ratio to compute, so fall back to the ordinary node ratio.
            return ordinary, "keep_fallback"
        child_natural_sum = 0.0
        child_perfect_sum = 0.0
        for child, weight in weights.items():
            child_natural_sum += weight * self._calc(child, False)[0]
            child_perfect_sum += weight * self._calc(child, True)[0]
        if child_perfect_sum <= 0.0:
            return ordinary, "keep_fallback"
        return 100.0 * child_natural_sum / child_perfect_sum, "drop_user"

    def _direct_metrics(
        self, fen: str
    ) -> tuple[float, float, float, float]:
        """Direct opening_score/confidence/coverage/weighted_depth for a FEN.

        Same formulas as :meth:`_base_root_score`, but usable for any scoreable
        position. Reuses the one shared memoized ``_calc`` traversal: at most two
        metric records per reachable FEN (natural + perfect), never one root walk
        per position.

        Report-time pre-fold quality (g-drop-user-score): the pre-fold opening_score
        is selected by ``report_self_term`` in :meth:`_prefold_quality` — the ordinary
        node ratio for ``"keep"``, the child-only ratio for a qualifying user-turn row
        under ``"drop_user"``. This is orthogonal to the coverage fold below.

        Report-time coverage fold (Option A, g-report-fold-score): when
        ``report_fold_p`` is active and the row is in scope, the selected pre-fold
        quality is multiplied by ``coverage_fraction ** report_fold_p`` exactly once.
        For that power only, the raw fraction passes an epsilon-tolerant bounds check
        and its exponent operand is clamped to ``[0, 1]``. The fold touches ONLY
        ``opening_score`` — confidence, displayed coverage, and weighted_depth stay
        byte-identical to the pre-fold scorer, and ``_calc``'s coverage channel is
        untouched. Scope ``"all"`` folds both turns; scope ``"user"`` folds only
        user-turn rows. Rows that are out of scope, or that run at
        ``report_fold_p == 0``, take an effective multiplier of 1.0 and never evaluate
        the power or validate coverage.
        """
        key = _normalized(fen)
        score, confidence, coverage, depth = self._calc(key, False)
        perfect_score, perfect_confidence, _, _ = self._calc(key, True)

        # Pre-fold 0..100 quality ratio (keep vs drop_user); the fold multiplies it
        # once. self_term_effective records which arm actually scored the row.
        opening_score, self_term_effective = self._prefold_quality(
            key, score, perfect_score
        )
        pre_fold_quality = opening_score
        multiplier = 1.0
        p = self.config.report_fold_p
        if p != 0.0 and (
            self.config.report_fold_scope == "all" or self._is_user_turn(key)
        ):
            # Active, in-scope row: tolerate only float accumulation drift around the
            # mathematical [0, 1] bounds, then clamp the exponent operand. Materially
            # out-of-range (including NaN) values still fail closed rather than
            # returning a complex/NaN opening_score. Keep ``coverage`` itself raw so
            # the displayed channel remains byte-identical to the dormant scorer.
            if not (
                -_REPORT_FOLD_COVERAGE_EPSILON
                <= coverage
                <= 1.0 + _REPORT_FOLD_COVERAGE_EPSILON
            ):
                raise ValueError(
                    "report-fold coverage fraction out of range for "
                    f"{key!r}: {coverage!r}"
                )
            bounded_coverage = min(1.0, max(0.0, coverage))
            multiplier = bounded_coverage**p
            opening_score *= multiplier

        if self.debug:
            # This FEN is being REPORTED, so back-fill its report-stage observability.
            # _calc(key, False) above already created the shared per-FEN NodeDebug (it
            # calls _record_debug on a miss, or the node exists from a prior miss on a
            # cache hit), so debug_nodes[key] is present. The same mutable object is
            # already referenced by any earlier RootScore snapshot, so this write is
            # visible through those views (the shared-object contract). reported_score
            # is exactly the opening_score returned below.
            debug = self.debug_nodes[key]
            debug.pre_fold_quality = pre_fold_quality
            debug.reported_score = opening_score
            debug.report_fold_multiplier = multiplier
            debug.report_self_term_effective = self_term_effective

        return (
            opening_score,
            (
                100.0 * confidence / perfect_confidence
                if perfect_confidence > 0
                else 0.0
            ),
            100.0 * coverage,
            depth,
        )

    def compute_position_scores(
        self, *, telemetry: PositionCalcTelemetry | None = None
    ) -> list[PositionScore]:
        """Direct position-score rows for the tree read model (g-tree-score-model).

        Iterates the selected scorer domain once, reusing this calculator's
        ``_metrics``/SCC-cut/weights/reachable caches (the same ones the named-root
        rows use), and emits only the rows the database needs:

        - in-book positions with mastery evidence at/below the FEN — full metrics;
        - connected observed off-book positions (not in ``OpeningGraph``) — metrics
          when evidence exists at/below, otherwise a no-data row so the API can
          distinguish a navigable observed off-book node from an unknown FEN.

        Static in-book positions with no evidence below are intentionally skipped:
        they are represented by ``OpeningGraph``, and the API returns no-data for an
        in-graph FEN absent from the batch. Score visibility is gated purely by
        ``has_mastery_below`` regardless of side to move, so a no-evidence user-turn
        row never surfaces the alpha/beta prior and a no-evidence opponent-turn leaf
        never surfaces ``_calc``'s perfect-looking ``(1.0, 1.0, 1.0, 0.0)`` result.
        """
        results: list[PositionScore] = []
        scoreable_count = 0
        off_book_count = 0
        for fen in sorted(self._domain):
            in_book = fen in self._graph_nodes
            has_evidence = self.has_mastery_below(fen)
            if not has_evidence and in_book:
                # Static in-book no-evidence node: represented by OpeningGraph, not
                # materialized here.
                continue
            if not in_book:
                off_book_count += 1
            if has_evidence:
                scoreable_count += 1
                opening_score, confidence, coverage, depth = self._direct_metrics(fen)
                sample_size, game_count, last_practiced_at = self._aggregate_metadata(
                    fen
                )
                results.append(
                    PositionScore(
                        normalized_fen=fen,
                        player_color=self.player_color,
                        in_book=in_book,
                        has_evidence=True,
                        opening_score=opening_score,
                        confidence=confidence,
                        coverage=coverage,
                        weighted_depth=depth,
                        sample_size=sample_size,
                        game_count=game_count,
                        last_practiced_at=last_practiced_at,
                        computed_at=self.now,
                    )
                )
            else:
                # Connected observed off-book node with no evidence at/below: a
                # navigable no-data row, never a fabricated prior.
                results.append(
                    PositionScore(
                        normalized_fen=fen,
                        player_color=self.player_color,
                        in_book=False,
                        has_evidence=False,
                        opening_score=None,
                        confidence=None,
                        coverage=None,
                        weighted_depth=None,
                        sample_size=0,
                        game_count=0,
                        last_practiced_at=None,
                        computed_at=self.now,
                    )
                )
        if telemetry is not None:
            telemetry.domain_count = len(self._domain)
            telemetry.scoreable_position_count = scoreable_count
            telemetry.observed_off_book_row_count = off_book_count
            telemetry.persisted_row_count = len(results)
            telemetry.metric_key_count = len(self._metrics)
        return results

    def _base_root_score(self, root: OpeningRoot) -> RootScore:
        key = _normalized(root.opening_key)
        opening_score, confidence, coverage, depth = self._direct_metrics(key)
        sample_size, game_count, last_practiced_at = self._aggregate_metadata(key)
        return RootScore(
            opening_key=root.opening_key,
            opening_name=root.opening_name,
            opening_family=root.opening_family,
            player_color=self.player_color,
            opening_score=opening_score,
            confidence=confidence,
            coverage=coverage,
            weighted_depth=depth,
            sample_size=sample_size,
            game_count=game_count,
            last_practiced_at=last_practiced_at,
            strongest_branch=None,
            weakest_branch=None,
            underexposed_branch=None,
            computed_at=self.now,
            debug_nodes=list(self.debug_nodes.values()) if self.debug else [],
        )

    def compute_roots(
        self,
        roots_to_compute: list[OpeningRoot],
        *,
        include_branch_summaries: bool,
    ) -> dict[str, RootScore]:
        scores = {
            root.opening_key: self._base_root_score(root) for root in roots_to_compute
        }
        if not include_branch_summaries:
            return scores

        enriched: dict[str, RootScore] = {}
        for root in roots_to_compute:
            score = scores[root.opening_key]
            score_reachable = self._score_reachable(_normalized(root.opening_key))
            immediate = [
                scores[key]
                for key in sorted(root.child_keys)
                if key in scores and _normalized(key) in score_reachable
            ]
            strongest = (
                max(immediate, key=lambda item: (item.opening_score, item.opening_key))
                if immediate
                else None
            )
            weakest = (
                min(immediate, key=lambda item: (item.opening_score, item.opening_key))
                if immediate
                else None
            )
            underexposed_candidates = [
                scores[descendant.opening_key]
                for descendant in self.roots.get_descendants(root.opening_key)
                if descendant.opening_key in scores
                and _normalized(descendant.opening_key) in score_reachable
                and not self._subtree_is_locally_covered(
                    _normalized(descendant.opening_key)
                )
            ]
            underexposed = (
                min(
                    underexposed_candidates,
                    key=lambda item: (item.coverage, item.opening_key),
                )
                if underexposed_candidates
                else None
            )
            enriched[root.opening_key] = replace(
                score,
                strongest_branch=(
                    BranchSummary(
                        strongest.opening_key,
                        strongest.opening_name,
                        strongest.opening_score,
                    )
                    if strongest
                    else None
                ),
                weakest_branch=(
                    BranchSummary(
                        weakest.opening_key,
                        weakest.opening_name,
                        weakest.opening_score,
                    )
                    if weakest
                    else None
                ),
                underexposed_branch=(
                    BranchSummary(
                        underexposed.opening_key,
                        underexposed.opening_name,
                        1.0 - underexposed.coverage / 100.0,
                    )
                    if underexposed
                    else None
                ),
            )
        return enriched


def _select_named_roots(
    calculator: _SharedCalculator,
    named_roots: list[OpeningRoot],
    *,
    include_synthetic_root: bool,
) -> tuple[list[OpeningRoot], set[str]]:
    eligible = {
        root.opening_key
        for root in named_roots
        if calculator.has_mastery_below(root.opening_key)
    }
    selected = [root for root in named_roots if root.opening_key in eligible]
    if include_synthetic_root and not any(
        root.opening_key == SYNTHETIC_INITIAL_FEN for root in selected
    ):
        # Reuse the same calculator/DAG pass for the whole-repertoire hero row.
        selected = [_synthetic_initial_root(), *selected]
    return selected, eligible


def _populate_root_telemetry(
    telemetry: CalcTelemetry,
    calculator: _SharedCalculator,
    named_roots: list[OpeningRoot],
    result: dict[str, RootScore],
) -> None:
    telemetry.named_root_count = len(named_roots)
    telemetry.actual_key_count = sum(1 for key in calculator._metrics if not key[1])
    telemetry.perfect_key_count = sum(1 for key in calculator._metrics if key[1])
    telemetry.calculation_misses = calculator.calculation_misses
    telemetry.raw_middlegame_root_count = _raw_middlegame_root_count(named_roots)
    telemetry.unscored_root_count = sum(
        1 for root in named_roots if root.opening_key not in result
    )


def _compute_scores(
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig | None,
    now: datetime | None,
    *,
    debug: bool,
    include_branch_summaries: bool,
    include_synthetic_root: bool,
    telemetry: CalcTelemetry | None,
    include_position_scores: bool,
    position_telemetry: PositionCalcTelemetry | None,
) -> tuple[dict[str, RootScore], set[str], list[PositionScore]]:
    """Build one shared calculator and derive named-root and direct position rows.

    Single source of truth behind ``compute_all_root_scores`` (root rows only) and
    ``compute_all_scores`` (root + position rows). One ``_SharedCalculator`` per call
    means the named rows, the synthetic repertoire row, and the direct FEN rows all
    reuse the same ``_metrics``, SCC cut, weights, and reachable caches.
    """
    config = config or RootCalcConfig()
    now = now or datetime.now(timezone.utc)
    named_roots = _iter_named_roots(roots)
    has_quality = any(node.quality_count > 0 for node in overlay.nodes.values())
    if not has_quality:
        # No quality evidence anywhere: no named root and no in-book position is
        # scoreable. Connected observed off-book positions still deserve navigable
        # no-data rows (so the API can tell them from unknown FENs), so only the
        # presence of such off-book endpoints justifies building the calculator on
        # the position path. Otherwise return before any calculator is built.
        need_off_book_rows = include_position_scores and bool(
            observed_off_book_fens(overlay, graph)
        )
        if not need_off_book_rows:
            # Populate telemetry with well-formed zeros (+ structural root counts)
            # so empty/low-evidence pairs get counters rather than None.
            if telemetry is not None:
                telemetry.named_root_count = len(named_roots)
                telemetry.raw_middlegame_root_count = _raw_middlegame_root_count(
                    named_roots
                )
                telemetry.unscored_root_count = len(named_roots)
            return {}, set(), []
    calculator = _SharedCalculator(
        player_color, graph, overlay, roots, config, now, debug=debug
    )
    if has_quality:
        selected, eligible = _select_named_roots(
            calculator, named_roots, include_synthetic_root=include_synthetic_root
        )
        result = calculator.compute_roots(
            selected, include_branch_summaries=include_branch_summaries
        )
    else:
        # No quality: emit no named-root rows and no synthetic repertoire row — only
        # the connected observed off-book no-data position rows computed below.
        eligible, result = set(), {}
    position_scores = (
        calculator.compute_position_scores(telemetry=position_telemetry)
        if include_position_scores
        else []
    )
    if telemetry is not None:
        _populate_root_telemetry(telemetry, calculator, named_roots, result)
    return result, eligible, position_scores


def compute_all_root_scores(
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
    now: datetime | None = None,
    *,
    debug: bool = False,
    include_branch_summaries: bool = True,
    include_synthetic_root: bool = False,
    telemetry: CalcTelemetry | None = None,
) -> tuple[dict[str, RootScore], set[str]]:
    result, eligible, _ = _compute_scores(
        player_color,
        graph,
        overlay,
        roots,
        config,
        now,
        debug=debug,
        include_branch_summaries=include_branch_summaries,
        include_synthetic_root=include_synthetic_root,
        telemetry=telemetry,
        include_position_scores=False,
        position_telemetry=None,
    )
    return result, eligible


def compute_scoped_root_scores(
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    opening_keys: list[str] | tuple[str, ...],
    config: RootCalcConfig | None = None,
    now: datetime | None = None,
) -> dict[str, RootScore]:
    """Score only the requested named roots from one reachability-closed domain.

    This is the terminal-delta fast path: it creates one shared calculator for the
    union of every coalesced session's played roots, emits no synthetic repertoire
    row or direct position rows, and skips branch summaries because the delta
    surface consumes only ``opening_score``.  Missing keys stay missing when the
    requested root has no mastery at/below it; callers must not fabricate the
    calculator prior as an ``after`` value.

    Seeding the calculator with the union, rather than looping over
    :func:`compute_root_score`, preserves shared traversal/SCC work and makes the
    result structurally equivalent to the corresponding entries from
    :func:`compute_all_root_scores`.
    """
    if not any(node.quality_count > 0 for node in overlay.nodes.values()):
        return {}

    requested: list[OpeningRoot] = []
    seen: set[str] = set()
    for opening_key in opening_keys:
        if opening_key in seen:
            continue
        seen.add(opening_key)
        root = roots.get_root(opening_key)
        if root is not None:
            requested.append(root)
    if not requested:
        return {}

    config = config or RootCalcConfig()
    now = now or datetime.now(timezone.utc)
    calculator = _SharedCalculator(
        player_color,
        graph,
        overlay,
        roots,
        config,
        now,
        seeds=[root.opening_key for root in requested],
    )
    selected = [
        root
        for root in requested
        if calculator.has_mastery_below(root.opening_key)
    ]
    return calculator.compute_roots(
        selected,
        include_branch_summaries=False,
    )


def compute_all_scores(
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
    now: datetime | None = None,
    *,
    debug: bool = False,
    include_branch_summaries: bool = True,
    include_synthetic_root: bool = False,
    telemetry: CalcTelemetry | None = None,
    position_telemetry: PositionCalcTelemetry | None = None,
) -> tuple[dict[str, RootScore], set[str], list[PositionScore]]:
    """Named-root scores plus direct position-score rows from one shared traversal.

    The position-row generation path for the opening tree read model: it returns
    the same ``(scores, eligible)`` pair as :func:`compute_all_root_scores` together
    with the list of :class:`PositionScore` rows to persist for ``(batch_id,
    normalized_fen)`` lookup. Both share a single ``_SharedCalculator`` so named and
    direct metrics can never disagree.
    """
    return _compute_scores(
        player_color,
        graph,
        overlay,
        roots,
        config,
        now,
        debug=debug,
        include_branch_summaries=include_branch_summaries,
        include_synthetic_root=include_synthetic_root,
        telemetry=telemetry,
        include_position_scores=True,
        position_telemetry=position_telemetry,
    )


def compute_root_score(
    opening_key: str,
    player_color: str,
    graph: OpeningGraph,
    overlay: EvidenceOverlay,
    roots: OpeningRoots,
    config: RootCalcConfig | None = None,
    now: datetime | None = None,
    debug: bool = False,
    include_branch_summaries: bool = True,
) -> RootScore:
    root = roots.get_root(opening_key)
    if root is None:
        raise ValueError(f"Unknown root: {opening_key}")
    config = config or RootCalcConfig()
    now = now or datetime.now(timezone.utc)
    calculator = _SharedCalculator(
        player_color,
        graph,
        overlay,
        roots,
        config,
        now,
        debug=debug,
        seeds=[opening_key],
    )
    selected = [
        candidate
        for candidate in _iter_named_roots(roots)
        if candidate.opening_key == opening_key
        or (
            calculator.has_mastery_below(candidate.opening_key)
            and _normalized(candidate.opening_key)
            in calculator._get_reachable(_normalized(opening_key))
        )
    ]
    return calculator.compute_roots(
        selected, include_branch_summaries=include_branch_summaries
    )[opening_key]
