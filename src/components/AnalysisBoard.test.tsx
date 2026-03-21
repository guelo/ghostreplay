import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '../test/utils'
import AnalysisBoard from './AnalysisBoard'
import type { AnalysisMove } from '../utils/api'
import type { VariationTree, VarNode } from '../types/variationTree'
import { createEmptyTree } from '../types/variationTree'

// --- Mutable mock state for useVariationTree ---

const mockAddMove = vi.fn(() => 'mock-node-id')
const mockSetSelectedVarNode = vi.fn()
const mockNavigateUp = vi.fn()
const mockNavigateDown = vi.fn(() => null)
const mockGetAbsolutePly = vi.fn(() => 0)
const mockGetVarAnalysis = vi.fn(() => undefined)
const mockRegisterPending = vi.fn()
const mockResolvePending = vi.fn()
const mockClearTree = vi.fn()
const mockPendingRequestsRef = { current: new Map<string, string>() }

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
    clearTree: mockClearTree,
    pendingRequestsRef: mockPendingRequestsRef,
    varAnalysisCacheRef: { current: new Map() },
    collectBranchNodes: vi.fn(() => []),
  }),
}))

const mockAnalyzeMove = vi.fn(() => 'req-123')

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: mockAnalyzeMove,
    analysisMap: new Map(),
    lastAnalysis: null,
    clearAnalysis: vi.fn(),
  }),
}))

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: () => ({
    info: [],
    isThinking: false,
    evaluatePosition: vi.fn(async () => {}),
    stopSearch: vi.fn(),
  }),
}))

// --- Prop-capturing mocks ---

let capturedChessboardProps: Record<string, unknown> = {}

vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => {
    capturedChessboardProps = options
    return <div data-testid="chessboard" />
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

vi.mock('./MoveList', () => ({
  default: (props: {
    moves: Array<{ san: string }>
    onNavigate: (index: number | null) => void
    playerColor?: 'white' | 'black'
    [key: string]: unknown
  }) => {
    capturedMoveListProps = props
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
  capturedEvalBarProps = {}
  capturedGraphProps = {}
  capturedMoveListProps = {}
  mockTree = createEmptyTree()
  mockSelectedVarNodeId = null
  mockPendingRequestsRef.current.clear()
  vi.clearAllMocks()
})

describe('AnalysisBoard position info', () => {
  it('shows played move eval, best move eval, eval delta, and classification', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.getByText('Played:')).toBeInTheDocument()
    expect(screen.getByText('c5')).toBeInTheDocument()
    expect(screen.getByText('(+1.2)')).toBeInTheDocument()

    expect(screen.getByText('Best:')).toBeInTheDocument()
    expect(screen.getByText('e5')).toBeInTheDocument()
    expect(screen.getByText('(+0.2)')).toBeInTheDocument()

    expect(screen.getByText('Delta:')).toBeInTheDocument()
    expect(screen.getByText('+100 cp')).toBeInTheDocument()
    expect(screen.getByText('inaccuracy')).toBeInTheDocument()
  })

  it('updates panel values when navigating to a different move', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    expect(screen.getAllByText('e4').length).toBeGreaterThan(0)
    expect(screen.getAllByText('(+0.3)').length).toBeGreaterThan(0)
    expect(screen.getByText('+0 cp')).toBeInTheDocument()
    expect(screen.getByText('best')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Latest' }))

    expect(screen.getByText('c5')).toBeInTheDocument()
    expect(screen.getByText('+100 cp')).toBeInTheDocument()
    expect(screen.getByText('inaccuracy')).toBeInTheDocument()
  })

  it('passes player color to MoveList from board orientation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    expect(screen.getByTestId('move-list-player-color')).toHaveTextContent('black')
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

  it('uses variation-cached eval for eval bar when in variation', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'
    // Return cached analysis for this variation FEN
    mockGetVarAnalysis.mockImplementation((fen: string) => {
      if (fen === varNodeFen) return { playedEval: 50, id: 'req-1', move: 'Bc4', bestMove: 'Nf3', bestEval: 30, currentPositionEval: null, moveIndex: null, delta: null, classification: null, blunder: false }
      return undefined
    })

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // White orientation: playerToWhite(50, 'white') = 50
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(50)
  })

  it('uses game eval for eval bar when not in variation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    // Last move (index 1): eval_cp = -120, white perspective = +120
    expect(capturedEvalBarProps.whitePerspectiveCp).toBe(120)
  })

  it('hides analysis graph when in variation', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.queryByTestId('analysis-graph')).not.toBeInTheDocument()
  })

  it('hides position info panel when in variation', () => {
    const node = makeVarNode()
    mockTree = { nodes: new Map([['var-node-1', node]]), rootBranches: new Map([[1, ['var-node-1']]]) }
    mockSelectedVarNodeId = 'var-node-1'

    render(<AnalysisBoard moves={moves} boardOrientation="white" />)

    expect(screen.queryByText('Played:')).not.toBeInTheDocument()
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
