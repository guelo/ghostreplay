import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLastDrillDeltaToast } from "./useLastDrillDeltaToast";
import { useGameStore } from "../stores/useGameStore";

const item = (key: string, before: number | null, after: number) => ({
  opening_key: key,
  opening_name: key,
  opening_family: key,
  eco: null,
  depth: 3,
  before,
  after,
  delta: before == null ? null : after - before,
  is_new: before == null,
});

/** Enqueue a late delta the way a superseded poll would. */
const queueLate = (sessionId: string, items: ReturnType<typeof item>[]) =>
  useGameStore
    .getState()
    .applyPolledOpeningDelta(
      sessionId,
      items,
      useGameStore.getState().openingDeltaPollToken,
    );

describe("useLastDrillDeltaToast", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useGameStore.setState(useGameStore.getInitialState(), true);
    useGameStore.setState({ sessionId: "current" });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("has no toast when nothing is queued", () => {
    const { result } = renderHook(() => useLastDrillDeltaToast());
    expect(result.current.toast).toBeNull();
  });

  it("surfaces the queued drill's badges", () => {
    act(() => {
      queueLate("s1", [item("Italian Game", 41, 47), item("Sicilian", 30, 30)]);
    });
    const { result } = renderHook(() => useLastDrillDeltaToast());

    // The zero-diff entry is suppressed by the shared badge rule.
    expect(result.current.toast?.badges).toEqual([
      { diff: 6, after: 47, dir: "up", openingName: "Italian Game" },
    ]);
  });

  it("acknowledges on dismiss and does not replay after a remount", () => {
    act(() => {
      queueLate("s1", [item("Italian Game", 41, 47)]);
    });
    const { result, unmount } = renderHook(() => useLastDrillDeltaToast());
    expect(result.current.toast).not.toBeNull();

    act(() => {
      result.current.dismiss();
    });
    expect(result.current.toast).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);

    unmount();
    const remounted = renderHook(() => useLastDrillDeltaToast());
    expect(remounted.result.current.toast).toBeNull();
  });

  it("auto-dismisses, then shows the next queued drill with its own window", () => {
    act(() => {
      queueLate("s1", [item("Italian Game", 41, 47)]);
      queueLate("s2", [item("Sicilian", 30, 38)]);
    });
    const { result } = renderHook(() => useLastDrillDeltaToast());
    expect(result.current.toast?.badges[0].openingName).toBe("Italian Game");

    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(result.current.toast?.badges[0].openingName).toBe("Sicilian");

    // The second notification gets a full window, not the first's remainder.
    act(() => {
      vi.advanceTimersByTime(5999);
    });
    expect(result.current.toast).not.toBeNull();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current.toast).toBeNull();
    expect(useGameStore.getState().lateOpeningDeltas).toEqual([]);
  });
});
