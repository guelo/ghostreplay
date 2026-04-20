import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BlundersPage from './BlundersPage';

// jsdom doesn't have matchMedia — stub it for useTouchOnly
beforeAll(() => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

// Mock the API module
const mockFetchBlunders = vi.fn();
const mockFetchAnalysis = vi.fn();
vi.mock('../utils/api', async () => {
  const actual = await vi.importActual('../utils/api');
  return {
    ...actual,
    fetchBlunders: (...args: unknown[]) => mockFetchBlunders(...args),
    fetchAnalysis: (...args: unknown[]) => mockFetchAnalysis(...args),
  };
});

// Mock AnalysisBoard
vi.mock('../components/AnalysisBoard', () => ({
  default: ({ initialMoveIndex }: { initialMoveIndex?: number }) => (
    <div
      data-testid="analysis-board"
      data-initial-move={initialMoveIndex === undefined ? 'undefined' : initialMoveIndex}
    />
  ),
}));

// Mock AppNav
vi.mock('../components/AppNav', () => ({
  default: () => <nav data-testid="app-nav" />,
}));

const BLUNDERS_RESPONSE = [
  {
    id: 1,
    fen: 'r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3',
    bad_move: 'Bc4',
    best_move: 'Bb5',
    eval_loss_cp: 100,
    srs_priority: 1.5,
    last_session_id: 'session-123',
    created_at: '2026-04-20T12:00:00Z',
  },
];

const ANALYSIS_RESPONSE = {
  session_id: 'session-123',
  player_color: 'white',
  moves: [
    { move_san: 'e4', fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' },
    { move_san: 'e5', fen_after: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2' },
    { move_san: 'Nf3', fen_after: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2' },
    { move_san: 'Nc6', fen_after: 'r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3' },
    { move_san: 'Bc4', fen_after: 'r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3' },
  ],
  position_analysis: {},
  is_complete: true,
};

describe('BlundersPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('passes correct initialMoveIndex when move is found in analysis', async () => {
    mockFetchBlunders.mockResolvedValue(BLUNDERS_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    // The blunder FEN matches the position BEFORE Bc4 (index 4)
    expect(screen.getByTestId('analysis-board')).toHaveAttribute('data-initial-move', '4');
  });

  it('falls back to undefined (latest) when move is not found in analysis', async () => {
    mockFetchBlunders.mockResolvedValue(BLUNDERS_RESPONSE);
    // Return analysis that doesn't contain the blunder move
    mockFetchAnalysis.mockResolvedValue({
      ...ANALYSIS_RESPONSE,
      moves: [{ move_san: 'd4', fen_after: '...' }],
    });

    render(
      <MemoryRouter>
        <BlundersPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(screen.getByTestId('analysis-board')).toHaveAttribute('data-initial-move', 'undefined');
  });
});
