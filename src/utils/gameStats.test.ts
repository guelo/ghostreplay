import { describe, it, expect } from 'vitest';
import { computeSideStats, CLASS_KEYS, selectionDots } from './gameStats';
import type { AnalysisMove } from './api';
import type { SideStats } from './gameStats';

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
    expect(result.player.avgCplCount).toBe(2);
    expect(result.opponent.avgCplCount).toBe(2);
  });

  it('rounds an exact-half avgCpl up (2, 3 -> 3), matching the backend', () => {
    // Cross-runtime contract: the backend rounds the same average half-up via
    // round_half_up_cpl (backend/app/centipawn_loss.py; see
    // docs/architecture/analysis-evidence.md). The deltas
    // above are exact integers, so they would still pass under trunc/floor.
    const moves = makeMoves([
      { color: 'white', eval_delta: 2 },
      { color: 'black', eval_delta: 0 },
      { color: 'white', eval_delta: 3 },
      { color: 'black', eval_delta: 0 },
    ]);

    const result = computeSideStats(moves, 'white');
    expect(result.player.avgCpl).toBe(3);
  });

  it('reports a null avgCpl and count of 0 when no moves carry eval_delta', () => {
    const moves = makeMoves([
      { color: 'white' },
      { color: 'black' },
    ]);
    const result = computeSideStats(moves, 'white');
    expect(result.player.avgCpl).toBeNull();
    expect(result.opponent.avgCpl).toBeNull();
    expect(result.player.avgCplCount).toBe(0);
    expect(result.opponent.avgCplCount).toBe(0);
  });

  it('reports avgCpl 0 (not null) when every evaluated delta is 0 — perfect play', () => {
    const moves = makeMoves([
      { color: 'white', eval_delta: 0 },
      { color: 'black', eval_delta: 0 },
      { color: 'white', eval_delta: 0 },
    ]);
    const result = computeSideStats(moves, 'white');
    expect(result.player.avgCpl).toBe(0);
    expect(result.opponent.avgCpl).toBe(0);
    expect(result.player.avgCplCount).toBe(2);
    expect(result.opponent.avgCplCount).toBe(1);
  });

  it('averages only the evaluated deltas when a side is partially analyzed', () => {
    const moves = makeMoves([
      { color: 'white', eval_delta: 40 },
      { color: 'white' },
      { color: 'black', eval_delta: 10 },
    ]);
    const result = computeSideStats(moves, 'white');
    // The unevaluated ply is skipped, not counted as 0 (which would give 20).
    expect(result.player.avgCpl).toBe(40);
    expect(result.player.avgCplCount).toBe(1);
  });

  it('returns zero stats for empty moves', () => {
    const result = computeSideStats([], 'white');
    expect(result.player.avgCpl).toBeNull();
    expect(result.opponent.avgCpl).toBeNull();
    expect(result.player.avgCplCount).toBe(0);
    expect(result.opponent.avgCplCount).toBe(0);
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

function makeSide(over: Partial<SideStats> = {}): SideStats {
  return {
    blunder: { count: 0, indices: [] },
    mistake: { count: 0, indices: [] },
    inaccuracy: { count: 0, indices: [] },
    avgCpl: 0,
    avgCplCount: 0,
    ...over,
  };
}

describe('selectionDots', () => {
  const sides = {
    player: makeSide({
      blunder: { count: 1, indices: [2] },
      inaccuracy: { count: 1, indices: [4] },
      mistake: { count: 1, indices: [6] },
    }),
    opponent: makeSide({
      blunder: { count: 1, indices: [1] },
      mistake: { count: 1, indices: [3] },
    }),
  };

  it('returns [] for a null selection', () => {
    expect(selectionDots(sides, null)).toEqual([]);
  });

  it("expands a single-class cell selection to that class's indices as dots", () => {
    expect(selectionDots(sides, { side: 'player', cls: 'blunder' })).toEqual([
      { index: 2, classification: 'blunder' },
    ]);
  });

  it("expands cls='all' to the class union, sorted ascending with per-dot classification", () => {
    expect(selectionDots(sides, { side: 'player', cls: 'all' })).toEqual([
      { index: 2, classification: 'blunder' },
      { index: 4, classification: 'inaccuracy' },
      { index: 6, classification: 'mistake' },
    ]);
  });

  it('isolates the selected side', () => {
    expect(selectionDots(sides, { side: 'opponent', cls: 'all' })).toEqual([
      { index: 1, classification: 'blunder' },
      { index: 3, classification: 'mistake' },
    ]);
  });
});
