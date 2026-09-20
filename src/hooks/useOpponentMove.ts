import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  deriveOpponentPresentation,
  type ApplyOpponentMoveOutcome,
  type LastCommittedOpponentDecision,
  type OpponentMode,
  type OpponentMoveCommittedObserver,
} from "../components/chess-game/domain/opponentPresentation";
import {
  getNextOpponentMove,
  type DrillRouteMetadata,
  type SessionDecisionSource,
  type TargetBlunderSrs,
} from "../utils/api";

export type OpponentMoveResult = {
  mode: OpponentMode;
  move: string;
  targetBlunderId: number | null;
  targetBlunderSrs: TargetBlunderSrs | null;
  targetFen: string | null;
  decisionSource: Exclude<SessionDecisionSource, "local_fallback">;
  drillRoute: DrillRouteMetadata | null;
  /** The decision row this move was served from — the id a root confirmation names. */
  decisionId: string | null;
};

/**
 * Queries the unified backend endpoint for the next opponent move.
 * Returns the move on success, or null on network/server error
 * so the caller can fall back to the local engine.
 */
export const determineOpponentMove = async (
  sessionId: string,
  fen: string,
  moves: string[] = [],
  onFailure?: (error: unknown) => void,
): Promise<OpponentMoveResult | null> => {
  try {
    const response = await getNextOpponentMove(sessionId, fen, moves);
    return {
      mode: response.mode,
      move: response.move.san,
      targetBlunderId: response.target_blunder_id,
      targetBlunderSrs: response.target_blunder_srs,
      targetFen: response.target_fen,
      decisionSource: response.decision_source,
      drillRoute: response.drill_route ?? null,
      decisionId: response.decision_id ?? null,
    };
  } catch (error) {
    console.error("[OpponentMove] Backend unavailable:", error);
    onFailure?.(error);
    return null;
  }
};

type UseOpponentMoveOptions = {
  sessionId: string | null;
  canApplyResult?: (requestSessionId: string | null, fen?: string) => boolean;
  onApplyBackendMove: (
    sanMove: string,
    decisionSource: Exclude<SessionDecisionSource, "local_fallback">,
    targetBlunderId: number | null,
    targetBlunderSrs: TargetBlunderSrs | null,
    targetFen: string | null,
    drillRoute: DrillRouteMetadata | null,
    decisionId: string | null,
    onCommitted: OpponentMoveCommittedObserver,
  ) => Promise<ApplyOpponentMoveOutcome>;
  onApplyLocalFallback: (
    onCommitted: OpponentMoveCommittedObserver,
  ) => Promise<ApplyOpponentMoveOutcome>;
  shouldUseLocalFallback?: () => boolean;
  onBackendFailure?: (error?: unknown) => Promise<void>;
};

/**
 * Hook that manages opponent move selection via the unified backend endpoint.
 * Falls back to the local Stockfish engine on network errors.
 */
export const useOpponentMove = ({
  sessionId,
  canApplyResult,
  onApplyBackendMove,
  onApplyLocalFallback,
  shouldUseLocalFallback,
  onBackendFailure,
}: UseOpponentMoveOptions) => {
  const [lastCommittedDecision, setLastCommittedDecision] =
    useState<LastCommittedOpponentDecision | null>(null);
  const canApplyResultRef = useRef(canApplyResult);
  const onApplyBackendMoveRef = useRef(onApplyBackendMove);
  const onApplyLocalFallbackRef = useRef(onApplyLocalFallback);
  const shouldUseLocalFallbackRef = useRef(shouldUseLocalFallback);
  const onBackendFailureRef = useRef(onBackendFailure);

  useEffect(() => {
    canApplyResultRef.current = canApplyResult;
  }, [canApplyResult]);

  useEffect(() => {
    onApplyBackendMoveRef.current = onApplyBackendMove;
  }, [onApplyBackendMove]);

  useEffect(() => {
    onApplyLocalFallbackRef.current = onApplyLocalFallback;
  }, [onApplyLocalFallback]);

  useEffect(() => {
    shouldUseLocalFallbackRef.current = shouldUseLocalFallback;
  }, [shouldUseLocalFallback]);

  useEffect(() => {
    onBackendFailureRef.current = onBackendFailure;
  }, [onBackendFailure]);

  const createCommitObserver = useCallback(
    (
      decision: Omit<LastCommittedOpponentDecision, "drill">,
    ): OpponentMoveCommittedObserver => {
      let published = false;
      return (context) => {
        if (published) return;
        published = true;
        setLastCommittedDecision({ ...decision, drill: context.drill });
      };
    },
    [],
  );

  const applyOpponentMove = useCallback(
    async (fen: string, moves: string[] = []) => {
      const requestSessionId = sessionId;

      if (!requestSessionId) {
        if (
          canApplyResultRef.current &&
          !canApplyResultRef.current(requestSessionId, fen)
        ) {
          return;
        }
        await onApplyLocalFallbackRef.current(
          createCommitObserver({
            mode: "engine",
            decisionSource: "local_fallback",
            targetBlunderId: null,
            targetBlunderSrs: null,
            targetFen: null,
            drillRoute: null,
            decisionId: null,
          }),
        );
        return;
      }

      // Check before dispatch too: expired drills must not retry on remount.
      if (canApplyResultRef.current && !canApplyResultRef.current(requestSessionId, fen)) return;
      let failure: unknown;
      const result = await determineOpponentMove(requestSessionId, fen, moves, (error) => {
        failure = error;
      });

      if (
        canApplyResultRef.current &&
        !canApplyResultRef.current(requestSessionId, fen)
      ) {
        return;
      }

      if (result) {
        console.log(
          `[OpponentMove] Applying ${result.mode} move:`,
          result.move
        );
        await onApplyBackendMoveRef.current(
          result.move,
          result.decisionSource,
          result.targetBlunderId,
          result.targetBlunderSrs,
          result.targetFen,
          result.drillRoute,
          result.decisionId,
          createCommitObserver({
            mode: result.mode,
            decisionSource: result.decisionSource,
            targetBlunderId: result.targetBlunderId,
            targetBlunderSrs: result.targetBlunderSrs,
            targetFen: result.targetFen,
            drillRoute: result.drillRoute,
            decisionId: result.decisionId,
          }),
        );
      } else {
        if (shouldUseLocalFallbackRef.current?.() ?? true) {
          await onApplyLocalFallbackRef.current(
            createCommitObserver({
              mode: "engine",
              decisionSource: "local_fallback",
              targetBlunderId: null,
              targetBlunderSrs: null,
              targetFen: null,
              drillRoute: null,
              decisionId: null,
            }),
          );
        } else {
          await onBackendFailureRef.current?.(failure);
        }
      }
    },
    [createCommitObserver, sessionId],
  );

  const resetPresentation = useCallback(() => {
    setLastCommittedDecision(null);
  }, []);

  const opponentPresentation = useMemo(
    () => deriveOpponentPresentation(lastCommittedDecision),
    [lastCommittedDecision],
  );

  return {
    opponentPresentation,
    applyOpponentMove,
    resetPresentation,
  };
};
