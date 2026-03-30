import pytest
from datetime import datetime, timezone, timedelta
from app.opening_rootcalc import compute_root_score, RootCalcConfig
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_evidence import EvidenceOverlay, NodeEvidence, EdgeEvidence
from app.opening_roots import OpeningRoots, OpeningRoot
from app.fen import active_color

def _make_node(fen: str) -> OpeningGraphNode:
    n = OpeningGraphNode(fen, active_color(fen))
    n.name = "Test Root"
    return n

def _make_root(fen: str, name: str="Test", children: set[str]=None) -> OpeningRoot:
    return OpeningRoot(
        opening_key=fen,
        opening_name=name,
        opening_family="TestFam",
        eco=None,
        depth=0,
        parent_keys=frozenset(),
        child_keys=frozenset(children or [])
    )

def test_unknown_root_raises():
    graph = OpeningGraph({}, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    overlay = EvidenceOverlay(1, "white")
    roots = OpeningRoots({}, {})
    with pytest.raises(ValueError):
        compute_root_score("unknown", "white", graph, overlay, roots)

def test_mastery():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    graph = OpeningGraph({fen: _make_node(fen)}, fen)
    roots = OpeningRoots({fen: _make_root(fen)}, {fen: frozenset([fen])})
    config = RootCalcConfig(alpha=1.0, beta=2.0)

    # 1. No evidence
    overlay = EvidenceOverlay(1, "white")
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config)
    assert pytest.approx(score.opening_score) == 100.0 * (1.0 / 3.0)
    
    # 2. All passes
    overlay.nodes[fen] = NodeEvidence(fen=fen, live_attempts=3, live_passes=3, live_fails=0)
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config)
    assert pytest.approx(score.opening_score) == 100.0 * (4.0 / 6.0)

    # 3. All fails
    overlay.nodes[fen] = NodeEvidence(fen=fen, live_attempts=3, live_passes=0, live_fails=3)
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config)
    assert pytest.approx(score.opening_score) == 100.0 * (1.0 / 6.0)

def test_confidence():
    import math
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    graph = OpeningGraph({fen: _make_node(fen)}, fen)
    roots = OpeningRoots({fen: _make_root(fen)}, {fen: frozenset([fen])})
    config = RootCalcConfig(k_evidence=5.0, half_life_days=45.0, lambda_review=0.5)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # 1. No evidence
    overlay = EvidenceOverlay(1, "white")
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config, now=now)
    assert score.confidence == 0.0

    # 2. Recent evidence, no decay
    overlay.nodes[fen] = NodeEvidence(fen=fen, live_attempts=5, last_live_at=now)
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config, now=now)
    expected_c = 1.0 - math.exp(-5.0 / 5.0)
    assert pytest.approx(score.confidence) == 100.0 * expected_c

    # 3. Stale decay
    stale_date = now - timedelta(days=45)
    overlay.nodes[fen] = NodeEvidence(fen=fen, live_attempts=5, last_live_at=stale_date)
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config, now=now)
    freshness = math.exp(-45.0 / 45.0)
    assert pytest.approx(score.confidence) == 100.0 * (expected_c * freshness)

    # 4. Review discount
    overlay.nodes[fen] = NodeEvidence(fen=fen, live_attempts=0, review_attempts=10, last_review_at=now)
    score = compute_root_score(fen, "white", graph, overlay, roots, config=config, now=now)
    # 10 review * 0.5 = 5.0 evidence eq.
    assert pytest.approx(score.confidence) == 100.0 * expected_c

def test_prepared_children():
    # User node -> 3 children. 1 prepared by attempts, 1 prepared by passes, 1 prepared by ghost
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_c1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_c2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    fen_c3 = "8/8/8/8/8/8/8/8 b KQkq - 0 3"
    
    nodes = {
        fen_u: _make_node(fen_u),
        fen_c1: _make_node(fen_c1),
        fen_c2: _make_node(fen_c2),
        fen_c3: _make_node(fen_c3)
    }
    nodes[fen_u].children = {"e4": fen_c1, "d4": fen_c2, "c4": fen_c3}
    
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_u, fen_c1)] = EdgeEvidence(fen_u, fen_c1, "e4", live_attempts=2) # Prepared by attempts
    overlay.edges[(fen_u, fen_c2)] = EdgeEvidence(fen_u, fen_c2, "d4", live_attempts=1, live_passes=1) # Prepared by pass
    overlay.nodes[fen_c3] = NodeEvidence(fen_c3, is_ghost_target=True) # Prepared by ghost
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, debug=True)
    # All three should be prepared.
    debug_u = next(d for d in score.debug_nodes if d.fen == fen_u)
    assert set(debug_u.prepared_children) == {fen_c1, fen_c2, fen_c3}

def test_repertoire_weights():
    # test rho smoothing, single child, equal children
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_c1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_c2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    
    nodes = {
        fen_u: _make_node(fen_u),
        fen_c1: _make_node(fen_c1),
        fen_c2: _make_node(fen_c2)
    }
    nodes[fen_u].children = {"e4": fen_c1, "d4": fen_c2}
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    config = RootCalcConfig(rho=1.0)
    
    overlay = EvidenceOverlay(1, "white")
    # Equal children
    overlay.edges[(fen_u, fen_c1)] = EdgeEvidence(fen_u, fen_c1, "e4", live_attempts=2)
    overlay.edges[(fen_u, fen_c2)] = EdgeEvidence(fen_u, fen_c2, "d4", live_attempts=2)
    score = compute_root_score(fen_u, "white", graph, overlay, roots, config=config, debug=True)
    debug_u = next(d for d in score.debug_nodes if d.fen == fen_u)
    assert debug_u.weights[fen_c1] == 0.5
    assert debug_u.weights[fen_c2] == 0.5
    
    # Differential
    overlay.edges[(fen_u, fen_c1)] = EdgeEvidence(fen_u, fen_c1, "e4", live_attempts=3)
    overlay.edges[(fen_u, fen_c2)] = EdgeEvidence(fen_u, fen_c2, "d4", live_attempts=1, live_passes=1) # Need live_passes=1 to be prepared!
    # total basis = (3+1) + (1+1) = 6. weights: 4/6 and 2/6
    score = compute_root_score(fen_u, "white", graph, overlay, roots, config=config, debug=True)
    debug_u = next(d for d in score.debug_nodes if d.fen == fen_u)
    assert pytest.approx(debug_u.weights[fen_c1]) == 4.0/6.0
    assert pytest.approx(debug_u.weights[fen_c2]) == 2.0/6.0

def test_score_recursion():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_o1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_o2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    fen_leaf1 = "8/8/8/8/8/8/8/8 w KQkq - 0 3"
    fen_leaf2 = "8/8/8/8/8/8/8/8 w KQkq - 0 4"
    
    nodes = {
        fen_u: OpeningGraphNode(fen_u, "w"),
        fen_o1: OpeningGraphNode(fen_o1, "b"),
        fen_o2: OpeningGraphNode(fen_o2, "b"),
        fen_leaf1: OpeningGraphNode(fen_leaf1, "w"),
        fen_leaf2: OpeningGraphNode(fen_leaf2, "w"),
    }
    nodes[fen_u].children = {"1": fen_o1, "2": fen_o2}
    nodes[fen_o1].children = {"1": fen_leaf1}
    nodes[fen_o2].children = {"1": fen_leaf2}
    
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    config = RootCalcConfig(alpha=1.0, beta=1.0, gamma=0.5, rho=1.0)
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_u, fen_o1)] = EdgeEvidence(fen_u, fen_o1, "1", live_attempts=2)
    overlay.edges[(fen_u, fen_o2)] = EdgeEvidence(fen_u, fen_o2, "2", live_attempts=2)
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, config=config, debug=True)
    assert pytest.approx(score.opening_score) == 100.0 * 0.625 / 1.5

def test_book_exit_extension():
    fen_book_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_ext_o = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_ext_u = "8/8/8/8/8/8/8/8 w KQkq - 0 2"
    fen_ext_o2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    
    nodes = {fen_book_u: OpeningGraphNode(fen_book_u, "w")}
    graph = OpeningGraph(nodes, fen_book_u)
    roots = OpeningRoots({fen_book_u: _make_root(fen_book_u)}, {fen_book_u: frozenset([fen_book_u])})
    
    config = RootCalcConfig(book_exit_extension_user_decisions=2)
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_book_u, fen_ext_o)] = EdgeEvidence(fen_book_u, fen_ext_o, "u", live_attempts=2)
    overlay.edges[(fen_ext_o, fen_ext_u)] = EdgeEvidence(fen_ext_o, fen_ext_u, "u")
    overlay.edges[(fen_ext_u, fen_ext_o2)] = EdgeEvidence(fen_ext_u, fen_ext_o2, "u", live_attempts=2)
    
    score = compute_root_score(fen_book_u, "white", graph, overlay, roots, config=config, debug=True)
    debug_u = {d.fen: d for d in score.debug_nodes}
    assert fen_book_u in debug_u
    assert fen_ext_o in debug_u
    assert fen_ext_u in debug_u
    assert fen_ext_o2 in debug_u
    
    assert debug_u[fen_ext_o].is_extension_node
    assert debug_u[fen_ext_u].is_extension_node
    assert debug_u[fen_ext_o2].is_extension_node

def test_coverage():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_o1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_o2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    fen_leaf = "8/8/8/8/8/8/8/8 w KQkq - 0 3"
    
    nodes = {
        fen_u: OpeningGraphNode(fen_u, "w"),
        fen_o1: OpeningGraphNode(fen_o1, "b"),
        fen_o2: OpeningGraphNode(fen_o2, "b"),
        fen_leaf: OpeningGraphNode(fen_leaf, "w"),
    }
    nodes[fen_u].children = {"1": fen_o1, "2": fen_o2}
    nodes[fen_o1].children = {"1": fen_leaf}
    nodes[fen_o2].children = {"1": fen_leaf}
    
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    config = RootCalcConfig(coverage_live_threshold=2)
    
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[fen_leaf] = NodeEvidence(fen_leaf, live_attempts=2)
    overlay.edges[(fen_u, fen_o1)] = EdgeEvidence(fen_u, fen_o1, "1", live_attempts=2)
    overlay.edges[(fen_u, fen_o2)] = EdgeEvidence(fen_u, fen_o2, "2", live_attempts=2)
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, config=config, debug=True)
    # user leaves with no prep children have coverage 0.0 but wait! The spec says:
    # "At user nodes: user leaf: Cov(n) = 1.0. user with no prepared children: Cov(n) = 0.0."
    # A leaf is a user leaf. Since my code uses `is_leaf` which gives 1.0, it works.
    assert score.coverage == 100.0

def test_weighted_depth():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_o = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    
    nodes = {fen_u: OpeningGraphNode(fen_u, "w"), fen_o: OpeningGraphNode(fen_o, "b")}
    nodes[fen_u].children = {"1": fen_o}
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    
    config = RootCalcConfig(gamma=0.5, alpha=1.0, beta=1.0)
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_u, fen_o)] = EdgeEvidence(fen_u, fen_o, "1", live_attempts=2)
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, config=config)
    assert pytest.approx(score.weighted_depth) == 0.5

def test_underexposed_branch():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_desc1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_desc2 = "8/8/8/8/8/8/8/8 w KQkq - 0 2"
    
    nodes = {
        fen_root: OpeningGraphNode(fen_root, "w"),
        fen_desc1: OpeningGraphNode(fen_desc1, "b"),
        fen_desc2: OpeningGraphNode(fen_desc2, "w")
    }
    nodes[fen_root].children = {"1": fen_desc1}
    nodes[fen_desc1].children = {"1": fen_desc2}
    
    graph = OpeningGraph(nodes, fen_root)
    root1 = _make_root(fen_root, "R", children={fen_desc1})
    root2 = _make_root(fen_desc1, "D1", children={fen_desc2})
    root3 = _make_root(fen_desc2, "D2")
    roots = OpeningRoots(
        {fen_root: root1, fen_desc1: root2, fen_desc2: root3},
        {fen_root: frozenset([fen_root]), fen_desc1: frozenset([fen_desc1]), fen_desc2: frozenset([fen_desc2])}
    )
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_root, fen_desc1)] = EdgeEvidence(fen_root, fen_desc1, "1", live_attempts=2)
    overlay.edges[(fen_desc1, fen_desc2)] = EdgeEvidence(fen_desc1, fen_desc2, "1", live_attempts=2)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots)
    assert score.underexposed_branch is not None
    assert score.underexposed_branch.opening_key in (fen_desc1, fen_desc2)

def test_branch_summaries():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_c1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_c2 = "8/8/8/8/8/8/8/8 b KQkq - 0 2"
    
    nodes = {
        fen_root: OpeningGraphNode(fen_root, "w"),
        fen_c1: OpeningGraphNode(fen_c1, "b"),
        fen_c2: OpeningGraphNode(fen_c2, "b"),
    }
    nodes[fen_root].children = {"1": fen_c1, "2": fen_c2}
    
    graph = OpeningGraph(nodes, fen_root)
    r = _make_root(fen_root, "R", children={fen_c1, fen_c2})
    rc1 = _make_root(fen_c1, "C1")
    rc2 = _make_root(fen_c2, "C2")
    roots = OpeningRoots({fen_root: r, fen_c1: rc1, fen_c2: rc2}, {f: frozenset([f]) for f in nodes})
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_root, fen_c1)] = EdgeEvidence(fen_root, fen_c1, "1", live_attempts=5, live_passes=5)
    overlay.edges[(fen_root, fen_c2)] = EdgeEvidence(fen_root, fen_c2, "2", live_attempts=2, live_fails=2)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots)
    assert score.strongest_branch is not None
    assert score.weakest_branch is not None

def test_dag_and_extension_safety():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_b = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    nodes = {fen_root: OpeningGraphNode(fen_root, "w")}
    graph = OpeningGraph(nodes, fen_root)
    roots = OpeningRoots({fen_root: _make_root(fen_root)}, {fen_root: frozenset([fen_root])})
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_root, fen_b)] = EdgeEvidence(fen_root, fen_b, "1", live_attempts=2)
    overlay.edges[(fen_b, fen_root)] = EdgeEvidence(fen_b, fen_root, "2", live_attempts=2)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots)
    assert score is not None

def test_aggregates():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    nodes = {fen_root: OpeningGraphNode(fen_root, "w")}
    graph = OpeningGraph(nodes, fen_root)
    roots = OpeningRoots({fen_root: _make_root(fen_root)}, {fen_root: frozenset([fen_root])})
    
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[fen_root] = NodeEvidence(fen_root, live_attempts=10, last_live_at=now)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots, now=now)
    assert score.sample_size == 10
    assert score.last_practiced_at == now

def test_edge_cases():
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    graph = OpeningGraph({fen: _make_node(fen)}, fen)
    roots = OpeningRoots({fen: _make_root(fen)}, {fen: frozenset([fen])})
    overlay = EvidenceOverlay(1, "white")
    score = compute_root_score(fen, "white", graph, overlay, roots)
    assert score.opening_score > 0
    assert score.confidence == 0
    assert score.coverage == 100.0


def test_gap_1_user_node_with_no_prepared_children():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_o = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    nodes = {fen_u: _make_node(fen_u), fen_o: _make_node(fen_o)}
    nodes[fen_u].children = {"1": fen_o}
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    
    # Root has one child but no evidence -> zero prepared children.
    overlay = EvidenceOverlay(1, "white")
    score = compute_root_score(fen_u, "white", graph, overlay, roots, debug=True)
    assert score.coverage == 0.0

def test_gap_2_opponent_extension_frontier():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_o = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_ext = "8/8/8/8/8/8/8/8 w KQkq - 0 2"
    nodes = {fen_u: _make_node(fen_u), fen_o: _make_node(fen_o)}
    nodes[fen_u].children = {"1": fen_o}
    graph = OpeningGraph(nodes, fen_u)
    roots = OpeningRoots({fen_u: _make_root(fen_u)}, {f: frozenset([fen_u]) for f in nodes})
    
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_u, fen_o)] = EdgeEvidence(fen_u, fen_o, "1", live_attempts=2)
    # Opponent exits the book
    overlay.edges[(fen_o, fen_ext)] = EdgeEvidence(fen_o, fen_ext, "e", live_attempts=2)
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, debug=True)
    debug_fens = {d.fen for d in score.debug_nodes}
    assert fen_ext in debug_fens

def test_gap_3_unseen_child_roots():
    fen_u = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_c = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    nodes = {fen_u: _make_node(fen_u), fen_c: _make_node(fen_c)}
    nodes[fen_u].children = {"1": fen_c}
    graph = OpeningGraph(nodes, fen_u)
    
    # fen_c is a descendant boundary root!
    root1 = _make_root(fen_u, children={fen_c})
    root2 = _make_root(fen_c)
    roots = OpeningRoots({fen_u: root1, fen_c: root2}, {fen_u: frozenset([fen_u]), fen_c: frozenset([fen_c])})
    
    overlay = EvidenceOverlay(1, "white")
    # NO evidence edge -> unseen child root!
    
    score = compute_root_score(fen_u, "white", graph, overlay, roots, debug=True)
    debug_fens = {d.fen for d in score.debug_nodes}
    
    # The child root MUST NOT leak into the debug nodes (it's not scored by parent)
    assert fen_c not in debug_fens
    
    # But it MUST appear in the strongest branch summary due to importance
    assert score.strongest_branch is not None
    assert score.strongest_branch.opening_key == fen_c


def test_gap_4_underexposed_branch_local_coverage():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_desc1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_deep = "8/8/8/8/8/8/8/8 w KQkq - 0 2"
    
    nodes = {
        fen_root: _make_node(fen_root),
        fen_desc1: _make_node(fen_desc1),
        fen_deep: _make_node(fen_deep)
    }
    nodes[fen_root].children = {"1": fen_desc1}
    nodes[fen_desc1].children = {"1": fen_deep}
    
    graph = OpeningGraph(nodes, fen_root)
    root1 = _make_root(fen_root, children={fen_desc1})
    root2 = _make_root(fen_desc1)
    roots = OpeningRoots({fen_root: root1, fen_desc1: root2}, {fen_root: frozenset([fen_root]), fen_desc1: frozenset([fen_desc1]), fen_deep: frozenset([fen_desc1])})
    
    overlay = EvidenceOverlay(1, "white")
    # Coverage deep in the descendant
    overlay.nodes[fen_deep] = NodeEvidence(fen_deep, live_attempts=2)
    overlay.edges[(fen_root, fen_desc1)] = EdgeEvidence(fen_root, fen_desc1, "1", live_attempts=5, live_passes=5)
    overlay.edges[(fen_desc1, fen_deep)] = EdgeEvidence(fen_desc1, fen_deep, "1", live_attempts=2)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots, debug=True)
    # Because it is fully covered deep down, it should NOT be emitted as underexposed!
    assert score.underexposed_branch is None

def test_gap_5_importance_ghost_target_boundary():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_desc1 = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_deep = "8/8/8/8/8/8/8/8 w KQkq - 0 2"
    
    nodes = {
        fen_root: _make_node(fen_root),
        fen_desc1: _make_node(fen_desc1),
        fen_deep: _make_node(fen_deep)
    }
    nodes[fen_root].children = {"1": fen_desc1}
    nodes[fen_desc1].children = {"1": fen_deep}
    
    graph = OpeningGraph(nodes, fen_root)
    root1 = _make_root(fen_root, children={fen_desc1})
    root2 = _make_root(fen_desc1)
    roots = OpeningRoots({fen_root: root1, fen_desc1: root2}, {fen_root: frozenset([fen_root]), fen_desc1: frozenset([fen_desc1]), fen_deep: frozenset([fen_desc1])})
    
    overlay = EvidenceOverlay(1, "white")
    # No live attempts at the root. But there is a ghost target deep inside desc1.
    overlay.nodes[fen_deep] = NodeEvidence(fen_deep, is_ghost_target=True)
    
    score = compute_root_score(fen_root, "white", graph, overlay, roots, debug=True)
    # The child root MUST appear in strongest_branch because importance crossed the boundary!
    assert score.strongest_branch is not None
    assert score.strongest_branch.opening_key == fen_desc1


def test_gap_6_global_ghost_target_overlay_path():
    fen_root = "8/8/8/8/8/8/8/8 w KQkq - 0 1"
    fen_desc = "8/8/8/8/8/8/8/8 b KQkq - 0 1"
    fen_ext = "8/8/8/8/8/8/8/8 w KQkq - 0 2"

    nodes = {fen_root: _make_node(fen_root), fen_desc: _make_node(fen_desc)}
    nodes[fen_root].children = {"1": fen_desc}
    graph = OpeningGraph(nodes, fen_root)

    root1 = _make_root(fen_root, children={fen_desc})
    root2 = _make_root(fen_desc)
    roots = OpeningRoots(
        {fen_root: root1, fen_desc: root2},
        {fen_root: frozenset([fen_root]), fen_desc: frozenset([fen_desc])},
    )

    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(fen_desc, fen_ext)] = EdgeEvidence(fen_desc, fen_ext, "1")
    overlay.nodes[fen_ext] = NodeEvidence(fen_ext, is_ghost_target=True)

    score = compute_root_score(fen_root, "white", graph, overlay, roots, debug=True)

    assert score.strongest_branch is not None
    assert score.strongest_branch.opening_key == fen_desc
