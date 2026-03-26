✦ This is a fascinating idea, and it's definitely doable with the data already available in the system. Since we already have an ECO opening book and track SRS streaks for every blunder,
  we can synthesize a "Mastery" score that flows up the move tree.

  Here is a breakdown of how we might implement this "Opening Knowledge Tree" (OKT) scoring system.

  1. The "Mastery" Scoring Model

  We can define a score for every "node" (position) in the opening tree and then use a recursive formula to aggregate them.

   * Node Mastery ($m_n$):
       * If you have a Blunder record here: Mastery is tied to your SRS streak. A simple formula could be: $m_n = 1 - 0.8^{\text{pass\_streak}}$.
           * 0 streak: 0.2 score (You've seen it, but don't "know" it yet).
           * 5 streak: ~0.67 score.
           * 10 streak: ~0.89 score (Mastered).
       * If you've played this position and never blundered: $m_n = 1.0$.
       * If you've never reached this position: $m_n = 0.5$ (Neutral/Unknown).

   * Recursive Aggregation ($M_n$):
      To get the score for an opening (e.g., "Sicilian Defense"), we look at all its child moves:
      $$M_{\text{parent}} = \alpha \cdot m_{\text{parent}} + (1 - \alpha) \cdot \frac{\sum (w_i \cdot M_{\text{child}_i})}{\sum w_i}$$
       * $\alpha$ (Decay Factor): e.g., 0.2. This means "knowing the current move" is worth 20%, and "knowing all the variations that follow" is worth 80%.
       * $w_i$ (Importance Weight): We can weight moves by their popularity (e.g., the "Main Line" has a higher weight than an "Off-beat Line").

  2. Solving the "Difficulties"

   * The "No Cutoff" Problem:
      We can use the ECO Book (eco.json) as our standard "map." Every node in the ECO book is a potential score point. If a user has blunders that go deeper than the book, we dynamically
  extend their personal tree for that line. This allows the system to reward players who study "theoretical depth" beyond the basics.

   * The "Off-beat Lines" Problem:
      We should distinguish between Mastery (how well you play what you see) and Coverage (how much of the tree you've explored).
       * Mastery Score: 90% (You are great at the lines the engine plays).
       * Coverage Score: 20% (The engine only plays the Main Line; you haven't seen 80% of the possible responses).
      This tells the user: "You know the Najdorf perfectly, but you're unprepared for the Smith-Morra Gambit."

  3. How the Dashboard Would Look

  Imagine a "Sunburst" chart or a Tree Map where:
   * Segment Size: Represents how popular/important the line is.
   * Segment Color (Mastery): Red (Frequent blunders) $\rightarrow$ Yellow (In SRS) $\rightarrow$ Green (Mastered).
   * Segment Opacity (Coverage): High opacity for lines you've played often, low opacity for "theoretical" lines you haven't encountered yet.

  4. Technical Feasibility
   * Backend: We can create an /api/stats/openings endpoint that joins Blunder records with the AnalysisCache and the ECO book tree.
   * Frontend: We can use the existing lookupOpeningByFen utility to map game history to the mastery tree.

  Next Steps:
  If you'd like to see this in action, we could start by building a small prototype that calculates a "Mastery Score" for a single opening (like the "Ruy Lopez") based on your current
  database of blunders and game history.

  What do you think of separating Mastery from Coverage? Does that help solve the "off-beat lines" concern?
