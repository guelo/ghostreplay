import math
from datetime import datetime, timedelta, timezone

import chess
import pytest

from app.fen import active_color, normalize_fen
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import (
    REPORT_FOLD_SCOPES,
    REPORT_SCORER_CONTRACT_ID,
    REPORT_SELF_TERM_MODES,
    SYNTHETIC_INITIAL_FEN,
    SYNTHETIC_ROOT_FAMILY,
    SYNTHETIC_ROOT_NAME,
    CalcTelemetry,
    PositionCalcTelemetry,
    RootCalcConfig,
    _normalized,
    _SharedCalculator,
    compute_all_root_scores,
    compute_all_scores,
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


def _neutral_config(**overrides) -> RootCalcConfig:
    return RootCalcConfig(
        lcb_z=0.0,
        coverage_fold="off",
        coverage_live_threshold=2,
        **overrides,
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
    config = _neutral_config(alpha=1.0, beta=2.0)

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
    config = _neutral_config(alpha=1.0, beta=1.0, gamma=0.5)

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

    config = _neutral_config()
    all_scores, _ = compute_all_root_scores("white", graph, overlay, roots, config)
    seeded_b = compute_root_score(b, "white", graph, overlay, roots, config)
    assert all_scores[b].opening_score == pytest.approx(seeded_b.opening_score)
    assert all_scores[b].opening_score == pytest.approx(45.0)
    assert all_scores[b].coverage == pytest.approx(100.0)

    calc = _SharedCalculator(
        "white",
        graph,
        overlay,
        roots,
        config,
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

    scores, _ = compute_all_root_scores("white", graph, overlay, roots, _neutral_config())
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

    scores, _ = compute_all_root_scores("white", graph, overlay, roots, _neutral_config())
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


# A king-and-king position: majors_and_minors == 0 satisfies the middlegame
# predicate, so it is a raw-middlegame root that carries no quality evidence.
_MIDGAME_FEN = "8/8/8/4k3/4K3/8/8/8 w - -"


def test_telemetry_counts_keys_and_roots_separately():
    root, _e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root, "Mainline"), _root(_MIDGAME_FEN, "Bare kings"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, _positions(["e2e4"])[1], "e2e4")
    overlay.nodes[root] = _quality(root, 0.5)
    overlay.nodes[e5] = _quality(e5, 0.5)

    tel = CalcTelemetry()
    scores, _ = compute_all_root_scores(
        "white", graph, overlay, roots, telemetry=tel
    )

    assert tel.named_root_count == 2
    # The natural and perfect passes traverse the same FEN set, so their distinct
    # memo-key classes are equal in count and never conflated into one number.
    assert tel.actual_key_count == tel.perfect_key_count
    assert tel.actual_key_count > 0
    # Each memo miss writes exactly one (fen, perfect) key, across both passes.
    assert tel.calculation_misses == tel.actual_key_count + tel.perfect_key_count
    # raw-middlegame and unscored counts are independent: the bare-kings root is
    # raw-middlegame AND unscored, the mainline root is neither.
    assert tel.raw_middlegame_root_count == 1
    assert tel.unscored_root_count == 1
    assert _MIDGAME_FEN not in scores


def test_score_ordering_is_reproducible_across_recomputes():
    # Rank stability (v1-free replacement for rank-change comparison): two
    # consecutive recomputes of the same snapshot (fixed `now`) are identical.
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[root] = _quality(root, 0.7)
    overlay.nodes[e5] = _quality(e5, 0.6)
    now = datetime(2026, 6, 6, tzinfo=timezone.utc)

    first, _ = compute_all_root_scores("white", graph, overlay, roots, now=now)
    second, _ = compute_all_root_scores("white", graph, overlay, roots, now=now)

    assert {k: v.opening_score for k, v in first.items()} == {
        k: v.opening_score for k, v in second.items()
    }


def test_monotonic_sanity_strong_outranks_weak():
    # Known-strong vs known-weak: a root whose user moves are near-best must
    # outrank one whose moves are poor, holding tree shape constant.
    root, e4 = _positions(["e2e4"])
    graph = _graph([["e2e4"]])

    def score_for(quality_sum: float) -> float:
        roots = _roots(_root(root))
        overlay = EvidenceOverlay(1, "white")
        _prepared(overlay, root, e4, "e2e4")
        overlay.nodes[root] = _quality(root, quality_sum, count=4)
        scores, _ = compute_all_root_scores("white", graph, overlay, roots)
        return scores[root].opening_score

    # Four near-best observations (sum 3.8) vs four poor ones (sum 0.8).
    assert score_for(0.95 * 4) > score_for(0.2 * 4)


def test_telemetry_populated_on_empty_early_return():
    root = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(root, "Start"), _root(_MIDGAME_FEN, "Bare kings"))
    overlay = EvidenceOverlay(1, "white")  # no quality observations at all

    tel = CalcTelemetry()
    scores, eligible = compute_all_root_scores(
        "white", graph, overlay, roots, telemetry=tel
    )

    assert scores == {}
    assert eligible == set()
    # Well-formed zeros + structural counts, never None, on the early-return path.
    assert tel.named_root_count == 2
    assert tel.actual_key_count == 0
    assert tel.perfect_key_count == 0
    assert tel.calculation_misses == 0
    assert tel.unscored_root_count == 2
    # Structural, so still reported even though no calculator was built.
    assert tel.raw_middlegame_root_count == 1


# ---------------------------------------------------------------------------
# Direct position-score read model (g-tree-score-model).
#
# compute_position_scores emits one direct row per evidence-bearing reachable
# position plus connected observed off-book nodes, reusing the SAME shared
# calculator/_metrics traversal the named-root rows use (no per-position root
# walk). Rows are keyed by normalized FEN; no-evidence in-book nodes are not
# materialized; no-evidence connected off-book nodes are no-data rows.
# ---------------------------------------------------------------------------


def _position_rows(calculator, *, telemetry=None):
    return {row.normalized_fen: row for row in calculator.compute_position_scores(telemetry=telemetry)}


def _calculator(graph, overlay, roots, *, seeds=None, now=None):
    return _SharedCalculator(
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(),
        now or datetime.now(timezone.utc),
        seeds=seeds,
    )


def test_position_row_metrics_match_named_root_score():
    # A scoreable position's direct row carries the SAME opening_score/confidence/
    # coverage/weighted_depth/sample_size as the named-root computation for that FEN.
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root, "Mainline"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[root] = _quality(root, 0.5, at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    overlay.nodes[e5] = _quality(e5, 0.5, at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)

    named = compute_root_score(root, "white", graph, overlay, roots, now=now)
    rows = _position_rows(_calculator(graph, overlay, roots, now=now))

    assert root in rows
    row = rows[root]
    assert row.in_book is True
    assert row.has_evidence is True
    assert row.opening_score == pytest.approx(named.opening_score)
    assert row.confidence == pytest.approx(named.confidence)
    assert row.coverage == pytest.approx(named.coverage)
    assert row.weighted_depth == pytest.approx(named.weighted_depth)
    assert row.sample_size == named.sample_size
    assert row.game_count == named.game_count
    assert row.last_practiced_at == named.last_practiced_at


def test_position_rows_skip_in_book_no_evidence_nodes():
    # Static in-book positions with no evidence below are represented by the graph,
    # not materialized as rows. The no-evidence opponent-turn leaf must NOT surface
    # _calc's perfect-looking (1.0, 1.0, 1.0, 0.0) result.
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    unplayed = _positions(["d2d4"])[1]  # in-book sibling with no evidence below
    graph = _graph([["e2e4", "e7e5"], ["d2d4"]])
    roots = _roots(_root(root, "Mainline"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[e5] = _quality(e5, 0.5)  # evidence only deep on the e4 line

    rows = _position_rows(_calculator(graph, overlay, roots))

    # The user-turn leaf e5 (has its own quality) and its ancestors are scored.
    assert e5 in rows and rows[e5].has_evidence is True
    assert root in rows and rows[root].has_evidence is True
    assert e4 in rows and rows[e4].has_evidence is True
    # The opponent-turn no-evidence in-book sibling is absent — no fabricated score.
    assert unplayed not in rows


def test_position_rows_include_connected_observed_off_book_as_no_data():
    # An observed off-book continuation with no evidence at/below is persisted as a
    # navigable no-data row (in_book False, metrics None) so the API can tell it
    # apart from an arbitrary unknown FEN.
    root = _positions([])[0]
    off_book = "8/8/8/8/8/8/4k3/4K3 b - -"  # not in graph
    graph = _graph([[]])
    roots = _roots(_root(root, "Start"))
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(root, off_book)] = EdgeEvidence(root, off_book, "observed")
    overlay.nodes[root] = _quality(root, 0.5)  # evidence at root, none at/below off_book

    rows = _position_rows(_calculator(graph, overlay, roots))

    assert off_book in rows
    off_row = rows[off_book]
    assert off_row.in_book is False
    assert off_row.has_evidence is False
    assert off_row.opening_score is None
    assert off_row.confidence is None
    assert off_row.coverage is None
    assert off_row.weighted_depth is None
    assert off_row.sample_size == 0
    assert off_row.game_count == 0
    assert off_row.last_practiced_at is None


def test_position_rows_score_observed_off_book_with_evidence_below():
    # An observed off-book node WITH quality at/below gets a scored row (in_book
    # False, has_evidence True).
    root = _positions([])[0]
    off_book = "8/8/8/8/8/8/4k3/4K3 b - -"
    graph = _graph([[]])
    roots = _roots(_root(root, "Start"))
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(root, off_book)] = EdgeEvidence(root, off_book, "observed")
    overlay.nodes[off_book] = _quality(off_book, 1.0)

    rows = _position_rows(_calculator(graph, overlay, roots))

    assert off_book in rows
    off_row = rows[off_book]
    assert off_row.in_book is False
    assert off_row.has_evidence is True
    assert off_row.opening_score is not None
    assert off_row.sample_size == 1


def test_position_rows_dedupe_transpositions_to_one_row_per_fen():
    # Two distinct UCI lines that reach the same normalized FEN collapse to ONE
    # position row (position identity is normalized FEN).
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

    all_rows = _calculator(graph, overlay, roots).compute_position_scores()
    leaf_rows = [row for row in all_rows if row.normalized_fen == leaf]
    assert len(leaf_rows) == 1


def test_position_rows_stop_at_book_middlegame_boundary():
    # Reference-only book branches stop at the raw middlegame predicate, so a
    # book middlegame child gets no position row; an observed continuation that
    # crosses the boundary off-book still does.
    root = _positions([])[0]
    observed_middle = "8/8/8/8/8/8/4k3/4K3 b - -"
    observed_continuation = "8/8/8/8/8/4k3/8/4K3 w - -"
    book_middle = "8/8/8/8/8/8/3k4/4K3 b - -"
    graph = _graph([[]])
    graph._nodes[observed_middle] = OpeningGraphNode(observed_middle, "black")
    graph._nodes[observed_continuation] = OpeningGraphNode(observed_continuation, "white")
    graph._nodes[book_middle] = OpeningGraphNode(book_middle, "black")
    graph._nodes[root].children = {"observed": observed_middle, "reference": book_middle}
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, observed_middle, "observed")
    overlay.edges[(observed_middle, observed_continuation)] = EdgeEvidence(
        observed_middle, observed_continuation, "continuation"
    )
    overlay.nodes[observed_continuation] = _quality(observed_continuation, 1.0)

    rows = _position_rows(_calculator(graph, overlay, roots))

    assert observed_continuation in rows
    assert book_middle not in rows


def test_position_scores_share_one_memoized_traversal():
    # Direct rows for every scoreable position come from ONE shared memoized
    # traversal: at most two _metrics records (natural + perfect) per reachable
    # FEN, never one independent root walk per visible card.
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    deep = _positions(["e2e4", "e7e5", "g1f3"])[3]
    graph = _graph([["e2e4", "e7e5", "g1f3"]])
    roots = _roots(_root(root, "Mainline"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    _prepared(overlay, e5, deep, "g1f3")
    overlay.nodes[root] = _quality(root, 0.5)
    overlay.nodes[e5] = _quality(e5, 0.6)
    overlay.nodes[deep] = _quality(deep, 0.7)
    calculator = _calculator(graph, overlay, roots)

    tel = PositionCalcTelemetry()
    rows = calculator.compute_position_scores(telemetry=tel)

    # Several scoreable positions, but the metric key count stays bounded by
    # 2 * domain (one actual + one perfect per reachable FEN).
    assert tel.scoreable_position_count >= 3
    assert tel.metric_key_count == len(calculator._metrics)
    assert tel.metric_key_count <= 2 * tel.domain_count
    assert tel.persisted_row_count == len(rows)


def test_compute_all_scores_shares_calculator_with_named_rows():
    # compute_all_scores returns named rows AND position rows from one calculator;
    # the named row and the position row for the same FEN carry identical metrics.
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root, "Mainline"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[root] = _quality(root, 0.5)
    overlay.nodes[e5] = _quality(e5, 0.5)
    now = datetime(2026, 1, 3, tzinfo=timezone.utc)

    scores, eligible, positions = compute_all_scores(
        "white", graph, overlay, roots, now=now, include_synthetic_root=True
    )
    by_fen = {row.normalized_fen: row for row in positions}

    assert root in eligible
    assert scores[root].opening_score == pytest.approx(by_fen[root].opening_score)
    assert scores[root].coverage == pytest.approx(by_fen[root].coverage)
    assert scores[root].sample_size == by_fen[root].sample_size
    # The synthetic repertoire row anchors at the initial FEN, which is also the
    # scored position row for that FEN (one shared traversal).
    assert SYNTHETIC_INITIAL_FEN == root
    assert by_fen[root].has_evidence is True


def test_compute_all_scores_returns_no_positions_without_evidence():
    root = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(root, "Start"))
    overlay = EvidenceOverlay(1, "white")  # no quality and no observed edges

    scores, eligible, positions = compute_all_scores("white", graph, overlay, roots)

    assert scores == {}
    assert eligible == set()
    assert positions == []


def test_compute_all_scores_emits_off_book_no_data_rows_without_any_quality():
    # An overlay with a connected observed off-book edge but ZERO quality anywhere
    # (e.g. a played game not yet analyzed) must still surface the navigable off-book
    # no-data row. The named-root early-return optimization must not drop it, and it
    # must NOT fabricate a named/synthetic row.
    root = _positions([])[0]
    off_book = "8/8/8/8/8/8/4k3/4K3 b - -"  # not in graph
    graph = _graph([[]])
    roots = _roots(_root(root, "Start"))
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(root, off_book)] = EdgeEvidence(root, off_book, "observed")

    scores, eligible, positions = compute_all_scores(
        "white", graph, overlay, roots, include_synthetic_root=True
    )

    # No named/synthetic rows without quality...
    assert scores == {}
    assert eligible == set()
    assert SYNTHETIC_INITIAL_FEN not in scores
    # ...but the connected observed off-book node is a navigable no-data row,
    # matching compute_position_scores() called directly.
    by_fen = {row.normalized_fen: row for row in positions}
    assert off_book in by_fen
    assert by_fen[off_book].in_book is False
    assert by_fen[off_book].has_evidence is False
    assert by_fen[off_book].opening_score is None


# ---------------------------------------------------------------------------
# Readiness folds (g-zc3p / g-5bcz): LCB on mastery + opponent-coverage gate.
#
# These exercise the scoring MATH around the calibrated production defaults
# (lcb_z=1.0, coverage_fold="gate", coverage_live_threshold=1), so tests that
# assert pre-fold arithmetic pass the neutral fields explicitly.
# ---------------------------------------------------------------------------


def _shared_calc(graph, overlay, roots, config, *, seeds=None):
    return _SharedCalculator(
        "white", graph, overlay, roots, config, datetime.now(timezone.utc), seeds=seeds
    )


def test_mastery_lcb_shrinks_thin_evidence():
    # LCB mastery = clamped normal approx clamp(mean - z*std, 0, 1). At z=0 it is
    # EXACTLY today's Beta-posterior mean; z>0 shrinks thin evidence below it.
    fen = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(fen))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[fen] = _quality(fen, 3.0, count=3)  # mean = (3+1)/(3+3) = 4/6

    def mastery(z: float) -> float:
        return _shared_calc(graph, overlay, roots, RootCalcConfig(lcb_z=z))._mastery(fen)

    assert mastery(0.0) == pytest.approx(4.0 / 6.0)  # unchanged at z=0
    a, b, total = 4.0, 2.0, 6.0  # a=quality_sum+alpha, b=(count-sum)+beta
    expected = 4.0 / 6.0 - math.sqrt(a * b / (total * total * (total + 1.0)))
    assert mastery(1.0) == pytest.approx(expected)
    assert mastery(1.0) < 4.0 / 6.0


def test_mastery_lcb_shrinks_zero_quality_prior_and_clamps():
    # DECIDED (no quality_count>0 guard): the LCB shrinks the UNEARNED prior too
    # (~0.33 -> ~0.10 at z=1), the backstop for the gate-alone line-606 leak.
    fen = _positions([])[0]
    graph = _graph([[]])
    roots = _roots(_root(fen))
    overlay = EvidenceOverlay(1, "white")  # no quality anywhere: prior only

    def mastery(z: float) -> float:
        return _shared_calc(graph, overlay, roots, RootCalcConfig(lcb_z=z))._mastery(fen)

    assert mastery(0.0) == pytest.approx(1.0 / 3.0)
    # a=1, b=2, total=3 → mean 1/3, std sqrt(2/36) ≈ 0.236 → ~0.10.
    assert mastery(1.0) == pytest.approx(1.0 / 3.0 - math.sqrt(2.0 / 36.0))
    assert mastery(1.0) < 1.0 / 3.0
    # Aggressive z clamps at 0 rather than going negative.
    assert mastery(10.0) == 0.0


def test_config_fingerprint_includes_readiness_fold_fields():
    base = root_calc_config_fingerprint()
    assert RootCalcConfig().lcb_z == 1.0
    assert RootCalcConfig().coverage_fold == "gate"
    assert RootCalcConfig().coverage_live_threshold == 1
    assert base != root_calc_config_fingerprint(RootCalcConfig(lcb_z=0.0))
    assert base != root_calc_config_fingerprint(RootCalcConfig(coverage_fold="off"))
    assert base != root_calc_config_fingerprint(RootCalcConfig(coverage_live_threshold=2))


def test_config_rejects_unknown_coverage_fold():
    # Fail fast so a bad mode never silently behaves as "gate" in _calc.
    with pytest.raises(ValueError):
        RootCalcConfig(coverage_fold="bogus")
    for mode in ("off", "gate", "gate_x_cov"):
        assert RootCalcConfig(coverage_fold=mode).coverage_fold == mode


def _opponent_root_two_replies():
    # Player white; the named root is the OPPONENT node after 1.e4 (black to move),
    # with two book replies: e5 (covered/strong) and c5 (unprepared user leaf).
    opp = _positions(["e2e4"])[1]
    covered = _positions(["e2e4", "e7e5"])[2]
    uncovered = _positions(["e2e4", "c7c5"])[2]
    graph = _graph([["e2e4", "e7e5"], ["e2e4", "c7c5"]])
    roots = _roots(_root(opp))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[covered] = _quality(covered, 4.0, count=4)  # mastery 5/7, live=4
    # `uncovered` has no evidence: prior mastery, subtree not covered.
    return graph, overlay, roots, opp, covered, uncovered


def test_opponent_gate_removes_unprepared_line_freebie():
    # An unprepared opponent reply (user leaf, no evidence) returns the ~0.33
    # mastery prior with child_cov=1.0. Under "off" it collects that freebie; the
    # 0/1 gate ALONE zeroes it (subtree not covered), leaving only the covered reply.
    graph, overlay, roots, opp, _covered, _unc = _opponent_root_two_replies()

    def score(fold: str) -> float:
        return compute_root_score(
            opp,
            "white",
            graph,
            overlay,
            roots,
            RootCalcConfig(lcb_z=0.0, coverage_fold=fold, coverage_live_threshold=2),
        ).opening_score

    off = score("off")
    gate = score("gate")
    # off: 0.5*(5/7) + 0.5*(1/3) over a perfect denominator of 1.0.
    assert off == pytest.approx(100.0 * (0.5 * (5.0 / 7.0) + 0.5 * (1.0 / 3.0)))
    # gate: the uncovered reply contributes exactly 0 → only the covered credit.
    assert gate == pytest.approx(100.0 * (0.5 * (5.0 / 7.0)))
    assert gate < off


def test_perfect_pass_assumes_full_coverage():
    # The perfect pass uses branch_cov=1.0, so the coverage shortfall bites the
    # numerator only. Both replies count at full credit in the denominator (=1.0);
    # had perfect ALSO been gated, the uncovered branch would cancel and the score
    # would RISE above "off" instead of dropping.
    graph, overlay, roots, opp, _covered, _unc = _opponent_root_two_replies()
    off = compute_root_score(
        opp,
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(lcb_z=0.0, coverage_fold="off", coverage_live_threshold=2),
    ).opening_score
    gate = compute_root_score(
        opp,
        "white",
        graph,
        overlay,
        roots,
        RootCalcConfig(lcb_z=0.0, coverage_fold="gate", coverage_live_threshold=2),
    ).opening_score
    # Perfect denominator = 1.0 (both replies full credit), so gate == 100*0.5*(5/7).
    assert gate == pytest.approx(100.0 * 0.5 * (5.0 / 7.0))
    # If perfect were gated too the denominator would be 0.5 → gate would be ~71 > off.
    assert gate < off


def test_deep_gap_penalized_once_gate_vs_gate_x_cov_diverge():
    # A coverage gap two opponent-nodes deep: opp1 -> u1 -> opp2 -> {covered, gap}.
    # The gate applies ONCE, at opp2 (where the gap hangs), γ-discounted upward.
    # gate_x_cov re-applies it at opp1 via child_cov(u1) — the double-count.
    opp1 = _positions(["e2e4"])[1]
    u1 = _positions(["e2e4", "e7e5"])[2]
    opp2 = _positions(["e2e4", "e7e5", "g1f3"])[3]
    covered = _positions(["e2e4", "e7e5", "g1f3", "b8c6"])[4]
    gap = _positions(["e2e4", "e7e5", "g1f3", "d7d6"])[4]
    graph = _graph(
        [["e2e4", "e7e5", "g1f3", "b8c6"], ["e2e4", "e7e5", "g1f3", "d7d6"]]
    )
    roots = _roots(_root(opp1))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, u1, opp2, "g1f3")
    overlay.nodes[u1] = _quality(u1, 4.0, count=4)  # covered, mastery 5/7
    overlay.nodes[covered] = _quality(covered, 4.0, count=4)  # covered reply
    # `gap` has no evidence: opp2's uncovered reply.

    def score(fold: str) -> float:
        return compute_root_score(
            opp1,
            "white",
            graph,
            overlay,
            roots,
            RootCalcConfig(lcb_z=0.0, coverage_fold=fold, coverage_live_threshold=2),
        ).opening_score

    off, gate, gate_x_cov = score("off"), score("gate"), score("gate_x_cov")
    assert off > gate > gate_x_cov
    # The gap is penalized ONCE under gate but re-applied under gate_x_cov, so the
    # extra drop from gate→gate_x_cov dwarfs the single off→gate penalty.
    assert (gate - gate_x_cov) > (off - gate)


def test_line_606_gate_alone_leak_returns_node_mastery():
    # The one shape gate-alone leaks: a covered user node with book children but
    # NONE prepared returns node mastery (not 0), child_cov=0.0. gate-alone credits
    # weight*mastery (accepted, small, LCB-shrunk); gate_x_cov zeroes it.
    opp = _positions(["e2e4"])[1]
    user_node = _positions(["e2e4", "e7e5"])[2]  # covered, book child, none prepared
    graph = _graph([["e2e4", "e7e5", "g1f3"]])
    roots = _roots(_root(opp))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[user_node] = _quality(user_node, 4.0, count=4)  # covered, mastery 5/7
    # No prepared edge out of user_node → weights empty → the line-606 result shape.

    def score(fold: str) -> float:
        return compute_root_score(
            opp,
            "white",
            graph,
            overlay,
            roots,
            RootCalcConfig(lcb_z=0.0, coverage_fold=fold, coverage_live_threshold=2),
        ).opening_score

    off, gate, gate_x_cov = score("off"), score("gate"), score("gate_x_cov")
    assert gate == pytest.approx(off)  # gate-alone leaks the node mastery
    assert gate > 0.0
    assert gate_x_cov == pytest.approx(0.0)  # child_cov=0 zeroes it


def test_weighted_depth_shifts_under_global_lcb_swap():
    # weighted_depth = mastery*(1+γ*depth_sum) is mastery-driven, so the global LCB
    # swap lowers it too (an easy-to-miss consequence, hence explicit coverage).
    root, e4, e5 = _positions(["e2e4", "e7e5"])
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, e4, "e2e4")
    overlay.nodes[root] = _quality(root, 1.0, count=1)
    overlay.nodes[e5] = _quality(e5, 1.0, count=1)

    depth0 = compute_root_score(
        root, "white", graph, overlay, roots, RootCalcConfig(lcb_z=0.0)
    ).weighted_depth
    depth1 = compute_root_score(
        root, "white", graph, overlay, roots, RootCalcConfig(lcb_z=1.0)
    ).weighted_depth
    assert depth1 < depth0


def test_position_score_hard_zero_for_all_gate_failing_opponent_turn():
    # compute_position_scores persists opening_score=0.0 with has_evidence=True for
    # an opponent-turn position whose reply subtrees ALL fail the coverage gate —
    # a genuine hard zero, DISTINCT from the None / has_evidence=False no-data path.
    root, opp, user_leaf = _positions(["e2e4", "e7e5"])  # opp=after e4 (black to move)
    off_book = "8/8/8/8/8/8/4k3/4K3 b - -"  # not in graph, no evidence below
    graph = _graph([["e2e4", "e7e5"]])
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    # Thin evidence at the reply: subtree live=1, review=0 fails BOTH gate clauses.
    overlay.nodes[user_leaf] = NodeEvidence(
        fen=user_leaf, quality_sum=0.5, quality_count=1, live_attempts=1
    )
    overlay.edges[(root, off_book)] = EdgeEvidence(root, off_book, "observed")

    calc = _shared_calc(
        graph,
        overlay,
        roots,
        RootCalcConfig(lcb_z=0.0, coverage_fold="gate", coverage_live_threshold=2),
    )
    rows = {row.normalized_fen: row for row in calc.compute_position_scores()}

    # The all-gate-failing opponent-turn position: real hard zero, evidence present.
    assert rows[opp].has_evidence is True
    assert rows[opp].opening_score == pytest.approx(0.0)
    # The no-data path is untouched: None score, has_evidence False.
    assert rows[off_book].has_evidence is False
    assert rows[off_book].opening_score is None


# ---------------------------------------------------------------------------
# Report-fold config surface + fingerprint compatibility (g-report-cfg-fp).
#
# The three dormant axes (report_fold_p / report_fold_scope / report_self_term) are
# validated and first-class, but MUST NOT perturb any pre-existing config
# fingerprint at their identity defaults. These GOLDEN hashes are pinned literals:
# a change here is a cache-invalidating fingerprint change and must be intentional.
# ---------------------------------------------------------------------------

# root_calc_config_fingerprint(RootCalcConfig()) — the production default config.
GOLDEN = "7ca0d6541f2fcf372b7548e0e4caead118547335d424a9359fd5089706fcd262"
# root_calc_config_fingerprint(RootCalcConfig(lcb_z=0.0, coverage_fold="off")) — the
# historical pre-readiness baseline cell.
BASELINE_GOLDEN = "7dd8d067f55f88c26c150c192203bd58de57762aadaf43ab8b6752e3fa6b1bde"


def test_report_fold_axes_have_identity_defaults():
    config = RootCalcConfig()
    assert config.report_fold_p == 0.0
    assert config.report_fold_scope == "all"
    assert config.report_self_term == "keep"
    # The contract id ships at v1 (the dormant axes are config-captured).
    assert REPORT_SCORER_CONTRACT_ID == "report-fold-v1"
    assert REPORT_FOLD_SCOPES == frozenset({"all", "user"})
    assert REPORT_SELF_TERM_MODES == frozenset({"keep", "drop_user"})


def test_config_fingerprint_pins_golden_hashes():
    # Both goldens are byte-stable after adding the dormant axes: the identity
    # config and the historical baseline cell hash exactly as before Phase 1a.
    assert root_calc_config_fingerprint() == GOLDEN
    assert root_calc_config_fingerprint(RootCalcConfig()) == GOLDEN
    assert (
        root_calc_config_fingerprint(RootCalcConfig(lcb_z=0.0, coverage_fold="off"))
        == BASELINE_GOLDEN
    )


def test_config_fingerprint_omits_inert_report_fold_fields():
    # At p == 0 the fold is off, so report_fold_p and report_fold_scope select
    # nothing: an identity-config scope/p change stays on GOLDEN. Signed and int zero
    # canonicalize to the same +0.0.
    assert root_calc_config_fingerprint(RootCalcConfig(report_fold_scope="user")) == GOLDEN
    assert root_calc_config_fingerprint(RootCalcConfig(report_fold_p=-0.0)) == GOLDEN
    assert root_calc_config_fingerprint(RootCalcConfig(report_fold_p=0)) == GOLDEN
    assert (
        root_calc_config_fingerprint(
            RootCalcConfig(report_fold_p=0.0, report_fold_scope="user")
        )
        == GOLDEN
    )


def test_config_fingerprint_moves_for_active_axes():
    # Active p and drop_user each move the fingerprint off GOLDEN; the two active
    # scopes differ from each other.
    active_all = root_calc_config_fingerprint(
        RootCalcConfig(report_fold_p=0.5, report_fold_scope="all")
    )
    active_user = root_calc_config_fingerprint(
        RootCalcConfig(report_fold_p=0.5, report_fold_scope="user")
    )
    assert active_all != GOLDEN
    assert active_user != GOLDEN
    assert active_all != active_user

    drop_user = root_calc_config_fingerprint(RootCalcConfig(report_self_term="drop_user"))
    assert drop_user != GOLDEN


def test_config_fingerprint_scope_inert_when_only_drop_user_active():
    # drop_user moves the fingerprint, but with p == 0 the scope is still inert: two
    # drop_user configs differing only in scope share one fingerprint.
    drop_all = root_calc_config_fingerprint(
        RootCalcConfig(report_self_term="drop_user", report_fold_scope="all")
    )
    drop_user_scope = root_calc_config_fingerprint(
        RootCalcConfig(report_self_term="drop_user", report_fold_scope="user")
    )
    assert drop_all != GOLDEN
    assert drop_all == drop_user_scope


def test_config_canonicalizes_report_fold_p_int_to_float():
    # Accepted ints become floats so 0/0.0 and 1/1.0 share identity AND fingerprint.
    assert isinstance(RootCalcConfig(report_fold_p=1).report_fold_p, float)
    assert RootCalcConfig(report_fold_p=1).report_fold_p == 1.0
    assert root_calc_config_fingerprint(
        RootCalcConfig(report_fold_p=1)
    ) == root_calc_config_fingerprint(RootCalcConfig(report_fold_p=1.0))


@pytest.mark.parametrize("bad_p", [True, False])
def test_config_rejects_bool_report_fold_p(bad_p):
    # bool is a real int in Python; reject it before int→float would accept True as 1.
    with pytest.raises(TypeError):
        RootCalcConfig(report_fold_p=bad_p)


@pytest.mark.parametrize("bad_p", ["0.5", 1j, None, [0.5]])
def test_config_rejects_non_real_report_fold_p(bad_p):
    with pytest.raises(TypeError):
        RootCalcConfig(report_fold_p=bad_p)


@pytest.mark.parametrize("bad_p", [float("nan"), float("inf"), float("-inf"), -0.5, -1])
def test_config_rejects_non_finite_or_negative_report_fold_p(bad_p):
    with pytest.raises(ValueError):
        RootCalcConfig(report_fold_p=bad_p)


@pytest.mark.parametrize("bad_p", [10**400, -(10**400)])
def test_config_rejects_out_of_range_int_report_fold_p(bad_p):
    # An int too large to represent as a float overflows float(p); it must surface as
    # the promised ValueError (either sign), not a raw OverflowError.
    with pytest.raises(ValueError):
        RootCalcConfig(report_fold_p=bad_p)


def test_config_rejects_unknown_report_fold_scope_and_self_term():
    with pytest.raises(ValueError):
        RootCalcConfig(report_fold_scope="bogus")
    with pytest.raises(ValueError):
        RootCalcConfig(report_self_term="bogus")
    for scope in REPORT_FOLD_SCOPES:
        assert RootCalcConfig(report_fold_scope=scope).report_fold_scope == scope
    for mode in REPORT_SELF_TERM_MODES:
        assert RootCalcConfig(report_self_term=mode).report_self_term == mode


def test_config_fingerprint_none_returns_default():
    assert root_calc_config_fingerprint(None) == root_calc_config_fingerprint(
        RootCalcConfig()
    )


class _NotAConfig:
    """A config look-alike that is NOT a RootCalcConfig (has the same fields)."""

    report_fold_p = 0.0
    report_fold_scope = "all"
    report_self_term = "keep"


@pytest.mark.parametrize("bad", [0, 0.0, "", False, [], {}, _NotAConfig()])
def test_config_fingerprint_rejects_non_config_with_typeerror(bad):
    # Falsy values used to slip through the old ``config or RootCalcConfig()`` and be
    # silently treated as the default; the explicit None branch now rejects every
    # non-RootCalcConfig (falsy included) with TypeError. (Raw GridCell rejection is
    # covered in test_calibrate_opening_scores.)
    with pytest.raises(TypeError):
        root_calc_config_fingerprint(bad)


# ---------------------------------------------------------------------------
# Report-time coverage fold (Option A, g-report-fold-score).
#
# _direct_metrics multiplies ONLY opening_score by coverage_fraction ** report_fold_p
# when the row is in scope and p is active. The coverage channel of _calc is
# unchanged; confidence, displayed coverage, and weighted_depth stay byte-identical.
# _legacy_direct_metrics preserves the pre-fold body verbatim as the p == 0 oracle.
# ---------------------------------------------------------------------------

FOLD_NOW = datetime(2026, 1, 3, tzinfo=timezone.utc)


def _legacy_direct_metrics(calc, fen):
    """The pre-fold ``_direct_metrics`` body, preserved verbatim as a p==0 oracle.

    At ``report_fold_p == 0`` the production method must return values byte-identical
    to this oracle across all four channels. Kept test-only so a future edit to the
    live scorer cannot silently move the p==0 baseline without a red test.
    """
    key = _normalized(fen)
    score, confidence, coverage, depth = calc._calc(key, False)
    perfect_score, perfect_confidence, _, _ = calc._calc(key, True)
    return (
        100.0 * score / perfect_score if perfect_score > 0 else 0.0,
        (
            100.0 * confidence / perfect_confidence
            if perfect_confidence > 0
            else 0.0
        ),
        100.0 * coverage,
        depth,
    )


def _fold_calc(graph, overlay, roots, config, *, color="white", now=None, seeds=None):
    return _SharedCalculator(
        color, graph, overlay, roots, config, now or FOLD_NOW, seeds=seeds
    )


def _fold_fixture():
    """Player white; one graph giving BOTH turns a fractional (0.5) coverage.

    ``root`` (white → USER turn) has a single prepared edge to ``opp`` (black →
    OPPONENT turn). ``opp`` has two book replies: ``covered`` (evidence-bearing, so
    locally covered) and an unprepared ``c5`` leaf (no evidence, uncovered). The 0/1
    coverage gate then makes ``opp`` exactly 50% covered, and ``root`` — whose one
    weighted child IS ``opp`` — inherits that 0.5. Both rows carry positive quality
    and non-zero confidence (fresh live evidence), so the fold has real channels to
    isolate. Returns ``(graph, overlay, roots, root, opp, covered)``.
    """
    root, opp = _positions(["e2e4"])
    covered = _positions(["e2e4", "e7e5"])[2]
    graph = _graph([["e2e4", "e7e5"], ["e2e4", "c7c5"]])
    roots = _roots(_root(root, "Root"), _root(opp, "Opp"))
    overlay = EvidenceOverlay(1, "white")
    _prepared(overlay, root, opp, "e2e4")
    overlay.nodes[root] = _quality(root, 2.0, count=2, at=FOLD_NOW)
    overlay.nodes[covered] = _quality(covered, 4.0, count=4, at=FOLD_NOW)
    return graph, overlay, roots, root, opp, covered


@pytest.mark.parametrize(
    "config",
    [RootCalcConfig(), RootCalcConfig(lcb_z=0.0, coverage_fold="off")],
    ids=["default", "historical-baseline"],
)
def test_report_fold_p0_matches_legacy_oracle(config):
    # p == 0 is byte-identical to the pre-fold scorer on ALL four channels, for both
    # the production default and the historical baseline, across user-turn rows,
    # opponent-turn rows, AND the shared position-row funnel.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    calc = _fold_calc(graph, overlay, roots, config)

    assert active_color(root) == "white"  # user turn
    assert active_color(opp) == "black"  # opponent turn
    for fen in (root, opp, covered):
        assert calc._direct_metrics(fen) == _legacy_direct_metrics(calc, fen)

    # Position rows run the same _direct_metrics; every scored row matches the oracle.
    rows = _position_rows(calc)
    scored = [row for row in rows.values() if row.has_evidence]
    assert scored  # the fixture yields at least one scored row
    for fen, row in rows.items():
        if not row.has_evidence:
            continue
        q, c, cov, d = _legacy_direct_metrics(calc, fen)
        assert (row.opening_score, row.confidence, row.coverage, row.weighted_depth) == (
            q,
            c,
            cov,
            d,
        )


def test_report_fold_uses_raw_fraction_not_percent():
    # The multiplier is coverage_fraction ** p (raw 0..1), NOT the displayed percent.
    # At 50% coverage the raw fold SHRINKS the score; a percent fold (50 ** p) would
    # blow it up by orders of magnitude.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    base = _fold_calc(graph, overlay, roots, RootCalcConfig())
    quality, _, cov_pct, _ = base._direct_metrics(root)
    frac = cov_pct / 100.0
    assert frac == pytest.approx(0.5)  # displayed 50%, raw fraction 0.5
    assert quality > 0.0

    p = 2.0
    active = _fold_calc(
        graph, overlay, roots, RootCalcConfig(report_fold_p=p, report_fold_scope="all")
    )
    folded, _, _, _ = active._direct_metrics(root)
    assert folded == pytest.approx(quality * frac**p)
    assert folded < quality
    # The percent reading would give a wildly larger number; pin that we are NOT it.
    assert folded != pytest.approx(quality * cov_pct**p)


@pytest.mark.parametrize("p", [0.5, 1.0, 2.0])
@pytest.mark.parametrize(
    "scope,fen_key,in_scope",
    [
        ("all", "root", True),
        ("all", "opp", True),
        ("user", "root", True),
        ("user", "opp", False),
    ],
)
def test_report_fold_isolates_opening_score(p, scope, fen_key, in_scope):
    # Only opening_score moves, and only for in-scope rows. Confidence, displayed
    # coverage, and weighted_depth are byte-identical to p == 0 regardless of scope.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    fen = {"root": root, "opp": opp}[fen_key]

    base = _fold_calc(graph, overlay, roots, RootCalcConfig())
    quality, confidence, cov_pct, depth = base._direct_metrics(fen)
    frac = cov_pct / 100.0
    # Every fixture row is genuinely fractional and positive before we assert motion.
    assert 0.0 < frac < 1.0
    assert quality > 0.0

    active = _fold_calc(
        graph, overlay, roots, RootCalcConfig(report_fold_p=p, report_fold_scope=scope)
    )
    folded_q, folded_c, folded_cov, folded_d = active._direct_metrics(fen)

    # Non-score channels never move — same non-zero values as the unfolded row.
    assert (folded_c, folded_cov, folded_d) == (confidence, cov_pct, depth)
    if in_scope:
        assert folded_q == pytest.approx(quality * frac**p)
        assert folded_q < quality
    else:
        # Out of scope: effective multiplier 1.0, opening_score untouched.
        assert folded_q == quality


def test_report_fold_out_of_scope_multiplier_is_identity():
    # Semantic multiplier-1.0 check: a user-scope fold leaves the opponent-turn row
    # exactly at its p == 0 value even though that row IS fractionally covered.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    base = _fold_calc(graph, overlay, roots, RootCalcConfig())
    dormant = base._direct_metrics(opp)
    assert 0.0 < dormant[2] / 100.0 < 1.0  # fractional, so a fold WOULD have moved it

    user_scope = _fold_calc(
        graph,
        overlay,
        roots,
        RootCalcConfig(report_fold_p=1.5, report_fold_scope="user"),
    )
    assert user_scope._direct_metrics(opp) == dormant


def test_report_fold_invalid_active_coverage_fails_closed():
    # An active, in-scope row with a coverage fraction outside [0, 1] raises rather
    # than returning a complex/NaN score. The FEN and the offending value are named.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    key = _normalized(root)

    for bad in (1.0 + 1e-9, 1.5, -0.1):
        active = _fold_calc(
            graph,
            overlay,
            roots,
            RootCalcConfig(report_fold_p=0.5, report_fold_scope="all"),
        )
        # Poison the shared memo so _calc surfaces an out-of-range coverage fraction.
        active._metrics[(key, False)] = (0.6, 0.0, bad, 0.5)
        active._metrics[(key, True)] = (1.0, 0.0, 1.0, 0.5)
        with pytest.raises(ValueError, match=repr(key)):
            active._direct_metrics(root)

        # p == 0 never evaluates the power or validates coverage: same poison passes.
        dormant = _fold_calc(graph, overlay, roots, RootCalcConfig())
        dormant._metrics[(key, False)] = (0.6, 0.0, bad, 0.5)
        dormant._metrics[(key, True)] = (1.0, 0.0, 1.0, 0.5)
        _, _, cov, _ = dormant._direct_metrics(root)
        assert cov == pytest.approx(100.0 * bad)

    # An out-of-scope active row also skips validation entirely (never powers).
    opp_key = _normalized(opp)
    out_of_scope = _fold_calc(
        graph,
        overlay,
        roots,
        RootCalcConfig(report_fold_p=0.5, report_fold_scope="user"),
    )
    out_of_scope._metrics[(opp_key, False)] = (0.6, 0.0, 1.5, 0.3)
    out_of_scope._metrics[(opp_key, True)] = (1.0, 0.0, 1.0, 0.3)
    _, _, cov, _ = out_of_scope._direct_metrics(opp)
    assert cov == pytest.approx(150.0)


def test_report_fold_named_root_and_position_row_agree():
    # Named-root and position rows share _direct_metrics, so the fold reaches both
    # identically — and it genuinely bites (the folded root scores below its p==0 row).
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    config = RootCalcConfig(report_fold_p=0.75, report_fold_scope="all")

    named = compute_root_score(root, "white", graph, overlay, roots, config, now=FOLD_NOW)
    rows = _position_rows(_fold_calc(graph, overlay, roots, config))
    row = rows[root]
    assert row.opening_score == pytest.approx(named.opening_score)
    assert row.confidence == pytest.approx(named.confidence)
    assert row.coverage == pytest.approx(named.coverage)
    assert row.weighted_depth == pytest.approx(named.weighted_depth)

    unfolded = compute_root_score(
        root, "white", graph, overlay, roots, RootCalcConfig(), now=FOLD_NOW
    )
    assert named.opening_score < unfolded.opening_score


# ---------------------------------------------------------------------------
# B1 report_self_term="drop_user" pre-fold quality (g-drop-user-score).
#
# For a reported user-turn row with a nonempty prepared-child weight set and a
# positive child perfect denominator, _direct_metrics scores the row by the
# CHILD-ONLY ratio 100 * sum(w * child_natural) / sum(w * child_perfect) instead
# of the ordinary aggregate node ratio — the node's own mastery self-term is
# dropped. It changes ONLY opening_score; confidence/coverage/weighted_depth and
# _calc's recursion are untouched. Opponent turns, user leaves, empty prepared-
# child sets, and non-positive child denominators all keep the ordinary ratio.
# Orthogonal to, and composed exactly once with, the report_fold_p coverage fold.
#
# _fold_fixture (above) gives root a single prepared child (opp) with a positive
# perfect denominator — the qualifying user-turn shape — while opp is an opponent
# turn and `covered` is a user-turn leaf, so one fixture exercises the arm and two
# of its fallbacks at once.
# ---------------------------------------------------------------------------


def test_drop_user_moves_only_score_vs_keep():
    # keep vs drop_user on identical evidence: confidence, displayed coverage, and
    # weighted_depth are byte-identical on EVERY row; only opening_score may move, and
    # only for the qualifying user-turn row.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    keep = _fold_calc(graph, overlay, roots, RootCalcConfig())  # report_self_term="keep"
    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))

    assert active_color(root) == "white"  # user turn, prepared child → arm applies
    assert active_color(opp) == "black"  # opponent turn → ordinary ratio
    for fen in (root, opp, covered):
        k = keep._direct_metrics(fen)
        d = drop._direct_metrics(fen)
        # Non-score channels never move.
        assert (d[1], d[2], d[3]) == (k[1], k[2], k[3])

    # The user-turn row with prepared children and a positive child denominator is the
    # only score that changes; it drops the mastery self-term so it reads differently.
    assert drop._direct_metrics(root)[0] != keep._direct_metrics(root)[0]
    # Opponent turn and the user leaf keep the ordinary ratio exactly.
    assert drop._direct_metrics(opp)[0] == keep._direct_metrics(opp)[0]
    assert drop._direct_metrics(covered)[0] == keep._direct_metrics(covered)[0]


def test_drop_user_selects_child_only_ratio():
    # The qualifying user-turn score equals the child-only ratio computed independently
    # from _get_weights + memoized _calc — NOT algebraically recovered from the node
    # score, and NOT the ordinary node ratio.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    key = _normalized(root)

    base = _fold_calc(graph, overlay, roots, RootCalcConfig())
    weights = base._get_weights(key)
    assert weights  # nonempty prepared-child set
    child_natural = sum(w * base._calc(c, False)[0] for c, w in weights.items())
    child_perfect = sum(w * base._calc(c, True)[0] for c, w in weights.items())
    assert child_perfect > 0.0
    child_only = 100.0 * child_natural / child_perfect

    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))
    dropped_q = drop._direct_metrics(root)[0]
    assert dropped_q == pytest.approx(child_only)
    # It is genuinely the child-only ratio, not the ordinary aggregate node ratio.
    ordinary_q = base._direct_metrics(root)[0]
    assert dropped_q != pytest.approx(ordinary_q)


def test_drop_user_opponent_turn_keeps_ordinary_ratio():
    # Opponent-turn rows always retain the ordinary ratio, even though opp HAS a
    # nonempty weight set (so the user-turn arm would have fired on the same shape).
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    assert active_color(opp) == "black"
    keep = _fold_calc(graph, overlay, roots, RootCalcConfig())
    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))
    assert drop._get_weights(_normalized(opp))  # not vacuously empty
    assert drop._direct_metrics(opp) == keep._direct_metrics(opp)


def test_drop_user_user_leaf_falls_back_to_ordinary():
    # Fallback shape 1: a user-turn LEAF has an empty weight set, so drop_user falls
    # back to the ordinary node ratio (== keep).
    fen = _positions([])[0]
    assert active_color(fen) == "white"  # user turn
    graph = _graph([[]])  # no children → leaf
    roots = _roots(_root(fen))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[fen] = _quality(fen, 1.5, count=3, at=FOLD_NOW)

    keep = _fold_calc(graph, overlay, roots, RootCalcConfig())
    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))
    assert not drop._get_weights(_normalized(fen))  # empty because leaf
    assert drop._direct_metrics(fen) == keep._direct_metrics(fen)


def test_drop_user_no_prepared_children_falls_back_to_ordinary():
    # Fallback shape 2: a user-turn row with a STRUCTURAL child but no prepared edge has
    # an empty weight set (distinct from a leaf), so drop_user falls back to ordinary.
    root, opp = _positions(["e2e4"])
    assert active_color(root) == "white"  # user turn
    graph = _graph([["e2e4", "e7e5"]])  # root has a structural child (opp)...
    roots = _roots(_root(root))
    overlay = EvidenceOverlay(1, "white")
    # ...but NO _prepared() call and no ghost target → the edge is not prepared.
    overlay.nodes[root] = _quality(root, 1.0, count=1, at=FOLD_NOW)

    keep = _fold_calc(graph, overlay, roots, RootCalcConfig())
    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))
    key = _normalized(root)
    assert drop._structural_children(key)  # NOT a leaf — it has a structural child
    assert not drop._get_weights(key)  # ...yet the prepared-child weight set is empty
    assert drop._direct_metrics(root) == keep._direct_metrics(root)


def test_drop_user_nonpositive_child_denominator_falls_back():
    # Fallback shape 3: nonempty prepared-child weights but child_perfect_sum == 0. A
    # narrow perfect-child _calc stub (poisoned memo) forces the zero denominator; the
    # row must fall back to the ordinary node ratio rather than dividing by zero.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    drop = _fold_calc(graph, overlay, roots, RootCalcConfig(report_self_term="drop_user"))
    key = _normalized(root)

    weights = drop._get_weights(key)
    assert weights  # nonempty prepared-child set (so we reach the denominator guard)
    # Stub _calc via the shared memo: node ratio 100 * 0.42 / 1.4 = 30.0, and every
    # prepared child returns a POSITIVE natural score but a ZERO perfect score, so
    # child_perfect_sum collapses to 0 while child_natural_sum stays positive.
    drop._metrics[(key, False)] = (0.42, 0.0, 1.0, 0.7)
    drop._metrics[(key, True)] = (1.4, 0.0, 1.0, 0.7)
    for child in weights:
        drop._metrics[(child, False)] = (0.9, 0.0, 1.0, 0.0)
        drop._metrics[(child, True)] = (0.0, 0.0, 1.0, 0.0)

    q, _, _, _ = drop._direct_metrics(root)
    assert q == pytest.approx(100.0 * 0.42 / 1.4)  # ordinary node ratio, not 0/0


def test_drop_user_composes_with_fold_exactly_once():
    # Combined axis: p > 0, drop_user, 0 < coverage < 1. The reported score equals the
    # INDEPENDENTLY computed child-only quality * coverage ** p, folded exactly once —
    # not the ordinary ratio, not folded twice, verified without any debug field.
    graph, overlay, roots, root, opp, covered = _fold_fixture()
    p = 1.5
    key = _normalized(root)

    base = _fold_calc(graph, overlay, roots, RootCalcConfig())
    _, base_conf, cov_pct, base_depth = base._direct_metrics(root)
    frac = cov_pct / 100.0
    assert 0.0 < frac < 1.0  # a real fractional coverage the fold can bite

    # Child-only quality from primitives (drop_user/fold never touch _calc or weights).
    weights = base._get_weights(key)
    child_natural = sum(w * base._calc(c, False)[0] for c, w in weights.items())
    child_perfect = sum(w * base._calc(c, True)[0] for c, w in weights.items())
    assert child_perfect > 0.0
    child_only = 100.0 * child_natural / child_perfect

    active = _fold_calc(
        graph,
        overlay,
        roots,
        RootCalcConfig(
            report_fold_p=p, report_fold_scope="all", report_self_term="drop_user"
        ),
    )
    folded_q, folded_conf, folded_cov, folded_depth = active._direct_metrics(root)

    # Folded exactly once over the child-only quality.
    assert folded_q == pytest.approx(child_only * frac**p)
    # Not folded twice (would be child_only * frac ** (2p), strictly smaller here).
    assert folded_q != pytest.approx(child_only * frac ** (2 * p))
    # Not the keep-ratio folded: the self-term arm genuinely moved the numerator.
    ordinary_folded = base._direct_metrics(root)[0] * frac**p
    assert folded_q != pytest.approx(ordinary_folded)
    # The fold + self-term still touch only opening_score.
    assert (folded_conf, folded_cov, folded_depth) == (base_conf, cov_pct, base_depth)
