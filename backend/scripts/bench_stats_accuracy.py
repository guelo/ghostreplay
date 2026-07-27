"""Release B read-switch benchmark: cached aggregate accuracy vs. the live shape.

Proves, in one run and in this order, that switching ``/api/stats/summary`` and
``/api/history`` onto ``game_sessions.player_accuracy`` is (1) semantically a
no-op, (2) structurally free of the ordered evaluation query and the per-session
PGN parse, and only then (3) at least ``MIN_RATIO``x faster.

**Manual and non-CI.** CI runs only ``backend/test_bench_stats_accuracy.py``,
which exercises every branch here on a 20-game fixture with injected timings. The
real timing gate is hardware-sensitive and must not be able to redden CI, so it is
run by hand:

    cd backend
    source .venv/bin/activate
    python -m scripts.bench_stats_accuracy --games 500 --plies 40 --warmup 2 --reps 5

The final stdout line is ``BENCH_RESULT {json}`` — the structured record. Append it
to the bead with ``bd update g-b-cache-reads --append-notes``.

Everything runs against this script's OWN in-memory SQLite engine and a seeded
synthetic fixture; there is deliberately no database URL option, so it can never be
pointed at a real database.

What the equivalence pass proves, stated honestly: both sides trace to the same
frozen v1 computation — one live through :func:`game_accuracy_for_rows`, one
through a column :func:`recompute_session_accuracy` stamped using that same guarded
function. It is a round-trip proof that the write hook, the column, the seam, and
the aggregate arithmetic agree end to end. It is NOT an independent check of the
algorithm; the frozen goldens own that.

Absolute medians are recorded as evidence only, never gated: they are
hardware-sensitive and no calibration owner exists. The only performance gate is
the same-run ratio.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import chess
import chess.pgn
from sqlalchemy import case, create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.accuracy import (
    accuracy_for_sessions,
    expected_total_moves_from_pgn,
    game_accuracy_for_rows,
    recompute_session_accuracy,
)
from app.models import Base, GameSession, SessionMove
from app.session_contracts import visible_session_filter

# Acceptance constants. Deliberately NOT flags: the gate must not be weakenable
# from the command line.
UNRESOLVED_GAMES = 10
MIN_RATIO = 20.0

SEED = 0

# Fixed epoch so ``started_at`` ordering — and therefore the fixture's color
# split — is reproducible run to run.
_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)

_BENCH_USER_ID = 1


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
class Counters:
    """Per-invocation structural counters.

    Reset immediately before ONE untimed invocation of a path and read immediately
    after it. Never read inside a timed region; the timed loop asserts nothing
    about them.
    """

    def __init__(self) -> None:
        self.total_statements = 0
        self.eval_queries = 0
        self.pgn_parses = 0

    def reset(self) -> None:
        self.total_statements = 0
        self.eval_queries = 0
        self.pgn_parses = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "total_statements": self.total_statements,
            "eval_queries": self.eval_queries,
            "pgn_parses": self.pgn_parses,
        }


COUNTERS = Counters()


def _is_ordered_eval_query(statement: str) -> bool:
    sql = " ".join(statement.lower().split())
    return "from session_moves" in sql and "eval_cp" in sql and "order by" in sql


def _on_cursor(conn, cursor, statement, parameters, context, executemany) -> None:
    COUNTERS.total_statements += 1
    if _is_ordered_eval_query(statement):
        COUNTERS.eval_queries += 1


def _counted_expected_total_moves(pgn: str | None) -> int | None:
    """The frozen PGN parse, with the per-invocation parse counter around it."""
    COUNTERS.pgn_parses += 1
    return expected_total_moves_from_pgn(pgn)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _bounded_int(name: str, lo: int, hi: int, *, even: bool = False, lo_label: str | None = None):
    lo_text = lo_label or str(lo)

    def _parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be an integer, got {raw!r}") from None
        if value < lo or value > hi:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {lo_text} and {hi}, got {value}"
            )
        if even and value % 2 != 0:
            raise argparse.ArgumentTypeError(f"{name} must be even, got {value}")
        return value

    return _parse


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse and validate. Any out-of-range or non-integer value exits 2."""
    parser = argparse.ArgumentParser(
        prog="bench_stats_accuracy",
        description="Cached vs. live aggregate accuracy: equivalence, counters, ratio.",
    )
    parser.add_argument(
        "--games",
        type=_bounded_int(
            "--games", 2 * UNRESOLVED_GAMES, 5000, lo_label="2 * UNRESOLVED_GAMES"
        ),
        default=500,
    )
    parser.add_argument("--plies", type=_bounded_int("--plies", 20, 200, even=True), default=40)
    parser.add_argument("--warmup", type=_bounded_int("--warmup", 0, 20), default=2)
    parser.add_argument("--reps", type=_bounded_int("--reps", 1, 50), default=5)
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
def _is_terminal(board: chess.Board) -> bool:
    return (
        board.is_checkmate()
        or board.is_stalemate()
        or board.is_insufficient_material()
        or board.is_fifty_moves()
        or board.is_repetition(3)
    )


def _play_exactly(plies: int, rng: random.Random, game_index: int):
    """Play exactly ``plies`` plies, never landing on a terminal position.

    Seeded rejection sampling: at each ply shuffle the legal moves and push the
    first candidate whose resulting position is not terminal. Because no ply may
    land on a terminal position, the game cannot end early and reaches exactly
    ``plies`` with the side to move still having legal moves.
    """
    board = chess.Board()
    fens: list[str] = []
    for ply in range(plies):
        candidates = list(board.legal_moves)
        rng.shuffle(candidates)
        for move in candidates:
            board.push(move)
            if _is_terminal(board):
                board.pop()
                continue
            fens.append(board.fen())
            break
        else:
            # The seed is fixed, so this is a reproducible fixture bug, never
            # flakiness.
            raise AssertionError(
                f"game {game_index}: every legal move at ply {ply} is terminal"
            )
    exporter = chess.pgn.StringExporter(headers=False, variations=False, comments=False)
    pgn = chess.pgn.Game.from_board(board).accept(exporter).strip()
    return fens, pgn


def build_fixture(db, *, games: int, plies: int, seed: int = SEED) -> list[GameSession]:
    """Create ``games`` ended, visible sessions and return them freshly preloaded.

    Games are generated in index order with ``player_color = "white" if i % 2 == 0
    else "black"``. The unresolved cohort is ``range(UNRESOLVED_GAMES)`` — the first
    ten indices, which alternate, so it splits 5 white / 5 black and every color
    group stays non-empty once the cohort drops out of the accuracy denominator.

    Each game yields a real PGN (so ``expected_total_moves_from_pgn`` equals
    ``plies`` exactly) and a contiguous mainline ``session_moves`` grid (so
    ``ply_coordinates_intact`` passes and the frozen computation returns a real
    integer). The unresolved cohort's rows carry NULL ``eval_cp`` / ``eval_mate``,
    so the frozen computation legitimately yields None for them.

    **Cache values are stamped by the production guarded write path.** After the
    rows are flushed this calls :func:`recompute_session_accuracy` once per session
    — the same bounded hook Release A's serving writers use — so the benchmark's
    cached column is produced by production code, ply-coordinate guard and
    stamp-a-computed-None rule included, not by a bench-local shortcut.

    Everything here is OUTSIDE every timing and counter window: generation, PGN
    synthesis, inserts, the stamping pass, the commit, the expunge, and the final
    preload.
    """
    for i in range(games):
        rng = random.Random(seed * 1_000_003 + i)
        fens, pgn = _play_exactly(plies, rng, i)
        unresolved = i < UNRESOLVED_GAMES
        session = GameSession(
            id=uuid.uuid4(),
            user_id=_BENCH_USER_ID,
            started_at=_EPOCH + timedelta(seconds=i),
            ended_at=_EPOCH + timedelta(seconds=i, minutes=10),
            status="ended",
            # Stamped synthetically; accuracy reads none of it.
            result="checkmate_win" if i % 3 else "draw",
            engine_elo=1500,
            player_color="white" if i % 2 == 0 else "black",
            session_mode="normal",
            is_rated=True,
            pgn=pgn,
        )
        db.add(session)
        for ply, fen in enumerate(fens):
            db.add(
                SessionMove(
                    session_id=session.id,
                    move_number=ply // 2 + 1,
                    color="white" if ply % 2 == 0 else "black",
                    move_san="e4",
                    fen_after=fen,
                    eval_cp=None if unresolved else rng.randint(-400, 400),
                    eval_mate=None,
                )
            )
    db.flush()

    for session in db.query(GameSession).all():
        recompute_session_accuracy(db, session)
    db.commit()

    # Drop every identity-map carryover from the stamping transaction, so both
    # measured paths run over the same freshly preloaded ORM sessions.
    db.expunge_all()
    return (
        db.query(GameSession)
        .filter(GameSession.status == "ended", visible_session_filter())
        .order_by(GameSession.started_at.asc())
        .all()
    )


def expected_denominators(games: int) -> dict[str, int]:
    """Accuracy denominators the fixture must produce, as a pure function of ``games``.

    Derived, not measured, so the tests can pin them without building a fixture:
    the cohort of ``UNRESOLVED_GAMES`` alternating leading indices drops out of the
    overall mean and out of each color's mean.
    """
    white_total = math.ceil(games / 2)
    black_total = games // 2
    return {
        "overall": games - UNRESOLVED_GAMES,
        "white": white_total - math.ceil(UNRESOLVED_GAMES / 2),
        "black": black_total - UNRESOLVED_GAMES // 2,
    }


# ---------------------------------------------------------------------------
# The two measured paths
# ---------------------------------------------------------------------------
def compute_path(db, sessions) -> dict[uuid.UUID, int | None]:
    """The shape stats.py ran BEFORE the read switch.

    One ordered ``session_moves`` evaluation query over all session ids, grouped in
    Python, one PGN parse per session, then :func:`game_accuracy_for_rows` — the
    guarded entry point, never the raw frozen function, because that is the path
    production actually ran and it carries the ply-coordinate guard.
    """
    session_ids = [session.id for session in sessions]
    color_order = case((SessionMove.color == "white", 0), else_=1)
    move_rows = (
        db.query(
            SessionMove.session_id,
            SessionMove.move_number,
            SessionMove.color,
            SessionMove.eval_cp,
            SessionMove.eval_mate,
        )
        .filter(SessionMove.session_id.in_(session_ids))
        .order_by(SessionMove.move_number.asc(), color_order.asc())
        .all()
    )
    rows_by_session: dict[uuid.UUID, list] = {}
    for row in move_rows:
        rows_by_session.setdefault(row.session_id, []).append(row)
    return {
        session.id: game_accuracy_for_rows(
            rows_by_session.get(session.id, []),
            player_color=session.player_color,
            expected_total_moves=_counted_expected_total_moves(session.pgn),
            session_id=session.id,
        )
        for session in sessions
    }


def cached_path(db, sessions) -> dict[uuid.UUID, int | None]:
    """The read switch: the shared seam over already-preloaded ORM sessions."""
    return accuracy_for_sessions(db, sessions)


def _round1(value: float) -> float:
    """The rounding the API returns (mirrors stats.py's ``_round1``)."""
    return round(value, 1)


def aggregate(accuracy_by_session: dict[uuid.UUID, int | None], sessions) -> dict:
    """Overall and per-color means, raw and rounded, plus the three denominators.

    Mirrors ``stats.py``'s ``_mean_accuracy``: unweighted mean of the per-game
    integers with NULLs dropped from the denominator.
    """
    groups: dict[str, list[int]] = {"overall": [], "white": [], "black": []}
    for session in sessions:
        value = accuracy_by_session.get(session.id)
        if value is None:
            continue
        groups["overall"].append(value)
        groups["black" if session.player_color == "black" else "white"].append(value)

    raw = {
        key: (sum(values) / len(values) if values else None) for key, values in groups.items()
    }
    return {
        "raw": raw,
        "rounded": {
            key: (None if value is None else _round1(value)) for key, value in raw.items()
        },
        "denominators": {key: len(values) for key, values in groups.items()},
    }


# ---------------------------------------------------------------------------
# Pass 1: equivalence
# ---------------------------------------------------------------------------
def equivalence_violations(
    compute_result: dict[uuid.UUID, int | None], cached_result: dict[uuid.UUID, int | None]
) -> list[str]:
    """Per-session exact equality — integers AND NULLs — across every session."""
    violations: list[str] = []
    missing = set(compute_result) ^ set(cached_result)
    if missing:
        violations.append(
            "session sets differ: " + ", ".join(sorted(str(sid) for sid in missing))
        )
    for sid in sorted(set(compute_result) & set(cached_result), key=str):
        expected = compute_result[sid]
        actual = cached_result[sid]
        if expected != actual:
            violations.append(
                f"session {sid}: compute={expected!r} cached={actual!r}"
            )
    return violations


def aggregate_violations(
    compute_agg: dict, cached_agg: dict, *, expected: dict[str, int]
) -> list[str]:
    """Identical means (raw AND at the API's rounding) and the pinned denominators."""
    violations: list[str] = []
    for scale in ("raw", "rounded"):
        for key in ("overall", "white", "black"):
            if compute_agg[scale][key] != cached_agg[scale][key]:
                violations.append(
                    f"{scale} {key} mean: compute={compute_agg[scale][key]!r} "
                    f"cached={cached_agg[scale][key]!r}"
                )
    for name, agg in (("compute", compute_agg), ("cached", cached_agg)):
        if agg["denominators"] != expected:
            violations.append(
                f"{name} denominators {agg['denominators']} != expected {expected}"
            )
    return violations


# ---------------------------------------------------------------------------
# Pass 2: structural counters
# ---------------------------------------------------------------------------
def counter_violations(path_name: str, counts: dict[str, int], *, games: int) -> list[str]:
    """Per-invocation structural requirements for one path."""
    violations: list[str] = []
    if path_name == "compute":
        if counts["eval_queries"] != 1:
            violations.append(
                f"compute: expected exactly 1 ordered eval query, got {counts['eval_queries']}"
            )
        # The ordered eval query must be the ONLY statement, not merely the only one
        # of its shape. The baseline's claim is "one query for all sessions"; a
        # per-session lazy load or refresh would still leave eval_queries at 1 while
        # inflating the compute median — and the compute median is the numerator of
        # the gated ratio, so unpinned extra SQL makes the gate easier to pass.
        if counts["total_statements"] != 1:
            violations.append(
                f"compute: expected exactly 1 SQL statement, got {counts['total_statements']}"
            )
        # Per INVOCATION, which is also what proves the query is not memoized
        # across calls.
        if counts["pgn_parses"] != games:
            violations.append(
                f"compute: expected {games} PGN parses, got {counts['pgn_parses']}"
            )
    elif path_name == "cached":
        # Asserted on the TOTAL statement count, not only the ordered-eval count:
        # a lazy load or an accidental refresh is any statement at all.
        if counts["total_statements"] != 0:
            violations.append(
                f"cached: expected 0 SQL statements, got {counts['total_statements']}"
            )
        if counts["pgn_parses"] != 0:
            violations.append(
                f"cached: expected 0 PGN parses, got {counts['pgn_parses']}"
            )
    else:  # pragma: no cover - guarded by callers
        violations.append(f"unknown path {path_name!r}")
    return violations


# ---------------------------------------------------------------------------
# Pass 3: timing
# ---------------------------------------------------------------------------
def ratio_violations(compute_ms: float, cached_ms: float) -> list[str]:
    """``>=`` MIN_RATIO, so exactly 20.0 passes."""
    if cached_ms <= 0:
        return [f"cached median {cached_ms} is not positive; ratio undefined"]
    ratio = compute_ms / cached_ms
    if ratio < MIN_RATIO:
        return [f"ratio {ratio:.2f}x below the required {MIN_RATIO}x"]
    return []


def default_measure(db, sessions, *, warmup: int, reps: int) -> dict[str, float]:
    """Discard ``warmup`` warmups, time ``reps`` repetitions, return median ms."""
    samples: dict[str, list[float]] = {"compute": [], "cached": []}
    for rep in range(warmup + reps):
        for name, path in (("compute", compute_path), ("cached", cached_path)):
            started = time.perf_counter()
            path(db, sessions)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            if rep >= warmup:
                samples[name].append(elapsed_ms)
    return {name: statistics.median(values) for name, values in samples.items()}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _make_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[GameSession.__table__, SessionMove.__table__])
    event.listen(engine, "before_cursor_execute", _on_cursor)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, factory()


def run(argv: list[str], *, measure=default_measure) -> int:
    args = parse_args(argv)
    engine, db = _make_session()
    try:
        sessions = build_fixture(db, games=args.games, plies=args.plies, seed=SEED)

        # --- Pass 1: baseline equivalence, untimed, FIRST. Speed against a
        # different answer is not a result.
        compute_result = compute_path(db, sessions)
        cached_result = cached_path(db, sessions)
        expected = expected_denominators(args.games)
        violations = equivalence_violations(compute_result, cached_result)
        violations += aggregate_violations(
            aggregate(compute_result, sessions),
            aggregate(cached_result, sessions),
            expected=expected,
        )
        if violations:
            _report(violations)
            return 1

        # --- Pass 2: structural counters, untimed, second. Per invocation: reset
        # immediately before ONE invocation and read immediately after it.
        counts: dict[str, dict[str, int]] = {}
        for name, path in (("compute", compute_path), ("cached", cached_path)):
            COUNTERS.reset()
            path(db, sessions)
            counts[name] = COUNTERS.snapshot()
        for name in ("compute", "cached"):
            violations += counter_violations(name, counts[name], games=args.games)
        if violations:
            _report(violations)
            return 1

        # --- Pass 3: timing.
        medians = measure(db, sessions, warmup=args.warmup, reps=args.reps)
        violations += ratio_violations(medians["compute"], medians["cached"])
        if violations:
            _report(violations)

        ratio = (
            medians["compute"] / medians["cached"] if medians["cached"] > 0 else None
        )
        # The structured record, and the FINAL stdout line either way.
        print(
            "BENCH_RESULT "
            + json.dumps(
                {
                    "games": args.games,
                    "plies": args.plies,
                    "warmup": args.warmup,
                    "reps": args.reps,
                    "compute_median_ms": medians["compute"],
                    "cached_median_ms": medians["cached"],
                    "ratio": ratio,
                    "min_ratio": MIN_RATIO,
                    "counters": counts,
                    "denominators": expected,
                },
                sort_keys=True,
            )
        )
        return 1 if violations else 0
    finally:
        db.close()
        engine.dispose()


def _report(violations: list[str]) -> None:
    for violation in violations:
        print(f"FAIL: {violation}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(sys.argv[1:]))
