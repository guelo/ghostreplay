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

Temperature per rating (g-maia3-elo-temp). A second set of modes fits a
temperature curve, one anchor per band, and checks it against the flatness rule
the user approved on 2026-11-22, before any run. That rule must not change after
the sweep starts.

    # The fit pool: the fixture above, its mover sidecar, and two more bands.
    zstd -dc prefix.pgn.zst | python scripts/probe_maia3.py --mover-index -
    zstd -dc prefix.pgn.zst | python scripts/probe_maia3.py --build-pool - \
        --bands 1300,2300 --pools natural \
        --fixture scripts/fixtures/maia3_probe_fit_extra.json \
        --exclude-fixture scripts/fixtures/maia3_probe_positions.json

    # The validation pool: another month and seed, every fit position held out.
    zstd -dc other-month.pgn.zst | python scripts/probe_maia3.py --build-pool - \
        --bands 600,1100,1300,1500,2000,2300,2600 --pools natural,opening \
        --seed 20261124 --source-url <URL> \
        --fixture scripts/fixtures/maia3_probe_validation.json \
        --exclude-fixture scripts/fixtures/maia3_probe_positions.json \
        --exclude-fixture scripts/fixtures/maia3_probe_fit_extra.json

    python scripts/probe_maia3.py --sweep --pilot    # projected wall clock
    python scripts/probe_maia3.py --sweep            # eight temperatures per band
    python scripts/probe_maia3.py --fit              # the anchors and the curve
    python scripts/probe_maia3.py --ladder --curve <curve>              # fit pool
    python scripts/probe_maia3.py --validate --pilot --curve <curve>
    python scripts/probe_maia3.py --validate --curve <curve>            # the rule
    python scripts/probe_maia3.py --ladder --pool validation --temperature 1

    # Decision 7 (the labels are blitz ratings): games per band and speed, and
    # how rapid and classical players of the same number compare, in Elo over
    # the human slope of a --report, --fit or --validate.
    zstd -dc prefix.pgn.zst | python scripts/probe_maia3.py --speed-counts - \
        --bands 600,1100,1300,1500,2000,2300,2600 --slope <b> [--max-games <n>]

A fixture stores, per natural position, a mover index: an integer shared by the
positions of one band with the same player to move, never a name. Intervals
resample movers, and reports print how many movers stand behind the positions.
A position is identified by the EPD of its board; --build-pool keeps every
position of an --exclude-fixture out of the new fixture, and --sweep, --fit,
--ladder and --validate refuse to run when two of their fixtures share one.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
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
    TemperatureCurve,
    format_temperature_curve,
    maia3_settings,
    open_uci,
    parse_temperature_curve,
    position_for,
)

logger = logging.getLogger("probe_maia3")

SOURCE_URL = (
    "https://database.lichess.org/standard/lichess_db_standard_rated_2025-10.pgn.zst"
)
FIXTURE_PATH = BACKEND / "scripts" / "fixtures" / "maia3_probe_positions.json"
FIT_EXTRA_PATH = BACKEND / "scripts" / "fixtures" / "maia3_probe_fit_extra.json"
VALIDATION_PATH = BACKEND / "scripts" / "fixtures" / "maia3_probe_validation.json"
PROBE_DIR = BACKEND / ".maia3_probe"
CACHE_PATH = PROBE_DIR / "cache.sqlite3"
ECO_PATH = BACKEND / "public" / "data" / "openings" / "eco.json"
# Schema 2 adds the optional per-position "mover". --build-pool writes it; the
# committed 2026-11-17 fixture is schema 1 and gets its movers from a sidecar.
FIXTURE_SCHEMA = 2
FIXTURE_SCHEMAS = (1, 2)
MOVERS_SCHEMA = 1
DEFAULT_SEED = 20261115

# Pools and runs.
BANDS = (600, 1100, 1500, 2000, 2600)
BAND_HALF_WIDTH = 100
GRID = tuple(range(600, 2601, 100))
NATURAL_PER_BAND = 1000
H1_PER_BAND = 100
K_H1 = 20
K_H2 = 5

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

# Temperature per rating (g-maia3-elo-temp): the sweep's grid, and the flatness
# rule the user approved on 2026-11-22, before the sweep. These must not change
# after the sweep starts.
SWEEP_TEMPERATURES = (0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0)
TOPP_ARMS = (0.9, 0.8)  # report only: T=1 with TopP under 1, at TOPP_BANDS
TOPP_BANDS = (2300, 2600)
VARIETY_TARGET = 2.0  # distinct moves in K_H1 samples: the fit's floor, and R3
R1_SHARE = 0.95
R2_BAND_ELO = 150.0
LADDER_PER_BAND = 300  # R4: every level plays each band's first 300 positions
LADDER_RESAMPLES = 4000
LADDER_CONFIDENCE = 0.95  # one-sided, simultaneous over all pairs of levels
FLATNESS_SEED = 20261122

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

_POSITION_KEYS = frozenset({"pool", "band", "moves", "human", "label", "mover"})
POOLS = ("natural", "tactical", "opening")


@dataclass(frozen=True)
class Position:
    """One probe position: UCI moves from the start, its pool and, for Lichess, band and human move.

    ``mover`` is an integer that is equal for the natural positions of one band
    that have the same player to move. It is a number, never a name.
    """

    pool: str
    moves: tuple[str, ...]
    band: int | None = None
    human: str | None = None
    label: str | None = None
    mover: int | None = None

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
        if self.mover is not None:
            out["mover"] = self.mover
        return out


class FixtureError(ValueError):
    """The probe fixture is missing, malformed or holds an illegal position."""


def position_from_json(entry: dict, bands: Sequence[int] = BANDS) -> Position:
    extra = set(entry) - _POSITION_KEYS
    if extra:
        raise FixtureError(f"unexpected keys {sorted(extra)} in {entry}")
    pool = entry.get("pool")
    if pool not in POOLS:
        raise FixtureError(f"unknown pool {pool!r}")
    moves = tuple(entry.get("moves", "").split())
    band = entry.get("band")
    if band is not None and band not in bands:
        raise FixtureError(f"unknown band {band!r}")
    if pool == "natural" and (band is None or entry.get("human") is None):
        raise FixtureError(f"natural position without band and human move: {entry}")
    mover = entry.get("mover")
    if mover is not None and (pool != "natural" or not isinstance(mover, int) or isinstance(mover, bool)):
        raise FixtureError(f"mover must be an integer on a natural position: {entry}")
    return Position(pool, moves, band, entry.get("human"), entry.get("label"), mover)


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


def fixture_bands(header: dict) -> tuple[int, ...]:
    """The bands a fixture was built for (its header's natural_filters.bands)."""
    return tuple(header.get("natural_filters", {}).get("bands", BANDS))


def load_fixture(path: Path = FIXTURE_PATH, check: bool = True) -> tuple[dict, list[Position]]:
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise FixtureError(
            f"no probe fixture at {path}; build it with --build-pool (see the docstring)"
        ) from None
    header = data.get("header", {})
    if header.get("schema") not in FIXTURE_SCHEMAS:
        raise FixtureError(f"fixture schema is not one of {FIXTURE_SCHEMAS}")
    bands = fixture_bands(header)
    positions = [position_from_json(entry, bands) for entry in data["positions"]]
    if check:
        for position in positions:
            check_position(position)
    return header, positions


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


def natural_by_band(
    positions: Iterable[Position], bands: Sequence[int] = BANDS
) -> dict[int, list[Position]]:
    """Natural positions per band, in the fixture's seeded order."""
    out: dict[int, list[Position]] = {band: [] for band in bands}
    for position in positions:
        if position.pool == "natural":
            out[position.band].append(position)
    return out


# Held-out positions and the mover index (g-maia3-elo-temp, decisions 4 and 5).


@functools.lru_cache(maxsize=None)
def _epd_after(moves: tuple[str, ...]) -> str:
    board = chess.Board()
    for uci in moves:
        board.push_uci(uci)
    return board.epd()


def position_id(position: Position) -> str:
    """What makes two positions the same one: the EPD of the board to move on.

    Pieces, side to move, castling rights and the en-passant square, so equal
    move lists and transpositions share an id. This is deliberately wider than
    the engine's input (the board 8 plies back plus the last 8 moves): a
    position on a fit board is held out whatever its history.
    """
    return _epd_after(position.moves)


def assert_disjoint(fixtures: Sequence[tuple[str, Sequence[Position]]]) -> int:
    """Raise FixtureError if two of the named fixtures share a position; else the count checked."""
    owner: dict[str, tuple[str, Position]] = {}
    shared: list[str] = []
    checked = 0
    for name, positions in fixtures:
        for position in positions:
            checked += 1
            pid = position_id(position)
            first = owner.setdefault(pid, (name, position))
            if first[0] != name:
                shared.append(
                    f"{first[0]} [{first[1].pool}] {first[1].key!r} = {name} [{position.pool}] {position.key!r}"
                )
    if shared:
        raise FixtureError(
            f"{len(shared)} positions are shared between fixtures: " + "; ".join(shared[:5])
            + ("; ..." if len(shared) > 5 else "")
        )
    return checked


def movers_path(fixture_path: Path) -> Path:
    """Where the mover sidecar of a schema-1 fixture lives."""
    fixture_path = Path(fixture_path)
    return fixture_path.with_name(fixture_path.stem + "_movers.json")


def attach_movers(path: Path, positions: Sequence[Position]) -> list[Position]:
    """The positions with their mover index: the fixture's own, or its sidecar's.

    A sidecar belongs to one exact fixture file. One whose sha256 is not the
    fixture's is refused, not ignored: it would group the wrong positions.
    Without either, the positions come back unchanged.
    """
    sidecar = movers_path(path)
    if any(p.mover is not None for p in positions) or not sidecar.exists():
        return list(positions)
    data = json.loads(sidecar.read_text())
    if data.get("schema") != MOVERS_SCHEMA or data.get("fixture_sha256") != fixture_digest(path):
        raise FixtureError(
            f"{sidecar.name} was not built for this {Path(path).name} (schema or sha256 differs); "
            "rebuild it with --mover-index"
        )
    remaining = {int(band): list(reversed(index)) for band, index in data["movers"].items()}
    out = []
    for position in positions:
        if position.pool == "natural":
            try:
                mover = remaining[position.band].pop()
            except (KeyError, IndexError):
                raise FixtureError(f"{sidecar.name} is short of movers for band {position.band}") from None
            position = Position(
                position.pool, position.moves, position.band, position.human, position.label, mover
            )
        out.append(position)
    if any(remaining.values()):
        raise FixtureError(f"{sidecar.name} holds more movers than the fixture has natural positions")
    return out


@dataclass(frozen=True)
class Pool:
    """A loaded fixture: its header, bands, digest and positions (movers attached)."""

    path: Path
    header: dict
    positions: tuple[Position, ...]
    sha256: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def bands(self) -> tuple[int, ...]:
        return fixture_bands(self.header)

    def natural(self) -> dict[int, list[Position]]:
        return natural_by_band(self.positions, self.bands)

    def opening(self) -> list[Position]:
        return [p for p in self.positions if p.pool == "opening"]


def load_pool(path: Path, check: bool = True) -> Pool:
    header, positions = load_fixture(path, check=check)
    return Pool(Path(path), header, tuple(attach_movers(path, positions)), fixture_digest(path))


def mover_summary(positions: Sequence[Position]) -> dict | None:
    """How many players stand behind these positions; None without a mover index.

    1,000 positions are not 1,000 independent observations when players repeat,
    so every report prints this next to its position count.
    """
    movers = [p.mover for p in positions]
    if not movers or any(m is None for m in movers):
        return None
    counts = Counter(movers)
    return {
        "positions": len(movers),
        "movers": len(counts),
        "busiest_share": max(counts.values()) / len(movers),
        "repeated_share": sum(c for c in counts.values() if c > 1) / len(movers),
    }


def with_mover_index(positions: Sequence[Position], names: Sequence[str]) -> list[Position]:
    """Natural positions of one band with ``mover`` set: an integer per name, by first appearance."""
    index: dict[str, int] = {}
    return [
        Position(p.pool, p.moves, p.band, p.human, p.label, index.setdefault(name, len(index)))
        for p, name in zip(positions, names, strict=True)
    ]


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
        # The players' names group a band's positions by mover (decision 5 of
        # g-maia3-elo-temp). They stay in memory; a fixture stores integers.
        b"White", b"Black",
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


def band_of(elo: int, bands: Sequence[int] = BANDS) -> int | None:
    for band in bands:
        if abs(elo - band) <= BAND_HALF_WIDTH:
            return band
    return None


def qualify(headers: dict[str, str], bands: Sequence[int] = BANDS) -> int | None:
    """The band of a qualifying game, or None.

    Rated, standard chess from the start position, blitz by Lichess's own rule,
    no BOT on either side, not ended for a rules infraction, and both players
    within BAND_HALF_WIDTH of the same band. Bands 200 apart share an edge
    rating; the first band that holds both players takes the game.
    """
    seconds = rated_standard_seconds(headers)
    if seconds is None or not BLITZ_SECONDS[0] <= seconds <= BLITZ_SECONDS[1]:
        return None
    return players_band(headers, bands)


def rated_standard_seconds(headers: dict[str, str]) -> int | None:
    """Lichess's estimated duration of a rated standard game from the start position, or None."""
    if not headers.get("Event", "").startswith("Rated "):
        return None
    if "FEN" in headers or headers.get("SetUp") == "1":
        return None
    if headers.get("Variant", "Standard") != "Standard":
        return None
    return blitz_seconds(headers.get("TimeControl", ""))


def players_band(headers: dict[str, str], bands: Sequence[int] = BANDS) -> int | None:
    """The band that holds both players, or None; also None with a BOT or after a rules infraction."""
    if "BOT" in (headers.get("WhiteTitle"), headers.get("BlackTitle")):
        return None
    if headers.get("Termination") == "Rules infraction":
        return None
    try:
        white, black = int(headers["WhiteElo"]), int(headers["BlackElo"])
    except (KeyError, ValueError):
        return None
    for band in bands:
        if abs(white - band) <= BAND_HALF_WIDTH and abs(black - band) <= BAND_HALF_WIDTH:
            return band
    return None


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
    """A position drawn from a qualifying game.

    game_index, utc and the mover's name stay out of the fixture; ``epd`` is
    the position's id (``position_id``), taken while the game is replayed.
    """

    band: int
    moves: tuple[str, ...]
    human: str
    game_index: int
    utc: str
    mover: str = ""
    epd: str = ""

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
        for san in sans[:ply]:
            move = board.parse_san(san)
            ucis.append(move.uci())
            board.push(move)
        epd = board.epd()
        human = board.parse_san(sans[ply]).uci()
    except ValueError:
        return None
    mover = game.headers.get("White" if board.turn == chess.WHITE else "Black", "")
    return Candidate(
        band, tuple(ucis), human, game_index, game_utc(game.headers),
        # A game without a name is its own mover.
        mover or f"?{game.headers.get('Site') or game_index}",
        epd,
    )


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
    # Candidates that never reached a selector: already in an excluded fixture,
    # or the same position as an earlier candidate of this build.
    dropped_excluded: int = 0
    dropped_repeated: int = 0


def selection_window(selector: OrderedSelector) -> dict | None:
    """Where in the stream a pool's positions come from, and how many candidates it judged.

    None for a pool that selects nothing (a build without a tactical pool).
    """
    chosen = selector.selected()
    if not chosen:
        return None
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
    bands: Sequence[int] = BANDS,
    exclude: frozenset[str] | None = None,
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

    Held-out positions: with ``exclude`` (position ids, possibly empty), a
    candidate whose position is excluded, or is the position of an earlier
    candidate of this build, is dropped before it reaches a selector. That
    happens in stream order too, and the selector fills the band from later
    games. ``exclude=None`` (the 2026-11-17 build) drops nothing.
    """
    natural = {band: OrderedSelector(per_band, band_cap) for band in bands}
    tactical = {band: OrderedSelector(tactical_per_band, tactical_cap) for band in bands}
    seen: set[str] = set()
    dropped = {"excluded": 0, "repeated": 0}
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
        band = qualify(game.headers, bands)
        if band is not None:
            selector = natural[band] if not natural[band].at_cap else tactical[band]
            if not selector.at_cap:
                candidate = candidate_from_game(game, band, seed, game_index)
                if candidate is not None and exclude is not None:
                    if candidate.epd in exclude:
                        dropped["excluded"] += 1
                        candidate = None
                    elif candidate.epd in seen:
                        dropped["repeated"] += 1
                        candidate = None
                    else:
                        seen.add(candidate.epd)
                if candidate is not None:
                    selector.add(candidate)
        drain(block=False)
        pump()

    chosen_natural = {band: natural[band].selected() for band in bands}
    chosen_tactical = [c for band in bands for c in tactical[band].selected()]
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
        natural_windows={band: selection_window(natural[band]) for band in bands},
        only_move_windows={
            band: window for band in bands if (window := selection_window(tactical[band]))
        },
        dropped_excluded=dropped["excluded"],
        dropped_repeated=dropped["repeated"],
    )


def opening_positions(
    eco_path: Path,
    seed: int,
    count: int = OPENING_COUNT,
    exclude: frozenset[str] | None = None,
    dropped: list[int] | None = None,
) -> list[Position]:
    """``count`` distinct opening-book positions at OPENING_PLIES, sampled with ``seed``.

    Lines are deduplicated by the book's own EPD of their final position; the
    chosen ones are replayed and checked by the caller. Lines whose position id
    is in ``exclude`` leave the book before the sampling, so the result still
    has ``count`` positions; ``dropped`` receives how many left.
    """
    entries = json.loads(Path(eco_path).read_text())["entries"]
    by_epd: dict[str, tuple[tuple[str, ...], str]] = {}
    for entry in sorted(entries, key=lambda e: e["uci"]):
        moves = tuple(entry["uci"].split())
        if OPENING_PLIES[0] <= len(moves) <= OPENING_PLIES[1]:
            by_epd.setdefault(entry["epd"], (moves, f"{entry['eco']} {entry['name']}"))
    pool = sorted(by_epd.values())
    if exclude:
        kept = [line for line in pool if position_id(Position("opening", line[0])) not in exclude]
        if dropped is not None:
            dropped.append(len(pool) - len(kept))
        pool = kept
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
    bands = tuple(args.bands)
    pools = tuple(pool for pool in POOLS if pool in args.pools)
    if "natural" not in pools:
        raise SystemExit("--pools must include natural")
    if Path(args.fixture).exists() and not args.overwrite:
        raise SystemExit(f"{args.fixture} exists; pass --overwrite to replace it")
    # Every position of these fixtures is held out of the new one.
    excluded = [load_pool(path) for path in args.exclude_fixture]
    excluded_ids = frozenset(position_id(p) for pool in excluded for p in pool.positions)
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
            per_band=NATURAL_PER_BAND,
            tactical_per_band=TACTICAL_PER_BAND if "tactical" in pools else 0,
            band_cap=CANDIDATE_CAP_PER_BAND,
            max_games=args.max_games,
            progress=logger.info,
            on_rows=keep_rows,
            bands=bands,
            exclude=excluded_ids,
        )
        hand = hand_tactical_positions() if "tactical" in pools else []
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
    for band in bands:
        chosen = list(build.natural[band])
        random.Random(f"{args.seed}:order:{band}").shuffle(chosen)
        natural.extend(
            with_mover_index([c.position("natural") for c in chosen], [c.mover for c in chosen])
        )
    tactical = hand + [c.position("tactical") for c in build.tactical]
    opening: list[Position] = []
    opening_dropped: list[int] = []
    if "opening" in pools:
        # No opening line may repeat a held-out position or one of this build.
        own_ids = frozenset(position_id(p) for p in natural + tactical)
        opening = opening_positions(
            ECO_PATH, args.seed, exclude=excluded_ids | own_ids, dropped=opening_dropped
        )
    positions = natural + tactical + opening
    for position in positions:
        check_position(position)
    if len({position_id(p) for p in positions}) != len(positions):
        raise FixtureError("the new fixture repeats a position")
    name = Path(args.fixture).name
    assert_disjoint([(name, positions)] + [(pool.name, pool.positions) for pool in excluded])
    movers = {band: mover_summary(rows) for band, rows in natural_by_band(natural, bands).items()}

    header = {
        "schema": FIXTURE_SCHEMA,
        "description": (
            "Maia3 probe positions (g-maia3-strength-probe, g-maia3-elo-temp). Public "
            "Lichess games (CC0 database dump) and the repo opening book; no usernames, "
            "game IDs, dates or clock values are stored per position. 'mover' is an "
            "integer shared by the natural positions of one band that have the same "
            "player to move."
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
                "movers": movers[band]["movers"],
            }
            for band, w in build.natural_windows.items()
        },
        "excluded_fixtures": [
            {"file": pool.name, "sha256": pool.sha256, "positions": len(pool.positions)}
            for pool in excluded
        ],
        "held_out": {
            "identity": "EPD of the board to move on: pieces, side to move, castling rights, en passant",
            "natural_candidates_dropped_as_excluded": build.dropped_excluded,
            "natural_candidates_dropped_as_repeated": build.dropped_repeated,
            "opening_lines_dropped": sum(opening_dropped),
            "shared_positions": 0,
        },
        "seed": args.seed,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "stockfish": {"engine": workers.name, "depth": SF_DEPTH},
        "natural_filters": {
            "bands": list(bands),
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
        "counts": dict(Counter(p.pool for p in positions)),
    }
    if "tactical" in pools:
        header["tactical"] = {
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
        }
    if "opening" in pools:
        header["opening"] = {
            "count": len(opening), "plies": list(OPENING_PLIES), "source": "public/data/openings/eco.json",
        }
    write_fixture(args.fixture, header, positions)
    logger.info(
        "wrote %s: %d positions, %d games scanned (%d read) in %.0f s",
        args.fixture, len(positions), build.games_scanned, build.games_read,
        time.monotonic() - started,
    )
    print(f"{name}: {dict(Counter(p.pool for p in positions))}")
    print_mover_summaries(natural_by_band(natural, bands))
    print(
        f"held out: {build.dropped_excluded} natural candidates already in an excluded fixture, "
        f"{build.dropped_repeated} repeating an earlier candidate, "
        f"{sum(opening_dropped)} opening lines; shared positions: 0"
    )
    return 0


def print_mover_summaries(by_band: dict[int, list[Position]]) -> None:
    """Per band: positions, distinct movers and how concentrated they are."""
    print("band  positions  movers  busiest mover  from movers seen more than once")
    for band, rows in by_band.items():
        summary = mover_summary(rows)
        if summary is None:
            print(f"{band:>5} {len(rows):>9}  no mover index: intervals resample positions")
            continue
        print(
            f"{band:>5} {summary['positions']:>9} {summary['movers']:>7} "
            f"{100 * summary['busiest_share']:>12.1f}% {100 * summary['repeated_share']:>12.0f}%"
        )


# ---------------------------------------------------------------------------
# --mover-index
# ---------------------------------------------------------------------------


def mover_index(
    games: Iterable[PgnGame],
    positions: Sequence[Position],
    bands: Sequence[int],
    seed: int,
    cap: int = CANDIDATE_CAP_PER_BAND,
) -> dict[int, list[int]]:
    """Per band, one integer per natural position in fixture order: its mover.

    For a fixture built before movers were stored. The stream must be the dump
    prefix the fixture was built from: each qualifying game's position is drawn
    as the builder drew it (same seed, same candidate cap) and matched to the
    fixture by its moves and the human's move. Names never leave this function.

    Raises:
        FixtureError: a natural position has no game in the stream.
    """
    by_band = natural_by_band(positions, bands)
    waiting: dict[int, dict[tuple[str, str | None], list[int]]] = {b: defaultdict(list) for b in bands}
    for band, rows in by_band.items():
        for index, position in enumerate(rows):
            waiting[band][(position.key, position.human)].append(index)
    names: dict[int, list[str | None]] = {band: [None] * len(by_band[band]) for band in bands}
    missing = sum(len(rows) for rows in by_band.values())
    candidates: Counter = Counter()
    for game_index, game in enumerate(games):
        if not missing:
            break
        band = qualify(game.headers, bands)
        if band is None or candidates[band] >= cap:
            continue
        candidate = candidate_from_game(game, band, seed, game_index)
        if candidate is None:
            continue
        candidates[band] += 1
        slots = waiting[band].get((" ".join(candidate.moves), candidate.human))
        if slots:
            names[band][slots.pop(0)] = candidate.mover
            missing -= 1
    if missing:
        short = {band: sum(name is None for name in names[band]) for band in bands}
        raise FixtureError(
            f"no game in the stream for {missing} natural positions (by band: "
            f"{ {b: n for b, n in short.items() if n} }); the stream must be the dump prefix "
            "the fixture was built from"
        )
    out: dict[int, list[int]] = {}
    for band in bands:
        index: dict[str, int] = {}
        out[band] = [index.setdefault(name, len(index)) for name in names[band]]
    return out


def write_movers(fixture_path: Path, header: dict, index: dict[int, list[int]]) -> Path:
    """The sidecar next to the fixture: integers only, one line per band."""
    fixture_path = Path(fixture_path)
    summaries = {}
    for band, movers in index.items():
        counts = Counter(movers)
        summaries[str(band)] = {
            "positions": len(movers),
            "movers": len(counts),
            "busiest_share": round(max(counts.values()) / len(movers), 4),
            "repeated_share": round(sum(c for c in counts.values() if c > 1) / len(movers), 4),
        }
    head = {
        "schema": MOVERS_SCHEMA,
        "description": (
            f"Mover index for {fixture_path.name} (g-maia3-elo-temp, decision 5). Per band, one "
            "integer per natural position in the fixture's order; positions of one band with the "
            "same integer have the same player to move. No names are stored. Re-derived from the "
            "dump prefix the fixture was built from."
        ),
        "fixture": fixture_path.name,
        "fixture_sha256": fixture_digest(fixture_path),
        "source_url": header.get("source_url"),
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "movers_by_band": summaries,
    }
    body = json.dumps(head, indent=2)[:-2]  # reopen the object for the index
    lines = ",\n".join(
        f'    "{band}": {json.dumps(movers, separators=(",", ":"))}' for band, movers in index.items()
    )
    path = movers_path(fixture_path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body + ',\n  "movers": {\n' + lines + "\n  }\n}\n")
    os.replace(tmp, path)
    return path


def run_mover_index(args: argparse.Namespace) -> int:
    header, positions = load_fixture(args.fixture)
    if any(p.mover is not None for p in positions):
        raise SystemExit(f"{args.fixture} already stores its movers")
    stream = sys.stdin.buffer if args.mover_index == "-" else open(args.mover_index, "rb")
    try:
        index = mover_index(iter_games(stream), positions, fixture_bands(header), header["seed"])
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
    path = write_movers(args.fixture, header, index)
    pool = load_pool(args.fixture, check=False)  # reads the sidecar back
    print(f"wrote {path}")
    print_mover_summaries(pool.natural())
    return 0


# ---------------------------------------------------------------------------
# --speed-counts: blitz against rapid and classical (g-maia3-elo-temp, decision 7)
# ---------------------------------------------------------------------------

SPEEDS = ("bullet", "blitz", "rapid", "classical")
# Lichess's speed by estimated duration (base + 40 x increment), slowest first.
_SPEED_FLOORS = ((1500, "classical"), (BLITZ_SECONDS[1] + 1, "rapid"), (BLITZ_SECONDS[0], "blitz"), (30, "bullet"))
_SERVER_EVAL = re.compile(r"\[%eval (#?)(-?[\d.]+)\]")


def speed_of(seconds: int | None) -> str | None:
    """Lichess's speed for an estimated duration; None for ultrabullet and for no clock."""
    if seconds is None:
        return None
    for floor, speed in _SPEED_FLOORS:
        if seconds >= floor:
            return speed
    return None


def server_eval_losses(movetext: str) -> list[float]:
    """Losses (cp) at the plies in PLY_RANGE, from the dump's own [%eval] comments.

    The natural pool's position rules: the mover had MIN_CLOCK_S left, and the
    position was not decided (no mate, within DECIDED_CP). Evals are White's.
    A mate counts as LOSS_CAP_CP and a loss is clamped to [0, LOSS_CAP_CP], as
    in the probe's CPL. Lichess analyses a minority of games, at the players'
    request, and not at the probe's depth: these numbers compare speeds with
    each other, not with the probe's.
    """
    evals: list[tuple[float, bool] | None] = []  # after each move: (cp, is a mate)
    clocks: list[float | None] = []
    for match in _TOKEN.finditer(movetext):
        comment, token = match.groups()
        if comment is not None:
            if not evals:
                continue
            found = _SERVER_EVAL.search(comment)
            if found:
                mate, number = found.groups()
                if mate:
                    evals[-1] = (float(-LOSS_CAP_CP if number.startswith("-") else LOSS_CAP_CP), True)
                else:
                    evals[-1] = (float(number) * 100, False)
            clock = _CLOCK.search(comment)
            if clock:
                hours, minutes, seconds = clock.groups()
                clocks[-1] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            continue
        if token in _RESULTS or token.startswith("$"):
            continue
        if _MOVE_NUMBER.sub("", token).rstrip("!?"):
            evals.append(None)
            clocks.append(None)
    low, high = PLY_RANGE
    losses = []
    for ply in range(low, min(high, len(evals) - 1) + 1):
        before, after = evals[ply - 1], evals[ply]
        if before is None or after is None:
            continue
        if before[1] or abs(before[0]) > DECIDED_CP:
            continue
        clock = clocks[ply - 2]
        if clock is None or clock < MIN_CLOCK_S:
            continue
        sign = 1 if ply % 2 == 0 else -1  # White is to move after an even number of half-moves
        loss = sign * (clamp_cp(before[0]) - clamp_cp(after[0]))
        losses.append(min(max(0.0, loss), float(LOSS_CAP_CP)))
    return losses


def speed_counts(
    games: Iterable[PgnGame],
    bands: Sequence[int],
    slope: float | None = None,
    max_games: int | None = None,
) -> dict:
    """Games per band and speed, and human loss by speed from the dump's own evals.

    The natural pool's filters with the speed left open. Per band and speed:
    the qualifying games, and over those Lichess analysed, the mean of the
    per-game mean losses. ``vs_blitz`` is ln(that speed's loss / blitz's loss)
    at the same rating number, with its standard error; given the human slope
    (ln CPL per Elo, negative) it is also in Elo, positive when players of that
    speed lose less than blitz players of the same number.
    """
    rated: Counter = Counter()
    qualifying: Counter = Counter()
    per_game: dict[tuple[int, str], list[float]] = defaultdict(list)
    moves: Counter = Counter()
    blunders: Counter = Counter()
    read = 0
    first = last = None
    for game in games:
        if max_games is not None and read >= max_games:
            break
        read += 1
        last = game_utc(game.headers)
        first = first or last
        speed = speed_of(rated_standard_seconds(game.headers))
        if speed is None:
            continue
        rated[speed] += 1
        band = players_band(game.headers, bands)
        if band is None:
            continue
        qualifying[band, speed] += 1
        if "%eval" not in game.movetext:
            continue
        losses = server_eval_losses(game.movetext)
        if losses:
            per_game[band, speed].append(mean(losses))
            moves[band, speed] += len(losses)
            blunders[band, speed] += sum(loss >= BLUNDER_CP for loss in losses)

    def cell(band: int, speed: str) -> dict:
        values = per_game[band, speed]
        out: dict = {"qualifying": qualifying[band, speed], "analysed": len(values)}
        if values:
            out["mean_cpl"] = mean(values)
            out["share_ge_200"] = blunders[band, speed] / moves[band, speed]
        if len(values) > 1:
            out["se"] = stdev(values) / math.sqrt(len(values))
        return out

    table = {band: {speed: cell(band, speed) for speed in SPEEDS} for band in bands}
    for row in table.values():
        blitz = row["blitz"]
        for speed in ("rapid", "classical"):
            other = row[speed]
            if "se" not in blitz or "se" not in other or min(blitz["mean_cpl"], other["mean_cpl"]) <= 0:
                continue
            log_ratio = math.log(other["mean_cpl"] / blitz["mean_cpl"])
            se = math.hypot(other["se"] / other["mean_cpl"], blitz["se"] / blitz["mean_cpl"])
            other["vs_blitz"] = {"log_ratio": log_ratio, "se": se}
            if slope:
                other["vs_blitz"].update(elo=log_ratio / slope, elo_se=se / abs(slope))
    return {
        "games_read": read,
        "utc_range": [first, last],
        "rated_standard": {speed: rated[speed] for speed in SPEEDS},
        "slope": slope,
        "bands": table,
    }


def _analysed_games(count: int) -> str:
    return f"{count} analysed game" + ("" if count == 1 else "s")


def print_speed_counts(report: dict) -> None:
    out = print
    first, last = report["utc_range"]
    out(f"# Blitz, rapid and classical: {report['games_read']} games read, UTC {first} to {last}")
    rated = report["rated_standard"]
    total = sum(rated.values()) or 1
    out("rated standard games by speed (ultrabullet and correspondence left out): " + ", ".join(
        f"{speed} {rated[speed]} ({100 * rated[speed] / total:.1f}%)" for speed in SPEEDS
    ))
    out(f"\n## Qualifying games (the natural pool's filters with the speed left open; both players within {BAND_HALF_WIDTH} of the band)")
    out("band   " + "".join(f"{speed:>11}" for speed in SPEEDS))
    for band, row in report["bands"].items():
        out(f"{band:>5}  " + "".join(f"{row[speed]['qualifying']:>11}" for speed in SPEEDS))
    low, high = PLY_RANGE
    out(
        f"\n## Human loss from the dump's own evals, plies {low}-{high}: "
        f"mean of per-game means, cp (SE; games; moves losing >= {BLUNDER_CP} cp)"
    )
    for band, row in report["bands"].items():
        cells = []
        for speed in ("blitz", "rapid", "classical"):
            c = row[speed]
            if "se" not in c:
                cells.append(f"{speed}: {_analysed_games(c['analysed'])}")
                continue
            cells.append(
                f"{speed}: {c['mean_cpl']:.1f} (SE {c['se']:.1f}; {c['analysed']}; {100 * c['share_ge_200']:.1f}%)"
            )
        out(f"{band:>5}  " + " | ".join(cells))
    slope = report["slope"]
    out(
        "\n## Against blitz players of the same rating number: the loss ratio"
        + (f", and in Elo over the slope {slope:.6f} (positive = loses less than blitz)" if slope else "")
    )
    for band, row in report["bands"].items():
        cells = []
        for speed in ("rapid", "classical"):
            versus = row[speed].get("vs_blitz")
            if versus is None:
                cells.append(f"{speed}: not measured ({_analysed_games(row[speed]['analysed'])})")
                continue
            text = f"{speed}: {100 * (math.exp(versus['log_ratio']) - 1):+.1f}% (SE {100 * versus['se']:.1f}%)"
            if "elo" in versus:
                text += f", {fmt_signed(versus['elo'])} Elo (SE {versus['elo_se']:.0f})"
            cells.append(text)
        out(f"{band:>5}  " + " | ".join(cells))
    out(
        "Only games that carry Lichess's own analysis count for the loss: a minority, chosen by the "
        "players, and not at the probe's Stockfish depth. Only the ratios carry over to the probe."
    )


def run_speed_counts(args: argparse.Namespace) -> int:
    stream = sys.stdin.buffer if args.speed_counts == "-" else open(args.speed_counts, "rb")
    try:
        report = speed_counts(iter_games(stream), args.bands, args.slope, args.max_games)
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()
    print_speed_counts(report)
    if args.json:
        Path(args.json).write_text(json.dumps(_jsonable(report), indent=2) + "\n")
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


@dataclass(frozen=True)
class Setting:
    """How the engine samples in one run: a flat temperature or a curve, and TopP.

    ``name`` is the cache key, and ``from_name`` reads it back. A flat setting
    is "T<t>", so 0 and 1 are the 2026-11-18 run's "T0" and "T1" and its moves
    are reused, with "P<p>" appended when TopP is not 1. A curve is "C:" plus
    its canonical string, so a revised curve never reuses another curve's
    moves. It is the only setting that runs with ``temperature=None``, the
    production path.
    """

    temperature: float | None = None
    curve: TemperatureCurve | None = None
    top_p: float = 1.0

    def __post_init__(self) -> None:
        if (self.temperature is None) == (self.curve is None):
            raise ValueError("a setting is a flat temperature or a curve, not both")
        if self.curve is not None and self.top_p != 1.0:
            raise ValueError("a curve setting has no TopP arm")

    @property
    def name(self) -> str:
        if self.curve is not None:
            return "C:" + format_temperature_curve(self.curve)
        name = f"T{self.temperature:g}"
        return name if self.top_p == 1.0 else f"{name}P{self.top_p:g}"

    @property
    def label(self) -> str:
        if self.curve is not None:
            return "curve " + format_temperature_curve(self.curve)
        return f"T={self.temperature:g}" + ("" if self.top_p == 1.0 else f" TopP={self.top_p:g}")

    @property
    def file_tag(self) -> str:
        """``name`` for a file name. A curve's string has ':' and ',', so it becomes a short digest;
        the file's own "setting" field holds the curve."""
        if self.curve is None:
            return self.name
        return "curve-" + hashlib.sha256(self.name.encode()).hexdigest()[:8]

    @classmethod
    def from_name(cls, name: str) -> Setting:
        if name.startswith("C:"):
            return cls(curve=parse_temperature_curve(name[2:]))
        if not name.startswith("T"):
            raise ValueError(f"unknown setting {name!r}")
        temperature, _, top_p = name[1:].partition("P")
        return cls(temperature=float(temperature), top_p=float(top_p) if top_p else 1.0)


def run_settings(model: str, setting: Setting, threads: int, weights_dir: Path) -> Maia3Settings:
    """Production service code, with the probe's overrides and roomy limits (CPU is shared)."""
    return Maia3Settings(
        enabled=True,
        model=model,
        weights_dir=weights_dir,
        temperature=setting.temperature,
        temperature_curve=setting.curve,
        top_p=setting.top_p,
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
    for task in todo:  # sorted by setting, so a worker restarts its engine once per setting
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
                        run_settings(model, Setting.from_name(task.setting), threads, weights_dir)
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
    """E = ln(M / H) / beta: positive when the setting plays stronger than its label.

    nan when a mean CPL is 0, and when the human slope is exactly 0: CPL then
    has no Elo scale, and the rule's guard reports the fit as unusable.
    """
    if maia <= 0 or human <= 0 or beta == 0:
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
                f"no {setting} move at {elo} sample {sample} for {position.key!r}; "
                "play the engine stage first (--run, --sweep, --ladder or --validate)"
            ) from None

    def loss(self, position: Position, move: str) -> float:
        try:
            best = self.evals[(position.key, "")][1]
            e_best = clamp_cp(self.evals[(position.key, best)][0])
            e_move = clamp_cp(self.evals[(position.key, move)][0])
        except KeyError:
            raise LookupError(
                f"move {move} of {position.key!r} is not scored; run the Stockfish stage first"
            ) from None
        return float(min(max(e_best - e_move, 0), LOSS_CAP_CP))

    def t0_loss(self, position: Position, elo: int) -> float:
        return self.loss(position, self.move(position, "T0", elo))

    def t1_losses(self, position: Position, elo: int, k: int) -> list[float]:
        return [self.loss(position, self.move(position, "T1", elo, s)) for s in range(k)]

    def losses(self, position: Position, setting: str, elo: int, k: int) -> list[float]:
        """The losses of the first ``k`` samples of a setting at ``elo``."""
        return [self.loss(position, self.move(position, setting, elo, s)) for s in range(k)]

    def distinct(self, position: Position, setting: str, elo: int, k: int) -> int:
        """How many different moves the first ``k`` samples hold."""
        return len({self.move(position, setting, elo, s) for s in range(k)})


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
        offsets = band_offsets(BANDS, h, {"e1": m1, "e0": m0})
        e1, e0 = offsets["e"]["e1"], offsets["e"]["e0"]
        return {
            "h": h, "m1": m1, "m0": m0, "alpha": offsets["alpha"], "beta": offsets["beta"],
            "residuals": offsets["residuals"], "e1": e1, "e0": e0,
            "d1": mean_abs_offset(e1, BANDS), "d0": mean_abs_offset(e0, BANDS),
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
    by_band = natural_by_band(positions)
    # Round-robin over the bands: every move changes Elo and, under a curve,
    # the temperature, as consecutive requests of different games do.
    sample = [by_band[band][index] for index in range(per_band) for band in BANDS]
    boards = [(presented_board(p), p.band) for p in sample]
    # MAIA3_TEMPERATURE, MAIA3_TEMPERATURE_CURVE and MAIA3_TOP_P as the API reads
    # them, so this measures the deployed sampling.
    deployed = maia3_settings()
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
                enabled=True, model=model, weights_dir=weights_dir,
                temperature=deployed.temperature, temperature_curve=deployed.temperature_curve,
                top_p=deployed.top_p,
                threads=threads, move_timeout_s=60.0, infer_timeout_s=120.0, start_timeout_s=180.0,
            )
            print(f"maia3 latency model={model} threads={threads} temperature: {settings.temperature_label()}", flush=True)
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
                "temperature": settings.temperature_label(),
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
# Temperature curve: pools and engine plans (g-maia3-elo-temp)
# ---------------------------------------------------------------------------


def fit_pools(args: argparse.Namespace) -> tuple[dict[int, list[Position]], list[Position], list[Pool]]:
    """The fit pool: natural positions per band from both fit fixtures, and the opening positions.

    Raises FixtureError if the two fixtures share a band or a position.
    """
    main, extra = load_pool(args.fixture), load_pool(args.fit_extra)
    if set(main.bands) & set(extra.bands):
        raise FixtureError(f"{main.name} and {extra.name} share bands")
    assert_disjoint([(main.name, main.positions), (extra.name, extra.positions)])
    natural = {**main.natural(), **extra.natural()}
    return {band: natural[band] for band in sorted(natural)}, main.opening(), [main, extra]


def validation_pools(args: argparse.Namespace) -> tuple[dict[int, list[Position]], list[Position], list[Pool]]:
    """The validation pool, after checking that no fit position is in it.

    Engine-free, and run before any engine starts. The validation fixture
    records the sha256 of every fixture it was built to exclude; a fit fixture
    that is not on that list, or has changed since, fails here.
    """
    _natural, _opening, fit = fit_pools(args)
    pool = load_pool(args.validation_fixture)
    recorded = {entry["file"]: entry["sha256"] for entry in pool.header.get("excluded_fixtures", [])}
    for fixture in fit:
        if recorded.get(fixture.name) != fixture.sha256:
            raise FixtureError(
                f"{pool.name} was not built with {fixture.name} (as it is on disk) excluded"
            )
    assert_disjoint([(p.name, p.positions) for p in [pool, *fit]])
    return pool.natural(), pool.opening(), [pool, *fit]


def band_sampling_tasks(
    setting: Setting, band: int, natural: Sequence[Position], opening: Sequence[Position]
) -> list[EngineTask]:
    """One setting at one band's Elo.

    K_H2 samples of each natural position; K_H1 on the band's first
    H1_PER_BAND and on the opening positions, the two pools variety is counted
    on. T=0 always plays the top move, so it is one move per natural position
    and its variety is 1 by definition.
    """
    name = setting.name
    if setting.temperature == 0:
        return [EngineTask(p, name, band, 0) for p in natural]
    tasks = [
        EngineTask(p, name, band, sample)
        for index, p in enumerate(natural)
        for sample in range(K_H1 if index < H1_PER_BAND else K_H2)
    ]
    tasks += [EngineTask(p, name, band, sample) for p in opening for sample in range(K_H1)]
    return tasks


def _unique(tasks: Iterable[EngineTask]) -> list[EngineTask]:
    return list({task.key: task for task in tasks}.values())


def plan_sweep_tasks(natural: dict[int, list[Position]], opening: Sequence[Position]) -> list[EngineTask]:
    """Every grid temperature at every band's Elo, and the TopP arms at their bands."""
    tasks: list[EngineTask] = []
    for band, rows in natural.items():
        settings = [Setting(temperature=t) for t in SWEEP_TEMPERATURES]
        if band in TOPP_BANDS:
            settings += [Setting(temperature=1.0, top_p=top_p) for top_p in TOPP_ARMS]
        for setting in settings:
            tasks += band_sampling_tasks(setting, band, rows, opening)
    return _unique(tasks)


def ladder_positions(natural: dict[int, list[Position]]) -> dict[int, list[Position]]:
    """The common positions every bot level plays: each band's first LADDER_PER_BAND.

    A fixture's order is a seeded shuffle per band, so these are a random
    LADDER_PER_BAND of the band's positions.
    """
    return {band: rows[:LADDER_PER_BAND] for band, rows in natural.items()}


def plan_ladder_tasks(natural: dict[int, list[Position]], setting: Setting) -> list[EngineTask]:
    return [
        EngineTask(p, setting.name, elo, sample)
        for rows in ladder_positions(natural).values()
        for p in rows
        for elo in ELO_BINS
        for sample in range(K_H2)
    ]


def variety_elos(bands: Iterable[int]) -> list[int]:
    """Where R3 counts variety on the opening pool: every band and every bot level."""
    return sorted(set(bands) | set(ELO_BINS))


def plan_validation_tasks(
    natural: dict[int, list[Position]], opening: Sequence[Position], curve: Setting
) -> list[EngineTask]:
    """The curve, T=1 and T=0 at each band's Elo, the curve on the opening pool at every level, and the ladder."""
    tasks: list[EngineTask] = []
    for band, rows in natural.items():
        tasks += band_sampling_tasks(curve, band, rows, opening)
        tasks += [EngineTask(p, "T1", band, sample) for p in rows for sample in range(K_H2)]
        tasks += [EngineTask(p, "T0", band, 0) for p in rows]
    for elo in variety_elos(natural):
        tasks += [EngineTask(p, curve.name, elo, sample) for p in opening for sample in range(K_H1)]
    tasks += plan_ladder_tasks(natural, curve)
    return _unique(tasks)


def natural_positions(tasks: Iterable[EngineTask]) -> list[Position]:
    """The positions that go to Stockfish: opening samples feed the variety count only."""
    return list({t.pos.key: t.pos for t in tasks if t.pos.pool == "natural"}.values())


def pilot_split(tasks: Sequence[EngineTask], per_group: int = PILOT_PER_BAND) -> tuple[list[EngineTask], dict[tuple, list[Position]]]:
    """A small share of a plan: ``per_group`` positions of every kind, with all their moves.

    Positions of one pool and band that get the same number of engine moves are
    one kind (a band's variety positions, its ladder positions, the rest).
    """
    per_position = Counter(t.pos.key for t in tasks)
    groups: dict[tuple, list[Position]] = defaultdict(list)
    for position in {t.pos.key: t.pos for t in tasks}.values():
        groups[(position.pool, position.band, per_position[position.key])].append(position)
    chosen = {p.key for rows in groups.values() for p in rows[:per_group]}
    return [t for t in tasks if t.pos.key in chosen], dict(groups)


def run_plan(args: argparse.Namespace, tasks: Sequence[EngineTask], what: str) -> None:
    """Play and score a plan (cached moves and evals are skipped), or only pilot it."""
    weights_dir = _weights_dir(args.model)
    cache = ProbeCache(args.cache)
    try:
        if not args.pilot:
            logger.info("%s: %d engine moves planned", what, len(tasks))
            stats = run_engine_tasks(tasks, cache, args.model, weights_dir, args.engines, args.threads)
            logger.info("engine stage done: %d moves in %.0f s", stats.done, stats.wall_s)
            if not args.no_cpl:
                cpl, _name = run_cpl(
                    natural_positions(tasks), cache, args.model, stockfish_path(args.stockfish), args.sf_workers
                )
                logger.info("cpl stage done: %d evals over %d positions in %.0f s", cpl.evals, cpl.positions, cpl.wall_s)
            return
        pilot_tasks, groups = pilot_split(tasks)
        piloted = {t.pos.key for t in pilot_tasks}
        engine = run_engine_tasks(pilot_tasks, cache, args.model, weights_dir, args.engines, args.threads)
        cached = cache.moves(args.model)
        remaining = sum(1 for t in tasks if t.key not in cached)
        lines = [f"pilot of {what}: {len(piloted)} positions of {len({t.pos.key for t in tasks})}, {len(pilot_tasks)} of {len(tasks)} engine moves"]
        engine_eta = None
        if engine.done:
            rate = engine.done / engine.wall_s
            engine_eta = remaining / rate
            lines.append(
                f"engine: {engine.done} moves in {engine.wall_s:.0f} s with {args.engines} engines = {rate:.1f} moves/s; "
                f"move p50 {median(engine.move_ms):.0f} ms, p95 {percentile(engine.move_ms, 0.95):.0f} ms (under load)"
            )
        else:
            lines.append("engine: all pilot moves were cached")
        lines.append(f"remaining engine moves: {remaining}")
        sf_eta = None
        if not args.no_cpl:
            scored = [p for p in natural_positions(pilot_tasks)]
            cpl, sf_name = run_cpl(scored, cache, args.model, stockfish_path(args.stockfish), args.sf_workers)
            if cpl.evals:
                sf_rate = cpl.evals / cpl.wall_s
                projected = 0.0
                for (pool, _band, _count), rows in groups.items():
                    if pool != "natural":
                        continue
                    done = [cpl.evals_by_position.get(p.key, 0) for p in rows if p.key in piloted]
                    projected += mean(done) * (len(rows) - len(done))
                sf_eta = projected / sf_rate
                lines.append(
                    f"stockfish ({sf_name}, depth {SF_DEPTH}): {cpl.evals} evals in {cpl.wall_s:.0f} s with "
                    f"{args.sf_workers} workers = {sf_rate:.2f} evals/s; about {projected:.0f} evals left"
                )
            else:
                lines.append("stockfish: all pilot evals were cached")
        if engine_eta is not None:
            lines.append(f"projected engine stage: {engine_eta / 3600:.2f} h")
        if sf_eta is not None:
            lines.append(f"projected Stockfish stage: {sf_eta / 3600:.2f} h")
        if engine_eta is not None and sf_eta is not None:
            lines.append(f"projected full run: {(engine_eta + sf_eta) / 3600:.2f} h wall clock (stages run one after the other)")
        for line in lines:
            print(line)
    finally:
        cache.close()


# ---------------------------------------------------------------------------
# Temperature curve: resampling by mover
# ---------------------------------------------------------------------------


@dataclass
class BandSample:
    """One band's positions as columns of per-position numbers, and who moved in each."""

    columns: dict
    movers: list[int | None]

    @property
    def size(self) -> int:
        return len(self.movers)

    def means(self) -> dict:
        return {name: mean(values) for name, values in self.columns.items()}

    def clusters(self, by_mover: bool) -> list[list[int]]:
        """The position indexes drawn together: one mover's positions, or single positions.

        Without a mover index every position is its own cluster.
        """
        if not by_mover or any(m is None for m in self.movers):
            return [[index] for index in range(self.size)]
        groups: dict[int, list[int]] = defaultdict(list)
        for index, mover in enumerate(self.movers):
            groups[mover].append(index)
        return list(groups.values())


def resample_sums(
    samples: dict[int, BandSample], resamples: int, rng: random.Random, by_mover: bool = True
) -> Iterator[dict[int, tuple[float, dict]]]:
    """Bootstrap resamples: per band, the number of positions drawn and each column's sum.

    Clusters are drawn with replacement within a band, as many as the band has
    (decision 5: a player's positions stay together). Every column of a band is
    read from the same draw, so differences between columns stay paired.
    """
    prepared = {}
    for band, sample in samples.items():
        clusters = sample.clusters(by_mover)
        sizes = [float(len(cluster)) for cluster in clusters]
        sums = {
            name: [math.fsum(values[i] for i in cluster) for cluster in clusters]
            for name, values in sample.columns.items()
        }
        prepared[band] = (sizes, sums)
    for _ in range(resamples):
        draw = {}
        for band, (sizes, sums) in prepared.items():
            counts = [0] * len(sizes)
            for index in rng.choices(range(len(sizes)), k=len(sizes)):
                counts[index] += 1
            draw[band] = (
                math.sumprod(counts, sizes),
                {name: math.sumprod(counts, column) for name, column in sums.items()},
            )
        yield draw


def resample_means(
    samples: dict[int, BandSample], resamples: int, rng: random.Random, by_mover: bool = True
) -> Iterator[dict[int, dict]]:
    for draw in resample_sums(samples, resamples, rng, by_mover):
        yield {band: {name: total / n for name, total in sums.items()} for band, (n, sums) in draw.items()}


def spread(point: float, by_movers: Sequence[float], by_positions: Sequence[float]) -> dict:
    """A statistic with its 95% interval (movers resampled) and its standard error both ways.

    When a resample has no value (an Elo offset under a human slope of 0), the
    interval and both errors are nan: a rule that reads them then fails.
    """
    if any(math.isnan(v) for v in (*by_movers, *by_positions)):
        return {"value": point, "interval": (math.nan, math.nan), "se_movers": math.nan, "se_positions": math.nan}
    return {
        "value": point,
        "interval": interval(by_movers),
        "se_movers": stdev(by_movers),
        "se_positions": stdev(by_positions),
    }


# ---------------------------------------------------------------------------
# Temperature curve: the fit
# ---------------------------------------------------------------------------


def pav_nondecreasing(values: Sequence[float]) -> list[float]:
    """The least-squares non-decreasing fit (pool-adjacent-violators, equal weights)."""
    return [-v for v in pav_nonincreasing([-v for v in values])]


def crossing_temperature(grid: Sequence[float], fitted: Sequence[float], target: float) -> float:
    """The highest temperature at which a non-decreasing fit is at or below ``target``.

    Linear between grid temperatures. The lowest grid temperature when even
    that one is above the target; the highest when it is still at or below it.
    """
    if fitted[0] > target:
        return float(grid[0])
    last = max(i for i in range(len(grid)) if fitted[i] <= target)
    if last == len(grid) - 1:
        return float(grid[-1])
    share = (target - fitted[last]) / (fitted[last + 1] - fitted[last])
    return grid[last] + share * (grid[last + 1] - grid[last])


def reaching_temperature(grid: Sequence[float], fitted: Sequence[float], target: float) -> float | None:
    """The lowest temperature at which a non-decreasing fit reaches ``target``; None if it never does."""
    if fitted[0] >= target:
        return float(grid[0])
    for i in range(1, len(grid)):
        if fitted[i] >= target:
            share = (target - fitted[i - 1]) / (fitted[i] - fitted[i - 1])
            return grid[i - 1] + share * (grid[i] - grid[i - 1])
    return None


def floor_temperature(grid: Sequence[float], variety_by_pool: dict[str, Sequence[float]]) -> dict:
    """The variety floor of one band (flatness rule, approved 2026-11-22).

    ``variety_by_pool`` holds, per pool, the mean number of distinct moves in
    K_H1 samples at each grid temperature (1 at T=0 by definition).

    1. Each pool's series gets a non-decreasing fit: true variety cannot fall as
       the temperature rises, so a dip is sampling noise.
    2. A pool's crossing is the lowest temperature at which its fit reaches
       VARIETY_TARGET, linear between grid temperatures.
    3. A crossing under the first sampled temperature counts as that
       temperature ("below the grid"): nothing is sampled under it, and a
       straight line from T=0 would put the floor too low.
    4. A pool still under the target at the last temperature makes the floor
       that temperature ("not reachable").
    5. The floor is the highest of the pools' values, so every pool reaches the
       target there.
    """
    first_sampled, last = float(grid[1]), float(grid[-1])
    pools = {}
    for name, raw in variety_by_pool.items():
        fitted = pav_nondecreasing(raw)
        crossing = reaching_temperature(grid, fitted, VARIETY_TARGET)
        below_grid = crossing is not None and crossing < first_sampled
        if crossing is None:
            value = last
        else:
            value = max(crossing, first_sampled)
        pools[name] = {
            "raw": list(raw), "fitted": fitted, "crossing": crossing, "floor": value,
            "below_grid": below_grid, "not_reachable": crossing is None,
        }
    floor = max(pool["floor"] for pool in pools.values())
    return {
        "floor": floor,
        # The flags describe the pool that sets the floor.
        "below_grid": any(p["below_grid"] and p["floor"] == floor for p in pools.values()),
        "not_reachable": any(p["not_reachable"] for p in pools.values()),
        "pools": pools,
    }


def anchor_temperature(t_star: float, floor: float) -> tuple[float, str]:
    """min(1, max(T* to the nearest 0.01, the floor rounded up to the next 0.01)), and what set it.

    Rounding the floor up means rounding never puts an anchor under a binding
    floor. The inner round() keeps a floor of exactly 0.35 at 0.35: 0.35 * 100
    is 35.00000000000001 in floating point.
    """
    crossing = round(t_star, 2)
    floor_up = math.ceil(round(floor * 100, 6)) / 100
    anchor = min(1.0, max(crossing, floor_up))
    if floor_up > crossing:
        return anchor, "floor"
    return anchor, "cap" if crossing >= 1.0 else "crossing"


def fit_band(
    grid: Sequence[float],
    cpl: Sequence[float],
    human: float,
    variety_by_pool: dict[str, Sequence[float]],
) -> dict:
    """One band's anchor from its mean CPL at each grid temperature and the humans' mean CPL.

    T* is the highest temperature at which the (non-decreasing) fit of the CPL
    loses no more than the humans, so E = 0 there whatever the human slope.
    """
    fitted = pav_nondecreasing(cpl)
    t_star = crossing_temperature(grid, fitted, human)
    floor = floor_temperature(grid, variety_by_pool)
    anchor, set_by = anchor_temperature(t_star, floor["floor"])
    bound = None
    if fitted[0] > human:
        bound = "even T=0 loses more than the humans"
    elif fitted[-1] <= human:
        bound = "T=1 loses no more than the humans"
    return {
        "human": human, "cpl": list(cpl), "cpl_fitted": fitted, "t_star": t_star, "bound": bound,
        "floor": floor, "anchor": anchor, "set_by": set_by,
    }


def band_offsets(bands: Sequence[int], h: dict[int, float], maia: dict[str, dict[int, float]]) -> dict:
    """Headline 2's arithmetic: the human fit over the bands, and each setting's Elo offsets.

    ln H = alpha + beta * Elo by least squares, then E = ln(M / H) / beta per
    band: positive when the setting plays stronger than its label.
    """
    alpha, beta, residuals = loglinear_fit(bands, [h[b] for b in bands])
    return {
        "alpha": alpha,
        "beta": beta,
        "residuals": dict(zip(bands, residuals)),
        "e": {name: {b: elo_offset(m[b], h[b], beta) for b in bands} for name, m in maia.items()},
    }


def mean_abs_offset(e: dict[int, float], bands: Sequence[int]) -> float:
    """D: the mean over the bands of |E|."""
    return mean([abs(e[b]) for b in bands])


def _variety(results: Results, positions: Sequence[Position], setting: str, elo: int) -> list[float]:
    return [float(results.distinct(p, setting, elo, K_H1)) for p in positions]


def build_fit(
    results: Results, natural: dict[int, list[Position]], opening: Sequence[Position]
) -> dict:
    """The anchors and the curve from the sweep (fit pool only; mechanical)."""
    bands = tuple(natural)
    grid = SWEEP_TEMPERATURES
    names = [Setting(temperature=t).name for t in grid]
    samples: dict[int, BandSample] = {}
    shares: dict[int, dict[str, float]] = {}
    for band, rows in natural.items():
        columns = {"human": [results.loss(p, p.human) for p in rows]}
        shares[band] = {}
        arms = [Setting(temperature=1.0, top_p=p).name for p in TOPP_ARMS] if band in TOPP_BANDS else []
        for name in names + arms:
            per_position = [results.losses(p, name, band, 1 if name == "T0" else K_H2) for p in rows]
            columns[name] = [mean(losses) for losses in per_position]
            flat = [v for losses in per_position for v in losses]
            shares[band][name] = sum(1 for v in flat if v >= BLUNDER_CP) / len(flat)
        samples[band] = BandSample(columns, [p.mover for p in rows])
    means = {band: sample.means() for band, sample in samples.items()}
    offsets = band_offsets(bands, {b: means[b]["human"] for b in bands}, {})
    beta = offsets["beta"]

    out_bands = {}
    for band, rows in natural.items():
        m = means[band]
        variety = {
            "natural": [1.0] + [mean(_variety(results, rows[:H1_PER_BAND], n, band)) for n in names[1:]],
            "opening": [1.0] + [mean(_variety(results, opening, n, band)) for n in names[1:]],
        }
        fit = fit_band(grid, [m[n] for n in names], m["human"], variety)

        def t_star(resampled: dict) -> float:
            fitted = pav_nondecreasing([resampled[n] for n in names])
            return crossing_temperature(grid, fitted, resampled["human"])

        draws = {
            by_mover: [
                t_star(r[band])
                for r in resample_means(
                    {band: samples[band]}, BOOTSTRAP_RESAMPLES,
                    random.Random(f"{FLATNESS_SEED}:fit:{band}:{by_mover}"), by_mover,
                )
            ]
            for by_mover in (True, False)
        }
        fit["t_star_spread"] = spread(fit["t_star"], draws[True], draws[False])
        fit["n"] = len(rows)
        fit["movers"] = mover_summary(rows)
        fit["e"] = [elo_offset(m[n], m["human"], beta) for n in names]
        fit["share_ge_200"] = [shares[band][n] for n in names]
        fit["human_share_ge_200"] = sum(1 for v in samples[band].columns["human"] if v >= BLUNDER_CP) / len(rows)
        if band in TOPP_BANDS:
            fit["top_p_arms"] = {
                arm: {
                    "cpl": m[arm],
                    "e": elo_offset(m[arm], m["human"], beta),
                    "share_ge_200": shares[band][arm],
                    "variety_natural": mean(_variety(results, rows[:H1_PER_BAND], arm, band)),
                    "variety_opening": mean(_variety(results, opening, arm, band)),
                }
                for arm in (Setting(temperature=1.0, top_p=p).name for p in TOPP_ARMS)
            }
        out_bands[band] = fit
    curve = tuple((band, out_bands[band]["anchor"]) for band in bands)
    Maia3Settings(temperature_curve=curve)  # the curve must pass the service's own validation
    return {
        "grid": list(grid),
        "alpha": offsets["alpha"],
        "beta": beta,
        "residuals": offsets["residuals"],
        "bands": out_bands,
        "curve": format_temperature_curve(curve),
    }


def print_fit(report: dict) -> None:
    out = print
    grid = report["grid"]
    out(f"# Temperature fit: {report['model']}, {report['engine']} depth {SF_DEPTH}")
    for name, digest in report["fixtures"].items():
        out(f"fixture {name} sha256 {digest}")
    out("shared positions between the fit fixtures: 0")
    out(
        f"human fit ln H = a + b*Elo: a={report['alpha']:.3f} b={report['beta']:.6f} "
        f"({100 * (math.exp(100 * report['beta']) - 1):+.1f}% CPL per 100 Elo); it converts CPL to Elo "
        "in the E column only, the anchors do not depend on it"
    )
    for band, fit in report["bands"].items():
        movers = fit["movers"]
        who = (
            f"{movers['movers']} movers, busiest {100 * movers['busiest_share']:.1f}%, "
            f"{100 * movers['repeated_share']:.0f}% of positions from movers seen more than once"
            if movers else "no mover index"
        )
        out(f"\n## Band {band}: {fit['n']} positions ({who})")
        out(f"humans: mean CPL {fit['human']:.1f}, {100 * fit['human_share_ge_200']:.1f}% of moves lose >= {BLUNDER_CP} cp")
        out(f"    T   mean CPL  isotonic      E   >={BLUNDER_CP}   distinct moves in {K_H1}: natural (fit)   opening (fit)")
        pools = fit["floor"]["pools"]
        for i, t in enumerate(grid):
            out(
                f"{t:5.2f} {fit['cpl'][i]:10.1f} {fit['cpl_fitted'][i]:9.1f} {fmt_signed(fit['e'][i]):>6} "
                f"{100 * fit['share_ge_200'][i]:6.1f}%   "
                f"{pools['natural']['raw'][i]:19.2f} ({pools['natural']['fitted'][i]:.2f})   "
                f"{pools['opening']['raw'][i]:7.2f} ({pools['opening']['fitted'][i]:.2f})"
            )
        s = fit["t_star_spread"]
        out(
            f"T* = {fit['t_star']:.3f} [{s['interval'][0]:.3f}, {s['interval'][1]:.3f}] "
            f"(SE {s['se_movers']:.3f} by movers, {s['se_positions']:.3f} by positions)"
            + (f"; at a bound: {fit['bound']}" if fit["bound"] else "")
        )
        floor = fit["floor"]
        crossings = ", ".join(
            f"{name} " + ("never" if p["crossing"] is None else f"{p['crossing']:.3f}")
            for name, p in floor["pools"].items()
        )
        flags = [text for flag, text in (
            (floor["below_grid"], "below the grid, counted as the first sampled temperature"),
            (floor["not_reachable"], f"NOT REACHABLE: a pool is under {VARIETY_TARGET:g} even at T=1"),
        ) if flag]
        out(f"variety floor = {floor['floor']:.3f} (reaches {VARIETY_TARGET:g} at: {crossings})" + "".join(f"; {f}" for f in flags))
        out(f"anchor = {fit['anchor']:.2f}, set by the {fit['set_by']}")
        for arm, row in fit.get("top_p_arms", {}).items():
            out(
                f"report only, {arm}: mean CPL {row['cpl']:.1f}, E {fmt_signed(row['e'])}, "
                f"{100 * row['share_ge_200']:.1f}% >= {BLUNDER_CP}, distinct moves natural "
                f"{row['variety_natural']:.2f}, opening {row['variety_opening']:.2f}"
            )
    out(f"\ncurve: {report['curve']}")


def run_sweep(args: argparse.Namespace) -> int:
    natural, opening, _pools = fit_pools(args)
    run_plan(args, plan_sweep_tasks(natural, opening), "the sweep")
    return 0


def _results(args: argparse.Namespace, cache: ProbeCache) -> tuple[Results, str]:
    engine = args.sf_engine or _cached_engine_name(cache)
    return Results(cache, args.model, engine), engine


def _write_json(args: argparse.Namespace, report: dict, default_name: str) -> None:
    out = args.json or PROBE_DIR / default_name
    Path(out).write_text(json.dumps(_jsonable(report), indent=2) + "\n")
    logger.info("wrote %s", out)


def run_fit(args: argparse.Namespace) -> int:
    natural, opening, pools = fit_pools(args)
    cache = ProbeCache(args.cache)
    try:
        results, engine = _results(args, cache)
        report = build_fit(results, natural, opening)
    finally:
        cache.close()
    report.update(model=args.model, engine=engine, fixtures={p.name: p.sha256 for p in pools})
    print_fit(report)
    _write_json(args, report, f"fit-{args.model}.json")
    return 0


# ---------------------------------------------------------------------------
# Temperature curve: the ladder (R4)
# ---------------------------------------------------------------------------


def ladder_samples(
    results: Results, natural: dict[int, list[Position]], setting: str, levels: Sequence[int] = ELO_BINS
) -> dict[int, BandSample]:
    """Each band's ladder positions: one column per level, the mean loss of its K_H2 samples."""
    return {
        band: BandSample(
            {level: [mean(results.losses(p, setting, level, K_H2)) for p in rows] for level in levels},
            [p.mover for p in rows],
        )
        for band, rows in ladder_positions(natural).items()
    }


def ladder_gaps(
    samples: dict[int, BandSample],
    levels: Sequence[int],
    rng: random.Random,
    resamples: int | None = None,
    confidence: float = LADDER_CONFIDENCE,
    by_mover: bool = True,
) -> dict:
    """R4's arithmetic: is some level clearly worse than a lower one?

    L(e) is level e's mean CPL over the common positions. For each pair of
    levels, d = ln(L(higher) / L(lower)); above 0 the higher level loses more.
    Each resample draws movers within every band once and reads every level
    from that draw, so the comparison stays paired. s is the spread of a pair's
    d over the resamples, and c the ``confidence`` quantile, over the
    resamples, of the largest (d_resample - d) / s among the pairs: every
    pair's lower bound d - c * s then holds together with that confidence. One
    pair alone gives its ordinary one-sided bound; more pairs give a larger c.

    A resample in which some level lost nothing has no ln for that level. It is
    left out for every pair, so each pair's s and the maximum read the same
    draws, and it is counted (``resamples_unusable``).

    A pair whose s is 0 and whose d is 0 is two levels that played the same
    moves everywhere; it is left out of the maximum and cannot fail. A pair
    whose s is 0 while its d is not has a gap that no resample moved: that is
    a pool too small to say how uncertain the gap is, not a certain gap. Then,
    and when fewer than two resamples are usable, there is no verdict
    (ValueError) rather than a reversal reported without its uncertainty.
    """
    resamples = LADDER_RESAMPLES if resamples is None else resamples
    levels = sorted(levels)
    column = {level: i for i, level in enumerate(levels)}
    positions = sum(sample.size for sample in samples.values())
    totals = [math.fsum(math.fsum(sample.columns[level]) for sample in samples.values()) for level in levels]
    if min(totals) <= 0:
        raise ValueError("a level lost nothing on the ladder positions; ln of its CPL is undefined")
    logs = [math.log(total) for total in totals]
    draws, unusable = [], 0
    for draw in resample_sums(samples, resamples, rng, by_mover):
        sums = [math.fsum(band_sums[level] for _n, band_sums in draw.values()) for level in levels]
        if min(sums) <= 0:
            unusable += 1
            continue
        draws.append([math.log(total) for total in sums])
    if len(draws) < 2:
        raise ValueError(
            f"a level lost nothing in {unusable} of {resamples} ladder resamples and {len(draws)} are left: "
            "the ladder's uncertainty cannot be estimated, so it has no verdict (the pool is too small)"
        )
    pairs = [(low, high) for i, low in enumerate(levels) for high in levels[i + 1:]]
    d, s, deviations = {}, {}, {}
    for low, high in pairs:
        i, j = column[low], column[high]
        d[low, high] = logs[j] - logs[i]
        resampled = [row[j] - row[i] for row in draws]
        deviation = stdev(resampled)
        # Equal gaps can differ in the last bit; real spreads are around 0.03.
        s[low, high] = deviation if deviation > 1e-12 else 0.0
        if not s[low, high] and d[low, high]:
            raise ValueError(
                f"levels {low} and {high} differ (d = {d[low, high]:+.3f}) but their gap is the same in all "
                f"{len(draws)} usable resamples ({unusable} of {resamples} had a level that lost nothing): "
                "its uncertainty cannot be estimated, so the ladder has no verdict (the pool is too small)"
            )
        deviations[low, high] = resampled
    live = [pair for pair in pairs if s[pair] > 0]
    c = 0.0
    if live:
        largest = [
            max((deviations[pair][r] - d[pair]) / s[pair] for pair in live) for r in range(len(draws))
        ]
        c = percentile(largest, confidence)
    gaps = [
        {"lower": low, "higher": high, "d": d[low, high], "s": s[low, high], "bound": d[low, high] - c * s[low, high]}
        for low, high in pairs
    ]
    return {
        "positions": positions,
        "levels": levels,
        "mean_cpl": {level: totals[column[level]] / positions for level in levels},
        "c": c,
        "resamples": resamples,
        "resamples_unusable": unusable,
        "confidence": confidence,
        "gaps": gaps,
        "reversed": [[g["lower"], g["higher"]] for g in gaps if g["bound"] > 0],
    }


def measure_ladder(results: Results, natural: dict[int, list[Position]], setting: str) -> dict:
    """R4 on a pool's ladder positions, plus what resampling movers changed.

    The verdict resamples movers. The same ladder resampled by single positions
    gives the other standard error of every gap; their ratio is reported.
    """
    samples = ladder_samples(results, natural, setting)
    ladder = ladder_gaps(samples, ELO_BINS, random.Random(f"{FLATNESS_SEED}:ladder"))
    by_positions = ladder_gaps(
        samples, ELO_BINS, random.Random(f"{FLATNESS_SEED}:ladder:positions"), by_mover=False
    )
    ratios = sorted(
        m["s"] / q["s"] for m, q in zip(ladder["gaps"], by_positions["gaps"]) if q["s"] > 0
    )
    ladder["se_movers_over_positions"] = (
        {"median": median(ratios), "min": ratios[0], "max": ratios[-1]} if ratios else None
    )
    return ladder


def human_slope(results: Results, natural: dict[int, list[Position]]) -> float:
    """beta of ln H = alpha + beta * Elo over the pool's bands (negative: stronger players lose less)."""
    bands = tuple(natural)
    h = {band: mean([results.loss(p, p.human) for p in rows]) for band, rows in natural.items()}
    return band_offsets(bands, h, {})["beta"]


def print_ladder(ladder: dict, beta: float, title: str) -> None:
    """The ladder in Elo. A reversal is d / |beta|: how much weaker the higher level plays.

    The verdict is taken on d itself. With a human slope of exactly 0 there is
    no Elo scale: the sizes print as nan and the verdict still stands.
    """
    out = print
    levels = ladder["levels"]
    scale = abs(beta)

    def elo(log_gap: float) -> float:
        return log_gap / scale if scale else math.nan

    out(f"\n## {title}")
    out(
        f"{ladder['positions']} common positions, {K_H2} samples per level; {ladder['resamples']} resamples; "
        f"c = {ladder['c']:.2f} (an ordinary one-sided 95% bound uses 1.64)"
    )
    if ladder.get("resamples_unusable"):
        out(
            f"NOTE: {ladder['resamples_unusable']} of {ladder['resamples']} resamples had a level that lost "
            "nothing and were left out of every pair; the pool is too small for this ladder"
        )
    if not scale:
        out("NOTE: the human slope is 0, so a gap has no size in Elo (nan below); the verdict does not use the slope")
    ratio = ladder.get("se_movers_over_positions")
    if ratio:
        out(
            f"standard error of a gap, resampling movers over resampling positions: median x{ratio['median']:.2f} "
            f"(x{ratio['min']:.2f} to x{ratio['max']:.2f} over the pairs)"
        )
    out("level  mean CPL")
    for level in levels:
        out(f"{level:>5} {ladder['mean_cpl'][level]:9.1f}")
    by_pair = {(g["lower"], g["higher"]): g for g in ladder["gaps"]}
    for heading, value in (
        ("Measured reversal in Elo (row = lower level, column = higher; above 0 the higher level plays weaker)",
         lambda g: elo(g["d"])),
        ("Simultaneous lower bound of the reversal in Elo (R4 fails where one is above 0)",
         lambda g: elo(g["bound"])),
    ):
        out(f"\n{heading}")
        out("      " + " ".join(f"{b:>5}" for b in levels[1:]))
        for low in levels[:-1]:
            cells = [f"{value(by_pair[low, high]):5.0f}" if high > low else "     " for high in levels[1:]]
            out(f"{low:>5} " + " ".join(cells))
    largest = sorted(ladder["gaps"], key=lambda g: g["d"], reverse=True)[:5]
    out("\nThe five largest measured reversals (whatever the verdict):")
    for g in largest:
        out(
            f"  {g['higher']} against {g['lower']}: {elo(g['d']):+.0f} Elo, "
            f"standard error {elo(g['s']):.0f}, simultaneous lower bound {elo(g['bound']):+.0f}"
        )
    if ladder["reversed"]:
        out("clear reversals: " + ", ".join(f"{high} plays below {low}" for low, high in ladder["reversed"]))
    else:
        out("clear reversals: none")


def _setting_from_args(args: argparse.Namespace, need_curve: bool = False) -> Setting:
    if args.curve is not None and args.temperature is None:
        setting = Setting(curve=parse_temperature_curve(args.curve))
        Maia3Settings(temperature_curve=setting.curve)  # fail on a bad curve before any engine starts
        return setting
    if need_curve or args.curve is not None or args.temperature is None:
        raise SystemExit("pass --curve <elo:T,...>" + ("" if need_curve else " or --temperature <T>, not both"))
    return Setting(temperature=args.temperature)


def run_ladder(args: argparse.Namespace) -> int:
    setting = _setting_from_args(args)
    natural, _opening, pools = (validation_pools if args.pool == "validation" else fit_pools)(args)
    run_plan(args, plan_ladder_tasks(natural, setting), f"the {args.pool}-pool ladder")
    if args.pilot:
        return 0
    cache = ProbeCache(args.cache)
    try:
        results, engine = _results(args, cache)
        ladder = measure_ladder(results, natural, setting.name)
        beta = human_slope(results, natural)
    finally:
        cache.close()
    print(f"# Ladder: {args.model}, {setting.label}, the {args.pool} pool, {engine} depth {SF_DEPTH}")
    print("shared positions between the fixtures: 0")
    print_mover_summaries(ladder_positions(natural))
    print_ladder(ladder, beta, "All 16 bot levels on the same positions")
    if args.pool == "fit":
        print("The fit pool is not the validation pool: this is for the fit gate, and no rule is applied to it.")
    report = {
        "model": args.model, "engine": engine, "setting": setting.name, "pool": args.pool,
        "fixtures": {p.name: p.sha256 for p in pools}, "beta": beta, "ladder": ladder,
    }
    _write_json(args, report, ladder_json_name(args.pool, args.model, setting))
    return 0


def ladder_json_name(pool: str, model: str, setting: Setting) -> str:
    """One file per setting: the T=1 ladder that follows a failed R4 must not replace the curve's."""
    return f"ladder-{pool}-{model}-{setting.file_tag}.json"


# ---------------------------------------------------------------------------
# Temperature curve: the flatness rule and --validate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlatnessDecision:
    guard: bool
    r1: bool
    r2: bool
    r3: bool
    r4: bool
    off_bands: tuple[int, ...]
    short_variety: tuple[str, ...]
    reversed_pairs: tuple[tuple[int, int], ...]
    outcome: str
    recommendation: str


def decide_flatness(
    *,
    beta_upper: float,
    d_curve: float,
    d_t1: float,
    share_flatter: float,
    e_curve_intervals: dict[int, tuple[float, float]],
    variety_uppers: dict[str, float],
    reversed_pairs: Sequence[Sequence[int]],
) -> FlatnessDecision:
    """The flatness rule approved on 2026-11-22. It must not change after the sweep starts.

    Guard: the human slope's 95% interval is below 0, or the Elo offsets mean nothing.
    R1: the curve is flatter than T=1 (D_c < D_1), and in at least R1_SHARE of the resamples.
    R2: every band's 95% interval for E_c reaches inside +-R2_BAND_ELO. A screen
        for gross error, not a certificate of rating accuracy.
    R3: every 95% interval for the mean number of distinct moves reaches
        VARIETY_TARGET. A warning against a bot that repeats itself.
    R4: no pair of levels is clearly reversed on the ladder (``ladder_gaps``).

    All hold: adopt. The guard or R1 fails: keep T=1. Only R2, R3 or R4 fails:
    the user decides; nothing is adopted meanwhile.
    """
    guard = beta_upper < 0
    r1 = d_curve < d_t1 and share_flatter >= R1_SHARE
    off_bands = tuple(
        band for band, (low, high) in e_curve_intervals.items()
        if not (high > -R2_BAND_ELO and low < R2_BAND_ELO)
    )
    short_variety = tuple(label for label, upper in variety_uppers.items() if not upper >= VARIETY_TARGET)
    pairs = tuple((low, high) for low, high in reversed_pairs)
    r2, r3, r4 = not off_bands, not short_variety, not pairs
    if not guard:
        outcome = "the human fit is unusable"
        recommendation = (
            "none: the human slope's 95% interval reaches 0, so the Elo offsets mean nothing; "
            "keep T=1 and check the human pool"
        )
    elif not r1:
        outcome = "R1 fails"
        recommendation = "keep T=1: the curve is not clearly flatter, so production does not change"
    elif r2 and r3 and r4:
        outcome = "all hold"
        recommendation = "adopt: the curve becomes the default for the model it was measured on"
    else:
        failed = [name for name, holds in (("R2", r2), ("R3", r3), ("R4", r4)) if not holds]
        outcome = "only " + ", ".join(failed) + (" fails" if len(failed) == 1 else " fail")
        recommendation = (
            "nothing is adopted until the user decides between the curve as measured and keeping "
            "T=1; a re-fit needs a new validation pool"
            + ("; run --ladder --pool validation --temperature 1 first, for T=1 on the same positions" if not r4 else "")
        )
    return FlatnessDecision(guard, r1, r2, r3, r4, off_bands, short_variety, pairs, outcome, recommendation)


def variety_spread(results: Results, positions: Sequence[Position], setting: str, elo: int, seed: str) -> dict:
    """The mean number of distinct moves in K_H1 samples, with its interval (movers where known)."""
    values = _variety(results, positions, setting, elo)
    sample = {0: BandSample({"v": values}, [p.mover for p in positions])}
    draws = {
        by_mover: [
            r[0]["v"]
            for r in resample_means(sample, BOOTSTRAP_RESAMPLES, random.Random(f"{seed}:{by_mover}"), by_mover)
        ]
        for by_mover in (True, False)
    }
    return {"n": len(values), **spread(mean(values), draws[True], draws[False])}


def build_validation(
    results: Results, natural: dict[int, list[Position]], opening: Sequence[Position], curve: Setting
) -> dict:
    """Everything the flatness rule reads, from the validation pool only."""
    bands = tuple(natural)
    settings = {"curve": curve.name, "t1": "T1", "t0": "T0"}
    samples = {
        band: BandSample(
            {
                "human": [results.loss(p, p.human) for p in rows],
                "curve": [mean(results.losses(p, curve.name, band, K_H2)) for p in rows],
                "t1": [mean(results.losses(p, "T1", band, K_H2)) for p in rows],
                "t0": [results.losses(p, "T0", band, 1)[0] for p in rows],
            },
            [p.mover for p in rows],
        )
        for band, rows in natural.items()
    }

    def estimate(means: dict[int, dict]) -> dict | None:
        h = {b: means[b]["human"] for b in bands}
        m = {name: {b: means[b][name] for b in bands} for name in settings}
        if min(h.values()) <= 0 or min(v for row in m.values() for v in row.values()) <= 0:
            return None  # somebody lost nothing at all: ln of a mean CPL of 0 is undefined
        offsets = band_offsets(bands, h, m)
        d = {name: mean_abs_offset(offsets["e"][name], bands) for name in settings}
        return {"h": h, "m": m, **offsets, "d": d, "improvement": d["t1"] - d["curve"]}

    point = estimate({band: sample.means() for band, sample in samples.items()})
    if point is None:
        raise ValueError("a band's mean CPL is 0 for the humans or for a setting; the offsets are undefined")
    boots = {}
    for by_mover in (True, False):
        draws = resample_means(
            samples, BOOTSTRAP_RESAMPLES, random.Random(f"{FLATNESS_SEED}:validate:{by_mover}"), by_mover
        )
        # A resample with a mean CPL of 0 has no offsets. That takes a pool far
        # smaller than 1,000 positions per band; the report says how many.
        boots[by_mover] = [usable for r in draws if (usable := estimate(r)) is not None]
        if not boots[by_mover]:
            raise ValueError("no bootstrap resample has a positive mean CPL in every band")

    def of(pick: Callable[[dict], float]) -> dict:
        return spread(pick(point), [pick(r) for r in boots[True]], [pick(r) for r in boots[False]])

    out_bands = {}
    for band, rows in natural.items():
        out_bands[band] = {
            "n": len(rows),
            "movers": mover_summary(rows),
            "h": point["h"][band],
            "residual": point["residuals"][band],
            **{f"m_{name}": point["m"][name][band] for name in settings},
            **{f"e_{name}": of(lambda r, name=name, band=band: r["e"][name][band]) for name in settings},
        }
    variety = {
        f"natural {band}": variety_spread(
            results, rows[:H1_PER_BAND], curve.name, band, f"{FLATNESS_SEED}:variety:natural:{band}"
        )
        for band, rows in natural.items()
    }
    variety.update({
        f"opening {elo}": variety_spread(
            results, opening, curve.name, elo, f"{FLATNESS_SEED}:variety:opening:{elo}"
        )
        for elo in variety_elos(bands)
    })
    ladder = measure_ladder(results, natural, curve.name)
    beta = of(lambda r: r["beta"])
    share = sum(r["d"]["curve"] < r["d"]["t1"] for r in boots[True]) / len(boots[True])
    decision = decide_flatness(
        beta_upper=beta["interval"][1],
        d_curve=point["d"]["curve"],
        d_t1=point["d"]["t1"],
        share_flatter=share,
        e_curve_intervals={band: out_bands[band]["e_curve"]["interval"] for band in bands},
        variety_uppers={label: row["interval"][1] for label, row in variety.items()},
        reversed_pairs=ladder["reversed"],
    )
    return {
        "curve": format_temperature_curve(curve.curve),
        "alpha": point["alpha"],
        "beta": beta,
        "bands": out_bands,
        "d": {name: point["d"][name] for name in settings},
        "noise_floor": {
            name: mean([FOLDED_NORMAL * out_bands[b][f"e_{name}"]["se_movers"] for b in bands]) for name in settings
        },
        "share_flatter": share,
        "resamples": BOOTSTRAP_RESAMPLES,
        "resamples_unusable": BOOTSTRAP_RESAMPLES - len(boots[True]),
        "improvement": of(lambda r: r["improvement"]),
        "variety": variety,
        "ladder": ladder,
        "decision": decision.__dict__,
    }


def _spread_text(s: dict, digits: int = 0) -> str:
    low, high = s["interval"]
    if digits:
        return f"{s['value']:.{digits}f} [{low:.{digits}f}, {high:.{digits}f}]"
    return f"{fmt_signed(s['value'])} [{fmt_signed(low)}, {fmt_signed(high)}]"


def print_validation(report: dict) -> None:
    out = print
    out(f"# Flatness check: {report['model']}, {report['engine']} depth {SF_DEPTH}")
    out(f"curve {report['curve']}")
    for name, digest in report["fixtures"].items():
        out(f"fixture {name} sha256 {digest}")
    out("shared positions between the validation fixture and the fit fixtures: 0")
    out("\n## Who is behind the positions")
    out("band  positions  movers  busiest mover  from movers seen more than once")
    for band, row in report["bands"].items():
        m = row["movers"]
        if m is None:
            out(f"{band:>5} {row['n']:>9}  no mover index: intervals resample positions")
        else:
            out(f"{band:>5} {m['positions']:>9} {m['movers']:>7} {100 * m['busiest_share']:>12.1f}% {100 * m['repeated_share']:>12.0f}%")

    out("\n## Against Lichess blitz players of the band (mean CPL)")
    out("band      H  curve    T=1    T=0")
    for band, row in report["bands"].items():
        out(f"{band:>5} {row['h']:6.1f} {row['m_curve']:6.1f} {row['m_t1']:6.1f} {row['m_t0']:6.1f}")
    beta = report["beta"]
    out(
        f"fit ln H = a + b*Elo: a={report['alpha']:.3f} b={beta['value']:.6f} "
        f"[{beta['interval'][0]:.6f}, {beta['interval'][1]:.6f}] "
        f"(SE {beta['se_movers']:.6f} by movers, {beta['se_positions']:.6f} by positions; "
        f"{100 * (math.exp(100 * beta['value']) - 1):+.1f}% CPL per 100 Elo)"
    )
    out("residuals (ln H - fit): " + ", ".join(
        f"{b}: {100 * (math.exp(r['residual']) - 1):+.1f}%" for b, r in report["bands"].items()
    ))
    out("\nElo offset E (positive = stronger than the label); 95% interval by movers; SE by movers / by positions")
    out("band   curve                    T=1                      T=0")
    for band, row in report["bands"].items():
        cells = [
            f"{_spread_text(row[key]):<18} {row[key]['se_movers']:3.0f}/{row[key]['se_positions']:<3.0f}"
            for key in ("e_curve", "e_t1", "e_t0")
        ]
        out(f"{band:>5}  " + "  ".join(cells))
    d, floor = report["d"], report["noise_floor"]
    out(
        f"\nD (mean |E| over the bands): curve {d['curve']:.0f} Elo (noise floor {floor['curve']:.0f}); "
        f"T=1 {d['t1']:.0f} (noise floor {floor['t1']:.0f}); T=0 {d['t0']:.0f}"
    )
    imp = report["improvement"]
    out(
        f"improvement D_1 - D_c: {_spread_text(imp)} Elo "
        f"(SE {imp['se_movers']:.0f} by movers, {imp['se_positions']:.0f} by positions); "
        f"the curve is flatter than T=1 in {100 * report['share_flatter']:.1f}% of resamples"
    )
    if report["resamples_unusable"]:
        out(
            f"NOTE: {report['resamples_unusable']} of {report['resamples']} resamples had a mean CPL of 0 "
            "in some band and were left out; the pool is too small for these intervals"
        )

    out(
        f"\n## Distinct moves in {K_H1} samples under the curve "
        f"(mean, 95% interval, SE by movers / by positions; * = mean under {VARIETY_TARGET:g})"
    )
    for label, row in report["variety"].items():
        mark = " *" if row["value"] < VARIETY_TARGET else ""
        out(
            f"{label:<14} n={row['n']:<4} {_spread_text(row, 2)} "
            f"{row['se_movers']:.2f}/{row['se_positions']:.2f}{mark}"
        )

    print_ladder(report["ladder"], beta["value"], "R4: all 16 bot levels on the same positions")

    dec = report["decision"]
    out("\n## Flatness rule (approved 2026-11-22)")
    out("guard (the human slope's 95% interval below 0): " + ("holds" if dec["guard"] else "FAILS"))
    out(f"R1 (flatter than T=1, in at least {100 * R1_SHARE:.0f}% of resamples): " + ("holds" if dec["r1"] else "fails"))
    out(
        f"R2 (every band's interval for E reaches inside +-{R2_BAND_ELO:.0f}; a screen for gross error): "
        + ("holds" if dec["r2"] else f"fails at {list(dec['off_bands'])}")
    )
    out(
        f"R3 (every variety interval reaches {VARIETY_TARGET:g}; a warning against repetition): "
        + ("holds" if dec["r3"] else f"fails at {list(dec['short_variety'])}")
    )
    out(
        "R4 (no level clearly below a lower one): "
        + ("holds" if dec["r4"] else "fails: " + ", ".join(f"{high} below {low}" for low, high in dec["reversed_pairs"]))
    )
    out(f"outcome: {dec['outcome']}. Recommendation: {dec['recommendation']}.")
    out(
        "How to word a pass: the curve brings the bot closer to Lichess blitz players than T=1 does, by the "
        "improvement above. It does not show that a level is within 150 Elo of its label, or that its play is human-like."
    )


def run_validate(args: argparse.Namespace) -> int:
    curve = _setting_from_args(args, need_curve=True)
    natural, opening, pools = validation_pools(args)  # before any engine starts
    run_plan(args, plan_validation_tasks(natural, opening, curve), "the validation run")
    if args.pilot:
        return 0
    cache = ProbeCache(args.cache)
    try:
        results, engine = _results(args, cache)
        report = build_validation(results, natural, opening, curve)
    finally:
        cache.close()
    report.update(model=args.model, engine=engine, fixtures={p.name: p.sha256 for p in pools})
    print_validation(report)
    _write_json(args, report, f"validation-{args.model}.json")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    cpus = os.cpu_count() or 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--build-pool", metavar="PGN", help="decompressed Lichess PGN ('-' for stdin)")
    mode.add_argument("--mover-index", metavar="PGN", help="write the mover sidecar of --fixture from the dump prefix it was built from ('-' for stdin)")
    mode.add_argument("--speed-counts", metavar="PGN", help="games per band and speed, and rapid and classical loss against blitz, from a dump's own evals ('-' for stdin)")
    mode.add_argument("--run", action="store_true", help="the full run (resumable)")
    mode.add_argument("--report", action="store_true", help="tables, both headlines, the decision rule")
    mode.add_argument("--sweep", action="store_true", help="the temperature grid on the fit pool (resumable)")
    mode.add_argument("--fit", action="store_true", help="the anchors and the curve from the sweep")
    mode.add_argument("--ladder", action="store_true", help="all 16 bot levels on the same positions, under --curve or --temperature")
    mode.add_argument("--validate", action="store_true", help="the flatness rule for --curve on the validation pool")
    mode.add_argument("--latency", action="store_true", help="cold start, per-move latency, child memory")
    parser.add_argument(
        "--pilot", action="store_true",
        help="alone: 20 positions per stage of --run; with --sweep, --ladder or --validate: "
        "a small share of that run. Prints the projected wall clock",
    )
    parser.add_argument("--model", default=os.environ.get("MAIA3_MODEL", "maia3-23m"), choices=sorted(maia3_pins.MODEL_PINS))
    parser.add_argument("--threads", type=int, default=1, help="engine threads for --pilot/--run")
    parser.add_argument("--engines", type=int, default=max(1, cpus - 2), help="parallel engines for --pilot/--run")
    parser.add_argument("--sf-workers", type=int, default=max(1, cpus - 2), help="parallel Stockfish processes")
    parser.add_argument("--stockfish", help="Stockfish binary (default: STOCKFISH_PATH, then PATH)")
    parser.add_argument("--sf-engine", help="--report: the Stockfish id name whose evals to use")
    parser.add_argument("--no-cpl", action="store_true", help="--pilot/--run: engine moves only, no Stockfish")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH, help="the fixture to read, or the one --build-pool writes")
    parser.add_argument("--fit-extra", type=Path, default=FIT_EXTRA_PATH, help="the fit pool's second fixture")
    parser.add_argument("--validation-fixture", type=Path, default=VALIDATION_PATH)
    parser.add_argument("--bands", type=lambda s: tuple(int(x) for x in s.split(",")), default=BANDS, help="--build-pool/--speed-counts: the bands, e.g. 1300,2300")
    parser.add_argument("--pools", type=lambda s: tuple(s.split(",")), default=POOLS, help="--build-pool: natural[,tactical][,opening]")
    parser.add_argument("--exclude-fixture", type=Path, action="append", default=[], help="--build-pool: hold every position of this fixture out (repeatable)")
    parser.add_argument("--overwrite", action="store_true", help="--build-pool: replace an existing fixture")
    parser.add_argument("--curve", help="--ladder/--validate: the curve, as elo:T,elo:T")
    parser.add_argument("--temperature", type=float, help="--ladder: a flat temperature instead of a curve")
    parser.add_argument("--pool", choices=("fit", "validation"), default="fit", help="--ladder: whose positions")
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--json", type=Path, help="--report/--latency/--speed-counts: write the numbers here too")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="--build-pool seed")
    parser.add_argument("--source-url", default=SOURCE_URL, help="--build-pool: the URL recorded in the header")
    parser.add_argument("--max-games", type=int, help="--build-pool/--speed-counts: stop after this many games")
    parser.add_argument("--slope", type=float, help="--speed-counts: the human slope b of ln CPL = a + b*Elo, to give the loss ratios in Elo")
    parser.add_argument(
        "--latency-models", type=lambda s: s.split(","), default=["maia3-23m", "maia3-5m"]
    )
    parser.add_argument(
        "--latency-threads", type=lambda s: [int(x) for x in s.split(",")], default=[1, 2]
    )
    parser.add_argument("--latency-moves", type=int, default=LATENCY_MOVES)
    args = parser.parse_args(argv)
    pilotable = args.sweep or args.ladder or args.validate
    chosen = bool(args.build_pool or args.mover_index or args.speed_counts) or any(
        (args.run, args.report, args.sweep, args.fit, args.ladder, args.validate, args.latency)
    )
    if args.pilot and chosen and not pilotable:
        parser.error("--pilot goes alone or with --sweep, --ladder or --validate")
    if not chosen and not args.pilot:
        parser.error("choose a mode")
    if set(args.pools) - set(POOLS):
        parser.error(f"--pools takes {', '.join(POOLS)}")
    return args


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
    if args.mover_index:
        return run_mover_index(args)
    if args.speed_counts:
        return run_speed_counts(args)
    if args.sweep:
        return run_sweep(args)
    if args.fit:
        return run_fit(args)
    if args.ladder:
        return run_ladder(args)
    if args.validate:
        return run_validate(args)
    if args.pilot:
        return run_pilot(args)
    if args.run:
        return run_full(args)
    if args.report:
        return run_report(args)
    return run_latency(args)


if __name__ == "__main__":
    sys.exit(main())
