import { captureEvent } from "../analytics/posthog";
import { useGameStore } from "../stores/useGameStore";
import { hasRenderableBadge } from "./openingDeltaBadge";
import { getOpeningScoreDelta } from "./api";
import type { OpeningScoreDeltaPollResponse } from "./api";
import type { OpeningDeltaSource } from "../stores/useGameStore";

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
  | "game_revert"
  | "game_opening_boundary"
  | "drill_opening_boundary";

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
  source: OpeningDeltaSource;
  reconciliationToken: string;
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

const abandonedBeforeStart = (
  sessionId: string,
  trigger: OpeningDeltaPollTrigger,
): Promise<OpeningDeltaPollResult> => {
  const visibility = getOpeningDeltaVisibility();
  return Promise.resolve({
    trigger,
    mode: pollMode(trigger),
    outcome: "abandoned",
    elapsedMs: 0,
    attemptCount: 0,
    requestErrorCount: 0,
    freshOnFirstAttempt: false,
    sessionReplacedBeforeCompletion:
      useGameStore.getState().sessionId !== sessionId,
    hasRenderableChange: false,
    visibilityAtStart: visibility,
    visibilityAtEnd: visibility,
    visibilityChanged: false,
  });
};

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
 * Reconcile either a proven live boundary or terminal endpoint warm value to a
 * provably-fresh score. Equivalent owners join one loop; terminal ownership
 * always supersedes live-boundary ownership for the same session.
 */
export function pollFreshOpeningDelta(
  sessionId: string,
  trigger: OpeningDeltaPollTrigger,
  options?: { boundaryToken?: string },
): Promise<OpeningDeltaPollResult> {
  const source: OpeningDeltaSource = trigger.endsWith("_opening_boundary")
    ? "opening_boundary"
    : "terminal";
  const state = useGameStore.getState();
  if (source === "opening_boundary") {
    if (!options?.boundaryToken) {
      throw new Error("Boundary delta polling requires a reconciliation token");
    }
    state.setBoundaryOpeningDeltaPending(sessionId, options.boundaryToken);
  }
  const owner = useGameStore.getState().openingScoreDelta;
  const reconciliationToken =
    source === "opening_boundary"
      ? options!.boundaryToken!
      : owner?.sessionId === sessionId && owner.source === "terminal"
        ? owner.reconciliationToken
        : `terminal:detached:${sessionId}`;

  const existing = runningLoops.get(sessionId);
  if (
    source === "opening_boundary" &&
    (owner?.sessionId !== sessionId ||
      owner.source !== source ||
      owner.reconciliationToken !== reconciliationToken)
  ) {
    if (
      existing &&
      !existing.controller.signal.aborted &&
      existing.source === "terminal" &&
      existing.promise
    ) {
      return existing.promise;
    }
    if (existing && !existing.controller.signal.aborted) {
      existing.abortReason = "abandoned";
      existing.controller.abort("terminal_owner");
    }
    return abandonedBeforeStart(sessionId, trigger);
  }
  if (
    existing &&
    !existing.controller.signal.aborted &&
    existing.promise
  ) {
    if (
      existing.source === source &&
      (source === "terminal" ||
        existing.reconciliationToken === reconciliationToken)
    ) {
      return existing.promise;
    }
    if (source === "opening_boundary" && existing.source === "terminal") {
      return existing.promise;
    }
    existing.abortReason = "abandoned";
    existing.controller.abort("superseded");
  }

  if (source === "opening_boundary") {
    const currentOwner = useGameStore.getState().openingScoreDelta;
    if (
      currentOwner?.sessionId !== sessionId ||
      currentOwner.source !== source ||
      currentOwner.reconciliationToken !== reconciliationToken
    ) {
      return abandonedBeforeStart(sessionId, trigger);
    }
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
    source,
    reconciliationToken,
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
          .applyPolledOpeningDelta(
            poll.sessionId,
            items,
            pollToken,
            poll.source,
            poll.reconciliationToken,
          );
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
      .markOpeningDeltaUnavailable(
        poll.sessionId,
        pollToken,
        poll.source,
        poll.reconciliationToken,
      );
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
      ...(poll.source === "opening_boundary"
        ? { boundaryToken: poll.reconciliationToken }
        : {}),
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

/** Cancel only a provisional boundary loop; terminal reconciliation survives. */
export function abortOpeningBoundaryDeltaPoll(
  sessionId: string,
  reconciliationToken?: string,
): void {
  const poll = runningLoops.get(sessionId);
  if (
    !poll ||
    poll.controller.signal.aborted ||
    poll.source !== "opening_boundary" ||
    (reconciliationToken !== undefined &&
      poll.reconciliationToken !== reconciliationToken)
  ) {
    return;
  }
  poll.abortReason = "abandoned";
  poll.controller.abort("abandoned");
}

/** Test seam: production-equivalent abandonment between cases. */
export const __resetOpeningDeltaPolls = abortOpeningDeltaPolls;
