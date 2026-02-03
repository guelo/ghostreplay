import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '../test/utils'
import ChessGame from './ChessGame'

const startGameMock = vi.fn()
const endGameMock = vi.fn()

vi.mock('../utils/api', () => ({
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
}))

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: () => ({
    status: 'ready',
    error: null,
    info: null,
    isThinking: false,
    evaluatePosition: vi.fn(),
    resetEngine: vi.fn(),
  }),
}))

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: vi.fn(),
    lastAnalysis: null,
    status: 'ready',
    isAnalyzing: false,
    analyzingMove: null,
  }),
}))

vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: { boardOrientation: string } }) => (
    <div data-testid="chessboard" data-orientation={options.boardOrientation} />
  ),
}))

describe('ChessGame start flow', () => {
  beforeEach(() => {
    startGameMock.mockReset()
    endGameMock.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('resolves random color on Play', async () => {
    vi.spyOn(Math, 'random').mockReturnValueOnce(0.9)
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-123',
      engine_elo: 1500,
      player_color: 'black',
    })

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play random/i }))

    expect(screen.getByText('Random')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalledWith(1500, 'black')
    })

    await waitFor(() => {
      expect(screen.getByText('Black')).toBeInTheDocument()
    })

  })
})
