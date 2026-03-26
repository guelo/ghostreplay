⏺ Good news: your existing data model is well-suited for this. You already have a position graph (positions + moves tables)
  that forms a per-user DAG rooted at the starting position, plus per-position blunder/SRS data and per-visit move
  classifications. Here's how I'd think about the scoring:

  ---
  Recursive Opening Mastery Score

  Layer 1: Local mastery at each position node

  Every position the user has visited gets a local mastery score (0–1):

  - Blunder position (has a blunders row): derive from pass_streak
    - mastery = 1 - 1/(1 + pass_streak) — so streak 0→0, streak 1→0.5, streak 3→0.75, streak 5→0.83
    - This naturally encodes SRS confidence: more consecutive passes = more mastery
  - Non-blunder position: derive from move quality across all visits
    - Look at session_moves.classification for every time the user was at this FEN
    - mastery = (count of best/excellent) / total_visits
    - A position visited 20 times with 18 good moves = 0.9

  Layer 2: Confidence damping

  Raw mastery gets damped by how much data you have:

  confidence = 1 - 1/(1 + visit_count)
  effective_mastery = mastery * confidence

  A position visited once with a "best" move gets 1.0 * 0.5 = 0.5, not 1.0. You need reps to prove you know it.

  You could also add recency decay — e.g., multiply by exp(-days_since_last_visit / half_life) — so a line you haven't
  practiced in 6 months fades.

  Layer 3: Subtree aggregation (the recursive part)

  A node's tree score combines its own mastery with its children:

  tree_score(P) = effective_mastery(P) * (α + (1-α) * weighted_avg(tree_score(child)))

  Where:
  - α ≈ 0.3–0.5 controls how much your own performance matters vs. depth
  - Children are weighted by visit frequency — if the opponent plays e5 80% of the time and d5 20%, weight accordingly
  - Leaf nodes (no children = you've never gone deeper): tree_score = effective_mastery — these naturally score lower
  because there's no depth proven

  This solves the "no cutoff" problem elegantly: you don't need to define where an opening ends. The tree just goes as deep
  as the user's data goes. Rarely-visited deep positions contribute very little due to low visit-frequency weights and low
  confidence.

  Layer 4: Opening-level scores

  Group the tree by ECO codes using your existing openingBook.ts data:

  1. Walk the position graph from the root
  2. At each position, look up the ECO code via FEN
  3. The opening score = tree_score at the deepest position still tagged with that ECO code (i.e., the last book position
  before you're "out of book")
  4. Sub-variations (e.g., "Italian Game: Giuoco Piano" vs "Italian Game: Evans Gambit") get their own scores since they
  diverge at a specific position node

  Addressing your two concerns

  "No cutoff for opening depth" — The score doesn't need one. It's a measure of how deep you've proven mastery, not how much
   of a fixed syllabus you've covered. A deeper tree with good scores = higher opening score, naturally.

  "Engine might not give you off-beat lines" — You could add a coverage metric alongside the mastery score. Using the
  position graph: at each node, count practiced_children / known_children. "Known children" could come from:
  - The ECO book (what continuations exist in theory)
  - Or more practically: moves from master game databases
  - Or simplest: moves that appear in any user's games at that position (if you go multi-user later)

  This gives you two dashboard numbers per opening: mastery (how well you play the lines you've seen) and coverage (how many
   lines you've been tested on). Low coverage + high mastery = "you know it well but haven't been tested widely."

  ---
  Implementation feasibility

  Everything can be computed from existing tables:
  - positions + moves → the tree structure
  - session_moves → visit counts, move quality per position
  - blunders → SRS pass_streak per position
  - openingBook → ECO labeling

  The computation is a single recursive tree walk (or bottom-up aggregation with memoization). Could be a periodic batch job
   that writes to an opening_scores table, or computed on-demand since opening trees aren't that large (typically <500 nodes
   per ECO code per user).
