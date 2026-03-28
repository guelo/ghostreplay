import { act, renderHook, waitFor } from "@testing-library/react";
import { Chess } from "chess.js";
import type { MutableRefObject } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import { useChessGameLifecycle } from "./useChessGameLifecycle";
import { useGameStore } from "../stores/useGameStore";
import type { GameAnalysisCoordinator } from "../services/GameAnalysisCoordinator";

const fetchCurrentRatingMock = vi.fn();
const startGameMock = vi.fn();
const endGameMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
}));

const initialStoreState = useGameStore.getInitialState();

const createMockCoordinator = (): GameAnalysisCoordinator =>
  ({
    startSession: vi.fn(),
    clearSession: vi.fn(),
    flushPendingUploads: vi.fn().mockResolvedValue(undefined),
    analyzeMove: vi.fn(),
    clearAnalysis: vi.fn(),
    sessionId: null,
    store: { getState: vi.fn().mockReturnValue({ analysisMap: new Map() }) },
  }) as unknown as GameAnalysisCoordinator;

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
  // Set up store state
  useGameStore.setState({
    ...initialStoreState,
    sessionId: "session-123",
    isGameActive,
    isRated,
    playerColor,
    playerColorChoice,
    engineElo: 1000,
    playerRating,
    moveHistory: [...moveHistory],
    liveFen: chess.fen(),
  });

  const coordinator = createMockCoordinator();
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
  const onOpenHistory = vi.fn();
  const setEngineMessage = vi.fn();
  const setIsStartingGame = vi.fn();
  const setStartError = vi.fn();
  const setShowStartOverlay = vi.fn();
  const setLiveOpening = vi.fn();
  const setBlunderAlert = vi.fn();
  const setShowFlash = vi.fn();
  const setBlunderReviewId = vi.fn();
  const setBlunderReviewSrs = vi.fn();
  const setShowPassToast = vi.fn();
  const setShowRehookToast = vi.fn();
  const setReviewFailModal = vi.fn();
  const setShowPostGamePrompt = vi.fn();
  const setShowRevertWarning = vi.fn();

  const { result } = renderHook(() =>
    useChessGameLifecycle({
      chess,
      coordinator,
      openingHistoryRef,
      blunderRecordedRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      clearMoveHighlights,
      resetMode,
      resetEngine,
      onOpenHistory,
      setEngineMessage,
      setIsStartingGame,
      setStartError,
      setShowStartOverlay,
      setLiveOpening,
      setBlunderAlert,
      setShowFlash,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setShowPassToast,
      setShowRehookToast,
      setReviewFailModal,
      setShowPostGamePrompt,
      showRevertWarning: false,
      setShowRevertWarning,
      setShowResignWarning: vi.fn(),
    }),
  );

  return {
    result,
    onOpenHistory,
    setShowRevertWarning,
    setShowPostGamePrompt,
    setShowStartOverlay,
  };
};

beforeEach(() => {
  useGameStore.setState(initialStoreState, true);
  fetchCurrentRatingMock.mockReset();
  fetchCurrentRatingMock.mockResolvedValue({
    current_rating: 1200,
    is_provisional: true,
    games_played: 10,
  });
  startGameMock.mockReset();
  endGameMock.mockReset();
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

    const { result } = setup({
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

    const store = useGameStore.getState();
    expect(store.isRated).toBe(false);
    expect(store.moveHistory).toEqual([]);
    expect(store.viewIndex).toBeNull();
  });

  it("routes view-analysis action through history callback and hides prompt", async () => {
    const { result, onOpenHistory, setShowPostGamePrompt } = setup();

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleViewAnalysis();
    });

    expect(setShowPostGamePrompt).toHaveBeenCalledWith(false);
    expect(onOpenHistory).toHaveBeenCalledWith(
      expect.objectContaining({
        select: "latest",
        source: "post_game_view_analysis",
      }),
    );
  });

  it("shows start overlay and resets side choice to random", async () => {
    const { result, setShowPostGamePrompt, setShowStartOverlay } =
      setup({ playerRating: 1350 });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleShowStartOverlay();
    });

    expect(useGameStore.getState().playerColorChoice).toBe("random");
    expect(setShowPostGamePrompt).toHaveBeenCalledWith(false);
    expect(setShowStartOverlay).toHaveBeenCalledWith(true);
  });
});
