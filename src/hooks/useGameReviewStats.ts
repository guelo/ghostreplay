import { useCallback, useMemo, useState } from 'react';
import type { AnalysisMove } from '../utils/api';
import { computeSideStats, type ClassKey, type SideStats, type StatSelection } from '../utils/gameStats';
import { useTouchOnly } from './useTouchOnly';

interface UseGameReviewStatsArgs {
  selectedId: string | null;
  moves: AnalysisMove[] | null;
  playerColor: 'white' | 'black';
  onJumpToMove?: (index: number) => void;
}

interface UseGameReviewStatsReturn {
  sideStats: { player: SideStats; opponent: SideStats } | null;
  highlightedMoves: { indices: number[]; classification: ClassKey } | null;
  handleStatHover: (sel: StatSelection) => void;
  handleStatClick: (sel: StatSelection) => void;
  handleGraphMoveClick: () => void;
  pinnedStat: StatSelection;
  activeStat: StatSelection;
}

export function useGameReviewStats({ selectedId, moves, playerColor, onJumpToMove }: UseGameReviewStatsArgs): UseGameReviewStatsReturn {
  const [statInteraction, setStatInteraction] = useState<{
    gameId: string | null;
    pinned: StatSelection;
    hovered: StatSelection;
    cycleIndex: number;
  }>({ gameId: null, pinned: null, hovered: null, cycleIndex: 0 });

  const pinnedStat = statInteraction.gameId === selectedId ? statInteraction.pinned : null;
  const hoveredStat = statInteraction.gameId === selectedId ? statInteraction.hovered : null;
  const cycleIndex = statInteraction.gameId === selectedId ? statInteraction.cycleIndex : 0;

  const setHoveredStat = useCallback((hovered: StatSelection) => {
    setStatInteraction(prev => ({ ...prev, gameId: selectedId, hovered }));
  }, [selectedId]);

  const isTouchOnly = useTouchOnly();

  const sideStats = useMemo(() => {
    if (!moves) return null;
    return computeSideStats(moves, playerColor);
  }, [moves, playerColor]);

  const activeStat = hoveredStat ?? pinnedStat;

  const highlightedMoves = useMemo(() => {
    if (!activeStat || !sideStats) return null;
    const stats = activeStat.side === 'player' ? sideStats.player : sideStats.opponent;
    return { indices: stats[activeStat.cls].indices, classification: activeStat.cls };
  }, [activeStat, sideStats]);

  const handleStatHover = useCallback((sel: StatSelection) => {
    if (isTouchOnly) return;
    setHoveredStat(sel);
  }, [isTouchOnly, setHoveredStat]);

  const handleStatClick = useCallback((sel: StatSelection) => {
    const isSameCategory = pinnedStat?.side === sel?.side && pinnedStat?.cls === sel?.cls;
    const targetStats = sel?.side === 'player' ? sideStats?.player : sideStats?.opponent;
    const targetMoves = sel && targetStats ? targetStats[sel.cls].indices : [];
    let newCycleIndex = 0;

    if (isSameCategory && targetMoves.length > 0) {
      newCycleIndex = (cycleIndex + 1) % targetMoves.length;
    }

    setStatInteraction((prev) => ({
      ...prev,
      gameId: selectedId,
      pinned: sel,
      cycleIndex: newCycleIndex,
    }));

    if (targetMoves.length > 0) {
      onJumpToMove?.(targetMoves[newCycleIndex]);
    }
  }, [cycleIndex, onJumpToMove, pinnedStat, selectedId, sideStats]);

  const handleGraphMoveClick = useCallback(() => {
    setStatInteraction(prev => ({ ...prev, gameId: selectedId, pinned: null, cycleIndex: 0 }));
  }, [selectedId]);

  return { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat };
}
