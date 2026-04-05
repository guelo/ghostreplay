import { describe, it, expect, vi, beforeEach } from "vitest";
import { Chess } from "chess.js";
import { renderHook, act } from "@testing-library/react";
import type { MutableRefObject } from "react";
import type { TargetBlunderSrs } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import type { ResolvedReview } from "../components/chess-game/types";
import { useGameStore } from "../stores/useGameStore";
import {
  useChessGameController,
  type PendingAnalysisContext,
  type PlayerMoveApplyResult,
  type PendingSrsReview,
} from "./useChessGameController";

const initialStoreState = useGameStore.getInitialState();

type SetupOptions = {
  chess?: Chess;
  playerColor?: "white" | "black";
  blunderReviewId?: number | null;
  blunderReviewSrs?: TargetBlunderSrs | null;
  resolvedReview?: ResolvedReview | null;
  moveHistory?: MoveRecord[];
};

const createSetup = ({
  chess = new Chess(),
  playerColor = "white",
  blunderReviewId = null,
  blunderReviewSrs = null,
  resolvedReview = null,
  moveHistory = [],
}: SetupOptions = {}) => {
  // Set up store state
  useGameStore.setState({
    ...initialStoreState,
    playerColor,
    liveFen: chess.fen(),
    moveHistory: [...moveHistory],
  });

  const pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null> = {
    current: null,
  };
  const pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null> = {
    current: null,
  };

  const setEngineMessage = vi.fn();
  const setBlunderAlert = vi.fn();
  const setBlunderReviewId = vi.fn();
  const setBlunderReviewSrs = vi.fn();
  const setBlunderTargetFen = vi.fn();
  const setShowGhostInfo = vi.fn();
  const setResolvedReview = vi.fn();
  const analyzeMove = vi.fn();
  const evaluatePosition = vi.fn().mockResolvedValue({ move: "(none)", raw: "" });
  const handleGameEnd = vi.fn().mockResolvedValue(undefined);
  const clearMoveHighlights = vi.fn();

  const { result } = renderHook(() =>
    useChessGameController({
      chess,
      blunderReviewId,
      blunderReviewSrs,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      setEngineMessage,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setShowGhostInfo,
      resolvedReview,
      setResolvedReview,
      analyzeMove,
      evaluatePosition,
      handleGameEnd,
      clearMoveHighlights,
    })
  );

  return {
    result,
    chess,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    setEngineMessage,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setShowGhostInfo,
    setResolvedReview,
    analyzeMove,
    evaluatePosition,
    handleGameEnd,
    clearMoveHighlights,
  };
};

beforeEach(() => {
  useGameStore.setState(initialStoreState, true);
});

describe("useChessGameController", () => {
  it("applies a player move and records analysis context", () => {
    const {
      result,
      chess,
      pendingAnalysisContextRef,
      analyzeMove,
      clearMoveHighlights,
      setBlunderAlert,
    } = createSetup();
    const startingFen = chess.fen();

    let moveResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);

    expect(clearMoveHighlights).toHaveBeenCalledTimes(1);
    expect(setBlunderAlert).toHaveBeenCalledWith(null);

    const store = useGameStore.getState();
    expect(store.moveHistory.length).toBe(1);
    expect(store.moveHistory[0]?.uci).toBe("e2e4");
    expect(store.viewIndex).toBeNull();

    expect(analyzeMove).toHaveBeenCalledWith(
      startingFen,
      "e2e4",
      "white",
      0,
      20,
    );
    expect(pendingAnalysisContextRef.current?.moveUci).toBe("e2e4");
  });

  it("captures pending SRS review metadata for targeted player moves", () => {
    const {
      result,
      pendingSrsReviewRef,
      setBlunderReviewId,
      setBlunderReviewSrs,
    } = createSetup({ blunderReviewId: 42 });

    let moveResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);
    expect(pendingSrsReviewRef.current).toEqual({
      analysisId: expect.any(String),
      blunderId: 42,
      moveIndex: 0,
      userMoveSan: "e4",
      srs: null,
    });
    expect(setBlunderReviewId).toHaveBeenCalledWith(null);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(null);
  });

  it("sets resolvedReview to pending synchronously when clearing blunder review", () => {
    const {
      result,
      setResolvedReview,
    } = createSetup({ blunderReviewId: 42 });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(setResolvedReview).toHaveBeenCalledWith({
      analysisId: expect.any(String),
      moveIndex: 0,
      result: "pending",
    });
  });

  it("clears previous resolvedReview on next move", () => {
    const {
      result,
      setResolvedReview,
    } = createSetup({
      resolvedReview: {
        analysisId: "analysis-0-e2e4",
        moveIndex: 0,
        result: "pass",
      },
    });

    act(() => {
      result.current.applyPlayerMove("e2", "e4");
    });

    expect(setResolvedReview).toHaveBeenCalledWith(null);
  });

  it("rejects drop moves when interaction preconditions fail", () => {
    // Set viewIndex to non-null so isViewingLive is false
    const { result, analyzeMove } = createSetup();
    useGameStore.setState({ viewIndex: 0 });

    let dropResult: PlayerMoveApplyResult = {
      applied: false,
    };
    act(() => {
      dropResult = result.current.handleDrop("e2", "e4");
    });

    expect(dropResult.applied).toBe(false);
    expect(analyzeMove).not.toHaveBeenCalled();
  });

  it("applies an engine move and analyzes from opponent perspective", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize engine test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };
    const fenBeforeEngineMove = chess.fen();

    const {
      result,
      evaluatePosition,
      analyzeMove,
      setEngineMessage,
      handleGameEnd,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
    });

    evaluatePosition.mockResolvedValueOnce({ move: "d7d5", raw: "bestmove d7d5" });

    await act(async () => {
      await result.current.applyEngineMove();
    });

    expect(evaluatePosition).toHaveBeenCalledWith(fenBeforeEngineMove);

    const store = useGameStore.getState();
    expect(store.moveHistory.length).toBe(2);
    expect(store.moveHistory[1]?.uci).toBe("d7d5");

    expect(analyzeMove).toHaveBeenCalledWith(
      fenBeforeEngineMove,
      "d7d5",
      "black",
      1,
      20,
    );
    expect(setEngineMessage).toHaveBeenCalledWith(null);
    expect(handleGameEnd).not.toHaveBeenCalled();
  });

  it("applies a ghost move and toggles review targeting state", async () => {
    const chess = new Chess();
    const whiteMove = chess.move("e4");
    if (!whiteMove) {
      throw new Error("Unable to initialize ghost test position");
    }
    const previousMove: MoveRecord = {
      san: whiteMove.san,
      fen: chess.fen(),
      uci: `${whiteMove.from}${whiteMove.to}${whiteMove.promotion ?? ""}`,
    };
    const targetSrs: TargetBlunderSrs = {
      last_reviewed_at: "2026-02-01T00:00:00Z",
      created_at: "2026-01-15T00:00:00Z",
      pass_count: 2,
      fail_count: 1,
      pass_streak: 1,
    };

    const {
      result,
      analyzeMove,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setShowGhostInfo,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
    });

    await act(async () => {
      await result.current.applyGhostMove("e5", "ghost_path", 77, targetSrs, "target-fen");
    });

    expect(analyzeMove).toHaveBeenCalledWith(
      expect.any(String),
      "e7e5",
      "black",
      1,
      20,
    );
    expect(setBlunderReviewId).toHaveBeenCalledWith(77);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(targetSrs);
    expect(setBlunderTargetFen).toHaveBeenCalledWith("target-fen");
    expect(setResolvedReview).toHaveBeenCalledWith(null);
    expect(setShowGhostInfo).not.toHaveBeenCalled();
  });
});
