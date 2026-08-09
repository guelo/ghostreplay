import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSessionOpenings } from "./useSessionOpenings";
import type {
  OpeningLineageItem,
  OpeningPlayerColor,
  OpeningScoreStatus,
  SessionOpeningsResponse,
} from "../utils/api";

const fetchSessionOpeningsMock = vi.fn();

vi.mock("../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/api")>();
  return {
    ...actual,
    fetchSessionOpenings: (...args: unknown[]) =>
      fetchSessionOpeningsMock(...args),
  };
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (err: unknown) => void;
}

function createDeferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let pending: Array<Deferred<SessionOpeningsResponse>> = [];

function makeItem(overrides: Partial<OpeningLineageItem>): OpeningLineageItem {
  return {
    opening_key: "k",
    opening_name: "Opening",
    opening_family: "Family",
    eco: null,
    depth: 0,
    score: 60,
    confidence: 0.5,
    coverage: 0.5,
    sample_size: 5,
    game_count: 2,
    path: [],
    moves: [],
    ...overrides,
  };
}

function response(
  keys: string[],
  playerColor: OpeningPlayerColor = "white",
  startPly = 1,
  scoreStatus: OpeningScoreStatus = "ready",
): SessionOpeningsResponse {
  return {
    player_color: playerColor,
    lineage: keys.map((opening_key, depth) =>
      makeItem({ opening_key, opening_name: opening_key, depth }),
    ),
    start_ply: startPly,
    score_status: scoreStatus,
  };
}

// Flush pending microtasks (promise .then/.finally chains) under fake timers.
async function flush() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

async function resolveFetch(index: number, resp: SessionOpeningsResponse) {
  pending[index].resolve(resp);
  await flush();
}

async function rejectFetch(index: number, err: unknown = new Error("boom")) {
  pending[index].reject(err);
  await flush();
}

describe("useSessionOpenings", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    pending = [];
    fetchSessionOpeningsMock.mockReset();
    fetchSessionOpeningsMock.mockImplementation(() => {
      const d = createDeferred<SessionOpeningsResponse>();
      pending.push(d);
      return d.promise;
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("does not fetch when sessionId is null", async () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) =>
        useSessionOpenings(id, { refetchKey: 0 }),
      { initialProps: { id: null as string | null } },
    );
    await flush();

    expect(fetchSessionOpeningsMock).not.toHaveBeenCalled();
    expect(result.current.lineage).toEqual([]);

    // Becomes enabled once a session is supplied.
    rerender({ id: "a" });
    await flush();
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);
  });

  it("fetches once and exposes the lineage + playerColor + startPly", async () => {
    const { result } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    await resolveFetch(0, response(["k1", "k2"], "black", 3));

    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual([
      "k1",
      "k2",
    ]);
    expect(result.current.playerColor).toBe("black");
    expect(result.current.startPly).toBe(3);
  });

  it("refetches when refetchKey changes", async () => {
    const { rerender } = renderHook(
      ({ key }: { key: string }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: '[1,false,0]' } },
    );
    await resolveFetch(0, response(["k1"]));
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);

    rerender({ key: '[1,false,1]' });
    await flush();
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the prior lineage on screen during a same-session refetch", async () => {
    const { result, rerender } = renderHook(
      ({ key }: { key: number }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: 1 } },
    );
    await resolveFetch(0, response(["k1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);

    // New fetch in flight (not yet resolved): prior lineage stays visible.
    rerender({ key: 2 });
    await flush();
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);

    await resolveFetch(1, response(["k1", "k2"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual([
      "k1",
      "k2",
    ]);
  });

  it("does not schedule delayed upload-lag fetches after a ready response", async () => {
    renderHook(() =>
      useSessionOpenings("a", { refetchKey: '[1,false,0]' }),
    );
    await resolveFetch(0, response(["k1"]));

    // The former upload-lag window fetched at 1.5s and 3.0s. A ready score may
    // keep bounded request-free score-watch ticks alive, but it must issue no
    // network traffic without a causal refetchKey change.
    await advance(12000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);
  });

  it("yields [] immediately on a session change and ignores a late old-session response", async () => {
    const { result, rerender } = renderHook(
      ({ id, key }: { id: string; key: number }) =>
        useSessionOpenings(id, { refetchKey: key }),
      { initialProps: { id: "a", key: 0 } },
    );
    // Establish session a's stack.
    await resolveFetch(0, response(["a1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["a1"]);

    // A slow same-session refetch goes in flight (call index 1).
    rerender({ id: "a", key: 1 });
    await flush();
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["a1"]);

    // Switch to session b: lineage drops to [] synchronously (no flash of a).
    rerender({ id: "b", key: 0 });
    await flush();
    expect(result.current.lineage).toEqual([]);

    // The stale session-a response (call index 1) must NOT overwrite b's state.
    await resolveFetch(1, response(["a1", "a2"]));
    expect(result.current.lineage).toEqual([]);

    // b's own fetch (call index 2) commits.
    await resolveFetch(2, response(["b1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["b1"]);
  });

  it("ignores a slow OLDER response that resolves after a newer one (out-of-order)", async () => {
    const { result, rerender } = renderHook(
      ({ key }: { key: number }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: 1 } },
    );
    await flush(); // call index 0 issued (shallow, will resolve LATE)

    rerender({ key: 2 });
    await flush(); // call index 1 issued (deep, resolves FIRST)

    // The deeper/newer response resolves first.
    await resolveFetch(1, response(["k1", "k2"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual([
      "k1",
      "k2",
    ]);

    // The older/shallower response resolves later — must be discarded.
    await resolveFetch(0, response(["k1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual([
      "k1",
      "k2",
    ]);
  });

  it("commits [] on the first error but retains prior data on a transient error", async () => {
    const { result, rerender } = renderHook(
      ({ key }: { key: number }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: 1 } },
    );
    // First load fails with no prior data -> [].
    await rejectFetch(0);
    expect(result.current.lineage).toEqual([]);

    // Establish data on the next refetch.
    rerender({ key: 2 });
    await resolveFetch(1, response(["k1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);

    // A subsequent transient error keeps the established stack.
    rerender({ key: 3 });
    await rejectFetch(2);
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);
  });

  it("loads the lineage under StrictMode (setup/cleanup/setup)", async () => {
    const { result } = renderHook(
      () => useSessionOpenings("a", { refetchKey: 1 }),
      { wrapper: StrictMode },
    );
    await flush();

    // StrictMode runs the fetch effect twice in dev; the first request is
    // aborted on cleanup and the second loads the data. A key-diff skip-guard
    // would mark the key fetched on the aborted pass and never load it.
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
    for (const d of pending) {
      d.resolve(response(["k1"]));
    }
    await flush();
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);
  });

  it("aborts the in-flight request on unmount", async () => {
    const { unmount } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    await flush();

    const signal = fetchSessionOpeningsMock.mock.calls[0][1].signal as AbortSignal;
    expect(signal.aborted).toBe(false);

    unmount();
    expect(signal.aborted).toBe(true);

    // A response after unmount must not throw / commit.
    await resolveFetch(0, response(["k1"]));
  });

  // -------------------------------------------------------------------------
  // Score reconciliation for a cold score cache (g-a5v3)
  // -------------------------------------------------------------------------

  const pendingResponse = (keys: string[]) =>
    response(keys, "white", 1, "pending");

  it("surfaces scoreStatus and defaults to ready", async () => {
    const { result } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    await resolveFetch(0, response(["k1"]));
    expect(result.current.scoreStatus).toBe("ready");
  });

  it("re-polls a pending status without a live-game gate", async () => {
    // History and the post-game board share this reconciliation; a cold cache
    // must resolve independently of the live upload-commit signal.
    const { result } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    await resolveFetch(0, pendingResponse(["k1"]));
    expect(result.current.scoreStatus).toBe("pending");

    await advance(3000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);

    // Scores land -> status clears and the cycle goes quiet.
    await resolveFetch(1, response(["k1"]));
    expect(result.current.scoreStatus).toBe("ready");

    await advance(60000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
  });

  it("reconciles when the first response resolves AFTER the first tick", async () => {
    // Regression guard: the status ref still reads the initial "ready" at the
    // first tick, so a cycle that stopped on a non-pending tick would never
    // start reconciling a slow cold response.
    const { result } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    // Tick fires while the initial request is still in flight.
    await advance(3000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);

    await resolveFetch(0, pendingResponse(["k1"]));
    expect(result.current.scoreStatus).toBe("pending");

    // The cycle is still alive and now sees the pending status.
    await advance(3000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
  });

  it("stops after the attempt cap and clears the pending status", async () => {
    const { result } = renderHook(() =>
      useSessionOpenings("a", { refetchKey: 1 }),
    );
    await resolveFetch(0, pendingResponse(["k1"]));

    // Eight bounded attempts, each staying pending.
    for (let attempt = 1; attempt <= 8; attempt += 1) {
      await advance(3000);
      expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(attempt + 1);
      await resolveFetch(attempt, pendingResponse(["k1"]));
      expect(result.current.scoreStatus).toBe("pending");
    }

    // Cap reached: the next tick gives up rather than polling forever...
    await advance(3000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(9);
    // ...and reports "ready" so consumers drop the loading affordance and
    // render the cards as unscored instead of spinning indefinitely.
    expect(result.current.scoreStatus).toBe("ready");

    await advance(60000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(9);
  });

  it("does not leak a pending status across a session change", async () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useSessionOpenings(id, { refetchKey: 1 }),
      { initialProps: { id: "a" } },
    );
    await resolveFetch(0, pendingResponse(["k1"]));
    expect(result.current.scoreStatus).toBe("pending");

    // Session change: the guard must report the NEW session's default, not the
    // previous game's pending state.
    rerender({ id: "b" });
    expect(result.current.scoreStatus).toBe("ready");
    expect(result.current.lineage).toEqual([]);
  });

  it("restores the loading affordance on a later move in the SAME session", async () => {
    // Exhaustion is keyed by the reconciliation WINDOW (session + refetchKey),
    // not the session: a later move arms a fresh budget, so the cards must be
    // able to show the loading state again rather than being permanently
    // "ready" for the rest of the game.
    const { result, rerender } = renderHook(
      ({ key }: { key: number }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: 1 } },
    );
    await resolveFetch(0, pendingResponse(["k1"]));
    for (let attempt = 1; attempt <= 8; attempt += 1) {
      await advance(3000);
      await resolveFetch(attempt, pendingResponse(["k1"]));
    }
    await advance(3000);
    expect(result.current.scoreStatus).toBe("ready"); // window exhausted

    // A new move -> new refetchKey -> new window.
    rerender({ key: 2 });
    const calls = fetchSessionOpeningsMock.mock.calls.length;
    await resolveFetch(calls - 1, pendingResponse(["k1", "k2"]));

    expect(result.current.scoreStatus).toBe("pending");
    await advance(3000);
    expect(fetchSessionOpeningsMock.mock.calls.length).toBe(calls + 1);
  });

  it("restores a fresh reconciliation budget on a session change", async () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useSessionOpenings(id, { refetchKey: 1 }),
      { initialProps: { id: "a" } },
    );
    await resolveFetch(0, pendingResponse(["k1"]));
    // Burn session a's whole budget.
    for (let attempt = 1; attempt <= 8; attempt += 1) {
      await advance(3000);
      await resolveFetch(attempt, pendingResponse(["k1"]));
    }
    await advance(3000);
    expect(result.current.scoreStatus).toBe("ready"); // exhausted

    rerender({ id: "b" });
    const callsAfterSwitch = fetchSessionOpeningsMock.mock.calls.length;
    await resolveFetch(callsAfterSwitch - 1, pendingResponse(["k9"]));
    // The new session is pending, not stuck behind the old session's exhaustion.
    expect(result.current.scoreStatus).toBe("pending");
    await advance(3000);
    expect(fetchSessionOpeningsMock.mock.calls.length).toBe(callsAfterSwitch + 1);
  });

});
