import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, act, waitFor } from "../../test/utils";
import AnalysisEffects from "./AnalysisEffects";
import { useGameStore } from "../../stores/useGameStore";
import {
  AnalysisStoreProvider,
  createAnalysisStore,
} from "../../stores/createAnalysisStore";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import { createRef } from "react";

const mockPlayBling = vi.fn();
const recordBlunderMock = vi.fn();
const reviewSrsBlunderMock = vi.fn();
vi.mock("../../utils/blingSound", () => ({
  playBling: () => mockPlayBling(),
}));
vi.mock("../../utils/api", () => ({
  recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
  reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
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
    recordBlunderMock.mockReset();
    reviewSrsBlunderMock.mockReset();
    recordBlunderMock.mockResolvedValue({});
    reviewSrsBlunderMock.mockResolvedValue({});
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

  it("does not record blunders during practice continuation", () => {
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: true,
      isPracticeContinuation: true,
    });

    const pendingAnalysisContextRef = createRef<any>();
    const blunderRecordedRef = createRef<any>();
    pendingAnalysisContextRef.current = {
      fen: "fen-before",
      pgn: "1. e4",
      moveSan: "e4",
      moveUci: "e2e4",
      moveIndex: 1,
    };
    blunderRecordedRef.current = false;

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={pendingAnalysisContextRef}
          blunderRecordedRef={blunderRecordedRef}
          pendingSrsReviewRef={createRef() as any}
          appendMoveMessage={vi.fn()}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={vi.fn()}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      store.getState().setLastAnalysis(makeResult({
        moveIndex: 1,
        bestMove: "d2d4",
        delta: 250,
        classification: "blunder",
        blunder: true,
        recordable: true,
      }));
    });

    expect(recordBlunderMock).not.toHaveBeenCalled();
  });

  it("submits an armed SRS review after the game has become inactive", async () => {
    const analysisId = "analysis-terminal";
    useGameStore.setState({
      sessionId: "session-1",
      playerColor: "white",
      isGameActive: false,
      isPracticeContinuation: false,
      moveHistory: [{ san: "Qg7#", fen: "mate-fen", uci: "g6g7" }],
    });

    const pendingSrsReviewRef = createRef<any>();
    pendingSrsReviewRef.current = {
      analysisId,
      blunderId: 42,
      moveIndex: 0,
      userMoveSan: "Qg7#",
      srs: {
        due_at: "2026-04-28T00:00:00Z",
        fail_count: 1,
        interval_days: 1,
        pass_count: 2,
        pass_streak: 1,
        state: "due",
      },
    };
    const appendMoveMessage = vi.fn();
    const setResolvedReview = vi.fn((updater) => {
      if (typeof updater === "function") {
        updater({ analysisId, moveIndex: 0, result: "pending" });
      }
    });

    render(
      <AnalysisStoreProvider value={store}>
        <AnalysisEffects
          pendingAnalysisContextRef={createRef() as any}
          blunderRecordedRef={createRef() as any}
          pendingSrsReviewRef={pendingSrsReviewRef}
          appendMoveMessage={appendMoveMessage}
          setBlunderAlert={vi.fn()}
          setShowFlash={vi.fn()}
          setResolvedReview={setResolvedReview}
        />
      </AnalysisStoreProvider>,
    );

    act(() => {
      store.getState().setLastAnalysis(makeResult({
        id: analysisId,
        move: "g6g7",
        bestMove: "g6e8",
        moveIndex: 0,
        delta: 0,
        classification: "excellent",
      }));
    });

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        "session-1",
        42,
        true,
        "Qg7#",
        0,
      );
    });
    expect(pendingSrsReviewRef.current).toBeNull();
    expect(appendMoveMessage).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ variant: "srs-pass" }),
    );
  });
});
