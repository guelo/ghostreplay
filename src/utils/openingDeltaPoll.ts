import { captureEvent } from "../analytics/posthog";
import { useGameStore } from "../stores/useGameStore";
import { hasRenderableBadge } from "./openingDeltaBadge";
import { getOpeningScoreDelta } from "./api";
import type { OpeningScoreDeltaPollResponse } from "./api";

// Approximately the scheduler's quiet-window debounce, so retries land roughly
// one recompute cycle apart.
const DELTA_POLL_INTERVAL_MS = 1500;
// Phase 2 publishes played-root scores before the whole-batch commit, but keeps
// the scheduler's 1.5s quiet window and can still queue behind an already-running
// whole-graph job. The measured durable-terminal-to-fresh-read bound was 37.2s:
//   1 + ceil((37.2s + 2 * 1.5s) / 1.5s) = 28 attempts.
// That is a 40.5s request-start span. If every request reaches its 4s timeout,
// the full gate can last 28 * 4s + 27 * 1.5s = 152.5s (plus browser suspension).
const DELTA_POLL_MAX_ATTEMPTS = 28;
const DELTA_POLL_REQUEST_TIMEOUT_MS = 4000;
// Matches the bounded late-result queue: no more than three live computations
// are useful, regardless of how many evicted continuations are still waking.
const DELTA_POLL_MAX_CONCURRENT = 3;

export type OpeningDeltaPollTrigger =
  | "drill_accuracy_fail"
  | "drill_natural_end"
  | "game_end"
  | "game_resign"
  | "game_revert";

export type OpeningDeltaPollOutcome =
  | "fresh"
  | "attempts_exhausted"
  | "abandoned"
  | "capacity_evicted";

export type OpeningDeltaPollResult = {
  trigger: OpeningDeltaPollTrigger;
  mode: "drill" | "game";
  outcome: OpeningDeltaPollOutcome;
  elapsedMs: number;
  attemptCount: number;
  requestErrorCount: number;
  freshOnFirstAttempt: boolean;
  sessionReplacedBeforeCompletion: boolean;
  hasRenderableChange: boolean;
  visibilityAtStart: string;
  visibilityAtEnd: string;
  visibilityChanged: boolean;
};

export type OpeningDeltaPollSnapshot = {
  trigger: OpeningDeltaPollTrigger;
  elapsedMs: number;
  attemptCount: number;
  requestErrorCount: number;
};

type LoopAbortReason = "abandoned" | "capacity_evicted";

type ActivePoll = {
  promise: Promise<OpeningDeltaPollResult> | null;
  controller: AbortController;
  abortReason: LoopAbortReason | null;
  sessionId: string;
  trigger: OpeningDeltaPollTrigger;
  startedAt: number;
  attemptCount: number;
  requestErrorCount: number;
  visibilityAtStart: string;
};

// Insertion-ordered by session. Aborted entries remain until their own single
// finalizer runs; capacity accounting counts only non-aborted entries.
const runningLoops = new Map<string, ActivePoll>();

const monotonicNow = (): number =>
  typeof performance === "undefined" ? Date.now() : performance.now();

export const getOpeningDeltaVisibility = (): string =>
  typeof document === "undefined" ? "unknown" : document.visibilityState;

const pollMode = (trigger: OpeningDeltaPollTrigger): "drill" | "game" =>
  trigger.startsWith("drill_") ? "drill" : "game";

const activeLoopCount = (): number => {
  let count = 0;
  for (const poll of runningLoops.values()) {
    if (!poll.controller.signal.aborted) count += 1;
  }
  return count;
};

const oldestLiveLoop = (): ActivePoll | null => {
  for (const poll of runningLoops.values()) {
    if (!poll.controller.signal.aborted) return poll;
  }
  return null;
};

/**
 * Reconcile the terminal endpoint's warm opening-score delta to a provably-fresh
 * value. Same-session callers join one loop and therefore share its clock,
 * counters, result, and completion event.
 */
export function pollFreshOpeningDelta(
  sessionId: string,
  trigger: OpeningDeltaPollTrigger,
): Promise<OpeningDeltaPollResult> {
  const existing = runningLoops.get(sessionId);
  if (
    existing &&
    !existing.controller.signal.aborted &&
    existing.promise
  ) {
    return existing.promise;
  }

  // Eviction is asynchronous: abort now, but let the evicted loop's continuation
  // own its unavailable transition, event, result, and map cleanup.
  while (activeLoopCount() >= DELTA_POLL_MAX_CONCURRENT) {
    const oldest = oldestLiveLoop();
    if (!oldest) break;
    oldest.abortReason = "capacity_evicted";
    oldest.controller.abort("capacity_evicted");
    console.warn(
      `[OpeningDelta] Poll concurrency cap (${DELTA_POLL_MAX_CONCURRENT}); ` +
        `aborted oldest (session ${oldest.sessionId}).`,
    );
  }

  const controller = new AbortController();
  const poll: ActivePoll = {
    promise: null,
    controller,
    abortReason: null,
    sessionId,
    trigger,
    startedAt: monotonicNow(),
    attemptCount: 0,
    requestErrorCount: 0,
    visibilityAtStart: getOpeningDeltaVisibility(),
  };
  poll.promise = runDeltaPollLoop(poll);
  runningLoops.set(sessionId, poll);
  return poll.promise;
}

export function getOpeningDeltaPollSnapshot(
  sessionId: string,
): OpeningDeltaPollSnapshot | null {
  const poll = runningLoops.get(sessionId);
  if (!poll || poll.controller.signal.aborted) return null;
  return {
    trigger: poll.trigger,
    elapsedMs: Math.max(0, Math.round(monotonicNow() - poll.startedAt)),
    attemptCount: poll.attemptCount,
    requestErrorCount: poll.requestErrorCount,
  };
}

async function runDeltaPollLoop(
  poll: ActivePoll,
): Promise<OpeningDeltaPollResult> {
  const pollToken = useGameStore.getState().openingDeltaPollToken;
  let sawFresh = false;
  let hasRenderableChange = false;
  let result: OpeningDeltaPollResult | null = null;

  try {
    for (let attempt = 0; attempt < DELTA_POLL_MAX_ATTEMPTS; attempt += 1) {
      if (attempt > 0) await delay(DELTA_POLL_INTERVAL_MS);
      if (poll.controller.signal.aborted) break;

      poll.attemptCount += 1;
      let response: OpeningScoreDeltaPollResponse;
      try {
        response = await requestDeltaWithTimeout(poll);
      } catch {
        // An explicit loop abort is a terminal outcome, not a request error.
        if (poll.controller.signal.aborted) break;
        poll.requestErrorCount += 1;
        continue;
      }

      // A capacity eviction may arrive while a fulfilled response continuation
      // is queued. It must never commit under its still-valid global store token.
      if (poll.controller.signal.aborted) break;

      if (response.is_fresh) {
        const items = response.opening_score_changes ?? null;
        sawFresh = true;
        hasRenderableChange = hasRenderableBadge(items);
        useGameStore
          .getState()
          .applyPolledOpeningDelta(poll.sessionId, items, pollToken);
        break;
      }
    }
  } finally {
    // This is the only terminal path for a newly-created loop.
    result = finalizePoll(poll, pollToken, sawFresh, hasRenderableChange);
  }

  return result;
}

function finalizePoll(
  poll: ActivePoll,
  pollToken: number,
  sawFresh: boolean,
  hasRenderableChange: boolean,
): OpeningDeltaPollResult {
  const outcome: OpeningDeltaPollOutcome = sawFresh
    ? "fresh"
    : poll.abortReason === "capacity_evicted"
      ? "capacity_evicted"
      : poll.controller.signal.aborted
        ? "abandoned"
        : "attempts_exhausted";

  if (runningLoops.get(poll.sessionId) === poll) {
    runningLoops.delete(poll.sessionId);
  }

  if (outcome === "attempts_exhausted" || outcome === "capacity_evicted") {
    useGameStore
      .getState()
      .markOpeningDeltaUnavailable(poll.sessionId, pollToken);
  }

  const visibilityAtEnd = getOpeningDeltaVisibility();
  const state = useGameStore.getState();
  const completion: OpeningDeltaPollResult = {
    trigger: poll.trigger,
    mode: pollMode(poll.trigger),
    outcome,
    elapsedMs: Math.max(0, Math.round(monotonicNow() - poll.startedAt)),
    attemptCount: poll.attemptCount,
    requestErrorCount: poll.requestErrorCount,
    freshOnFirstAttempt: sawFresh && poll.attemptCount === 1,
    sessionReplacedBeforeCompletion: state.sessionId !== poll.sessionId,
    hasRenderableChange,
    visibilityAtStart: poll.visibilityAtStart,
    visibilityAtEnd,
    visibilityChanged: poll.visibilityAtStart !== visibilityAtEnd,
  };

  captureEvent("opening_delta_poll_completed", {
    trigger: completion.trigger,
    mode: completion.mode,
    outcome: completion.outcome,
    elapsed_ms: completion.elapsedMs,
    attempt_count: completion.attemptCount,
    request_error_count: completion.requestErrorCount,
    fresh_on_first_attempt: completion.freshOnFirstAttempt,
    session_replaced_before_completion:
      completion.sessionReplacedBeforeCompletion,
    has_renderable_change: completion.hasRenderableChange,
    visibility_at_start: completion.visibilityAtStart,
    visibility_at_end: completion.visibilityAtEnd,
    visibility_changed: completion.visibilityChanged,
  });

  return completion;
}

async function requestDeltaWithTimeout(
  poll: ActivePoll,
): Promise<OpeningScoreDeltaPollResponse> {
  const timeout = new AbortController();
  const timeoutId = setTimeout(
    () => timeout.abort("request_timeout"),
    DELTA_POLL_REQUEST_TIMEOUT_MS,
  );
  try {
    return await getOpeningScoreDelta(poll.sessionId, {
      signal: anySignal([poll.controller.signal, timeout.signal]),
    });
  } finally {
    clearTimeout(timeoutId);
  }
}

const delay = (milliseconds: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

/** Compose abort signals without depending on AbortSignal.any in jsdom. */
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

/** Stop every live loop as a deliberate abandonment. Finalization stays owned
 * by each loop's continuation, preserving one completion path and event. */
export function abortOpeningDeltaPolls(): void {
  for (const poll of runningLoops.values()) {
    if (poll.controller.signal.aborted) continue;
    poll.abortReason = "abandoned";
    poll.controller.abort("abandoned");
  }
}

/** Test seam: production-equivalent abandonment between cases. */
export const __resetOpeningDeltaPolls = abortOpeningDeltaPolls;
