"""CI coverage for scripts/bench_overlay_evidence.py.

The benchmark itself is manual (its timing gate is hardware-sensitive and must
never be able to redden CI). These tests exercise every branch of it on a small
fixture, with timings injected where a real measurement would be needed, so the
gates cannot silently rot into no-ops.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

import app.opening_evidence as opening_evidence
from app.opening_evidence import EdgeEvidence, EvidenceOverlay, NodeEvidence
from scripts import bench_overlay_evidence as bench

SMALL = ["--games", "20", "--plies", "12", "--openings", "4", "--reps", "1",
         "--warmup", "0"]


def _bench_result(capsys) -> dict:
    lines = [
        line for line in capsys.readouterr().out.splitlines()
        if line.startswith("BENCH_RESULT ")
    ]
    assert len(lines) == 1, "expected exactly one BENCH_RESULT line"
    return json.loads(lines[0][len("BENCH_RESULT "):])


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #
def test_run_passes_and_emits_the_structured_record(capsys):
    assert bench.run(SMALL) == 0
    record = _bench_result(capsys)

    assert record["games"] == 20
    assert record["session_move_rows"] == 20 * 12
    assert record["overlay_nodes"] > 0
    assert record["overlay_edges"] > 0
    assert (
        record["min_cold_persisted_ratio"]
        == bench.MIN_COLD_PERSISTED_RATIO
    )
    # The claim of the bead, asserted on the record itself.
    assert record["counters"]["cold_empty"]["replays"] == 20
    assert record["counters"]["cold_empty"]["divider_calls"] == 20
    assert record["counters"]["restart_persisted"]["replays"] == 0
    assert record["counters"]["restart_persisted"]["divider_calls"] == 0
    assert record["counters"]["restart_persisted"]["row_fetches"] == 0
    assert record["cache_stats"]["restart_persisted"]["l2_hits"] == 20
    assert record["counters"]["incremental"]["replays"] == 1
    assert record["counters"]["incremental"]["divider_calls"] == 1
    assert record["counters"]["incremental"]["row_fetches"] == 1
    for phase in (
        "cold_empty",
        "restart_persisted",
        "warm_memory",
        "incremental",
    ):
        assert record["counters"][phase]["probe_queries"] == 1
        assert set(record["phase_medians"][phase]) == {
            "overlay_ms",
            "reconstruction_ms",
            "divider_ms",
            "residual_ms",
        }


def test_broken_reuse_fails_before_timing(capsys, monkeypatch):
    """If the digest never matches, the warm build re-replays everything. The
    structural gate must catch that and no BENCH_RESULT should be emitted."""
    counter = {"n": 0}
    real = bench.opening_evidence._session_digest

    def never_matching(
        row_count,
        body,
        session_ts,
        session_pgn_body,
        terminal_line_reconciled=False,
    ):
        counter["n"] += 1
        digest = real(
            row_count,
            body,
            session_ts,
            session_pgn_body,
            terminal_line_reconciled,
        )
        return (
            f"{digest}-{counter['n']}"
        )

    monkeypatch.setattr(bench.opening_evidence, "_session_digest", never_matching)
    assert bench.run(SMALL) == 1
    out = capsys.readouterr().out
    assert "BENCH_RESULT" not in out
    assert "expected 0 replays" in out
    assert "restart_persisted" in out


def test_drifted_reuse_fails_equivalence_first(capsys, monkeypatch):
    """Pass 1 runs before the counters: a reused build that DRIFTS from a clean one
    is reported as an equivalence violation, not as a counter violation."""
    real_snapshot = bench.snapshot
    calls = {"n": 0}

    def drifting(overlay):
        calls["n"] += 1
        snap = real_snapshot(overlay)
        if calls["n"] == 1:  # perturb only the reused snapshot
            snap["excluded_sessions"] = snap["excluded_sessions"] + 7
        return snap

    monkeypatch.setattr(bench, "snapshot", drifting)
    assert bench.run(SMALL) == 1
    out = capsys.readouterr().out
    assert "BENCH_RESULT" not in out
    assert "excluded_sessions" in out


def test_run_fails_on_a_slow_ratio_but_still_records_the_medians(capsys):
    def slow(db, graph, session_ids, *, warmup, reps):
        def phase(overlay_ms):
            return {
                "overlay_ms": overlay_ms,
                "reconstruction_ms": 1.0,
                "divider_ms": 0.5,
                "residual_ms": overlay_ms - 1.5,
            }

        return {
            "cold_empty": phase(10.0),
            "restart_persisted": phase(5.0),
            "warm_memory": phase(4.0),
            "incremental": phase(6.0),
        }

    assert bench.run(SMALL, measure=slow) == 1
    out = capsys.readouterr().out
    assert "below the required" in out
    # The record is still the final line, so a failing run is still evidence.
    assert out.strip().splitlines()[-1].startswith("BENCH_RESULT ")
    record = json.loads(out.strip().splitlines()[-1][len("BENCH_RESULT "):])
    assert record["cold_persisted_ratio"] == pytest.approx(2.0)
    assert record["cold_empty_median_ms"] == 10.0
    assert record["restart_persisted_median_ms"] == 5.0
    assert record["incremental_median_ms"] == 6.0


def test_run_restores_the_patched_replay_entry_point():
    before = opening_evidence.reconstruct_board_sequence
    before_divide = opening_evidence.divide
    bench.run(SMALL)
    assert opening_evidence.reconstruct_board_sequence is before
    assert opening_evidence.divide is before_divide


# --------------------------------------------------------------------------- #
# Gate units
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cold_ms,warm_ms,expect_violation",
    [
        (4.0, 1.0, False),  # exactly MIN_COLD_PERSISTED_RATIO passes
        (3.99, 1.0, True),
        (40.0, 1.0, False),
        (10.0, 0.0, True),  # undefined ratio
    ],
)
def test_ratio_boundary(cold_ms, warm_ms, expect_violation):
    assert bool(bench.ratio_violations(cold_ms, warm_ms)) is expect_violation


def _counts(**overrides):
    values = {
        "replays": 0,
        "reconstruction_ms": 0.0,
        "divider_calls": 0,
        "divider_ms": 0.0,
        "probe_queries": 1,
        "row_fetches": 0,
        "l2_reads": 0,
        "l2_writes": 0,
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "phase,counts,needle",
    [
        ("cold_empty", _counts(replays=3, divider_calls=5, row_fetches=1, l2_writes=1), "expected 5 replays"),
        ("cold_empty", _counts(replays=5, divider_calls=5, row_fetches=0, l2_writes=1), "expected 1 row fetch"),
        ("restart_persisted", _counts(replays=1, l2_reads=1), "expected 0 replays"),
        ("restart_persisted", _counts(row_fetches=1, l2_reads=1), "expected 0 row fetches"),
        ("warm_memory", _counts(l2_reads=1), "expected 0 l2_reads"),
        ("incremental", _counts(replays=2, divider_calls=1, row_fetches=1, l2_reads=1, l2_writes=1), "expected 1 replay"),
        ("incremental", _counts(replays=1, divider_calls=1, row_fetches=2, l2_reads=1, l2_writes=1), "expected 1 row fetch"),
        ("warm_memory", _counts(probe_queries=2), "expected exactly 1 digest probe"),
        ("warm_memory", _counts(probe_queries=0), "expected exactly 1 digest probe"),
        ("bogus", _counts(), "unknown phase"),
    ],
)
def test_counter_violations(phase, counts, needle):
    violations = bench.counter_violations(phase, counts, games=5)
    assert any(needle in v for v in violations), violations


def test_counter_violations_clean_readings_pass():
    assert bench.counter_violations(
        "cold_empty",
        _counts(replays=5, divider_calls=5, row_fetches=1, l2_reads=1, l2_writes=1),
        games=5,
    ) == []
    assert bench.counter_violations(
        "restart_persisted", _counts(l2_reads=1), games=5
    ) == []
    assert bench.counter_violations(
        "warm_memory", _counts(), games=5
    ) == []
    assert bench.counter_violations(
        "incremental",
        _counts(replays=1, divider_calls=1, row_fetches=1, l2_reads=1, l2_writes=1),
        games=5,
    ) == []


def test_restart_gate_requires_exact_chunked_l2_read_count():
    games = opening_evidence._SESSION_REPLAY_READ_CHUNK_SIZE + 1
    expected_reads = 2

    assert bench.counter_violations(
        "restart_persisted",
        _counts(l2_reads=expected_reads),
        games=games,
    ) == []
    violations = bench.counter_violations(
        "restart_persisted",
        _counts(l2_reads=expected_reads + 1),
        games=games,
    )
    assert any("expected exactly 2 L2 reads" in item for item in violations)


def test_phase_stats_contract_is_checked_separately():
    clean = opening_evidence.ReplayCacheStats(
        build_count=1,
        probed_sessions=5,
        l2_hits=5,
    )
    assert bench.stats_violations("restart_persisted", clean, games=5) == []
    broken = dataclasses.replace(clean, raw_derivations=1)
    assert any(
        "raw_derivations" in item
        for item in bench.stats_violations("restart_persisted", broken, games=5)
    )


def test_equivalence_violations_names_the_field_and_key():
    base = {
        "nodes": {"a": (1,), "b": (2,)},
        "edges": {},
        "source_counts": {"x": 1},
        "excluded_sessions": 0,
        "phase_samples": [],
        "shared_scope": ((), (), ()),
    }
    other = {
        "nodes": {"a": (9,), "c": (3,)},
        "edges": {},
        "source_counts": {"x": 2},
        "excluded_sessions": 1,
        "phase_samples": [(1, None, None)],
        "shared_scope": (("raw",), ("norm",), (1,)),
    }
    violations = bench.equivalence_violations(base, other)
    joined = " ".join(violations)
    assert "nodes" in joined
    assert "source_counts" in joined
    assert "excluded_sessions" in joined
    assert "phase_samples" in joined
    assert "shared_scope" in joined


def test_equivalence_violations_accepts_identical_snapshots():
    snap = {
        "nodes": {"a": (1,)},
        "edges": {("a", "b"): (1,)},
        "source_counts": {"x": 1},
        "excluded_sessions": 2,
        "phase_samples": [(4, 5, None)],
        "shared_scope": (("raw",), ("norm",), (1,)),
    }
    assert bench.equivalence_violations(snap, dict(snap)) == []


# --------------------------------------------------------------------------- #
# The equivalence check must not go blind when a field is added
# --------------------------------------------------------------------------- #
def test_snapshot_covers_every_overlay_field():
    compared = {
        "nodes",
        "edges",
        "source_counts",
        "excluded_sessions",
        "phase_samples",
        "shared_scope",
    }
    identity = {"user_id", "player_color"}
    diagnostics = {"replay_cache_stats"}
    actual = {f.name for f in dataclasses.fields(EvidenceOverlay)}
    assert actual == compared | identity | diagnostics, (
        "EvidenceOverlay gained/lost a field — update bench.snapshot() and "
        "bench.equivalence_violations(), or explicitly classify it as a "
        "non-semantic diagnostic"
    )


@pytest.mark.parametrize(
    "statement,expected",
    [
        ("SELECT x FROM session_moves sm GROUP BY sm.session_id", "probe"),
        ("SELECT x FROM session_moves sm WHERE sm.session_id IN (?, ?)", "raw_fetch"),
        ("SELECT x FROM opening_session_replay_cache WHERE session_id IN (?)", "l2_read"),
        ("INSERT INTO opening_session_replay_cache (session_id) VALUES (?)", "l2_write"),
        ("DELETE FROM opening_session_replay_cache", None),
        ("SELECT count(*) FROM session_moves", None),
    ],
)
def test_sql_classifiers_are_mutually_exclusive(statement, expected):
    assert bench.classify_sql(statement) == expected


@pytest.mark.parametrize(
    "cls,projector,sample",
    [
        (NodeEvidence, bench._node_tuple, NodeEvidence(fen="f")),
        (
            EdgeEvidence,
            bench._edge_tuple,
            EdgeEvidence(parent_fen="p", child_fen="c", uci="e2e4"),
        ),
    ],
)
def test_projectors_cover_every_evidence_field(cls, projector, sample):
    assert len(projector(sample)) == len(dataclasses.fields(cls)), (
        f"{cls.__name__} gained a field the benchmark's projection ignores"
    )


# --------------------------------------------------------------------------- #
# Fixture and helpers
# --------------------------------------------------------------------------- #
def test_book_lines_are_distinct_deterministic_and_full_depth():
    import random

    first = bench._book_lines(random.Random(bench.SEED), 6, 5)
    second = bench._book_lines(random.Random(bench.SEED), 6, 5)
    assert first == second
    assert len(first) == 6
    assert len({tuple(line) for line in first}) == 6
    assert all(len(line) == 5 for line in first)


def test_evict_one_drops_the_session_and_its_row_count(capsys):
    """The finalize shape the incremental phase measures."""
    assert bench.run(SMALL) == 0
    capsys.readouterr()
    with opening_evidence._SESSION_EVIDENCE_LOCK:
        opening_evidence._SESSION_EVIDENCE_CACHE.clear()
        opening_evidence._session_cache_rows = 0
    # Evicting an absent session is a no-op rather than an error.
    bench.evict_one("does-not-exist")
    assert opening_evidence._session_cache_rows == 0


# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["--games", "19"],           # below the floor
        ["--games", "5001"],         # above the ceiling
        ["--plies", "9"],
        ["--plies", "201"],
        ["--openings", "0"],
        ["--book-depth", "1"],
        ["--reps", "0"],
        ["--warmup", "-1"],
        ["--games", "not-a-number"],
        ["--database-url", "postgresql://nope"],  # deliberately not a flag
    ],
)
def test_invalid_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        bench.parse_args(argv)
    assert exc.value.code == 2


def test_defaults():
    args = bench.parse_args([])
    assert (args.games, args.plies, args.openings) == (400, 30, 40)
    assert (args.book_depth, args.warmup, args.reps) == (6, 1, 5)


def test_the_gate_constant_is_not_a_flag():
    """The ratio floor must not be CLI-tunable around a failing run."""
    with pytest.raises(SystemExit):
        bench.parse_args(["--min-cold-persisted-ratio", "1.0"])


def test_help_warns_that_extreme_fixtures_can_exceed_the_l1_budget(capsys):
    with pytest.raises(SystemExit) as exc:
        bench.parse_args(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "120,000 cached-move L1 budget" in help_text
    assert "--games 5000 --plies 200" in help_text
