import { describe, expect, it } from 'vitest'
import {
  opponentToMoverScore,
  postToRootScore,
  rootToPostScore,
  terminalScoreAfterMove,
} from './terminal'

describe('Node terminal score table', () => {
  it('maps a delivered checkmate to post mate 0 and root mate +1', () => {
    expect(
      terminalScoreAfterMove(
        '7k/5Q2/6K1/8/8/8/8/8 w - - 0 1',
        'f7f8',
      ),
    ).toEqual({
      postMove: { type: 'mate', value: 0 },
      root: { type: 'mate', value: 1 },
      outcome: 'checkmate',
    })
  })

  it('maps every non-checkmate terminal to exact draw scores', () => {
    expect(
      terminalScoreAfterMove('k7/2Q5/2K5/8/8/8/8/8 w - - 0 1', 'c7b6'),
    ).toEqual({
      postMove: { type: 'cp', value: 0 },
      root: { type: 'cp', value: 0 },
      outcome: 'stalemate',
    })
    expect(
      terminalScoreAfterMove('8/8/8/8/8/2k5/1b6/K1B5 w - - 0 1', 'c1b2'),
    ).toEqual({
      postMove: { type: 'cp', value: 0 },
      root: { type: 'cp', value: 0 },
      outcome: 'insufficient-material',
    })
  })

  it('converts CP sign and mate distance at the correct frame boundary', () => {
    expect(opponentToMoverScore({ type: 'cp', value: -42 })).toEqual({
      type: 'cp',
      value: 42,
    })
    expect(opponentToMoverScore({ type: 'mate', value: -3 })).toEqual({
      type: 'mate',
      value: 3,
    })

    for (const rootMate of [1, 2, 10, -1, -2, -10]) {
      const root = { type: 'mate' as const, value: rootMate }
      expect(postToRootScore(rootToPostScore(root))).toEqual(root)
    }
    expect(rootToPostScore({ type: 'mate', value: 1 })).toEqual({
      type: 'mate',
      value: 0,
    })
    expect(postToRootScore({ type: 'mate', value: 0 })).toEqual({
      type: 'mate',
      value: 1,
    })
    expect(() => rootToPostScore({ type: 'mate', value: 0 })).toThrow(/root mate 0/)
  })
})
