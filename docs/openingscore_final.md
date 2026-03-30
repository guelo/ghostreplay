# Opening Score Final Specification

## Recommendation

Adopt a side-specific, repertoire-aware opening score with three separate metrics:

- `Opening Score`: how well the user currently navigates the opening tree
- `Confidence`: how much evidence supports that score
- `Coverage`: how much of the important opponent-response tree has actually been exposed

This final plan keeps the best product framing from v7 and the better recursion model from v8, with one important refinement:

> On user turns, aggregate across the user's prepared repertoire, but weight branches by actual repertoire usage, not by successful plays.

That avoids the main failure mode in v8's draft weighting: a weak but frequently chosen line should not disappear just because the user keeps failing it.

## What This Plan Keeps

### Keep from v7

- separate `Opening Score`, `Confidence`, and `Coverage`
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

### 4. Book-exit extension

There is no clean universal endpoint for an opening, so do not stop exactly at the last named book node.

For MVP:

- score the full named book subtree
- allow up to `2` additional user decisions beyond the last book node
- apply the normal depth discount in the extension
- stop immediately when user evidence ends

This keeps the model honest: deeper personal prep gets credit, but the score does not silently turn into a middlegame score.

## Core Metrics

### Metric 1: Opening Score

`Opening Score` is the normalized recursive mastery score on a `0-100` scale.

It should reward:

- surviving deeper into the tree without mistakes
- handling multiple important opponent replies
- knowing multiple prepared self-lines

It should not penalize:

- not learning every legal move on your own turn

### Metric 2: Confidence

`Confidence` is the trust level of the score on a `0-100` scale.

It rises with:

- more evidence
- more recent evidence
- intentional review evidence

It is displayed next to the score, not multiplied into it.

### Metric 3: Coverage

`Coverage` is the fraction of important opponent-response weight that has actually been exposed with enough evidence to be meaningful.

It exists to separate:

- "you fail here"
- "you have barely been shown this branch"

## Local Statistics

### User-node mastery

At a user-to-move node `n`:

```text
attempts_n = live_passes_n + live_fails_n

p_n = (live_passes_n + alpha) / (attempts_n + alpha + beta)
```

Recommended starting values:

- `alpha = 1`
- `beta = 2`

This gives a skeptical prior, so one clean result does not look mastered.

### Pass/fail rule

At a user node:

- `pass` if `eval_delta < 50`
- `fail` if `eval_delta >= 50`

This matches the existing Ghost/SRS threshold and keeps the score aligned with the rest of the product.

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
S_opp(n) = sum over known children e of w_e * S(child_e)
```

Where `w_e` are opponent reply weights over known book replies.

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
- `Coverage` separately judges how much of the important opponent tree they have faced

## Confidence Model

Confidence should be parallel to mastery, not multiplied into it.

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

- `2` or more live attempts
- `1` live attempt plus `1` review event

These thresholds are deliberately conservative. A single accidental appearance should not count as meaningful exposure.

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
| `coverage_live_threshold` | 2 |
| `coverage_review_threshold` | `1 live + 1 review` |
| `book_exit_extension_user_decisions` | 2 |

These should stay configurable in a debug view during tuning.

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
