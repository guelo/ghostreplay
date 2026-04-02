import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import GameAnalysisPage from './GameAnalysisPage';

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

// Mock the API module — re-export ApiError so the component can instanceof-check it
import { ApiError } from '../utils/api';
const mockFetchAnalysis = vi.fn();
vi.mock('../utils/api', async () => {
  const actual = await vi.importActual('../utils/api');
  return { ...actual, fetchAnalysis: (...args: unknown[]) => mockFetchAnalysis(...args) };
});

// Mock AnalysisBoard to avoid pulling in chess rendering
vi.mock('../components/AnalysisBoard', () => ({
  default: ({ boardOrientation }: { boardOrientation: string }) => (
    <div data-testid="analysis-board" data-orientation={boardOrientation} />
  ),
}));

// Mock AppNav
vi.mock('../components/AppNav', () => ({
  default: () => <nav data-testid="app-nav" />,
}));

function renderPage(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <GameAnalysisPage />
    </MemoryRouter>,
  );
}

// We need to wrap in Routes to test Navigate redirect
import { Routes, Route } from 'react-router-dom';

function renderWithRoutes(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/game" element={<GameAnalysisPage />} />
        <Route path="/play" element={<div data-testid="play-page" />} />
      </Routes>
    </MemoryRouter>,
  );
}

const ANALYSIS_RESPONSE = {
  session_id: 'abc-123',
  pgn: '1. e4 e5',
  result: 'checkmate_win',
  player_color: 'black',
  moves: [
    {
      move_number: 1,
      color: 'white',
      move_san: 'e4',
      fen_after: 'fen1',
      eval_cp: 20,
      eval_mate: null,
      best_move_san: 'e4',
      best_move_eval_cp: 20,
      eval_delta: 0,
      classification: null,
    },
    {
      move_number: 1,
      color: 'black',
      move_san: 'e5',
      fen_after: 'fen2',
      eval_cp: 15,
      eval_mate: null,
      best_move_san: 'e5',
      best_move_eval_cp: 15,
      eval_delta: 5,
      classification: null,
    },
  ],
  summary: { blunders: 0, mistakes: 0, inaccuracies: 0, average_centipawn_loss: 2 },
  position_analysis: {},
  expected_total_moves: 2,
  analyzed_moves: 2,
  is_complete: true,
};

describe('GameAnalysisPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('redirects to /play when no id param is present', () => {
    renderWithRoutes('/game');
    expect(screen.getByTestId('play-page')).toBeInTheDocument();
  });

  it('fetches analysis and renders board with correct orientation from player_color', async () => {
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    renderPage('/game?id=abc-123');

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(mockFetchAnalysis).toHaveBeenCalledWith('abc-123');
    expect(screen.getByTestId('analysis-board')).toHaveAttribute(
      'data-orientation',
      'black',
    );
  });

  it('shows loading state initially', () => {
    mockFetchAnalysis.mockReturnValue(new Promise(() => {})); // never resolves
    renderPage('/game?id=abc-123');
    expect(screen.getByText('Loading analysis...')).toBeInTheDocument();
  });

  it('shows processing UI and polls through transient errors', async () => {
    mockFetchAnalysis.mockRejectedValue(new Error('Network error'));

    renderPage('/game?id=abc-123');

    await waitFor(() => {
      expect(screen.queryByText('Loading analysis...')).not.toBeInTheDocument();
    });

    // No terminal error — shows processing indicator while retrying
    expect(screen.queryByText('Network error')).not.toBeInTheDocument();
    expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
    expect(screen.getByText(/Analysis still processing/)).toBeInTheDocument();
  });

  it('shows backend error immediately for permanent 4xx failures', async () => {
    mockFetchAnalysis.mockRejectedValue(
      new ApiError('Game session not found', { status: 404 }),
    );

    renderPage('/game?id=bad-id');

    await waitFor(() => {
      expect(screen.getByText('Game session not found')).toBeInTheDocument();
    });

    // Should NOT show processing/retry UI
    expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
  });

  it('shows backend error immediately for 403 forbidden', async () => {
    mockFetchAnalysis.mockRejectedValue(
      new ApiError('Not authorized to access this game', { status: 403 }),
    );

    renderPage('/game?id=someone-elses-game');

    await waitFor(() => {
      expect(screen.getByText('Not authorized to access this game')).toBeInTheDocument();
    });
  });

  it('shows error when player_color is missing from response', async () => {
    const response = { ...ANALYSIS_RESPONSE, player_color: undefined };
    mockFetchAnalysis.mockResolvedValue(response);

    renderPage('/game?id=abc-123');

    await waitFor(() => {
      expect(
        screen.getByText('Analysis response missing player color. Please try again later.'),
      ).toBeInTheDocument();
    });

    // Board should NOT render
    expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();
  });
});
