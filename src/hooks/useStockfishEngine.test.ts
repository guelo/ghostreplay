import { describe, expect, it, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useStockfishEngine } from "./useStockfishEngine";
import type { WorkerResponse } from "../workers/stockfishMessages";

// ---------------------------------------------------------------------------
// Minimal Worker mock — captures postMessage calls and lets us push messages
// back into the hook's message handler.
// ---------------------------------------------------------------------------

let messageHandler: ((e: MessageEvent<WorkerResponse>) => void) | null = null;

class FakeWorker {
  postMessage = vi.fn();
  terminate = vi.fn();

  addEventListener(type: string, handler: (e: MessageEvent) => void) {
    if (type === "message") messageHandler = handler;
  }

  removeEventListener() {
    messageHandler = null;
  }
}

// Stub globalThis.Worker so the hook can construct one.
vi.stubGlobal("Worker", FakeWorker);
// SharedArrayBuffer must be present for the hook to initialise.
vi.stubGlobal("SharedArrayBuffer", ArrayBuffer);

function emit(response: WorkerResponse) {
  if (!messageHandler) throw new Error("No worker message handler registered");
  messageHandler(new MessageEvent("message", { data: response }));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("useStockfishEngine – info handler", () => {
  beforeEach(() => {
    messageHandler = null;
  });

  it("does not overwrite slot 0 with a pv-less currmove info line", () => {
    const { result } = renderHook(() => useStockfishEngine());

    // Boot the engine so evaluatePosition can be called.
    act(() => emit({ type: "ready" }));

    const requestId = "req-1";

    // Simulate the worker sending "thinking" (clears info[]).
    act(() => emit({ type: "thinking", id: requestId, fen: "startpos" }));
    expect(result.current.info).toEqual([]);

    // Depth 10: multipv 1, 2, 3 arrive with PVs.
    act(() => {
      emit({
        type: "info",
        id: requestId,
        info: { depth: 10, multipv: 1, pv: ["e2e4"], score: { type: "cp", value: 30 } },
        raw: "info depth 10 multipv 1 score cp 30 pv e2e4",
      });
      emit({
        type: "info",
        id: requestId,
        info: { depth: 10, multipv: 2, pv: ["d2d4"], score: { type: "cp", value: 20 } },
        raw: "info depth 10 multipv 2 score cp 20 pv d2d4",
      });
      emit({
        type: "info",
        id: requestId,
        info: { depth: 10, multipv: 3, pv: ["c2c4"], score: { type: "cp", value: 10 } },
        raw: "info depth 10 multipv 3 score cp 10 pv c2c4",
      });
    });

    // All three lines present and have PVs.
    expect(result.current.info).toHaveLength(3);
    expect(result.current.info[0]?.pv).toEqual(["e2e4"]);

    // Now Stockfish emits a currmove status line — depth only, no multipv, no pv.
    act(() => {
      emit({
        type: "info",
        id: requestId,
        info: { depth: 11 },
        raw: "info depth 11 currmove e2e4 currmovenumber 1",
      });
    });

    // Slot 0 must still contain the depth-10 PV line, NOT the currmove stub.
    expect(result.current.info[0]?.pv).toEqual(["e2e4"]);
    expect(result.current.info).toHaveLength(3);
  });

  it("still updates slot 0 for a real multipv 1 line with pv", () => {
    const { result } = renderHook(() => useStockfishEngine());

    act(() => emit({ type: "ready" }));

    const requestId = "req-2";
    act(() => emit({ type: "thinking", id: requestId, fen: "startpos" }));

    // First real PV line at depth 10.
    act(() => {
      emit({
        type: "info",
        id: requestId,
        info: { depth: 10, multipv: 1, pv: ["e2e4"], score: { type: "cp", value: 30 } },
        raw: "info depth 10 multipv 1 score cp 30 pv e2e4",
      });
    });
    expect(result.current.info[0]?.pv).toEqual(["e2e4"]);

    // Updated PV line at depth 11 — should replace slot 0.
    act(() => {
      emit({
        type: "info",
        id: requestId,
        info: { depth: 11, multipv: 1, pv: ["d2d4", "d7d5"], score: { type: "cp", value: 35 } },
        raw: "info depth 11 multipv 1 score cp 35 pv d2d4 d7d5",
      });
    });
    expect(result.current.info[0]?.pv).toEqual(["d2d4", "d7d5"]);
    expect(result.current.info[0]?.depth).toBe(11);
  });
});
