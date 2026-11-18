# Maia3 local engine: strength probe (maia3-23m)

Status: measured locally on 2026-11-18 under `g-maia3-strength-probe` (chunk 3
of `g-maia3-local-engine`). The temperature decision is the user's and is
recorded in that bead. `g-maia3-railway-deploy` appends the production numbers.

## Outcome

The decision rule approved on 2026-11-15 gives **"only criterion 2 fails":
keep T=1 (`MAIA3_TEMPERATURE=1`) and file a follow-up for the 2600 band**, for
the user to decide.

**Decision (user, 2026-11-18):** keep T=1, which is already the default, so no
env change. No follow-up is filed for the 2600 band.

| | Value |
|---|---|
| D1: T=1's mean distance from its labels | 211 Elo (noise floor F1 = 73) |
| D0: T=0's mean distance from its labels | 493 Elo (F0 = 99) |
| Bootstrap resamples with D1 < D0 | 100% (1,000 of 1,000) |
| Criterion 1, D1 < D0 | holds |
| Criterion 2, every band's E1 upper 95% bound above -150 | fails at 2600 (upper bound -362) |
| Guard: the human slope's 95% interval below 0 | holds |

Both D values are more than twice their noise floor, so each is within a few
percent of the real distance (the bead's "How to read D").

In short:
- **T=1 vs humans:** sampled play matches Lichess blitz players of its label at
  1500 and 2000, is somewhat weaker at 600 and 1100, and is much weaker at 2600.
- **T=0 vs humans:** top-move play, which is how the pre-outage bots chose their
  moves, is 450 to 800 Elo stronger than its label everywhere except 2600, where
  it is close to the label.

## Setup

- **Code:** `backend/scripts/probe_maia3.py` at `a08f9e4`.
- **Fixture:** `backend/scripts/fixtures/maia3_probe_positions.json`, sha256
  `c9dfd07ac6f125ec28d96ae470fd1d252876e815e6339c20b47111936edef875`.
- **Engine:**
  - maia3-23m, the unmodified Maia3 child over UCI, driven through the
    production `Maia3EngineService`.
  - One thread per engine, eight engines in parallel.
  - T=1 runs with TopP = 1.
- **CPL:**
  - Stockfish 19 (Homebrew, apple-silicon build) at depth 18.
  - Eight workers, each with one thread and 64 MB hash; the hash is cleared for
    every position.
  - Loss = eval(after Stockfish's best move) - eval(after the move), from the
    mover's point of view. Each eval is clamped to ±1000 cp, mates count as
    1000, and the loss is clamped to [0, 1000].
- **Host:** M1 Pro (8 performance + 2 efficiency cores), 32 GB, macOS.
- **Run:** 1 h 32 min in all.
  - Engine moves: 93,430 in 19 min (82 per second).
  - Stockfish evals: 21,282 in 72 min (4.9 per second).
  - Reused from the cache: the pilot's 2,540 moves and 324 evals, and the pool
    build's 5,530 root evals.
  - The pilot projected 4.1 h. Its Stockfish rate, 1.75 evals per second, was a
    third of the real one; the cause was not found.
- **Reproducing:**
  - **The report:** `python scripts/probe_maia3.py --report` from `backend/`,
    with this run's cache. The machine-readable report is
    `maia3-local-engine-2026-11-18.json`, next to this file.
  - **The run:** a rerun draws different samples. Each engine start picks a
    random seed for its T=1 samples, so this run's cache is the only record of
    them. A copy is kept outside the repository; its location is in the bead
    notes.

## Positions

The fixture holds 5,070 positions:

- **Natural:** 1,000 per band.
  - Drawn from the first 1,926,604 games of the Lichess standard dump for
    2025-10 (CC0), all played on 2025-10-01 (UTC), one position per game.
  - Filters:
    - rated blitz (base + 40 × increment in 180-479 s), played from the
      standard start
    - both players non-BOT and within 100 of the band
    - "Rules infraction" terminations excluded
  - The ply is drawn uniformly from 16-60. A position is skipped when the mover
    had under 30 s, and replaced when Stockfish puts it beyond ±1000 cp.
- **Tactical:** 30 positions. Ten are hand-picked; the other 20 are only-move
  positions (best move at least 150 cp ahead of the second), four per band,
  taken from later games than the natural pool.
- **Opening:** 40 positions from the repo opening book at plies 8-20.

The first 100 natural positions of each band, plus the tactical and opening
pools, form the 570 positions that are played across the whole Elo grid.

A band's positions are its first passing games in the order of the dump, so
time of day is not controlled:

| Band | Games read to fill it | Time window (UTC) | Rejected as already decided |
|---|---|---|---|
| 600 | 1,558,031 | 00:00-14:48 | 327 of 1,327 (24.6%) |
| 1100 | 38,774 | 00:00-00:34 | 126 of 1,126 (11.2%) |
| 1500 | 16,546 | 00:00-00:14 | 46 of 1,046 (4.4%) |
| 2000 | 20,309 | 00:00-00:18 | 26 of 1,026 (2.5%) |
| 2600 | 1,105,888 | 00:00-11:58 | 5 of 1,005 (0.5%) |

## Headline 2: against Lichess blitz players of the band

This is the input to the decision rule. On each band's 1,000 positions:
- H is the mean CPL of the human moves.
- M1 is the mean CPL of T=1 at the band's Elo, over five samples per position.
- M0 is the mean CPL of T=0 at the band's Elo.

| Band | H | M1 | M0 | M1 - H [95%] | M0 - H [95%] |
|---|---|---|---|---|---|
| 600 | 157.0 | 179.6 | 121.9 | +22.5 [+10.5, +34.7] | -35.1 [-48.8, -21.2] |
| 1100 | 107.1 | 119.6 | 80.2 | +12.4 [+1.7, +22.8] | -27.0 [-37.9, -16.4] |
| 1500 | 95.3 | 95.4 | 60.7 | +0.1 [-8.4, +9.8] | -34.6 [-44.7, -25.0] |
| 2000 | 72.7 | 75.8 | 52.5 | +3.2 [-5.0, +11.6] | -20.2 [-29.1, -11.0] |
| 2600 | 48.5 | 65.8 | 45.4 | +17.3 [+11.2, +23.6] | -3.1 [-10.0, +4.4] |

Human CPL against rating: ln H = 5.368 - 0.000562 × Elo, with a slope interval
of [-0.000646, -0.000482]. That is -5.5% CPL per 100 Elo. The bands' residuals
from the line are +2.6%, -7.3%, +3.3%, +4.3% and -2.5%.

The Elo offset is E = ln(M / H) / slope: the rating difference at which humans
would lose that much. A positive E means the setting plays stronger than its
label.

| Band | E1 [95%] | SE1 | E0 [95%] | SE0 |
|---|---|---|---|---|
| 600 | -238 [-414, -103] | 80 | +450 [+275, +630] | 91 |
| 1100 | -196 [-382, -24] | 90 | +516 [+315, +742] | 110 |
| 1500 | -3 [-197, +159] | 87 | +803 [+585, +1082] | 129 |
| 2000 | -76 [-284, +128] | 106 | +579 [+304, +879] | 147 |
| 2600 | -542 [-724, -362] | 92 | +119 [-160, +395] | 141 |

Share of moves losing 200 cp or more, and median CPL:

| Band | Human: ≥200 / median | T=1: ≥200 / median | T=0: ≥200 / median |
|---|---|---|---|
| 600 | 24.7% / 50.5 | 27.9% / 59.5 | 19.0% / 19.0 |
| 1100 | 17.3% / 22.5 | 19.8% / 27.0 | 13.1% / 0.0 |
| 1500 | 14.4% / 19.0 | 15.2% / 17.0 | 8.7% / 0.0 |
| 2000 | 11.0% / 9.0 | 11.5% / 12.0 | 7.2% / 0.0 |
| 2600 | 6.8% / 0.0 | 10.7% / 2.0 | 7.2% / 0.0 |

What the tables show:
- **T=1 at 2600:** it loses 17 cp a move more than 2600 humans and makes a
  ≥200 cp mistake on 10.7% of moves, against their 6.8%. Human CPL changes only
  about 3 cp per 100 Elo at that level, so the gap converts to -542 Elo.
- **T=0 at 2600:** it is within its interval of the label.
- **T=1 at 600 and 1100:** it reads about 200 Elo below its label. The rule's
  upper bounds (-103 and -24) are inside its -150 floor, so these bands do not
  count as clearly low.
- **T=0 outside 2600:** it is 450 to 800 Elo above its label. That is the
  pre-outage bots' known overshoot.

## Headline 1: against the pre-outage bots

X(b) is the Elo at which top-move Maia (T=0) has the same mean CPL as T=1 at
band b, on the 500 headline-1 natural positions. The T=0 curve, after an
isotonic fit, falls from 105.9 cp at 600 to 44.1 cp at 2600.

| T=1 at | T=1 mean CPL | Plays like top-move Maia at [95%] |
|---|---|---|
| 600 | 171.6 | below 600 [below 600, below 600] |
| 1100 | 117.6 | below 600 [below 600, 609] |
| 1500 | 98.2 | ~669 [below 600, 840] |
| 2000 | 80.9 | ~968 [699, 1166] |
| 2600 | 68.2 | ~1197 [1046, 1608] |

**Relative to the pre-outage bots, every T=1 bot is much weaker than the old
bot with the same label.** T=1 at 2600 plays like the old bot at about 1200.
Users who played the old bots will notice the drop. The old bots played above
their labels, so this headline alone doesn't show that T=1 plays below its
label; headline 2 measures that.

## Elo conditioning and sampling spread

- **Moves at 600 vs 2600:** T=0 picks a different move on 242 of the 500
  headline-1 positions (48.4%). Neighbouring bins (100-200 Elo apart) differ on
  3-16% of positions. The full 16-bin matrix is in the JSON.
- **Distinct moves in 20 T=1 samples**, mean per position:

| Pool | 600 | 1100 | 1500 | 2000 | 2600 |
|---|---|---|---|---|---|
| Natural (headline 1) | 7.66 | 5.82 | 5.21 | 4.50 | 4.02 |
| Tactical | 6.13 | 3.97 | 3.47 | 2.63 | 2.07 |
| Opening | 8.03 | 6.45 | 5.62 | 4.65 | 3.52 |

## CPL per pool and Elo

Each cell is the mean CPL / the share of moves losing 200 cp or more.

T=0, one move per position. There are 500 natural, 30 tactical and 40 opening
positions:

| Elo | Natural (headline 1) | Tactical | Opening |
|---|---|---|---|
| 600 | 105.9 / 17.8% | 273.7 / 40.0% | 63.0 / 5.0% |
| 700 | 94.7 / 15.4% | 250.6 / 36.7% | 49.3 / 2.5% |
| 800 | 89.0 / 15.0% | 252.7 / 36.7% | 51.5 / 2.5% |
| 900 | 83.9 / 14.2% | 235.3 / 36.7% | 51.1 / 2.5% |
| 1000 | 79.4 / 13.6% | 235.3 / 36.7% | 48.5 / 2.5% |
| 1100 | 77.0 / 13.2% | 249.4 / 40.0% | 47.1 / 2.5% |
| 1200 | 67.9 / 12.0% | 244.7 / 36.7% | 42.9 / 2.5% |
| 1300 | 67.0 / 11.4% | 231.3 / 36.7% | 40.9 / 2.5% |
| 1400 | 63.7 / 10.8% | 171.4 / 26.7% | 38.6 / 2.5% |
| 1500 | 64.3 / 10.8% | 171.4 / 26.7% | 39.0 / 2.5% |
| 1600 | 58.4 / 10.0% | 147.2 / 23.3% | 15.8 / 0.0% |
| 1700 | 58.4 / 10.2% | 147.2 / 23.3% | 15.8 / 0.0% |
| 1800 | 58.2 / 10.2% | 102.5 / 16.7% | 14.1 / 0.0% |
| 1900 | 56.4 / 9.6% | 102.5 / 16.7% | 12.3 / 0.0% |
| 2000 | 55.4 / 9.8% | 69.2 / 13.3% | 8.4 / 0.0% |
| 2100 | 53.0 / 9.2% | 69.2 / 13.3% | 8.4 / 0.0% |
| 2200 | 50.1 / 8.8% | 56.3 / 10.0% | 7.0 / 0.0% |
| 2300 | 46.3 / 8.2% | 39.9 / 6.7% | 4.4 / 0.0% |
| 2400 | 45.0 / 7.8% | 39.9 / 6.7% | 5.1 / 0.0% |
| 2500 | 44.1 / 7.8% | 69.8 / 10.0% | 4.9 / 0.0% |
| 2600 | 44.1 / 8.0% | 69.8 / 10.0% | 4.8 / 0.0% |

T=1, 20 samples per position:

| Elo | Natural (headline 1) | Tactical | Opening |
|---|---|---|---|
| 600 | 171.6 / 29.0% | 430.8 / 58.7% | 76.9 / 7.9% |
| 1100 | 117.6 / 20.5% | 293.1 / 44.7% | 52.6 / 4.1% |
| 1500 | 98.2 / 17.0% | 239.9 / 37.5% | 36.3 / 1.8% |
| 2000 | 80.9 / 14.0% | 151.2 / 23.5% | 20.0 / 0.8% |
| 2600 | 68.2 / 11.6% | 106.5 / 19.2% | 9.9 / 0.2% |

The tactical pool has 30 positions, so one position moves its T=0 mean by
about 33 cp. Its T=0 curve is not monotone.

## Latency and memory

These were measured in step 1 with `--latency`, at T=1.
- **macOS arm64, 200 moves:**
  - maia3-23m with one thread: p50 31.7 ms, p95 35.5 ms, max 45.3 ms. Cold
    start is 1,002 ms to `uciok` and 1,170 ms until the warm-up move is done.
    Sampled peak RSS is 401,184 KiB.
  - maia3-23m with two threads: p50 33.1 ms, p95 40.0 ms.
  - maia3-5m with one thread: p50 13.4 ms, p95 14.8 ms, 260,720 KiB.
  - A second thread does not help on macOS.
- **Docker linux/amd64, emulated on arm64, 100 moves:**
  - Peak memory (VmHWM): 495,708 KiB for maia3-23m with one thread, 492,900
    KiB with two, and 379,276 KiB for maia3-5m.
  - The 23m peak matches the CI native smoke (496,464 KiB), so emulation does
    not inflate it.
  - Emulated latency (p50 437 ms for 23m) means nothing for production.

## Production on Railway

Measured on 2026-11-18 on deployment `69ffa93e` (commit `11714af`), with
maia3-23m, one thread and T=1. The engine is on through the service variable
`MAIA3_ENABLED=1`. The CPU reports as `Intel Xeon Processor (Icelake)`.

| Run | Moves | p50 | p95 | Max |
|---|---|---|---|---|
| `--latency`, a second engine, 500 moves per rating | 2,500 | 116.2 ms | 136.5 ms | 195.9 ms |
| Real games, from the `maia3 move` log lines | 74 | 119.5 ms | 143 ms | 169 ms |

- **Start:** the build smoke started in 1,692 ms and played its move in 97 ms.
  The live engine was ready 3,813 ms after it was spawned. The `--latency`
  engine finished its warm-up move after 2,525 ms.
- **Engine memory (VmRSS):**
  - The live engine held 354,500 KiB three minutes after start and 354,884 KiB
    after two games. Its peak (VmHWM) is 424,368 KiB, which it reached before
    the first game.
  - The `--latency` engine ended 2,500 moves at 355,896 KiB (364 MB), which is
    136 MB under the 500 MB budget. This is one end reading, so it bounds the
    growth over the run rather than tracing it.
  - The emulated 300-move run read 474,468 KiB, about 121 MB higher.
- **Cost:** at $10 per GB-month, 364 MB is about $3.64 a month. Railway's memory
  metric rose from 148 MB before the deployment to 411 MB two minutes after it,
  about 263 MB or $2.63 a month. That is less than the child's VmRSS, and the
  reason is not established.
- **Container after use:** `memory.current` was 695 MB after the games, and the
  API process had grown from 168,116 KiB to 371,752 KiB. That growth is the
  API's, with opening score recomputes logged in the same window, and the engine
  did not grow. Railway's metric peaked at 955 MB while the second engine ran.
- **Disk (`du -m`, MiB):** the container's root filesystem holds 2,243 MiB. Torch
  takes 809 MiB and the weights 88 MiB, together 897 MiB or 40% of it. The size
  before the engine was not captured, so 897 MiB is the engine's main share and
  not the exact increase. It leaves out maia3 and its other dependencies.
- **Choice:** maia3-23m, one thread and T=1, all approved by the user on
  2026-11-18.
- **Games:** one game at 600 (21 engine moves) and one at 2600 (53) were played
  by hand, and both felt like the right difficulty. No move was abandoned or
  slow, and the HTTP log for the deployment shows no 4xx or 5xx response.
- **`MAIA3_MIN_PLAY_BUDGET_S`:** twice the measured p95 is 273 ms, which rounds
  up to 0.3 s, against a code default of 0.5 s. The service variable was set to
  0.3 after the measurements above, which produced deployment `c61ba175`. Its
  engine was ready in 2,381 ms. No game has been measured at this setting.

## Caveats

- **Time of day.** The common bands span 14-34 minutes after midnight UTC, and
  600 and 2600 span 12-15 hours (the table above). How much human CPL varies
  with time of day is not known.
- **Players repeat.**
  - The bootstrap resamples positions as if they were independent. A band
    with few active players, which is most likely 2600, has the same players
    behind many positions, so its intervals are probably too narrow.
  - The fixture stores no usernames, so this can't be corrected.
  - Rough check: with twice its standard error, the 2600 E1 upper bound would
    be about -180, still below the -150 floor.
- **One day, blitz only.** All games are from 2025-10-01.
- **CPL stands in for strength.** The Elo conversion rests on a five-band
  log-linear fit of human CPL. The fit's residuals reach 7.3% (at 1100).
