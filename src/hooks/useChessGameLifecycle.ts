import { useCallback, useEffect } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import { Chess } from "chess.js";
import type { AnalysisResult } from "./useMoveAnalysis";
import type { OpeningLookupResult } from "../openings/openingBook";
import type { TargetBlunderSrs, RatingChange } from "../utils/api";
import { endGame, fetchCurrentRating, startGame, uploadSessionMoves } from "../utils/api";
import type {
  BlunderAlert,
  MoveRecord,
  ReviewFailInfo,
} from "../components/chess-game/domain/movePresentation";
import { buildSessionMoveUploads } from "../components/chess-game/domain/sessionUpload";
import type { GameResult } from "../components/chess-game/domain/status";
import {
  ANALYSIS_UPLOAD_TIMEOUT_MS,
  MAIA_ELO_BINS,
  STARTING_FEN,
} from "../components/chess-game/config";
import { sampleEloBin } from "../components/chess-game/elo";
import type { BoardOrientation, OpenHistoryOptions } from "../components/chess-game/types";

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
  sessionId: string | null;
  isGameActive: boolean;
  isRated: boolean;
  playerColor: BoardOrientation;
  playerColorChoice: BoardOrientation | "random";
  engineElo: (typeof MAIA_ELO_BINS)[number];
  playerRating: number;
  moveHistory: MoveRecord[];
  moveCountRef: MutableRefObject<number>;
  moveHistoryRef: MutableRefObject<MoveRecord[]>;
  analysisMapRef: MutableRefObject<Map<number, AnalysisResult>>;
  analysisStatusRef: MutableRefObject<string>;
  isAnalyzingRef: MutableRefObject<boolean>;
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
  setPlayerRating: Dispatch<SetStateAction<number>>;
  setIsProvisional: Dispatch<SetStateAction<boolean>>;
  setEngineElo: Dispatch<SetStateAction<(typeof MAIA_ELO_BINS)[number]>>;
  setIsStartingGame: Dispatch<SetStateAction<boolean>>;
  setStartError: Dispatch<SetStateAction<string | null>>;
  setPlayerColor: Dispatch<SetStateAction<BoardOrientation>>;
  setPlayerColorChoice: Dispatch<
    SetStateAction<BoardOrientation | "random">
  >;
  setBoardOrientation: Dispatch<SetStateAction<BoardOrientation>>;
  setSessionId: Dispatch<SetStateAction<string | null>>;
  setIsGameActive: Dispatch<SetStateAction<boolean>>;
  setShowStartOverlay: Dispatch<SetStateAction<boolean>>;
  setFen: Dispatch<SetStateAction<string>>;
  setGameResult: Dispatch<SetStateAction<GameResult | null>>;
  setRatingChange: Dispatch<SetStateAction<RatingChange | null>>;
  setMoveHistory: Dispatch<SetStateAction<MoveRecord[]>>;
  setViewIndex: Dispatch<SetStateAction<number | null>>;
  setLiveOpening: Dispatch<SetStateAction<OpeningLookupResult | null>>;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setBlunderReviewId: Dispatch<SetStateAction<number | null>>;
  setBlunderReviewSrs: Dispatch<SetStateAction<TargetBlunderSrs | null>>;
  setShowPassToast: Dispatch<SetStateAction<boolean>>;
  setShowRehookToast: Dispatch<SetStateAction<boolean>>;
  setReviewFailModal: Dispatch<SetStateAction<ReviewFailInfo | null>>;
  setShowPostGamePrompt: Dispatch<SetStateAction<boolean>>;
  setIsRated: Dispatch<SetStateAction<boolean>>;
  showRevertWarning: boolean;
  setShowRevertWarning: Dispatch<SetStateAction<boolean>>;
};

const sleep = (ms: number) =>
  new Promise<void>((resolve) => {
    setTimeout(resolve, ms);
  });

export const useChessGameLifecycle = ({
  chess,
  sessionId,
  isGameActive,
  isRated,
  playerColor,
  playerColorChoice,
  engineElo,
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
  showRevertWarning,
  setShowRevertWarning,
}: UseChessGameLifecycleArgs) => {
  useEffect(() => {
    fetchCurrentRating()
      .then((data) => {
        setPlayerRating(data.current_rating);
        setIsProvisional(data.is_provisional);
        setEngineElo(sampleEloBin(data.current_rating));
      })
      .catch(() => {});
  }, [setEngineElo, setIsProvisional, setPlayerRating]);

  const waitForQueuedAnalyses = useCallback(
    async (expectedMoves: number) => {
      const analysisHasErrored = () => analysisStatusRef.current === "error";

      if (expectedMoves <= 0) {
        return;
      }

      if (
        analysisHasErrored() ||
        analysisMapRef.current.size >= expectedMoves
      ) {
        return;
      }

      const initialSize = analysisMapRef.current.size;
      await sleep(150);
      if (
        analysisHasErrored() ||
        analysisMapRef.current.size >= expectedMoves
      ) {
        return;
      }

      if (
        !isAnalyzingRef.current &&
        analysisMapRef.current.size === initialSize
      ) {
        return;
      }

      const deadline = Date.now() + ANALYSIS_UPLOAD_TIMEOUT_MS;
      while (Date.now() < deadline) {
        if (
          analysisHasErrored() ||
          analysisMapRef.current.size >= expectedMoves
        ) {
          return;
        }

        if (!isAnalyzingRef.current) {
          const sizeBeforeIdleCheck = analysisMapRef.current.size;
          await sleep(100);
          if (analysisMapRef.current.size === sizeBeforeIdleCheck) {
            return;
          }
        } else {
          await sleep(50);
        }
      }
    },
    [analysisMapRef, analysisStatusRef, isAnalyzingRef],
  );

  const uploadSessionAnalysisBatch = useCallback(
    async (targetSessionId: string, expectedMoveCount: number) => {
      if (uploadedAnalysisSessionsRef.current.has(targetSessionId)) {
        return;
      }

      await waitForQueuedAnalyses(expectedMoveCount);

      const historySnapshot = [...moveHistoryRef.current];
      if (historySnapshot.length === 0) {
        uploadedAnalysisSessionsRef.current.add(targetSessionId);
        return;
      }

      const analysesSnapshot = new Map(analysisMapRef.current);
      const payload = buildSessionMoveUploads(
        historySnapshot,
        analysesSnapshot,
        STARTING_FEN,
      );
      await uploadSessionMoves(targetSessionId, payload);
      uploadedAnalysisSessionsRef.current.add(targetSessionId);
    },
    [
      analysisMapRef,
      moveHistoryRef,
      uploadedAnalysisSessionsRef,
      waitForQueuedAnalyses,
    ],
  );

  const handleGameEnd = useCallback(async () => {
    if (!sessionId || !isGameActive) return;

    let result: GameResult | null = null;

    if (chess.isCheckmate()) {
      const loser = chess.turn() === "w" ? "white" : "black";
      const playerWon = playerColor !== loser;
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
          await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
        } catch (uploadError) {
          console.error(
            "[SessionMoves] Failed to upload session moves:",
            uploadError,
          );
        }
        const endResponse = await endGame(
          sessionId,
          result.type,
          chess.pgn(),
          isRated,
        );
        if (endResponse.rating) {
          setRatingChange(endResponse.rating);
          setPlayerRating(endResponse.rating.rating_after);
          setIsProvisional(endResponse.rating.is_provisional);
        }
        setIsGameActive(false);
        setGameResult(result);
        setShowPostGamePrompt(true);
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to end game.";
        setEngineMessage(message);
      }
    }
  }, [
    chess,
    isGameActive,
    isRated,
    moveCountRef,
    playerColor,
    sessionId,
    setEngineMessage,
    setGameResult,
    setIsGameActive,
    setIsProvisional,
    setPlayerRating,
    setRatingChange,
    setShowPostGamePrompt,
    uploadSessionAnalysisBatch,
  ]);

  const executeRevert = useCallback(() => {
    if (!isGameActive || moveHistory.length === 0 || chess.isGameOver()) return;

    setIsRated(false);
    setShowRevertWarning(false);

    const isPlayerTurn = chess.turn() === (playerColor === "white" ? "w" : "b");
    const undoCount = isPlayerTurn && moveHistory.length >= 2 ? 2 : 1;

    for (let i = 0; i < undoCount; i++) {
      chess.undo();
    }

    const newHistory = moveHistory.slice(0, -undoCount);
    moveHistoryRef.current = newHistory;
    moveCountRef.current = newHistory.length;
    setMoveHistory(newHistory);
    setFen(chess.fen());
    setViewIndex(null);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderAlert(null);
    pendingSrsReviewRef.current = null;
    pendingAnalysisContextRef.current = null;
  }, [
    chess,
    isGameActive,
    moveCountRef,
    moveHistory,
    moveHistoryRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    playerColor,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setFen,
    setIsRated,
    setMoveHistory,
    setShowRevertWarning,
    setViewIndex,
  ]);

  const handleRevertClick = useCallback(() => {
    if (isRated) {
      setShowRevertWarning(true);
    } else {
      executeRevert();
    }
  }, [executeRevert, isRated, setShowRevertWarning]);

  const cancelRevert = useCallback(() => {
    setShowRevertWarning(false);
  }, [setShowRevertWarning]);

  const handleNewGame = useCallback(
    async (colorOverride?: BoardOrientation | "random") => {
      try {
        setIsStartingGame(true);
        setStartError(null);
        if (sessionId && isGameActive) {
          try {
            await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
          } catch (uploadError) {
            console.error(
              "[SessionMoves] Failed to upload session moves:",
              uploadError,
            );
          }
          await endGame(sessionId, "abandon", chess.pgn(), isRated);
        }

        const effectiveChoice = colorOverride ?? playerColorChoice;
        const resolvedPlayerColor =
          effectiveChoice === "random"
            ? Math.random() < 0.5
              ? "white"
              : "black"
            : effectiveChoice;
        setPlayerColor(resolvedPlayerColor);
        setBoardOrientation(resolvedPlayerColor);

        const response = await startGame(engineElo, resolvedPlayerColor);
        setSessionId(response.session_id);
        setIsGameActive(true);
        setIsStartingGame(false);
        setShowStartOverlay(false);

        chess.reset();
        setFen(chess.fen());
        setEngineMessage(null);
        setGameResult(null);
        setRatingChange(null);
        setMoveHistory([]);
        moveCountRef.current = 0;
        setViewIndex(null);
        setLiveOpening(null);
        openingHistoryRef.current = [];
        resetEngine();
        clearAnalysis();
        moveHistoryRef.current = [];
        uploadedAnalysisSessionsRef.current.clear();
        setBlunderAlert(null);
        setShowFlash(false);
        setBlunderReviewId(null);
        setBlunderReviewSrs(null);
        setShowPassToast(false);
        setReviewFailModal(null);
        setShowPostGamePrompt(false);
        setIsRated(true);
        setShowRevertWarning(false);
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
      engineElo,
      isGameActive,
      isRated,
      moveCountRef,
      moveHistoryRef,
      openingHistoryRef,
      pendingAnalysisContextRef,
      pendingSrsReviewRef,
      playerColorChoice,
      resetEngine,
      resetMode,
      sessionId,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBoardOrientation,
      setEngineMessage,
      setFen,
      setGameResult,
      setIsGameActive,
      setIsRated,
      setIsStartingGame,
      setLiveOpening,
      setMoveHistory,
      setPlayerColor,
      setRatingChange,
      setReviewFailModal,
      setSessionId,
      setShowFlash,
      setShowPassToast,
      setShowPostGamePrompt,
      setShowRevertWarning,
      setShowStartOverlay,
      setStartError,
      setViewIndex,
      uploadSessionAnalysisBatch,
      uploadedAnalysisSessionsRef,
    ],
  );

  const handleResign = useCallback(async () => {
    if (!sessionId || !isGameActive) {
      return;
    }

    try {
      try {
        await uploadSessionAnalysisBatch(sessionId, moveCountRef.current);
      } catch (uploadError) {
        console.error(
          "[SessionMoves] Failed to upload session moves:",
          uploadError,
        );
      }
      const endResponse = await endGame(sessionId, "resign", chess.pgn(), isRated);
      if (endResponse.rating) {
        setRatingChange(endResponse.rating);
        setPlayerRating(endResponse.rating.rating_after);
        setIsProvisional(endResponse.rating.is_provisional);
      }
      setIsGameActive(false);
      setGameResult({ type: "resign", message: "You resigned." });
      setShowPostGamePrompt(true);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  }, [
    chess,
    isGameActive,
    isRated,
    moveCountRef,
    sessionId,
    setEngineMessage,
    setGameResult,
    setIsGameActive,
    setIsProvisional,
    setPlayerRating,
    setRatingChange,
    setShowPostGamePrompt,
    uploadSessionAnalysisBatch,
  ]);

  const handleReset = useCallback(() => {
    chess.reset();
    setFen(chess.fen());
    setBoardOrientation(playerColor);
    setEngineMessage(null);
    setSessionId(null);
    setIsGameActive(false);
    setGameResult(null);
    setMoveHistory([]);
    moveCountRef.current = 0;
    setViewIndex(null);
    setLiveOpening(null);
    openingHistoryRef.current = [];
    resetEngine();
    clearAnalysis();
    moveHistoryRef.current = [];
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
    setIsRated(true);
    setShowRevertWarning(false);
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
    moveCountRef,
    moveHistoryRef,
    openingHistoryRef,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    playerColor,
    resetEngine,
    resetMode,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBoardOrientation,
    setEngineMessage,
    setFen,
    setGameResult,
    setIsGameActive,
    setIsRated,
    setLiveOpening,
    setMoveHistory,
    setReviewFailModal,
    setSessionId,
    setShowFlash,
    setShowPassToast,
    setShowPostGamePrompt,
    setShowRehookToast,
    setShowRevertWarning,
    setShowStartOverlay,
    setViewIndex,
    uploadedAnalysisSessionsRef,
  ]);

  const handleShowStartOverlay = useCallback(() => {
    setPlayerColorChoice("random");
    setShowPostGamePrompt(false);
    setShowStartOverlay(true);
    setEngineElo(sampleEloBin(playerRating));
  }, [
    playerRating,
    setEngineElo,
    setPlayerColorChoice,
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
    handleResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
    uploadSessionAnalysisBatch,
    showRevertWarning,
  };
};
