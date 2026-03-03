#!/usr/bin/env python3
"""Pre-compute Stockfish analysis for every position in the opening book.

Reads public/data/openings/eco.json, walks each opening's UCI move sequence
to produce (fen_before, move_uci) pairs, deduplicates them, runs Stockfish
at depth 20 on each position, and upserts results into the analysis_cache table.

Usage:
    python scripts/precompute_openings.py
    python scripts/precompute_openings.py --database-url postgresql+psycopg://...
    python scripts/precompute_openings.py --depth 16 --workers 4
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("precompute")

import chess
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import AnalysisCache, Base

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
DEFAULT_ECO_PATH = PROJECT_ROOT / "public" / "data" / "openings" / "eco.json"
DEFAULT_DEPTH = 24
DEFAULT_WORKERS = 1
BATCH_SIZE = 100

# Set by main() so child processes (via _worker_fn) can read it
_verbose = False


@dataclass(frozen=True)
class PositionToAnalyze:
    fen_before: str
    move_uci: str
    move_san: str


@dataclass
class AnalysisResult:
    fen_before: str
    move_uci: str
    move_san: str
    best_move_uci: str | None
    best_move_san: str | None
    played_eval: int | None  # white-relative cp
    best_eval: int | None  # white-relative cp
    eval_delta: int | None


def extract_positions(eco_path: Path) -> list[PositionToAnalyze]:
    """Walk every opening line and collect unique (fen_before, move_uci) pairs."""
    with open(eco_path) as f:
        data = json.load(f)

    seen: set[tuple[str, str]] = set()
    positions: list[PositionToAnalyze] = []

    for entry in data["entries"]:
        uci_moves = entry["uci"].split()
        board = chess.Board()

        for uci_str in uci_moves:
            fen_before = board.fen()
            move = chess.Move.from_uci(uci_str)
            san = board.san(move)
            key = (fen_before, uci_str)

            if key not in seen:
                seen.add(key)
                positions.append(PositionToAnalyze(
                    fen_before=fen_before,
                    move_uci=uci_str,
                    move_san=san,
                ))

            board.push(move)

    return positions


def _parse_score(info_line: str, side_to_move_is_white: bool) -> int | None:
    """Extract centipawn eval from a Stockfish info line, normalized to white-relative."""
    tokens = info_line.split()
    try:
        idx = tokens.index("score")
    except ValueError:
        return None

    score_type = tokens[idx + 1]
    score_value = int(tokens[idx + 2])

    if score_type == "mate":
        mate_base = 10000
        mate_decay = 10
        if score_value == 0:
            cp = -mate_base
        else:
            sign = 1 if score_value > 0 else -1
            cp = sign * (mate_base - abs(score_value) * mate_decay)
    elif score_type == "cp":
        cp = score_value
    else:
        return None

    # Stockfish reports from the side-to-move perspective; normalize to white
    return cp if side_to_move_is_white else -cp


def _run_search(proc: subprocess.Popen, fen: str, moves: list[str], depth: int) -> tuple[str, int | None]:
    """Run a single Stockfish search and return (bestmove, eval_cp_white_relative)."""
    moves_segment = f" moves {' '.join(moves)}" if moves else ""
    proc.stdin.write(f"position fen {fen}{moves_segment}\n")
    proc.stdin.write(f"go depth {depth}\n")
    proc.stdin.flush()

    board = chess.Board(fen)
    for m in moves:
        board.push(chess.Move.from_uci(m))
    side_is_white = board.turn == chess.WHITE

    last_score: int | None = None
    bestmove = ""

    for line in proc.stdout:
        line = line.strip()
        if line.startswith("info") and "score" in line and " pv " in line:
            parsed = _parse_score(line, side_is_white)
            if parsed is not None:
                last_score = parsed
        elif line.startswith("bestmove"):
            bestmove = line.split()[1] if len(line.split()) > 1 else ""
            break

    return bestmove, last_score


def _uci_to_san(fen: str, uci_move: str) -> str | None:
    """Convert a UCI move to SAN given a FEN."""
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci_move)
        return board.san(move)
    except Exception:
        return None


def analyze_position(pos: PositionToAnalyze, depth: int, stockfish_path: str) -> AnalysisResult:
    """Analyze a single position using Stockfish CLI."""
    proc = subprocess.Popen(
        [stockfish_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    try:
        # Wait for uciok
        proc.stdin.write("uci\n")
        proc.stdin.write("setoption name Hash value 128\n")
        proc.stdin.write("setoption name Threads value 1\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "uciok":
                break
        proc.stdin.write("isready\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "readyok":
                break

        # Search 1: eval after played move
        _, played_eval = _run_search(proc, pos.fen_before, [pos.move_uci], depth)

        # Search 2: find best move in position
        best_move_uci, _ = _run_search(proc, pos.fen_before, [], depth)

        # Search 3: eval after best move (if different from played)
        if best_move_uci and best_move_uci != pos.move_uci and best_move_uci != "(none)":
            _, best_eval = _run_search(proc, pos.fen_before, [best_move_uci], depth)
        else:
            best_move_uci = pos.move_uci
            best_eval = played_eval

        eval_delta: int | None = None
        if best_eval is not None and played_eval is not None:
            # Compute from perspective of the side that moved
            board = chess.Board(pos.fen_before)
            if board.turn == chess.WHITE:
                eval_delta = best_eval - played_eval
            else:
                eval_delta = played_eval - best_eval
            eval_delta = max(eval_delta, 0)

        best_move_san = _uci_to_san(pos.fen_before, best_move_uci) if best_move_uci else None

        return AnalysisResult(
            fen_before=pos.fen_before,
            move_uci=pos.move_uci,
            move_san=pos.move_san,
            best_move_uci=best_move_uci if best_move_uci != "(none)" else None,
            best_move_san=best_move_san,
            played_eval=played_eval,
            best_eval=best_eval,
            eval_delta=eval_delta,
        )
    finally:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.terminate()
        proc.wait(timeout=5)


def _analyze_batch(positions: list[PositionToAnalyze], depth: int, stockfish_path: str) -> list[AnalysisResult]:
    """Analyze a batch of positions, reusing a single Stockfish process."""
    proc = subprocess.Popen(
        [stockfish_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    results: list[AnalysisResult] = []

    try:
        proc.stdin.write("uci\n")
        proc.stdin.write("setoption name Hash value 128\n")
        proc.stdin.write("setoption name Threads value 1\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "uciok":
                break
        proc.stdin.write("isready\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "readyok":
                break

        for i, pos in enumerate(positions):
            pos_start = time.time()

            _, played_eval = _run_search(proc, pos.fen_before, [pos.move_uci], depth)
            best_move_uci, _ = _run_search(proc, pos.fen_before, [], depth)

            if best_move_uci and best_move_uci != pos.move_uci and best_move_uci != "(none)":
                _, best_eval = _run_search(proc, pos.fen_before, [best_move_uci], depth)
            else:
                best_move_uci = pos.move_uci
                best_eval = played_eval

            eval_delta: int | None = None
            if best_eval is not None and played_eval is not None:
                board = chess.Board(pos.fen_before)
                if board.turn == chess.WHITE:
                    eval_delta = best_eval - played_eval
                else:
                    eval_delta = played_eval - best_eval
                eval_delta = max(eval_delta, 0)

            best_move_san = _uci_to_san(pos.fen_before, best_move_uci) if best_move_uci else None
            pos_elapsed = time.time() - pos_start

            delta_str = f"Δ{eval_delta}cp" if eval_delta is not None else "Δ?"
            best_str = best_move_san or best_move_uci or "?"
            # Use print (not log) so output appears from child processes too
            if _verbose:
                print(
                    f"  {pos.move_san} ({pos.move_uci}) → best {best_str}  "
                    f"{delta_str}  [{pos_elapsed:.1fs}]",
                    flush=True,
                )

            results.append(AnalysisResult(
                fen_before=pos.fen_before,
                move_uci=pos.move_uci,
                move_san=pos.move_san,
                best_move_uci=best_move_uci if best_move_uci != "(none)" else None,
                best_move_san=best_move_san,
                played_eval=played_eval,
                best_eval=best_eval,
                eval_delta=eval_delta,
            ))
    finally:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.terminate()
        proc.wait(timeout=5)

    return results


def _worker_thread(
    worker_id: int,
    work_queue: queue.Queue[PositionToAnalyze | None],
    result_list: list[AnalysisResult],
    result_lock: threading.Lock,
    counter: list[int],
    total: int,
    start_time: float,
    depth: int,
    stockfish_path: str,
) -> None:
    """Worker thread: starts a persistent Stockfish process, pulls positions
    from the shared queue, and logs each one immediately."""
    proc = subprocess.Popen(
        [stockfish_path],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    try:
        proc.stdin.write("uci\n")
        proc.stdin.write("setoption name Hash value 128\n")
        proc.stdin.write("setoption name Threads value 1\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "uciok":
                break
        proc.stdin.write("isready\n")
        proc.stdin.flush()
        for line in proc.stdout:
            if line.strip() == "readyok":
                break

        while True:
            pos = work_queue.get()
            if pos is None:
                break

            pos_start = time.time()

            _, played_eval = _run_search(proc, pos.fen_before, [pos.move_uci], depth)
            best_move_uci, _ = _run_search(proc, pos.fen_before, [], depth)

            if best_move_uci and best_move_uci != pos.move_uci and best_move_uci != "(none)":
                _, best_eval = _run_search(proc, pos.fen_before, [best_move_uci], depth)
            else:
                best_move_uci = pos.move_uci
                best_eval = played_eval

            eval_delta: int | None = None
            if best_eval is not None and played_eval is not None:
                board = chess.Board(pos.fen_before)
                if board.turn == chess.WHITE:
                    eval_delta = best_eval - played_eval
                else:
                    eval_delta = played_eval - best_eval
                eval_delta = max(eval_delta, 0)

            best_move_san = _uci_to_san(pos.fen_before, best_move_uci) if best_move_uci else None
            pos_elapsed = time.time() - pos_start

            result = AnalysisResult(
                fen_before=pos.fen_before,
                move_uci=pos.move_uci,
                move_san=pos.move_san,
                best_move_uci=best_move_uci if best_move_uci != "(none)" else None,
                best_move_san=best_move_san,
                played_eval=played_eval,
                best_eval=best_eval,
                eval_delta=eval_delta,
            )

            with result_lock:
                result_list.append(result)
                counter[0] += 1
                n = counter[0]

            delta_str = f"Δ{eval_delta}cp" if eval_delta is not None else "Δ?"
            best_str = best_move_san or best_move_uci or "?"
            elapsed = time.time() - start_time
            rate = n / elapsed if elapsed > 0 else 0
            eta_min = (total - n) / rate / 60 if rate > 0 else 0
            pct = n * 100 // total

            if _verbose:
                log.info(
                    "[w%d] %d/%d (%d%%) %s (%s) → best %s  %s  [%.1fs]  %.1f pos/s  ETA %.0fm",
                    worker_id, n, total, pct,
                    pos.move_san, pos.move_uci, best_str, delta_str,
                    pos_elapsed, rate, eta_min,
                )
            elif n % 50 == 0 or n == total:
                log.info(
                    "%d/%d (%d%%) — %.2f pos/s — ETA %.0fm",
                    n, total, pct, rate, eta_min,
                )

    finally:
        proc.stdin.write("quit\n")
        proc.stdin.flush()
        proc.terminate()
        proc.wait(timeout=5)


def upsert_results(db: Session, results: list[AnalysisResult]) -> int:
    """Upsert analysis results into the cache. Returns number of rows affected."""
    if not results:
        return 0

    values = [
        {
            "fen_before": r.fen_before,
            "move_uci": r.move_uci,
            "move_san": r.move_san,
            "best_move_uci": r.best_move_uci,
            "best_move_san": r.best_move_san,
            "played_eval": r.played_eval,
            "best_eval": r.best_eval,
            "eval_delta": r.eval_delta,
            "source": "precomputed",
        }
        for r in results
    ]

    dialect_name = db.bind.dialect.name if db.bind else ""
    if dialect_name == "sqlite":
        stmt = sqlite_insert(AnalysisCache).values(values)
    elif dialect_name == "postgresql":
        stmt = postgresql_insert(AnalysisCache).values(values)
    else:
        for val in values:
            existing = db.query(AnalysisCache).filter(
                AnalysisCache.fen_before == val["fen_before"],
                AnalysisCache.move_uci == val["move_uci"],
            ).first()
            if existing:
                for k, v in val.items():
                    if k not in ("fen_before", "move_uci"):
                        setattr(existing, k, v)
            else:
                db.add(AnalysisCache(**val))
        db.commit()
        return len(values)

    stmt = stmt.on_conflict_do_update(
        index_elements=[AnalysisCache.fen_before, AnalysisCache.move_uci],
        set_={
            "move_san": stmt.excluded.move_san,
            "best_move_uci": stmt.excluded.best_move_uci,
            "best_move_san": stmt.excluded.best_move_san,
            "played_eval": stmt.excluded.played_eval,
            "best_eval": stmt.excluded.best_eval,
            "eval_delta": stmt.excluded.eval_delta,
            "source": stmt.excluded.source,
        },
    )
    db.execute(stmt)
    db.commit()
    return len(values)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-compute Stockfish analysis for the opening book."
    )
    parser.add_argument(
        "--database-url",
        default=DEFAULT_DATABASE_URL,
        help=f"SQLAlchemy database URL (default: {DEFAULT_DATABASE_URL})",
    )
    parser.add_argument(
        "--eco-path",
        type=Path,
        default=DEFAULT_ECO_PATH,
        help=f"Path to eco.json (default: {DEFAULT_ECO_PATH})",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help=f"Stockfish search depth (default: {DEFAULT_DEPTH})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Number of parallel Stockfish processes (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--stockfish",
        default="stockfish",
        help="Path to Stockfish binary (default: stockfish)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract positions without running analysis or writing to DB.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Log each position as it is analyzed.",
    )
    args = parser.parse_args()

    global _verbose
    _verbose = args.verbose

    log.info("Loading opening book from %s", args.eco_path)
    positions = extract_positions(args.eco_path)
    log.info("Extracted %d unique positions to analyze", len(positions))

    if args.dry_run:
        log.info("Dry run — skipping analysis and database writes.")
        return

    engine = create_engine(args.database_url)
    Base.metadata.create_all(engine)

    total = len(positions)
    start = time.time()

    log.info(
        "Starting analysis: %d positions, depth %d, %d worker(s)",
        total, args.depth, args.workers,
    )

    # Shared state for worker threads
    work_queue: queue.Queue[PositionToAnalyze | None] = queue.Queue()
    result_list: list[AnalysisResult] = []
    result_lock = threading.Lock()
    counter = [0]  # mutable int for threads

    for pos in positions:
        work_queue.put(pos)
    # Poison pills to stop workers
    for _ in range(args.workers):
        work_queue.put(None)

    threads = []
    for i in range(args.workers):
        t = threading.Thread(
            target=_worker_thread,
            args=(i, work_queue, result_list, result_lock, counter, total, start, args.depth, args.stockfish),
            daemon=True,
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    # Batch-upsert all results
    log.info("Writing %d results to database...", len(result_list))
    with Session(engine) as db:
        for i in range(0, len(result_list), BATCH_SIZE):
            batch = result_list[i : i + BATCH_SIZE]
            upsert_results(db, batch)

    elapsed = time.time() - start
    rate = total / elapsed if elapsed > 0 else 0
    log.info(
        "Done. %d positions analyzed in %.1f minutes (%.2f pos/s avg).",
        total, elapsed / 60, rate,
    )


if __name__ == "__main__":
    main()
