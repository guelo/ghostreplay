import { Chess } from 'chess.js'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '../test/utils'
import AnalysisBoard from './AnalysisBoard'
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
const {
  mockEngineInfoRef,
  mockEngineInfoFenRef,
  mockEvaluatePosition,
  mockStopSearch,
  mockUseStockfishEngine,
} = vi.hoisted(() => {
  const mockEngineInfoRef = { current: [] as EngineInfo[] }
  const mockEngineInfoFenRef = { current: null as string | null }
  const mockEvaluatePosition = vi.fn(async () => {})
  const mockStopSearch = vi.fn()
  const mockUseStockfishEngine = vi.fn((_options?: { enabled?: boolean }) => ({
    info: mockEngineInfoRef.current,
    infoFen: mockEngineInfoFenRef.current,
    isThinking: false,
    evaluatePosition: mockEvaluatePosition,
    stopSearch: mockStopSearch,
  }))

  return {
    mockEngineInfoRef,
    mockEngineInfoFenRef,
    mockEvaluatePosition,
    mockStopSearch,
    mockUseStockfishEngine,
  }
})

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: mockAnalyzeMove,
    analysisMap: new Map(),
    lastAnalysis: null,
    clearAnalysis: vi.fn(),
  }),
}))

vi.mock('../hooks/useStockfishEngine', () => ({
  useStockfishEngine: mockUseStockfishEngine,
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

const capturedMaterialDisplays: Array<{ fen: string; perspective: string }> = []

vi.mock('./MaterialDisplay', () => ({
  default: (props: { fen: string; perspective: string }) => {
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
  capturedEvalBarProps = {}
  capturedGraphProps = {}
  capturedMoveListProps = {}
  capturedMaterialDisplays.length = 0
  mockTree = createEmptyTree()
  mockSelectedVarNodeId = null
  mockEngineInfoRef.current = []
  mockEngineInfoFenRef.current = null
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

describe('AnalysisBoard MoveList', () => {
  it('passes player color to MoveList from board orientation', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="black" />)

    expect(screen.getByTestId('move-list-player-color')).toHaveTextContent('black')
  })

  it('initializes to initialMoveIndex when provided', () => {
    render(<AnalysisBoard moves={moves} boardOrientation="white" initialMoveIndex={0} />)

    expect(capturedChessboardProps.position).toBe(moves[0].fen_after)
    expect(capturedMoveListProps.currentIndex).toBe(0)
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

    fireEvent.click(screen.getByLabelText('Engine lines'))

    expect(screen.queryByText('d12')).not.toBeInTheDocument()
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

    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    expect(screen.queryByText('d12')).not.toBeInTheDocument()
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
      if (fen === varNodeFen) return { playedEval: 50, id: 'req-1', move: 'Bc4', bestMove: 'Nf3', bestEval: 30, currentPositionEval: null, moveIndex: null, delta: null, classification: null, blunder: false, recordable: false }
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
    return onDrop({ sourceSquare: source, targetSquare: target })
  }

  it('main-line continuation: advances cursor instead of creating variation', () => {
    // Navigate to move 0 (e4), then play the next game move (c5)
    render(<AnalysisBoard moves={moves} boardOrientation="white" />)
    // Navigate to move 0 (e4)
    fireEvent.click(screen.getByRole('button', { name: 'Move 1' }))

    // The displayed FEN should be after e4, and the next game move is c5 (c7c5)
    let result: boolean
    act(() => {
      result = invokeDrop('c7', 'c5')
    })

    expect(result!).toBe(true)
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
      bestEval: 20, currentPositionEval: null, moveIndex: null,
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

    let result: boolean
    act(() => {
      result = invokeDrop('g1', 'f3')
    })

    expect(result!).toBe(true)
    expect(mockAddMove).not.toHaveBeenCalled()
    // handleDrop calls setCurrentIndex(null) for last move — verify via MoveList prop
    expect(capturedMoveListProps.currentIndex).toBeNull()
  })
})
