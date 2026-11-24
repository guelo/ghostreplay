# Maia3 temperature per rating: fit and validation (maia3-23m)

Status: measured locally on 2026-11-22 and 2026-11-23 under `g-maia3-elo-temp`.
The flatness rule was approved by the user on 2026-11-22, before any run, and
did not change afterwards. The user chose the candidate curve on 2026-11-23,
before the validation run, and approved its adoption after it. It was deployed
on 2026-11-23; the checks are under "Production on Railway".

It follows `maia3-local-engine-2026-11-18.md`, which found that sampled play
(T=1) is close to its label at 1500 and 2000, somewhat weak at 600 and 1100,
and about 540 Elo weak at 2600.

## Outcome

**The sampling temperature now follows the target rating.** The default for
maia3-23m is the curve

`600:0.72,1300:1.0,1500:1.0,2000:0.94,2300:0.7,2600:0.49`

(`DEFAULT_TEMPERATURE_CURVES` in `backend/app/maia3_client.py`). The
temperature is linear between two anchors and flat outside them, so the 16 bot
levels play at:

| Level | 600 | 800 | 1000 | 1100 | 1200 | 1300 | 1400 | 1500 |
|---|---|---|---|---|---|---|---|---|
| T | 0.72 | 0.80 | 0.88 | 0.92 | 0.96 | 1.00 | 1.00 | 1.00 |

| Level | 1600 | 1700 | 1800 | 1900 | 2000 | 2200 | 2400 | 2600 |
|---|---|---|---|---|---|---|---|---|
| T | 0.988 | 0.976 | 0.964 | 0.952 | 0.94 | 0.78 | 0.63 | 0.49 |

On positions that were not used for fitting, the curve passes every part of the
flatness rule, so the rule's outcome is "adopt":

| Check | Result |
|---|---|
| Guard: the human slope's 95% interval is below 0 | holds: -6.7% CPL per 100 Elo, [-7.4%, -6.0%] |
| R1: flatter than T=1, in at least 95% of resamples | holds: 100% of 1,000 |
| R2: every band's interval for its offset reaches inside ±150 Elo | holds |
| R3: every variety interval reaches 2 distinct moves in 20 | holds: the lowest is 2.02 [1.73, 2.35] |
| R4: no bot level clearly below a lower one | holds: every higher level measured stronger |

| | Under the curve | At T=1 |
|---|---|---|
| D: mean distance from the label over the seven bands | 88 Elo (noise floor 61) | 226 Elo (noise floor 61) |
| Improvement, D at T=1 minus D under the curve | 138 Elo, 95% interval [66, 188] | |

**What the pass means.** The curve brings the bot about 138 Elo closer to
Lichess blitz players of its label than T=1 does, on average over the bands. It
does not show that a level is within 150 Elo of its label, or that its play is
human-like. R2 and R3 are screens for gross error.

What a reader should know before relying on it:
- **Not the mechanical fit.** The fit gave an anchor at 1100 (0.79). The user
  dropped it at the fit gate, before validation ("Why the 1100 anchor was
  dropped").
- **The 2600 anchor is set by the variety floor, not by strength.** Matching
  2600 players' loss would need about T=0.31; the floor keeps it at 0.49, so
  2600 still measures 177 Elo below its label.
- **600 overshoots.** The bot is now about as far above 600 players (+146) as
  it was below them at T=1 (-141), and its interval excludes zero.
- **1200 and 1300 play alike.** On both pools the 1300 level is no stronger
  than the 1200 level ("The ladder").
- **Most of the gain is at the top.** The offsets at 2000, 2300 and 2600 shrink
  by 169, 294 and 410 Elo. Levels 1300 to 1500 play at T=1 as before.

A change to any anchor needs a new validation pool: this one has been used.

**Configuration and rollback.**
- `MAIA3_TEMPERATURE_CURVE` replaces the default curve; `MAIA3_TEMPERATURE`
  sets one temperature for every rating and wins over any curve.
- The curve is the default for maia3-23m only. maia3-5m was not measured and
  plays at T=1 unless a curve is set for it.
- Rollback: set `MAIA3_TEMPERATURE=1` on the service, or revert the commit.
- The "engine ready" log line names the temperature in force and where it came
  from.

## Setup

- **Code:** `backend/scripts/probe_maia3.py` at `cff1cdb`, sha256
  `594bc5df42d6609c718a9f0247e72b14dd237f98ca60804733957cd9a3d1e618`. The
  rule's constants are in that file. The file had the same hash when the
  candidate was frozen and after the validation run.
- **Fixtures** (`backend/scripts/fixtures/`):
  - `maia3_probe_positions.json` (the 2026-11-18 fixture), sha256
    `c9dfd07ac6f125ec28d96ae470fd1d252876e815e6339c20b47111936edef875`, with
    its mover sidecar `maia3_probe_positions_movers.json`.
  - `maia3_probe_fit_extra.json`, sha256
    `a53b8048be44de388404bcc47535a2ffa2eb1c1ea468adc033d7ed4b587a0000`.
  - `maia3_probe_validation.json`, sha256
    `1acd8b41f9d106e703344c15a435010c121b048c08cb1fb5791e0427aec508b9`.
- **Engine:**
  - maia3-23m, the unmodified Maia3 child over UCI, driven through the
    production `Maia3EngineService`.
  - The temperature is the UCI option `Temperature`, sent with every move. A
    curve runs through `Maia3Settings.temperature_curve`, which is the
    production path.
  - One thread per engine, eight engines in parallel, TopP = 1.
- **CPL:** Stockfish 19 at depth 18, with the loss defined and clamped as on
  2026-11-18.
- **Host:** M1 Pro (8 performance + 2 efficiency cores), 32 GB, macOS.
- **Runs:**

| Run | Wall clock | New engine moves | New Stockfish evals |
|---|---|---|---|
| Sweep, fit pool (8 temperatures per band) | 2 h 06 min | 352,400 | 17,568 |
| Fit-pool ladder, the curve as fitted | 56 min | 168,000 | 6,638 |
| Fit-pool ladders, two alternative curves | 44 and 38 min | 336,000 | 5,114 |
| Validation | 3 h 06 min | 260,100 | 38,254 |

The sweep's and the validation's move counts include their pilots; their eval
counts are the main Stockfish stage, without the pilots' 149 and 616.

- **Reproducing:**
  - **The reports**, from `backend/` with this run's cache:
    - `python scripts/probe_maia3.py --fit`
    - `python scripts/probe_maia3.py --ladder --curve <curve>`
    - `python scripts/probe_maia3.py --validate --curve <curve>`
  - **The numbers:** next to this file, `…-fit.json`, `…-fit-ladder.json`
    (the adopted curve on the fit pool) and `…-validation.json`.
  - **The runs:** a rerun draws different samples. Each engine start picks a
    random seed, so the cache is the only record of these samples. A copy is
    kept outside the repository; its location is in the bead notes.

## Positions

Both pools use the 2026-11-18 filters: rated blitz from the standard start,
both players non-BOT and within 100 of the band, one position per game at a ply
drawn from 16-60, the mover with at least 30 s, and no position that Stockfish
puts beyond ±1000 cp. Each band has 1,000 natural positions.

- **Fit pool:** games of 2025-10-01 (UTC). The five bands of the 2026-11-18
  fixture, plus 1300 and 2300 from the same dump, and the fixture's 40 opening
  positions.
- **Validation pool:** games of 2026-10-01 (UTC), a year later, built with
  another seed. All seven bands, and 40 other opening positions from the repo
  opening book.
- **Held out.** A position is identified by the EPD of its board. The builder
  keeps every position of a fit fixture out. No natural candidate was in one;
  four repeated an earlier candidate and were dropped. 57 lines left the
  opening book before sampling, because their position was in a fit fixture or
  among the pool's own natural positions. The validation run checks again
  before it starts: 0 shared positions.

A band takes its first passing games in the order of the dump, so time of day
is not controlled. "Movers" is the number of different players to move behind
the 1,000 positions. Intervals on natural positions resample movers. The 40
opening positions come from the opening book and have no mover, so their
variety intervals resample positions. That includes the tightest R3 check, the
opening figure at 2600.

| Band | Fit: window (UTC) | Fit: movers | Validation: window (UTC) | Validation: movers | Validation: busiest mover |
|---|---|---|---|---|---|
| 600 | 00:00-14:48 | 655 | 00:02-14:46 | 670 | 1.2% |
| 1100 | 00:00-00:34 | 766 | 00:00-00:33 | 768 | 0.5% |
| 1300 | 00:00-00:21 | 794 | 00:00-00:20 | 813 | 0.5% |
| 1500 | 00:00-00:14 | 827 | 00:00-00:13 | 867 | 0.3% |
| 2000 | 00:00-00:18 | 756 | 00:00-00:18 | 767 | 0.5% |
| 2300 | 00:00-01:27 | 514 | 00:00-01:43 | 477 | 1.3% |
| 2600 | 00:00-11:58 | 366 | 00:02-10:36 | 372 | 3.6% |

At 2300 and 2600 three quarters or more of the positions come from players
seen more than once (75% and 84% on the validation pool). Standard errors by
movers and by positions still agree within 11% everywhere; both are in the JSON.

### What the labels mean

**Band centres.** The fixtures store no ratings. Matching every natural
position back to its game gives the mean rating of the player to move, minus
the label (standard error about 1.7 each):

| Band | 600 | 1100 | 1300 | 1500 | 2000 | 2300 | 2600 |
|---|---|---|---|---|---|---|---|
| Fit pool | +26.9 | +9.1 | +4.8 | +6.0 | -13.1 | -20.6 | -16.2 |
| Validation pool | +24.8 | +3.4 | +5.4 | +2.5 | -8.9 | -11.3 | -20.6 |

So the reference for "600" is players of about 625 and for "2600" players of
about 2580: the bands span about 1,955 Elo, not 2,000. Nothing here corrects
for it.

**Blitz ratings.** Maia3 was trained on blitz games, and the labels are Lichess
blitz ratings. GhostReplay has no clock, so a rapid rating may be the closer
comparison for some players. From the first 1,244,491 games of the 2025-10
dump, on Lichess's own analysis where a game carries it, a rapid or classical
player of the same rating number compares to a blitz player like this (in Elo;
positive means the rapid or classical player loses less per move):

| Band | Rapid against blitz (SE) | Classical against blitz (SE) |
|---|---|---|
| 600 | -166 (141) | not measured |
| 1100 | -79 (26) | -355 (105) |
| 1300 | -40 (23) | -184 (81) |
| 1500 | -39 (20) | -107 (55) |
| 2000 | +84 (24) | +343 (105) |
| 2300 | +171 (46) | +965 (231), 14 games |
| 2600 | +463 (193), 17 games | not measured |

Below 2000 the bot's blitz label reads slightly high next to a rapid rating,
and from 2000 up it reads low. `--speed-counts` prints this table.

## The fit (fit pool)

For each band the engine played the band's 1,000 positions at eight
temperatures, five samples per position (one at T=0). The mean CPL at each
temperature gets a non-decreasing fit over T.

- **T\*** is the highest temperature at which the engine loses no more than the
  band's humans, capped at 1.
- **The variety floor** is the lowest temperature at which 20 samples hold 2
  distinct moves on average, on the band's first 100 natural positions and on
  the 40 opening positions.
- **The anchor** is the larger of the two, at most 1. T\* is rounded to the
  nearest 0.01 and a floor is rounded up.

Mean CPL per temperature:

| T | 600 | 1100 | 1300 | 1500 | 2000 | 2300 | 2600 |
|---|---|---|---|---|---|---|---|
| 0.00 | 121.9 | 80.2 | 68.9 | 60.7 | 52.5 | 43.6 | 45.4 |
| 0.20 | 124.3 | 83.3 | 68.9 | 63.4 | 52.8 | 44.2 | 45.3 |
| 0.35 | 128.3 | 82.6 | 73.5 | 65.8 | 54.4 | 45.5 | 49.8 |
| 0.50 | 135.3 | 89.3 | 76.0 | 69.4 | 57.1 | 49.6 | 50.2 |
| 0.65 | 151.6 | 98.1 | 85.1 | 79.2 | 60.9 | 55.9 | 55.0 |
| 0.80 | 163.3 | 107.9 | 92.9 | 84.1 | 64.0 | 60.9 | 58.1 |
| 0.90 | 174.4 | 111.3 | 98.0 | 91.6 | 70.7 | 65.2 | 63.3 |
| 1.00 | 179.6 | 119.6 | 106.0 | 95.4 | 75.8 | 65.7 | 65.8 |
| **Humans** | **157.0** | **107.1** | **106.0** | **95.3** | **72.7** | **57.6** | **48.5** |

The anchors:

| Band | T\* [95%] | Variety floor | Anchor | Set by |
|---|---|---|---|---|
| 600 | 0.720 [0.590, 0.850] | 0.200 (below the grid) | 0.72 | the crossing |
| 1100 | 0.788 [0.636, 0.976] | 0.265 | 0.79 | the crossing |
| 1300 | 1.000 [0.888, 1.000] | 0.262 | 1.00 | the cap |
| 1500 | 0.996 [0.833, 1.000] | 0.326 | 1.00 | the cap |
| 2000 | 0.938 [0.815, 1.000] | 0.361 | 0.94 | the crossing |
| 2300 | 0.701 [0.527, 0.881] | 0.370 | 0.70 | the crossing |
| 2600 | 0.306 [0.000, 0.654] | 0.483 | 0.49 | the floor |

The mechanical result is
`600:0.72,1100:0.79,1300:1.0,1500:1.0,2000:0.94,2300:0.7,2600:0.49`. The share
of moves losing 200 cp or more and the distinct-move counts per temperature are
in the fit JSON.

**2600.** At T=0.5 the fit pool reads 50.2 cp against the humans' 48.5, about
60 Elo below the label; at T=1 it reads 535 below. The floor comes from the
opening pool: at T=0.35 the 2600 bot plays 1.60 distinct moves in 20 from an
opening position, and at T=0.5 it plays 2.05.

**TopP, report only.** TopP stays 1. The sweep also played T=1 with a narrower
nucleus at the two top bands:

| Band and arm | Mean CPL | Offset | Distinct moves: natural / opening |
|---|---|---|---|
| 2300, TopP 0.9 | 57.4 | +6 | 2.81 / 2.62 |
| 2300, TopP 0.8 | 48.1 | +316 | 2.05 / 1.80 |
| 2600, TopP 0.9 | 54.4 | -203 | 2.72 / 2.02 |
| 2600, TopP 0.8 | 48.5 | -2 | 1.99 / 1.45 |

TopP 0.8 matches 2600 players' loss, but it repeats itself in the opening more
than the floor allows.

### Why the 1100 anchor was dropped

With the curve as fitted, all 16 levels played the same 2,100 fit-pool
positions. No level was clearly below a lower one, but the ladder was flat from
1000 to 1300:

| Level | 1000 | 1100 | 1200 | 1300 | 1400 | 1500 |
|---|---|---|---|---|---|---|
| As fitted: mean CPL | 107.8 | 103.8 | 104.0 | 108.1 | 102.2 | 99.5 |
| Without the 1100 anchor | 118.2 | 111.1 | 107.2 | 107.7 | 104.6 | 97.1 |
| Without the 1300 anchor | 108.8 | 104.2 | 101.1 | 100.3 | 99.7 | 98.5 |

From 1000 to 1500 the fitted curve gained 141 Elo for 500 on the labels. The
cause is in the fit pool's human means:
- Its 1100 players lose only 1.0% more than its 1300 players (107.1 against
  106.0 cp).
- Over thousands of games with Lichess's own analysis, 1100 blitz players lose
  10.9% more than 1300 players (90.7 against 81.8 cp).
- So the fit pool's 1100 players happened to play well, and an anchor fitted to
  them (0.79) makes that level too strong.

The plan's remedy is to drop one of the two anchors involved. Without the 1100
anchor the same ladder gains 345 Elo from 1000 to 1500; without the 1300
anchor, 174. The alternative ladders were an addition to the plan, run on the
fit pool only.

**Decision (user, 2026-11-23, before any validation move):** validate the curve
without the 1100 anchor, keep the 2600 anchor at 0.49, and still evaluate 1100
as a validation band. The candidate was frozen and recorded with the file
hashes before the run. Level 1100 then plays at 0.92, on the line from 600 to
1300.

## Validation (validation pool)

The curve, T=1 and T=0 each played every band's 1,000 positions at the band's
Elo, five samples per position (one at T=0). H is the mean CPL of the human
moves.

| Band | H | Curve | T=1 | T=0 |
|---|---|---|---|---|
| 600 | 168.8 | 152.4 | 186.1 | 110.1 |
| 1100 | 115.8 | 114.1 | 125.4 | 75.2 |
| 1300 | 112.1 | 114.7 | 110.8 | 73.1 |
| 1500 | 99.4 | 94.1 | 92.8 | 62.7 |
| 2000 | 66.1 | 71.7 | 80.6 | 51.0 |
| 2300 | 52.8 | 51.3 | 66.8 | 40.6 |
| 2600 | 41.9 | 47.4 | 63.0 | 43.0 |

Human CPL against rating on this pool: ln H = 5.573 - 0.000695 × Elo, slope
interval [-0.000769, -0.000618]. The bands' residuals from the line are -2.7%,
-5.5%, +5.1%, +7.0%, +0.7%, -0.8% and -3.1%.

The Elo offset is E = ln(M / H) / slope, as on 2026-11-18. A positive E means
the setting plays stronger than its label.

| Band | E under the curve [95%] | SE | E at T=1 [95%] | E at T=0 |
|---|---|---|---|---|
| 600 | +146 [+34, +252] | 55 | -141 [-261, -29] | +614 |
| 1100 | +22 [-108, +153] | 68 | -115 [-266, +18] | +622 |
| 1300 | -32 [-171, +94] | 70 | +17 [-130, +146] | +617 |
| 1500 | +79 [-62, +217] | 70 | +98 [-41, +232] | +662 |
| 2000 | -118 [-257, +26] | 74 | -287 [-444, -138] | +373 |
| 2300 | +43 [-150, +227] | 94 | -337 [-515, -161] | +379 |
| 2600 | -177 [-366, +32] | 105 | -587 [-766, -395] | -38 |

- **1300 and 1500** play at T=1 in both columns. Their differences are two
  draws of the same setting, and show the sampling noise in a band's bot mean.
- **1100** has no anchor and measures +22. By the sweep, the same temperature
  read about 93 below the fit pool's 1100 players, which agrees with those
  players having played well by chance.
- **600** passes R2 only because its interval starts at +34, inside ±150. The
  validation pool's 600 players lose 168.8 cp where the fit pool's lost 157.0.
- **D.** The noise floor is the D a bot exactly on its labels would still show
  from sampling noise. Under the curve D is 1.4 times its floor, so much of
  what remains is the size of the noise. At T=1 it is 3.7 times its floor.

Share of moves losing 200 cp or more, and median CPL:

| Band | Human: ≥200 / median | Curve: ≥200 / median | T=1: ≥200 / median | T=0: ≥200 / median |
|---|---|---|---|---|
| 600 | 26.3% / 51.0 | 24.4% / 48.0 | 29.4% / 68.0 | 17.9% / 16.0 |
| 1100 | 17.6% / 29.0 | 17.6% / 23.5 | 19.6% / 27.0 | 11.3% / 0.0 |
| 1300 | 16.8% / 20.0 | 18.1% / 22.0 | 17.2% / 23.0 | 11.1% / 0.0 |
| 1500 | 16.0% / 14.0 | 14.9% / 18.0 | 14.3% / 19.0 | 9.7% / 0.0 |
| 2000 | 9.8% / 6.5 | 10.7% / 11.0 | 12.5% / 12.0 | 7.4% / 0.0 |
| 2300 | 7.2% / 3.0 | 7.1% / 0.0 | 10.0% / 7.0 | 5.2% / 0.0 |
| 2600 | 6.0% / 0.0 | 7.1% / 0.0 | 10.1% / 2.0 | 6.3% / 0.0 |

This table is not in the validation JSON. It was read from the same cache with
the probe's own lookups.

### A year between the pools

The validation pool is a year newer than the fit pool, so a miss could be the
players changing and not the curve. The control is T=1, which was measured on
both:

| Band | 600 | 1100 | 1300 | 1500 | 2000 | 2300 | 2600 |
|---|---|---|---|---|---|---|---|
| E at T=1, fit pool (2025-10) | -235 | -193 | +1 | -3 | -74 | -232 | -535 |
| E at T=1, validation pool (2026-10) | -141 | -115 | +17 | +98 | -287 | -337 | -587 |

The shape is the same: T=1 is weak at the bottom, close in the middle and
weakest at 2600. A single band moves by up to 213 Elo between the pools (2000),
against a standard error of about 130 for that difference. The human slope is
steeper on the validation pool (-6.7% per 100 Elo against -5.5%), so the same
CPL ratio is fewer Elo there.

### Variety

Distinct moves in 20 samples under the curve, mean per position with its 95%
interval. The natural column resamples movers, the opening column positions:

| Level | Natural (100 positions) | Opening (40 positions) |
|---|---|---|
| 600 | 5.66 [5.13, 6.22] | 5.60 [4.85, 6.30] |
| 1100 | 5.17 [4.66, 5.65] | 4.83 [4.15, 5.43] |
| 1300 | 5.45 [4.89, 6.00] | 5.20 [4.47, 5.90] |
| 1500 | 5.41 [4.93, 5.88] | 5.45 [4.65, 6.23] |
| 2000 | 4.48 [4.05, 4.92] | 4.42 [3.80, 5.00] |
| 2200 | | 3.23 [2.75, 3.73] |
| 2300 | 3.36 [3.08, 3.64] | 2.80 [2.38, 3.27] |
| 2400 | | 2.60 [2.25, 2.95] |
| 2600 | 2.41 [2.15, 2.66] | 2.02 [1.73, 2.35] |

Every other level's opening figure is above 4 and is in the JSON. At T=1 the
2026-11-18 run measured 4.02 natural and 3.52 opening at 2600 on its own
positions, so the 2600 bot now repeats itself more. Two distinct moves in 20 is a floor against a bot that
always plays the same game, not a measure of human-like variety.

## The ladder

All 16 levels played the same 2,100 positions (the first 300 of each band)
under the adopted curve, five samples each, once on each pool. Mean CPL:

| Level | T | Fit pool | Validation pool |
|---|---|---|---|
| 600 | 0.72 | 145.6 | 161.2 |
| 800 | 0.80 | 127.0 | 137.5 |
| 1000 | 0.88 | 118.2 | 125.1 |
| 1100 | 0.92 | 111.1 | 117.7 |
| 1200 | 0.96 | 107.2 | 112.5 |
| 1300 | 1.00 | 107.7 | 112.3 |
| 1400 | 1.00 | 104.6 | 105.8 |
| 1500 | 1.00 | 97.1 | 100.4 |
| 1600 | 0.988 | 93.6 | 94.9 |
| 1700 | 0.976 | 88.3 | 87.1 |
| 1800 | 0.964 | 82.4 | 83.4 |
| 1900 | 0.952 | 78.6 | 78.3 |
| 2000 | 0.94 | 72.4 | 71.7 |
| 2200 | 0.78 | 59.3 | 58.1 |
| 2400 | 0.63 | 50.2 | 48.4 |
| 2600 | 0.49 | 43.9 | 42.8 |

R4 holds on the validation pool: over the 120 pairs of levels, no simultaneous
95% lower bound of a reversal is above 0 (c = 3.13), and every higher level
measured stronger than every lower one. The pair nearest to a reversal is 1300
against 1200.

Gain in Elo from one level to a higher one (standard error). Each pool uses its
own human slope, so the same CPL ratio is about 22% more Elo on the fit pool:

| Step | On the labels | Fit pool | Validation pool |
|---|---|---|---|
| 600 to 1000 | 400 | +365 (33) | +365 (28) |
| 1000 to 1100 | 100 | +109 (32) | +87 (27) |
| 1100 to 1200 | 100 | +63 (32) | +66 (27) |
| **1200 to 1300** | **100** | **-8 (34)** | **+2 (29)** |
| 1300 to 1400 | 100 | +50 (35) | +86 (29) |
| 1400 to 1500 | 100 | +131 (32) | +76 (30) |
| 1000 to 1500 | 500 | +345 (32) | +317 (29) |
| 1500 to 2000 | 500 | +515 (40) | +484 (36) |
| 2000 to 2600 | 600 | +878 (53) | +742 (44) |

**1200 and 1300.** On both pools the 1300 level plays no stronger than the 1200
level, and the compression is confined to that step. Its cause is not
established:
- The temperature rises from 0.96 to 1.00 across the step. By the sweep at band
  1300 (98.0 cp at T=0.9, 106.0 at T=1), 0.04 of temperature costs about 3% of
  CPL there, or about 45 to 55 Elo. That is measured on band-1300 positions,
  not on the ladder's mixed positions, so it shows the ramp could matter and
  does not attribute the gap to it.
- Whether the two levels also play alike at T=1 was not measured. The plan runs
  a T=1 ladder only when R4 fails.

Sizes on common positions are indicative: R4 reads only the sign.

## Production on Railway

Read on 2026-11-24 between 00:56 and 01:29 UTC (the evening of 2026-11-23 in
Pacific time), on deployment `260ee80b` built from commit `b80e9f9`. It
replaced `73452b67` (commit `9bb3ae3`), which played at T=1.

- **Variables:** the service has `MAIA3_ENABLED=1` and
  `MAIA3_MIN_PLAY_BUDGET_S=0.3`. It has no `MAIA3_TEMPERATURE`,
  `MAIA3_TEMPERATURE_CURVE`, `MAIA3_MODEL` or `MAIA3_TOP_P`, before and after
  the deployment, so nothing overrides the default curve.
- **The live engine's log line**, at 01:00:00 UTC:
  `maia3 engine ready pid=16 model=maia3-23m ... threads=1 start_ms=2307
  temperature=curve 600:0.72,1300:1.0,1500:1.0,2000:0.94,2300:0.7,2600:0.49
  (default for maia3-23m)`.
- **Move lines of real games:** the user played one game at 600 and one at
  2600 by hand. All 19 engine moves at 600 logged `temp=0.72`, and all 34 at
  2600 logged `temp=0.49`. No move was abandoned. The deployment's HTTP log
  holds 493 responses, all between 01:16 and 01:29 UTC and every one 2xx.

Latency. `--latency` ran in the container on a second engine with one thread:
500 moves at each of 600, 1100, 1500, 2000 and 2600 in turn, so the
temperature changes on every move. The mode printed the same temperature label
as the live engine. The real games' figures are the `ms=` of the `maia3 move`
log lines.

| Run | CPU | Moves | p50 | p95 | Max |
|---|---|---|---|---|---|
| 2026-11-24, the curve, `--latency` | Intel Xeon 6975P-C | 2,500 | 62.6 ms | 69.0 ms | 85.9 ms |
| 2026-11-24, the curve, real games | Intel Xeon 6975P-C | 53 | 68 ms | 77 ms | 84 ms |
| 2026-11-18, T=1, `--latency` | Intel Xeon Processor (Icelake) | 2,500 | 116.2 ms | 136.5 ms | 195.9 ms |
| 2026-11-18, T=1, real games | Intel Xeon Processor (Icelake) | 74 | 119.5 ms | 143 ms | 169 ms |

- **The two days are on different hosts,** so the halving is the CPU and says
  nothing about the curve. The temperature is one more UCI option sent with
  each move, next to the Elo. Locally the curve cost nothing visible: p50 31.2
  ms over 10 moves under the curve, against 31.7 ms over 200 at T=1 on
  2026-11-18.
- **Against the budgets:** on both hosts every move is under the 0.3 s minimum
  play budget and far under the 5 s request budget. The slowest move in the
  four rows is 195.9 ms.
- **Start:** the live engine was ready 2,307 ms after it was spawned (3,813 ms
  on 2026-11-18). The `--latency` engine finished its warm-up move after 1,252
  ms (2,525 ms).
- **Engine memory (VmRSS):** the live engine held 358,956 KiB before the run
  and 358,948 KiB after it, with a peak (VmHWM) of 423,564 KiB. The `--latency`
  engine ended at 357,400 KiB with a peak of 420,972 KiB. The live engine is
  within 2% of its 2026-11-18 readings (354,500 and 424,368 KiB); the model is
  the same.
- **Container:** `memory.current` was 429 MB before and after the run.
  `memory.peak` read 828 MB afterwards. It was not read before the run, so it
  is likely, but not shown, that the second engine accounts for the rise.

## Caveats

- **CPL stands in for strength.** Matching mean CPL does not guarantee matching
  results. The Elo conversion rests on a seven-band log-linear fit of human
  CPL, whose residuals reach 7.0% (at 1500).
- **The rule's reach.** One band's offset has a standard error of 55 to 105
  Elo. The rule can show that the curve is clearly flatter than T=1, that no
  band is grossly off and that no level plays grossly below a lower one. It
  cannot show that a band is within 100 Elo of its label, or rule out a
  reversal of under 100 Elo. R4 catches a 100-Elo reversal between two levels
  only about 25-60% of the time.
- **One temperature for the whole game.** A low temperature at 2600 also
  narrows its openings; R3 measures that on 40 opening positions.
- **Blitz only, one day per pool, time of day not controlled.** 600 and 2600
  span 10 to 15 hours of their day, the common bands minutes after 00:00 UTC.
- **Players repeat.** Band 2600 has 372 movers behind its 1,000 validation
  positions. Intervals on natural positions resample movers.
- **maia3-23m only.** A curve set by hand for another model is the operator's
  responsibility.
- **A stale `MAIA3_TEMPERATURE` disables the curve.** The start-up log line
  names which one is in force.
- **The validation pool is spent.** It has judged this curve; a revised curve
  needs positions that neither fit nor validation has seen.
