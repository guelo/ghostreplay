• Yes, this is doable, but the score should be derived from full opening-phase decisions, not just blunders. In this
  codebase, that’s possible because backend/app/models.py already has full session_moves and blunder_reviews, and src/
  openings/openingBook.ts can map a FEN to an opening label in a transposition-aware way. If you used only blunders, the
  score would be badly biased because GhostReplay only auto-records the first opening mistake per game in SPEC.md.

  I’d recommend a 3-part model:

  1. Mastery 0-100
  2. Confidence 0-100
  3. Coverage 0-100

  Mastery is the headline number. Confidence tells you how much evidence supports it. Coverage tells you how much of the
  opening tree you’ve actually been exposed to.

  A good recursive score is “expected discounted mastered depth” over the opening tree:

  For each edge e from node n:
  - w_e = branch importance weight
  - g_e = mastery gate for that edge
    - g_e = 1 for opponent-forced edges
    - g_e = estimated probability you play the right move on your turn

  Score(n) = sum over children e of:
    w_e * g_e * (1 + gamma * Score(child_e))

  OpeningMastery = 100 * Score(root) / PerfectScore(root)

  Use gamma around 0.75 to 0.85. That makes deeper knowledge matter, but not infinitely. The recursion naturally rewards
  both:

  - depth, because deeper subtrees only count if earlier moves are known
  - breadth, because sibling branches all contribute through the weighted sum

  For the user-turn mastery gate g_e, use Bayesian smoothing so “one lucky pass” does not look like mastery:

  g_e = (passes_e + alpha) / (passes_e + fails_e + alpha + beta)

  Then show Confidence separately from sample size, for example:

  confidence_e = 1 - exp(-attempts_e / k)

  Aggregate confidence through the same tree weights. This gives the “I’ve passed this line 8 times” effect you want.

  How this handles the hard parts:

  - No hard opening cutoff:
    Don’t claim “you finished the opening.” Instead say “you know this opening to weighted depth 6.4 with 72% coverage.”
    Stop recursion when the FEN is no longer recognized as that opening, or after a soft ply cap.
  - Offbeat lines not offered by engine:
    Keep an unknown/unseen branch bucket with some reserved weight. That prevents the score from hitting fake 100s just
    because the engine never challenged weird sidelines.
  - Transpositions:
    Group by normalized FEN, not move order. Your opening lookup already supports that.

  For a dashboard card, I’d show something like:

  - Sicilian Defense
  - Mastery: 74
  - Confidence: 61
  - Coverage: 48
  - Weighted depth: 5.8 plies
  - Strongest branch: Open Sicilian
  - Weakest branch: Alapin

  The main product decision is this: the headline score should mean “how well you know the opening frontier you’ve
  encountered,” not “how completely you know all possible lines.” That avoids the impossible completeness problem and
  matches how GhostReplay actually trains.
