import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act } from "../../test/utils";
import AnalysisEffects from "./AnalysisEffects";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import { createRef } from "react";

const mockPlayBling = vi.fn();
vi.mock("../../utils/blingSound", () => ({
  playBling: () => mockPlayBling(),
}));

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

const initialGameState = useGameStore.getInitialState();

describe("AnalysisEffects — best-move bling", () => {
  let store: ReturnType<typeof createAnalysisStore>;

  beforeEach(() => {
    mockPlayBling.mockClear();
    useGameStore.setState(initialGameState, true);
    store = createAnalysisStore();
  });

  function renderEffects() {
    return render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={createRef() as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={createRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          resolvedReview={null}
          setResolvedReview={vi.fn()}
        />
      </AnalysisStoreProvider>,
    );
  }

  it("plays bling when best-move analysis arrives for a player move", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    // Player's move (index 0 = white = player)
    act(() => {
      store.getState().resolveAnalysis(0, makeResult({
        moveIndex: 0,
        classification: "best",
      }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("plays bling when best-move analysis arrives for a black player move", () => {
    useGameStore.setState({ playerColor: "black", isGameActive: true });
    renderEffects();

    // Player's move (index 1 = black = player)
    act(() => {
      store.getState().resolveAnalysis(1, makeResult({
        moveIndex: 1,
        classification: "best",
      }));
    });

    expect(mockPlayBling).toHaveBeenCalledTimes(1);
  });

  it("does NOT play bling for engine moves with best classification", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    // Engine move (index 1 = black = engine when player is white)
    act(() => {
      store.getState().resolveAnalysis(1, makeResult({
        moveIndex: 1,
        classification: "best",
      }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });

  it("does NOT play bling for non-best player moves", () => {
    useGameStore.setState({ playerColor: "white", isGameActive: true });
    renderEffects();

    act(() => {
      store.getState().resolveAnalysis(0, makeResult({
        moveIndex: 0,
        classification: "good",
      }));
    });

    expect(mockPlayBling).not.toHaveBeenCalled();
  });
});
