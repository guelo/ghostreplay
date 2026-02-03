import { useCallback, useEffect, useRef } from 'react'

type Move = {
  san: string
}

type MoveListProps = {
  moves: Move[]
  currentIndex: number | null // null means viewing latest position
  onNavigate: (index: number | null) => void
}

const MoveList = ({ moves, currentIndex, onNavigate }: MoveListProps) => {
  const moveListRef = useRef<HTMLDivElement>(null)
  const selectedMoveRef = useRef<HTMLButtonElement>(null)

  // Effective index for display purposes (null means at the end)
  const effectiveIndex = currentIndex ?? moves.length - 1

  const canGoBack = moves.length > 0 && effectiveIndex > -1
  const canGoForward = moves.length > 0 && effectiveIndex < moves.length - 1

  const handlePrev = useCallback(() => {
    if (!canGoBack) return
    const newIndex = effectiveIndex - 1
    onNavigate(newIndex < 0 ? null : newIndex)
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

  return (
    <div className="move-list-container">
      <div className="move-list-header">
        <span className="move-list-title">Moves</span>
        {!isAtLatest && (
          <span className="move-list-viewing">
            Viewing position {effectiveIndex + 1}/{moves.length}
          </span>
        )}
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
            {movePairs.map((pair, pairIndex) => (
              <div key={pair.number} className="move-list-row">
                <span className="move-number">{pair.number}.</span>
                <button
                  ref={pairIndex * 2 === effectiveIndex ? selectedMoveRef : null}
                  className={`move-button ${pairIndex * 2 === effectiveIndex ? 'selected' : ''}`}
                  type="button"
                  onClick={() => handleMoveClick(pairIndex * 2)}
                >
                  {pair.white.san}
                </button>
                {pair.black && (
                  <button
                    ref={
                      pairIndex * 2 + 1 === effectiveIndex
                        ? selectedMoveRef
                        : null
                    }
                    className={`move-button ${pairIndex * 2 + 1 === effectiveIndex ? 'selected' : ''}`}
                    type="button"
                    onClick={() => handleMoveClick(pairIndex * 2 + 1)}
                  >
                    {pair.black.san}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

export default MoveList
