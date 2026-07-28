import { describe, expect, it } from 'vitest'
import type { EngineScore } from '../stockfishMessages'
import type { CandidateContext, CandidateSearchOptions } from './contract'
import { MAX_ANALYZE_MOVE_DEPTH_EXCLUSIVE, variantA } from './variantA'

/**
 * The arm against a fake context, so its branch matrix is covered without
 * driving the whole worker.
 *
 * The worker-level tests pin the UCI stream and the response fields; this pins
 * what the arm ASKS FOR and what it hands the shared tail — which is where a
 * silent extra search or a wrongly reused score would live.
 */

type Call = { moves: string[]; options: CandidateSearchOptions | undefined }

const cp = (value: number): EngineScore => ({ type: 'cp', value })

const fakeContext = (overrides: {
  playedMove: string
  results: Array<{ bestmove: string; score?: EngineScore | null; pv?: string[] | null; capFired?: boolean; reachedDepth?: number | null }>
  terminal?: Record<string, EngineScore>
  requestedDepth?: number
}) => {
  const calls: Call[] = []
  const streamed: Array<{ score: EngineScore; depth: number }> = []
  let cancelChecks = 0
  const results = [...overrides.results]

  const context: CandidateContext = {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    playedMove: overrides.playedMove,
    playerColor: 'white',
    sideToMove: 'w',
    requestedDepth: overrides.requestedDepth ?? 17,
    search: (moves, options) => {
      calls.push({ moves, options })
      const next = results.shift()
      if (!next) throw new Error('the arm ran more searches than the fixture allows')
      return Promise.resolve({
        bestmove: next.bestmove,
        score: next.score ?? null,
        pv: next.pv ?? null,
        capFired: next.capFired ?? false,
        reachedDepth: next.reachedDepth ?? null,
      })
    },
    checkCanceled: () => {
      cancelChecks += 1
    },
    terminalScoreAfterMove: (move) => overrides.terminal?.[move] ?? null,
    streamPlayed: (score, depth) => {
      streamed.push({ score, depth })
    },
  }

  return { context, calls, streamed, cancelChecks: () => cancelChecks, remaining: () => results.length }
}

describe('variantA (minimal, §3.1)', () => {
  it('runs the root at N+1 and the played move at N, and nothing else', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e3',
      results: [
        { bestmove: 'e2e4', score: cp(30), pv: ['e2e4', 'e7e5'], reachedDepth: 18 },
        { bestmove: 'e7e5', score: cp(-12), pv: ['e7e5'] },
      ],
    })

    const outcome = await variantA.run(fixture.context)

    expect(fixture.calls.map((call) => call.moves)).toEqual([[], ['e2e3']])
    expect(fixture.calls[0].options?.depth).toBe(18)
    expect(fixture.calls[1].options?.depth).toBe(17)
    // The whole point of the variant: no third search on the P !== B split.
    expect(fixture.remaining()).toBe(0)
    expect(outcome.reachedDepth).toBe(18)
    expect(outcome.rootPv).toEqual(['e2e4', 'e7e5'])
  })

  it('declines to grade a P !== B row rather than comparing two frames', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e3',
      results: [
        { bestmove: 'e2e4', score: cp(30), pv: ['e2e4', 'e7e5'] },
        { bestmove: 'e7e5', score: cp(-12), pv: ['e7e5', 'g1f3'] },
      ],
    })

    const outcome = await variantA.run(fixture.context)

    // §5 normalization lands in g-grade-variant-b; an inline conversion here
    // would be a sign inversion plus a mate-distance error.
    expect(outcome.postBestScore).toBeNull()
    expect(outcome.postPlayedScore).toEqual(cp(-12))
    // The continuation is a line after the PLAYED move, not after B.
    expect(outcome.continuationPv).toBeNull()
  })

  it('reuses the post-played search as the post-best score when P === B', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      results: [
        { bestmove: 'e2e4', score: cp(30), pv: ['e2e4', 'e7e5'] },
        { bestmove: 'e7e5', score: cp(-20), pv: ['e7e5', 'g1f3'] },
      ],
    })

    const outcome = await variantA.run(fixture.context)

    // §3.1 keeps the post-played search on this split: it supplies the
    // resulting-position eval and the streaming updates. Same position, same
    // search — not a second measurement of it.
    expect(fixture.calls.map((call) => call.moves)).toEqual([[], ['e2e4']])
    expect(outcome.postBestScore).toEqual(cp(-20))
    expect(outcome.postBestScore).toBe(outcome.postPlayedScore)
    expect(outcome.continuationPv).toEqual(['e7e5', 'g1f3'])
  })

  it('streams the played search, and only it', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      results: [
        { bestmove: 'e2e4', score: cp(30) },
        { bestmove: 'e7e5', score: cp(-20) },
      ],
    })

    await variantA.run(fixture.context)

    expect(fixture.calls[0].options?.onInfo).toBeUndefined()
    expect(fixture.calls[1].options?.onInfo).toBe(fixture.context.streamPlayed)
  })

  it('never searches a terminal played move', async () => {
    const fixture = fakeContext({
      playedMove: 'a1a8',
      results: [{ bestmove: 'a1a8', score: { type: 'mate', value: 1 }, pv: ['a1a8'] }],
      terminal: { a1a8: { type: 'mate', value: 0 } },
    })

    const outcome = await variantA.run(fixture.context)

    expect(fixture.calls.map((call) => call.moves)).toEqual([[]])
    expect(outcome.postPlayedScore).toEqual({ type: 'mate', value: 0 })
    // P === B, so the exact terminal score serves both sides.
    expect(outcome.postBestScore).toEqual({ type: 'mate', value: 0 })
  })

  it('returns (none) without running a second search', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      results: [{ bestmove: '(none)', reachedDepth: 18 }],
    })

    const outcome = await variantA.run(fixture.context)

    expect(fixture.calls).toHaveLength(1)
    expect(outcome).toMatchObject({
      bestMove: '(none)',
      postPlayedScore: null,
      postBestScore: null,
      reachedDepth: 18,
    })
  })

  it('folds a capped root into the outcome', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      results: [
        { bestmove: 'e2e4', score: cp(30), capFired: true },
        { bestmove: 'e7e5', score: cp(-20) },
      ],
    })

    expect((await variantA.run(fixture.context)).capFired).toBe(true)
  })

  it('folds a capped post-played search into the outcome', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      results: [
        { bestmove: 'e2e4', score: cp(30) },
        { bestmove: 'e7e5', score: cp(-20), capFired: true },
      ],
    })

    expect((await variantA.run(fixture.context)).capFired).toBe(true)
  })

  it('refuses a request whose N+1 would break §2’s depth invariant', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e4',
      requestedDepth: MAX_ANALYZE_MOVE_DEPTH_EXCLUSIVE - 1,
      results: [{ bestmove: 'e2e4' }],
    })

    // Throwing makes the runner record a per-row error instead of measuring a
    // protocol nobody asked for.
    await expect(variantA.run(fixture.context)).rejects.toThrow(/depth invariant/)
    expect(fixture.calls).toHaveLength(0)
  })

  it('checks cancellation between phases', async () => {
    const fixture = fakeContext({
      playedMove: 'e2e3',
      results: [
        { bestmove: 'e2e4', score: cp(30) },
        { bestmove: 'e7e5', score: cp(-12) },
      ],
    })

    await variantA.run(fixture.context)

    expect(fixture.cancelChecks()).toBe(2)
  })
})
