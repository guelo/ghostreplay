"""Opening-evidence two-tier replay-cache benchmark (g-overlay-cold-bootstrap).

Proves, in one run and in this order, that a persisted-bootstrap rebuild is
semantically identical to a clean build and structurally performs no raw-session
fetch, board reconstruction, or phase division. It then distinguishes four
phases: empty L1/L2, process restart with persisted L2, memory warm, and one
session invalidated in both tiers.

**Manual and non-CI.** CI runs only ``backend/test_bench_overlay_evidence.py``,
which exercises every branch here on a small fixture with injected timings. The
real timing gate is hardware-sensitive and must not be able to redden CI, so it is
run by hand:

    cd backend
    source .venv/bin/activate
    python -m scripts.bench_overlay_evidence --games 400 --plies 30 --warmup 1 --reps 5

The final stdout line is ``BENCH_RESULT {json}`` — the structured record. Append it
to the bead with ``bd update g-overlay-cold-bootstrap --append-notes``.

Everything runs against this script's OWN in-memory SQLite engine and a seeded
synthetic fixture; there is deliberately no database URL option, so it can never be
pointed at a real database.

WHAT THE GATES ACTUALLY PROVE, stated honestly. The structural counters are the
load-bearing gate and they are hardware-independent: a persisted restart must
issue ZERO raw fetches, ZERO board reconstructions, and ZERO divider calls; an
incremental rebuild after exactly one session changes must issue exactly one of
each. The cold/persisted RATIO is gated because it is
same-run and therefore hardware-cancelling; the ABSOLUTE medians are recorded as
evidence only and never gated, because they are hardware-sensitive and no
calibration owner exists. Absolute acceptance figures for this bead
(<=0.5 s target / <2.0 s requirement) were taken against a restored production
dump, NOT here — the synthetic SQLite fixture is a structural regression, not a
production-latency oracle.

WHAT IT DOES NOT PROVE. Overlay CORRECTNESS is owned by
``backend/test_opening_evidence.py``; pass 1 here is a round-trip check that reuse
does not drift from a clean build on a large fixture — exact equality, floats
included, no tolerance — not an independent check of the evidence semantics.

Keep the synthetic fixture within the application's 120,000 cached-move L1
budget when interpreting ``warm_memory``. Extreme combinations such as
``--games 5000 --plies 200`` can exceed that budget and intentionally produce L1
churn, so the pure-memory warm gates will fail even though persisted hydration is
working correctly.

It also says nothing about the probe's WIRE payload. The server-side md5 fold
that keeps that payload O(sessions) exists only on PostgreSQL; on SQLite the fold
is the identity, and an in-memory engine has no wire at all. That claim is held
by ``test_opening_evidence_digest_pg.py::test_probe_payload_is_fixed_size_per_session``.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import chess
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.opening_evidence as opening_evidence
from app.models import Base
from app.opening_evidence import overlay_evidence
from app.opening_graph import _fen_from_board, build_opening_graph

# Cold/warm speedup required in the SAME run. Deliberately conservative next to
# what production data shows (a restored prod dump measured ~38x on a 1118-session
# user) because a synthetic SQLite fixture has a much cheaper cold path: SQLite is
# in-process, so the cold build's row fetch costs a fraction of a real network
# round trip, which compresses the ratio.
MIN_COLD_PERSISTED_RATIO = 4.0

SEED = 20260729
USER_ID = 1
COLOR = "white"


# ---------------------------------------------------------------------------
# Structural counters
# ---------------------------------------------------------------------------
class Counters:
    """Replay CPU plus mutually-exclusive SQL shapes in ``_build_move_rows``."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.replays = 0
        self.reconstruction_ms = 0.0
        self.divider_calls = 0
        self.divider_ms = 0.0
        self.probe_queries = 0
        self.row_fetches = 0
        self.l2_reads = 0
        self.l2_writes = 0

    def snapshot(self) -> dict[str, int | float]:
        return {
            "replays": self.replays,
            "reconstruction_ms": self.reconstruction_ms,
            "divider_calls": self.divider_calls,
            "divider_ms": self.divider_ms,
            "probe_queries": self.probe_queries,
            "row_fetches": self.row_fetches,
            "l2_reads": self.l2_reads,
            "l2_writes": self.l2_writes,
        }


COUNTERS = Counters()


def classify_sql(statement: str) -> str | None:
    """Classify the four measured SQL shapes; ignore setup/cleanup statements."""
    operation = statement.lstrip().split(None, 1)[0].upper()
    if "GROUP BY sm.session_id" in statement:
        return "probe"
    if "sm.session_id IN" in statement:
        return "raw_fetch"
    if operation == "SELECT" and "FROM opening_session_replay_cache" in statement:
        return "l2_read"
    if operation == "INSERT" and "INSERT INTO opening_session_replay_cache" in statement:
        return "l2_write"
    return None


def install_counters(engine, monkeypatch_target=opening_evidence) -> None:
    """Count board replay/division plus the four measured SQL shapes.

    The statements are matched on fragments unique to each — the probe is the only
    GROUP BY over session_moves and the scoped fetch is the only ``session_id IN``
    — so a future edit that renames them fails loudly here rather than silently
    zeroing a gate.
    """
    real_replay = monkeypatch_target.reconstruct_board_sequence
    real_divide = monkeypatch_target.divide

    def counting_replay(moves):
        COUNTERS.replays += 1
        started = time.perf_counter()
        try:
            return real_replay(moves)
        finally:
            COUNTERS.reconstruction_ms += (time.perf_counter() - started) * 1000.0

    def counting_divide(*args, **kwargs):
        COUNTERS.divider_calls += 1
        started = time.perf_counter()
        try:
            return real_divide(*args, **kwargs)
        finally:
            COUNTERS.divider_ms += (time.perf_counter() - started) * 1000.0

    monkeypatch_target.reconstruct_board_sequence = counting_replay
    monkeypatch_target.divide = counting_divide

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        classification = classify_sql(statement)
        if classification == "probe":
            COUNTERS.probe_queries += 1
        elif classification == "raw_fetch":
            COUNTERS.row_fetches += 1
        elif classification == "l2_read":
            COUNTERS.l2_reads += 1
        elif classification == "l2_write":
            COUNTERS.l2_writes += 1


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------
def _book_lines(rng: random.Random, count: int, depth: int) -> list[list[str]]:
    """``count`` distinct legal opening lines of ``depth`` plies, deterministic."""
    lines: set[tuple[str, ...]] = set()
    guard = 0
    while len(lines) < count and guard < count * 50:
        guard += 1
        board = chess.Board()
        line: list[str] = []
        for _ in range(depth):
            moves = sorted(m.uci() for m in board.legal_moves)
            if not moves:
                break
            uci = rng.choice(moves)
            line.append(uci)
            board.push_uci(uci)
        if len(line) == depth:
            lines.add(tuple(line))
    return [list(line) for line in sorted(lines)]


def build_graph(lines: list[list[str]], tmpdir: Path):
    """An OpeningGraph whose book is exactly ``lines`` (each prefix an entry)."""
    entries = []
    for index, line in enumerate(lines):
        board = chess.Board()
        for ply, uci in enumerate(line):
            board.push_uci(uci)
            entries.append(
                {
                    "eco": f"X{index % 100:02d}",
                    "name": f"synthetic {index}/{ply}",
                    "pgn": "",
                    "uci": " ".join(line[: ply + 1]),
                    "epd": _fen_from_board(board),
                }
            )
    eco_path = tmpdir / "eco.json"
    bypos_path = tmpdir / "bypos.json"
    eco_path.write_text(
        json.dumps(
            {
                "dataset": "bench",
                "source_commit": "bench",
                "entry_count": len(entries),
                "entries": entries,
            }
        )
    )
    bypos_path.write_text(
        json.dumps(
            {
                "dataset": "bench",
                "source_commit": "bench",
                "position_count": 0,
                "by_position": {},
            }
        )
    )
    return build_opening_graph(eco_path, bypos_path)


def seed_fixture(db, *, games: int, plies: int, lines: list[list[str]]) -> list[str]:
    """Seed ``games`` ended sessions, each a book line continued off-book.

    Every session gets primary evals on SOME plies and none on others, so both the
    ``session_eval`` quality path and the ``analysis_cache`` fallback candidate
    path are populated (the fallback finds no rows — the point is to pay the
    candidate-collection cost, which is what the real overlay does).
    """
    rng = random.Random(SEED)
    db.execute(
        text(
            "INSERT INTO users (id, username, is_anonymous) VALUES (:id, 'bench', 1)"
        ),
        {"id": USER_ID},
    )

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    session_ids: list[str] = []
    session_params: list[dict] = []
    move_params: list[dict] = []
    for game in range(games):
        sid = str(uuid.UUID(int=game))
        session_ids.append(sid)
        session_params.append(
            {
                "id": sid,
                "uid": USER_ID,
                "sa": (base + timedelta(minutes=game)).isoformat(sep=" "),
                "ea": (base + timedelta(minutes=game, seconds=30)).isoformat(sep=" "),
                "pc": COLOR,
            }
        )
        board = chess.Board()
        line = lines[game % len(lines)]
        for ply in range(plies):
            if ply < len(line):
                uci = line[ply]
            else:
                moves = sorted(m.uci() for m in board.legal_moves)
                if not moves:
                    break
                uci = rng.choice(moves)
            move = chess.Move.from_uci(uci)
            san = board.san(move)
            fen_before = board.fen()
            color = "white" if board.turn == chess.WHITE else "black"
            board.push(move)
            # Some plies carry primary evals and some do not, so both the
            # session_eval quality path and the analysis_cache CANDIDATE path are
            # populated (no cache rows exist, so no candidate upgrades — the point
            # is to pay the candidate-collection cost the real overlay pays).
            has_primary = (ply % 3) != 0
            move_params.append(
                {
                    "sid": sid,
                    "mn": ply // 2 + 1,
                    "c": color,
                    "ms": san,
                    "fb": fen_before,
                    "fa": board.fen(),
                    "ed": rng.randint(0, 200),
                    "ec": rng.randint(-100, 100) if has_primary else None,
                    "bc": rng.randint(-100, 100) if has_primary else None,
                }
            )

    db.execute(
        text(
            "INSERT INTO game_sessions (id, user_id, started_at, ended_at, status,"
            " engine_elo, player_color, is_rated, session_mode)"
            " VALUES (:id, :uid, :sa, :ea, 'ended', 1500, :pc, 1, 'normal')"
        ),
        session_params,
    )
    db.execute(
        text(
            "INSERT INTO session_moves (session_id, move_number, color, move_san,"
            " fen_before, fen_after, eval_delta, eval_cp, best_move_eval_cp, segment)"
            " VALUES (:sid, :mn, :c, :ms, :fb, :fa, :ed, :ec, :bc, 'normal')"
        ),
        move_params,
    )
    db.commit()
    return session_ids


# ---------------------------------------------------------------------------
# Pass 1: equivalence
# ---------------------------------------------------------------------------
def _node_tuple(n) -> tuple:
    return (
        n.fen, n.live_attempts, n.live_passes, n.live_fails,
        n.quality_sum, n.quality_count, tuple(sorted(n.session_ids)),
        n.last_live_at, n.review_attempts, n.review_passes, n.review_fails,
        n.last_review_at, n.is_ghost_target,
    )


def _edge_tuple(e) -> tuple:
    return (
        e.parent_fen, e.child_fen, e.uci, e.traversal_count, e.live_attempts,
        e.live_passes, e.live_fails, e.quality_sum, e.quality_count,
    )


def snapshot(overlay) -> dict:
    return {
        "nodes": {k: _node_tuple(v) for k, v in overlay.nodes.items()},
        "edges": {k: _edge_tuple(v) for k, v in overlay.edges.items()},
        "source_counts": dict(overlay.source_counts),
        "excluded_sessions": overlay.excluded_sessions,
        "phase_samples": [
            (p.opening_interval_len, p.middle_ply, p.end_ply)
            for p in overlay.phase_samples
        ],
        "shared_scope": (
            overlay.shared_scope.raw_fens,
            overlay.shared_scope.norm_fens,
            overlay.shared_scope.move_row_ids,
        ),
    }


def equivalence_violations(reused: dict, scratch: dict) -> list[str]:
    """Every overlay field must match; report WHERE, not just that it differed."""
    violations = []
    for field in ("nodes", "edges"):
        a, b = reused[field], scratch[field]
        only_reused = sorted(set(a) - set(b))
        only_scratch = sorted(set(b) - set(a))
        changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        if only_reused or only_scratch or changed:
            violations.append(
                f"{field}: {len(only_reused)} reused-only, "
                f"{len(only_scratch)} scratch-only, {len(changed)} differing "
                f"(first: {(only_reused + only_scratch + changed)[:1]})"
            )
    for field in (
        "source_counts",
        "excluded_sessions",
        "phase_samples",
        "shared_scope",
    ):
        if reused[field] != scratch[field]:
            violations.append(
                f"{field}: reused={reused[field]!r} scratch={scratch[field]!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# Pass 2: structural counters
# ---------------------------------------------------------------------------
def counter_violations(
    phase: str, counts: dict[str, int | float], *, games: int
) -> list[str]:
    """The load-bearing, hardware-independent gate."""
    violations = []
    if phase == "cold_empty":
        if counts["replays"] != games:
            violations.append(
                f"cold_empty: expected {games} replays, got {counts['replays']}"
            )
        if counts["divider_calls"] != games:
            violations.append(
                f"cold_empty: expected {games} divider calls, got "
                f"{counts['divider_calls']}"
            )
        if counts["row_fetches"] != 1:
            violations.append(
                f"cold_empty: expected 1 row fetch, got {counts['row_fetches']}"
            )
        if counts["l2_writes"] != 1:
            violations.append(
                f"cold_empty: expected 1 L2 write, got {counts['l2_writes']}"
            )
    elif phase == "restart_persisted":
        if counts["replays"] != 0:
            violations.append(
                f"restart_persisted: expected 0 replays, got {counts['replays']}"
            )
        if counts["divider_calls"] != 0:
            violations.append(
                "restart_persisted: expected 0 divider calls, got "
                f"{counts['divider_calls']}"
            )
        if counts["row_fetches"] != 0:
            violations.append(
                "restart_persisted: expected 0 row fetches, got "
                f"{counts['row_fetches']}"
            )
        expected_reads = (
            games + opening_evidence._SESSION_REPLAY_READ_CHUNK_SIZE - 1
        ) // opening_evidence._SESSION_REPLAY_READ_CHUNK_SIZE
        if counts["l2_reads"] != expected_reads:
            violations.append(
                "restart_persisted: expected exactly "
                f"{expected_reads} L2 reads, got {counts['l2_reads']}"
            )
        if counts["l2_writes"] != 0:
            violations.append(
                f"restart_persisted: expected 0 L2 writes, got {counts['l2_writes']}"
            )
    elif phase == "warm_memory":
        for field in ("replays", "divider_calls", "row_fetches", "l2_reads", "l2_writes"):
            if counts[field] != 0:
                violations.append(
                    f"warm_memory: expected 0 {field}, got {counts[field]}"
                )
    elif phase == "incremental":
        if counts["replays"] != 1:
            violations.append(
                f"incremental: expected 1 replay, got {counts['replays']}"
            )
        if counts["divider_calls"] != 1:
            violations.append(
                f"incremental: expected 1 divider call, got {counts['divider_calls']}"
            )
        if counts["row_fetches"] != 1:
            violations.append(
                f"incremental: expected 1 row fetch, got {counts['row_fetches']}"
            )
        if counts["l2_reads"] != 1:
            violations.append(
                f"incremental: expected 1 L2 read, got {counts['l2_reads']}"
            )
        if counts["l2_writes"] != 1:
            violations.append(
                f"incremental: expected 1 L2 write, got {counts['l2_writes']}"
            )
    else:  # pragma: no cover - guarded by callers
        violations.append(f"unknown phase {phase!r}")
    if counts["probe_queries"] != 1:
        violations.append(
            f"{phase}: expected exactly 1 digest probe, got "
            f"{counts['probe_queries']}"
        )
    return violations


def stats_violations(phase: str, stats, *, games: int) -> list[str]:
    """Assert the application's own per-build diagnostics, independently."""
    expected = {
        "cold_empty": (0, 0, games, games),
        "restart_persisted": (0, games, 0, 0),
        "warm_memory": (games, 0, 0, 0),
        "incremental": (games - 1, 0, 1, 1),
    }
    if phase not in expected:
        return [f"unknown phase {phase!r}"]
    l1_hits, l2_hits, raw_derivations, persisted_upserts = expected[phase]
    violations = []
    fields = {
        "build_count": 1,
        "probed_sessions": games,
        "l1_hits": l1_hits,
        "l2_hits": l2_hits,
        "raw_derivations": raw_derivations,
        "persisted_upserts": persisted_upserts,
        "l2_read_failed": False,
        "l2_write_failed": False,
    }
    for field, value in fields.items():
        if getattr(stats, field) != value:
            violations.append(
                f"{phase}: expected stats.{field}={value!r}, got "
                f"{getattr(stats, field)!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# Pass 3: timing
# ---------------------------------------------------------------------------
def ratio_violations(cold_ms: float, persisted_ms: float) -> list[str]:
    """``>=`` MIN_COLD_PERSISTED_RATIO, so exactly 4.0 passes."""
    if persisted_ms <= 0:
        return [f"persisted median {persisted_ms} is not positive; ratio undefined"]
    ratio = cold_ms / persisted_ms
    if ratio < MIN_COLD_PERSISTED_RATIO:
        return [
            f"ratio {ratio:.2f}x below the required "
            f"{MIN_COLD_PERSISTED_RATIO}x"
        ]
    return []


def default_measure(db, graph, session_ids, *, warmup: int, reps: int) -> dict:
    """Measure all four phases, including replay/divider/residual attribution."""
    phases = ("cold_empty", "restart_persisted", "warm_memory", "incremental")
    metrics = ("overlay_ms", "reconstruction_ms", "divider_ms", "residual_ms")
    samples = {
        phase: {metric: [] for metric in metrics}
        for phase in phases
    }

    def measure_phase(*, commit: bool) -> dict[str, float]:
        COUNTERS.reset()
        started = time.perf_counter()
        overlay_evidence(db, USER_ID, COLOR, graph)
        if commit:
            db.commit()
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        counts = COUNTERS.snapshot()
        reconstruction_ms = float(counts["reconstruction_ms"])
        divider_ms = float(counts["divider_ms"])
        return {
            "overlay_ms": elapsed_ms,
            "reconstruction_ms": reconstruction_ms,
            "divider_ms": divider_ms,
            "residual_ms": elapsed_ms - reconstruction_ms - divider_ms,
        }

    for rep in range(warmup + reps):
        opening_evidence.reset_session_evidence_cache()
        clear_persisted_cache(db)
        readings = {"cold_empty": measure_phase(commit=True)}

        opening_evidence.reset_session_evidence_cache()
        readings["restart_persisted"] = measure_phase(commit=False)
        readings["warm_memory"] = measure_phase(commit=False)

        invalidate_one(db, session_ids[rep % len(session_ids)])
        readings["incremental"] = measure_phase(commit=True)

        if rep >= warmup:
            for phase in phases:
                for metric in metrics:
                    samples[phase][metric].append(readings[phase][metric])
    return {
        phase: {
            metric: statistics.median(values)
            for metric, values in phase_metrics.items()
        }
        for phase, phase_metrics in samples.items()
    }


def evict_one(session_id: str) -> None:
    """Drop one session from the replay cache — the finalize-a-new-drill shape."""
    with opening_evidence._SESSION_EVIDENCE_LOCK:
        entry = opening_evidence._SESSION_EVIDENCE_CACHE.pop(session_id, None)
        if entry is not None:
            opening_evidence._session_cache_rows -= len(entry[1].moves)


def clear_persisted_cache(db) -> None:
    db.execute(text("DELETE FROM opening_session_replay_cache"))
    db.commit()


def invalidate_one(db, session_id: str) -> None:
    """Remove one session from both tiers before the incremental phase."""
    evict_one(session_id)
    db.execute(
        text("DELETE FROM opening_session_replay_cache WHERE session_id = :sid"),
        {"sid": session_id},
    )
    db.commit()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def _bounded_int(flag: str, low: int, high: int):
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{flag} must be an integer") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{flag} must be in [{low}, {high}]")
        return value

    return parse


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bench_overlay_evidence",
        description=(
            "Two-tier overlay replay benchmark: equivalence, four structural "
            "phases, then cold/persisted timing. Synthetic fixture only — no "
            "database URL option."
        ),
        epilog=(
            "Keep the fixture below the 120,000 cached-move L1 budget when "
            "interpreting warm_memory; --games 5000 --plies 200 can exceed it."
        ),
    )
    parser.add_argument(
        "--games", type=_bounded_int("--games", 20, 5000), default=400,
        help="sessions to seed (default 400)",
    )
    parser.add_argument(
        "--plies", type=_bounded_int("--plies", 10, 200), default=30,
        help="plies per session (default 30)",
    )
    parser.add_argument(
        "--openings", type=_bounded_int("--openings", 1, 500), default=40,
        help="distinct book lines to generate (default 40)",
    )
    parser.add_argument(
        "--book-depth", type=_bounded_int("--book-depth", 2, 20), default=6,
        help="plies of each book line that are IN the graph (default 6)",
    )
    parser.add_argument("--warmup", type=_bounded_int("--warmup", 0, 20), default=1)
    parser.add_argument("--reps", type=_bounded_int("--reps", 1, 50), default=5)
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(argv: list[str], measure=default_measure) -> int:
    args = parse_args(argv)

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    real_replay = opening_evidence.reconstruct_board_sequence
    real_divide = opening_evidence.divide
    try:
        rng = random.Random(SEED)
        lines = _book_lines(rng, args.openings, args.book_depth)
        if not lines:  # pragma: no cover - generator is deterministic
            print("FAIL: could not generate any book lines")
            return 1
        session_ids = seed_fixture(
            db, games=args.games, plies=args.plies, lines=lines
        )
        with tempfile.TemporaryDirectory() as tmp:
            graph = build_graph(lines, Path(tmp))

        row_count = db.execute(
            text("SELECT count(*) FROM session_moves")
        ).scalar_one()
        install_counters(engine)
        violations: list[str] = []

        # --- Pass 1: equivalence, untimed, first. A persisted-bootstrap build
        # must not drift from a genuinely empty L1/L2 build.
        opening_evidence.reset_session_evidence_cache()
        clear_persisted_cache(db)
        cold_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        db.commit()
        scratch = snapshot(cold_overlay)
        opening_evidence.reset_session_evidence_cache()
        persisted_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        reused = snapshot(persisted_overlay)
        violations += equivalence_violations(reused, scratch)
        if violations:
            _report(violations)
            return 1

        # --- Pass 2: structural counters and application diagnostics, untimed.
        counts: dict[str, dict[str, int | float]] = {}
        phase_stats = {}
        opening_evidence.reset_session_evidence_cache()
        clear_persisted_cache(db)
        COUNTERS.reset()
        cold_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        db.commit()
        counts["cold_empty"] = COUNTERS.snapshot()
        phase_stats["cold_empty"] = cold_overlay.replay_cache_stats

        opening_evidence.reset_session_evidence_cache()
        COUNTERS.reset()
        restart_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        counts["restart_persisted"] = COUNTERS.snapshot()
        phase_stats["restart_persisted"] = restart_overlay.replay_cache_stats

        COUNTERS.reset()
        warm_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        counts["warm_memory"] = COUNTERS.snapshot()
        phase_stats["warm_memory"] = warm_overlay.replay_cache_stats

        invalidate_one(db, session_ids[0])
        COUNTERS.reset()
        incremental_overlay = overlay_evidence(db, USER_ID, COLOR, graph)
        db.commit()
        counts["incremental"] = COUNTERS.snapshot()
        phase_stats["incremental"] = incremental_overlay.replay_cache_stats

        for phase in (
            "cold_empty",
            "restart_persisted",
            "warm_memory",
            "incremental",
        ):
            violations += counter_violations(
                phase, counts[phase], games=args.games
            )
            violations += stats_violations(
                phase, phase_stats[phase], games=args.games
            )
        if violations:
            _report(violations)
            return 1

        # --- Pass 3: timing.
        medians = measure(
            db, graph, session_ids, warmup=args.warmup, reps=args.reps
        )
        cold_ms = medians["cold_empty"]["overlay_ms"]
        persisted_ms = medians["restart_persisted"]["overlay_ms"]
        violations += ratio_violations(cold_ms, persisted_ms)
        if violations:
            _report(violations)

        ratio = cold_ms / persisted_ms if persisted_ms > 0 else None
        print(
            "BENCH_RESULT "
            + json.dumps(
                {
                    "games": args.games,
                    "plies": args.plies,
                    "openings": args.openings,
                    "book_depth": args.book_depth,
                    "warmup": args.warmup,
                    "reps": args.reps,
                    "session_move_rows": row_count,
                    "overlay_nodes": len(reused["nodes"]),
                    "overlay_edges": len(reused["edges"]),
                    "cold_empty_median_ms": cold_ms,
                    "restart_persisted_median_ms": persisted_ms,
                    "warm_memory_median_ms": medians["warm_memory"]["overlay_ms"],
                    "incremental_median_ms": medians["incremental"]["overlay_ms"],
                    "phase_medians": medians,
                    "cold_persisted_ratio": ratio,
                    "min_cold_persisted_ratio": MIN_COLD_PERSISTED_RATIO,
                    "counters": counts,
                    "cache_stats": {
                        phase: asdict(stats)
                        for phase, stats in phase_stats.items()
                    },
                },
                sort_keys=True,
            )
        )
        return 1 if violations else 0
    finally:
        opening_evidence.reconstruct_board_sequence = real_replay
        opening_evidence.divide = real_divide
        db.close()
        engine.dispose()


def _report(violations: list[str]) -> None:
    for violation in violations:
        print(f"FAIL: {violation}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(sys.argv[1:]))
