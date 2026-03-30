"""Tests for opening root derivation, family grouping, and ownership."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import chess
import pytest

from app.opening_graph import (
    OpeningGraph,
    _fen_from_board,
    build_opening_graph,
)
from app.opening_roots import (
    OpeningRoot,
    OpeningRoots,
    _reset_opening_roots_for_testing,
    build_opening_roots,
    derive_family,
    get_opening_roots,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_graph() -> OpeningGraph:
    return build_opening_graph()


@pytest.fixture(scope="module")
def real_roots(real_graph: OpeningGraph) -> OpeningRoots:
    return build_opening_roots(real_graph)


def _fen_after(moves: list[str]) -> str:
    """Return the normalized FEN after playing a sequence of UCI moves."""
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return _fen_from_board(board)


# ---------------------------------------------------------------------------
# Family derivation unit tests
# ---------------------------------------------------------------------------


class TestDeriveFamily:
    def test_alias_sicilian(self):
        assert derive_family("Sicilian: Najdorf") == "Sicilian Defense"

    def test_alias_spanish(self):
        assert derive_family("Spanish: Berlin Wall") == "Ruy Lopez"

    def test_alias_qgd(self):
        assert derive_family("QGD: Tartakower") == "Queen's Gambit Declined"

    def test_alias_qga(self):
        assert derive_family("QGA: Classical") == "Queen's Gambit Accepted"

    def test_alias_english(self):
        assert derive_family("English: Symmetrical") == "English Opening"

    def test_alias_french(self):
        assert derive_family("French: Winawer") == "French Defense"

    def test_alias_caro_kann(self):
        assert derive_family("Caro-Kann: Advance") == "Caro-Kann Defense"

    def test_alias_nimzo_indian(self):
        assert derive_family("Nimzo-Indian: Samisch") == "Nimzo-Indian Defense"

    def test_alias_kings_indian(self):
        assert derive_family("King's Indian: Classical") == "King's Indian Defense"

    def test_no_colon(self):
        assert derive_family("Italian Game") == "Italian Game"

    def test_no_alias(self):
        assert derive_family("Dutch: Leningrad") == "Dutch"

    def test_no_kings_pawn_alias(self):
        """King's Pawn should NOT alias to King's Pawn Game."""
        assert derive_family("King's Pawn") == "King's Pawn"

    def test_no_queens_pawn_alias(self):
        assert derive_family("Queen's Pawn") == "Queen's Pawn"


# ---------------------------------------------------------------------------
# Real-data tests
# ---------------------------------------------------------------------------


class TestBoundaryRootCount:
    def test_count(self, real_roots: OpeningRoots):
        assert real_roots.root_count == 11274


class TestChildrenNavigation:
    def test_get_children_none(self, real_roots: OpeningRoots):
        top_level = real_roots.get_children(None)
        assert len(top_level) > 0
        assert all(len(root.parent_keys) == 0 for root in top_level)
        assert top_level == sorted(top_level, key=lambda root: (root.depth, root.opening_key))

    def test_polish_single_top_level(self, real_roots: OpeningRoots):
        polish = [
            root for root in real_roots.get_children(None)
            if root.opening_name == "Polish Opening"
        ]
        assert len(polish) == 1

    def test_overlap_invariants(self, real_roots: OpeningRoots):
        multi_parent = sum(
            1 for root in real_roots._roots.values() if len(root.parent_keys) > 1
        )
        assert multi_parent > 0

        multiple_top_level = 0
        for root in real_roots._roots.values():
            top_level_ancestors = {
                candidate.opening_key
                for candidate in real_roots.get_children(None)
                if real_roots.is_descendant_of(root.opening_key, candidate.opening_key)
            }
            if len(top_level_ancestors) > 1:
                multiple_top_level += 1

        assert multiple_top_level > 0


class TestFamilyGrouping:
    def test_every_root_has_family(self, real_roots: OpeningRoots):
        for fam in real_roots.get_families():
            members = real_roots.get_family(fam)
            assert len(members) > 0
            for root in members:
                assert root.opening_family
                assert len(root.opening_family) > 0

    def test_sicilian_defense_family(self, real_roots: OpeningRoots):
        """Roots named 'Sicilian Defense' and 'Sicilian Defense: Najdorf Variation'
        should both have family 'Sicilian Defense'."""
        sic_members = real_roots.get_family("Sicilian Defense")
        assert len(sic_members) > 0
        names = {r.opening_name for r in sic_members}
        assert "Sicilian Defense" in names or any(
            "Sicilian Defense" in n for n in names
        )
        # Check a specific Sicilian Defense: variant is in the family
        najdorf_in_family = any(
            "Najdorf" in r.opening_name for r in sic_members
        )
        assert najdorf_in_family

    def test_alias_groups_sicilian_colon_names(self, real_roots: OpeningRoots):
        """Roots named 'Sicilian: ...' should also have family 'Sicilian Defense'."""
        sic_members = real_roots.get_family("Sicilian Defense")
        sicilian_colon = [
            r for r in sic_members if r.opening_name.startswith("Sicilian:")
        ]
        assert len(sicilian_colon) > 0

    def test_family_count(self, real_roots: OpeningRoots):
        assert real_roots.family_count > 600


class TestDAGStructure:
    def test_dag_is_acyclic(self, real_roots: OpeningRoots):
        """The full root DAG should have no cycles."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}

        def has_cycle_from(key: str) -> bool:
            stack = [(key, False)]
            while stack:
                node, returning = stack.pop()
                if returning:
                    color[node] = BLACK
                    continue
                if color.get(node) == BLACK:
                    continue
                if color.get(node) == GRAY:
                    return True
                color[node] = GRAY
                stack.append((node, True))
                root = real_roots.get_root(node)
                if root:
                    for ck in root.child_keys:
                        if color.get(ck) == GRAY:
                            return True
                        if color.get(ck) != BLACK:
                            stack.append((ck, False))
            return False

        for fam in real_roots.get_families():
            for r in real_roots.get_family(fam):
                assert not has_cycle_from(r.opening_key), (
                    f"Cycle detected from {r.opening_key}"
                )

    def test_multi_parent_exists(self, real_roots: OpeningRoots):
        """At least one boundary root should have multiple parent_keys."""
        found = False
        for fam in real_roots.get_families():
            for root in real_roots.get_family(fam):
                if len(root.parent_keys) > 1:
                    found = True
                    break
            if found:
                break
        assert found, "No boundary root with multiple parent_keys found"

    def test_is_descendant_of_najdorf(self, real_roots: OpeningRoots):
        """Najdorf root should be a descendant of Sicilian Defense root."""
        sic_members = real_roots.get_family("Sicilian Defense")
        najdorf_roots = [
            r for r in sic_members
            if "Najdorf Variation" in r.opening_name
            and r.opening_name.startswith("Sicilian Defense:")
        ]
        sicilian_base = [
            r for r in sic_members if r.opening_name == "Sicilian Defense"
        ]
        assert len(najdorf_roots) > 0
        assert len(sicilian_base) > 0

        # At least one Najdorf root should be a descendant of at least one
        # Sicilian Defense base root
        found = False
        for nr in najdorf_roots:
            for sb in sicilian_base:
                if real_roots.is_descendant_of(nr.opening_key, sb.opening_key):
                    found = True
                    break
            if found:
                break
        assert found

    def test_not_descendant_reverse(self, real_roots: OpeningRoots):
        """Sicilian Defense should NOT be a descendant of Najdorf."""
        sic_members = real_roots.get_family("Sicilian Defense")
        najdorf_roots = [
            r for r in sic_members
            if "Najdorf Variation" in r.opening_name
            and r.opening_name.startswith("Sicilian Defense:")
        ]
        sicilian_base = [
            r for r in sic_members if r.opening_name == "Sicilian Defense"
        ]
        if najdorf_roots and sicilian_base:
            for sb in sicilian_base:
                for nr in najdorf_roots:
                    assert not real_roots.is_descendant_of(
                        sb.opening_key, nr.opening_key
                    )


class TestFingerprint:
    def _roots_with_structure(
        self,
        *,
        parent_keys: frozenset[str] = frozenset(),
        child_keys: frozenset[str] = frozenset(),
        ownership: dict[str, frozenset[str]] | None = None,
    ) -> OpeningRoots:
        root = OpeningRoot(
            opening_key="root-a",
            opening_name="Example Opening",
            opening_family="Example Family",
            eco="A00",
            depth=1,
            parent_keys=parent_keys,
            child_keys=child_keys,
        )
        return OpeningRoots(
            {"root-a": root},
            ownership or {"fen-a": frozenset({"root-a"})},
        )

    def test_fingerprint_changes_when_ownership_changes(self):
        roots_a = self._roots_with_structure(ownership={"fen-a": frozenset({"root-a"})})
        roots_b = self._roots_with_structure(ownership={"fen-a": frozenset({"root-a"}), "fen-b": frozenset({"root-a"})})

        assert roots_a.fingerprint != roots_b.fingerprint

    def test_fingerprint_changes_when_parent_child_links_change(self):
        roots_a = self._roots_with_structure(child_keys=frozenset({"child-a"}))
        roots_b = self._roots_with_structure(parent_keys=frozenset({"parent-a"}))

        assert roots_a.fingerprint != roots_b.fingerprint


    def test_same_name_parent_edge(self, real_roots: OpeningRoots):
        """Same-name boundary roots at different FENs should still have
        parent/child edges. Regression test: the name guard was dropping
        these edges."""
        # Find any boundary root whose parent_keys includes a root with
        # the same opening_name
        found = False
        for fam in real_roots.get_families():
            for root in real_roots.get_family(fam):
                for pk in root.parent_keys:
                    parent = real_roots.get_root(pk)
                    if parent and parent.opening_name == root.opening_name:
                        found = True
                        # Also verify is_descendant_of works
                        assert real_roots.is_descendant_of(
                            root.opening_key, pk
                        )
                        break
                if found:
                    break
            if found:
                break
        assert found, "No same-name parent edge found (expected ~120)"

    def test_zukertort_sicilian_invitation_ancestry(
        self, real_roots: OpeningRoots, real_graph: OpeningGraph,
    ):
        """Concrete case: two 'Zukertort Opening: Sicilian Invitation' roots
        should have a parent-child edge in the root DAG."""
        zuk_roots = [
            r for fam in real_roots.get_families()
            for r in real_roots.get_family(fam)
            if r.opening_name == "Zukertort Opening: Sicilian Invitation"
        ]
        assert len(zuk_roots) >= 2, (
            f"Expected >=2 Zukertort Sicilian Invitation roots, got {len(zuk_roots)}"
        )
        # At least one pair should have a descendant relationship
        found_pair = False
        for i, a in enumerate(zuk_roots):
            for b in zuk_roots[i + 1:]:
                if (real_roots.is_descendant_of(a.opening_key, b.opening_key)
                        or real_roots.is_descendant_of(b.opening_key, a.opening_key)):
                    found_pair = True
                    break
            if found_pair:
                break
        assert found_pair


class TestOwnership:
    def test_najdorf_position_ownership(self, real_roots: OpeningRoots):
        """Position after Najdorf moves should be owned by the Najdorf root(s),
        not only 'Sicilian Defense'."""
        # 1.e4 c5 2.Nf3 d6 3.d4 cxd4 4.Nxd4 Nf6 5.Nc3 a6
        najdorf_fen = _fen_after([
            "e2e4", "c7c5", "g1f3", "d7d6",
            "d2d4", "c5d4", "f3d4", "g8f6",
            "b1c3", "a7a6",
        ])
        owners = real_roots.owning_root_keys(najdorf_fen)
        assert len(owners) > 0

        # Check that at least one owner is a Najdorf-related root
        owner_names = set()
        for ok in owners:
            root = real_roots.get_root(ok)
            if root:
                owner_names.add(root.opening_name)
        assert any("Najdorf" in n for n in owner_names), (
            f"Expected Najdorf owner, got: {owner_names}"
        )

    def test_graph_root_has_no_owners(self, real_roots: OpeningRoots):
        """The starting position has no owning roots."""
        root_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
        owners = real_roots.owning_root_keys(root_fen)
        assert len(owners) == 0

    def test_boundary_root_owns_itself(self, real_roots: OpeningRoots):
        """A boundary root's owning_root_keys should include itself."""
        sic_members = real_roots.get_family("Sicilian Defense")
        for root in sic_members[:5]:
            owners = real_roots.owning_root_keys(root.opening_key)
            assert root.opening_key in owners


# ---------------------------------------------------------------------------
# Synthetic tests
# ---------------------------------------------------------------------------


def _write_synthetic_tree(tmp: Path) -> tuple[Path, Path]:
    """3 entries forming a simple tree: root -> A -> B, root -> C."""
    eco_path = tmp / "eco.json"
    bypos_path = tmp / "bypos.json"

    eco_data = {
        "dataset": "test",
        "source_commit": "abc",
        "entry_count": 3,
        "entries": [
            {
                "eco": "A00",
                "name": "Opening A",
                "pgn": "1. e4",
                "uci": "e2e4",
                "epd": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -",
            },
            {
                "eco": "B00",
                "name": "Opening B",
                "pgn": "1. e4 e5",
                "uci": "e2e4 e7e5",
                "epd": "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -",
            },
            {
                "eco": "A00",
                "name": "Opening C",
                "pgn": "1. d4",
                "uci": "d2d4",
                "epd": "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -",
            },
        ],
    }

    bypos_data = {
        "dataset": "test",
        "source_commit": "abc",
        "position_count": 3,
        "by_position": {
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Opening A",
            },
            "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -": {
                "eco": "B00",
                "name": "Opening B",
            },
            "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Opening C",
            },
        },
    }

    eco_path.write_text(json.dumps(eco_data))
    bypos_path.write_text(json.dumps(bypos_data))
    return eco_path, bypos_path


class TestSyntheticTree:
    def test_boundary_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_tree(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        # root (unnamed) -> A -> B, root -> C = 3 boundary roots
        assert roots.root_count == 3

    def test_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_tree(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        families = roots.get_families()
        assert "Opening A" in families
        assert "Opening B" in families
        assert "Opening C" in families

    def test_child_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_tree(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        # Opening A should have Opening B as a child
        a_roots = roots.get_family("Opening A")
        assert len(a_roots) == 1
        a_root = a_roots[0]
        assert len(a_root.child_keys) == 1

        b_roots = roots.get_family("Opening B")
        assert len(b_roots) == 1
        b_root = b_roots[0]
        assert b_root.opening_key in a_root.child_keys

        # Opening C should have no children
        c_roots = roots.get_family("Opening C")
        assert len(c_roots) == 1
        assert len(c_roots[0].child_keys) == 0

    def test_get_children_with_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_tree(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        opening_a = roots.get_family("Opening A")[0]
        children = roots.get_children(opening_a.opening_key)
        assert [child.opening_name for child in children] == ["Opening B"]

    def test_get_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_synthetic_tree(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        opening_a = roots.get_family("Opening A")[0]
        opening_c = roots.get_family("Opening C")[0]

        assert [root.opening_name for root in roots.get_descendants(opening_a.opening_key)] == [
            "Opening B"
        ]
        assert roots.get_descendants(opening_c.opening_key) == []


def _write_transposition_data(tmp: Path) -> tuple[Path, Path]:
    """Two paths reaching the same position with different opening names."""
    eco_path = tmp / "eco.json"
    bypos_path = tmp / "bypos.json"

    # Path A: 1.d4 Nf6 2.c4 -> "Opening X" then "Opening Z"
    # Path B: 1.c4 Nf6 2.d4 -> "Opening Y" then "Opening Z"
    # Position after d4+c4+Nf6 is the same via transposition
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
        "position_count": 5,
        "by_position": {
            # After d4
            "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Opening X",
            },
            # After c4
            "rnbqkbnr/pppppppp/8/8/2P5/8/PP1PPPPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Opening Y",
            },
            # After d4 Nf6
            "rnbqkb1r/pppppppp/5n2/8/3P4/8/PPP1PPPP/RNBQKBNR w KQkq -": {
                "eco": "A00",
                "name": "Opening X: Nf6",
            },
            # After c4 Nf6
            "rnbqkb1r/pppppppp/5n2/8/2P5/8/PP1PPPPP/RNBQKBNR w KQkq -": {
                "eco": "A00",
                "name": "Opening Y: Nf6",
            },
            # Transposition point: after d4 Nf6 c4 = c4 Nf6 d4
            "rnbqkb1r/pppppppp/5n2/8/2PP4/8/PP2PPPP/RNBQKBNR b KQkq -": {
                "eco": "A00",
                "name": "Opening Z",
            },
        },
    }

    eco_path.write_text(json.dumps(eco_data))
    bypos_path.write_text(json.dumps(bypos_data))
    return eco_path, bypos_path


class TestTransposition:
    def test_boundary_root_has_two_parent_keys(self):
        """Opening Z is reachable from both Opening X: Nf6 and Opening Y: Nf6,
        so it should have 2 parent_keys."""
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_transposition_data(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        z_roots = roots.get_family("Opening Z")
        assert len(z_roots) == 1
        z_root = z_roots[0]
        assert len(z_root.parent_keys) == 2

    def test_ownership_through_transposition(self):
        """A position reachable via two boundary roots should be owned by both."""
        with tempfile.TemporaryDirectory() as tmp:
            eco_path, bypos_path = _write_transposition_data(Path(tmp))
            graph = build_opening_graph(eco_path, bypos_path)
            roots = build_opening_roots(graph)

        # Opening Z is itself a boundary root and should own itself
        z_roots = roots.get_family("Opening Z")
        z_root = z_roots[0]
        owners = roots.owning_root_keys(z_root.opening_key)
        assert z_root.opening_key in owners

    def test_get_descendants_deduplicates_shared_node(self):
        root_a = OpeningRoot(
            opening_key="root-a",
            opening_name="Root A",
            opening_family="Root A",
            eco="A00",
            depth=1,
            parent_keys=frozenset(),
            child_keys=frozenset({"root-b", "root-c"}),
        )
        root_b = OpeningRoot(
            opening_key="root-b",
            opening_name="Root B",
            opening_family="Root B",
            eco="A01",
            depth=2,
            parent_keys=frozenset({"root-a"}),
            child_keys=frozenset({"root-d"}),
        )
        root_c = OpeningRoot(
            opening_key="root-c",
            opening_name="Root C",
            opening_family="Root C",
            eco="A02",
            depth=2,
            parent_keys=frozenset({"root-a"}),
            child_keys=frozenset({"root-d"}),
        )
        root_d = OpeningRoot(
            opening_key="root-d",
            opening_name="Root D",
            opening_family="Root D",
            eco="A03",
            depth=3,
            parent_keys=frozenset({"root-b", "root-c"}),
            child_keys=frozenset(),
        )

        roots = OpeningRoots(
            {
                root_a.opening_key: root_a,
                root_b.opening_key: root_b,
                root_c.opening_key: root_c,
                root_d.opening_key: root_d,
            },
            {},
        )

        descendants = roots.get_descendants(root_a.opening_key)
        assert [root.opening_key for root in descendants] == [
            "root-b",
            "root-c",
            "root-d",
        ]


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    @pytest.fixture(autouse=True)
    def _isolate(self):
        _reset_opening_roots_for_testing()
        yield
        _reset_opening_roots_for_testing()

    def test_returns_same_object(self):
        r1 = get_opening_roots()
        r2 = get_opening_roots()
        assert r1 is r2

    def test_reset_forces_rebuild(self):
        r1 = get_opening_roots()
        _reset_opening_roots_for_testing()
        r2 = get_opening_roots()
        assert r1 is not r2
