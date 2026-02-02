import { useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import { Chessboard } from 'react-chessboard'
import type { PieceDropHandlerArgs } from 'react-chessboard'

type BoardOrientation = 'white' | 'black'

const ChessGame = () => {
  const chess = useMemo(() => new Chess(), [])
  const [fen, setFen] = useState(chess.fen())
  const [boardOrientation, setBoardOrientation] =
    useState<BoardOrientation>('white')
  const [autoRotate, setAutoRotate] = useState(true)

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

  const handleDrop = ({
    sourceSquare,
    targetSquare,
  }: PieceDropHandlerArgs) => {
    if (!targetSquare) {
      return false
    }

    const move = chess.move({
      from: sourceSquare,
      to: targetSquare,
      promotion: 'q',
    })

    if (!move) {
      return false
    }

    setFen(chess.fen())

    if (autoRotate) {
      setBoardOrientation((current) => (current === 'white' ? 'black' : 'white'))
    }

    return true
  }

  const handleReset = () => {
    chess.reset()
    setFen(chess.fen())
    setBoardOrientation('white')
  }

  const flipBoard = () => {
    setBoardOrientation((current) => (current === 'white' ? 'black' : 'white'))
  }

  const toggleAutoRotate = () => {
    setAutoRotate((value) => !value)
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
            <button
              className="chess-button"
              type="button"
              aria-pressed={autoRotate}
              onClick={toggleAutoRotate}
            >
              {autoRotate ? 'Auto-rotate on' : 'Auto-rotate off'}
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
              boardStyle: {
                borderRadius: '1.25rem',
                boxShadow: '0 20px 45px rgba(2, 6, 23, 0.5)',
              },
            }}
          />
        </div>
      </div>
    </section>
  )
}

export default ChessGame
