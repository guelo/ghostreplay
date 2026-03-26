  Opening Mastery Score — Unified Plan

  What each plan gets right

  Plan 1 has the cleanest layered structure (local mastery → confidence damping → subtree aggregation → opening grouping)
  and introduces recency decay — a line you haven't practiced in months should fade.

  Plan 2 contributes the best UX ideas: using the ECO book as the standard "map" for coverage, and a sunburst visualization
  where segment size = line importance, color = mastery, opacity = coverage.

  Plan 3 has the most rigorous scoring model and the most critical insight: you must score from all session_moves, not just
  blunders, because the first-blunder-only rule means many opening mistakes aren't in the blunders table at all. It also
  correctly distinguishes user-turn edges (where mastery is a gate) from opponent-turn edges (where you just weight by
  frequency), uses Bayesian smoothing to prevent "one lucky pass = mastered", and reserves weight for unseen branches to
  prevent fake 100% scores.

  Where they fall short

  - Plan 1 gives mastery=0 for a blunder with pass_streak=0, which is wrong — you've at least identified it.
  - Plan 2's "never blundered = 1.0" is too generous — low sample size should not mean perfection.
  - Plan 3 has no recency decay and no visualization ideas.
  - None of them address how to actually build the opening tree from existing data in concrete SQL/code terms.

  ---
  The Unified Model

  Three headline metrics per opening

  ┌────────────┬───────────────────────────────────────────────────────────┬───────┐
  │   Metric   │                          Meaning                          │ Range │
  ├────────────┼───────────────────────────────────────────────────────────┼───────┤
  │ Mastery    │ How well you play the lines you've encountered            │ 0–100 │
  ├────────────┼───────────────────────────────────────────────────────────┼───────┤
  │ Confidence │ How much evidence supports that mastery score             │ 0–100 │
  ├────────────┼───────────────────────────────────────────────────────────┼───────┤
  │ Coverage   │ How much of the known opening tree you've been exposed to │ 0–100 │
  └────────────┴───────────────────────────────────────────────────────────┴───────┘

  Step 1: Build the opening tree

  The user's position graph (positions + moves tables) already forms a per-user DAG rooted at the starting position. To get
  the opening subtree:

  1. Walk the position graph from root.
  2. At each position, look up the ECO code via eco.byPosition.json (7,484 indexed FENs, fits in memory).
  3. Group nodes into subtrees by ECO code. A position belongs to the deepest ECO code it matches (e.g., "B42 Sicilian, Kan"
   not just "B40 Sicilian Defense").
  4. The tree extends beyond the book — if the user has positions deeper than the last ECO-tagged node, include them as part
   of that opening's tree.

  Step 2: Score each edge (from Plan 3, refined)

  Every edge (move) in the tree gets a mastery gate g_e:

  - Opponent-turn edges (you don't control what they play): g_e = 1.0 always. These are weighted by observed frequency
  instead.
  - User-turn edges (your move): Use Bayesian smoothing from session_moves:

  g_e = (correct_plays + α) / (correct_plays + incorrect_plays + α + β)

  Where correct_plays = times user played this exact move when facing this position, incorrect_plays = times they played
  something else or worse. Use α=1, β=1 (uniform prior) so:
  - 0 attempts → g = 0.5 (uncertain, not zero)
  - 1/1 correct → g = 0.67 (not 1.0 — need more reps)
  - 5/5 correct → g = 0.86
  - 10/10 correct → g = 0.92

  Blunder SRS boost: If this edge has a blunder record, blend the pass_streak signal:

  g_blunder = (pass_streak + α) / (pass_streak + fails + α + β)

  Take g_e = max(g_session_moves, g_blunder) — the SRS signal is the strongest evidence of mastery since it's
  spaced-repetition-tested.

  Step 3: Recursive aggregation (from Plan 3's formula, with Plan 1's weighting)

  Score(n) = Σ over child edges e of:  w_e × g_e × (1 + γ × Score(child_e))

  - w_e = branch importance weight:
    - For opponent-turn moves: observed frequency (how often they play this)
    - For user-turn moves: 1.0 for the "correct" move, 0 for alternatives (you want to play the best move)
    - Reserve weight for unseen branches (Plan 3's key idea): if the ECO book shows 3 responses at this node but the user
  has only faced 2, allocate weight to the missing one with Score=0. This prevents inflated scores.
  - γ ≈ 0.8 — discount factor so deeper knowledge matters but not infinitely
  - Weights at each node are normalized to sum to 1.0

  Opening Mastery = 100 × Score(root) / PerfectScore(root)

  where PerfectScore assumes all gates = 1.0 (every move known perfectly).

  Step 4: Confidence (from Plans 1+3, combined)

  Per-edge confidence from sample size:

  confidence_e = 1 - exp(-attempts_e / k)    (k ≈ 5)

  So 1 attempt → 18% confidence, 5 attempts → 63%, 10 attempts → 87%.

  Recency decay (from Plan 1): multiply by exp(-days_since_last_practice / half_life) where half_life ≈ 60 days. A line you
  haven't touched in 4 months fades to ~25% confidence.

  Aggregate confidence through the same tree weights as mastery. The headline Confidence number tells the user "how much
  should you trust the Mastery score."

  Step 5: Coverage (from Plan 2, grounded in ECO book)

  At each node in the ECO book tree:

  coverage_node = practiced_children / known_children

  Where known_children = moves that exist in the ECO book at this position. Aggregate up the tree weighted by branch
  importance.

  Coverage answers: "of the theoretically known lines, how many have you actually been tested on?"

  ---
  Data sources (mapped to actual schema)

  ┌───────────────────┬──────────────────────────────────────────────────────┬──────────────────────────────────────────┐
  │       What        │                        Source                        │               Key columns                │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ Position graph    │ positions + moves                                    │ fen_hash, from_position_id,              │
  │                   │                                                      │ to_position_id                           │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ Move quality per  │ session_moves                                        │ classification, fen_before, move_san,    │
  │ visit             │                                                      │ best_move_san                            │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ SRS mastery       │ blunders                                             │ position_id, pass_streak, bad_move_san,  │
  │                   │                                                      │ best_move_san                            │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ SRS history       │ blunder_reviews                                      │ passed, move_played_san                  │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ Opening lookup    │ eco.byPosition.json                                  │ normalized FEN → {eco, name}             │
  ├───────────────────┼──────────────────────────────────────────────────────┼──────────────────────────────────────────┤
  │ Recency           │ blunders.last_reviewed_at, session_moves via         │ timestamps                               │
  │                   │ game_sessions.ended_at                               │                                          │
  └───────────────────┴──────────────────────────────────────────────────────┴──────────────────────────────────────────┘

  The critical data gap

  Currently, session_moves records every move classified but doesn't link back to the position graph's position_id. To
  efficiently query "how many times did the user play move X at position Y across all games," you'd either:

  1. Join via FEN: session_moves.fen_before → normalize → match positions.fen_hash. Works but requires FEN normalization at
  query time.
  2. Add a column: session_moves.position_id FK — cleaner, requires a migration + backfill.

  Option 1 is fine for a first pass. The opening tree is small (~100-500 nodes per ECO code), so the query cost is
  manageable.

  Computation strategy

  - Batch job, not on-demand. Run after each game session ends (or nightly).
  - Write results to an opening_scores table: (user_id, eco_code, opening_name, mastery, confidence, coverage,
  weighted_depth, strongest_branch, weakest_branch, computed_at).
  - The tree walk is O(nodes) per opening — typically <500 nodes, so it's fast.
  - Load the ECO position index into memory once (it's ~1MB).

  Dashboard UX

  Card per opening (from Plan 3's dashboard, styled per Plan 2):

  Sicilian Defense: Najdorf
  Mastery: 74    Confidence: 61    Coverage: 48
  Depth: 5.8 plies
  Strongest: Main Line (6. Bg5)  ■■■■■■■■░░ 82
  Weakest:   English Attack       ■■■░░░░░░░ 31

  Sunburst visualization (from Plan 2): rings = ply depth, segments = branches, color = mastery (red→yellow→green),
  opacity/saturation = coverage. Clicking a segment shows that subtree's details.

  What this plan deliberately avoids

  - No "you finished the opening" claim — there's no meaningful cutoff. Instead, weighted depth tells the user how deep
  their knowledge goes.
  - No fake 100% scores — the unseen-branch budget and Bayesian smoothing prevent this.
  - No over-reliance on blunders — session_moves are the primary signal; blunder pass_streak is a boost, not the only input.
