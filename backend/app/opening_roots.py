"""Derive named subtree roots, family groupings, and descendant relationships
from the opening graph.

Boundary roots are positions where the opening label changes from a parent's
label. Each root carries a canonical family name for dashboard grouping,
parent/child edges forming a DAG, and a position-ownership map for downstream
scoring.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from dataclasses import dataclass

from app.opening_graph import OpeningGraph, get_opening_graph

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Family derivation
# ---------------------------------------------------------------------------

FAMILY_ALIASES: dict[str, str] = {
    "Sicilian": "Sicilian Defense",
    "Spanish": "Ruy Lopez",
    "QGD": "Queen's Gambit Declined",
    "QGA": "Queen's Gambit Accepted",
    "English": "English Opening",
    "French": "French Defense",
    "Caro-Kann": "Caro-Kann Defense",
    "Nimzo-Indian": "Nimzo-Indian Defense",
    "King's Indian": "King's Indian Defense",
}


def derive_family(opening_name: str) -> str:
    """Extract canonical family label from an opening name.

    Splits on the first colon, then applies alias normalization.
    """
    raw = opening_name.split(":", 1)[0].strip()
    return FAMILY_ALIASES.get(raw, raw)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OpeningRoot:
    """A named subtree root in the opening graph."""

    opening_key: str  # normalized 4-field FEN (durable identity)
    opening_name: str  # full name from eco.byPosition
    opening_family: str  # canonicalized family label
    eco: str | None  # ECO code
    depth: int  # BFS depth from graph root
    parent_keys: frozenset[str]  # immediate ancestor boundary root FENs
    child_keys: frozenset[str]  # immediate descendant boundary root FENs


class OpeningRoots:
    """Registry of named subtree roots derived from the opening graph.

    The root structure is a DAG (not a tree) — boundary roots can have
    multiple parent roots due to transpositions. No canonical tree projection
    is baked in; consumers choose how to handle multi-parent cases.
    """

    def __init__(
        self,
        roots: dict[str, OpeningRoot],
        ownership: dict[str, frozenset[str]],
    ) -> None:
        self._roots = roots  # opening_key -> OpeningRoot
        self._ownership = ownership  # any graph FEN -> deepest owning root keys

        # Build secondary indexes
        self._families: dict[str, list[OpeningRoot]] = {}
        self._children_of: dict[str | None, list[OpeningRoot]] = {None: []}
        for root in roots.values():
            self._families.setdefault(root.opening_family, []).append(root)
            if not root.parent_keys:
                self._children_of[None].append(root)
            for parent_key in root.parent_keys:
                self._children_of.setdefault(parent_key, []).append(root)
        # Sort each family's roots by depth for stable ordering
        for members in self._families.values():
            members.sort(key=lambda r: (r.depth, r.opening_key))
        for children in self._children_of.values():
            children.sort(key=lambda r: (r.depth, r.opening_key))

        # Descendant closures are memoized on demand to avoid eager
        # materialization of every reachable pair in the DAG.
        self._descendants: dict[str, list[OpeningRoot]] = {}
        self._fingerprint = _compute_opening_roots_fingerprint(roots, ownership)

    # -- Core lookups --

    def get_root(self, opening_key: str) -> OpeningRoot | None:
        return self._roots.get(opening_key)

    def get_family(self, family_name: str) -> list[OpeningRoot]:
        return list(self._families.get(family_name, []))

    def get_families(self) -> list[str]:
        """Return sorted list of canonical family names."""
        return sorted(self._families.keys())

    def get_children(self, parent_key: str | None) -> list[OpeningRoot]:
        """Return immediate DAG children for the given root key.

        `None` returns top-level roots with no parent boundary root.
        """
        return list(self._children_of.get(parent_key, []))

    def get_descendants(self, root_key: str) -> list[OpeningRoot]:
        """Return all descendant roots, deduplicated across DAG overlaps."""
        cached = self._descendants.get(root_key)
        if cached is not None:
            return list(cached)

        seen: set[str] = set()
        ordered: list[OpeningRoot] = []
        queue: deque[OpeningRoot] = deque(self._children_of.get(root_key, []))
        while queue:
            child = queue.popleft()
            if child.opening_key in seen:
                continue
            seen.add(child.opening_key)
            ordered.append(child)
            queue.extend(self._children_of.get(child.opening_key, []))

        self._descendants[root_key] = ordered
        return list(ordered)

    def is_descendant_of(self, a_key: str, b_key: str) -> bool:
        """Return True if root *a* is a descendant of root *b* in the DAG."""
        if a_key == b_key:
            return False
        # BFS upward through parent_keys from a looking for b
        visited: set[str] = set()
        queue = deque([a_key])
        while queue:
            current = queue.popleft()
            root = self._roots.get(current)
            if root is None:
                continue
            for pk in root.parent_keys:
                if pk == b_key:
                    return True
                if pk not in visited:
                    visited.add(pk)
                    queue.append(pk)
        return False

    # -- Ownership (DAG-aware) --

    def owning_root_keys(self, fen: str) -> frozenset[str]:
        """Return the set of deepest boundary root keys that are ancestors
        of the given graph position.

        Used by rootcalc to determine which roots should include this
        position in their score.
        """
        return self._ownership.get(fen, frozenset())

    @property
    def root_count(self) -> int:
        return len(self._roots)

    @property
    def family_count(self) -> int:
        return len(self._families)

    @property
    def fingerprint(self) -> str:
        return self._fingerprint


def _compute_opening_roots_fingerprint(
    roots: dict[str, OpeningRoot],
    ownership: dict[str, frozenset[str]],
) -> str:
    """Return a stable fingerprint for the scoring-relevant opening registry."""
    payload = "|".join(
        [
            *[
                (
                    f"root\t{root.opening_key}\t{root.opening_name}\t{root.opening_family}"
                    f"\t{root.eco or ''}\t{root.depth}"
                    f"\t{','.join(sorted(root.parent_keys))}"
                    f"\t{','.join(sorted(root.child_keys))}"
                )
                for root in sorted(roots.values(), key=lambda item: item.opening_key)
            ],
            *[
                f"own\t{fen}\t{','.join(sorted(owner_keys))}"
                for fen, owner_keys in sorted(ownership.items())
            ],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def opening_roots_fingerprint(roots: OpeningRoots) -> str:
    """Return the stable fingerprint for the current opening-root registry."""
    return roots.fingerprint


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _identify_boundary_roots(graph: OpeningGraph) -> set[str]:
    """Return FENs of all boundary root positions in the graph.

    A node N is a boundary root if N.name is not None and at least one
    graph-parent has a different name (or is the unnamed graph root).
    """
    boundary: set[str] = set()
    for node in graph._nodes.values():
        if node.name is None:
            continue
        if not node.parents:
            # Named node with no parents (shouldn't happen except root)
            continue
        for parent_fen, _ in node.parents:
            parent = graph.get_node(parent_fen)
            if parent is not None and parent.name != node.name:
                boundary.add(node.fen)
                break
    return boundary


def _compute_depths(graph: OpeningGraph) -> dict[str, int]:
    """BFS from graph root to compute shortest depth for every node."""
    depths: dict[str, int] = {graph.root_fen: 0}
    queue: deque[str] = deque([graph.root_fen])
    while queue:
        fen = queue.popleft()
        node = graph.get_node(fen)
        if node is None:
            continue
        d = depths[fen]
        for _uci, child_fen in node.children.items():
            if child_fen not in depths:
                depths[child_fen] = d + 1
                queue.append(child_fen)
    return depths


def _build_root_dag(
    graph: OpeningGraph,
    boundary_fens: set[str],
    depths: dict[str, int],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Build parent_keys and child_keys for each boundary root.

    For each boundary root B, its parent_keys are the boundary roots (or
    nothing, if graph root is the immediate ancestor) reachable by walking
    B's graph-parents upward until hitting another boundary root.

    Returns (parent_map, child_map) where each maps boundary FEN -> set of
    boundary FENs.
    """
    parent_map: dict[str, set[str]] = {fen: set() for fen in boundary_fens}
    child_map: dict[str, set[str]] = {fen: set() for fen in boundary_fens}

    for br_fen in boundary_fens:
        node = graph.get_node(br_fen)
        if node is None:
            continue
        # Walk each graph-parent chain upward until hitting a boundary root
        # or the graph root
        visited: set[str] = set()
        walk_queue: deque[str] = deque()
        for pfen, _ in node.parents:
            if pfen not in visited:
                visited.add(pfen)
                walk_queue.append(pfen)

        while walk_queue:
            cur = walk_queue.popleft()
            if cur in boundary_fens:
                # cur is a boundary root — it's an immediate parent root
                # of br_fen. Identity is FEN-based: same-name roots at
                # different positions are still distinct edges.
                if cur != br_fen:
                    parent_map[br_fen].add(cur)
                    child_map[cur].add(br_fen)
                # Stop walking this chain (don't go past boundary roots)
                continue
            # cur is not a boundary root — keep walking up
            cur_node = graph.get_node(cur)
            if cur_node is None:
                continue
            for gpfen, _ in cur_node.parents:
                if gpfen not in visited:
                    visited.add(gpfen)
                    walk_queue.append(gpfen)

    return parent_map, child_map


def _topological_order(graph: OpeningGraph) -> list[str]:
    """Compute topological ordering of the opening graph via Kahn's algorithm.

    Returns list of FENs in topological order (all parents before children).
    """
    # Compute in-degrees
    in_degree: dict[str, int] = {fen: 0 for fen in graph._nodes}
    for node in graph._nodes.values():
        for _uci, child_fen in node.children.items():
            if child_fen in in_degree:
                in_degree[child_fen] += 1

    # Start with zero-in-degree nodes
    queue: deque[str] = deque()
    for fen, deg in in_degree.items():
        if deg == 0:
            queue.append(fen)

    order: list[str] = []
    while queue:
        fen = queue.popleft()
        order.append(fen)
        node = graph.get_node(fen)
        if node is None:
            continue
        for _uci, child_fen in node.children.items():
            if child_fen in in_degree:
                in_degree[child_fen] -= 1
                if in_degree[child_fen] == 0:
                    queue.append(child_fen)

    if len(order) != len(in_degree):
        unprocessed = len(in_degree) - len(order)
        raise RuntimeError(
            f"Opening graph contains a cycle: {unprocessed} nodes "
            "were not reachable in topological order"
        )

    return order


def _build_ownership(
    graph: OpeningGraph,
    boundary_fens: set[str],
) -> dict[str, frozenset[str]]:
    """Build position-to-owning-roots map using topological sort.

    For each graph position, compute the set of deepest boundary root
    ancestors. A boundary root resets ownership (does not inherit from
    parents); a non-boundary node inherits the union of all parents'
    ownership sets.
    """
    topo = _topological_order(graph)
    ownership: dict[str, frozenset[str]] = {}

    for fen in topo:
        if fen in boundary_fens:
            ownership[fen] = frozenset({fen})
        else:
            # Union of all parents' ownership
            node = graph.get_node(fen)
            if node is None:
                ownership[fen] = frozenset()
                continue
            merged: set[str] = set()
            for parent_fen, _ in node.parents:
                parent_own = ownership.get(parent_fen)
                if parent_own:
                    merged.update(parent_own)
            ownership[fen] = frozenset(merged)

    return ownership


def build_opening_roots(graph: OpeningGraph) -> OpeningRoots:
    """Derive named subtree roots and their relationships from the graph."""
    boundary_fens = _identify_boundary_roots(graph)
    depths = _compute_depths(graph)
    parent_map, child_map = _build_root_dag(graph, boundary_fens, depths)
    ownership = _build_ownership(graph, boundary_fens)

    roots: dict[str, OpeningRoot] = {}
    for fen in boundary_fens:
        node = graph.get_node(fen)
        if node is None or node.name is None:
            continue
        roots[fen] = OpeningRoot(
            opening_key=fen,
            opening_name=node.name,
            opening_family=derive_family(node.name),
            eco=node.eco,
            depth=depths.get(fen, 0),
            parent_keys=frozenset(parent_map.get(fen, set())),
            child_keys=frozenset(child_map.get(fen, set())),
        )

    logger.info(
        "opening_roots: %d boundary roots, %d families",
        len(roots),
        len({r.opening_family for r in roots.values()}),
    )
    return OpeningRoots(roots=roots, ownership=ownership)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_opening_roots: OpeningRoots | None = None


def get_opening_roots() -> OpeningRoots:
    """Return the singleton opening roots registry, building on first access."""
    global _opening_roots
    if _opening_roots is None:
        _opening_roots = build_opening_roots(get_opening_graph())
    return _opening_roots


def _reset_opening_roots_for_testing() -> None:
    """Clear the singleton so the next call rebuilds."""
    global _opening_roots
    _opening_roots = None
