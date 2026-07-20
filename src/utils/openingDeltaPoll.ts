import { getOpeningScoreDelta } from "./api";
import type { OpeningScoreDeltaPollResponse } from "./api";
import { useGameStore } from "../stores/useGameStore";

// ≈ the scheduler's quiet_window debounce, so each RETRY lands roughly one
// recompute cycle apart; the max-attempts ceiling bounds total work even if the
// user lingers on the end screen.
const DELTA_POLL_INTERVAL_MS = 1500;
const DELTA_POLL_MAX_ATTEMPTS = 15; // ≈ 22s ceiling
const DELTA_POLL_REQUEST_TIMEOUT_MS = 4000;
// Matches the store's LATE_OPENING_DELTA_LIMIT: a poll that survives to commit
// needs a queue slot to land in, so holding more polls than the queue can hold
// only buys work that will be discarded.
const DELTA_POLL_MAX_CONCURRENT = 3;

/**
 * Reconcile-poll the end-of-session opening-score delta (g-fix-end-latency).
 *
 * The terminal endpoints serve a warm — possibly stale — delta immediately (no
 * multi-second scheduler wait) and enqueue a background recompute. This loop
 * polls GET /api/openings/score-delta until the server reports `is_fresh`, then
 * commits the provably-fresh value (erasing any transient over/under-statement
 * from the warm read). It also stops on `is_fresh=true` when nothing crossed /
 * the cache was already fresh.
 *
 * The first attempt fires IMMEDIATELY (the sleep is trailing, not leading): the
 * old leading sleep created a guaranteed 1500ms window in which clicking "Again"
 * destroyed the just-finished drill's reconciliation before it was ever
 * attempted (g-f3m4).
 *
 * Cancellation is no longer "bail when sessionId changes" — that discarded drill
 * A's diff outright. Two distinct transitions are modelled instead:
 *
 *   - SUPERSEDE (a new drill starts): the loop runs to completion; the store
 *     routes the result to the late-notification queue, owned by A, never into
 *     B's inline badges.
 *   - ABANDON (handleReset): the poll token is bumped and this loop's signal is
 *     aborted. The token is re-checked at COMMIT time inside the store updater —
 *     an AbortController alone leaves a race where the response resolves between
 *     the abort and the commit.
 */

type ActivePoll = { promise: Promise<void>; controller: AbortController };

// In-flight loops keyed by session, insertion-ordered. A same-session
// double-start (overlapping terminal paths) joins the running loop instead of
// spawning a second one.
const runningLoops = new Map<string, ActivePoll>();

export function pollFreshOpeningDelta(sessionId: string): Promise<void> {
  const existing = runningLoops.get(sessionId);
  if (existing) return existing.promise;

  // Overflow: abort and drop the OLDEST active poll before starting the newest,
  // mirroring the late queue's drop-oldest rule so the two layers can never
  // discard different drills.
  while (runningLoops.size >= DELTA_POLL_MAX_CONCURRENT) {
    const oldest = runningLoops.entries().next().value;
    if (!oldest) break;
    const [oldestId, poll] = oldest;
    poll.controller.abort();
    runningLoops.delete(oldestId);
    console.warn(
      `[OpeningDelta] Poll concurrency cap (${DELTA_POLL_MAX_CONCURRENT}); ` +
        `aborted oldest (session ${oldestId}).`,
    );
  }

  const controller = new AbortController();
  const promise = runDeltaPollLoop(sessionId, controller.signal).finally(() => {
    // Only remove our own entry — an overflow eviction may already have replaced
    // it with a newer poll for the same key.
    if (runningLoops.get(sessionId)?.controller === controller) {
      runningLoops.delete(sessionId);
    }
  });
  runningLoops.set(sessionId, { promise, controller });
  return promise;
}

async function runDeltaPollLoop(
  sessionId: string,
  signal: AbortSignal,
): Promise<void> {
  // Snapshot the token at request time; the commit re-checks it against the
  // token as of the commit, which is what actually closes the race.
  const pollToken = useGameStore.getState().openingDeltaPollToken;

  for (let attempt = 0; attempt < DELTA_POLL_MAX_ATTEMPTS; attempt += 1) {
    // Trailing sleep: attempt 0 fires with no delay.
    if (attempt > 0) {
      await new Promise((resolve) =>
        setTimeout(resolve, DELTA_POLL_INTERVAL_MS),
      );
    }
    if (signal.aborted) return;

    let res: OpeningScoreDeltaPollResponse;
    try {
      res = await getOpeningScoreDelta(sessionId, {
        signal: anySignal([
          signal,
          AbortSignal.timeout(DELTA_POLL_REQUEST_TIMEOUT_MS),
        ]),
      });
    } catch {
      // Abort / timeout / transient network error. An abort is terminal; a
      // timeout retries on the next tick.
      if (signal.aborted) return;
      continue;
    }

    // Re-check AFTER the await: a concurrency eviction (or an abandonment) can
    // land while the response is in flight, and a fulfilled promise still runs
    // its continuation. The store's poll token does not cover eviction — that is
    // a client-side capacity decision, not a global invalidation — so an evicted
    // loop would otherwise commit with a perfectly valid token.
    if (signal.aborted) return;

    if (res.is_fresh) {
      useGameStore
        .getState()
        .applyPolledOpeningDelta(
          sessionId,
          res.opening_score_changes ?? null,
          pollToken,
        );
      return;
    }
  }
}

/**
 * Compose abort signals. `AbortSignal.any` is not available in every runtime we
 * target (or in the jsdom test environment), so fall back to a manual relay.
 */
function anySignal(signals: AbortSignal[]): AbortSignal {
  if (typeof AbortSignal.any === "function") return AbortSignal.any(signals);
  const controller = new AbortController();
  for (const signal of signals) {
    if (signal.aborted) {
      controller.abort(signal.reason);
      break;
    }
    signal.addEventListener("abort", () => controller.abort(signal.reason), {
      once: true,
    });
  }
  return controller.signal;
}

/**
 * Stop every in-flight poll. Pairs with `abandonOpeningDeltas`: that action
 * invalidates RESULTS via the token, but a loop whose server keeps answering
 * `is_fresh: false` never reaches a commit, so without this it would burn its
 * full attempt budget and hold a concurrency slot against the next drill.
 */
export function abortOpeningDeltaPolls(): void {
  for (const poll of runningLoops.values()) poll.controller.abort();
  runningLoops.clear();
}

/** Test seam: drop all in-flight poll bookkeeping between cases. */
export const __resetOpeningDeltaPolls = abortOpeningDeltaPolls;
