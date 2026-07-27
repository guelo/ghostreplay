import { readdirSync, readFileSync } from 'node:fs'
import { relative, resolve } from 'node:path'
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

  it('keeps posting heartbeat pings through a long silent iteration (past the inactivity window) and still resolves on a late bestmove', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(0)
      await import('./analysisWorker')

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      const id = 'analysis-heartbeat'
      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id,
            // The real g-f2mg position: a sharp depth-14 iteration ran ~8.7s with
            // no info line, false-killing the live search before this fix.
            fen: 'r4rk1/pp3ppp/4p3/3p4/b1pP1B2/2q1P3/2P1BPPP/1K1R3R w - - 2 17',
            move: 'd1c1',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )

      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      postMessageMock.mockClear()
      const progressCount = () =>
        postMessageMock.mock.calls.filter(
          ([m]) => m.type === 'analysis-progress' && m.id === id,
        ).length

      // A single long iteration: NO engine line for 30s — well past the 8s
      // coordinator inactivity window AND past any plausible engine-silence
      // ceiling. The wall-clock heartbeat must keep pinging on its ~1s cadence the
      // WHOLE time (g-f2mg AC: posts UNCONDITIONALLY while a search is active), so
      // the coordinator never sees inactivity and never false-kills a live search.
      await vi.advanceTimersByTimeAsync(30_000)
      expect(progressCount()).toBeGreaterThanOrEqual(28)

      // The iteration finally completes; the root search resolves and the worker
      // advances to the post-played search (moves d1c1) — proving the long-silent
      // search was never abandoned (a would-be ceiling+inactivity window of ~28s
      // came and went with the search still live).
      engineMessageHandler?.('info depth 14 score cp 950 nodes 5537499 pv c3a3')
      engineMessageHandler?.('bestmove c3a3')
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
          expect.stringContaining('moves d1c1'),
        )
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the heartbeat once the analysis completes, leaking no timer into idle time', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(0)
      await import('./analysisWorker')

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      const id = 'analysis-heartbeat-complete'
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

      // Root search → bestmove (best === played, so only post-played follows).
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })
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
          expect.objectContaining({ type: 'analysis', id }),
        )
      })

      // The request is fully resolved; no search is active. Advancing the clock
      // must emit no further heartbeat pings (timer cleared on bestmove and at
      // the per-request finally — no leak into the next request).
      postMessageMock.mockClear()
      await vi.advanceTimersByTimeAsync(10_000)
      expect(
        postMessageMock.mock.calls.filter(([m]) => m.type === 'analysis-progress'),
      ).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the heartbeat on cancel so a canceled search stops pinging', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(0)
      await import('./analysisWorker')

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      const id = 'analysis-heartbeat-cancel'
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

      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      messageHandler?.(
        new MessageEvent('message', {
          data: { type: 'cancel-analysis', id } satisfies AnalysisWorkerRequest,
        }),
      )
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('stop')

      // Heartbeat stopped eagerly on cancel — even though no bestmove arrived to
      // null activeSearch, advancing the clock emits no pings.
      postMessageMock.mockClear()
      await vi.advanceTimersByTimeAsync(10_000)
      expect(
        postMessageMock.mock.calls.filter(([m]) => m.type === 'analysis-progress'),
      ).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the heartbeat on terminate so no timer leaks after teardown', async () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(0)
      await import('./analysisWorker')

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      const id = 'analysis-heartbeat-terminate'
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

      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      messageHandler?.(
        new MessageEvent('message', {
          data: { type: 'terminate' } satisfies AnalysisWorkerRequest,
        }),
      )

      postMessageMock.mockClear()
      await vi.advanceTimersByTimeAsync(10_000)
      expect(
        postMessageMock.mock.calls.filter(([m]) => m.type === 'analysis-progress'),
      ).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('defaults to go depth 17 when no depth is supplied (in-game path)', async () => {
    await import('./analysisWorker')
    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()

    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'no-depth',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e4',
          playerColor: 'white',
        } satisfies AnalysisWorkerRequest,
      }),
    )

    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
    })
    expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('go depth 21')
  })

  it('threads the evidence depth into the root, post-played, and post-best searches', async () => {
    await import('./analysisWorker')
    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')
    engineWorkerPostMessageMock.mockClear()

    // played (e2e3) != best (e2e4) so all THREE searches run.
    messageHandler?.(
      new MessageEvent('message', {
        data: {
          type: 'analyze-move',
          id: 'evidence-depth',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          move: 'e2e3',
          playerColor: 'white',
          depth: 21,
        } satisfies AnalysisWorkerRequest,
      }),
    )

    // Root best search.
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 21')
    })
    engineMessageHandler?.('info depth 21 multipv 1 score cp 30 pv e2e4 e7e5')
    engineMessageHandler?.('bestmove e2e4')

    // Post-played search (moves e2e3).
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e3'),
      )
    })
    engineMessageHandler?.('info depth 21 score cp 20 pv e7e5')
    engineMessageHandler?.('bestmove e7e5')

    // Post-best search (moves e2e4).
    await vi.waitFor(() => {
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
        expect.stringContaining('moves e2e4'),
      )
    })
    engineMessageHandler?.('info depth 21 score cp 30 pv e7e5')
    engineMessageHandler?.('bestmove e7e5')

    await vi.waitFor(() => {
      expect(postMessageMock).toHaveBeenCalledWith(
        expect.objectContaining({ type: 'analysis', id: 'evidence-depth', canonical: true }),
      )
    })

    // All three internal searches used depth 21; none used the default 17.
    const depthCalls = engineWorkerPostMessageMock.mock.calls.filter(
      ([c]) => typeof c === 'string' && c.startsWith('go depth'),
    )
    expect(depthCalls).toEqual([['go depth 21'], ['go depth 21'], ['go depth 21']])
  })

  // --- shared per-move deadline + provenance honesty (g-mk1d §1.6) -------------

  /** Drive one full analyze-move to completion, returning the emitted message. */
  const runAnalysisToCompletion = async (id: string) => {
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
    // Three sequential searches: root, post-played, post-best (played != best).
    const phases = [
      { position: 'position fen rnbqkbnr', best: 'd2d4' },
      { position: 'moves e2e4', best: 'e7e5' },
      { position: 'moves d2d4', best: 'g8f6' },
    ]
    for (const phase of phases) {
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
          expect.stringContaining(phase.position),
        )
      })
      engineMessageHandler?.(`info depth 17 score cp 20 pv ${phase.best}`)
      engineMessageHandler?.(`bestmove ${phase.best}`)
    }
    return await vi.waitFor(() => {
      const call = postMessageMock.mock.calls.find(
        ([m]) => m?.type === 'analysis' && m.id === id,
      )
      expect(call).toBeDefined()
      return call![0]
    })
  }

  it('reports capFired=false for a search that completed on its own', async () => {
    await import('./analysisWorker')
    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    const message = await runAnalysisToCompletion('no-cap')
    expect(message.capFired).toBe(false)
  })

  it('is a strict no-op while the budget is dormant, however long a search runs', async () => {
    // The parity landing ships MAX_ANALYSIS_MS = null. A search running far past
    // the ~8s inactivity window (heartbeat alive) must still report capFired=false
    // and keep its provenance — proving the mechanism cannot change behavior
    // before the ceiling raise enables it.
    vi.useFakeTimers()
    try {
      await import('./analysisWorker')
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'dormant',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })
      engineWorkerPostMessageMock.mockClear()
      await vi.advanceTimersByTimeAsync(60_000)
      // No stop was ever issued: with no budget there is no deadline to elapse.
      expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('stop')
    } finally {
      vi.useRealTimers()
    }
  })

  it('stops the search and marks capFired when a finite budget elapses', async () => {
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(1_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'capped',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      // The deadline lands AFTER the deepest iteration was reported — the exact
      // case a `reachedDepth < requestedDepth` inference gets wrong, since the
      // depths now match yet the run WAS truncated.
      engineMessageHandler?.('info depth 17 score cp 20 pv d2d4')
      await vi.advanceTimersByTimeAsync(1_500)
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('stop')

      engineMessageHandler?.('bestmove d2d4')
      for (let phase = 0; phase < 2; phase += 1) {
        await vi.advanceTimersByTimeAsync(1)
        engineMessageHandler?.('info depth 12 score cp 20 pv e7e5')
        engineMessageHandler?.('bestmove e7e5')
      }

      const message = await vi.waitFor(() => {
        const call = postMessageMock.mock.calls.find(
          ([m]) => m?.type === 'analysis' && m.id === 'capped',
        )
        expect(call).toBeDefined()
        return call![0]
      })
      expect(message.capFired).toBe(true)
      expect(message.reachedDepth).toBe(17)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shares ONE budget across the reset and all three searches, not one each', async () => {
    // A per-search timer would let a single move consume ~3x the intended budget
    // and blow the per-move latency target the tiers are validated against.
    // The budget here is deliberately large relative to the ~1s of fake time
    // `vi.waitFor` may itself advance, so only the explicit advances matter.
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(100_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'shared-budget',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      // Burn ~90% of the shared budget in the root search, then finish it. No
      // stop yet: the move is still inside its budget.
      await vi.advanceTimersByTimeAsync(90_000)
      expect(engineWorkerPostMessageMock).not.toHaveBeenCalledWith('stop')
      engineMessageHandler?.('bestmove d2d4')

      // The SECOND search inherits only what is LEFT. A per-search timer would
      // have handed it a fresh 100s and it would have finished uncapped.
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
          expect.stringContaining('moves e2e4'),
        )
      })
      // Step forward in slices rather than one 12s jump, so the `bestmove`
      // answering this stop lands well inside the post-stop grace. A single long
      // advance would leave the stop unanswered past the grace and trip the
      // wedged-engine teardown — correct behavior, but not what this test is about.
      const sawStop = () =>
        engineWorkerPostMessageMock.mock.calls.some(([c]) => c === 'stop')
      for (let step = 0; step < 40 && !sawStop(); step += 1) {
        await vi.advanceTimersByTimeAsync(500)
      }
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('stop')
      engineMessageHandler?.('bestmove e7e5')

      // The post-best search starts already expired and stops immediately.
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
          expect.stringContaining('moves d2d4'),
        )
      })
      await vi.advanceTimersByTimeAsync(1)
      engineMessageHandler?.('bestmove g8f6')

      const message = await vi.waitFor(() => {
        const call = postMessageMock.mock.calls.find(
          ([m]) => m?.type === 'analysis' && m.id === 'shared-budget',
        )
        expect(call).toBeDefined()
        return call![0]
      })
      // One truncated constituent search poisons the whole move's provenance.
      expect(message.capFired).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('tears down a hung engine on a reset timeout instead of poisoning the FIFO', async () => {
    // A canceled/errored reset leaves a `done` placeholder so its in-flight
    // readyok is absorbed. That is WRONG for a hang: the readyok never comes, so
    // the orphan would swallow the NEXT request's ack and deadlock every
    // subsequent reset. The deadline path must therefore terminate + recreate.
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(1_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      autoReadyok = false // the engine is hung: no reset ack will ever arrive

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'hung-reset',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('ucinewgame')
      })
      await vi.advanceTimersByTimeAsync(1_500)

      // The engine is torn down and the move fails SCOPED to its own id — no
      // partial analysis, because no search ever ran.
      expect(terminateMock).toHaveBeenCalled()
      await vi.waitFor(() => {
        expect(postMessageMock).toHaveBeenCalledWith(
          expect.objectContaining({ type: 'error', id: 'hung-reset' }),
        )
      })
      expect(
        postMessageMock.mock.calls.some(([m]) => m?.type === 'analysis'),
      ).toBe(false)

      // The queue is clean: a fresh engine re-runs the init handshake and the
      // NEXT request's reset resolves normally rather than being absorbed.
      autoReadyok = true
      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'after-teardown',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.runOnlyPendingTimersAsync()
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('bounds the wait for a stop the engine never answers', async () => {
    // `stop` is a REQUEST, not a guarantee. Without a post-stop grace the deadline
    // path is not a wall-clock bound at all — and it fails WORSE than a plain hang:
    // `activeSearch` stays set, so the unconditional liveness heartbeat keeps
    // vouching for the request and the coordinator's inactivity watchdog never
    // trips either. The queue would wedge with no bound anywhere in the system.
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(1_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'ignores-stop',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      const terminatesBefore = terminateMock.mock.calls.length
      await vi.advanceTimersByTimeAsync(1_500)
      expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('stop')
      // The engine gets its grace period first — a slow-but-alive engine that
      // answers within it must NOT be torn down.
      expect(terminateMock.mock.calls.length).toBe(terminatesBefore)

      // ...but no `bestmove` ever comes. The grace expires and the engine is
      // declared wedged.
      await vi.advanceTimersByTimeAsync(2_500)
      expect(terminateMock.mock.calls.length).toBeGreaterThan(terminatesBefore)
      await vi.waitFor(() => {
        expect(postMessageMock).toHaveBeenCalledWith(
          expect.objectContaining({ type: 'error', id: 'ignores-stop' }),
        )
      })
      // No fabricated result: the search never produced a bestmove to report.
      expect(
        postMessageMock.mock.calls.some(([m]) => m?.type === 'analysis'),
      ).toBe(false)

      // The worker recovers — the next request runs against the fresh engine.
      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'after-stop-timeout',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.runOnlyPendingTimersAsync()
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not tear down an engine that answers the stop within the grace', async () => {
    // The complement of the test above: the grace must be a hang detector, not a
    // second deadline. A truncated-but-answered search still returns its shallower
    // result, keeps the engine, and reports capFired.
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(1_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'slow-but-alive',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      const terminatesBefore = terminateMock.mock.calls.length
      engineMessageHandler?.('info depth 14 score cp 20 pv d2d4')
      await vi.advanceTimersByTimeAsync(1_500)
      // Answers late, but inside the grace.
      await vi.advanceTimersByTimeAsync(500)
      engineMessageHandler?.('bestmove d2d4')
      for (let phase = 0; phase < 2; phase += 1) {
        await vi.advanceTimersByTimeAsync(1)
        engineMessageHandler?.('info depth 9 score cp 20 pv e7e5')
        engineMessageHandler?.('bestmove e7e5')
      }

      const message = await vi.waitFor(() => {
        const call = postMessageMock.mock.calls.find(
          ([m]) => m?.type === 'analysis' && m.id === 'slow-but-alive',
        )
        expect(call).toBeDefined()
        return call![0]
      })
      expect(message.capFired).toBe(true)
      expect(message.stopReason).toBe('deadline')
      // Crucially: the engine survived. A grace that fired here would destroy a
      // healthy engine on every capped move.
      expect(terminateMock.mock.calls.length).toBe(terminatesBefore)
    } finally {
      vi.useRealTimers()
    }
  })

  it('shares ONE stop grace across the searches, not a fresh one each', async () => {
    // The grace is the second half of the move's wall-clock bound, so it has to be
    // shared exactly like the deadline is. An analyze-move runs up to three
    // sequential searches and every search entered after the deadline stops
    // immediately, so a per-search grace would let one move run
    // MAX_ANALYSIS_MS + 3x STOP_GRACE_MS — reintroducing, one level down, the very
    // ~3x overshoot the shared deadline exists to prevent, and invalidating the
    // per-move latency bound the finite budget is supposed to promise.
    vi.useFakeTimers()
    try {
      const worker = await import('./analysisWorker')
      worker.__setMaxAnalysisMsForTests(1_000)
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'shared-grace',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'e2e4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      const goCalls = () =>
        engineWorkerPostMessageMock.mock.calls.filter(([c]) =>
          typeof c === 'string' && c.startsWith('go depth'),
        ).length
      await vi.waitFor(() => expect(goCalls()).toBe(1))

      const terminatesBefore = terminateMock.mock.calls.length
      const sawStop = () =>
        engineWorkerPostMessageMock.mock.calls.some(([c]) => c === 'stop')

      // Step to the deadline in slices so the moment the ROOT search is capped —
      // which is when the SHARED grace clock starts — is observed, not assumed.
      engineMessageHandler?.('info depth 14 score cp 20 pv d2d4')
      for (let step = 0; step < 40 && !sawStop(); step += 1) {
        await vi.advanceTimersByTimeAsync(50)
      }
      expect(sawStop()).toBe(true)
      // The stop landed within the last slice, so the true grace expiry is in
      // [graceExpiresAt - 50, graceExpiresAt]. Every assertion below keeps a wide
      // margin around that.
      const graceExpiresAt = Date.now() + 2_000

      // The engine is slow but ALIVE: it answers the root stop 500ms in and
      // survives. The post-played search then starts against an already-expired
      // deadline, stops immediately — and wedges, never answering.
      await vi.advanceTimersByTimeAsync(500)
      engineMessageHandler?.('bestmove d2d4')
      for (let tick = 0; tick < 20 && goCalls() < 2; tick += 1) {
        // Flush microtasks WITHOUT advancing: `vi.waitFor` polls by advancing fake
        // timers, which would blur the very timing this test measures.
        await vi.advanceTimersByTimeAsync(0)
      }
      expect(goCalls()).toBe(2)

      // Still inside the shared grace: no teardown yet.
      await vi.advanceTimersByTimeAsync(
        Math.max(0, graceExpiresAt - 200 - Date.now()),
      )
      expect(terminateMock.mock.calls.length).toBe(terminatesBefore)

      // Deadline + ONE grace. With a per-search grace the post-played search would
      // have been armed fresh when it started (~500ms after the root stop) and had
      // until ~graceExpiresAt + 500, so nothing would have fired here and the move
      // would have overrun the bound the finite budget promises.
      await vi.advanceTimersByTimeAsync(300)
      expect(terminateMock.mock.calls.length).toBeGreaterThan(terminatesBefore)
      await vi.waitFor(() => {
        expect(postMessageMock).toHaveBeenCalledWith(
          expect.objectContaining({ type: 'error', id: 'shared-grace' }),
        )
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('reports stopReason=bestmove for an uncapped move', async () => {
    await import('./analysisWorker')
    engineMessageHandler?.('uciok')
    engineMessageHandler?.('readyok')

    const message = await runAnalysisToCompletion('reason-natural')
    expect(message.stopReason).toBe('bestmove')
    expect(message.capFired).toBe(false)
  })

  describe('atomic snapshot instrumentation (g-two-search-grade §4.3)', () => {
    /**
     * A recorded engine transcript whose ROOT search makes the two selectors
     * disagree, and whose post-move searches make them agree.
     *
     * The root phase reports an aspiration fail-high at depth 16 (which moves
     * `lastScore` even though it is not a settled evaluation), a settled depth-16
     * line, and then a depth-17 `currmove` status line that moves `lastDepth`
     * without ever producing a depth-17 PV. The legacy accumulators therefore
     * report a depth-17 search that no depth-17 line ever corroborated; the
     * atomic selector's deepest complete batch is depth 16 and it rejects.
     */
    const TRANSCRIPT = [
      {
        marker: 'position fen rnbqkbnr',
        lines: [
          'info depth 15 multipv 1 score cp 28 nodes 1000 time 10 pv e2e4 e7e5 g1f3',
          'info depth 16 score cp 62 lowerbound nodes 1500 time 15 pv e2e4 c7c5',
          'info depth 16 multipv 1 score cp 34 nodes 1800 time 18 pv e2e4 e7e5 b1c3',
          'info depth 17 currmove e2e4 currmovenumber 1',
        ],
        bestmove: 'bestmove e2e4',
      },
      {
        marker: 'moves d2d4',
        lines: ['info depth 17 multipv 1 score cp -12 nodes 4000 time 40 pv d7d5 c2c4'],
        bestmove: 'bestmove d7d5',
      },
      {
        marker: 'moves e2e4',
        lines: ['info depth 17 multipv 1 score cp -20 nodes 5000 time 50 pv e7e5 g1f3'],
        bestmove: 'bestmove e7e5',
      },
    ]

    /** Replay the transcript for one analyze-move; returns its response and UCI stream. */
    const replayTranscript = async (id: string) => {
      const commands: string[] = []
      engineWorkerPostMessageMock.mockClear()

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id,
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'd2d4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )

      for (const phase of TRANSCRIPT) {
        await vi.waitFor(() => {
          expect(engineWorkerPostMessageMock).toHaveBeenCalledWith(
            expect.stringContaining(phase.marker),
          )
        })
        for (const line of phase.lines) {
          engineMessageHandler?.(line)
        }
        engineMessageHandler?.(phase.bestmove)
        // Snapshot this phase's commands and clear, so the NEXT phase's marker
        // wait cannot match a command an earlier phase already posted.
        commands.push(...engineWorkerPostMessageMock.mock.calls.map(([c]) => c as string))
        engineWorkerPostMessageMock.mockClear()
      }

      const analysis = await vi.waitFor(() => {
        const call = postMessageMock.mock.calls.find(
          ([m]) => m?.type === 'analysis' && m.id === id,
        )
        expect(call).toBeDefined()
        return call![0] as Record<string, unknown>
      })

      const { id: _id, ...payload } = analysis
      return { payload, commands }
    }

    it('emits an identical result and UCI stream with assembly enabled and disabled', async () => {
      const worker = await import('./analysisWorker')
      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')

      worker.__setSnapshotInstrumentationForTests(true)
      const withAssembly = await replayTranscript('assembly-on')

      worker.__setSnapshotInstrumentationForTests(false)
      const withoutAssembly = await replayTranscript('assembly-off')

      // The legacy arm's selector is the accumulators, and nothing on this path
      // reads a snapshot, so capturing them cannot move a single emitted value.
      expect(withAssembly.payload).toEqual(withoutAssembly.payload)
      expect(withAssembly.commands).toEqual(withoutAssembly.commands)

      // Pinned so the identity above cannot be satisfied by both arms breaking.
      expect(withAssembly.payload).toMatchObject({
        move: 'd2d4',
        bestMove: 'e2e4',
        bestLine: ['e2e4', 'e7e5', 'b1c3'],
        canonical: true,
        capFired: false,
        stopReason: 'bestmove',
        // The accumulator hazard, preserved verbatim: no depth-17 root line ever
        // carried a PV, yet the legacy selector reports depth 17.
        reachedDepth: 17,
      })
    })

    it('counts legacy_selector_divergence by reason without changing any emitted value', async () => {
      const worker = await import('./analysisWorker')
      // Same module registry as the worker's own import, so these are the very
      // counters it mutates.
      const { getSnapshotCounters, resetSnapshotCounters } = await import('./pvSnapshots')
      resetSnapshotCounters()

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      worker.__setSnapshotInstrumentationForTests(true)

      const { payload } = await replayTranscript('divergence')

      const counters = getSnapshotCounters()
      // Root diverges (stale-depth); both post-move searches agree exactly.
      expect(counters.legacy_selector_divergence).toBe(1)
      expect(counters.divergence_by_reason['stale-depth']).toBe(1)
      expect(counters.divergence_by_reason.accepted).toBe(0)
      expect(counters.rejections['stale-depth']).toBe(1)

      // The measured row was still emitted, unchanged, by the legacy selector.
      expect(payload).toMatchObject({ bestMove: 'e2e4', reachedDepth: 17 })
    })

    it('counts nothing at all while instrumentation is disabled', async () => {
      const worker = await import('./analysisWorker')
      const { getSnapshotCounters, resetSnapshotCounters } = await import('./pvSnapshots')
      resetSnapshotCounters()

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      worker.__setSnapshotInstrumentationForTests(false)

      await replayTranscript('no-instrumentation')

      const counters = getSnapshotCounters()
      expect(counters.legacy_selector_divergence).toBe(0)
      expect(counters.batches_complete).toBe(0)
    })

    it('tallies a canceled search instead of scoring it as a divergence', async () => {
      const worker = await import('./analysisWorker')
      const { getSnapshotCounters, resetSnapshotCounters } = await import('./pvSnapshots')
      resetSnapshotCounters()

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      worker.__setSnapshotInstrumentationForTests(true)

      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'canceled',
            fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            move: 'd2d4',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      // Depth 15 against a requested 17: were this search graded, the atomic
      // selector would reject it `stale-depth` and the row would be counted.
      engineMessageHandler?.('info depth 15 multipv 1 score cp 28 nodes 900 time 9 pv e2e4 e7e5')

      messageHandler?.(
        new MessageEvent('message', {
          data: { type: 'cancel-analysis', id: 'canceled' } satisfies AnalysisWorkerRequest,
        }),
      )
      // Stockfish answers the cancel's `stop` with a truncated bestmove.
      engineMessageHandler?.('bestmove e2e4')

      await vi.waitFor(() => {
        expect(getSnapshotCounters().searches_canceled).toBe(1)
      })

      // No row exists to disagree about: analyzeMove unwinds and posts nothing.
      expect(postMessageMock).not.toHaveBeenCalledWith(
        expect.objectContaining({ type: 'analysis', id: 'canceled' }),
      )
      const counters = getSnapshotCounters()
      expect(counters.legacy_selector_divergence).toBe(0)
      expect(counters.rejections['stale-depth']).toBe(0)
    })

    it('exempts a real one-move mating PV from the two-move minimum', async () => {
      const worker = await import('./analysisWorker')
      const { getSnapshotCounters, resetSnapshotCounters } = await import('./pvSnapshots')
      resetSnapshotCounters()

      engineMessageHandler?.('uciok')
      engineMessageHandler?.('readyok')
      worker.__setSnapshotInstrumentationForTests(true)

      // Ra1-a8 is mate, so it is both the played move and the best move: the two
      // follow-up searches resolve terminally and the root search is the only one.
      messageHandler?.(
        new MessageEvent('message', {
          data: {
            type: 'analyze-move',
            id: 'mate-in-one',
            fen: '6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1',
            move: 'a1a8',
            playerColor: 'white',
          } satisfies AnalysisWorkerRequest,
        }),
      )
      await vi.waitFor(() => {
        expect(engineWorkerPostMessageMock).toHaveBeenCalledWith('go depth 17')
      })

      // A mate in one has nothing to continue with, so its PV is one move long.
      engineMessageHandler?.('info depth 17 multipv 1 score mate 1 nodes 800 time 8 pv a1a8')
      engineMessageHandler?.('bestmove a1a8')

      await vi.waitFor(() => {
        expect(postMessageMock).toHaveBeenCalledWith(
          expect.objectContaining({ type: 'analysis', id: 'mate-in-one' }),
        )
      })

      // The worker supplies `endsGame` from the search's own root position, so
      // the exemption is available in a real run and not only to a unit test that
      // passes the callback by hand.
      const counters = getSnapshotCounters()
      expect(counters.rejections['pv-short']).toBe(0)
      expect(counters.legacy_selector_divergence).toBe(0)
    })

    it('keeps the atomic selector out of every production path', () => {
      const srcRoot = resolve(__dirname, '..')

      // The shared worker does not even NAME the atomic selector: its only
      // atomic-selector call goes through the void-returning divergence recorder,
      // so no selection can reach an emitted value by accident.
      expect(readFileSync(resolve(__dirname, 'analysisWorker.ts'), 'utf8')).not.toContain(
        'selectAtomicSnapshot',
      )

      // Production modules only: a test may name the selector freely.
      const productionSources = (dir: string): string[] =>
        readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
          const full = resolve(dir, entry.name)
          if (entry.isDirectory()) return productionSources(full)
          return /\.tsx?$/.test(entry.name) && !/\.(test|spec)\.tsx?$/.test(entry.name)
            ? [full]
            : []
        })

      const referencing = productionSources(srcRoot)
        .filter((file) => readFileSync(file, 'utf8').includes('selectAtomicSnapshot'))
        .map((file) => relative(srcRoot, file))
        // The benchmark harness (src/bench, g-grade-device-runner) replays a
        // worker's logged UCI transcript through the selector to REPORT §4.2
        // acceptance. It is not a production path: a separate BENCH=1-gated Vite
        // entry that nothing in the app imports — pinned by
        // `src/bench/isolation.test.ts`, which is what bounds this exemption.
        .filter((file) => !file.startsWith('bench/'))
        .sort()

      // Only the module that DEFINES it. Candidate arms (§12 step 3) will be its
      // first callers and production takes it over at §12 step 9, not before —
      // extending this list is a deliberate act, never an accident.
      expect(referencing).toEqual(['workers/pvSnapshots.ts'])
    })
  })
})
