"""Tests for opening graph construction and access."""

from __future__ import annotations

import json
import pickle
import tempfile
from pathlib import Path
from types import MappingProxyType

import chess
import pytest

from app.fen import normalize_fen
from app.opening_graph import (
    OpeningGraph,
    OpeningGraphNode,
    _build_from_scratch,
    _fen_from_board,
    _load_or_build,
    _reset_opening_graph_for_testing,
    build_opening_graph,
    get_opening_graph,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ECO_PATH = _PROJECT_ROOT / "public" / "data" / "openings" / "eco.json"
_BYPOS_PATH = _PROJECT_ROOT / "public" / "data" / "openings" / "eco.byPosition.json"

ROOT_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"


@pytest.fixture(scope="module")
def real_graph() -> OpeningGraph:
    """Load the real-data graph (uses disk cache for speed)."""
    return build_opening_graph()


@pytest.fixture()
def bypos_position_count() -> int:
    with open(_BYPOS_PATH) as f:
        data = json.load(f)
    return data["position_count"]


# -- Synthetic fixtures --

def _write_synthetic_data(tmp: Path) -> tuple[Path, Path]:
    """Write minimal eco.json and eco.byPosition.json for testing."""
    eco_path = tmp / "eco.json"
    bypos_path = tmp / "bypos.json"

    eco_data = {
        "dataset": "test",
        "source_commit": "abc",
        "entry_count": 3,
        "entries": [
            {
                "eco": "A00",
                "name": "Test Opening A",
                "pgn": "1. e4",
                "uci": "e2e4",
                "epd": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -",
            },
            {
                "eco": "A00",
                "name": "Test Opening B",
                "pgn": "1. d4",
                "uci": "d2d4",
                "epd": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -",
            },
            {
                "eco": "B00",
                "name": "Test Opening C",
                "pgn": "1. e4 e5",
                "uci": "e2e4 e7e5",
                "epd": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -",
            },
        ],
    }

    # Only label the leaf positions, not the root or intermediate
    bypos_data = {
        "dataset": "test",
        "source_commit": "abc",
        "position_count": 2,
        "by_position": {
            "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Test Opening B",
            },
            "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -": {
                "eco": "B00",
                "name": "Test Opening C",
            },
        },
    }

    eco_path.write_text(json.dumps(eco_data))
    bypos_path.write_text(json.dumps(bypos_data))
    return eco_path, bypos_path


# -- Construction tests (real data) --


class TestConstruction:
    def test_node_count_matches_position_count_plus_root(
        self, real_graph: OpeningGraph, bypos_position_count: int
    ):
        assert real_graph.node_count == bypos_position_count + 1

    def test_root_exists(self, real_graph: OpeningGraph):
        assert real_graph.has_position(ROOT_FEN)

    def test_root_has_no_label(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        assert root is not None
        assert root.eco is None
        assert root.name is None

    def test_edge_count_exceeds_node_count(self, real_graph: OpeningGraph):
        assert real_graph.edge_count > real_graph.node_count


# -- Edge correctness (real data) --


class TestEdges:
    def test_e2e4_from_root(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        assert root is not None
        child_fen = root.children.get("e2e4")
        assert child_fen is not None
        # Verify it's the correct position
        board = chess.Board()
        board.push_uci("e2e4")
        expected = _fen_from_board(board)
        assert child_fen == expected

    def test_parent_backlink(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        assert root is not None
        child_fen = root.children["e2e4"]
        child = real_graph.get_node(child_fen)
        assert child is not None
        assert (ROOT_FEN, "e2e4") in child.parents

    def test_some_node_has_multiple_parents(self, real_graph: OpeningGraph):
        """At least one node in the graph should have multiple parent positions."""
        found = False
        for node in real_graph._nodes.values():
            parent_fens = {pf for pf, _ in node.parents}
            if len(parent_fens) >= 2:
                found = True
                break
        assert found, "No transposition found in the graph"

    def test_get_children_returns_nodes(self, real_graph: OpeningGraph):
        children = real_graph.get_children(ROOT_FEN)
        assert len(children) > 0
        for uci, child_node in children.items():
            assert isinstance(child_node, OpeningGraphNode)
            assert child_node.fen == real_graph.get_node(ROOT_FEN).children[uci]

    def test_get_parents_returns_nodes(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        child_fen = root.children["e2e4"]
        parents = real_graph.get_parents(child_fen)
        assert len(parents) >= 1
        root_in_parents = any(p.fen == ROOT_FEN for p, _ in parents)
        assert root_in_parents


# -- Side-to-move (real data) --


class TestSideToMove:
    def test_root_is_white(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        assert root is not None
        assert root.side_to_move == "white"

    def test_after_e4_is_black(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        child_fen = root.children["e2e4"]
        child = real_graph.get_node(child_fen)
        assert child is not None
        assert child.side_to_move == "black"


# -- Labels (real data) --


class TestLabels:
    def test_sicilian_label(self, real_graph: OpeningGraph):
        """Position after 1.e4 c5 should be labeled Sicilian Defense."""
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("c7c5")
        fen = _fen_from_board(board)
        node = real_graph.get_node(fen)
        assert node is not None
        assert node.name is not None
        assert "Sicilian" in node.name

    def test_italian_game_label(self, real_graph: OpeningGraph):
        """Position after 1.e4 e5 2.Nf3 Nc6 3.Bc4 should be Italian Game."""
        board = chess.Board()
        for uci in ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]:
            board.push_uci(uci)
        fen = _fen_from_board(board)
        node = real_graph.get_node(fen)
        assert node is not None
        assert node.name is not None
        assert "Italian" in node.name


# -- Normalization --


class TestNormalization:
    def test_all_fens_are_four_fields(self, real_graph: OpeningGraph):
        """Every FEN key should have exactly 4 space-separated fields."""
        for fen in list(real_graph._nodes.keys())[:1000]:
            parts = fen.split(" ")
            assert len(parts) == 4, f"FEN has {len(parts)} fields: {fen}"

    def test_fen_from_board_matches_normalize_fen(self):
        """_fen_from_board should agree with normalize_fen for various positions."""
        board = chess.Board()
        positions = [
            [],
            ["e2e4"],
            ["e2e4", "e7e5"],
            ["e2e4", "d7d5"],  # EP position
            ["d2d4", "g8f6", "c2c4", "e7e6"],
        ]
        for moves in positions:
            board.reset()
            for uci in moves:
                board.push_uci(uci)
            from_board = _fen_from_board(board)
            from_normalize = normalize_fen(board.fen())
            assert from_board == from_normalize, f"Mismatch after {moves}"


# -- Immutability --


class TestImmutability:
    def test_children_is_mapping_proxy(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        assert isinstance(root.children, MappingProxyType)

    def test_parents_is_frozenset(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        child_fen = root.children["e2e4"]
        child = real_graph.get_node(child_fen)
        assert isinstance(child.parents, frozenset)

    def test_children_not_writable(self, real_graph: OpeningGraph):
        root = real_graph.get_node(ROOT_FEN)
        with pytest.raises(TypeError):
            root.children["z9z9"] = "fake"  # type: ignore[index]

    def test_node_attribute_not_reassignable(self, real_graph: OpeningGraph):
        """Setting any attribute on a frozen node should raise."""
        root = real_graph.get_node(ROOT_FEN)
        with pytest.raises(AttributeError, match="frozen"):
            root.name = "corrupted"
        with pytest.raises(AttributeError, match="frozen"):
            root.children = {}  # type: ignore[assignment]

    def test_node_attribute_not_deletable(self, real_graph: OpeningGraph):
        node = real_graph.get_node(ROOT_FEN)
        with pytest.raises(AttributeError, match="frozen"):
            del node.eco


# -- Synthetic build --


class TestSyntheticBuild:
    def test_node_and_edge_counts(self):
        """3 entries (e4, d4, e4 e5) produce 4 nodes and 3 edges."""
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_data(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)

        # root + after_e4 + after_d4 + after_e4_e5 = 4 nodes
        assert graph.node_count == 4
        # root->e4, root->d4, e4->e5 = 3 edges
        assert graph.edge_count == 3

    def test_labels_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_data(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)

        # Root and after_e4 have no label in byPosition
        root = graph.get_node(ROOT_FEN)
        assert root.eco is None

        # after_d4 should be labeled
        board = chess.Board()
        board.push_uci("d2d4")
        d4_node = graph.get_node(_fen_from_board(board))
        assert d4_node is not None
        assert d4_node.eco == "A00"
        assert d4_node.name == "Test Opening B"

    def test_parent_collision_with_synthetic_data(self):
        """Two entries reaching same child via same UCI from different parents."""
        with tempfile.TemporaryDirectory() as tmp:
            eco_path = Path(tmp) / "eco.json"
            bypos_path = Path(tmp) / "bypos.json"

            # Entry 1: e4 d5 (reaching d5 position)
            # Entry 2: e3 d5 (reaching same d5 move but from different parent)
            # Actually, d7d5 from different parent FENs produces different child FENs
            # because the board state differs. For a real collision we need a transposition.
            #
            # Better: two paths to the same position
            # Path A: 1.d4 Nf6 2.c4
            # Path B: 1.c4 Nf6 2.d4
            eco_data = {
                "dataset": "test",
                "source_commit": "abc",
                "entry_count": 2,
                "entries": [
                    {
                        "eco": "A00",
                        "name": "Path A",
                        "pgn": "1. d4 Nf6 2. c4",
                        "uci": "d2d4 g8f6 c2c4",
                        "epd": "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq -",
                    },
                    {
                        "eco": "A00",
                        "name": "Path B",
                        "pgn": "1. c4 Nf6 2. d4",
                        "uci": "c2c4 g8f6 d2d4",
                        "epd": "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq -",
                    },
                ],
            }
            bypos_data = {
                "dataset": "test",
                "source_commit": "abc",
                "position_count": 0,
                "by_position": {},
            }
            eco_path.write_text(json.dumps(eco_data))
            bypos_path.write_text(json.dumps(bypos_data))

            graph = build_opening_graph(eco_path, bypos_path)

        # The final position should have two parent edges (from different parents)
        board = chess.Board()
        for uci in ["d2d4", "g8f6", "c2c4"]:
            board.push_uci(uci)
        target_fen = _fen_from_board(board)

        node = graph.get_node(target_fen)
        assert node is not None
        parent_fens = {pf for pf, _ in node.parents}
        assert len(parent_fens) == 2


# -- Singleton --


class TestSingleton:
    @pytest.fixture(autouse=True)
    def _isolate_singleton(self, monkeypatch, tmp_path):
        """Patch build_opening_graph to return cheap synthetic graphs."""
        eco_path, bypos_path = _write_synthetic_data(tmp_path)

        def _cheap_build(eco_path=None, by_position_path=None):
            return build_opening_graph(eco_path or _eco, by_position_path or _bypos)

        _eco, _bypos = eco_path, bypos_path
        monkeypatch.setattr("app.opening_graph.build_opening_graph", _cheap_build)
        _reset_opening_graph_for_testing()
        yield
        _reset_opening_graph_for_testing()

    def test_returns_same_object(self):
        g1 = get_opening_graph()
        g2 = get_opening_graph()
        assert g1 is g2

    def test_reset_forces_rebuild(self):
        g1 = get_opening_graph()
        _reset_opening_graph_for_testing()
        g2 = get_opening_graph()
        assert g1 is not g2

    def test_no_path_args(self):
        """get_opening_graph() should not accept path arguments."""
        import inspect
        from app.opening_graph import get_opening_graph as real_fn
        sig = inspect.signature(real_fn)
        assert len(sig.parameters) == 0


# -- Cache --


class TestCache:
    def test_cache_roundtrip(self, tmp_path):
        """Cache save + load produces an equivalent graph."""
        eco_path, bypos_path = _write_synthetic_data(tmp_path)
        cache_dir = tmp_path / "cache"

        # First call: builds from scratch, writes cache
        g1 = _load_or_build(eco_path, bypos_path, cache_dir)
        assert g1.node_count == 4

        # Second call: loads from cache
        g2 = _load_or_build(eco_path, bypos_path, cache_dir)
        assert g2.node_count == g1.node_count
        assert g2.edge_count == g1.edge_count

    def test_cache_invalidates_on_version_bump(self, tmp_path, monkeypatch):
        """Bumping CACHE_VERSION forces a rebuild."""
        eco_path, bypos_path = _write_synthetic_data(tmp_path)
        from app import opening_graph

        cache_dir = tmp_path / "cache"

        # Build with current version
        _load_or_build(eco_path, bypos_path, cache_dir)
        cache_files_before = list(cache_dir.iterdir())
        assert len(cache_files_before) == 1

        # Bump version — old cache should be ignored
        monkeypatch.setattr(opening_graph, "CACHE_VERSION", 999)
        g2 = _load_or_build(eco_path, bypos_path, cache_dir)
        assert g2.node_count == 4
        # New cache file created with new version
        cache_files_after = list(cache_dir.iterdir())
        assert len(cache_files_after) == 2
