/**
 * API client for Ghost Replay backend
 */

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

/**
 * Get or create a user ID for API requests.
 * For MVP, we use a simple numeric ID stored in localStorage.
 * In production, this would integrate with the auth endpoints.
 */
const getUserId = (): string => {
  let userId = localStorage.getItem('ghost_replay_user_id')
  if (!userId) {
    // Generate a simple numeric user ID for MVP
    userId = String(Math.floor(Math.random() * 1000000) + 1)
    localStorage.setItem('ghost_replay_user_id', userId)
  }
  return userId
}

interface StartGameRequest {
  engine_elo: number
}

interface StartGameResponse {
  session_id: string
  engine_elo: number
}

interface EndGameRequest {
  session_id: string
  result: 'checkmate_win' | 'checkmate_loss' | 'resign' | 'draw' | 'abandon'
  pgn: string
}

interface EndGameResponse {
  session_id: string
  blunders_recorded: number
  blunders_reviewed: number
}

/**
 * Start a new game session
 */
export const startGame = async (
  engineElo: number = 1500
): Promise<StartGameResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/game/start`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-Id': getUserId(),
    },
    body: JSON.stringify({ engine_elo: engineElo } satisfies StartGameRequest),
  })

  if (!response.ok) {
    throw new Error(`Failed to start game: ${response.statusText}`)
  }

  return response.json()
}

/**
 * End a game session
 */
export const endGame = async (
  sessionId: string,
  result: EndGameRequest['result'],
  pgn: string
): Promise<EndGameResponse> => {
  const response = await fetch(`${API_BASE_URL}/api/game/end`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-User-Id': getUserId(),
    },
    body: JSON.stringify({ session_id: sessionId, result, pgn } satisfies EndGameRequest),
  })

  if (!response.ok) {
    throw new Error(`Failed to end game: ${response.statusText}`)
  }

  return response.json()
}
