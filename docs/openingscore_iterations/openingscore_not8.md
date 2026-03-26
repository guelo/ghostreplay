# Opening Score Specification v8

## Goal

Give the user a quantitative measure of their opening knowledge, broken down by opening family (e.g. "Sicilian Defense") and exact variation (e.g. "Sicilian, Kan"). The score should help them track progress, find weak spots, and feel progression as they practice with SRS.

## Three Headline Metrics

| Metric     | Meaning                                                       | Range |
|------------|---------------------------------------------------------------|-------|
| Mastery    | How well you play the lines in your prepared repertoire       | 0–100 |
| Confidence | How much evidence supports that mastery score                 | 0–100 |
| Coverage   | How much of the known opponent-response tree you've been exposed to | 0–100 |

**Product meaning of the headline Mastery score:**

> If this opening appears again, how reliably can you navigate your prepared repertoire without a recordable mistake?

This is intentionally *not* "have you memorized every obscure sideline" or "have you finished this opening." Those are not realistic goals.

## Key Design Decisions

1. **Score from `session_moves`, not just `blunders`.** Auto-capture records only the first opening blunder per game, so many opening mistakes never appear in the blunders table. `session_moves` records every move classified and is the primary evidence source.

2. **Distinguish user-turn from opponent-turn.** At opponent-turn nodes, breadth matters — you need to handle whatever they play. At user-turn nodes, repertoire choice matters — you are not penalized for not learning every legal book alternative from the same position.

3. **No fake 100% scores.** Bayesian smoothing prevents "one lucky pass = mastered." A reserved unknown-branch weight prevents "engine never showed me that line = full marks." Unseen lines are not scored as neutral 0.5.

4. **Branch weights from the book, not observed frequency.** Observed frequency bakes in engine sampling bias. Use equal weight across known book replies (MVP) with a reserved unknown bucket.

5. **Confidence and mastery stay separate.** Confidence is never multiplied into mastery. They answer different questions: "how well?" vs "should we trust this yet?"

## Data Sources

| What              | Source                          | Key Columns                                         |
|-------------------|---------------------------------|------------------------------------------------------|
| Position graph    | `positions` + `moves`           | `fen_hash`, `from_position_id`, `to_position_id`    |
| Move quality      | `session_moves`                 | `eval_delta`, `fen_before`, `move_san`, `best_move_san` |
| SRS mastery       | `blunders`                      | `position_id`, `pass_streak`, `bad_move_san`, `best_move_san` |
| SRS history       | `blunder_reviews`               | `passed`, `move_played_san`                          |
| Opening lookup    | `eco.byPosition.json`           | normalized FEN → `{eco, name}`                       |
| Recency           | `blunders.last_reviewed_at`, `game_sessions.ended_at` | timestamps                          |

### Data gap

`session_moves` does not link back to the position graph's `position_id`. Two options:

1. **Join via FEN:** `session_moves.fen_before` → normalize → match `positions.fen_hash`. Works for a first pass since the opening tree is small (~100–500 nodes per ECO code).
2. **Add a column:** `session_moves.position_id` FK — cleaner, requires migration + backfill.

Option 1 is fine for MVP.

## Building the Opening Tree

1. Walk the `positions` + `moves` graph from the start position.
2. At each position, look up the ECO code via `eco.byPosition.json` (7,484 indexed FENs, fits in memory).
3. Assign each position to the **deepest** ECO code it matches (e.g. "B42 Sicilian, Kan" not just "B40 Sicilian Defense").
4. Group nodes into subtrees by ECO code for family-level and variation-level scoring.
5. **Book-exit extension:** Do not stop exactly at the last ECO-tagged node. Allow 2–4 user decisions beyond it with a steeper depth discount. This rewards deeper preparation without pretending the opening has a clean endpoint.

## Scoring Model

### Step 1 — Pass/fail at user decision nodes

Derive pass/fail from `session_moves.eval_delta`, not from move classification names:

- **pass:** `eval_delta < 50`
- **fail:** `eval_delta >= 50`

This matches the SRS pass threshold and avoids inventing a second definition of "knowing a move."

### Step 2 — Local mastery at user-turn nodes

For a user decision node *n*:

```
live_passes  = count of session_moves from n with eval_delta < 50
live_fails   = count of session_moves from n with eval_delta >= 50

g_session = (live_passes + α) / (live_passes + live_fails + α + β)
```

**SRS boost:** If this node has a blunder record, compute a parallel gate from pass_streak:

```
g_srs = (pass_streak + α) / (pass_streak + fails + α + β)
```

Take the stronger signal:

```
g_n = max(g_session, g_srs)
```

SRS pass_streak is spaced-repetition-tested, so it can be stronger evidence than raw live play.

**Priors:** `α = 1, β = 2` (skeptical). This means:

- 0 attempts → g = 0.33 (not zero, not generous)
- 1/1 correct → g = 0.50
- 5/5 correct → g = 0.75
- 10/10 correct → g = 0.85

### Step 3 — Recursive mastery aggregation

Different recursion rules for each side.

**Opponent-to-move node** — breadth matters:

```
M(n) = Σ  w_e × M(child_e)
       children e
```

where `w_e` is the importance weight of the opponent reply and child weights sum to 1 (including the unknown bucket).

**User-to-move node** — repertoire choice matters:

```
M(n) = g_n × (1 + γ × max  M(child_e))
                        prepared
                        children e
```

- `g_n` gates all deeper credit: if you fail here, nothing downstream counts.
- `max` over prepared children means the user is not penalized for not learning every own-move branch.
- "Prepared children" = book or personally studied continuations the user has actually played or manually captured.
- If no prepared child yet: `M(n) = g_n`

**Normalizing to 0–100:**

Compute a `PerfectM(root)` using the same recursion with all `g_n = 1` and the same tree structure. Then:

```
Mastery = 100 × M(root) / PerfectM(root)
```

**Constants:** `γ = 0.8`

### Step 4 — Local confidence

Per-node confidence reflects sample size, SRS reinforcement, and freshness.

```
live_attempts    = live_passes + live_fails
review_attempts  = count of blunder_reviews at n

sample_conf  = 1 - exp(-live_attempts / k_live)
review_conf  = 1 - exp(-(pass_streak + review_attempts) / k_review)
freshness    = exp(-days_since_last_touch / half_life)

c_n = sample_conf × (0.7 + 0.3 × review_conf) × freshness
```

This means:
- 1 live attempt → 18% sample confidence
- 5 live attempts → 63%
- 10 live attempts → 87%
- A line untouched for 4 months fades to ~25% freshness

SRS passes increase confidence without double-counting as independent live-game mastery.

**Constants:** `k_live = 5`, `k_review = 4`, `half_life = 45 days`

### Step 5 — Confidence aggregation

Aggregate confidence through the same tree structure via a parallel recursion:

```
C_opp(n) = Σ  w_e × C(child_e)
C_user(n) = c_n × max  C(child_e)
                   prepared children
```

At a user leaf: `C_user(n) = c_n`. Normalize to 0–100.

### Step 6 — Coverage

Coverage is **opponent-centric** — it counts important opponent branches the user has faced, and does not punish for not learning multiple repertoire moves from the same user-turn node.

At each opponent node:

```
covered_weight = Σ w_e   for child replies with enough evidence
coverage(n) = covered_weight / total_weight
```

**"Enough evidence"** (conservative):
- at least 2 live attempts in the child subtree, or
- at least 1 live attempt plus an SRS review on that subtree

Aggregate coverage recursively across opponent nodes. Output on a 0–100 scale.

## Branch Weights (MVP)

At opponent-turn nodes, use the opening-book branch structure:

- Known book replies share **85%** of total weight equally.
- A reserved **unknown replies** bucket gets **15%**.

If the user has not encountered those unknown replies, coverage stays below 100 and the UI explains why.

**Later improvement:** Replace equal sibling weights with popularity-based weights (from a master or Lichess database) for opponent replies only.

## Output Per Opening

For each opening family or variation, return:

| Field            | Description                                      |
|------------------|--------------------------------------------------|
| `mastery`        | 0–100 recursive mastery score                    |
| `confidence`     | 0–100 aggregated confidence                      |
| `coverage`       | 0–100 opponent-centric coverage                  |
| `weighted_depth` | Human-readable depth (e.g. "5.8 plies")          |
| `strongest_branch` | Best-scoring named subtree                     |
| `weakest_branch`   | Worst-scoring named subtree                    |
| `sample_size`    | Total live attempts across tree                  |
| `last_practiced_at` | Timestamp of most recent activity             |

**Weighted depth:** Computed from `M(root)` where each mastered user decision contributes 1 ply-equivalent. More interpretable than a bare 0–100 number.

## Architecture

### Backend: `OpeningScoreCalculator` service

1. Load the ECO position index into memory (~1 MB, 7,484 FENs).
2. Walk the user's position graph from root.
3. Join user evidence from `session_moves`, `blunders`, `blunder_reviews` by normalized FEN.
4. Compute recursive mastery, confidence, and coverage.
5. Return scores for one opening family or one exact variation.

Tree walk is O(nodes) per opening — typically <500 nodes, fast enough for batch.

### API

```
GET /api/stats/openings              → all opening family cards
GET /api/stats/openings/{eco_code}   → single opening detail with branch breakdown
```

### Caching

Compute scores as a batch job (not on-demand) after:
- game session upload
- SRS review completion
- or nightly

Write results to:

```sql
user_opening_scores (
  user_id,
  eco_code,
  opening_name,
  opening_type,     -- 'family' or 'variation'
  mastery,
  confidence,
  coverage,
  weighted_depth,
  sample_size,
  strongest_branch,
  weakest_branch,
  computed_at
)
```

## Dashboard UX

### Family list view

Show opening families on the main dashboard. Each card:

```
Sicilian Defense: Najdorf
Mastery  74    Confidence  61    Coverage  48
Depth: 5.8 plies
Strongest: Main Line (6. Bg5)   ████████░░  82
Weakest:   English Attack        ███░░░░░░░  31
```

### Variation drill-down

Clicking a family card opens the variation-level view with individual variation cards.

### Sunburst / tree visualization

On the detail view:
- **Rings** = ply depth
- **Segments** = branches
- **Color** = mastery (red → yellow → green)
- **Opacity/saturation** = confidence (faded = lucky/new, solid = proven)
- **Gray / hatched** = uncovered opponent branches

Clicking a segment shows that subtree's details.

Do not imply the user is expected to learn every own-move alternative.

## Tuning Constants

| Constant          | Starting Value | Controls                              |
|-------------------|----------------|---------------------------------------|
| `α` (prior)       | 1              | Bayesian smoothing — pseudo-passes    |
| `β` (prior)       | 2              | Bayesian smoothing — pseudo-fails     |
| `γ` (discount)    | 0.8            | How much deeper plies contribute      |
| `k_live`          | 5              | Live-attempt confidence scaling       |
| `k_review`        | 4              | SRS-review confidence scaling         |
| `half_life`       | 45 days        | Recency decay rate                    |
| Unknown bucket    | 15%            | Reserved weight for unseen opponent replies |
| Coverage threshold| 2 live attempts (or 1 + SRS) | Minimum evidence to count a branch as covered |

Ship with these exposed behind a debug view so scores can be inspected against real trees before they become user-facing.

## Implementation Phases

1. **Offline calculator / debug endpoint** — Build `OpeningScoreCalculator`, tune formulas against real user data. No UI yet.
2. **Stats endpoint** — Wire up the API routes, return dashboard card metrics.
3. **Cached scores** — Add the `user_opening_scores` table and batch recomputation triggers.
4. **Dashboard UI** — Family cards, variation drill-down, sunburst visualization.

## Open Questions

1. Should book-exit extension be 2 user decisions, 4 plies, or a confidence-based stop rule?
2. Should manual captures count as prepared children immediately, or only after one successful review?
3. Should coverage treat rare but named sidelines differently from the unknown bucket?
4. Should family cards sort by mastery, by weakness, or by confidence-adjusted weakness?
