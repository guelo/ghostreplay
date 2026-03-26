# Opening Score Proposal v8

## Summary

This plan keeps the best ideas from `openingscore4.md`, `openingscore5.md`, and `openingscore6.md`, but tightens two things they still left fuzzy:

- the score must reward learning multiple sub-lines of your repertoire, not just the single best child
- the implementation should map cleanly onto the opening-book assets and schema that already exist in this repo

The result is a side-specific opening score built from a reference opening tree plus user evidence:

- `Opening Score` answers: "How well can you navigate this opening tree right now?"
- `Confidence` answers: "How much evidence supports that score?"
- `Coverage` answers: "How much of the important opponent-response tree have you actually seen?"

`Opening Score` is recursive, depth-sensitive, and breadth-aware on opponent turns, but it does not require the user to learn every legal move on their own turns.

## What This Keeps From v4, v5, and v6

### Keep from v4

- separate `Opening Score`, `Confidence`, and `Coverage`
- distinguish opponent turns from user turns in the recursion
- make coverage opponent-centric
- allow a small personal extension beyond the end of the labeled book

### Keep from v5

- use `session_moves` as the primary mastery signal
- ground the plan in the actual repo schema and opening-book files
- compute scores in a backend service and cache them
- return branch summaries for dashboard drill-down

### Keep from v6

- keep the product framing simple: one main score plus two supporting metrics
- keep the recursive "expected discounted mastered depth" intuition

### Reject

- scoring only from `blunders`
- treating unseen lines as neutral `0.5`
- using `max(child)` on user turns
- relying only on observed engine frequency for branch weights
- implying the user can ever "finish" an opening in a strict sense

`max(child)` is the main correction in this version. It solves "pick one line and ignore the rest," but it undercounts users who genuinely know multiple sub-openings. User-turn recursion should aggregate across the user's prepared repertoire, not only their strongest continuation.

## Product Meaning

The headline score should mean:

> If this opening appears again from this side, how reliably can the user stay on track through the important opponent replies in the repertoire they have actually trained?

This is intentionally not:

- a claim of total theoretical completeness
- a claim that every obscure sideline has been tested
- a promise that the engine exposed every relevant branch fairly

That is why `Confidence` and `Coverage` are shown next to the main score, not hidden.

## Scope and Identity

Opening scores must be side-specific.

The user should have separate stats for:

- `Sicilian Defense as White`
- `Sicilian Defense as Black`

Score records should therefore be keyed at minimum by:

- `user_id`
- `player_color`
- `opening_key`

Where `opening_key` is the normalized FEN of the named subtree root. The display label should be stored separately from the key.

## Tree Definition

### 1. Reference opening tree

Build a transposition-aware reference tree from the existing book assets:

- `public/data/openings/eco.json`
- `public/data/openings/eco.byPosition.json`

Use `eco.json` to build the actual move graph and sibling relationships.
Use `eco.byPosition.json` to label normalized FENs with the deepest known opening name.

Key every node by normalized FEN using the same normalization rule already used by the frontend opening lookup:

- first 4 FEN fields only
- canonical en-passant handling via chess rules, not raw string slicing alone

This gives:

- transposition-aware nodes
- named subtree roots for family/variation drill-down
- known book children at each node for breadth and coverage

### 2. User evidence overlay

Overlay user evidence onto the reference tree from:

1. `session_moves`
2. `game_sessions`
3. `blunders`
4. `blunder_reviews`

For each node and edge, collect:

- live attempts
- live passes
- live fails
- successful child frequencies
- last live attempt timestamp
- review count
- review pass/fail counts
- last review timestamp
- whether the node/edge is in the ghost library

### 3. Book-exit extension

There is no universal opening cutoff, so do not stop exactly at the last named ECO node.

Instead:

- score the full named book subtree
- allow a personal extension of up to `2` additional user decisions beyond the last book node
- apply the normal depth discount in the extension
- stop extension when there is no user evidence

This rewards deeper preparation without pretending the opening has a hard endpoint.

## Data Rules

### Primary mastery signal

`session_moves` is the primary mastery source.

Reason:

- the app records only the first auto-blunder per session in `blunders`
- opening knowledge is broader than stored ghost targets
- the user may repeatedly play a line correctly without it ever becoming a current blunder target

### Pass/fail rule

At a user decision node, treat the move as:

- `pass` if `eval_delta < 50`
- `fail` if `eval_delta >= 50`

This matches the existing SRS threshold and avoids inventing a second definition of "knowing the move."

### What `blunders` and `blunder_reviews` are for

Use `blunders` and `blunder_reviews` mainly for:

- confidence
- recency
- evidence that a branch is an intentional training target

Do not make them the only mastery signal.

Also do not let review rows dominate mastery directly, because those review events already occur inside game sessions and are therefore already represented in `session_moves`.

## Core Model

### Metric 1: Opening Score

`Opening Score` is the normalized recursive score on a `0-100` scale.

It should reward:

- surviving deeper into the tree without a mistake
- handling multiple important opponent replies
- knowing multiple prepared self-lines

### Metric 2: Confidence

`Confidence` is the trust level of the score on a `0-100` scale.

It should rise with:

- more attempts
- repeated review evidence
- recency

### Metric 3: Coverage

`Coverage` is the fraction of important opponent-response weight that has actually been exposed and tested.

It exists specifically to separate:

- "you fail here a lot"
- "the engine has barely shown you this branch"

## Local Node Statistics

### User decision reliability

For a user-to-move node `n`:

```text
attempts_n = live_passes_n + live_fails_n

p_n = (live_passes_n + alpha) / (attempts_n + alpha + beta)
```

Recommended starting values:

- `alpha = 1`
- `beta = 2`

This gives a skeptical prior so one good result does not look mastered.

### Prepared repertoire children

At a user node, define `prepared_children(n)` as children with at least one of:

- at least 1 passed live attempt on that child edge
- a descendant ghost target in `blunders`
- a manual capture or explicit training marker, if that concept is later added

For prepared children, define repertoire weights:

```text
r_e = (successful_plays_e + rho) /
      sum over prepared children j of (successful_plays_j + rho)
```

Recommended starting value:

- `rho = 1`

These weights are normalized only across prepared self-lines.

That means:

- learning two prepared sub-lines should improve the score
- not learning every legal self-move should not be a penalty

### Opponent branch weights

At opponent-to-move nodes, breadth matters.

For MVP:

- split `0.90` weight equally across known book replies
- reserve `0.10` as an `unknown_reply` bucket

If popularity data is added later, replace equal weights with popularity weights for opponent replies only.

Do not use observed engine frequency as the primary importance definition. That would bake engine sampling bias into the score.

## Recursive Aggregation

Let `S(n)` be the recursive opening score before normalization.

Use `gamma = 0.8` as the initial depth discount.

### Opponent node

At opponent nodes, aggregate across important replies:

```text
S_opp(n) = sum over known children e of w_e * S(child_e)
```

Where:

- `w_e` are normalized opponent reply weights
- the reserved unknown bucket contributes `0` until exposed and modeled

### User node

At user nodes, gate deeper credit by local reliability, then aggregate across the user's prepared repertoire:

```text
S_user(n) = p_n * (1 + gamma * sum over prepared children e of r_e * S(child_e))
```

If there are no prepared children:

```text
S_user(n) = p_n
```

This is the key behavior of v8:

- deeper knowledge matters
- multiple prepared self-lines matter
- unprepared self-lines do not drag the score down

### Perfect normalization

Compute `PerfectS(root)` on the same tree with:

- every user reliability term set to `1`
- the same opponent weights
- the same prepared-child set
- the same depth discount

Then:

```text
OpeningScore = 100 * S(root) / PerfectS(root)
```

This keeps the score interpretable across openings with different branching factors.

## Confidence Model

Confidence should be parallel to mastery, not multiplied into it.

That keeps the meanings clean:

- mastery: how well the line seems known
- confidence: how trustworthy that estimate is

### Local confidence

For user node `n`:

```text
live_attempts_n = live_passes_n + live_fails_n
review_attempts_n = review_passes_n + review_fails_n

evidence_n = live_attempts_n + lambda_review * review_attempts_n
sample_conf_n = 1 - exp(-evidence_n / k_evidence)
freshness_n = exp(-days_since_last_touch_n / half_life_days)

c_n = sample_conf_n * freshness_n
```

Recommended starting values:

- `lambda_review = 0.5`
- `k_evidence = 5`
- `half_life_days = 45`

### Recursive confidence

Use the same tree semantics as the score:

```text
C_opp(n) = sum over known children e of w_e * C(child_e)
C_user(n) = c_n * (1 if no prepared children else sum over prepared children e of r_e * C(child_e))
```

Then normalize to `0-100`.

This makes stale lines fade even if their historical score was once high.

## Coverage Model

Coverage should be opponent-centric.

The user should be judged on whether they have been exposed to important opponent replies, not on whether they have memorized every alternative move for themselves.

### Covered branch rule

An opponent reply branch counts as covered if its child subtree has at least one of:

- at least `2` live attempts
- `1` live attempt plus `1` review event

Those thresholds are deliberately conservative. A single accidental appearance should not imply real coverage.

### Recursive coverage

At opponent nodes:

```text
Cov_opp(n) = sum over known children e of w_e * covered_e * Cov(child_e)
```

Where:

- `covered_e` is `1` if the child branch meets the evidence threshold, else `0`
- the unknown bucket contributes `0` unless later modeled explicitly

At user nodes:

```text
Cov_user(n) = 0, if there are no prepared children
Cov_user(n) = sum over prepared children e of r_e * Cov(child_e), otherwise
```

Leaves contribute `1` once reached.

Then:

```text
Coverage = 100 * Cov(root)
```

This lets a user have:

- high mastery in the lines they know
- lower coverage if many important opponent replies remain unseen

That is the correct outcome for the engine-exposure problem.

## Family Scores and Variation Scores

The UI should support two levels.

### Family cards

Main dashboard cards should show opening families, for example:

- `Sicilian Defense`
- `Italian Game`
- `Queen's Gambit Declined`

### Drill-down

Clicking a family card should reveal named descendant subtrees:

- exact variations
- major sub-openings
- strongest branches
- weakest branches
- underexposed branches

The drill-down tree should follow named book positions, not fragile string parsing alone.

## Output Per Opening

Each computed opening record should include:

- `opening_score`
- `confidence`
- `coverage`
- `weighted_depth`
- `sample_size`
- `last_practiced_at`
- `strongest_branch`
- `weakest_branch`
- `underexposed_branch`
- `player_color`
- `computed_at`

### Weighted depth

Also compute a human-readable depth number:

```text
weighted_depth = recursive expected mastered depth in ply-equivalents
```

This is useful because a score like `74` is abstract, while "weighted depth 5.8" tells the user how deep the opening tends to stay comfortable.

## Implementation Plan

### Phase 1: calculator and debug output

Build a backend `OpeningScoreCalculator` that:

1. loads the opening-book graph from `eco.json`
2. applies the same FEN normalization used by the frontend lookup
3. overlays user evidence from `session_moves`, `game_sessions`, `blunders`, and `blunder_reviews`
4. computes `opening_score`, `confidence`, `coverage`, and branch summaries for one opening root

Ship this first as:

- a debug script, or
- a debug-only API endpoint

This phase is for formula tuning.

### Phase 2: cached stats

Add a cache table such as:

```text
user_opening_scores(
  user_id,
  player_color,
  opening_key,
  opening_name,
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
- or in a background batch if needed

### Phase 3: dashboard

Add:

- opening family cards sorted by weakness opportunity
- a detail view for subtree drill-down
- UI states that distinguish:
  - low score because of repeated fails
  - low confidence because of sparse evidence
  - low coverage because the branch is underexposed

## MVP Data Constraints

This can be implemented with current data, but a later migration would make it cheaper.

### Works now

- `session_moves.fen_before` gives the pre-move position
- `session_moves.fen_after` gives the child position
- `game_sessions.player_color` scopes the score to the user's side
- `blunders` and `blunder_reviews` provide explicit training evidence

### Nice later migration

Add one or both of:

- normalized `fen_before` column on `session_moves`
- `position_id` foreign key on `session_moves`

MVP can normalize FEN on read. The tree sizes are small enough that this is acceptable for initial tuning.

## Tuning Knobs

These constants should be adjustable in a debug view:

- `alpha`, `beta`
- `rho`
- `gamma`
- `lambda_review`
- `k_evidence`
- `half_life_days`
- unknown opponent bucket weight
- coverage evidence threshold
- book-exit extension depth

## Why This Is the Recommended Plan

This version best matches the real GhostReplay training loop:

- it scores from actual move outcomes, not just stored blunders
- it respects the difference between opponent breadth and user repertoire choice
- it does not claim impossible completeness
- it still penalizes openings where important opponent replies remain unseen
- it rewards learning multiple sub-lines instead of collapsing everything to one best branch

That makes it a better fit for the dashboard you described: a meaningful opening score at the top level, with drill-down into sub-openings like `Queen's Gambit Declined` and clear explanations for whether a branch is weak, uncertain, or simply underexposed.
