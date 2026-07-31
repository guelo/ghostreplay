import { describe, expect, it } from 'vitest'
import {
  assertVisibleTtSnapshotsEqual,
  buildVisibleTtSearchPositions,
  runVisibleTtDeterminismCheck,
  VisibleTtEngine,
} from './visibleTtDeterminism'
import type {
  VisibleTtSnapshot,
  VisibleTtTransport,
} from './visibleTtDeterminism'

type FakeReply = {
  lines: string[]
  bestmove: string
}

class FakeTransport implements VisibleTtTransport {
  readonly commands: string[] = []
  readonly listeners = new Set<(line: string) => void>()
  readonly replies: FakeReply[] = []
  closed = false

  onLine = (listener: (line: string) => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private emit(line: string) {
    queueMicrotask(() => {
      for (const listener of this.listeners) {
        listener(line)
      }
    })
  }

  send = (command: string) => {
    this.commands.push(command)
    if (command === 'uci') {
      this.emit('uciok')
    } else if (command === 'isready') {
      this.emit('readyok')
    } else if (command.startsWith('go ')) {
      const reply = this.replies.shift()
      if (!reply) {
        throw new Error('fake transport has no search reply')
      }
      reply.lines.forEach((line) => this.emit(line))
      this.emit(`bestmove ${reply.bestmove}`)
    }
  }

  close = () => {
    this.closed = true
  }
}

const infoLine = (multipv: number, score: number, pv: string[]) =>
  [
    'info depth 21',
    `multipv ${multipv}`,
    `score cp ${score}`,
    `pv ${pv.join(' ')}`,
  ].join(' ')

const completeReply = (): FakeReply => ({
  lines: [
    infoLine(1, 40, ['e2e4', 'e7e5']),
    infoLine(2, 30, ['d2d4', 'd7d5']),
    infoLine(3, 20, ['g1f3', 'g8f6']),
  ],
  bestmove: 'e2e4',
})

const completeSnapshot = (): VisibleTtSnapshot => ({
  bestmove: 'e2e4',
  lines: [
    {
      depth: 21,
      multipv: 1,
      score: { type: 'cp', value: 40 },
      pv: ['e2e4', 'e7e5'],
    },
    {
      depth: 21,
      multipv: 2,
      score: { type: 'cp', value: 30 },
      pv: ['d2d4', 'd7d5'],
    },
    {
      depth: 21,
      multipv: 3,
      score: { type: 'cp', value: 20 },
      pv: ['g1f3', 'g8f6'],
    },
  ],
})

describe('visible TT determinism check', () => {
  it('searches target, four derived browse positions, then target with production commands', async () => {
    const positions = buildVisibleTtSearchPositions()
    expect(positions.map((position) => position.id)).toEqual([
      'target:cold',
      'thermal:ply-024',
      'thermal:ply-030',
      'thermal:ply-036',
      'thermal:ply-042',
      'target:after-browse',
    ])
    expect(positions[0].fen).toBe(positions[positions.length - 1].fen)

    const transport = new FakeTransport()
    positions.forEach(() => transport.replies.push(completeReply()))
    const engine = await VisibleTtEngine.create({ transport })

    await expect(runVisibleTtDeterminismCheck(engine, positions)).resolves.toMatchObject({
      positions,
      snapshots: expect.arrayContaining([
        expect.objectContaining({ bestmove: 'e2e4' }),
      ]),
    })

    expect(transport.commands).toEqual([
      'uci',
      'isready',
      'setoption name Hash value 64',
      ...positions.flatMap((position) => [
        'ucinewgame',
        'setoption name MultiPV value 3',
        `position fen ${position.fen}`,
        'go depth 21',
      ]),
    ])
    engine.close()
    expect(transport.closed).toBe(true)
  })

  it('accepts identical evidence-bearing target snapshots', () => {
    const snapshot = completeSnapshot()
    expect(() => assertVisibleTtSnapshotsEqual(snapshot, structuredClone(snapshot)))
      .not.toThrow()
  })

  it.each([
    {
      field: 'bestmove',
      mutate: (snapshot: VisibleTtSnapshot) => {
        snapshot.bestmove = 'd2d4'
      },
      message: /bestmove changed/,
    },
    {
      field: 'score',
      mutate: (snapshot: VisibleTtSnapshot) => {
        snapshot.lines[1].score = { type: 'cp', value: 31 }
      },
      message: /score changed/,
    },
    {
      field: 'PV',
      mutate: (snapshot: VisibleTtSnapshot) => {
        snapshot.lines[2].pv[1] = 'd7d5'
      },
      message: /PV changed/,
    },
  ])('rejects a changed $field', ({ mutate, message }) => {
    const cold = completeSnapshot()
    const changed = structuredClone(cold)
    mutate(changed)
    expect(() => assertVisibleTtSnapshotsEqual(cold, changed)).toThrow(message)
  })

  it('rejects an incomplete depth-21 snapshot', async () => {
    const transport = new FakeTransport()
    const reply = completeReply()
    reply.lines.pop()
    transport.replies.push(reply)
    const engine = await VisibleTtEngine.create({ transport })

    await expect(engine.search(buildVisibleTtSearchPositions()[0]))
      .rejects.toThrow(/missing complete depth-21 MultiPV slot 3/)
    engine.close()
  })
})
