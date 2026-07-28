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

from app.analysis_profiles import CANONICAL_PROFILE_ID, IDENTITY_FIELDS, get_profile
from app.fen import normalize_fen
from app.models import (
    AnalysisCache,
    OpeningPositionEdge,
    OpeningPositionScore,
    OpeningScoreBatch,
)
from app.opening_aggregate import CachedPositionScoreRow
from app.opening_cache import opening_score_inputs_fingerprint
from app.opening_densify import DensifiedEdges, RoutingView
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
# 1.e4 e5 2.Bc4 (Bishop's Opening) — a second white book reply at E4E5, used to
# build a white-to-move branch column in the eval-sort tests.
BISHOP = "rnbqkbnr/pppp1ppp/8/4p3/2B1P3/8/PPPP1PPP/RNBQK1NR b KQkq -"

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


def _white_branch_graph() -> OpeningGraph:
    """1.e4 e5 with two white book replies — 2.Nf3 and 2.Bc4 — so the column at
    E4E5 is a *white-to-move* branch with a play-frequency tie the eval sort can
    break. The shared _make_graph branches only at black-to-move nodes (E4, NF3),
    so it cannot exercise the white-column sort direction."""
    nodes = {fen: _node(fen) for fen in (START, E4, E4E5, NF3, BISHOP)}
    edges = [
        (START, "e2e4", E4),
        (E4, "e7e5", E4E5),
        (E4E5, "g1f3", NF3),
        (E4E5, "f1c4", BISHOP),
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
    patched ``lookup_observed_edges_for_parents`` can serve the requested-parent
    subset (the exact shape that function returns).
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
          mid_fens=None, user_id=123, routing=None, routing_error=None):
    """Issue a GET /tree with all DB-touching collaborators patched.

    The persisted tree read models are simulated in memory: ``ensure_tree_cache``
    resolves a ``(batch_id, computed_at, cache_state)`` triple, observed edges are
    served from the ``overlay`` fixture via the patched bounded-prefetch lookup
    (``lookup_observed_edges_for_parents``, which the builder calls once per wave with
    the requested parent set), and position rows from ``position_rows`` via the patched
    batch lookup. ``batch_id`` is non-None whenever the fixture carries any cache
    content (a batch, observed edges, or position rows); the cold/no-evidence book-only
    path is modelled by leaving all three empty so ``batch_id`` resolves to None.

    ``routing`` injects a :class:`RoutingView` (transposition overlay); omitted, the
    route sees the base graph with no overlay — today's behaviour. ``routing_error``
    makes the patched ``routing_view`` raise, modelling a broken artifact load.
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

    def _loep(db, b_id, parent_fens):
        wanted = set(parent_fens)
        return {
            parent: list(edges)
            for parent, edges in observed_by_parent.items()
            if parent in wanted
        }

    def _lpsfb(db, b_id, fens):
        return {fen: row for fen, row in position_rows.items() if fen in fens}

    def _lme(db, requests):
        # move_evals may be keyed by uci (tests use unique ucis per column).
        return {req: move_evals.get(req[1]) for req in requests}

    def _lre(db, fen):
        return root_eval

    def _routing_view(g):
        if routing_error is not None:
            raise routing_error
        return routing if routing is not None else RoutingView(g)

    cms = [
        patch("app.api.openings.routing_view", side_effect=_routing_view),
        patch("app.api.openings.get_opening_graph", return_value=graph),
        patch("app.api.openings.get_opening_roots", return_value=roots),
        patch("app.api.openings.ensure_tree_cache", side_effect=_ensure),
        patch("app.api.openings.lookup_observed_edges_for_parents", side_effect=_loep),
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

    The builder no longer eager-loads in ``__init__``; the bounded 2-wave prefetch runs
    inside ``build()``. Tests that call structural methods directly (without ``build()``)
    therefore resolve observed edges lazily through ``_observed_children``'s per-parent
    fallback, which this serves from the in-memory overlay (not the real
    ``opening_position_edges`` table) via a patched ``lookup_observed_edges_for_parent``.
    Returns ``(builder, patch_cm)``; ``patch_cm`` must wrap any direct structural-method
    calls so the per-parent fallback hits the overlay instead of a ``None`` db.
    """
    from app.api.openings import _OpeningTreeBuilder

    by_parent = _observed_by_parent(overlay)

    def _loep(db, b_id, parent_fen):
        return list(by_parent.get(parent_fen, []))

    builder = _OpeningTreeBuilder(
        None, graph, roots, batch_id, batch_computed_at, player_color, user_id
    )
    patch_cm = patch(
        "app.api.openings.lookup_observed_edges_for_parent", side_effect=_loep
    )
    return builder, patch_cm


def _run_build(graph, roots, overlay, moves, *, player_color="white", batch_id=1,
               extra_batch_edges=None, routing=None, mid_fens=None):
    """Run ``builder.build()`` with the bounded prefetch served from ``overlay``.

    Models the persisted batch as the ``overlay`` edges plus optional
    ``extra_batch_edges`` (edges under parents the build should never visit). The
    patched ``lookup_observed_edges_for_parents`` returns ONLY the requested parents
    and records each requested set, so an under-collecting prefetch surfaces as a
    non-zero ``_observed_straggler_count`` (the singular fallback is left UNpatched, so
    a straggler would hit the ``None`` db and fail loudly) and an over-fetch surfaces
    as an unrelated parent appearing in ``requested_parents``. Returns
    ``(builder, response, requested_parents)``.
    """
    from app.api.openings import _OpeningTreeBuilder

    store = _observed_by_parent(overlay)
    for (parent, _child), edge in (extra_batch_edges or {}).items():
        store.setdefault(parent, []).append(edge)
    requested_parents: list[set] = []

    def _loep(db, b_id, parent_fens):
        wanted = set(parent_fens)
        requested_parents.append(wanted)
        return {p: list(es) for p, es in store.items() if p in wanted}

    builder = _OpeningTreeBuilder(
        None, graph, roots, batch_id, None, player_color, 123, routing=routing
    )
    with contextlib.ExitStack() as stack:
        for cm in (
            patch("app.api.openings.lookup_observed_edges_for_parents", side_effect=_loep),
            patch("app.api.openings.lookup_position_scores_for_batch", return_value={}),
            patch("app.api.openings.lookup_move_evals",
                  side_effect=lambda db, reqs: {r: None for r in reqs}),
            patch("app.api.openings.lookup_root_eval", return_value=None),
        ):
            stack.enter_context(cm)
        if mid_fens is not None:
            stack.enter_context(patch("app.api.openings.is_middlegame_position",
                                      side_effect=lambda fen: fen in mid_fens))
        response = builder.build(moves, None)
    return builder, response, requested_parents


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
    assert data["model_version"] == "sm-v2-4"
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
    assert "observed_prefetch_ms=" in timing
    assert "observed_edge_queries=" in timing
    assert "observed_stragglers=" in timing
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

def test_tree_off_tree_move_becomes_user_selected_node(client, auth_headers):
    # 1.e4 then a legal-but-unknown black reply (1...d5) is now KEPT as a
    # user-selected (third type) move instead of being truncated (g-obh5).
    resp = _call(client, auth_headers,
                 params={"player_color": "white", "move": ["e2e4", "d7d5"]})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "d7d5"]
    assert data["selected_fen"] == normalize_fen(_full_fen(["e2e4", "d7d5"]))
    assert data["selected_ply"] == 2
    d5 = _by_uci(data["columns"][1], "d7d5")
    assert d5["is_user_selected"] is True
    assert d5["is_navigable"] is True
    assert d5["in_book"] is False
    assert d5["is_observed"] is False
    # A brand-new position has no score row and (here) no eval — null metrics.
    assert d5["opening_score"] is None
    assert d5["eval_cp"] is None
    assert d5["eval_mate"] is None


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


def test_tree_selected_boundary_move_becomes_navigable(client, auth_headers):
    # Selecting the middlegame boundary move b8c6 now keeps it in the line and
    # forces it navigable for THIS line — a crossed boundary is the third move
    # type (g-obh5), distinct from the same move as an unselected sibling (which
    # stays non-navigable, see test_tree_middlegame_book_child_is_terminal_boundary).
    resp = _call(client, auth_headers, mid_fens={NC6},
                 params={"player_color": "white",
                         "move": ["e2e4", "e7e5", "g1f3", "b8c6"]},
                 # Selecting a boundary must not restore its cached row: boundary
                 # membership is a property of the position, not of the selection.
                 position_rows={
                     NC6: _pos_row(NC6, score=88.0, confidence=0.9, coverage=0.7,
                                   sample_size=20, game_count=9, has_evidence=True),
                 })
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert data["selected_fen"] == NC6
    boundary = _by_uci(data["columns"][3], "b8c6")
    assert boundary["is_navigable"] is True
    assert boundary["is_user_selected"] is True
    # It IS a book move (just a middlegame boundary), so in_book stays true.
    assert boundary["in_book"] is True
    assert boundary["opening_score"] is None
    assert boundary["confidence"] is None
    assert boundary["coverage"] is None
    assert boundary["game_count"] is None


def test_tree_chain_of_off_tree_moves_all_render(client, auth_headers):
    # Two consecutive off-tree moves (1.e4 d5 2.exd5): the SECOND must still
    # render a node even though its parent (the first off-tree position) has no
    # column children — the build loop injects the selected move (g-obh5).
    resp = _call(client, auth_headers,
                 params={"player_color": "white",
                         "move": ["e2e4", "d7d5", "e4d5"]})
    data = resp.json()
    assert data["canonical_line"] == ["e2e4", "d7d5", "e4d5"]
    d5 = _by_uci(data["columns"][1], "d7d5")
    assert d5["is_user_selected"] is True
    exd5 = _by_uci(data["columns"][2], "e4d5")
    assert exd5["is_user_selected"] is True
    assert exd5["is_navigable"] is True


def test_tree_off_tree_transposition_reexpands_book(client, auth_headers):
    # 1.Nf3 e5 2.e4 transposes into 1.e4 e5 2.Nf3 (NF3). The off-tree line is
    # kept, and reaching a known book FEN re-expands that position's book
    # children (g-obh5).
    resp = _call(client, auth_headers,
                 params={"player_color": "white",
                         "move": ["g1f3", "e7e5", "e2e4"]})
    data = resp.json()
    assert data["canonical_line"] == ["g1f3", "e7e5", "e2e4"]
    assert data["selected_fen"] == NF3
    g1f3 = _by_uci(data["columns"][0], "g1f3")
    assert g1f3["is_user_selected"] is True
    # The START reveal column still offers the book move e2e4 alongside it.
    assert _by_uci(data["columns"][0], "e2e4")["is_user_selected"] is False
    # The transposed-into NF3 position re-expands its book children.
    reveal = data["columns"][3]
    assert set(_ucis(reveal)) == {"b8c6", "g8f6"}
    assert _by_uci(reveal, "b8c6")["is_navigable"] is True
    assert _by_uci(reveal, "g8f6")["is_navigable"] is True


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


def test_root_position_score_fields_populated_when_batch_has_start_row(client, auth_headers):
    """Root metrics come from the starting position's CachedPositionScoreRow."""
    start_row = _pos_row(START, score=72.5, confidence=0.8, coverage=0.6,
                         game_count=15, has_evidence=True)
    resp = _call(client, auth_headers, position_rows={START: start_row},
                 params={"player_color": "white"})
    data = resp.json()
    assert data["root_opening_score"] == pytest.approx(72.5)
    assert data["root_coverage"] == pytest.approx(0.6)
    assert data["root_confidence"] == pytest.approx(0.8)
    assert data["root_game_count"] == 15


def test_root_position_score_fields_null_when_no_batch_row(client, auth_headers):
    """Root metrics are null when the batch has no row for the starting position."""
    resp = _call(client, auth_headers, params={"player_color": "white"})
    data = resp.json()
    assert data["root_opening_score"] is None
    assert data["root_coverage"] is None
    assert data["root_confidence"] is None
    assert data["root_game_count"] is None


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


def test_tree_eval_does_not_override_play_frequency(client, auth_headers):
    # Eval is only the SECONDARY key: a large eval on the book move must not pull
    # it ahead of an observed move (play frequency stays primary).
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=1,
                               live_attempts=1)
    overlay = _overlay("white", edges={edge_key: edge})
    move_evals = {"g1f3": MoveEval(cp=900, mate=None), "d2d4": MoveEval(cp=-50, mate=None)}
    resp = _call(client, auth_headers, overlay=overlay, move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    order = _ucis(resp.json()["columns"][2])
    assert order[0] == "d2d4"  # observed still wins despite worse eval


def test_tree_eval_breaks_ties_white_to_move_column(client, auth_headers):
    # White-to-move column (the E4E5 replies): two equally-unplayed book moves
    # tie on play frequency, so the eval sort breaks the tie toward the side to
    # move — White — and the higher (white-relative) eval sorts first.
    move_evals = {"g1f3": MoveEval(cp=50, mate=None),
                  "f1c4": MoveEval(cp=-30, mate=None)}
    resp = _call(client, auth_headers, graph=_white_branch_graph(),
                 move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    order = _ucis(resp.json()["columns"][2])
    assert order == ["g1f3", "f1c4"]  # +50 (best for White) on top


def test_tree_eval_breaks_ties_black_to_move_column(client, auth_headers):
    # Black-to-move column (the E4 replies): the eval sort breaks the
    # play-frequency tie toward the side to move — Black — so the lower
    # (most-negative, best-for-Black) white-relative eval sorts first.
    move_evals = {"e7e5": MoveEval(cp=50, mate=None),
                  "c7c5": MoveEval(cp=-30, mate=None)}
    resp = _call(client, auth_headers, move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4"]})
    order = _ucis(resp.json()["columns"][1])
    assert order == ["c7c5", "e7e5"]  # -30 (best for Black) on top


def test_tree_eval_tie_break_keys_on_column_side_not_repertoire_color(client, auth_headers):
    # The eval tie-break keys on the COLUMN's side to move, not the repertoire
    # color: the same black-to-move column sorts identically for a white and a
    # black repertoire (best-for-Black first either way).
    move_evals = {"e7e5": MoveEval(cp=50, mate=None),
                  "c7c5": MoveEval(cp=-30, mate=None)}
    white = _call(client, auth_headers, move_evals=move_evals,
                  params={"player_color": "white", "move": ["e2e4"]})
    black = _call(client, auth_headers, move_evals=move_evals,
                  params={"player_color": "black", "move": ["e2e4"]})
    assert _ucis(white.json()["columns"][1]) == ["c7c5", "e7e5"]
    assert _ucis(black.json()["columns"][1]) == ["c7c5", "e7e5"]


def test_tree_eval_mate_dominates_centipawns_white_to_move(client, auth_headers):
    # White-to-move column: a forced mate for White outranks a large positive cp
    # when play frequency ties.
    move_evals = {"g1f3": MoveEval(cp=None, mate=3),    # White mates -> best
                  "f1c4": MoveEval(cp=800, mate=None)}  # big cp, still below mate
    resp = _call(client, auth_headers, graph=_white_branch_graph(),
                 move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4", "e7e5"]})
    order = _ucis(resp.json()["columns"][2])
    assert order == ["g1f3", "f1c4"]  # mate-in-3 for White on top


def test_tree_eval_mate_dominates_centipawns_black_to_move(client, auth_headers):
    # Black-to-move column: a forced mate for Black (white-relative mate -3)
    # outranks a large negative (good-for-Black) cp when play frequency ties.
    move_evals = {"e7e5": MoveEval(cp=None, mate=-3),    # Black mates -> best
                  "c7c5": MoveEval(cp=-800, mate=None)}  # big cp, still below mate
    resp = _call(client, auth_headers, move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4"]})
    order = _ucis(resp.json()["columns"][1])
    assert order == ["e7e5", "c7c5"]  # mate-in-3 for Black on top


def test_tree_eval_unknown_sorts_after_scored_in_tie_break(client, auth_headers):
    # When play frequency ties, a move with no eval sorts after one that has an
    # eval, even when the scored move is unfavorable for the side to move (here
    # the black-to-move column: +500 is bad for Black, but still beats unknown).
    move_evals = {"e7e5": MoveEval(cp=500, mate=None)}  # c7c5 has no eval
    resp = _call(client, auth_headers, move_evals=move_evals,
                 params={"player_color": "white", "move": ["e2e4"]})
    order = _ucis(resp.json()["columns"][1])
    assert order == ["e7e5", "c7c5"]  # scored (even if bad) before unknown


def test_tree_eval_mate_zero_winner_is_unknown(client, auth_headers):
    # A mate-0 (checkmate-on-board) carries no winner in the white-relative count
    # alone, so it must NOT be read as a favorable mate for either side: when
    # frequency ties it sorts LAST (as unknown), behind the scored reply. The
    # white-relative count is color-independent, so both repertoires agree.
    move_evals = {"e7e5": MoveEval(cp=None, mate=0),
                  "c7c5": MoveEval(cp=20, mate=None)}
    white = _call(client, auth_headers, move_evals=move_evals,
                  params={"player_color": "white", "move": ["e2e4"]})
    black = _call(client, auth_headers, move_evals=move_evals,
                  params={"player_color": "black", "move": ["e2e4"]})
    # mate-0 (e7e5) treated as unknown -> last, behind the scored c7c5.
    assert _ucis(white.json()["columns"][1]) == ["c7c5", "e7e5"]
    assert _ucis(black.json()["columns"][1]) == ["c7c5", "e7e5"]


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
    """Exercise the real ensure_tree_cache + lookup_observed_edges_for_parents +
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
    # bounded prefetch lookup_observed_edges_for_parents (no overlay rebuild).
    db_session.add(OpeningPositionEdge(
        batch_id=batch.id, user_id=123, player_color="white",
        parent_fen=E4, child_fen=E4E5, uci="e7e5",
        traversal_count=6, live_attempts=2, live_passes=1, live_fails=1,
        computed_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
    ))
    # Eval rows: a played eval for 1.e4 and a best eval at the start position. The
    # Phase-4 lookups apply the move/position trust gate, so this row carries full
    # canonical identity + the legacy resolver-complete-v2 contract (otherwise an
    # unidentified row would be rejected and both evals would read null).
    canon = get_profile(CANONICAL_PROFILE_ID)
    identity = {f: getattr(canon, f) for f in IDENTITY_FIELDS}
    db_session.add(AnalysisCache(
        fen_before=start_full, normalized_fen_before=normalize_fen(start_full),
        move_uci="e2e4", move_san="e4", played_eval=33, best_eval=28,
        best_move_uci="e2e4", best_line_uci="e2e4 e7e5", classification="best",
        source="precomputed", analysis_profile_id=CANONICAL_PROFILE_ID,
        evidence_contract_id="resolver-complete-v2", **identity,
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


# --- warm read: no overlay rebuild, bounded 2-wave edge prefetch --------------

def test_tree_warm_read_no_overlay_rebuild_and_bounded_edge_queries(client, auth_headers):
    """A warm read serves observed edges from the cache via a BOUNDED 2-wave prefetch
    (g-0qe6 Option B: one ``parent_fen IN (...)`` query per wave, independent of node
    count, fetching only the visible parents) and never rebuilds overlay_evidence on
    the request thread."""
    edge_key, edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=2,
                               live_attempts=1)
    overlay = _overlay("white", edges={edge_key: edge})
    by_parent = _observed_by_parent(overlay)

    def _loep(db, b_id, parent_fens):
        wanted = set(parent_fens)
        return {p: list(es) for p, es in by_parent.items() if p in wanted}

    loep = MagicMock(side_effect=_loep)

    with (
        patch("app.api.openings.get_opening_graph", return_value=_make_graph()),
        patch("app.api.openings.get_opening_roots", return_value=_make_roots()),
        patch("app.api.openings.ensure_tree_cache", return_value=(1, None, "warm_fresh")),
        patch("app.api.openings.lookup_observed_edges_for_parents", loep),
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
    # The observed off-book move is served from the prefetch.
    assert "d2d4" in _ucis(resp.json()["columns"][2])
    # Exactly TWO observed-edge queries per request (one per wave), regardless of
    # node/column count.
    assert loep.call_count == 2
    # Each is a bounded prefetch: (db, batch_id, parent_fens) — never a whole-batch read.
    for call in loep.call_args_list:
        assert len(call.args) == 3
        assert call.args[1] == 1  # batch_id


def test_tree_cold_no_evidence_issues_zero_edge_queries(client, auth_headers):
    """A cold / no-evidence user (batch_id is None) yields a book-only tree and never
    touches the observed-edge read model (g-a6k2/g-0qe6 acceptance)."""
    loep = MagicMock(side_effect=lambda db, b_id, parent_fens: {})

    with (
        patch("app.api.openings.get_opening_graph", return_value=_make_graph()),
        patch("app.api.openings.get_opening_roots", return_value=_make_roots()),
        patch("app.api.openings.ensure_tree_cache",
              return_value=(None, None, "book_only")),
        patch("app.api.openings.lookup_observed_edges_for_parents", loep),
        patch("app.api.openings.lookup_position_scores_for_batch", return_value={}),
        patch("app.api.openings.lookup_move_evals",
              side_effect=lambda db, reqs: {r: None for r in reqs}),
        patch("app.api.openings.lookup_root_eval", return_value=None),
    ):
        resp = client.get(TREE_URL,
                          params={"player_color": "white", "move": ["e2e4"]},
                          headers=auth_headers())

    assert resp.status_code == 200
    # Book-only tree: the E4 column still lists the book replies.
    assert set(_ucis(resp.json()["columns"][1])) == {"e7e5", "c7c5"}
    # No batch ⇒ zero observed-edge queries.
    assert loep.call_count == 0


def test_tree_prefetch_covers_all_visited_parents_zero_stragglers():
    """The 2-wave prefetch loads every parent the build's structural pass reads —
    line positions (wave 1) plus their column-children frontier (wave 2), including the
    off-book OBSERVED frontier and the terminal-probe children of frontier nodes — so
    the defensive per-parent fallback never fires (g-0qe6 acceptance)."""
    graph = _make_graph()
    roots = _make_roots()

    # Off-book observed frontier: at NF3 (a line position) a black off-book reply d7d6
    # to X (a navigable observed frontier node, NOT on the line), and X carries its own
    # observed child so the wave-2 terminal probe reads X's edges to find it non-empty.
    nf3_off, nf3_off_edge = _obs_edge(["e2e4", "e7e5", "g1f3", "d7d6"],
                                      traversal_count=1)
    x_child, x_child_edge = _obs_edge(["e2e4", "e7e5", "g1f3", "d7d6", "d2d4"],
                                      traversal_count=1)
    overlay = _overlay("white", edges={nf3_off: nf3_off_edge, x_child: x_child_edge})

    # Build A: a deep book line to NC6 — wave 2's frontier off NF3 includes the
    # on-line child (NC6), the book sibling (PETROV), and the off-book observed X.
    builder, _, _ = _run_build(graph, roots, overlay,
                               ["e2e4", "e7e5", "g1f3", "b8c6"])
    assert builder._observed_straggler_count == 0

    # Build B: an INJECTED off-tree selected move (c2c3 at E4E5 is legal but neither a
    # book nor an observed child) — its child is the next line position, already loaded
    # by wave 1, so it too needs no straggler fallback.
    builder_b, _, _ = _run_build(graph, roots, _overlay("white"),
                                 ["e2e4", "e7e5", "c2c3"])
    assert builder_b._observed_straggler_count == 0


def test_tree_prefetch_fetches_only_visible_parent_rows():
    """Option B's whole point: the prefetch loads edges for ONLY the visible parents.
    An unrelated edge elsewhere in the batch (under a parent the build never visits) is
    never requested or counted — unlike the g-a6k2 whole-batch read that pulled it
    all."""
    graph = _make_graph()
    roots = _make_roots()
    # A visible observed edge on the e5 line (off E4E5) ...
    visible, visible_edge = _obs_edge(["e2e4", "e7e5", "d2d4"], traversal_count=3)
    # ... and an unrelated edge deep in the Sicilian branch the e5 line never reaches.
    unrelated, unrelated_edge = _obs_edge(
        ["e2e4", "c7c5", "g1f3", "d7d6", "d2d4"], traversal_count=9)
    overlay = _overlay("white", edges={visible: visible_edge})

    builder, _, requested_parents = _run_build(
        graph, roots, overlay, ["e2e4", "e7e5"],
        extra_batch_edges={unrelated: unrelated_edge},
    )

    assert builder._observed_straggler_count == 0
    # Exactly two waves were issued (line, then frontier).
    assert builder._observed_edge_query_count == 2
    # The unrelated Sicilian parent is never requested and never loaded ...
    all_requested = set().union(*requested_parents)
    assert unrelated[0] not in all_requested
    # ... so only the single visible-line edge row is counted (not the whole batch).
    assert builder._observed_edge_row_count == 1


def test_load_observed_counts_chunked_selects_not_waves():
    """observed_edge_query_count is an honest DB round-trip count: a single wave whose
    parent set exceeds the IN-chunk cap issues multiple chunked SELECTs and bumps the
    counter once per chunk, never once per wave (g-0qe6 review fix)."""
    from app.api.openings import _OpeningTreeBuilder
    from app.opening_cache import OBSERVED_EDGE_PARENT_CHUNK_SIZE as CAP

    builder = _OpeningTreeBuilder(None, _make_graph(), _make_roots(), 1, None,
                                  "white", 123)
    # A single _load_observed call with > CAP distinct (synthetic) parents.
    fens = {f"chunk-fen-{i} w - -" for i in range(CAP + 5)}
    with patch("app.api.openings.lookup_observed_edges_for_parents",
               return_value={}) as loep:
        builder._load_observed(fens)
    # The helper itself is called once with the whole subset (it does the chunking)...
    assert loep.call_count == 1
    # ...but the round-trip counter reflects the 2 chunks that subset splits into.
    assert builder._observed_edge_query_count == 2


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


# --- GET /tree/status: non-blocking cold-cache probe (g-k4z2) -----------------
#
# The status route reports warm | building | cold from a CHEAP check
# (get_latest_opening_score_batch + registry-fingerprint compare, and a limit=1
# evidence check only when there is no batch). It must never build the overlay and
# never call the blocking refresh_now; on a cold/registry-stale (user, color) with
# evidence it fires only the BACKGROUND request_recompute and reports progress.

STATUS_URL = "/api/openings/tree/status"


@contextlib.contextmanager
def _status_patches(
    *, batch, has_evidence=True, scheduled=False, graph=None, roots=None
):
    """Patch the cheap collaborators behind resolve_tree_cache_state.

    ``overlay_evidence`` and ``refresh_now`` are spied so each test can assert the
    probe never builds the overlay and never blocks on the bootstrap. The scheduler
    facades are patched on their source module (resolve_tree_cache_state imports
    them lazily at call time)."""
    overlay_spy = MagicMock()
    request_recompute = MagicMock()
    refresh_now = MagicMock()
    with (
        patch("app.api.openings.get_opening_graph",
              return_value=graph or _make_graph()),
        patch("app.api.openings.get_opening_roots",
              return_value=roots or _make_roots()),
        patch("app.opening_cache.get_latest_opening_score_batch",
              return_value=batch),
        patch("app.opening_cache.has_opening_evidence", return_value=has_evidence),
        patch("app.opening_cache.overlay_evidence", overlay_spy),
        patch("app.opening_score_scheduler.is_recompute_scheduled",
              return_value=scheduled),
        patch("app.opening_score_scheduler.request_recompute", request_recompute),
        patch("app.opening_score_scheduler.refresh_now", refresh_now),
    ):
        yield overlay_spy, request_recompute, refresh_now


def _current_batch():
    """A batch whose registry fingerprint matches the synthetic graph/roots."""
    batch = _batch(datetime(2026, 6, 12, tzinfo=timezone.utc))
    batch.registry_fingerprint = opening_score_inputs_fingerprint(
        _make_graph(), _make_roots()
    )
    return batch


def test_tree_status_no_auth_returns_401(client):
    resp = client.get(STATUS_URL, params={"player_color": "white"})
    assert resp.status_code == 401


def test_tree_status_invalid_color_returns_422(client, auth_headers):
    resp = client.get(STATUS_URL, params={"player_color": "sideways"},
                      headers=auth_headers())
    assert resp.status_code == 422


def test_tree_status_warm_with_current_registry_batch(client, auth_headers):
    """A current-registry batch ⇒ warm. The probe schedules nothing and builds no
    overlay (the /tree GET owns the background revalidate)."""
    with _status_patches(batch=_current_batch()) as (overlay, recompute, refresh):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json() == {"player_color": "white", "state": "warm"}
    overlay.assert_not_called()
    recompute.assert_not_called()
    refresh.assert_not_called()


def test_tree_status_warm_when_no_batch_and_no_evidence(client, auth_headers):
    """No batch + no opening evidence ⇒ warm: the tree is correctly book-only and
    /tree is fast (a recompute would write no batch, so polling could never flip)."""
    with _status_patches(batch=None, has_evidence=False) as (overlay, recompute,
                                                             refresh):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["state"] == "warm"
    overlay.assert_not_called()
    recompute.assert_not_called()
    refresh.assert_not_called()


def test_tree_status_cold_fires_background_recompute(client, auth_headers):
    """No batch + evidence + nothing scheduled ⇒ cold: fire the BACKGROUND
    request_recompute once, never refresh_now, never build the overlay."""
    with _status_patches(batch=None, has_evidence=True, scheduled=False) as (
        overlay, recompute, refresh
    ):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["state"] == "cold"
    recompute.assert_called_once_with(123, "white")
    refresh.assert_not_called()
    overlay.assert_not_called()


def test_tree_status_building_when_recompute_already_scheduled(client, auth_headers):
    """No batch + evidence + work already pending/in-flight ⇒ building, WITHOUT a
    redundant re-enqueue."""
    with _status_patches(batch=None, has_evidence=True, scheduled=True) as (
        overlay, recompute, refresh
    ):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["state"] == "building"
    recompute.assert_not_called()
    refresh.assert_not_called()
    overlay.assert_not_called()


def test_tree_status_registry_stale_batch_is_building_not_warm(client, auth_headers):
    """A registry/schema-stale batch (predates this read model) is NOT warm even with
    no current evidence: recompute_opening_scores_if_needed always rebuilds it, so the
    probe reports progress (here cold, kicking off the background rebuild) and never
    consults has_opening_evidence (the batch-present branch short-circuits)."""
    stale = _batch(datetime(2026, 6, 1, tzinfo=timezone.utc))
    stale.registry_fingerprint = "stale-registry-fingerprint"
    with _status_patches(batch=stale, has_evidence=False, scheduled=False) as (
        overlay, recompute, refresh
    ):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["state"] == "cold"
    recompute.assert_called_once_with(123, "white")
    refresh.assert_not_called()
    overlay.assert_not_called()


def test_tree_status_end_to_end_warm_against_seeded_batch(
    client, auth_headers, db_session
):
    """End-to-end with the REAL resolve_tree_cache_state + get_latest_opening_score_batch
    against a seeded current-registry batch (only graph/roots + scheduler stubbed):
    warm, and no overlay rebuild on the probe path."""
    graph = _make_graph()
    roots = _make_roots()
    db_session.add(OpeningScoreBatch(
        user_id=123, player_color="white", generation=1,
        registry_fingerprint=opening_score_inputs_fingerprint(graph, roots),
        computed_at=datetime(2026, 6, 12, tzinfo=timezone.utc),
    ))
    db_session.commit()

    with (
        patch("app.api.openings.get_opening_graph", return_value=graph),
        patch("app.api.openings.get_opening_roots", return_value=roots),
        patch("app.opening_cache.overlay_evidence") as overlay_spy,
        patch("app.opening_score_scheduler.request_recompute") as recompute,
        patch("app.opening_score_scheduler.refresh_now") as refresh,
    ):
        resp = client.get(STATUS_URL, params={"player_color": "white"},
                          headers=auth_headers())

    assert resp.status_code == 200
    assert resp.json()["state"] == "warm"
    overlay_spy.assert_not_called()
    recompute.assert_not_called()
    refresh.assert_not_called()


def test_tree_response_carries_cache_state(client, auth_headers):
    """Part A: the /tree response surfaces ensure_tree_cache's cache_state so a DIRECT
    caller can detect a degraded book_only / bootstrap_timeout tree."""
    with (
        patch("app.api.openings.get_opening_graph", return_value=_make_graph()),
        patch("app.api.openings.get_opening_roots", return_value=_make_roots()),
        patch("app.api.openings.ensure_tree_cache",
              return_value=(None, None, "bootstrap_timeout")),
        patch("app.api.openings.lookup_observed_edges_for_parents",
              return_value={}),
        patch("app.api.openings.lookup_position_scores_for_batch", return_value={}),
        patch("app.api.openings.lookup_move_evals",
              side_effect=lambda db, reqs: {r: None for r in reqs}),
        patch("app.api.openings.lookup_root_eval", return_value=None),
    ):
        resp = client.get(TREE_URL, params={"player_color": "white"},
                          headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["cache_state"] == "bootstrap_timeout"


# --- transposition cards (g-openings-transpose) -------------------------------
#
# The motivating route: 1.c4 e6 2.Nc3 Nf6 3.d4 d5 reaches the same normalized
# position as the Queen's Gambit Declined order 1.d4 d5 2.c4 e6 3.Nc3 Nf6. The
# base ECO graph reaches the Hedgehog System only through 1.c4 Nf6 2.Nc3 e6, so
# the two English-order edges below exist ONLY in the routing overlay.

ENGLISH_NC3 = "rnbqkbnr/pppp1ppp/4p3/8/2P5/2N5/PP1PPPPP/R1BQKBNR b KQkq -"
HEDGEHOG = "rnbqkb1r/pppp1ppp/4pn2/8/2P5/2N5/PP1PPPPP/R1BQKBNR w KQkq -"
PRE_D5 = "rnbqkb1r/pppp1ppp/4pn2/8/2PP4/2N5/PP2PPPP/R1BQKBNR b KQkq -"
QGD_TARGET = "rnbqkb1r/ppp2ppp/4pn2/3p4/2PP4/2N5/PP2PPPP/R1BQKBNR w KQkq -"

# The two edges the base graph deliberately lacks. d7d5 (PRE_D5 -> QGD_TARGET) is
# a BASE edge, so the last step of the route is ordinary book navigation.
ENGLISH_OVERLAY_EDGES = (
    (ENGLISH_NC3, "g8f6", HEDGEHOG),
    (HEDGEHOG, "d2d4", PRE_D5),
)


def _english_graph() -> OpeningGraph:
    """Base ECO graph for the English/QGD transposition, WITHOUT the two English-
    order edges the overlay supplies. Every position on the route is present and
    named — reached through the source book's own move orders."""
    lines = [
        ["c2c4", "e7e6", "b1c3"],                            # -> ENGLISH_NC3
        ["c2c4", "g8f6", "b1c3", "e7e6"],                    # -> HEDGEHOG
        ["d2d4", "g8f6", "c2c4", "e7e6", "b1c3", "d7d5"],    # -> PRE_D5 -> QGD_TARGET
        ["d2d4", "d7d5", "c2c4", "e7e6", "b1c3", "g8f6"],    # -> QGD_TARGET (QGD order)
    ]
    names = {
        ENGLISH_NC3: ("English Opening: Agincourt Defense", "A13"),
        HEDGEHOG: ("English Opening: Anglo-Indian Defense, Hedgehog System", "A17"),
        PRE_D5: ("Indian Defense: Anti-Nimzo-Indian", "E10"),
        QGD_TARGET: ("Queen's Gambit Declined: Normal Defense", "D31"),
    }
    nodes: dict[str, OpeningGraphNode] = {}

    def _ensure(fen: str) -> OpeningGraphNode:
        if fen not in nodes:
            name, eco = names.get(fen, (None, None))
            nodes[fen] = _node(fen, name, eco)
        return nodes[fen]

    _ensure(START)
    for line in lines:
        board = chess.Board()
        for uci in line:
            parent = normalize_fen(board.fen())
            board.push(chess.Move.from_uci(uci))
            child = normalize_fen(board.fen())
            _ensure(parent).children[uci] = child
            _ensure(child).parents.add((parent, uci))
    return OpeningGraph(nodes, START)


def _english_routing(graph: OpeningGraph, edges=ENGLISH_OVERLAY_EDGES) -> RoutingView:
    return RoutingView(graph, DensifiedEdges(tuple(sorted(edges))))


def _english_call(client, auth_headers, moves, **kwargs):
    graph = kwargs.pop("graph", None) or _english_graph()
    kwargs.setdefault("routing", _english_routing(graph))
    kwargs.setdefault("mid_fens", frozenset())
    return _call(
        client, auth_headers,
        params=[("player_color", "white"), *(("move", m) for m in moves)],
        graph=graph, roots=OpeningRoots({}, {}), **kwargs,
    )


def test_english_transposition_is_navigable_from_the_english_move_order(
    client, auth_headers
):
    """1. ...Nf6 from 1.c4 e6 2.Nc3 is offered as a navigable transposition card,
    even though the base graph reaches the Hedgehog only via 1.c4 Nf6 2.Nc3 e6."""
    resp = _english_call(client, auth_headers, ["c2c4", "e7e6", "b1c3"])
    assert resp.status_code == 200
    column = next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3)
    node = _by_uci(column, "g8f6")
    assert node["is_transposition"] is True
    assert node["is_navigable"] is True
    assert node["is_user_selected"] is False
    assert node["in_book"] is False       # not this parent's book edge
    assert node["child_fen"] == HEDGEHOG


def test_english_transposition_card_carries_the_destinations_own_name(
    client, auth_headers
):
    """4. A transposition card is named by the destination graph node itself, so
    it reads as the real opening rather than inheriting an ancestor's name."""
    resp = _english_call(client, auth_headers, ["c2c4", "e7e6", "b1c3"])
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["opening_name"] == (
        "English Opening: Anglo-Indian Defense, Hedgehog System"
    )
    assert node["eco"] == "A17"


def test_english_route_reaches_qgd_normal_defense_through_cards(client, auth_headers):
    """2 + 3. The full route is card-navigable: d4 from the Hedgehog is a second
    transposition, and the base d7d5 edge lands on the same normalized position
    the QGD move order reaches."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6", "d2d4", "d7d5"]
    )
    assert resp.status_code == 200
    body = resp.json()
    columns = {c["position_fen"]: c for c in body["columns"]}

    d4_node = _by_uci(columns[HEDGEHOG], "d2d4")
    assert d4_node["is_transposition"] is True
    assert d4_node["is_navigable"] is True

    d5_node = _by_uci(columns[PRE_D5], "d7d5")
    assert d5_node["is_transposition"] is False   # ordinary base-book edge
    assert d5_node["in_book"] is True
    assert d5_node["is_navigable"] is True
    assert d5_node["child_fen"] == QGD_TARGET
    assert d5_node["opening_name"] == "Queen's Gambit Declined: Normal Defense"

    # Same destination as the canonical QGD order 1.d4 d5 2.c4 e6 3.Nc3 Nf6.
    assert body["selected_fen"] == QGD_TARGET


def test_transposition_merges_with_an_observed_edge_keeping_its_evidence(
    client, auth_headers
):
    """5. An edge that is both observed and in the overlay is ONE card that keeps
    its evidence counts while still being identified as a transposition."""
    key, edge = _obs_edge(
        ["c2c4", "e7e6", "b1c3", "g8f6"],
        traversal_count=7, live_attempts=3, live_passes=2,
    )
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"], overlay=_overlay("white", {key: edge})
    )
    column = next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3)
    assert _ucis(column).count("g8f6") == 1
    node = _by_uci(column, "g8f6")
    assert node["is_transposition"] is True
    assert node["is_observed"] is True
    assert node["encounter_count"] == 7
    assert node["user_choice_count"] == 3
    assert node["is_prepared"] is True


def test_missing_overlay_falls_back_to_the_base_tree(client, auth_headers):
    """6. With no usable overlay the tree is exactly today's base/observed tree:
    the English-order Nf6 simply is not offered."""
    resp = _english_call(client, auth_headers, ["c2c4", "e7e6", "b1c3"], routing=None)
    assert resp.status_code == 200
    columns = {c["position_fen"]: c for c in resp.json()["columns"]}
    # Without the overlay this position is a base-graph leaf, so it has no column
    # at all — the English-order Nf6 is simply undiscoverable, as today.
    assert "g8f6" not in _ucis(columns.get(ENGLISH_NC3, {"nodes": []}))


def test_routing_view_failure_does_not_break_the_tree(client, auth_headers):
    """6. A raising overlay load degrades to the base tree rather than 500ing."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"],
        routing_error=RuntimeError("artifact unreadable"),
    )
    assert resp.status_code == 200
    columns = {c["position_fen"]: c for c in resp.json()["columns"]}
    # Without the overlay this position is a base-graph leaf, so it has no column
    # at all — the English-order Nf6 is simply undiscoverable, as today.
    assert "g8f6" not in _ucis(columns.get(ENGLISH_NC3, {"nodes": []}))


def test_overlay_edge_into_a_middlegame_is_a_display_only_boundary(
    client, auth_headers
):
    """7. An overlay edge crossing into the middlegame gets the same treatment as
    a base boundary edge: visible, terminal, non-navigable, null scorer metrics."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"], mid_fens=frozenset({HEDGEHOG})
    )
    column = next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3)
    node = _by_uci(column, "g8f6")
    assert node["is_transposition"] is True
    assert node["is_navigable"] is False
    assert node["terminal_reason"] == "opening_boundary"
    assert node["opening_score"] is None


def test_middlegame_parent_gains_no_overlay_children(client, auth_headers):
    """8. The overlay never reopens a line past the opening boundary: a parent that
    is already a middlegame position gets no unobserved overlay expansion."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6"],
        mid_fens=frozenset({HEDGEHOG}),
    )
    columns = {c["position_fen"]: c for c in resp.json()["columns"]}
    assert "d2d4" not in _ucis(columns.get(HEDGEHOG, {"nodes": []}))


def test_selected_overlay_boundary_pins_both_transposition_and_user_selected(
    client, auth_headers
):
    """10. The documented non-disjoint case: selecting an overlay edge that crosses
    the middlegame boundary sets BOTH flags — it really comes from the overlay, and
    it really is navigable only because it is the selected move of this line."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6"],
        mid_fens=frozenset({HEDGEHOG}),
        # Boundary membership is a position property, so selecting the card must
        # not restore the destination's cached row (owned via another move order).
        position_rows={
            HEDGEHOG: _pos_row(HEDGEHOG, score=88.0, confidence=0.9, coverage=0.7,
                               sample_size=20, game_count=9, has_evidence=True),
        },
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["is_transposition"] is True
    assert node["is_user_selected"] is True
    assert node["is_navigable"] is True          # for this line only
    assert node["terminal_reason"] == "opening_boundary"
    assert node["opening_score"] is None
    assert node["confidence"] is None
    assert node["coverage"] is None
    assert node["game_count"] is None


def test_overlay_edge_selected_from_a_middlegame_parent_is_still_a_transposition(
    client, auth_headers
):
    """10b. The injection-path variant of the same overlap: the overlay does not
    volunteer d4 from a middlegame Hedgehog (test 8), but a manually selected one
    is still identified as a transposition."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6", "d2d4"],
        mid_fens=frozenset({HEDGEHOG}),
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == HEDGEHOG), "d2d4"
    )
    assert node["is_transposition"] is True
    assert node["is_user_selected"] is True
    assert node["is_navigable"] is True


def test_selected_non_boundary_transposition_is_not_user_selected(client, auth_headers):
    """11. An ordinary (non-boundary) transposition is a persistent card, so being
    the selected move must NOT relabel it as the user's own board exploration."""
    resp = _english_call(client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6"])
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["is_transposition"] is True
    assert node["is_user_selected"] is False
    assert node["is_navigable"] is True


def test_transposition_destination_hydrates_normal_position_metrics(
    client, auth_headers
):
    """12. Metrics follow the destination, not the edge's provenance: an unobserved
    transposition has zero edge counts but full destination-position metrics."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"],
        position_rows={
            HEDGEHOG: _pos_row(HEDGEHOG, score=71.5, confidence=0.8, coverage=0.6,
                               sample_size=12, game_count=4, has_evidence=True),
        },
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["opening_score"] == 71.5
    assert node["confidence"] == 0.8
    assert node["coverage"] == 0.6
    assert node["game_count"] == 4
    assert node["encounter_count"] == 0
    assert node["user_choice_count"] == 0
    assert node["is_prepared"] is False


def test_transposition_survives_a_shorter_prefix_render(client, auth_headers):
    """13. A transposition is a persistent column member, not an ephemeral per-line
    injection: it is still listed in its column when a DEEPER line is requested, and
    with is_user_selected false, so a cached shorter-prefix view keeps it (the
    frontend drops stale user-selected siblings only)."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6", "d2d4", "d7d5"]
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["is_transposition"] is True
    assert node["is_user_selected"] is False


def test_transposition_frontier_is_covered_by_wave_two_prefetch(client, auth_headers):
    """14. Transposition destinations join the existing wave-two frontier rather
    than adding a third wave or per-node queries: exactly 2 observed-edge queries
    and no per-parent stragglers."""
    graph = _english_graph()
    key, edge = _obs_edge(["c2c4", "e7e6"], traversal_count=2)
    builder, response, requested = _run_build(
        graph, OpeningRoots({}, {}), _overlay("white", {key: edge}),
        ["c2c4", "e7e6", "b1c3"],
        routing=_english_routing(graph), mid_fens=frozenset(),
    )
    assert builder._observed_edge_query_count == 2
    assert builder._observed_straggler_count == 0
    assert len(requested) == 2
    # The Hedgehog is only reachable via the overlay, so its presence in wave two
    # proves the transposition frontier was collected.
    assert HEDGEHOG in requested[1]


def test_overlay_never_widens_the_scorer_structural_domain(client, auth_headers):
    """Pinned invariants: transposition cards are a display/navigation layer only.
    The scorer's structural-child domain, the graph fingerprint, and the canonical
    book path are all computed from the base graph and must not move."""
    from app.api.openings import _OpeningTreeBuilder

    graph = _english_graph()
    fingerprint_before = graph.fingerprint
    routing = _english_routing(graph)
    builder = _OpeningTreeBuilder(
        None, graph, OpeningRoots({}, {}), None, None, "white", 123, routing=routing
    )
    with patch("app.api.openings.is_middlegame_position", return_value=False):
        structural = builder._structural_children(ENGLISH_NC3)
        navigable = builder._navigable_children(ENGLISH_NC3)
        column = builder._column_children(ENGLISH_NC3)
        # The canonical book path still routes through the source book's own move
        # order (1.c4 Nf6 2.Nc3 e6), never through the overlay.
        book_path = builder._bfs_book_path(HEDGEHOG)

    assert "g8f6" not in structural            # scorer domain unchanged
    assert "g8f6" in navigable                 # browsable
    assert set(structural) <= set(navigable) <= set(column)
    assert book_path == ["c2c4", "g8f6", "b1c3", "e7e6"]
    assert graph.fingerprint == fingerprint_before
    assert graph.get_node(ENGLISH_NC3).children == {}   # graph never mutated


def test_observed_boundary_edge_keeps_its_overlay_provenance(client, auth_headers):
    """Provenance is independent of navigation eligibility: an observed edge that
    is ALSO an overlay edge and crosses into the middlegame keeps
    is_transposition, so the card is never mislabelled "Off book" (a move from the
    player's own games) when it is in fact a book transposition."""
    key, edge = _obs_edge(["c2c4", "e7e6", "b1c3", "g8f6"], traversal_count=4)
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"],
        overlay=_overlay("white", {key: edge}), mid_fens=frozenset({HEDGEHOG}),
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["is_observed"] is True
    assert node["is_transposition"] is True
    assert node["in_book"] is False


def test_observed_overlay_edge_from_a_middlegame_parent_keeps_its_provenance(
    client, auth_headers
):
    """The other half of the same contract: an observed edge out of an ALREADY
    middlegame parent gets no overlay expansion (test 8) but must still report the
    overlay provenance it genuinely has."""
    key, edge = _obs_edge(["c2c4", "e7e6", "b1c3", "g8f6", "d2d4"], traversal_count=3)
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3", "g8f6"],
        overlay=_overlay("white", {key: edge}), mid_fens=frozenset({HEDGEHOG}),
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == HEDGEHOG), "d2d4"
    )
    assert node["is_observed"] is True
    assert node["is_transposition"] is True
    assert node["is_navigable"] is True   # observed edges are phase-authoritative


def test_display_only_boundary_card_never_shows_scorer_metrics(client, auth_headers):
    """A display-only boundary card is outside the scorer domain, so a cached row
    the destination owns via another (observed) move order must not leak onto it —
    the null-metrics contract holds even when a row exists."""
    resp = _english_call(
        client, auth_headers, ["c2c4", "e7e6", "b1c3"],
        mid_fens=frozenset({HEDGEHOG}),
        position_rows={
            HEDGEHOG: _pos_row(HEDGEHOG, score=88.0, confidence=0.9, coverage=0.7,
                               sample_size=20, game_count=9, has_evidence=True),
        },
    )
    node = _by_uci(
        next(c for c in resp.json()["columns"] if c["position_fen"] == ENGLISH_NC3),
        "g8f6",
    )
    assert node["is_navigable"] is False
    assert node["terminal_reason"] == "opening_boundary"
    assert node["opening_score"] is None
    assert node["confidence"] is None
    assert node["coverage"] is None
    assert node["game_count"] is None
