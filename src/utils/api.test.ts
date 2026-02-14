import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  ApiError,
  startGame,
  endGame,
  uploadSessionMoves,
  recordBlunder,
  recordManualBlunder,
  getNextOpponentMove,
  reviewSrsBlunder,
  getStatsSummary,
} from './api'

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

const mockResponse = (
  data: Record<string, unknown>,
  ok = true,
  statusText = 'OK',
  status = ok ? 200 : 500,
) => {
  fetchMock.mockResolvedValueOnce({
    ok,
    status,
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
    mockResponse({}, false, 'Internal Server Error', 500)

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
    mockResponse({}, false, 'Not Found', 404)

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

describe('uploadSessionMoves', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends POST request to session moves endpoint', async () => {
    mockResponse({ moves_inserted: 2 })

    await uploadSessionMoves('sess-1', [
      {
        move_number: 1,
        color: 'white',
        move_san: 'e4',
        fen_after: 'fen-1',
        eval_cp: 20,
        eval_mate: null,
        best_move_san: 'e4',
        best_move_eval_cp: 20,
        eval_delta: 0,
        classification: 'best',
      },
      {
        move_number: 1,
        color: 'black',
        move_san: 'e5',
        fen_after: 'fen-2',
        eval_cp: 10,
        eval_mate: null,
        best_move_san: 'e5',
        best_move_eval_cp: 12,
        eval_delta: 2,
        classification: 'excellent',
      },
    ])

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/session/sess-1/moves'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          moves: [
            {
              move_number: 1,
              color: 'white',
              move_san: 'e4',
              fen_after: 'fen-1',
              eval_cp: 20,
              eval_mate: null,
              best_move_san: 'e4',
              best_move_eval_cp: 20,
              eval_delta: 0,
              classification: 'best',
            },
            {
              move_number: 1,
              color: 'black',
              move_san: 'e5',
              fen_after: 'fen-2',
              eval_cp: 10,
              eval_mate: null,
              best_move_san: 'e5',
              best_move_eval_cp: 12,
              eval_delta: 2,
              classification: 'excellent',
            },
          ],
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { moves_inserted: 4 }
    mockResponse(expected)

    const result = await uploadSessionMoves('sess-1', [])

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unprocessable Entity', 422)

    await expect(uploadSessionMoves('sess-1', [])).rejects.toThrow(
      'Failed to upload session moves: Unprocessable Entity',
    )
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
    mockResponse({}, false, 'Unprocessable Entity', 422)

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

describe('recordManualBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct request body to manual endpoint', async () => {
    mockResponse({ blunder_id: 1, position_id: 10, positions_created: 3, is_new: true })

    await recordManualBlunder(
      'sess-1',
      '1. e4',
      'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
      'e4',
      null,
      null,
      null,
    )

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/blunder/manual'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          pgn: '1. e4',
          fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
          user_move: 'e4',
          best_move: null,
          eval_before: null,
          eval_after: null,
        }),
      }),
    )
  })

  it('returns parsed response', async () => {
    const expected = { blunder_id: 1, position_id: 10, positions_created: 3, is_new: false }
    mockResponse(expected)

    const result = await recordManualBlunder('sess-1', '1. e4', 'fen', 'e4', 'd4', 20, 5)

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unauthorized', 401)

    await expect(
      recordManualBlunder('sess-1', '1. e4', 'fen', 'e4', null, null, null),
    ).rejects.toThrow('Failed to add move to ghost library: Unauthorized')
  })
})

describe('getNextOpponentMove', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends POST request with JSON body', async () => {
    mockResponse({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: 42,
      decision_source: 'ghost_path',
    })

    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    await getNextOpponentMove('sess-1', fen, ['e2e4', 'e7e5'])

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/game/next-opponent-move')
    expect(options.method).toBe('POST')
    expect(options.body).toBe(JSON.stringify({ session_id: 'sess-1', fen, moves: ['e2e4', 'e7e5'] }))
  })

  it('defaults to empty moves array when not provided', async () => {
    mockResponse({
      mode: 'engine',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'backend_engine',
    })

    await getNextOpponentMove('sess-1', 'some-fen')

    const [, options] = fetchMock.mock.calls[0]
    const body = JSON.parse(options.body)
    expect(body.moves).toEqual([])
  })

  it('returns ghost mode response', async () => {
    const expected = {
      mode: 'ghost',
      move: { uci: 'g8f6', san: 'Nf6' },
      target_blunder_id: 7,
      decision_source: 'ghost_path',
    }
    mockResponse(expected)

    const result = await getNextOpponentMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('returns engine mode response', async () => {
    const expected = {
      mode: 'engine',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: null,
      decision_source: 'backend_engine',
    }
    mockResponse(expected)

    const result = await getNextOpponentMove('sess-1', 'some-fen')

    expect(result).toEqual(expected)
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Forbidden', 403)

    await expect(getNextOpponentMove('sess-1', 'fen')).rejects.toThrow(
      'Failed to get opponent move: Forbidden',
    )
  })

  it('retries idempotent request on retryable server errors', async () => {
    mockResponse(
      {
        detail: 'Service unavailable',
        error: { code: 'internal_error', message: 'Internal server error', retryable: true },
      },
      false,
      'Service Unavailable',
      503,
    )
    mockResponse(
      {
        mode: 'ghost',
        move: { uci: 'e7e5', san: 'e5' },
        target_blunder_id: 11,
        decision_source: 'ghost_path',
      },
      true,
      'OK',
      200,
    )

    const result = await getNextOpponentMove('sess-1', 'fen')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(result).toEqual({
      mode: 'ghost',
      move: { uci: 'e7e5', san: 'e5' },
      target_blunder_id: 11,
      decision_source: 'ghost_path',
    })
  })

  it('throws typed ApiError with normalized fields', async () => {
    mockResponse(
      {
        detail: 'Internal server error',
        error: {
          code: 'internal_error',
          message: 'Database unavailable',
          details: { service: 'postgres' },
          retryable: false,
        },
      },
      false,
      'Internal Server Error',
      503,
    )

    try {
      await getNextOpponentMove('sess-1', 'fen')
      throw new Error('Expected getNextOpponentMove to fail')
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError)
      const apiError = error as ApiError
      expect(apiError.status).toBe(503)
      expect(apiError.code).toBe('internal_error')
      expect(apiError.message).toBe('Database unavailable')
      expect(apiError.retryable).toBe(false)
      expect(apiError.details).toEqual({ service: 'postgres' })
    }
  })
})

describe('reviewSrsBlunder', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends correct review payload', async () => {
    mockResponse({
      blunder_id: 42,
      pass_streak: 3,
      priority: 1.25,
      next_expected_review: '2026-02-08T12:00:00Z',
    })

    await reviewSrsBlunder('sess-1', 42, false, 'Qh5', 50)

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/srs/review'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          session_id: 'sess-1',
          blunder_id: 42,
          passed: false,
          user_move: 'Qh5',
          eval_delta: 50,
        }),
      }),
    )
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Unauthorized', 401)

    await expect(
      reviewSrsBlunder('sess-1', 42, true, 'Nf3', 20),
    ).rejects.toThrow('Failed to record SRS review: Unauthorized')
  })
})

describe('getStatsSummary', () => {
  beforeEach(() => {
    fetchMock.mockReset()
    mockStore = {}
  })

  it('sends default window query parameter', async () => {
    mockResponse({
      window_days: 30,
      generated_at: '2026-01-01T00:00:00Z',
      games: {
        played: 0,
        completed: 0,
        active: 0,
        record: { wins: 0, losses: 0, draws: 0, resigns: 0, abandons: 0 },
        avg_duration_seconds: 0,
        avg_moves: 0,
      },
      colors: {
        white: {
          games: 0,
          completed: 0,
          wins: 0,
          losses: 0,
          draws: 0,
          avg_cpl: 0,
          blunders_per_100_moves: 0,
        },
        black: {
          games: 0,
          completed: 0,
          wins: 0,
          losses: 0,
          draws: 0,
          avg_cpl: 0,
          blunders_per_100_moves: 0,
        },
      },
      moves: {
        player_moves: 0,
        avg_cpl: 0,
        mistakes_per_100_moves: 0,
        blunders_per_100_moves: 0,
        quality_distribution: {
          best: 0,
          excellent: 0,
          good: 0,
          inaccuracy: 0,
          mistake: 0,
          blunder: 0,
        },
      },
      library: {
        blunders_total: 0,
        positions_total: 0,
        edges_total: 0,
        new_blunders_in_window: 0,
        avg_blunder_eval_loss_cp: 0,
        top_costly_blunders: [],
      },
      data_completeness: {
        sessions_with_uploaded_moves_pct: 0,
        notes: [],
      },
    })

    await getStatsSummary()

    const [url, options] = fetchMock.mock.calls[0]
    expect(options.method).toBe('GET')
    expect(url).toContain('/api/stats/summary')
    expect(url).toContain('window_days=30')
  })

  it('sends provided window query parameter', async () => {
    mockResponse({
      window_days: 90,
      generated_at: '2026-01-01T00:00:00Z',
      games: {
        played: 0,
        completed: 0,
        active: 0,
        record: { wins: 0, losses: 0, draws: 0, resigns: 0, abandons: 0 },
        avg_duration_seconds: 0,
        avg_moves: 0,
      },
      colors: {
        white: {
          games: 0,
          completed: 0,
          wins: 0,
          losses: 0,
          draws: 0,
          avg_cpl: 0,
          blunders_per_100_moves: 0,
        },
        black: {
          games: 0,
          completed: 0,
          wins: 0,
          losses: 0,
          draws: 0,
          avg_cpl: 0,
          blunders_per_100_moves: 0,
        },
      },
      moves: {
        player_moves: 0,
        avg_cpl: 0,
        mistakes_per_100_moves: 0,
        blunders_per_100_moves: 0,
        quality_distribution: {
          best: 0,
          excellent: 0,
          good: 0,
          inaccuracy: 0,
          mistake: 0,
          blunder: 0,
        },
      },
      library: {
        blunders_total: 0,
        positions_total: 0,
        edges_total: 0,
        new_blunders_in_window: 0,
        avg_blunder_eval_loss_cp: 0,
        top_costly_blunders: [],
      },
      data_completeness: {
        sessions_with_uploaded_moves_pct: 0,
        notes: [],
      },
    })

    await getStatsSummary(90)

    const [url] = fetchMock.mock.calls[0]
    expect(url).toContain('window_days=90')
  })

  it('throws on non-ok response', async () => {
    mockResponse({}, false, 'Bad Request', 400)

    await expect(getStatsSummary(30)).rejects.toThrow(
      'Failed to load stats summary: Bad Request',
    )
  })
})
