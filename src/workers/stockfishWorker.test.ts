import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { WorkerRequest, WorkerResponse } from './stockfishMessages'

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

  beforeEach(() => {
    vi.resetModules()

    engineWorkerPostMessageMock = vi.fn()
    terminateMock = vi.fn()
    postMessageMock = vi.fn()
    engineMessageHandler = undefined
    constructedUrl = undefined

    vi.stubGlobal('self', {
      addEventListener: vi.fn(
        (_type: string, _handler: (event: MessageEvent<WorkerRequest>) => void) => {},
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
})
