"""Tests for the opening-score v2 calibration script.

Fixture-driven and DB-free: the statistics, URL guard, in-memory scoring,
report assembly, JSON output, filtering, and the no-write default are all
exercised without a live database.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import platform
import py_compile
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import chess
import pytest

from app.db import DATABASE_URL
from app.fen import active_color, normalize_fen
from app.opening_cache import evidence_derivation_fingerprint
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

        result = cal.score_overlay(1, "white", graph, overlay, roots, as_of=cal.SYNTHETIC_AS_OF)

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

        result = cal.score_overlay(1, "white", graph, overlay, roots, as_of=cal.SYNTHETIC_AS_OF)

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
            cal.score_overlay(1, "white", graph, overlay, roots, as_of=cal.SYNTHETIC_AS_OF)
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
            cell_scores = cal.score_pair_grid(
                db, 1, "white", graph, roots, cells, as_of=cal.SYNTHETIC_AS_OF
            )
        # The ~2.6s overlay build happens ONCE, not once per cell.
        assert spy.call_count == 1
        assert set(cell_scores) == set(cells)
        # The gate lowers the opponent-root score below the ungated cell.
        ungated = cell_scores[off_cell].named_score_map[opp]
        gated = cell_scores[gate_cell].named_score_map[opp]
        assert gated < ungated

    def test_named_score_map_populated(self):
        graph, overlay, roots, opp = _opponent_root_overlay()
        result = cal.score_overlay(1, "white", graph, overlay, roots, as_of=cal.SYNTHETIC_AS_OF)
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
            cell: cal.score_overlay(
                1, "white", graph, overlay, roots, cell.config, as_of=cal.SYNTHETIC_AS_OF
            )
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


# ===========================================================================
# Frozen-cohort artifact (g-p4ih-artifact): schema, canonical encoding, semantic
# validation, split load guard, release-guard binding. All synthetic — no DB,
# no private cohort; synthetic overlays + synthetic provenance bytes + a
# synthetic RuntimeBinding.
# ===========================================================================

_FZ_AS_OF = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
_FZ_GRAPH_FP = "graph-fp-1"
_FZ_ROOTS_FP = "roots-fp-1"
_FZ_MIRRORED = (
    "schema_version", "as_of", "captured_model_version", "graph_fingerprint",
    "roots_fingerprint", "evidence_derivation_fingerprint", "pair_count",
    "min_observations", "cohort_rules", "release_guard_opening_key",
    "release_guard_child_opening_key",
    "capture_scorer_source_digest", "capture_source_revision",
    "capture_python_version", "capture_chess_version",
)

# Synthetic capture attestation (g-p4ih-producer-bind): format-valid, deliberately NOT
# the current environment's values — every green load below also exercises the
# non-gating decision.
_FZ_CAPTURE_DIGEST = "d1" * 32
_FZ_CAPTURE_REVISION = "ab" * 20
_FZ_CAPTURE_PYTHON = "CPython 3.12.1"
_FZ_CAPTURE_CHESS = "1.11.2"


def _fz_root() -> str:
    return normalize_fen(chess.Board().fen())


def _fz_e4() -> str:
    board = chess.Board()
    board.push_uci("e2e4")
    return normalize_fen(board.fen())


def _fz_overlay(uid: int, color: str, n: int, *, last_days: int = 2) -> EvidenceOverlay:
    """A consistent single-node/single-edge overlay: node quality_count sum == edge
    quality_count sum == source_counts sum == n (the cross-field telemetry invariant).
    White nodes sit at the start position (white to move); black nodes at 1.e4 (black to
    move), so each node's active color matches player_color."""
    root, e4 = _fz_root(), _fz_e4()
    node_fen = root if color == "white" else e4
    overlay = EvidenceOverlay(uid, color)
    overlay.nodes[node_fen] = NodeEvidence(
        fen=node_fen,
        quality_sum=float(n),
        quality_count=n,
        session_ids={f"{uid}-{color}-{i}" for i in range(n)},
        live_attempts=n,
        live_passes=n,
        live_fails=0,
        last_live_at=_FZ_AS_OF - timedelta(days=last_days),
    )
    overlay.edges[(root, e4)] = EdgeEvidence(
        root, e4, "e2e4",
        traversal_count=n, live_attempts=n, live_passes=n, live_fails=0,
        quality_sum=float(n), quality_count=n,
    )
    if n:
        overlay.source_counts["session_eval"] = n
    overlay.phase_samples.append(PhaseSample(4, None, None))
    return overlay


def _fz_inputs(quantile_obs=(25, 22), guard_obs=(15, 15)):
    """The canonical cohort shape: two User-14 release-guard records (both colors, one
    subject) + two quantile pairs (users 2 & 3)."""
    return [
        cal.CapturedPairInput(_fz_overlay(14, "white", guard_obs[0]), "release_guard", 5, "fp-14w"),
        cal.CapturedPairInput(_fz_overlay(14, "black", guard_obs[1]), "release_guard", 5, "fp-14b"),
        cal.CapturedPairInput(_fz_overlay(2, "black", quantile_obs[0]), "quantile", 3, "fp-2b"),
        cal.CapturedPairInput(_fz_overlay(3, "white", quantile_obs[1]), "quantile", 3, "fp-3w"),
    ]


def _fz_header_input(**overrides) -> "cal.ArtifactHeaderInput":
    base = dict(
        as_of=_FZ_AS_OF,
        graph_fingerprint=_FZ_GRAPH_FP,
        roots_fingerprint=_FZ_ROOTS_FP,
        cache_epoch=7,
        captured_model_version="sm-v2-3",
        evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
        capture_scorer_source_digest=_FZ_CAPTURE_DIGEST,
        capture_source_revision=_FZ_CAPTURE_REVISION,
        capture_python_version=_FZ_CAPTURE_PYTHON,
        capture_chess_version=_FZ_CAPTURE_CHESS,
    )
    base.update(overrides)
    return cal.ArtifactHeaderInput(**base)


def _fz_freeze(inputs=None, header=None) -> bytes:
    return cal.freeze_frozen_artifact(inputs or _fz_inputs(), header or _fz_header_input())


# The canonical GOOD header dict (used to build schema-valid provenance for artifact
# semantic tests, so a header semantic error is caught in Phase A before integrity).
_FZ_REF_HEADER = json.loads(_fz_freeze())["header"]


def _fz_prov(artifact_bytes: bytes, header: dict | None = None, **overrides) -> bytes:
    header = header if header is not None else _FZ_REF_HEADER
    record = {k: header[k] for k in _FZ_MIRRORED}
    record["sha256"] = hashlib.sha256(artifact_bytes).hexdigest()
    record.update(overrides)
    return cal._canonical_dumps(record)


def _fz_rb(**overrides) -> "cal.RuntimeBinding":
    base = dict(
        graph_fingerprint=_FZ_GRAPH_FP,
        roots_fingerprint=_FZ_ROOTS_FP,
        evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
        min_observations=cal.DEFAULT_MIN_OBSERVATIONS,
        cohort_rules=cal.COHORT_RULES_ID,
        release_guard_opening_key=cal.RELEASE_GUARD_OPENING_KEY,
        release_guard_child_opening_key=cal.RELEASE_GUARD_CHILD_OPENING_KEY,
    )
    base.update(overrides)
    return cal.RuntimeBinding(**base)


def _fz_load(artifact_bytes: bytes, provenance_bytes: bytes | None = None, rb=None):
    return cal.load_frozen_artifact(
        artifact_bytes,
        provenance_bytes if provenance_bytes is not None else _fz_prov(artifact_bytes),
        rb or _fz_rb(),
    )


class _FingerprintOnly:
    """A stand-in registry for the producer entry point (g-p4ih-capture).

    ``_current_runtime_binding`` reads exactly one attribute off graph and roots —
    ``.fingerprint`` — so these two stubs reproduce ``_fz_rb()`` EXACTLY, which is what lets
    the same bytes go through both entry points under the same binding. A rejecting row
    never reaches scoring, so nothing else about a real registry is needed."""

    __slots__ = ("fingerprint",)

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint


_FZ_GRAPH_STUB = _FingerprintOnly(_FZ_GRAPH_FP)
_FZ_ROOTS_STUB = _FingerprintOnly(_FZ_ROOTS_FP)


def _fz_assert_self_check_parity(artifact_bytes, provenance_bytes, expected):
    """g-p4ih-capture: whatever the CONSUMER's load guard rejects, the PRODUCER's
    pre-publication self-check must reject identically.

    This is asserted HERE, inside the shared rejection helper, rather than transcribed into
    the capture tests — so every row of this module's malformation corpus is a parity row by
    construction, and a row added tomorrow is covered the day it lands. The failure mode it
    exists to prevent is a self-check that keeps header/provenance validation but skips pair
    semantics: capture would then publish an artifact the release path fails closed on, and
    the operator would discover it at release time with no artifact to fall back to."""
    try:
        cal.validate_capture_candidate(
            artifact_bytes, provenance_bytes,
            graph=_FZ_GRAPH_STUB, roots=_FZ_ROOTS_STUB,
        )
    except Exception as exc:  # noqa: BLE001 - the comparison IS the assertion
        actual = (type(exc), str(exc))
    else:
        raise AssertionError(
            "validate_capture_candidate ACCEPTED bytes the load guard rejected with "
            f"{expected[0].__name__}: {expected[1]!r} — capture would publish an artifact "
            "the release path refuses to load"
        )
    assert actual == expected, (
        "the producer self-check and the consumer load guard disagree on the same bytes:\n"
        f"  load_frozen_artifact:        {expected[0].__name__}: {expected[1]!r}\n"
        f"  validate_capture_candidate:  {actual[0].__name__}: {actual[1]!r}"
    )


def _fz_reject_bytes(artifact_bytes, provenance_bytes, substring, exc=cal.ArtifactSemanticError):
    """The BYTE-LEVEL rejection assertion, shared by ``_fz_reject`` and by every
    hand-crafted raw-byte case (duplicate JSON keys, Infinity/-0.0 literals, whitespace /
    unsorted-key / exponent-float non-canonicality, shuffled nodes, token ordering,
    integrity/digest, provenance-record schema, Phase-C drift, ...).

    Assert ``load_frozen_artifact`` raises ``exc`` matching ``substring`` under the NORMAL
    ``_fz_rb()`` binding, THEN replay the same bytes through the capture producer's self-check
    and require it to reject IDENTICALLY (same type, same message). Routing the raw-byte cases
    through here — rather than calling ``load_frozen_artifact`` directly — is what makes the
    parity claim cover the WHOLE rejection corpus and not just the rows that go through
    ``_fz_reject``.

    There is NO parity opt-out, on purpose. The self-check builds its binding from the
    registries it is handed, and those reproduce ``_fz_rb()`` EXACTLY (asserted by
    ``TestSharedRejectionCorpusIsWiredToTheSelfCheck``), so it applies the identical load
    guard to identical bytes — every rejection row is a parity row unconditionally. A Phase-C
    DRIFT row proves its drift the way the mirrored min_observations/cohort_rules rows already
    do: put the drifted value in BOTH the header and the record (so integrity passes) and load
    under the normal binding — never by injecting a custom binding the self-check cannot see."""
    with pytest.raises(exc) as ei:
        cal.load_frozen_artifact(artifact_bytes, provenance_bytes, _fz_rb())
    assert substring in str(ei.value), f"expected {substring!r} in {ei.value!r}"
    _fz_assert_self_check_parity(
        artifact_bytes, provenance_bytes, (type(ei.value), str(ei.value)))
    return str(ei.value)


def _fz_reject(perturb, substring, exc=cal.ArtifactSemanticError):
    """Perturb a fresh valid payload, re-encode canonically, load with SCHEMA-VALID
    provenance (from the reference header), and assert the load raises ``exc`` whose
    message contains ``substring``. Provenance is good so a header malformation is
    owned by Phase A semantic validation, not integrity.

    Delegates the assertion (loader + producer-self-check parity) to ``_fz_reject_bytes``."""
    payload = json.loads(_fz_freeze())
    perturb(payload)
    bs = cal._canonical_dumps(payload)
    return _fz_reject_bytes(bs, _fz_prov(bs), substring, exc)


class TestEvidenceDerivationFingerprint:
    def test_pins_exactly_five_surfaces(self):
        from app.game_phase import DIVIDER_VERSION
        from app.opening_evidence import OPENING_EVIDENCE_INPUTS_VERSION
        from app.opening_quality import QUALITY_VERSION, TAU_CP, TAU_WC

        fp = evidence_derivation_fingerprint()
        # The five folded surfaces are present.
        for surface in (DIVIDER_VERSION, QUALITY_VERSION, repr(TAU_WC), repr(TAU_CP), OPENING_EVIDENCE_INPUTS_VERSION):
            assert surface in fp
        # It is exactly their colon join — adding/removing a surface is a schema change.
        assert fp == (
            f"{DIVIDER_VERSION}:{QUALITY_VERSION}:{TAU_WC!r}:{TAU_CP!r}"
            f":{OPENING_EVIDENCE_INPUTS_VERSION}"
        )

    def test_excludes_scoring_and_graph_surfaces(self):
        from app.opening_cache import (
            OPENING_SCORE_CACHE_SCHEMA_VERSION,
            SCORE_MODEL_VERSION,
        )
        from app.opening_rootcalc import root_calc_config_fingerprint

        fp = evidence_derivation_fingerprint()
        # Scoring-side / cache-side / graph surfaces are DELIBERATELY not folded in
        # (the artifact stays reusable across model bumps).
        assert SCORE_MODEL_VERSION not in fp
        assert root_calc_config_fingerprint() not in fp
        assert OPENING_SCORE_CACHE_SCHEMA_VERSION not in fp
        assert _FZ_GRAPH_FP not in fp


class TestReleaseGuardKeys:
    def test_opening_key_is_derived_not_hand_typed(self):
        board = chess.Board()
        board.push_uci("e2e4")
        assert cal.RELEASE_GUARD_OPENING_KEY == normalize_fen(board.fen())
        assert active_color(cal.RELEASE_GUARD_OPENING_KEY) == "black"

    def test_child_key_is_derived_not_hand_typed(self):
        board = chess.Board()
        board.push_uci("e2e4")
        board.push_uci("c7c6")
        assert cal.RELEASE_GUARD_CHILD_OPENING_KEY == normalize_fen(board.fen())

    def test_pinned_module_constants(self):
        assert cal.ARTIFACT_SCHEMA_VERSION == 2
        assert cal.COHORT_RULES_ID == "opening-cohort-rules-v1"
        assert cal.DEFAULT_MIN_OBSERVATIONS == 20
        assert cal.TIMESTAMP_FLOOR == datetime(2000, 1, 1, tzinfo=timezone.utc)


class TestFreezeByteStability:
    def test_freeze_returns_bytes_and_loads_green(self):
        bs = _fz_freeze()
        assert isinstance(bs, bytes)
        cohort = _fz_load(bs)
        assert cohort.artifact_sha256 == hashlib.sha256(bs).hexdigest()
        assert [p.pair_id for p in cohort.pairs] == ["pair-00", "pair-01", "pair-02", "pair-03"]
        assert [p.surrogate_user_id for p in cohort.pairs] == [1, 2, 3, 4]
        # The two User-14 records share one subject and are the release guards.
        guards = [p for p in cohort.pairs if p.cohort_role == "release_guard"]
        assert {g.player_color for g in guards} == {"white", "black"}
        assert len({g.subject_id for g in guards}) == 1

    def test_byte_identity_across_two_serializations(self):
        assert _fz_freeze() == _fz_freeze()

    def test_byte_identity_nontrivial_and_integral_floats(self):
        # A fractional quality_sum keeps its shortest round-trip repr; an integral
        # quality_sum stays 2.0, never 2.
        overlay = _fz_overlay(2, "black", 3)
        node = next(iter(overlay.nodes.values()))
        node.quality_sum = 0.1 + 0.2  # 0.30000000000000004
        overlay.edges[(_fz_root(), _fz_e4())].quality_sum = 2.0
        inputs = _fz_inputs()
        inputs[2] = cal.CapturedPairInput(overlay, "quantile", 3, "fp-2b")
        # node qc=3, edge qc=3, source_counts=3 still hold (only quality_sum changed).
        bs = _fz_freeze(inputs)
        assert b'"quality_sum":0.30000000000000004' in bs
        assert b'"quality_sum":2.0' in bs
        assert b'"quality_sum":2,' not in bs  # never bare int
        assert bs == _fz_freeze(inputs)  # deterministic

    def test_shuffle_invariance_pair_and_node_order(self):
        inputs = _fz_inputs()
        b1 = _fz_freeze(inputs)
        # Reverse pair order and rebuild each overlay's nodes dict in reversed order —
        # determinism comes from the sorted-source assignment, not insertion order.
        shuffled = list(reversed(inputs))
        for cp in shuffled:
            items = list(cp.overlay.nodes.items())
            cp.overlay.nodes = dict(reversed(items))
        assert _fz_freeze(shuffled) == b1

    def test_timestamp_offsets_reconstruct_bit_for_bit(self):
        cohort = _fz_load(_fz_freeze())
        black_guard = next(p for p in cohort.pairs if p.player_color == "black" and p.cohort_role == "release_guard")
        node = next(iter(black_guard.overlay.nodes.values()))
        # equality, not epsilon.
        assert node.last_live_at == _FZ_AS_OF - timedelta(days=2)

    def test_canonical_as_of_roundtrips_byte_identically(self):
        header = json.loads(_fz_freeze())["header"]
        assert header["as_of"] == "2026-07-01T12:00:00.000000Z"
        cohort = _fz_load(_fz_freeze())
        assert cohort.header.as_of == _FZ_AS_OF

    def test_same_instant_different_offsets_identical_bytes(self):
        plus_two = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        b_utc = _fz_freeze(header=_fz_header_input(as_of=_FZ_AS_OF))
        b_off = _fz_freeze(header=_fz_header_input(as_of=plus_two))
        assert b_utc == b_off
        assert hashlib.sha256(b_utc).hexdigest() == hashlib.sha256(b_off).hexdigest()

    def test_naive_as_of_rejected_at_freeze(self):
        with pytest.raises(cal.ArtifactSemanticError, match="timezone-aware"):
            _fz_freeze(header=_fz_header_input(as_of=datetime(2026, 7, 1, 12, 0, 0)))

    def test_naive_evidence_timestamp_rejected_at_freeze(self):
        overlay = _fz_overlay(2, "black", 25)
        next(iter(overlay.nodes.values())).last_live_at = datetime(2026, 6, 1, 0, 0, 0)  # naive
        inputs = _fz_inputs()
        inputs[2] = cal.CapturedPairInput(overlay, "quantile", 3, "fp-2b")
        with pytest.raises(cal.ArtifactSemanticError, match="timezone-aware"):
            _fz_freeze(inputs)

    def test_distinct_source_precondition(self):
        dup = _fz_inputs() + [cal.CapturedPairInput(_fz_overlay(2, "black", 25), "quantile", 3, "dup")]
        with pytest.raises(cal.ArtifactSemanticError, match="duplicate source pair"):
            _fz_freeze(dup)

    def test_duplicate_phase_sample_roundtrip_preserved(self):
        overlay = _fz_overlay(14, "white", 15)
        overlay.phase_samples = [PhaseSample(4, 8, None), PhaseSample(4, 8, None)]
        inputs = _fz_inputs()
        inputs[0] = cal.CapturedPairInput(overlay, "release_guard", 5, "fp-14w")
        cohort = _fz_load(_fz_freeze(inputs))
        white_guard = next(p for p in cohort.pairs if p.player_color == "white" and p.cohort_role == "release_guard")
        assert len(white_guard.overlay.phase_samples) == 2
        assert white_guard.overlay.phase_samples == [PhaseSample(4, 8, None), PhaseSample(4, 8, None)]


class TestSemanticRejectionTable:
    """One malformation per rule; each rejects with its OWN distinct message."""

    def _q_node(self, payload):
        return payload["pairs"][2]["nodes"][0]

    def _q_edge(self, payload):
        return payload["pairs"][2]["edges"][0]

    def test_bool_as_count(self):
        def p(pl):
            self._q_node(pl)["live_attempts"] = True
        _fz_reject(p, "bool rejected")

    def test_negative_count(self):
        def p(pl):
            self._q_node(pl)["live_fails"] = -1
        _fz_reject(p, "live_fails")

    def test_quality_sum_gt_quality_count(self):
        def p(pl):
            node = self._q_node(pl)
            node["quality_sum"] = float(node["quality_count"]) + 1.0
        _fz_reject(p, "out of [0, quality_count")

    def test_attempts_ne_passes_plus_fails(self):
        def p(pl):
            self._q_node(pl)["live_passes"] += 1
        _fz_reject(p, "!= live_passes + live_fails")

    def test_review_attempts_ne_passes_plus_fails(self):
        def p(pl):
            self._q_node(pl)["review_attempts"] = 1  # passes+fails still 0
        _fz_reject(p, "review_attempts")

    def test_edge_live_attempts_gt_traversal(self):
        def p(pl):
            edge = self._q_edge(pl)
            edge["live_attempts"] = edge["traversal_count"] + 1
            edge["live_passes"] = edge["live_attempts"]
        _fz_reject(p, "> traversal_count")

    def test_edge_quality_count_gt_traversal(self):
        def p(pl):
            edge = self._q_edge(pl)
            edge["quality_count"] = edge["traversal_count"] + 1
        # cross-field telemetry also shifts, but quality>traversal is checked first.
        msg = _fz_reject(p, "> traversal_count")
        assert "quality_count" in msg

    def test_bad_color(self):
        def p(pl):
            pl["pairs"][2]["player_color"] = "green"
        _fz_reject(p, "player_color")

    def test_node_active_color_ne_player_color(self):
        # pair-02 is white (node at start position, white to move). Swap in a
        # black-to-move fen → active color mismatches player_color.
        def p(pl):
            node = pl["pairs"][2]["nodes"][0]
            node["fen"] = _fz_e4()  # black to move, but the pair is 'black' already...
        # pair-02 is actually 'black' after sort? guard against ordering: find a white pair.
        payload = json.loads(_fz_freeze())
        white_idx = next(i for i, pr in enumerate(payload["pairs"]) if pr["player_color"] == "white")

        def p2(pl):
            pl["pairs"][white_idx]["nodes"][0]["fen"] = _fz_e4()
        _fz_reject(p2, "active-color")

    def test_malformed_fen(self):
        def p(pl):
            self._q_node(pl)["fen"] = "not a fen"
        _fz_reject(p, "parseable FEN")

    def test_non_normalized_fen(self):
        def p(pl):
            # A full 6-field FEN (has move clocks) is not in normalized 4-field form.
            node = self._q_node(pl)
            node["fen"] = node["fen"] + " 0 1"
        _fz_reject(p, "normalized 4-field form")

    def test_unknown_key_top(self):
        _fz_reject(lambda pl: pl.__setitem__("extra", 1), "unknown key")

    def test_unknown_key_header(self):
        _fz_reject(lambda pl: pl["header"].__setitem__("leak", 1), "unknown key")

    def test_unknown_key_pair(self):
        _fz_reject(lambda pl: pl["pairs"][2].__setitem__("user_id", 99), "unknown key")

    def test_unknown_key_node(self):
        _fz_reject(lambda pl: self._q_node(pl).__setitem__("session_ids", []), "unknown key")

    def test_unknown_key_edge(self):
        _fz_reject(lambda pl: self._q_edge(pl).__setitem__("secret", 1), "unknown key")

    def test_missing_required_key(self):
        _fz_reject(lambda pl: self._q_node(pl).pop("is_ghost_target"), "missing required key")

    def test_duplicate_json_object_key(self):
        # Hand-craft raw bytes with a duplicate key — json.loads would silently
        # last-write-win, so the object_pairs_hook must reject it.
        bs = _fz_freeze()
        assert b'"pair_count":4,' in bs
        raw = bs.replace(b'"pair_count":4,', b'"pair_count":4,"pair_count":4,', 1)
        _fz_reject_bytes(raw, _fz_prov(raw), "duplicate JSON object key")

    def test_duplicate_node_key(self):
        def p(pl):
            nodes = pl["pairs"][2]["nodes"]
            nodes.append(copy.deepcopy(nodes[0]))
        _fz_reject(p, "duplicate node key")

    def test_duplicate_edge_key(self):
        def p(pl):
            edges = pl["pairs"][2]["edges"]
            edges.append(copy.deepcopy(edges[0]))
        _fz_reject(p, "duplicate edge key")

    def test_missing_uci(self):
        _fz_reject(lambda pl: self._q_edge(pl).pop("uci"), "missing required key")

    def test_unparseable_uci(self):
        _fz_reject(lambda pl: self._q_edge(pl).__setitem__("uci", "zzzz"), "parseable UCI")

    def test_legal_uci_not_reaching_child(self):
        # g1f3 is legal from the start position but does not reach the e4 child.
        def p(pl):
            self._q_edge(pl)["uci"] = "g1f3"
        _fz_reject(p, "does not reach child_fen")

    def test_illegal_uci_from_parent(self):
        def p(pl):
            self._q_edge(pl)["uci"] = "e2e5"  # not legal from start
        _fz_reject(p, "not legal from parent_fen")

    def test_negative_offset(self):
        def p(pl):
            self._q_node(pl)["last_live_us_before"] = -1
        _fz_reject(p, "last_live_us_before")

    def test_overflow_offset(self):
        def p(pl):
            self._q_node(pl)["last_live_us_before"] = 10 ** 20
        _fz_reject(p, "exceeds")

    def test_float_offset(self):
        def p(pl):
            self._q_node(pl)["last_live_us_before"] = 1.5
        _fz_reject(p, "last_live_us_before")

    def test_non_canonical_as_of_offset_suffix(self):
        def p(pl):
            pl["header"]["as_of"] = "2026-07-01T12:00:00.000000+00:00"
        _fz_reject(p, "canonical")

    def test_non_canonical_as_of_missing_z(self):
        def p(pl):
            pl["header"]["as_of"] = "2026-07-01T12:00:00.000000"
        _fz_reject(p, "canonical")

    def test_leaky_subject_id(self):
        def p(pl):
            for pr in pl["pairs"]:
                if pr["cohort_role"] == "release_guard":
                    pr["subject_id"] = "subject-u14"
        # subject format check fires (before the contiguity structure check).
        _fz_reject(p, "subject-\\d+")

    def test_cross_pair_session_token(self):
        def p(pl):
            self._q_node(pl)["session_tokens"][0] = "pair-99-g0"
        _fz_reject(p, "token for this pair")

    def test_pair_count_mismatch(self):
        def p(pl):
            pl["header"]["pair_count"] = 3
        _fz_reject(p, "pair_count")

    def test_infinity_literal(self):
        bs = _fz_freeze()
        assert b'"quality_sum":25.0' in bs
        raw = bs.replace(b'"quality_sum":25.0', b'"quality_sum":Infinity', 1)
        _fz_reject_bytes(raw, _fz_prov(raw), "non-finite JSON literal")

    def test_unknown_source_counts_label(self):
        def p(pl):
            pl["pairs"][2]["source_counts"] = {"mystery": 25}
        _fz_reject(p, "unknown quality-source label")

    def test_zero_source_counts_value(self):
        def p(pl):
            pl["pairs"][2]["source_counts"] = {"session_eval": 25, "eval_delta": 0}
        _fz_reject(p, "must be >= 1")

    def test_source_counts_sum_ne_quality_count(self):
        def p(pl):
            pl["pairs"][2]["source_counts"] = {"session_eval": 24}  # node/edge qc still 25
        _fz_reject(p, "telemetry does not describe overlay")

    def test_malformed_phase_sample_missing_key(self):
        def p(pl):
            pl["pairs"][2]["phase_samples"] = [{"opening_interval_len": 4, "middle_ply": None}]
        _fz_reject(p, "missing required key")

    def test_malformed_phase_sample_negative(self):
        def p(pl):
            pl["pairs"][2]["phase_samples"] = [{"opening_interval_len": -1, "middle_ply": None, "end_ply": None}]
        _fz_reject(p, "opening_interval_len")

    def test_unsupported_schema_version(self):
        def p(pl):
            pl["header"]["schema_version"] = 3
        _fz_reject(p, "unsupported schema", exc=cal.UnsupportedArtifactSchemaError)

    def test_format_malformed_cohort_rules(self):
        def p(pl):
            pl["header"]["cohort_rules"] = "free text"
        _fz_reject(p, "opening-cohort-rules")

    def test_format_malformed_captured_model_version(self):
        def p(pl):
            pl["header"]["captured_model_version"] = "leaked-secret-v9"
        _fz_reject(p, "sm-v")

    def test_as_of_before_timestamp_floor_all_null_offsets(self):
        # An all-null-timestamp artifact whose as_of predates the floor still rejects.
        overlay = _fz_overlay(2, "black", 25)
        next(iter(overlay.nodes.values())).last_live_at = None
        inputs = _fz_inputs()
        inputs[2] = cal.CapturedPairInput(overlay, "quantile", 3, "fp-2b")
        early = datetime(1999, 1, 1, tzinfo=timezone.utc)
        bs = _fz_freeze(inputs, header=_fz_header_input(as_of=early))
        _fz_reject_bytes(bs, _fz_prov(bs, header=json.loads(bs)["header"]), "TIMESTAMP_FLOOR")

    def test_shuffled_nodes_sequence(self):
        # A two-node overlay whose nodes are out of fen order (strict-order owns this,
        # NOT the non-canonical-bytes check).
        payload = json.loads(_fz_freeze(_fz_two_node_inputs()))
        pair = payload["pairs"][_fz_two_node_index(payload)]
        pair["nodes"] = list(reversed(pair["nodes"]))
        bs = cal._canonical_dumps(payload)
        _fz_reject_bytes(bs, _fz_prov(bs, header=payload["header"]), "strictly ascending by fen")

    def test_out_of_order_phase_samples(self):
        def p(pl):
            pl["pairs"][2]["phase_samples"] = [
                {"opening_interval_len": 8, "middle_ply": None, "end_ply": None},
                {"opening_interval_len": 4, "middle_ply": None, "end_ply": None},
            ]
        _fz_reject(p, "nondecreasing")


def _fz_two_node_inputs():
    """A cohort where one release-guard pair carries a genuine two-node overlay (both
    nodes black-to-move), for shuffle / strict-order tests."""
    root, e4 = _fz_root(), _fz_e4()
    # second black-to-move node: after 1.e4 e5 2.Nf3.
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    e5_fen = normalize_fen(board.fen())
    board.push_uci("g1f3")
    nf3_fen = normalize_fen(board.fen())
    overlay = EvidenceOverlay(14, "black")
    overlay.nodes[e4] = NodeEvidence(
        fen=e4, quality_sum=10.0, quality_count=10,
        session_ids={f"s{i}" for i in range(10)},
        live_attempts=10, live_passes=10, live_fails=0,
        last_live_at=_FZ_AS_OF - timedelta(days=1),
    )
    overlay.nodes[nf3_fen] = NodeEvidence(
        fen=nf3_fen, quality_sum=15.0, quality_count=15,
        session_ids={f"s{i}" for i in range(5, 20)},
        live_attempts=15, live_passes=15, live_fails=0,
        last_live_at=_FZ_AS_OF - timedelta(days=1),
    )
    overlay.edges[(root, e4)] = EdgeEvidence(
        root, e4, "e2e4", traversal_count=10, live_attempts=10, live_passes=10, live_fails=0,
        quality_sum=10.0, quality_count=10,
    )
    overlay.edges[(e5_fen, nf3_fen)] = EdgeEvidence(
        e5_fen, nf3_fen, "g1f3", traversal_count=15, live_attempts=15, live_passes=15, live_fails=0,
        quality_sum=15.0, quality_count=15,
    )
    overlay.source_counts["session_eval"] = 25
    inputs = _fz_inputs()
    inputs[1] = cal.CapturedPairInput(overlay, "release_guard", 5, "fp-14b")
    return inputs


def _fz_two_node_index(payload):
    return next(i for i, pr in enumerate(payload["pairs"]) if len(pr["nodes"]) == 2)


def _fz_shared_session_inputs():
    """A release-guard pair with two nodes SHARING session "a": node 1.e4 has {a},
    node 1.e4 e5 2.Nf3 has {a, b}. Session "a" touches both nodes (the legit cross-node
    case → token g0 appears on both), so game_count over the union is 2."""
    root, e4 = _fz_root(), _fz_e4()
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    e5 = normalize_fen(board.fen())
    board.push_uci("g1f3")
    nf3 = normalize_fen(board.fen())
    overlay = EvidenceOverlay(14, "black")
    overlay.nodes[e4] = NodeEvidence(
        fen=e4, quality_sum=1.0, quality_count=1, session_ids={"a"},
        live_attempts=1, live_passes=1, live_fails=0,
        last_live_at=_FZ_AS_OF - timedelta(days=1),
    )
    overlay.nodes[nf3] = NodeEvidence(
        fen=nf3, quality_sum=2.0, quality_count=2, session_ids={"a", "b"},
        live_attempts=2, live_passes=2, live_fails=0,
        last_live_at=_FZ_AS_OF - timedelta(days=1),
    )
    overlay.edges[(root, e4)] = EdgeEvidence(
        root, e4, "e2e4", traversal_count=1, live_attempts=1, live_passes=1, live_fails=0,
        quality_sum=1.0, quality_count=1,
    )
    overlay.edges[(e5, nf3)] = EdgeEvidence(
        e5, nf3, "g1f3", traversal_count=2, live_attempts=2, live_passes=2, live_fails=0,
        quality_sum=2.0, quality_count=2,
    )
    overlay.source_counts["session_eval"] = 3
    inputs = _fz_inputs()
    inputs[1] = cal.CapturedPairInput(overlay, "release_guard", 5, "fp-14b")
    return inputs


class TestCanonicalByteReencode:
    """Byte-level noncanonicality that survives a full semantic pass: whitespace,
    unsorted object keys, exponent-form floats, a -0.0. Owned by the re-encode check
    even with a matching provenance digest."""

    def test_positive_control_loads_green(self):
        bs = _fz_freeze()
        assert _fz_load(bs).artifact_sha256 == hashlib.sha256(bs).hexdigest()

    def test_whitespace_rejected(self):
        payload = json.loads(_fz_freeze())
        raw = json.dumps(payload, sort_keys=True).encode("ascii")  # default separators add spaces
        _fz_reject_bytes(raw, _fz_prov(raw), "non-canonical bytes", cal.ArtifactCanonicalBytesError)

    def test_unsorted_keys_rejected(self):
        payload = json.loads(_fz_freeze())
        reordered = {"pairs": payload["pairs"], "header": payload["header"]}  # pairs before header
        raw = json.dumps(reordered, separators=(",", ":")).encode("ascii")
        _fz_reject_bytes(raw, _fz_prov(raw), "non-canonical bytes", cal.ArtifactCanonicalBytesError)

    def test_exponent_float_rejected(self):
        bs = _fz_freeze()
        assert b'"quality_sum":25.0' in bs
        raw = bs.replace(b'"quality_sum":25.0', b'"quality_sum":25e0', 1)
        _fz_reject_bytes(raw, _fz_prov(raw), "non-canonical bytes", cal.ArtifactCanonicalBytesError)

    def test_negative_zero_rejected(self):
        payload = json.loads(_fz_freeze())
        payload["pairs"][2]["nodes"][0]["quality_sum"] = -0.0
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
        assert b"-0.0" in raw
        _fz_reject_bytes(raw, _fz_prov(raw), "non-canonical bytes", cal.ArtifactCanonicalBytesError)


class TestProducerCanonicalStructure:
    """Self-consistent artifacts (valid types + matching digest) the canonical producer
    could NEVER emit — the loader asserts the producer's structure directly."""

    def test_misaligned_pair_id(self):
        def p(pl):
            pl["pairs"][0]["pair_id"] = "pair-05"
        _fz_reject(p, "array-order-aligned")

    def test_gap_in_pair_ids(self):
        # Drop pair-01, leaving pair-00, pair-02, pair-03 → index 1 expects pair-01.
        def p(pl):
            del pl["pairs"][1]
            pl["header"]["pair_count"] = 3
        _fz_reject(p, "array-order-aligned")

    def test_misaligned_surrogate(self):
        def p(pl):
            pl["pairs"][0]["surrogate_user_id"] = 99
        _fz_reject(p, "array-order-aligned")

    def test_noncontiguous_subject_block(self):
        # Force the two release_guard subject to reappear after a different subject by
        # relabeling so subjects interleave: subject-00, subject-01, subject-00, subject-01.
        def p(pl):
            pl["pairs"][0]["subject_id"] = "subject-00"
            pl["pairs"][1]["subject_id"] = "subject-01"
            pl["pairs"][2]["subject_id"] = "subject-00"
            pl["pairs"][3]["subject_id"] = "subject-01"
        _fz_reject(p, "non-contiguous")

    def test_duplicate_subject_color(self):
        # Make the two same-subject release-guard records BOTH white (copying pair-02's
        # white node into pair-03 so the active-color check passes), leaving a duplicate
        # (subject-02, white) for the subject-structure check to catch.
        def perturb(pl):
            pl["pairs"][3]["player_color"] = "white"
            pl["pairs"][3]["nodes"] = copy.deepcopy(pl["pairs"][2]["nodes"])
            for node in pl["pairs"][3]["nodes"]:
                node["session_tokens"] = [t.replace("pair-02", "pair-03") for t in node["session_tokens"]]
        _fz_reject(perturb, "duplicate (subject_id, player_color)")

    def test_session_token_union_gap(self):
        # g0, g2 but no g1 → the pair's node token set has a hole.
        def p(pl):
            node = pl["pairs"][2]["nodes"][0]
            node["session_tokens"] = ["pair-02-g0", "pair-02-g2"]
            # keep counts consistent so the union check (not a count check) fires.
        _fz_reject(p, "contiguous zero-based")

    def test_session_tokens_lexical_order(self):
        # g10 before g2 → not strictly ascending by NUMERIC k.
        overlay = _fz_overlay(2, "black", 25)
        inputs = _fz_inputs()
        inputs[2] = cal.CapturedPairInput(overlay, "quantile", 3, "fp-2b")
        payload = json.loads(_fz_freeze(inputs))
        node = payload["pairs"][2]["nodes"][0]
        node["session_tokens"] = ["pair-02-g10", "pair-02-g2"]
        bs = cal._canonical_dumps(payload)
        _fz_reject_bytes(bs, _fz_prov(bs, header=payload["header"]),
                         "strictly ascending by numeric token index")


class TestSplitLoadGuard:
    def test_schema_version_before_integrity(self):
        # A wrong schema_version fails Phase A even if nothing else is wrong.
        payload = json.loads(_fz_freeze())
        payload["header"]["schema_version"] = 7
        bs = cal._canonical_dumps(payload)
        _fz_reject_bytes(bs, _fz_prov(bs), "unsupported schema", cal.UnsupportedArtifactSchemaError)

    def test_integrity_field_mismatch(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs, graph_fingerprint="tampered")
        _fz_reject_bytes(bs, prov, "integrity", cal.ArtifactIntegrityError)

    def test_integrity_digest_mismatch(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs)
        prov = prov.replace(hashlib.sha256(bs).hexdigest().encode(), (b"0" * 64), 1)
        _fz_reject_bytes(bs, prov, "digest", cal.ArtifactIntegrityError)

    # Phase-C drift, INVERTED so it is producer-parity too (g-p4ih-capture): put the drifted
    # value in the header AND mirror it into the record so integrity passes, then load under
    # the NORMAL binding — which now disagrees with the header. validate_capture_candidate
    # promises to rerun the full split guard including these exact fields, so it must produce
    # the identical drift diagnostic. (The older form injected a custom binding the self-check
    # could not see, which left precisely this guarantee untested.) Same pattern as the
    # mirrored min_observations / cohort_rules rows below.
    def _drift_header(self, field, drifted_value):
        payload = json.loads(_fz_freeze())
        payload["header"][field] = drifted_value
        bs = cal._canonical_dumps(payload)
        prov = _fz_prov(bs, header=payload["header"])  # record MIRRORS the drifted value
        return _fz_reject_bytes(bs, prov, field, cal.ArtifactScoringValidityError)

    def test_scoring_validity_graph_drift(self):
        msg = self._drift_header("graph_fingerprint", "drifted")
        assert "drifted from the current runtime" in msg

    def test_scoring_validity_derivation_drift(self):
        msg = self._drift_header("evidence_derivation_fingerprint", "old-derivation")
        assert "drifted from the current runtime" in msg

    def test_scoring_validity_release_key_drift(self):
        # A DIFFERENT but well-formed key (Phase A only requires a non-empty string), so the
        # rejection is the Phase-C drift and not a semantic malformation.
        msg = self._drift_header("release_guard_opening_key", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq -")
        assert "drifted from the current runtime" in msg

    def test_min_observations_mirrored_passes_ab_fails_c(self):
        payload = json.loads(_fz_freeze())
        payload["header"]["min_observations"] = 0
        bs = cal._canonical_dumps(payload)
        prov = _fz_prov(bs, header=payload["header"])  # record MIRRORS 0
        # The DEFAULT binding rejects this (min_observations must be DEFAULT_MIN_OBSERVATIONS),
        # and the self-check uses the default binding too — so parity holds.
        _fz_reject_bytes(bs, prov, "min_observations", cal.ArtifactScoringValidityError)

    def test_non_current_cohort_rules_mirrored_passes_ab_fails_c(self):
        payload = json.loads(_fz_freeze())
        payload["header"]["cohort_rules"] = "opening-cohort-rules-v2"
        bs = cal._canonical_dumps(payload)
        prov = _fz_prov(bs, header=payload["header"])  # format-valid, mirrored
        _fz_reject_bytes(bs, prov, "cohort_rules", cal.ArtifactScoringValidityError)

    def test_model_version_bump_still_loads(self):
        # A captured sm-v2-3 artifact loads green after an sm-v2-4 bump: raw overlays
        # are reusable across model versions (captured_model_version is provenance-only).
        bs = _fz_freeze(header=_fz_header_input(captured_model_version="sm-v2-3"))
        cohort = _fz_load(bs)  # runtime binding carries no model version at all
        assert cohort.header.captured_model_version == "sm-v2-3"

    def test_str_artifact_bytes_rejected(self):
        bs = _fz_freeze()
        _fz_reject_bytes(bs.decode(), _fz_prov(bs), "artifact_bytes must be bytes", TypeError)

    def test_str_provenance_bytes_rejected(self):
        bs = _fz_freeze()
        _fz_reject_bytes(bs, _fz_prov(bs).decode(), "provenance_bytes must be bytes", TypeError)


class TestProvenanceRecordSchema:
    def test_stray_row_bearing_key_rejected(self):
        bs = _fz_freeze()
        record = json.loads(_fz_prov(bs))
        record["pairs"] = []
        prov = cal._canonical_dumps(record)
        _fz_reject_bytes(bs, prov, "unknown key", cal.ProvenanceRecordError)

    def test_duplicate_object_key_rejected(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs)
        raw = prov.replace(b'"pair_count":4', b'"pair_count":4,"pair_count":4', 1)
        _fz_reject_bytes(bs, raw, "duplicate JSON object key", cal.ProvenanceRecordError)

    def test_format_malformed_cohort_rules_rejected(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs, cohort_rules="free text")
        _fz_reject_bytes(bs, prov, "opening-cohort-rules", cal.ProvenanceRecordError)

    def test_format_malformed_captured_model_version_rejected(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs, captured_model_version="leaked")
        _fz_reject_bytes(bs, prov, "sm-v", cal.ProvenanceRecordError)

    def test_missing_required_key_rejected(self):
        bs = _fz_freeze()
        record = json.loads(_fz_prov(bs))
        del record["roots_fingerprint"]
        prov = cal._canonical_dumps(record)
        _fz_reject_bytes(bs, prov, "missing required key", cal.ProvenanceRecordError)

    def test_malformed_sha256_rejected(self):
        bs = _fz_freeze()
        prov = _fz_prov(bs, sha256="ABC123")
        _fz_reject_bytes(bs, prov, "sha256", cal.ProvenanceRecordError)


class TestCaptureAttestation:
    """g-p4ih-producer-bind: the four capture-attestation fields are presence+format
    validated on BOTH sides, mirrored by integrity, and NEVER equality-gated at load."""

    _FIELDS = (
        "capture_scorer_source_digest",
        "capture_source_revision",
        "capture_python_version",
        "capture_chess_version",
    )
    _MALFORMED = {
        "capture_scorer_source_digest": "ABC123",  # bad length + non-lowercase-hex
        "capture_source_revision": "ab" * 19 + "a",  # 39 hex chars, not 40
        "capture_python_version": "PyPy 3.12.1",  # non-CPython implementation
        "capture_chess_version": "1.11",  # two segments violate ^\d+.\d+.\d+$
    }

    def test_round_trip_green(self):
        cohort = _fz_load(_fz_freeze())
        h = cohort.header
        assert h.capture_scorer_source_digest == _FZ_CAPTURE_DIGEST
        assert h.capture_source_revision == _FZ_CAPTURE_REVISION
        assert h.capture_python_version == _FZ_CAPTURE_PYTHON
        assert h.capture_chess_version == _FZ_CAPTURE_CHESS

    @pytest.mark.parametrize("field", _FIELDS)
    def test_header_missing_field_distinct_error(self, field):
        def p(pl):
            del pl["header"][field]
        msg = _fz_reject(p, "missing required key")
        assert field in msg

    @pytest.mark.parametrize("field", _FIELDS)
    def test_record_missing_field_distinct_error(self, field):
        bs = _fz_freeze()
        record = json.loads(_fz_prov(bs))
        del record[field]
        msg = _fz_reject_bytes(bs, cal._canonical_dumps(record), "missing required key",
                               cal.ProvenanceRecordError)
        assert field in msg

    @pytest.mark.parametrize("field", _FIELDS)
    def test_header_malformed_field_distinct_error(self, field):
        def p(pl):
            pl["header"][field] = self._MALFORMED[field]
        msg = _fz_reject(p, f"header.{field}")
        assert "missing required key" not in msg

    @pytest.mark.parametrize("field", _FIELDS)
    def test_record_malformed_field_distinct_error(self, field):
        bs = _fz_freeze()
        prov = _fz_prov(bs, **{field: self._MALFORMED[field]})
        _fz_reject_bytes(bs, prov, f"provenance.{field}", cal.ProvenanceRecordError)

    def test_python_version_without_implementation_rejected(self):
        # platform.python_version() alone is NOT enough: the byte-stability contract is
        # CPython-only, so the implementation must be pinned inside the value.
        def p(pl):
            pl["header"]["capture_python_version"] = "3.12.1"
        _fz_reject(p, "capture_python_version")

    def test_chess_version_suffixed_rejected(self):
        # The explicit malformed-value proof for the ^\d+.\d+.\d+$ grammar; if
        # python-chess ever ships a suffixed release, widening the regex is a
        # deliberate change caught here.
        def p(pl):
            pl["header"]["capture_chess_version"] = "1.11.2rc1"
        _fz_reject(p, "capture_chess_version")

    @pytest.mark.parametrize("field,other", [
        ("capture_scorer_source_digest", "e2" * 32),
        ("capture_source_revision", "cd" * 20),
        ("capture_python_version", "CPython 3.13.2"),
        ("capture_chess_version", "1.12.0"),
    ])
    def test_header_record_disagreement_is_integrity_mismatch(self, field, other):
        bs = _fz_freeze()
        prov = _fz_prov(bs, **{field: other})  # format-valid on BOTH sides
        _fz_reject_bytes(bs, prov, field, cal.ArtifactIntegrityError)

    def test_attestation_differing_from_current_environment_loads_green(self):
        """THE NON-GATING DECISION (g-p4ih-producer-bind): the attestation is recorded
        and reviewed, never equality-gated at load. Do NOT "fix" a red version of this
        test by extending _check_scoring_validity or RuntimeBinding — an equality gate
        would reimpose re-capture on every dependency bump and create standing pressure
        to re-run authorized PRODUCTION captures of private data."""
        current_python = f"{platform.python_implementation()} {platform.python_version()}"
        different_python = "CPython 3.9.1" if current_python != "CPython 3.9.1" else "CPython 3.8.1"
        different_chess = "9.9.9" if chess.__version__ != "9.9.9" else "9.9.8"
        different_digest = "0" * 64 if cal.scorer_source_digest() != "0" * 64 else "1" * 64
        bs = _fz_freeze(header=_fz_header_input(
            capture_scorer_source_digest=different_digest,
            capture_source_revision="f" * 40,  # certainly not the resolved HEAD
            capture_python_version=different_python,
            capture_chess_version=different_chess,
        ))
        cohort = cal.load_frozen_artifact(
            bs, _fz_prov(bs, header=json.loads(bs)["header"]), _fz_rb()
        )
        assert cohort.header.capture_python_version == different_python  # loaded GREEN

    def test_genuine_v1_artifact_gets_version_diagnostic(self):
        """REAL prior-schema bytes — v1 key set, WITHOUT the four capture fields — must
        fail as UnsupportedArtifactSchemaError, never as a missing-keys error. (A v2
        payload with only its schema_version changed still carries the four keys and
        would pass the key set, so this builds genuinely v1-shaped bytes.)"""
        payload = json.loads(_fz_freeze())
        for field in self._FIELDS:
            del payload["header"][field]
        payload["header"]["schema_version"] = 1
        bs = cal._canonical_dumps(payload)
        msg = _fz_reject_bytes(bs, _fz_prov(bs), "unsupported schema",
                               cal.UnsupportedArtifactSchemaError)
        assert "missing required" not in msg

    def test_genuine_v1_record_gets_version_diagnostic(self):
        bs = _fz_freeze()
        record = json.loads(_fz_prov(bs))
        for field in self._FIELDS:
            del record[field]
        record["schema_version"] = 1
        msg = _fz_reject_bytes(bs, cal._canonical_dumps(record), "unsupported schema",
                               cal.UnsupportedArtifactSchemaError)
        assert "missing required" not in msg


class TestCohortMembership:
    def test_below_threshold_quantile_pair_rejected(self):
        bs = _fz_freeze(_fz_inputs(quantile_obs=(5, 22)))
        _fz_reject_bytes(bs, _fz_prov(bs, header=json.loads(bs)["header"]),
                         "below header.min_observations")

    def test_release_guard_below_threshold_ok(self):
        # Guards are exempt from the observation threshold (release-gate-only).
        bs = _fz_freeze(_fz_inputs(guard_obs=(3, 3)))
        cohort = _fz_load(bs)
        assert sum(1 for p in cohort.pairs if p.cohort_role == "release_guard") == 2


class TestCrossFieldTelemetry:
    def test_consistent_telemetry_loads_green(self):
        cohort = _fz_load(_fz_freeze())
        assert cohort.pairs

    def test_perturbing_source_counts_rejects(self):
        def p(pl):
            pl["pairs"][2]["source_counts"] = {"session_eval": 24}
        _fz_reject(p, "telemetry does not describe overlay")


class TestOverlayTelemetryRoundtrip:
    def test_telemetry_reconstructs_onto_overlay(self):
        overlay = _fz_overlay(2, "black", 25)
        overlay.excluded_sessions = 4
        overlay.phase_samples = [PhaseSample(4, 8, 20), PhaseSample(6, None, None)]
        overlay.source_counts = Counter({"session_eval": 20, "analysis_cache": 5})
        inputs = _fz_inputs()
        inputs[2] = cal.CapturedPairInput(overlay, "quantile", 3, "fp-2b")
        cohort = _fz_load(_fz_freeze(inputs))
        loaded = next(p for p in cohort.pairs if p.surrogate_user_id == 1).overlay
        assert loaded.excluded_sessions == 4
        assert dict(loaded.source_counts) == {"session_eval": 20, "analysis_cache": 5}
        assert sorted(loaded.phase_samples, key=lambda s: s.opening_interval_len) == [
            PhaseSample(4, 8, 20), PhaseSample(6, None, None)
        ]


def _fz_game_count(overlay) -> int:
    return len({s for node in overlay.nodes.values() for s in node.session_ids})


class TestSessionTokenUnion:
    def test_game_count_union_matches_real_uuids(self):
        # A two-node overlay where session s5..s9 touch BOTH nodes: the cross-node union
        # of real ids equals the union of frozen tokens exactly.
        inputs = _fz_two_node_inputs()
        original = inputs[1].overlay
        orig_games = _fz_game_count(original)
        header, pairs = cal.validate_artifact_bytes(_fz_freeze(inputs))
        loaded = next(p for p in pairs if len(p.overlay.nodes) == 2).overlay
        assert _fz_game_count(loaded) == orig_games == 20

    def test_leading_zero_token_string_rejected(self):
        # `g00` parses to k=0 via int(...) but is NOT the canonical spelling `g0`.
        with pytest.raises(cal.ArtifactSemanticError, match="canonical spelling"):
            cal._validate_session_tokens(["pair-00-g00"], "pair-00", "x")
        with pytest.raises(cal.ArtifactSemanticError, match="canonical spelling"):
            cal._validate_session_tokens(["pair-00-g0", "pair-00-g015"], "pair-00", "x")
        # Canonical tokens still pass.
        assert cal._validate_session_tokens(["pair-00-g0", "pair-00-g1"], "pair-00", "x") == [
            "pair-00-g0", "pair-00-g1"
        ]

    def test_alias_token_on_different_node_rejected(self):
        # The game_count-inflation attack: `pair-XX-g0` on one node and `pair-XX-g00` on
        # ANOTHER node both parse to k=0, so a NUMERIC-suffix contiguity check would pass,
        # but reconstruction keeps the two distinct strings as session_ids and inflates
        # the cross-node game_count union. Canonical bytes + matching provenance.
        inputs = _fz_shared_session_inputs()
        payload = json.loads(_fz_freeze(inputs))
        idx = _fz_two_node_index(payload)
        pair = payload["pairs"][idx]
        pid = pair["pair_id"]

        # Baseline: the honest artifact loads with game_count == 2 (session "a" shared).
        _hdr, base_pairs = cal.validate_artifact_bytes(cal._canonical_dumps(payload))
        base_overlay = next(p for p in base_pairs if len(p.overlay.nodes) == 2).overlay
        assert _fz_game_count(base_overlay) == 2

        # Attack: on the node holding two tokens, rewrite its `g0` as the alias `g00`
        # (a distinct string on a different node from the other `g0`). Numeric union is
        # unchanged {0, 1}; the string union gains a third member.
        two_tok_node = max(pair["nodes"], key=lambda n: len(n["session_tokens"]))
        assert f"{pid}-g0" in two_tok_node["session_tokens"]
        two_tok_node["session_tokens"] = [
            f"{pid}-g00" if t == f"{pid}-g0" else t for t in two_tok_node["session_tokens"]
        ]
        bs = cal._canonical_dumps(payload)
        # Provenance mirrors the (canonical, self-consistent) bytes.
        _fz_reject_bytes(bs, _fz_prov(bs, header=payload["header"]), "canonical spelling")

    def test_alias_token_union_string_based(self):
        # Belt-and-suspenders: even a token that clears the per-token spelling check
        # (e.g. a genuine gap g0,g2) is caught by the STRING union check, which counts
        # exactly what _aggregate_metadata unions — not numeric suffixes.
        payload = json.loads(_fz_freeze())
        payload["pairs"][2]["nodes"][0]["session_tokens"] = ["pair-02-g0", "pair-02-g2"]
        bs = cal._canonical_dumps(payload)
        _fz_reject_bytes(bs, _fz_prov(bs, header=payload["header"]), "contiguous zero-based")


class TestArtifactShapeGuard:
    def test_valid_shape_ok(self):
        cal.assert_artifact_shape(_fz_load(_fz_freeze()).pairs)

    def _pairs_with(self, roles_colors):
        pairs = []
        for i, (role, color) in enumerate(roles_colors):
            subject = "subject-99" if role == "release_guard" else f"subject-{i:02d}"
            pairs.append(cal.LoadedPair(
                pair_id=f"pair-{i:02d}", subject_id=subject, cohort_role=role,
                surrogate_user_id=i + 1, player_color=color, evidence_seq=0,
                inputs_fingerprint="fp", overlay=EvidenceOverlay(i + 1, color),
            ))
        return pairs

    def test_three_release_guards_rejected(self):
        pairs = self._pairs_with([
            ("quantile", "white"), ("quantile", "black"),
            ("release_guard", "white"), ("release_guard", "black"), ("release_guard", "white"),
        ])
        with pytest.raises(cal.ReleaseGuardShapeError, match="exactly two release_guard"):
            cal.assert_artifact_shape(pairs)

    def test_same_color_guards_rejected(self):
        pairs = self._pairs_with([
            ("quantile", "white"), ("quantile", "black"),
            ("release_guard", "white"), ("release_guard", "white"),
        ])
        with pytest.raises(cal.ReleaseGuardShapeError, match="exactly \\{white, black\\}"):
            cal.assert_artifact_shape(pairs)

    def test_split_guard_subjects_rejected(self):
        pairs = [
            cal.LoadedPair("pair-00", "subject-00", "quantile", 1, "white", 0, "fp", EvidenceOverlay(1, "white")),
            cal.LoadedPair("pair-01", "subject-01", "quantile", 2, "black", 0, "fp", EvidenceOverlay(2, "black")),
            cal.LoadedPair("pair-02", "subject-02", "release_guard", 3, "white", 0, "fp", EvidenceOverlay(3, "white")),
            cal.LoadedPair("pair-03", "subject-03", "release_guard", 4, "black", 0, "fp", EvidenceOverlay(4, "black")),
        ]
        with pytest.raises(cal.ReleaseGuardShapeError, match="share one subject_id"):
            cal.assert_artifact_shape(pairs)

    def test_zero_quantile_pairs_rejected(self):
        pairs = self._pairs_with([("release_guard", "white"), ("release_guard", "black")])
        with pytest.raises(cal.ReleaseGuardShapeError, match="too-few-quantile-pairs"):
            cal.assert_artifact_shape(pairs)

    def test_one_quantile_pair_rejected(self):
        pairs = self._pairs_with([
            ("quantile", "white"), ("release_guard", "white"), ("release_guard", "black"),
        ])
        with pytest.raises(cal.ReleaseGuardShapeError, match="too-few-quantile-pairs"):
            cal.assert_artifact_shape(pairs)


class _FakeScored:
    def __init__(self, player_color, grid):
        self.player_color = player_color
        self.grid = grid


class _FakePairScore:
    def __init__(self, named_score_map=None, named_scores=None):
        self.named_score_map = named_score_map or {}
        self.named_scores = named_scores if named_scores is not None else []


class TestScoreShapeGuard:
    def _cell(self):
        return cal.CURRENT_SM_V2_3_CELL

    def _valid_guards(self):
        cell = self._cell()
        white = _FakeScored("white", {cell: _FakePairScore({cal.RELEASE_GUARD_OPENING_KEY: 55.0}, [55.0])})
        black = _FakeScored("black", {cell: _FakePairScore(
            {cal.RELEASE_GUARD_OPENING_KEY: 40.0, cal.RELEASE_GUARD_CHILD_OPENING_KEY: 60.0}, [40.0]
        )})
        return [white, black]

    def test_valid_shape_ok(self):
        cal.assert_release_guard_score_shape(self._valid_guards(), [self._cell()], _fz_rb())

    def test_missing_opening_key_rejected(self):
        cell = self._cell()
        guards = self._valid_guards()
        guards[0].grid[cell].named_score_map = {}
        with pytest.raises(cal.ReleaseGuardShapeError, match="missing named_score_map"):
            cal.assert_release_guard_score_shape(guards, [cell], _fz_rb())

    def test_missing_child_key_on_black_rejected(self):
        cell = self._cell()
        guards = self._valid_guards()
        del guards[1].grid[cell].named_score_map[cal.RELEASE_GUARD_CHILD_OPENING_KEY]
        with pytest.raises(cal.ReleaseGuardShapeError, match="missing named_score_map"):
            cal.assert_release_guard_score_shape(guards, [cell], _fz_rb())

    def test_out_of_range_score_rejected(self):
        cell = self._cell()
        guards = self._valid_guards()
        guards[0].grid[cell].named_score_map[cal.RELEASE_GUARD_OPENING_KEY] = 150.0
        with pytest.raises(cal.ReleaseGuardShapeError, match="out of \\[0, 100\\]"):
            cal.assert_release_guard_score_shape(guards, [cell], _fz_rb())

    def test_non_finite_score_rejected(self):
        cell = self._cell()
        guards = self._valid_guards()
        guards[0].grid[cell].named_score_map[cal.RELEASE_GUARD_OPENING_KEY] = float("nan")
        with pytest.raises(cal.ReleaseGuardShapeError, match="not finite"):
            cal.assert_release_guard_score_shape(guards, [cell], _fz_rb())


class TestMinQuantileScoresPerCell:
    def _cell(self):
        return cal.CURRENT_SM_V2_3_CELL

    def test_two_pooled_scores_ok(self):
        cell = self._cell()
        pairs = [_FakeScored("white", {cell: _FakePairScore(named_scores=[1.0])}),
                 _FakeScored("black", {cell: _FakePairScore(named_scores=[2.0])})]
        cal.assert_min_quantile_scores_per_cell(pairs, [cell])

    def test_zero_pooled_scores_rejected(self):
        cell = self._cell()
        pairs = [_FakeScored("white", {cell: _FakePairScore(named_scores=[])}),
                 _FakeScored("black", {cell: _FakePairScore(named_scores=[])})]
        with pytest.raises(cal.ReleaseGuardShapeError, match="too-few-pooled-quantile-scores"):
            cal.assert_min_quantile_scores_per_cell(pairs, [cell])

    def test_one_pooled_score_rejected(self):
        cell = self._cell()
        pairs = [_FakeScored("white", {cell: _FakePairScore(named_scores=[1.0])}),
                 _FakeScored("black", {cell: _FakePairScore(named_scores=[])})]
        with pytest.raises(cal.ReleaseGuardShapeError, match="too-few-pooled-quantile-scores"):
            cal.assert_min_quantile_scores_per_cell(pairs, [cell])


class TestScoreOverlayPseudonyms:
    def _fixtures(self):
        root, e4 = _fz_root(), _fz_e4()
        graph = OpeningGraph(
            {root: OpeningGraphNode(root, active_color(root)), e4: OpeningGraphNode(e4, active_color(e4))},
            root,
        )
        roots = OpeningRoots(
            {e4: OpeningRoot(opening_key=e4, opening_name="KP", opening_family="F", eco=None, depth=0, parent_keys=frozenset(), child_keys=frozenset())},
            {e4: frozenset([e4])},
        )
        overlay = EvidenceOverlay(1, "white")
        overlay.edges[(root, e4)] = EdgeEvidence(root, e4, "e2e4", live_attempts=2)
        return graph, overlay, roots

    def test_live_path_leaves_new_fields_none(self):
        graph, overlay, roots = self._fixtures()
        # Same user_id for both calls — the only difference is the added pseudonym
        # kwargs — so every existing field must stay field-for-field equal.
        with patch("time.perf_counter", return_value=0.0):
            live = cal.score_overlay(
                7, "white", graph, overlay, roots, as_of=cal.SYNTHETIC_AS_OF
            )
            frozen = cal.score_overlay(
                7, "white", graph, overlay, roots,
                as_of=cal.SYNTHETIC_AS_OF,
                pair_id="pair-00", subject_id="subject-00", cohort_role="quantile",
            )
        assert (live.pair_id, live.subject_id, live.cohort_role) == (None, None, None)
        # Every existing field is field-for-field equal between the two calls (with the
        # clock pinned), so the added kwargs do not perturb live scoring.
        for f in ("named_scores", "named_score_map", "synthetic_score", "observation_total",
                  "source_counts", "excluded_sessions", "phase_samples", "user_id", "player_color"):
            assert getattr(live, f) == getattr(frozen, f), f

    def test_frozen_path_carries_pseudonyms(self):
        graph, overlay, roots = self._fixtures()
        frozen = cal.score_overlay(
            7, "white", graph, overlay, roots,
            as_of=cal.SYNTHETIC_AS_OF,
            pair_id="pair-00", subject_id="subject-00", cohort_role="quantile",
        )
        assert frozen.pair_id == "pair-00"
        assert frozen.subject_id == "subject-00"
        assert frozen.cohort_role == "quantile"
        assert frozen.user_id == 7  # surrogate rides the int user_id


class TestHandoffContract:
    def test_loaded_cohort_shape(self):
        bs = _fz_freeze()
        cohort = _fz_load(bs)
        assert isinstance(cohort.header, cal.LoadedHeader)
        assert cohort.header.as_of == _FZ_AS_OF
        assert cohort.artifact_sha256 == hashlib.sha256(bs).hexdigest()
        for lp in cohort.pairs:
            assert isinstance(lp, cal.LoadedPair)
            assert isinstance(lp.overlay, EvidenceOverlay)
        first = cohort.pairs[0]
        assert first.pair_id == "pair-00"
        assert first.evidence_seq in (3, 5)
        assert first.inputs_fingerprint

    def test_freeze_returns_bytes_not_str(self):
        assert isinstance(_fz_freeze(), bytes)


# ---------------------------------------------------------------------------
# g-p4ih-replay-bind: as_of threading, source/runtime binding, build_selection_inputs
# ---------------------------------------------------------------------------


def _bsi_graph() -> OpeningGraph:
    """A minimal graph containing the two positions the _fz overlays touch (start + 1.e4)."""
    return _graph([["e2e4"]])


def _bsi_roots() -> OpeningRoots:
    """Roots with the King's-Pawn release-guard key so cohort scoring produces named
    scores. (The Caro child key need not be a root here — the release-guard SCORE-shape
    check is downstream in g-p4ih-selection, not in build_selection_inputs.)"""
    return _roots(_root(cal.RELEASE_GUARD_OPENING_KEY, "KP"))


def _bsi_inputs(quantile_colors=("black", "black")):
    """A cohort shaped for SCORING, not merely for freeze/load. Both quantile pairs are
    BLACK by default, so each pools a named score at the only root in _bsi_roots() and
    derive_cutoffs' ">= 2 pooled scores" precondition is satisfiable.

    _fz_inputs' white quantile pair does NOT: _fz_overlay puts a white pair's evidence at
    the start position, which sits ABOVE the release-guard root, so it legitimately scores
    zero named roots. That is the real shape the builder must reject — see
    TestPooledQuantilePrecondition, which uses exactly those inputs."""
    return [
        cal.CapturedPairInput(_fz_overlay(14, "white", 15), "release_guard", 5, "fp-14w"),
        cal.CapturedPairInput(_fz_overlay(14, "black", 15), "release_guard", 5, "fp-14b"),
        cal.CapturedPairInput(_fz_overlay(2, quantile_colors[0], 25), "quantile", 3, "fp-2"),
        cal.CapturedPairInput(_fz_overlay(3, quantile_colors[1], 22), "quantile", 3, "fp-3"),
    ]


def _bsi_artifact(tmp_path, *, as_of=_FZ_AS_OF, graph=None, roots=None, inputs=None):
    """Freeze a synthetic artifact + a schema-valid provenance record BOUND to the given
    graph/roots (their real fingerprints), written under tmp_path. Returns
    (graph, roots, artifact_path, provenance_path, as_of, provenance_bytes)."""
    graph = graph if graph is not None else _bsi_graph()
    roots = roots if roots is not None else _bsi_roots()
    inputs = inputs if inputs is not None else _bsi_inputs()
    header = cal.ArtifactHeaderInput(
        as_of=as_of,
        graph_fingerprint=graph.fingerprint,
        roots_fingerprint=roots.fingerprint,
        cache_epoch=7,
        captured_model_version="sm-v2-3",
        evidence_derivation_fingerprint=evidence_derivation_fingerprint(),
        capture_scorer_source_digest=_FZ_CAPTURE_DIGEST,
        capture_source_revision=_FZ_CAPTURE_REVISION,
        capture_python_version=_FZ_CAPTURE_PYTHON,
        capture_chess_version=_FZ_CAPTURE_CHESS,
    )
    art = cal.freeze_frozen_artifact(inputs, header)
    hdr = json.loads(art)["header"]
    rec = {k: hdr[k] for k in _FZ_MIRRORED}
    rec["sha256"] = hashlib.sha256(art).hexdigest()
    prov = cal._canonical_dumps(rec)
    art_path = tmp_path / "artifact.json"
    prov_path = tmp_path / "cohort_provenance.json"
    art_path.write_bytes(art)
    prov_path.write_bytes(prov)
    return graph, roots, art_path, prov_path, as_of, prov


_BACKEND_ROOT = Path(cal.__file__).resolve().parents[1]

_CHILD_BUILD = """
import json, sys
import test_calibrate_opening_scores as t
import scripts.calibrate_opening_scores_v2 as cal
try:
    si = cal._build_selection_inputs(
        sys.argv[1], provenance_path=sys.argv[2],
        graph=t._bsi_graph(), roots=t._bsi_roots(),
    )
    out = {"preexec": si.cohort.scorer_source_verified_preexec}
except cal.ScorerSourceUnstableError as e:
    out = {"error": type(e).__name__}
print(json.dumps(out))
"""

# A module whose two revisions are the SAME SIZE, so a timestamp-validated .pyc compiled
# from the first stays "valid" for the second once the mtime is restored.
_STALE_V1 = 'VALUE = "AAA"\n'
_STALE_V2 = 'VALUE = "BBB"\n'


def _write_preserving_mtime(path: Path, text: str) -> None:
    stat = path.stat()
    path.write_text(text)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))


# Bytecode-cache policy is INHERITED through the environment, so any child that a bytecode
# test depends on must have it stripped. Otherwise running the suite under
# PYTHONDONTWRITEBYTECODE=1 (or with a PYTHONPYCACHEPREFIX) silently changes what the child
# does — no .pyc is written, or it lands somewhere the parent is not looking, and the
# stale-cache tests stop testing a stale cache.
_BYTECODE_ENV_VARS = ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")


def _bytecode_fixture_env(**overrides) -> dict[str, str]:
    """os.environ with the inherited bytecode-cache policy removed, plus explicit overrides."""
    env = {k: v for k, v in os.environ.items() if k not in _BYTECODE_ENV_VARS}
    env.update(overrides)
    return env


def _run_child_build(artifact_path, provenance_path, *, env_digest,
                     pycache_prefix=None, write_bytecode=False):
    """Build selection inputs in a FRESH interpreter launched with (or without)
    SCORER_SOURCE_DIGEST_ENV already in its environment. The flag only means anything for
    an INHERITED digest, so it cannot be exercised by setenv inside this process — that is
    the very promotion the flag must refuse (see test_late_setenv_cannot_upgrade_the_flag).

    ``pycache_prefix`` / ``write_bytecode`` model what a launcher controls: a verified run
    needs an empty/fresh bytecode cache and no .pyc writing, or CPython can serve the
    scorer from bytecode it never checked against the source the digest hashes."""
    env = _bytecode_fixture_env()
    env.pop(cal.SCORER_SOURCE_DIGEST_ENV, None)
    if env_digest is not None:
        env[cal.SCORER_SOURCE_DIGEST_ENV] = env_digest
    if pycache_prefix is not None:
        env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix)
    if not write_bytecode:
        env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_BUILD, str(artifact_path), str(provenance_path)],
        capture_output=True, text=True, cwd=str(_BACKEND_ROOT), env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _clock_class(now_value=None, *, raise_on_now=False):
    """A ``datetime`` subclass whose ``.now`` is pinned/raising, so patching it over
    opening_rootcalc's ``datetime`` fakes the WALL clock without breaking datetime
    arithmetic/construction (as_of is a real datetime instance)."""
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            if raise_on_now:
                raise AssertionError("scoring path read the wall clock")
            return now_value
    return _Clock


class TestAsOfRequiredGuard:
    def test_score_overlay_requires_as_of(self):
        graph, overlay, roots, _e4 = _scored_overlay()
        with pytest.raises(TypeError):
            cal.score_overlay(1, "white", graph, overlay, roots)  # no as_of

    def test_score_pair_requires_as_of(self):
        graph, overlay, roots, _e4 = _scored_overlay()
        with patch.object(cal, "overlay_evidence", return_value=overlay):
            with pytest.raises(TypeError):
                cal.score_pair(MagicMock(), 1, "white", graph, roots)  # no as_of

    def test_score_pair_grid_requires_as_of(self):
        graph, overlay, roots, _e4 = _scored_overlay()
        with patch.object(cal, "overlay_evidence", return_value=overlay):
            with pytest.raises(TypeError):
                cal.score_pair_grid(MagicMock(), 1, "white", graph, roots,
                                    (cal.CURRENT_SM_V2_3_CELL,))  # no as_of


class TestLiveClockSingleSampling:
    def test_one_run_as_of_across_two_pairs(self, capsys):
        graph = _graph([["e2e4"]])
        overlay = EvidenceOverlay(7, "white")  # empty -> fast early return
        db = MagicMock()
        # datetime.now ADVANCES on every sample; a single-sampling main takes only the
        # first, so both pairs' grids must carry t0 even though the clock moved.
        t0 = datetime(2026, 7, 1, 8, 0, 0, tzinfo=timezone.utc)
        clock = MagicMock(side_effect=[t0 + timedelta(minutes=i) for i in range(10)])
        with patch.object(cal, "overlay_evidence", return_value=overlay), patch(
            "app.opening_cache.list_opening_score_candidate_pairs",
            return_value=[(7, "white"), (8, "black")],
        ), patch.object(cal, "get_opening_graph", return_value=graph), patch.object(
            cal, "get_opening_roots", return_value=_roots(_root(_positions(["e2e4"])[1]))
        ), patch.object(cal, "_utcnow", clock), patch.object(
            cal, "score_pair_grid", wraps=cal.score_pair_grid
        ) as spy:
            report = cal.main(["--min-observations", "0"], session_factory=_fake_factory(db))

        assert clock.call_count == 1  # sampled EXACTLY once
        seen = {call.kwargs["as_of"] for call in spy.call_args_list}
        assert spy.call_count == 2 and seen == {t0}  # both pairs under the one clock
        assert report["run_as_of"] == t0.isoformat()
        assert "Run clock (as_of): " in capsys.readouterr().out


class TestScorerSourceDigest:
    def test_exact_manifest_pin(self):
        # The app.* import CLOSURE of both entry points (g-p4ih-producer-bind) + the
        # declared dependency set + the scorer itself.
        assert cal.SCORER_SOURCE_FILES == (
            "backend/app/analysis_profiles.py",
            "backend/app/analysis_trust.py",
            "backend/app/centipawn_loss.py",
            "backend/app/database_url.py",
            "backend/app/db.py",
            "backend/app/evidence_contracts.py",
            "backend/app/fen.py",
            "backend/app/game_phase.py",
            "backend/app/models.py",
            "backend/app/move_classification.py",
            "backend/app/opening_aggregate.py",
            "backend/app/opening_cache.py",
            "backend/app/opening_evidence.py",
            "backend/app/opening_graph.py",
            "backend/app/opening_quality.py",
            "backend/app/opening_rootcalc.py",
            "backend/app/opening_roots.py",
            "backend/app/opening_score_scheduler.py",
            "backend/app/position_analysis_policy.py",
            "backend/app/position_analysis_repo.py",
            "backend/app/posthog_client.py",
            "backend/requirements.txt",
            "backend/scripts/calibrate_opening_scores_v2.py",
        )
        assert list(cal.SCORER_SOURCE_FILES) == sorted(cal.SCORER_SOURCE_FILES)

    def test_import_completeness_guard_passes_on_real_tree(self):
        cal.check_scorer_source_manifest()  # no raise

    def test_import_completeness_guard_fails_when_import_missing(self):
        # opening_rootcalc imports app.opening_evidence; dropping it must be caught.
        reduced = tuple(p for p in cal.SCORER_SOURCE_FILES
                        if p != "backend/app/opening_evidence.py")
        with pytest.raises(cal.ScorerSourceManifestError):
            cal.check_scorer_source_manifest(reduced)

    @pytest.mark.parametrize("dropped", [
        "backend/app/analysis_profiles.py",
        "backend/app/analysis_trust.py",
        "backend/app/centipawn_loss.py",
        "backend/app/models.py",
        "backend/app/position_analysis_repo.py",
    ])
    def test_closure_guard_rejects_dropped_derivation_module(self, dropped):
        # The F1 hazard (g-p4ih-producer-bind): opening_evidence.py — the derivation
        # entry point capture runs — imports these; a manifest missing any of them
        # would let a dirty edit move captured evidence under an unchanged digest.
        reduced = tuple(p for p in cal.SCORER_SOURCE_FILES if p != dropped)
        with pytest.raises(cal.ScorerSourceManifestError, match="import-closure"):
            cal.check_scorer_source_manifest(reduced)

    def test_closure_walks_through_a_module_absent_from_the_manifest(self):
        # app.database_url is reached only THROUGH app.db. Dropping BOTH must still
        # report both: the walk follows a discovered module even when it is absent
        # from the manifest, so a gap cannot hide its own transitive imports.
        reduced = tuple(
            p for p in cal.SCORER_SOURCE_FILES
            if p not in ("backend/app/db.py", "backend/app/database_url.py")
        )
        closure = cal.scorer_imported_app_modules(reduced)
        assert {"app.db", "app.database_url"} <= closure
        with pytest.raises(cal.ScorerSourceManifestError):
            cal.check_scorer_source_manifest(reduced)

    def test_digest_stable_and_changes_with_bytes(self, tmp_path, monkeypatch):
        for rel in cal.SCORER_SOURCE_FILES:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"orig:" + rel.encode())
        monkeypatch.setattr(cal, "_REPO_ROOT", tmp_path)
        d1 = cal.scorer_source_digest()
        assert d1 == cal.scorer_source_digest()  # stable across two runs, identical bytes
        target = tmp_path / cal.SCORER_SOURCE_FILES[0]
        target.write_bytes(target.read_bytes() + b"x")
        d2 = cal.scorer_source_digest()
        assert d1 != d2 and d2 == cal.scorer_source_digest()  # changed, then stable

    def test_requirements_pins_chess_with_exact_equals(self):
        text = (cal._REPO_ROOT / "backend" / "requirements.txt").read_text()
        import re
        assert re.search(r"(?m)^chess==\d+\.\d+(\.\d+)?\s*$", text), \
            "chess must be pinned with an exact == in requirements.txt"


class TestSourceStabilityFence:
    """The digest must describe the code that ACTUALLY produced the scores. Python keeps
    running the modules it imported even after their files change, so a digest read at
    stamp time can name code that never ran. Every run is fenced: import snapshot ==
    open read == close read, or nothing is stamped."""

    def test_import_snapshot_matches_the_bytes_on_disk(self):
        assert cal._SCORER_SOURCE_DIGEST_AT_IMPORT == cal.scorer_source_digest()

    def test_fails_closed_when_source_moved_since_import(self, tmp_path, monkeypatch):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        ap.unlink()  # the fence must reject BEFORE anything is loaded or scored
        monkeypatch.setattr(cal, "_SCORER_SOURCE_DIGEST_AT_IMPORT", "0" * 64)
        with pytest.raises(cal.ScorerSourceUnstableError):  # not FileNotFoundError
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

    def test_fails_closed_when_source_changes_mid_run(self, tmp_path, monkeypatch):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        snap = cal._SCORER_SOURCE_DIGEST_AT_IMPORT
        # Open read matches the import snapshot; a manifest file is edited while the run
        # scores, so the close read differs -> the whole run is discarded.
        digest = MagicMock(side_effect=[snap, "f" * 64])
        monkeypatch.setattr(cal, "scorer_source_digest", digest)
        with pytest.raises(cal.ScorerSourceUnstableError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert digest.call_count == 2  # fence opened and closed

    def test_stamps_the_fenced_digest_and_reads_it_exactly_twice(self, tmp_path, monkeypatch):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        snap = cal._SCORER_SOURCE_DIGEST_AT_IMPORT
        digest = MagicMock(side_effect=[snap, snap])  # a 3rd read would raise StopIteration
        monkeypatch.setattr(cal, "scorer_source_digest", digest)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert si.cohort.scorer_source_digest == snap
        assert digest.call_count == 2  # the stamp reuses the fenced read, never a fresh one

    def test_late_setenv_cannot_upgrade_the_flag(self, tmp_path, monkeypatch):
        """THE regression test. scorer_source_verified_preexec means "a digest computed
        before this interpreter existed agreed with mine". If the env were read at scoring
        time, code running in THIS process — after the scorer was already compiled — could
        set the matching digest and mint that proof for itself. It must not be able to."""
        monkeypatch.setenv(cal.SCORER_SOURCE_DIGEST_ENV, cal.scorer_source_digest())
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert si.cohort.scorer_source_verified_preexec is False  # NOT promoted
        assert cal.build_winner_binding(
            si.cohort, cal.CURRENT_SM_V2_3_CELL
        ).scorer_source_verified_preexec is False

    # The env var is only meaningful when INHERITED, so the three cases below have to be
    # exercised in a fresh interpreter that was launched with it already set.

    def test_no_launcher_digest_stamps_unverified(self, tmp_path):
        _g, _r, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        assert _run_child_build(ap, pp, env_digest=None) == {"preexec": False}

    def test_inherited_matching_digest_stamps_verified(self, tmp_path):
        # A digest computed BEFORE the interpreter started predates the compilation of every
        # manifest file, so agreeing with it is what closes the compile window. The child
        # also gets a fresh, write-disabled bytecode cache, so every scorer module is
        # compiled from the source that digest hashes.
        _g, _r, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        digest = cal.scorer_source_digest()
        assert _run_child_build(
            ap, pp, env_digest=digest.upper() + " ", pycache_prefix=tmp_path / "pyc",
        ) == {"preexec": True}

    def test_inherited_disagreeing_digest_fails_closed(self, tmp_path):
        # The scorer moved while Python was compiling it: old code runs, new bytes hash,
        # and BOTH in-process fence reads agree. Only the inherited digest catches this.
        _g, _r, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        assert _run_child_build(ap, pp, env_digest="a" * 64) == {
            "error": "ScorerSourceUnstableError"
        }

    def test_verified_run_needs_a_fresh_bytecode_cache(self, tmp_path):
        # Same inherited digest as the passing case above, but the child is allowed to write
        # .pyc files. CPython caches the scorer module before its body runs, after which a
        # stale .pyc and a freshly compiled one are indistinguishable — so the run must
        # refuse to certify rather than stamp a flag it cannot back.
        _g, _r, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        assert _run_child_build(
            ap, pp, env_digest=cal.scorer_source_digest(),
            pycache_prefix=tmp_path / "pyc", write_bytecode=True,
        ) == {"error": "StaleBytecodeError"}

    def test_verified_run_refuses_a_populated_bytecode_cache(self, tmp_path):
        # Populate the cache (this child fails, but not before CPython writes the .pyc
        # files), then run verified against that now-populated cache: the timestamp .pyc
        # files are exactly what a same-size, mtime-preserving edit exploits.
        _g, _r, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        prefix = tmp_path / "pyc"
        digest = cal.scorer_source_digest()
        _run_child_build(ap, pp, env_digest=digest, pycache_prefix=prefix,
                         write_bytecode=True)
        assert any(prefix.rglob("*.pyc"))  # the cache is now populated
        assert _run_child_build(
            ap, pp, env_digest=digest, pycache_prefix=prefix, write_bytecode=False,
        ) == {"error": "StaleBytecodeError"}

    def test_manifest_completeness_gates_the_release_path(self, tmp_path, monkeypatch):
        # An unbound scoring import makes the digest incomplete, so the fence checks
        # manifest coverage before it trusts a digest at all.
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        monkeypatch.setattr(cal, "check_scorer_source_manifest", MagicMock(
            side_effect=cal.ScorerSourceManifestError("uncovered import")))
        with pytest.raises(cal.ScorerSourceManifestError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)


class TestScorerImportOrigins:
    """The digest says what the bytes WERE; the bytecode check says source (not a stale
    .pyc) was compiled. Neither says the module OBJECTS the scorer runs came from that tree.
    An import binds whatever is already in sys.modules, so a preload from another checkout
    executes while every source read describes this one."""

    def test_the_live_run_imported_its_app_modules_from_this_tree(self):
        cal.check_scorer_import_origins()  # this suite's own app.* imports resolve here

    def test_an_app_module_loaded_from_another_checkout_fails_closed(self):
        # THE case. No environment scrubbing reaches it: a .pth or sitecustomize runs at
        # interpreter startup, before the launcher's arguments are read, and the resulting
        # module object is what the scorer's own import silently reuses.
        impostor = MagicMock()
        impostor.__file__ = "/somewhere/else/backend/app/fen.py"
        with pytest.raises(cal.ScorerImportOriginError, match="not from the tree"):
            cal.check_scorer_import_origins(modules={"app.fen": impostor})

    def test_a_preload_from_the_hashed_tree_is_allowed(self):
        # The rule is ORIGIN, not order: these are the same bytes the digest binds, and the
        # launcher's hash predates the whole process including this compile. Being stricter
        # would buy nothing and would fail every run that imports app.* before the scorer.
        same_tree = MagicMock()
        same_tree.__file__ = str(_BACKEND_ROOT / "app" / "fen.py")
        cal.check_scorer_import_origins(modules={"app.fen": same_tree})

    def test_a_module_without_a_file_fails_closed(self):
        # A namespace package or an exec'd-in module has no __file__ to check, so it cannot
        # be proven to come from the hashed tree and must not be assumed to.
        impostor = MagicMock(spec=[])  # no __file__ attribute at all
        with pytest.raises(cal.ScorerImportOriginError):
            cal.check_scorer_import_origins(modules={"app.fen": impostor})

    def test_unimported_manifest_modules_are_not_required(self):
        # Not every manifest module is imported on every path; absence is not substitution.
        cal.check_scorer_import_origins(modules={})

    def test_is_a_source_instability_error(self):
        # Same family as StaleBytecodeError: the running code is not the named code — unlike
        # UnverifiedScorerSourceError, which means nothing was claimed in the first place.
        assert issubclass(cal.ScorerImportOriginError, cal.ScorerSourceUnstableError)

    def test_the_verified_path_checks_import_origins(self, tmp_path, monkeypatch):
        # The check must be WIRED to the verified path, not merely defined. Dev/test runs
        # legitimately run with app.* imported, so it fires only when a launcher digest is
        # present — which is exactly when the flag would otherwise be minted.
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        monkeypatch.setattr(cal, "_LAUNCHER_SCORER_DIGEST", cal.scorer_source_digest())
        monkeypatch.setattr(cal, "check_scorer_bytecode", MagicMock())
        monkeypatch.setattr(cal, "check_scorer_import_origins", MagicMock(
            side_effect=cal.ScorerImportOriginError("preloaded")))
        with pytest.raises(cal.ScorerImportOriginError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)


class TestReleaseSourceGate:
    """The gate that spends the flag (g-p4ih-srcfence). build_selection_inputs deliberately
    does NOT refuse an unverified run — dev runs have no launcher and must still work — so
    the refusal has to happen where the digest is actually relied on: --select-release and
    the Phase-3 preflight, before an approved winner is applied."""

    def test_refuses_an_unverified_cohort_and_winner(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert si.cohort.scorer_source_verified_preexec is False  # no launcher: the test path
        for bound in (si.cohort, cal.build_winner_binding(si.cohort, cal.CURRENT_SM_V2_3_CELL)):
            with pytest.raises(cal.UnverifiedScorerSourceError, match="not proven to name"):
                cal.require_preexec_verified_source(bound)

    def test_admits_a_verified_cohort(self, tmp_path):
        # The launcher path is exercised for real in test_release_calibration_launcher.py;
        # here the flag is set directly, so the gate is tested rather than the launcher.
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        verified = dataclasses.replace(si.cohort, scorer_source_verified_preexec=True)
        cal.require_preexec_verified_source(verified)  # does not raise
        cal.require_preexec_verified_source(
            cal.build_winner_binding(verified, cal.CURRENT_SM_V2_3_CELL)
        )

    def test_an_unstamped_object_fails_closed(self):
        # Reading the flag by attribute, not .get()/getattr-with-default: a shape that was
        # never stamped must raise, not read as falsy-and-therefore-refused for the wrong
        # reason — or, worse, be duck-typed into passing.
        with pytest.raises(AttributeError):
            cal.require_preexec_verified_source(object())

    def test_a_truthy_non_true_flag_is_refused(self):
        # `is not True`, not `if not flag`: the gate certifies a specific proof, so anything
        # that merely looks true (a "false" string, a 1 from a JSON round-trip) is refused.
        class _Fake:
            scorer_source_verified_preexec = "yes"

        with pytest.raises(cal.UnverifiedScorerSourceError):
            cal.require_preexec_verified_source(_Fake())

    def test_is_not_a_source_instability_error(self):
        # An unverified run is not a BROKEN run — nothing is known to be wrong, the proof
        # was simply never established. A caller catching ScorerSourceUnstableError (a tree
        # that moved) must not silently swallow "this run never claimed anything".
        assert not issubclass(cal.UnverifiedScorerSourceError, cal.ScorerSourceUnstableError)


class TestBytecodeFreshness:
    """A source digest binds .py bytes; the interpreter runs .pyc. Under CPython's default
    timestamp invalidation a cached .pyc is accepted whenever the source's (mtime, size)
    match its header — so a same-size edit with a preserved mtime executes the OLD bytecode
    while every source digest hashes the NEW file. scorer_source_verified_preexec would
    then certify code that never ran."""

    def _stale_tree(self, tmp_path, monkeypatch):
        """A package whose .pyc is compiled from v1, then a same-size v2 written over the
        source with the mtime restored — CPython still considers the v1 .pyc valid.

        The cache is compiled EXPLICITLY (py_compile, TIMESTAMP invalidation) rather than as
        a side effect of importing in a child, because a child inherits the ambient bytecode
        policy: run the suite under PYTHONDONTWRITEBYTECODE=1 and no .pyc is written, so the
        "stale" tree is not stale and the test silently stops testing anything. For the same
        reason the parent's pycache_prefix is pinned to None here, so the path py_compile
        writes to is the one _bytecode_cache_state (and the child below) will look in."""
        monkeypatch.setattr(sys, "pycache_prefix", None)
        pkg = tmp_path / "app"
        pkg.mkdir()
        mod = pkg / "opening_rootcalc.py"
        mod.write_text(_STALE_V1)
        pyc = Path(importlib.util.cache_from_source(str(mod)))
        py_compile.compile(
            str(mod), cfile=str(pyc), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
        assert pyc.exists()  # the fixture is only meaningful if the cache really exists
        _write_preserving_mtime(mod, _STALE_V2)
        return tmp_path, mod

    def test_cpython_executes_stale_bytecode_on_a_same_size_mtime_preserving_edit(
        self, tmp_path, monkeypatch
    ):
        """The hazard itself, on THIS interpreter. Not hypothetical: the source says BBB
        and the code that runs says AAA."""
        root, mod = self._stale_tree(tmp_path, monkeypatch)
        assert mod.read_text() == _STALE_V2  # the bytes any source digest would hash
        out = subprocess.run(
            [sys.executable, "-c", "import app.opening_rootcalc as m; print(m.VALUE)"],
            cwd=str(root), check=True, capture_output=True, text=True,
            env=_bytecode_fixture_env(),  # must not inherit the suite's cache policy
        )
        assert out.stdout.strip() == "AAA"  # ... but THIS is what executed

    def test_snapshot_flags_that_exact_tree_as_timestamp_cached(self, tmp_path, monkeypatch):
        root, _mod = self._stale_tree(tmp_path, monkeypatch)
        monkeypatch.setattr(cal, "_REPO_ROOT", root)
        monkeypatch.setattr(cal, "SCORER_SOURCE_FILES", ("app/opening_rootcalc.py",))
        assert cal._bytecode_cache_state() == {"app/opening_rootcalc.py": "timestamp"}

    def test_snapshot_reports_absent_when_there_is_no_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "pycache_prefix", None)
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "opening_rootcalc.py").write_text(_STALE_V1)
        monkeypatch.setattr(cal, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(cal, "SCORER_SOURCE_FILES", ("app/opening_rootcalc.py",))
        assert cal._bytecode_cache_state() == {"app/opening_rootcalc.py": "absent"}

    def test_snapshot_accepts_a_checked_hash_cache(self, tmp_path, monkeypatch):
        # PEP 552 checked-hash: CPython validates the .pyc against the source CONTENT, so
        # the mtime/size forgery has nothing to bite on and the cache is trustworthy.
        monkeypatch.setattr(sys, "pycache_prefix", None)
        (tmp_path / "app").mkdir()
        mod = tmp_path / "app" / "opening_rootcalc.py"
        mod.write_text(_STALE_V1)
        py_compile.compile(
            str(mod), doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        monkeypatch.setattr(cal, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(cal, "SCORER_SOURCE_FILES", ("app/opening_rootcalc.py",))
        assert cal._bytecode_cache_state() == {"app/opening_rootcalc.py": "checked-hash"}
        # ... and once the source changes, that same .pyc is no longer usable at all.
        _write_preserving_mtime(mod, _STALE_V2)
        assert cal._bytecode_cache_state() == {"app/opening_rootcalc.py": "unusable"}

    def test_snapshot_skips_non_python_manifest_entries(self, monkeypatch):
        monkeypatch.setattr(cal, "SCORER_SOURCE_FILES", ("backend/requirements.txt",))
        assert cal._bytecode_cache_state() == {}  # never compiled, so never cached

    @pytest.mark.parametrize("verdict", ["timestamp", "unchecked-hash"])
    def test_check_refuses_unverified_bytecode(self, verdict, monkeypatch):
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
        with pytest.raises(cal.StaleBytecodeError, match="unverified bytecode"):
            cal.check_scorer_bytecode({"backend/app/opening_rootcalc.py": verdict})

    @pytest.mark.parametrize("verdict", ["absent", "unusable", "checked-hash"])
    def test_check_accepts_bytecode_cpython_had_to_verify(self, verdict, monkeypatch):
        # absent: nothing to load. unusable: CPython rejects it and recompiles. checked-hash:
        # CPython validates the .pyc against the source CONTENT — exactly our guarantee.
        monkeypatch.setattr(sys, "dont_write_bytecode", True)
        cal.check_scorer_bytecode({"backend/app/opening_rootcalc.py": verdict})  # no raise

    def test_check_refuses_when_bytecode_writing_is_enabled(self, monkeypatch):
        monkeypatch.setattr(sys, "dont_write_bytecode", False)
        with pytest.raises(cal.StaleBytecodeError, match="disable bytecode writing"):
            cal.check_scorer_bytecode({})  # even with a clean cache

    def test_stale_bytecode_error_is_a_source_unstable_error(self):
        # So every fail-closed handler that already catches the source fence catches this.
        assert issubclass(cal.StaleBytecodeError, cal.ScorerSourceUnstableError)


class TestPublicEntryPointTrustBoundary:
    """build_selection_inputs is single-argument on purpose: the provenance record and the
    graph/roots registries are trust boundaries, and a caller who can move them can select
    against an artifact nobody approved."""

    def test_signature_takes_only_the_artifact_path(self):
        import inspect
        params = inspect.signature(cal.build_selection_inputs).parameters
        assert list(params) == ["artifact_path"]

    @pytest.mark.parametrize("kwargs", [
        {"provenance_path": "x.json"},
        {"graph": None},
        {"roots": None},
    ])
    def test_rejects_injected_inputs(self, tmp_path, kwargs):
        _g, _r, ap, _pp, _as_of, _prov = _bsi_artifact(tmp_path)
        with pytest.raises(TypeError):
            cal.build_selection_inputs(ap, **kwargs)

    def test_uses_the_committed_record_and_the_live_registries(self, tmp_path, monkeypatch):
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path)
        monkeypatch.setattr(cal, "COHORT_PROVENANCE_PATH", pp)
        monkeypatch.setattr(cal, "get_opening_graph", lambda: graph)
        monkeypatch.setattr(cal, "get_opening_roots", lambda: roots)
        si = cal.build_selection_inputs(ap)  # nothing but the artifact path
        assert si.cohort.provenance_record_sha256 == hashlib.sha256(prov).hexdigest()
        assert si.cohort.provenance.graph_fingerprint == graph.fingerprint

    def test_ignores_a_matching_record_shipped_beside_the_artifact(self, tmp_path, monkeypatch):
        # The attack the single-argument contract closes: an UNAPPROVED artifact handed
        # over with a freshly generated record that matches it. The split guard accepts any
        # self-consistent (artifact, record) pair, so the record must come from the
        # committed path — never from the caller, and never from beside the artifact.
        approved, forged = tmp_path / "approved", tmp_path / "forged"
        approved.mkdir()
        forged.mkdir()
        graph, roots, _ap, committed_pp, _a1, _p1 = _bsi_artifact(approved)
        # Same overlays, later clock -> different bytes -> a different artifact sha256.
        _g, _r, forged_ap, forged_pp, _a2, _p2 = _bsi_artifact(
            forged, as_of=datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc),
            graph=graph, roots=roots,
        )
        monkeypatch.setattr(cal, "COHORT_PROVENANCE_PATH", committed_pp)
        monkeypatch.setattr(cal, "get_opening_graph", lambda: graph)
        monkeypatch.setattr(cal, "get_opening_roots", lambda: roots)
        with pytest.raises(cal.ArtifactIntegrityError):
            cal.build_selection_inputs(forged_ap)
        # The forged record matches the forged artifact — it is simply never consulted.
        assert json.loads(forged_pp.read_bytes())["sha256"] == \
            hashlib.sha256(forged_ap.read_bytes()).hexdigest()


class TestBuildSelectionInputs:
    """Exercised through the private injected helper — the public entry point pins the
    record and the registries (see TestPublicEntryPointTrustBoundary)."""

    def test_stamps_header_clock_everywhere(self, tmp_path):
        # After the _fz overlay timestamps (~2026-06-29) so as_of-relative offsets stay >= 0.
        as_of = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path, as_of=as_of)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        c = si.cohort
        assert c.as_of == as_of
        assert si.diagnostics.as_of == as_of
        assert c.provenance.artifact_as_of == as_of
        assert c.model_version == si.diagnostics.model_version == cal.SCORE_MODEL_VERSION
        assert (c.scorer_contract_id == si.diagnostics.scorer_contract_id
                == cal.REPORT_SCORER_CONTRACT_ID)

    def test_scores_required_cells_and_diagnostics_over_required_plus_demo(self, tmp_path):
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        required = set(cal.build_arm_grid().cells)
        assert si.cohort.required_cells == frozenset(required)
        assert set(si.cohort.config_fingerprints) == required
        for p in si.cohort.pairs:
            assert set(p.grid) == required  # NO demo cells in cohort grids
        assert set(si.diagnostics.cells) == required | set(cal.DEMO_CELLS)
        assert set(si.diagnostics.config_fingerprints) == required | set(cal.DEMO_CELLS)
        # config fingerprints agree cell-for-cell on the shared required cells.
        for cell in required:
            assert (si.cohort.config_fingerprints[cell]
                    == si.diagnostics.config_fingerprints[cell] == cal._cfg_fp(cell))

    def test_stamps_runtime_and_provenance_record_binding(self, tmp_path):
        import platform
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        c = si.cohort
        assert c.runtime_python == platform.python_version()
        assert c.runtime_chess_version == chess.__version__
        assert c.provenance_record_sha256 == hashlib.sha256(prov).hexdigest()
        assert c.scorer_source_digest == cal.scorer_source_digest()
        assert isinstance(c.source_dirty_paths, tuple)

    def test_provenance_echoes_header_verbatim(self, tmp_path):
        graph, roots, ap, pp, as_of, prov = _bsi_artifact(tmp_path)
        loaded = cal.load_frozen_artifact(ap.read_bytes(), prov, cal._current_runtime_binding(graph, roots))
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        pv = si.cohort.provenance
        h = loaded.header
        assert pv.artifact_sha256 == loaded.artifact_sha256
        assert pv.graph_fingerprint == h.graph_fingerprint
        assert pv.roots_fingerprint == h.roots_fingerprint
        assert pv.captured_model_version == h.captured_model_version
        assert pv.schema_version == h.schema_version
        assert pv.pair_count == h.pair_count
        assert pv.min_observations == h.min_observations
        assert pv.cohort_rules == h.cohort_rules
        assert pv.evidence_derivation_fingerprint == h.evidence_derivation_fingerprint
        assert pv.release_guard_opening_key == h.release_guard_opening_key
        assert pv.release_guard_child_opening_key == h.release_guard_child_opening_key
        # manifest = every loaded pair, no dups.
        assert si.cohort.manifest_pair_ids == frozenset(p.pair_id for p in loaded.pairs)
        assert len(si.cohort.pairs) == len(loaded.pairs)

    def test_fails_closed_when_artifact_missing(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        ap.unlink()
        with pytest.raises(FileNotFoundError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

    def test_fails_closed_when_provenance_missing(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        pp.unlink()
        with pytest.raises(FileNotFoundError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

    def test_fails_closed_on_graph_fingerprint_drift(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        drifted = _graph([["e2e4"], ["d2d4"]])  # different fingerprint than the header
        with pytest.raises(cal.ArtifactScoringValidityError):
            cal._build_selection_inputs(ap, provenance_path=pp, graph=drifted, roots=roots)

    def test_succeeds_with_dirty_worktree_no_cleanliness_gate(self, tmp_path):
        # An uncommitted provenance record + an unrelated dirty file are the EXPECTED
        # state; build must succeed and record source_* as AUDIT fields only.
        (tmp_path / "unrelated_dirty.txt").write_text("scratch")
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert isinstance(si, cal.SelectionInputs)
        assert isinstance(si.cohort.source_dirty_paths, tuple)
        assert si.cohort.source_revision is None or isinstance(si.cohort.source_revision, str)


class TestPooledQuantilePrecondition:
    """A valid artifact with the required quantile pair count can still pool < 2 named
    scores for a cell: the load-time observation threshold counts EVIDENCE, not scored
    roots, so a pair whose evidence sits off the roots' subtrees is admitted and scores
    nothing. The builder must reject that with a per-cell diagnostic instead of handing
    Phase 2 a cohort that detonates inside derive_cutoffs as a bare ValueError."""

    def _build(self, tmp_path, quantile_colors):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(
            tmp_path, inputs=_bsi_inputs(quantile_colors=quantile_colors)
        )
        return cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

    def test_fails_closed_on_zero_pooled_quantile_scores(self, tmp_path):
        with pytest.raises(cal.ReleaseGuardShapeError) as e:
            self._build(tmp_path, ("white", "white"))  # neither pair scores a named root
        assert "too-few-pooled-quantile-scores" in str(e.value) and "number 0" in str(e.value)

    def test_fails_closed_on_one_pooled_quantile_score(self, tmp_path):
        # The boundary case, and the shape _fz_inputs actually has: two quantile pairs,
        # one of which pools nothing. derive_cutoffs needs >= 2.
        with pytest.raises(cal.ReleaseGuardShapeError) as e:
            self._build(tmp_path, ("black", "white"))
        assert "number 1" in str(e.value)

    def test_names_the_offending_cell(self, tmp_path):
        with pytest.raises(cal.ReleaseGuardShapeError) as e:
            self._build(tmp_path, ("white", "white"))
        assert any(c.label in str(e.value) for c in cal.build_arm_grid().cells)

    def test_two_pooled_scores_is_enough_and_cutoffs_derive(self, tmp_path):
        si = self._build(tmp_path, ("black", "black"))
        for cell in si.cohort.required_cells:
            pooled = sorted(s for p in si.cohort.pairs if p.cohort_role == "quantile"
                            for s in p.grid[cell].named_scores)
            assert len(pooled) >= 2  # the precondition the builder now guarantees


class TestSurrogateUserIdBinding:
    """The scored surrogate id must survive the CellScore snapshot: PairScore.user_id is
    dropped there, so without hoisting it g-p4ih-selection's uniqueness binding check has
    nothing to read."""

    def test_cohort_carries_the_scored_surrogate_ids_uniquely(self, tmp_path):
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        loaded = cal.load_frozen_artifact(
            ap.read_bytes(), prov, cal._current_runtime_binding(graph, roots)
        )
        by_id = {p.pair_id: p.surrogate_user_id for p in si.cohort.pairs}
        assert by_id == {lp.pair_id: lp.surrogate_user_id for lp in loaded.pairs}
        # The uniqueness check g-p4ih-selection must be able to run:
        surrogates = [p.surrogate_user_id for p in si.cohort.pairs]
        assert len(set(surrogates)) == len(si.cohort.pairs)

    def test_rejects_a_pair_scored_under_a_different_surrogate(self):
        # The failure this guards: the overlay was scored as user 9 but the wrapper claims
        # user 1, so every score in the grid belongs to a different subject than the label.
        cell = cal.CURRENT_SM_V2_3_CELL
        ps = cal.PairScore(9, "white", pair_id="pair-00", subject_id="s",
                           cohort_role="quantile")
        with pytest.raises(ValueError, match="surrogate_user_id mismatch"):
            cal.ScoredPair.from_pair_scores("pair-00", "s", 1, "quantile", "white", {cell: ps})


class TestGridCellKeying:
    """GridCell.label is a HUMAN label, not an identity. It folds 2 of the 6 behavioral
    axes, so keying per-cell results by it silently merges distinct cells — which is how
    the two-clock determinism proof came to cover 3 of 11 required cells. Anything keyed
    per cell must use the GridCell itself or _cfg_fp(cell)."""

    def test_label_collides_across_behaviorally_distinct_cells(self):
        cells = cal.build_arm_grid().cells
        assert len({c.label for c in cells}) < len(cells)  # lossy: 3 labels, 11 cells

    def test_cell_and_config_fingerprint_are_lossless(self):
        cells = cal.build_arm_grid().cells
        assert len(set(cells)) == len(cells)                      # GridCell: hashable identity
        assert len({cal._cfg_fp(c) for c in cells}) == len(cells)  # _cfg_fp: 1:1 with the cell

    def test_the_release_wrappers_key_by_cell_not_label(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        n = len(cal.build_arm_grid().cells)
        assert len(si.cohort.config_fingerprints) == n
        assert all(len(p.grid) == n for p in si.cohort.pairs)
        assert len(si.diagnostics.cells) == n + len(cal.DEMO_CELLS)


class TestClockDeterminism:
    def test_release_path_never_reads_wall_clock(self, tmp_path, monkeypatch):
        import app.opening_rootcalc as orc
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        monkeypatch.setattr(orc, "datetime", _clock_class(raise_on_now=True))
        # Succeeds despite .now() raising -> the release scoring path threads as_of and
        # never falls through to the wall clock.
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert isinstance(si, cal.SelectionInputs)

    def test_two_fake_clocks_same_as_of_identical(self, tmp_path, monkeypatch):
        import app.opening_rootcalc as orc
        # Two quantile pairs BOTH black so >= 2 named scores pool per cell for cutoffs.
        inputs = [
            cal.CapturedPairInput(_fz_overlay(14, "white", 22), "release_guard", 5, "fp-14w"),
            cal.CapturedPairInput(_fz_overlay(14, "black", 21), "release_guard", 5, "fp-14b"),
            cal.CapturedPairInput(_fz_overlay(2, "black", 25), "quantile", 3, "fp-2b"),
            cal.CapturedPairInput(_fz_overlay(3, "black", 22), "quantile", 3, "fp-3b"),
        ]
        graph, roots, ap, pp, as_of, _prov = _bsi_artifact(tmp_path, inputs=inputs)

        def _run():
            return cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

        monkeypatch.setattr(orc, "datetime", _clock_class(datetime(1999, 1, 1, tzinfo=timezone.utc)))
        a = _run()
        monkeypatch.setattr(orc, "datetime", _clock_class(datetime(2099, 12, 31, tzinfo=timezone.utc)))
        b = _run()

        # EVERY per-cell result below is keyed by _cfg_fp(cell), NOT cell.label. The label
        # folds only 2 of the 6 behavioral axes, so it collapses the 11 required cells into
        # 3 keys — label-keyed dicts silently overwrote 8 of them, and the "all cells agree
        # under two clocks" claim actually covered a quarter of the grid. Each assertion
        # below therefore also checks its dict is the FULL size, which is what makes a
        # regression to lossy keying fail loudly instead of passing quietly.
        n_required = len(a.cohort.required_cells)
        assert n_required == len(cal.build_arm_grid().cells)

        def _pair_maps(si):
            # Scores AND confidences: confidence is the clock-sensitive surface (see
            # test_confidence_moves_with_the_clock_while_the_score_does_not), so comparing
            # opening_scores alone would let a clock leak through untested.
            return [{cal._cfg_fp(cell): (dict(p.grid[cell].named_score_map),
                                         dict(p.grid[cell].named_confidence_map),
                                         p.grid[cell].synthetic_score,
                                         p.grid[cell].synthetic_confidence)
                     for cell in si.cohort.required_cells}
                    for p in si.cohort.pairs]

        maps_a = _pair_maps(a)
        assert maps_a == _pair_maps(b)  # identical opening_scores AND confidences
        assert maps_a and all(len(m) == n_required for m in maps_a)  # all 11 cells compared
        # ... and the confidences compared are non-trivial, so the equality is not vacuous.
        conf = [c for cell_map in maps_a for (_s, cs, _ss, _sc) in cell_map.values()
                for c in cs.values()]
        assert conf and all(0.0 < c < 100.0 for c in conf)

        def _cutoffs(si):
            # Capture BOTH the derived Cutoffs and a CutoffCollision deterministically —
            # the derivation is a pure function of the pooled scores, so a fixed as_of
            # must reproduce the same outcome (Cutoffs OR collision) regardless of the
            # wall clock.
            out = {}
            for cell in si.cohort.required_cells:
                pooled = sorted(
                    s for p in si.cohort.pairs if p.cohort_role == "quantile"
                    for s in p.grid[cell].named_scores
                )
                assert len(pooled) >= 2  # the builder's precondition, per cell
                try:
                    out[cal._cfg_fp(cell)] = cal.derive_cutoffs(pooled)
                except cal.CutoffCollision as e:
                    out[cal._cfg_fp(cell)] = ("collision", str(e))
            return out

        cut_a, cut_b = _cutoffs(a), _cutoffs(b)
        assert cut_a == cut_b and len(cut_a) == n_required  # every cell, identical outcome

        # Diagnostics identical too — over required ∪ DEMO, again keyed losslessly.
        diag_a = {cal._cfg_fp(c): r for c, r in a.diagnostics.cells.items()}
        diag_b = {cal._cfg_fp(c): r for c, r in b.diagnostics.cells.items()}
        assert diag_a == diag_b and len(diag_a) == len(a.diagnostics.cells)
        assert len(diag_a) == n_required + len(cal.DEMO_CELLS)
        del cut_a, cut_b

    def test_confidence_moves_with_the_clock_while_the_score_does_not(self):
        """The positive control the determinism assertions rest on. Confidence folds
        freshness = exp(-days_since_last_touch / half_life), so timestamped evidence scored
        under a LATER as_of must lose confidence — while opening_score here does not budge.
        That is precisely the leak an opening_score-only comparison cannot see: this test
        is what makes 'confidences match under two wall clocks' mean something."""
        graph, roots = _bsi_graph(), _bsi_roots()
        overlay = _fz_overlay(2, "black", 25)  # last_live_at = _FZ_AS_OF - 2 days
        key = cal.RELEASE_GUARD_OPENING_KEY
        fresh = cal.score_overlay(2, "black", graph, overlay, roots, as_of=_FZ_AS_OF)
        stale = cal.score_overlay(
            2, "black", graph, overlay, roots,
            as_of=_FZ_AS_OF.replace(year=_FZ_AS_OF.year + 1),
        )
        assert fresh.named_score_map[key] == stale.named_score_map[key]  # score: unmoved
        assert stale.named_confidence_map[key] < fresh.named_confidence_map[key] / 10
        assert 0.0 < stale.named_confidence_map[key]
        assert stale.synthetic_confidence < fresh.synthetic_confidence

    def test_timestamp_free_synthetic_scores_identically_under_two_clocks(self):
        scenario = cal._user14_scenario()
        cell = cal.CURRENT_SM_V2_3_CELL
        a = cal._user14_cell_operands(scenario, cell, as_of=cal.SYNTHETIC_AS_OF)
        b = cal._user14_cell_operands(
            scenario, cell, as_of=datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
        )
        assert a == b  # timestamp-free scenario is clock-invariant

    def test_release_diagnostics_use_header_clock_not_synthetic(self, tmp_path):
        as_of = datetime(2027, 1, 1, tzinfo=timezone.utc)  # != SYNTHETIC_AS_OF
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path, as_of=as_of)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        assert si.diagnostics.as_of == as_of != cal.SYNTHETIC_AS_OF


class TestScoredPairConsistency:
    def test_rejects_pseudonym_mismatch(self):
        cell = cal.CURRENT_SM_V2_3_CELL
        ps = cal.PairScore(1, "white", pair_id="pair-01", subject_id="s", cohort_role="quantile")
        with pytest.raises(ValueError):
            cal.ScoredPair.from_pair_scores("pair-00", "s", 1, "quantile", "white", {cell: ps})

    def test_rejects_color_mismatch(self):
        cell = cal.CURRENT_SM_V2_3_CELL
        ps = cal.PairScore(1, "black", pair_id="pair-00", subject_id="s", cohort_role="quantile")
        with pytest.raises(ValueError):
            cal.ScoredPair.from_pair_scores("pair-00", "s", 1, "quantile", "white", {cell: ps})

    def test_rejects_a_mutable_pair_score_in_the_grid(self):
        # The whole point of CellScore: a PairScore smuggled into the grid would be a live
        # mutable handle on a release-gate score.
        cell = cal.CURRENT_SM_V2_3_CELL
        ps = cal.PairScore(1, "white", pair_id="pair-00", subject_id="s", cohort_role="quantile")
        with pytest.raises(TypeError):
            cal.ScoredPair("pair-00", "s", 1, "quantile", "white", {cell: ps})


class TestSelectionInputsDeeplyImmutable:
    """`frozen=True` only blocks rebinding the attribute. Without a deep snapshot, a score
    reachable from the cohort can be rewritten AFTER every binding is stamped — moving
    cutoffs, gates or the winner while the artifact hash, source digest and runtime stamps
    all still verify. These are the mutation attempts that must not work."""

    @pytest.fixture
    def si(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        return cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)

    def test_cohort_scores_cannot_be_rewritten(self, si):
        cell = next(iter(si.cohort.required_cells))
        cs = si.cohort.pairs[0].grid[cell]
        key = next(iter(cs.named_score_map))
        with pytest.raises(TypeError):
            cs.named_score_map[key] = 99.0        # mappingproxy
        with pytest.raises(TypeError):
            cs.named_scores[0] = 99.0             # tuple
        with pytest.raises(TypeError):
            cs.named_confidence_map[key] = 0.0    # mappingproxy
        with pytest.raises(dataclasses.FrozenInstanceError):
            cs.synthetic_score = 99.0
        with pytest.raises(dataclasses.FrozenInstanceError):
            cs.observation_total = 0

    def test_cohort_grids_and_pair_set_cannot_be_rewritten(self, si):
        cell = next(iter(si.cohort.required_cells))
        with pytest.raises(TypeError):
            si.cohort.pairs[0].grid[cell] = None      # mappingproxy
        with pytest.raises(TypeError):
            si.cohort.config_fingerprints[cell] = "x"  # mappingproxy
        with pytest.raises(TypeError):
            si.cohort.pairs[0] = None                 # tuple

    def test_diagnostic_operands_cannot_be_rewritten(self, si):
        cell = next(iter(si.diagnostics.cells))
        with pytest.raises(TypeError):
            si.diagnostics.cells[cell] = None  # mappingproxy
        with pytest.raises(dataclasses.FrozenInstanceError):
            si.diagnostics.cells[cell].user_tp_score = 0.0

    def test_constructor_copies_so_the_callers_dict_is_not_a_live_handle(self):
        # Passing a dict in and mutating it afterwards must not reach the stamped value.
        cell = cal.CURRENT_SM_V2_3_CELL
        scores = {"a": 50.0}
        cs = cal.CellScore(
            named_scores=(50.0,), named_score_map=scores, named_confidence_map={},
            synthetic_score=None, synthetic_confidence=None, observation_total=1,
        )
        scores["a"] = 99.0  # mutating the ORIGINAL dict
        assert cs.named_score_map["a"] == 50.0

        grid = {cell: cs}
        sp = cal.ScoredPair("p", "s", 1, "quantile", "white", grid)
        grid[cell] = None
        assert sp.grid[cell] is cs


class TestWinnerBinding:
    _EXPECTED_FIELDS = {
        "config_fingerprint", "scorer_contract_id", "scorer_source_digest",
        "scorer_source_verified_preexec",
        "provenance_record_sha256", "runtime_python", "runtime_chess_version",
        "source_revision", "source_dirty_paths", "model_version", "artifact_sha256",
        "graph_fingerprint", "roots_fingerprint", "evidence_derivation_fingerprint",
        "release_guard_opening_key", "release_guard_child_opening_key",
    }

    def test_field_set_is_complete(self):
        import dataclasses
        assert {f.name for f in dataclasses.fields(cal.WinnerBinding)} == self._EXPECTED_FIELDS

    def test_assembled_from_cohort(self, tmp_path):
        graph, roots, ap, pp, _as_of, prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        c = si.cohort
        cell = cal.CURRENT_SM_V2_3_CELL
        wb = cal.build_winner_binding(c, cell)
        assert wb.config_fingerprint == c.config_fingerprints[cell]
        assert wb.scorer_contract_id == c.scorer_contract_id
        assert wb.scorer_source_digest == c.scorer_source_digest
        assert wb.scorer_source_verified_preexec == c.scorer_source_verified_preexec
        assert wb.provenance_record_sha256 == c.provenance_record_sha256
        assert wb.runtime_python == c.runtime_python
        assert wb.runtime_chess_version == c.runtime_chess_version
        assert wb.source_revision == c.source_revision
        assert wb.source_dirty_paths == c.source_dirty_paths
        assert wb.model_version == c.model_version
        assert wb.artifact_sha256 == c.provenance.artifact_sha256
        assert wb.graph_fingerprint == c.provenance.graph_fingerprint
        assert wb.roots_fingerprint == c.provenance.roots_fingerprint
        assert wb.evidence_derivation_fingerprint == c.provenance.evidence_derivation_fingerprint
        assert wb.release_guard_opening_key == c.provenance.release_guard_opening_key
        assert wb.release_guard_child_opening_key == c.provenance.release_guard_child_opening_key

    def test_unknown_winner_cell_fails_closed(self, tmp_path):
        graph, roots, ap, pp, _as_of, _prov = _bsi_artifact(tmp_path)
        si = cal._build_selection_inputs(ap, provenance_path=pp, graph=graph, roots=roots)
        with pytest.raises(KeyError):
            cal.build_winner_binding(si.cohort, cal.GridCell(0.5, "off"))  # not a required cell
