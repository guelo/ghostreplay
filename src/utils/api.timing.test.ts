import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock the analytics singleton so `reportApiRequest` -> `captureEvent` is an
// observable spy and never touches the real PostHog client / network.
vi.mock('../analytics/posthog', () => ({
  captureEvent: vi.fn(),
  initAnalytics: vi.fn(),
  identifyUser: vi.fn(),
  resetAnalytics: vi.fn(),
  isAnalyticsEnabled: vi.fn(() => false),
  posthog: { capture: vi.fn(), identify: vi.fn(), reset: vi.fn() },
}))

import { captureEvent } from '../analytics/posthog'
import { normalizeApiPath, startGame, getNextOpponentMove } from './api'

const captureMock = vi.mocked(captureEvent)

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

// Minimal localStorage stub (jsdom's may not be a full Storage here).
let mockStore: Record<string, string> = {}
Object.defineProperty(window, 'localStorage', {
  value: {
    getItem: (key: string) => mockStore[key] ?? null,
    setItem: (key: string, value: string) => {
      mockStore[key] = value
    },
    removeItem: (key: string) => {
      delete mockStore[key]
    },
    clear: () => {
      mockStore = {}
    },
    length: 0,
    key: () => null,
  },
  writable: true,
})

// Mimics fetch's case-insensitive Headers.get, echoing a server request id.
const headersWith = (requestId: string) => ({
  get: (name: string) =>
    name.toLowerCase() === 'x-request-id' ? requestId : null,
})

const okResponse = (data: Record<string, unknown>, status = 200) =>
  fetchMock.mockResolvedValueOnce({
    ok: true,
    status,
    statusText: 'OK',
    headers: headersWith('req-ok'),
    json: () => Promise.resolve(data),
  })

const errorResponse = (
  data: Record<string, unknown>,
  status: number,
  statusText = 'Error',
) =>
  fetchMock.mockResolvedValueOnce({
    ok: false,
    status,
    statusText,
    headers: headersWith('req-err'),
    json: () => Promise.resolve(data),
  })

/** The single `api_request_client` event captured during this call. */
const onlyClientEvent = () => {
  const calls = captureMock.mock.calls.filter(
    ([event]) => event === 'api_request_client',
  )
  expect(calls).toHaveLength(1)
  return calls[0][1] as Record<string, unknown>
}

describe('normalizeApiPath', () => {
  const uuid = '7f3e4d2a-1b2c-4d5e-8f90-abcdef123456'

  it('maps a dynamic drill route to the exact backend template', () => {
    expect(normalizeApiPath(`http://localhost:8000/api/drills/${uuid}/fail`)).toBe(
      '/api/drills/{session_id}/fail',
    )
  })

  it('maps a bare drill-session route to the backend template', () => {
    expect(normalizeApiPath(`/api/drills/${uuid}`)).toBe('/api/drills/{session_id}')
  })

  it('maps the session moves route to the backend template', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/moves`)).toBe(
      '/api/session/{session_id}/moves',
    )
  })

  it('maps the analysis-evidence route to the exact template (not {id} fallback)', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/analysis-evidence`)).toBe(
      '/api/session/{session_id}/analysis-evidence',
    )
  })

  it('still maps the sibling session analysis route to its own template', () => {
    expect(normalizeApiPath(`/api/session/${uuid}/analysis`)).toBe(
      '/api/session/{session_id}/analysis',
    )
  })

  it('maps the opening score-delta poll route to the backend template', () => {
    expect(normalizeApiPath(`/api/openings/score-delta/${uuid}`)).toBe(
      '/api/openings/score-delta/{session_id}',
    )
  })

  it('does not mislabel a static openings sibling like /api/openings/tree', () => {
    expect(normalizeApiPath('/api/openings/tree/status')).toBe(
      '/api/openings/tree/status',
    )
  })

  it('does not mislabel a static sibling like /api/drills/start', () => {
    expect(normalizeApiPath('/api/drills/start')).toBe('/api/drills/start')
  })

  it('falls back to {id} for unknown UUID/numeric segments', () => {
    expect(normalizeApiPath('/api/blunder/12345')).toBe('/api/blunder/{id}')
    expect(normalizeApiPath(`/api/widgets/${uuid}/edit`)).toBe('/api/widgets/{id}/edit')
  })

  it('strips the query string', () => {
    expect(normalizeApiPath('/api/stats/rating-history?range=7d')).toBe(
      '/api/stats/rating-history',
    )
  })

  it('strips the origin/base URL down to the pathname', () => {
    expect(
      normalizeApiPath('https://api.example.com/api/openings/tree?player_color=white'),
    ).toBe('/api/openings/tree')
  })

  it('leaves a template-free relative path unchanged', () => {
    expect(normalizeApiPath('/api/game/start')).toBe('/api/game/start')
  })
})

describe('requestJson client timing capture', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    captureMock.mockClear()
    mockStore = {}
  })

  it('emits exactly one success event per logical request', async () => {
    okResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame()

    const props = onlyClientEvent()
    expect(props).toMatchObject({
      route: '/api/game/start',
      method: 'POST',
      status: 200,
      ok: true,
      attempts: 1,
      error_kind: null,
      request_id: 'req-ok',
    })
    expect(props.duration_ms).toEqual(expect.any(Number))
  })

  it('emits one http-error event on a non-retryable failure', async () => {
    errorResponse({ detail: 'boom' }, 500, 'Internal Server Error')

    await expect(startGame()).rejects.toThrow()

    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/start',
      status: 500,
      ok: false,
      attempts: 1,
      error_kind: 'http',
      request_id: 'req-err',
    })
  })

  it('emits a single final event (not per attempt) across retries', async () => {
    errorResponse(
      { error: { code: 'internal_error', message: 'down', retryable: true } },
      503,
      'Service Unavailable',
    )
    okResponse({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'ghost_path',
    })

    await getNextOpponentMove('sess-1', 'fen')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/next-opponent-move',
      status: 200,
      ok: true,
      attempts: 2,
      error_kind: null,
    })
  })

  it('reports a parse failure as error_kind "parse", not a success', async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: headersWith('req-parse'),
      json: () => Promise.reject(new SyntaxError('Unexpected token')),
    })

    await expect(startGame()).rejects.toThrow(SyntaxError)

    expect(onlyClientEvent()).toMatchObject({
      status: 200,
      ok: false,
      attempts: 1,
      error_kind: 'parse',
      request_id: 'req-parse',
    })
  })

  it('reports a network failure with status 0, error_kind "network", null request_id', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))

    await expect(startGame()).rejects.toThrow(TypeError)

    expect(onlyClientEvent()).toMatchObject({
      route: '/api/game/start',
      status: 0,
      ok: false,
      attempts: 1,
      error_kind: 'network',
      request_id: null,
    })
  })

  it('classifies an aborted-by-deadline failure as error_kind "timeout"', async () => {
    const timeout = new DOMException('The operation timed out', 'TimeoutError')
    fetchMock.mockRejectedValueOnce(timeout)

    await expect(startGame()).rejects.toThrow()

    expect(onlyClientEvent()).toMatchObject({
      status: 0,
      ok: false,
      error_kind: 'timeout',
    })
  })

  it('does not emit for a deliberately aborted (superseded) request', async () => {
    fetchMock.mockRejectedValueOnce(
      new DOMException('The user aborted a request.', 'AbortError'),
    )

    await expect(startGame()).rejects.toThrow()

    const clientEvents = captureMock.mock.calls.filter(
      ([event]) => event === 'api_request_client',
    )
    expect(clientEvents).toHaveLength(0)
  })
})
