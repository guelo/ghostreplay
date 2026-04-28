import { useCallback, useEffect, useRef } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import { Chess } from "chess.js";
import type { OpeningLookupResult } from "../openings/openingBook";
import type { TargetBlunderSrs } from "../utils/api";
import {
  endGame,
  fetchCurrentRating,
  startGame,
  uploadSessionMoves,
} from "../utils/api";
import type {
  BlunderAlert,
  MoveRecord,
  ReviewFailInfo,
} from "../components/chess-game/domain/movePresentation";
import type { GameResult } from "../components/chess-game/domain/status";
import { sampleEloBin } from "../components/chess-game/elo";
import type { BoardOrientation, OpenHistoryOptions, ResolvedReview } from "../components/chess-game/types";
import { useGameStore } from "../stores/useGameStore";
import type { GameAnalysisCoordinator } from "../services/GameAnalysisCoordinator";
import { buildSessionMoveUploads } from "../components/chess-game/domain/sessionUpload";
import { STARTING_FEN } from "../components/chess-game/config";

type PendingAnalysisContext = {
  fen: string;
  pgn: string;
  moveSan: string;
  moveUci: string;
  moveIndex: number;
};

type PendingSrsReview = {
  analysisId: string;
  blunderId: number;
  moveIndex: number;
  userMoveSan: string;
  srs: TargetBlunderSrs | null;
};

type UseChessGameLifecycleArgs = {
  chess: Chess;
  coordinator: GameAnalysisCoordinator;
  openingHistoryRef: MutableRefObject<(OpeningLookupResult | null)[]>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  clearMoveHighlights: () => void;
  resetMode: () => void;
  resetEngine: () => void;
  onOpenHistory?: (options: OpenHistoryOptions) => void;
  setEngineMessage: Dispatch<SetStateAction<string | null>>;
  setIsStartingGame: Dispatch<SetStateAction<boolean>>;
  setStartError: Dispatch<SetStateAction<string | null>>;
  setShowStartOverlay: Dispatch<SetStateAction<boolean>>;
  setLiveOpening: Dispatch<SetStateAction<OpeningLookupResult | null>>;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setBlunderReviewId: Dispatch<SetStateAction<number | null>>;
  setBlunderReviewSrs: Dispatch<SetStateAction<TargetBlunderSrs | null>>;
  setBlunderTargetFen: Dispatch<SetStateAction<string | null>>;
  setShowPassToast: Dispatch<SetStateAction<boolean>>;
  setShowRehookToast: Dispatch<SetStateAction<boolean>>;
  setReviewFailModal: Dispatch<SetStateAction<ReviewFailInfo | null>>;
  setShowPostGamePrompt: Dispatch<SetStateAction<boolean>>;
  setIsRevertPending: Dispatch<SetStateAction<boolean>>;
  setRevertError: Dispatch<SetStateAction<string | null>>;
  showRevertWarning: boolean;
  setShowRevertWarning: Dispatch<SetStateAction<boolean>>;
  setShowResignWarning: Dispatch<SetStateAction<boolean>>;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
  setPendingPromotion: Dispatch<SetStateAction<{ from: string; to: string } | null>>;
  clearBlunderBoardOverride?: () => void;
};

export const useChessGameLifecycle = ({
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
  showRevertWarning,
  setShowRevertWarning,
  setShowResignWarning,
  setResolvedReview,
  setPendingPromotion,
  clearBlunderBoardOverride,
}: UseChessGameLifecycleArgs) => {
  const revertExecutionIdRef = useRef(0);
  const isCurrentRevertExecution = useCallback(
    (executionId: number) => revertExecutionIdRef.current === executionId,
    [],
  );

  const finishLocalGame = useCallback(
    (
      result: GameResult,
      options?: {
        showPostGamePrompt?: boolean;
        preserveResolvedReviewMoveIndex?: number;
      },
    ) => {
      const store = useGameStore.getState();
      store.setIsGameActive(false);
      store.setGameResult(result);
      setBlunderReviewId(null);
      setBlunderReviewSrs(null);
      setBlunderTargetFen(null);
      setResolvedReview((prev) =>
        prev?.moveIndex === options?.preserveResolvedReviewMoveIndex
          ? prev
          : null,
      );
      setPendingPromotion(null);
      setShowPostGamePrompt(options?.showPostGamePrompt ?? true);
    },
    [
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setPendingPromotion,
      setShowPostGamePrompt,
    ],
  );

  useEffect(() => {
    fetchCurrentRating()
      .then((data) => {
        const s = useGameStore.getState();
        s.setPlayerRating(data.current_rating);
        s.setIsProvisional(data.is_provisional);
        // Only resample engine ELO if no active game — otherwise the
        // displayed Maia name and stake would diverge from the backend session.
        if (!s.isGameActive) {
          s.setEngineElo(sampleEloBin(data.current_rating));
        }
      })
      .catch(() => {});
  }, []);

  const handleGameEnd = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) return;

    let result: GameResult | null = null;

    if (chess.isCheckmate()) {
      const loser = chess.turn() === "w" ? "white" : "black";
      const playerWon = store.playerColor !== loser;
      result = playerWon
        ? { type: "checkmate_win", message: "Checkmate! You won!" }
        : { type: "checkmate_loss", message: "Checkmate! You lost." };
    } else if (chess.isStalemate()) {
      result = { type: "draw", message: "Stalemate! The game is a draw." };
    } else if (chess.isThreefoldRepetition()) {
      result = { type: "draw", message: "Draw by threefold repetition." };
    } else if (chess.isInsufficientMaterial()) {
      result = { type: "draw", message: "Draw by insufficient material." };
    } else if (chess.isDraw()) {
      result = { type: "draw", message: "The game is a draw." };
    }

    if (result) {
      if (store.isPracticeContinuation) {
        finishLocalGame(result, {
          preserveResolvedReviewMoveIndex: store.moveHistory.length - 1,
        });
        return;
      }

      try {
        // Best-effort flush of already-resolved analyses — does not block
        coordinator.flushPendingUploads().catch((err) =>
          console.error("[SessionMoves] Flush failed:", err),
        );

        const endResponse = await endGame(
          store.sessionId,
          result.type,
          chess.pgn(),
          store.isRated,
        );
        if (endResponse.rating) {
          const s = useGameStore.getState();
          s.setRatingChange(endResponse.rating);
          s.setPlayerRating(endResponse.rating.rating_after);
          s.setIsProvisional(endResponse.rating.is_provisional);
        }
        finishLocalGame(result, {
          preserveResolvedReviewMoveIndex: store.moveHistory.length - 1,
        });
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to end game.";
        setEngineMessage(message);
      }
    }
  }, [
    chess,
    coordinator,
    finishLocalGame,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setEngineMessage,
    setResolvedReview,
    setShowPostGamePrompt,
  ]);

  const rewindBoardLocally = useCallback((storeMoveHistory: MoveRecord[]) => {
    const store = useGameStore.getState();
    const isPlayerTurn = chess.turn() === (store.playerColor === "white" ? "w" : "b");
    const undoCount = isPlayerTurn && storeMoveHistory.length >= 2 ? 2 : 1;

    for (let i = 0; i < undoCount; i++) {
      chess.undo();
    }

    const newHistory = storeMoveHistory.slice(0, -undoCount);
    store.setMoveHistory(newHistory);
    store.setLiveFen(chess.fen());
    store.setViewIndex(null);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderTargetFen(null);
    setResolvedReview(null);
    setBlunderAlert(null);
    setPendingPromotion(null);
    pendingSrsReviewRef.current = null;
    pendingAnalysisContextRef.current = null;
  }, [
    chess,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setResolvedReview,
    setPendingPromotion,
    setShowRevertWarning,
    clearBlunderBoardOverride,
  ]);

  const executeRevert = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.isGameActive || store.moveHistory.length === 0 || chess.isGameOver()) return;

    const executionId = revertExecutionIdRef.current + 1;
    revertExecutionIdRef.current = executionId;
    setShowResignWarning(false);
    setRevertError(null);
    setIsRevertPending(true);
    clearBlunderBoardOverride?.();

    const snapshotMoveHistory = [...store.moveHistory];

    try {
      if (!store.isPracticeContinuation && store.isRated) {
        const snapshotPgn = chess.pgn();
        const snapshotUploads = buildSessionMoveUploads(
          snapshotMoveHistory,
          new Map(coordinator.store.getState().analysisMap),
          STARTING_FEN,
        );

        await uploadSessionMoves(store.sessionId!, snapshotUploads);
        if (!isCurrentRevertExecution(executionId)) {
          return;
        }
        const endResponse = await endGame(
          store.sessionId!,
          "resign",
          snapshotPgn,
          true,
        );
        if (!isCurrentRevertExecution(executionId)) {
          return;
        }
        if (endResponse.rating) {
          const s = useGameStore.getState();
          s.setRatingChange(endResponse.rating);
          s.setPlayerRating(endResponse.rating.rating_after);
          s.setIsProvisional(endResponse.rating.is_provisional);
        }
        const s = useGameStore.getState();
        s.setIsRated(false);
        s.setIsPracticeContinuation(true);
        coordinator.stopSessionUploads();
      }

      if (!isCurrentRevertExecution(executionId)) {
        return;
      }

      rewindBoardLocally(snapshotMoveHistory);
      setShowRevertWarning(false);
    } catch (error) {
      if (!isCurrentRevertExecution(executionId)) {
        return;
      }
      const message =
        error instanceof Error ? error.message : "Failed to record resignation before revert.";
      setRevertError(message);
    } finally {
      if (isCurrentRevertExecution(executionId)) {
        setIsRevertPending(false);
      }
    }
  }, [
    chess,
    clearBlunderBoardOverride,
    coordinator,
    isCurrentRevertExecution,
    rewindBoardLocally,
    setIsRevertPending,
    setRevertError,
    setShowResignWarning,
    setShowRevertWarning,
  ]);

  const handleRevertClick = useCallback(() => {
    setRevertError(null);
    if (useGameStore.getState().isRated) {
      setShowRevertWarning(true);
    } else {
      void executeRevert();
    }
  }, [executeRevert, setRevertError, setShowRevertWarning]);

  const cancelRevert = useCallback(() => {
    if (useGameStore.getState().isGameActive) {
      setRevertError(null);
    }
    setShowRevertWarning(false);
  }, [setRevertError, setShowRevertWarning]);

  const handleNewGame = useCallback(
    async (colorOverride?: BoardOrientation | "random") => {
      try {
        setIsStartingGame(true);
        setStartError(null);
        revertExecutionIdRef.current += 1;

        const store = useGameStore.getState();
        if (
          store.sessionId &&
          store.isGameActive &&
          !store.isPracticeContinuation
        ) {
          coordinator.flushPendingUploads().catch((err) =>
            console.error("[SessionMoves] Flush failed:", err),
          );
          await endGame(store.sessionId, "abandon", chess.pgn(), store.isRated);
        }

        const effectiveChoice = colorOverride ?? store.playerColorChoice;
        const resolvedPlayerColor =
          effectiveChoice === "random"
            ? Math.random() < 0.5
              ? "white"
              : "black"
            : effectiveChoice;

        const s = useGameStore.getState();
        s.setPlayerColor(resolvedPlayerColor);
        s.setBoardOrientation(resolvedPlayerColor);

        const response = await startGame(store.engineElo, resolvedPlayerColor);
        const s2 = useGameStore.getState();
        s2.setSessionId(response.session_id);
        s2.setIsGameActive(true);
        setIsStartingGame(false);
        setShowStartOverlay(false);

        chess.reset();
        s2.setLiveFen(chess.fen());
        setEngineMessage(null);
        s2.setGameResult(null);
        s2.setRatingChange(null);
        s2.setMoveHistory([]);
        s2.setViewIndex(null);
        setLiveOpening(null);
        openingHistoryRef.current = [];
        resetEngine();
        coordinator.startSession(response.session_id);
        clearBlunderBoardOverride?.();
        setBlunderAlert(null);
        setShowFlash(false);
        setBlunderReviewId(null);
        setBlunderReviewSrs(null);
        setBlunderTargetFen(null);
        setResolvedReview(null);
        setPendingPromotion(null);
        setShowPassToast(false);
        setReviewFailModal(null);
        setShowPostGamePrompt(false);
        setRevertError(null);
        setIsRevertPending(false);
        s2.setIsRated(true);
        s2.setIsPracticeContinuation(false);
        setShowRevertWarning(false);
        setShowResignWarning(false);
        clearMoveHighlights();
        blunderRecordedRef.current = false;
        pendingAnalysisContextRef.current = null;
        pendingSrsReviewRef.current = null;
        resetMode();
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to start new game.";
        setEngineMessage(message);
        setStartError(message);
        setIsStartingGame(false);
      }
    },
    [
      blunderRecordedRef,
      chess,
      coordinator,
      clearMoveHighlights,
      openingHistoryRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      resetEngine,
      resetMode,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setPendingPromotion,
      clearBlunderBoardOverride,
      setEngineMessage,
      setIsStartingGame,
      setLiveOpening,
      setReviewFailModal,
      setShowFlash,
      setShowPassToast,
      setShowPostGamePrompt,
      setShowResignWarning,
      setShowRevertWarning,
      setShowStartOverlay,
      setStartError,
    ],
  );

  const handleResign = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) {
      return;
    }

    if (store.isPracticeContinuation) {
      finishLocalGame({ type: "resign", message: "Practice ended." });
      return;
    }

    try {
      coordinator.flushPendingUploads().catch((err) =>
        console.error("[SessionMoves] Flush failed:", err),
      );

      const endResponse = await endGame(
        store.sessionId,
        "resign",
        chess.pgn(),
        store.isRated,
      );
      if (endResponse.rating) {
        const s = useGameStore.getState();
        s.setRatingChange(endResponse.rating);
        s.setPlayerRating(endResponse.rating.rating_after);
        s.setIsProvisional(endResponse.rating.is_provisional);
      }
      finishLocalGame({ type: "resign", message: "You resigned." });
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  }, [
    chess,
    coordinator,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setEngineMessage,
    setResolvedReview,
    setPendingPromotion,
    setShowPostGamePrompt,
  ]);

  const executeResign = useCallback(() => {
    setShowResignWarning(false);
    handleResign();
  }, [handleResign, setShowResignWarning]);

  const handleResignClick = useCallback(() => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) return;
    setShowResignWarning(true);
  }, [setShowResignWarning]);

  const cancelResign = useCallback(() => {
    setShowResignWarning(false);
  }, [setShowResignWarning]);

  const handleReset = useCallback(() => {
    const store = useGameStore.getState();
    revertExecutionIdRef.current += 1;
    chess.reset();
    store.setLiveFen(chess.fen());
    store.setBoardOrientation(store.playerColor);
    setEngineMessage(null);
    store.setSessionId(null);
    store.setIsGameActive(false);
    store.setGameResult(null);
    store.setMoveHistory([]);
    store.setViewIndex(null);
    setLiveOpening(null);
    openingHistoryRef.current = [];
    resetEngine();
    coordinator.clearSession();
    clearBlunderBoardOverride?.();
    setBlunderAlert(null);
    setShowFlash(false);
    setShowPassToast(false);
    setShowRehookToast(false);
    setReviewFailModal(null);
    setShowPostGamePrompt(false);
    setRevertError(null);
    setIsRevertPending(false);
    setShowStartOverlay(true);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderTargetFen(null);
    setResolvedReview(null);
    setPendingPromotion(null);
    store.setIsRated(true);
    store.setIsPracticeContinuation(false);
    setShowRevertWarning(false);
    setShowResignWarning(false);
    clearMoveHighlights();
    blunderRecordedRef.current = false;
    pendingAnalysisContextRef.current = null;
    pendingSrsReviewRef.current = null;
    resetMode();
  }, [
    blunderRecordedRef,
    chess,
    coordinator,
    clearMoveHighlights,
    openingHistoryRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    resetEngine,
    resetMode,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setResolvedReview,
    setPendingPromotion,
    clearBlunderBoardOverride,
    setEngineMessage,
    setLiveOpening,
    setReviewFailModal,
    setShowFlash,
      setShowPassToast,
      setShowPostGamePrompt,
      setShowRehookToast,
      setIsRevertPending,
      setRevertError,
      setShowResignWarning,
      setShowRevertWarning,
    setShowStartOverlay,
  ]);

  const handleShowStartOverlay = useCallback(() => {
    const store = useGameStore.getState();
    store.setPlayerColorChoice("random");
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
    store.setEngineElo(sampleEloBin(store.playerRating));
  }, [
    setShowPostGamePrompt,
    setShowStartOverlay,
  ]);

  const handleViewAnalysis = useCallback(() => {
    setShowPostGamePrompt(false);
    const sid = useGameStore.getState().sessionId ?? undefined;
    onOpenHistory?.({ select: "latest", source: "post_game_view_analysis", sessionId: sid });
  }, [onOpenHistory, setShowPostGamePrompt]);

  const handleViewHistory = useCallback(() => {
    setShowPostGamePrompt(false);
    const sid = useGameStore.getState().sessionId ?? undefined;
    onOpenHistory?.({ select: "latest", source: "post_game_history", sessionId: sid });
  }, [onOpenHistory, setShowPostGamePrompt]);

  return {
    handleGameEnd,
    executeRevert,
    handleRevertClick,
    cancelRevert,
    handleNewGame,
    handleResignClick,
    executeResign,
    cancelResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
    showRevertWarning,
  };
};
