"""Tests for the drill-routing transposition overlay.

The load-bearing properties, in order of what would hurt most if they broke:

1. The combined graph stays ACYCLIC. Drill routing treats any on-route position
   as valid, so a cycle would let a player shuffle pieces forever and still
   "reach" the target.
2. `graph.fingerprint` is UNCHANGED. It gates the opening score cache and the
   frozen calibration-analysis artifact, which is invalidated on mismatch.
3. `--check` catches an INCOMPLETE artifact. Provenance proves origin, not
   completeness; only exact recomputation proves the latter.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from types import MappingProxyType

import chess
import pytest
from unittest.mock import patch

from app.opening_densify import (
    EMPTY_DENSIFIED_EDGES,
    SCHEMA_VERSION,
    DensificationError,
    DensifiedEdges,
    RoutingView,
    _reset_routing_views_for_testing,
    compute_densified_edges,
    graph_topology_fingerprint,
    load_densified_edges,
    longest_path_depths,
    resolve_artifact_path,
    routing_view,
    scan_transposition_edges,
    serialize_edges,
)
from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _fen_from_board,
    build_opening_graph,
)
from app.opening_roots import build_opening_roots
from scripts.densify_opening_graph import _check

ARTIFACT = "eco.transpositions.json"

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"

# 1.c4 e6 2.Nc3 Nf6 3.d4 d5 — the English move order into Queen's Gambit
# Declined: Normal Defense. It reaches the identical normalized FEN as the main
# 1.d4 d5 2.c4 e6 3.Nc3 Nf6 order, but ECO never recorded it, so exactly two
# edges are missing from the book: g8f6 and d2d4.
QGD_ENGLISH_LINE = ["c2c4", "e7e6", "b1c3", "g8f6", "d2d4", "d7d5"]
QGD_MAIN_LINE = ["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"]


def _replay(ucis: list[str]) -> list[str]:
    """Normalized FENs along a line, starting with the start position."""
    board = chess.Board()
    fens = [_fen_from_board(board)]
    for uci in ucis:
        board.push(chess.Move.from_uci(uci))
        fens.append(_fen_from_board(board))
    return fens


# -- Real-data fixtures (module-scoped: the scan is ~37s) --


@pytest.fixture(scope="module")
def real_graph() -> OpeningGraph:
    return build_opening_graph()


@pytest.fixture(scope="module")
def scanned_edges(real_graph: OpeningGraph) -> tuple:
    """Every transposition connection, BEFORE the forward-progress filter."""
    return scan_transposition_edges(real_graph)


@pytest.fixture(scope="module")
def real_depths(real_graph: OpeningGraph) -> dict[str, int]:
    return longest_path_depths(real_graph)


@pytest.fixture(scope="module")
def real_edges(real_graph: OpeningGraph, scanned_edges, real_depths) -> tuple:
    """The retained edges. Derived from the shared scan so the module pays the
    ~37s board walk once rather than once per fixture."""
    return tuple(e for e in scanned_edges if real_depths[e[2]] > real_depths[e[0]])


@pytest.fixture(scope="module")
def artifact_path() -> Path:
    path = resolve_artifact_path()
    assert path is not None and path.is_file(), (
        "eco.transpositions.json is missing; run scripts/densify_opening_graph.py"
    )
    return path


# -- Synthetic graphs --


def _graph(edges: list[tuple[str, str, str]], freeze: bool = True) -> OpeningGraph:
    """Build a graph from abstract (parent, uci, child) triples. FENs here are
    opaque labels — these tests exercise topology, not chess."""
    nodes: dict[str, OpeningGraphNode] = {}
    for parent_fen, uci, child_fen in edges:
        for fen in (parent_fen, child_fen):
            if fen not in nodes:
                nodes[fen] = OpeningGraphNode(fen, "white")
        nodes[parent_fen].children[uci] = child_fen
        nodes[child_fen].parents.add((parent_fen, uci))
    graph = OpeningGraph(nodes, next(iter(nodes)) if nodes else START_FEN)
    if freeze:
        graph.freeze()
    return graph


def _has_cycle(
    fens: set[str], children_of
) -> bool:
    """Kahn over an arbitrary edge relation: any node left unordered is in or
    downstream of a cycle."""
    in_degree = {fen: 0 for fen in fens}
    for fen in fens:
        for child_fen in children_of(fen):
            if child_fen in in_degree:
                in_degree[child_fen] += 1
    queue = deque(fen for fen, deg in in_degree.items() if deg == 0)
    ordered = 0
    while queue:
        fen = queue.popleft()
        ordered += 1
        for child_fen in children_of(fen):
            if child_fen not in in_degree:
                continue
            in_degree[child_fen] -= 1
            if in_degree[child_fen] == 0:
                queue.append(child_fen)
    return ordered != len(fens)


# -- Acyclicity: the property the whole filter exists to protect --


class TestAcyclicity:
    def test_base_graph_is_acyclic(self, real_graph: OpeningGraph, real_depths):
        """The longest-path potential is only defined on an acyclic base, so this
        is a precondition of everything below."""
        assert len(real_depths) == real_graph.node_count

    def test_combined_graph_is_acyclic(
        self, real_graph: OpeningGraph, real_edges
    ):
        overlay: dict[str, set[str]] = {}
        for parent_fen, _uci, child_fen in real_edges:
            overlay.setdefault(parent_fen, set()).add(child_fen)

        def children_of(fen: str):
            yield from real_graph._nodes[fen].children.values()
            yield from overlay.get(fen, ())

        assert not _has_cycle(set(real_graph._nodes), children_of)

    def test_unfiltered_densification_would_cycle(
        self, real_graph: OpeningGraph, scanned_edges
    ):
        """The filter is REQUIRED, not a nicety: without it the overlay closes
        cycles, and drill routing would call an endless shuffle on-route."""
        overlay: dict[str, set[str]] = {}
        for parent_fen, _uci, child_fen in scanned_edges:
            overlay.setdefault(parent_fen, set()).add(child_fen)

        def children_of(fen: str):
            yield from real_graph._nodes[fen].children.values()
            yield from overlay.get(fen, ())

        assert _has_cycle(set(real_graph._nodes), children_of)

    def test_compute_fails_loudly_on_a_cyclic_base(self):
        graph = _graph([("a", "1", "b"), ("b", "2", "c"), ("c", "3", "a")])
        with pytest.raises(DensificationError, match="cyclic"):
            compute_densified_edges(graph)


# -- The forward-progress potential --


class TestLongestPathPotential:
    def test_strictly_increases_across_every_base_edge(
        self, real_graph: OpeningGraph, real_depths
    ):
        """True by construction of longest-path depth. Together with the filter
        on new edges, this is the whole acyclicity proof: every edge in the
        combined graph strictly increases a fixed potential."""
        violations = [
            (fen, child_fen)
            for fen, node in real_graph._nodes.items()
            for child_fen in node.children.values()
            if real_depths[child_fen] <= real_depths[fen]
        ]
        assert violations == []

    def test_strictly_increases_across_every_retained_edge(
        self, real_edges, real_depths
    ):
        violations = [
            edge for edge in real_edges if real_depths[edge[2]] <= real_depths[edge[0]]
        ]
        assert violations == []

    def test_minimum_root_depth_would_not_be_a_valid_potential(
        self, real_graph: OpeningGraph
    ):
        """Regression pin for the earlier draft's mistake. Minimum root depth
        looks like it works — filtering new edges by it happens to measure
        acyclic — but it is not a potential over the base graph at all, so it
        proves nothing. The base graph contains exactly one edge running
        backwards under it: d2f4, from min-depth 16 to 15.
        """
        min_depth: dict[str, int] = {real_graph.root_fen: 0}
        queue = deque([real_graph.root_fen])
        while queue:
            fen = queue.popleft()
            for child_fen in real_graph._nodes[fen].children.values():
                if child_fen not in min_depth:
                    min_depth[child_fen] = min_depth[fen] + 1
                    queue.append(child_fen)

        backwards = [
            (fen, uci, min_depth[fen], min_depth[child_fen])
            for fen, node in real_graph._nodes.items()
            for uci, child_fen in node.children.items()
            if child_fen in min_depth
            and fen in min_depth
            and min_depth[child_fen] <= min_depth[fen]
        ]
        assert len(backwards) == 1
        _fen, uci, parent_depth, child_depth = backwards[0]
        assert (uci, parent_depth, child_depth) == ("d2f4", 16, 15)


class TestForwardProgressFilter:
    def test_drops_only_the_non_increasing_edges(
        self, scanned_edges, real_edges, real_depths
    ):
        assert len(scanned_edges) == 2150
        assert len(real_edges) == 2141

        dropped = set(scanned_edges) - set(real_edges)
        assert len(dropped) == 9
        assert all(real_depths[edge[2]] <= real_depths[edge[0]] for edge in dropped)

    def test_keeps_both_missing_qgd_english_links(self, real_edges):
        """The issue's own example: the English order 1.c4 e6 2.Nc3 Nf6 3.d4 d5
        reaches the QGD Normal Defense target but is missing exactly these two
        edges. The filter must not drop them."""
        fens = _replay(QGD_ENGLISH_LINE)
        retained = set(real_edges)
        assert (fens[3], "g8f6", fens[4]) in retained
        assert (fens[4], "d2d4", fens[5]) in retained

    def test_qgd_main_line_needed_no_help(self, real_graph: OpeningGraph):
        """The main order was already fully in book — the bug was only ever the
        English order's missing edges."""
        fens = _replay(QGD_MAIN_LINE)
        for i, uci in enumerate(QGD_MAIN_LINE):
            assert real_graph._nodes[fens[i]].children.get(uci) == fens[i + 1]
        assert fens[-1] == _replay(QGD_ENGLISH_LINE)[-1]

    def test_scan_never_duplicates_an_existing_graph_edge(
        self, real_graph: OpeningGraph, scanned_edges
    ):
        for parent_fen, uci, _child_fen in scanned_edges:
            assert uci not in real_graph._nodes[parent_fen].children


# -- The fingerprint that protects opening scores and calibration analysis --


class TestGraphIsUntouched:
    def test_graph_fingerprint_unchanged_by_densification(
        self, real_graph: OpeningGraph, real_edges
    ):
        """The decisive constraint. graph.fingerprint feeds
        opening_score_inputs_fingerprint, which gates the opening score cache,
        and the frozen calibration cohort pins it and FAILS CLOSED on mismatch.
        Merging these edges into node.children would detonate both to fix a
        drill routing bug."""
        before = real_graph.fingerprint
        view = RoutingView(real_graph, DensifiedEdges(real_edges))
        assert view.routing_children(_replay(QGD_ENGLISH_LINE)[3])  # view is live
        assert real_graph.fingerprint == before
        assert view.graph_fingerprint == before

    def test_densified_edges_stay_out_of_node_children(
        self, real_graph: OpeningGraph, real_edges
    ):
        RoutingView(real_graph, DensifiedEdges(real_edges))
        for parent_fen, uci, _child_fen in real_edges:
            assert uci not in real_graph._nodes[parent_fen].children

    def test_opening_roots_unchanged_by_densification(
        self, real_graph: OpeningGraph, real_edges
    ):
        """Roots derive from graph parents and a depth BFS over children, so a
        leaked edge would shift root depths and the root DAG."""
        before = build_opening_roots(real_graph).fingerprint
        RoutingView(real_graph, DensifiedEdges(real_edges))
        assert build_opening_roots(real_graph).fingerprint == before

    def test_densification_is_outside_the_calibration_scorer_manifest(self):
        """The manifest digest hashes file BYTES, so a pure function added to a
        pinned file would churn the scorer-source digest and force a release
        re-calibration. graph_topology_fingerprint lives in opening_densify.py
        for exactly this reason — keep it, and the routing modules, out."""
        from scripts.calibrate_opening_scores_v2 import SCORER_SOURCE_FILES

        assert "backend/app/opening_densify.py" not in SCORER_SOURCE_FILES
        assert "backend/app/drill_steering.py" not in SCORER_SOURCE_FILES
        # ...and the fingerprint helper is not hiding in a pinned file.
        import app.opening_graph as opening_graph_module

        assert not hasattr(opening_graph_module, "graph_topology_fingerprint")

    def test_topology_fingerprint_ignores_labels(self):
        """The artifact pins topology, not graph.fingerprint, so a label-only
        byPosition edit does not force a spurious ~40s regeneration."""
        graph = _graph([("a", "1", "b")], freeze=False)
        before = graph_topology_fingerprint(graph)
        graph._nodes["b"].name = "Some Opening"
        graph._nodes["b"].eco = "A00"
        assert graph_topology_fingerprint(graph) == before

    def test_topology_fingerprint_tracks_edges(self):
        graph = _graph([("a", "1", "b")], freeze=False)
        before = graph_topology_fingerprint(graph)
        graph._nodes["a"].children["2"] = "b"
        assert graph_topology_fingerprint(graph) != before


# -- Overlay + view --


class TestRoutingView:
    def test_routing_parents_include_densified_edges(self):
        graph = _graph([("a", "1", "b")])
        view = RoutingView(graph, DensifiedEdges((("a", "2", "b"),)))
        assert {uci for _node, uci in view.routing_parents("b")} == {"1", "2"}
        assert {uci for _node, uci in graph.get_parents("b")} == {"1"}

    def test_routing_children_merge_base_and_overlay(self):
        graph = _graph([("a", "1", "b"), ("a", "3", "c")])
        view = RoutingView(graph, DensifiedEdges((("a", "2", "c"),)))
        assert dict(view.routing_children("a")) == {"1": "b", "2": "c", "3": "c"}
        assert dict(graph._nodes["a"].children) == {"1": "b", "3": "c"}

    def test_routing_children_of_untouched_position_are_base_children(self):
        graph = _graph([("a", "1", "b"), ("b", "2", "c")])
        view = RoutingView(graph, DensifiedEdges((("a", "3", "c"),)))
        assert dict(view.routing_children("b")) == {"2": "c"}

    def test_unknown_position_has_no_routing_edges(self):
        view = RoutingView(_graph([("a", "1", "b")]))
        assert view.routing_children("nope") == {}
        assert view.routing_parents("nope") == []
        assert not view.has_position("nope")

    def test_empty_overlay_view_matches_the_bare_graph(self):
        graph = _graph([("a", "1", "b")])
        view = RoutingView(graph)
        assert view.overlay_fingerprint == EMPTY_DENSIFIED_EDGES.fingerprint
        assert dict(view.routing_children("a")) == {"1": "b"}

    def test_overlay_fingerprint_separates_different_edge_sets(self):
        one = DensifiedEdges((("a", "2", "b"),))
        other = DensifiedEdges((("a", "3", "b"),))
        assert one.fingerprint != other.fingerprint
        assert DensifiedEdges((("a", "2", "b"),)).fingerprint == one.fingerprint


class TestDeepImmutability:
    """Mirrors the frozen-node tests: a frozen dataclass over plain dicts would
    still hand callers a mutable shared map."""

    def test_overlay_children_reject_mutation(self):
        overlay = DensifiedEdges((("a", "2", "b"),))
        assert isinstance(overlay.children_of("a"), MappingProxyType)
        with pytest.raises(TypeError):
            overlay.children_of("a")["9"] = "z"  # type: ignore[index]

    def test_overlay_parents_are_a_tuple(self):
        overlay = DensifiedEdges((("a", "2", "b"),))
        assert overlay.parents_of("b") == (("a", "2"),)

    def test_absent_lookups_do_not_grow_the_overlay(self):
        overlay = DensifiedEdges((("a", "2", "b"),))
        assert overlay.children_of("zzz") == {}
        assert overlay.parents_of("zzz") == ()
        assert len(overlay) == 1

    def test_merged_routing_children_reject_mutation(self):
        view = RoutingView(_graph([("a", "1", "b")]), DensifiedEdges((("a", "2", "b"),)))
        with pytest.raises(TypeError):
            view.routing_children("a")["9"] = "z"  # type: ignore[index]

    def test_mutating_the_source_edges_cannot_reach_the_overlay(self):
        edges = [("a", "2", "b")]
        overlay = DensifiedEdges(tuple(edges))
        edges.append(("a", "3", "c"))
        assert len(overlay) == 1


# -- Artifact --


class TestArtifact:
    def test_checked_in_artifact_matches_the_current_graph(
        self, real_graph: OpeningGraph, real_edges, artifact_path: Path
    ):
        overlay = load_densified_edges(real_graph, artifact_path)
        assert overlay.edges == tuple(sorted(real_edges))

    def test_every_artifact_edge_is_a_legal_move_between_graph_positions(
        self, real_graph: OpeningGraph, artifact_path: Path
    ):
        overlay = load_densified_edges(real_graph, artifact_path)
        for parent_fen, uci, child_fen in overlay.edges:
            board = chess.Board(f"{parent_fen} 0 1")
            move = chess.Move.from_uci(uci)
            assert move in board.legal_moves, f"{uci} illegal from {parent_fen}"
            board.push(move)
            assert _fen_from_board(board) == child_fen
            assert real_graph.has_position(parent_fen)
            assert real_graph.has_position(child_fen)

    def test_artifact_has_no_duplicate_edges(self, artifact_path: Path):
        payload = json.loads(artifact_path.read_bytes())
        keys = [(edge[0], edge[1]) for edge in payload["edges"]]
        assert len(keys) == len(set(keys))

    def test_load_rejects_a_stale_topology_fingerprint(self, tmp_path: Path):
        graph = _graph([("a", "1", "b")])
        payload = serialize_edges(graph, (), "2026-01-01T00:00:00+00:00")
        payload["graph_topology_fingerprint"] = "0" * 64
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(payload))
        with pytest.raises(DensificationError, match="graph_topology_fingerprint"):
            load_densified_edges(graph, path)

    def test_load_rejects_an_unknown_schema_version(self, tmp_path: Path):
        graph = _graph([("a", "1", "b")])
        payload = serialize_edges(graph, (), "2026-01-01T00:00:00+00:00")
        payload["schema_version"] = SCHEMA_VERSION + 1
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(payload))
        with pytest.raises(DensificationError, match="schema_version"):
            load_densified_edges(graph, path)

    def test_load_rejects_an_edge_whose_endpoint_left_the_graph(self, tmp_path: Path):
        graph = _graph([("a", "1", "b")])
        payload = serialize_edges(graph, (("a", "2", "gone"),), "2026-01-01T00:00:00+00:00")
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(payload))
        with pytest.raises(DensificationError, match="child not in graph"):
            load_densified_edges(graph, path)

    def test_load_rejects_an_edge_the_graph_already_has(self, tmp_path: Path):
        graph = _graph([("a", "1", "b")])
        payload = serialize_edges(graph, (("a", "1", "b"),), "2026-01-01T00:00:00+00:00")
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(payload))
        with pytest.raises(DensificationError, match="duplicates an existing"):
            load_densified_edges(graph, path)

    @pytest.mark.parametrize(
        "raw",
        ["[]", '"nope"', "42", "null",
         '{"schema_version": 1, "edges": [{"a": 1}], "edge_count": 1}'],
    )
    def test_load_normalizes_every_invalid_shape_to_densification_error(
        self, tmp_path: Path, raw: str
    ):
        """DensificationError is the ONLY exception `_build_routing_view` degrades
        on. A payload that is valid JSON but not the expected shape must not leak
        an AttributeError/TypeError past it: that escapes the routing-view
        singleton uncached, so drill routing 500s and every consumer re-raises and
        re-logs on each request instead of falling back once."""
        path = tmp_path / ARTIFACT
        path.write_text(raw)
        with pytest.raises(DensificationError):
            load_densified_edges(_graph([("a", "1", "b")]), path)

    def test_a_malformed_artifact_degrades_to_one_cached_empty_routing_view(
        self, tmp_path: Path
    ):
        """The fallback must be cached: a broken artifact costs one log line, not
        one per request, and every consumer sees the same empty overlay."""
        graph = _graph([("a", "1", "b")])
        path = tmp_path / ARTIFACT
        path.write_text("[]")
        _reset_routing_views_for_testing()
        with patch("app.opening_densify.resolve_artifact_path", return_value=path):
            first = routing_view(graph)
            second = routing_view(graph)
        assert first is second                 # built once, cached
        assert len(first.overlay) == 0         # degraded to the base graph
        assert first.routing_children("a") == {"1": "b"}
        _reset_routing_views_for_testing()

    def test_load_rejects_a_miscounted_artifact(self, tmp_path: Path):
        graph = _graph([("a", "1", "b")])
        payload = serialize_edges(graph, (("a", "2", "b"),), "2026-01-01T00:00:00+00:00")
        payload["edge_count"] = 99
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(payload))
        with pytest.raises(DensificationError, match="edge_count"):
            load_densified_edges(graph, path)


class TestCheckProvesCompleteness:
    """Provenance proves which graph an artifact came from — NOT that it holds
    every edge that graph generates. Only exact recomputation proves that, which
    is why `--check` diffs the edge SET rather than trusting the fingerprint."""

    @staticmethod
    def _graph_and_expected(edge_count: int) -> tuple[OpeningGraph, dict]:
        graph = _graph([("a", "1", "b"), ("b", "2", "d"), ("a", "3", "c"), ("c", "4", "d")])
        all_edges = (("a", "9", "d"), ("b", "8", "c"))
        return graph, serialize_edges(
            graph, all_edges[:edge_count], "2026-01-01T00:00:00+00:00"
        )

    def test_check_passes_on_a_complete_artifact(self, tmp_path: Path):
        graph, expected = self._graph_and_expected(2)
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(expected))
        assert _check(path, expected) == 0

    def test_check_catches_a_truncated_artifact(self, tmp_path: Path):
        graph, expected = self._graph_and_expected(2)
        _, truncated = self._graph_and_expected(1)
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(truncated))

        # The truncated artifact is INDISTINGUISHABLE from a good one to every
        # per-edge guard: valid provenance, both endpoints in the graph, no
        # duplicate, honest edge_count. It loads clean.
        overlay = load_densified_edges(graph, path)
        assert len(overlay) == 1

        # Only the exact diff sees that an edge is missing.
        assert _check(path, expected) == 1

    def test_check_catches_a_missing_artifact(self, tmp_path: Path):
        _graph_, expected = self._graph_and_expected(2)
        assert _check(tmp_path / ARTIFACT, expected) == 1

    def test_check_catches_an_extra_hand_added_edge(self, tmp_path: Path):
        graph, expected = self._graph_and_expected(1)
        tampered = json.loads(json.dumps(expected))
        tampered["edges"].append(["b", "8", "c"])
        tampered["edge_count"] = 2
        path = tmp_path / ARTIFACT
        path.write_text(json.dumps(tampered))
        assert _check(path, expected) == 1
