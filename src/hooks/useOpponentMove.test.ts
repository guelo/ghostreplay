import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  determineOpponentMove,
  useOpponentMove,
} from "./useOpponentMove";

const getNextOpponentMoveMock = vi.fn();

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
    : "backend_engine"
) => ({
  mode,
  move: { uci: san === "Nf3" ? "g1f3" : "e2e4", san },
  target_blunder_id: targetBlunderId,
  decision_source: decisionSource,
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
    });
    expect(getNextOpponentMoveMock).toHaveBeenCalledWith(
      "session-123",
      "test-fen"
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

  it("initializes with engine mode", () => {
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove: vi.fn(),
        onApplyLocalFallback: vi.fn(),
      })
    );

    expect(result.current.opponentMode).toBe("engine");
  });

  it("applies ghost move from backend", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "Nf3", 42)
    );

    const onApplyBackendMove = vi.fn().mockResolvedValue(undefined);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(undefined);

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

    expect(result.current.opponentMode).toBe("ghost");
    expect(onApplyBackendMove).toHaveBeenCalledWith("Nf3", 42);
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("applies engine move from backend (no local fallback)", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("engine", "e4")
    );

    const onApplyBackendMove = vi.fn().mockResolvedValue(undefined);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(undefined);

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

    expect(result.current.opponentMode).toBe("engine");
    expect(onApplyBackendMove).toHaveBeenCalledWith("e4", null);
    expect(onApplyLocalFallback).not.toHaveBeenCalled();
  });

  it("falls back to local engine on API error", async () => {
    getNextOpponentMoveMock.mockRejectedValueOnce(new Error("API error"));

    const onApplyBackendMove = vi.fn().mockResolvedValue(undefined);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(undefined);

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

    expect(result.current.opponentMode).toBe("engine");
    expect(onApplyBackendMove).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).toHaveBeenCalled();
  });

  it("uses local engine when sessionId is null", async () => {
    const onApplyBackendMove = vi.fn().mockResolvedValue(undefined);
    const onApplyLocalFallback = vi.fn().mockResolvedValue(undefined);

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

    expect(result.current.opponentMode).toBe("engine");
    expect(getNextOpponentMoveMock).not.toHaveBeenCalled();
    expect(onApplyLocalFallback).toHaveBeenCalled();
  });

  it("resets mode to engine", async () => {
    getNextOpponentMoveMock.mockResolvedValueOnce(
      backendResponse("ghost", "e4", 42)
    );

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyBackendMove: vi.fn().mockResolvedValue(undefined),
        onApplyLocalFallback: vi.fn().mockResolvedValue(undefined),
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentMode).toBe("ghost");

    act(() => {
      result.current.resetMode();
    });

    expect(result.current.opponentMode).toBe("engine");
  });
});
