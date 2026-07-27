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
      seldepth: 20,
      multipv: 1,
      score: { type: 'cp', value: 30 },
      bound: 'exact',
      nodes: 500000,
      nps: 250000,
      time: 2000,
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
      bound: 'exact',
      pv: ['d1h5', 'e8d7', 'h5f7'],
    })
  })

  it('parses a negative mate score', () => {
    const result = parseUciInfoLine('info depth 9 score mate -2 pv e1g1 d8h4')
    expect(result?.score).toEqual({ type: 'mate', value: -2 })
    expect(result?.bound).toBe('exact')
  })

  it('parses mate 0', () => {
    // Degenerate but well-formed: the snapshot layer, not the parser, rejects it.
    const result = parseUciInfoLine('info depth 1 score mate 0 pv e1g1')
    expect(result?.score).toEqual({ type: 'mate', value: 0 })
  })

  it('parses hashfull', () => {
    const result = parseUciInfoLine('info depth 20 hashfull 512 score cp 12 pv e2e4')
    expect(result?.hashfull).toBe(512)
  })

  describe('score bounds', () => {
    it('reports an aspiration fail-high as a lower bound', () => {
      const result = parseUciInfoLine('info depth 14 score cp 62 lowerbound pv d2d4')
      expect(result?.bound).toBe('lower')
      expect(result?.score).toEqual({ type: 'cp', value: 62 })
    })

    it('reports an aspiration fail-low as an upper bound', () => {
      const result = parseUciInfoLine('info depth 14 score cp -18 upperbound pv d2d4')
      expect(result?.bound).toBe('upper')
    })

    it('reports a settled score as exact', () => {
      expect(parseUciInfoLine('info depth 14 score cp 20 pv d2d4')?.bound).toBe('exact')
    })

    it('bounds a mate score too', () => {
      expect(
        parseUciInfoLine('info depth 14 score mate 4 lowerbound pv d2d4')?.bound,
      ).toBe('lower')
    })

    it('omits bound entirely when the line carries no score', () => {
      const result = parseUciInfoLine('info depth 12 seldepth 18 nodes 100 pv e2e4')
      expect(result).not.toBeNull()
      expect(result).not.toHaveProperty('bound')
    })
  })

  describe('missing tokens', () => {
    it('omits every optional field absent from the line', () => {
      const result = parseUciInfoLine('info depth 7 score cp 5 pv e2e4 e7e5')
      expect(result).toEqual({
        depth: 7,
        score: { type: 'cp', value: 5 },
        bound: 'exact',
        pv: ['e2e4', 'e7e5'],
      })
    })

    it('parses a pv-only line', () => {
      const result = parseUciInfoLine('info pv e2e4 e7e5')
      expect(result).toEqual({ pv: ['e2e4', 'e7e5'] })
    })

    it('ignores a malformed numeric token', () => {
      const result = parseUciInfoLine('info depth 12 nodes abc score cp 4 pv e2e4')
      expect(result).not.toHaveProperty('nodes')
      expect(result?.depth).toBe(12)
    })

    it('ignores an unknown score type', () => {
      const result = parseUciInfoLine('info depth 12 score bogus 4 pv e2e4')
      expect(result).not.toHaveProperty('score')
      expect(result).not.toHaveProperty('bound')
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
      expect(result!.seldepth).toBe(18)
      expect(result!.nodes).toBe(100000)
      expect(result!.nps).toBe(500000)
      expect(result!.time).toBe(200)
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
      bound: 'exact',
      pv: ['d2d4', 'd7d5'],
    })
  })

  it('extracts a high multipv slot', () => {
    expect(parseUciInfoLine('info depth 9 multipv 5 score cp -40 pv h2h3')?.multipv).toBe(5)
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
