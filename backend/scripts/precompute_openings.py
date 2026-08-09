#!/usr/bin/env python3
"""Pre-compute canonical Stockfish analysis for every opening-book position.

Reads public/data/openings/eco.json, walks each opening's UCI move sequence to
produce (fen_before, move_uci) pairs, deduplicates them, analyzes each position
with Stockfish under the pinned canonical profile, and upserts resolver-complete
rows into analysis_cache via the shared quality-aware writer.

The analyzer mirrors the frontend worker exactly (search ordering, terminal-score
synthesis, score conversion, PV/continuation rule, win-chance classifier, and the
per-position reset boundary); its output contract is versioned by
``analysis_profiles.ANALYZER_PROTOCOL_VERSION``.

Usage:
    python scripts/precompute_openings.py
    python scripts/precompute_openings.py --database-url postgresql+psycopg://...
    python scripts/precompute_openings.py --depth 24 --workers 4
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("precompute")

import chess
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.analysis_cache_repo import (
    write_analysis_cache_rows,
    _row_to_dict,
)
# _identity_verified moved to the policy module (g-xox0: single projector home).
from app.analysis_cache_policy import Reason, _identity_verified
from app.analysis_profiles import (
    ANALYZER_PROTOCOL_VERSION,
    get_profile,
    resolve_profile,
    stamp_identity,
)
from app.evidence_contracts import (
    RESOLVER_COMPLETE_V2,
    contract_satisfied,
    get_contract,
)
from app.models import AnalysisCache, Base
from app.move_classification import EngineScore, classify_move_advanced

DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/ghostreplay"
DEFAULT_ECO_PATH = PROJECT_ROOT / "public" / "data" / "openings" / "eco.json"
DEFAULT_DEPTH = 24
DEFAULT_WORKERS = 1
BATCH_SIZE = 100

# Pinned canonical UCI options (must equal the canonical profile manifest).
HASH_MB = 128
THREADS = 1
MULTIPV = 1

# Engine liveness deadlines. A handshake or search that exceeds these is a hung
# or dead engine; fail closed rather than block forever. The search deadline is
# PER search, and a position runs up to three searches (root/played/best). The
# slowest opening-book positions take ~4 min for a single depth-24 search even on
# an idle box, so the default is generous enough that a timeout means a genuinely
# dead engine rather than an honest-but-slow search losing a race with CPU load.
# Override with --search-deadline when tuning for a slower/faster box.
HANDSHAKE_DEADLINE_S = 30.0
SEARCH_DEADLINE_S = 600.0

# Mate<->cp conversion, identical to analysisUtils.ts mateToCp.
_MATE_BASE = 10000
_MATE_DECAY = 10

# Outcome buckets for fail-closed accounting.
OK = "ok"
SKIPPED_NO_CONTINUATION = "skipped-no-continuation"
ERROR = "error"

_ACCEPTED_REASONS = frozenset(
    {
        Reason.NEW_KEY,
        Reason.DOMINATES_REPLACE,
        Reason.LEGACY_REPLACED_BY_AUTH,
        # A MEASURED replacement (D4 steps 4-5) is a successful write like any other.
        # Latent today — the two canonical manifests compare EQUAL, so no canonical
        # pair ranks — but a future deeper canonical profile with no explicit edge
        # would rank, and without this entry that successful upgrade would land in
        # `write_failures` and exit the run unsuccessfully (g-mk1d review).
        Reason.STRENGTH_REPLACE,
        # A cross-grain authority replacement (Rules 4b/5b) is likewise a successful
        # write. Latent today — this script targets resolver-complete-v2, which is not
        # a grain-split contract, so the rule cannot fire — but once the canonical
        # writer emits move-complete-v1 (g-v2-deprecation.2) every stored browser-v2
        # row it relocates earns this verdict, and without this entry a whole run of
        # correct replacements would land in `write_failures` (g-6xc3).
        Reason.CROSS_GRAIN_AUTHORITY_REPLACE,
        # The same canonical profile's legacy combined v2 row may transition in
        # place after this producer has durably committed its position winner.
        # This is a successful REPLACE during the g-v2-deprecation.2 cutover.
        Reason.SAME_PROFILE_GRAIN_TRANSITION_REPLACE,
        # PROTOCOL_CORRECTED_REPLACE stays out: this producer is authoritative, and
        # the authority barrier resolves canonical-vs-browser before explicit edges,
        # so a canonical write can never earn a protocol-correction verdict.
        Reason.SAME_PROFILE_SUPERSET_MERGE,
        Reason.SAME_PROFILE_CONTRACT_UPGRADE,
        # Accepted ONLY when post-write verification confirms the stored row is
        # the wanted authoritative v2 evidence (an exact re-run is idempotent).
        Reason.SAME_PROFILE_IDEMPOTENT,
    }
)

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
    best_line_uci: str | None
    played_eval: int | None  # white-relative cp
    played_eval_mate: int | None  # white-relative mate count or None
    best_eval: int | None  # white-relative cp
    best_eval_mate: int | None  # white-relative mate count or None
    eval_delta: int | None
    classification: str | None


class EngineTimeout(RuntimeError):
    """Raised when the engine fails to respond within a deadline."""


class EngineDied(RuntimeError):
    """Raised when the engine subprocess exits before producing a result."""


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


# ── Engine process wrapper with a reader thread + per-read deadline ──────────

class EngineProc:
    """Persistent Stockfish process with a background reader and read deadlines.

    ``subprocess`` pipes have no native read timeout, so a reader thread drains
    stdout into a queue and ``readline`` blocks only up to a deadline. EOF (engine
    exit) and missed deadlines surface as exceptions so a hung/dead engine fails
    closed instead of blocking the run forever.
    """

    def __init__(self, path: str):
        self.proc = subprocess.Popen(
            [path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._q: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for line in self.proc.stdout:
                self._q.put(line)
        finally:
            self._q.put(None)  # EOF sentinel

    def send(self, command: str) -> None:
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise EngineDied("engine stdin closed") from exc

    def readline(self, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EngineTimeout("engine read deadline exceeded")
        try:
            line = self._q.get(timeout=remaining)
        except queue.Empty:
            if self.proc.poll() is not None:
                raise EngineDied("engine exited during search")
            raise EngineTimeout("engine read deadline exceeded")
        if line is None:
            raise EngineDied("engine closed stdout")
        return line.strip()

    def await_token(self, token: str, deadline: float) -> None:
        while True:
            line = self.readline(deadline)
            if line == token or line.startswith(token):
                return

    def close(self) -> None:
        try:
            self.send("quit")
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        # The reader thread owns stdout, so let it observe EOF and finish before
        # the pipes are closed; otherwise the fds leak to the garbage collector
        # (an unraisable ResourceWarning from whichever thread happens to run).
        self._reader.join(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass


def _init_engine(engine: EngineProc) -> None:
    """Single shared handshake: uci -> uciok -> setoptions -> isready -> readyok.

    All options are sent AFTER ``uciok`` (the previous code sent them before, which
    is out of spec). Used by every engine entry point so the handshake/options are
    identical everywhere.
    """
    deadline = time.monotonic() + HANDSHAKE_DEADLINE_S
    engine.send("uci")
    engine.await_token("uciok", deadline)
    engine.send(f"setoption name Hash value {HASH_MB}")
    engine.send(f"setoption name Threads value {THREADS}")
    engine.send(f"setoption name MultiPV value {MULTIPV}")
    engine.send("isready")
    engine.await_token("readyok", deadline)


def _reset_position(engine: EngineProc) -> None:
    """Reset boundary: ucinewgame + isready ONCE before each independent position
    (never between the root/played/best searches of one position)."""
    deadline = time.monotonic() + HANDSHAKE_DEADLINE_S
    engine.send("ucinewgame")
    engine.send("isready")
    engine.await_token("readyok", deadline)


# ── Score parsing / conversion (mirrors analysisUtils.ts) ────────────────────

def _parse_info(line: str) -> tuple[dict | None, list[str] | None]:
    """Parse a Stockfish ``info`` line into a side-to-move raw score + PV moves."""
    tokens = line.split()
    raw: dict | None = None
    pv: list[str] | None = None
    try:
        idx = tokens.index("score")
        score_type = tokens[idx + 1]
        score_value = int(tokens[idx + 2])
        if score_type in ("cp", "mate"):
            raw = {"type": score_type, "value": score_value}
    except (ValueError, IndexError):
        raw = None
    try:
        pidx = tokens.index("pv")
        moves = tokens[pidx + 1:]
        pv = moves if moves else None
    except ValueError:
        pv = None
    return raw, pv


def _raw_to_white_cp(raw: dict, side_to_move_is_white: bool) -> int:
    """White-relative cp for a side-to-move raw score (mirrors mateToCp)."""
    if raw["type"] == "mate":
        v = raw["value"]
        if v == 0:
            cp = -_MATE_BASE
        else:
            cp = (1 if v > 0 else -1) * (_MATE_BASE - abs(v) * _MATE_DECAY)
    else:
        cp = raw["value"]
    return cp if side_to_move_is_white else -cp


def _white_mate(raw: dict, side_to_move_is_white: bool) -> int | None:
    """White-relative mate count for a side-to-move raw score, or None."""
    if raw["type"] != "mate":
        return None
    white = raw["value"] if side_to_move_is_white else -raw["value"]
    return 0 if white == 0 else white


def _terminal_score_after_move(fen: str, move_uci: str) -> dict | None:
    """Synthesize a side-to-move raw score for a game-over position (no search).

    Port of analysisWorker.ts terminalScoreAfterMove: native Stockfish emits no
    scored info line after mate/stalemate. Returns ``{'mate':0}`` for checkmate,
    ``{'cp':0}`` for stalemate/draw, else ``None`` (position is not terminal).
    """
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(move_uci)
        if move not in board.legal_moves:
            return None
        board.push(move)
    except Exception:
        return None
    if not board.is_game_over():
        return None
    if board.is_checkmate():
        return {"type": "mate", "value": 0}
    return {"type": "cp", "value": 0}


def _run_search(
    engine: EngineProc, fen: str, moves: list[str], depth: int
) -> tuple[str, dict | None, int | None, list[str] | None]:
    """Run one search; return (bestmove, raw_score, white_cp, pv_moves).

    ``raw_score`` is the engine-reported {type,value} from the resulting (post-
    moves) position's POV (for the classifier); ``white_cp`` is white-relative
    (for storage); ``pv_moves`` is the PV from the last scored info line.
    """
    moves_segment = f" moves {' '.join(moves)}" if moves else ""
    engine.send(f"position fen {fen}{moves_segment}")
    engine.send(f"go depth {depth}")

    board = chess.Board(fen)
    for m in moves:
        board.push(chess.Move.from_uci(m))
    side_is_white = board.turn == chess.WHITE

    last_raw: dict | None = None
    last_pv: list[str] | None = None
    bestmove = ""

    deadline = time.monotonic() + SEARCH_DEADLINE_S
    while True:
        line = engine.readline(deadline)
        if line.startswith("info") and "score" in line:
            raw, pv = _parse_info(line)
            if raw is not None:
                last_raw = raw
                if pv is not None:
                    last_pv = pv
        elif line.startswith("bestmove"):
            parts = line.split()
            bestmove = parts[1] if len(parts) > 1 else ""
            break

    white_cp = _raw_to_white_cp(last_raw, side_is_white) if last_raw else None
    return bestmove, last_raw, white_cp, last_pv


def _uci_to_san(fen: str, uci_move: str) -> str | None:
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci_move)
        return board.san(move)
    except Exception:
        return None


def _build_best_line(
    best_move: str, root_pv: list[str] | None, continuation_pv: list[str] | None
) -> list[str]:
    """Port of analysisWorker.ts buildBestLine."""
    if root_pv and len(root_pv) > 1 and root_pv[0] == best_move:
        return root_pv
    if continuation_pv:
        return [best_move, *continuation_pv]
    return [best_move]


def _analyze_with_engine(
    engine: EngineProc, pos: PositionToAnalyze, depth: int
) -> tuple[AnalysisResult, str]:
    """Analyze one position on an initialized engine. Returns (result, outcome).

    Outcome is ``ok`` or ``skipped-no-continuation``. Engine liveness failures
    raise (EngineTimeout / EngineDied) and abort the run.
    """
    _reset_position(engine)

    fen = pos.fen_before
    board = chess.Board(fen)
    mover = "white" if board.turn == chess.WHITE else "black"
    opp = "black" if mover == "white" else "white"
    # After any legal move it is the opponent to move.
    opp_is_white = opp == "white"

    def _empty(best_uci: str | None) -> AnalysisResult:
        return AnalysisResult(
            fen_before=fen, move_uci=pos.move_uci, move_san=pos.move_san,
            best_move_uci=best_uci, best_move_san=None, best_line_uci=None,
            played_eval=None, played_eval_mate=None,
            best_eval=None, best_eval_mate=None,
            eval_delta=None, classification=None,
        )

    # 1. Root/best search FIRST (so TT warmup from the played move never biases
    #    the root PV), mirroring analysisWorker.ts.
    best_move_uci, _best_root_raw, _best_root_cp, root_pv = _run_search(
        engine, fen, [], depth
    )
    if not best_move_uci or best_move_uci == "(none)":
        return _empty(None), SKIPPED_NO_CONTINUATION

    # 2. Eval after the played move (terminal synthesis if game over).
    played_terminal = _terminal_score_after_move(fen, pos.move_uci)
    if played_terminal is not None:
        played_raw, played_cp, played_pv = played_terminal, _raw_to_white_cp(
            played_terminal, opp_is_white
        ), None
    else:
        _, played_raw, played_cp, played_pv = _run_search(
            engine, fen, [pos.move_uci], depth
        )

    is_best = best_move_uci == pos.move_uci

    # 3. Eval after the best move (reuse played search when best == played).
    if is_best:
        post_best_raw, best_cp, post_best_pv = played_raw, played_cp, played_pv
    else:
        best_terminal = _terminal_score_after_move(fen, best_move_uci)
        if best_terminal is not None:
            post_best_raw, best_cp, post_best_pv = best_terminal, _raw_to_white_cp(
                best_terminal, opp_is_white
            ), None
        else:
            _, post_best_raw, best_cp, post_best_pv = _run_search(
                engine, fen, [best_move_uci], depth
            )

    # eval_delta is side-to-move (mover) relative, clamped at >= 0.
    eval_delta: int | None = None
    if played_cp is not None and best_cp is not None:
        eval_delta = best_cp - played_cp if mover == "white" else played_cp - best_cp
        eval_delta = max(eval_delta, 0)

    played_mate = _white_mate(played_raw, opp_is_white) if played_raw else None
    best_mate = _white_mate(post_best_raw, opp_is_white) if post_best_raw else None

    # Classification: advanced win-chance model only. No fallback to the
    # deprecated delta classifier — reject the row instead.
    classification: str | None = None
    if post_best_raw is not None and played_raw is not None:
        classification = classify_move_advanced(
            EngineScore.from_dict(post_best_raw),
            EngineScore.from_dict(played_raw),
            score_pov=opp,
            mover=mover,
            is_best=is_best,
        )
    if classification is None:
        return _empty(best_move_uci), ERROR

    # best_line: prefer the root PV, else reuse the already-run continuation.
    continuation_pv = played_pv if is_best else post_best_pv
    best_line = _build_best_line(best_move_uci, root_pv, continuation_pv)
    if len(best_line) <= 1:
        # mate-in-one / no-continuation: intentionally not cached (consistent with
        # the frontend canResolveCachedAnalysis len>1 rule), bucketed as benign.
        return _empty(best_move_uci), SKIPPED_NO_CONTINUATION

    result = AnalysisResult(
        fen_before=fen,
        move_uci=pos.move_uci,
        move_san=pos.move_san,
        best_move_uci=best_move_uci,
        best_move_san=_uci_to_san(fen, best_move_uci),
        best_line_uci=" ".join(best_line),
        played_eval=played_cp,
        played_eval_mate=played_mate,
        best_eval=best_cp,
        best_eval_mate=best_mate,
        eval_delta=eval_delta,
        classification=classification,
    )
    return result, OK


def analyze_position(
    pos: PositionToAnalyze, depth: int, stockfish_path: str
) -> tuple[AnalysisResult, str]:
    """Analyze a single position with a fresh engine (used by tests)."""
    engine = EngineProc(stockfish_path)
    try:
        _init_engine(engine)
        return _analyze_with_engine(engine, pos, depth)
    finally:
        engine.close()


def _worker_thread(
    worker_id: int,
    work_queue: "queue.Queue[PositionToAnalyze | None]",
    result_list: list[AnalysisResult],
    outcomes: dict,
    result_lock: threading.Lock,
    abort: threading.Event,
    worker_errors: list,
    counter: list[int],
    total: int,
    start_time: float,
    depth: int,
    stockfish_path: str,
) -> None:
    """Worker: one persistent engine, pull positions, fail closed on engine death.

    Engine construction/handshake happen INSIDE the failure-accounting block: a
    worker that fails to initialize records the error and aborts the run, so the
    survivors draining every key can never yield a falsely successful run.
    """
    engine: EngineProc | None = None
    try:
        engine = EngineProc(stockfish_path)
        _init_engine(engine)
    except Exception as exc:
        log.error("[w%d] engine initialization failed: %s", worker_id, exc)
        with result_lock:
            worker_errors.append((worker_id, f"init: {exc}"))
        abort.set()
        if engine is not None:
            engine.close()
        return

    try:
        while True:
            if abort.is_set():
                break
            pos = work_queue.get()
            if pos is None:
                break

            pos_start = time.time()
            try:
                result, outcome = _analyze_with_engine(engine, pos, depth)
            except (EngineTimeout, EngineDied) as exc:
                log.error("[w%d] engine failure on %s (%s) at fen %s: %s",
                          worker_id, pos.move_san, pos.move_uci, pos.fen_before, exc)
                with result_lock:
                    outcomes[(pos.fen_before, pos.move_uci)] = ERROR
                abort.set()
                break
            except Exception as exc:  # unexpected: abort the run
                log.error("[w%d] unexpected error on %s (%s) at fen %s: %s",
                          worker_id, pos.move_san, pos.move_uci, pos.fen_before, exc)
                with result_lock:
                    outcomes[(pos.fen_before, pos.move_uci)] = ERROR
                    worker_errors.append((worker_id, f"{pos.move_uci}: {exc}"))
                abort.set()
                break

            with result_lock:
                outcomes[(pos.fen_before, pos.move_uci)] = outcome
                if outcome == OK:
                    result_list.append(result)
                counter[0] += 1
                n = counter[0]

            if n % 50 == 0 or n == total:
                elapsed = time.time() - start_time
                rate = n / elapsed if elapsed > 0 else 0
                eta_min = (total - n) / rate / 60 if rate > 0 else 0
                log.info("%d/%d (%d%%) — %.2f pos/s — ETA %.0fm",
                         n, total, n * 100 // total, rate, eta_min)
            if _verbose:
                log.info("[w%d] %s (%s) → best %s Δ%s [%s] (%.1fs) fen %s",
                         worker_id, pos.move_san, pos.move_uci,
                         result.best_move_uci, result.eval_delta, outcome,
                         time.time() - pos_start, pos.fen_before)
    finally:
        engine.close()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def observe_engine_identity(stockfish_path: str, depth: int) -> dict:
    """Derive runtime-observable identity (RESOLUTION_FIELDS) from the engine.

    Captures only what can be verified reproducibly without extracting an embedded
    NNUE blob: the executable SHA-256 (``engine_build``), the parsed version, and
    the EvalFile / EvalFileSmall *filenames* reported over UCI. The canonical
    profile manifest carries the full network hashes; ``stamp_identity`` copies
    them onto rows after resolution.
    """
    observed: dict = {
        "engine_name": None,
        "engine_version": None,
        "engine_build": None,
        "eval_file": None,
        "eval_file_small": None,
        "search_limit_type": "depth",
        "search_limit_value": depth,
        "threads": THREADS,
        "hash_mb": HASH_MB,
        "multipv": MULTIPV,
        "analyzer_protocol_version": ANALYZER_PROTOCOL_VERSION,
    }
    resolved = shutil.which(stockfish_path) or stockfish_path
    try:
        observed["engine_build"] = _sha256_file(resolved)
    except OSError:
        observed["engine_build"] = None

    engine = EngineProc(resolved)
    try:
        deadline = time.monotonic() + HANDSHAKE_DEADLINE_S
        engine.send("uci")
        while True:
            line = engine.readline(deadline)
            if line.startswith("id name "):
                tokens = line[len("id name "):].strip().split()
                if tokens:
                    observed["engine_name"] = tokens[0]
                if len(tokens) > 1:
                    observed["engine_version"] = tokens[1]
            elif "name EvalFileSmall" in line and "default" in line:
                parts = line.split("default")
                if len(parts) > 1 and parts[1].strip():
                    observed["eval_file_small"] = parts[1].strip().split()[0]
            elif "name EvalFile" in line and "default" in line:
                parts = line.split("default")
                if len(parts) > 1 and parts[1].strip():
                    observed["eval_file"] = parts[1].strip().split()[0]
            elif line.startswith("uciok"):
                break
    finally:
        engine.close()
    return observed


def assert_can_produce_target_contract(contract_id: str = RESOLVER_COMPLETE_V2) -> None:
    """Fail-fast: refuse to run unless the script can populate every required
    field of its declared target contract."""
    contract = get_contract(contract_id)
    if contract is None:
        raise SystemExit(f"Unknown target evidence contract: {contract_id!r}")
    producible = {f.name for f in dataclasses.fields(AnalysisResult)}
    # fen_before is required by the contract and is a field on AnalysisResult.
    missing = set(contract.required_fields) - producible
    if missing:
        raise SystemExit(
            "precompute cannot satisfy target contract "
            f"{contract_id!r}: AnalysisResult is missing required field(s) "
            f"{sorted(missing)}."
        )


def _result_row(r: AnalysisResult, *, profile_id: str, observed: dict) -> dict:
    return {
        "fen_before": r.fen_before,
        "move_uci": r.move_uci,
        "move_san": r.move_san,
        "best_move_uci": r.best_move_uci,
        "best_move_san": r.best_move_san,
        "best_line_uci": r.best_line_uci,
        "played_eval": r.played_eval,
        "played_eval_mate": r.played_eval_mate,
        "best_eval": r.best_eval,
        "best_eval_mate": r.best_eval_mate,
        "eval_delta": r.eval_delta,
        "classification": r.classification,
        "source": "precomputed",
        "analysis_profile_id": profile_id,
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        **observed,
        # Phase-2 stamp: manifest-derived full network identity + digest.
        **stamp_identity(profile_id),
    }


def upsert_results(
    db: Session,
    results: list[AnalysisResult],
    *,
    profile_id: str,
    observed: dict,
) -> list[tuple[tuple[str, str], Reason]]:
    """Upsert canonical results via the shared writer; return per-key reasons."""
    if not results:
        return []
    rows = [_result_row(r, profile_id=profile_id, observed=observed) for r in results]
    db.commit()
    return write_analysis_cache_rows(db, rows)


# Evidence columns that must match this run's produced result exactly.
_VERIFIED_EVIDENCE_FIELDS = (
    "best_move_uci",
    "best_move_san",
    "best_line_uci",
    "played_eval",
    "played_eval_mate",
    "best_eval",
    "best_eval_mate",
    "eval_delta",
    "classification",
)


def _verify_stored(
    db: Session, expected: list[AnalysisResult], *, profile_id: str
) -> list[tuple[str, str]]:
    """Re-query expected keys; return the list of keys that did NOT persist as the
    wanted authoritative v2 evidence. Empty list means the run is verified.

    This is the gate that makes the idempotent re-run reason safe: it does NOT
    trust the writer's reason, it re-reads the stored row and asserts the
    persisted profile/contract AND every evidence value equals what THIS run
    produced. A stale value (e.g. a leftover best_eval_mate the rerun set to
    None) is therefore caught instead of silently surviving as "idempotent".
    """
    profile = get_profile(profile_id)
    failures: list[tuple[str, str]] = []
    for r in expected:
        row = (
            db.query(AnalysisCache)
            .filter(
                AnalysisCache.fen_before == r.fen_before,
                AnalysisCache.move_uci == r.move_uci,
            )
            .first()
        )
        ok = (
            row is not None
            and row.analysis_profile_id == profile_id
            and row.evidence_contract_id == RESOLVER_COMPLETE_V2
            and profile is not None
            and row.profile_manifest_digest == profile.profile_manifest_digest
            and row.best_line_uci is not None
            and row.best_line_uci.split()[:1] == [row.best_move_uci]
            # Every produced evidence value must match the stored value exactly.
            and all(
                getattr(row, f) == getattr(r, f)
                for f in _VERIFIED_EVIDENCE_FIELDS
            )
        )
        if not ok:
            failures.append((r.fen_before, r.move_uci))
    return failures


def filter_unstored_positions(
    db: Session, positions: list[PositionToAnalyze], profile_id: str
) -> tuple[list[PositionToAnalyze], int]:
    """Return ``(remaining_positions, already_stored_count)`` for resume.

    A position is treated as already done only when its stored row passes the
    exact gate prod uses at read time: full stored identity matches the
    registered profile (``_identity_verified`` — covers the manifest digest and
    every IDENTITY_FIELD) AND the row satisfies the v2 contract's semantic
    validation (``contract_satisfied`` — classification enum, full eval triple,
    PV length/order, delta consistency). Rows that only nominally claim the
    profile/contract but fail either check are KEPT (re-analyzed), so a malformed
    legacy row can never short-circuit into a false ``status: ok``.
    """
    rows = (
        db.query(AnalysisCache)
        .filter(
            AnalysisCache.analysis_profile_id == profile_id,
            AnalysisCache.evidence_contract_id == RESOLVER_COMPLETE_V2,
        )
        .all()
    )
    verified: set[tuple[str, str]] = set()
    for row in rows:
        data = _row_to_dict(row)
        if _identity_verified(data) and contract_satisfied(RESOLVER_COMPLETE_V2, data):
            verified.add((row.fen_before, row.move_uci))
    remaining = [
        p for p in positions if (p.fen_before, p.move_uci) not in verified
    ]
    return remaining, len(positions) - len(remaining)


def main() -> None:
    global _verbose, SEARCH_DEADLINE_S
    parser = argparse.ArgumentParser(
        description="Pre-compute canonical Stockfish analysis for the opening book."
    )
    parser.add_argument("--database-url", default=DEFAULT_DATABASE_URL)
    parser.add_argument("--eco-path", type=Path, default=DEFAULT_ECO_PATH)
    parser.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--stockfish", default="stockfish")
    parser.add_argument("--search-deadline", type=float, default=SEARCH_DEADLINE_S,
                        help="Per-search wall-clock deadline in seconds (a position "
                             "runs up to 3 searches). Exceeding it is treated as a "
                             f"dead engine and aborts the run. Default: {SEARCH_DEADLINE_S:g}.")
    parser.add_argument("--no-resume", action="store_true",
                        help="Disable resume; re-analyze every position even if an "
                             "authoritative resolver-complete-v2 row already exists "
                             "for the resolved profile.")
    parser.add_argument("--manifest-out", type=Path, default=None,
                        help="Run manifest sidecar path (default: "
                             "backend/precompute_run_manifest.json; always written).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _verbose = args.verbose
    SEARCH_DEADLINE_S = args.search_deadline

    if args.dry_run:
        log.info("Loading opening book from %s", args.eco_path)
        positions = extract_positions(args.eco_path)
        log.info("Extracted %d unique positions to analyze", len(positions))
        log.info("Dry run — skipping analysis and database writes.")
        return

    start = time.time()
    manifest_path = args.manifest_out or (BACKEND_ROOT / "precompute_run_manifest.json")
    # Write a "running" manifest immediately — BEFORE loading the opening book —
    # so a prior "ok" sidecar can never outlive a later run that fails anywhere,
    # including an ECO load failure, resolution, connection, migration, or write.
    # This write is STRICT: if the sidecar path is unwritable we refuse to start a
    # run whose outcome could not be recorded, rather than proceed and let a stale
    # "ok" masquerade as this run's result.
    _write_run_manifest(manifest_path, "running", started_at=start, strict=True)

    # Track whether THIS run persisted a terminal manifest. We must NOT infer that
    # from disk: a prior run's stale "ok"/"failed" sidecar is indistinguishable
    # from one written by this run, so a failed "running" write over an unwritable
    # stale "ok" would otherwise be mistaken for our own terminal status.
    terminal_written = False

    try:
        log.info("Loading opening book from %s", args.eco_path)
        positions = extract_positions(args.eco_path)
        total = len(positions)
        log.info("Extracted %d unique positions to analyze", total)

        # Fail-fast BEFORE launching any engine.
        assert_can_produce_target_contract()

        # Resolve the executable ONCE (absolute path) and launch that exact path in
        # every worker, instead of each worker re-resolving via PATH.
        resolved_path = shutil.which(args.stockfish) or args.stockfish

        observed = observe_engine_identity(resolved_path, args.depth)
        profile_id = resolve_profile(observed)
        if profile_id is None:
            raise SystemExit(
                "Engine identity does not match any active canonical profile; "
                "refusing to run an expensive non-authoritative precompute. Observed: "
                f"engine_build={observed.get('engine_build')}, "
                f"eval_file={observed.get('eval_file')}, "
                f"depth={observed.get('search_limit_value')}."
            )
        log.info("Resolved analysis profile: %s", profile_id)

        engine = create_engine(args.database_url)
        Base.metadata.create_all(engine)

        # Resume: skip positions that already carry a TRUSTWORTHY authoritative
        # resolver-complete-v2 row for THIS resolved profile. "Trustworthy" means
        # the exact gate prod uses at read time: full stored identity matches the
        # registered profile (``_identity_verified``, which covers the manifest
        # digest and every IDENTITY_FIELD) AND the row passes the v2 contract's
        # semantic validation (``contract_satisfied`` — classification enum, the
        # full eval triple, PV length/order, and delta consistency). A row that
        # only nominally claims the profile/contract but fails either check is NOT
        # skipped: it is re-analyzed and re-written, then caught by the final
        # verification gate — so a malformed legacy row can never yield a false
        # ``status: ok``. For a brand-new profile (e.g. SF19) nothing matches, so
        # resume is a no-op and the full book is analyzed.
        book_total = total
        already_stored = 0
        if not args.no_resume:
            with Session(engine) as db:
                positions, already_stored = filter_unstored_positions(
                    db, positions, profile_id
                )
            total = len(positions)
            log.info(
                "Resume: %d/%d positions already verified-stored for %s; "
                "%d remaining.",
                already_stored, book_total, profile_id, total,
            )

        if total == 0:
            log.info("Nothing to analyze — all positions already stored. "
                     "Skipping worker startup.")
            result_list: list[AnalysisResult] = []
            outcomes: dict = {}
            unprocessed: set = set()
            errored: list = []
            skipped: list = []
            write_failures: list[tuple[str, str]] = []
            verify_failures: list[tuple[str, str]] = []
            worker_errors = []
            accepted = 0
            _write_run_manifest(
                manifest_path, "ok", started_at=start, strict=True,
                profile_id=profile_id,
                profile_manifest_digest=get_profile(profile_id).profile_manifest_digest,
                eco_sha256=_sha256_file(str(args.eco_path)),
                workers=args.workers, depth=args.depth,
                total=total, book_total=book_total,
                already_stored_skipped=already_stored,
                stored=0, skipped_no_continuation=0, errored=0,
                unprocessed=0, write_failures=0, verify_failures=0, worker_errors=0,
            )
            terminal_written = True
            log.info("Done. Nothing to do in %.1f min.", (time.time() - start) / 60)
            return

        log.info("Starting analysis: %d positions, depth %d, %d worker(s)",
                 total, args.depth, args.workers)

        work_queue: "queue.Queue[PositionToAnalyze | None]" = queue.Queue()
        result_list: list[AnalysisResult] = []
        outcomes: dict = {}
        result_lock = threading.Lock()
        abort = threading.Event()
        worker_errors: list = []
        counter = [0]

        for pos in positions:
            work_queue.put(pos)
        for _ in range(args.workers):
            work_queue.put(None)

        threads = []
        for i in range(args.workers):
            t = threading.Thread(
                target=_worker_thread,
                args=(i, work_queue, result_list, outcomes, result_lock, abort,
                      worker_errors, counter, total, start, args.depth, resolved_path),
                daemon=True,
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join()

        # Account for queued-but-unprocessed keys (when a worker aborted).
        processed_keys = set(outcomes)
        all_keys = {(p.fen_before, p.move_uci) for p in positions}
        unprocessed = all_keys - processed_keys
        errored = [k for k, o in outcomes.items() if o == ERROR]
        skipped = [k for k, o in outcomes.items() if o == SKIPPED_NO_CONTINUATION]

        log.info("Writing %d results to database...", len(result_list))
        accepted = 0
        write_failures: list[tuple[str, str]] = []
        with Session(engine) as db:
            for i in range(0, len(result_list), BATCH_SIZE):
                batch = result_list[i: i + BATCH_SIZE]
                reasons = upsert_results(db, batch, profile_id=profile_id, observed=observed)
                for key, reason in reasons:
                    if reason in _ACCEPTED_REASONS:
                        accepted += 1
                    else:
                        write_failures.append(key)

            # Post-write verification (authoritative gate): the idempotent path is
            # only trusted once the stored row is confirmed to be the wanted v2 row.
            verify_failures = _verify_stored(db, result_list, profile_id=profile_id)

        elapsed = time.time() - start
        log.info(
            "Done. stored=%d skipped-no-continuation=%d errored=%d unprocessed=%d "
            "worker_errors=%d in %.1f min.",
            accepted, len(skipped), len(errored), len(unprocessed),
            len(worker_errors), elapsed / 60,
        )

        # A worker that failed to initialize (or hit an unexpected error) is a
        # genuine failure even if survivors drained every key.
        failed = bool(
            errored or unprocessed or write_failures or verify_failures or worker_errors
        )
        # The terminal write is STRICT: if it cannot be persisted (bad path,
        # permissions, full disk), the run must NOT report success — the error
        # propagates and is handled as a failure below.
        _write_run_manifest(
            manifest_path,
            "failed" if failed else "ok",
            started_at=start,
            strict=True,
            profile_id=profile_id,
            profile_manifest_digest=get_profile(profile_id).profile_manifest_digest,
            eco_sha256=_sha256_file(str(args.eco_path)),
            workers=args.workers,
            depth=args.depth,
            total=total,
            book_total=book_total,
            already_stored_skipped=already_stored,
            stored=accepted,
            skipped_no_continuation=len(skipped),
            errored=len(errored),
            unprocessed=len(unprocessed),
            write_failures=len(write_failures),
            verify_failures=len(verify_failures),
            worker_errors=len(worker_errors),
        )
        terminal_written = True

        # Fail closed: "complete" means verified, not "loop finished".
        if failed:
            raise SystemExit(
                f"precompute did not complete cleanly: errored={len(errored)} "
                f"unprocessed={len(unprocessed)} write_failures={len(write_failures)} "
                f"verify_failures={len(verify_failures)} "
                f"worker_errors={len(worker_errors)} (see {manifest_path})"
            )
    except BaseException as exc:
        # Any unexpected exit (resolution/connection/migration/write exception, or
        # the fail-closed SystemExit above) must leave a non-"ok" manifest. Decide
        # solely from whether THIS run wrote a terminal manifest — never from disk
        # state, which may be a prior run's stale "ok". When we have not, always
        # ATTEMPT the failure stamp (best-effort): it overwrites a stale "ok" when
        # the path is writable, and merely logs when it is not.
        if not terminal_written:
            _write_run_manifest(
                manifest_path, "failed", started_at=start,
                total=locals().get("total"),
                error=f"{type(exc).__name__}: {exc}",
            )
        raise


def _write_run_manifest(path: Path, status: str, *, started_at: float | None = None,
                        strict: bool = False, **extra) -> None:
    """Write the run-manifest sidecar with an explicit status + timestamps.

    ``strict=True`` (the initial "running" write and the terminal write) re-raises
    an ``OSError`` so a manifest that cannot be persisted fails the run instead of
    reporting silent success. The non-strict best-effort failure re-stamp only
    logs, so it never masks the real underlying failure.
    """
    manifest = {
        "status": status,
        "app_git_commit": _git_commit(),
        "evidence_contract_id": RESOLVER_COMPLETE_V2,
        "ended_at": datetime.now(timezone.utc).isoformat(),
    }
    if started_at is not None:
        manifest["started_at"] = datetime.fromtimestamp(
            started_at, timezone.utc
        ).isoformat()
    manifest.update(extra)
    try:
        path.write_text(json.dumps(manifest, indent=2))
        log.info("Wrote run manifest to %s (status=%s)", path, status)
    except OSError as exc:
        log.error("Failed to write run manifest %s: %s", path, exc)
        if strict:
            raise


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


if __name__ == "__main__":
    main()
