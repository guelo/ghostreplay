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

  beforeEach(() => {
    vi.resetModules()

    engineWorkerPostMessageMock = vi.fn()
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
})
