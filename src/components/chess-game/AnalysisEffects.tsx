import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import type { TargetBlunderSrs } from "../../utils/api";
import type { ResolvedReview } from "./types";
import {
  recordBlunder,
  reviewSrsBlunder,
} from "../../utils/api";
import { shouldRecordBlunder } from "../../utils/blunder";
import { isRecordableFailure } from "../../workers/analysisUtils";
import {
  buildBlunderAlert,
  fenBeforeMove,
  sanForUciMove,
  type BlunderAlert,
} from "./domain/movePresentation";
import type { MoveMessage } from "../MoveList";
import { BLUNDER_AUDIO_CLIPS } from "./config";
import { useAnalysisStore, useAnalysisStoreApi } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";
import { playBling } from "../../utils/blingSound";

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

type AnalysisEffectsProps = {
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
};

const playRandomBlunderAudio = () => {
  if (typeof Audio === "undefined" || BLUNDER_AUDIO_CLIPS.length === 0) {
    return;
  }
  const randomIndex = Math.floor(Math.random() * BLUNDER_AUDIO_CLIPS.length);
  const clip = BLUNDER_AUDIO_CLIPS[randomIndex];
  const audio = new Audio(clip);
  void audio.play().catch(() => {});
};

const AnalysisEffects = ({
  pendingAnalysisContextRef,
  blunderRecordedRef,
  pendingSrsReviewRef,
  appendMoveMessage,
  setBlunderAlert,
  setShowFlash,
  setResolvedReview,
}: AnalysisEffectsProps) => {
  const analysisStoreApi = useAnalysisStoreApi();
  const lastAnalysis = useAnalysisStore((s) => s.lastAnalysis);
  const sessionId = useGameStore((s) => s.sessionId);
  const isGameActive = useGameStore((s) => s.isGameActive);
  const playerColor = useGameStore((s) => s.playerColor);

  const isPlayerMoveIndex = (index: number) => {
    if (index < 0) return false;
    const isWhiteMove = index % 2 === 0;
    return playerColor === "white" ? isWhiteMove : !isWhiteMove;
  };

  // Blunder detection: POST /api/blunder on first blunder this session
  useEffect(() => {
    const blunderData = shouldRecordBlunder({
      analysis: lastAnalysis,
      context: pendingAnalysisContextRef.current,
      sessionId,
      isGameActive,
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
  }, [lastAnalysis, sessionId, isGameActive, pendingAnalysisContextRef, blunderRecordedRef]);

  // SRS review grading: evaluate user move from a targeted blunder position.
  useEffect(() => {
    if (
      !sessionId ||
      !isGameActive ||
      !lastAnalysis ||
      lastAnalysis.moveIndex === null
    ) {
      return;
    }

    const pendingReview = pendingSrsReviewRef.current;
    if (
      !pendingReview ||
      pendingReview.analysisId !== lastAnalysis.id ||
      pendingReview.moveIndex !== lastAnalysis.moveIndex
    ) {
      return;
    }

    pendingSrsReviewRef.current = null;

    if (lastAnalysis.delta === null) {
      return;
    }

    const evalLossCp = Math.max(lastAnalysis.delta, 0);
    const passed = !isRecordableFailure(evalLossCp);

    // Only update the overlay if it's still the pending review for this move.
    // Use functional update to avoid adding resolvedReview to deps (which would
    // cause this effect to re-fire on the pending→pass/fail transition itself,
    // potentially skipping the pending state if analysis returned quickly).
    setResolvedReview((prev) =>
      prev?.analysisId === lastAnalysis.id
        ? {
            analysisId: lastAnalysis.id,
            moveIndex: lastAnalysis.moveIndex!,
            result: passed ? "pass" : "fail",
          }
        : prev,
    );

    if (passed) {
      const srs = pendingReview.srs;
      appendMoveMessage(lastAnalysis.moveIndex, {
        key: `srs-${lastAnalysis.moveIndex}`,
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
        lastAnalysis.moveIndex,
      );
      const bestMoveSan = sanForUciMove(sourceFen, lastAnalysis.bestMove);

      const srs = pendingReview.srs;
      appendMoveMessage(lastAnalysis.moveIndex, {
        key: `srs-${lastAnalysis.moveIndex}`,
        text: "You made this mistake again!",
        variant: "srs-fail",
        srsFailDetail: {
          userMoveSan: pendingReview.userMoveSan,
          bestMoveSan,
          userMoveUci: lastAnalysis.move,
          bestMoveUci: lastAnalysis.bestMove,
        },
        srsStats: srs
          ? {
              passCount: srs.pass_count,
              failCount: srs.fail_count + 1,
              streak: 0,
            }
          : undefined,
      });
    }

    const postReview = async () => {
      try {
        await reviewSrsBlunder(
          sessionId,
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
  }, [isGameActive, lastAnalysis, sessionId, pendingSrsReviewRef, appendMoveMessage, setResolvedReview]);

  // Blunder alert: show flash + toast + arrows for player blunders
  useEffect(() => {
    if (
      !lastAnalysis?.blunder ||
      lastAnalysis.delta === null ||
      lastAnalysis.moveIndex === null
    ) {
      return;
    }

    if (!isPlayerMoveIndex(lastAnalysis.moveIndex)) {
      return;
    }

    const moveHistory = useGameStore.getState().moveHistory;
    const moveSan =
      moveHistory[lastAnalysis.moveIndex]?.san ?? lastAnalysis.move;
    setBlunderAlert(
      buildBlunderAlert({
        moveHistory,
        moveIndex: lastAnalysis.moveIndex,
        moveSan,
        moveUci: lastAnalysis.move,
        bestMoveUci: lastAnalysis.bestMove,
        delta: lastAnalysis.delta,
        shouldRewind: true,
      }),
    );
    setShowFlash(true);
    playRandomBlunderAudio();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastAnalysis, playerColor, setBlunderAlert, setShowFlash]);

  // Best-move bling sound for player moves.
  // Uses zustand subscribe (not a React effect) so it fires for every
  // resolveAnalysis call even when React batches multiple updates.
  useEffect(() => {
    const unsub = analysisStoreApi.subscribe((state, prev) => {
      if (state.lastAnalysis === prev.lastAnalysis) return;
      const la = state.lastAnalysis;
      if (!la || la.moveIndex === null || la.classification !== "best") return;
      // Read playerColor directly from the game store (not a ref) to avoid
      // stale reads when analysis arrives before React re-renders with the
      // new playerColor (e.g. on game start).
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
