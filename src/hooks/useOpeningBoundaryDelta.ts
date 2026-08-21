import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import type { OpeningBoundarySource } from "../services/GameAnalysisCoordinator";
import { useGameStore } from "../stores/useGameStore";
import { captureEvent } from "../analytics/posthog";
import {
  abortOpeningBoundaryDeltaPoll,
  pollFreshOpeningDelta,
} from "../utils/openingDeltaPoll";

/**
 * Reconcile a coordinator-proven live opening boundary into the existing score
 * card slot and compare it with the browser-derived graph marker. The
 * coordinator owns proof/revision fencing; this hook owns the boundary poll
 * lifetime, boundary-scoped store cleanup, and mismatch diagnostic.
 */
export function useOpeningBoundaryDelta(
  coordinator: OpeningBoundarySource,
  sessionId: string | null,
  isDrillMode: boolean,
  browserOpeningMiddlePly?: number | null,
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
  const reportedMismatchRef = useRef<string | null>(null);

  useEffect(() => {
    if (browserOpeningMiddlePly === undefined) return;
    const boundary = coordinator.getOpeningBoundarySnapshot(sessionId);
    if (!boundary) {
      reportedMismatchRef.current = null;
      return;
    }
    if (browserOpeningMiddlePly === boundary.openingMiddlePly) {
      reportedMismatchRef.current = null;
      return;
    }

    const mismatchKey = [
      boundary.sessionId,
      boundary.openingMiddlePly,
      browserOpeningMiddlePly ?? "unknown",
    ].join(":");
    if (reportedMismatchRef.current === mismatchKey) return;
    reportedMismatchRef.current = mismatchKey;
    captureEvent("opening_boundary_client_mismatch", {
      browser_opening_ply: browserOpeningMiddlePly,
      server_opening_ply: boundary.openingMiddlePly,
      boundary_transition_revision: boundary.transitionRevision,
      is_drill_mode: isDrillMode,
    });
  }, [
    browserOpeningMiddlePly,
    coordinator,
    isDrillMode,
    revision,
    sessionId,
  ]);

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
