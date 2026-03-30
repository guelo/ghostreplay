from __future__ import annotations

import math
import hashlib
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from collections import deque
from typing import Set, Dict, List, Tuple

from app.fen import active_color
from app.opening_evidence import EvidenceOverlay
from app.opening_graph import OpeningGraph
from app.opening_roots import OpeningRoots, OpeningRoot


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
    book_exit_extension_user_decisions: int = 2


def root_calc_config_fingerprint(config: RootCalcConfig | None = None) -> str:
    """Return a stable fingerprint for the active root scoring configuration."""
    if config is None:
        config = RootCalcConfig()
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
    last_practiced_at: datetime | None
    strongest_branch: BranchSummary | None
    weakest_branch: BranchSummary | None
    underexposed_branch: BranchSummary | None
    computed_at: datetime
    debug_nodes: list[NodeDebug]


class _Calculator:
    def __init__(
        self,
        opening_key: str,
        player_color: str,
        graph: OpeningGraph,
        overlay: EvidenceOverlay,
        roots: OpeningRoots,
        config: RootCalcConfig,
        now: datetime,
        debug: bool,
        include_branch_summaries: bool = True,
    ) -> None:
        self.opening_key = opening_key
        self.player_color = player_color
        self.graph = graph
        self.overlay = overlay
        self.roots = roots
        self.config = config
        self.now = now
        self.debug = debug
        self.include_branch_summaries = include_branch_summaries

        self.root_node = self.roots.get_root(opening_key)
        
        # Domains
        self.in_book_fens: set[str] = set()
        self.extension_fens: dict[str, int] = {} # fen -> user_decisions depth
        
        self.in_book_edges: set[tuple[str, str]] = set() # (parent_fen, child_fen)

        # Memoization & Helpers
        self._memo_score: dict[str, float] = {}
        self._memo_perfect_score: dict[str, float] = {}
        self._memo_confidence: dict[str, float] = {}
        self._memo_perfect_conf: dict[str, float] = {}
        self._memo_coverage: dict[str, float] = {}
        self._memo_depth: dict[str, float] = {}

        self._memo_reachable: dict[str, set[str]] = {}
        self._memo_ghost_target: dict[str, bool] = {}
        self._memo_global_ghost_target: dict[str, bool] = {}
        self._memo_graph_weights: dict[str, dict[str, float]] = {}
        
        self.debug_nodes: dict[str, NodeDebug] = {}

        # Subtree caches
        self._subtree_live_cache: dict[str, int] = {}
        self._subtree_review_cache: dict[str, int] = {}

        # Cycle guard stack
        self._active_stack: set[str] = set()

        # Scored domain tracking
        self.scored_user_nodes: set[str] = set()
        
        # Edge index for fast lookups
        self._overlay_edges_by_parent: dict[str, list[str]] = {}
        for parent_fen, child_fen in self.overlay.edges.keys():
            if parent_fen not in self._overlay_edges_by_parent:
                self._overlay_edges_by_parent[parent_fen] = []
            self._overlay_edges_by_parent[parent_fen].append(child_fen)

        if self.root_node:
            self._build_domains()

    def _build_domains(self) -> None:
        # 1a. In book owned subtree
        queue = deque([self.opening_key])
        self.in_book_fens.add(self.opening_key)
        
        while queue:
            fen = queue.popleft()
            node = self.graph.get_node(fen)
            if node is None:
                continue
                
            for child_fen in node.children.values():
                if self.opening_key in self.roots.owning_root_keys(child_fen):
                    if child_fen not in self.in_book_fens:
                        self.in_book_fens.add(child_fen)
                        queue.append(child_fen)
                    self.in_book_edges.add((fen, child_fen))

        # 1b. Off book extension subtree
        # Start from edges that leave in-book subtree (both book exits and early user/opponent exits)
        extension_queue = deque()
        for parent_fen in self.in_book_fens:
            for child_fen in self._overlay_edges_by_parent.get(parent_fen, []):
                edge_key = (parent_fen, child_fen)
                if edge_key not in self.in_book_edges:
                    p_node = self.graph.get_node(parent_fen)
                    is_graph_edge = p_node is not None and child_fen in p_node.children.values()
                    if not is_graph_edge:
                        p_color = active_color(parent_fen)
                        cost = 1 if p_color == self.player_color else 0
                        if cost <= self.config.book_exit_extension_user_decisions:
                            extension_queue.append((child_fen, cost))

        while extension_queue:
            fen, cost = extension_queue.popleft()
            
            # keep min cost if multiple paths
            if fen in self.extension_fens and self.extension_fens[fen] <= cost:
                continue
            self.extension_fens[fen] = cost
            
            # Find next edges in overlay
            for c_fen in self._overlay_edges_by_parent.get(fen, []):
                p_color = active_color(fen)
                next_cost = cost + (1 if p_color == self.player_color else 0)
                if next_cost <= self.config.book_exit_extension_user_decisions:
                    extension_queue.append((c_fen, next_cost))

    def _is_in_domain(self, fen: str) -> bool:
        return fen in self.in_book_fens or fen in self.extension_fens

    def _get_children(self, fen: str) -> list[str]:
        """Returns scored children of a node based on domain classification."""
        children = []
        if fen in self.in_book_fens:
            node = self.graph.get_node(fen)
            if node:
                for c_fen in node.children.values():
                    if c_fen in self.in_book_fens:
                        children.append(c_fen)
            # Add extension edges leaving this node
            for c_fen in self._overlay_edges_by_parent.get(fen, []):
                edge_key = (fen, c_fen)
                if edge_key not in self.in_book_edges and c_fen in self.extension_fens:
                    children.append(c_fen)
        elif fen in self.extension_fens:
            # Extension node: only overlay continuation edges
            for c_fen in self._overlay_edges_by_parent.get(fen, []):
                if c_fen in self.extension_fens:
                    children.append(c_fen)
        return children

    def _get_reachable(self, fen: str) -> set[str]:
        if fen in self._memo_reachable:
            return self._memo_reachable[fen]
        
        reachable = set()
        stack = [fen]
        visited = set()
        
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            reachable.add(curr)
            for c in self._get_children(curr):
                stack.append(c)
                
        self._memo_reachable[fen] = reachable
        return reachable

    def _subtree_has_ghost_target(self, fen: str) -> bool:
        if fen in self._memo_ghost_target:
            return self._memo_ghost_target[fen]
        
        res = False
        for r_fen in self._get_reachable(fen):
            node_ev = self.overlay.nodes.get(r_fen)
            if node_ev and node_ev.is_ghost_target:
                res = True
                break
        self._memo_ghost_target[fen] = res
        return res

    def _global_has_ghost_target(self, fen: str) -> bool:
        if fen in self._memo_global_ghost_target:
            return self._memo_global_ghost_target[fen]
            
        stack = [fen]
        visited = set()
        res = False
        
        while stack:
            curr = stack.pop()
            if curr in visited:
                continue
            visited.add(curr)
            
            node_ev = self.overlay.nodes.get(curr)
            if node_ev and node_ev.is_ghost_target:
                res = True
                break
                
            node = self.graph.get_node(curr)
            if node:
                for c in node.children.values():
                    stack.append(c)
                    
            for c_fen in self._overlay_edges_by_parent.get(curr, []):
                stack.append(c_fen)
                    
        self._memo_global_ghost_target[fen] = res
        return res

    def _subtree_coverage_totals(self, fen: str) -> tuple[int, int]:
        if fen in self._subtree_live_cache:
            return self._subtree_live_cache[fen], self._subtree_review_cache[fen]
            
        live_tot = 0
        rev_tot = 0
        for r_fen in self._get_reachable(fen):
            node_ev = self.overlay.nodes.get(r_fen)
            if node_ev:
                live_tot += node_ev.live_attempts
                rev_tot += node_ev.review_attempts
                
        self._subtree_live_cache[fen] = live_tot
        self._subtree_review_cache[fen] = rev_tot
        return live_tot, rev_tot

    def _subtree_is_locally_covered(self, fen: str) -> bool:
        live_tot, rev_tot = self._subtree_coverage_totals(fen)
        if live_tot >= self.config.coverage_live_threshold:
            return True
        if live_tot >= 1 and rev_tot >= 1:
            return True
        return False

    def _is_user_turn(self, fen: str) -> bool:
        return active_color(fen) == self.player_color

    def _get_prepared_children(self, fen: str) -> list[str]:
        """At a user node, return the list of prepared children FENs."""
        prepared = []
        for c_fen in self._get_children(fen):
            edge_ev = self.overlay.edges.get((fen, c_fen))
            is_prep = False
            if edge_ev:
                if edge_ev.live_attempts >= 2:
                    is_prep = True
                elif edge_ev.live_passes >= 1:
                    is_prep = True
            if not is_prep:
                if self._subtree_has_ghost_target(c_fen):
                    is_prep = True
            if is_prep:
                prepared.append(c_fen)
        return prepared

    def _get_weights(self, fen: str) -> dict[str, float]:
        """Return dict mapping child_fen to weight."""
        weights: dict[str, float] = {}
        is_user = self._is_user_turn(fen)
        children = self._get_children(fen)
        
        if is_user:
            prepared = self._get_prepared_children(fen)
            if not prepared:
                return {}
            total_basis = 0.0
            bases = {}
            for c_fen in prepared:
                edge_ev = self.overlay.edges.get((fen, c_fen))
                attempts = edge_ev.live_attempts if edge_ev else 0
                basis = attempts + self.config.rho
                bases[c_fen] = basis
                total_basis += basis
            for c_fen in prepared:
                weights[c_fen] = bases[c_fen] / total_basis
        else:
            if fen in self.in_book_fens:
                in_book_children = [c for c in children if c in self.in_book_fens]
                if in_book_children:
                    w = 1.0 / len(in_book_children)
                    for c in in_book_children:
                        weights[c] = w
                elif children:
                    w = 1.0 / len(children)
                    for c in children:
                        weights[c] = w
            else:
                if children:
                    w = 1.0 / len(children)
                    for c in children:
                        weights[c] = w
        return weights

    def _get_p_n(self, fen: str) -> float:
        node_ev = self.overlay.nodes.get(fen)
        if node_ev:
            attempts = node_ev.live_passes + node_ev.live_fails
            p_n = (node_ev.live_passes + self.config.alpha) / (attempts + self.config.alpha + self.config.beta)
        else:
            p_n = self.config.alpha / (self.config.alpha + self.config.beta)
        return p_n

    def _get_c_n_and_components(self, fen: str) -> tuple[float, float, float, float]:
        """Returns c_n, sample_conf, freshness, evidence_total."""
        node_ev = self.overlay.nodes.get(fen)
        if not node_ev:
            return 0.0, 0.0, 0.0, 0.0
            
        last_live = node_ev.last_live_at
        last_rev = node_ev.last_review_at
        last_touch = None
        if last_live and last_rev:
            last_touch = max(last_live, last_rev)
        elif last_live:
            last_touch = last_live
        elif last_rev:
            last_touch = last_rev
            
        if not last_touch:
            return 0.0, 0.0, 0.0, 0.0
            
        evidence_n = node_ev.live_attempts + self.config.lambda_review * node_ev.review_attempts
        sample_conf = 1.0 - math.exp(-evidence_n / self.config.k_evidence)
        
        days_diff = (self.now - last_touch).total_seconds() / 86400.0
        days_diff = max(0.0, days_diff)
        freshness = math.exp(-days_diff / self.config.half_life_days)
        
        c_n = sample_conf * freshness
        return c_n, sample_conf, freshness, evidence_n

    def _record_debug(self, fen: str, is_user: bool, weights: dict[str, float], prepared: list[str]) -> None:
        if not self.debug:
            return
        
        if fen in self.debug_nodes:
            return
            
        node_ev = self.overlay.nodes.get(fen)
        last_touch = None
        days_since = 0.0
        if node_ev:
            if node_ev.last_live_at and node_ev.last_review_at:
                last_touch = max(node_ev.last_live_at, node_ev.last_review_at)
            else:
                last_touch = node_ev.last_live_at or node_ev.last_review_at
            if last_touch:
                days_since = max(0.0, (self.now - last_touch).total_seconds() / 86400.0)

        c_n, sc, fresh, ev_tot = self._get_c_n_and_components(fen)
        sl, sr = self._subtree_coverage_totals(fen)
        
        self.debug_nodes[fen] = NodeDebug(
            fen=fen,
            is_user_turn=is_user,
            in_book=fen in self.in_book_fens,
            is_extension_node=fen in self.extension_fens,
            p_n=self._get_p_n(fen) if is_user else 1.0,
            c_n=c_n if is_user else 1.0,
            sample_conf=sc,
            freshness=fresh,
            evidence_total=ev_tot,
            days_since_last_touch=days_since,
            last_touch_at=last_touch,
            live_attempts=node_ev.live_attempts if node_ev else 0,
            live_passes=node_ev.live_passes if node_ev else 0,
            review_attempts=node_ev.review_attempts if node_ev else 0,
            prepared_children=prepared,
            weights=weights,
            subtree_live_attempts=sl,
            subtree_review_attempts=sr,
            covered_locally=self._subtree_is_locally_covered(fen),
            raw_score=0.0,
            raw_confidence=0.0,
            raw_coverage=0.0,
            raw_depth=0.0,
            is_leaf=False # updated later
        )

    def _calc_metrics(self, fen: str, is_perfect: bool = False) -> tuple[float, float, float, float]:
        """Returns (score, confidence, coverage, depth)."""
        cache_key = f"{fen}_perf" if is_perfect else fen
        
        if cache_key in self._memo_score:
            return self._memo_score[cache_key], self._memo_confidence[cache_key], self._memo_coverage[cache_key], self._memo_depth[cache_key]

        # Cycle guard
        if fen in self._active_stack:
            # Reached a back edge, break the cycle by returning pessimistic defaults
            return 0.0, 0.0, 0.0, 0.0
            
        self._active_stack.add(fen)

        is_user = self._is_user_turn(fen)
        if is_user:
            self.scored_user_nodes.add(fen)

        weights = self._get_weights(fen)
        prepared = self._get_prepared_children(fen) if is_user else []
        
        self._record_debug(fen, is_user, weights, prepared)

        # Base cases
        is_leaf = False
        domain_children = self._get_children(fen)
        if not domain_children:
            is_leaf = True

        if self.debug and cache_key in self.debug_nodes:
            self.debug_nodes[cache_key].is_leaf = is_leaf

        s_val, c_val, cov_val, d_val = 0.0, 0.0, 0.0, 0.0

        if is_leaf:
            if is_user:
                p_n = 1.0 if is_perfect else self._get_p_n(fen)
                c_n, _, _, _ = self._get_c_n_and_components(fen)
                conf = 1.0 if is_perfect else c_n
                
                s_val = p_n
                c_val = conf
                cov_val = 1.0
                d_val = p_n
            else:
                s_val = 1.0
                c_val = 1.0
                cov_val = 1.0
                d_val = 0.0
        else:
            # Recursive step
            if is_user:
                p_n = 1.0 if is_perfect else self._get_p_n(fen)
                c_n, _, _, _ = self._get_c_n_and_components(fen)
                conf = 1.0 if is_perfect else c_n

                s_sum = 0.0
                c_sum = 0.0
                cov_sum = 0.0
                d_sum = 0.0

                if not prepared:
                    s_val = p_n
                    c_val = conf
                    cov_val = 0.0
                    d_val = p_n
                else:
                    for c_fen, w in weights.items():
                        c_s, c_c, c_cov, c_d = self._calc_metrics(c_fen, is_perfect)
                        s_sum += w * c_s
                        c_sum += w * c_c
                        cov_sum += w * c_cov
                        d_sum += w * c_d
                    
                    s_val = p_n * (1.0 + self.config.gamma * s_sum)
                    c_val = conf * c_sum
                    cov_val = cov_sum
                    d_val = p_n * (1.0 + self.config.gamma * d_sum)
            else:
                s_sum = 0.0
                c_sum = 0.0
                cov_sum = 0.0
                d_sum = 0.0
                
                for c_fen, w in weights.items():
                    c_s, c_c, c_cov, c_d = self._calc_metrics(c_fen, is_perfect)
                    covered_e = 1.0 if self._subtree_is_locally_covered(c_fen) else 0.0
                    
                    s_sum += w * c_s
                    c_sum += w * c_c
                    cov_sum += w * covered_e * c_cov
                    d_sum += w * c_d
                
                s_val = s_sum
                c_val = c_sum
                cov_val = cov_sum
                d_val = d_sum

        self._active_stack.remove(fen)
        
        self._memo_score[cache_key] = s_val
        self._memo_confidence[cache_key] = c_val
        self._memo_coverage[cache_key] = cov_val
        self._memo_depth[cache_key] = d_val

        if not is_perfect and self.debug and fen in self.debug_nodes:
            self.debug_nodes[fen].raw_score = s_val
            self.debug_nodes[fen].raw_confidence = c_val
            self.debug_nodes[fen].raw_coverage = cov_val
            self.debug_nodes[fen].raw_depth = d_val

        return s_val, c_val, cov_val, d_val

    def _get_graph_weights(self, fen: str) -> dict[str, float]:
        """Returns weights for all graph children, plus extension children if off-book."""
        if fen in self._memo_graph_weights:
            return self._memo_graph_weights[fen]
            
        weights: dict[str, float] = {}
        is_user = self._is_user_turn(fen)
        
        node = self.graph.get_node(fen)
        graph_children = list(node.children.values()) if node else []
        
        ext_children = []
        for c_fen in self._overlay_edges_by_parent.get(fen, []):
            if not node or c_fen not in node.children.values():
                ext_children.append(c_fen)
                
        all_children = graph_children + ext_children
        
        if is_user:
            prepared = []
            for c_fen in all_children:
                edge_ev = self.overlay.edges.get((fen, c_fen))
                is_prep = False
                if edge_ev:
                    if edge_ev.live_attempts >= 2:
                        is_prep = True
                    elif edge_ev.live_passes >= 1:
                        is_prep = True
                if not is_prep:
                    if self._global_has_ghost_target(c_fen):
                        is_prep = True
                if is_prep:
                    prepared.append(c_fen)
                    
            if prepared:
                total_basis = 0.0
                bases = {}
                for c_fen in prepared:
                    edge_ev = self.overlay.edges.get((fen, c_fen))
                    attempts = edge_ev.live_attempts if edge_ev else 0
                    basis = attempts + self.config.rho
                    bases[c_fen] = basis
                    total_basis += basis
                for c_fen in prepared:
                    weights[c_fen] = bases[c_fen] / total_basis
            else:
                if graph_children:
                    w = 1.0 / len(graph_children)
                    for c in graph_children:
                        weights[c] = w
                elif ext_children:
                    w = 1.0 / len(ext_children)
                    for c in ext_children:
                        weights[c] = w
        else:
            if graph_children:
                w = 1.0 / len(graph_children)
                for c in graph_children:
                    weights[c] = w
            elif ext_children:
                w = 1.0 / len(ext_children)
                for c in ext_children:
                    weights[c] = w
                    
        self._memo_graph_weights[fen] = weights
        return weights

    def _path_importance(self, target_fen: str, current_fen: str, current_weight: float, visited: set[str] = None) -> float:
        if current_fen == target_fen:
            return current_weight
            
        if visited is None:
            visited = set()
        if current_fen in visited:
            return 0.0
        visited.add(current_fen)
        
        weights = self._get_graph_weights(current_fen)
        
        total = 0.0
        if weights:
            for c_fen, w in weights.items():
                total += self._path_importance(target_fen, c_fen, current_weight * w, visited)
                
        visited.remove(current_fen)
        return total

    def _get_importance(self, target_fen: str) -> float:
        return self._path_importance(target_fen, self.opening_key, 1.0)

    def compute(self) -> RootScore:
        if not self.root_node:
            raise ValueError(f"Unknown root: {self.opening_key}")
            
        # Execute normal pass
        self.scored_user_nodes.clear()
        s_val, c_val, cov_val, d_val = self._calc_metrics(self.opening_key, False)
        
        # Execute perfect pass
        perf_s, perf_c, _, _ = self._calc_metrics(self.opening_key, True)

        opening_score = 0.0
        if perf_s > 0:
            opening_score = 100.0 * s_val / perf_s
            
        confidence = 0.0
        if perf_c > 0:
            confidence = 100.0 * c_val / perf_c
            
        coverage = 100.0 * cov_val

        strongest_branch = None
        weakest_branch = None
        underexposed_branch = None
        if self.include_branch_summaries:
            max_s = -1.0
            min_s = float('inf')

            immediate_children_keys = list(self.root_node.child_keys)
            for c_key in immediate_children_keys:
                c_root = self.roots.get_root(c_key)
                if not c_root:
                    continue
                imp = self._get_importance(c_key)
                if imp > 0:
                    child_calc = _Calculator(
                        c_key,
                        self.player_color,
                        self.graph,
                        self.overlay,
                        self.roots,
                        self.config,
                        self.now,
                        False,
                        include_branch_summaries=False,
                    )
                    child_score = child_calc.compute()

                    bs = BranchSummary(c_key, c_root.opening_name, child_score.opening_score)
                    if child_score.opening_score > max_s:
                        max_s = child_score.opening_score
                        strongest_branch = bs
                    if child_score.opening_score < min_s:
                        min_s = child_score.opening_score
                        weakest_branch = bs

            max_gap = -1.0

            def get_all_descendants(r_key: str, desc: set[str]):
                r = self.roots.get_root(r_key)
                if r:
                    for ck in r.child_keys:
                        if ck not in desc:
                            desc.add(ck)
                            get_all_descendants(ck, desc)

            desc_keys: set[str] = set()
            get_all_descendants(self.opening_key, desc_keys)

            for d_key in desc_keys:
                d_root = self.roots.get_root(d_key)
                if not d_root:
                    continue

                imp = self._get_importance(d_key)
                if imp > 0:
                    d_calc = _Calculator(
                        d_key,
                        self.player_color,
                        self.graph,
                        self.overlay,
                        self.roots,
                        self.config,
                        self.now,
                        False,
                        include_branch_summaries=False,
                    )
                    if not d_calc._subtree_is_locally_covered(d_key):
                        d_res = d_calc.compute()

                        gap = imp * (1.0 - d_res.coverage / 100.0)
                        if gap > max_gap:
                            max_gap = gap
                            underexposed_branch = BranchSummary(d_key, d_root.opening_name, gap)

        sample_size = 0
        last_practiced_at = None
        for fn in self.scored_user_nodes:
            node_ev = self.overlay.nodes.get(fn)
            if node_ev:
                sample_size += node_ev.live_attempts
                if node_ev.last_live_at:
                    if not last_practiced_at or node_ev.last_live_at > last_practiced_at:
                        last_practiced_at = node_ev.last_live_at
                if node_ev.last_review_at:
                    if not last_practiced_at or node_ev.last_review_at > last_practiced_at:
                        last_practiced_at = node_ev.last_review_at

        return RootScore(
            opening_key=self.opening_key,
            opening_name=self.root_node.opening_name,
            opening_family=self.root_node.opening_family,
            player_color=self.player_color,
            opening_score=opening_score,
            confidence=confidence,
            coverage=coverage,
            weighted_depth=d_val,
            sample_size=sample_size,
            last_practiced_at=last_practiced_at,
            strongest_branch=strongest_branch,
            weakest_branch=weakest_branch,
            underexposed_branch=underexposed_branch,
            computed_at=self.now,
            debug_nodes=list(self.debug_nodes.values()) if self.debug else [],
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
    if config is None:
        config = RootCalcConfig()
    if now is None:
        now = datetime.now(timezone.utc)

    calc = _Calculator(
        opening_key=opening_key,
        player_color=player_color,
        graph=graph,
        overlay=overlay,
        roots=roots,
        config=config,
        now=now,
        debug=debug,
        include_branch_summaries=include_branch_summaries,
    )
    return calc.compute()
