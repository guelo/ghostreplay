import { useCallback, useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import { Chessboard } from 'react-chessboard'
import type { PieceDropHandlerArgs } from 'react-chessboard'
import { useStockfishEngine } from '../hooks/useStockfishEngine'
import { useMoveAnalysis } from '../hooks/useMoveAnalysis'

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
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : 'Unable to apply Stockfish move.'
      setEngineMessage(message)
    }
  }, [chess, evaluatePosition])

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

    if (!chess.isGameOver()) {
      void applyEngineMove()
    }

    return true
  }

  const handleReset = () => {
    chess.reset()
    setFen(chess.fen())
    setBoardOrientation('white')
    setEngineMessage(null)
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
          <div className="chess-controls">
            <button className="chess-button primary" type="button" onClick={handleReset}>
              Reset game
            </button>
            <button className="chess-button" type="button" onClick={flipBoard}>
              Flip board
            </button>
          </div>
        </div>

        <div className="chessboard-wrapper">
          <Chessboard
            options={{
              position: fen,
              onPieceDrop: handleDrop,
              boardOrientation,
              animationDurationInMs: 200,
              allowDragging:
                engineStatus === 'ready' && chess.turn() === 'w' && !isThinking,
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
    </section>
  )
}

export default ChessGame
