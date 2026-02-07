import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import {
  determineOpponentMove,
  useOpponentMove,
} from "./useOpponentMove";

const getGhostMoveMock = vi.fn();

vi.mock("../utils/api", () => ({
  getGhostMove: (...args: unknown[]) => getGhostMoveMock(...args),
}));

describe("determineOpponentMove", () => {
  beforeEach(() => {
    getGhostMoveMock.mockReset();
  });

  it("returns ghost mode when ghost move is available", async () => {
    getGhostMoveMock.mockResolvedValueOnce({
      mode: "ghost",
      move: "e4",
      target_blunder_id: 42,
    });

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toEqual({ mode: "ghost", move: "e4", targetBlunderId: 42 });
    expect(getGhostMoveMock).toHaveBeenCalledWith("session-123", "test-fen");
  });

  it("returns engine mode when ghost move is null", async () => {
    getGhostMoveMock.mockResolvedValueOnce({
      mode: "engine",
      move: null,
      target_blunder_id: null,
    });

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toEqual({ mode: "engine", move: null, targetBlunderId: null });
  });

  it("returns engine mode on API error", async () => {
    getGhostMoveMock.mockRejectedValueOnce(new Error("Network error"));

    const result = await determineOpponentMove("session-123", "test-fen");

    expect(result).toEqual({ mode: "engine", move: null, targetBlunderId: null });
  });
});

describe("useOpponentMove", () => {
  beforeEach(() => {
    getGhostMoveMock.mockReset();
  });

  it("initializes with engine mode", () => {
    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyGhostMove: vi.fn(),
        onApplyEngineMove: vi.fn(),
      })
    );

    expect(result.current.opponentMode).toBe("engine");
  });

  it("applies ghost move when available", async () => {
    getGhostMoveMock.mockResolvedValueOnce({
      mode: "ghost",
      move: "Nf3",
      target_blunder_id: 42,
    });

    const onApplyGhostMove = vi.fn().mockResolvedValue(undefined);
    const onApplyEngineMove = vi.fn().mockResolvedValue(undefined);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyGhostMove,
        onApplyEngineMove,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentMode).toBe("ghost");
    expect(onApplyGhostMove).toHaveBeenCalledWith("Nf3", 42);
    expect(onApplyEngineMove).not.toHaveBeenCalled();
  });

  it("applies engine move when ghost move is null", async () => {
    getGhostMoveMock.mockResolvedValueOnce({
      mode: "engine",
      move: null,
      target_blunder_id: null,
    });

    const onApplyGhostMove = vi.fn().mockResolvedValue(undefined);
    const onApplyEngineMove = vi.fn().mockResolvedValue(undefined);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyGhostMove,
        onApplyEngineMove,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentMode).toBe("engine");
    expect(onApplyGhostMove).not.toHaveBeenCalled();
    expect(onApplyEngineMove).toHaveBeenCalled();
  });

  it("falls back to engine mode on API error", async () => {
    getGhostMoveMock.mockRejectedValueOnce(new Error("API error"));

    const onApplyGhostMove = vi.fn().mockResolvedValue(undefined);
    const onApplyEngineMove = vi.fn().mockResolvedValue(undefined);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyGhostMove,
        onApplyEngineMove,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentMode).toBe("engine");
    expect(onApplyEngineMove).toHaveBeenCalled();
  });

  it("uses engine mode when sessionId is null", async () => {
    const onApplyGhostMove = vi.fn().mockResolvedValue(undefined);
    const onApplyEngineMove = vi.fn().mockResolvedValue(undefined);

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: null,
        onApplyGhostMove,
        onApplyEngineMove,
      })
    );

    await act(async () => {
      await result.current.applyOpponentMove("test-fen");
    });

    expect(result.current.opponentMode).toBe("engine");
    expect(getGhostMoveMock).not.toHaveBeenCalled();
    expect(onApplyEngineMove).toHaveBeenCalled();
  });

  it("resets mode to engine", async () => {
    getGhostMoveMock.mockResolvedValueOnce({
      mode: "ghost",
      move: "e4",
      target_blunder_id: 42,
    });

    const { result } = renderHook(() =>
      useOpponentMove({
        sessionId: "session-123",
        onApplyGhostMove: vi.fn().mockResolvedValue(undefined),
        onApplyEngineMove: vi.fn().mockResolvedValue(undefined),
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
