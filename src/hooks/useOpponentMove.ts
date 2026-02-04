import { useCallback, useState } from "react";
import { getGhostMove } from "../utils/api";

export type OpponentMode = "ghost" | "engine";

export type OpponentMoveResult = {
  mode: OpponentMode;
  move: string | null;
};

/**
 * Determines the opponent's move by querying the ghost-move endpoint.
 * Returns ghost move if available, otherwise signals to use engine.
 */
export const determineOpponentMove = async (
  sessionId: string,
  fen: string
): Promise<OpponentMoveResult> => {
  try {
    const response = await getGhostMove(sessionId, fen);
    return { mode: response.mode, move: response.move };
  } catch (error) {
    console.error("[GhostMove] Failed to get ghost move:", error);
    return { mode: "engine", move: null };
  }
};

type UseOpponentMoveOptions = {
  sessionId: string | null;
  onApplyGhostMove: (sanMove: string) => Promise<void>;
  onApplyEngineMove: () => Promise<void>;
};

/**
 * Hook that manages opponent move selection between ghost and engine modes.
 */
export const useOpponentMove = ({
  sessionId,
  onApplyGhostMove,
  onApplyEngineMove,
}: UseOpponentMoveOptions) => {
  const [opponentMode, setOpponentMode] = useState<OpponentMode>("engine");

  const applyOpponentMove = useCallback(
    async (fen: string) => {
      if (!sessionId) {
        setOpponentMode("engine");
        await onApplyEngineMove();
        return;
      }

      const result = await determineOpponentMove(sessionId, fen);
      setOpponentMode(result.mode);

      if (result.mode === "ghost" && result.move) {
        console.log("[GhostMove] Applying ghost move:", result.move);
        await onApplyGhostMove(result.move);
      } else {
        await onApplyEngineMove();
      }
    },
    [sessionId, onApplyGhostMove, onApplyEngineMove]
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
