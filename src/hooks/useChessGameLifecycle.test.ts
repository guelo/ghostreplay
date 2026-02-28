import { act, renderHook, waitFor } from "@testing-library/react";
import { Chess } from "chess.js";
import type { MutableRefObject } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { AnalysisResult } from "./useMoveAnalysis";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import { useChessGameLifecycle } from "./useChessGameLifecycle";

const fetchCurrentRatingMock = vi.fn();
const startGameMock = vi.fn();
const endGameMock = vi.fn();
const uploadSessionMovesMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
}));

type SetupOptions = {
  chess?: Chess;
  moveHistory?: MoveRecord[];
  isGameActive?: boolean;
  isRated?: boolean;
  playerColor?: "white" | "black";
  playerColorChoice?: "white" | "black" | "random";
  playerRating?: number;
};

const setup = ({
  chess = new Chess(),
  moveHistory = [],
  isGameActive = false,
  isRated = true,
  playerColor = "white",
  playerColorChoice = "random",
  playerRating = 1200,
}: SetupOptions = {}) => {
  const moveCountRef: MutableRefObject<number> = { current: moveHistory.length };
  const moveHistoryRef: MutableRefObject<MoveRecord[]> = {
    current: [...moveHistory],
  };
  const analysisMapRef: MutableRefObject<Map<number, AnalysisResult>> = {
    current: new Map(),
  };
  const analysisStatusRef: MutableRefObject<string> = { current: "ready" };
  const isAnalyzingRef: MutableRefObject<boolean> = { current: false };
  const uploadedAnalysisSessionsRef: MutableRefObject<Set<string>> = {
    current: new Set(),
  };
  const openingHistoryRef: MutableRefObject<Array<null>> = { current: [] };
  const blunderRecordedRef: MutableRefObject<boolean> = { current: false };
  const pendingAnalysisContextRef: MutableRefObject<{
    fen: string;
    pgn: string;
    moveSan: string;
    moveUci: string;
    moveIndex: number;
  } | null> = { current: null };
  const pendingSrsReviewRef: MutableRefObject<{
    blunderId: number;
    moveIndex: number;
    userMoveSan: string;
  } | null> = { current: null };

  const clearMoveHighlights = vi.fn();
  const resetMode = vi.fn();
  const resetEngine = vi.fn();
  const clearAnalysis = vi.fn();
  const onOpenHistory = vi.fn();
  const setEngineMessage = vi.fn();
  const setPlayerRating = vi.fn();
  const setIsProvisional = vi.fn();
  const setEngineElo = vi.fn();
  const setIsStartingGame = vi.fn();
  const setStartError = vi.fn();
  const setPlayerColor = vi.fn();
  const setPlayerColorChoice = vi.fn();
  const setBoardOrientation = vi.fn();
  const setSessionId = vi.fn();
  const setIsGameActive = vi.fn();
  const setShowStartOverlay = vi.fn();
  const setFen = vi.fn();
  const setGameResult = vi.fn();
  const setRatingChange = vi.fn();
  const setMoveHistory = vi.fn();
  const setViewIndex = vi.fn();
  const setLiveOpening = vi.fn();
  const setBlunderAlert = vi.fn();
  const setShowFlash = vi.fn();
  const setBlunderReviewId = vi.fn();
  const setBlunderReviewSrs = vi.fn();
  const setShowPassToast = vi.fn();
  const setShowRehookToast = vi.fn();
  const setReviewFailModal = vi.fn();
  const setShowPostGamePrompt = vi.fn();
  const setIsRated = vi.fn();
  const setShowRevertWarning = vi.fn();

  const { result } = renderHook(() =>
    useChessGameLifecycle({
      chess,
      sessionId: "session-123",
      isGameActive,
      isRated,
      playerColor,
      playerColorChoice,
      engineElo: 1000,
      playerRating,
      moveHistory,
      moveCountRef,
      moveHistoryRef,
      analysisMapRef,
      analysisStatusRef,
      isAnalyzingRef,
      uploadedAnalysisSessionsRef,
      openingHistoryRef,
      blunderRecordedRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      clearMoveHighlights,
      resetMode,
      resetEngine,
      clearAnalysis,
      onOpenHistory,
      setEngineMessage,
      setPlayerRating,
      setIsProvisional,
      setEngineElo,
      setIsStartingGame,
      setStartError,
      setPlayerColor,
      setPlayerColorChoice,
      setBoardOrientation,
      setSessionId,
      setIsGameActive,
      setShowStartOverlay,
      setFen,
      setGameResult,
      setRatingChange,
      setMoveHistory,
      setViewIndex,
      setLiveOpening,
      setBlunderAlert,
      setShowFlash,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setShowPassToast,
      setShowRehookToast,
      setReviewFailModal,
      setShowPostGamePrompt,
      setIsRated,
      showRevertWarning: false,
      setShowRevertWarning,
    }),
  );

  return {
    result,
    moveCountRef,
    moveHistoryRef,
    onOpenHistory,
    setMoveHistory,
    setFen,
    setIsRated,
    setShowRevertWarning,
    setPlayerColorChoice,
    setShowPostGamePrompt,
    setShowStartOverlay,
  };
};

beforeEach(() => {
  fetchCurrentRatingMock.mockReset();
  fetchCurrentRatingMock.mockResolvedValue({
    current_rating: 1200,
    is_provisional: true,
    games_played: 10,
  });
  startGameMock.mockReset();
  endGameMock.mockReset();
  uploadSessionMovesMock.mockReset();
});

describe("useChessGameLifecycle", () => {
  it("shows the revert warning instead of reverting when game is rated", async () => {
    const { result, setShowRevertWarning } = setup({
      isGameActive: true,
      isRated: true,
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleRevertClick();
    });

    expect(setShowRevertWarning).toHaveBeenCalledWith(true);
  });

  it("reverts two half-moves when it is the player's turn", async () => {
    const chess = new Chess();
    const moveOne = chess.move("e4");
    const fenAfterMoveOne = chess.fen();
    const moveTwo = chess.move("e5");
    const fenAfterMoveTwo = chess.fen();
    if (!moveOne || !moveTwo) {
      throw new Error("Unable to construct test position");
    }
    const moveHistory: MoveRecord[] = [
      { san: moveOne.san, fen: fenAfterMoveOne, uci: "e2e4" },
      { san: moveTwo.san, fen: fenAfterMoveTwo, uci: "e7e5" },
    ];

    const { result, moveCountRef, moveHistoryRef, setMoveHistory, setIsRated, setFen } =
      setup({
        chess,
        moveHistory,
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.executeRevert();
    });

    expect(setIsRated).toHaveBeenCalledWith(false);
    expect(setMoveHistory).toHaveBeenCalledWith([]);
    expect(moveHistoryRef.current).toEqual([]);
    expect(moveCountRef.current).toBe(0);
    expect(setFen).toHaveBeenCalledWith(chess.fen());
  });

  it("routes view-analysis action through history callback and hides prompt", async () => {
    const { result, onOpenHistory, setShowPostGamePrompt } = setup();

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleViewAnalysis();
    });

    expect(setShowPostGamePrompt).toHaveBeenCalledWith(false);
    expect(onOpenHistory).toHaveBeenCalledWith({
      select: "latest",
      source: "post_game_view_analysis",
    });
  });

  it("shows start overlay and resets side choice to random", async () => {
    const { result, setPlayerColorChoice, setShowPostGamePrompt, setShowStartOverlay } =
      setup({ playerRating: 1350 });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleShowStartOverlay();
    });

    expect(setPlayerColorChoice).toHaveBeenCalledWith("random");
    expect(setShowPostGamePrompt).toHaveBeenCalledWith(false);
    expect(setShowStartOverlay).toHaveBeenCalledWith(true);
  });
});
