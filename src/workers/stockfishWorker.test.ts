import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  CompletedRootAnalysis,
  WorkerRequest,
  WorkerResponse,
} from './stockfishMessages'

vi.mock('stockfish/bin/stockfish-18-lite-single.js?url', () => ({
  default: '/mock/stockfish-18-lite-single.js',
}))

vi.mock('stockfish/bin/stockfish-18-lite-single.wasm?url', () => ({
  default: '/mock/stockfish-18-lite-single.wasm',
}))

describe('stockfishWorker', () => {
  let engineWorkerPostMessageMock: ReturnType<typeof vi.fn>
  let terminateMock: ReturnType<typeof vi.fn>
  let engineMessageHandler: ((line: string) => void) | undefined
  let constructedUrl: string | undefined
  let postMessageMock: ReturnType<typeof vi.fn>
  // The worker's OWN inbound-message handler (self.addEventListener('message')),
  // captured so a test can post WorkerRequests into it like the host thread does.
  let outerMessageHandler:
    | ((event: MessageEvent<WorkerRequest>) => void)
    | undefined

  beforeEach(() => {
    vi.resetModules()

    engineWorkerPostMessageMock = vi.fn()
    terminateMock = vi.fn()
    postMessageMock = vi.fn()
    engineMessageHandler = undefined
    constructedUrl = undefined
    outerMessageHandler = undefined

    vi.stubGlobal('self', {
      addEventListener: vi.fn(
        (type: string, handler: (event: MessageEvent<WorkerRequest>) => void) => {
          if (type === 'message') {
            outerMessageHandler = handler
          }
        },
      ),
      postMessage: postMessageMock,
    })

    vi.stubGlobal('Worker', class {
      constructor(url: string | URL) {
        constructedUrl = String(url)
      }

      postMessage = engineWorkerPostMessageMock
      terminate = terminateMock
      addEventListener = vi.fn((type: string, handler: (event: MessageEvent<string>) => void) => {
        if (type === 'message') {
          engineMessageHandler = (line: string) =>
            handler(new MessageEvent('message', { data: line }))
        }
      })
      removeEventListener = vi.fn()
    })
  })

  it('boots through a nested stockfish worker and emits booted then ready', async () => {
    await import('./stockfishWorker')

    await vi.waitFor(() => {
      expect(constructedUrl).toContain('/mock/stockfish-18-lite-single.js')
      expect(constructedUrl).toContain(
        `#${encodeURIComponent('/mock/stockfish-18-lite-single.wasm')}`,
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('uci')
    })

    expect(postMessageMock).toHaveBeenCalledWith(
      { type: 'booted' } satisfies WorkerResponse,
    )

    engineMessageHandler?.('uciok')
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('isready')

    engineMessageHandler?.('readyok')

    expect(postMessageMock).toHaveBeenCalledWith(
      { type: 'ready' } satisfies WorkerResponse,
    )
  })

  // Drive the engine to ready, then post one request into the worker's inbound
  // handler. Returns once `evaluate-position` has been issued to the engine.
  const bootReadyAnd = async (request: WorkerRequest) => {
    await import('./stockfishWorker')
    await vi.waitFor(() =>
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('uci'),
    )
    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    expect(outerMessageHandler).toBeTypeOf('function')
    outerMessageHandler?.(new MessageEvent('message', { data: request }))
  }

  const lastSnapshot = (): CompletedRootAnalysis => {
    const call = [...postMessageMock.mock.calls]
      .reverse()
      .find(([msg]) => (msg as WorkerResponse).type === 'bestmove')
    expect(call, 'a bestmove response was emitted').toBeDefined()
    return (call![0] as Extract<WorkerResponse, { type: 'bestmove' }>).snapshot
  }

  it('accumulates MultiPV slots and emits one completed snapshot at bestmove', async () => {
    const FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await bootReadyAnd({
      type: 'evaluate-position',
      id: 'req-1',
      fen: FEN,
      depth: 21,
      multipv: 3,
    })

    // The request is issued to the engine as an unrestricted depth-21 MultiPV-3 go.
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
      'setoption name MultiPV value 3',
    )
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 21')

    // Shallow pass across three slots...
    engineMessageHandler?.('info depth 12 multipv 1 score cp 25 pv e2e4 e7e5')
    engineMessageHandler?.('info depth 12 multipv 2 score cp 10 pv d2d4 d7d5')
    engineMessageHandler?.('info depth 12 multipv 3 score cp 5 pv g1f3 g8f6')
    // ...then a DEEPER line-1 iteration that must overwrite the shallow slot 1.
    engineMessageHandler?.('info depth 21 multipv 1 score cp 30 pv e2e4 c7c5')
    engineMessageHandler?.('bestmove e2e4 ponder c7c5')

    const snapshot = lastSnapshot()
    expect(snapshot.requestId).toBe('req-1')
    expect(snapshot.fen).toBe(FEN)
    expect(snapshot.bestMove).toBe('e2e4')
    expect(snapshot.limit).toEqual({ type: 'depth', value: 21 })
    expect(snapshot.multipv).toBe(3)
    expect(snapshot.searchmoves).toBeNull()

    // Exactly three slots, ascending multipv order.
    expect(snapshot.lines.map((l) => l.multipv)).toEqual([1, 2, 3])
    // Slot 1 is the DEEPER iteration (latest wins), not the shallow cp 25 line.
    expect(snapshot.lines[0]).toMatchObject({
      depth: 21,
      score: { type: 'cp', value: 30 },
      pv: ['e2e4', 'c7c5'],
    })
    expect(snapshot.lines[2]).toMatchObject({ pv: ['g1f3', 'g8f6'] })
  })

  it('excludes non-PV info lines and records a restricted searchmoves shape', async () => {
    const FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await bootReadyAnd({
      type: 'evaluate-position',
      id: 'req-2',
      fen: FEN,
      depth: 21,
      multipv: 3,
      searchmoves: ['e2e4'],
    })
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
      'go depth 21 searchmoves e2e4',
    )

    // A PV-less info line (e.g. a currmove/nodes update) must NOT create a slot.
    engineMessageHandler?.('info depth 5 currmove e2e4 currmovenumber 1')
    engineMessageHandler?.('info depth 21 multipv 1 score cp 30 pv e2e4 e7e5')
    engineMessageHandler?.('bestmove e2e4')

    const snapshot = lastSnapshot()
    // Only the single PV-bearing slot survives; the currmove line is dropped.
    expect(snapshot.lines).toHaveLength(1)
    expect(snapshot.lines[0].pv).toEqual(['e2e4', 'e7e5'])
    // The restricted request shape is faithfully recorded for local eligibility.
    expect(snapshot.searchmoves).toEqual(['e2e4'])
  })
})
