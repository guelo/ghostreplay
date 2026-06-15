from __future__ import annotations

import hashlib
import math
from collections import deque
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

from app.fen import active_color, normalize_fen
from app.game_phase import is_middlegame_position
from app.opening_evidence import EvidenceOverlay
from app.opening_graph import OpeningGraph
from app.opening_roots import OpeningRoot, OpeningRoots


# Standard chess initial position (normalized 4-field FEN). This is the graph
# root_fen and the key under which the synthetic whole-repertoire hero row is
# persisted/served.
SYNTHETIC_INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
SYNTHETIC_ROOT_NAME = "Repertoire"
SYNTHETIC_ROOT_FAMILY = "__repertoire__"


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
    coverage_live_threshold: int = 2


def root_calc_config_fingerprint(config: RootCalcConfig | None = None) -> str:
    """Return a stable fingerprint for the active root scoring configuration."""
    config = config or RootCalcConfig()
    payload = "|".join(
        f"{config_field.name}={getattr(config, config_field.name)!r}"
        for config_field in fields(config)
    )
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
        node = self._overlay_nodes.get(fen)
        quality_sum = node.quality_sum if node is not None else 0.0
        quality_count = node.quality_count if node is not None else 0
        return (quality_sum + self.config.alpha) / (
            quality_count + self.config.alpha + self.config.beta
        )

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
            score_sum = confidence_sum = coverage_sum = depth_sum = 0.0
            for child, weight in weights.items():
                child_score, child_conf, child_cov, child_depth = self._calc(
                    child, perfect
                )
                score_sum += weight * child_score
                confidence_sum += weight * child_conf
                coverage_sum += (
                    weight
                    * float(self._subtree_is_locally_covered(child))
                    * child_cov
                )
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

    def _base_root_score(self, root: OpeningRoot) -> RootScore:
        key = _normalized(root.opening_key)
        score, confidence, coverage, depth = self._calc(key, False)
        perfect_score, perfect_confidence, _, _ = self._calc(key, True)
        reachable = self._get_reachable(key)
        sample_size = sum(
            node.quality_count
            for fen in reachable
            if (node := self._overlay_nodes.get(fen)) is not None
        )
        # Distinct games over the reachable subtree: union of per-node session
        # ids. Unlike ``sample_size`` (move-observations), a single game played
        # through nine subtree positions counts once.
        game_count = len(
            {
                session_id
                for fen in reachable
                if (node := self._overlay_nodes.get(fen)) is not None
                for session_id in node.session_ids
            }
        )
        touches = [
            touch
            for fen in reachable
            if (node := self._overlay_nodes.get(fen)) is not None
            for touch in (node.last_live_at, node.last_review_at)
            if touch is not None
        ]
        return RootScore(
            opening_key=root.opening_key,
            opening_name=root.opening_name,
            opening_family=root.opening_family,
            player_color=self.player_color,
            opening_score=100.0 * score / perfect_score if perfect_score > 0 else 0.0,
            confidence=(
                100.0 * confidence / perfect_confidence
                if perfect_confidence > 0
                else 0.0
            ),
            coverage=100.0 * coverage,
            weighted_depth=depth,
            sample_size=sample_size,
            game_count=game_count,
            last_practiced_at=max(touches) if touches else None,
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
    config = config or RootCalcConfig()
    now = now or datetime.now(timezone.utc)
    named_roots = _iter_named_roots(roots)
    if not any(node.quality_count > 0 for node in overlay.nodes.values()):
        # Early return before any calculator is built. Still populate telemetry
        # with well-formed zeros (+ structural root counts) so empty/low-evidence
        # pairs get counters rather than None.
        if telemetry is not None:
            telemetry.named_root_count = len(named_roots)
            telemetry.raw_middlegame_root_count = _raw_middlegame_root_count(named_roots)
            telemetry.unscored_root_count = len(named_roots)
        return {}, set()
    calculator = _SharedCalculator(
        player_color, graph, overlay, roots, config, now, debug=debug
    )
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
    result = calculator.compute_roots(
        selected, include_branch_summaries=include_branch_summaries
    )
    if telemetry is not None:
        telemetry.named_root_count = len(named_roots)
        telemetry.actual_key_count = sum(
            1 for key in calculator._metrics if not key[1]
        )
        telemetry.perfect_key_count = sum(
            1 for key in calculator._metrics if key[1]
        )
        telemetry.calculation_misses = calculator.calculation_misses
        telemetry.raw_middlegame_root_count = _raw_middlegame_root_count(named_roots)
        telemetry.unscored_root_count = sum(
            1 for root in named_roots if root.opening_key not in result
        )
    return result, eligible


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
