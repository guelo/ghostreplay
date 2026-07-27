"""Tests for the Release B read-switch benchmark (g-b-cache-reads).

The script is structured for injection rather than as one ``main()``, so its
equivalence, counter, ratio, validation, and failure-reporting branches are
testable without a 500-game run. Every case here uses a SMALL fixture
(``--games 20 --plies 20``) or no fixture at all, and every timing is INJECTED, so
this file is fast and deterministic and can run in CI.

**The real ``--games 500`` timing gate is deliberately NOT here.** It is
hardware-sensitive and must not be able to redden CI; it stays a manual run whose
``BENCH_RESULT`` line is appended to the bead as evidence.
"""

from __future__ import annotations

import json

import pytest

import scripts.bench_stats_accuracy as bench
from app.accuracy import ACCURACY_ALGO_VERSION, expected_total_moves_from_pgn

SMALL = ["--games", "20", "--plies", "20", "--warmup", "0", "--reps", "1"]


def _passing_measure(compute_ms=100.0, cached_ms=1.0):
    def _measure(db, sessions, *, warmup, reps):
        return {"compute": compute_ms, "cached": cached_ms}

    return _measure


def _bench_result(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "no stdout at all"
    assert lines[-1].startswith("BENCH_RESULT "), lines
    return json.loads(lines[-1][len("BENCH_RESULT ") :])


# ---------------------------------------------------------------------------
# 1. Happy path.
# ---------------------------------------------------------------------------
def test_run_passes_and_emits_the_structured_record(capsys):
    assert bench.run(SMALL, measure=_passing_measure()) == 0

    record = _bench_result(capsys)
    assert record["games"] == 20
    assert record["plies"] == 20
    assert record["ratio"] == 100.0
    assert record["denominators"] == bench.expected_denominators(20)
    assert record["counters"]["compute"] == {
        "eval_queries": 1,
        "pgn_parses": 20,
        "total_statements": 1,
    }
    assert record["counters"]["cached"] == {
        "eval_queries": 0,
        "pgn_parses": 0,
        "total_statements": 0,
    }


# ---------------------------------------------------------------------------
# 2. Mismatch reporting.
# ---------------------------------------------------------------------------
def test_perturbed_cache_column_fails_and_names_the_session(monkeypatch, capsys):
    """Perturb ONE session's cached column after the guarded stamping pass.

    The equivalence pass must catch it before any timing runs, and the message must
    carry the session id and BOTH values — a bare "mismatch" would leave an operator
    with nothing to chase.
    """
    real_build = bench.build_fixture
    seen: dict[str, object] = {}

    def _perturbing(db, *, games, plies, seed):
        sessions = real_build(db, games=games, plies=plies, seed=seed)
        # A RESOLVED game (index >= UNRESOLVED_GAMES), so the mismatch is
        # integer-vs-different-integer rather than integer-vs-NULL.
        target = sessions[-1]
        assert target.player_accuracy is not None
        seen["id"] = target.id
        seen["computed"] = target.player_accuracy
        seen["poisoned"] = (target.player_accuracy + 1) % 101
        target.player_accuracy = seen["poisoned"]
        db.commit()
        return sessions

    monkeypatch.setattr(bench, "build_fixture", _perturbing)

    assert bench.run(SMALL, measure=_passing_measure()) == 1

    out = capsys.readouterr().out
    assert "BENCH_RESULT" not in out  # failed before timing ran
    offender = [line for line in out.splitlines() if str(seen["id"]) in line]
    assert offender, out
    assert f"compute={seen['computed']!r}" in offender[0]
    assert f"cached={seen['poisoned']!r}" in offender[0]


def test_equivalence_violations_reports_both_values():
    sid = "11111111-1111-1111-1111-111111111111"
    violations = bench.equivalence_violations({sid: 71}, {sid: 70})
    assert len(violations) == 1
    assert sid in violations[0]
    assert "compute=71" in violations[0]
    assert "cached=70" in violations[0]


def test_equivalence_accepts_matching_nulls():
    sid = "11111111-1111-1111-1111-111111111111"
    assert bench.equivalence_violations({sid: None}, {sid: None}) == []
    # ...but a NULL against an integer is a mismatch, not a tolerance.
    assert bench.equivalence_violations({sid: None}, {sid: 0}) != []


# ---------------------------------------------------------------------------
# 3-5. Counter violations.
# ---------------------------------------------------------------------------
def test_cached_path_rejects_any_sql_statement():
    counts = {"total_statements": 1, "eval_queries": 0, "pgn_parses": 0}
    violations = bench.counter_violations("cached", counts, games=20)
    assert violations and "0 SQL statements" in violations[0]


def test_cached_path_rejects_a_pgn_parse():
    counts = {"total_statements": 0, "eval_queries": 0, "pgn_parses": 1}
    violations = bench.counter_violations("cached", counts, games=20)
    assert violations and "PGN parses" in violations[0]


def test_cached_path_clean_reading_passes():
    counts = {"total_statements": 0, "eval_queries": 0, "pgn_parses": 0}
    assert bench.counter_violations("cached", counts, games=20) == []


@pytest.mark.parametrize(
    ("counts", "needle"),
    [
        ({"total_statements": 2, "eval_queries": 2, "pgn_parses": 20}, "eval query"),
        ({"total_statements": 1, "eval_queries": 1, "pgn_parses": 19}, "PGN parses"),
        # The shape that passes an eval-query-only check: one ordered query plus a
        # stray statement (a lazy load, a refresh). It inflates the compute median —
        # the NUMERATOR of the gated ratio — so leaving it unpinned would make the
        # 20x gate easier to pass while breaking the one-statement baseline claim.
        ({"total_statements": 2, "eval_queries": 1, "pgn_parses": 20}, "1 SQL statement"),
    ],
)
def test_compute_path_counter_violations(counts, needle):
    violations = bench.counter_violations("compute", counts, games=20)
    assert violations and needle in violations[0]


def test_compute_path_clean_reading_passes():
    counts = {"total_statements": 1, "eval_queries": 1, "pgn_parses": 20}
    assert bench.counter_violations("compute", counts, games=20) == []


# ---------------------------------------------------------------------------
# 6. Ratio boundary.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("compute_ms", "expect_violation"),
    [(19.9, True), (20.0, False), (40.0, False)],
)
def test_ratio_boundary(compute_ms, expect_violation):
    violations = bench.ratio_violations(compute_ms, 1.0)
    assert bool(violations) is expect_violation


def test_run_fails_on_a_slow_ratio_but_still_records_the_medians(capsys):
    assert bench.run(SMALL, measure=_passing_measure(compute_ms=19.9, cached_ms=1.0)) == 1

    out = capsys.readouterr().out
    assert "below the required 20.0x" in out
    # The record is still the FINAL line: an operator needs the medians of the run
    # that failed the gate most of all.
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1].startswith("BENCH_RESULT ")
    record = json.loads(lines[-1][len("BENCH_RESULT ") :])
    assert record["ratio"] == pytest.approx(19.9)


# ---------------------------------------------------------------------------
# 7. Argument validation.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["--games", "9"],            # below 2 * UNRESOLVED_GAMES
        ["--games", "5001"],
        ["--plies", "41"],           # odd
        ["--plies", "4"],            # below the floor
        ["--plies", "202"],
        ["--reps", "0"],
        ["--reps", "51"],
        ["--warmup", "-1"],
        ["--warmup", "21"],
        ["--games", "abc"],          # non-integer
        ["--plies", "20.5"],
    ],
)
def test_invalid_arguments_exit_2(argv, capsys):
    with pytest.raises(SystemExit) as excinfo:
        bench.parse_args(argv)
    assert excinfo.value.code == 2


def test_games_floor_message_names_the_constant(capsys):
    with pytest.raises(SystemExit):
        bench.parse_args(["--games", "9"])
    err = capsys.readouterr().err
    assert "--games" in err
    assert "2 * UNRESOLVED_GAMES" in err


def test_defaults():
    args = bench.parse_args([])
    assert (args.games, args.plies, args.warmup, args.reps) == (500, 40, 2, 5)


def test_the_gate_constants_are_not_flags():
    """UNRESOLVED_GAMES and MIN_RATIO must not be weakenable from the command line."""
    args = bench.parse_args([])
    assert not hasattr(args, "min_ratio")
    assert not hasattr(args, "unresolved_games")
    for bad in (["--min-ratio", "1"], ["--unresolved-games", "0"]):
        with pytest.raises(SystemExit) as excinfo:
            bench.parse_args(bad)
        assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# 8. Denominator formulas — no fixture built.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("games", "expected"),
    [
        (20, {"overall": 10, "white": 5, "black": 5}),
        (21, {"overall": 11, "white": 6, "black": 5}),
        (500, {"overall": 490, "white": 245, "black": 245}),
    ],
)
def test_expected_denominators(games, expected):
    assert bench.expected_denominators(games) == expected


# ---------------------------------------------------------------------------
# 9. The fixture itself.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def small_fixture():
    engine, db = bench._make_session()
    try:
        yield db, bench.build_fixture(db, games=20, plies=20, seed=bench.SEED)
    finally:
        db.close()
        engine.dispose()


def test_fixture_shape(small_fixture):
    db, sessions = small_fixture
    assert len(sessions) == 20

    from app.models import SessionMove

    for session in sessions:
        # A REAL PGN: the frozen parse must see exactly --plies mainline plies, or
        # compute_game_accuracy's completeness rule would null the whole fixture.
        assert expected_total_moves_from_pgn(session.pgn) == 20
        rows = db.query(SessionMove).filter(SessionMove.session_id == session.id).count()
        assert rows == 20

    colors = [session.player_color for session in sessions]
    assert colors.count("white") == 10
    assert colors.count("black") == 10


def test_fixture_cache_is_stamped_by_the_guarded_write_path(small_fixture):
    """Exactly UNRESOLVED_GAMES sessions carry a NULL accuracy, all at version 1.

    A stamped version on a NULL value is the signature of ``recompute_session_accuracy``
    having ATTEMPTED the computation — a bench-local shortcut that just wrote the
    column would leave the version NULL.
    """
    _, sessions = small_fixture
    nulls = [s for s in sessions if s.player_accuracy is None]
    assert len(nulls) == bench.UNRESOLVED_GAMES
    assert all(s.player_accuracy_algo_version == ACCURACY_ALGO_VERSION for s in sessions)
    # The resolved games score a real integer in range, so the fixture is not
    # vacuously equal on both paths.
    for session in sessions:
        if session.player_accuracy is not None:
            assert 0 <= session.player_accuracy <= 100
    # The unresolved cohort alternates, so it costs each color exactly five games.
    assert sum(1 for s in nulls if s.player_color == "white") == 5
    assert sum(1 for s in nulls if s.player_color == "black") == 5


def test_fixture_is_deterministic():
    """Same seed, same PGNs and same cached accuracies — the gate is reproducible."""
    runs = []
    for _ in range(2):
        engine, db = bench._make_session()
        try:
            sessions = bench.build_fixture(db, games=20, plies=20, seed=bench.SEED)
            runs.append([(s.pgn, s.player_accuracy) for s in sessions])
        finally:
            db.close()
            engine.dispose()
    assert runs[0] == runs[1]


def test_aggregate_matches_the_api_arithmetic(small_fixture):
    """Unweighted mean of the per-game integers, NULLs dropped, rounded to 1 decimal."""
    db, sessions = small_fixture
    agg = bench.aggregate(bench.cached_path(db, sessions), sessions)

    assert agg["denominators"] == bench.expected_denominators(20)
    scored = [s.player_accuracy for s in sessions if s.player_accuracy is not None]
    assert agg["raw"]["overall"] == sum(scored) / len(scored)
    assert agg["rounded"]["overall"] == round(agg["raw"]["overall"], 1)


def test_both_paths_agree_on_the_small_fixture(small_fixture):
    db, sessions = small_fixture
    assert (
        bench.equivalence_violations(
            bench.compute_path(db, sessions), bench.cached_path(db, sessions)
        )
        == []
    )
