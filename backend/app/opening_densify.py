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

import logging
import threading
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import chess

from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _fen_from_board,
)
from app.opening_transposition_artifact import (
    ARTIFACT_FILENAME,
    DensificationError,
    DensifiedEdge,
    DensifiedEdges,
    EMPTY_DENSIFIED_EDGES,
    SCHEMA_VERSION,
    graph_topology_fingerprint,
    load_densified_edges,
    longest_path_depths,
    resolve_artifact_path,
    serialize_edges,
)

__all__ = [
    "ARTIFACT_FILENAME",
    "DensificationError",
    "DensifiedEdge",
    "DensifiedEdges",
    "EMPTY_DENSIFIED_EDGES",
    "SCHEMA_VERSION",
    "graph_topology_fingerprint",
    "load_densified_edges",
    "longest_path_depths",
    "resolve_artifact_path",
    "serialize_edges",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Edge computation (offline / test only — never on the startup path)
# ---------------------------------------------------------------------------


def _board_for_fen(fen: str) -> chess.Board:
    return chess.Board(f"{fen} 0 1")


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


_EMPTY_MOVES: Mapping[str, str] = MappingProxyType({})


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
# Routing view resolution
# ---------------------------------------------------------------------------

# Keyed by BOTH immutable inputs.  Loading happens before lookup so replacing only
# the artifact in a live process cannot return a view over the previous edge set.
_ROUTING_VIEWS: dict[tuple[str, str], RoutingView] = {}
# Operational source cache avoids reparsing (and re-logging) an unchanged broken
# artifact on every browse request. Its file metadata is only a reload hint; the
# authoritative view cache below remains keyed by graph + validated edge content.
_ROUTING_SOURCE_VIEWS: dict[
    tuple[str, str, int, int, int, int], RoutingView
] = {}
_ROUTING_VIEW_LOCK = threading.Lock()


def _build_routing_view(
    graph: OpeningGraph, path: Path | None = None
) -> RoutingView:
    if path is None:
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
    """Return a view keyed by graph identity and the currently loaded edge set."""

    path = resolve_artifact_path()
    try:
        stat = path.stat() if path is not None else None
    except OSError:
        stat = None
    source_key = (
        graph.fingerprint,
        str(path),
        stat.st_ino if stat is not None else -1,
        stat.st_mtime_ns if stat is not None else -1,
        stat.st_ctime_ns if stat is not None else -1,
        stat.st_size if stat is not None else -1,
    )
    with _ROUTING_VIEW_LOCK:
        source_view = _ROUTING_SOURCE_VIEWS.get(source_key)
        if source_view is not None:
            return source_view

        candidate = _build_routing_view(graph, path)
        key = (graph.fingerprint, candidate.overlay_fingerprint)
        view = _ROUTING_VIEWS.get(key)
        if view is None:
            view = candidate
            _ROUTING_VIEWS[key] = view
        _ROUTING_SOURCE_VIEWS[source_key] = view
        return view


def _reset_routing_views_for_testing() -> None:
    with _ROUTING_VIEW_LOCK:
        _ROUTING_VIEWS.clear()
        _ROUTING_SOURCE_VIEWS.clear()
