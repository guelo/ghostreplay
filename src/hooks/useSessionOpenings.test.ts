import { StrictMode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useSessionOpenings } from "./useSessionOpenings";
import type {
  OpeningLineageItem,
  OpeningPlayerColor,
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
): SessionOpeningsResponse {
  return {
    player_color: playerColor,
    lineage: keys.map((opening_key, depth) =>
      makeItem({ opening_key, opening_name: opening_key, depth }),
    ),
    start_ply: startPly,
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
      ({ key }: { key: number }) =>
        useSessionOpenings("a", { refetchKey: key }),
      { initialProps: { key: 1 } },
    );
    await resolveFetch(0, response(["k1"]));
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(1);

    rerender({ key: 2 });
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

  it("re-polls a bounded number of times while active, then goes quiet", async () => {
    const { result } = renderHook(() =>
      useSessionOpenings("a", {
        refetchKey: 1,
        lagRepollMs: 1500,
        active: true,
      }),
    );
    // First (shallow) response from the immediate fetch.
    await resolveFetch(0, response(["k1"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual(["k1"]);

    // Re-poll tick 1 fires a new request; the deeper lineage is now available.
    // The next tick is armed only in this fetch's .finally(), so each poll
    // fetch must settle before advancing to the next tick.
    await advance(1500);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
    await resolveFetch(1, response(["k1", "k2"]));
    expect(result.current.lineage.map((i) => i.opening_key)).toEqual([
      "k1",
      "k2",
    ]);

    // Re-poll tick 2 — the last of the bounded chain.
    await advance(1500);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(3);
    await resolveFetch(2, response(["k1", "k2"]));

    // Chain exhausted: no further requests no matter how long we wait.
    await advance(30000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(3);
  });

  it("re-arms the bounded re-poll when refetchKey changes", async () => {
    const { rerender } = renderHook(
      ({ key }: { key: number }) =>
        useSessionOpenings("a", {
          refetchKey: key,
          lagRepollMs: 1500,
          active: true,
        }),
      { initialProps: { key: 1 } },
    );
    // Exhaust the first arm: immediate fetch + 2 re-poll ticks.
    await resolveFetch(0, response(["k1"]));
    await advance(1500);
    await resolveFetch(1, response(["k1"]));
    await advance(1500);
    await resolveFetch(2, response(["k1"]));
    await advance(30000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(3);

    // A key bump (new move) re-arms: another immediate fetch + 2 more ticks.
    rerender({ key: 2 });
    await flush();
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(4);
    await resolveFetch(3, response(["k1", "k2"]));
    await advance(1500);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(5);
    await resolveFetch(4, response(["k1", "k2"]));
    await advance(1500);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(6);
    await resolveFetch(5, response(["k1", "k2"]));

    // Second chain exhausted too.
    await advance(30000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(6);
  });

  it("stops re-polling when active flips false without firing a new fetch", async () => {
    const { rerender } = renderHook(
      ({ active }: { active: boolean }) =>
        useSessionOpenings("a", {
          refetchKey: 1,
          lagRepollMs: 4000,
          active,
        }),
      { initialProps: { active: true } },
    );
    await resolveFetch(0, response(["k1"]));
    await advance(4000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
    await resolveFetch(1, response(["k1"]));

    // Flipping active false must NOT fire a fetch (finding C) and must tear down
    // the timer so no further ticks happen.
    rerender({ active: false });
    await flush();
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
    await advance(20000);
    expect(fetchSessionOpeningsMock).toHaveBeenCalledTimes(2);
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
});
