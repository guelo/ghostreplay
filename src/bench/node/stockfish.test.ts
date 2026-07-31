import { describe, expect, it } from 'vitest'
import type { UciTransport } from './stockfish'
import { NodeStockfish } from './stockfish'

type GoReply = {
  lines: string[]
  bestmove?: string
  answerStop?: string
}

class FakeTransport implements UciTransport {
  readonly commands: string[] = []
  readonly listeners = new Set<(line: string) => void>()
  readonly replies: GoReply[] = []
  closed = false
  private active: GoReply | null = null

  onLine = (listener: (line: string) => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private emit(line: string) {
    queueMicrotask(() => {
      for (const listener of this.listeners) listener(line)
    })
  }

  send = (command: string) => {
    this.commands.push(command)
    if (command === 'uci') this.emit('uciok')
    if (command === 'isready') this.emit('readyok')
    if (command.startsWith('go ')) {
      const reply = this.replies.shift()
      if (!reply) throw new Error('fake has no go reply')
      this.active = reply
      for (const line of reply.lines) this.emit(line)
      if (reply.bestmove) {
        this.emit(`bestmove ${reply.bestmove}`)
        this.active = null
      }
    }
    if (command === 'stop' && this.active?.answerStop) {
      this.emit(`bestmove ${this.active.answerStop}`)
      this.active = null
    }
  }

  close = () => {
    this.closed = true
  }
}

const exact = (
  depth: number,
  move: string,
  score: number,
  options: { multipv?: number; nodes?: number } = {},
) => [
  'info',
  `depth ${depth}`,
  'seldepth 24',
  ...(options.multipv ? [`multipv ${options.multipv}`] : []),
  `score cp ${score}`,
  `nodes ${options.nodes ?? 100}`,
  'nps 50000',
  'time 2',
  'hashfull 0',
  `pv ${move} e7e5`,
].join(' ')

describe('Node Stockfish adapter', () => {
  it('uses the production init/reset policy and instruments one exact search', async () => {
    const transport = new FakeTransport()
    transport.replies.push({
      lines: [exact(17, 'e2e4', 30)],
      bestmove: 'e2e4',
    })
    const engine = await NodeStockfish.create({ transport })

    await engine.reset()
    const result = await engine.search({
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      depth: 17,
      phase: 'root',
    })

    expect(transport.commands).toEqual([
      'uci',
      'setoption name Hash value 128',
      'setoption name MultiPV value 1',
      'isready',
      'ucinewgame',
      'isready',
      'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      'go depth 17',
    ])
    expect(result).toMatchObject({
      bestmove: 'e2e4',
      score: { type: 'cp', value: 30 },
      pv: ['e2e4', 'e7e5'],
      reachedDepth: 17,
      selection: { accepted: true, depth: 17 },
      phase: {
        name: 'root',
        requestedDepth: 17,
        bestmove: 'e2e4',
        nodes: 100,
        timeMs: 2,
        infoLines: 1,
        admittedLines: 1,
        snapshot: { accepted: true, depth: 17 },
        legacyDivergence: null,
        terminated: true,
      },
    })
    engine.close()
    expect(transport.closed).toBe(true)
  })

  it('sets restricted MultiPV immediately before the search and restores it', async () => {
    const transport = new FakeTransport()
    transport.replies.push({
      lines: [
        exact(27, 'e2e4', 50, { multipv: 1, nodes: 100 }),
        exact(27, 'd2d4', 20, { multipv: 2, nodes: 110 }),
      ],
      bestmove: 'e2e4',
    })
    const engine = await NodeStockfish.create({ transport })
    const result = await engine.search({
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      depth: 27,
      phase: 'other',
      multipv: 2,
      searchmoves: ['e2e4', 'd2d4'],
    })

    expect(result.selection).toMatchObject({
      accepted: true,
      depth: 27,
    })
    expect(transport.commands.slice(-4)).toEqual([
      'setoption name MultiPV value 2',
      'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      'go depth 27 searchmoves e2e4 d2d4',
      'setoption name MultiPV value 1',
    ])
  })

  it('stops and drains a timed-out search before restoring MultiPV', async () => {
    const transport = new FakeTransport()
    transport.replies.push({
      lines: [],
      answerStop: 'e2e4',
    })
    const engine = await NodeStockfish.create({ transport })

    await expect(engine.search({
      fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      depth: 27,
      phase: 'other',
      multipv: 2,
      searchmoves: ['e2e4', 'd2d4'],
      timeoutMs: 5,
    })).rejects.toThrow(/timed out/)

    expect(transport.commands.slice(-2)).toEqual([
      'stop',
      'setoption name MultiPV value 1',
    ])
  })
})
