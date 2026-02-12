import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '../test/utils'
import AnalysisBoard from './AnalysisBoard'
import type { AnalysisMove } from '../utils/api'

vi.mock('react-chessboard', () => ({
  Chessboard: () => <div data-testid="chessboard" />,
}))

vi.mock('./EvalBar', () => ({
  default: () => <div data-testid="eval-bar" />,
}))

vi.mock('./AnalysisGraph', () => ({
  default: () => <div data-testid="analysis-graph" />,
}))

vi.mock('./MoveList', () => ({
  default: ({
    moves,
    onNavigate,
  }: {
    moves: Array<{ san: string }>
    onNavigate: (index: number | null) => void
  }) => (
    <div>
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
})
