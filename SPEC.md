Here is the **SPEC.md** for your "Ghost Replay" Chess Application.

---

# SPEC.md - Ghost Replay Chess App

## 1. Product Description

**Ghost Replay** is a chess training web application designed to fix a player's leaks by forcing them to confront their past mistakes. Unlike standard analysis tools that passively show what went wrong, Ghost Replay uses an active "Ghost" opponent mechanism.

### The Core Loop

1. **Play:** The user plays a game against a bot.
2. **Analyze:** The client-side engine detects blunders in real-time.
3. **Store:** Blunders are saved to a personal "Chess Graph" database.
4. **Replay (The Ghost):** In future games, the bot prioritizes move sequences that steer the user back into positions where they previously blundered.
5. **Spaced Repetition:** If the user repeats the mistake, the game pauses for immediate correction. The interval for reviewing that specific blunder resets. If the user plays the correct move, the blunder is pushed further into the future (SRS).

---

## 2. User Stories & Features

### 2.1 Gameplay & Ghost Mode

* **Dynamic Opening:** As the user plays opening moves (e.g., `e4`), the system checks if this path leads to any "Due" blunders.
* **The Ghost Opponent:** If a path is found, the bot plays the exact moves required to reach the blunder position.
* **Seamless Deviation:** If the user plays a move that deviates from all known blunder paths, the "Ghost" deactivates, and a standard Stockfish engine takes over to finish the game.
* **Re-Hooking:** If a user deviates but later transposes back into a known position with a downstream blunder, the Ghost reactivates.

### 2.2 Analysis & Blunder Detection

* **Client-Side Analysis:** Blunders are detected in the browser using a secondary Web Worker to save server costs.
* **Blunder Definition:** A move is recorded as a blunder if the evaluation drops by a set threshold (e.g., > 200 centipawns) compared to the engine's best move.
* **First Mistake Only:** To prevent exponential data growth, only the *first* blunder of any single game session is recorded into the graph.

### 2.3 Spaced Repetition System (SRS)

* **Review Schedule:** Blunders are scheduled for review based on a modified SM-2 or Leitner system.
* **Instant Feedback:** When a user reaches a stored blunder position:
* **Failure:** If they repeat the mistake, the game pauses. "You made this mistake again." -> Interval resets to 0.
* **Success:** If they play the engine's top move (or a specific safe alternative), the system notifies "Blunder Fixed!" -> Interval expands.



---

## 3. High-Level Architecture (MVP)

The system uses a **Client-Coordinator-Memory** architecture. Heavy computation is offloaded to the client; the server acts as a lightweight state manager.

```mermaid
graph TD
    User[User Browser]
    
    subgraph "Frontend (React)"
        WorkerA[Stockfish A<br/>(The Opponent)]
        WorkerB[Stockfish B<br/>(The Analyst)]
        GameUI[Board UI]
    end

    subgraph "Backend (Python FastAPI)"
        API[API Coordinator]
    end

    subgraph "Database (PostgreSQL)"
        DB[(Chess Graph & SRS)]
    end

    User --> GameUI
    GameUI --> WorkerA
    GameUI --> WorkerB
    GameUI --> API
    API --> DB

```

### 3.1 Frontend (The Smart Client)

* **Responsibility:** UI, Move Validation, Engine Calculations.
* **Double Worker Pattern:**
* **Worker A (Opponent):** Plays the game. In "Ghost Mode," it blindly executes moves sent by the API. In "Engine Mode," it calculates its own moves (Elo 800-2500).
* **Worker B (Analyst):** Runs in the background at max strength (Skill 20). Analyzes every user move. If `(BestEval - UserEval) > Threshold`, it triggers a `POST /blunder`.



### 3.2 Backend (The Coordinator)

* **Responsibility:** Graph traversal and SRS updates.
* **Stateless:** The API does not hold game state. It receives a FEN (Board Position) and answers: *"What move should the Ghost play next?"*

### 3.3 Database (The Memory)

* **Responsibility:** Storing the graph of positions and moves.
* **Graph Structure:** Moves are not stored as linear games, but as a Directed Acyclic Graph (DAG) of unique FEN positions.

---

## 4. Tech Stack

| Component | Choice | Justification |
| --- | --- | --- |
| **Frontend** | React + Vite | Fast development, massive ecosystem for state management. |
| **Chess UI** | `react-chessboard` | Robust wrapper for chessboard.js. |
| **Chess Logic** | `chess.js` | Standard library for move generation/validation. |
| **Engine** | `stockfish.js` (WASM) | Runs full Stockfish 16 in the browser via Web Workers. |
| **Backend** | Python (FastAPI) | High performance, excellent libraries (`python-chess`). |
| **Database** | PostgreSQL | Required for Recursive CTEs (Graph traversal queries). |

---

## 5. Database Schema

The core innovation is storing chess history as a Graph.

### 5.1 `positions` (Nodes)

Represents a unique board state.

```sql
CREATE TABLE positions (
    id BIGSERIAL PRIMARY KEY,
    fen_hash VARCHAR(64) UNIQUE NOT NULL,  -- SHA256 of Normalized FEN
    fen_raw TEXT NOT NULL,
    is_blunder BOOLEAN DEFAULT FALSE,      -- Is this a tracked mistake?
    
    -- SRS Columns (Only used if is_blunder = TRUE)
    srs_due_date TIMESTAMP,                -- When to practice next
    srs_interval INTEGER DEFAULT 0,        -- Current interval in days
    srs_ease_factor FLOAT DEFAULT 2.5
);

```

### 5.2 `moves` (Edges)

Represents the transition between positions.

```sql
CREATE TABLE moves (
    from_position_id BIGINT REFERENCES positions(id),
    to_position_id BIGINT REFERENCES positions(id),
    move_san VARCHAR(10) NOT NULL,         -- e.g., "Nf3"
    
    PRIMARY KEY (from_position_id, move_san)
);

```

---

## 6. Data & Logic Flow

### 6.1 The "Scent" Logic (Next Move Selection)

When the user plays a move, the API must decide: *Continue Ghost path OR Switch to Engine?*

**Query Logic (Recursive CTE):**

1. **Input:** Current FEN Hash.
2. **Search:** Find all downstream nodes connected to this FEN.
3. **Filter:** `WHERE position.is_blunder = TRUE AND position.srs_due_date <= NOW()`.
4. **Scoring:**
* Sort paths by **Urgency** (Overdue items first).
* Sort by **Distance** (Closer blunders preferred).


5. **Output:** The immediate next move (SAN) that leads to the highest-scoring blunder.

### 6.2 The Blunder Capture Logic

1. User plays move .
2. **Worker B** (Frontend) calculates:
*  (Eval of engine's best move)
*  (Eval of user's move )


3. If  (approx 200cp):
* Frontend sends `POST /api/blunder` with `{ fen, user_move, best_move }`.
* Backend updates the graph:
* Inserts new Position Node (marked `is_blunder=TRUE`, `interval=0`).
* Inserts Move Edge connecting previous position to this one.





### 6.3 The SRS Update Logic

1. User arrives at a known Blunder Position.
2. **Scenario A (User Repeats Blunder):**
* Frontend detects the same bad move.
* Result: `Fail`.
* Backend Logic: `Next_Interval = 0` (Reset).


3. **Scenario B (User Plays Best Move):**
* Frontend detects the engine's suggested move.
* Result: `Pass`.
* Backend Logic: `Next_Interval = Current_Interval * 2.5`.



---

## 7. MVP Constraints & Scope

* **Single Variation per Game:** The system only records the *first* blunder of a session to keep the graph manageable initially.
* **No Redis:** All state checks go directly to PostgreSQL (acceptable performance for turn-based MVP).
* **Color:** MVP supports user playing as **White** only (simplifies graph directionality). *Future: Support Black.*
