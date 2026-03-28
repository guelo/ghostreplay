import { useCallback, useEffect } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import { Chess } from "chess.js";
import type { OpeningLookupResult } from "../openings/openingBook";
import type { TargetBlunderSrs } from "../utils/api";
import { endGame, fetchCurrentRating, startGame, uploadSessionMoves } from "../utils/api";
import type {
  BlunderAlert,
  ReviewFailInfo,
} from "../components/chess-game/domain/movePresentation";
import { buildSessionMoveUploads } from "../components/chess-game/domain/sessionUpload";
import type { GameResult } from "../components/chess-game/domain/status";
import {
  ANALYSIS_UPLOAD_TIMEOUT_MS,
  STARTING_FEN,
} from "../components/chess-game/config";
import { sampleEloBin } from "../components/chess-game/elo";
import type { BoardOrientation, OpenHistoryOptions } from "../components/chess-game/types";
import { useGameStore } from "../stores/useGameStore";
import type { AnalysisStore } from "../stores/createAnalysisStore";

type PendingAnalysisContext = {
  fen: string;
  pgn: string;
  moveSan: string;
  moveUci: string;
  moveIndex: number;
};

type PendingSrsReview = {
  blunderId: number;
  moveIndex: number;
  userMoveSan: string;
};

type UseChessGameLifecycleArgs = {
  chess: Chess;
  analysisStore: AnalysisStore;
  uploadedAnalysisSessionsRef: MutableRefObject<Set<string>>;
  openingHistoryRef: MutableRefObject<(OpeningLookupResult | null)[]>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  clearMoveHighlights: () => void;
  resetMode: () => void;
  resetEngine: () => void;
  clearAnalysis: () => void;
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
  setShowPassToast: Dispatch<SetStateAction<boolean>>;
  setShowRehookToast: Dispatch<SetStateAction<boolean>>;
  setReviewFailModal: Dispatch<SetStateAction<ReviewFailInfo | null>>;
  setShowPostGamePrompt: Dispatch<SetStateAction<boolean>>;
  showRevertWarning: boolean;
  setShowRevertWarning: Dispatch<SetStateAction<boolean>>;
  setShowResignWarning: Dispatch<SetStateAction<boolean>>;
};

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

export const useChessGameLifecycle = ({
  chess,
  analysisStore,
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
  showRevertWarning,
  setShowRevertWarning,
  setShowResignWarning,
}: UseChessGameLifecycleArgs) => {
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

  const waitForQueuedAnalyses = useCallback(
    async (expectedMoves: number) => {
      const snap = () => analysisStore.getState();
      const analysisHasErrored = () => snap().status === "error";

      if (expectedMoves <= 0) {
        return;
      }

      if (
        analysisHasErrored() ||
        snap().analysisMap.size >= expectedMoves
      ) {
        return;
      }

      const initialSize = snap().analysisMap.size;
      await sleep(150);
      if (
        analysisHasErrored() ||
        snap().analysisMap.size >= expectedMoves
      ) {
        return;
      }

      if (
        !snap().isAnalyzing &&
        snap().analysisMap.size === initialSize
      ) {
        return;
      }

      const deadline = Date.now() + ANALYSIS_UPLOAD_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (
          analysisHasErrored() ||
          snap().analysisMap.size >= expectedMoves
        ) {
          return;
        }

        if (!snap().isAnalyzing) {
          const sizeBeforeIdleCheck = snap().analysisMap.size;
          await sleep(100);
          if (snap().analysisMap.size === sizeBeforeIdleCheck) {
            return;
          }
        } else {
          await sleep(50);
        }
      }
    },
    [analysisStore],
  );

  const uploadSessionAnalysisBatch = useCallback(
    async (targetSessionId: string, expectedMoveCount: number) => {
      if (uploadedAnalysisSessionsRef.current.has(targetSessionId)) {
        return;
      }

      await waitForQueuedAnalyses(expectedMoveCount);

      const historySnapshot = [...useGameStore.getState().moveHistory];
      if (historySnapshot.length === 0) {
        uploadedAnalysisSessionsRef.current.add(targetSessionId);
        return;
      }

      const analysesSnapshot = new Map(analysisStore.getState().analysisMap);
      const payload = buildSessionMoveUploads(
        historySnapshot,
        analysesSnapshot,
        STARTING_FEN,
      );
      await uploadSessionMoves(targetSessionId, payload);
      uploadedAnalysisSessionsRef.current.add(targetSessionId);
    },
    [
      analysisStore,
      uploadedAnalysisSessionsRef,
      waitForQueuedAnalyses,
    ],
  );

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
      try {
        try {
          await uploadSessionAnalysisBatch(
            store.sessionId,
            store.moveHistory.length,
          );
        } catch (uploadError) {
          console.error(
            "[SessionMoves] Failed to upload session moves:",
            uploadError,
          );
        }
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
        useGameStore.getState().setIsGameActive(false);
        useGameStore.getState().setGameResult(result);
        setShowPostGamePrompt(true);
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to end game.";
        setEngineMessage(message);
      }
    }
  }, [
    chess,
    setEngineMessage,
    setShowPostGamePrompt,
    uploadSessionAnalysisBatch,
  ]);

  const executeRevert = useCallback(() => {
    const store = useGameStore.getState();
    if (!store.isGameActive || store.moveHistory.length === 0 || chess.isGameOver()) return;

    store.setIsRated(false);
    setShowRevertWarning(false);
    setShowResignWarning(false);

    const isPlayerTurn = chess.turn() === (store.playerColor === "white" ? "w" : "b");
    const undoCount = isPlayerTurn && store.moveHistory.length >= 2 ? 2 : 1;

    for (let i = 0; i < undoCount; i++) {
      chess.undo();
    }

    const newHistory = store.moveHistory.slice(0, -undoCount);
    store.setMoveHistory(newHistory);
    store.setLiveFen(chess.fen());
    store.setViewIndex(null);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderAlert(null);
    pendingSrsReviewRef.current = null;
    pendingAnalysisContextRef.current = null;
  }, [
    chess,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setShowResignWarning,
    setShowRevertWarning,
  ]);

  const handleRevertClick = useCallback(() => {
    if (useGameStore.getState().isRated) {
      setShowRevertWarning(true);
    } else {
      executeRevert();
    }
  }, [executeRevert, setShowRevertWarning]);

  const cancelRevert = useCallback(() => {
    setShowRevertWarning(false);
  }, [setShowRevertWarning]);

  const handleNewGame = useCallback(
    async (colorOverride?: BoardOrientation | "random") => {
      try {
        setIsStartingGame(true);
        setStartError(null);

        const store = useGameStore.getState();
        if (store.sessionId && store.isGameActive) {
          try {
            await uploadSessionAnalysisBatch(
              store.sessionId,
              store.moveHistory.length,
            );
          } catch (uploadError) {
            console.error(
              "[SessionMoves] Failed to upload session moves:",
              uploadError,
            );
          }
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
        clearAnalysis();
        uploadedAnalysisSessionsRef.current.clear();
        setBlunderAlert(null);
        setShowFlash(false);
        setBlunderReviewId(null);
        setBlunderReviewSrs(null);
        setShowPassToast(false);
        setReviewFailModal(null);
        setShowPostGamePrompt(false);
        s2.setIsRated(true);
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
      clearAnalysis,
      clearMoveHighlights,
      openingHistoryRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      resetEngine,
      resetMode,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
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
      uploadSessionAnalysisBatch,
      uploadedAnalysisSessionsRef,
    ],
  );

  const handleResign = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) {
      return;
    }

    try {
      try {
        await uploadSessionAnalysisBatch(
          store.sessionId,
          store.moveHistory.length,
        );
      } catch (uploadError) {
        console.error(
          "[SessionMoves] Failed to upload session moves:",
          uploadError,
        );
      }
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
      const s = useGameStore.getState();
      s.setIsGameActive(false);
      s.setGameResult({ type: "resign", message: "You resigned." });
      setShowPostGamePrompt(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  }, [
    chess,
    setEngineMessage,
    setShowPostGamePrompt,
    uploadSessionAnalysisBatch,
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
    clearAnalysis();
    uploadedAnalysisSessionsRef.current.clear();
    setBlunderAlert(null);
    setShowFlash(false);
    setShowPassToast(false);
    setShowRehookToast(false);
    setReviewFailModal(null);
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    store.setIsRated(true);
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
    clearAnalysis,
    clearMoveHighlights,
    openingHistoryRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    resetEngine,
    resetMode,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setEngineMessage,
    setLiveOpening,
    setReviewFailModal,
    setShowFlash,
    setShowPassToast,
    setShowPostGamePrompt,
    setShowRehookToast,
    setShowResignWarning,
    setShowRevertWarning,
    setShowStartOverlay,
    uploadedAnalysisSessionsRef,
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
    onOpenHistory?.({ select: "latest", source: "post_game_view_analysis" });
  }, [onOpenHistory, setShowPostGamePrompt]);

  const handleViewHistory = useCallback(() => {
    setShowPostGamePrompt(false);
    onOpenHistory?.({ select: "latest", source: "post_game_history" });
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
    uploadSessionAnalysisBatch,
    showRevertWarning,
  };
};
