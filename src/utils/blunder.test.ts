import { describe, it, expect } from 'vitest'
import {
  shouldRecordBlunder,
  type AnalysisResult,
  type BlunderContext,
} from './blunder'

describe('shouldRecordBlunder', () => {
  const makeAnalysis = (
    overrides: Partial<AnalysisResult> = {},
  ): AnalysisResult => ({
    move: 'e2e4', // UCI format (what analysis worker returns)
    bestMove: 'd2d4',
    bestEval: 50,
    playedEval: -150,
    delta: 200,
    blunder: true,
    ...overrides,
  })

  const makeContext = (
    overrides: Partial<BlunderContext> = {},
  ): BlunderContext => ({
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    pgn: '1. e4',
    moveSan: 'e4', // SAN format (for API)
    moveUci: 'e2e4', // UCI format (for matching with analysis)
    moveIndex: 0,
    ...overrides,
  })

  it('returns blunder data when all conditions are met', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toEqual({
      sessionId: 'session-123',
      pgn: '1. e4',
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      userMove: 'e4', // SAN format for API
      bestMove: 'd2d4',
      evalBefore: 50,
      evalAfter: -150,
    })
  })

  it('returns null when analysis is null', () => {
    const result = shouldRecordBlunder({
      analysis: null,
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('returns null when analysis is not a blunder', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis({ blunder: false, delta: 30 }),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('returns null when sessionId is null', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: makeContext(),
      sessionId: null,
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('returns null when game is not active', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: false,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('returns null when blunder already recorded (first blunder only)', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: true,
    })

    expect(result).toBeNull()
  })

  it('returns null when context is null', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: null,
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('returns null when analysis move does not match context move (UCI comparison)', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis({ move: 'g1f3' }), // UCI for Nf3
      context: makeContext({ moveUci: 'e2e4' }), // UCI for e4
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })

  it('uses 0 for null eval values', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis({ bestEval: null, playedEval: null }),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).not.toBeNull()
    expect(result!.evalBefore).toBe(0)
    expect(result!.evalAfter).toBe(0)
  })

  it('handles edge case: delta exactly at threshold (50cp)', () => {
    // Note: the blunder flag is set by the analysis worker based on delta >= 50
    // This test verifies we correctly pass through when blunder=true
    const result = shouldRecordBlunder({
      analysis: makeAnalysis({ delta: 50, blunder: true }),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).not.toBeNull()
  })

  it('handles large eval losses', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis({
        bestEval: 500,
        playedEval: -800,
        delta: 1300,
        blunder: true,
      }),
      context: makeContext(),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).not.toBeNull()
    expect(result!.evalBefore).toBe(500)
    expect(result!.evalAfter).toBe(-800)
  })

  it('returns null for moves after full move 10', () => {
    const result = shouldRecordBlunder({
      analysis: makeAnalysis(),
      context: makeContext({ moveIndex: 20 }),
      sessionId: 'session-123',
      isGameActive: true,
      alreadyRecorded: false,
    })

    expect(result).toBeNull()
  })
})
