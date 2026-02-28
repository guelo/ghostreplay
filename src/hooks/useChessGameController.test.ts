import { describe, it, expect, vi } from "vitest";
import { Chess } from "chess.js";
import { renderHook, act } from "@testing-library/react";
import type { MutableRefObject } from "react";
import type { TargetBlunderSrs } from "../utils/api";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import {
  useChessGameController,
  type PendingAnalysisContext,
  type PendingSrsReview,
} from "./useChessGameController";

type SetupOptions = {
  chess?: Chess;
  playerColor?: "white" | "black";
  opponentColor?: "white" | "black";
  isPlayersTurn?: boolean;
  isViewingLive?: boolean;
  blunderReviewId?: number | null;
  moveHistory?: MoveRecord[];
  moveCount?: number;
};

const createSetup = ({
  chess = new Chess(),
  playerColor = "white",
  opponentColor = "black",
  isPlayersTurn = true,
  isViewingLive = true,
  blunderReviewId = null,
  moveHistory = [],
  moveCount = 0,
}: SetupOptions = {}) => {
  const moveCountRef: MutableRefObject<number> = { current: moveCount };
  const moveHistoryRef: MutableRefObject<MoveRecord[]> = {
    current: [...moveHistory],
  };
  const pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null> = {
    current: null,
  };
  const pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null> = {
    current: null,
  };

  const setFen = vi.fn();
  const setMoveHistory = vi.fn();
  const setViewIndex = vi.fn();
  const setEngineMessage = vi.fn();
  const setBlunderAlert = vi.fn();
  const setBlunderReviewId = vi.fn();
  const setBlunderReviewSrs = vi.fn();
  const setBlunderTargetFen = vi.fn();
  const setShowGhostInfo = vi.fn();
  const analyzeMove = vi.fn();
  const evaluatePosition = vi.fn().mockResolvedValue({ move: "(none)", raw: "" });
  const handleGameEnd = vi.fn().mockResolvedValue(undefined);
  const clearMoveHighlights = vi.fn();

  const { result } = renderHook(() =>
    useChessGameController({
      chess,
      playerColor,
      opponentColor,
      isPlayersTurn,
      isViewingLive,
      blunderReviewId,
      moveCountRef,
      moveHistoryRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      setFen,
      setMoveHistory,
      setViewIndex,
      setEngineMessage,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setShowGhostInfo,
      analyzeMove,
      evaluatePosition,
      handleGameEnd,
      clearMoveHighlights,
    })
  );

  return {
    result,
    chess,
    moveCountRef,
    moveHistoryRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    setFen,
    setMoveHistory,
    setViewIndex,
    setEngineMessage,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setShowGhostInfo,
    analyzeMove,
    evaluatePosition,
    handleGameEnd,
    clearMoveHighlights,
  };
};

describe("useChessGameController", () => {
  it("applies a player move and records analysis context", () => {
    const {
      result,
      chess,
      moveCountRef,
      moveHistoryRef,
      pendingAnalysisContextRef,
      analyzeMove,
      clearMoveHighlights,
      setBlunderAlert,
    } = createSetup();
    const startingFen = chess.fen();

    let moveResult: ReturnType<typeof result.current.applyPlayerMove> = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);
    if (!moveResult.applied) {
      throw new Error("Expected a legal player move");
    }

    expect(clearMoveHighlights).toHaveBeenCalledTimes(1);
    expect(setBlunderAlert).toHaveBeenCalledWith(null);
    expect(moveCountRef.current).toBe(1);
    expect(moveHistoryRef.current[0]?.uci).toBe("e2e4");
    expect(moveResult.uciHistory).toEqual(["e2e4"]);
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

    let moveResult: ReturnType<typeof result.current.applyPlayerMove> = {
      applied: false,
    };
    act(() => {
      moveResult = result.current.applyPlayerMove("e2", "e4");
    });

    expect(moveResult.applied).toBe(true);
    expect(pendingSrsReviewRef.current).toEqual({
      blunderId: 42,
      moveIndex: 0,
      userMoveSan: "e4",
    });
    expect(setBlunderReviewId).toHaveBeenCalledWith(null);
    expect(setBlunderReviewSrs).toHaveBeenCalledWith(null);
  });

  it("rejects drop moves when interaction preconditions fail", () => {
    const { result, analyzeMove } = createSetup({
      isPlayersTurn: false,
      isViewingLive: true,
    });

    let dropResult: ReturnType<typeof result.current.handleDrop> = {
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
      moveCountRef,
      moveHistoryRef,
      evaluatePosition,
      analyzeMove,
      setEngineMessage,
      handleGameEnd,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
      moveCount: 1,
    });

    evaluatePosition.mockResolvedValueOnce({ move: "d7d5", raw: "bestmove d7d5" });

    await act(async () => {
      await result.current.applyEngineMove();
    });

    expect(evaluatePosition).toHaveBeenCalledWith(fenBeforeEngineMove);
    expect(moveCountRef.current).toBe(2);
    expect(moveHistoryRef.current[1]?.uci).toBe("d7d5");
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
      setShowGhostInfo,
    } = createSetup({
      chess,
      moveHistory: [previousMove],
      moveCount: 1,
    });

    await act(async () => {
      await result.current.applyGhostMove("e5", 77, targetSrs, "target-fen");
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
    expect(setShowGhostInfo).not.toHaveBeenCalled();
  });
});
