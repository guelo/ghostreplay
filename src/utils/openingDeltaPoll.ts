import { getOpeningScoreDelta } from "./api";
import type { OpeningScoreDeltaPollResponse } from "./api";
import { useGameStore } from "../stores/useGameStore";

// ≈ the scheduler's quiet_window debounce, so each poll lands roughly one
// recompute cycle apart; the max-attempts ceiling bounds total work even if the
// user lingers on the end screen.
const DELTA_POLL_INTERVAL_MS = 1500;
const DELTA_POLL_MAX_ATTEMPTS = 15; // ≈ 22s ceiling
const DELTA_POLL_REQUEST_TIMEOUT_MS = 4000;

/**
 * Reconcile-poll the end-of-session opening-score delta (g-fix-end-latency).
 *
 * The terminal endpoints now serve a warm — possibly stale — delta immediately
 * (no multi-second scheduler wait) and enqueue a background recompute. This loop
 * polls GET /api/openings/score-delta until the server reports `is_fresh`, then
 * overwrites the banner in place with the provably-fresh value (erasing any
 * transient over/under-statement from the warm read). It also stops on
 * `is_fresh=true` when nothing crossed / the cache was already fresh.
 *
 * Cancellation reuses the terminal handlers' existing guard: a new game/drill
 * flips `sessionId` (and resets `openingScoreChanges`), so a late poll bails
 * before it can repopulate a stale banner. Each request carries its own
 * `AbortSignal.timeout` so a hung GET can't stall the loop.
 */
export async function pollFreshOpeningDelta(sessionId: string): Promise<void> {
  for (let attempt = 0; attempt < DELTA_POLL_MAX_ATTEMPTS; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, DELTA_POLL_INTERVAL_MS));
    // Superseded by a new game/drill — abandon without writing a stale banner.
    if (useGameStore.getState().sessionId !== sessionId) return;

    let res: OpeningScoreDeltaPollResponse;
    try {
      res = await getOpeningScoreDelta(sessionId, {
        signal: AbortSignal.timeout(DELTA_POLL_REQUEST_TIMEOUT_MS),
      });
    } catch {
      // Timeout / transient network error — retry on the next tick.
      continue;
    }

    if (useGameStore.getState().sessionId !== sessionId) return;
    if (res.is_fresh) {
      useGameStore
        .getState()
        .setOpeningScoreChanges(res.opening_score_changes ?? null);
      return;
    }
  }
}
