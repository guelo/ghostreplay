# Opening Knowledge Score Specification

## Overview

The goal is to provide users with a quantitative measure of their opening knowledge, broken down by specific openings (e.g., "Sicilian Defense"). This score will help users track their progress, identify weak spots, and feel a sense of progression as they practice with the SRS system.

## Core Metrics

We will track three distinct metrics for each opening to address the "off-beat lines" and "completeness" problems:

1.  **Mastery (0-100):** How well do you play the lines you have actually encountered?
    *   *Interpretation:* High mastery means "I don't make mistakes in the variations I play."
2.  **Confidence (0-100):** How much evidence do we have for your mastery?
    *   *Interpretation:* High confidence means "I've proven this mastery over many repetitions/games," preventing a lucky 1-0 streak from looking like perfection.
3.  **Coverage (0-100):** What percentage of the "standard" theory have you explored?
    *   *Interpretation:* High coverage means "I have experience with most main lines and common sidelines."

## Data Model

The scoring system will build a **User Opening Tree** for each opening (rooted at the starting position of that opening).

### Nodes (Positions)
Each unique position (FEN) visited by the user in `session_moves` or `blunders` becomes a node.

### Edge Scoring (Moves)
For each move $m$ from position $P$ to child $C$:

1.  **Local Mastery ($mastery_m$):**
    *   **If Blunder Record Exists:** Derived from SRS pass streak.
        $$mastery_m = 1 - \frac{1}{1 + pass\_streak}$$
        *   Streak 0: 0.0
        *   Streak 1: 0.5
        *   Streak 5: ~0.83
    *   **If No Blunder Record (Good Move):** Derived from move classification in `session_moves`.
        $$mastery_m = \frac{\text{count(Best/Excellent)}}{\text{total\_visits}}$$
    *   **If No Data (Unseen/Unplayed):** 0.5 (Neutral assumption) or 0.0 (if strict).

2.  **Confidence ($confidence_m$):**
    *   Derived from total attempts/visits.
    *   $$confidence_m = 1 - e^{-\frac{visits}{k}}$$ (where $k$ is a scaling factor, e.g., 5).

3.  **Effective Edge Score ($score_m$):**
    *   $$score_m = mastery_m \times confidence_m$$

## Recursive Aggregation (The "Score")

We calculate the score for a position $P$ recursively. The score represents the **Expected Discounted Mastered Depth**.

$$Score(P) = \sum_{m \in Moves(P)} w_m \cdot score_m \cdot (1 + \gamma \cdot Score(C_m))$$

*   **$w_m$ (Weight):** The probability/importance of this move.
    *   *Primary Source:* User's own frequency (what they actually face).
    *   *Secondary Source:* Global popularity (from Master DB or Lichess DB) if user data is sparse.
*   **$\gamma$ (Discount Factor):** e.g., 0.8. Ensures deeper moves contribute less to the current node's score, preventing infinite sums and focusing on the immediate frontier.

### The Headline "Mastery"
The final Mastery score for an opening is the normalized $Score(Root)$.

## Coverage Calculation

$$Coverage = \frac{\text{Count(Visited Nodes in ECO)}}{\text{Count(Total Nodes in ECO for this Opening)}}$$

*   We filter "Total Nodes" to reasonable depth (e.g., depth 10-15) or popularity threshold to avoid penalizing for obscure infinite lines.
*   We can also visualize this as "Known" vs "Unknown" branches.

## Architecture & Implementation

### 1. Backend: `OpeningScoreCalculator` service
*   **Input:** `user_id`, `opening_root_fen`.
*   **Process:**
    1.  Fetch all `session_moves` and `blunders` for the user reachable from `opening_root_fen`.
    2.  Build the adjacency graph.
    3.  Compute leaf scores.
    4.  Propagate scores up to the root using the recursive formula.
    5.  Compute coverage by cross-referencing with `eco.json`.
*   **Output:** `OpeningStats` object (Mastery, Confidence, Coverage).

### 2. Database
*   No new tables strictly required for calculation (can be computed on-fly or cached).
*   For performance, we might add a `user_opening_stats` table to cache the results of the expensive tree walk.

### 3. Frontend: Dashboard
*   **List View:**
    *   "Sicilian Defense": Mastery 82% | Coverage 15% | Confidence 40%
*   **Detail View (Sunburst/Tree):**
    *   Visualizes the tree.
    *   Color = Mastery (Red=Bad, Green=Good).
    *   Opacity = Confidence (Faded=Lucky/New, Solid=Proven).
    *   Grayed out areas = Unexplored Coverage.

## Addressing User Concerns

*   **"No cutoff":** The discount factor $\gamma$ naturally handles this. Depth adds value, but diminishing returns apply.
*   **"Off-beat lines":** Separating **Coverage** solves this. If the engine never plays the Alapin Sicilian, your "Sicilian Mastery" can be 100% (you know what you play), but "Sicilian Coverage" will be low (you haven't seen everything).
