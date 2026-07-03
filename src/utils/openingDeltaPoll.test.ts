import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Spy on the network helper so the poll never touches fetch; the loop's own
// timers are driven with fake timers.
const getOpeningScoreDeltaMock = vi.fn();
vi.mock("./api", () => ({
  getOpeningScoreDelta: (...args: unknown[]) => getOpeningScoreDeltaMock(...args),
}));

import { pollFreshOpeningDelta } from "./openingDeltaPoll";
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

// One poll interval (1500ms): advance the faked clock and flush the awaited
// request microtask so a single loop iteration completes.
const tick = () => vi.advanceTimersByTimeAsync(1500);

describe("pollFreshOpeningDelta", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getOpeningScoreDeltaMock.mockReset();
    useGameStore.setState(useGameStore.getInitialState(), true);
    useGameStore.setState({ sessionId: "s1" });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("writes the fresh delta to the store and stops on the first is_fresh response", async () => {
    const items = [makeItem("k1", 44)];
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: items,
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1");
    await tick();
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledWith(
      "s1",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(useGameStore.getState().openingScoreChanges).toEqual(items);
  });

  it("keeps polling while is_fresh is false and applies the value once it flips fresh", async () => {
    const fresh = [makeItem("k1", 50)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1");
    await tick(); // attempt 1 — not fresh, no write
    expect(useGameStore.getState().openingScoreChanges).toBeNull();
    await tick(); // attempt 2 — still not fresh
    await tick(); // attempt 3 — fresh
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(3);
    expect(useGameStore.getState().openingScoreChanges).toEqual(fresh);
  });

  it("overwrites a stale immediate delta with the fresh polled value (reconcile)", async () => {
    // The terminal handler already wrote a warm/possibly-stale value; the poll
    // must correct it once the provably-fresh delta lands.
    const stale = [makeItem("k1", 40)];
    useGameStore.setState({ openingScoreChanges: stale });
    const fresh = [makeItem("k1", 47)];
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: fresh,
      is_fresh: true,
    });

    const done = pollFreshOpeningDelta("s1");
    await tick();
    await done;

    expect(useGameStore.getState().openingScoreChanges).toEqual(fresh);
    expect(useGameStore.getState().openingScoreChanges).not.toEqual(stale);
  });

  it("bails without writing when the session is superseded before the response", async () => {
    const fresh = [makeItem("k1", 60)];
    getOpeningScoreDeltaMock.mockImplementation(async () => {
      // A new game/drill started while this request was in flight.
      useGameStore.setState({ sessionId: "s2" });
      return { opening_score_changes: fresh, is_fresh: true };
    });

    const done = pollFreshOpeningDelta("s1");
    await tick();
    await done;

    expect(useGameStore.getState().openingScoreChanges).toBeNull();
  });

  it("stops after the max attempts when the cache never goes fresh", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: false,
    });

    const done = pollFreshOpeningDelta("s1");
    // Drive well past the 15-attempt ceiling.
    for (let i = 0; i < 20; i += 1) {
      await tick();
    }
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(15);
    expect(useGameStore.getState().openingScoreChanges).toBeNull();
  });

  it("joins the in-flight loop on a same-session double-start", async () => {
    const fresh = [makeItem("k1", 55)];
    getOpeningScoreDeltaMock
      .mockResolvedValueOnce({ opening_score_changes: null, is_fresh: false })
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    // Overlapping terminal paths both start the poll for the same session.
    const p1 = pollFreshOpeningDelta("s1");
    const p2 = pollFreshOpeningDelta("s1");
    expect(p2).toBe(p1);

    await tick(); // attempt 1 — not fresh
    await tick(); // attempt 2 — fresh
    await p1;

    // A single loop's request count — the double-start added none.
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreChanges).toEqual(fresh);
  });

  it("starts a new loop for the same session after the previous one completes", async () => {
    getOpeningScoreDeltaMock.mockResolvedValue({
      opening_score_changes: null,
      is_fresh: true,
    });

    const first = pollFreshOpeningDelta("s1");
    await tick();
    await first;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(1);

    // The guard cleared on completion: a later call polls again.
    const second = pollFreshOpeningDelta("s1");
    expect(second).not.toBe(first);
    await tick();
    await second;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
  });

  it("does not block a new session's loop while an old session's loop is mid-flight", async () => {
    const s2fresh = [makeItem("k2", 61)];
    getOpeningScoreDeltaMock.mockImplementation(async (sid: unknown) =>
      sid === "s2"
        ? { opening_score_changes: s2fresh, is_fresh: true }
        : { opening_score_changes: null, is_fresh: false },
    );

    const s1loop = pollFreshOpeningDelta("s1");
    await tick(); // s1 attempt 1 — not fresh, keeps looping

    // A new game takes over mid-flight.
    useGameStore.setState({ sessionId: "s2" });
    const s2loop = pollFreshOpeningDelta("s2");
    expect(s2loop).not.toBe(s1loop);

    // s1 bails on its store check without a request; s2 commits its fresh delta.
    await tick();
    await s2loop;
    await s1loop;
    expect(useGameStore.getState().openingScoreChanges).toEqual(s2fresh);
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2); // 1× s1, 1× s2

    // Both guards are clear afterwards: a fresh s2 call polls again.
    const again = pollFreshOpeningDelta("s2");
    expect(again).not.toBe(s2loop);
    await tick();
    await again;
    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(3);
  });

  it("retries after a rejected (timed-out) request instead of failing", async () => {
    const fresh = [makeItem("k1", 33)];
    getOpeningScoreDeltaMock
      .mockRejectedValueOnce(new DOMException("timed out", "TimeoutError"))
      .mockResolvedValueOnce({ opening_score_changes: fresh, is_fresh: true });

    const done = pollFreshOpeningDelta("s1");
    await tick(); // attempt 1 rejects -> caught, continue
    await tick(); // attempt 2 resolves fresh
    await done;

    expect(getOpeningScoreDeltaMock).toHaveBeenCalledTimes(2);
    expect(useGameStore.getState().openingScoreChanges).toEqual(fresh);
  });
});
