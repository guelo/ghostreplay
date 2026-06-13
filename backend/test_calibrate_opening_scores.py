"""Tests for the opening-score v2 calibration script.

Fixture-driven and DB-free: the statistics, URL guard, in-memory scoring,
report assembly, JSON output, filtering, and the no-write default are all
exercised without a live database.
"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import chess
import pytest

from app.db import DATABASE_URL
from app.fen import active_color, normalize_fen
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence, PhaseSample
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import CalcTelemetry
from app.opening_roots import OpeningRoot, OpeningRoots

import scripts.calibrate_opening_scores_v2 as cal


# --- in-memory fixtures (mirrors test_opening_rootcalc helpers) -------------


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


def _root(fen: str, name: str = "Test") -> OpeningRoot:
    return OpeningRoot(
        opening_key=fen,
        opening_name=name,
        opening_family="Test Family",
        eco=None,
        depth=0,
        parent_keys=frozenset(),
        child_keys=frozenset(),
    )


def _roots(*items: OpeningRoot) -> OpeningRoots:
    return OpeningRoots(
        {item.opening_key: item for item in items},
        {item.opening_key: frozenset([item.opening_key]) for item in items},
    )


_MIDGAME_FEN = "8/8/8/4k3/4K3/8/8/8 w - -"


def _scored_overlay() -> tuple[OpeningGraph, EvidenceOverlay, OpeningRoots, str]:
    root, e4 = _positions(["e2e4"])
    graph = _graph([["e2e4"]])
    roots = _roots(_root(e4, "King's Pawn"), _root(_MIDGAME_FEN, "Bare kings"))
    overlay = EvidenceOverlay(1, "white")
    overlay.edges[(root, e4)] = EdgeEvidence(root, e4, "e2e4", live_attempts=2)
    overlay.nodes[e4] = NodeEvidence(fen=e4, quality_sum=0.8, quality_count=3)
    overlay.source_counts["session_eval"] = 3
    overlay.phase_samples.append(PhaseSample(opening_interval_len=4, middle_ply=None, end_ply=None))
    return graph, overlay, roots, e4


# --- pure statistics --------------------------------------------------------


class TestStats:
    def test_percentile_interpolates_and_handles_edges(self):
        assert cal.percentile([], 50.0) is None
        assert cal.percentile([7.0], 50.0) == 7.0
        assert cal.percentile([0.0, 100.0], 50.0) == pytest.approx(50.0)
        assert cal.percentile([0.0, 10.0, 20.0, 30.0, 40.0], 25.0) == pytest.approx(10.0)

    def test_histogram_buckets_including_closed_top(self):
        # 100.0 lands in the final bucket rather than falling off the end.
        assert cal.histogram([0.0, 19.9, 20.0, 80.0, 100.0]) == [2, 1, 0, 0, 2]

    def test_source_mix_zero_denominator_guarded(self):
        assert cal.source_mix(Counter()) == {"total": 0, "pct": {}}

    def test_source_mix_percentages(self):
        mix = cal.source_mix(Counter({"session_eval": 3, "eval_delta": 1}))
        assert mix["total"] == 4
        assert mix["pct"]["session_eval"] == pytest.approx(75.0)
        assert mix["pct"]["eval_delta"] == pytest.approx(25.0)

    def test_summarize_empty(self):
        s = cal.summarize([])
        assert s["count"] == 0
        assert s["mean"] is None
        assert s["histogram"] == [0, 0, 0, 0, 0]


# --- write-bench URL guard --------------------------------------------------


class TestWriteBenchGuard:
    def test_rejects_production_url(self):
        with pytest.raises(ValueError):
            cal.validate_write_bench_database_url(DATABASE_URL, DATABASE_URL)

    def test_rejects_unguarded_url(self):
        with pytest.raises(ValueError):
            cal.validate_write_bench_database_url(
                "postgresql+psycopg://h:5432/ghostreplay", "postgresql://other/db"
            )

    def test_accepts_sqlite_under_tmp(self):
        url = "sqlite:////repo/backend/.tmp/calib.db"
        assert cal.validate_write_bench_database_url(url, "postgresql://prod/db") == url

    def test_accepts_calibrate_database_name(self):
        url = "postgresql+psycopg://h:5432/ghostreplay_calibrate"
        assert cal.validate_write_bench_database_url(url, "postgresql://prod/db") == url


# --- in-memory scoring ------------------------------------------------------


class TestScoreOverlay:
    def test_empty_overlay_populates_zero_telemetry(self):
        graph = _graph([["e2e4"]])
        roots = _roots(_root(_positions(["e2e4"])[1]), _root(_MIDGAME_FEN))
        overlay = EvidenceOverlay(1, "white")  # no quality observations

        result = cal.score_overlay(1, "white", graph, overlay, roots)

        assert result.named_scores == []
        assert result.synthetic_score is None
        assert result.observation_total == 0
        # Telemetry is well-formed zeros + named-root count on the early return.
        assert result.telemetry.named_root_count == 2
        assert result.telemetry.actual_key_count == 0
        assert result.telemetry.perfect_key_count == 0
        assert result.telemetry.unscored_root_count == 2

    def test_synthetic_row_separated_from_named(self):
        graph, overlay, roots, _e4 = _scored_overlay()

        result = cal.score_overlay(1, "white", graph, overlay, roots)

        # The synthetic hero row is reported in its own field, not the named list.
        assert result.synthetic_score is not None
        assert 0.0 <= result.synthetic_score <= 100.0
        assert result.named_scores  # the named King's Pawn root scored
        assert result.emitted_row_count == len(result.named_scores) + 1
        # raw-middlegame and unscored counts stay distinct.
        assert result.telemetry.raw_middlegame_root_count == 1
        assert result.telemetry.actual_key_count == result.telemetry.perfect_key_count
        assert result.telemetry.actual_key_count > 0

    def test_passes_include_synthetic_root_true(self):
        graph, overlay, roots, _e4 = _scored_overlay()
        with patch.object(
            cal, "compute_all_root_scores", wraps=cal.compute_all_root_scores
        ) as spy:
            cal.score_overlay(1, "white", graph, overlay, roots)
        _args, kwargs = spy.call_args
        assert kwargs["include_synthetic_root"] is True
        assert isinstance(kwargs["telemetry"], CalcTelemetry)


# --- report assembly --------------------------------------------------------


def _pair_score(user_id, color, named, synthetic, obs, **kw):
    tel = kw.pop("telemetry", CalcTelemetry(named_root_count=10, actual_key_count=5,
                                            perfect_key_count=5, calculation_misses=10,
                                            raw_middlegame_root_count=2,
                                            unscored_root_count=3))
    return cal.PairScore(
        user_id=user_id,
        player_color=color,
        named_scores=named,
        synthetic_score=synthetic,
        observation_total=obs,
        source_counts=kw.pop("source_counts", Counter({"session_eval": obs})),
        excluded_sessions=kw.pop("excluded_sessions", 0),
        phase_samples=kw.pop("phase_samples", []),
        telemetry=tel,
        scoring_seconds=kw.pop("scoring_seconds", 0.01),
    )


class TestBuildReport:
    def test_cohort_split_on_min_observations(self):
        included = _pair_score(1, "white", [60.0, 70.0, 80.0], 65.0, 30)
        below = _pair_score(2, "black", [40.0], 40.0, 5)
        report = cal.build_report(
            [included, below], min_observations=20, named_root_count=10
        )
        assert report["cohort"]["included_pairs"] == 1
        excluded = report["cohort"]["excluded_low_evidence_pairs"]
        assert len(excluded) == 1
        assert excluded[0]["user_id"] == 2
        # The low-evidence pair is excluded from the pooled distribution.
        assert report["named_score_distribution"]["pooled"]["count"] == 3

    def test_synthetic_reported_separately(self):
        p = _pair_score(1, "white", [50.0, 60.0], 55.0, 30)
        report = cal.build_report([p], min_observations=0, named_root_count=10)
        assert report["synthetic_hero_distribution"]["count"] == 1
        assert report["synthetic_hero_distribution"]["mean"] == pytest.approx(55.0)
        # Pooled named distribution excludes the synthetic row.
        assert report["named_score_distribution"]["pooled"]["count"] == 2

    def test_raw_middlegame_and_unscored_reported_distinctly(self):
        p = _pair_score(1, "white", [50.0], 55.0, 30)
        report = cal.build_report([p], min_observations=0, named_root_count=10)
        horizon = report["horizon"]
        assert horizon["raw_middlegame_root_count"] == 2
        assert horizon["unscored_root_count"]["max"] == 3.0

    def test_recursion_keys_reported_per_class(self):
        p = _pair_score(1, "white", [50.0], 55.0, 30)
        report = cal.build_report([p], min_observations=0, named_root_count=10)
        rec = report["recursion"]
        assert rec["named_root_count"] == 10
        assert rec["actual_key_count"]["mean"] == 5.0
        assert rec["perfect_key_count"]["mean"] == 5.0

    def test_zero_denominator_source_mix(self):
        p = _pair_score(1, "white", [], None, 0, source_counts=Counter())
        report = cal.build_report([p], min_observations=0, named_root_count=10)
        assert report["source_mix"] == {"total": 0, "pct": {}}

    def test_report_is_json_serializable(self):
        p = _pair_score(1, "white", [50.0, 60.0], 55.0, 30)
        report = cal.build_report([p], min_observations=0, named_root_count=10)
        # Round-trips cleanly for --json (default=str covers any stray types).
        assert json.loads(json.dumps(report, default=str))

    def test_below_threshold_pair_telemetry_still_surfaces(self):
        # Score distributions exclude low-evidence pairs, but their well-formed
        # early-return telemetry must still surface (raw-middlegame roots, source
        # mix, recursion counters) even when the included cohort is empty.
        below = _pair_score(
            1,
            "white",
            [],
            None,
            5,
            source_counts=Counter({"session_eval": 5}),
            telemetry=CalcTelemetry(
                named_root_count=10,
                actual_key_count=3,
                perfect_key_count=3,
                calculation_misses=6,
                raw_middlegame_root_count=4,
                unscored_root_count=8,
            ),
        )
        report = cal.build_report([below], min_observations=20, named_root_count=10)

        assert report["cohort"]["included_pairs"] == 0
        assert report["named_score_distribution"]["pooled"]["count"] == 0
        # Telemetry aggregates over all candidate pairs, not just the included ones.
        assert report["source_mix"]["total"] == 5
        assert report["horizon"]["raw_middlegame_root_count"] == 4
        assert report["recursion"]["actual_key_count"]["mean"] == 3.0
        assert report["recursion"]["calculation_misses"]["mean"] == 6.0

    def test_per_pair_distribution_preserves_shape(self):
        p1 = _pair_score(1, "white", [10.0, 50.0, 90.0], 50.0, 30)
        p2 = _pair_score(2, "black", [60.0, 70.0], 65.0, 30)
        report = cal.build_report([p1, p2], min_observations=0, named_root_count=10)

        per_pair = report["named_score_distribution"]["per_pair"]
        assert len(per_pair) == 2
        first = next(e for e in per_pair if e["user_id"] == 1)
        # Full per-pair shape is retained, not just the median.
        assert first["summary"]["count"] == 3
        assert first["summary"]["percentiles"]["p50"] == pytest.approx(50.0)
        assert first["summary"]["percentiles"]["p95"] == pytest.approx(86.0)

    def test_per_pair_latency_and_gates_reported(self):
        fast = _pair_score(1, "white", [50.0], 50.0, 30, scoring_seconds=0.5)
        slow = _pair_score(2, "black", [60.0], 60.0, 30, scoring_seconds=2.0)
        report = cal.build_report([fast, slow], min_observations=0, named_root_count=10)

        latency = report["throughput"]["scoring_seconds_per_pair"]
        assert latency["max"] == pytest.approx(2.0)
        assert latency["median"] == pytest.approx(1.25)
        gates = report["gates"]
        assert gates["scoring_latency_pass"] is True  # 2.0 < 5.0
        # No write-bench → cache-read gate is not applicable.
        assert gates["cache_read_pass"] is None

    def test_scoring_latency_gate_fails_when_a_pair_is_slow(self):
        slow = _pair_score(1, "white", [50.0], 50.0, 30, scoring_seconds=6.0)
        report = cal.build_report([slow], min_observations=0, named_root_count=10)
        assert report["gates"]["scoring_latency_pass"] is False

    def test_cache_read_gate_from_write_bench(self):
        p = _pair_score(1, "white", [50.0], 50.0, 30)
        report = cal.build_report(
            [p],
            min_observations=0,
            named_root_count=10,
            write_bench={"cache_read_ms": 4.5},
        )
        assert report["gates"]["cache_read_pass"] is True


# --- filtering --------------------------------------------------------------


class TestFiltering:
    def test_parse_user_filter(self):
        assert cal._parse_user_filter(None) is None
        assert cal._parse_user_filter("1, 2,3") == {1, 2, 3}

    def test_parse_pair_filter(self):
        assert cal._parse_pair_filter(None) is None
        assert cal._parse_pair_filter("1:white, 2:black") == {(1, "white"), (2, "black")}

    def test_select_pairs_applies_filters(self):
        candidates = [(1, "white"), (1, "black"), (2, "white")]
        assert cal.select_pairs(candidates, users={1}, pairs=None) == [
            (1, "white"),
            (1, "black"),
        ]
        assert cal.select_pairs(candidates, users=None, pairs={(2, "white")}) == [
            (2, "white")
        ]


# --- orchestration / no-write default --------------------------------------


@contextmanager
def _fake_session(_db):
    yield _db


def _fake_factory(db):
    def factory():
        return _fake_session(db)

    return factory


class TestMainNoWriteDefault:
    def test_default_run_never_calls_recompute(self, capsys):
        graph = _graph([["e2e4"]])
        overlay = EvidenceOverlay(7, "white")  # empty → early return, no writes
        db = MagicMock()

        with patch.object(cal, "overlay_evidence", return_value=overlay), patch(
            "app.opening_cache.list_opening_score_candidate_pairs",
            return_value=[(7, "white")],
        ), patch(
            "app.opening_cache.recompute_opening_scores"
        ) as recompute, patch.object(
            cal, "get_opening_graph", return_value=graph
        ), patch.object(
            cal, "get_opening_roots", return_value=_roots(_root(_positions(["e2e4"])[1]))
        ):
            report = cal.main(
                ["--min-observations", "0"], session_factory=_fake_factory(db)
            )

        recompute.assert_not_called()
        assert report["cohort"]["candidate_pairs"] == 1
        out = capsys.readouterr().out
        assert "Opening score v2 calibration" in out

    def test_json_flag_emits_parseable_json(self, capsys):
        graph = _graph([["e2e4"]])
        db = MagicMock()
        with patch(
            "app.opening_cache.list_opening_score_candidate_pairs", return_value=[]
        ), patch.object(cal, "get_opening_graph", return_value=graph), patch.object(
            cal, "get_opening_roots", return_value=_roots(_root(_positions(["e2e4"])[1]))
        ):
            cal.main(["--json"], session_factory=_fake_factory(db))
        payload = json.loads(capsys.readouterr().out)
        assert payload["cohort"]["candidate_pairs"] == 0

    def test_write_bench_requires_allow_writes(self):
        with pytest.raises(SystemExit):
            cal.main(["--write-bench"], session_factory=_fake_factory(MagicMock()))

    def test_write_bench_rejects_production_url(self):
        with pytest.raises(ValueError):
            cal.main(
                ["--write-bench", "--allow-writes", "--database-url", DATABASE_URL],
                session_factory=_fake_factory(MagicMock()),
            )
