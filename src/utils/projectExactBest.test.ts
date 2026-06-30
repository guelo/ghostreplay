import { describe, it, expect } from 'vitest'
import { projectExactBest } from './projectExactBest'
import type { AnalysisMove, PositionAnalysis } from './api'

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
// FEN after 1. e4
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1'

const makeMove = (over: Partial<AnalysisMove> = {}): AnalysisMove => ({
  move_number: 1,
  color: 'white',
  move_san: 'e4',
  fen_after: AFTER_E4,
  eval_cp: 30,
  eval_mate: null,
  best_move_san: 'd4', // a weaker/earlier run named a DIFFERENT best
  best_move_eval_cp: 25,
  eval_delta: 8,
  classification: 'good',
  ...over,
})

const makePos = (over: Partial<PositionAnalysis> = {}): PositionAnalysis => ({
  best_move_uci: 'e2e4',
  best_move_san: 'e4',
  best_move_eval_cp: 30,
  best_move_eval_mate: null,
  best_line_uci: ['e2e4', 'e7e5'],
  position_trusted: true,
  ...over,
})

describe('projectExactBest', () => {
  it('promotes a played move that equals the TRUSTED position best to best/loss-0', () => {
    const moves = [makeMove({ classification: 'good' })]
    const out = projectExactBest(moves, { [STARTING_FEN]: makePos() })

    expect(out[0].classification).toBe('best')
    expect(out[0].eval_delta).toBe(0)
    // best_move_san repointed at the played move so no contradictory best-arrow
    expect(out[0].best_move_san).toBe('e4')
    // untouched magnitudes survive
    expect(out[0].eval_cp).toBe(30)
    // original input is not mutated
    expect(moves[0].classification).toBe('good')
  })

  it('does NOT promote when the position is untrusted (position_trusted === false)', () => {
    const moves = [makeMove({ classification: 'good' })]
    const out = projectExactBest(moves, {
      [STARTING_FEN]: makePos({ position_trusted: false }),
    })

    expect(out[0].classification).toBe('good')
    expect(out[0].eval_delta).toBe(8)
    expect(out[0].best_move_san).toBe('d4')
    // untrusted entry → passed through by identity
    expect(out[0]).toBe(moves[0])
  })

  it('leaves a genuinely non-best move unchanged (played !== trusted best)', () => {
    const moves = [makeMove({ classification: 'good' })]
    const out = projectExactBest(moves, {
      [STARTING_FEN]: makePos({ best_move_uci: 'd2d4', best_move_san: 'd4' }),
    })

    expect(out[0]).toBe(moves[0])
    expect(out[0].classification).toBe('good')
  })

  it('leaves a move with no position_analysis entry for its fen_before unchanged', () => {
    const moves = [makeMove({ classification: 'good' })]
    const out = projectExactBest(moves, {}) // empty map
    expect(out[0]).toBe(moves[0])

    // undefined position_analysis returns the same array reference
    const out2 = projectExactBest(moves, undefined)
    expect(out2).toBe(moves)
  })

  it('chains fen_before from the previous move fen_after for non-first plies', () => {
    const moves = [
      makeMove({ color: 'white', move_san: 'e4', fen_after: AFTER_E4, classification: 'best', best_move_san: 'e4' }),
      makeMove({ color: 'black', move_san: 'e5', fen_after: 'after-e5', classification: 'good', best_move_san: 'c5' }),
    ]
    const out = projectExactBest(moves, {
      // ply 1's fen_before is the previous move's fen_after (AFTER_E4)
      [AFTER_E4]: makePos({ best_move_uci: 'e7e5', best_move_san: 'e5', best_line_uci: ['e7e5'] }),
    })

    expect(out[1].classification).toBe('best')
    expect(out[1].eval_delta).toBe(0)
    expect(out[1].best_move_san).toBe('e5')
    // ply 0 had no entry for STARTING_FEN → unchanged
    expect(out[0]).toBe(moves[0])
  })

  it('is idempotent — projecting an already-projected array is a no-op', () => {
    const pos = { [STARTING_FEN]: makePos() }
    const once = projectExactBest([makeMove({ classification: 'good' })], pos)
    const twice = projectExactBest(once, pos)

    expect(twice[0]).toBe(once[0]) // already best → returned by identity
    expect(twice[0].classification).toBe('best')
    expect(twice[0].eval_delta).toBe(0)
  })
})
