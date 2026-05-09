import { describe, expect, it, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { WorkerResponse } from '../workers/stockfishMessages'

// ---------------------------------------------------------------------------
// Minimal Worker mock — captures postMessage calls and lets us push messages
// back into the hook's message handler.
// ---------------------------------------------------------------------------

let messageHandler: ((e: MessageEvent<WorkerResponse>) => void) | null = null
const workerInstances: FakeWorker[] = []

class FakeWorker {
  postMessage = vi.fn()
  terminate = vi.fn()

  constructor() {
    workerInstances.push(this)
  }

  addEventListener(type: string, handler: (e: MessageEvent) => void) {
    if (type === 'message') messageHandler = handler
  }

  removeEventListener() {
    messageHandler = null
  }
}

const loadHook = async () => {
  const module = await import('./useStockfishEngine')
  return module.useStockfishEngine
}

function emit(response: WorkerResponse) {
  if (!messageHandler) throw new Error('No worker message handler registered')
  messageHandler(new MessageEvent('message', { data: response }))
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('useStockfishEngine', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.unstubAllGlobals()
    vi.stubGlobal('Worker', FakeWorker)
    vi.stubGlobal('SharedArrayBuffer', ArrayBuffer)
    messageHandler = null
    workerInstances.length = 0
  })

  it('starts in booting when SharedArrayBuffer is unavailable', async () => {
    vi.stubGlobal('SharedArrayBuffer', undefined)
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    expect(result.current.status).toBe('booting')
    expect(result.current.error).toBeNull()
  })

  it('does not create a worker while disabled', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine({ enabled: false }))

    expect(workerInstances).toHaveLength(0)
    await expect(result.current.evaluatePosition('fen-1')).rejects.toThrow(
      'Stockfish engine disabled',
    )
  })

  it('terminates the worker when disabled after being enabled', async () => {
    const useStockfishEngine = await loadHook()
    const { rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useStockfishEngine({ enabled }),
      { initialProps: { enabled: true } },
    )

    const worker = workerInstances[0]
    expect(worker).toBeDefined()

    rerender({ enabled: false })

    expect(worker.terminate).toHaveBeenCalled()
  })

  it('does not overwrite slot 0 with a pv-less currmove info line', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    await act(async () => {
      void result.current.evaluatePosition('startpos', { depth: 21 }).catch(() => {})
    })
    const worker = workerInstances[0]
    const requestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string

    act(() => emit({ type: 'thinking', id: requestId, fen: 'startpos' }))
    expect(result.current.info).toEqual([])

    act(() => {
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 10, multipv: 1, pv: ['e2e4'], score: { type: 'cp', value: 30 } },
        raw: 'info depth 10 multipv 1 score cp 30 pv e2e4',
      })
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 10, multipv: 2, pv: ['d2d4'], score: { type: 'cp', value: 20 } },
        raw: 'info depth 10 multipv 2 score cp 20 pv d2d4',
      })
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 10, multipv: 3, pv: ['c2c4'], score: { type: 'cp', value: 10 } },
        raw: 'info depth 10 multipv 3 score cp 10 pv c2c4',
      })
    })

    expect(result.current.info).toHaveLength(3)
    expect(result.current.info[0]?.pv).toEqual(['e2e4'])

    act(() => {
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 11 },
        raw: 'info depth 11 currmove e2e4 currmovenumber 1',
      })
    })

    expect(result.current.info[0]?.pv).toEqual(['e2e4'])
    expect(result.current.info).toHaveLength(3)
  })

  it('still updates slot 0 for a real multipv 1 line with pv', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    await act(async () => {
      void result.current.evaluatePosition('startpos', { depth: 21 }).catch(() => {})
    })
    const worker = workerInstances[0]
    const requestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string
    act(() => emit({ type: 'thinking', id: requestId, fen: 'startpos' }))

    act(() => {
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 10, multipv: 1, pv: ['e2e4'], score: { type: 'cp', value: 30 } },
        raw: 'info depth 10 multipv 1 score cp 30 pv e2e4',
      })
    })
    expect(result.current.info[0]?.pv).toEqual(['e2e4'])

    act(() => {
      emit({
        type: 'info',
        id: requestId,
        info: { depth: 11, multipv: 1, pv: ['d2d4', 'd7d5'], score: { type: 'cp', value: 35 } },
        raw: 'info depth 11 multipv 1 score cp 35 pv d2d4 d7d5',
      })
    })
    expect(result.current.info[0]?.pv).toEqual(['d2d4', 'd7d5'])
    expect(result.current.info[0]?.depth).toBe(11)
  })

  it('forwards worker log messages to console', async () => {
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {})
    const useStockfishEngine = await loadHook()
    renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'log', line: '[stockfishWorker <-] info depth 10 pv e2e4' }))

    expect(logSpy).toHaveBeenCalledWith(
      '[StockfishEngine] [stockfishWorker <-] info depth 10 pv e2e4',
    )

    logSpy.mockRestore()
  })

  it('does not reuse a single-pv cache entry for a later multipv request', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    const worker = workerInstances[0]
    expect(worker).toBeDefined()

    let firstPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      firstPromise = result.current.evaluatePosition('fen-1', { depth: 21 })
    })

    expect(worker.postMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({
        type: 'evaluate-position',
        fen: 'fen-1',
        depth: 21,
        multipv: undefined,
      }),
    )
    const firstRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string

    act(() => emit({ type: 'thinking', id: firstRequestId, fen: 'fen-1' }))
    act(() => {
      emit({
        type: 'info',
        id: firstRequestId,
        info: { depth: 21, multipv: 1, pv: ['e2e4'], score: { type: 'cp', value: 30 } },
        raw: 'info depth 21 multipv 1 score cp 30 pv e2e4',
      })
    })
    act(() => emit({ type: 'bestmove', id: firstRequestId, move: 'e2e4', raw: 'bestmove e2e4' }))
    await act(async () => {
      await firstPromise
    })

    worker.postMessage.mockClear()

    let secondPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      secondPromise = result.current.evaluatePosition('fen-1', { depth: 21, multipv: 3 })
    })

    expect(worker.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'evaluate-position',
        fen: 'fen-1',
        depth: 21,
        multipv: 3,
      }),
    )
    const secondRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string

    act(() => emit({ type: 'thinking', id: secondRequestId, fen: 'fen-1' }))
    act(() => emit({ type: 'bestmove', id: secondRequestId, move: 'e2e4', raw: 'bestmove e2e4' }))
    await act(async () => {
      await secondPromise
    })
  })

  it('rejects a pending evaluation when a newer evaluation supersedes it', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    let firstPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      firstPromise = result.current.evaluatePosition('fen-1', { depth: 21 })
    })
    const firstRejection = firstPromise.catch((error: Error) => error)

    await act(async () => {
      void result.current.evaluatePosition('fen-2', { depth: 21 }).catch(() => {})
    })

    await expect(firstRejection).resolves.toMatchObject({
      message: 'Stockfish evaluation superseded',
    })
  })

  it('ignores stale worker messages after a newer evaluation supersedes an active request', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    const worker = workerInstances[0]
    expect(worker).toBeDefined()

    let firstPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      firstPromise = result.current.evaluatePosition('fen-1', { depth: 21 })
    })
    const firstRejection = firstPromise.catch((error: Error) => error)
    const firstRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string

    act(() => emit({ type: 'thinking', id: firstRequestId, fen: 'fen-1' }))
    act(() => {
      emit({
        type: 'info',
        id: firstRequestId,
        info: { depth: 12, multipv: 1, pv: ['e2e4'], score: { type: 'cp', value: 30 } },
        raw: 'info depth 12 multipv 1 score cp 30 pv e2e4',
      })
    })
    expect(result.current.info[0]?.pv).toEqual(['e2e4'])

    let secondPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      secondPromise = result.current.evaluatePosition('fen-2', { depth: 21 })
    })
    const secondRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string

    act(() => {
      emit({
        type: 'info',
        id: firstRequestId,
        info: { depth: 21, multipv: 1, pv: ['d2d4'], score: { type: 'cp', value: 80 } },
        raw: 'info depth 21 multipv 1 score cp 80 pv d2d4',
      })
      emit({ type: 'bestmove', id: firstRequestId, move: 'd2d4', raw: 'bestmove d2d4' })
    })
    expect(result.current.info).toEqual([])

    act(() => emit({ type: 'thinking', id: secondRequestId, fen: 'fen-2' }))
    act(() => {
      emit({
        type: 'info',
        id: secondRequestId,
        info: { depth: 21, multipv: 1, pv: ['g1f3'], score: { type: 'cp', value: 15 } },
        raw: 'info depth 21 multipv 1 score cp 15 pv g1f3',
      })
      emit({ type: 'bestmove', id: secondRequestId, move: 'g1f3', raw: 'bestmove g1f3' })
    })

    await expect(firstRejection).resolves.toMatchObject({
      message: 'Stockfish evaluation superseded',
    })
    await act(async () => {
      await secondPromise
    })

    worker.postMessage.mockClear()

    let cachedPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      cachedPromise = result.current.evaluatePosition('fen-2', { depth: 21 })
    })

    await expect(cachedPromise).resolves.toEqual({ move: 'g1f3', raw: '' })
    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'command', command: 'stop' })
    expect(worker.postMessage).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'evaluate-position', fen: 'fen-2' }),
    )
    expect(result.current.info[0]?.pv).toEqual(['g1f3'])
  })

  it('cached evaluations supersede active uncached work', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    const worker = workerInstances[0]
    expect(worker).toBeDefined()

    let cachedSeedPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      cachedSeedPromise = result.current.evaluatePosition('fen-cached', { depth: 21 })
    })
    const cachedRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string
    act(() => emit({ type: 'thinking', id: cachedRequestId, fen: 'fen-cached' }))
    act(() => {
      emit({
        type: 'info',
        id: cachedRequestId,
        info: { depth: 21, multipv: 1, pv: ['c2c4'], score: { type: 'cp', value: 25 } },
        raw: 'info depth 21 multipv 1 score cp 25 pv c2c4',
      })
      emit({ type: 'bestmove', id: cachedRequestId, move: 'c2c4', raw: 'bestmove c2c4' })
    })
    await act(async () => {
      await cachedSeedPromise
    })

    let activePromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      activePromise = result.current.evaluatePosition('fen-active', { depth: 21 })
    })
    const activeRejection = activePromise.catch((error: Error) => error)
    const activeRequestId = worker.postMessage.mock.calls.at(-1)?.[0]?.id as string
    act(() => emit({ type: 'thinking', id: activeRequestId, fen: 'fen-active' }))

    worker.postMessage.mockClear()

    let cachedPromise!: Promise<{ move: string; raw: string }>
    await act(async () => {
      cachedPromise = result.current.evaluatePosition('fen-cached', { depth: 21 })
    })

    await expect(cachedPromise).resolves.toEqual({ move: 'c2c4', raw: '' })
    await expect(activeRejection).resolves.toMatchObject({
      message: 'Stockfish evaluation superseded',
    })
    expect(worker.postMessage).toHaveBeenCalledWith({ type: 'command', command: 'stop' })

    act(() => {
      emit({
        type: 'info',
        id: activeRequestId,
        info: { depth: 21, multipv: 1, pv: ['e7e5'], score: { type: 'cp', value: 5 } },
        raw: 'info depth 21 multipv 1 score cp 5 pv e7e5',
      })
      emit({ type: 'bestmove', id: activeRequestId, move: 'e7e5', raw: 'bestmove e7e5' })
    })

    expect(result.current.info[0]?.pv).toEqual(['c2c4'])
    expect(result.current.isThinking).toBe(false)
  })
})
