import { useCallback, useEffect, useRef, useState } from "react";
import {
  getNextOpponentMove,
  type SessionDecisionSource,
  type TargetBlunderSrs,
} from "../utils/api";

export type OpponentMode = "ghost" | "engine";

export type OpponentMoveResult = {
  mode: OpponentMode;
  move: string;
  targetBlunderId: number | null;
  targetBlunderSrs: TargetBlunderSrs | null;
  targetFen: string | null;
  decisionSource: Exclude<SessionDecisionSource, "local_fallback">;
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
    };
  } catch (error) {
    console.error("[OpponentMove] Backend unavailable:", error);
    return null;
  }
};

type UseOpponentMoveOptions = {
  sessionId: string | null;
  canApplyResult?: (requestSessionId: string | null) => boolean;
  onApplyBackendMove: (
    sanMove: string,
    decisionSource: Exclude<SessionDecisionSource, "local_fallback">,
    targetBlunderId: number | null,
    targetBlunderSrs: TargetBlunderSrs | null,
    targetFen: string | null,
  ) => Promise<void>;
  onApplyLocalFallback: () => Promise<void>;
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
}: UseOpponentMoveOptions) => {
  const [opponentMode, setOpponentMode] = useState<OpponentMode>("engine");
  const canApplyResultRef = useRef(canApplyResult);
  const onApplyBackendMoveRef = useRef(onApplyBackendMove);
  const onApplyLocalFallbackRef = useRef(onApplyLocalFallback);

  useEffect(() => {
    canApplyResultRef.current = canApplyResult;
  }, [canApplyResult]);

  useEffect(() => {
    onApplyBackendMoveRef.current = onApplyBackendMove;
  }, [onApplyBackendMove]);

  useEffect(() => {
    onApplyLocalFallbackRef.current = onApplyLocalFallback;
  }, [onApplyLocalFallback]);

  const applyOpponentMove = useCallback(
    async (fen: string, moves: string[] = []) => {
      const requestSessionId = sessionId;

      if (!requestSessionId) {
        setOpponentMode("engine");
        await onApplyLocalFallbackRef.current();
        return;
      }

      const result = await determineOpponentMove(requestSessionId, fen, moves);

      if (
        canApplyResultRef.current &&
        !canApplyResultRef.current(requestSessionId)
      ) {
        return;
      }

      if (result) {
        setOpponentMode(result.mode);
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
        );
      } else {
        setOpponentMode("engine");
        await onApplyLocalFallbackRef.current();
      }
    },
    [sessionId]
  );

  const resetMode = useCallback(() => {
    setOpponentMode("engine");
  }, []);

  return {
    opponentMode,
    applyOpponentMove,
    resetMode,
  };
};
