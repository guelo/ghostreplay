import { describe, expect, it } from 'vitest'
import { Chess } from 'chess.js'
import {
  DEFAULT_THERMAL_PLIES,
  SMOKE_POSITIONS,
  buildPositionSet,
  buildThermalPositions,
} from './positions'

const isLegal = (fen: string, uci: string) => {
  const chess = new Chess(fen)
  try {
    return Boolean(
      chess.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        ...(uci.length > 4 ? { promotion: uci.slice(4, 5) } : {}),
      }),
    )
  } catch {
    return false
  }
}

describe('thermal sequence', () => {
  const positions = buildThermalPositions()

  it('is long enough for §10.4 and runs in game order', () => {
    expect(positions).toHaveLength(DEFAULT_THERMAL_PLIES)
    expect(DEFAULT_THERMAL_PLIES).toBeGreaterThanOrEqual(40)
    expect(positions.map((position) => position.thermalIndex)).toEqual(
      positions.map((_, index) => index + 1),
    )
  })

  it('emits a legal (position, move) pair for every ply', () => {
    // The game is stored as moves precisely so an illegal one fails here rather
    // than silently measuring a position that never occurred.
    for (const position of positions) {
      expect(isLegal(position.fen, position.playedMove)).toBe(true)
    }
  })

  it('agrees with the side to move encoded in each FEN', () => {
    for (const position of positions) {
      expect(new Chess(position.fen).turn()).toBe(position.playerColor === 'white' ? 'w' : 'b')
    }
  })

  it('uses stable, unique position ids', () => {
    const ids = positions.map((position) => position.positionId)
    expect(new Set(ids).size).toBe(ids.length)
    expect(ids[0]).toBe('thermal:ply-001')
  })
})

describe('smoke set', () => {
  it('has a legal move in every position', () => {
    for (const position of SMOKE_POSITIONS) {
      expect(isLegal(position.fen, position.playedMove)).toBe(true)
    }
  })

  it('covers both colours and unique ids', () => {
    expect(new Set(SMOKE_POSITIONS.map((position) => position.playerColor))).toEqual(
      new Set(['white', 'black']),
    )
    expect(new Set(SMOKE_POSITIONS.map((position) => position.positionId)).size).toBe(
      SMOKE_POSITIONS.length,
    )
  })
})

describe('buildPositionSet', () => {
  it('caps the thermal sequence to the requested ply count', () => {
    expect(buildPositionSet('thermal-40', 12).positions).toHaveLength(12)
    expect(buildPositionSet('smoke-6').positions).toHaveLength(SMOKE_POSITIONS.length)
  })
})
