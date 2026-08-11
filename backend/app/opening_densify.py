"""Transposition densification for drill routing.

The opening graph is built by replaying ECO move sequences, so it is nearly a
tree: a position recorded only under one move order has no edges for the other
orders that reach it. Drill routing is already transposition-tolerant (it BFSes
backwards over parents, and positions are keyed by normalized FEN), so the only
thing missing is the connecting edges.

This module derives those edges offline and exposes them through a `RoutingView`
that drill steering reads INSTEAD of the graph. The edges are deliberately NOT
merged into `OpeningGraphNode.children`:

    `graph.fingerprint` is computed from node children and gates the opening
    score cache and the frozen calibration-analysis artifact. Merging would
    mass-invalidate score caches and invalidate a calibration analysis merely to
    fix a drill-routing bug. A routing-only overlay leaves
    `graph.fingerprint` untouched.

Cycle safety: an edge is retained only when it strictly increases LONGEST-PATH
depth over the base DAG. Every base edge strictly increases that depth by
construction and every retained edge does so by the filter, so no cycle can
close in the combined graph. This matters because drill routing treats any
on-route position as valid — a cycle would let a player shuffle forever without
going off route. Minimum root depth is NOT a valid potential here: the base
graph contains an edge (d2f4) running from min-depth 16 to 15.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import deque
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import chess

from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _default_opening_data_dir_candidates,
    _fen_from_board,
    _opening_data_dir_has_files,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

ARTIFACT_FILENAME = "eco.transpositions.json"

# (parent_fen, uci, child_fen)
DensifiedEdge = tuple[str, str, str]


class DensificationError(RuntimeError):
    """The artifact is unusable: wrong schema, stale provenance, or invalid edges."""


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def graph_topology_fingerprint(graph: OpeningGraph) -> str:
    """SHA-256 over the graph's FEN key set and child edges — nothing else.

    This is the EXACT dependency of the densified edge set: the scan reads only
    which positions exist and which edges they already have. `graph.fingerprint`
    also folds in eco/name labels, so pinning the artifact to it would force a
    spurious ~40s regeneration on any label-only byPosition edit.

    Lives here rather than in opening_graph.py on purpose: opening_graph.py is
    pinned in the calibration source manifest, which hashes file BYTES, so even
    adding a pure function there would churn the scorer-source digest.
    """
    payload = "|".join(
        f"{fen}\t{','.join(f'{uci}:{child_fen}' for uci, child_fen in sorted(node.children.items()))}"
        for fen, node in sorted(graph._nodes.items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _edges_fingerprint(edges: tuple[DensifiedEdge, ...]) -> str:
    payload = "|".join(f"{parent}\t{uci}\t{child}" for parent, uci, child in edges)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Edge computation (offline / test only — never on the startup path)
# ---------------------------------------------------------------------------


def _board_for_fen(fen: str) -> chess.Board:
    return chess.Board(f"{fen} 0 1")


def _topological_order(graph: OpeningGraph) -> list[str]:
    """Kahn order over the base graph. Raises if the base graph is cyclic —
    the longest-path potential is undefined on a cyclic base, so densification
    must fail loudly rather than emit edges it cannot prove safe."""
    in_degree: dict[str, int] = {fen: 0 for fen in graph._nodes}
    for node in graph._nodes.values():
        for child_fen in node.children.values():
            if child_fen in in_degree:
                in_degree[child_fen] += 1

    queue: deque[str] = deque(fen for fen, deg in in_degree.items() if deg == 0)
    order: list[str] = []
    while queue:
        fen = queue.popleft()
        order.append(fen)
        node = graph._nodes[fen]
        for child_fen in node.children.values():
            if child_fen not in in_degree:
                continue
            in_degree[child_fen] -= 1
            if in_degree[child_fen] == 0:
                queue.append(child_fen)

    if len(order) != len(in_degree):
        raise DensificationError(
            "Base opening graph is cyclic: "
            f"{len(in_degree) - len(order)} nodes are not topologically orderable, "
            "so the longest-path progress filter is undefined"
        )
    return order


def longest_path_depths(graph: OpeningGraph) -> dict[str, int]:
    """depth[v] = max(depth[parent] + 1) over BASE-graph edges, in topological
    order. Defined only over base edges: the potential must sit on a fixed,
    acyclic base, never on the densified result it is used to filter."""
    order = _topological_order(graph)
    depths: dict[str, int] = {fen: 0 for fen in order}
    for fen in order:
        depth = depths[fen]
        for child_fen in graph._nodes[fen].children.values():
            if child_fen in depths and depths[child_fen] < depth + 1:
                depths[child_fen] = depth + 1
    return depths


def scan_transposition_edges(graph: OpeningGraph) -> tuple[DensifiedEdge, ...]:
    """Every legal move connecting two existing graph positions that the graph
    does not already record — UNFILTERED. ~37s over the production graph.

    Not safe to route over as-is: it closes cycles. Use compute_densified_edges.
    """
    fens = set(graph._nodes)
    edges: list[DensifiedEdge] = []
    for fen in sorted(fens):
        node = graph._nodes[fen]
        board = _board_for_fen(fen)
        for move in board.legal_moves:
            uci = move.uci()
            if uci in node.children:
                continue
            board.push(move)
            child_fen = _fen_from_board(board)
            board.pop()
            if child_fen in fens:
                edges.append((fen, uci, child_fen))
    return tuple(sorted(edges))


def compute_densified_edges(graph: OpeningGraph) -> tuple[DensifiedEdge, ...]:
    """The routing-safe transposition edges: every connection the scan found that
    strictly increases longest-path depth.

    ~37s over the production graph — this runs during regeneration and CI
    `--check`, never at app startup. Returns a sorted, deterministic tuple so
    `--check` can diff it exactly against the artifact.

    Dropped edges are real transpositions; they are simply the ones routing may
    not cross without risking a cycle.
    """
    depths = longest_path_depths(graph)
    return tuple(
        edge
        for edge in scan_transposition_edges(graph)
        if depths[edge[2]] > depths[edge[0]]
    )


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


class DensifiedEdges:
    """A deeply immutable set of routing-only edges.

    Immutability mirrors the frozen graph (opening_graph.py:136): the maps are
    MappingProxyType over copies and the values are tuples, so no caller can
    mutate the shared overlay. A frozen dataclass over plain dicts would not be.
    """

    __slots__ = ("_edges", "_children", "_parents", "_fingerprint")

    def __init__(self, edges: tuple[DensifiedEdge, ...]) -> None:
        children: dict[str, dict[str, str]] = {}
        parents: dict[str, list[tuple[str, str]]] = {}
        for parent_fen, uci, child_fen in edges:
            children.setdefault(parent_fen, {})[uci] = child_fen
            parents.setdefault(child_fen, []).append((parent_fen, uci))

        self._edges = edges
        self._children = MappingProxyType(
            {fen: MappingProxyType(dict(moves)) for fen, moves in children.items()}
        )
        self._parents = MappingProxyType(
            {fen: tuple(refs) for fen, refs in parents.items()}
        )
        self._fingerprint = _edges_fingerprint(edges)

    @property
    def edges(self) -> tuple[DensifiedEdge, ...]:
        return self._edges

    @property
    def fingerprint(self) -> str:
        """Namespaces the drill route cache. Not redundant with graph.fingerprint:
        the artifact can change without the graph changing."""
        return self._fingerprint

    def children_of(self, fen: str) -> Mapping[str, str]:
        return self._children.get(fen, _EMPTY_MOVES)

    def parents_of(self, fen: str) -> tuple[tuple[str, str], ...]:
        return self._parents.get(fen, ())

    def __len__(self) -> int:
        return len(self._edges)


_EMPTY_MOVES: Mapping[str, str] = MappingProxyType({})

EMPTY_DENSIFIED_EDGES = DensifiedEdges(())


class RoutingView:
    """The graph as drill routing sees it: base edges plus the overlay.

    Both directions read the same snapshot, so the backward BFS and the forward
    child lookups can never disagree about which edges exist.
    """

    __slots__ = ("_graph", "_overlay", "_merged_children")

    def __init__(
        self, graph: OpeningGraph, overlay: DensifiedEdges = EMPTY_DENSIFIED_EDGES
    ) -> None:
        self._graph = graph
        self._overlay = overlay
        # Only the ~2k positions that gained an edge need a merged map; every
        # other position hands back node.children, which is already read-only.
        merged: dict[str, Mapping[str, str]] = {}
        for parent_fen, uci, child_fen in overlay.edges:
            node = graph.get_node(parent_fen)
            if node is None:
                continue
            existing = merged.get(parent_fen)
            if existing is None:
                combined = dict(node.children)
            else:
                combined = dict(existing)
            combined[uci] = child_fen
            merged[parent_fen] = MappingProxyType(combined)
        self._merged_children = MappingProxyType(merged)

    @property
    def graph(self) -> OpeningGraph:
        return self._graph

    @property
    def overlay(self) -> DensifiedEdges:
        return self._overlay

    @property
    def graph_fingerprint(self) -> str:
        return self._graph.fingerprint

    @property
    def overlay_fingerprint(self) -> str:
        return self._overlay.fingerprint

    def has_position(self, fen: str) -> bool:
        return self._graph.has_position(fen)

    def get_node(self, fen: str) -> OpeningGraphNode | None:
        return self._graph.get_node(fen)

    def routing_children(self, fen: str) -> Mapping[str, str]:
        merged = self._merged_children.get(fen)
        if merged is not None:
            return merged
        node = self._graph.get_node(fen)
        if node is None:
            return _EMPTY_MOVES
        return node.children

    def routing_parents(self, fen: str) -> list[tuple[OpeningGraphNode, str]]:
        parents = self._graph.get_parents(fen)
        for parent_fen, uci in self._overlay.parents_of(fen):
            node = self._graph.get_node(parent_fen)
            if node is not None:
                parents.append((node, uci))
        return parents


# ---------------------------------------------------------------------------
# Artifact
# ---------------------------------------------------------------------------


def resolve_artifact_path() -> Path | None:
    """The artifact sits beside eco.json in the SAME resolved data dir, so it
    follows OPENING_DATA_DIR rather than a hardcoded backend/public/."""
    for data_dir in _default_opening_data_dir_candidates():
        if _opening_data_dir_has_files(data_dir):
            return data_dir / ARTIFACT_FILENAME
    return None


def serialize_edges(
    graph: OpeningGraph, edges: tuple[DensifiedEdge, ...], generated_at: str
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_topology_fingerprint": graph_topology_fingerprint(graph),
        "generated_at": generated_at,
        "edge_count": len(edges),
        "edges": [list(edge) for edge in edges],
    }


def load_densified_edges(graph: OpeningGraph, path: Path) -> DensifiedEdges:
    """Parse and validate the artifact against `graph`. Raises DensificationError
    on anything suspect; callers decide whether to fail or degrade.

    Validation is dict membership only — the explicit child_fen in each triple
    means no chess.Board pushes are needed, keeping this under ~100ms. Per-edge
    legality is NOT checked here; `densify_opening_graph.py --check` in CI is
    the guard for that, and for completeness, which per-edge checks cannot see.
    """
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise DensificationError(f"Cannot read {path}: {exc}") from exc

    # Every invalid shape must normalize to DensificationError: this is the ONLY
    # exception `_build_routing_view` degrades on, and a leaked AttributeError
    # from a non-object payload would escape the singleton uncached — 500ing
    # drill routing and re-raising per request for every other consumer.
    if not isinstance(payload, dict):
        raise DensificationError(
            f"{path}: payload must be a JSON object, got {type(payload).__name__}"
        )

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise DensificationError(
            f"{path}: schema_version {schema_version!r} != {SCHEMA_VERSION}"
        )

    expected_fingerprint = graph_topology_fingerprint(graph)
    actual_fingerprint = payload.get("graph_topology_fingerprint")
    if actual_fingerprint != expected_fingerprint:
        raise DensificationError(
            f"{path}: graph_topology_fingerprint {actual_fingerprint!r} does not "
            f"match the loaded graph ({expected_fingerprint!r}). Regenerate with "
            "scripts/densify_opening_graph.py."
        )

    raw_edges = payload.get("edges")
    if not isinstance(raw_edges, list):
        raise DensificationError(f"{path}: 'edges' must be a list")

    declared_count = payload.get("edge_count")
    if declared_count != len(raw_edges):
        raise DensificationError(
            f"{path}: edge_count {declared_count!r} != {len(raw_edges)} edges present"
        )

    edges: list[DensifiedEdge] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or not all(isinstance(field, str) for field in raw)
        ):
            # The str check is part of the same normalization: an unhashable
            # element would otherwise raise TypeError out of the dict lookups below.
            raise DensificationError(f"{path}: malformed edge {raw!r}")
        parent_fen, uci, child_fen = raw
        parent = graph.get_node(parent_fen)
        if parent is None:
            raise DensificationError(f"{path}: edge parent not in graph: {parent_fen!r}")
        if not graph.has_position(child_fen):
            raise DensificationError(f"{path}: edge child not in graph: {child_fen!r}")
        if uci in parent.children:
            raise DensificationError(
                f"{path}: edge {parent_fen!r} {uci!r} duplicates an existing graph edge"
            )
        if (parent_fen, uci) in seen:
            raise DensificationError(f"{path}: duplicate edge {parent_fen!r} {uci!r}")
        seen.add((parent_fen, uci))
        edges.append((parent_fen, uci, child_fen))

    return DensifiedEdges(tuple(sorted(edges)))


# ---------------------------------------------------------------------------
# Routing view resolution
# ---------------------------------------------------------------------------

# Keyed by graph.fingerprint. Derived from the graph the CALLER resolved rather
# than from a separate singleton, so a test that swaps in a synthetic graph can
# never end up routing over the real one.
_ROUTING_VIEWS: dict[str, RoutingView] = {}
_ROUTING_VIEW_LOCK = threading.Lock()


def _build_routing_view(graph: OpeningGraph) -> RoutingView:
    path = resolve_artifact_path()
    if path is None or not path.is_file():
        logger.error(
            "opening_densify: no %s found beside the opening data; drill routing "
            "will not cross transpositions and /openings will not show "
            "transposition cards. Regenerate with "
            "scripts/densify_opening_graph.py.",
            ARTIFACT_FILENAME,
        )
        return RoutingView(graph)
    try:
        overlay = load_densified_edges(graph, path)
    except DensificationError:
        # Degrade to today's behaviour rather than raise: hard-failing here would
        # take the whole app down over a drill routing feature. CI's `--check`
        # exact diff is the guard that makes a stale artifact non-silent.
        logger.error(
            "opening_densify: %s is unusable; drill routing will not cross "
            "transpositions and /openings will not show transposition cards",
            path,
            exc_info=True,
        )
        return RoutingView(graph)
    logger.info("opening_densify: loaded %d transposition edges", len(overlay))
    return RoutingView(graph, overlay)


def routing_view(graph: OpeningGraph) -> RoutingView:
    """The routing view for `graph`, built once per graph. Single-flight so
    concurrent first callers block on one build instead of each parsing the
    artifact; the fallback decision and its log therefore happen once, not per
    move."""
    key = graph.fingerprint
    view = _ROUTING_VIEWS.get(key)
    if view is not None:
        return view
    with _ROUTING_VIEW_LOCK:
        view = _ROUTING_VIEWS.get(key)
        if view is None:
            view = _build_routing_view(graph)
            _ROUTING_VIEWS[key] = view
        return view


def _reset_routing_views_for_testing() -> None:
    with _ROUTING_VIEW_LOCK:
        _ROUTING_VIEWS.clear()
