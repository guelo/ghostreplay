import { useEffect, type Dispatch, type SetStateAction } from "react";
import type { ResolvedReview } from "./types";
import type { BlunderAlert } from "./domain/movePresentation";
import type { MoveMessage, SrsFailDetail } from "../MoveList";
import { useAnalysisStoreApi } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";
import { playBling } from "../../utils/blingSound";
import { playBuzzer } from "../../utils/buzzerSound";
import { playRandomBlunderAudio } from "./blunderAudio";
import {
  gameAnalysisCoordinator,
  type GameAnalysisCoordinator,
} from "../../services/GameAnalysisCoordinator";
import type { DecisionOwner } from "../../services/DecisionOwner";

/** The coordinator-owned recording/SRS decision owner is the permanent consumer
 *  of the outcome/reset channel (g-2m0p). AnalysisEffects only leases the UI
 *  surface onto it for as long as it is mounted. */
type DecisionOwnerHost = Pick<GameAnalysisCoordinator, "decisionOwner">;

type AnalysisEffectsProps = {
  appendMoveMessage: (moveIndex: number, msg: MoveMessage) => void;
  setBlunderAlert: Dispatch<SetStateAction<BlunderAlert | null>>;
  setShowFlash: Dispatch<SetStateAction<boolean>>;
  setResolvedReview: Dispatch<SetStateAction<ResolvedReview | null>>;
  onSrsFail: (detail: SrsFailDetail, moveIndex: number) => void;
  /** Defaults to the singleton coordinator; injectable for tests. */
  coordinator?: DecisionOwnerHost;
};

const AnalysisEffects = ({
  appendMoveMessage,
  setBlunderAlert,
  setShowFlash,
  setResolvedReview,
  onSrsFail,
  coordinator = gameAnalysisCoordinator,
}: AnalysisEffectsProps) => {
  const analysisStoreApi = useAnalysisStoreApi();

  // Lease the UI surface onto the coordinator-owned DecisionOwner for the mount
  // lifetime. Recording/SRS/outbox decisions run on the owner regardless of
  // mount; only the transient UI (alert flash/audio, resolved-review overlay,
  // move messages, onSrsFail) is gated on this lease.
  const decisionOwner: DecisionOwner = coordinator.decisionOwner;
  useEffect(() => {
    // A decision may have resolved durably while this component was unmounted,
    // leaving a frozen "pending" overlay. Clear it ONLY when the owner no longer
    // has that review pending — a still-pending review must keep its overlay so
    // the owner can transition it to pass/fail on resolution (otherwise the
    // analysisId would no longer match and the toast would never appear).
    setResolvedReview((prev) =>
      prev?.result === "pending" && !decisionOwner.hasPendingReview(prev.analysisId)
        ? null
        : prev,
    );
    return decisionOwner.registerUICallbacks({
      appendMoveMessage,
      setBlunderAlert: (alert) => setBlunderAlert(alert),
      setShowFlash: (show) => setShowFlash(show),
      setResolvedReview,
      onSrsFail,
      playBuzzer,
      playBlunderAudio: playRandomBlunderAudio,
    });
  }, [
    decisionOwner,
    appendMoveMessage,
    setBlunderAlert,
    setShowFlash,
    setResolvedReview,
    onSrsFail,
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
