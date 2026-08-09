import { useCallback, useSyncExternalStore } from "react";
import type { UploadCommitSource } from "../services/GameAnalysisCoordinator";

/**
 * React adapter for the coordinator's session-scoped incremental-upload
 * revision. The coordinator remains the source of truth; this hook only turns
 * its synchronous snapshot/subscription pair into a render dependency.
 */
export function useSessionUploadCommitRevision(
  coordinator: UploadCommitSource,
  sessionId: string | null,
): number {
  const subscribe = useCallback(
    (listener: () => void) => coordinator.addUploadCommitListener(listener),
    [coordinator],
  );
  const getSnapshot = useCallback(
    () => coordinator.getUploadCommitRevision(sessionId),
    [coordinator, sessionId],
  );

  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
