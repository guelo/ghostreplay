import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useMoveAnalysis } from './useMoveAnalysis'

type MessageHandler = (event: MessageEvent) => void
type ErrorHandler = (event: ErrorEvent) => void
type WorkerListener = MessageHandler | ErrorHandler

let messageHandler: MessageHandler | null = null
let errorHandler: ErrorHandler | null = null

const postMessageMock = vi.fn()
const terminateMock = vi.fn()

// Must be a real function (not arrow) so it's new-able
function MockWorker() {
  // @ts-expect-error -- mock constructor
  this.postMessage = postMessageMock
  // @ts-expect-error -- mock constructor
  this.addEventListener = vi.fn((type: string, handler: WorkerListener) => {
    if (type === 'message') messageHandler = handler as MessageHandler
    if (type === 'error') errorHandler = handler as ErrorHandler
  })
  // @ts-expect-error -- mock constructor
  this.removeEventListener = vi.fn()
  // @ts-expect-error -- mock constructor
  this.terminate = terminateMock
}

vi.stubGlobal('Worker', MockWorker)

const simulateMessage = (data: Record<string, unknown>) => {
  messageHandler?.({ data } as MessageEvent)
}

const simulateError = (message: string) => {
  errorHandler?.({ message } as ErrorEvent)
}

describe('useMoveAnalysis', () => {
  beforeEach(() => {
    postMessageMock.mockClear()
    terminateMock.mockClear()
    messageHandler = null
    errorHandler = null
  })

  it('initializes with booting status', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    expect(result.current.status).toBe('booting')
    expect(result.current.error).toBeNull()
    expect(result.current.lastAnalysis).toBeNull()
    expect(result.current.isAnalyzing).toBe(false)
    expect(result.current.analyzingMove).toBeNull()
  })

  it('transitions to ready when worker sends ready message', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    expect(result.current.status).toBe('ready')
  })

  it('transitions to error on worker error message', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'error', error: 'Engine failed to load' })
    })

    expect(result.current.status).toBe('error')
    expect(result.current.error).toBe('Engine failed to load')
    expect(result.current.isAnalyzing).toBe(false)
    expect(result.current.analyzingMove).toBeNull()
  })

  it('transitions to error on worker ErrorEvent', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateError('Worker script failed')
    })

    expect(result.current.status).toBe('error')
    expect(result.current.error).toBe('Worker script failed')
  })

  it('sets analyzing state on analysis-started message', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({ type: 'analysis-started', id: 'req-1', move: 'e2e4' })
    })

    expect(result.current.isAnalyzing).toBe(true)
    expect(result.current.analyzingMove).toBe('e2e4')
  })

  it('populates lastAnalysis on analysis result', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: 'req-1',
        move: 'e2e4',
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: -150,
        delta: 200,
        blunder: true,
      })
    })

    expect(result.current.isAnalyzing).toBe(false)
    expect(result.current.analyzingMove).toBeNull()
    expect(result.current.lastAnalysis).toEqual({
      id: 'req-1',
      move: 'e2e4',
      bestMove: 'd2d4',
      bestEval: 50,
      playedEval: -150,
      currentPositionEval: -150,
      moveIndex: null,
      delta: 200,
      blunder: true,
    })
  })

  it('sets blunder flag correctly for non-blunder analysis', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: 'req-2',
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 40,
        delta: 10,
        blunder: false,
      })
    })

    expect(result.current.lastAnalysis?.blunder).toBe(false)
    expect(result.current.lastAnalysis?.delta).toBe(10)
  })

  it('posts correct message to worker on analyzeMove', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

    act(() => {
      result.current.analyzeMove(fen, 'e2e4', 'white')
    })

    expect(postMessageMock).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'analyze-move',
        fen,
        move: 'e2e4',
        playerColor: 'white',
        id: expect.any(String),
      }),
    )
  })

  it('does not post to worker when status is error', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'error', error: 'broken' })
    })

    act(() => {
      result.current.analyzeMove('some-fen', 'e2e4', 'white')
    })

    expect(postMessageMock).not.toHaveBeenCalled()
  })

  it('terminates worker on unmount', () => {
    const { unmount } = renderHook(() => useMoveAnalysis())

    unmount()

    expect(terminateMock).toHaveBeenCalled()
  })

  it('clears analyzing state when error occurs during analysis', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({ type: 'analysis-started', id: 'req-1', move: 'e2e4' })
    })

    expect(result.current.isAnalyzing).toBe(true)

    act(() => {
      simulateMessage({ type: 'error', error: 'Analysis failed' })
    })

    expect(result.current.isAnalyzing).toBe(false)
    expect(result.current.analyzingMove).toBeNull()
  })

  it('stores result in analysisMap when moveIndex is provided', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    // Call analyzeMove with moveIndex
    act(() => {
      result.current.analyzeMove(
        'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
        'e2e4',
        'white',
        0,
      )
    })

    // Get the request ID from the posted message
    const postedMessage = postMessageMock.mock.calls[0][0]
    const requestId = postedMessage.id

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: 30,
        delta: 20,
        blunder: false,
      })
    })

    expect(result.current.analysisMap.size).toBe(1)
    expect(result.current.analysisMap.get(0)).toEqual(
      expect.objectContaining({ move: 'e2e4', delta: 20, moveIndex: 0 }),
    )
    expect(result.current.lastAnalysis?.moveIndex).toBe(0)
  })

  it('accumulates multiple results in analysisMap at different indices', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    // First move at index 0
    act(() => {
      result.current.analyzeMove('fen-1', 'e2e4', 'white', 0)
    })
    const id1 = postMessageMock.mock.calls[0][0].id

    // Second move at index 2
    act(() => {
      result.current.analyzeMove('fen-2', 'd2d4', 'white', 2)
    })
    const id2 = postMessageMock.mock.calls[1][0].id

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: id1,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        blunder: false,
      })
    })

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: id2,
        move: 'd2d4',
        bestMove: 'd2d4',
        bestEval: 40,
        playedEval: 30,
        delta: 10,
        blunder: false,
      })
    })

    expect(result.current.analysisMap.size).toBe(2)
    expect(result.current.analysisMap.get(0)?.move).toBe('e2e4')
    expect(result.current.analysisMap.get(2)?.move).toBe('d2d4')
  })

  it('does not store in analysisMap when moveIndex is omitted', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    // Call without moveIndex
    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white')
    })

    const requestId = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        blunder: false,
      })
    })

    expect(result.current.analysisMap.size).toBe(0)
    // But lastAnalysis should still be set
    expect(result.current.lastAnalysis).not.toBeNull()
  })

  it('clears all state on clearAnalysis', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })

    const requestId = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        blunder: false,
      })
    })

    expect(result.current.analysisMap.size).toBe(1)
    expect(result.current.lastAnalysis).not.toBeNull()

    act(() => {
      result.current.clearAnalysis()
    })

    expect(result.current.analysisMap.size).toBe(0)
    expect(result.current.lastAnalysis).toBeNull()
  })

  it('handles analysis with null eval values', () => {
    const { result } = renderHook(() => useMoveAnalysis())

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: 'req-3',
        move: 'e2e4',
        bestMove: '(none)',
        bestEval: null,
        playedEval: null,
        delta: null,
        blunder: false,
      })
    })

    expect(result.current.lastAnalysis).toEqual({
      id: 'req-3',
      move: 'e2e4',
      bestMove: '(none)',
      bestEval: null,
      playedEval: null,
      currentPositionEval: null,
      moveIndex: null,
      delta: null,
      blunder: false,
    })
  })
})
