This began as the initial **SPEC** for the "Ghost Replay" Chess Application. It has since been updated to track the current codebase and is now maintained as the **living design document** — describing the ideas, architecture, and behavior as actually implemented. Some forward-looking notes remain (marked "post-MVP" / "deferred") to capture roadmap intent.

---

# SPEC.md - Ghost Replay Chess App

## Table of Contents

1. Product Description
2. User Stories & Features
3. High-Level Architecture
4. Tech Stack
5. Database Schema
6. Data & Logic Flow
7. Game Sessions & Lifecycle
8. API Specification
9. After-Game Analysis Display
10. Game History View
11. Testing Strategy
12. Rating System
13. Opening Weakness Tracking
14. Analysis Cache
15. Local Fallback
16. Practice Continuation
17. Drill Mode

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
* **Game Settings:** A gear menu in the game panel header exposes persisted settings: **sound** (mute toggle + 0–100% volume slider, applied to all clips — move, capture, buzzer, best-move bling, end-game, and blunder audio) and **rating display** (Elo/Chess.com/Lichess). Both persist across sessions via localStorage.

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

## 3. High-Level Architecture

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
* **State management:** Two Zustand stores per game:
  * `useGameStore` — session/game state (FEN, move history, player color, ratings, drill state)
  * `createAnalysisStore` — per-game analysis results (analysisMap, streaming evals, worker status)
* **Analysis worker:** `analysisWorker.ts` runs Stockfish-18-lite in a dedicated Web Worker. Managed by `GameAnalysisCoordinator`, a singleton service that survives route navigation so in-flight analysis is never lost.
* **Opponent engine:** `useStockfishEngine` drives a second Stockfish instance for ghost/engine move selection during play.
* **Analysis cache:** The coordinator dispatches worker analysis and batches `POST /api/analysis/lookup` (`lookupAnalysisCache`) in parallel. A cache hit resolves the move only when it has classification data, a `best_move_uci`, and a multi-move `best_line_uci` beginning with that best move; incomplete hits fall through to the worker result.
* **Forced-move exemption:** When the position has ≤ 2 legal moves, the move is never classified as a blunder and never auto-recorded, regardless of eval delta.
* **Sound settings:** `utils/soundSettings.ts` holds a three-layer source of truth — an in-memory snapshot (authoritative for playback, read by `applyAudioSettings` before every clip), localStorage (persistence only, seeded once at init, best-effort writes), and a `useGameStore` mirror (`soundMuted`/`soundVolume`) for the settings UI. Store setters store the canonical clamped value the snapshot setter returns, so storage failures can never desync playback from the UI.
* **Key hooks:** `useChessGameController` (move application, promotion), `useChessGameLifecycle` (session lifecycle), `useOpponentMove` (ghost/engine reply), `useMoveAnalysis` (wraps coordinator for hook consumers).
* **Routes** (`AppRoutes.tsx`):

| Path | Component | Purpose |
|------|-----------|---------|
| `/` | `App` | Landing / home |
| `/play` | `GamePage` | Live game |
| `/game` | `GameAnalysisPage` | Post-game analysis |
| `/history` | `HistoryPage` | Game history list |
| `/blunders` | `BlundersPage` | Ghost Move Library (due blunders) |
| `/openings` | `OpeningsPage` | Opening performance stats |
| `/stats` | `StatsPage` | Overall stats / rating graph |
| `/login` | `AuthForm` | Login |
| `/register` | `AuthForm` | Registration |

The landing page is a route-driven product tour of the full training loop:
play a ghost game, identify mistakes through background analysis, revisit due
blunders with spaced repetition, drill weak opening branches, review completed
games, and track progress. Its hero training board is illustrative only and does
not fetch or display account data.

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
| **Frontend** | React 19 + Vite + TypeScript | Fast development, type safety. |
| **State management** | Zustand | Lightweight, minimal boilerplate; narrow stores per concern. |
| **Chess UI** | `react-chessboard` | Robust wrapper for chessboard.js. |
| **Chess Logic** | `chess.js` | Standard library for move generation/validation. |
| **Charts** | `recharts` | Rating history graph and score visualizations. |
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
    user_id BIGINT NOT NULL,
    fen_hash VARCHAR(64) NOT NULL,         -- SHA256 of Normalized FEN
    fen_raw TEXT NOT NULL,
    active_color VARCHAR(5) NOT NULL,      -- 'white' or 'black' (side to move)
    created_at TIMESTAMP DEFAULT NOW(),

    CONSTRAINT ck_positions_active_color CHECK (active_color IN ('white', 'black')),
    UNIQUE (user_id, fen_hash)
);

CREATE INDEX idx_positions_user ON positions(user_id);
CREATE INDEX idx_positions_user_active_color ON positions(user_id, active_color);
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
    source_session_id UUID REFERENCES game_sessions(id),  -- Session that produced this blunder

    UNIQUE (user_id, position_id)          -- One ghost-library target per position per user
);

CREATE INDEX idx_blunders_user ON blunders(user_id);
CREATE INDEX idx_blunders_position_user ON blunders(position_id, user_id);
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

CREATE INDEX idx_blunder_reviews_blunder ON blunder_reviews(blunder_id, reviewed_at);
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
    username VARCHAR(50) UNIQUE,           -- nullable; auto-generated for anonymous accounts
    password_hash VARCHAR(255),             -- nullable; set during registration or claim
    is_anonymous BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Constraints:**
- `username`: 3-50 characters, alphanumeric + underscores only
- Anonymous users start with auto-generated usernames (e.g., `ghost_a3b5c7d9`)
- `is_anonymous`: TRUE for auto-created accounts, FALSE after claiming

### 5.5 `rating_history` (Rating Snapshots)

Tracks per-game rating changes for a user, enabling rating trend display and provisional-flag tracking.

```sql
CREATE TABLE rating_history (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    game_session_id UUID NOT NULL REFERENCES game_sessions(id) UNIQUE,
    rating INTEGER NOT NULL,              -- New rating after this game
    is_provisional BOOLEAN NOT NULL,       -- TRUE if user has played < PROVISIONAL_THRESHOLD games
    games_played INTEGER NOT NULL,         -- Total rated games played at the time of this record
    chesscom_rating FLOAT,                 -- Optional: imported Chess.com rating (nullable)
    chesscom_rd FLOAT,                     -- Optional: Chess.com rating deviation
    lichess_rating FLOAT,                  -- Optional: imported Lichess rating (nullable)
    lichess_rd FLOAT,                      -- Optional: Lichess rating deviation
    lichess_volatility FLOAT,              -- Optional: Lichess Glicko volatility parameter
    recorded_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_rating_history_user_timestamp ON rating_history(user_id, recorded_at);
CREATE UNIQUE INDEX uq_rating_history_game_session ON rating_history(game_session_id);
```

**Key semantics:**
- One row per completed rated game; inserted by `POST /api/game/end` when `is_rated=true`
- `is_provisional` tracks whether the rating is still considered provisional (based on games played count)
- `games_played` enables the frontend to show progress toward a stable rating
- `chesscom_*` and `lichess_*` fields are nullable; reserved for future cross-platform rating import

### 5.6 `analysis_cache` (Move Analysis Cache)

Caches move-level analysis results keyed by `(fen_before, move_uci)` to avoid re-running Stockfish for positions that have already been evaluated.

```sql
CREATE TABLE analysis_cache (
    id BIGSERIAL PRIMARY KEY,
    fen_before TEXT NOT NULL,               -- Position before the move (full 6-field FEN)
    normalized_fen_before TEXT,             -- Normalized 4-field FEN of fen_before (transposition key)
    move_uci VARCHAR(5) NOT NULL,           -- Move in UCI notation (e.g., "e2e4")
    move_san VARCHAR(10) NOT NULL,          -- Move in SAN notation (e.g., "e4")
    best_move_uci VARCHAR(5),               -- Engine's best move in UCI
    best_move_san VARCHAR(10),              -- Engine's best move in SAN
    best_line_uci TEXT,                     -- Space-joined root best-move PV
    played_eval INTEGER,                    -- Eval after the played move (centipawns, white-relative)
    played_eval_mate INTEGER,               -- White-relative mate count for the played move (NULL = not mate)
    best_eval INTEGER,                      -- Eval of best move (centipawns, white-relative)
    best_eval_mate INTEGER,                 -- White-relative mate count for the best move (NULL = not mate)
    eval_delta INTEGER,                     -- best_eval - played_eval (positive = lost advantage)
    classification VARCHAR(20),             -- Move classification
    source VARCHAR(20) NOT NULL DEFAULT 'game',  -- Provenance only: 'game' | 'precomputed' | 'jeffml-scores'
    -- Provenance / quality metadata (nullable; NULL = legacy/untrusted row)
    analysis_profile_id VARCHAR(64),        -- id into the in-code profile registry
    engine_name VARCHAR(64),
    engine_version VARCHAR(64),
    engine_build VARCHAR(128),              -- binary SHA-256 (not UCI id author)
    network_id VARCHAR(128),                -- NNUE EvalFile name + content hash
    search_limit_type VARCHAR(16),          -- 'depth' | 'nodes' | 'movetime'
    search_limit_value INTEGER,
    threads INTEGER,
    hash_mb INTEGER,
    multipv INTEGER,
    eval_file_id TEXT,                      -- Full NNUE big-net identity "<filename>:<hash>"
    eval_file_small_id TEXT,                -- Full NNUE small-net identity "<filename>:<hash>"
    analyzer_protocol_version VARCHAR(64),  -- Version of the analyzer output contract
    profile_manifest_digest VARCHAR(64),    -- Digest of the producing profile's identity bits
    evidence_contract_id VARCHAR(64),       -- id into the evidence-contract registry
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),

    UNIQUE (fen_before, move_uci)
);

CREATE INDEX idx_analysis_cache_fen ON analysis_cache(fen_before);
-- Composite index for the opening-tree normalized transposition fallback.
CREATE INDEX idx_analysis_cache_norm_move ON analysis_cache(normalized_fen_before, move_uci);
```

**Key semantics:**
- The `(fen_before, move_uci)` unique pair enables O(1) lookup: "has this exact position+move been analyzed before?"
- `normalized_fen_before` is **derived** from `fen_before` (`app/fen.py::normalize_fen`) and set only on INSERT in the shared writer; it is the indexed key for the opening-tree transposition fallback (see below). NULL on rows whose FEN failed to parse during backfill.
- `played_eval` / `best_eval` (and their `_mate` companions) are **white-relative**: positive favors White regardless of side to move.
- `source` records provenance only (`game`, `precomputed`, `jeffml-scores`); it is **not** the quality comparator.
- The frontend races its own local Stockfish worker against this cache to avoid redundant computation

**Opening-tree eval lookup (`backend/app/tree_eval.py`).** The horizontal opening
tree reads a per-node eval from this cache and never runs its own engine. A move
node has key `(parent_fen, move_uci)`, but the tree replays the UCI line from the
initial board so `parent_fen` is a full 6-field FEN whose clocks may differ from a
stored `fen_before` (transpositions). `lookup_move_evals` resolves each node in at
most two indexed, batched queries (never a scan, never one query per child):
1. **Exact `(fen_before, move_uci)` wins** when its row has a usable played eval.
2. Otherwise an **indexed normalized fallback** over `(normalized_fen_before, move_uci)`
   selects deterministically: prefer rows with mate data, then `source=precomputed` >
   `game` > other, then lowest `id`.
The returned value prefers `played_eval_mate` over `played_eval` (mate over cp). The
column-0 root uses `lookup_root_eval`, returning the position's `best_eval`/`best_eval_mate`
(the eval under the engine's best move — a property of the position); any row at the
starting FEN with a usable best eval qualifies, with the complete best-move row
(`move_uci == best_move_uci`) merely preferred in ranking. Values stay white-relative;
`eval_for_perspective` negates cp and mate for Black. Missing eval is a normal null
state (rendered as an em dash), never an error and never a trigger for browser engine
analysis.

**Quality-aware writes (rows are NOT immutable).** Every writer — session/game
uploads, opening precompute, and JeffML score ingestion — routes through one
shared helper (`backend/app/analysis_cache_repo.py::write_analysis_cache_rows`)
that applies a single deterministic replacement policy
(`backend/app/analysis_cache_policy.py::decide_analysis_cache_replacement`)
returning INSERT / REPLACE / MERGE / KEEP plus a reason code. The policy reasons
over two registries, never over raw numeric depth:
- **Profile registry** (`backend/app/analysis_profiles.py`) — immutable, versioned
  engine/search profiles. A row is *trusted* only when its stored identity
  metadata matches its claimed profile (`identity_verified`). Cross-family
  replacement requires an explicit `dominates` edge **and** an authoritative
  profile.
- **Evidence-contract registry** (`backend/app/evidence_contracts.py`) — versioned
  data-shape contracts with per-contract semantic validation
  (`resolver-complete-v1` mirrors the worker's `canResolveCachedAnalysis`;
  `minimal-played-eval-v1` / `minimal-best-eval-v1` cover eval-only rows).
  Replacement/merge requires contract succession plus a populated-field superset
  so no datum is ever silently dropped.

Net guarantees: a browser `game` upload is non-authoritative — it may fill keys
that have no evidence but can never downgrade a canonical or legacy row; sparse
JeffML rows can never replace richer ones; only a re-run authoritative canonical
profile reclaims legacy rows. Writes are serialized safely: PostgreSQL uses
insert-first + `SELECT … FOR UPDATE`; file-backed SQLite uses `BEGIN IMMEDIATE` +
`busy_timeout` with bounded retry; other dialects are rejected. The `/api/analysis/lookup`
response exposes `source`, `analysis_profile_id`, `engine_version`, `engine_build`,
`evidence_contract_id`, and an `authoritative` trust flag derived from the same
validation the writer uses.

### 5.7 Opening Score Tables

The opening score system computes and stores per-user 0-100 mastery scores for opening lines (higher = better). Three tables work together: batches group computation runs, cursors track the latest batch per user/color, and scores hold the actual per-opening metrics.

#### 5.7.1 `opening_score_batches` (Computation Runs)

Each row represents a single batch computation of opening scores for a user/color pair.

```sql
CREATE TABLE opening_score_batches (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    player_color VARCHAR(5) NOT NULL,      -- 'white' or 'black'
    generation INTEGER NOT NULL,            -- Monotonic counter per (user_id, player_color)
    registry_fingerprint TEXT,              -- Hash of the opening registry used, to detect staleness
    computed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    UNIQUE (user_id, player_color, generation)
);

CREATE INDEX idx_opening_score_batches_user_color ON opening_score_batches(user_id, player_color, generation);
```

#### 5.7.2 `opening_score_cursors` (Latest Batch Tracking)

Tracks the latest generation per user/color so the system can quickly find the current scores without scanning the batches table.

```sql
CREATE TABLE opening_score_cursors (
    user_id BIGINT NOT NULL,
    player_color VARCHAR(5) NOT NULL,      -- 'white' or 'black'
    latest_generation INTEGER NOT NULL DEFAULT 0,

    PRIMARY KEY (user_id, player_color),
    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black'))
);
```

#### 5.7.3 `user_opening_scores` (Per-Opening Metrics)

Stores the actual mastery metrics for each opening line, computed within a batch.

```sql
CREATE TABLE user_opening_scores (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES opening_score_batches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    player_color VARCHAR(5) NOT NULL,      -- 'white' or 'black'
    opening_key TEXT NOT NULL,             -- ECO-style key identifying the opening line
    opening_name TEXT NOT NULL,            -- Human-readable opening name
    opening_family TEXT NOT NULL,          -- Broader family grouping (e.g., "Sicilian Defense")
    opening_score FLOAT NOT NULL,          -- Mastery score (0-100, higher = better) computed directly per root
    confidence FLOAT NOT NULL,             -- Statistical confidence in the score
    coverage FLOAT NOT NULL,               -- Fraction of moves in this line that have been played
    weighted_depth FLOAT NOT NULL,         -- Average depth of user's games in this opening
    sample_size INTEGER NOT NULL,          -- Number of data points used for this score
    last_practiced_at TIMESTAMP,           -- When the user last played this opening
    strongest_branch_name TEXT,            -- Name of the user's best sub-line
    strongest_branch_key TEXT,             -- Key of the user's best sub-line
    strongest_branch_score FLOAT,          -- Score of the user's best sub-line
    weakest_branch_name TEXT,              -- Name of the user's worst sub-line
    weakest_branch_key TEXT,               -- Key of the user's worst sub-line
    weakest_branch_score FLOAT,            -- Score of the user's worst sub-line
    underexposed_branch_name TEXT,         -- Name of a sub-line the user hasn't practiced enough
    underexposed_branch_key TEXT,          -- Key of an underexposed sub-line
    underexposed_branch_value FLOAT,       -- Coverage gap value for the underexposed branch
    computed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    UNIQUE (batch_id, opening_key)
);

CREATE INDEX idx_user_opening_scores_batch ON user_opening_scores(batch_id);
CREATE INDEX idx_user_opening_scores_user_color ON user_opening_scores(user_id, player_color);
```

**Key semantics:**
- `opening_score` is a **0-100 mastery** score (higher = better), computed **directly per root** from the shared scoring DAG. There is no confidence-weighted descendant rollup: a card shows its own root row, and the top-level hero shows a synthetic initial-position ("whole repertoire") row persisted under the normalized initial FEN with `opening_family = "__repertoire__"`.
- `confidence` and `sample_size` let the frontend de-emphasize scores based on sparse data
- Branch fields (strongest/weakest/underexposed) are persisted from the same shared calculation and read directly by the drill-down (no per-request recompute)
- Batches are replaced atomically: a new batch is computed, then the cursor is updated to point to it; old batches are pruned
- `registry_fingerprint` includes the score-model, phase-divider, and quality-curve versions (`SCORE_MODEL_VERSION`, `DIVIDER_VERSION`, `QUALITY_VERSION`, `TAU_WC`, `TAU_CP`), so any model/divider/curve change invalidates all prior snapshots on the next read.
- `inputs_fingerprint` is a **cheap raw-input freshness digest** (`opening_score_raw_inputs_fingerprint`): it hashes a canonical, order-independent projection of exactly the raw DB rows the evidence overlay consumes (session_moves + the bounded analysis_cache fallback subset, ghost-target blunders/positions, and blunder_reviews) **without any python-chess board replay or overlay build**, folded together with `registry_fingerprint` and an explicit `OPENING_EVIDENCE_INPUTS_VERSION`. The latter is bumped on any evidence-derivation semantic change a raw-row hash is blind to (e.g. `PASS_THRESHOLD`, quality-source precedence, FEN normalization, phase-filter application, or the digest's own projection/filters). The overlay is a pure deterministic function of these inputs, so a matching digest provably implies identical scores.
- `recompute_opening_scores_if_needed()` is the single recompute-decision function, run on the scheduler's serialized worker. It computes the raw-input digest first (cheap SQL) and serves the cached batch on the fast path **without building the expensive overlay** when nothing changed; the overlay (per-session board reconstruction + Lichess phase divider) is rebuilt only on a cache miss, registry drift, stale branch keys, a digest change, or decay-staleness. Reads are stale-while-revalidate: a **warm** reader (batch present) calls `request_recompute()` to schedule a coalesced background convergence and serves the cached batch immediately — never blocking; only a **cold** reader (no batch yet) blocks on `refresh_now()` for the one-time initial compute.

#### 5.7.4 `opening_position_scores` (Direct Tree Position Metrics)

Sibling read model to `user_opening_scores`, persisted under the **same** `opening_score_batches` generation but keyed by `(batch_id, normalized_fen)` instead of a named-root key. It supplies direct per-position metrics for the horizontal opening tree (`/openings`), so the tree read path serves intermediate move nodes — not only the named boundary roots — **without running a full root calculation per visible card**.

```sql
CREATE TABLE opening_position_scores (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES opening_score_batches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    player_color VARCHAR(5) NOT NULL,      -- 'white' or 'black'
    normalized_fen TEXT NOT NULL,          -- Normalized 4-field FEN — the position identity
    in_book BOOLEAN NOT NULL,              -- True when the FEN is a reference OpeningGraph position
    has_evidence BOOLEAN NOT NULL,         -- True when mastery evidence exists at/below the FEN
    opening_score FLOAT,                   -- 0-100 mastery (NULL when has_evidence is false: no-data)
    confidence FLOAT,                      -- NULL when no-data
    coverage FLOAT,                        -- NULL when no-data
    weighted_depth FLOAT,                  -- NULL when no-data
    sample_size INTEGER NOT NULL DEFAULT 0,-- Move-observation count over the reachable subtree
    game_count INTEGER NOT NULL DEFAULT 0, -- Distinct games over the reachable subtree
    last_practiced_at TIMESTAMP,           -- Max live/review touch over the reachable subtree (NULL no-data)
    computed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    UNIQUE (batch_id, normalized_fen)
);

CREATE INDEX idx_opening_position_scores_batch_fen ON opening_position_scores(batch_id, normalized_fen);
CREATE INDEX idx_opening_position_scores_user_color ON opening_position_scores(user_id, player_color);
```

**Read-model contract:**
- **One shared traversal.** All rows for a batch are produced by a single `_SharedCalculator` pass (`opening_rootcalc.compute_all_scores` → `compute_position_scores`). The named-root rows, the synthetic repertoire row, and every direct position row reuse the same memoized `_metrics`, SCC cut, weights, and reachable caches — so named and direct metrics can never disagree, and the cost is at most two metric records (natural + perfect) per reachable FEN, never one root walk per card. The metric formulas are identical to the named-root path (`_base_root_score`).
- **Sparse persistence (no full structural materialization).** Only rows the database actually needs are written: in-book positions with mastery evidence at/below the FEN, plus connected observed off-book positions. **Static in-book positions with no evidence below are intentionally not materialized** — they are already represented by `OpeningGraph`, so the read path returns no-data for an in-graph FEN absent from the batch. (Full per-book-FEN materialization is deferred pending a separate row-volume/write/storage benchmark; ~15.5k book nodes per batch per color is not written by default.)
- **No-data gating.** A row exists for a connected observed off-book node even without evidence so the API can tell a navigable observed off-book node from an arbitrary unknown FEN, but its four metric columns are NULL and its counts are zero. Visibility is gated purely by evidence at/below the FEN regardless of side to move: a no-evidence user-turn row never surfaces the alpha/beta prior, and a no-evidence opponent-turn leaf never surfaces `_calc`'s perfect-looking `(1.0, 1.0, 1.0, 0.0)` result.
- **Observed off-book domain.** Off-book positions enter the scorer only by reachable observed `overlay.edges` (book-boundary exit → observed continuation), matching the tree's navigable data; disconnected off-book blunders/reviews are not seeded. `opening_evidence.observed_off_book_fens()` is the explicit contract for the candidate off-book endpoints.
- **Transposition-safe identity.** The key is the normalized 4-field FEN, so transposed routes reached through different UCI lines share one row. `opening_cache.lookup_position_scores()` **normalizes every incoming (possibly raw, clock-bearing) FEN before lookup**, so halfmove/fullmove differences and transpositions hit the same row instead of silently missing.
- **Distinct game count.** `game_count` is the number of distinct sessions reaching the scored subtree (union of per-node `session_ids`), never `sample_size` (move-observations) relabeled.
- **Generation retention.** `batch_id` cascades on delete from `opening_score_batches` exactly like `user_opening_scores`, so `prune_old_opening_score_batches` removes direct rows through the same generation-retention path (keep=2) — no unbounded leak across recomputes. Rows are written in the same transaction as the named rows of the batch.

### 5.8 Authentication

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
4. **Eligibility:** A blunder is eligible (due) when `srs_priority > 1.0`:
   ```
   expected_interval = BASE_INTERVAL * (BACKOFF_FACTOR ^ pass_streak)   -- hours
   srs_priority = hours_since_review / expected_interval
   ```
5. **Scoring:** For each eligible blunder, compute a composite score in Python after the SQL fetch:
   ```
   urgency       = 1 + log2(1 + overdue)
                   where overdue = hours_since_review / expected_interval

   severity      = log1p(eval_loss_cp / 50)          -- logarithmic; 200cp ≈ 1.61, 50cp ≈ 0.69

   distance_weight = exp(-0.35 * depth)               -- exponential decay; depth=1 → 0.70, depth=5 → 0.17

   score = urgency × severity × distance_weight
   ```
6. **Selection:** Weighted random from top-5 first-move groups (see §6.1.2).
7. **Output:** The immediate next move (SAN) on the chosen path.

**Color Scope Rule:** Only consider blunders where the **position side-to-move** equals the session's `player_color`. This prevents mixing blunders made as White with those made as Black. Use `positions.active_color` for efficient filtering.

**Reference Implementation (SQLite-compatible):**

```sql
WITH RECURSIVE reachable(position_id, depth, path, first_move) AS (
    -- Base case: current position (depth 0, no first_move yet)
    SELECT
        CAST(:start_position_id AS BIGINT),
        0,
        ',' || :start_position_id || ',',
        CAST(NULL AS TEXT)

    UNION ALL

    -- Recursive case: follow moves up to steering radius
    SELECT
        m.to_position_id,
        r.depth + 1,
        r.path || m.to_position_id || ',',
        COALESCE(r.first_move, m.move_san)
    FROM reachable r
    JOIN moves m ON m.from_position_id = r.position_id
    WHERE r.depth < :steering_radius                                    -- Depth limit: 5-move steering radius
      AND r.path NOT LIKE '%,' || CAST(m.to_position_id AS TEXT) || ',%'  -- Cycle detection
)
SELECT
    r.first_move,
    b.id AS blunder_id,
    r.depth,
    b.eval_loss_cp,
    b.pass_streak,
    b.last_reviewed_at,
    b.created_at,
    b.opening_family
FROM reachable r
JOIN positions p ON p.id = r.position_id
JOIN blunders b ON b.position_id = r.position_id
WHERE b.user_id = :user_id
  AND p.active_color = :player_color
  AND r.first_move IS NOT NULL;
```

Scoring and selection are computed in Python after this query returns (see `GhostMoveCandidate.score()` in `backend/app/api/game.py`).

**Key Safeguards:**
- `depth < 5`: Steering radius—only considers blunders reachable within 5 moves, where the Ghost can reliably steer
- `path` string: Accumulates visited position IDs as a comma-delimited string along each traversal path
- `path NOT LIKE '%,<id>,%'`: SQLite-compatible cycle guard; prevents following edges that revisit a position already in the current path

### 6.1.1 Re-Hooking Logic (Transposition Detection)

When the user deviates from the Ghost path, backend engine mode takes over. However, the user may later transpose into a known position that has a due blunder downstream. The Ghost reactivates automatically on every move.

**When to Check:** Every user move. The `POST /api/game/next-opponent-move` endpoint is called after each move regardless of current mode.

**Re-Hook Trigger:** The Ghost reactivates when:
1. The current position exists in the user's Ghost Move Library (matched by normalized FEN hash)
2. At least one blunder target with `srs_priority > 1.0` is reachable within 5 moves downstream (via the `blunders` table)

**Backend Logic:**

```
POST /api/game/next-opponent-move
After a user move, backend returns exactly one opponent move. It first tries Ghost steering; if no due path exists, it falls back to the remote Maia3 API.

1. Validate session ownership and that it's the opponent's turn for `fen`.
2. Compute normalized FEN hash.
3. Look up position by `fen_hash` in `positions` table (for this user).
4. If found:
   → Run downstream blunder query (recursive CTE, depth ≤ 5).
   → Filter for blunders with srs_priority > 1.0.
5. If due blunder(s) reachable:
   → Score candidates; select via weighted random from top-5 first-move groups.
   → Return:
     { "mode": "ghost", "move": { "uci": "...", "san": "..." },
       "target_blunder_id": <id>, "decision_source": "ghost_path" }
6. If no due blunders reachable:
   → Call remote Maia3 API for engine move.
   → Return:
     { "mode": "engine", "move": { "uci": "...", "san": "..." },
       "target_blunder_id": null, "decision_source": "backend_engine" }
```

**Performance Target:** Ghost-path lookup < 100ms for typical Ghost Move Libraries (< 10,000 positions). The 5-move depth cap keeps the search space small; full fallback (including Maia3 API call) should target sub-second p95 in MVP. The Maia3 remote API adds ~200–500ms network latency per engine fallback call.

**Caching Consideration (Post-MVP):** Position lookups are hot-path. Consider caching:
- FEN hash → position existence (simple boolean)
- Position ID → downstream blunder count (invalidate on new blunder insertion)

### 6.1.2 First-Move Group Selection

After scoring all eligible candidates, the Ghost selects its move via a two-stage weighted random process to add natural variety and avoid mechanical repetition.

**Stage 1 — Group by first move:**
Candidates are grouped by `first_move` (the immediate opponent move the Ghost would play). Each group gets an aggregate score:
```
aggregate_score = best_candidate_score + 0.15 × sum(remaining_candidate_scores)
```

**Stage 2 — Top-K weighted random:**
1. Sort groups by `penalized_score` (aggregate × repeat penalty, see below).
2. Keep top `TOP_K = 5` groups.
3. Weight each group by `penalized_score ^ 0.5` (square-root flattening reduces winner-take-all dominance).
4. Sample one group using `random.choices` seeded by a stable, deterministic seed.
5. Within the chosen group, sample one candidate using the same weight formula.

**Stable seeding:**
```
seed = SHA-256(user_id | fen_hash | session_id)[:8]   -- big-endian int
```
The seed is stable across Python restarts and equivalent FENs (normalized by `fen_hash`), producing consistent results for the same position within a session while varying across sessions.

**Repeat penalties:**
To avoid showing the same ghost move repeatedly from the same position, the Ghost looks back at the 3 most recent ghost moves played from this FEN and penalizes their first moves:
```
factors = (0.35, 0.60, 0.80)   -- most-recent to least-recent
```
A move seen twice incurs both factors multiplicatively.

### 6.2 Ghost Move Library Capture Logic

Ghost Move Library targets enter the system through two capture paths.

#### 6.2.1 Automatic Capture (analysis-triggered blunder)

1. User plays move M from position P_before, resulting in position P_after.
2. **Forced-move exemption:** If P_before has ≤ 2 legal moves, the move is never classified as a blunder and auto-capture is skipped entirely, regardless of eval delta.
3. **Worker B** (Frontend) calculates (using two independent post-move searches to avoid depth-mismatch inflation):
   * E_best (Eval of post-best-move position, from opponent's perspective)
   * E_user (Eval of post-played-move position, from opponent's perspective)
   * delta = E_best − E_user (player-perspective centipawns)
4. If delta ≥ 50cp (recording threshold) **and** the move is within the first 10 full moves of the game:
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

Each blunder record has an SRS priority that tracks how overdue it is. A blunder is **due** when `srs_priority > 1.0`:

```
expected_interval = BASE_INTERVAL * (BACKOFF_FACTOR ^ pass_streak)   -- hours, capped at MAX_INTERVAL
srs_priority      = hours_since_review / expected_interval
```

`last_reviewed_at` falls back to `created_at` when the blunder has never been reviewed.

When selecting which blunder to steer toward, the Ghost uses a composite score that combines urgency (saturating), severity (logarithmic), and distance (exponential decay):

```
overdue         = hours_since_review / expected_interval
urgency         = 1 + log2(1 + overdue)            -- saturating; grows slowly once very overdue

severity        = log1p(eval_loss_cp / 50)          -- 50cp → 0.69, 100cp → 1.10, 200cp → 1.61

distance_weight = exp(-0.35 × depth)                -- depth=1 → 0.70, depth=3 → 0.35, depth=5 → 0.17

score = urgency × severity × distance_weight
```

**Constants:**
| Constant | Value | Purpose |
|----------|-------|---------|
| `BASE_INTERVAL` | 4 hours | Minimum review interval (first attempt) |
| `BACKOFF_FACTOR` | 2.0 | Exponential interval growth per pass |
| `MAX_INTERVAL` | 4320 hours (180 days) | Interval cap |
| `STEERING_RADIUS` | 5 moves | Max depth for ghost path traversal |
| `SEVERITY_NORMALIZER_CP` | 50 cp | Denominator in log1p severity formula |
| `DISTANCE_DECAY_RATE` | 0.35 | Exponential decay rate for distance weight |
| `TOP_K` | 5 | Number of first-move groups considered for weighted random selection |
| `RECORDING_MOVE_CAP` | 10 | Only record blunders in the first 10 moves |

**SRS Priority Examples (before severity/distance weighting):**
| pass_streak | expected_interval | After 4hr | After 24hr | After 7 days |
|-------------|-------------------|-----------|------------|--------------|
| 0 (new)     | 4 hr              | 1.0       | 6.0        | 42.0         |
| 1           | 8 hr              | 0.5       | 3.0        | 21.0         |
| 3           | 32 hr             | 0.125     | 0.75       | 5.25         |
| 5           | 128 hr            | 0.03      | 0.19       | 1.31         |
| 10          | 4096 hr (~171 days, capped at 4320) | 0.001 | 0.006 | 0.04 |

**Urgency vs. overdue:**
| overdue (hours_since / expected) | urgency = 1 + log2(1 + overdue) |
|----------------------------------|----------------------------------|
| 0.5 (half-due)                   | 1.58                             |
| 1.0 (exactly due)                | 2.00                             |
| 3.0                              | 3.00                             |
| 7.0                              | 4.00                             |
| 15.0                             | 5.00                             |

**Severity examples (log1p scale):**
| eval_loss_cp | severity = log1p(cp/50) |
|--------------|-------------------------|
| 50cp         | 0.69                    |
| 100cp        | 1.10                    |
| 200cp        | 1.61                    |
| 400cp        | 2.20                    |

Higher composite score = more likely to be selected when Ghost chooses a path.

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
| Depth | 17 | Sufficient tactical accuracy; diminishing returns beyond |
| Time limit | None | Search runs until depth 17 is reached |
| MultiPV | 1 | Only the best move needed for delta calculation |
| Hash | 128 MB | Transposition table |
| Threads | 1 | Web Worker constraint; WASM single-threaded |

**Stopping condition:** Search terminates when depth 17 is reached. The evaluation from the final `info` line before `bestmove` is used.

**Dual-search protocol:** Each move triggers two independent searches (played-move position and best-move position). Using post-move positions for both avoids depth-mismatch inflation that occurs when comparing pre-move minimax against post-move searches.

**Implementation (JavaScript):**
```javascript
// Send to Stockfish worker (on uciok)
worker.postMessage('setoption name Hash value 128');
worker.postMessage('setoption name MultiPV value 1');

// Per-move analysis: two searches
worker.postMessage(`position fen ${fen} moves ${playedMove}`);
worker.postMessage('go depth 17');
// ... then after bestmove received:
worker.postMessage(`position fen ${fen} moves ${bestMove}`);
worker.postMessage('go depth 17');
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

**Delta calculation:** Always computed as `best_move_eval - played_move_eval` using player-perspective values. A positive delta means the played move was worse.

**Move classification:** Delta is used only for the recording threshold (≥ 50cp → recordable). The quality label (blunder/mistake/inaccuracy/good/excellent/best) is produced by `classifyMoveAdvanced`, which uses a Lichess-style logistic win-chance model instead of raw cp (see §9.2.2).

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

**Database storage:** When eval is a mate score, the mate count travels
**alongside** the converted cp sibling rather than replacing it:
- `eval_cp` = the mate-converted centipawn value (`mateToCp(N)`, e.g. ±~10000),
  so cp-only consumers and delta math keep working unchanged
- `eval_mate` = N (positive = winning, negative = losing)
- The analysis cache mirrors this: `played_eval` (white-relative cp) plus
  `played_eval_mate` (white-relative mate count, NULL when not a mate)
- Delta calculations use the converted cp value at comparison time

#### 6.4.4 Evaluation Stability

Engine evaluations fluctuate during iterative deepening. The protocol uses **depth-gated snapshots** rather than convergence detection.

**Rule:** Use the evaluation reported when depth 17 is reached. Do not wait for successive identical evaluations.

**Rationale:**
- Convergence detection adds latency and implementation complexity
- Depth 17 provides sufficient stability for tactically critical positions
- Positions with high eval variance at depth 17 are typically near-equal (within ±50cp of 0.00)

**Known limitation:** In rare positions (e.g., deep sacrificial lines, fortress detection), depth 17 may not capture the full picture. This is acceptable for MVP; users experiencing systematic false blunders can be addressed post-MVP with configurable depth.

#### 6.4.5 Edge Cases

| Scenario | Handling |
|----------|----------|
| **Tablebase position** | Stockfish handles internally; reports mate distance or draw |
| **Book opening moves** | Evaluate normally; no special-casing for theory |
| **Threefold repetition claim available** | Engine may report 0cp; user's non-draw move compared to 0 |
| **50-move rule proximity** | Engine accounts internally; may report draw |
| **Worker crash/timeout** | Skip evaluation for that move; log error; do not flag as blunder |
| **Eval exactly at threshold** | ≥50cp = recorded and fails review (inclusive boundary) |
| **Forced move (≤ 2 legal moves)** | Move is never classified as blunder and never auto-recorded, regardless of delta |

#### 6.4.6 Frontend Implementation Notes

**Coordinator:** Analysis is managed by `GameAnalysisCoordinator` (`src/services/GameAnalysisCoordinator.ts`), a singleton that outlives individual route renders. This ensures in-flight analysis is not lost when the user navigates between `/play` and `/game`.

**Analysis cache race:** On each move, the coordinator simultaneously dispatches to the analysis worker and queries `GET /api/analysis-cache`. If the cache responds first with a complete entry (`classification` or `eval_delta` present), it resolves immediately without waiting for the worker. Cache entries lacking both fields are treated as misses so the worker can produce a full result.

**Classification model:** The worker uses `classifyMoveAdvanced` (Lichess logistic win-chance model, see §6.4.2) as the primary classifier. `classifyMove` (cp-based thresholds) is deprecated and used only as a fallback when a cache entry has `eval_delta` but no explicit `classification`.

**Batching:** Worker B evaluates moves asynchronously. During fast play, evaluations queue; moves are processed in order. Each move index tracks its latest request ID so that stale results from retried analysis are discarded.

**Memory:** Each evaluation result is stored in `createAnalysisStore.analysisMap` during the game and uploaded through the coordinator's incremental session-move uploader, with a final best-effort flush on game end (see Section 7.4).

**Error recovery:** If Worker B fails to initialize (WASM load failure), the game continues without analysis. The `session_moves` table will be empty for that game, and no automatic blunders can be recorded (manual MoveList capture is still available).

---

## 7. Game Sessions & Lifecycle

A **game session** represents a single game from start to termination. Sessions enforce the "first auto-blunder only" rule for analysis-triggered capture, track game outcomes, and store game history with analysis.

### 7.1 Session Definition

A session begins when the user clicks "New Game" and ends when the game terminates (checkmate, resignation, draw, or abandonment). Each session has a unique identifier.

### 7.2 Game States

```
 ┌─────────┐     New Game     ┌─────────┐     Terminal Event     ┌─────────┐
 │  IDLE   │ ───────────────► │  ACTIVE │ ─────────────────────► │  ENDED  │
 └─────────┘                  └─────────┘                        └─────────┘
```

**State Transitions:**
| From | To | Trigger |
|------|------|---------|
| IDLE | ACTIVE | User clicks "New Game" |
| ACTIVE | ENDED | Checkmate, stalemate, resignation, draw, or explicit abandon |

There is no background-job abandonment or `abandoned` status. If the user closes the browser without calling `/api/game/end`, the session remains `active` indefinitely.

### 7.3 Session Schema

```sql
CREATE TABLE game_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(id),
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at TIMESTAMP,
    status VARCHAR(20) NOT NULL DEFAULT 'active',  -- 'active', 'ended'
    result VARCHAR(20),           -- 'checkmate_win', 'checkmate_loss', 'resign', 'draw', 'abandon', 'drill_abandon'
    engine_elo INTEGER NOT NULL,  -- Bot difficulty selected for this game
    player_color VARCHAR(5) NOT NULL DEFAULT 'white', -- 'white' or 'black' (user side for this session)
    blunder_recorded BOOLEAN NOT NULL DEFAULT FALSE,  -- First auto-blunder rule flag (manual captures bypass)
    is_rated BOOLEAN NOT NULL DEFAULT TRUE,  -- Whether this game affects the user's rating
    pgn TEXT,                     -- Full game in PGN format

    -- Drill mode columns (NULL for normal sessions)
    session_mode VARCHAR(10) NOT NULL DEFAULT 'normal',  -- 'normal' or 'drill'
    drill_state VARCHAR(12),      -- 'active', 'root_reached', 'failed', 'abandoned', 'converted'
    drill_opening_key TEXT,       -- Opening key for drill target (e.g. "e4_e5_Nf3")
    drill_strictness VARCHAR(12), -- 'lenient', 'standard', 'strict'
    drill_strictness_cp INTEGER,  -- Custom centipawn threshold override (0–50)
    drill_terminal_reason VARCHAR(20),  -- 'off_route', 'accuracy', 'natural_end'
    normal_started_at TIMESTAMP,  -- When conversion to rated play occurred
    converted_at TIMESTAMP,       -- Timestamp of the drill→normal conversion
    rated_start_ply INTEGER,      -- Ply number where rated play began (post-conversion)

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    CONSTRAINT valid_session_mode CHECK (session_mode IN ('normal', 'drill')),
    CONSTRAINT valid_drill_state CHECK (drill_state IS NULL OR drill_state IN ('active', 'root_reached', 'failed', 'abandoned', 'converted'))
);

CREATE INDEX idx_game_sessions_user ON game_sessions(user_id);
CREATE INDEX idx_game_sessions_status ON game_sessions(status);
CREATE INDEX idx_game_sessions_user_started ON game_sessions(user_id, started_at);
CREATE INDEX idx_game_sessions_drill_state ON game_sessions(drill_state);
```

**`is_rated` flag:** Passed by the client in `POST /api/game/end`. When `true` and the result is `checkmate_win`, `checkmate_loss`, `resign`, or `draw`, the server computes a rating change and appends a `rating_history` row. Results of `abandon` never affect rating regardless of `is_rated`. The flag defaults to `true`; clients set it to `false` for practice games.

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
    fen_before TEXT,                   -- Position before this move (for analysis cache lookups)
    best_move_uci VARCHAR(5),          -- Engine's recommended move in UCI notation
    decision_source VARCHAR(20),      -- 'ghost_path', 'backend_engine', 'local_fallback', or NULL
    target_blunder_id BIGINT REFERENCES blunders(id),  -- Blunder being steered toward (ghost moves only)

    CONSTRAINT valid_color CHECK (color IN ('white', 'black')),
    CONSTRAINT valid_decision_source CHECK (
        decision_source IS NULL OR decision_source IN ('ghost_path', 'backend_engine', 'local_fallback')
    ),
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
3. `GameAnalysisCoordinator` incrementally uploads resolved dirty moves to `POST /api/session/{id}/moves` and retries transient failures with a frozen payload
4. On game end or conversion-sensitive transitions, the coordinator performs a best-effort flush of any remaining resolved moves
5. Server bulk-upserts `session_moves` records

**Upload cancellation:** Unconverted drill sessions are best-effort evidence until they are converted. When a drill is abandoned, naturally ended, reset, or replaced by another drill/normal game without conversion, the client disables and aborts that drill's pending session-move uploads so stale rounds do not occupy live gameplay request capacity. If a late upload for an already ended, unconverted drill still reaches the backend, the backend keeps the raw `session_moves` upsert idempotent but skips expensive evidence side effects (ghost graph, blunder opportunity, analysis-cache, and opening-score recompute).

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

**Additional constraints on auto-recording (`POST /api/blunder` only):**
- **First-move exemption:** A blunder on the very first move (only 1 move in the PGN) is silently skipped and not recorded. Ghost mode can never steer back to the starting position, so recording the first move is meaningless.
- **10-move cap:** Auto-recording is restricted to blunders occurring within the first 10 full moves. Blunders after move 10 return HTTP 400. Manual capture (`/api/blunder/manual`) has no move-count restriction.

### 7.6 Game Termination

**Resignation:**
- User clicks "Resign" button
- Frontend sends `POST /api/game/end` with `{ "session_id": "{id}", "result": "resign" }`
- Session marked as ended

**Checkmate/Stalemate:**
- `chess.js` detects game over state
- Frontend sends `POST /api/game/end` with `{ "session_id": "{id}", "result": "<outcome>" }`
- Session marked as ended

**Abandonment:**
- User explicitly abandons (e.g., clicks "New Game" mid-game)
- Frontend sends `POST /api/game/end` with `{ "session_id": "{id}", "result": "abandon" }`
- Session marked as ended (`status = 'ended'`, `result = 'abandon'`)
- `abandon` result does not affect rating (excluded from `RESULT_SCORES`)
- If the user closes the browser without calling this endpoint, the session remains `active` indefinitely (no background cleanup job)

### 7.7 Session Persistence

**What IS persisted:**
- Session metadata (start/end times, result, engine Elo)
- Full PGN of the game
- Per-move engine analysis (eval, best move, classification)
- Ghost Move Library targets: auto blunders and manually selected MoveList decisions (anchored to the `positions` + `moves` graph)

**Browser Refresh Behavior:**
- Refreshing mid-game loses the current game state
- User must start a new game
- Previous session remains `active` in the DB (no cleanup job runs)
- *Future enhancement: LocalStorage-based state recovery*

---

## 8. API Specification

All endpoints use JSON request/response bodies. The API is RESTful.

### 8.1 Base URL

```
/api
```

### 8.2 Authentication

All endpoints except `/api/auth/*` require authentication via Bearer token.

**Header:** `Authorization: Bearer <jwt_token>`

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/auth/register` | Create anonymous account (auto-generated credentials) |
| POST | `/api/auth/login` | Authenticate and get token (optional, cross-device) |
| POST | `/api/auth/claim` | Upgrade anonymous account to claimed account |

#### POST /api/auth/register

Creates an anonymous user account with auto-generated credentials. Called automatically by frontend on first visit.

**Request:**
```json
{
  "username": "string (3-50 chars, auto-generated by frontend)",
  "password": "string (auto-generated by frontend)"
}
```

**Response (201):**
```json
{
  "user_id": "integer",
  "username": "string",
  "token": "string (JWT)"
}
```

**Implementation Notes:**
- Creates user with `is_anonymous = TRUE` (not returned in response)
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
  "new_username": "string (3-50 chars, alphanumeric + underscore)",
  "new_password": "string (min 6 chars)"
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

### 8.3 Game Flow

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

**Implementation Notes:**
- Session is created with `status = 'active'`
- `session_mode` defaults to `'normal'`

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
  "target_fen": "string | null",
  "target_blunder_srs": {
    "last_reviewed_at": "timestamp | null",
    "created_at": "timestamp | null",
    "pass_count": "integer",
    "fail_count": "integer",
    "pass_streak": "integer",
    "opportunities_since_review": "integer",
    "opportunities_30d": "integer",
    "reached_30d": "integer",
    "p_reach": "float"
  },
  "decision_source": "ghost_path | backend_engine"
}
```

- `mode: "ghost"` - Ghost is steering toward a blunder; `move` contains the next move.
- `mode: "engine"` - No blunder path found; `move` is produced by the remote Maia3 API.
- `target_blunder_id` - ID of the blunder being targeted, or `null` in engine mode.
- `target_fen` - FEN of the blunder position the ghost is steering toward (ghost mode only), or `null`.
- `target_blunder_srs` - SRS metadata for the targeted blunder (ghost mode only), or `null`.
- `decision_source` - Backend decision branch used to produce the move.

#### POST /api/game/end

Ends the current game session.

**Request:**
```json
{
  "session_id": "uuid",
  "result": "checkmate_win | checkmate_loss | resign | draw | abandon",
  "pgn": "string (full game PGN)",
  "is_rated": "boolean (default: true)"
}
```

**Response (200):**
```json
{
  "session_id": "uuid",
  "result": "string",
  "ended_at": "timestamp",
  "rating": {
    "rating_before": "integer",
    "rating_after": "integer",
    "is_provisional": "boolean"
  }
}
```

- `rating` is `null` when the game is unrated or the result has no rating impact (e.g., `abandon`).

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
      "classification": "best",
      "fen_before": "string | null",
      "move_uci": "string | null",
      "best_move_uci": "string | null",
      "best_line_uci": ["string"] ,
      "decision_source": "ghost_path | backend_engine | local_fallback | null",
      "target_blunder_id": "integer | null"
    }
  ]
}
```

- `session_id` comes from the path parameter.
- `eval_cp` / `best_move_eval_cp` use the normalized centipawn scale described in §7.4.
- `classification` must be one of `best|excellent|good|inaccuracy|mistake|blunder`.
- `decision_source` applies to opponent moves only; use `null` for player moves.
- `target_blunder_id` links the move to the blunder being targeted in ghost mode.

**Response (200):**
```json
{
  "moves_inserted": "integer"
}
```

**Rules:**
- Endpoint is called once per completed game; repeat calls replace the existing move set for idempotency.
- Any PGN string is still sent to `/api/game/end` (stored in `game_sessions.pgn`), while this endpoint remains JSON so downstream analytics don't need to re-parse PGN comments.

### 8.4 Blunders / Ghost Move Library Targets

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/blunder` | Record an auto-detected blunder (analysis-triggered) |
| POST | `/api/blunder/manual` | Manually add a MoveList decision to the Ghost Move Library |
| GET | `/api/blunders` | List user's Ghost Move Library targets |

#### POST /api/blunder

Records a mistake detected by the client-side engine (delta >= 50cp, within first 10 moves). Stores the full path from game start to the target position in the Ghost Move Library.
This endpoint enforces the first-auto-blunder-per-session rule and the 10-move recording cap.

**First-move exemption:** When the PGN contains only a single move (`len(moves_data) == 1`), the blunder is silently skipped and a no-op response is returned. Ghost mode cannot steer back to the starting position, so recording this case is pointless.

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
      "last_session_id": "uuid | null",
      "last_played_at": "timestamp | null",
      "created_at": "timestamp"
    }
  ]
}
```

- Results are sorted by `last_played_at` DESC (nulls last).
- `last_played_at` is the most recent timestamp at which the blunder position was reached in a game session.
- `last_session_id` is the session UUID for the most recent play.

### 8.5 SRS (Spaced Repetition)

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
- Call `recompute_opening_scores_if_needed()` for the blunder's player color to keep opening score cache fresh

### 8.6 Error Responses

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

### 8.7 Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| API Style | REST | Simpler than GraphQL for MVP; FastAPI excels at REST |
| Pagination | Deferred | MVP uses simple limit; cursor-based pagination post-MVP |
| Auth | Stateless JWT | No session storage needed; simple horizontal scaling |
| Error Format | Structured JSON | Consistent parsing for frontend error handling |

---

## 9. After-Game Analysis Display

When a game ends, users are presented with an analysis view showing their performance with engine evaluations.

### 9.1 Screen Layout

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

### 9.2 Components

#### 9.2.1 Chessboard

- Displays the position at the currently selected move
- Arrows can optionally show the best move (toggle)
- Highlights the last move played (from/to squares)

#### 9.2.2 Evaluation Graph

- **X-axis:** Move number (1 to N)
- **Y-axis:** Engine evaluation in pawns (-5 to +5, clamped)
- **Line color:** Gradient from white's perspective (green = white advantage, red = black advantage)
- **Markers:** Dots on the line at each move; colored by classification (win-chance model):
  - Red dot: Blunder (≥ 0.30 win-chance drop)
  - Orange dot: Mistake (≥ 0.20 win-chance drop)
  - Yellow dot: Inaccuracy (≥ 0.10 win-chance drop)
- **Interaction:** Clicking on the graph jumps to that move
- **Current position:** Vertical line indicator shows selected move

#### 9.2.3 Evaluation Bar

- Vertical or horizontal bar showing current position advantage
- Filled portion represents winning probability (based on eval)
- Numerical eval displayed: `+1.2` or `M3` (mate in 3)
- Color: White fill for white advantage, black fill for black advantage

#### 9.2.4 Navigation Controls

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

#### 9.2.5 Move List

- Standard two-column format (white move | black move)
- Current move highlighted
- Classification icons per move (from `classifyMoveAdvanced`):
  - `??` — Blunder (≥ 0.30 win-chance drop)
  - `?` — Mistake (≥ 0.20 win-chance drop)
  - `?!` — Inaccuracy (≥ 0.10 win-chance drop)
  - `✓` — Good move (≥ 0.02 win-chance drop)
  - `!` — Excellent move (< 0.02 win-chance drop, not best)
  - `⭐` — Best move (played move matches engine's top choice)
- Clicking a move navigates to that position

#### 9.2.6 Position Analysis Panel

Shows details for the currently selected move:

- **Best move:** Engine's recommended move with eval
- **Played move:** What was actually played with eval
- **Eval delta:** Difference in centipawns
- **Classification:** Blunder/Mistake/Inaccuracy/Good/Excellent/Best

### 9.3 Data Source

Analysis data comes from two sources:
- `session_moves` table — populated during gameplay by `GameAnalysisCoordinator` (Worker B) via `POST /api/session/{id}/moves` on game end
- `analysis_cache` table — pre-computed results for known positions; queried in parallel with the worker to accelerate first-analysis

Classifications are produced by `classifyMoveAdvanced` (win-chance model) during gameplay, stored alongside centipawn evals in `session_moves`.

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

### 9.4 API Endpoint

#### GET /api/session/:id/analysis

Returns full analysis for a completed game session.

**Response (200):**
```json
{
  "session_id": "uuid",
  "pgn": "string | null",
  "result": "checkmate_win | checkmate_loss | resign | draw | abandon | null",
  "player_color": "white | black",
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
      "classification": "best",
      "segment": "string"
    }
  ],
  "summary": {
    "blunders": 2,
    "mistakes": 3,
    "inaccuracies": 5,
    "average_centipawn_loss": 24,
    "accuracy": "integer | null"
  },
  "position_analysis": {
    "<fen_after>": {
      "best_move_uci": "string",
      "best_move_san": "string | null",
      "best_move_eval_cp": "integer | null",
      "best_line_uci": ["string"] 
    }
  },
  "expected_total_moves": "integer | null",
  "analyzed_moves": "integer",
  "is_complete": "boolean"
}
```

The summary's blunder/mistake/inaccuracy counts and `average_centipawn_loss`
are player-only: only moves whose `color` matches `player_color` contribute.
Average centipawn loss is nonnegative; negative `eval_delta` values are treated
as `0` for display/summary purposes.

### 9.5 Entry Points

The analysis screen (`/game?id=<session_id>`) is accessible from:

**Entry Point 1: Post-Game Prompt**
- Immediately after a game ends on `/play`, the game UI offers a link to view analysis
- Navigates to `/game?id=<session_id>` for the just-completed game

**Entry Point 2: Game History**
- User navigates to `/history`
- Selects any completed game from the list
- Opens `/game?id=<session_id>` for that historical game

**App navigation** (via `AppNav`):
- `/play` — Start/continue a game
- `/history` — Browse past games
- `/blunders` — Due blunders (Ghost Move Library)
- `/openings` — Opening performance
- `/stats` — Overall stats and rating graph

### 9.6 MVP Constraints

- **No engine lines:** MVP shows only the single best move, not multiple variations
- **No local analysis:** Display only the analysis captured during gameplay (no re-analysis)
- **No export:** PGN download deferred to post-MVP
- **No sharing:** Social/sharing features deferred

---

## 10. Game History View

The Game History view allows users to browse their past games and access analysis for any completed game.

### 10.1 Entry Points

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

### 10.2 Screen Layout

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

### 10.3 Game Card Data

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

### 10.4 Interaction Flow

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
│  (Section 9)     │
└───────────────────┘
```

**Click behavior:** Clicking anywhere on a game card opens the analysis view for that game (Section 9).

### 10.5 API Endpoint

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

History summaries follow the same player-only rule as session analysis for
blunder/mistake/inaccuracy counts and `average_centipawn_loss`. ACPL also clamps
negative eval deltas to zero, including legacy stored rows.

### 10.6 Empty State

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

### 10.7 MVP Constraints

- **No sorting:** Games always shown newest first
- **No filtering:** All games shown (filter by date/result/blunders deferred)
- **No pagination:** Simple limit-based loading (cursor pagination deferred)
- **No search:** Full-text search in PGN deferred
- **No mini-board preview:** Showing final position thumbnail deferred

---

## 11. Testing Strategy

### 11.1 Tooling

| Layer | Tooling | Scope |
| --- | --- | --- |
| Unit (Frontend) | Vitest | Pure functions, state reducers, utilities |
| Unit (Backend) | pytest | SRS math, Ghost Move Library helpers, DB query builders |
| Integration (Frontend) | React Testing Library | UI flows, board events, ghost state transitions |
| Integration (Backend) | pytest + httpx | API endpoints, DB interactions, SRS updates |
| E2E | Playwright | Full user journeys in the browser |

### 11.2 Coverage Priorities (MVP)

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

### 11.3 Key Test Cases

| Area | Test Case | Expectation |
| --- | --- | --- |
| SRS | pass_streak increments on correct replay | priority decreases |
| SRS | replay failure resets pass_streak | priority increases |
| Ghost | user deviates off path | ghost deactivates |
| Ghost | user transposes back to known node | ghost reactivates |
| Blunder | blunder stored against pre-move FEN | decision point preserved |
| Analysis | first auto blunder only | later mistakes ignored unless manually added |
| Manual add | duplicate position capture | `is_new=false` and UI shows "already in library" |

### 11.4 Test Data & Determinism

- Use fixed PGNs with known engine evals for replay scenarios.
- Seed any probabilistic SRS selection to make tests deterministic.
- Pin Stockfish evaluation settings for unit/integration tests that rely on eval deltas.

---

## 12. Rating System

Ghost Replay uses an Elo-style rating system to track player strength against the engine.

### 12.1 Constants

| Constant | Value | Meaning |
|----------|-------|---------|
| `DEFAULT_RATING` | 1200 | Starting rating for new users |
| `PROVISIONAL_THRESHOLD` | 20 | Games played before rating stabilizes |
| `K_PROVISIONAL` | 40 | K-factor during provisional period |
| `K_STABLE` | 20 | K-factor once rating is stable |

### 12.2 Formula

```
expected = 1 / (1 + 10^((engine_elo - player_rating) / 400))
new_rating = round(player_rating + K * (score - expected))
```

The opponent rating is `engine_elo` from the game session (the bot difficulty setting).

### 12.3 Result Scores

| Result | Score |
|--------|-------|
| `checkmate_win` | 1.0 |
| `checkmate_loss` | 0.0 |
| `resign` | 0.0 |
| `draw` | 0.5 |
| `abandon` | not rated |

### 12.4 When Rating Is Computed

A `rating_history` row is inserted at `POST /api/game/end` when `is_rated=true` and the result is one of the four rated outcomes above. `abandon` results are never rated regardless of `is_rated`.

The `is_provisional` flag is `true` when `games_played < PROVISIONAL_THRESHOLD` at the time of the game.

### 12.5 Post-Game Display

The `/api/game/end` response includes a `rating` field:

```json
{
  "rating_before": 1200,
  "rating_after": 1214,
  "is_provisional": true
}
```

The end-game banner uses these values to show the rating change. When `is_provisional=true`, a provisional indicator is shown alongside the rating.

`rating` is `null` when the game is unrated or the result is `abandon`.

DB reference: §5.5

---

## 13. Opening Weakness Tracking

The opening score system computes per-user 0-100 mastery scores (higher = better) for each opening line and surfaces them on the `/openings` page.

### 13.1 Trigger Points

- **After move uploads:** `recompute_opening_scores_if_needed()` is called at the end of `POST /api/session/:id/moves`. If the user's inputs (game history or opening registry) have changed since the last batch, a new batch is computed.
- **After SRS reviews:** `recompute_opening_scores_if_needed()` is called after each SRS review submission, since a review pass can change per-opening accuracy.
- **On openings page load:** reads are stale-while-revalidate. A **warm** reader (batch present) calls `request_recompute()` to schedule a coalesced background convergence and serves the cached batch immediately, never blocking; only a **cold** reader (no batch yet) blocks on `refresh_now()` for the one-time initial compute. All recompute decisions — cache miss, registry drift, stale branch keys, evidence change — are consolidated in `recompute_opening_scores_if_needed()` run on the single serialized worker. The worker first computes a **cheap raw-input freshness digest** (pure SQL, no python-chess) and, when nothing has changed, serves the cached batch **without building the evidence overlay** — the per-session board reconstruction + Lichess phase divider only run on the non-fast paths. This keeps unchanged loads at ~10ms instead of paying the full overlay rebuild.

### 13.2 Batch/Cursor Pattern

Computation runs are not overwritten in-place. Instead:

1. A new `opening_score_batches` row is created with a monotonically increasing `generation`.
2. `user_opening_scores` (named-root) rows and `opening_position_scores` (direct tree-position) rows for the new batch are written from one shared calculation, in the same transaction (see §5.7.4).
3. The `opening_score_cursors` row for `(user_id, player_color)` is updated to point to the new generation.
4. Stale batches are pruned (cascading both score tables through `batch_id ON DELETE CASCADE`).

This ensures the current scores are always available atomically and reads never see a partially-computed state.

`registry_fingerprint` captures a hash of the opening registry **plus** the score-model, phase-divider, and quality-curve versions (`SCORE_MODEL_VERSION`, `DIVIDER_VERSION`, `QUALITY_VERSION`, `TAU_WC`, `TAU_CP`) at compute time. If it changes (new openings, or a model/divider/curve change), the next trigger forces a full recompute and all prior snapshots are invalidated.

`inputs_fingerprint` is the **raw-input freshness digest** (`opening_score_raw_inputs_fingerprint`) used to decide whether evidence changed. It hashes a canonical, order-independent projection of exactly the raw DB rows the evidence overlay reads — session_moves (+ game_sessions), the bounded analysis_cache fallback subset, ghost-target blunders/positions, and blunder_reviews — with **no overlay build and no python-chess board replay**, folded together with `registry_fingerprint` and an explicit `OPENING_EVIDENCE_INPUTS_VERSION`. Because the overlay is a pure deterministic function of these inputs, an unchanged digest provably implies identical scores, so the worker can fast-path. `OPENING_EVIDENCE_INPUTS_VERSION` must be bumped on any evidence-derivation semantic change a raw-row hash cannot see (e.g. `PASS_THRESHOLD`, quality-source precedence, FEN normalization, phase-filter application, or the digest's own SQL projection/filters); on first read after such a change (or after deploy) the stored fingerprint mismatches and self-heals with exactly one recompute per (user, color). Scoping that is broader than the overlay (all session moves, analysis_cache keyed by FEN only) is correctness-safe: it can only cause an unnecessary, never a missed, recompute. The digest is computed **before** the overlay in `recompute_opening_scores`, so a stored fingerprint can never be newer than the scored inputs.

### 13.3 Score Semantics

- `opening_score`: **0-100 mastery** score (higher = better), computed **directly per root** — no confidence-weighted descendant rollup.
- **Card / hero semantics:** each `/openings` card shows its **direct** root row; the top-level hero shows a **synthetic initial-position** ("whole repertoire") row persisted under the normalized initial FEN (`opening_family = "__repertoire__"`), which is excluded from family roll-ups. `subtree_root_count` is navigation metadata (count of scored named rows in the subtree) and never feeds a score; a card is unscored when its direct `subtree_score` is null.
- `confidence` and `sample_size`: let the frontend de-emphasize scores backed by sparse data
- Branch fields: each score row includes `strongest_branch`, `weakest_branch`, and `underexposed_branch` sub-line details, persisted from the same shared calculation and read directly by the drill-down (no per-request recompute)

The score engine evaluates all named roots for one user/color as a shared DAG rather
than building a separate name-owned subtree per opening. Structural reachability
includes observed session continuations plus reference-book edges that remain before
the raw middlegame boundary. Observed edges are already phase-authoritative and are
therefore never removed by the board-local middlegame predicate.

Local mastery uses continuous move quality with a skeptical beta prior. Actual and
perfect recursive metrics are memoized by normalized FEN, so transpositions are
computed once and opening labels do not change node values. Repetition cycles are cut
deterministically by SCC and canonical FEN order, with remaining child weights
renormalized. A named root receives a score row only when its structurally reachable
set contains at least one quality observation; review-only and ghost-target-only
positions do not create prior-only rows.

DB reference: §5.7

### 13.4 Opening Lineage in `/history`

The `/history` analysis footer renders an opening-lineage stack (`GameOpeningLineage`) showing the openings played in the selected game, broadest to deepest, each with its score and grade.

- **Single-action chip:** Each chip is one button. Clicking it (1) toggles the inline `OpeningFamilyCard` (analysis variant) and (2) selects that opening's root position on the board/MoveList/graph by jumping to the game move whose `fen_after` matches the opening key. A second click on the same chip collapses its card. If no game move matches the opening key, the board selection is a no-op (the card still toggles).
- **In-card actions:** The link to `/openings` ("View in Openings") and the **Start Drill** button live inside the expanded card. Start Drill is a new opening-drill entry point from history — it navigates to `/play` with `drillSetup: { openingKey, playerColor }`.

---

## 14. Analysis Cache

The analysis cache avoids re-running Stockfish on positions that have already been evaluated in prior games.

### 14.1 Key Structure

Each entry is keyed by `(fen_before, move_uci)` — the exact position before a move and the move played in UCI notation. This pair uniquely identifies an analysis result.

### 14.2 Frontend Lookup

`lookupAnalysisCache(positions)` in `src/utils/api.ts` sends a batch `POST /api/analysis/lookup` request. It returns a `Map<string, CachedAnalysis>` keyed by `"fen::move_uci"` (only cache hits are returned).

Used in `GameAnalysisCoordinator` and `useMoveAnalysis` alongside Stockfish analysis tasks. Cache hits bypass the local engine only when `canResolveCachedAnalysis` can prove the row is complete: it must include classification data, `best_move_uci`, and a multi-move `best_line_uci` whose first move matches `best_move_uci`. Legacy, precomputed, eval-only, or one-token rows are treated as misses so the worker can backfill a full PV.

### 14.3 Staleness & Quality-Aware Replacement

There is no time-based invalidation, but entries are **not** immutable: a higher-quality analysis of the same `(fen_before, move_uci)` can replace or merge into an existing row. All writers go through the shared quality-aware writer and deterministic replacement policy described in §5.6. The governing rules:

- A row's trust comes from its **profile** (engine/search identity, verified against the in-code registry) and its **evidence contract** (data-shape, with per-contract semantic validation) — never from raw numeric depth.
- Browser `game` uploads are non-authoritative: they fill keys with no evidence but never downgrade a canonical or legacy row.
- Sparse rows (e.g. JeffML eval-only) can never replace richer rows; replacement/merge requires contract succession plus a populated-field superset, so no datum is silently dropped.
- Only a re-run authoritative canonical profile reclaims legacy (NULL-metadata) rows.

Writes are concurrency-safe: PostgreSQL uses insert-first + `SELECT … FOR UPDATE`; file-backed SQLite uses `BEGIN IMMEDIATE` + `busy_timeout` with bounded retry.

### 14.4 `source` Field & quality metadata

`source` records provenance only (it is not the quality comparator):

| Value | Meaning |
|-------|---------|
| `game` | Entry written during a normal game session (browser worker) |
| `precomputed` | Entry written by the opening precompute pass |
| `jeffml-scores` | Entry ingested from the JeffML scores dataset (eval-only) |

Quality/trust is carried by the metadata columns (`analysis_profile_id`, engine identity, `evidence_contract_id`) and surfaced on the lookup response via an `authoritative` flag. See §5.6 for the full model.

DB reference: §5.6

### 14.5 Cache Repair & Invalidation

The write guard (§5.6) only protects *new* writes. Rows that predate it —
game-overwritten depth-17 results, partial legacy precompute rows, or rows that
claim a profile they cannot back up — can still preserve unstable best moves,
deltas, and classifications, which feed the eval-delta / win-chance fallbacks
even though they never count as `trusted_for_resolution` hits.

Repair has two halves, run **after** write protection is live:

1. **Regenerate** — `scripts/precompute_openings.py` rewrites authoritative
   `resolver-complete-v2` rows for opening positions. It is idempotent/resumable
   and routes through the shared writer, so re-running is safe.
2. **Invalidate** — `scripts/repair_analysis_cache.py` deletes the rows the
   current write guard would reject if they arrived today. The classifier
   (`backend/app/analysis_cache_audit.py`) is anchored on one predicate — the
   comparator's own `incoming_is_valid` gate (contract satisfied AND no
   unverifiable profile claim) — and sorts every row into one category:

   | Category | Guard | Action |
   |----------|-------|--------|
   | `canonical_trusted` | accepts | keep (the rows the precompute produces) |
   | `canonical_retired` | accepts | keep (identity-verified; retired or weaker-than-v2) |
   | `non_auth_valid` | accepts | keep (browser/JeffML, valid for its contract) |
   | `legacy_valid` | accepts | keep (no profile claim; satisfied contract) |
   | `legacy_invalid` | rejects | invalidate **only** under `--include-legacy-null` |
   | `contaminated_profile_claim` | rejects | invalidate by default |

   `contaminated_profile_claim` carries a non-null `analysis_profile_id` but
   fails the guard (unverifiable stored identity, or evidence that fails its
   declared contract). `legacy_invalid` is profile-less yet still guard-rejected
   (null / unsatisfied evidence contract, including key-only placeholders) —
   these are consumed without trust validation by the eval-delta fallback, so the
   opt-in removes them.

The repair tool defaults to a non-destructive **audit** (per-category counts +
JSON report); `--apply` performs the bounded, separately-committed, resumable
deletes; `--verify` exits non-zero if any invalidation-eligible row remains.
Deployment and rollback steps are in `backend/scripts/REPAIR_ANALYSIS_CACHE.md`.

---

## 15. Local Fallback

When the backend is unreachable, the frontend uses local Stockfish to generate opponent moves rather than blocking gameplay.

### 15.1 `decision_source: 'local_fallback'`

The `decision_source` column on `session_moves` accepts three values:

| Value | Source |
|-------|--------|
| `ghost_path` | Backend served a Ghost Move Library move |
| `backend_engine` | Backend served an engine move |
| `local_fallback` | Frontend generated the move locally (backend unreachable) |

`local_fallback` is set exclusively by the frontend in `applyLocalFallbackMove()` (`useChessGameController.ts`). The backend never produces this value — it is excluded from the `NextOpponentMoveResponse` type.

### 15.2 Behavior

- Ghost path steering is unavailable in fallback mode (no backend response to provide path data).
- The locally-generated move is committed with `decisionSource: "local_fallback"` and the game continues normally.
- Client-side blunder detection and analysis still run.
- Move uploads proceed as normal once connectivity is restored.

---

## 16. Practice Continuation

Practice Continuation is the local free-play state a session enters when the user rewinds the board to a prior position mid-game.

### 16.1 Trigger Flow

1. User clicks the Revert button to select an earlier position.
2. If the current game is rated (`isRated=true`), a confirmation modal is shown (`showRevertWarning=true`).
3. On confirm: the game is ended as `resign` via `POST /api/game/end`, and any rating change is applied.
4. The board rewinds locally to the selected position.
5. `isPracticeContinuation = true`, `isRated = false`, drill state cleared, session move uploads halted.

### 16.2 Behavior While Active

| Aspect | Normal game | Practice continuation |
|--------|-------------|----------------------|
| Game-over API call | `POST /api/game/end` | None — `finishLocalGame()` only |
| Move uploads | Yes | No |
| SRS reviews | Triggered | Not triggered |
| Rating change | Yes (if rated) | No |
| Ghost / drill | Active | Disabled |
| Resign button | `POST /api/game/end` | `finishLocalGame()` locally |

### 16.3 Reset

`isPracticeContinuation` resets to `false` when a new game session is started.

---

## 17. Drill Mode

Drill Mode is a structured opening practice feature. The user plays toward a specific target position from the opening graph, then optionally converts the session into a rated game from that point forward.

### 17.1 Session Type

Drill sessions use `session_mode = 'drill'` in `game_sessions`. They start unrated (`is_rated = false`) and can become rated upon conversion.

### 17.2 Drill States

| State | Meaning |
|-------|---------|
| `active` | Playing toward the target opening position |
| `root_reached` | User successfully reached the target FEN |
| `failed` | User deviated from route or made an accuracy mistake post-root |
| `abandoned` | User quit the drill without converting |
| `converted` | User elected to continue as a rated game after reaching root |

Only `converted` drill sessions appear in game history alongside normal games; all other states are hidden.

### 17.3 Strictness

Strictness controls the centipawn threshold for accuracy failures after `root_reached`.

| Tier | cp threshold |
|------|-------------|
| `strict` | 15 |
| `standard` | 35 |
| `lenient` | 50 |

A custom `strictness_cp` integer (0–50) overrides the tier value.
At `strictness_cp = 0`, the drill requires the exact engine best move; non-best
moves fail even when post-move eval noise resolves to 0cp loss or better.

### 17.4 Route Check

`POST /api/drills/:id/route-check` is called after each move. The backend uses `DrillRouteMap` (a BFS-derived map from the opening graph) to classify the current position:

| Status | Meaning |
|--------|---------|
| `on_route` | Position is on the path to target; response includes route-preserving move suggestions |
| `root_reached` | Target FEN reached; `drill_state` advances to `root_reached` |
| `failed` | Position left the route (`off_route`) or an accuracy threshold was exceeded (`accuracy`) |

### 17.5 Conversion

`POST /api/drills/:id/continue` converts a `root_reached` drill into a rated game:

1. `drill_state = 'converted'`, `is_rated = true`
2. `rated_start_ply` records the ply at which normal play begins
3. `resegment_session_moves()` retroactively labels prior moves `segment = 'drill'` and future moves `segment = 'normal'`
4. Normal game flow continues; game ends via `POST /api/game/end` with rating impact

> **Note (g-a406):** Conversion via the `/continue` endpoint remains the path for a
> `root_reached` drill the engine code may still drive, but the drill-end UI no longer
> offers "Continue as normal game" — that action distorted ratings (drill until a strong
> position, then play out an easy win). It is replaced by **Analyze** (§17.8).

### 17.6 Terminal Reasons

| Reason | Cause |
|--------|-------|
| `off_route` | Player left the prescribed opening route |
| `accuracy` | Post-root move exceeded the centipawn strictness threshold |
| `natural_end` | Game ended by checkmate/draw before root was reached |

### 17.7 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/drills/start` | Create a new drill session |
| GET | `/api/drills/:id` | Fetch the drill session contract |
| POST | `/api/drills/:id/route-check` | Check current position against drill route |
| POST | `/api/drills/:id/continue` | Convert to rated game after root reached |
| POST | `/api/drills/:id/fail` | Mark drill failed (accuracy, post-root only) |
| POST | `/api/drills/:id/natural-end` | Record natural game-over during drill phase |
| POST | `/api/drills/:id/abandon` | Abandon drill (use `/api/game/end` for converted drills) |

### 17.8 Post-Drill Analysis (transient)

When a drill stops (`failed`), the drill-end actions are **Again** and **Analyze**
(replacing the removed "Continue as normal game"; see §17.5 note). **Analyze** opens an
ephemeral, in-memory review of the just-played drill on the dedicated `/drill-analysis`
route:

1. While `ChessGame` + `AnalysisEffects` are still mounted, a targeted completion barrier
   awaits only the failed move's analysis if it is still pending (off-route failures check
   the route independently of engine analysis). Blunder/SRS and normal evidence side effects
   are flushed *before* navigation so the fire-and-forget POSTs survive the unmount.
2. The live `moveHistory` + analysis map are snapshotted into a narrow, non-persisted client
   store (`drillAnalysisStore`). Plies whose analysis is still unresolved keep null fields.
3. The drill is abandoned (`abandonStoppedDrill`): unrated, hidden, game inactive. The live
   analysis session is cleared so it idle-shuts down.
4. `/drill-analysis` renders the existing data-driven `AnalysisBoard` from the snapshot, with
   a minimal "Drill review — not saved" footer (no `GameReviewStats` — accuracy is not
   available for a transient snapshot).

The review is ephemeral: refreshing or navigating directly to `/drill-analysis` finds no
snapshot and redirects to `/play`. **No conversion, rating, history entry, or game statistics
are created.** Abandoned/failed drills stay hidden from `/api/session/:id/analysis`, history,
and normal game analysis via the existing visibility guard. Persisting a drill review would
require a dedicated drill-analysis endpoint (future work).

**Returning to the drill (g-65ve).** The review surface has an explicit "Back to drill"
control (in addition to the browser back button) that navigates to `/play` with a one-shot
router marker `{ returnFromDrillAnalysis: { sourceSessionId } }`. The snapshot is
identity-bound: it carries the exact `sourceSessionId` it was captured from, and the marker,
snapshot, and retained game store must all reference that same session for a restore to occur.
On mount, `/play` decides **synchronously** (no post-paint effect, so the new-game popup never
flashes) whether this is a valid reviewed-return: the marker/snapshot/store session IDs match,
`isGameActive === false`, `drillState === "abandoned"`, the opening key and move history are
present, and the full restart settings (player color, engine Elo, strictness tier, exact
`drillStrictnessCp`) are available. When valid, the retained board, moves, orientation, and
settings are read straight from the game store and the original drill-stopped actions
(`DrillStopActions` — the terminal-reason subtitle plus **Again**/settings) are restored;
the **Analyze** action keeps its original label but is re-wired on return to simply
re-navigate to `/drill-analysis` using the still-present snapshot (rebuilding would overwrite
the saved review with an empty map, since the live analysis session was cleared on the way
out). The generic post-game
"New game" banner is suppressed so no misleading "Drill abandoned" message appears. The board
stays disabled behind the `isGameActive === false` gate. The on-mount rating fetch does **not**
resample engine Elo while a drill context is loaded, so "Again" replays the retained Elo. The
"Back to drill" control is an in-flow row; the analysis board's viewport-driven height is
compensated by that row so the board is not pushed below the fold. The marker is consumed via
replace
navigation but the reviewed presentation persists until an explicit transition clears it
(successful drill/normal-game start, the gear opening the setup overlay, or a reset). Identity
is never inferred from opening key, moves, or reusable settings, and the abandoned backend
session is **never revived** — any mismatch or missing precondition falls back to ordinary
`/play` initialization.

DB reference: §7.3 (`game_sessions` drill columns)
