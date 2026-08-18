import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Chess } from "chess.js";
import { useAnalysisStore, useAnalysisStoreApi } from "../../stores/createAnalysisStore";
import { useGameStore } from "../../stores/useGameStore";
import { moverMateToWhiteCp, toWhitePerspective, toWhitePerspectiveMate } from "../../workers/analysisUtils";
import { isCheckmateFen, whiteCpForMove } from "../../utils/moveGraphEval";
import {
  deriveAnnotatedMoves,
} from "./domain/movePresentation";
import { recordManualBlunder } from "../../utils/api";
import { STARTING_FEN } from "./config";
import EvalBar from "../EvalBar";
import AnalysisGraph from "../AnalysisGraph";
import MoveList from "../MoveList";
import HorizontalMoveList from "../HorizontalMoveList";
import { useMediaQuery } from "../../hooks/useMediaQuery";
import { GAME_MOBILE_QUERY } from "../../styles/breakpoints";
import type { MoveMessage, SrsFailDetail } from "../MoveList";

// Walk back from selectedMoveIndex to the most recent move with a played eval
// and return it in white's perspective, or null when none is available.
function selectedEvalFromMap(
  analysisMap: ReadonlyMap<
    number,
    { playedEval?: number | null; playedEvalMate?: number | null }
  >,
  selectedMoveIndex: number | null,
  selectedFen: string | null,
): { cp: number | null; mate: number | null } {
  if (selectedMoveIndex === null || selectedMoveIndex < 0) {
    return { cp: null, mate: null };
  }
  const selected = analysisMap.get(selectedMoveIndex);
  // Terminal checkmate with no stored eval: the winner is known from the FEN, so
  // peg to it (extreme ±cp + mate 0) instead of inheriting a prior move's eval.
  // Gated on the selected move's FEN because a numeric eval must always win.
  if (
    selected?.playedEval == null &&
    selected?.playedEvalMate == null &&
    isCheckmateFen(selectedFen)
  ) {
    return { cp: moverMateToWhiteCp(0, selectedMoveIndex), mate: 0 };
  }
  for (let idx = selectedMoveIndex; idx >= 0; idx -= 1) {
    const analysis = analysisMap.get(idx);
    if (analysis?.playedEval == null && analysis?.playedEvalMate == null) continue;
    const mate = toWhitePerspectiveMate(analysis.playedEvalMate ?? null, idx);
    // Fall back to a correctly-signed, mate-derived cp so the bar/graph still
    // position when only a mate score is available (the mate code itself takes
    // display precedence; this also resolves the mate-0 winner via ply parity).
    const cp =
      toWhitePerspective(analysis.playedEval ?? null, idx) ??
      moverMateToWhiteCp(analysis.playedEvalMate ?? null, idx);
    return { cp, mate };
  }
  return { cp: null, mate: null };
}

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

  const selectedEval = selectedEvalFromMap(
    analysisMap,
    selectedMoveIndex,
    selectedMoveIndex === null
      ? null
      : moveHistory[selectedMoveIndex]?.fen ?? null,
  );

  return (
    <EvalBar
      whitePerspectiveCp={selectedEval.cp}
      whitePerspectiveMate={selectedEval.mate}
      whiteOnBottom={boardOrientation === "white"}
    />
  );
});
ConnectedEvalBar.displayName = "ConnectedEvalBar";

// ---------------------------------------------------------------------------
// ConnectedAnalysisGraph
// ---------------------------------------------------------------------------

type ConnectedAnalysisGraphProps = {
  onSelectMove: (index: number | null) => void;
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

    // Normalize a graph click on the session's latest ply to null so the board
    // stays live (matches MoveList behavior). Compares against the full move
    // history length, not the trimmed `evals` length, so selecting the rightmost
    // *plotted* point while later moves are still unanalyzed stays historical.
    const handleSelectMove = useCallback(
      (index: number) => {
        const latestPly = moveHistory.length - 1;
        onSelectMove(index === latestPly ? null : index);
      },
      [moveHistory.length, onSelectMove],
    );

    const evals = useMemo(() => {
      const lastIdx = moveHistory.length - 1;
      // whiteCpForMove prefers the CP channel, then a correctly-signed mate cp,
      // and for the terminal ply synthesizes an unevaluated checkmate (both
      // channels null/absent) from the post-move FEN. Applied BEFORE trailing-null
      // trimming so a synthesized terminal mate survives the trim.
      const raw = moveHistory.map((mv, i) => {
        const a = analysisMap.get(i);
        return whiteCpForMove(
          a?.playedEval ?? null,
          a?.playedEvalMate ?? null,
          i,
          i === lastIdx ? mv.fen : null,
        );
      });
      let end = raw.length;
      while (end > 0 && raw[end - 1] === null) end--;
      return raw.slice(0, end);
    }, [moveHistory, analysisMap]);

    const pendingIndices = useMemo(() => {
      const pending: number[] = [];
      const lastIdx = moveHistory.length - 1;
      for (let i = 0; i < moveHistory.length; i++) {
        if (analysisMap.has(i)) continue;
        // A terminal checkmate ply has a known eval synthesized from its FEN — it
        // is plotted, not pending, so don't also mark it with a zero-line hollow
        // dot. Only the last ply can be synthesized, so scope the guard to it.
        if (i === lastIdx && evals[i] != null) continue;
        pending.push(i);
      }
      return pending;
    }, [moveHistory, analysisMap, evals]);

    const selectedEval = selectedEvalFromMap(
      analysisMap,
      selectedMoveIndex,
      selectedMoveIndex === null
        ? null
        : moveHistory[selectedMoveIndex]?.fen ?? null,
    );

    const graphStreamingEval = useMemo(() => {
      if (!streamingEval) return null;
      return {
        index: streamingEval.moveIndex,
        cp:
          toWhitePerspective(streamingEval.cp, streamingEval.moveIndex) ?? 0,
      };
    }, [streamingEval]);

    const isCheckmate = useMemo(() => {
      if (selectedMoveIndex === null) return false;
      return isCheckmateFen(moveHistory[selectedMoveIndex]?.fen ?? null);
    }, [moveHistory, selectedMoveIndex]);

    if (!evals.some((e) => e !== null) && pendingIndices.length === 0) {
      return null;
    }

    return (
      <AnalysisGraph
        evals={evals}
        currentIndex={selectedMoveIndex}
        onSelectMove={handleSelectMove}
        playerColor={playerColor}
        evalCp={selectedEval.cp}
        evalMate={selectedEval.mate}
        isCheckmate={isCheckmate}
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
  onResign?: () => void;
  isResignDisabled?: boolean;
  onRevert?: () => void;
  isRevertDisabled?: boolean;
  onFlipBoard?: () => void;
  onCopyPosition?: () => void;
  onReset?: () => void;
  isGameActive?: boolean;
  isInteractionDisabled?: boolean;
  /** FEN used for the relocated material in the mobile controls row. */
  materialFen?: string;
};

export const ConnectedMoveList = memo(
  ({
    onNavigate,
    messages,
    onRevealSrsFail,
    revealedSrsFailIndex,
    onResign,
    isResignDisabled,
    onRevert,
    isRevertDisabled,
    onFlipBoard,
    onCopyPosition,
    onReset,
    isGameActive,
    isInteractionDisabled,
    materialFen,
  }: ConnectedMoveListProps) => {
    const analysisStoreApi = useAnalysisStoreApi();
    const isNarrow = useMediaQuery(GAME_MOBILE_QUERY);
    const analysisMap = useAnalysisStore((s) => s.analysisMap);
    const moveHistory = useGameStore((s) => s.moveHistory);
    const viewIndex = useGameStore((s) => s.viewIndex);
    const playerColor = useGameStore((s) => s.playerColor);
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

    const freshlyResolved = useAnalysisStore((s) => s.freshlyResolved);

    // Mark freshly-resolved player moves via zustand subscribe (not a React
    // effect) so every resolveAnalysis call is captured even when React
    // batches multiple updates. Reads playerColor from the game store
    // directly to avoid stale ref reads on game start.
    useEffect(() => {
      const unsub = analysisStoreApi.subscribe((state, prev) => {
        if (state.lastAnalysis === prev.lastAnalysis) return;
        const la = state.lastAnalysis;
        if (!la || la.moveIndex === null || !la.classification) return;
        const pc = useGameStore.getState().playerColor;
        const isWhite = la.moveIndex % 2 === 0;
        const isPlayer = pc === "white" ? isWhite : !isWhite;
        if (!isPlayer) return;
        analysisStoreApi.getState().markFreshlyResolved(la.moveIndex);
      });
      return unsub;
    }, [analysisStoreApi]);

    const handleFreshAnimationDone = useCallback(
      (moveIndex: number) => {
        analysisStoreApi.getState().clearFreshlyResolved(moveIndex);
      },
      [analysisStoreApi],
    );

    const prevAnnotatedRef = useRef<
      ReturnType<typeof deriveAnnotatedMoves>
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
          old.eval === item.eval &&
          old.evalMate === item.evalMate
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

    const Component = isNarrow ? HorizontalMoveList : MoveList;
    return (
      <Component
        moves={annotatedMoves}
        currentIndex={viewIndex}
        onNavigate={onNavigate}
        canAddSelectedMove={canAddSelectedMove}
        isAddingSelectedMove={isAddingToLibrary}
        onAddSelectedMove={handleAddSelectedMove}
        messages={messages}
        analyzingIndices={analyzingIndices}
        freshlyResolvedIndices={freshlyResolved}
        onFreshAnimationDone={handleFreshAnimationDone}
        playerColor={playerColor}
        onRevealSrsFail={onRevealSrsFail}
        revealedSrsFailIndex={revealedSrsFailIndex}
        onResign={onResign}
        isResignDisabled={isResignDisabled}
        onRevert={onRevert}
        isRevertDisabled={isRevertDisabled}
        onFlipBoard={onFlipBoard}
        onCopyPosition={onCopyPosition}
        onReset={onReset}
        isGameActive={isGameActive}
        isInteractionDisabled={isInteractionDisabled}
        materialFen={materialFen}
        materialPerspective={playerColor}
        showSoundToggle={isNarrow}
      />
    );
  },
);
ConnectedMoveList.displayName = "ConnectedMoveList";
