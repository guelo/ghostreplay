import { memo, useCallback, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import type { Dispatch, SetStateAction } from "react";
import { useAnalysisStore, useAnalysisStoreApi } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";
import { toWhitePerspective } from "../../workers/analysisUtils";
import {
  deriveAnnotatedMoves,
  type BlunderAlert,
  type ReviewFailInfo,
} from "./domain/movePresentation";
import { recordManualBlunder } from "../../utils/api";
import { STARTING_FEN } from "./config";
import EvalBar from "../EvalBar";
import AnalysisGraph from "../AnalysisGraph";
import MoveList from "../MoveList";
import type { MoveMessage, SrsFailDetail } from "../MoveList";

// ---------------------------------------------------------------------------
// ConnectedEvalBar
// ---------------------------------------------------------------------------

export const ConnectedEvalBar = memo(() => {
  const analysisMap = useAnalysisStore((s) => s.analysisMap);
  const moveHistory = useGameStore((s) => s.moveHistory);
  const viewIndex = useGameStore((s) => s.viewIndex);
  const boardOrientation = useGameStore((s) => s.boardOrientation);
  const selectedMoveIndex =
    moveHistory.length === 0 ? null : (viewIndex ?? moveHistory.length - 1);

  const selectedEvalCp = useMemo(() => {
    if (selectedMoveIndex === null || selectedMoveIndex < 0) {
      return null;
    }
    for (let idx = selectedMoveIndex; idx >= 0; idx -= 1) {
      const analysis = analysisMap.get(idx);
      if (analysis?.playedEval == null) continue;
      return toWhitePerspective(analysis.playedEval, idx);
    }
    return null;
  }, [analysisMap, selectedMoveIndex]);

  return (
    <EvalBar
      whitePerspectiveCp={selectedEvalCp}
      whiteOnBottom={boardOrientation === "white"}
    />
  );
});
ConnectedEvalBar.displayName = "ConnectedEvalBar";

// ---------------------------------------------------------------------------
// ConnectedAnalysisGraph
// ---------------------------------------------------------------------------

type ConnectedAnalysisGraphProps = {
  onSelectMove: (index: number) => void;
};

export const ConnectedAnalysisGraph = memo(
  ({ onSelectMove }: ConnectedAnalysisGraphProps) => {
    const analysisMap = useAnalysisStore((s) => s.analysisMap);
    const streamingEval = useAnalysisStore((s) => s.streamingEval);
    const moveHistory = useGameStore((s) => s.moveHistory);
    const viewIndex = useGameStore((s) => s.viewIndex);
    const playerColor = useGameStore((s) => s.playerColor);

    const selectedMoveIndex =
      moveHistory.length === 0 ? null : (viewIndex ?? moveHistory.length - 1);

    const evals = useMemo(() => {
      const raw = moveHistory.map((_, i) => {
        const a = analysisMap.get(i);
        return a?.playedEval != null
          ? toWhitePerspective(a.playedEval, i)
          : null;
      });
      let end = raw.length;
      while (end > 0 && raw[end - 1] === null) end--;
      return raw.slice(0, end);
    }, [moveHistory, analysisMap]);

    const pendingIndices = useMemo(() => {
      const pending: number[] = [];
      for (let i = 0; i < moveHistory.length; i++) {
        if (!analysisMap.has(i)) pending.push(i);
      }
      return pending;
    }, [moveHistory, analysisMap]);

    const selectedEvalCp = useMemo(() => {
      if (selectedMoveIndex === null || selectedMoveIndex < 0) {
        return null;
      }
      for (let idx = selectedMoveIndex; idx >= 0; idx -= 1) {
        const analysis = analysisMap.get(idx);
        if (analysis?.playedEval == null) continue;
        return toWhitePerspective(analysis.playedEval, idx);
      }
      return null;
    }, [analysisMap, selectedMoveIndex]);

    const graphStreamingEval = useMemo(() => {
      if (!streamingEval) return null;
      return {
        index: streamingEval.moveIndex,
        cp:
          toWhitePerspective(streamingEval.cp, streamingEval.moveIndex) ?? 0,
      };
    }, [streamingEval]);

    if (!evals.some((e) => e !== null) && pendingIndices.length === 0) {
      return null;
    }

    return (
      <AnalysisGraph
        evals={evals}
        currentIndex={selectedMoveIndex}
        onSelectMove={onSelectMove}
        playerColor={playerColor}
        evalCp={selectedEvalCp}
        streamingEval={graphStreamingEval}
        pendingIndices={pendingIndices}
      />
    );
  },
);
ConnectedAnalysisGraph.displayName = "ConnectedAnalysisGraph";

// ---------------------------------------------------------------------------
// ConnectedMoveList
// ---------------------------------------------------------------------------

type ConnectedMoveListProps = {
  onNavigate: (index: number | null) => void;
  messages: ReadonlyMap<number, MoveMessage[]>;
  onRevealSrsFail: (detail: SrsFailDetail, moveIndex: number) => void;
  revealedSrsFailIndex: number | null;
};

export const ConnectedMoveList = memo(
  ({
    onNavigate,
    messages,
    onRevealSrsFail,
    revealedSrsFailIndex,
  }: ConnectedMoveListProps) => {
    const analysisStoreApi = useAnalysisStoreApi();
    const analysisMap = useAnalysisStore((s) => s.analysisMap);
    const moveHistory = useGameStore((s) => s.moveHistory);
    const viewIndex = useGameStore((s) => s.viewIndex);
    const playerColor = useGameStore((s) => s.playerColor);
    const isGameActive = useGameStore((s) => s.isGameActive);
    const sessionId = useGameStore((s) => s.sessionId);

    const selectedMoveIndex =
      moveHistory.length === 0 ? null : (viewIndex ?? moveHistory.length - 1);

    const isPlayerMoveIndex = useCallback(
      (index: number) => {
        if (index < 0) return false;
        const isWhiteMove = index % 2 === 0;
        return playerColor === "white" ? isWhiteMove : !isWhiteMove;
      },
      [playerColor],
    );

    const prevAnnotatedRef = useRef<
      { san: string; classification?: string | null; eval?: number | null }[]
    >([]);

    const annotatedMoves = useMemo(() => {
      const fresh = deriveAnnotatedMoves(moveHistory, analysisMap);
      const prev = prevAnnotatedRef.current;
      const stable = fresh.map((item, i) => {
        const old = prev[i];
        if (
          old &&
          old.san === item.san &&
          old.classification === item.classification &&
          old.eval === item.eval
        ) {
          return old;
        }
        return item;
      });
      prevAnnotatedRef.current = stable;
      return stable;
    }, [moveHistory, analysisMap]);

    const analyzingIndices = useMemo(() => {
      if (!isGameActive) return new Set<number>();
      const pending = new Set<number>();
      for (let i = 0; i < moveHistory.length; i++) {
        if (!analysisMap.has(i)) pending.add(i);
      }
      return pending;
    }, [isGameActive, moveHistory.length, analysisMap]);

    const canAddSelectedMove = useMemo(() => {
      if (!sessionId || selectedMoveIndex === null) return false;
      return isPlayerMoveIndex(selectedMoveIndex);
    }, [sessionId, selectedMoveIndex, isPlayerMoveIndex]);

    const [isAddingToLibrary, setIsAddingToLibrary] = useState(false);

    const handleAddSelectedMove = useCallback(
      async (moveIndex: number) => {
        if (!sessionId || !isPlayerMoveIndex(moveIndex)) return;

        const history = useGameStore.getState().moveHistory;
        const analysis = analysisStoreApi.getState().analysisMap;

        if (moveIndex < 0 || moveIndex >= history.length) return;

        const preMoveFen =
          moveIndex === 0 ? STARTING_FEN : history[moveIndex - 1]?.fen;
        if (!preMoveFen) return;

        const replay = new Chess();
        for (let i = 0; i <= moveIndex; i += 1) {
          const applied = replay.move(history[i].san);
          if (!applied) return;
        }

        const a = analysis.get(moveIndex);
        const userMove = history[moveIndex].san;

        setIsAddingToLibrary(true);
        try {
          await recordManualBlunder(
            sessionId,
            replay.pgn(),
            preMoveFen,
            userMove,
            a?.bestMove ?? userMove,
            a?.bestEval ?? 0,
            a?.playedEval ?? a?.bestEval ?? 0,
          );
        } catch (error) {
          console.error(
            "[BlunderLibrary] Failed to record manual blunder:",
            error,
          );
        } finally {
          setIsAddingToLibrary(false);
        }
      },
      [analysisStoreApi, isPlayerMoveIndex, sessionId],
    );

    return (
      <MoveList
        moves={annotatedMoves}
        currentIndex={viewIndex}
        onNavigate={onNavigate}
        canAddSelectedMove={canAddSelectedMove}
        isAddingSelectedMove={isAddingToLibrary}
        onAddSelectedMove={handleAddSelectedMove}
        messages={messages}
        analyzingIndices={analyzingIndices}
        playerColor={playerColor}
        onRevealSrsFail={onRevealSrsFail}
        revealedSrsFailIndex={revealedSrsFailIndex}
      />
    );
  },
);
ConnectedMoveList.displayName = "ConnectedMoveList";
