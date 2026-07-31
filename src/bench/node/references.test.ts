import { describe, expect, it } from 'vitest'
import type { EngineScore } from '../../workers/stockfishMessages'
import type { CorpusPosition } from './corpus'
import type { ReferenceEngine } from './references'
import {
  REFERENCE_SEARCH_TIMEOUT_MS,
  adjudicatePosition,
  rankReference,
  runBiasReference,
  runPrimaryReference,
} from './references'
import type { NodeSearchRequest, NodeSearchResult } from './stockfish'

const startFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

const position = (
  playedMove = 'd2d4',
  fen = startFen,
): CorpusPosition => ({
  id: 'p',
  fen,
  playedMove,
  playerColor: fen.split(' ')[1] === 'w' ? 'white' : 'black',
  phase: 'opening',
  tags: ['quiet', 'target-good'],
  label: 'test',
  source: 'test',
})

const result = (
  request: NodeSearchRequest,
  slots: Array<{ move: string; score: EngineScore }>,
  bestmove = slots[0]?.move ?? '(none)',
): NodeSearchResult => ({
  bestmove,
  score: slots.at(-1)?.score ?? null,
  pv: slots[0] ? [slots[0].move, 'e7e5'] : null,
  reachedDepth: request.depth,
  selection: slots.length === 0
    ? { accepted: false, reason: 'no-slot' }
    : {
      accepted: true,
      depth: request.depth,
      slots: slots.map((slot, index) => ({
        depth: request.depth,
        seldepth: null,
        multipv: index + 1,
        score: slot.score,
        bound: 'exact',
        pv: [slot.move, 'e7e5'],
        nodes: 100 + index,
        timeMs: 2,
        seq: index,
      })),
    },
  rawLines: [],
  phase: {
    index: 0,
    name: request.phase,
    moves: request.moves ?? [],
    requestedDepth: request.depth,
    bestmove,
    nodes: 100,
    timeMs: 2,
    nps: 50_000,
    hashfull: 0,
    reachedDepth: request.depth,
    seldepth: 20,
    wallMs: 3,
    infoLines: slots.length,
    admittedLines: slots.length,
    terminated: true,
    snapshot: slots.length === 0
      ? { accepted: false, reason: 'no-slot' }
      : { accepted: true, depth: request.depth },
    legacyDivergence: null,
    stopObserved: false,
  },
})

class FakeEngine implements ReferenceEngine {
  resets = 0
  requests: NodeSearchRequest[] = []
  private readonly replies: Array<
    (request: NodeSearchRequest) => NodeSearchResult
  >

  constructor(replies: Array<(request: NodeSearchRequest) => NodeSearchResult>) {
    this.replies = [...replies]
  }

  reset = async () => {
    this.resets += 1
    return 1
  }

  search = async (request: NodeSearchRequest) => {
    this.requests.push(request)
    const reply = this.replies.shift()
    if (!reply) throw new Error('missing fake reply')
    return reply(request)
  }
}

describe('depth-26/27 reference adjudication', () => {
  it('runs the primary unrestricted 26 then joint restricted 27 reference', async () => {
    const engine = new FakeEngine([
      (request) => result(request, [{ move: 'e2e4', score: { type: 'cp', value: 40 } }]),
      (request) => result(request, [
        { move: 'e2e4', score: { type: 'cp', value: 40 } },
        { move: 'd2d4', score: { type: 'cp', value: 10 } },
      ]),
    ])

    const observed = await runPrimaryReference(engine, position())

    expect(engine.resets).toBe(1)
    expect(engine.requests).toMatchObject([
      { depth: 26, phase: 'root' },
      {
        depth: 27,
        phase: 'other',
        searchmoves: ['e2e4', 'd2d4'],
        multipv: 2,
      },
    ])
    expect(engine.requests.every(
      (request) => request.timeoutMs === REFERENCE_SEARCH_TIMEOUT_MS,
    )).toBe(true)
    expect(observed.verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'e2e4',
      playedMove: 'd2d4',
      bestRoot: { type: 'cp', value: 40 },
      playedRoot: { type: 'cp', value: 10 },
      deltaCp: 30,
      pEqualsB: false,
    })
  })

  it('resolves a Variant-A-shaped contradiction and promotes P atomically', async () => {
    const fen = '2kr1b1r/pp1q4/2p5/P1P2p2/2NP2pp/8/1B3PPP/R2QR1K1 w - - 0 22'
    const engine = new FakeEngine([
      (request) => result(request, [{ move: 'a5a6', score: { type: 'cp', value: 50 } }]),
      // Post-played, opponent-to-move -80 -> mover post/root +80.
      (request) => result(request, [{ move: 'a7b6', score: { type: 'cp', value: -80 } }]),
      // Equal scores, but the joint search ranks P in slot 1.
      (request) => result(request, [
        { move: 'c4b6', score: { type: 'cp', value: 60 } },
        { move: 'a5a6', score: { type: 'cp', value: 60 } },
      ], 'c4b6'),
    ])

    const observed = await runBiasReference(
      engine,
      position('c4b6', fen),
    )

    expect(engine.requests.map((request) => ({
      depth: request.depth,
      moves: request.moves,
      searchmoves: request.searchmoves,
      multipv: request.multipv,
      timeoutMs: request.timeoutMs,
    }))).toEqual([
      {
        depth: 27,
        moves: undefined,
        searchmoves: undefined,
        multipv: undefined,
        timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
      },
      {
        depth: 26,
        moves: ['c4b6'],
        searchmoves: undefined,
        multipv: undefined,
        timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
      },
      {
        depth: 27,
        moves: undefined,
        searchmoves: ['a5a6', 'c4b6'],
        multipv: 2,
        timeoutMs: REFERENCE_SEARCH_TIMEOUT_MS,
      },
    ])
    expect(observed.verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'c4b6',
      playedMove: 'c4b6',
      deltaCp: 0,
      classification: 'best',
      resolutionUsed: true,
      pEqualsB: true,
    })
  })

  it('expresses the other g-kgiq branch: equal scores with B first retain B', () => {
    const verdict = rankReference({
      mover: 'white',
      rootNamedBest: 'a5a6',
      playedMove: 'c4b6',
      namedBestRoot: { type: 'cp', value: 60 },
      playedRoot: { type: 'cp', value: 60 },
      jointFirstMove: 'a5a6',
      jointLines: new Map([
        ['a5a6', ['a5a6', 'a7a6']],
        ['c4b6', ['c4b6', 'a7b6']],
      ]),
      namedBestLine: ['a5a6', 'a7a6'],
      playedTerminal: false,
      namedBestTerminal: false,
      tieAuthority: 'joint',
      resolutionUsed: false,
    })

    expect(verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'a5a6',
      deltaCp: 0,
      classification: 'excellent',
      pEqualsB: false,
    })
  })

  it('declines typed-order versus CP-surrogate conflicts instead of clamping', () => {
    const verdict = rankReference({
      mover: 'white',
      rootNamedBest: 'e2e4',
      playedMove: 'd2d4',
      namedBestRoot: { type: 'mate', value: 5 },
      playedRoot: { type: 'cp', value: 9990 },
      jointFirstMove: 'e2e4',
      jointLines: new Map([['e2e4', ['e2e4', 'e7e5']]]),
      namedBestLine: ['e2e4', 'e7e5'],
      playedTerminal: false,
      namedBestTerminal: false,
      tieAuthority: 'joint',
      resolutionUsed: false,
    })

    expect(verdict).toMatchObject({
      status: 'declined',
      reason: 'representation-conflict',
    })
  })

  it('retains a disagreeing row as unadjudicable rather than dropping it', async () => {
    const engine = new FakeEngine([
      // Primary: e4 wins.
      (request) => result(request, [{ move: 'e2e4', score: { type: 'cp', value: 40 } }]),
      (request) => result(request, [
        { move: 'e2e4', score: { type: 'cp', value: 40 } },
        { move: 'd2d4', score: { type: 'cp', value: 10 } },
      ]),
      // Bias: d4 is its root best, so P === B.
      (request) => result(request, [{ move: 'd2d4', score: { type: 'cp', value: 20 } }]),
      (request) => result(request, [{ move: 'e7e5', score: { type: 'cp', value: -20 } }]),
    ])

    const observed = await adjudicatePosition(engine, position())

    expect(engine.resets).toBe(2)
    expect(observed.adjudication).toEqual({
      status: 'unadjudicable',
      reason: 'best-move-disagreement',
      detail: 'primary e2e4, bias d2d4',
    })
    expect(observed.primary.verdict.status).toBe('accepted')
    expect(observed.bias.verdict.status).toBe('accepted')
  })

  it('runs and retains bias evidence even when primary declines', async () => {
    const engine = new FakeEngine([
      (request) => result(request, [], 'e2e4'),
      (request) => result(request, [{ move: 'd2d4', score: { type: 'cp', value: 20 } }]),
      (request) => result(request, [{ move: 'e7e5', score: { type: 'cp', value: -20 } }]),
    ])

    const observed = await adjudicatePosition(engine, position())

    expect(engine.resets).toBe(2)
    expect(observed.adjudication).toMatchObject({
      status: 'unadjudicable',
      reason: 'primary-declined',
    })
    expect(observed.bias.verdict.status).toBe('accepted')
  })

  it('retains the completed primary observation when the bias engine fails', async () => {
    const engine = new FakeEngine([
      (request) => result(request, [{ move: 'e2e4', score: { type: 'cp', value: 40 } }]),
      (request) => result(request, [
        { move: 'e2e4', score: { type: 'cp', value: 40 } },
        { move: 'd2d4', score: { type: 'cp', value: 10 } },
      ]),
      () => {
        throw new Error('bias root timed out')
      },
    ])

    const observed = await adjudicatePosition(engine, position())

    expect(observed.primary.verdict.status).toBe('accepted')
    expect(observed.bias).toMatchObject({
      resetMs: 1,
      phases: [],
      verdict: {
        status: 'declined',
        reason: 'engine-error',
        detail: 'bias root timed out',
      },
    })
    expect(observed.adjudication).toMatchObject({
      status: 'unadjudicable',
      reason: 'bias-declined',
    })
  })

  it('keeps the root PV when the bias post score promotes the same move', async () => {
    const engine = new FakeEngine([
      (request) => result(
        request,
        [{ move: 'd2d4', score: { type: 'cp', value: 10 } }],
        'd2d4',
      ),
      // Opponent-to-move -20 converts to mover/root +20 for the same move.
      (request) => result(
        request,
        [{ move: 'e7e5', score: { type: 'cp', value: -20 } }],
        'e7e5',
      ),
    ])

    const observed = await runBiasReference(engine, position())

    expect(observed.verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'd2d4',
      bestLine: ['d2d4', 'e7e5'],
      deltaCp: 0,
      classification: 'best',
    })
  })

  it('orders two exact terminal operands without a restricted search', async () => {
    const fen = '7k/5Q2/6K1/8/8/8/8/8 w - - 0 1'
    const engine = new FakeEngine([
      (request) => result(
        request,
        [{ move: 'f7f8', score: { type: 'mate', value: 1 } }],
        'f7f8',
      ),
    ])

    const observed = await runPrimaryReference(engine, position('f7e6', fen))

    expect(engine.requests).toHaveLength(1)
    expect(observed.verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'f7f8',
      bestRoot: { type: 'mate', value: 1 },
      playedRoot: { type: 'cp', value: 0 },
      deltaCp: 10_000,
      pEqualsB: false,
    })
  })

  it('searches only the nonterminal operand in a mixed terminal pair', async () => {
    const fen = '7k/5Q2/6K1/8/8/8/8/8 w - - 0 1'
    const engine = new FakeEngine([
      (request) => result(
        request,
        [{ move: 'f7f6', score: { type: 'cp', value: 500 } }],
        'f7f6',
      ),
      (request) => result(
        request,
        [{ move: 'f7f6', score: { type: 'cp', value: 500 } }],
        'f7f6',
      ),
    ])

    const observed = await runPrimaryReference(engine, position('f7f8', fen))

    expect(engine.requests[1]).toMatchObject({
      searchmoves: ['f7f6'],
      multipv: 1,
    })
    expect(observed.verdict).toMatchObject({
      status: 'accepted',
      bestMove: 'f7f8',
      playedMove: 'f7f8',
      bestRoot: { type: 'mate', value: 1 },
      deltaCp: 0,
      classification: 'best',
      pEqualsB: true,
    })
  })
})
