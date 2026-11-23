#!/usr/bin/env python3
"""Maia3 strength probe: how strongly the local engine plays, and at which temperature.

g-maia3-strength-probe (chunk 3 of g-maia3-local-engine). The probe drives the
production engine service (``app.maia3_client.Maia3EngineService``, an
unmodified Maia3 child over UCI) with overrides for model, temperature and
threads, scores every move with local Stockfish, and applies the decision rule
for MAIA3_TEMPERATURE that the user approved on 2026-11-15, before any run. The
rule must not change after the run starts.

Usage (from backend/, in a venv with requirements-maia3.txt and the weights):

    # 1. Build the position fixture once, from the public Lichess dump (CC0).
    #    Read a local prefix of the dump (about 3.6M games): the build stops
    #    reading while Stockfish catches up, and a paused download can be
    #    dropped by the server (it was, on 2026-11-17, mid-build). Evals are
    #    cached as they arrive, so a rerun repeats none of them.
    curl -sL -r 0-1199999999 <SOURCE_URL> -o prefix.pgn.zst
    zstd -dc prefix.pgn.zst | python scripts/probe_maia3.py --build-pool -

    # 2. Measure throughput on 20 positions per stage; print the projected wall clock:
    python scripts/probe_maia3.py --pilot

    # 3. The full run, in the background. Results are cached, so it resumes.
    #    On a laptop, keep it awake and on AC power (macOS: caffeinate -i):
    python scripts/probe_maia3.py --run
    #    Then copy backend/.maia3_probe/ somewhere durable: T=1 samples come
    #    from a fresh random seed per engine start, so the cache is their only copy.

    # 4. The report: CPL tables, both headlines, the decision rule's outcome:
    python scripts/probe_maia3.py --report

    # Cold start, per-move latency and engine child memory (no Stockfish; this is
    # what runs under docker and in production):
    python scripts/probe_maia3.py --latency

Positions (``scripts/fixtures/maia3_probe_positions.json``). Each entry holds
the UCI moves from the start position, its pool and, for Lichess positions, the
band and the move the human played. It stores no usernames, game IDs, dates or
clock values, so results are safe to record in beads and docs.

* natural: 1,000 per band (600, 1100, 1500, 2000, 2600) from rated standard
  blitz games (base + 40 x increment in 180-479 s) between two non-BOT players
  both within 100 of the band, one position per game at a ply drawn uniformly
  from 16-60, skipped when the mover had under 30 s, replaced when Stockfish
  puts it beyond +-1000 cp. Each band takes its first 1,000 passing games in
  stream order, so time of day is not controlled: a common band spans minutes
  of the dump and a rare one hours (the header records each band's window).
  Each band is stored in a seeded order; the first 100 of each band are the
  headline-1 positions.
* tactical: 10 hand-picked positions plus 20 only-move positions (Stockfish
  MultiPV 2: the best move at least 150 cp ahead of the second) from further
  qualifying games, never from the natural pool.
* opening: 40 positions sampled with a fixed seed from the repo opening book at
  plies 8-20.

Runs. Headline-1, tactical and opening positions (570): T=0 at Elo 600-2600 in
steps of 100, and T=1 (TopP=1) with 20 samples at each band Elo. The other
natural positions (4,500): T=0 and T=1 (5 samples) at their band's Elo only.
Headline 2 uses the first 5 samples of every natural position.

CPL. Stockfish at depth 18, one thread per worker. Every eval is of the position
after a move, from the mover's point of view, clamped to +-1000 cp (mates count
as 1000), and loss = eval(after Stockfish's best move) - eval(after the move),
clamped to [0, 1000]. Both sides of the difference are searched the same way.

The cache (``backend/.maia3_probe/cache.sqlite3``, untracked) keys engine moves
by (model, position, setting, Elo, sample index) and evals by (Stockfish
version, depth, position, move).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import chess  # noqa: E402
import chess.engine  # noqa: E402

from app import maia3_pins  # noqa: E402
from app.maia3_client import (  # noqa: E402
    ELO_BINS,
    Maia3EngineService,
    Maia3Error,
    Maia3Settings,
    maia3_settings,
    open_uci,
    position_for,
)

logger = logging.getLogger("probe_maia3")

SOURCE_URL = (
    "https://database.lichess.org/standard/lichess_db_standard_rated_2025-10.pgn.zst"
)
FIXTURE_PATH = BACKEND / "scripts" / "fixtures" / "maia3_probe_positions.json"
PROBE_DIR = BACKEND / ".maia3_probe"
CACHE_PATH = PROBE_DIR / "cache.sqlite3"
ECO_PATH = BACKEND / "public" / "data" / "openings" / "eco.json"
FIXTURE_SCHEMA = 1
DEFAULT_SEED = 20261115

# Pools and runs.
BANDS = (600, 1100, 1500, 2000, 2600)
BAND_HALF_WIDTH = 100
GRID = tuple(range(600, 2601, 100))
NATURAL_PER_BAND = 1000
H1_PER_BAND = 100
K_H1 = 20
K_H2 = 5
TEMPERATURES = {"T0": 0.0, "T1": 1.0}

# Natural-position filters.
PLY_RANGE = (16, 60)
MIN_CLOCK_S = 30.0
BLITZ_SECONDS = (180, 479)
DECIDED_CP = 1000

# Tactical and opening pools.
ONLY_MOVE_GAP_CP = 150
TACTICAL_PER_BAND = 4
TACTICAL_AUTO = TACTICAL_PER_BAND * len(BANDS)
OPENING_COUNT = 40
OPENING_PLIES = (8, 20)

# The selector keeps up to this many candidates per band, in stream order, so
# rejections can be replaced from the same prefix of the stream. A band's later
# qualifying games are its only-move candidates. 1600 leaves room for the 600
# band's decided-position rejections (about 23% on 2026-11-17).
CANDIDATE_CAP_PER_BAND = 1600
TACTICAL_CANDIDATE_CAP = 400
# Reading stops once every unfinished pool holds this many spare candidates
# (half its target) or is at its cap, so a stalled or closed download after
# that point cannot matter.
CANDIDATE_RESERVE = 0.5

# CPL.
SF_DEPTH = 18
SF_HASH_MB = 64
MATE_CP = 100_000
LOSS_CAP_CP = 1000
BLUNDER_CP = 200

# Statistics and the decision rule (approved 2026-11-15; fixed before the run).
BOOTSTRAP_RESAMPLES = 1000
BOOTSTRAP_SEED = 20261115
CRITERION2_FLOOR_ELO = -150.0
FOLDED_NORMAL = math.sqrt(2 / math.pi)

# Pilot and latency.
PILOT_PER_BAND = 4
LATENCY_MOVES = 200
MEMORY_SAMPLE_S = 0.1

# Hand-picked tactical positions: opening traps and two famous combinations.
# Each is checked with Stockfish (depth 18, MultiPV 2, clamped scores): the
# named move is at least ONLY_MOVE_GAP_CP ahead of the second-best move.
HAND_TACTICS: tuple[tuple[str, str], ...] = (
    (
        "Opera Game, Morphy 1858: 16.Qb8+ mates",
        "e2e4 e7e5 g1f3 d7d6 d2d4 c8g4 d4e5 g4f3 d1f3 d6e5 f1c4 g8f6 f3b3 d8e7 "
        "b1c3 c7c6 c1g5 b7b5 c3b5 c6b5 c4b5 b8d7 e1c1 a8d8 d1d7 d8d7 h1d1 e7e6 "
        "b5d7 f6d7",
    ),
    ("Legal's mate: 6.Bxf7+", "e2e4 e7e5 g1f3 d7d6 f1c4 c8g4 b1c3 g7g6 f3e5 g4d1"),
    (
        "Siberian trap: 9...Nd4",
        "e2e4 c7c5 d2d4 c5d4 c2c3 d4c3 b1c3 b8c6 g1f3 e7e6 f1c4 d8c7 e1g1 g8f6 "
        "d1e2 f6g4 h2h3",
    ),
    (
        "Lasker trap: 7...fxg1=N+",
        "d2d4 d7d5 c2c4 e7e5 d4e5 d5d4 e2e3 f8b4 c1d2 d4e3 d2b4 e3f2 e1e2",
    ),
    ("Elephant trap: 6...Nxd5", "d2d4 d7d5 c2c4 e7e6 b1c3 g8f6 c1g5 b8d7 c4d5 e6d5 c3d5"),
    (
        "Noah's Ark trap: 8...c5",
        "e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 d7d6 d2d4 b7b5 a4b3 c6d4 f3d4 e5d4 d1d4",
    ),
    ("Englund Gambit trap: 6...Bb4", "d2d4 e7e5 d4e5 b8c6 g1f3 d8e7 c1f4 e7b4 f4d2 b4b2 d2c3"),
    (
        "Kieninger trap: 8...Nd3#",
        "d2d4 g8f6 c2c4 e7e5 d4e5 f6g4 c1f4 b8c6 g1f3 f8b4 b1d2 d8e7 a2a3 g4e5 a3b4",
    ),
    (
        "Reti-Tartakower, Vienna 1910: 9.Qd8+",
        "e2e4 c7c6 d2d4 d7d5 b1c3 d5e4 c3e4 g8f6 d1d3 e7e5 d4e5 d8a5 c1d2 a5e5 e1c1 f6e4",
    ),
    ("Scholar's mate: 4.Qxf7#", "e2e4 e7e5 f1c4 b8c6 d1h5 g8f6"),
)


# ---------------------------------------------------------------------------
# Positions and the fixture
# ---------------------------------------------------------------------------

_POSITION_KEYS = frozenset({"pool", "band", "moves", "human", "label"})
POOLS = ("natural", "tactical", "opening")


@dataclass(frozen=True)
class Position:
    """One probe position: UCI moves from the start, its pool and, for Lichess, band and human move."""

    pool: str
    moves: tuple[str, ...]
    band: int | None = None
    human: str | None = None
    label: str | None = None

    @property
    def key(self) -> str:
        return " ".join(self.moves)

    def board(self) -> chess.Board:
        board = chess.Board()
        for uci in self.moves:
            board.push_uci(uci)
        return board

    def to_json(self) -> dict:
        out: dict = {"pool": self.pool}
        if self.band is not None:
            out["band"] = self.band
        if self.label is not None:
            out["label"] = self.label
        out["moves"] = self.key
        if self.human is not None:
            out["human"] = self.human
        return out


class FixtureError(ValueError):
    """The probe fixture is missing, malformed or holds an illegal position."""


def position_from_json(entry: dict) -> Position:
    extra = set(entry) - _POSITION_KEYS
    if extra:
        raise FixtureError(f"unexpected keys {sorted(extra)} in {entry}")
    pool = entry.get("pool")
    if pool not in POOLS:
        raise FixtureError(f"unknown pool {pool!r}")
    moves = tuple(entry.get("moves", "").split())
    band = entry.get("band")
    if band is not None and band not in BANDS:
        raise FixtureError(f"unknown band {band!r}")
    if pool == "natural" and (band is None or entry.get("human") is None):
        raise FixtureError(f"natural position without band and human move: {entry}")
    return Position(pool, moves, band, entry.get("human"), entry.get("label"))


def check_position(position: Position) -> None:
    """Raise FixtureError unless every move (and the human move) is legal and the game goes on."""
    board = chess.Board()
    try:
        for uci in position.moves:
            board.push_uci(uci)
        if position.human is not None:
            board.copy(stack=False).push_uci(position.human)
    except ValueError as exc:
        raise FixtureError(f"illegal move in {position.to_json()}: {exc}") from None
    if board.is_game_over():
        raise FixtureError(f"position is over: {position.to_json()}")


def load_fixture(path: Path = FIXTURE_PATH, check: bool = True) -> tuple[dict, list[Position]]:
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise FixtureError(
            f"no probe fixture at {path}; build it with --build-pool (see the docstring)"
        ) from None
    if data.get("header", {}).get("schema") != FIXTURE_SCHEMA:
        raise FixtureError(f"fixture schema is not {FIXTURE_SCHEMA}")
    positions = [position_from_json(entry) for entry in data["positions"]]
    if check:
        for position in positions:
            check_position(position)
    return data["header"], positions


def write_fixture(path: Path, header: dict, positions: Sequence[Position]) -> None:
    """One position per line, so a review diff stays readable."""
    body = json.dumps(header, indent=2).replace("\n", "\n  ")
    lines = ",\n".join(
        "    " + json.dumps(p.to_json(), ensure_ascii=False) for p in positions
    )
    text = '{\n  "header": ' + body + ',\n  "positions": [\n' + lines + "\n  ]\n}\n"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def fixture_digest(path: Path = FIXTURE_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def natural_by_band(positions: Iterable[Position]) -> dict[int, list[Position]]:
    """Natural positions per band, in the fixture's seeded order."""
    out: dict[int, list[Position]] = {band: [] for band in BANDS}
    for position in positions:
        if position.pool == "natural":
            out[position.band].append(position)
    return out


def headline1_natural(positions: Iterable[Position]) -> list[Position]:
    """The first H1_PER_BAND natural positions of each band (500)."""
    by_band = natural_by_band(positions)
    return [p for band in BANDS for p in by_band[band][:H1_PER_BAND]]


def headline1_set(positions: Sequence[Position]) -> list[Position]:
    """The positions on the full grid: headline-1 natural, tactical and opening (570)."""
    return headline1_natural(positions) + [p for p in positions if p.pool != "natural"]


# ---------------------------------------------------------------------------
# Lichess PGN stream
# ---------------------------------------------------------------------------

_NEEDED_HEADERS = frozenset(
    {
        b"Event", b"Site", b"UTCDate", b"UTCTime", b"WhiteElo", b"BlackElo",
        b"WhiteTitle", b"BlackTitle", b"TimeControl", b"Termination", b"FEN",
        b"SetUp", b"Variant",
    }
)


@dataclass
class PgnGame:
    headers: dict[str, str]
    movetext: str


def iter_games(stream: BinaryIO) -> Iterator[PgnGame]:
    """Games from a PGN byte stream. Keeps only the headers the filters read."""
    headers: dict[str, str] = {}
    movetext: list[bytes] = []
    for raw in stream:
        if raw.startswith(b"[Event "):
            if headers or movetext:
                yield PgnGame(headers, b" ".join(movetext).decode("utf-8", "replace"))
                headers, movetext = {}, []
        if raw.startswith(b"["):
            key, _, rest = raw[1:].partition(b" ")
            if key in _NEEDED_HEADERS:
                value = rest.rstrip()
                if value.endswith(b"]"):
                    value = value[:-1]
                headers[key.decode()] = value.strip(b'"').decode("utf-8", "replace")
        else:
            line = raw.strip()
            if line:
                movetext.append(line)
    if headers or movetext:
        yield PgnGame(headers, b" ".join(movetext).decode("utf-8", "replace"))


def blitz_seconds(time_control: str) -> int | None:
    """Lichess's estimated duration, base + 40 x increment, or None (e.g. correspondence)."""
    base, sep, increment = time_control.partition("+")
    if not sep:
        return None
    try:
        return int(base) + 40 * int(increment)
    except ValueError:
        return None


def band_of(elo: int) -> int | None:
    for band in BANDS:
        if abs(elo - band) <= BAND_HALF_WIDTH:
            return band
    return None


def qualify(headers: dict[str, str]) -> int | None:
    """The band of a qualifying game, or None.

    Rated, standard chess from the start position, blitz by Lichess's own rule,
    no BOT on either side, not ended for a rules infraction, and both players
    within BAND_HALF_WIDTH of the same band.
    """
    if not headers.get("Event", "").startswith("Rated "):
        return None
    if "FEN" in headers or headers.get("SetUp") == "1":
        return None
    if headers.get("Variant", "Standard") != "Standard":
        return None
    seconds = blitz_seconds(headers.get("TimeControl", ""))
    if seconds is None or not BLITZ_SECONDS[0] <= seconds <= BLITZ_SECONDS[1]:
        return None
    if "BOT" in (headers.get("WhiteTitle"), headers.get("BlackTitle")):
        return None
    if headers.get("Termination") == "Rules infraction":
        return None
    try:
        white, black = int(headers["WhiteElo"]), int(headers["BlackElo"])
    except (KeyError, ValueError):
        return None
    band = band_of(white)
    if band is None or band_of(black) != band:
        return None
    return band


_TOKEN = re.compile(r"\{([^}]*)\}|(\S+)")
_CLOCK = re.compile(r"\[%clk (\d+):(\d+):(\d+(?:\.\d+)?)\]")
_MOVE_NUMBER = re.compile(r"^\d+\.+")
_RESULTS = frozenset({"1-0", "0-1", "1/2-1/2", "*"})


def parse_movetext(text: str) -> tuple[list[str], list[float | None]]:
    """SAN moves, and each move's [%clk] (seconds left after it) or None."""
    sans: list[str] = []
    clocks: list[float | None] = []
    for match in _TOKEN.finditer(text):
        comment, token = match.groups()
        if comment is not None:
            clock = _CLOCK.search(comment)
            if clock and sans:
                hours, minutes, seconds = clock.groups()
                clocks[-1] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            continue
        if token in _RESULTS or token.startswith("$"):
            continue
        token = _MOVE_NUMBER.sub("", token).rstrip("!?")
        if token:
            sans.append(token)
            clocks.append(None)
    return sans, clocks


@dataclass(frozen=True)
class Candidate:
    """A position drawn from a qualifying game; game_index and utc stay out of the fixture."""

    band: int
    moves: tuple[str, ...]
    human: str
    game_index: int
    utc: str

    def position(self, pool: str) -> Position:
        return Position(pool, self.moves, self.band, self.human)


def game_utc(headers: dict[str, str]) -> str:
    return f"{headers.get('UTCDate', '?')} {headers.get('UTCTime', '?')}"


def candidate_from_game(
    game: PgnGame, band: int, seed: int, game_index: int
) -> Candidate | None:
    """One position from a qualifying game, or None.

    The position is the one after ``p`` half-moves, with ``p`` drawn uniformly
    from PLY_RANGE among the plies the game reached before its last move (the
    human must have moved from it). The draw is seeded by the game's Site, so it
    does not depend on how the stream is processed. The game is skipped when the
    mover had under MIN_CLOCK_S left (the [%clk] after their previous move) or
    when that clock is missing.
    """
    sans, clocks = parse_movetext(game.movetext)
    low, high = PLY_RANGE
    last = min(high, len(sans) - 1)
    if last < low:
        return None
    rng = random.Random(f"{seed}:{game.headers.get('Site') or game_index}")
    ply = rng.randint(low, last)
    clock = clocks[ply - 2]
    if clock is None or clock < MIN_CLOCK_S:
        return None
    board = chess.Board()
    ucis: list[str] = []
    try:
        for san in sans[: ply + 1]:
            move = board.parse_san(san)
            ucis.append(move.uci())
            board.push(move)
    except ValueError:
        return None
    return Candidate(band, tuple(ucis[:-1]), ucis[-1], game_index, game_utc(game.headers))


class OrderedSelector:
    """The first ``target`` passing candidates in stream order.

    Evaluations finish in any order. Submitted candidates always form a prefix
    of the stored ones, and a candidate is submitted only while accepted +
    pending < target, so when the selector is done the prefix holds exactly
    ``target`` passes and every candidate in it has been judged.
    """

    def __init__(self, target: int, cap: int) -> None:
        self.target = target
        self.cap = cap
        self.candidates: list[Candidate] = []
        self.verdicts: dict[int, bool] = {}
        self.submitted = 0
        self.pending = 0
        self.accepted = 0

    @property
    def at_cap(self) -> bool:
        return len(self.candidates) >= self.cap

    @property
    def done(self) -> bool:
        return self.accepted >= self.target

    def needs_stream(self) -> bool:
        """More of the stream could still matter: not done, not at cap, and short of a reserve."""
        if self.done or self.at_cap:
            return False
        unsubmitted = len(self.candidates) - self.submitted
        reserve = math.ceil(CANDIDATE_RESERVE * self.target)
        return self.accepted + self.pending + unsubmitted < self.target + reserve

    def add(self, candidate: Candidate) -> None:
        self.candidates.append(candidate)

    def to_submit(self) -> list[int]:
        out = []
        while (
            self.accepted + self.pending < self.target
            and self.submitted < len(self.candidates)
        ):
            out.append(self.submitted)
            self.submitted += 1
            self.pending += 1
        return out

    def record(self, index: int, passed: bool) -> None:
        self.pending -= 1
        self.verdicts[index] = passed
        self.accepted += int(passed)

    def selected(self) -> list[Candidate]:
        return [self.candidates[i] for i in sorted(self.verdicts) if self.verdicts[i]]


class PoolShort(RuntimeError):
    """The stream ended before every band (or the tactical pool) filled."""


Judge = Callable[[Candidate], tuple[bool, list[tuple]]]


@dataclass
class PoolBuild:
    natural: dict[int, list[Candidate]]
    tactical: list[Candidate]
    games_read: int
    games_scanned: int
    utc_first: str
    utc_last: str
    eval_rows: list[tuple] = field(default_factory=list)
    natural_windows: dict[int, dict] = field(default_factory=dict)
    only_move_windows: dict[int, dict] = field(default_factory=dict)


def selection_window(selector: OrderedSelector) -> dict:
    """Where in the stream a pool's positions come from, and how many candidates it judged."""
    chosen = selector.selected()
    judged = len(selector.verdicts)
    return {
        "games_scanned": max(c.game_index for c in chosen) + 1,
        "utc_range": [min(c.utc for c in chosen), max(c.utc for c in chosen)],
        "judged": judged,
        "rejected": judged - len(chosen),
    }


def build_pool(
    games: Iterable[PgnGame],
    judge_natural: Judge,
    judge_tactical: Judge,
    executor: concurrent.futures.Executor,
    seed: int,
    per_band: int = NATURAL_PER_BAND,
    tactical_per_band: int = TACTICAL_PER_BAND,
    band_cap: int = CANDIDATE_CAP_PER_BAND,
    tactical_cap: int = TACTICAL_CANDIDATE_CAP,
    max_games: int | None = None,
    progress: Callable[[str], None] | None = None,
    on_rows: Callable[[list[tuple]], None] | None = None,
) -> PoolBuild:
    """Select the natural and auto-tactical positions from a game stream.

    A qualifying game goes to its band's candidates until the band holds
    ``band_cap`` of them; after that it goes to that band's tactical
    candidates, so no game feeds both pools and every band gives
    ``tactical_per_band`` only-move positions. Routing depends only on counts, never on when an
    evaluation finishes, so the selection is a function of the stream. The
    stream is read on while Stockfish judges candidates in the background, so an
    upstream ``curl`` never stalls for long. ``on_rows`` receives the judges'
    cache rows as they arrive (on this thread), so an interrupted build keeps them.
    """
    natural = {band: OrderedSelector(per_band, band_cap) for band in BANDS}
    tactical = {band: OrderedSelector(tactical_per_band, tactical_cap) for band in BANDS}
    selectors: dict[str, OrderedSelector] = {str(b): s for b, s in natural.items()}
    selectors.update({f"tactical-{b}": s for b, s in tactical.items()})
    judges = {
        name: judge_tactical if name.startswith("tactical") else judge_natural
        for name in selectors
    }
    done_q: queue.Queue = queue.Queue()
    eval_rows: list[tuple] = []
    pending = 0

    def pump() -> None:
        nonlocal pending
        for name, selector in selectors.items():
            for index in selector.to_submit():
                future = executor.submit(judges[name], selector.candidates[index])
                future.add_done_callback(
                    lambda f, name=name, index=index: done_q.put((name, index, f))
                )
                pending += 1

    def drain(block: bool) -> None:
        nonlocal pending
        while True:
            try:
                name, index, future = done_q.get(block=block, timeout=1.0 if block else None)
            except queue.Empty:
                return
            pending -= 1
            passed, rows = future.result()  # a judge that raised aborts the build
            selectors[name].record(index, passed)
            eval_rows.extend(rows)
            if on_rows is not None and rows:
                on_rows(rows)
            block = False

    def all_done() -> bool:
        return all(s.done for s in selectors.values())

    games_read = 0
    last_report = time.monotonic()
    stream = iter(games)
    stream_open = True
    while not all_done():
        if progress and time.monotonic() - last_report > 30:
            last_report = time.monotonic()
            progress(
                f"games={games_read} "
                + " ".join(
                    f"{name}={s.accepted}/{s.target}(+{s.pending}, {len(s.candidates)} cand)"
                    for name, s in selectors.items()
                )
            )
        exhausted = [
            name for name, s in selectors.items()
            if s.at_cap and not s.done and not s.pending and s.submitted == len(s.candidates)
        ]
        if exhausted:
            raise PoolShort(f"{exhausted} rejected too many of their candidates")
        wants_stream = stream_open and any(s.needs_stream() for s in selectors.values())
        if max_games is not None and games_read >= max_games:
            wants_stream = False
        if not wants_stream:
            if not pending:
                short = {n: f"{s.accepted}/{s.target}" for n, s in selectors.items() if not s.done}
                raise PoolShort(f"stream ended after {games_read} games with pools short: {short}")
            # Every pool has its reserve: wait for verdicts instead of reading on.
            drain(block=True)
            pump()
            continue
        try:
            game = next(stream)
        except StopIteration:
            stream_open = False
            continue
        game_index = games_read
        games_read += 1
        band = qualify(game.headers)
        if band is not None:
            selector = natural[band] if not natural[band].at_cap else tactical[band]
            if not selector.at_cap:
                candidate = candidate_from_game(game, band, seed, game_index)
                if candidate is not None:
                    selector.add(candidate)
        drain(block=False)
        pump()

    chosen_natural = {band: natural[band].selected() for band in BANDS}
    chosen_tactical = [c for band in BANDS for c in tactical[band].selected()]
    used = [c for cs in chosen_natural.values() for c in cs] + chosen_tactical
    # The dump is not strictly in start-time order, so the window is the min
    # and max over the games used, not the first and last game read.
    return PoolBuild(
        natural=chosen_natural,
        tactical=chosen_tactical,
        games_read=games_read,
        games_scanned=max(c.game_index for c in used) + 1,
        utc_first=min(c.utc for c in used),
        utc_last=max(c.utc for c in used),
        eval_rows=eval_rows,
        natural_windows={band: selection_window(natural[band]) for band in BANDS},
        only_move_windows={band: selection_window(tactical[band]) for band in BANDS},
    )


def opening_positions(eco_path: Path, seed: int, count: int = OPENING_COUNT) -> list[Position]:
    """``count`` distinct opening-book positions at OPENING_PLIES, sampled with ``seed``.

    Lines are deduplicated by the book's own EPD of their final position; the
    chosen ones are replayed and checked by the caller.
    """
    entries = json.loads(Path(eco_path).read_text())["entries"]
    by_epd: dict[str, tuple[tuple[str, ...], str]] = {}
    for entry in sorted(entries, key=lambda e: e["uci"]):
        moves = tuple(entry["uci"].split())
        if OPENING_PLIES[0] <= len(moves) <= OPENING_PLIES[1]:
            by_epd.setdefault(entry["epd"], (moves, f"{entry['eco']} {entry['name']}"))
    pool = sorted(by_epd.values())
    chosen = random.Random(f"{seed}:opening").sample(pool, count)
    return [Position("opening", moves, label=label) for moves, label in chosen]


def hand_tactical_positions() -> list[Position]:
    return [Position("tactical", tuple(moves.split()), label=label) for label, moves in HAND_TACTICS]


# ---------------------------------------------------------------------------
# Stockfish
# ---------------------------------------------------------------------------


def stockfish_path(explicit: str | None) -> str:
    path = explicit or os.environ.get("STOCKFISH_PATH") or shutil.which("stockfish")
    if not path:
        raise SystemExit("Stockfish not found: set STOCKFISH_PATH or brew install stockfish")
    return path


def clamp_cp(score: int) -> int:
    return max(-LOSS_CAP_CP, min(LOSS_CAP_CP, score))


def mover_score(info: dict, mover: chess.Color) -> int:
    """The search score from the root mover's point of view; mates as +-(MATE_CP - n)."""
    return info["score"].pov(mover).score(mate_score=MATE_CP)


class StockfishWorkers:
    """A thread pool where each worker thread owns one single-threaded Stockfish."""

    def __init__(self, path: str, workers: int, depth: int = SF_DEPTH, hash_mb: int = SF_HASH_MB):
        self.path = path
        self.depth = depth
        self.hash_mb = hash_mb
        self._local = threading.local()
        self._engines: list[chess.engine.SimpleEngine] = []
        self._lock = threading.Lock()
        probe = chess.engine.SimpleEngine.popen_uci(path)
        try:
            self.name = probe.id.get("name", "unknown")
        finally:
            probe.quit()
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="stockfish", initializer=self._open
        )

    def _open(self) -> None:
        engine = chess.engine.SimpleEngine.popen_uci(self.path)
        engine.configure({"Threads": 1, "Hash": self.hash_mb})
        self._local.engine = engine
        with self._lock:
            self._engines.append(engine)

    @property
    def engine(self) -> chess.engine.SimpleEngine:
        return self._local.engine

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            engines, self._engines = self._engines, []
        for engine in engines:
            try:
                engine.quit()
            except Exception:  # noqa: BLE001 - best effort on the way out
                engine.close()

    # Searches. ``key`` groups the searches of one position: python-chess sends
    # ucinewgame (a cleared hash) whenever it changes.

    def root(self, board: chess.Board, key: str) -> tuple[int, str]:
        info = self.engine.analyse(board, chess.engine.Limit(depth=self.depth), game=key)
        return mover_score(info, board.turn), info["pv"][0].uci()

    def top_two(self, board: chess.Board, key: str) -> list[int]:
        infos = self.engine.analyse(
            board, chess.engine.Limit(depth=self.depth), multipv=2, game=key
        )
        return [mover_score(info, board.turn) for info in infos]

    def after(self, board: chess.Board, uci: str, key: str) -> int:
        """The position after ``uci``, scored for the side that played it."""
        mover = board.turn
        child = board.copy()
        child.push_uci(uci)
        if child.is_checkmate():
            return MATE_CP
        if child.is_stalemate() or child.is_insufficient_material():
            return 0
        info = self.engine.analyse(child, chess.engine.Limit(depth=self.depth), game=key)
        return mover_score(info, mover)


def only_move_gap(scores: Sequence[int]) -> int | None:
    """Clamped best-minus-second score, or None with fewer than two moves."""
    if len(scores) < 2:
        return None
    return clamp_cp(scores[0]) - clamp_cp(scores[1])


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moves (
    model TEXT NOT NULL, pos TEXT NOT NULL, setting TEXT NOT NULL,
    elo INTEGER NOT NULL, sample INTEGER NOT NULL,
    move TEXT NOT NULL, ms REAL NOT NULL,
    PRIMARY KEY (model, pos, setting, elo, sample)
);
CREATE TABLE IF NOT EXISTS evals (
    engine TEXT NOT NULL, depth INTEGER NOT NULL, pos TEXT NOT NULL,
    move TEXT NOT NULL, score INTEGER NOT NULL, best TEXT,
    PRIMARY KEY (engine, depth, pos, move)
);
"""


class ProbeCache:
    """Engine moves and Stockfish evals. Used from the main thread only."""

    def __init__(self, path: Path = CACHE_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def moves(self, model: str) -> dict[tuple[str, str, int, int], tuple[str, float]]:
        rows = self.db.execute(
            "SELECT pos, setting, elo, sample, move, ms FROM moves WHERE model = ?", (model,)
        )
        return {(p, s, e, k): (m, ms) for p, s, e, k, m, ms in rows}

    def add_moves(self, model: str, rows: Iterable[tuple[str, str, int, int, str, float]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO moves VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(model, *row) for row in rows],
        )
        self.db.commit()

    def evals(self, engine: str, depth: int) -> dict[tuple[str, str], tuple[int, str | None]]:
        rows = self.db.execute(
            "SELECT pos, move, score, best FROM evals WHERE engine = ? AND depth = ?",
            (engine, depth),
        )
        return {(p, m): (s, b) for p, m, s, b in rows}

    def add_evals(self, rows: Iterable[tuple[str, int, str, str, int, str | None]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO evals VALUES (?, ?, ?, ?, ?, ?)", list(rows))
        self.db.commit()


# ---------------------------------------------------------------------------
# --build-pool
# ---------------------------------------------------------------------------


def run_build_pool(args: argparse.Namespace) -> int:
    path = stockfish_path(args.stockfish)
    stream = sys.stdin.buffer if args.build_pool == "-" else open(args.build_pool, "rb")
    workers = StockfishWorkers(path, args.sf_workers)
    cache = ProbeCache(args.cache)
    # Root evals from an earlier, interrupted build are reused (read-only here).
    known = cache.evals(workers.name, SF_DEPTH)
    pending_rows: list[tuple] = []
    last_flush = [time.monotonic()]
    logger.info(
        "building the pool with %s, depth %d, %d workers (%d evals cached)",
        workers.name, SF_DEPTH, args.sf_workers, len(known),
    )

    def judge_natural(candidate: Candidate) -> tuple[bool, list[tuple]]:
        key = " ".join(candidate.moves)
        cached = known.get((key, ""))
        if cached is not None:
            return abs(cached[0]) <= DECIDED_CP, []
        score, best = workers.root(candidate.position("natural").board(), key)
        row = (workers.name, SF_DEPTH, key, "", score, best)
        return abs(score) <= DECIDED_CP, [row]

    def keep_rows(rows: list[tuple]) -> None:
        pending_rows.extend(rows)
        if len(pending_rows) >= 200 or time.monotonic() - last_flush[0] > 30:
            cache.add_evals(pending_rows)
            pending_rows.clear()
            last_flush[0] = time.monotonic()

    def judge_tactical(candidate: Candidate) -> tuple[bool, list[tuple]]:
        board = candidate.position("tactical").board()
        gap = only_move_gap(workers.top_two(board, " ".join(candidate.moves)))
        return gap is not None and gap >= ONLY_MOVE_GAP_CP, []

    started = time.monotonic()
    try:
        build = build_pool(
            iter_games(stream),
            judge_natural,
            judge_tactical,
            workers.executor,
            seed=args.seed,
            max_games=args.max_games,
            progress=logger.info,
            on_rows=keep_rows,
        )
        hand = hand_tactical_positions()
        gaps = workers.executor.map(
            lambda p: only_move_gap(workers.top_two(p.board(), p.key)), hand
        )
        for position, gap in zip(hand, gaps):
            if gap is None or gap < ONLY_MOVE_GAP_CP:
                raise RuntimeError(f"hand-picked {position.label!r} is not an only move (gap {gap})")
    finally:
        workers.close()
        if stream is not sys.stdin.buffer:
            stream.close()
        cache.add_evals(pending_rows)
        cache.close()

    natural: list[Position] = []
    for band in BANDS:
        chosen = [c.position("natural") for c in build.natural[band]]
        random.Random(f"{args.seed}:order:{band}").shuffle(chosen)
        natural.extend(chosen)
    tactical = hand + [c.position("tactical") for c in build.tactical]
    opening = opening_positions(ECO_PATH, args.seed)
    positions = natural + tactical + opening
    for position in positions:
        check_position(position)

    header = {
        "schema": FIXTURE_SCHEMA,
        "description": (
            "Maia3 strength probe positions (g-maia3-strength-probe). Public Lichess "
            "games (CC0 database dump) and the repo opening book; no usernames, game "
            "IDs, dates or clock values are stored per position."
        ),
        "source_url": args.source_url,
        "games_scanned": build.games_scanned,
        "stream_utc_range": [build.utc_first, build.utc_last],
        "time_of_day": (
            "not controlled: each band takes its first passing games in stream "
            "order, so a common band spans minutes and a rare one hours "
            "(natural_by_band utc_range)"
        ),
        "natural_by_band": {
            str(band): {
                "games_scanned": w["games_scanned"],
                "utc_range": w["utc_range"],
                "candidates_judged": w["judged"],
                "rejected_decided": w["rejected"],
            }
            for band, w in build.natural_windows.items()
        },
        "seed": args.seed,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "stockfish": {"engine": workers.name, "depth": SF_DEPTH},
        "natural_filters": {
            "bands": list(BANDS),
            "band_half_width": BAND_HALF_WIDTH,
            "per_band": NATURAL_PER_BAND,
            "headline1_per_band": H1_PER_BAND,
            "rated_standard_from_start": True,
            "blitz_seconds_base_plus_40_increment": list(BLITZ_SECONDS),
            "exclude_titles": ["BOT"],
            "exclude_terminations": ["Rules infraction"],
            "ply_range": list(PLY_RANGE),
            "min_mover_clock_s": MIN_CLOCK_S,
            "max_abs_eval_cp": DECIDED_CP,
            "order": "seeded shuffle per band; the first headline1_per_band are headline 1",
        },
        "tactical": {
            "hand_picked": len(hand),
            "only_move": len(build.tactical),
            "only_move_per_band": TACTICAL_PER_BAND,
            "only_move_gap_cp": ONLY_MOVE_GAP_CP,
            "only_move_source": (
                f"each band's qualifying games after its first {CANDIDATE_CAP_PER_BAND} "
                "(the natural candidates), so never in the natural pool"
            ),
            "only_move_by_band": {
                str(band): {
                    "games_scanned": w["games_scanned"],
                    "utc_range": w["utc_range"],
                    "candidates_judged": w["judged"],
                }
                for band, w in build.only_move_windows.items()
            },
        },
        "opening": {"count": len(opening), "plies": list(OPENING_PLIES), "source": "public/data/openings/eco.json"},
        "counts": dict(Counter(p.pool for p in positions)),
    }
    write_fixture(args.fixture, header, positions)
    logger.info(
        "wrote %s: %d positions, %d games scanned (%d read) in %.0f s",
        args.fixture, len(positions), build.games_scanned, build.games_read,
        time.monotonic() - started,
    )
    return 0


# ---------------------------------------------------------------------------
# Engine runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineTask:
    pos: Position
    setting: str
    elo: int
    sample: int

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.pos.key, self.setting, self.elo, self.sample)


def plan_engine_tasks(positions: Sequence[Position], all_positions: Sequence[Position]) -> list[EngineTask]:
    """The engine moves for ``positions``; membership in headline 1 comes from ``all_positions``."""
    grid_keys = {p.key for p in headline1_set(all_positions)}
    tasks: dict[tuple, EngineTask] = {}

    def add(task: EngineTask) -> None:
        tasks.setdefault(task.key, task)

    for position in positions:
        if position.key in grid_keys:
            for elo in GRID:
                add(EngineTask(position, "T0", elo, 0))
            for elo in BANDS:
                for sample in range(K_H1):
                    add(EngineTask(position, "T1", elo, sample))
        if position.pool == "natural":
            add(EngineTask(position, "T0", position.band, 0))
            for sample in range(K_H2):
                add(EngineTask(position, "T1", position.band, sample))
    return list(tasks.values())


def run_settings(model: str, temperature: float, threads: int, weights_dir: Path) -> Maia3Settings:
    """Production service code, with the probe's overrides and roomy limits (CPU is shared)."""
    return Maia3Settings(
        enabled=True,
        model=model,
        weights_dir=weights_dir,
        temperature=temperature,
        top_p=1.0,
        threads=threads,
        move_timeout_s=60.0,
        min_play_budget_s=0.5,
        infer_timeout_s=120.0,
        start_timeout_s=180.0,
    )


def presented_board(position: Position) -> chess.Board:
    """The board a production request would send for this position (8 plies of history)."""
    board, _history = position_for(position.board().fen(), list(position.moves))
    return board


@dataclass
class EngineRunStats:
    done: int = 0
    wall_s: float = 0.0
    move_ms: list[float] = field(default_factory=list)


def run_engine_tasks(
    tasks: Sequence[EngineTask],
    cache: ProbeCache,
    model: str,
    weights_dir: Path,
    workers: int,
    threads: int = 1,
) -> EngineRunStats:
    cached = cache.moves(model)
    todo = sorted(
        (t for t in tasks if t.key not in cached),
        key=lambda t: (t.setting, t.pos.key, t.elo, t.sample),
    )
    stats = EngineRunStats()
    if not todo:
        logger.info("engine moves: all %d cached", len(tasks))
        return stats
    workers = max(1, min(workers, len(todo)))
    logger.info("engine moves: %d to play (%d cached), %d engines", len(todo), len(tasks) - len(todo), workers)
    unique = {t.pos.key: t.pos for t in todo}
    boards = {key: presented_board(pos) for key, pos in unique.items()}
    task_q: queue.Queue[EngineTask] = queue.Queue()
    for task in todo:  # all T0 first, so each worker switches setting at most once
        task_q.put(task)
    result_q: queue.Queue = queue.Queue()
    stop = threading.Event()
    marks: dict[str, float] = {}
    # Every engine is up before any move, so the rate excludes the first spawns.
    ready = threading.Barrier(workers, action=lambda: marks.setdefault("go", time.monotonic()))

    def worker() -> None:
        service: Maia3EngineService | None = None
        setting: str | None = None
        first = True
        try:
            while not stop.is_set():
                try:
                    task = task_q.get_nowait() if not first else task_q.get(timeout=1)
                except queue.Empty:
                    return
                if task.setting != setting:
                    if service is not None:
                        service.shutdown()
                    service = Maia3EngineService(
                        run_settings(model, TEMPERATURES[task.setting], threads, weights_dir)
                    )
                    if not service.wait_ready(240, stop_on_failure=True):
                        raise RuntimeError(f"engine did not start: {service.last_spawn_error}")
                    setting = task.setting
                if first:
                    first = False
                    try:
                        ready.wait(timeout=600)
                    except threading.BrokenBarrierError:
                        pass  # some worker found no task; carry on
                for attempt in range(3):
                    try:
                        started = time.perf_counter()
                        move = service.play(boards[task.pos.key].copy(), task.elo)
                        ms = (time.perf_counter() - started) * 1000
                        break
                    except Maia3Error:
                        if attempt == 2:
                            raise
                        logger.warning("engine move failed; waiting for a respawn", exc_info=True)
                        service.wait_ready(240)
                result_q.put(("move", task, move.uci(), ms))
        except BaseException as exc:  # noqa: BLE001 - reported to the main thread
            stop.set()
            ready.abort()
            result_q.put(("error", exc))
        finally:
            if service is not None:
                service.shutdown()
            result_q.put(("exit", None))

    threads_ = [threading.Thread(target=worker, name=f"maia3-probe-{i}", daemon=True) for i in range(workers)]
    for thread in threads_:
        thread.start()
    started = time.monotonic()
    first_result: float | None = None
    exited = 0
    batch: list[tuple] = []
    error: BaseException | None = None
    last_flush = last_report = time.monotonic()
    while exited < workers:
        kind, *rest = result_q.get()
        if kind == "exit":
            exited += 1
        elif kind == "error":
            error = error or rest[0]
        else:
            task, move, ms = rest
            if first_result is None:
                first_result = time.monotonic()
            batch.append((*task.key, move, ms))
            stats.done += 1
            stats.move_ms.append(ms)
        now = time.monotonic()
        if batch and (len(batch) >= 500 or now - last_flush > 10 or exited == workers):
            cache.add_moves(model, batch)
            batch = []
            last_flush = now
        if now - last_report > 60:
            last_report = now
            rate = stats.done / max(now - (first_result or now), 1e-9)
            logger.info(
                "engine moves %d/%d, %.1f/s, about %.0f min left",
                stats.done, len(todo), rate, (len(todo) - stats.done) / max(rate, 1e-9) / 60,
            )
    if batch:
        cache.add_moves(model, batch)
    stats.wall_s = time.monotonic() - marks.get("go", started)
    if error is not None:
        raise RuntimeError(f"engine run failed after {stats.done} moves: {error!r}") from error
    return stats


# ---------------------------------------------------------------------------
# CPL
# ---------------------------------------------------------------------------


@dataclass
class CplRunStats:
    positions: int = 0
    evals: int = 0
    wall_s: float = 0.0
    evals_by_position: dict[str, int] = field(default_factory=dict)


def moves_to_score(
    positions: Sequence[Position], engine_moves: dict[tuple[str, str, int, int], tuple[str, float]]
) -> dict[str, set[str]]:
    by_key: dict[str, set[str]] = defaultdict(set)
    wanted = {p.key for p in positions}
    for (pos, _setting, _elo, _sample), (move, _ms) in engine_moves.items():
        if pos in wanted:
            by_key[pos].add(move)
    for position in positions:
        if position.human is not None:
            by_key[position.key].add(position.human)
    return by_key


def run_cpl(
    positions: Sequence[Position],
    cache: ProbeCache,
    model: str,
    sf_path: str,
    workers: int,
) -> tuple[CplRunStats, str]:
    stats = CplRunStats()
    sf = StockfishWorkers(sf_path, workers)
    try:
        evals = cache.evals(sf.name, SF_DEPTH)
        needed = moves_to_score(positions, cache.moves(model))
        jobs = []
        for position in {p.key: p for p in positions}.values():
            moves = needed.get(position.key, set())
            root = evals.get((position.key, ""))
            have = {m for m in moves | ({root[1]} if root else set()) if (position.key, m) in evals}
            if root is not None and (moves | {root[1]}) <= have:
                continue
            jobs.append((position, moves, root, have))
        stats.positions = len(jobs)
        if not jobs:
            logger.info("cpl: every move already scored")
            return stats, sf.name
        logger.info("cpl: %d positions to score with %s depth %d, %d workers", len(jobs), sf.name, SF_DEPTH, workers)

        def job(position: Position, moves: set[str], root, have: set[str]) -> list[tuple]:
            board = position.board()
            rows = []
            if root is None:
                score, best = sf.root(board, position.key)
                rows.append((sf.name, SF_DEPTH, position.key, "", score, best))
            else:
                best = root[1]
            for uci in sorted((moves | {best}) - have):
                rows.append((sf.name, SF_DEPTH, position.key, uci, sf.after(board, uci, position.key), None))
            return rows

        started = time.monotonic()
        futures = {sf.executor.submit(job, *j): j[0] for j in jobs}
        batch: list[tuple] = []
        last_flush = last_report = time.monotonic()
        done = 0
        for future in concurrent.futures.as_completed(futures):
            rows = future.result()
            done += 1
            batch.extend(rows)
            stats.evals += len(rows)
            stats.evals_by_position[futures[future].key] = len(rows)
            now = time.monotonic()
            if len(batch) >= 500 or now - last_flush > 10:
                cache.add_evals(batch)
                batch = []
                last_flush = now
            if now - last_report > 60:
                last_report = now
                rate = stats.evals / (now - started)
                logger.info(
                    "cpl positions %d/%d, %.2f evals/s, about %.0f min left",
                    done, len(jobs), rate,
                    (len(jobs) - done) * (stats.evals / done) / max(rate, 1e-9) / 60,
                )
        if batch:
            cache.add_evals(batch)
        stats.wall_s = time.monotonic() - started
        return stats, sf.name
    finally:
        sf.close()


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between order statistics; +-inf allowed at the ends."""
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    low, high = math.floor(pos), math.ceil(pos)
    if low == high or ordered[low] == ordered[high]:
        return ordered[low]
    if math.isinf(ordered[low]) or math.isinf(ordered[high]):
        return ordered[low] if pos - low < 0.5 else ordered[high]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def interval(values: Sequence[float]) -> tuple[float, float]:
    return percentile(values, 0.025), percentile(values, 0.975)


def stdev(values: Sequence[float]) -> float:
    finite = [v for v in values if math.isfinite(v)]
    m = mean(finite)
    return math.sqrt(sum((v - m) ** 2 for v in finite) / (len(finite) - 1))


def pav_nonincreasing(values: Sequence[float]) -> list[float]:
    """The least-squares non-increasing fit (pool-adjacent-violators, equal weights)."""
    blocks: list[list[float]] = []  # [sum, count]
    for value in values:
        blocks.append([value, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] < blocks[-1][0] / blocks[-1][1]:
            total, count = blocks.pop()
            blocks[-1][0] += total
            blocks[-1][1] += count
    fitted: list[float] = []
    for total, count in blocks:
        fitted.extend([total / count] * int(count))
    return fitted


def crossing_elo(grid: Sequence[int], fitted: Sequence[float], target: float) -> float:
    """The smallest Elo whose fitted (non-increasing) CPL is <= target.

    Linear between grid points. -inf ("below 600") when the lowest grid Elo
    already qualifies, +inf ("above 2600") when none does.
    """
    if fitted[0] <= target:
        return -math.inf
    for i in range(1, len(grid)):
        if fitted[i] <= target:
            high, low = fitted[i - 1], fitted[i]
            return grid[i - 1] + (high - target) / (high - low) * (grid[i] - grid[i - 1])
    return math.inf


def loglinear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, list[float]]:
    """Least squares ln y = alpha + beta * x; returns alpha, beta, residuals of ln y."""
    logs = [math.log(y) for y in ys]
    mx, my = mean(xs), mean(logs)
    sxx = sum((x - mx) ** 2 for x in xs)
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, logs)) / sxx
    alpha = my - beta * mx
    return alpha, beta, [y - (alpha + beta * x) for x, y in zip(xs, logs)]


def elo_offset(maia: float, human: float, beta: float) -> float:
    """E = ln(M / H) / beta: positive when the setting plays stronger than its label."""
    if maia <= 0 or human <= 0:
        return math.nan
    return math.log(maia / human) / beta


@dataclass(frozen=True)
class Decision:
    d1: float
    d0: float
    f1: float
    f0: float
    share_d1_below_d0: float
    criterion1: bool
    criterion2: bool
    low_bands: tuple[int, ...]
    fit_usable: bool
    outcome: str
    recommendation: str


def decide(
    e1: dict[int, float],
    e0: dict[int, float],
    e1_upper: dict[int, float],
    se1: dict[int, float],
    se0: dict[int, float],
    share_d1_below_d0: float,
    beta_upper: float,
) -> Decision:
    """The decision rule approved on 2026-11-15. It must not change after the run starts.

    One guard, added before the run: the Elo offsets divide by the human slope,
    so when its 95% interval reaches zero (human CPL does not clearly fall with
    rating) they mean nothing and the rule makes no recommendation.
    """
    d1 = mean([abs(e1[b]) for b in BANDS])
    d0 = mean([abs(e0[b]) for b in BANDS])
    f1 = mean([FOLDED_NORMAL * se1[b] for b in BANDS])
    f0 = mean([FOLDED_NORMAL * se0[b] for b in BANDS])
    criterion1 = d1 < d0
    low_bands = tuple(b for b in BANDS if not e1_upper[b] > CRITERION2_FLOOR_ELO)
    criterion2 = not low_bands
    fit_usable = beta_upper < 0
    if not fit_usable:
        outcome = "the human fit is unusable"
        recommendation = (
            "none: the human slope's 95% interval reaches 0, so the Elo offsets mean "
            "nothing; check the human pool before choosing T"
        )
    elif not criterion1:
        outcome = "criterion 1 fails"
        recommendation = "T=0 (MAIA3_TEMPERATURE=0), for the user to approve"
    elif not criterion2:
        outcome = "only criterion 2 fails"
        recommendation = (
            "keep T=1 and file a follow-up for bands "
            + ", ".join(str(b) for b in low_bands)
            + " (for example a lower temperature there), for the user to decide"
        )
    else:
        outcome = "both criteria hold"
        recommendation = "keep T=1 (MAIA3_TEMPERATURE=1)"
    return Decision(
        d1, d0, f1, f0, share_d1_below_d0, criterion1, criterion2, low_bands,
        fit_usable, outcome, recommendation,
    )


# ---------------------------------------------------------------------------
# --report
# ---------------------------------------------------------------------------


class Results:
    """Engine moves and CPL looked up from the cache."""

    def __init__(self, cache: ProbeCache, model: str, engine: str) -> None:
        self.moves = cache.moves(model)
        self.evals = cache.evals(engine, SF_DEPTH)

    def move(self, position: Position, setting: str, elo: int, sample: int = 0) -> str:
        try:
            return self.moves[(position.key, setting, elo, sample)][0]
        except KeyError:
            raise LookupError(
                f"no {setting} move at {elo} sample {sample} for {position.key!r}; run --run first"
            ) from None

    def loss(self, position: Position, move: str) -> float:
        try:
            best = self.evals[(position.key, "")][1]
            e_best = clamp_cp(self.evals[(position.key, best)][0])
            e_move = clamp_cp(self.evals[(position.key, move)][0])
        except KeyError:
            raise LookupError(f"move {move} of {position.key!r} is not scored; run --run first") from None
        return float(min(max(e_best - e_move, 0), LOSS_CAP_CP))

    def t0_loss(self, position: Position, elo: int) -> float:
        return self.loss(position, self.move(position, "T0", elo))

    def t1_losses(self, position: Position, elo: int, k: int) -> list[float]:
        return [self.loss(position, self.move(position, "T1", elo, s)) for s in range(k)]


def cpl_summary(losses: Sequence[float]) -> dict:
    return {
        "n": len(losses),
        "mean": mean(losses),
        "median": median(losses),
        "share_ge_200": sum(1 for v in losses if v >= BLUNDER_CP) / len(losses),
    }


def fmt_elo(value: float) -> str:
    if value == -math.inf:
        return "<600"
    if value == math.inf:
        return ">2600"
    if math.isnan(value):
        return "n/a"
    return f"{value:.0f}"


def fmt_signed(value: float) -> str:
    if math.isinf(value) or math.isnan(value):
        return fmt_elo(value)
    return f"{value:+.0f}"


def disagreement_matrix(results: Results, positions: Sequence[Position]) -> dict[int, dict[int, float]]:
    moves = {elo: [results.move(p, "T0", elo) for p in positions] for elo in ELO_BINS}
    return {
        a: {b: sum(x != y for x, y in zip(moves[a], moves[b])) / len(positions) for b in ELO_BINS}
        for a in ELO_BINS
    }


def headline1(results: Results, positions: Sequence[Position], rng: random.Random) -> dict:
    """X(b): the Elo at which top-move Maia's (isotonic) mean CPL matches T=1 at b."""
    n = len(positions)
    t0 = [[results.t0_loss(p, elo) for elo in GRID] for p in positions]
    t1 = [[mean(results.t1_losses(p, band, K_H1)) for band in BANDS] for p in positions]

    def estimate(idx: Sequence[int]) -> tuple[list[float], list[float], list[float]]:
        curve = [mean([t0[i][g] for i in idx]) for g in range(len(GRID))]
        fitted = pav_nonincreasing(curve)
        t1_means = [mean([t1[i][b] for i in idx]) for b in range(len(BANDS))]
        return curve, fitted, [crossing_elo(GRID, fitted, m) for m in t1_means] + t1_means

    curve, fitted, point = estimate(range(n))
    xs, t1_means = point[: len(BANDS)], point[len(BANDS):]
    boot = [estimate([rng.randrange(n) for _ in range(n)])[2][: len(BANDS)] for _ in range(BOOTSTRAP_RESAMPLES)]
    return {
        "positions": n,
        "t0_curve": dict(zip(GRID, curve)),
        "t0_fitted": dict(zip(GRID, fitted)),
        "bands": {
            band: {
                "t1_mean": t1_means[i],
                "x": xs[i],
                "x_interval": interval([row[i] for row in boot]),
            }
            for i, band in enumerate(BANDS)
        },
    }


def headline2(results: Results, by_band: dict[int, list[Position]], rng: random.Random) -> dict:
    """Maia against humans on each band's positions, in cp and in Elo via the human slope."""
    data = {}
    for band in BANDS:
        rows = by_band[band]
        data[band] = (
            [results.loss(p, p.human) for p in rows],
            [mean(results.t1_losses(p, band, K_H2)) for p in rows],
            [results.t0_loss(p, band) for p in rows],
        )

    def estimate(idx: dict[int, Sequence[int]]) -> dict:
        h = {b: mean([data[b][0][i] for i in idx[b]]) for b in BANDS}
        m1 = {b: mean([data[b][1][i] for i in idx[b]]) for b in BANDS}
        m0 = {b: mean([data[b][2][i] for i in idx[b]]) for b in BANDS}
        alpha, beta, residuals = loglinear_fit(BANDS, [h[b] for b in BANDS])
        e1 = {b: elo_offset(m1[b], h[b], beta) for b in BANDS}
        e0 = {b: elo_offset(m0[b], h[b], beta) for b in BANDS}
        return {
            "h": h, "m1": m1, "m0": m0, "alpha": alpha, "beta": beta,
            "residuals": dict(zip(BANDS, residuals)), "e1": e1, "e0": e0,
            "d1": mean([abs(e1[b]) for b in BANDS]), "d0": mean([abs(e0[b]) for b in BANDS]),
        }

    sizes = {b: len(data[b][0]) for b in BANDS}
    point = estimate({b: range(sizes[b]) for b in BANDS})
    boot = [
        estimate({b: [rng.randrange(sizes[b]) for _ in range(sizes[b])] for b in BANDS})
        for _ in range(BOOTSTRAP_RESAMPLES)
    ]

    def band_interval(key: str, band: int) -> tuple[float, float]:
        return interval([r[key][band] for r in boot])

    bands = {}
    for b in BANDS:
        bands[b] = {
            "n": sizes[b],
            "h": point["h"][b], "m1": point["m1"][b], "m0": point["m0"][b],
            "m1_minus_h": point["m1"][b] - point["h"][b],
            "m1_minus_h_interval": interval([r["m1"][b] - r["h"][b] for r in boot]),
            "m0_minus_h": point["m0"][b] - point["h"][b],
            "m0_minus_h_interval": interval([r["m0"][b] - r["h"][b] for r in boot]),
            "e1": point["e1"][b], "e1_interval": band_interval("e1", b),
            "se1": stdev([r["e1"][b] for r in boot]),
            "e0": point["e0"][b], "e0_interval": band_interval("e0", b),
            "se0": stdev([r["e0"][b] for r in boot]),
            "residual": point["residuals"][b],
        }
    share = sum(r["d1"] < r["d0"] for r in boot) / len(boot)
    beta_interval = interval([r["beta"] for r in boot])
    decision = decide(
        point["e1"], point["e0"],
        {b: bands[b]["e1_interval"][1] for b in BANDS},
        {b: bands[b]["se1"] for b in BANDS},
        {b: bands[b]["se0"] for b in BANDS},
        share,
        beta_interval[1],
    )
    return {
        "alpha": point["alpha"],
        "beta": point["beta"],
        "beta_interval": beta_interval,
        "bands": bands,
        "decision": decision.__dict__,
    }


def build_report(results: Results, positions: Sequence[Position]) -> dict:
    by_band = natural_by_band(positions)
    h1 = headline1_natural(positions)
    pools = {
        "natural_headline1": h1,
        "tactical": [p for p in positions if p.pool == "tactical"],
        "opening": [p for p in positions if p.pool == "opening"],
    }
    report: dict = {"model": None, "engine": None}
    matrix = disagreement_matrix(results, h1)
    report["disagreement"] = {
        "positions": len(h1),
        "matrix": matrix,
        "600_vs_2600": matrix[600][2600],
        "600_vs_2600_count": round(matrix[600][2600] * len(h1)),
    }
    report["distinct_t1_moves"] = {
        name: {
            band: mean([len({results.move(p, "T1", band, s) for s in range(K_H1)}) for p in rows])
            for band in BANDS
        }
        for name, rows in pools.items()
    }
    cpl: dict = {}
    for name, rows in pools.items():
        cpl[name] = {
            "T0": {elo: cpl_summary([results.t0_loss(p, elo) for p in rows]) for elo in GRID},
            "T1": {
                band: cpl_summary([v for p in rows for v in results.t1_losses(p, band, K_H1)])
                for band in BANDS
            },
        }
    cpl["natural_by_band"] = {
        band: {
            "human": cpl_summary([results.loss(p, p.human) for p in by_band[band]]),
            "T0": cpl_summary([results.t0_loss(p, band) for p in by_band[band]]),
            "T1": cpl_summary([v for p in by_band[band] for v in results.t1_losses(p, band, K_H2)]),
        }
        for band in BANDS
    }
    report["cpl"] = cpl
    rng = random.Random(BOOTSTRAP_SEED)
    report["headline1"] = headline1(results, h1, rng)
    report["headline2"] = headline2(results, by_band, rng)
    return report


def print_report(report: dict) -> None:
    out = print
    out(f"# Maia3 strength probe: {report['model']}, {report['engine']} depth {SF_DEPTH}")
    out(f"fixture sha256 {report['fixture_sha256']}")
    d = report["disagreement"]
    out(f"\n## T=0 disagreement over the {d['positions']} headline-1 positions (% of positions where the moves differ)")
    out("      " + " ".join(f"{b:>5}" for b in ELO_BINS))
    for a in ELO_BINS:
        out(f"{a:>5} " + " ".join(f"{100 * d['matrix'][a][b]:5.0f}" for b in ELO_BINS))
    out(f"600 vs 2600: {d['600_vs_2600_count']} of {d['positions']} ({100 * d['600_vs_2600']:.1f}%)")

    out(f"\n## Distinct moves in {K_H1} T=1 samples (mean per position)")
    out("pool                " + " ".join(f"{b:>6}" for b in BANDS))
    for name, row in report["distinct_t1_moves"].items():
        out(f"{name:<20}" + " ".join(f"{row[b]:6.2f}" for b in BANDS))

    out("\n## CPL by pool and Elo: mean / median / share of moves losing >= 200 cp")
    for name in ("natural_headline1", "tactical", "opening"):
        block = report["cpl"][name]
        out(f"\n### {name}")
        out("setting  Elo     n    mean  median  >=200")
        for elo, s in block["T0"].items():
            out(f"T=0   {elo:>6} {s['n']:>5} {s['mean']:7.1f} {s['median']:7.1f} {100 * s['share_ge_200']:5.1f}%")
        for elo, s in block["T1"].items():
            out(f"T=1   {elo:>6} {s['n']:>5} {s['mean']:7.1f} {s['median']:7.1f} {100 * s['share_ge_200']:5.1f}%")
    out(f"\n### natural, each band's {NATURAL_PER_BAND} positions at its own Elo (T=1: {K_H2} samples)")
    out("band   who      n    mean  median  >=200")
    for band, rows in report["cpl"]["natural_by_band"].items():
        for who, s in rows.items():
            out(f"{band:>5}  {who:<6}{s['n']:>5} {s['mean']:7.1f} {s['median']:7.1f} {100 * s['share_ge_200']:5.1f}%")

    h1 = report["headline1"]
    out(f"\n## Headline 1: against the pre-outage bots ({h1['positions']} positions)")
    out("T=0 Elo   raw mean   isotonic")
    for elo in GRID:
        out(f"{elo:>7} {h1['t0_curve'][elo]:9.1f} {h1['t0_fitted'][elo]:10.1f}")
    for band, row in h1["bands"].items():
        low, high = row["x_interval"]
        out(
            f"Relative to the pre-outage bots, T=1 at {band} plays like top-move Maia at "
            f"~{fmt_elo(row['x'])} [{fmt_elo(low)}, {fmt_elo(high)}] (T=1 mean {row['t1_mean']:.1f} cp)."
        )
    out("Those bots played above their labels, so a large X alone doesn't show that T=1 plays below its label.")

    h2 = report["headline2"]
    beta = h2["beta"]
    out("\n## Headline 2: against Lichess blitz players of the band")
    out("band      H     M1     M0   M1-H [95%]          M0-H [95%]")
    for band, r in h2["bands"].items():
        a1, b1 = r["m1_minus_h_interval"]
        a0, b0 = r["m0_minus_h_interval"]
        out(
            f"{band:>5} {r['h']:6.1f} {r['m1']:6.1f} {r['m0']:6.1f}  "
            f"{r['m1_minus_h']:+6.1f} [{a1:+.1f}, {b1:+.1f}]  {r['m0_minus_h']:+6.1f} [{a0:+.1f}, {b0:+.1f}]"
        )
    lo_b, hi_b = h2["beta_interval"]
    out(
        f"fit ln H = a + b*Elo: a={h2['alpha']:.3f} b={beta:.6f} [{lo_b:.6f}, {hi_b:.6f}] "
        f"({100 * (math.exp(100 * beta) - 1):+.1f}% CPL per 100 Elo)"
    )
    out("residuals (ln H - fit): " + ", ".join(
        f"{b}: {100 * (math.exp(r['residual']) - 1):+.1f}%" for b, r in h2["bands"].items()
    ))
    out("band   E1 [95%]  SE1    E0 [95%]  SE0   (Elo; positive = stronger than the label)")
    for band, r in h2["bands"].items():
        a1, b1 = r["e1_interval"]
        a0, b0 = r["e0_interval"]
        out(
            f"{band:>5} {fmt_signed(r['e1'])} [{fmt_signed(a1)}, {fmt_signed(b1)}] {r['se1']:4.0f}   "
            f"{fmt_signed(r['e0'])} [{fmt_signed(a0)}, {fmt_signed(b0)}] {r['se0']:4.0f}"
        )
    dec = h2["decision"]
    out("\n## Decision rule (approved 2026-11-15)")
    out(
        "human fit (the slope's 95% interval below 0): "
        + ("holds" if dec["fit_usable"] else "FAILS, so the Elo offsets above mean nothing")
    )
    out(f"D1 = {dec['d1']:.0f} Elo (noise floor F1 = {dec['f1']:.0f});  D0 = {dec['d0']:.0f} Elo (F0 = {dec['f0']:.0f})")
    out(f"share of bootstrap resamples with D1 < D0: {100 * dec['share_d1_below_d0']:.1f}%")
    out(
        "How to read D: it is not the real distance from the label, and F must not be subtracted "
        "from it. A D near F can't be told from zero; a D of about 2F or more is within a few "
        "percent of the real distance; in between, D overstates by an amount that shrinks as D grows."
    )
    out(f"criterion 1 (D1 < D0): {'holds' if dec['criterion1'] else 'fails'}")
    out(
        f"criterion 2 (every band's E1 upper 95% bound above {CRITERION2_FLOOR_ELO:.0f}): "
        + ("holds" if dec["criterion2"] else f"fails at {list(dec['low_bands'])}")
    )
    out(f"outcome: {dec['outcome']}. Recommendation: {dec['recommendation']}.")


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float) and math.isinf(value):
        return "-inf" if value < 0 else "inf"
    return value


def run_report(args: argparse.Namespace) -> int:
    header, positions = load_fixture(args.fixture)
    cache = ProbeCache(args.cache)
    engine = args.sf_engine or _cached_engine_name(cache)
    results = Results(cache, args.model, engine)
    report = build_report(results, positions)
    report["model"] = args.model
    report["engine"] = engine
    report["fixture_sha256"] = fixture_digest(args.fixture)
    print_report(report)
    out = args.json or PROBE_DIR / f"report-{args.model}.json"
    Path(out).write_text(json.dumps(_jsonable(report), indent=2) + "\n")
    logger.info("wrote %s", out)
    return 0


def _cached_engine_name(cache: ProbeCache) -> str:
    rows = cache.db.execute("SELECT DISTINCT engine FROM evals WHERE depth = ?", (SF_DEPTH,)).fetchall()
    if len(rows) != 1:
        raise SystemExit(f"expected one Stockfish version in the cache, found {[r[0] for r in rows]}; pass --sf-engine")
    return rows[0][0]


# ---------------------------------------------------------------------------
# --pilot and --run
# ---------------------------------------------------------------------------


def pilot_positions(positions: Sequence[Position]) -> tuple[list[Position], list[Position]]:
    """PILOT_PER_BAND headline-1 and PILOT_PER_BAND headline-2-only positions per band."""
    by_band = natural_by_band(positions)
    grid = [p for b in BANDS for p in by_band[b][:PILOT_PER_BAND]]
    own = [p for b in BANDS for p in by_band[b][H1_PER_BAND: H1_PER_BAND + PILOT_PER_BAND]]
    return grid, own


def run_pilot(args: argparse.Namespace) -> int:
    _header, positions = load_fixture(args.fixture)
    weights_dir = _weights_dir(args.model)
    grid, own = pilot_positions(positions)
    cache = ProbeCache(args.cache)
    all_tasks = plan_engine_tasks(positions, positions)
    cached_before = cache.moves(args.model)
    pilot_tasks = plan_engine_tasks(grid + own, positions)
    engine = run_engine_tasks(pilot_tasks, cache, args.model, weights_dir, args.engines, args.threads)
    cached_after = cache.moves(args.model)
    remaining_moves = sum(1 for t in all_tasks if t.key not in cached_after)
    lines = [f"pilot: {len(grid)} grid positions, {len(own)} own-band positions"]
    if engine.done:
        rate = engine.done / engine.wall_s
        lines.append(
            f"engine: {engine.done} moves in {engine.wall_s:.0f} s with {args.engines} engines "
            f"= {rate:.1f} moves/s; move p50 {median(engine.move_ms):.0f} ms, "
            f"p95 {percentile(engine.move_ms, 0.95):.0f} ms (under load)"
        )
    else:
        rate = None
        lines.append(f"engine: all pilot moves were cached ({len(cached_before)} before)")
    engine_eta = remaining_moves / rate if rate else None

    sf_eta = None
    if not args.no_cpl:
        path = stockfish_path(args.stockfish)
        cpl, sf_name = run_cpl(grid + own, cache, args.model, path, args.sf_workers)
        if cpl.evals:
            sf_rate = cpl.evals / cpl.wall_s
            per_grid = mean([cpl.evals_by_position.get(p.key, 0) for p in grid])
            per_own = mean([cpl.evals_by_position.get(p.key, 0) for p in own])
            # Natural roots were scored during the pool build; tactical and opening roots were not.
            grid_rest = len(headline1_set(positions)) - len(grid)
            own_rest = len(positions_of(positions, "natural")) - len(headline1_natural(positions)) - len(own)
            non_natural = len(positions) - len(positions_of(positions, "natural"))
            projected = per_grid * grid_rest + per_own * own_rest + non_natural
            sf_eta = projected / sf_rate
            lines.append(
                f"stockfish ({sf_name}, depth {SF_DEPTH}): {cpl.evals} evals in {cpl.wall_s:.0f} s with "
                f"{args.sf_workers} workers = {sf_rate:.2f} evals/s; {per_grid:.1f} evals per grid "
                f"position, {per_own:.1f} per own-band position; about {projected:.0f} evals left"
            )
        else:
            lines.append("stockfish: all pilot evals were cached")
    lines.append(f"remaining engine moves: {remaining_moves}")
    if engine_eta is not None:
        lines.append(f"projected engine stage: {engine_eta / 3600:.2f} h")
    if sf_eta is not None:
        lines.append(f"projected Stockfish stage: {sf_eta / 3600:.2f} h")
    if engine_eta is not None and sf_eta is not None:
        lines.append(f"projected full run: {(engine_eta + sf_eta) / 3600:.2f} h wall clock (stages run one after the other)")
    for line in lines:
        print(line)
    cache.close()
    return 0


def positions_of(positions: Sequence[Position], pool: str) -> list[Position]:
    return [p for p in positions if p.pool == pool]


def run_full(args: argparse.Namespace) -> int:
    _header, positions = load_fixture(args.fixture)
    weights_dir = _weights_dir(args.model)
    cache = ProbeCache(args.cache)
    tasks = plan_engine_tasks(positions, positions)
    logger.info("full run: %d positions, %d engine moves planned", len(positions), len(tasks))
    stats = run_engine_tasks(tasks, cache, args.model, weights_dir, args.engines, args.threads)
    logger.info("engine stage done: %d moves in %.0f s", stats.done, stats.wall_s)
    if not args.no_cpl:
        cpl, _name = run_cpl(positions, cache, args.model, stockfish_path(args.stockfish), args.sf_workers)
        logger.info("cpl stage done: %d evals over %d positions in %.0f s", cpl.evals, cpl.positions, cpl.wall_s)
    cache.close()
    return 0


def _weights_dir(model: str) -> Path:
    settings = maia3_settings()
    pin = maia3_pins.MODEL_PINS[model]
    path = Path(settings.weights_dir) / pin.filename
    maia3_pins.verify_weights(path, pin)
    return Path(settings.weights_dir)


# ---------------------------------------------------------------------------
# --latency
# ---------------------------------------------------------------------------


class SpawnTiming:
    """An opener that records spawn -> uciok -> readyok -> warm-up done."""

    def __init__(self) -> None:
        self.marks: dict[str, float] = {}

    async def opener(self, argv: list[str], env: dict[str, str]):
        self.marks["spawn"] = time.monotonic()
        handle = await open_uci(argv, env)
        self.marks["uciok"] = time.monotonic()
        return _TimedHandle(handle, self.marks)

    def phases_ms(self) -> dict[str, float]:
        m = self.marks
        out = {}
        if "uciok" in m:
            out["spawn_to_uciok_ms"] = (m["uciok"] - m["spawn"]) * 1000
        if "readyok" in m:
            out["to_readyok_ms"] = (m["readyok"] - m["spawn"]) * 1000
        if "warmup" in m:
            out["to_warmup_done_ms"] = (m["warmup"] - m["spawn"]) * 1000
        return out


class _TimedHandle:
    def __init__(self, inner, marks: dict[str, float]) -> None:
        self._inner = inner
        self._marks = marks
        self.pid = inner.pid
        self.exited = inner.exited

    async def ping(self) -> None:
        await self._inner.ping()
        self._marks.setdefault("readyok", time.monotonic())

    async def play(self, board: chess.Board, elo: int, temperature: float):
        move = await self._inner.play(board, elo, temperature)
        self._marks.setdefault("warmup", time.monotonic())
        return move

    async def quit(self) -> None:
        await self._inner.quit()

    def kill(self) -> None:
        self._inner.kill()


def child_memory_kb(pid: int, proc_root: Path = Path("/proc")) -> dict[str, int | str]:
    """VmRSS and VmHWM from /proc on Linux (not ru_maxrss); ps rss elsewhere."""
    status = proc_root / str(pid) / "status"
    if proc_root.is_dir():
        out: dict[str, int | str] = {"source": "proc"}
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                name, value = line.split(":", 1)
                out[name.lower()] = int(value.split()[0])
        return out
    rss = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, check=True
    ).stdout.strip()
    return {"source": "ps", "vmrss": int(rss)}


def run_latency(args: argparse.Namespace) -> int:
    _header, positions = load_fixture(args.fixture)
    per_band = args.latency_moves // len(BANDS)
    sample = [pos for rows in natural_by_band(positions).values() for pos in rows[:per_band]]
    boards = [(presented_board(p), p.band) for p in sample]
    rows = []
    for model in args.latency_models:
        try:
            weights_dir = _weights_dir(model)
        except maia3_pins.WeightsError as exc:
            print(f"{model}: skipped ({exc}); fetch with: python scripts/prepare_maia3_engine.py --model {model} --no-smoke")
            continue
        for threads in args.latency_threads:
            timing = SpawnTiming()
            settings = Maia3Settings(
                enabled=True, model=model, weights_dir=weights_dir, temperature=1.0, top_p=1.0,
                threads=threads, move_timeout_s=60.0, infer_timeout_s=120.0, start_timeout_s=180.0,
            )
            service = Maia3EngineService(settings, opener=timing.opener)
            started = time.monotonic()
            try:
                if not service.wait_ready(240, stop_on_failure=True):
                    raise RuntimeError(f"{model} threads={threads} did not start: {service.last_spawn_error}")
                ready_ms = (time.monotonic() - started) * 1000
                pid = service.handle.pid
                peak = {"rss": 0}
                stop = threading.Event()

                def sample_memory() -> None:
                    while not stop.wait(MEMORY_SAMPLE_S):
                        try:
                            peak["rss"] = max(peak["rss"], int(child_memory_kb(pid)["vmrss"]))
                        except Exception:  # noqa: BLE001 - the child may be exiting
                            return

                sampler = threading.Thread(target=sample_memory, daemon=True)
                sampler.start()
                latencies = []
                for board, elo in boards:
                    t = time.perf_counter()
                    service.play(board.copy(), elo)
                    latencies.append((time.perf_counter() - t) * 1000)
                stop.set()
                sampler.join()
                memory = child_memory_kb(pid)
            finally:
                service.shutdown()
            row = {
                "model": model,
                "threads": threads,
                "platform": f"{sys.platform}/{os.uname().machine}",
                "ready_ms": ready_ms,
                **timing.phases_ms(),
                "moves": len(latencies),
                "p50_ms": median(latencies),
                "p95_ms": percentile(latencies, 0.95),
                "max_ms": max(latencies),
                "child_vmrss_kb": memory.get("vmrss"),
                "child_vmhwm_kb": memory.get("vmhwm"),
                "child_rss_peak_sampled_kb": peak["rss"],
                "memory_source": memory["source"],
            }
            rows.append(row)
            print(
                "maia3 latency model={model} threads={threads} platform={platform} "
                "spawn_to_uciok_ms={u:.0f} to_readyok_ms={r:.0f} to_warmup_done_ms={w:.0f} "
                "moves={moves} p50_ms={p50_ms:.1f} p95_ms={p95_ms:.1f} max_ms={max_ms:.1f} "
                "child_vmrss_kb={child_vmrss_kb} child_vmhwm_kb={child_vmhwm_kb} "
                "child_rss_peak_sampled_kb={child_rss_peak_sampled_kb} memory_source={memory_source}".format(
                    u=row.get("spawn_to_uciok_ms", math.nan),
                    r=row.get("to_readyok_ms", math.nan),
                    w=row.get("to_warmup_done_ms", math.nan),
                    **row,
                ),
                flush=True,
            )
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    cpus = os.cpu_count() or 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build-pool", metavar="PGN", help="decompressed Lichess PGN ('-' for stdin)")
    mode.add_argument("--pilot", action="store_true", help="20 positions per stage; projected wall clock")
    mode.add_argument("--run", action="store_true", help="the full run (resumable)")
    mode.add_argument("--report", action="store_true", help="tables, both headlines, the decision rule")
    mode.add_argument("--latency", action="store_true", help="cold start, per-move latency, child memory")
    parser.add_argument("--model", default=os.environ.get("MAIA3_MODEL", "maia3-23m"), choices=sorted(maia3_pins.MODEL_PINS))
    parser.add_argument("--threads", type=int, default=1, help="engine threads for --pilot/--run")
    parser.add_argument("--engines", type=int, default=max(1, cpus - 2), help="parallel engines for --pilot/--run")
    parser.add_argument("--sf-workers", type=int, default=max(1, cpus - 2), help="parallel Stockfish processes")
    parser.add_argument("--stockfish", help="Stockfish binary (default: STOCKFISH_PATH, then PATH)")
    parser.add_argument("--sf-engine", help="--report: the Stockfish id name whose evals to use")
    parser.add_argument("--no-cpl", action="store_true", help="--pilot/--run: engine moves only, no Stockfish")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--json", type=Path, help="--report/--latency: write the numbers here too")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="--build-pool seed")
    parser.add_argument("--source-url", default=SOURCE_URL, help="--build-pool: the URL recorded in the header")
    parser.add_argument("--max-games", type=int, help="--build-pool: stop after this many games")
    parser.add_argument(
        "--latency-models", type=lambda s: s.split(","), default=["maia3-23m", "maia3-5m"]
    )
    parser.add_argument(
        "--latency-threads", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2]
    )
    parser.add_argument("--latency-moves", type=int, default=LATENCY_MOVES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # One INFO line per engine move is noise here; warnings still show.
    logging.getLogger("app.maia3_client").setLevel(logging.WARNING)
    if args.build_pool:
        return run_build_pool(args)
    if args.pilot:
        return run_pilot(args)
    if args.run:
        return run_full(args)
    if args.report:
        return run_report(args)
    return run_latency(args)


if __name__ == "__main__":
    sys.exit(main())
