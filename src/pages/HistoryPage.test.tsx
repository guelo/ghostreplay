import { describe, it, expect, vi, beforeEach, beforeAll } from 'vitest';
import { forwardRef, useImperativeHandle } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import HistoryPage from './HistoryPage';

const setMatchMedia = (matches: boolean) => {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
};

// Spy on navigation (Start Drill entry point) while keeping the rest of the router.
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});

// jsdom doesn't have matchMedia — stub it for useTouchOnly
beforeAll(() => {
  setMatchMedia(false);
});

// Mock the API module
const mockFetchHistory = vi.fn();
const mockFetchAnalysis = vi.fn();
const mockFetchSessionOpenings = vi.fn();
vi.mock('../utils/api', async () => {
  const actual = await vi.importActual('../utils/api');
  return {
    ...actual,
    fetchHistory: (...args: unknown[]) => mockFetchHistory(...args),
    fetchAnalysis: (...args: unknown[]) => mockFetchAnalysis(...args),
    fetchSessionOpenings: (...args: unknown[]) => mockFetchSessionOpenings(...args),
  };
});

// Mock AnalysisBoard to avoid pulling in chess rendering. Render the footer so
// the opening lineage block (passed via footer) is exercised. Use forwardRef +
// useImperativeHandle so the HistoryPage ref's jumpToMove is observable.
const mockJumpToMove = vi.fn();
vi.mock('../components/AnalysisBoard', () => ({
  default: forwardRef(
    (
      {
        boardOrientation,
        initialMoveIndex,
        footer,
        mobileToolbar,
      }: {
        boardOrientation: string;
        initialMoveIndex?: number;
        footer?: React.ReactNode;
        mobileToolbar?: React.ReactNode;
      },
      ref: React.Ref<{ jumpToMove: (index: number) => void }>,
    ) => {
      useImperativeHandle(ref, () => ({ jumpToMove: mockJumpToMove }), []);
      return (
        <div
          data-testid="analysis-board"
          data-orientation={boardOrientation}
          data-initial-move={initialMoveIndex}
        >
          {mobileToolbar}
          {footer}
        </div>
      );
    },
  ),
}));

// Mock AppNav
vi.mock('../components/AppNav', () => ({
  default: () => <nav data-testid="app-nav" />,
}));

const captureEventMock = vi.fn();
vi.mock('../analytics/posthog', () => ({
  captureEvent: (...args: unknown[]) => captureEventMock(...args),
}));

// Mock react-chessboard so expanding an opening card doesn't pull in real rendering.
vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: Record<string, unknown> }) => (
    <div data-testid="card-board" data-position={options.position as string} />
  ),
}));

const HISTORY_RESPONSE = [
  {
    session_id: 'abc-123',
    player_color: 'white',
    result: 'checkmate_win',
    engine_elo: 1500,
    ended_at: '2026-04-20T12:00:00Z',
    opening_name: 'Sicilian Defense',
    summary: { total_moves: 20, blunders: 0, mistakes: 1, inaccuracies: 2, average_centipawn_loss: 15, accuracy: 88 },
  },
];

const ANALYSIS_RESPONSE = {
  session_id: 'abc-123',
  player_color: 'white',
  moves: [
    { move_san: 'e4', fen_after: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1' },
    { move_san: 'c5', fen_after: 'rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2' },
    { move_san: 'Nf3', fen_after: 'rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2' },
  ],
  position_analysis: {},
  is_complete: true,
  summary: { total_moves: 3, blunders: 0, mistakes: 0, inaccuracies: 0, average_centipawn_loss: 0, accuracy: 88 },
};

describe('HistoryPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setMatchMedia(false);
    mockFetchSessionOpenings.mockResolvedValue({ player_color: 'white', lineage: [] });
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

  it('captures history_game_selected when a different game is chosen', async () => {
    const user = userEvent.setup();
    const twoGames = [
      HISTORY_RESPONSE[0],
      {
        ...HISTORY_RESPONSE[0],
        session_id: 'def-456',
        result: 'resign',
        opening_name: 'French Defense',
      },
    ];
    mockFetchHistory.mockResolvedValue(twoGames);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    // First game auto-selects on load (does not emit a selection event).
    await screen.findByTestId('analysis-board');
    expect(captureEventMock).not.toHaveBeenCalled();

    // Open the dropdown and pick the second game.
    await user.click(screen.getByRole('button', { name: /Win vs 1500/ }));
    await user.click(screen.getAllByRole('option')[1]);

    expect(captureEventMock).toHaveBeenCalledWith('history_game_selected', {
      session_id: 'def-456',
      result: 'resign',
    });
  });

  it('places the game selector inside the analysis board on narrow screens', async () => {
    setMatchMedia(true);
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    const board = await screen.findByTestId('analysis-board');
    const selector = screen.getByRole('button', { name: /Win vs 1500/ });

    expect(board).toContainElement(selector);
    expect(screen.getAllByRole('button', { name: /Win vs 1500/ })).toHaveLength(1);
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

  it('fetches and renders the opening lineage once analysis moves arrive', async () => {
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockFetchSessionOpenings.mockResolvedValue({
      player_color: 'white',
      lineage: [
        {
          opening_key: 'key-ruy',
          opening_name: 'Ruy Lopez',
          opening_family: 'Ruy Lopez',
          eco: 'C60',
          depth: 0,
          score: 72,
          confidence: 0.8,
          coverage: 0.5,
          sample_size: 10,
          game_count: 3,
          path: [],
        },
      ],
    });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Ruy Lopez')).toBeInTheDocument();
    });

    expect(mockFetchSessionOpenings).toHaveBeenCalledWith(
      'abc-123',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(screen.getByRole('region', { name: 'Openings played' })).toBeInTheDocument();
  });

  it('clicking an opening chip jumps the board to the matching move index', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockFetchSessionOpenings.mockResolvedValue({
      player_color: 'white',
      lineage: [
        {
          // opening_key normalizes to ANALYSIS_RESPONSE.moves[2].fen_after, so a
          // correct lookup yields index 2 (guards against a hardcoded-zero jump).
          opening_key: 'rnbqkbnr/pp1ppppp/8/2p5/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2',
          opening_name: 'Open Game',
          opening_family: 'Open Game',
          eco: 'C20',
          depth: 0,
          score: 60,
          confidence: 0.7,
          coverage: 0.5,
          sample_size: 8,
          game_count: 2,
          path: [],
        },
      ],
    });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Open Game')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Select Open Game/ }));

    expect(mockJumpToMove).toHaveBeenCalledWith(2);
  });

  it('clicking an opening chip with no matching move does not jump the board', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockFetchSessionOpenings.mockResolvedValue({
      player_color: 'white',
      lineage: [
        {
          opening_key: 'unmatched-fen-key',
          opening_name: 'Mystery Line',
          opening_family: 'Mystery',
          eco: null,
          depth: 0,
          score: 40,
          confidence: 0.3,
          coverage: 0.2,
          sample_size: 2,
          game_count: 1,
          path: [],
        },
      ],
    });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Mystery Line')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Select Mystery Line/ }));

    expect(mockJumpToMove).not.toHaveBeenCalled();
  });

  it('Start Drill from an opening chip navigates to /play with drillSetup state', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue(HISTORY_RESPONSE);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);
    mockFetchSessionOpenings.mockResolvedValue({
      player_color: 'white',
      lineage: [
        {
          opening_key: 'key-ruy',
          opening_name: 'Ruy Lopez',
          opening_family: 'Ruy Lopez',
          eco: 'C60',
          depth: 0,
          score: 72,
          confidence: 0.8,
          coverage: 0.5,
          sample_size: 10,
          game_count: 3,
          path: [],
        },
      ],
    });

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByText('Ruy Lopez')).toBeInTheDocument();
    });

    await user.click(screen.getByRole('button', { name: /Select Ruy Lopez/ }));
    await user.click(screen.getByRole('button', { name: 'Start Drill' }));

    expect(mockNavigate).toHaveBeenCalledWith('/play', {
      state: { drillSetup: { openingKey: 'key-ruy', playerColor: 'white' } },
    });
  });

  it('does not fetch openings when analysis has no moves (avoids stale-empty race)', async () => {
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

    expect(mockFetchSessionOpenings).not.toHaveBeenCalled();
  });

  it('selecting a different game in the dropdown updates the analysis', async () => {
    const user = userEvent.setup();
    mockFetchHistory.mockResolvedValue([
      ...HISTORY_RESPONSE,
      {
        session_id: 'def-456',
        player_color: 'black',
        result: 'draw',
        engine_elo: 1700,
        ended_at: '2026-04-21T12:00:00Z',
        opening_name: 'French Defense',
        summary: { total_moves: 30, blunders: 1, mistakes: 0, inaccuracies: 1, average_centipawn_loss: 18, accuracy: 80 },
      },
    ]);
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    render(
      <MemoryRouter>
        <HistoryPage />
      </MemoryRouter>
    );

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });
    expect(mockFetchAnalysis).toHaveBeenCalledWith('abc-123');

    await user.click(screen.getByRole('button', { name: /Win vs 1500/ }));
    await user.click(screen.getByText('French Defense'));

    await waitFor(() => {
      expect(mockFetchAnalysis).toHaveBeenCalledWith('def-456');
    });
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
