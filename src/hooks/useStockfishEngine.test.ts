import { describe, expect, it, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { WorkerResponse } from '../workers/stockfishMessages'

// ---------------------------------------------------------------------------
// Minimal Worker mock — captures postMessage calls and lets us push messages
// back into the hook's message handler.
// ---------------------------------------------------------------------------

let messageHandler: ((e: MessageEvent<WorkerResponse>) => void) | null = null

class FakeWorker {
  postMessage = vi.fn()
  terminate = vi.fn()

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
  })

  it('starts in booting when SharedArrayBuffer is unavailable', async () => {
    vi.stubGlobal('SharedArrayBuffer', undefined)
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    expect(result.current.status).toBe('booting')
    expect(result.current.error).toBeNull()
  })

  it('does not overwrite slot 0 with a pv-less currmove info line', async () => {
    const useStockfishEngine = await loadHook()
    const { result } = renderHook(() => useStockfishEngine())

    act(() => emit({ type: 'ready' }))

    const requestId = 'req-1'

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

    const requestId = 'req-2'
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
})
