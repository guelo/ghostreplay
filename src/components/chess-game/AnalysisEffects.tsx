import { useCallback, useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { TargetBlunderSrs } from "../../utils/api";
import type { ResolvedReview } from "./types";
import {
  recordBlunder,
  reviewSrsBlunder,
} from "../../utils/api";
import { shouldRecordBlunder } from "../../utils/blunder";
import { evalLoss, gradeRecordableMove } from "../../workers/analysisUtils";
import {
  buildBlunderAlert,
  fenBeforeMove,
  sanForUciMove,
  type BlunderAlert,
} from "./domain/movePresentation";
import type { MoveMessage, SrsFailDetail } from "../MoveList";
import { useAnalysisStoreApi } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";
import { playBling } from "../../utils/blingSound";
import { playBuzzer } from "../../utils/buzzerSound";
import { playRandomBlunderAudio } from "./blunderAudio";
import type { AnalysisResult } from "../../hooks/useMoveAnalysis";
import {
  gameAnalysisCoordinator,
  type AnalysisOutcome,
  type AnalysisOutcomeSource,
  type AnalysisResetInfo,
} from "../../services/GameAnalysisCoordinator";

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
  srsDecisionId: string;
};

/**
 * Recording-frontier slot (g-hpw4 consumer b). One per moveIndex. `pending`
 * blocks the frontier; the terminal statuses unblock it. Only `resolved` slots
 * are blunder candidates, decided through the existing shouldRecordBlunder. The
 * `*Snapshot` fields capture every shouldRecordBlunder input at resolution time
 * (Finding 5/J1), since the decision may run later (buffered behind an earlier
 * pending slot).
 */
type FrontierSlot = {
  requestId: string;
  // `pending` blocks the frontier; the terminal statuses unblock it. (A
  // `scheduled` outcome opens the slot as `pending`.)
  status: "pending" | "resolved" | "failed" | "skipped";
  result?: AnalysisResult;
  context?: PendingAnalysisContext;
  sessionIdSnapshot?: string | null;
  isGameActiveSnapshot?: boolean;
};

type AnalysisEffectsProps = {
  pendingAnalysisContextRef: MutableRefObject<Map<string, PendingAnalysisContext>>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingSrsReviewRef: MutableRefObject<Map<string, PendingSrsReview>>;
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
  onSrsFail: (detail: SrsFailDetail, moveIndex: number) => void;
  /** Defaults to the singleton coordinator; injectable for tests. */
  coordinator?: AnalysisOutcomeSource;
};

const AnalysisEffects = ({
  pendingAnalysisContextRef,
  blunderRecordedRef,
  pendingSrsReviewRef,
  appendMoveMessage,
  setBlunderAlert,
  setShowFlash,
  setResolvedReview,
  onSrsFail,
  coordinator = gameAnalysisCoordinator,
}: AnalysisEffectsProps) => {
  const analysisStoreApi = useAnalysisStoreApi();

  // --- Recording frontier + decision state (component refs, per the bead) ---
  const frontierRef = useRef<Map<number, FrontierSlot>>(new Map());
  const nextDecisionIndexRef = useRef(0);
  const committedDecisionIndexRef = useRef(0);
  const currentGenerationRef = useRef(0);

  // --- Microtask-coalesced blunder alert buffer (consumer c) ---
  const alertBufferRef = useRef<Array<{ moveIndex: number; result: AnalysisResult }>>([]);
  const alertScheduledRef = useRef(false);
  const alertEpochRef = useRef(0);

  const isPlayerMoveIndex = (index: number) => {
    if (index < 0) return false;
    const playerColor = useGameStore.getState().playerColor;
    const isWhiteMove = index % 2 === 0;
    return playerColor === "white" ? isWhiteMove : !isWhiteMove;
  };

  const processSrsReview = useCallback((analysis: AnalysisResult | null) => {
    if (!analysis || analysis.moveIndex === null) {
      return;
    }

    const pendingReview = pendingSrsReviewRef.current.get(analysis.id);
    if (!pendingReview || pendingReview.moveIndex !== analysis.moveIndex) {
      return;
    }

    // Tri-state grade BEFORE consuming the pending entry. An `unavailable`
    // result (missing/non-finite eval) must neither post a review nor delete
    // the entry — that would record a false pass/fail.
    const grade = gradeRecordableMove(analysis.delta);
    if (grade === "unavailable") {
      return;
    }

    pendingSrsReviewRef.current.delete(analysis.id);

    const evalLossCp = evalLoss(analysis.delta) ?? 0;
    const passed = grade === "pass";

    setResolvedReview((prev) =>
      prev?.analysisId === analysis.id
        ? {
            analysisId: analysis.id,
            moveIndex: analysis.moveIndex!,
            result: passed ? "pass" : "fail",
          }
        : prev,
    );

    if (passed) {
      const srs = pendingReview.srs;
      appendMoveMessage(analysis.moveIndex, {
        key: `srs-${analysis.id}`,
        text: "Correct! You avoided your past mistake.",
        variant: "srs-pass",
        srsStats: srs
          ? {
              passCount: srs.pass_count + 1,
              failCount: srs.fail_count,
              streak: srs.pass_streak + 1,
            }
          : undefined,
      });
    }

    if (!passed) {
      const sourceFen = fenBeforeMove(
        useGameStore.getState().moveHistory,
        analysis.moveIndex,
      );
      const bestMoveSan = sanForUciMove(sourceFen, analysis.bestMove);

      const srs = pendingReview.srs;
      const srsFailDetail: SrsFailDetail = {
        userMoveSan: pendingReview.userMoveSan,
        bestMoveSan,
        userMoveUci: analysis.move,
        bestMoveUci: analysis.bestMove,
      };
      appendMoveMessage(analysis.moveIndex, {
        key: `srs-${analysis.id}`,
        text: "You made this mistake again!",
        variant: "srs-fail",
        srsFailDetail,
        srsStats: srs
          ? {
              passCount: srs.pass_count,
              failCount: srs.fail_count + 1,
              streak: 0,
            }
          : undefined,
      });

      onSrsFail(srsFailDetail, analysis.moveIndex);
      playBuzzer();
    }

    const postReview = async () => {
      try {
        await reviewSrsBlunder(
          pendingReview.sessionId,
          pendingReview.blunderId,
          passed,
          pendingReview.userMoveSan,
          evalLossCp,
        );
      } catch (error) {
        console.error("[SRS] Failed to record review:", error);
      }
    };

    void postReview();
  }, [pendingSrsReviewRef, appendMoveMessage, setResolvedReview, onSrsFail]);

  // Run the recording decision for a single resolved candidate, via the
  // existing eligibility helper using the SNAPSHOTTED inputs (Finding 3/5).
  const runRecordingDecision = useCallback((slot: FrontierSlot) => {
    const blunderData = shouldRecordBlunder({
      analysis: slot.result ?? null,
      context: slot.context ?? null,
      sessionId: slot.sessionIdSnapshot ?? null,
      isGameActive: slot.isGameActiveSnapshot ?? false,
      alreadyRecorded: blunderRecordedRef.current,
    });

    if (!blunderData) {
      return;
    }

    blunderRecordedRef.current = true;

    const postBlunder = async () => {
      try {
        await recordBlunder(
          blunderData.sessionId,
          blunderData.pgn,
          blunderData.fen,
          blunderData.userMove,
          blunderData.bestMove,
          blunderData.evalBefore,
          blunderData.evalAfter,
        );
        console.log("[Blunder] Recorded blunder to backend");
      } catch (error) {
        console.error("[Blunder] Failed to record blunder:", error);
      }
    };

    void postBlunder();
  }, [blunderRecordedRef]);

  // Drain the frontier across every contiguous TERMINAL slot, blocking on the
  // first `pending`. Recording is gated by the monotonic committedDecisionIndex
  // (M2) so a rewound/replayed index never re-records.
  const advanceFrontier = useCallback(() => {
    const frontier = frontierRef.current;
    for (;;) {
      const idx = nextDecisionIndexRef.current;
      const slot = frontier.get(idx);
      if (!slot || slot.status === "pending") {
        break; // no slot yet, or open/reopened → wait for a terminal
      }

      if (
        slot.status === "resolved" &&
        idx >= committedDecisionIndexRef.current
      ) {
        runRecordingDecision(slot);
      }

      // The recording decision for this index is now finalized (recorded or
      // definitively passed over) — advance the irreversible boundary.
      committedDecisionIndexRef.current = Math.max(
        committedDecisionIndexRef.current,
        idx + 1,
      );
      nextDecisionIndexRef.current = idx + 1;
    }
  }, [runRecordingDecision]);

  const flushAlert = useCallback((epochAtSchedule: number) => {
    alertScheduledRef.current = false;
    const buffer = alertBufferRef.current;
    alertBufferRef.current = [];
    // K2: a session change / revert / unmount between scheduling and flush bumps
    // the epoch — drop the stale alert + audio.
    if (epochAtSchedule !== alertEpochRef.current) return;
    if (buffer.length === 0) return;

    // Latest-only: highest-moveIndex buffered player blunder (H3/I2).
    const top = buffer.reduce((a, b) => (b.moveIndex > a.moveIndex ? b : a));
    const result = top.result;
    if (result.moveIndex === null || result.delta === null) return;

    const moveHistory = useGameStore.getState().moveHistory;
    const moveSan = moveHistory[result.moveIndex]?.san ?? result.move;
    setBlunderAlert(
      buildBlunderAlert({
        moveHistory,
        moveIndex: result.moveIndex,
        moveSan,
        moveUci: result.move,
        bestMoveUci: result.bestMove,
        delta: result.delta,
        shouldRewind: true,
      }),
    );
    setShowFlash(true);
    playRandomBlunderAudio();
  }, [setBlunderAlert, setShowFlash]);

  const scheduleAlert = useCallback(() => {
    if (alertScheduledRef.current) return;
    alertScheduledRef.current = true;
    const epochAtSchedule = alertEpochRef.current;
    queueMicrotask(() => flushAlert(epochAtSchedule));
  }, [flushAlert]);

  // Single outcome-channel subscription: SRS-immediate, recording-frontier,
  // microtask alert. Seeds generation from getEpoch (M3) and drops stale
  // outcomes. Decision state lives in the refs above.
  useEffect(() => {
    currentGenerationRef.current = coordinator.getEpoch().generation;

    const handleOutcome = (o: AnalysisOutcome) => {
      if (o.generation !== currentGenerationRef.current) return;

      const frontier = frontierRef.current;

      if (o.status === "scheduled") {
        // Supersession/retry: migrate context + pending SRS old→new (one-shot),
        // (re)open the slot to pending, rewind the display frontier (M2 keeps
        // recording from re-firing via committedDecisionIndex).
        if (o.previousRequestId) {
          const ctxMap = pendingAnalysisContextRef.current;
          const prevCtx = ctxMap.get(o.previousRequestId);
          if (prevCtx && !ctxMap.has(o.requestId)) {
            ctxMap.set(o.requestId, prevCtx);
          }
          ctxMap.delete(o.previousRequestId);

          const srsMap = pendingSrsReviewRef.current;
          const prevSrs = srsMap.get(o.previousRequestId);
          if (prevSrs && !srsMap.has(o.requestId)) {
            srsMap.set(o.requestId, { ...prevSrs, analysisId: o.requestId });
            srsMap.delete(o.previousRequestId);
            setResolvedReview((prev) =>
              prev && prev.analysisId === o.previousRequestId
                ? { analysisId: o.requestId, moveIndex: prev.moveIndex, result: prev.result }
                : prev,
            );
          }
        }

        frontier.set(o.moveIndex, { requestId: o.requestId, status: "pending" });
        if (nextDecisionIndexRef.current > o.moveIndex) {
          nextDecisionIndexRef.current = o.moveIndex;
        }
        advanceFrontier();
        return;
      }

      if (o.status === "resolved" && o.result) {
        const result = o.result;
        const context = pendingAnalysisContextRef.current.get(o.requestId);
        // Snapshot every shouldRecordBlunder input at resolution time.
        const gameStore = useGameStore.getState();
        frontier.set(o.moveIndex, {
          requestId: o.requestId,
          status: "resolved",
          result,
          context,
          sessionIdSnapshot: gameStore.sessionId,
          isGameActiveSnapshot:
            gameStore.isGameActive && !gameStore.isPracticeContinuation,
        });
        // J1: resolved context is now snapshotted; drop the map entry.
        pendingAnalysisContextRef.current.delete(o.requestId);

        // SRS — immediate, request-targeted, NOT behind the frontier (I3/N2).
        processSrsReview(result);

        // Alert — latest-only microtask coalesce (c).
        if (
          result.blunder &&
          result.delta !== null &&
          result.moveIndex !== null &&
          isPlayerMoveIndex(result.moveIndex)
        ) {
          alertBufferRef.current.push({ moveIndex: result.moveIndex, result });
          scheduleAlert();
        }

        advanceFrontier();
        return;
      }

      // failed | skipped — terminal, unblock the frontier. Context for these is
      // RETAINED (L2) so a retry/replacement can migrate it.
      frontier.set(o.moveIndex, {
        requestId: o.requestId,
        status: o.status === "failed" ? "failed" : "skipped",
      });
      advanceFrontier();
    };

    const handleReset = (info: AnalysisResetInfo) => {
      if (info.fromMoveIndex === undefined) {
        // Full session-change reset.
        frontierRef.current.clear();
        pendingAnalysisContextRef.current.clear();
        nextDecisionIndexRef.current = 0;
        committedDecisionIndexRef.current = 0;
        currentGenerationRef.current = info.generation;
      } else {
        // Revert prune: drop slots >= k; rewind the display frontier; keep the
        // monotonic committed boundary (M2). Context/SRS pruning is done by the
        // lifecycle in the same synchronous turn.
        const k = info.fromMoveIndex;
        for (const idx of [...frontierRef.current.keys()]) {
          if (idx >= k) frontierRef.current.delete(idx);
        }
        if (nextDecisionIndexRef.current > k) {
          nextDecisionIndexRef.current = k;
        }
      }
      alertBufferRef.current = [];
      alertEpochRef.current += 1;
    };

    const unsubOutcome = coordinator.addAnalysisOutcomeListener(handleOutcome);
    const unsubReset = coordinator.addAnalysisResetListener(handleReset);
    return () => {
      unsubOutcome();
      unsubReset();
      // Unmount: bail any queued alert flush.
      alertEpochRef.current += 1;
    };
  }, [
    coordinator,
    advanceFrontier,
    processSrsReview,
    scheduleAlert,
    pendingAnalysisContextRef,
    pendingSrsReviewRef,
    setResolvedReview,
  ]);

  // Best-move bling sound for player moves. Uses the analysis store subscription
  // (lastAnalysis still drives board display + variation resolution) so it fires
  // for every resolveAnalysis even when React batches updates.
  useEffect(() => {
    const unsub = analysisStoreApi.subscribe((state, prev) => {
      if (state.lastAnalysis === prev.lastAnalysis) return;
      const la = state.lastAnalysis;
      if (!la || la.moveIndex === null || la.classification !== "best") return;
      const pc = useGameStore.getState().playerColor;
      const isWhite = la.moveIndex % 2 === 0;
      const isPlayer = pc === "white" ? isWhite : !isWhite;
      if (!isPlayer) return;
      playBling();
    });
    return unsub;
  }, [analysisStoreApi]);

  return null;
};

export default AnalysisEffects;
