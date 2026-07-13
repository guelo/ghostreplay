import { describe, it, expect, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useGameReviewStats } from './useGameReviewStats';
import type { AnalysisMove } from '../utils/api';

function makeMoves(entries: Partial<AnalysisMove>[]): AnalysisMove[] {
  return entries.map((e, i) => ({
    move_number: Math.floor(i / 2) + 1,
    color: i % 2 === 0 ? 'white' : 'black',
    move_san: 'e4',
    fen_after: 'fen',
    eval_cp: null,
    eval_mate: null,
    best_move_san: null,
    best_move_eval_cp: null,
    eval_delta: null,
    classification: null,
    ...e,
  }));
}

// playerColor = white → player moves at even indices.
// Player: blunder@2, inaccuracy@4, mistake@6. Opponent (black): blunder@1, mistake@3.
const MOVES = makeMoves([
  { color: 'white' }, // 0
  { color: 'black', classification: 'blunder' }, // 1 (opponent)
  { color: 'white', classification: 'blunder' }, // 2
  { color: 'black', classification: 'mistake' }, // 3 (opponent)
  { color: 'white', classification: 'inaccuracy' }, // 4
  { color: 'black' }, // 5
  { color: 'white', classification: 'mistake' }, // 6
  { color: 'black' }, // 7
]);

function setup(onJumpToMove?: (index: number) => void) {
  return renderHook(() =>
    useGameReviewStats({
      selectedId: 'game-1',
      moves: MOVES,
      playerColor: 'white',
      onJumpToMove,
    }),
  );
}

describe('useGameReviewStats', () => {
  it("hovering a side header highlights the union of that side's dots sorted by index", () => {
    const { result } = setup();
    act(() => result.current.handleStatHover({ side: 'player', cls: 'all' }));
    expect(result.current.highlightedMoves).toEqual({
      dots: [
        { index: 2, classification: 'blunder' },
        { index: 4, classification: 'inaccuracy' },
        { index: 6, classification: 'mistake' },
      ],
    });
  });

  it('excludes the other side from the header highlight', () => {
    const { result } = setup();
    act(() => result.current.handleStatHover({ side: 'opponent', cls: 'all' }));
    expect(result.current.highlightedMoves).toEqual({
      dots: [
        { index: 1, classification: 'blunder' },
        { index: 3, classification: 'mistake' },
      ],
    });
  });

  it('hovering a single cell highlights only that class (cell regression guard)', () => {
    const { result } = setup();
    act(() => result.current.handleStatHover({ side: 'player', cls: 'blunder' }));
    expect(result.current.highlightedMoves).toEqual({
      dots: [{ index: 2, classification: 'blunder' }],
    });
  });

  it('clicking a side header cycles the board through every dot then wraps, and pins the header', () => {
    const onJumpToMove = vi.fn();
    const { result } = setup(onJumpToMove);
    const header = { side: 'player' as const, cls: 'all' as const };

    act(() => result.current.handleStatClick(header));
    act(() => result.current.handleStatClick(header));
    act(() => result.current.handleStatClick(header));
    act(() => result.current.handleStatClick(header));

    expect(onJumpToMove.mock.calls.map((c) => c[0])).toEqual([2, 4, 6, 2]);
    expect(result.current.pinnedStat).toEqual({ side: 'player', cls: 'all' });
  });

  it('clicking a graph move clears the pin', () => {
    const { result } = setup();
    act(() => result.current.handleStatClick({ side: 'player', cls: 'all' }));
    expect(result.current.pinnedStat).toEqual({ side: 'player', cls: 'all' });
    act(() => result.current.handleGraphMoveClick());
    expect(result.current.pinnedStat).toBeNull();
  });
});
