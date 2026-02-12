import { useCallback, useState } from "react";
import { getNextOpponentMove } from "../utils/api";

export type OpponentMode = "ghost" | "engine";

export type OpponentMoveResult = {
  mode: OpponentMode;
  move: string;
  targetBlunderId: number | null;
};

/**
 * Queries the unified backend endpoint for the next opponent move.
 * Returns the move on success, or null on network/server error
 * so the caller can fall back to the local engine.
 */
export const determineOpponentMove = async (
  sessionId: string,
  fen: string
): Promise<OpponentMoveResult | null> => {
  try {
    const response = await getNextOpponentMove(sessionId, fen);
    return {
      mode: response.mode,
      move: response.move.san,
      targetBlunderId: response.target_blunder_id,
    };
  } catch (error) {
    console.error("[OpponentMove] Backend unavailable:", error);
    return null;
  }
};

type UseOpponentMoveOptions = {
  sessionId: string | null;
  onApplyBackendMove: (
    sanMove: string,
    targetBlunderId: number | null
  ) => Promise<void>;
  onApplyLocalFallback: () => Promise<void>;
};

/**
 * Hook that manages opponent move selection via the unified backend endpoint.
 * Falls back to the local Stockfish engine on network errors.
 */
export const useOpponentMove = ({
  sessionId,
  onApplyBackendMove,
  onApplyLocalFallback,
}: UseOpponentMoveOptions) => {
  const [opponentMode, setOpponentMode] = useState<OpponentMode>("engine");

  const applyOpponentMove = useCallback(
    async (fen: string) => {
      if (!sessionId) {
        setOpponentMode("engine");
        await onApplyLocalFallback();
        return;
      }

      const result = await determineOpponentMove(sessionId, fen);

      if (result) {
        setOpponentMode(result.mode);
        console.log(
          `[OpponentMove] Applying ${result.mode} move:`,
          result.move
        );
        await onApplyBackendMove(result.move, result.targetBlunderId);
      } else {
        setOpponentMode("engine");
        await onApplyLocalFallback();
      }
    },
    [sessionId, onApplyBackendMove, onApplyLocalFallback]
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
