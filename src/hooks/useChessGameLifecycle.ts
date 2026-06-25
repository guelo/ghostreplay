import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { Chess } from "chess.js";
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
  naturalEndDrill,
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
  clearMoveHighlights: () => void;
  resetMode: () => void;
  resetEngine: () => void;
  onOpenHistory?: (options: OpenHistoryOptions) => void;
  setEngineMessage: Dispatch<SetStateAction<string | null>>;
  setIsStartingGame: Dispatch<SetStateAction<boolean>>;
  setStartError: Dispatch<SetStateAction<string | null>>;
  setShowStartOverlay: Dispatch<SetStateAction<boolean>>;
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
  /**
   * Clear the route-local reviewed-drill-return presentation (g-65ve). Called
   * on successful new-game/new-drill starts and on reset so the retained
   * "Again" banner never lingers once a fresh session is live.
   */
  clearReviewedDrillReturn?: () => void;
};

export const useChessGameLifecycle = ({
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
  clearReviewedDrillReturn,
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
        // Only resample engine ELO when truly idle (no active game AND no drill
        // context loaded). A reviewed-drill return mounts inactive but with the
        // abandoned drill still in the store; resampling here would clobber the
        // retained engine ELO before "Again" replays it (g-65ve).
        if (!s.isGameActive && s.drillState === null) {
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

      if (
        store.drillOpeningKey &&
        (store.drillState === "active" || store.drillState === "root_reached") &&
        (result.type === "checkmate_win" ||
          result.type === "checkmate_loss" ||
          result.type === "draw")
      ) {
        try {
          const contract = await naturalEndDrill(
            store.sessionId,
            result.type,
            chess.pgn(),
          );
          if (useGameStore.getState().sessionId !== finalizingSessionId) {
            return;
          }
          const s = useGameStore.getState();
          s.setDrillState(contract.drill_state);
          s.setDrillTerminalReason(contract.terminal_reason ?? null);
          s.setIsRated(false);
          // Natural-ended drills remain hidden and unrated unless converted.
          // Persisted evidence is best-effort, so discard any resolved upload
          // tail that had not already reached the server.
          coordinator.stopSessionUploads();
          finishLocalGame(result, {
            preserveResolvedReviewMoveIndex: store.moveHistory.length - 1,
            playEndGameAudio: true,
            finalizingSessionId,
          });
        } catch (error) {
          const message =
            error instanceof Error ? error.message : "Failed to end drill.";
          setEngineMessage(message);
        }
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
    // Synchronously prune coordinator-owned resolution/lineage state for the
    // reverted indices (M1), in the same turn as the UI reset. This drives the
    // DecisionOwner's partial reset (frontier/context/SRS prune) via emitReset.
    coordinator.pruneFromMoveIndex(newHistory.length);
  }, [
    chess,
    coordinator,
    getRewindHistoryLength,
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
    // Cancel pending SRS reviews for the reverted indices BEFORE the awaited
    // upload/endGame, so an analysis resolving during that async window cannot
    // POST a review the revert is cancelling (durable resolved slots survive).
    coordinator.decisionOwner.cancelPendingSrsReviews(
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
        s.setDrillStrictnessCp(null);
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
          // Cancel pending SRS reviews BEFORE the awaited abandon/endGame so an
          // analysis resolving during that window cannot POST a review for the
          // game being abandoned (durable resolved slots survive).
          coordinator.decisionOwner.cancelPendingSrsReviews();
          setResolvedReview(null);
          if (store.drillOpeningKey && store.drillState !== "converted") {
            await abandonDrill(store.sessionId);
            coordinator.stopSessionUploads();
          } else {
            coordinator.flushPendingUploads().catch((err) =>
              console.error("[SessionMoves] Flush failed:", err),
            );
            await endGame(store.sessionId, "abandon", chess.pgn(), store.isRated);
          }
        }

        coordinator.decisionOwner.cancelPendingSrsReviews();
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
        clearReviewedDrillReturn?.();

        chess.reset();
        s2.setLiveFen(chess.fen());
        setEngineMessage(null);
        s2.setGameResult(null);
        s2.setRatingChange(null);
        s2.setScoreChanges(null);
        s2.setMoveHistory([]);
        s2.setViewIndex(null);
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
        s2.setDrillLine(null);
        s2.setDrillOpeningName(null);
        s2.setDrillState(null);
        s2.setDrillStrictness(null);
        s2.setDrillStrictnessCp(null);
        setShowRevertWarning(false);
        setShowResignWarning(false);
        clearMoveHighlights();
        // DecisionOwner state (contextMap/pendingSrsMap/blunderReserved/frontier)
        // is cleared by its fullReset, driven by coordinator.startSession →
        // emitReset above. No React-ref decision state remains to clear here.
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
      chess,
      coordinator,
      clearMoveHighlights,
      resetEngine,
      resetMode,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setPendingPromotion,
      clearBlunderBoardOverride,
      clearReviewedDrillReturn,
      setEngineMessage,
      setIsStartingGame,
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
      // Ad-hoc card drills: the full UCI line to the target FEN. Omitted/undefined
      // for registered-root drills (routed via the book BFS).
      line?: string[];
    }) => {
      try {
        setIsStartingGame(true);
        setStartError(null);
        revertExecutionIdRef.current += 1;
        playedEndGameAudioSessionIdRef.current = null;

        const store = useGameStore.getState();
        if (store.sessionId && store.isGameActive && !store.isPracticeContinuation) {
          // Cancel pending SRS reviews BEFORE the awaited abandon/endGame.
          coordinator.decisionOwner.cancelPendingSrsReviews();
          setResolvedReview(null);
          if (store.drillOpeningKey && store.drillState !== "converted") {
            await abandonDrill(store.sessionId);
            coordinator.stopSessionUploads();
          } else {
            coordinator.flushPendingUploads().catch((err) =>
              console.error("[SessionMoves] Flush failed:", err),
            );
            await endGame(store.sessionId, "abandon", chess.pgn(), store.isRated);
          }
        }

        coordinator.decisionOwner.cancelPendingSrsReviews();
        setResolvedReview(null);

        const response = await startDrill({
          opening_key: options.openingKey,
          player_color: options.playerColor,
          engine_elo: options.engineElo,
          strictness: options.strictness,
          strictness_cp: options.strictnessCp,
          line: options.line,
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
        // Durable copy of the ad-hoc line (null for registered roots) so the
        // reviewed-return "Again" survives the /drill-analysis remount.
        s.setDrillLine(options.line ?? null);
        s.setDrillOpeningName(response.opening_name);
        s.setDrillState(response.drill_state);
        s.setDrillStrictness(options.strictness);
        s.setDrillStrictnessCp(response.strictness_cp ?? options.strictnessCp);
        s.setDrillTerminalReason(null);
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
        // DecisionOwner decision state is cleared by its fullReset, driven by
        // coordinator.clearSession/startSession → emitReset above.
        resetMode();

        setIsStartingGame(false);
        setShowStartOverlay(false);
        clearReviewedDrillReturn?.();
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
      chess,
      coordinator,
      clearMoveHighlights,
      resetEngine,
      resetMode,
      setBlunderAlert,
      setBlunderReviewId,
      setBlunderReviewSrs,
      setBlunderTargetFen,
      setResolvedReview,
      setPendingPromotion,
      clearBlunderBoardOverride,
      clearReviewedDrillReturn,
      setEngineMessage,
      setIsStartingGame,
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

  /**
   * Finalize a stopped (failed) drill without rating, conversion, or history
   * (g-a406). Used by the "Analyze" drill-end action: the drill is abandoned,
   * marked unrated, and the local game is ended so /play shows no stale active
   * drill. finishLocalGame is closure-private, so this is the only way ChessGame
   * can finalize a stopped drill.
   */
  const abandonStoppedDrill = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) {
      return;
    }
    const finalizingSessionId = store.sessionId;

    // Let abandonDrill rejections propagate to the caller — finalizing locally
    // on a backend failure would leave an active failed drill with no cleanup
    // opportunity. The caller keeps the drill active and surfaces an error.
    if (store.drillOpeningKey && store.drillState !== "converted") {
      const contract = await abandonDrill(store.sessionId);
      if (useGameStore.getState().sessionId !== finalizingSessionId) {
        return;
      }
      coordinator.stopSessionUploads();
      const s = useGameStore.getState();
      s.setDrillState(contract.drill_state);
      s.setIsRated(false);
    }

    if (useGameStore.getState().sessionId !== finalizingSessionId) {
      return;
    }
    finishLocalGame(
      { type: "resign", message: "Drill abandoned." },
      { playEndGameAudio: false, finalizingSessionId },
    );
  }, [finishLocalGame]);

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
      if (store.drillOpeningKey && store.drillState !== "converted") {
        const contract = await abandonDrill(store.sessionId);
        if (useGameStore.getState().sessionId !== finalizingSessionId) {
          return;
        }
        coordinator.stopSessionUploads();
        const s = useGameStore.getState();
        s.setDrillState(contract.drill_state);
        s.setIsRated(false);
        finishLocalGame(
          { type: "resign", message: "Drill abandoned." },
          { playEndGameAudio: false, finalizingSessionId },
        );
        return;
      }

      coordinator.flushPendingUploads().catch((err) =>
        console.error("[SessionMoves] Flush failed:", err),
      );

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
    resetEngine();
    if (store.drillOpeningKey && store.drillState !== "converted") {
      coordinator.stopSessionUploads();
    }
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
    clearReviewedDrillReturn?.();
    setShowStartOverlay(true);
    setBlunderReviewId(null);
    setBlunderReviewSrs(null);
    setBlunderTargetFen(null);
    setResolvedReview(null);
    setPendingPromotion(null);
    store.setIsRated(true);
    store.setIsPracticeContinuation(false);
    store.setDrillOpeningKey(null);
    store.setDrillLine(null);
    store.setDrillOpeningName(null);
    store.setDrillState(null);
    store.setDrillStrictness(null);
    store.setDrillStrictnessCp(null);
    setShowRevertWarning(false);
    setShowResignWarning(false);
    clearMoveHighlights();
    // DecisionOwner decision state is cleared by its fullReset, driven by
    // coordinator.clearSession → emitReset above.
    resetMode();
  }, [
    chess,
    coordinator,
    clearMoveHighlights,
    resetEngine,
    resetMode,
    setBlunderAlert,
    setBlunderReviewId,
    setBlunderReviewSrs,
    setBlunderTargetFen,
    setResolvedReview,
    setPendingPromotion,
    clearBlunderBoardOverride,
    clearReviewedDrillReturn,
    setEngineMessage,
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
    if (
      !store.sessionId ||
      (store.drillState !== "root_reached" && store.drillState !== "failed")
    ) {
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
    abandonStoppedDrill,
    showRevertWarning,
  };
};
