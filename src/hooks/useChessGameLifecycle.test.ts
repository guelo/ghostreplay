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
const uploadSessionMovesMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
}));

const initialStoreState = useGameStore.getInitialState();

const createMockCoordinator = (): GameAnalysisCoordinator =>
  ({
    startSession: vi.fn(),
    clearSession: vi.fn(),
    flushPendingUploads: vi.fn().mockResolvedValue(undefined),
    stopSessionUploads: vi.fn(),
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
  resolvedReview?: { analysisId: string; moveIndex: number; result: "pending" | "pass" | "fail" } | null;
  pendingSrsEntries?: Array<[string, {
    sessionId: string;
    analysisId: string;
    blunderId: number;
    moveIndex: number;
    userMoveSan: string;
    srs: null;
  }]>;
};

const setup = ({
  chess = new Chess(),
  moveHistory = [],
  isGameActive = false,
  isRated = true,
  playerColor = "white",
  playerColorChoice = "random",
  playerRating = 1200,
  resolvedReview = null,
  pendingSrsEntries = [],
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
  const pendingSrsReviewRef: MutableRefObject<Map<string, {
    sessionId: string;
    analysisId: string;
    blunderId: number;
    moveIndex: number;
    userMoveSan: string;
    srs: null;
  }>> = { current: new Map(pendingSrsEntries) };

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
  const setBlunderTargetFen = vi.fn();
  const setShowPassToast = vi.fn();
  const setShowRehookToast = vi.fn();
  const setReviewFailModal = vi.fn();
  const setShowPostGamePrompt = vi.fn();
  const setIsRevertPending = vi.fn();
  const setRevertError = vi.fn();
  const setShowRevertWarning = vi.fn();
  const setResolvedReview = vi.fn();
  let currentResolvedReview = resolvedReview;
  setResolvedReview.mockImplementation((value) => {
    currentResolvedReview =
      typeof value === "function" ? value(currentResolvedReview) : value;
  });

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
      setBlunderTargetFen,
      setShowPassToast,
      setShowRehookToast,
      setReviewFailModal,
      setShowPostGamePrompt,
      setIsRevertPending,
      setRevertError,
      showRevertWarning: false,
      setShowRevertWarning,
      setShowResignWarning: vi.fn(),
      setResolvedReview,
      setPendingPromotion: vi.fn(),
    }),
  );

  return {
    result,
    onOpenHistory,
    setIsRevertPending,
    setRevertError,
    setShowRevertWarning,
    setShowPostGamePrompt,
    setShowStartOverlay,
    setResolvedReview,
    pendingSrsReviewRef,
    getResolvedReview: () => currentResolvedReview,
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
  uploadSessionMovesMock.mockReset();
  uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
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

  it("records a resignation before reverting a rated game into practice mode", async () => {
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

    const { result, setIsRevertPending, setShowRevertWarning } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: true,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: {
        rating_before: 1200,
        rating_after: 1184,
        is_provisional: true,
      },
    });

    await act(async () => {
      await result.current.executeRevert();
    });

    const store = useGameStore.getState();
    expect(store.isRated).toBe(false);
    expect(store.isPracticeContinuation).toBe(true);
    expect(store.moveHistory).toEqual([]);
    expect(store.viewIndex).toBeNull();
    expect(uploadSessionMovesMock).toHaveBeenCalledWith(
      "session-123",
      expect.arrayContaining([
        expect.objectContaining({
          move_number: 1,
          color: "white",
          move_san: "e4",
        }),
        expect.objectContaining({
          move_number: 1,
          color: "black",
          move_san: "e5",
        }),
      ]),
    );
    expect(endGameMock).toHaveBeenCalledWith(
      "session-123",
      "resign",
      expect.any(String),
      true,
    );
    expect(setIsRevertPending).toHaveBeenNthCalledWith(1, true);
    expect(setIsRevertPending).toHaveBeenLastCalledWith(false);
    expect(setShowRevertWarning).toHaveBeenLastCalledWith(false);
  });

  it("prunes only pending SRS reviews removed by a local rewind", async () => {
    const chess = new Chess();
    const moveOne = chess.move("e4");
    const moveTwo = chess.move("e5");
    const moveThree = chess.move("Nf3");
    if (!moveOne || !moveTwo || !moveThree) {
      throw new Error("Unable to construct test position");
    }
    const moveHistory: MoveRecord[] = [
      { san: moveOne.san, fen: "fen-after-e4", uci: "e2e4" },
      { san: moveTwo.san, fen: "fen-after-e5", uci: "e7e5" },
      { san: moveThree.san, fen: chess.fen(), uci: "g1f3" },
    ];

    const { result, pendingSrsReviewRef } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
      pendingSrsEntries: [
        [
          "kept-analysis",
          {
            sessionId: "session-123",
            analysisId: "kept-analysis",
            blunderId: 42,
            moveIndex: 0,
            userMoveSan: "e4",
            srs: null,
          },
        ],
        [
          "removed-analysis",
          {
            sessionId: "session-123",
            analysisId: "removed-analysis",
            blunderId: 99,
            moveIndex: 2,
            userMoveSan: "Nf3",
            srs: null,
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.executeRevert();
    });

    expect(useGameStore.getState().moveHistory).toHaveLength(2);
    expect(Array.from(pendingSrsReviewRef.current.keys())).toEqual([
      "kept-analysis",
    ]);
  });

  it("prunes pending SRS reviews before rated revert network calls resolve", async () => {
    const chess = new Chess();
    const moveOne = chess.move("e4");
    const moveTwo = chess.move("e5");
    const moveThree = chess.move("Nf3");
    if (!moveOne || !moveTwo || !moveThree) {
      throw new Error("Unable to construct test position");
    }
    const moveHistory: MoveRecord[] = [
      { san: moveOne.san, fen: "fen-after-e4", uci: "e2e4" },
      { san: moveTwo.san, fen: "fen-after-e5", uci: "e7e5" },
      { san: moveThree.san, fen: chess.fen(), uci: "g1f3" },
    ];

    const { result, pendingSrsReviewRef, getResolvedReview } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: true,
      playerColor: "white",
      resolvedReview: {
        analysisId: "removed-analysis",
        moveIndex: 2,
        result: "pending",
      },
      pendingSrsEntries: [
        [
          "kept-analysis",
          {
            sessionId: "session-123",
            analysisId: "kept-analysis",
            blunderId: 42,
            moveIndex: 0,
            userMoveSan: "e4",
            srs: null,
          },
        ],
        [
          "removed-analysis",
          {
            sessionId: "session-123",
            analysisId: "removed-analysis",
            blunderId: 99,
            moveIndex: 2,
            userMoveSan: "Nf3",
            srs: null,
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    let resolveUpload!: (value: { moves_inserted: number }) => void;
    uploadSessionMovesMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: null,
    });

    let pendingRevert!: Promise<void>;
    act(() => {
      pendingRevert = result.current.executeRevert();
    });

    expect(Array.from(pendingSrsReviewRef.current.keys())).toEqual([
      "kept-analysis",
    ]);
    expect(getResolvedReview()).toBeNull();

    await act(async () => {
      resolveUpload({ moves_inserted: 3 });
      await pendingRevert;
    });
  });

  it("keeps cancelled SRS UI cleared when rated revert sealing fails", async () => {
    const chess = new Chess();
    const moveOne = chess.move("e4");
    const moveTwo = chess.move("e5");
    const moveThree = chess.move("Nf3");
    if (!moveOne || !moveTwo || !moveThree) {
      throw new Error("Unable to construct test position");
    }
    const moveHistory: MoveRecord[] = [
      { san: moveOne.san, fen: "fen-after-e4", uci: "e2e4" },
      { san: moveTwo.san, fen: "fen-after-e5", uci: "e7e5" },
      { san: moveThree.san, fen: chess.fen(), uci: "g1f3" },
    ];

    const { result, pendingSrsReviewRef, getResolvedReview, setRevertError } =
      setup({
        chess,
        moveHistory,
        isGameActive: true,
        isRated: true,
        playerColor: "white",
        resolvedReview: {
          analysisId: "removed-analysis",
          moveIndex: 2,
          result: "pending",
        },
        pendingSrsEntries: [
          [
            "removed-analysis",
            {
              sessionId: "session-123",
              analysisId: "removed-analysis",
              blunderId: 99,
              moveIndex: 2,
              userMoveSan: "Nf3",
              srs: null,
            },
          ],
        ],
      });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    uploadSessionMovesMock.mockRejectedValueOnce(new Error("upload failed"));

    await act(async () => {
      await result.current.executeRevert();
    });

    expect(useGameStore.getState().moveHistory).toHaveLength(3);
    expect(pendingSrsReviewRef.current.size).toBe(0);
    expect(getResolvedReview()).toBeNull();
    expect(setRevertError).toHaveBeenCalledWith("upload failed");
  });

  it("does not apply stale revert side effects after reset cancels a pending revert", async () => {
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
      isRated: true,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    let resolveUpload!: (value: { moves_inserted: number }) => void;
    uploadSessionMovesMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpload = resolve;
      }),
    );
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-19T00:00:00Z",
      rating: null,
    });

    const pendingRevert = result.current.executeRevert();

    act(() => {
      result.current.handleReset();
      useGameStore.setState({
        sessionId: "session-new",
        isGameActive: true,
        isRated: true,
        isPracticeContinuation: false,
      });
    });

    await act(async () => {
      resolveUpload({ moves_inserted: 2 });
      await pendingRevert;
    });

    const store = useGameStore.getState();
    expect(store.sessionId).toBe("session-new");
    expect(store.isGameActive).toBe(true);
    expect(store.isRated).toBe(true);
    expect(store.isPracticeContinuation).toBe(false);
    expect(store.moveHistory).toEqual([]);
    expect(store.liveFen).toBe(new Chess().fen());
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

  it("preserves the final move review state when terminal game finalization runs", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result, getResolvedReview } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      resolvedReview: {
        analysisId: "analysis-terminal",
        moveIndex: 0,
        result: "pass",
      },
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "checkmate_win",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    expect(getResolvedReview()).toEqual({
      analysisId: "analysis-terminal",
      moveIndex: 0,
      result: "pass",
    });

    act(() => {
      result.current.handleReset();
    });

    expect(getResolvedReview()).toBeNull();
  });

  it("clears pending SRS review registry on reset", async () => {
    const { result, pendingSrsReviewRef } = setup({
      pendingSrsEntries: [
        [
          "analysis-one",
          {
            sessionId: "session-123",
            analysisId: "analysis-one",
            blunderId: 42,
            moveIndex: 0,
            userMoveSan: "e4",
            srs: null,
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleReset();
    });

    expect(pendingSrsReviewRef.current.size).toBe(0);
  });

  it("clears pending SRS review registry when replacing an abandoned session", async () => {
    const { result, pendingSrsReviewRef, getResolvedReview } = setup({
      isGameActive: true,
      isRated: true,
      resolvedReview: {
        analysisId: "analysis-one",
        moveIndex: 0,
        result: "pending",
      },
      pendingSrsEntries: [
        [
          "analysis-one",
          {
            sessionId: "session-123",
            analysisId: "analysis-one",
            blunderId: 42,
            moveIndex: 0,
            userMoveSan: "e4",
            srs: null,
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "abandon",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
    });
    startGameMock.mockRejectedValueOnce(new Error("start failed"));

    await act(async () => {
      await result.current.handleNewGame("white");
    });

    expect(pendingSrsReviewRef.current.size).toBe(0);
    expect(getResolvedReview()).toBeNull();
  });

  it("clears pending SRS review registry before active new-game abandonment resolves", async () => {
    const { result, pendingSrsReviewRef, getResolvedReview } = setup({
      isGameActive: true,
      isRated: true,
      resolvedReview: {
        analysisId: "analysis-one",
        moveIndex: 0,
        result: "pending",
      },
      pendingSrsEntries: [
        [
          "analysis-one",
          {
            sessionId: "session-123",
            analysisId: "analysis-one",
            blunderId: 42,
            moveIndex: 0,
            userMoveSan: "e4",
            srs: null,
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    let resolveAbandon!: (value: {
      session_id: string;
      result: string;
      ended_at: string;
      rating: null;
    }) => void;
    endGameMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveAbandon = resolve;
      }),
    );
    startGameMock.mockResolvedValueOnce({ session_id: "session-new" });

    let pendingNewGame!: Promise<void>;
    act(() => {
      pendingNewGame = result.current.handleNewGame("white");
    });

    expect(pendingSrsReviewRef.current.size).toBe(0);
    expect(getResolvedReview()).toBeNull();

    await act(async () => {
      resolveAbandon({
        session_id: "session-123",
        result: "abandon",
        ended_at: "2026-04-28T00:00:00Z",
        rating: null,
      });
      await pendingNewGame;
    });
  });

  it("clears unrelated resolved review state when resignation finalization runs", async () => {
    const chess = new Chess();
    const firstMove = chess.move("e4");
    const secondMove = chess.move("e5");
    if (!firstMove || !secondMove) {
      throw new Error("Unable to construct test moves");
    }
    const { result, getResolvedReview } = setup({
      chess,
      moveHistory: [
        { san: firstMove.san, fen: "fen-after-e4", uci: "e2e4" },
        { san: secondMove.san, fen: chess.fen(), uci: "e7e5" },
      ],
      isGameActive: true,
      isRated: false,
      resolvedReview: {
        analysisId: "analysis-old",
        moveIndex: 0,
        result: "pass",
      },
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
    });

    act(() => {
      result.current.executeResign();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    expect(getResolvedReview()).toBeNull();
  });
});
