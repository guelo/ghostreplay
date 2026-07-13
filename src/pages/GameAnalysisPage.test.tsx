import { StrictMode, forwardRef, useImperativeHandle } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach, beforeAll } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useNavigate } from 'react-router-dom';
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

// Mock AnalysisBoard to avoid pulling in chess rendering. Render the footer so
// the stats pane is exercised, and use forwardRef + useImperativeHandle so the
// page ref's jumpToMove (board cycling) is observable — this is the guard for the
// onJumpToMove/ref wiring the page needs for header/cell cycling to move the board.
const mockJumpToMove = vi.fn();
vi.mock('../components/AnalysisBoard', () => ({
  default: forwardRef(
    (
      {
        boardOrientation,
        initialMoveIndex,
        sessionId,
        footer,
      }: {
        boardOrientation: string;
        initialMoveIndex?: number;
        sessionId?: string;
        footer?: React.ReactNode;
      },
      ref: React.Ref<{ jumpToMove: (index: number) => void }>,
    ) => {
      useImperativeHandle(ref, () => ({ jumpToMove: mockJumpToMove }), []);
      return (
        <div
          data-testid="analysis-board"
          data-orientation={boardOrientation}
          data-initial-move={initialMoveIndex}
          data-session-id={sessionId}
        >
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

function renderPage(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <GameAnalysisPage />
    </MemoryRouter>,
  );
}

function renderStrictPage(path: string) {
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[path]}>
        <GameAnalysisPage />
      </MemoryRouter>
    </StrictMode>,
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
  summary: { blunders: 0, mistakes: 0, inaccuracies: 0, average_centipawn_loss: 2, accuracy: 91 },
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
    expect(screen.getByTestId('analysis-board')).toHaveAttribute(
      'data-initial-move',
      '0',
    );
    // Passes the page's saved-game session id to the evidence driver.
    expect(screen.getByTestId('analysis-board')).toHaveAttribute(
      'data-session-id',
      'abc-123',
    );
  });

  it('cycles the board through the player union when the You header is clicked', async () => {
    const user = userEvent.setup();
    // player_color = black → player moves at odd indices: blunder@1, mistake@3,
    // inaccuracy@5. Union sorted by index = [1, 3, 5]. Empty position_analysis
    // means projectExactBest leaves these classifications untouched.
    mockFetchAnalysis.mockResolvedValue({
      ...ANALYSIS_RESPONSE,
      moves: [
        { move_number: 1, color: 'white', move_san: 'e4', fen_after: 'fen0', classification: null },
        { move_number: 1, color: 'black', move_san: 'e5', fen_after: 'fen1', classification: 'blunder' },
        { move_number: 2, color: 'white', move_san: 'Nf3', fen_after: 'fen2', classification: null },
        { move_number: 2, color: 'black', move_san: 'Nc6', fen_after: 'fen3', classification: 'mistake' },
        { move_number: 3, color: 'white', move_san: 'Bb5', fen_after: 'fen4', classification: null },
        { move_number: 3, color: 'black', move_san: 'a6', fen_after: 'fen5', classification: 'inaccuracy' },
      ],
    });

    renderPage('/game?id=abc-123');

    await screen.findByTestId('analysis-board');

    const you = screen.getByRole('button', {
      name: /all of your blunders, mistakes, and inaccuracies/i,
    });
    await user.click(you);
    expect(mockJumpToMove).toHaveBeenLastCalledWith(1);
    await user.click(you);
    expect(mockJumpToMove).toHaveBeenLastCalledWith(3);
  });

  it('reuses the initial analysis request during StrictMode effect replay', async () => {
    mockFetchAnalysis.mockResolvedValue(ANALYSIS_RESPONSE);

    renderStrictPage('/game?id=abc-123');

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(mockFetchAnalysis).toHaveBeenCalledTimes(1);
    expect(mockFetchAnalysis).toHaveBeenCalledWith('abc-123');
  });

  it('fetches analysis and renders board without initialMoveIndex for empty game', async () => {
    mockFetchAnalysis.mockResolvedValue({ ...ANALYSIS_RESPONSE, moves: [] });

    renderPage('/game?id=abc-123');

    await waitFor(() => {
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
    });

    expect(screen.getByTestId('analysis-board')).not.toHaveAttribute('data-initial-move');
  });

  it('shows loading state initially', () => {
    mockFetchAnalysis.mockReturnValue(new Promise(() => {})); // never resolves
    renderPage('/game?id=abc-123');
    expect(screen.getByText('Loading analysis...')).toBeInTheDocument();
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

  // The four poll outcomes must stay distinct: still-processing (active), incomplete
  // (terminal), stale (terminal, board survives), and load failure (no payload ever).
  // These need the poll clock, so they run on fake timers; the cases above do not.
  describe('poll outcomes', () => {
    const POLL_INTERVAL_MS = 2000;
    const POLL_MAX_ATTEMPTS = 60;

    /** An analysis the backend has not finished evaluating: no accuracy yet. */
    const INCOMPLETE = {
      ...ANALYSIS_RESPONSE,
      is_complete: false,
      analyzed_moves: 1,
      summary: { ...ANALYSIS_RESPONSE.summary, accuracy: null },
    };

    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    /** Flush the pending fetch's microtasks without advancing the poll clock. */
    const settle = () =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });

    /** Advance past every scheduled poll. The async variant flushes the microtasks
     *  between polls, so each poll's own timer is picked up in the same call. */
    const exhaustPolls = () =>
      act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS * (POLL_MAX_ATTEMPTS + 1));
      });

    /** The accuracy cell — the only stat that renders the pending state. */
    const accuracyCell = () => document.querySelector('.history-stats-pane__value--you');

    it('keeps the active processing notice while polls are still running', async () => {
      mockFetchAnalysis.mockResolvedValue(INCOMPLETE);

      renderPage('/game?id=abc-123');
      await settle();

      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.getByText(/Analysis still processing/)).toBeInTheDocument();
      expect(accuracyCell()).toHaveTextContent('computing');
      // Neither terminal notice may render while polling continues.
      expect(screen.queryByText(/Analysis incomplete/)).not.toBeInTheDocument();
      expect(screen.queryByText(/showing the last loaded result/)).not.toBeInTheDocument();
    });

    it('stays in the loading state through the retry window, claiming nothing about a payload it never got', async () => {
      mockFetchAnalysis.mockRejectedValue(new Error('Network error'));

      renderPage('/game?id=abc-123');
      await settle();

      expect(screen.getByText('Loading analysis...')).toBeInTheDocument();
      // "still processing" describes the PAYLOAD; there is no payload.
      expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
      expect(screen.queryByText('Network error')).not.toBeInTheDocument();

      expect(mockFetchAnalysis).toHaveBeenCalledTimes(1);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      });
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(2);
    });

    it('renders the board when a retry succeeds after a failed initial fetch', async () => {
      // Guards the unconditional setLoading(false): the retry runs with isInitial ===
      // false, so an isInitial-guarded clear would leave the page on "Loading analysis..."
      // forever with a good payload in hand.
      mockFetchAnalysis
        .mockRejectedValueOnce(new Error('Network error'))
        .mockResolvedValue(ANALYSIS_RESPONSE);

      renderPage('/game?id=abc-123');
      await settle();
      expect(screen.getByText('Loading analysis...')).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      });

      expect(screen.queryByText('Loading analysis...')).not.toBeInTheDocument();
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Analysis incomplete/)).not.toBeInTheDocument();
    });

    it('goes terminal-incomplete when the payload never completes', async () => {
      mockFetchAnalysis.mockResolvedValue(INCOMPLETE);

      renderPage('/game?id=abc-123');
      await settle();
      await exhaustPolls();

      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
      expect(screen.getByText(/Analysis incomplete/)).toBeInTheDocument();
      // Accuracy stops pretending it is coming.
      expect(accuracyCell()).toHaveTextContent('—');
      expect(accuracyCell()).not.toHaveTextContent('computing');

      // Polling has stopped.
      const calls = mockFetchAnalysis.mock.calls.length;
      await exhaustPolls();
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(calls);
    });

    it('keeps the board and goes stale when refresh polls fail transiently to exhaustion', async () => {
      // Regression: the old catch set `error`, and the board gate is `!error`, so a page
      // that had loaded a good payload went BLANK after 60 failed refreshes.
      mockFetchAnalysis
        .mockResolvedValueOnce(INCOMPLETE)
        .mockRejectedValue(new Error('Network error'));

      renderPage('/game?id=abc-123');
      await settle();
      await exhaustPolls();

      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.getByText(/showing the last loaded result/)).toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
      expect(screen.queryByText(/Analysis still processing/)).not.toBeInTheDocument();
    });

    it('keeps the board and goes stale when a refresh poll fails permanently', async () => {
      mockFetchAnalysis
        .mockResolvedValueOnce(INCOMPLETE)
        .mockRejectedValue(new ApiError('Game session not found', { status: 404 }));

      renderPage('/game?id=abc-123');
      await settle();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS);
      });

      // Terminal at the first failing refresh — no need to spend the 60 attempts.
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.getByText(/showing the last loaded result/)).toBeInTheDocument();
      expect(screen.queryByText('Game session not found')).not.toBeInTheDocument();

      const calls = mockFetchAnalysis.mock.calls.length;
      await exhaustPolls();
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(calls);
    });

    it('surfaces a load failure when no payload ever arrives', async () => {
      mockFetchAnalysis.mockRejectedValue(new Error('Network error'));

      renderPage('/game?id=abc-123');
      await settle();
      await exhaustPolls();

      expect(screen.getByText('Failed to load analysis')).toBeInTheDocument();
      expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();
      expect(screen.queryByText(/showing the last loaded result/)).not.toBeInTheDocument();
    });

    it('stops immediately on a permanent error on the initial fetch', async () => {
      mockFetchAnalysis.mockRejectedValue(
        new ApiError('Game session not found', { status: 404 }),
      );

      renderPage('/game?id=bad-id');
      await settle();

      expect(screen.getByText('Game session not found')).toBeInTheDocument();
      expect(screen.queryByTestId('analysis-board')).not.toBeInTheDocument();

      await exhaustPolls();
      expect(mockFetchAnalysis).toHaveBeenCalledTimes(1);
    });

    it('clears a terminal notice when the polled id changes', async () => {
      // The effect's reset is the thing under test, so the switch must happen on a LIVE
      // component — a second render() would remount and pass even with the reset missing.
      function NavHarness() {
        const navigate = useNavigate();
        return (
          <>
            <button onClick={() => navigate('/game?id=s2')}>go-s2</button>
            <GameAnalysisPage />
          </>
        );
      }
      mockFetchAnalysis.mockImplementation((id: string) =>
        id === 's2' ? Promise.resolve(ANALYSIS_RESPONSE) : Promise.resolve(INCOMPLETE),
      );

      render(
        <MemoryRouter initialEntries={['/game?id=s1']}>
          <NavHarness />
        </MemoryRouter>,
      );
      await settle();
      await exhaustPolls();
      expect(screen.getByText(/Analysis incomplete/)).toBeInTheDocument();

      // fireEvent, not userEvent: userEvent's internal delay does not cooperate with the
      // fake clock these poll tests depend on, and the harness button is a plain onClick.
      fireEvent.click(screen.getByRole('button', { name: 'go-s2' }));
      await settle();

      expect(screen.queryByText(/Analysis incomplete/)).not.toBeInTheDocument();
      expect(screen.getByTestId('analysis-board')).toBeInTheDocument();
      expect(screen.queryByText(/Failed to load analysis/)).not.toBeInTheDocument();
    });
  });
});
