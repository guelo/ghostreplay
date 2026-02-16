#!/usr/bin/env python3
"""
ELO Benchmark: measure Maia-2 move quality at various rating levels.

Plays complete games (Maia via OpponentMoveController vs. strength-limited
Stockfish), then analyzes Maia's moves with full-strength Stockfish to
compute ACPL (Average CentiPawn Loss).

Usage:
    python scripts/elo_benchmark.py
    python scripts/elo_benchmark.py --games 20 --elos 800,1200,1800
    python scripts/elo_benchmark.py --games 1 --elos 1200 -v
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup: allow importing backend.app modules
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# Force VMU pipeline on so choose_move() uses the calibrated path
os.environ.setdefault("VMU_ENABLED", "true")

import chess  # noqa: E402
from stockfish import Stockfish  # noqa: E402

from app.maia_engine import MaiaEngineService  # noqa: E402
from app.opponent_move_controller import choose_move  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_PLIES = 400  # 200 full moves hard cap
STOCKFISH_MIN_UCI_ELO = 1320

# Approximate Skill Level mapping for ELOs below Stockfish's UCI_Elo minimum.
_SKILL_LEVEL_MAP = {
    600: 0,
    700: 1,
    800: 2,
    900: 3,
    1000: 5,
    1100: 7,
    1200: 8,
    1300: 10,
}

# Expected ACPL by ELO from Lichess aggregate data (for comparison).
EXPECTED_ACPL = {
    600: 180,
    800: 130,
    1000: 95,
    1200: 70,
    1500: 45,
    1800: 30,
}

DEFAULT_ELOS = [600, 800, 1000, 1200, 1500, 1800]

log = logging.getLogger("benchmark")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class MoveRecord:
    """Record of a single Maia move for post-game analysis."""
    ply: int
    fen_before: str
    uci: str
    san: str
    method: str


@dataclass
class GameResult:
    """Outcome of one complete game."""
    game_number: int
    maia_elo: int
    maia_color: chess.Color
    outcome: str  # "win", "draw", "loss" from Maia's perspective
    termination: str
    total_plies: int
    maia_moves: list[MoveRecord] = field(default_factory=list)
    maia_acpl: float | None = None


@dataclass
class ELOBenchmarkResult:
    """Aggregated stats for one ELO bracket."""
    elo: int
    games: list[GameResult]

    @property
    def wins(self) -> int:
        return sum(1 for g in self.games if g.outcome == "win")

    @property
    def draws(self) -> int:
        return sum(1 for g in self.games if g.outcome == "draw")

    @property
    def losses(self) -> int:
        return sum(1 for g in self.games if g.outcome == "loss")

    @property
    def mean_acpl(self) -> float | None:
        acpls = [g.maia_acpl for g in self.games if g.maia_acpl is not None]
        return sum(acpls) / len(acpls) if acpls else None

    @property
    def score_pct(self) -> float:
        total = len(self.games)
        if total == 0:
            return 0.0
        return (self.wins + 0.5 * self.draws) / total * 100


# ---------------------------------------------------------------------------
# Stockfish factory helpers
# ---------------------------------------------------------------------------
def create_opponent_stockfish(
    target_elo: int,
    sf_path: str,
    move_time_ms: int = 200,
) -> Stockfish:
    """Create a strength-limited Stockfish for the opponent role."""
    sf = Stockfish(path=sf_path, depth=10, parameters={"Hash": 32, "Threads": 1})

    if target_elo >= STOCKFISH_MIN_UCI_ELO:
        sf.set_elo_rating(target_elo)
    else:
        closest_elo = min(_SKILL_LEVEL_MAP.keys(), key=lambda e: abs(e - target_elo))
        skill = _SKILL_LEVEL_MAP[closest_elo]
        sf.set_skill_level(skill)

    return sf


def create_analyzer_stockfish(sf_path: str, depth: int = 18) -> Stockfish:
    """Create a full-strength Stockfish for post-game ACPL analysis."""
    return Stockfish(
        path=sf_path,
        depth=depth,
        parameters={"Hash": 128, "Threads": 1},
    )


# ---------------------------------------------------------------------------
# Game play
# ---------------------------------------------------------------------------
def play_one_game(
    maia_elo: int,
    maia_color: chess.Color,
    opponent_sf: Stockfish,
    opponent_move_time_ms: int,
    game_number: int,
) -> GameResult:
    """Play a complete game between Maia and Stockfish opponent."""
    board = chess.Board()
    maia_moves: list[MoveRecord] = []
    ply = 0

    while not board.is_game_over(claim_draw=True) and ply < MAX_PLIES:
        fen = board.fen()

        if board.turn == maia_color:
            # Maia's turn
            result = choose_move(fen, maia_elo)
            move = chess.Move.from_uci(result.uci)
            san = board.san(move)
            maia_moves.append(MoveRecord(
                ply=ply,
                fen_before=fen,
                uci=result.uci,
                san=san,
                method=result.method,
            ))
            board.push(move)
            log.debug("  Maia (%d): %s [%s]", maia_elo, san, result.method)
        else:
            # Stockfish opponent's turn
            opponent_sf.set_fen_position(fen)
            best = opponent_sf.get_best_move_time(opponent_move_time_ms)
            if best is None:
                break
            move = chess.Move.from_uci(best)
            san = board.san(move)
            board.push(move)
            log.debug("  SF opponent: %s", san)

        ply += 1

    outcome, termination = _classify_outcome(board, maia_color, ply)
    color_str = "White" if maia_color == chess.WHITE else "Black"
    log.info(
        "  Game %d @ ELO %d (Maia=%s): %s (%s, %d plies)",
        game_number, maia_elo, color_str, outcome, termination, ply,
    )
    return GameResult(
        game_number=game_number,
        maia_elo=maia_elo,
        maia_color=maia_color,
        outcome=outcome,
        termination=termination,
        total_plies=ply,
        maia_moves=maia_moves,
    )


def _classify_outcome(
    board: chess.Board,
    maia_color: chess.Color,
    ply: int,
) -> tuple[str, str]:
    """Return (outcome, termination) from Maia's perspective."""
    if board.is_checkmate():
        winner = not board.turn
        outcome = "win" if winner == maia_color else "loss"
        return outcome, "checkmate"
    if board.is_stalemate():
        return "draw", "stalemate"
    if board.is_insufficient_material():
        return "draw", "insufficient"
    if board.is_seventyfive_moves():
        return "draw", "75-move"
    if board.is_fivefold_repetition():
        return "draw", "repetition"
    if board.can_claim_draw():
        return "draw", "draw-claim"
    if ply >= MAX_PLIES:
        return "draw", "max-moves"
    return "draw", "unknown"


# ---------------------------------------------------------------------------
# ACPL analysis
# ---------------------------------------------------------------------------
def _raw_to_cp(ev: dict) -> int:
    """Convert Stockfish eval dict to centipawns from side-to-move's POV.

    Stockfish get_evaluation() returns {"type": "cp"|"mate", "value": int}
    where positive = good for the side to move (UCI convention).
    """
    if ev["type"] == "mate":
        return 10_000 if ev["value"] > 0 else -10_000
    return ev["value"]


def analyze_game_acpl(game: GameResult, analyzer: Stockfish) -> float:
    """Compute ACPL for Maia's moves using full-strength Stockfish.

    For each Maia move:
      - eval_before: position with Maia to move → cp from Maia's POV
      - eval_after: position with opponent to move → negate for Maia's POV
      - loss = max(0, before - after)
    """
    if not game.maia_moves:
        game.maia_acpl = 0.0
        return 0.0

    total_loss = 0
    count = 0

    for rec in game.maia_moves:
        board = chess.Board(rec.fen_before)

        # Before: Maia to move, so positive = good for Maia
        analyzer.set_fen_position(rec.fen_before)
        cp_before = _raw_to_cp(analyzer.get_evaluation())

        board.push(chess.Move.from_uci(rec.uci))
        if board.is_game_over():
            if board.is_checkmate():
                # Maia delivered checkmate -- no loss
                count += 1
                continue
            else:
                cp_after_maia = 0  # draw
        else:
            # After: opponent to move, so positive = good for opponent
            # Negate to get Maia's perspective
            analyzer.set_fen_position(board.fen())
            cp_after_maia = -_raw_to_cp(analyzer.get_evaluation())

        loss = max(0, cp_before - cp_after_maia)
        total_loss += loss
        count += 1

    acpl = total_loss / count if count > 0 else 0.0
    game.maia_acpl = acpl
    return acpl


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_benchmark(
    elos: list[int],
    games_per_elo: int,
    sf_path: str,
    analyzer_depth: int,
    opponent_move_time_ms: int,
) -> list[ELOBenchmarkResult]:
    """Main benchmark loop."""
    log.info("Warming up Maia-2 engine...")
    t0 = time.time()
    MaiaEngineService.warmup()
    log.info("Maia-2 ready in %.1fs", time.time() - t0)

    log.info("Initializing analyzer Stockfish (depth=%d)...", analyzer_depth)
    analyzer = create_analyzer_stockfish(sf_path, depth=analyzer_depth)

    results: list[ELOBenchmarkResult] = []

    for elo in elos:
        log.info("")
        log.info("=" * 60)
        log.info("ELO %d: playing %d games", elo, games_per_elo)
        log.info("=" * 60)

        opponent = create_opponent_stockfish(elo, sf_path, opponent_move_time_ms)
        games: list[GameResult] = []

        for i in range(games_per_elo):
            maia_color = chess.WHITE if i % 2 == 0 else chess.BLACK

            game = play_one_game(
                maia_elo=elo,
                maia_color=maia_color,
                opponent_sf=opponent,
                opponent_move_time_ms=opponent_move_time_ms,
                game_number=i + 1,
            )

            acpl = analyze_game_acpl(game, analyzer)
            log.info("    ACPL: %.1f", acpl)
            games.append(game)

        results.append(ELOBenchmarkResult(elo=elo, games=games))
        del opponent

    return results


def print_summary(results: list[ELOBenchmarkResult]) -> None:
    """Print formatted summary table."""
    print()
    print("=" * 78)
    print("  Maia-2 ELO Benchmark Results")
    print("=" * 78)
    print()
    hdr = (
        f"  {'ELO':>5}  {'Games':>5}  {'W':>3}  {'D':>3}  {'L':>3}  "
        f"{'Score%':>6}  {'ACPL':>6}  {'Expected':>8}  {'Delta':>6}"
    )
    sep = (
        f"  {'-----':>5}  {'-----':>5}  {'---':>3}  {'---':>3}  {'---':>3}  "
        f"{'------':>6}  {'------':>6}  {'--------':>8}  {'------':>6}"
    )
    print(hdr)
    print(sep)

    for r in results:
        acpl = r.mean_acpl
        expected = EXPECTED_ACPL.get(r.elo)
        acpl_str = f"{acpl:.1f}" if acpl is not None else "N/A"
        expected_str = str(expected) if expected is not None else "N/A"
        delta_str = ""
        if acpl is not None and expected is not None:
            delta_str = f"{acpl - expected:+.1f}"

        print(
            f"  {r.elo:>5}  {len(r.games):>5}  {r.wins:>3}  {r.draws:>3}  "
            f"{r.losses:>3}  {r.score_pct:>5.1f}%  {acpl_str:>6}  "
            f"{expected_str:>8}  {delta_str:>6}"
        )

    print()
    print("  ACPL = Average CentiPawn Loss (lower = stronger)")
    print("  Expected values from Lichess aggregate data")
    print("  Delta = measured - expected (negative = better than expected)")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark Maia-2 playing strength at various ELO levels",
    )
    parser.add_argument(
        "--games", "-n", type=int, default=10,
        help="Number of games per ELO bracket (default: 10)",
    )
    parser.add_argument(
        "--elos", type=str,
        default=",".join(str(e) for e in DEFAULT_ELOS),
        help="Comma-separated ELO levels to test (default: %(default)s)",
    )
    parser.add_argument(
        "--stockfish-path", type=str,
        default=os.environ.get("STOCKFISH_PATH", "/usr/local/bin/stockfish"),
        help="Path to Stockfish binary",
    )
    parser.add_argument(
        "--analyzer-depth", type=int, default=18,
        help="Stockfish analysis depth for ACPL (default: 18)",
    )
    parser.add_argument(
        "--opponent-move-time", type=int, default=200,
        help="Opponent Stockfish move time in ms (default: 200)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging (show every move)",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for reproducibility",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    elos = [int(e.strip()) for e in args.elos.split(",")]

    if args.seed is not None:
        import random
        random.seed(args.seed)

    results = run_benchmark(
        elos=elos,
        games_per_elo=args.games,
        sf_path=args.stockfish_path,
        analyzer_depth=args.analyzer_depth,
        opponent_move_time_ms=args.opponent_move_time,
    )

    print_summary(results)


if __name__ == "__main__":
    main()
