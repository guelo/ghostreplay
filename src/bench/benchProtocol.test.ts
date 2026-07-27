import { describe, expect, it, vi } from 'vitest'
import {
  BENCH_HANDSHAKE_TIMEOUT_MS,
  armUnavailableReason,
  buildAnalyzeMessage,
  enableBenchMode,
} from './benchProtocol'
import type { BenchWorkerLike } from './benchProtocol'
import type { AnalyzeMoveMessage } from '../workers/analysisMessages'

const request: AnalyzeMoveMessage = {
  type: 'analyze-move',
  id: 'req-1',
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  move: 'e2e4',
  playerColor: 'white',
  depth: 17,
}

/** A worker that optionally answers `bench-init`, like a candidate-aware build. */
const fakeWorker = (arms: string[] | null) => {
  const listeners = new Set<(event: MessageEvent) => void>()
  const posted: unknown[] = []
  const worker: BenchWorkerLike = {
    postMessage: (message) => {
      posted.push(message)
      if (arms === null) return
      for (const listener of listeners) {
        listener({ data: { type: 'bench-ready', arms } } as MessageEvent)
      }
    },
    addEventListener: (_type, listener) => {
      listeners.add(listener)
    },
    removeEventListener: (_type, listener) => {
      listeners.delete(listener)
    },
  }
  return { worker, posted, listeners }
}

describe('per-message arm selector (C7 key 2)', () => {
  it('leaves the default arm byte-identical to a production message', () => {
    const message = buildAnalyzeMessage(request, 'current')

    expect(message).toBe(request)
    expect(Object.keys(message)).toEqual([
      'type',
      'id',
      'fen',
      'move',
      'playerColor',
      'depth',
    ])
    expect('arm' in message).toBe(false)
  })

  it('stamps the selector only for a candidate arm', () => {
    const message = buildAnalyzeMessage(request, 'variantA')

    expect(message.arm).toBe('variantA')
    expect(message.id).toBe('req-1')
  })
})

describe('bench-mode handshake (C7 key 1)', () => {
  it('reports the arms a bench-aware worker advertises', async () => {
    const { worker, posted } = fakeWorker(['variantA'])

    await expect(enableBenchMode(worker)).resolves.toEqual(['variantA'])
    expect(posted).toEqual([{ type: 'bench-init', bench: true }])
  })

  it('resolves to no arms when the worker never answers', async () => {
    vi.useFakeTimers()
    try {
      const { worker } = fakeWorker(null)
      const pending = enableBenchMode(worker)
      await vi.advanceTimersByTimeAsync(BENCH_HANDSHAKE_TIMEOUT_MS)

      // The expected outcome against any build without the worker-side C7 half.
      await expect(pending).resolves.toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })

  it('detaches its listener once settled', async () => {
    const { worker, listeners } = fakeWorker(['variantB'])

    await enableBenchMode(worker)

    expect(listeners.size).toBe(0)
  })
})

describe('armUnavailableReason', () => {
  it('always allows the default arm', () => {
    expect(armUnavailableReason('current', [])).toBeNull()
  })

  it('refuses a candidate arm the worker did not advertise', () => {
    // The whole point of the handshake: a row must never be labelled with an arm
    // the worker never dispatched.
    expect(armUnavailableReason('variantA', [])).toMatch(/not available in this worker build/)
    expect(armUnavailableReason('variantA', ['variantB'])).toMatch(/bench mode advertised: variantB/)
  })

  it('allows a candidate arm the worker advertised', () => {
    expect(armUnavailableReason('variantA', ['variantA', 'variantB'])).toBeNull()
  })
})
