import { useCallback, useMemo, useState } from 'react'
import { Chess } from 'chess.js'
import { Chessboard } from 'react-chessboard'
import type { PieceDropHandlerArgs } from 'react-chessboard'
import type { AnalysisMove, SessionMoveClassification } from '../utils/api'
import type { MoveClassification } from '../workers/analysisUtils'
import AnalysisGraph from './AnalysisGraph'
import MoveList from './MoveList'

const STARTING_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'

type AnalysisBoardProps = {
  moves: AnalysisMove[]
  boardOrientation: 'white' | 'black'
  startingFen?: string
}

type WhatIfMove = {
  san: string
  fen: string
}

// Map API classification to MoveList's classification type
const mapClassification = (
  c: SessionMoveClassification | null,
): MoveClassification | null => {
  if (!c) return null
  switch (c) {
    case 'best':
      return 'best'
    case 'excellent':
      return 'great'
    case 'good':
      return 'good'
    case 'inaccuracy':
      return 'inaccuracy'
    case 'mistake':
      return 'inaccuracy'
    case 'blunder':
      return 'blunder'
  }
}

// Convert SAN move to start/end squares using chess.js
const sanToSquares = (
  fen: string,
  san: string,
): { from: string; to: string } | null => {
  try {
    const tempChess = new Chess(fen)
    const result = tempChess.move(san)
    if (!result) return null
    return { from: result.from, to: result.to }
  } catch {
    return null
  }
}

const formatEvalCp = (cp: number): string => {
  const value = cp / 100
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}`
}

const AnalysisBoard = ({
  moves,
  boardOrientation,
  startingFen = STARTING_FEN,
}: AnalysisBoardProps) => {
  const [currentIndex, setCurrentIndex] = useState<number | null>(null)
  const [whatIfMoves, setWhatIfMoves] = useState<WhatIfMove[]>([])
  const [whatIfBranchPoint, setWhatIfBranchPoint] = useState(-1)

  const isInWhatIf = whatIfMoves.length > 0
  const effectiveIndex = currentIndex ?? moves.length - 1

  // Map AnalysisMove[] to Move[] for MoveList
  const mappedMoves = useMemo(
    () =>
      moves.map((m) => ({
        san: m.move_san,
        classification: mapClassification(m.classification),
        eval: m.eval_cp,
      })),
    [moves],
  )

  // Extract eval values for the graph
  const evals = useMemo(() => moves.map((m) => m.eval_cp), [moves])

  // Combined moves for MoveList when in what-if mode
  const moveListMoves = useMemo(() => {
    if (!isInWhatIf) return mappedMoves
    const base = mappedMoves.slice(0, whatIfBranchPoint + 1)
    const branch = whatIfMoves.map((m) => ({ san: m.san }))
    return [...base, ...branch]
  }, [isInWhatIf, mappedMoves, whatIfBranchPoint, whatIfMoves])

  // Current move list index accounting for what-if
  const moveListIndex = useMemo(() => {
    if (!isInWhatIf) return currentIndex
    // In what-if mode, navigate within the combined array
    return null // viewing latest (end of what-if line)
  }, [isInWhatIf, currentIndex])

  // FEN at the position before the current move (needed for arrow SAN→UCI)
  const fenBeforeCurrentMove = useMemo(() => {
    if (isInWhatIf) return null // no arrows in what-if
    if (effectiveIndex < 0) return null
    if (effectiveIndex === 0) return startingFen
    return moves[effectiveIndex - 1]?.fen_after ?? startingFen
  }, [isInWhatIf, effectiveIndex, moves, startingFen])

  // Displayed FEN
  const displayedFen = useMemo(() => {
    if (isInWhatIf) {
      if (whatIfMoves.length === 0) return startingFen
      return whatIfMoves[whatIfMoves.length - 1].fen
    }
    if (effectiveIndex === -1) return startingFen
    return moves[effectiveIndex]?.fen_after ?? startingFen
  }, [isInWhatIf, whatIfMoves, effectiveIndex, moves, startingFen])

  // Arrows for the current position
  const arrows = useMemo(() => {
    if (isInWhatIf) return undefined
    if (effectiveIndex < 0 || !fenBeforeCurrentMove) return undefined

    const move = moves[effectiveIndex]
    if (!move) return undefined

    const result: { startSquare: string; endSquare: string; color: string }[] =
      []

    // Red arrow: played move (always shown)
    const playedSquares = sanToSquares(fenBeforeCurrentMove, move.move_san)
    if (playedSquares) {
      result.push({
        startSquare: playedSquares.from,
        endSquare: playedSquares.to,
        color: 'rgba(248, 113, 113, 0.8)',
      })
    }

    // Green arrow: best move (shown when available and different from played)
    if (move.best_move_san && move.best_move_san !== move.move_san) {
      const bestSquares = sanToSquares(fenBeforeCurrentMove, move.best_move_san)
      if (bestSquares) {
        result.push({
          startSquare: bestSquares.from,
          endSquare: bestSquares.to,
          color: 'rgba(52, 211, 153, 0.8)',
        })
      }
    }

    return result.length > 0 ? result : undefined
  }, [isInWhatIf, effectiveIndex, fenBeforeCurrentMove, moves])

  // Current move data for position info panel
  const currentMove = useMemo(() => {
    if (isInWhatIf || effectiveIndex < 0) return null
    return moves[effectiveIndex] ?? null
  }, [isInWhatIf, effectiveIndex, moves])

  // Handle MoveList navigation
  const handleNavigate = useCallback(
    (index: number | null) => {
      if (isInWhatIf) {
        // Clicking a main-line move exits what-if
        setWhatIfMoves([])
        setWhatIfBranchPoint(-1)
      }
      setCurrentIndex(index)
    },
    [isInWhatIf],
  )

  // Handle piece drop for what-if exploration
  const handleDrop = useCallback(
    ({ sourceSquare, targetSquare }: PieceDropHandlerArgs) => {
      // Determine the FEN to play from
      let baseFen: string
      if (isInWhatIf) {
        baseFen =
          whatIfMoves.length > 0
            ? whatIfMoves[whatIfMoves.length - 1].fen
            : startingFen
      } else {
        baseFen = displayedFen
      }

      try {
        const tempChess = new Chess(baseFen)
        const result = tempChess.move({
          from: sourceSquare,
          to: targetSquare,
          promotion: 'q',
        })
        if (!result) return false

        if (!isInWhatIf) {
          // Entering what-if mode
          setWhatIfBranchPoint(effectiveIndex)
        }

        setWhatIfMoves((prev) => [
          ...prev,
          { san: result.san, fen: tempChess.fen() },
        ])
        return true
      } catch {
        return false
      }
    },
    [isInWhatIf, whatIfMoves, startingFen, displayedFen, effectiveIndex],
  )

  // Exit what-if mode
  const handleExitWhatIf = useCallback(() => {
    const branchIdx = whatIfBranchPoint
    setWhatIfMoves([])
    setWhatIfBranchPoint(-1)
    setCurrentIndex(branchIdx >= moves.length - 1 ? null : branchIdx)
  }, [whatIfBranchPoint, moves.length])

  return (
    <div className="analysis-board">
      <div className="analysis-board__layout">
        <div className="analysis-board__board-col">
          <Chessboard
            options={{
              position: displayedFen,
              boardOrientation,
              onPieceDrop: handleDrop,
              allowDragging: true,
              animationDurationInMs: 200,
              arrows,
              boardStyle: {
                borderRadius: '0',
                boxShadow: '0 20px 45px rgba(2, 6, 23, 0.5)',
              },
            }}
          />
        </div>
        <div className="analysis-board__moves-col">
          <MoveList
            moves={moveListMoves}
            currentIndex={moveListIndex}
            onNavigate={handleNavigate}
          />
        </div>
      </div>

      {!isInWhatIf && evals.length > 0 && (
        <AnalysisGraph
          evals={evals}
          currentIndex={currentIndex}
          onSelectMove={handleNavigate}
        />
      )}

      {isInWhatIf && (
        <div className="analysis-board__whatif-bar">
          <span>Exploring alternate line</span>
          <button type="button" onClick={handleExitWhatIf}>
            Exit
          </button>
        </div>
      )}

      {currentMove && !isInWhatIf && (
        <div className="analysis-board__position-info">
          <div className="analysis-board__position-info-row">
            {currentMove.eval_cp != null && (
              <span
                className={`analysis-board__eval-text ${currentMove.eval_cp >= 0 ? 'analysis-board__eval-text--positive' : 'analysis-board__eval-text--negative'}`}
              >
                {formatEvalCp(currentMove.eval_cp)}
              </span>
            )}
            {currentMove.classification && (
              <span
                className={`analysis-board__classification analysis-board__classification--${currentMove.classification}`}
              >
                {currentMove.classification}
              </span>
            )}
          </div>
          {currentMove.best_move_san &&
            currentMove.best_move_san !== currentMove.move_san && (
              <div className="analysis-board__position-info-row">
                <span className="analysis-board__played-label">
                  Played:{' '}
                  <strong className="analysis-board__played-move">
                    {currentMove.move_san}
                  </strong>
                </span>
                <span className="analysis-board__best-label">
                  Best:{' '}
                  <strong className="analysis-board__best-move">
                    {currentMove.best_move_san}
                  </strong>
                </span>
              </div>
            )}
        </div>
      )}
    </div>
  )
}

export default AnalysisBoard
