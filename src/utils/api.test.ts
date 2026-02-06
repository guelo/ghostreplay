import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { startGame, endGame, recordBlunder, getGhostMove } from './api'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

// Mock localStorage (jsdom's built-in one may not be available)
let mockStore: Record<string, string> = {}
const localStorageMock = {
  getItem: (key: string) => mockStore[key] ?? null,
  setItem: (key: string, value: string) => { mockStore[key] = value },
  removeItem: (key: string) => { delete mockStore[key] },
  clear: () => { mockStore = {} },
  length: 0,
  key: () => null,
}
Object.defineProperty(window, 'localStorage', { value: localStorageMock, writable: true })

const mockResponse = (data: Record<string, unknown>, ok = true, statusText = 'OK') => {
  fetchMock.mockResolvedValueOnce({
    ok,
    statusText,
    json: () => Promise.resolve(data),
  })
}

describe('startGame', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  afterEach(() => {
    mockStore = {}
  })

  it('sends correct request body', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame(1500, 'white')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/game/start'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ engine_elo: 1500, player_color: 'white' }),
      }),
    )
  })

  it('uses default values', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500, player_color: 'white' })

    await startGame()

    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        body: JSON.stringify({ engine_elo: 1500, player_color: 'white' }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { session_id: 'sess-1', engine_elo: 1500, player_color: 'black' }
    mockResponse(expected)

    const result = await startGame(1500, 'black')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Internal Server Error')

    await expect(startGame()).rejects.toThrow(
      'Failed to start game: Internal Server Error',
    )
  })

  it('includes JWT token in headers when available', async () => {
    localStorage.setItem('ghost_replay_token', 'test-jwt-token')
    mockResponse({ session_id: 'sess-1', engine_elo: 1500 })

    await startGame()

    expect(fetchMock).toHaveBeenCalledWith(
      expect.any(String),
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: 'Bearer test-jwt-token',
          'Content-Type': 'application/json',
        }),
      }),
    )
  })

  it('omits Authorization header when no token', async () => {
    mockResponse({ session_id: 'sess-1', engine_elo: 1500 })

    await startGame()

    const [, options] = fetchMock.mock.calls[0]
    expect(options.headers).not.toHaveProperty('Authorization')
    expect(options.headers['Content-Type']).toBe('application/json')
  })
})

describe('endGame', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct request body', async () => {
    mockResponse({ session_id: 'sess-1', blunders_recorded: 1, blunders_reviewed: 0 })

    await endGame('sess-1', 'checkmate_win', '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#')

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/game/end'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          result: 'checkmate_win',
          pgn: '1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7#',
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { session_id: 'sess-1', blunders_recorded: 1, blunders_reviewed: 0 }
    mockResponse(expected)

    const result = await endGame('sess-1', 'resign', '1. e4')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Not Found')

    await expect(endGame('sess-1', 'resign', '')).rejects.toThrow(
      'Failed to end game: Not Found',
    )
  })

  it('handles all result types', async () => {
    const results = ['checkmate_win', 'checkmate_loss', 'resign', 'draw', 'abandon'] as const

    for (const result of results) {
      fetchMock.mockReset()
      mockResponse({ session_id: 'sess-1', blunders_recorded: 0, blunders_reviewed: 0 })

      await endGame('sess-1', result, '1. e4')

      const body = JSON.parse(fetchMock.mock.calls[0][1].body)
      expect(body.result).toBe(result)
    }
  })
})

describe('recordBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct snake_case request body', async () => {
    mockResponse({ blunder_id: 1, position_id: 10, positions_created: 3, is_new: true })

    await recordBlunder(
      'sess-1',
      '1. e4 d5 2. Bb5+',
      'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
      'Bb5+',
      'd2d4',
      50,
      -150,
    )

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          pgn: '1. e4 d5 2. Bb5+',
          fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
          user_move: 'Bb5+',
          best_move: 'd2d4',
          eval_before: 50,
          eval_after: -150,
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { blunder_id: 1, position_id: 10, positions_created: 3, is_new: true }
    mockResponse(expected)

    const result = await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unprocessable Entity')

    await expect(
      recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100),
    ).rejects.toThrow('Failed to record blunder: Unprocessable Entity')
  })

  it('sends auth headers', async () => {
    localStorage.setItem('ghost_replay_token', 'jwt-123')
    mockResponse({ blunder_id: 1, position_id: 1, positions_created: 1, is_new: true })

    await recordBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 50, -100)

    const [, options] = fetchMock.mock.calls[0]
    expect(options.headers.Authorization).toBe('Bearer jwt-123')
  })
})

describe('getGhostMove', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends GET request with query parameters', async () => {
    mockResponse({ mode: 'ghost', move: 'e4', target_blunder_id: 42 })

    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await getGhostMove('sess-1', fen)

    const [url, options] = fetchMock.mock.calls[0]
    expect(options.method).toBe('GET')
    expect(url).toContain('session_id=sess-1')
    expect(url).toContain('/api/game/ghost-move')
  })

  it('returns ghost mode response', async () => {
    const expected = { mode: 'ghost', move: 'Nf3', target_blunder_id: 7 }
    mockResponse(expected)

    const result = await getGhostMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('returns engine mode response', async () => {
    const expected = { mode: 'engine', move: null, target_blunder_id: null }
    mockResponse(expected)

    const result = await getGhostMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Forbidden')

    await expect(getGhostMove('sess-1', 'fen')).rejects.toThrow(
      'Failed to get ghost move: Forbidden',
    )
  })
})
