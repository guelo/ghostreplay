import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HistoryPage from './HistoryPage';

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
const mockFetchHistory = vi.fn();
const mockFetchAnalysis = vi.fn();
vi.mock('../utils/api', async () => {
  const actual = await vi.importActual('../utils/api');
  return {
    ...actual,
    fetchHistory: (...args: unknown[]) => mockFetchHistory(...args),
    fetchAnalysis: (...args: unknown[]) => mockFetchAnalysis(...args),
  };
});

// Mock AnalysisBoard to avoid pulling in chess rendering
vi.mock('../components/AnalysisBoard', () => ({
  default: ({ boardOrientation, initialMoveIndex }: { boardOrientation: string; initialMoveIndex?: number }) => (
    <div
      data-testid="analysis-board"
      data-orientation={boardOrientation}
      data-initial-move={initialMoveIndex}
    />
  ),
}));

// Mock AppNav
vi.mock('../components/AppNav', () => ({
  default: () => <nav data-testid="app-nav" />,
}));

const HISTORY_RESPONSE = [
  {
    session_id: 'abc-123',
    player_color: 'white',
    result: 'checkmate_win',
    engine_elo: 1500,
    ended_at: '2026-04-20T12:00:00Z',
    summary: { total_moves: 20, blunders: 0, mistakes: 1, inaccuracies: 2, average_centipawn_loss: 15 },
  },
];

const ANALYSIS_RESPONSE = {
  session_id: 'abc-123',
  player_color: 'white',
  moves: [
    { move_san: 'e4', fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' }
  ],
  position_analysis: {},
  is_complete: true,
};

describe('HistoryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('fetches history and analysis, then renders board with initialMoveIndex=0 for non-empty game', async () => {
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(mockFetchHistory).toHaveBeenCalled();
    expect(mockFetchAnalysis).toHaveBeenCalledWith('abc-123');
    expect(screen.getByTestId('analysis-board')).toHaveAttribute('data-initial-move', '0');
  });

  it('fetches history and analysis, then renders board without initialMoveIndex for empty game', async () => {
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue({ ...ANALYSIS_RESPONSE, moves: [] });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(screen.getByTestId('analysis-board')).not.toHaveAttribute('data-initial-move');
  });

  it('shows empty state when no games played', async () => {
    mockFetchHistory.mockResolvedValue([]);

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('No games played yet')).toBeInTheDocument();
    });
  });
});
