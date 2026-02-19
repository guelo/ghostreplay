import React, { useCallback, useEffect, useRef } from 'react'
import type { MoveClassification } from '../workers/analysisUtils'
import { ANNOTATION_SYMBOL } from '../workers/analysisUtils'

type Move = {
  san: string
  classification?: MoveClassification | null
  eval?: number | null // centipawns, white perspective
}

export type MoveListBubble = {
  moveIndex: number
  text: string
  variant: 'analyzing' | 'blunder' | 'inaccuracy' | 'good' | 'great' | 'best'
}

type MoveListProps = {
  moves: Move[]
  currentIndex: number | null // null means viewing latest position
  onNavigate: (index: number | null) => void
  canAddSelectedMove?: boolean
  isAddingSelectedMove?: boolean
  onAddSelectedMove?: (index: number) => void
  bubble?: MoveListBubble | null
}

const formatEval = (cp: number): string => {
  const value = cp / 100
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)}`
}

const classificationClass = (c?: MoveClassification | null): string => {
  if (!c) return ''
  return `move-${c}`
}

const MoveList = ({
  moves,
  currentIndex,
  onNavigate,
  canAddSelectedMove = false,
  isAddingSelectedMove = false,
  onAddSelectedMove,
  bubble = null,
}: MoveListProps) => {
  const moveListRef = useRef<HTMLDivElement>(null)
  const selectedMoveRef = useRef<HTMLButtonElement>(null)
  const bubbleRef = useRef<HTMLDivElement>(null)

  // Effective index for display purposes (null means at the end)
  const effectiveIndex = currentIndex ?? moves.length - 1

  const canGoBack = moves.length > 0 && effectiveIndex > -1
  const canGoForward = moves.length > 0 && effectiveIndex < moves.length - 1

  const handlePrev = useCallback(() => {
    if (!canGoBack) return
    onNavigate(effectiveIndex - 1) // -1 is valid (starting position)
  }, [canGoBack, effectiveIndex, onNavigate])

  const handleNext = useCallback(() => {
    if (!canGoForward) return
    const newIndex = effectiveIndex + 1
    // If we've reached the end, use null to indicate "live" position
    onNavigate(newIndex >= moves.length - 1 ? null : newIndex)
  }, [canGoForward, effectiveIndex, moves.length, onNavigate])

  const handleMoveClick = (index: number) => {
    // If clicking on the last move, set to null (live position)
    onNavigate(index === moves.length - 1 ? null : index)
  }

  const handleStartPosition = () => {
    if (moves.length > 0) {
      onNavigate(-1) // -1 indicates starting position (before any moves)
    }
  }

  // Keyboard navigation
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't handle if user is typing in an input
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      ) {
        return
      }

      if (e.key === 'ArrowLeft') {
        e.preventDefault()
        handlePrev()
      } else if (e.key === 'ArrowRight') {
        e.preventDefault()
        handleNext()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [handlePrev, handleNext])

  // Auto-scroll to selected move
  useEffect(() => {
    if (selectedMoveRef.current && moveListRef.current) {
      selectedMoveRef.current.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
      })
    }
  }, [effectiveIndex])

  // Auto-scroll bubble into view
  useEffect(() => {
    if (bubbleRef.current && moveListRef.current) {
      bubbleRef.current.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest',
      })
    }
  }, [bubble?.moveIndex, bubble?.text])

  // Group moves into pairs (white move, black move)
  const movePairs: { number: number; white: Move; black?: Move }[] = []
  for (let i = 0; i < moves.length; i += 2) {
    movePairs.push({
      number: Math.floor(i / 2) + 1,
      white: moves[i],
      black: moves[i + 1],
    })
  }

  const isAtStart = effectiveIndex === -1
  const isAtLatest = currentIndex === null
  const showAddButton =
    Boolean(onAddSelectedMove) &&
    moves.length > 0 &&
    effectiveIndex >= 0 &&
    canAddSelectedMove

  const renderMoveCell = (move: Move, index: number) => {
    const isSelected = index === effectiveIndex
    const annotation = move.classification ? ANNOTATION_SYMBOL[move.classification] : ''
    const colorClass = classificationClass(move.classification)

    return (
      <button
        ref={isSelected ? selectedMoveRef : null}
        className={`move-button ${colorClass} ${isSelected ? 'selected' : ''}`}
        type="button"
        onClick={() => handleMoveClick(index)}
      >
        <span className="move-annotation">{annotation}</span>
        <span className="move-san">{move.san}</span>
        <span className="move-eval">
          {move.eval != null ? formatEval(move.eval) : ''}
        </span>
      </button>
    )
  }

  return (
    <div className="move-list-container">
      <div className="move-list-header">
        <span className="move-list-title">Moves</span>
        <div className="move-list-actions">
          <span className={`move-list-viewing ${isAtLatest ? 'hidden' : ''}`}>
            Viewing position {effectiveIndex + 1}/{moves.length}
          </span>
          {showAddButton ? (
            <button
              className="move-list-add-button"
              type="button"
              onClick={() => {
                if (onAddSelectedMove && effectiveIndex >= 0) {
                  onAddSelectedMove(effectiveIndex)
                }
              }}
              disabled={isAddingSelectedMove}
              title="Add selected move to ghost library"
            >
              {isAddingSelectedMove ? 'Adding…' : 'Add to Ghost Library'}
            </button>
          ) : null}
        </div>
      </div>
      <div className="move-list-nav">
        <button
          className="move-nav-button"
          type="button"
          onClick={handleStartPosition}
          disabled={moves.length === 0 || isAtStart}
          title="Go to starting position"
        >
          ⟨⟨
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={handlePrev}
          disabled={!canGoBack}
          title="Previous move (←)"
        >
          ⟨
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={handleNext}
          disabled={!canGoForward}
          title="Next move (→)"
        >
          ⟩
        </button>
        <button
          className="move-nav-button"
          type="button"
          onClick={() => onNavigate(null)}
          disabled={isAtLatest}
          title="Go to current position"
        >
          ⟩⟩
        </button>
      </div>

      <div className="move-list-scroll" ref={moveListRef}>
        {moves.length === 0 ? (
          <p className="move-list-empty">No moves yet</p>
        ) : (
          <div className="move-list-grid">
            {movePairs.map((pair, pairIndex) => {
              const showBubble = bubble &&
                (bubble.moveIndex === pairIndex * 2 || bubble.moveIndex === pairIndex * 2 + 1)
              return (
                <React.Fragment key={pair.number}>
                  <div className="move-list-row">
                    <span className="move-number">{pair.number}.</span>
                    {renderMoveCell(pair.white, pairIndex * 2)}
                    {pair.black ? (
                      renderMoveCell(pair.black, pairIndex * 2 + 1)
                    ) : (
                      <span className="move-button-placeholder" />
                    )}
                  </div>
                  {showBubble && (
                    <div ref={bubbleRef} className={`move-bubble move-bubble--${bubble.variant}`}>
                      {bubble.text}
                    </div>
                  )}
                </React.Fragment>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}

export default MoveList
