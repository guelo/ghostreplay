import math
from datetime import datetime, timedelta, timezone

import chess
import pytest

from app.fen import active_color, normalize_fen
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import (
    SYNTHETIC_INITIAL_FEN,
    SYNTHETIC_ROOT_FAMILY,
    SYNTHETIC_ROOT_NAME,
    RootCalcConfig,
    _SharedCalculator,
    compute_all_root_scores,
    compute_root_score,
    root_calc_config_fingerprint,
)
from app.opening_roots import OpeningRoot, OpeningRoots


def _fen(board: chess.Board) -> str:
    return normalize_fen(board.fen())


def _positions(moves: list[str]) -> list[str]:
    board = chess.Board()
    result = [_fen(board)]
    for uci in moves:
        board.push_uci(uci)
        result.append(_fen(board))
    return result


def _graph(paths: list[list[str]]) -> OpeningGraph:
    nodes: dict[str, OpeningGraphNode] = {}
    root_fen = _fen(chess.Board())
    for moves in paths:
        board = chess.Board()
        parent = _fen(board)
        nodes.setdefault(parent, OpeningGraphNode(parent, active_color(parent)))
        for uci in moves:
            board.push_uci(uci)
            child = _fen(board)
            nodes.setdefault(child, OpeningGraphNode(child, active_color(child)))
            nodes[parent].children[uci] = child
            nodes[child].parents.add((parent, uci))
            parent = child
    return OpeningGraph(nodes, root_fen)


def _root(
    fen: str,
    name: str = "Test",
    *,
    parents: set[str] | None = None,
    children: set[str] | None = None,
) -> OpeningRoot:
    return OpeningRoot(
        opening_key=fen,
        opening_name=name,
        opening_family="Test Family",
        eco=None,
        depth=0,
        parent_keys=frozenset(parents or ()),
        child_keys=frozenset(children or ()),
    )


def _roots(*items: OpeningRoot) -> OpeningRoots:
    return OpeningRoots(
        {item.opening_key: item for item in items},
        {item.opening_key: frozenset([item.opening_key]) for item in items},
    )


def _quality(
    fen: str,
    value: float,
    count: int = 1,
    *,
    at: datetime | None = None,
) -> NodeEvidence:
    return NodeEvidence(
        fen=fen,
        live_attempts=count,
        quality_sum=value,
        quality_count=count,
        last_live_at=at,
    )


def _prepared(
    overlay: EvidenceOverlay,
    parent: str,
    child: str,
    uci: str = "move",
    attempts: int = 2,
) -> None:
    overlay.edges[(parent, child)] = EdgeEvidence(
        parent, child, uci, live_attempts=attempts
    )


def test_unknown_root_raises():
    graph = _graph([[]])
    with pytest.raises(ValueError):
        compute_root_score(
            "unknown", "white", graph, EvidenceOverlay(1, "white"), _roots()
        )


def test_mastery_uses_continuous_quality():
    fen = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(fen))
    config = RootCalcConfig(alpha=1.0, beta=2.0)

    prior = compute_root_score(
        fen, "white", graph, EvidenceOverlay(1, "white"), roots, config
    )
    assert prior.opening_score == pytest.approx(100.0 / 3.0)

    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[fen] = _quality(fen, 1.5, count=3)
    score = compute_root_score(fen, "white", graph, overlay, roots, config)
    assert score.opening_score == pytest.approx(100.0 * 2.5 / 6.0)
    assert score.sample_size == 3


def test_confidence_preserves_sample_freshness_and_review_discount():
    fen = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(fen))
    config = RootCalcConfig(k_evidence=5.0, half_life_days=45.0, lambda_review=0.5)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    overlay = EvidenceOverlay(1, "white")

    overlay.nodes[fen] = NodeEvidence(fen, live_attempts=5, last_live_at=now)
    recent = compute_root_score(fen, "white", graph, overlay, roots, config, now)
    expected = 1.0 - math.exp(-1.0)
    assert recent.confidence == pytest.approx(100.0 * expected)

    overlay.nodes[fen].last_live_at = now - timedelta(days=45)
    stale = compute_root_score(fen, "white", graph, overlay, roots, config, now)
    assert stale.confidence == pytest.approx(100.0 * expected * math.exp(-1.0))

    overlay.nodes[fen] = NodeEvidence(
        fen, review_attempts=10, last_review_at=now
    )
    review = compute_root_score(fen, "white", graph, overlay, roots, config, now)
    assert review.confidence == pytest.approx(100.0 * expected)


def test_prepared_children_and_rho_weights():
    root, e4, d4, c4 = (
        _positions(["e2e4"])[0],
        _positions(["e2e4"])[1],
        _positions(["d2d4"])[1],
        _positions(["c2c4"])[1],
    )
    graph = _graph([["e2e4"], ["d2d4"], ["c2c4"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4", attempts=3)
    overlay.edges[(root, d4)] = EdgeEvidence(
        root, d4, "d2d4", live_attempts=1, live_passes=1
    )
    overlay.nodes[c4] = NodeEvidence(c4, is_ghost_target=True)

    score = compute_root_score(root, "white", graph, overlay, roots, debug=True)
    debug = next(node for node in score.debug_nodes if node.fen == root)
    assert set(debug.prepared_children) == {e4, d4, c4}
    assert debug.weights[e4] == pytest.approx(4.0 / 7.0)
    assert debug.weights[d4] == pytest.approx(2.0 / 7.0)
    assert debug.weights[c4] == pytest.approx(1.0 / 7.0)


def test_config_fingerprint_changes_with_scoring_parameters():
    assert root_calc_config_fingerprint() != root_calc_config_fingerprint(
        RootCalcConfig(alpha=1.25)
    )


def test_score_recursion_and_weighted_depth():
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[root] = _quality(root, 0.5)
    overlay.nodes[e5] = _quality(e5, 0.5)
    config = RootCalcConfig(alpha=1.0, beta=1.0, gamma=0.5)

    score = compute_root_score(root, "white", graph, overlay, roots, config)
    assert score.opening_score == pytest.approx(100.0 * 0.625 / 1.5)
    assert score.weighted_depth == pytest.approx(0.625)


def test_opponent_weights_prefer_reference_replies_over_observed_off_book_replies():
    root, opponent, reference = _positions(["e2e4", "e7e5"])
    off_book = _positions(["e2e4", "c7c5"])[2]
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, opponent, "e2e4")
    overlay.edges[(opponent, off_book)] = EdgeEvidence(
        opponent, off_book, "c7c5"
    )
    overlay.nodes[reference] = _quality(reference, 1.0)
    overlay.nodes[off_book] = _quality(off_book, 0.0)

    score = compute_root_score(root, "white", graph, overlay, roots, debug=True)
    debug = next(node for node in score.debug_nodes if node.fen == opponent)
    assert debug.weights == {reference: 1.0}


def test_observed_continuations_cross_raw_middlegame_but_book_edges_stop():
    root = _positions([])[0]
    observed_middle = "8/8/8/8/8/8/4k3/4K3 b - -"
    observed_continuation = "8/8/8/8/8/4k3/8/4K3 w - -"
    book_middle = "8/8/8/8/8/8/3k4/4K3 b - -"
    graph = _graph([[]])
    graph._nodes[observed_middle] = OpeningGraphNode(observed_middle, "black")
    graph._nodes[observed_continuation] = OpeningGraphNode(
        observed_continuation, "white"
    )
    graph._nodes[book_middle] = OpeningGraphNode(book_middle, "black")
    graph._nodes[root].children = {
        "observed": observed_middle,
        "reference": book_middle,
    }
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, observed_middle, "observed")
    overlay.edges[(observed_middle, observed_continuation)] = EdgeEvidence(
        observed_middle, observed_continuation, "continuation"
    )
    overlay.nodes[observed_continuation] = _quality(observed_continuation, 1.0)

    score = compute_root_score(root, "white", graph, overlay, roots, debug=True)
    debug_fens = {node.fen for node in score.debug_nodes}
    assert observed_middle in debug_fens
    assert observed_continuation in debug_fens
    assert book_middle not in debug_fens


def test_cycle_cut_is_seed_independent_and_renormalized():
    a, b = _positions(["e2e4"])
    graph = _graph([["e2e4"]])
    roots = _roots(
        _root(a, "A", children={b}),
        _root(b, "B", parents={a}),
    )
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, a, b, "e2e4")
    _prepared(overlay, b, a, "repeat")
    overlay.nodes[a] = _quality(a, 0.8)
    overlay.nodes[b] = _quality(b, 0.6)

    all_scores, _ = compute_all_root_scores("white", graph, overlay, roots)
    seeded_b = compute_root_score(b, "white", graph, overlay, roots)
    assert all_scores[b].opening_score == pytest.approx(seeded_b.opening_score)
    assert all_scores[b].opening_score == pytest.approx(45.0)
    assert all_scores[b].coverage == pytest.approx(100.0)

    calc = _SharedCalculator(
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(),
        datetime.now(timezone.utc),
        seeds=[a],
    )
    for total in (
        sum(calc._get_weights(a).values()),
        sum(calc._get_weights(b).values()),
    ):
        assert total == 0 or total == pytest.approx(1.0)
    assert not (calc._score_children(a) and calc._score_children(b))


def test_scc_cut_does_not_activate_observed_opponent_fallback_cycle():
    opponent = _positions(["e2e4"])[1]
    reference = _positions(["e2e4", "e7e5"])[2]
    observed = _positions(["e2e4", "c7c5"])[2]
    graph = OpeningGraph(
        {
            opponent: OpeningGraphNode(opponent, "black"),
            reference: OpeningGraphNode(reference, "white"),
        },
        opponent,
    )
    graph._nodes[opponent].children["reference"] = reference
    roots = _roots(_root(opponent))
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(opponent, observed)] = EdgeEvidence(
        opponent, observed, "observed"
    )
    _prepared(overlay, reference, opponent, "repeat-reference")
    _prepared(overlay, observed, opponent, "repeat-observed")
    overlay.nodes[observed] = _quality(observed, 1.0)

    calc = _SharedCalculator(
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(),
        datetime.now(timezone.utc),
        seeds=[opponent],
    )
    assert calc._precut_weights[opponent] == {reference: 1.0}
    assert calc._get_weights(opponent) == {}
    assert observed not in calc._get_weights(opponent)

    score = compute_root_score(opponent, "white", graph, overlay, roots)
    assert score.opening_score == pytest.approx(100.0)


def test_named_root_behind_unprepared_ancestor_is_still_scored():
    root, child = _positions(["e2e4"])
    graph = _graph([["e2e4"]])
    roots = _roots(
        _root(root, "Outer", children={child}),
        _root(child, "Inner", parents={root}),
    )
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[child] = _quality(child, 0.8)

    scores, eligible = compute_all_root_scores("white", graph, overlay, roots)
    assert child in eligible
    assert child in scores
    assert scores[root].strongest_branch is None


def test_transposition_sample_size_counts_unique_nodes():
    root = _positions([])[0]
    e4 = _positions(["e2e4"])[1]
    nf3 = _positions(["g1f3"])[1]
    diamond = _positions(["e2e4", "e7e5"])[2]
    graph = _graph([["e2e4"], ["g1f3"]])
    graph._nodes.setdefault(diamond, OpeningGraphNode(diamond, active_color(diamond)))
    graph._nodes[e4].children["to-d"] = diamond
    graph._nodes[nf3].children["to-d"] = diamond
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    _prepared(overlay, root, nf3, "g1f3")
    overlay.edges[(e4, diamond)] = EdgeEvidence(e4, diamond, "to-d")
    overlay.edges[(nf3, diamond)] = EdgeEvidence(nf3, diamond, "to-d")
    overlay.nodes[diamond] = _quality(diamond, 1.2, count=2)

    score = compute_root_score(root, "white", graph, overlay, roots)
    assert score.sample_size == 2


def test_prior_only_ghost_or_review_does_not_create_rows():
    root = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[root] = NodeEvidence(
        root,
        review_attempts=1,
        last_review_at=datetime.now(timezone.utc),
        is_ghost_target=True,
    )
    scores, eligible = compute_all_root_scores("white", graph, overlay, roots)
    assert scores == {}
    assert eligible == set()


def test_unprepared_descendant_is_not_an_underexposed_branch():
    root = _positions([])[0]
    prepared = _positions(["e2e4"])[1]
    ignored = _positions(["d2d4"])[1]
    graph = _graph([["e2e4"], ["d2d4"]])
    roots = _roots(
        _root(root, "Root", children={prepared, ignored}),
        _root(prepared, "Prepared", parents={root}),
        _root(ignored, "Ignored", parents={root}),
    )
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, prepared, "e2e4")
    overlay.nodes[prepared] = _quality(prepared, 0.8, count=2)
    overlay.nodes[ignored] = _quality(ignored, 0.2)

    scores, _ = compute_all_root_scores("white", graph, overlay, roots)
    summary = scores[root].underexposed_branch
    assert summary is None or summary.opening_key != ignored


def test_underexposed_value_is_fractional_coverage_gap():
    root, opponent, child, _unprepared = _positions(
        ["e2e4", "e7e5", "g1f3"]
    )
    graph = _graph([["e2e4", "e7e5", "g1f3"]])
    roots = _roots(
        _root(root, "Root", children={child}),
        _root(child, "Child", parents={root}),
    )
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, opponent, "e2e4")
    overlay.nodes[child] = _quality(child, 1.0)

    scores, _ = compute_all_root_scores("white", graph, overlay, roots)
    assert scores[child].coverage == pytest.approx(0.0)
    summary = scores[root].underexposed_branch
    assert summary is not None
    assert summary.opening_key == child
    assert summary.value == pytest.approx(1.0)


def test_scores_are_name_independent():
    root, child = _positions(["e2e4"])
    graph = _graph([["e2e4"]])
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, child, "e2e4")
    overlay.nodes[root] = _quality(root, 0.7)
    overlay.nodes[child] = _quality(child, 0.9)
    without_boundary = _roots(_root(root, "Outer"))
    with_boundary = _roots(
        _root(root, "Outer", children={child}),
        _root(child, "Inner", parents={root}),
    )

    first = compute_root_score(root, "white", graph, overlay, without_boundary)
    second = compute_root_score(root, "white", graph, overlay, with_boundary)
    assert first.opening_score == pytest.approx(second.opening_score)
    assert first.coverage == pytest.approx(second.coverage)


def test_shared_memo_computes_diamond_nodes_once_per_pass():
    root = _positions([])[0]
    left = _positions(["e2e4"])[1]
    right = _positions(["d2d4"])[1]
    leaf = _positions(["e2e4", "e7e5"])[2]
    graph = _graph([["e2e4"], ["d2d4"]])
    graph._nodes.setdefault(leaf, OpeningGraphNode(leaf, active_color(leaf)))
    graph._nodes[left].children["left-leaf"] = leaf
    graph._nodes[right].children["right-leaf"] = leaf
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, left, "e2e4")
    _prepared(overlay, root, right, "d2d4")
    overlay.edges[(left, leaf)] = EdgeEvidence(left, leaf, "left-leaf")
    overlay.edges[(right, leaf)] = EdgeEvidence(right, leaf, "right-leaf")
    overlay.nodes[root] = _quality(root, 0.8)
    overlay.nodes[leaf] = _quality(leaf, 0.9)
    calculator = _SharedCalculator(
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(),
        datetime.now(timezone.utc),
    )

    calculator.compute_roots([roots.get_root(root)], include_branch_summaries=False)
    assert calculator.calculation_misses == 8
    assert len(calculator._metrics) == 8


def test_synthetic_initial_root_emitted_only_when_requested():
    a, b = _positions(["e2e4"])
    assert a == SYNTHETIC_INITIAL_FEN
    graph = _graph([["e2e4"]])
    roots = _roots(_root(b, "King's Pawn"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, a, b, "e2e4")
    overlay.nodes[b] = _quality(b, 0.8)

    without, _ = compute_all_root_scores("white", graph, overlay, roots)
    assert SYNTHETIC_INITIAL_FEN not in without

    with_syn, _ = compute_all_root_scores(
        "white", graph, overlay, roots, include_synthetic_root=True
    )
    assert SYNTHETIC_INITIAL_FEN in with_syn
    syn = with_syn[SYNTHETIC_INITIAL_FEN]
    assert syn.opening_family == SYNTHETIC_ROOT_FAMILY
    assert syn.opening_name == SYNTHETIC_ROOT_NAME
    assert 0.0 <= syn.opening_score <= 100.0
    # The named root score is unchanged whether or not the synthetic row is added
    # (same shared DAG pass).
    assert with_syn[b].opening_score == pytest.approx(without[b].opening_score)
