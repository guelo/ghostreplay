import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  determineOpponentMove,
  useOpponentMove,
} from "./useOpponentMove";
import type { OpponentMoveCommittedObserver } from "../components/chess-game/domain/opponentPresentation";

const getNextOpponentMoveMock = vi.fn();
const nonCommit = { committed: false, reason: "inactive" } as const;

vi.mock("../utils/api", () => ({
  getNextOpponentMove: (...args: unknown[]) =>
    getNextOpponentMoveMock(...args),
}));

/** Helper to build a NextOpponentMoveResponse-shaped object. */
const backendResponse = (
  mode: "ghost" | "engine",
  san: string,
  targetBlunderId: number | null = null,
  decisionSource: "ghost_path" | "backend_engine" = mode === "ghost"
    ? "ghost_path"
    : "backend_engine",
  targetBlunderSrs: { last_reviewed_at: string | null; created_at: string | null; pass_count: number; fail_count: number; pass_streak: number } | null = null,
  targetFen: string | null = null,
) => ({
  mode,
  move: { uci: san === "Nf3" ? "g1f3" : "e2e4", san },
  target_blunder_id: targetBlunderId,
  target_blunder_srs: targetBlunderSrs,
  target_fen: targetFen,
  decision_source: decisionSource,
  drill_route: null,
});

describe("determineOpponentMove", () => {
  beforeEach(() => {
    getNextOpponentMoveMock.mockReset();
  });

  it("returns ghost mode when ghost move is available", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "e4", 42)
    );

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toEqual({
      mode: "ghost",
      move: "e4",
      targetBlunderId: 42,
      targetBlunderSrs: null,
      targetFen: null,
      decisionSource: "ghost_path",
      drillRoute: null,
      decisionId: null,
    });
    expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
      "session-123",
      "test-fen",
      [],
    );
  });

  it("returns engine mode with move from backend", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("engine", "e4")
    );

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toEqual({
      mode: "engine",
      move: "e4",
      targetBlunderId: null,
      targetBlunderSrs: null,
      targetFen: null,
      decisionSource: "backend_engine",
      drillRoute: null,
      decisionId: null,
    });
  });

  it("returns null on API error (triggers local fallback)", async () => {
    getNextOpponentMoveMock.mockRejectedValueOnce(new Error("Network error"));

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toBeNull();
  });
});

describe("useOpponentMove", () => {
  beforeEach(() => {
    getNextOpponentMoveMock.mockReset();
  });

  it.each([true, false])("keeps expiry failures for drills and permits normal fallback=%s", async (fallback) => {
    const failure = { status: 410, details: { error_code: "OPPONENT_SESSION_EXPIRED" } };
    getNextOpponentMoveMock.mockReset().mockRejectedValue(failure);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);
    const onBackendFailure = vi.fn();
    const { result } = renderHook(() => useOpponentMove({
      sessionId: "session", onApplyBackendMove: vi.fn(), onApplyLocalFallback,
      shouldUseLocalFallback: () => fallback, onBackendFailure,
    }));
    await act(async () => { await result.current.applyOpponentMove("fen"); });
    if (fallback) {
      expect(onApplyLocalFallback).toHaveBeenCalledTimes(1);
      expect(onBackendFailure).not.toHaveBeenCalled();
    } else {
      expect(onApplyLocalFallback).not.toHaveBeenCalled();
      expect(onBackendFailure).toHaveBeenCalledWith(failure);
    }
  });

  it("ignores late errors when the session or position guard rejects the response", async () => {
    let reject!: (error: unknown) => void;
    getNextOpponentMoveMock.mockReset().mockReturnValue(new Promise((_, r) => { reject = r; }));
    const canApplyResult = vi.fn().mockReturnValue(true);
    const onBackendFailure = vi.fn();
    const onApplyLocalFallback = vi.fn();
    const { result } = renderHook(() => useOpponentMove({
      sessionId: "old", canApplyResult, onApplyBackendMove: vi.fn(),
      onApplyLocalFallback, shouldUseLocalFallback: () => false, onBackendFailure,
    }));
    const pending = result.current.applyOpponentMove("old-fen");
    canApplyResult.mockReturnValue(false);
    await act(async () => { reject(new Error("expired")); await pending; });
    expect(onBackendFailure).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
    await result.current.applyOpponentMove("new-fen");
    expect(getNextOpponentMoveMock).toHaveBeenCalledTimes(1);
  });

  it("initializes with engine presentation", () => {
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove: vi.fn(),
        onApplyLocalFallback: vi.fn(),
      })
    );

    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
  });

  it("threads decision_id through to onApplyBackendMove", async () => {
    // The id names the row a root confirmation validates against, so it has to
    // survive the whole hop from response to the move-applying callback.
    getNextOpponentMoveMock.mockResolvedValueOnce({
      ...backendResponse("ghost", "Nf3", null),
      decision_id: "decision-abc",
      drill_route: {
        status: "root_pending",
        target_fen: "target-fen",
        resulting_fen: "resulting-fen",
        plies_to_target: 0,
        reaches_root: true,
      },
    });

    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback: vi.fn(),
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(onApplyBackendMove).toHaveBeenCalledWith(
      "Nf3",
      "ghost_path",
      null,
      null,
      null,
      expect.objectContaining({ status: "root_pending", reaches_root: true }),
      "decision-abc",
      expect.any(Function),
    );
  });

  it("applies ghost move from backend", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "Nf3", 42)
    );

    const onApplyBackendMove = vi.fn(async (...args: unknown[]) => {
      const onCommitted = args.at(-1) as OpponentMoveCommittedObserver;
      onCommitted({ drill: null });
      return { committed: true } as const;
    });
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation).toEqual({
      kind: "targeted_ghost",
      targetBlunderId: 42,
      targetBlunderSrs: null,
      targetFen: null,
    });
    expect(onApplyBackendMove).toHaveBeenCalledWith(
      "Nf3",
      "ghost_path",
      42,
      null,
      null,
      null,
      null,
      expect.any(Function),
    );
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("publishes only when the move commits and stays published while application settles", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "Nf3", 42),
    );
    let signalCommit!: OpponentMoveCommittedObserver;
    let settleApplication!: () => void;
    const committedOutcome = () => ({ committed: true }) as const;
    const onApplyBackendMove = vi.fn(
      (...args: unknown[]) => {
        signalCommit = args.at(-1) as OpponentMoveCommittedObserver;
        return new Promise<ReturnType<typeof committedOutcome>>((resolve) => {
          settleApplication = () => resolve(committedOutcome());
        });
      },
    );
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback: vi.fn(),
      }),
    );

    let pending!: Promise<void>;
    await act(async () => {
      pending = result.current.applyOpponentMove("test-fen");
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(onApplyBackendMove).toHaveBeenCalledTimes(1);
    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });

    act(() => {
      signalCommit({ drill: null });
    });
    expect(result.current.opponentPresentation.kind).toBe("targeted_ghost");

    await act(async () => {
      settleApplication();
      await pending;
    });
    expect(result.current.opponentPresentation.kind).toBe("targeted_ghost");
  });

  it("stamps targetless Ghost moves committed during a drill as Opening Guide", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "Nf3", null),
    );
    const onApplyBackendMove = vi.fn(async (...args: unknown[]) => {
      const onCommitted = args.at(-1) as OpponentMoveCommittedObserver;
      onCommitted({ drill: { openingKey: "sicilian", state: "active" } });
      return { committed: true } as const;
    });
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback: vi.fn(),
      }),
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation).toEqual({
      kind: "opening_guide",
    });
  });

  it("preserves the prior presentation when an application does not commit", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce(backendResponse("ghost", "Nf3", 42))
      .mockResolvedValueOnce(backendResponse("engine", "e4"));
    const onApplyBackendMove = vi
      .fn()
      .mockImplementationOnce(async (...args: unknown[]) => {
        const onCommitted = args.at(-1) as OpponentMoveCommittedObserver;
        onCommitted({ drill: null });
        return { committed: true } as const;
      })
      .mockResolvedValueOnce({ committed: false, reason: "inactive" });
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback: vi.fn(),
      }),
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
      await result.current.applyOpponentMove("next-fen");
    });

    expect(result.current.opponentPresentation.kind).toBe("targeted_ghost");
  });

  it("publishes engine only after a successful local fallback commit", async () => {
    getNextOpponentMoveMock
      .mockResolvedValueOnce(backendResponse("ghost", "Nf3", 42))
      .mockRejectedValueOnce(new Error("API error"));
    const onApplyBackendMove = vi.fn(async (...args: unknown[]) => {
      const onCommitted = args.at(-1) as OpponentMoveCommittedObserver;
      onCommitted({ drill: null });
      return { committed: true } as const;
    });
    const onApplyLocalFallback = vi.fn(
      async (onCommitted: OpponentMoveCommittedObserver) => {
        onCommitted({ drill: null });
        return { committed: true } as const;
      },
    );
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback,
      }),
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });
    expect(result.current.opponentPresentation.kind).toBe("targeted_ghost");

    await act(async () => {
      await result.current.applyOpponentMove("next-fen");
    });
    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
  });

  it("applies engine move from backend (no local fallback)", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("engine", "e4")
    );

    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
    expect(onApplyBackendMove).toHaveBeenCalledWith(
      "e4",
      "backend_engine",
      null,
      null,
      null,
      null,
      null,
      expect.any(Function),
    );
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("falls back to local engine on API error", async () => {
    getNextOpponentMoveMock.mockRejectedValueOnce(new Error("API error"));

    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove,
        onApplyLocalFallback,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
    expect(onApplyBackendMove).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).toHaveBeenCalled();
  });

  it("uses local engine when sessionId is null", async () => {
    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: null,
        onApplyBackendMove,
        onApplyLocalFallback,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).toHaveBeenCalled();
  });

  it("does not use local engine for a null-session request when the stale guard rejects it", async () => {
    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: null,
        canApplyResult: () => false,
        onApplyBackendMove,
        onApplyLocalFallback,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("uses the latest sessionId immediately after rerender", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("engine", "e4")
    );

    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);

    const { result, rerender } = renderHook(
      ({ sessionId }: { sessionId: string | null }) =>
        useOpponentMove({
          sessionId,
          onApplyBackendMove,
          onApplyLocalFallback,
        }),
      {
        initialProps: { sessionId: null } as { sessionId: string | null },
      },
    );

    rerender({ sessionId: "session-new" });

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
      "session-new",
      "test-fen",
      [],
    );
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("resets the committed presentation to engine", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "e4", 42)
    );

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove: vi.fn(async (...args: unknown[]) => {
          const onCommitted = args.at(-1) as OpponentMoveCommittedObserver;
          onCommitted({ drill: null });
          return { committed: true } as const;
        }),
        onApplyLocalFallback: vi.fn().mockResolvedValue(nonCommit),
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentPresentation.kind).toBe("targeted_ghost");

    act(() => {
      result.current.resetPresentation();
    });

    expect(result.current.opponentPresentation).toEqual({ kind: "engine" });
  });

  it("drops an in-flight backend reply when canApplyResult turns false before resolution", async () => {
    let resolveMove!: (value: ReturnType<typeof backendResponse>) => void;
    getNextOpponentMoveMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveMove = resolve;
      }),
    );

    const onApplyBackendMove = vi.fn().mockResolvedValue(nonCommit);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(nonCommit);
    let shouldApply = true;

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        canApplyResult: () => shouldApply,
        onApplyBackendMove,
        onApplyLocalFallback,
      }),
    );

    const pending = act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    shouldApply = false;

    resolveMove(backendResponse("engine", "e4"));
    await pending;

    expect(onApplyBackendMove).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });
});
