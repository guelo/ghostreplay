import { describe, expect, it, beforeEach } from "vitest";
import { createAnalysisStore } from "./createAnalysisStore";
import type { AnalysisResult } from "../hooks/useMoveAnalysis";

const makeResult = (
  overrides: Partial<AnalysisResult> = {},
): AnalysisResult => ({
  id: crypto.randomUUID(),
  move: "e2e4",
  currentPositionEval: 0,
  moveIndex: 0,
  playedEval: 0,
  bestEval: 0,
  bestMove: "e2e4",
  delta: 0,
  classification: "good",
  blunder: false,
  recordable: false,
  ...overrides,
});

describe("createAnalysisStore — freshlyResolved", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    store = createAnalysisStore();
  });

  it("resolveAnalysis does not mark freshlyResolved (marking is done by subscribers)", () => {
    store.getState().resolveAnalysis(3, makeResult({ moveIndex: 3, classification: "best" }));
    expect(store.getState().freshlyResolved.size).toBe(0);
  });

  it("markFreshlyResolved adds index to set", () => {
    store.getState().markFreshlyResolved(5);
    expect(store.getState().freshlyResolved.has(5)).toBe(true);
  });

  it("clearFreshlyResolved removes index from set", () => {
    store.getState().markFreshlyResolved(5);
    store.getState().clearFreshlyResolved(5);
    expect(store.getState().freshlyResolved.has(5)).toBe(false);
  });

  it("resetTransient clears the set", () => {
    store.getState().markFreshlyResolved(1);
    store.getState().markFreshlyResolved(2);
    store.getState().resetTransient();
    expect(store.getState().freshlyResolved.size).toBe(0);
  });

  it("clearAll clears the set", () => {
    store.getState().markFreshlyResolved(1);
    store.getState().clearAll();
    expect(store.getState().freshlyResolved.size).toBe(0);
  });

  it("subscribe fires for each resolveAnalysis call (no batching)", () => {
    const seen: (AnalysisResult | null)[] = [];
    store.subscribe((state, prev) => {
      if (state.lastAnalysis !== prev.lastAnalysis) {
        seen.push(state.lastAnalysis);
      }
    });
    store.getState().resolveAnalysis(0, makeResult({ moveIndex: 0, classification: "good" }));
    store.getState().resolveAnalysis(1, makeResult({ moveIndex: 1, classification: "best" }));
    expect(seen).toHaveLength(2);
    expect(seen[0]?.moveIndex).toBe(0);
    expect(seen[1]?.moveIndex).toBe(1);
  });
});
