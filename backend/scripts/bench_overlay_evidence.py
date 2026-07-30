"""Opening-evidence overlay reuse benchmark (g-overlay-evidence-reuse).

Proves, in one run and in this order, that rebuilding the whole-(user,color)
evidence overlay on a warm replay cache is (1) semantically identical to a clean
from-scratch build, (2) structurally free of BOTH the per-session board replay and
the raw-row fetch, and only then (3) at least ``MIN_COLD_WARM_RATIO``x faster than
the cold build.

**Manual and non-CI.** CI runs only ``backend/test_bench_overlay_evidence.py``,
which exercises every branch here on a small fixture with injected timings. The
real timing gate is hardware-sensitive and must not be able to redden CI, so it is
run by hand:

    cd backend
    source .venv/bin/activate
    python -m scripts.bench_overlay_evidence --games 400 --plies 30 --warmup 1 --reps 5

The final stdout line is ``BENCH_RESULT {json}`` — the structured record. Append it
to the bead with ``bd update g-overlay-evidence-reuse --append-notes``.

Everything runs against this script's OWN in-memory SQLite engine and a seeded
synthetic fixture; there is deliberately no database URL option, so it can never be
pointed at a real database.

WHAT THE GATES ACTUALLY PROVE, stated honestly. The structural counters are the
load-bearing gate and they are hardware-independent: a warm rebuild must issue
ZERO board replays and ZERO scoped row fetches, and an incremental rebuild after
exactly one session changes must issue exactly one of each. Those two facts are
the whole claim of this bead. The cold/warm RATIO is gated because it is
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
MIN_COLD_WARM_RATIO = 4.0

SEED = 20260729
USER_ID = 1
COLOR = "white"


# ---------------------------------------------------------------------------
# Structural counters
# ---------------------------------------------------------------------------
class Counters:
    """Board replays plus the two statements ``_build_move_rows`` can issue."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.replays = 0
        self.probe_queries = 0
        self.row_fetches = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "replays": self.replays,
            "probe_queries": self.probe_queries,
            "row_fetches": self.row_fetches,
        }


COUNTERS = Counters()


def install_counters(engine, monkeypatch_target=opening_evidence) -> None:
    """Count board replays via the replay entry point and the two SQL shapes.

    The statements are matched on fragments unique to each — the probe is the only
    GROUP BY over session_moves and the scoped fetch is the only ``session_id IN``
    — so a future edit that renames them fails loudly here rather than silently
    zeroing a gate.
    """
    real_replay = monkeypatch_target.reconstruct_board_sequence

    def counting_replay(moves):
        COUNTERS.replays += 1
        return real_replay(moves)

    monkeypatch_target.reconstruct_board_sequence = counting_replay

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        if "GROUP BY sm.session_id" in statement:
            COUNTERS.probe_queries += 1
        elif "sm.session_id IN" in statement:
            COUNTERS.row_fetches += 1


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
    for field in ("source_counts", "excluded_sessions", "phase_samples"):
        if reused[field] != scratch[field]:
            violations.append(
                f"{field}: reused={reused[field]!r} scratch={scratch[field]!r}"
            )
    return violations


# ---------------------------------------------------------------------------
# Pass 2: structural counters
# ---------------------------------------------------------------------------
def counter_violations(phase: str, counts: dict[str, int], *, games: int) -> list[str]:
    """The load-bearing, hardware-independent gate."""
    violations = []
    if phase == "cold":
        if counts["replays"] != games:
            violations.append(
                f"cold: expected {games} replays, got {counts['replays']}"
            )
        if counts["row_fetches"] != 1:
            violations.append(
                f"cold: expected 1 row fetch, got {counts['row_fetches']}"
            )
    elif phase == "warm":
        if counts["replays"] != 0:
            violations.append(
                f"warm: expected 0 replays, got {counts['replays']} — the replay "
                "cache is not being reused"
            )
        if counts["row_fetches"] != 0:
            violations.append(
                f"warm: expected 0 row fetches, got {counts['row_fetches']} — the "
                "digest probe is not preventing the whole-history fetch"
            )
    elif phase == "incremental":
        if counts["replays"] != 1:
            violations.append(
                f"incremental: expected 1 replay, got {counts['replays']}"
            )
        if counts["row_fetches"] != 1:
            violations.append(
                f"incremental: expected 1 row fetch, got {counts['row_fetches']}"
            )
    else:  # pragma: no cover - guarded by callers
        violations.append(f"unknown phase {phase!r}")
    if counts["probe_queries"] != 1:
        violations.append(
            f"{phase}: expected exactly 1 digest probe, got "
            f"{counts['probe_queries']}"
        )
    return violations


# ---------------------------------------------------------------------------
# Pass 3: timing
# ---------------------------------------------------------------------------
def ratio_violations(cold_ms: float, warm_ms: float) -> list[str]:
    """``>=`` MIN_COLD_WARM_RATIO, so exactly 4.0 passes."""
    if warm_ms <= 0:
        return [f"warm median {warm_ms} is not positive; ratio undefined"]
    ratio = cold_ms / warm_ms
    if ratio < MIN_COLD_WARM_RATIO:
        return [f"ratio {ratio:.2f}x below the required {MIN_COLD_WARM_RATIO}x"]
    return []


def default_measure(db, graph, session_ids, *, warmup: int, reps: int) -> dict:
    """Discard ``warmup`` warmups, time ``reps`` repetitions, return median ms.

    ``incremental`` evicts exactly one session's cache entry before each timed
    build — the end-of-drill finalize shape this bead exists to make fast.
    """
    samples: dict[str, list[float]] = {"cold": [], "warm": [], "incremental": []}
    for rep in range(warmup + reps):
        opening_evidence.reset_session_evidence_cache()
        started = time.perf_counter()
        overlay_evidence(db, USER_ID, COLOR, graph)
        cold = (time.perf_counter() - started) * 1000.0

        started = time.perf_counter()
        overlay_evidence(db, USER_ID, COLOR, graph)
        warm = (time.perf_counter() - started) * 1000.0

        evict_one(session_ids[rep % len(session_ids)])
        started = time.perf_counter()
        overlay_evidence(db, USER_ID, COLOR, graph)
        incremental = (time.perf_counter() - started) * 1000.0

        if rep >= warmup:
            samples["cold"].append(cold)
            samples["warm"].append(warm)
            samples["incremental"].append(incremental)
    return {k: statistics.median(v) for k, v in samples.items()}


def evict_one(session_id: str) -> None:
    """Drop one session from the replay cache — the finalize-a-new-drill shape."""
    with opening_evidence._SESSION_EVIDENCE_LOCK:
        entry = opening_evidence._SESSION_EVIDENCE_CACHE.pop(session_id, None)
        if entry is not None:
            opening_evidence._session_cache_rows -= len(entry[1].moves)


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
            "Overlay-reuse benchmark: equivalence, then structural counters, then "
            "the cold/warm ratio. Synthetic fixture only — no database URL option."
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

        # --- Pass 1: equivalence, untimed, first. A reused build must not drift
        # from a clean one; if it does, nothing downstream is worth measuring.
        opening_evidence.reset_session_evidence_cache()
        overlay_evidence(db, USER_ID, COLOR, graph)  # cold, populates the cache
        reused = snapshot(overlay_evidence(db, USER_ID, COLOR, graph))
        opening_evidence.reset_session_evidence_cache()
        scratch = snapshot(overlay_evidence(db, USER_ID, COLOR, graph))
        violations += equivalence_violations(reused, scratch)
        if violations:
            _report(violations)
            return 1

        # --- Pass 2: structural counters, untimed, second. Per phase: reset
        # immediately before ONE build and read immediately after it.
        counts: dict[str, dict[str, int]] = {}
        opening_evidence.reset_session_evidence_cache()
        COUNTERS.reset()
        overlay_evidence(db, USER_ID, COLOR, graph)
        counts["cold"] = COUNTERS.snapshot()

        COUNTERS.reset()
        overlay_evidence(db, USER_ID, COLOR, graph)
        counts["warm"] = COUNTERS.snapshot()

        evict_one(session_ids[0])
        COUNTERS.reset()
        overlay_evidence(db, USER_ID, COLOR, graph)
        counts["incremental"] = COUNTERS.snapshot()

        for phase in ("cold", "warm", "incremental"):
            violations += counter_violations(
                phase, counts[phase], games=args.games
            )
        if violations:
            _report(violations)
            return 1

        # --- Pass 3: timing.
        medians = measure(
            db, graph, session_ids, warmup=args.warmup, reps=args.reps
        )
        violations += ratio_violations(medians["cold"], medians["warm"])
        if violations:
            _report(violations)

        ratio = medians["cold"] / medians["warm"] if medians["warm"] > 0 else None
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
                    "cold_median_ms": medians["cold"],
                    "warm_median_ms": medians["warm"],
                    "incremental_median_ms": medians["incremental"],
                    "ratio": ratio,
                    "min_cold_warm_ratio": MIN_COLD_WARM_RATIO,
                    "counters": counts,
                },
                sort_keys=True,
            )
        )
        return 1 if violations else 0
    finally:
        opening_evidence.reconstruct_board_sequence = real_replay
        db.close()
        engine.dispose()


def _report(violations: list[str]) -> None:
    for violation in violations:
        print(f"FAIL: {violation}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run(sys.argv[1:]))
