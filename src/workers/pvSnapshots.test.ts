import { beforeEach, describe, expect, it } from 'vitest'
import { parseUciInfoLine } from './parseInfo'
import {
  admitInfoLine,
  createSnapshotAssembler,
  getSnapshotCounters,
  recordCanceledSearch,
  recordLegacySelectorDivergence,
  resetSnapshotCounters,
  selectAtomicSnapshot,
  SNAPSHOT_REJECTIONS,
} from './pvSnapshots'
import type {
  AtomicSelectionRequest,
  PvSnapshot,
  SnapshotAssembler,
  SnapshotRejection,
} from './pvSnapshots'
import type { EngineScore } from './stockfishMessages'

const cp = (value: number): EngineScore => ({ type: 'cp', value })
const mate = (value: number): EngineScore => ({ type: 'mate', value })

/** Drive the real parser into the real assembler, exactly as the worker does. */
const feed = (assembler: SnapshotAssembler, ...lines: string[]) => {
  for (const line of lines) {
    const info = parseUciInfoLine(line)
    if (info) {
      admitInfoLine(assembler, info)
    }
  }
}

const scoresOf = (slots: PvSnapshot[] | undefined) =>
  slots?.map((slot) => (slot.score.type === 'cp' ? slot.score.value : slot.score))

/** A hand-built snapshot, for invariants the admission filter makes unreachable. */
const snapshot = (overrides: Partial<PvSnapshot> = {}): PvSnapshot => ({
  depth: 17,
  seldepth: null,
  multipv: 1,
  score: cp(30),
  bound: 'exact',
  pv: ['e2e4', 'e7e5'],
  nodes: null,
  timeMs: null,
  seq: 0,
  ...overrides,
})

const selectFrom = (
  slotsByDepth: Array<[number, PvSnapshot[]]>,
  overrides: Partial<Omit<AtomicSelectionRequest, 'assembler'>> & { requiredSlots?: number } = {},
) => {
  const { requiredSlots = 1, ...rest } = overrides
  return selectAtomicSnapshot({
    assembler: { requiredSlots, completeBatches: new Map(slotsByDepth) },
    requestedDepth: 17,
    bestMove: 'e2e4',
    capFired: false,
    stopReason: 'bestmove',
    ...rest,
  })
}

const rejectionOf = (selection: ReturnType<typeof selectFrom>): SnapshotRejection | 'accepted' =>
  selection.accepted ? 'accepted' : selection.reason

const zeroedRejections = () =>
  Object.fromEntries(SNAPSHOT_REJECTIONS.map((reason) => [reason, 0]))

beforeEach(() => {
  resetSnapshotCounters()
})

describe('admitInfoLine', () => {
  it('admits an exact line carrying both a score and a PV', () => {
    const assembler = createSnapshotAssembler(1)
    feed(assembler, 'info depth 14 multipv 1 score cp 30 nodes 900 time 40 pv e2e4 e7e5')
    expect(assembler.completeBatches.get(14)).toEqual([
      {
        depth: 14,
        seldepth: null,
        multipv: 1,
        score: { type: 'cp', value: 30 },
        bound: 'exact',
        pv: ['e2e4', 'e7e5'],
        nodes: 900,
        timeMs: 40,
        seq: 0,
      },
    ])
  })

  it('carries seldepth through when the engine reports it', () => {
    const assembler = createSnapshotAssembler(1)
    feed(assembler, 'info depth 14 seldepth 25 score cp 30 pv e2e4 e7e5')
    expect(assembler.completeBatches.get(14)?.[0].seldepth).toBe(25)
  })

  it('defaults an absent multipv token to slot 1', () => {
    const assembler = createSnapshotAssembler(1)
    feed(assembler, 'info depth 14 score cp 30 pv e2e4 e7e5')
    expect(assembler.completeBatches.get(14)?.[0].multipv).toBe(1)
  })

  it('rejects score-only, PV-only, bounded, and depth-less lines', () => {
    const assembler = createSnapshotAssembler(1)
    feed(
      assembler,
      'info depth 14 score cp 30',
      'info depth 14 pv e2e4 e7e5',
      'info depth 14 score cp 62 lowerbound pv e2e4 e7e5',
      'info depth 14 score cp -18 upperbound pv e2e4 e7e5',
      'info score cp 30 pv e2e4 e7e5',
      'info depth 15 currmove e2e4 currmovenumber 1',
    )
    expect(assembler.completeBatches.size).toBe(0)
    expect(assembler.seq).toBe(0)
  })

  it('advances seq once per admitted line, in emission order', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 pv e2e4 e7e5',
      'info depth 14 score cp 62 lowerbound pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 pv d2d4 d7d5',
    )
    expect(assembler.completeBatches.get(14)?.map((slot) => slot.seq)).toEqual([0, 1])
  })
})

describe('batch assembly (§4.1)', () => {
  it('keeps a same-depth aspiration re-search from pairing across passes', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 1000 time 10 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 1200 time 12 pv d2d4 d7d5',
      // Re-search of the SAME depth with a different slot-1 score. Keying on
      // (depth, slot) would overwrite slot 1 and leave the old slot 2 beside it.
      'info depth 14 multipv 1 score cp 45 nodes 2000 time 20 pv e2e4 c7c5',
      'info depth 14 multipv 2 score cp 12 nodes 2200 time 22 pv d2d4 g8f6',
    )
    expect(assembler.completeBatches.size).toBe(1)
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([45, 12])
    expect(assembler.completeBatches.get(14)?.map((slot) => slot.pv)).toEqual([
      ['e2e4', 'c7c5'],
      ['d2d4', 'g8f6'],
    ])
    // The later complete batch replaced the earlier one outright, so no mixed
    // (45, 10) pairing exists anywhere.
    expect(getSnapshotCounters().batches_complete).toBe(2)
  })

  it('lets a bounded line neither open nor pollute a batch', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 1000 pv e2e4 e7e5',
      'info depth 14 multipv 1 score cp 62 lowerbound nodes 1100 pv e2e4 g8f6',
      'info depth 14 multipv 2 score cp 10 nodes 1200 pv d2d4 d7d5',
    )
    expect(assembler.completeBatches.size).toBe(1)
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([30, 10])
    expect(getSnapshotCounters().batches_complete).toBe(1)
  })

  it('opens a new batch per line at K=1, latest wins', () => {
    const assembler = createSnapshotAssembler(1)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 1000 pv e2e4 e7e5',
      'info depth 14 multipv 1 score cp 40 nodes 2000 pv d2d4 d7d5',
    )
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([40])
    expect(getSnapshotCounters().batches_complete).toBe(2)
  })

  it('falls back to the previous complete depth instead of pairing across depths', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 1000 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 1200 pv d2d4 d7d5',
      // Depth 15 never produced slot 2.
      'info depth 15 multipv 1 score cp 35 nodes 3000 pv g1f3 g8f6',
    )
    expect([...assembler.completeBatches.keys()]).toEqual([14])

    const selection = selectAtomicSnapshot({
      assembler,
      requestedDepth: 14,
      bestMove: 'e2e4',
      capFired: false,
      stopReason: 'bestmove',
    })
    expect(selection).toMatchObject({ accepted: true, depth: 14 })
    expect(selection.accepted && selection.slots.map((slot) => slot.pv[0])).toEqual([
      'e2e4',
      'd2d4',
    ])
  })

  it('never selects a partial batch left behind when the deadline fires', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 16 multipv 1 score cp 30 nodes 1000 pv e2e4 e7e5',
      'info depth 16 multipv 2 score cp 10 nodes 1200 pv d2d4 d7d5',
      'info depth 17 multipv 1 score cp 33 nodes 5000 pv e2e4 c7c5',
    )
    expect([...assembler.completeBatches.keys()]).toEqual([16])

    const selection = selectAtomicSnapshot({
      assembler,
      requestedDepth: 17,
      bestMove: 'e2e4',
      capFired: true,
      stopReason: 'deadline',
    })
    // The only complete batch is a depth behind, so the truncated search yields
    // nothing — the partial depth-17 slot is not a fallback.
    expect(selection).toEqual({ accepted: false, reason: 'stale-depth' })
  })

  it('rejects a batch whose nodes decrease in emission order', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 5000 time 50 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 4000 time 60 pv d2d4 d7d5',
    )
    expect(assembler.completeBatches.size).toBe(0)
    expect(getSnapshotCounters().batches_corroboration_failed).toBe(1)

    const selection = selectAtomicSnapshot({
      assembler,
      requestedDepth: 14,
      bestMove: 'e2e4',
      capFired: false,
      stopReason: 'bestmove',
    })
    expect(selection).toEqual({ accepted: false, reason: 'iteration-mismatch' })
  })

  it('rejects a batch whose time decreases in emission order', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 5000 time 90 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 5200 time 60 pv d2d4 d7d5',
    )
    expect(assembler.completeBatches.size).toBe(0)
    expect(getSnapshotCounters().batches_corroboration_failed).toBe(1)
  })

  it('accepts equal nodes across slots of one iteration', () => {
    // MultiPV lines share a search and report a RUNNING count, so equality is
    // normal — the rule is monotonicity, never identity.
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 5000 time 50 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 5000 time 50 pv d2d4 d7d5',
    )
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([30, 10])
  })

  it('assembles on emission order alone when nodes and time are absent', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 pv d2d4 d7d5',
    )
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([30, 10])
    expect(getSnapshotCounters().batches_corroboration_failed).toBe(0)
  })

  it('drops the pass a failed re-search superseded instead of falling back to it', () => {
    // The d14 re-search completed — it holds slots 1..K — so it replaces the pass
    // before it. Keeping only the OLD batch would hand the selector a superseded
    // result as the depth's latest, which is the cross-pass pairing §4.1 forbids.
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 5000 time 50 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 5200 time 52 pv d2d4 d7d5',
    )
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([30, 10])

    feed(
      assembler,
      'info depth 14 multipv 1 score cp 45 nodes 6000 time 60 pv e2e4 c7c5',
      'info depth 14 multipv 2 score cp 20 nodes 5500 time 62 pv d2d4 g8f6',
    )

    expect(assembler.completeBatches.has(14)).toBe(false)
    expect(assembler.iterationMismatches).toBe(1)
    expect(getSnapshotCounters().batches_corroboration_failed).toBe(1)
    expect(
      selectAtomicSnapshot({
        assembler,
        requestedDepth: 14,
        bestMove: 'e2e4',
        capFired: false,
        stopReason: 'bestmove',
      }),
    ).toEqual({ accepted: false, reason: 'iteration-mismatch' })
  })

  it('leaves other depths intact when one depth is poisoned', () => {
    // Only the re-searched depth is dropped: a shallower complete batch is still
    // a genuine iteration and remains the deepest thing the selector can use.
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 13 multipv 1 score cp 25 nodes 3000 time 30 pv e2e4 e7e5',
      'info depth 13 multipv 2 score cp 5 nodes 3100 time 31 pv d2d4 d7d5',
      'info depth 14 multipv 1 score cp 45 nodes 6000 time 60 pv e2e4 c7c5',
      'info depth 14 multipv 2 score cp 20 nodes 5500 time 62 pv d2d4 g8f6',
    )

    expect(assembler.completeBatches.has(14)).toBe(false)
    expect(
      selectAtomicSnapshot({
        assembler,
        requestedDepth: 13,
        bestMove: 'e2e4',
        capFired: false,
        stopReason: 'bestmove',
        requiredMoves: ['e2e4', 'd2d4'],
      }),
    ).toMatchObject({ accepted: true, depth: 13 })
  })

  it('does not re-store a completed batch when a further slot arrives', () => {
    const assembler = createSnapshotAssembler(2)
    feed(
      assembler,
      'info depth 14 multipv 1 score cp 30 nodes 100 pv e2e4 e7e5',
      'info depth 14 multipv 2 score cp 10 nodes 200 pv d2d4 d7d5',
      'info depth 14 multipv 3 score cp 5 nodes 300 pv g1f3 g8f6',
    )
    expect(scoresOf(assembler.completeBatches.get(14))).toEqual([30, 10])
    expect(getSnapshotCounters().batches_complete).toBe(1)
  })
})

describe('selectAtomicSnapshot (§4.2)', () => {
  it('accepts an exact slot at the requested depth whose PV starts with bestmove', () => {
    const selection = selectFrom([[17, [snapshot()]]])
    expect(selection).toMatchObject({ accepted: true, depth: 17 })
  })

  it('prefers the deepest acceptable batch', () => {
    const selection = selectFrom([
      [17, [snapshot({ depth: 17, score: cp(30) })]],
      [16, [snapshot({ depth: 16, score: cp(80) })]],
    ])
    expect(selection.accepted && selection.depth).toBe(17)
  })

  describe('named rejections', () => {
    it('no-slot: the batch does not hold the required slot', () => {
      expect(rejectionOf(selectFrom([[17, [snapshot({ multipv: 2 })]]]))).toBe('no-slot')
    })

    it('bounded: a slot that is not exact', () => {
      expect(rejectionOf(selectFrom([[17, [snapshot({ bound: 'lower' })]]]))).toBe('bounded')
    })

    it('stale-depth: the deepest complete batch is below the requested depth', () => {
      expect(rejectionOf(selectFrom([[14, [snapshot({ depth: 14 })]]]))).toBe('stale-depth')
    })

    it('pv-mismatch: slot 1 does not start with the engine bestmove', () => {
      const selection = selectFrom([[17, [snapshot({ pv: ['d2d4', 'd7d5'] })]]])
      expect(rejectionOf(selection)).toBe('pv-mismatch')
    })

    it('pv-short: a one-move PV whose first move does not end the game', () => {
      expect(rejectionOf(selectFrom([[17, [snapshot({ pv: ['e2e4'] })]]]))).toBe('pv-short')
    })

    it('slot-set-mismatch: the batch does not cover exactly the required moves', () => {
      const selection = selectFrom(
        [
          [
            17,
            [
              snapshot({ multipv: 1, pv: ['e2e4', 'e7e5'], score: cp(30) }),
              snapshot({ multipv: 2, pv: ['g1f3', 'g8f6'], score: cp(10) }),
            ],
          ],
        ],
        { requiredSlots: 2, requiredMoves: ['e2e4', 'd2d4'] },
      )
      expect(rejectionOf(selection)).toBe('slot-set-mismatch')
    })

    it('iteration-mismatch: no complete batch at any depth', () => {
      expect(rejectionOf(selectFrom([]))).toBe('iteration-mismatch')
    })

    it('slot-order-disagreement: engine slot order contradicts the comparator', () => {
      const selection = selectFrom(
        [
          [
            17,
            [
              snapshot({ multipv: 1, pv: ['e2e4', 'e7e5'], score: cp(10) }),
              snapshot({ multipv: 2, pv: ['d2d4', 'd7d5'], score: cp(50) }),
            ],
          ],
        ],
        { requiredSlots: 2, requiredMoves: ['e2e4', 'd2d4'] },
      )
      expect(rejectionOf(selection)).toBe('slot-order-disagreement')
    })

    it('mate-zero: a root-frame mate 0', () => {
      expect(rejectionOf(selectFrom([[17, [snapshot({ score: mate(0) })]]]))).toBe('mate-zero')
    })

    it('gives every named rejection its own counter', () => {
      const twoSlots = { requiredSlots: 2, requiredMoves: ['e2e4', 'd2d4'] }
      const triggers: Record<SnapshotRejection, () => void> = {
        'no-slot': () => selectFrom([[17, [snapshot({ multipv: 2 })]]]),
        bounded: () => selectFrom([[17, [snapshot({ bound: 'upper' })]]]),
        'stale-depth': () => selectFrom([[14, [snapshot({ depth: 14 })]]]),
        'pv-mismatch': () => selectFrom([[17, [snapshot({ pv: ['d2d4', 'd7d5'] })]]]),
        'pv-short': () => selectFrom([[17, [snapshot({ pv: ['e2e4'] })]]]),
        'slot-set-mismatch': () =>
          selectFrom(
            [
              [
                17,
                [
                  snapshot({ multipv: 1, score: cp(30) }),
                  snapshot({ multipv: 2, pv: ['g1f3', 'g8f6'], score: cp(10) }),
                ],
              ],
            ],
            twoSlots,
          ),
        'iteration-mismatch': () => selectFrom([]),
        'slot-order-disagreement': () =>
          selectFrom(
            [
              [
                17,
                [
                  snapshot({ multipv: 1, score: cp(10) }),
                  snapshot({ multipv: 2, pv: ['d2d4', 'd7d5'], score: cp(50) }),
                ],
              ],
            ],
            twoSlots,
          ),
        'mate-zero': () => selectFrom([[17, [snapshot({ score: mate(0) })]]]),
      }

      // Every name in the exported set has a trigger, and firing it moves that
      // counter and only that counter.
      expect(Object.keys(triggers).sort()).toEqual([...SNAPSHOT_REJECTIONS].sort())
      for (const reason of SNAPSHOT_REJECTIONS) {
        resetSnapshotCounters()
        triggers[reason]()
        expect(getSnapshotCounters().rejections).toEqual({
          ...zeroedRejections(),
          [reason]: 1,
        })
      }
    })
  })

  describe('depth and PV exemptions', () => {
    it('accepts an untruncated forced mate that ended below the requested depth', () => {
      const selection = selectFrom([[12, [snapshot({ depth: 12, score: mate(3) })]]], {
        capFired: false,
        stopReason: 'bestmove',
      })
      expect(selection).toMatchObject({ accepted: true, depth: 12 })
    })

    it('denies the mate exemption to a capped search', () => {
      const selection = selectFrom([[12, [snapshot({ depth: 12, score: mate(3) })]]], {
        capFired: true,
        stopReason: 'deadline',
      })
      expect(rejectionOf(selection)).toBe('stale-depth')
    })

    it('denies the mate exemption to a below-depth centipawn score', () => {
      const selection = selectFrom([[12, [snapshot({ depth: 12, score: cp(300) })]]])
      expect(rejectionOf(selection)).toBe('stale-depth')
    })

    it('accepts a one-move PV whose first move ends the game', () => {
      const selection = selectFrom([[17, [snapshot({ pv: ['d1h5'] })]]], {
        bestMove: 'd1h5',
        endsGame: (uci) => uci === 'd1h5',
      })
      expect(selection).toMatchObject({ accepted: true })
    })

    it('accepts a batch deeper than requested', () => {
      const selection = selectFrom([[18, [snapshot({ depth: 18 })]]])
      expect(selection).toMatchObject({ accepted: true, depth: 18 })
    })
  })

  describe('restricted two-slot grading', () => {
    const restricted = (slot1: Partial<PvSnapshot>, slot2: Partial<PvSnapshot>) =>
      selectFrom(
        [
          [
            17,
            [
              snapshot({ multipv: 1, pv: ['e2e4', 'e7e5'], score: cp(40), ...slot1 }),
              snapshot({ multipv: 2, pv: ['d2d4', 'd7d5'], score: cp(15), ...slot2 }),
            ],
          ],
        ],
        { requiredSlots: 2, requiredMoves: ['d2d4', 'e2e4'] },
      )

    it('accepts slots covering exactly {B, P} in comparator order', () => {
      const selection = restricted({}, {})
      expect(selection).toMatchObject({ accepted: true, depth: 17 })
      expect(selection.accepted && selection.slots.map((slot) => slot.pv[0])).toEqual([
        'e2e4',
        'd2d4',
      ])
    })

    it('rejects a duplicated move across the two slots', () => {
      expect(rejectionOf(restricted({}, { pv: ['e2e4', 'c7c5'] }))).toBe('slot-set-mismatch')
    })

    it('refuses to pair slots from different depths under the forced-mate exemption', () => {
      // Both slots are mates that ended normally below target, so each passes the
      // per-slot depth check on its own — the only route to a cross-depth pairing.
      const selection = selectFrom(
        [
          [
            15,
            [
              snapshot({ multipv: 1, depth: 15, score: mate(2) }),
              snapshot({ multipv: 2, depth: 14, pv: ['d2d4', 'd7d5'], score: mate(4) }),
            ],
          ],
        ],
        { requiredSlots: 2, requiredMoves: ['d2d4', 'e2e4'] },
      )
      expect(rejectionOf(selection)).toBe('iteration-mismatch')
    })

    it('reports the deepest batch failure rather than a shallower one', () => {
      const selection = selectFrom(
        [
          [17, [snapshot({ depth: 17, pv: ['d2d4', 'd7d5'] })]],
          [16, [snapshot({ depth: 16 })]],
        ],
        { requestedDepth: 16 },
      )
      // Depth 17 fails pv-mismatch; depth 16 would fail nothing but is shallower,
      // so it is tried second and accepted.
      expect(selection).toMatchObject({ accepted: true, depth: 16 })
    })

    it('reports the deepest reason when no depth is acceptable', () => {
      const selection = selectFrom([
        [17, [snapshot({ depth: 17, pv: ['d2d4', 'd7d5'] })]],
        [16, [snapshot({ depth: 16 })]],
      ])
      expect(rejectionOf(selection)).toBe('pv-mismatch')
    })
  })
})

describe('recordLegacySelectorDivergence (§4.3)', () => {
  const request = (
    slots: PvSnapshot[],
    overrides: Partial<Omit<AtomicSelectionRequest, 'assembler'>> = {},
  ): AtomicSelectionRequest => ({
    assembler: { requiredSlots: 1, completeBatches: new Map([[17, slots]]) },
    requestedDepth: 17,
    bestMove: 'e2e4',
    capFired: false,
    stopReason: 'bestmove',
    ...overrides,
  })

  it('counts nothing when both selectors agree', () => {
    recordLegacySelectorDivergence(request([snapshot()]), {
      score: cp(30),
      pv: ['e2e4', 'e7e5'],
      reachedDepth: 17,
    })
    expect(getSnapshotCounters().legacy_selector_divergence).toBe(0)
  })

  it('counts a value disagreement under `accepted`', () => {
    // The legacy accumulators stapled a later iteration's score onto this PV.
    recordLegacySelectorDivergence(request([snapshot()]), {
      score: cp(55),
      pv: ['e2e4', 'e7e5'],
      reachedDepth: 17,
    })
    const counters = getSnapshotCounters()
    expect(counters.legacy_selector_divergence).toBe(1)
    expect(counters.divergence_by_reason.accepted).toBe(1)
  })

  it('counts a depth disagreement, which is the accumulator hazard', () => {
    // lastDepth tracks any info line, including a currmove line for an iteration
    // that never produced a PV.
    recordLegacySelectorDivergence(request([snapshot()]), {
      score: cp(30),
      pv: ['e2e4', 'e7e5'],
      reachedDepth: 18,
    })
    expect(getSnapshotCounters().legacy_selector_divergence).toBe(1)
    expect(getSnapshotCounters().divergence_by_reason.accepted).toBe(1)
  })

  it('splits a rejected row by its rejection reason', () => {
    recordLegacySelectorDivergence(request([snapshot({ pv: ['e2e4'] })]), {
      score: cp(30),
      pv: ['e2e4'],
      reachedDepth: 17,
    })
    const counters = getSnapshotCounters()
    expect(counters.legacy_selector_divergence).toBe(1)
    expect(counters.divergence_by_reason['pv-short']).toBe(1)
    expect(counters.rejections['pv-short']).toBe(1)
  })

  it('counts a legacy row the atomic selector could not have produced at all', () => {
    recordLegacySelectorDivergence(
      {
        assembler: { requiredSlots: 1, completeBatches: new Map() },
        requestedDepth: 17,
        bestMove: 'e2e4',
        capFired: false,
        stopReason: 'bestmove',
      },
      { score: cp(30), pv: ['e2e4', 'e7e5'], reachedDepth: 17 },
    )
    expect(getSnapshotCounters().divergence_by_reason['iteration-mismatch']).toBe(1)
  })

  it('counts a legacy row that produced no score', () => {
    recordLegacySelectorDivergence(request([snapshot()]), {
      score: null,
      pv: null,
      reachedDepth: null,
    })
    expect(getSnapshotCounters().legacy_selector_divergence).toBe(1)
  })

  it('tallies a canceled search on its own, touching no failure counter', () => {
    recordCanceledSearch()
    const counters = getSnapshotCounters()
    expect(counters.searches_canceled).toBe(1)
    expect(counters.legacy_selector_divergence).toBe(0)
    expect(counters.rejections).toEqual(zeroedRejections())
  })
})
