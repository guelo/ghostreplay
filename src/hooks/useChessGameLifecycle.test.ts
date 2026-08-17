import { act, renderHook, waitFor } from "@testing-library/react";
import { Chess } from "chess.js";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { MoveRecord } from "../components/chess-game/domain/movePresentation";
import { useChessGameLifecycle } from "./useChessGameLifecycle";
import { useGameStore } from "../stores/useGameStore";
import type { GameAnalysisCoordinator } from "../services/GameAnalysisCoordinator";

const fetchCurrentRatingMock = vi.fn();
const startGameMock = vi.fn();
const endGameMock = vi.fn();
const uploadSessionMovesMock = vi.fn();
const startDrillMock = vi.fn();
const continueDrillMock = vi.fn();
const abandonDrillMock = vi.fn();
const naturalEndDrillMock = vi.fn();
const getOpeningRootsMock = vi.fn();
const audioCtorMock = vi.fn();
const audioPlayMock = vi.fn();

vi.mock("../utils/api", () => ({
  fetchCurrentRating: (...args: unknown[]) => fetchCurrentRatingMock(...args),
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
  newClientRequestId: () => "final-request-123",
  startDrill: (...args: unknown[]) => startDrillMock(...args),
  continueDrill: (...args: unknown[]) => continueDrillMock(...args),
  abandonDrill: (...args: unknown[]) => abandonDrillMock(...args),
  naturalEndDrill: (...args: unknown[]) => naturalEndDrillMock(...args),
  getOpeningRoots: (...args: unknown[]) => getOpeningRootsMock(...args),
}));

const getOpeningBookMock = vi.fn();

vi.mock("../openings/openingBook", () => ({
  getOpeningBook: (...args: unknown[]) => getOpeningBookMock(...args),
}));

// The terminal handlers fire the reconcile-poll (g-fix-end-latency). Stub it as a
// spy so no real timers/network run, and assert it's invoked with the finalizing
// session id at each terminal site. (The full ../utils/api mock above omits
// getOpeningScoreDelta, so the real helper would call an undefined fn.)
const pollFreshOpeningDeltaMock = vi.fn();
const abortOpeningDeltaPollsMock = vi.fn();
vi.mock("../utils/openingDeltaPoll", () => ({
  pollFreshOpeningDelta: (...args: unknown[]) =>
    pollFreshOpeningDeltaMock(...args),
  abortOpeningDeltaPolls: () => abortOpeningDeltaPollsMock(),
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
    pruneFromMoveIndex: vi.fn(),
    markSkipped: vi.fn(),
    sessionId: null,
    // g-2nrn: the final upload validates the coordinator epoch against the
    // store BEFORE stopping uploads and again after the tail wait. Track the
    // live store id so the guard passes in the normal flow; tests that exercise
    // staleness override this per-case.
    getEpoch: vi.fn(() => ({
      generation: 0,
      sessionId: useGameStore.getState().sessionId,
    })),
    ensurePendingAnalysis: vi.fn().mockReturnValue(true),
    settleWithin: vi.fn().mockResolvedValue(undefined),
    settleLineSynchronizationWithin: vi.fn().mockResolvedValue("synchronized"),
    getLineRevision: vi.fn().mockReturnValue(0),
    transitionMoveLine: vi.fn().mockReturnValue(false),
    armLateEvaluationRepair: vi.fn().mockReturnValue(false),
    releaseLateEvaluationRepair: vi.fn(),
    cancelLateEvaluationRepair: vi.fn(),
    store: { getState: vi.fn().mockReturnValue({ analysisMap: new Map() }) },
    // Coordinator-owned recording/SRS owner (g-2m0p). The lifecycle only calls
    // cancelPendingSrsReviews on it; spy on that to assert the early-clear contract.
    decisionOwner: {
      cancelPendingSrsReviews: vi.fn(),
      registerSrsReview: vi.fn(),
      registerBlunderContext: vi.fn(),
    },
  }) as unknown as GameAnalysisCoordinator;

type SetupOptions = {
  chess?: Chess;
  moveHistory?: MoveRecord[];
  isGameActive?: boolean;
  isRated?: boolean;
  isPracticeContinuation?: boolean;
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
    srsDecisionId: string;
  }]>;
};

const setup = ({
  chess = new Chess(),
  moveHistory = [],
  isGameActive = false,
  isRated = true,
  isPracticeContinuation = false,
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
    isPracticeContinuation,
    playerColor,
    playerColorChoice,
    engineElo: 1000,
    playerRating,
    moveHistory: [...moveHistory],
    liveFen: chess.fen(),
  });

  const coordinator = createMockCoordinator();
  // Seed pending SRS reviews onto the coordinator-owned owner (g-2m0p).
  for (const [requestId, review] of pendingSrsEntries) {
    coordinator.decisionOwner.registerSrsReview(requestId, review);
  }

  const clearMoveHighlights = vi.fn();
  const resetMode = vi.fn();
  const resetEngine = vi.fn();
  const onOpenHistory = vi.fn();
  const setEngineMessage = vi.fn();
  const setIsStartingGame = vi.fn();
  const setStartError = vi.fn();
  const setShowStartOverlay = vi.fn();
  const setSeedEngineElo = vi.fn();
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
  const onGameFinished = vi.fn();

  const { result } = renderHook(() =>
    useChessGameLifecycle({
      chess,
      coordinator,
      clearMoveHighlights,
      resetMode,
      resetEngine,
      onOpenHistory,
      setEngineMessage,
      setIsStartingGame,
      setStartError,
      setShowStartOverlay,
      setSeedEngineElo,
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
      onGameFinished,
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
    setStartError,
    setSeedEngineElo,
    setResolvedReview,
    onGameFinished,
    coordinator,
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
  startDrillMock.mockReset();
  continueDrillMock.mockReset();
  abandonDrillMock.mockReset();
  naturalEndDrillMock.mockReset();
  getOpeningRootsMock.mockReset();
  getOpeningBookMock.mockReset();
  pollFreshOpeningDeltaMock.mockReset();
  uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 });
  audioCtorMock.mockReset();
  audioPlayMock.mockReset();
  audioPlayMock.mockResolvedValue(undefined);
  class MockAudio {
    constructor(src: string) {
      audioCtorMock(src);
    }

    play() {
      return audioPlayMock();
    }
  }
  vi.stubGlobal("Audio", MockAudio);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
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
      // Terminal resign-before-revert: tagged as a revert upload, and drives the
      // single opportunity recompute (g-y90g / g-upload-observe).
      expect.objectContaining({ uploadKind: "revert", recomputeOpportunity: true }),
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
    // Reconcile-poll fires for the resigned session (g-fix-end-latency).
    expect(pollFreshOpeningDeltaMock).toHaveBeenCalledWith(
      "session-123",
      "game_revert",
    );
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

    const { result, coordinator } = setup({
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
            srsDecisionId: "decision",
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
            srsDecisionId: "decision",
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.executeRevert();
    });

    expect(useGameStore.getState().moveHistory).toHaveLength(2);
    // SRS reviews for reverted indices (>= new length) are cancelled on the owner.
    expect(coordinator.decisionOwner.cancelPendingSrsReviews).toHaveBeenCalledWith(2);
    // M1: revert synchronously prunes coordinator-owned state from the new length.
    expect(coordinator.pruneFromMoveIndex).toHaveBeenCalledWith(2);
  });

  it("starts active-unrated line synchronization before the optimistic board rewind", async () => {
    const chess = new Chess();
    const moveHistory = ["e4", "e5", "Nf3"].map((san) => {
      const move = chess.move(san);
      return {
        san: move.san,
        fen: chess.fen(),
        uci: move.from + move.to + (move.promotion ?? ""),
      };
    });
    const { result, coordinator } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: false,
      isPracticeContinuation: false,
    });
    vi.mocked(coordinator.transitionMoveLine).mockImplementationOnce(
      (afterPly) => {
        expect(afterPly).toBe(2);
        // The transition owns epoch replacement + pruning while the old branch
        // is still visible; the board/store rewind follows synchronously.
        expect(useGameStore.getState().moveHistory).toHaveLength(3);
        expect(chess.history()).toHaveLength(3);
        return true;
      },
    );

    await act(async () => {
      await result.current.executeRevert();
    });

    expect(coordinator.transitionMoveLine).toHaveBeenCalledWith(2);
    expect(coordinator.pruneFromMoveIndex).not.toHaveBeenCalled();
    expect(useGameStore.getState().moveHistory).toHaveLength(2);
    expect(chess.history()).toHaveLength(2);
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

    const { result, coordinator, getResolvedReview } = setup({
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
            srsDecisionId: "decision",
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
            srsDecisionId: "decision",
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

    // The cancel ran synchronously, BEFORE the awaited upload resolves.
    expect(coordinator.decisionOwner.cancelPendingSrsReviews).toHaveBeenCalledWith(2);
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

    const { result, coordinator, getResolvedReview, setRevertError } =
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
              srsDecisionId: "decision",
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
    // Cancel ran before the (failing) seal await, so the SRS UI stays cleared.
    expect(coordinator.decisionOwner.cancelPendingSrsReviews).toHaveBeenCalledWith(2);
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

  it("shows start overlay, resets side to random, and seeds difficulty without mutating the store", async () => {
    const { result, setShowPostGamePrompt, setShowStartOverlay, setSeedEngineElo } =
      setup({ playerRating: 1350 });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    // The idle-mount rating fetch already seeded once; isolate the New Game click.
    setSeedEngineElo.mockClear();
    const eloBefore = useGameStore.getState().engineElo;
    act(() => {
      result.current.handleShowStartOverlay();
    });

    expect(useGameStore.getState().playerColorChoice).toBe("random");
    expect(setShowPostGamePrompt).toHaveBeenCalledWith(false);
    expect(setShowStartOverlay).toHaveBeenCalledWith(true);
    // Difficulty is re-randomized into the panel seed, NOT the committed store
    // engineElo — opening the New Game popup must not touch game state (g-fxrm).
    expect(setSeedEngineElo).toHaveBeenCalledTimes(1);
    expect(useGameStore.getState().engineElo).toBe(eloBefore);
  });

  it("seeds the panel difficulty from the rating sample on idle mount without mutating the store", async () => {
    const { setSeedEngineElo } = setup({ playerRating: 1350 });
    const eloBefore = useGameStore.getState().engineElo;

    // The idle (no game, no drill) rating fetch samples a difficulty near the
    // rating. In the draft model it must seed the panel, not the committed store.
    await waitFor(() => expect(setSeedEngineElo).toHaveBeenCalled());
    expect(useGameStore.getState().engineElo).toBe(eloBefore);
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

  it("plays a win clip once after successful checkmate win finalization", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    vi.spyOn(Math, "random").mockReturnValue(0);
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
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

    expect(audioCtorMock).toHaveBeenCalledTimes(1);
    expect(audioCtorMock.mock.calls[0][0]).toContain("/assets/audio/win/");
    expect(audioPlayMock).toHaveBeenCalledTimes(1);
  });

  it("plays a loss clip after successful checkmate loss finalization", async () => {
    const chess = new Chess("7K/8/6qk/8/8/8/8/8 b - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    vi.spyOn(Math, "random").mockReturnValue(0);
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "checkmate_loss",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(audioCtorMock).toHaveBeenCalledTimes(1);
    expect(audioCtorMock.mock.calls[0][0]).toContain("/assets/audio/lose/");
    expect(audioPlayMock).toHaveBeenCalledTimes(1);
  });

  it("does not play audio for draw finalization", async () => {
    const chess = new Chess("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1");
    if (!chess.isStalemate()) {
      throw new Error("Unable to construct stalemate test position");
    }
    const { result } = setup({
      chess,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "draw",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(audioCtorMock).not.toHaveBeenCalled();
  });

  it("plays end-game audio at most once for duplicate same-session finalization", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    let resolveFirst!: (value: {
      session_id: string;
      result: string;
      ended_at: string;
      rating: null;
    }) => void;
    let resolveSecond!: (value: {
      session_id: string;
      result: string;
      ended_at: string;
      rating: null;
    }) => void;
    endGameMock
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveSecond = resolve;
        }),
      );

    let first!: Promise<void>;
    let second!: Promise<void>;
    act(() => {
      first = result.current.handleGameEnd();
      second = result.current.handleGameEnd();
    });

    await act(async () => {
      resolveFirst({
        session_id: "session-123",
        result: "checkmate_win",
        ended_at: "2026-04-28T00:00:00Z",
        rating: null,
      });
      resolveSecond({
        session_id: "session-123",
        result: "checkmate_win",
        ended_at: "2026-04-28T00:00:00Z",
        rating: null,
      });
      await first;
      await second;
    });

    expect(audioCtorMock).toHaveBeenCalledTimes(1);
  });

  it("does not play audio when stale game finalization resolves after session replacement", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    let resolveEndGame!: (value: {
      session_id: string;
      result: string;
      ended_at: string;
      rating: {
        rating_before: number;
        rating_after: number;
        is_provisional: boolean;
      };
    }) => void;
    endGameMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveEndGame = resolve;
      }),
    );

    let pendingEnd!: Promise<void>;
    act(() => {
      pendingEnd = result.current.handleGameEnd();
      useGameStore.setState({
        sessionId: "session-new",
        isGameActive: true,
        isRated: true,
        isPracticeContinuation: false,
      });
    });

    await act(async () => {
      resolveEndGame({
        session_id: "session-123",
        result: "checkmate_win",
        ended_at: "2026-04-28T00:00:00Z",
        rating: {
          rating_before: 1200,
          rating_after: 1216,
          is_provisional: false,
        },
      });
      await pendingEnd;
    });

    expect(audioCtorMock).not.toHaveBeenCalled();
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        sessionId: "session-new",
        isGameActive: true,
        isRated: true,
        isPracticeContinuation: false,
        ratingChange: null,
        scoreChanges: null,
      }),
    );
  });

  it("does not play audio for terminal checkmate in practice continuation", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      isPracticeContinuation: true,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(useGameStore.getState().isGameActive).toBe(false);
    expect(endGameMock).not.toHaveBeenCalled();
    expect(audioCtorMock).not.toHaveBeenCalled();
  });

  it("clears pending SRS review registry on reset", async () => {
    const { result, coordinator } = setup({
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
            srsDecisionId: "decision",
          },
        ],
      ],
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.handleReset();
    });

    // Reset clears decision state via the coordinator's clearSession → the
    // owner's fullReset (driven by emitReset), not a React ref.
    expect(coordinator.clearSession).toHaveBeenCalledTimes(1);
    // Abandonment must also stop the delta polls, not merely invalidate their
    // results: a loop that never sees `is_fresh` never reaches the token check
    // and would keep retrying against a session the player discarded (g-f3m4).
    expect(abortOpeningDeltaPollsMock).toHaveBeenCalled();
  });

  it("clears pending SRS review registry when replacing an abandoned session", async () => {
    const { result, coordinator, getResolvedReview } = setup({
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
            srsDecisionId: "decision",
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

    expect(coordinator.decisionOwner.cancelPendingSrsReviews).toHaveBeenCalledWith();
    expect(getResolvedReview()).toBeNull();
  });

  it("clears pending SRS review registry before active new-game abandonment resolves", async () => {
    const { result, coordinator, getResolvedReview } = setup({
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
            srsDecisionId: "decision",
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

    // Cancel ran synchronously, BEFORE the awaited abandon resolves.
    expect(coordinator.decisionOwner.cancelPendingSrsReviews).toHaveBeenCalledWith();
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

  it("plays a loss clip after successful resignation finalization", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result } = setup({
      chess,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
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
    expect(audioCtorMock).toHaveBeenCalledTimes(1);
    expect(audioCtorMock.mock.calls[0][0]).toContain("/assets/audio/lose/");
  });

  it("does not apply stale resignation rating side effects after session replacement", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result } = setup({
      chess,
      isGameActive: true,
      isRated: true,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    let resolveResign!: (value: {
      session_id: string;
      result: string;
      ended_at: string;
      rating: {
        rating_before: number;
        rating_after: number;
        is_provisional: boolean;
      };
    }) => void;
    endGameMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveResign = resolve;
      }),
    );

    act(() => {
      result.current.executeResign();
      useGameStore.setState({
        sessionId: "session-new",
        isGameActive: true,
        isRated: true,
        isPracticeContinuation: false,
        ratingChange: null,
        scoreChanges: null,
      });
    });

    await act(async () => {
      resolveResign({
        session_id: "session-123",
        result: "resign",
        ended_at: "2026-04-28T00:00:00Z",
        rating: {
          rating_before: 1200,
          rating_after: 1184,
          is_provisional: false,
        },
      });
    });

    expect(audioCtorMock).not.toHaveBeenCalled();
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        sessionId: "session-new",
        isGameActive: true,
        isRated: true,
        isPracticeContinuation: false,
        ratingChange: null,
        scoreChanges: null,
      }),
    );
  });

  it("does not play audio when resignation finalization fails", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result } = setup({
      chess,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockRejectedValueOnce(new Error("resign failed"));

    act(() => {
      result.current.executeResign();
    });

    await waitFor(() => expect(endGameMock).toHaveBeenCalledTimes(1));
    expect(audioCtorMock).not.toHaveBeenCalled();
  });

  it("does not play audio when practice continuation is ended by resignation", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result } = setup({
      chess,
      isGameActive: true,
      isRated: false,
      isPracticeContinuation: true,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.executeResign();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    expect(useGameStore.getState().gameResult).toEqual({
      type: "resign",
      message: "Practice ended.",
    });
    expect(endGameMock).not.toHaveBeenCalled();
    expect(audioCtorMock).not.toHaveBeenCalled();
  });

  it("abandons an unconverted drill instead of ending it as a game", async () => {
    const { result, coordinator } = setup({
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });
    useGameStore.setState({
      sessionId: "drill-session-123",
      drillOpeningKey: "target-fen",
      drillState: "failed",
      drillStrictness: "standard",
    });
    abandonDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-123",
      mode: "drill",
      // The server preserves the terminal outcome across abandon
      // (g-drill-failed-overwrite); the store still finalizes to "abandoned".
      drill_state: "failed",
      opening_key: "target-fen",
      opening_name: "Target",
      opening_family: "Target",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    act(() => {
      result.current.executeResign();
    });

    await waitFor(() =>
      expect(abandonDrillMock).toHaveBeenCalledWith("drill-session-123"),
    );
    expect(endGameMock).not.toHaveBeenCalled();
    expect(coordinator.flushPendingUploads).not.toHaveBeenCalled();
    expect(coordinator.stopSessionUploads).toHaveBeenCalledTimes(1);
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        isGameActive: false,
        drillState: "abandoned",
        isRated: false,
        gameResult: {
          type: "resign",
          message: "Drill abandoned.",
        },
      }),
    );
    expect(audioCtorMock).not.toHaveBeenCalled();
  });

  it("abandonStoppedDrill finalizes the failed drill unrated without ending it as a game", async () => {
    const { result, coordinator } = setup({
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });
    useGameStore.setState({
      sessionId: "drill-session-456",
      drillOpeningKey: "target-fen",
      drillState: "failed",
    });
    abandonDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-456",
      // Server keeps 'failed'; the store's drillState is the CLIENT lifecycle
      // sentinel and must still land on "abandoned" (g-drill-failed-overwrite).
      drill_state: "failed",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    await act(async () => {
      await result.current.abandonStoppedDrill();
    });

    expect(abandonDrillMock).toHaveBeenCalledWith("drill-session-456");
    expect(endGameMock).not.toHaveBeenCalled();
    expect(coordinator.stopSessionUploads).toHaveBeenCalledTimes(1);
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        isGameActive: false,
        drillState: "abandoned",
        isRated: false,
        gameResult: { type: "resign", message: "Drill abandoned." },
      }),
    );
  });

  it("abandonStoppedDrill propagates abandon failure without finalizing locally", async () => {
    const { result } = setup({
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });
    useGameStore.setState({
      sessionId: "drill-session-789",
      drillOpeningKey: "target-fen",
      drillState: "failed",
    });
    abandonDrillMock.mockRejectedValueOnce(new Error("network down"));

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

    await expect(
      act(async () => {
        await result.current.abandonStoppedDrill();
      }),
    ).rejects.toThrow("network down");

    // Drill stays active; not finalized locally.
    expect(useGameStore.getState().isGameActive).toBe(true);
    expect(useGameStore.getState().gameResult).toBeNull();
  });

  it("handleNewDrill calls startDrill API and sets store correctly", async () => {
    const { result } = setup();

    startDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-123",
      mode: "drill",
      drill_state: "active",
      opening_key: "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
      opening_name: "Sicilian Defense",
      opening_family: "Sicilian",
      eco: "B20",
      depth: 1,
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
    });

    getOpeningBookMock.mockResolvedValueOnce({
      byEpd: new Map(),
    });

    await act(async () => {
      await result.current.handleNewDrill({
        openingKey: "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        playerColor: "white",
        engineElo: 1000,
        strictness: "standard",
        strictnessCp: 25,
      });
    });

    expect(startDrillMock).toHaveBeenCalledWith({
      opening_key: "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      strictness_cp: 25,
    });

    const store = useGameStore.getState();
    expect(store.isGameActive).toBe(true);
    expect(store.sessionId).toBe("drill-session-123");
    expect(store.drillOpeningKey).toBe("rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2");
    expect(store.drillOpeningName).toBe("Sicilian Defense");
    expect(store.drillState).toBe("active");
    expect(store.drillStrictness).toBe("standard");
    expect(store.isRated).toBe(false);
  });

  it("finalizes an abandoned drill locally before a replacement start that fails", async () => {
    const {
      result,
      coordinator,
      setShowStartOverlay,
      setStartError,
    } = setup({
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });
    useGameStore.setState({
      sessionId: "drill-session-old",
      drillOpeningKey: "old-target",
      drillState: "active",
      drillStrictness: "standard",
    });
    abandonDrillMock.mockResolvedValueOnce({ drill_state: "abandoned" });
    let rejectStart!: (error: Error) => void;
    startDrillMock.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectStart = reject;
        }),
    );

    let pendingReplacement!: Promise<unknown>;
    act(() => {
      pendingReplacement = result.current.handleNewDrill({
        openingKey: "new-target",
        playerColor: "white",
        engineElo: 1000,
        strictness: "standard",
        strictnessCp: 25,
      });
    });

    await waitFor(() => expect(startDrillMock).toHaveBeenCalledTimes(1));
    expect(abandonDrillMock).toHaveBeenCalledWith("drill-session-old");
    expect(abandonDrillMock.mock.invocationCallOrder[0]).toBeLessThan(
      startDrillMock.mock.invocationCallOrder[0],
    );
    expect(coordinator.stopSessionUploads).toHaveBeenCalledTimes(1);
    expect(coordinator.clearSession).toHaveBeenCalledTimes(1);
    expect(coordinator.startSession).not.toHaveBeenCalled();
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        sessionId: "drill-session-old",
        isGameActive: false,
        isRated: false,
        drillState: "abandoned",
        gameResult: { type: "resign", message: "Drill abandoned." },
        departingSessionId: "drill-session-old",
      }),
    );

    await act(async () => {
      rejectStart(new Error("replacement unavailable"));
      await pendingReplacement;
    });

    expect(setStartError).toHaveBeenCalledWith("replacement unavailable");
    expect(setShowStartOverlay).not.toHaveBeenCalled();
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        sessionId: "drill-session-old",
        isGameActive: false,
        isRated: false,
        drillState: "abandoned",
        gameResult: { type: "resign", message: "Drill abandoned." },
        departingSessionId: null,
      }),
    );
  });

  it("handleNewDrill forwards the ad-hoc line to startDrill", async () => {
    const { result } = setup();

    startDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-line",
      mode: "drill",
      drill_state: "active",
      opening_key: "target-fen",
      opening_name: "Custom line",
      opening_family: "Custom line",
      eco: null,
      depth: 2,
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
    });

    getOpeningBookMock.mockResolvedValueOnce({ byEpd: new Map() });

    await act(async () => {
      await result.current.handleNewDrill({
        openingKey: "target-fen",
        playerColor: "white",
        engineElo: 1000,
        strictness: "standard",
        strictnessCp: 25,
        line: ["e2e4", "c7c5"],
      });
    });

    // The card's UCI line rides along so the backend can validate + persist it.
    expect(startDrillMock).toHaveBeenCalledWith({
      opening_key: "target-fen",
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      strictness_cp: 25,
      line: ["e2e4", "c7c5"],
    });
  });

  it("handleNewDrill starts from the initial position without replaying the root", async () => {
    const { result } = setup();

    startDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-456",
      mode: "drill",
      drill_state: "active",
      opening_key: "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
      opening_name: "French Defense",
      opening_family: "French",
      eco: "C00",
      depth: 1,
      player_color: "white",
      engine_elo: 1200,
      strictness: "strict",
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
    });

    await act(async () => {
      await result.current.handleNewDrill({
        openingKey: "rnbqkbnr/pppp1ppp/4p3/8/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
        playerColor: "white",
        engineElo: 1200,
        strictness: "strict",
        strictnessCp: 0,
      });
    });

    const store = useGameStore.getState();
    expect(store.liveFen).toBe("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
    expect(store.moveHistory).toEqual([]);
    expect(getOpeningBookMock).not.toHaveBeenCalled();
  });

  it("handleNewDrill keeps the target root as metadata while the board stays at start", async () => {
    const { result } = setup();

    startDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-789",
      mode: "drill",
      drill_state: "active",
      opening_key: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
      opening_name: "Unknown",
      opening_family: "",
      eco: null,
      depth: 0,
      player_color: "black",
      engine_elo: 800,
      strictness: "lenient",
      is_rated: false,
      rated_start_ply: null,
      normal_started_at: null,
      converted_at: null,
    });

    await act(async () => {
      await result.current.handleNewDrill({
        openingKey: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        playerColor: "black",
        engineElo: 800,
        strictness: "lenient",
        strictnessCp: 50,
      });
    });

    const store = useGameStore.getState();
    expect(store.liveFen).toBe("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
    expect(store.moveHistory).toEqual([]);
    expect(store.drillOpeningKey).toBe("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1");
    expect(getOpeningBookMock).not.toHaveBeenCalled();
  });

  it("handleContinueDrill converts only after root_reached", async () => {
    const { result, coordinator } = setup({
      isGameActive: true,
      isRated: false,
      moveHistory: [
        {
          san: "e4",
          fen: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
          uci: "e2e4",
        },
      ],
    });
    useGameStore.setState({
      sessionId: "drill-session-123",
      drillOpeningKey: "target-fen",
      drillState: "root_reached",
      drillStrictness: "standard",
    });
    continueDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-123",
      mode: "drill",
      drill_state: "converted",
      opening_key: "target-fen",
      opening_name: "Target",
      opening_family: "Target",
      eco: null,
      depth: 1,
      player_color: "white",
      engine_elo: 1000,
      strictness: "standard",
      is_rated: true,
      rated_start_ply: 1,
      normal_started_at: "2026-05-20T00:00:00Z",
      converted_at: "2026-05-20T00:00:00Z",
    });

    await act(async () => {
      await result.current.handleContinueDrill();
    });

    expect(coordinator.flushPendingUploads).toHaveBeenCalledTimes(1);
    expect(continueDrillMock).toHaveBeenCalledWith("drill-session-123", 1);
    expect(useGameStore.getState()).toEqual(
      expect.objectContaining({
        drillState: "converted",
        isRated: true,
        isPracticeContinuation: false,
      }),
    );
    expect(coordinator.startSession).not.toHaveBeenCalled();
  });

  // --- opening-score delta wiring (g-xanz) --------------------------------

  const OPENING_CHANGES = [
    {
      opening_key: "k1",
      opening_name: "Italian Game",
      opening_family: "Italian Game",
      eco: "C50",
      depth: 3,
      before: 41,
      after: 44,
      delta: 3,
      is_new: false,
    },
  ];

  it("stamps the opening delta from the game-end response and uploads the full history first", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "checkmate_win",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    // The delta is stamped with the session that earned it (g-f3m4).
    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "session-123",
      items: OPENING_CHANGES,
      freshness: "pending",
    });
    // The reconcile-poll fires for the finalizing session (g-fix-end-latency).
    expect(pollFreshOpeningDeltaMock).toHaveBeenCalledWith(
      "session-123",
      "game_end",
    );
    // P1: the full move history is uploaded BEFORE endGame's recompute. Assert
    // order (the bug was ordering-specific), not just that both were called.
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    expect(uploadSessionMovesMock.mock.invocationCallOrder[0]).toBeLessThan(
      endGameMock.mock.invocationCallOrder[0],
    );
  });

  it("stamps the opening delta from the resign response (P2)", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result } = setup({
      chess,
      moveHistory: [{ san: "e4", fen: chess.fen(), uci: "e2e4" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    act(() => {
      result.current.executeResign();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    // The delta is stamped with the session that earned it (g-f3m4).
    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "session-123",
      items: OPENING_CHANGES,
      freshness: "pending",
    });
    // The reconcile-poll fires for the finalizing session (g-fix-end-latency).
    expect(pollFreshOpeningDeltaMock).toHaveBeenCalledWith(
      "session-123",
      "game_resign",
    );
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    expect(uploadSessionMovesMock.mock.invocationCallOrder[0]).toBeLessThan(
      endGameMock.mock.invocationCallOrder[0],
    );
  });

  it("stamps the opening delta from the natural-end drill contract", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result, coordinator } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });
    useGameStore.setState({
      sessionId: "drill-session-xanz",
      drillOpeningKey: "target-fen",
      drillState: "active",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    naturalEndDrillMock.mockResolvedValueOnce({
      session_id: "drill-session-xanz",
      drill_state: "failed",
      terminal_reason: "natural_end",
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(naturalEndDrillMock).toHaveBeenCalled();
    // The delta is stamped with the session that earned it (g-f3m4).
    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "drill-session-xanz",
      items: OPENING_CHANGES,
      freshness: "pending",
    });
    // The reconcile-poll fires for the finalizing drill session (g-fix-end-latency).
    expect(pollFreshOpeningDeltaMock).toHaveBeenCalledWith(
      "drill-session-xanz",
      "drill_natural_end",
    );
    // P1: the drill's moves are uploaded BEFORE naturalEndDrill recomputes (and
    // before stopSessionUploads discards the tail). Assert order, not just calls.
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    expect(uploadSessionMovesMock.mock.invocationCallOrder[0]).toBeLessThan(
      naturalEndDrillMock.mock.invocationCallOrder[0],
    );
    expect(coordinator.stopSessionUploads).toHaveBeenCalled();
  });

  it("still records the terminal endpoint when the final upload fails/times out", async () => {
    const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
    const move = chess.move({ from: "g6", to: "g7" });
    if (!move || !chess.isCheckmate()) {
      throw new Error("Unable to construct terminal test move");
    }
    const { result } = setup({
      chess,
      moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    // The bounded upload hangs/aborts — must not block endGame.
    uploadSessionMovesMock.mockRejectedValueOnce(new Error("upload timed out"));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "checkmate_win",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    // The primary terminal action still ran despite the upload failure.
    expect(endGameMock).toHaveBeenCalled();
    // The delta is stamped with the session that earned it (g-f3m4).
    expect(useGameStore.getState().openingScoreDelta).toEqual({
      sessionId: "session-123",
      items: OPENING_CHANGES,
      freshness: "pending",
    });
  });

  // g-y90g: the final full upload stops the incremental uploader FIRST (folded
  // into uploadFullMoveHistoryBeforeEnd, so every terminal path inherits it) and
  // carries recomputeOpportunity:true so opportunity is recomputed exactly once.
  it("game-end: stops uploads before the final upload and flags the opportunity recompute", async () => {
    const chess = new Chess();
    const moveHistory = ["f3", "e5", "g4", "Qh4#"].map((san) => {
      const move = chess.move(san);
      return {
        san: move.san,
        fen: chess.fen(),
        uci: move.from + move.to + (move.promotion ?? ""),
      };
    });
    if (!chess.isCheckmate()) throw new Error("Unable to construct terminal test line");
    const { result, coordinator } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "checkmate_win",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(coordinator.stopSessionUploads).toHaveBeenCalled();
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    // stop precedes the final upload (the folded-in invariant).
    expect(
      vi.mocked(coordinator.stopSessionUploads).mock.invocationCallOrder[0],
    ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
    // The final upload is tagged final_full with the game-end terminal action and
    // drives the single opportunity recompute (g-upload-observe).
    expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
      expect.objectContaining({
        uploadKind: "final_full",
        terminalAction: "game_end",
        recomputeOpportunity: true,
      }),
    );
    expect(uploadSessionMovesMock.mock.calls[0][2].deadlineMs).toEqual(
      expect.any(Number),
    );
    const finalPayload = uploadSessionMovesMock.mock.calls[0][1];
    expect(finalPayload).toHaveLength(4);
    // g-broken-ply-grids: final_full serializes the whole history, not only
    // resolved analysis indices, and keeps the canonical coordinate grid even
    // when an interior analysis is absent.
    expect(
      finalPayload.map(
        (move: { move_number: number; color: string }) => [
          move.move_number,
          move.color,
        ],
      ),
    ).toEqual([
      [1, "white"],
      [1, "black"],
      [2, "white"],
      [2, "black"],
    ]);
    expect(finalPayload[2]).toEqual(
      expect.objectContaining({
        eval_cp: null,
        eval_mate: null,
      }),
    );
    expect(finalPayload[2]).not.toHaveProperty(
      "synthetic_terminal_eval",
    );
    expect(finalPayload[3]).toEqual(
      expect.objectContaining({
        move_san: "Qh4#",
        eval_cp: 10000,
        eval_mate: 0,
        eval_delta: 0,
        synthetic_terminal_eval: true,
      }),
    );
  });

  // ---------------------------------------------------------------
  // g-2nrn / g-history-accuracy: bounded unresolved-evaluation recovery ahead
  // of the final upload
  // ---------------------------------------------------------------
  describe("terminal unresolved-evaluation recovery", () => {
    const buildTerminalGame = () => {
      const chess = new Chess();
      const moveHistory = ["f3", "e5", "g4", "Qh4#"].map((san) => {
        const move = chess.move(san);
        return {
          san: move.san,
          fen: chess.fen(),
          uci: move.from + move.to + (move.promotion ?? ""),
        };
      });
      return { chess, moveHistory };
    };

    const endGameOnce = () =>
      endGameMock.mockResolvedValueOnce({
        session_id: "session-123",
        result: "checkmate_win",
        ended_at: "2026-04-28T00:00:00Z",
        rating: null,
        opening_score_changes: OPENING_CHANGES,
      });

    it("waits on every unresolved nonterminal ply, excluding the synthetic terminal ply", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      endGameOnce();

      await act(async () => {
        await result.current.handleGameEnd();
      });

      // All real analyses are unresolved in this production-shaped mock. The
      // terminal ply (3) is filled deterministically by fillUnresolvedTerminal,
      // but every earlier gap must be retried and included in the shared wait.
      expect(coordinator.settleWithin).toHaveBeenCalledTimes(1);
      expect(vi.mocked(coordinator.settleWithin).mock.calls[0][0]).toEqual([
        0, 1, 2,
      ]);
      expect(vi.mocked(coordinator.settleWithin).mock.calls[0][0]).not.toContain(3);
    });

    it("resignation waits on and arms the final non-terminal ply", async () => {
      const chess = new Chess();
      const move = chess.move("e4");
      if (!move) throw new Error("Unable to construct resignation test move");
      const moveHistory = [{
        san: move.san,
        fen: chess.fen(),
        uci: move.from + move.to,
      }];
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      vi.mocked(coordinator.armLateEvaluationRepair).mockReturnValueOnce(true);

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "resign",
        );
      });

      expect(coordinator.settleWithin).toHaveBeenCalledTimes(1);
      expect(vi.mocked(coordinator.settleWithin).mock.calls[0][0]).toEqual([0]);
      const waitBudget = vi.mocked(coordinator.settleWithin).mock.calls[0][1];
      expect(waitBudget).toBeGreaterThan(0);
      expect(waitBudget).toBeLessThanOrEqual(300);
      expect(Number.isInteger(waitBudget)).toBe(true);
      expect(coordinator.armLateEvaluationRepair).toHaveBeenCalledWith(
        "session-123",
        0,
        0,
        moveHistory,
        "final-request-123",
      );
      const payload = uploadSessionMovesMock.mock.calls[0][1];
      expect(payload[0]).toEqual(expect.objectContaining({
        move_san: "e4",
        eval_cp: null,
        eval_mate: null,
      }));
      expect(payload[0]).not.toHaveProperty("synthetic_terminal_eval");
    });

    it("waits AFTER stopping ordinary uploads, before the final full upload", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      endGameOnce();

      await act(async () => {
        await result.current.handleGameEnd();
      });

      // stop -> wait -> upload. Stopping does NOT stop analysis resolution, so
      // this ordering preserves final-full ownership while exposing a late eval.
      const stopOrder = vi.mocked(coordinator.stopSessionUploads).mock
        .invocationCallOrder[0];
      const waitOrder = vi.mocked(coordinator.settleWithin).mock
        .invocationCallOrder[0];
      expect(stopOrder).toBeLessThan(waitOrder);
      expect(waitOrder).toBeLessThan(
        uploadSessionMovesMock.mock.invocationCallOrder[0],
      );
    });

    it("arms each unresolved repair and releases them only after final_full settles", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      endGameOnce();
      vi.mocked(coordinator.armLateEvaluationRepair).mockReturnValue(true);

      await act(async () => {
        await result.current.handleGameEnd();
      });

      expect(coordinator.armLateEvaluationRepair).toHaveBeenCalledTimes(3);
      for (const moveIndex of [0, 1, 2]) {
        expect(coordinator.armLateEvaluationRepair).toHaveBeenCalledWith(
          "session-123",
          0,
          moveIndex,
          moveHistory,
          "final-request-123",
        );
      }
      expect(
        vi.mocked(coordinator.armLateEvaluationRepair).mock.invocationCallOrder[0],
      ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
      expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
        expect.objectContaining({ clientRequestId: "final-request-123" }),
      );
      expect(uploadSessionMovesMock.mock.invocationCallOrder[0]).toBeLessThan(
        vi.mocked(coordinator.releaseLateEvaluationRepair).mock
          .invocationCallOrder[0],
      );
    });

    it("disarms only the row that resolves into the final snapshot", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      vi.mocked(coordinator.armLateEvaluationRepair).mockReturnValue(true);
      vi.mocked(coordinator.store.getState).mockReturnValueOnce({
        analysisMap: new Map(),
      } as ReturnType<GameAnalysisCoordinator["store"]["getState"]>);
      vi.mocked(coordinator.store.getState).mockReturnValue({
        analysisMap: new Map([[2, { playedEval: 12 }]]),
      } as ReturnType<GameAnalysisCoordinator["store"]["getState"]>);

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "game_end",
        );
      });

      expect(coordinator.cancelLateEvaluationRepair).toHaveBeenCalledWith(
        "session-123",
        0,
        2,
      );
      expect(coordinator.releaseLateEvaluationRepair).toHaveBeenCalledWith(
        "session-123",
        0,
      );
    });

    it("hands the upload only the REMAINDER of the absolute deadline", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
      endGameOnce();

      // Burn ~250ms of the budget inside the tail wait.
      vi.mocked(coordinator.settleWithin).mockImplementationOnce(
        () => new Promise((resolve) => setTimeout(resolve, 250)),
      );

      await act(async () => {
        await result.current.handleGameEnd();
      });

      // The terminal bound must stay 4s, not drift to 4.25s: uploadSessionMoves is
      // handed deadlineMs = 4000 - elapsed (it constructs its own timeout from that
      // value, so the recorded deadline and the live signal cannot drift), never a
      // fresh 4000.
      const options = uploadSessionMovesMock.mock.calls[0][2] as {
        uploadKind: string;
        deadlineMs: number;
      };
      expect(options.uploadKind).toBe("final_full");
      const granted = options.deadlineMs;
      expect(granted).toBeLessThan(4000);
      expect(granted).toBeLessThanOrEqual(3750);
      // Floored to an integer — AbortSignal.timeout rejects a fractional delay.
      expect(Number.isInteger(granted)).toBe(true);
    });

    it("gives the tail first claim and line sync only the unused 300ms subdeadline", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      let now = 1000;
      vi.spyOn(performance, "now").mockImplementation(() => now);
      vi.mocked(coordinator.settleWithin).mockImplementationOnce(async () => {
        now += 5;
      });

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "game_end",
        );
      });

      expect(coordinator.settleWithin).toHaveBeenCalledWith([0, 1, 2], 300);
      expect(
        coordinator.settleLineSynchronizationWithin,
      ).toHaveBeenCalledWith(295);
      expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
        expect.objectContaining({ deadlineMs: 3995 }),
      );
    });

    it("a full 300ms tail leaves no extra sync wait and preserves the 3700ms final_full floor", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      let now = 2000;
      vi.spyOn(performance, "now").mockImplementation(() => now);
      vi.mocked(coordinator.settleWithin).mockImplementationOnce(async () => {
        now += 300;
      });

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "game_end",
        );
      });

      expect(
        coordinator.settleLineSynchronizationWithin,
      ).toHaveBeenCalledWith(0);
      expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
        expect.objectContaining({ deadlineMs: 3700 }),
      );
    });

    it("records an unsynchronized verdict and still attempts final_full", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      vi.mocked(coordinator.settleLineSynchronizationWithin).mockResolvedValueOnce(
        "permanent_conflict",
      );
      vi.mocked(coordinator.getLineRevision).mockReturnValueOnce(7);

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "game_end",
        );
      });

      expect(uploadSessionMovesMock).toHaveBeenCalledOnce();
      expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
        expect.objectContaining({
          lineRevision: 7,
          lineSyncVerdict: "permanent_conflict",
          recomputeOpportunity: true,
          clientRequestId: "final-request-123",
        }),
      );
    });

    it("uploads nothing when the session was already replaced (step-1 guard)", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

      // A stale invocation for a session that is no longer current must stop
      // NOTHING — otherwise it would disable the NEW session's uploads.
      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-stale",
          "game_end",
        );
      });

      expect(coordinator.stopSessionUploads).not.toHaveBeenCalled();
      expect(coordinator.settleWithin).not.toHaveBeenCalled();
      expect(uploadSessionMovesMock).not.toHaveBeenCalled();
    });

    it("uploads nothing when a new session starts DURING the wait (step-5 guard)", async () => {
      const { chess, moveHistory } = buildTerminalGame();
      const { result, coordinator } = setup({
        chess, moveHistory, isGameActive: true, isRated: false, playerColor: "white",
      });
      await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));

      // startSession() mid-wait bumps the generation, replaces uploadState and
      // clears analysisMap. Building the payload afterwards would persist the
      // NEW session's data under the OLD session id.
      vi.mocked(coordinator.getEpoch)
        .mockReturnValueOnce({ generation: 0, sessionId: "session-123" })
        .mockReturnValueOnce({ generation: 1, sessionId: "session-123" });

      await act(async () => {
        await result.current.uploadFullMoveHistoryBeforeEnd(
          "session-123",
          "game_end",
        );
      });

      expect(coordinator.stopSessionUploads).toHaveBeenCalledTimes(1);
      expect(uploadSessionMovesMock).not.toHaveBeenCalled();
    });
  });

  // g-terminal-draws: the same final-upload path fills an unresolved terminal
  // DRAW ply through the draw branch of the shared helper — eval_cp=0,
  // eval_mate=null, eval_delta=null, synthetic_terminal_eval=true — proving the
  // draw values (not just checkmate) reach the payload end-to-end.
  it("game-end: fills an unresolved terminal-draw final ply on the final upload", async () => {
    const chess = new Chess();
    // Sam Loyd's fastest stalemate: White's final Qe6 stalemates Black.
    const moveHistory = [
      "e3", "a5", "Qh5", "Ra6", "Qxa5", "h5", "Qxc7", "Rah6", "h4", "f6",
      "Qxd7+", "Kf7", "Qxb7", "Qd3", "Qxb8", "Qh7", "Qxc8", "Kg6", "Qe6",
    ].map((san) => {
      const move = chess.move(san);
      return {
        san: move.san,
        fen: chess.fen(),
        uci: move.from + move.to + (move.promotion ?? ""),
      };
    });
    if (!chess.isStalemate()) throw new Error("Unable to construct stalemate test line");
    const { result, coordinator } = setup({
      chess,
      moveHistory,
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "draw",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.handleGameEnd();
    });

    expect(coordinator.stopSessionUploads).toHaveBeenCalled();
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    // stop precedes the final upload (the folded-in invariant), unchanged by draws.
    expect(
      vi.mocked(coordinator.stopSessionUploads).mock.invocationCallOrder[0],
    ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
    const payload = uploadSessionMovesMock.mock.calls[0][1];
    expect(payload).toHaveLength(moveHistory.length);
    expect(payload[payload.length - 1]).toEqual(
      expect.objectContaining({
        move_san: "Qe6",
        eval_cp: 0,
        eval_mate: null,
        eval_delta: null,
        synthetic_terminal_eval: true,
      }),
    );
    // Only the terminal ply is stamped; the penultimate stays unresolved/unmarked.
    expect(payload[payload.length - 2]).not.toHaveProperty("synthetic_terminal_eval");
  });

  it("resign: stops uploads before the final upload and flags the opportunity recompute", async () => {
    const chess = new Chess();
    chess.move("e4");
    const { result, coordinator } = setup({
      chess,
      moveHistory: [{ san: "e4", fen: chess.fen(), uci: "e2e4" }],
      isGameActive: true,
      isRated: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      result.current.executeResign();
    });

    await waitFor(() => expect(useGameStore.getState().isGameActive).toBe(false));
    expect(coordinator.stopSessionUploads).toHaveBeenCalled();
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    expect(
      vi.mocked(coordinator.stopSessionUploads).mock.invocationCallOrder[0],
    ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
    expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
      expect.objectContaining({
        uploadKind: "final_full",
        terminalAction: "resign",
        recomputeOpportunity: true,
      }),
    );
  });

  it("revert: stops uploads before the snapshot upload and flags the opportunity recompute", async () => {
    const chess = new Chess();
    const moveOne = chess.move("e4");
    const fenAfterMoveOne = chess.fen();
    const moveTwo = chess.move("e5");
    const fenAfterMoveTwo = chess.fen();
    if (!moveOne || !moveTwo) {
      throw new Error("Unable to construct test position");
    }
    const { result, coordinator } = setup({
      chess,
      moveHistory: [
        { san: moveOne.san, fen: fenAfterMoveOne, uci: "e2e4" },
        { san: moveTwo.san, fen: fenAfterMoveTwo, uci: "e7e5" },
      ],
      isGameActive: true,
      isRated: true,
      isPracticeContinuation: false,
      playerColor: "white",
    });

    await waitFor(() => expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1));
    endGameMock.mockResolvedValueOnce({
      session_id: "session-123",
      result: "resign",
      ended_at: "2026-04-28T00:00:00Z",
      rating: null,
      opening_score_changes: OPENING_CHANGES,
    });

    await act(async () => {
      await result.current.executeRevert();
    });

    expect(coordinator.stopSessionUploads).toHaveBeenCalled();
    expect(uploadSessionMovesMock).toHaveBeenCalled();
    // executeRevert uploads the pre-revert snapshot directly (not via
    // uploadFullMoveHistoryBeforeEnd), so it owns its own stop-before-upload.
    expect(
      vi.mocked(coordinator.stopSessionUploads).mock.invocationCallOrder[0],
    ).toBeLessThan(uploadSessionMovesMock.mock.invocationCallOrder[0]);
    expect(uploadSessionMovesMock.mock.calls[0][2]).toEqual(
      expect.objectContaining({ uploadKind: "revert", recomputeOpportunity: true }),
    );
  });

  describe("end-game announcement (reason tagging + onGameFinished)", () => {
    // Load-bearing coverage for g-8079: synthetic GameResults in the component
    // tests can't catch a missed `reason` on a real lifecycle path, so drive the
    // actual terminal branches and assert the STORED gameResult.reason plus the
    // onGameFinished payload.
    const endGameResolves = (result: string) =>
      endGameMock.mockResolvedValueOnce({
        session_id: "session-123",
        result,
        ended_at: "2026-04-28T00:00:00Z",
        rating: null,
      });

    it("tags a checkmate win 'checkmate' and fires onGameFinished once", async () => {
      const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
      const move = chess.move({ from: "g6", to: "g7" });
      if (!move || !chess.isCheckmate()) {
        throw new Error("Unable to construct checkmate win position");
      }
      const { result, onGameFinished } = setup({
        chess,
        moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );
      endGameResolves("checkmate_win");

      await act(async () => {
        await result.current.handleGameEnd();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(useGameStore.getState().gameResult).toMatchObject({
        type: "checkmate_win",
        reason: "checkmate",
      });
      expect(onGameFinished).toHaveBeenCalledTimes(1);
      expect(onGameFinished).toHaveBeenCalledWith(
        expect.objectContaining({ type: "checkmate_win", reason: "checkmate" }),
      );
    });

    it("tags a checkmate loss 'checkmate'", async () => {
      const chess = new Chess("7K/8/6qk/8/8/8/8/8 b - - 0 1");
      const move = chess.move({ from: "g6", to: "g7" });
      if (!move || !chess.isCheckmate()) {
        throw new Error("Unable to construct checkmate loss position");
      }
      const { result, onGameFinished } = setup({
        chess,
        moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );
      endGameResolves("checkmate_loss");

      await act(async () => {
        await result.current.handleGameEnd();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(useGameStore.getState().gameResult).toMatchObject({
        type: "checkmate_loss",
        reason: "checkmate",
      });
      expect(onGameFinished).toHaveBeenCalledWith(
        expect.objectContaining({ type: "checkmate_loss", reason: "checkmate" }),
      );
    });

    it.each([
      ["stalemate", "7k/5Q2/6K1/8/8/8/8/8 b - - 0 1", undefined, "stalemate"],
      ["insufficient material", "k7/8/K7/8/8/8/8/8 w - - 0 1", undefined, "insufficient"],
      ["fifty-move", "4k3/8/4K3/4R3/8/8/8/8 w - - 100 60", undefined, "fifty_move"],
    ] as const)(
      "tags a %s draw with its reason and fires onGameFinished",
      async (_label, fen, _moves, expectedReason) => {
        const chess = new Chess(fen);
        const { result, onGameFinished } = setup({
          chess,
          isGameActive: true,
          isRated: false,
          playerColor: "white",
        });
        await waitFor(() =>
          expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
        );
        endGameResolves("draw");

        await act(async () => {
          await result.current.handleGameEnd();
        });

        await waitFor(() =>
          expect(useGameStore.getState().isGameActive).toBe(false),
        );
        expect(useGameStore.getState().gameResult).toMatchObject({
          type: "draw",
          reason: expectedReason,
        });
        expect(onGameFinished).toHaveBeenCalledWith(
          expect.objectContaining({ type: "draw", reason: expectedReason }),
        );
      },
    );

    it("tags a threefold-repetition draw 'threefold'", async () => {
      // Threefold needs real repeated positions in the Chess instance; a bare
      // FEN can't express the position-count history.
      const chess = new Chess();
      for (const m of ["Nf3", "Nf6", "Ng1", "Ng8", "Nf3", "Nf6", "Ng1", "Ng8"]) {
        chess.move(m);
      }
      if (!chess.isThreefoldRepetition()) {
        throw new Error("Unable to construct threefold position");
      }
      const { result, onGameFinished } = setup({
        chess,
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );
      endGameResolves("draw");

      await act(async () => {
        await result.current.handleGameEnd();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(useGameStore.getState().gameResult).toMatchObject({
        type: "draw",
        reason: "threefold",
      });
      expect(onGameFinished).toHaveBeenCalledWith(
        expect.objectContaining({ type: "draw", reason: "threefold" }),
      );
    });

    it("tags a genuine resignation 'resignation' and fires onGameFinished", async () => {
      const chess = new Chess();
      chess.move("e4");
      const { result, onGameFinished } = setup({
        chess,
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );
      endGameResolves("resign");

      act(() => {
        result.current.executeResign();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(useGameStore.getState().gameResult).toMatchObject({
        type: "resign",
        reason: "resignation",
      });
      expect(onGameFinished).toHaveBeenCalledWith(
        expect.objectContaining({ type: "resign", reason: "resignation" }),
      );
    });

    it("does not fire onGameFinished for a practice-continuation end", async () => {
      const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
      const move = chess.move({ from: "g6", to: "g7" });
      if (!move || !chess.isCheckmate()) {
        throw new Error("Unable to construct checkmate position");
      }
      const { result, onGameFinished } = setup({
        chess,
        moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
        isGameActive: true,
        isRated: false,
        isPracticeContinuation: true,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );

      await act(async () => {
        await result.current.handleGameEnd();
      });

      expect(useGameStore.getState().isGameActive).toBe(false);
      expect(onGameFinished).not.toHaveBeenCalled();
    });

    it("does not fire onGameFinished when a practice continuation is resigned", async () => {
      const chess = new Chess();
      chess.move("e4");
      const { result, onGameFinished } = setup({
        chess,
        isGameActive: true,
        isRated: false,
        isPracticeContinuation: true,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );

      act(() => {
        result.current.executeResign();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(onGameFinished).not.toHaveBeenCalled();
    });

    it("does not fire onGameFinished when an unconverted drill is abandoned", async () => {
      const { result, onGameFinished } = setup({
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      useGameStore.setState({
        sessionId: "drill-session-123",
        drillOpeningKey: "target-fen",
        drillState: "failed",
        drillStrictness: "standard",
      });
      abandonDrillMock.mockResolvedValueOnce({
        session_id: "drill-session-123",
        drill_state: "failed",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );

      act(() => {
        result.current.executeResign();
      });

      await waitFor(() =>
        expect(useGameStore.getState().isGameActive).toBe(false),
      );
      expect(onGameFinished).not.toHaveBeenCalled();
    });

    it("fires onGameFinished at most once per session on duplicate finalization", async () => {
      const chess = new Chess("7k/8/6QK/8/8/8/8/8 w - - 0 1");
      const move = chess.move({ from: "g6", to: "g7" });
      if (!move || !chess.isCheckmate()) {
        throw new Error("Unable to construct checkmate position");
      }
      const { result, onGameFinished } = setup({
        chess,
        moveHistory: [{ san: move.san, fen: chess.fen(), uci: "g6g7" }],
        isGameActive: true,
        isRated: false,
        playerColor: "white",
      });
      await waitFor(() =>
        expect(fetchCurrentRatingMock).toHaveBeenCalledTimes(1),
      );
      endGameResolves("checkmate_win");
      endGameResolves("checkmate_win");

      await act(async () => {
        await result.current.handleGameEnd();
        await result.current.handleGameEnd();
      });

      expect(onGameFinished).toHaveBeenCalledTimes(1);
    });
  });
});
