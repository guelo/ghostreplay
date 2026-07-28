import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { Chess } from 'chess.js'
import {
  DEFAULT_THERMAL_PLIES,
  SMOKE_POSITIONS,
  bestMovePlies,
  buildBestMovePositions,
  buildPositionSet,
  buildThermalPositions,
} from './positions'
import { parseJsonl } from '../benchRecord'
import type { BenchMoveRecord, BenchRunRecord } from '../benchRecord'

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

describe('best-move set (the kill-gate corpus)', () => {
  const positions = buildBestMovePositions()

  it('is 30 positions, dropping every 4th ply of the thermal sequence', () => {
    expect(positions).toHaveLength(30)
    expect(bestMovePlies()).toHaveLength(30)
    expect(positions.map((position) => position.positionId)).toEqual(
      bestMovePlies().map((ply) => `best30:ply-${String(ply).padStart(3, '0')}`),
    )
    // Spread from opening to endgame, not the first 30 plies.
    expect(bestMovePlies().at(-1)).toBe(39)
  })

  it('never reuses a thermal id, which would collide on the join key', () => {
    // These rows carry a DIFFERENT played move at the same FEN, so sharing an id
    // would give two different measurements the same key across files.
    const thermalIds = new Set(buildThermalPositions().map((position) => position.positionId))
    for (const position of positions) {
      expect(position.positionId.startsWith('best30:')).toBe(true)
      expect(thermalIds.has(position.positionId)).toBe(false)
    }
    expect(new Set(positions.map((position) => position.positionId)).size).toBe(positions.length)
  })

  it('declares itself unordered, so it cannot be graphed as a thermal curve', () => {
    for (const position of positions) {
      expect(position.thermalIndex).toBeNull()
    }
    expect(buildPositionSet('best-30').isThermalSequence).toBe(false)
  })

  it('has a legal move in every position, played by the side to move', () => {
    for (const position of positions) {
      expect(isLegal(position.fen, position.playedMove), position.positionId).toBe(true)
      expect(new Chess(position.fen).turn()).toBe(position.playerColor === 'white' ? 'w' : 'b')
    }
  })

  it('shares its FENs with the thermal sequence it was derived from', () => {
    const byPly = new Map(
      buildThermalPositions().map((position) => [position.thermalIndex, position.fen]),
    )
    for (const position of positions) {
      const ply = Number(position.positionId.slice('best30:ply-'.length))
      expect(position.fen, position.positionId).toBe(byPly.get(ply))
    }
  })

  /**
   * The check that stops the corpus drifting from the evidence that produced it.
   *
   * The played moves ARE the engine's own recorded depth-17 best moves, read out
   * of the committed thermal-40 baselines — which is what makes this set the
   * `P === B` cohort by construction under the current protocol. If a baseline
   * is ever replaced, or an entry mistyped, the two stop agreeing here rather
   * than at the far end of a 20-minute phone capture.
   */
  it('matches result.bestMove in every committed thermal-40 baseline', () => {
    const analysisDir = resolve(__dirname, '..', '..', '..', 'docs', 'analysis')
    const files = readdirSync(analysisDir).filter((name) => name.endsWith('.jsonl'))
    const expected = new Map(positions.map((position) => [position.fen, position.playedMove]))
    const idByFen = new Map(positions.map((position) => [position.fen, position.positionId]))
    const allIds = [...idByFen.values()].sort()
    const filesChecked: string[] = []
    const cells: string[] = []

    for (const file of files) {
      const records = parseJsonl(readFileSync(resolve(analysisDir, file), 'utf8'))
      const header = records.find((record) => record.kind === 'run') as BenchRunRecord | undefined
      if (header?.plan.positionSetId !== 'thermal-40') continue

      // The corpus is the SHIPPING protocol's depth-17 best moves, so only its
      // rows can contradict it — a candidate arm searching a deeper root may
      // legitimately name a different move. Warm-ups are priming duplicates and
      // would double-count the position they primed.
      const rows = records.filter(
        (record): record is BenchMoveRecord =>
          record.kind === 'move' && record.arm === 'current' && !record.warmup,
      )
      if (rows.length === 0) continue
      filesChecked.push(file)

      // Driven by the header's DECLARED repeat count, not by the repeats the
      // rows happen to contain: a map keyed off the rows simply omits a repeat
      // that vanished entirely, and nothing downstream would notice.
      for (let repeat = 0; repeat < header.plan.repeats; repeat += 1) {
        const rowsForRepeat = rows.filter((row) => row.repeat === repeat)
        expect(rowsForRepeat.length, `${file} declares repeat ${repeat} but has no rows`)
          .toBeGreaterThan(0)

        const seen: string[] = []
        for (const row of rowsForRepeat) {
          const id = idByFen.get(row.fen)
          if (id === undefined) continue
          seen.push(id)
          expect(row.result?.bestMove, `${file} ${row.positionId} (${id})`).toBe(
            expected.get(row.fen),
          )
        }

        // EXACT-ONCE over the whole set, compared as a sorted list rather than a
        // count. A count of 30 is also produced by a repeat that duplicates one
        // position and loses another, and by a truncated file whose surviving
        // rows happen to number 30.
        expect(seen.sort(), `${file} repeat ${repeat}`).toEqual(allIds)
        cells.push(`${file}#${repeat}`)
      }
    }

    // The three committed baselines, each fully covered above. Floors, so adding
    // a fourth device does not require editing these lines — and they are only
    // the non-vacuity guard now that completeness is asserted per file.
    expect(filesChecked.length, 'committed thermal-40 baselines').toBeGreaterThanOrEqual(3)
    expect(cells.length).toBeGreaterThanOrEqual(9)
  })
})

describe('buildPositionSet', () => {
  it('caps the thermal sequence to the requested ply count', () => {
    expect(buildPositionSet('thermal-40', 12).positions).toHaveLength(12)
    expect(buildPositionSet('smoke-6').positions).toHaveLength(SMOKE_POSITIONS.length)
  })

  it('builds each set as itself, never substituting one for another', () => {
    // `buildPositionSet` used to be a two-way test, and its caller in `form.ts`
    // still was: a third id would have run the thermal sequence under a
    // `best-30` header.
    expect(buildPositionSet('best-30').positions).toHaveLength(30)
    expect(buildPositionSet('best-30').id).toBe('best-30')
    expect(buildPositionSet('thermal-40').positions).toHaveLength(DEFAULT_THERMAL_PLIES)
  })
})
