# Opening Score Proposal v4

## Summary

This proposal combines the strongest ideas from `openingscore1.md`, `openingscore2.md`, and `openingscore3.md` into one scoring model that fits GhostReplay's actual data and training loop.

The headline idea is:

- show one primary `Opening Score` that means "how well you know the opening frontier you have actually trained"
- show `Confidence` beside it so lucky small samples do not look authoritative
- show `Coverage` beside it so the score does not pretend the engine exposed every important sideline

The score should be recursive over an opening tree, but it should treat opponent choices and user choices differently:

- on opponent turns, breadth matters, so important replies should all contribute
- on user turns, repertoire choice matters, so the score should not require mastering every legal book move from the same position

That distinction is the main addition in this version.

## Why the Earlier Plans Need to Be Merged

### Keep from `openingscore1.md`

- Use the existing move graph and recursive aggregation.
- Separate `Mastery` from `Coverage`.
- Add confidence damping and optional recency decay.

### Keep from `openingscore2.md`

- Treat this as a product feature, not just a formula.
- Present the dashboard as mastery plus coverage, not fake completeness.

### Keep from `openingscore3.md`

- Base the score on full opening decisions, not just `blunders`.
- Use an "expected discounted mastered depth" framing.
- Keep `Mastery`, `Confidence`, and `Coverage` separate.

### Reject

- Do not score only from `blunders`.
  GhostReplay stores full move outcomes in `session_moves`, while auto-capture records only the first opening blunder in a game.
- Do not treat unseen lines as neutral `0.5`.
  That inflates scores.
- Do not weight branches only by observed engine frequency.
  That bakes engine sampling bias into the score.
- Do not require the user to know every move on their own turns.
  That measures book breadth, not repertoire knowledge.

## Product Meaning

The headline `Opening Score` should mean:

> If this opening appears again, how reliably can the user navigate the important branches of their prepared repertoire without a recordable mistake?

This is intentionally not:

- "How completely do you know all theory forever?"
- "Have you finished this opening?"
- "Can you name every obscure sideline?"

Those are not realistic goals for this product.

## The Three Metrics

### 1. Opening Score (headline)

`Opening Score` is the normalized recursive mastery score on a 0-100 scale.

It rewards:

- going deeper without mistakes
- handling multiple opponent replies
- proving the same decisions repeatedly

It does not try to claim absolute completeness.

### 2. Confidence

`Confidence` is how much evidence supports the opening score.

It should increase when the user:

- reaches the same decision point many times in normal play
- passes SRS reviews on that decision point
- has practiced the line recently

Confidence answers:

> Should we trust this score yet?

### 3. Coverage

`Coverage` is how much of the opening's important opponent-response tree the user has actually been exposed to and tested on.

Coverage answers:

> How much of this opening's important breadth have you really seen?

This is the metric that guards against the "the engine never gave me the offbeat line" problem.

## Data Sources

Use four data sources together:

1. `session_moves`
   Primary evidence for whether the user played acceptable opening moves from a position.
2. `blunders`
   Identifies positions that are explicitly in the user's ghost library and provides `pass_streak`.
3. `blunder_reviews`
   Provides explicit spaced-repetition review history and stronger confidence evidence.
4. Opening book lookup
   Maps normalized FENs to opening family and variation labels in a transposition-aware way.

## Tree Definition

### Reference Tree

Build a reference opening tree from the opening book, keyed by normalized FEN.

This tree defines:

- the canonical family and variation labels
- the set of known book branches
- the parent/child relationships used for recursive scoring

### User Evidence Overlay

Overlay the user's evidence on top of the reference tree by normalized FEN.

For each book position, store:

- number of live attempts from `session_moves`
- number of live passes and fails
- whether the position is in `blunders`
- `pass_streak`
- review count and review pass/fail counts
- last live attempt timestamp
- last review timestamp

### Book Exit Extension

Do not stop exactly at the end of ECO labeling.

Instead:

- score the full labeled opening tree from the book
- allow a small personal extension beyond the last labeled book node, such as 2-4 user decisions
- apply a stronger depth discount beyond book exit

This rewards deeper preparation without pretending the opening has a clean universal endpoint.

## Core Scoring Model

### Step 1: Determine Pass/Fail at User Decision Nodes

At a user decision node, derive pass/fail from `session_moves.eval_delta`, not from move classification names.

- pass: `eval_delta < 50`
- fail: `eval_delta >= 50`

This matches the actual SRS pass threshold and avoids inventing a second definition of "knowing a move."

### Step 2: Estimate Local User-Move Reliability

For a user decision node `n`:

```text
live_passes_n = count of session_moves from n with eval_delta < 50
live_fails_n  = count of session_moves from n with eval_delta >= 50

p_n = (live_passes_n + alpha) / (live_passes_n + live_fails_n + alpha + beta)
```

Where:

- `p_n` is the estimated probability the user plays an acceptable move from this position
- `alpha` and `beta` are Bayesian smoothing constants

Recommended starting values:

- `alpha = 1`
- `beta = 2`

This creates a skeptical prior so one lucky pass does not produce a perfect score.

### Step 3: Compute Local Confidence

Confidence should reflect sample size, SRS reinforcement, and freshness.

For node `n`:

```text
live_attempts_n = live_passes_n + live_fails_n
review_attempts_n = number of blunder_reviews at n

sample_conf_n = 1 - exp(-live_attempts_n / k_live)
review_conf_n = 1 - exp(-(pass_streak_n + review_attempts_n) / k_review)
freshness_n = exp(-days_since_last_touch_n / half_life_days)

c_n = sample_conf_n * (0.7 + 0.3 * review_conf_n) * freshness_n
```

Recommended starting values:

- `k_live = 5`
- `k_review = 4`
- `half_life_days = 45`

This makes repeated SRS passes increase confidence without double-counting them as independent live-game mastery.

### Step 4: Recursive Mastery Aggregation

Use different recursion rules based on whose turn it is.

#### Opponent-to-move node

At opponent nodes, breadth matters:

```text
M_opp(n) = sum over children e of w_e * M(child_e)
```

Where:

- `w_e` is the importance weight of the opponent reply
- child weights sum to 1, including any reserved unknown bucket

#### User-to-move node

At user nodes, repertoire choice matters:

```text
M_user(n) = p_n * (1 + gamma * max over prepared children e of M(child_e))
```

Where:

- `p_n` gates all deeper credit
- `max` means the user is not penalized for not learning every own-move branch
- "prepared children" means book or personally studied continuations that the user has actually played or manually captured

If the user has no prepared child yet:

```text
M_user(n) = p_n
```

#### Perfect normalization

Compute the same recursion with:

- `p_n = 1`
- `gamma` unchanged
- the same tree structure

Then:

```text
OpeningScore = 100 * M(root) / PerfectM(root)
```

Recommended starting value:

- `gamma = 0.8`

This gives a stable "expected discounted mastered depth" score.

## Branch Weights

Branch weighting is critical.

### Rule

Do not derive branch weights only from observed user history or engine frequency.

Observed frequency is useful for analytics, but not as the primary definition of importance.

### MVP weighting

For MVP, use opening-book branch structure with:

- equal weight across known book replies at an opponent node
- a reserved `unknown replies` bucket of 10-15%

Example:

- known book replies share 0.85 total weight
- unknown bucket gets 0.15

If the user has not seen those unknown replies, coverage stays below 100 and the UI can explain why.

### Later improvement

If a reliable popularity source is added later, replace equal sibling weights with popularity-based weights for opponent replies only.

## Coverage Model

Coverage should be opponent-centric.

That means:

- count important opponent branches the user has faced
- do not punish the user for not learning multiple repertoire moves from the same user-turn node

For an opponent node:

```text
covered_weight(n) = sum of w_e for child replies with enough evidence
coverage(n) = covered_weight(n) / total_weight(n)
```

Define "enough evidence" conservatively, for example:

- at least 2 live attempts in the child subtree, or
- at least 1 live attempt plus an SRS review on that subtree

Aggregate coverage recursively across opponent nodes in the opening.

Output:

- `Coverage` on a 0-100 scale
- optional `unknown_weight` for UI explanation

## Confidence Aggregation

Aggregate node confidence through the same tree, but do not multiply it directly into mastery.

Use a parallel recursion:

```text
C_opp(n) = sum over children e of w_e * C(child_e)
C_user(n) = c_n * max over prepared children e of C(child_e)
```

At a user leaf:

```text
C_user(n) = c_n
```

Then normalize to 0-100.

This keeps the meanings clean:

- mastery says how well the line is known
- confidence says how trustworthy that estimate is

## Family Scores vs Variation Scores

The dashboard should support two levels.

### Opening family

Examples:

- `Sicilian Defense`
- `Italian Game`

Family scores roll up all important opponent replies under the family root.

### Exact variation

Examples:

- `Sicilian Defense: Alapin Variation`
- `Italian Game: Evans Gambit`

Variation scores use the exact named subtree and are more interpretable for detailed training feedback.

Recommendation:

- show opening families on the main dashboard
- show exact variations on drill-down

## Output Metrics Per Card

For each opening family or variation, return:

- `opening_score`
- `confidence`
- `coverage`
- `weighted_depth`
- `last_practiced_at`
- `strongest_branch`
- `weakest_branch`
- `sample_size`

### Weighted depth

Also compute a human-readable depth metric:

```text
weighted_depth = M(root) when each mastered user decision contributes 1 ply-equivalent
```

This is useful because "74" alone is abstract, while "weighted depth 5.8" is easier to interpret.

## UI Recommendation

### Dashboard card

Show:

- opening name
- `Opening Score`
- `Confidence`
- `Coverage`
- weighted depth
- strongest branch
- weakest branch

### Detail view

Use a tree, treemap, or sunburst with:

- color for mastery
- opacity for confidence
- hatch or gray treatment for uncovered opponent branches

Do not imply the user is expected to learn every own-move alternative.

## Implementation Plan

### Phase 1: Offline calculator or debug endpoint

Build an `OpeningScoreCalculator` service that:

1. loads the opening reference tree
2. joins user evidence from `session_moves`, `blunders`, and `blunder_reviews`
3. computes recursive mastery, confidence, and coverage
4. returns scores for one opening family or one exact variation

This phase is for tuning the formulas.

### Phase 2: Stats endpoint

Add an endpoint such as:

```text
GET /api/stats/openings
GET /api/stats/openings/{opening_key}
```

Return the dashboard card metrics and branch summaries.

### Phase 3: Cached scores

If on-demand recursion is too expensive, add a cached table such as:

```text
user_opening_scores(
  user_id,
  opening_key,
  opening_type,
  opening_score,
  confidence,
  coverage,
  weighted_depth,
  sample_size,
  computed_at
)
```

Recompute:

- after session upload
- after SRS review
- or in a background batch

## Tuning Notes

These constants will need tuning with real user data:

- `alpha`, `beta`
- `gamma`
- `k_live`
- `k_review`
- `half_life_days`
- unknown-branch reserved weight
- the evidence threshold for coverage

The product should ship with the formulas exposed behind a debug view so scores can be inspected against real trees before they become user-facing.

## Open Questions

1. Should book-exit extension be 2 user decisions, 4 plies, or a confidence-based stop rule?
2. Should manual captures count as prepared children immediately, or only after one successful review?
3. Should coverage treat rare but named sidelines differently from the unknown bucket?
4. Should family cards sort by opening score, by weakness, or by confidence-adjusted weakness?

## Recommendation

Ship this as:

- one headline `Opening Score`
- two supporting metrics: `Confidence` and `Coverage`
- an opening-family dashboard with variation drill-down

The score should be based on full move outcomes from `session_moves`, with SRS data strengthening confidence, and with recursive aggregation that sums important opponent replies but takes the best prepared continuation on the user's own turns.

That best matches how GhostReplay actually teaches openings.
