import { describe, expect, it } from 'vitest'
import type { EngineScore } from '../../workers/stockfishMessages'
import type { CorpusPosition } from './corpus'
import type { CurrentProtocolEngine } from './currentProtocol'
import { runCurrentProtocol } from './currentProtocol'
import type { NodeSearchRequest, NodeSearchResult } from './stockfish'

const rootFen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

const position = (
  playedMove: string,
  fen = rootFen,
): CorpusPosition => ({
  id: 'test',
  fen,
  playedMove,
  playerColor: fen.split(' ')[1] === 'w' ? 'white' : 'black',
  phase: 'opening',
  tags: ['quiet', 'target-best'],
  label: 'test',
  source: 'test',
})

const searchResult = (
  request: NodeSearchRequest,
  bestmove: string,
  score: EngineScore,
  pv: string[] = [bestmove, 'e7e5'],
): NodeSearchResult => ({
  bestmove,
  score,
  pv,
  reachedDepth: request.depth,
  selection: {
    accepted: true,
    depth: request.depth,
    slots: [{
      depth: request.depth,
      seldepth: null,
      multipv: 1,
      score,
      bound: 'exact',
      pv,
      nodes: 100,
      timeMs: 2,
      seq: 0,
    }],
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
    infoLines: 1,
    admittedLines: 1,
    terminated: true,
    snapshot: { accepted: true, depth: request.depth },
    legacyDivergence: null,
    stopObserved: false,
  },
})

class FakeEngine implements CurrentProtocolEngine {
  resets = 0
  requests: NodeSearchRequest[] = []
  private readonly reply: (request: NodeSearchRequest) => NodeSearchResult

  constructor(reply: (request: NodeSearchRequest) => NodeSearchResult) {
    this.reply = reply
  }

  reset = async () => {
    this.resets += 1
    return 1
  }

  search = async (request: NodeSearchRequest) => {
    this.requests.push(request)
    return this.reply(request)
  }
}

describe('Node current-protocol harness', () => {
  it('reproduces 1.e4 without comparing the +97 root score to the +29 post score', async () => {
    const engine = new FakeEngine((request) => {
      if ((request.moves ?? []).length === 0) {
        return searchResult(request, 'e2e4', { type: 'cp', value: 97 })
      }
      return searchResult(request, 'e7e5', { type: 'cp', value: -29 })
    })

    const outcome = await runCurrentProtocol(engine, position('e2e4'), 17)

    expect(engine.resets).toBe(1)
    expect(engine.requests.map((request) => request.moves ?? [])).toEqual([[], ['e2e4']])
    expect(outcome.result).toMatchObject({
      bestMove: 'e2e4',
      bestEval: 29,
      playedEval: 29,
      delta: 0,
      classification: 'best',
      canonical: true,
    })
    expect(outcome.error).toBeNull()
  })

  it('uses three searches for a non-best non-terminal move without an inter-phase reset', async () => {
    const engine = new FakeEngine((request) => {
      const moves = request.moves ?? []
      if (moves.length === 0) {
        return searchResult(request, 'e2e4', { type: 'cp', value: 30 })
      }
      if (moves[0] === 'd2d4') {
        return searchResult(request, 'd7d5', { type: 'cp', value: 10 })
      }
      return searchResult(request, 'e7e5', { type: 'cp', value: -30 })
    })

    const outcome = await runCurrentProtocol(engine, position('d2d4'), 17)

    expect(engine.resets).toBe(1)
    expect(engine.requests.map((request) => request.moves ?? [])).toEqual([
      [],
      ['d2d4'],
      ['e2e4'],
    ])
    expect(outcome.phases.map((phase) => phase.index)).toEqual([0, 1, 2])
    expect(outcome.result.bestLine.slice(0, 2)).toEqual(['e2e4', 'e7e5'])
  })

  it('uses the exact terminal score and skips a post-move search', async () => {
    const fen = '7k/5Q2/6K1/8/8/8/8/8 w - - 0 1'
    const engine = new FakeEngine((request) =>
      searchResult(request, 'f7f8', { type: 'mate', value: 1 }, ['f7f8']))

    const outcome = await runCurrentProtocol(engine, position('f7f8', fen), 17)

    expect(engine.requests).toHaveLength(1)
    expect(outcome.result).toMatchObject({
      bestMove: 'f7f8',
      bestEval: 10_000,
      playedEval: 10_000,
      bestEvalMate: 0,
      playedEvalMate: 0,
      delta: 0,
      classification: 'best',
    })
  })

  it('reports a strict inversion before any nonnegative clamp can hide it', async () => {
    const engine = new FakeEngine((request) => {
      const moves = request.moves ?? []
      if (moves.length === 0) {
        return searchResult(request, 'e2e4', { type: 'cp', value: 20 })
      }
      if (moves[0] === 'd2d4') {
        // Black-to-move -50 => +50 for the white mover.
        return searchResult(request, 'd7d5', { type: 'cp', value: -50 })
      }
      // Black-to-move -20 => +20 for the white mover.
      return searchResult(request, 'e7e5', { type: 'cp', value: -20 })
    })

    const outcome = await runCurrentProtocol(engine, position('d2d4'), 17)

    expect(outcome.result.delta).toBe(-30)
    expect(outcome.error).toMatch(/ordering inversion/)
  })
})
