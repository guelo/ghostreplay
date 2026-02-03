import { useCallback, useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import { Chessboard } from 'react-chessboard'
import type { PieceDropHandlerArgs } from 'react-chessboard'
import { useStockfishEngine } from '../hooks/useStockfishEngine'
import { useMoveAnalysis } from '../hooks/useMoveAnalysis'
import { startGame, endGame } from '../utils/api'

type BoardOrientation = 'white' | 'black'

const formatScore = (score?: { type: 'cp' | 'mate'; value: number }) => {
  if (!score) {
    return null
  }

  if (score.type === 'mate') {
    return `M${score.value}`
  }

  const value = score.value / 100
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}`
}

type GameResult = {
  type: 'checkmate_win' | 'checkmate_loss' | 'draw' | 'resign'
  message: string
}

const ChessGame = () => {
  const chess = useMemo(() => new Chess(), [])
  const [fen, setFen] = useState(chess.fen())
  const [boardOrientation, setBoardOrientation] =
    useState<BoardOrientation>('white')
  const {
    status: engineStatus,
    error: engineError,
    info: engineInfo,
    isThinking,
    evaluatePosition,
    resetEngine,
  } = useStockfishEngine()
  const { analyzeMove, lastAnalysis, status: analysisStatus, isAnalyzing, analyzingMove } = useMoveAnalysis()
  const [engineMessage, setEngineMessage] = useState<string | null>(null)
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [isGameActive, setIsGameActive] = useState(false)
  const [gameResult, setGameResult] = useState<GameResult | null>(null)

  const handleGameEnd = useCallback(async () => {
    if (!sessionId || !isGameActive) return

    let result: GameResult | null = null

    if (chess.isCheckmate()) {
      // If it's white's turn, white is in checkmate, so black (engine) wins
      // User plays white, so if white is checkmated, user loses
      if (chess.turn() === 'w') {
        result = { type: 'checkmate_loss', message: 'Checkmate! You lost.' }
      } else {
        result = { type: 'checkmate_win', message: 'Checkmate! You won!' }
      }
    } else if (chess.isStalemate()) {
      result = { type: 'draw', message: 'Stalemate! The game is a draw.' }
    } else if (chess.isThreefoldRepetition()) {
      result = { type: 'draw', message: 'Draw by threefold repetition.' }
    } else if (chess.isInsufficientMaterial()) {
      result = { type: 'draw', message: 'Draw by insufficient material.' }
    } else if (chess.isDraw()) {
      result = { type: 'draw', message: 'The game is a draw.' }
    }

    if (result) {
      try {
        await endGame(sessionId, result.type, chess.pgn())
        setIsGameActive(false)
        setGameResult(result)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to end game.'
        setEngineMessage(message)
      }
    }
  }, [chess, sessionId, isGameActive])

  const statusText = (() => {
    if (chess.isCheckmate()) {
      const winningColor = chess.turn() === 'w' ? 'Black' : 'White'
      return `${winningColor} wins by checkmate`
    }

    if (chess.isDraw()) {
      return 'Drawn position'
    }

    if (chess.isGameOver()) {
      return 'Game over'
    }

    const active = chess.turn() === 'w' ? 'White' : 'Black'
    const suffix = chess.inCheck() ? ' (check)' : ''
    return `${active} to move${suffix}`
  })()

  const engineStatusText = (() => {
    if (engineError) {
      return engineError
    }

    if (engineMessage) {
      return engineMessage
    }

    if (engineStatus === 'booting') {
      return 'Stockfish is warming up…'
    }

    if (engineStatus === 'error') {
      return 'Stockfish is unavailable.'
    }

    if (isThinking) {
      const formattedScore = formatScore(engineInfo?.score)
      const parts = [
        'Stockfish is thinking…',
        engineInfo?.depth ? `depth ${engineInfo.depth}` : null,
        formattedScore ? `eval ${formattedScore}` : null,
      ].filter(Boolean)
      return parts.join(' · ')
    }

    return 'Stockfish is ready.'
  })()

  const analysisStatusText = (() => {
    if (analysisStatus === 'booting') {
      return 'Analyst is warming up…'
    }

    if (analysisStatus === 'error') {
      return 'Analyst is unavailable.'
    }

    if (isAnalyzing && analyzingMove) {
      return `Analyzing ${analyzingMove}…`
    }

    if (!lastAnalysis) {
      return 'Analyst is ready.'
    }

    const evalText =
      lastAnalysis.currentPositionEval !== null
        ? ` Eval: ${lastAnalysis.currentPositionEval > 0 ? '+' : ''}${(lastAnalysis.currentPositionEval / 100).toFixed(2)}`
        : ''

    if (lastAnalysis.blunder && lastAnalysis.delta !== null) {
      return `⚠️ ${lastAnalysis.move}: Blunder! Lost ${lastAnalysis.delta}cp. Best: ${lastAnalysis.bestMove}.${evalText}`
    }

    if (lastAnalysis.delta !== null) {
      if (lastAnalysis.delta === 0) {
        return `✓ ${lastAnalysis.move}: Best move!${evalText}`
      }
      if (lastAnalysis.delta < 50) {
        return `✓ ${lastAnalysis.move}: Good move. Lost ${lastAnalysis.delta}cp. Best: ${lastAnalysis.bestMove}.${evalText}`
      }
      return `${lastAnalysis.move}: Inaccuracy. Lost ${lastAnalysis.delta}cp. Best: ${lastAnalysis.bestMove}.${evalText}`
    }

    return 'Analyst is ready.'
  })()

  const applyEngineMove = useCallback(async () => {
    try {
      const result = await evaluatePosition(chess.fen())

      if (result.move === '(none)') {
        setEngineMessage('Stockfish has no legal moves.')
        return
      }

      const from = result.move.slice(0, 2)
      const to = result.move.slice(2, 4)
      const promotion = result.move.slice(4) || undefined
      const appliedMove = chess.move({ from, to, promotion })

      if (!appliedMove) {
        throw new Error(`Engine returned illegal move: ${result.move}`)
      }

      setFen(chess.fen())
      setEngineMessage(null)

      // Check if the engine's move ended the game
      if (chess.isGameOver()) {
        await handleGameEnd()
      }
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : 'Unable to apply Stockfish move.'
      setEngineMessage(message)
    }
  }, [chess, evaluatePosition, handleGameEnd])

  const handleDrop = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs) => {
    if (!targetSquare) {
      return false
    }

    const fenBeforeMove = chess.fen()
    const move = chess.move({
      from: sourceSquare,
      to: targetSquare,
      promotion: 'q',
    })

    if (!move) {
      return false
    }

    setFen(chess.fen())

    const uciMove = `${move.from}${move.to}${move.promotion ?? ''}`
    analyzeMove(fenBeforeMove, uciMove)

    if (chess.isGameOver()) {
      void handleGameEnd()
    } else {
      void applyEngineMove()
    }

    return true
  }

  const handleNewGame = async () => {
    try {
      // End current session if active
      if (sessionId && isGameActive) {
        await endGame(sessionId, 'abandon', chess.pgn())
      }

      // Start new game session
      const response = await startGame(1500)
      setSessionId(response.session_id)
      setIsGameActive(true)

      // Reset the board
      chess.reset()
      setFen(chess.fen())
      setBoardOrientation('white')
      setEngineMessage(null)
      setGameResult(null)
      resetEngine()
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to start new game.'
      setEngineMessage(message)
    }
  }

  const handleCloseModal = () => {
    setGameResult(null)
  }

  const handleResign = async () => {
    if (!sessionId || !isGameActive) {
      return
    }

    try {
      await endGame(sessionId, 'resign', chess.pgn())
      setIsGameActive(false)
      setGameResult({ type: 'resign', message: 'You resigned.' })
    } catch (error) {
      const message =
        error instanceof Error ? error.message : 'Failed to resign game.'
      setEngineMessage(message)
    }
  }

  const handleReset = () => {
    chess.reset()
    setFen(chess.fen())
    setBoardOrientation('white')
    setEngineMessage(null)
    setSessionId(null)
    setIsGameActive(false)
    setGameResult(null)
    resetEngine()
  }

  const flipBoard = () => {
    setBoardOrientation((current) => (current === 'white' ? 'black' : 'white'))
  }

  return (
    <section className="chess-section">
      <header className="chess-header">
        <p className="eyebrow">Self-play sandbox</p>
        <h2>ChessGame</h2>
        <p>
          Practice both sides of the board locally. Moves are validated with
          chess.js so illegal drops snap back instantly.
        </p>
      </header>

      <div className="chess-layout">
        <div className="chess-panel" aria-live="polite">
          <p className="chess-status">{statusText}</p>
          <p className="chess-meta">
            Orientation: {boardOrientation === 'white' ? 'White' : 'Black'} on
            bottom
          </p>
          <p className="chess-meta">
            Session:{' '}
            <span className={isGameActive ? 'chess-meta-strong' : ''}>
              {isGameActive ? 'Active' : 'None (click New game to start)'}
            </span>
          </p>
          <div className="chess-controls">
            <button
              className="chess-button danger"
              type="button"
              onClick={handleResign}
              disabled={!isGameActive || chess.isGameOver()}
            >
              Resign
            </button>
            <button className="chess-button" type="button" onClick={flipBoard}>
              Flip board
            </button>
            <button className="chess-button" type="button" onClick={handleReset}>
              Reset
            </button>
          </div>
        </div>

        <div className="chessboard-wrapper">
          {!isGameActive && (
            <div className="chessboard-overlay">
              <button
                className="chess-button primary overlay-button"
                type="button"
                onClick={handleNewGame}
              >
                New game
              </button>
            </div>
          )}
          <Chessboard
            options={{
              position: fen,
              onPieceDrop: handleDrop,
              boardOrientation,
              animationDurationInMs: 200,
              allowDragging:
                isGameActive && engineStatus === 'ready' && chess.turn() === 'w' && !isThinking,
              boardStyle: {
                borderRadius: '0',
                boxShadow: '0 20px 45px rgba(2, 6, 23, 0.5)',
              },
            }}
          />
          <div className="engine-status">
            <p className="chess-meta">
              Engine status:{' '}
              <span className="chess-meta-strong">{engineStatusText}</span>
            </p>
            {engineInfo?.pv && isThinking && (
              <p className="chess-meta">
                Candidate line: {engineInfo.pv.slice(0, 4).join(' ')}
              </p>
            )}
          </div>
          <div className="engine-status">
            <p className="chess-meta">
              Analyst status:{' '}
              <span className="chess-meta-strong">{analysisStatusText}</span>
            </p>
          </div>
        </div>
      </div>

      {gameResult && (
        <div className="game-end-modal-overlay" onClick={handleCloseModal}>
          <div className="game-end-modal" onClick={(e) => e.stopPropagation()}>
            <h3 className="game-end-title">Game Over</h3>
            <p className="game-end-message">{gameResult.message}</p>
            <div className="game-end-actions">
              <button
                className="chess-button primary"
                type="button"
                onClick={handleNewGame}
              >
                New Game
              </button>
              <button
                className="chess-button"
                type="button"
                onClick={handleCloseModal}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  )
}

export default ChessGame
