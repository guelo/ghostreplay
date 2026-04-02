import { useCallback, useMemo, useState } from 'react';
import type { AnalysisMove } from '../utils/api';
import { computeSideStats, type ClassKey, type SideStats, type StatSelection } from '../utils/gameStats';
import { useTouchOnly } from './useTouchOnly';

interface UseGameReviewStatsArgs {
  selectedId: string | null;
  moves: AnalysisMove[] | null;
  playerColor: 'white' | 'black';
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

export function useGameReviewStats({ selectedId, moves, playerColor }: UseGameReviewStatsArgs): UseGameReviewStatsReturn {
  const [statInteraction, setStatInteraction] = useState<{
    gameId: string | null;
    pinned: StatSelection;
    hovered: StatSelection;
  }>({ gameId: null, pinned: null, hovered: null });

  const pinnedStat = statInteraction.gameId === selectedId ? statInteraction.pinned : null;
  const hoveredStat = statInteraction.gameId === selectedId ? statInteraction.hovered : null;

  const setPinnedStat = useCallback((pinned: StatSelection) => {
    setStatInteraction(prev => ({ ...prev, gameId: selectedId, pinned }));
  }, [selectedId]);

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
    const isToggleOff = pinnedStat?.side === sel?.side && pinnedStat?.cls === sel?.cls;
    setPinnedStat(isToggleOff ? null : sel);
  }, [pinnedStat, setPinnedStat]);

  const handleGraphMoveClick = useCallback(() => {
    setPinnedStat(null);
  }, [setPinnedStat]);

  return { sideStats, highlightedMoves, handleStatHover, handleStatClick, handleGraphMoveClick, pinnedStat, activeStat };
}
