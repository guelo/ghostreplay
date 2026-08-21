import { Chess } from 'chess.js'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '../test/utils'
import { setMatchMedia } from '../test/setup'
import AnalysisBoard from './AnalysisBoard'
import { buildMainLineMoveDetails, computeBoardEvalIcon } from './AnalysisBoard.helpers'
import type { AnalysisMove } from '../utils/api'
import type { VariationTree, VarNode } from '../types/variationTree'
import { createEmptyTree } from '../types/variationTree'
import type { AddMoveParams } from '../hooks/useVariationTree'
import type { AnalysisResult } from '../hooks/useMoveAnalysis'
import type { EngineInfo } from '../workers/stockfishMessages'

// --- Mutable mock state for useVariationTree ---

const mockAddMove = vi.fn<(params: AddMoveParams) => string | null>(() => 'mock-node-id')
const mockSetSelectedVarNode = vi.fn()
const mockNavigateUp = vi.fn()
const mockNavigateDown = vi.fn(() => null)
const mockGetAbsolutePly = vi.fn(() => 0)
const mockGetVarAnalysis = vi.fn<(fen: string) => AnalysisResult | undefined>(() => undefined)
const mockRegisterPending = vi.fn()
const mockResolvePending = vi.fn()
const mockClearTree = vi.fn()
const mockPendingRequestsRef = { current: new Map<string, string>() }
// Mirror the real rejectPending: drop the pending entry so hasPendingForFen
// frees up (Finding F3).
const mockRejectPending = vi.fn((requestId: string) => {
  mockPendingRequestsRef.current.delete(requestId)
})

let mockTree: VariationTree = createEmptyTree()
let mockSelectedVarNodeId: string | null = null

vi.mock('../hooks/useVariationTree', () => ({
  useVariationTree: () => ({
    tree: mockTree,
    selectedVarNodeId: mockSelectedVarNodeId,
    setSelectedVarNode: mockSetSelectedVarNode,
    addMove: mockAddMove,
    navigateUp: mockNavigateUp,
    navigateDown: mockNavigateDown,
    getAbsolutePly: mockGetAbsolutePly,
    getVarAnalysis: mockGetVarAnalysis,
    registerPending: mockRegisterPending,
    resolvePending: mockResolvePending,
    rejectPending: mockRejectPending,
    clearTree: mockClearTree,
    pendingRequestsRef: mockPendingRequestsRef,
    varAnalysisCacheRef: { current: new Map() },
    collectBranchNodes: vi.fn(() => []),
  }),
}))

const mockAnalyzeMove = vi.fn(() => 'req-123')
const {
  mockEngineInfoRef,
  mockEngineInfoFenRef,
  mockEngineThinkingRef,
  mockEvaluatePosition,
  mockStopSearch,
  mockUseStockfishEngine,
} = vi.hoisted(() => {
  const mockEngineInfoRef = { current: [] as EngineInfo[] }
  const mockEngineInfoFenRef = { current: null as string | null }
  const mockEngineThinkingRef = { current: false }
  // Resolve a completed-root snapshot so AnalysisBoard's completion callback (which
  // feeds the evidence reuse layer) fires. The snapshot content is irrelevant here
  // because the reuse layer is mocked; the reuse gate itself is tested in
  // analysisEvidence.test.tsx.
  const mockEvaluatePosition = vi.fn(async () => ({
    move: '',
    raw: '',
    snapshot: {
      requestId: 'r',
      fen: '',
      bestMove: '',
      lines: [],
      limit: { type: 'depth' as const, value: 21 },
      multipv: 3,
      searchmoves: null,
    },
  }))
  const mockStopSearch = vi.fn()
  const mockUseStockfishEngine = vi.fn((_options?: { enabled?: boolean }) => ({
    info: mockEngineInfoRef.current,
    infoFen: mockEngineInfoFenRef.current,
    isThinking: mockEngineThinkingRef.current,
    evaluatePosition: mockEvaluatePosition,
    stopSearch: mockStopSearch,
  }))

  return {
    mockEngineInfoRef,
    mockEngineInfoFenRef,
    mockEngineThinkingRef,
    mockEvaluatePosition,
    mockStopSearch,
    mockUseStockfishEngine,
  }
})

// Captures the onVariationError callback AnalysisBoard passes in, so a test can
// drive the failure channel (Finding F3).
let capturedOnVariationError: ((id: string) => void) | undefined
vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: (_store: unknown, onVariationError?: (id: string) => void) => {
    capturedOnVariationError = onVariationError
    return {
      analyzeMove: mockAnalyzeMove,
      analysisMap: new Map(),
      lastAnalysis: null,
      clearAnalysis: vi.fn(),
    }
  },
}))

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: mockUseStockfishEngine,
}))

// Evidence reuse layer (g-reuse-d21-search): spy on considerCompletedSearch so the
// context AnalysisBoard passes on visible-search completion can be asserted without
// a real search. Capture the onAcceptedEvidence callback (g-xox0 Part B) so a test
// can drive the live-overlay path directly. Also capture the sessionId the hook was
// initialized with.
const {
  mockConsiderCompletedSearch,
  capturedOnAcceptedRef,
  capturedSessionIdRef,
} = vi.hoisted(() => ({
  mockConsiderCompletedSearch: vi.fn(),
  capturedOnAcceptedRef: { current: undefined as unknown },
  capturedSessionIdRef: { current: undefined as unknown },
}))
vi.mock('../services/analysisEvidence', () => ({
  useAnalysisEvidence: (sessionId: unknown, onAccepted: unknown) => {
    capturedOnAcceptedRef.current = onAccepted
    capturedSessionIdRef.current = sessionId
    return {
      considerCompletedSearch: mockConsiderCompletedSearch,
    }
  },
}))

// --- Prop-capturing mocks ---

let capturedChessboardProps: Record<string, unknown> = {}
const capturedChessboardRenders: Array<{ kind: 'main' | 'preview'; options: Record<string, unknown> }> = []

vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    const kind = options.allowDragging === false ? 'preview' : 'main'
    capturedChessboardRenders.push({ kind, options })
    if (kind === 'main') {
      capturedChessboardProps = options
    }
    return <div data-testid={`${kind}-chessboard`} />
  },
  defaultArrowOptions: {
    color: '#ffaa00',
    secondaryColor: '#4caf50',
    tertiaryColor: '#f44336',
    arrowLengthReducerDenominator: 8,
    sameTargetArrowLengthReducerDenominator: 4,
    arrowWidthDenominator: 5,
    activeArrowWidthMultiplier: 0.9,
    opacity: 0.65,
    activeOpacity: 0.5,
  },
}))

let capturedEvalBarProps: Record<string, unknown> = {}

vi.mock('./EvalBar', () => ({
  default: (props: Record<string, unknown>) => {
    capturedEvalBarProps = props
    return <div data-testid="eval-bar" />
  },
}))

let capturedGraphProps: Record<string, unknown> = {}

vi.mock('./AnalysisGraph', () => ({
  default: (props: Record<string, unknown>) => {
    capturedGraphProps = props
    return <div data-testid="analysis-graph" />
  },
}))

let capturedMoveListProps: Record<string, unknown> = {}
// Arrow keys that actually reached the (mocked) main move list's window
// listener — i.e. were NOT intercepted by the board's capture-phase handler.
const capturedMoveListKeys: string[] = []

vi.mock('./MoveList', async () => {
  const { useEffect } = await import('react')
  return {
    default: function MockMoveList(props: {
      moves: Array<{ san: string }>
      onNavigate: (index: number | null) => void
      playerColor?: 'white' | 'black'
      suppressKeyboardNavigation?: boolean
      [key: string]: unknown
    }) {
      capturedMoveListProps = props
      // Mirror the real MoveList contract: a window keydown listener active only
      // while not suppressed. A regressed capture handler that stole arrows in
      // hover mode would stopPropagation before this bubble listener fires.
      useEffect(() => {
        const handler = (event: KeyboardEvent) => {
          if (props.suppressKeyboardNavigation) return
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            capturedMoveListKeys.push(event.key)
          }
        }
        window.addEventListener('keydown', handler)
        return () => window.removeEventListener('keydown', handler)
      }, [props.suppressKeyboardNavigation])
      return (
        <div>
          <div data-testid="move-list-player-color">{props.playerColor ?? 'unset'}</div>
          {props.moves.map((move, index) => (
            <button
              key={`${move.san}-${index}`}
              type="button"
              onClick={() => props.onNavigate(index)}
            >
              Move {index + 1}
            </button>
          ))}
          <button type="button" onClick={() => props.onNavigate(null)}>
            Latest
          </button>
        </div>
      )
    },
  }
})

vi.mock('./HorizontalMoveList', () => ({
  default: (props: { playerColor?: 'white' | 'black'; [key: string]: unknown }) => {
    capturedMoveListProps = props
    return <div data-testid="h-move-list" />
  },
}))

const capturedMaterialDisplays: Array<{
  fen: string
  perspective: string
  label?: string
}> = []

vi.mock('./MaterialDisplay', () => ({
  default: (props: { fen: string; perspective: string; label?: string }) => {
    capturedMaterialDisplays.push(props)
    return <div data-testid={`material-display-${props.perspective}`} data-fen={props.fen} />
  },
}))

const moves: AnalysisMove[] = [
  {
    move_number: 1,
    color: 'white',
    move_san: 'e4',
    fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1',
    eval_cp: 30,
    eval_mate: null,
    best_move_san: 'e4',
    best_move_eval_cp: 30,
    eval_delta: 0,
    classification: 'best',
  },
  {
    move_number: 1,
    color: 'black',
    move_san: 'c5',
    fen_after: 'rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
    eval_cp: -120,
    eval_mate: null,
    best_move_san: 'e5',
    best_move_eval_cp: -20,
    eval_delta: 100,
    classification: 'inaccuracy',
  },
]

beforeEach(() => {
  capturedChessboardProps = {}
  capturedChessboardRenders.length = 0
  capturedEvalBarProps = {}
  capturedGraphProps = {}
  capturedMoveListProps = {}
  capturedMoveListKeys.length = 0
  capturedMaterialDisplays.length = 0
  mockTree = createEmptyTree()
  mockSelectedVarNodeId = null
  mockEngineInfoRef.current = []
  mockEngineInfoFenRef.current = null
  mockEngineThinkingRef.current = false
  mockPendingRequestsRef.current.clear()
  vi.clearAllMocks()
})

describe('AnalysisBoard — MaterialDisplays', () => {
  it('renders two material displays with correct perspectives', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const displays = screen.getAllByTestId(/material-display-/)
    expect(displays).toHaveLength(2)

    expect(capturedMaterialDisplays[0].perspective).toBe('black')
    expect(capturedMaterialDisplays[1].perspective).toBe('white')
  })

  it('keeps both material displays in the moves column when a mobile toolbar is supplied', () => {
    setMatchMedia('(max-width: 720px)', true)
    const { container } = render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        mobileToolbar={<div data-testid="mobile-toolbar-content">picker</div>}
      />,
    )

    const toolbar = screen.getByTestId('mobile-toolbar-content').parentElement?.parentElement
    expect(toolbar).toHaveClass('analysis-board__mobile-toolbar')
    expect(toolbar?.querySelector('[data-testid^="material-display-"]')).toBeNull()

    // Both sit in the moves column, where the narrow layout puts them on the
    // engine-toggle row.
    const movesCol = container.querySelector('.analysis-board__moves-col')
    expect(movesCol?.querySelectorAll('[data-testid^="material-display-"]')).toHaveLength(2)
    expect(screen.getAllByTestId(/material-display-/)).toHaveLength(2)
  })

  it('labels the displays You / Ghost, following the player rather than the colour', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    // Playing black: the white-perspective display is the ghost's.
    expect(capturedMaterialDisplays).toEqual([
      expect.objectContaining({ perspective: 'white', label: 'Ghost:' }),
      expect.objectContaining({ perspective: 'black', label: 'You:' }),
    ])
  })

  it('passes displayedFen to both displays for latest move', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    // latest move FEN
    expect(capturedMaterialDisplays[0].fen).toBe(moves[1].fen_after)
    expect(capturedMaterialDisplays[1].fen).toBe(moves[1].fen_after)
  })

  it('passes displayedFen to both displays when navigating to main line move', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // click Move 1
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    const lastRenderedDisplays = capturedMaterialDisplays.slice(-2)
    expect(lastRenderedDisplays[0].fen).toBe(moves[0].fen_after)
    expect(lastRenderedDisplays[1].fen).toBe(moves[0].fen_after)
  })

  it('passes displayedFen to both displays when selecting a variation node', () => {
    const node: VarNode = {
      id: 'var-1',
      san: 'Bc4',
      fen: 'rnbqkbnr/pp1ppppp/8/2p5/2B1P3/8/PPPP1PPP/RNBQKNR b KQkq - 1 2',
      fenBefore: moves[0].fen_after,
      uci: 'f1c4',
      parentId: null,
      parentGameIndex: 1,
      branchPlyOffset: 0,
      children: [],
      nestingLevel: 0,
    }
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedMaterialDisplays[0].fen).toBe(node.fen)
    expect(capturedMaterialDisplays[1].fen).toBe(node.fen)
  })
})

describe('AnalysisBoard — what-if graph', () => {
  const makeNode = (overrides: Partial<VarNode>): VarNode => ({
    id: 'var-1',
    san: 'Bc4',
    fen: 'rnbqkbnr/pp1ppppp/8/2p5/2B1P3/8/PPPP1PPP/RNBQKNR b KQkq - 1 2',
    fenBefore: moves[0].fen_after,
    uci: 'f1c4',
    parentId: null,
    parentGameIndex: 1,
    branchPlyOffset: 0,
    children: [],
    nestingLevel: 0,
    ...overrides,
  })

  it('keeps the graph and footer mounted in what-if mode', () => {
    const node = makeNode({})
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        footer={<div data-testid="footer-stats">stats</div>}
      />,
    )

    expect(screen.getByTestId('analysis-graph')).toBeTruthy()
    expect(screen.getByTestId('footer-stats')).toBeTruthy()
  })

  // The board wash-out (AnalysisBoard.css:
  // .analysis-board__board-frame--variation [data-square]) hangs off this
  // class, so guard that it toggles with the variation state. The filter itself
  // can't be asserted in jsdom.
  it('marks the board frame as a variation only while in what-if mode', () => {
    const node = makeNode({})
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'

    const { container } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(
      container.querySelector('.analysis-board__board-frame--variation'),
    ).toBeTruthy()
  })

  it('does not mark the board frame as a variation on the main line', () => {
    // beforeEach resets mockSelectedVarNodeId to null (main line).
    const { container } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const frame = container.querySelector('.analysis-board__board-frame')
    expect(frame).toBeTruthy()
    expect(frame?.classList.contains('analysis-board__board-frame--variation')).toBe(false)
  })

  // g-kepv is fixed in CSS (a board-sized backdrop-filter wash that no longer makes
  // each square a stacking context), so the board must KEEP animating in what-if
  // mode — showAnimations must not be disabled, animation stays 200ms.
  it('keeps board animations enabled in what-if/variation mode (g-kepv)', () => {
    const node = makeNode({})
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedChessboardProps.showAnimations).not.toBe(false)
    expect(capturedChessboardProps.animationDurationInMs).toBe(200)
  })

  it('builds a variationLine with an anchor at the departure move and a pending tip', () => {
    const node = makeNode({})
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'
    mockGetAbsolutePly.mockReturnValue(2)

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const variationLine = capturedGraphProps.variationLine as {
      anchor: { index: number } | null
      points: Array<{ index: number; pending: boolean }>
      streaming: unknown
    }
    expect(variationLine.anchor).toEqual({ index: 1, cp: expect.any(Number) })
    expect(variationLine.points).toEqual([{ index: 2, cp: 0, pending: true }])
    // Red indicator sits at the selected ply even though it is unanalysed
    expect(capturedGraphProps.currentIndex).toBe(2)
  })

  it('omits the anchor when the variation departs the starting position', () => {
    const node = makeNode({ parentGameIndex: -1 })
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[-1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'
    mockGetAbsolutePly.mockReturnValue(0)

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const variationLine = capturedGraphProps.variationLine as {
      anchor: unknown
      points: Array<{ index: number }>
    }
    expect(variationLine.anchor).toBeNull()
    expect(variationLine.points[0].index).toBe(0)
  })

  it('truncates the variationLine to the selected ancestor when stepping back', () => {
    const parent = makeNode({ id: 'var-1', children: ['var-2'] })
    const child = makeNode({
      id: 'var-2',
      san: 'Nf6',
      parentId: 'var-1',
      parentGameIndex: 1,
    })
    mockTree = {
      nodes: new Map([['var-1', parent], ['var-2', child]]),
      rootBranches: new Map([[1, ['var-1']]]),
    }
    // Select the parent, not the deeper child
    mockSelectedVarNodeId = 'var-1'
    mockGetAbsolutePly.mockReturnValue(2)

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const variationLine = capturedGraphProps.variationLine as {
      points: Array<{ index: number }>
    }
    // Only the selected ancestor is plotted — the deeper child is excluded
    expect(variationLine.points).toHaveLength(1)
    expect(variationLine.points[0].index).toBe(2)
  })
})

describe('AnalysisBoard — variation failure channel (Finding F3)', () => {
  // 17d. A scoped variation error calls onVariationError → rejectPending, after
  // which hasPendingForFen(fen) is false and the FEN can be re-requested.
  it('wires onVariationError to rejectPending so a failed FEN frees up', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // A variation request is in flight for this FEN.
    const fen = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
    mockPendingRequestsRef.current.set('req-var', fen)
    const hasPendingForFen = () => {
      for (const v of mockPendingRequestsRef.current.values()) {
        if (v === fen) return true
      }
      return false
    }
    expect(hasPendingForFen()).toBe(true)

    // useMoveAnalysis received the failure callback.
    expect(capturedOnVariationError).toBeTypeOf('function')

    // Simulate a scoped variation error firing the callback.
    capturedOnVariationError?.('req-var')

    expect(mockRejectPending).toHaveBeenCalledWith('req-var')
    expect(hasPendingForFen()).toBe(false)
  })
})

describe('AnalysisBoard MoveList', () => {
  it('passes player color to MoveList from board orientation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    expect(screen.getByTestId('move-list-player-color')).toHaveTextContent('black')
  })

  it('renders HorizontalMoveList below the analysis 720px breakpoint', () => {
    setMatchMedia('(max-width: 720px)', true)
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    expect(screen.getByTestId('h-move-list')).toBeTruthy()
    expect(screen.queryByTestId('move-list-player-color')).toBeNull()
  })

  it('renders the vertical MoveList above the 720px breakpoint', () => {
    setMatchMedia('(max-width: 720px)', false)
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    expect(screen.getByTestId('move-list-player-color')).toBeTruthy()
    expect(screen.queryByTestId('h-move-list')).toBeNull()
  })

  it('initializes to initialMoveIndex when provided', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" initialMoveIndex={0} />)

    expect(capturedChessboardProps.position).toBe(moves[0].fen_after)
    expect(capturedMoveListProps.currentIndex).toBe(0)
  })

  it('highlights main-line moves and arrows from cached move details', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const styles = capturedChessboardProps.squareStyles as Record<string, unknown>
    expect(styles).toHaveProperty('c7')
    expect(styles).toHaveProperty('c5')

    const arrows = capturedChessboardProps.arrows as Array<{ startSquare: string; endSquare: string; color: string }>
    expect(arrows).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ startSquare: 'c7', endSquare: 'c5' }),
        expect.objectContaining({ startSquare: 'e7', endSquare: 'e5' }),
      ]),
    )
  })

  it('disables the Stockfish hook and stops search when engine lines are turned off', async () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(mockUseStockfishEngine).toHaveBeenLastCalledWith({ enabled: true })

    fireEvent.click(screen.getByLabelText('Engine lines'))

    await waitFor(() => {
      expect(mockUseStockfishEngine).toHaveBeenLastCalledWith({ enabled: false })
    })
    expect(mockStopSearch).toHaveBeenCalled()
  })

  it('does not request new engine evaluations while engine lines are disabled', async () => {
    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[0].fen_after]: {
            best_move_uci: 'g8f6',
            best_move_san: 'Nf6',
            best_move_eval_cp: 20,
            position_trusted: true,
          },
        }}
      />,
    )

    await waitFor(() => {
      expect(mockEvaluatePosition).toHaveBeenCalled()
    })

    mockEvaluatePosition.mockClear()
    fireEvent.click(screen.getByLabelText('Engine lines'))
    mockStopSearch.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    expect(mockEvaluatePosition).not.toHaveBeenCalled()
    expect(mockStopSearch).not.toHaveBeenCalled()
  })

  it('debounces engine evaluations while navigating through the move list', () => {
    vi.useFakeTimers()
    try {
      render(<AnalysisBoard moves={moves} boardOrientation="white" />)

      act(() => {
        vi.advanceTimersByTime(119)
      })
      expect(mockEvaluatePosition).not.toHaveBeenCalled()

      fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))
      fireEvent.click(screen.getByRole('button', { name: 'Latest' }))

      act(() => {
        vi.advanceTimersByTime(119)
      })
      expect(mockEvaluatePosition).not.toHaveBeenCalled()

      act(() => {
        vi.advanceTimersByTime(1)
      })

      expect(mockEvaluatePosition).toHaveBeenCalledTimes(1)
      expect(mockEvaluatePosition).toHaveBeenCalledWith(
        moves[1].fen_after,
        { depth: 21, multipv: 3 },
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('hides engine-only display data when engine lines are disabled', async () => {
    mockEngineInfoRef.current = [
      {
        pv: ['g1f3'],
        score: { type: 'cp', value: 30 },
        depth: 12,
      },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    await waitFor(() => {
      expect(screen.getByText('d12')).toBeInTheDocument()
    })
    expect(screen.getByRole('progressbar', { name: 'Engine analysis depth' })).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Engine lines'))

    expect(screen.queryByText('d12')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar', { name: 'Engine analysis depth' })).not.toBeInTheDocument()
    const arrows = capturedChessboardProps.arrows as
      | Array<{ startSquare: string; endSquare: string }>
      | undefined
    expect(
      arrows?.some((arrow) => arrow.startSquare === 'g1' && arrow.endSquare === 'f3'),
    ).not.toBe(true)
  })

  it('hides stale engine depth immediately after navigating to another position', async () => {
    mockEngineInfoRef.current = [
      {
        pv: ['g1f3'],
        score: { type: 'cp', value: 30 },
        depth: 12,
      },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    await waitFor(() => {
      expect(screen.getByText('d12')).toBeInTheDocument()
    })
    expect(screen.getByRole('progressbar', { name: 'Engine analysis depth' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    expect(screen.queryByText('d12')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar', { name: 'Engine analysis depth' })).not.toBeInTheDocument()
  })

  it('renders engine depth as capped determinate progress while keeping the raw label', async () => {
    mockEngineInfoRef.current = [
      {
        pv: ['g1f3'],
        score: { type: 'cp', value: 30 },
        depth: 12,
      },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const progressbar = await screen.findByRole('progressbar', { name: 'Engine analysis depth' })
    expect(progressbar).toHaveAttribute('aria-valuenow', '12')
    expect(progressbar).toHaveAttribute('aria-valuemax', '21')
    expect(screen.getByText('d12')).toBeInTheDocument()
    expect(progressbar.firstElementChild).toHaveStyle({ width: '57.14285714285714%' })
  })

  it('caps over-depth engine progress without changing the visible depth label', async () => {
    mockEngineInfoRef.current = [
      {
        pv: ['g1f3'],
        score: { type: 'cp', value: 30 },
        depth: 24,
      },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const progressbar = await screen.findByRole('progressbar', { name: 'Engine analysis depth' })
    expect(progressbar).toHaveAttribute('aria-valuenow', '21')
    expect(progressbar).toHaveAttribute('aria-valuemax', '21')
    expect(screen.getByText('d24')).toBeInTheDocument()
    expect(progressbar.firstElementChild).toHaveStyle({ width: '100%' })
  })

  it('animates engine progress only while Stockfish is thinking', async () => {
    mockEngineInfoRef.current = [
      {
        pv: ['g1f3'],
        score: { type: 'cp', value: 30 },
        depth: 12,
      },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after
    mockEngineThinkingRef.current = true
    const { rerender } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    let progressbar = await screen.findByRole('progressbar', { name: 'Engine analysis depth' })
    expect(progressbar.firstElementChild).toHaveClass('analysis-board__engine-progress-fill--thinking')

    mockEngineThinkingRef.current = false
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)

    progressbar = screen.getByRole('progressbar', { name: 'Engine analysis depth' })
    expect(progressbar.firstElementChild).not.toHaveClass('analysis-board__engine-progress-fill--thinking')
  })

  it('keeps the depth badge in the DOM at depth 0 so the panel width never changes', async () => {
    // The badge is width-reserved in CSS; rendering it conditionally would still
    // let the panel jump the moment the first depth lands.
    const { container, rerender } = render(
      <AnalysisBoard moves={moves} boardOrientation="white" />,
    )

    const badge = container.querySelector('.analysis-board__engine-depth')
    expect(badge).not.toBeNull()
    expect(badge).toBeEmptyDOMElement()

    mockEngineInfoRef.current = [
      { pv: ['g1f3'], score: { type: 'cp', value: 30 }, depth: 12 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)

    await screen.findByText('d12')
    expect(container.querySelectorAll('.analysis-board__engine-depth')).toHaveLength(1)
  })

  it('collapses the engine panel to just the toggle below the 720px breakpoint', () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 30 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    setMatchMedia('(max-width: 720px)', true)

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.getByLabelText('Engine lines')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show engine line 1' })).toBeNull()
  })

  it('opens an engine-line popup with the full SAN PV and preview after the first move', async () => {
    const pv = ['g1f3', 'd7d6', 'd2d4']
    mockEngineInfoRef.current = [{ pv, score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const chess = new Chess(moves[1].fen_after)
    chess.move({ from: 'g1', to: 'f3' })
    const firstFen = chess.fen()

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))

    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })
    expect(dialog).toHaveTextContent('Nf3')
    expect(dialog).toHaveTextContent('d6')
    expect(dialog).toHaveTextContent('d4')
    const preview = capturedChessboardRenders.find((rendered) => rendered.kind === 'preview')
    expect(preview?.options.position).toBe(firstFen)
    expect(capturedChessboardProps.id).toMatch(/^analysis-board-/)
    expect(preview?.options.id).toMatch(/^analysis-board-/)
    expect(preview?.options.id).not.toBe(capturedChessboardProps.id)
    expect(preview?.options.animationDurationInMs).toBe(0)
    expect(capturedMoveListProps.suppressKeyboardNavigation).toBe(true)
  })

  it('annotates the PV with move numbers from a white-to-move FEN', async () => {
    const pv = ['g1f3', 'd7d6', 'd2d4']
    mockEngineInfoRef.current = [{ pv, score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after // white to move, fullmove 2

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })

    // White moves are prefixed with "<n>.", black follow-up moves have no prefix.
    expect(dialog).toHaveTextContent('2.Nf3')
    expect(dialog).toHaveTextContent('3.d4')
    // Clicking a numbered token still updates the preview board for that ply.
    const chess = new Chess(moves[1].fen_after)
    chess.move({ from: 'g1', to: 'f3' })
    chess.move({ from: 'd7', to: 'd6' })
    chess.move({ from: 'd2', to: 'd4' })
    const thirdFen = chess.fen()
    fireEvent.click(screen.getByRole('button', { name: 'd4' }))
    await waitFor(() => {
      const lastPreview = capturedChessboardRenders.filter((r) => r.kind === 'preview').at(-1)
      expect(lastPreview?.options.position).toBe(thirdFen)
    })
  })

  it('annotates a black-to-move starting PV with ellipsis notation', async () => {
    const pv = ['g8f6', 'd2d4']
    mockEngineInfoRef.current = [{ pv, score: { type: 'cp', value: 12 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[0].fen_after // black to move, fullmove 1

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    // Navigate to the black-to-move position so the engine line is shown there.
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Show engine line 1' }))
    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })

    expect(dialog).toHaveTextContent('1...Nf6')
    expect(dialog).toHaveTextContent('2.d4')
  })

  it('renders the cached best line popup with its full stored PV continuation', async () => {
    // Restricted live search returns a non-best line; the cached best line is
    // merged in as slot 1 and must replay its full stored PV, not one move.
    mockEngineInfoRef.current = [
      { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 10 }, depth: 18, multipv: 2 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[1].fen_after]: {
            best_move_uci: 'g1f3',
            best_move_san: 'Nf3',
            best_move_eval_cp: 42,
            best_line_uci: ['g1f3', 'd7d6', 'd2d4'],
            position_trusted: true,
          },
        }}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))

    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })
    expect(dialog).toHaveTextContent('Nf3')
    expect(dialog).toHaveTextContent('d6')
    expect(dialog).toHaveTextContent('d4')
  })

  it('requests a restricted search that excludes the cached best move', () => {
    vi.useFakeTimers()
    try {
      render(
        <AnalysisBoard
          moves={moves}
          boardOrientation="white"
          positionAnalysis={{
            [moves[1].fen_after]: {
              best_move_uci: 'g1f3',
              best_move_san: 'Nf3',
              best_move_eval_cp: 42,
              best_line_uci: ['g1f3', 'd7d6', 'd2d4'],
              position_trusted: true,
            },
          }}
        />,
      )

      act(() => {
        vi.advanceTimersByTime(120)
      })

      expect(mockEvaluatePosition).toHaveBeenCalledWith(
        moves[1].fen_after,
        expect.objectContaining({
          depth: 21,
          multipv: 2,
          searchmoves: expect.not.arrayContaining(['g1f3']),
        }),
      )
      // The restricted search must still offer the other legal replies.
      const calls = mockEvaluatePosition.mock.calls as unknown as Array<
        [string, { searchmoves?: string[] }]
      >
      const call = calls.find(([fen]) => fen === moves[1].fen_after)
      expect(call?.[1].searchmoves?.length).toBeGreaterThan(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('falls back to a single-move cached best line when no stored PV is present', async () => {
    mockEngineInfoRef.current = [
      { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 10 }, depth: 18, multipv: 2 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[1].fen_after]: {
            best_move_uci: 'g1f3',
            best_move_san: 'Nf3',
            best_move_eval_cp: 42,
            position_trusted: true,
          },
        }}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))

    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })
    expect(dialog).toHaveTextContent('Nf3')
    expect(dialog).not.toHaveTextContent('d6')
  })

  it('runs a full multipv search for an untrusted cached seed instead of pinning it as line 1 (g-54h5)', () => {
    vi.useFakeTimers()
    try {
      render(
        <AnalysisBoard
          moves={moves}
          boardOrientation="white"
          positionAnalysis={{
            [moves[1].fen_after]: {
              best_move_uci: 'g1f3',
              best_move_san: 'Nf3',
              best_move_eval_cp: 42,
              position_trusted: false,
            },
          }}
        />,
      )

      act(() => {
        vi.advanceTimersByTime(120)
      })

      // No searchmoves restriction — the untrusted best move is NOT excluded, so
      // the live engine ranks every move at one depth (inverse of the restricted
      // search test above).
      expect(mockEvaluatePosition).toHaveBeenCalledWith(
        moves[1].fen_after,
        { depth: 21, multipv: 3 },
      )
      const calls = mockEvaluatePosition.mock.calls as unknown as Array<
        [string, { searchmoves?: string[] }]
      >
      const call = calls.find(([fen]) => fen === moves[1].fen_after)
      expect(call?.[1].searchmoves).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('re-ranks merged lines so a higher live line becomes line 1 and the canonical marker follows it (g-54h5)', () => {
    // Cached canonical best is Nf3 with a LOW stored eval; the live restricted
    // search returns Nc3 scoring higher. Pre-fix this pinned Nf3 (cached) as line
    // 1 with the better Nc3 below it — the bead's "line 2 better than line 1".
    mockEngineInfoRef.current = [
      { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 50 }, depth: 21, multipv: 2 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[1].fen_after]: {
            best_move_uci: 'g1f3',
            best_move_san: 'Nf3',
            best_move_eval_cp: 10,
            best_line_uci: ['g1f3', 'd7d6'],
            position_trusted: true,
          },
        }}
      />,
    )

    const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
    const line2 = screen.getByRole('button', { name: 'Show engine line 2' })

    // Line 1 is now the live Nc3 line (higher eval), with NO canonical marker.
    expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nc3')
    expect(line1.querySelector('.analysis-board__engine-eval')).toHaveTextContent('+0.5')
    expect(line1.querySelector('.analysis-board__engine-source')).toBeNull()

    // Line 2 is the cached canonical Nf3 line — the marker moved slots with it.
    expect(line2.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nf3')
    expect(line2.querySelector('.analysis-board__engine-eval')).toHaveTextContent('+0.1')
    expect(line2.querySelector('.analysis-board__engine-source')).not.toBeNull()

    // Downstream consumer follows: the solid-blue best-move arrow now points to
    // the live best (Nc3 = b1->c3), not the demoted cached move.
    const arrows = capturedChessboardProps.arrows as Array<{
      startSquare: string
      endSquare: string
      color: string
    }>
    const bestArrow = arrows.find((a) => a.color === 'rgba(59, 130, 246, 1.00)')
    expect(bestArrow).toEqual(
      expect.objectContaining({ startSquare: 'b1', endSquare: 'c3' }),
    )
  })

  it('skip-optimizes a trusted mate-only winner and renders it as the canonical line 1 with a mate eval (g-jsac)', () => {
    // Mate-only trusted winner: best_move_eval_cp is null but best_move_eval_mate
    // is set. The widened trust gate must still drive the restricted search and
    // prepend the canonical Nf3 line, which (mate >> cp via mateToCp) outranks the
    // live Nc3 cp line and sorts to line 1 with the canonical marker + an Mn eval.
    vi.useFakeTimers()
    try {
      mockEngineInfoRef.current = [
        { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 50 }, depth: 21, multipv: 2 },
      ]
      mockEngineInfoFenRef.current = moves[1].fen_after

      render(
        <AnalysisBoard
          moves={moves}
          boardOrientation="white"
          positionAnalysis={{
            [moves[1].fen_after]: {
              best_move_uci: 'g1f3',
              best_move_san: 'Nf3',
              best_move_eval_cp: null,
              best_move_eval_mate: 3,
              best_line_uci: ['g1f3', 'd7d6'],
              position_trusted: true,
            },
          }}
        />,
      )

      act(() => {
        vi.advanceTimersByTime(120)
      })

      // Restricted search: the canonical best move Nf3 is excluded from searchmoves.
      const calls = mockEvaluatePosition.mock.calls as unknown as Array<
        [string, { multipv?: number; searchmoves?: string[] }]
      >
      const call = calls.find(([fen]) => fen === moves[1].fen_after)
      expect(call?.[1].multipv).toBe(2)
      expect(call?.[1].searchmoves).not.toContain('g1f3')
      expect(call?.[1].searchmoves?.length).toBeGreaterThan(0)

      // Line 1 is the cached canonical Nf3 line with an M3 eval + canonical marker.
      const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
      expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nf3')
      expect(line1.querySelector('.analysis-board__engine-eval')).toHaveTextContent('M3')
      expect(line1.querySelector('.analysis-board__engine-source')).not.toBeNull()

      // Line 2 is the live Nc3 cp line, below the mate.
      const line2 = screen.getByRole('button', { name: 'Show engine line 2' })
      expect(line2.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nc3')
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders the mate eval when a trusted winner carries both cp and mate (mate-first, g-jsac)', () => {
    // A superset merge of disagreeing runs can deliver both best_move_eval_cp and
    // best_move_eval_mate. The panel treats mate as authoritative (mate-first,
    // matching backend tree_eval._best_move_eval) — the canonical line shows M3.
    mockEngineInfoRef.current = [
      { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 50 }, depth: 21, multipv: 2 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[1].fen_after]: {
            best_move_uci: 'g1f3',
            best_move_san: 'Nf3',
            best_move_eval_cp: 35,
            best_move_eval_mate: 3,
            best_line_uci: ['g1f3', 'd7d6'],
            position_trusted: true,
          },
        }}
      />,
    )

    const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
    expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nf3')
    // mate wins over the +0.35 cp, and (mateToCp) outranks the live +0.5 cp line.
    expect(line1.querySelector('.analysis-board__engine-eval')).toHaveTextContent('M3')
    expect(line1.querySelector('.analysis-board__engine-eval')).not.toHaveTextContent('+0.3')
    expect(line1.querySelector('.analysis-board__engine-source')).not.toBeNull()
  })

  it('survives sparse streaming MultiPV output when merging the cached line (g-54h5)', () => {
    // useStockfishEngine fills slots by multipv index, so line 2 can arrive
    // before line 1 — a sparse [<hole>, line2]. The merge spread materializes the
    // hole as `undefined`; the re-rank must drop it instead of dereferencing it.
    const sparseLines: EngineInfo[] = []
    sparseLines[1] = { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 50 }, depth: 21, multipv: 2 }
    mockEngineInfoRef.current = sparseLines
    mockEngineInfoFenRef.current = moves[1].fen_after

    expect(() =>
      render(
        <AnalysisBoard
          moves={moves}
          boardOrientation="white"
          positionAnalysis={{
            [moves[1].fen_after]: {
              best_move_uci: 'g1f3',
              best_move_san: 'Nf3',
              best_move_eval_cp: 10,
              best_line_uci: ['g1f3', 'd7d6'],
              position_trusted: true,
            },
          }}
        />,
      ),
    ).not.toThrow()

    // The hole is dropped: only the two real lines render, re-ranked by eval
    // (live Nc3 +0.5 above the cached canonical Nf3 +0.1).
    const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
    const line2 = screen.getByRole('button', { name: 'Show engine line 2' })
    expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nc3')
    expect(line2.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nf3')
    expect(line2.querySelector('.analysis-board__engine-source')).not.toBeNull()
    expect(screen.queryByRole('button', { name: 'Show engine line 3' })).toBeNull()
  })

  it('does not mark a live line canonical when the trusted best is the only legal move (g-54h5)', () => {
    // Only one legal move (Kg7) → searchmoves is empty → undefined → the restricted
    // merge never runs, so the displayed line is LIVE depth-21 output even though
    // its first move matches trustedBest. The canonical marker must stay off it.
    const oneMoveFen = 'R6k/7p/8/8/8/8/8/7K b - - 0 1'
    const oneMoveMoves: AnalysisMove[] = [
      {
        move_number: 1,
        color: 'white',
        move_san: 'Ra8+',
        fen_after: oneMoveFen,
        eval_cp: 0,
        eval_mate: null,
        best_move_san: 'Ra8+',
        best_move_eval_cp: 0,
        eval_delta: 0,
        classification: 'best',
      },
    ]
    mockEngineInfoRef.current = [
      { pv: ['h8g7'], score: { type: 'cp', value: 30 }, depth: 21, multipv: 1 },
    ]
    mockEngineInfoFenRef.current = oneMoveFen

    render(
      <AnalysisBoard
        moves={oneMoveMoves}
        boardOrientation="white"
        positionAnalysis={{
          [oneMoveFen]: {
            best_move_uci: 'h8g7',
            best_move_san: 'Kg7',
            best_move_eval_cp: 42,
            best_line_uci: ['h8g7'],
            position_trusted: true,
          },
        }}
      />,
    )

    const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
    expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Kg7')
    // Live line, not the cached canonical eval — no marker, and the popup shows
    // the live depth chip (d21) rather than the "Canonical" label.
    expect(line1.querySelector('.analysis-board__engine-source')).toBeNull()
    fireEvent.click(line1)
    const dialog = screen.getByRole('dialog', { name: 'Engine line preview' })
    expect(dialog).toHaveTextContent('d21')
    expect(dialog).not.toHaveTextContent('Canonical')
  })

  it('keeps the cached canonical line as line 1 when its eval already exceeds the live lines (g-54h5)', () => {
    // Guard against over-rotating the sort: cached Nf3 (80) > live Nc3 (20) must
    // stay line 1 with the canonical marker on line 1.
    mockEngineInfoRef.current = [
      { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 20 }, depth: 21, multipv: 2 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        positionAnalysis={{
          [moves[1].fen_after]: {
            best_move_uci: 'g1f3',
            best_move_san: 'Nf3',
            best_move_eval_cp: 80,
            best_line_uci: ['g1f3', 'd7d6'],
            position_trusted: true,
          },
        }}
      />,
    )

    const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
    const line2 = screen.getByRole('button', { name: 'Show engine line 2' })

    expect(line1.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nf3')
    expect(line1.querySelector('.analysis-board__engine-eval')).toHaveTextContent('+0.8')
    expect(line1.querySelector('.analysis-board__engine-source')).not.toBeNull()

    expect(line2.querySelector('.analysis-board__engine-pv')).toHaveTextContent('Nc3')
    expect(line2.querySelector('.analysis-board__engine-source')).toBeNull()
  })

  it('runs a full multipv search when a trusted seed has no comparable eval (g-54h5)', () => {
    vi.useFakeTimers()
    try {
      render(
        <AnalysisBoard
          moves={moves}
          boardOrientation="white"
          positionAnalysis={{
            [moves[1].fen_after]: {
              best_move_uci: 'g1f3',
              best_move_san: 'Nf3',
              best_move_eval_cp: null,
              best_move_eval_mate: null,
              position_trusted: true,
            },
          }}
        />,
      )

      act(() => {
        vi.advanceTimersByTime(120)
      })

      // A trusted seed with neither a cp nor a mate eval can't be ranked against
      // searched lines, so it falls through to a full search rather than line 1.
      expect(mockEvaluatePosition).toHaveBeenCalledWith(
        moves[1].fen_after,
        { depth: 21, multipv: 3 },
      )
      const calls = mockEvaluatePosition.mock.calls as unknown as Array<
        [string, { searchmoves?: string[] }]
      >
      const call = calls.find(([fen]) => fen === moves[1].fen_after)
      expect(call?.[1].searchmoves).toBeUndefined()
    } finally {
      vi.useRealTimers()
    }
  })

  it('updates only the preview board when clicking PV moves or using arrow keys', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6', 'd2d4'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const chess = new Chess(moves[1].fen_after)
    chess.move({ from: 'g1', to: 'f3' })
    chess.move({ from: 'd7', to: 'd6' })
    const secondFen = chess.fen()

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })
    fireEvent.click(screen.getByRole('button', { name: 'd6' }))

    await waitFor(() => {
      const lastPreview = capturedChessboardRenders.filter((rendered) => rendered.kind === 'preview').at(-1)
      expect(lastPreview?.options.position).toBe(secondFen)
    })
    expect(capturedMoveListProps.currentIndex).toBeNull()
    expect(capturedChessboardProps.position).toBe(moves[1].fen_after)

    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    await waitFor(() => {
      expect(dialog.querySelector('[aria-current="step"]')).toHaveTextContent('Nf3')
    })
    expect(capturedMoveListProps.currentIndex).toBeNull()
  })

  it('preserves the popup across engine refreshes and clamps when the selected PV shortens', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6', 'd2d4'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const { rerender } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    fireEvent.click(screen.getByRole('button', { name: 'd4' }))
    expect(screen.getByRole('button', { name: 'd4' })).toHaveAttribute('aria-current', 'step')

    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 30 }, depth: 19 }]
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)

    expect(screen.getByRole('dialog', { name: 'Engine line preview' })).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: 'Engine line preview' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Nf3' })).toHaveAttribute('aria-current', 'step')
    })
    expect(screen.queryByRole('button', { name: 'd4' })).not.toBeInTheDocument()
  })

  it('keeps popup identity tied to the selected source engine slot when earlier slots become invalid', async () => {
    mockEngineInfoRef.current = [
      { pv: ['g1f3'], score: { type: 'cp', value: 42 }, depth: 18 },
      { pv: ['d2d4'], score: { type: 'cp', value: 30 }, depth: 18 },
    ]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const { rerender } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 2' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    expect(screen.getByRole('button', { name: 'd4' })).toHaveAttribute('aria-current', 'step')

    mockEngineInfoRef.current = [
      { pv: ['a1a2'], score: { type: 'cp', value: 44 }, depth: 19 },
      { pv: ['d2d4', 'g8f6'], score: { type: 'cp', value: 28 }, depth: 19 },
    ]
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)

    expect(screen.getByRole('dialog', { name: 'Engine line preview' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'd4' })).toHaveAttribute('aria-current', 'step')
    expect(screen.queryByRole('button', { name: 'Nf3' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show engine line 2' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('recalculates popup position on resize and renders outside the clipped moves column', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const { container } = render(
      <div className="history-page">
        <AnalysisBoard moves={moves} boardOrientation="white" />
      </div>,
    )
    const root = container.querySelector('.analysis-board') as HTMLElement
    vi.spyOn(root, 'getBoundingClientRect').mockReturnValue({
      x: 100, y: 50, top: 50, left: 100, right: 700, bottom: 600, width: 600, height: 550, toJSON: () => '',
    } as DOMRect)
    const anchor = screen.getByRole('button', { name: 'Show engine line 1' })
    vi.spyOn(anchor, 'getBoundingClientRect').mockReturnValue({
      x: 600, y: 90, top: 90, left: 600, right: 690, bottom: 112, width: 90, height: 22, toJSON: () => '',
    } as DOMRect)

    fireEvent.click(anchor)
    const dialog = await screen.findByRole('dialog', { name: 'Engine line preview' })
    await waitFor(() => {
      expect(dialog.style.top).toBe('70px')
    })
    expect(dialog.closest('.analysis-board__moves-col')).toBeNull()

    vi.spyOn(anchor, 'getBoundingClientRect').mockReturnValue({
      x: 300, y: 80, top: 80, left: 300, right: 390, bottom: 102, width: 90, height: 22, toJSON: () => '',
    } as DOMRect)
    fireEvent.resize(window)
    await waitFor(() => {
      expect(dialog.style.top).toBe('60px')
    })
  })

  it('closes the popup on selected line disappearance, engine toggle off, displayed position changes, and outside pointer-down', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const { rerender } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    mockEngineInfoRef.current = []
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Engine line preview' })).not.toBeInTheDocument())

    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 42 }, depth: 18 }]
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    fireEvent.click(screen.getByLabelText('Engine lines'))
    expect(screen.queryByRole('dialog', { name: 'Engine line preview' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Engine lines'))
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))
    expect(screen.queryByRole('dialog', { name: 'Engine line preview' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))
    mockEngineInfoFenRef.current = moves[1].fen_after
    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 20 }, depth: 18 }]
    rerender(<AnalysisBoard moves={[...moves]} boardOrientation="white" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', { name: 'Engine line preview' })
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog', { name: 'Engine line preview' })).not.toBeInTheDocument()
  })

  it('does not render focusable buttons for missing engine line placeholders', () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.getAllByRole('button', { name: /Show engine line/ })).toHaveLength(1)
  })

  it('reserves the engine-line row while recomputing so the MoveList does not jitter', () => {
    // Seed a real line whose engineInfoFen points at an OLD position while the
    // board shows the latest position. engineInfoFen !== displayedFen, so the
    // stale line is filtered out (no buttons), but the row must stay mounted to
    // hold its height during the recompute gap.
    mockEngineInfoRef.current = [{ pv: ['g1f3'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[0].fen_after

    const { container } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(container.querySelector('.analysis-board__engine-lines')).not.toBeNull()
    expect(screen.queryAllByRole('button', { name: /Show engine line/ })).toHaveLength(0)
  })

  it('does not reserve the engine-line row when engine lines are turned off', () => {
    const { container } = render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(container.querySelector('.analysis-board__engine-lines')).not.toBeNull()

    fireEvent.click(screen.getByLabelText('Engine lines'))

    expect(container.querySelector('.analysis-board__engine-lines')).toBeNull()
  })
})

describe('AnalysisBoard — engine line hover', () => {
  const dialogQuery = { name: 'Engine line preview' }

  it('opens the popup on hover and auto-dismisses after the grace window on leave', async () => {
    vi.useFakeTimers()
    try {
      mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 }]
      mockEngineInfoFenRef.current = moves[1].fen_after
      render(<AnalysisBoard moves={moves} boardOrientation="white" />)

      const line = screen.getByRole('button', { name: 'Show engine line 1' })
      act(() => {
        fireEvent.mouseEnter(line)
      })
      expect(screen.getByRole('dialog', dialogQuery)).toBeInTheDocument()

      act(() => {
        fireEvent.mouseLeave(line)
      })
      // Still open before the grace timer fires.
      expect(screen.getByRole('dialog', dialogQuery)).toBeInTheDocument()
      act(() => {
        vi.advanceTimersByTime(100)
      })
      expect(screen.queryByRole('dialog', dialogQuery)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the popup open when crossing the button→popup gap before the timer fires', async () => {
    vi.useFakeTimers()
    try {
      mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 }]
      mockEngineInfoFenRef.current = moves[1].fen_after
      render(<AnalysisBoard moves={moves} boardOrientation="white" />)

      const line = screen.getByRole('button', { name: 'Show engine line 1' })
      act(() => {
        fireEvent.mouseEnter(line)
      })
      const dialog = screen.getByRole('dialog', dialogQuery)
      act(() => {
        fireEvent.mouseLeave(line)
        fireEvent.mouseEnter(dialog)
        vi.advanceTimersByTime(100)
      })
      expect(screen.getByRole('dialog', dialogQuery)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('promotes hover to persist when interacting inside the popup', async () => {
    vi.useFakeTimers()
    try {
      mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 }]
      mockEngineInfoFenRef.current = moves[1].fen_after
      render(<AnalysisBoard moves={moves} boardOrientation="white" />)

      const line = screen.getByRole('button', { name: 'Show engine line 1' })
      act(() => {
        fireEvent.mouseEnter(line)
      })
      const dialog = screen.getByRole('dialog', dialogQuery)
      act(() => {
        fireEvent.pointerDown(dialog)
      })
      // Persisted: leaving no longer auto-dismisses.
      act(() => {
        fireEvent.mouseLeave(dialog)
        vi.advanceTimersByTime(100)
      })
      expect(screen.getByRole('dialog', dialogQuery)).toBeInTheDocument()
      expect(capturedMoveListProps.suppressKeyboardNavigation).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps a persisted popup open on leave and hovering a different line', async () => {
    vi.useFakeTimers()
    try {
      mockEngineInfoRef.current = [
        { pv: ['g1f3', 'd7d6'], score: { type: 'cp', value: 42 }, depth: 18 },
        { pv: ['b1c3', 'b8c6'], score: { type: 'cp', value: 10 }, depth: 18, multipv: 2 },
      ]
      mockEngineInfoFenRef.current = moves[1].fen_after
      render(<AnalysisBoard moves={moves} boardOrientation="white" />)

      const line1 = screen.getByRole('button', { name: 'Show engine line 1' })
      const line2 = screen.getByRole('button', { name: 'Show engine line 2' })
      // Click persists line 1.
      act(() => {
        fireEvent.click(line1)
      })
      expect(line1).toHaveAttribute('aria-pressed', 'true')

      // Hovering line 2 must not downgrade/replace the persisted popup.
      act(() => {
        fireEvent.mouseEnter(line2)
        fireEvent.mouseLeave(line2)
        vi.advanceTimersByTime(100)
      })
      expect(screen.getByRole('dialog', dialogQuery)).toBeInTheDocument()
      expect(line1).toHaveAttribute('aria-pressed', 'true')
      expect(line2).toHaveAttribute('aria-pressed', 'false')

      // Outside click dismisses it.
      act(() => {
        fireEvent.pointerDown(document.body)
      })
      expect(screen.queryByRole('dialog', dialogQuery)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('routes arrows to the main move list (not the preview) while only hovering', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6', 'd2d4'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const chess = new Chess(moves[1].fen_after)
    chess.move({ from: 'g1', to: 'f3' })
    const firstFen = chess.fen()

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    const line = screen.getByRole('button', { name: 'Show engine line 1' })
    act(() => {
      fireEvent.mouseEnter(line)
    })
    await screen.findByRole('dialog', dialogQuery)
    expect(capturedMoveListProps.suppressKeyboardNavigation).toBe(false)

    act(() => {
      fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    })
    // The arrow reached the main move-list listener (not stolen by a capture
    // handler), and the preview PV did NOT advance.
    expect(capturedMoveListKeys).toContain('ArrowRight')
    const lastPreview = capturedChessboardRenders.filter((r) => r.kind === 'preview').at(-1)
    expect(lastPreview?.options.position).toBe(firstFen)
  })

  it('drives the preview PV and withholds arrows from the main list once persisted', async () => {
    mockEngineInfoRef.current = [{ pv: ['g1f3', 'd7d6', 'd2d4'], score: { type: 'cp', value: 42 }, depth: 18 }]
    mockEngineInfoFenRef.current = moves[1].fen_after
    const chess = new Chess(moves[1].fen_after)
    chess.move({ from: 'g1', to: 'f3' })
    const firstFen = chess.fen()
    chess.move({ from: 'd7', to: 'd6' })
    const secondFen = chess.fen()

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Show engine line 1' }))
    await screen.findByRole('dialog', dialogQuery)
    expect(capturedMoveListProps.suppressKeyboardNavigation).toBe(true)

    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    await waitFor(() => {
      const lastPreview = capturedChessboardRenders.filter((r) => r.kind === 'preview').at(-1)
      expect(lastPreview?.options.position).toBe(secondFen)
    })
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    await waitFor(() => {
      const lastPreview = capturedChessboardRenders.filter((r) => r.kind === 'preview').at(-1)
      expect(lastPreview?.options.position).toBe(firstFen)
    })
    // The capture handler intercepted the arrows; none reached the main list.
    expect(capturedMoveListKeys).not.toContain('ArrowRight')
    expect(capturedMoveListKeys).not.toContain('ArrowLeft')
  })
})

describe('AnalysisBoard — AnalysisGraph props', () => {
  beforeEach(() => {
    capturedGraphProps = {}
  })

  it('forwards playerColor and evalCp to AnalysisGraph', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    expect(capturedGraphProps.playerColor).toBe('black')
    // Default view shows last move (index 1), eval_cp = -120, white perspective = +120
    expect(capturedGraphProps.evalCp).toBe(120)
  })

  const openingCrossingMoves = (sans: string[]): AnalysisMove[] => {
    const chess = new Chess()
    return sans.map((san, index) => {
      chess.move(san)
      return {
        move_number: Math.floor(index / 2) + 1,
        color: index % 2 === 0 ? 'white' : 'black',
        move_san: san,
        fen_after: chess.fen(),
        eval_cp: 0,
        eval_mate: null,
        best_move_san: san,
        best_move_eval_cp: 0,
        eval_delta: 0,
        classification: 'best',
      }
    })
  }

  const developmentLine = [
    'Nf3', 'Nf6', 'g3', 'g6', 'Bg2', 'Bg7', 'd3', 'd6', 'Nbd2',
    'Nbd7', 'b3', 'b6', 'Bb2', 'Bb7', 'e3', 'e6', 'Qe2', 'Qe7',
  ]

  it('forwards the computed opening boundary for a complete standard game', () => {
    render(
      <AnalysisBoard
        moves={openingCrossingMoves(developmentLine)}
        boardOrientation="white"
      />,
    )

    expect(capturedGraphProps.openingPlyCount).toBe(17)
  })

  it('forwards the boundary when the crossing is the terminal plotted ply', () => {
    render(
      <AnalysisBoard
        moves={openingCrossingMoves(developmentLine.slice(0, 17))}
        boardOrientation="white"
      />,
    )

    expect(capturedGraphProps.openingPlyCount).toBe(17)
  })

  it('fails closed for a nonstandard start or a line missing ply 1 white', () => {
    const standardMoves = openingCrossingMoves(developmentLine)
    const { rerender } = render(
      <AnalysisBoard
        moves={standardMoves}
        boardOrientation="white"
        startingFen="8/8/8/8/8/8/8/8 w - - 0 1"
      />,
    )
    expect(capturedGraphProps.openingPlyCount).toBeNull()

    rerender(
      <AnalysisBoard
        moves={[{ ...standardMoves[0], move_number: 2 }, ...standardMoves.slice(1)]}
        boardOrientation="white"
      />,
    )
    expect(capturedGraphProps.openingPlyCount).toBeNull()
  })

  it('forwards evalCp in latest-view when currentIndex is null', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // Latest view (null currentIndex) uses effectiveIndex = last move
    expect(capturedGraphProps.evalCp).toBe(120)
    expect(capturedGraphProps.currentIndex).toBeNull()
  })

  it('forwards isCheckmate=false when eval_mate is not 0', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedGraphProps.isCheckmate).toBe(false)
  })

  it('forwards isCheckmate=true and synthetic evalCp when eval_mate is 0 and eval_cp is null', () => {
    const checkmatedMoves: AnalysisMove[] = [
      ...moves,
      {
        move_number: 2,
        color: 'white',
        move_san: 'Qh5',
        fen_after: 'rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3',
        eval_cp: null,
        eval_mate: 0,
        best_move_san: 'Qh5',
        best_move_eval_cp: null,
        eval_delta: 0,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={checkmatedMoves} boardOrientation="white" />)

    expect(capturedGraphProps.isCheckmate).toBe(true)
    // eval_cp is null but eval_mate=0 → synthetic evalCp derived from mateToCp(0)
    expect(capturedGraphProps.evalCp).toBeTypeOf('number')
    expect(capturedGraphProps.evalCp).not.toBe(0)
    // evals array should also include a mate-derived value (not null) for the mate move
    const evals = capturedGraphProps.evals as (number | null)[]
    const mateEval = evals[evals.length - 1]
    expect(mateEval).toBeTypeOf('number')
    expect(mateEval).not.toBe(0)
  })

  it('odd-index mate: evalCp sign is correct (no double white-perspective conversion)', () => {
    // Mate at index 1 (odd): black delivered checkmate, white is mated
    const oddMatedMoves: AnalysisMove[] = [
      {
        move_number: 1,
        color: 'white',
        move_san: 'f3',
        fen_after: 'rnbqkbnr/pppppppp/8/8/8/5P2/PPPPP1PP/RNBQKBNR b KQkq - 0 1',
        eval_cp: -50,
        eval_mate: null,
        best_move_san: 'e4',
        best_move_eval_cp: 30,
        eval_delta: 80,
        classification: 'inaccuracy',
      },
      {
        move_number: 1,
        color: 'black',
        move_san: 'Qh4#',
        fen_after: 'rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3',
        eval_cp: null,
        eval_mate: 0,
        best_move_san: 'Qh4#',
        best_move_eval_cp: null,
        eval_delta: 0,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={oddMatedMoves} boardOrientation="white" />)

    expect(capturedGraphProps.isCheckmate).toBe(true)
    // White is mated → white perspective should be a large negative value
    const evalCp = capturedGraphProps.evalCp as number
    expect(evalCp).toBeLessThan(0)
    // evals array mate entry should also be negative (white is losing)
    const evals = capturedGraphProps.evals as (number | null)[]
    expect(evals[1]).toBeLessThan(0)
  })

  it('plots nonzero mate-only moves with the correct (mover-relative) sign', () => {
    // White move with mover-relative mate-in-3 (white mates) and no cp.
    const whiteMateMoves: AnalysisMove[] = [
      {
        move_number: 1,
        color: 'white',
        move_san: 'Qd5',
        fen_after: 'rnbqkbnr/pppppppp/8/3Q4/8/8/PPPPPPPP/RNB1KBNR b KQkq - 0 1',
        eval_cp: null,
        eval_mate: 3,
        best_move_san: 'Qd5',
        best_move_eval_cp: null,
        eval_delta: 0,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={whiteMateMoves} boardOrientation="white" initialMoveIndex={0} />)

    const evals = capturedGraphProps.evals as (number | null)[]
    // White mates → white-perspective eval must be strongly positive (was
    // previously sign-flipped to ~-9970 by the missing inner negation).
    expect(evals[0]).toBeGreaterThan(0)
    expect(capturedGraphProps.evalMate).toBe(3)
  })

  it('plots a black mover-relative mate as white-losing', () => {
    const blackMateMoves: AnalysisMove[] = [
      moves[0],
      {
        move_number: 1,
        color: 'black',
        move_san: 'Qh4',
        fen_after: 'rnb1kbnr/pppp1ppp/4p3/8/6Pq/8/PPPPPP1P/RNBQKBNR w KQkq - 1 2',
        eval_cp: null,
        eval_mate: 2, // mover (black) mates in 2
        best_move_san: 'Qh4',
        best_move_eval_cp: null,
        eval_delta: 0,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={blackMateMoves} boardOrientation="white" initialMoveIndex={1} />)

    const evals = capturedGraphProps.evals as (number | null)[]
    expect(evals[1]).toBeLessThan(0) // white is getting mated
    expect(capturedGraphProps.evalMate).toBe(-2) // white-perspective: loss in 2
  })

  // g-j7br: the truly-unevaluated terminal-mate shape (eval_cp AND eval_mate both
  // null) is the bug — the persistence race can upload the checkmating move before
  // its search resolves. The last point must peg to the winner from fen_after, not
  // land null-coerced on the equal line, and the badge must show #.
  it('pegs an unevaluated terminal mate to the winner (white wins) and shows #', () => {
    const wonByMate: AnalysisMove[] = [
      ...moves,
      {
        move_number: 2,
        color: 'white',
        move_san: 'Qxf7#',
        // Scholar's mate — index 2 (even) → white delivered mate.
        fen_after: 'r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4',
        eval_cp: null,
        eval_mate: null, // never evaluated (both channels null)
        best_move_san: 'Qxf7#',
        best_move_eval_cp: null,
        eval_delta: null,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={wonByMate} boardOrientation="white" />)

    expect(capturedGraphProps.isCheckmate).toBe(true)
    const evals = capturedGraphProps.evals as (number | null)[]
    // Even ply → white mates → strongly positive, NOT null and NOT the 0 line.
    expect(evals[2]).toBe(10000)
    expect(capturedGraphProps.evalCp).toBe(10000)
  })

  it('pegs an unevaluated terminal mate to the winner (white loses) and shows #', () => {
    const lostByMate: AnalysisMove[] = [
      {
        move_number: 1,
        color: 'white',
        move_san: 'f3',
        fen_after: 'rnbqkbnr/pppppppp/8/8/8/5P2/PPPPP1PP/RNBQKBNR b KQkq - 0 1',
        eval_cp: -50,
        eval_mate: null,
        best_move_san: 'e4',
        best_move_eval_cp: 30,
        eval_delta: 80,
        classification: 'inaccuracy',
      },
      {
        move_number: 1,
        color: 'black',
        move_san: 'Qh4#',
        // Fool's mate — index 1 (odd) → black delivered mate, white is mated.
        fen_after: 'rnb1kbnr/pppp1ppp/4p3/8/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3',
        eval_cp: null,
        eval_mate: null, // never evaluated (both channels null)
        best_move_san: 'Qh4#',
        best_move_eval_cp: null,
        eval_delta: null,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={lostByMate} boardOrientation="black" />)

    expect(capturedGraphProps.isCheckmate).toBe(true)
    const evals = capturedGraphProps.evals as (number | null)[]
    // Odd ply → black mates → white-perspective strongly negative.
    expect(evals[1]).toBe(-10000)
    expect(capturedGraphProps.evalCp).toBe(-10000)
  })
})

describe('AnalysisBoard — variation tree integration', () => {
  const varNodeFen = 'rnbqkbnr/pp1ppppp/8/2p5/2B1P3/8/PPPP1PPP/RNBQKNR b KQkq - 1 2'
  const varNodeFenBefore = 'rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2'

  const makeVarNode = (overrides?: Partial<VarNode>): VarNode => ({
    id: 'var-node-1',
    san: 'Bc4',
    fen: varNodeFen,
    fenBefore: varNodeFenBefore,
    uci: 'f1c4',
    parentId: null,
    parentGameIndex: 1,
    branchPlyOffset: 0,
    children: [],
    nestingLevel: 0,
    ...overrides,
  })

  it('displays variation node FEN on the board when a variation is selected', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedChessboardProps.position).toBe(varNodeFen)
  })

  it('highlights from/to squares from variation node', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    const styles = capturedChessboardProps.squareStyles as Record<string, unknown>
    // Bc4 moves from f1 to c4
    expect(styles).toHaveProperty('f1')
    expect(styles).toHaveProperty('c4')
  })

  it('falls back to one variation-cached eval across all selected-position consumers', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    mockGetAbsolutePly.mockReturnValue(2)
    // Return cached analysis for this variation FEN
    mockGetVarAnalysis.mockImplementation((fen: string) => {
      if (fen === varNodeFen) return { playedEval: 50, id: 'req-1', move: 'Bc4', bestMove: 'Nf3', bestEval: 30, currentPositionEval: null, playedEvalMate: null, currentPositionEvalMate: null, moveIndex: null, delta: null, classification: null, blunder: false, recordable: false }
      return undefined
    })

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // White orientation: playerToWhite(50, 'white') = 50
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(50)
    expect(capturedMoveListProps.headerEvalOverride).toBe('+0.5')
    expect(capturedGraphProps.evalCp).toBe(50)
    expect(capturedGraphProps.evalMate).toBeNull()
    expect(capturedGraphProps.variationLine).toEqual(
      expect.objectContaining({
        points: [{ index: 2, cp: 50, pending: false }],
        streaming: null,
      }),
    )
  })

  it('uses a FEN-matched live cp eval across the selected variation while cache analysis is pending', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    mockGetAbsolutePly.mockReturnValue(2)
    mockEngineInfoFenRef.current = varNodeFen
    // Black is to move in varNodeFen, so a side-to-move -40cp score is +40cp
    // from white's perspective.
    mockEngineInfoRef.current = [
      { score: { type: 'cp', value: -40 }, depth: 18, multipv: 1 },
    ]

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(40)
    expect(capturedEvalBarProps.whitePerspectiveMate).toBeNull()
    expect(capturedMoveListProps.headerEvalOverride).toBe('+0.4')
    expect(capturedGraphProps.evalCp).toBe(40)
    expect(capturedGraphProps.evalMate).toBeNull()
    expect(capturedGraphProps.variationLine).toEqual(
      expect.objectContaining({
        points: [{ index: 2, cp: 40, pending: false }],
        streaming: null,
      }),
    )
  })

  it('uses a live mate-0 score for the selected variation badge, checkmate signal, and graph tip', () => {
    const checkmateFen = 'r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4'
    const beforeCheckmateFen = 'r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4'
    const node = makeVarNode({
      fen: checkmateFen,
      fenBefore: beforeCheckmateFen,
      san: 'Qxf7#',
      uci: 'h5f7',
    })
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    mockGetAbsolutePly.mockReturnValue(2)
    mockEngineInfoFenRef.current = checkmateFen
    mockEngineInfoRef.current = [
      { score: { type: 'mate', value: 0 }, depth: 21, multipv: 1 },
    ]

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // Black is the checkmated side to move, so white's graph/bar cp is terminal +.
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(10000)
    expect(capturedEvalBarProps.whitePerspectiveMate).toBe(0)
    expect(capturedMoveListProps.headerEvalOverride).toBe('#')
    expect(capturedGraphProps.evalCp).toBe(10000)
    expect(capturedGraphProps.evalMate).toBe(0)
    expect(capturedGraphProps.isCheckmate).toBe(true)
    expect(capturedGraphProps.variationLine).toEqual(
      expect.objectContaining({
        points: [{ index: 2, cp: 10000, pending: false }],
        streaming: null,
      }),
    )
  })

  it('uses the terminal variation FEN before either eval source resolves', () => {
    const checkmateFen = 'r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4'
    const beforeCheckmateFen = 'r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4'
    const node = makeVarNode({
      fen: checkmateFen,
      fenBefore: beforeCheckmateFen,
      san: 'Qxf7#',
      uci: 'h5f7',
    })
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    mockGetAbsolutePly.mockReturnValue(2)

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(10000)
    expect(capturedEvalBarProps.whitePerspectiveMate).toBe(0)
    expect(capturedMoveListProps.headerEvalOverride).toBe('#')
    expect(capturedGraphProps.evalCp).toBe(10000)
    expect(capturedGraphProps.evalMate).toBe(0)
    expect(capturedGraphProps.isCheckmate).toBe(true)
    expect(capturedGraphProps.variationLine).toEqual(
      expect.objectContaining({
        points: [{ index: 2, cp: 10000, pending: false }],
        streaming: null,
      }),
    )
  })

  it('normalizes a cp-only cache entry at a terminal variation FEN to checkmate', () => {
    const checkmateFen = 'r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4'
    const beforeCheckmateFen = 'r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4'
    const node = makeVarNode({
      fen: checkmateFen,
      fenBefore: beforeCheckmateFen,
      san: 'Qxf7#',
      uci: 'h5f7',
    })
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    mockGetAbsolutePly.mockReturnValue(2)
    mockGetVarAnalysis.mockImplementation((fen: string) => {
      if (fen === checkmateFen) return { playedEval: 25, id: 'req-1', move: 'Qxf7#', bestMove: 'Qxf7#', bestEval: 25, currentPositionEval: null, playedEvalMate: null, currentPositionEvalMate: null, moveIndex: null, delta: null, classification: null, blunder: false, recordable: false }
      return undefined
    })

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(10000)
    expect(capturedEvalBarProps.whitePerspectiveMate).toBe(0)
    expect(capturedMoveListProps.headerEvalOverride).toBe('#')
    expect(capturedGraphProps.evalCp).toBe(10000)
    expect(capturedGraphProps.evalMate).toBe(0)
    expect(capturedGraphProps.isCheckmate).toBe(true)
    expect(capturedGraphProps.variationLine).toEqual(
      expect.objectContaining({
        points: [{ index: 2, cp: 10000, pending: false }],
        streaming: null,
      }),
    )
  })

  it('uses game eval for eval bar when not in variation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // Last move (index 1): eval_cp = -120, white perspective = +120
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(120)
  })

  it('keeps the analysis graph mounted when in variation', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.queryByTestId('analysis-graph')).toBeInTheDocument()
  })

  it('passes variation tree props to MoveList', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(capturedMoveListProps.variationTree).toBe(mockTree)
    expect(capturedMoveListProps.getAbsolutePly).toBe(mockGetAbsolutePly)
    expect(capturedMoveListProps.navigateUp).toBe(mockNavigateUp)
    expect(capturedMoveListProps.navigateDown).toBe(mockNavigateDown)
  })

  it('handleNavigate clears selectedVarNodeId but does not clear tree', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // Click a main-line move via MoveList mock
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    expect(mockSetSelectedVarNode).toHaveBeenCalledWith(null)
    expect(mockClearTree).not.toHaveBeenCalled()
  })

  it('shows analysis graph when not in variation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.getByTestId('analysis-graph')).toBeInTheDocument()
  })
})

describe('AnalysisBoard — handleDrop behavior', () => {
  // Helper to invoke onPieceDrop from the captured Chessboard props
  const invokeDrop = (source: string, target: string): boolean => {
    const onDrop = capturedChessboardProps.onPieceDrop as (args: { sourceSquare: string; targetSquare: string }) => boolean
    // The drop synchronously updates AnalysisBoard state (cursor/variation), so
    // wrap it in act() to keep those re-renders out of "not wrapped in act(...)".
    let result!: boolean
    act(() => {
      result = onDrop({ sourceSquare: source, targetSquare: target })
    })
    return result
  }

  it('main-line continuation: advances cursor instead of creating variation', () => {
    // Navigate to move 0 (e4), then play the next game move (c5)
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    // Navigate to move 0 (e4)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // The displayed FEN should be after e4, and the next game move is c5 (c7c5)
    const result = invokeDrop('c7', 'c5')

    expect(result).toBe(true)
    // Should NOT have called addMove — this is a main-line continuation
    expect(mockAddMove).not.toHaveBeenCalled()
    expect(mockAnalyzeMove).not.toHaveBeenCalled()
  })

  it('alternate move from game position: creates variation and triggers analysis', () => {
    // Navigate to move 0 (e4), then play d5 instead of c5
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    const result = invokeDrop('d7', 'd5')

    expect(result).toBe(true)
    expect(mockAddMove).toHaveBeenCalledTimes(1)
    const addMoveArg = mockAddMove.mock.calls[0]![0]
    expect(addMoveArg.san).toBe('d5')
    expect(addMoveArg.parentContext).toEqual({ type: 'game', moveIndex: 0 })
    // Should select the new node
    expect(mockSetSelectedVarNode).toHaveBeenCalledWith('mock-node-id')
    // Should trigger analysis and register pending
    expect(mockAnalyzeMove).toHaveBeenCalledTimes(1)
    expect(mockRegisterPending).toHaveBeenCalledWith('req-123', expect.any(String))
  })

  it('dedup: skips analyzeMove when FEN is already cached', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // Mock: getVarAnalysis returns a result for the FEN that d5 produces
    mockGetVarAnalysis.mockImplementation(() => ({
      playedEval: 10, id: 'old-req', move: 'd5', bestMove: 'e5',
      bestEval: 20, currentPositionEval: null, playedEvalMate: null, currentPositionEvalMate: null, moveIndex: null,
      delta: null, classification: null, blunder: false, recordable: false,
    }))

    const result = invokeDrop('d7', 'd5')

    expect(result).toBe(true)
    expect(mockAddMove).toHaveBeenCalledTimes(1)
    expect(mockSetSelectedVarNode).toHaveBeenCalledWith('mock-node-id')
    // Should NOT trigger analysis — already cached
    expect(mockAnalyzeMove).not.toHaveBeenCalled()
    expect(mockRegisterPending).not.toHaveBeenCalled()
  })

  it('dedup: skips analyzeMove when FEN has a pending request', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // Pre-populate pending with the FEN that d5 will produce from after-e4 position
    // We need to compute it: after e4 FEN + d5 move
    const chess = new Chess(moves[0].fen_after)
    chess.move({ from: 'd7', to: 'd5', promotion: 'q' })
    const resultFen = chess.fen()
    mockPendingRequestsRef.current.set('existing-req', resultFen)

    const result = invokeDrop('d7', 'd5')

    expect(result).toBe(true)
    expect(mockAddMove).toHaveBeenCalledTimes(1)
    expect(mockSetSelectedVarNode).toHaveBeenCalledWith('mock-node-id')
    // Should NOT trigger analysis — already pending
    expect(mockAnalyzeMove).not.toHaveBeenCalled()
    expect(mockRegisterPending).not.toHaveBeenCalled()
  })

  it('variation continuation: uses variation node FEN as base and creates nested branch', () => {
    const varNode: VarNode = {
      id: 'var-1',
      san: 'd5',
      // FEN after 1. e4 d5 (valid position)
      fen: 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
      fenBefore: moves[0].fen_after,
      uci: 'd7d5',
      parentId: null,
      parentGameIndex: 0,
      branchPlyOffset: 0,
      children: [],
      nestingLevel: 0,
    }
    mockTree = { nodes: new Map([['var-1', varNode]]), rootBranches: new Map([[0, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // From the variation position (after 1. e4 d5), play 2. Nf3
    const result = invokeDrop('g1', 'f3')

    expect(result).toBe(true)
    expect(mockAddMove).toHaveBeenCalledTimes(1)
    const addMoveArg = mockAddMove.mock.calls[0]![0]
    expect(addMoveArg.san).toBe('Nf3')
    expect(addMoveArg.parentContext).toEqual({ type: 'variation', nodeId: 'var-1' })
    expect(addMoveArg.fenBefore).toBe(varNode.fen)
  })

  it('main-line continuation at last move uses null for currentIndex', () => {
    // 3-move game: navigate to move 1 (index 1), play a move that matches move 2
    const threeMoves: AnalysisMove[] = [
      ...moves,
      {
        move_number: 2,
        color: 'white',
        move_san: 'Nf3',
        fen_after: 'rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
        eval_cp: 40,
        eval_mate: null,
        best_move_san: 'Nf3',
        best_move_eval_cp: 40,
        eval_delta: 0,
        classification: 'best',
      },
    ]

    render(<AnalysisBoard moves={threeMoves} boardOrientation="white" />)
    // Navigate to move 1 (c5), then play Nf3 which is move 2 (last move)
    fireEvent.click(screen.getByRole('button', { name: 'Move 2' }))

    const result = invokeDrop('g1', 'f3')

    expect(result).toBe(true)
    expect(mockAddMove).not.toHaveBeenCalled()
    // handleDrop calls setCurrentIndex(null) for last move — verify via MoveList prop
    expect(capturedMoveListProps.currentIndex).toBeNull()
  })
})

describe('AnalysisBoard — click-to-move behavior', () => {
  const invokeClick = (square: string): void => {
    const onClick = capturedChessboardProps.onSquareClick as (args: { square: string }) => void
    act(() => {
      onClick({ square })
    })
  }

  it('selecting an own-side piece highlights it and dots its legal destinations', () => {
    // Latest view: after 1. e4 c5, white to move. Click the e4 pawn.
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    invokeClick('e4')

    const styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    // Selected square highlighted yellow
    expect(styles.e4?.background).toBe('rgba(255, 255, 0, 0.4)')
    // e5 is a quiet legal destination → radial-gradient dot
    expect(styles.e5?.background).toContain('radial-gradient')
    expect(styles.e5?.borderRadius).toBe('50%')
  })

  it('tints capture destinations red', () => {
    // Position after 1. e4 d5 — white to move, e4 pawn can capture on d5.
    const captureMoves: AnalysisMove[] = [
      moves[0],
      {
        move_number: 1,
        color: 'black',
        move_san: 'd5',
        fen_after: 'rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2',
        eval_cp: 20,
        eval_mate: null,
        best_move_san: 'd5',
        best_move_eval_cp: 20,
        eval_delta: 0,
        classification: 'best',
      },
    ]
    render(<AnalysisBoard moves={captureMoves} boardOrientation="white" />)

    invokeClick('e4')

    const styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    // exd5 is a capture → red tint
    expect(styles.d5?.background).toBe('rgba(255, 0, 0, 0.4)')
  })

  it('clicking a dotted destination makes the move (creates a variation)', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    // Navigate to move 0 (after e4), white... actually after e4 it is black to move.
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // After 1. e4, black to move. Select c7 pawn then click c5 (matches main line).
    invokeClick('c7')
    let styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    expect(styles.c7?.background).toBe('rgba(255, 255, 0, 0.4)')
    // c6 is a quiet legal destination → radial-gradient dot
    expect(styles.c6?.background).toContain('radial-gradient')

    // Click c5 — main-line continuation, advances cursor without addMove
    invokeClick('c5')
    expect(mockAddMove).not.toHaveBeenCalled()
    // Option dots cleared after the move (c6 no longer dotted)
    styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    expect(styles.c6).toBeUndefined()
  })

  it('clicking an alternate destination branches a variation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // After 1. e4, select d7 then click d5 (not the main-line c5) → variation
    invokeClick('d7')
    invokeClick('d5')

    expect(mockAddMove).toHaveBeenCalledTimes(1)
    expect(mockAddMove.mock.calls[0]![0].san).toBe('d5')
  })

  it('clicking an empty square clears option dots', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    invokeClick('e4')
    expect((capturedChessboardProps.squareStyles as Record<string, unknown>).e4).toBeDefined()

    invokeClick('e3') // empty square, no own piece
    const styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    // option dots cleared; only last-move highlights (if any) remain
    expect(styles.e4?.background).not.toBe('rgba(255, 255, 0, 0.4)')
  })

  it('navigating clears option dots', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    invokeClick('e4')
    expect((capturedChessboardProps.squareStyles as Record<string, unknown>).e4).toBeDefined()

    // Navigate via MoveList → clearMoveHints should fire
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))
    const styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    expect(styles.e5).toBeUndefined()
  })

  it('clicking the opponent piece (not side-to-move) selects nothing', () => {
    // Latest: white to move. Click a black piece (d7 pawn) → no selection/dots.
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    invokeClick('d7')
    const styles = capturedChessboardProps.squareStyles as Record<string, React.CSSProperties>
    // No yellow selection and no option dots for the opponent piece
    expect(styles.d7).toBeUndefined()
    expect(styles.d6).toBeUndefined()
  })
})

describe('computeBoardEvalIcon', () => {
  it('returns a badge for non-good classifications', () => {
    for (const c of ['blunder', 'mistake', 'inaccuracy', 'best', 'excellent'] as const) {
      const icon = computeBoardEvalIcon({
        square: 'e4',
        classification: c,
        boardOrientation: 'white',
      })
      expect(icon).not.toBeNull()
      expect(icon?.classification).toBe(c)
      expect(icon?.icon).toBeTruthy()
    }
  })

  it('skips "good", null, and undefined classifications', () => {
    expect(
      computeBoardEvalIcon({ square: 'e4', classification: 'good', boardOrientation: 'white' }),
    ).toBeNull()
    expect(
      computeBoardEvalIcon({ square: 'e4', classification: null, boardOrientation: 'white' }),
    ).toBeNull()
    expect(
      computeBoardEvalIcon({ square: 'e4', classification: undefined, boardOrientation: 'white' }),
    ).toBeNull()
  })

  it('skips a null/invalid square', () => {
    expect(
      computeBoardEvalIcon({ square: null, classification: 'blunder', boardOrientation: 'white' }),
    ).toBeNull()
    expect(
      computeBoardEvalIcon({ square: 'z9', classification: 'blunder', boardOrientation: 'white' }),
    ).toBeNull()
  })

  it('positions the badge at the top-right of the square (white)', () => {
    // e4: file=4, rank=4 → squareLeft=50%, squareTop=50%
    const icon = computeBoardEvalIcon({
      square: 'e4',
      classification: 'mistake',
      boardOrientation: 'white',
    })
    expect(icon?.left).toBe('61.5%') // 50 + 11.5
    expect(icon?.top).toBe('51%') // 50 + 1
  })

  it('mirrors to top-left on the right edge (h-file, white)', () => {
    // h4: file=7 → right edge; squareLeft=87.5%
    const icon = computeBoardEvalIcon({
      square: 'h4',
      classification: 'blunder',
      boardOrientation: 'white',
    })
    expect(icon?.left).toBe('88.5%') // 87.5 + 1
    expect(icon?.top).toBe('51%')
  })

  it('clamps top to the badge radius on the top rank (white)', () => {
    // e8: rank=8 → squareTop=0; centerY clamped to 2.5%
    const icon = computeBoardEvalIcon({
      square: 'e8',
      classification: 'best',
      boardOrientation: 'white',
    })
    expect(icon?.top).toBe('2.5%')
  })

  it('flips coordinates and edges for black orientation', () => {
    // a1 black: file=0 → right edge; squareLeft=(7-0)*12.5=87.5; rank=1 → top edge
    const icon = computeBoardEvalIcon({
      square: 'a1',
      classification: 'inaccuracy',
      boardOrientation: 'black',
    })
    expect(icon?.left).toBe('88.5%') // mirrored to top-left of edge square
    expect(icon?.top).toBe('2.5%') // clamped at top
  })
})

describe('AnalysisBoard evidence reuse wiring (g-reuse-d21-search)', () => {
  const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'
  const wiredMoves: AnalysisMove[] = [
    { ...moves[0], fen_before: START, move_uci: 'e2e4', fen_after: AFTER_E4 },
    { ...moves[1], fen_before: AFTER_E4, move_uci: 'c7c5' },
  ]

  beforeEach(() => {
    mockConsiderCompletedSearch.mockClear()
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  // Advance past the search debounce, then flush the evaluatePosition microtasks so
  // the completion callback (considerCompletedSearch) runs.
  const runCompletion = async () => {
    await act(async () => {
      vi.advanceTimersByTime(200)
    })
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
  }

  it('feeds the completed snapshot with the next mainline move context', async () => {
    render(
      <AnalysisBoard moves={wiredMoves} boardOrientation="white" sessionId="s1" initialMoveIndex={0} />,
    )
    await runCompletion()
    expect(mockConsiderCompletedSearch).toHaveBeenCalled()
    const [, context] = mockConsiderCompletedSearch.mock.calls.at(-1)!
    expect(context).toMatchObject({
      fenBefore: AFTER_E4,
      moveUci: 'c7c5',
      isMainline: true,
      engineEnabled: true,
    })
    // The hook is initialized with the session so the layer may submit.
    expect(capturedSessionIdRef.current).toBe('s1')
  })

  it('passes a null wire key when no next move exists (latest position)', async () => {
    render(<AnalysisBoard moves={wiredMoves} boardOrientation="white" sessionId="s1" />)
    await runCompletion()
    const call = mockConsiderCompletedSearch.mock.calls.at(-1)
    if (call) {
      expect(call[1]).toMatchObject({ fenBefore: null, moveUci: null })
    }
  })

  it('marks the context non-mainline while in a variation', async () => {
    const node: VarNode = {
      id: 'var-1',
      san: 'Bc4',
      fen: 'rnbqkbnr/pp1ppppp/8/2p5/2B1P3/8/PPPP1PPP/RNBQKNR b KQkq - 1 2',
      fenBefore: AFTER_E4,
      uci: 'f1c4',
      parentId: null,
      parentGameIndex: 1,
      branchPlyOffset: 0,
      children: [],
      nestingLevel: 0,
    }
    mockTree = { nodes: new Map([['var-1', node]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'
    render(
      <AnalysisBoard moves={wiredMoves} boardOrientation="white" sessionId="s1" initialMoveIndex={0} />,
    )
    await runCompletion()
    const call = mockConsiderCompletedSearch.mock.calls.at(-1)
    if (call) {
      expect(call[1]).toMatchObject({ isMainline: false, fenBefore: null })
    }
  })

  it('passes null wire fields for a legacy move', async () => {
    const legacyMoves: AnalysisMove[] = [
      { ...moves[0], fen_before: START, move_uci: 'e2e4', fen_after: AFTER_E4 },
      { ...moves[1], fen_before: null, move_uci: null },
    ]
    render(
      <AnalysisBoard moves={legacyMoves} boardOrientation="white" sessionId="s1" initialMoveIndex={0} />,
    )
    await runCompletion()
    const call = mockConsiderCompletedSearch.mock.calls.at(-1)
    if (call) {
      expect(call[1]).toMatchObject({ fenBefore: null, moveUci: null })
    }
  })
})

describe('buildMainLineMoveDetails wire-field migration (g-cache-stronger-evals)', () => {
  const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

  const move = (over: Partial<AnalysisMove>): AnalysisMove => ({
    move_number: 1,
    color: 'white',
    move_san: 'e4',
    fen_after: AFTER_E4,
    eval_cp: 30,
    eval_mate: null,
    best_move_san: null,
    best_move_eval_cp: null,
    eval_delta: 0,
    classification: 'best',
    ...over,
  })

  it('prefers the wire fen_before over the chain-reconstructed FEN', () => {
    // Clock-drift variant: reconstruction for index 0 would use START.
    const DRIFTED = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 9 5'
    const details = buildMainLineMoveDetails([move({ fen_before: DRIFTED })], START)
    expect(details[0].fenBefore).toBe(DRIFTED)
  })

  it('falls back to reconstruction when the wire fen_before is null (legacy)', () => {
    const details = buildMainLineMoveDetails([move({ fen_before: null })], START)
    expect(details[0].fenBefore).toBe(START)
  })

  it('reconstructs a later move from the previous fen_after when wire is null', () => {
    const details = buildMainLineMoveDetails(
      [
        move({ fen_before: null, move_san: 'e4', fen_after: AFTER_E4 }),
        move({ fen_before: null, color: 'black', move_san: 'e5', fen_after: START }),
      ],
      START,
    )
    expect(details[1].fenBefore).toBe(AFTER_E4)
  })
})

// --------------------------------------------------------------------------- #
// Re-annotation overlay (g-xox0): source-aware precedence + consistent display
// --------------------------------------------------------------------------- #
describe('AnalysisBoard — re-annotation overlay (g-xox0)', () => {
  const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'
  const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1'

  type Upgrade = NonNullable<AnalysisMove['upgraded']>

  const e4Move = (over: Partial<AnalysisMove> = {}): AnalysisMove => ({
    move_number: 1,
    color: 'white',
    move_san: 'e4',
    move_uci: 'e2e4',
    fen_before: START,
    fen_after: AFTER_E4,
    eval_cp: 30,
    eval_mate: null,
    best_move_san: 'd4',
    best_move_eval_cp: 50,
    eval_delta: 60,
    classification: 'excellent',
    ...over,
  })

  const upgrade = (over: Partial<Upgrade> = {}): Upgrade => ({
    classification: 'best',
    eval_cp: 80,
    eval_mate: null,
    best_move_san: 'e4', // played is now best → no contradictory best-arrow
    best_move_eval_cp: 80,
    eval_delta: 0,
    authoritative: false,
    ...over,
  })

  const trustedBest = (bestUci: string) => ({
    [START]: {
      best_move_uci: bestUci,
      best_move_san: bestUci === 'e2e4' ? 'e4' : 'd4',
      best_move_eval_cp: 30,
      best_line_uci: [bestUci, 'e7e5'],
      position_trusted: true,
    },
  })

  const badge = () =>
    (capturedMoveListProps.moves as Array<{ classification: string | null }>)[0]
      .classification
  const listEval = () =>
    (capturedMoveListProps.moves as Array<{ eval: number | null }>)[0].eval

  it('untrusted position: a non-authoritative overlay flips badge, eval, graph, and eval-bar consistently', () => {
    // g-kgiq: no trusted position evidence for this FEN, so the browser-analysis
    // overlay applies. Base 'excellent'/d4-best → upgraded 'best'/played-is-best.
    render(
      <AnalysisBoard
        moves={[e4Move({ upgraded: upgrade({ eval_cp: 80 }) })]}
        boardOrientation="white"
        initialMoveIndex={0}
      />,
    )
    expect(badge()).toBe('best')
    expect(listEval()).toBe(80)
    expect((capturedGraphProps.evals as number[])[0]).toBe(80)
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(80)
    // No stale red "you should have played d4" arrow beside the upgraded best.
    expect(capturedChessboardProps.arrows).toBeUndefined()
  })

  it('trusted position + played==best: a NON-authoritative overlay is SKIPPED (stays best/base eval)', () => {
    render(
      <AnalysisBoard
        moves={[e4Move({ eval_cp: 30, upgraded: upgrade({ classification: 'good', eval_cp: 5 }) })]}
        boardOrientation="white"
        positionAnalysis={trustedBest('e2e4')}
        initialMoveIndex={0}
      />,
    )
    // projectExactBest still promotes played==trusted-best to 'best'; the browser-
    // analysis 'good'/eval-5 was NOT applied (base eval magnitude survives).
    expect(badge()).toBe('best')
    expect(listEval()).toBe(30)
  })

  it('trusted position + played!=best: a NON-authoritative overlay is SKIPPED (keeps base label)', () => {
    // The case promotion-only projectExactBest cannot repair — the SKIP protects it.
    render(
      <AnalysisBoard
        moves={[e4Move({ classification: 'good', upgraded: upgrade({ classification: 'best', eval_cp: 99 }) })]}
        boardOrientation="white"
        positionAnalysis={trustedBest('d2d4')}
        initialMoveIndex={0}
      />,
    )
    expect(badge()).toBe('good')
    expect(listEval()).toBe(30)
  })

  it('trusted position + played==best: an AUTHORITATIVE overlay APPLIES (not suppressed)', () => {
    render(
      <AnalysisBoard
        moves={[e4Move({ eval_cp: 30, upgraded: upgrade({ eval_cp: 99, authoritative: true }) })]}
        boardOrientation="white"
        positionAnalysis={trustedBest('e2e4')}
        initialMoveIndex={0}
      />,
    )
    expect(badge()).toBe('best')
    expect(listEval()).toBe(99) // canonical overlay eval applied
  })

  it('RAW (unprojected) moves still get the trusted-best promotion via the seam', () => {
    // No `upgraded` at all; the board seam re-runs projectExactBest so a
    // played==trusted-best move promotes even for a caller that hands it unprojected
    // moves (drill-review snapshots, direct tests — both review pages project first).
    render(
      <AnalysisBoard
        moves={[e4Move({ classification: 'good', best_move_san: 'd4' })]}
        boardOrientation="white"
        positionAnalysis={trustedBest('e2e4')}
        initialMoveIndex={0}
      />,
    )
    expect(badge()).toBe('best')
  })

  it('an AUTHORITATIVE fetched upgrade is not shadowed by a stale non-authoritative live one', () => {
    // A later refetch brings a canonical (authoritative) upgrade for a key this
    // session already dwelled at d21. The stale non-authoritative live row must not
    // shadow it — and on a trusted position must not cause the skip to drop it.
    render(
      <AnalysisBoard
        moves={[
          e4Move({
            eval_cp: 30,
            upgraded: upgrade({ classification: 'best', eval_cp: 99, authoritative: true }),
          }),
        ]}
        boardOrientation="white"
        sessionId="s1"
        positionAnalysis={trustedBest('e2e4')}
        initialMoveIndex={0}
      />,
    )
    // Authoritative fetched upgrade applied even at the trusted-best played move.
    expect(badge()).toBe('best')
    expect(listEval()).toBe(99)
    // A stale non-authoritative live upgrade arrives for the same key.
    const onAcceptedStale = capturedOnAcceptedRef.current as (
      fen: string,
      uci: string,
      up: Upgrade,
    ) => void
    act(() => onAcceptedStale(START, 'e2e4', upgrade({ classification: 'good', eval_cp: 5 })))
    // Canonical truth still wins; it is neither shadowed nor dropped by the skip.
    expect(badge()).toBe('best')
    expect(listEval()).toBe(99)
  })

  it('live path: onAcceptedEvidence patches the open MoveList immediately', () => {
    render(
      <AnalysisBoard
        moves={[e4Move()]}
        boardOrientation="white"
        sessionId="s1"
        initialMoveIndex={0}
      />,
    )
    expect(badge()).toBe('excellent')
    const onAccepted = capturedOnAcceptedRef.current as (
      fen: string,
      uci: string,
      up: Upgrade,
    ) => void
    act(() => onAccepted(START, 'e2e4', upgrade({ eval_cp: 88 })))
    expect(badge()).toBe('best')
    expect(listEval()).toBe(88)
  })
})

// ---------------------------------------------------------------------------
// Displayed main-line index callback (g-m1xc)
// ---------------------------------------------------------------------------

describe('AnalysisBoard — onDisplayedMainlineIndexChange', () => {
  const varNode: VarNode = {
    id: 'var-1',
    san: 'Bc4',
    fen: 'rnbqkbnr/pp1ppppp/8/2p5/2B1P3/8/PPPP1PPP/RNBQKNR b KQkq - 1 2',
    fenBefore: moves[0].fen_after,
    uci: 'f1c4',
    parentId: null,
    parentGameIndex: 1,
    branchPlyOffset: 0,
    children: [],
    nestingLevel: 0,
  }

  it('emits initialMoveIndex on mount', () => {
    const onChange = vi.fn()
    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        initialMoveIndex={0}
        onDisplayedMainlineIndexChange={onChange}
      />,
    )

    expect(onChange).toHaveBeenLastCalledWith(0)
  })

  it('normalizes "latest" (no current index) to the final move index', () => {
    const onChange = vi.fn()
    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        onDisplayedMainlineIndexChange={onChange}
      />,
    )

    // Mounting with no initialMoveIndex is the "latest" state, which is the last
    // played move — not null.
    expect(onChange).toHaveBeenLastCalledWith(moves.length - 1)

    // Navigating back and then returning to latest resolves the same way.
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))
    expect(onChange).toHaveBeenLastCalledWith(0)
    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))
    expect(onChange).toHaveBeenLastCalledWith(moves.length - 1)
  })

  it('emits -1 for an empty move list (starting position)', () => {
    const onChange = vi.fn()
    render(
      <AnalysisBoard
        moves={[]}
        boardOrientation="white"
        onDisplayedMainlineIndexChange={onChange}
      />,
    )

    expect(onChange).toHaveBeenLastCalledWith(-1)
  })

  it('follows MoveList and graph navigation', () => {
    const onChange = vi.fn()
    render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        initialMoveIndex={0}
        onDisplayedMainlineIndexChange={onChange}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Move 2' }))
    expect(onChange).toHaveBeenLastCalledWith(1)

    const onSelectMove = capturedGraphProps.onSelectMove as (index: number) => void
    act(() => onSelectMove(0))
    expect(onChange).toHaveBeenLastCalledWith(0)
  })

  it('emits null in a variation and the main-line index on return', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <AnalysisBoard
        moves={moves}
        boardOrientation="white"
        initialMoveIndex={1}
        onDisplayedMainlineIndexChange={onChange}
      />,
    )
    expect(onChange).toHaveBeenLastCalledWith(1)

    // Entering a hypothetical variation: the board is no longer on a played move.
    // (A fresh `moves` array defeats the memo so the mocked tree state is re-read.)
    mockTree = { nodes: new Map([['var-1', varNode]]), rootBranches: new Map([[1, ['var-1']]]) }
    mockSelectedVarNodeId = 'var-1'
    rerender(
      <AnalysisBoard
        moves={[...moves]}
        boardOrientation="white"
        initialMoveIndex={1}
        onDisplayedMainlineIndexChange={onChange}
      />,
    )
    expect(onChange).toHaveBeenLastCalledWith(null)

    // Leaving it restores the displayed main-line move.
    mockSelectedVarNodeId = null
    rerender(
      <AnalysisBoard
        moves={[...moves]}
        boardOrientation="white"
        initialMoveIndex={1}
        onDisplayedMainlineIndexChange={onChange}
      />,
    )
    expect(onChange).toHaveBeenLastCalledWith(1)
  })
})
