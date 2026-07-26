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
18. Stats Summary Populations

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
  * `eval_loss_cp` — Severity of the original mistake (larger = higher priority). Severity is normalized to a **decisive-mistake ceiling** (0..1000cp): raw values above the ceiling do not increase priority further (a mate pseudo-cp ~10000 and a real −1200 blunder are equally severe), though the other factors still differentiate them. Larger-*below*-cap still means higher priority.
  * `distance` — Moves to reach the blunder from the current position (closer = higher priority)
* **Steering Radius:** The Ghost only targets blunders reachable within 5 moves of the current position. Anything beyond 5 moves is ignored — the branching factor makes deeper steering unreliable.
* **Binary Grading:** Pass or fail only. No easy/good/hard ratings — chess moves are unambiguous.
* **Instant Feedback:** When a user reaches a stored blunder position:
  * **Failure:** If they play a move ≥50cp worse than the best move, the game pauses. "You made this blunder again." → `pass_streak` resets to 0.
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
    eval_loss_cp INTEGER NOT NULL,         -- RAW/UNCAPPED centipawn delta at capture time; MAY BE NEGATIVE (manual captures accept independent eval_before/eval_after). Normalized 0..1000 only at read/decision (Ghost/SRS severity, /stats, Blunder Library).

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
- `eval_loss_cp` is stored **RAW/uncapped** (audit + future severity models) and **may be negative** for manual captures. It is **normalized 0..1000** (`centipawn_loss`: floor 0, cap `CENTIPAWN_LOSS_CAP_CP = 1000`) at every read/decision — Ghost/SRS severity, /stats avg + top-costly, and the Blunder Library list — never in place. No migration corrects legacy rows; read-time normalization is the guarantee.

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
    eval_delta_cp INTEGER NOT NULL                      -- Server-normalized to 0..1000 (centipawn_loss) at write; write-only
);

CREATE INDEX idx_blunder_reviews_blunder ON blunder_reviews(blunder_id, reviewed_at);
```

**Usage notes:**
- Rows are append-only to preserve the user's study history
- `reviewed_at` doubles as the timestamp returned in `review_history`
- The API response nests `{ reviewed_at, passed, move_played }` derived from this table (with `move_played` mapped from `move_played_san`)
- `eval_delta_cp` is **normalized server-side** (`centipawn_loss`: floor 0, cap 1000) at write, regardless of client — an old/non-browser client that bypasses the frontend cap cannot persist a mate-magnitude or negative value. It is a uniform 0..1000 quantity **after server normalization** across storage and analytics (inbound wire values may still arrive raw). The column is **write-only** — no backend decision or display reads it — so legacy pre-normalization rows are harmless and there is no backfill.

#### 5.2.2 Centipawn-loss contract: raw evidence vs. normalized display/decision CPL (living design)

There are two distinct notions of "centipawn loss" that must never be conflated:

1. **RAW evidence** — the exact, uncapped magnitude, retained at rest for audit, contract validation, and future models. A mate blunder legitimately holds ~10020 (`analysis_cache.eval_delta`) or ~10000 (`blunders.eval_loss_cp`). Raw values are **never** capped in place.
2. **NORMALIZED display/decision CPL** — the 0..1000 value shown to users and read by decisions (Avg CPL, Ghost/SRS severity, /stats, Blunder Library). Produced by the single normalizer pair `centipawn_loss()` / `centipawn_loss_expr()` (Python) ↔ `evalLoss()` (TS), applied at **read / projection / decision** time.

Per-column contract:

| Column | At rest | Normalized |
|--------|---------|-----------|
| `analysis_cache.eval_delta` | **RAW** (contract-bound; `/api/analysis/lookup` returns it raw) | Only at projection (`build_move_upgrade` → `centipawn_loss`); capping in place would fail the resolver-complete-v2 `delta == best − played` equality contract |
| `blunders.eval_loss_cp` | **RAW/uncapped** (may be negative for manual captures) | At every read/decision: Ghost + SRS severity, Ghost sort tiebreaker, /stats avg + top-costly, Blunder Library list |
| `session_moves.eval_delta` | **Normalized** on new writes; legacy rows **MIXED** (raw >1000) | Again at every read (Avg CPL, per-move echo, opening-quality curve) — read-time normalization is the actual guarantee |
| `blunder_reviews.eval_delta_cp` | **Normalized** server-authoritatively at write | Write-only — no read normalizes it because nothing reads it |

The cap constant is `CENTIPAWN_LOSS_CAP_CP = 1000` (backend) / `EVAL_LOSS_CAP_CP = 1000` (frontend) — the **decisive-mistake ceiling**, defined independently per runtime and pinned equal by a cross-runtime golden-vector fixture + a unit test. No migration corrects any legacy row; read-time / decision-time normalization is the correctness guarantee (`analysis_cache` and `blunder_reviews` are the exceptions — the former stays raw by contract, the latter is normalized on write).

**Aggregate rounding.** Normalized CPL aggregates are averaged in the database and rounded to an integer with **Decimal-preserving half-up** rounding (`round_half_up_cpl`, `backend/app/centipawn_loss.py`), matching the frontend's `Math.round` (`gameStats.ts`) and the convention `accuracy_v1.py` already set for accuracy. Half-up, not banker's: an exact `.5` rounds **up** (2.5 → 3). The value is rounded through `Decimal`, never a float round-trip — PostgreSQL `AVG(NUMERIC)` returns `Decimal` while SQLite returns `float`, and casting a near-half `Decimal` to float corrupts it (`float(Decimal("2.4999999999999999")) == 2.5`, which would round up to 3 instead of down to 2). The three aggregate sites — `average_centipawn_loss` in session analysis and in history summaries, and `library.avg_blunder_eval_loss_cp` in `/stats` — pass the database aggregate to the helper directly, with no intervening `float()`. The helper is nonnegative-only by contract (`centipawn_loss_expr` floors at 0): half-up and away-from-zero coincide only for nonnegatives, so it is not a general-purpose rounder.

*Forward direction:* a bounded **win-chance-loss** severity would be more meaningful than a centipawn delta, but requires retaining before/after evals — a future refinement, out of scope here.

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
-- Release A durable-head index (g-accuracy-schema). Built CONCURRENTLY in production.
CREATE INDEX idx_rating_history_user_chain
    ON rating_history(user_id, games_played DESC, recorded_at DESC, id DESC);
```

**Key semantics:**
- One row per completed rated game; inserted by `POST /api/game/end` when `is_rated=true`
- `is_provisional` tracks whether the rating is still considered provisional (based on games played count)
- `games_played` enables the frontend to show progress toward a stable rating
- `chesscom_*` and `lichess_*` fields are nullable; reserved for future cross-platform rating import

**Durable head (Release A, g-rating-serial).** The "current rating for a user" is the row with the greatest `games_played` — not the most recent wall-clock `recorded_at`, which can go backwards under clock skew. The head lookup is `WHERE user_id = ? ORDER BY games_played DESC, recorded_at DESC, id DESC LIMIT 1`; `idx_rating_history_user_chain`'s trailing DESC columns let PostgreSQL answer that ORDER BY straight from the index with **no Sort node**. The older `idx_rating_history_user_timestamp` stays for chronological trend reads.

**Rated game-end serialization (g-rating-serial).** A rated, rating-affecting `POST /api/game/end` takes a `FOR NO KEY UPDATE` lock on the game's `users` row (via `app.row_locks.for_no_key_update`) before computing the rating delta and appending the row, so two concurrent rated ends for the same user cannot both read the same `games_played` and lose an increment. Unrated and non-rating-affecting ends take no users lock. A rated end for a **missing** users row fails closed (500) rather than inventing a rating. The `uq_rating_history_game_session` unique index makes a concurrent double-end of the *same* session idempotent: exactly one insert wins and the loser gets a 400.

**Production build note.** In production `idx_rating_history_user_chain` is created (and, on downgrade, dropped) `CONCURRENTLY` inside an Alembic `autocommit_block` so rated writes stay available during the build. A `CONCURRENTLY` build that fails partway leaves an **INVALID** index that must be `DROP INDEX CONCURRENTLY`-ed and rebuilt — it can never be validated in place (check `SELECT indisvalid FROM pg_index …`). See the release runbook (`docs/release_a_runbook.md`) for the rehearsed durations and recovery drill.

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
1. **Exact `(fen_before, move_uci)` wins** when its row has a usable, *move-trusted*
   played eval.
2. Otherwise an **indexed normalized fallback** over `(normalized_fen_before, move_uci)`
   among *move-trusted* rows selects deterministically: prefer rows with mate data, then
   `source=precomputed` > `game` > other, then lowest `id`.
3. Otherwise — only when **no trusted eval exists** — an **untrusted played-eval
   fallback** surfaces whatever cached eval we have so off-book cards show a number
   instead of an em dash: the untrusted exact row (tier 3), else the best untrusted
   normalized row by the same deterministic key (tier 4). This tier is source-agnostic
   (any non-authoritative row with a usable played eval — browser-game, bare-source, or
   other — qualifies), so the **move-card eval is NOT strictly trust-gated**. The
   column-0 **root** eval (`lookup_root_eval`) has no such fallback and stays trusted-only.
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
  (`resolver-complete-v1` mirrors the worker's old combined resolve guard;
  `resolver-complete-v2` is the fail-closed trust contract — full eval triple,
  enum-valid classification, PV-first-equals-best, and active-color delta
  consistency; `minimal-played-eval-v1` / `minimal-best-eval-v1` cover eval-only
  rows; the grain-split `position-complete-v1` / `move-complete-v1` contracts —
  §14.6.4 — mirror the worker's now-split `canResolvePositionAnalysis` /
  `canResolveMoveAnalysis` guards). Replacement/merge requires contract succession
  plus a populated-field superset so no datum is ever silently dropped.

Net guarantees: a browser `game` upload is non-authoritative — it may fill keys
that have no evidence but can never downgrade a canonical or legacy row; sparse
JeffML rows can never replace richer ones; only a re-run authoritative canonical
profile reclaims legacy rows. Writes are serialized safely: PostgreSQL uses
insert-first + `SELECT … FOR UPDATE`; file-backed SQLite uses `BEGIN IMMEDIATE` +
`busy_timeout` with bounded retry; other dialects are rejected. The `/api/analysis/lookup`
response exposes `source`, `analysis_profile_id`, `engine_version`, `engine_build`,
`evidence_contract_id`, and an `authoritative` trust flag derived from the same
validation the writer uses.

**Stronger analysis-board evidence (g-cache-stronger-evals → g-reuse-d21-search).**
The analysis board runs a depth-21 search that is deeper than the in-game depth-17
producer, and that stronger evidence is persistable. As of **g-reuse-d21-search**
the durable producer no longer runs a *second* hidden analyzer: it REUSES the
already-completed, unrestricted visible depth-21 MultiPV-3 search the board already
performs for arrows/lines. Key points:

- **Visible-MultiPV reuse producer.** When the exact played mainline move appears in
  the completed visible MultiPV lines, a same-search evidence row is derived from
  line 1 (best) and the played line — with NO additional Stockfish search — and
  submitted through the analysis-evidence endpoint. The best and played facts are two
  lines of ONE completed request, so the tuple is internally consistent by
  construction (unlike the retired hidden protocol, whose independent post-move
  searches could contradict the hidden root ordering — g-kgiq). When the played move
  is OUTSIDE the visible lines the producer does nothing and the existing depth-17
  evidence is retained (no targeted search — a separate product/compute decision). A
  restricted/hybrid visible search (the `trustedBest` `searchmoves` MultiPV-2 path) is
  skipped; the reuse feature never spends extra compute to widen coverage there.
- **Five-layer evidence vocabulary (`backend/app/evidence_policy.py`).** The shared
  browser-evidence policy (g-browser-policy-v2) separates *identity* (does a row's
  stored metadata match its claimed profile — one `verify_identity`, replacing five
  duplicated exact-equality checks; a per-profile `dynamic_fields` seam, filled by
  `browser-game-v2` in g-mk1d), *protocol* (is the producer internally consistent —
  `PROTOCOLS`, whose `internally_consistent` flag now GATES capability grants at
  registry load rather than merely documenting them), *contract* (evidence shape —
  `evidence_contracts`), *comparison*
  (which of two valid rows supersedes the other and why — `compare_evidence_rows`),
  and *capability* (which consumers may reuse a row — `has_capability`). Read/reuse
  grants beyond `DISPLAY_OVERLAY` (g-v21l) and the cross-grain authority rule (g-6xc3)
  lay their API here but are not yet wired; measured-strength comparison is wired by
  g-mk1d (below).
- **`EDGES` and kinds.** Cross-profile ordering is explicit directed edges, never raw
  depth, each tagged `AUTHORITY` (canonical over any non-authoritative row),
  `PROTOCOL_CORRECTION` (a truthful protocol fixes a defective one), or `TIER_BASELINE`
  (a deeper same-family tier replaces a shallower one). Current edges: both canonical
  manifests → {`browser-game-v1`, `browser-analysis-v1`, `browser-analysis-multipv-v2`,
  `jeffml-scores-v1`} (AUTHORITY); `browser-analysis-v1` → `browser-game-v1`
  (TIER_BASELINE); **`browser-analysis-multipv-v2` → `browser-analysis-v1`**
  (PROTOCOL_CORRECTION); **`browser-analysis-multipv-v2` → `browser-game-v1`**
  (TIER_BASELINE). A registry-load assertion fails closed unless `EDGES` and each
  profile's `dominates` set agree in both directions. `compare_evidence_rows`
  implements the authority barrier and explicit-edge steps; unequal non-edged rows
  then fall through to the measured-strength steps g-mk1d filled in, which
  **delegate to `compare_row_strength`** (see below) and return `INCOMPARABLE` unless
  the two rows share scoring semantics. Delegation, not a second implementation:
  one pair of rows must not order differently depending on which caller asked.
- **`Supersession` carries the GRAIN that decided, not just the winner.**
  `A_SUPERSEDES` / `B_SUPERSEDES` mean a CATEGORICAL win from the authority barrier or
  a registered edge — independent of any number either row measured, so a depth-30
  browser row still loses to canonical. `A_STRONGER` / `B_STRONGER` / `EQUAL` mean a
  MEASURED ordering from steps 4-5, and carry `kind=None` because no registered edge
  justifies them; they agree name-for-name with `compare_row_strength`, which is steps
  4-5 alone (Rule 2a calls it directly, where steps 2-3 cannot fire). The comparator
  never flattens the measured grain into the categorical one: a caller that cannot
  tell them apart cannot report a measured replacement as `strength_replace`. Callers
  whose local decision genuinely treats outcomes alike collapse them THEMSELVES —
  Rule 5 keeps the stored row for `B_STRONGER`, `EQUAL` and `INCOMPARABLE` alike.
  Cache Rule 5 routes through it: a measured `A_STRONGER` reports `strength_replace`
  (the same reason Rule 2a uses for the same fact), a PROTOCOL_CORRECTION supersession
  reports `protocol_corrected_replace`, AUTHORITY / TIER_BASELINE keep
  `dominates_replace`. This is REACHABLE today, not latent: a `browser-game-v2` row
  shares engine, build, net, MultiPV 1 and the `browser-analyzer-v1` protocol with a
  stored retired `browser-analysis-v1` row and is connected to it by no edge, so the
  two differ only in depth and rank normally — a self-reported depth above 21 replaces
  the stored d21 row with `strength_replace`. (The shipped client clamps to 17, but the
  dynamic provenance contract is deliberately permissive about the VALUE; strength
  still cannot cross the authority barrier or reclaim an all-`None` legacy row.) Every
  reason that means "the stored row is now this evidence" must therefore appear in
  EVERY consumer's success allowlist — the evidence endpoint's
  `_EVIDENCE_ACCEPTED_REASONS` (else the write lands and the open MoveList silently
  never receives its `MoveUpgrade`) and the precompute script's `_ACCEPTED_REASONS`
  (else a successful upgrade is counted as a `write_failure` and fails the run).
  `protocol_corrected_replace` is excluded from the precompute list on purpose: that
  producer is authoritative, and the authority barrier resolves canonical-vs-browser
  before explicit edges, so a canonical write can never earn it.
- **`browser-analysis-multipv-v2` profile.** The corrective successor. Its identity is
  the ACTUAL visible worker (`stockfishWorker.ts`): the same pinned
  `stockfish-18-lite-single` artifact and single net `nn-9067e33176e8.nnue` as the
  retired hidden profile, but the visible worker's real **Hash 64** and **MultiPV 3**
  under the internally-consistent `browser-visible-multipv-v1` protocol, at depth 21.
  NON-authoritative, `replacement_eligible`, ACTIVE. It correctively replaces a
  defective `browser-analysis-v1` row (PROTOCOL_CORRECTION) and a weaker
  `browser-game-v1` d17 row (TIER_BASELINE) for the exact key, but never dominates
  canonical, reclaims legacy rows, or becomes read-trusted.
- **`browser-analysis-v1` retirement.** The hidden root + independent post-move
  protocol (`browser-analyzer-v1`) is internally inconsistent and is RETIRED
  (`active=False`) in this release. Its stored rows stay `identity_verified` (the
  manifest digest excludes `active`/`dominates`), so they keep `DISPLAY_OVERLAY`
  (a retirement-surviving capability) and remain correctively replaceable, but a new
  incoming v1 row fails closed (`inactive_profile_keep`) — closing the fail-open
  retirement window in the decision layer, in addition to the endpoint discriminator.
- **Producer discriminator (mandatory).** The endpoint stamps all identity/profile
  fields server-side; the client selects no profile id and instead sends
  `producer: "visible-multipv-v1"`. An absent producer (a stale client running the
  retired hidden worker) is rejected per-row `stale_producer`; an unrecognized value
  `unknown_producer`; the single allowed token maps to `browser-analysis-multipv-v2`.
  HTTP stays 200 with the normal result list.
- **Backend classification rederivation (every row).** Beyond
  `resolver-complete-v2`'s enum/arithmetic checks, the endpoint independently rederives
  each row's classification from the best/played root-relative scores using a dedicated
  root-alternative classifier and rejects any disagreement (`classification_mismatch`).
  Lower lines and mate transitions are as client-supplied as line 1. The root
  classifier (`classify_root_alternative` / `classifyRootAlternative`) takes a truthful
  ROOT side-to-move contract — NOT the post-move opponent-to-move argument order of
  `classify_move_advanced` — while sharing the win-chance / mate thresholds; the two
  classifiers are pinned across TS and Python by
  `backend/tests/fixtures/root_classification_vectors.json`.
- **Capabilities / overlay.** `display_upgrade_eligible` is now
  `has_capability(row, DISPLAY_OVERLAY)` AND the profile's `OVERLAY_MODE == ALWAYS`
  (truth-table-identical to the old `dominates(browser-game-v1)` test for the profiles
  that existed before, now additionally admitting `browser-analysis-multipv-v2`).
  Canonical holds all eight capabilities; `browser-analysis-multipv-v2` and the retired
  `browser-analysis-v1` hold only `DISPLAY_OVERLAY`; `browser-game-v1` / jeffml hold
  none. g-mk1d adds `browser-game-v2`, which holds `DISPLAY_OVERLAY` under the third
  mode `REQUIRES_COMPARISON` (below) — the ALWAYS-only `display_upgrade_eligible`
  predicate the one-row seam uses still excludes it.
  A registry-load assertion states the grant precondition PROTOCOL-side rather than
  per-profile: **holding any active-required (non-`RETIREMENT_SURVIVING`) capability
  requires an internally consistent protocol.** So `browser-game-v1`,
  `browser-game-v2` and the retired `browser-analysis-v1` (all `browser-analyzer-v1`
  or no declared protocol) are capped at `DISPLAY_OVERLAY` whether active or retired,
  while the internally consistent `browser-analysis-multipv-v2` may receive
  read/reuse grants (the seam g-v21l needs); an unregistered
  `analyzer_protocol_version` fails closed. Lifecycle is deliberately NOT part of
  that rule — retirement is enforced at USE time by `has_capability`
  (`profile.active` or capability ∈ `RETIREMENT_SURVIVING`).
  A second load assertion pins the two overlay tables to agree: every `OVERLAY_MODE`
  entry names a registered profile and is a real `OverlayMode`, and **a non-`NEVER`
  mode holds if and only if the profile is granted `DISPLAY_OVERLAY`.** Either half
  alone is inert — a mode without the grant never runs (`has_capability` rejects the
  row first), and a grant without a non-`NEVER` entry never runs either
  (`overlay_mode` defaults an unlisted profile to `NEVER`) — and both read downstream
  as "this profile does not overlay", indistinguishable from an intended `NEVER`, so
  the drift is silent everywhere except at load. WHICH non-`NEVER` mode a profile
  takes stays a policy choice.
- **Exact-key model.** `SessionAnalysisMove` carries `fen_before` and `move_uci`.
  The FEN half is the durable `SessionMove.fen_before` (the same bytes browser-game
  wrote); the UCI half is server-derived from stored SAN via python-chess (SessionMove
  has no stored UCI). Evidence writes key on those exact values so a depth-21 row
  lands on the existing depth-17 `browser-game-v1` row. A derivation mismatch degrades
  gracefully to a near-duplicate `NEW_KEY` insert beside the old row, never
  corruption. Display helpers (`buildMainLineMoveDetails`, `projectExactBest`) prefer
  the wire fields with a legacy-only reconstruction fallback; the evidence driver has
  NO fallback (a null wire field skips the move).
- **Eval storage.** Evals are stored white-relative; mate is stored both as a finite
  mate-to-CP `*_eval` and a raw `*_eval_mate` count. The client recomputes `eval_delta`
  from the white-relative evals and clamps it at `>= 0` (never forwarding the worker's
  raw mover-relative delta), making each row self-consistent with `resolver-complete-v2`.
- **Mate fields never veto dominance.** Rule 5's completeness (superset) check strips
  the optional `played_eval_mate`/`best_eval_mate` before comparing, so a stronger
  CP-only row replaces a weaker row that merely stored a raw mate count. The exclusion
  is symmetric and global to Rule 5 (a CP-only canonical write also replaces a browser
  row that stored mate counts). Same-profile MERGE (Rule 2) is unchanged: there mate
  fields are genuinely additive and still participate in agreement/superset checks.
- **Network identity.** `browser-analysis-v1` pins the lite-single net
  `nn-9067e33176e8.nnue` (full SHA-256 …993f314d), distinct from canonical SF18's big
  net `nn-c288c895ea92.nnue`, so browser-analysis is network-incompatible with
  canonical and can never dominate it. `engine_build` is the SHA-256 of the compiled
  `stockfish-18-lite-single.wasm` artifact; the JS loader hash, npm package version
  (`stockfish@18.0.7`), and npm integrity are surrounding provenance only.
  `engine_version="18"` is the UCI `id name` token (npm `18.0.7` is provenance only).
- **Canonical replacement is guaranteed** for current canonical `resolver-complete-v2`
  writes replacing browser-analysis; a future canonical `move-complete-v1` would NOT
  replace a browser-analysis v2 row under the current superset check (cross-grain gap
  tracked in `g-6xc3`).
- **Not read-trusted.** Browser-analysis rows are never `/lookup` trusted hits or
  frontend trusted publications; read-time trust for stronger browser rows is the
  follow-up `g-v21l`.
- **Source ranking** is `precomputed < analysis < game < other`. Rows written by the
  evidence endpoint stamp `source="analysis"`. Its only functional effect is in
  `tree_eval.lookup_move_evals` tier 4 (the normalized untrusted transposition
  fallback): a normalized `analysis` row outranks a normalized `game` row there ONLY
  when no exact untrusted row exists (tier-3 exact rows win first). Position-grain
  resolution is unaffected (browser-analysis is non-authoritative and
  `resolve_trusted_positions` pre-filters to trusted rows). Accepted cross-user blast
  radius: a client-supplied `source="analysis"` row from one user's owned session can
  outrank a `source="game"` row in other users' tier-4 untrusted fallback — the same
  broad trust tier as existing browser-game rows, with stricter full-PV legality
  validation, never crossing into trusted paths.
- **Endpoint.** `POST /api/session/{session_id}/analysis-evidence` is session-scoped,
  owner-only, gated on `_should_run_session_move_evidence` (hidden/abandoned drills
  rejected with `session_not_evidence_eligible`), and guarded by exact mainline
  membership. SAN is server-derived from validated UCI and never trusted from the
  client; the backend stamps all profile/authority/source identity. FEN, move,
  best-move, and full-PV legality are validated (stricter than the browser-game upload,
  so it may drop otherwise-uploadable rows as a missed upgrade). The response is one
  entry per submitted row in request order, including `duplicate_request_key` handling.
  Owned-session client evals are accepted unverified (matching browser-game); server
  eval re-verification is out of scope. Evidence-writing surfaces are the saved-game
  `GameAnalysisPage`, `HistoryPage`, and `BlundersPage` boards; `DrillAnalysisPage` and
  ephemeral boards never write.
- **Reuse layer (no second worker).** The evidence layer (`src/services/analysisEvidence.ts`)
  owns NO Stockfish worker and runs NO extra search. `stockfishWorker.ts` builds an
  immutable completed-root snapshot atomically at its `bestmove` boundary (associating
  every info line with the request id, accumulating PV-bearing slots by one-based
  multipv index); the snapshot is deep-frozen on the MAIN THREAD in
  `useStockfishEngine` (a structured-clone across `postMessage` strips a worker-side
  freeze) and both the UI and the reuse layer observe that one frozen value. On each
  visible-search completion `AnalysisBoard` feeds the snapshot to
  `considerCompletedSearch` with the current next-mainline-move context read via a ref,
  so a stale search that settles after navigation is ignored (its `fen` no longer
  matches the current `fenBefore`). The layer's eligibility gate requires a saved
  session, `showEngineArrows`, the mainline (not a variation), exact non-null wire
  `fen_before`/`move_uci`, an unrestricted depth-21 MultiPV-3 shape, `min(3, legalMoves)`
  dense depth-21 slots, and the played move present as `pv[0]` of a complete line;
  submission is deduped by a content signature over the evidence-bearing snapshot/row
  with disjoint in-flight and terminal sets (a network failure clears in-flight and may
  retry; any HTTP 200 is terminal; a session change clears both). With visible engine
  analysis disabled, no analysis-board evidence computation or submission occurs.
  Read-time skip logic for stronger browser rows is deferred to `g-v21l`.

**Per-device in-game evidence (g-mk1d).** In-game browser evidence used to be a
single undifferentiated tier: every device searched to depth 17 and every uploaded
row carried all-`None` engine metadata, so the first row for a `(fen_before,
move_uci)` key won regardless of what a later, stronger run found. g-mk1d makes the
in-game producer honest about what it actually ran, and makes browser rows
comparable *within* the browser tier — without changing the authority barrier.

- **Per-device depth selector (`src/workers/deviceAnalysisTier.ts`).**
  `computeDeviceAnalysisDepth` picks a depth from deterministic `navigator` signals
  (`hardwareConcurrency`, and Chromium's `deviceMemory` treated as UNKNOWN rather
  than "small" when absent, so non-Chromium devices are not pinned to the floor
  forever). No timing probe: a benchmark would make provenance a function of
  measurement noise. `BASELINE_DEPTH = 17` is a FLOOR — any missing signal, unknown
  browser, or thrown lookup returns it, so the change can only be neutral-or-better.
  `sessionAnalysisDepth()` memoizes the result for the whole page session; that
  homogeneity is load-bearing, because every row a session uploads then claims the
  SAME `search_limit_value` and per-slot upload coalescing can never pair one
  upload's numbers with another's depth claim. Both in-game callers
  (`GameAnalysisCoordinator`, `useMoveAnalysis`) now send it as
  `AnalyzeMoveMessage.depth`; the analysis-board reuse path is unaffected.
- **`MAX_DEVICE_DEPTH` ships AT the baseline (parity), gated on a latency
  benchmark.** A depth ceiling does NOT bound latency — `go depth N` has no time
  limit — so raising it requires a per-move p95/p99 acceptance target measured on the
  WEAKEST device each tier admits, explicitly including the adversarial 8-core /
  `deviceMemory`-unavailable cell (mobile Safari) that the signal heuristic routes to
  the high tier. The ceiling raise and the wall-clock cap below are enabled together,
  under that one gate.
- **Shared per-move wall-clock cap, DORMANT at parity.** `analyzeMove` establishes ONE
  `MAX_ANALYSIS_MS` deadline covering the engine reset and all three sequential
  searches — not a per-search timer, which would let one move consume ~3x the budget.
  It ships `null` (disabled). That is deliberate, not timidity: a healthy depth-17
  analyze-move can legitimately run many seconds (g-f2mg recorded a single depth-14
  iteration at 8.7s), so ANY finite value small enough to bound live-play latency
  would truncate healthy searches and change their classifications. At depth 17,
  bounding latency IS the behavior change. The cap is a TOTAL-DURATION bound and is
  ORTHOGONAL to the ~8s inactivity watchdog (an inactivity window continuously reset
  by the 1s heartbeat); there is no ordering constraint between them, and a ratified
  `MAX_ANALYSIS_MS` may sit well above 8s. A reset that hits the deadline takes the
  FATAL path — terminate + recreate the engine and clear `resetAckQueue` — because the
  usual leave-in-queue absorber assumes the `readyok` is still coming; against a hung
  engine an orphaned placeholder would swallow the NEXT request's ack and deadlock
  every later reset. That move rejects scoped to its own id with no partial result.
- **The deadline needs a post-stop grace to actually BE a bound.** `stop` is a request
  to the nested engine, not a guarantee, so the deadline alone bounds only when the
  stop is *sent*. An engine that never answers it would leave the search pending
  forever — and worse than an ordinary hang, because `activeSearch` stays set and the
  unconditional liveness heartbeat keeps vouching for the request, so the coordinator's
  inactivity watchdog never trips either and the queue wedges with no bound anywhere.
  A `STOP_GRACE_MS` (2s) timer armed alongside the `stop` closes this: answered in
  time ⇒ the normal truncated-but-usable result; expired ⇒ the same fatal
  terminate + recreate path as the reset timeout, rejecting that move scoped to its
  own id. Every abandonment path (`terminate`, a mid-search cancel/error) clears both
  timers, since an orphaned grace timer would otherwise destroy a healthy engine
  mid-way through a LATER request. The grace is ONE PER MOVE, not one per search:
  the deadline and the grace are two fields of a single `AnalysisBudget` object
  threaded through the reset and all three searches, and the clock starts at the
  move's FIRST deadline `stop`, so later searches inherit what is LEFT of it. A
  per-search grace would let a move run `MAX_ANALYSIS_MS + 3x STOP_GRACE_MS` —
  every search entered after the deadline stops immediately, so all three would arm
  their own — reintroducing one level down the ~3x overshoot the shared deadline
  exists to prevent. The move's total wall-clock bound is therefore
  `MAX_ANALYSIS_MS + STOP_GRACE_MS`, which is what the latency gate measures against.
- **Provenance honesty.** A truncated search reports an explicit `capFired` /
  `stopReason`, never an inferred `reachedDepth < depth` (wrong in both directions: a
  stop can land just after `info depth N`, and a forced mate finishes below N with no
  cap — there the configured limit WAS honestly satisfied). Provenance survives only
  for a tuple that is BOTH untruncated AND unrewritten: `reconcileTrustedBest` clears
  it on both of its rewrite branches, since a canonically corrected tuple's best-ness
  came from the position grain, not from a stronger search. Cache-sourced results
  (someone else's search) carry none. A cleared claim simply falls back to
  `browser-game-v1` with no strength claim.
- **`browser-game-v2` — the first DECLARED-DYNAMIC profile.** Its FIXED half
  (`engine_name`, no small net, `multipv=1`, `browser-analyzer-v1`, manifest digest) is
  exact-equality verified and always server-stamped; its DYNAMIC half
  (`engine_version`, `engine_build`, `eval_file_id`, `search_limit_type`,
  `search_limit_value`, `threads`, `hash_mb`) is per-field validated by
  `DYNAMIC_FIELD_VALIDATORS` instead — the `dynamic_fields` seam g-reuse-d21-search
  reserved. No client ever sends a profile id. NON-authoritative,
  `replacement_eligible`, with NO `dominates` edge. These are self-reported
  diagnostics by design: forging them can only reorder non-authoritative rows within
  the browser tier, never cross the authority barrier, earn a capability, or touch
  `position_analysis`.
- **Identity constants are artifact-derived, not hand-copied.**
  `src/workers/browserEngineIdentity.ts` is GENERATED (`npm run gen:engine-identity`)
  by hashing the actually-bundled `stockfish-18-lite-single.wasm`; because that build
  EMBEDS its net, the WASM hash pins the network transitively, and
  `scripts/browser-engine-manifest.json` records WHICH network (there is no standalone
  `.nnue` to hash). A CI test re-hashes the artifact and cross-checks both values
  against the backend registry, which loads the same binary. This closes REPOSITORY
  drift — a stale constant that no longer describes the shipped artifact would
  identity-verify fine and silently mislabel every uploaded row. It cannot stop a
  modified client self-reporting a valid-looking identity; see the threat model above.
- **Measured strength (`compare_row_strength`, D4 steps 4-5).** Reads the ROW's stored
  identity columns, not the profile's (a dynamic profile's registry values are all
  `None`). Step 4 is a semantic compatibility guard — same engine, net(s), MultiPV,
  analyzer protocol, and search-limit TYPE, plus compatible builds; step 5 compares
  `engine_version` (leading int) then `search_limit_value`. Differing self-reported
  builds are INCOMPARABLE: `BUILD_EQUIVALENCE` is keyed by profile id and meaningless
  when every device shares one id. An all-`None` `browser-game-v1` row is UNKNOWN
  strength, never weak, so it is INCOMPARABLE to every v2 row and a deeper v2 search
  can NEVER reclaim it by depth. `browser-game-v2` vs `browser-analysis-multipv-v2` is
  INCOMPARABLE (MultiPV 1 vs 3); the multipv-v2 → browser-analysis-v1 corrective edge
  is untouched.
- **Rule 2a — same-profile replacement.** For a declared-dynamic profile, two rows at
  one key are NOT interchangeable. Stronger comparable ⇒ `strength_replace` (guarded
  by the same completeness check as Rule 5, else `incoming_less_complete_keep`);
  weaker ⇒ `strength_weaker_keep`; incomparable ⇒ `strength_incomparable_keep`;
  identical provenance ⇒ the historical idempotent/merge path. There is NO
  cross-provenance MERGE: a merged row carries exactly ONE provenance tuple, so
  unioning evidence produced under different settings would attribute one device's
  numbers to another device's identity — equal-strength-but-different-provenance rows
  are therefore idempotent, not merged. `_dedupe_batch` resolves same-key dynamic rows
  by simulating the decision in BOTH orders, so the batch survivor is
  permutation-independent and matches what sequential writes would produce; no
  agreeing strict winner ⇒ `duplicate_conflict`.
- **Per-row provenance wire, fully permissive.** Provenance rides INSIDE each move
  payload, not at request level: the deferred scheduler coalesces per `(move_number,
  color)` slot with last-write-wins, so a request-level value would have no defined
  merge rule and could mis-stamp rows — per-row needs ZERO scheduler change.
  `SessionMoveInput.provenance` is typed `Any` on purpose; a constrained shape (even
  `dict[str, object]`) makes Pydantic reject non-object JSON during request parsing and
  422 the ENTIRE batch. All checking — starting with "is this even a mapping?" — moves
  into `validate_browser_provenance`, run per row, with strict numeric typing that
  rejects JSON booleans (`isinstance(True, int)` would otherwise accept `true` as a
  depth-1 claim) and per-type `search_limit_value` bounds. VALID ⇒ v2 with the seven
  dynamic columns; ABSENT ⇒ v1 as before; MALFORMED ⇒ the cache row is DROPPED, never
  laundered into a silent v1 downgrade, while the `session_moves` row still persists
  (with NULL provenance) and the batch stays HTTP 200.
- **Two-grain observability.** The `analysis_cache_write` summary log — emitted once
  per COALESCED SCHEDULER RUN per session, NOT per request — carries row-weighted
  `provenance_valid` / `provenance_absent` / `provenance_malformed` (the operational
  health signal) plus a length-independent per-run `session_provenance` bit
  (`v2` | `legacy` | `mixed_malformed` | `none`, malformed dominating). The
  session-grain adoption measurement reads only the latter, rolled up by
  `app/browser_provenance_metrics.session_v2_adoption` to ONE verdict per DISTINCT
  session (its latest FINAL run) — never the row counts, which a few long
  legacy-client games would dominate, and never per-run counts, which would re-weight
  by upload frequency. That rollup was built to be the `browser-game-v1` retirement
  criterion; it never became one — the retirement below needed no adoption gate at all,
  so this measures the fleet without authorizing anything. The Railway log adapter
  that read this bit for the gate was DELETED with the gate (g-bgv1-report-fate); its
  hard part — how completely Railway logs can be retrieved, which loss modes are
  detectable and which two are irreducibly fail-open — is preserved in
  [`docs/railway-log-query-completeness.md`](docs/railway-log-query-completeness.md)
  and applies to any future log-based measurement. Finality comes from a
  dedicated `session_final` field that OR-folds the client's `terminal_action` through
  the scheduler entry, NOT from `run_opportunity`: the revert upload also sets
  `recompute_opportunity=True`, and a client predating g-y90g sets it on every mid-game
  upload, so reusing it would score abandoned and reverted sessions as complete. The
  pre-existing `final`/`kind` fields on the same log line keep their `run_opportunity`
  meaning — they are g-dckw's LATENCY cohort, and redefining them would silently
  re-cohort a live metric.
  Sessions with no final run are excluded from the ratio but COUNTED into
  `sessions_without_final`, because that exclusion is not neutral: a client too old to
  send `terminal_action` is also too old to send provenance, so silently dropping
  those sessions inflates the adoption fraction toward 1.0 exactly when the legacy
  fleet is largest. A large `sessions_without_final` invalidates the fraction.
- **`browser-game-v1` is RETIRED for writes (`active=False`, g-bgv1-cutover).** v2
  shipped purely additively first; v1 was then flipped unconditionally, with NO adoption
  gate. A gate looked necessary by analogy with `browser-analysis-v1`, whose ENDPOINT
  producer discriminator fails a stale client closed — but v1 is written from a
  different path, `upsert_session_moves` → `_upsert_analysis_cache`, which has no
  producer discriminator at all. There an inactive declared profile is refused by the
  batch writer (`declared_profile_inactive` ⇒ `INACTIVE_PROFILE_KEEP`) WITHOUT raising:
  a legacy client's upload still returns 200, its `session_moves` rows still persist,
  and its own eval/classification display is unaffected. Nothing fails closed, so
  retirement was never gated on fleet adoption. The cost is a deliberate trade, not
  free: a refused row carried a real played eval that `tree_eval` tiers 3-4 could have
  shown, but an all-`None` v1 row is UNKNOWN strength and therefore INCOMPARABLE to
  every v2 row (D7.1), so every one it wrote would occupy its key against every future
  v2 upload forever. A blank card today beats a key no browser game can improve.
- **The stranded v1 rows are KEPT, not purged.** Rows already stored keep
  `identity_verified` (the manifest digest excludes `active`/`dominates`), and the
  `tree_eval` tier 3-4 fallback is trust-based rather than active-based — so retirement
  does not stop stored v1 evals from being read. Nor are they permanently unimprovable:
  `browser-analysis-multipv-v2` is active and DOES dominate v1, so an authoritative run
  correctively replaces such a row in place, at its own key, whenever it reaches the
  position. A purge would therefore delete live evidence to free keys that are already
  reclaimable — D7.2's opportunistic-only stance holds for these rows. Identity backfill
  stays rejected: their provenance was never recorded, so stamping one would fabricate
  it.
- **REQUIRES_COMPARISON overlay.** `browser-game-v2` holds `DISPLAY_OVERLAY` under a
  third `OVERLAY_MODE`: it re-labels a played move only when provably STRONGER than a
  LIVE operand, via the SAME `compare_row_strength` that governs storage — so what the
  writer calls stronger and what the reader will display cannot drift apart. The
  one-row Part-B seam supplies no operand and therefore never overlays such a row.
  Part C (`GET /{session_id}/analysis`) builds the operand from
  `session_moves.browser_provenance`, a nullable JSON `Text` column holding only the
  DYNAMIC subset — the fixed half is reconstructed from the server registry at read
  time, so a hand-edited row cannot claim an identity it did not earn. It is written
  through the SAME validator as the cache write and refuses the same rows: absent,
  malformed, AND synthetic-terminal all persist NULL. The synthetic case matters
  because the two gates are separate code paths — a fabricated game-ending eval that
  the cache correctly declines would otherwise re-enter here as the overlay's
  supposedly-live search operand and suppress a genuine stored row. It is the only
  operand available at GET for a saved game (request-side provenance exists only at
  upload time; a reloaded page holds no client-side analysis — precisely when a
  stronger cross-user row matters most). NULL/legacy/tampered ⇒ no operand ⇒ no
  overlay, and the player keeps their own label. ALWAYS-mode profiles (canonical,
  browser-analysis-v1/-multipv-v2) ignore the operand entirely — exact parity.

**Position-truth foundation (g-position-analysis).** `analysis_cache` conflates two
grains on one row — *position* facts (the position's best move / best line / best
eval, properties of the normalized FEN) and *move* evidence (a played move's eval /
loss / classification, keyed by `(fen_before, move_uci)`). Because mixed-provenance
sibling rows for different played moves at the same FEN could disagree about a
position's best move (a canonical row and a browser row carrying opposite
`best_move_uci`), a consumer reading position facts off an untrusted move row could
surface a wrong "best move" for the whole position. The split is now implemented:
position truth lives in its own normalized-FEN-keyed storage, and `tree_eval.py`, the
session drill-review export, and `/api/analysis/lookup` all read it (with a legacy-v2
projection fallback during migration). The two storage tables are:
- `position_analysis` — one trusted winner per `normalized_fen` (`best_move_uci` NOT
  NULL; first-class `best_eval` / `best_eval_mate`; `best_line_uci`; full profile
  identity + `evidence_contract_id`; representative `fen` and `source_cache_id` as
  provenance only; `updated_at` since winners are replaced over time).
  `UniqueConstraint("normalized_fen")`. Deliberately distinct from the full-FEN-keyed
  `position_analysis: dict[str, PositionAnalysis]` session-wire field — a storage row
  is never returned as the session map directly.
- `position_analysis_conflicts` — append-only audit sink (indexed on `normalized_fen`,
  not unique) recording the disagreeing candidate cache rows and per-axis disagreement
  (`best_move`, `pv`, `best_eval`, `best_eval_mate`) plus a `policy_reason` when a
  position has no clean winner.

See **§14.6** for the full grain-ownership, trust-contract, write-policy, eval-loss,
legacy-v2-transition, and migration model.

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
- `confidence` and `sample_size` quantify how sparse the evidence behind a score is. They remain backend/API metrics but are **no longer surfaced in the opening UI** (`g-9dph` removed the Confidence tile from the cards and the `/openings` legend); the API response still carries them
- Branch fields (strongest/weakest/underexposed) are persisted from the same shared calculation and read directly by the drill-down (no per-request recompute)
- Batches are replaced atomically: a new batch is computed, then the cursor is updated to point to it; old batches are pruned
- `registry_fingerprint` includes the score-model, phase-divider, and quality-curve versions (`SCORE_MODEL_VERSION`, `DIVIDER_VERSION`, `QUALITY_VERSION`, `TAU_WC`, `TAU_CP`), so any model/divider/curve change invalidates all prior snapshots on the next read.
- `inputs_fingerprint` is a **cheap raw-input freshness digest** (`opening_score_raw_inputs_fingerprint`): it hashes a canonical, order-independent projection of exactly the raw DB rows the evidence overlay consumes — session_moves; the cache-fallback's **two trusted grains** (the exact `(fen_before, move_uci)` `analysis_cache` move rows plus their move-trust columns, AND the trusted position sources the fallback pairs them with: `position_analysis` storage winners and the legacy `analysis_cache` position-fallback rows at the candidate **normalized** FENs, including their grouping key `normalized_fen_before`); ghost-target blunders/positions; and blunder_reviews — **without any python-chess board replay or overlay build**, folded together with `registry_fingerprint` and an explicit `OPENING_EVIDENCE_INPUTS_VERSION`. The latter is bumped on any evidence-derivation semantic change a raw-row hash is blind to (e.g. `PASS_THRESHOLD`, quality-source precedence, FEN normalization, phase-filter application, or the digest's own projection/filters). The overlay is a pure deterministic function of these inputs, so a matching digest provably implies identical scores.
- `recompute_opening_scores_if_needed()` is the single recompute-decision function, run on the scheduler's serialized worker. It computes the raw-input digest first (cheap SQL) and serves the cached batch on the fast path **without building the expensive overlay** when nothing changed; the overlay (per-session board reconstruction + Lichess phase divider) is rebuilt only on a cache miss, registry drift, stale branch keys, a digest change, or decay-staleness. Reads are stale-while-revalidate: a **warm** reader (batch present) calls `request_recompute()` to schedule a coalesced background convergence and serves the cached batch immediately — never blocking; only a **cold** reader (no batch yet) blocks on `refresh_now()` for the one-time initial compute.
- **The overlay rebuild itself is incremental (g-25mp).** An in-process, per-`session_id` bounded LRU memoizes the per-session board REPLAY (reconstruct + Lichess divider + opening-premove extraction), keyed by a session content-hash folded with `DIVIDER_VERSION` + `OPENING_EVIDENCE_INPUTS_VERSION`, so a rebuild replays only new/changed/unseen sessions; unchanged finished sessions and previously-excluded broken sessions (whose exclusion warning is then logged once per content, not once per rebuild) load from cache. Move quality is recomputed on copy-out rather than cached, so a `QUALITY_VERSION`/`TAU_WC`/`TAU_CP` change needs no cache invalidation. **Cold-start tradeoff:** the cache is in-process (matching the single-worker deployment below), so a restart empties it and the first post-restart rebuild pays the full replay once per session, then is incremental; a persisted table (deferred) would also cover cold bootstrap, and the key is storage-agnostic for that later swap. **Budget caveat:** the LRU is bounded by cached move-row count (`_SESSION_CACHE_MAX_ROWS`), not session count; if a single `(user, player_color)` working set exceeds the budget, entries evict mid-build and that user re-replays wholesale each rebuild — correctness is unaffected (eviction only forces re-derivation), only the "replay just the new session" performance claim degrades, and evictions are logged.
- **Batch `computed_at` is an evidence-read upper bound.** `recompute_opening_scores` samples `computed_at` (via the `_utcnow()` seam) **after** the fingerprint and overlay evidence reads, not before, so the timestamp bounds — from above — everything the batch reflects. The async opening-baseline capture depends on this: a batch dated strictly before a session's `started_at` cannot contain any of that session's evidence.

**In-process schedulers (single-process, one-worker, best-effort — no durable outbox).** Three daemon-thread schedulers defer expensive work off request paths; all assume the single-worker/single-replica deployment and degrade to best-effort no-ops on failure. `OpeningScoreScheduler` (keyed by `(user_id, player_color)`, debounced) runs `recompute_opening_scores_if_needed`; `SessionEvidenceScheduler` (keyed by `session_id`, debounced) runs the `/moves` graph/opportunity/analysis-cache side effects. `OpeningBaselineScheduler` is the third: **session-keyed, no debounce, best-effort, one worker**. It exists to move `GameSession.opening_score_baseline` capture off the `/start` hot path (proving the cached batch fresh costs an O(all-evidence) digest — g-mxeo). `/api/game/start` and `/api/drills/start` enqueue a job and return 201 immediately; the worker calls `run_baseline_snapshot_job`, which persists a baseline only when the pre-session cached batch is **provably fresh AND dated strictly before `started_at`** (freshness alone is insufficient because the batch may already fold in this session's incrementally-uploaded evidence). A defense-in-depth conditional UPDATE re-checks `status='active'`, the captured `user_id`/`player_color`, a NULL baseline, and the absence of any session-scoped `session_moves`/`blunder_reviews`/`blunders` — atomic with the write. Any race, a dropped job on hard kill, or a stale/mis-routed enqueue degrades to **NULL / no-delta**, never a wrong (post-session) baseline. It is a **leaf** worker (reads cache state, writes the session row, enqueues no recompute), so shutdown drains it first.

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

#### 5.7.5 `opening_position_edges` (Observed Tree Edges)

Sibling read model to `opening_position_scores`, persisted under the **same** `opening_score_batches` generation but keyed by `(batch_id, parent_fen, child_fen)` — mirroring the `EvidenceOverlay` edge key. It supplies the **observed move edges** (structural shape plus the `traversal_count` / `live_attempts` / `live_passes` counters) the `/api/openings/tree` builder needs, so the tree read path **no longer rebuilds `overlay_evidence` on the request thread** (that rebuild — a full replay of every session board line through the Lichess phase divider — previously dominated warm-read latency, scaling with total game count rather than visible nodes).

```sql
CREATE TABLE opening_position_edges (
    id BIGSERIAL PRIMARY KEY,
    batch_id BIGINT NOT NULL REFERENCES opening_score_batches(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    player_color VARCHAR(5) NOT NULL,      -- 'white' or 'black'
    parent_fen TEXT NOT NULL,              -- Normalized 4-field FEN — observed edge parent
    child_fen TEXT NOT NULL,               -- Normalized 4-field FEN — observed edge child
    uci TEXT NOT NULL,                     -- The move connecting parent_fen -> child_fen
    traversal_count INTEGER NOT NULL DEFAULT 0,  -- Times the edge was observed (encounter_count)
    live_attempts INTEGER NOT NULL DEFAULT 0,    -- User-choice live attempts on this edge
    live_passes INTEGER NOT NULL DEFAULT 0,      -- Live passes (drives is_prepared)
    live_fails INTEGER NOT NULL DEFAULT 0,       -- EdgeEvidence parity; not read by the tree
    computed_at TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    UNIQUE (batch_id, parent_fen, child_fen)
);

CREATE INDEX idx_opening_position_edges_batch_parent ON opening_position_edges(batch_id, parent_fen);
CREATE INDEX idx_opening_position_edges_user_color ON opening_position_edges(user_id, player_color);
```

**Read-model contract:**
- **Bounded per-parent reads.** The tree builder loads observed edges lazily via `opening_cache.lookup_observed_edges_for_parent(db, batch_id, parent_fen)` — one indexed point query per parent it actually visits (the visible line positions ∪ their rendered frontier children), so a warm read is proportional to rendered nodes, not to total session history. The reconstructed `EdgeEvidence` carries `quality_sum=0.0, quality_count=0` because the tree never reads quality.
- **Quality columns omitted.** `quality_sum` / `quality_count` are deliberately not persisted: the tree never reads them, and the scorer builds its own in-memory overlay during recompute. If the scorer is ever changed to read scores from this table, the two quality columns must be added.
- **Same write transaction.** One `OpeningPositionEdge` per `overlay.edges` value is written in the same transaction as the named and direct position rows of the batch.
- **Generation retention.** `batch_id` cascades on delete from `opening_score_batches` exactly like `opening_position_scores`, so `prune_old_opening_score_batches` removes edge rows through the same keep=2 generation-retention path.
- **Schema-version self-healing.** The set of persisted read-model tables is versioned by `OPENING_SCORE_CACHE_SCHEMA_VERSION` (`edges-v1`), folded into the **registry fingerprint**. A batch built before this read model existed therefore reports registry drift and recomputes once per `(user, color)` on first read, materializing its edge rows. On the `/api/openings/tree` path this drift forces a **blocking one-time bootstrap** (`ensure_tree_cache` → `refresh_now`) rather than a background revalidate, because serving a registry-stale (edgeless) batch would render a book-only tree that silently hides the user's observed moves; warm-fresh batches serve immediately while a background recompute revalidates evidence/decay. The families/score read path keeps its existing background revalidate (stale scores are tolerable there).

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

   severity      = log1p(min(max(eval_loss_cp, 0), 1000) / 50)   -- logarithmic; 200cp ≈ 1.61, 50cp ≈ 0.69

   distance_weight = exp(-0.35 * depth)               -- exponential decay; depth=1 → 0.70, depth=5 → 0.17

   score = urgency × severity × distance_weight
   ```
   `eval_loss_cp` is stored uncapped; it is normalized 0..1000 (`centipawn_loss`) at decision (Ghost/SRS severity) and display (/stats, Blunder Library) time — `CENTIPAWN_LOSS_CAP_CP = 1000` (decisive-mistake ceiling). Only the **severity** factor saturates; urgency, distance, reach, and opening-family weight still fully differentiate two equally-severe blunders.
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

**Performance Target:** Ghost-path lookup < 100ms for typical Ghost Move Libraries (< 10,000 positions). The 5-move depth cap keeps the search space small; full fallback (including Maia3 API call) should target sub-second p95 in MVP. The Maia3 remote API adds ~200–500ms network latency per engine fallback call. The backend must release the request DB transaction before waiting on the remote Maia fallback, so DNS/network stalls do not hold a database connection or read transaction. Slow-path logs are thresholded around ghost search, Maia fallback, analytics capture, and `/api/analysis/lookup` to distinguish DB time from remote-engine and request-lifecycle stalls.

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

severity        = log1p(min(max(eval_loss_cp, 0), 1000) / 50)   -- 50cp → 0.69, 100cp → 1.10, 200cp → 1.61; saturates at 1000cp → 3.04

distance_weight = exp(-0.35 × depth)                -- depth=1 → 0.70, depth=3 → 0.35, depth=5 → 0.17

score = urgency × severity × distance_weight
```

`eval_loss_cp` is stored RAW/uncapped; severity normalizes it 0..1000 (`centipawn_loss`, `CENTIPAWN_LOSS_CAP_CP = 1000`) so a mate pseudo-cp (~10000) and a real −1200 blunder saturate to the same severity. Only severity saturates — urgency, distance, reach, and opening-family weight still differentiate two equally-severe blunders.

**Constants:**
| Constant | Value | Purpose |
|----------|-------|---------|
| `BASE_INTERVAL` | 4 hours | Minimum review interval (first attempt) |
| `BACKOFF_FACTOR` | 2.0 | Exponential interval growth per pass |
| `MAX_INTERVAL` | 4320 hours (180 days) | Interval cap |
| `STEERING_RADIUS` | 5 moves | Max depth for ghost path traversal |
| `SEVERITY_NORMALIZER_CP` | 50 cp | Denominator in log1p severity formula |
| `CENTIPAWN_LOSS_CAP_CP` | 1000 cp | Decisive-mistake ceiling: floor 0 + cap on the per-move CPL / severity input (`centipawn_loss`), applied at read/decision |
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

**Severity examples (log1p scale, saturating at the 1000cp decisive-mistake ceiling):**
| eval_loss_cp | severity = log1p(min(max(cp, 0), 1000)/50) |
|--------------|--------------------------------------------|
| ≤0cp (floored) | 0.00                                     |
| 50cp         | 0.69                                       |
| 100cp        | 1.10                                       |
| 200cp        | 1.61                                       |
| 400cp        | 2.20                                       |
| 1000cp (cap) | 3.04                                       |
| 10000cp (mate, saturated) | 3.04                          |

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

**Dual-search protocol:** The delta is computed from two independent **post-move** searches (played-move position and best-move position). Using post-move positions for both avoids depth-mismatch inflation that occurs when comparing pre-move minimax against post-move searches.

**Search count per move:** Up to **three** depth-17 searches run, not two — the two compared post-move searches are preceded by a root search that identifies the best move:

1. **Root search** on the pre-move position → yields `bestMove`.
2. **Post-played search** (`position fen <fen> moves <playedMove>`) → E_user.
3. **Post-best search** (`position fen <fen> moves <bestMove>`) → E_best. Skipped when `playedMove === bestMove`, since search 2 already evaluated that position.

Searches 2 and 3 are each skipped when the resulting position is terminal (checkmate/stalemate), where the score is assigned deterministically instead of searched. A non-terminal move where the player did not find the best move therefore costs the full three searches.

**Implementation (JavaScript):**
```javascript
// Send to Stockfish worker (on uciok)
worker.postMessage('setoption name Hash value 128');
worker.postMessage('setoption name MultiPV value 1');

// Per-move analysis: up to three searches.
// 1. Root search on the pre-move position to find bestMove.
worker.postMessage(`position fen ${fen}`);
worker.postMessage('go depth 17');
// ... after `bestmove <bestMove>` is received:

// 2. Post-played position (skipped if terminal — score assigned directly).
worker.postMessage(`position fen ${fen} moves ${playedMove}`);
worker.postMessage('go depth 17');

// 3. Post-best position — only when playedMove !== bestMove
//    (and likewise skipped if that position is terminal).
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

**Analysis cache race:** On each move the coordinator dispatches to the analysis worker and, in parallel, batches a `POST /api/analysis/lookup` query (`src/utils/api.ts`). Two consumers read the result on separate paths:

- **Published path** (the `AnalysisResult` consumed by recording / SRS / blunder display): a cache hit pre-empts the worker only when it passes the grain-specific trust gate — `isTrustedMoveHit` for played-move facts, `isTrustedPositionHit` for best-move / PV. Untrusted rows are treated as misses so the worker produces the authoritative result. Mere presence of `classification` / `eval_delta` no longer short-circuits: a post-split move row can be CP-trusted yet carry no publishable `eval_delta`, and an untrusted browser row must never drive the verdict.
- **Drill-truth side channel** (`waitForDrillGrade`, g-position-analysis Phase 6): a separate, drill-only channel that records trusted exact-best *position* truth (`best_move_uci` plus the backend-derived `position_eval_loss_cp`) *beside* the published result. Strictness-0 grades by comparing the played move to the trusted `best_move_uci` with no eval needed; strictness > 0 grades from `position_eval_loss_cp` when present (both grains trusted, pure-CP, equal search strength), else falls back to the worker. Every terminal cache path settles the channel (to truth or `null`) so a drill grade waiter can never hang. See §14.6.

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

    -- Cached whole-game accuracy (Release A, g-accuracy-schema). Maintained by the
    -- serving write hooks; see §7.3.1. NULL means "not (yet) computed / not eligible".
    player_accuracy INTEGER,                -- 0..100 or NULL (a computed-None is stored as NULL)
    player_accuracy_algo_version SMALLINT,  -- ACCURACY_ALGO_VERSION stamped when the session was scored

    CONSTRAINT valid_player_color CHECK (player_color IN ('white', 'black')),
    CONSTRAINT valid_session_mode CHECK (session_mode IN ('normal', 'drill')),
    CONSTRAINT valid_drill_state CHECK (drill_state IS NULL OR drill_state IN ('active', 'root_reached', 'failed', 'abandoned', 'converted')),
    -- NOT VALID in Release A: enforced for every new/updated row but existing rows
    -- are NOT scanned (no validation lock). Release B validates it once clean.
    CONSTRAINT ck_game_sessions_player_accuracy CHECK (player_accuracy IS NULL OR (player_accuracy >= 0 AND player_accuracy <= 100))
);

CREATE INDEX idx_game_sessions_user ON game_sessions(user_id);
CREATE INDEX idx_game_sessions_status ON game_sessions(status);
CREATE INDEX idx_game_sessions_user_started ON game_sessions(user_id, started_at);
CREATE INDEX idx_game_sessions_drill_state ON game_sessions(drill_state);
```

**`is_rated` flag:** Passed by the client in `POST /api/game/end`. When `true` and the result is `checkmate_win`, `checkmate_loss`, `resign`, or `draw`, the server computes a rating change and appends a `rating_history` row. Results of `abandon` never affect rating regardless of `is_rated`. The flag defaults to `true`; clients set it to `false` for practice games.

### 7.3.1 Cached session accuracy (Release A)

Release A adds two `game_sessions` columns — `player_accuracy` (0–100 or NULL) and `player_accuracy_algo_version` — and the serving write hooks that maintain them, **without** any backfill of pre-existing rows and **without** switching any read onto the cache. The `game-review` stats and history surfaces still compute accuracy live from `session_moves`; Release B owns the backfill, the `NOT VALID` CHECK validation, and the cache-only read switch. See §5.5 for the parallel `rating_history` durable-head work shipped alongside.

**Frozen algorithm.** The scoring surface (`expected_total_moves_from_pgn`, `win_percent_from_cp`, `accuracy_from_win_percents`, `AccuracyMove`, `compute_game_accuracy`) is frozen in `app/accuracy_v1.py` and pinned to `python-chess`; `app/accuracy.py` re-exports it and defines `ACCURACY_ALGO_VERSION = 1`. Freezing v1 lets a future v2 coexist and lets a cached value be interpreted against the exact algorithm that produced it.

**Cached population (exact).** A session is scored **iff**

```
status == "ended" AND (session_mode == "normal" OR drill_state == "converted")
```

evaluated by the shared `is_visible_game_session` / `visible_session_filter` predicate from `app/session_contracts.py` (never re-spelled). Active sessions and ended **failed/abandoned** drills are left wholly unstamped — both columns stay NULL and invisible to both consumers.

**`recompute_session_accuracy(db, session)`** is the bounded write hook. It reads the **in-memory** session (so it sees the caller's not-yet-committed terminal status/PGN), and:

- returns before touching either column unless the in-memory session is ended-and-visible — an ineligible session costs no move query and no PGN parse;
- issues exactly one `session_moves` eval query for this session, ordered by move number with white before black (the ply order `compute_game_accuracy` requires);
- **stamps a computed NULL:** it assigns `player_accuracy` even when the computation legitimately yields `None` (e.g. insufficient resolved evals) and **always** sets `player_accuracy_algo_version` once an eligible computation runs, so an eligible session is never left half-stamped. A NULL `player_accuracy` with a **non-NULL** version means "scored, but no accuracy was derivable"; both NULL means "never scored";
- never commits or flushes — the caller owns the transaction, so the dirty accuracy assignment drains in the caller's own pre-cursor flush (keeping the cursor bump last, §7.4).

**Dual-hook lifecycle.** The normal terminal flow awaits the full `/moves` upload **before** `POST /api/game/end`, so the game-end hook computes the **first** terminal value. A later `POST /api/session/{id}/moves` can add, change, or clear evaluations after end, so it **recomputes** (self-healing: a game-end value computed before the last evals arrived is corrected on the next post-end upload). Both hooks call `recompute_session_accuracy` while holding that session's `FOR NO KEY UPDATE` lock (§7.4). Non-converted drills remain unstamped and invisible to both consumers.

#### 7.3.1.1 Backfill, repair, and fail-closed activation (Release B core)

Alembic revision `20260719_01` (`down_revision = 20260718_01`) is Release B's correctness state machine. It runs three phases and then two assertions that must both pass before any cache-only read may serve.

**Phases.**

1. **validate** — PostgreSQL only: `ALTER TABLE game_sessions VALIDATE CONSTRAINT ck_game_sessions_player_accuracy`, the CHECK Release A created `NOT VALID`. It takes `SHARE UPDATE EXCLUSIVE`, which does not conflict with the write hooks' `ROW EXCLUSIVE` / `FOR NO KEY UPDATE`, so it blocks DDL and autovacuum but not live writers. `set_config(..., true)` is `SET LOCAL` — **transaction**-scoped, and `alembic/env.py` opens one transaction around the whole run — so the revision explicitly re-arms `lock_timeout` immediately afterwards rather than letting every later row lock inherit the DDL wait.
2. **backfill** — keyset paging (never `OFFSET`: updated rows leave the stale predicate) over `status = 'ended' AND (session_mode = 'normal' OR drill_state = 'converted') AND (player_accuracy_algo_version IS NULL OR < 1)`. **Never filters on `ended_at`** — it is nullable, and a malformed ended-visible row must still be backfilled. Each batch loads its plies in one ordered query, validates the ply-coordinate grid **before** building `AccuracyMove`s, and issues **one** guarded server statement with explicitly typed arrays (`unnest(CAST(:ids AS uuid[]), CAST(:accuracies AS integer[]))`) whose `RETURNING` names exactly the rows the stale-version guard admitted. Rows an A hook stamped first are absent and logged, not an error.
3. **repair** — the rows the backfill's predicate *skips*: already stamped version 1 by the **unguarded** Release-A hook, so they may serve a value computed over a broken grid. Each pass materializes the candidate set once into a temp table, then per candidate: **lock, re-read with the session-scoped detector in a fresh statement, then act**. A lock-free set-based `UPDATE ... WHERE id IN (<detector>)` is unsafe — the subplan is re-evaluated under the statement's original snapshot, so it would overwrite a hook's freshly-corrected value with NULL, and the stale-version guard cannot catch it because both values are version 1. **The hook always wins.** Repaired rows stay version 1: v1 attempted the computation and its input contract rejected the inputs.

**Ply-coordinate detector — three definitions that must agree.** `app/accuracy_rows_v1.ply_coordinates_intact` (frozen Python), `PLY_DETECTOR_SQL` (set-wide), and `PLY_DETECTOR_ONE_{PG,SQLITE}` (session-scoped). `session_moves` has coordinate indexes but **no stored defect marker**, and a `row_number()` window is not an indexable predicate — so every set-wide execution is a full scan of the whole relation regardless of how many rows need repair, while the session-scoped form is index-served at O(plies of one session). Hence the per-candidate re-read uses the scoped form and each pass materializes the set-wide form once. Parity across all three is pinned on **both** dialects.

**Fail-closed assertions.** *Coverage*: zero ended-visible rows with `player_accuracy_algo_version IS DISTINCT FROM 1` — checks the **version**, since a computed NULL is valid. *Ply-coordinate soundness*: zero ended-visible rows at version 1 with a non-NULL accuracy over a broken grid — checks the **value**, since a repaired row is still version 1 and coverage passes either way. Neither can be answered from the repair's materialized candidate set, which is stale by construction; catching a row that broke *during* the migration is the whole point. Either failing raises, the run rolls back, and `alembic_version` does not advance.

Environment surface is exactly two variables, `GHOSTREPLAY_ACCURACY_BACKFILL_MODE` and `GHOSTREPLAY_ACCURACY_BACKFILL_BATCH`, neither of which can disable an admission guard. Statements are reached only through a per-dialect `StatementBundle`, so a `_PG` constant is unreachable on SQLite by construction. Downgrade is an explicit no-op — production rollback is a forward revert, not data reversal.

Both convergence statements (`BACKFILL_REMAINING_SQL`, `REPAIR_REMAINING_SQL`) return the **count and up to 20 sampled ids in one dialect-neutral statement** — a windowed `count(*) OVER ()` over the phase's detector, where zero returned rows means zero remaining. That is not a convenience: the exhaustion template's `remaining` / `passes` / `first_remaining` must come from the scan the pass **already paid for**, and fetching the sample with a second `SELECT … LIMIT 20` would be another full detector scan, breaking both one-scan-per-pass and the import scan budget.

##### 7.3.1.1.1 Runtime envelope (Release B)

The same revision carries the production envelope around that core.

**One clock.** `REVISION_DEADLINE_S = 900` is **revision-wide**, taken once at the top of `upgrade()` — before `VALIDATE` and before the population counts — and covers everything the revision executes, including both closing assertions. A phase clock leaves three reachable holes: the repair population count runs during mode binding before any runner exists, both assertions run *after* the runner returns, and a scan armed with a fresh `SCAN_STMT_TIMEOUT_MS` can start at second 899 and finish past second 900. Three rules close them, in both modes: (1) every statement is armed by one `_arm` with the **least of every deadline in force** — its own cap, the remaining revision budget, the batch remainder inside a per-batch batch, and in atomic mode the residual stall budget — with `lock_timeout = min(the mode's per-wait cap, that same remaining)`; (2) Python work is checked against the same clock before each pass, batch, session, repair candidate and assertion, and the compute watchdog is armed to the same remaining; (3) exhaustion raises one frozen template with `phase=<validate|backfill|repair|assert>` and **explicit `n/a`** where a field cannot exist — a mid-statement cancellation leaves the transaction aborted, so no diagnostic query is attempted and `sqlstate` carries `57014`/`55P03` instead. Rule 1's *hardness* is PostgreSQL-only (both GUCs are PostgreSQL's); on SQLite the same clock is enforced best-effort between statements, which is acceptable because SQLite is dev/CI-only, single-writer, atomic-only, and has no concurrent writer whose stall to bound. "Between statements" is meant literally and is where a per-statement count matters: the SQLite backfill applies one single-row `UPDATE` per session rather than one set-returning statement per batch, so the check is re-run before **each** of them — a single check before the loop would gate the first write and leave the rest of the batch writing unchecked past the deadline. For the same reason — no row lock this revision can shorten, and no live writer to protect — the residual stall envelope below is gated on PostgreSQL as well as on atomic mode, so a nonempty SQLite upgrade is bounded by the revision deadline alone.

**Mode binding.** The mode variable is parsed on **every** dialect (`.strip()` then an exact, case-sensitive match — `ATOMIC`, `Batch`, a typo and a blank-but-set string all raise before any row is touched) and applied per dialect and per population. Off PostgreSQL, unset means atomic and `batch` raises as unsupported. On PostgreSQL the variable is **required** once either population is nonzero — there is no default, because an unset variable is a deployment error and never a silent atomic run — while a database with **both populations empty** upgrades with no configuration at all, skipping the runner but still running `VALIDATE` and both assertions (two scan-bearing `session_moves` statements, zero row locks). Binding first counts both populations and, on PostgreSQL, **probes the live relation dimensions** from the catalog (`pg_total_relation_size`, exact and O(1); `pg_class.reltuples`, an estimate and O(1) — never `count(*)`, which would add a relation scan purely to price another) and derives `G_moves` / `G_sessions` as `max(1.0, byte ratio, row ratio)` against the four frozen `SIZED_*` dimensions. Those factors are load-bearing: a correctly stamped version-1 session is in **neither** population yet adds rows and pages to both relations, and Release A is the sole writer for the whole interval between sizing and deploy — so a guard that rechecks only the populations is checking the one dimension that cannot move.

**Admission.** The scan-budget invariant is rechecked at runtime with the frozen constants **rescaled by relation** (the `session_moves` terms by `G_moves`, the coverage assertion, the convergence count *and the sweep's relation-scan component* by `G_sessions`) **and the selection sweep re-priced at the page count the run's resolved batch size and live `N_stale` actually produce**, in both modes — batch mode has no stall projection, so this is what catches an outgrown relation there. Atomic mode additionally projects the **full** stall — row work, all scans under lock, the coverage assertion, the backfill's own unindexed `game_sessions` selection sweep and convergence count, **and the commit itself** via a teardown floor plus a per-mutated-row slope — and raises if it exceeds `MAX_WRITER_STALL_MS` or if the teardown reserve leaves no positive residual work budget. The backfill's own two terms are mandatory and easy to miss: its keyset selection sweep and `BACKFILL_REMAINING_SQL` both filter `game_sessions` on `player_accuracy_algo_version IS NULL OR < 1`, which **no index covers**, so each is `O(G_sessions)` and not `O(N_stale)` — a correctly stamped row *raises* their cost while leaving the population unchanged. The sweep is additionally a **two-component model, never a scalar**: it is `ceil(N_stale / batch_size) + 1` pages of one pass (the `+1` is the empty page that terminates it, and `N_stale = 0` is one page rather than zero), so its cost is a function of an operator-chosen variable that ranges over `MIN_ADMITTED_BATCH..MAX_BATCH_SIZE` and moves the measured cost by 42x. `MARGINED_MS_BACKFILL_SWEEP_SCAN x G_sessions` prices the relation walk — served from the primary key, so the relation is read about once *in total* rather than once per page — and `MARGINED_US_BACKFILL_SWEEP_PER_PAGE x pages / 1000` prices statement startup, which takes **neither** growth factor because a larger relation does not make starting a statement more expensive. `batch_size` is a **required** argument of `project_atomic_stall_ms`, `bind_mode`, `stall_for` and `assert_runtime_scan_budget` with no default: a call site that does not say what page count it is budgeting for is the defect this model replaced, and an admission projection blind to its own dominant variable cannot refuse an inadmissible configuration.

**Two envelopes, both shipped.** The deployment chooses which one *runs*, never which code exists, so the per-batch runner and its PostgreSQL suite stay permanently present and permanently gated. **Atomic mode** uses Alembic's single transaction and unlocked selection, one pass per phase (no `SKIP LOCKED`, so nothing is transiently skipped and extra passes were never admitted by the projection). **Per-batch mode** runs on an independent connection whose transaction lifecycle is explicit — the `application_name` label and PID log autobegin a transaction, so it is **committed** before the batch loop opens its first one, or a first-batch rollback would silently drop the name an operator is watching for — with one explicit transaction per batch, `FOR NO KEY UPDATE SKIP LOCKED` selection, a `MAX_BATCH_MS` batch deadline, up to `MAX_PASSES` passes with a `0.5, 1, 2, 4, 5, 5 …` backoff clamped to the remaining budget (a backoff that would sleep *past* the deadline raises instead), and no transaction open during a sleep.

**What bounds what, at its true confidence.** *Enforced by PostgreSQL:* no SQL runs past the deadline; no **single** lock wait exceeds the mode's cap; and no **sum** of lock waits exceeds the budget the hold spends from, because `lock_timeout` is armed as `min(cap, remaining)`. The third is separate from the second on purpose — `lock_timeout` applies **per acquisition**, so on its own it permits any number of just-under-cap waits, each extending a hold already open over every row locked so far. *Enforced where it can arm:* Python compute, by a `signal.setitimer` watchdog re-armed **per session** to `min(MAX_SINGLE_SESSION_COMPUTE_MS, batch remaining, revision remaining, atomic remaining)`, with the previous handler and pending timer saved and restored; main thread only, and off it the runner **logs that it is unarmed** rather than claiming enforcement it lacks. Because the batch remainder is one of those minima, `MAX_BATCH_MS` is batch-wide over SQL *and* Python — which is why `EST_MAX_LOCK_HOLD_MS = MAX_BATCH_MS + TEARDOWN_ALLOWANCE_MS` and has **no** per-session compute addend. *Estimated only:* teardown, which `statement_timeout` does not cover. Per-batch mode therefore gets an after-the-fact **tripwire** — each batch compares its observed hold against `EST_MAX_LOCK_HOLD_MS` and raises, explicitly *not* a bound: by the time it fires the lock has been held too long, and what it buys is that the next batch does not repeat it and the breach becomes recorded evidence. Atomic mode **on PostgreSQL** reserves its teardown out of `MAX_WRITER_STALL_MS` and holds the work to the residual: at `t_stall_0` — the instant immediately **before** the first lock-bearing statement, conservative because it also charges that first acquisition's wait — it derives `ATOMIC_WORK_BUDGET_MS` and arms every subsequent statement against it.

**Atomic mode has no lock-hold tripwire and must not claim one.** Its hold ends when Alembic's transaction commits, which happens in `env.py` *after* `upgrade()` has returned, so the revision can neither observe "first row lock through commit" nor raise on it — and raising after a durable commit would fail a deploy whose data is fine while a rerun (both populations now empty) could never reproduce the observation. What it gets instead is enforcement in front (the projection) and in flight (the residual deadline), plus **observation** behind both: the revision hands `max_stall_ms` and `projected_stall_ms` to the already-shipped `app.migration_guard.migration_stall_probe` at `t_stall_0`, and the existing `report()` in `env.py`'s `finally` — which fires exactly when COMMIT *or* ROLLBACK returns, i.e. when the locks are released, on both paths — logs `observed_atomic_stall_ms` at INFO with the projection alongside, and at **ERROR** when it exceeds the bound. It never raises. Both threshold arguments are optional, so revisions that record only a timestamp keep their INFO-only behaviour. That log line is the only empirical check on the atomic projection, and sizing qualification is where it is read.

**Single-runner guard (`app/migration_guard.py`, PostgreSQL only).** `alembic upgrade head` opens the migration transaction with no mutual exclusion, so two overlapping replica starts run two upgrades at once — and `SKIP LOCKED` does not make that safe (it protects individual session rows, not `alembic_version` stamping, `VALIDATE CONSTRAINT`, or the atomic-mode single transaction). `alembic/env.py` therefore takes a **session-scoped two-key `pg_advisory_lock`** on a **dedicated guard connection** — never the migration connection — *before* the migration connection is opened, and releases it in a `finally` (explicit unlock → `invalidate()` if the unlock returned false/raised → close under `NullPool`). Session scope is mandatory: `20260709_02`'s `autocommit_block` commits the migration transaction mid-chain, which would drop a transaction-scoped lock with the backfill/repair/validate/stamp still ahead of it. A second overlapping upgrade blocks on the lock, then reads the already-advanced `alembic_version` and no-ops. Acquisition is fixed-order (label `application_name` → log `pg_backend_pid()` → arm a transaction-local `lock_timeout` → acquire → commit); a lock-timeout — and **only** SQLSTATE `55P03` — becomes the named `ConcurrentMigrationError`, while every other `OperationalError` (disconnect, shutdown, crash) propagates unchanged. The guard and migration connections are each labelled session-scoped (`ghostreplay_alembic_guard` / `ghostreplay_alembic_migration`) and log their backend PID so an operator or cancellation probe can tell them apart; `ghostreplay_accuracy_backfill` is frozen here and applied by `20260719_01`'s per-batch runner through the same shared helpers. Labelling and the PID log run **inside** `context.begin_transaction()` (never before `context.configure()`), or SQLAlchemy autobegins a transaction Alembic treats as external, `begin_transaction()` degrades to a no-op, and the whole run is silently rolled back at close under `NullPool`. A `migration_stall_probe` (first-lock-wins record, consume-and-clear `report()` from the inner `finally`, never raises) observes the atomic-mode row-lock hold, and classifies it at ERROR when the revision supplied a threshold — see §7.3.1.1.1. The lock makes correctness independent of Railway configuration, so both migration-ownership branches (`preDeployCommand`, or `startCommand` with single-replica evidence) stay open; the recorded production choice is deferred to release integration.

#### 7.3.1.2 Sizing derivation and admission constants (Release B)

The revision refuses an atomic run whose projected writer stall exceeds `MAX_WRITER_STALL_MS` and enforces a per-batch SQL deadline. Both bounds are made of constants only a measured run can produce, and measuring *through* the revision with those guards armed is circular — in exactly the case where batch mode is mandatory, the guarded run aborts before producing the number that would have proved it. There is deliberately **no bypass**: an environment variable that disarms the guards is production-reachable by definition, since matching `current_database()` only prevents *accidental* reuse and the production database name is knowable. Measurement therefore lives in a standalone harness, `backend/scripts/size_accuracy_backfill.py`, run by hand against a disposable restore and never on a deployment path. It reaches every statement through `ScriptDirectory.get_revision("20260719_01").module` and calls the revision's own `_accuracy_for` / `_load_moves`, so the population it measures and the statements it times are the ones that ship; `backend/test_release_b_sizing.py` asserts that structurally and forbids a harness-local constant carrying a `/* ghostreplay: */` marker.

**Six kinds of work, six scaling laws.** Scaling one combined number by stale-session count makes work that is *independent of both populations* vanish from the projection whenever those populations are small or zero. So the model prices separately: `VALIDATE` (whole `game_sessions`); backfill row work (the larger of the stale-session and evaluated-move ratios); repair **per-candidate mutation only** — lock, session-scoped re-read, conditional update, **scans excluded**, scaled by `N_repair`; the **maximum of the four complete scan-bearing statements** over `session_moves` (`REPAIR_POPULATE_SQL`, `REPAIR_REMAINING_SQL`, `SOUNDNESS_ASSERT_SQL`, the pre-flight repair population count — never a bare detector, which no code path executes alone), scaled by relation size and **never zero**; the coverage assertion (whole `game_sessions`, never zero); and **atomic teardown**, a population-independent floor **plus** a per-mutated-row slope, measured at two points because one point cannot yield both — each on **its own fresh restore**, since a second pass in the same process has already validated the CHECK, so its `COMMIT` flushes no catalog change and the difference would charge `VALIDATE`'s own commit to the per-row slope. On a clean audit with a small stale set the last three *are* the whole stall — the exact shape a population-scaled model scores at nearly zero and wrongly admits into atomic mode.

**The breach path is measured from outside the cancelled process.** `TEARDOWN_ALLOWANCE_MS` takes the larger of observed commit and observed **cancel-to-unlock**: the interval from cancel issuance to the moment a competing `FOR NO KEY UPDATE NOWAIT` on a held row *acquires*, observed from a second connection over ≥ 20 trials against a locked, fully dirtied batch parked by a harness-local `AFTER … FOR EACH STATEMENT` trigger. "Fully dirtied" is *proved*, not assumed: the trigger fires only once the `UPDATE` has written every row, so the controller polls `pg_stat_activity` for `wait_event = 'PgSleep'` on the holder's backend before issuing the cancel and discards a trial that never parks — a timed sleep would race the write phase and measure the rollback of a partially dirtied batch. Phase 2 refuses to freeze any teardown constant without both probe scopes present, ≥ 20 landed trials each, and an atomic probe that locked at least as many rows as the atomic transaction mutated. A clock the cancelled process starts begins *after* PostgreSQL's interrupt latency and the statement unwind have already elapsed and cannot contain them; the process-side rollback-only duration is recorded beside it, named as that, and is never the frozen input.

**Frozen literals, checked at import.** The revision declares `MAX_WRITER_STALL_MS`, `MAX_BATCH_MS`, `BATCH_LOCK_WAIT_MS` / `ATOMIC_LOCK_WAIT_MS`, `REVISION_DEADLINE_S`, `MAX_PASSES`, `ATOMIC_SCANS_UNDER_LOCK`, the measured `MARGINED_*` / `SCAN_STMT_TIMEOUT_MS` / `MAX_SINGLE_SESSION_COMPUTE_MS` / `TEARDOWN_ALLOWANCE_MS` terms, and the four `SIZED_*` relation dimensions the two scan constants were measured against — frozen *in the revision*, because a correctly stamped version-1 row joins neither population while adding rows and pages to both relations, so a population recount cannot see the growth that matters most to the scan terms, and a dimension living only in the runbook cannot be divided by. `MARGINED_US_ATOMIC_TEARDOWN_PER_ROW` and `MARGINED_US_BACKFILL_SWEEP_PER_PAGE` are denominated in **microseconds**, and for the same reason: rounding a sub-millisecond slope up to an integer millisecond would add a phantom second of projected stall per thousand rows in the first case and ~2.9 s at the sweep's 6,001-page worst case in the second. Each is divided by 1000 at exactly one call site, and a constant test pins that so a “tidy-up” into milliseconds fails loudly. `MAX_BATCH_SIZE = min(B_formula, B_tested)` and `REPAIR_BATCH_SIZE = min(R_formula, R_tested)` are derived at import, so no admitted batch exceeds a size sizing actually demonstrated; the zero-batch boundary is **admissible at equality** and **raises** one millisecond past it rather than clamping to 1, which would silently violate the budget the constant exists to enforce. The scan budget `(2·MAX_PASSES + 2)·MARGINED_MS_PER_SCAN_STMT + MARGINED_MS_COVERAGE_ASSERT + MAX_PASSES·(backfill_sweep_ms(pages) + MARGINED_MS_BACKFILL_REMAINING) < REVISION_DEADLINE_S·1000` also raises at import, so a `session_moves` whose scans alone cannot fit the wall clock fails when the revision loads instead of 900 seconds into a run; the last group is the backfill's own per-pass `game_sessions` work, which a `session_moves`-only budget lost entirely. `pages` is keyword-only with **no default**, and at import it is the DECLARED worst case — `IMPORT_WORST_CASE_SWEEP_PAGES = ceil(SIZED_TOTAL_ROWS / MIN_ADMITTED_BATCH) + 1 = 6,001` — because module load has no database, no population and no resolved batch size. That charges 85.6 s against the 900 s deadline, 814 s of headroom, so nothing forces raising the admitted batch floor. `SCAN_STMT_TIMEOUT_MS` is separately required to be the maximum over **every** statement it is armed on, and the two convergence scans land on *different* terms there: `REPAIR_REMAINING_SQL` wraps the ply detector, so it scans `session_moves` and is already one of the four complete statements (`G_moves`), while `BACKFILL_REMAINING_SQL` scans `game_sessions` on the unindexed predicate and is priced by `MARGINED_MS_BACKFILL_REMAINING` (`G_sessions`) — so that term, scaling by neither population, is the one relation growth alone can push above the rest, and the one the maximum must not omit. Arming a statement with less than its own measured cost is a self-inflicted cancellation that surfaces as non-convergence. **Neither** sweep component is in *that* maximum, and only there are they excluded: each page of the sweep is armed by the mode's batch cap, so the two sweep constants price a multi-statement unit no single armed value has to cover. `EST_MAX_LOCK_HOLD_MS ≤ MAX_WRITER_STALL_MS` is arithmetic over frozen literals: it proves the *estimate* fits the budget and nothing more — what backs it at run time is the compute watchdog, the armed SQL timeouts, and the observed-lock-hold tripwire.

Provenance — snapshot, date, timed SHA, every raw number, every batch candidate tried and which bound won, and the executable `GHOSTREPLAY_ACCURACY_BACKFILL_MODE` verdict — is recorded in [`docs/release_b_runbook.md`](docs/release_b_runbook.md). The constants currently frozen are derived from a **locally synthesized snapshot**, not a production restore; re-derivation, the guarded Phase 3 reruns, and the health-window verdict are the sizing-qualification bead's.

**These constants never gated the production run.** Revision `20260719_01` reached production and executed on or before 2026-07-24 — at the shape it had on 2026-07-19, before the admission constants, the runtime envelope, and the harness above existed. A 2026-07-24 production dump restored 2026-07-25 shows `alembic_version = 20260720_01`, the CHECK `convalidated`, both populations empty, and 1,603 sessions that ended before the serving write hook was even committed (`95be57a`, 2026-07-11 23:57 PDT) carrying `player_accuracy_algo_version = 1`. The run left a clean terminal state — both assertions pass, and all 95 fail-closed `NULL`-accuracy rows are refusals the frozen algorithm still makes — but no armed timeout, admission projection, compute watchdog, or lock-hold tripwire was involved in producing it, and Alembic will not re-run the revision against that database. Everything in §7.3.1.1.1 and §7.3.1.2 therefore governs only a **from-scratch** run: a fresh development or staging database, a rebuilt production, or a restore brought to head.

**What the restore does and does not settle.** Production holds 4,184 sessions and 131,676 moves — fewer rows than the sizing snapshot on both axes. Row counts are the only dimension a *logical* dump carries: `pg_total_relation_size` on a restore measures the locally rebuilt relation, and readings of the same restore gave 4,079,616, then 4,096,000 once autovacuum materialised the FSM/VM forks, then 5,414,912 … 6,848,512 across seven sibling copies — same source population, same `game_sessions` row count, different synthesis and vacuum state. Nor does "fewer rows" imply "smaller constants": every `MARGINED_*` term is an elapsed time, jointly determined by relation size, chosen plan, storage, CPU and server major. A re-derivation on production's own major (18.4) against the restore is recorded in `docs/release_b_runbook.md` §7; it is **not** applied to the revision, and applying it is the sizing-qualification bead's. **That rerun is no longer blocked on missing evidence** (`g-b-size-measurement-json`, 2026-07-26): §7's derivation had exactly two of its eight measurement artifacts on disk — the sweep domains — so every other constant survived only as a transcription, and one re-measurement of the whole set on the 18.4 restore now commits all ten inputs plus `--derive`'s own output under `docs/sizing/`, re-derived from them by `test_the_committed_measurement_set_re_derives_its_published_output` in `test_release_b_sizing.py` and recorded in §8. It found what a transcription hides: §7's derived table carries the sweep pair at the *shipped* basis inside a column otherwise at that run's own, and the same two artifacts through the LP give 71 / 491 µs there — a transcription defect that moved no frozen constant, since the coefficients are basis-dependent by construction. What committed evidence cannot reach is §3's shipped literals, frozen on 15.18 against a synthesized 6,000-row snapshot that no longer exists; only a re-freeze from a committed set makes those reproducible, which is the sizing-qualification bead's. **The provenance defect that first blocked it is fixed** (`g-b-size-harness-defects`, 2026-07-26): the harness synthesizes its populations before the measured pass, so the relation the timed statements ran against is not the one the copy arrived as — 6,144,000 bytes against 4,096,000, 130,676 moves against 131,676 — and it used to record one unlabelled reading of it while `SIZED_*`, the basis the runtime divides every scan term by, is frozen from exactly that. The post-synthesis reading was never the wrong one; it is what the timings ran against, and it stays what `SIZED_*` is frozen from, because a term and its declared basis have to move together. What was missing was the *label*. Every artifact now carries **both** readings under `dimension_bases` plus an explicit `timing_basis`, and `--derive` fails closed on an artifact with no machine-readable basis, on one whose recorded basis disagrees with the `dimensions_before` its statements were timed against, and on freezing `SIZED_*` from a snapshot whose pre-synthesis reading was never taken. Substituting the pre-synthesis reading while leaving the timings alone is refused rather than accepted as a correction — and *not* because it is optimistic: the error runs in both directions, depending on which side of a ratio the substituted reading lands on. A sweep copy's own reading substituted downward raises that point's `N_copy = frozen/copy` and over-charges it; the frozen basis substituted downward lowers every `N_copy` and under-charges the fit, while raising the run-time `g_sessions = live/SIZED` and over-charging the scan terms. The guard holds because a timing and its basis have to move together, not because either direction is safe. The pre-synthesis reading is emitted as provenance and divided by nowhere — carrying a timing onto it is a separate, explicit step. The two committed sweep artifacts were **migrated, not re-measured**: their post-synthesis reading is accurate and is all the fit needs, so both keep their points and stay active constraints, while their never-taken pre-synthesis reading is recorded as `not_recorded_legacy` rather than reconstructed from prose, a sibling copy, or an inverted growth factor. It did time `BACKFILL_REMAINING_SQL` and the selection sweep directly for the first time. **A timing may only be normalized by the basis of the copy it actually ran on**: the 1.147 ms `BACKFILL_REMAINING_SQL` maximum was measured on `gr_p1_atomic` and carries that copy's 6,848,512 bytes, giving `ceil(3 · 1.147 · max(6000/4184, 10010624/6848512)) = 6`. `MARGINED_MS_BACKFILL_REMAINING = 6` is qualified as the **worst of seven such pairings**, not of any single one, and carries no page-count term since that statement runs once per pass.

**The selection sweep is a two-component model, because it cannot be a scalar at all.** The sweep is every `SELECT_BATCH_*` page of one pass, so `pages = ceil(N_stale / batch_size) + 1`, and `resolve_batch_size` admits `GHOSTREPLAY_ACCURACY_BACKFILL_BATCH` anywhere in `MIN_ADMITTED_BATCH..MAX_BATCH_SIZE`. Measured across that whole domain the cost spans **42x** (3.66 ms at 2 pages to 282.25 ms at 1,647), so no scalar is honest across it: the frozen 37 was 21x under-charged at `batch_size = 1`, and unqualified at *every* page size measured with enough trials to estimate a maximum — not merely outside part of its range. It was **deleted**, not re-picked; a scalar left in the module is a scalar something goes on to price a sweep with. What replaced it is `MARGINED_MS_BACKFILL_SWEEP_SCAN = 72` ms (the relation walk, `xG_sessions` — keyset pagination is served from the primary key, so the relation is read about once *in total* rather than once per page, which is where the original `pages x scan` derivation went wrong) plus `MARGINED_US_BACKFILL_SWEEP_PER_PAGE = 518` µs (statement startup, `x pages`, no growth factor). Both are frozen from an **upper-envelope** fit rather than a least-squares one — a regression through a set of maxima sits below some of them, so tripling its coefficients still under-prices a measurement it was fitted to — specifically the least conservative line covering every measured maximum on record, a two-variable LP solved exactly in **frozen-basis coordinates** with each point carrying the growth factor of the copy *it* ran on, since the sizing copies share a row count while their relations differ by 26% and one shared factor silently under-charges the leanest. Every measured maximum constrains the bound including the legacy 3-trial run, whose `(4 pages, 13.60 ms)` outlier is an active constraint; the 7-trial floor applies only to points that *steer* the objective. Correctly pricing the sweep moves the atomic rejection boundary from `N_stale = 5,675` to **5,136** at `batch_size = 1` — a false-admission band that is itself a function of the batch size, which is the point. **The model is measured across the whole domain it is evaluated over, out to the 6,001 pages the import-time budget charges and past the atomic rejection boundary near 5,137.** It was frozen from a domain stopping at 1,647 pages and linearly extrapolated beyond it, with the gap declared as an assumption; `g-b-sweep-endpoint-measure` closed it on a **second production-shaped basis** — `gr_p2_sweep6000`, a fresh restore of the same production dump grown by `--synthesize-sessions` to `N_stale = SIZED_TOTAL_ROWS = 6,000`, every row stale, swept at `MIN_ADMITTED_BATCH` for exactly 6,001 pages, seven trials per point, every trial retained. Nothing was rebased or merged: its points enter the same LP through their own `N_copy`, which is **clamped at 1** because that copy is larger than the frozen basis on both axes, so its timings are charged undiscounted. The vertex does not move — same `a`, same `b`, same two active constraints — and no point of the new basis binds: at 6,001 pages the frozen pair models 3,180.518 ms against the 3 x 1,004.131 = 3,012.394 ms coverage demands. A PostgreSQL-gated endpoint test asserts the same *shape* on whatever host runs it, since a per-page term that stays one as the page count grows is a property of the machine rather than of the constant: it executes a 6,001-page sweep and compares that host's upper-range per-page slope against its own lower-range one — paired, interleaved samples reduced by median, segment by segment, so a late nonlinearity fails — with absolute coverage asserted separately. Its timings are never evidence for the frozen numbers and never enter the LP, since its relation is a fixture of clones rather than a production-shaped copy. `MARGINED_MS_BACKFILL_REMAINING = 6` is unaffected and directly qualified: it runs once per pass, carries no page-count term, and the worst of seven measurements normalized on their own bases is exactly 6. Raw trials, the migrated domain artifact and the endpoint basis in `docs/sizing/sweep_batch_domain_20260725.json` and `docs/sizing/sweep_batch_domain_endpoint_20260725.json`. See `docs/release_b_runbook.md` §0 and §5-§7.

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
4. On a terminal path, the frontend stops incremental uploads, builds the complete history, and replays its exact SAN/FEN chain from the known starting FEN. If and only if that valid chain ends in a **terminal position** and its final ply remains unresolved, the frontend synthesizes that ply's eval deterministically — no engine round-trip — from the replayed board:
   - **checkmate** → mover-relative `eval_cp=+10000`, `eval_mate=0`, `eval_delta=0` (a mating move is provably unbeatable, so the delta is exact);
   - **terminal draw** (stalemate, threefold repetition, fifty-move rule, or insufficient material) → `eval_cp=0`, `eval_mate=null`, `eval_delta=null`. The played position is fixed at 0, but a draw does **not** prove the move was best (repetition, stalemate, or a fifty-move draw can squander a win), so the delta is unknown and is **explicitly nulled** — a stale non-null delta on the unresolved row is cleared rather than left to understate CPL.

   Terminality is read from the **reconstructed history**, not the bare final FEN: threefold repetition is undetectable from a lone FEN (the repetition count is not encoded), and the fifty-move clock is only carried because the replay recomputes it. Every synthesized row is stamped `synthetic_terminal_eval=true`. Any worker-resolved row wins (including a resolved threefold draw whose search produced a nonzero eval), and malformed, truncated, extended, mismatched, or nonterminal chains remain untouched. The provenance flag keeps this sparse synthesized result out of the global analysis cache while allowing it to repair the session's persisted accuracy inputs.
5. The frontend performs the bounded final full-history upload before requesting the terminal recompute
6. Server bulk-upserts `session_moves` records
7. For the **final full-history upload only**, the server writes a durable `session_upload_receipt` row in the SAME transaction as the moves (see below)

**Final-upload receipt (g-upload-observe).** The end-of-session full upload is bounded by a 4 s client deadline and times out on ~18.5% of terminal actions; a client `TimeoutError` is an abort, **not** proven loss (the server can commit row-by-row after the client gives up). To measure the exact **final-upload noncommit rate** — independent of fire-and-forget PostHog delivery — every request carries a client-generated `X-Client-Request-ID` (present even on a timeout, unlike the server-echoed `X-Request-ID`), and each of the three `/moves` callers tags its `upload_kind` (`final_full` | `incremental` | `revert`). Only the `final_full` upload sends a `terminal_action` (`game_end` | `resign` | `drill_natural_end` | `accuracy_fail`); its presence is the server's discriminator. For `final_full` the server writes a `session_upload_receipt` row inside the moves' transaction, keyed by the middleware-normalized (non-null) `client_request_id`. A `final_full` upload lacking a valid client id is rejected **400 before any writes**, so no null-id receipt can exist. The invariant the join depends on is **`final_full` 200 ⇒ receipt**: an empty `final_full` upload writes no moves but still commits its receipt, so the endpoint never returns success without one. Presence/absence of the matching receipt row (on the post-rollout cohort, past a maturation window) is the exact commit classification: **present ⇒ committed; absent ⇒ the final request did not commit** — which alone does NOT prove tail rows are missing (earlier incremental uploads may already have persisted them), and actual tail-loss is not measured here. The middleware + backend + migration MUST deploy before the frontend, or new client events would hit an old server that writes no receipt and manufacture false loss. Client-side, a deadline that expires **while the response body streams** is classified `timeout`, not `parse`: `fetch()` resolves as soon as headers arrive, so a late abort surfaces on the body read, and that overrun must stay in the timeout cohort (its real status + `X-Request-ID` are retained, since they prove the server answered). `session_moves_uploaded` is retained as a timing/convenience signal only.

This deterministic fill is deliberately a **final-ply-only mitigation**. If an earlier nonterminal tail ply—commonly the penultimate ply—is also unresolved, that row remains null and whole-game accuracy can remain unavailable; resolving that worker-dependent residual requires the separate post-resolution strategy tracked by `g-2nrn`.

**Upload cancellation:** Unconverted drill sessions are best-effort evidence until they are converted. When a drill is abandoned, naturally ended, reset, or replaced by another drill/normal game without conversion, the client disables and aborts that drill's pending session-move uploads so stale rounds do not occupy live gameplay request capacity. If a late upload for an already ended, unconverted drill still reaches the backend, the backend keeps the raw `session_moves` upsert idempotent but skips expensive evidence side effects (ghost graph, blunder opportunity, analysis-cache, and opening-score recompute).

**Deferred evidence side effects (g-yjtn).** `POST /api/session/:id/moves` returns as soon as the `session_moves` rows are durably committed. On the request path only the cheap O(1) work remains: the `session_moves` upsert + commit (the durability boundary), the post-commit `session_moves_uploaded` analytics event, and a non-blocking **enqueue**. The expensive accounting — the graph-dependent transaction (advisory lock + ghost-graph upsert + blunder-opportunity recompute + commit), the analysis-cache write, and the opening-score recompute enqueue — is handed to an **in-process background worker** (`app/session_evidence_scheduler.py`), removing the per-row round-trips and the `pg_advisory_xact_lock` wait from user-visible latency. The worker mirrors the opening-score scheduler (§13.1): a single daemon thread, in-memory coalescing, drain-on-graceful-shutdown — **not** a durable DB outbox.

**Parent-row writer locking and evidence sink.** Writers that mutate `game_sessions` or `blunders` serialize same-row changes with PostgreSQL `FOR NO KEY UPDATE` (centralized by `app.row_locks.for_no_key_update`, with `populate_existing()` for identity-map safety). This still conflicts with another writer lock on the same row while remaining compatible with foreign-key `KEY SHARE` locks taken by child inserts. For moves, game end, drill transitions, first-blunder recording, and SRS reviews, all entity writes are explicitly flushed before `opening_score_cursors.evidence_seq` is advanced; the cursor upsert is the transaction's final blocking database statement before commit. Manual target adds do not lock their source session and therefore bump every newly inserted target unconditionally, whether the source is active or already ended; duplicate targets do not bump.

**Branch-scoped stale-write locks (g-branch-locks).** The drill route-check and next-opponent endpoints also take the session's `FOR NO KEY UPDATE` lock, but only around the **branch that actually mutates**. Route-check re-reads the drill state under the lock: the `root_reached` and on-route branches are pure **snapshots** that write nothing (and take no lock work beyond the read), while the target-reached and off-route branches lock and write; a snapshot branch preserves a concurrently-recorded failure rather than clobbering it. Next-opponent returns **400** on a stale drill that has already `failed` under the lock, and **falls through** (no drill write) on one that has already `converted`. Critically, next-opponent **releases the session lock before the engine call** so a concurrent `/moves` upload can commit while the (potentially slow) engine computes the reply — the lock spans only the stale-write check, not the engine work.

**NKU writer inventory (source-scanned).** The complete set of sanctioned `game_sessions` / `blunders` writer-lock sites — game end (×2), post-end `/moves`, the five drill writers, route-check (two branches), next-opponent, first/auto and manual blunder recording, and SRS review — is pinned by `test_writer_locks.py`, which also **source-scans** the lock modules and fails if any `.with_for_update()` there is not `FOR NO KEY UPDATE` (i.e. `key_share=True`, non-`read`). This keeps the inventory honest: a new non-NKU lock on these tables breaks the guard rather than shipping a silent `FOR UPDATE`/`FOR KEY SHARE` deadlock regression. The `analysis_cache` / `position_analysis` repos deliberately take a different (bare `FOR UPDATE`) lock on their own tables and are out of this inventory's scope.

**Coordination graph (acyclic, cursor is a pure sink).** Across all these paths the lock-acquisition order is consistent and forms a DAG: `session → users`/`advisory → cursor`, `blunder → cursor`, `advisory → cursor`. No path takes any lock *after* writing `opening_score_cursors`, so the evidence cursor is a pure sink and the writers cannot form a lock cycle. The `session_upload_receipt` write (g-upload-observe) preserves this: its `session_id` is a **plain, FK-free column**, so the append-only insert takes **no `KEY SHARE` lock** on `game_sessions`, and it is flushed **before** the `evidence_seq` cursor bump (never after the transaction's final blocking statement) — a pure sink alongside the cursor, adding no edge to the DAG.

- **Coalescing & dedup.** Pending work is keyed by `session_id` (one user + player_color per session). Within a coalesced entry, moves are deduped by `(move_number, color)` with **last-write-wins** — the same key as the `session_moves` upsert — so the end-of-session burst (the incremental fire-and-forget uploader plus the final full-history upload re-sending the same slots) collapses to **one worker run carrying exactly one payload per committed slot**. The entry is bounded by session size, not upload count, and the deduped payload avoids the analysis-cache `DUPLICATE_CONFLICT` that a naive concatenation of overlapping slots (with differing evals) would cause. The single worker thread serializes **all** evidence runs (even across sessions/users), so in single-process/single-replica prod the advisory lock is effectively uncontended; it still guards the unique indexes against any cross-process writer.
- **Best-effort enqueue.** The enqueue swallows and logs any scheduler fault so it can never regress `/moves` from 200 to 500 (same contract as the opening-score `request_recompute`). The request returns 200 with the usual `drill_state` / `drill_terminal_reason`.
- **Durability risk (accepted).** A hard kill (SIGKILL/OOM/deploy-kill) between enqueue and worker completion drops that session's deferred accounting — a narrow regression vs the prior synchronous commit, traded for the latency win. Backstops: (1) blunder-opportunity events **self-heal** on the next successful same-session upload (a full recompute from all committed `session_moves`); (2) the offline `scripts/recompute_srs_opportunities.py` recompute; (3) `drain=True` on graceful shutdown.
- **Shutdown ordering (load-bearing).** On graceful shutdown the lifespan drains the evidence scheduler **first**, then shuts down the opening-score scheduler (then PostHog, then `engine.dispose()`). The evidence drain's final step calls the opening scheduler's `request_recompute`, which silently early-returns once that scheduler is shutting down; draining evidence *before* stopping the opening scheduler keeps those recompute enqueues live, and they drain in the opening scheduler's own subsequent shutdown.

**Per-user graph-write serialization (g-q0aw, PostgreSQL only).** On the worker, the graph-dependent work runs as a single **transaction** that takes `pg_advisory_xact_lock(user_id)` and, holding it, upserts the ghost graph, recomputes the session's blunder-opportunity events, and commits. The advisory lock makes concurrent same-user graph writes **queue deterministically** instead of racing the `(user_id, fen_hash)` / `(from_position_id, move_san)` unique indexes; the writes are idempotent, so they converge on the same deduped edge set. In single-process prod the single evidence worker already serializes these runs, so the lock primarily guards against a cross-process writer (extra replicas/workers, the backfill script). The lock and two transaction-local guardrails — `lock_timeout` (default `5s`, on advisory-lock acquisition) and `statement_timeout` (default `10s`) — are set via `set_config(..., is_local=true)` as the first statements of the fresh transaction and are released/reset by that transaction's commit, so the lock spans exactly the graph-upsert + opportunity-event contention window. The backfill script (`scripts/backfill_ghost_graph.py`) calls the graph upsert directly, **bypassing** this lock and the timeouts (intentional for a single-threaded admin migration); it must not run concurrently with live uploads.

**Timeout degradation.** On a recoverable Postgres timeout SQLSTATE (`55P03` lock-not-available / `57014` query-canceled) the graph transaction rolls back and **retries once** — a clean full recompute, since the `session_moves` rows are already committed and opportunity events are rebuilt from all session moves. If the retry also times out, the worker rolls back and **accepts the gap** with an explicit WARNING: blunder-opportunity accounting for that session is dropped and does **not** self-heal (SRS counters read the persisted rows with no lazy recompute), regenerating only on the next successful same-session upload. On the timeout-degrade path the **analysis-cache write** (its own transaction) and the **opening-score recompute enqueue** (cheap, coalesced, self-healing) still run — they sit outside the graph-dependent failure boundary. Other `OperationalError`s (connection failures, etc.) and all non-timeout errors instead abort the run and are **logged on the worker thread** (the `/moves` response has already returned 200, so nothing surfaces to the client); each session's run is isolated, so one failure never wedges the worker or other sessions' runs.

**Connection pool knobs.** The SQLAlchemy engine pool is env-overridable via `DB_POOL_SIZE` (default 10) and `DB_MAX_OVERFLOW` (default 10), bumped from the prior hard-coded 5. The evidence **worker** now opens its own pooled session per run and holds one connection during the graph/analysis-cache work (bounded by `lock_timeout`) instead of the request holding it. The effective connection ceiling is `(pool_size + max_overflow)` **per process** — multiply by worker/replica count before tuning against PostgreSQL `max_connections`.

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

The response's `position_analysis` map is keyed by full `fen_before` (one entry per played position), but its best-move / best-line / best-eval truth is sourced at the *position* grain: `backend/app/api/session.py` resolves each entry by `normalize_fen(move.fen_before)` against the `position_analysis` storage table and emits it under the original full-FEN key. Each entry carries an explicit `position_trusted` flag — `true` for a trusted storage winner / legacy-v2 projection, `false` for an untrusted `SessionMove` seed fallback. `best_move_eval_cp` is side-to-move-relative (the white-relative storage `best_eval` sign-converted by active color). `best_move_eval_mate` is likewise side-to-move-relative (the white-relative storage `best_eval_mate` sign-converted by active color) and is emitted whenever the trusted winner carries a mate eval — typically a mate-only winner (`best_eval=None`), but a superset merge of disagreeing runs (`backend/app/position_analysis_policy.py`) can retain *both* `best_eval` and `best_eval_mate`, in which case both wire fields are populated. Consumers treat mate as authoritative when both are present (mate-first, matching `tree_eval._best_move_eval`). See §14.6.

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
    "average_centipawn_loss": "integer | null",
    "accuracy": "integer | null"
  },
  "position_analysis": {
    "<fen_before>": {
      "best_move_uci": "string",
      "best_move_san": "string | null",
      "best_move_eval_cp": "integer | null",
      "best_move_eval_mate": "integer | null",
      "best_line_uci": ["string"],
      "position_trusted": "boolean"
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
as `0` for display/summary purposes. `average_centipawn_loss` is rounded
**half-up** to an integer (an exact `.5` rounds up), matching the frontend's
`Math.round` — see §5.2.2.

`average_centipawn_loss` is `null` if and only if no player move has an
`eval_delta` — an unanalyzed game reports `null`, not `0`. It is deliberately
not gated on completeness: a partially analyzed game reports the average over
the plies that did resolve. A value of `0` therefore means perfect play, not
missing data, and clients must not collapse the two (use a null check, never a
truthiness check).

#### Evidence grain: summary vs. displayed stats

The numbers on a review screen do not all come from the same evidence. Four
distinct grains, deliberately:

1. **Backend `summary` (blunders/mistakes/inaccuracies/`average_centipawn_loss`)
   and `accuracy`** — ORIGINAL game-time evidence, computed server-side over the
   persisted (base) `session_moves` rows. Base, not immutable: a post-end
   `POST /api/session/{id}/moves` upload can add, change, or clear evaluations,
   which is why accuracy self-heals on a later upload. Accuracy v1 is frozen.
2. **Displayed class counts and Avg CPL on BOTH review pages** (`/history` and
   `/game`, via `GameReviewStats`) — EXACT-BEST-PROJECTED moves: a played move
   equal to the *trusted* position best counts as `best` with `0` CPL. Each page
   projects at its own seam and hands the projected array to the stats hook and
   the board, so for TRUSTED EXACT-BEST PROMOTIONS the pane and the board's gold
   "best" stars agree. That guarantee is scoped to promotions only — a board star
   raised by a grain-3 overlay is still board-only and is not counted by the pane.
3. **Board-only re-annotation overlays** (the `upgraded` field, §Read-time
   re-annotation) — board display only. They never reach page-level stats: the
   overlay layer lives inside the board, below the array the page hands it.
4. **`/history`'s no-analysis fallback panel** (`summary.*` from `/api/history`,
   shown when a game has no analysis) — ORIGINAL evidence, unprojected. A
   different surface from `GameReviewStats`, deliberately left alone.

Grain 2 (displayed stats) therefore sits beside grain 1 (accuracy) on the same
pane. This skew is accepted, not a bug: accuracy v1 is frozen, and projection
only ever *promotes*, so the skew is bounded and one-directional. Two notes
on that bound:

- **It holds on the displayed integer, not only on the unrounded mean.**
  Projection only ever replaces a nonnegative `eval_delta` with `0` and never
  raises one, so the unrounded projected player mean cannot exceed the unrounded
  summary mean, and the projected class counts cannot exceed the summary counts.
  The displayed integers now inherit that bound: both sides round half-up — the
  frontend's `Math.round` (`gameStats.ts`) and the backend's `round_half_up_cpl`
  (§5.2.2) — and half-up rounding is monotone, so it cannot reorder two means
  that are already ordered. This was previously conditional: while the backend
  still used Python's banker's rounding, an exact-half mean could display one
  point HIGHER than the summary (unrounded `2.5` → displayed `3` vs. summary
  `2`) even though projection lowered nothing.
- **Null is not comparable, and projection can create a `0` where the summary is
  `null`.** Promotion writes `eval_delta: 0` unconditionally — including onto a
  move whose stored delta was `null` — so a game with no resolved player deltas
  but one promoted move displays `0` against a `null` summary. `≤` is undefined
  there; the bound is asserted only over games where both sides are non-null.

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
        "average_centipawn_loss": "integer | null"
      }
    }
  ]
}
```

History summaries follow the same player-only rule as session analysis for
blunder/mistake/inaccuracy counts and `average_centipawn_loss`. ACPL also clamps
negative eval deltas to zero, including legacy stored rows, and uses the same
half-up rounding rule as session analysis — see §5.2.2.

`average_centipawn_loss` carries the same null semantics as session analysis: it
is `null` if and only if no player move has an `eval_delta` (an unanalyzed game,
or a game with no moves at all), a partially analyzed game reports the average
over the plies that resolved, and `0` means perfect play rather than missing
data.

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

- **After move uploads:** the recompute is no longer called inline at the end of `POST /api/session/:id/moves`. That handler commits `session_moves` and enqueues the evidence side effects to a background worker (§7.4); the worker's final step calls `request_recompute()` to schedule a **coalesced** opening-score recompute off the request path (g-yjtn). The opening-score worker then runs `recompute_opening_scores_if_needed()` and, if the user's inputs (game history or opening registry) have changed since the last batch, computes a new batch.
- **After SRS reviews:** `recompute_opening_scores_if_needed()` is called after each SRS review submission, since a review pass can change per-opening accuracy.
- **On openings page load:** reads are stale-while-revalidate. A **warm** reader (batch present) calls `request_recompute()` to schedule a coalesced background convergence and serves the cached batch immediately, never blocking; only a **cold** reader (no batch yet) blocks on `refresh_now()` for the one-time initial compute. All recompute decisions — cache miss, registry drift, stale branch keys, evidence change — are consolidated in `recompute_opening_scores_if_needed()` run on the single serialized worker. The worker first computes a **cheap raw-input freshness digest** (pure SQL, no python-chess) and, when nothing has changed, serves the cached batch **without building the evidence overlay** — the per-session board reconstruction + Lichess phase divider only run on the non-fast paths. This keeps unchanged loads at ~10ms instead of paying the full overlay rebuild.

### 13.2 Batch/Cursor Pattern

Computation runs are not overwritten in-place. Instead:

1. A new `opening_score_batches` row is created with a monotonically increasing `generation`.
2. `user_opening_scores` (named-root) rows and `opening_position_scores` (direct tree-position) rows for the new batch are written from one shared calculation, in the same transaction (see §5.7.4).
3. The `opening_score_cursors` row for `(user_id, player_color)` is updated to point to the new generation.
4. Stale batches are pruned (cascading both score tables through `batch_id ON DELETE CASCADE`).

This ensures the current scores are always available atomically and reads never see a partially-computed state.

`registry_fingerprint` captures a hash of the opening registry **plus** the score-model, phase-divider, and quality-curve versions (`SCORE_MODEL_VERSION`, `DIVIDER_VERSION`, `QUALITY_VERSION`, `TAU_WC`, `TAU_CP`) and the persisted-read-model schema version (`OPENING_SCORE_CACHE_SCHEMA_VERSION`, bumped when the **set** of persisted batch read-model tables/columns changes, independent of the scoring math) at compute time. If it changes (new openings, a model/divider/curve change, or a read-model schema change), the next trigger forces a full recompute and all prior snapshots are invalidated.

`inputs_fingerprint` is the **raw-input freshness digest** (`opening_score_raw_inputs_fingerprint`) used to decide whether evidence changed. It hashes a canonical, order-independent projection of exactly the raw DB rows the evidence overlay reads — session_moves (+ game_sessions); the cache fallback's **two trusted grains** (the exact `(fen_before, move_uci)` `analysis_cache` move rows plus the columns the move-trust gate reads, AND the trusted position sources they are paired with: `position_analysis` storage winners and the legacy normalized-FEN `analysis_cache` position-fallback rows — keyed and hashed by `normalized_fen_before`, the column `resolve_trusted_positions` groups them by); ghost-target blunders/positions; and blunder_reviews — with **no overlay build and no python-chess board replay**, folded together with `registry_fingerprint` and an explicit `OPENING_EVIDENCE_INPUTS_VERSION`. Because the overlay is a pure deterministic function of these inputs, an unchanged digest provably implies identical scores, so the worker can fast-path. `OPENING_EVIDENCE_INPUTS_VERSION` must be bumped on any evidence-derivation semantic change a raw-row hash cannot see (e.g. `PASS_THRESHOLD`, quality-source precedence, the position/move trust split that selects the paired evals, FEN normalization, phase-filter application, or the digest's own SQL projection/filters); on first read after such a change (or after deploy) the stored fingerprint mismatches and self-heals with exactly one recompute per (user, color). Scoping that is broader than the overlay (all player-color session moves lacking a primary eval, not just opening premoves) is correctness-safe: it can only cause an unnecessary, never a missed, recompute. The candidate normalized FENs are derived with `normalize_fen` in Python — exactly as the runtime consumer keys `resolve_trusted_positions` — not from the nullable stored `normalized_fen_before` column, so digest key == runtime key by construction. The digest is computed **before** the overlay in `recompute_opening_scores`, so a stored fingerprint can never be newer than the scored inputs.

### 13.3 Score Semantics

- `opening_score`: **0-100 readiness** score (higher = better), computed **directly per root** with sample sufficiency folded in through an LCB mastery term and opponent breadth folded in through the calibrated coverage gate (`lcb_z=1.0`, `coverage_fold="gate"`, `coverage_live_threshold=1`) — no confidence-weighted descendant rollup.
- **Tree page semantics:** `/openings` (`OpeningsPage`) renders the chesstree.net-style **horizontal selected-branch move tree** synced to a board, replacing the earlier grid of per-root cards + hero. The page parses the URL line (§13.5 contract), drives a narrow data-flow hook (`useOpeningsTree`) over `GET /api/openings/tree`, and renders a synthesized **root "whole repertoire" card** (start-position eval; `score = —`, since the tree response carries metrics only on child nodes — a deliberate follow-up) plus one column of `OpeningTreeNodeCard` per position along the selected line. **Only the deepest selected node is expanded**; the rest of the selected path is compact + highlighted. The page owns: node selection (truncates the deeper line, pushes history), board-drop → tree sync (accept **any legal board drag**: an in-tree frontier move selects its node, any other legal move **extends the line as a user-selected "third type" move** (`g-obh5`) the backend resolver keeps and the build loop injects as a forced-navigable node; only an illegal drag snaps back), perspective switch (preserves the shared line, flips orientation immediately, refetches color-specific metrics), and URL canonicalization (only when the rendered view is **settled** for the current route — a stale response kept on screen during a refetch never rewrites the URL backward). User-selected moves are **ephemeral / URL-scoped**: a node carries `is_user_selected` only as the selected move of its own column, lives only while it is part of the current `move=` line, and has null metrics on a novel position (no DB row, no eval). The hook refetches the exact prefix when the displayed response carries any such node (rather than reusing it via the prefix no-fetch path), and `buildTreeView` drops a `is_user_selected` node whose uci ≠ the column's selected move, so a line-scoped node can never leak as a navigable sibling of a shorter prefix. Five distinct states: initial-loading skeleton, no-data banner (`batch_computed_at === null` → book-only tree, still navigable), page error + Retry, per-node missing eval (em dash, never an engine search), and child-column append error + Retry (existing columns untouched). Pure transforms (display-column build, board replay, drop→uci) live in `src/openings/treeView.ts`; the response cache + stale-request guard live in the hook. Card evals are kept **white-relative** (standard +white / −black) and rendered as-is — the per-column secondary **sort** by the *column's side-to-move* eval favorability is applied on the backend (`_OpeningTreeBuilder._sort_key`), so the frontend never flips eval signs.
- **Tree page layout (`g-tree-layout`):** The workspace is a board + a horizontally-scrolling tree canvas. Each column scrolls vertically **independently** (viewport-bounded `max-height: clamp(420px, 100dvh − chrome, …)` with a 420px usable floor) and the tree scrolls **horizontally**; the whole tree never scrolls vertically. The board is **square and sticky** (`clamp(280–380px)`, below the column floor so it never dictates column height). Connectors along the **selected path** are SVG bezier curves whose geometry is measured (`src/openings/useTreeConnectors.ts`: a rAF-coalesced `useLayoutEffect` measuring `element − canvas` rects, re-measuring on selection/column-count change, window resize, per-column scroll, and a canvas `ResizeObserver`) while **style is applied at render** from the selected child's edge metadata (`connectorStyle` in `treeView.ts`): **dashed** = book-only (`in_book && !is_observed`), **solid** = observed, **thickness** = `clamp(2, 6, 2 + log2(encounter_count + 1))`, and a **`variant` colour axis** (`default` | `selected`) where only the **selected** (third type, `is_user_selected`) edge is recoloured a distinct hue via the wrapping `<g>` `color` (so its stroke + clamp tips inherit) plus a dedicated arrowhead `<marker>` (a marker's `currentColor` resolves against the marker, not the referencing group); book vs observed is conveyed by **thickness**, not colour, so they share the `default` connector colour. A page-level **move-type legend** (`OpeningsMetricsLegend`) names the three types alongside the score metrics, and `OpeningTreeNodeCard` flags the third type with a **"Your move"** chip (the player's own off-book *game* line keeps the distinct "Off book" chip). Origin/target endpoints clamp to the column's visible band when scrolled out, signaled by reduced opacity + a tip glyph (never by toggling the dash). At `≤720px` the board stacks above the tree and un-sticks (the page scrolls normally) while the tree keeps horizontal scroll with one next column peeking.
- `confidence` and `sample_size`: quantify evidence age/sparsity around the readiness score. Sample sufficiency is already folded into `opening_score`; these remain backend/API telemetry only — **not surfaced in the opening UI** (`g-9dph` dropped the Confidence tile from `OpeningTreeNodeCard` and the `/openings` legend); the tree/lineage API responses still carry them
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

**Dormant report-stage axes & debug contract (Phase 1a, `g-report-cfg-fp` / `g-report-fold-score` / `g-drop-user-score` / `g-report-debug-api`).** `RootCalcConfig` carries three report-stage axes — `report_fold_p` (coverage-fold exponent), `report_fold_scope` (`all`/`user`), and `report_self_term` (`keep`/`drop_user`) — that are **dormant at their identity defaults** (`0.0`/`all`/`keep`): a default config is byte-identical to the pre-Phase-1 scorer, and the config fingerprint omits all three at identity, so introducing them perturbs no existing cache key (the report-scorer contract id stays `report-fold-v1`). When active, the report-time transform lives **only in `_direct_metrics`** and touches **only `opening_score`**: `report_self_term` first selects a pre-fold quality base (the ordinary aggregate node ratio for `keep`, or the child-only ratio `100·Σw·child_natural / Σw·child_perfect` for a qualifying user-turn row under `drop_user`), then the coverage fold multiplies it by `coverage_fraction ** report_fold_p` exactly once for in-scope rows. Confidence, displayed coverage, weighted_depth, and `_calc`'s recursion are untouched, so this is orthogonal to the live readiness semantics above.

The `NodeDebug`/`NodeDebugResponse` debug surface exposes this truthfully via four report-stage fields: `pre_fold_quality` (the base actually selected), `reported_score` (`pre_fold_quality × report_fold_multiplier`, i.e. the returned `opening_score`), `report_fold_multiplier` (`coverage_fraction ** report_fold_p` for an active in-scope row, else `1.0`), and `report_self_term_effective` — a shared `Literal["keep","drop_user","keep_fallback"]` the API rejects out-of-vocabulary spellings against. `keep_fallback` marks a `drop_user` user-turn row that could not take the child ratio (leaf, empty prepared-child set, or non-positive child denominator) and fell back to the ordinary ratio; opponent rows report `keep`. These fields are **null until the FEN is reported as its own row** and are back-filled idempotently on one shared mutable per-FEN object, so a descendant later reported on its own becomes non-null through the earlier root snapshots that reference it; a FEN only ever visited as a descendant stays null.

DB reference: §5.7

### 13.4 Opening card contract (all surfaces, g-d65n)

Every `OpeningTreeNodeCard` — the `/openings` move-tree cards (`kind="move"`) and the `/history` & `/play` lineage cards (`kind="family"`) — leads with the **opening name** as the header/primary line and shows the **played move list** as the secondary line:

- **Header = opening name** on every surface. On `/openings` the column header still shows the selected move (`formatMoveLabel`), so the name-led card complements it; sibling move-cards in a column may share an inherited name, disambiguated by the bold last move below.
- **Secondary = the played move list** (`buildMoveListTokens`), e.g. `1.e4 c6 2.Bc4` (White plies numbered, Black plies bare), with the **last (crossing) move bold**. Compact mode **truncates** it to one line (full text in the `title`); expanded mode **wraps** it (no truncation). The synthesized `/openings` start card shows "Starting position" with no move list; a family card whose `moves` is empty shows just the name.
- The move list is the player's **actual SAN moves** for family cards (from `SessionMove`s, `GET /api/session/{id}/openings` → `OpeningLineageItem.moves`), numbered from `SessionOpeningsResponse.start_ply` (ply of `moves[0]`, computed from `move_number`/`color` so a drill starting mid-game numbers correctly). On `/openings` the list is the selected-line prefix replayed once with chess.js, always numbered from ply 1.

The `/history` analysis footer renders an opening-lineage stack (`GameOpeningLineage`) showing the openings played in the selected game, broadest to deepest, each with its score and grade. Each entry is the `OpeningTreeNodeCard` in family mode (no SAN/eval/move-type chips, no mini board) — a compact card that expands in place to the card's expanded variant.

- **Single-action card:** Each compact card is one button. Clicking it (1) expands it in place to the expanded card and (2) selects that opening's root position on the board/MoveList/graph by jumping to the game move whose `fen_after` matches the opening key. A second click — on the expanded card's full-surface collapse overlay — collapses it. If no game move matches the opening key, the board selection is a no-op (the card still toggles).
- **In-card actions:** The link to `/openings` ("View in Openings") is passed to the card as a `footerAction` node — rendered **inside** the expanded card, raised above the collapse overlay with its clicks stopped so a tap never collapses the card (the card stays router-free; `GameOpeningLineage` owns the `Link`). The **Start Drill** button also lives in the expanded card. Start Drill navigates `/history` to `/play` with `drillSetup: { openingKey, playerColor }`.

### 13.4.1 Opening Lineage in the live/post-game panel

The same `GameOpeningLineage` component also renders in the live game chess-panel (g-8nke). The lineage stays mounted after the game ends (gated on `gameResult`) and while a drill is stopped (gated on the active game), so the post-game score signal reads against the same cards the player saw mid-game.

- **Board navigation (g-d65n, play + post-game):** selecting a card **navigates the board** to that opening's position — matching the move whose `fen` normalizes to the opening key (`handleNavigate`), mirroring `/history`. This is wired **during live play as well as after the game ends**: it only reviews a past position (`viewIndex`), exactly like clicking a past move in the MoveList or analysis graph, so it never disturbs the live game.
- **Start Drill (g-d65n, post-game only):** once the game has ended (`gameResult !== null`), the expanded card offers **Start Drill**. On `/play` it mirrors the `/openings` route-state intercept flow in place (set drill mode, seed the pending drill setup, open the setup overlay — which fetches the opening roots and resolves the selection) rather than navigating away. Gated on the game being over so it never starts a drill mid-game.

- **Inline score-diff badge (g-3gmc):** After a game or drill ends, each card shows an inline score-diff badge to its right — `+N → M` (green) when the score rose, `-N → M` (red) when it fell. The badge is computed from the **rounded** before/after (the cards display rounded scores), so it renders nothing when the rounded diff is `0` (guards against a `+0`/misleading `+1` from sub-1.0 float wobble) or when the opening is **brand-new** this session (`is_new`). The badge is a sibling of the card (never inside it) and shows in both the collapsed and expanded states; in the ~240px panel the expanded card's metrics collapse to two-up to keep the card + badge inside the column. This replaces the former standalone post-game `OpeningScoreDelta` list (removed from the post-game banner and stopped-drill actions).
- **Terminal lineage refetch:** The deltas can land before the lineage exists — a resign or fast drill-stop sets the score changes **without adding a move and with live polling already off**. To guarantee the cards exist to host the badges, the lineage's `refetchKey` bumps once when the terminal deltas arrive (`moveHistory.length + (openingScoreChanges ? 1 : 0)`), forcing exactly one extra fetch (the fetch effect ignores the poll gate). `openingScoreChanges` is the session-gated memo derived from `openingScoreDelta` (see below), so the key is driven by the *current* drill's own delta. A new game resets the session and clears the deltas back to null.

#### Session-scoped delta ownership (g-f3m4)

A delta is **owned by the session that earned it**, not by "whatever session is current when it arrives". The terminal endpoints serve a warm (possibly stale) delta immediately and `pollFreshOpeningDelta` reconciles it once the background recompute lands — but the player can start the next drill before that reconciliation resolves. Previously the poll led with a 1500ms sleep and bailed as soon as `sessionId` flipped, so clicking "Again" quickly destroyed drill A's diff before it was ever attempted.

- **Stamped slots.** The store holds `openingScoreDelta: { sessionId, items, origin }` (`origin: "terminal" | "reconciled"`) rather than a bare item list. The inline badges render `openingScoreDelta.items` **only** when its `sessionId` matches the live session, so a late arrival can never be misattributed to the next drill.
- **Immediate first attempt.** The poll's sleep is **trailing**: attempt 0 fires with no delay, removing the guaranteed blind window. Retries keep the ~1500ms cadence (≈ the scheduler's quiet window), bounded by 15 attempts and a per-request `AbortSignal.timeout`.
- **Supersede vs. abandon.** Starting a new game/drill **supersedes**: the old poll runs to completion, and a result for a session the player has left is routed to a bounded FIFO queue (cap 3, drop-oldest) surfaced as a **"last drill" toast** in its own board-area slot — never as the current drill's badges. `handleReset` **abandons**: it bumps a monotonic poll token, clears both slots, *and* aborts the in-flight loops. Both halves are needed — the token only invalidates a **commit**, and a loop whose server keeps answering `is_fresh: false` never reaches one, so without the abort it would burn its full attempt budget and hold a concurrency slot against the next drill. The token is re-checked **at commit time inside the store updater**, because an `AbortController` alone leaves a race in which the response resolves between the abort and the commit.
- **Departure marks the routing decision, not the session flip.** `setDepartingSession(id)` is called when the player commits to leaving — *before* the awaited `/start` round-trip, while `sessionId` is still the old one but its end screen is already gone. A delta reconciling in that window goes straight to the late queue, since committing it inline would render to nobody. This is what lets `beginSession` promote **nothing**: a delta still sitting in the slot was necessarily visible inline, and replaying it as a toast would show the same numbers twice. A failed start clears the mark, restoring inline delivery.
- **Atomic session flip.** `beginSession(newSessionId)` flips the session and clears the delta slot as one transition — a separate flip-then-clear would destroy a poll that resolved during the `/start` await.
- **Eviction is not invalidation.** The poll module caps concurrency at 3 and evicts the oldest loop, mirroring the queue's drop-oldest rule. An evicted loop is killed by its **own signal**, which the store's global token deliberately does not cover; the loop therefore re-checks `signal.aborted` *after* its `await`, since a response that fulfilled before the eviction still runs its continuation and would otherwise commit under a perfectly valid token.
- **One badge rule, two surfaces.** `src/utils/openingDeltaBadge.ts` owns `badgeFor`, shared by the inline badges and the toast, so a delta that renders nothing inline is never queued as a notification (an unrenderable head would otherwise block the drills behind it). Late notifications are acknowledged **by nonce**, never by session — acking by session could remove a later duplicate that was never shown.

#### Immediate card display (g-a5v3)

During live play the cards are derived **client-side from local move history**, so a card renders on the **same tick** as the move that crossed its root. The server round-trip is *not* causally ordered with the move — a move only becomes uploadable after local analysis resolves, and uploads then flush on an interval — so gating display on `GET /api/session/{id}/openings` made cards appear seconds late, or not until the *next* move re-armed the poll.

- **Local derivation:** `src/openings/deriveLiveLineage.ts` mirrors the backend's `played_opening_chain_indexed`: normalize each played FEN to the 4-field opening key (shared `normalize_fen`), skip keys absent from the root registry, dedupe **consecutive** repeats only, and keep each crossing's own move index (so a root reached, left, and re-reached keeps its own, longer SAN prefix). Order comes from the move-order walk, never from `OpeningRoot.depth`.
- **Root registry preload:** `useLiveOpeningLineage` loads `/api/openings/roots` once per app session (the in-flight promise is shared across mounts; a failure is *not* cached so it retries). If the registry is unavailable the hook falls back to the server lineage — i.e. the previous behavior, never a blank panel.
- **Merge, never replace:** the server response hydrates **scores only**, keyed on `(opening_key, crossing_index)` — *not* `opening_key` alone, which would hydrate both crossings of a repeated root from one row. A shorter or empty server lineage can never remove a locally visible card.
- **Parity is enforced, not assumed.** The two implementations of the chain walk are pinned by a generated shared fixture (`backend/scripts/gen_opening_chain_parity_fixture.py` → `src/openings/__fixtures__/openingChainParity.json`) consumed by *both* `backend/test_opening_chain_parity.py` and `src/openings/deriveLiveLineage.parity.test.ts`. Changing either walk means regenerating the fixture and fixing the other side. Coverage includes transpositions, retained non-consecutive re-crossings, consecutive-repeat dedupe, and **both** en-passant halves — a legally capturable ep square (which stays part of the key) and a **hand-injected raw FEN** carrying an ep square that cannot legally be captured. The raw injection is necessary because python-chess's `board.fen()` *and* chess.js's `fen()` both already canonicalize an uncapturable ep square to `-`, so a fixture derived purely from move replay can never carry one — leaving the `has_legal_en_passant()` gate untested. This duplicated logic is the deliberate cost of immediacy; the alternative (persisting moves structurally before analysis) was rejected as a much larger blast radius on the move-upload contract.
- **History is unchanged:** `HistoryPage` has no live local move source and continues to use the persisted server lineage.

#### Non-blocking scores (`score_status`)

`GET /api/session/{id}/openings` previously stamped scores via `load_cached_rows`, whose **cold** branch blocks on `refresh_now` (up to 5s) — delaying the whole JSON, so cards did not render unscored, they did not render at all.

- The endpoint now uses `load_cached_rows_nonblocking` (`app/opening_cache.py`), which returns `(batch, rows, scores_pending)`. Cold: return no batch immediately, never `refresh_now`. Warm: unchanged — serve the batch and call `request_recompute` **unconditionally** (that warm enqueue is load-bearing; it is the only trigger catching evidence changes with no write-path enqueue). `load_cached_rows` itself is untouched, since other `/opening` readers depend on its blocking cold behavior.
- **Cold-with-evidence vs. genuinely unscored.** `recompute_opening_scores_if_needed` bails out *without creating a batch* when `has_opening_evidence` is false, so "no batch" alone does not mean "a batch is coming". A cold read therefore checks evidence: with evidence it is **pending** (and enqueues); with **no** evidence it is **not pending** and enqueues nothing, mirroring `ensure_opening_scores`, which likewise reports this case as settled rather than building. Without this split a first-time user — whose only game is still in progress, and so is not yet eligible evidence — would sit behind a shimmer for their whole first game while the client re-scheduled recomputes the worker would decline, with each new move resetting the attempt budget.
- The cold enqueue is **guarded on `is_recompute_scheduled`** (mirroring `ensure_opening_scores`): `request_recompute` pushes the debounced deadline to `now + quiet_window`, so an unguarded enqueue from a polling reader would repeatedly postpone the very compute it is waiting on. Re-enqueueing when nothing is scheduled also retries work lost to a worker fault.
- `score_status` is resolved **independently of whether the persisted chain is empty**, and before the empty-chain return. The client derives its own lineage locally, so it can be showing a card while this (upload-lagged) server chain is still empty; a bare `"ready"` there would strand that card on "—" with nothing enqueued and no pending status to reconcile from.
- The response carries `score_status: "ready" | "pending"`. `"pending"` means the lineage is complete but every score is null and a recompute is running. A **warm** batch is always `"ready"` even with a background refresh in flight — it is displayable, and calling stale-warm "pending" would pin a permanent spinner on the common path. The client defaults an absent field to `"ready"`, so an older backend degrades to the previous behavior.
- **Client reconciliation** (`useSessionOpenings`): a `"pending"` status drives a bounded re-poll (~3s × 8) gated **only** on the status — not on `active`/`lagRepollMs`, since `HistoryPage` passes neither and `active: isGameActive` would cancel reconciliation at exactly the terminal moment the badges need scores. The interval must exceed the scheduler's 1.5s quiet window. The status is read through a **ref** and kept out of the dep array, preserving the invariant that this effect may only fetch on a timer tick, never on a dep change. On exhaustion the hook reports `"ready"` so the cards fall back to "—" instead of spinning forever. Exhaustion is keyed by the reconciliation **window** (`sessionId::refetchKey`), not the session, so a later move arms a fresh budget and can show the affordance again — keying on the session alone would leave the rest of that game permanently "ready".
- **Loading affordance:** cards render a shimmer + accessible "Score loading" label in place of the score, in both card variants, reserving the slot width so hydration does not reflow. Carried as an explicit `scorePending` prop — never inferred from `score == null`, which already means "genuinely unscored". The **terminal score pin wins**: when a delta badge is present the pinned pre-game number is shown, never a shimmer, so the badge never quotes a number that is off screen.

### 13.5 Opening Tree API (`GET /api/openings/tree`)

The chesstree.net-style horizontal move graph reads from `GET /api/openings/tree`. One request returns one hydrated **column** per position along a canonical move line, so a deep link or refresh renders in a single round trip. The endpoint does **zero per-request scoring** and **no per-request overlay rebuild**: structural shape comes from the opening graph + the persisted observed-edge read model (§5.7.5, read by bounded per-parent indexed lookups), direct metrics from the persisted batch (§5.7.4), and engine evals from `analysis_cache` (§14, via `app/tree_eval.py`). A warm read therefore scales with the rendered line/frontier size and indexed DB lookups, not with the user's total session history. The single stale-while-revalidate trigger (`ensure_tree_cache`) serves a warm-fresh batch immediately while scheduling a background recompute, and **blocks** for a one-time bootstrap only when the latest batch is cold or registry/schema-stale (predating the §5.7.5 read model) so observed moves are never hidden.

**Request.** `player_color=white|black` (required; bad color → 422). The selected line is given either as a repeated UCI param `move=e2e4&move=c7c5`, or — legacy, only when `move` is empty — as `opening=<normalized FEN>`. With neither, the line is empty (root).

**Canonicalization contract.** The response `canonical_line` is the deepest valid **legal** prefix of the request — any legal move is kept, so a move past the book/observed frontier survives as a user-selected (third type) node (`g-obh5`) instead of being truncated; the frontend caches and addresses by `(player_color, canonical_line)`.
- A **malformed** UCI token, or an `opening` FEN that fails to parse, is a client error → **422** (never a silent truncate, never a 500).
- A well-formed move **truncates** the line (canonical-URL behavior) only when it is illegal on the board (legality is checked **before** replay so a corrupt overlay edge cannot corrupt the line), lands on a position with no legal continuations (checkmate/stalemate), revisits a position (cycle), or exceeds the 80-ply ceiling. A legal move that is **not** a navigable child is **no longer** a truncation cause — it is kept as a user-selected (third type) node (`g-obh5`).
- A legacy `opening` resolves via a deterministic shortest-path book BFS (explicit visited set, UCI tie-break) **re-validated through the same move validator**, so legacy and `move` entrypoints never diverge. An `opening` that parses but is unreachable / out of graph resolves to the empty line (root), not a 404.

**Three child sets.** Per position, `_column_children ⊇ _navigable_children ⊇ _structural_children` always holds:
- `_structural_children` = observed edges (**always** — phase-authoritative) ∪ reference book children whose child is **not** a middlegame position. This is the **structural** set, identical to the scorer's domain (`opening_rootcalc._structural_children`). Only this set feeds scoring, and the transposition overlay never widens it.
- `_navigable_children` = the structural set **plus** routing-overlay edges (§17.4) whose destination is not a middlegame position, taken only when the parent is itself not a middlegame position — the overlay must never *volunteer* a move past the opening boundary (`g-openings-transpose`). Provenance is read from `overlay.children_of`, **not** `routing_children` (which unions base and overlay edges and so cannot tell them apart). An overlay edge that duplicates an observed one merges into a single card retaining its evidence.
  - **Provenance tagging is independent of that eligibility filter.** `is_transposition` is set on *any* edge the overlay carries — including one that is also observed, one crossing into the middlegame, and one out of an already-middlegame parent. Folding the tag into the filter would strip provenance from exactly those cases and render them as "Off book", i.e. as moves from the player's own games, which they are not. A node's `is_navigable` is `uci ∈ _navigable_children` **OR** the node is this column's user-selected move (a board-played legal move forced navigable for its own line only, `g-obh5`); `is_navigable` gates node-click navigation but **no longer** gates entry into `canonical_line` — any legal move (including an in-book middlegame boundary move played on the board) may enter the line as a user-selected (third type) node.
- `_column_children` = the navigable set **plus**, when the parent is itself not a middlegame, the parent's middlegame children **from the book and from the overlay alike** as **display-only, non-navigable boundary** nodes.

Transposition cards are persistent column members (they are written to the column cache and stay visible in a cached shorter-prefix view), unlike the per-line user-selected injection, which is a local copy. Their destinations join the existing **wave-two** observed-edge prefetch frontier rather than adding a third wave or per-node queries.

**Terminal reasons** (precedence, checked via the board first so a short mate is never mislabeled): `checkmate` → `stalemate` → not navigable ⇒ `opening_boundary` (a display-only middlegame book boundary) → navigable dead-end ⇒ `opening_boundary` when the child is a middlegame position else `no_children` → null. The selected position's `selected_is_terminal` / `selected_terminal_reason` are derived directly from `pos[k]`, independent of columns; a leaf deepest position yields no column `k`.

**Node hydration.** Each node carries `san`, `ply`, opening `name`/`eco` (a child without its own graph name inherits the deepest named ancestor along the line), `in_book` / `is_observed` / `is_user_selected` (a board-played legal move **outside `_navigable_children`**, valid only as its own column's selected move, `g-obh5`) / `is_transposition` (the edge comes from the routing overlay, `g-openings-transpose`) / `is_prepared`, `user_choice_count` (edge live attempts) and `encounter_count` (edge traversals), the persisted direct metrics (`opening_score`, `confidence`, `coverage`, `sample_size`, `game_count`, `last_practiced_at`; absent ⇒ no-data), `eval_cp` / `eval_mate` (**white-relative**; the card renders them as-is in the standard +white / −black convention — only the backend column sort applies the column's side-to-move favorability, the frontend never flips eval signs), `drill_opening_key` (still set only on named boundary roots, but it **no longer gates the Start Drill button** — every expanded move card is drillable; non-root / off-book cards drill via the reconstructed played line, so this field now denotes named-root identity only), and `is_selected`. In-book middlegame boundary nodes are outside the scorer domain, so their null metrics are structural-by-design.

**Move-type flags are independent, not disjoint.** `in_book`, `is_observed`, `is_transposition`, and `is_user_selected` each state one fact about the edge, and they overlap. In particular **any selected overlay edge outside `_navigable_children` carries both `is_transposition` and `is_user_selected`** — reached either by an overlay edge crossing the first middlegame boundary (in the column, displayed, not navigable) or by an overlay edge manually selected from an already-middlegame parent (not in the column at all, so it arrives via the injection path, yet `overlay.children_of` still names its provenance). Metrics follow the **destination**, not the edge's provenance: hydration is keyed by `child_fen`, so an unobserved transposition has zero *edge* counts and `is_prepared=false` but retains normal destination-position metrics whenever a row exists. **Display-only boundary cards are the exception and are enforced, not merely expected**: they sit outside the scorer domain, and a middlegame destination *can* own a cached row (reached through some other, observed move order), so hydration suppresses their scorer metrics explicitly rather than relying on the row being absent. Evals are a position property, not a scorer metric, and are still shown. Boundary membership is a property of the **position**, not of the current selection — it is derived from *persistent column member ∖ navigable set*, computed **before** the selected move is forced navigable. Deriving it from `not is_navigable` would let merely selecting a boundary card (base-book or overlay alike) restore the destination's cached row and silently break the contract.

The card resolves the overlap into **exactly one** move-type chip, by a total order: **Your move** (`is_user_selected`) → **Transposition** (`is_transposition`) → **Off book** (`!in_book`) → **Book move** (no chip). Transposition outranks Off book because an overlay edge is emphatically not a move from the player's own games; Your move outranks Transposition because at those nodes the actionable fact is that the line continues only because the user selected it past the opening boundary — the provenance stays on the wire in `is_transposition` regardless.

**Sorting** depends on the parent's side to move. **Play frequency is primary; engine eval is the secondary tie-breaker** (it never overrides frequency). On the user's turn: observed first, then most-chosen. On the opponent's turn: most-encountered first. Then **engine eval, most favorable to the column's side to move first** — the best move *for that column* floats up (a White-to-move column ranks the highest white-relative value first; a Black-to-move column ranks the lowest/most-negative first), keyed to the side to move rather than the repertoire color; a mate-0 has no recoverable winner — the white-relative count carries no sign and `_played_eval` collapses mate rows to mate-only — so it is treated as unknown and sorts last, alongside no-eval nodes. Then weakest mastery (null last). A destination/source/promotion/UCI tail makes distinct UCIs a total order.

**Color specificity.** The reference book skeleton and the white-relative eval values are color-independent; the observed node set, counts, and metrics are color-specific (the overlay filters by user **and** color), so the **primary** play-frequency sort key still differs by color. The **secondary** eval tie-break, by contrast, is keyed to the column's side to move — a position property — so it is color-independent: the same column sorts its evals identically for a white and a black repertoire. The backend holds no cross-color cache.

**Cache resolution + batched lookups** (per request, no overlay rebuild): `ensure_tree_cache` resolves the batch to serve from and fires the single stale-while-revalidate trigger (warm-fresh schedules a background recompute and serves immediately; cold or registry/schema-stale blocks once on `refresh_now` so observed edges are materialized before serving — §5.7.5); observed move edges come from `lookup_observed_edges_for_parent` via bounded per-parent indexed point queries over `opening_position_edges`; the persisted direct metrics from `lookup_position_scores_for_batch` (the same resolved `batch_id`, §5.7.4); the move-eval batch; and one root-eval for the column-0 start position. `ensure_tree_cache` captures `batch_id` / `batch_computed_at` as plain scalars **before** the request's `db.rollback()`, so the builder reads no ORM batch field afterward. The response also returns `batch_computed_at` and `model_version` (`SCORE_MODEL_VERSION`).

**Frontend `/openings` URL contract.** The canonical frontend URL is
`/openings?color=white|black` plus a repeated UCI param `move=<uci>` (one per
ply along the selected line). `src/openings/route.ts` owns this contract:
`buildOpeningsSearchParams` builds the query for **all** callers, and
`parseOpeningsSearchParams` / `buildCanonicalReplacement` parse and canonicalize
the tree route. No component hand-builds or inline-parses an `/openings` query
string.

- **Param mapping is 1:1 with the tree API request _except the color param is
  renamed_:** frontend `color` → API **`player_color`**; `move` and `opening`
  keep their names. A future tree API client must send `player_color=`, not
  `color=` (the endpoint requires `player_color` and 422s on a bad/missing one).
- `opening=<normalized FEN>` is the legacy deep-link entry, honored only when no
  `move` is present, and is rewritten to the resolved `move=` line on response
  (the frontend replaces the URL with `canonical_line` via
  `buildCanonicalReplacement`, which returns `null` — no history write — when the
  URL is already canonical).
- The legacy `openingKey`+`path` URL form has been removed (g-tree-cleanup);
  only the `opening=<fen>` deep-link entry above remains as a non-tree input, and
  it is rewritten to the canonical `move=` line on response.

---

## 14. Analysis Cache

The analysis cache avoids re-running Stockfish on positions that have already been evaluated in prior games.

### 14.1 Key Structure

Each entry is keyed by `(fen_before, move_uci)` — the exact position before a move and the move played in UCI notation. This pair uniquely identifies an analysis result. This is the *move-evidence* grain; position-level truth (best move / line / eval) is no longer authoritative here — it lives in the normalized-FEN-keyed `position_analysis` table. See **§14.6**.

### 14.2 Frontend Lookup

`lookupAnalysisCache(positions)` in `src/utils/api.ts` sends a batch `POST /api/analysis/lookup` request. It returns a `Map<string, CachedAnalysis>` keyed by `"fen::move_uci"` (only cache hits are returned). Each `CachedAnalysis` now carries the *position* grain (`best_move_uci`, `best_move_san`, `best_line_uci`, `best_eval`, `best_eval_mate`, `position_trusted`) and the *move* grain (`move_san` — nullable, `played_eval`, `played_eval_mate`, `eval_delta`, `classification`, `move_trusted`) independently, plus the cross-grain `position_eval_loss_cp`. Position-only hits (trusted position resolved, no exact `(fen, move_uci)` row) are emitted with a null `move_san`.

Used in `GameAnalysisCoordinator` and `useMoveAnalysis` alongside Stockfish analysis tasks. The completeness check is split per grain: `canResolvePositionAnalysis` requires `best_move_uci` + a multi-move `best_line_uci` whose first move matches it; `canResolveMoveAnalysis` requires an enum-valid classification + a finite played eval. The trust gates (`isTrustedPositionHit`, `isTrustedExactBestHit`, `isTrustedMoveHit`) layer `position_trusted` / `move_trusted` on top. A cache row bypasses the local engine on a grain only when its grain gate passes; otherwise the worker backfills. See §14.6.

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
even though they never pass the grain-specific trust gates (`position_trusted` /
`move_trusted`; §14.6.4).

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

### 14.6 Position-vs-Move Grain Split (g-position-analysis)

`analysis_cache` is keyed by `(fen_before, move_uci)` — the right grain for *move
evidence*, but the same row also stored *position* facts that don't depend on which
move was played. Duplicating those across sibling rows let a lower-trust browser row
redefine a position's best move for any consumer that skipped the trust contract. The
split makes position truth a separately-keyed, separately-trusted entity.

**Grain ownership.** Best move / line / eval (and its mate companion) are properties of
the *position*; everything about the played move stays move-grain.

| Datum | Grain | Key | Stored in |
|-------|-------|-----|-----------|
| `best_move_uci`, `best_move_san`, `best_line_uci`, `best_eval`, `best_eval_mate` | **position** | `normalized_fen` | `position_analysis` |
| `move_san`, `played_eval`, `played_eval_mate`, `classification`, `eval_delta` | **move** | `(fen_before, move_uci)` | `analysis_cache` |

**Storage & trust.** `position_analysis` holds one winner per `normalized_fen` (storage
tables in §5.6). Trust is computed per grain via two evidence contracts —
`position-complete-v1` (best move + multi-move PV + a CP or mate eval) and
`move-complete-v1` (played eval + classification) — surfaced as independent
`position_trusted` / `move_trusted` flags that replaced the removed `trusted_for_resolution` flag.
Position writes route through a dominance policy that mirrors the move-cache one and
**structurally rejects non-authoritative browser rows before any comparison**, so they
can never become position truth even on a new key. Backfill groups cache rows by
normalized FEN and picks one winner per group; genuine disagreements are persisted to
`position_analysis_conflicts` rather than logged.

**Eval-loss ownership.** The drill threshold loss is the backend-derived
`position_eval_loss_cp` on `/api/analysis/lookup` (position `best_eval` vs trusted move
`played_eval`, mover-relative, clamped ≥ 0). It is CP-only and emitted only when both
grains are trusted, pure-CP, and equal search strength; mate cases fall back to local
worker recompute. `analysis_cache.eval_delta` is a separate canonical-run snapshot for
blunder / SRS / display only and must not drive strict outcomes. `best_eval_mate` is
stored first-class so exact-best and `tree_eval.py` mate ranking keep forced-mate
preference.

**Wire shape & consumers.** `POST /api/analysis/lookup` (renamed from the old
`GET /api/analysis-cache`) returns both grains independently rather than one flattened
row, including position-only hits (trusted position, no exact move row — `move_san`
null). Trust-gated consumers — `tree_eval.py` **root** eval, the session drill-review
export (below), the **opening-score quality fallback** (below), and the split frontend
guards — read position facts from
`position_analysis` (legacy-v2 fallback during migration), never from raw `analysis_cache`
position columns. **Exception:** `tree_eval.py`'s **move-card** eval (`lookup_move_evals`)
carries an untrusted display fallback (§14, tiers 3-4) — when no trusted eval exists it
surfaces an untrusted `played_eval` so off-book cards still render a number. The root eval
and all position-fact reads remain strictly trust-gated. For drill grading, strictness-0 compares the played move to the trusted
`best_move_uci` alone (no move-eval needed); thresholds use `position_eval_loss_cp` or
fall back to the worker (see §6.4.6).

**Opening-score quality fallback.** When a session move lacks its primary
`session_moves` evals, `opening_evidence._apply_cache_fallbacks` upgrades it to a
win-chance quality only by pairing a **trusted position best** (`resolve_trusted_positions`
— storage winner or legacy-v2 fallback at the normalized FEN) with a **move-trusted
played eval** from the exact `(fen_before, move_uci)` row, and only when both come from
the **same search strength** (`compare_search_strength` EQUAL). It never reads the move
row's own (possibly duplicated/untrusted) `best_eval` as position truth; any failed guard
degrades deterministically to the `eval_delta` quality. The session-local
`session_moves.best_move_eval_cp` precedence above it is **retained** as user-owned
session analysis (not position truth) and is intentionally exempt from this split.

**Session-wire compatibility.** `GET /api/session/:id/analysis` keeps its full-`fen_before`
wire grain: `session.py` looks up storage by `normalize_fen(move.fen_before)`, emits each
entry under the original full FEN, and stamps an explicit `position_trusted` (untrusted
`SessionMove` seeds stay `false`). The reused `position_analysis` name is intentional —
storage is normalized-FEN-keyed, the wire map is full-FEN-keyed, and a storage row is
never returned as the session map directly.

**Migration.** `resolver-complete-v2` stays a legacy read/projection contract so existing
canonical rows keep conferring trust; new writes use the grain-specific contracts, and
full v2 deprecation is deferred to a follow-up after cutover is verified. The duplicated
best-move columns still on `analysis_cache` remain (backfill source + v2 projection) but
are no longer authoritative; dropping them is deferred to the same follow-up.

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

Drill Mode is a structured opening practice feature. The user plays toward a specific target position — a registered boundary root, **or any `/openings` tree position reached via its played line** (every expanded move card is drillable) — then optionally converts the session into a rated game from that point forward.

Card-initiated drills (ad-hoc, non-root) send the target FEN plus the full UCI line from the start position; `/api/drills/start` validates the line by replay (each move legal and the line reaching the claimed target, else `422`) and persists it as `game_sessions.drill_line` (space-joined UCI; `NULL` for registered-root drills). The session's display metadata (name/family/eco/depth) is synthesized to match the card: the deepest named book node along the line (the same name inheritance §16 uses), `depth = len(line)`.

### 17.1 Session Type

Drill sessions use `session_mode = 'drill'` in `game_sessions`. They start unrated (`is_rated = false`) and can become rated upon conversion.

### 17.2 Drill States

| State | Meaning |
|-------|---------|
| `active` | Playing toward the target opening position |
| `root_reached` | User successfully reached the target FEN |
| `failed` | User deviated from route or made an accuracy mistake post-root |
| `abandoned` | User quit the drill **without a terminal outcome** |
| `converted` | User elected to continue as a rated game after reaching root |

`drill_state` is the **outcome** record; `status`/`result`/`ended_at` are the separate
**lifecycle** record. Abandoning a drill that already `failed` ends the session
(`status='ended'`, `result='drill_abandon'`, unrated) but **preserves**
`drill_state='failed'` and its `drill_terminal_reason`. That `failed` + `status='ended'`
pairing is one natural-end already produces, so it is a proven row shape — but the two
stay distinguishable by `result`: natural-end writes `checkmate_win`/`checkmate_loss`/
`draw`, abandon writes `drill_abandon`. Only a drill with no terminal outcome yet
(`active`/`root_reached`) becomes `abandoned`. Before g-drill-failed-overwrite the abandon
write was unconditional, and
since the client abandons on every exit from a stopped drill (Analyze, Again, New Game,
Resign) it relabelled essentially every real failure — historical rows undercount
`failed` drastically.

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

**Drill setup (force-always, g-09mu):** the setup panel opens with **no
strictness tier pre-selected on every open** — fresh opens, opens with a saved
pref, and gear/Again-settings opens alike. Start Drill is gated until the user
picks a tier. The UI maps tiers to seed cps **Strict → 0 (exact-best),
Standard → 25, Lenient → 50**, then offers a fine-tune slider constrained to
the tier's band (0–15 / 16–35 / 36–50). This is a UI affordance over the
existing cp-is-source-of-truth contract: the wire `strictness` tier stays
derived from the chosen cp.

### 17.4 Route Check

`POST /api/drills/:id/route-check` is called after each move. The backend builds a `DrillRouteMap` to classify the current position, branching on whether the target is in the book graph (`route_map_for_target`):

- **In-book target** (registered roots and named cards): a **BFS-derived** map from the opening graph — transposition-tolerant, so any move order reaching an on-route position counts.
- **Off-book target** (a card's exact played line is the only route): a **strict played-line map** (`build_line_route_map`) where on-route = following the exact line, the single route-preserving suggestion is the next line move, and success = reaching the line's final position.

#### Transposition overlay

Positions are keyed by normalized FEN, so the BFS map is transposition-tolerant by
construction — but only across edges the book actually records. The graph is built by
replaying ECO move sequences and is nearly a tree, so a position recorded under one move
order carries no edges for the other orders reaching it. Queen's Gambit Declined: Normal
Defense is in book via 1.d4 d5 2.c4 e6 3.Nc3 Nf6; the English order 1.c4 e6 2.Nc3 Nf6
3.d4 d5 reaches the identical FEN but is missing the `g8f6` and `d2d4` edges.

`app/opening_densify.py` closes those gaps with a **routing-only overlay**, precomputed
offline into `public/data/openings/eco.transpositions.json` (2,141 edges) and regenerated
by `scripts/densify_opening_graph.py`. Drill routing, opponent steering, and the
`/openings` tree builder (§13.5) all read the same `RoutingView` (graph + overlay)
instead of the graph; nothing else changes.

Three distinct concerns read this data, and they must not be conflated:

| Concern | Domain | Overlay's role |
|---------|--------|----------------|
| **Scoring structure** | `_structural_children` — observed ∪ non-middlegame base-book edges | **None.** Overlay edges never enter it, so opening roots, depths, `graph.fingerprint`, cache inputs, and calibration inputs are untouched. |
| **Browsing** (`/openings` cards) | `_navigable_children` / `_column_children` | Adds forward-progress transposition cards, flagged `is_transposition` (§13.5). |
| **Drill routing / steering** | `routing_children` / `routing_parents` | Accepts and steers alternate move orders into the target. |

Both runtime consumers share one load-once, provenance-validated snapshot and one
fallback: a missing or unusable artifact logs once (naming both consumers) and
degrades to the base graph — drills stop crossing transpositions and `/openings`
stops showing transposition cards, but neither fails.

- **Not merged into the graph.** `graph.fingerprint` is derived from node children and
  gates the opening score cache and the frozen release-calibration cohort, which fails
  closed on mismatch. The overlay leaves it byte-identical.
- **Forward-progress filter.** An edge is retained only if it strictly increases
  longest-path depth over the base DAG. Every base edge does so by construction, so no
  cycle can close in the combined graph — necessary because route-check treats any
  on-route position as valid, and a cycle would let a player shuffle indefinitely without
  going off route. (Minimum root depth is *not* a valid potential: the base graph contains
  one edge, `d2f4`, running from min-depth 16 to 15.)
- **Staleness is caught in CI**, by `--check` diffing the artifact against a fresh
  recomputation — provenance proves origin, not completeness. At runtime a missing or
  stale artifact logs once and degrades to no densification.
- **Every invalid shape normalizes to `DensificationError` inside the loader**, which is
  the only exception `_build_routing_view` degrades on. A leaked `AttributeError` from a
  non-object payload would escape the routing-view singleton *uncached*, so the fallback
  would never be memoized: drill routing would 500 and every consumer would re-raise and
  re-log on each request instead of degrading once.
- Off-book targets are not in the graph, so densification cannot reach them; they keep the
  strict single-line map.

The same `route_map_for_target` selector drives opponent steering (`/api/game/next-opponent-move`) so route-check and the opponent's reply never diverge. Status classification:

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

`drill_terminal_reason` is written only by the fail paths and is **never cleared** —
including by `/continue`, which sets `drill_state='converted'` and leaves the reason in
place. So `drill_terminal_reason IS NOT NULL` means the session **experienced a failure**,
not that its outcome is `failed`. A **newly written** `abandoned` row therefore carries a
`NULL` reason, since abandon now claims the outcome slot only when no fail path has. This
is a property of the write paths, **not an invariant of the table**: the schema does not
couple `drill_terminal_reason` to `drill_state`, and rows predating
g-drill-failed-overwrite are `abandoned` while carrying `accuracy` or `off_route` — which
is exactly the shape the recovery backfill (`g-drill-failed-backfill`) targets. Do not
read `abandoned` as implying a null reason when querying historical data.

### 17.7 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/drills/start` | Create a new drill session |
| GET | `/api/drills/:id` | Fetch the drill session contract |
| POST | `/api/drills/:id/route-check` | Check current position against drill route |
| POST | `/api/drills/:id/continue` | Convert to rated game after root reached |
| POST | `/api/drills/:id/fail` | Mark drill failed (accuracy, post-root only) |
| POST | `/api/drills/:id/natural-end` | Record natural game-over during drill phase |
| POST | `/api/drills/:id/abandon` | End an unconverted drill session — **preserves** an existing `failed` outcome, only labels `abandoned` when there is none (§17.2). No-op once `status='ended'`. Use `/api/game/end` for converted drills |

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
3. The session is **ended** (`abandonStoppedDrill` → `POST /api/drills/:id/abandon`):
   `status='ended'`, `result='drill_abandon'`, unrated, hidden, game inactive. A drill that
   already failed **keeps** `drill_state='failed'` and its terminal reason (§17.2). The live
   analysis session is cleared so it idle-shuts down.
4. `/drill-analysis` renders the existing data-driven `AnalysisBoard` from the snapshot, with
   a minimal "Drill review — not saved" footer (no `GameReviewStats` — accuracy is not
   available for a transient snapshot).

The review is ephemeral: refreshing or navigating directly to `/drill-analysis` finds no
snapshot and redirects to `/play`. **No conversion, rating, history entry, or game statistics
are created.** Abandoned/failed drills stay hidden from `/api/session/:id/analysis`, history,
and normal game analysis: the shared visibility guard (`visible_session_filter()` /
`is_visible_game_session`) admits **only** `session_mode='normal'` OR
`drill_state='converted'`, so neither `failed` nor `abandoned` qualifies and hiding never
distinguishes the two. (The guard contains no `status` term at all; `/history` applies
`status='ended'` as a separate filter alongside it.) Persisting a drill review would
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
`drillStrictnessCp`) are available. The store's `drillState` here is the **client's local
finalization sentinel**, deliberately distinct from the persisted `drill_state`: the client
already sets it from inferred state without a server round trip (off-route and accuracy
failures), and a successful abandon pins it to `"abandoned"` regardless of the outcome label
the server now preserves (§17.2). Three client predicates read that sentinel — this
reviewed-return check, the stopped-drill post-game banner, and the Continue action — and all
three mean "this client finalized the drill", not "the row says abandoned". When valid, the retained board, moves, orientation, and
settings are read straight from the game store and the original drill-stopped actions
(`DrillStopActions` — the terminal-reason subtitle plus **Again**/settings) are restored;
the **Analyze** action keeps its original label but is re-wired on return to simply
re-navigate to `/drill-analysis` using the still-present snapshot (rebuilding would overwrite
the saved review with an empty map, since the live analysis session was cleared on the way
out). The generic post-game
"New game" banner is suppressed so no misleading "Drill abandoned" message appears. The board
stays disabled behind the `isGameActive === false` gate. The on-mount rating fetch does **not**
resample engine Elo while a drill context is loaded; "Again" itself resamples it, drawing
uniformly from every Maia bin rather than from the rating-centred Gaussian used by New Game, so
repeated drills of one opening face a wide spread of opponents and therefore a wide spread of
replies (g-acsr). Drills start unrated, so an out-of-band opponent costs the player no rating. The
"Back to drill" control is an in-flow row; the analysis board's viewport-driven height is
compensated by that row so the board is not pushed below the fold. The marker is consumed via
replace
navigation but the reviewed presentation persists until an explicit transition clears it
(successful drill/normal-game start, the gear opening the setup overlay, or a reset). Identity
is never inferred from opening key, moves, or reusable settings, and the abandoned backend
session is **never revived** — any mismatch or missing precondition falls back to ordinary
`/play` initialization.

DB reference: §7.3 (`game_sessions` drill columns)

---

## 18. Stats Summary Populations

`GET /api/stats/summary` (`app/api/stats.py`) reports over a `window_days` window: the
user's sessions that pass `visible_session_filter()` (normal games + converted drills,
§7.3) and whose normal play started at or after the cutoff. Within that one window, the
three numbers on the **moves** card are computed over **three different populations**.
This is deliberate, and each is pinned by a test in `test_stats_api.py`.

| Field | Grain | Population (denominator) |
|-------|-------|--------------------------|
| `quality_distribution` | **move** | Classified player moves across **all** windowed sessions — **in-progress games included** |
| `mistake_free_game_rate` | **game** | **All** ended sessions in the window |
| `accuracy_pct` | **game** | Ended sessions in the window **that scored** — i.e. whose accuracy is not `None` |

`colors.{white,black}.accuracy_pct` is the same statistic as `accuracy_pct`, scoped to
that color. All three fields (and the color-scoped ones) are `null`, never `0`, when
their population is empty — clients must null-check, never truthiness-check.

### 18.1 The grains differ on purpose

**`quality_distribution` is move-grain and counts in-progress games.** Every classified
player move is evidence about how the user is currently moving, and there is no reason to
discard the moves of a game that happens to still be open. **The game-grain metrics are
ended-only** because they are not *defined* until a game is over: a game in progress has
not yet had the chance to contain a blunder, so counting it would score every young game
as "mistake-free" and inflate the rate.

This is pinned, with its rationale, at `test_stats_api.py:243-259` — the test asserts that
an active game's clean player move **does** count toward `quality_distribution` while
**not** inflating the mistake-free denominator (2 ended games, 1 clean → `50.0`, not the
`66.7` you would get by counting the active clean game).

Note that a single scan of player moves feeds both grains: the per-session blunder counts
that decide "is this game clean" are built from the same all-sessions move rows that build
the distribution, then consulted only for ended sessions.

### 18.2 `accuracy_pct` is an unweighted mean of per-game integers

`compute_game_accuracy` returns a **rounded 0..100 integer** per game (accuracy v1,
frozen — §7.3.1). `_mean_accuracy` averages those integers and rounds again to one
decimal. Two consequences, both accepted:

- **It is double-rounded** (per-game round, then round the mean).
- **It is unweighted:** a 10-move game weighs exactly as much as a 60-move game.

Keep it that way. It answers *"how well do I play in a typical game"*, which is a
per-game question, so per-game weighting is the honest one. Decisively: that per-game
integer is **the same value g-aeq8's cached `game_sessions.player_accuracy` column will
serve** (§7.3.1). A move-weighted variant would need per-move evidence the cache does not
retain, so it would collide with the Release B read switch on arrival.

### 18.3 The accuracy denominator is "ended games that **scored**"

`_mean_accuracy` **silently drops games whose accuracy is `None`**. So `accuracy_pct` and
`mistake_free_game_rate` are both game-grain and both ended-only, and *still* do not share
a denominator: an ended game that fails to score is absent from the accuracy mean but
present (as clean) in the mistake-free rate.

That drop-arm is not a rare edge. It fires today for an ended game with no resolved evals,
and the frozen ply-coordinate guard (g-22t8.6) makes it **load-bearing**: a game whose ply
coordinates are broken scores `None` rather than a silently wrong number, and drops out of
the mean. The guard's fail-closed contract depends on this arm existing.
