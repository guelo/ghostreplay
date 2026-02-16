Here is the **SPEC.md** for your "Ghost Replay" Chess Application.

---

# SPEC.md - Ghost Replay Chess App

## Table of Contents

1. Product Description
   - The Core Loop
2. User Stories & Features
   - 2.1 Gameplay & Ghost Mode
   - 2.2 Analysis & Blunder Detection
   - 2.3 Spaced Repetition System (SRS)
3. High-Level Architecture (MVP)
   - 3.1 Frontend (The Smart Client)
   - 3.2 Backend (The Coordinator)
   - 3.3 Database (The Memory)
4. Tech Stack
5. Database Schema
   - 5.1 `positions` (Nodes)
     - 5.1.1 FEN Normalization
   - 5.2 `blunders` (Ghost Move Library Targets)
     - 5.2.1 `blunder_reviews` (Review Events)
   - 5.3 `moves` (Edges)
   - 5.4 `users` (Identity)
   - 5.5 Authentication
6. Data & Logic Flow
   - 6.1 The "Scent" Logic (Next Move Selection)
     - 6.1.1 Re-Hooking Logic (Transposition Detection)
   - 6.2 Ghost Move Library Capture Logic
   - 6.3 The SRS Update Logic
     - 6.3.1 Replay Priority Score
     - 6.3.2 Update Rules
     - 6.3.3 Evaluation Thresholds
   - 6.4 Engine Evaluation Protocol
     - 6.4.1 Search Parameters
     - 6.4.2 Evaluation Perspective (Sign Convention)
     - 6.4.3 Mate Score Conversion
     - 6.4.4 Evaluation Stability
     - 6.4.5 Edge Cases
     - 6.4.6 Frontend Implementation Notes
7. Game Sessions & Lifecycle
   - 7.1 Session Definition
   - 7.2 Game States
   - 7.3 Session Schema
   - 7.4 Move Analysis Storage
   - 7.5 First-Blunder Rule Enforcement
   - 7.6 Game Termination
   - 7.7 Session Persistence
8. MVP Constraints & Scope
9. API Specification
   - 9.1 Base URL
   - 9.2 Authentication
   - 9.3 Game Flow
   - 9.4 Blunders / Ghost Move Library Targets
   - 9.5 SRS (Spaced Repetition)
   - 9.6 Error Responses
   - 9.7 Design Decisions
10. After-Game Analysis Display
    - 10.1 Screen Layout
    - 10.2 Components
      - 10.2.1 Chessboard
      - 10.2.2 Evaluation Graph
      - 10.2.3 Evaluation Bar
      - 10.2.4 Navigation Controls
      - 10.2.5 Move List
      - 10.2.6 Position Analysis Panel
    - 10.3 Data Source
    - 10.4 API Endpoint
    - 10.5 Entry Points
    - 10.6 MVP Constraints
11. Game History View
    - 11.1 Entry Points
    - 11.2 Screen Layout
    - 11.3 Game Card Data
    - 11.4 Interaction Flow
    - 11.5 API Endpoint
    - 11.6 Empty State
    - 11.7 MVP Constraints
12. Testing Strategy
    - 12.1 Tooling
    - 12.2 Coverage Priorities (MVP)
    - 12.3 Key Test Cases
    - 12.4 Test Data & Determinism

---

## 1. Product Description

**Ghost Replay** is a chess training web application designed to fix a player's leaks by forcing them to confront their past mistakes. Unlike standard analysis tools that passively show what went wrong, Ghost Replay uses an active "Ghost" opponent mechanism.

### The Core Loop

1. **Play:** The user plays a game against a bot.
2. **Analyze:** The client-side engine detects blunders in real-time.
3. **Store:** Blunders are saved to a personal Ghost Move Library database.
4. **Replay (The Ghost):** In future games, the bot prioritizes move sequences that steer the user back into positions where they previously blundered.
5. **Spaced Repetition:** If the user repeats the mistake, the game pauses for immediate correction. The interval for reviewing that specific blunder resets. If the user plays the correct move, the blunder is pushed further into the future (SRS).

---

## 2. User Stories & Features

### 2.1 Gameplay & Ghost Mode

* **Dynamic Opening:** As the user plays opening moves (e.g., `e4`), the system checks if this path leads to any "Due" blunders.
* **The Ghost Opponent:** If a path is found, the bot plays the exact moves required to reach the blunder position.
* **Seamless Deviation:** If the user plays a move that deviates from all known blunder paths, the backend automatically switches to engine-generated opponent moves for continuity.
* **Re-Hooking:** If a user deviates but later transposes back into a known position with a downstream blunder, the Ghost reactivates.
* **Player Side:** The user can play as **White or Black** per session; Ghost targeting only considers blunders made as that side.

### 2.2 Analysis & Blunder Detection

* **Client-Side Analysis:** Blunders are detected in the browser using a secondary Web Worker to save server costs.
* **Recording Threshold:** A move is recorded as a Ghost Move Library target if the evaluation drops by ≥50 centipawns compared to the engine's best move (inaccuracy level and above).
* **Opening Moves Only:** Only mistakes in the first 10 moves of the game are eligible for automatic recording. Opening positions have low branching factor and are the most likely to recur in future games, making them viable Ghost steering targets.
* **First Mistake Only:** To prevent exponential data growth, only the *first* recorded mistake of any single game session is saved into the Ghost Move Library.

### 2.3 Spaced Repetition System (SRS)

* **Probability-Based Scheduling:** Instead of strict "due dates," each blunder has a **replay priority score** that determines how likely it is to appear. This allows natural spacing without arbitrary caps.
* **Priority Factors:**
  * `pass_streak` — Consecutive correct responses (higher = lower priority)
  * `time_since_last_review` — Time elapsed since last encounter (longer = higher priority)
  * `eval_loss_cp` — Severity of the original mistake (larger = higher priority)
  * `distance` — Moves to reach the blunder from the current position (closer = higher priority)
* **Steering Radius:** The Ghost only targets blunders reachable within 5 moves of the current position. Anything beyond 5 moves is ignored — the branching factor makes deeper steering unreliable.
* **Binary Grading:** Pass or fail only. No easy/good/hard ratings — chess moves are unambiguous.
* **Instant Feedback:** When a user reaches a stored blunder position:
  * **Failure:** If they play a move ≥50cp worse than the best move, the game pauses. "You made this mistake again." → `pass_streak` resets to 0.
  * **Success:** If they play any move within 50cp of the engine's best, the system notifies "Correct!" → `pass_streak` increments.



---

## 3. High-Level Architecture (MVP)

The system uses a **Client-Coordinator-Memory** architecture. Opponent move selection is centralized in the backend, while tactical blunder analysis remains client-side.

```mermaid
graph TD
    User[User Browser]

    subgraph "Frontend (React)"
        WorkerB[Stockfish B<br/>(The Analyst)]
        GameUI[Board UI]
    end

    subgraph "Backend (Python FastAPI)"
        API[API Coordinator]
    end

    subgraph "Database (PostgreSQL)"
        DB[(Ghost Move Library & SRS)]
    end

    Maia3[Maia3 API<br/>maiachess.com]

    User --> GameUI
    GameUI --> WorkerB
    GameUI --> API
    API --> DB
    API --> Maia3

```

### 3.1 Frontend (The Smart Client)

* **Responsibility:** UI, move validation, and analysis orchestration.
* **Single Worker Pattern:**
* **Worker B (Analyst):** Runs in the background at max strength (Skill 20). Analyzes every user move. If `(BestEval - UserEval) > Threshold`, it triggers a `POST /blunder`.



### 3.2 Backend (The Coordinator)

* **Responsibility:** Ghost-path traversal, opponent move selection (via remote Maia3 API), and SRS updates.
* **Stateless:** The API does not hold game state. It receives the current FEN and move history, then answers: *"What is the next opponent move (ghost or engine)?"*

### 3.3 Database (The Memory)

* **Responsibility:** Storing the Ghost Move Library graph (`positions` + `moves`), plus the user decision targets that are practiced later.
* **Graph Structure:** Moves are not stored as linear games, but as a **directed graph** of unique FEN positions. Note: While games progress forward in time, the Ghost Move Library can contain cycles (e.g., threefold repetition, perpetual checks, transpositions that revisit the same FEN). Recursive queries must include cycle detection and depth bounds.
* **Ghost Move Library Semantics:** The Ghost Move Library is the move graph itself (`positions` + `moves`); auto-identified blunders and manually selected MoveList decisions are stored as target rows in `blunders`.

---

## 4. Tech Stack

| Component | Choice | Justification |
| --- | --- | --- |
| **Frontend** | React + Vite | Fast development, massive ecosystem for state management. |
| **Chess UI** | `react-chessboard` | Robust wrapper for chessboard.js. |
| **Chess Logic** | `chess.js` | Standard library for move generation/validation. |
| **Opponent Engine** | Maia3 (remote API via maiachess.com) | Backend proxies move requests to the Maia3 API, selecting the appropriate ELO model (600–2600). No local model files or GPU required. |
| **Analysis Engine** | `stockfish.js` (WASM) | Browser-side analyst worker for blunder detection/SRS grading. |
| **Backend** | Python (FastAPI) | High performance, excellent libraries (`python-chess`). |
| **Database** | PostgreSQL | Required for Recursive CTEs (Graph traversal queries). |

---

## 5. Database Schema

The core innovation is storing chess history as the Ghost Move Library. The Ghost Move Library is composed of positions as nodes and moves as edges, with user decision targets stored in `blunders`.
The complication is that the user moves only on every other edge, so capture logic must validate side-to-move ownership.

**User Scoping:** All data is scoped per-user. Each user has their own Ghost Move Library graph (`positions` + `moves`) and target rows in `blunders`. There is no sharing of data between users (MVP).

### 5.1 `positions` (Nodes)

Represents a unique board state. Positions are pure Ghost Move Library nodes—they contain no blunder or SRS data.

```sql
CREATE TABLE positions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    fen_hash VARCHAR(64) NOT NULL,         -- SHA256 of Normalized FEN
    fen_raw TEXT NOT NULL,
    active_color VARCHAR(5) NOT NULL,      -- 'white' or 'black' (side to move)
    created_at TIMESTAMP DEFAULT NOW(),

    UNIQUE (user_id, fen_hash)
);

CREATE INDEX idx_positions_user ON positions(user_id);
CREATE INDEX idx_positions_fen_hash ON positions(user_id, fen_hash);
CREATE INDEX idx_positions_user_color ON positions(user_id, active_color);
```

#### 5.1.1 FEN Normalization

The `fen_hash` is computed from a **normalized FEN**, not the raw FEN string. This ensures positions reached via different move orders are recognized as identical.

**Standard FEN Fields:**
```
rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1
│                                             │ │    │  │ └─ Fullmove number
│                                             │ │    │  └─── Halfmove clock
│                                             │ │    └────── En passant square
│                                             │ └─────────── Castling rights
│                                             └───────────── Active color
└─────────────────────────────────────────────────────────── Piece placement
```

**Normalization Rule:** Keep fields 1-4, strip fields 5-6.

| Field | Kept | Reason |
|-------|------|--------|
| Piece placement | ✓ | Defines the position |
| Active color | ✓ | Whose turn matters for blunders |
| Castling rights | ✓ | Affects legal moves and evaluation |
| En passant square | ✓ | Affects legal moves (canonicalize: only keep when capture is legal) |
| Halfmove clock | ✗ | Same position via different path should match |
| Fullmove number | ✗ | Irrelevant for position identity |

**Canonical EP Rule:** Some PGN/FEN sources populate the en passant square even when no capture is legal. Before hashing, recompute the EP flag from the board state: if no legal en passant capture exists, force the value to `-`. This keeps transpositions equivalent and makes Ghost re-hooking reliable.

**Implementation:**
```python
def normalize_fen(fen: str) -> str:
    """Strip move clocks from FEN for position hashing."""
    parts = fen.split(' ')
    board = chess.Board(fen)
    ep = board.ep_square  # None if capture not legal
    parts[3] = board.square_name(ep) if ep is not None else '-'
    return ' '.join(parts[:4])

def fen_hash(fen: str) -> str:
    """Generate SHA256 hash of normalized FEN."""
    normalized = normalize_fen(fen)
    return hashlib.sha256(normalized.encode()).hexdigest()

def active_color(fen: str) -> str:
    """Return 'white' or 'black' from the FEN active color field."""
    parts = fen.split(' ')
    return 'white' if parts[1] == 'w' else 'black'
```

**Example:**
```
Raw:        rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1
Normalized: rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3
Hash:       a1b2c3d4... (SHA256)
```

### 5.2 `blunders` (Ghost Move Library Targets)

Represents a decision point the user will practice from a specific position. This is the core SRS entity linked to the Ghost Move Library. Entries come from both:
- auto-detected blunders (`POST /api/blunder`)
- manually selected moves from MoveList (`POST /api/blunder/manual`)

```sql
CREATE TABLE blunders (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    position_id BIGINT NOT NULL REFERENCES positions(id),  -- Pre-move position (decision point)
    bad_move_san VARCHAR(10) NOT NULL,     -- Selected move captured at this decision point
    best_move_san VARCHAR(10) NOT NULL,    -- Engine recommended move at capture time
    eval_loss_cp INTEGER NOT NULL,         -- Centipawn delta at capture time (0 allowed for manual captures)

    -- SRS Fields
    pass_streak INTEGER NOT NULL DEFAULT 0,
    last_reviewed_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (user_id, position_id)          -- One ghost-library target per position per user
);

CREATE INDEX idx_blunders_user ON blunders(user_id);
CREATE INDEX idx_blunders_position ON blunders(position_id);
CREATE INDEX idx_blunders_due ON blunders(user_id, pass_streak, last_reviewed_at);
```

**Key semantics:**
- `position_id` references the **pre-move** position—where the user faced the decision
- `bad_move_san` is the move captured when the target was added (for auto blunders this is the mistake; for manual captures it may be a good move)
- SRS pass/fail is determined by **real-time engine evaluation**, not by checking against `bad_move_san`
- Any move within 50cp of the engine's best passes; any move ≥50cp worse fails
- The unique constraint means duplicate adds at the same position return the existing target (`is_new=false`, shown in UI as "already in library")
- Targets are only recorded when it is **the user's turn to move**; Ghost selection filters by the session's `player_color` so users can play either side without cross-contamination

#### 5.2.1 `blunder_reviews` (Review Events)

Each spaced-repetition encounter with a blunder must be persisted so the API can return a `review_history` timeline. This table stores those immutable review events.

```sql
CREATE TABLE blunder_reviews (
    id BIGSERIAL PRIMARY KEY,
    blunder_id BIGINT NOT NULL REFERENCES blunders(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES game_sessions(id), -- The game context for the review
    reviewed_at TIMESTAMP NOT NULL DEFAULT NOW(),
    passed BOOLEAN NOT NULL,
    move_played_san VARCHAR(10) NOT NULL,
    eval_delta_cp INTEGER NOT NULL                      -- Positive means worse than best
);

CREATE INDEX idx_blunder_reviews_blunder ON blunder_reviews(blunder_id, reviewed_at DESC);
```

**Usage notes:**
- Rows are append-only to preserve the user's study history
- `reviewed_at` doubles as the timestamp returned in `review_history`
- The API response nests `{ reviewed_at, passed, move_played }` derived from this table (with `move_played` mapped from `move_played_san`)

### 5.3 `moves` (Edges)

Represents the transition between positions.

```sql
CREATE TABLE moves (
    from_position_id BIGINT REFERENCES positions(id),
    to_position_id BIGINT REFERENCES positions(id),
    move_san VARCHAR(10) NOT NULL,         -- e.g., "Nf3"

    PRIMARY KEY (from_position_id, move_san)
);
```

### 5.4 `users` (Identity)

Represents both anonymous and claimed user accounts.

```sql
CREATE TABLE users (
    id BIGSERIAL PRIMARY KEY,
    username VARCHAR(32) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,   -- bcrypt hash
    is_anonymous BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

**Constraints:**
- `username`: 3-32 characters, alphanumeric + underscores only
- Anonymous users start with auto-generated usernames (e.g., `ghost_a3b5c7d9`)
- `is_anonymous`: TRUE for auto-created accounts, FALSE after claiming

### 5.5 Authentication

**Method:** Anonymous-first with stateless JWT tokens

**Token Structure:**
```json
{
  "sub": "<user_id>",
  "username": "<username>",
  "is_anonymous": "<boolean>",
  "exp": "<expiry_timestamp>"
}
```

**Anonymous-First Flow:**

1. **First Visit (Automatic):**
   - Frontend generates random username (e.g., `ghost_a3b5c7d9`) and password
   - Stores credentials in localStorage
   - Calls `POST /api/auth/register` with auto-generated credentials
   - Receives JWT token, stores in localStorage
   - User can immediately start playing without manual registration

2. **Subsequent Visits:**
   - Frontend checks localStorage for credentials
   - Auto-login with stored username/password via `POST /api/auth/login`
   - User experience is seamless - no login prompt

3. **Account Claiming (Optional):**
   - User can upgrade anonymous account to permanent account
   - Calls `POST /api/auth/claim` with new username/password
   - Updates `is_anonymous` flag to FALSE
   - Preserves all user data and progress

4. **Cross-Device Access (Optional):**
   - Users who claimed their account can log in from new devices
   - Traditional login flow via `POST /api/auth/login`
   - Anonymous users are device-specific (localStorage-bound)

**Token Lifetime:** 7 days (MVP). Refresh tokens deferred to post-MVP.

**Design Rationale:**
- Removes friction for new users - no sign-up barrier
- Users build progress before deciding to commit to an account
- Anonymous users can experiment risk-free
- Claimed accounts enable cross-device access and data security

---

## 6. Data & Logic Flow

### 6.1 The "Scent" Logic (Next Move Selection)

When the user plays a move, the API must decide: *Continue Ghost path OR Switch to Engine?*

**Query Logic (Recursive CTE with Safeguards):**

The Ghost Move Library can contain cycles (threefold repetition, transpositions). Recursive queries **must** include:
- **Depth bounds:** Hard cap at 5 moves. Beyond 5 moves the branching factor makes steering unreliable, so deeper blunders are not considered.
- **Cycle detection:** Track visited positions to prevent infinite loops

1. **Input:** Current FEN Hash + `session_id` (to scope to `player_color`).
2. **Search:** Find all downstream positions connected to this FEN (up to 5 moves, avoiding cycles).
3. **Filter:** Join with `blunders` table to find positions where user has a recorded target.
4. **Scoring:** For each reachable target, calculate:
   ```
   expected_interval = BASE_INTERVAL * (BACKOFF_FACTOR ^ pass_streak)
   hours_since_review = (NOW - last_reviewed_at) in hours
   srs_priority = hours_since_review / expected_interval

   -- Weight by severity (bigger mistakes surface first)
   -- Adjust for distance (closer blunders slightly preferred)
   score = srs_priority * (eval_loss_cp / 50) / (1 + 0.1 * distance)
   ```
5. **Selection:** Pick the path leading to the highest `score` blunder.
6. **Output:** The immediate next move (SAN) on that path.

**Color Scope Rule:** Only consider blunders where the **position side-to-move** equals the session's `player_color`. This prevents mixing blunders made as White with those made as Black. Use `positions.active_color` for efficient filtering.

**Reference Implementation (PostgreSQL):**

```sql
WITH RECURSIVE scent_path AS (
    -- BASE CASE: Starting moves from current position
    SELECT
        m.from_position_id,
        m.to_position_id,
        m.move_san AS root_move,
        1 AS depth,
        ARRAY[m.from_position_id] as path_history
    FROM moves m
    JOIN positions p ON p.id = m.from_position_id
    WHERE p.fen_hash = :current_fen_hash
      AND p.user_id = :user_id

    UNION ALL

    -- RECURSIVE STEP
    SELECT
        child.from_position_id,
        child.to_position_id,
        parent.root_move,
        parent.depth + 1,
        parent.path_history || child.from_position_id
    FROM moves child
    JOIN scent_path parent ON parent.to_position_id = child.from_position_id
    WHERE
        parent.depth < 5                                           -- Depth limit: 5-move steering radius
        AND NOT (child.to_position_id = ANY(parent.path_history))  -- Cycle detection
)
SELECT
    sp.root_move,
    MAX(
        (EXTRACT(EPOCH FROM NOW() - b.last_reviewed_at) / 3600)
        / (1.0 * POWER(2, b.pass_streak))
        * (b.eval_loss_cp / 50.0)
        / (1.0 + 0.1 * sp.depth)
    ) as best_score
FROM scent_path sp
JOIN blunders b ON b.position_id = sp.to_position_id
JOIN positions bp ON bp.id = b.position_id
JOIN game_sessions gs ON gs.id = :session_id AND gs.user_id = :user_id
WHERE b.user_id = :user_id
  AND bp.active_color = gs.player_color
GROUP BY sp.root_move
ORDER BY best_score DESC
LIMIT 1;
```

**Key Safeguards:**
- `depth < 5`: Steering radius—only considers blunders reachable within 5 moves, where the Ghost can reliably steer
- `path_history` array: Accumulates visited position IDs along each path
- `NOT ... = ANY(path_history)`: Prevents following edges that would create a cycle in the current traversal path

### 6.1.1 Re-Hooking Logic (Transposition Detection)

When the user deviates from the Ghost path, backend engine mode takes over. However, the user may later transpose into a known position that has a due blunder downstream. The Ghost should reactivate in this case.

**When to Check:** Every user move. The `POST /api/game/next-opponent-move` endpoint is called after each move regardless of current mode.

**Re-Hook Trigger:** The Ghost reactivates when:
1. The current position exists in the user's Ghost Move Library (matched by normalized FEN hash)
2. At least one blunder target with `srs_priority > 1.0` is reachable within 5 moves downstream (via the `blunders` table)

**Backend Logic:**

```
POST /api/game/next-opponent-move
After a user move, backend returns exactly one opponent move. It first tries Ghost steering; if no due path exists, it falls back to backend engine inference.


1. Validate session ownership and that it's the opponent's turn for `fen`.
2. Compute normalized FEN hash.
3. Look up position by `fen_hash` in `positions` table (for this user).
4. If found:
   → Run downstream blunder query (recursive CTE joining `blunders` table, depth ≤ 5).
   → Filter for blunders with `srs_priority > 1.0`.
5. If due blunder(s) reachable:
   → Select highest-priority path.
   → return:
     `{ "mode": "ghost", "move": { "uci": "...", "san": "..." }, "target_blunder_id": <id>, "decision_source": "ghost_path" }`
6. If no due blunders reachable:
   → call remote Maia3 API for engine move.
   → return:
     `{ "mode": "engine", "move": { "uci": "...", "san": "..." }, "target_blunder_id": null, "decision_source": "backend_engine" }`
```

**Performance Target:** Ghost-path lookup < 100ms for typical Ghost Move Libraries (< 10,000 positions). The 5-move depth cap keeps the search space small; full fallback (including Maia3 API call) should target sub-second p95 in MVP. The Maia3 remote API adds ~200-500ms network latency per engine fallback call.

**Caching Consideration (Post-MVP):** Position lookups are hot-path. Consider caching:
- FEN hash → position existence (simple boolean)
- Position ID → downstream blunder count (invalidate on new blunder insertion)

### 6.2 Ghost Move Library Capture Logic

Ghost Move Library targets enter the system through two capture paths.

#### 6.2.1 Automatic Capture (analysis-triggered blunder)

1. User plays move M from position P_before, resulting in position P_after.
2. **Worker B** (Frontend) calculates:
   * E_best (Eval of engine's best move from P_before)
   * E_user (Eval after user's move M)
3. If delta ≥ 50cp (recording threshold) **and** the move is within the first 10 moves of the game:
   * Frontend sends `POST /api/blunder` with:
     * `pgn`: Full game history up to and including the bad move (e.g., `"1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"`)
     * `fen`: The position BEFORE the bad move (P_before) — used as sanity check
     * `user_move`: The bad move played (SAN)
     * `best_move`: Engine's recommended move (SAN)
     * `eval_before` / `eval_after`: Centipawn evaluations
   * Backend builds the full Ghost Move Library path:
     1. **Replay PGN** using `python-chess` to generate all intermediate positions
     2. **Sanity check:** Verify position before final move matches provided `fen`. Reject with 422 if mismatch.
     3. **Insert positions:** For each position in the replay (including start), upsert into `positions` table (deduplicated by `fen_hash`)
     4. **Insert edges:** For each move in the PGN, upsert into `moves` table connecting consecutive positions
     5. **Create ghost-library target:** Insert/reuse row in `blunders` referencing P_before (the decision point)
4. This path enforces the session's first-auto-blunder rule via `game_sessions.blunder_recorded`.

#### 6.2.2 Manual Capture (MoveList-selected move)

1. User selects a move in MoveList and clicks **Add to Ghost Move Library**.
2. The client may call this flow during both active and ended games.
3. There is no capture threshold for this path: any eligible player move can be added.
4. Frontend sends `POST /api/blunder/manual` with PGN history through the selected move plus the selected pre-move FEN.
5. Backend replays that PGN history and upserts positions/moves exactly as in automatic capture.
6. Backend inserts/reuses the same `blunders` table keyed by `(user_id, position_id)`, then returns `is_new`.
7. If `is_new=false`, frontend shows duplicate UX: **"already in library"**.
8. Manual capture does not mutate `game_sessions.blunder_recorded`.

**Why store the full path:** The Ghost's scent query (Section 6.1) traverses from the current board position downstream to find reachable targets. Without intermediate positions in the Ghost Move Library, there's no path to traverse — Ghost would always fall back to engine mode.

**Critical semantic:** Every target references the **pre-move position** (P_before), because that's where the user faced the decision and will be tested again during SRS review.





### 6.3 The SRS Update Logic

#### 6.3.1 Replay Priority Score

Each blunder record has an SRS priority that tracks how overdue it is, combined with severity weighting for selection:

```
expected_interval = BASE_INTERVAL * (BACKOFF_FACTOR ^ pass_streak)
hours_since_review = (NOW - last_reviewed_at) in hours
srs_priority = hours_since_review / expected_interval
```

A blunder is **due** when `srs_priority > 1.0` (overdue for review).

When selecting which blunder to steer toward during a game, severity and distance are factored in:

```
score = srs_priority * (eval_loss_cp / 50) / (1 + 0.1 * distance)
```

- `eval_loss_cp / 50`: Severity weight — a 200cp blunder scores 4× higher than a 50cp inaccuracy at the same overdue-ness
- `1 + 0.1 * distance`: Distance tiebreaker — closer blunders slightly preferred within the 5-move steering radius

**Constants (MVP defaults):**
- `BASE_INTERVAL = 1` (hour)
- `BACKOFF_FACTOR = 2.0` (exponential backoff)
- `MAX_INTERVAL = 4320` (180 days in hours, cap)
- `STEERING_RADIUS = 5` (max moves to steer toward a blunder)
- `RECORDING_MOVE_CAP = 10` (only record mistakes in the first 10 moves)

**SRS Priority Examples (before severity/distance weighting):**
| pass_streak | expected_interval | After 1hr | After 24hr | After 7 days |
|-------------|-------------------|-----------|------------|--------------|
| 0 (new)     | 1 hr              | 1.0       | 24.0       | 168.0        |
| 1           | 2 hr              | 0.5       | 12.0       | 84.0         |
| 3           | 8 hr              | 0.125     | 3.0        | 21.0         |
| 5           | 32 hr             | 0.03      | 0.75       | 5.25         |
| 10          | 1024 hr (~43 days)| 0.001     | 0.02       | 0.16         |

**Selection Score Examples (srs_priority=2.0, distance=1):**
| eval_loss_cp | Severity Weight | Score |
|--------------|-----------------|-------|
| 50cp         | 1.0×            | 1.82  |
| 100cp        | 2.0×            | 3.64  |
| 200cp        | 4.0×            | 7.27  |

Higher score = more likely to be selected when Ghost chooses a path.

#### 6.3.2 Update Rules

1. User arrives at a position that has an associated blunder record (i.e., `blunders.position_id` matches current position).
2. User plays a move from this position.
3. **Worker B** (Frontend) evaluates the move in real-time.

4. **Scenario A (Fail - Suboptimal move):**
   * Move drops eval by ≥50cp compared to best move
   * Result: `Fail`
   * Backend updates `blunders` record: `pass_streak = 0`, `last_reviewed_at = NOW`
   * Note: Any move outside the 50cp threshold fails, whether it's a minor inaccuracy or a major blunder. This is the same threshold used for recording.

5. **Scenario B (Pass - Good move):**
   * Move is within 50cp of best move's eval
   * Result: `Pass`
   * Backend updates `blunders` record: `pass_streak += 1`, `last_reviewed_at = NOW`

#### 6.3.3 Evaluation Thresholds

The system uses a single 50cp threshold for both recording and review:

| Threshold | Value | Purpose |
|-----------|-------|---------|
| **SRS Pass** | 50cp | Move must be within 50cp of best to pass review |
| **Recording** | 50cp | Move must lose ≥50cp to be recorded as Ghost Move Library target |

**SRS Pass Criteria:**
A move passes review if the **real-time engine evaluation** shows:
- Eval drop < 50cp compared to engine's best move

This means:
- User doesn't have to play *the* engine's top move
- Any move within 50cp of optimal passes (multiple solutions accepted)
- The stored `bad_move_san` is for display only, not for pass/fail logic

**Recording Criteria:**
A move is recorded as a new Ghost Move Library target if:
- Eval drop ≥ 50cp compared to engine's best move
- The move is within the first 10 moves of the game

**Design Rationale:**
- 50cp threshold catches inaccuracies, not just major blunders — opening inaccuracies are worth drilling because the positions recur frequently
- The first-10-moves cap keeps the target pool focused on reachable positions (low branching factor in openings) and prevents the library from filling with unreachable middlegame/endgame positions
- Severity weighting in the priority formula ensures major blunders still surface before minor inaccuracies

### 6.4 Engine Evaluation Protocol

Worker B (the Analyst) produces all engine evaluations used for blunder detection, SRS grading, and post-game analysis. To ensure consistent, reproducible results, the following protocol applies.

#### 6.4.1 Search Parameters

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Depth | 18 (minimum) | Sufficient tactical accuracy; diminishing returns beyond |
| Time limit | 2000ms | Upper bound to maintain UI responsiveness |
| MultiPV | 1 | Only the best move needed for delta calculation |
| Threads | 1 | Web Worker constraint; WASM single-threaded |

**Stopping condition:** Search terminates when EITHER depth 18 is reached OR 2000ms elapsed, whichever comes first. The evaluation from the final `info` line before `bestmove` is used.

**Implementation (JavaScript):**
```javascript
// Send to Stockfish worker
worker.postMessage('setoption name MultiPV value 1');
worker.postMessage(`position fen ${fen}`);
worker.postMessage('go depth 18 movetime 2000');

// Capture last info line before bestmove
let lastEval = null;
worker.onmessage = (e) => {
  const line = e.data;
  if (line.startsWith('info') && line.includes('score')) {
    lastEval = parseInfoLine(line);
  }
  if (line.startsWith('bestmove')) {
    onEvalComplete(lastEval);
  }
};
```

#### 6.4.2 Evaluation Perspective (Sign Convention)

Stockfish reports evaluations from **White's perspective** by default. All evaluations are **normalized to side-to-move perspective** before storage and delta comparison.

**Normalization rule:**
```
normalized_eval = raw_eval * (1 if white_to_move else -1)
```

**Example:**
| Position | Stockfish Raw | Side to Move | Normalized |
|----------|---------------|--------------|------------|
| After 1.e4 | +30 | Black | -30 |
| After 1.e4 e5 | +25 | White | +25 |
| After 1.e4 e5 2.Qh5 | +45 | Black | -45 |

**Delta calculation:** Always computed as `best_move_eval - played_move_eval` using normalized values. A positive delta means the played move was worse.

**Storage:** The `session_moves.eval_cp` column stores the **normalized** (side-to-move) value.

#### 6.4.3 Mate Score Conversion

When Stockfish reports mate scores (`score mate N`), they are converted to centipawn equivalents for threshold comparison.

**Conversion formula:**
```
eval_cp = sign * (MATE_BASE - abs(moves_to_mate) * MATE_DECAY)

Constants:
  MATE_BASE  = 10000
  MATE_DECAY = 10
  sign       = +1 if winning (positive N), -1 if losing (negative N)
```

**Conversion table:**
| Stockfish Output | Meaning | Centipawn Equivalent |
|------------------|---------|---------------------|
| `score mate 1` | Side-to-move mates in 1 | +9990 |
| `score mate 5` | Side-to-move mates in 5 | +9950 |
| `score mate 20` | Side-to-move mates in 20 | +9800 |
| `score mate -1` | Side-to-move gets mated in 1 | -9990 |
| `score mate -3` | Side-to-move gets mated in 3 | -9970 |

**Threshold application:** Mate scores use converted centipawn values for all comparisons:

| Scenario | Calculation | Result |
|----------|-------------|--------|
| Had M3, played move keeps M5 | `9970 - 9950 = 20cp` | Pass (< 50cp) |
| Had M3, played move loses to +500 | `9970 - 500 = 9470cp` | Recorded (≥ 50cp) |
| Had +200, blundered into M-5 | `200 - (-9950) = 10150cp` | Recorded (≥ 50cp) |
| Had M-10, delayed to M-15 | `-9900 - (-9850) = -50cp` → abs = 50cp | Borderline pass |

**Database storage:** When eval is a mate score:
- `eval_cp` = NULL
- `eval_mate` = N (positive = winning, negative = losing)
- Delta calculations use the converted value at comparison time

#### 6.4.4 Evaluation Stability

Engine evaluations fluctuate during iterative deepening. The protocol uses **depth-gated snapshots** rather than convergence detection.

**Rule:** Use the evaluation reported at the stopping condition (depth 18 reached or 2000ms elapsed). Do not wait for successive identical evaluations.

**Rationale:**
- Convergence detection adds latency and implementation complexity
- Depth 18 provides sufficient stability for tactically critical positions
- Positions with high eval variance at depth 18 are typically near-equal (within ±50cp of 0.00)
- The 2000ms timeout prevents pathological positions from blocking the UI

**Known limitation:** In rare positions (e.g., deep sacrificial lines, fortress detection), depth 18 may not capture the full picture. This is acceptable for MVP; users experiencing systematic false blunders can be addressed post-MVP with configurable depth.

#### 6.4.5 Edge Cases

| Scenario | Handling |
|----------|----------|
| **Tablebase position** | Stockfish handles internally; reports mate distance or draw |
| **Book opening moves** | Evaluate normally; no special-casing for theory |
| **Threefold repetition claim available** | Engine may report 0cp; user's non-draw move compared to 0 |
| **50-move rule proximity** | Engine accounts internally; may report draw |
| **Worker crash/timeout** | Skip evaluation for that move; log error; do not flag as blunder |
| **Eval exactly at threshold** | ≥50cp = recorded and fails review (inclusive boundary) |

#### 6.4.6 Frontend Implementation Notes

**Batching:** Worker B evaluates moves asynchronously. During fast play, evaluations may queue. Process in order; never skip a move.

**Memory:** Each evaluation result is held in memory during the game and batch-uploaded on game end (see Section 7.4).

**Error recovery:** If Worker B fails to initialize (WASM load failure), the game continues without analysis. The `session_moves` table will be empty for that game, and no automatic blunders can be recorded (manual MoveList capture is still available).

---

## 7. Game Sessions & Lifecycle

A **game session** represents a single game from start to termination. Sessions enforce the "first auto-blunder only" rule for analysis-triggered capture, track game outcomes, and store game history with analysis.

### 7.1 Session Definition

A session begins when the user clicks "New Game" and ends when the game terminates (checkmate, resignation, draw, or abandonment). Each session has a unique identifier.

### 7.2 Game States

```
┌─────────┐     New Game     ┌─────────────┐     Terminal Event     ┌─────────┐
│  IDLE   │ ───────────────► │ IN_PROGRESS │ ─────────────────────► │  ENDED  │
└─────────┘                  └─────────────┘                        └─────────┘
                                   │                                     │
                                   │ Browser close/refresh               │
                                   ▼                                     │
                             ┌───────────┐                               │
                             │ ABANDONED │ ◄─────────────────────────────┘
                             └───────────┘   (timeout after disconnect)
```

**State Transitions:**
| From | To | Trigger |
|------|------|---------|
| IDLE | IN_PROGRESS | User clicks "New Game" |
| IN_PROGRESS | ENDED | Checkmate, stalemate, resignation, draw agreement |
| IN_PROGRESS | ABANDONED | Browser disconnect + timeout (MVP: 5 minutes) |

### 7.3 Session Schema

```sql
CREATE TABLE game_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(id),
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'in_progress',  -- 'in_progress', 'ended', 'abandoned'
    result VARCHAR(20),           -- 'checkmate_win', 'checkmate_loss', 'resign', 'draw', 'abandon'
    engine_elo INTEGER NOT NULL,  -- Bot difficulty selected for this game
    player_color VARCHAR(5) NOT NULL, -- 'white' or 'black' (user side for this session)
    blunder_recorded BOOLEAN NOT NULL DEFAULT FALSE,  -- First auto-blunder rule flag (manual captures bypass)
    pgn TEXT,                     -- Full game in PGN format

    CONSTRAINT valid_status CHECK (status IN ('in_progress', 'ended', 'abandoned')),
    CONSTRAINT valid_result CHECK (result IS NULL OR result IN (
        'checkmate_win', 'checkmate_loss', 'resign', 'draw', 'abandon'
    )),
    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black'))
);

CREATE INDEX idx_sessions_user ON game_sessions(user_id);
CREATE INDEX idx_sessions_active ON game_sessions(user_id, status) WHERE status = 'in_progress';
```

### 7.4 Move Analysis Storage

Per-move engine evaluations are captured during gameplay (from Worker B) and stored for post-game review.

```sql
CREATE TABLE session_moves (
    id BIGSERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES game_sessions(id) ON DELETE CASCADE,
    move_number INTEGER NOT NULL,      -- 1, 2, 3... (full moves, not half-moves)
    color VARCHAR(5) NOT NULL,         -- 'white' or 'black'
    move_san VARCHAR(10) NOT NULL,     -- e.g., "Nf3", "O-O"
    fen_after TEXT NOT NULL,           -- Position after this move
    eval_cp INTEGER,                   -- Engine eval in centipawns (NULL if mate)
    eval_mate INTEGER,                 -- Moves to mate (NULL if not mate)
    best_move_san VARCHAR(10),         -- Engine's recommended move
    best_move_eval_cp INTEGER,         -- Eval if best move was played
    eval_delta INTEGER,                -- best_move_eval - actual_eval (positive = lost advantage)
    classification VARCHAR(20),        -- 'best', 'excellent', 'good', 'inaccuracy', 'mistake', 'blunder'

    CONSTRAINT valid_color CHECK (color IN ('white', 'black')),
    UNIQUE (session_id, move_number, color)
);

CREATE INDEX idx_session_moves_session ON session_moves(session_id);
```

**Classification Thresholds:**
| Classification | Eval Delta (centipawns) |
|----------------|-------------------------|
| best           | 0 (played engine's top choice) |
| excellent      | 1-10 |
| good           | 11-50 |
| inaccuracy     | 51-100 |
| mistake        | 101-149 |
| blunder        | ≥ 150 |

**Data Flow:**
1. User plays move → Worker B evaluates position
2. Frontend stores eval data in memory during game
3. On game end → Frontend sends full analysis batch to `POST /api/session/{id}/moves`
4. Server bulk-inserts all `session_moves` records

### 7.5 First-Auto-Blunder Rule Enforcement

The `blunder_recorded` flag ensures only one automatically detected blunder per session enters the Ghost Move Library:

```
POST /api/blunder called
        │
        ▼
┌───────────────────────┐
│ Check session.blunder │
│   _recorded flag      │
└───────────────────────┘
        │
   ┌────┴────┐
   │         │
 FALSE      TRUE
   │         │
   ▼         ▼
┌─────────┐  ┌─────────┐
│ Record  │  │ Ignore  │
│ blunder │  │ (return │
│ to graph│  │  200 OK)│
│ Set flag│  └─────────┘
│ = TRUE  │
└─────────┘
```

**API Behavior:**
1. Client sends `POST /api/blunder` with `{ session_id, fen, user_move, eval_delta }`
2. Server checks `blunder_recorded` flag on session
3. If `FALSE`: Insert blunder into the Ghost Move Library, set flag `TRUE`, return `201 Created`
4. If `TRUE`: Skip insertion, return `200 OK` with `{ "recorded": false, "reason": "session_limit" }`
5. `POST /api/blunder/manual` is not subject to this flag (manual capture is allowed in active and ended sessions).

### 7.6 Game Termination

**Resignation:**
- User clicks "Resign" button
- Frontend sends `POST /api/game/end` with `{ "session_id": "{id}", "result": "resign" }`
- Session marked as ended

**Checkmate/Stalemate:**
- `chess.js` detects game over state
- Frontend sends `POST /api/game/end` with `{ "session_id": "{id}", "result": "<outcome>" }`
- Session marked as ended

**Abandonment (MVP):**
- User closes browser or navigates away
- Session remains `in_progress`
- Background job marks sessions as `abandoned` if no activity for 5+ minutes
- Abandoned sessions are treated as ended for all purposes

### 7.7 Session Persistence

**What IS persisted:**
- Session metadata (start/end times, result, engine Elo)
- Full PGN of the game
- Per-move engine analysis (eval, best move, classification)
- Ghost Move Library targets: auto blunders and manually selected MoveList decisions (anchored to the `positions` + `moves` graph)

**Browser Refresh Behavior (MVP):**
- Refreshing mid-game loses the current game state
- User must start a new game
- Previous session auto-abandoned after timeout
- *Future enhancement: LocalStorage-based state recovery*

---

## 8. MVP Constraints & Scope

* **Single Variation per Game:** The system only records the *first* blunder of a session to keep the Ghost Move Library manageable initially.
* **No Redis:** All state checks go directly to PostgreSQL (acceptable performance for turn-based MVP).

---

## 9. API Specification

All endpoints use JSON request/response bodies. The API is RESTful.

### 9.1 Base URL

```
/api
```

### 9.2 Authentication

All endpoints except `/api/auth/*` require authentication via Bearer token.

**Header:** `Authorization: Bearer <jwt_token>`

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/register` | Create anonymous account (auto-generated credentials) |
| POST | `/api/auth/login` | Authenticate and get token (optional, cross-device) |
| POST | `/api/auth/claim` | Upgrade anonymous account to claimed account |
| POST | `/api/auth/logout` | Invalidate token (optional for stateless JWT) |

#### POST /api/auth/register

Creates an anonymous user account with auto-generated credentials. Called automatically by frontend on first visit.

**Request:**
```json
{
  "username": "string (3-32 chars, auto-generated by frontend)",
  "password": "string (auto-generated by frontend)"
}
```

**Response (201):**
```json
{
  "user_id": "integer",
  "username": "string",
  "is_anonymous": true,
  "token": "string (JWT)"
}
```

**Implementation Notes:**
- Creates user with `is_anonymous = TRUE`
- Username should be auto-generated format (e.g., `ghost_<random>`)
- No email validation or CAPTCHA for MVP

#### POST /api/auth/login

Authenticates existing user and returns JWT. Optional endpoint - mainly for cross-device access.

**Request:**
```json
{
  "username": "string",
  "password": "string"
}
```

**Response (200):**
```json
{
  "user_id": "integer",
  "username": "string",
  "is_anonymous": "boolean",
  "token": "string (JWT)"
}
```

**Implementation Notes:**
- Works for both anonymous and claimed accounts
- Frontend auto-calls this on subsequent visits using localStorage credentials
- Most users never manually use this endpoint

#### POST /api/auth/claim

Upgrades an anonymous account to a claimed (permanent) account. Allows user to choose custom username and password.

**Request:**
```json
{
  "new_username": "string (3-32 chars, alphanumeric + underscore)",
  "new_password": "string (min 8 chars)"
}
```

**Headers:**
- `Authorization: Bearer <jwt_token>` (current anonymous user's token)

**Response (200):**
```json
{
  "user_id": "integer",
  "username": "string (new username)",
  "is_anonymous": false,
  "token": "string (new JWT with updated claims)"
}
```

**Error Responses:**

| Status | Condition |
|--------|-----------|
| 400 | User already claimed (`is_anonymous = FALSE`) |
| 409 | New username already taken |
| 422 | Invalid username/password format |

**Implementation Notes:**
- Verify current user `is_anonymous = TRUE`
- Check new username availability
- Update user record: `username`, `password_hash`, `is_anonymous = FALSE`, `updated_at`
- Return new JWT with updated claims
- Frontend updates localStorage with new credentials

### 9.3 Game Flow

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/game/start` | Start new game session |
| POST | `/api/game/next-opponent-move` | Get next opponent move (ghost-first, engine fallback) |
| POST | `/api/game/end` | End game session |
| POST | `/api/session/:id/moves` | Upload full move analysis for a session |

#### POST /api/game/start

Creates a new game session. The session tracks which game the blunders belong to.

**Request:**
```json
{
  "engine_elo": "integer",
  "player_color": "white | black"
}
```

**Response (201):**
```json
{
  "session_id": "uuid",
  "engine_elo": "integer",
  "player_color": "white | black"
}
```

#### POST /api/game/next-opponent-move

Given a position, returns the next opponent move from Ghost-path traversal if available, otherwise from the remote Maia3 API.

**Request:**
```json
{
  "session_id": "uuid",
  "fen": "string",
  "moves": ["string (UCI)", "..."]
}
```

- `moves`: Full game move history as UCI strings from the starting position (e.g. `["e2e4", "e7e5", "g1f3"]`). Required for engine fallback — the Maia3 API accepts move history rather than FEN. The frontend tracks these alongside its existing `moveHistory` state.

**Response (200):**
```json
{
  "mode": "ghost | engine",
  "move": {
    "uci": "string",
    "san": "string"
  },
  "target_blunder_id": "integer | null",
  "decision_source": "ghost_path | backend_engine"
}
```

- `mode: "ghost"` - Ghost is steering toward a blunder; `move` contains the next move.
- `mode: "engine"` - No blunder path found; `move` is produced by the remote Maia3 API.
- `target_blunder_id` - ID of the blunder being targeted (for debugging/display), or `null` in engine mode.
- `decision_source` - Backend decision branch used to produce the move.

#### POST /api/game/end

Ends the current game session.

**Request:**
```json
{
  "session_id": "uuid",
  "result": "checkmate_win | checkmate_loss | resign | draw | abandon"
}
```

**Response (200):**
```json
{
  "session_id": "uuid",
  "blunders_recorded": "integer",
  "blunders_reviewed": "integer"
}
```

`result` values match `game_sessions.result` exactly:
- `checkmate_win` – user delivered mate
- `checkmate_loss` – user was checkmated
- `resign` – user resigned
- `draw` – draw by stalemate/agreement/repetition/etc.
- `abandon` – client disconnected and timeout elapsed

Frontend helpers may expose simplified UI strings (e.g., "Win"), but the payload must send the canonical enum for consistency across storage and analytics.

#### POST /api/session/:id/moves

Bulk-ingests the analyzed move data collected during the session. The request mirrors the `session_moves` schema so the backend can persist evaluations without parsing PGN annotations.

**Request:**
```json
{
  "moves": [
    {
      "move_number": 1,
      "color": "white",
      "move_san": "e4",
      "fen_after": "string",
      "eval_cp": 20,
      "eval_mate": null,
      "best_move_san": "e4",
      "best_move_eval_cp": 20,
      "eval_delta": 0,
      "classification": "best"
    }
  ]
}
```

- `session_id` comes from the path parameter.
- `eval_cp` / `best_move_eval_cp` use the normalized centipawn scale described in §7.4.
- `classification` must be one of `best|excellent|good|inaccuracy|mistake|blunder`.

**Response (200):**
```json
{
  "moves_inserted": "integer"
}
```

**Rules:**
- Endpoint is called once per completed game; repeat calls replace the existing move set for idempotency.
- Any PGN string is still sent to `/api/game/end` (stored in `game_sessions.pgn`), while this endpoint remains JSON so downstream analytics don't need to re-parse PGN comments.

### 9.4 Blunders / Ghost Move Library Targets

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/blunder` | Record an auto-detected blunder (analysis-triggered) |
| POST | `/api/blunder/manual` | Manually add a MoveList decision to the Ghost Move Library |
| GET | `/api/blunders` | List user's Ghost Move Library targets |
| GET | `/api/blunders/:id` | Get single Ghost Move Library target details |

#### POST /api/blunder

Records a mistake detected by the client-side engine (delta >= 50cp, within first 10 moves). Stores the full path from game start to the target position in the Ghost Move Library.
This endpoint enforces the first-auto-blunder-per-session rule and the 10-move recording cap.

**Request:**
```json
{
  "session_id": "uuid",
  "pgn": "string (full game history, e.g. '1. e4 e5 2. Nf3 Nc6 3. Bb5 a6')",
  "fen": "string (position BEFORE bad move - used as sanity check)",
  "user_move": "string (SAN of bad move, should match last move in PGN)",
  "best_move": "string (SAN of engine's best move)",
  "eval_before": "integer (centipawns, eval of best move)",
  "eval_after": "integer (centipawns, eval after user's move)"
}
```

**Backend Processing:**
1. Parse PGN and replay to generate all intermediate positions
2. Verify position before final move matches `fen` (reject with 422 if mismatch)
3. Upsert all positions into the Ghost Move Library (deduplicated by `fen_hash`)
4. Upsert all edges connecting consecutive positions
5. Create blunder record referencing the pre-move position (decision point)

**Response (201):**
```json
{
  "blunder_id": "integer",
  "position_id": "integer",
  "positions_created": "integer",
  "is_new": "boolean"
}
```

- `positions_created`: Number of new positions added to the Ghost Move Library (0 if path already existed)
- `is_new: false` means this position already has a Ghost Move Library target (frontend message: "already in library")

#### POST /api/blunder/manual

Manually adds a MoveList decision point to the Ghost Move Library. This endpoint is allowed for both active and ended sessions.

**Request:**
```json
{
  "session_id": "uuid",
  "pgn": "string (game history up to and including the selected move)",
  "fen": "string (position BEFORE selected move - used as sanity check)",
  "user_move": "string (SAN of selected move)",
  "best_move": "string | null (engine best move if available)",
  "eval_before": "integer | null (centipawns, optional metadata)",
  "eval_after": "integer | null (centipawns, optional metadata)"
}
```

**Rules:**
1. No 50cp threshold is applied; any eligible player move can be added.
2. Backend replays PGN and upserts positions/moves exactly like automatic capture.
3. Backend inserts/reuses `(user_id, position_id)` target row in `blunders`.
4. Duplicate capture returns `is_new=false` so UI can show "already in library".
5. This endpoint does not set or check `game_sessions.blunder_recorded`.

**Response (201):**
```json
{
  "blunder_id": "integer",
  "position_id": "integer",
  "positions_created": "integer",
  "is_new": "boolean"
}
```

#### GET /api/blunders

Lists the user's recorded Ghost Move Library targets (auto blunders + manual MoveList selections).

**Query Parameters:**
- `due` (boolean, optional) - Only return blunders with srs_priority > 1.0 (overdue for review)
- `limit` (integer, optional, default 50, max 100)

**Response (200):**
```json
{
  "blunders": [
    {
      "id": "integer",
      "position_id": "integer",
      "fen": "string (the decision point position)",
      "bad_move": "string (SAN captured when target was added)",
      "best_move": "string (SAN of engine's recommendation)",
      "eval_loss_cp": "integer",
      "pass_streak": "integer",
      "priority": "float",
      "last_reviewed_at": "timestamp | null",
      "created_at": "timestamp"
    }
  ]
}
```

#### GET /api/blunders/:id

**Response (200):**
```json
{
  "id": "integer",
  "position_id": "integer",
  "fen": "string (the decision point position)",
  "bad_move": "string (SAN captured when target was added)",
  "best_move": "string (SAN of engine's recommendation)",
  "eval_loss_cp": "integer",
  "pass_streak": "integer",
  "priority": "float",
  "last_reviewed_at": "timestamp | null",
  "created_at": "timestamp",
  "review_history": [
    {
      "reviewed_at": "timestamp",
      "passed": "boolean",
      "move_played": "string (SAN)"
    }
  ]
}
```

`review_history` is sourced from the most recent entries in `blunder_reviews` (ordered `reviewed_at DESC`). Include every recorded attempt for now; pagination can be added later if needed.

### 9.5 SRS (Spaced Repetition)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/srs/review` | Record a review result |

#### POST /api/srs/review

Records whether the user passed or failed a blunder review.

**Request:**
```json
{
  "session_id": "uuid",
  "blunder_id": "integer",
  "passed": "boolean",
  "user_move": "string (SAN)",
  "eval_delta": "integer (centipawns)"
}
```

**Response (200):**
```json
{
  "blunder_id": "integer",
  "pass_streak": "integer",
  "priority": "float",
  "next_expected_review": "timestamp"
}
```

**Side effects:**
- Insert a row into `blunder_reviews` capturing `{blunder_id, session_id, reviewed_at, passed, move_played_san, eval_delta_cp}`
- Update the parent `blunders` row: `pass_streak` (reset or increment) and `last_reviewed_at = reviewed_at`
- Recalculate priority / due logic using the updated SRS state

### 9.6 Error Responses

All errors follow a consistent format:

```json
{
  "error": {
    "code": "string",
    "message": "string",
    "details": "object | null"
  }
}
```

**Standard Error Codes:**

| HTTP Status | Code | Description |
|-------------|------|-------------|
| 400 | `invalid_request` | Malformed request body or parameters |
| 401 | `unauthorized` | Missing or invalid auth token |
| 403 | `forbidden` | Valid token but insufficient permissions |
| 404 | `not_found` | Resource does not exist |
| 409 | `conflict` | Resource already exists (e.g., duplicate username) |
| 422 | `validation_error` | Request valid but data constraints violated |
| 500 | `internal_error` | Server error |

**Example (401):**
```json
{
  "error": {
    "code": "unauthorized",
    "message": "Token expired",
    "details": null
  }
}
```

### 9.7 Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| API Style | REST | Simpler than GraphQL for MVP; FastAPI excels at REST |
| Pagination | Deferred | MVP uses simple limit; cursor-based pagination post-MVP |
| Auth | Stateless JWT | No session storage needed; simple horizontal scaling |
| Error Format | Structured JSON | Consistent parsing for frontend error handling |

---

## 10. After-Game Analysis Display

When a game ends, users are presented with an analysis view showing their performance with engine evaluations.

### 10.1 Screen Layout

```
┌─────────────────────────────────────────────────────────────────────┐
│  ┌───────────────────────┐   ┌─────────────────────────────────┐   │
│  │                       │   │  Evaluation Graph               │   │
│  │                       │   │  ┌─────────────────────────────┐│   │
│  │      Chessboard       │   │  │    ▲                        ││   │
│  │                       │   │  │   / \    /\                 ││   │
│  │                       │   │  │  /   \  /  \      /\        ││   │
│  │                       │   │  │0├─────\/────\────/──\──     ││   │
│  │                       │   │  │  ▼          \  /    ▼       ││   │
│  └───────────────────────┘   │  │              \/             ││   │
│                              │  └─────────────────────────────┘│   │
│  ┌───────────────────────┐   │  Move: 15 of 42                 │   │
│  │ ◀◀  ◀  ▶  ▶▶        │   └─────────────────────────────────┘   │
│  │ Navigation Controls   │                                        │
│  └───────────────────────┘   ┌─────────────────────────────────┐   │
│                              │  Move List (scrollable)         │   │
│  ┌───────────────────────┐   │  1. e4    e5                    │   │
│  │ Eval Bar              │   │  2. Nf3   Nc6                   │   │
│  │ ████████░░  +1.2      │   │  3. Bb5   a6                    │   │
│  └───────────────────────┘   │  4. Ba4   Nf6                   │   │
│                              │  5. O-O   Be7                    │   │
│  ┌───────────────────────┐   │  6. Re1   b5?!  ← inaccuracy    │   │
│  │ Current Position      │   │  7. Bb3   d6                    │   │
│  │ Best: Nc6 (+0.3)      │   │  8. c3    O-O                   │   │
│  │ Played: d5?? (-2.1)   │   │  9. h3    Na5??  ← BLUNDER      │   │
│  │ Classification: Blunder│   │  ...                           │   │
│  └───────────────────────┘   └─────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ [New Game]  [Review Blunders]  [Back to Dashboard]          │   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 10.2 Components

#### 10.2.1 Chessboard

- Displays the position at the currently selected move
- Arrows can optionally show the best move (toggle)
- Highlights the last move played (from/to squares)

#### 10.2.2 Evaluation Graph

- **X-axis:** Move number (1 to N)
- **Y-axis:** Engine evaluation in pawns (-5 to +5, clamped)
- **Line color:** Gradient from white's perspective (green = white advantage, red = black advantage)
- **Markers:** Dots on the line at each move; colored by classification:
  - Red dot: Blunder (≥150cp loss)
  - Orange dot: Mistake (101-149cp loss)
  - Yellow dot: Inaccuracy (51-100cp loss)
- **Interaction:** Clicking on the graph jumps to that move
- **Current position:** Vertical line indicator shows selected move

#### 10.2.3 Evaluation Bar

- Vertical or horizontal bar showing current position advantage
- Filled portion represents winning probability (based on eval)
- Numerical eval displayed: `+1.2` or `M3` (mate in 3)
- Color: White fill for white advantage, black fill for black advantage

#### 10.2.4 Navigation Controls

| Button | Action |
|--------|--------|
| ◀◀ | Jump to start |
| ◀ | Previous move |
| ▶ | Next move |
| ▶▶ | Jump to end |

**Keyboard shortcuts:**
- `←` / `→` : Previous/next move
- `Home` / `End` : Jump to start/end
- `↑` / `↓` : Jump to previous/next critical moment (blunder/mistake)

#### 10.2.5 Move List

- Standard two-column format (white move | black move)
- Current move highlighted
- Color-coded annotations:
  - `??` (blunder) - Red background
  - `?` (mistake) - Orange background
  - `?!` (inaccuracy) - Yellow background
  - `!` (good move) - Light green background
  - `!!` (brilliant) - Green background (if within 5cp of engine and non-obvious)
- Clicking a move navigates to that position

#### 10.2.6 Position Analysis Panel

Shows details for the currently selected move:

- **Best move:** Engine's recommended move with eval
- **Played move:** What was actually played with eval
- **Eval delta:** Difference in centipawns
- **Classification:** Blunder/Mistake/Inaccuracy/Good/Excellent/Best

### 10.3 Data Source

Analysis data comes from the `session_moves` table, populated during gameplay by Worker B:

```typescript
interface MoveAnalysis {
  moveNumber: number;
  color: 'white' | 'black';
  moveSan: string;
  fenAfter: string;
  evalCp: number | null;      // null if mate
  evalMate: number | null;    // moves to mate
  bestMoveSan: string;
  bestMoveEvalCp: number;
  evalDelta: number;
  classification: 'best' | 'excellent' | 'good' | 'inaccuracy' | 'mistake' | 'blunder';
}
```

### 10.4 API Endpoint

#### GET /api/session/:id/analysis

Returns full analysis for a completed game session.

**Response (200):**
```json
{
  "session_id": "uuid",
  "pgn": "string",
  "result": "checkmate_win | checkmate_loss | resign | draw",
  "moves": [
    {
      "move_number": 1,
      "color": "white",
      "move_san": "e4",
      "fen_after": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
      "eval_cp": 30,
      "eval_mate": null,
      "best_move_san": "e4",
      "best_move_eval_cp": 30,
      "eval_delta": 0,
      "classification": "best"
    }
  ],
  "summary": {
    "blunders": 2,
    "mistakes": 3,
    "inaccuracies": 5,
    "average_centipawn_loss": 24
  }
}
```

### 10.5 Entry Points

The analysis screen is accessible from two locations:

```
                    ┌─────────────────┐
                    │   Game Ends     │
                    │                 │
                    │ "View Analysis?"│
                    │  [Yes]   [No]   │
                    └────────┬────────┘
                             │ Yes
                             ▼
┌─────────────────┐    ┌─────────────────┐
│  Game History   │───►│ Analysis Screen │
│  (select game)  │    │   (this spec)   │
└─────────────────┘    └────────┬────────┘
                                │
                                ▼
                    ┌─────────────────────┐
                    │  [New Game]         │──► Start new game
                    │  [Review Blunders]  │──► Blunders list
                    │  [Game History]     │──► Past games
                    │  [Dashboard]        │──► Main menu
                    └─────────────────────┘
```

**Entry Point 1: Post-Game Prompt**
- Immediately after a game ends, user is prompted "View Analysis?"
- Selecting "Yes" opens analysis for the just-completed game
- Selecting "No" returns to dashboard

**Entry Point 2: Game History**
- User navigates to Game History from dashboard
- Selects any completed game from the list
- Opens analysis screen for that historical game

### 10.6 MVP Constraints

- **No engine lines:** MVP shows only the single best move, not multiple variations
- **No local analysis:** Display only the analysis captured during gameplay (no re-analysis)
- **No export:** PGN download deferred to post-MVP
- **No sharing:** Social/sharing features deferred

---

## 11. Game History View

The Game History view allows users to browse their past games and access analysis for any completed game.

### 11.1 Entry Points

```
┌─────────────────────┐
│     Dashboard       │
│                     │
│  [New Game]         │
│  [Game History] ────┼──────► Game History View
│  [Due Blunders]     │
└─────────────────────┘

┌─────────────────────┐
│   Game Ends         │
│                     │
│  "View Analysis?"   │
│  [Yes] → Analysis   │
│  [No]  → Dashboard  │
│  [History] ─────────┼──────► Game History View
└─────────────────────┘
```

### 11.2 Screen Layout

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Game History                                                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ ▶ Jan 31, 2026 • 10:45 AM                                           │   │
│  │   Result: Won (Checkmate)  •  vs Bot (1200)  •  32 moves            │   │
│  │   Blunders: 1  •  Mistakes: 2  •  Inaccuracies: 4                   │   │
│  │   Avg Centipawn Loss: 18                                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ ▶ Jan 30, 2026 • 3:22 PM                                            │   │
│  │   Result: Lost (Checkmate)  •  vs Bot (1400)  •  45 moves           │   │
│  │   Blunders: 3  •  Mistakes: 1  •  Inaccuracies: 2                   │   │
│  │   Avg Centipawn Loss: 42                                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ ▶ Jan 30, 2026 • 11:08 AM                                           │   │
│  │   Result: Draw (Stalemate)  •  vs Bot (1000)  •  58 moves           │   │
│  │   Blunders: 0  •  Mistakes: 3  •  Inaccuracies: 5                   │   │
│  │   Avg Centipawn Loss: 12                                            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ... (scrollable list, newest first)                                        │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────┐     │
│  │  [Back to Dashboard]                                               │     │
│  └───────────────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 11.3 Game Card Data

Each game in the list displays:

| Field | Source | Example |
|-------|--------|---------|
| Date/Time | `game_sessions.started_at` | Jan 31, 2026 • 10:45 AM |
| Result | `game_sessions.result` | Won (Checkmate), Lost (Resign), Draw |
| Opponent Elo | `game_sessions.engine_elo` | vs Bot (1200) |
| Move Count | Derived from PGN | 32 moves |
| Blunders | Count from `session_moves` | 2 |
| Mistakes | Count from `session_moves` | 3 |
| Inaccuracies | Count from `session_moves` | 5 |
| Avg CP Loss | Computed from `session_moves.eval_delta` | 18 |

**Result Display Mapping:**

| `result` value | Display Text |
|----------------|--------------|
| `checkmate_win` | Won (Checkmate) |
| `checkmate_loss` | Lost (Checkmate) |
| `resign` | Lost (Resigned) |
| `draw` | Draw |
| `abandon` | Abandoned |

### 11.4 Interaction Flow

```
User clicks game card
        │
        ▼
┌───────────────────┐
│  Load Analysis    │
│  GET /api/session │
│  /{id}/analysis   │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│  Analysis Screen  │
│  (Section 10)     │
└───────────────────┘
```

**Click behavior:** Clicking anywhere on a game card opens the analysis view for that game (Section 10).

### 11.5 API Endpoint

#### GET /api/history

Returns list of user's completed games (newest first).

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 50 | Max games to return (max 100) |

**Response (200):**
```json
{
  "games": [
    {
      "session_id": "uuid",
      "started_at": "2026-01-31T10:45:00Z",
      "ended_at": "2026-01-31T11:02:00Z",
      "result": "checkmate_win",
      "engine_elo": 1200,
      "move_count": 32,
      "summary": {
        "blunders": 1,
        "mistakes": 2,
        "inaccuracies": 4,
        "average_centipawn_loss": 18
      }
    }
  ]
}
```

### 11.6 Empty State

When user has no completed games:

```
┌─────────────────────────────────────────────────────────────────┐
│  Game History                                                   │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│                     ♔                                           │
│                                                                 │
│              No games played yet                                │
│                                                                 │
│     Play your first game to start building your history!        │
│                                                                 │
│                    [Start New Game]                             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 11.7 MVP Constraints

- **No sorting:** Games always shown newest first
- **No filtering:** All games shown (filter by date/result/blunders deferred)
- **No pagination:** Simple limit-based loading (cursor pagination deferred)
- **No search:** Full-text search in PGN deferred
- **No mini-board preview:** Showing final position thumbnail deferred

---

## 12. Testing Strategy

### 12.1 Tooling

| Layer | Tooling | Scope |
| --- | --- | --- |
| Unit (Frontend) | Vitest | Pure functions, state reducers, utilities |
| Unit (Backend) | pytest | SRS math, Ghost Move Library helpers, DB query builders |
| Integration (Frontend) | React Testing Library | UI flows, board events, ghost state transitions |
| Integration (Backend) | pytest + httpx | API endpoints, DB interactions, SRS updates |
| E2E | Playwright | Full user journeys in the browser |

### 12.2 Coverage Priorities (MVP)

**SRS & Ghost Logic**
- Priority score calculation (pass streak + time since last review)
- Due selection weighting (deterministic with fixed seed)
- Ghost activation/deactivation on path deviations
- Re-hooking on transpositions (normalized FEN hashing)

**Blunder Detection**
- First auto-detected mistake only per session (within first 10 moves)
- Threshold handling (>=50cp recording, >=50cp replay failure)
- Pre-move position reference (P_before) for stored blunders
- 10-move recording cap enforcement (moves 11+ rejected)
- Manual MoveList capture supports any player move (no threshold requirement)

**Graph Traversal**
- Recursive query cycle detection
- Depth bounds and stopping conditions
- Correct next-move selection for ghost path

**Frontend Interaction**
- Pause + feedback modal on replay failure
- Resume flow after correction
- UI state when backend response switches between `ghost` and `engine` mode

### 12.3 Key Test Cases

| Area | Test Case | Expectation |
| --- | --- | --- |
| SRS | pass_streak increments on correct replay | priority decreases |
| SRS | replay failure resets pass_streak | priority increases |
| Ghost | user deviates off path | ghost deactivates |
| Ghost | user transposes back to known node | ghost reactivates |
| Blunder | blunder stored against pre-move FEN | decision point preserved |
| Analysis | first auto blunder only | later mistakes ignored unless manually added |
| Manual add | duplicate position capture | `is_new=false` and UI shows "already in library" |

### 12.4 Test Data & Determinism

- Use fixed PGNs with known engine evals for replay scenarios.
- Seed any probabilistic SRS selection to make tests deterministic.
- Pin Stockfish evaluation settings for unit/integration tests that rely on eval deltas.
