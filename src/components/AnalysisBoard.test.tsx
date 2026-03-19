import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '../test/utils'
import AnalysisBoard from './AnalysisBoard'
import type { AnalysisMove } from '../utils/api'

vi.mock('../hooks/useMoveAnalysis', () => ({
  useMoveAnalysis: () => ({
    analyzeMove: vi.fn(),
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

vi.mock('react-chessboard', () => ({
  Chessboard: () => <div data-testid="chessboard" />,
}))

vi.mock('./EvalBar', () => ({
  default: () => <div data-testid="eval-bar" />,
}))

let capturedGraphProps: Record<string, unknown> = {}

vi.mock('./AnalysisGraph', () => ({
  default: (props: Record<string, unknown>) => {
    capturedGraphProps = props
    return <div data-testid="analysis-graph" />
  },
}))

vi.mock('./MoveList', () => ({
  default: ({
    moves,
    onNavigate,
    playerColor,
  }: {
    moves: Array<{ san: string }>
    onNavigate: (index: number | null) => void
    playerColor?: 'white' | 'black'
  }) => (
    <div>
      <div data-testid="move-list-player-color">{playerColor ?? 'unset'}</div>
      {moves.map((move, index) => (
        <button
          key={`${move.san}-${index}`}
          type="button"
          onClick={() => onNavigate(index)}
        >
          Move {index + 1}
        </button>
      ))}
      <button type="button" onClick={() => onNavigate(null)}>
        Latest
      </button>
    </div>
  ),
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
