import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useSessionAccuracy } from "./useSessionAccuracy";
import { ApiError, type SessionAnalysis } from "../utils/api";

const fetchAnalysisMock = vi.fn();

vi.mock("../utils/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../utils/api")>();
  return {
    ...actual,
    fetchAnalysis: (...args: unknown[]) => fetchAnalysisMock(...args),
  };
});

const payload = (
  accuracy: number | null,
  is_complete: boolean,
): SessionAnalysis =>
  ({
    session_id: "s1",
    pgn: null,
    result: "1-0",
    moves: [],
    summary: {
      blunders: 0,
      mistakes: 0,
      inaccuracies: 0,
      average_centipawn_loss: 12,
      accuracy,
    },
    expected_total_moves: 40,
    analyzed_moves: is_complete ? 40 : 10,
    is_complete,
    player_color: "white",
  }) as SessionAnalysis;

beforeEach(() => {
  vi.useFakeTimers();
  fetchAnalysisMock.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

/**
 * Drive time forward inside act(). RTL's waitFor cannot be used here: it polls on
 * a timer, which never fires under vitest's fake clock.
 */
const advance = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

describe("useSessionAccuracy", () => {
  it("stays idle until a session actually ends", async () => {
    const { result, rerender } = renderHook(
      ({ enabled }) => useSessionAccuracy("s1", enabled),
      { initialProps: { enabled: false } },
    );

    expect(result.current.status).toBe("idle");
    expect(fetchAnalysisMock).not.toHaveBeenCalled();

    fetchAnalysisMock.mockResolvedValue(payload(87, true));
    rerender({ enabled: true });
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
    expect(fetchAnalysisMock).toHaveBeenCalledWith("s1");
    await advance();
  });

  it("reports the accuracy once the payload completes", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(87, true));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    expect(result.current.status).toBe("pending");
    await advance();
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(87);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
  });

  // A partial payload's accuracy would move under the player as more plies land.
  it("holds the placeholder through an incomplete payload, then settles", async () => {
    fetchAnalysisMock
      .mockResolvedValueOnce(payload(40, false))
      .mockResolvedValue(payload(87, true));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    await advance();
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("pending");
    expect(result.current.accuracy).toBeNull();

    await advance(2000);
    await advance();
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(87);
  });

  it("stops polling as soon as the payload is complete", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(87, true));
    renderHook(() => useSessionAccuracy("s1", true));

    await advance();
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
    await advance(30_000);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
  });

  // accuracy fails CLOSED to null; callers must be able to tell that apart from 0.
  it("settles unavailable when the completed payload carries no accuracy", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(null, true));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    await advance();
    expect(result.current.status).toBe("unavailable");
    expect(result.current.accuracy).toBeNull();
  });

  it("settles unavailable on a permanent error without retrying", async () => {
    fetchAnalysisMock.mockRejectedValue(new ApiError("gone", { status: 404, retryable: false }));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    await advance();
    expect(result.current.status).toBe("unavailable");
    await advance(10_000);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
  });

  it("retries a transient failure and reports the value it recovers", async () => {
    fetchAnalysisMock
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValue(payload(72, true));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    await advance();
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("pending");

    await advance(2000);
    await advance();
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(72);
  });

  // "New Game" hides the post-game prompt; cancelling the overlay brings it back.
  // The analysis for an ended session is immutable, so that round trip must not
  // buy a second request — nor a chance for a later failure to erase a known
  // number.
  it("does not re-request accuracy when the surface is toggled off and back on", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(87, true));
    const { result, rerender } = renderHook(
      ({ enabled }) => useSessionAccuracy("s1", enabled),
      { initialProps: { enabled: true } },
    );

    await advance();
    expect(result.current.accuracy).toBe(87);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);

    rerender({ enabled: false });
    fetchAnalysisMock.mockRejectedValue(new Error("network"));
    rerender({ enabled: true });

    await advance(10_000);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(87);
  });

  // An UNsettled poll is different: it has no answer to keep, so re-enabling
  // must resume the work rather than leave the row on a placeholder forever.
  it("resumes the poll when the surface is toggled off before it settles", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(40, false));
    const { result, rerender } = renderHook(
      ({ enabled }) => useSessionAccuracy("s1", enabled),
      { initialProps: { enabled: true } },
    );

    await advance();
    expect(result.current.status).toBe("pending");

    rerender({ enabled: false });
    fetchAnalysisMock.mockResolvedValue(payload(72, true));
    rerender({ enabled: true });

    await advance();
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(72);
  });

  it("never attributes one session's accuracy to the next", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(87, true));
    const { result, rerender } = renderHook(
      ({ sessionId }) => useSessionAccuracy(sessionId, true),
      { initialProps: { sessionId: "s1" } },
    );

    await advance();
    expect(result.current.accuracy).toBe(87);

    fetchAnalysisMock.mockReturnValue(new Promise(() => {}));
    rerender({ sessionId: "s2" });

    expect(result.current.accuracy).toBeNull();
    expect(result.current.status).toBe("pending");
  });

  it("gives up after the poll budget and keeps the last value it saw", async () => {
    fetchAnalysisMock.mockResolvedValue(payload(64, false));
    const { result } = renderHook(() => useSessionAccuracy("s1", true));

    // 60 scheduled retries at 2s, plus the initial request.
    await advance(2000 * 61);
    await advance();
    expect(result.current.status).toBe("ready");
    expect(result.current.accuracy).toBe(64);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(61);

    await advance(20_000);
    expect(fetchAnalysisMock).toHaveBeenCalledTimes(61);
  });
});
