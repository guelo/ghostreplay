import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '../test/utils'
import ChessGame from './ChessGame'

const startGameMock = vi.fn()
const endGameMock = vi.fn()
const uploadSessionMovesMock = vi.fn()
const getGhostMoveMock = vi.fn()
const recordBlunderMock = vi.fn()
const recordManualBlunderMock = vi.fn()
const reviewSrsBlunderMock = vi.fn()

vi.mock('../utils/api', () => ({
  startGame: (...args: unknown[]) => startGameMock(...args),
  endGame: (...args: unknown[]) => endGameMock(...args),
  uploadSessionMoves: (...args: unknown[]) => uploadSessionMovesMock(...args),
  getGhostMove: (...args: unknown[]) => getGhostMoveMock(...args),
  recordBlunder: (...args: unknown[]) => recordBlunderMock(...args),
  recordManualBlunder: (...args: unknown[]) => recordManualBlunderMock(...args),
  reviewSrsBlunder: (...args: unknown[]) => reviewSrsBlunderMock(...args),
}))

const evaluatePositionMock = vi.fn()
const lookupOpeningByFenMock = vi.fn()

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: () => ({
    status: 'ready',
    error: null,
    info: null,
    isThinking: false,
    evaluatePosition: evaluatePositionMock,
    resetEngine: vi.fn(),
  }),
}))

vi.mock('../openings/openingBook', () => ({
  lookupOpeningByFen: (...args: unknown[]) => lookupOpeningByFenMock(...args),
}))

let mockLastAnalysis: {
  id: string
  move: string
  bestMove: string
  bestEval: number | null
  playedEval: number | null
  currentPositionEval: number | null
  moveIndex?: number | null
  delta: number | null
  blunder: boolean
} | null = null
let mockAnalysisMap = new Map<number, unknown>()
const mockAnalyzeMove = vi.fn()

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: mockAnalyzeMove,
    lastAnalysis: mockLastAnalysis,
    analysisMap: mockAnalysisMap,
    status: 'ready',
    isAnalyzing: false,
    analyzingMove: null,
    clearAnalysis: vi.fn(),
  }),
}))

// Capture onPieceDrop from the Chessboard mock so tests can simulate moves
let capturedPieceDrop: ((args: { sourceSquare: string; targetSquare: string }) => boolean) | null =
  null

vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    capturedPieceDrop = options.onPieceDrop as typeof capturedPieceDrop
    return (
      <div
        data-testid="chessboard"
        data-orientation={options.boardOrientation as string}
      />
    )
  },
}))

describe('ChessGame start flow', () => {
  beforeEach(() => {
    startGameMock.mockReset()
    endGameMock.mockReset()
    uploadSessionMovesMock.mockReset()
    getGhostMoveMock.mockReset()
    recordManualBlunderMock.mockReset()
    reviewSrsBlunderMock.mockReset()
    lookupOpeningByFenMock.mockReset()
    mockAnalysisMap = new Map()
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 })
    // Default: no ghost move available, fall back to engine
    getGhostMoveMock.mockResolvedValue({ mode: 'engine', move: null, target_blunder_id: null })
    lookupOpeningByFenMock.mockResolvedValue(null)
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('defaults to random color on Play', async () => {
    vi.spyOn(Math, 'random').mockReturnValueOnce(0.9)
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-123',
      engine_elo: 1500,
      player_color: 'black',
    })

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    expect(screen.getByRole('button', { name: /play random/i })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    expect(screen.getByText('Random')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalledWith(1500, 'black')
    })

    await waitFor(() => {
      expect(screen.getByText('Black')).toBeInTheDocument()
    })

  })

  it('calls ghost-move endpoint when playing as black', async () => {
    const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-456',
      engine_elo: 1500,
      player_color: 'black',
    })

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play black/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(getGhostMoveMock).toHaveBeenCalledWith('session-456', STARTING_FEN)
    })
  })
})

describe('ChessGame blunder recording', () => {
  beforeEach(() => {
    // jsdom doesn't implement scrollIntoView
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
    startGameMock.mockReset()
    endGameMock.mockReset()
    uploadSessionMovesMock.mockReset()
    getGhostMoveMock.mockReset()
    recordBlunderMock.mockReset()
    recordManualBlunderMock.mockReset()
    reviewSrsBlunderMock.mockReset()
    mockAnalyzeMove.mockReset()
    evaluatePositionMock.mockReset()
    lookupOpeningByFenMock.mockReset()
    mockLastAnalysis = null
    mockAnalysisMap = new Map()
    capturedPieceDrop = null

    getGhostMoveMock.mockResolvedValue({
      mode: 'engine',
      move: null,
      target_blunder_id: null,
    })
    // Return a valid engine move so applyEngineMove succeeds
    evaluatePositionMock.mockResolvedValue({ move: 'd7d5' })
    lookupOpeningByFenMock.mockResolvedValue(null)
    recordBlunderMock.mockResolvedValue({
      blunder_id: 1,
      position_id: 10,
      positions_created: 3,
      is_new: true,
    })
    recordManualBlunderMock.mockResolvedValue({
      blunder_id: 2,
      position_id: 11,
      positions_created: 1,
      is_new: true,
    })
    reviewSrsBlunderMock.mockResolvedValue({
      blunder_id: 42,
      pass_streak: 1,
      priority: 0,
      next_expected_review: '2026-02-08T00:00:00Z',
    })
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 })
  })

  afterEach(() => {
    mockLastAnalysis = null
    vi.restoreAllMocks()
  })

  const startGameAsWhite = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-blunder',
      engine_elo: 1500,
      player_color: 'white',
    })

    const renderResult = render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play white/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled()
    })

    return renderResult
  }

  it('calls recordBlunder when analysis detects a blunder after user move', async () => {
    // When analyzeMove is called (during handleDrop), set up a blunder result
    // for the next render cycle
    mockAnalyzeMove.mockImplementation(() => {
      mockLastAnalysis = {
        id: 'test-blunder',
        move: 'e2e4',
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        delta: 200,
        blunder: true,
      }
    })

    const { rerender } = await startGameAsWhite()

    // Simulate user making a move (e2 to e4)
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    // Wait for analyzeMove to be called
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.stringContaining('rnbqkbnr'), // FEN before move
        'e2e4',
        'white',
        0, // move index
      )
    })

    // Re-render to pick up the new lastAnalysis from the mock
    rerender(<ChessGame />)

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledWith(
        'session-blunder',
        expect.any(String), // PGN
        expect.stringContaining('rnbqkbnr'), // FEN before move
        'e4', // SAN format
        'd2d4', // best move
        50, // eval before
        -150, // eval after
      )
    })
  })

  it('does not call recordBlunder for non-blunder analysis', async () => {
    mockAnalyzeMove.mockImplementation(() => {
      mockLastAnalysis = {
        id: 'test-ok',
        move: 'e2e4',
        bestMove: 'e2e4',
        bestEval: 50,
        playedEval: 40,
        currentPositionEval: 40,
        delta: 10,
        blunder: false,
      }
    })

    const { rerender } = await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled()
    })

    rerender(<ChessGame />)

    // Give effects time to run
    await new Promise((r) => setTimeout(r, 50))

    expect(recordBlunderMock).not.toHaveBeenCalled()
  })

  it('records only the first blunder per session (first-only rule)', async () => {
    mockAnalyzeMove.mockImplementation(() => {
      mockLastAnalysis = {
        id: 'blunder-1',
        move: 'e2e4',
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        delta: 200,
        blunder: true,
      }
    })

    const { rerender } = await startGameAsWhite()

    // First move triggers first blunder
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled()
    })

    rerender(<ChessGame />)

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)
    })

    // Simulate a second blunder (different analysis object to trigger useEffect)
    mockLastAnalysis = {
      id: 'blunder-2',
      move: 'e2e4',
      bestMove: 'g1f3',
      bestEval: 100,
      playedEval: -200,
      currentPositionEval: -200,
      delta: 300,
      blunder: true,
    }

    rerender(<ChessGame />)

    // Wait for any effects
    await new Promise((r) => setTimeout(r, 50))

    // Should still be exactly 1 call - second blunder NOT recorded
    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
  })

  it('does not call recordBlunder when move UCI does not match analysis', async () => {
    // Analysis is for a different move than what was played
    mockAnalyzeMove.mockImplementation(() => {
      mockLastAnalysis = {
        id: 'test-mismatch',
        move: 'g1f3', // Analysis is for Nf3, not e4
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        delta: 200,
        blunder: true,
      }
    })

    const { rerender } = await startGameAsWhite()

    // User plays e2e4, but analysis will claim it's for g1f3
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled()
    })

    rerender(<ChessGame />)

    await new Promise((r) => setTimeout(r, 50))

    expect(recordBlunderMock).not.toHaveBeenCalled()
  })

  it('does not call recordBlunder when no session is active', async () => {
    // Don't start a game - just render with no session
    mockLastAnalysis = {
      id: 'no-session',
      move: 'e2e4',
      bestMove: 'd2d4',
      bestEval: 50,
      playedEval: -150,
      currentPositionEval: -150,
      delta: 200,
      blunder: true,
    }

    render(<ChessGame />)

    await new Promise((r) => setTimeout(r, 50))

    expect(recordBlunderMock).not.toHaveBeenCalled()
  })

  it('does not retry recordBlunder on API failure', async () => {
    recordBlunderMock.mockRejectedValueOnce(new Error('Network error'))

    mockAnalyzeMove.mockImplementation(() => {
      mockLastAnalysis = {
        id: 'fail-test',
        move: 'e2e4',
        bestMove: 'd2d4',
        bestEval: 50,
        playedEval: -150,
        currentPositionEval: -150,
        delta: 200,
        blunder: true,
      }
    })

    const { rerender } = await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalled()
    })

    rerender(<ChessGame />)

    await waitFor(() => {
      expect(recordBlunderMock).toHaveBeenCalledTimes(1)
    })

    // Even after a second analysis result, no retry since blunderRecordedRef is true
    mockLastAnalysis = {
      id: 'fail-test-2',
      move: 'e2e4',
      bestMove: 'd2d4',
      bestEval: 50,
      playedEval: -150,
      currentPositionEval: -150,
      delta: 200,
      blunder: true,
    }

    rerender(<ChessGame />)

    await new Promise((r) => setTimeout(r, 50))

    expect(recordBlunderMock).toHaveBeenCalledTimes(1)
  })

  it('adds selected player move to ghost library from MoveList', async () => {
    evaluatePositionMock.mockResolvedValue({ move: '(none)' })
    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    fireEvent.click(screen.getByRole('button', { name: /e4/i }))
    fireEvent.click(screen.getByRole('button', { name: /add to ghost library/i }))

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledWith(
        'session-blunder',
        expect.stringContaining('1. e4'),
        'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
        'e4',
        'e4',
        0,
        0,
      )
    })
  })

  it('handles duplicate add without rendering status line', async () => {
    evaluatePositionMock.mockResolvedValue({ move: '(none)' })
    recordManualBlunderMock.mockResolvedValueOnce({
      blunder_id: 2,
      position_id: 11,
      positions_created: 0,
      is_new: false,
    })
    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    fireEvent.click(screen.getByRole('button', { name: /add to ghost library/i }))

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledTimes(1)
    })

    expect(screen.queryByText('Already in library.')).not.toBeInTheDocument()
    expect(screen.queryByText('Added to ghost library.')).not.toBeInTheDocument()
  })

  it('allows manual add after game has ended', async () => {
    evaluatePositionMock.mockResolvedValue({ move: '(none)' })
    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    fireEvent.click(screen.getByRole('button', { name: /resign/i }))

    await waitFor(() => {
      expect(screen.getByText('You resigned.')).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /add to ghost library/i }))

    await waitFor(() => {
      expect(recordManualBlunderMock).toHaveBeenCalledTimes(1)
    })
  })

  it('hides add button when selected move is not a player move', async () => {
    evaluatePositionMock.mockResolvedValue({ move: 'd7d5' })
    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    fireEvent.click(screen.getByRole('button', { name: /d5/i }))

    expect(
      screen.queryByRole('button', { name: /add to ghost library/i }),
    ).not.toBeInTheDocument()
  })

  it('shows review warning when arriving at a previously failed position', async () => {
    getGhostMoveMock
      .mockResolvedValueOnce({
        mode: 'ghost',
        move: 'e5',
        target_blunder_id: 77,
      })
      .mockResolvedValue({
        mode: 'engine',
        move: null,
        target_blunder_id: null,
      })

    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(
        screen.getByText('Be careful. You screwed this position up last time.'),
      ).toBeInTheDocument()
    })

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'g1', targetSquare: 'f3' })
    })

    await waitFor(() => {
      expect(
        screen.queryByText('Be careful. You screwed this position up last time.'),
      ).not.toBeInTheDocument()
    })
  })

  it('records SRS pass for review target when eval delta is below 50cp', async () => {
    getGhostMoveMock
      .mockResolvedValueOnce({
        mode: 'ghost',
        move: 'e5',
        target_blunder_id: 42,
      })
      .mockResolvedValue({
        mode: 'engine',
        move: null,
        target_blunder_id: null,
      })

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          mockLastAnalysis = {
            id: 'review-pass',
            move,
            bestMove: 'g1f3',
            bestEval: 40,
            playedEval: 20,
            currentPositionEval: 20,
            moveIndex: 2,
            delta: 20,
            blunder: false,
          }
        }
      },
    )

    const { rerender } = await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        'e7e5',
        'black',
        1,
      )
    })

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'g1', targetSquare: 'f3' })
    })
    rerender(<ChessGame />)

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        'session-blunder',
        42,
        true,
        'Nf3',
        20,
      )
    })
    expect(screen.getByText('You avoided your past mistake.')).toBeInTheDocument()
  })

  it('records SRS fail for review target when eval delta is 50cp or higher', async () => {
    getGhostMoveMock
      .mockResolvedValueOnce({
        mode: 'ghost',
        move: 'e5',
        target_blunder_id: 99,
      })
      .mockResolvedValue({
        mode: 'engine',
        move: null,
        target_blunder_id: null,
      })

    mockAnalyzeMove.mockImplementation(
      (_fen: string, move: string, _color: string, moveIndex: number) => {
        if (moveIndex === 2) {
          mockLastAnalysis = {
            id: 'review-fail',
            move,
            bestMove: 'g1f3',
            bestEval: 40,
            playedEval: -10,
            currentPositionEval: -10,
            moveIndex: 2,
            delta: 50,
            blunder: false,
          }
        }
      },
    )

    const { rerender } = await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })
    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'g1', targetSquare: 'f3' })
    })
    rerender(<ChessGame />)

    await waitFor(() => {
      expect(reviewSrsBlunderMock).toHaveBeenCalledWith(
        'session-blunder',
        99,
        false,
        'Nf3',
        50,
      )
    })
    expect(screen.queryByText('You avoided your past mistake.')).not.toBeInTheDocument()
  })
})

describe('ChessGame move analysis', () => {
  beforeEach(() => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn()
    startGameMock.mockReset()
    endGameMock.mockReset()
    uploadSessionMovesMock.mockReset()
    getGhostMoveMock.mockReset()
    recordBlunderMock.mockReset()
    recordManualBlunderMock.mockReset()
    reviewSrsBlunderMock.mockReset()
    mockAnalyzeMove.mockReset()
    evaluatePositionMock.mockReset()
    lookupOpeningByFenMock.mockReset()
    mockLastAnalysis = null
    mockAnalysisMap = new Map()
    capturedPieceDrop = null

    getGhostMoveMock.mockResolvedValue({
      mode: 'engine',
      move: null,
      target_blunder_id: null,
    })
    evaluatePositionMock.mockResolvedValue({ move: 'd7d5' })
    lookupOpeningByFenMock.mockResolvedValue(null)
    reviewSrsBlunderMock.mockResolvedValue({
      blunder_id: 1,
      pass_streak: 1,
      priority: 0,
      next_expected_review: '2026-02-08T00:00:00Z',
    })
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 })
  })

  afterEach(() => {
    mockLastAnalysis = null
    vi.restoreAllMocks()
  })

  const startGameAsWhite = async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-analysis',
      engine_elo: 1500,
      player_color: 'white',
    })

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play white/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(startGameMock).toHaveBeenCalled()
    })
  }

  it('calls analyzeMove for both player and engine moves', async () => {
    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    // Player move analyzed with player color and index 0
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.stringContaining('rnbqkbnr'),
        'e2e4',
        'white',
        0,
      )
    })

    // Engine responds with d7d5 — analyzed with opponent color and index 1
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        'd7d5',
        'black',
        1,
      )
    })
  })

  it('calls analyzeMove for ghost moves with opponent color', async () => {
    // Ghost returns a move instead of engine
    getGhostMoveMock.mockResolvedValue({
      mode: 'ghost',
      move: 'e5',
      target_blunder_id: null,
    })

    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    // Player move
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        'e2e4',
        'white',
        0,
      )
    })

    // Ghost move analyzed with opponent color
    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        'e7e5',
        'black',
        1,
      )
    })
  })

  it('uploads player and engine move analysis batch on resign', async () => {
    endGameMock.mockResolvedValue({
      session_id: 'session-analysis',
      blunders_recorded: 0,
      blunders_reviewed: 0,
    })
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 2 })

    await startGameAsWhite()

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(mockAnalyzeMove).toHaveBeenCalledWith(
        expect.any(String),
        'd7d5',
        'black',
        1,
      )
    })

    mockAnalysisMap.set(0, {
      id: 'analysis-0',
      move: 'e2e4',
      bestMove: 'e2e4',
      bestEval: 25,
      playedEval: 25,
      currentPositionEval: 25,
      moveIndex: 0,
      delta: 0,
      blunder: false,
    })
    mockAnalysisMap.set(1, {
      id: 'analysis-1',
      move: 'd7d5',
      bestMove: 'd7d5',
      bestEval: 16,
      playedEval: 8,
      currentPositionEval: 8,
      moveIndex: 1,
      delta: 8,
      blunder: false,
    })

    fireEvent.click(screen.getByRole('button', { name: /resign/i }))

    await waitFor(() => {
      expect(uploadSessionMovesMock).toHaveBeenCalledTimes(1)
    })

    expect(uploadSessionMovesMock).toHaveBeenCalledWith(
      'session-analysis',
      [
        expect.objectContaining({
          move_number: 1,
          color: 'white',
          move_san: 'e4',
          fen_after: expect.any(String),
          eval_cp: 25,
          eval_mate: null,
          best_move_san: 'e4',
          best_move_eval_cp: 25,
          eval_delta: 0,
          classification: 'best',
        }),
        expect.objectContaining({
          move_number: 1,
          color: 'black',
          move_san: 'd5',
          fen_after: expect.any(String),
          eval_cp: 8,
          eval_mate: null,
          best_move_san: 'd5',
          best_move_eval_cp: 16,
          eval_delta: 8,
          classification: 'excellent',
        }),
      ],
    )

    await waitFor(() => {
      expect(endGameMock).toHaveBeenCalledWith('session-analysis', 'resign', expect.any(String))
    })
  })
})

describe('ChessGame opening display', () => {
  beforeEach(() => {
    startGameMock.mockReset()
    uploadSessionMovesMock.mockReset()
    getGhostMoveMock.mockReset()
    evaluatePositionMock.mockReset()
    lookupOpeningByFenMock.mockReset()
    mockAnalysisMap = new Map()
    capturedPieceDrop = null

    getGhostMoveMock.mockResolvedValue({
      mode: 'engine',
      move: null,
      target_blunder_id: null,
    })
    evaluatePositionMock.mockResolvedValue({ move: 'e7e5' })
    lookupOpeningByFenMock.mockResolvedValue({
      eco: 'C20',
      name: "King's Pawn Game",
      source: 'eco',
    })
    uploadSessionMovesMock.mockResolvedValue({ moves_inserted: 0 })
  })

  it('shows opening only during an active game', async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-opening',
      engine_elo: 1500,
      player_color: 'white',
    })

    render(<ChessGame />)

    expect(screen.queryByText(/^Opening:/i)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play white/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(screen.getByText('Opening:')).toBeInTheDocument()
      expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByRole('button', { name: /reset/i }))
    expect(screen.queryByText(/^Opening:/i)).not.toBeInTheDocument()
  })

  it('keeps opening tied to live position while navigating history', async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-live-opening',
      engine_elo: 1500,
      player_color: 'white',
    })
    lookupOpeningByFenMock.mockResolvedValue({
      eco: 'C50',
      name: 'Italian Game',
      source: 'eco',
    })

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play white/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(lookupOpeningByFenMock).toHaveBeenCalled()
    })

    const initialLookupCount = lookupOpeningByFenMock.mock.calls.length

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(lookupOpeningByFenMock.mock.calls.length).toBeGreaterThan(
        initialLookupCount,
      )
      expect(screen.getByText('C50 Italian Game')).toBeInTheDocument()
    })

    const afterMoveLookupCount = lookupOpeningByFenMock.mock.calls.length
    fireEvent.click(screen.getByTitle(/previous move/i))

    expect(screen.getByText('C50 Italian Game')).toBeInTheDocument()
    expect(lookupOpeningByFenMock.mock.calls.length).toBe(afterMoveLookupCount)
  })

  it('shows Unknown after leaving the opening book', async () => {
    startGameMock.mockResolvedValueOnce({
      session_id: 'session-sticky-opening',
      engine_elo: 1500,
      player_color: 'white',
    })
    lookupOpeningByFenMock
      .mockResolvedValueOnce({
        eco: 'C20',
        name: "King's Pawn Game",
        source: 'eco',
      })
      .mockResolvedValue(null)

    render(<ChessGame />)

    fireEvent.click(screen.getByRole('button', { name: /new game/i }))
    fireEvent.click(screen.getByRole('button', { name: /play white/i }))
    fireEvent.click(screen.getByRole('button', { name: /^play$/i }))

    await waitFor(() => {
      expect(screen.getByText("C20 King's Pawn Game")).toBeInTheDocument()
    })

    await act(async () => {
      capturedPieceDrop?.({ sourceSquare: 'e2', targetSquare: 'e4' })
    })

    await waitFor(() => {
      expect(lookupOpeningByFenMock.mock.calls.length).toBeGreaterThanOrEqual(2)
    })

    expect(screen.getByText('Unknown')).toBeInTheDocument()
  })
})
