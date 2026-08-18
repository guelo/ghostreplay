import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Spy on the network helper so the poll never touches fetch; the loop's own
// timers are driven with fake timers.
const getOpeningScoreDeltaMock = vi.fn();
const captureEventMock = vi.fn();
vi.mock("./api", () => ({
  getOpeningScoreDelta: (...args: unknown[]) => getOpeningScoreDeltaMock(...args),
}));
vi.mock("../analytics/posthog", () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

import {
  abortOpeningDeltaPolls,
  getOpeningDeltaPollSnapshot,
  pollFreshOpeningDelta,
  __resetOpeningDeltaPolls,
} from "./openingDeltaPoll";
import { useGameStore } from "../stores/useGameStore";
import type {
  OpeningScoreDeltaItem,
  OpeningScoreDeltaPollResponse,
} from "./api";

const makeItem = (key: string, after: number): OpeningScoreDeltaItem => ({
  opening_key: key,
  opening_name: key,
  opening_family: key,
  eco: null,
  depth: 1,
  before: null,
  after,
  delta: null,
  is_new: true,
});

// The first attempt fires with NO delay (the sleep is trailing), so a bare
// microtask flush completes it.
const settle = () => vi.advanceTimersByTimeAsync(0);
// One retry interval (1500ms): drives exactly one further loop iteration.
const tick = () => vi.advanceTimersByTimeAsync(1500);

describe("pollFreshOpeningDelta", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getOpeningScoreDeltaMock.mockReset();
    captureEventMock.mockReset();
    __resetOpeningDeltaPolls();
    useGameStore.setState(useGameStore.getInitialState(), true);
    useGameStore.setState({ sessionId: "s1" });
  });

  afterEach(() => {
    __resetOpeningDeltaPolls();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fires the first attempt immediately, with no timer advance", async () => {
    // Regression for the guaranteed 1500ms blind window: the old leading sleep
    // meant clicking "Again" quickly destroyed the reconciliation before it was
    // ever attempted (g-f3m4).
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: [makeItem("k1", 44)],
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    const result = await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(result).toMatchObject({
      trigger: "game_end",
      mode: "game",
      outcome: "fresh",
      elapsedMs: 0,
      attemptCount: 1,
      requestErrorCount: 0,
      freshOnFirstAttempt: true,
      sessionReplacedBeforeCompletion: false,
      hasRenderableChange: true,
    });
    expect(captureEventMock).toHaveBeenCalledTimes(1);
    expect(captureEventMock).toHaveBeenCalledWith(
      "opening_delta_poll_completed",
      expect.objectContaining({
        trigger: "game_end",
        mode: "game",
        outcome: "fresh",
        elapsed_ms: 0,
        attempt_count: 1,
        request_error_count: 0,
        fresh_on_first_attempt: true,
        session_replaced_before_completion: false,
        has_renderable_change: true,
      }),
    );
    const completionProperties = captureEventMock.mock.calls[0][1] as Record<
      string,
      unknown
    >;
    expect(Object.keys(completionProperties).sort()).toEqual(
      [
        "attempt_count",
        "elapsed_ms",
        "fresh_on_first_attempt",
        "has_renderable_change",
        "mode",
        "outcome",
        "request_error_count",
        "session_replaced_before_completion",
        "trigger",
        "visibility_at_end",
        "visibility_at_start",
        "visibility_changed",
      ].sort(),
    );
  });

  it("commits the fresh delta to the current slot, stamped and reconciled", async () => {
    const items = [makeItem("k1", 44)];
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: items,
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await done;

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items,
      freshness: "fresh",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it("keeps polling on the retry cadence while is_fresh is false", async () => {
    const fresh = [makeItem("k1", 50)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle(); // attempt 0 — immediate, not fresh
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    await tick(); // attempt 1 — one interval later
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    await tick(); // attempt 2 — fresh
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(3);
    expect(useGameStore.getState().openingScoreDelta?.items).toEqual(fresh);
  });

  it("overwrites a warm terminal delta with the fresh polled value", async () => {
    const stale = [makeItem("k1", 40)];
    useGameStore.getState().setTerminalOpeningDelta("s1", stale);
    const fresh = [makeItem("k1", 47)];
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: fresh,
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await done;

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items: fresh,
      freshness: "fresh",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
  });

  it("routes a superseded session's fresh delta to the late queue, not the current slot", async () => {
    // THE core regression: drill A reconciles after the player clicked "Again".
    // Its diff belongs to A — surfaced as a late notification, never dropped and
    // never rendered as B's inline badges.
    const fresh = [makeItem("k1", 60)];
    getOpeningScoreDeltaMock.mockImplementation(async () => {
      useGameStore.getState().beginSession("s2");
      return { opening_score_changes: fresh, is_fresh: true };
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await done;

    const state = useGameStore.getState();
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toHaveLength(1);
    expect(state.lateOpeningDeltas[0]).toMatchObject({
      sessionId: "s1",
      items: fresh,
      freshness: "fresh",
    });
  });

  it("drops a fresh delta whose poll was abandoned mid-flight (token race)", async () => {
    // handleReset while the request is on the wire. An AbortController alone
    // would not catch this: the response resolves between abort and commit. The
    // token is re-checked inside the store updater, at commit time.
    const fresh = [makeItem("k1", 60)];
    getOpeningScoreDeltaMock.mockImplementation(async () => {
      useGameStore.getState().abandonOpeningDeltas();
      return { opening_score_changes: fresh, is_fresh: true };
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await done;

    const state = useGameStore.getState();
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toEqual([]);

    // ...and both slots stay empty through the next session start.
    useGameStore.getState().beginSession("s2");
    expect(useGameStore.getState().openingScoreDelta).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("stops after the max attempts when the cache never goes fresh", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: false,
    });

    useGameStore.getState().setTerminalOpeningDelta("s1", null);
    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    // One extra tick past the ceiling to prove the loop stops on its own.
    for (let i = 0; i < 29; i += 1) {
      await tick();
    }
    const result = await done;

    // Matches the Phase-2 measured-bound formula in openingDeltaPoll.ts.
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(28);
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      sessionId: "s1",
      freshness: "unavailable",
    });
    expect(result).toMatchObject({
      outcome: "attempts_exhausted",
      elapsedMs: 40_500,
      attemptCount: 28,
      requestErrorCount: 0,
    });
    expect(captureEventMock).toHaveBeenCalledTimes(1);
  });

  it("joins the in-flight loop on a same-session double-start", async () => {
    const fresh = [makeItem("k1", 55)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const p1 = pollFreshOpeningDelta("s1", "game_end");
    const p2 = pollFreshOpeningDelta("s1", "game_resign");
    expect(p2).toBe(p1);

    await settle();
    await tick();
    const result = await p1;

    // A single loop's request count — the double-start added none.
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreDelta?.items).toEqual(fresh);
    expect(result.trigger).toBe("game_end");
    expect(captureEventMock).toHaveBeenCalledTimes(1);
  });

  it("replaces an older boundary token and drops its fulfilled continuation", async () => {
    useGameStore.setState({ isGameActive: true });
    let resolveOld!: (value: {
      opening_score_changes: OpeningScoreDeltaItem[];
      is_fresh: boolean;
    }) => void;
    const oldResponse = new Promise<{
      opening_score_changes: OpeningScoreDeltaItem[];
      is_fresh: boolean;
    }>((resolve) => {
      resolveOld = resolve;
    });
    const newer = [makeItem("k1", 52)];
    getOpeningScoreDeltaMock
      .mockReturnValueOnce(oldResponse)
      .mockResolvedValueOnce({
        opening_score_changes: newer,
        is_fresh: true,
      });

    const oldPoll = pollFreshOpeningDelta(
      "s1",
      "game_opening_boundary",
      { boundaryToken: "a".repeat(64) },
    );
    await settle();
    const newPoll = pollFreshOpeningDelta(
      "s1",
      "game_opening_boundary",
      { boundaryToken: "b".repeat(64) },
    );
    await settle();
    await newPoll;

    resolveOld({
      opening_score_changes: [makeItem("k1", 99)],
      is_fresh: true,
    });
    await settle();
    expect((await oldPoll).outcome).toBe("abandoned");
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      items: newer,
      source: "opening_boundary",
      reconciliationToken: "b".repeat(64),
    });
    expect(getOpeningScoreDeltaMock).toHaveBeenNthCalledWith(
      2,
      "s1",
      expect.objectContaining({ boundaryToken: "b".repeat(64) }),
    );
  });

  it("lets terminal ownership preempt a boundary loop", async () => {
    useGameStore.setState({ isGameActive: true });
    let resolveBoundary!: (value: OpeningScoreDeltaPollResponse) => void;
    getOpeningScoreDeltaMock
      .mockReturnValueOnce(
        new Promise<OpeningScoreDeltaPollResponse>((resolve) => {
          resolveBoundary = resolve;
        }),
      )
      .mockResolvedValueOnce({
        opening_score_changes: [makeItem("k1", 61)],
        is_fresh: true,
      });

    const boundary = pollFreshOpeningDelta(
      "s1",
      "game_opening_boundary",
      { boundaryToken: "c".repeat(64) },
    );
    await settle();
    useGameStore
      .getState()
      .setTerminalOpeningDelta("s1", [makeItem("k1", 55)]);
    const terminal = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await terminal;
    resolveBoundary({
      opening_score_changes: [makeItem("k1", 99)],
      is_fresh: true,
    });
    await settle();
    expect((await boundary).outcome).toBe("abandoned");
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      items: [makeItem("k1", 61)],
      source: "terminal",
    });
  });

  it("does not start boundary work after terminal ownership has settled", async () => {
    useGameStore.setState({ isGameActive: true });
    useGameStore
      .getState()
      .setTerminalOpeningDelta("s1", [makeItem("k1", 61)]);

    const result = await pollFreshOpeningDelta(
      "s1",
      "game_opening_boundary",
      { boundaryToken: "d".repeat(64) },
    );

    expect(result).toMatchObject({ outcome: "abandoned", attemptCount: 0 });
    expect(getOpeningScoreDeltaMock).not.toHaveBeenCalled();
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      source: "terminal",
      items: [makeItem("k1", 61)],
    });
  });

  it("exposes only active low-cardinality timing metadata in snapshots", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: false,
    });

    const done = pollFreshOpeningDelta("s1", "drill_accuracy_fail");
    await settle();
    await vi.advanceTimersByTimeAsync(725);

    expect(getOpeningDeltaPollSnapshot("s1")).toEqual({
      trigger: "drill_accuracy_fail",
      elapsedMs: 725,
      attemptCount: 1,
      requestErrorCount: 0,
    });
    expect(Object.keys(getOpeningDeltaPollSnapshot("s1")!)).toEqual([
      "trigger",
      "elapsedMs",
      "attemptCount",
      "requestErrorCount",
    ]);

    abortOpeningDeltaPolls();
    expect(getOpeningDeltaPollSnapshot("s1")).toBeNull();
    await vi.advanceTimersByTimeAsync(775);
    await done;
  });

  it("starts a new loop for the same session after the previous one completes", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: true,
    });

    const first = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    await first;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);

    const second = pollFreshOpeningDelta("s1", "game_end");
    expect(second).not.toBe(first);
    await settle();
    await second;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
  });

  it("lets an old session's loop run to completion alongside a new one", async () => {
    // The old loop is no longer cancelled by the session flip — that cancellation
    // is what used to lose drill A's diff entirely.
    const s1fresh = [makeItem("k1", 30)];
    const s2fresh = [makeItem("k2", 61)];
    let s1Calls = 0;
    getOpeningScoreDeltaMock.mockImplementation(async (sid: unknown) => {
      if (sid === "s2") return { opening_score_changes: s2fresh, is_fresh: true };
      s1Calls += 1;
      return s1Calls > 1
        ? { opening_score_changes: s1fresh, is_fresh: true }
        : { opening_score_changes: null, is_fresh: false };
    });

    const s1loop = pollFreshOpeningDelta("s1", "game_end");
    await settle(); // s1 attempt 0 — not fresh, keeps looping

    useGameStore.getState().beginSession("s2");
    const s2loop = pollFreshOpeningDelta("s2", "game_end");
    await settle();
    await s2loop;
    await tick(); // s1's retry lands fresh
    await s1loop;

    const state = useGameStore.getState();
    // s2 owns the inline slot; s1's late diff is queued for its own toast.
    expect(state.openingScoreDelta).toEqual({
      sessionId: "s2",
      items: s2fresh,
      freshness: "fresh",
      source: "terminal",
      reconciliationToken: expect.any(String),
    });
    expect(state.lateOpeningDeltas).toHaveLength(1);
    expect(state.lateOpeningDeltas[0].sessionId).toBe("s1");
  });

  it("aborts and drops the oldest active poll when the concurrency cap is hit", async () => {
    // Overflow must mirror the late queue's drop-oldest rule, or the two layers
    // discard different drills.
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: false,
    });
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    useGameStore.setState({ sessionId: "a" });
    useGameStore.getState().setTerminalOpeningDelta("a", null);
    const a = pollFreshOpeningDelta("a", "game_end");
    const b = pollFreshOpeningDelta("b", "game_end");
    const c = pollFreshOpeningDelta("c", "game_end");
    await settle();
    const callsBefore = getOpeningScoreDeltaMock.mock.calls.length;

    // The 4th poll evicts "a".
    const d = pollFreshOpeningDelta("d", "game_end");
    await settle();
    await tick();

    const aResult = await a;
    expect(aResult.outcome).toBe("capacity_evicted");
    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      sessionId: "a",
      freshness: "unavailable",
    });
    expect(
      captureEventMock.mock.calls.filter(
        ([event, properties]) =>
          event === "opening_delta_poll_completed" &&
          (properties as { outcome?: string }).outcome === "capacity_evicted",
      ),
    ).toHaveLength(1);

    // "a" made no further requests after eviction; b/c/d kept going.
    const aCallsAfter = getOpeningScoreDeltaMock.mock.calls.filter(
      (call, index) => index >= callsBefore && call[0] === "a",
    );
    expect(aCallsAfter).toHaveLength(0);
    expect(warn).toHaveBeenCalled();

    // Drain: aborted loops are parked in their trailing sleep and only observe
    // the abort on the next wake, so the fake clock has to advance once more.
    __resetOpeningDeltaPolls();
    await tick();
    await Promise.all([b, c, d]);
  });

  it("does not commit a result whose poll was evicted while the response was in flight", async () => {
    // Eviction is a client-side capacity decision, so the store's poll token is
    // still valid — only the loop's own signal marks it dead. Without a re-check
    // after the await, a fulfilled response still runs its continuation and
    // commits for a drill the cap already gave up on.
    const fresh = [makeItem("k1", 44)];
    let releaseA!: (res: unknown) => void;
    const aResponse = new Promise((resolve) => {
      releaseA = resolve;
    });
    getOpeningScoreDeltaMock.mockImplementation((sid: unknown) =>
      sid === "a"
        ? aResponse
        : Promise.resolve({ opening_score_changes: null, is_fresh: false }),
    );
    vi.spyOn(console, "warn").mockImplementation(() => {});

    useGameStore.setState({ sessionId: "a" });
    useGameStore.getState().setTerminalOpeningDelta("a", null);
    const a = pollFreshOpeningDelta("a", "game_end");
    await settle(); // "a" is registered and parked on its request

    pollFreshOpeningDelta("b", "game_end");
    pollFreshOpeningDelta("c", "game_end");
    pollFreshOpeningDelta("d", "game_end"); // hits the cap -> evicts "a"
    await settle();

    releaseA({ opening_score_changes: fresh, is_fresh: true });
    await settle();
    await a;

    expect(useGameStore.getState().openingScoreDelta).toMatchObject({
      sessionId: "a",
      freshness: "unavailable",
    });
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });

  it("stops retrying once the polls are aborted, freeing the concurrency slot", async () => {
    // handleReset's token bump only invalidates a COMMIT. A loop the server keeps
    // answering `is_fresh: false` never commits, so without an explicit abort it
    // would burn its full attempt budget and hold a slot against the next drill.
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: false,
    });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle();
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);

    useGameStore.getState().abandonOpeningDeltas();
    abortOpeningDeltaPolls();

    await tick(); // the parked sleep wakes and observes the abort
    const result = await done;
    await tick();

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
    expect(result.outcome).toBe("abandoned");
    expect(captureEventMock).toHaveBeenCalledTimes(1);
  });

  it("retries after a rejected (timed-out) request instead of failing", async () => {
    const fresh = [makeItem("k1", 33)];
    getOpeningScoreDeltaMock
      .mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"))
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1", "game_end");
    await settle(); // attempt 0 rejects -> caught, continue
    await tick(); // attempt 1 resolves fresh
    const result = await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreDelta?.items).toEqual(fresh);
    expect(result).toMatchObject({
      outcome: "fresh",
      attemptCount: 2,
      requestErrorCount: 1,
      freshOnFirstAttempt: false,
    });
  });

  it("measures the full 152.5-second timeout-heavy attempt budget", async () => {
    useGameStore.getState().setTerminalOpeningDelta("s1", null);
    getOpeningScoreDeltaMock.mockImplementation(
      (_sessionId: unknown, options: { signal: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          const rejectOnAbort = () =>
            reject(new DOMException("aborted", "AbortError"));
          if (options.signal.aborted) {
            rejectOnAbort();
            return;
          }
          options.signal.addEventListener("abort", rejectOnAbort, { once: true });
        }),
    );

    const done = pollFreshOpeningDelta("s1", "game_end");
    await vi.advanceTimersByTimeAsync(152_500);
    const result = await done;

    expect(result).toMatchObject({
      outcome: "attempts_exhausted",
      elapsedMs: 152_500,
      attemptCount: 28,
      requestErrorCount: 28,
    });
    expect(useGameStore.getState().openingScoreDelta?.freshness).toBe(
      "unavailable",
    );
  });
});
