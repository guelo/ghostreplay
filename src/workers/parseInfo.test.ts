import { describe, expect, it } from 'vitest'
import { parseUciInfoLine } from './parseInfo'
import { parseScoreInfo } from './analysisUtils'

describe('parseUciInfoLine', () => {
  it('returns null for non-info lines', () => {
    expect(parseUciInfoLine('bestmove e2e4 ponder d7d5')).toBeNull()
    expect(parseUciInfoLine('readyok')).toBeNull()
    expect(parseUciInfoLine('')).toBeNull()
  })

  it('parses a full multipv info line', () => {
    const line =
      'info depth 15 seldepth 20 multipv 1 score cp 30 nodes 500000 nps 250000 time 2000 pv e2e4 e7e5 g1f3'
    const result = parseUciInfoLine(line)
    expect(result).toEqual({
      depth: 15,
      multipv: 1,
      score: { type: 'cp', value: 30 },
      pv: ['e2e4', 'e7e5', 'g1f3'],
    })
  })

  it('parses a mate score', () => {
    const line = 'info depth 18 multipv 1 score mate 3 pv d1h5 e8d7 h5f7'
    const result = parseUciInfoLine(line)
    expect(result).toEqual({
      depth: 18,
      multipv: 1,
      score: { type: 'mate', value: 3 },
      pv: ['d1h5', 'e8d7', 'h5f7'],
    })
  })

  describe('currmove / status-only lines', () => {
    it('parses currmove line as depth-only (no pv, no multipv)', () => {
      const line = 'info depth 15 currmove e2e4 currmovenumber 1'
      const result = parseUciInfoLine(line)
      // parseUciInfoLine returns it because depth is present, but critically
      // it has no pv and no multipv — the hook guard prevents it from
      // overwriting slot 0.
      expect(result).not.toBeNull()
      expect(result!.depth).toBe(15)
      expect(result!.pv).toBeUndefined()
      expect(result!.multipv).toBeUndefined()
    })

    it('parses seldepth-only status line', () => {
      const line = 'info depth 12 seldepth 18 nodes 100000 nps 500000 time 200'
      const result = parseUciInfoLine(line)
      expect(result).not.toBeNull()
      expect(result!.depth).toBe(12)
      expect(result!.pv).toBeUndefined()
      expect(result!.multipv).toBeUndefined()
    })
  })

  it('returns null for info string lines (no depth/score/pv)', () => {
    expect(parseUciInfoLine('info string NNUE evaluation using nn-...')).toBeNull()
  })

  it('parses multipv 2+ lines correctly', () => {
    const line =
      'info depth 15 multipv 2 score cp 10 pv d2d4 d7d5'
    const result = parseUciInfoLine(line)
    expect(result).toEqual({
      depth: 15,
      multipv: 2,
      score: { type: 'cp', value: 10 },
      pv: ['d2d4', 'd7d5'],
    })
  })
})

describe('parseScoreInfo', () => {
  it('returns score for a score-bearing info line', () => {
    const result = parseScoreInfo('info depth 15 score cp 30 pv e2e4')
    expect(result).toEqual({ score: { type: 'cp', value: 30 } })
  })

  it('returns null for currmove line (no score)', () => {
    expect(parseScoreInfo('info depth 15 currmove e2e4 currmovenumber 1')).toBeNull()
  })

  it('returns null for non-info lines', () => {
    expect(parseScoreInfo('bestmove e2e4')).toBeNull()
  })
})
