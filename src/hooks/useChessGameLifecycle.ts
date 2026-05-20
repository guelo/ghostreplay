import { useCallback, useEffect, useRef } from "react";
import type {
  Dispatch,
  MutableRefObject,
  SetStateAction,
} from "react";
import { Chess } from "chess.js";
import type { OpeningLookupResult } from "../openings/openingBook";
import type {
  DrillSessionContract,
  DrillStrictness,
  TargetBlunderSrs,
} from "../utils/api";
import {
  abandonDrill,
  continueDrill,
  endGame,
  fetchCurrentRating,
  startDrill,
  startGame,
  uploadSessionMoves,
} from "../utils/api";
import type {
  BlunderAlert,
  MoveRecord,
  ReviewFailInfo,
} from "../components/chess-game/domain/movePresentation";
import type { GameResult } from "../components/chess-game/domain/status";
import { playEndGameAudio } from "../components/chess-game/endGameAudio";
import { sampleEloBin } from "../components/chess-game/elo";
import type { BoardOrientation, OpenHistoryOptions, ResolvedReview } from "../components/chess-game/types";
import { useGameStore } from "../stores/useGameStore";
import type { GameAnalysisCoordinator } from "../services/GameAnalysisCoordinator";
import { buildSessionMoveUploads } from "../components/chess-game/domain/sessionUpload";
import { STARTING_FEN } from "../components/chess-game/config";
import type { RatingScores } from "../utils/api";
import type { OpeningRootItem } from "../utils/api";

type PendingAnalysisContext = {
  fen: string;
  pgn: string;
  moveSan: string;
  moveUci: string;
  moveIndex: number;
};

type PendingSrsReview = {
  sessionId: string;
  analysisId: string;
  blunderId: number;
  moveIndex: number;
  userMoveSan: string;
  srs: TargetBlunderSrs | null;
};

const applyRatingScores = (scores: RatingScores | null | undefined) => {
  if (!scores) return;
  const s = useGameStore.getState();
  s.setRatingScores(scores);
  s.setPlayerRating(scores.elo.rating);
  s.setIsProvisional(scores.elo.is_provisional);
};

type UseChessGameLifecycleArgs = {
  chess: Chess;
  coordinator: GameAnalysisCoordinator;
  openingHistoryRef: MutableRefObject<(OpeningLookupResult | null)[]>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  pendingSrsReviewRef: MutableRefObject<Map<string, PendingSrsReview>>;
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
  const playedEndGameAudioSessionIdRef = useRef<string | null>(null);
  const isCurrentRevertExecution = useCallback(
    (executionId: number) => revertExecutionIdRef.current === executionId,
    [],
  );

  const getRewindHistoryLength = useCallback(
    (storeMoveHistory: MoveRecord[]) => {
      const store = useGameStore.getState();
      const isPlayerTurn =
        chess.turn() === (store.playerColor === "white" ? "w" : "b");
      const undoCount = isPlayerTurn && storeMoveHistory.length >= 2 ? 2 : 1;
      return Math.max(0, storeMoveHistory.length - undoCount);
    },
    [chess],
  );

  const prunePendingSrsReviewsFromMoveIndex = useCallback(
    (boundaryMoveIndex: number) => {
      for (const [analysisId, pendingReview] of pendingSrsReviewRef.current) {
        if (pendingReview.moveIndex >= boundaryMoveIndex) {
          pendingSrsReviewRef.current.delete(analysisId);
        }
      }
    },
    [pendingSrsReviewRef],
  );

  const finishLocalGame = useCallback(
    (
      result: GameResult,
      options?: {
        showPostGamePrompt?: boolean;
        preserveResolvedReviewMoveIndex?: number;
        playEndGameAudio?: boolean;
        finalizingSessionId?: string | null;
      },
    ) => {
      const store = useGameStore.getState();
      const finalizingSessionId =
        options?.finalizingSessionId ?? store.sessionId;
      if (
        finalizingSessionId &&
        store.sessionId !== finalizingSessionId
      ) {
        return;
      }

      store.setIsGameActive(false);
      store.setGameResult(result);
      if (
        (options?.playEndGameAudio ?? true) &&
        finalizingSessionId &&
        playedEndGameAudioSessionIdRef.current !== finalizingSessionId
      ) {
        playedEndGameAudioSessionIdRef.current = finalizingSessionId;
        playEndGameAudio(result);
      }
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
        applyRatingScores(
          data.scores ?? {
            elo: {
              rating: data.current_rating,
              is_provisional: data.is_provisional,
            },
            chesscom: null,
            lichess: null,
          },
        );
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
    const finalizingSessionId = store.sessionId;

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
          playEndGameAudio: false,
          finalizingSessionId,
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
        if (useGameStore.getState().sessionId !== finalizingSessionId) {
          return;
        }
        if (endResponse.rating) {
          const s = useGameStore.getState();
          s.setRatingChange(endResponse.rating);
          s.setScoreChanges(endResponse.score_changes ?? null);
          applyRatingScores(endResponse.scores_after ?? endResponse.scores);
        }
        finishLocalGame(result, {
          preserveResolvedReviewMoveIndex: store.moveHistory.length - 1,
          finalizingSessionId,
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
    const newHistoryLength = getRewindHistoryLength(storeMoveHistory);
    const undoCount = storeMoveHistory.length - newHistoryLength;

    for (let i = 0; i < undoCount; i++) {
      chess.undo();
    }

    const newHistory = storeMoveHistory.slice(0, newHistoryLength);
    store.setMoveHistory(newHistory);
    store.setLiveFen(chess.fen());
    store.setViewIndex(null);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderTargetFen(null);
    setResolvedReview(null);
    setBlunderAlert(null);
    setPendingPromotion(null);
    prunePendingSrsReviewsFromMoveIndex(newHistory.length);
    pendingAnalysisContextRef.current = null;
  }, [
    chess,
    getRewindHistoryLength,
    pendingAnalysisContextRef,
    prunePendingSrsReviewsFromMoveIndex,
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
    prunePendingSrsReviewsFromMoveIndex(
      getRewindHistoryLength(snapshotMoveHistory),
    );
    setResolvedReview(null);

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
          s.setScoreChanges(endResponse.score_changes ?? null);
          applyRatingScores(endResponse.scores_after ?? endResponse.scores);
        }
        const s = useGameStore.getState();
        s.setIsRated(false);
        s.setIsPracticeContinuation(true);
        s.setDrillState(null);
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
    getRewindHistoryLength,
    isCurrentRevertExecution,
    prunePendingSrsReviewsFromMoveIndex,
    rewindBoardLocally,
    setIsRevertPending,
    setRevertError,
    setResolvedReview,
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
        playedEndGameAudioSessionIdRef.current = null;

        const store = useGameStore.getState();
        if (
          store.sessionId &&
          store.isGameActive &&
          !store.isPracticeContinuation
        ) {
          pendingSrsReviewRef.current.clear();
          setResolvedReview(null);
          coordinator.flushPendingUploads().catch((err) =>
            console.error("[SessionMoves] Flush failed:", err),
          );
          if (store.drillOpeningKey) {
            await abandonDrill(store.sessionId);
          } else {
            await endGame(store.sessionId, "abandon", chess.pgn(), store.isRated);
          }
        }

        pendingSrsReviewRef.current.clear();
        setResolvedReview(null);
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
        s2.setScoreChanges(null);
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
        s2.setDrillOpeningKey(null);
        s2.setDrillState(null);
        s2.setDrillStrictness(null);
        setShowRevertWarning(false);
        setShowResignWarning(false);
        clearMoveHighlights();
        blunderRecordedRef.current = false;
        pendingAnalysisContextRef.current = null;
        pendingSrsReviewRef.current.clear();
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

  const handleNewDrill = useCallback(
    async (options: {
      openingKey: string;
      playerColor: "white" | "black";
      engineElo: number;
      strictness: DrillStrictness;
      strictnessCp: number;
      selectedOpening: OpeningRootItem;
    }) => {
      try {
        setIsStartingGame(true);
        setStartError(null);
        revertExecutionIdRef.current += 1;
        playedEndGameAudioSessionIdRef.current = null;

        const store = useGameStore.getState();
        if (store.sessionId && store.isGameActive && !store.isPracticeContinuation) {
          pendingSrsReviewRef.current.clear();
          setResolvedReview(null);
          coordinator.flushPendingUploads().catch((err) =>
            console.error("[SessionMoves] Flush failed:", err),
          );
          if (store.drillOpeningKey) {
            await abandonDrill(store.sessionId);
          } else {
            await endGame(store.sessionId, "abandon", chess.pgn(), store.isRated);
          }
        }

        pendingSrsReviewRef.current.clear();
        setResolvedReview(null);

        const response = await startDrill({
          opening_key: options.openingKey,
          player_color: options.playerColor,
          engine_elo: options.engineElo,
          strictness: options.strictness,
          strictness_cp: options.strictnessCp,
        });

        const tempChess = new Chess();
        const records: MoveRecord[] = [];

        const s = useGameStore.getState();
        s.setSessionId(response.session_id);
        s.setIsGameActive(true);
        s.setPlayerColor(options.playerColor);
        s.setBoardOrientation(options.playerColor);
        s.setEngineElo(options.engineElo);
        s.setIsRated(false);
        s.setIsPracticeContinuation(false);
        s.setDrillOpeningKey(options.openingKey);
        s.setDrillState(response.drill_state);
        s.setDrillStrictness(options.strictness);
        s.setLiveFen(tempChess.fen());
        s.setMoveHistory(records);

        chess.reset();

        s.setViewIndex(null);
        s.setGameResult(null);
        s.setRatingChange(null);
        s.setScoreChanges(null);

        resetEngine();
        coordinator.clearSession();
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
        setShowRehookToast(false);
        setRevertError(null);
        setIsRevertPending(false);
        setShowRevertWarning(false);
        setShowResignWarning(false);
        clearMoveHighlights();
        setLiveOpening(null);
        openingHistoryRef.current = [];
        blunderRecordedRef.current = false;
        pendingAnalysisContextRef.current = null;
        pendingSrsReviewRef.current.clear();
        resetMode();

        setIsStartingGame(false);
        setShowStartOverlay(false);
        setEngineMessage(null);

        try {
          const prefs = {
            openingKey: options.openingKey,
            engineElo: options.engineElo,
            strictnessCp: options.strictness === "strict" ? 0 : options.strictness === "standard" ? 25 : 50,
            playerColor: options.playerColor,
          };
          localStorage.setItem("ghostreplay_drill_prefs", JSON.stringify(prefs));
        } catch {
          // ignore storage errors
        }

        if (tempChess.turn() !== (options.playerColor === "white" ? "w" : "b")) {
          // opponent's turn — trigger opponent move via callback in ChessGame
          // We return the needed state so ChessGame can apply opponent move
        }

        return { fen: tempChess.fen(), uciHistory: records.map((r) => r.uci) };
      } catch (error) {
        const message =
          error instanceof Error ? error.message : "Failed to start drill.";
        setEngineMessage(message);
        setStartError(message);
        setIsStartingGame(false);
        return null;
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
      setShowRehookToast,
      setShowResignWarning,
      setShowRevertWarning,
      setShowStartOverlay,
      setStartError,
      setIsRevertPending,
      setRevertError,
    ],
  );

  const handleResign = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) {
      return;
    }
    const finalizingSessionId = store.sessionId;

    if (store.isPracticeContinuation) {
      finishLocalGame(
        { type: "resign", message: "Practice ended." },
        { playEndGameAudio: false, finalizingSessionId },
      );
      return;
    }

    try {
      coordinator.flushPendingUploads().catch((err) =>
        console.error("[SessionMoves] Flush failed:", err),
      );

      if (store.drillOpeningKey && store.drillState !== "converted") {
        const contract = await abandonDrill(store.sessionId);
        if (useGameStore.getState().sessionId !== finalizingSessionId) {
          return;
        }
        const s = useGameStore.getState();
        s.setDrillState(contract.drill_state);
        s.setIsRated(false);
        finishLocalGame(
          { type: "resign", message: "Drill abandoned." },
          { playEndGameAudio: false, finalizingSessionId },
        );
        return;
      }

      const endResponse = await endGame(
        store.sessionId,
        "resign",
        chess.pgn(),
        store.isRated,
      );
      if (useGameStore.getState().sessionId !== finalizingSessionId) {
        return;
      }
      if (endResponse.rating) {
        const s = useGameStore.getState();
        s.setRatingChange(endResponse.rating);
        s.setScoreChanges(endResponse.score_changes ?? null);
        applyRatingScores(endResponse.scores_after ?? endResponse.scores);
      }
      finishLocalGame(
        { type: "resign", message: "You resigned." },
        { finalizingSessionId },
      );
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to resign game.";
      setEngineMessage(message);
    }
  }, [
    chess,
    coordinator,
    finishLocalGame,
    setEngineMessage,
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
    playedEndGameAudioSessionIdRef.current = null;
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
    store.setDrillOpeningKey(null);
    store.setDrillState(null);
    store.setDrillStrictness(null);
    setShowRevertWarning(false);
    setShowResignWarning(false);
    clearMoveHighlights();
    blunderRecordedRef.current = false;
    pendingAnalysisContextRef.current = null;
    pendingSrsReviewRef.current.clear();
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

  const handleContinueDrill = useCallback(async (): Promise<
    DrillSessionContract | undefined
  > => {
    const store = useGameStore.getState();
    if (!store.sessionId || store.drillState !== "root_reached") {
      return;
    }
    try {
      setEngineMessage(null);
      await coordinator.flushPendingUploads();
      const contract = await continueDrill(store.sessionId, store.moveHistory.length);
      const next = useGameStore.getState();
      next.setDrillState(contract.drill_state);
      next.setIsRated(contract.is_rated);
      next.setIsPracticeContinuation(false);
      setShowPostGamePrompt(false);
      return contract;
    } catch (error) {
      const message =
        error instanceof Error ? error.message : "Failed to continue drill.";
      setEngineMessage(message);
      setStartError(message);
    }
  }, [coordinator, setEngineMessage, setShowPostGamePrompt, setStartError]);

  return {
    handleGameEnd,
    executeRevert,
    handleRevertClick,
    cancelRevert,
    handleNewGame,
    handleNewDrill,
    handleResignClick,
    executeResign,
    cancelResign,
    handleReset,
    handleShowStartOverlay,
    handleViewAnalysis,
    handleViewHistory,
    handleContinueDrill,
    showRevertWarning,
  };
};
