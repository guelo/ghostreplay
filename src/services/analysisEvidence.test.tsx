import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, render, act } from '@testing-library/react'
import { useEffect, useLayoutEffect } from 'react'
import {
  deriveEvidenceRow,
  completedSearchSignature,
  useAnalysisEvidence,
  type EvidenceReuseContext,
} from './analysisEvidence'
import type {
  CompletedRootAnalysis,
  EngineInfo,
  EngineScore,
} from '../workers/stockfishMessages'

const submitAnalysisEvidenceMock = vi.fn()
vi.mock('../utils/api', () => ({
  submitAnalysisEvidence: (...args: unknown[]) => submitAnalysisEvidenceMock(...args),
}))

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
// Black to move (after 1. e4), 20 legal moves.
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
// White to move, exactly 1 legal move (Kxg2).
const FEN_1LEGAL = '7k/8/8/8/8/8/6q1/7K w - - 0 1'
// White to move, exactly 2 legal moves (Kh2 / Kf1).
const FEN_2LEGAL = '8/8/8/8/8/5k2/7r/6K1 w - - 0 1'

const cp = (value: number): EngineScore => ({ type: 'cp', value })
const mate = (value: number): EngineScore => ({ type: 'mate', value })
const line = (
  multipv: number,
  pv: string[],
  score: EngineScore,
  depth = 21,
): EngineInfo => ({ depth, multipv, pv, score })

// Default snapshot: black to move at AFTER_E4, unrestricted depth-21 MultiPV-3.
const snap = (o: Partial<CompletedRootAnalysis> = {}): CompletedRootAnalysis => ({
  requestId: 'r',
  fen: AFTER_E4,
  bestMove: 'e7e5',
  lines: [
    line(1, ['e7e5', 'g1f3'], cp(20)),
    line(2, ['c7c5', 'g1f3'], cp(10)),
    line(3, ['d7d5', 'g1f3'], cp(5)),
  ],
  limit: { type: 'depth', value: 21 },
  multipv: 3,
  searchmoves: null,
  ...o,
})

const rowOf = (r: ReturnType<typeof deriveEvidenceRow>) => {
  if (!('row' in r)) throw new Error(`expected a row, got skip=${JSON.stringify(r)}`)
  return r.row
}

// --------------------------------------------------------------------------- #
// deriveEvidenceRow — eligibility gate + atomic row
// --------------------------------------------------------------------------- #
describe('deriveEvidenceRow', () => {
  it('played move on line 1 builds best with equal evals and zero delta', () => {
    const row = rowOf(deriveEvidenceRow(snap(), AFTER_E4, 'e7e5'))
    expect(row.classification).toBe('best')
    expect(row.best_move_uci).toBe('e7e5')
    expect(row.eval_delta).toBe(0)
    expect(row.played_eval).toBe(row.best_eval)
    expect(row.best_line_uci).toEqual(['e7e5', 'g1f3'])
  })

  it('played move on a lower line uses same-search scores (black to move)', () => {
    const row = rowOf(deriveEvidenceRow(snap(), AFTER_E4, 'c7c5'))
    // Black mover: white-relative best = -20 (line1 cp20), played = -10 (line2 cp10).
    expect(row.best_eval).toBe(-20)
    expect(row.played_eval).toBe(-10)
    // Black to move: delta = playedWhite - bestWhite = -10 - (-20) = 10.
    expect(row.eval_delta).toBe(10)
    expect(row.best_move_uci).toBe('e7e5')
    expect(row.classification).not.toBe('best')
  })

  it('white-to-move root: evals stay white-relative and delta = best - played', () => {
    const s = snap({
      fen: START,
      bestMove: 'e2e4',
      lines: [
        line(1, ['e2e4', 'e7e5'], cp(40)),
        line(2, ['g1f3', 'g8f6'], cp(25)),
        line(3, ['d2d4', 'd7d5'], cp(20)),
      ],
    })
    const row = rowOf(deriveEvidenceRow(s, START, 'g1f3'))
    expect(row.best_eval).toBe(40)
    expect(row.played_eval).toBe(25)
    expect(row.eval_delta).toBe(15)
  })

  it('converts mate scores to white-relative (black to move)', () => {
    const s = snap({
      bestMove: 'e7e5',
      lines: [
        line(1, ['e7e5', 'g1f3'], mate(2)),
        line(2, ['c7c5', 'g1f3'], cp(10)),
        line(3, ['d7d5', 'g1f3'], cp(5)),
      ],
    })
    const row = rowOf(deriveEvidenceRow(s, AFTER_E4, 'e7e5'))
    // Black mover -> white-relative mate count sign flips.
    expect(row.best_eval_mate).toBe(-2)
    expect(row.played_eval_mate).toBe(-2)
  })

  it('skips a played move absent from all lines (unrepresented, no row)', () => {
    const r = deriveEvidenceRow(snap(), AFTER_E4, 'b8c6')
    expect(r).toEqual({ skip: 'unrepresented' })
  })

  it('skips a restricted searchmoves / MultiPV-2 hybrid result', () => {
    const restricted = snap({ multipv: 2, searchmoves: ['c7c5'] })
    expect(deriveEvidenceRow(restricted, AFTER_E4, 'e7e5')).toEqual({ skip: 'restricted' })
    const mp2 = snap({ multipv: 2 })
    expect(deriveEvidenceRow(mp2, AFTER_E4, 'e7e5')).toEqual({ skip: 'restricted' })
  })

  it('skips a non-depth-21 search', () => {
    const movetime = snap({ limit: { type: 'movetime', value: 1200 }, multipv: 1 })
    expect(deriveEvidenceRow(movetime, AFTER_E4, 'e7e5')).toEqual({ skip: 'restricted' })
    const d20 = snap({ limit: { type: 'depth', value: 20 } })
    expect(deriveEvidenceRow(d20, AFTER_E4, 'e7e5')).toEqual({ skip: 'restricted' })
  })

  it('skips a snapshot whose FEN differs from the target position (stale)', () => {
    expect(deriveEvidenceRow(snap({ fen: START }), AFTER_E4, 'e7e5')).toEqual({ skip: 'stale' })
  })

  it('requires all three dense slots at a 3+-legal position', () => {
    const twoSlots = snap({ lines: [line(1, ['e7e5', 'g1f3'], cp(20)), line(2, ['c7c5', 'g1f3'], cp(10))] })
    expect(deriveEvidenceRow(twoSlots, AFTER_E4, 'e7e5')).toEqual({ skip: 'sparse' })
  })

  it('rejects a mixed-depth snapshot (a slot below terminal depth)', () => {
    const mixed = snap({
      lines: [
        line(1, ['e7e5', 'g1f3'], cp(20)),
        line(2, ['c7c5', 'g1f3'], cp(10), 20), // shallow
        line(3, ['d7d5', 'g1f3'], cp(5)),
      ],
    })
    expect(deriveEvidenceRow(mixed, AFTER_E4, 'e7e5')).toEqual({ skip: 'sparse' })
  })

  it('is eligible at a 1-legal-move position with a single dense slot', () => {
    const s = snap({
      fen: FEN_1LEGAL,
      bestMove: 'h1g2',
      lines: [line(1, ['h1g2', 'h8g8'], cp(900))],
    })
    const row = rowOf(deriveEvidenceRow(s, FEN_1LEGAL, 'h1g2'))
    expect(row.classification).toBe('best')
    expect(row.move_uci).toBe('h1g2')
  })

  it('is eligible at a 2-legal-move position with two dense slots', () => {
    const s = snap({
      fen: FEN_2LEGAL,
      bestMove: 'g1h2',
      lines: [line(1, ['g1h2', 'f3f2'], cp(0)), line(2, ['g1f1', 'h2h1'], cp(-50))],
    })
    const row = rowOf(deriveEvidenceRow(s, FEN_2LEGAL, 'g1f1'))
    expect(row.best_move_uci).toBe('g1h2')
    expect(row.move_uci).toBe('g1f1')
  })

  it('rejects when line 1 has only a single-move PV', () => {
    const s = snap({ lines: [line(1, ['e7e5'], cp(20)), line(2, ['c7c5', 'g1f3'], cp(10)), line(3, ['d7d5', 'g1f3'], cp(5))] })
    // Line 1 fails the multi-move-PV gate; it is also not a dense slot count issue.
    const r = deriveEvidenceRow(s, AFTER_E4, 'e7e5')
    expect('skip' in r).toBe(true)
  })

  it('rejects when line 1 does not begin with bestMove', () => {
    const s = snap({ bestMove: 'c7c5' }) // line 1 pv[0] is e7e5, not c7c5
    expect(deriveEvidenceRow(s, AFTER_E4, 'e7e5')).toEqual({ skip: 'incomplete' })
  })
})

// --------------------------------------------------------------------------- #
// completedSearchSignature — content signature
// --------------------------------------------------------------------------- #
describe('completedSearchSignature', () => {
  it('differs when the played-line score differs, even with same fen/options/bestMove', () => {
    const s1 = snap()
    const s2 = snap({
      lines: [line(1, ['e7e5', 'g1f3'], cp(20)), line(2, ['c7c5', 'g1f3'], cp(1)), line(3, ['d7d5', 'g1f3'], cp(5))],
    })
    const r1 = rowOf(deriveEvidenceRow(s1, AFTER_E4, 'c7c5'))
    const r2 = rowOf(deriveEvidenceRow(s2, AFTER_E4, 'c7c5'))
    expect(completedSearchSignature('s1', s1, r1)).not.toBe(
      completedSearchSignature('s1', s2, r2),
    )
  })

  it('is stable for the same completed search', () => {
    const s = snap()
    const r = rowOf(deriveEvidenceRow(s, AFTER_E4, 'e7e5'))
    expect(completedSearchSignature('s1', s, r)).toBe(completedSearchSignature('s1', s, r))
  })

  it('never collides across sessions', () => {
    const s = snap()
    const r = rowOf(deriveEvidenceRow(s, AFTER_E4, 'e7e5'))
    expect(completedSearchSignature('s1', s, r)).not.toBe(
      completedSearchSignature('s2', s, r),
    )
  })
})

// --------------------------------------------------------------------------- #
// useAnalysisEvidence — submission + dedupe lifecycle
// --------------------------------------------------------------------------- #
const ctx = (o: Partial<EvidenceReuseContext> = {}): EvidenceReuseContext => ({
  fenBefore: AFTER_E4,
  moveUci: 'e7e5',
  isMainline: true,
  engineEnabled: true,
  ...o,
})

describe('useAnalysisEvidence', () => {
  beforeEach(() => {
    submitAnalysisEvidenceMock.mockReset()
    submitAnalysisEvidenceMock.mockResolvedValue([])
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  const flush = async () => {
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
  }

  it('submits the derived row for an eligible completed search', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => {
      result.current.considerCompletedSearch(snap(), ctx())
    })
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(1)
    const [sessionId, rows] = submitAnalysisEvidenceMock.mock.calls[0]
    expect(sessionId).toBe('s1')
    expect(rows[0]).toMatchObject({ fen: AFTER_E4, move_uci: 'e7e5', best_move_uci: 'e7e5' })
  })

  it('no-ops without a sessionId', async () => {
    const { result } = renderHook(() => useAnalysisEvidence(undefined))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('no-ops when engine analysis is disabled', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx({ engineEnabled: false })))
    await flush()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('no-ops inside a variation', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx({ isMainline: false })))
    await flush()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('no-ops with a null wire key (legacy move)', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx({ moveUci: null })))
    await flush()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('does not submit an unrepresented played move', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx({ moveUci: 'b8c6' })))
    await flush()
    expect(submitAnalysisEvidenceMock).not.toHaveBeenCalled()
  })

  it('dedupes: an identical resolved signature does not resubmit', async () => {
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(1)
  })

  it('in-flight exclusion: a second call before the POST resolves launches no second request', async () => {
    let resolveSubmit: (v: unknown[]) => void = () => {}
    submitAnalysisEvidenceMock.mockReturnValue(
      new Promise((r) => {
        resolveSubmit = r as (v: unknown[]) => void
      }),
    )
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(1)
    await act(async () => {
      resolveSubmit([])
      await Promise.resolve()
    })
  })

  it('retry: a network failure clears in-flight so a later completion resubmits', async () => {
    submitAnalysisEvidenceMock.mockRejectedValueOnce(new Error('network'))
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    submitAnalysisEvidenceMock.mockResolvedValueOnce([])
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(2)
  })

  it('terminal: an HTTP-200 rejection does not resubmit', async () => {
    submitAnalysisEvidenceMock.mockResolvedValue([
      { fen: AFTER_E4, move_uci: 'e7e5', reason: 'incompatible_keep', upgrade: null },
    ])
    const { result } = renderHook(() => useAnalysisEvidence('s1'))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(1)
  })

  it('a sessionId change clears the dedupe sets', async () => {
    const { result, rerender } = renderHook(
      ({ sid }: { sid: string }) => useAnalysisEvidence(sid),
      { initialProps: { sid: 's1' } },
    )
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    rerender({ sid: 's2' })
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(2)
    expect(submitAnalysisEvidenceMock.mock.calls[1][0]).toBe('s2')
  })

  const upgrade = {
    classification: 'best' as const,
    eval_cp: 20,
    eval_mate: null,
    best_move_san: 'e5',
    best_move_eval_cp: 20,
    eval_delta: 0,
    authoritative: false,
  }

  it('ignores a POST that resolves after an in-place session change', async () => {
    // A submission launched under s1 must not touch s2's dedupe state or invoke
    // s2's onAcceptedEvidence when it completes late (g-reuse-d21-search P2).
    let resolveSubmit: (v: unknown[]) => void = () => {}
    submitAnalysisEvidenceMock.mockReturnValueOnce(
      new Promise((r) => {
        resolveSubmit = r as (v: unknown[]) => void
      }),
    )
    const onAccepted = vi.fn()
    const { result, rerender } = renderHook(
      ({ sid }: { sid: string }) => useAnalysisEvidence(sid, onAccepted),
      { initialProps: { sid: 's1' } },
    )
    // Launch under s1 (POST hangs), then switch to s2 before it resolves.
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    rerender({ sid: 's2' })
    // The stale s1 POST now completes with an accepted upgrade.
    await act(async () => {
      resolveSubmit([{ fen: AFTER_E4, move_uci: 'e7e5', reason: 'new_key', upgrade }])
      await flush()
    })
    // Stale completion is dropped: s2's callback never fires...
    expect(onAccepted).not.toHaveBeenCalled()
    // ...and the s1 signature did not leak into s2's terminal set, so a genuine
    // s2 completion of the same content still submits and fires.
    submitAnalysisEvidenceMock.mockResolvedValueOnce([
      { fen: AFTER_E4, move_uci: 'e7e5', reason: 'new_key', upgrade },
    ])
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(submitAnalysisEvidenceMock).toHaveBeenCalledTimes(2)
    expect(onAccepted).toHaveBeenCalledWith(AFTER_E4, 'e7e5', upgrade)
  })

  it('drops a POST that settles at the session-change commit', async () => {
    // Tighter than the stale test above: the s1 POST is settled from a sibling LAYOUT
    // effect at the exact s2 commit, so the completion runs at the commit boundary
    // rather than after all effects have flushed. Asserts the generation guard is
    // already armed at that point (the callback is dropped).
    //
    // NOTE: this cannot by itself prove the invalidation is a LAYOUT effect rather
    // than a passive one — RTL's `act` eagerly flushes passive effects at the commit
    // boundary, collapsing the very timing gap that motivates useLayoutEffect. That
    // window is browser-only (React defers passive effects past commit) and is why
    // the invalidation uses useLayoutEffect; see the hook. What this test DOES pin is
    // the guard mechanism itself: remove the generation check and it fails.
    let resolveSubmit: (v: unknown[]) => void = () => {}
    submitAnalysisEvidenceMock.mockReturnValueOnce(
      new Promise((r) => {
        resolveSubmit = r as (v: unknown[]) => void
      }),
    )
    const onAccepted = vi.fn()
    const captured: {
      fn?: (s: CompletedRootAnalysis, c: EvidenceReuseContext) => void
    } = {}

    function Harness({ sid }: { sid: string }) {
      const { considerCompletedSearch } = useAnalysisEvidence(sid, onAccepted)
      useEffect(() => {
        captured.fn = considerCompletedSearch
      }, [considerCompletedSearch])
      // Settle the hanging s1 POST during the s2 commit — a completion at the commit
      // boundary rather than after all effects have flushed.
      useLayoutEffect(() => {
        if (sid === 's2') {
          resolveSubmit([{ fen: AFTER_E4, move_uci: 'e7e5', reason: 'new_key', upgrade }])
        }
      }, [sid])
      return null
    }

    const { rerender } = render(<Harness sid="s1" />)
    await act(async () => captured.fn?.(snap(), ctx())) // launch s1 POST (hangs)
    await act(async () => {
      rerender(<Harness sid="s2" />)
      await flush()
    })
    expect(onAccepted).not.toHaveBeenCalled()
  })

  it('fires onAcceptedEvidence with the endpoint upgrade on an accepted write', async () => {
    const onAccepted = vi.fn()
    submitAnalysisEvidenceMock.mockResolvedValue([
      { fen: AFTER_E4, move_uci: 'e7e5', reason: 'new_key', upgrade },
    ])
    const { result } = renderHook(() => useAnalysisEvidence('s1', onAccepted))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(onAccepted).toHaveBeenCalledWith(AFTER_E4, 'e7e5', upgrade)
  })

  it('does not fire onAcceptedEvidence when the write carries no upgrade', async () => {
    const onAccepted = vi.fn()
    submitAnalysisEvidenceMock.mockResolvedValue([
      { fen: AFTER_E4, move_uci: 'e7e5', reason: 'incompatible_keep', upgrade: null },
    ])
    const { result } = renderHook(() => useAnalysisEvidence('s1', onAccepted))
    await act(async () => result.current.considerCompletedSearch(snap(), ctx()))
    await flush()
    expect(onAccepted).not.toHaveBeenCalled()
  })
})
