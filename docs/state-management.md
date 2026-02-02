# Frontend State Management Decision

## Context

The client orchestrates two Stockfish workers, the chessboard UI, and future calls to the coordinator API (`SPEC.md`). We therefore have two distinct state domains:

- **Local & volatile state** – current FEN, move list, timers, UI toggles (auto-rotate, flips), worker readiness, and transient error banners. These must update synchronously with chessboard events and web worker messages.
- **Server-synchronized state** – Ghost suggestions, queued blunders, authentication/session info, and SRS metadata that lives in PostgreSQL and is accessed via the FastAPI coordinator.

The state solution must stay lightweight for now but scale to multi-view routing and background syncing during Milestone 1.

## Decision

Adopt **Zustand** for local client state and **TanStack Query** for server data. This pairing keeps the baseline bundle size very small (~1 KB + adapters) while still enabling normalized selectors, optimistic updates, and background refetch when API endpoints ship.

## Rationale

### Zustand (local/stateful UI)

- Minimal boilerplate: stores are plain functions, so we can colocate slices next to features (e.g., `gameStore`, `uiStore`) without ceremony.
- Excellent TypeScript inference and middleware support (devtools, immer) when we start debugging Ghost mode edge cases.
- Works nicely with Web Workers: state updates are simple function calls, so dispatching from worker message handlers does not require thunk middleware.
- Keeps bundle lean; avoids shipping Redux Toolkit when Milestone 1 only needs a few stores.

### TanStack Query (server state)

- Separates caching/fetch lifecycles from the UI, allowing the Ghost suggestion queue or user profile to stay fresh in the background.
- Built-in retry, exponential backoff, and mutation helpers simplify the error handling strategy task (`ghostreplay-sq8`).
- Plays well with Zustand—use Zustand for local chessboard data, and derive selectors from TanStack Query for API responses to keep each concern isolated.

## Alternatives Considered

### Redux Toolkit

- **Pros:** Batteries-included devtools, Immer baked in, standard pattern for large teams.
- **Cons:** Adds ~10 KB before middleware, requires boilerplate slices, and async thunks are heavier than our current needs. Web worker bridging would still need custom middleware.

### React Context + `useReducer`

- **Pros:** Zero dependencies, straightforward for very small trees.
- **Cons:** Context updates cause unnecessary re-renders across the chessboard, which hurts performance during fast move sequences. Scaling to multiple contexts (game, UI, session) becomes unwieldy, and there is no built-in devtool story.

## Next Steps

1. Install dependencies: `npm install zustand @tanstack/react-query`.
2. Create an initial `useGameStore` for board state + worker readiness, and a `QueryClientProvider` in `src/main.tsx`.
3. Document store boundaries in `SPEC.md` once the API surfaces are finalized so future tasks can extend the pattern consistently.
