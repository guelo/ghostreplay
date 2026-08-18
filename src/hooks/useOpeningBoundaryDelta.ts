import { useCallback, useEffect, useSyncExternalStore } from "react";
import type { OpeningBoundarySource } from "../services/GameAnalysisCoordinator";
import { useGameStore } from "../stores/useGameStore";
import {
  abortOpeningBoundaryDeltaPoll,
  pollFreshOpeningDelta,
} from "../utils/openingDeltaPoll";

/**
 * Reconcile a coordinator-proven live opening boundary into the existing score
 * card slot. The coordinator owns proof/revision fencing; this hook owns only
 * the boundary poll lifetime and boundary-scoped store cleanup.
 */
export function useOpeningBoundaryDelta(
  coordinator: OpeningBoundarySource,
  sessionId: string | null,
  isDrillMode: boolean,
): void {
  const subscribe = useCallback(
    (listener: () => void) => coordinator.addOpeningBoundaryListener(listener),
    [coordinator],
  );
  const getRevision = useCallback(
    () => coordinator.getOpeningBoundaryRevision(sessionId),
    [coordinator, sessionId],
  );
  const revision = useSyncExternalStore(subscribe, getRevision, getRevision);

  useEffect(() => {
    const boundary = coordinator.getOpeningBoundarySnapshot(sessionId);
    if (!boundary) return;
    const token = boundary.reconciliationToken;
    void pollFreshOpeningDelta(
      boundary.sessionId,
      isDrillMode
        ? "drill_opening_boundary"
        : "game_opening_boundary",
      { boundaryToken: token },
    );

    return () => {
      abortOpeningBoundaryDeltaPoll(boundary.sessionId, token);
      useGameStore
        .getState()
        .clearBoundaryOpeningDelta(boundary.sessionId, token);
    };
  }, [coordinator, isDrillMode, revision, sessionId]);
}
