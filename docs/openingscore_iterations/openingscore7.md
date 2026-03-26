# Opening Score — v7 Unified Specification

## Product Goal

Give users a dashboard showing how well they know each opening they practice. One headline score per opening, backed by two supporting metrics that keep the score honest.

**What "Opening Score" means:**

> If this opening appears again, how reliably can you navigate the important branches of your prepared repertoire without a mistake?

**What it does not mean:**

- "You've finished learning this opening" — there is no finish line.
- "You know every sideline" — that is Coverage's job to track.
- "You got lucky once" — that is Confidence's job to temper.

---

## Three Metrics

| Metric | Meaning | Range |
|---|---|---|
| **Opening Score** | How well you play the lines you've trained | 0–100 |
| **Confidence** | How much evidence supports that score | 0–100 |
| **Coverage** | How much of the known theory you've been exposed to | 0–100 |

These are always displayed together. A high Opening Score with low Confidence is a "probably fine but unproven" signal. A high Opening Score with low Coverage is a "strong in what you've seen, but blind spots exist" signal.

---

## The Key Insight: User Turns vs Opponent Turns

The opening tree alternates between positions where the user decides and positions where the opponent decides. These must be scored differently:

**Opponent-to-move nodes** — breadth matters. The user should be prepared for all important opponent replies, so every known reply contributes to the score weighted by importance.

**User-to-move nodes** — repertoire choice matters. The user picks one prepared continuation. They should not be penalized for not learning every alternative book move from the same position. The score gates on whether the user plays their chosen move correctly, then follows the best-prepared child.

This distinction is the single most important design decision in the scoring model.

---

## Data Sources

| What | Source | Key columns |
|---|---|---|
| Position graph | `positions` + `moves` | `fen_hash`, `from_position_id`, `to_position_id`, `move_san` |
| Move quality per visit | `session_moves` | `fen_before`, `move_san`, `eval_delta`, `best_move_san`, `classification` |
| SRS mastery | `blunders` | `position_id`, `pass_streak`, `bad_move_san`, `best_move_san` |
| SRS review history | `blunder_reviews` | `blunder_id`, `passed`, `move_played_san` |
| Recency | `blunders.last_reviewed_at`, `game_sessions.ended_at` | timestamps |
| Opening lookup | `eco.byPosition.json` | normalized FEN → `{eco, name}` (7,484 positions) |

### Data gap: session_moves → position linking

`session_moves` records every classified move but has no `position_id` foreign key. Linking a session move to the position graph requires FEN normalization:

```
session_moves.fen_before → normalize → match positions.fen_hash
```

This is acceptable for the opening tree, which is small (typically < 500 nodes per ECO code). No migration needed for v1 — join via FEN at computation time.

---

## Tree Construction

### Step 1: Build the reference tree from the ECO book

Load `eco.byPosition.json` (fits in memory at ~1 MB). Walk from the starting position FEN outward. Each position maps to its deepest matching ECO code and opening name.

Group positions into subtrees by ECO code. A position belongs to the deepest ECO code it matches — e.g., "B42 Sicilian, Kan" rather than just "B40 Sicilian Defense".

### Step 2: Overlay user evidence

For each ECO position that the user has visited, attach:

- Live attempt count, passes, fails (from `session_moves` via FEN join)
- Whether a `blunders` record exists at this position
- `pass_streak` from blunders
- Review count and pass/fail from `blunder_reviews`
- Last live attempt timestamp
- Last review timestamp

### Step 3: Extend beyond book exit

Do not stop at the last ECO-tagged node. If the user has positions deeper than the book boundary, include up to 4 additional user decisions as part of that opening's tree. Apply a steeper depth discount (γ² instead of γ) beyond the book boundary to reward deeper prep without letting the tree grow unbounded.

### Handling transpositions

The position graph is a DAG, not a tree. When the same FEN is reachable by multiple move orders, it appears once in the position graph (keyed by `fen_hash`). During recursive scoring, memoize by `position_id` to avoid recomputation.

---

## Core Scoring Model

### Step 1: Pass/fail at user decision nodes

At a user-to-move position, a move is:

- **pass**: `eval_delta < 50` (matches the existing SRS threshold)
- **fail**: `eval_delta >= 50`

This uses the same standard the SRS system already uses, so the score and the practice loop agree on what "knowing a move" means.

### Step 2: Local mastery at user nodes

For a user decision node `n`:

```
live_passes  = count of session_moves from n with eval_delta < 50
live_fails   = count of session_moves from n with eval_delta >= 50

p_session(n) = (live_passes + α) / (live_passes + live_fails + α + β)
```

With `α = 1, β = 2` (skeptical prior):

| Record | p_session |
|---|---|
| 0/0 (unseen) | 0.33 |
| 1/1 | 0.50 |
| 3/3 | 0.67 |
| 5/5 | 0.75 |
| 10/10 | 0.85 |
| 10/12 | 0.73 |

**SRS boost.** If this position has a blunder record, the pass_streak provides additional evidence:

```
p_srs(n) = (pass_streak + α) / (pass_streak + srs_fails + α + β)
```

Where `srs_fails` = total review failures from `blunder_reviews` for this blunder.

Take the stronger signal:

```
p(n) = max(p_session(n), p_srs(n))
```

The SRS signal is the strongest evidence — it is spaced-repetition-tested by definition.

### Step 3: Recursive aggregation

#### Opponent-to-move node

All known replies contribute, weighted by importance:

```
M(n) = Σ  w_e × M(child_e)
       e∈children
```

Where `w_e` is the importance weight of each opponent reply and weights sum to 1.0 (including an unknown-reply budget — see Branch Weights below).

#### User-to-move node

The user's mastery gates all deeper credit, and only the best-prepared line counts:

```
M(n) = p(n) × (1 + γ × max   M(child_e))
                         e∈prep
```

Where `prep` = book or personally studied continuations that the user has actually played or captured. If no prepared child exists:

```
M(n) = p(n)
```

**γ = 0.8** — deeper knowledge contributes but with diminishing returns.

#### User-to-move leaf (no children)

```
M(n) = p(n)
```

#### Opponent-to-move leaf (no children)

```
M(n) = 1.0
```

(Terminal opponent node contributes its full weight — the user already passed the previous user node to reach here.)

#### Normalization

Compute `PerfectM(root)` using the same recursion with all `p(n) = 1.0`:

```
Opening Score = 100 × M(root) / PerfectM(root)
```

This produces a stable 0–100 scale regardless of tree depth or shape.

### Worked example

Consider a small opening tree (user plays White):

```
1. e4     (user, p=0.9)
├── 1...e5   (opponent, w=0.6)
│   └── 2. Nf3  (user, p=0.85)
│       └── (leaf)
├── 1...c5   (opponent, w=0.25)
│   └── 2. Nf3  (user, p=0.70)
│       └── (leaf)
└── unknown  (opponent, w=0.15, M=0)

M(2.Nf3 after e5) = 0.85
M(1...e5 subtree) = 0.85
M(2.Nf3 after c5) = 0.70
M(1...c5 subtree) = 0.70

M(root) = 0.9 × (1 + 0.8 × [0.6×0.85 + 0.25×0.70 + 0.15×0])
        = 0.9 × (1 + 0.8 × [0.51 + 0.175 + 0])
        = 0.9 × (1 + 0.8 × 0.685)
        = 0.9 × 1.548
        = 1.393

PerfectM(root) = 1 × (1 + 0.8 × [0.6×1 + 0.25×1 + 0.15×0])
               = 1 × (1 + 0.8 × 0.85)
               = 1.68

Opening Score = 100 × 1.393 / 1.68 = 82.9
```

The unknown bucket caps the score below 100 even with perfect play on known lines.

---

## Branch Weights

### MVP: Equal + unknown budget

At each opponent node with `k` known book replies:

- Each known reply gets weight `0.85 / k`
- Unknown replies bucket gets weight `0.15` with `M = 0`

This avoids baking engine sampling bias into the score. The unknown bucket prevents inflated scores and maps directly to a Coverage gap the UI can explain.

### Future: Popularity-weighted

If a reliable popularity source is added later (e.g., Lichess master DB frequencies), replace equal sibling weights with popularity-based weights for opponent replies only. User-turn weighting does not change (always max over prepared children).

---

## Confidence

Confidence reflects how much the score should be trusted based on sample size, SRS reinforcement, and recency.

### Per-node confidence

```
sample_conf(n)  = 1 - exp(-attempts / k_live)        k_live = 5
srs_conf(n)     = 1 - exp(-pass_streak / k_srs)      k_srs = 4
freshness(n)    = exp(-days_stale / half_life)        half_life = 60 days

c(n) = sample_conf × max(0.6, 0.6 + 0.4 × srs_conf) × freshness
```

Where `days_stale` = days since the most recent of (last live attempt, last SRS review), and `attempts` = total live attempts from session_moves.

| Attempts | pass_streak | Days stale | c(n) |
|---|---|---|---|
| 1 | 0 | 0 | 0.11 |
| 5 | 0 | 0 | 0.38 |
| 5 | 3 | 0 | 0.50 |
| 10 | 5 | 0 | 0.78 |
| 10 | 5 | 60 | 0.39 |
| 10 | 5 | 120 | 0.20 |

Recency decay means a line you haven't practiced in 4 months fades to ~14% confidence, even if you once knew it well.

### Aggregated confidence

Use a parallel recursion through the same tree:

```
C_opp(n) = Σ  w_e × C(child_e)
           e

C_user(n) = c(n) × max   C(child_e)     (or c(n) at leaves)
                    e∈prep
```

Normalize to 0–100 the same way as mastery.

Confidence is displayed alongside the score but never multiplied into it. They answer different questions: "how well?" vs "how sure?"

---

## Coverage

Coverage is opponent-centric — it tracks how much of the important response tree the user has actually been exposed to.

### Per opponent node

```
covered_weight(n) = Σ w_e for children with sufficient evidence
coverage(n) = covered_weight(n) / total_weight(n)
```

"Sufficient evidence" means at least one of:
- 2+ live attempts in the child subtree, OR
- 1 live attempt + 1 SRS review on that subtree

### Why opponent-centric

The user should not be penalized for not learning every alternative book move on their own turns. Coverage only asks: "of the important opponent replies, which ones have you faced?"

### Aggregation

Weight coverage by importance through the tree:

```
Coverage(root) = weighted average of coverage(n) across all opponent nodes,
                 weighted by each node's contribution to PerfectM
```

Normalize to 0–100.

### The unknown bucket and Coverage

The 15% unknown-reply budget at each opponent node means Coverage can approach but never reach 100% from book moves alone. If the user encounters an off-book reply and handles it, that evidence can reduce the unknown bucket.

---

## Family Scores vs Variation Scores

The dashboard supports two levels of granularity.

**Opening family** — e.g., "Sicilian Defense", "Italian Game". Family scores roll up all important opponent replies under the family root. Shown on the main dashboard.

**Exact variation** — e.g., "Sicilian Defense: Najdorf", "Italian Game: Evans Gambit". Variation scores use the named subtree. Shown on drill-down.

A position's family and variation come from the deepest ECO match in `eco.byPosition.json`. Variations within a family share opponent-node structure at the top of the tree and diverge deeper.

---

## Output Per Opening Card

```
Sicilian Defense: Najdorf
Score: 74    Confidence: 61    Coverage: 48
Depth: 5.8 plies
Strongest: Main Line (6. Bg5)    ████████░░ 82
Weakest:   English Attack        ███░░░░░░░ 31
Last practiced: 3 days ago
```

Fields returned per opening:

- `opening_score` (0–100)
- `confidence` (0–100)
- `coverage` (0–100)
- `weighted_depth` — human-readable ply count (how deep your knowledge extends on average)
- `strongest_branch` — variation name + score
- `weakest_branch` — variation name + score
- `last_practiced_at` — timestamp
- `sample_size` — total live attempts across the tree

**Weighted depth** is the mastery recursion where each mastered user decision contributes 1 ply-equivalent. "Score 74" is abstract; "depth 5.8" is concrete.

---

## Visualization

### Detail view: Sunburst

Rings = ply depth. Segments = branches.

- **Color** = mastery (red → yellow → green)
- **Saturation** = confidence (faded = unproven, solid = proven)
- **Gray/hatched** = uncovered opponent branches

Clicking a segment shows that subtree's details. User-turn segments that the user hasn't prepared are simply absent (not shown as red), reinforcing that the score does not demand learning every alternative.

---

## Architecture

### Backend: `OpeningScoreCalculator` service

```
Input:  user_id, opening_key (ECO code or family slug)
Output: OpeningStats {
  opening_score, confidence, coverage,
  weighted_depth, strongest_branch, weakest_branch,
  last_practiced_at, sample_size
}
```

Process:
1. Load the ECO reference tree (cached in memory, ~1 MB).
2. Query the user's position graph for nodes within this opening.
3. Join `session_moves` via FEN normalization for pass/fail counts.
4. Join `blunders` + `blunder_reviews` via `position_id` for SRS data.
5. Walk the tree bottom-up, computing M(n), C(n), and coverage(n).
6. Normalize and return.

### API endpoints

```
GET /api/stats/openings              → list of opening family cards
GET /api/stats/openings/{eco_code}   → detail + branch breakdown
```

### Caching

Tree walks are O(nodes) per opening — typically < 500 nodes, so on-demand is fine initially.

If performance becomes a concern, add a cached table:

```sql
user_opening_scores (
  user_id        BIGINT,
  opening_key    TEXT,
  opening_type   TEXT,     -- 'family' or 'variation'
  opening_score  REAL,
  confidence     REAL,
  coverage       REAL,
  weighted_depth REAL,
  sample_size    INTEGER,
  computed_at    TIMESTAMPTZ
)
```

Recompute after session upload, after SRS review, or in a nightly batch.

---

## Implementation Phases

### Phase 1: Offline calculator

Build the `OpeningScoreCalculator` as a standalone service/script. Run it against real user data. Expose a debug view showing the tree with per-node scores. Tune constants.

### Phase 2: API + dashboard cards

Wire up the endpoints. Build the frontend dashboard with family cards. No caching yet — compute on demand.

### Phase 3: Drill-down + sunburst

Add the variation detail view with the sunburst visualization. This is the engagement hook — users will want to "fill in" gray segments.

### Phase 4: Caching + triggers

Add the cached scores table if needed. Recompute on session end and SRS review completion.

---

## Tuning Constants

| Constant | Starting value | What it controls |
|---|---|---|
| α (Bayesian) | 1 | Smoothing — pseudo-passes |
| β (Bayesian) | 2 | Smoothing — pseudo-fails (skeptical prior) |
| γ (depth discount) | 0.8 | How much deeper knowledge contributes |
| k_live | 5 | Live attempts needed for confidence |
| k_srs | 4 | SRS streak needed for confidence boost |
| half_life | 60 days | Recency decay rate |
| unknown_budget | 0.15 | Reserved weight for unseen opponent replies |
| eval_delta threshold | 50 cp | Pass/fail cutoff (matches SRS threshold) |
| book_exit_extension | 4 user decisions | How far past book to score |

All should be exposed in a debug view before the score is user-facing.

---

## Addressing the Hard Problems

**"There is no cutoff for how deep an opening goes."**
The depth discount γ handles this. Each ply deeper contributes γ× less. Book-exit extension with steeper discount (γ²) lets deeper prep register without the tree growing forever. Weighted depth gives users a concrete "how deep" number.

**"The engine might not give you certain lines."**
Coverage tracks this explicitly. The unknown-reply budget at each opponent node means even perfect play on known lines cannot reach 100% score. The UI can show "you haven't faced the Alapin" as a Coverage gap, not a Mastery failure.

**"One lucky pass shouldn't mean mastery."**
The skeptical Bayesian prior (α=1, β=2) starts everyone at 0.33 and requires repeated evidence. Confidence tracks sample size separately. Together they prevent small-sample inflation.

---

## Open Questions

1. Should book-exit extension be 4 user decisions, or should it use a confidence-based stop rule (stop extending when c(n) drops below a threshold)?
2. Should manually captured blunders count as "prepared children" immediately, or only after one successful review?
3. Should the unknown budget scale with tree depth (larger near the root where surprise is costlier)?
4. What should the dashboard sort order be — weakest opening first (actionable) or strongest first (motivating)?
5. Should there be a minimum evidence threshold to show a score at all, or show everything with low confidence?
