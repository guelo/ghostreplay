---
name: zustand-store-design
description: Preferences for zustand store design — keep stores narrow, derive cheap values, keep Chess instance and async effects outside the store
type: feedback
---

When introducing zustand stores:

1. **Keep canonical state small, derive cheap values in selectors or subscriber components.** Don't use computed-on-write as default — storing derived values like `displayedFen`, `lastMoveSquares`, `allowDragging` can drift across reset/revert/navigation/ghost moves/postgame. Reserve computed-on-write only for genuinely expensive computations or subscription isolation.

2. **Keep mutable instances (like Chess.js) outside the store.** Use a module-local singleton/controller ref or a dedicated service layer. Store actions consume results from that layer. Mixing mutation and store updates causes divergence.

3. **Keep effects in dedicated hooks, not raw `subscribe` listeners.** Hooks subscribing to narrow store selectors give rerender isolation without hiding app logic in store internals. Easier to trace and test.

4. **Preferred sequencing: render split first, store second.** Split components → extract hot-path children → introduce zustand only for cross-boundary shared state → refactor hooks last. Earlier perf checkpoint, lower rollback cost.

5. **Slice design specifics:**
   - `playerColor` → session/game state, not board
   - `allowDragging` → derived, don't store unless proven expensive
   - Keep overlay flags separate unless they share lifecycle
   - `engineStatus`/`isThinking` can stay in existing hooks initially

**Why:** Overly eager store migration creates hidden coupling, makes rollback expensive, and risks state divergence across the many game transitions (reset, revert, navigation, ghost moves, postgame).

**How to apply:** When planning zustand adoption, start with the narrowest useful store. "Move all memos/effects into the store" is the anti-pattern to avoid.
