"""Tests for GET /api/openings/tree (g-tree-api).

The endpoint hydrates one column per position along a canonical move line. These
tests drive it with a small synthetic opening graph + evidence overlay patched
into the route, plus controllable position-row / eval lookups, so each acceptance
bullet (graph inclusion, phase-boundary terminals, off-book observed edges,
transpositions, canonical URLs, null evidence, sorting, eval integration, drill
flags, errors) is exercised in isolation. The exact/normalized eval-resolution
logic itself lives in (and is tested by) test_tree_eval.py.
"""
from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import chess
import pytest

from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
)
from app.opening_aggregate import CachedPositionScoreRow
from app.opening_cache import opening_score_inputs_fingerprint
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_roots import OpeningRoot, OpeningRoots
from app.tree_eval import MoveEval

# --- normalized 4-field FENs along the synthetic graph ------------------------
START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -"
E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq -"
E4E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"
NF3 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -"
NC6 = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"
BC4 = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq -"
PETROV = "rnbqkb1r/pppp1ppp/5n2/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq -"
PETROV_NXE5 = "rnbqkb1r/pppp1ppp/5n2/4N3/4P3/8/PPPP1PPP/RNBQKB1R b KQkq -"
SICILIAN = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"

TREE_URL = "/api/openings/tree"


def _node(fen: str, name: str | None = None, eco: str | None = None) -> OpeningGraphNode:
    from app.fen import active_color

    node = OpeningGraphNode(fen, active_color(fen))
    node.name = name
    node.eco = eco
    return node


def _make_graph() -> OpeningGraph:
    """1.e4 e5 2.Nf3 (Nc6 -> Bc4 | Nf6) and the 1.e4 c5 Sicilian branch."""
    nodes = {
        START: _node(START),
        E4: _node(E4, "King's Pawn Game", "B00"),
        E4E5: _node(E4E5, "King's Pawn Game", "C20"),
        NF3: _node(NF3, "King's Knight Opening", "C40"),
        NC6: _node(NC6, "King's Knight: Normal Variation", "C44"),
        BC4: _node(BC4, "Italian Game", "C50"),
        PETROV: _node(PETROV, "Petrov's Defense", "C42"),
        PETROV_NXE5: _node(PETROV_NXE5, "Petrov: Classical", "C42"),
        SICILIAN: _node(SICILIAN, "Sicilian Defense", "B20"),
    }
    edges = [
        (START, "e2e4", E4),
        (E4, "e7e5", E4E5),
        (E4, "c7c5", SICILIAN),
        (E4E5, "g1f3", NF3),
        (NF3, "b8c6", NC6),
        (NF3, "g8f6", PETROV),
        (PETROV, "f3e5", PETROV_NXE5),
        (NC6, "f1c4", BC4),
    ]
    for parent, uci, child in edges:
        nodes[parent].children[uci] = child
        nodes[child].parents.add((parent, uci))
    return OpeningGraph(nodes, START)


def _make_roots() -> OpeningRoots:
    """Italian Game and Sicilian Defense are named drill roots; nothing else is."""
    roots = {
        BC4: OpeningRoot(BC4, "Italian Game", "Italian Game", "C50", 5,
                         frozenset(), frozenset()),
        SICILIAN: OpeningRoot(SICILIAN, "Sicilian Defense", "Sicilian Defense", "B20",
                              2, frozenset(), frozenset()),
    }
    ownership = {key: frozenset({key}) for key in roots}
    return OpeningRoots(roots, ownership)


def _overlay(player_color: str, edges: dict | None = None,
             nodes: dict | None = None) -> EvidenceOverlay:
    overlay = EvidenceOverlay(user_id=123, player_color=player_color)
    overlay.edges = edges or {}
    overlay.nodes = nodes or {}
    return overlay


def _obs_edge(ucis: list[str], **counts) -> tuple[tuple[str, str], EdgeEvidence]:
    """Build an observed EdgeEvidence keyed by (parent_norm, child_norm)."""
    board = chess.Board()
    for uci in ucis[:-1]:
        board.push(chess.Move.from_uci(uci))
    parent = normalize_fen(board.fen())
    board.push(chess.Move.from_uci(ucis[-1]))
    child = normalize_fen(board.fen())
    edge = EdgeEvidence(parent_fen=parent, child_fen=child, uci=ucis[-1], **counts)
    return (parent, child), edge


def _observed_by_parent(overlay: EvidenceOverlay) -> dict[str, list[EdgeEvidence]]:
    """Group an overlay's observed edges by normalized parent FEN.

    The persisted ``opening_position_edges`` read model is keyed by
    ``(batch_id, parent_fen)``; this reproduces that grouping in memory so the
    patched ``lookup_observed_edges_for_parent`` can serve each parent's edges.
    """
    by_parent: dict[str, list[EdgeEvidence]] = defaultdict(list)
    for (parent_fen, _child_fen), edge in overlay.edges.items():
        by_parent[parent_fen].append(edge)
    return by_parent


def _full_fen(ucis: list[str]) -> str:
    board = chess.Board()
    for uci in ucis:
        board.push(chess.Move.from_uci(uci))
    return board.fen()


def _batch(computed_at: datetime | None = None) -> OpeningScoreBatch:
    batch = OpeningScoreBatch(id=1, user_id=123, player_color="white", generation=1)
    batch.computed_at = computed_at or datetime(2026, 6, 1, tzinfo=timezone.utc)
    return batch


def _pos_row(fen: str, *, score=None, confidence=None, coverage=None,
             sample_size=0, game_count=0, last_practiced_at=None,
             has_evidence=False, in_book=True) -> CachedPositionScoreRow:
    return CachedPositionScoreRow(
        normalized_fen=fen,
        player_color="white",
        in_book=in_book,
        has_evidence=has_evidence,
        opening_score=score,
        confidence=confidence,
        coverage=coverage,
        weighted_depth=None,
        sample_size=sample_size,
        game_count=game_count,
        last_practiced_at=last_practiced_at,
    )


def _call(client, auth_headers, *, params, graph=None, roots=None, overlay=None,
          batch=None, position_rows=None, move_evals=None, root_eval=None,
          mid_fens=None, user_id=123):
    """Issue a GET /tree with all DB-touching collaborators patched.

    The persisted tree read models are simulated in memory: ``ensure_tree_cache``
    resolves a ``(batch_id, computed_at, cache_state)`` triple, observed edges are
    served from the ``overlay`` fixture via the patched per-parent lookup, and
    position rows from ``position_rows`` via the patched batch lookup. ``batch_id`` is
    non-None whenever the fixture carries any cache content (a batch, observed edges,
    or position rows); the cold/no-evidence book-only path is modelled by leaving all
    three empty so ``batch_id`` resolves to None.
    """
    graph = graph or _make_graph()
    roots = roots or _make_roots()
    overlay = overlay if overlay is not None else _overlay("white")
    position_rows = position_rows or {}
    move_evals = move_evals or {}
    observed_by_parent = _observed_by_parent(overlay)

    has_cache = batch is not None or bool(observed_by_parent) or bool(position_rows)
    batch_id = (batch.id if batch is not None else 1) if has_cache else None
    batch_computed_at = batch.computed_at if batch is not None else None
    cache_state = "book_only" if batch_id is None else "warm_fresh"

    def _ensure(db, uid, color, g, r):
        return batch_id, batch_computed_at, cache_state

    def _loep(db, b_id, parent_fen):
        return list(observed_by_parent.get(parent_fen, []))

    def _lpsfb(db, b_id, fens):
        return position_rows

    def _lme(db, requests):
        # move_evals may be keyed by uci (tests use unique ucis per column).
        return {req: move_evals.get(req[1]) for req in requests}

    def _lre(db, fen):
        return root_eval

    cms = [
        patch("app.api.openings.get_opening_graph", return_value=graph),
        patch("app.api.openings.get_opening_roots", return_value=roots),
        patch("app.api.openings.ensure_tree_cache", side_effect=_ensure),
        patch("app.api.openings.lookup_observed_edges_for_parent", side_effect=_loep),
        patch("app.api.openings.lookup_position_scores_for_batch", side_effect=_lpsfb),
        patch("app.api.openings.lookup_move_evals", side_effect=_lme),
        patch("app.api.openings.lookup_root_eval", side_effect=_lre),
    ]
    if mid_fens is not None:
        cms.append(patch("app.api.openings.is_middlegame_position",
                         side_effect=lambda fen: fen in mid_fens))
    with contextlib.ExitStack() as stack:
        for cm in cms:
            stack.enter_context(cm)
        return client.get(TREE_URL, params=params, headers=auth_headers(user_id=user_id))


def _make_builder(graph, roots, overlay, player_color="white", *, batch_id=1,
                  batch_computed_at=None, user_id=123):
    """Construct an _OpeningTreeBuilder whose observed edges resolve from ``overlay``.

    Returns ``(builder, patch_cm)``: enter ``patch_cm`` around any call that reaches
    ``_observed_children`` so the per-parent lookup is served from the in-memory
    overlay instead of the real ``opening_position_edges`` table.
    """
    from app.api.openings import _OpeningTreeBuilder

    by_parent = _observed_by_parent(overlay)

    def _loep(db, b_id, parent_fen):
        return list(by_parent.get(parent_fen, []))

    builder = _OpeningTreeBuilder(
        None, graph, roots, batch_id, batch_computed_at, player_color, user_id
    )
    return builder, patch(
        "app.api.openings.lookup_observed_edges_for_parent", side_effect=_loep
    )


def _ucis(column: dict) -> list[str]:
    return [n["uci"] for n in column["nodes"]]


def _by_uci(column: dict, uci: str) -> dict:
    return next(n for n in column["nodes"] if n["uci"] == uci)


# --- auth / validation --------------------------------------------------------

def test_tree_no_auth_returns_401(client):
    resp = client.get(TREE_URL, params={"player_color": "white"})
    assert resp.status_code == 401


def test_tree_invalid_color_returns_422(client, auth_headers):
    resp = _call(client, auth_headers, params={"player_color": "red"})
    assert resp.status_code == 422


def test_tree_malformed_uci_returns_422(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4", "not-a-move"]})
    assert resp.status_code == 422


def test_tree_bad_opening_fen_returns_422(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "opening": "totally not a fen"})
    assert resp.status_code == 422


# --- default root + graph inclusion ------------------------------------------

def test_tree_default_root_lists_book_first_move(client, auth_headers):
    resp = _call(client, auth_headers, params={"player_color": "white"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["canonical_line"] == []
    assert data["selected_fen"] == START
    assert data["selected_ply"] == 0
    assert data["selected_is_terminal"] is False
    assert data["model_version"] == "sm-v2-2"
    assert len(data["columns"]) == 1
    col = data["columns"][0]
    assert col["position_fen"] == START
    assert col["selected_uci"] is None
    assert _ucis(col) == ["e2e4"]
    node = col["nodes"][0]
    assert node["in_book"] is True and node["is_navigable"] is True
    assert node["san"] == "e4"
    assert node["ply"] == 1


def test_tree_timing_log_can_be_forced(client, auth_headers, monkeypatch, caplog):
    monkeypatch.setenv("OPENING_TREE_TIMING_LOG", "true")
    caplog.set_level(logging.INFO, logger="app.api.openings")

    resp = _call(client, auth_headers, params={"player_color": "white"})

    assert resp.status_code == 200
    messages = [record.getMessage() for record in caplog.records]
    timing = next(message for message in messages if message.startswith("opening_tree timing"))
    assert "ensure_cache_ms=" in timing
    assert "cache_state=" in timing
    assert "observed_edge_queries=" in timing
    assert "position_rows_ms=" in timing
    assert "move_evals_ms=" in timing
    assert "total_ms=" in timing


def test_tree_columns_exclude_legal_but_unknown_moves(client, auth_headers):
    # The E4 column must list only book/observed black replies, never every legal
    # move (e.g. 1...d5 is legal but absent from this book).
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4"]})
    data = resp.json()
    e4_col = data["columns"][1]
    assert set(_ucis(e4_col)) == {"e7e5", "c7c5"}
    assert "d7d5" not in _ucis(e4_col)


def test_tree_move_line_builds_one_column_per_position(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "e7e5"]
    assert data["selected_fen"] == E4E5
    assert data["selected_ply"] == 2
    positions = [c["position_fen"] for c in data["columns"]]
    assert positions == [START, E4, E4E5]
    # selected_uci threads the line; reveal column (k) has none.
    assert data["columns"][0]["selected_uci"] == "e2e4"
    assert data["columns"][1]["selected_uci"] == "e7e5"
    assert data["columns"][2]["selected_uci"] is None
    # is_selected only on the navigable selected move of each non-reveal column.
    assert _by_uci(data["columns"][0], "e2e4")["is_selected"] is True
    assert _by_uci(data["columns"][1], "e7e5")["is_selected"] is True
    assert _by_uci(data["columns"][1], "c7c5")["is_selected"] is False


def test_tree_deepest_opening_name_inherited(client, auth_headers):
    # NF3 carries its own name; reveal column at NF3 lists Nc6 (own name) and Nf6
    # (own name). Give NC6 no name to verify inheritance from the deepest ancestor.
    graph = _make_graph()
    graph.get_node(NC6).name = None
    graph.get_node(NC6).eco = None
    resp = _call(client, auth_headers, graph=graph,
                 params={"player_color": "white", "move": ["e2e4", "e7e5", "g1f3"]})
    data = resp.json()
    nf3_col = data["columns"][3]
    nc6 = _by_uci(nf3_col, "b8c6")
    # Inherits the deepest named ancestor along the line (NF3's name).
    assert nc6["opening_name"] == "King's Knight Opening"
    assert nc6["eco"] == "C40"
    nf6 = _by_uci(nf3_col, "g8f6")
    assert nf6["opening_name"] == "Petrov's Defense"


# --- canonical URLs -----------------------------------------------------------

def test_tree_stale_move_truncates_to_canonical(client, auth_headers):
    # 1.e4 then a legal-but-unknown black reply (1...d5) truncates the line.
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4", "d7d5"]})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4"]
    assert data["selected_fen"] == E4
    assert data["selected_ply"] == 1


def test_tree_cycle_truncates():
    # A knight round-trip (Nf3 Nf6 Ng1 Ng8) returns to START's normalized FEN. The
    # visited-set guard must truncate before re-entering it. Drive the builder's
    # resolver directly with observed edges forming the cycle.
    round_trip = ["g1f3", "g8f6", "f3g1", "f6g8"]
    edges = dict(_obs_edge(round_trip[: i + 1], traversal_count=1)
                 for i in range(len(round_trip)))
    overlay = _overlay("white", edges=edges)
    root_only = OpeningGraph({START: _node(START)}, START)
    builder, observed_patch = _make_builder(root_only, _make_roots(), overlay)
    with observed_patch:
        # The 4th move would revisit START (already seeded) -> truncate to 3 moves.
        assert builder._resolve_moves(round_trip) == ["g1f3", "g8f6", "f3g1"]


def test_tree_legacy_opening_resolves_via_bfs(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "opening": E4E5})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "e7e5"]
    assert data["selected_fen"] == E4E5


def test_tree_legacy_opening_matches_move_line(client, auth_headers):
    via_opening = _call(client, auth_headers,
                        params={"player_color": "white", "opening": NF3}).json()
    via_moves = _call(client, auth_headers,
                      params={"player_color": "white",
                              "move": ["e2e4", "e7e5", "g1f3"]}).json()
    assert via_opening["canonical_line"] == via_moves["canonical_line"]
    assert via_opening["selected_fen"] == via_moves["selected_fen"]


def test_tree_out_of_graph_opening_resolves_to_root(client, auth_headers):
    # A valid FEN that is not reachable in the book → empty line (root), not 404.
    unreachable = "8/8/8/4k3/8/4K3/8/8 w - -"
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "opening": unreachable})
    assert resp.status_code == 200
    assert resp.json()["canonical_line"] == []
    assert resp.json()["selected_fen"] == START


def test_tree_root_opening_resolves_to_empty_line(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "opening": START})
    assert resp.json()["canonical_line"] == []


# --- phase-boundary terminals -------------------------------------------------

def test_tree_middlegame_book_child_is_terminal_boundary(client, auth_headers):
    # Mark NC6 as a middlegame position: from NF3 (not middlegame) it becomes a
    # display-only, non-navigable boundary node, while Nf6 stays navigable.
    resp = _call(client, auth_headers, mid_fens={NC6},
                 params={"player_color": "white", "move": ["e2e4", "e7e5", "g1f3"]})
    data = resp.json()
    nf3_col = data["columns"][3]
    assert set(_ucis(nf3_col)) == {"b8c6", "g8f6"}
    boundary = _by_uci(nf3_col, "b8c6")
    assert boundary["is_navigable"] is False
    assert boundary["in_book"] is True
    assert boundary["terminal_reason"] == "opening_boundary"
    assert boundary["opening_score"] is None
    navigable = _by_uci(nf3_col, "g8f6")
    assert navigable["is_navigable"] is True
    assert navigable["terminal_reason"] is None


def test_tree_stale_url_into_boundary_truncates(client, auth_headers):
    # Selecting the non-navigable middlegame boundary move truncates the line.
    resp = _call(client, auth_headers, mid_fens={NC6},
                 params={"player_color": "white",
                         "move": ["e2e4", "e7e5", "g1f3", "b8c6"]})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "e7e5", "g1f3"]
    assert data["selected_fen"] == NF3


def test_tree_selected_terminal_reason_direct(client, auth_headers):
    # k==0 legacy resolving onto a position whose only children are middlegame
    # boundaries is itself non-terminal (it still reveals a final column), but a
    # leaf (no column children at all) reports a direct terminal reason (Bug B).
    # BC4 has no book children and no observed edges → no_children leaf.
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "opening": BC4})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "e7e5", "g1f3", "b8c6", "f1c4"]
    assert data["selected_fen"] == BC4
    assert data["selected_is_terminal"] is True
    assert data["selected_terminal_reason"] == "no_children"
    # A leaf deepest position yields no column k.
    assert [c["position_fen"] for c in data["columns"]][-1] == NC6


# --- observed off-book edges --------------------------------------------------

def test_tree_observed_off_book_edge_appears_navigable(client, auth_headers):
    # An off-book white 3rd move (d2d4) observed after 1.e4 e5: it is navigable,
    # observed, carries counts, and (absent a position row) renders no-data.
    edge_key, edge = _obs_edge(
        ["e2e4", "e7e5", "d2d4"], traversal_count=3, live_attempts=2, live_passes=1
    )
    overlay = _overlay("white", edges={edge_key: edge})
    resp = _call(client, auth_headers, overlay=overlay,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    data = resp.json()
    e4e5_col = data["columns"][2]
    assert "d2d4" in _ucis(e4e5_col)
    off_book = _by_uci(e4e5_col, "d2d4")
    assert off_book["is_observed"] is True
    assert off_book["in_book"] is False
    assert off_book["is_navigable"] is True
    assert off_book["is_prepared"] is True  # live_attempts >= 2
    assert off_book["user_choice_count"] == 2
    assert off_book["encounter_count"] == 3
    assert off_book["opening_score"] is None  # no row -> no-data
    # And the off-book move is now selectable (navigable into canonical_line).
    deeper = _call(client, auth_headers, overlay=overlay,
                   params={"player_color": "white",
                           "move": ["e2e4", "e7e5", "d2d4"]}).json()
    assert deeper["canonical_line"] == ["e2e4", "e7e5", "d2d4"]


def test_tree_observed_in_book_edge_sets_both_flags(client, auth_headers):
    edge_key, edge = _obs_edge(
        ["e2e4", "e7e5"], traversal_count=4, live_attempts=1, live_passes=1
    )
    overlay = _overlay("white", edges={edge_key: edge})
    resp = _call(client, auth_headers, overlay=overlay,
                 params={"player_color": "white", "move": ["e2e4"]})
    e4_col = resp.json()["columns"][1]
    e5 = _by_uci(e4_col, "e7e5")
    assert e5["in_book"] is True and e5["is_observed"] is True
    assert e5["encounter_count"] == 4


# --- replay legality guard (finding #4) --------------------------------------

def test_tree_illegal_observed_edge_is_skipped_not_500(client, auth_headers):
    # A corrupt observed edge: structurally listed at E4 but its uci is illegal on
    # the real board (a1a8). It must be skipped during hydration, never 500.
    parent = E4
    child = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/1NBQKBNR b KQkq -"  # fabricated
    bad_edge = EdgeEvidence(parent_fen=parent, child_fen=child, uci="a1a8",
                            traversal_count=1)
    overlay = _overlay("white", edges={(parent, child): bad_edge})
    resp = _call(client, auth_headers, overlay=overlay,
                 params={"player_color": "white", "move": ["e2e4"]})
    assert resp.status_code == 200
    e4_col = resp.json()["columns"][1]
    assert "a1a8" not in _ucis(e4_col)
    # And selecting the illegal edge truncates rather than corrupting replay.
    truncated = _call(client, auth_headers, overlay=overlay,
                      params={"player_color": "white", "move": ["e2e4", "a1a8"]}).json()
    assert truncated["canonical_line"] == ["e2e4"]


# --- transpositions -----------------------------------------------------------

def test_tree_two_ucis_to_same_child_are_distinct_nodes(client, auth_headers):
    # Add a second book move from START that transposes to E4 (illegal in reality,
    # but the column lists one node per UCI edge and never dedups by child_fen).
    graph = _make_graph()
    graph.get_node(START).children["d2d4"] = E4  # fabricated transposition edge
    resp = _call(client, auth_headers, graph=graph,
                 params={"player_color": "white"})
    start_col = resp.json()["columns"][0]
    # Both UCIs are present as distinct nodes even though they share a child_fen.
    assert sorted(_ucis(start_col)) == ["d2d4", "e2e4"]
    assert {n["child_fen"] for n in start_col["nodes"]} == {E4}


# --- null evidence ------------------------------------------------------------

def test_tree_null_evidence_serves_structural_book_tree(client, auth_headers):
    # No batch, no overlay: the structural book tree still renders and navigates,
    # with null metrics throughout.
    resp = _call(client, auth_headers, batch=None,
                 params={"player_color": "white", "move": ["e2e4"]})
    data = resp.json()
    assert data["batch_computed_at"] is None
    e4_col = data["columns"][1]
    for node in e4_col["nodes"]:
        assert node["opening_score"] is None
        assert node["sample_size"] is None
        assert node["is_navigable"] is True


# --- metrics + drill flags + batch metadata ----------------------------------

def test_tree_hydrates_position_metrics_and_batch_metadata(client, auth_headers):
    batch = _batch(datetime(2026, 6, 10, tzinfo=timezone.utc))
    rows = {
        E4E5: _pos_row(E4E5, score=72.5, confidence=0.6, coverage=0.4,
                       sample_size=8, game_count=3,
                       last_practiced_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                       has_evidence=True),
    }
    resp = _call(client, auth_headers, batch=batch, position_rows=rows,
                 params={"player_color": "white", "move": ["e2e4"]})
    data = resp.json()
    assert "2026-06-10" in data["batch_computed_at"]
    e5 = _by_uci(data["columns"][1], "e7e5")
    assert e5["opening_score"] == pytest.approx(72.5)
    assert e5["confidence"] == pytest.approx(0.6)
    assert e5["sample_size"] == 8
    assert e5["game_count"] == 3
    assert "2026-05-01" in e5["last_practiced_at"]


def test_tree_drill_opening_key_only_on_named_roots(client, auth_headers):
    # Sicilian and Italian Game are named roots; other positions are not.
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4"]})
    e4_col = resp.json()["columns"][1]
    assert _by_uci(e4_col, "c7c5")["drill_opening_key"] == SICILIAN
    assert _by_uci(e4_col, "e7e5")["drill_opening_key"] is None


def test_tree_top_level_drill_key_for_selected_named_root(client, auth_headers):
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4", "c7c5"]})
    data = resp.json()
    assert data["selected_fen"] == SICILIAN
    assert data["drill_opening_key"] == SICILIAN


# --- eval integration ---------------------------------------------------------

def test_tree_eval_fields_passed_through_white_relative(client, auth_headers):
    move_evals = {"e2e4": MoveEval(cp=37, mate=None)}
    root_eval = MoveEval(cp=21, mate=None)
    resp = _call(client, auth_headers, move_evals=move_evals, root_eval=root_eval,
                 params={"player_color": "white"})
    data = resp.json()
    assert data["root_eval_cp"] == 21
    assert data["root_eval_mate"] is None
    e4 = _by_uci(data["columns"][0], "e2e4")
    assert e4["eval_cp"] == 37
    assert e4["eval_mate"] is None


def test_tree_eval_mate_over_cp_and_miss_is_null(client, auth_headers):
    move_evals = {"e2e4": MoveEval(cp=None, mate=2)}
    resp = _call(client, auth_headers, move_evals=move_evals, root_eval=None,
                 params={"player_color": "white"})
    data = resp.json()
    assert data["root_eval_cp"] is None and data["root_eval_mate"] is None
    e4 = _by_uci(data["columns"][0], "e2e4")
    assert e4["eval_mate"] == 2 and e4["eval_cp"] is None


# --- color specificity (finding #1) ------------------------------------------

def test_tree_color_specificity_keeps_book_and_eval_but_differs_in_evidence(
    client, auth_headers
):
    # White has an observed in-book edge; Black does not. The book skeleton and
    # the (white-relative) eval values are identical across colors; the observed
    # flag / counts differ.
    move_evals = {"e7e5": MoveEval(cp=15, mate=None)}
    white_edge_key, white_edge = _obs_edge(
        ["e2e4", "e7e5"], traversal_count=5, live_attempts=2
    )
    white = _call(client, auth_headers, overlay=_overlay("white", {white_edge_key: white_edge}),
                  move_evals=move_evals,
                  params={"player_color": "white", "move": ["e2e4"]}).json()
    black = _call(client, auth_headers, overlay=_overlay("black"),
                  move_evals=move_evals,
                  params={"player_color": "black", "move": ["e2e4"]}).json()

    white_e5 = _by_uci(white["columns"][1], "e7e5")
    black_e5 = _by_uci(black["columns"][1], "e7e5")
    # Same book skeleton (node set) + white-relative eval; sort order may differ.
    assert set(_ucis(white["columns"][1])) == set(_ucis(black["columns"][1]))
    assert white_e5["eval_cp"] == black_e5["eval_cp"] == 15
    # Different observed evidence.
    assert white_e5["is_observed"] is True
    assert black_e5["is_observed"] is False
    assert white_e5["encounter_count"] == 5 and black_e5["encounter_count"] == 0


# --- sorting ------------------------------------------------------------------

def test_tree_user_turn_orders_observed_first(client, auth_headers):
    # Parent E4E5 is white-to-move; for player_color=white this is a user turn.
    # An observed off-book move sorts before the unobserved book move.
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=2,
                               live_attempts=3)
    overlay = _overlay("white", edges={edge_key: edge})
    resp = _call(client, auth_headers, overlay=overlay,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    order = _ucis(resp.json()["columns"][2])
    assert order[0] == "d2d4"  # observed, user turn -> first
    assert "g1f3" in order


def test_tree_opponent_turn_orders_by_encounter(client, auth_headers):
    # Parent E4 is black-to-move; for player_color=white this is an opponent turn.
    # The more-encountered reply sorts first regardless of book order.
    e5_key, e5_edge = _obs_edge(["e2e4", "e7e5"], traversal_count=1)
    c5_key, c5_edge = _obs_edge(["e2e4", "c7c5"], traversal_count=9)
    overlay = _overlay("white", edges={e5_key: e5_edge, c5_key: c5_edge})
    resp = _call(client, auth_headers, overlay=overlay,
                 params={"player_color": "white", "move": ["e2e4"]})
    order = _ucis(resp.json()["columns"][1])
    assert order[0] == "c7c5"  # encounter_count 9 > 1


def test_tree_engine_eval_does_not_reorder(client, auth_headers):
    # A large eval on the book move must not pull it ahead of an observed move.
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=1,
                               live_attempts=1)
    overlay = _overlay("white", edges={edge_key: edge})
    move_evals = {"g1f3": MoveEval(cp=900, mate=None), "d2d4": MoveEval(cp=-50, mate=None)}
    resp = _call(client, auth_headers, overlay=overlay, move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    order = _ucis(resp.json()["columns"][2])
    assert order[0] == "d2d4"  # observed still wins despite worse eval


# --- parity with the scorer's structural domain (finding #2) -----------------

def test_structural_children_parity_with_scorer(client, auth_headers):
    from app.opening_rootcalc import RootCalcConfig, _SharedCalculator

    graph = _make_graph()
    roots = _make_roots()
    # Observed off-book edge so the "observed always included" arm is exercised.
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=1)
    overlay = _overlay("white", edges={edge_key: edge})

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    builder, observed_patch = _make_builder(graph, roots, overlay)
    # Patch the phase predicate in BOTH namespaces so API and scorer agree that
    # NC6 is a middlegame position (excluded from the navigable domain).
    with (
        observed_patch,
        patch("app.api.openings.is_middlegame_position", side_effect=lambda f: f == NC6),
        patch("app.opening_rootcalc.is_middlegame_position", side_effect=lambda f: f == NC6),
    ):
        calc = _SharedCalculator("white", graph, overlay, roots, RootCalcConfig(), now)
        for position in [START, E4, E4E5, NF3]:
            api_set = {ce.child_fen for ce in builder._structural_children(position).values()}
            scorer_set = set(calc._structural_children(position))
            assert api_set == scorer_set, position
        # NF3's middlegame book child (NC6) is excluded from the navigable set...
        assert NC6 not in {
            ce.child_fen for ce in builder._structural_children(NF3).values()
        }
        # ...but present as a display-only column node.
        assert NC6 in {ce.child_fen for ce in builder._column_children(NF3).values()}


# --- end-to-end: real position-row + eval lookups against a seeded DB ---------

def test_tree_end_to_end_real_lookups(client, auth_headers, db_session):
    """Exercise the real ensure_tree_cache + lookup_observed_edges_for_parent +
    lookup_position_scores_for_batch + analysis_cache eval lookups against a seeded
    batch (only the graph/roots and the scheduler are stubbed). The request path must
    NOT rebuild overlay_evidence."""
    graph = _make_graph()
    roots = _make_roots()
    start_full = chess.Board().fen()

    # Warm-fresh batch: its registry fingerprint matches the current graph/roots so
    # ensure_tree_cache serves it without a blocking bootstrap.
    batch = OpeningScoreBatch(
        user_id=123, player_color="white", generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        computed_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
    )
    db_session.add(batch)
    db_session.flush()
    db_session.add(OpeningPositionScore(
        batch_id=batch.id, user_id=123, player_color="white",
        normalized_fen=E4E5, in_book=True, has_evidence=True,
        opening_score=64.0, confidence=0.5, coverage=0.4, weighted_depth=2.0,
        sample_size=7, game_count=2,
        computed_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
    ))
    # A persisted observed edge for the in-book 1...e5 reply, read by the real
    # lookup_observed_edges_for_parent (no overlay rebuild).
    db_session.add(OpeningPositionEdge(
        batch_id=batch.id, user_id=123, player_color="white",
        parent_fen=E4, child_fen=E4E5, uci="e7e5",
        traversal_count=6, live_attempts=2, live_passes=1, live_fails=1,
        computed_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
    ))
    # Eval rows: a played eval for 1.e4 and a best eval at the start position.
    db_session.add(AnalysisCache(
        fen_before=start_full, normalized_fen_before=normalize_fen(start_full),
        move_uci="e2e4", move_san="e4", played_eval=33, best_eval=28,
        best_move_uci="e2e4", source="precomputed",
    ))
    db_session.commit()

    with (
        patch("app.api.openings.get_opening_graph", return_value=graph),
        patch("app.api.openings.get_opening_roots", return_value=roots),
        patch("app.api.openings.overlay_evidence") as overlay_spy,
        patch("app.opening_score_scheduler.request_recompute"),
        patch("app.opening_score_scheduler.refresh_now", return_value=False),
    ):
        resp = client.get(TREE_URL,
                          params={"player_color": "white", "move": ["e2e4"]},
                          headers=auth_headers())

    assert resp.status_code == 200
    # The hot read path never rebuilds the evidence overlay.
    overlay_spy.assert_not_called()
    data = resp.json()
    assert "2026-06-12" in data["batch_computed_at"]
    # Root eval comes from the start position's best_eval.
    assert data["root_eval_cp"] == 28
    # The 1.e4 node carries its played eval.
    assert _by_uci(data["columns"][0], "e2e4")["eval_cp"] == 33
    # The e7e5 node hydrates the persisted E4E5 metric row AND the observed edge.
    e5 = _by_uci(data["columns"][1], "e7e5")
    assert e5["opening_score"] == pytest.approx(64.0)
    assert e5["sample_size"] == 7 and e5["game_count"] == 2
    assert e5["is_observed"] is True
    assert e5["encounter_count"] == 6
    assert e5["user_choice_count"] == 2


# --- warm read: no overlay rebuild, bounded per-parent edge lookups -----------

def test_tree_warm_read_no_overlay_rebuild_and_bounded_edge_queries(client, auth_headers):
    """A warm read serves observed edges from the cache via bounded per-parent point
    queries and never rebuilds overlay_evidence on the request thread."""
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=2,
                               live_attempts=1)
    overlay = _overlay("white", edges={edge_key: edge})
    by_parent = _observed_by_parent(overlay)
    loep = MagicMock(side_effect=lambda db, b_id, parent_fen: list(by_parent.get(parent_fen, [])))

    with (
        patch("app.api.openings.get_opening_graph", return_value=_make_graph()),
        patch("app.api.openings.get_opening_roots", return_value=_make_roots()),
        patch("app.api.openings.ensure_tree_cache", return_value=(1, None, "warm_fresh")),
        patch("app.api.openings.lookup_observed_edges_for_parent", loep),
        patch("app.api.openings.lookup_position_scores_for_batch", return_value={}),
        patch("app.api.openings.lookup_move_evals",
              side_effect=lambda db, reqs: {r: None for r in reqs}),
        patch("app.api.openings.lookup_root_eval", return_value=None),
        patch("app.api.openings.overlay_evidence") as overlay_spy,
    ):
        resp = client.get(TREE_URL,
                          params={"player_color": "white", "move": ["e2e4", "e7e5"]},
                          headers=auth_headers())

    assert resp.status_code == 200
    overlay_spy.assert_not_called()
    # The observed off-book move is served from the cache.
    assert "d2d4" in _ucis(resp.json()["columns"][2])
    # Bounded by visited parents (line positions ∪ rendered frontier), not the total
    # observed-edge count — a handful of memoized per-parent point queries.
    assert 0 < loep.call_count <= 20
    # Each lookup is a per-parent point query: (db, batch_id, parent_fen).
    for call in loep.call_args_list:
        assert len(call.args) == 3


def test_tree_builder_holds_scalars_not_orm_batch(client, auth_headers):
    """Finding #2: the builder must hold only the (batch_id, batch_computed_at)
    scalars the route captured before db.rollback() — never an ORM batch/overlay that
    could fire a surprise refresh SELECT after the rollback."""
    batch = _batch(datetime(2026, 6, 9, tzinfo=timezone.utc))
    resp = _call(client, auth_headers, batch=batch,
                 params={"player_color": "white", "move": ["e2e4"]})
    # The pre-rollback scalar flows straight through to the response.
    assert "2026-06-09" in resp.json()["batch_computed_at"]

    builder, _ = _make_builder(_make_graph(), _make_roots(), _overlay("white"),
                               batch_id=batch.id, batch_computed_at=batch.computed_at)
    assert builder.batch_id == batch.id
    assert builder.batch_computed_at == batch.computed_at
    # No ORM batch / overlay retained on the builder.
    assert not hasattr(builder, "overlay")
    assert not hasattr(builder, "batch")
