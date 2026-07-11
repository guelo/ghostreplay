"""Tests for the opening-score v2 calibration script.

Fixture-driven and DB-free: the statistics, URL guard, in-memory scoring,
report assembly, JSON output, filtering, and the no-write default are all
exercised without a live database.
"""
from __future__ import annotations

import json
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import chess
import pytest

from app.db import DATABASE_URL
from app.fen import active_color, normalize_fen
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence, PhaseSample
from app.opening_graph import OpeningGraph, OpeningGraphNode
from app.opening_rootcalc import (
    CalcTelemetry,
    NodeDebug,
    RootCalcConfig,
    RootScore,
    root_calc_config_fingerprint,
)
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


# --- calibration grid (g-p4ih): arm structure, roles, anchors --------------

_ALT_AS_OF = datetime(2030, 6, 1, tzinfo=timezone.utc)

_SIX_AXES = {
    "lcb_z",
    "coverage_fold",
    "coverage_live_threshold",
    "report_fold_p",
    "report_fold_scope",
    "report_self_term",
}


def _opponent_root_overlay():
    """Opponent node after 1.e4 (black to move) as the named root, so the LCB and
    the coverage gate actually move the score across grid cells: one covered reply
    (e5) and one unprepared reply (c5)."""
    opp = _positions(["e2e4"])[1]
    covered = _positions(["e2e4", "e7e5"])[2]
    graph = _graph([["e2e4", "e7e5"], ["e2e4", "c7c5"]])
    roots = _roots(_root(opp, "After e4"))
    overlay = EvidenceOverlay(1, "white")
    overlay.nodes[covered] = NodeEvidence(
        fen=covered, quality_sum=4.0, quality_count=4, live_attempts=4
    )
    return graph, overlay, roots, opp


def _is_demo_axes(cell_axes: dict) -> bool:
    return cell_axes == cal._cell_axes(cal.DEMO_GATE_UNIFORM_FOLD_CELL)


class TestReportFoldGridParsing:
    def test_default_only_from_none(self):
        # None (flag omitted) is the ONLY path to the default.
        assert cal.parse_report_fold_grid(None) == cal.REPORT_FOLD_P_GRID

    def test_valid_explicit_dedupes_order_preserving(self):
        assert cal.parse_report_fold_grid("0.5,0.5,0.25") == (0.5, 0.25)
        assert cal.parse_report_fold_grid("0.25, 0.5 ,0.75") == (0.25, 0.5, 0.75)

    @pytest.mark.parametrize(
        "raw", ["", "   ", ",", "0", "1.5", "-0.5", "nan", "inf", "bogus"]
    )
    def test_present_but_invalid_raises(self, raw):
        # An explicit "" / whitespace / "," is a HARD ERROR, never the default; each
        # out-of-domain / non-finite / non-numeric token raises ValueError.
        with pytest.raises(ValueError):
            cal.parse_report_fold_grid(raw)

    def test_parse_args_default_and_reject(self):
        # CLI layer: omitted flag -> default tuple; an invalid value -> SystemExit.
        args = cal._parse_args([])
        assert args.report_fold_p_grid == cal.REPORT_FOLD_P_GRID
        with pytest.raises(SystemExit):
            cal._parse_args(["--report-fold-grid", "0"])
        with pytest.raises(SystemExit):
            cal._parse_args(["--report-fold-grid", "1.5"])


class TestArmGrid:
    def test_exact_set_anchor_first_deduped_no_demo(self):
        grid = cal.build_arm_grid()
        cells = grid.cells
        expected = {
            cal.ORIGINAL_CELL,
            cal.CURRENT_SM_V2_3_CELL,
            *cal.arm1_cells(),
            *cal.arm2_cells(),
            cal.B1_CELL,
        }
        assert set(cells) == expected
        assert cal.DEMO_GATE_UNIFORM_FOLD_CELL not in cells
        # native identity IS the behavioral key (no dupes)
        assert len(cells) == len(set(cells))
        # anchors first
        assert cells[0] == cal.ORIGINAL_CELL
        assert cells[1] == cal.CURRENT_SM_V2_3_CELL
        # required_cells (derived from it) excludes every demo
        assert all(cell not in cal.DEMO_CELLS for cell in cells)

    def test_inert_axis_dedupe(self):
        # (a) p=0 scope="all" == p=0 scope="user" and hash-equal -> dedupe to one
        a = cal.GridCell(1.0, "gate", report_fold_p=0.0, report_fold_scope="all")
        b = cal.GridCell(1.0, "gate", report_fold_p=0.0, report_fold_scope="user")
        assert a == b and hash(a) == hash(b)
        assert len({a, b}) == 1
        # (b) p>0 scope differs
        c = cal.GridCell(1.0, "gate", report_fold_p=0.5, report_fold_scope="all")
        d = cal.GridCell(1.0, "gate", report_fold_p=0.5, report_fold_scope="user")
        assert c != d
        # (c) build_arm_grid never emits two cells with equal native identity
        assert len(cal.build_arm_grid().cells) == len(set(cal.build_arm_grid().cells))
        # (d) CURRENT built with a different scope literal still equals canonical CURRENT
        alt = cal.GridCell(
            1.0,
            "gate",
            coverage_live_threshold=1,
            report_fold_p=0.0,
            report_fold_scope="user",
            report_self_term="keep",
        )
        assert alt == cal.CURRENT_SM_V2_3_CELL

    def test_role_wrapper_merged_label(self):
        # builder-only p=0 path (CLI rejects 0): ARM-2 at p=0 canonicalizes onto CURRENT
        grid = cal.build_arm_grid(p_grid=(0.0, 0.5))
        assert grid.roles_by_cell[cal.CURRENT_SM_V2_3_CELL] == ("current", "arm2")
        assert len(grid.cells) == len(set(grid.cells))

    def test_canonical_roles_deterministic(self):
        assert cal._canonical_roles(["arm2", "current"]) == ("current", "arm2")
        assert cal._canonical_roles(["current", "arm2"]) == ("current", "arm2")
        assert cal._canonical_roles(["arm2", "arm2"]) == ("arm2",)
        with pytest.raises(KeyError):
            cal._canonical_roles(["bogus"])

    def test_role_serialization_stable_across_builds(self):
        run1 = cal.run_user14_diagnostic(cal.build_arm_grid())["rows"]
        run2 = cal.run_user14_diagnostic(cal.build_arm_grid())["rows"]
        # A set-derived tuple would reorder run-to-run; the canonical order is stable.
        assert [r["roles"] for r in run1] == [r["roles"] for r in run2]


class TestGridCellConfig:
    def test_all_six_axes_map_through_config(self):
        cell = cal.GridCell(
            lcb_z=1.28,
            coverage_fold="gate_x_cov",
            coverage_live_threshold=3,
            report_fold_p=0.5,
            report_fold_scope="user",
            report_self_term="drop_user",
        )
        cfg = cell.config
        assert cfg.lcb_z == 1.28
        assert cfg.coverage_fold == "gate_x_cov"
        assert cfg.coverage_live_threshold == 3
        assert cfg.report_fold_p == 0.5
        assert cfg.report_fold_scope == "user"
        assert cfg.report_self_term == "drop_user"
        assert cal._cfg_fp(cell) == root_calc_config_fingerprint(cell.config)

    def test_canonical_scope_reaches_config(self):
        # p=0 scope="user" canonicalizes to "all"; the canonicalization reaches .config.
        cell = cal.GridCell(1.0, "gate", report_fold_p=0.0, report_fold_scope="user")
        assert cell.config.report_fold_scope == "all"
        assert cal._cfg_fp(cell) == cal._cfg_fp(
            cal.GridCell(1.0, "gate", report_fold_p=0.0, report_fold_scope="all")
        )


class TestCfgFpRouter:
    """_cfg_fp is the sole sanctioned GridCell/config fingerprint router."""

    # ORIGINAL_CELL keeps the pre-g-zc3p legacy golden (unchanged by the rename).
    ORIGINAL_GOLDEN = (
        "7dd8d067f55f88c26c150c192203bd58de57762aadaf43ab8b6752e3fa6b1bde"
    )
    # CURRENT_SM_V2_3_CELL golden (sm-v2-3, pinned by literal fields).
    CURRENT_GOLDEN = (
        "7ca0d6541f2fcf372b7548e0e4caead118547335d424a9359fd5089706fcd262"
    )

    def test_gridcell_routes_through_config(self):
        cell = cal.GridCell(1.0, "gate")
        assert cal._cfg_fp(cell) == root_calc_config_fingerprint(cell.config)

    def test_rootcalcconfig_passes_through(self):
        config = RootCalcConfig(lcb_z=1.28, coverage_fold="gate_x_cov")
        assert cal._cfg_fp(config) == root_calc_config_fingerprint(config)

    def test_raw_gridcell_fingerprint_raises(self):
        with pytest.raises(TypeError):
            root_calc_config_fingerprint(cal.GridCell(0.0, "off"))

    def test_original_cell_hits_legacy_golden(self):
        assert cal._cfg_fp(cal.ORIGINAL_CELL) == self.ORIGINAL_GOLDEN

    def test_current_cell_anti_drift(self):
        # Pin CURRENT.config field-by-field against an EXPLICIT sm-v2-3 config (NOT a
        # no-argument RootCalcConfig(), which Phase 3 intentionally flips).
        explicit = RootCalcConfig(
            lcb_z=1.0,
            coverage_fold="gate",
            coverage_live_threshold=1,
            report_fold_p=0.0,
            report_fold_scope="all",
            report_self_term="keep",
        )
        assert cal.CURRENT_SM_V2_3_CELL.config == explicit
        assert cal._cfg_fp(cal.CURRENT_SM_V2_3_CELL) == root_calc_config_fingerprint(
            explicit
        )
        assert cal._cfg_fp(cal.CURRENT_SM_V2_3_CELL) == self.CURRENT_GOLDEN


class TestScorePairGrid:
    def test_overlay_built_once_scored_per_cell(self):
        graph, overlay, roots, opp = _opponent_root_overlay()
        # Isolate the gate effect at a fixed lcb_z=1.0: off vs gate.
        off_cell = cal.GridCell(1.0, "off")
        gate_cell = cal.GridCell(1.0, "gate")
        cells = (off_cell, gate_cell)
        db = MagicMock()
        with patch.object(cal, "overlay_evidence", return_value=overlay) as spy:
            cell_scores = cal.score_pair_grid(db, 1, "white", graph, roots, cells)
        # The ~2.6s overlay build happens ONCE, not once per cell.
        assert spy.call_count == 1
        assert set(cell_scores) == set(cells)
        # The gate lowers the opponent-root score below the ungated cell.
        ungated = cell_scores[off_cell].named_score_map[opp]
        gated = cell_scores[gate_cell].named_score_map[opp]
        assert gated < ungated

    def test_named_score_map_populated(self):
        graph, overlay, roots, opp = _opponent_root_overlay()
        result = cal.score_overlay(1, "white", graph, overlay, roots)
        assert opp in result.named_score_map
        assert result.named_score_map[opp] == pytest.approx(result.named_scores[0])


class TestDeltas:
    def test_pair_key_deltas_keep_opening_keys(self):
        reference = cal.PairScore(1, "white", named_score_map={"a": 50.0, "b": 60.0})
        cell = cal.PairScore(
            1, "white", named_score_map={"a": 40.0, "b": 66.0, "c": 1.0}
        )
        records = {r["opening_key"]: r for r in cal.pair_key_deltas(cell, reference)}
        # "c" is not in reference -> skipped; a: -10, b: +6, keyed with both scores.
        assert set(records) == {"a", "b"}
        assert records["a"] == {
            "opening_key": "a",
            "current_score": 50.0,
            "cell": 40.0,
            "delta": -10.0,
        }
        assert records["b"]["delta"] == pytest.approx(6.0)

    def test_summarize_deltas_has_no_histogram(self):
        summary = cal.summarize_deltas([-10.0, 6.0])
        assert summary["count"] == 2
        assert summary["mean"] == pytest.approx(-2.0)
        assert "histogram" not in summary


class TestGridReport:
    def _pair_grid(self, cells):
        graph, overlay, roots, _opp = _opponent_root_overlay()
        return {
            cell: cal.score_overlay(1, "white", graph, overlay, roots, cell.config)
            for cell in cells
        }

    def test_reference_omits_deltas_original_and_arm_carry(self):
        arm = cal.arm1_cells()[1]  # gate off + fold -> differs from current on opp root
        cells = (cal.CURRENT_SM_V2_3_CELL, cal.ORIGINAL_CELL, arm)
        pair_grids = [self._pair_grid(cells)]
        report = cal.build_grid_report(cells, pair_grids, min_observations=0)
        rows = report["cells"]
        reference = next(c for c in rows if c["is_reference"])
        assert reference["is_reference"] is True and reference["is_original"] is False
        assert "deltas_vs_current" not in reference
        # every non-reference cell carries deltas_vs_current + both anchor booleans
        for c in rows:
            assert {"is_original", "is_reference"} <= set(c.keys())
            if not c["is_reference"]:
                assert "deltas_vs_current" in c
        original = next(c for c in rows if c["is_original"])
        keys = original["deltas_vs_current"]["per_pair"][0]["keys"]
        assert keys and "current_score" in keys[0] and "delta" in keys[0]

    def test_cohort_rows_six_axis_identity_unique(self):
        cells = cal.build_arm_grid().cells
        pair_grids = [self._pair_grid(cells)]
        report = cal.build_grid_report(cells, pair_grids, min_observations=0)
        rows = report["cells"]
        assert len(rows) == len(cells)
        for r in rows:
            assert set(r["cell"].keys()) == _SIX_AXES
        identities = [
            tuple(r["cell"][axis] for axis in sorted(_SIX_AXES)) for r in rows
        ]
        # The four ARM-1 p-cells and four ARM-2 p-cells each serialize DISTINCTLY,
        # and B1 is distinguishable from CURRENT/ARM-2 on report_self_term.
        assert len(set(identities)) == len(rows)

    def test_min_observations_excludes_low_evidence(self):
        cells = (cal.CURRENT_SM_V2_3_CELL,)
        pair_grids = [self._pair_grid(cells)]  # observation_total == 4
        report = cal.build_grid_report(cells, pair_grids, min_observations=100)
        assert report["cells"][0]["named_score_distribution"]["count"] == 0


# --- grade-decoupling primitives -------------------------------------------


class TestGradeDecoupling:
    def test_grade_rank_inversion_cases(self):
        # "grade(parent) <= grade(child)" := rank(parent) >= rank(child).
        # A vs C: parent A is BETTER than child C -> the "<=" gate FAILS.
        assert not (cal.grade_rank("A") >= cal.grade_rank("C"))
        # C vs B: C <= B -> PASSES.
        assert cal.grade_rank("C") >= cal.grade_rank("B")
        # D and F are exactly one rank apart (3 vs 4).
        assert cal.grade_rank("F") - cal.grade_rank("D") == 1
        # "no more than one letter above": B is one above C -> passes; A two above -> fails.
        assert cal.grade_rank("B") >= cal.grade_rank("C") - 1
        assert not (cal.grade_rank("A") >= cal.grade_rank("C") - 1)
        with pytest.raises((AssertionError, KeyError)):
            cal.grade_rank("Z")

    def test_fixed_band_boundaries(self):
        assert cal.fixed_band(50.0) == "A"
        assert cal.fixed_band(49.9) == "B"
        assert cal.fixed_band(38.0) == "B"
        assert cal.fixed_band(27.9) == "D"
        assert cal.fixed_band(22.0) == "D"
        assert cal.fixed_band(21.9) == "F"

    def test_provisional_grade(self):
        cutoffs = cal.Cutoffs(a=44, b=29, c=8, d=2, alert=5, watch=29)
        assert cal.provisional_grade(44, cutoffs) == "A"
        assert cal.provisional_grade(43, cutoffs) == "B"
        assert cal.provisional_grade(8, cutoffs) == "C"
        assert cal.provisional_grade(2, cutoffs) == "D"
        assert cal.provisional_grade(1, cutoffs) == "F"

    def test_opp_guard_tolerance_boundary(self):
        # sm-v2-3 broad-guard reference (55.456, A): 43.5 passes, 43.0 fires (raw cap).
        assert not cal._opp_guard_fires(43.5, 55.456)
        assert cal._opp_guard_fires(43.0, 55.456)
        # A candidate two ranks worse fires on the RANK cap even within the raw cap:
        # reference 39 (B) vs candidate 27.9 (D) -> rank fires; raw drop 11.1 <= 12.
        assert cal._opp_guard_fires(27.9, 39.0)
        assert (39.0 - 27.9) <= cal.OPP_GUARD_MAX_RAW_DROP_PTS

    def test_leak_tolerance_boundary(self):
        # F-band leak reference: +5 candidate passes, +7 fires (raw cap).
        assert not cal._leak_fires(18.0 + 5.0, 18.0)
        assert cal._leak_fires(18.0 + 7.0, 18.0)

    def test_distribution_stats(self):
        stats = cal.distribution_stats([0.0, 10.0, 20.0, 30.0, 40.0])
        assert stats.p50 == pytest.approx(20.0)
        assert stats.p05 == pytest.approx(2.0)
        assert stats.p95 == pytest.approx(38.0)
        with pytest.raises(ValueError):
            cal.distribution_stats([1.0])
        # all-equal is VALID (spread 0) -- not a collision.
        equal = cal.distribution_stats([5.0, 5.0, 5.0])
        assert equal.p05 == equal.p95 == 5.0


class TestDeriveCutoffs:
    def test_fixed_vector_exact_cutoffs(self):
        scores = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        assert cal.derive_cutoffs(scores) == cal.Cutoffs(
            a=95, b=82, c=40, d=12, alert=25, watch=82
        )

    def test_round_half_up_not_bankers(self):
        # q25 of [10..60] interpolates to 22.5 -> round-half-up -> 23 (banker's: 22).
        cutoffs = cal.derive_cutoffs([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
        assert cutoffs == cal.Cutoffs(a=58, b=51, c=30, d=16, alert=23, watch=51)
        assert cal._round_half_up(2.5) == 3
        assert cal._round_half_up(0.5) == 1

    def test_compressed_collides(self):
        with pytest.raises(cal.CutoffCollision):
            cal.derive_cutoffs([10.0, 10.0, 10.0, 10.0])

    def test_p82_watch_shared_accepted(self):
        # watch and grade-b are BOTH p82 -> the same integer is fine, not a collision.
        cutoffs = cal.derive_cutoffs(
            [0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        )
        assert cutoffs.b == cutoffs.watch == 82

    def test_requires_two_scores(self):
        with pytest.raises(ValueError):
            cal.derive_cutoffs([5.0])

    def test_purity(self):
        vector = [1.0, 5.0, 9.8, 20.8, 43.9, 14.6, 0.4]
        assert cal.derive_cutoffs(vector) == cal.derive_cutoffs(vector)

    def test_numpy_type7_cross_check(self):
        numpy = pytest.importorskip("numpy")
        ordered = sorted([1.0, 5.0, 9.8, 20.8, 43.9, 14.6, 0.4])
        for q in (12.0, 25.0, 40.0, 82.0, 95.0):
            assert cal._interp_percentile(ordered, q) == pytest.approx(
                float(numpy.percentile(ordered, q, method="linear"))
            )


# --- percentile empty/singleton behavior preserved -------------------------


class TestPercentilePreserved:
    def test_percentile_none_and_singleton(self):
        assert cal.percentile([], 50.0) is None
        assert cal.percentile([7.0], 50.0) == 7.0
        assert cal.percentiles([]) == {
            "p5": None,
            "p25": None,
            "p50": None,
            "p75": None,
            "p95": None,
        }

    def test_percentiles_primitive_requires_two(self):
        with pytest.raises(ValueError):
            cal._percentiles([1.0], (50.0,))


# --- paired diagnostics -----------------------------------------------------

_USER14_OPERANDS = {
    "synth_black_root_score",
    "synth_caro_child_score",
    "synth_root_coverage_fraction",
    "synth_user_turn_pre_fold_quality",
    "synth_user_turn_multiplier",
    "synth_opp_turn_score",
    "synth_opp_turn_pre_fold_quality",
    "synth_opp_turn_multiplier",
    "user_tp_score",
}


class TestDiagnostics:
    def test_run_diagnostics_three_keys(self):
        diag = cal.run_diagnostics(cal.build_arm_grid())
        assert set(diag) == {"user14", "opponent_guard", "cliff"}
        # No leftover top-level specialist / broad_guard keys.
        assert "specialist" not in diag and "broad_guard" not in diag

    def test_user14_operands_aggregate_and_roles(self):
        grid = cal.build_arm_grid()
        diag = cal.run_user14_diagnostic(grid)
        # reference row is CURRENT and grades A; aggregate passes (all arms drop <= C).
        assert cal.fixed_band(diag["reference"]["user_tp_score"]) == "A"
        assert diag["passed"] is True
        # full operand set present on every row
        for row in diag["rows"]:
            assert _USER14_OPERANDS <= set(row.keys())
        # anchors + demo graded_for="none"; arms selection; B1 reference
        by_roles = {row["roles"]: row for row in diag["rows"]}
        assert by_roles[("original",)]["graded_for"] == "none"
        assert by_roles[("current",)]["graded_for"] == "none"
        arm_rows = [r for r in diag["rows"] if r["graded_for"] == "selection"]
        assert len(arm_rows) == 8  # 4 arm1 + 4 arm2
        assert all(
            cal.grade_rank(cal.fixed_band(r["user_tp_score"])) >= cal.grade_rank("C")
            for r in arm_rows
        )
        b1_row = by_roles[("b1",)]
        assert b1_row["graded_for"] == "reference"
        assert 30.0 <= b1_row["synth_black_root_score"] <= 38.0  # ~34 de-inflation ref

    def test_user14_b1_failure_never_flips_passed(self):
        # B1 is graded_for="reference": even scored, it never enters passed.
        grid = cal.build_arm_grid()
        diag = cal.run_user14_diagnostic(grid)
        selection = [
            r for r in diag["rows"] if r["graded_for"] == "selection" and r["applicable"]
        ]
        assert all(r["roles"] in {("arm1",), ("arm2",)} for r in selection)
        assert diag["passed"] is True

    def test_opponent_guard_applicability_and_operands(self):
        diag = cal.run_opponent_guard_diagnostic(cal.build_arm_grid())
        by_roles_applicable = {}
        for row in diag["rows"]:
            by_roles_applicable.setdefault(row["roles"], []).append(row["applicable"])
            assert {"broad_guard_opp_score", "specialist_pre_fold_quality"} <= set(row)
        # only ARM-1 (gate off) is applicable on the opponent turn
        assert all(by_roles_applicable[("arm1",)])
        assert not any(by_roles_applicable[("arm2",)])
        assert not any(by_roles_applicable[("b1",)])

    def test_leak_operand_unmasked(self):
        # ARM-1's ungated pre_fold_quality is STRICTLY GREATER than its reported opp
        # score (coverage**p masks it); the leak gate reads the ungated channel.
        diag = cal.run_opponent_guard_diagnostic(cal.build_arm_grid())
        arm1_row = next(r for r in diag["rows"] if r["roles"] == ("arm1",))
        pre_fold = arm1_row["specialist_pre_fold_quality"]
        assert pre_fold is not None
        graph, overlay, roots, target = cal._specialist_scenario()
        p = arm1_row["cell"]["report_fold_p"]
        arm1_cell = cal.GridCell(1.0, "off", report_fold_p=p, report_fold_scope="all")
        reported = cal._score_target(
            target, graph, overlay, roots, arm1_cell.config, now=cal.SYNTHETIC_AS_OF
        )
        assert pre_fold > reported

    def test_opponent_guard_arm1_leak_fires_aggregate_fails(self):
        # ARM-1 (fold-instead-of-gate) leaks: its ungated specialist quality exceeds the
        # current gated quality beyond the raw cap, so the leak gate fires and the
        # aggregate FAILS. Pin this so a regression that skips/reverses the leak gate
        # (leaving the operand tests green) cannot slip through.
        diag = cal.run_opponent_guard_diagnostic(cal.build_arm_grid())
        assert diag["passed"] is False
        reference_gated = diag["reference"]["specialist_pre_fold_quality"]
        reference_opp = diag["reference"]["broad_guard_opp_score"]
        arm1 = next(r for r in diag["rows"] if r["roles"] == ("arm1",))
        # the failure is specifically the LEAK: it fires, while the crater guard does not.
        assert cal._leak_fires(arm1["specialist_pre_fold_quality"], reference_gated) is True
        assert cal._opp_guard_fires(arm1["broad_guard_opp_score"], reference_opp) is False

    def test_cliff_reproduces_jump_probe_on_current(self):
        diag = cal.run_cliff_diagnostic(cal.build_arm_grid())
        assert diag["passed"] is True
        # the probe rows are the CURRENT (is_reference) gate cell at each threshold
        ref_rows = {
            r["coverage_live_threshold"]: r for r in diag["rows"] if r["is_reference"]
        }
        assert ref_rows[2]["thin_score"] == pytest.approx(0.0)
        assert ref_rows[2]["reviewed_score"] > 0.0
        assert ref_rows[1]["thin_score"] == pytest.approx(ref_rows[1]["reviewed_score"])
        # cliff rows carry the new six-axis cell identity
        assert set(diag["rows"][0]["cell"].keys()) == _SIX_AXES

    def test_cliff_rows_cell_axes_match_effective_threshold(self):
        # The serialized six-axis identity must reflect the threshold actually scored,
        # not the base cell's threshold (else a threshold-2 row's cell says threshold 1).
        diag = cal.run_cliff_diagnostic(cal.build_arm_grid())
        seen_thresholds = set()
        for row in diag["rows"]:
            assert (
                row["cell"]["coverage_live_threshold"]
                == row["coverage_live_threshold"]
            )
            seen_thresholds.add(row["coverage_live_threshold"])
        assert seen_thresholds == {1, 2}  # both swept thresholds are present

    def test_diagnostics_none_without_applicable_arm(self):
        anchors_only = cal.ArmGrid(
            cells=(cal.ORIGINAL_CELL, cal.CURRENT_SM_V2_3_CELL),
            roles_by_cell={
                cal.ORIGINAL_CELL: ("original",),
                cal.CURRENT_SM_V2_3_CELL: ("current",),
            },
        )
        assert cal.run_user14_diagnostic(anchors_only)["passed"] is None
        assert cal.run_opponent_guard_diagnostic(anchors_only)["passed"] is None

    def test_diagnostic_rows_json_primitive_no_default_str(self):
        # Every user14/opponent_guard row json.dumps WITHOUT default=str (a stray
        # dataclass/datetime would raise), row["cell"] is a six-axis dict, and the
        # NAMED *_pre_fold_quality operands are float-or-None with no generic key.
        diag = cal.run_diagnostics(cal.build_arm_grid())
        checks = {
            "user14": (
                "synth_user_turn_pre_fold_quality",
                "synth_opp_turn_pre_fold_quality",
            ),
            "opponent_guard": ("specialist_pre_fold_quality",),
        }
        for key, named_ops in checks.items():
            for row in diag[key]["rows"]:
                json.dumps(row)  # no default=str
                assert set(row["cell"].keys()) == _SIX_AXES
                assert "pre_fold_quality" not in row
                for op in named_ops:
                    assert row[op] is None or isinstance(row[op], float)


class TestClockThreading:
    def test_explicit_as_of_threads_and_default(self):
        grid = cal.build_arm_grid()
        # User-14 operands are clock-invariant, so an explicit as_of matches the default.
        default = cal.run_user14_diagnostic(grid)
        explicit = cal.run_user14_diagnostic(grid, as_of=_ALT_AS_OF)
        assert (
            default["reference"]["user_tp_score"]
            == pytest.approx(explicit["reference"]["user_tp_score"])
        )
        fx_default = cal.build_user14_fixture(cal.CURRENT_SM_V2_3_CELL, "sm-v2-3")
        fx_explicit = cal.build_user14_fixture(
            cal.CURRENT_SM_V2_3_CELL, "sm-v2-3", as_of=_ALT_AS_OF
        )
        assert fx_default["black_root_score"] == pytest.approx(
            fx_explicit["black_root_score"]
        )

    def test_no_datetime_now_reached(self):
        # Patch datetime.now to raise; a standalone run must never reach the wall clock.
        import app.opening_rootcalc as rc

        real_datetime = rc.datetime

        class _NoNow(real_datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: D401
                raise AssertionError("datetime.now reached")

        with patch.object(rc, "datetime", _NoNow):
            diag = cal.run_diagnostics(cal.build_arm_grid())
        assert set(diag) == {"user14", "opponent_guard", "cliff"}


# --- report-stage FEN lookup (leak gate) -----------------------------------


def _node_debug(fen, *, pre_fold_quality=None, reported_score=None, report_fold_multiplier=None):
    return NodeDebug(
        fen=fen,
        is_user_turn=True,
        in_book=True,
        is_extension_node=False,
        p_n=0.0,
        c_n=0.0,
        sample_conf=0.0,
        freshness=0.0,
        evidence_total=0.0,
        days_since_last_touch=0.0,
        last_touch_at=None,
        live_attempts=0,
        live_passes=0,
        review_attempts=0,
        prepared_children=[],
        weights={},
        subtree_live_attempts=0,
        subtree_review_attempts=0,
        covered_locally=False,
        raw_score=0.0,
        raw_confidence=0.0,
        raw_coverage=0.0,
        raw_depth=0.0,
        is_leaf=True,
        pre_fold_quality=pre_fold_quality,
        reported_score=reported_score,
        report_fold_multiplier=report_fold_multiplier,
    )


def _root_score(nodes):
    return RootScore(
        opening_key="k",
        opening_name="n",
        opening_family="f",
        player_color="black",
        opening_score=0.0,
        confidence=0.0,
        coverage=0.0,
        weighted_depth=0.0,
        sample_size=0,
        game_count=0,
        last_practiced_at=None,
        strongest_branch=None,
        weakest_branch=None,
        underexposed_branch=None,
        computed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        debug_nodes=nodes,
    )


class TestReportNodeFor:
    def _reported_and_descendant(self):
        # Score the User-14 black root with debug=True: root_fen is REPORTED (its own
        # root), deep_fen is a DESCENDANT-only node (never reported -> null operands).
        graph, black, _white, roots, root_fen, _child = cal._user14_scenario()
        deep_fen = cal._diag_positions(["e2e4", "c7c6", "d2d4", "d7d5"])[4]
        rs = cal.compute_root_score(
            root_fen,
            "black",
            graph,
            black,
            roots,
            cal.CURRENT_SM_V2_3_CELL.config,
            now=cal.SYNTHETIC_AS_OF,
            debug=True,
        )
        return rs, root_fen, deep_fen

    def test_absent_fen_raises(self):
        rs, _root_fen, _deep = self._reported_and_descendant()
        absent = cal._diag_positions(["d2d4"])[1]  # 1.d4 not reachable in this graph
        with pytest.raises(ValueError):
            cal._report_node_for(rs, absent)

    def test_descendant_only_raises(self):
        rs, _root_fen, deep_fen = self._reported_and_descendant()
        with pytest.raises(ValueError):
            cal._report_node_for(rs, deep_fen)

    def test_reported_returns_node(self):
        rs, root_fen, _deep = self._reported_and_descendant()
        node = cal._report_node_for(rs, root_fen)
        assert node.pre_fold_quality is not None
        assert cal.pre_fold_quality_for(rs, root_fen) == node.pre_fold_quality

    def test_require_narrowing(self):
        fen = _positions(["e2e4"])[1]
        # pre_fold_quality set, but reported_score None.
        rs = _root_score(
            [_node_debug(fen, pre_fold_quality=50.0, reported_score=None, report_fold_multiplier=1.0)]
        )
        # Narrow require succeeds; the default require (all three) raises on the null.
        assert (
            cal._report_node_for(rs, fen, require=("pre_fold_quality",)).pre_fold_quality
            == 50.0
        )
        with pytest.raises(ValueError):
            cal._report_node_for(rs, fen)


# --- User-14 fixture builder + writer ---------------------------------------


class TestUser14Fixture:
    def test_build_shape_and_internal_consistency(self):
        for cell in (cal.CURRENT_SM_V2_3_CELL, cal.arm2_cells()[1]):
            fixture = cal.build_user14_fixture(cell, "sm-v2-3")
            assert fixture["schema_version"] == 1
            assert fixture["model_version"] == "sm-v2-3"
            assert fixture["config_fingerprint"] == cal._cfg_fp(cell)
            assert fixture["report_fold_p"] == cell.report_fold_p
            frac = fixture["root_coverage_fraction"]
            expected = 100.0 * frac ** cell.report_fold_p
            assert fixture["coverage_implied_score"] == pytest.approx(expected, abs=1e-9)

    def test_writer_round_trips_to_tmp(self, tmp_path):
        payload = cal.build_user14_fixture(cal.CURRENT_SM_V2_3_CELL, "sm-v2-3")
        target = tmp_path / "nested" / "user14.json"
        written = cal.write_user14_fixture(payload, path=target)
        assert written == target
        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert json.loads(text) == payload
        # deterministic: sorted keys, 2-space indent
        assert json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n" == text

    def test_writer_never_touches_checked_in_default(self, tmp_path):
        # The Phase-1 tests always write to a tmp path; the settled fixture is untouched.
        payload = cal.build_user14_fixture(cal.CURRENT_SM_V2_3_CELL, "sm-v2-3")
        target = tmp_path / "user14.json"
        cal.write_user14_fixture(payload, path=target)
        assert cal.DEFAULT_USER14_FIXTURE_PATH != target


# --- orchestration end-to-end (default + demo) ------------------------------


class TestMainEndToEnd:
    def _main(self, argv):
        graph, overlay, roots, _opp = _opponent_root_overlay()
        db = MagicMock()
        with patch.object(cal, "overlay_evidence", return_value=overlay), patch(
            "app.opening_cache.list_opening_score_candidate_pairs", return_value=[]
        ), patch.object(cal, "get_opening_graph", return_value=graph), patch.object(
            cal, "get_opening_roots", return_value=roots
        ):
            return cal.main(argv, session_factory=_fake_factory(db))

    def test_diagnostics_keys_and_primitive_rows(self):
        report = self._main(["--min-observations", "0"])
        assert set(report["diagnostics"]) == {"user14", "opponent_guard", "cliff"}
        named = {
            "user14": (
                "synth_user_turn_pre_fold_quality",
                "synth_opp_turn_pre_fold_quality",
            ),
            "opponent_guard": ("specialist_pre_fold_quality",),
        }
        for key, ops in named.items():
            for row in report["diagnostics"][key]["rows"]:
                json.dumps(row)  # NO default=str
                assert set(row["cell"].keys()) == _SIX_AXES
                assert "pre_fold_quality" not in row
                for op in ops:
                    assert row[op] is None or isinstance(row[op], float)
        # whole report still round-trips with default=str (production emission)
        assert json.loads(json.dumps(report, indent=2, default=str))
        # text renders and names all three diagnostics
        text = cal.render_text(report)
        assert "User-14" in text
        assert "opponent regression guard" in text
        assert "cliff" in text

    def test_default_excludes_demo(self):
        report = self._main(["--min-observations", "0"])
        for diag in ("user14", "opponent_guard"):
            assert all(
                row["roles"] != ("demo",)
                for row in report["diagnostics"][diag]["rows"]
            )
        assert all(not _is_demo_axes(c["cell"]) for c in report["grid"]["cells"])

    def test_include_demo_diagnostics(self):
        report = self._main(["--min-observations", "0", "--include-demo-diagnostics"])
        for diag in ("user14", "opponent_guard"):
            demo_rows = [
                row
                for row in report["diagnostics"][diag]["rows"]
                if row["roles"] == ("demo",)
            ]
            assert len(demo_rows) == 1
            assert demo_rows[0]["graded_for"] == "none"
        # report["grid"] STILL excludes demos even when the flag is on.
        assert all(not _is_demo_axes(c["cell"]) for c in report["grid"]["cells"])
