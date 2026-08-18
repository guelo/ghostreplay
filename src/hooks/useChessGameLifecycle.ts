import { useCallback, useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { Chess } from "chess.js";
import type {
  DrillSessionContract,
  DrillStrictness,
  TargetBlunderSrs,
  TerminalAction,
} from "../utils/api";
import {
  abandonDrill,
  continueDrill,
  endGame,
  fetchCurrentRating,
  naturalEndDrill,
  newClientRequestId,
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
import {
  abortOpeningDeltaPolls,
  pollFreshOpeningDelta,
} from "../utils/openingDeltaPoll";
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

// Slice of FINAL_UPLOAD_TIMEOUT_MS (not additive to it) spent waiting for every
// unresolved move analysis before the final upload (g-2nrn,
// g-history-accuracy). Checkmate/draw endings exclude only the final ply because
// fillUnresolvedTerminal can supply that exact terminal value; non-terminal
// resignation and accuracy-fail paths cannot synthesize any row. One missing
// evaluation anywhere in the played grid can null whole-game accuracy.
//
// Sized from measurement, not intuition. Depth-17 settle time is bimodal: a
// tail ply inside a forced mating sequence settles in ~3ms (tiny tree),
// while a quiet one (resign/draw endings) takes ~1.7s median and ~3.0s p90 on
// mid-tier desktop hardware. Covering the quiet arm would need most of the 4s
// budget, and the final upload ALREADY times out at ~18.5% of terminal actions
// — taking that time would convert "null accuracy" into "tail never persisted",
// which is strictly worse. So this buys the forced-mate arm plus analyses that
// are nearly done (the wait only needs the RESIDUAL settle time, since the
// analysis has been running since its move was played), and leaves
// the upload budget essentially intact.
const TAIL_SETTLE_BUDGET_MS = 300;

const hasUsableMoveEvaluation = (
  analysis:
    | { playedEval: number | null; playedEvalMate: number | null }
    | undefined,
): boolean =>
  analysis !== undefined &&
  (analysis.playedEval !== null || analysis.playedEvalMate !== null);

type ReanalysisInput = {
  fenBefore: string;
  moveUci: string;
  moverColor: "white" | "black";
  legalMoveCount: number;
};

/**
 * Recover the immutable inputs needed to retry one recorded move. Fail closed
 * unless chess.js can replay the UCI move from the recorded pre-move FEN and
 * reproduce both its SAN and post-move FEN.
 */
const reanalysisInputForMove = (
  history: MoveRecord[],
  moveIndex: number,
): ReanalysisInput | null => {
  const move = history[moveIndex];
  if (!move || move.uci.length < 4) return null;

  const fenBefore =
    moveIndex === 0
      ? STARTING_FEN
      : history[moveIndex - 1]?.fen;
  if (!fenBefore) return null;

  try {
    const replay = new Chess(fenBefore);
    const legalMoveCount = replay.moves().length;
    const applied = replay.move({
      from: move.uci.slice(0, 2),
      to: move.uci.slice(2, 4),
      promotion: move.uci.slice(4) || undefined,
    });
    if (
      !applied ||
      applied.san !== move.san ||
      replay.fen() !== move.fen
    ) {
      return null;
    }
    return {
      fenBefore,
      moveUci: move.uci,
      moverColor: moveIndex % 2 === 0 ? "white" : "black",
      legalMoveCount,
    };
  } catch {
    return null;
  }
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
  }, [setSeedEngineElo]);

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
    async (
      sessionId: string,
      terminalAction: TerminalAction,
    ): Promise<number | null | undefined> => {
      // `undefined` is an ownership-cancellation sentinel, not an unknown line
      // revision: callers must skip their terminal request because this async
      // invocation no longer owns the session. `null` means the current session
      // has unknown line state and must still terminalize fail-closed.
      // Stop the ordinary incremental uploader FIRST so this is the sole
      // FULL/finality upload this client emits for the session (g-y90g). Folding
      // the stop in here — rather than at each terminal call site — makes that
      // ownership structural: no terminal path (game end, drill natural-end,
      // resign, accuracy-fail) can forget it. Later g-residual-eval-gaps
      // requests are sparse real-evaluation repairs, never another full snapshot.
      // stopSessionUploads only touches
      // the coordinator's upload bookkeeping (disables the timer, clears dirty,
      // aborts the in-flight fetch); the full-history upload below reads
      // moveHistory + analysisMap directly, so it is unaffected. All callers are
      // terminal, so the permanent disable until the next startSession is correct.
      // VALIDATE before stopping anything (g-2nrn). An already-stale invocation
      // that merely captured its session id would otherwise disable a NEWLY
      // started session's uploads before ever reaching the post-wait revalidation.
      // Both the store and the coordinator must agree we are still finalizing
      // the requested session.
      const epochBefore = coordinator.getEpoch();
      if (
        useGameStore.getState().sessionId !== sessionId ||
        epochBefore.sessionId !== sessionId
      ) {
        return undefined;
      }
      const deadlineStartedAt = performance.now();
      const preUploadDeadlineAt =
        deadlineStartedAt + TAIL_SETTLE_BUDGET_MS;

      // A takeback response that has not been cleanly acknowledged leaves the
      // branch generation unknown. Never wait for it and never publish a final
      // upload from that branch; the terminal endpoint receives null and
      // atomically discards move evidence while still completing the action.
      const lineRevision = coordinator.getLineRevision(sessionId);

      coordinator.stopSessionUploads();
      if (lineRevision === null) return null;

      // Freeze the history BEFORE the wait so the payload can never mix this
      // session's plies with a later session's. Stopping uploads does not stop
      // analysis RESOLUTION — resolveAnalysis writes to analysisMap before the
      // uploadsEnabled gate — so a tail ply that settles during the wait still
      // reaches the payload below, while g-y90g's "final upload is the last
      // /moves" invariant is preserved.
      const frozenHistory = useGameStore.getState().moveHistory;

      // Recover every row that lacks a real score, not just the penultimate
      // one. Production showed that a serial worker can preserve later results
      // after an earlier request fails, leaving a complete row grid with a
      // multi-row hole just before the terminal move. Reuse a live request when
      // one exists; otherwise reconstruct immutable analysis inputs from the
      // recorded FEN chain and reschedule it.
      const canSynthesizeFinalEvaluation =
        terminalAction === "game_end" ||
        terminalAction === "drill_natural_end";
      const initialAnalysisMap =
        coordinator.store.getState().analysisMap;
      const reanalysisInputs = new Map<number, ReanalysisInput>();
      const unresolvedEvalIndices: number[] = [];
      const lastIndex = frozenHistory.length - 1;
      for (let moveIndex = 0; moveIndex < frozenHistory.length; moveIndex += 1) {
        if (canSynthesizeFinalEvaluation && moveIndex === lastIndex) continue;
        if (hasUsableMoveEvaluation(initialAnalysisMap.get(moveIndex))) continue;

        const input = reanalysisInputForMove(frozenHistory, moveIndex);
        if (!input) continue;
        reanalysisInputs.set(moveIndex, input);
        if (
          coordinator.ensurePendingAnalysis(
            sessionId,
            epochBefore.generation,
            moveIndex,
            input.fenBefore,
            input.moveUci,
            input.moverColor,
            input.legalMoveCount,
          )
        ) {
          unresolvedEvalIndices.push(moveIndex);
        }
      }

      if (unresolvedEvalIndices.length > 0) {
        const tailBudgetMs = Math.max(
          0,
          Math.floor(preUploadDeadlineAt - performance.now()),
        );
        await coordinator.settleWithin(
          unresolvedEvalIndices,
          tailBudgetMs,
        );
      }

      // REVALIDATE after the await. If startSession() ran during the wait it
      // bumped the generation, replaced uploadState and cleared analysisMap —
      // uploading now would persist the NEW session's data under the OLD
      // session id.
      const epochAfter = coordinator.getEpoch();
      if (
        useGameStore.getState().sessionId !== sessionId ||
        epochAfter.generation !== epochBefore.generation
      ) {
        return undefined;
      }

      const finalClientRequestId = newClientRequestId();
      const armedRepairIndices = new Set<number>();
      try {
        // If any real analysis missed the shared bounded wait, retain an
        // independently guarded sparse repair for that row. Arm BEFORE
        // snapshotting analysisMap so there is no gap where a resolution can
        // miss both final_full and its repair. A request that failed during the
        // wait is rescheduled once here before arming.
        for (const moveIndex of unresolvedEvalIndices) {
          const input = reanalysisInputs.get(moveIndex);
          if (!input) continue;
          try {
            if (
              coordinator.ensurePendingAnalysis(
                sessionId,
                epochBefore.generation,
                moveIndex,
                input.fenBefore,
                input.moveUci,
                input.moverColor,
                input.legalMoveCount,
              ) &&
              coordinator.armLateEvaluationRepair(
                sessionId,
                epochBefore.generation,
                moveIndex,
                frozenHistory,
                finalClientRequestId,
              )
            ) {
              armedRepairIndices.add(moveIndex);
            }
          } catch (err) {
            console.error(
              "[SessionMoves] Could not arm late evaluation repair:",
              err,
            );
          }
        }

        const analysisMap = new Map(
          coordinator.store.getState().analysisMap,
        );
        // A resolution that won the race into the final snapshot is already
        // carried by final_full, so disarm only that row's sparse repair.
        for (const moveIndex of [...armedRepairIndices]) {
          if (!hasUsableMoveEvaluation(analysisMap.get(moveIndex))) continue;
          coordinator.cancelLateEvaluationRepair(
            sessionId,
            epochBefore.generation,
            moveIndex,
          );
          armedRepairIndices.delete(moveIndex);
        }

        const uploads = fillUnresolvedTerminal(
          buildSessionMoveUploads(
            frozenHistory,
            analysisMap,
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
          // ONE ABSOLUTE DEADLINE, not additive: the upload inherits whatever
          // is left of FINAL_UPLOAD_TIMEOUT_MS after the tail wait, so the
          // terminal-action bound stays 4s rather than drifting to 4.3s.
          // Subtract ACTUAL elapsed, not the nominal budget — a tail that
          // settles in 5ms hands the upload back its remaining ~3995ms.
          // FLOOR, and floor rather than round: AbortSignal.timeout() rejects a
          // fractional delay (performance.now() is sub-millisecond), and
          // rounding up could push past the absolute deadline.
          const remainingBudgetMs = Math.max(
            0,
            Math.floor(
              FINAL_UPLOAD_TIMEOUT_MS - (performance.now() - deadlineStartedAt),
            ),
          );
          // final_full: uploadSessionMoves constructs the timeout from deadlineMs
          // (so the recorded deadline_ms and the live signal cannot drift) and
          // stamps upload_kind + terminal_action so this end-of-session upload is
          // isolable in telemetry and joinable to its durable receipt (g-upload-observe).
          await uploadSessionMoves(sessionId, uploads, {
            uploadKind: "final_full",
            terminalAction,
            deadlineMs: remainingBudgetMs,
            clientRequestId: finalClientRequestId,
            recomputeOpportunity: true,
            lineRevision,
          });
        }
      } catch (err) {
        console.error(
          "[SessionMoves] Final move-history upload failed/timed out:",
          err,
        );
      } finally {
        if (armedRepairIndices.size > 0) {
          // Synchronous release only: the terminal action never awaits the late
          // repairs or inherits their retries.
          try {
            coordinator.releaseLateEvaluationRepair(
              sessionId,
              epochBefore.generation,
            );
          } catch (err) {
            console.error(
              "[SessionMoves] Could not release late evaluation repair:",
              err,
            );
          }
        }
      }
      return lineRevision;
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
          const lineRevision = await uploadFullMoveHistoryBeforeEnd(
            store.sessionId,
            "drill_natural_end",
          );
          if (lineRevision === undefined) return;
          const contract = await naturalEndDrill(
            store.sessionId,
            result.type,
            chess.pgn(),
            lineRevision,
          );
          if (useGameStore.getState().sessionId !== finalizingSessionId) {
            return;
          }
          const s = useGameStore.getState();
          s.setDrillState(contract.drill_state);
          s.setDrillTerminalReason(contract.terminal_reason ?? null);
          s.setTerminalOpeningDelta(
            finalizingSessionId,
            contract.opening_score_changes ?? null,
          );
          // The immediate delta is the warm/possibly-stale cache; reconcile to the
          // provably-fresh value once the background recompute lands (g-fix-end-latency).
          void pollFreshOpeningDelta(
            finalizingSessionId,
            "drill_natural_end",
          );
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
        const lineRevision = await uploadFullMoveHistoryBeforeEnd(
          store.sessionId,
          "game_end",
        );
        if (lineRevision === undefined) return;

        const endResponse = await endGame(
          store.sessionId,
          result.type,
          chess.pgn(),
          store.isRated,
          lineRevision,
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
          .setTerminalOpeningDelta(
            finalizingSessionId,
            endResponse.opening_score_changes ?? null,
          );
        // Reconcile the warm delta to the provably-fresh value once the background
        // recompute lands (g-fix-end-latency).
        void pollFreshOpeningDelta(finalizingSessionId, "game_end");
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
    finishLocalGame,
    uploadFullMoveHistoryBeforeEnd,
    setEngineMessage,
  ]);

  const rewindBoardLocally = useCallback((
    storeMoveHistory: MoveRecord[],
    coordinatorAlreadyPruned = false,
  ) => {
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
    if (!coordinatorAlreadyPruned) {
      coordinator.pruneFromMoveIndex(newHistory.length);
    }
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
  ]);

  const executeRevert = useCallback(async () => {
    const store = useGameStore.getState();
    if (!store.isGameActive || store.moveHistory.length === 0 || chess.isGameOver()) return;

    const snapshotMoveHistory = [...store.moveHistory];
    const afterPly = getRewindHistoryLength(snapshotMoveHistory);
    const synchronizeActiveUnratedLine =
      !store.isRated && !store.isPracticeContinuation;
    const coordinatorPruned = synchronizeActiveUnratedLine
      ? coordinator.transitionMoveLine(afterPly)
      : false;
    if (synchronizeActiveUnratedLine && !coordinatorPruned) {
      // canTransitionMoveLine disables the ordinary refusal cases in the UI.
      // This is only a race backstop between the subscribed render and click:
      // preserve every local/SRS state change and leave the board untouched.
      return;
    }

    const executionId = revertExecutionIdRef.current + 1;
    revertExecutionIdRef.current = executionId;
    setShowResignWarning(false);
    setRevertError(null);
    setIsRevertPending(true);
    clearBlunderBoardOverride?.();

    // Cancel pending SRS reviews for the reverted indices BEFORE the awaited
    // upload/endGame, so an analysis resolving during that async window cannot
    // POST a review the revert is cancelling (durable resolved slots survive).
    coordinator.decisionOwner.cancelPendingSrsReviews(
      afterPly,
    );
    setResolvedReview(null);

    try {
      if (!store.isPracticeContinuation && store.isRated) {
        const snapshotPgn = chess.pgn();
        const lineRevision = coordinator.getLineRevision(store.sessionId!);
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
        if (lineRevision !== null) {
          await uploadSessionMoves(store.sessionId!, snapshotUploads, {
            uploadKind: "revert",
            recomputeOpportunity: true,
            lineRevision,
          });
        }
        if (!isCurrentRevertExecution(executionId)) {
          return;
        }
        const endResponse = await endGame(
          store.sessionId!,
          "resign",
          snapshotPgn,
          true,
          lineRevision,
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
        s.setTerminalOpeningDelta(
          store.sessionId!,
          endResponse.opening_score_changes ?? null,
        );
        // Reconcile the warm delta to the provably-fresh value once the background
        // recompute lands (g-fix-end-latency). store.sessionId is the id resigned above.
        void pollFreshOpeningDelta(store.sessionId!, "game_revert");
        s.setIsRated(false);
        s.setIsPracticeContinuation(true);
        s.setDrillState(null);
        s.setDrillStrictnessCp(null);
        // (Uploads were already stopped before the snapshot upload above — g-y90g.)
      }

      if (!isCurrentRevertExecution(executionId)) {
        return;
      }

      rewindBoardLocally(snapshotMoveHistory, coordinatorPruned);
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
        // The end screen is gone as of this click; a delta reconciling during the
        // awaits below belongs in the late queue, not an invisible slot (g-f3m4).
        store.setDepartingSession(store.sessionId);
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
            await abandonDrill(
              store.sessionId,
              coordinator.getLineRevision(store.sessionId),
            );
            coordinator.stopSessionUploads();
          } else {
            coordinator.flushPendingUploads().catch((err) =>
              console.error("[SessionMoves] Flush failed:", err),
            );
            await endGame(
              store.sessionId,
              "abandon",
              chess.pgn(),
              store.isRated,
              coordinator.getLineRevision(store.sessionId),
            );
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
        // Flip the session and clear the delta slot as ONE transition (g-f3m4);
        // a separate flip-then-clear would destroy a delta that resolved during
        // the await. Late arrivals were already routed to the queue by the
        // setDepartingSession mark above.
        s2.beginSession(response.session_id, response.move_line_revision);
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
        coordinator.startSession(
          response.session_id,
          response.move_line_revision,
        );
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
        // The start failed, so the player is still on the old session's end
        // screen — undo the departure mark or its delta would only ever queue.
        useGameStore.getState().setDepartingSession(null);
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
      setIsRevertPending,
      setIsStartingGame,
      setRevertError,
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
      let clearedDepartingDrillCoordinator = false;
      try {
        setIsStartingGame(true);
        setStartError(null);
        revertExecutionIdRef.current += 1;
        announcedEndGameSessionIdRef.current = null;

        const store = useGameStore.getState();
        // The end screen is gone as of this click; a delta reconciling during the
        // awaits below belongs in the late queue, not an invisible slot (g-f3m4).
        store.setDepartingSession(store.sessionId);
        if (store.sessionId && store.isGameActive && !store.isPracticeContinuation) {
          // Cancel pending SRS reviews BEFORE the awaited abandon/endGame.
          coordinator.decisionOwner.cancelPendingSrsReviews();
          setResolvedReview(null);
          if (store.drillOpeningKey && store.drillState !== "converted") {
            const abandonedSessionId = store.sessionId;
            await abandonDrill(
              abandonedSessionId,
              coordinator.getLineRevision(abandonedSessionId),
            );
            if (useGameStore.getState().sessionId !== abandonedSessionId) {
              setIsStartingGame(false);
              return null;
            }
            coordinator.stopSessionUploads();
            // The backend has ended this drill. Cross the matching client-side
            // terminal boundary before requesting its replacement so a failed
            // start cannot resurrect a playable-but-abandoned board, and so no
            // old-session analysis can resolve during the request gap.
            coordinator.clearSession();
            clearedDepartingDrillCoordinator = true;
            const abandonedStore = useGameStore.getState();
            abandonedStore.setDrillState("abandoned");
            abandonedStore.setIsRated(false);
            finishLocalGame(
              { type: "resign", message: "Drill abandoned." },
              { playEndGameAudio: false, finalizingSessionId: abandonedSessionId },
            );
          } else {
            coordinator.flushPendingUploads().catch((err) =>
              console.error("[SessionMoves] Flush failed:", err),
            );
            await endGame(
              store.sessionId,
              "abandon",
              chess.pgn(),
              store.isRated,
              coordinator.getLineRevision(store.sessionId),
            );
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
        // Atomic flip + clear; see the startGame path (g-f3m4).
        s.beginSession(response.session_id, response.move_line_revision);
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
        if (!clearedDepartingDrillCoordinator) {
          coordinator.clearSession();
        }
        coordinator.startSession(
          response.session_id,
          response.move_line_revision,
        );
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
        // A successfully abandoned drill was finalized locally before startDrill;
        // clearing this marker only restores delta routing and never reactivates
        // that old board. Other failures still leave their prior end screen up.
        useGameStore.getState().setDepartingSession(null);
        return null;
      }
    },
    [
      chess,
      coordinator,
      clearMoveHighlights,
      finishLocalGame,
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
      await abandonDrill(
        store.sessionId,
        coordinator.getLineRevision(store.sessionId),
      );
      if (useGameStore.getState().sessionId !== finalizingSessionId) {
        return;
      }
      coordinator.stopSessionUploads();
      const s = useGameStore.getState();
      // The server now PRESERVES a terminal drill_state ('failed') across abandon
      // (g-drill-failed-overwrite), so it no longer reports 'abandoned' for a
      // stopped drill. The store's drillState is the CLIENT lifecycle: a successful
      // abandon is this client's "finalized" sentinel, regardless of the persisted
      // outcome label. Read by isReviewedDrillReturnValid, isStoppedDrill, and
      // handleContinueDrill, none of which should see 'failed' after finalization.
      s.setDrillState("abandoned");
      s.setIsRated(false);
    }

    if (useGameStore.getState().sessionId !== finalizingSessionId) {
      return;
    }
    finishLocalGame(
      { type: "resign", message: "Drill abandoned." },
      { playEndGameAudio: false, finalizingSessionId },
    );
  }, [coordinator, finishLocalGame]);

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
        await abandonDrill(
          store.sessionId,
          coordinator.getLineRevision(store.sessionId),
        );
        if (useGameStore.getState().sessionId !== finalizingSessionId) {
          return;
        }
        coordinator.stopSessionUploads();
        const s = useGameStore.getState();
        // Client lifecycle sentinel, not a mirror of the persisted outcome — see
        // abandonStoppedDrill (g-drill-failed-overwrite).
        s.setDrillState("abandoned");
        s.setIsRated(false);
        finishLocalGame(
          { type: "resign", message: "Drill abandoned." },
          { playEndGameAudio: false, finalizingSessionId },
        );
        return;
      }

      // Await a complete move upload so the resigned game's opening-score delta
      // reflects the full played chain (matches handleGameEnd).
      const lineRevision = await uploadFullMoveHistoryBeforeEnd(
        store.sessionId,
        "resign",
      );
      if (lineRevision === undefined) return;

      const endResponse = await endGame(
        store.sessionId,
        "resign",
        chess.pgn(),
        store.isRated,
        lineRevision,
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
        .setTerminalOpeningDelta(
          finalizingSessionId,
          endResponse.opening_score_changes ?? null,
        );
      // Reconcile the warm delta to the provably-fresh value once the background
      // recompute lands (g-fix-end-latency).
      void pollFreshOpeningDelta(finalizingSessionId, "game_resign");
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
    store.setMoveLineRevision(0);
    // Deliberate abandonment, not a supersede: drop the current delta, drop any
    // queued late notifications, and invalidate in-flight polls so a response
    // already on the wire cannot resurface as a phantom toast (g-f3m4). The
    // token invalidates results; aborting stops the loops still retrying.
    store.abandonOpeningDeltas();
    abortOpeningDeltaPolls();
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
    handleContinueDrill,
    abandonStoppedDrill,
    // Exposed so the drill accuracy-fail path (in ChessGame) can apply the same
    // bounded full-history upload barrier before requesting its terminal delta.
    uploadFullMoveHistoryBeforeEnd,
    showRevertWarning,
  };
};
