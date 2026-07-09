# Opening Score Final Specification

## Recommendation

Adopt a side-specific, repertoire-aware readiness score with companion diagnostics:

- `Opening Score`: how prepared the user is to play the opening in real games,
  including sample sufficiency and opponent-response breadth
- `Confidence`: backend/API evidence freshness and sparsity telemetry
- `Coverage`: how much of the important opponent-response tree has actually been exposed

This final plan keeps the best product framing from v7 and the better recursion model from v8, with one important refinement:

> On user turns, aggregate across the user's prepared repertoire, but weight branches by actual repertoire usage, not by successful plays.

That avoids the main failure mode in v8's draft weighting: a weak but frequently chosen line should not disappear just because the user keeps failing it.

## What This Plan Keeps

### Keep from v7

- companion `Opening Score`, `Confidence`, and `Coverage` surfaces, with score
  now carrying the readiness-critical confidence/coverage signals
- opponent-turn breadth weighting
- normalized recursive scoring with a stable `0-100` output
- `weighted_depth` as a concrete companion to the abstract score

### Keep from v8

- side-specific identity via `player_color`
- repertoire-aware aggregation on user turns instead of `max(child)`
- opening-book-first architecture using `eco.json` and `eco.byPosition.json`
- confidence based on unified evidence volume plus recency
- `underexposed_branch` as a first-class opening-page output

### Reject from both

- treating opening knowledge as ever "complete"
- using observed engine frequency as the primary opponent-importance weight
- letting `blunders` alone define mastery
- letting one lucky pass look like mastery

## Product Meaning

The headline score should mean:

> If this opening appears again from this side, how reliably can the user stay on track through the important opponent replies in the repertoire they have actually trained?

It should not mean:

- the user knows every legal sideline
- the user has "finished" the opening
- the engine has fairly exposed every off-beat reply

That is why `Confidence`, `Coverage`, and `underexposed_branch` are part of the product, not optional extras.

## Scope and Identity

Opening scores are side-specific.

The system should distinguish at minimum:

- `Sicilian Defense as White`
- `Sicilian Defense as Black`

Each score record is keyed by:

- `user_id`
- `player_color`
- `opening_key`

Where:

- `opening_key` = normalized FEN of the named subtree root
- `opening_name` = display label for that root
- `opening_family` = shallow family label used for top-level opening-page grouping

The active player at a node is determined by comparing the position's side to move with `player_color`:

- user turn: position side to move equals `player_color`
- opponent turn: otherwise

## Tree Definition

### 1. Reference tree

Build the reference opening graph from:

- `public/data/openings/eco.json`
- `public/data/openings/eco.byPosition.json`

Use the same canonical FEN normalization already implemented in the repo:

- frontend: `src/openings/openingBook.ts`
- backend: `backend/app/fen.py`

Normalization rules:

- keep FEN fields 1-4 only
- canonicalize en passant using actual legal EP availability

The score implementation should use the backend normalization so the service and database agree on position identity.

### 2. Named roots

Every score is computed for a named subtree root from the opening book.

The system should precompute named roots by walking the book graph and identifying positions where the deepest opening label changes. Those roots become:

- top-level opening-page family cards
- variation drill-down entries
- branch summary anchors

Use normalized FEN roots, not ECO code alone, as the durable identity.

### 3. User evidence overlay

Overlay evidence from:

- `session_moves`
- `game_sessions`
- `blunders`
- `blunder_reviews`
- optionally `positions` and `moves` for extension/debugging

For each node and edge, collect:

- live attempts
- live passes
- live fails
- edge traversal count
- last live attempt timestamp
- review attempts
- review passes
- review fails
- last review timestamp
- whether the subtree contains an explicit ghost target

`session_moves` is the primary mastery source. `blunders` and `blunder_reviews` mainly provide:

- extra evidence for confidence
- training-intent signals
- drill-down and debugging context

### 4. Phase horizon (shipped: exact Lichess divider)

> **v2 — replaces the v1 "book-exit extension".** The earlier two-decision
> book-exit rule (`book_exit_extension_user_decisions = 2`) is **removed**. There
> is no fixed user-decision cutoff and no per-extension depth discount tied to it.

The opening horizon is the **opening / middlegame boundary** computed by the
exact Lichess phase divider — a faithful static port of `Divider.scala` from
`lichess-org/scalachess` (`backend/app/game_phase.py`; upstream verified
2026-06-06:
<https://github.com/lichess-org/scalachess/blob/master/core/src/main/scala/Divider.scala>).
Per session we reconstruct the board line, call `divide(boards)`, and keep only
opening-interval premoves (everything at or beyond the first middlegame board is
dropped from opening evidence).

Consequences:

- The horizon is **position-driven**, not count-driven: deeper personal prep is
  scored as long as it is still the opening phase, and the score cannot silently
  turn into a middlegame score.
- A named root whose own board already satisfies the middlegame predicate is a
  **raw-middlegame root**. It may *still* be scored when observed off-book moves
  reach quality observations, so "raw-middlegame root" and "unscored root" are
  reported as two distinct counts (see the calibration script).
- `DIVIDER_VERSION` is part of the score fingerprint, so a divider change
  invalidates cached snapshots.

## Core Metrics

### Metric 1: Opening Score

`Opening Score` is the normalized recursive readiness score on a `0-100` scale.
Local mastery uses a lower-confidence bound, and opponent replies only contribute
score credit when their subtree is locally covered.

It should reward:

- surviving deeper into the tree without mistakes
- handling multiple important opponent replies
- knowing multiple prepared self-lines

It should not penalize:

- not learning every legal move on your own turn

### Metric 2: Confidence

`Confidence` is the backend/API trust level of the evidence on a `0-100` scale.

It rises with:

- more evidence
- more recent evidence
- intentional review evidence

Sample sufficiency is now folded into the score through the mastery LCB; the
remaining confidence metric is telemetry for evidence age/sparsity and is not
surfaced as a separate opening-card tile.

### Metric 3: Coverage

`Coverage` is the fraction of important opponent-response weight that has
actually been exposed with enough evidence to be meaningful.

It exists to separate:

- "you fail here"
- "you have barely been shown this branch"

Opponent-branch coverage also gates score credit, so the visible score no longer
grants a prior freebie for opponent replies the user has not prepared.

## Local Statistics

### User-node mastery (shipped: continuous win-chance quality)

> **v2 — replaces the v1 binary pass/fail mastery.** Local mastery is no longer a
> Beta posterior over `pass`/`fail` counts. Each user move contributes a
> **continuous quality** value in `[0, 1]`, and mastery is the skeptical-prior
> mean of those qualities.

At a user-to-move node `n` (`backend/app/opening_rootcalc.py:_mastery`):

```text
p_n = (quality_sum_n + alpha) / (quality_count_n + alpha + beta)
```

where `quality_sum_n` / `quality_count_n` aggregate the per-move quality
observations at `n`. Starting values are unchanged:

- `alpha = 1`
- `beta = 2`

so the prior is still skeptical (one clean observation does not look mastered),
but a near-best move now scores near `1.0` and a small inaccuracy degrades the
term smoothly instead of flipping it to `0`.

### Move quality (shipped: win-chance loss, replaces the eval_delta < 50 rule)

> **v2 — replaces the binary `eval_delta < 50` pass/fail rule.** Quality is a
> continuous function of **win-chance loss**, not a centipawn threshold.

For a user move (`backend/app/opening_quality.py`):

```text
W(cp)      = 2 / (1 + exp(-0.00368208 * cp)) - 1          # Lichess win-chance, clamped to [-1, 1]
wc_loss    = W(best) - W(played)                          # mover perspective, >= 0
quality    = exp(-wc_loss / tau_wc)                       # in (0, 1], 1.0 = best move
```

Quality is resolved from the best available evidence in a fixed
**source-precedence ladder**, recorded per observation for calibration:

1. `session_eval` — primary `(played, best)` evals stored on the move,
2. `analysis_cache` — reconstructed `(played, best)` from a matching
   `analysis_cache` row when the primary evals are absent,
3. `eval_delta` — deterministic centipawn-delta fallback when neither is
   available.

`QUALITY_VERSION`, `TAU_WC`, and `TAU_CP` are part of the score fingerprint, so a
curve change invalidates cached snapshots.

### Prepared repertoire children

At a user node, define `prepared_children(n)` as child edges that satisfy at least one of:

- `edge_live_attempts >= 2`
- `edge_live_passes >= 1`
- the child subtree contains a `blunder` target
- a future explicit manual training marker exists

This is intentionally more permissive than "passed once" and more conservative than "seen once." It captures actual repertoire choices without letting one accidental trial define the repertoire.

### Repertoire weights

For each prepared user child edge `e`, define:

```text
basis_e = edge_live_attempts_e + rho

r_e = basis_e / sum over prepared children j of basis_j
```

Recommended starting value:

- `rho = 1`

Use attempt count, not successful-play count, for the weight basis.

Reason:

- a real repertoire line should still matter if it is weak
- repeated failures should lower the score, not remove the branch from the average
- the score should reflect chosen repertoire breadth, not only the polished part of it

### Opponent reply weights

At opponent nodes, breadth matters.

For MVP:

- split `1.0` equally across known book replies

Do not use observed engine frequency as the primary weight definition. That would bake engine-sampling bias into the score.

If reliable popularity data is added later, use it only for opponent replies.

Future option:

- if tuning shows the known-book tree is too optimistic, add a small `unknown_reply` budget for coverage only, not for the mastery denominator

## Recursive Score

Let `S(n)` be the raw recursive score before normalization.

Use:

- `gamma = 0.8`

### Opponent node

```text
branch_covered_e = 1 if child subtree is locally covered, else 0
S_opp(n) = sum over known children e of w_e * branch_covered_e * S(child_e)
```

Where `w_e` are opponent reply weights over known book replies. In the perfect
normalization pass, `branch_covered_e = 1` so coverage shortfalls affect only the
real numerator.

### User node

```text
S_user(n) = p_n * (1 + gamma * sum over prepared children e of r_e * S(child_e))
```

If there are no prepared children:

```text
S_user(n) = p_n
```

This is the key final choice:

- deeper knowledge matters
- multiple prepared self-lines matter
- unprepared self-lines do not drag the score down

### Perfect normalization

Compute `PerfectS(root)` on the same rooted tree with:

- every user mastery term set to `1`
- the same opponent weights
- the same prepared-child set
- the same repertoire weights
- the same depth discount

Then:

```text
OpeningScore = 100 * S(root) / PerfectS(root)
```

Using the same prepared-child set in the denominator preserves the intended semantics:

- the score judges how well the user knows the repertoire they have actually trained
- `Coverage` remains a separate diagnostic for how much of the important
  opponent tree they have faced

## Confidence Model

Confidence remains parallel telemetry rather than a final multiplier. Sample
sufficiency is now folded into local mastery through the LCB.

### Local confidence

For a user node `n`:

```text
live_attempts_n = live_passes_n + live_fails_n
review_attempts_n = review_passes_n + review_fails_n

evidence_n = live_attempts_n + lambda_review * review_attempts_n
sample_conf_n = 1 - exp(-evidence_n / k_evidence)
freshness_n = exp(-days_since_last_touch_n / half_life_days)

c_n = sample_conf_n * freshness_n
```

Where:

- `last_touch` = max(last live attempt, last review)

Recommended starting values:

- `lambda_review = 0.5`
- `k_evidence = 5`
- `half_life_days = 45`

This treats review evidence as additive but discounted, which is appropriate because review attempts usually also appear in `session_moves`.

### Recursive confidence

```text
C_opp(n) = sum over known children e of w_e * C(child_e)

C_user(n) = c_n, if there are no prepared children
C_user(n) = c_n * sum over prepared children e of r_e * C(child_e), otherwise
```

Normalize to `0-100` against the same tree shape.

## Coverage Model

Coverage is opponent-centric.

The user should be judged on whether they have been exposed to important opponent replies, not on whether they memorized every alternative move for themselves.

### Covered branch rule

An opponent child branch counts as covered if its subtree has at least one of:

- `1` or more live attempts

The historical `1` live plus `1` review clause remains available through
configuration, but with the calibrated default `coverage_live_threshold = 1` a
single live pass earns coverage. Thinness is handled by the score's LCB rather
than by a hard coverage cliff.

### Recursive coverage

At opponent nodes:

```text
covered_e = 1 if child subtree meets the coverage threshold, else 0

Cov_opp(n) = sum over known children e of w_e * covered_e * Cov(child_e)
```

At user nodes:

```text
Cov_user(n) = 1, if n is a leaf
Cov_user(n) = 0, if there are no prepared children
Cov_user(n) = sum over prepared children e of r_e * Cov(child_e), otherwise
```

Then:

```text
Coverage = 100 * Cov(root)
```

## Underexposed Branch

Return one branch summary specifically for the engine-exposure problem:

- `underexposed_branch`

Definition:

- among named descendant subtrees, choose the branch with the largest weighted coverage gap
- weighted coverage gap = branch importance toward the root multiplied by `(1 - coverage_branch)`
- require the branch to fail the local coverage rule

This tells the user:

- not "you are bad here"
- but "this branch matters and the system has not shown it enough"

That is the right product answer to off-beat lines and uneven engine exposure.

## Family Cards and Drill-Down

The UI should support two levels on a dedicated opening page.

Do not tack this onto the existing Stats page. The opening score surface should live on its own page.

### Family cards

Top-level opening-page cards show opening families such as:

- `Sicilian Defense`
- `Italian Game`
- `Queen's Gambit Declined`

### Drill-down

Clicking a family card reveals named descendant roots:

- sub-openings
- major variations
- strongest branches
- weakest branches
- underexposed branches

Each drill-down entry is just the same scoring algorithm applied to a deeper named root.

### Shipped card / hero semantics (v2)

The persisted cache and the `/openings` API use **direct-row** semantics:

- Each card shows its **direct** root row (`opening_score` is the root's own
  0-100 mastery value, higher = better). There is no confidence-weighted
  descendant rollup. A card is **unscored** when its direct row is absent
  (`subtree_score == null`); `subtree_root_count` is navigation metadata only
  (count of scored named rows in the subtree) and never feeds a score.
- The top-level hero shows a **synthetic initial-position** ("whole repertoire")
  row, computed in the same shared DAG pass and persisted under the normalized
  initial FEN with `opening_family = "__repertoire__"`. It is excluded from
  family roll-ups. Drilled-in, the current-branch hero is the selected root's
  direct row.
- Strongest / weakest / underexposed branch summaries are persisted from the one
  shared calculation and read directly by the drill-down (no per-request
  recompute).

### Shipped cache invalidation (v2)

The batch fingerprints fold in the score-model, phase-divider, and quality-curve
versions (`SCORE_MODEL_VERSION`, `DIVIDER_VERSION`, `QUALITY_VERSION`, `TAU_WC`,
`TAU_CP`) alongside graph/roots/config, so any model/divider/curve change
invalidates all prior snapshots on the next read. Recompute decisions (cache
miss, registry drift, stale branch keys, evidence change) are consolidated in
`recompute_opening_scores_if_needed()` and run only on the scheduler's single
serialized worker. Reads are stale-while-revalidate: a warm reader (batch
present) calls `request_recompute()` to schedule a coalesced background
convergence and serves the cached batch immediately, never blocking; only a cold
reader (no batch yet) blocks on `refresh_now()` for the one-time initial compute.

## Output Per Opening

Each computed opening record should include:

- `opening_key`
- `opening_name`
- `opening_family`
- `player_color`
- `opening_score`
- `confidence`
- `coverage`
- `weighted_depth`
- `sample_size`
- `last_practiced_at`
- `strongest_branch`
- `weakest_branch`
- `underexposed_branch`
- `computed_at`

### Weighted depth

Also compute a human-readable depth number:

```text
weighted_depth = expected comfortable depth in user decisions
```

Using the same recursion shape:

```text
D_opp(n) = sum over known children e of w_e * D(child_e)
D_user(n) = p_n, if there are no prepared children
D_user(n) = p_n * (1 + gamma * sum over prepared children e of r_e * D(child_e)), otherwise
```

This is easier to reason about than the raw score alone.

## Implementation Plan

### Phase 1: offline calculator

Build `OpeningScoreCalculator` as a backend script or debug-only service that:

1. loads the book graph from `eco.json` and `eco.byPosition.json`
2. uses backend FEN normalization from `backend/app/fen.py`
3. derives named roots and family relationships
4. overlays evidence from `session_moves`, `game_sessions`, `blunders`, and `blunder_reviews`
5. computes `opening_score`, `confidence`, `coverage`, and branch summaries for one root
6. exposes per-node debug output so constants can be tuned

This phase is for formula tuning, not production UI.

### Phase 2: cached stats

Add a cache table such as:

```text
user_opening_scores(
  user_id,
  player_color,
  opening_key,
  opening_name,
  opening_family,
  opening_score,
  confidence,
  coverage,
  weighted_depth,
  sample_size,
  strongest_branch,
  weakest_branch,
  underexposed_branch,
  last_practiced_at,
  computed_at
)
```

Recompute:

- after session upload
- after SRS review
- or by a background batch if that proves simpler

### Phase 3: opening page

Add a dedicated opening page with:

- family cards sorted by weakest opportunity
- drill-down into named descendants
- UI treatment that clearly distinguishes:
  - low score
  - low confidence
  - low coverage
  - underexposed branch

## MVP Data Constraints

This can be implemented with current data.

### Works now

- `session_moves.fen_before` and `fen_after` identify observed edges
- `game_sessions.player_color` scopes user-side identity
- `blunders` and `blunder_reviews` provide explicit training evidence
- repo already has canonical FEN normalization in both frontend and backend

### Nice later migrations

Later migrations could make the calculator cheaper and simpler:

- add normalized `fen_before` on `session_moves`
- add `position_id` on `session_moves`
- add stored `move_uci` on `session_moves` if edge joins need to be stricter than SAN plus FEN

None of those are required for MVP tuning.

## Starting Constants

| Constant | Value |
|---|---:|
| `alpha` | 1 |
| `beta` | 2 |
| `rho` | 1 |
| `gamma` | 0.8 |
| `lambda_review` | 0.5 |
| `k_evidence` | 5 |
| `half_life_days` | 45 |
| `coverage_live_threshold` | 1 |
| `coverage_review_threshold` | `1 live + 1 review` |
| `tau_wc` | 0.20 |
| `tau_cp` | 100.0 |

> **v2:** `book_exit_extension_user_decisions` is **removed** — the opening
> horizon is the exact Lichess phase divider (§4), not a fixed user-decision
> count. `tau_wc` / `tau_cp` are the continuous win-chance quality-curve
> parameters (`backend/app/opening_quality.py`).

These should stay configurable in a debug view during tuning.

## Calibration Outcome (v2)

v2 is the only live scoring model (no v1 baseline), so calibration is run
**directly on v2** via `backend/scripts/calibrate_opening_scores_v2.py`
(no-write default; see `CALIBRATE_OPENING_SCORES.md`). The script reports
per-user and pooled score distributions, the source mix, horizon behaviour, and
the recursion bound, and is the reproducible basis for the decisions below.

### Quality-curve parameters — **retained**

`tau_wc = 0.20`, `tau_cp = 100.0`. The curve produces the intended smooth,
context-sensitive grading: a near-equality move losing 49cp from an even
position yields `quality ≈ 0.63` (`exp(-0.0899 / 0.20)`), and there is no
discontinuity across the old 49↔50cp pass/fail boundary (asserted by
`test_no_49_50_discontinuity` / `test_context_sensitivity` in
`backend/test_opening_quality.py`). No change.

### Readiness fold parameters — **chosen** (g-xnv7, 2026-07-09)

`lcb_z = 1.0`, `coverage_fold = "gate"`, `coverage_live_threshold = 1`.

The final grid (`backend/.tmp/g-xnv7-final-grid.txt`) used 16 candidate pairs,
5 included pairs at `min_observations = 20`, and passed all three diagnostics:
one-variation specialist true-positive, broadly-prepared false-positive guard,
and thin-but-earned cliff. `gate_x_cov` was rejected because it over-compressed
the bottom of the real distribution; `gate` kept the intended recursive penalty
without double-counting deeper coverage gaps. The broad-prepared guard passed, so
`g-idgs` is not a blocker for this rollout.

At the chosen cell (`lcb_z=1`, `coverage_fold=gate`, live threshold 1), pooled
named-root stats were mean **14.6**, p5 **0.4** / p25 **5.1** / p50 **9.8** /
p75 **20.8** / p95 **43.9** (n=478), synthetic-hero mean **27.6**.

`SCORE_MODEL_VERSION` is bumped to `sm-v2-3`; combined with the
`RootCalcConfig` default change, this causes registry drift and one recompute per
`(user, color)` on first read. In-flight sessions during deploy can show a
one-time crop of negative score deltas if their baseline was captured under
`sm-v2-2`; the next baseline capture self-corrects.

The stats page strongest/weakest top-3 lists sort persisted `opening_score`, so
specialist openings can reshuffle downward under the readiness fold. That is a
coherent score-model change, not a stats regression.

### Grade thresholds — **re-centred** (g-xnv7, 2026-07-09)

`A ≥ 44`, `B ≥ 29`, `C ≥ 8`, `D ≥ 2`, `F < 2`; tones `alert < 5`,
`watch < 29` (`src/openings/format.ts`, pinned by `src/openings/format.test.ts`).

The boundaries are re-centred onto the final combined readiness distribution so
grades differentiate after the score folds in sample sufficiency and opponent
breadth: `A ≥ 44` (~p95), `B ≥ 29` (~p82), `C ≥ 8` (~p40), `D ≥ 2` (~p12),
`F < 2`; tones `alert < 5` (~p25) / `watch < 29`.

> **The raw score is still displayed unchanged** (`formatScore`). Grade and
> number can read e.g. "**A · 50**" — the grade is the *relative* position on the
> observed distribution, the number is the *absolute* win-chance quality. This was
> a deliberate product decision (keep the honest absolute number rather than hide
> or rescale it).
>
> Grade/tone are display of an unchanged stored score, so this re-centre does
> **not** bump `QUALITY_VERSION`. The score-model semantic change itself is
> represented by `RootCalcConfig` drift and `SCORE_MODEL_VERSION = sm-v2-3`.
> The cohort is still volume-dominated by one user, so the central tendency is
> useful but the fine 5-band *shape* should be revisited when more high-observation
> users exist.

### Source mix & horizon

Reported by the script per run (aggregated `session_eval` / `analysis_cache` /
`eval_delta` share with a guarded zero denominator; opening-interval-length
distribution; raw-middlegame vs unscored root counts kept distinct).

### Numeric gates (release bar)

- One-pass in-memory scoring per pair (full ~11k-root registry): **< 5 s**.
  Observed on the 2026-06-24 run: median **3.2 s/pair**, max **3.7 s** (total
  38.1 s across the candidate pairs / 11,274 named roots) → **PASS**.
- Cache read (`list_cached_opening_scores` after one isolated recompute, under
  `--write-bench`): **< 50 ms**. Observed **38.96 ms** over 216 cached rows on an
  isolated `ghostreplay_calibrate` Postgres copy (earlier write-bench run; the
  2026-06-24 run was read-only, so cache read was `n/a`) → **PASS**.

**Source mix (2026-06-24 run):** 99.6% `session_eval` / 0.4% `analysis_cache`
(17,693 observations; 128 sessions excluded for broken continuity). **Horizon:**
opening-interval length mean 15.3 plies; 1,253 of 2,345 samples reached
middlegame. **Cohort:** 6 of 13 candidate pairs included (`min_observations = 20`).

## Why This Is The Final Recommendation

This version is the best fit for GhostReplay's actual training loop:

- it measures opening knowledge from real move outcomes, not only stored blunders
- it keeps the crucial opponent-turn vs user-turn distinction
- it rewards learning multiple self-lines without demanding every legal alternative
- it keeps weak repertoire lines visible instead of washing them out
- it treats side-specific identity as a real part of the data model
- it directly exposes the engine-exposure problem through `Coverage` and `underexposed_branch`

That makes it suitable for the dedicated opening page you want:

- one honest score per opening
- drill-down into sub-openings like `Queen's Gambit Declined`
- clear explanations for whether a branch is weak, uncertain, or simply underexposed
