import { describe, it, expect } from 'vitest';
import { computeSideStats, CLASS_KEYS } from './gameStats';
import type { AnalysisMove } from './api';

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

describe('computeSideStats', () => {
  it('counts classifications per side', () => {
    const moves = makeMoves([
      { color: 'white', classification: 'blunder', eval_delta: 100 },
      { color: 'black', classification: 'mistake', eval_delta: 50 },
      { color: 'white', classification: 'inaccuracy', eval_delta: 20 },
      { color: 'black', classification: 'blunder', eval_delta: 80 },
    ]);

    const result = computeSideStats(moves, 'white');
    expect(result.player.blunder.count).toBe(1);
    expect(result.player.inaccuracy.count).toBe(1);
    expect(result.player.mistake.count).toBe(0);
    expect(result.opponent.blunder.count).toBe(1);
    expect(result.opponent.mistake.count).toBe(1);
  });

  it('computes avgCpl per side', () => {
    const moves = makeMoves([
      { color: 'white', eval_delta: 10 },
      { color: 'black', eval_delta: 30 },
      { color: 'white', eval_delta: 20 },
      { color: 'black', eval_delta: 50 },
    ]);

    const result = computeSideStats(moves, 'white');
    expect(result.player.avgCpl).toBe(15);
    expect(result.opponent.avgCpl).toBe(40);
  });

  it('returns zero stats for empty moves', () => {
    const result = computeSideStats([], 'white');
    expect(result.player.avgCpl).toBe(0);
    expect(result.opponent.avgCpl).toBe(0);
    for (const cls of CLASS_KEYS) {
      expect(result.player[cls].count).toBe(0);
      expect(result.opponent[cls].count).toBe(0);
    }
  });

  it('tracks indices correctly', () => {
    const moves = makeMoves([
      { color: 'white', classification: 'blunder' },
      { color: 'black', classification: null },
      { color: 'white', classification: 'blunder' },
    ]);

    const result = computeSideStats(moves, 'white');
    expect(result.player.blunder.indices).toEqual([0, 2]);
  });

  it('respects playerColor=black', () => {
    const moves = makeMoves([
      { color: 'white', classification: 'blunder' },
      { color: 'black', classification: 'mistake' },
    ]);

    const result = computeSideStats(moves, 'black');
    expect(result.player.mistake.count).toBe(1);
    expect(result.opponent.blunder.count).toBe(1);
  });
});
