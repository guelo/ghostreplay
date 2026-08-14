"""Immutable transposition-artifact contract shared by scoring and browsing.

The generated routing artifact is a score input as well as a browsing input.  This
module deliberately contains only the small common boundary: schema/provenance
validation, the immutable edge snapshot, its content fingerprint, the forward-
progress proof, and the opening-boundary predicate.  Densification generation and
browse-only fallback policy remain in :mod:`app.opening_densify`.
"""

from __future__ import annotations

import hashlib
import json
import stat as stat_module
import threading
from collections import deque
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from app.game_phase import is_middlegame_position
from app.opening_graph import (
    OpeningGraph,
    _default_opening_data_dir_candidates,
    _opening_data_dir_has_files,
)

SCHEMA_VERSION = 1
ARTIFACT_FILENAME = "eco.transpositions.json"

# (parent_fen, uci, child_fen)
DensifiedEdge = tuple[str, str, str]


class DensificationError(RuntimeError):
    """The artifact is missing or unusable for the supplied opening graph."""


def graph_topology_fingerprint(graph: OpeningGraph) -> str:
    """SHA-256 over the graph's FEN key set and child edges, excluding labels."""

    payload = "|".join(
        f"{fen}\t{','.join(f'{uci}:{child_fen}' for uci, child_fen in sorted(node.children.items()))}"
        for fen, node in sorted(graph._nodes.items())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def edges_fingerprint(edges: tuple[DensifiedEdge, ...]) -> str:
    """Content identity of the sorted edge set; generated-at metadata is ignored."""

    payload = "|".join(f"{parent}\t{uci}\t{child}" for parent, uci, child in edges)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _topological_order(graph: OpeningGraph) -> list[str]:
    in_degree: dict[str, int] = {fen: 0 for fen in graph._nodes}
    for node in graph._nodes.values():
        for child_fen in node.children.values():
            if child_fen in in_degree:
                in_degree[child_fen] += 1

    queue: deque[str] = deque(fen for fen, degree in in_degree.items() if degree == 0)
    order: list[str] = []
    while queue:
        fen = queue.popleft()
        order.append(fen)
        for child_fen in graph._nodes[fen].children.values():
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
    """A forward potential shared by artifact generation and strict validation."""

    order = _topological_order(graph)
    depths: dict[str, int] = {fen: 0 for fen in order}
    for fen in order:
        depth = depths[fen]
        for child_fen in graph._nodes[fen].children.values():
            if child_fen in depths and depths[child_fen] < depth + 1:
                depths[child_fen] = depth + 1
    return depths


def coverage_structural_edge_is_eligible(
    parent_fen: str,
    child_fen: str,
    *,
    is_middlegame: Callable[[str], bool] | None = None,
) -> bool:
    """Whether a reference/routing edge stays inside the opening boundary.

    Observed evidence edges intentionally do not use this predicate: the evidence
    divider already decided whether those exact played edges belong to the overlay.
    """

    classify = is_middlegame or is_middlegame_position
    return not classify(parent_fen) and not classify(child_fen)


class DensifiedEdges:
    """Deeply immutable, validated routing-edge snapshot."""

    __slots__ = ("_edges", "_children", "_parents", "_fingerprint")

    def __init__(self, edges: tuple[DensifiedEdge, ...]) -> None:
        ordered = tuple(sorted(edges))
        children: dict[str, dict[str, str]] = {}
        parents: dict[str, list[tuple[str, str]]] = {}
        for parent_fen, uci, child_fen in ordered:
            children.setdefault(parent_fen, {})[uci] = child_fen
            parents.setdefault(child_fen, []).append((parent_fen, uci))

        self._edges = ordered
        self._children = MappingProxyType(
            {fen: MappingProxyType(dict(moves)) for fen, moves in children.items()}
        )
        self._parents = MappingProxyType(
            {fen: tuple(refs) for fen, refs in parents.items()}
        )
        self._fingerprint = edges_fingerprint(ordered)

    @property
    def edges(self) -> tuple[DensifiedEdge, ...]:
        return self._edges

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def children_of(self, fen: str) -> Mapping[str, str]:
        return self._children.get(fen, _EMPTY_MOVES)

    def parents_of(self, fen: str) -> tuple[tuple[str, str], ...]:
        return self._parents.get(fen, ())

    def __len__(self) -> int:
        return len(self._edges)


_EMPTY_MOVES: Mapping[str, str] = MappingProxyType({})
EMPTY_DENSIFIED_EDGES = DensifiedEdges(())

# Strict scoring reads are frequent freshness checks over one immutable graph and
# artifact. Cache successful validation by the graph's stable identity plus the
# source file identity; an atomic replacement or in-place rewrite changes at least
# one stat component and is therefore re-read and revalidated. Browsing keeps its
# separate logged-fallback cache in opening_densify.
_StrictSourceKey = tuple[str, str, int, int, int, int, int]
_STRICT_SNAPSHOT_LOCK = threading.Lock()
_STRICT_SNAPSHOTS: dict[_StrictSourceKey, DensifiedEdges] = {}


def _strict_source_key(
    graph: OpeningGraph, path: Path
) -> _StrictSourceKey:
    try:
        source_stat = path.stat()
    except OSError as exc:
        raise DensificationError(
            f"Required {ARTIFACT_FILENAME} is missing beside the opening data"
        ) from exc
    if not stat_module.S_ISREG(source_stat.st_mode):
        raise DensificationError(
            f"Required {ARTIFACT_FILENAME} is missing beside the opening data"
        )
    return (
        graph.fingerprint,
        str(path),
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
        source_stat.st_size,
    )


def resolve_artifact_path() -> Path | None:
    """Resolve the artifact beside the currently selected ECO data files."""

    for data_dir in _default_opening_data_dir_candidates():
        if _opening_data_dir_has_files(data_dir):
            return data_dir / ARTIFACT_FILENAME
    return None


def serialize_edges(
    graph: OpeningGraph, edges: tuple[DensifiedEdge, ...], generated_at: str
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "graph_topology_fingerprint": graph_topology_fingerprint(graph),
        "generated_at": generated_at,
        "edge_count": len(edges),
        "edges": [list(edge) for edge in edges],
    }


def load_densified_edges(graph: OpeningGraph, path: Path) -> DensifiedEdges:
    """Read once, validate completely, and return one immutable edge snapshot."""

    try:
        artifact_bytes = path.read_bytes()
        payload = json.loads(artifact_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise DensificationError(f"Cannot read {path}: {exc}") from exc

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

    depths = longest_path_depths(graph)
    edges: list[DensifiedEdge] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if (
            not isinstance(raw, list)
            or len(raw) != 3
            or not all(isinstance(field, str) for field in raw)
        ):
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
        if depths[child_fen] <= depths[parent_fen]:
            raise DensificationError(
                f"{path}: edge {parent_fen!r} {uci!r} -> {child_fen!r} does not "
                "strictly increase the graph longest-path potential"
            )
        seen.add((parent_fen, uci))
        edges.append((parent_fen, uci, child_fen))

    return DensifiedEdges(tuple(edges))


def load_strict_densified_edges(
    graph: OpeningGraph, path: Path | None = None
) -> DensifiedEdges:
    """Resolve and memoize the required scoring snapshot, failing closed if absent."""

    resolved = path if path is not None else resolve_artifact_path()
    if resolved is None:
        raise DensificationError(
            f"Required {ARTIFACT_FILENAME} is missing beside the opening data"
        )
    resolved = resolved.expanduser().resolve(strict=False)
    source_key = _strict_source_key(graph, resolved)
    with _STRICT_SNAPSHOT_LOCK:
        cached = _STRICT_SNAPSHOTS.get(source_key)
        if cached is not None:
            return cached

        snapshot = load_densified_edges(graph, resolved)
        # Do not retain a snapshot under stale metadata when the artifact was
        # replaced during validation. The current caller still receives the exact
        # immutable bytes it validated; the next call observes and loads the new file.
        if _strict_source_key(graph, resolved) == source_key:
            graph_fp, source_path = source_key[:2]
            for stale_key in tuple(_STRICT_SNAPSHOTS):
                if stale_key[:2] == (graph_fp, source_path):
                    del _STRICT_SNAPSHOTS[stale_key]
            _STRICT_SNAPSHOTS[source_key] = snapshot
        return snapshot


def _reset_strict_densified_edges_cache_for_testing() -> None:
    with _STRICT_SNAPSHOT_LOCK:
        _STRICT_SNAPSHOTS.clear()
