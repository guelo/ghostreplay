import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AnalysisWorkerRequest,
  AnalysisWorkerResponse,
} from './analysisMessages'

vi.mock('stockfish/bin/stockfish-18-lite-single.js?url', () => ({
  default: '/mock/stockfish-18-lite-single.js',
}))

vi.mock('stockfish/bin/stockfish-18-lite-single.wasm?url', () => ({
  default: '/mock/stockfish-18-lite-single.wasm',
}))

describe('analysisWorker', () => {
  let engineWorkerPostMessageMock: ReturnType<typeof vi.fn>
  let terminateMock: ReturnType<typeof vi.fn>
  let engineMessageHandler: ((line: string) => void) | undefined
  let messageHandler: ((event: MessageEvent<AnalysisWorkerRequest>) => void) | undefined
  let postMessageMock: ReturnType<typeof vi.fn>
  let constructedUrl: string | undefined
  // The per-request reset sends `ucinewgame`+`isready` and waits for `readyok`.
  // The first `isready` is the init handshake (driven manually by each test);
  // every later one is a reset, which we auto-answer so existing tests keep
  // their flow. Set `autoReadyok = false` to exercise a missing/late readyok.
  let isReadyCount: number
  let autoReadyok: boolean

  beforeEach(() => {
    vi.resetModules()

    isReadyCount = 0
    autoReadyok = true
    engineWorkerPostMessageMock = vi.fn((command: string) => {
      if (command === 'isready') {
        isReadyCount += 1
        if (isReadyCount > 1 && autoReadyok) {
          queueMicrotask(() => engineMessageHandler?.('readyok'))
        }
      }
    })
    terminateMock = vi.fn()
    postMessageMock = vi.fn()
    engineMessageHandler = undefined
    messageHandler = undefined
    constructedUrl = undefined

    vi.stubGlobal('self', {
      addEventListener: vi.fn(
        (type: string, handler: (event: MessageEvent<AnalysisWorkerRequest>) => void) => {
          if (type === 'message') {
            messageHandler = handler
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

  it('initializes via a nested stockfish worker, emits ready, and analyzes through postMessage', async () => {
    await import('./analysisWorker')

    await vi.waitFor(() => {
      expect(constructedUrl).toContain('/mock/stockfish-18-lite-single.js')
      expect(constructedUrl).toContain(
        `#${encodeURIComponent('/mock/stockfish-18-lite-single.wasm')}`,
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('uci')
    })

    engineMessageHandler?.('uciok')
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('setoption name Hash value 128')
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('setoption name MultiPV value 1')
    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('isready')
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      expect.stringContaining('setoption name Threads value'),
    )

    engineMessageHandler?.('readyok')

    expect(postMessageMock).toHaveBeenCalledWith(
      { type: 'ready' } satisfies AnalysisWorkerResponse,
    )

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-1',
          fen: '4k3/8/8/8/8/8/8/4K2R w - - 0 1',
          move: 'e1e2',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        { type: 'analysis-started', id: 'analysis-1', move: 'e1e2' } satisfies AnalysisWorkerResponse,
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen 4k3/8/8/8/8/8/8/4K2R w - - 0 1',
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })
  })

  it('stops an active analysis when a cancel message arrives', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-1',
          fen: '4k3/8/8/8/8/8/8/4K2R w - - 0 1',
          move: 'e1e2',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'cancel-analysis',
          id: 'analysis-1',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('stop')
  })

  it('removes a queued analysis before it ever starts', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-1',
          fen: '4k3/8/8/8/8/8/8/4K2R w - - 0 1',
          move: 'e1e2',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-2',
          fen: '8/8/8/8/8/8/8/4K3 w - - 0 1',
          move: 'e1e2',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'cancel-analysis',
          id: 'analysis-2',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    engineMessageHandler?.('bestmove e1e2')

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen 4k3/8/8/8/8/8/8/4K2R w - - 0 1 moves e1e2',
      )
    })

    engineMessageHandler?.('bestmove e1e2')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-1',
        }),
      )
    })

    expect(postMessageMock).not.toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'analysis-started',
        id: 'analysis-2',
      }),
    )
    expect(postMessageMock).not.toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'analysis',
        id: 'analysis-2',
      }),
    )
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      'position fen 8/8/8/8/8/8/8/4K3 w - - 0 1',
    )
  })

  it('synthesizes terminal mate scores without searching post-move positions', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-mate',
          fen: '7k/8/6QK/8/8/8/8/8 w - - 0 1',
          move: 'g6g7',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen 7k/8/6QK/8/8/8/8/8 w - - 0 1',
      )
    })

    engineMessageHandler?.('bestmove g6e8')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-mate',
          bestMove: 'g6e8',
          playedEval: 10000,
          bestEval: 10000,
          playedEvalMate: 0,
          bestEvalMate: 0,
          delta: 0,
        }),
      )
    })

    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      'position fen 7k/8/6QK/8/8/8/8/8 w - - 0 1 moves g6g7',
    )
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      'position fen 7k/8/6QK/8/8/8/8/8 w - - 0 1 moves g6e8',
    )
  })

  it('synthesizes terminal draw scores so inferior draw conversions get a delta', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-draw',
          fen: '7k/5K2/6Q1/8/8/8/8/8 w - - 0 1',
          move: 'f7e8',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen 7k/5K2/6Q1/8/8/8/8/8 w - - 0 1',
      )
    })

    engineMessageHandler?.('bestmove g6g7')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-draw',
          bestMove: 'g6g7',
          playedEval: expect.closeTo(0, 0),
          bestEval: 10000,
          delta: 10000,
          classification: 'blunder',
        }),
      )
    })

    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      'position fen 7k/5K2/6Q1/8/8/8/8/8 w - - 0 1 moves f7e8',
    )
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith(
      'position fen 7k/5K2/6Q1/8/8/8/8/8 w - - 0 1 moves g6g7',
    )
  })

  it('classifies alternate black terminal mating moves from the mated side perspective', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-black-mate',
          fen: '8/8/8/8/8/6qk/8/7K b - - 0 1',
          move: 'g3g2',
          playerColor: 'black',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen 8/8/8/8/8/6qk/8/7K b - - 0 1',
      )
    })

    engineMessageHandler?.('bestmove g3h2')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-black-mate',
          bestMove: 'g3h2',
          playedEval: 10000,
          bestEval: 10000,
          delta: 0,
          classification: 'excellent',
        }),
      )
    })
  })

  it('captures the root best-move PV and reports it as bestLine', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-pv',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      )
    })

    // Root best search emits a multipv-1 PV starting with the bestmove.
    engineMessageHandler?.('info depth 17 multipv 1 score cp 30 pv e2e4 e7e5 g1f3')
    engineMessageHandler?.('bestmove e2e4')

    // Post-played search (best === played here, so no separate best search).
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 17 score cp -25 pv e7e5')
    engineMessageHandler?.('bestmove e7e5')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-pv',
          bestMove: 'e2e4',
          bestLine: ['e2e4', 'e7e5', 'g1f3'],
          // cp-only evals carry no mate count.
          playedEvalMate: null,
          bestEvalMate: null,
        }),
      )
    })
  })

  it('uses the continuation PV when the root PV does not start with bestmove', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-bad-pv',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      )
    })

    // Stale PV from a prior depth whose head no longer matches the final bestmove.
    engineMessageHandler?.('info depth 16 multipv 1 score cp 30 pv d2d4 d7d5')
    engineMessageHandler?.('bestmove e2e4')

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 17 score cp -25 pv e7e5 g1f3')
    engineMessageHandler?.('bestmove e7e5')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-bad-pv',
          bestMove: 'e2e4',
          bestLine: ['e2e4', 'e7e5', 'g1f3'],
        }),
      )
    })
  })

  it('uses the post-best continuation PV when the played move is not best', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-post-best-pv',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'd2d4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      )
    })

    engineMessageHandler?.('info depth 16 multipv 1 score cp 30 pv c2c4 e7e5')
    engineMessageHandler?.('bestmove e2e4')

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves d2d4'),
      )
    })
    engineMessageHandler?.('info depth 17 score cp -10 pv d7d5')
    engineMessageHandler?.('bestmove d7d5')

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 17 score cp -25 pv e7e5 g1f3')
    engineMessageHandler?.('bestmove e7e5')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-post-best-pv',
          bestMove: 'e2e4',
          bestLine: ['e2e4', 'e7e5', 'g1f3'],
        }),
      )
    })
  })

  it('emits player-relative mate counts when the engine reports a mate score', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'analysis-mate-score',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        'position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      )
    })
    engineMessageHandler?.('bestmove e2e4')

    // Post-played search: black to move and getting mated in 3 (mate -3 from
    // the side-to-move/black perspective). White player delivers mate in 3.
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 17 score mate -3 pv a7a6')
    engineMessageHandler?.('bestmove a7a6')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'analysis',
          id: 'analysis-mate-score',
          playedEvalMate: 3,
          bestEvalMate: 3,
        }),
      )
    })
  })

  it('resets the engine (ucinewgame+isready) once at the start, before the root search and not between the related searches', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'reset-1',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    // The reset precedes the root search.
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('ucinewgame')
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    const order = engineWorkerPostMessageMock.mock.calls.map((c) => c[0] as string)
    const firstNewGame = order.indexOf('ucinewgame')
    const firstPosition = order.findIndex((c) => c.startsWith('position fen'))
    expect(firstNewGame).toBeGreaterThanOrEqual(0)
    expect(firstNewGame).toBeLessThan(firstPosition)

    engineMessageHandler?.('info depth 17 score cp 30 pv e2e4 e7e5')
    engineMessageHandler?.('bestmove e2e4')

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 17 score cp -25 pv e7e5')
    engineMessageHandler?.('bestmove e7e5')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'analysis', id: 'reset-1' }),
      )
    })

    // Exactly one reset for the whole request (best move == played move here, so
    // only the root + post-played searches ran — never a reset between them).
    const newGameCalls = engineWorkerPostMessageMock.mock.calls.filter(
      (c) => c[0] === 'ucinewgame',
    )
    expect(newGameCalls).toHaveLength(1)
  })

  it('settles a cancel that arrives while awaiting the per-request reset, clearing analysisInFlight', async () => {
    await import('./analysisWorker')

    autoReadyok = false // hold back the reset readyok so the cancel races it

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'cancel-reset',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    // Reset issued, but no readyok yet — the root search never starts.
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('isready')
    })
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('go depth 17')

    messageHandler?.(
      new MessageEvent('message', {
        data: { type: 'cancel-analysis', id: 'cancel-reset' } satisfies AnalysisWorkerRequest,
      }),
    )

    // A late readyok must not start a search for the canceled request, and the
    // queue must be free to run the next analysis (analysisInFlight cleared).
    engineMessageHandler?.('readyok')
    autoReadyok = true

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'after-cancel',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'd2d4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'analysis-started', id: 'after-cancel' }),
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    expect(postMessageMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'analysis', id: 'cancel-reset' }),
    )
  })

  it("absorbs a canceled request's late readyok instead of releasing the next request's reset barrier", async () => {
    await import('./analysisWorker')

    autoReadyok = false // drive every readyok manually to control ordering

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok') // init handshake
    engineWorkerPostMessageMock.mockClear()
    postMessageMock.mockClear()

    // Request A starts and issues its reset, then is canceled mid-reset.
    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'req-a',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('isready')
    })
    messageHandler?.(
      new MessageEvent('message', {
        data: { type: 'cancel-analysis', id: 'req-a' } satisfies AnalysisWorkerRequest,
      }),
    )

    // Request B drains next and installs its OWN reset waiter (A's readyok is
    // still in flight at this point).
    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'req-b',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'd2d4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )
    await vi.waitFor(() => {
      // Two resets issued (A + B); B's barrier is not yet satisfied.
      const newGames = engineWorkerPostMessageMock.mock.calls.filter(
        (c) => c[0] === 'ucinewgame',
      )
      expect(newGames.length).toBe(2)
    })
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('go depth 17')

    // First readyok belongs to A (FIFO). It must be ABSORBED — B must not start.
    engineMessageHandler?.('readyok')
    await Promise.resolve()
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('go depth 17')

    // Second readyok belongs to B — only now does B cross its reset barrier.
    engineMessageHandler?.('readyok')
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })
    expect(postMessageMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'analysis', id: 'req-a' }),
    )
  })

  it('emits analysis-progress during the root search (root-phase liveness)', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    const id = 'analysis-root'
    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id,
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    // The per-request reset (ucinewgame+isready→readyok) auto-resolves; wait for
    // the root search to begin.
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    postMessageMock.mockClear()
    // A root-search info line — BEFORE any bestmove. The root phase emitted no
    // observable activity before this change; now it surfaces a liveness ping.
    engineMessageHandler?.('info depth 5 score cp 20 pv e2e4')

    expect(postMessageMock).toHaveBeenCalledWith(
      { type: 'analysis-progress', id } satisfies AnalysisWorkerResponse,
    )
  })

  it('emits analysis-progress on bestmove (phase-boundary liveness)', async () => {
    await import('./analysisWorker')

    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    const id = 'analysis-bestmove'
    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id,
          fen: '4k3/8/8/8/8/8/8/4K2R w - - 0 1',
          move: 'e1e2',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })

    postMessageMock.mockClear()
    // The root search completes with a bestmove and NO surrounding info line; the
    // phase-boundary ping still surfaces liveness for the silent between-phase gap.
    engineMessageHandler?.('bestmove e1e2')

    expect(postMessageMock).toHaveBeenCalledWith(
      { type: 'analysis-progress', id } satisfies AnalysisWorkerResponse,
    )
  })

  it('throttles analysis-progress pings to one per window of rapid info lines', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(0)
      await import('./analysisWorker')

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      const id = 'analysis-throttle'
      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id,
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )

      // Flush the per-request reset → root search start under fake timers.
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      postMessageMock.mockClear()
      const progressCount = () =>
        postMessageMock.mock.calls.filter(([m]) => m.type === 'analysis-progress').length

      // Two rapid info lines within the 250ms throttle window → one ping.
      engineMessageHandler?.('info depth 5 score cp 20 pv e2e4')
      engineMessageHandler?.('info depth 6 score cp 22 pv e2e4')
      expect(progressCount()).toBe(1)

      // Past the window → the next info line emits a second ping.
      await vi.advanceTimersByTimeAsync(300)
      engineMessageHandler?.('info depth 7 score cp 25 pv e2e4')
      expect(progressCount()).toBe(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
