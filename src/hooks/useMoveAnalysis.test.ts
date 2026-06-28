import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useMoveAnalysis } from './useMoveAnalysis'
import { createAnalysisStore, type AnalysisStore } from '../stores/createAnalysisStore'

const lookupAnalysisCacheMock = vi.fn()

vi.mock('../utils/api', () => ({
  lookupAnalysisCache: (...args: unknown[]) => lookupAnalysisCacheMock(...args),
}))

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
  let store: AnalysisStore

  beforeEach(() => {
    postMessageMock.mockClear()
    terminateMock.mockClear()
    lookupAnalysisCacheMock.mockReset()
    lookupAnalysisCacheMock.mockResolvedValue(new Map())
    messageHandler = null
    errorHandler = null
    store = createAnalysisStore()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('initializes with booting status', () => {
    renderHook(() => useMoveAnalysis(store))

    const s = store.getState()
    expect(s.status).toBe('booting')
    expect(s.error).toBeNull()
    expect(s.lastAnalysis).toBeNull()
    expect(s.isAnalyzing).toBe(false)
    expect(s.analyzingMove).toBeNull()
  })

  it('transitions to ready when worker sends ready message', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    expect(store.getState().status).toBe('ready')
  })

  it('transitions to error on worker error message', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'error', error: 'Engine failed to load' })
    })

    const s = store.getState()
    expect(s.status).toBe('error')
    expect(s.error).toBe('Engine failed to load')
    expect(s.isAnalyzing).toBe(false)
    expect(s.analyzingMove).toBeNull()
  })

  it('transitions to error on worker ErrorEvent', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateError('Worker script failed')
    })

    const s = store.getState()
    expect(s.status).toBe('error')
    expect(s.error).toBe('Worker script failed')
  })

  it('sets analyzing state on analysis-started message', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({ type: 'analysis-started', id: 'req-1', move: 'e2e4' })
    })

    const s = store.getState()
    expect(s.isAnalyzing).toBe(true)
    expect(s.analyzingMove).toBe('e2e4')
  })

  it('populates lastAnalysis on analysis result', () => {
    renderHook(() => useMoveAnalysis(store))

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
        playedEvalMate: null,
        bestEvalMate: null,
        delta: 200,
        classification: 'blunder',
      })
    })

    const s = store.getState()
    expect(s.isAnalyzing).toBe(false)
    expect(s.analyzingMove).toBeNull()
    expect(s.lastAnalysis).toEqual({
      id: 'req-1',
      move: 'e2e4',
      bestMove: 'd2d4',
      bestEval: 50,
      playedEval: -150,
      currentPositionEval: -150,
      playedEvalMate: null,
      currentPositionEvalMate: null,
      moveIndex: null,
      delta: 200,
      classification: 'blunder',
      blunder: true,
      recordable: false,
    })
  })

  it('propagates a worker mate count into the resolved analysis', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    let requestId: string | undefined
    act(() => {
      requestId = result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 10000,
        playedEval: 10000,
        playedEvalMate: 2,
        bestEvalMate: 2,
        delta: 0,
        classification: 'best',
      })
    })

    // Worker result is buffered until the cache misses and releases it.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    const resolved = store.getState().analysisMap.get(0)
    expect(resolved?.playedEvalMate).toBe(2)
    expect(resolved?.currentPositionEvalMate).toBe(2)
  })

  it('sets blunder flag correctly for non-blunder analysis', () => {
    renderHook(() => useMoveAnalysis(store))

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
        classification: 'excellent',
      })
    })

    const s = store.getState()
    expect(s.lastAnalysis?.blunder).toBe(false)
    expect(s.lastAnalysis?.classification).toBe('excellent')
    expect(s.lastAnalysis?.delta).toBe(10)
  })

  it('posts correct message to worker on analyzeMove', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

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

  it('clears variation streaming state on worker error message', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id, move: 'e7e5', cp: 40 })
    })
    expect(store.getState().variationStreamingEval).toEqual({ ply: 2, fen, cp: 40 })

    act(() => {
      simulateMessage({ type: 'error', error: 'Engine crashed' })
    })
    expect(store.getState().variationStreamingEval).toBeNull()
  })

  it('clears variation streaming state on worker ErrorEvent', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id, move: 'e7e5', cp: 40 })
    })
    expect(store.getState().variationStreamingEval).not.toBeNull()

    act(() => {
      simulateError('Worker script failed')
    })
    expect(store.getState().variationStreamingEval).toBeNull()
  })

  it('invokes onVariationError on a scoped variation error (Finding F3)', () => {
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({ type: 'error', id, error: 'Variation search failed' })
    })

    expect(onVariationError).toHaveBeenCalledTimes(1)
    expect(onVariationError).toHaveBeenCalledWith(id)
    // Scoped error must not flip global status.
    expect(store.getState().status).not.toBe('error')
  })

  it('invokes onVariationError for every pending variation on fatal teardown (Finding F3)', () => {
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fenA = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
    const fenB = 'rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fenA, 'e7e5', 'white', undefined, undefined, 2, fenA)
      result.current.analyzeMove(fenB, 'd7d5', 'white', undefined, undefined, 2, fenB)
    })
    const idA = postMessageMock.mock.calls[0][0].id
    const idB = postMessageMock.mock.calls[1][0].id

    act(() => {
      simulateMessage({ type: 'error', error: 'Engine crashed' })
    })

    expect(onVariationError).toHaveBeenCalledWith(idA)
    expect(onVariationError).toHaveBeenCalledWith(idB)
    expect(onVariationError).toHaveBeenCalledTimes(2)
  })

  it('invokes onVariationError for pending variations on clearAnalysis (Finding F3)', () => {
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => {
      result.current.clearAnalysis()
    })

    expect(onVariationError).toHaveBeenCalledTimes(1)
    expect(onVariationError).toHaveBeenCalledWith(id)
  })

  it('invokes onVariationError for pending variations on unmount (Finding F3)', () => {
    const onVariationError = vi.fn()
    const { result, unmount } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    unmount()

    expect(onVariationError).toHaveBeenCalledTimes(1)
    expect(onVariationError).toHaveBeenCalledWith(id)
  })

  it('invokes onVariationError when a variation request times out (Finding F3)', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    // A silent variation fails after the per-variation inactivity watchdog window
    // (ANALYSIS_INACTIVITY_TIMEOUT_MS).
    act(() => {
      vi.advanceTimersByTime(8_000)
    })

    expect(onVariationError).toHaveBeenCalledTimes(1)
    expect(onVariationError).toHaveBeenCalledWith(id)
  })

  it('keeps a progressing variation alive past the inactivity window', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove(fen, 'e7e5', 'white', undefined, undefined, 2, fen)
    })
    const id = postMessageMock.mock.calls[0][0].id

    // Progress every < window, total elapsed well past 2× the window — each ping
    // resets the variation watchdog, so a slow-but-progressing variation survives.
    act(() => {
      for (let i = 0; i < 8; i++) {
        simulateMessage({ type: 'analysis-progress', id })
        vi.advanceTimersByTime(6_000)
      }
    })
    expect(onVariationError).not.toHaveBeenCalled()

    // It still fails once it finally goes silent for a full window.
    act(() => { vi.advanceTimersByTime(8_000) })
    expect(onVariationError).toHaveBeenCalledTimes(1)
    expect(onVariationError).toHaveBeenCalledWith(id)
  })

  it('fails a started-but-silent variation even while an indexed request progresses', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove('fen-0', 'e2e4', 'white', 0)
      result.current.analyzeMove('fen-v', 'e7e5', 'white', undefined, undefined, 2, 'fen-v')
    })
    const analyzeCalls = postMessageMock.mock.calls.filter(([m]) => m.type === 'analyze-move')
    const indexedId = analyzeCalls.find((c) => c[0].moveIndex === 0)![0].id
    const varId = analyzeCalls.find((c) => c[0].moveIndex === undefined)![0].id

    act(() => {
      simulateMessage({ type: 'analysis-started', id: indexedId, move: 'e2e4' })
      simulateMessage({ type: 'analysis-started', id: varId, move: 'e7e5' })
    })

    // The indexed request progresses; the STARTED variation stays silent. A
    // started request is reset only by its own activity, so unrelated indexed
    // progress does NOT keep the variation alive — it fails after its window.
    act(() => {
      for (let i = 0; i < 8; i++) {
        simulateMessage({ type: 'analysis-progress', id: indexedId })
        vi.advanceTimersByTime(6_000)
      }
    })
    expect(onVariationError).toHaveBeenCalledWith(varId)
  })

  it('keeps a queued (not-started) variation alive behind a progressing indexed request', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove('fen-0', 'e2e4', 'white', 0)
      result.current.analyzeMove('fen-v', 'e7e5', 'white', undefined, undefined, 2, 'fen-v')
    })
    const analyzeCalls = postMessageMock.mock.calls.filter(([m]) => m.type === 'analyze-move')
    const indexedId = analyzeCalls.find((c) => c[0].moveIndex === 0)![0].id
    const varId = analyzeCalls.find((c) => c[0].moveIndex === undefined)![0].id

    act(() => { simulateMessage({ type: 'analysis-started', id: indexedId, move: 'e2e4' }) })

    // The indexed request progresses while the variation stays silent and
    // not-started; serial-worker liveness keeps the queued variation alive.
    act(() => {
      for (let i = 0; i < 8; i++) {
        simulateMessage({ type: 'analysis-progress', id: indexedId })
        vi.advanceTimersByTime(6_000)
      }
    })
    expect(onVariationError).not.toHaveBeenCalled()

    // The variation finally starts and resolves — still no error.
    act(() => {
      simulateMessage({ type: 'analysis-started', id: varId, move: 'e7e5' })
      simulateMessage({
        type: 'analysis', id: varId, move: 'e7e5', bestMove: 'e7e5',
        bestEval: 0, playedEval: 0, delta: 0, classification: 'best',
      })
    })
    expect(onVariationError).not.toHaveBeenCalled()
  })

  it('stale variation progress does NOT extend a queued indexed request (self-guard)', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))

    act(() => {
      simulateMessage({ type: 'ready' })
      // Queued indexed request B (no activity → stays not-started).
      result.current.analyzeMove('fen-0', 'e2e4', 'white', 0)
      // A variation we then terminate so its id becomes stale/deleted.
      result.current.analyzeMove('fen-v', 'e7e5', 'white', undefined, undefined, 2, 'fen-v')
    })
    const analyzeCalls = postMessageMock.mock.calls.filter(([m]) => m.type === 'analyze-move')
    const indexedId = analyzeCalls.find((c) => c[0].moveIndex === 0)![0].id
    const varId = analyzeCalls.find((c) => c[0].moveIndex === undefined)![0].id

    // Terminate the variation → its id leaves pendingVariationPlies.
    act(() => {
      simulateMessage({
        type: 'analysis', id: varId, move: 'e7e5', bestMove: 'e7e5',
        bestEval: 0, playedEval: 0, delta: 0, classification: 'best',
      })
    })

    // Just before B's window (armed at t=0 since ready): a stale variation ping
    // must be inert and must NOT re-arm B via serial liveness.
    act(() => { vi.advanceTimersByTime(7_000) })
    act(() => { simulateMessage({ type: 'analysis-progress', id: varId }) })

    // Past B's original window with no LIVE activity → B fails at its window,
    // canceling its worker request.
    act(() => { vi.advanceTimersByTime(2_000) })
    const canceledB = postMessageMock.mock.calls.some(
      ([m]) => m.type === 'cancel-analysis' && m.id === indexedId,
    )
    expect(canceledB).toBe(true)
  })

  it('resets an indexed request watchdog on analysis-progress', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })

    // Progress every < window keeps the indexed request alive well past the
    // window — the spinner stays up and nothing is failed.
    act(() => {
      for (let i = 0; i < 8; i++) {
        simulateMessage({ type: 'analysis-progress', id })
        vi.advanceTimersByTime(6_000)
      }
    })
    expect(store.getState().isAnalyzing).toBe(true)
    expect(store.getState().analysisMap.has(0)).toBe(false)

    // A final worker result still resolves it.
    act(() => {
      simulateMessage({
        type: 'analysis', id, move: 'e2e4', bestMove: 'e2e4',
        bestEval: 10, playedEval: 10, delta: 0, classification: 'best',
      })
    })
    expect(store.getState().analysisMap.get(0)?.id).toBe(id)
  })

  it('a late analysis-started after an indexed watchdog failure does not revive the spinner (P2)', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    // Cache miss → released; the started request then goes silent → watchdog fails
    // it (entry removed, id still in latestRequestIds).
    act(() => { vi.advanceTimersByTime(200) })
    act(() => { vi.advanceTimersByTime(8_000) })
    expect(store.getState().isAnalyzing).toBe(false)

    // A racey late analysis-started for the failed request must NOT revive it —
    // the start gate now requires a live resolution entry (P2).
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(false)
  })

  it('drops a late result/start for a timed-out variation (P1/P2)', () => {
    vi.useFakeTimers()
    const onVariationError = vi.fn()
    const { result } = renderHook(() => useMoveAnalysis(store, onVariationError))

    act(() => {
      simulateMessage({ type: 'ready' })
      result.current.analyzeMove('fen-v', 'e7e5', 'white', undefined, undefined, 2, 'fen-v')
    })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e7e5' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    // The started variation goes silent → its watchdog fails it, clearing the
    // spinner and tombstoning the id.
    act(() => { vi.advanceTimersByTime(8_000) })
    expect(onVariationError).toHaveBeenCalledWith(id)
    expect(store.getState().isAnalyzing).toBe(false)
    expect(store.getState().lastAnalysis).toBeNull()

    // A racey late analysis-started for the canceled variation must NOT revive the
    // spinner (P2).
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e7e5' }) })
    expect(store.getState().isAnalyzing).toBe(false)

    // A racey late analysis for the canceled variation must NOT fall through to the
    // ad-hoc branch and clobber lastAnalysis (P1).
    act(() => {
      simulateMessage({
        type: 'analysis', id, move: 'e7e5', bestMove: 'e7e5',
        bestEval: 0, playedEval: 0, delta: 0, classification: 'best',
      })
    })
    expect(store.getState().lastAnalysis).toBeNull()
  })

  it('does not post to worker when status is error', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'error', error: 'broken' })
    })

    act(() => {
      result.current.analyzeMove('some-fen', 'e2e4', 'white')
    })

    expect(postMessageMock).not.toHaveBeenCalled()
  })

  it('terminates worker on unmount', () => {
    const { unmount } = renderHook(() => useMoveAnalysis(store))

    unmount()

    expect(terminateMock).toHaveBeenCalled()
  })

  it('clears analyzing state when error occurs during analysis', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({ type: 'analysis-started', id: 'req-1', move: 'e2e4' })
    })

    expect(store.getState().isAnalyzing).toBe(true)

    act(() => {
      simulateMessage({ type: 'error', error: 'Analysis failed' })
    })

    const s = store.getState()
    expect(s.isAnalyzing).toBe(false)
    expect(s.analyzingMove).toBeNull()
  })

  it('stores result in analysisMap when moveIndex is provided', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

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
        classification: 'good',
      })
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    const s = store.getState()
    expect(s.analysisMap.size).toBe(1)
    expect(s.analysisMap.get(0)).toEqual(
      expect.objectContaining({ move: 'e2e4', delta: 20, moveIndex: 0 }),
    )
    expect(s.lastAnalysis?.moveIndex).toBe(0)
  })

  it('accumulates multiple results in analysisMap at different indices', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

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
        classification: 'best',
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
        classification: 'excellent',
      })
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    const s = store.getState()
    expect(s.analysisMap.size).toBe(2)
    expect(s.analysisMap.get(0)?.move).toBe('e2e4')
    expect(s.analysisMap.get(2)?.move).toBe('d2d4')
  })

  it('does not store in analysisMap when moveIndex is omitted', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

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
        classification: 'best',
      })
    })

    const s = store.getState()
    expect(s.analysisMap.size).toBe(0)
    // But lastAnalysis should still be set
    expect(s.lastAnalysis).not.toBeNull()
  })

  it('clears all state on clearAnalysis', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

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
        classification: 'best',
      })
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    expect(store.getState().analysisMap.size).toBe(1)
    expect(store.getState().lastAnalysis).not.toBeNull()

    act(() => {
      result.current.clearAnalysis()
    })

    const s = store.getState()
    expect(s.analysisMap.size).toBe(0)
    expect(s.lastAnalysis).toBeNull()
  })

  // ── analysis-streaming & throttle ────────────────────────────────

  it('sets streamingEval on first analysis-streaming message', () => {
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 3)
    })

    const requestId = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 42, depth: 5 })
    })

    expect(store.getState().streamingEval).toEqual({ moveIndex: 3, cp: 42 })
  })

  it('throttles rapid streaming updates to ~250ms intervals', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })

    const requestId = postMessageMock.mock.calls[0][0].id

    // First message goes through immediately
    const now = vi.spyOn(performance, 'now')
    now.mockReturnValue(1000)

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 10, depth: 1 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 10 })

    // Second message 100ms later — should be throttled
    now.mockReturnValue(1100)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 20, depth: 2 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 10 })

    // Third message 249ms after first — still throttled
    now.mockReturnValue(1249)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 30, depth: 3 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 10 })

    // Fourth message 250ms after first — goes through
    now.mockReturnValue(1250)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 40, depth: 4 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 40 })

  })

  it('resets throttle timer when analysis completes, allowing immediate update for next move', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    const now = vi.spyOn(performance, 'now')

    // Analyze first move
    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })
    const id1 = postMessageMock.mock.calls[0][0].id

    // Stream at t=1000
    now.mockReturnValue(1000)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: id1, cp: 50, depth: 10 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 50 })

    // Complete analysis (resets throttle timer)
    act(() => {
      simulateMessage({
        type: 'analysis',
        id: id1,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        classification: 'best',
      })
    })
    expect(store.getState().streamingEval).toBeNull()

    // Analyze second move — first streaming message should go through immediately
    // even though only 10ms has passed since previous stream update
    act(() => {
      result.current.analyzeMove('fen-2', 'd2d4', 'white', 1)
    })
    const id2 = postMessageMock.mock.calls[1][0].id

    now.mockReturnValue(1010)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: id2, cp: 30, depth: 1 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 1, cp: 30 })

  })

  it('resets throttle timer on clearAnalysis', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    const now = vi.spyOn(performance, 'now')

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })
    const requestId = postMessageMock.mock.calls[0][0].id

    now.mockReturnValue(1000)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 50, depth: 5 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 50 })

    // Clear all analysis state
    act(() => {
      result.current.clearAnalysis()
    })
    expect(store.getState().streamingEval).toBeNull()

    // New analysis — first stream should go through immediately
    act(() => {
      result.current.analyzeMove('fen-2', 'd2d4', 'white', 0)
    })
    const id2 = postMessageMock.mock.calls
      .filter((c) => c[0].type === 'analyze-move')
      .at(-1)![0].id

    now.mockReturnValue(1010)
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: id2, cp: 25, depth: 1 })
    })
    expect(store.getState().streamingEval).toEqual({ moveIndex: 0, cp: 25 })

  })

  it('does not update streamingEval for already-resolved move indices', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })
    const requestId = postMessageMock.mock.calls[0][0].id

    // Resolve the analysis first (buffered worker released by cache miss)
    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        classification: 'best',
      })
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })
    expect(store.getState().streamingEval).toBeNull()

    // Late streaming message for the same request — should be ignored
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 99, depth: 15 })
    })
    expect(store.getState().streamingEval).toBeNull()
  })

  it('ignores streaming messages with unknown request IDs', () => {
    renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: 'unknown-id', cp: 50, depth: 5 })
    })

    expect(store.getState().streamingEval).toBeNull()
  })

  it('clears streamingEval when analysis completes', () => {
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0)
    })
    const requestId = postMessageMock.mock.calls[0][0].id

    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: requestId, cp: 42, depth: 5 })
    })
    expect(store.getState().streamingEval).not.toBeNull()

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 50,
        delta: 0,
        classification: 'best',
      })
    })
    expect(store.getState().streamingEval).toBeNull()
  })

  it('handles analysis with null eval values', () => {
    renderHook(() => useMoveAnalysis(store))

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
        classification: null,
      })
    })

    expect(store.getState().lastAnalysis).toEqual({
      id: 'req-3',
      move: 'e2e4',
      bestMove: '(none)',
      bestEval: null,
      playedEval: null,
      currentPositionEval: null,
      moveIndex: null,
      delta: null,
      classification: null,
      blunder: false,
      recordable: false,
    })
  })

  it('threads best_line_uci from a cache hit into the resolved analysis bestLine', async () => {
    vi.useFakeTimers()

    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0, 20)
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    act(() => {
      resolveLookup(new Map([
        ['fen::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5', 'g1f3'],
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(store.getState().analysisMap.get(0)).toEqual(
      expect.objectContaining({
        bestMove: 'e2e4',
        bestLine: ['e2e4', 'e7e5', 'g1f3'],
      }),
    )
  })

  it('flips a cached white-relative mate count to player-relative for black', async () => {
    vi.useFakeTimers()

    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    // Black move at ply 1.
    act(() => {
      result.current.analyzeMove('fen', 'd7d5', 'black', 1, 20)
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    await act(async () => {
      resolveLookup(new Map([
        ['fen::d7d5', {
          move_san: 'd5',
          best_move_uci: 'd7d5',
          best_move_san: 'd5',
          best_line_uci: ['d7d5', 'g1f3'],
          // White-relative mate -2 (white mates) → black player-relative +2.
          played_eval: -9980,
          played_eval_mate: -2,
          best_eval: -9980,
          eval_delta: 0,
          classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
    })

    await act(async () => {})

    await vi.waitFor(() => {
      expect(store.getState().analysisMap.get(1)).toEqual(
        expect.objectContaining({
          playedEvalMate: 2,
          currentPositionEvalMate: 2,
        }),
      )
    })
  })

  it('ignores cache hits without a usable best line and waits for the worker result', async () => {
    vi.useFakeTimers()

    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => {
      simulateMessage({ type: 'ready' })
    })

    act(() => {
      result.current.analyzeMove('fen', 'e2e4', 'white', 0, 20)
    })

    const requestId = postMessageMock.mock.calls[0][0].id

    await act(async () => {
      await vi.advanceTimersByTimeAsync(200)
    })

    act(() => {
      resolveLookup(new Map([
        ['fen::e2e4', {
          move_san: 'e4',
          best_move_uci: 'e2e4',
          best_move_san: 'e4',
          played_eval: 25,
          best_eval: 25,
          eval_delta: 0,
          classification: 'best',
        }],
      ]))
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(store.getState().analysisMap.size).toBe(0)
    expect(
      postMessageMock.mock.calls.some(
        ([message]) => message.type === 'cancel-analysis' && message.id === requestId,
      ),
    ).toBe(false)

    act(() => {
      simulateMessage({
        type: 'analysis',
        id: requestId,
        move: 'e2e4',
        bestMove: 'e2e4',
        bestLine: ['e2e4', 'e7e5'],
        bestEval: 25,
        playedEval: 25,
        delta: 0,
        classification: 'best',
      })
    })

    expect(store.getState().analysisMap.get(0)).toEqual(
      expect.objectContaining({
        id: requestId,
        move: 'e2e4',
        delta: 0,
        classification: 'best',
      }),
    )
  })

  // ── code-review regressions (Findings 1-3) ───────────────────────

  it('does not let a superseded request stream update the replacement index (Finding 2)', () => {
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })

    act(() => { result.current.analyzeMove('fen-old', 'e2e4', 'white', 0) })
    const staleId = postMessageMock.mock.calls[0][0].id

    // Replay the same index — supersedes the first request.
    act(() => { result.current.analyzeMove('fen-new', 'd2d4', 'white', 0) })

    // A stale streaming message for the superseded request must be ignored.
    act(() => {
      simulateMessage({ type: 'analysis-streaming', id: staleId, cp: 99, depth: 9 })
    })
    expect(store.getState().streamingEval).toBeNull()
  })

  it('a stale result does not clear the spinner of a live newer request (Finding 2)', () => {
    vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })

    act(() => { result.current.analyzeMove('fen-old', 'e2e4', 'white', 0) })
    const staleId = postMessageMock.mock.calls[0][0].id

    act(() => { result.current.analyzeMove('fen-new', 'd2d4', 'white', 0) })
    const liveId = postMessageMock.mock.calls
      .filter((c) => c[0].type === 'analyze-move')
      .at(-1)![0].id

    // The live request owns the spinner.
    act(() => { simulateMessage({ type: 'analysis-started', id: liveId, move: 'd2d4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    // A stale result for the superseded request must not clear it.
    act(() => {
      simulateMessage({
        type: 'analysis', id: staleId, move: 'e2e4', bestMove: 'e2e4',
        bestEval: 10, playedEval: 10, delta: 0, classification: 'best',
      })
    })
    expect(store.getState().isAnalyzing).toBe(true)
    expect(store.getState().analyzingMove).toBe('d2d4')
  })

  it('a late ready cannot reopen analysis after a fatal error (Finding 3)', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    // Fatal (unscoped) error.
    act(() => { simulateMessage({ type: 'error', error: 'fatal' }) })
    expect(store.getState().status).toBe('error')
    expect(terminateMock).toHaveBeenCalled()

    // A late ready must NOT flip status back and reopen the ad-hoc path.
    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => {
      simulateMessage({
        type: 'analysis', id, move: 'e2e4', bestMove: 'e2e4',
        bestEval: 10, playedEval: 10, delta: 0, classification: 'best',
      })
    })
    expect(store.getState().status).toBe('error')
    expect(store.getState().lastAnalysis).toBeNull()
  })

  it('clears the spinner when the worker stalls past the total deadline (Finding 1)', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    // Cache misses (released, no buffered worker), worker never emits.
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    expect(store.getState().isAnalyzing).toBe(false)
    expect(store.getState().analyzingMove).toBeNull()
  })

  it('cancels the stalled worker request when the indexed deadline elapses', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })

    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })

    // The deadline must tell the worker to abandon the request so its serial
    // queue cannot stay blocked behind a missing readyok.
    expect(
      postMessageMock.mock.calls.some(
        ([message]) => message.type === 'cancel-analysis' && message.id === id,
      ),
    ).toBe(true)
  })

  it('gives a variation (what-if) request a deadline that cancels the stalled worker request', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => {
      // moveIndex undefined -> variation path (ply + fen tracked separately).
      result.current.analyzeMove('fen-v', 'e2e4', 'white', undefined, undefined, 4, 'fen-v')
    })
    const id = postMessageMock.mock.calls[0][0].id
    expect(id).toBeTruthy()

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })

    // Before the deadline: no cancel yet.
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    expect(
      postMessageMock.mock.calls.some(([m]) => m.type === 'cancel-analysis'),
    ).toBe(false)

    // After the deadline: the worker request is canceled and streaming cleared.
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(
      postMessageMock.mock.calls.some(
        ([m]) => m.type === 'cancel-analysis' && m.id === id,
      ),
    ).toBe(true)
    expect(store.getState().isAnalyzing).toBe(false)
  })

  it('cancels the still-running worker request when a trusted cache hit wins', async () => {
    vi.useFakeTimers()
    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))
    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0, 20) })
    const id = postMessageMock.mock.calls[0][0].id
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })

    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    act(() => {
      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'e2e4', best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25, best_eval: 25, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(store.getState().analysisMap.get(0)?.bestMove).toBe('e2e4')
    expect(
      postMessageMock.mock.calls.some(
        ([m]) => m.type === 'cancel-analysis' && m.id === id,
      ),
    ).toBe(true)
  })

  // Phase 5 grain split: the cache row resolves the move only when ALL of
  // isTrustedPositionHit, isTrustedMoveHit, and hasCpEvalLoss hold.
  it.each([
    ['position trusted, move untrusted (split case a)', { move_trusted: false }],
    ['move trusted, position untrusted (split case b)', { position_trusted: false }],
    [
      'move-trusted mate-only row with no CP delta (split case c)',
      { classification: 'blunder', played_eval: null, played_eval_mate: -2, eval_delta: null },
    ],
  ])('falls back to the worker when %s', async (_label, overrides) => {
    vi.useFakeTimers()
    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))
    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0, 20) })
    const id = postMessageMock.mock.calls[0][0].id

    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    act(() => {
      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'e2e4', best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25, played_eval_mate: null,
          best_eval: 25, best_eval_mate: null, eval_delta: 0, classification: 'best',
          position_trusted: true, move_trusted: true,
          ...overrides,
        }],
      ]))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    // The cache row did not resolve the move; the worker still owns it.
    expect(store.getState().analysisMap.has(0)).toBe(false)
    // The worker request is NOT cancelled (it must finish the analysis).
    expect(
      postMessageMock.mock.calls.some(
        ([m]) => m.type === 'cancel-analysis' && m.id === id,
      ),
    ).toBe(false)
  })

  it('cancels the superseded worker request when the same index is re-analyzed', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))
    act(() => { simulateMessage({ type: 'ready' }) })

    act(() => { result.current.analyzeMove('fen-old', 'e2e4', 'white', 0) })
    const staleId = postMessageMock.mock.calls[0][0].id

    act(() => { result.current.analyzeMove('fen-new', 'd2d4', 'white', 0) })

    // The superseded request is canceled at the worker, not just locally dropped.
    expect(
      postMessageMock.mock.calls.some(
        ([m]) => m.type === 'cancel-analysis' && m.id === staleId,
      ),
    ).toBe(true)
  })

  it('cancels in-flight worker requests on clearAnalysis', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))
    act(() => { simulateMessage({ type: 'ready' }) })

    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const indexedId = postMessageMock.mock.calls[0][0].id
    act(() => {
      result.current.analyzeMove('fen-v', 'g1f3', 'white', undefined, undefined, 4, 'fen-v')
    })
    const variationId = postMessageMock.mock.calls.at(-1)![0].id

    act(() => { result.current.clearAnalysis() })

    const canceled = postMessageMock.mock.calls
      .filter(([m]) => m.type === 'cancel-analysis')
      .map(([m]) => m.id)
    expect(canceled).toContain(indexedId)
    expect(canceled).toContain(variationId)
  })

  it('clears the spinner on a scoped worker error (Finding 1)', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    // Scoped worker error while cache pending — spinner must clear.
    act(() => { simulateMessage({ type: 'error', id, error: 'boom' }) })
    expect(store.getState().isAnalyzing).toBe(false)
    expect(store.getState().analyzingMove).toBeNull()
    // Scoped error must NOT set the global error status.
    expect(store.getState().status).not.toBe('error')
  })

  it('a trusted cache hit clears the spinner even if the worker is still running (Finding 1)', async () => {
    vi.useFakeTimers()
    let resolveLookup!: (value: Map<string, unknown>) => void
    lookupAnalysisCacheMock.mockReturnValueOnce(
      new Promise((resolve) => { resolveLookup = resolve }),
    )

    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0, 20) })
    const id = postMessageMock.mock.calls[0][0].id

    // Worker started — spinner on. The worker never finishes (stalls).
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    await act(async () => { await vi.advanceTimersByTimeAsync(200) })
    act(() => {
      resolveLookup(new Map([
        ['fen-0::e2e4', {
          move_san: 'e4', best_move_uci: 'e2e4', best_move_san: 'e4',
          best_line_uci: ['e2e4', 'e7e5'],
          played_eval: 25, best_eval: 25, eval_delta: 0, classification: 'best',
          position_trusted: true,
          move_trusted: true,
        }],
      ]))
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })

    expect(store.getState().analysisMap.get(0)?.bestMove).toBe('e2e4')
    expect(store.getState().isAnalyzing).toBe(false)
    expect(store.getState().analyzingMove).toBeNull()
  })

  it('clears the spinner on a worker ErrorEvent (Finding 2)', () => {
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id
    act(() => { simulateMessage({ type: 'analysis-started', id, move: 'e2e4' }) })
    expect(store.getState().isAnalyzing).toBe(true)

    act(() => { simulateError('worker crashed') })
    expect(store.getState().isAnalyzing).toBe(false)
    expect(store.getState().analyzingMove).toBeNull()
  })

  it('clearAnalysis drops a late indexed result instead of setting lastAnalysis (Finding 1)', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useMoveAnalysis(store))

    act(() => { simulateMessage({ type: 'ready' }) })
    act(() => { result.current.analyzeMove('fen-0', 'e2e4', 'white', 0) })
    const id = postMessageMock.mock.calls[0][0].id

    act(() => { result.current.clearAnalysis() })

    // Late worker result for the discarded indexed request → dropped.
    act(() => {
      simulateMessage({
        type: 'analysis', id, move: 'e2e4', bestMove: 'e2e4',
        bestEval: 10, playedEval: 10, delta: 0, classification: 'best',
      })
    })
    await act(async () => { await vi.advanceTimersByTimeAsync(200) })

    expect(store.getState().lastAnalysis).toBeNull()
    expect(store.getState().analysisMap.has(0)).toBe(false)
  })

  // ── best-move promotion from trusted position truth (g-49e2) ─────────
  // Same root cause as g-move-best-icon, but the post-game AnalysisBoard path
  // (this hook) had no exact-best truth channel. A move-untrusted cache row whose
  // played move equals the trusted best_move_uci falls back to the worker, which
  // under-rates it 'excellent'; the trusted position grain must still promote it
  // to the best-move star.
  describe('best-move promotion from trusted position truth (g-49e2)', () => {
    it('promotes a worker-fallback result to best when the played move equals the trusted best (the c4 case)', async () => {
      vi.useFakeTimers()

      let resolveLookup!: (value: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const { result } = renderHook(() => useMoveAnalysis(store))
      act(() => { simulateMessage({ type: 'ready' }) })
      act(() => { result.current.analyzeMove('fen-0', 'c2c4', 'white', 0, 20) })
      const requestId = postMessageMock.mock.calls[0][0].id

      await act(async () => { await vi.advanceTimersByTimeAsync(200) })

      // position_trusted but move_untrusted -> the published gate releases to the
      // worker, but the exact-best truth (best == played == c2c4) is recorded.
      act(() => {
        resolveLookup(new Map([
          ['fen-0::c2c4', {
            move_san: 'c4', best_move_uci: 'c2c4', best_move_san: 'c4',
            best_line_uci: ['c2c4', 'g8f6'], best_eval: 35,
            played_eval: 42, played_eval_mate: null, eval_delta: 0,
            classification: 'excellent',
            position_trusted: true, move_trusted: false, position_eval_loss_cp: null,
          }],
        ]))
      })
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })

      // The worker under-rates c4 as 'excellent' with a different best move.
      act(() => {
        simulateMessage({
          type: 'analysis', id: requestId, move: 'c2c4', bestMove: 'g1f3',
          bestLine: ['g1f3', 'd7d5'], bestEval: 35, playedEval: 42,
          playedEvalMate: null, delta: 7, classification: 'excellent',
        })
      })

      const resolved = store.getState().analysisMap.get(0)
      expect(resolved?.classification).toBe('best')
      expect(resolved?.bestMove).toBe('c2c4')
      expect(resolved?.bestLine).toEqual(['c2c4'])
      expect(resolved?.delta).toBe(0)
      expect(resolved?.blunder).toBe(false)
      // Eval magnitude is preserved from the worker/move grain.
      expect(resolved?.playedEval).toBe(42)
    })

    it('leaves a non-best played move classification untouched (the Bf4 case)', async () => {
      vi.useFakeTimers()

      let resolveLookup!: (value: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const { result } = renderHook(() => useMoveAnalysis(store))
      act(() => { simulateMessage({ type: 'ready' }) })
      act(() => { result.current.analyzeMove('fen-0', 'c1f4', 'white', 0, 20) })
      const requestId = postMessageMock.mock.calls[0][0].id

      await act(async () => { await vi.advanceTimersByTimeAsync(200) })

      // Trusted best is c2c4 but the played move is c1f4 (Bf4) — not the best,
      // so the excellent icon must stay even though truth is recorded.
      act(() => {
        resolveLookup(new Map([
          ['fen-0::c1f4', {
            move_san: 'Bf4', best_move_uci: 'c2c4', best_move_san: 'c4',
            best_line_uci: ['c2c4', 'g8f6'], best_eval: 35,
            played_eval: 44, played_eval_mate: null, eval_delta: 0,
            classification: 'excellent',
            position_trusted: true, move_trusted: false, position_eval_loss_cp: null,
          }],
        ]))
      })
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })

      act(() => {
        simulateMessage({
          type: 'analysis', id: requestId, move: 'c1f4', bestMove: 'c2c4',
          bestLine: ['c2c4', 'g8f6'], bestEval: 35, playedEval: 44,
          playedEvalMate: null, delta: 9, classification: 'excellent',
        })
      })

      const resolved = store.getState().analysisMap.get(0)
      expect(resolved?.classification).toBe('excellent')
      expect(resolved?.bestMove).toBe('c2c4')
    })

    it("demotes a fallback that wrongly graded a non-best move 'best' (the d5 vs Nf6 case, g-jfdj)", async () => {
      vi.useFakeTimers()

      let resolveLookup!: (value: Map<string, unknown>) => void
      lookupAnalysisCacheMock.mockReturnValueOnce(
        new Promise((resolve) => { resolveLookup = resolve }),
      )

      const { result } = renderHook(() => useMoveAnalysis(store))
      act(() => { simulateMessage({ type: 'ready' }) })
      act(() => { result.current.analyzeMove('fen-0', 'd7d5', 'black', 0, 20) })
      const requestId = postMessageMock.mock.calls[0][0].id

      await act(async () => { await vi.advanceTimersByTimeAsync(200) })

      // Trusted best is g8f6 (Nf6); the played move d7d5 is NOT the best move,
      // so the exact-best truth records g8f6 as the trusted best.
      act(() => {
        resolveLookup(new Map([
          ['fen-0::d7d5', {
            move_san: 'd5', best_move_uci: 'g8f6', best_move_san: 'Nf6',
            best_line_uci: ['g8f6'], best_eval: 35,
            played_eval: 20, played_eval_mate: null, eval_delta: 0,
            classification: 'best',
            position_trusted: true, move_trusted: false, position_eval_loss_cp: null,
          }],
        ]))
      })
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })

      // A browser-game-v1 fallback wrongly classifies d7d5 as 'best'.
      act(() => {
        simulateMessage({
          type: 'analysis', id: requestId, move: 'd7d5', bestMove: 'd7d5',
          bestLine: ['d7d5'], bestEval: 35, playedEval: 20,
          playedEvalMate: null, delta: 0, classification: 'best',
        })
      })

      const resolved = store.getState().analysisMap.get(0)
      expect(resolved?.classification).toBe('excellent')
      expect(resolved?.bestMove).toBe('g8f6')
      expect(resolved?.bestLine).toEqual(['g8f6'])
    })
  })
})
