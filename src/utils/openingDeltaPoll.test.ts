import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Spy on the network helper so the poll never touches fetch; the loop's own
// timers are driven with fake timers.
const getOpeningScoreDeltaMock = vi.fn();
vi.mock("./api", () => ({
  getOpeningScoreDelta: (...args: unknown[]) => getOpeningScoreDeltaMock(...args),
}));

import {
  abortOpeningDeltaPolls,
  pollFreshOpeningDelta,
  __resetOpeningDeltaPolls,
} from "./openingDeltaPoll";
import { useGameStore } from "../stores/useGameStore";
import type { OpeningScoreDeltaItem } from "./api";

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

    const done = pollFreshOpeningDelta("s1");
    await settle();
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("commits the fresh delta to the current slot, stamped and reconciled", async () => {
    const items = [makeItem("k1", 44)];
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: items,
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1");
    await settle();
    await done;

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items,
      origin: "reconciled",
    });
  });

  it("keeps polling on the retry cadence while is_fresh is false", async () => {
    const fresh = [makeItem("k1", 50)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1");
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

    const done = pollFreshOpeningDelta("s1");
    await settle();
    await done;

    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "s1",
      items: fresh,
      origin: "reconciled",
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

    const done = pollFreshOpeningDelta("s1");
    await settle();
    await done;

    const state = useGameStore.getState();
    expect(state.openingScoreDelta).toBeNull();
    expect(state.lateOpeningDeltas).toHaveLength(1);
    expect(state.lateOpeningDeltas[0]).toMatchObject({
      sessionId: "s1",
      items: fresh,
      origin: "reconciled",
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

    const done = pollFreshOpeningDelta("s1");
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

    const done = pollFreshOpeningDelta("s1");
    await settle();
    // One extra tick past the ceiling to prove the loop stops on its own.
    for (let i = 0; i < 31; i += 1) {
      await tick();
    }
    await done;

    // Matches DELTA_POLL_MAX_ATTEMPTS (~45s ceiling; raised from 15 in
    // g-drill-delta-latency's cheap fallback).
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(30);
    expect(useGameStore.getState().openingScoreDelta).toBeNull();
  });

  it("joins the in-flight loop on a same-session double-start", async () => {
    const fresh = [makeItem("k1", 55)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const p1 = pollFreshOpeningDelta("s1");
    const p2 = pollFreshOpeningDelta("s1");
    expect(p2).toBe(p1);

    await settle();
    await tick();
    await p1;

    // A single loop's request count — the double-start added none.
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreDelta?.items).toEqual(fresh);
  });

  it("starts a new loop for the same session after the previous one completes", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: true,
    });

    const first = pollFreshOpeningDelta("s1");
    await settle();
    await first;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);

    const second = pollFreshOpeningDelta("s1");
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

    const s1loop = pollFreshOpeningDelta("s1");
    await settle(); // s1 attempt 0 — not fresh, keeps looping

    useGameStore.getState().beginSession("s2");
    const s2loop = pollFreshOpeningDelta("s2");
    await settle();
    await s2loop;
    await tick(); // s1's retry lands fresh
    await s1loop;

    const state = useGameStore.getState();
    // s2 owns the inline slot; s1's late diff is queued for its own toast.
    expect(state.openingScoreDelta).toEqual({
      sessionId: "s2",
      items: s2fresh,
      origin: "reconciled",
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

    const a = pollFreshOpeningDelta("a");
    const b = pollFreshOpeningDelta("b");
    const c = pollFreshOpeningDelta("c");
    await settle();
    const callsBefore = getOpeningScoreDeltaMock.mock.calls.length;

    // The 4th poll evicts "a".
    const d = pollFreshOpeningDelta("d");
    await settle();
    await tick();

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
    await Promise.all([a, b, c, d]);
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

    const a = pollFreshOpeningDelta("a");
    await settle(); // "a" is registered and parked on its request

    pollFreshOpeningDelta("b");
    pollFreshOpeningDelta("c");
    pollFreshOpeningDelta("d"); // hits the cap -> evicts "a"
    await settle();

    releaseA({ opening_score_changes: fresh, is_fresh: true });
    await settle();
    await a;

    expect(useGameStore.getState().openingScoreDelta).toBeNull();
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

    const done = pollFreshOpeningDelta("s1");
    await settle();
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);

    useGameStore.getState().abandonOpeningDeltas();
    abortOpeningDeltaPolls();

    await tick(); // the parked sleep wakes and observes the abort
    await done;
    await tick();

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
  });

  it("retries after a rejected (timed-out) request instead of failing", async () => {
    const fresh = [makeItem("k1", 33)];
    getOpeningScoreDeltaMock
      .mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"))
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1");
    await settle(); // attempt 0 rejects -> caught, continue
    await tick(); // attempt 1 resolves fresh
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreDelta?.items).toEqual(fresh);
  });
});
