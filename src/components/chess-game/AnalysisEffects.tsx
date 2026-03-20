import { useEffect, type Dispatch, type MutableRefObject, type SetStateAction } from "react";
import { Chess } from "chess.js";
import type { TargetBlunderSrs } from "../../utils/api";
import {
  recordBlunder,
  reviewSrsBlunder,
} from "../../utils/api";
import { shouldRecordBlunder } from "../../utils/blunder";
import { isRecordableFailure } from "../../workers/analysisUtils";
import type { BlunderAlert } from "./domain/movePresentation";
import type { MoveMessage } from "../MoveList";
import {
  BLUNDER_AUDIO_CLIPS,
  STARTING_FEN,
} from "./config";
import { useAnalysisStore } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";

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
  srs: TargetBlunderSrs | null;
};

type AnalysisEffectsProps = {
  pendingAnalysisContextRef: MutableRefObject<PendingAnalysisContext | null>;
  blunderRecordedRef: MutableRefObject<boolean>;
  pendingSrsReviewRef: MutableRefObject<PendingSrsReview | null>;
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
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
}: AnalysisEffectsProps) => {
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
    if (!pendingReview || pendingReview.moveIndex !== lastAnalysis.moveIndex) {
      return;
    }

    pendingSrsReviewRef.current = null;

    if (lastAnalysis.delta === null) {
      return;
    }

    const evalLossCp = Math.max(lastAnalysis.delta, 0);
    const passed = !isRecordableFailure(evalLossCp);

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
      let bestMoveSan = lastAnalysis.bestMove;
      const fenBeforeMove =
        lastAnalysis.moveIndex === 0
          ? STARTING_FEN
          : useGameStore.getState().moveHistory[lastAnalysis.moveIndex - 1]
              ?.fen;
      if (fenBeforeMove) {
        try {
          const tempChess = new Chess(fenBeforeMove);
          const from = lastAnalysis.bestMove.slice(0, 2);
          const to = lastAnalysis.bestMove.slice(2, 4);
          const promotion = lastAnalysis.bestMove.slice(4) || undefined;
          const bestMoveResult = tempChess.move({ from, to, promotion });
          if (bestMoveResult) {
            bestMoveSan = bestMoveResult.san;
          }
        } catch {
          // Fall back to UCI notation
        }
      }

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
  }, [isGameActive, lastAnalysis, sessionId, pendingSrsReviewRef, appendMoveMessage]);

  // Blunder alert: show flash + toast + arrows for player blunders
  useEffect(() => {
    if (
      !lastAnalysis?.blunder ||
      lastAnalysis.delta === null ||
      lastAnalysis.moveIndex === null
    ) {
      return;
    }

    if (lastAnalysis.moveIndex === 0) {
      return;
    }

    if (!isPlayerMoveIndex(lastAnalysis.moveIndex)) {
      return;
    }

    const moveHistory = useGameStore.getState().moveHistory;
    const moveSan =
      moveHistory[lastAnalysis.moveIndex]?.san ?? lastAnalysis.move;

    let bestMoveSan = lastAnalysis.bestMove;
    try {
      const fenBeforeMove =
        lastAnalysis.moveIndex === 0
          ? STARTING_FEN
          : moveHistory[lastAnalysis.moveIndex - 1]?.fen;
      if (fenBeforeMove) {
        const tempChess = new Chess(fenBeforeMove);
        const from = lastAnalysis.bestMove.slice(0, 2);
        const to = lastAnalysis.bestMove.slice(2, 4);
        const promotion = lastAnalysis.bestMove.slice(4) || undefined;
        const bestMoveResult = tempChess.move({ from, to, promotion });
        if (bestMoveResult) {
          bestMoveSan = bestMoveResult.san;
        }
      }
    } catch {
      // Fall back to UCI notation
    }

    setBlunderAlert({
      moveSan,
      moveUci: lastAnalysis.move,
      bestMoveUci: lastAnalysis.bestMove,
      bestMoveSan,
      delta: lastAnalysis.delta,
    });
    setShowFlash(true);
    playRandomBlunderAudio();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastAnalysis, playerColor, setBlunderAlert, setShowFlash]);

  return null;
};

export default AnalysisEffects;
