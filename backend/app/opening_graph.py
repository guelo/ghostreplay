"""In-memory opening book graph built from eco.json and eco.byPosition.json.

Provides a directed graph of chess opening positions keyed by normalized
4-field FEN. Each node carries child/parent edges (UCI moves), side-to-move,
and optional ECO label. The graph is frozen after construction (children are
MappingProxyType, parents are frozenset).

A pickle-based disk cache avoids the ~27-30s replay cost on subsequent loads.
"""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from types import MappingProxyType

import chess

from app.fen import active_color, normalize_fen

logger = logging.getLogger(__name__)

CACHE_VERSION = 1

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_ECO_PATH = _PROJECT_ROOT / "public" / "data" / "openings" / "eco.json"
_DEFAULT_BYPOS_PATH = (
    _PROJECT_ROOT / "public" / "data" / "openings" / "eco.byPosition.json"
)
_DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / ".opening_graph_cache"


def _fen_from_board(board: chess.Board) -> str:
    """Extract normalized 4-field FEN from a live board without reparsing."""
    epd = board.epd()
    if not board.has_legal_en_passant():
        parts = epd.split(" ")
        parts[3] = "-"
        return " ".join(parts)
    return epd


class OpeningGraphNode:
    """A single position in the opening graph.

    After the owning OpeningGraph is frozen, all attributes become read-only.
    """

    __slots__ = (
        "fen",
        "side_to_move",
        "children",
        "parents",
        "eco",
        "name",
        "_frozen",
    )

    def __init__(self, fen: str, side_to_move: str) -> None:
        object.__setattr__(self, "_frozen", False)
        self.fen: str = fen
        self.side_to_move: str = side_to_move
        self.children: dict[str, str] | MappingProxyType[str, str] = {}
        self.parents: set[tuple[str, str]] | frozenset[tuple[str, str]] = set()
        self.eco: str | None = None
        self.name: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        if self._frozen:
            raise AttributeError(
                f"OpeningGraphNode is frozen: cannot set '{name}'"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if self._frozen:
            raise AttributeError(
                f"OpeningGraphNode is frozen: cannot delete '{name}'"
            )
        object.__delattr__(self, name)


class OpeningGraph:
    """Directed graph of opening book positions keyed by normalized FEN."""

    def __init__(
        self, nodes: dict[str, OpeningGraphNode], root_fen: str
    ) -> None:
        self._nodes = nodes
        self.root_fen = root_fen
        self._frozen = False

    def freeze(self) -> None:
        """Freeze all nodes: immutable containers + attribute writes blocked."""
        if self._frozen:
            return
        for node in self._nodes.values():
            if not isinstance(node.children, MappingProxyType):
                node.children = MappingProxyType(node.children)  # type: ignore[assignment]
            if not isinstance(node.parents, frozenset):
                node.parents = frozenset(node.parents)
            object.__setattr__(node, "_frozen", True)
        self._frozen = True

    def get_node(self, fen: str) -> OpeningGraphNode | None:
        return self._nodes.get(fen)

    def get_children(self, fen: str) -> dict[str, OpeningGraphNode]:
        node = self._nodes.get(fen)
        if node is None:
            return {}
        return {
            uci: self._nodes[child_fen]
            for uci, child_fen in node.children.items()
            if child_fen in self._nodes
        }

    def get_parents(self, fen: str) -> list[tuple[OpeningGraphNode, str]]:
        node = self._nodes.get(fen)
        if node is None:
            return []
        return [
            (self._nodes[parent_fen], uci)
            for parent_fen, uci in node.parents
            if parent_fen in self._nodes
        ]

    def has_position(self, fen: str) -> bool:
        return fen in self._nodes

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return sum(len(n.children) for n in self._nodes.values())


def _get_or_create_node(
    nodes: dict[str, OpeningGraphNode], fen: str
) -> OpeningGraphNode:
    node = nodes.get(fen)
    if node is None:
        node = OpeningGraphNode(fen=fen, side_to_move=active_color(fen))
        nodes[fen] = node
    return node


def _build_from_scratch(
    eco_path: Path, by_position_path: Path
) -> OpeningGraph:
    with open(eco_path) as f:
        eco_data = json.load(f)
    with open(by_position_path) as f:
        bypos_data = json.load(f)

    entries = eco_data["entries"]
    by_position = bypos_data["by_position"]

    # Step 1: Replay UCI sequences and build edges
    nodes: dict[str, OpeningGraphNode] = {}
    board = chess.Board()

    for entry in entries:
        board.reset()
        uci_moves = entry["uci"].split()
        for uci_str in uci_moves:
            parent_fen = _fen_from_board(board)
            move = chess.Move.from_uci(uci_str)
            board.push(move)
            child_fen = _fen_from_board(board)

            parent_node = _get_or_create_node(nodes, parent_fen)
            child_node = _get_or_create_node(nodes, child_fen)

            parent_node.children[uci_str] = child_fen  # type: ignore[index]
            child_node.parents.add((parent_fen, uci_str))  # type: ignore[union-attr]

    # Step 2: Attach opening labels from byPosition
    unmatched = 0
    for epd, info in by_position.items():
        normalized = normalize_fen(epd + " 0 1")
        node = nodes.get(normalized)
        if node is None:
            unmatched += 1
            continue
        node.eco = info["eco"]
        node.name = info["name"]

    if unmatched:
        logger.warning(
            "opening_graph: %d byPosition entries did not match any graph node",
            unmatched,
        )

    # Step 3: Assert root
    root_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
    if root_fen not in nodes:
        raise RuntimeError("Opening graph is missing the starting position")

    return OpeningGraph(nodes=nodes, root_fen=root_fen)


def _load_or_build(
    eco_path: Path,
    by_position_path: Path,
    cache_dir: Path,
) -> OpeningGraph:
    cache_file = cache_dir / f"opening_graph_v{CACHE_VERSION}.pkl"
    eco_mtime = eco_path.stat().st_mtime
    bypos_mtime = by_position_path.stat().st_mtime

    if cache_file.exists():
        try:
            payload = pickle.loads(cache_file.read_bytes())
            if (
                payload.get("cache_version") == CACHE_VERSION
                and payload.get("eco_mtime") == eco_mtime
                and payload.get("bypos_mtime") == bypos_mtime
            ):
                logger.info("opening_graph: loaded from cache")
                graph = payload["graph"]
                graph.freeze()
                return graph
        except Exception:
            logger.warning("opening_graph: cache load failed, rebuilding")

    logger.info("opening_graph: building from scratch (this takes ~30s)...")
    graph = _build_from_scratch(eco_path, by_position_path)

    # Cache before freezing (MappingProxyType is not picklable)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(
            pickle.dumps(
                {
                    "cache_version": CACHE_VERSION,
                    "eco_mtime": eco_mtime,
                    "bypos_mtime": bypos_mtime,
                    "graph": graph,
                }
            )
        )
        logger.info("opening_graph: cached to %s", cache_file)
    except Exception:
        logger.warning("opening_graph: failed to write cache", exc_info=True)

    graph.freeze()
    return graph


def build_opening_graph(
    eco_path: Path | None = None,
    by_position_path: Path | None = None,
) -> OpeningGraph:
    """Build the opening graph, using disk cache when default paths are used."""
    eco = eco_path or _DEFAULT_ECO_PATH
    bypos = by_position_path or _DEFAULT_BYPOS_PATH

    # Custom paths bypass cache (used for testing with synthetic data)
    if eco_path is not None or by_position_path is not None:
        graph = _build_from_scratch(eco, bypos)
        graph.freeze()
        return graph

    return _load_or_build(eco, bypos, _DEFAULT_CACHE_DIR)


# -- Singleton --

_opening_graph: OpeningGraph | None = None


def get_opening_graph() -> OpeningGraph:
    """Return the singleton opening graph (default paths), building on first access."""
    global _opening_graph
    if _opening_graph is None:
        _opening_graph = build_opening_graph()
    return _opening_graph


def _reset_opening_graph_for_testing() -> None:
    """Clear the singleton so the next call rebuilds."""
    global _opening_graph
    _opening_graph = None
