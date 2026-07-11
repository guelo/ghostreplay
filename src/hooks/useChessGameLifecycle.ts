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
import { pollFreshOpeningDelta } from "../utils/openingDeltaPoll";
import type { GameAnalysisCoordinator } from "../services/GameAnalysisCoordinator";
import {
  buildSessionMoveUploads,
  fillUnresolvedTerminal,
} from "../components/chess-game/domain/sessionUpload";
import { STARTING_FEN } from "../components/chess-game/config";
import type { RatingScores } from "../utils/api";

// Upper bound on the final pre-terminal move upload (g-xanz). Keeps a hung or
// lock-bound /moves from blocking game/drill end; the opening-score delta is
// supplementary, so on timeout we proceed and let it degrade.
const FINAL_UPLOAD_TIMEOUT_MS = 4000;

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
  // Non-committed difficulty seed for the start panel (g-fxrm): post-game New
  // Game samples into this, not the committed store engineElo.
  setSeedEngineElo: Dispatch<SetStateAction<number>>;
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
  /**
   * Fired once per session at the single genuine-end choke point (g-8079),
   * under the exact same gate as end-game audio — so it is suppressed for
   * practice-continuation ends, drill-abandon, and "practice ended". Drives the
   * dramatic win/loss/draw fanfare over the board.
   */
  onGameFinished?: (result: GameResult) => void;
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
  showRevertWarning,
  setShowRevertWarning,
  setShowResignWarning,
  setResolvedReview,
  setPendingPromotion,
  clearBlunderBoardOverride,
  clearReviewedDrillReturn,
  onGameFinished,
}: UseChessGameLifecycleArgs) => {
  const revertExecutionIdRef = useRef(0);
  // Per-session dedupe for the end-game announcement (audio + fanfare). Set to
  // the finalizing session id the first time it announces, so a duplicate
  // same-session finalization stays silent.
  const announcedEndGameSessionIdRef = useRef<string | null>(null);
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
        announcedEndGameSessionIdRef.current !== finalizingSessionId
      ) {
        announcedEndGameSessionIdRef.current = finalizingSessionId;
        playEndGameAudio(result);
        // Fire the fanfare from the same gate/choke point as audio: genuine ends
        // only, once per session (g-8079).
        onGameFinished?.(result);
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
      onGameFinished,
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
        // abandoned drill still in the store; skip resampling so the post-drill
        // UI shows the Elo you just played. Again/gear resample on action.
        if (!s.isGameActive && s.drillState === null) {
          // Seed the panel difficulty only (g-fxrm) — an idle mount must not
          // mutate the committed store engineElo; it commits on Start. seedEngineElo
          // resyncs into the open panel so the first start uses this sample.
          setSeedEngineElo(sampleEloBin(data.current_rating));
        }
      })
      .catch(() => {});
  }, []);

  // Durably upload the FULL move history before a terminal recompute. The
  // end-of-session opening-score delta diffs the played chain from session_moves
  // and recomputes after-scores from graph evidence, so both depend on this
  // game's moves being persisted first. The incremental uploader is fire-and-
  // forget and resolved-analysis-only, so it can race the recompute and yield a
  // short/stale chain. This awaits a complete upload (every move carries its
  // fen_after even when analysis is unresolved; the /moves endpoint upserts), so
  // the delta is correct.
  //
  // BOUNDED + non-fatal: the delta is supplementary, so the upload must never
  // hold the primary terminal action (rating/result/drill-state) hostage. A
  // hung or lock-bound /moves is cut off by an AbortSignal timeout; on
  // abort/reject we log and proceed, leaving the delta to degrade.
  const uploadFullMoveHistoryBeforeEnd = useCallback(
    async (sessionId: string) => {
      // Stop the incremental uploader FIRST so this is the last /moves this
      // client emits for the session (g-y90g). Folding the stop in here — rather
      // than at each terminal call site — makes "the final full upload is the
      // last /moves" STRUCTURAL: no terminal path (game end, drill natural-end,
      // resign, accuracy-fail) can forget it. stopSessionUploads only touches
      // the coordinator's upload bookkeeping (disables the timer, clears dirty,
      // aborts the in-flight fetch); the full-history upload below reads
      // moveHistory + analysisMap directly, so it is unaffected. All callers are
      // terminal, so the permanent disable until the next startSession is correct.
      coordinator.stopSessionUploads();
      try {
        const uploads = fillUnresolvedTerminal(
          buildSessionMoveUploads(
            useGameStore.getState().moveHistory,
            new Map(coordinator.store.getState().analysisMap),
            STARTING_FEN,
          ),
          STARTING_FEN,
        );
        if (uploads.length > 0) {
          // The final, complete upload carries recomputeOpportunity: true so the
          // backend computes blunder opportunity exactly once at finalize (the
          // mid-game incremental uploads skipped it). Any already-dispatched
          // in-flight incremental that races to the server is harmless: the
          // evidence enqueue coalesces by session_id and only this true-flagged
          // entry drives the single recompute.
          await uploadSessionMoves(sessionId, uploads, {
            signal: AbortSignal.timeout(FINAL_UPLOAD_TIMEOUT_MS),
            recomputeOpportunity: true,
          });
        }
      } catch (err) {
        console.error(
          "[SessionMoves] Final move-history upload failed/timed out:",
          err,
        );
      }
    },
    [coordinator],
  );

  const handleGameEnd = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.sessionId || !store.isGameActive) return;
    const finalizingSessionId = store.sessionId;

    let result: GameResult | null = null;

    if (chess.isCheckmate()) {
      const loser = chess.turn() === "w" ? "white" : "black";
      const playerWon = store.playerColor !== loser;
      result = playerWon
        ? { type: "checkmate_win", message: "Checkmate! You won!", reason: "checkmate" }
        : { type: "checkmate_loss", message: "Checkmate! You lost.", reason: "checkmate" };
    } else if (chess.isStalemate()) {
      result = { type: "draw", message: "Stalemate! The game is a draw.", reason: "stalemate" };
    } else if (chess.isThreefoldRepetition()) {
      result = { type: "draw", message: "Draw by threefold repetition.", reason: "threefold" };
    } else if (chess.isInsufficientMaterial()) {
      result = { type: "draw", message: "Draw by insufficient material.", reason: "insufficient" };
    } else if (chess.isDrawByFiftyMoves()) {
      // Checked BEFORE the generic isDraw() (which also returns true here) so
      // the fanfare names the fifty-move rule instead of a bare "Draw" (g-8079).
      result = { type: "draw", message: "Draw by the fifty-move rule.", reason: "fifty_move" };
    } else if (chess.isDraw()) {
      result = { type: "draw", message: "The game is a draw.", reason: "draw" };
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
          // Persist the full drill move history before the backend recomputes,
          // so the opening-score delta reflects this drill (g-xanz). This also
          // stops the incremental uploader (folded into the helper, g-y90g),
          // discarding the unresolved tail and flagging the opportunity recompute.
          await uploadFullMoveHistoryBeforeEnd(store.sessionId);
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
          s.setOpeningScoreChanges(contract.opening_score_changes ?? null);
          // The immediate delta is the warm/possibly-stale cache; reconcile to the
          // provably-fresh value once the background recompute lands (g-fix-end-latency).
          void pollFreshOpeningDelta(finalizingSessionId);
          s.setIsRated(false);
          // Natural-ended drills remain hidden and unrated unless converted.
          // (The upload tail was already discarded by stopSessionUploads, folded
          // into uploadFullMoveHistoryBeforeEnd above — g-y90g.)
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
        // Await a complete move upload so the opening-score delta sees the full
        // played chain and fresh after-scores (replaces the prior fire-and-forget
        // resolved-only flush, which could race the recompute).
        await uploadFullMoveHistoryBeforeEnd(store.sessionId);

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
        // Opening deltas are independent of rating gating (unrated/practice games
        // still earn them), so set them outside the rating block.
        useGameStore
          .getState()
          .setOpeningScoreChanges(endResponse.opening_score_changes ?? null);
        // Reconcile the warm delta to the provably-fresh value once the background
        // recompute lands (g-fix-end-latency).
        void pollFreshOpeningDelta(finalizingSessionId);
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
    uploadFullMoveHistoryBeforeEnd,
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

        // This path uploads the PRE-revert snapshot directly (not the live
        // moveHistory), so it does NOT go through uploadFullMoveHistoryBeforeEnd.
        // Stop the incremental uploader first so this terminal resign-before-
        // revert upload is the last /moves emitted, and flag it for the single
        // opportunity recompute (g-y90g).
        coordinator.stopSessionUploads();
        await uploadSessionMoves(store.sessionId!, snapshotUploads, {
          recomputeOpportunity: true,
        });
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
        s.setOpeningScoreChanges(endResponse.opening_score_changes ?? null);
        // Reconcile the warm delta to the provably-fresh value once the background
        // recompute lands (g-fix-end-latency). store.sessionId is the id resigned above.
        void pollFreshOpeningDelta(store.sessionId!);
        s.setIsRated(false);
        s.setIsPracticeContinuation(true);
        s.setDrillState(null);
        s.setDrillStrictnessCp(null);
        // (Uploads were already stopped before the snapshot upload above — g-y90g.)
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
        announcedEndGameSessionIdRef.current = null;

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
        s2.setOpeningScoreChanges(null);
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
        announcedEndGameSessionIdRef.current = null;

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
        s.setOpeningScoreChanges(null);

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
          // strictnessCp is intentionally not persisted (g-09mu force-always):
          // the setup panel never prefills strictness, so a stored value would
          // never be read.
          const prefs = {
            openingKey: options.openingKey,
            engineElo: options.engineElo,
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

      // Await a complete move upload so the resigned game's opening-score delta
      // reflects the full played chain (matches handleGameEnd).
      await uploadFullMoveHistoryBeforeEnd(store.sessionId);

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
      // Opening deltas are rating-independent, so set them outside the rating
      // block (P2: a resigned game must still surface them).
      useGameStore
        .getState()
        .setOpeningScoreChanges(endResponse.opening_score_changes ?? null);
      // Reconcile the warm delta to the provably-fresh value once the background
      // recompute lands (g-fix-end-latency).
      void pollFreshOpeningDelta(finalizingSessionId);
      // The only resign path that reaches the fanfare (audio gate default-on), so
      // tag its reason for the termination-type subtitle (g-8079). The three
      // pseudo-end resign literals (abandonStoppedDrill "Drill abandoned.",
      // handleResign practice-ended + drill-abandoned) pass playEndGameAudio:false
      // and never display, so they are intentionally left untagged; the optional
      // reason + type fallback (resign→resignation) still covers any future display.
      finishLocalGame(
        { type: "resign", message: "You resigned.", reason: "resignation" },
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
    uploadFullMoveHistoryBeforeEnd,
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
    announcedEndGameSessionIdRef.current = null;
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
    // Re-randomize difficulty (g-ncvm) into the panel seed only — opening the
    // popup must not mutate the committed store engineElo (g-fxrm).
    setSeedEngineElo(sampleEloBin(store.playerRating));
  }, [
    setShowPostGamePrompt,
    setShowStartOverlay,
    setSeedEngineElo,
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
    // Exposed so the drill accuracy-fail path (in ChessGame) can apply the same
    // bounded full-history upload barrier before requesting its terminal delta.
    uploadFullMoveHistoryBeforeEnd,
    showRevertWarning,
  };
};
